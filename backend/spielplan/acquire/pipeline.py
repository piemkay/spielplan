"""The ten-stage driver: park, resume, and the one place §8's stage names are spelled.

Writes `acquisition_task` (the schedule) and `acquisition_job` (the board, from stage 1 on). The board
stage is the resume point. Builds at most one Fetcher per drain, lazily (decision 373).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from spielplan.acquire import fetch, queue, stages
from spielplan.connectors import registry, resolve
from spielplan.flywheel import thin
from spielplan.llm import spend

log = logging.getLogger("spielplan.acquire.pipeline")

# One kind for the whole pipeline. One kind alone does not stop two workers walking one title via two
# keys; `_TITLE_LOCK` does.
TASK_KIND = "acquire"

# Advisory-lock namespace for per-title locks (`stages._MINT_LOCK = 8002` is its sibling). Two ints,
# a different lock space from the one-argument `hashtext` form.
_TITLE_LOCK = 8001

# Small: the worker loop is sequential and a batch must fit the job's budget.
DRAIN_LIMIT = 8


@dataclass(frozen=True)
class Stage:
    """One stage of §8's pipeline: its number, its name verbatim, and what the driver must know.

    Flags the driver reads before calling: `implemented` (the spend gate), `paid`, `fetches` (needs the
    drain's Fetcher), `reask_from` (where an expired park re-enters, decisions 421/467) and
    `observes_coverage` (§8.4's thin-facet feed, decision 440). Flags, not stage numbers.
    """

    number: int
    name: str
    run: Callable[[stages.StageContext], Awaitable[stages.Outcome]]
    paid: bool = False
    implemented: bool = True
    owner: str = "M5.1"
    fetches: bool = False
    reask_from: int | None = None
    observes_coverage: bool = False


# §8's ten, verbatim and in order; a test reads them back out of the spec. `owner` is provenance
# once a stage has a body.
STAGES: tuple[Stage, ...] = (
    Stage(1, "identify", stages.identify),
    Stage(2, "enrich", stages.enrich, owner="M5.3", fetches=True),
    Stage(3, "derive", stages.derive, owner="M5.3"),
    Stage(4, "reviews gate", stages.reviews_gate, owner="M5.3", reask_from=2),
    # Stages 5, 7 and 8 have had bodies since M5 (decisions 461, 462, 463).
    Stage(5, "dna pack", stages.dna_pack, owner="M5.4"),
    # The only paid stage. `reask_from=5` (decision 467): an expired park rebuilds the pack first.
    Stage(6, "dna extract", stages.dna_extract, paid=True, implemented=True, owner="M5.5", fetches=True,
          reask_from=5),
    Stage(7, "verify", stages.verify, owner="M5.4"),
    # The driver observes the title's facets when this stage finishes (decision 440).
    Stage(8, "project", stages.project, owner="M5.4", observes_coverage=True),
    Stage(9, "place", stages.place),
    Stage(10, "ready", stages.ready),
)

# The board's terminal state for a job that walked all ten.
READY = "ready"
RUNNING = "running"
PARKED = "parked"
FAILED = "failed"
# Written by board actions and flywheel launches (`actions.make_due`), never here.
QUEUED = "queued"

NO_SPEND_CAP = (
    "no spend cap is configured, and a paid stage never auto-retries past one. Configure "
    "the extraction providers and the cap in Admin, and this title resumes here"
)

# A task naming a deleted title; shown on the queue, since the board row cascaded away. The key is
# closed, so re-enqueueing only helps a `title:<id>` task or after a board revive.
TITLE_GONE = (
    "title {} no longer exists, so there is nothing left for this task to work on. Its board row "
    "went with it. Re-enqueue the Jellyfin item if the title should come back, or - if this task "
    "is the one keyed on that item - revive it from the acquisition board first: the queue will "
    "not re-enqueue a key it has already closed"
)

StageGate = Callable[[Stage, stages.StageContext], Awaitable[stages.Outcome | None]]


async def refuse_uncapped_spend(stage: Stage, ctx: stages.StageContext) -> stages.Outcome | None:
    """The default gate. None means "run it"; an `Outcome` means "do not, and record this".

    Declared no-ops pass; an implemented paid stage asks `llm/spend.cap_check` and parks with its
    sentence (`NO_SPEND_CAP` when no cap is set) and a deadline, never a failure (decision 348).
    """
    if not stage.paid or not stage.implemented:
        return None
    # The task's batch plan, through the same reader stage 6 uses (decision 442).
    batch = stages.task_plan(ctx.task)
    asked = {"title_id": ctx.title_id} if batch is None else {"title_id": ctx.title_id, "batch": batch}
    refusal = await spend.cap_check(ctx.conn, **asked)
    if refusal is None:
        return None
    if refusal.kind == spend.NO_CAP:
        return stages.park(
            NO_SPEND_CAP,
            until=stages.waiting_on_the_world(),
            detail={"paid_stage": stage.name},
        )
    return stages.park(
        refusal.reason,
        until=stages.waiting_on_the_world(),
        detail={"paid_stage": stage.name, **refusal.detail},
    )


# --- the board -----------------------------------------------------------------------------------


# `detail` accumulates one key per stage (jsonb `||`). `$6::text::jsonb`: the pool's encoder
# would double-encode a dict.
_BOARD = """
INSERT INTO acquisition_job (title_id, stage, status, reason, retry_after, detail)
VALUES ($1, $2, $3, $4, $5, $6::text::jsonb)
ON CONFLICT (title_id) DO UPDATE SET
    stage       = EXCLUDED.stage,
    status      = EXCLUDED.status,
    reason      = EXCLUDED.reason,
    retry_after = EXCLUDED.retry_after,
    detail      = acquisition_job.detail || EXCLUDED.detail,
    updated_at  = now()
"""


async def write_board(
    conn: asyncpg.Connection,
    title_id: int,
    *,
    stage: int,
    status: str,
    reason: str | None = None,
    retry_after: datetime | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """§6.6's board row for one title. The pipeline's only writer of `acquisition_job`.

    `reason` is written exactly as given, NULL included. The caller owes a live title (FK).
    `default=str` so any detail value serialises.
    """
    await conn.execute(
        _BOARD, title_id, stage, status, reason or None, retry_after,
        json.dumps(detail or {}, default=str),
    )


# --- task identity -------------------------------------------------------------------------------


def key_for_item(item: dict[str, Any]) -> str:
    """The `(kind, key)` key for a Jellyfin item: `jellyfin:<id>`, else a prefixed provider id.

    The one spelling, so `UNIQUE (kind, key)` dedupes every enqueuer.
    """
    jellyfin_id = str(item.get("Id") or "").strip()
    if jellyfin_id:
        return f"jellyfin:{jellyfin_id}"
    ids = resolve.identity(item)
    for column, prefix in (("imdb_id", "imdb"), ("tmdb_id", "tmdb"), ("tvdb_id", "tvdb")):
        if ids[column] is not None:
            return f"{prefix}:{ids[column]}"
    raise ValueError(
        "cannot key an acquisition task: the item carries neither a Jellyfin id nor a provider id"
    )


async def enqueue_item(
    conn: asyncpg.Connection, item: dict[str, Any], *, priority: int = 100
) -> bool:
    """Enqueue one Jellyfin item for the pipeline. True when a task was created.

    The payload carries the item, not a title id: the title may not exist yet (decision 322).
    """
    return await queue.enqueue(
        conn, TASK_KIND, key_for_item(item), {"item": item}, priority=priority
    )


async def enqueue_title(
    conn: asyncpg.Connection, title_id: int, *, priority: int = 100
) -> bool:
    """Enqueue a title that already exists (`title:<id>`). True when a task was created.

    Drains `_park_thin`'s inbox rows, which re-enter at the board's stage. A second key for one title;
    `_TITLE_LOCK` keeps two walks apart.
    """
    return await queue.enqueue(
        conn, TASK_KIND, f"title:{title_id}", {"title_id": int(title_id)}, priority=priority
    )


# --- the driver ----------------------------------------------------------------------------------


@dataclass
class TaskReport:
    """What one task did, for the drain's log and for a test to assert against."""

    task_id: int
    key: str
    title_id: int | None = None
    stage: int = 0
    status: str = ""
    reason: str = ""
    stages_run: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id, "key": self.key, "title_id": self.title_id,
            "stage": self.stage, "status": self.status, "reason": self.reason,
            "stages": list(self.stages_run),
        }


@dataclass
class DrainReport:
    leased: int = 0
    ready: int = 0
    parked: int = 0
    failed: int = 0
    reclaimed: dict[str, int] = field(default_factory=dict)
    tasks: list[TaskReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "leased": self.leased, "ready": self.ready, "parked": self.parked,
            "failed": self.failed, "reclaimed": dict(self.reclaimed),
            "tasks": [t.as_dict() for t in self.tasks],
        }


async def _resume_index(conn: asyncpg.Connection, title_id: int) -> int:
    """Where this title's board says the pipeline is, as an index into `STAGES`.

    The board, not the task, is the resume point; `ready` re-enters at stage 10. A park whose deadline
    has passed (by the database clock) re-enters at its stage's `reask_from`.
    """
    row = await conn.fetchrow(
        "SELECT stage, status = $2 AND retry_after <= now() AS closed"
        "  FROM acquisition_job WHERE title_id = $1",
        title_id, PARKED,
    )
    if row is None:
        return 0
    stage = max(1, min(len(STAGES), int(row["stage"])))
    reask = STAGES[stage - 1].reask_from
    if row["closed"] and reask is not None:
        return reask - 1
    return stage - 1


async def _remember_title(conn: asyncpg.Connection, task: queue.Task, title_id: int) -> None:
    """Record on the task which title stage 1 established; written by the lease holder only."""
    await conn.execute(
        "UPDATE acquisition_task SET payload = payload || $2::text::jsonb, updated_at = now()"
        " WHERE id = $1",
        task.id, json.dumps({"title_id": int(title_id)}),
    )


# `FetcherFactory` makes an un-entered Fetcher that the drain enters and exits once;
# `FetcherSupply` hands `run_task` the drain's one Fetcher, built on first call.
FetcherFactory = Callable[[asyncpg.Connection], Awaitable[fetch.Fetcher]]
FetcherSupply = Callable[[], Awaitable[fetch.Fetcher]]


async def _default_fetcher(conn: asyncpg.Connection) -> fetch.Fetcher:
    """The fetcher a real drain uses. One per drain, paced from `fetch_host_state`.

    The Jellyfin host comes from `connector_config`; an unreadable one means no exemption.
    """
    jellyfin = await registry.load_jellyfin(conn)
    return fetch.Fetcher(conn=conn, jellyfin_host=jellyfin.url)


class _OneFetcher:
    """One `Fetcher` per drain, built on the first stage that declares it fetches.

    A drain that never reaches such a stage never builds an HTTP client (exit measure 5).
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        stack: contextlib.AsyncExitStack,
        factory: FetcherFactory,
    ) -> None:
        self._conn = conn
        self._stack = stack
        self._factory = factory
        self._fetcher: fetch.Fetcher | None = None

    async def __call__(self) -> fetch.Fetcher:
        if self._fetcher is None:
            self._fetcher = await self._stack.enter_async_context(await self._factory(self._conn))
        return self._fetcher


async def _run_stage(
    stage: Stage, ctx: stages.StageContext, gate: StageGate,
    open_fetcher: FetcherSupply | None = None,
) -> stages.Outcome:
    """One stage, gated, with a raise turned into `fail` and a bad return turned into one too.

    `except Exception`, so a cancellation propagates. The gate runs inside the guard and must return
    None or a park/fail with a deadline; an `advance` is refused. The fetcher opens after the gate,
    outside the guard; a paid stage gets the supply and opens it only when a request is next.
    """
    try:
        refusal = await gate(stage, ctx)
        if refusal is not None:
            if not isinstance(refusal, stages.Outcome):
                return stages.fail(
                    f"the gate on stage {stage.number} ({stage.name}) returned "
                    f"{type(refusal).__name__} and not an Outcome: a gate answers with None to "
                    "run the stage, or with park or fail to refuse it (decision 348)"
                )
            if refusal.verb == stages.ADVANCE:
                return stages.fail(
                    f"the gate on stage {stage.number} ({stage.name}) answered with advance, "
                    "which would skip the stage rather than run it: a gate answers with None to "
                    "run the stage, or with park or fail to refuse it (decision 348)"
                )
            if refusal.verb == stages.PARK and refusal.until is None:
                return stages.fail(
                    f"the gate on stage {stage.number} ({stage.name}) parked with no deadline, "
                    "which closes the task for good instead of refusing one stage: a gate's park "
                    "carries an `until`, because what a gate waits on - a cap that is configured, "
                    "a budget period that rolls over, an operator setting - is by definition a "
                    "thing that may change (decision 348, decision 336)"
                )
            return refusal
    except Exception as exc:                                             # noqa: BLE001
        log.exception("acquisition stage %d (%s) raised", stage.number, stage.name)
        return stages.fail(f"{type(exc).__name__}: {exc}")
    if stage.fetches and ctx.fetcher is None and open_fetcher is not None:
        if stage.paid:
            ctx.open_fetcher = open_fetcher
        else:
            ctx.fetcher = await open_fetcher()
    try:
        outcome = await stage.run(ctx)
    except Exception as exc:                                             # noqa: BLE001
        log.exception("acquisition stage %d (%s) raised", stage.number, stage.name)
        return stages.fail(f"{type(exc).__name__}: {exc}")
    if not isinstance(outcome, stages.Outcome):
        return stages.fail(
            f"stage {stage.number} ({stage.name}) returned {type(outcome).__name__} and not an "
            "Outcome: a stage answers with advance, park or fail"
        )
    return outcome


async def run_task(
    conn: asyncpg.Connection,
    task: queue.Task,
    *,
    gate: StageGate = refuse_uncapped_spend,
    run_id: int | None = None,
    open_fetcher: FetcherSupply | None = None,
) -> TaskReport:
    """Walk one task through §8's stages from wherever its title's board row says it is.

    Advance, park or fail each write both tables. No board row before stage 1 establishes a title. A
    walk that cannot take `_TITLE_LOCK` yields. `open_fetcher` is asked only by stages that fetch.
    """
    ctx = stages.StageContext(
        conn=conn, task=task, title_id=_payload_title_id(task), run_id=run_id
    )
    report = TaskReport(task_id=task.id, key=task.key, title_id=ctx.title_id)
    if ctx.title_id is not None and not await _title_exists(conn, ctx.title_id):
        # The payload names a title that no longer exists: skip with the reason on the task (no board row
        # can reference it).
        report.status = PARKED
        report.reason = TITLE_GONE.format(ctx.title_id)
        await queue.skip(conn, task.id, report.reason)
        log.warning("acquisition task %d names title %d, which no longer exists", task.id,
                    ctx.title_id)
        return report

    held: int | None = None
    try:
        if ctx.title_id is not None:
            if not await _claim_title(conn, ctx.title_id):
                return await _yield_the_title(conn, task, report, ctx.title_id)
            held = ctx.title_id
        index = await _resume_index(conn, ctx.title_id) if ctx.title_id is not None else 0

        while index < len(STAGES):
            stage = STAGES[index]
            report.stage = stage.number
            # A factory that raises is the drain's failure, not this stage's.
            outcome = await _run_stage(stage, ctx, gate, open_fetcher)
            report.stages_run.append(stage.name)
            detail = {stage.name: outcome.detail} if outcome.detail else {}

            if outcome.verb != stages.ADVANCE:
                report.status = PARKED if outcome.verb == stages.PARK else FAILED
                report.reason = outcome.reason
                await _record_stop(conn, ctx, stage, outcome, detail)
                return report

            if outcome.title_id is not None and ctx.title_id is None:
                ctx.title_id = int(outcome.title_id)
                report.title_id = ctx.title_id
                await _remember_title(conn, task, ctx.title_id)
                # A resolved title may be walked by another worker's `title:<id>` task.
                if not await _claim_title(conn, ctx.title_id):
                    return await _yield_the_title(conn, task, report, ctx.title_id)
                held = ctx.title_id
                # Jump forward to the board's stage; the board knows how far the title got.
                resumed = await _resume_index(conn, ctx.title_id)
                if resumed > index:
                    index = resumed
                    # The board row just read is the FK, so the title existed a round trip ago.
                    await write_board(
                        conn, ctx.title_id, stage=STAGES[index].number, status=RUNNING,
                        detail=detail,
                    )
                    continue

            index += 1
            if ctx.title_id is None:
                # Stage 1 advanced without establishing a title; later stages fail on it.
                continue
            # The title can be deleted during the walk; skip with `TITLE_GONE` rather than an FK
            # violation.
            if not await _title_exists(conn, ctx.title_id):
                report.status = PARKED
                report.reason = TITLE_GONE.format(ctx.title_id)
                await queue.skip(conn, task.id, report.reason)
                log.warning("acquisition task %d lost title %d mid-walk at stage %d",
                            task.id, ctx.title_id, stage.number)
                return report
            # §8.4's feed, written as the stage finishes (decision 440). Not guarded: a failed write fails
            # the walk.
            if stage.observes_coverage:
                await thin.observe_title(conn, ctx.title_id)
            if index < len(STAGES):
                await write_board(
                    conn, ctx.title_id, stage=STAGES[index].number, status=RUNNING, detail=detail
                )
            else:
                await write_board(
                    conn, ctx.title_id, stage=stage.number, status=READY, detail=detail
                )
                report.status = READY
                await queue.complete(conn, task.id, f"ready at stage {stage.number}")
    finally:
        # Always release: a session lock travels back into the pool with the connection. Suppressed.
        if held is not None:
            with contextlib.suppress(Exception):
                await _release_title(conn, held)
    return report


TITLE_IN_FLIGHT = (
    "another worker is already walking this title, so this task waited rather than writing "
    "alongside it. It is due again immediately and has spent no attempt (decision 322)"
)


async def _claim_title(conn: asyncpg.Connection, title_id: int) -> bool:
    """Take this title for this walk, or say that someone else has it. Never waits (the loop is
    sequential).
    """
    return bool(
        await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", _TITLE_LOCK, int(title_id))
    )


async def _release_title(conn: asyncpg.Connection, title_id: int) -> None:
    await conn.fetchval("SELECT pg_advisory_unlock($1, $2)", _TITLE_LOCK, int(title_id))


async def _yield_the_title(
    conn: asyncpg.Connection, task: queue.Task, report: TaskReport, title_id: int
) -> TaskReport:
    """Hand this task back because another worker holds its title. Decision 336's `parked`.

    Deferred to now with no attempt spent, and no board row written.
    """
    report.status = PARKED
    report.reason = TITLE_IN_FLIGHT
    await queue.defer(conn, task.kind, task.key, datetime.now(UTC), reason=report.reason)
    log.info("acquisition task %d yields title %d to the worker already walking it",
             task.id, title_id)
    return report


async def _record_stop(
    conn: asyncpg.Connection,
    ctx: stages.StageContext,
    stage: Stage,
    outcome: stages.Outcome,
    detail: dict[str, Any],
) -> None:
    """Write a park or a failure to both tables, board first.

    Board first, so a crash overstates rather than understates. The detail says whether a retry is
    coming; a permanent failure (decision 431) is also marked on the task payload.
    """
    task = ctx.task
    if outcome.verb == stages.FAIL:
        detail = {stage.name: outcome.detail | {
            "attempts": task.attempts,
            "retrying": not outcome.permanent and task.attempts < task.max_attempts,
        }}
    # A title can vanish mid-walk; only write the board if it still exists.
    if ctx.title_id is not None and await _title_exists(conn, ctx.title_id):
        await write_board(
            conn, ctx.title_id,
            stage=stage.number,
            status=PARKED if outcome.verb == stages.PARK else FAILED,
            reason=outcome.reason,
            retry_after=outcome.until,
            detail=detail,
        )
    if outcome.verb == stages.PARK:
        if outcome.until is not None:
            await queue.defer(conn, task.kind, task.key, outcome.until, reason=outcome.reason)
        else:
            await queue.skip(conn, task.id, outcome.reason)
    else:
        async with conn.transaction():
            await conn.execute(_PERMANENCE, task.id, outcome.permanent)
            await queue.fail(conn, task.id, outcome.reason, permanent=outcome.permanent)


# `jsonb_build_object` rather than a bound jsonb, for `_BOARD`'s reason. `payload` is NOT NULL.
_PERMANENCE = (
    "UPDATE acquisition_task SET payload = CASE WHEN $2::bool"
    f" THEN payload || jsonb_build_object('{stages.FAILED_FOR_GOOD_MARK}', true)"
    f" ELSE payload - '{stages.FAILED_FOR_GOOD_MARK}' END WHERE id = $1"
)


def _payload_title_id(task: queue.Task) -> int | None:
    raw = (task.payload or {}).get("title_id")
    return int(raw) if raw is not None else None


async def _title_exists(conn: asyncpg.Connection, title_id: int) -> bool:
    """Does the board's foreign key still have something to point at?"""
    return bool(await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id))


