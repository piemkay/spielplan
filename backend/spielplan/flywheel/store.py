"""The flywheel's queue: the row writer, the admin queue's read, and M6's two feeds.

`reason` is shown verbatim to the admin, so writers compose it and readers never reword it.
No cost is stored at enqueue (decision 441).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import asyncpg

from spielplan.db import dna_terms

# 0029's CHECK.
KINDS = ("empty_predicate", "uncovered_frontier", "thin_facet")
EMPTY_PREDICATE, UNCOVERED_FRONTIER, THIN_FACET = KINDS

# 0029's one-open-row index definition; `approved` is unused but counted.
OPEN = ("queued", "approved", "running")

QUEUE_LIMIT = 200

_ENQUEUE = """
INSERT INTO flywheel_item (kind, detail, reason, title_id, est_titles)
VALUES ($1, $2::text::jsonb, $3, $4, $5)
RETURNING id
"""

# The LEFT JOINs make a row legible: its title and, while running, its board row.
_QUEUE = """
SELECT f.id, f.kind, f.reason, f.detail, f.est_titles, f.status, f.created_at, f.batch_id,
       f.title_id, t.name, t.year, j.stage AS job_stage, j.status AS job_status,
       j.reason AS job_reason
  FROM flywheel_item f
  LEFT JOIN title t ON t.id = f.title_id
  LEFT JOIN acquisition_job j ON j.title_id = f.title_id
 WHERE f.status = ANY($1::text[])
 ORDER BY f.created_at DESC, f.id DESC
 LIMIT $2
"""

_KNOWN_TERMS = "SELECT count(DISTINCT term) FROM dna_term WHERE version = $1 AND term = ANY($2::text[])"


async def enqueue(
    conn: asyncpg.Connection,
    *,
    kind: str,
    detail: dict[str, Any],
    reason: str,
    title_id: int | None = None,
    est_titles: int | None = None,
) -> int:
    """Append one queued row and return its id. The kind is 0029's CHECK's to refuse.

    `$2::text::jsonb` because the pool's jsonb encoder would double-encode a dict. Thin-facet rows are
    written by `thin.observe_title` instead.
    """
    return await conn.fetchval(_ENQUEUE, kind, json.dumps(detail), reason, title_id, est_titles)


# Back to `queued` with no batch; stays open so 0029's index still allows one row (decision 448).
_RELEASE = """
UPDATE flywheel_item SET status = 'queued', batch_id = NULL
 WHERE kind = $1 AND title_id = $2 AND status = 'running'
RETURNING id
"""


async def release_title(conn: asyncpg.Connection, title_id: int) -> int:
    """Queue a title's launched rows again, out of their batch; returns how many (decision 448)."""
    return len(await conn.fetch(_RELEASE, THIN_FACET, title_id))


def _decoded(detail: Any) -> dict[str, Any]:
    # Pooled connections decode jsonb, a bare `asyncpg.connect` does not.
    if isinstance(detail, str):
        detail = json.loads(detail) if detail else {}
    return detail or {}


async def queue(conn: asyncpg.Connection, *, limit: int = QUEUE_LIMIT) -> list[dict[str, Any]]:
    """The open rows, newest first. `title` and `board` are None when there is none."""
    rows = await conn.fetch(_QUEUE, list(OPEN), limit)
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "reason": row["reason"],
            "detail": _decoded(row["detail"]),
            "est_titles": row["est_titles"],
            "status": row["status"],
            "created_at": row["created_at"],
            "batch_id": row["batch_id"],
            "title_id": row["title_id"],
            "title": None if row["name"] is None else {"name": row["name"], "year": row["year"]},
            "board": None if row["job_status"] is None else {
                "stage": row["job_stage"], "status": row["job_status"], "reason": row["job_reason"],
            },
        }
        for row in rows
    ]


async def _vocabulary_carries(conn: asyncpg.Connection, terms: Sequence[str]) -> str | None:
    """The active vocabulary's version when it carries every one of `terms`; otherwise None (decision
    344).
    """
    named = sorted(set(terms))
    if not named:
        return None
    version = await dna_terms.active_version(conn)
    if version is None:
        return None
    known = await conn.fetchval(_KNOWN_TERMS, version, named)
    return version if known == len(named) else None


def _spoken(terms: Sequence[str]) -> str:
    """`themes.robots` as "robots"; several joined by " + "."""
    return " + ".join(term.split(".", 1)[-1].replace("_", " ") for term in dict.fromkeys(terms))


async def enqueue_empty_predicate(
    conn: asyncpg.Connection, *, query: str, predicate: str, terms: Sequence[str]
) -> int | None:
    """§6.4's empty-predicate feed. The row id, or None when nothing was written.

    Uncalled until M6. Writes nothing when a term is outside the active vocabulary (decision 344).
    """
    version = await _vocabulary_carries(conn, terms)
    if version is None:
        return None
    detail = {"query": query, "predicate": predicate, "terms": sorted(set(terms)), "version": version}
    return await enqueue(
        conn, kind=EMPTY_PREDICATE, detail=detail,
        reason=f"no owned title carries {_spoken(terms)}",
    )


async def enqueue_uncovered_frontier(
    conn: asyncpg.Connection, *, region: str, terms: Sequence[str]
) -> int | None:
    """§8.4's uncovered-frontier feed. The row id, or None when nothing was written.

    Uncalled until M6, under `enqueue_empty_predicate`'s contract.
    """
    version = await _vocabulary_carries(conn, terms)
    if version is None:
        return None
    detail = {"region": region, "terms": sorted(set(terms)), "version": version}
    return await enqueue(
        conn, kind=UNCOVERED_FRONTIER, detail=detail,
        reason=f"the explore frontier at {region} has no owned title carrying {_spoken(terms)}",
    )
