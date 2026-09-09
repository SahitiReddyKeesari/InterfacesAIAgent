"""The capability artifact: what a discovery run produces and replay consumes.

This is a *contract*, not a step list. Two different readers have to understand it
without help: a human reviewer approving it for unattended use, and an AI agent
deciding whether it is the right capability to call and what to pass it. Everything in
here exists to serve one of those two readers.

Three design commitments worth stating up front, because they shape everything else:

1. **Values never live in the artifact - only placeholders.** A recorded step fills the
   member field with `{member_id}`, never with `12345`. That is what makes one recording
   serve every member, and it is also the redaction story: a saved artifact physically
   cannot contain the PII of whoever it was recorded against.

2. **Success is not the only declared ending.** A capability that only knows what
   success looks like must treat "no such member" as a failure, which is wrong - it is a
   legitimate answer the caller asked for. So known business outcomes and recoverable
   conditions are declared alongside the success checkpoint, each with its own
   recognisable signature. Anything that matches none of them is a hard failure.

3. **Robustness reasoning is recorded, not just the selector.** Every locator carries
   why it identifies a control that way. A reviewer approving unattended execution needs
   to see the reasoning, and a locator that can only be justified by position is a
   capability that should not be approved.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..surfaces.models import Action, Checkpoint, Locator

SCHEMA_VERSION = "1.0"
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Human-facing prose. Excluded from the content fingerprint: rewording a justification
# does not change what a plan does, and forcing re-approval for a typo fix trains
# reviewers to rubber-stamp version bumps.
PROSE_KEYS = {"rationale", "description", "intent", "notes"}


def _strip_prose(blob: Any) -> Any:
    if isinstance(blob, dict):
        return {k: _strip_prose(v) for k, v in blob.items() if k not in PROSE_KEYS}
    if isinstance(blob, list):
        return [_strip_prose(x) for x in blob]
    return blob


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    ENUM = "enum"


class RiskClass(str, Enum):
    """How much damage a step can do if it runs when it should not.

    The split is by *reversibility*, not by how it is spelled. Reading a balance and
    filling a form are both SAFE because nothing survives them. Opening an account is
    IRREVERSIBLE because a record now exists. Blocking a card is IRREVERSIBLE and
    terminal - there is no undo path in the target application at all.
    """

    SAFE = "safe"                  # read-only or trivially undone
    CONSEQUENTIAL = "consequential"  # writes state that can be reversed by another flow
    IRREVERSIBLE = "irreversible"  # cannot be undone through the UI


class Outcome(str, Enum):
    """How a replay ended. The distinction `BUSINESS` vs `HARD` is the whole point:
    conflating them is the single most common design mistake in this problem."""

    SUCCESS = "success"
    BUSINESS = "business_outcome"
    RECOVERED = "recovered"
    HARD_FAILURE = "hard_failure"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    ESCALATED = "escalated"


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class InputParam(BaseModel):
    """One value the calling agent supplies per invocation."""

    name: str
    type: ParamType = ParamType.STRING
    description: str
    required: bool = True
    example: str | None = None
    enum_values: list[str] | None = None
    pattern: str | None = None
    sensitive: bool = False   # never logged, never written to evidence

    @model_validator(mode="after")
    def _no_example_for_secrets(self) -> "InputParam":
        """An example of a tax identifier is a tax identifier. Documentation is not an
        exemption from redaction, so a sensitive input simply has no example."""
        if self.sensitive and self.example:
            self.example = None
        return self

    def validate_value(self, value: Any) -> str | None:
        """Return an error message, or None when the value is acceptable."""
        text = "" if value is None else str(value)
        if not text and self.required:
            return f"{self.name} is required"
        if not text:
            return None
        if self.type is ParamType.INTEGER and not re.fullmatch(r"-?\d+", text):
            return f"{self.name} must be an integer, got {text!r}"
        if self.type is ParamType.DECIMAL and not re.fullmatch(r"-?\d+(\.\d+)?", text):
            return f"{self.name} must be a decimal, got {text!r}"
        if self.type is ParamType.ENUM and self.enum_values and text not in self.enum_values:
            return f"{self.name} must be one of {self.enum_values}, got {text!r}"
        if self.pattern and not re.fullmatch(self.pattern, text):
            return f"{self.name} does not match {self.pattern}"
        return None


class OutputField(BaseModel):
    """One value the caller gets back. `source_step` ties it to the Read that produces
    it, so a reviewer can see where a returned value actually came from."""

    name: str
    type: ParamType = ParamType.STRING
    description: str
    sensitive: bool = False
    source_step: int | None = None


class KnownOutcome(BaseModel):
    """A legitimate non-success ending the caller needs to be told about by name.

    "No such member" belongs here. It is an answer, not a crash, and a caller that
    receives it should be able to branch on `name` rather than parse an error string.
    """

    name: str
    description: str
    signature: Checkpoint
    outcome: Outcome = Outcome.BUSINESS


class RecoverableCondition(BaseModel):
    """Something replay is expected to meet and handle without giving up.

    The recovery is a recorded action sequence, not model reasoning - a session timeout
    is dismissed the same way every time, and letting an LLM improvise here would put a
    model back in the production path.
    """

    name: str
    description: str
    signature: Checkpoint
    recovery: list[Action] = Field(default_factory=list)
    max_attempts: int = 2
    retry_from_step: int | None = None   # None = retry the failing step only


class Step(BaseModel):
    """One recorded action, with the postcondition that proves it landed."""

    index: int
    intent: str                      # why this step exists, in plain language
    action: Action
    checkpoint: Checkpoint | None = None
    risk: RiskClass = RiskClass.SAFE
    optional: bool = False           # skipping it is not a failure
    notes: str = ""


class SurfaceBinding(BaseModel):
    """What kind of surface this was recorded against, and where it starts.

    `kind` is what lets a replay refuse to run a web recording against a desktop
    surface. `tenant` and `variant` are unused today but reserved deliberately: the
    multi-tenant story is per-tenant overrides on a shared capability, and leaving no
    room for them here is exactly the corner this schema must not paint us into.
    """

    kind: str = "web"
    entry_url: str
    tenant: str | None = None
    variant: str | None = None


class Provenance(BaseModel):
    """How this artifact came to exist. A reviewer's first question is 'where did this
    come from', and an artifact that cannot answer it should not be approved."""

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    goal: str = ""
    model: str | None = None
    run_id: str | None = None
    discovery_steps: int = 0
    weakest_locator: float | None = None   # lowest top-candidate confidence in the plan


