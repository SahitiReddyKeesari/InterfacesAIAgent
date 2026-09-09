"""The Surface protocol - the only contract the layers above depend on.

A Surface is anything that can be perceived and acted on: a browser page, a legacy
frameset app, a terminal emulator, a native desktop window. Implementations differ
entirely in how they gather an Observation and carry out an Action; nothing above this
line changes when one is added.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (Action, ActionResult, CheckResult, Checkpoint, Locator,
                     Observation, Resolution)


@runtime_checkable
class Surface(Protocol):
    """Perceive and act. Deliberately narrow."""

    name: str

    def observe(self) -> Observation:
        """Snapshot everything currently perceivable."""

    def resolve(self, locator: Locator) -> Resolution:
        """Try a locator's fallback chain without acting. Used for checkpoints and
        for reporting which strategy actually carried a step."""

    def act(self, action: Action) -> ActionResult:
        """Carry out one action, returning what happened rather than raising."""

    def current_url(self) -> str:
        """Where the surface is now. Cheap - no full observation."""

    def check(self, checkpoint: Checkpoint) -> CheckResult:
        """Evaluate a checkpoint by observing only. Never acts, so it is safe to call
        repeatedly and cannot disturb the state being verified."""

    def capture(self, label: str) -> Path | None:
        """Persist a richer signal for evidence (screenshot, snapshot, trace)."""

    def close(self) -> None:
        ...
