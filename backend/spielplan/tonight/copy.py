"""Tonight's strings (§6.2 step 5, §6.5).

D supports "one of you is likely to land below your usual" (AUC 0.610), never "someone will hate
this".
"""

from __future__ import annotations

# Spec-fixed copy, quoted rather than paraphrased: a paraphrase is where over-claim creeps in.
SPLIT_LINE = "You're split on {facet} — here's one of each. The axis is zeroed, not averaged."

# `{d}` in the data voice: model numbers appear next to their name (§6.8).
D_LINE = "D {d:.2f} — one of you is likely to land below your usual tonight."

# Member register (decision 486): `for_member` swaps it in at read time.
D_LINE_PLAIN = "One of you is likely to land below your usual tonight."

# Decision 479's headline for a split with no axis to name; fixed, never a model's.
PERSON_SPLIT_LINE = "You're pulling different ways tonight — here's one for each of you."
# Without the promise, for a room the three slots could not serve.
PERSON_SPLIT_SHORT = "You're pulling different ways tonight."

# Model vocabulary, dropped for a member without Show the model (decision 486).
_MODEL_SENTENCE = " The axis is zeroed, not averaged."

# The honest negative §6.2 step 7 quotes verbatim, for a participant no term pulls toward.
NO_PULL_LINE = "nothing here is their pull — {term} works against them"

# §6.2 step 7's pull lines, each saying only what its branch in `play._match_lines` establishes.
LEANED_LINE = "{name} leaned toward {terms} tonight"
USUAL_LINE = "suits {name}'s usual taste — {terms}"

# §6.2 step 7: every participant gets a match line, a guest without a profile too.
NO_PROFILE_LINE = "{name} — no profile yet"


def split_line(facet: str) -> str:
    return SPLIT_LINE.format(facet=facet)


def conflict(facet: str, *, d: float) -> dict[str, object]:
    """Everything a surfaced split says, all of it fixed by §6.2."""
    return {
        "facet": facet,
        "d": round(float(d), 4),
        "headline": split_line(facet),
        "explanation": D_LINE.format(d=d),
    }


def person_conflict(*, d: float, one_for_each: bool) -> dict[str, object]:
    """Decision 479's surfaced split, in `conflict`'s shape; `by` names which split this is."""
    return {
        "facet": None,
        "by": "person",
        "d": round(float(d), 4),
        "headline": PERSON_SPLIT_LINE if one_for_each else PERSON_SPLIT_SHORT,
        "explanation": D_LINE.format(d=d),
    }


def for_member(conflict: dict[str, object] | None) -> dict[str, object] | None:
    """A stored conflict as a member with Show the model off may read it (decision 486)."""
    if not conflict:
        return conflict
    plain = {k: v for k, v in conflict.items() if k != "d"}
    plain["explanation"] = D_LINE_PLAIN
    headline = str(plain.get("headline") or "")
    plain["headline"] = headline.replace(_MODEL_SENTENCE, "")
    return plain


def no_pull(term: str, *, name: str | None = None) -> str:
    """§6.2 step 7's honest negative, verbatim, prefixed with whose it is when named."""
    line = NO_PULL_LINE.format(term=term)
    return f"{name}: {line}" if name else line


def no_profile(name: str) -> str:
    return NO_PROFILE_LINE.format(name=name)


def _joined(words: list[str]) -> str:
    return " and ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " and " + words[-1]


def leaned(name: str, words: list[str]) -> str:
    return LEANED_LINE.format(name=name, terms=_joined(words))


def usual(name: str, words: list[str]) -> str:
    return USUAL_LINE.format(name=name, terms=_joined(words))


__all__ = [
    "D_LINE",
    "D_LINE_PLAIN",
    "LEANED_LINE",
    "NO_PROFILE_LINE",
    "NO_PULL_LINE",
    "PERSON_SPLIT_LINE",
    "PERSON_SPLIT_SHORT",
    "SPLIT_LINE",
    "USUAL_LINE",
    "conflict",
    "for_member",
    "leaned",
    "no_profile",
    "no_pull",
    "person_conflict",
    "split_line",
    "usual",
]
