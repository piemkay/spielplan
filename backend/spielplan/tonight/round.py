"""§6.2 step 4's adaptive round (54b/54c): a Gaussian posterior per candidate, pure and seeded.

`either`/`neither` are scored against a virtual opponent at the pool median (decision 154). §13's
guard: `replay` drops held-out answers before any belief moves; the hold-out arm never falls back.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

# --- decision 154's four answers ----------------------------------------------------------

A = "A"
B = "B"
EITHER = "EITHER"
NEITHER = "NEITHER"
ANSWERS: tuple[str, ...] = (A, B, EITHER, NEITHER)

# `session_answer.selection`'s two values (0013's CHECK), spelled as the column spells them.
SELECTION_ADAPTIVE = "adaptive"
SELECTION_HOLDOUT = "uniform_holdout"

# 54c's three endings; §14 risk 6 wants the rate of each.
CONVERGED = "converged"
CAP = "cap"
ESCAPE = "escape"
END_REASONS: tuple[str, ...] = (CONVERGED, CAP, ESCAPE)

# 54c's own numbers, not §4.3 tunables.
CAP_PAIRS = 20
ESCAPE_FROM_PAIR = 6
# 54b's "one pair in ten", drawn as a rate from a stable key (`is_holdout`, decision 223).
HOLDOUT_EVERY = 10

# 54d: the boundary is the cut between rank 3 and rank 4.
SHORTLIST_SIZE = 3

# The straddle width in sigmas, on the rank-standardised scale (decision 477); calibrated by the
# sweep `test_tonight_round.py` pins. Not §6.3's `straddle_z`: a different scale (decision 214).
BOUNDARY_Z = 0.6

# What a still-answering seat is told to expect: that sweep's film-night median, not the cap.
TYPICAL_PAIRS = 10

# The noise of one answer on the tonight-score scale; a mood question, so not small.
BETA = 0.5

# A guest's flat prior is this much wider, so their round runs "a little longer" (54c).
GUEST_VAR_FACTOR = 4.0


class EscapeTooEarly(Exception):
    """54c's escape before the round has learned anything: refused, not ignored."""


@dataclass(frozen=True)
class Belief:
    """One candidate's tonight score as a posterior; `var` is what makes "straddles" answerable."""

    mu: float
    var: float


@dataclass(frozen=True)
class Pair:
    title_a: int
    title_b: int
    selection: str
    reason: str

    def public(self) -> dict[str, object]:
        """The pair as a device sees it; `selection` travels for §13, sealed by the server (54b)."""
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


@dataclass(frozen=True)
class Round:
    """Everything a replay produces: the posterior, what is still unresolved, what to ask
    next, and whether to ask at all."""

    beliefs: dict[int, Belief]
    answered: int
    straddlers: frozenset[int]
    next_pair: Pair | None
    stop_reason: str | None


# --- the prior -----------------------------------------------------------------------------


def initial(
    pool_scores: Mapping[int, float], *, prior_var: float = 1.0, has_profile: bool = True
) -> dict[int, Belief]:
    """The posterior a round starts from: a member's own score, or for a guest the pool's own
    order at `GUEST_VAR_FACTOR` times the variance (54c), never the host's Ledger.

    `prior_var` stays 1.0, the pool's variance by construction (decision 477); narrowing it was
    measured and is not a lever (decision 214).
    """
    var = prior_var * (GUEST_VAR_FACTOR if not has_profile else 1.0)
    return {t: Belief(mu=float(s), var=var) for t, s in pool_scores.items()}


def anchor_of(beliefs: Mapping[int, Belief]) -> float:
    """The virtual opponent for `either`/`neither`: the pool median, robust to a runaway favourite."""
    if not beliefs:
        return 0.0
    mus = sorted(b.mu for b in beliefs.values())
    mid = len(mus) // 2
    return mus[mid] if len(mus) % 2 else (mus[mid - 1] + mus[mid]) / 2.0


