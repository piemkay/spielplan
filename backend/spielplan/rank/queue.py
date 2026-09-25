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

WHICH PAIR INSIDE AN ADAPTIVE ARM (decision 494). §6.3 fixes the shares and not the pair, and the
first household showed what a uniform construction spends them on: fine-vs-fine at the B/A cut,
which sits in the middle of the verdict arm's "fine" band while the cutpoints are still the prior,
and disliked-vs-disliked many logits deep in F, where no answer can move a tier. §0 row 2 and §5.2
place the measured value of a comparison inside the liked class. So both adaptive arms lean toward
the top of the board and neither re-serves a pair it has already asked; the held-out arm does
neither, because §13 admits it as evaluation data only while it is uniform and memoryless.
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

# Decision 494's partner rule, which is M3-open-points §3.1's fix shape: "draw the partner from the
# k nearest". The nearest title across a boundary is the most informative one and also the one
# every draw lands on, so the partner is the least-compared of the five nearest instead - Patrick's
# first sitting partnered Ready Player One in four of six boundary draws, and ties on the ~10
# no-coordinate films that share one `s` all fell to the lowest title id.
K_NEAREST = 5

# Decision 494's no-repeat window: the titles of the last three answered adaptive pairs wait until
# another title can take their place, as anchor and as partner. A pair is never served twice
# (`asked`); a title coming straight back in the next pair is what the person actually noticed
# (Meet Joe Black in three of Jenny's ten). Soft rather than a filter, so a small board is never
# emptied by it - M4.10 finding 12 is what a filter on the anchor set does to one.
RECENT_WINDOW = 3


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


def _jitter(pool: Iterable[Candidate], rng: random.Random) -> dict[int, float]:
    """One draw-time tie-breaker per title, taken once per draw from the draw's own generator.

    It replaces "ties by id": on a real board about ten films have no coordinate at all and share
    one `s` set by their verdict alone, so an id tie-break handed every tie to the same film on
    every draw. Drawn rather than hashed, so a draw stays reproducible from its seed - which is
    what `api/rank.py::_queue_rng` and every test here depend on."""
    return {c.title_id: rng.random() for c in pool}


def _partner(
    options: Iterable[Candidate],
    anchor: Candidate,
    *,
    asked: set[frozenset[int]],
    recent: frozenset[int],
    jitter: dict[int, float],
) -> Candidate | None:
    """Decision 494's partner: of the `K_NEAREST` unasked titles nearest the anchor in `s`, the
    one outside the recent window, then the least-compared, then the draw.

    Nearness picks the neighbourhood, where the answer is least predictable and so most
    informative; the least-compared title inside it is what makes the arm rotate rather than
    settle on one partner. An answered pair is never offered again: each repeat is an independent
    Davidson row, so one judgement asked k times shrinks that pair's posterior by the root of k -
    §13's reliability inflation, reached through the selector (M3-open-points §3.1)."""
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
    """Decision 494's value weight: the index of the upper tier of the boundary a straddler
    crosses - 1 for F/D up to K-1 for A+/S on §6.3's seven, so S/A+ weighs six times D/F.

    Why the upper tier: a boundary duel can only move titles across a cut, because the cuts
    themselves learn from `tier_edit` alone (§5.2's tier arm) - so its value is the value of
    getting that cut's two tiers right, and §0 and §5.2 put the measured value inside the liked
    class. `model.straddle` only ever names an adjacent tier, so this is never 0."""
    return max(candidate.tier, int(candidate.straddle if candidate.straddle is not None else 0))


