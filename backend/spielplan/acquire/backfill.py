"""The metadata walk: §8 stages 2 (TMDB only) and 3 for bundle titles lacking TMDB data (decision 522).

No board row, no DNA ledger, no paid stage. A resolver id another title already holds is set back to
NULL so the resolver's answers do not change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx

from spielplan.acquire import pipeline, queue, stages
from spielplan.acquire.fetch import Fetcher
from spielplan.derive import rebuild
from spielplan.sources import credentials, tmdb

log = logging.getLogger("spielplan.acquire.backfill")

# Not `pipeline.TASK_KIND`, so the drain and board actions never touch it; same key as the pipeline.
KIND = "backfill"

# At 18 rps two requests a title is ~11 s of pacing, inside the job's 300 s budget.
BATCH = 100

# The filing order, lowest first, which is `queue.lease`'s `ORDER BY priority, id`.
OWNED, SEEN_OR_RATED, SEEDED, WARM, PLACED, REST = 10, 20, 30, 40, 50, 60

# Decision 484's thirty days: "no record" can change.
NONE_AFTER = timedelta(days=30)
# Retried daily; the queue's own curve would spend all attempts inside one outage.
FAILED_AFTER = timedelta(days=1)

DERIVED, NONE, UNASKABLE, FAILED, YIELDED, STOP = (
    "derived", "none", "unaskable", "failed", "yielded", "stop",
)

HOST = "api.themoviedb.org"

_FILE = """
SELECT t.id,
       CASE WHEN t.is_owned THEN $3::int
            WHEN EXISTS (SELECT 1 FROM user_title u WHERE u.title_id = t.id AND u.state = 'seen')
              OR EXISTS (SELECT 1 FROM verdict v
                          WHERE v.title_id = t.id AND v.superseded_by IS NULL) THEN $4::int
            WHEN EXISTS (SELECT 1 FROM seed_list s WHERE s.title_id = t.id) THEN $5::int
            WHEN t.placement = 'warm' THEN $6::int
            WHEN t.placement <> 'unplaced' THEN $7::int
            ELSE $8::int END AS tier
  FROM title t
 WHERE t.origin = 'bundle'
   AND (t.tmdb_id IS NOT NULL OR t.imdb_id IS NOT NULL)
   AND NOT EXISTS (SELECT 1 FROM title_meta m WHERE m.title_id = t.id AND m.source = 'tmdb')
   AND NOT EXISTS (SELECT 1 FROM acquisition_task q WHERE q.kind = $1 AND q.key = 'title:' || t.id)
 ORDER BY tier, t.id
 LIMIT $2
"""


def key_for(title_id: int) -> str:
    return f"title:{int(title_id)}"


async def file_candidates(conn: asyncpg.Connection, room: int) -> int:
    """File up to `room` bundle titles with no TMDB block, in the owner's order. Returns how many."""
    if room <= 0:
        return 0
    rows = await conn.fetch(_FILE, KIND, room, OWNED, SEEN_OR_RATED, SEEDED, WARM, PLACED, REST)
    filed = 0
    for row in rows:
        filed += await queue.enqueue(
            conn, KIND, key_for(row["id"]), {"title_id": int(row["id"])}, priority=row["tier"]
        )
    return filed


async def walk(
    conn: asyncpg.Connection,
    *,
    limit: int = BATCH,
    run_id: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object] | None:
    """File what is owed, lease a batch and walk it. None when there was nothing to do.

    No TMDB key, no walk; nothing filed is lost.
    """
    if await credentials.tmdb_auth(conn) is None:
        return None
    due = await queue.pending_count(conn, [KIND])
    filed = await file_candidates(conn, limit - due)
    leased = await queue.lease(conn, [KIND], limit=limit)
    if not leased:
        return {"filed": filed} if filed else None

    counts = {DERIVED: 0, NONE: 0, UNASKABLE: 0, FAILED: 0, YIELDED: 0}
    stopped = None
    async with Fetcher(conn=conn, transport=transport) as fetcher:
        for position, task in enumerate(leased):
            try:
                outcome, note = await _walk_one(conn, fetcher, task, run_id)
            except asyncio.CancelledError:
                # The budget cancelled us: hand unstarted titles back refunded; a failed release must not
                # win.
                with contextlib.suppress(Exception):
                    await queue.release(
                        conn, [t.id for t in leased[position + 1:]],
                        note="the walk's budget expired before this title was started",
                    )
                raise
            except Exception as exc:                                     # noqa: BLE001
                log.exception("metadata walk: title task %s broke", task.key)
                outcome, note = FAILED, f"{type(exc).__name__}: {exc}"
            if outcome == STOP:
                # A refused key or paused host is a fact about TMDB, recorded against no title.
                stopped = note
                await queue.release(conn, [t.id for t in leased[position:]], note=note)
                log.warning("metadata walk stopped: %s", note)
                break
            await _settle(conn, task, outcome, note)
            counts[outcome] += 1
    report: dict[str, object] = {"walked": sum(counts.values()), **counts, "filed": filed}
    if stopped:
        report["stopped"] = stopped
    return report


