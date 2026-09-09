"""Tests for the capability artifact: contract, versioning, binding, execution.

The schema's claims are testable ones - that it validates its own consistency, that it
never stores live values, that one recording serves many arguments, and that a declared
business outcome is reported as such rather than as a failure.
"""
from __future__ import annotations

import pytest

from cua.artifact.binding import REDACTED, BindingError, bind
from cua.artifact.schema import (ApprovalState, Capability, InputParam, KnownOutcome,
                                 OutputField, ParamType, RiskClass, Step, SurfaceBinding)
from cua.artifact.store import Store
from cua.surfaces.models import (Candidate, Checkpoint, Fill, Locator, Navigate,
                                 Read, Role, Scope, Strategy)


def a_locator(desc="a control", value="Caption:") -> Locator:
    return Locator(description=desc, role=Role.TEXTBOX,
                   candidates=[Candidate(strategy=Strategy.LABEL_TEXT, value=value,
                                         confidence=0.85, rationale="caption")])


def a_capability(**over) -> Capability:
    base = dict(
        id="demo.cap", name="Demo", description="A demo capability.",
        surface=SurfaceBinding(entry_url="http://example.test/app"),
        inputs=[InputParam(name="member_id", description="Member number.",
                           pattern=r"\d+", example="1")],
        outputs=[OutputField(name="who", description="Name.", source_step=2)],
        steps=[
            Step(index=0, intent="open", action=Navigate(url="http://example.test/app")),
            Step(index=1, intent="search",
                 action=Fill(locator=a_locator(), text="{member_id}")),
            Step(index=2, intent="read", action=Read(locator=a_locator(), output="who")),
        ],
        success=Checkpoint(description="done", text_present=["Detail"]),
    )
    base.update(over)
    return Capability(**base)


# ------------------------------------------------------------------- contract
def test_a_well_formed_capability_has_no_contract_problems():
    assert a_capability().validate_contract() == []


def test_placeholder_without_a_declared_input_is_caught():
    cap = a_capability(inputs=[])
    assert any("{member_id}" in p for p in cap.validate_contract())


def test_declared_but_unused_input_is_caught():
    cap = a_capability(inputs=[
        InputParam(name="member_id", description="x", pattern=r"\d+"),
        InputParam(name="unused", description="never referenced"),
    ])
    assert any("'unused'" in p for p in cap.validate_contract())


def test_output_with_no_step_producing_it_is_caught():
    cap = a_capability(outputs=[OutputField(name="ghost", description="nothing reads it")])
    assert any("ghost" in p for p in cap.validate_contract())


def test_empty_success_checkpoint_is_caught():
    cap = a_capability(success=Checkpoint(description="nothing asserted"))
    assert any("success checkpoint is empty" in p for p in cap.validate_contract())


def test_irreversible_steps_cannot_be_approved_without_declared_outcomes():
    cap = a_capability(approval=ApprovalState.APPROVED)
    cap.steps[1].risk = RiskClass.IRREVERSIBLE
    assert any("irreversible" in p for p in cap.validate_contract())


# ----------------------------------------------------------------- versioning
def test_fingerprint_ignores_provenance_and_approval():
    """Re-approving or re-recording must not look like the plan changed."""
    cap = a_capability()
    before = cap.fingerprint()
    cap.approval = ApprovalState.APPROVED
    cap.provenance.goal = "a different description of the same goal"
    assert cap.fingerprint() == before


def test_fingerprint_changes_when_the_plan_changes():
    cap = a_capability()
    before = cap.fingerprint()
    cap.steps[1].action.text = "{member_id}-edited"
    assert cap.fingerprint() != before


def test_saving_identical_content_does_not_bump_the_version(tmp_path):
    store = Store(tmp_path)
    store.save(a_capability())
    store.save(a_capability())
    assert store.versions("demo.cap") == [1]


def test_saving_a_changed_plan_creates_a_new_version(tmp_path):
    store = Store(tmp_path)
    store.save(a_capability())
    changed = a_capability()
    changed.steps[1].action.text = "{member_id} "
    store.save(changed)
    assert store.versions("demo.cap") == [1, 2]
    # the version an agent is already calling must still be loadable, unchanged
    assert store.load("demo.cap", 1).fingerprint() != store.load("demo.cap", 2).fingerprint()


def test_store_rejects_an_unsafe_capability_id(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path).save(a_capability(id="../escape"))


# -------------------------------------------------------------------- binding
def test_binding_rejects_arguments_that_violate_the_contract():
    cap = a_capability()
    for bad in ({"member_id": "abc"}, {}, {"member_id": "1", "extra": "x"}):
        with pytest.raises(BindingError):
            bind(cap, bad)


def test_binding_substitutes_into_actions_and_scopes():
    cap = a_capability()
    cap.steps[1].action.locator.scope = Scope(contains_text="{member_id}")
    plan = bind(cap, {"member_id": "12345"})
    assert plan.steps[1].action.text == "12345"
    assert plan.steps[1].action.locator.scope.contains_text == "12345"


