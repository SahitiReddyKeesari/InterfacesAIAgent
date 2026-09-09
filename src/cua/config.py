"""Runtime configuration, read from the environment (and a .env file if present).

Kept tiny and explicit. Nothing here has a default that would quietly widen what the
system is allowed to do - the allowlist in particular is always derived from the entry
point the caller named, never from a wildcard.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO_ROOT / "artifacts"
EVIDENCE = REPO_ROOT / "evidence"
INTERVENTIONS = REPO_ROOT / "evidence" / "interventions"


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing variables win, so an explicit
    export always overrides the file."""
    env = path or REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def provider(name: str | None = None):
    """Build the configured LLM provider. Imported lazily so the deterministic path
    never pulls in model code."""
    choice = (name or os.getenv("CUA_LLM_PROVIDER") or "gemini").lower()
    if choice == "gemini":
        from .discovery.llm.gemini import GeminiProvider
        return GeminiProvider()
    raise ValueError(f"unknown provider {choice!r}; supported: gemini")
