"""Injectable runtime faults.

The point of the assignment is not UI drift - it is the exceptional states that
legitimately occur at runtime. Real demo sites will not produce those on demand, so
the target app exposes them as switches. Replay evidence depends on being able to
summon a specific failure reproducibly.
"""
from __future__ import annotations

import threading
import time

# Faults a replay must tell apart. The taxonomy here mirrors replay/outcomes.py:
#   business  -> a legitimate answer the caller needs ("no such member")
#   recover   -> transient or dismissable; replay should handle and continue
#   hard      -> stop and surface a debuggable error
KNOWN: dict[str, str] = {
    "not_found":         "business",
    "permission_denied": "business",
    "validation_error":  "business",
    "interstitial":      "recover",
    "slow_load":         "recover",
    "session_timeout":   "recover",
    "server_error":      "hard",
}

_lock = threading.Lock()
_armed: dict[str, int] = {}   # fault name -> remaining firings


def arm(name: str, count: int = 1) -> None:
    """Arm a fault to fire on the next `count` opportunities."""
    if name not in KNOWN:
        raise ValueError(f"unknown fault: {name}")
    with _lock:
        _armed[name] = count


def clear() -> None:
    with _lock:
        _armed.clear()


def armed() -> dict[str, int]:
    with _lock:
        return dict(_armed)


def fires(name: str) -> bool:
    """Consume one firing of `name` if armed. Not idempotent - it decrements."""
    with _lock:
        left = _armed.get(name, 0)
        if left <= 0:
            return False
        if left == 1:
            _armed.pop(name, None)
        else:
            _armed[name] = left - 1
        return True


def maybe_stall(seconds: float = 6.0) -> None:
    """Simulate a slow backend, the kind a fixed sleep would flake against."""
    if fires("slow_load"):
        time.sleep(seconds)
