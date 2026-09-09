"""Mock back-office host serving both legacy dashboards.

  /meridian/  Dashboard A - ASP.NET WebForms dialect (frameset, __doPostBack, ViewState)
  /summit/    Dashboard B - Java/Struts dialect (.do actions, sync token, iframe)

Both expose the same business flow behind different markup, terminology and
navigation mechanics, which is what makes them useful as a heterogeneity test.

The /__control endpoints are a test harness, not part of the simulated product.
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, redirect, request, url_for

from . import data, faults
from .summit import bp as summit_bp
from .webforms import bp as webforms_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(webforms_bp)
    app.register_blueprint(summit_bp)

    @app.get("/")
    def index():
        return (
            "<h3 style='font-family:sans-serif'>Mock back-office</h3>"
            "<ul style='font-family:sans-serif'>"
            f"<li><a href='{url_for('webforms.index')}'>Meridian Core Servicing</a>"
            " &mdash; ASP.NET WebForms dialect</li>"
            f"<li><a href='{url_for('summit.index')}'>Summit Servicing</a>"
            " &mdash; Java/Struts dialect</li></ul>"
        )

    # ---- test control surface -------------------------------------------
    @app.post("/__control/fault")
    def arm_fault():
        body = request.get_json(silent=True) or {}
        try:
            faults.arm(body.get("name", ""), int(body.get("count", 1)))
        except ValueError as exc:
            return jsonify(error=str(exc), known=sorted(faults.KNOWN)), 400
        return jsonify(armed=faults.armed())

    @app.post("/__control/reset")
    def reset_all():
        faults.clear()
        data.reset()
        return jsonify(ok=True, armed=faults.armed())

    @app.get("/__control/state")
    def state():
        return jsonify(armed=faults.armed(), taxonomy=faults.KNOWN)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.getenv("MOCKBANK_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCKBANK_PORT", "5001")),
        debug=False,
    )
