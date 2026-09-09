"""Tests for deterministic replay.

The point of most of these is the outcome taxonomy. A replay that reports "no such
member" and a replay that reports a 500 must not look alike to the caller, and a replay
that had to recover must say so even though it succeeded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.artifact.schema import Outcome, RiskClass
from cua.replay.engine import ReplayEngine
from cua.safety.policy import Policy, for_host
from cua.surfaces.web import PlaywrightSurface
from tests.fixtures.meridian_capability import build

GOOD = {"member_id": "12345", "account_type": "Savings"}


@pytest.fixture(scope="module")
def capability(base_url):
    return build(base_url)


@pytest.fixture()
def replay(base_url, tmp_path):
    """A fresh engine per test, writing evidence into the test's own directory."""
    def _run(capability, args=None, policy=None, approver=None, evidence=True):
        # One browser per run, closed immediately: Playwright's sync API does not
        # allow two live instances in the same thread.
        surface = PlaywrightSurface()
        try:
            engine = ReplayEngine(
                surface, policy or for_host(base_url + "/meridian/"),
                evidence_root=tmp_path if evidence else None, approver=approver)
            return engine.run(capability, args or GOOD)
        finally:
            surface.close()

    return _run


# ------------------------------------------------------------------- success
def test_success_returns_every_declared_output(replay, capability, srv):
    result = replay(capability)
    assert result.outcome is Outcome.SUCCESS
    assert result.ok
    assert set(result.outputs) == {o.name for o in capability.outputs}
    assert result.outputs["balance"] == "8421.55"


def test_replay_is_deterministic_across_runs(replay, capability, srv):
    first, second = replay(capability), replay(capability)
    assert first.outputs == second.outputs
    assert [s.strategy for s in first.steps] == [s.strategy for s in second.steps]


def test_no_step_relies_on_a_weak_locator(replay, capability, srv):
    """A run that only passed via low-confidence fallbacks is a warning, not a pass."""
    assert replay(capability).weak_steps == []


# ---------------------------------------------------------- business outcomes
def test_unknown_member_is_a_named_business_outcome(replay, capability, srv):
    result = replay(capability, {"member_id": "99999", "account_type": "Savings"})
    assert result.outcome is Outcome.BUSINESS
    assert result.business_outcome == "member_not_found"
    assert result.ok is False          # a valid answer, but not a completed capability
    assert result.error == ""          # and emphatically not an error


def test_an_injected_not_found_is_also_classified_as_business(replay, capability, srv):
    srv.arm("not_found")
    result = replay(capability)
    assert result.outcome is Outcome.BUSINESS
    assert result.business_outcome == "member_not_found"


# --------------------------------------------------------------- recoverable
def test_a_session_timeout_is_recovered_and_reported(replay, capability, srv):
    srv.arm("session_timeout")
    result = replay(capability)
    assert result.outcome is Outcome.RECOVERED
    assert result.recoveries == ["session_expired"]
    assert result.ok                     # the caller still gets what it asked for
    assert result.outputs["balance"] == "8421.55"


def test_a_slow_backend_does_not_become_a_failure(replay, capability, srv):
    srv.arm("slow_load")
    result = replay(capability)
    assert result.ok
    assert result.duration_ms >= 5000    # it genuinely waited rather than guessing


# -------------------------------------------------------------- hard failure
def test_a_server_error_is_a_hard_failure_with_debuggable_detail(replay, capability, srv):
    srv.arm("server_error")
    result = replay(capability)
    assert result.outcome is Outcome.HARD_FAILURE
    assert result.failed_step is not None
    assert result.expected and result.observed     # what we wanted vs what we saw
    assert "step" in result.error


def test_arguments_that_violate_the_contract_fail_before_anything_runs(
        replay, capability, srv):
    result = replay(capability, {"member_id": "abc", "account_type": "Savings"})
    assert result.outcome is Outcome.HARD_FAILURE
    assert result.failed_step is None              # nothing was executed
    assert "contract" in result.error


# --------------------------------------------------------------------- policy
def test_replay_cannot_act_outside_the_allowlist(replay, capability, srv, base_url):
    elsewhere = Policy(allowed_url_patterns=[base_url + "/summit/*"])
    result = replay(capability, policy=elsewhere)
    assert result.outcome is Outcome.BLOCKED_BY_POLICY
    assert result.outputs == {}


def test_an_irreversible_step_is_not_run_unattended(replay, capability, srv, base_url):
    risky = capability.model_copy(deep=True)
    risky.steps[2].risk = RiskClass.IRREVERSIBLE
    result = replay(risky)
    assert result.outcome is Outcome.BLOCKED_BY_POLICY
    assert result.failed_step == 2


def test_an_irreversible_step_runs_when_explicitly_approved(replay, capability, srv):
    risky = capability.model_copy(deep=True)
    risky.steps[2].risk = RiskClass.IRREVERSIBLE
    approvals: list[str] = []

    def approve(step, reason):
        approvals.append(reason)
        return True

    result = replay(risky, approver=approve)
    assert result.ok
    assert approvals, "the approver should have been consulted"


# ------------------------------------------------------------------ evidence
def test_every_run_leaves_evidence(replay, capability, srv):
    result = replay(capability)
    directory = Path(result.evidence_dir)
    assert (directory / "events.jsonl").exists()
    assert (directory / "summary.json").exists()
    assert (directory / "result.json").exists()
    events = [json.loads(line) for line in
              (directory / "events.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in events}
    assert {"run_started", "bound", "step", "run_finished"} <= kinds


def test_a_failing_run_captures_a_screenshot(replay, capability, srv):
    srv.arm("server_error")
    result = replay(capability)
    directory = Path(result.evidence_dir)
    assert list(directory.glob("*.png")), "a failure must leave a richer signal"


def test_sensitive_arguments_never_reach_the_evidence_directory(
        replay, capability, srv):
    sensitive = capability.model_copy(deep=True)
    sensitive.inputs[0].sensitive = True
    result = replay(sensitive)
    written = "\n".join(p.read_text() for p in Path(result.evidence_dir).glob("*")
                        if p.suffix in {".json", ".jsonl"})
    assert "12345" not in written
