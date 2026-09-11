"""Transferring control of a live session to a person, and taking it back.

The seam this implies, stated plainly: automation must be able to pause, cede control,
and resume *on the same session*, and at every moment it must be knowable who is in
control. That is why this is a Surface wrapper rather than a helper the engines call -
while a human holds control, an automation action is refused rather than merely
discouraged, so a stray retry cannot fight the operator for the keyboard.

What is real here and what is mocked, deliberately:

* Real: the control-transfer protocol, the single-holder guarantee, the request payload,
  the resume signal, and the before/after diff that records what the human changed.
* Real: the session itself. A headed run hands over the actual browser window the
  automation was driving - not a fresh one, not a copy.
* Mocked: the operator console is a CLI (`cua operator`). The brief puts a real-time
  co-browsing console out of scope; the seam it would plug into is the broker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..surfaces.base import Surface
from ..surfaces.models import (Action, ActionResult, CheckResult, Checkpoint, Locator,
                               Observation, Resolution)
from ..surfaces.models import Role
from .broker import InterventionBroker
from .protocol import Control, Handoff, InterventionRequest, RequestState

# An operator adapter is whatever actually performs the human's work on the paused
# session. In production that is a person at the headed browser and the adapter is
# absent - the broker's claim/release is the signal. An adapter is how anything else
# plugs into the same seam: an operator console driving the session on the person's
# behalf, or a scripted remediation for a condition a team has decided to automate.
# It receives the live surface and the request, and returns the operator's name.
OperatorAdapter = "Callable[[Surface, InterventionRequest], str]"


# Only controls a person can actually change. Static text moving around is the
# application responding, not the operator acting.
_EDITABLE = {Role.TEXTBOX, Role.COMBOBOX, Role.CHECKBOX, Role.RADIO}


def diff_observations(before: Observation, after: Observation) -> list[str]:
    """Describe what changed between two snapshots, in operator-readable terms.

    Deliberately shallow. The point is an auditable record of what a person did to a
    banking session, not a faithful reconstruction - and a short, true list is more use
    to a reviewer than an exhaustive one nobody reads.
    """
    changes: list[str] = []
    if before.url != after.url:
        changes.append(f"navigated from {before.url} to {after.url}")

    def keyed(observation: Observation) -> dict[str, str]:
        """Values a person would recognise, keyed by what they are called on screen.

        Only controls with a human-meaningful name: a caption, an accessible name or a
        column heading. A generated id or an element reference identifies nothing to
        somebody reading this later, and listing every layout cell buries the one line
        that matters.
        """
        out: dict[str, str] = {}
        for element in observation.elements:
            if element.role not in _EDITABLE:
                continue
            name = (element.label_text or element.name or element.column_header or "")
            name = name.strip().rstrip(":")
            if not name or element.value is None:
                continue
            out[name] = element.value
        return out

    old, new = keyed(before), keyed(after)
    for name, value in new.items():
        was = old.get(name)
        if was is None or was == value:
            continue
        changes.append(f"changed {name} from {was!r} to {value!r}")
    for name in sorted(new.keys() - old.keys()):
        if new[name]:
            changes.append(f"entered {new[name]!r} in {name}")
    if not changes and before.text_digest != after.text_digest:
        changes.append("the screen moved on, but no field value was edited")
    return changes


class ControlledSurface:
    """A Surface that can be handed to a person and taken back."""

    def __init__(self, inner: Surface, broker: InterventionBroker,
                 recorder=None, session_hint: str = "",
                 operator_timeout_s: float = 300.0,
                 operator_adapter=None):
        self.inner = inner
        self.broker = broker
        self.recorder = recorder
        self.session_hint = session_hint
        self.operator_timeout_s = operator_timeout_s
        self.operator_adapter = operator_adapter
        self.control = Control.AUTOMATION
        self.handoffs: list[Handoff] = []
        self.name = f"controlled({getattr(inner, 'name', 'surface')})"
        # Policy lives beneath control transfer: a human may act freely, but every
        # automated action still passes the allowlist underneath this wrapper.
        self.guarded = getattr(inner, "guarded", False)

    # ---------------------------------------------------------------- surface
    def observe(self) -> Observation:
        return self.inner.observe()

    def resolve(self, locator: Locator) -> Resolution:
        return self.inner.resolve(locator)

    def check(self, checkpoint: Checkpoint) -> CheckResult:
        return self.inner.check(checkpoint)

    def current_url(self) -> str:
        return self.inner.current_url()

    def capture(self, label: str, into: Path) -> Path | None:
        return self.inner.capture(label, into)

    def act(self, action: Action) -> ActionResult:
        if self.control is Control.HUMAN:
            return ActionResult(
                ok=False, blocked=True,
                detail="a human holds control of this session; automation is paused")
        return self.inner.act(action)

    def close(self) -> None:
        self.inner.close()

    # -------------------------------------------------------------- handoff
    def escalate(self, reason: str, evidence_dir: Path | None = None,
                 **context) -> Handoff:
        """Pause, hand the live session to a person, wait, and take it back."""
        before = self.inner.observe()
        screenshot = None
        if evidence_dir is not None:
            shot = self.inner.capture("escalation", evidence_dir)
            screenshot = shot.name if shot else None

        request = self.broker.raise_request(InterventionRequest(
            reason=reason, url=self.inner.current_url(), screenshot=screenshot,
            session_hint=self.session_hint or "the live browser session this run is using",
            **context))

        self.control = Control.HUMAN          # refuse automation from this moment
        ceded_at = datetime.now(timezone.utc)
        self._log("escalated", reason, request=request.id, step=context.get("step_index"))

        if self.operator_adapter is not None:
            # An adapter works the session directly. It reaches past this wrapper on
            # purpose: while control is ceded, the operator is not subject to the
            # automation's own pause.
            operator = self.broker.claim(request.id, "adapter").operator
            try:
                operator = self.operator_adapter(self.inner, request) or operator
                self.broker.release(request.id, notes=f"handled by {operator}")
            except Exception as exc:
                self.broker.abandon(request.id, notes=f"operator adapter failed: {exc}")
            settled = self.broker.load(request.id)
            if settled.state is RequestState.RELEASED:
                settled.operator = operator
                # An adapter may have set a resume point on the request it was handed.
                settled.resume_from_step = (settled.resume_from_step
                                            if settled.resume_from_step is not None
                                            else request.resume_from_step)
        else:
            settled = self.broker.wait_for_release(request.id, self.operator_timeout_s)

        after = self.inner.observe()
        changes = diff_observations(before, after)
        if settled.state is RequestState.RELEASED and not settled.human_changes:
            self.broker.release(request.id, settled.operator_notes, changes)

        self.control = Control.AUTOMATION      # take it back
        handoff = Handoff(request_id=request.id, ceded_at=ceded_at,
                          reclaimed_at=datetime.now(timezone.utc),
                          operator=settled.operator, notes=settled.operator_notes,
                          changes=changes,
                          resumed_from_step=(settled.resume_from_step
                                             if settled.resume_from_step is not None
                                             else context.get("step_index")))
        self.handoffs.append(handoff)
        self._log("control_returned",
                  f"{settled.state.value} by {settled.operator or 'nobody'}",
                  request=request.id, changes=changes)
        return handoff

    def _log(self, kind: str, message: str, **data) -> None:
        if self.recorder is not None:
            self.recorder.event(kind, message, **data)
