"""The ladder's set-up, once (§6.1, decision 547): one step per tier from the best down, each a page of
films in platform-score order. Finishing it is the member's cut-over (decision 537). Films only: series
go straight onto the ladder (decision 550)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.db import library
from spielplan.ledger import ladder, model, observations
from spielplan.rate import session

KIND = "movie"

# By the verdict class the step stands for: where its films open in the platform-score order.
HINTS = {2: "Highest rated first.", 1: "From the middle.", 0: "Lowest rated first."}
TAP = " Tap the ones you remember well."


@dataclass(frozen=True)
class Step:
    tier: int
    word: str
    hint: str


async def _tier_set(conn: asyncpg.Connection, user_id: int) -> tuple[str, ...]:
    return await observations.tier_set_of(conn, user_id=user_id, kind=KIND)


async def steps(conn: asyncpg.Connection, *, user_id: int) -> list[Step]:
    """One step per tier of the person's film set, best first, named by its word (no letter, §6.1)."""
    tier_set = await _tier_set(conn, user_id)
    k = len(tier_set)
    words = observations.tier_words(tier_set)
    return [
        Step(tier, words[tier], HINTS[model.verdict_class_of_tier(tier, k)] + TAP)
        for tier in reversed(range(k))
    ]


def start_of(tier: int, k: int) -> float:
    """Where a step opens in the order (0 = the highest rated, 1 = the lowest): where its tier begins in
    the measured shape at K = 7, the bottom step at the lowest; equal steps at another K."""
    if k < 2:
        return 0.0
    if tier == 0:
        return 1.0
    if k == len(model.MEASURED_TIER_SHARES):
        return float(sum(model.MEASURED_TIER_SHARES[tier + 1:]))
    return (k - 1 - tier) / (k - 1)


async def films_page(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    step: int,
    offset: int,
    limit: int,
    exclude: Sequence[int] = (),
) -> tuple[list[dict[str, Any]], bool]:
    """A page of the step's films and whether more follow. Raises ValueError for a step outside the set."""
    k = len(await _tier_set(conn, user_id))
    if not 0 <= step < k:
        raise ValueError(f"step {step} is outside a set of {k}")
    return await library.films_by_platform_score(
        conn, user_id=user_id, start=start_of(step, k), offset=offset, limit=limit, exclude=exclude
    )


async def finish(
    conn: asyncpg.Connection, *, user_id: int, picks: Sequence[tuple[int, int]]
) -> dict[str, Any]:
    """The cut-over, then the live Rate session ends, so no Undo reaches across it (decision 35).
    Raises ladder.AlreadySetUp and ladder.SetupRefused. Returns the done screen's counts."""
    async with conn.transaction():
        result = await ladder.finish_setup(conn, user_id=user_id, picks=picks)
        await session.end_session(conn, user_id=user_id)

    by_tier: dict[int, list[int]] = {}
    for placement in result.placed:
        by_tier.setdefault(placement.tier, []).append(placement.edit.title_ids[0])
    firsts = await conn.fetch(
        "SELECT id, name, poster_path FROM title WHERE id = ANY($1::int[])",
        [ids[0] for ids in by_tier.values()],
    )
    first = {r["id"]: dict(r) for r in firsts}
    words = observations.tier_words(await _tier_set(conn, user_id))
    return {
        "done": True,
        "placed": len(result.placed),
        "tiers": [
            {
                "tier": tier,
                "word": words[tier],
                "count": len(by_tier.get(tier, ())),
                "first": first.get(by_tier[tier][0]) if tier in by_tier else None,
            }
            for tier in reversed(range(len(words)))
        ],
        "earlier_ratings": result.earlier_ratings,
        "rated_before": result.rated_before,
    }


__all__ = ["HINTS", "KIND", "Step", "films_page", "finish", "start_of", "steps"]
