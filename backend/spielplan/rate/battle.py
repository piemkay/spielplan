"""§6.1 Battle: a pair drawn uniformly at random from one verdict band of the person's seen titles.

Never across a band or a kind (§4.1 rule 5); an answered pair returns only as a §13 re-ask.
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

# Decision 493: below this many live ratings a profile battle leaves the disliked band out.
EARLY_LABELS = 50
DISLIKED = 0

# Uniform attempts before enumerating the unanswered remainder of a mostly-compared pool.
_REJECTION_TRIES = 64


@dataclass(frozen=True)
class BattlePair:
    title_a: int
    title_b: int
    verdict_class: int     # the shared verdict class the pair was drawn from
    reason: str
    reask_of: int | None   # duel.id being silently re-asked; None otherwise

    def public(self) -> dict[str, Any]:
        """The allow-list projection that may reach the client: `reask_of` stays server-side."""
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
    """The pool keyed by (kind, verdict class): a pair crossing either is not representable."""
    out: dict[Stratum, list[int]] = {}
    for member in pool:
        out.setdefault((member.kind, member.verdict_class), []).append(member.title_id)
    for members in out.values():
        members.sort()
    return out


def eligible_pairs(pool: Sequence[PoolMember]) -> list[tuple[int, int]]:
    """Every unordered pair `draw` can produce, sorted."""
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

    None when no stratum holds two members or every pair is answered. Strata are weighted by pair
    count `n*(n-1)/2`, so the draw is uniform over the union of pairs rather than over strata.
    Rejection keeps it uniform over the unanswered remainder; `rng.sample` also randomises A/B.
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
    """§6.8's one-line why: a function of the band alone, so a re-ask reads the same."""
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
    """§6.1's "the user's seen titles within verdict bands": seen AND verdicted, one join each."""
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

    Via `rank_read.asked_pairs`, so a pair settled in Rank is not handed back as a battle.
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
    """The next pair, or None when no open band holds an uncompared pair.

    About `reask_rate` of pairs are §13 stream (b) re-asks of duels at least three days old, exempt
    from decision 493; with no candidate the draw falls through. `labels` is read when not given.
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
