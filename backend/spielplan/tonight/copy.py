"""Tonight's strings, and the one that is a hard rule rather than a preference.

Spec v2.1 §6.2 step 5 (rewritten, 54d), §6.5, §6.8, §0 row 3; DNA_MODEL §5.3 by pointer.

§6.2 step 5, twice over — once in v2.1 and again in the rewrite:

    "Conflict copy obeys the measured constraint (DNA_MODEL §5.3): D predicts 'one of you is
     likely to land below your usual tonight' (AUC 0.610), never 'someone will hate this' — a
     hard rule on the §6.6 conflict-phrasing LLM task."

THE RULE IS A BOUND ON A CLAIM, NOT A STYLE GUIDE. AUC 0.610 is a weak signal — better than a
coin and nowhere near a prediction about a person's feelings. "One of you will land below your
usual" is what 0.610 supports; "someone will hate this" is a different and unmeasured claim
wearing the same number. §6.6 assigns the phrasing to an LLM, and an LLM handed "explain why
these two disagree" will reach for the stronger sentence every time, because it reads better.
So the bound is enforced here, on the way out, rather than requested in a prompt: `bounded()`
takes whatever the model produced and returns the sanctioned string in its place if the
phrasing over-claims. A prompt is a request; this is the guarantee.

§6.5 carries the same rule for the Divisive tab ("divergence copy predicts a relatively worse
night, never active hate"), which is why the bound lives in a module either surface can call
rather than inside the Tonight combine.
"""

from __future__ import annotations

import re

# §6.2 step 5's own sentence, and 54d's second half. Both quoted rather than paraphrased: the
# spec fixes this copy, and a paraphrase is where the over-claim creeps back in.
SPLIT_LINE = "You're split on {facet} — here's one of each. The axis is zeroed, not averaged."

# The one thing D is licensed to say. `{d}` is rendered in the data voice (§6.8: model numbers
# appear next to their name, never bare).
D_LINE = "D {d:.2f} — one of you is likely to land below your usual tonight."

# The same sentence in the member register (decision 486): D is a model number, and a member with
# Show the model off is told what it predicts and never the number. `for_member` below swaps it in
# at read time, so a stored conflict written with the number still reads plainly.
D_LINE_PLAIN = "One of you is likely to land below your usual tonight."

# Decision 479's headline for a split with no axis to name: the alternative in hand is a person's,
# and the slate is built so the sentence is true. Fixed like SPLIT_LINE, never a model's.
PERSON_SPLIT_LINE = "You're pulling different ways tonight — here's one for each of you."
# And the same headline without the promise, for the room the three slots could not serve (four or
# more seats pulling apart): the split is still surfaced, and nothing is claimed that is false.
PERSON_SPLIT_SHORT = "You're pulling different ways tonight."

# SPLIT_LINE's second sentence is model vocabulary ("axis", "zeroed"), so a member without Show
# the model reads the first sentence alone (decision 486; §6.2 step 5's copy as decision 479
# amends it). Kept verbatim in the stored row, which is the record of what the spec fixed.
_MODEL_SENTENCE = " The axis is zeroed, not averaged."

# The honest negative §6.2 step 7 quotes verbatim, for a participant no term pulls toward.
NO_PULL_LINE = "nothing here is their pull — {term} works against them"

# §6.2 step 7's pull lines, in plain words. "pulls Patrick with pulp + escapist" read as jargon on
# the second household evening: "pulls ... with" is the round's own verb and "+" is notation. Each
# sentence says only what its branch in `play._match_lines` establishes: the first that this
# person's own answers tonight leaned toward terms the title carries, the second that the title
# sits at or above the middle of this person's own order tonight, i.e. not below their usual.
LEANED_LINE = "{name} leaned toward {terms} tonight"
USUAL_LINE = "suits {name}'s usual taste — {terms}"

# A guest with no grid profile gets a line rather than being silently omitted (§6.2 step 7:
# every participant gets a match line).
NO_PROFILE_LINE = "{name} — no profile yet"

