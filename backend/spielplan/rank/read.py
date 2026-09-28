"""The database side of §6.3's board: the queries that feed the pure `board` and `queue`.

"Every rated title" is `ledger_state.observed`; the assigned tier is the latest `tier_edit`. Selector
inputs exclude §13's held-out stream.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg
import numpy as np

from spielplan.db.library import RankFilters, rank_filters
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import (
    DEFAULT_TIER_SET,
    HELD_OUT,
    latest_tier_edit_sql,
    rescale_level,
)
from spielplan.rank import board, queue

log = logging.getLogger("spielplan.rank.read")


@dataclass(frozen=True)
class Cutpoints:
    boundaries: np.ndarray
    tier_set: tuple[str, ...]
    # `refit_requested_at` is set (decision 209); read with the boundaries to save a round trip.
    refit_owed: bool = False


async def cutpoints_of(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> Cutpoints:
    """The fitted boundaries, or §6.3's prior shape when there is no row (and no refit asked for)."""
    row = await conn.fetchrow(
        "SELECT boundaries, tier_set, refit_requested_at FROM ledger_cutpoints "
        "WHERE user_id = $1 AND kind = $2",
        user_id,
        kind,
    )
    if row is None:
        from spielplan.ledger import model

        return Cutpoints(
            boundaries=model.initial_cutpoints(len(DEFAULT_TIER_SET)),
            tier_set=DEFAULT_TIER_SET,
        )
    return Cutpoints(
        boundaries=np.asarray([float(b) for b in row["boundaries"]], dtype=float),
        tier_set=tuple(row["tier_set"]),
        refit_owed=row["refit_requested_at"] is not None,
    )


async def items(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    filters: RankFilters | None = None,
) -> list[board.Item]:
    """§6.3's "every rated title", filtered, with the displayed (freshness-inflated) σ."""
    where, args = rank_filters(kind=kind, user_id=user_id, filters=filters)
    user = f"${len(args) + 1}"
    rows = await conn.fetch(
        f"""
        SELECT ls.title_id, t.name, t.year, ls.s, COALESCE(ls.sigma_eff, ls.sigma) AS sigma,
               te.tier AS assigned_tier, te.n_levels AS assigned_k, lv.value AS verdict,
               -- The K the drop is being READ against, in the same round trip as the drop: a
               -- rescale that depends on a second call is a rescale a caller can forget, and this
               -- file already argues (see `Cutpoints.refit_owed`) that a board read does not get a
               -- second query for one scalar. It is uncorrelated, so it runs once.
               (SELECT cardinality(c.tier_set) FROM ledger_cutpoints c
                 WHERE c.user_id = {user} AND c.kind = ${len(args) + 2}) AS tier_set_k
        FROM ledger_state ls
        JOIN title t ON t.id = ls.title_id
        LEFT JOIN ({latest_tier_edit_sql(user)}) te ON te.title_id = ls.title_id
        -- The live verdict, which holds the model tier inside its band (decision 508). The same
        -- reading as `observations.LIVE_LABEL_SQL`: the latest non-re-ask row per title.
        LEFT JOIN (
            SELECT DISTINCT ON (title_id) title_id, value
            FROM verdict
            WHERE user_id = {user} AND NOT is_reask
            ORDER BY title_id, created_at DESC, id DESC
        ) lv ON lv.title_id = ls.title_id
        WHERE ls.user_id = {user} AND ls.kind = ${len(args) + 2} AND ls.observed
          AND {where}
        ORDER BY ls.s DESC, ls.title_id
        """,
        *args,
        user_id,
        kind,
    )
    # Decision 11: the stored index is re-read against today's set. NULL `tier_set_k` means the
    # default set, as in `cutpoints_of`.
    return [
        board.Item(
            title_id=int(r["title_id"]),
            name=str(r["name"]),
            s=float(r["s"]),
            sigma=float(r["sigma"]),
            assigned_tier=(
                None
                if r["assigned_tier"] is None
                else rescale_level(
                    int(r["assigned_tier"]),
                    k_from=r["assigned_k"],
                    k_to=int(r["tier_set_k"] or len(DEFAULT_TIER_SET)),
                )
            ),
            verdict=None if r["verdict"] is None else int(r["verdict"]),
            year=r["year"],
        )
        for r in rows
    ]


