"""Tests for the perceive/act seam.

These pin the two properties the rest of the system depends on: that a control can be
identified without any test id or <label for>, and that the same code drives two
different legacy dialects without knowing which one it is looking at.
"""
from __future__ import annotations

import time

import pytest

from cua.surfaces import locators
from cua.surfaces.models import Click, Element, Fill, Navigate, Role, Strategy


def open_at(surface, base_url, path):
    surface.act(Navigate(url=base_url + path))
    return surface.observe()


def find_input_by_caption(obs, caption: str):
    return next(e for e in obs.elements
                if e.role is Role.TEXTBOX and caption in (e.label_text or ""))


# ------------------------------------------------------------------- perception
def test_observation_reaches_into_frameset_children(surface, base_url):
    obs = open_at(surface, base_url, "/meridian/")
    assert {"banner", "nav", "main"} <= set(obs.frames)
    # the flow's controls live in an inner frame, not the top document
    assert any(e.frame_path == ["main"] for e in obs.elements)


def test_observation_reaches_into_iframe_workspace(surface, base_url):
    obs = open_at(surface, base_url, "/summit/")
    assert "workspace" in obs.frames
    assert any(e.frame_path == ["workspace"] for e in obs.elements)


def test_key_field_has_no_accessible_name_only_a_caption(surface, base_url):
    """The premise of the whole locator strategy: these inputs are anonymous."""
    obs = open_at(surface, base_url, "/meridian/")
    field = find_input_by_caption(obs, "Member / Name")
    assert field.name == ""            # no aria-label, no <label for>, no title
    assert field.label_text == "Member / Name:"


def test_no_dashboard_exposes_a_test_id(surface, base_url):
    for path in ("/meridian/", "/summit/"):
        obs = open_at(surface, base_url, path)
        assert "data-testid" not in obs.text_digest


# ---------------------------------------------------------------------- locators
def test_caption_strategy_wins_for_an_unlabelled_field(surface, base_url):
    obs = open_at(surface, base_url, "/meridian/")
    loc = locators.build(find_input_by_caption(obs, "Member / Name"))
    assert loc.candidates[0].strategy is Strategy.LABEL_TEXT
    res = surface.resolve(loc)
    assert res.resolved and res.strategy is Strategy.LABEL_TEXT


def test_row_indexed_ids_are_ranked_below_ordinals_never_first():
    """A generated id carrying a row number identifies a position, not a control."""
    e = Element(ref="r", role=Role.LINK, name="Select", text="Select",
                control_id="ctl00$ContentPlaceHolder1$gvResults$ctl02$lnkSelect")
    loc = locators.build(e)
    by = {c.strategy: c.confidence for c in loc.candidates}
    assert by[Strategy.CONTROL_ID] < by[Strategy.ROLE_NAME]
    assert loc.candidates[0].strategy is not Strategy.CONTROL_ID


def test_ordinal_is_always_present_and_always_last():
    e = Element(ref="r", role=Role.TEXTBOX, label_text="Member / Name:")
    loc = locators.build(e)
    assert loc.candidates[-1].strategy is Strategy.ORDINAL


def test_row_scope_pins_a_grid_row_by_its_business_key(surface, base_url):
    obs = open_at(surface, base_url, "/meridian/")
    surface.act(Fill(locator=locators.build(find_input_by_caption(obs, "Member / Name")),
                     text="a"))     # matches three members
    search = next(e for e in obs.elements if e.role is Role.LINK and e.name == "Search")
    surface.act(Click(locator=locators.build(search)))
    obs2 = surface.observe()

    sel = next(e for e in obs2.elements if e.role is Role.LINK and e.name == "Select")

    # Unscoped, every row has a "Select" link, so role+name is ambiguous and the chain
    # falls through to the row-indexed id. That *resolves* - to exactly one control -
    # but it identifies a position, not a member. Resolving is not the same as being
    # right, which is why the winning confidence is reported alongside it.
    unscoped = surface.resolve(locators.build(sel))
    assert unscoped.resolved
    assert unscoped.strategy is Strategy.CONTROL_ID
    assert Strategy.ROLE_NAME in unscoped.fell_through
    assert unscoped.weak, "a positional match must be reported as low confidence"

    # Scoped to the row carrying the member number, the same link is identified by what
    # the row *is* rather than where it sits.
    scoped = locators.build(sel, scope=locators.row_scope("12347", "member number"))
    assert surface.resolve(scoped).resolved

    surface.act(Click(locator=scoped))
    assert "Member Detail — 12347" in surface.observe().text_digest


