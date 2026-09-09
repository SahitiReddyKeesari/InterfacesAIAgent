"""What the model is told, and the shape of what it may say back.

The prompt deliberately describes controls the way the surface abstraction does - by
role, accessible name and caption - and never mentions HTML, selectors or the DOM. Two
reasons: the model should choose a control the way an operator would, and anything it
says has to survive being replayed against a surface that may not be a browser at all.
"""
from __future__ import annotations

from ..surfaces.models import Observation

SYSTEM = """\
You are operating a back-office business application on behalf of a bank employee, by \
reading the screen and acting on it - exactly as a human operator would.

You are shown the controls and values currently visible. Each has a reference like \
`main#7`, a role, and either an accessible name or the caption printed beside it. Many \
of these applications label nothing properly, so the caption in the neighbouring cell is \
often a field's only identifier. That is normal; use it.

Choose ONE action at a time, and explain your reasoning in one short sentence.

Rules that matter:
- Act only on controls listed in the observation. Never invent a reference.
- When several controls share a name (an "Open" link on every row of a grid), set \
`scope_text` to a value that appears in the row you mean - an account number, a \
reference - so the right row is identified by what it contains rather than by its \
position.
- `checkpoint_text` must be a short, distinctive phrase you expect to be visible after \
the action succeeds. It is what proves the step worked when this flow is replayed \
without you. Prefer a heading or a label over a value that will differ next time.
- Classify each action's `risk`: `safe` if nothing persists (reading, typing, \
navigating), `consequential` if it writes something that another flow could undo, \
`irreversible` if it cannot be undone through this application.
- To type a value the caller supplied, set `parameter` to its name. The exact value is \
substituted for you - you never see it and must not guess, reformat, pad or zero-fill \
it. Set `parameter` to "none" only when typing a literal you chose yourself.
- `text` is the literal characters to type, and nothing else. Never put an explanation, \
a note or a placeholder in it.
- Use `read` to extract a value the caller asked for, and give it a short snake_case \
`output_name`.
- Answer `done` when the goal is complete, and set `success_text` to a phrase that \
proves it. Answer `give_up` if you are stuck or the goal appears impossible.

Never enter credentials, and never take an irreversible action unless the goal \
explicitly asks for it."""

NO_PARAMETER = "none"


def decision_schema(parameter_names: list[str] | None = None) -> dict:
    """The response contract for one run.

    `parameter` is constrained to an enum of the actual declared names because a free
    string invited exactly the failure this was written after: asked to name a
    parameter, the model instead wrote an explanatory sentence into the value field and
    the loop typed it into the search box. Constraining a field is far more reliable
    than instructing the model about it - the same reason `action` is an enum.
    """
    schema = {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in _DECISION_SCHEMA.items()}
    schema["properties"] = {k: dict(v) for k, v in _DECISION_SCHEMA["properties"].items()}
    schema["properties"]["parameter"]["enum"] = [*(parameter_names or []), NO_PARAMETER]
    return schema


# The response contract. Constrained so a malformed reply is impossible rather than
# merely unlikely - the loop should never have to parse prose.
_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string",
                      "description": "One sentence: why this action, now."},
        "action": {"type": "string",
                   "enum": ["fill", "click", "select", "read", "done", "give_up"]},
        "target_ref": {"type": "string",
                       "description": "Reference of the control to act on, e.g. main#7."},
        "parameter": {"type": "string",
                      "description": "Name of a caller-supplied parameter to type, or "
                                     "\"none\" for a literal in `text`."},
        "text": {"type": "string",
                 "description": "A literal value to type or select, when no parameter "
                                "applies. Never a reformatted parameter value."},
        "output_name": {"type": "string",
                        "description": "snake_case name for a value being read."},
        "scope_text": {"type": "string",
                       "description": "Text identifying the row containing the control."},
        "checkpoint_text": {"type": "string",
                            "description": "Distinctive phrase expected after success."},
        "risk": {"type": "string", "enum": ["safe", "consequential", "irreversible"]},
        "success_text": {"type": "string",
                         "description": "On done: a phrase proving the goal was met."},
        "note": {"type": "string"},
    },
    "required": ["reasoning", "action"],
}

MAX_LISTED = 60


def render_observation(observation: Observation, goal: str, history: list[str],
                       step: int, budget: int,
                       parameters: dict[str, str] | None = None) -> str:
    """Compact the screen into something worth spending tokens on.

    Only visible, enabled, actionable-or-readable controls, capped - a full element dump
    of a legacy page is mostly layout scaffolding and crowds out the reasoning.
    """
    lines = [f"GOAL: {goal}", f"STEP {step} of at most {budget}", ""]
    if parameters:
        lines.append("VALUES THE CALLER SUPPLIES (use `parameter`, never retype these):")
        lines += [f"  {name}" for name in parameters]
        lines.append("")
    if history:
        lines += ["WHAT YOU HAVE DONE SO FAR:", *(f"  {h}" for h in history[-8:]), ""]

    lines.append(f"CURRENT SCREEN ({observation.title or observation.url}):")
    shown = 0
    for element in observation.elements:
        if not element.enabled or shown >= MAX_LISTED:
            continue
        label = element.name or element.label_text or element.column_header or ""
        descriptor = f"  {element.ref}  {element.role.value}"
        if label:
            descriptor += f"  {label!r}"
        if element.value and element.value != label:
            descriptor += f"  = {element.value[:60]!r}"
        lines.append(descriptor)
        shown += 1

    lines += ["", "VISIBLE TEXT:", _content_first(observation.text_digest)]
    return "\n".join(lines)


PER_FRAME_CHARS = 1200
TOTAL_DIGEST_CHARS = 2400


def _content_first(digest: str) -> str:
    """Order frame text by how much of it there is, largest first.

    A frameset puts the banner and the navigation ahead of the workspace, so a naive
    dump leads with chrome and risks truncating away the one frame that holds the
    answer. Ranking by volume is a crude proxy for "where the content is", but it is
    stable and needs no knowledge of the application's layout.
    """
    blocks = [b.strip() for b in digest.split("\n") if b.strip()]
    blocks.sort(key=len, reverse=True)
    out, budget = [], TOTAL_DIGEST_CHARS
    for block in blocks:
        chunk = block[:PER_FRAME_CHARS]
        if budget - len(chunk) < 0:
            break
        out.append(chunk)
        budget -= len(chunk)
    return "\n".join(out)
