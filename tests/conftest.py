"""Shared fixtures: a live mock back-office for the whole test session.

The Java server is compiled and started once per session on a free port, then torn
down. Tests drive it over HTTP, exactly as the automation does.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_SH = REPO / "mock" / "run.sh"

P = "ctl00$ContentPlaceHolder1$"
TOKEN_FIELD = "org.apache.struts.taglib.html.TOKEN"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url() -> str:
    port = _free_port()
    proc = subprocess.Popen(
        ["bash", str(RUN_SH)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "MOCKBANK_HOST": "127.0.0.1", "MOCKBANK_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    # javac runs on first start, so allow a generous readiness window.
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"mock server exited early:\n{proc.stdout.read()}")
        try:
            urllib.request.urlopen(f"{url}/__control/state", timeout=1).read()
            break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.4)
    else:
        proc.kill()
        pytest.fail("mock server did not become ready")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def srv(base_url):
    """Per-test handle with a clean fault/data state either side."""
    api = _Client(base_url)
    api.reset()
    yield api
    api.reset()


class _Client:
    def __init__(self, base: str):
        self.base = base

    def get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return r.read().decode()

    def post(self, path: str, **fields) -> tuple[int, str]:
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def html(self, path: str, **fields) -> str:
        return self.post(path, **fields)[1]

    def reset(self):
        urllib.request.urlopen(
            urllib.request.Request(self.base + "/__control/reset", data=b"{}"), timeout=20).read()

    def arm(self, name: str, count: int = 1):
        payload = json.dumps({"name": name, "count": count}).encode()
        urllib.request.urlopen(
            urllib.request.Request(self.base + "/__control/fault", data=payload,
                                   headers={"Content-Type": "application/json"}),
            timeout=20).read()

    def state(self) -> dict:
        return json.loads(self.get("/__control/state"))




@pytest.fixture()
def surface(base_url):
    """A live browser Surface, torn down after each test."""
    from cua.surfaces.web import PlaywrightSurface
    s = PlaywrightSurface()
    try:
        yield s
    finally:
        s.close()
