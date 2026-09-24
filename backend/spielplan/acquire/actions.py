"""§6.6's board actions - retry, retry from stage N, abandon - and the one lever that makes a title due.

Spec v2.1 §8 ("Failure at any stage parks the job with a reason, retryable from admin; paid stages
(6) never auto-retry past the spend cap"), §6.6 Data's board clause as v2.1.3 amends it; decisions
322, 330, 336, 345, 348, 421, 424, 431, 442, 443 and 444.

THE LEVER EVERY EARLIER MILESTONE REFUSED TO BUILD, NOW THAT IT HAS AN OWNER. `acquire/queue.py`
did not port the corpus's `retry_failed`, on the ground that "a lever that revives finished work
before anything has decided what revival means is a lever with no owner"; `queue.defer` and
`queue.release` were fenced so neither could become it by accident; and `pipeline.TITLE_GONE`,
`stages.waiting_on_the_world` and `stages.FAILED_FOR_GOOD` each tell an operator that decision
330's revive arrives here. Decision 330 adopted proposal 109's three actions and decision 444 says
what each state admits and what each writes, so this module is that decision and nothing wider.
`acquire/board.py` stays the read half, as its own docstring argues; the rules about what a revive
may touch live here, beside the driver whose tables they write.

WHAT EACH STATE ADMITS (decision 444, on decision 336's two words):

    parked      retry from stage N, abandon     waiting on something that may change
    failed      retry, retry from stage N,      "the only state offering a plain retry"
                abandon
    abandoned   retry from stage N              an operator stopped it; nothing is waiting
    ready       retry from stage N              a finished title walked again from a stage
    queued,     nothing                         in flight, or about to be
    running

The board envelope carries this per job (`admitted`), so the surface renders what the server admits
rather than deriving it, and every action checks it again here whatever the client sent.

A RETRY RESUMES AT THE STAGE IT NAMES AND NOTHING BEFORE IT RUNS (decision 424). It writes the
board `queued` at stage N with `retry_after` cleared - which is the whole of why a job parked at the
reviews gate and retried from stage 4 re-enters at 4 and asks nothing, even once its window has
closed: `pipeline._resume_index` re-enters at `reask_from` only for a park whose own deadline has
passed (decision 421), and a retry is not a park. From stage 3 the derive re-reads the stored
documents; from stage 4 no request leaves the process. And it revives the title's tasks rather than
writing new ones, so a flywheel-launched task keeps the batch plan in its payload (decision 442).

THE PAID STAGE IS ASKED ABOUT BEFORE ANYTHING IS WRITTEN. §8's "never auto-retry past the spend cap"
is held by the driver's gate on every walk whatever made the task due; what a retry adds is the
coverage row's clause that a retry which would breach the cap "is refused with that reason rather
than queued". So when the first implemented stage at or after N is the paid one - read off
`pipeline.STAGES`' flags, never a literal 6 - `spend.retry_refusal` is asked with the plan the
revived task will walk with, and its sentence is the refusal, with nothing changed.

NOT WHILE A WALK HOLDS THE TITLE. Every action takes `pipeline._TITLE_LOCK` - the driver's own
namespace, imported and never re-spelled - as a transaction-level lock, and refuses when a walk
holds it or one of the title's tasks is leased: two writers of one board row and one task is the
state decision 322's lock exists to prevent, and an operator's click is a writer too. The lock is
held to the end of the action's transaction, so a walk that starts meanwhile yields to it
(`pipeline._yield_the_title`) rather than walking half-revived rows.
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

# The board's fourth outcome, which 0029 adds to `acquisition_job`'s CHECK: neither parked (nothing
# is waiting) nor failed (nothing raised), so a thin-but-placed title abandoned from the board is
# never shown as broken (decision 444).
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

# The stage a flywheel launch makes a title due at (decision 443), looked up by §8's name in the
# driver's one tuple rather than written as a number.
PACK_STAGE = next(s.number for s in pipeline.STAGES if s.name == "dna pack")

# The task states a revive brings back. `done` is not among them: a finished task stays finished,
# and a title whose only tasks are done is walked again through its `title:<id>` task.
_REVIVABLE = (queue.FAILED, queue.SKIPPED, queue.PENDING)

NO_JOB = "no acquisition job for this title"

IN_FLIGHT = (
    "this title is being walked right now - a worker holds it or one of its tasks is leased - so "
    "nothing was changed. Try again once the walk stops; the board shows where it got to"
)

# Every sentence below is shown verbatim on §6.6's board or returned as a refusal, so each says what
# was done or refused and what the operator can do next.
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
    " another plan (decision 448)."
)

# Every task of the title, LOCKED, and not a bare read of whether one is leased. A read took no lock,
# so the drain's lease - one UPDATE over `FOR UPDATE SKIP LOCKED`, blind to the advisory lock and to
# the board row - could land in the round trips between it and this action's write: abandon then
# skipped nothing and committed `abandoned` over a task that walked on and overwrote it, and a launch
# left the leased task to walk without the batch plan. Held to the end of the action's transaction,
# these rows are the ones `queue.lease` skips; one it holds mid-statement is waited for and re-read,
# and then refuses here as leased. [M5.6 review cycle 1, M56-BOARD-LEASE-RACE]
_LEASED = "SELECT state FROM acquisition_task WHERE kind = $1 AND payload ->> 'title_id' = $2 FOR UPDATE"

_JOB = "SELECT stage, status, reason FROM acquisition_job WHERE title_id = $1 FOR UPDATE"

# The tasks a revive may bring back, as `queue.Task` rows so their plan is read by the one reader.
_TASKS = (
    "SELECT id, kind, key, payload, attempts, max_attempts, priority, paid, state"
    "  FROM acquisition_task WHERE kind = $1 AND payload ->> 'title_id' = $2 ORDER BY id"
)

# Decision 444's revive: pending, attempts 0, due now, the lease and the error cleared, the
# failed-for-good mark removed - so stage 6's `_FAILED_FOR_GOOD` stops parking the title's other keys
# the moment the task that failed is revived, which is what `stages.FAILED_FOR_GOOD` tells the
# operator will happen - and the caller's payload keys merged in (a launch's plan, decision 443).
# `$4::text::jsonb`, for `queue.enqueue`'s reason: `db/pool.py`'s codec encodes a bound dict twice.
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

# A title with no task to revive gets its `title:<id>` task, spelled as `pipeline.enqueue_title`
# spells it so the two can never be two claims: `_park_thin`'s inbox rows have no task at all
# (decision 336), and a ready title's tasks are done. An existing key in any state but leased is set
# pending the same way; a leased one is a walk that began after this action's check, and matches
# nothing here.
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

# `queue.skip`'s statement over the title's open tasks: skipped, the lease cleared, the note written,
# `last_error` kept because a task skipped after a failure still carries how it got here.
_SKIP_OPEN = """
UPDATE acquisition_task
   SET state = $3, lease_owner = NULL, lease_expires = NULL,
       result_note = left($4::text, $5), updated_at = now()
 WHERE kind = $1 AND payload ->> 'title_id' = $2 AND state = $6
