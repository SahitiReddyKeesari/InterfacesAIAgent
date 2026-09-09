"""Building locators from perceived elements.

The ordering here is the robustness argument, and it is the part of the system a
reviewer should read most carefully. Each candidate carries a rationale that travels
into the artifact, so a human reviewing a recorded capability can see *why* a control
is identified the way it is - not just how.

Ordering principle: prefer what a human operator would use to find the control
(its role and its caption) over what happens to be true of this particular render
(its generated id, its position in a list).
"""
from __future__ import annotations

import re

from .models import Candidate, Element, Locator, Role, Scope, Strategy

# ASP.NET GridView-style ids embed the row index: ..._gvResults_ctl02_lnkSelect.
# Those are positional by construction - the same link for a different row differs only
# by the number - so they must never be trusted as a primary identifier.
_POSITIONAL_ID = re.compile(r"(_ctl\d+_|\$ctl\d+\$)")

_INPUT_ROLES = {Role.TEXTBOX, Role.COMBOBOX, Role.CHECKBOX, Role.RADIO}
_CLICKABLE_ROLES = {Role.BUTTON, Role.LINK}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def build(element: Element, description: str | None = None,
          scope: Scope | None = None) -> Locator:
    """Derive an ordered fallback chain for one element.

    Pass a `scope` when the control is one of many identical siblings - every row of a
    results grid has its own "Select" link, so the row must be pinned by its business
    key before the link can be identified at all.
    """
    candidates: list[Candidate] = []
    name = _clean(element.name)
    label = _clean(element.label_text)
    text = _clean(element.text)
    cid = _clean(element.control_id)

    # 1. Role + accessible name. The most portable identifier there is: it exists in
    #    ARIA and in every desktop accessibility API, and it is what a person reads.
    if name:
        candidates.append(Candidate(
            strategy=Strategy.ROLE_NAME,
            value=f"{element.role.value}:{name}",
            confidence=0.92,
            rationale="role plus accessible name; survives markup changes and has a "
                      "direct equivalent on desktop accessibility APIs",
        ))

    # 2. Caption text. In these apps inputs have no <label for>, so the caption in the
    #    neighbouring cell is the only human-meaningful handle on the field.
    if label and element.role in _INPUT_ROLES:
        candidates.append(Candidate(
            strategy=Strategy.LABEL_TEXT,
            value=label,
            confidence=0.85,
            rationale="caption in the adjacent table cell; this page associates no "
                      "<label for> with its inputs, so this is the field's only "
                      "human-meaningful anchor",
        ))

    # 3. Generated control id. Stable in WebForms while the control tree is stable -
    #    but worthless when it encodes a row index.
    if cid:
        positional = bool(_POSITIONAL_ID.search(cid))
        candidates.append(Candidate(
            strategy=Strategy.CONTROL_ID,
            value=cid,
            confidence=0.35 if positional else 0.7,
            rationale=(
                "generated id embeds a row index, so it identifies a position rather "
                "than a control and breaks when the grid reorders"
                if positional else
                "server-generated id, stable while the control tree is unchanged, but "
                "opaque and not portable off this surface"
            ),
        ))

    # 4. Visible text, for things a person clicks by their words.
    if text and element.role in _CLICKABLE_ROLES and text != name:
        candidates.append(Candidate(
            strategy=Strategy.TEXT,
            value=text,
            confidence=0.6,
            rationale="visible link or button text; readable but easily duplicated "
                      "across rows of a grid",
        ))

    # 5. Ordinal. Always last, always low - recorded so replay has something to fall
    #    back to, and so a fall-through to it is a visible drift signal.
    candidates.append(Candidate(
        strategy=Strategy.ORDINAL,
        value=f"{element.role.value}:{element.ordinal}",
        confidence=0.15,
        rationale="position among controls of the same role in this frame; a last "
                  "resort, and reaching it during replay indicates the surface moved",
    ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return Locator(
        description=description or element.summary(),
        frame_path=list(element.frame_path),
        scope=scope,
        candidates=candidates,
    )


def row_scope(business_key: str, what: str = "record") -> Scope:
    """Scope a locator to the grid row carrying a business key."""
    return Scope(
        contains_text=business_key,
        rationale=f"pins the row by the {what} it displays rather than by its position, "
                  f"so the step survives reordering, paging and differing result counts",
    )


def durability(locator: Locator) -> float:
    """Confidence of the strongest candidate - used to flag fragile recordings."""
    return max((c.confidence for c in locator.candidates), default=0.0)
