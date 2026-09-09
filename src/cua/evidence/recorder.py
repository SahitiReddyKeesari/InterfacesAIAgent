"""Structured run evidence.

Every run - discovery or replay - writes a directory containing what happened and why.
Three decisions shape it:

* **Append as you go, not at the end.** Events are flushed per line, so a run that
  crashes or is killed still leaves everything up to the moment it died. Evidence that
  only exists for successful runs is evidence for the case you least need it in.
* **Redact on write.** Every string passes the redactor on its way to disk, rather than
  being scrubbed later and hopefully everywhere.
* **Richer signal on failure only.** Screenshots are captured when something goes wrong,
  not per step: a full-page capture of every step is slow and buries the one frame that
  matters.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..safety.redaction import Redactor


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"


class RunRecorder:
    """Writes one run's evidence directory."""

    def __init__(self, root: Path, kind: str, run_id: str | None = None,
                 redactor: Redactor | None = None):
        self.run_id = run_id or new_run_id(kind)
        self.kind = kind
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._events = self.dir / "events.jsonl"
        self._seq = 0
        self._started = time.monotonic()
        self.event("run_started", f"{kind} run {self.run_id}")

    # ------------------------------------------------------------------ write
    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor.scrub(value)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def event(self, kind: str, message: str = "", **data: Any) -> dict:
        """Record one thing that happened. Returns the written record."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
            "kind": kind,
            "message": self.redactor.scrub(message),
            **self._scrub(data),
        }
        with self._events.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    def step(self, index: int, action_kind: str, intent: str, **data: Any) -> dict:
        return self.event("step", intent, step=index, action=action_kind, **data)

    def failure(self, message: str, surface=None, label: str = "failure",
                **data: Any) -> dict:
        """Record a failure and, where possible, a richer signal alongside it."""
        shot = None
        if surface is not None:
            try:
                path = surface.capture(label, self.dir)
                shot = path.name if path else None
            except Exception as exc:                       # capture must never mask
                shot = f"capture-failed: {exc}"            # the failure it documents
        return self.event("failure", message, screenshot=shot, **data)

    def attach(self, name: str, content: str) -> Path:
        """Write a companion file (an artifact, a DOM snapshot, a transcript)."""
        path = self.dir / name
        path.write_text(self.redactor.scrub(content))
        return path

    # ---------------------------------------------------------------- finish
    def finish(self, outcome: str, **summary: Any) -> Path:
        self.event("run_finished", outcome, outcome=outcome)
        payload = {
            "run_id": self.run_id,
            "kind": self.kind,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - self._started) * 1000),
            "events": self._seq,
            **self._scrub(summary),
        }
        path = self.dir / "summary.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path

    def read_events(self) -> list[dict]:
        if not self._events.exists():
            return []
        return [json.loads(line) for line in self._events.read_text().splitlines() if line]
