"""Tests for the discovery loop's control flow and its recording decisions.

These use a scripted provider rather than a live model. What is being tested here is
not whether a model can drive a screen - the real run in /evidence proves that - but
that the loop stops when it should, records placeholders rather than live values, and
never lets a model's opinion become a durable locator.
"""
from __future__ import annotations

import re

import pytest

from cua.artifact.schema import RiskClass
from cua.discovery.agent import DiscoveryAgent, DiscoveryConfig, DiscoveryFailed
from cua.discovery.llm.base import LLMError
from cua.safety.policy import Policy, for_host
from cua.safety.surface import PolicySurface
from cua.surfaces.models import Strategy
from cua.surfaces.web import PlaywrightSurface


class ScriptedProvider:
    """Returns canned decisions, resolving `target` captions to whatever reference the
    observation actually used - so the tests do not depend on reference numbering."""

    name = "scripted"
    model = "scripted"

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.prompts: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self.prompts.append(user)
        if not self.script:
            raise LLMError("script exhausted")
        decision = dict(self.script.pop(0))
        wanted = decision.pop("target", None)
        # Optional, for controls whose caption repeats (a column heading appears once
        # per row): pick the line that also shows this value.
        value = decision.pop("target_value", None)
        if wanted:
            pattern = rf"^\s*(\S+)\s+\w+\s+'{re.escape(wanted)}'.*"
            if value:
                pattern += rf"'{re.escape(value)}'"
            match = re.search(pattern, user, re.M)
            if not match:
                raise AssertionError(
                    f"no control captioned {wanted!r} on screen:\n{user[:800]}")
            decision["target_ref"] = match.group(1)
        return decision


@pytest.fixture()
def agent_for(base_url):
    """Build an agent over a policy-guarded live surface."""
    made: list[PlaywrightSurface] = []

    def _make(script, policy=None):
        surface = PlaywrightSurface()
        made.append(surface)
        guarded = PolicySurface(surface, policy or for_host(base_url + "/meridian/"))
        return DiscoveryAgent(guarded, ScriptedProvider(script)), guarded

    yield _make
    for surface in made:
        surface.close()


HAPPY = [
    {"reasoning": "Enter the member number.", "action": "fill",
     "target": "Member / Name:", "text": "12345", "risk": "safe",
     "checkpoint_text": "Member Inquiry"},
    {"reasoning": "Run the search.", "action": "click", "target": "Search",
     "risk": "safe", "checkpoint_text": "Search Results"},
    {"reasoning": "Open the member record.", "action": "click", "target": "Select",
     "scope_text": "12345", "risk": "safe", "checkpoint_text": "Member Detail"},
    {"reasoning": "Read the member's name.", "action": "read", "target": "Name:",
     "output_name": "member_name", "risk": "safe"},
    {"reasoning": "Goal reached.", "action": "done",
     "success_text": "Account Relationships"},
]


def config(base_url, **over):
    base = dict(goal="Look up a member and read their name",
                entry_url=base_url + "/meridian/",
                capability_id="test.lookup",
                parameters={"member_id": "12345"})
    base.update(over)
    return DiscoveryConfig(**base)


# ------------------------------------------------------------------ the loop
def test_a_completed_run_produces_a_valid_capability(agent_for, base_url, srv):
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    assert capability.validate_contract() == []
    assert capability.steps[0].action.kind == "navigate"
    assert [s.action.kind for s in capability.steps[1:]] == ["fill", "click", "click", "read"]


def test_success_checkpoint_comes_from_the_model(agent_for, base_url, srv):
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    assert capability.success.text_present == ["Account Relationships"]


def test_read_steps_become_declared_outputs(agent_for, base_url, srv):
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    assert [o.name for o in capability.outputs] == ["member_name"]
    assert capability.outputs[0].source_step is not None


