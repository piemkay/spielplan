"""§6.6's board actions (retry, retry from stage N, abandon) and `make_due` (decisions 330, 444).

Each takes the driver's title lock and re-checks what the job's state admits; a retry that meets the
paid stage first asks the spend cap before writing anything (decision 464).
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from spielplan.acquire import pipeline, queue, stages
from spielplan.flywheel import store as flywheel_store
from spielplan.llm import spend

RETRY = "retry"
RETRY_FROM = "retry_from"
ABANDON = "abandon"

# 0029's fourth board outcome: neither parked nor failed (decision 444).
ABANDONED = "abandoned"
QUEUED = pipeline.QUEUED

_ADMITTED: dict[str, tuple[str, ...]] = {
    pipeline.PARKED: (RETRY_FROM, ABANDON),
    pipeline.FAILED: (RETRY, RETRY_FROM, ABANDON),
    ABANDONED: (RETRY_FROM,),
    pipeline.READY: (RETRY_FROM,),
}

# How each action reads in a refusal's sentence.
_SPOKEN = {RETRY: "a plain retry", RETRY_FROM: "a retry from a stage", ABANDON: "abandon"}

# The stage a flywheel launch makes a title due at (decision 443), looked up by name.
PACK_STAGE = next(s.number for s in pipeline.STAGES if s.name == "dna pack")

# A finished task stays finished; a title with only done tasks walks via its `title:<id>` task.
_REVIVABLE = (queue.FAILED, queue.SKIPPED, queue.PENDING)

NO_JOB = "no acquisition job for this title"

IN_FLIGHT = (
    "this title is being walked right now - a worker holds it or one of its tasks is leased - so "
    "nothing was changed. Try again once the walk stops; the board shows where it got to"
)

# Every sentence below is shown verbatim on the board or returned as a refusal.
RETRIED = (
    "retried from the admin board: resumes at stage {number} ({name}); nothing before it runs again"
)
RETRIED_FAILED = (
    "retried from the admin board: resumes at stage {number} ({name}), where it failed; nothing "
    "before it runs again"
)
ABANDONED_REASON = (
    "abandoned from the admin board: nothing runs for this title until it is retried from a stage"
)
ABANDONED_NOTE = "abandoned from the admin board; retry the job from a stage to bring it back"
RELEASED = (
    "Its row in the extraction queue is queued again and out of its batch, so it can be launched on"
    " another plan."
)

# Every task of the title, LOCKED, so the drain's lease cannot land between the check and the write.
_LEASED = "SELECT state FROM acquisition_task WHERE kind = $1 AND payload ->> 'title_id' = $2 FOR UPDATE"

_JOB = "SELECT stage, status, reason FROM acquisition_job WHERE title_id = $1 FOR UPDATE"

# The tasks a revive may bring back, as `queue.Task` rows so their plan is read by the one reader.
_TASKS = (
    "SELECT id, kind, key, payload, attempts, max_attempts, priority, state"
    "  FROM acquisition_task WHERE kind = $1 AND payload ->> 'title_id' = $2 ORDER BY id"
)

# Decision 444's revive: pending, attempts 0, due now, lease/error/failed-for-good cleared, the
# caller's payload merged. `$4::text::jsonb`: the pool's codec would double-encode a dict.
_REVIVE = """
UPDATE acquisition_task
   SET state = $5, attempts = 0, next_attempt_at = now(),
       lease_owner = NULL, lease_expires = NULL, last_error = NULL,
       result_note = left($3::text, $6),
       payload = (payload - $7::text) || $4::text::jsonb,
       updated_at = now()
 WHERE kind = $1 AND payload ->> 'title_id' = $2 AND state = ANY($8::text[])
RETURNING id
"""

# A title with no task to revive gets its `title:<id>` task, spelled as `pipeline.enqueue_title`.
# A leased key matches nothing here.
_TITLE_TASK = """
INSERT INTO acquisition_task (kind, key, payload, result_note)
VALUES ($1, $2, jsonb_build_object('title_id', $3::int) || $4::text::jsonb, left($5::text, $6))
ON CONFLICT (kind, key) DO UPDATE SET
       state = $7, attempts = 0, next_attempt_at = now(),
       lease_owner = NULL, lease_expires = NULL, last_error = NULL,
       result_note = EXCLUDED.result_note,
       payload = (acquisition_task.payload - $8::text) || EXCLUDED.payload,
       updated_at = now()
 WHERE acquisition_task.state <> $9
