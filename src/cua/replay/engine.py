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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..artifact.binding import BindingError, BoundPlan, bind
from ..artifact.schema import Capability, KnownOutcome, Outcome, RecoverableCondition
from ..evidence.recorder import RunRecorder
from ..safety.policy import Decision, Policy
from ..safety.redaction import Redactor
from ..safety.surface import PolicySurface
from ..surfaces.base import Surface
from ..surfaces.models import ActionResult, CheckResult, Read
from .outcomes import ReplayResult, StepTrace

# Given the surface and the reason, may this risky step proceed? Default refuses.
Approver = Callable[[object, str], bool]

MAX_TOTAL_RECOVERIES = 6   # guards against two conditions triggering each other


@dataclass
class _Run:
    """Mutable state for one execution. A small bag beats threading six lists through
    every helper, and keeps the step loop readable."""

    plan: BoundPlan
    capability: Capability
    recorder: RunRecorder | None
    traces: list[StepTrace] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    recoveries: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    human_changes: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    escalated: set[int] = field(default_factory=set)

    def result(self, outcome: Outcome, **over) -> ReplayResult:
        """Build a result carrying everything accumulated so far."""
        return ReplayResult(
            outcome=outcome, capability_id=self.plan.capability_id,
            version=self.plan.version, fingerprint=self.plan.fingerprint,
            steps=self.traces, outputs=self.outputs, recoveries=self.recoveries,
            escalations=self.escalations, human_changes=self.human_changes, **over)