def test_artifact_on_disk_never_contains_a_live_value(tmp_path):
    """The stored plan holds placeholders; only the runtime BoundPlan holds values."""
    store = Store(tmp_path)
    path = store.save(a_capability())
    assert "{member_id}" in path.read_text()
    assert "12345" not in path.read_text()


def test_sensitive_arguments_are_marked_secret_and_redactable():
    cap = a_capability(inputs=[InputParam(name="member_id", description="x",
                                          pattern=r"\d+", sensitive=True)])
    plan = bind(cap, {"member_id": "12345"})
    assert plan.steps[1].action.secret is True
    assert plan.scrub("looked up 12345 today") == f"looked up {REDACTED} today"
    assert "12345" not in repr(plan)


# ---------------------------------------------------------------- agent-facing
def test_tool_schema_exposes_inputs_outputs_and_known_outcomes():
    cap = a_capability(known_outcomes=[KnownOutcome(
        name="not_found", description="no such member",
        signature=Checkpoint(description="x", text_present=["No member"]))])
    schema = cap.tool_schema()
    assert schema["name"] == "demo.cap"
    assert schema["input_schema"]["required"] == ["member_id"]
    assert "who" in schema["returns"]
    assert "not_found" in schema["known_outcomes"]


def test_enum_inputs_reach_the_tool_schema_as_enums():
    cap = a_capability(inputs=[InputParam(name="member_id", description="x",
                                          type=ParamType.ENUM,
                                          enum_values=["A", "B"])])
    assert cap.tool_schema()["input_schema"]["properties"]["member_id"]["enum"] == ["A", "B"]


# ------------------------------------------------------------- live execution
# A deliberately minimal runner. The real engine - retries, recovery, the full
# outcome taxonomy - is the replay module; this exists only to prove the artifact
# is executable rather than merely declarative.
def run_plan(surface, plan) -> tuple[str, dict[str, str]]:
    outputs: dict[str, str] = {}
    for step in plan.steps:
        result = surface.act(step.action)
        if getattr(step.action, "kind", "") == "read" and result.ok:
            outputs[step.action.output] = result.value
        if step.checkpoint and not surface.check(step.checkpoint).passed:
            for known in plan.known_outcomes:
                if surface.check(known.signature).passed:
                    return known.name, outputs
            return f"step_{step.index}_failed", outputs
    passed = surface.check(plan.success).passed
    return ("success" if passed else "success_check_failed"), outputs


@pytest.fixture(scope="module")
def recorded(base_url):
    from tests.fixtures.meridian_capability import build
    return build(base_url)


def test_recorded_capability_satisfies_its_own_contract(recorded):
    assert recorded.validate_contract() == []


def test_every_recorded_locator_has_a_non_positional_primary(recorded):
    """Nothing in the plan may be identified primarily by where it sits."""
    for step in recorded.steps:
        loc = getattr(step.action, "locator", None)
        if loc is not None:
            assert loc.candidates[0].strategy is not Strategy.ORDINAL, step.intent


def test_recorded_capability_runs_and_returns_declared_outputs(surface, recorded):
    plan = bind(recorded, {"member_id": "12345", "account_type": "Savings"})
    verdict, outputs = run_plan(surface, plan)
    assert verdict == "success"
    assert outputs["member_name"] == "Dolores Abernathy"
    assert outputs["balance"] == "8421.55"
    assert set(outputs) == {o.name for o in recorded.outputs}


@pytest.mark.parametrize("member,account,expected_name,expected_balance", [
    ("12345", "Checking", "Dolores Abernathy", "1290.03"),
    ("12347", "Certificate", "Maeve Millay", "25000.00"),
])
def test_one_recording_serves_different_arguments(surface, recorded, member, account,
                                                  expected_name, expected_balance):
    """The point of parameterisation: no re-recording per member or per account."""
    plan = bind(recorded, {"member_id": member, "account_type": account})
    verdict, outputs = run_plan(surface, plan)
    assert verdict == "success"
    assert outputs["member_name"] == expected_name
    assert outputs["balance"] == expected_balance


def test_unknown_member_is_a_named_business_outcome_not_a_failure(surface, recorded):
    plan = bind(recorded, {"member_id": "99999", "account_type": "Savings"})
    verdict, _ = run_plan(surface, plan)
    assert verdict == "member_not_found"


def test_rewording_a_rationale_does_not_force_a_new_version():
    """Prose is documentation. A typo fix must not invalidate an approved plan."""
    cap = a_capability()
    before = cap.fingerprint()
    cap.steps[1].action.locator.candidates[0] = (
        cap.steps[1].action.locator.candidates[0].model_copy(
            update={"rationale": "reworded entirely"}))
    cap.steps[1].intent = "reworded intent"
    cap.description = "a completely different description"
    assert cap.fingerprint() == before


def test_changing_a_locator_value_does_force_a_new_version():
    cap = a_capability()
    before = cap.fingerprint()
    cap.steps[1].action.locator.candidates[0] = (
        cap.steps[1].action.locator.candidates[0].model_copy(
            update={"value": "Different Caption:"}))
    assert cap.fingerprint() != before
