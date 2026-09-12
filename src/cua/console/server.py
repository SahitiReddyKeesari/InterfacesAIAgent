"""A small HTTP API over the same operations the CLI exposes.

Every endpoint calls the same code the command line does - there is no second
implementation of replay, of policy, or of the intervention protocol. That matters more
than it sounds: a console that reimplemented any of them would eventually disagree with
the system it is supposed to be observing.
"""
from __future__ import annotations

import json
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..artifact.store import Store
from ..config import ARTIFACTS, EVIDENCE, INTERVENTIONS
from ..escalation.broker import InterventionBroker
from ..replay.engine import ReplayEngine
from ..safety.policy import for_host
from ..surfaces.web import PlaywrightSurface

HERE = Path(__file__).parent
STATIC = {"/": HERE / "ui.html", "/ui.html": HERE / "ui.html"}
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
        ".css": "text/css", ".png": "image/png", ".json": "application/json"}

_STOPWORDS = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "is", "are",
              "what", "whats", "show", "me", "please", "their", "this", "that", "look",
              "up", "find", "get", "tell", "report"}


class ConsoleState:
    """Shared state for the console: where things live, and what is running."""

    def __init__(self, artifacts: Path, evidence: Path, interventions: Path):
        self.store = Store(artifacts)
        self.evidence = Path(evidence)
        self.broker = InterventionBroker(interventions)
        self.lock = threading.Lock()
        # One run at a time, of either kind. They drive a real browser against a real
        # application; letting a click in the UI start a second concurrent run against
        # the same target would produce results neither run could explain.
        self.busy = False
        # Discovery takes minutes and the caller polls, so its outcome outlives the
        # request that started it.
        self.discoveries: dict[str, dict[str, Any]] = {}


def _capability_summary(store: Store, capability_id: str) -> dict[str, Any]:
    capability = store.load(capability_id)
    return {
        "id": capability.id,
        "version": capability.version,
        "versions": store.versions(capability_id),
        "name": capability.name,
        "description": capability.description,
        "approval": capability.approval.value,
        "fingerprint": capability.fingerprint(),
        "entry_url": capability.surface.entry_url,
        "steps": len(capability.steps),
        "inputs": [p.model_dump(mode="json") for p in capability.inputs],
        "outputs": [o.model_dump(mode="json") for o in capability.outputs],
        "known_outcomes": [{"name": k.name, "description": k.description}
                           for k in capability.known_outcomes],
        "recoverable": [{"name": r.name, "description": r.description}
                        for r in capability.recoverable],
        "weakest_locator": capability.provenance.weakest_locator,
        "model": capability.provenance.model,
        "problems": capability.validate_contract(),
    }


def _run_replay(state: ConsoleState, body: dict) -> dict[str, Any]:
    with state.lock:
        if state.busy:
            return {"error": "a replay is already running"}
        state.busy = True
    try:
        capability = state.store.load(body["capability_id"], body.get("version"))
        policy = for_host(capability.surface.entry_url)
        surface = PlaywrightSurface(headless=not body.get("headed"))
        try:
            engine = ReplayEngine(surface, policy, evidence_root=state.evidence,
                                  approver=(lambda step, reason: True)
                                  if body.get("approve_risky") else None)
            result = engine.run(capability, body.get("args") or {})
        finally:
            surface.close()
        payload = result.model_dump(mode="json")
        payload["summary"] = result.summary()
        payload["weak_steps"] = result.weak_steps
        return payload
    finally:
        with state.lock:
            state.busy = False


def _suggested_id(goal: str, taken: set[str]) -> str:
    """A stable, readable id for a capability the system is about to learn."""
    words = [w for w in re.split(r"[^a-z0-9]+", goal.lower()) if w
             and w not in _STOPWORDS][:4]
    base = "meridian." + ("_".join(words) or "capability")
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def _ask(state: ConsoleState, body: dict) -> dict[str, Any]:
    """Route a free-text question to a capability, or report that none fits.

    The model runs here, in the assistant, deciding *what* to do. Whatever it decides,
    carrying it out is still a deterministic replay.
    """
    from ..config import provider
    from ..discovery.router import route

    question = (body.get("question") or "").strip()
    if not question:
        return {"error": "ask something"}

    catalog = state.store.catalog()
    try:
        decision = route(provider(), question, catalog)
    except Exception as exc:
        return {"error": f"the assistant could not reach a model: {exc}"}

    payload = decision.to_dict()
    payload["question"] = question
    if decision.capability_id is None:
        payload["suggested_id"] = _suggested_id(
            decision.goal or question, set(state.store.list_capabilities()))
    return payload


