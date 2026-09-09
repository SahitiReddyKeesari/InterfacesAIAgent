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

# Generated ids are split on their separators and each segment judged on its own.
# A segment that is all digits, or letters with trailing digits, is an index:
# `ctl02`, `row_3`, `tbl:0:btn`, `item[2]`. Framework agnostic on purpose - an earlier
# version matched only one vendor's convention, so any other framework's numbering was
# silently trusted.
_ID_SEPARATORS = re.compile(r"[_$\-\[\]:.]+")
_INDEX_SEGMENT = re.compile(r"^(?:\d{1,4}|[A-Za-z]+\d{1,4})$")

# Digits collapsed to a marker, so two ids that differ only by an index compare equal.
_DIGITS = re.compile(r"\d+")

_INPUT_ROLES = {Role.TEXTBOX, Role.COMBOBOX, Role.CHECKBOX, Role.RADIO}
_READABLE_ROLES = {Role.TEXT, Role.CELL}
_CLICKABLE_ROLES = {Role.BUTTON, Role.LINK}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _id_shape(control_id: str) -> str:
    """An id with every digit run collapsed, so `gv_ctl02_link` and `gv_ctl03_link`
    share a shape."""
    return _DIGITS.sub("#", control_id)


def is_positional_id(element: Element, peers: list[Element] | None = None) -> tuple[bool, str]:
    """Decide whether an id names a control or merely a slot.

    Two signals, strongest first:

    1. Structural, and authoritative when peers are available. If another control of
       the same role in the same frame has an id of the same shape - identical once
       digit runs are collapsed - then the digits are an index. This needs no knowledge
       of the framework that generated them, which is the point: it catches ASP.NET's
       `ctl02`, Struts' `row_3` and anything else that numbers repeated controls.

    2. Shape alone, used when no peers were supplied. Weaker, because an id may
       legitimately contain a number, so it is only a suspicion.
    """
    cid = _clean(element.control_id)
    if not cid:
        return False, ""
    comparable = [p for p in (peers or [])
                  if p is not element
                  and p.role is element.role
                  and p.frame_path == element.frame_path
                  and p.control_id]
    if comparable:
        # Authoritative: with siblings to compare against, their agreement or
        # disagreement settles it, and the weaker shape heuristic is not consulted.
        shape = _id_shape(cid)
        twins = [p for p in comparable
                 if _id_shape(_clean(p.control_id)) == shape
                 and _clean(p.control_id) != cid]
        if twins:
            return True, (f"another {len(twins) + 1} controls of this role share the id "
                          f"shape {shape!r}, so the digits index a position rather than "
                          f"name a control")
        return False, ""
    if any(_INDEX_SEGMENT.match(seg) for seg in _ID_SEPARATORS.split(cid) if seg):
        return True, ("a segment of the id is an index; treated as positional until "
                      "peers prove otherwise, which errs towards distrusting an id "
                      "rather than towards selecting the wrong record")
    return False, ""


def build(element: Element, description: str | None = None,
          scope: Scope | None = None,
          peers: list[Element] | None = None) -> Locator:
    """Derive an ordered fallback chain for one element.

    Pass a `scope` when the control is one of many identical siblings - every row of a
    results grid has its own "Select" link, so the row must be pinned by its business
    key before the link can be identified at all.

    Pass `peers` - the other elements of the same observation - to let generated ids be
    judged structurally rather than by pattern-matching one framework's conventions.
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
    header = _clean(element.column_header)
    # For a grid data cell the "preceding cell" is the neighbouring column's *value*,
    # not a caption - anchoring to it would justify a step with unrelated data. A cell
    # that knows its column heading is named by that instead.
    caption_is_meaningful = not (element.role is Role.CELL and header)
    if label and caption_is_meaningful and element.role in (_INPUT_ROLES | _READABLE_ROLES):
        anchored = ("field" if element.role in _INPUT_ROLES else "displayed value")
        candidates.append(Candidate(
            strategy=Strategy.LABEL_TEXT,
            value=label,
            confidence=0.85,
            rationale=f"caption in the adjacent table cell; this page associates no "
                      f"<label for> with its controls, so this is the {anchored}'s only "
                      f"human-meaningful anchor",
        ))

    # 2b. A grid cell is named by its column, not its position. Pairing this with a
    #     row scope gives "the Current Balance cell of the Savings row", which survives
    #     both reordered rows and reordered columns.
    if header and element.role is Role.CELL:
        candidates.append(Candidate(
            strategy=Strategy.COLUMN_CELL,
            value=header,
            confidence=0.88,
            rationale="cell identified by its column heading; combined with a row scope "
                      "this names the value rather than its coordinates, and column "
                      "headings exist on desktop grids too",
        ))

    # 3. Generated control id. Stable in WebForms while the control tree is stable -
    #    but worthless when it encodes a row index.
    if cid:
        positional, why = is_positional_id(element, peers)
        candidates.append(Candidate(
            strategy=Strategy.CONTROL_ID,
            value=cid,
            confidence=0.35 if positional else 0.7,
            rationale=(
                f"identifies a position, not a control: {why}; it will select the wrong "
                f"record as soon as the list reorders"
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
        role=element.role,
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