def _weighted_order(
    candidates: Sequence[Candidate], rng: random.Random, *, recent: frozenset[int]
) -> list[Candidate]:
    """The straddlers in a random order weighted by `boundary_height`, recent titles last.

    A weighted draw WITHOUT replacement (each key is u ** (1/w), largest first), so an anchor that
    has no unasked partner left hands over to the next one instead of ending the arm, and every
    straddler stays reachable - the weight orders the set and never shrinks it, which is the line
    M4.10 finding 12 drew under a restricted anchor set."""
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
    """70% — "posterior-straddling pairs".

    §6.3 gives the share and not the construction, so: a straddling title, paired with a
    near neighbour **in the tier its posterior reaches**. A straddler paired with an arbitrary
    partner would be a comparison about nothing in particular; the pair that settles a boundary
    is the one that spans it, and a title near it across the cut is one the answer is hard to
    predict for and therefore informative about.

    Which straddler and which partner are decision 494's: the anchor is drawn weighted toward the
    top of the board (`boundary_height`), the partner by `_partner`, and a pair already answered is
    never served again - the half of M3-open-points §3.1 this arm used to leave open, which served
    Jenny the same Rocky Horror / Meet Joe Black pair twice in four answers. An anchor with no
    unasked partner hands over to the next; when no straddler has one, the arm has nothing left and
    `draw` falls through to exploration, which says so.
    """
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
    """20% — "exploration".

    §6.3 names the share and nothing else. The arm that is *not* boundary-targeted and *not*
    uniform is the one that reduces uncertainty where no cutpoint is at stake: the title the
    person has compared least, against a near neighbour in `s` (`_partner`). It rotates on its own
    — answering increments both titles' counts, so the least-compared title is a different one
    next time.

    TIES GO TO THE TOP OF THE BOARD (decision 494), and no longer to the widest posterior. On a
    real board the widest σ belonged to titles whose embedding is off-scale, which sit at the far
    tails of `s` - so "least compared, widest posterior" served Grease against Miss Congeniality
    at s = -17 and -14, a pair no answer could move out of F. Among equally compared titles the
    higher tier anchors first; nothing is removed from the order, so a board that is all F and D
    is still explored, only after everything above it at the same count.

    THE WHOLE POOL, and that word is the repair. The anchor used to be chosen inside
    `[c for c in pool if c.straddle is None] or list(pool)`, and §6.3 licenses no such
    restriction: it names the share and calls the arm "exploration". On a young board nothing is
    in that list, so the arm silently drew straddlers; the moment one title left the straddle set
    the list inverted into the handful the model is *most* sure about, and because the route's
    incremental update shrinks exactly the two titles it answered and never moves the cutpoints,
    they stayed in it. Simulated over 500 answers with the route's own semantics: from answer 110
    the list held 1-2 of 900 titles, one pair served 78 of 109 exploration draws, and its anchor
    ended with the most comparisons on the board — under the line "the least-compared title on
    your board".

    `asked` is the unordered pairs this person has already answered, and none of them is served
    again. `tonight/round.py`'s `select` takes the same set for the same reason
    (M3-open-points §3.1): each repeat is an independent Davidson row, so ten repeats shrink one
    pair's posterior by the root of ten on the strength of a single judgement — §13's reliability
    inflation, arriving through the selector rather than through the evaluation stream.
    Exhausting an anchor's partners moves on to the next anchor rather than returning nothing, so
    the arm gives up only when the board itself has no unasked pair left.
    """
    if len(pool) < 2:
        return None
    already = {frozenset(p) for p in (asked or ())}
    held = frozenset(recent or ())
    jitter = _jitter(pool, rng)
    # Fewest comparisons first, then out of the recent window, then the higher tier (decision
    # 494). The shuffle before the sort is what breaks an exact tie on all three by the draw
    # rather than by id order, so a board where everything is equally unexplored (a new one) does
    # not walk the same prefix every time; `list.sort` is stable, so the ranking decides and the
    # shuffle survives only inside the ties.
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


def draw(
    pool: Sequence[Candidate],
    *,
    rng: random.Random,
    asked: Iterable[frozenset[int]] | None = None,
    recent: Iterable[int] | None = None,
) -> Pair | None:
    """One pair, and the arm that produced it.

    The roll picks an arm by §6.3's shares. A boundary roll on a pool with no straddler — or with
    no straddler left that has an unasked partner — falls through to exploration and *says*
    exploration; nothing ever falls into or out of the held-out arm, because its rate is the one
    thing §13 needs to be independent of the model. When neither adaptive arm has a pair left the
    draw is None, and the route says there is nothing left to settle.

    `asked` and `recent` reach the two adaptive arms and never the held-out one. A uniform sample
    with pairs removed according to what the model has already been told is not a uniform sample,
    and §13 admits no other evaluation data, so the tenth stays memoryless on purpose (decision
    494).
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
