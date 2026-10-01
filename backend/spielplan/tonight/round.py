"""§6.2 step 4's round (decisions 539, 550): pairs of a seat's own liked films, asked to learn
tonight's mood.

The mood is a Gaussian over the pool's few directions (`tilt.Space`). `A`/`B` is a probit on the
chosen-minus-rejected projection, `EITHER` says the difference does not matter, `NEITHER` that tonight
lies away from both. §13's guard: a hold-out answer moves nothing, and neither selection nor stopping
reads it. Pure and seeded.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

# --- the four answers (decisions 154 and 539) ---------------------------------------------

A = "A"
B = "B"
EITHER = "EITHER"
NEITHER = "NEITHER"
ANSWERS: tuple[str, ...] = (A, B, EITHER, NEITHER)

# `session_answer.selection`'s two values (0013's CHECK), spelled as the column spells them.
SELECTION_ADAPTIVE = "adaptive"
SELECTION_HOLDOUT = "uniform_holdout"

# §14 risk 6 wants the rate of each.
CONVERGED = "converged"
CAP = "cap"
ESCAPE = "escape"
END_REASONS: tuple[str, ...] = (CONVERGED, CAP, ESCAPE)

# Decision 550's numbers; every one a starting value the household's evenings may move.
CAP_PAIRS = 8
ESCAPE_FROM_PAIR = 4
TYPICAL_PAIRS = 5
MIN_ROUND_FILMS = 8
PAIR_STEP = 1
PAIR_RUNTIME_MIN = 30
MOOD_DIRECTIONS = 5
TILT_REACH = 30
WELL_KNOWN_CROWD = 30000
# §13's "one pair in ten", drawn as a rate from a stable key (`is_holdout`, decision 223).
HOLDOUT_EVERY = 10

# The stop rule: at least this many adaptive answers, and the top three unchanged by each of the last two.
SHORTLIST_SIZE = 3
STOP_AFTER = 3
STOP_HELD = 2
# The sealed draw picks among pairs within this share of the best expected information.
NEAR_BEST = 0.9

# The mood's prior per direction, and one answer's noise, on the rank-normal scale (decision 477).
# A wider prior reads stronger moods and runs longer rounds.
MOOD_PRIOR_SD = 0.35
ANSWER_NOISE = 1.0

# The round's version, frozen with the pool so a room started under another rule is refused.
ROUND_MARKER = "seen_pairs_v1"


class EscapeTooEarly(Exception):
    """An escape before pair 4: refused, not ignored."""


@dataclass(frozen=True)
class Film:
    """One film a seat may be asked about: `step` on their ladder (None for a guest), and where it sits
    on tonight's mood directions."""

    title_id: int
    step: int | None
    runtime_min: int | None
    z: tuple[float, ...]


