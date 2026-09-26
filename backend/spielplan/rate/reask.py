"""§13 stream (b): the silent re-ask stream, and the flip rate it exists to measure.

Invisible on the wire (no `reask_of` in `public()`), marked in the row, and excluded from the fit.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from spielplan.rate import LIVE_LABEL

log = logging.getLogger("spielplan.rate.reask")

# §13: "~10% of comparisons/verdicts re-asked".
REASK_RATE = 0.10
# §13: "after >=3 days".
REASK_MIN_AGE = timedelta(days=3)
# Not in §13: without it a small library re-asks the same handful every sitting.
REASK_COOLDOWN = timedelta(days=90)
# §13: "~200 re-asks measure the flip rate sigma".
FLIP_RATE_TARGET_N = 200


def draws(rng: random.Random, *, rate: float = REASK_RATE) -> bool:
    """One slot's coin flip. Separate so a test can watch the rate rather than the outcome."""
    return rng.random() < rate


@dataclass(frozen=True)
class VerdictReask:
    """A verdict worth posing again; `verdict_id` becomes the new row's `reask_of`."""

    verdict_id: int
    title_id: int
    value: int
    asked_at: datetime


@dataclass(frozen=True)
class DuelReask:
    """A duel worth posing again, in the order it was asked, so a flip is `outcome <> original`."""

    duel_id: int
    title_a: int
    title_b: int
    verdict_class: int
    outcome: str
    asked_at: datetime


_VERDICT_CANDIDATES = f"""
WITH label AS ({LIVE_LABEL})
SELECT v.id, v.title_id, v.value, v.created_at
  FROM verdict v
  JOIN title t ON t.id = v.title_id
  -- the person's CURRENT answer only. `label` is the newest non-re-ask row per title, so
  -- joining on its *id* is what excludes an answer they have since replaced — and it is the
  -- same definition of "current label" the battle bands and the class-balance widget use.
  JOIN label l ON l.verdict_id = v.id
  JOIN user_title ut ON ut.user_id = v.user_id AND ut.title_id = v.title_id
 WHERE v.user_id = $1
   AND NOT v.is_reask
   AND t.kind = ANY($2::text[])
   AND ut.state = 'seen'
   AND v.created_at <= COALESCE($6::timestamptz, now()) - $3::interval
   AND NOT (v.title_id = ANY($4::int[]))
   AND NOT EXISTS (SELECT 1 FROM verdict r
                    WHERE r.reask_of = v.id
                      AND r.created_at > COALESCE($6::timestamptz, now()) - $5::interval)
 ORDER BY v.id
"""

