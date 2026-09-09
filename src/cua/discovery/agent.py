"""The LLM-driven observe -> decide -> act loop.

This is the only place a model is in the decision loop, and it runs once. What it
produces is not a transcript but a capability artifact: a typed, parameterised plan that
replay can execute forever without asking anything.

Two things here are worth more than the loop itself:

* **Parameterisation happens at record time.** The caller says which values are
  arguments and supplies an example of each. Wherever the model types or matches one of
  those examples, the artifact records `{member_id}` instead of `12345`. That is what
  makes one recording serve every member - and it is also why a saved artifact cannot
  contain the data of whoever it was recorded against.
* **Locators are derived from the observation, not from the model.** The model chooses
  *which* control; how that control will be found again is computed from what was
  perceived, with the whole observation passed as peers so a generated id can be judged
  structurally. A model is good at intent and unreliable at durable identifiers.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..artifact.schema import (Capability, InputParam, OutputField, Provenance,
                               RiskClass, Step, SurfaceBinding)
from ..evidence.recorder import RunRecorder
from ..surfaces import locators
from ..surfaces.base import Surface
from ..surfaces.models import (Checkpoint, Click, Fill, Navigate, Observation,
                               Read, Scope, Select)
from .llm.base import LLMError, LLMProvider
from .prompts import NO_PARAMETER, SYSTEM, decision_schema, render_observation


class DiscoveryFailed(RuntimeError):
    """The run ended without reaching the goal."""


@dataclass
class DiscoveryConfig:
    goal: str
    entry_url: str
    capability_id: str
    parameters: dict[str, str] = field(default_factory=dict)   # name -> example value
    parameter_docs: dict[str, str] = field(default_factory=dict)
    sensitive_parameters: set[str] = field(default_factory=set)
    max_steps: int = 18
    name: str = ""
    description: str = ""


class DiscoveryAgent:
    def __init__(self, surface: Surface, llm: LLMProvider,
                 recorder: RunRecorder | None = None):
        self.surface = surface
        self.llm = llm
        self.recorder = recorder
        # Kept for evidence only. The artifact is deliberately decoupled from the raw
        # transcript - a capability must be reviewable without reading the model's
        # deliberations - but a discovery run has to be able to show its working.
        self._transcript: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def _log(self, kind: str, message: str = "", **data) -> None:
        if self.recorder is not None:
            self.recorder.event(kind, message, **data)

    def _placeholder(self, value: str | None, parameters: dict[str, str]) -> str | None:
        """Swap an example value back to its parameter name, longest match first so a
        short value that is a substring of a longer one cannot mask it."""
        if not value:
            return value
        for name, example in sorted(parameters.items(),
                                    key=lambda kv: len(str(kv[1])), reverse=True):
            if example and str(example) in value:
                value = value.replace(str(example), "{" + name + "}")
        return value

    @staticmethod
    def _risk(raw: str | None) -> RiskClass:
        try:
            return RiskClass(raw or "safe")
        except ValueError:
            return RiskClass.SAFE

    @staticmethod
    def _checkpoint(text: str | None, intent: str) -> Checkpoint | None:
        if not text or not text.strip():
            return None
        return Checkpoint(description=f"After: {intent}", text_present=[text.strip()])

    # ------------------------------------------------------------------- run
    def run(self, config: DiscoveryConfig) -> Capability:
        started = time.monotonic()
        self._log("discovery_started", config.goal, entry_url=config.entry_url,
                  model=getattr(self.llm, "model", "?"), budget=config.max_steps)

        schema = decision_schema(list(config.parameters))
        steps: list[Step] = [Step(index=0, intent="Open the application.",
                                  action=Navigate(url=config.entry_url),
                                  risk=RiskClass.SAFE)]
        outputs: list[OutputField] = []
        history: list[str] = []
        success_text: str | None = None

        result = self.surface.act(Navigate(url=config.entry_url))
        if not result.ok:
            raise DiscoveryFailed(f"could not open {config.entry_url}: {result.detail}")

        for turn in range(1, config.max_steps + 1):
            observation = self.surface.observe()
            prompt = render_observation(observation, config.goal, history,
                                        turn, config.max_steps, config.parameters)
            self._transcript.append({"turn": turn, "screen": prompt})
            try:
                decision = self.llm.complete_json(SYSTEM, prompt, schema)
                self._transcript[-1]["decision"] = decision
            except LLMError as exc:
                self._log("model_error", str(exc), turn=turn)
                self._save_transcript()
                raise DiscoveryFailed(f"model failed at step {turn}: {exc}") from exc

            action_kind = decision.get("action", "")
            reasoning = decision.get("reasoning", "").strip()
            self._log("decision", reasoning, turn=turn, action=action_kind,
                      target=decision.get("target_ref"), risk=decision.get("risk"))

            if action_kind == "done":
                success_text = decision.get("success_text") or None
                history.append(f"declared done: {reasoning}")
                break
            if action_kind == "give_up":
                self._save_transcript()
                raise DiscoveryFailed(f"model gave up at step {turn}: "
                                      f"{decision.get('note') or reasoning}")

            step = self._build_step(decision, observation, len(steps), config)
            if step is None:
                history.append(f"step {turn} rejected: could not use that reference")
                continue

            outcome = self.surface.act(self._concrete(step, config.parameters))
            self._log("acted", step.intent, turn=turn, ok=outcome.ok,
                      strategy=outcome.resolution.strategy.value
                      if outcome.resolution and outcome.resolution.strategy else None,
                      blocked=outcome.blocked, detail=outcome.detail)

            if outcome.blocked:
                raise DiscoveryFailed(f"policy blocked the run: {outcome.detail}")
            if not outcome.ok:
                history.append(f"step {turn} failed: {outcome.detail}")
                continue

            steps.append(step)
            history.append(f"{step.action.kind}: {step.intent}")
            if isinstance(step.action, Read):
                outputs.append(OutputField(
                    name=step.action.output,
                    description=reasoning or f"Value read at step {step.index}.",
                    source_step=step.index))
        else:
            self._save_transcript()
            raise DiscoveryFailed(f"step budget of {config.max_steps} exhausted "
                                  f"without reaching the goal")

        capability = self._assemble(config, steps, outputs, success_text)
        self._save_transcript()
        self._log("discovery_finished", capability.id,
                  steps=len(capability.steps), outputs=len(capability.outputs),
                  duration_ms=int((time.monotonic() - started) * 1000))
        if self.recorder is not None:
            self.recorder.attach("capability.json", capability.model_dump_json(indent=2))
        return capability

    def _save_transcript(self) -> None:
        """Write the model transcript. Called on every exit path - a run that failed is
        exactly when someone needs to see what the model was looking at."""
        if self.recorder is not None:
            self.recorder.attach("transcript.json", json.dumps(self._transcript, indent=2))

    # -------------------------------------------------------------- assembly
    def _build_step(self, decision: dict, observation: Observation, index: int,
                    config: DiscoveryConfig) -> Step | None:
        """Turn one decision into a recorded step, or None if it cannot be used."""
        kind = decision["action"]
        element = observation.by_ref(decision.get("target_ref", "") or "")
        if element is None:
            self._log("bad_reference", decision.get("target_ref", ""), action=kind)
            return None

        scope_text = self._placeholder(decision.get("scope_text"), config.parameters)
        scope = (Scope(contains_text=scope_text,
                       rationale="identifies the row by a value it displays rather than "
                                 "by its position, chosen during discovery")
                 if scope_text else None)
        # Peers let a generated id be judged against its siblings rather than by
        # pattern-matching one framework's conventions.
        locator = locators.build(element, decision.get("reasoning", "")[:80] or None,
                                 scope=scope, peers=observation.elements)

        # The value is the caller's, never the model's. A model asked to "type the
        # member number" may helpfully reformat or zero-pad it to match what it thinks
        # a legacy field wants, and a value it adjusted no longer matches the caller's.
        # It chooses the parameter; the value is substituted verbatim.
        named = (decision.get("parameter") or "").strip()
        if named in config.parameters:
            text = "{" + named + "}"
        else:
            if named and named != NO_PARAMETER:
                self._log("unknown_parameter", named,
                          note="not a declared parameter; falling back to the literal")
            text = self._placeholder(decision.get("text"), config.parameters)
        intent = decision.get("reasoning", "").strip() or f"{kind} {locator.description}"
        risk = self._risk(decision.get("risk"))
        checkpoint = self._checkpoint(decision.get("checkpoint_text"), intent)

        if kind == "fill":
            literal = text or ""
            if element.max_length and len(literal) > element.max_length:
                # The browser would truncate this silently and the flow would proceed
                # with a value nobody chose - which is exactly how a real run typed a
                # model's deliberations into a member-number field.
                self._log("value_rejected",
                          f"value of {len(literal)} chars exceeds the "
                          f"{element.max_length}-char field",
                          field=element.label_text or element.name)
                return None
            action = Fill(locator=locator, text=literal)
        elif kind == "select":
            action = Select(locator=locator, option=text or "")
        elif kind == "click":
            action = Click(locator=locator)
        elif kind == "read":
            name = (decision.get("output_name") or f"value_{index}").strip()
            action = Read(locator=locator, output=name)
        else:
            return None

        return Step(index=index, intent=intent, action=action, risk=risk,
                    checkpoint=checkpoint)

    @staticmethod
    def _concrete(step: Step, parameters: dict[str, str]):
        """The step as recorded holds placeholders; acting needs the example values."""
        action = step.action.model_copy(deep=True)
        for attribute in ("text", "option"):
            value = getattr(action, attribute, None)
            if isinstance(value, str):
                for name, example in parameters.items():
                    value = value.replace("{" + name + "}", str(example))
                setattr(action, attribute, value)
        scope = getattr(getattr(action, "locator", None), "scope", None)
        if scope is not None:
            for name, example in parameters.items():
                scope.contains_text = scope.contains_text.replace(
                    "{" + name + "}", str(example))
        return action

    def _assemble(self, config: DiscoveryConfig, steps: list[Step],
                  outputs: list[OutputField], success_text: str | None) -> Capability:
        success = Checkpoint(
            description=success_text or f"The goal was reached: {config.goal}",
            text_present=[success_text] if success_text else [])

        inputs = []
        for name, example in config.parameters.items():
            secret = name in config.sensitive_parameters
            default_doc = ("Sensitive value supplied per invocation."
                           if secret else
                           f"Value supplied per invocation (example: {example}).")
            inputs.append(InputParam(
                name=name,
                description=config.parameter_docs.get(name, default_doc),
                example=None if secret else str(example),
                sensitive=secret))
        # An input nothing referenced is a contract lie - the caller would be asked for
        # a value that changes nothing. Drop it and say so.
        used = {p for step in steps for p in _placeholders_in(step)}
        kept = [p for p in inputs if p.name in used]
        for dropped in [p.name for p in inputs if p.name not in used]:
            self._log("unused_parameter", dropped,
                      note="declared but never referenced by any recorded step")

        weakest = min(
            (locators.durability(step.action.locator)
             for step in steps if getattr(step.action, "locator", None)),
            default=None)

        return Capability(
            id=config.capability_id,
            name=config.name or config.capability_id.replace(".", " ").replace("_", " "),
            description=config.description or config.goal,
            surface=SurfaceBinding(kind=getattr(self.surface, "name", "web"),
                                   entry_url=config.entry_url),
            inputs=kept, outputs=outputs, steps=steps, success=success,
            provenance=Provenance(goal=config.goal,
                                  model=getattr(self.llm, "model", None),
                                  run_id=self.recorder.run_id if self.recorder else None,
                                  discovery_steps=len(steps),
                                  weakest_locator=weakest))


def _placeholders_in(step: Step) -> set[str]:
    from ..artifact.schema import PLACEHOLDER
    return set(PLACEHOLDER.findall(step.model_dump_json()))
