"""The §6.1 sweep queue: which title to ask about next, and the one line saying why.

Order: pinned, recorded-seen, seed list (decision 490), then P(seen), a stated-prior logistic
that only orders the queue. The SQL and `p_seen` spell one formula; keep them in step.
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


@dataclass(frozen=True)
class SeenWeights:
    """Log-odds weights for P(seen): a stated prior, not a fit."""

    intercept: float = -2.2   # nothing known at all -> 0.10
    playback: float = 2.5     # §7.3's >=90% playback poll fired and nobody answered the prompt
    co_seen: float = 1.2      # §6.1's "household co-seen"
    crowd: float = 2.0        # §6.1's "popularity", as title_prior.item_n
    owned: float = 0.8        # it is in the Jellyfin library (§7.2 keeps is_owned re-derived)
    age: float = 0.6          # more years on the shelf, more chances to have seen it
    unfamiliar: float = 2.0   # the feature is <= 0, so this only ever lowers P(seen)


WEIGHTS = SeenWeights()

# Pseudo-count shrinking a person's own answers towards their own seen rate for the kind.
FAMILIAR_PSEUDO = 2.0

# Years past which a title's age stops carrying information.
AGE_SATURATION_YEARS = 40.0
# log1p(n)/log1p(SAT) clipped to 1: n=10 -> 0.21, n=1e3 -> 0.60, n=1e4 -> 0.80.
CROWD_SATURATION = 100_000.0

# §13: "not-seen rate in the rating queue (>50% = queue bug)".
NOT_SEEN_BUG_THRESHOLD = 0.50
NOT_SEEN_WINDOW = 200

# A recorded state is not an estimate.
P_SEEN_RECORDED = 1.0

SOURCES: tuple[str, ...] = ("pinned", "seed", "p_seen", "pending_verdict", "reask")

FEATURE_NAMES: tuple[str, ...] = ("playback", "co_seen", "crowd", "owned", "age", "unfamiliar")

# log1p(1000)/log1p(CROWD_SATURATION) = 0.60: below it member copy never says "well-known".
WELL_KNOWN_CROWD = 0.6


# --- the estimate -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    """The six circumstantial signals, plus the one recorded fact that overrides them."""

    seen: bool = False        # user_title.state = 'seen' — a record, not a signal
    playback: bool = False
    co_seen: float = 0.0      # share of the *other* active household members who have seen it
    crowd: float = 0.0        # log1p(item_n) / log1p(CROWD_SATURATION), clipped
    owned: bool = False
    age: float = 0.0          # (this year - release year) / 40, clipped
    unfamiliar: float = 0.0   # -2..0: `unfamiliarity` of this person's answers in its language

    def vector(self) -> dict[str, float]:
        return {
            "playback": float(self.playback),
            "co_seen": float(self.co_seen),
            "crowd": float(self.crowd),
            "owned": float(self.owned),
            "age": float(self.age),
            "unfamiliar": float(self.unfamiliar),
        }


def unfamiliarity(
    seen: float,
    answered: float,
    kind_seen: float,
    kind_answered: float,
    pseudo: float = FAMILIAR_PSEUDO,
) -> float:
    """How firmly this person's own answers say they do not know titles of one language and kind.

    The group's seen share, shrunk by 2 x `pseudo` answers towards the person's own seen rate for
    the kind, read only below that rate and doubled: 0 at or above it, -2 x rate at the limit.
    Never positive, so the why-line never names it (decision 521).
    """
    rate = kind_seen / kind_answered if kind_answered > 0 else 0.5
    share = (seen + 2.0 * pseudo * rate) / (answered + 2.0 * pseudo)
    return min(0.0, share - rate) * 2.0


def contributions(features: Features, weights: SeenWeights = WEIGHTS) -> dict[str, float]:
    """Each term's signed log-odds contribution; the intercept is the same for every title."""
    v = features.vector()
    return {name: getattr(weights, name) * v[name] for name in FEATURE_NAMES}


def p_seen(features: Features, weights: SeenWeights = WEIGHTS) -> float:
    """P(this person has seen this title); a recorded `seen` is 1.0, not a large weight."""
    if features.seen:
        return P_SEEN_RECORDED
    z = weights.intercept + sum(contributions(features, weights).values())
    return 1.0 / (1.0 + math.exp(-z))


def dominant(features: Features, weights: SeenWeights = WEIGHTS) -> str | None:
    """The term that put this title where it is, or None when no term is positive."""
    scored = contributions(features, weights)
    best = max(scored, key=lambda name: (scored[name], name))
    return best if scored[best] > 0.0 else None


# §6.8's one-line why, in the person's vocabulary; no model number on the card (decision 486).
PHRASES = {
    "playback": "You played it to the end.",
    "co_seen": "Someone else in the house has seen it.",
    "crowd": "A well-known {noun}.",
    "owned": "It's in your library.",
    "age": "It has been out {years}.",
}

