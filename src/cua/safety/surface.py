"""A Surface that enforces policy, wrapping any other Surface.

Written as a decorator rather than as checks inside the engines, for one reason: a
guardrail that the caller has to remember to consult is not a guardrail. Every action
reaches the real surface through this object, so an allowlist violation is impossible
regardless of what the discovery loop or the replay engine believes it is doing.

It also closes the redaction loop. A fill of a value declared sensitive registers that
value with the redactor as it happens, so it is scrubbed from every later observation
without anyone having to remember to pass it along.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..surfaces.base import Surface
from ..surfaces.models import (Action, ActionResult, CheckResult, Checkpoint, Locator,
                               Observation, Resolution)
from .policy import Decision, Policy
from .redaction import Redactor

# Asked to approve a CONFIRM verdict. Returns True to permit. The default refuses:
# unattended execution must not silently self-approve a risky action.
Approver = Callable[[Action, str], bool]


def _deny(action: Action, reason: str) -> bool:
    return False


class PolicySurface:
    """Wraps a Surface, enforcing an allowlist and scrubbing what comes back out."""

    def __init__(self, inner: Surface, policy: Policy,
                 redactor: Redactor | None = None,
                 approver: Approver | None = None):
        self.inner = inner
        self.policy = policy
        self.redactor = redactor or Redactor()
        self.approver = approver or _deny
        self.name = f"policy({getattr(inner, 'name', 'surface')})"
        self.violations: list[str] = []

    # -------------------------------------------------------------- perceive
    def observe(self) -> Observation:
        return self.redactor.scrub_observation(self.inner.observe())

    def resolve(self, locator: Locator) -> Resolution:
        return self.inner.resolve(locator)

    def check(self, checkpoint: Checkpoint) -> CheckResult:
        return self.inner.check(checkpoint)

    def current_url(self) -> str:
        return self.inner.current_url()

    # ------------------------------------------------------------------ act
    def act(self, action: Action) -> ActionResult:
        verdict = self.policy.check_action(action)

        if verdict.decision is Decision.BLOCK:
            self.violations.append(verdict.reason)
            return ActionResult(ok=False, blocked=True,
                                detail=f"blocked by policy [{verdict.rule}]: {verdict.reason}")

        if verdict.decision is Decision.CONFIRM and not self.approver(action, verdict.reason):
            return ActionResult(ok=False, blocked=True,
                                detail=f"not approved [{verdict.rule}]: {verdict.reason}")

        # A value the caller declared sensitive must never surface again in a log,
        # an observation or an artifact - register it the moment it is used.
        if getattr(action, "kind", "") == "fill" and getattr(action, "secret", False):
            self.redactor.add(action.text)

        result = self.inner.act(action)

        # A click can navigate. Checking only the requested URL would let the surface
        # be carried somewhere the policy forbids by something the page did.
        landed = self.inner.current_url()
        if landed and not self.policy.url_allowed(landed):
            self.violations.append(f"navigated to {landed!r} outside the allowlist")
            return ActionResult(
                ok=False, blocked=True, resolution=result.resolution,
                detail=f"action left the permitted surface: {landed!r} is not allowed")

        if result.value:
            result = result.model_copy(update={"value": self.redactor.scrub(result.value)})
        return result

    # ------------------------------------------------------------- evidence
    def capture(self, label: str, into: Path) -> Path | None:
        """Screenshots cannot be scrubbed after the fact - a rendered value is pixels.
        Evidence capture is therefore a deliberate act by the caller, and the policy's
        answer to sensitive screens is not to capture them rather than to mask them."""
        return self.inner.capture(label, into)

    def close(self) -> None:
        self.inner.close()

    def __enter__(self) -> "PolicySurface":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
