"""Turning a stored capability into a concrete, runnable plan.

The artifact on disk holds placeholders. Binding substitutes the caller's arguments to
produce a BoundPlan, which is deliberately a *different type*: it holds real values,
including regulated data, and must never be written anywhere. Keeping it a separate
class means "did we just persist live PII?" is answerable by looking at the type rather
than by auditing every call site.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..surfaces.models import Checkpoint
from .schema import (PLACEHOLDER, Capability, KnownOutcome, OutputField,
                     RecoverableCondition, Step)

REDACTED = "[REDACTED]"


class BindingError(ValueError):
    """Arguments do not satisfy the capability's declared contract."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class BoundPlan(BaseModel):
    """A capability with arguments substituted. Runtime-only - never persist this."""

    capability_id: str
    version: int
    fingerprint: str
    entry_url: str
    steps: list[Step]
    success: Checkpoint
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recoverable: list[RecoverableCondition] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    redact: list[str] = Field(default_factory=list)

    def scrub(self, text: str) -> str:
        """Remove any sensitive bound value from a string headed for a log or evidence."""
        for secret in self.redact:
            if secret:
                text = text.replace(secret, REDACTED)
        return text

    def __repr__(self) -> str:   # keeps secrets out of tracebacks and REPL output
        return (f"BoundPlan({self.capability_id} v{self.version}, "
                f"{len(self.steps)} steps, {len(self.redact)} redacted values)")

    __str__ = __repr__


def validate_args(capability: Capability, args: dict[str, Any]) -> list[str]:
    """Check arguments against the declared inputs. Returns problems, empty when fine."""
    problems: list[str] = []
    declared = {p.name: p for p in capability.inputs}
    for name in args:
        if name not in declared:
            problems.append(f"unexpected argument {name!r}")
    for name, param in declared.items():
        err = param.validate_value(args.get(name))
        if err:
            problems.append(err)
    return problems


def _substitute(blob: Any, values: dict[str, str]) -> Any:
    """Replace {param} in every string, recursively, leaving structure intact."""
    if isinstance(blob, str):
        return PLACEHOLDER.sub(
            lambda m: values.get(m.group(1), m.group(0)), blob)
    if isinstance(blob, list):
        return [_substitute(x, values) for x in blob]
    if isinstance(blob, dict):
        return {k: _substitute(v, values) for k, v in blob.items()}
    return blob


def bind(capability: Capability, args: dict[str, Any]) -> BoundPlan:
    """Validate arguments and substitute them into the plan.

    Raises BindingError rather than binding partially: a plan half-filled with
    placeholders would type literal "{member_id}" into a live banking screen.
    """
    problems = validate_args(capability, args)
    if problems:
        raise BindingError(problems)

    values = {p.name: ("" if args.get(p.name) is None else str(args[p.name]))
              for p in capability.inputs}
    sensitive = [values[p.name] for p in capability.inputs
                 if p.sensitive and values.get(p.name)]

    raw = capability.model_dump(mode="json")
    bound = _substitute(
        {k: raw[k] for k in ("steps", "success", "known_outcomes", "recoverable")},
        values)

    # Mark fills of sensitive parameters so the surface and the logger both know.
    for step in bound["steps"]:
        action = step.get("action", {})
        if action.get("kind") == "fill" and action.get("text") in sensitive:
            action["secret"] = True

    return BoundPlan(
        capability_id=capability.id,
        version=capability.version,
        fingerprint=capability.fingerprint(),
        entry_url=_substitute(capability.surface.entry_url, values),
        steps=[Step.model_validate(s) for s in bound["steps"]],
        success=Checkpoint.model_validate(bound["success"]),
        known_outcomes=[KnownOutcome.model_validate(k) for k in bound["known_outcomes"]],
        recoverable=[RecoverableCondition.model_validate(r) for r in bound["recoverable"]],
        outputs=list(capability.outputs),
        redact=sensitive,
    )