async def comparison_counts(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> dict[int, int]:
    """How many comparisons each title carries, **excluding §13's held-out stream** (a selector input)."""
    rows = await conn.fetch(
        """
        SELECT side.title_id, count(*) AS n
        FROM (
            SELECT d.title_a AS title_id FROM duel d
            WHERE d.user_id = $1 AND d.selection <> $3
            UNION ALL
            SELECT d.title_b FROM duel d
            WHERE d.user_id = $1 AND d.selection <> $3
        ) side
        JOIN title t ON t.id = side.title_id AND t.kind = $2
        GROUP BY side.title_id
        """,
        user_id,
        kind,
        HELD_OUT,
    )
    return {int(r["title_id"]): int(r["n"]) for r in rows}


async def asked_pairs(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> set[frozenset[int]]:
    """The unordered pairs this person has already judged in any context, **held-out excluded**.

    Neither adaptive arm re-serves one. Both sides are joined: a re-import can reclassify one side.
    """
    rows = await conn.fetch(
        """
        SELECT d.title_a, d.title_b FROM duel d
        JOIN title ta ON ta.id = d.title_a AND ta.kind = $2
        JOIN title tb ON tb.id = d.title_b AND tb.kind = $2
        WHERE d.user_id = $1 AND d.selection <> $3
        """,
        user_id,
        kind,
        HELD_OUT,
    )
    return {frozenset((int(r["title_a"]), int(r["title_b"]))) for r in rows}


async def recent_titles(
    conn: asyncpg.Connection, *, user_id: int, kind: str, window: int = queue.RECENT_WINDOW
) -> set[int]:
    """The titles of this person's last `window` answered queue pairs, **held-out excluded**.

    Decision 494's no-repeat window; `tier_queue` only, since it is about the current sitting.
    """
    rows = await conn.fetch(
        """
        SELECT d.title_a, d.title_b FROM duel d
        JOIN title ta ON ta.id = d.title_a AND ta.kind = $2
        JOIN title tb ON tb.id = d.title_b AND tb.kind = $2
        WHERE d.user_id = $1 AND d.context = 'tier_queue' AND d.selection <> $3
        ORDER BY d.id DESC
        LIMIT $4
        """,
        user_id,
        kind,
        HELD_OUT,
        window,
    )
    return {int(r[side]) for r in rows for side in ("title_a", "title_b")}


async def answered_comparisons(
    conn: asyncpg.Connection, *, user_id: int, kind: str, context: str = "tier_queue"
) -> int:
    """How many answers this person has given for this kind in one duel context.

    Sealed with each pair, so a replayed seal names a count that has moved on. One context only:
    a drop mid-queue must not invalidate the pair on screen.
    """
    return int(
        await conn.fetchval(
            """
            SELECT count(*) FROM duel d
            JOIN title t ON t.id = d.title_a AND t.kind = $2
            WHERE d.user_id = $1 AND d.context = $3
            """,
            user_id,
            kind,
            context,
        )
        or 0
    )


async def load(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    filters: RankFilters | None = None,
) -> tuple[tuple[board.Tier, ...], Cutpoints, list[board.Item]]:
    """The board, its boundaries and the items behind it (the queue draws from the same items)."""
    cuts = await cutpoints_of(conn, user_id=user_id, kind=kind)
    rows = await items(conn, user_id=user_id, kind=kind, filters=filters)
    tiers = board.build(rows, cuts=cuts.boundaries, tier_set=cuts.tier_set, hp=hp)
    return tiers, cuts, rows


async def placements(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    title_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """Where these titles sit on the whole UNFILTERED board now, as the board's own public rows.

    Placement, never "moved": a held-out answer is never refitted, and "unchanged" would name it.
    """
    tiers, _cuts, _rows = await load(conn, user_id=user_id, kind=kind, hp=hp)
    by_id = {entry.title_id: entry for tier in tiers for entry in tier.entries}
    return [by_id[int(t)].public() for t in title_ids if int(t) in by_id]


async def candidates(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    rows: Sequence[board.Item] | None = None,
) -> list[queue.Candidate]:
    """The queue's pool: the whole rated board, unfiltered (filters only change the view)."""
    cuts = await cutpoints_of(conn, user_id=user_id, kind=kind)
    pool = list(rows) if rows is not None else await items(conn, user_id=user_id, kind=kind)
    return queue.candidates(
        pool,
        cuts=cuts.boundaries,
        tier_set=cuts.tier_set,
        hp=hp,
        comparisons=await comparison_counts(conn, user_id=user_id, kind=kind),
    )


async def names_for(
    conn: asyncpg.Connection, title_ids: Sequence[int]
) -> dict[int, str]:
    """Titles by id, for the §6.7 log line — which names entities, not row ids (proposal 120)."""
    rows = await conn.fetch(
        "SELECT id, name FROM title WHERE id = ANY($1::int[])", [int(t) for t in title_ids]
    )
    return {int(r["id"]): str(r["name"]) for r in rows}


async def cards_for(
    conn: asyncpg.Connection, title_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """What a pair shows of each title: the poster's id, the name and the meta line's facts."""
    rows = await conn.fetch(
        "SELECT id, kind, name, year, runtime_min FROM title WHERE id = ANY($1::int[])",
        [int(t) for t in title_ids],
    )
    return {int(r["id"]): dict(r) for r in rows}


def public(tiers: Sequence[board.Tier], per_tier: int | None = None) -> list[dict[str, Any]]:
    return [
        {
            "index": tier.index,
            "label": tier.label,
            "verdict": tier.verdict,
            "count": len(tier.entries),
            "entries": [entry.public() for entry in tier.entries[:per_tier]],
        }
        for tier in tiers
    ]


__all__ = [
    "Cutpoints",
    "answered_comparisons",
    "asked_pairs",
    "candidates",
    "cards_for",
    "comparison_counts",
    "cutpoints_of",
    "items",
    "load",
    "names_for",
    "placements",
    "public",
    "recent_titles",
]