RETURNING id
"""

# ABANDON TAKES THE TITLE OUT OF ITS BATCH (decision 448). The plan and the batch leave every task of
# the title - the ones this abandon skips and the ones a park, a failure or a finish closed before
# it - because a revive keeps the payload (decision 442's Cost) and every reader prices what it
# finds there: a batch plan left on a skipped task would come back with the next retry from a stage,
# and on a title whose batch named a provider since un-keyed it would refuse every paid retry for
# good. A relaunch merges its own.
_UNPLAN = """
UPDATE acquisition_task
   SET payload = payload - $3::text - $4::text, updated_at = now()
 WHERE kind = $1 AND payload ->> 'title_id' = $2 AND (payload ? $3 OR payload ? $4)
"""

# Decision 431's permanence per title, in `stages._FAILED_FOR_GOOD`'s two readings: the mark
# `pipeline._record_stop` writes on a permanent failure, or a task closed with attempts still left.
# A revive removes the mark, so this is exactly what only a board retry clears.
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
    """§8's ten stages as the board renders them, out of the driver's one tuple.

    The surface draws its segments from this and never from a list of its own, which is plan A2's
    "one tuple, shared with the backend's STAGES, so the two spellings cannot drift" - and the
    tuple is the one `test_acquire_pipeline.py` reads back out of the spec file.
    """
    return [{"number": stage.number, "name": stage.name} for stage in pipeline.STAGES]


def admitted(status: str) -> list[str]:
    """The actions a board row in `status` admits, per decision 444; none for a job in flight."""
    return list(_ADMITTED.get(status, ()))


def _stage(number: int) -> pipeline.Stage:
    return next(stage for stage in pipeline.STAGES if stage.number == number)


async def _hold(conn: asyncpg.Connection, title_id: int) -> None:
    """Take the title for this transaction, or refuse because a walk has it.

    `pg_try_advisory_xact_lock` in the driver's own namespace: the walk's session-level lock and this
    transaction-level one are the same lock, so either excludes the other, and this one is released
    by the commit or the rollback - there is no path out of an action that forgets to. Tried and
    never waited for, for `pipeline._claim_title`'s reason: an admin request that queued behind a
    walk of minutes would hold a connection and a board row lock for all of it. Re-entrant within
    one transaction, so `make_due` can take it again under an action that already holds it.
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
            f"this job is {job['status']}, so it is in flight and admits no action until it stops "
            "(decision 444)"
        )
    if action == RETRY:
        raise ActionRefused(
            f"a plain retry is offered on a failed job alone (decision 336), and this job is "
            f"{job['status']}: retry it from a stage instead"
        )
    raise ActionRefused(
        f"this job is {job['status']}, which admits {' or '.join(_SPOKEN[a] for a in allowed)} and "
        f"not {_SPOKEN[action]} (decision 444)"
    )


