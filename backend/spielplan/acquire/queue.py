"""The durable (kind,key) task queue (§8, §5.3): `pending|leased|done|failed|skipped`.

Recovery is by lease expiry only (survives `kill -9`); enqueueing a pair twice is a no-op. The drain's
budget must stay well under `LEASE_SECONDS`, which is why nothing is fenced on the lease owner.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

log = logging.getLogger("spielplan.acquire.queue")

# Bounded below by the longest attempt and above by how long a dead worker's task may stay invisible.
LEASE_SECONDS = 900.0

PENDING = "pending"
LEASED = "leased"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

# `min(600, 15 * 3 ** attempts)`; the cap stops a day-long outage pushing a task a week out.
BACKOFF_BASE_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 600.0

# An error is a sentence on the board, not a stack trace.
ERROR_LIMIT = 1000
NOTE_LIMIT = 500

# Written by the reaper; it reports what it read and nothing else. ASCII.
ABANDONED = (
    "the lease expired with no outcome reported and every attempt is spent: a worker was "
    "stopped, killed or abandoned this task. Nothing will lease it again."
)


def worker_id() -> str:
    """Who holds a lease: host and pid, so two workers in one container differ."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class Task:
    """One leased unit of work, as the drain sees it; `paid` lets a stage assert its batch."""

    id: int
    kind: str
    key: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    priority: int
    paid: bool

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Task:
        # A bare `asyncpg.connect` returns jsonb as a string; decode it here.
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        return cls(
            id=row["id"], kind=row["kind"], key=row["key"], payload=payload or {},
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            priority=row["priority"], paid=row["paid"],
        )


async def enqueue(
    conn: asyncpg.Connection,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    max_attempts: int = 4,
    paid: bool = False,
) -> bool:
    """Add one task. True when a row was created, False when this (kind, key) was already here.

    `ON CONFLICT DO NOTHING` also never touches a leased task's payload. `$3::text::jsonb`: the pool's
    encoder would double-encode a dict.
    """
    created = await conn.fetchval(
        "INSERT INTO acquisition_task (kind, key, payload, priority, max_attempts, paid) "
        "VALUES ($1, $2, $3::text::jsonb, $4, $5, $6) "
        "ON CONFLICT (kind, key) DO NOTHING RETURNING id",
        kind, key, json.dumps(payload or {}), priority, max_attempts, paid,
    )
    return created is not None


async def enqueue_many(
    conn: asyncpg.Connection,
    kind: str,
    items: Iterable[tuple[str, dict[str, Any] | None]],
    *,
    priority: int = 100,
    max_attempts: int = 4,
    paid: bool = False,
) -> int:
    """Add a batch of one kind in one `unnest` statement. Returns how many rows were created."""
    batch = [(key, json.dumps(payload or {})) for key, payload in items]
    if not batch:
        return 0
    created = await conn.fetch(
        "INSERT INTO acquisition_task (kind, key, payload, priority, max_attempts, paid) "
        "SELECT $1, item.key, item.payload::jsonb, $4, $5, $6 "
        "  FROM unnest($2::text[], $3::text[]) AS item(key, payload) "
        "ON CONFLICT (kind, key) DO NOTHING RETURNING id",
        kind, [key for key, _ in batch], [payload for _, payload in batch],
        priority, max_attempts, paid,
    )
    return len(created)


