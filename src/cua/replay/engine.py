"""Deterministic replay - the path an agent triggers in production.

No model is consulted here, and this module imports nothing from `discovery`. That is
enforced by the dependency direction rather than by discipline: the production path
*cannot* re-reason about the UI, because the code that knows how to do so is not
reachable from here.

The interesting design problem is not executing the steps - it is deciding what a step
that did not do what was expected actually means. Three answers, and conflating them is
the mistake the brief singles out:

  * a **known business outcome** - "no such member". The caller asked a question and
    this is the answer. Not a failure.
  * a **recoverable condition** - a session timeout, an interstitial. Handle it the way
    the recording says and carry on.
  * anything else - a **hard failure**, stopped at the first sign of trouble and
    reported with enough context to debug from.

Classification happens *before* despairing of a step, because these conditions usually
present as a step failing: when the search returns nothing, the row to click is simply
not there.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..artifact.binding import BindingError, BoundPlan, bind
from ..artifact.schema import Capability, KnownOutcome, Outcome, RecoverableCondition
from ..evidence.recorder import RunRecorder
from ..safety.policy import Decision, Policy
from ..safety.redaction import Redactor
from ..safety.surface import PolicySurface
from ..surfaces.base import Surface
from ..surfaces.models import Read
from .outcomes import ReplayResult, StepTrace

# Given the surface and the reason, may this risky step proceed? Default refuses.
Approver = Callable[[object, str], bool]

MAX_TOTAL_RECOVERIES = 6   # guards against two conditions triggering each other


class ReplayEngine:
    """Runs a stored capability against a live surface, without a model."""

    def __init__(self, surface: Surface, policy: Policy,
                 evidence_root: Path | None = None,
                 approver: Approver | None = None):
        self.policy = policy
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.redactor = Redactor()
        # Wrap unconditionally. A caller that forgets to pass a guarded surface must
        # still not be able to act outside the allowlist.
        self.surface = (surface if isinstance(surface, PolicySurface)
                        else PolicySurface(surface, policy, self.redactor, approver))
        self.approver = approver

    # ------------------------------------------------------------------- run
    def run(self, capability: Capability, args: dict) -> ReplayResult:
        started = time.monotonic()
        recorder = self._recorder(capability)

        try:
            plan = bind(capability, args)
        except BindingError as exc:
            return self._finish(recorder, ReplayResult(
                outcome=Outcome.HARD_FAILURE, capability_id=capability.id,
                version=capability.version, fingerprint=capability.fingerprint(),
                error=f"arguments do not satisfy the contract: {exc}",
                expected="arguments matching the declared inputs",
                observed="; ".join(exc.problems)), started)

        # Anything the caller declared sensitive is registered before the first action,
        # so it can never reach the evidence directory even on step 1.
        self.redactor.add(*plan.redact)
        recorder.event("bound", f"{capability.id} v{capability.version}",
                       fingerprint=plan.fingerprint,
                       arguments=sorted(args), inputs_redacted=len(plan.redact))

        result = self._execute(plan, capability, recorder)
        return self._finish(recorder, result, started)

    # -------------------------------------------------------------- internals
    def _recorder(self, capability: Capability) -> RunRecorder | None:
        if self.evidence_root is None:
            return None
        return RunRecorder(self.evidence_root, "replay", redactor=self.redactor)

    def _execute(self, plan: BoundPlan, capability: Capability,
                 recorder: RunRecorder | None) -> ReplayResult:
        base = dict(capability_id=plan.capability_id, version=plan.version,
                    fingerprint=plan.fingerprint)
        traces: list[StepTrace] = []
        outputs: dict[str, str] = {}
        recoveries: list[str] = []
        attempts: dict[str, int] = {}

        index = 0
        while index < len(plan.steps):
            step = plan.steps[index]

            gate = self.policy.check_step(step, capability.approval)
            if gate.decision is Decision.BLOCK or (
                    gate.decision is Decision.CONFIRM
                    and not (self.approver and self.approver(step, gate.reason))):
                self._log(recorder, "blocked", gate.reason, step=step.index)
                return ReplayResult(outcome=Outcome.BLOCKED_BY_POLICY, **base,
                                    failed_step=step.index, error=gate.reason,
                                    expected=f"policy to permit a {step.risk.value} step",
                                    observed=gate.decision.value,
                                    steps=traces, outputs=outputs, recoveries=recoveries)

            result = self.surface.act(step.action)
            trace = StepTrace(
                index=step.index, intent=step.intent,
                action=getattr(step.action, "kind", "?"), ok=result.ok,
                strategy=result.resolution.strategy.value
                if result.resolution and result.resolution.strategy else None,
                confidence=result.resolution.confidence if result.resolution else 0.0,
                detail=result.detail)
            traces.append(trace)
            self._log(recorder, "step", step.intent, step=step.index,
                      action=trace.action, ok=result.ok, strategy=trace.strategy,
                      confidence=trace.confidence)

            if result.blocked:
                return ReplayResult(outcome=Outcome.BLOCKED_BY_POLICY, **base,
                                    failed_step=step.index, error=result.detail,
                                    expected="an action permitted by policy",
                                    observed=result.detail, steps=traces,
                                    outputs=outputs, recoveries=recoveries)

            if isinstance(step.action, Read) and result.ok:
                outputs[step.action.output] = result.value or ""

            checkpoint = self.surface.check(step.checkpoint) if step.checkpoint else None
            if checkpoint is not None:
                trace.checkpoint_passed = checkpoint.passed
                self._log(recorder, "checkpoint", step.checkpoint.description,
                          step=step.index, passed=checkpoint.passed,
                          reason=checkpoint.reason())

            went_wrong = (not result.ok and not step.optional) or (
                checkpoint is not None and not checkpoint.passed)

            if not went_wrong:
                index += 1
                continue

            # Something is off. Decide what it *means* before calling it a failure.
            known = self._match_known(plan)
            if known is not None:
                self._log(recorder, "business_outcome", known.description,
                          name=known.name, step=step.index)
                return ReplayResult(outcome=known.outcome, **base,
                                    business_outcome=known.name,
                                    business_detail=known.description,
                                    steps=traces, outputs=outputs, recoveries=recoveries)

            condition = self._match_recoverable(plan)
            if condition is not None:
                used = attempts.get(condition.name, 0)
                if used < condition.max_attempts and len(recoveries) < MAX_TOTAL_RECOVERIES:
                    attempts[condition.name] = used + 1
                    recoveries.append(condition.name)
                    self._log(recorder, "recovering", condition.description,
                              name=condition.name, attempt=used + 1, step=step.index)
                    for action in condition.recovery:
                        self.surface.act(action)
                    index = (condition.retry_from_step
                             if condition.retry_from_step is not None else index)
                    continue
                self._log(recorder, "recovery_exhausted", condition.name,
                          attempts=used, step=step.index)

            reason = (checkpoint.reason() if checkpoint is not None and not checkpoint.passed
                      else result.detail)
            if recorder:
                recorder.failure(f"step {step.index} ({step.intent}) did not complete",
                                 surface=self.surface, label=f"step-{step.index}-failed",
                                 step=step.index, reason=reason)
            return ReplayResult(
                outcome=Outcome.HARD_FAILURE, **base, failed_step=step.index,
                error=f"step {step.index} ({step.intent}) did not complete",
                expected=(step.checkpoint.description if step.checkpoint
                          else f"the {trace.action} to succeed"),
                observed=reason, steps=traces, outputs=outputs, recoveries=recoveries)

        # Every step ran. The success checkpoint is the last word.
        final = self.surface.check(plan.success)
        self._log(recorder, "success_checkpoint", plan.success.description,
                  passed=final.passed, reason=final.reason())
        if final.passed:
            outcome = Outcome.RECOVERED if recoveries else Outcome.SUCCESS
            return ReplayResult(outcome=outcome, **base, outputs=outputs,
                                steps=traces, recoveries=recoveries)

        known = self._match_known(plan)
        if known is not None:
            return ReplayResult(outcome=known.outcome, **base,
                                business_outcome=known.name,
                                business_detail=known.description,
                                steps=traces, outputs=outputs, recoveries=recoveries)

        if recorder:
            recorder.failure("every step ran but the success condition was not met",
                             surface=self.surface, label="success-check-failed")
        return ReplayResult(
            outcome=Outcome.HARD_FAILURE, **base,
            failed_step=plan.steps[-1].index if plan.steps else None,
            error="every step ran but the success condition was not met",
            expected=plan.success.description, observed=final.reason(),
            steps=traces, outputs=outputs, recoveries=recoveries)

    # ------------------------------------------------------------ classifiers
    def _match_known(self, plan: BoundPlan) -> KnownOutcome | None:
        for known in plan.known_outcomes:
            if self.surface.check(known.signature).passed:
                return known
        return None

    def _match_recoverable(self, plan: BoundPlan) -> RecoverableCondition | None:
        for condition in plan.recoverable:
            if self.surface.check(condition.signature).passed:
                return condition
        return None

    # ----------------------------------------------------------------- output
    @staticmethod
    def _log(recorder: RunRecorder | None, kind: str, message: str, **data) -> None:
        if recorder is not None:
            recorder.event(kind, message, **data)

    def _finish(self, recorder: RunRecorder | None, result: ReplayResult,
                started: float) -> ReplayResult:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if recorder is not None:
            result.run_id = recorder.run_id
            result.evidence_dir = str(recorder.dir)
            recorder.attach("result.json", result.model_dump_json(indent=2))
            recorder.finish(result.outcome.value, summary=result.summary(),
                            weak_steps=result.weak_steps)
        return result