class ReplayEngine:
    """Runs a stored capability against a live surface, without a model."""

    def __init__(self, surface: Surface, policy: Policy,
                 evidence_root: Path | None = None,
                 approver: Approver | None = None):
        self.policy = policy
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.redactor = Redactor()
        # Wrap only if nothing in the chain is already enforcing policy - that lets a
        # ControlledSurface sit outside a PolicySurface without losing the allowlist.
        self.surface = (surface if getattr(surface, "guarded", False)
                        else PolicySurface(surface, policy, self.redactor, approver))
        self.approver = approver

    # ------------------------------------------------------------------- run
    def run(self, capability: Capability, args: dict) -> ReplayResult:
        started = time.monotonic()
        recorder = (RunRecorder(self.evidence_root, "replay", redactor=self.redactor)
                    if self.evidence_root else None)

        try:
            plan = bind(capability, args)
        except BindingError as exc:
            return self._finish(recorder, ReplayResult(
                outcome=Outcome.HARD_FAILURE, capability_id=capability.id,
                version=capability.version, fingerprint=capability.fingerprint(),
                error=f"arguments do not satisfy the contract: {exc}",
                expected="arguments matching the declared inputs",
                observed="; ".join(exc.problems)), started)

        # Anything declared sensitive is registered before the first action, so it can
        # never reach the evidence directory even on step 1.
        self.redactor.add(*plan.redact)
        self._log(recorder, "bound", f"{capability.id} v{capability.version}",
                  fingerprint=plan.fingerprint, arguments=sorted(args),
                  inputs_redacted=len(plan.redact))

        run = _Run(plan=plan, capability=capability, recorder=recorder)
        return self._finish(recorder, self._execute(run), started)

    # --------------------------------------------------------------- the loop
    def _execute(self, run: _Run) -> ReplayResult:
        index = 0
        while index < len(run.plan.steps):
            step = run.plan.steps[index]

            refused = self._gate(run, step)
            if refused is not None:
                return refused

            result, checkpoint = self._perform(run, step)
            if result.blocked:
                return run.result(Outcome.BLOCKED_BY_POLICY, failed_step=step.index,
                                  error=result.detail,
                                  expected="an action permitted by policy",
                                  observed=result.detail)

            if not self._went_wrong(step, result, checkpoint):
                index += 1
                continue

            # Something is off. Decide what it *means* before calling it a failure.
            resume, outcome = self._diagnose(run, step, index, result, checkpoint)
            if outcome is not None:
                return outcome
            index = resume

        return self._conclude(run)

    # -------------------------------------------------------------- one step
    def _gate(self, run: _Run, step) -> ReplayResult | None:
        """Refuse a step the policy will not permit unattended."""
        verdict = self.policy.check_step(step, run.capability.approval)
        permitted = (verdict.decision is Decision.ALLOW
                     or (verdict.decision is Decision.CONFIRM
                         and self.approver is not None
                         and self.approver(step, verdict.reason)))
        if permitted:
            return None
        self._log(run.recorder, "blocked", verdict.reason, step=step.index)
        return run.result(Outcome.BLOCKED_BY_POLICY, failed_step=step.index,
                          error=verdict.reason,
                          expected=f"policy to permit a {step.risk.value} step",
                          observed=verdict.decision.value)

    def _perform(self, run: _Run, step) -> tuple[ActionResult, CheckResult | None]:
        """Carry out one step and verify its checkpoint, recording both."""
        result = self.surface.act(step.action)
        resolution = result.resolution
        trace = StepTrace(
            index=step.index, intent=step.intent,
            action=getattr(step.action, "kind", "?"), ok=result.ok,
            strategy=resolution.strategy.value
            if resolution and resolution.strategy else None,
            confidence=resolution.confidence if resolution else 0.0,
            detail=result.detail)
        run.traces.append(trace)
        self._log(run.recorder, "step", step.intent, step=step.index,
                  action=trace.action, ok=result.ok, strategy=trace.strategy,
                  confidence=trace.confidence)

        if isinstance(step.action, Read) and result.ok:
            run.outputs[step.action.output] = result.value or ""

        checkpoint = self.surface.check(step.checkpoint) if step.checkpoint else None
        if checkpoint is not None:
            trace.checkpoint_passed = checkpoint.passed
            self._log(run.recorder, "checkpoint", step.checkpoint.description,
                      step=step.index, passed=checkpoint.passed,
                      reason=checkpoint.reason())
        return result, checkpoint

    @staticmethod
    def _went_wrong(step, result: ActionResult, checkpoint: CheckResult | None) -> bool:
        if checkpoint is not None and not checkpoint.passed:
            return True
        return not result.ok and not step.optional

    # ------------------------------------------------------------- diagnosis
    def _diagnose(self, run: _Run, step, index: int, result: ActionResult,
                  checkpoint: CheckResult | None) -> tuple[int, ReplayResult | None]:
        """Work out what a step going wrong actually means.

        Returns the index to resume from, or a finished result. The order is the whole
        error taxonomy: a declared answer first, then a condition we know how to handle,
        then a person, and only then failure.
        """
        known = self._match_known(run.plan)
        if known is not None:
            self._log(run.recorder, "business_outcome", known.description,
                      name=known.name, step=step.index)
            return index, run.result(known.outcome, business_outcome=known.name,
                                     business_detail=known.description)

        # An ambiguous request is something the caller can fix by narrowing the key,
        # so it is an answer rather than a fault. It is recognised here rather than
        # declared per capability because the application never reports it - the
        # system detects it, by finding more than one record behind one key.
        if result.resolution is not None and result.resolution.ambiguous:
            detail = (f"{result.resolution.ambiguous} records matched the value given "
                      f"for step {step.index}; it does not identify one record. Supply "
                      f"a value that names a single record - an account number rather "
                      f"than a product type.")
            self._log(run.recorder, "ambiguous_request", detail, step=step.index,
                      matched=result.resolution.ambiguous)
            return index, run.result(Outcome.BUSINESS,
                                     business_outcome="ambiguous_request",
                                     business_detail=detail)

        recovered = self._try_recover(run, step, index)
        if recovered is not None:
            return recovered, None

        reason = (checkpoint.reason() if checkpoint is not None and not checkpoint.passed
                  else result.detail)

        escalated = self._try_escalate(run, step, index, reason)
        if escalated is not None:
            return escalated

        if run.recorder:
            run.recorder.failure(f"step {step.index} ({step.intent}) did not complete",
                                 surface=self.surface,
                                 label=f"step-{step.index}-failed",
                                 step=step.index, reason=reason)
        return index, run.result(
            Outcome.HARD_FAILURE, failed_step=step.index,
            error=f"step {step.index} ({step.intent}) did not complete",
            expected=(step.checkpoint.description if step.checkpoint
                      else f"the {getattr(step.action, 'kind', '?')} to succeed"),
            observed=reason)

    def _try_recover(self, run: _Run, step, index: int) -> int | None:
        """Handle a declared condition. Returns where to resume, or None."""
        condition = self._match_recoverable(run.plan)
        if condition is None:
            return None
        used = run.attempts.get(condition.name, 0)
        if used >= condition.max_attempts or len(run.recoveries) >= MAX_TOTAL_RECOVERIES:
            self._log(run.recorder, "recovery_exhausted", condition.name,
                      attempts=used, step=step.index)
            return None
        run.attempts[condition.name] = used + 1
        run.recoveries.append(condition.name)
        self._log(run.recorder, "recovering", condition.description,
                  name=condition.name, attempt=used + 1, step=step.index)
        for action in condition.recovery:
            self.surface.act(action)
        if condition.retry_from_step is None:
            return index
        return next((i for i, s in enumerate(run.plan.steps)
                     if s.index == condition.retry_from_step), index)

    def _try_escalate(self, run: _Run, step, index: int,
                      reason: str) -> tuple[int, ReplayResult | None] | None:
        """Bring in a person, if this session can be handed over.

        Once per step: a hopeless step must not be able to loop a human.
        """
        if not hasattr(self.surface, "escalate") or step.index in run.escalated:
            return None
        run.escalated.add(step.index)

        handoff = self.surface.escalate(
            reason=f"step {step.index} ({step.intent}) could not be completed",
            evidence_dir=run.recorder.dir if run.recorder else None,
            run_id=run.recorder.run_id if run.recorder else "",
            capability_id=run.plan.capability_id,
            goal=run.capability.provenance.goal,
            step_index=step.index, step_intent=step.intent,
            expected=(step.checkpoint.description if step.checkpoint
                      else "the step to succeed"),
            observed=reason)
        run.escalations.append(handoff.request_id)
        run.human_changes.extend(handoff.changes)
        self._log(run.recorder, "resumed", "control returned to automation",
                  step=step.index, changes=handoff.changes, operator=handoff.operator)

        if handoff.operator is None:
            return index, run.result(
                Outcome.ESCALATED, failed_step=step.index,
                error="no operator took the intervention", expected=reason,
                observed="intervention abandoned")

        # Resume where the operator said it is safe to. Someone who reset the
        # application has invalidated the steps before this one too.
        resume = index
        if (handoff.resumed_from_step is not None
                and handoff.resumed_from_step != step.index):
            resume = next((i for i, s in enumerate(run.plan.steps)
                           if s.index == handoff.resumed_from_step), index)
            self._log(run.recorder, "resuming_from",
                      f"operator set the resume point to step {handoff.resumed_from_step}",
                      step=step.index)
        return resume, None

    # ------------------------------------------------------------ conclusion
    def _conclude(self, run: _Run) -> ReplayResult:
        """Every step ran. The success checkpoint is the last word."""
        final = self.surface.check(run.plan.success)
        self._log(run.recorder, "success_checkpoint", run.plan.success.description,
                  passed=final.passed, reason=final.reason())
        if final.passed:
            needed_help = run.recoveries or run.escalations
            return run.result(Outcome.RECOVERED if needed_help else Outcome.SUCCESS)

        known = self._match_known(run.plan)
        if known is not None:
            return run.result(known.outcome, business_outcome=known.name,
                              business_detail=known.description)

        if run.recorder:
            run.recorder.failure("every step ran but the success condition was not met",
                                 surface=self.surface, label="success-check-failed")
        return run.result(
            Outcome.HARD_FAILURE,
            failed_step=run.plan.steps[-1].index if run.plan.steps else None,
            error="every step ran but the success condition was not met",
            expected=run.plan.success.description, observed=final.reason())

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
