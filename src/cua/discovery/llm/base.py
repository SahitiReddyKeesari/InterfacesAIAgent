"""The model boundary.

One narrow interface, for two reasons. The obvious one is not being welded to a vendor.
The more useful one is that it draws a visible line around the only place in the system
where a model influences behaviour: everything on the production path is deterministic,
and that claim is checkable by seeing who imports this module.
"""
from __future__ import annotations

from typing import Any, Protocol


class LLMError(RuntimeError):
    """The provider could not produce a usable decision."""


class LLMProvider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict:
        """Return a JSON object conforming to `schema`.

        Implementations must retry transient failures themselves - a free tier that
        rejects one call in three would otherwise make every discovery run a coin flip.
        """


class RecordedProvider:
    """Replays canned decisions. Used to test the loop's control flow without a
    network call, so the tests that exercise stopping conditions, budgets and
    malformed responses stay fast and deterministic."""

    name = "recorded"

    def __init__(self, decisions: list[dict], model: str = "recorded"):
        self.model = model
        self._decisions = list(decisions)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append((system, user))
        if not self._decisions:
            raise LLMError("no further recorded decisions")
        return self._decisions.pop(0)