# --- the boundary --------------------------------------------------------------------------


def boundary(beliefs: Mapping[int, Belief]) -> float | None:
    """The shortlist boundary between rank 3 and rank 4 (54d), or None for a pool of three or fewer."""
    if len(beliefs) <= SHORTLIST_SIZE:
        return None
    mus = sorted((b.mu for b in beliefs.values()), reverse=True)
    return (mus[SHORTLIST_SIZE - 1] + mus[SHORTLIST_SIZE]) / 2.0


def straddles(belief: Belief, cut: float, *, z: float = BOUNDARY_Z) -> bool:
    return abs(belief.mu - cut) < z * math.sqrt(max(belief.var, 0.0))


def straddlers(beliefs: Mapping[int, Belief], *, z: float = BOUNDARY_Z) -> set[int]:
    """Who the round still cannot place either side of the cut; one predicate for selection and
    stopping, at BOUNDARY_Z rather than §6.3's scale (decision 214)."""
    cut = boundary(beliefs)
    if cut is None:
        return set()
    return {t for t, b in beliefs.items() if straddles(b, cut, z=z)}


# --- the update ----------------------------------------------------------------------------


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _v_w(t: float) -> tuple[float, float]:
    """The truncated-Gaussian moment-match pair (`v`, `w = v(v + t)`).

    At large negative `t`, `Phi` underflows; the limit `v = -t` avoids a NaN.
    """
    denom = _Phi(t)
    v = -t if denom < 1e-12 else _phi(t) / denom
    w = v * (v + t)
    return v, min(max(w, 0.0), 1.0)


def _duel(winner: Belief, loser: Belief) -> tuple[Belief, Belief]:
    c2 = winner.var + loser.var + BETA * BETA
    c = math.sqrt(c2)
    t = (winner.mu - loser.mu) / c
    v, w = _v_w(t)
    return (
        Belief(mu=winner.mu + winner.var / c * v, var=winner.var * (1.0 - winner.var / c2 * w)),
        Belief(mu=loser.mu - loser.var / c * v, var=loser.var * (1.0 - loser.var / c2 * w)),
    )


def _against_anchor(belief: Belief, anchor: float, *, wins: bool) -> Belief:
    """One candidate against the virtual opponent. The anchor has no variance of its own and
    never updates: it is a reference level, not a competitor."""
    fixed = Belief(mu=anchor, var=0.0)
    if wins:
        moved, _ = _duel(belief, fixed)
    else:
        _, moved = _duel(fixed, belief)
    return moved


def update(
    beliefs: Mapping[int, Belief], *, title_a: int, title_b: int, answer: str, anchor: float
) -> dict[int, Belief]:
    """One answer, applied (decision 154); returns a new mapping and touches only the two titles.

    A/B separate the pair; EITHER lifts both against the anchor, NEITHER lowers both.
    """
    if answer not in ANSWERS:
        raise ValueError(f"{answer!r} is not one of {ANSWERS}")
    out = dict(beliefs)
    a, b = out[title_a], out[title_b]
    if answer == A:
        out[title_a], out[title_b] = _duel(a, b)
    elif answer == B:
        out[title_b], out[title_a] = _duel(b, a)
    else:
        wins = answer == EITHER
        out[title_a] = _against_anchor(a, anchor, wins=wins)
        out[title_b] = _against_anchor(b, anchor, wins=wins)
    return out


# --- stopping, the cap, and the escape -----------------------------------------------------


def escape_available(answered: int) -> bool:
    """54c: "From the sixth pair a persistent 'just pick for us'…". A participant answering
    their Nth pair has N-1 behind them, so the control appears once five are answered."""
    return answered >= ESCAPE_FROM_PAIR - 1


def escape(*, answered: int) -> str:
    if not escape_available(answered):
        # The 409's message: named by the control's label, not 54c's word (decision 486).
        raise EscapeTooEarly(
            f'"just pick for us" opens at pair {ESCAPE_FROM_PAIR}; {answered} answered so far'
        )
    return ESCAPE