def _start_discovery(state: ConsoleState, body: dict) -> dict[str, Any]:
    """Kick off a discovery run in the background and return its id immediately.

    Discovery drives an LLM through a live application and takes minutes, so the
    request cannot wait for it. The caller polls; the run's own evidence file is the
    progress feed, which means there is no second source of truth about what happened.
    """
    with state.lock:
        if state.busy:
            return {"error": "a run is already in progress"}
        state.busy = True

    from ..discovery.agent import DiscoveryAgent, DiscoveryConfig
    from ..evidence.recorder import RunRecorder
    from ..safety.redaction import Redactor
    from ..safety.surface import PolicySurface

    redactor = Redactor()
    recorder = RunRecorder(state.evidence, "discovery", redactor=redactor)
    record = {"run_id": recorder.run_id, "done": False, "ok": False,
              "capability_id": body["capability_id"], "error": None}
    state.discoveries[recorder.run_id] = record

    def work():
        from ..config import provider
        surface = None
        try:
            llm = provider()
            surface = PlaywrightSurface(headless=True)
            guarded = PolicySurface(surface, for_host(body["url"]), redactor)
            capability = DiscoveryAgent(guarded, llm, recorder).run(DiscoveryConfig(
                goal=body["goal"], entry_url=body["url"],
                capability_id=body["capability_id"],
                parameters=body.get("params") or {},
                parameter_docs=body.get("describe") or {},
                max_steps=int(body.get("max_steps", 10))))
            state.store.save(capability)
            recorder.finish("success", capability=capability.id)
            record["ok"] = True
        except Exception as exc:
            # Any failure is reported to the caller rather than raised into a thread
            # nobody is watching - including DiscoveryFailed, which is the expected
            # kind when a model gives up or the step budget runs out.
            record["error"] = str(exc)
            try:
                recorder.failure(str(exc), surface=surface, label="discovery-failed")
                recorder.finish("failed")
            except Exception:
                pass
        finally:
            if surface is not None:
                surface.close()
            record["done"] = True
            with state.lock:
                state.busy = False

    threading.Thread(target=work, daemon=True).start()
    return record


def _discovery_status(state: ConsoleState, run_id: str) -> dict[str, Any]:
    record = dict(state.discoveries.get(run_id) or {"error": "no such discovery"})
    record["events"] = _run_detail(state, run_id).get("events", [])
    return record


def _busy(state: ConsoleState) -> dict[str, Any]:
    """Whether a run holds the browser, and what it has managed so far.

    "Something is running" is not useful on its own: a discovery run can take twenty
    minutes, and a caller who cannot see progress cannot tell it apart from a hang.
    """
    live = {"busy": state.busy, "run_id": None, "kind": None, "steps": 0, "last": ""}
    if not state.busy:
        return live
    recent = [r for r in state.discoveries.values() if not r["done"]]
    if recent:
        record = recent[-1]
        events = _run_detail(state, record["run_id"]).get("events", [])
        live.update(run_id=record["run_id"], kind="discovery",
                    steps=sum(1 for e in events if e["kind"] == "acted"),
                    last=(events[-1]["message"] if events else "")[:90])
    else:
        live.update(kind="replay")
    return live


def _runs(state: ConsoleState) -> list[dict[str, Any]]:
    out = []
    for directory in sorted(state.evidence.glob("*-*"), reverse=True):
        summary = directory / "summary.json"
        if not summary.exists():
            continue
        try:
            data = json.loads(summary.read_text())
        except json.JSONDecodeError:
            continue
        data["id"] = directory.name
        data["screenshots"] = sorted(p.name for p in directory.glob("*.png"))
        out.append(data)
    return out[:60]


