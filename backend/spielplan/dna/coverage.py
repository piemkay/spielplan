"""§8.4's thin-facet measurement: how many terms a title's sources named, per facet.

Counts only, no threshold (decision 390); nothing here is `is_thin`, which is a different test.
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms


async def facet_coverage(
    conn: asyncpg.Connection, title_id: int, *, version: str | None = None
) -> dict[str, int]:
    """How many distinct extracted-tier terms one title carries in each declared facet.

    Every declared facet is a key, zeros included, in `dna_facet.ord` order. A term counts once however
    many providers named it. Empty mapping when there is no vocabulary.
    """
    scoped = await dna_terms.active_version(conn) if version is None else version
    if scoped is None:
        return {}

    # Facets on the left side of the join, so an unnamed facet is a zero rather than a missing row.
    rows = await conn.fetch(
        """
        SELECT f.facet, count(DISTINCT d.term) AS n
          FROM dna_facet f
          LEFT JOIN dna_tagged d
                 ON d.version = f.version
                AND d.facet = f.facet
                AND d.title_id = $2
                AND d.tier = 'extracted'
         WHERE f.version = $1
         GROUP BY f.facet, f.ord
         ORDER BY f.ord
        """,
        scoped, title_id,
    )
    return {row["facet"]: int(row["n"]) for row in rows}
