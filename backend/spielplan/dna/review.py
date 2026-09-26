"""§6.6 Data's review of DNA rejects and low-evidence tags: two orderings, and never a filter.

Neither read accepts anything: the only action is a ledger row (§8 stage 7, §4.1 rule 2).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from spielplan.db import dna_terms

# The rows past it are the oldest refusals.
REJECT_LIMIT = 200

# LEFT JOIN keeps `unknown_title` refusals, whose title is NULL (decision 396).
_REJECTS = """
    SELECT r.id, r.title_id, t.name, t.year, r.term, r.facet, r.salience, r.quote,
           r.rule_violated, r.provider, r.at
      FROM dna_reject r LEFT JOIN title t ON t.id = r.title_id
     ORDER BY r.at DESC, r.id DESC
     LIMIT $1
"""


async def rejects(conn: asyncpg.Connection, *, limit: int = REJECT_LIMIT) -> list[dict[str, Any]]:
    """The newest refusals, each with its title, quote and broken rule, returned as stored."""
    return [dict(row) for row in await conn.fetch(_REJECTS, limit)]


# Decision 446's ordering. NULLS LAST: NULL means unmeasured, not weakest.
_LOW_EVIDENCE = """
    SELECT t.term, t.facet, t.salience, t.confidence, t.n_sources, t.provider
      FROM dna_tag t
     WHERE t.title_id = $1 AND t.version = $2
     ORDER BY t.confidence ASC NULLS LAST, t.n_sources ASC NULLS LAST, t.term, t.provider
"""


async def low_evidence(conn: asyncpg.Connection, title_id: int) -> dict[str, Any]:
    """Every extracted tag of one title at the active vocabulary, weakest first."""
    version = await dna_terms.active_version(conn)
    if version is None:
        return {"title_id": title_id, "version": None, "tags": []}
    rows = await conn.fetch(_LOW_EVIDENCE, title_id, version)
    return {"title_id": title_id, "version": version, "tags": [dict(row) for row in rows]}
