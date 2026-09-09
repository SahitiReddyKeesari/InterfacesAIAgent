"""What the automation is permitted to do, and where.

The guardrail model has two entry points because the two questions are asked by
different layers, and neither can answer the other's:

* `check_action` is asked by the surface, which sees *what is about to happen* - a
  click, a navigation to a URL - but has no idea why. Enforcing the allowlist here,
  below everything else, is what makes it unbypassable: a bug in the discovery loop
  cannot navigate somewhere it should not, because the loop is not the thing enforcing
  it.

* `check_step` is asked by the engines, which see *intent* - this step is irreversible,
  this capability is only a draft. The surface cannot classify risk because risk is a
  property of the recorded plan, not of a click.

Both return a verdict rather than raising. A blocked action is a legitimate outcome the
caller has to report, not an exception to unwind on.
"""
from __future__ import annotations

import fnmatch
import re
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from ..artifact.schema import ApprovalState, RiskClass


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"   # permitted only with explicit human approval
    BLOCK = "block"


class Verdict(BaseModel):
    decision: Decision
    reason: str = ""
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


ALLOW = Verdict(decision=Decision.ALLOW)


class Policy(BaseModel):
    """An explicit, configurable allowlist. Default-deny in both dimensions.

    Empty lists mean "nothing permitted" rather than "no restriction". A policy that
    silently permits everything when misconfigured is worse than no policy, because it
    reads like protection.
    """

    name: str = "default"
    allowed_url_patterns: list[str] = Field(default_factory=list)
    allowed_actions: set[str] = Field(
        default_factory=lambda: {"navigate", "click", "fill", "select", "press",
                                 "read", "wait_for"})
    # Risk classes that may never run unattended, whatever the artifact says.
    confirm_risks: set[RiskClass] = Field(
        default_factory=lambda: {RiskClass.IRREVERSIBLE})
    blocked_risks: set[RiskClass] = Field(default_factory=set)
    require_approved_artifact: bool = False
    max_steps: int = 60

    # ------------------------------------------------------------------ urls
    def url_allowed(self, url: str) -> bool:
        """Match scheme://host/path against the patterns. Host is matched exactly or
        by glob; a pattern never matches by mere substring, so `evil-example.test`
        cannot pass a rule written for `example.test`."""
        if not url:
            return False
        parsed = urlparse(url)
        target = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        return any(fnmatch.fnmatch(target, pattern)
                   for pattern in self.allowed_url_patterns)

    def check_navigation(self, url: str) -> Verdict:
        if self.url_allowed(url):
            return ALLOW
        return Verdict(decision=Decision.BLOCK, rule="allowed_url_patterns",
                       reason=f"{url!r} is outside the permitted surface")

    # --------------------------------------------------------------- actions
    def check_action(self, action) -> Verdict:
        kind = getattr(action, "kind", "")
        if kind not in self.allowed_actions:
            return Verdict(decision=Decision.BLOCK, rule="allowed_actions",
                           reason=f"action {kind!r} is not permitted by policy {self.name!r}")
        if kind == "navigate":
            return self.check_navigation(action.url)
        return ALLOW

    # ----------------------------------------------------------------- steps
    def check_step(self, step, approval: ApprovalState = ApprovalState.DRAFT) -> Verdict:
        """Risk gate. Consulted by the engines, which know what a step means."""
        if step.risk in self.blocked_risks:
            return Verdict(decision=Decision.BLOCK, rule="blocked_risks",
                           reason=f"step {step.index} is {step.risk.value} and this "
                                  f"policy forbids that class outright")
        if self.require_approved_artifact and approval is not ApprovalState.APPROVED:
            return Verdict(decision=Decision.CONFIRM, rule="require_approved_artifact",
                           reason=f"capability is {approval.value}; unattended execution "
                                  f"requires an approved artifact")
        if step.risk in self.confirm_risks:
            return Verdict(decision=Decision.CONFIRM, rule="confirm_risks",
                           reason=f"step {step.index} is {step.risk.value} "
                                  f"({step.intent}) and needs explicit approval")
        return ALLOW


def for_host(url: str, **over) -> Policy:
    """A policy scoped to one application's origin - the common case."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return Policy(allowed_url_patterns=[origin, f"{origin}/*"], **over)
