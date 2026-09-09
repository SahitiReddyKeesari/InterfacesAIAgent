"""A browser Surface built on Playwright.

Two things here are specific to the environment this targets rather than to browsers in
general, and both are the reason the seam exists:

  * Frames. The flow often does not live in the top-level document: legacy apps use
    framesets, and newer ones embed a workspace in an iframe. Every element carries the
    frame path it was found in, and acting resolves back into that frame.
  * Caption-based targeting. These pages have no test ids and no <label for>, so a
    field is identified by the caption in the neighbouring cell. That has a direct
    analogue on a desktop surface (the static text preceding a control), which is what
    makes the recorded flow portable off the browser.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame, Page, TimeoutError as PWTimeout, sync_playwright

from .models import (Action, ActionResult, CheckResult, Checkpoint, Element, Locator,
                     Observation, Resolution, Role, Strategy)

_JS = (Path(__file__).parent / "_inventory.js").read_text()

# CSS for "any control of this role", used only by the ordinal fallback.
_ROLE_CSS = {
    Role.TEXTBOX: "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=checkbox]):not([type=radio]), textarea",
    Role.BUTTON: "button, input[type=submit], input[type=button], input[type=reset]",
    Role.LINK: "a[href]",
    Role.COMBOBOX: "select",
    Role.CHECKBOX: "input[type=checkbox]",
    Role.RADIO: "input[type=radio]",
}

_INPUT_CSS = "input:not([type=hidden]), select, textarea"
_READABLE = {Role.TEXT, Role.CELL}


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        return Role.UNKNOWN


class PlaywrightSurface:
    """A Surface over one browser page."""

    name = "web"

    def __init__(self, headless: bool = True, slow_mo: int = 0) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self._page: Page = self._browser.new_page(viewport={"width": 1280, "height": 800})
        self._shots = 0
        # Count in-flight *document* requests. A postback to a slow backend is a
        # pending navigation of an inner frame: the old document is still "loaded",
        # nothing has rendered differently yet, and no page-level load state changes.
        # Counting the request itself is the only signal that survives all three.
        self._pending_docs = 0
        self._last_frame_miss: str | None = None
        self._page.on("request", self._on_request)
        self._page.on("requestfinished", self._on_request_done)
        self._page.on("requestfailed", self._on_request_done)

    def _on_request(self, request: Any) -> None:
        if request.resource_type == "document":
            self._pending_docs += 1

    def _on_request_done(self, request: Any) -> None:
        if request.resource_type == "document":
            self._pending_docs = max(0, self._pending_docs - 1)

    # ------------------------------------------------------------------ frames
    def _frame_path(self, frame: Frame) -> list[str]:
        """Names from the top document down. Unnamed frames fall back to a url tail."""
        path: list[str] = []
        node: Frame | None = frame
        while node is not None and node.parent_frame is not None:
            path.append(node.name or node.url.rsplit("/", 1)[-1] or "frame")
            node = node.parent_frame
        return list(reversed(path))

    def _frame_for(self, path: list[str]) -> Frame:
        if not path:
            return self._page.main_frame
        for frame in self._page.frames:
            if self._frame_path(frame) == path:
                return frame
        # A frame that moved or was renamed: fall back to matching the last segment,
        # which is far more often right than failing outright.
        for frame in self._page.frames:
            if self._frame_path(frame)[-1:] == path[-1:]:
                return frame
        # Nothing matched. Returning the top document here is a guess, and a locator
        # that then fails looks like a missing control rather than a missing frame -
        # so record it for the resolution detail.
        self._last_frame_miss = "/".join(path)
        return self._page.main_frame

    # ----------------------------------------------------------------- observe
    def observe(self) -> Observation:
        self._settle()
        elements: list[Element] = []
        frames: list[str] = []
        digest: list[str] = []
        title = self._page.title()

        for frame in self._page.frames:
            path = self._frame_path(frame)
            label = "/".join(path) if path else "(top)"
            frames.append(label)
            try:
                data: dict[str, Any] = frame.evaluate(_JS)
            except Exception:
                continue  # a frame mid-navigation is not an error, just not readable yet
            if data.get("text"):
                digest.append(f"[{label}] {data['text']}")
            for i, raw in enumerate(data.get("elements", [])):
                if not raw.get("visible"):
                    continue
                elements.append(Element(
                    ref=f"{label}#{i}",
                    role=_role(raw["role"]),
                    name=raw.get("name") or "",
                    value=raw.get("value"),
                    label_text=raw.get("label_text") or None,
                    column_header=raw.get("column_header") or None,
                    control_id=raw.get("control_id") or None,
                    text=raw.get("text") or None,
                    enabled=bool(raw.get("enabled", True)),
                    visible=True,
                    frame_path=path,
                    ordinal=int(raw.get("ordinal", 0)),
                ))

        return Observation(
            url=self._page.url, title=title, elements=elements, frames=frames,
            text_digest="\n".join(digest)[:8000],
        )

    def _signature(self) -> tuple:
        """Cheap fingerprint of every frame's url and rendered text."""
        out = []
        for frame in self._page.frames:
            try:
                n = frame.evaluate(
                    "() => { const b = document.body;"
                    " return b ? b.innerText.length + '|' + b.innerText.slice(0, 200) : ''; }")
            except Exception:
                n = "?"
            out.append((frame.url, n))
        return tuple(out)

    def _settle(self, timeout_ms: int = 15_000, quiet_ms: int = 200) -> None:
        """Wait for the surface to be genuinely ready, in two stages.

        A postback navigates an inner frame while the top document stays put, so waiting
        on the page's load state returns immediately and the next action races the old
        DOM.

        Content stability alone does not fix that either: while a slow postback is still
        in flight nothing has changed *yet*, so a fingerprint of the rendered text looks
        perfectly quiet and the old page gets read as if it were the new one. An
        outstanding request is invisible to any content-based check.

        So: first wait for in-flight document requests to come back, then wait for what
        rendered to stop moving. Together those ride out a genuinely slow backend, which
        a fixed sleep cannot do without either flaking or padding every step with the
        worst case.
        """
        deadline = time.time() + timeout_ms / 1000

        # Stage 1: let a navigation start, then wait for it to come back.
        # NB: wait_for_timeout, not time.sleep. Playwright's sync API only dispatches
        # events while inside one of its calls, so sleeping natively here would freeze
        # the request counters at whatever they were and spin until the deadline.
        grace = time.time() + 0.35
        while time.time() < grace and self._pending_docs == 0:
            self._page.wait_for_timeout(20)
        while self._pending_docs > 0 and time.time() < deadline:
            self._page.wait_for_timeout(50)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=1000)
        except PWTimeout:
            pass
        last: tuple | None = None
        quiet_from: float | None = None
        while time.time() < deadline:
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=400)
            except PWTimeout:
                pass
            sig = self._signature()
            if sig == last:
                if quiet_from is None:
                    quiet_from = time.time()
                elif (time.time() - quiet_from) * 1000 >= quiet_ms:
                    return
            else:
                last, quiet_from = sig, None
            self._page.wait_for_timeout(50)

    # ----------------------------------------------------------------- resolve
    # Containers that repeat one record. Ordered most-specific first: a semantic
    # container is a better scope than a generic block, but a surface that uses neither
    # (a div-based list, a desktop pane) still has to work - which the earlier
    # tr-only version did not.
    _SCOPE_TIERS = (
        "tr, [role=row], li, [role=listitem]",
        "fieldset, article, section, td, div",
    )

    def _scoped(self, frame: Frame, locator: Locator):
        """Root to search under - the whole frame, or the one record containing the key.

        Two subtleties, both learned the hard way on this markup:

        * Layouts nest containers inside containers, so an outer element also "contains"
          the text and would silently widen the scope back to the whole list - which
          then falls through to a positional id and selects the wrong record. The
          tightest match is the right one.
        * A container that holds the text but not the control is not a scope at all
          (a label cell matches "12347" without containing the Select link), so
          candidates are filtered by whether they actually hold the target role.
        """
        if locator.scope is None:
            return frame.locator("body")

        wanted = _ROLE_CSS.get(locator.role) if locator.role else None
        for tier in self._SCOPE_TIERS:
            rows = frame.locator(tier).filter(has_text=locator.scope.contains_text)
            best, best_key = None, None
            for i in range(min(rows.count(), 40)):
                row = rows.nth(i)
                try:
                    if wanted and row.locator(wanted).count() == 0:
                        continue
                    key = (row.locator(tier).count(), len(row.inner_text()))
                except Exception:
                    continue
                if best_key is None or key < best_key:
                    best, best_key = row, key
            if best is not None:
                return best
        return None

    def _try(self, root, candidate, role: Role | None = None) -> Any:
        strategy, value = candidate.strategy, candidate.value
        if strategy == Strategy.ROLE_NAME:
            role, _, name = value.partition(":")
            return root.get_by_role(role, name=name, exact=False)  # type: ignore[arg-type]
        if strategy == Strategy.LABEL_TEXT:
            # The caption lives in a sibling cell; walk from it to whatever it labels.
            # For an input that is the control in the next cell; for a displayed value
            # it is the next cell itself.
            escaped = value.replace('"', '\\"')
            base = f'xpath=.//td[contains(normalize-space(.), "{escaped}")]/following-sibling::td[1]'
            if role in _READABLE:
                return root.locator(base)
            return root.locator(
                base + '//*[self::input or self::select or self::textarea]')
        if strategy == Strategy.COLUMN_CELL:
            # Resolve the column by its heading, then take that cell of the scoped row.
            # Two steps rather than one selector, because a selector cannot express
            # "the cell under the heading that says X".
            try:
                idx = root.evaluate(
                    """(row, header) => {
                        const table = row.closest('table');
                        if (!table || !table.rows.length) return -1;
                        const head = table.rows[0];
                        for (let i = 0; i < head.cells.length; i++) {
                            if (head.cells[i].textContent.trim() === header) return i;
                        }
                        return -1;
                    }""", value)
            except Exception:
                return None
            return None if idx < 0 else root.locator("td").nth(idx)
        if strategy == Strategy.CONTROL_ID:
            v = value.replace('"', '\\"')
            return root.locator(f'[id="{v}"], [name="{v}"]')
        if strategy == Strategy.TEXT:
            return root.get_by_text(value, exact=False)
        if strategy == Strategy.ORDINAL:
            role_name, _, idx = value.rpartition(":")
            css = _ROLE_CSS.get(_role(role_name), _INPUT_CSS)
            return root.locator(css).nth(int(idx))
        return None

    def resolve(self, locator: Locator) -> Resolution:
        self._last_frame_miss = None
        frame = self._frame_for(locator.frame_path)
        root = self._scoped(frame, locator)
        if root is None:
            return Resolution(resolved=False, detail=(
                f"scope row containing {locator.scope.contains_text!r} not present"
                if locator.scope else "scope root missing"))

        fell: list[Strategy] = []
        for cand in locator.candidates:
            try:
                found = self._try(root, cand, locator.role)
                if found is None:
                    continue
                n = found.count()
            except Exception:
                fell.append(cand.strategy)
                continue
            if n == 1:
                return Resolution(resolved=True, strategy=cand.strategy,
                                  confidence=cand.confidence, matches=1,
                                  fell_through=fell,
                                  detail=f"matched by {cand.strategy.value}")
            fell.append(cand.strategy)
        miss = (f"; frame {self._last_frame_miss!r} was not present"
                if self._last_frame_miss else "")
        return Resolution(resolved=False, matches=0, fell_through=fell,
                          detail="no candidate resolved to exactly one control" + miss)

    def _handle(self, locator: Locator):
        """The Playwright locator for the winning candidate, or None."""
        frame = self._frame_for(locator.frame_path)
        root = self._scoped(frame, locator)
        if root is None:
            return None, Resolution(resolved=False, detail="scope not present")
        fell: list[Strategy] = []
        for cand in locator.candidates:
            try:
                found = self._try(root, cand, locator.role)
                if found is not None and found.count() == 1:
                    return found, Resolution(resolved=True, strategy=cand.strategy,
                                             confidence=cand.confidence, matches=1,
                                             fell_through=fell,
                                             detail=f"matched by {cand.strategy.value}")
            except Exception:
                pass
            fell.append(cand.strategy)
        return None, Resolution(resolved=False, fell_through=fell,
                                detail="no candidate resolved to exactly one control")

    # --------------------------------------------------------------------- act
    def act(self, action: Action) -> ActionResult:
        kind = action.kind

        if kind == "navigate":
            self._page.goto(action.url, wait_until="domcontentloaded")
            # Child frames attach after the top document commits. Without settling here
            # the next step resolves against a page whose frames do not exist yet, and
            # silently falls back to the top document.
            self._settle()
            return ActionResult(ok=True, detail=f"navigated to {action.url}")

        if kind == "press":
            self._page.keyboard.press(action.key)
            self._settle()
            return ActionResult(ok=True, detail=f"pressed {action.key}")

        if kind == "wait_for":
            if action.text is not None:
                try:
                    self._page.wait_for_function(
                        "t => document.body && document.body.innerText.includes(t)",
                        arg=action.text, timeout=action.timeout_ms)
                    return ActionResult(ok=True, detail=f"saw {action.text!r}")
                except PWTimeout:
                    return ActionResult(ok=False, detail=f"never saw {action.text!r}")
            res = self.resolve(action.locator)
            return ActionResult(ok=res.resolved, resolution=res)

        handle, res = self._handle(action.locator)
        if handle is None:
            return ActionResult(ok=False, resolution=res,
                                detail=f"could not locate {action.locator.description!r}")

        if kind == "read":
            try:
                tag = handle.evaluate("e => e.tagName.toLowerCase()")
                value = (handle.input_value() if tag in {"input", "textarea", "select"}
                         else handle.inner_text())
            except Exception as exc:
                return ActionResult(ok=False, resolution=res, detail=str(exc))
            return ActionResult(ok=True, resolution=res, value=value.strip())

        try:
            if kind == "click":
                handle.click()
            elif kind == "fill":
                handle.fill(action.text)
            elif kind == "select":
                try:
                    handle.select_option(label=action.option)
                except Exception:
                    handle.select_option(action.option)
            self._settle()
        except Exception as exc:
            return ActionResult(ok=False, resolution=res, detail=str(exc))

        return ActionResult(ok=True, resolution=res,
                            detail=f"{kind} via {res.strategy.value if res.strategy else '?'}")

    def current_url(self) -> str:
        return self._page.url

    # ------------------------------------------------------------- checkpoints
    def check(self, checkpoint: Checkpoint) -> CheckResult:
        obs = self.observe()
        haystack = f"{obs.text_digest}\n{obs.title}"
        missing = [t for t in checkpoint.text_present if t not in haystack]
        forbidden = [t for t in checkpoint.text_absent if t in haystack]
        unresolved = [loc.description for loc in checkpoint.locator_present
                      if not self.resolve(loc).resolved]
        return CheckResult(
            passed=not (missing or forbidden or unresolved),
            checkpoint=checkpoint.description,
            missing_text=missing, forbidden_text=forbidden, unresolved=unresolved,
        )

    # ---------------------------------------------------------------- evidence
    def capture(self, label: str, into: Path) -> Path | None:
        into = Path(into)
        into.mkdir(parents=True, exist_ok=True)
        self._shots += 1
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:48]
        path = into / f"{self._shots:02d}-{safe}.png"
        self._page.screenshot(path=str(path), full_page=True)
        return path

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()

    def __enter__(self) -> "PlaywrightSurface":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
