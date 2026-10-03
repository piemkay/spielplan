"""§6.3's comparison queue: 50% boundary / 25% cross-tier check / 15% exploration / 10% uniform held
out (§13). Pure, seeded.

The held-out arm never receives a fallback and stays uniform and memoryless; a fallback is reported as
the arm that drew.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import rescale_level
from spielplan.rank.board import Item, _placed, straddles

# `duel.selection` values (0051's CHECK); ARM_HOLDOUT must equal `observations.HELD_OUT`.
ARM_BOUNDARY = "boundary"
ARM_CROSS = "cross_tier"
ARM_EXPLORATION = "exploration"
ARM_HOLDOUT = "uniform_holdout"

# §6.3's mix, in the order the roll walks: the spec's numbers, not a bundle constant.
SHARES: tuple[tuple[str, float], ...] = (
    (ARM_BOUNDARY, 0.50),
    (ARM_CROSS, 0.25),
    (ARM_EXPLORATION, 0.15),
    (ARM_HOLDOUT, 0.10),
)

# Decision 494: the partner is the least-compared of the k nearest, so one title is not always drawn.
K_NEAREST = 5

# Decision 494: titles of the last N answered adaptive pairs go last. Soft, so a small board
# is never emptied.
RECENT_WINDOW = 3

# Decision 564: a title in this many of the last `EXPOSURE_WINDOW` adaptive pairs rests too.
EXPOSURE_WINDOW = 15
ROUND_CAP = 2

# Decision 564: a cross-tier partner is shown this many steps from the anchor or more; an anchor's
# weight grows by one every `STALE_DAYS` since its placement.
CROSS_MIN_GAP = 2
# Wider pairs (an S film against an E one) settle nothing the person does not already know.
CROSS_MAX_GAP = 3
STALE_DAYS = 30.0

# Decision 564: partners in the anchor's own top quarter by likeness are offered first.
SIMILAR_SHARE = 0.25

# §13 stream (b): about one Sharpen pair in ten poses again a pair answered three or more days ago.
REASK_RATE = 0.10
REASK_MIN_AGE = timedelta(days=3)
# Not in §13: without it a small board re-asks the same handful every sitting.
REASK_COOLDOWN = timedelta(days=90)
# The `duel.selection` a re-ask is stored under: no arm drew it.
ARM_REASK = "random"

# A pair's reason names its shared genre with the kind's noun (decision 550).
NOUNS = {"movie": "films", "series": "series"}


@dataclass(frozen=True)
class Candidate:
    """One title the queue may draw. `comparisons` and `answers_since` EXCLUDE held-out duels (§13), in
    the query. `genres` are the canonical genres, the rarest on this board first; `terms` maps a DNA
    term to `(idf, naming rank, label)`; `embedding` is the unit coordinate or None."""

    item: Item
    comparisons: int = 0
    straddle: int | None = None
    tier: int = 0
    genres: tuple[str, ...] = ()
    terms: Mapping[str, tuple[float, float, str]] = field(default_factory=dict, compare=False)
    embedding: np.ndarray | None = field(default=None, compare=False)
    stale_days: float = 0.0
    answers_since: int = 0

    @property
    def title_id(self) -> int:
        return self.item.title_id

    @property
    def s(self) -> float:
        return self.item.s

    @cached_property
    def term_norm(self) -> float:
        return math.sqrt(sum(v[0] ** 2 for v in self.terms.values()))

    @property
    def shown(self) -> int:
        """The step the board renders it in: the latest placement, else the model's tier."""
        return self.tier if self.item.assigned_tier is None else int(self.item.assigned_tier)


