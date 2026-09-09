"""Flow and fault-taxonomy tests for the two mock dashboards.

These pin the behaviour the replay engine will be written against: the happy path
reaches a checkpoint, and each injected fault produces its own distinguishable
surface response rather than a generic failure.
"""
from __future__ import annotations

import re

import pytest

from mockbank import data
from mockbank.app import create_app

P = "ctl00$ContentPlaceHolder1$"
TOKEN_FIELD = "org.apache.struts.taglib.html.TOKEN"


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c.post("/__control/reset")
        yield c
        c.post("/__control/reset")


def viewstate(html: str) -> str:
    m = re.search(r'name="__VIEWSTATE" id="__VIEWSTATE" value="([^"]*)"', html)
    assert m, "no __VIEWSTATE on page"
    return m.group(1)


def token(html: str) -> str:
    m = re.search(rf'name="{re.escape(TOKEN_FIELD)}" value="([^"]*)"', html)
    assert m, "no struts token on page"
    return m.group(1)


def post(client, url, **fields) -> str:
    return client.post(url, data=fields).get_data(as_text=True)


def arm(client, name: str, count: int = 1):
    r = client.post("/__control/fault", json={"name": name, "count": count})
    assert r.status_code == 200


# --------------------------------------------------------------- Dashboard A
def meridian_to_subaccount(client, member_id="12345") -> str:
    """Walk Meridian as far as the sub-account form; return that page's HTML."""
    h = client.get("/meridian/main.aspx").get_data(as_text=True)
    h = post(client, "/meridian/main.aspx", __VIEWSTATE=viewstate(h),
             __EVENTTARGET=P + "btnSearch", **{P + "txtMemberId": member_id})
    h = post(client, "/meridian/main.aspx", __VIEWSTATE=viewstate(h),
             __EVENTTARGET=P + "gvResults$ctl02$lnkSelect", **{P + "txtMemberId": member_id})
    assert "Member Detail" in h
    return post(client, "/meridian/main.aspx", __VIEWSTATE=viewstate(h),
                __EVENTTARGET=P + "btnOpenSub")


def test_meridian_happy_path_reaches_confirmation(client):
    h = meridian_to_subaccount(client)
    h = post(client, "/meridian/main.aspx", __VIEWSTATE=viewstate(h),
             __EVENTTARGET=P + "btnSubmit",
             **{P + "ddlAcctType": "Savings", P + "txtInitial": "250.00"})
    assert "opened successfully" in h
    assert re.search(r'lblNewAcct">(\d{10})<', h)


def test_meridian_never_renders_unmasked_ssn(client):
    h = meridian_to_subaccount(client)
    assert data.get("12345").ssn not in h


def test_meridian_missing_viewstate_is_expired_session(client):
    assert "Session Expired" in post(client, "/meridian/main.aspx",
                                     __EVENTTARGET=P + "btnSearch")


@pytest.mark.parametrize("fault,marker,status", [
    ("not_found", "No member records match", 200),
    ("session_timeout", "Session Expired", 200),
    ("interstitial", "Acknowledge", 200),
    ("server_error", "Runtime Error", 500),
])
def test_meridian_faults_are_distinguishable(client, fault, marker, status):
    h = client.get("/meridian/main.aspx").get_data(as_text=True)
    arm(client, fault)
    r = client.post("/meridian/main.aspx", data={
        "__VIEWSTATE": viewstate(h), "__EVENTTARGET": P + "btnSearch",
        P + "txtMemberId": "12345"})
    assert r.status_code == status
    assert marker in r.get_data(as_text=True)


def test_meridian_validation_errors_are_reported_per_field(client):
    h = meridian_to_subaccount(client, "12346")
    h = post(client, "/meridian/main.aspx", __VIEWSTATE=viewstate(h),
             __EVENTTARGET=P + "btnSubmit",
             **{P + "ddlAcctType": "", P + "txtInitial": "abc"})
    assert "Account Type is required" in h
    assert "numeric amount" in h


# --------------------------------------------------------------- Dashboard B
def test_summit_happy_path_reaches_posted(client):
    h = client.get("/summit/memberSearch.do").get_data(as_text=True)
    h = post(client, "/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
             customerNbr="12345", submitAction="Retrieve")
    assert "Retrieved Customers" in h
    h = post(client, "/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
             customerNbr="12345", selectCustomer="Open", selectedIdx="0")
    assert "Customer Record" in h
    h = post(client, "/summit/customerDetail.do", **{TOKEN_FIELD: token(h)},
             customerNbr="12345", submitAction="Add Related Account")
    assert "Add Related Account" in h
    h = post(client, "/summit/relatedAccount.do", **{TOKEN_FIELD: token(h)},
             customerNbr="12345", productCd="Checking", openingAmt="75.50",
             submitAction="Post")
    assert "Related account established" in h


def test_summit_stale_token_terminates_session(client):
    h = post(client, "/summit/memberSearch.do", **{TOKEN_FIELD: "bogus"},
             customerNbr="12345", submitAction="Retrieve")
    assert "Session Terminated" in h


def test_summit_not_found_is_a_business_outcome(client):
    h = client.get("/summit/memberSearch.do").get_data(as_text=True)
    arm(client, "not_found")
    h = post(client, "/summit/memberSearch.do", **{TOKEN_FIELD: token(h)},
             customerNbr="12345", submitAction="Retrieve")
    assert "No customer records retrieved" in h


# ------------------------------------------------------------ heterogeneity
def test_dashboards_diverge_in_wording_and_navigation(client):
    a = client.get("/meridian/main.aspx").get_data(as_text=True)
    b = client.get("/summit/memberSearch.do").get_data(as_text=True)
    assert "Member / Name" in a and "Customer Nbr" in b
    assert "__doPostBack" in a and "memberSearch.do" in b
    assert "data-testid" not in a and "data-testid" not in b


def test_fault_names_all_carry_a_taxonomy_class(client):
    body = client.get("/__control/state").get_json()
    assert set(body["taxonomy"].values()) == {"business", "recover", "hard"}