async def _settle(conn: asyncpg.Connection, task: queue.Task, outcome: str, note: str) -> None:
    now = datetime.now(UTC)
    if outcome == DERIVED:
        await queue.complete(conn, task.id, note)
    elif outcome == NONE:
        await queue.defer(conn, task.kind, task.key, now + NONE_AFTER, reason=note)
    elif outcome == YIELDED:
        await queue.defer(conn, task.kind, task.key, now, reason=note)
    elif outcome == UNASKABLE:
        await queue.skip(conn, task.id, note)
    else:
        await queue.fail(conn, task.id, note, retry_in=FAILED_AFTER.total_seconds())


async def _walk_one(
    conn: asyncpg.Connection, fetcher: Fetcher, task: queue.Task, run_id: int | None
) -> tuple[str, str]:
    title_id = int(task.payload["title_id"])
    # The pipeline's own per-title lock, so the two walks never write one title at once.
    if not await pipeline._claim_title(conn, title_id):
        return YIELDED, pipeline.TITLE_IN_FLIGHT
    try:
        before = await conn.fetchrow(
            "SELECT kind, imdb_id, tmdb_id, tvdb_id FROM title WHERE id = $1", title_id
        )
        if before is None:
            return UNASKABLE, f"title {title_id} no longer exists"
        if await _has_tmdb(conn, title_id):
            return DERIVED, "already carries TMDB's record"

        ctx = stages.StageContext(
            conn=conn, task=task, title_id=title_id, run_id=run_id, fetcher=fetcher
        )
        results = []
        for adapter in (tmdb.resolve, tmdb.detail):
            result = await adapter(ctx)
            results.append(result)
            stop = await _refused(conn, fetcher, result)
            if stop:
                return STOP, stop
            if not result.ok and result.ran:
                return await _failure(conn, result)
        resolved, detail = results
        if not detail.ran:
            # No key now means stop; otherwise TMDB holds no record, or there was nothing to resolve from.
            if await credentials.tmdb_auth(conn) is None:
                return STOP, "no TMDB key is configured"
            if resolved.ran:
                return NONE, resolved.note
            return UNASKABLE, f"{resolved.note}; {detail.note}"

        given_back = await _give_back_shared_ids(conn, title_id, before)
        derived = await rebuild.derive_title(conn, title_id, adjudicate=False)
        note = f"card derived from {', '.join(derived.documents) or 'nothing'}"
        if given_back:
            note += "; " + "; ".join(
                f"{column} given back, title {holder} already carries it"
                for column, holder in given_back.items()
            )
        return DERIVED, note
    finally:
        # A session lock returns to the pool with the connection; a failed unlock must not win.
        with contextlib.suppress(Exception):
            await pipeline._release_title(conn, title_id)


async def _has_tmdb(conn: asyncpg.Connection, title_id: int) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM title_meta WHERE title_id = $1 AND source = 'tmdb'", title_id
    ))


async def _refused(conn: asyncpg.Connection, fetcher: Fetcher, result: Any) -> str | None:
    """A sentence when TMDB will not be asked again this run (refused key, paused host), else None."""
    if result.ok:
        return None
    if result.doc_id is not None:
        status = await conn.fetchval(
            "SELECT http_status FROM raw_document WHERE id = $1", result.doc_id
        )
        if status in (401, 403):
            return f"TMDB refused the configured key (HTTP {status})"
    for host in fetcher.host_report():
        if host["host"] == HOST and host["paused_for"] > 0:
            return f"{HOST} is paused by its circuit breaker for {host['paused_for']:.0f}s"
    return None


async def _failure(conn: asyncpg.Connection, result: Any) -> tuple[str, str]:
    """A kind that ran and did not answer. A 404 or 410 is TMDB saying the id names nothing."""
    if result.doc_id is not None:
        status = await conn.fetchval(
            "SELECT http_status FROM raw_document WHERE id = $1", result.doc_id
        )
        if status in (404, 410):
            return NONE, result.note
    return FAILED, result.note or "no answer"


# `resolve_title_id`'s match columns and whether each is per kind (§4.1 rule 6).
_RESOLVER_IDS = (("imdb_id", False), ("tmdb_id", True), ("tvdb_id", True))


async def _give_back_shared_ids(
    conn: asyncpg.Connection, title_id: int, before: asyncpg.Record
) -> dict[str, int]:
    """Set back to NULL each resolver id this walk filled that another title already carries."""
    given: dict[str, int] = {}
    for column, per_kind in _RESOLVER_IDS:
        if before[column] is not None:
            continue
        same_kind = " AND o.kind = t.kind" if per_kind else ""
        holder = await conn.fetchval(
            f"SELECT o.id FROM title t JOIN title o ON o.{column} = t.{column} AND o.id <> t.id"
            f"{same_kind} WHERE t.id = $1 ORDER BY o.id LIMIT 1",
            title_id,
        )
        if holder is None:
            continue
        await conn.execute(
            f"UPDATE title SET {column} = NULL, updated_at = now() WHERE id = $1", title_id
        )
        given[column] = int(holder)
    return given


__all__ = ["BATCH", "KIND", "file_candidates", "key_for", "walk"]
