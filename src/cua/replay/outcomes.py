"""The result contract a caller receives from a replay.

The shape of this type is the answer to the brief's central warning: conflating a
business outcome with a failure is the most common design mistake in this problem.
"No such member" is an answer the caller asked for. A 500 is not. They must not arrive
looking the same, so `outcome` is a closed enum the caller can branch on and every
non-success carries the detail needed to act or to debug.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..artifact.schema import Outcome


class StepTrace(BaseModel):
    """What one step did - the debugging record, one entry per attempt."""

    index: int
    intent: str
    action: str
    ok: bool
    strategy: str | None = None
    confidence: float = 0.0
    checkpoint_passed: bool | None = None
    detail: str = ""

    @property
    def weak(self) -> bool:
        """Carried by a low-trust candidate. Resolving weakly is not success - a
        positional match can find exactly one control and still be the wrong record."""
        return self.ok and 0 < self.confidence < 0.5


class ReplayResult(BaseModel):
    """Everything the caller needs, and everything a human needs to debug."""

    outcome: Outcome
    capability_id: str
    version: int
    fingerprint: str

    # SUCCESS
    outputs: dict[str, str] = Field(default_factory=dict)

    # BUSINESS - a legitimate answer, named so the caller can branch on it
    business_outcome: str | None = None
    business_detail: str = ""

    # RECOVERED - handled without giving up, recorded because repeated recovery is
    # a signal the surface has changed even though the run succeeded
    recoveries: list[str] = Field(default_factory=list)

    # HARD_FAILURE / BLOCKED_BY_POLICY
    failed_step: int | None = None
    expected: str = ""
    observed: str = ""
    error: str = ""

    # Always
    steps: list[StepTrace] = Field(default_factory=list)
    run_id: str = ""
    evidence_dir: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when the capability completed, with or without recovery. A business
        outcome is a valid answer but not a completed capability, so callers must
        handle it explicitly rather than reading it as failure."""
        return self.completed

    @property
    def weak_steps(self) -> list[int]:
        """Steps that only resolved via a low-confidence fallback - a drift warning
        even on a run that passed."""
        return [s.index for s in self.steps if s.weak]

    @property
    def completed(self) -> bool:
        """The capability did what it was asked. RECOVERED is a success that had to
        work for it - the caller gets its outputs either way, and the distinction
        exists so repeated recovery can be noticed rather than hidden."""
        return self.outcome in (Outcome.SUCCESS, Outcome.RECOVERED)

    def summary(self) -> str:
        if self.completed:
            body = ", ".join(f"{k}={v}" for k, v in self.outputs.items()) or "no outputs"
            notes = []
            if self.recoveries:
                notes.append(f"recovered from {', '.join(self.recoveries)}")
            if self.weak_steps:
                notes.append(f"weak steps: {self.weak_steps}")
            suffix = f"  ({'; '.join(notes)})" if notes else ""
            return f"{self.outcome.value}: {body}{suffix}"
        if self.outcome is Outcome.BUSINESS:
            return f"business outcome '{self.business_outcome}': {self.business_detail}"
        if self.outcome is Outcome.BLOCKED_BY_POLICY:
            return f"blocked at step {self.failed_step}: {self.error}"
        return (f"{self.outcome.value} at step {self.failed_step}: {self.error}\n"
                f"  expected: {self.expected}\n  observed: {self.observed}")
