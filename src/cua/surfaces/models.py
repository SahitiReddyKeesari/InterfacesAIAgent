"""The vocabulary of the perceive/act seam.

Nothing in this module knows about Playwright, a DOM, or a screen. That is the point:
these are the only types the discovery loop, the artifact and the replay engine ever
speak, so adding a surface (a legacy frameset app, a native desktop client) means adding
a Surface implementation rather than changing the recorded flow or the replayer.

The locator vocabulary is chosen for portability. Role, accessible name, label text and
ordinal position all exist in desktop accessibility APIs (AX on macOS, UIA on Windows)
as well as in a browser. A CSS selector does not, which is why one never appears here.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Role(str, Enum):
    """Surface-agnostic control roles. Deliberately small - these all map onto both
    ARIA roles and desktop accessibility roles."""

    BUTTON = "button"
    LINK = "link"
    TEXTBOX = "textbox"
    COMBOBOX = "combobox"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    TABLE = "table"
    ROW = "row"
    CELL = "cell"
    HEADING = "heading"
    TEXT = "text"
    IMAGE = "image"
    UNKNOWN = "unknown"


class Strategy(str, Enum):
    """How a control can be re-found, ordered by how durable it tends to be.

    LABEL_TEXT exists because of the environment this targets: legacy enterprise pages
    routinely have no <label for>, so a field's only stable clue is the caption text in
    the adjacent table cell. It is the single most useful strategy against these apps,
    and it has a direct desktop analogue (the static text preceding a control).
    """

    ROLE_NAME = "role_name"
    LABEL_TEXT = "label_text"
    CONTROL_ID = "control_id"
    TEXT = "text"
    COLUMN_CELL = "column_cell"
    ORDINAL = "ordinal"


class Candidate(BaseModel):
    """One way to find a control, with the reasoning preserved for review."""

    strategy: Strategy
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str

    model_config = {"frozen": True}


class Scope(BaseModel):
    """Narrow the search to the row (or panel) containing some text.

    This is what makes grid targeting survivable. "The Select link in row 3" breaks the
    moment results reorder; "the Select link in the row containing 12345" does not, and
    it is how an operator actually finds the row. The text may carry a {parameter}
    placeholder, which is what lets one recording serve every member id.
    """

    contains_text: str
    rationale: str = ""


class Locator(BaseModel):
    """An ordered fallback chain plus the frame it lives in.

    Replay walks the candidates in order and uses the first that resolves to exactly one
    control, recording which one won. A locator that repeatedly falls through to a weak
    candidate is a drift signal worth surfacing.
    """

    description: str
    role: Role | None = None
    frame_path: list[str] = Field(default_factory=list)
    scope: Scope | None = None
    candidates: list[Candidate]

    def best(self) -> Candidate:
        return self.candidates[0]


class Element(BaseModel):
    """One perceivable control in an Observation."""

    ref: str
    role: Role
    name: str = ""
    value: str | None = None
    label_text: str | None = None
    column_header: str | None = None
    max_length: int | None = None     # what the control will actually accept
    row_values: list[str] = Field(default_factory=list)   # for a grid cell, its row
    control_id: str | None = None
    text: str | None = None
    enabled: bool = True
    visible: bool = True
    frame_path: list[str] = Field(default_factory=list)
    ordinal: int = 0

    def summary(self) -> str:
        bits = [self.role.value]
        if self.name:
            bits.append(repr(self.name))
        elif self.label_text:
            bits.append(f"labelled {self.label_text!r}")
        if self.value:
            bits.append(f"= {self.value!r}")
        return " ".join(bits)


class Observation(BaseModel):
    """A snapshot of what is perceivable right now."""

    url: str
    title: str
    elements: list[Element] = Field(default_factory=list)
    frames: list[str] = Field(default_factory=list)
    text_digest: str = ""
    screenshot: str | None = None

    def by_ref(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)


# --------------------------------------------------------------------- actions
# A discriminated union rather than one Action class with optional fields: it
# serialises into an artifact a human can actually read, and it makes an
# unsupported action on a given surface a type-level fact rather than a runtime
# surprise.


class Click(BaseModel):
    kind: Literal["click"] = "click"
    locator: Locator


class Fill(BaseModel):
    kind: Literal["fill"] = "fill"
    locator: Locator
    text: str
    secret: bool = False


class Select(BaseModel):
    kind: Literal["select"] = "select"
    locator: Locator
    option: str


class Press(BaseModel):
    kind: Literal["press"] = "press"
    key: str


class Navigate(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url: str


class Read(BaseModel):
    """Extract a value into a named output the caller receives."""

    kind: Literal["read"] = "read"
    locator: Locator
    output: str


class WaitFor(BaseModel):
    kind: Literal["wait_for"] = "wait_for"
    locator: Locator | None = None
    text: str | None = None
    timeout_ms: int = 10_000


Action = Annotated[
    Union[Click, Fill, Select, Press, Navigate, Read, WaitFor],
    Field(discriminator="kind"),
]


class Checkpoint(BaseModel):
    """A predicate over what is currently perceivable.

    Checkpoints are how a step proves it actually arrived, rather than assuming the
    click worked. They are declarative so they can be recorded into an artifact and
    re-evaluated on replay without a model, and they are evaluated by *observing* -
    never by acting - so testing one can never change the state it is testing.

    `text_absent` matters as much as `text_present`: reaching the confirmation screen
    while an error banner is also on the page is not success.
    """

    description: str
    text_present: list[str] = Field(default_factory=list)
    text_absent: list[str] = Field(default_factory=list)
    locator_present: list[Locator] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.text_present or self.text_absent or self.locator_present)


class CheckResult(BaseModel):
    """Why a checkpoint passed or failed, in enough detail to debug from."""

    passed: bool
    checkpoint: str
    missing_text: list[str] = Field(default_factory=list)
    forbidden_text: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    def reason(self) -> str:
        if self.passed:
            return "checkpoint satisfied"
        bits = []
        if self.missing_text:
            bits.append(f"expected text absent: {self.missing_text}")
        if self.forbidden_text:
            bits.append(f"forbidden text present: {self.forbidden_text}")
        if self.unresolved:
            bits.append(f"expected controls missing: {self.unresolved}")
        return "; ".join(bits) or "checkpoint failed"


class Resolution(BaseModel):
    """What happened when a locator was resolved - the debugging record.

    `confidence` is the winning candidate's, and it matters more than `resolved`. A
    positional candidate can resolve to exactly one control and still be pointing at the
    wrong record - it identifies a slot, not a thing. Resolving weakly is therefore not
    success, it is a warning that the step is running on a fallback.
    """

    resolved: bool
    strategy: Strategy | None = None
    confidence: float = 0.0
    matches: int = 0
    fell_through: list[Strategy] = Field(default_factory=list)
    detail: str = ""

    @property
    def weak(self) -> bool:
        """True when only a low-trust candidate carried the step."""
        return self.resolved and self.confidence < 0.5


class ActionResult(BaseModel):
    """What an action did.

    `blocked` is separate from `ok` on purpose: a refusal by policy is not a failure of
    the automation, and a caller that cannot tell them apart will retry something it was
    told not to do.
    """

    ok: bool
    blocked: bool = False
    resolution: Resolution | None = None
    value: str | None = None
    detail: str = ""
