"""§6.3's comparison queue — the selector, and §13 stream (a)'s held-out arm.

Spec v2.1 §6.3, §13 stream (a), §0 row 6; proposals 73, 146, 157, decision 54b.

§6.3 gives the policy in one line: "boundary-targeted active selection (70% posterior-
straddling pairs / 20% exploration / 10% uniform-random held out for honest evaluation — the
adaptive-inflation guard)". Three arms and their shares are all it gives, so what a
*boundary-targeted pair* actually is had to be decided; the reasoning is under each arm.

Pure, and seeded, for the same reason `rate/battle.py` is: "over a long draw the shares are
70/20/10" is a claim about a distribution, and a distribution is measured by drawing from it
twenty thousand times, which is not a thing to do through Postgres.

THE GUARD IS THE POINT OF THE MODULE. §13: "the 10% uniform-random comparison stream is the
*only* data used to evaluate the tier model — adaptively-selected pairs inflate reliability
(measured effect; the guard is non-negotiable)." Two consequences live here:

  * **The held-out arm never receives a fallback.** When the boundary arm has nothing to draw
    it falls through to exploration, because a pool with no straddler is a pool where every
    comparison is exploratory. It must never fall through to the held-out arm: the evaluation
    stream's rate would then depend on the model's own confidence, which is precisely the
    coupling the guard exists to break. If neither adaptive arm can draw, there is nothing to
    sharpen and the queue says so.
  * **An arm is reported as the arm that drew it.** `Pair.arm` is what reaches
    `duel.selection` and the §6.7 log line, and a fallback says "exploration" because that is
    what happened. The prototype logged `boundary-targeted pair (70/20/10 policy)`
    unconditionally, including on the held-out tenth (proposal 120).

WHY THIS IS NOT §6.1's BATTLE. §0 row 6: "for *profiles*, no selection rule beats random (best
+0.0013, CI spans 0); for *ranking*, boundary-targeted selection does help". `rate/battle.py`
draws uniformly on purpose and this module does not, and the two must not be merged.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams
from spielplan.rank.board import Item, straddles

# The values that reach `duel.selection` (0005's CHECK) and §6.7's log line. `uniform_holdout`
# is spelled the way the column spells it — `observations.HELD_OUT` is the same string, and a
# second spelling here is how an exclusion silently stops matching.
ARM_BOUNDARY = "boundary"
ARM_EXPLORATION = "exploration"
ARM_HOLDOUT = "uniform_holdout"

# §6.3's mix, in the order the roll walks. Not tunable: these are the spec's own numbers, not
# constants the corpus project re-tunes offline, so §4.3 is not where they belong.
SHARES: tuple[tuple[str, float], ...] = (
    (ARM_BOUNDARY, 0.70),
    (ARM_EXPLORATION, 0.20),
    (ARM_HOLDOUT, 0.10),
)


@dataclass(frozen=True)
class Candidate:
    """One title the queue may draw. `comparisons` counts the duels it already carries **with
    the held-out ones excluded** — a selector that counted them would be reading the evaluation
    stream, which §13 forbids. The exclusion happens in the query; this is the contract."""

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
        """§6.3's pairs carry their reason in the data voice; the arm travels because
        proposal 146 requires the held-out stream to be identifiable end to end."""
        return {
            "title_a": self.title_a,
            "title_b": self.title_b,
            "arm": self.arm,
            "reason": self.reason,
        }


def eligible(
    items: Sequence[Item], *, cuts: np.ndarray, hp: Hyperparams
) -> list[Item]:
    """§6.3: "a straddling title shows "A/S" **and becomes queue-eligible**".

    The same `straddles()` the badge uses, deliberately — see its docstring. This is the
    *straddling* set, which is what the boundary arm draws from; the pool the held-out arm
    draws from is every rated title, because a uniform sample restricted to what the model is
    unsure about is not a uniform sample.
    """
    return [i for i in items if straddles(i, cuts=cuts, hp=hp) is not None]


def candidates(
    items: Sequence[Item],
    *,
    cuts: np.ndarray,
    tier_set: Sequence[str],
    hp: Hyperparams,
    comparisons: dict[int, int] | None = None,
) -> list[Candidate]:
    """Decorate the board's items with what the selector needs."""
    from spielplan.ledger import model

    counts = comparisons or {}
    cuts = np.asarray(cuts, dtype=float)
    out = []
    for item in items:
        out.append(
            Candidate(
                item=item,
                comparisons=int(counts.get(item.title_id, 0)),
                straddle=straddles(item, cuts=cuts, hp=hp),
                tier=int(model.tier_of(np.array([item.s]), cuts)[0]),
            )
        )
    return out


