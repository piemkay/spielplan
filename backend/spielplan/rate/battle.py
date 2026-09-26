"""§6.1 Battle: two posters, drawn at random from the person's seen titles within a verdict band.

§6.1: "Pairs drawn **at random** from the user's seen titles within verdict bands — no clever
selection for profiles (measured null; the reason ships as UI copy: 'Random pairs. For profiles
no selection rule beats random — the clever ones pay off where the question is which of these
few, not how do you rank everything: the tier queue (§6.3) and tonight's round (§6.2).')."

That copy is 54a's, and the clause it replaced — "the clever ones only pay off in the tier
queue" — was false one surface over: `tonight/round.py` selects adaptively on purpose. 54a's
argument is that the two rules were never in tension, because the round solves best-arm
identification inside a pool of tens rather than the global-ranking problem row 6 measured.
Nothing here changes: this module still draws uniformly over the union of eligible pairs (the
ones not yet answered -- see below). [§6.1, §6.8, proposal 54a; M4.10 finding 22]

§0 row 6 is the measurement behind that: the best selection rule beat random by +0.0013 with a
confidence interval spanning zero. So a cleverer sampler here is a measured non-improvement,
and a *concentrated* one — one that keeps re-drawing the same few pairs because it weights
strata by member count, or picks a stratum uniformly regardless of how many pairs it holds — is
a straightforward bug. `draw` therefore weights each stratum by the number of unordered pairs
it contains, `n*(n-1)/2`, which is exactly what makes the draw uniform over the *union* of all
eligible pairs rather than uniform over strata.

TWO MEMBERSHIP RULES, AND BOTH ARE CONJUNCTIONS
A pool member must be **seen** AND **verdicted**. Seen without a verdict has no band to be
drawn from; verdicted without seen cannot happen through this app (a verdict implies seen) but
can arrive from a correction that set the title back to unseen, and such a title must leave the
pool — that is exactly what §6.1's corrections row is for.

WHY A PAIR NEVER CROSSES A BAND OR A KIND
Within-class pairs are what add resolution (§5.2: "comparisons add resolution *within* the liked
class"). A cross-class pair re-derives a boundary the ordered-logit arm already knows from the
verdicts themselves. And §4.1 rule 5 partitions every ranking surface by kind, so a duel with
one foot in each partition is not evidence about either — `observations.record_duel` refuses to
write one, and the strata key makes it unreachable here.

A PAIR THE PERSON HAS ALREADY ANSWERED IS NOT DRAWN AGAIN. This paragraph used to say the
opposite -- "repeating a pair is informative" -- and the first household test served S.W.A.T. vs
The Village three times in Jenny's first eight cards, answered B, A, TIE inside 24 s, each an
ordinary duel row. `rank/read.asked_pairs` already states the rule Rank and Tonight select under:
each repeat is an independent Davidson row, so ten repeats shrink one pair's posterior by the root
of ten on the strength of a single judgement -- §13's reliability inflation reached through the
selector (M3-open-points §3.1). So the draw is uniform over the eligible pairs NOT YET ANSWERED in
any context, and §13's re-ask branch below -- spaced at least three days, recorded as a re-ask --
is the only way a pair comes back. Sampling without replacement is still random, so §0 row 6's
measured null is untouched. [owner instruction of 2026-09-25 after the first household user test]

THE DISLIKED BAND WAITS FOR A FIRST SITTING (decision 493). Until the person holds
`EARLY_LABELS` live ratings a profile battle draws from the fine and liked bands only. Both
members met their first battle comparing two films they had just called disliked (duels 2-5 on
the v20260925 install), and §5.2 credits comparisons with resolution *within* the liked class.
Past the first sitting every band is drawn uniformly again, as §6.1 and §0 row 6 have it.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rank import read as rank_read
from spielplan.rate import LIVE_LABEL, VERDICT_LABELS, balance
from spielplan.rate import reask as reask_stream

log = logging.getLogger("spielplan.rate.battle")

# Decision 493: the live-rating count below which a profile battle leaves the disliked band out.
# §6.1's "Aim for 50-100 in your first sitting or two", at its low end -- the first sitting.
EARLY_LABELS = 50
DISLIKED = 0

# How many uniform draws over the whole eligible set `draw` tries before it enumerates what is
# left. Rejection keeps a large pool's draw O(1) and is exactly uniform over the pairs not yet
# answered; enumeration is only reached when most of a small pool has been compared already.
_REJECTION_TRIES = 64


@dataclass(frozen=True)
class BattlePair:
    title_a: int
    title_b: int
    verdict_class: int     # the shared verdict class the pair was drawn from
    reason: str
    reask_of: int | None   # duel.id being silently re-asked; None otherwise

    def public(self) -> dict[str, Any]:
        """The allow-list projection that may reach the client.

        `reask_of` is absent for §13 stream (b), and `verdict_class` stays because it is a
        property of the *pair* (both members share it) rather than of the stream: a re-ask pair
        has one too, and it is what the why-line already says out loud.
        """
        return {
            "title_a": self.title_a,
            "title_b": self.title_b,
            "verdict_class": self.verdict_class,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PoolMember:
    title_id: int
    kind: str
    verdict_class: int


Stratum = tuple[str, int]


def strata(pool: Sequence[PoolMember]) -> dict[Stratum, list[int]]:
    """The pool, keyed by (kind, verdict class). The key IS §6.1's "within verdict bands" and
    §4.1 rule 5's partition — a pair that crosses either is not representable."""
    out: dict[Stratum, list[int]] = {}
    for member in pool:
        out.setdefault((member.kind, member.verdict_class), []).append(member.title_id)
    for members in out.values():
        members.sort()
    return out