async def drain(
    conn: asyncpg.Connection,
    *,
    limit: int = DRAIN_LIMIT,
    gate: StageGate = refuse_uncapped_spend,
    run_id: int | None = None,
    fetcher_factory: FetcherFactory | None = None,
) -> DrainReport:
    """Reclaim what died, lease up to `limit` ready tasks, and walk each one. §5.3's job body.

    Sequential. Free work only: stage 6 is gated, the task is not paid. One task's raise costs one task;
    a cancellation refunds the untouched rest. One Fetcher for the batch, flushed on every exit.
    """
    report = DrainReport()
    report.reclaimed = await queue.reclaim_expired(conn)
    await close_abandoned_boards(conn)
    await complete_landed_boards(conn)
    leased = await queue.lease(conn, [TASK_KIND], limit=limit)
    report.leased = len(leased)
    async with contextlib.AsyncExitStack() as stack:
        open_fetcher = _OneFetcher(conn, stack, fetcher_factory or _default_fetcher)
        await _walk_batch(conn, leased, report, gate=gate, run_id=run_id,
                          open_fetcher=open_fetcher)
    if report.leased:
        log.info("acquisition drain: %s", report.as_dict())
    return report


async def _walk_batch(
    conn: asyncpg.Connection,
    leased: list[queue.Task],
    report: DrainReport,
    *,
    gate: StageGate,
    run_id: int | None,
    open_fetcher: FetcherSupply,
) -> None:
    """`drain`'s loop, lifted out so the fetcher's `async with` can wrap it.

    The report is `drain`'s and is mutated here; nothing is returned.
    """
    for position, task in enumerate(leased):
        try:
            outcome = await run_task(conn, task, gate=gate, run_id=run_id,
                                     open_fetcher=open_fetcher)
        except asyncio.CancelledError:
            # Cancelled at the budget: refund the tasks behind this one (they did nothing); the one in
            # flight
            # keeps its attempt so a never-finishing stage stays bounded. Suppressed so the cancellation
            # wins.
            with contextlib.suppress(Exception):
                released = await queue.release(
                    conn, [t.id for t in leased[position + 1:]],
                    note="the drain's budget expired before this task was started",
                )
                if released:
                    log.warning(
                        "acquisition drain was cancelled at its budget; %d task(s) it had claimed "
                        "and not started are back in the queue", released,
                    )
            raise
        except Exception as exc:                                         # noqa: BLE001
            log.exception("acquisition task %d (%s) broke the driver", task.id, task.key)
            reason = f"the driver failed outside any stage: {type(exc).__name__}: {exc}"
            # The fourth exit writes both tables too, at the board's own stage. Read the title back
            # through
            # the task row: stage 1 wrote it after the lease.
            stopped = await conn.fetchrow(
                "SELECT j.title_id, j.stage FROM acquisition_task t"
                "  JOIN acquisition_job j ON j.title_id::text = t.payload ->> 'title_id'"
                " WHERE t.id = $1",
                task.id,
            )
            if stopped is not None:
                await write_board(conn, int(stopped["title_id"]), stage=int(stopped["stage"]),
                                  status=FAILED, reason=reason)
            await queue.fail(conn, task.id, reason)
            outcome = TaskReport(task_id=task.id, key=task.key, status=FAILED, reason=reason)
        report.tasks.append(outcome)
        if outcome.status == READY:
            report.ready += 1
        elif outcome.status == PARKED:
            report.parked += 1
        elif outcome.status == FAILED:
            report.failed += 1


