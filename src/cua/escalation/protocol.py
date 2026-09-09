"""The control-transfer model.

The hard part of human-in-the-loop is not showing someone a screenshot - it is that the
human must operate *the same live session* the automation was using, and then give it
back. That imposes three things this module makes explicit:

* **Exactly one party holds control at a time.** Not a convention: while a human holds
  it, the automation's actions are refused, so a stray retry cannot fight the operator
  for the keyboard mid-transaction.
* **The request must carry enough to act on.** Which capability, which step, what was
  expected, what was seen, and a picture. An intervention request that says "it failed"
  makes the operator start from nothing.
* **What the human did has to be recorded.** The automation resumes on a session someone
  else has changed, so the run's evidence must show what changed and who changed it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Control(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class RequestState(str, Enum):
    OPEN = "open"           # raised, nobody has picked it up
    CLAIMED = "claimed"     # an operator holds control of the live session
    RELEASED = "released"   # control handed back; the run may resume
    ABANDONED = "abandoned"  # nobody took it in time, or the operator gave up


class InterventionRequest(BaseModel):
    """A request for a person to take over, carrying the context to act on it."""

    id: str = Field(default_factory=lambda: f"iv-{uuid.uuid4().hex[:10]}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state: RequestState = RequestState.OPEN

    run_id: str = ""
    capability_id: str = ""
    goal: str = ""
    step_index: int | None = None
    step_intent: str = ""

    reason: str = ""            # why the automation stopped
    expected: str = ""
    observed: str = ""
    url: str = ""
    screenshot: str | None = None
    session_hint: str = ""      # how to reach the live session

    operator: str | None = None
    claimed_at: datetime | None = None
    released_at: datetime | None = None
    operator_notes: str = ""
    human_changes: list[str] = Field(default_factory=list)
    # Where automation should pick up. Retrying only the failed step is the default,
    # but an operator who reset the application has invalidated earlier steps too -
    # so the person handing control back gets to say where it is safe to resume.
    resume_from_step: int | None = None

    def brief(self) -> str:
        """What an operator sees before deciding to take it."""
        lines = [
            f"[{self.id}] {self.capability_id or 'discovery'} - {self.reason}",
            f"  goal     : {self.goal}",
            f"  stopped  : step {self.step_index} ({self.step_intent})"
            if self.step_index is not None else "  stopped  : before any step",
            f"  expected : {self.expected}",
            f"  observed : {self.observed}",
            f"  session  : {self.session_hint or 'not reachable'}",
        ]
        if self.screenshot:
            lines.append(f"  screen   : {self.screenshot}")
        return "\n".join(lines)


class Handoff(BaseModel):
    """The record of one control transfer, written into the run's evidence."""

    request_id: str
    ceded_at: datetime
    reclaimed_at: datetime | None = None
    operator: str | None = None
    notes: str = ""
    changes: list[str] = Field(default_factory=list)
    resumed_from_step: int | None = None