def _nearest(pool: Iterable[Candidate], anchor: Candidate) -> Candidate | None:
    """The closest title in `s`, excluding the anchor itself. Ties by id, so a draw is
    reproducible from its seed."""
    others = [c for c in pool if c.title_id != anchor.title_id]
    if not others:
        return None
    return min(others, key=lambda c: (abs(c.s - anchor.s), c.title_id))


def _boundary(pool: Sequence[Candidate], rng: random.Random) -> Pair | None:
    """70% — "posterior-straddling pairs".

    §6.3 gives the share and not the construction, so: a straddling title, paired with its
    nearest neighbour **in the tier its posterior reaches**. A straddler paired with an
    arbitrary partner would be a comparison about nothing in particular; the pair that settles
    a boundary is the one that spans it, and the nearest title across it is the one the answer
    is least predictable for and therefore most informative about.
    """
    straddlers = [c for c in pool if c.straddle is not None]
    rng.shuffle(straddlers)
    for anchor in straddlers:
        across = [c for c in pool if c.tier == anchor.straddle and c.title_id != anchor.title_id]
        partner = _nearest(across, anchor)
        if partner is not None:
            return Pair(
                title_a=anchor.title_id,
                title_b=partner.title_id,
                arm=ARM_BOUNDARY,
                reason="its posterior crosses this boundary",
            )
    return None


def _exploration(pool: Sequence[Candidate], rng: random.Random) -> Pair | None:
    """20% — "exploration".

    §6.3 names the share and nothing else. The arm that is *not* boundary-targeted and *not*
    uniform is the one that reduces uncertainty where no cutpoint is at stake: the title the
    person has compared least (ties broken by the widest posterior), against its nearest
    neighbour in `s`. It rotates on its own — answering increments both titles' counts, so the
    least-compared title is a different one next time.
    """
    away = [c for c in pool if c.straddle is None] or list(pool)
    if len(pool) < 2:
        return None
    fewest = min(c.comparisons for c in away)
    widest = [c for c in away if c.comparisons == fewest]
    # Fewest comparisons first, then the widest posterior — both are "where the model knows
    # least". An exact tie on both is broken by the draw rather than by id order, so a board
    # where everything is equally unexplored (a new one) does not serve the same pair forever.
    broadest = max(c.item.sigma for c in widest)
    anchor = rng.choice([c for c in widest if c.item.sigma == broadest])
    partner = _nearest(pool, anchor)
    if partner is None:
        return None
    return Pair(
        title_a=anchor.title_id,
        title_b=partner.title_id,
        arm=ARM_EXPLORATION,
        reason="the least-compared title on your board",
    )


def _holdout(pool: Sequence[Candidate], rng: random.Random) -> Pair | None:
    """10% — "uniform-random held out for honest evaluation".

    Uniform over unordered **pairs** of the whole eligible pool, not over any stratum and not
    over the straddling set: a sample restricted to what the model is unsure about is exactly
    the adaptive selection §13 is guarding against. Drawing an ordered pair uniformly and
    forgetting the order is uniform over unordered pairs, which is why it is done that way
    rather than by materialising n(n-1)/2 of them.
    """
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


def draw(pool: Sequence[Candidate], *, rng: random.Random) -> Pair | None:
    """One pair, and the arm that produced it.

    The roll picks an arm by §6.3's shares. A boundary roll on a pool with no straddler falls
    through to exploration and *says* exploration; nothing ever falls into or out of the
    held-out arm, because its rate is the one thing §13 needs to be independent of the model.
    """
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
        return _boundary(pool, rng) or _exploration(pool, rng)
    return _exploration(pool, rng)


__all__ = [
    "ARM_BOUNDARY",
    "ARM_EXPLORATION",
    "ARM_HOLDOUT",
    "Candidate",
    "Pair",
    "SHARES",
    "candidates",
    "draw",
    "eligible",
]