@dataclass(frozen=True)
class Pair:
    title_a: int
    title_b: int
    arm: str
    reason: str
    # The duel a §13(b) re-ask poses again; sealed, never sent.
    reask_of: int | None = None

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
    genres: dict[int, tuple[str, ...]] | None = None,
    terms: dict[int, Mapping[str, tuple[float, float, str]]] | None = None,
    embeddings: dict[int, np.ndarray] | None = None,
    placed: dict[int, tuple[float, int]] | None = None,
) -> list[Candidate]:
    """Decorate the board's items with what the selector needs, via the board's `_placed`. `placed`
    maps a title to (days since its latest placement, Sharpen answers since it)."""
    counts = comparisons or {}
    by_title = genres or {}
    carried = Counter(g for item in items for g in by_title.get(item.title_id, ()))
    cuts = np.asarray(cuts, dtype=float)
    out = []
    for item in items:
        tier, straddle = _placed(item, cuts, hp)
        vector = (embeddings or {}).get(item.title_id)
        norm = 0.0 if vector is None else float(np.linalg.norm(vector))
        stale, since = (placed or {}).get(item.title_id, (0.0, 0))
        out.append(
            Candidate(
                item=item,
                comparisons=int(counts.get(item.title_id, 0)),
                straddle=straddle,
                tier=tier,
                genres=tuple(sorted(by_title.get(item.title_id, ()), key=lambda g: (carried[g], g))),
                terms=(terms or {}).get(item.title_id, {}),
                embedding=None if norm == 0.0 else np.asarray(vector, dtype=float) / norm,
                stale_days=float(stale),
                answers_since=int(since),
            )
        )
    return out


def likeness(a: Candidate, b: Candidate) -> float:
    """Decision 564: the mean of the signals both titles carry: the idf-weighted DNA cosine (decision
    513), the coordinate cosine floored at 0, and the genre Jaccard. 0 when they share no signal."""
    signals = []
    if a.terms and b.terms:
        dot = sum(a.terms[t][0] ** 2 for t in a.terms.keys() & b.terms.keys())
        norm = a.term_norm * b.term_norm
        signals.append(dot / norm if norm else 0.0)
    if a.embedding is not None and b.embedding is not None:
        signals.append(max(float(a.embedding @ b.embedding), 0.0))
    if a.genres and b.genres:
        signals.append(len(set(a.genres) & set(b.genres)) / len(set(a.genres) | set(b.genres)))
    return sum(signals) / len(signals) if signals else 0.0


def _strongest_term(a: Candidate, b: Candidate) -> str | None:
    """The shared term with the highest idf times lesser naming rank, as `why.likeness` names it."""
    shared = a.terms.keys() & b.terms.keys()
    if not shared:
        return None
    best = min(shared, key=lambda t: (-a.terms[t][0] * min(a.terms[t][1], b.terms[t][1]), t))
    return a.terms[best][2]


def why(a: Candidate, b: Candidate, tier_set: Sequence[str], kind: str) -> str:
    """A pair's reason, the same form on every arm (decision 550): the steps the board shows the two
    in, lower first, then the strongest shared term and the rarest shared genre.
    "One in A, one in S · both science fiction films · slow-burn"."""
    low, high = sorted(rescale_level(c.shown, k_from=None, k_to=len(tier_set)) for c in (a, b))
    steps = (
        f"Both in {tier_set[low]}"
        if low == high
        else f"One in {tier_set[low]}, one in {tier_set[high]}"
    )
    genre = next((g.lower() for g in a.genres if g in b.genres), None)
    term = _strongest_term(a, b)
    if term and genre and term.lower() != genre:
        return f"{steps} · both {genre} {NOUNS[kind]} · {term}"
    if genre:
        return f"{steps} · both {genre} {NOUNS[kind]}"
    return f"{steps} · both {term}" if term else steps


def _jitter(pool: Iterable[Candidate], rng: random.Random) -> dict[int, float]:
    """One tie-breaker per title from the draw's own generator: not by id, and still reproducible."""
    return {c.title_id: rng.random() for c in pool}


def _resting(recent: Iterable[int] | None, exposure: Mapping[int, int] | None) -> frozenset[int]:
    """Decision 564's rest: in the newest pairs, or in `ROUND_CAP` of the last `EXPOSURE_WINDOW`."""
    capped = {t for t, n in (exposure or {}).items() if n >= ROUND_CAP}
    return frozenset(recent or ()) | capped


def _nearest(anchor: Candidate, jitter: dict[int, float]) -> Callable[[list[Candidate]], list]:
    """Decision 494's nearness: the `K_NEAREST` closest in `s`."""
    return lambda pool: sorted(pool, key=lambda c: (abs(c.s - anchor.s), jitter[c.title_id]))[
        :K_NEAREST
    ]