def eligible_pairs(pool: Sequence[PoolMember]) -> list[tuple[int, int]]:
    """Every unordered pair `draw` can produce, sorted. The reference set the uniformity test
    measures against, and the definition of "eligible" in one place rather than two."""
    pairs: list[tuple[int, int]] = []
    for members in strata(pool).values():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pairs.append((a, b))
    return sorted(pairs)


def _stratum(keys: Sequence[Stratum], weights: Sequence[int], threshold: float) -> Stratum:
    running = 0
    for key, weight in zip(keys, weights, strict=True):
        running += weight
        if threshold < running:
            return key
    return keys[-1]


def draw(
    pool: Sequence[PoolMember],
    *,
    rng: random.Random,
    answered: Collection[frozenset[int]] = frozenset(),
) -> tuple[int, int, str, int] | None:
    """One uniform draw over `eligible_pairs(pool)` minus `answered`, as (a, b, kind, class).

    None when no single (kind, class) stratum holds two members — the state §6.1 reaches before
    the first two verdicts of one class exist — or when every pair the pool holds is in
    `answered`, the unordered pairs this person has already compared (see the module docstring).

    The stratum is chosen with probability proportional to `n*(n-1)/2`, its pair count. Choosing
    uniformly over strata, or proportionally to `n`, would over-serve the small ones: a person
    with 30 liked and 2 disliked titles would spend half their battles on the same single
    disliked pair, and every one of those repeats is a comparison §5.2 says adds nothing.

    An answered pair is rejected and the draw taken again, which keeps it uniform over the pairs
    that remain: every attempt is uniform over the whole set, so the one that is kept is uniform
    over what is left. After `_REJECTION_TRIES` refusals the remainder is enumerated and one is
    chosen uniformly, which is the same distribution reached the slow way. With nothing answered
    the first attempt is always kept, so the draw spends the RNG exactly as it always has.

    `rng.sample` also decides which of the two is A, so the left/right position is randomised
    rather than baked in by, say, id order.
    """
    live = {key: members for key, members in strata(pool).items() if len(members) >= 2}
    if not live:
        return None
    keys = sorted(live)
    weights = [len(live[key]) * (len(live[key]) - 1) // 2 for key in keys]
    total = sum(weights)
    for _ in range(_REJECTION_TRIES if answered else 1):
        chosen = _stratum(keys, weights, rng.random() * total)
        a, b = rng.sample(live[chosen], 2)
        if frozenset((a, b)) not in answered:
            return a, b, chosen[0], chosen[1]
    remaining = [
        (a, b, key)
        for key in keys
        for i, a in enumerate(live[key])
        for b in live[key][i + 1 :]
        if frozenset((a, b)) not in answered
    ]
    if not remaining:
        return None
    a, b, key = rng.choice(remaining)
    if rng.random() < 0.5:
        a, b = b, a
    return a, b, key[0], key[1]


def reason_for(verdict_class: int) -> str:
    """§6.8's one-line why, in the member register.

    Identical for a re-ask, by construction: it is a function of the band alone, and a re-ask
    pair has a band like any other.

    It used to carry 54a's whole clause, section references and all ("the tier queue (§6.3) and
    tonight's round (§6.2)"), about fifty words under two posters: on an iPhone that pushed Tie,
    the decisive toggle and Skip below the fold, and decision 486 keeps section references off
    every member surface. The line now says the two things this card is about -- the pair shares
    the person's own answer, and it was drawn at random -- and §6.1's pair-selection sentence,
    restated in plain words by decision 491, lives whole in the rail's "why these questions?"
    card, which the phone reaches with one tap. [decisions 486, 491; C5.6]

    Then "queued because: you rated both liked · random pairs build your profile best" read as
    the queue talking about itself on the second household test, over a card that never asked
    its question. The card asks it now ("Which did you enjoy more?"), and this line is the one
    fact that makes the pair a fair question: the person put both titles in the same place. How
    the pairs are drawn is the rail's to say. [§6.1, §6.8; A1 and A3 of the 2026-09-26 test]
    """
    return f"You rated both of these {VERDICT_LABELS[verdict_class]}."


_POOL = f"""
WITH label AS ({LIVE_LABEL})
SELECT ut.title_id, t.kind, l.value AS verdict_class
  FROM user_title ut
  JOIN title t ON t.id = ut.title_id
  JOIN label l ON l.title_id = ut.title_id
 WHERE ut.user_id = $1
   AND ut.state = 'seen'
   AND t.kind = ANY($2::text[])
   AND NOT (ut.title_id = ANY($3::int[]))
 ORDER BY ut.title_id
"""


async def battle_pool(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    exclude: Sequence[int] = (),
) -> list[PoolMember]:
    """§6.1's "the user's seen titles within verdict bands", as one conjunction.

    The JOIN onto `user_title` is the "seen" half and the JOIN onto `label` is the "verdicted"
    half; neither is a filter that can be relaxed without changing what a battle means.
    """
    if not kinds:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    rows = await conn.fetch(_POOL, user_id, list(kinds), [int(t) for t in exclude])
    return [
        PoolMember(
            title_id=int(r["title_id"]), kind=str(r["kind"]), verdict_class=int(r["verdict_class"])
        )
        for r in rows
    ]


def open_bands(pool: Sequence[PoolMember], *, labels: int) -> list[PoolMember]:
    """Decision 493: below `EARLY_LABELS` live ratings the disliked band sits out."""
    if labels >= EARLY_LABELS:
        return list(pool)
    return [member for member in pool if member.verdict_class != DISLIKED]


async def answered_pairs(
    conn: asyncpg.Connection, *, user_id: int, kinds: Sequence[str]
) -> set[frozenset[int]]:
    """Every pair this person has already compared, in any context, for the kinds in play.

    `rank_read.asked_pairs` and not a query of this module's own: a pair settled in a Rank
    comparison or by a drop is a pair the person has answered, and a copy scoped to
    `profile_battle` would hand it back as a battle. Called once per kind because that helper
    is partitioned by kind (§4.1 rule 5) and a Rate session may hold both.
    """
    pairs: set[frozenset[int]] = set()
    for kind in kinds:
        pairs |= await rank_read.asked_pairs(conn, user_id=user_id, kind=kind)
    return pairs


async def next_battle_pair(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    exclude: Sequence[int] = (),
    rng: random.Random | None = None,
    reask_rate: float = reask_stream.REASK_RATE,
    labels: int | None = None,
) -> BattlePair | None:
    """The next pair, or None when no open band holds two seen+verdicted titles that have not
    already been compared.

    About `reask_rate` of pairs are §13 stream (b) re-asks of duels at least three days old,
    served with the stored `(title_a, title_b)` order preserved and with the same why-line as
    any other pair. When no duel qualifies the draw falls through to an ordinary one, so the
    stream never costs a person a question. The re-ask is the one path by which a compared pair
    returns, and it is exempt from decision 493 because it re-asks what was already asked.

    `labels` is the person's live-rating count over `kinds` — the class-balance widget's total —
    and is read here when the caller has not already read it.
    """
    rng = rng or random.Random()
    if labels is None:
        labels = (await balance.class_balance(conn, user_id=user_id, kinds=kinds)).total
    if reask_stream.draws(rng, rate=reask_rate):
        candidates = await reask_stream.duel_candidates(
            conn, user_id=user_id, kinds=kinds, limit=1, exclude=exclude, rng=rng
        )
        if candidates:
            again = candidates[0]
            return BattlePair(
                title_a=again.title_a,
                title_b=again.title_b,
                verdict_class=again.verdict_class,
                reason=reason_for(again.verdict_class),
                reask_of=again.duel_id,
            )
    pool = open_bands(
        await battle_pool(conn, user_id=user_id, kinds=kinds, exclude=exclude), labels=labels
    )
    drawn = draw(
        pool, rng=rng, answered=await answered_pairs(conn, user_id=user_id, kinds=kinds)
    )
    if drawn is None:
        return None
    title_a, title_b, _kind, verdict_class = drawn
    return BattlePair(
        title_a=title_a,
        title_b=title_b,
        verdict_class=verdict_class,
        reason=reason_for(verdict_class),
        reask_of=None,
    )


__all__ = [
    "EARLY_LABELS",
    "BattlePair",
    "PoolMember",
    "answered_pairs",
    "battle_pool",
    "draw",
    "eligible_pairs",
    "next_battle_pair",
    "open_bands",
    "reason_for",
    "strata",
]