def test_giving_up_is_reported_not_silently_returned(agent_for, base_url, srv):
    agent, _ = agent_for([{"reasoning": "Cannot find it.", "action": "give_up",
                           "note": "no such screen"}])
    with pytest.raises(DiscoveryFailed, match="gave up"):
        agent.run(config(base_url))


def test_the_step_budget_is_enforced(agent_for, base_url, srv):
    """A loop with no budget is a loop that can run forever against a bank system."""
    filler = [{"reasoning": "type again", "action": "fill",
               "target": "Member / Name:", "text": "1"} for _ in range(10)]
    agent, _ = agent_for(filler)
    with pytest.raises(DiscoveryFailed, match="budget"):
        agent.run(config(base_url, max_steps=3))


def test_an_invalid_reference_is_skipped_rather_than_fatal(agent_for, base_url, srv):
    """A model naming a control that does not exist is a bad turn, not a crash."""
    script = [{"reasoning": "act on nothing", "action": "click",
               "target_ref": "main#999"}] + HAPPY
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    assert capability.validate_contract() == []


# --------------------------------------------------------------- recording
def test_example_values_are_recorded_as_placeholders(agent_for, base_url, srv):
    """The whole point of parameterisation - and the reason an artifact cannot carry
    the data of whoever it was recorded against."""
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    fill = next(s for s in capability.steps if s.action.kind == "fill")
    assert fill.action.text == "{member_id}"
    # The plan holds no live value. The declared input may carry an example, which is
    # documentation - unless it is sensitive, which the next test covers.
    plan = capability.model_dump_json(include={"steps", "success", "recoverable"})
    assert "12345" not in plan


def test_a_sensitive_parameter_leaves_no_example_anywhere(agent_for, base_url, srv):
    """Documentation is not an exemption from redaction."""
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url, sensitive_parameters={"member_id"}))
    assert capability.inputs[0].sensitive is True
    assert capability.inputs[0].example is None
    assert "12345" not in capability.model_dump_json()


def test_scope_text_is_parameterised_too(agent_for, base_url, srv):
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    scoped = next(s for s in capability.steps
                  if getattr(s.action, "locator", None)
                  and s.action.locator.scope is not None)
    assert scoped.action.locator.scope.contains_text == "{member_id}"


def test_a_parameter_no_step_uses_is_dropped_from_the_contract(agent_for, base_url, srv):
    """Declaring an input the plan never references would ask the caller for a value
    that changes nothing."""
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url, parameters={"member_id": "12345",
                                                        "unused": "zzz"}))
    assert [p.name for p in capability.inputs] == ["member_id"]


def test_locators_are_derived_from_the_observation_not_from_the_model(
        agent_for, base_url, srv):
    """The model picks which control; how it is found again is computed - so a grid
    link is never recorded by its row index."""
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    for step in capability.steps:
        locator = getattr(step.action, "locator", None)
        if locator:
            assert locator.candidates[0].strategy is not Strategy.ORDINAL
            assert locator.candidates[0].strategy is not Strategy.CONTROL_ID


def test_model_supplied_risk_is_carried_into_the_artifact(agent_for, base_url, srv):
    script = list(HAPPY)
    script[1] = {**script[1], "risk": "irreversible"}
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    risky = [s for s in capability.steps if s.risk is RiskClass.IRREVERSIBLE]
    assert len(risky) == 1


def test_provenance_records_how_the_capability_was_made(agent_for, base_url, srv):
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    assert capability.provenance.model == "scripted"
    assert capability.provenance.goal
    assert 0 < (capability.provenance.weakest_locator or 0) <= 1


# ------------------------------------------------------------------- policy
def test_discovery_cannot_act_outside_the_allowlist(agent_for, base_url, srv):
    elsewhere = Policy(allowed_url_patterns=[base_url + "/summit/*"])
    agent, _ = agent_for(HAPPY, policy=elsewhere)
    with pytest.raises(DiscoveryFailed, match="policy"):
        agent.run(config(base_url))


