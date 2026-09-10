"""Tests for the operator console's HTTP API.

The console is optional, but it is a second door onto a system that drives banking
screens, so the things worth pinning are the ones that would be embarrassing: that it
serves no file outside the directories it is meant to, that it refuses to start a second
replay against the same target while one is running, and that it reports a business
outcome as an outcome rather than as an error.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from cua.console.server import serve
from tests.fixtures.meridian_capability import build


class Console(str):
    """The console's base URL, carrying the directories it was given so a test can
    reach the same queue the console reads."""

    interventions: Path


@pytest.fixture(scope="module")
def console(base_url, tmp_path_factory):
    """A console over a private artifacts/evidence directory."""
    root = tmp_path_factory.mktemp("console")
    artifacts, evidence = root / "artifacts", root / "evidence"
    artifacts.mkdir()
    evidence.mkdir()
    interventions = evidence / "interventions"

    from cua.artifact.store import Store
    Store(artifacts).save(build(base_url))

    server = serve("127.0.0.1", 0, artifacts, evidence, interventions)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    handle = Console(f"http://127.0.0.1:{server.server_address[1]}")
    handle.interventions = interventions
    yield handle
    server.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def get(console, path):
    with urllib.request.urlopen(console + path, timeout=120) as r:
        return r.status, json.loads(r.read())


def post(console, path, body):
    request = urllib.request.Request(
        console + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as r:
        return r.status, json.loads(r.read())


def status_of(console, path):
    try:
        with urllib.request.urlopen(console + path, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ---------------------------------------------------------------- read the shelf
def test_capabilities_expose_the_contract_the_ui_needs(console):
    _, caps = get(console, "/api/capabilities")
    assert len(caps) == 1
    cap = caps[0]
    assert cap["id"] == "meridian.read_account_balance"
    assert [p["name"] for p in cap["inputs"]] == ["member_id", "account_type"]
    assert [k["name"] for k in cap["known_outcomes"]] == ["member_not_found"]
    assert cap["problems"] == []


def test_the_catalog_is_the_same_one_an_agent_would_call(console):
    _, catalog = get(console, "/api/catalog")
    assert catalog[0]["input_schema"]["required"] == ["member_id", "account_type"]


def test_the_ui_and_its_vendored_react_are_served(console):
    assert status_of(console, "/") == 200
    assert status_of(console, "/vendor/react.js") == 200


# ------------------------------------------------------------------- boundaries
@pytest.mark.parametrize("path", [
    "/vendor/../server.py",
    "/vendor/../../cli.py",
    "/evidence/../../pyproject.toml",
    "/api/runs/../../etc/hosts",
])
def test_no_file_outside_the_served_directories_is_reachable(console, path):
    assert status_of(console, path) in (403, 404)


def test_an_unknown_route_is_a_clean_404(console):
    assert status_of(console, "/api/nonsense") == 404


# ------------------------------------------------------------------- run a plan
def test_replaying_through_the_api_returns_outputs(console, srv):
    _, result = post(console, "/api/replay", {
        "capability_id": "meridian.read_account_balance",
        "args": {"member_id": "12345", "account_type": "Savings"}})
    assert result["outcome"] == "success"
    assert result["outputs"]["balance"] == "8421.55"
    assert [s["strategy"] for s in result["steps"]][1] == "label_text"


def test_a_business_outcome_is_reported_as_an_outcome_not_an_error(console, srv):
    _, result = post(console, "/api/replay", {
        "capability_id": "meridian.read_account_balance",
        "args": {"member_id": "99999", "account_type": "Savings"}})
    assert result["outcome"] == "business_outcome"
    assert result["business_outcome"] == "member_not_found"
    assert "error" not in result or not result["error"]


def test_a_finished_run_appears_in_the_runs_list(console, srv):
    _, runs = get(console, "/api/runs")
    assert runs, "the replays above should have left evidence"
    _, detail = get(console, f"/api/runs/{runs[0]['id']}")
    assert {e["kind"] for e in detail["events"]} >= {"run_started", "step", "run_finished"}


def test_two_replays_cannot_run_against_the_same_target_at_once(console, srv):
    """A second concurrent run would produce results neither run could explain."""
    from cua.console.server import ConsoleState
    from pathlib import Path

    state = ConsoleState(Path("artifacts"), Path("evidence"), Path("evidence/interventions"))
    state.busy = True
    from cua.console.server import _run_replay
    assert "already running" in _run_replay(state, {"capability_id": "x"})["error"]


# ---------------------------------------------------------------- interventions
def test_an_intervention_can_be_claimed_and_released(console):
    """The console is a real operator surface: it must be able to take control of a
    paused session and hand it back, not merely display that one is waiting."""
    from cua.escalation.broker import InterventionBroker
    from cua.escalation.protocol import InterventionRequest

    request = InterventionBroker(console.interventions).raise_request(
        InterventionRequest(capability_id="demo", goal="read a balance",
                            reason="stuck", step_index=1, step_intent="click Select",
                            expected="Member Detail", observed="an error page"))

    _, listed = get(console, "/api/interventions")
    assert any(r["id"] == request.id for r in listed)

    _, claimed = post(console, "/api/interventions/claim",
                      {"id": request.id, "operator": "mthompson"})
    assert claimed["state"] == "claimed"
    assert claimed["operator"] == "mthompson"

    _, released = post(console, "/api/interventions/release",
                       {"id": request.id, "notes": "cleared the error page"})
    assert released["state"] == "released"
    assert released["operator_notes"] == "cleared the error page"


# ------------------------------------------------------- discovery from the UI
def test_discovery_can_be_started_and_polled_without_blocking(console, srv,
                                                              monkeypatch, base_url):
    """The Assistant tab starts a discovery run and narrates it while it works.

    Stubbed model on purpose: this test is about the wiring - that the request returns
    at once, that the run's own evidence is the progress feed, and that a finished run
    leaves a saved capability - not about whether a model can drive a screen, which the
    real run in /evidence proves.
    """
    import time

    from tests.test_discovery import HAPPY, ScriptedProvider

    monkeypatch.setattr("cua.config.provider", lambda name=None: ScriptedProvider(HAPPY))

    status, started = post(console, "/api/discover", {
        "capability_id": "console.smoke",
        "url": base_url + "/meridian/",
        "goal": "Look up a member and read their name",
        "params": {"member_id": "12345"},
        "max_steps": 8,
    })
    assert status == 200
    assert started["run_id"] and started["done"] is False   # returned immediately

    deadline = time.time() + 120
    while time.time() < deadline:
        _, state = get(console, f"/api/discover/{started['run_id']}")
        if state["done"]:
            break
        time.sleep(1)

    assert state["done"], "discovery never finished"
    assert state["ok"], state.get("error")
    assert any(e["kind"] == "acted" for e in state["events"]), "no progress to narrate"

    _, caps = get(console, "/api/capabilities")
    assert any(c["id"] == "console.smoke" for c in caps), "the capability was not saved"


def test_a_second_run_is_refused_while_one_is_in_flight(console):
    """One browser against one target. A second concurrent run would produce results
    neither run could explain."""
    from pathlib import Path

    from cua.console.server import ConsoleState, _start_discovery

    state = ConsoleState(Path("artifacts"), Path("evidence"), Path("evidence/interventions"))
    state.busy = True
    assert "already in progress" in _start_discovery(state, {"capability_id": "x"})["error"]