RETURNING id
"""

# `queue.skip`'s statement over the title's open tasks; `last_error` is kept.
_SKIP_OPEN = """
UPDATE acquisition_task
   SET state = $3, lease_owner = NULL, lease_expires = NULL,
       result_note = left($4::text, $5), updated_at = now()
 WHERE kind = $1 AND payload ->> 'title_id' = $2 AND state = $6
RETURNING id
"""

# Abandon takes the title out of its batch (decision 448): the plan leaves every task, or a later
# retry would bring it back.
_UNPLAN = """
UPDATE acquisition_task
   SET payload = payload - $3::text - $4::text, updated_at = now()
 WHERE kind = $1 AND payload ->> 'title_id' = $2 AND (payload ? $3 OR payload ? $4)
"""

# Decision 431's permanence per title; only a board retry clears it.
_FAILED_FOR_GOOD = (
    "SELECT key FROM acquisition_task"
    " WHERE kind = $1 AND payload ->> 'title_id' = $2 AND state = $3"
    f"   AND (attempts < max_attempts OR payload ->> '{stages.FAILED_FOR_GOOD_MARK}' = 'true')"
    " ORDER BY updated_at DESC LIMIT 1"
)


class ActionRefused(Exception):
    """The action was refused and nothing was written; `reason` is the sentence to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NoJob(LookupError):
    """The title has no board row - the pipeline has never touched it - or no longer exists."""


def stage_legend() -> list[dict[str, Any]]:
    """§8's ten stages as the board renders them, out of the driver's one tuple."""
    return [{"number": stage.number, "name": stage.name} for stage in pipeline.STAGES]


def admitted(status: str) -> list[str]:
    """The actions a board row in `status` admits, per decision 444; none for a job in flight."""
    return list(_ADMITTED.get(status, ()))


def _stage(number: int) -> pipeline.Stage:
    return next(stage for stage in pipeline.STAGES if stage.number == number)


async def _hold(conn: asyncpg.Connection, title_id: int) -> None:
    """Take the title for this transaction, or refuse because a walk has it.

    The driver's lock, transaction-scoped so it cannot leak; tried, never waited for. Re-entrant.
    """
    if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1, $2)", pipeline._TITLE_LOCK,
                               int(title_id)):
        raise ActionRefused(IN_FLIGHT)
    if any(row["state"] == queue.LEASED for row in await conn.fetch(_LEASED, pipeline.TASK_KIND,
                                                                     str(title_id))):
        raise ActionRefused(IN_FLIGHT)


async def _open(conn: asyncpg.Connection, title_id: int) -> asyncpg.Record:
    """The title's board row, locked for this transaction, with the title held."""
    job = await conn.fetchrow(_JOB, title_id)
    if job is None:
        raise NoJob(NO_JOB)
    await _hold(conn, title_id)
    return job


def _admit(job: asyncpg.Record, action: str) -> None:
    allowed = admitted(job["status"])
    if action in allowed:
        return
    if not allowed:
        raise ActionRefused(
            f"this job is {job['status']}, so it is in flight and admits no action until it stops"
        )
    if action == RETRY:
        raise ActionRefused(
            "a plain retry is offered on a failed job alone, and this job is "
            f"{job['status']}: retry it from a stage instead"
        )
    raise ActionRefused(
        f"this job is {job['status']}, which admits {' or '.join(_SPOKEN[a] for a in allowed)} and "
        f"not {_SPOKEN[action]}"
    )


async def _walking_plans(conn: asyncpg.Connection, title_id: int) -> list[Any]:
    """The distinct batch plans the revived tasks will walk with, or `[None]` for the stored settings."""
    rows = await conn.fetch(_TASKS, pipeline.TASK_KIND, str(title_id))
    walking = [row for row in rows if row["state"] in _REVIVABLE]
    if not walking:
        walking = [row for row in rows if row["key"] == f"title:{title_id}"]
    plans: dict[str, Any] = {}
    for row in walking:
        plan = stages.task_plan(queue.Task.from_row(row))
        plans.setdefault(json.dumps(plan, sort_keys=True, default=str), plan)
    return list(plans.values()) or [None]


async def failed_for_good(conn: asyncpg.Connection, title_id: int) -> str | None:
    """The key of a task of this title closed for good (decision 431), or None.

    Asked by a flywheel launch, which must not revive a title only a board retry may bring back (448).
    """
    return await conn.fetchval(_FAILED_FOR_GOOD, pipeline.TASK_KIND, str(title_id), queue.FAILED)


