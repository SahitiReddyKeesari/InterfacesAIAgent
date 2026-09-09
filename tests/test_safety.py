"""Tests for the guardrail model.

Two properties matter more than the individual rules: the allowlist is default-deny,
and a policy refusal is reported as a refusal rather than as a failure. A caller that
cannot tell the two apart will retry something it was told not to do.
"""
from __future__ import annotations

import pytest

from cua.artifact.schema import ApprovalState, RiskClass, Step
from cua.safety.policy import Decision, Policy, for_host
from cua.safety.redaction import REDACTED, Redactor
from cua.safety.surface import PolicySurface
from cua.surfaces import locators
from cua.surfaces.base import Surface
from cua.surfaces.models import Click, Element, Fill, Navigate, Role


# --------------------------------------------------------------------- policy
def test_an_unconfigured_policy_permits_nothing():
    """Empty means 'nothing permitted', never 'no restriction'. A policy that opens up
    when misconfigured is worse than none, because it reads like protection."""
    assert Policy().check_navigation("http://127.0.0.1:8080/").decision is Decision.BLOCK


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:8080/meridian/", True),
    ("http://127.0.0.1:8080/summit/memberSearch.do", True),
    ("https://127.0.0.1:8080/meridian/", False),      # scheme differs
    ("http://127.0.0.1:9999/meridian/", False),       # port differs
    ("http://evil.test/", False),
    ("http://127.0.0.1:8080.evil.test/", False),      # substring, not a match
])
def test_url_allowlist_matches_origins_not_substrings(url, allowed):
    policy = for_host("http://127.0.0.1:8080/meridian/")
    assert policy.url_allowed(url) is allowed


def a_click() -> Click:
    return Click(locator=locators.build(
        Element(ref="r", role=Role.LINK, name="Go")))


def test_action_kinds_outside_the_allowlist_are_blocked():
    """A read-only policy must refuse to write, whatever the caller intends."""
    read_only = Policy(allowed_url_patterns=["*"], allowed_actions={"read", "wait_for"})
    assert read_only.check_action(a_click()).decision is Decision.BLOCK


def test_action_kinds_inside_the_allowlist_are_permitted():
    full = Policy(allowed_url_patterns=["*"], allowed_actions={"click"})
    assert full.check_action(a_click()).decision is Decision.ALLOW


def test_irreversible_steps_require_confirmation_by_default():
    step = Step(index=3, intent="Block the card permanently",
                action=Navigate(url="http://x.test/"), risk=RiskClass.IRREVERSIBLE)
    verdict = Policy(allowed_url_patterns=["*"]).check_step(step)
    assert verdict.decision is Decision.CONFIRM
    assert "irreversible" in verdict.reason


def test_safe_steps_need_no_confirmation():
    step = Step(index=1, intent="Read a balance",
                action=Navigate(url="http://x.test/"), risk=RiskClass.SAFE)
    assert Policy(allowed_url_patterns=["*"]).check_step(step).decision is Decision.ALLOW


def test_draft_artifacts_can_be_gated_from_unattended_use():
    policy = Policy(allowed_url_patterns=["*"], require_approved_artifact=True)
    step = Step(index=0, intent="anything", action=Navigate(url="http://x.test/"))
    assert policy.check_step(step, ApprovalState.DRAFT).decision is Decision.CONFIRM
    assert policy.check_step(step, ApprovalState.APPROVED).decision is Decision.ALLOW


# ------------------------------------------------------------------ redaction
@pytest.mark.parametrize("text,expected_change", [
    ("Tax ID: 412-88-9031", True),
    ("Card 4539881022454412 on file", True),
    ("Card 4539 8810 2245 4412", True),
    ("Masked XXX-XX-9031 only", False),
    ("Masked **** **** **** 4412", False),
    ("Account 0001234501 balance 8421.55", False),
])
def test_redaction_catches_sensitive_shapes_without_mangling_other_numbers(
        text, expected_change):
    assert (Redactor().scrub(text) != text) is expected_change


def test_a_masked_pan_keeps_its_last_four_for_debuggability():
    assert Redactor().scrub("Card 4539881022454412") == f"Card {REDACTED}4412"


def test_declared_secrets_are_removed():
    assert Redactor(secrets=["hunter2"]).scrub("token hunter2 ok") == f"token {REDACTED} ok"


def test_longer_secrets_are_masked_before_shorter_substrings():
    r = Redactor(secrets=["123", "1234567"])
    assert r.scrub("value 1234567 here") == f"value {REDACTED} here"


# ------------------------------------------------------------ policy surface
@pytest.fixture()
def guarded(base_url):
    from cua.surfaces.web import PlaywrightSurface
    inner = PlaywrightSurface()
    surface = PolicySurface(inner, for_host(base_url + "/meridian/"))
    try:
        yield surface
    finally:
        surface.close()


def test_policy_surface_is_a_surface(guarded):
    assert isinstance(guarded, Surface)


def test_navigation_outside_the_allowlist_is_refused_not_failed(guarded):
    result = guarded.act(Navigate(url="https://example.com/"))
    assert result.blocked is True
    assert result.ok is False


def test_permitted_navigation_proceeds(guarded, base_url):
    assert guarded.act(Navigate(url=base_url + "/meridian/")).ok


def test_a_click_that_leaves_the_permitted_surface_is_caught(guarded, tmp_path, base_url):
    """The allowlist must survive navigation the page initiates, not only navigation
    the automation requests."""
    outside = tmp_path / "outside.html"
    outside.write_text("<h1>Elsewhere</h1>")
    inside = tmp_path / "inside.html"
    inside.write_text(f'<a href="{outside.as_uri()}">Leave</a>')

    policy = Policy(allowed_url_patterns=[inside.as_uri()])
    guarded.policy = policy
    guarded.inner.act(Navigate(url=inside.as_uri()))       # bypass to set the scene
    obs = guarded.observe()
    link = next(e for e in obs.elements if e.role is Role.LINK)
    result = guarded.act(Click(locator=locators.build(link)))
    assert result.blocked is True
    assert "left the permitted surface" in result.detail


def test_a_secret_fill_is_scrubbed_from_later_observations(guarded, base_url):
    guarded.act(Navigate(url=base_url + "/meridian/"))
    obs = guarded.observe()
    field = next(e for e in obs.elements if e.role is Role.TEXTBOX
                 and (e.label_text or "").startswith("Member"))
    guarded.act(Fill(locator=locators.build(field), text="12345", secret=True))
    button = next(e for e in obs.elements if e.role is Role.LINK and e.name == "Search")
    guarded.act(Click(locator=locators.build(button)))

    after = guarded.observe()
    assert "12345" not in after.text_digest
    assert REDACTED in after.text_digest


def test_confirmation_is_refused_when_no_approver_is_supplied():
    """Unattended execution must not silently self-approve a risky action."""
    from cua.safety.surface import _deny
    assert _deny(Navigate(url="http://x.test/"), "because") is False