def test_resolution_reports_which_strategy_carried_the_step(surface, base_url):
    obs = open_at(surface, base_url, "/meridian/")
    res = surface.resolve(locators.build(find_input_by_caption(obs, "Member / Name")))
    assert res.strategy is not None and res.matches == 1


# ------------------------------------------------------------------- determinism
def test_slow_backend_does_not_produce_a_stale_read(surface, base_url, srv):
    """A pending postback is invisible to any content check - the wait must not
    return until the request comes back."""
    obs = open_at(surface, base_url, "/meridian/")
    surface.act(Fill(locator=locators.build(find_input_by_caption(obs, "Member / Name")),
                     text="12345"))
    search = next(e for e in obs.elements if e.role is Role.LINK and e.name == "Search")
    srv.arm("slow_load")
    started = time.time()
    surface.act(Click(locator=locators.build(search)))
    after = surface.observe()
    assert "Search Results" in after.text_digest       # not the stale page
    assert time.time() - started >= 5.0                # actually waited it out


def test_wait_is_adaptive_not_padded(surface, base_url):
    """With no stall the same step must be fast - a fixed worst-case sleep would not."""
    obs = open_at(surface, base_url, "/meridian/")
    surface.act(Fill(locator=locators.build(find_input_by_caption(obs, "Member / Name")),
                     text="12345"))
    search = next(e for e in obs.elements if e.role is Role.LINK and e.name == "Search")
    started = time.time()
    surface.act(Click(locator=locators.build(search)))
    assert "Search Results" in surface.observe().text_digest
    assert time.time() - started < 4.0


# ----------------------------------------------------------------- portability
@pytest.mark.parametrize("path,caption,button,role,expect", [
    ("/meridian/", "Member / Name", "Search", Role.LINK, "Search Results"),
    ("/summit/", "Customer Nbr", "Retrieve", Role.BUTTON, "Retrieved Customers"),
])
def test_identical_code_drives_both_dialects(surface, base_url, path, caption,
                                             button, role, expect):
    """No branch on which dashboard this is: one dialect uses __doPostBack links in a
    frameset, the other submit buttons in an iframe, and the seam hides the difference."""
    obs = open_at(surface, base_url, path)
    surface.act(Fill(locator=locators.build(find_input_by_caption(obs, caption)),
                     text="12345"))
    btn = next(e for e in obs.elements if e.role is role and e.name == button)
    assert surface.act(Click(locator=locators.build(btn))).ok
    assert expect in surface.observe().text_digest


# ------------------------------------------------- framework-agnostic locators
@pytest.mark.parametrize("ids,label", [
    (["a$ctl02$lnk", "a$ctl03$lnk", "a$ctl04$lnk"], "ASP.NET WebForms"),
    (["row_3_select", "row_4_select"], "Struts-style"),
    (["form:tbl:0:btn", "form:tbl:1:btn"], "JSF-style"),
    (["grid[7]open", "grid[8]open"], "bracket-indexed"),
])
def test_positional_ids_are_detected_regardless_of_framework(ids, label):
    """Detection is structural - peers sharing an id shape mean the digits are an
    index - so it is not tied to one vendor's naming convention."""
    peers = [Element(ref=f"r{i}", role=Role.LINK, name="Open", control_id=c)
             for i, c in enumerate(ids)]
    loc = locators.build(peers[0], peers=peers)
    control_id = next(c for c in loc.candidates if c.strategy is Strategy.CONTROL_ID)
    assert control_id.confidence < 0.5, f"{label} index not detected"
    assert loc.candidates[0].strategy is not Strategy.CONTROL_ID