def _partner(
    pool: Sequence[Candidate],
    options: Iterable[Candidate],
    anchor: Candidate,
    *,
    asked: set[frozenset[int]],
    resting: frozenset[int],
    exposure: Mapping[int, int],
    jitter: dict[int, float],
    near: Callable[[list[Candidate]], list[Candidate]],
) -> Candidate | None:
    """The partner, in passes: alike and rested, then rested, then any unasked title (decision 564).
    Within a pass: the arm's `near`, then fewer recent appearances, fewer comparisons, more alike, the
    draw. An answered pair is never re-served (§13 inflation)."""
    fresh = [
        c
        for c in options
        if c.title_id != anchor.title_id
        and frozenset((anchor.title_id, c.title_id)) not in asked
    ]
    if not fresh:
        return None
    like = {c.title_id: likeness(anchor, c) for c in pool if c.title_id != anchor.title_id}
    ranked = sorted(like.values(), reverse=True)
    floor = max(ranked[max(math.ceil(len(ranked) * SIMILAR_SHARE) - 1, 0)], 1e-12)
    rested = [c for c in fresh if c.title_id not in resting]
    for offered in ([c for c in rested if like[c.title_id] >= floor], rested, fresh):
        if offered:
            return min(
                near(offered),
                key=lambda c: (
                    exposure.get(c.title_id, 0), c.comparisons, -like[c.title_id], jitter[c.title_id]
                ),
            )
    return None


def boundary_height(candidate: Candidate) -> int:
    """Decision 494's weight: the upper tier index of the boundary crossed (never 0), favouring the top."""
    return max(candidate.tier, int(candidate.straddle if candidate.straddle is not None else 0))


def cross_weight(candidate: Candidate) -> float:
    """Decision 564: a stale placement weighs more, one Sharpen has already checked weighs less."""
    return (1.0 + candidate.stale_days / STALE_DAYS) / (1.0 + candidate.answers_since)


def _weighted_order(
    candidates: Sequence[Candidate],
    rng: random.Random,
    *,
    resting: frozenset[int],
    weight: Callable[[Candidate], float] = boundary_height,
) -> list[Candidate]:
    """The candidates weighted without replacement (key u ** (1/w)), resting ones last."""
    keyed = [
        (c.title_id in resting, -(rng.random() ** (1.0 / weight(c))), c)
        for c in candidates
    ]
    keyed.sort(key=lambda row: (row[0], row[1]))
    return [c for _rests, _key, c in keyed]


def _boundary(
    pool: Sequence[Candidate],
    rng: random.Random,
    *,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
    exposure: Mapping[int, int] | None = None,
) -> Pair | None:
    """50%: a straddling title against a near neighbour in the tier its posterior reaches."""
    already = {frozenset(p) for p in (asked or ())}
    resting = _resting(recent, exposure)
    jitter = _jitter(pool, rng)
    straddlers = [c for c in pool if c.straddle is not None]
    for anchor in _weighted_order(straddlers, rng, resting=resting):
        across = [c for c in pool if c.tier == anchor.straddle]
        partner = _partner(
            pool, across, anchor, asked=already, resting=resting, exposure=exposure or {},
            jitter=jitter, near=_nearest(anchor, jitter),
        )
        if partner is not None:
            return Pair(
                title_a=anchor.title_id,
                title_b=partner.title_id,
                arm=ARM_BOUNDARY,
                reason="its posterior crosses this boundary",
            )
    return None


