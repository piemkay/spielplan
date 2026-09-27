"""The flywheel's queue: the admin queue's read and the release of a title's launched rows.

`reason` is shown verbatim to the admin, so writers compose it and readers never reword it.
No cost is stored at enqueue (decision 441).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

# The one kind with a producer at M5; 0029's CHECK also admits M6's two.
THIN_FACET = "thin_facet"

# 0029's one-open-row index definition; `approved` is unused but counted.
OPEN = ("queued", "approved", "running")

QUEUE_LIMIT = 200

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
