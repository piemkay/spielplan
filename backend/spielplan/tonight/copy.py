"""Tonight's strings (§6.2 steps 4, 5 and 8).

D supports "one of you is likely to land below your usual" (AUC 0.610), never "someone will hate
this".
"""

from __future__ import annotations

# `{d}` in the data voice: model numbers appear next to their name (§6.8).
D_LINE = "D {d:.2f} — one of you is likely to land below your usual tonight."

# Member register (decision 486): `for_member` swaps it in at read time.
D_LINE_PLAIN = "One of you is likely to land below your usual tonight."

# Spec-fixed copy, quoted rather than paraphrased: a paraphrase is where over-claim creeps in.
PERSON_SPLIT_LINE = "You're pulling different ways tonight — here's one for each of you."
# Without the promise, for a room the three slots could not serve.
PERSON_SPLIT_SHORT = "You're pulling different ways tonight."

# The honest negative §6.2 step 7 quotes verbatim, for a participant no term pulls toward.
NO_PULL_LINE = "nothing here is their pull — {term} works against them"

# §6.2 step 7's pull lines, each saying only what its branch in `play._match_lines` establishes.
LEANED_LINE = "{name} leaned toward {terms} tonight"
USUAL_LINE = "suits {name}'s usual taste — {terms}"

# §6.2 step 7: every participant gets a match line, a guest without a profile too.
NO_PROFILE_LINE = "{name} — no profile yet"

# §6.2 step 4: a seat with too little to ask about goes straight to the picks, saying why.
NO_ROUND_LINE = (
    "No mood questions tonight — they need {need} films on your ladder at {word} or higher, "
    "and you have {n}."
)
# When some of those films cannot be asked about: marked Not seen, or carrying a vetoed term.
NO_ROUND_SEEN_LINE = (
    "No mood questions tonight — they need {need} films on your ladder at {word} or higher that "
    "you've seen{vetoes}, and you have {n}."
)
NO_ROUND_GUEST_LINE = (
    "No mood questions tonight — they need {need} well-known films in the library{vetoes}, "
    "and it has {n}."
)

# §6.2 step 8's provenance lines; `{budget}` is "2h 10m", per episode on a series night.
PROVENANCE_MOOD = "Your mood tonight — {terms} · fits in {budget}"
PROVENANCE_FLAT = "No strong mood tonight — your usual favourites · fits in {budget}"
PROVENANCE_USUAL = "Your usual favourites · fits in {budget}"


def no_round(
    *, need: int, have: int, word: str | None, placed: int = 0, vetoed: bool = False
) -> str:
    """`word` is the lowest liked tier's word on the person's ladder, None for a guest; `placed` is
    how many films that tier and above hold, `have` how many of them the round may ask about."""
    if word is None:
        vetoes = " that tonight's vetoes leave in" if vetoed else ""
        return NO_ROUND_GUEST_LINE.format(need=need, n=have, vetoes=vetoes)
    if placed > have:
        vetoes = " and tonight's vetoes leave in" if vetoed else ""
        return NO_ROUND_SEEN_LINE.format(need=need, word=word, n=have, vetoes=vetoes)
    return NO_ROUND_LINE.format(need=need, word=word, n=have)


def provenance(*, budget: str, terms: list[str] | None) -> str:
    """No round: `terms` is None. A round that read no strong mood: an empty list."""
    if terms is None:
        return PROVENANCE_USUAL.format(budget=budget)
    if not terms:
        return PROVENANCE_FLAT.format(budget=budget)
    return PROVENANCE_MOOD.format(terms=" · ".join(terms), budget=budget)


def person_conflict(*, d: float, one_for_each: bool) -> dict[str, object]:
    """Everything a surfaced split says, all of it fixed by §6.2 step 5."""
    return {
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
    "NO_ROUND_GUEST_LINE",
    "NO_ROUND_LINE",
    "NO_ROUND_SEEN_LINE",
    "PERSON_SPLIT_LINE",
    "PERSON_SPLIT_SHORT",
    "PROVENANCE_FLAT",
    "PROVENANCE_MOOD",
    "PROVENANCE_USUAL",
    "USUAL_LINE",
    "for_member",
    "leaned",
    "no_profile",
    "no_pull",
    "no_round",
    "person_conflict",
    "provenance",
    "usual",
]