# Terminal tasks beside a board still `running`, `parked` with a deadline, or `queued` are crash
# markers (a write that did not land). The NOT EXISTS skips titles with another live task; the
# reason is kept and the date cleared.
_CLOSE_ABANDONED = """
UPDATE acquisition_job j
   SET status = $2,
       reason = CASE WHEN coalesce(j.reason, '') = '' THEN $3
                     WHEN j.status = $8 THEN $3 || ' It had been queued with: ' || j.reason
                     ELSE $3 || ' It had parked with: ' || j.reason END,
       retry_after = NULL,
       updated_at = now()
 WHERE (j.status IN ($1, $8) OR (j.status = $7 AND j.retry_after IS NOT NULL))
   AND EXISTS (
           SELECT 1 FROM acquisition_task t
            WHERE t.payload ->> 'title_id' = j.title_id::text
              AND t.state = $4 AND t.last_error = $3
       )
   AND NOT EXISTS (
           SELECT 1 FROM acquisition_task t
            WHERE t.payload ->> 'title_id' = j.title_id::text
              AND t.state IN ($5, $6)
       )
RETURNING j.title_id
"""


async def close_abandoned_boards(conn: asyncpg.Connection) -> int:
    """Say on §6.6's board what `queue.reclaim_expired` has just said on the queue.

    The reaper closes tasks but knows no titles (decision 322), so the board is corrected here.
    """
    closed = await conn.fetch(
        _CLOSE_ABANDONED, RUNNING, FAILED, queue.ABANDONED,
        queue.FAILED, queue.PENDING, queue.LEASED, PARKED, QUEUED,
    )
    if closed:
        log.warning(
            "acquisition board: %d job(s) whose worker never came back are closed as failed",
            len(closed),
        )
    return len(closed)


