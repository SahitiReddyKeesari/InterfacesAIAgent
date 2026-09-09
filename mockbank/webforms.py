"""Dashboard A - "Meridian Core Servicing", an ASP.NET WebForms-style surface.

Reproduces the properties that make this class of app hard to automate:
  * a three-frame shell, so the flow does not live in the top-level document
  * every navigation is a form postback via __doPostBack, not a link with an href
  * __VIEWSTATE must be echoed back; a request without it is an expired session
  * control ids are generated (ctl00$ContentPlaceHolder1$...) and carry row indexes
  * table layout, and no <label for=...> - a field's only clue is the text in the
    adjacent cell, which is exactly why a CSS selector strategy is a dead end here

Flow: Member Inquiry -> Results -> Member Detail -> Open Sub-Account -> Confirmation
"""
from __future__ import annotations

import base64
import hmac
import json
import uuid
from hashlib import sha256
from typing import Any

from flask import Blueprint, render_template, request, url_for

from . import data, faults

bp = Blueprint("webforms", __name__, url_prefix="/meridian")

_SECRET = b"meridian-viewstate-key"
_EV = "wEWBAKD3rSKAgKM54rGBgLSwpmHDAK7q7GGCA=="   # static, like the real thing


def _sign(payload: bytes) -> str:
    return hmac.new(_SECRET, payload, sha256).hexdigest()[:16]


def encode_viewstate(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode() + "." + _sign(raw)


def decode_viewstate(blob: str | None) -> dict[str, Any] | None:
    """Return None when the blob is missing or tampered with - i.e. session expired."""
    if not blob or "." not in blob:
        return None
    b64, sig = blob.rsplit(".", 1)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(raw)):
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _render(template: str, state: dict[str, Any], **ctx: Any):
    return render_template(
        f"webforms/{template}",
        viewstate=encode_viewstate(state),
        eventvalidation=_EV,
        **ctx,
    )


def _search_screen(state, term="", results=None, message=None):
    state = {**state, "screen": "search"}
    return _render("search.html", state, term=term, results=results, message=message)


def _detail_screen(state, m):
    state = {**state, "screen": "detail", "member_id": m.member_id}
    return _render("detail.html", state, m=m)


def _subaccount_screen(state, m, form=None, errors=None):
    state = {**state, "screen": "subaccount", "member_id": m.member_id}
    form = form or {"acct_type": "", "initial": ""}
    return _render("subaccount.html", state, m=m, form=form, errors=errors or [])


# --------------------------------------------------------------------------- shell
@bp.get("/")
def index():
    return render_template("webforms/frameset.html")


@bp.get("/banner.aspx")
def banner():
    return render_template("webforms/banner.html")


@bp.get("/nav.aspx")
def nav():
    return render_template("webforms/nav.html")


@bp.route("/signon.aspx", methods=["GET", "POST"])
def signon():
    if request.method == "POST":
        return _search_screen({}, message="Session re-established.")
    return render_template("webforms/signon.html")


# ----------------------------------------------------------------------- main form
@bp.route("/main.aspx", methods=["GET", "POST"])
def main():
    if request.method == "GET":
        return _search_screen({})

    if faults.fires("server_error"):
        return render_template("webforms/error.html", ref=uuid.uuid4().hex[:12]), 500

    state = decode_viewstate(request.form.get("__VIEWSTATE"))
    if state is None or faults.fires("session_timeout"):
        return render_template("webforms/timeout.html")

    if faults.fires("interstitial"):
        return render_template("webforms/interstitial.html", next_url=url_for("webforms.main"))

    faults.maybe_stall()

    target = request.form.get("__EVENTTARGET", "")
    field = lambda n: request.form.get(f"ctl00$ContentPlaceHolder1${n}", "")

    # --- inquiry -----------------------------------------------------------
    if target.endswith("btnSearch"):
        term = field("txtMemberId")
        if not term.strip():
            return _search_screen(state, term, message="Enter a member number or name.")
        hits = [] if faults.fires("not_found") else data.search(term)
        if not hits:
            return _search_screen(state, term, results=[],
                                  message="No member records match the criteria entered.")
        return _search_screen(state, term, results=hits)

    if target.endswith("btnClear"):
        return _search_screen(state)

    # --- grid row select ---------------------------------------------------
    if "gvResults" in target and target.endswith("lnkSelect"):
        row = int(target.rsplit("$ctl", 1)[1].split("$")[0]) - 2
        hits = data.search(field("txtMemberId"))
        if not (0 <= row < len(hits)):
            return _search_screen(state, field("txtMemberId"), results=hits,
                                  message="Selected row is no longer available.")
        return _detail_screen(state, hits[row])

    member = data.get(state.get("member_id", ""))

    if target.endswith("btnOpenSub"):
        if member is None:
            return _search_screen(state, message="Member context lost.")
        if member.status == "Restricted":
            return _detail_screen(state, member)
        return _subaccount_screen(state, member)

    # --- sub-account submit ------------------------------------------------
    if target.endswith("btnSubmit"):
        if member is None:
            return _search_screen(state, message="Member context lost.")
        form = {"acct_type": field("ddlAcctType"), "initial": field("txtInitial")}
        errors: list[str] = []
        if faults.fires("validation_error"):
            errors.append("Initial deposit is below the product minimum (PRD-1180).")
        if not form["acct_type"]:
            errors.append("Account Type is required.")
        try:
            amount = float(form["initial"] or "")
            if amount < 0:
                errors.append("Initial Deposit may not be negative.")
        except ValueError:
            errors.append("Initial Deposit must be a numeric amount.")
            amount = 0.0
        if errors:
            return _subaccount_screen(state, member, form, errors)
        acct = data.open_subaccount(member.member_id, form["acct_type"], amount)
        return _render("confirm.html", {**state, "screen": "confirm"}, acct=acct)

    if target.endswith("btnCancel") and member is not None:
        return _detail_screen(state, member)

    return _search_screen(state)
