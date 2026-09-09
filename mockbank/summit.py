"""Dashboard B - "Summit Servicing", a Java/Struts-style surface.

A deliberately different legacy dialect from Meridian, so the surface abstraction has
to earn its keep rather than being fitted to one app:
  * real form posts to *.do actions, not __doPostBack
  * a Struts synchronizer token that must round-trip, else the session is terminated
  * an iframe workspace instead of a frameset
  * nested tables, <font> tags, spacer gifs, inset borders

It also renames every business concept while keeping the same underlying flow -
member becomes "customer", sub-account becomes "related account", branch becomes
"servicing office", Submit becomes "Post". Any locator strategy that memorised
Meridian's wording breaks here, which is the point: it stands in for two institutions
running differently configured software.
"""
from __future__ import annotations

import base64
import hmac
import uuid
from hashlib import sha256

from flask import Blueprint, Response, render_template, request, url_for

from . import data, faults

bp = Blueprint("summit", __name__, url_prefix="/summit")

_SECRET = b"summit-struts-token-key"
_SPACER = base64.b64decode(
    b"R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
)


def _token() -> str:
    return hmac.new(_SECRET, b"session", sha256).hexdigest()


def _valid(tok: str | None) -> bool:
    return bool(tok) and hmac.compare_digest(tok, _token())


def _guard(next_url: str):
    """Shared fault gate. Returns a Response to short-circuit with, or None."""
    if faults.fires("server_error"):
        return Response(
            render_template("summit/error.html", ref=uuid.uuid4().hex[:12]), status=500
        )
    if faults.fires("session_timeout"):
        return Response(render_template("summit/timeout.html"))
    if faults.fires("interstitial"):
        return Response(render_template("summit/interstitial.html", next_url=next_url))
    faults.maybe_stall()
    return None


@bp.get("/spacer.gif")
def spacer():
    return Response(_SPACER, mimetype="image/gif")


@bp.get("/")
def index():
    return render_template("summit/shell.html")


@bp.get("/memberSearch.do")
def search():
    return render_template("summit/search.html", token=_token(), term="", results=None,
                           message=None)


@bp.post("/memberSearch.do")
def search_submit():
    blocked = _guard(url_for("summit.search"))
    if blocked:
        return blocked
    if not _valid(request.form.get("org.apache.struts.taglib.html.TOKEN")):
        return render_template("summit/timeout.html")

    page = lambda **kw: render_template("summit/search.html", token=_token(), **kw)
    term = request.form.get("customerNbr", "")

    if request.form.get("submitAction") == "Reset":
        return page(term="", results=None, message=None)

    # Row selection posts the same form with selectedIdx populated.
    idx = request.form.get("selectedIdx", "")
    if request.form.get("selectCustomer") and idx.isdigit():
        hits = data.search(term)
        i = int(idx)
        if 0 <= i < len(hits):
            return render_template("summit/detail.html", token=_token(), m=hits[i])
        return page(term=term, results=hits, message="Selected row is stale. Retrieve again.")

    if not term.strip():
        return page(term=term, results=None, message="Customer Nbr or Surname is required.")

    hits = [] if faults.fires("not_found") else data.search(term)
    if not hits:
        return page(term=term, results=[], message="No customer records retrieved (SUM-0110).")
    return page(term=term, results=hits, message=None)


@bp.post("/customerDetail.do")
def detail_submit():
    blocked = _guard(url_for("summit.search"))
    if blocked:
        return blocked
    if not _valid(request.form.get("org.apache.struts.taglib.html.TOKEN")):
        return render_template("summit/timeout.html")

    m = data.get(request.form.get("customerNbr", ""))
    if m is None:
        return render_template("summit/search.html", token=_token(), term="", results=[],
                               message="Customer context lost (SUM-0114).")
    if request.form.get("submitAction") == "Return":
        return render_template("summit/search.html", token=_token(), term="", results=None,
                               message=None)
    return render_template("summit/addaccount.html", token=_token(), m=m,
                           form={"acct_type": "", "initial": ""}, errors=[])


@bp.post("/relatedAccount.do")
def add_submit():
    blocked = _guard(url_for("summit.search"))
    if blocked:
        return blocked
    if not _valid(request.form.get("org.apache.struts.taglib.html.TOKEN")):
        return render_template("summit/timeout.html")

    m = data.get(request.form.get("customerNbr", ""))
    if m is None:
        return render_template("summit/search.html", token=_token(), term="", results=[],
                               message="Customer context lost (SUM-0114).")
    if request.form.get("submitAction") == "Abandon":
        return render_template("summit/detail.html", token=_token(), m=m)

    form = {"acct_type": request.form.get("productCd", ""),
            "initial": request.form.get("openingAmt", "")}
    errors: list[str] = []
    if faults.fires("validation_error"):
        errors.append("Opening amount under product minimum (SUM-2204).")
    if not form["acct_type"]:
        errors.append("Product Code must be supplied.")
    try:
        amount = float(form["initial"] or "")
        if amount < 0:
            errors.append("Opening amount may not be negative.")
    except ValueError:
        errors.append("Opening amount is not a valid figure.")
        amount = 0.0
    if errors:
        return render_template("summit/addaccount.html", token=_token(), m=m,
                               form=form, errors=errors)

    acct = data.open_subaccount(m.member_id, form["acct_type"], amount)
    return render_template("summit/posted.html", acct=acct)