def test_genuinely_named_ids_keep_their_confidence():
    """The detector must not cry wolf on ids that merely happen to be generated."""
    peers = [Element(ref="a", role=Role.TEXTBOX, control_id="customerNbr"),
             Element(ref="b", role=Role.TEXTBOX, control_id="openingAmt")]
    loc = locators.build(peers[0], peers=peers)
    control_id = next(c for c in loc.candidates if c.strategy is Strategy.CONTROL_ID)
    assert control_id.confidence == 0.7


def test_peers_can_clear_an_id_that_merely_looks_indexed():
    """A lone id containing digits is suspected; peers that disagree exonerate it."""
    suspect = Element(ref="a", role=Role.TEXTBOX, control_id="address1")
    alone = next(c for c in locators.build(suspect).candidates
                 if c.strategy is Strategy.CONTROL_ID)
    with_peers = next(c for c in locators.build(
        suspect, peers=[suspect, Element(ref="b", role=Role.TEXTBOX,
                                         control_id="postcode")]).candidates
        if c.strategy is Strategy.CONTROL_ID)
    assert alone.confidence < with_peers.confidence


def test_row_scoping_works_without_a_table(surface, tmp_path):
    """Scoping must not assume <tr>: a div or list based surface has records too."""
    page = tmp_path / "list.html"
    page.write_text("""
      <ul>
        <li><span>PX-9931</span><span>Adrian Vela</span><button type=button>Open</button></li>
        <li><span>PX-9932</span><span>Rosa Imani</span><button type=button>Open</button></li>
      </ul>""")
    surface.act(Navigate(url=page.as_uri()))
    obs = surface.observe()
    opn = next(e for e in obs.elements if e.role is Role.BUTTON and e.name == "Open")

    for key, expected in [("PX-9931", "Adrian Vela"), ("PX-9932", "Rosa Imani")]:
        loc = locators.build(opn, scope=locators.row_scope(key), peers=obs.elements)
        assert surface.resolve(loc).resolved
        row = surface._scoped(surface._frame_for(loc.frame_path), loc)
        assert expected in row.inner_text()


def test_a_scope_matching_several_records_refuses_rather_than_picking_one(
        surface, base_url, srv):
    """Member 12346 holds two savings accounts. "the Savings row" names neither of
    them, and answering with whichever is listed first gives a caller one balance with
    no hint the other exists - confident, and possibly wrong."""
    open_at(surface, base_url, "/meridian/")
    obs = surface.observe()
    surface.act(Fill(locator=locators.build(find_input_by_caption(obs, "Member / Name")),
                     text="12346"))
    search = next(e for e in obs.elements if e.role is Role.LINK and e.name == "Search")
    surface.act(Click(locator=locators.build(search)))
    obs2 = surface.observe()
    sel = next(e for e in obs2.elements if e.role is Role.LINK and e.name == "Select")
    surface.act(Click(locator=locators.build(sel, scope=locators.row_scope("12346"))))
    obs3 = surface.observe()

    cell = next(e for e in obs3.elements
                if e.role is Role.CELL and e.column_header == "Current Balance")
    ambiguous = locators.build(cell, "balance", scope=locators.row_scope("Savings"),
                               peers=obs3.elements)
    result = surface.resolve(ambiguous)
    assert not result.resolved
    assert "2 records match" in result.detail
    assert "guess" in result.detail

    # A product this member holds exactly one of still resolves.
    single = locators.build(cell, "balance", scope=locators.row_scope("0001234602"),
                            peers=obs3.elements)
    assert surface.resolve(single).resolved