def _cross_tier(
    pool: Sequence[Candidate],
    rng: random.Random,
    *,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
    exposure: Mapping[int, int] | None = None,
) -> Pair | None:
    """25%: a gap of `CROSS_MIN_GAP` to `CROSS_MAX_GAP` steps, drawn as a uniform pair's would be so
    the gap tells nothing about the arm, then a title weighted by `cross_weight` against one that
    far (decision 564)."""
    already = {frozenset(p) for p in (asked or ())}
    resting = _resting(recent, exposure)
    jitter = _jitter(pool, rng)
    steps = Counter(c.shown for c in pool)
    pairs_at = Counter()
    for low, n_low in steps.items():
        for high, n_high in steps.items():
            if CROSS_MIN_GAP <= high - low <= CROSS_MAX_GAP:
                pairs_at[high - low] += n_low * n_high
    for gap in sorted(pairs_at, key=lambda g: -(rng.random() ** (1.0 / pairs_at[g]))):
        reach = [c for c in pool if steps[c.shown - gap] or steps[c.shown + gap]]
        for anchor in _weighted_order(reach, rng, resting=resting, weight=cross_weight):
            partner = _partner(
                pool, [c for c in pool if abs(c.shown - anchor.shown) == gap], anchor,
                asked=already, resting=resting, exposure=exposure or {}, jitter=jitter,
                near=lambda offered: offered,
            )
            if partner is not None:
                return Pair(
                    title_a=anchor.title_id,
                    title_b=partner.title_id,
                    arm=ARM_CROSS,
                    reason="its step against one two or more away",
                )
    return None


def _exploration(
    pool: Sequence[Candidate],
    rng: random.Random,
    *,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
    exposure: Mapping[int, int] | None = None,
) -> Pair | None:
    """15%: the least-compared title in the WHOLE pool against a near neighbour in `s`.

    Resting titles go last, ties go to the higher tier (decision 494). Answered pairs are never
    re-served.
    """
    if len(pool) < 2:
        return None
    already = {frozenset(p) for p in (asked or ())}
    resting = _resting(recent, exposure)
    jitter = _jitter(pool, rng)
    # The shuffle breaks exact ties by the draw; `list.sort` is stable.
    order = list(pool)
    rng.shuffle(order)
    order.sort(key=lambda c: (c.title_id in resting, c.comparisons, -c.tier))
    for anchor in order:
        partner = _partner(
            pool, pool, anchor, asked=already, resting=resting, exposure=exposure or {},
            jitter=jitter, near=_nearest(anchor, jitter),
        )
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
    exposure: Mapping[int, int] | None = None,
) -> Pair | None:
    """One pair, and the arm that produced it; None when no adaptive arm has a pair left.

    Cross-tier falls through to boundary, boundary to exploration. `asked`, `recent` and `exposure`
    never reach the held-out arm.
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
    reads = {
        "asked": {frozenset(p) for p in (asked or ())},
        "recent": frozenset(recent or ()),
        "exposure": exposure or {},
    }
    chain = {
        ARM_CROSS: (_cross_tier, _boundary, _exploration),
        ARM_BOUNDARY: (_boundary, _exploration),
        ARM_EXPLORATION: (_exploration,),
    }[arm]
    for selector in chain:
        pair = selector(pool, rng, **reads)
        if pair is not None:
            return pair
    return None


def reask(
    pool: Sequence[Candidate], answered: Sequence[tuple[int, int, int]], rng: random.Random
) -> Pair | None:
    """§13(b): while an answered `(duel_id, title_a, title_b)` has both titles still on the board,
    `REASK_RATE` of draws pose one again, chosen uniformly and in its first order, so a flip reads
    against the first outcome."""
    on_board = {c.title_id for c in pool}
    eligible = [row for row in answered if row[1] in on_board and row[2] in on_board]
    if not eligible or rng.random() >= REASK_RATE:
        return None
    duel_id, title_a, title_b = eligible[rng.randrange(len(eligible))]
    return Pair(
        title_a=title_a,
        title_b=title_b,
        arm=ARM_REASK,
        reason="asked again, held out of the fit",
        reask_of=duel_id,
    )


__all__ = [
    "ARM_BOUNDARY",
    "ARM_CROSS",
    "ARM_EXPLORATION",
    "ARM_HOLDOUT",
    "ARM_REASK",
    "CROSS_MAX_GAP",
    "CROSS_MIN_GAP",
    "EXPOSURE_WINDOW",
    "K_NEAREST",
    "NOUNS",
    "REASK_COOLDOWN",
    "REASK_MIN_AGE",
    "REASK_RATE",
    "RECENT_WINDOW",
    "ROUND_CAP",
    "SHARES",
    "SIMILAR_SHARE",
    "STALE_DAYS",
    "Candidate",
    "Pair",
    "boundary_height",
    "candidates",
    "cross_weight",
    "draw",
    "eligible",
    "likeness",
    "reask",
    "why",
]
