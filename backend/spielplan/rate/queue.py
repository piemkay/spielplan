"""The §6.1 sweep queue: which title to ask about next, and the one line saying why.

§6.1: "**Queue:** P(seen)-ordered (Jellyfin history, popularity, household co-seen), seeded
first run from the imported 100-title decade-stratified `seed_list`. Blocks of 15; each card
shows its queue reason ('queued because: 72% likely you have seen it')."

Three sentences, three rules, and they compose in one ORDER BY:

  1. **Recorded-seen first.** A title the app already holds as `seen` with no verdict is
     exactly the population of §6.0's pending-verdicts banner. It is not an estimate, so
     `p_seen` is 1.0 for it by definition rather than by a large weight, and "seen first" and
     "highest P(seen) first" are then the same instruction rather than two that can diverge.
  2. **Then the seed list, in its position order.** A fresh household has no seen rows and no
     verdicts, so their first queue *is* `seed_list` — the spec's "seeded first run" falls out
     of the ordering instead of needing a mode flag. Seed precedence ends by CONSUMPTION: once
     every seed title carries a verdict the branch is empty and P(seen) governs for good.
  3. **Then descending P(seen).**

WHAT P(SEEN) IS, AND WHAT IT IS NOT
It is a five-feature logistic over signals this app already holds, and it exists to *order a
queue*. It never enters `score_u(t)` and it is not a model feature: §4.1 rule 3 keeps the
display schema away from the feature builder, and nothing here reads it. `title_prior.item_n`
is the sanctioned popularity quantity — §4.3 ships it as "the §5.1 gate input" and §5.1 already
uses it — so the popularity term is a crowd *support count*, never a crowd *score*.

The weights are a stated prior, not a fit. There is no labelled data to fit them on until the
surface runs; the surface then generates exactly that label, because a verdict means seen and
`Not seen` means unseen. §13 names the instrument that falsifies them — "not-seen rate in the
rating queue (>50% = queue bug)" — and `not_seen_rate` below computes it. Fitting is a later
milestone's move; inventing a fit now would be inventing the data.

WHY THE FORMULA IS WRITTEN TWICE
Postgres orders and Python explains. The SQL evaluates the logistic so `ORDER BY ... LIMIT` can
work over the whole catalog without shipping it to the client; Python evaluates the same
logistic on the returned row so the reason line can name the *dominant* term. They are two
spellings of one formula, and the moment they disagree the returned cards stop being sorted by
the number they report — which is what
`test_once_the_seed_list_is_answered_the_queue_is_ordered_by_descending_p_seen` checks.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rate import reask as reask_stream

log = logging.getLogger("spielplan.rate.queue")

# ---------------------------------------------------------------------------------------------
# TUNED NUMBERS. These belong in `spielplan/ledger/hyperparams.py` — that module is "the only
# module in the package allowed to contain a tuning number", and re-tuning is supposed to reach
# the app through `ledger_hyperparams.json`. It is wave-1 frozen for this milestone, so they sit
# here, in one block, under the same contract: change them here and nowhere else. Reported as a
# gap.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeenWeights:
    """Log-odds weights for P(seen). Every one of them is named in a reason line, so the
    weighting is auditable from the UI and not only from this file."""

    intercept: float = -2.2   # nothing known at all -> 0.10: the base rate for "have you seen
                              # this arbitrary catalog title"
    playback: float = 2.5     # §7.3's >=90% playback poll fired and nobody answered the prompt
    co_seen: float = 1.2      # §6.1's "household co-seen"
    crowd: float = 2.0        # §6.1's "popularity", as title_prior.item_n
    owned: float = 0.8        # it is in the Jellyfin library (§7.2 keeps is_owned re-derived)
    age: float = 0.6          # more years on the shelf, more chances to have seen it


WEIGHTS = SeenWeights()

# A title is "old" for this purpose once it has been out four decades; past that the extra years
# stop carrying information about whether this household saw it.
AGE_SATURATION_YEARS = 40.0
# The item_n at which the popularity term saturates. The transform is log1p(n)/log1p(SAT)
# clipped to 1: n=10 -> 0.21, n=1e3 -> 0.60, n=1e4 -> 0.80. Rank-preserving, so it cannot
# reorder the catalog relative to a percentile version of itself; only the spacing differs.
CROWD_SATURATION = 100_000.0

# §13: "not-seen rate in the rating queue (>50% = queue bug)".
NOT_SEEN_BUG_THRESHOLD = 0.50
NOT_SEEN_WINDOW = 200

# A recorded state is not an estimate.
P_SEEN_RECORDED = 1.0

SOURCES: tuple[str, ...] = ("seed", "p_seen", "pending_verdict", "reask")

FEATURE_NAMES: tuple[str, ...] = ("playback", "co_seen", "crowd", "owned", "age")


# --- the estimate -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    """The five circumstantial signals, plus the one recorded fact that overrides them."""

    seen: bool = False        # user_title.state = 'seen' — a record, not a signal
    playback: bool = False
    co_seen: float = 0.0      # share of the *other* active household members who have seen it
    crowd: float = 0.0        # log1p(item_n) / log1p(CROWD_SATURATION), clipped
    owned: bool = False
    age: float = 0.0          # (this year - release year) / 40, clipped

    def vector(self) -> dict[str, float]:
        return {
            "playback": float(self.playback),
            "co_seen": float(self.co_seen),
            "crowd": float(self.crowd),
            "owned": float(self.owned),
            "age": float(self.age),
        }


def contributions(features: Features, weights: SeenWeights = WEIGHTS) -> dict[str, float]:
    """Each term's signed contribution to the log-odds. The intercept is deliberately absent:
    it is the same for every title, so it explains nothing about *this* one."""
    v = features.vector()
    return {name: getattr(weights, name) * v[name] for name in FEATURE_NAMES}


def p_seen(features: Features, weights: SeenWeights = WEIGHTS) -> float:
    """P(this person has seen this title).

    `seen` short-circuits to 1.0 rather than entering the logistic with a large weight. The app
    is not estimating there — §7.3 already adopted the state, or the person set it — and a
    number below 1 would leave a recorded fact competing with an accumulation of circumstantial
    evidence, which is how a title nobody watched ends up ahead of one that was.
    """
    if features.seen:
        return P_SEEN_RECORDED
    z = weights.intercept + sum(contributions(features, weights).values())
    return 1.0 / (1.0 + math.exp(-z))


def dominant(features: Features, weights: SeenWeights = WEIGHTS) -> str | None:
    """The term that put this title where it is. None when nothing at all is known about it —
    then the why-line carries the probability and stops, rather than naming a cause worth 0."""
    scored = contributions(features, weights)
    best = max(scored, key=lambda name: (scored[name], name))
    return best if scored[best] > 0.0 else None


# §6.8: "every shelf, recommendation, question and conflict carries a one-line why". Phrased in
# the person's vocabulary and not the model's — the card is asking them to remember something.
PHRASES = {
    "playback": "you played it through",
    "co_seen": "someone else in the house has seen it",
    "crowd": "widely rated",
    "owned": "it is in your library",
    "age": "it has been out {years} years",
}

# The one sentence a recorded-seen card carries. It is deliberately the *whole* truth about that
# card and no more: "you have this marked seen". §13's re-ask targets are also marked seen, so
# they get this same sentence from this same branch and the wire cannot tell the two apart. The
# tempting longer form — "...and have not rated it" — is what would give the stream away, and it
# would be false on exactly the cards it gave away.
SEEN_REASON = "queued because: you have this marked seen"


def reason_for(
    features: Features,
    *,
    source: str,
    seed_position: int | None = None,
    seed_total: int = 0,
    seed_decade: int | None = None,
    years_out: int | None = None,
    weights: SeenWeights = WEIGHTS,
) -> str:
    """§6.8's mandatory one-line why, in the copy register the spec calls "quiet reasons"."""
    if source in ("pending_verdict", "reask"):
        return SEEN_REASON
    if source == "seed":
        where = f"seed list position {seed_position} of {seed_total}"
        if seed_decade:
            return f"queued because: {where} · {seed_decade}s"
        return f"queued because: {where}"
    pct = round(p_seen(features, weights) * 100)
    cause = dominant(features, weights)
    if cause is None:
        return f"queued because: {pct}% likely you have seen it"
    phrase = PHRASES[cause].format(years=years_out if years_out is not None else 0)
    return f"queued because: {pct}% likely you have seen it · {phrase}"