# When no term can be named truthfully.
UNSURE_REASON = "One you might have seen."

# §13's re-ask targets get this same sentence from the same branch, so the wire cannot tell them
# apart; "...and have not rated it" would give the stream away.
SEEN_REASON = "You have this marked as seen."

PINNED_REASON = "You picked this one."


def reason_for(
    features: Features,
    *,
    source: str,
    kind: str = "movie",
    seed_decade: int | None = None,
    years_out: int | None = None,
    weights: SeenWeights = WEIGHTS,
) -> str:
    """§6.8's mandatory one-line why, in the member register and with no number in it.

    A seed card says "well-known" only where the crowd term bears it out (`WELL_KNOWN_CROWD`).
    """
    if source in ("pending_verdict", "reask"):
        return SEEN_REASON
    if source == "pinned":
        return PINNED_REASON
    noun = "series" if kind == "series" else "film"
    known = features.crowd >= WELL_KNOWN_CROWD
    if source == "seed":
        when = f" from the {seed_decade}s" if seed_decade else ""
        if known:
            return f"A well-known {noun}{when}."
        return f"A {noun}{when} we ask everyone about first."
    cause = dominant(features, weights)
    if cause == "crowd" and not known:
        # Too small to call well-known: name the strongest term that is true instead.
        scored = contributions(features, weights)
        rest = [name for name in scored if name != "crowd" and scored[name] > 0.0]
        cause = max(rest, key=lambda name: (scored[name], name)) if rest else None
    if cause is None:
        return UNSURE_REASON
    years = years_out if years_out is not None else 0
    return PHRASES[cause].format(years="1 year" if years == 1 else f"{years} years", noun=noun)


# --- the card ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueCard:
    title_id: int
    reason: str            # §6.8 one-line why, e.g. "It's in your library."
    p_seen: float | None
    source: str            # seed | p_seen | pending_verdict | reask
    reask_of: int | None   # verdict.id being silently re-asked; None otherwise

    def public(self) -> dict[str, Any]:
        """The allow-list projection that may reach the client.

        No `source` and no `reask_of`: either would mark a §13 re-ask on the wire.
        """
        return {"title_id": self.title_id, "reason": self.reason, "p_seen": self.p_seen}


# --- the candidate query ----------------------------------------------------------------------

# A rated title never returns (`NOT is_reask`: a re-ask supersedes the row it re-asks); a "not
# seen" answer returns only when pinned. History arrives as CTEs rather than per-row sub-selects,
# because the whole partition is scored whatever the LIMIT.
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
), familiar AS (
    -- This person's own answers per kind and original language: `unfamiliarity`'s two counts.
    -- An 'unseen' row is an answer somebody gave (an adopted unseen is an absent row).
    SELECT t.kind, t.original_language AS lang,
           count(*) FILTER (WHERE ut.state = 'seen')::float8 AS seen_n,
           count(*)::float8                                   AS answered_n
      FROM user_title ut
      JOIN title t ON t.id = ut.title_id
     WHERE ut.user_id = $1 AND t.original_language IS NOT NULL
     GROUP BY t.kind, t.original_language
), familiar_kind AS (
    -- The same rows per kind: the person's own seen rate each language is measured against.
    SELECT kind, sum(seen_n) / sum(answered_n) AS rate
      FROM familiar
     GROUP BY kind
), cand AS (
    SELECT t.id,
           t.kind,
           t.year,
           -- The clock the printed age is counted on, selected rather than read off
           -- `datetime.now()` in Python: `age` below is clipped to 1.0 because that is where the
           -- *feature* saturates, so the why-line cannot be derived from it and has to subtract
           -- years itself. Two clocks would make the sentence disagree with the ordering it
           -- explains across a midnight or a mis-set container TZ; both `now()` calls in this
           -- statement are the one transaction timestamp. [M4.10 finding 19, §6.8]
           EXTRACT(year FROM now())::int                             AS this_year,
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
           COALESCE(fa.seen_n, 0.0)                                 AS lang_seen_n,
           COALESCE(fa.answered_n, 0.0)                             AS lang_answered_n,
           COALESCE(fk.rate, 0.5)                                   AS kind_rate,
           array_position($6::int[], t.id)                          AS head_pos
      FROM title t
      LEFT JOIN user_title  ut ON ut.title_id = t.id AND ut.user_id = $1
      LEFT JOIN seed_list   sl ON sl.title_id = t.id
      LEFT JOIN title_prior tp ON tp.title_id = t.id
      LEFT JOIN co_seen     cs ON cs.title_id = t.id
      LEFT JOIN played      pl ON pl.title_id = t.id
      LEFT JOIN rated       rt ON rt.title_id = t.id
      LEFT JOIN familiar    fa ON fa.kind = t.kind AND fa.lang = t.original_language
      LEFT JOIN familiar_kind fk ON fk.kind = t.kind
     WHERE t.kind = ANY($2::text[])
       AND NOT (t.id = ANY($3::int[]))
       AND rt.title_id IS NULL
       AND (t.id = ANY($6::int[]) OR NOT (ut.title_id IS NOT NULL AND ut.state = 'unseen'))
), scored AS (
    SELECT c.*,
           least(1.0, ln(1.0 + c.item_n) / ln(1.0 + $7::float8))              AS crowd,
           least(1.0, c.co_seen_n / greatest(1.0, (SELECT n FROM household))) AS co_seen,
           least(0.0, (c.lang_seen_n + 2.0 * $15::float8 * c.kind_rate)
                      / (c.lang_answered_n + 2.0 * $15::float8) - c.kind_rate) * 2.0
                                                                          AS unfamiliar
      FROM cand c
)
SELECT s.*,
       CASE WHEN s.seen THEN 1.0
            ELSE 1.0 / (1.0 + exp(-( $8::float8
                                     + $9::float8  * (s.playback)::int
                                     + $10::float8 * s.co_seen
                                     + $11::float8 * s.crowd
                                     + $12::float8 * (s.owned)::int
                                     + $13::float8 * s.age
                                     + $14::float8 * s.unfamiliar ))) END AS p_seen
  FROM scored s
 ORDER BY s.head_pos ASC NULLS LAST,
          NOT s.seen,
          -- Decision 490: the seed list still leads, and inside it P(seen) decides.
          s.seed_position IS NULL,
          p_seen DESC,
          s.seed_position ASC NULLS LAST,
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
        unfamiliar=float(row["unfamiliar"]),
    )