_DUEL_CANDIDATES = f"""
WITH label AS ({LIVE_LABEL})
SELECT d.id, d.title_a, d.title_b, d.outcome, d.created_at, la.value AS verdict_class
  FROM duel d
  JOIN title ta ON ta.id = d.title_a
  JOIN title tb ON tb.id = d.title_b
  JOIN label la ON la.title_id = d.title_a
  JOIN label lb ON lb.title_id = d.title_b
  JOIN user_title ua ON ua.user_id = d.user_id AND ua.title_id = d.title_a
  JOIN user_title ub ON ub.user_id = d.user_id AND ub.title_id = d.title_b
 WHERE d.user_id = $1
   AND NOT d.is_reask
   AND d.context = 'profile_battle'
   AND ta.kind = ANY($2::text[]) AND tb.kind = ANY($2::text[])
   AND ua.state = 'seen' AND ub.state = 'seen'
   -- §6.1: both members of a battle pair share a verdict class. A re-rating that split the
   -- pair makes the pair unaskable rather than making it a cross-class question.
   AND la.value = lb.value
   AND d.created_at <= COALESCE($6::timestamptz, now()) - $3::interval
   AND NOT (d.title_a = ANY($4::int[])) AND NOT (d.title_b = ANY($4::int[]))
   AND NOT EXISTS (SELECT 1 FROM duel r
                    WHERE r.reask_of = d.id
                      AND r.created_at > COALESCE($6::timestamptz, now()) - $5::interval)
 ORDER BY d.id
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


async def verdict_candidates(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    limit: int = 1,
    exclude: Sequence[int] = (),
    rng: random.Random | None = None,
    now: datetime | None = None,
    min_age: timedelta = REASK_MIN_AGE,
    cooldown: timedelta = REASK_COOLDOWN,
) -> list[VerdictReask]:
    """Up to `limit` verdicts eligible to be posed again, in a uniformly random order.

    Both age cutoffs use Postgres's clock, the one that stamped `created_at`; `now` overrides both.
    """
    if limit <= 0 or not kinds:
        return []
    rows = await conn.fetch(
        _VERDICT_CANDIDATES,
        user_id,
        list(kinds),
        min_age,
        [int(t) for t in exclude],
        cooldown,
        now,
    )
    return [
        VerdictReask(
            verdict_id=int(r["id"]),
            title_id=int(r["title_id"]),
            value=int(r["value"]),
            asked_at=r["created_at"],
        )
        for r in _sample(list(rows), limit=limit, rng=rng)
    ]


async def duel_candidates(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    limit: int = 1,
    exclude: Sequence[int] = (),
    rng: random.Random | None = None,
    now: datetime | None = None,
    min_age: timedelta = REASK_MIN_AGE,
    cooldown: timedelta = REASK_COOLDOWN,
) -> list[DuelReask]:
    """Up to `limit` duels eligible to be posed again; cutoffs as in `verdict_candidates`."""
    if limit <= 0 or not kinds:
        return []
    rows = await conn.fetch(
        _DUEL_CANDIDATES,
        user_id,
        list(kinds),
        min_age,
        [int(t) for t in exclude],
        cooldown,
        now,
    )
    return [
        DuelReask(
            duel_id=int(r["id"]),
            title_a=int(r["title_a"]),
            title_b=int(r["title_b"]),
            verdict_class=int(r["verdict_class"]),
            outcome=str(r["outcome"]),
            asked_at=r["created_at"],
        )
        for r in _sample(list(rows), limit=limit, rng=rng)
    ]


# --- the instrument ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmFlips:
    arm: str
    n: int
    flips: int

    @property
    def rate(self) -> float | None:
        return None if self.n == 0 else self.flips / self.n

    def as_dict(self) -> dict[str, Any]:
        return {"arm": self.arm, "n": self.n, "flips": self.flips, "rate": self.rate}


@dataclass(frozen=True)
class FlipRate:
    """§13's sigma, per arm and pooled: a verdict and a duel are different questions."""

    verdicts: ArmFlips
    duels: ArmFlips
    target: int = FLIP_RATE_TARGET_N

    @property
    def n(self) -> int:
        return self.verdicts.n + self.duels.n

    @property
    def flips(self) -> int:
        return self.verdicts.flips + self.duels.flips

    @property
    def sigma(self) -> float | None:
        return None if self.n == 0 else self.flips / self.n

    @property
    def sufficient(self) -> bool:
        """§13 wants ~200 re-asks before sigma is worth quoting."""
        return self.n >= self.target

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "flips": self.flips,
            "sigma": self.sigma,
            "target": self.target,
            "sufficient": self.sufficient,
            "verdicts": self.verdicts.as_dict(),
            "duels": self.duels.as_dict(),
        }


async def flip_rate(
    conn: asyncpg.Connection,
    *,
    user_id: int | None = None,
    kinds: Sequence[str] | None = None,
) -> FlipRate:
    """Compute sigma over every stored re-ask. `user_id=None` pools the household.

    A re-ask whose original was undone drops out of both numerator and denominator.
    """
    kind_list = list(kinds) if kinds else None
    verdicts = await conn.fetchrow(
        """
        SELECT count(*) AS n, count(*) FILTER (WHERE r.value <> v.value) AS flips
          FROM verdict r
          JOIN verdict v ON v.id = r.reask_of
          JOIN title t ON t.id = r.title_id
         WHERE r.is_reask
           AND ($1::bigint IS NULL OR r.user_id = $1)
           AND ($2::text[] IS NULL OR t.kind = ANY($2::text[]))
        """,
        user_id,
        kind_list,
    )
    duels = await conn.fetchrow(
        """
        SELECT count(*) AS n, count(*) FILTER (WHERE r.outcome <> d.outcome) AS flips
          FROM duel r
          JOIN duel d ON d.id = r.reask_of
          JOIN title t ON t.id = r.title_a
         WHERE r.is_reask
           AND ($1::bigint IS NULL OR r.user_id = $1)
           AND ($2::text[] IS NULL OR t.kind = ANY($2::text[]))
        """,
        user_id,
        kind_list,
    )
    return FlipRate(
        verdicts=ArmFlips("verdict", int(verdicts["n"]), int(verdicts["flips"])),
        duels=ArmFlips("duel", int(duels["n"]), int(duels["flips"])),
    )


__all__ = [
    "FLIP_RATE_TARGET_N",
    "REASK_COOLDOWN",
    "REASK_MIN_AGE",
    "REASK_RATE",
    "ArmFlips",
    "DuelReask",
    "FlipRate",
    "VerdictReask",
    "draws",
    "duel_candidates",
    "flip_rate",
    "verdict_candidates",
]
