"""Tests for human-in-the-loop control transfer.

The properties worth pinning are not the queue mechanics but the control model: exactly
one party holds the session, automation is genuinely refused while a human has it, the
request carries enough context to act on, and what the person did is recorded.
"""
from __future__ import annotations

import threading
import time

import pytest

from cua.artifact.schema import Outcome
from cua.escalation.broker import InterventionBroker
from cua.escalation.protocol import Control, InterventionRequest, RequestState
from cua.escalation.session import ControlledSurface, diff_observations
from cua.replay.engine import ReplayEngine
from cua.safety.policy import for_host
from cua.safety.surface import PolicySurface
from cua.surfaces.models import Element, Navigate, Observation, Role
from cua.surfaces.web import PlaywrightSurface
from tests.fixtures.meridian_capability import build

GOOD = {"member_id": "12345", "account_type": "Savings"}


@pytest.fixture()
def broker(tmp_path):
    return InterventionBroker(tmp_path / "interventions")


def a_request(**over) -> InterventionRequest:
    base = dict(capability_id="demo.cap", goal="do the thing", step_index=2,
                step_intent="click submit", reason="checkpoint failed",
                expected="Confirmation", observed="validation error")
    base.update(over)
    return InterventionRequest(**base)


# ---------------------------------------------------------------------- queue
def test_a_request_carries_enough_context_to_act_on(broker):
    """'It failed' makes the operator start from nothing."""
    request = broker.raise_request(a_request())
    brief = request.brief()
    for expected in ("do the thing", "step 2", "Confirmation", "validation error"):
        assert expected in brief


def test_a_session_cannot_be_handed_to_two_people(broker):
    request = broker.raise_request(a_request())
    broker.claim(request.id, "first")
    with pytest.raises(ValueError, match="not open"):
        broker.claim(request.id, "second")


def test_releasing_records_what_the_operator_did(broker):
    request = broker.raise_request(a_request())
    broker.claim(request.id, "mthompson")
    released = broker.release(request.id, notes="corrected the date",
                              changes=["set Opening Date to 2026-01-04"])
    assert released.state is RequestState.RELEASED
    assert released.operator == "mthompson"
    assert released.human_changes == ["set Opening Date to 2026-01-04"]


def test_an_unclaimed_request_is_abandoned_not_left_hanging(broker):
    """A session nobody holds must not look like one somebody is working on."""
    request = broker.raise_request(a_request())
    settled = broker.wait_for_release(request.id, timeout_s=0.5, poll_s=0.1)
    assert settled.state is RequestState.ABANDONED


def test_open_requests_exclude_ones_already_taken(broker):
    first = broker.raise_request(a_request())
    broker.raise_request(a_request())
    broker.claim(first.id, "someone")
    assert [r.id for r in broker.open_requests()] != [first.id]
    assert len(broker.open_requests()) == 1


# ----------------------------------------------------------------- the diff
def test_the_diff_describes_what_a_person_changed():
    before = Observation(url="u1", title="t", elements=[
        Element(ref="a", role=Role.TEXTBOX, label_text="Amount:", value="0.00")])
    after = Observation(url="u1", title="t", elements=[
        Element(ref="a", role=Role.TEXTBOX, label_text="Amount:", value="250.00"),
        Element(ref="b", role=Role.TEXTBOX, label_text="Reason:", value="manual")])
    changes = diff_observations(before, after)
    assert any("Amount" in c and "250.00" in c for c in changes)
    assert any("Reason" in c for c in changes)


# ------------------------------------------------------------ control model
@pytest.fixture()
def controlled(base_url, broker):
    surface = PlaywrightSurface()
    guarded = PolicySurface(surface, for_host(base_url + "/meridian/"))
    controlled = ControlledSurface(guarded, broker, session_hint="test session",
                                   operator_timeout_s=6.0)
    try:
        yield controlled
    finally:
        controlled.close()


def test_automation_is_refused_while_a_human_holds_control(controlled, base_url, srv):
    controlled.control = Control.HUMAN
    result = controlled.act(Navigate(url=base_url + "/meridian/"))
    assert result.blocked is True
    assert "human holds control" in result.detail


def test_control_returns_to_automation_after_a_handoff(controlled, base_url, srv, broker):
    controlled.act(Navigate(url=base_url + "/meridian/"))

    def operator():
        deadline = time.time() + 5
        while time.time() < deadline:
            open_now = broker.open_requests()
            if open_now:
                broker.claim(open_now[0].id, "mthompson")
                broker.release(open_now[0].id, notes="looked at it")
                return
            time.sleep(0.1)

    threading.Thread(target=operator, daemon=True).start()
    handoff = controlled.escalate(reason="stuck", goal="g", step_index=1,
                                  step_intent="do a thing")
    assert controlled.control is Control.AUTOMATION
    assert handoff.operator == "mthompson"
    assert handoff.reclaimed_at is not None


def test_policy_is_still_enforced_beneath_control_transfer(controlled, srv):
    """Wrapping for handoff must not quietly remove the allowlist."""
    assert controlled.guarded is True
    assert controlled.act(Navigate(url="https://example.com/")).blocked is True


# ------------------------------------------------------- replay integration
def test_replay_escalates_and_resumes_on_the_same_session(base_url, tmp_path, srv):
    """The whole point: a run that could not continue alone finishes after a person
    intervenes on the same session, without starting over."""
    capability = build(base_url)
    broker = InterventionBroker(tmp_path / "interventions")
    handled: list[str] = []

    def operator(surface, request):
        """Stands in for a person at the paused session: gets the app off the error
        screen and back to a usable state, exactly as an operator would."""
        handled.append(request.id)
        surface.act(Navigate(url=base_url + "/meridian/"))
        # Resetting the app discards what earlier steps typed, so the operator says
        # where it is safe to pick up rather than letting it retry a step whose
        # preconditions no longer hold.
        request.resume_from_step = 0
        return "mthompson"

    surface = PlaywrightSurface()
    guarded = PolicySurface(surface, for_host(base_url + "/meridian/"))
    controlled = ControlledSurface(guarded, broker, session_hint="test session",
                                   operator_adapter=operator)

    srv.arm("server_error")          # one failure, then the surface behaves again
    try:
        result = ReplayEngine(controlled, for_host(base_url + "/meridian/"),
                              evidence_root=tmp_path).run(capability, GOOD)
    finally:
        controlled.close()

    assert handled, "the run should have asked for help"
    assert result.escalations == handled
    assert result.outcome is Outcome.RECOVERED
    assert result.human_changes, "what the operator did must be recorded"


def test_replay_reports_escalated_when_nobody_takes_it(base_url, tmp_path, srv):
    capability = build(base_url)
    broker = InterventionBroker(tmp_path / "interventions")
    surface = PlaywrightSurface()
    guarded = PolicySurface(surface, for_host(base_url + "/meridian/"))
    controlled = ControlledSurface(guarded, broker, operator_timeout_s=1.0)

    srv.arm("server_error", 5)       # keeps failing, so no retry can save it
    try:
        result = ReplayEngine(controlled, for_host(base_url + "/meridian/"),
                              evidence_root=tmp_path).run(capability, GOOD)
    finally:
        controlled.close()

    assert result.outcome is Outcome.ESCALATED
    assert "no operator" in result.error