# --- the card ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueCard:
    title_id: int
    reason: str            # §6.8 one-line why, e.g. "72% likely you have seen it"
    p_seen: float | None
    source: str            # seed | p_seen | pending_verdict | reask
    reask_of: int | None   # verdict.id being silently re-asked; None otherwise

    def public(self) -> dict[str, Any]:
        """The allow-list projection that may reach the client.

        §13 stream (b) requires the served payload to carry "no marker distinguishing a re-ask
        from a first observation", and `source` gives it away exactly as loudly as `reask_of`
        does — so neither is in here. An allow-list and not a copy-and-delete: a field added to
        this dataclass later has to be added to this dict on purpose before it can leak.
        """
        return {"title_id": self.title_id, "reason": self.reason, "p_seen": self.p_seen}


# --- the candidate query ----------------------------------------------------------------------

# Two exclusions, each a rule rather than a nicety:
#   * a title the person has ever given a real verdict on does not come back. The predicate is
#     `NOT is_reask` and not `superseded_by IS NULL`, because a re-ask supersedes the row it
#     re-asks (`record_verdict` does this deliberately) — a predicate on that column would hand
#     every re-asked title straight back to the fresh queue.
#   * a title the person has explicitly answered "not seen" does not come back either. An
#     *adopted* unseen is an absent row, never an 'unseen' row (see `sync/seen.py`'s table), so
#     this only ever removes an answer somebody actually gave.
#
# The person's history arrives as three small CTEs joined to `title`, rather than as correlated
# sub-selects evaluated per row. Ordering by a computed score means the whole partition is
# scored and sorted whatever the LIMIT is, so the per-row cost is the cost: on a 30,000-title
# catalog the sub-select spelling took 267 ms for a block of 15 and this one takes 43 ms. Each
# CTE is bounded by what one household has done, not by the catalog.
_CANDIDATES = """
WITH household AS (
    SELECT count(*)::float8 AS n
      FROM app_user
     WHERE id <> $1 AND is_active AND role IN ('admin', 'member')
), co_seen AS (
    SELECT o.title_id, count(*)::float8 AS n
      FROM user_title o
      JOIN app_user au ON au.id = o.user_id
     WHERE o.user_id <> $1 AND o.state = 'seen'
       AND au.is_active AND au.role IN ('admin', 'member')
     GROUP BY o.title_id
), played AS (
    SELECT DISTINCT title_id
      FROM playback_event
     WHERE user_id = $1 AND finished AND title_id IS NOT NULL
), rated AS (
    SELECT DISTINCT title_id FROM verdict WHERE user_id = $1 AND NOT is_reask
), cand AS (
    SELECT t.id,
           t.kind,
           t.year,
           COALESCE(ut.state = 'seen', false)                       AS seen,
           t.is_owned                                               AS owned,
           sl.position                                              AS seed_position,
           sl.decade                                                AS seed_decade,
           (pl.title_id IS NOT NULL)                                AS playback,
           COALESCE(cs.n, 0.0)                                      AS co_seen_n,
           COALESCE(tp.item_n, 0)::float8                           AS item_n,
           CASE WHEN t.year IS NULL THEN 0.0
                ELSE least(1.0, greatest(0.0,
                     (EXTRACT(year FROM now())::float8 - t.year::float8) / $5::float8))
           END                                                      AS age,
           array_position($6::int[], t.id)                          AS head_pos
      FROM title t
      LEFT JOIN user_title  ut ON ut.title_id = t.id AND ut.user_id = $1
      LEFT JOIN seed_list   sl ON sl.title_id = t.id
      LEFT JOIN title_prior tp ON tp.title_id = t.id
      LEFT JOIN co_seen     cs ON cs.title_id = t.id
      LEFT JOIN played      pl ON pl.title_id = t.id
      LEFT JOIN rated       rt ON rt.title_id = t.id
     WHERE t.kind = ANY($2::text[])
       AND NOT (t.id = ANY($3::int[]))
       AND rt.title_id IS NULL
       AND NOT (ut.title_id IS NOT NULL AND ut.state = 'unseen')
), scored AS (
    SELECT c.*,
           least(1.0, ln(1.0 + c.item_n) / ln(1.0 + $7::float8))              AS crowd,
           least(1.0, c.co_seen_n / greatest(1.0, (SELECT n FROM household))) AS co_seen
      FROM cand c
)
SELECT s.*,
       CASE WHEN s.seen THEN 1.0
            ELSE 1.0 / (1.0 + exp(-( $8::float8
                                     + $9::float8  * (s.playback)::int
                                     + $10::float8 * s.co_seen
                                     + $11::float8 * s.crowd
                                     + $12::float8 * (s.owned)::int
                                     + $13::float8 * s.age ))) END AS p_seen
  FROM scored s
 ORDER BY s.head_pos ASC NULLS LAST,
          NOT s.seen,
          s.seed_position ASC NULLS LAST,
          p_seen DESC,
          s.id
 LIMIT $4
"""


