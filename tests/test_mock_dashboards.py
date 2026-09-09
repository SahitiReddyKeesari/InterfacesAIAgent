"""Flow and fault-taxonomy tests for the two mock dashboards.

Black-box HTTP tests against the running Java server. They pin the behaviour the replay
engine will be written against: the happy path reaches a checkpoint, and each injected
fault produces its own distinguishable surface response rather than a generic failure.
"""
from __future__ import annotations

import re

import pytest

P = "ctl00$ContentPlaceHolder1$"
TOKEN_FIELD = "org.apache.struts.taglib.html.TOKEN"


def viewstate(html: str) -> str:
    m = re.search(r'name="__VIEWSTATE" id="__VIEWSTATE" value="([^"]*)"', html)
    assert m, "no __VIEWSTATE on page"
    return m.group(1)


def token(html: str) -> str:
    m = re.search(rf'name="{re.escape(TOKEN_FIELD)}" value="([^"]*)"', html)
    assert m, "no struts token on page"
    return m.group(1)


# --------------------------------------------------------------- Dashboard A
def meridian_to_subaccount(srv, member_id="12345") -> str:
    """Walk Meridian as far as the sub-account form; return that page's HTML."""
    h = srv.get("/meridian/main.aspx")
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "btnSearch", **{P + "txtMemberId": member_id})
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "gvResults$ctl02$lnkSelect",
                 **{P + "txtMemberId": member_id})
    assert "Member Detail" in h
    return srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                    __EVENTTARGET=P + "btnOpenSub")


def test_meridian_happy_path_reaches_confirmation(srv):
    h = meridian_to_subaccount(srv)
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "btnSubmit",
                 **{P + "ddlAcctType": "Savings", P + "txtInitial": "250.00"})
    assert "opened successfully" in h
    assert re.search(r'lblNewAcct">(\d{10})<', h)


def test_meridian_never_renders_unmasked_ssn(srv):
    """The detail screen shows a tax id - it must be the masked form, never the raw one."""
    h = srv.get("/meridian/main.aspx")
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "btnSearch", **{P + "txtMemberId": "12345"})
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "gvResults$ctl02$lnkSelect",
                 **{P + "txtMemberId": "12345"})
    assert "Member Detail" in h
    assert "XXX-XX-9031" in h
    assert "412-88-9031" not in h


def test_meridian_missing_viewstate_is_expired_session(srv):
    assert "Session Expired" in srv.html("/meridian/main.aspx",
                                         __EVENTTARGET=P + "btnSearch")


@pytest.mark.parametrize("fault,marker,status", [
    ("not_found", "No member records match", 200),
    ("session_timeout", "Session Expired", 200),
    ("interstitial", "Acknowledge", 200),
    ("server_error", "Runtime Error", 500),
])
def test_meridian_faults_are_distinguishable(srv, fault, marker, status):
    h = srv.get("/meridian/main.aspx")
    srv.arm(fault)
    code, body = srv.post("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                          __EVENTTARGET=P + "btnSearch",
                          **{P + "txtMemberId": "12345"})
    assert code == status
    assert marker in body


def test_meridian_validation_errors_are_reported_per_field(srv):
    h = meridian_to_subaccount(srv, "12346")
    h = srv.html("/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                 __EVENTTARGET=P + "btnSubmit",
                 **{P + "ddlAcctType": "", P + "txtInitial": "abc"})
    assert "Account Type is required" in h
    assert "numeric amount" in h


# --------------------------------------------------------------- Dashboard B
def test_summit_happy_path_reaches_posted(srv):
    h = srv.get("/summit/memberSearch.do")
    h = srv.html("/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
                 customerNbr="12345", submitAction="Retrieve")
    assert "Retrieved Customers" in h
    h = srv.html("/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
                 customerNbr="12345", selectCustomer="Open", selectedIdx="0")
    assert "Customer Record" in h
    h = srv.html("/summit/customerDetail.do", **{TOKEN_FIELD: token(h)},
                 customerNbr="12345", submitAction="Add Related Account")
    assert "Add Related Account" in h
    h = srv.html("/summit/relatedAccount.do", **{TOKEN_FIELD: token(h)},
                 customerNbr="12345", productCd="Checking", openingAmt="75.50",
                 submitAction="Post")
    assert "Related account established" in h


def test_summit_stale_token_terminates_session(srv):
    h = srv.html("/summit/memberSearch.do", **{TOKEN_FIELD: "bogus"},
                 customerNbr="12345", submitAction="Retrieve")
    assert "Session Terminated" in h


def test_summit_not_found_is_a_business_outcome(srv):
    h = srv.get("/summit/memberSearch.do")
    srv.arm("not_found")
    h = srv.html("/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
                 customerNbr="12345", submitAction="Retrieve")
    assert "No customer records retrieved" in h


