"""§8.4's thin-facet feed, observed the moment a title's walk finishes stage 8 (decision 440).

Presence only (decision 329): one declared facet with zero extracted-tier terms makes a title thin.
"""

from __future__ import annotations

import json

import asyncpg

from spielplan.db import dna_terms
from spielplan.dna import coverage
from spielplan.flywheel import store

# Running rows end at the title's next stage-8 observation (decision 443).
_CLOSE_RUNNING = (
    "UPDATE flywheel_item SET status = 'done' WHERE kind = $1 AND title_id = $2 AND status = 'running'"
)

_CLOSE_OPEN = (
    "UPDATE flywheel_item SET status = 'done'"
    " WHERE kind = $1 AND title_id = $2 AND status = ANY($3::text[])"
)

# One open row per title (0029's index); the conflict target repeats its predicate so Postgres
# infers it. The refresh keeps `created_at` and `status`.
_REFRESH = """
INSERT INTO flywheel_item (kind, title_id, detail, reason, est_titles)
VALUES ('thin_facet', $1, $2::text::jsonb, $3, 1)
ON CONFLICT (title_id) WHERE kind = 'thin_facet' AND status IN ('queued', 'approved', 'running')
DO UPDATE SET reason = EXCLUDED.reason, detail = EXCLUDED.detail
RETURNING id
"""


def _reason(unnamed: list[str], declared: int, version: str) -> str:
    return (
        f"{len(unnamed)} of the {declared} facets vocabulary {version} declares carry no"
        f" extracted-tier term for this title: {', '.join(unnamed)}. An extraction batch asks its"
        " sources again for these, under terms the vocabulary already has"
    )


async def observe_title(conn: asyncpg.Connection, title_id: int) -> int | None:
    """Record what stage 8's finish says about one title's facets. The open row's id, or None.

    One transaction, so the row is visible at once. Nothing is caught: a failure fails the walk.
    """
    async with conn.transaction():
        await conn.execute(_CLOSE_RUNNING, store.THIN_FACET, title_id)
        version = await dna_terms.active_version(conn)
        if version is None:
            return None
        named = await coverage.facet_coverage(conn, title_id, version=version)
        if not named:
            return None
        unnamed = [facet for facet, terms in named.items() if terms == 0]
        if not unnamed:
            await conn.execute(_CLOSE_OPEN, store.THIN_FACET, title_id, list(store.OPEN))
            return None
        detail = {"title_id": title_id, "version": version, "coverage": named, "unnamed": unnamed}
        return await conn.fetchval(
            _REFRESH, title_id, json.dumps(detail), _reason(unnamed, len(named), version)
        )