# Phrasings that predict a feeling rather than a relative outcome. Deliberately about the
# CLAIM and not about politeness: "you may find this slow" is fine, "Jenny will hate this" is
# not, and the difference is whether the sentence asserts something AUC 0.610 cannot support.
_OVERCLAIM = re.compile(
    r"\b("
    r"hate[sd]?|hating|loathe[sd]?|despise[sd]?|detest[sd]?|"
    r"dislike[sd]?|disliking|"
    r"can'?t stand|won'?t (?:like|enjoy|want|stand)|will not (?:like|enjoy)|"
    r"ruin(?:s|ed)?|miserable|resent[sd]?|"
    r"awful|terrible|unbearable"
    r")\b",
    re.IGNORECASE,
)


def overclaims(phrase: str) -> bool:
    """Does this sentence assert more than D's measured power?

    Substring-safe by construction: the pattern is word-bounded, so "Cathartic" is not "hate"
    and "The Hateful Eight" — an actual title — is not a prediction, because the check is
    applied to the *explanation*, never to a title.
    """
    return bool(_OVERCLAIM.search(phrase or ""))


def bounded(phrase: str | None, *, d: float) -> str:
    """The line a participant actually sees.

    An empty or over-claiming candidate is replaced, not edited: editing a sentence to remove
    the word "hate" leaves the sentence that wanted to say it, and the row's requirement is
    that the sanctioned string is "emitted in its place".
    """
    if phrase and not overclaims(phrase):
        return phrase
    return D_LINE.format(d=d)


def split_line(facet: str) -> str:
    return SPLIT_LINE.format(facet=facet)


def conflict(facet: str, *, d: float, phrasing: str | None = None) -> dict[str, object]:
    """Everything a surfaced split says, as one object.

    `headline` is fixed by §6.2 and never comes from a model; only `explanation` is the §6.6
    LLM task's output, and it passes through `bounded` on the way out. Keeping them apart is
    what stops a model rewriting the sentence the spec fixed.
    """
    return {
        "facet": facet,
        "d": round(float(d), 4),
        "headline": split_line(facet),
        "explanation": bounded(phrasing, d=d),
    }


def person_conflict(
    *, d: float, phrasing: str | None = None, one_for_each: bool
) -> dict[str, object]:
    """Decision 479's surfaced split, in `conflict`'s shape. `facet` is None because no axis is
    named; `by` says which of the two splits this is, so a reader never infers it from the copy."""
    return {
        "facet": None,
        "by": "person",
        "d": round(float(d), 4),
        "headline": PERSON_SPLIT_LINE if one_for_each else PERSON_SPLIT_SHORT,
        "explanation": bounded(phrasing, d=d),
    }


def for_member(conflict: dict[str, object] | None) -> dict[str, object] | None:
    """A stored conflict as a member with Show the model off may read it (decision 486).

    Server-side, where the payload is built: the number `d` leaves the payload, the D line becomes
    its plain sentence, and the axis split's second sentence goes. A model phrasing that survived
    `bounded` is the household's own sentence and passes through untouched.
    """
    if not conflict:
        return conflict
    plain = {k: v for k, v in conflict.items() if k != "d"}
    explanation = str(plain.get("explanation") or "")
    if re.fullmatch(r"D -?\d+\.\d+ — one of you is likely to land below your usual tonight\.",
                    explanation):
        plain["explanation"] = D_LINE_PLAIN
    headline = str(plain.get("headline") or "")
    plain["headline"] = headline.replace(_MODEL_SENTENCE, "")
    return plain


def no_pull(term: str, *, name: str | None = None) -> str:
    """§6.2 step 7's honest negative, verbatim, and — when there is more than one person on the
    card — whose it is. Two of the unnamed sentence on one winner card could not be told apart."""
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
    "bounded",
    "conflict",
    "for_member",
    "leaned",
    "no_profile",
    "no_pull",
    "overclaims",
    "person_conflict",
    "split_line",
    "usual",
]
