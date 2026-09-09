"""Reading and writing capability artifacts.

Versioning is content-driven rather than manual. Saving a capability whose executable
content differs from the stored latest writes a new version instead of overwriting one:
a capability that some agent is calling in production must not change underneath it
because someone re-recorded the flow. Re-saving identical content is a no-op, so
re-running discovery does not churn version numbers.
"""
from __future__ import annotations

import re
from pathlib import Path

from .schema import Capability

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, capability_id: str) -> Path:
        if not _SAFE_ID.match(capability_id):
            raise ValueError(f"unsafe capability id: {capability_id!r}")
        return self.root / capability_id

    def versions(self, capability_id: str) -> list[int]:
        d = self._dir(capability_id)
        if not d.exists():
            return []
        out = []
        for p in d.glob("v*.json"):
            try:
                out.append(int(p.stem[1:]))
            except ValueError:
                continue
        return sorted(out)

    def load(self, capability_id: str, version: int | None = None) -> Capability:
        vs = self.versions(capability_id)
        if not vs:
            raise FileNotFoundError(f"no artifact for {capability_id!r}")
        v = version if version is not None else vs[-1]
        if v not in vs:
            raise FileNotFoundError(f"{capability_id} has no version {v}")
        path = self._dir(capability_id) / f"v{v}.json"
        return Capability.model_validate_json(path.read_text())

    def save(self, capability: Capability) -> Path:
        """Persist, bumping the version only when executable content actually changed."""
        d = self._dir(capability.id)
        d.mkdir(parents=True, exist_ok=True)
        existing = self.versions(capability.id)

        if existing:
            latest = self.load(capability.id, existing[-1])
            if latest.fingerprint() == capability.fingerprint():
                # Same plan: keep the version, refresh provenance/approval in place.
                capability.version = latest.version
                path = d / f"v{latest.version}.json"
                path.write_text(capability.model_dump_json(indent=2))
                return path
            capability.version = existing[-1] + 1

        path = d / f"v{capability.version}.json"
        path.write_text(capability.model_dump_json(indent=2))
        return path

    def list_capabilities(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def catalog(self) -> list[dict]:
        """Tool schemas for every approved capability - what a calling agent sees."""
        out = []
        for cid in self.list_capabilities():
            try:
                cap = self.load(cid)
            except (FileNotFoundError, ValueError):
                continue
            out.append(cap.tool_schema())
        return out