async def lease(
    conn: asyncpg.Connection,
    kinds: Sequence[str] | None = None,
    *,
    limit: int = 1,
    owner: str | None = None,
    paid: bool = False,
) -> list[Task]:
    """Claim up to `limit` ready tasks, atomically, and return them.

    The claim is one `UPDATE ... FOR UPDATE SKIP LOCKED`, so exactly one of two racing workers wins.
    Ready means `pending` and due. The ready index serves the filter, not the `ORDER BY` (a small sort,
    accepted; tested). Attempts are counted on the claim, so a task that kills its worker still counts.
    """
    claimed = await conn.fetch(
        "UPDATE acquisition_task SET"
        "       state = $1,"
        "       lease_owner = $2,"
        "       lease_expires = now() + ($3::float8 * interval '1 second'),"
        "       attempts = attempts + 1,"
        "       updated_at = now()"
        " WHERE id IN ("
        "       SELECT id FROM acquisition_task"
        "        WHERE state = $4 AND next_attempt_at <= now() AND paid = $5::bool"
        "          AND ($6::text[] IS NULL OR kind = ANY($6::text[]))"
        "        ORDER BY priority, id LIMIT $7 FOR UPDATE SKIP LOCKED"
        " ) RETURNING id, kind, key, payload, attempts, max_attempts, priority, paid",
        LEASED, owner or worker_id(), LEASE_SECONDS, PENDING, paid,
        list(kinds) if kinds else None, limit,
    )
    # `UPDATE ... RETURNING` has no order, and the drain refunds positionally, so sort here.
    return [Task.from_row(row) for row in sorted(claimed, key=lambda row: (row["priority"], row["id"]))]


async def complete(conn: asyncpg.Connection, task_id: int, note: str = "") -> None:
    """The work is done. Clears the lease and the error; the note is what §6.6's board shows.

    Not fenced on the lease owner: the budget arithmetic already guarantees the holder.
    """
    await conn.execute(
        "UPDATE acquisition_task SET state = $2, lease_owner = NULL, lease_expires = NULL,"
        "       last_error = NULL, result_note = left($3::text, $4), updated_at = now()"
        " WHERE id = $1",
        task_id, DONE, note, NOTE_LIMIT,
    )


async def skip(conn: asyncpg.Connection, task_id: int, note: str = "") -> None:
    """The task is legitimately not applicable, and no retry can change that today.

    Not `failed`: nothing raised. `last_error` is kept.
    """
    await conn.execute(
        "UPDATE acquisition_task SET state = $2, lease_owner = NULL, lease_expires = NULL,"
        "       result_note = left($3::text, $4), updated_at = now()"
        " WHERE id = $1",
        task_id, SKIPPED, note, NOTE_LIMIT,
    )


async def fail(
    conn: asyncpg.Connection,
    task_id: int,
    error: str,
    *,
    retry_in: float | None = None,
    permanent: bool = False,
) -> str | None:
    """A stage raised. Schedule the retry, or stop. Returns the state that was written.

    Branch and backoff computed in SQL against the row's own `attempts`. `retry_in` for a known wait,
    `permanent` for never. None for an unknown id (a caller bug).
    """
    state = await conn.fetchval(
        "UPDATE acquisition_task SET"
        "       state = CASE WHEN $3::bool OR attempts >= max_attempts THEN $4 ELSE $5 END,"
        "       lease_owner = NULL,"
        "       lease_expires = NULL,"
        "       last_error = left($2::text, $6),"
        "       next_attempt_at = CASE"
        "           WHEN $3::bool OR attempts >= max_attempts THEN next_attempt_at"
        "           ELSE now() + (coalesce($7::float8, least($8::float8,"
        "                                                   $9::float8 * (3 ^ attempts)))"
        "                         * interval '1 second')"
        "       END,"
        "       updated_at = now()"
        " WHERE id = $1 RETURNING state",
        task_id, error, permanent, FAILED, PENDING, ERROR_LIMIT,
        retry_in, MAX_BACKOFF_SECONDS, BACKOFF_BASE_SECONDS,
    )
    return state


async def defer(
    conn: asyncpg.Connection,
    kind: str,
    key: str,
    until: datetime,
    *,
    reason: str = "",
) -> bool:
    """Put a task back until a named instant, without spending an attempt.

    The only mechanism for §8 stage 4's window. The reason goes to `result_note`, not `last_error`
    (decision 336). Moves only a pending or leased task, never a finished one.
    """
    deferred = await conn.fetchval(
        "UPDATE acquisition_task SET state = $3, lease_owner = NULL, lease_expires = NULL,"
        "       attempts = greatest(0, attempts - 1), next_attempt_at = $4,"
        "       result_note = left($5::text, $6), updated_at = now()"
        " WHERE kind = $1 AND key = $2 AND state IN ($3, $7) RETURNING id",
        kind, key, PENDING, until, reason, NOTE_LIMIT, LEASED,
    )
    return deferred is not None