class Capability(BaseModel):
    """A reusable, reviewable, parameterised capability an agent can invoke."""

    schema_version: str = SCHEMA_VERSION
    id: str
    version: int = 1
    name: str
    description: str
    surface: SurfaceBinding
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    success: Checkpoint
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recoverable: list[RecoverableCondition] = Field(default_factory=list)
    approval: ApprovalState = ApprovalState.DRAFT
    provenance: Provenance = Field(default_factory=Provenance)

    # ------------------------------------------------------------------ integrity
    def fingerprint(self) -> str:
        """Content hash over the executable parts only.

        Excludes provenance and approval, so re-approving or re-recording the same flow
        does not look like a change. Also excludes prose - rationales, intents,
        descriptions - because rewording a justification does not alter what the plan
        does, and forcing re-approval for a typo fix trains reviewers to rubber-stamp
        version bumps. What this detects is a plan that would *behave* differently.
        """
        payload = _strip_prose(self.model_dump(
            mode="json",
            include={"steps", "inputs", "outputs", "success",
                     "known_outcomes", "recoverable", "surface"}))
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def placeholders(self) -> set[str]:
        """Every {param} referenced anywhere in the plan."""
        blob = json.dumps(self.model_dump(mode="json",
                                          include={"steps", "success", "known_outcomes",
                                                   "recoverable", "surface"}))
        return set(PLACEHOLDER.findall(blob))

    def validate_contract(self) -> list[str]:
        """Self-consistency problems a reviewer should never have to find by hand."""
        problems: list[str] = []
        declared = {p.name for p in self.inputs}
        used = self.placeholders()
        for missing in sorted(used - declared):
            problems.append(f"step references {{{missing}}} but no input declares it")
        for unused in sorted(declared - used):
            problems.append(f"input {unused!r} is declared but never used")
        if self.success.is_empty():
            problems.append("success checkpoint is empty - replay could not verify anything")
        produced = {s.action.output for s in self.steps
                    if getattr(s.action, "kind", "") == "read"}
        for out in self.outputs:
            if out.name not in produced:
                problems.append(f"output {out.name!r} is declared but no step reads it")
        risky = [s.index for s in self.steps if s.risk is RiskClass.IRREVERSIBLE]
        if risky and self.approval is ApprovalState.APPROVED and not self.known_outcomes:
            problems.append(f"steps {risky} are irreversible but no known outcomes are "
                            f"declared; unattended approval is unsafe")
        return problems

    def risk_profile(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.risk.value] = counts.get(s.risk.value, 0) + 1
        return counts

    # -------------------------------------------------------------- agent-facing
    def tool_schema(self) -> dict[str, Any]:
        """JSON Schema describing this capability as a callable tool.

        This is what makes 'an agent can invoke it' concrete rather than aspirational:
        the same artifact that a human reviews is the thing that generates the function
        signature the calling agent sees.
        """
        json_types = {ParamType.INTEGER: "integer", ParamType.DECIMAL: "number",
                      ParamType.BOOLEAN: "boolean"}
        props: dict[str, Any] = {}
        for p in self.inputs:
            spec: dict[str, Any] = {"type": json_types.get(p.type, "string"),
                                    "description": p.description}
            if p.enum_values:
                spec["enum"] = p.enum_values
            if p.example:
                spec["examples"] = [p.example]
            props[p.name] = spec
        return {
            "name": self.id,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.inputs if p.required],
            },
            "returns": {o.name: o.description for o in self.outputs},
            "known_outcomes": {k.name: k.description for k in self.known_outcomes},
        }

    # ---------------------------------------------------------------- reviewable
    def review(self) -> str:
        """A human-readable rendering of the contract, for approval."""
        lines = [
            f"# {self.name}  (v{self.version}, {self.approval.value})",
            f"{self.description}",
            f"\n**id** `{self.id}` · **fingerprint** `{self.fingerprint()}` · "
            f"**surface** {self.surface.kind} @ {self.surface.entry_url}",
        ]
        if self.inputs:
            lines.append("\n## Inputs")
            for p in self.inputs:
                flag = " *(sensitive)*" if p.sensitive else ""
                req = "required" if p.required else "optional"
                lines.append(f"- `{p.name}`: {p.type.value}, {req}{flag} — {p.description}")
        if self.outputs:
            lines.append("\n## Outputs")
            for o in self.outputs:
                lines.append(f"- `{o.name}`: {o.type.value} — {o.description}")
        lines.append("\n## Plan")
        for s in self.steps:
            loc = getattr(s.action, "locator", None)
            how = ""
            if isinstance(loc, Locator) and loc.candidates:
                top = loc.candidates[0]
                how = f" · found by {top.strategy.value} ({top.confidence:.2f})"
            mark = {"safe": "", "consequential": " ⚠", "irreversible": " ⛔"}[s.risk.value]
            lines.append(f"{s.index}. **{s.action.kind}**{mark} — {s.intent}{how}")
            if isinstance(loc, Locator) and loc.candidates:
                lines.append(f"   - _{loc.candidates[0].rationale}_")
            if s.checkpoint:
                lines.append(f"   - verifies: {s.checkpoint.description}")
        lines.append(f"\n## Success\n{self.success.description}")
        if self.known_outcomes:
            lines.append("\n## Known business outcomes")
            for k in self.known_outcomes:
                lines.append(f"- `{k.name}` — {k.description}")
        if self.recoverable:
            lines.append("\n## Recoverable conditions")
            for r in self.recoverable:
                lines.append(f"- `{r.name}` — {r.description} "
                             f"(up to {r.max_attempts} attempts)")
        return "\n".join(lines)