def _run_detail(state: ConsoleState, run_id: str) -> dict[str, Any]:
    directory = state.evidence / run_id
    if not directory.is_dir() or ".." in run_id or "/" in run_id:
        return {"error": "no such run"}
    events = []
    log = directory / "events.jsonl"
    if log.exists():
        events = [json.loads(line) for line in log.read_text().splitlines() if line]
    return {"id": run_id, "events": events,
            "screenshots": sorted(p.name for p in directory.glob("*.png"))}


def build_routes(state: ConsoleState) -> dict[str, Callable[[dict], Any]]:
    """path -> handler. Handlers take the parsed body (empty for GET)."""
    return {
        "GET /api/capabilities":
            lambda _: [_capability_summary(state.store, cid)
                       for cid in state.store.list_capabilities()],
        "GET /api/catalog":
            lambda _: state.store.catalog(),
        "POST /api/replay":
            lambda body: _run_replay(state, body),
        "POST /api/ask":
            lambda body: _ask(state, body),
        "POST /api/discover":
            lambda body: _start_discovery(state, body),
        "GET /api/busy":
            lambda _: _busy(state),
        "GET /api/runs":
            lambda _: _runs(state),
        "GET /api/interventions":
            lambda _: [r.model_dump(mode="json") for r in state.broker.all()],
        "POST /api/interventions/claim":
            lambda body: state.broker.claim(body["id"], body["operator"]).model_dump(
                mode="json"),
        "POST /api/interventions/release":
            lambda body: state.broker.release(
                body["id"], body.get("notes", "")).model_dump(mode="json"),
    }


class Handler(BaseHTTPRequestHandler):
    state: ConsoleState
    routes: dict[str, Callable[[dict], Any]]

    def log_message(self, *args):        # quiet by default; the runs view is the log
        pass

    # ------------------------------------------------------------------ verbs
    def do_GET(self):
        path = urlparse(self.path).path
        if path in STATIC:
            return self._send_file(STATIC[path])
        if path.startswith("/vendor/"):
            candidate = (HERE / "vendor" / Path(path).name).resolve()
            if candidate.parent == (HERE / "vendor").resolve() and candidate.exists():
                return self._send_file(candidate)
            return self._send_json({"error": "not found"}, 404)
        if path.startswith("/evidence/"):
            return self._send_evidence_file(path)
        if path.startswith("/api/runs/") and len(path.split("/")) == 4:
            return self._send_json(_run_detail(self.state, path.split("/")[3]))
        if path.startswith("/api/discover/") and len(path.split("/")) == 4:
            return self._send_json(_discovery_status(self.state, path.split("/")[3]))
        return self._dispatch("GET", path, {})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "body must be JSON"}, 400)
        return self._dispatch("POST", path, body)

    # --------------------------------------------------------------- plumbing
    def _dispatch(self, verb: str, path: str, body: dict):
        handler = self.routes.get(f"{verb} {path}")
        if handler is None:
            return self._send_json({"error": f"no route for {verb} {path}"}, 404)
        try:
            return self._send_json(handler(body))
        except FileNotFoundError as exc:
            return self._send_json({"error": str(exc)}, 404)
        except Exception as exc:
            # Surfaced rather than swallowed: a console that hides its own errors is
            # worse than no console.
            return self._send_json({"error": str(exc),
                                    "trace": traceback.format_exc()[-800:]}, 500)

    def _send_evidence_file(self, path: str):
        relative = Path(path[len("/evidence/"):])
        candidate = (self.state.evidence / relative).resolve()
        if self.state.evidence.resolve() not in candidate.parents or not candidate.exists():
            return self._send_json({"error": "not found"}, 404)
        return self._send_file(candidate)

    def _send_file(self, path: Path):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Any, status: int = 200):
        data = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(host: str = "127.0.0.1", port: int = 8765,
          artifacts: Path | None = None, evidence: Path | None = None,
          interventions: Path | None = None) -> ThreadingHTTPServer:
    state = ConsoleState(artifacts or ARTIFACTS, evidence or EVIDENCE,
                         interventions or INTERVENTIONS)
    handler = type("BoundHandler", (Handler,),
                   {"state": state, "routes": build_routes(state)})
    return ThreadingHTTPServer((host, port), handler)