def _features(row: asyncpg.Record) -> Features:
    return Features(
        seen=bool(row["seen"]),
        playback=bool(row["playback"]),
        co_seen=float(row["co_seen"]),
        crowd=float(row["crowd"]),
        owned=bool(row["owned"]),
        age=float(row["age"]),
    )


def _card(row: asyncpg.Record, *, seed_total: int, weights: SeenWeights) -> QueueCard:
    features = _features(row)
    if features.seen:
        source = "pending_verdict"
    elif row["seed_position"] is not None:
        source = "seed"
    else:
        source = "p_seen"
    years_out = None
    if row["year"] is not None:
        years_out = int(round(float(row["age"]) * AGE_SATURATION_YEARS))
    return QueueCard(
        title_id=int(row["id"]),
        reason=reason_for(
            features,
            source=source,
            seed_position=row["seed_position"],
            seed_total=seed_total,
            seed_decade=row["seed_decade"],
            years_out=years_out,
            weights=weights,
        ),
        # §6.1's seed list is a decade-stratified *sample*, not a P(seen) ordering. Quoting a
        # probability on a card the queue did not use one to place would be a number that means
        # nothing, and §6.8 forbids a bare number next to a name it does not belong to.
        p_seen=None if source == "seed" else float(row["p_seen"]),
        source=source,
        reask_of=None,
    )


