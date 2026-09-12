"""Turning a person's question into a capability call - or admitting it cannot.

This is the *agent's* use of a model, not the automation's. The brief draws the line
plainly: the agent-facing product decides what to do; this system is how it reliably
does it. Choosing which capability answers a question, and pulling arguments out of a
sentence, is deciding what to do. Executing it stays deterministic, and nothing in
`replay/` can reach this module.

The design commitment that matters here is the one about not guessing. A router that
always returns its best match will confidently call the balance capability for a
question about card status, and the caller gets a plausible answer to a question nobody
asked. So the contract has room to say `none`, to report low confidence, and to name
arguments it could not determine - and the assistant is expected to relay that rather
than answer anyway.
"""
from __future__ import annotations

import json
from typing import Any

from .llm.base import LLMError, LLMProvider

SYSTEM = """\
You route a bank employee's question to one of the automated capabilities available to \
you, or say that none of them fits.

You are given each capability's name, what it does, what arguments it takes and what it \
returns. Decide which one answers the question, and extract its arguments from the \
question.

Rules that matter more than being helpful:
- Only match a capability that genuinely answers the question asked. A capability that \
returns a balance does not answer a question about a card, however similar the wording.
- If nothing fits, set `match` to "none" and write a `goal` describing what would have \
to be done in the application to answer it - one sentence, in the imperative. Put into \
`arguments` every value in the question that would differ for another caller - a member \
number, an account type, a card - named in snake_case. Those become the new \
capability's inputs, and a capability recorded without them only ever works for the one \
person who first asked.
- Set `confidence` honestly. "low" is the right answer when the question is ambiguous, \
when it could plausibly mean two different capabilities, or when you are guessing.
- List in `missing` any argument the capability requires that the question does not \
supply. Never invent a value - an invented member number is a lookup of somebody \
else's account.
- Extract arguments exactly as the question gives them. Do not reformat, pad or expand \
them."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {"type": "string",
                  "description": "Capability id that answers this, or \"none\"."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "arguments": {"type": "string",
                      "description": "JSON object of name to value, extracted from the "
                                     "question. When a capability matched, its argument "
                                     "values. When none matched, the values that should "
                                     "become the new capability's inputs. \"{}\" only "
                                     "if the question truly contains no such value."},
        "missing": {"type": "string",
                    "description": "Comma-separated names of required arguments the "
                                   "question does not supply. Empty if none."},
        "goal": {"type": "string",
                 "description": "If match is \"none\": one imperative sentence "
                                "describing what to do in the application."},
        "reasoning": {"type": "string", "description": "One sentence."},
    },
    "required": ["match", "confidence", "reasoning"],
}

NONE = "none"


class Route:
    """What the router decided, in a form the assistant can act on."""

    def __init__(self, raw: dict[str, Any], known: set[str]):
        self.reasoning = (raw.get("reasoning") or "").strip()
        self.confidence = (raw.get("confidence") or "low").strip().lower()
        self.goal = (raw.get("goal") or "").strip()
        match = (raw.get("match") or NONE).strip()
        # A model naming a capability that does not exist is the same as no match.
        self.capability_id = match if match in known else None
        self.missing = [m.strip() for m in (raw.get("missing") or "").split(",")
                        if m.strip()]
        self.arguments = _as_object(raw.get("arguments"))

    @property
    def confident(self) -> bool:
        return self.confidence in ("high", "medium")

    @property
    def can_answer(self) -> bool:
        """Only when a real capability was matched, confidently, with every argument."""
        return bool(self.capability_id) and self.confident and not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "confidence": self.confidence,
                "arguments": self.arguments, "missing": self.missing,
                "goal": self.goal, "reasoning": self.reasoning,
                "can_answer": self.can_answer}


def _as_object(value: Any) -> dict[str, str]:
    """Arguments arrive as a JSON string because nested free-form objects are not
    reliably expressible in the response schema."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    return {}


def describe_catalog(catalog: list[dict[str, Any]]) -> str:
    """The capabilities, as the router sees them."""
    if not catalog:
        return "There are no capabilities available yet."
    lines = []
    for entry in catalog:
        args = ", ".join(entry.get("input_schema", {}).get("properties", {}))
        returns = ", ".join(entry.get("returns", {}))
        lines.append(f"- {entry['name']}: {entry['description']}\n"
                     f"    arguments: {args or 'none'}\n"
                     f"    returns:   {returns or 'nothing'}")
    return "\n".join(lines)


def route(llm: LLMProvider, question: str, catalog: list[dict[str, Any]]) -> Route:
    """Decide which capability answers `question`, or that none does."""
    prompt = (f"CAPABILITIES AVAILABLE:\n{describe_catalog(catalog)}\n\n"
              f"QUESTION FROM THE OPERATOR:\n{question.strip()}")
    try:
        raw = llm.complete_json(SYSTEM, prompt, SCHEMA)
    except LLMError as exc:
        return Route({"match": NONE, "confidence": "low",
                      "reasoning": f"the router was unavailable: {exc}"}, set())
    return Route(raw, {entry["name"] for entry in catalog})
