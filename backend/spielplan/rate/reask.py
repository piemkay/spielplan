"""§13 stream (b): the silent re-ask of ladder placements (decision 550).

Invisible on the wire (the card's `reask_of` stays server-side), marked in the row
(`tier_edit.reask_of`), and skipped by the fit where the answer lands on the same step.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from spielplan.ledger.observations import latest_tier_edit_sql

# §13: "~10% of comparisons and ladder placements re-asked".
REASK_RATE = 0.10
# §13: "after >=3 days".
REASK_MIN_AGE = timedelta(days=3)
# Not in §13: without it a small library re-asks the same handful every sitting.
REASK_COOLDOWN = timedelta(days=90)


@dataclass(frozen=True)
class PlacementReask:
    """A placement worth posing again; `tier_edit_id` becomes the new edit's `reask_of`."""

    tier_edit_id: int
    title_id: int
    tier: int
    asked_at: datetime


_PLACEMENT_CANDIDATES = f"""
SELECT e.id, e.title_id, e.tier, e.created_at
  FROM ({latest_tier_edit_sql()}) e
  JOIN title t ON t.id = e.title_id
  JOIN user_title ut ON ut.user_id = $1 AND ut.title_id = e.title_id
 WHERE t.kind = $2
   AND ut.state = 'seen'
   AND e.created_at <= COALESCE($6::timestamptz, now()) - $3::interval
   AND NOT (e.title_id = ANY($4::int[]))
   AND NOT EXISTS (SELECT 1 FROM tier_edit r
                    WHERE r.user_id = $1 AND r.title_id = e.title_id AND r.reask_of IS NOT NULL
                      AND r.created_at > COALESCE($6::timestamptz, now()) - $5::interval)
 ORDER BY e.id
"""


def _sample(rows: list[Any], *, limit: int, rng: random.Random | None) -> list[Any]:
    """Uniform over the eligible rows, deterministic given `rng`.

    Not age-ordered: that would confound test-retest noise with genuine drift.
    """
    if limit <= 0 or not rows:
        return []
    if len(rows) <= limit:
        picked = list(rows)
        (rng or random).shuffle(picked)
        return picked
    return (rng or random).sample(rows, limit)


async def placement_candidates(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    limit: int = 1,
    exclude: Sequence[int] = (),
    rng: random.Random | None = None,
    now: datetime | None = None,
    min_age: timedelta = REASK_MIN_AGE,
    cooldown: timedelta = REASK_COOLDOWN,
) -> list[PlacementReask]:
    """Up to `limit` of the person's latest placements since their set-up that may be posed again, in
    a uniformly random order: seen, `min_age` old, and the title not re-asked inside `cooldown`.

    Both cutoffs use Postgres's clock, the one that stamped `created_at`; `now` overrides both.
    """
    if limit <= 0:
        return []
    rows = await conn.fetch(
        _PLACEMENT_CANDIDATES,
        user_id,
        kind,
        min_age,
        [int(t) for t in exclude],
        cooldown,
        now,
    )
    return [
        PlacementReask(
            tier_edit_id=int(r["id"]),
            title_id=int(r["title_id"]),
            tier=int(r["tier"]),
            asked_at=r["created_at"],
        )
        for r in _sample(list(rows), limit=limit, rng=rng)
    ]


__all__ = [
    "REASK_COOLDOWN",
    "REASK_MIN_AGE",
    "REASK_RATE",
    "PlacementReask",
    "placement_candidates",
]
