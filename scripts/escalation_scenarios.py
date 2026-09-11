"""The situations that bring a human into the loop, run end to end.

Section 3.6 names three moments where a person is needed: the automation is stuck, a
replay hits something it cannot recover from, or a risky step needs somebody to decide.
Each scenario below provokes one of them against the live application and shows what the
system does - including the two where the right answer is to refuse.

    python scripts/escalation_scenarios.py [base_url]

Every scenario leaves a run directory under evidence/.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from cua.artifact.schema import RiskClass
from cua.artifact.store import Store
from cua.escalation.broker import InterventionBroker
from cua.escalation.session import ControlledSurface
from cua.replay.engine import ReplayEngine
from cua.safety.policy import Policy, for_host
from cua.safety.surface import PolicySurface
from cua.surfaces.models import Navigate
from cua.surfaces.web import PlaywrightSurface

REPO = Path(__file__).resolve().parent.parent
CAPABILITY = "meridian.read_savings_balance"
GOOD = {"member_id": "12345", "account_type": "Savings"}


def control(base: str, name: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{base}/__control/{name}", data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def run(base: str, capability, args: dict, *, adapter=None, policy=None,
        approver=None, operator_timeout: float = 8.0):
    """One replay, optionally able to hand its session to a person."""
    policy = policy or for_host(capability.surface.entry_url)
    surface = PlaywrightSurface()
    active = PolicySurface(surface, policy, approver=approver)
    if adapter is not None or operator_timeout:
        active = ControlledSurface(
            active, InterventionBroker(REPO / "evidence" / "interventions"),
            session_hint="the live browser window this run is driving",
            operator_adapter=adapter, operator_timeout_s=operator_timeout)
    try:
        return ReplayEngine(active, policy, evidence_root=REPO / "evidence",
                            approver=approver).run(capability, args)
    finally:
        active.close()


def report(title: str, expectation: str, result) -> None:
    print(f"\n{title}")
    print(f"  expected      : {expectation}")
    print(f"  outcome       : {result.outcome.value}")
    if result.escalations:
        print(f"  interventions : {result.escalations}")
    if result.human_changes:
        print(f"  operator did  : {result.human_changes[:2]}")
    if result.business_outcome:
        print(f"  answer        : {result.business_outcome}")
    if result.outputs:
        print(f"  outputs       : {result.outputs}")
    if result.error:
        print(f"  error         : {result.error[:110]}")


def main(base: str) -> None:
    store = Store(REPO / "artifacts")
    capability = store.load(CAPABILITY)

    # 1 ─ Stuck, and a person fixes it -------------------------------------
    control(base, "reset")
    control(base, "fault", {"name": "server_error"})
    seen: list[str] = []

    def operator(surface, request):
        """Stands in for a person at the paused session. A real operator reads the
        same context and works the same browser window."""
        seen.append(request.id)
        print(f"  operator sees : {request.reason[:70]}")
        print(f"  context       : expected {request.expected[:34]!r} / "
              f"observed {request.observed[:34]!r}")
        surface.act(Navigate(url=f"{base}/meridian/"))
        # Resetting the application invalidates the earlier steps, so the operator
        # says where it is safe to pick up rather than letting it retry blind.
        request.resume_from_step = 0
        return "mthompson"

    report("1. Unrecoverable error, operator intervenes and the run resumes",
           "escalates, a person repairs the session, replay finishes",
           run(base, capability, GOOD, adapter=operator))

    # 2 ─ Stuck, and nobody comes ------------------------------------------
    control(base, "reset")
    control(base, "fault", {"name": "server_error", "count": 6})
    report("2. Unrecoverable error and no operator available",
           "escalates, times out, reports ESCALATED rather than pretending",
           run(base, capability, GOOD, operator_timeout=2.0))

    # 3 ─ A risky step nobody approved -------------------------------------
    control(base, "reset")
    risky = capability.model_copy(deep=True)
    risky.steps[2].risk = RiskClass.IRREVERSIBLE
    report("3. An irreversible step with no approver",
           "blocked by policy before the step runs",
           run(base, risky, GOOD, operator_timeout=0))

    # 4 ─ The same step, explicitly approved -------------------------------
    control(base, "reset")
    report("4. The same irreversible step, a person approves it",
           "runs, and the approval is recorded",
           run(base, risky, GOOD, approver=lambda step, why: True,
               operator_timeout=0))

    # 5 ─ A request that names more than one record ------------------------
    control(base, "reset")
    report("5. A request that does not identify one record",
           "refuses to guess; a person can narrow it",
           run(base, capability, {"member_id": "12346", "account_type": "Savings"},
               operator_timeout=0))

    # 6 ─ Pointed outside the surface it is permitted to touch --------------
    # Not an escalation: no operator can approve their way past an allowlist, and the
    # fix is a policy change by someone accountable for it, not a click.
    control(base, "reset")
    report("6. The automation is pointed outside its allowlist",
           "refused before acting; no in-run approval can override it",
           run(base, capability, GOOD,
               policy=Policy(allowed_url_patterns=[f"{base}/summit/*"]),
               operator_timeout=0))

    control(base, "reset")
    print("\nevidence written under evidence/")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080")
