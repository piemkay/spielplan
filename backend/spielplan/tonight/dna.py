"""The DNA the round reasons over: one vector per candidate.

Both tiers via `dna_tagged` (§4.1 rule 1); salience and confidence weight but never filter
(rule 2); every read is scoped to one vocabulary version (§10).
"""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from spielplan.db import dna_terms

# Shared with `home/why.py`: extracted speaks louder, both tiers stay admissible (§4.1).
TERM_WEIGHT = dna_terms.TERM_WEIGHT


async def vectors_for(
    conn: asyncpg.Connection, title_ids: Sequence[int], *, version: str
) -> dict[int, dict[str, float]]:
    """One sparse `term -> weight` vector per title.

    `max()` over the tiers, not a sum: a term in both tiers counts once (§4.1 rule 1).
    """
    ids = sorted({int(t) for t in title_ids})
    if not ids or not version:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT d.title_id, d.term, max({TERM_WEIGHT}) AS weight
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = ANY($2)
         GROUP BY d.title_id, d.term
        """,
        version, ids,
    )
    out: dict[int, dict[str, float]] = {t: {} for t in ids}
    for row in rows:
        out[row["title_id"]][row["term"]] = float(row["weight"])
    return out


async def active_version(conn: asyncpg.Connection) -> str | None:
    """The vocabulary every read here is scoped to, resolved as `home/why.py` resolves it."""
    return await dna_terms.active_version(conn)


async def terms_carried_by(
    conn: asyncpg.Connection, title_id: int, *, version: str, limit: int = 6
) -> list[dict[str, object]]:
    """The terms one title actually carries, loudest first, each with its tier.

    §6.2 step 7's match lines may only name terms the winner carries.
    """
    rows = await conn.fetch(
        f"""
        SELECT d.term, min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               max({TERM_WEIGHT}) AS weight
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = $2
         GROUP BY d.term
         ORDER BY max({TERM_WEIGHT}) DESC, d.term
         LIMIT $3
        """,
        version, title_id, limit,
    )
    return [
        {"term": r["term"], "facet": r["facet"], "tier": r["tier"], "weight": float(r["weight"])}
        for r in rows
    ]


__all__ = [
    "TERM_WEIGHT",
    "active_version",
    "terms_carried_by",
    "vectors_for",
]
