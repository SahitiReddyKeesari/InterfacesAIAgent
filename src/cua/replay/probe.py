"""Deriving a capability's known business outcomes, without a model.

A discovery run records the happy path. That leaves the artifact unable to distinguish
"no such member" from "the application is broken" - and conflating those is the mistake
the brief singles out. Declaring them by hand works but does not scale to hundreds of
capabilities.

They can be derived instead. Replay the recorded plan twice: once with arguments that
succeed, once with arguments chosen to provoke the outcome. Whatever the application
says in the second run and not in the first *is* the signature of that outcome. No model
is involved, which matters - a signature invented by an LLM is exactly the hallucinated
checkpoint problem in a new costume.

The comparison is what makes it safe. Text taken from the probe run alone would include
the entire page chrome; text unique to it is what actually distinguishes the outcome.

Candidates come from the *perceived elements*, not from the page's flattened text. The
surface has already segmented the screen into discrete values, so a message arrives as
one string. Splitting flattened text instead produced signatures like "Member Inquiry
Member / Name: Servicing Branch: (All) ... No member records match" - an entire panel,
matched as one substring, and therefore far too brittle to survive any change.
"""
from __future__ import annotations

import re

from ..artifact.schema import Capability, KnownOutcome, Outcome
from ..surfaces.models import Checkpoint, Observation

# Lines shorter than this are rarely distinctive enough to identify an outcome, and
# lines longer are usually a whole panel rather than a message.
MIN_SIGNATURE = 12
MAX_SIGNATURE = 160

# A candidate line that changes between runs by itself - a balance, a count, a
# timestamp - is not a signature, it is data.
_HAS_DIGITS = re.compile(r"\d")

# Enterprise applications label their conditions. A code is the single most durable
# thing on the screen: wording gets rewritten, codes are referenced in runbooks.
_CONDITION_CODE = re.compile(r"\b[A-Z]{2,4}-\d{3,5}\b")


def candidates(observation: Observation) -> list[str]:
    """Discrete strings the surface perceived - one per control or value."""
    out: list[str] = []
    for element in observation.elements:
        for text in (element.value, element.text, element.name):
            piece = (text or "").strip()
            if MIN_SIGNATURE <= len(piece) <= MAX_SIGNATURE and piece not in out:
                out.append(piece)
    title = (observation.title or "").strip()
    if MIN_SIGNATURE <= len(title) <= MAX_SIGNATURE:
        out.append(title)
    return out


def distinctive_text(baseline: Observation, probe: Observation) -> list[str]:
    """Values the probe run showed and the successful run did not."""
    seen = set(candidates(baseline))
    unique: list[str] = []
    for line in candidates(probe):
        if line in seen or line in unique:
            continue
        if _HAS_DIGITS.search(line) and not any(c.isalpha() for c in line):
            continue          # a bare number is data, not a message
        unique.append(line)
    return unique


def propose_outcome(baseline: Observation, probe: Observation, name: str,
                    description: str, outcome: Outcome = Outcome.BUSINESS,
                    limit: int = 2) -> KnownOutcome | None:
    """Build a KnownOutcome from what the two runs disagreed about.

    Returns None when nothing distinguished them - which is itself informative: an
    outcome that looks identical to success cannot be recognised on replay, and
    pretending otherwise would produce a signature that matches everything.
    """
    unique = distinctive_text(baseline, probe)
    if not unique:
        return None
    ranked = sorted(unique, key=lambda line: (-_score(line), len(line)))
    # Only keep lines that look like the application *saying* something. A field
    # caption can differ between two runs simply because they stopped on different
    # screens, and padding the signature with one makes it depend on a label that has
    # nothing to do with the outcome.
    speaking = [line for line in ranked if _score(line) >= 2][:limit]
    signature = speaking or ranked[:1]
    return KnownOutcome(
        name=name,
        description=description,
        outcome=outcome,
        signature=Checkpoint(
            description=f"The application reports: {signature[0]}",
            text_present=signature),
    )


def _score(line: str) -> int:
    """How much this line looks like an application telling you what happened.

    Not all differences are equal. "Search Results (0)" differs from the happy path but
    is a count, and a count of zero can happen for reasons that are not this outcome.
    "No member records match the criteria entered." is the application naming the
    condition, and a code like SEC-0917 is better still.
    """
    score = 0
    if _CONDITION_CODE.search(line):
        score += 4
    if line.rstrip().endswith((".", "!")):
        score += 2
    if _HAS_DIGITS.search(line):
        score -= 2          # counts and amounts drift; wording does not
    if len(line) > 100:
        score -= 1          # probably a whole panel rather than one message
    return score


def add_outcome(capability: Capability, outcome: KnownOutcome) -> Capability:
    """Return a copy with the outcome added, replacing one of the same name."""
    updated = capability.model_copy(deep=True)
    updated.known_outcomes = [k for k in updated.known_outcomes
                              if k.name != outcome.name] + [outcome]
    return updated
