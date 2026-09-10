"""Google Gemini, via the Generative Language REST API.

Two behaviours here exist because of what the free tier actually does rather than what
the documentation says:

* **Retry with backoff on 429 and 503.** Overload responses are routine, and arrive
  after a queue wait of tens of seconds. A single-shot call turns a discovery run into
  a coin flip.
* **Model fallback.** Model availability differs per key - `gemini-2.5-flash` is closed
  to newer keys, for instance - so a list is tried in order and the first that answers
  is kept for the rest of the run.
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any

import httpx

from .base import LLMError

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Tried in order. The first that answers is remembered for the rest of the run.
DEFAULT_MODELS = ("gemini-flash-latest", "gemini-2.5-flash-lite",
                  "gemini-flash-lite-latest")

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema -> the upper-cased dialect the API expects."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type":
            out["type"] = value.upper()
        elif key == "properties":
            out["properties"] = {k: _to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = _to_gemini_schema(value)
        elif key in ("enum", "required", "description"):
            out[key] = value
    return out


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 models: tuple[str, ...] = DEFAULT_MODELS,
                 max_attempts: int = 6, timeout: float = 120.0):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        preferred = model or os.getenv("GEMINI_MODEL") or ""
        self._candidates = ([preferred] if preferred else []) + [
            m for m in models if m != preferred]
        self.model = self._candidates[0]
        self.max_attempts = max_attempts
        self._client = httpx.Client(timeout=timeout)
        self.calls = 0
        self.tokens = 0

    def close(self) -> None:
        self._client.close()

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(schema),
                "temperature": 0.0,   # discovery should be as repeatable as it can be
            },
        }

        last = "no attempt made"
        unavailable: list[str] = []
        for model in list(self._candidates):
            for attempt in range(self.max_attempts):
                try:
                    response = self._client.post(
                        ENDPOINT.format(model=model),
                        params={"key": self.api_key}, json=payload)
                except httpx.HTTPError as exc:
                    last = f"{model}: transport error {exc}"
                else:
                    if response.status_code == 200:
                        self.model = model         # remember what works
                        for dead in unavailable:
                            if len(self._candidates) > 1:
                                self._candidates.remove(dead)
                        return self._parse(response.json())
                    last = f"{model}: HTTP {response.status_code} {response.text[:160]}"
                    if response.status_code not in _RETRYABLE:
                        # A model this key cannot use will never become usable. Retrying
                        # it on every later call wastes the run's time budget on a
                        # certainty - some models are closed to newer keys.
                        unavailable.append(model)
                        break
                # Exponential backoff with jitter, so parallel runs do not resonate.
                time.sleep(min(2 ** attempt + random.random(), 45))
        for model in unavailable:
            if len(self._candidates) > 1:
                self._candidates.remove(model)
        raise LLMError(f"no model produced a decision; last error: {last}")

    def _parse(self, body: dict) -> dict:
        self.calls += 1
        self.tokens += body.get("usageMetadata", {}).get("totalTokenCount", 0)
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:200]}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned non-JSON despite a schema: {text[:200]}") from exc
