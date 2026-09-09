"""Record the example capability against a running mock back-office.

    python scripts/record_example.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from cua.artifact.examples import build
from cua.artifact.store import Store

REPO = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    cap = build(base)
    problems = cap.validate_contract()
    print("contract problems:", problems or "none")
    path = Store(REPO / "artifacts").save(cap)
    print("saved:", path.relative_to(REPO), "| fingerprint", cap.fingerprint())
    print("weakest locator confidence:", cap.provenance.weakest_locator)