async def _walking_plans(conn: asyncpg.Connection, title_id: int) -> list[Any]:
    """The distinct batch plans of the tasks a revive will make due - the ones the paid stage will
    walk with - or `[None]`, the stored settings, when those tasks carry none."""
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

    For a flywheel launch to ask before it calls `make_due`, whose revive brings back every failed
    task and removes the mark: that is the board retry's own act, and decision 431 makes the retry
    "the only way back", so a batch that selected the title would otherwise buy attempt 1 and the
    named retry again from a provider that has refused the contract twice (decision 448). The board
    actions do not ask: they are the way back.
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

    THE CALLER HOLDS THE TRANSACTION, and this refuses to run outside one: the title lock below is
    transaction-scoped, so in autocommit it would be released the instant it was taken and guard
    nothing. The board actions here and the flywheel launch (decision 443) each call it inside
    their own, so a refusal anywhere in the caller rolls back everything this wrote.

    `stage` is clamped to the board's stage when there is a board row: a job is never moved forward
    past where it got to. The board is written `queued` with `reason` and `retry_after` cleared
    (decisions 421, 424); the title's failed, skipped and pending tasks are revived, `payload`'s keys
    merged into theirs; and a title with none gets its `title:<id>` task. A task leased after the
    lock was checked matches nothing, and zero tasks due is refused as in flight.
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
            f"from 1 to {reached}, the stage it reached, and never moves a job forward (decision 444)"
        )
    first = next((s for s in pipeline.STAGES if s.number >= stage and s.implemented), None)
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

    Admitted on every job that is not in flight (decision 444). When the first implemented stage at
    or after `stage` is the paid one, the spend cap is asked first, with the plan the revived task
    carries, and an over-cap month refuses the retry with the meter's sentence and changes nothing.
    """
    async with conn.transaction():
        job = await _open(conn, title_id)
        return await _retry_from(conn, title_id, job, stage, reason=None)


async def retry(conn: asyncpg.Connection, title_id: int) -> int:
    """The plain retry: a failed job walked again from the stage that failed. Tasks due.

    Failed alone (decision 336: `failed` "is the only state offering a plain retry"); a parked job
    is waiting on something that may change, and retrying it from its own stage is a retry from a
    stage, which it admits by that name.
    """
    async with conn.transaction():
        job = await _open(conn, title_id)
        _admit(job, RETRY)
        return await _retry_from(conn, title_id, job, int(job["stage"]), reason=RETRIED_FAILED)


async def abandon(conn: asyncpg.Connection, title_id: int) -> int:
    """Stop a parked or failed job: its open tasks skipped, the board `abandoned`. Tasks closed.

    A board state and not a delete (decision 444): the title, its derived rows and its raw documents
    stay, and a retry from a stage brings it back. The reason keeps what the job had stopped with,
    for `close_abandoned_boards`' reason: why it stopped is still true, and the abandonment is what
    happened to it afterwards.

    AND IT TAKES A LAUNCHED TITLE BACK OUT OF ITS BATCH (decision 448, amending 443). A launched
    row is `running` until the title's next stage-8 observation, and a walk the gate parks over the
    cap, or one whose batch named a provider since un-keyed, never reaches it: its row stayed running
    and its tasks kept the batch plan, so no relaunch could re-plan it and every retry priced the
    plan the household could not revise. So the title's running rows are queued again with no batch
    and the plan leaves its tasks, and the board says so.
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