async def make_due(
    conn: asyncpg.Connection,
    title_id: int,
    *,
    stage: int,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> int:
    """Make one title due at `stage`, writing both tables; returns how many of its tasks are due.

    Must run inside the caller's transaction (the lock is transaction-scoped). `stage` is clamped to
    the board's; zero tasks due is refused as in flight.
    """
    if not conn.is_in_transaction():
        raise RuntimeError("make_due needs its caller's transaction: its title lock is transaction-scoped")
    await _hold(conn, title_id)
    if not await conn.fetchval("SELECT 1 FROM title WHERE id = $1 FOR KEY SHARE", title_id):
        raise NoJob(f"title {title_id} no longer exists")
    reached = await conn.fetchval(
        "SELECT stage FROM acquisition_job WHERE title_id = $1 FOR UPDATE", title_id
    )
    if reached is not None:
        stage = min(stage, int(reached))
    await pipeline.write_board(conn, title_id, stage=stage, status=QUEUED, reason=reason,
                               retry_after=None)
    extra = json.dumps(payload or {}, default=str)
    revived = await conn.fetch(
        _REVIVE, pipeline.TASK_KIND, str(title_id), reason, extra, queue.PENDING, queue.NOTE_LIMIT,
        stages.FAILED_FOR_GOOD_MARK, list(_REVIVABLE),
    )
    if revived:
        return len(revived)
    created = await conn.fetchval(
        _TITLE_TASK, pipeline.TASK_KIND, f"title:{title_id}", int(title_id), extra, reason,
        queue.NOTE_LIMIT, queue.PENDING, stages.FAILED_FOR_GOOD_MARK, queue.LEASED,
    )
    if created is None:
        raise ActionRefused(IN_FLIGHT)
    return 1


async def _retry_from(
    conn: asyncpg.Connection, title_id: int, job: asyncpg.Record, stage: int, *, reason: str | None
) -> int:
    _admit(job, RETRY_FROM)
    reached = int(job["stage"])
    if isinstance(stage, bool) or not isinstance(stage, int) or not 1 <= stage <= reached:
        raise ActionRefused(
            f"stage {stage!r} is not one this job can be retried from: a retry resumes at a stage "
            f"from 1 to {reached}, the stage it reached, and never moves a job forward"
        )
    # Decision 464: the first stage that fetches or holds a re-ask window, or the paid stage itself.
    first = next(
        (s for s in pipeline.STAGES
         if s.number >= stage and s.implemented and (s.paid or s.fetches or s.reask_from is not None)),
        None,
    )
    if first is not None and first.paid:
        for plan in await _walking_plans(conn, title_id):
            refused = await spend.retry_refusal(conn, title_id=title_id, batch=plan)
            if refused is not None:
                raise ActionRefused(refused)
    named = _stage(stage)
    return await make_due(
        conn, title_id, stage=stage,
        reason=(reason or RETRIED).format(number=named.number, name=named.name),
    )


async def retry_from(conn: asyncpg.Connection, title_id: int, stage: int) -> int:
    """Walk the title again from `stage`, nothing before it running (decision 424). Tasks due.

    When the paid stage comes first, the spend cap is asked now with the revived task's plan
    (decision 464); the driver's gate still guards every walk.
    """
    async with conn.transaction():
        job = await _open(conn, title_id)
        return await _retry_from(conn, title_id, job, stage, reason=None)


async def retry(conn: asyncpg.Connection, title_id: int) -> int:
    """The plain retry: a failed job walked again from the stage that failed (failed alone, decision 336).
    """
    async with conn.transaction():
        job = await _open(conn, title_id)
        _admit(job, RETRY)
        return await _retry_from(conn, title_id, job, int(job["stage"]), reason=RETRIED_FAILED)


async def abandon(conn: asyncpg.Connection, title_id: int) -> int:
    """Stop a parked or failed job: its open tasks skipped, the board `abandoned`. Tasks closed.

    A board state, not a delete. Also takes a launched title out of its batch (decision 448).
    """
    async with conn.transaction():
        job = await _open(conn, title_id)
        _admit(job, ABANDON)
        closed = await conn.fetch(
            _SKIP_OPEN, pipeline.TASK_KIND, str(title_id), queue.SKIPPED, ABANDONED_NOTE,
            queue.NOTE_LIMIT, queue.PENDING,
        )
        await conn.execute(_UNPLAN, pipeline.TASK_KIND, str(title_id), stages.PLAN_KEY, stages.BATCH_KEY)
        released = await flywheel_store.release_title(conn, title_id)
        reason = ABANDONED_REASON + "."
        if released:
            reason += " " + RELEASED
        if job["reason"]:
            named = _stage(int(job["stage"]))
            reason += (
                f" It had {job['status']} at stage {named.number} ({named.name}) with: {job['reason']}"
            )
        await pipeline.write_board(conn, title_id, stage=int(job["stage"]), status=ABANDONED,
                                   reason=reason, retry_after=None)
        return len(closed)
