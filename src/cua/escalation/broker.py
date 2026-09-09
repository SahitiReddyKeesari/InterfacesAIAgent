"""A file-backed intervention queue.

Deliberately a directory of JSON files rather than a queue service. The brief warns
against building scaling infrastructure, and the interesting part of this problem is the
control-transfer protocol, not the transport. Every operation is a whole-file write, so
the state of a handoff survives either side crashing - which matters more here than
throughput, because the failure mode to avoid is a session left with nobody holding it.

Swapping this for a real queue means reimplementing four methods.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .protocol import InterventionRequest, RequestState


class InterventionBroker:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, request_id: str) -> Path:
        return self.root / f"{request_id}.json"

    # ------------------------------------------------------------------ write
    def raise_request(self, request: InterventionRequest) -> InterventionRequest:
        self._write(request)
        return request

    def _write(self, request: InterventionRequest) -> None:
        self._path(request.id).write_text(request.model_dump_json(indent=2))

    def claim(self, request_id: str, operator: str) -> InterventionRequest:
        """An operator takes control. Refuses to hand the same session to two people."""
        request = self.load(request_id)
        if request.state is not RequestState.OPEN:
            raise ValueError(f"{request_id} is {request.state.value}, not open")
        request.state = RequestState.CLAIMED
        request.operator = operator
        request.claimed_at = datetime.now(timezone.utc)
        self._write(request)
        return request

    def release(self, request_id: str, notes: str = "",
                changes: list[str] | None = None) -> InterventionRequest:
        """The operator hands control back and the run may resume."""
        request = self.load(request_id)
        request.state = RequestState.RELEASED
        request.released_at = datetime.now(timezone.utc)
        request.operator_notes = notes
        request.human_changes = changes or request.human_changes
        self._write(request)
        return request

    def abandon(self, request_id: str, notes: str = "") -> InterventionRequest:
        request = self.load(request_id)
        request.state = RequestState.ABANDONED
        request.operator_notes = notes
        request.released_at = datetime.now(timezone.utc)
        self._write(request)
        return request

    # ------------------------------------------------------------------- read
    def load(self, request_id: str) -> InterventionRequest:
        path = self._path(request_id)
        if not path.exists():
            raise FileNotFoundError(f"no intervention {request_id!r}")
        return InterventionRequest.model_validate_json(path.read_text())

    def open_requests(self) -> list[InterventionRequest]:
        return [r for r in self.all() if r.state is RequestState.OPEN]

    def all(self) -> list[InterventionRequest]:
        out = []
        for path in sorted(self.root.glob("iv-*.json")):
            try:
                out.append(InterventionRequest.model_validate_json(path.read_text()))
            except Exception:
                continue        # a half-written file is not worth failing the queue over
        return out

    def wait_for_release(self, request_id: str, timeout_s: float,
                         poll_s: float = 1.0) -> InterventionRequest:
        """Block until an operator hands control back, or the wait expires.

        Timing out marks the request abandoned rather than leaving it open: a session
        nobody holds must not look like one somebody is working on.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            request = self.load(request_id)
            if request.state in (RequestState.RELEASED, RequestState.ABANDONED):
                return request
            time.sleep(poll_s)
        return self.abandon(request_id, notes=f"no operator within {timeout_s:.0f}s")