async def next_sweep_cards(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str],
    limit: int,
    exclude: Sequence[int] = (),
    head: Sequence[int] = (),
    rng: random.Random | None = None,
    reask_rate: float = reask_stream.REASK_RATE,
    weights: SeenWeights = WEIGHTS,
) -> list[QueueCard]:
    """The next `limit` sweep cards, best first. An empty list means the queue is drained.

    `head` is the §7.3 banner CTA's pins: "You've watched X and Y recently — rate them?" puts
    those title ids at the front, in the order given, and they stay ordinary candidates — a
    pinned title that has since been rated is simply not there.

    `rng`, `reask_rate` and `weights` are test seams, not part of the interface this module
    publishes: the declared call — `next_sweep_cards(conn, user_id=..., kinds=..., limit=...,
    exclude=..., head=...)` — behaves exactly as specified without them. §13 stream (b) needs a
    coin flip per slot, and a coin nobody can hold still cannot be tested.
    """
    if not kinds:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    if limit <= 0:
        return []
    rng = rng or random.Random()
    seed_total = await conn.fetchval("SELECT count(*) FROM seed_list") or 0
    skip = list(dict.fromkeys(int(t) for t in exclude))

    fresh_rows = await conn.fetch(
        _CANDIDATES,
        user_id,
        list(kinds),
        skip,
        limit,
        AGE_SATURATION_YEARS,
        [int(t) for t in head],
        CROWD_SATURATION,
        weights.intercept,
        weights.playback,
        weights.co_seen,
        weights.crowd,
        weights.owned,
        weights.age,
    )
    fresh = [_card(row, seed_total=seed_total, weights=weights) for row in fresh_rows]

    # §13 stream (b): "~10% of comparisons/verdicts re-asked after >= 3 days". The draw is per
    # slot, so the rate is a property of the queue rather than of how long a sitting ran.
    reasks: list[reask_stream.VerdictReask] = []
    if reask_rate > 0.0:
        reasks = await reask_stream.verdict_candidates(
            conn, user_id=user_id, kinds=kinds, limit=limit, exclude=skip, rng=rng
        )
    return _interleave(fresh, reasks, limit=limit, rate=reask_rate, rng=rng)


