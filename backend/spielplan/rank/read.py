"""The database side of §6.3's board. Spec v2.1 §6.3, §4.1 rules 1/2/5, §4.2, §5.2, §13.

`board` and `queue` are pure; this is the query that feeds them. Four things come out of the
database and each is a rule rather than a convenience:

  **"every rated title"** (§6.3) is `ledger_state.observed` — exactly "this person has an
  observation of some arm on this title". An owned title nobody has rated has a coordinate
  (§12's M2 exit criterion) and does not belong on a tier list.

  **The assigned tier** is the person's most recent `tier_edit`, and it is read here rather
  than derived, because §6.3's "stays in the assigned tier" is a statement about the last thing
  they did and not about the fit.

  **Comparison counts exclude the held-out stream.** §13: the uniform-random 10% feeds neither
  the selection rule nor any quality figure, and `queue._exploration` picks by exactly this
  number — so counting held-out rows here would make the selector a reader of the evaluation
  stream, quietly, in a way no test of `queue` could see.

  **The filters are §6.3's six**, and two of them are traps. A DNA predicate goes through the
  `dna_tagged` view (0004), the only sanctioned union, because §4.1 rule 1 forbids answering it
  from anything that has lost the `tier` discriminator; and it never puts a threshold on
  `salience`, `confidence` or `n_sources`, because rule 2 forbids that and a 0.5 cut deletes
  44% of the extracted tier.
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
from spielplan.ledger.observations import DEFAULT_TIER_SET, HELD_OUT
from spielplan.rank import board, queue

log = logging.getLogger("spielplan.rank.read")


@dataclass(frozen=True)
class Cutpoints:
    boundaries: np.ndarray
    tier_set: tuple[str, ...]


async def cutpoints_of(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> Cutpoints:
    """The fitted boundaries, or §6.3's prior shape when nobody has been fitted yet.

    §5.2: the tier arm's cutpoints *are* the displayed boundaries, so there is no second set
    and no percentile fallback — the fallback when there is no row at all is the same prior the
    model would start from, which is not the same thing as cutting the current population.
    """
    row = await conn.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = $2",
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
    )


async def items(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    filters: RankFilters | None = None,
) -> list[board.Item]:
    """§6.3's "every rated title", filtered.

    `sigma_eff` rather than `sigma`: §5.2's freshness rule inflates the *displayed* σ after
    twelve untouched months, and the badges are a claim about how sure the model is now.
    """
    where, args = rank_filters(kind=kind, user_id=user_id, filters=filters)
    rows = await conn.fetch(
        f"""
        SELECT ls.title_id, t.name, ls.s, COALESCE(ls.sigma_eff, ls.sigma) AS sigma,
               (
                 SELECT te.tier FROM tier_edit te
                 WHERE te.user_id = ls.user_id AND te.title_id = ls.title_id
                 ORDER BY te.created_at DESC, te.id DESC LIMIT 1
               ) AS assigned_tier
        FROM ledger_state ls
        JOIN title t ON t.id = ls.title_id
        WHERE ls.user_id = ${len(args) + 1} AND ls.kind = ${len(args) + 2} AND ls.observed
          AND {where}
        ORDER BY ls.s DESC, ls.title_id
        """,
        *args,
        user_id,
        kind,
    )
    return [
        board.Item(
            title_id=int(r["title_id"]),
            name=str(r["name"]),
            s=float(r["s"]),
            sigma=float(r["sigma"]),
            assigned_tier=None if r["assigned_tier"] is None else int(r["assigned_tier"]),
        )
        for r in rows
    ]


async def comparison_counts(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> dict[int, int]:
    """How many comparisons each title carries, **excluding §13's held-out stream**.

    `queue._exploration` picks the least-compared title, so this number is a selector input.
    §13 says the held-out 10% feeds neither the selection rule nor any quality figure, and the
    only place that can be enforced is the query that produces the input — a selector cannot
    decline to read a number it was handed.
    """
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


async def load(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    filters: RankFilters | None = None,
) -> tuple[tuple[board.Tier, ...], Cutpoints, list[board.Item]]:
    """The board, its boundaries and the items behind it, in one place.

    The items travel back with the tiers because the comparison queue draws from the *same*
    population §6.3 badges — that identity is the point of proposal 157, and handing the queue
    a differently-filtered list is the shape the bug would take.
    """
    cuts = await cutpoints_of(conn, user_id=user_id, kind=kind)
    rows = await items(conn, user_id=user_id, kind=kind, filters=filters)
    tiers = board.build(rows, cuts=cuts.boundaries, tier_set=cuts.tier_set, hp=hp)
    return tiers, cuts, rows


async def candidates(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    rows: Sequence[board.Item] | None = None,
) -> list[queue.Candidate]:
    """The queue's pool: the whole rated board, unfiltered.

    Unfiltered on purpose. §6.3's filters are a way of *looking* at the board; the queue
    sharpens the ranking, and a queue that only ever compared the titles matching whatever the
    person last typed would sharpen one corner of it.
    """
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


def public(tiers: Sequence[board.Tier]) -> list[dict[str, Any]]:
    return [
        {
            "index": tier.index,
            "label": tier.label,
            "entries": [entry.public() for entry in tier.entries],
        }
        for tier in tiers
    ]


__all__ = [
    "Cutpoints",
    "candidates",
    "comparison_counts",
    "cutpoints_of",
    "items",
    "load",
    "names_for",
    "public",
]