def _card(row: asyncpg.Record, *, weights: SeenWeights) -> QueueCard:
    features = _features(row)
    if features.seen:
        source = "pending_verdict"
    elif row["head_pos"] is not None:
        source = "pinned"
    elif row["seed_position"] is not None:
        source = "seed"
    else:
        source = "p_seen"
    years_out = None
    if row["year"] is not None:
        # From the year, not from `age`, which is clipped at AGE_SATURATION_YEARS.
        years_out = max(0, int(row["this_year"]) - int(row["year"]))
    return QueueCard(
        title_id=int(row["id"]),
        reason=reason_for(
            features,
            source=source,
            kind=str(row["kind"]),
            seed_decade=row["seed_decade"],
            years_out=years_out,
            weights=weights,
        ),
        p_seen=float(row["p_seen"]),
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

    `head` pins titles to the front in order (§6.0's banner, search, "Rate it"); a pin lifts an
    earlier "not seen", and `exclude` still wins over it. `rng`, `reask_rate` and `weights` are
    test seams.
    """
    if not kinds:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    if limit <= 0:
        return []
    rng = rng or random.Random()
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
        weights.unfamiliar,
        FAMILIAR_PSEUDO,
    )
    fresh = [_card(row, weights=weights) for row in fresh_rows]

    # §13 stream (b): the draw is per slot, so the rate is a property of the queue.
    reasks: list[reask_stream.VerdictReask] = []
    if reask_rate > 0.0:
        reasks = await reask_stream.verdict_candidates(
            conn, user_id=user_id, kinds=kinds, limit=limit, exclude=skip, rng=rng
        )
    return _interleave(
        fresh, reasks, limit=limit, rate=reask_rate, rng=rng, head=tuple(head)
    )


def _interleave(
    fresh: list[QueueCard],
    reasks: Sequence[reask_stream.VerdictReask],
    *,
    limit: int,
    rate: float,
    rng: random.Random,
    head: Sequence[int] = (),
) -> list[QueueCard]:
    """Spend about `rate` of the slots on re-asks, and fall through when either pool runs dry.

    A pinned card is never traded for a re-ask: §6.0's banner must serve the titles it names.
    """
    fresh_q = list(fresh)
    reask_q = list(reasks)
    pinned = set(head)
    out: list[QueueCard] = []
    taken: set[int] = set()
    while len(out) < limit and (fresh_q or reask_q):
        next_is_pinned = bool(fresh_q) and fresh_q[0].title_id in pinned
        take_reask = (
            bool(reask_q) and not next_is_pinned and (not fresh_q or rng.random() < rate)
        )
        if take_reask:
            candidate = reask_q.pop(0)
            if candidate.title_id in taken:
                continue
            card = QueueCard(
                title_id=candidate.title_id,
                # Same sentence and probability as a pending card: a re-ask is indistinguishable.
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

    From the append-only journal: in `user_title` a later "seen" erases the "not seen" measured.
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
    "unfamiliarity",
]