def test_the_model_cannot_alter_a_caller_supplied_value(agent_for, base_url, srv):
    """A real run failed exactly this way: the model zero-padded the member number to
    what it guessed a legacy field wanted, and the search then matched nothing. The
    model chooses which parameter; the value is the caller's, substituted verbatim."""
    script = list(HAPPY)
    script[0] = {"reasoning": "Enter the member number.", "action": "fill",
                 "target": "Member / Name:", "parameter": "member_id",
                 "text": "1234500000000000000000",   # the model's mangled version
                 "risk": "safe"}
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    fill = next(s for s in capability.steps if s.action.kind == "fill")
    assert fill.action.text == "{member_id}"
    assert "00000" not in capability.model_dump_json()


def test_the_prompt_names_the_available_parameters(agent_for, base_url, srv):
    """The model has to know which parameters exist before it can name one."""
    agent, _ = agent_for(HAPPY)
    agent.run(config(base_url))
    first_prompt = agent.llm.prompts[0]
    assert "member_id" in first_prompt
    assert "never retype" in first_prompt


def test_the_parameter_field_is_constrained_to_declared_names():
    """A free-text parameter field invited the model to write an explanation into the
    value instead of naming a parameter. An enum makes that impossible."""
    from cua.discovery.prompts import NO_PARAMETER, decision_schema

    schema = decision_schema(["member_id", "account_type"])
    assert schema["properties"]["parameter"]["enum"] == [
        "member_id", "account_type", NO_PARAMETER]


def test_an_unrecognised_parameter_name_falls_back_to_the_literal(agent_for, base_url, srv):
    script = list(HAPPY)
    script[0] = {**script[0], "parameter": "not_declared", "text": "12345"}
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    fill = next(s for s in capability.steps if s.action.kind == "fill")
    assert fill.action.text == "{member_id}"   # the literal still gets parameterised


def test_a_value_too_long_for_the_field_is_refused(agent_for, base_url, srv):
    """A real run typed a thousand characters of model deliberation into a 32-character
    member field; the browser truncated it and the search matched nothing. A value the
    control cannot hold is a wrong value, not something to type anyway."""
    script = [{"reasoning": "type a novel", "action": "fill",
               "target": "Member / Name:", "parameter": "none",
               "text": "x" * 400}] + HAPPY
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    # The oversized step was dropped, and the run still completed via the good ones.
    assert all(getattr(s.action, "text", "") != "x" * 400 for s in capability.steps)
    assert capability.validate_contract() == []


def test_field_capacity_is_perceived(surface, base_url):
    """maxlength is part of what a control is - the loop cannot respect a limit it
    cannot see."""
    from cua.surfaces.models import Navigate, Role
    surface.act(Navigate(url=base_url + "/meridian/"))
    field = next(e for e in surface.observe().elements
                 if e.role is Role.TEXTBOX and (e.label_text or "").startswith("Member"))
    assert field.max_length == 32


def test_a_grid_cell_read_gets_a_row_scope_without_the_model_supplying_one(
        agent_for, base_url, srv):
    """A column-based locator needs a row to look in. The row is visible at record
    time, so the anchor is derived from perception rather than hoped for."""
    script = [
        {"reasoning": "Enter the member number.", "action": "fill",
         "target": "Member / Name:", "parameter": "member_id", "text": ""},
        {"reasoning": "Search.", "action": "click", "target": "Search",
         "parameter": "none", "checkpoint_text": "Search Results"},
        {"reasoning": "Open the record.", "action": "click", "target": "Select",
         "parameter": "none", "scope_text": "12345", "checkpoint_text": "Member Detail"},
        {"reasoning": "Read the balance.", "action": "read",
         "target": "Current Balance", "target_value": "8421.55",
         "parameter": "none", "output_name": "balance"},   # deliberately no scope_text
        {"reasoning": "Done.", "action": "done", "parameter": "none",
         "success_text": "Account Relationships"},
    ]
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url, parameters={"member_id": "12345",
                                                        "account_type": "Savings"}))
    read = next(s for s in capability.steps if s.action.kind == "read")
    assert read.action.locator.scope is not None, "the read needs a row anchor"
    # And the anchor is parameterised, so one recording serves every account type.
    assert read.action.locator.scope.contains_text == "{account_type}"