@dataclass(frozen=True)
class Pair:
    title_a: int
    title_b: int
    selection: str
    reason: str

    def public(self) -> dict[str, object]:
        """The pair as a device sees it; `selection` travels for §13, sealed by the server."""
        return {
            "title_a": self.title_a,
            "title_b": self.title_b,
            "selection": self.selection,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Answered:
    """One `session_answer` row, as the replay needs it."""

    seq: int
    title_a: int
    title_b: int
    answer: str
    selection: str = SELECTION_ADAPTIVE


@dataclass(frozen=True, eq=False)
class Mood:
    """What the round believes about tonight: a mean and covariance over the mood directions."""

    mean: np.ndarray
    cov: np.ndarray


@dataclass(frozen=True, eq=False)
class Round:
    """Everything a replay produces: the mood, how many answers moved it, what to ask next, and
    whether to."""

    mood: Mood
    answered: int
    adaptive: int
    next_pair: Pair | None
    stop_reason: str | None


# --- the mood --------------------------------------------------------------------------------


def prior(k: int = MOOD_DIRECTIONS) -> Mood:
    return Mood(mean=np.zeros(k), cov=np.eye(k) * MOOD_PRIOR_SD**2)


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _probit(mood: Mood, x: np.ndarray) -> Mood:
    """The moment-matched posterior after observing `mood . x > 0` through `ANSWER_NOISE`."""
    sx = mood.cov @ x
    s2 = float(x @ sx) + ANSWER_NOISE**2
    s = math.sqrt(s2)
    t = float(mood.mean @ x) / s
    denom = _Phi(t)
    # At large negative `t`, `Phi` underflows; the limit `v = -t` avoids a NaN.
    v = -t if denom < 1e-12 else _phi(t) / denom
    w = min(max(v * (v + t), 0.0), 1.0)
    return Mood(mean=mood.mean + sx * (v / s), cov=_sym(mood.cov - np.outer(sx, sx) * (w / s2)))


def _level(mood: Mood, x: np.ndarray) -> Mood:
    """The posterior after observing `mood . x` near zero, through `ANSWER_NOISE`."""
    sx = mood.cov @ x
    s2 = float(x @ sx) + ANSWER_NOISE**2
    return Mood(
        mean=mood.mean - sx * (float(mood.mean @ x) / s2),
        cov=_sym(mood.cov - np.outer(sx, sx) / s2),
    )


def _sym(cov: np.ndarray) -> np.ndarray:
    return (cov + cov.T) / 2.0


def observe(mood: Mood, *, a: Sequence[float], b: Sequence[float], answer: str) -> Mood:
    """One answer about films at `a` and `b`. A side is the film nearer tonight; EITHER says their
    difference does not matter; NEITHER that tonight lies away from both."""
    if answer not in ANSWERS:
        raise ValueError(f"{answer!r} is not one of {ANSWERS}")
    za, zb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if answer == A:
        return _probit(mood, za - zb)
    if answer == B:
        return _probit(mood, zb - za)
    if answer == EITHER:
        return _level(mood, za - zb)
    return _probit(mood, -(za + zb) / 2.0)


# --- what the mood does to the candidates ------------------------------------------------------


def adjustments(mean: np.ndarray, reach: Mapping[int, Sequence[float]]) -> dict[int, float]:
    """What the mood adds to each candidate within reach."""
    return {t: float(np.dot(mean, z)) for t, z in reach.items()}


def tonight(
    stable: Mapping[int, float], reach: Mapping[int, Sequence[float]], mean: np.ndarray
) -> dict[int, float]:
    """A seat's tonight scores (decision 550): the mood re-ranks only its reach, by stable taste plus
    the mood, onto the reach's own stable values; every other candidate keeps its own."""
    adjust = adjustments(mean, reach)
    within = [t for t in reach if t in stable]
    order = sorted(within, key=lambda t: (-(stable[t] + adjust[t]), t))
    values = sorted((stable[t] for t in within), reverse=True)
    out = dict(stable)
    out.update(zip(order, values, strict=True))
    return out


def top_three(
    stable: Mapping[int, float], reach: Mapping[int, Sequence[float]], mean: np.ndarray
) -> frozenset[int]:
    scores = tonight(stable, reach, mean)
    return frozenset(sorted(scores, key=lambda t: (-scores[t], t))[:SHORTLIST_SIZE])


# --- which pairs may be asked ------------------------------------------------------------------


def has_round(films: Sequence[Film]) -> bool:
    """Too little to ask about, no round (decision 539): the seat ends at once with no tilt."""
    return len(films) >= MIN_ROUND_FILMS


def _askable(
    films: Sequence[Film], shown: Sequence[frozenset[int]]
) -> tuple[list[Film], np.ndarray, np.ndarray]:
    """`askable` as index pairs into the films in id order, over every pair at once: a guest's
    well-known films run to hundreds. A pair holds level at most one step apart (members) and
    within 30 minutes of runtime, both known."""
    ordered = sorted(films, key=lambda f: f.title_id)
    n = len(ordered)
    i, j = np.triu_indices(n, 1)
    if not i.size:
        return ordered, i, j
    ids = np.asarray([f.title_id for f in ordered], dtype=np.int64)
    steps = np.asarray([np.nan if f.step is None else f.step for f in ordered], dtype=float)
    runtime = np.asarray([np.nan if f.runtime_min is None else f.runtime_min for f in ordered])
    # NaN compares false: an unknown step holds level, an unknown runtime does not.
    ok = ~(np.abs(steps[i] - steps[j]) > PAIR_STEP)
    ok &= np.abs(runtime[i] - runtime[j]) <= PAIR_RUNTIME_MIN
    span = int(ids.max()) + 1
    asked = [min(p) * span + max(p) for p in shown if len(p) == 2]
    ok &= ~np.isin(ids[i] * span + ids[j], np.asarray(asked, dtype=np.int64))
    times = np.zeros(n, dtype=np.int64)
    row = {int(t): k for k, t in enumerate(ids)}
    for pair in shown:
        for t in pair:
            if t in row:
                times[row[t]] += 1
    load = times[i] + times[j]
    if ok.any():
        ok &= load == load[ok].min()
    return ordered, i[ok], j[ok]


def askable(films: Sequence[Film], shown: Sequence[frozenset[int]]) -> list[tuple[int, int]]:
    """Level pairs not yet shown this evening, of the films shown least: a film returns with a new
    partner only once every film that can still be paired has been shown (decision 550)."""
    ordered, i, j = _askable(films, shown)
    return [(ordered[a].title_id, ordered[b].title_id) for a, b in zip(i, j, strict=True)]


def is_holdout(seq: int, *, key: str | None) -> bool:
    """§13's "one pair in ten", as a rate drawn from a stable key (decision 223); never in solo (key None).

    The key is the seat id, never client-supplied; `random.Random(str)`, never the salted `hash()`.
    """
    if key is None:
        return False
    return random.Random(f"{key}:{seq}").random() < 1.0 / HOLDOUT_EVERY


def _oriented(a: Film, b: Film, rng: random.Random) -> tuple[int, int]:
    """Which film stands on the left is drawn too, so neither side is the lower id's."""
    return (a.title_id, b.title_id) if rng.random() < 0.5 else (b.title_id, a.title_id)


def select(
    films: Sequence[Film],
    mood: Mood,
    *,
    shown: Sequence[frozenset[int]],
    seq: int,
    rng: random.Random,
    holdout_key: str | None,
) -> Pair | None:
    """The next pair, and the arm that drew it. The hold-out arm is decided first and never falls back;
    it is uniform over the askable pairs. The adaptive arm draws among the pairs whose projected
    difference has near the largest predictive variance: the most expected information."""
    ordered, i, j = _askable(films, shown)
    if not i.size:
        return None
    if is_holdout(seq, key=holdout_key):
        k = rng.randrange(i.size)
        a, b = _oriented(ordered[i[k]], ordered[j[k]], rng)
        return Pair(
            title_a=a, title_b=b, selection=SELECTION_HOLDOUT,
            reason="uniform-random, held out - this pair never steers tonight's picks",
        )
    z = np.asarray([f.z for f in ordered], dtype=float)
    d = z[i] - z[j]
    gain = np.maximum(np.einsum("pk,kl,pl->p", d, mood.cov, d), 0.0)
    near = np.flatnonzero(gain >= NEAR_BEST * float(gain.max()))
    k = int(near[rng.randrange(near.size)])
    a, b = _oriented(ordered[i[k]], ordered[j[k]], rng)
    return Pair(
        title_a=a, title_b=b, selection=SELECTION_ADAPTIVE,
        reason="the pair that would tell the most about tonight's mood",
    )


# `replay`'s `select` flag shadows this name there; a test observing its call patches `_select_pair`.
_select_pair = select


# --- stopping, the cap, and the escape ---------------------------------------------------------


def escape_available(answered: int) -> bool:
    """From pair 4: a seat answering its Nth pair has N-1 behind it."""
    return answered >= ESCAPE_FROM_PAIR - 1


def escape(*, answered: int) -> str:
    if not escape_available(answered):
        # The 409's message: named by the control's label (decision 486).
        raise EscapeTooEarly(
            f'"just pick for us" opens at pair {ESCAPE_FROM_PAIR}; {answered} answered so far'
        )
    return ESCAPE


def stop_reason(tops: Sequence[frozenset[int]], *, answered: int) -> str | None:
    """`tops` is the top three before any answer, then after each adaptive one. Convergence first, so
    the cap is never misreported."""
    adaptive = len(tops) - 1
    if adaptive >= STOP_AFTER and len(set(tops[-(STOP_HELD + 1):])) == 1:
        return CONVERGED
    if answered >= CAP_PAIRS:
        return CAP
    return None


# --- replay ----------------------------------------------------------------------------------


def replay(
    films: Sequence[Film],
    stable: Mapping[int, float],
    reach: Mapping[int, Sequence[float]],
    answers: Sequence[Answered],
    *,
    holdout_key: str | None,
    rng: random.Random | None = None,
    escaped: bool = False,
    select: bool = True,
) -> Round:
    """The whole round from its rows. Every row counts toward the cap; a hold-out, or a row naming a
    film the seat cannot be asked about (§10), moves nothing. `select=False` skips only the draw."""
    by_id = {f.title_id: f for f in films}
    mood = prior()
    tops = [top_three(stable, reach, mood.mean)]
    shown: list[frozenset[int]] = []
    for row in sorted(answers, key=lambda x: x.seq):
        shown.append(frozenset({row.title_a, row.title_b}))
        if row.selection == SELECTION_HOLDOUT or row.title_a == row.title_b:
            continue
        if row.title_a not in by_id or row.title_b not in by_id:
            continue
        mood = observe(mood, a=by_id[row.title_a].z, b=by_id[row.title_b].z, answer=row.answer)
        tops.append(top_three(stable, reach, mood.mean))

    count = len(answers)
    if not has_round(films):
        reason: str | None = CONVERGED
    else:
        reason = ESCAPE if escaped else stop_reason(tops, answered=count)
    nxt = (
        None
        if reason or not select
        else _select_pair(films, mood, shown=shown, seq=count + 1, rng=rng or random.Random(0),
                          holdout_key=holdout_key)
    )
    if select and reason is None and nxt is None:
        # Sooner only when no level pair is left: recorded as `cap`, the nearest ending.
        reason = CAP
    return Round(
        mood=mood, answered=count, adaptive=len(tops) - 1, next_pair=nxt, stop_reason=reason
    )


__all__ = [
    "A",
    "ANSWERS",
    "ANSWER_NOISE",
    "Answered",
    "B",
    "CAP",
    "CAP_PAIRS",
    "CONVERGED",
    "END_REASONS",
    "ESCAPE",
    "ESCAPE_FROM_PAIR",
    "EITHER",
    "EscapeTooEarly",
    "Film",
    "HOLDOUT_EVERY",
    "MIN_ROUND_FILMS",
    "MOOD_DIRECTIONS",
    "MOOD_PRIOR_SD",
    "Mood",
    "NEAR_BEST",
    "NEITHER",
    "PAIR_RUNTIME_MIN",
    "PAIR_STEP",
    "Pair",
    "ROUND_MARKER",
    "Round",
    "SELECTION_ADAPTIVE",
    "SELECTION_HOLDOUT",
    "SHORTLIST_SIZE",
    "STOP_AFTER",
    "STOP_HELD",
    "TILT_REACH",
    "TYPICAL_PAIRS",
    "WELL_KNOWN_CROWD",
    "adjustments",
    "askable",
    "escape",
    "escape_available",
    "has_round",
    "is_holdout",
    "observe",
    "prior",
    "replay",
    "select",
    "stop_reason",
    "tonight",
    "top_three",
]
