"""§6.3's comparison queue: 70% boundary / 20% exploration / 10% uniform held out (§13). Pure, seeded.

The held-out arm never receives a fallback and stays uniform and memoryless; a fallback is reported as
the arm that drew. Not §6.1's battle, which draws uniformly on purpose (§0 row 6).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams
from spielplan.rank.board import Item, _placed, straddles

# `duel.selection` values (0005's CHECK); ARM_HOLDOUT must equal `observations.HELD_OUT`.
ARM_BOUNDARY = "boundary"
ARM_EXPLORATION = "exploration"
ARM_HOLDOUT = "uniform_holdout"

# §6.3's mix, in the order the roll walks: the spec's numbers, not a bundle constant.
SHARES: tuple[tuple[str, float], ...] = (
    (ARM_BOUNDARY, 0.70),
    (ARM_EXPLORATION, 0.20),
    (ARM_HOLDOUT, 0.10),
)

# Decision 494: the partner is the least-compared of the k nearest, so one title is not always drawn.
K_NEAREST = 5

# Decision 494: titles of the last N answered adaptive pairs go last. Soft, so a small board
# is never emptied.
RECENT_WINDOW = 3


@dataclass(frozen=True)
class Candidate:
    """One title the queue may draw. `comparisons` EXCLUDES held-out duels (§13), in the query."""

    item: Item
    comparisons: int = 0
    straddle: int | None = None
    tier: int = 0

    @property
    def title_id(self) -> int:
        return self.item.title_id

    @property
    def s(self) -> float:
        return self.item.s


@dataclass(frozen=True)
class Pair:
    title_a: int
    title_b: int
    arm: str
    reason: str

    def public(self) -> dict[str, object]:
        """The arm travels so the held-out stream is identifiable end to end (proposal 146)."""
        return {
            "title_a": self.title_a,
            "title_b": self.title_b,
            "arm": self.arm,
            "reason": self.reason,
        }


def eligible(
    items: Sequence[Item], *, cuts: np.ndarray, hp: Hyperparams
) -> list[Item]:
    """§6.3's straddling set, through the badge's own `straddles()`. The held-out pool is everything."""
    return [i for i in items if straddles(i, cuts=cuts, hp=hp) is not None]


def candidates(
    items: Sequence[Item],
    *,
    cuts: np.ndarray,
    tier_set: Sequence[str],
    hp: Hyperparams,
    comparisons: dict[int, int] | None = None,
) -> list[Candidate]:
    """Decorate the board's items with what the selector needs, via the board's `_placed`."""
    counts = comparisons or {}
    cuts = np.asarray(cuts, dtype=float)
    out = []
    for item in items:
        tier, straddle = _placed(item, cuts, hp)
        out.append(
            Candidate(
                item=item,
                comparisons=int(counts.get(item.title_id, 0)),
                straddle=straddle,
                tier=tier,
            )
        )
    return out


def _jitter(pool: Iterable[Candidate], rng: random.Random) -> dict[int, float]:
    """One tie-breaker per title from the draw's own generator: not by id, and still reproducible."""
    return {c.title_id: rng.random() for c in pool}


def _partner(
    options: Iterable[Candidate],
    anchor: Candidate,
    *,
    asked: set[frozenset[int]],
    recent: frozenset[int],
    jitter: dict[int, float],
) -> Candidate | None:
    """Decision 494's partner: of the `K_NEAREST` unasked titles nearest in `s`, the one outside the
    recent window, then the least-compared. An answered pair is never re-served (§13 inflation)."""
    fresh = [
        c
        for c in options
        if c.title_id != anchor.title_id
        and frozenset((anchor.title_id, c.title_id)) not in asked
    ]
    if not fresh:
        return None
    nearest = sorted(fresh, key=lambda c: (abs(c.s - anchor.s), jitter[c.title_id]))[:K_NEAREST]
    return min(
        nearest, key=lambda c: (c.title_id in recent, c.comparisons, jitter[c.title_id])
    )


def boundary_height(candidate: Candidate) -> int:
    """Decision 494's weight: the upper tier index of the boundary crossed (never 0), favouring the top."""
    return max(candidate.tier, int(candidate.straddle if candidate.straddle is not None else 0))