# A worker that died between writing `ready` and `queue.complete`: complete the task instead of
# leaving it failed beside a ready board. The note, not `last_error`.
_COMPLETE_LANDED = """
UPDATE acquisition_task t
   SET state = $2, last_error = NULL, result_note = left($5::text, 500), updated_at = now()
 WHERE t.state = $1
   AND t.last_error = $3
   AND EXISTS (
           SELECT 1 FROM acquisition_job j
            WHERE j.title_id::text = t.payload ->> 'title_id'
              AND j.status = $4
       )
RETURNING t.id
"""

LANDED = (
    "the lease expired with no outcome reported, but this title's board row reads ready: the "
    "pipeline had finished and only the report of it was lost."
)


async def complete_landed_boards(conn: asyncpg.Connection) -> int:
    """Correct a task the reaper closed for work the board says actually landed.

    Only reaper-closed tasks (`last_error = queue.ABANDONED`) beside a `ready` board.
    """
    landed = await conn.fetch(
        _COMPLETE_LANDED, queue.FAILED, queue.DONE, queue.ABANDONED, READY, LANDED,
    )
    if landed:
        log.warning(
            "acquisition queue: %d task(s) were closed as abandoned although the board says the "
            "title is ready; the outcome was lost rather than the work", len(landed),
        )
    return len(landed)