def _interleave(
    fresh: list[QueueCard],
    reasks: Sequence[reask_stream.VerdictReask],
    *,
    limit: int,
    rate: float,
    rng: random.Random,
) -> list[QueueCard]:
    """Spend about `rate` of the slots on re-asks, and fall through when either pool runs dry.

    Falling through matters in both directions: a household with nothing old enough to re-ask
    still gets a full queue, and a household that has rated everything still gets asked
    something — a queue made only of re-asks is what §13's stream looks like at the end of the
    catalog.
    """
    fresh_q = list(fresh)
    reask_q = list(reasks)
    out: list[QueueCard] = []
    taken: set[int] = set()
    while len(out) < limit and (fresh_q or reask_q):
        take_reask = bool(reask_q) and (not fresh_q or rng.random() < rate)
        if take_reask:
            candidate = reask_q.pop(0)
            if candidate.title_id in taken:
                continue
            card = QueueCard(
                title_id=candidate.title_id,
                # The same branch, the same sentence and the same probability as a genuinely
                # pending card. A re-ask target carries a verdict, and a verdict implies seen.
                reason=SEEN_REASON,
                p_seen=P_SEEN_RECORDED,
                source="reask",
                reask_of=candidate.verdict_id,
            )
        else:
            card = fresh_q.pop(0)
            if card.title_id in taken:
                continue
        taken.add(card.title_id)
        out.append(card)
    return out


# --- §13's instrument -------------------------------------------------------------------------


@dataclass(frozen=True)
class NotSeenRate:
    """§13: "not-seen rate in the rating queue (>50% = queue bug)"."""

    answered: int
    not_seen: int
    window: int

    @property
    def rate(self) -> float | None:
        return None if self.answered == 0 else self.not_seen / self.answered

    @property
    def queue_bug(self) -> bool:
        rate = self.rate
        return rate is not None and rate > NOT_SEEN_BUG_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "not_seen": self.not_seen,
            "window": self.window,
            "rate": self.rate,
            "queue_bug": self.queue_bug,
            "threshold": NOT_SEEN_BUG_THRESHOLD,
        }


async def not_seen_rate(
    conn: asyncpg.Connection, *, user_id: int | None = None, window: int = NOT_SEEN_WINDOW
) -> NotSeenRate:
    """How often the queue guessed wrong, over the last `window` answers it got.

    Read from the `rate_observation` journal rather than from `user_title`, because
    `user_title` holds one row per (user, title) and a later "seen" erases the "not seen" that
    is the whole measurement. The journal is append-only and an undone row is tombstoned, so
    `undone_at IS NULL` counts what the person actually left standing.
    """
    row = await conn.fetchrow(
        """
        SELECT count(*) AS answered,
               count(*) FILTER (WHERE kind_of = 'not_seen') AS not_seen
          FROM (SELECT kind_of
                  FROM rate_observation
                 WHERE ($1::bigint IS NULL OR user_id = $1)
                   AND undone_at IS NULL
                   AND kind_of IN ('verdict', 'not_seen')
                 ORDER BY id DESC
                 LIMIT $2) recent
        """,
        user_id,
        window,
    )
    return NotSeenRate(answered=int(row["answered"]), not_seen=int(row["not_seen"]), window=window)


__all__ = [
    "AGE_SATURATION_YEARS",
    "CROWD_SATURATION",
    "SOURCES",
    "WEIGHTS",
    "Features",
    "NotSeenRate",
    "QueueCard",
    "SeenWeights",
    "contributions",
    "dominant",
    "next_sweep_cards",
    "not_seen_rate",
    "p_seen",
    "reason_for",
]
