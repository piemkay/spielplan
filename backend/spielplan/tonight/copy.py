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

# The honest negative §6.2 step 7 quotes verbatim, for a participant no term pulls toward.
NO_PULL_LINE = "nothing here is their pull — {term} works against them"

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


def no_pull(term: str) -> str:
    return NO_PULL_LINE.format(term=term)


def no_profile(name: str) -> str:
    return NO_PROFILE_LINE.format(name=name)


__all__ = [
    "D_LINE",
    "NO_PROFILE_LINE",
    "NO_PULL_LINE",
    "SPLIT_LINE",
    "bounded",
    "conflict",
    "no_profile",
    "no_pull",
    "overclaims",
    "split_line",
]
