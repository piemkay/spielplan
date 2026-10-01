"""Tonight's mood space: a few directions computed from the candidate pool's DNA (§6.2 step 4).

Each title is centred on the pool and scaled by its spread, skipping the terms it does not carry
(decision 218). The directions are the pool's principal ones, which damps a rare term, each scaled so
the pool's projections on it have unit spread. Pure: plain dicts in, numpy out.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from spielplan.tonight import round as round_rules

# Below this a term's spread is float zero: it carries nothing and must not be divided by.
MIN_SPREAD = 1e-9

Vector = Mapping[str, float]


@dataclass(frozen=True)
class Frame:
    """The pool's mean and spread per term, frozen for the session so answers stay comparable."""

    mean: dict[str, float]
    spread: dict[str, float]


def _matrix(pool_dna: Mapping[int, Vector]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """The pool as (terms, weights, carried): absence is a zero weight and a False."""
    terms = sorted({t for vec in pool_dna.values() for t in vec})
    col = {t: i for i, t in enumerate(terms)}
    weights = np.zeros((len(pool_dna), len(terms)))
    carried = np.zeros((len(pool_dna), len(terms)), dtype=bool)
    for r, vec in enumerate(pool_dna.values()):
        for t, w in vec.items():
            weights[r, col[t]] = w
            carried[r, col[t]] = True
    return terms, weights, carried


def _frame_of(terms: list[str], weights: np.ndarray) -> Frame:
    if not terms:
        return Frame(mean={}, spread={})
    return Frame(
        mean=dict(zip(terms, weights.mean(axis=0).tolist(), strict=True)),
        spread=dict(zip(terms, weights.std(axis=0).tolist(), strict=True)),
    )


def frame(pool_dna: Mapping[int, Vector]) -> Frame:
    """The mean and spread of every term across the candidate pool.

    Absence is a zero here (the pool's distribution), unlike in `centred`, where an absent term
    gets no coordinate (decision 218).
    """
    terms, weights, _ = _matrix(pool_dna)
    return _frame_of(terms, weights)


def centred(vec: Vector, f: Frame) -> dict[str, float]:
    """One title as its deviation from tonight's pool, in units of the pool's own spread.

    A term the vector does not carry gets no coordinate (decision 218), so an untagged title sits at
    the pool's centre.
    """
    out = {}
    for t, m in f.mean.items():
        s = f.spread.get(t, 0.0)
        if s <= MIN_SPREAD or t not in vec:
            continue
        out[t] = (vec[t] - m) / s
    return out


@dataclass(frozen=True, eq=False)
class Space:
    """The mood's directions over the pool's terms: `axes[i, k]` is `terms[i]`'s weight on direction k."""

    frame: Frame
    terms: tuple[str, ...]
    axes: np.ndarray

    def project(self, vec: Vector) -> tuple[float, ...]:
        """Where one title sits on each direction, from its centred vector."""
        index = {t: i for i, t in enumerate(self.terms)}
        x = np.zeros(len(self.terms))
        for t, v in centred(vec, self.frame).items():
            if t in index:
                x[index[t]] = v
        return tuple(float(v) for v in x @ self.axes)


def back(terms: Sequence[str], axes: np.ndarray, mean: np.ndarray) -> dict[str, float]:
    """A mood as term weights, what it projects back onto the vocabulary: their dot with a title's
    centred vector is the mood's adjustment of that title. Stored as `session_participant.tilt`."""
    weights = np.asarray(axes, dtype=float) @ np.asarray(mean, dtype=float)
    return {t: float(w) for t, w in zip(terms, weights, strict=True) if w != 0.0}


def space(pool_dna: Mapping[int, Vector], *, k: int = round_rules.MOOD_DIRECTIONS) -> Space:
    """Tonight's `k` mood directions from the pool's centred term vectors."""
    terms, weights, carried = _matrix(pool_dna)
    f = _frame_of(terms, weights)
    if not terms:
        return Space(frame=f, terms=(), axes=np.zeros((0, k)))
    spread = weights.std(axis=0)
    varies = spread > MIN_SPREAD
    kept = [t for t, keep in zip(terms, varies, strict=True) if keep]
    axes = np.zeros((len(kept), k))
    if kept:
        w = weights[:, varies]
        x = np.where(carried[:, varies], (w - w.mean(axis=0)) / spread[varies], 0.0)
        # The principal directions of the centred vectors; a title still projects from its own
        # vector, so one that carries nothing sits at zero on every direction.
        _, singular, vt = np.linalg.svd(x - x.mean(axis=0), full_matrices=False)
        sd = singular / math.sqrt(x.shape[0])
        for i in range(min(k, singular.size)):
            if sd[i] > MIN_SPREAD:
                axes[:, i] = vt[i] / sd[i]
    return Space(frame=f, terms=tuple(kept), axes=axes)


__all__ = [
    "Frame",
    "MIN_SPREAD",
    "Space",
    "back",
    "centred",
    "frame",
    "space",
]