def stop_reason(
    beliefs: Mapping[int, Belief], *, answered: int, z: float = BOUNDARY_Z
) -> str | None:
    """Why this round is over, or None; convergence first, so `cap` is never misreported."""
    if not straddlers(beliefs, z=z):
        return CONVERGED
    if answered >= CAP_PAIRS:
        return CAP
    return None


# --- selection -----------------------------------------------------------------------------


def is_holdout(seq: int, *, key: str) -> bool:
    """54b's "one pair in ten", as a rate of 1/HOLDOUT_EVERY drawn from a stable key (decision 223).

    A rate, not a schedule, so rounds ended early still carry hold-outs. The key is the seat id or
    `user.id` for solo, never client-supplied; `random.Random(str)`, never the salted `hash()`.
    """
    return random.Random(f"{key}:{seq}").random() < 1.0 / HOLDOUT_EVERY


def _answer_probabilities(a: Belief, b: Belief, anchor: float) -> dict[str, float]:
    """What the model expects a participant to answer: A/B by the pairwise Gaussian, the rest
    split into `either`/`neither` by level against the anchor (decision 154)."""
    c = math.sqrt(a.var + b.var + BETA * BETA)
    p_a = _Phi((a.mu - b.mu) / c)
    # The "both" mass shrinks as the pair separates.
    both = math.exp(-abs(a.mu - b.mu) / max(c, 1e-9))
    level = (a.mu + b.mu) / 2.0 - anchor
    p_either = both * _Phi(level / max(c, 1e-9))
    p_neither = both * (1.0 - _Phi(level / max(c, 1e-9)))
    p_a_only = (1.0 - both) * p_a
    p_b_only = (1.0 - both) * (1.0 - p_a)
    total = p_a_only + p_b_only + p_either + p_neither
    if total <= 0:
        return {A: 0.25, B: 0.25, EITHER: 0.25, NEITHER: 0.25}
    return {
        A: p_a_only / total, B: p_b_only / total,
        EITHER: p_either / total, NEITHER: p_neither / total,
    }


def expected_straddlers(
    beliefs: Mapping[int, Belief], *, title_a: int, title_b: int, anchor: float, z: float
) -> float:
    """54c's selection objective: expected straddlers after this pair, over the four answers."""
    probs = _answer_probabilities(beliefs[title_a], beliefs[title_b], anchor)
    total = 0.0
    for answer, p in probs.items():
        if p <= 0.0:
            continue
        after = update(beliefs, title_a=title_a, title_b=title_b, answer=answer, anchor=anchor)
        total += p * len(straddlers(after, z=z))
    return total


def _holdout(pool: Sequence[int], rng: random.Random) -> Pair | None:
    """54b: "drawn uniformly at random from the candidate pool", over the whole pool's pairs."""
    n = len(pool)
    if n < 2:
        return None
    i = rng.randrange(n)
    j = rng.randrange(n - 1)
    if j >= i:
        j += 1
    return Pair(
        title_a=pool[i], title_b=pool[j],
        selection=SELECTION_HOLDOUT,
        reason="uniform-random, held out — this pair never tunes tonight's shortlist",
    )

# --- selection: 54c's expectation, evaluated for every pair at once -------------------------
#
# Serves the same pair as the scalar `expected_straddlers` (the equivalence tests assert it). An
# answer moves two beliefs, so the cut stays among the top six and straddlers are counted by
# `searchsorted`; the moved titles are removed under the same rewrite they were counted with.

_ERF = np.frompyfunc(math.erf, 1, 1)
_SQRT2 = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)

# Pairs per block, bounding peak memory by this number rather than by pool size; unobservable.
_PAIR_BLOCK = 1 << 15


