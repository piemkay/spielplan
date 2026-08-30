"""§6.1 Battle: two posters, drawn at random from the person's seen titles within a verdict band.

§6.1: "Pairs drawn **at random** from the user's seen titles within verdict bands — no clever
selection for profiles (measured null; the reason ships as UI copy: 'For profiles no selection
rule beats random — the clever ones only pay off in the tier queue.')."

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

ALREADY-DUELLED PAIRS ARE NOT EXCLUDED. Repeating a pair is informative — it is literally what
§13's re-ask stream does on purpose — and excluding them would put a hole in the uniformity this
module's coverage row tests. Spec-silent, decided here.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rate import LIVE_LABEL, VERDICT_LABELS
from spielplan.rate import reask as reask_stream

log = logging.getLogger("spielplan.rate.battle")


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


def draw(pool: Sequence[PoolMember], *, rng: random.Random) -> tuple[int, int, str, int] | None:
    """One uniform draw over `eligible_pairs(pool)`, as (title_a, title_b, kind, class).

    None when no single (kind, class) stratum holds two members — the state §6.1 reaches before
    the first two verdicts of one class exist.

    The stratum is chosen with probability proportional to `n*(n-1)/2`, its pair count. Choosing
    uniformly over strata, or proportionally to `n`, would over-serve the small ones: a person
    with 30 liked and 2 disliked titles would spend half their battles on the same single
    disliked pair, and every one of those repeats is a comparison §5.2 says adds nothing.

    `rng.sample` also decides which of the two is A, so the left/right position is randomised
    rather than baked in by, say, id order.
    """
    live = {key: members for key, members in strata(pool).items() if len(members) >= 2}
    if not live:
        return None
    keys = sorted(live)
    weights = [len(live[key]) * (len(live[key]) - 1) // 2 for key in keys]
    threshold = rng.random() * sum(weights)
    running = 0
    chosen = keys[-1]
    for key, weight in zip(keys, weights, strict=True):
        running += weight
        if threshold < running:
            chosen = key
            break
    a, b = rng.sample(live[chosen], 2)
    return a, b, chosen[0], chosen[1]


def reason_for(verdict_class: int) -> str:
    """§6.8's one-line why, carrying §6.1's measured-null copy.

    Identical for a re-ask, by construction: it is a function of the band alone, and a re-ask
    pair has a band like any other.
    """
    return (
        f"queued because: both of these you rated {VERDICT_LABELS[verdict_class]}, "
        "drawn at random within the class — for profiles no selection rule beats random"
    )


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


async def next_battle_pair(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    exclude: Sequence[int] = (),
    rng: random.Random | None = None,
    reask_rate: float = reask_stream.REASK_RATE,
) -> BattlePair | None:
    """The next pair, or None when the user has fewer than two seen+verdicted titles in any one
    class.

    About `reask_rate` of pairs are §13 stream (b) re-asks of duels at least three days old,
    served with the stored `(title_a, title_b)` order preserved and with the same why-line as
    any other pair. When no duel qualifies the draw falls through to an ordinary one, so the
    stream never costs a person a question.
    """
    rng = rng or random.Random()
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
    drawn = draw(await battle_pool(conn, user_id=user_id, kinds=kinds, exclude=exclude), rng=rng)
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
    "BattlePair",
    "PoolMember",
    "battle_pool",
    "draw",
    "eligible_pairs",
    "next_battle_pair",
    "reason_for",
    "strata",
]
