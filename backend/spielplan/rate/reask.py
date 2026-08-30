"""§13 stream (b): the silent re-ask stream, and the flip rate it exists to measure.

§13: "a separate silent **re-ask stream** — ~10% of comparisons/verdicts re-asked after >=3
days; ~200 re-asks measure the flip rate sigma that sets the tier budget (DNA_MODEL §4.2
build-order #1) and settles the corpus's zero-test-retest-data unknown."

Decision-doc proposal 50 spells out the UI half: "~10% of queue slots are silent re-asks of
verdicts and duels >=3 days old — indistinguishable from a normal card by design, never
labelled in the UI, and excluded from the class-balance widget."

Three properties have to hold at once, and each is enforced somewhere different:

  (a) **Invisible on the wire.** The re-ask reference never leaves the server. This module
      returns it on `VerdictReask` / `DuelReask`; the card the client gets is built by
      `queue.QueueCard` / `battle.BattlePair`, whose `public()` is an allow-list that has no
      `reask_of` and no `source`. The reason line is the *same sentence* a genuinely pending
      card carries, produced by the same branch of the same function, so there is no phrasing
      to compare either.
  (b) **Distinguishable in the row.** `verdict.is_reask` / `verdict.reask_of` and the same pair
      on `duel`. `flip_rate` below is why that matters: a stored field nobody can compute from
      is not an instrument, it is a comment in a column.
  (c) **Counted once.** `ledger.observations.load_observations` filters `NOT is_reask` out of
      the fit, and `rate.balance` and `rate.battle` read the person's current label through
      `rate.LIVE_LABEL`, which does the same. A stream that exists to settle the test-retest
      unknown cannot also move the model it measures.

WHICH ROWS ARE ELIGIBLE, AND WHY EACH CLAUSE IS THERE
  * `NOT is_reask` — a re-ask of a re-ask measures the wrong interval and chains the flip
    definition to a row that was itself an instrument reading.
  * the newest non-re-ask row for that (user, title) — an answer the person has since replaced
    is not the judgement whose stability is in question.
  * `created_at <= now - 3 days` — §13's own floor.
  * no re-ask of this row inside the cooldown — otherwise a small library re-asks the same
    verdict every sitting, and sigma measures one title's mood rather than the household's
    consistency.
  * still marked seen — so the card's why-line is the true one for a pending card AND for this
    one. A re-ask whose title is no longer marked seen would need a different sentence, and a
    different sentence is a marker.
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

# ---------------------------------------------------------------------------------------------
# TUNED NUMBERS. §13 fixes all four; they belong in `spielplan/ledger/hyperparams.py`, which is
# "the only module in the package allowed to contain a tuning number" and is the file the corpus
# project re-tunes through `ledger_hyperparams.json`. That module is wave-1 frozen for this
# milestone, so they live here in one block under the same contract. Reported as a gap.
# ---------------------------------------------------------------------------------------------

# §13: "~10% of comparisons/verdicts re-asked".
REASK_RATE = 0.10
# §13: "after >=3 days".
REASK_MIN_AGE = timedelta(days=3)
# Not in §13. A row re-asked once is not re-asked again for a season: without it a household
# with forty rated titles re-asks the same handful every sitting and sigma stops being a
# household-level reading. Chosen once, here, with that reason.
REASK_COOLDOWN = timedelta(days=90)
# §13: "~200 re-asks measure the flip rate sigma".
FLIP_RATE_TARGET_N = 200


def draws(rng: random.Random, *, rate: float = REASK_RATE) -> bool:
    """One slot's coin flip. Separate so a test can watch the rate rather than the outcome."""
    return rng.random() < rate


@dataclass(frozen=True)
class VerdictReask:
    """A verdict worth posing again. `verdict_id` is what the eventual write puts in
    `verdict.reask_of`; `value` is the earlier answer, kept so a caller can compute the flip
    without a second query."""

    verdict_id: int
    title_id: int
    value: int
    asked_at: datetime


@dataclass(frozen=True)
class DuelReask:
    """A duel worth posing again, with `(title_a, title_b)` in the ORDER IT WAS ASKED.

    Preserving the order is what makes a flip literally `outcome <> original.outcome` with no
    normalisation, and whatever left/right position bias exists is then constant across both
    asks and cancels out of the rate.
    """

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

    The sampling happens here rather than as `ORDER BY random() LIMIT n` because a test that
    cannot hold the draw still cannot prove the rate, and because §13's stream has to be a
    uniform sample of the person's answers: ordering by age instead would confound test-retest
    noise with genuine drift, which is the one confusion this instrument exists to avoid.
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

    Both age cutoffs are evaluated by Postgres, against the same `now()` that stamped
    `created_at`. Computing "three days ago" in Python instead compares two clocks: this
    machine's Postgres runs ~220 ms ahead of the application process, which is enough to make a
    row backdated to exactly the boundary fall on the wrong side of it. `now` overrides the
    clock for both cutoffs at once, which is how a test reaches past the cooldown.
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
    """Up to `limit` duels eligible to be posed again, in a uniformly random order. Both age
    cutoffs are evaluated by Postgres — see `verdict_candidates` for why that matters."""
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
    """§13's sigma: how often the same question, posed again, gets a different answer.

    Reported per arm as well as pooled, because a 3-class verdict and a 3-outcome duel are
    different questions and a pooled rate over an unbalanced mix of the two is a number about
    the sampler rather than about the household.
    """

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

    The join is `reask.reask_of -> original.id`, so a re-ask whose original was undone (and
    therefore deleted — `observations.undo` is the one path allowed to delete a verdict) drops
    out of both the numerator and the denominator rather than counting as a non-flip.
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
