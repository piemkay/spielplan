"""§6.6's acquisition board, read side (decisions 322, 336, 345).

Shows `raw_document` metadata, never the bytes or `content_path` (decision 345). A document
belongs to a title via the key of one of its tasks (`payload ->> 'title_id'`).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

# Unbounded reads on admin surfaces fall over on large installs.
BOARD_LIMIT = 200

# Per title; newest first, so the cut falls on the oldest fetches.
DOCUMENT_LIMIT = 100

_BOARD = """
SELECT j.title_id, j.stage, j.status, j.reason, j.retry_after, j.updated_at,
       t.name, t.year, t.kind, t.origin
  FROM acquisition_job j
  JOIN title t ON t.id = j.title_id
 ORDER BY j.updated_at DESC
 LIMIT $1
"""

_JOB = """
SELECT j.title_id, j.stage, j.status, j.reason, j.retry_after, j.updated_at, j.detail,
       t.name, t.year, t.kind, t.origin
  FROM acquisition_job j
  JOIN title t ON t.id = j.title_id
 WHERE j.title_id = $1
"""


_DOCUMENTS = """
SELECT d.id, d.source, d.kind, d.entity_key, d.url, d.http_status, d.content_sha256,
       d.byte_size, d.content_type, d.fetched_at, d.ok, d.error
  FROM raw_document d
 WHERE d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.fetched_at DESC, d.id DESC
 LIMIT $2
"""


def _job_row(row: asyncpg.Record) -> dict[str, Any]:
    """One board row, with `reason` carried across untouched: it is "shown verbatim on the admin board".
    """
    return {
        "title_id": row["title_id"],
        "name": row["name"],
        "year": row["year"],
        "title_kind": row["kind"],
        "origin": row["origin"],
        "stage": row["stage"],
        "status": row["status"],
        "reason": row["reason"],
        "retry_after": row["retry_after"],
        "updated_at": row["updated_at"],
    }


async def board(conn: asyncpg.Connection, *, limit: int = BOARD_LIMIT) -> list[dict[str, Any]]:
    """Every title the pipeline has an opinion about, newest movement first.

    INNER JOIN: `acquisition_job` cascades on title delete, so a nameless row cannot exist.
    """
    return [_job_row(row) for row in await conn.fetch(_BOARD, limit)]


async def job(conn: asyncpg.Connection, title_id: int) -> dict[str, Any] | None:
    """One title's board row, or None when the pipeline has never touched it; includes `detail`."""
    row = await conn.fetchrow(_JOB, title_id)
    if row is None:
        return None
    # A bare `asyncpg.connect` returns jsonb as a string; decode it here.
    detail = row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail) if detail else {}
    return {**_job_row(row), "detail": detail or {}}


async def documents_for_title(
    conn: asyncpg.Connection, title_id: int, *, limit: int = DOCUMENT_LIMIT
) -> list[dict[str, Any]]:
    """The `raw_document` METADATA rows for one title. Decision 345, and never the bytes.

    `ok = false` rows are included: the board shows what happened, not only what parsed.
    """
    rows = await conn.fetch(_DOCUMENTS, str(title_id), limit)
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "kind": row["kind"],
            "entity_key": row["entity_key"],
            "url": row["url"],
            "http_status": row["http_status"],
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "content_type": row["content_type"],
            "fetched_at": row["fetched_at"],
            "ok": row["ok"],
            "error": row["error"],
        }
        for row in rows
    ]
