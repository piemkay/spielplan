"""The mood tilt. Spec v2.1 §6.2 steps 4-5, §0 row 4, §4.2 `session_participant.tilt`.

    "Each vote also yields a tilt observation — chosen-minus-rejected DNA, **centred on the
     candidate-pool mean** (the measured centring lever)."

§0 row 4 is the whole reason this module is three functions rather than none: the stored 8-axis
mood profile is worth **0.000** for choose-tonight, while **3 shortlist-anchored answers ≈
+0.088 AUC**, and "centring on the shortlist matters more than question form". The centring is
the lever; the questionnaire around it was deleted.

WHY "CENTRED" HAD TO BECOME "STANDARDISED", AND WHY THAT IS NOT A LIBERTY. For a difference,
additive centring cancels exactly:

    (a − m) − (b − m) = a − b

so "chosen-minus-rejected, centred on the pool mean", read as subtraction alone, is a no-op on
every A/B answer — the pool cannot reach the tilt at all, and the thing §0 measured at +0.088
would be indistinguishable from the thing it measured at 0.000. The coverage row states the
requirement the spec's wording drops: "the identical answer on the identical pair produces a
different tilt when the surrounding pool differs". Centring on the pool's mean **and scaling by
the pool's own spread** is the reading under which every word of §6.2 holds and that requirement
is met: the same choice between the same two films is a stronger statement in a pool where they
are the extremes than in one where they are unremarkable. Filed as a v2.2 spec defect.

§4.1 RULES 1 AND 2. This module never reads the database; it is handed DNA vectors that the
caller has already read through the sanctioned `dna_tagged` view, carrying their tier. Salience
and confidence reach it only as *weights* inside those vectors — there is no predicate here,
and no threshold, so a 0.5 cut that would delete 44% of the extracted tier is unrepresentable
rather than merely discouraged.

THE SHAPE IS jsonb. §4.2 stores the accumulated tilt on `session_participant.tilt`, so
everything here is plain dicts of `str -> float` and never a numpy array.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

# A facet the pool does not vary on carries no information about tonight, and dividing by its
# spread would be an infinity rather than an insight. The floor is not a tuned constant: it is
# the point below which a spread is indistinguishable from zero in float arithmetic.
MIN_SPREAD = 1e-9

Vector = Mapping[str, float]


@dataclass(frozen=True)
class Frame:
    """The pool's own frame of reference: what an average candidate looks like tonight, and how
    much tonight's candidates differ from each other.

    Built once per session from the candidate pool and carried, because §6.2 step 6 says
    nothing re-ranks within the evening — a frame that drifted as answers arrived would make
    the same answer mean different things at pair 3 and pair 13.
    """

    mean: dict[str, float]
    spread: dict[str, float]


def frame(pool_dna: Mapping[int, Vector]) -> Frame:
    """The mean and spread of every term across the candidate pool.

    A term absent from a candidate is a zero, not a gap: "this film is not cosy" is information
    about the pool, and treating absence as missing would make the mean a statement about the
    subset that happens to carry the term.
    """
    terms: set[str] = set()
    for vec in pool_dna.values():
        terms |= set(vec)
    n = len(pool_dna) or 1

    mean = {t: sum(v.get(t, 0.0) for v in pool_dna.values()) / n for t in terms}
    spread = {}
    for t in terms:
        var = sum((v.get(t, 0.0) - mean[t]) ** 2 for v in pool_dna.values()) / n
        spread[t] = math.sqrt(var)
    return Frame(mean=mean, spread=spread)


def centred(vec: Vector, f: Frame) -> dict[str, float]:
    """One candidate as its deviation from tonight's pool, in units of the pool's own spread.

    A candidate sitting exactly at the pool mean on a facet is a zero there, which is what
    makes "this film is unremarkable tonight" and "this film is not in the pool" the same
    statement — as they should be, for a tilt that is about tonight and not about the library.
    """
    out = {}
    for t, m in f.mean.items():
        s = f.spread.get(t, 0.0)
        if s <= MIN_SPREAD:
            continue
        out[t] = (vec.get(t, 0.0) - m) / s
    return out


def _accumulate(tilt: Mapping[str, float], delta: Mapping[str, float]) -> dict[str, float]:
    out = dict(tilt)
    for term, value in delta.items():
        out[term] = out.get(term, 0.0) + value
    return out


def observe(
    tilt: Mapping[str, float], *, chosen: Vector, rejected: Vector, f: Frame | None = None,
    frame: Frame | None = None,
) -> dict[str, float]:
    """§6.2 step 5's separating answer: chosen minus rejected, both in the pool's frame.

    `frame` is the keyword the callers use; `f` is accepted so the module reads naturally from
    inside itself. Exactly one is required.
    """
    ref = frame if frame is not None else f
    if ref is None:
        raise TypeError("observe() needs the session's pool frame")
    a, b = centred(chosen, ref), centred(rejected, ref)
    delta = {t: a.get(t, 0.0) - b.get(t, 0.0) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


def observe_level(
    tilt: Mapping[str, float], *, first: Vector, second: Vector, toward: bool,
    f: Frame | None = None, frame: Frame | None = None,
) -> dict[str, float]:
    """Decision 154's two level answers, reaching the tilt.

    `either` (`toward=True`) says *both of these would do*, which is a statement about a region
    of DNA space rather than about an ordering; `neither` says the opposite of it. Both are the
    sum of the two centred vectors, signed — so a pair sitting at the pool's own centre carries
    no direction, which is exactly right: "either of these two unremarkable films" is not a mood.
    """
    ref = frame if frame is not None else f
    if ref is None:
        raise TypeError("observe_level() needs the session's pool frame")
    a, b = centred(first, ref), centred(second, ref)
    sign = 1.0 if toward else -1.0
    delta = {t: sign * (a.get(t, 0.0) + b.get(t, 0.0)) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


def adjustment(tilt: Mapping[str, float], vec: Vector, f: Frame) -> float:
    """What this participant's tilt adds to one candidate's tonight score.

    The inner product of the tilt with the candidate's **centred** vector — the same frame on
    both sides. Against a raw vector it would measure a candidate's absolute DNA rather than its
    position in tonight's pool, which is the un-centred version §0 row 4 measured at 0.000.

    Divided by the number of terms the frame carries, so a pool described by forty terms does
    not out-shout one described by four. An empty tilt is exactly zero, which is what 54f's
    "ranked by the personal Ledger with **no tilt**" depends on.
    """
    if not tilt:
        return 0.0
    centred_vec = centred(vec, f)
    total = sum(weight * centred_vec.get(term, 0.0) for term, weight in tilt.items())
    return total / max(len(f.mean), 1)


def moved_facets(tilt: Mapping[str, float], *, top: int = 3) -> list[tuple[str, float]]:
    """The terms the round actually moved, strongest first — what §6.7's rail and any
    "leaning in / leaning away" chip would name. Ordering only; no threshold, because a
    threshold on a weight is what §4.1 rule 2 forbids."""
    return sorted(tilt.items(), key=lambda kv: -abs(kv[1]))[:top]


__all__ = [
    "Frame",
    "MIN_SPREAD",
    "adjustment",
    "centred",
    "frame",
    "moved_facets",
    "observe",
    "observe_level",
]
