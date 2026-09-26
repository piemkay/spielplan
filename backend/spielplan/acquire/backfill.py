"""The metadata walk: §8 stages 2 and 3 for a bundle title the corpus never fetched TMDB for.

Spec v2.1 §8 (stage 2's `tmdb:resolve -> tmdb:detail`, stage 3's derive, the politeness clause),
§6.0's title card, §6.8; decisions 162, 372, 411, 484, 499, 501 and 522.

WHY IT EXISTS. The corpus fetched TMDB's record for about half its titles. On the seeded install
(bundle v20260926b) 9,162 of 19,071 bundle titles carry no `tmdb` block in `title_meta`, 9,025 of
them no overview at all once decision 499 took MPST's synopses off the card, and at the second
household test Moulin Rouge (1952) and The Grudge opened as a name, a year and a tinted panel.
Decision 411 exits a placed bundle title at §8 stage 1 because "stages 2 to 9 have nothing to
produce", which is untrue of these: stage 2's TMDB kinds and stage 3's derive have their card to
produce. Decision 522 gives them exactly those two stages, here, and nothing else.

WHAT IT RUNS, AND IT WRITES NOTHING ITSELF.
  * `tmdb:resolve` and `tmdb:detail`, the adapters §8 stage 2 runs (`sources/tmdb.py`), so the
    bytes land in the raw store under this walk's task and identity is filled, never clobbered
    (decision 372).
  * §8 stage 3's derive, so the overview, the tagline and the poster arrive through the card's
    one resolution - the corpus's source order, no MPST overview, no shared synopsis, servable art
    only (decisions 499, 501) - and a field is replaced only by TMDB's own value, which leads that
    order. The year, the runtime and the language are filled and never overwritten.
  * NOT the adjudication ledger (`derive_title(adjudicate=False)`): the extracted DNA tier is the
    bundle's and this walk leaves it alone. Not the other seven sources, not the pack, not a paid
    stage, not placement, and no board row: §6.6's board is the pipeline's, and this title's
    pipeline exit at stage 1 stands.

WHO FIRST, which is the owner's order: titles the household owns, then titles a member has seen
or rated, then the seed list, then the rest - placed before unplaced, warm first, because Rate
serves them. Filed a batch at a time, so a title that becomes owned or rated is filed ahead of
everything still waiting.

POLITE BY THE SAME ARGUMENT AS DECISION 484. One `Fetcher` per run, which is decision 373's unit,
paced at `api.themoviedb.org`'s declared policy and persisted to `fetch_host_state`. The worker's
tick is sequential, so the drain, this walk and the art lookup never ask the host at once. A
hundred titles is at most two hundred requests a half hour.

AN IDENTITY ANOTHER TITLE ALREADY HOLDS IS GIVEN BACK. The corpus splits some films across two
rows - S.W.A.T. (1975) is an imdb-only row and a tmdb-only row - and `tmdb:resolve` or
`tmdb:detail` would fill the first with the second's id. Two rows answering one id make
`connectors/resolve.resolve_title_id`'s provider arms pick one arbitrarily, so a column this walk
filled and another title of the same kind already carries is set back to the NULL it was before
the walk: the card is the same film's, and the resolver keeps answering as it did.
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
from spielplan.sources import base as sources
from spielplan.sources import credentials

log = logging.getLogger("spielplan.acquire.backfill")

# The queue kind. Not `pipeline.TASK_KIND`, so the drain never leases this work and the board's
# actions never revive it (both filter on that kind); the key is the one `pipeline.enqueue_title`
# spells, so a document belongs to the title under either walk (`acquire/board.py`).
KIND = "backfill"

# The two kinds §8 stage 2 names first, in the registry's order (resolve 10, detail 20).
KINDS = ("tmdb:resolve", "tmdb:detail")

# One run's reach. At most two TMDB requests a title at the declared 18 rps is about eleven seconds
# of pacing, plus one title's derive per title in its own transaction, inside the job's 300 s; a
# title the budget cuts off is handed back refunded. At the half-hourly cadence the seeded
# install's 2,593 placed titles are walked in about thirteen hours and all 9,162 in about two days.
BATCH = 100

# The filing order, lowest first, which is `queue.lease`'s `ORDER BY priority, id`.
OWNED, SEEN_OR_RATED, SEEDED, WARM, PLACED, REST = 10, 20, 30, 40, 50, 60

# TMDB holding no record is an answer that can change, so it is asked again: decision 484's thirty
# days for the same question about a poster.
NONE_AFTER = timedelta(days=30)
# A failure is asked again tomorrow, and a title the host refuses four days running is closed: the
# queue's own curve would spend all four attempts inside one outage of an hour.
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

    No TMDB key, no walk, and nothing filed is lost: decision 484's rule for the same host.
    """
    if await credentials.tmdb_auth(conn) is None:
        return None
    due = await queue.pending_count(conn, [KIND])
    filed = await file_candidates(conn, limit - due)
    leased = await queue.lease(conn, [KIND], limit=limit)
    if not leased:
        return {"filed": filed} if filed else None

    sources.load_all()
    counts = {DERIVED: 0, NONE: 0, UNASKABLE: 0, FAILED: 0, YIELDED: 0}
    stopped = None
    async with Fetcher(conn=conn, transport=transport) as fetcher:
        for position, task in enumerate(leased):
            try:
                outcome, note = await _walk_one(conn, fetcher, task, run_id)
            except asyncio.CancelledError:
                # The job's budget, not a dead worker: the titles this run claimed and never
                # started go back refunded, as `pipeline._walk_batch` hands back the drain's, and a
                # release that fails must not replace the cancellation.
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
                # Nothing is recorded against the titles: a refused key or a paused host is a fact
                # about TMDB, and the next run asks again.
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
    # The pipeline's own per-title lock, so this walk and a pipeline walk of the same title (an add
    # of an unplaced bundle title, a flywheel launch) never write one title at once.
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
        for kind in KINDS:
            result = await sources.REGISTRY[kind].fn(ctx)
            results.append(result)
            stop = await _refused(conn, fetcher, result)
            if stop:
                return STOP, stop
            if not result.ok and result.ran:
                return await _failure(conn, result)
        resolved, detail = results
        if not detail.ran:
            # `tmdb:detail` asked nothing. A key removed since this run began is a stop and not a
            # fact about the film; otherwise TMDB answered the resolve and holds no record of it
            # (or files it under the other kind, which is not a stage-2 write), or there was
            # nothing to resolve from at all.
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
        # `pipeline.run_task`'s rule: a session lock travels back into the pool with the
        # connection, and an unlock that fails must not replace what this walk is reporting.
        with contextlib.suppress(Exception):
            await pipeline._release_title(conn, title_id)


async def _has_tmdb(conn: asyncpg.Connection, title_id: int) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM title_meta WHERE title_id = $1 AND source = 'tmdb'", title_id
    ))


async def _refused(conn: asyncpg.Connection, fetcher: Fetcher, result: Any) -> str | None:
    """A sentence when TMDB will not be asked again this run, else None.

    A key TMDB refuses refuses every title alike, and a paused host is the breaker saying stop; in
    both, asking the next title is impolite and records nothing true about it.
    """
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


# The columns `connectors/resolve.resolve_title_id` matches on, and whether a match is per kind
# (§4.1 rule 6: tmdb and tvdb ids are unique only within a kind; an imdb id across both).
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


__all__ = ["BATCH", "KIND", "KINDS", "file_candidates", "key_for", "walk"]
