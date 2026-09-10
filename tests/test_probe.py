"""Tests for deriving known business outcomes without a model.

The property that matters: a signature must be what the application *said* about this
condition, not merely something that happened to differ between two runs. A signature
that is really a field caption or a row count will match situations that are not this
outcome, which is worse than having no signature at all.
"""
from __future__ import annotations


from cua.artifact.schema import KnownOutcome, Outcome
from cua.replay.engine import ReplayEngine
from cua.replay.probe import add_outcome, candidates, distinctive_text, propose_outcome
from cua.safety.policy import for_host
from cua.surfaces.models import Checkpoint, Element, Observation, Role
from cua.surfaces.web import PlaywrightSurface
from tests.fixtures.meridian_capability import build


def screen(*values: str, title: str = "App") -> Observation:
    return Observation(url="http://x.test/", title=title, elements=[
        Element(ref=f"r{i}", role=Role.TEXT, value=v) for i, v in enumerate(values)])


# ------------------------------------------------------------------- selection
def test_only_text_unique_to_the_probe_run_is_a_candidate():
    baseline = screen("Member Detail", "Account Relationships")
    probe = screen("Member Detail", "No member records match the criteria entered.")
    assert distinctive_text(baseline, probe) == [
        "No member records match the criteria entered."]


def test_a_message_is_preferred_over_a_row_count():
    """'Search Results (0)' differs from the happy path, but a count of zero can
    happen for reasons that are not this outcome."""
    outcome = propose_outcome(
        screen("Member Detail", "Account Relationships"),
        screen("Search Results (0)", "No member records match the criteria entered."),
        "member_not_found", "no such member")
    assert outcome.signature.text_present == [
        "No member records match the criteria entered."]


def test_a_condition_code_outranks_prose():
    """Wording gets rewritten; codes are referenced in runbooks."""
    outcome = propose_outcome(
        screen("Member Detail"),
        screen("Operator not authorized for this relationship.",
               "Access refused SEC-0917 for this operator."),
        "permission_denied", "not authorised")
    assert "SEC-0917" in outcome.signature.text_present[0]


def test_an_incidental_caption_is_not_used_as_a_signature():
    """Two runs stopping on different screens differ by every label on them."""
    outcome = propose_outcome(
        screen("Member Detail", "Account Relationships"),
        screen("Member / Name:", "Servicing Branch:",
               "No member records match the criteria entered."),
        "member_not_found", "no such member")
    assert outcome.signature.text_present == [
        "No member records match the criteria entered."]


def test_indistinguishable_runs_yield_no_signature():
    """An outcome that looks exactly like success cannot be recognised on replay, and
    inventing a signature would produce one that matches everything."""
    same = screen("Member Detail", "Account Relationships")
    assert propose_outcome(same, same, "ghost", "nothing happened") is None


def test_candidates_ignore_fragments_and_whole_panels():
    obs = screen("OK", "x" * 400, "No member records match the criteria entered.")
    found = candidates(obs)
    assert "OK" not in found                       # too short to identify anything
    assert "x" * 400 not in found                  # a panel, not a message
    assert "No member records match the criteria entered." in found


# -------------------------------------------------------------------- merging
def test_adding_an_outcome_replaces_one_of_the_same_name():
    first = KnownOutcome(name="dup", description="old",
                         signature=Checkpoint(description="x", text_present=["old"]))
    second = KnownOutcome(name="dup", description="new",
                          signature=Checkpoint(description="y", text_present=["new"]))
    from cua.artifact.schema import Capability, SurfaceBinding
    base = Capability(id="t.c", name="t", description="d",
                      surface=SurfaceBinding(entry_url="http://x.test/"),
                      success=Checkpoint(description="ok", text_present=["ok"]),
                      known_outcomes=[first])
    merged = add_outcome(base, second)
    assert [k.description for k in merged.known_outcomes] == ["new"]


def test_adding_an_outcome_leaves_the_original_untouched():
    from cua.artifact.schema import Capability, SurfaceBinding
    base = Capability(id="t.c", name="t", description="d",
                      surface=SurfaceBinding(entry_url="http://x.test/"),
                      success=Checkpoint(description="ok", text_present=["ok"]))
    add_outcome(base, KnownOutcome(name="a", description="b",
                                   signature=Checkpoint(description="s",
                                                        text_present=["s"])))
    assert base.known_outcomes == []


# ---------------------------------------------------------------- integration
def test_a_probed_outcome_turns_a_hard_failure_into_a_business_outcome(
        base_url, tmp_path, srv):
    """The whole point: a capability that could not tell 'no such member' from a broken
    application learns to, without anyone declaring it by hand."""
    capability = build(base_url)
    capability.known_outcomes = []          # start from what discovery would produce
    policy = for_host(base_url + "/meridian/")

    def run(cap, args):
        surface = PlaywrightSurface()
        try:
            engine = ReplayEngine(surface, policy, evidence_root=tmp_path)
            return engine.run(cap, args), engine.surface.observe()
        finally:
            surface.close()

    good, baseline = run(capability, {"member_id": "12345", "account_type": "Savings"})
    bad, probe = run(capability, {"member_id": "99999", "account_type": "Savings"})
    assert good.outcome is Outcome.SUCCESS
    assert bad.outcome is Outcome.HARD_FAILURE      # before probing

    derived = propose_outcome(baseline, probe, "member_not_found", "no such member")
    assert derived is not None
    learned = add_outcome(capability, derived)

    after, _ = run(learned, {"member_id": "99999", "account_type": "Savings"})
    assert after.outcome is Outcome.BUSINESS        # after probing
    assert after.business_outcome == "member_not_found"

    # and the happy path is unaffected
    still_good, _ = run(learned, {"member_id": "12345", "account_type": "Savings"})
    assert still_good.outcome is Outcome.SUCCESS
