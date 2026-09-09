"""Keeping regulated data out of artifacts, logs and evidence.

Two layers, because relying on either alone fails in a predictable way:

* **Declared secrets** - values the caller passed for an input marked sensitive. Exact,
  reliable, but only covers what someone remembered to declare.
* **Shape detection** - tax identifiers and card numbers recognised by their form.
  Catches what nobody declared: a balance screen that happens to render a full SSN, a
  model transcript quoting the page back. Cannot be complete, which is why it is a
  second line and not the first.

Redaction is applied on the way *out* of the surface, so anything written down is
already scrubbed rather than scrubbed later and hopefully everywhere.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# US tax identifier, the form these applications display.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13-19 digits, optionally grouped - a candidate card number, confirmed by Luhn so
# account numbers and reference numbers are not mangled.
_PAN_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask_pan(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if not (13 <= len(digits) <= 19) or not _luhn_ok(digits):
        return match.group(0)
    return f"{REDACTED}{digits[-4:]}"


class Redactor:
    """Scrubs declared secrets and recognisable sensitive shapes from text."""

    def __init__(self, secrets: list[str] | None = None):
        # Longest first, so a longer secret is not partly masked by a shorter one
        # that happens to be a substring of it.
        self._secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def add(self, *values: str) -> None:
        self._secrets = sorted({*self._secrets, *(v for v in values if v)},
                               key=len, reverse=True)

    def scrub(self, text: str | None) -> str:
        if not text:
            return text or ""
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _SSN.sub(REDACTED, text)
        return _PAN_CANDIDATE.sub(_mask_pan, text)

    def scrub_observation(self, observation):
        """Return a copy with every text-bearing field scrubbed."""
        copy = observation.model_copy(deep=True)
        copy.text_digest = self.scrub(copy.text_digest)
        copy.title = self.scrub(copy.title)
        copy.url = self.scrub(copy.url)
        for element in copy.elements:
            element.value = self.scrub(element.value) if element.value else element.value
            element.text = self.scrub(element.text) if element.text else element.text
            element.name = self.scrub(element.name)
            if element.label_text:
                element.label_text = self.scrub(element.label_text)
        return copy