# ------------------------------------------------------------ heterogeneity
def test_dashboards_diverge_in_wording_and_navigation(srv):
    a = srv.get("/meridian/main.aspx")
    b = srv.get("/summit/memberSearch.do")
    assert "Member / Name" in a and "Customer Nbr" in b
    assert "__doPostBack" in a and "memberSearch.do" in b
    assert "data-testid" not in a and "data-testid" not in b


def test_fault_names_all_carry_a_taxonomy_class(srv):
    assert set(srv.state()["taxonomy"].values()) == {"business", "recover", "hard"}


# ------------------------------------------------------------- card services
CARDS = "/meridian/cards.aspx"


def card_rows(html: str) -> list[tuple[str, str, str]]:
    """(masked number, type, status) per row of the card grid."""
    return re.findall(
        r"<td>(\*\*\*\* \*\*\*\* \*\*\*\* \d{4})</td><td>(\w+)</td><td>[\d/]+</td><td>(\w+)</td>",
        html)


def card_message(html: str) -> str:
    m = re.search(r'lblCardMsg">(?:<b>)?([^<]*)', html)
    return m.group(1).strip() if m else ""


def cards_for(srv, member_id: str) -> str:
    h = srv.get(CARDS)
    return srv.html(CARDS, __VIEWSTATE=viewstate(h),
                    __EVENTTARGET=P + "btnCardSearch",
                    **{P + "txtCardMember": member_id})


def card_action(srv, html: str, ctl: str, action: str, member_id: str = "12345") -> str:
    return srv.html(CARDS, __VIEWSTATE=viewstate(html),
                    __EVENTTARGET=f"{P}gvCards${ctl}$lnk{action}",
                    **{P + "txtCardMember": member_id})


def test_cards_never_render_a_full_pan(srv):
    h = cards_for(srv, "12345")
    assert "4539881022444412" not in h
    assert "**** **** **** 4412" in h


def test_activate_moves_inactive_card_to_active(srv):
    h = card_action(srv, cards_for(srv, "12345"), "ctl03", "Activate")
    assert "activated" in card_message(h)
    assert card_rows(h)[1][2] == "Active"


def test_activating_an_active_card_is_a_business_outcome(srv):
    h = card_action(srv, cards_for(srv, "12345"), "ctl03", "Activate")
    h = card_action(srv, h, "ctl03", "Activate")
    assert "CRD-2201" in card_message(h)


def test_lock_and_unlock_are_reversible(srv):
    h = card_action(srv, cards_for(srv, "12345"), "ctl02", "Lock")
    assert card_rows(h)[0][2] == "Locked"
    h = card_action(srv, h, "ctl02", "Unlock")
    assert card_rows(h)[0][2] == "Active"


def test_block_requires_confirmation_before_taking_effect(srv):
    """The irreversible action must not fire straight off the grid link."""
    h = card_action(srv, cards_for(srv, "12345"), "ctl02", "Block")
    assert "Confirm Permanent Block" in h
    assert "cannot be reversed" in h
    assert "Blocked" not in "".join(r[2] for r in card_rows(h))


def test_cancelling_a_block_changes_nothing(srv):
    h = card_action(srv, cards_for(srv, "12345"), "ctl02", "Block")
    h = srv.html(CARDS, __VIEWSTATE=viewstate(h), __EVENTTARGET=P + "btnCancelBlock",
                 **{P + "txtCardMember": "12345"})
    assert card_rows(h)[0][2] == "Active"
    assert "cancelled" in card_message(h)


def test_confirmed_block_is_terminal(srv):
    h = card_action(srv, cards_for(srv, "12345"), "ctl02", "Block")
    h = srv.html(CARDS, __VIEWSTATE=viewstate(h), __EVENTTARGET=P + "btnConfirmBlock",
                 **{P + "txtCardMember": "12345"})
    assert card_rows(h)[0][2] == "Blocked"
    assert "permanently blocked" in card_message(h)
    # A blocked card offers no further actions in the UI.
    assert "&mdash;" in h
    # And forging the postback anyway still gets a business outcome, not a crash.
    h = card_action(srv, h, "ctl02", "Block")
    h = srv.html(CARDS, __VIEWSTATE=viewstate(h), __EVENTTARGET=P + "btnConfirmBlock",
                 **{P + "txtCardMember": "12345"})
    assert "CRD-2210" in card_message(h)


def test_restricted_member_card_action_is_permission_denied(srv):
    h = card_action(srv, cards_for(srv, "12347"), "ctl02", "Lock", member_id="12347")
    assert "SEC-0917" in card_message(h)