def test_a_transient_model_outage_does_not_discard_the_run(agent_for, base_url, srv):
    """Losing completed turns to a brief outage wastes surface state that cannot be
    rebuilt without repeating every action."""
    class FlakyProvider(ScriptedProvider):
        def __init__(self, script):
            super().__init__(script)
            self.failures = 0

        def complete_json(self, system, user, schema):
            if self.failures < 1:
                self.failures += 1
                raise LLMError("503 everything is busy")
            return super().complete_json(system, user, schema)

    agent, _ = agent_for(HAPPY)
    agent.llm = FlakyProvider(HAPPY)
    cfg = config(base_url)
    cfg.model_retry_pause_s = 0.01          # no need to actually wait in a test
    capability = agent.run(cfg)
    assert agent.llm.failures == 1
    assert capability.validate_contract() == []


def test_a_row_anchor_is_never_a_volatile_value(agent_for, base_url, srv):
    """An earlier version anchored a Status cell to the balance beside it - a step that
    breaks the next time the member spends anything. A caller-supplied value is
    preferred, and a bare number is never chosen."""
    script = [
        {"reasoning": "Enter the member number.", "action": "fill",
         "target": "Member / Name:", "parameter": "member_id", "text": ""},
        {"reasoning": "Search.", "action": "click", "target": "Search",
         "parameter": "none"},
        {"reasoning": "Open the record.", "action": "click", "target": "Select",
         "parameter": "none", "scope_text": "12345"},
        {"reasoning": "Read the status.", "action": "read", "target": "Status",
         "target_value": "Open", "parameter": "none"},
        {"reasoning": "Done.", "action": "done", "parameter": "none",
         "success_text": "Account Relationships"},
    ]
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url, parameters={"member_id": "12345",
                                                        "account_type": "Savings"}))
    read = next(s for s in capability.steps if s.action.kind == "read")
    assert read.action.locator.scope.contains_text == "{account_type}"


def test_an_unnamed_output_is_named_from_its_column(agent_for, base_url, srv):
    """`value_4` in a published contract is a contract nobody can read."""
    script = [
        {"reasoning": "Enter the member number.", "action": "fill",
         "target": "Member / Name:", "parameter": "member_id", "text": ""},
        {"reasoning": "Search.", "action": "click", "target": "Search", "parameter": "none"},
        {"reasoning": "Open.", "action": "click", "target": "Select",
         "parameter": "none", "scope_text": "12345"},
        {"reasoning": "Read the balance.", "action": "read", "target": "Current Balance",
         "target_value": "8421.55", "parameter": "none"},        # no output_name
        {"reasoning": "Done.", "action": "done", "parameter": "none",
         "success_text": "Account Relationships"},
    ]
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url, parameters={"member_id": "12345",
                                                        "account_type": "Savings"}))
    assert [o.name for o in capability.outputs] == ["current_balance"]


def test_the_success_phrase_is_parameterised(agent_for, base_url, srv):
    """A success check naming the member it was recorded against fails for every other
    member - which would make the capability worthless the first time it is reused."""
    script = list(HAPPY)
    script[-1] = {"reasoning": "Goal reached.", "action": "done", "parameter": "none",
                  "success_text": "Member Detail — 12345"}
    agent, _ = agent_for(script)
    capability = agent.run(config(base_url))
    assert capability.success.text_present == ["Member Detail — {member_id}"]


def test_the_recorded_surface_kind_is_not_a_wrapper_name(agent_for, base_url, srv):
    """`policy(web)` is a description of our plumbing, not of the surface."""
    agent, _ = agent_for(HAPPY)
    capability = agent.run(config(base_url))
    assert capability.surface.kind == "web"