async def release(conn: asyncpg.Connection, task_ids: Sequence[int], *, note: str = "") -> int:
    """Hand back leases for work that was claimed and never started. Returns how many moved.

    Refunds the attempt, since nothing ran. `state = LEASED` stops a stale id reviving a finished task,
    but this is unfenced on the owner, like `complete`: callers must pass ids they hold.
    """
    ids = [int(task_id) for task_id in task_ids]
    if not ids:
        return 0
    released = await conn.fetch(
        "UPDATE acquisition_task SET state = $2, lease_owner = NULL, lease_expires = NULL,"
        "       attempts = greatest(0, attempts - 1), next_attempt_at = now(),"
        "       result_note = left($3::text, $4), updated_at = now()"
        " WHERE id = ANY($1::bigint[]) AND state = $5 RETURNING id",
        ids, PENDING, note, NOTE_LIMIT, LEASED,
    )
    return len(released)


async def reclaim_expired(conn: asyncpg.Connection) -> dict[str, int]:
    """Recover the tasks of workers that died. Returns {"pending": n, "failed": n}.

    Tasks with attempts left go back to the pool; spent ones are closed with `ABANDONED`, so a task that
    kills its worker cannot loop forever.
    """
    reclaimed = await conn.fetch(
        "UPDATE acquisition_task SET"
        "       state = CASE WHEN attempts >= max_attempts THEN $2 ELSE $3 END,"
        "       lease_owner = NULL,"
        "       lease_expires = NULL,"
        "       last_error = CASE WHEN attempts >= max_attempts THEN $4::text ELSE last_error END,"
        "       updated_at = now()"
        " WHERE state = $1 AND lease_expires < now() RETURNING state",
        LEASED, FAILED, PENDING, ABANDONED,
    )
    outcome = {
        PENDING: sum(1 for row in reclaimed if row["state"] == PENDING),
        FAILED: sum(1 for row in reclaimed if row["state"] == FAILED),
    }
    if outcome[FAILED]:
        log.warning(
            "acquisition queue: %d task(s) were abandoned past every attempt and are closed",
            outcome[FAILED],
        )
    if outcome[PENDING]:
        log.info("acquisition queue: %d abandoned lease(s) returned to the pool", outcome[PENDING])
    return outcome


async def stats(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Queue depth per kind and state, as rows a caller can serialise unchanged."""
    rows = await conn.fetch(
        "SELECT kind, state, count(*)::bigint AS n FROM acquisition_task "
        " GROUP BY kind, state ORDER BY kind, state"
    )
    return [{"kind": r["kind"], "state": r["state"], "count": int(r["n"])} for r in rows]


async def pending_count(
    conn: asyncpg.Connection, kinds: Sequence[str] | None = None, *, paid: bool = False
) -> int:
    """How much work could be leased right now -- the drain's question, not the board's.

    Due tasks only, and `paid` defaults like `lease`'s, so it answers "is there work I will take".
    """
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM acquisition_task"
            " WHERE state = $1 AND next_attempt_at <= now() AND paid = $3::bool"
            "   AND ($2::text[] IS NULL OR kind = ANY($2::text[]))",
            PENDING, list(kinds) if kinds else None, paid,
        )
    )


async def filed_since(
    conn: asyncpg.Connection, kind: str, since: datetime, *, except_priority: int
) -> tuple[int, bool]:
    """How many tasks of `kind` were filed at or after `since`, and whether one was ever filed.

    Feeds decision 451's projection. `except_priority` drops re-offers of owned titles.
    """
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE created_at >= $2)::bigint AS recent,"
        "       count(*) > 0 AS ever"
        "  FROM acquisition_task WHERE kind = $1 AND priority <> $3",
        kind, since, except_priority,
    )
    return int(row["recent"]), bool(row["ever"])