def _weighted_order(
    candidates: Sequence[Candidate], rng: random.Random, *, recent: frozenset[int]
) -> list[Candidate]:
    """The straddlers weighted by `boundary_height` without replacement (key u ** (1/w)), recent last."""
    keyed = [
        (c.title_id in recent, -(rng.random() ** (1.0 / boundary_height(c))), c)
        for c in candidates
    ]
    keyed.sort(key=lambda row: (row[0], row[1]))
    return [c for _in_recent, _key, c in keyed]


def _boundary(
    pool: Sequence[Candidate],
    rng: random.Random,
    *,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
) -> Pair | None:
    """70%: a straddling title against a near neighbour in the tier its posterior reaches."""
    already = {frozenset(p) for p in (asked or ())}
    held = frozenset(recent or ())
    jitter = _jitter(pool, rng)
    straddlers = [c for c in pool if c.straddle is not None]
    for anchor in _weighted_order(straddlers, rng, recent=held):
        across = [c for c in pool if c.tier == anchor.straddle]
        partner = _partner(across, anchor, asked=already, recent=held, jitter=jitter)
        if partner is not None:
            return Pair(
                title_a=anchor.title_id,
                title_b=partner.title_id,
                arm=ARM_BOUNDARY,
                reason="its posterior crosses this boundary",
            )
    return None


def _exploration(
    pool: Sequence[Candidate],
    rng: random.Random,
    *,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
) -> Pair | None:
    """20%: the least-compared title in the WHOLE pool against a near neighbour in `s`.

    Ties go to the higher tier (decision 494). Answered pairs are never re-served.
    """
    if len(pool) < 2:
        return None
    already = {frozenset(p) for p in (asked or ())}
    held = frozenset(recent or ())
    jitter = _jitter(pool, rng)
    # The shuffle breaks exact ties by the draw; `list.sort` is stable.
    order = list(pool)
    rng.shuffle(order)
    order.sort(key=lambda c: (c.comparisons, c.title_id in held, -c.tier))
    for anchor in order:
        partner = _partner(pool, anchor, asked=already, recent=held, jitter=jitter)
        if partner is not None:
            return Pair(
                title_a=anchor.title_id,
                title_b=partner.title_id,
                arm=ARM_EXPLORATION,
                reason="the least-compared title on your board",
            )
    return None


def _holdout(pool: Sequence[Candidate], rng: random.Random) -> Pair | None:
    """10%: uniform over unordered pairs of the whole pool (an ordered draw, order forgotten)."""
    n = len(pool)
    if n < 2:
        return None
    a = rng.randrange(n)
    b = rng.randrange(n - 1)
    if b >= a:
        b += 1
    return Pair(
        title_a=pool[a].title_id,
        title_b=pool[b].title_id,
        arm=ARM_HOLDOUT,
        reason="uniform-random, held out — this pair never tunes the model",
    )


def draw(
    pool: Sequence[Candidate],
    *,
    rng: random.Random,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
) -> Pair | None:
    """One pair, and the arm that produced it; None when neither adaptive arm has a pair left.

    Boundary falls through to exploration. `asked` and `recent` never reach the held-out arm.
    """
    already = {frozenset(p) for p in (asked or ())}
    held = frozenset(recent or ())
    if len(pool) < 2:
        return None
    roll = rng.random()
    cumulative = 0.0
    arm = SHARES[-1][0]
    for name, share in SHARES:
        cumulative += share
        if roll < cumulative:
            arm = name
            break

    if arm == ARM_HOLDOUT:
        return _holdout(pool, rng)
    if arm == ARM_BOUNDARY:
        return _boundary(pool, rng, asked=already, recent=held) or _exploration(
            pool, rng, asked=already, recent=held
        )
    return _exploration(pool, rng, asked=already, recent=held)


__all__ = [
    "ARM_BOUNDARY",
    "ARM_EXPLORATION",
    "ARM_HOLDOUT",
    "K_NEAREST",
    "RECENT_WINDOW",
    "Candidate",
    "Pair",
    "SHARES",
    "boundary_height",
    "candidates",
    "draw",
    "eligible",
]
