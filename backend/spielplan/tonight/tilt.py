"""The mood tilt: chosen-minus-rejected DNA centred on the candidate pool (§6.2 step 4, §0 row 4).

Centred AND scaled by the pool's spread: for a difference, centring alone cancels exactly. Pure,
plain dicts (stored as jsonb on `session_participant.tilt`); weights only, never a threshold.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

# Decision 154's answers live in `round`, a one-way edge (it imports nothing from this package).
from spielplan.tonight import round as round_rules

# Below this a facet's spread is float zero: it carries nothing and must not be divided by.
MIN_SPREAD = 1e-9

Vector = Mapping[str, float]


@dataclass(frozen=True)
class Frame:
    """The pool's mean and spread per term, frozen for the session so answers stay comparable."""

    mean: dict[str, float]
    spread: dict[str, float]


def frame(pool_dna: Mapping[int, Vector]) -> Frame:
    """The mean and spread of every term across the candidate pool.

    Absence is a zero here (the pool's distribution), unlike in `centred`, where an absent term
    gets no coordinate (decision 218).
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

    A term the vector does not carry gets no coordinate (decision 218), so an untagged title
    adjusts by exactly 0.0.
    """
    out = {}
    for t, m in f.mean.items():
        s = f.spread.get(t, 0.0)
        if s <= MIN_SPREAD:
            continue
        if t not in vec:
            continue
        out[t] = (vec[t] - m) / s
    return out


def _accumulate(tilt: Mapping[str, float], delta: Mapping[str, float]) -> dict[str, float]:
    out = dict(tilt)
    for term, value in delta.items():
        out[term] = out.get(term, 0.0) + value
    return out


def observe(
    tilt: Mapping[str, float], *, chosen: Vector, rejected: Vector, frame: Frame
) -> dict[str, float]:
    """§6.2 step 4's separating answer: chosen minus rejected in the pool's frame."""
    a, b = centred(chosen, frame), centred(rejected, frame)
    delta = {t: a.get(t, 0.0) - b.get(t, 0.0) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


def observe_level(
    tilt: Mapping[str, float], *, first: Vector, second: Vector, toward: bool, frame: Frame
) -> dict[str, float]:
    """Decision 154's level answers: `either` adds both centred vectors, `neither` subtracts them."""
    a, b = centred(first, frame), centred(second, frame)
    sign = 1.0 if toward else -1.0
    delta = {t: sign * (a.get(t, 0.0) + b.get(t, 0.0)) for t in set(a) | set(b)}
    return _accumulate(tilt, delta)


# --- one answer, applied ---------------------------------------------------------------------


def applies(vectors: Mapping[int, Vector], *, title_a: int, title_b: int) -> bool:
    """Whether a stored answer still names two distinct candidates of tonight's pool (§10).

    The tilt and `round.replay` must skip the same rows, and solo counts N answers with it.
    """
    return title_a != title_b and title_a in vectors and title_b in vectors


def applied(
    tilt: Mapping[str, float],
    *,
    answer: str,
    title_a: int,
    title_b: int,
    vectors: Mapping[int, Vector],
    frame: Frame,
) -> dict[str, float]:
    """§6.2 step 4's observation for one answer, whichever of decision 154's four it is.

    The one dispatch for all three callers; the membership check is inside it.
    """
    if not applies(vectors, title_a=title_a, title_b=title_b):
        return dict(tilt)
    a_dna, b_dna = vectors[title_a], vectors[title_b]
    if answer == round_rules.A:
        return observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
    if answer == round_rules.B:
        return observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
    return observe_level(
        tilt, first=a_dna, second=b_dna, frame=frame, toward=answer == round_rules.EITHER,
    )


def adjustment(tilt: Mapping[str, float], vec: Vector, f: Frame) -> float:
    """What this participant's tilt adds to one candidate's tonight score.

    The tilt against the candidate's centred vector, over the frame's term count; an empty tilt
    is exactly zero.
    """
    if not tilt:
        return 0.0
    centred_vec = centred(vec, f)
    total = sum(weight * centred_vec.get(term, 0.0) for term, weight in tilt.items())
    return total / max(len(f.mean), 1)


__all__ = [
    "Frame",
    "MIN_SPREAD",
    "adjustment",
    "applied",
    "applies",
    "centred",
    "frame",
    "observe",
    "observe_level",
]