def _phi_many(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / _SQRT2PI


def _Phi_many(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _ERF(x / _SQRT2).astype(np.float64))


def _v_w_many(t: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`_v_w`, elementwise, also returning its denominator `Phi(t)`: `p_a` for the same `t`."""
    denom = _Phi_many(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = _phi_many(t) / denom
    v = np.where(denom < 1e-12, -t, ratio)
    w = v * (v + t)
    return v, np.clip(w, 0.0, 1.0), denom


@dataclass(frozen=True, eq=False)
class _Board:
    """One `select` call's beliefs as the pair search reads them; `ids` ascending."""

    ids: np.ndarray             # int64[n], ascending title ids
    row: dict[int, int]
    mu: np.ndarray              # float64[n]
    var: np.ndarray             # float64[n]
    reach: np.ndarray           # float64[n] — z*sigma, the scalar's own straddle expression
    lo_row: np.ndarray          # float64[n] — mu-reach, in row order
    hi_row: np.ndarray          # float64[n] — mu+reach, in row order
    can: np.ndarray             # bool[n] — the rows whose interval survives rounding
    lo: np.ndarray              # sorted lo_row over `can`
    hi: np.ndarray              # sorted hi_row over `can`
    pin: np.ndarray             # sorted mu over the rows whose interval collapsed to a point
    top: np.ndarray             # the (up to six) rows with the largest mu
    top_mu: np.ndarray
    either_mu: np.ndarray
    either_var: np.ndarray
    neither_mu: np.ndarray
    neither_var: np.ndarray
    anchor: float
    z: float
    live: bool                  # False when the pool is no larger than the shortlist

    @property
    def n(self) -> int:
        return int(self.ids.size)

    @classmethod
    def of(cls, beliefs: Mapping[int, Belief], pool: Sequence[int], *, anchor: float, z: float) -> _Board:
        n = len(pool)
        ids = np.asarray(pool, dtype=np.int64)
        mu = np.fromiter((beliefs[t].mu for t in pool), dtype=np.float64, count=n)
        var = np.fromiter((beliefs[t].var for t in pool), dtype=np.float64, count=n)
        reach = z * np.sqrt(np.maximum(var, 0.0))
        lo_row, hi_row = mu - reach, mu + reach
        # Three disjoint sets, so every row contributes 0 or 1: `can` (interval survives rounding),
        # `pin` (reach under half an ulp: straddles only at equality), and the rest (never).
        can = lo_row < hi_row
        pin = (reach > 0.0) & ~can
        top = np.argsort(-mu, kind="stable")[: min(n, SHORTLIST_SIZE + 3)]

        # `either`/`neither` against the anchor: the moved belief depends on the title alone.
        c2 = var + BETA * BETA
        c = np.sqrt(c2)
        v_up, w_up, _ = _v_w_many((mu - anchor) / c)
        v_dn, w_dn, _ = _v_w_many((anchor - mu) / c)
        return cls(
            ids=ids,
            row={int(t): i for i, t in enumerate(pool)},
            mu=mu,
            var=var,
            reach=reach,
            lo_row=lo_row,
            hi_row=hi_row,
            can=can,
            lo=np.sort(lo_row[can]),
            hi=np.sort(hi_row[can]),
            pin=np.sort(mu[pin]),
            top=top,
            top_mu=mu[top],
            either_mu=mu + var / c * v_up,
            either_var=var * (1.0 - var / c2 * w_up),
            neither_mu=mu - var / c * v_dn,
            neither_var=var * (1.0 - var / c2 * w_dn),
            anchor=anchor,
            z=z,
            live=n > SHORTLIST_SIZE,
        )

    def _cut(self, ia: np.ndarray, ib: np.ndarray, mu_a: np.ndarray, mu_b: np.ndarray) -> np.ndarray:
        """`boundary()` after the two substitutions, from the top six plus the two moved mus."""
        k = self.top.size
        cand = np.empty((ia.size, k + 2), dtype=np.float64)
        struck = (self.top[None, :] == ia[:, None]) | (self.top[None, :] == ib[:, None])
        cand[:, :k] = np.where(struck, -np.inf, self.top_mu[None, :])
        cand[:, k] = mu_a
        cand[:, k + 1] = mu_b
        cand.sort(axis=1)
        return (cand[:, k - 1] + cand[:, k - 2]) / 2.0

    def _count(
        self, ia: np.ndarray, ib: np.ndarray,
        mu_a: np.ndarray, var_a: np.ndarray, mu_b: np.ndarray, var_b: np.ndarray,
    ) -> np.ndarray:
        """`len(straddlers(after))` for one answer, over every pair at once."""
        cut = self._cut(ia, ib, mu_a, mu_b)
        # The whole board by interval, plus pinned rows, which straddle only when the cut IS their mean.
        base = (
            np.searchsorted(self.lo, cut, side="left")
            - np.searchsorted(self.hi, cut, side="right")
        )
        if self.pin.size:
            base = base + (
                np.searchsorted(self.pin, cut, side="right")
                - np.searchsorted(self.pin, cut, side="left")
            )
        # Remove `a` and `b` in `base`'s own arithmetic, under their old intervals.
        was = self._contributes(ia, cut) + self._contributes(ib, cut)
        # Re-add them under the new beliefs with `straddles()`' own expression.
        now = (np.abs(mu_a - cut) < self.z * np.sqrt(np.maximum(var_a, 0.0))).astype(np.int64)
        now += (np.abs(mu_b - cut) < self.z * np.sqrt(np.maximum(var_b, 0.0))).astype(np.int64)
        return base - was + now

    def _contributes(self, rows: np.ndarray, cut: np.ndarray) -> np.ndarray:
        """What `base` counted for these rows, in `base`'s own arithmetic."""
        can = self.can[rows]
        interval = can & (self.lo_row[rows] < cut) & (self.hi_row[rows] > cut)
        pinned = ~can & (self.reach[rows] > 0.0) & (self.mu[rows] == cut)
        return (interval | pinned).astype(np.int64)

    def _block(self, ia: np.ndarray, ib: np.ndarray) -> np.ndarray:
        mu, var = self.mu, self.var
        ma, mb, va, vb = mu[ia], mu[ib], var[ia], var[ib]

        # One `c` and one `t` serve A, B and `p_a`: the ±0.0 asymmetry is invisible to exp and erf.
        c2 = va + vb + BETA * BETA
        c = np.sqrt(c2)
        t = (ma - mb) / c
        v_p, w_p, p_a = _v_w_many(t)
        v_m, w_m, _ = _v_w_many(-t)

        # `_answer_probabilities`, verbatim and in its order, with `p_a` from the duel above.
        cg = np.maximum(c, 1e-9)
        both = np.exp(-np.abs(ma - mb) / cg)
        level = (ma + mb) / 2.0 - self.anchor
        phi_level = _Phi_many(level / cg)
        p_either = both * phi_level
        p_neither = both * (1.0 - phi_level)
        p_a_only = (1.0 - both) * p_a
        p_b_only = (1.0 - both) * (1.0 - p_a)
        total = p_a_only + p_b_only + p_either + p_neither
        with np.errstate(divide="ignore", invalid="ignore"):
            probs = (
                np.where(total <= 0.0, 0.25, p_a_only / total),
                np.where(total <= 0.0, 0.25, p_b_only / total),
                np.where(total <= 0.0, 0.25, p_either / total),
                np.where(total <= 0.0, 0.25, p_neither / total),
            )

        moved = (
            (ma + va / c * v_p, va * (1.0 - va / c2 * w_p),
             mb - vb / c * v_p, vb * (1.0 - vb / c2 * w_p)),
            (ma - va / c * v_m, va * (1.0 - va / c2 * w_m),
             mb + vb / c * v_m, vb * (1.0 - vb / c2 * w_m)),
            (self.either_mu[ia], self.either_var[ia], self.either_mu[ib], self.either_var[ib]),
            (self.neither_mu[ia], self.neither_var[ia], self.neither_mu[ib], self.neither_var[ib]),
        )

        # Summed in `probs`' own order and skipping the same terms, because the scalar's
        # accumulator is a Python float and float addition is not associative.
        out = np.zeros(ia.size, dtype=np.float64)
        for p, (mu_a, var_a, mu_b, var_b) in zip(probs, moved, strict=True):
            count = self._count(ia, ib, mu_a, var_a, mu_b, var_b)
            out = out + np.where(p <= 0.0, 0.0, p * count)
        return out

    def expected(self, ia: np.ndarray, ib: np.ndarray) -> np.ndarray:
        """54c's expectation for every `(ia, ib)` pair, as `expected_straddlers` gives it."""
        if not self.live:
            return np.zeros(ia.size, dtype=np.float64)
        if ia.size <= _PAIR_BLOCK:
            return self._block(ia, ib)
        out = np.empty(ia.size, dtype=np.float64)
        for start in range(0, ia.size, _PAIR_BLOCK):
            end = start + _PAIR_BLOCK
            out[start:end] = self._block(ia[start:end], ib[start:end])
        return out


def select(
    beliefs: Mapping[int, Belief],
    *,
    seq: int,
    rng: random.Random,
    holdout_key: str,
    z: float = BOUNDARY_Z,
    asked: Iterable[frozenset[int]] | None = None,
) -> Pair | None:
    """The next pair, and the arm that produced it.

    The hold-out arm is decided first and never falls back; `holdout_key` has no default so seats
    do not share an arm. `asked` pairs are not re-served (§13's reliability inflation).
    """
    pool = sorted(beliefs)
    if len(pool) < 2:
        return None
    if is_holdout(seq, key=holdout_key):
        return _holdout(pool, rng)

    already = set(asked or ())
    anchor = anchor_of(beliefs)
    board = _Board.of(beliefs, pool, anchor=anchor, z=z)
    n = board.n
    unresolved = np.asarray(
        [board.row[t] for t in sorted(straddlers(beliefs, z=z))], dtype=np.int64
    )
    # An answered pair as `i*n + j`; one naming a title no longer in the pool (§10) blocks nothing.
    blocked = np.asarray(
        [
            board.row[min(p)] * n + board.row[max(p)]
            for p in already
            if len(p) == 2 and min(p) in board.row and max(p) in board.row
        ],
        dtype=np.int64,
    )

    def _best(pairs: tuple[np.ndarray, np.ndarray]) -> tuple[float, int, int] | None:
        ia, ib = pairs
        if ia.size and blocked.size:
            keep = ~np.isin(ia * n + ib, blocked)
            ia, ib = ia[keep], ib[keep]
        if not ia.size:
            return None
        expected = board.expected(ia, ib)
        # Rounded so equal information is a real tie; `np.round` only narrows
        # the field and the tie is decided with the scalar's own `round`.
        approx = np.round(expected, 9)
        near = np.flatnonzero(approx <= approx.min() + 1.5e-9)
        uniq, back = np.unique(expected[near], return_inverse=True)
        rounded = np.asarray([round(float(v), 9) for v in uniq])[back]
        best = rounded.min()
        tie = near[rounded == best]
        a_ids, b_ids = board.ids[ia[tie]], board.ids[ib[tie]]
        # The smallest (a, b) wins a tie.
        k = int(np.lexsort((b_ids, a_ids))[0])
        return (float(best), int(a_ids[k]), int(b_ids[k]))

    def _within(xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        i, j = np.triu_indices(xs.size, 1)
        return xs[i], xs[j]

    def _touching(xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        left = np.repeat(xs, n)
        right = np.tile(np.arange(n, dtype=np.int64), xs.size)
        keep = left != right
        left, right = left[keep], right[keep]
        # Two straddlers name each other twice; the scalar evaluates the repeat and keeps the
        # same winner, so collapsing it here changes the cost and not the answer.
        key = np.unique(np.minimum(left, right) * n + np.maximum(left, right))
        return key // n, key % n

    # Narrowest first: between straddlers (54c as written), then a straddler against anyone once
    # those pairs are answered, then anything when nothing straddles.
    best = _best(_within(unresolved)) if unresolved.size >= 2 else None
    if best is None and unresolved.size:
        best = _best(_touching(unresolved))
    if best is None:
        best = _best(_within(np.arange(n, dtype=np.int64)))
    if best is None:
        return None
    _, a, b = best
    return Pair(
        title_a=a, title_b=b,
        selection=SELECTION_ADAPTIVE,
        reason="the pair that would settle the most of tonight's shortlist",
    )


# `replay`'s `select` flag shadows this name there; a test observing its call patches `_select_pair`.
_select_pair = select


# --- replay --------------------------------------------------------------------------------


def replay(
    pool_scores: Mapping[int, float],
    answers: Sequence[Answered],
    *,
    holdout_key: str,
    z: float = BOUNDARY_Z,
    prior_var: float = 1.0,
    has_profile: bool = True,
    rng: random.Random | None = None,
    escaped: bool = False,
    select: bool = True,
) -> Round:
    """The whole round, from the pool and the rows.

    §13's guard lives here: hold-out answers move no belief (they still count toward the cap).
    `select=False` replays the beliefs without the pair search, for solo (54f).
    """
    beliefs = initial(pool_scores, prior_var=prior_var, has_profile=has_profile)
    for answered_row in sorted(answers, key=lambda x: x.seq):
        if answered_row.selection == SELECTION_HOLDOUT:
            continue
        if (
            answered_row.title_a == answered_row.title_b
            or answered_row.title_a not in beliefs
            or answered_row.title_b not in beliefs
        ):
            # §10 can move the pool under a stored answer, and one title named twice compares nothing.
            continue
        beliefs = update(
            beliefs,
            title_a=answered_row.title_a,
            title_b=answered_row.title_b,
            answer=answered_row.answer,
            anchor=anchor_of(beliefs),
        )

    count = len(answers)
    reason = ESCAPE if escaped else stop_reason(beliefs, answered=count, z=z)
    unresolved = frozenset(straddlers(beliefs, z=z))
    # Adaptive answers only: a hold-out may not steer selection either (54b).
    asked = {
        frozenset({x.title_a, x.title_b})
        for x in answers
        if x.selection != SELECTION_HOLDOUT
    }
    nxt = (
        None
        if reason or not select
        else _select_pair(beliefs, seq=count + 1, rng=rng or random.Random(0),
                          holdout_key=holdout_key, z=z, asked=asked)
    )
    if select and reason is None and nxt is None:
        # Out of distinct pairs with the boundary unresolved: recorded as `cap`, 54g's nearest ending.
        reason = CAP
    return Round(
        beliefs=beliefs, answered=count, straddlers=unresolved,
        next_pair=nxt, stop_reason=reason,
    )


__all__ = [
    "A",
    "ANSWERS",
    "B",
    "Answered",
    "BETA",
    "BOUNDARY_Z",
    "Belief",
    "CAP",
    "CAP_PAIRS",
    "CONVERGED",
    "END_REASONS",
    "ESCAPE",
    "ESCAPE_FROM_PAIR",
    "EITHER",
    "EscapeTooEarly",
    "GUEST_VAR_FACTOR",
    "HOLDOUT_EVERY",
    "NEITHER",
    "Pair",
    "Round",
    "SELECTION_ADAPTIVE",
    "SELECTION_HOLDOUT",
    "SHORTLIST_SIZE",
    "TYPICAL_PAIRS",
    "anchor_of",
    "boundary",
    "escape",
    "escape_available",
    "expected_straddlers",
    "initial",
    "is_holdout",
    "replay",
    "select",
    "stop_reason",
    "straddles",
    "straddlers",
    "update",
]
