"""The ten-stage driver: park, resume, and the one place §8's stage names are spelled.

Spec v2.1 §8 (the pipeline and "Failure at any stage parks the job with a reason, retryable from
admin; paid stages (6) never auto-retry past the spend cap"), §5.3, §6.6; decisions 162, 322,
323, 336.

WHAT THIS MODULE IS. `acquire/stages.py` holds the ten callables and the contract they answer to;
this file holds the ORDER, the registry that says which of them costs money, and the loop that
turns one leased task into writes on two tables - `acquisition_task` (the schedule) and
`acquisition_job` (§6.6's board). Every one of M5.2 through M5.7 implements a stage against this
shape, so it is published here before those lanes open rather than discovered inside one of them.

ONE SPELLING OF THE TEN NAMES. `STAGES` carries §8's labels verbatim and in order, and
`test_acquire_pipeline.py` reads them back out of `docs/spielplan-spec_v2.1.md`'s own §8 block
rather than out of a list a test author retyped - so the tuple cannot drift from the spec without
the build saying so. Proposal 136 (`spec-v2.2-proposals.md:1617`) argues §6.6's board should show
these strings verbatim; whether that clause is adopted is decision 330's, which is M5.6's to take.
The code carries one tuple either way, because two spellings is the state where a board label and
a park reason name different stages.

PARK AND RESUME, AND WHERE THE RESUME POINT LIVES. §8: "Failure at any stage parks the job with a
reason, retryable from admin". The reason lives on the board, verbatim, and the STAGE NUMBER is
the resume point: a job parked at 4 re-enters at 4 and not at 1. That is what makes the raw store
worth having - §8's other promise, "All fetched bytes land in the app's own raw store, so
re-parsing is free forever" (`spec:398`), is only cashed if a resumed job re-reads those bytes
instead of re-fetching, and a resume that restarted at stage 1 would re-fetch by construction.

TWO TABLES, AND WHICH ONE ANSWERS WHICH QUESTION (decision 322). `acquisition_task` is the
schedule: what runs next, when, how many attempts are left, who holds the lease. `acquisition_job`
is the board: one row per title, what an operator sees. They are not redundant. The task can exist
before its title does, because stage 1 is what mints it; the board row cannot, because its primary
key is `title_id`. So the board is written from the moment stage 1 establishes a title and never
before.

TWO WRITERS REACH `acquisition_job` DURING A DRAIN, and the arrangement between them is what
keeps that safe. This module's `write_board` is one, with `DO UPDATE`. The other is
`placement/reconcile._park_thin`, whose insert is `ON CONFLICT (title_id) DO NOTHING`
(`reconcile.py:250-254`) and whose own comment says why: "a title already moving through the
pipeline must not be dragged back to stage 2 by a nightly sweep". It is NOT only the nightly
sweep, and this paragraph used to say it was: `stages.place` calls
`reconcile(scope="app_acquired")`, so stage 9 reaches `_park_thin` on the driver's own call stack,
inside a transaction the driver did not open. That is safe for exactly one reason and it is worth
stating rather than relying on - THE DRIVER HAS ALREADY WRITTEN A BOARD ROW FOR THIS TITLE BEFORE
STAGE 9 RUNS, so `DO NOTHING` does nothing. Drop or reorder that pre-stage-9 write and
`_park_thin` becomes the CREATOR of the row, at `(stage = 2, status = 'parked')`, and this title's
own stage-9 result lands on a history that is a sweep's. [M5.1 review cycle 1, M51-CRASH-07]

THE DRIVER MUST NEVER RELY ON THE SWEEP TO ADVANCE ANYTHING: the sweep's insert is a no-op on
every row this module has already written, so a stage that left work for it would leave it for
ever.

THE PAID SEAM, WHICH IS ALL M5.1 OWES M5.5. §8: "paid stages (6) never auto-retry past the spend
cap." A stage that runs and then checks a cap has already spent the money, so the contract has to
carry a stage that REFUSES TO RUN. `Stage.paid` marks which one, the driver consults a gate before
calling it, and `refuse_uncapped_spend` is the default: it lets a declared no-op through and
refuses an IMPLEMENTED paid stage while no cap is configured. No cap, no price table and no meter
are built here - those are M5.5's and M5.7's. What is built is the refusal, and the reason it is
built now is that it cannot be retrofitted: the day stage 6 gets a body is the day the gate has to
already exist, or the first drain after that commit bills the household.

NOTHING HERE FETCHES. `acquire/fetch.py` is deliberately not imported: stages 2 and 3 are M5.3's
and at M5.1 they are declared no-ops, so a dependency on the HTTP layer would be a dependency on a
module this file does not use.
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

from spielplan.acquire import queue, stages
from spielplan.connectors import resolve

log = logging.getLogger("spielplan.acquire.pipeline")

# One kind for the whole pipeline, where the corpus has one kind per SOURCE. The corpus's kinds
# are its unit of work; here the unit of work is a title walking ten stages, and splitting it per
# stage would put the sequence in the queue's ORDER BY - where two tasks of one title can be
# leased by two workers at once, which is exactly the write §14 risk 5 and decision 162 forbid.
#
# ONE KIND DOES NOT ITSELF PREVENT THAT STATE, and this comment implied it did. `enqueue_item`
# keys `jellyfin:<item>` and `enqueue_title` keys `title:<id>` - two rows, one kind, deliberately
# (see `enqueue_title`) - and `queue.lease` filters on `(state, next_attempt_at, paid, kind)` with
# `FOR UPDATE SKIP LOCKED`, which is exactly what hands the two rows to two workers. The board is
# read once per walk and is not a lock, so "`run_task` reconciles them through the board" holds
# only while the walks are SEQUENTIAL. `_TITLE_LOCK` below is the exclusion that makes it hold
# under the two-loop operating model this queue's own lease docstring calls "the ordinary state
# during a rolling restart". [M5.1 review cycle 2, seam322-06]
TASK_KIND = "acquire"

# The namespace half of the per-title advisory lock, following `sync/seen.py:101-102`'s note that
# a namespace is what stops a title id colliding with another feature's lock on the same integer.
# (`stages._MINT_LOCK = 8002` is its sibling: the claim stage 1 takes on a film's IDENTITY before
# any title id exists. Both numbers are registered in both comments so neither can move alone.)
# The existing numbers are a spec section and a serial - 6202 §6.2's finish, 6206 the ballot, 6303
# and 6304 §6.3's, 7303 and 7304 §7.3's - and §8 has no subsection, so this is 80 and the first
# serial. Two ints and not the single-argument `hashtext(name)` form, which `api/deps.py` records
# as a different lock space again.
_TITLE_LOCK = 8001

# What the drain takes in one tick. Small on purpose: `worker.py`'s loop is sequential (§5.3,
# `worker.py:987-1006`), so every task in a batch is time the rest of the loop does not get, and
# a batch that outruns the job's own budget is cancelled mid-stage. The bound that matters is the
# JOB's timeout against its interval; this is the number that keeps one tick's work predictable.
DRAIN_LIMIT = 8


@dataclass(frozen=True)
class Stage:
    """One stage of §8's pipeline: its number, its name verbatim, and what the driver must know.

    `implemented` is not decoration and not a to-do marker. The spend gate reads it: a stage that
    is declared a no-op cannot spend money, so refusing it would make the spine untestable end to
    end - which is D3's whole purpose - while refusing an implemented paid stage with no cap is
    §8's rule. `owner` is the milestone that owes the body, so a stub surviving into M5.6 is
    visible in the tuple as well as in its own docstring.
    """

    number: int
    name: str
    run: Callable[[stages.StageContext], Awaitable[stages.Outcome]]
    paid: bool = False
    implemented: bool = True
    owner: str = "M5.1"


# §8's ten, verbatim and in order (`docs/spielplan-spec_v2.1.md` §8's own block). The names carry
# spaces because the spec's do; the callables carry underscores because Python's do, and the test
# that reads the spec back asserts the pair rather than either alone.
STAGES: tuple[Stage, ...] = (
    Stage(1, "identify", stages.identify),
    Stage(2, "enrich", stages.enrich, implemented=False, owner="M5.3"),
    Stage(3, "derive", stages.derive, implemented=False, owner="M5.3"),
    Stage(4, "reviews gate", stages.reviews_gate, implemented=False, owner="M5.3"),
    Stage(5, "dna pack", stages.dna_pack, implemented=False, owner="M5.4"),
    # The only paid one, and marked so while it still spends nothing. See `refuse_uncapped_spend`.
    Stage(6, "dna extract", stages.dna_extract, paid=True, implemented=False, owner="M5.5"),
    Stage(7, "verify", stages.verify, implemented=False, owner="M5.4"),
    Stage(8, "project", stages.project, implemented=False, owner="M5.4"),
    Stage(9, "place", stages.place),
    Stage(10, "ready", stages.ready),
)

# The board's terminal state for a job that walked all ten. `acquisition_job.status`'s CHECK
# already admits it (`0005_ledger.sql:136`), written at M0 against a pipeline that did not exist.
READY = "ready"
RUNNING = "running"
PARKED = "parked"
FAILED = "failed"

NO_SPEND_CAP = (
    "no spend cap is configured, and §8 says a paid stage never auto-retries past one. Configure "
    "the extraction providers and the cap in Admin, and this title resumes here"
)

# A task that names a title the database no longer has. Shown on the queue and never on the board,
# because the board row went with the title: `acquisition_job.title_id` is
# `REFERENCES title(id) ON DELETE CASCADE`. [M5.1 review cycle 1, seam322-01]
#
# THE ADVICE NAMES ITS OWN CONSTRAINT, which is `stages.NO_PROVIDER_ID`'s rule applied to the
# sentence that rule's own fix introduced: "a reason shown verbatim to an operator has to name the
# lever that exists". This park is a `queue.skip`, so THIS task's key is closed and
# `queue.enqueue`'s `ON CONFLICT (kind, key) DO NOTHING` makes re-enqueueing the same item a no-op
# against it. Re-enqueueing is still the right advice and is still the only lever - for a
# `title:<id>` task it is a different key and works outright, and for a `jellyfin:` task it works
# once decision 330's revive action arrives at M5.6 - but an operator who tried it and watched
# nothing happen would be reading a sentence that had not told them why.
# [M5.1 review cycle 2, M51-C2-322-05]
TITLE_GONE = (
    "title {} no longer exists, so there is nothing left for this task to work on. Its board row "
    "went with it. Re-enqueue the Jellyfin item if the title should come back, or - if this task "
    "is the one keyed on that item - revive it from the acquisition board first: the queue will "
    "not re-enqueue a key it has already closed"
)

StageGate = Callable[[Stage, stages.StageContext], Awaitable[stages.Outcome | None]]


async def refuse_uncapped_spend(stage: Stage, _ctx: stages.StageContext) -> stages.Outcome | None:
    """The default gate. None means "run it"; an `Outcome` means "do not, and record this".

    §8's clause is about a stage that BILLS: "paid stages (6) never auto-retry past the spend
    cap". Two readings were available and only one of them survives contact with M5.1's tree.

    A gate that refuses every paid stage unconditionally would park every task at stage 6 today,
    because stage 6 is `paid=True` from this commit. §8's own ten-stage walk would then be
    unreachable and M5.1's exit criterion - a task reaching `ready` with stages 2-8 declared
    no-ops - would be unsatisfiable, so the spine could not be proved before the lanes that plug
    into it opened. That is the outcome the milestone exists to prevent.

    So the gate asks the question §8 is actually asking: is this stage going to spend money? A
    declared no-op cannot. An implemented paid stage can, and until a cap exists there is nothing
    for "past the spend cap" to mean - so it parks, with a reason an operator can act on, and
    parking never auto-fails (decision 336). The day M5.5 gives `stages.dna_extract` a body,
    `implemented` becomes True and this refusal starts firing, which is the seam holding: M5.5
    cannot ship a billing stage that runs without a cap having been supplied.

    WHAT HOLDS `implemented` TO REALITY IS A TEST AND NOT A CONSTRUCTION, and the sentence that
    used to stand here - "neither milestone has to remember, because neither can forget" - was
    false. `implemented` is a hand-written literal in `STAGES` below and nothing in the tree ties
    it to whether `stages.dna_extract` has a body, so a milestone that wrote the billing call and
    left the flag would pass this gate and run.
    `test_acquire_pipeline.py::test_every_stage_declared_a_no_op_returns_its_stub_marker` is what
    makes that impossible: it calls every `implemented=False` stage and asserts the stub marker,
    so a stage that gains a body reddens the build with a message naming the stage that got one -
    and the only way to green it is to set the flag, which is the moment this gate starts firing.
    [M5.1 review cycle 1, M51-REV-04, M51-REV-PAID-01]

    A park and not a failure, and the difference is not cosmetic: `fail` spends an attempt and
    four of them close the task for good, so a household that has not yet configured a cap would
    lose the title rather than wait for the setting. WITH A DEADLINE, for the same reason and by
    the same arithmetic: a park with no deadline is `queue.skip`, which closes the task on attempt
    one and is therefore strictly worse than the failure this paragraph rejects. Configuring a cap
    is a thing a person does, so this is `stages.waiting_on_the_world()`; the stage never runs and
    nothing is billed while it waits. M5.5's own "over the spend cap" park is a different sentence
    about a different state - a cap that EXISTS and is reached - and §8's "never auto-retry past
    the spend cap" is about that one, which M5.5 writes. [M5.1 review cycle 1, M51-CRASH-01]
    """
    if not stage.paid or not stage.implemented:
        return None
    return stages.park(
        NO_SPEND_CAP,
        until=stages.waiting_on_the_world(),
        detail={"paid_stage": stage.name},
    )


# --- the board -----------------------------------------------------------------------------------


# `detail` is CONCATENATED rather than replaced, so the board accumulates one key per stage and an
# operator reading a parked job can see what the stages before it did. jsonb `||` is a shallow
# merge, which is what is wanted: one stage's key never silently merges into another's.
#
# `$6::text::jsonb` and not `$6::jsonb`, for the reason `acquire/queue.py::enqueue` states and
# `backup/movie_data.py:767-770` states before it: `db/pool.py` registers `json.dumps` as the
# jsonb encoder, so a dict passed against a `jsonb`-typed parameter is encoded twice and stored as
# a JSON *string* that every reader then iterates character by character.
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

    `reason` is written EXACTLY as the stage gave it, including on an advance where it is NULL:
    the column's own comment says "shown verbatim on the admin board" (`0005_ledger.sql:138`), and
    a reason left over from a park the next stage cleared is a sentence describing a state the row
    is no longer in.

    `retry_after` is written whenever the park carried a time. The column was added for §8 stage
    4's thirty-day review-accrual window and its comment names that case, but the fact it records
    - when this job comes back by itself - is the same fact at any stage, and a board that could
    not state it for a job deferred by the spend cap would be showing an operator a wait with no
    end date.

    THE CALLER OWES THIS FUNCTION A LIVE TITLE. `acquisition_job.title_id` is
    `PRIMARY KEY REFERENCES title(id)` (`0005_ledger.sql:134`), so an id naming a row that is gone
    is a `ForeignKeyViolationError` from a statement every stop path runs. `run_task` and
    `_record_stop` below check before they call; M5.2 through M5.7 will call this too, and the
    same debt is theirs. [M5.1 review cycle 1, seam322-01]

    THAT SENTENCE WAS TRUE OF THE STOP PATHS AND NOT OF THE ADVANCE PATH, which checked once at
    the top of `run_task` against the payload's title id and then wrote three more times against a
    title stage 1 had established - a title another session can delete while a stage runs. Every
    advance-path write now pays the same `SELECT 1`, so the debt this paragraph describes is one
    the driver actually settles; the one write still taken on trust says so where it is made.
    A later author adding a call here inherits the debt, not an exemption.
    [M5.1 review cycle 4, seam322-C4-01]

    `default=str` ON THE DUMP, because `detail` is whatever a stage returned and `Outcome.detail`
    is published as `dict[str, Any]` with no serialisation contract anywhere in this package. A
    stage-4 author writing `{"window_closes": <datetime>}` - the obvious shape for the thirty-day
    window - or an M5.4 stage handing back a numpy scalar out of a projection would otherwise
    raise `TypeError` from inside the driver's own bookkeeping. `str` and not a refusal: a board
    detail is an operator's diagnostic, and losing the type of one value is a far smaller harm
    than losing the row. [M5.1 review cycle 1, M51-REV-01, M51-CRASH-02]
    """
    await conn.execute(
        _BOARD, title_id, stage, status, reason or None, retry_after,
        json.dumps(detail or {}, default=str),
    )


# --- task identity -------------------------------------------------------------------------------


def key_for_item(item: dict[str, Any]) -> str:
    """The `(kind, key)` key for a Jellyfin item. Decision 322's "keyed on the Jellyfin item".

    The item id first, because it is the household's own identity for the thing and is what
    `resolve.resolve_title_id` tries first; a provider id as the fallback, so §8.4's flywheel can
    enqueue work for something Jellyfin has never shown us. Prefixed, because a bare `tt0113277`
    and a bare `949` in one column is a key space where two namespaces can collide.

    This function is the one spelling. A second enqueuer that built the key its own way would
    defeat `UNIQUE (kind, key)` - the whole point of which is that the nightly sweep and the
    flywheel enqueueing the same title is a no-op rather than two crawls.
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

    The payload carries the ITEM and not a title id, because there may be no title yet - that is
    decision 322's reason for a queue table with no foreign key to `title`. M4.11's sweep already
    produces exactly this input: `ResolveReport.unmatched` is the list §12's note calls "M5's
    acquisition pipeline consumes this sweep's unmatched report".
    """
    return await queue.enqueue(
        conn, TASK_KIND, key_for_item(item), {"item": item}, priority=priority
    )


async def enqueue_title(
    conn: asyncpg.Connection, title_id: int, *, priority: int = 100
) -> bool:
    """Enqueue a title that already exists. True when a task was created.

    THE INBOX IS NOT EMPTY ON A REAL INSTALL. `placement/reconcile._park_thin` has been writing
    `(stage = 2, status = 'parked')` rows since M4.13 for every thin-but-placed title, and §5.3
    calls them what they are: "parked as acquisition jobs for M5 enrichment". Nothing has ever
    drained them, because until this milestone there was nothing to drain them with. Those rows
    are the pipeline's inbox rather than a backlog of failures (decision 336), and a task made
    here re-enters at the stage the board records - 2, enrich - rather than at 1.

    Keyed `title:<id>` and therefore distinct from the same title's `jellyfin:<item>` task. That
    is a deliberate consequence rather than an oversight: the two keys are two different claims
    about what the work is, and `run_task` reconciles them through the board, which is the row
    that knows how far the title actually got.

    THE BOARD RECONCILES THE TWO CLAIMS AND DOES NOT SERIALISE THEM, which this paragraph used to
    run together. `_resume_index` is a `SELECT` with no `FOR UPDATE` and the walk holds no
    transaction, so the reconciliation is a snapshot: it makes a second walk START where the first
    one got to, and says nothing about the two running at once. `run_task` takes `_TITLE_LOCK` for
    that. [M5.1 review cycle 2, seam322-06]
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

    The BOARD is the resume point and the task is not, because the board is per title and the task
    is per key: a title can carry a `jellyfin:` task from the sweep and a `title:` task from the
    flywheel, and both must re-enter where the title actually got to rather than where their own
    row was created. A title with no board row has not started, which is index 0.

    A job already at `ready` re-enters at stage 10, which re-checks and re-stamps rather than
    doing nothing. That is D5's idempotence rather than a wasted call: the alternative - returning
    "past the end" - would make a re-run of a finished task a silent no-op, and a silent no-op is
    indistinguishable from a driver that stopped working.
    """
    row = await conn.fetchrow(
        "SELECT stage FROM acquisition_job WHERE title_id = $1", title_id
    )
    if row is None:
        return 0
    stage = max(1, min(len(STAGES), int(row["stage"])))
    return stage - 1


async def _remember_title(conn: asyncpg.Connection, task: queue.Task, title_id: int) -> None:
    """Record on the task which title stage 1 established.

    Written by the worker HOLDING THE LEASE and adding a key, which is the one safe shape for a
    payload write: `queue.enqueue`'s `ON CONFLICT DO NOTHING` refuses to touch a payload precisely
    because "a second enqueue that clobbered the payload of a task already leased would change the
    work under a running stage". This does not change the work; it records what the work turned
    out to be, so a reclaim after a crash does not have to re-derive it.
    """
    await conn.execute(
        "UPDATE acquisition_task SET payload = payload || $2::text::jsonb, updated_at = now()"
        " WHERE id = $1",
        task.id, json.dumps({"title_id": int(title_id)}),
    )


async def _run_stage(
    stage: Stage, ctx: stages.StageContext, gate: StageGate
) -> stages.Outcome:
    """One stage, gated, with a raise turned into `fail` and a bad return turned into one too.

    `except Exception` and NOT `except BaseException`: `worker.py`'s `_tick` bounds every job with
    `asyncio.wait_for`, which CANCELS, and `CancelledError` is not an `Exception` in this Python -
    so a cancellation must propagate rather than be written to the board as a stage failure. The
    task it leaves behind is `leased` with an expiry, which is the row `queue.reclaim_expired`
    exists for and the only recovery that survives a `kill -9` at all.

    THE GATE IS INSIDE THE GUARD. It was one line above it, which made the one extension point
    this milestone publishes - `StageGate`, which M5.5 fills with a spend cap read out of the
    database - the one call in the driver whose failure could not be described as a stage outcome.
    A cap that cannot be read is decision 336's `failed` exactly ("this stage raised and will raise
    again"), and it is now recorded as one instead of escaping `run_task`.
    [M5.1 review cycle 1, M51-CRASH-02]

    AND THE RETURN VALUE IS CHECKED, because this function's contract with a stage author is that
    a stage cannot break the tick - only its own task. That contract stopped at the `return`: a
    stage with a branch that falls off the end returns None, and `run_task` then dereferenced
    `.detail` on it and raised `AttributeError` from the driver, with the task left `leased`, the
    board frozen at `running` and nothing naming the stage. Eighteen stage bodies are still to be
    written against this shape, so the refusal says which stage and what it returned, which
    `AttributeError: 'NoneType'` does not. [M5.1 review cycle 1, seam322-05]

    THE GATE'S RETURN IS CHECKED BY THE SAME SENTENCE, because the paragraph above was written
    about this function's whole contract and then applied to one of its two calls. `return
    refusal` left the `try` three lines above the guard, so a gate answering with anything other
    than an `Outcome` landed on `run_task`'s own `outcome.detail` with no handler between: through
    `drain` that is a task failed with "the driver failed outside any stage", naming no stage and
    spending an attempt, and through `run_task` - which every M5.2-M5.7 test and `ops/` script
    calls directly - it is a task left `leased` with nothing written at all. `StageGate` is the one
    extension point this milestone publishes and `Outcome | None` is a type annotation nothing in
    this repo enforces, so the gate deserves the guard the stage already has, not less of one.

    AND AN `ADVANCE` FROM A GATE IS REFUSED, which is the worse shape rather than the loud one.
    Decision 348 gives a gate exactly two answers - None means "run it", anything else means "do
    not, and record this" - so a gate answering "the cap is fine" with `advance()` is not a near
    miss, it is the stage SKIPPED: the walk runs on with the billing stage's name in its report
    and its body never called, and §6.6's board shows a clean walk. A gate that means "run it" has
    to say None, and this is where that is enforced rather than hoped for.
    [M5.1 review cycle 4, M51-C4-PAID-01]

    AND A GATE'S PARK CARRIES A DEADLINE, which is the one gate mistake of the three that cannot
    be taken back. `_record_stop` turns a park with no `until` into `queue.skip`, and
    `stages.waiting_on_the_world`'s docstring states what that means in this tree: nothing moves a
    row out of `skipped` - `lease` claims `pending` only, `defer` is fenced on pending-or-leased,
    neither reaper matches, `enqueue` is `ON CONFLICT DO NOTHING`, and decision 330's revive is
    M5.6's. So where the other two arms cost one stage or one attempt, this one closes the task on
    its first refusal, and `stages.park`'s signature makes `until` optional while
    `ROADMAP-M5.md:324` spells M5.5's cap refusal as the bare sentence "stage 6 parks `over spend
    cap`" - which is the natural reading of a gate that "never auto-retries". A STAGE may
    legitimately park with no deadline: stage 1's `NO_PROVIDER_ID` waits on a person editing
    Jellyfin and says so. A GATE may not, because what a gate waits on is configuration - a cap
    that is set, a period that rolls over, a setting an operator changes - which is decision 336's
    "waiting on something that may change" in its plainest form. M5.1 made this exact mistake in
    `refuse_uncapped_spend` (cycle 1) and in `stages.place` (cycle 2); a class this milestone has
    shipped twice is not hypothetical at the one extension point it publishes for a milestone that
    bills real money. [M5.1 review cycle 4 second pass, M51-C4-PAID-04]
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
) -> TaskReport:
    """Walk one task through §8's stages from wherever its title's board row says it is.

    The three exits, and each writes BOTH tables because they answer different questions:

      * every stage advanced - the board reads `stage = 10, status = 'ready'` and the task is
        `complete`d;
      * a stage parked - the board reads that stage with the reason verbatim, and the task is
        `defer`red to the park's own instant or `skip`ped when the park named none. Decision 336:
        a park with a time waits for the world, a park without one waits for an operator, and
        neither spends an attempt;
      * a stage failed - the board reads `failed` with the reason, and `queue.fail` decides in SQL
        whether that is a retry or the end of the task's attempts.

    NOTHING IS WRITTEN TO THE BOARD BEFORE STAGE 1 ESTABLISHES A TITLE, because the board's
    primary key is `title_id` and decision 322's whole point is that the task can exist first. A
    task that parks at stage 1 - "no provider id", decision 323 - therefore writes no board row at
    all, and the reason lives on the TASK, where `queue.skip` puts it. That is the honest place
    for it: there is no title for §6.6 to show a row about, and inventing one to hang a reason on
    would be the mint decision 323 forbids.

    AND A FOURTH OUTCOME, WHICH IS NOT A STAGE'S: this walk yields because another worker is
    already walking this title. `_TITLE_LOCK` is taken the moment a title is established - from
    the payload at the top, or from stage 1's mint below - and released at every exit. Decision
    322 gives one title two keys on purpose (`enqueue_title`), `queue.lease` filters on the kind
    and not on the title, and `FOR UPDATE SKIP LOCKED` is what hands those two rows to two
    workers - so under the rolling restart `queue.lease`'s own docstring calls "the ordinary
    state", two loops walked one title in parallel and both wrote. The board cannot stop that: it
    is read at most twice per walk, with no `FOR UPDATE` and no transaction around it, so it
    decides where a walk STARTS and nothing about what runs at the same time. A session-level
    advisory lock is this tree's answer to that question in five other places
    (`ledger/refit.py`, `rank/drop.py`, `sync/seen.py`, `tonight/ballot.py`, `importer/bundle.py`),
    it costs nothing when uncontended, and Postgres releases it if the worker dies - which is the
    only release that survives a `kill -9`. The loser DEFERS with no attempt spent and writes no
    board row, because the row belongs to the walk that holds the title.
    [M5.1 review cycle 2, seam322-06]
    """
    ctx = stages.StageContext(
        conn=conn, task=task, title_id=_payload_title_id(task), run_id=run_id
    )
    report = TaskReport(task_id=task.id, key=task.key, title_id=ctx.title_id)
    if ctx.title_id is not None and not await _title_exists(conn, ctx.title_id):
        # A task whose payload names a title that is no longer there. Decision 322 gives this queue
        # no foreign key to `title` on purpose - so that a task can exist before its title does -
        # and the same absence lets one outlive its title, which `test_acquire_schema.py` builds
        # deliberately. `stages.identify` short-circuits on a payload title id without touching the
        # database, so the first thing the driver would do with such a task is INSERT the stage-2
        # board row, and `acquisition_job.title_id` is `REFERENCES title(id)`: a
        # ForeignKeyViolationError from the driver's own bookkeeping, outside every handler.
        #
        # Handled the way decision 323 handles an item with no provider id, and for the same
        # reason `run_task`'s docstring gives below: there is no title for §6.6 to show a row
        # about, so the reason lives on the TASK, which has no foreign key. A skip and not a fail,
        # because nothing raised and no retry can put the title back.
        # [M5.1 review cycle 1, seam322-01, M51-REV-06]
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
            outcome = await _run_stage(stage, ctx, gate)
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
                # A minted title is one nobody else can hold; a RESOLVED one is exactly the title
                # another worker's `title:<id>` task may be walking right now, and stage 1 is the
                # first moment this walk knows which it got. [M5.1 review cycle 2, seam322-06]
                if not await _claim_title(conn, ctx.title_id):
                    return await _yield_the_title(conn, task, report, ctx.title_id)
                held = ctx.title_id
                # The board may know more than the task does. A title reached through one key can
                # already be in flight under another - the sweep's `jellyfin:` task and the
                # flywheel's `title:` task are two claims about one title - and the board is the
                # row that knows how far it actually got. Jumping FORWARD only: a board behind
                # this run is a row this run is about to correct.
                resumed = await _resume_index(conn, ctx.title_id)
                if resumed > index:
                    index = resumed
                    # Not guarded by `_title_exists` the way the two writes below are: `_resume_index`
                    # one line up read the board row whose existence is the foreign key, so this
                    # branch is only reached when the title was there a round trip ago. The window
                    # left is the one no read can close.
                    await write_board(
                        conn, ctx.title_id, stage=STAGES[index].number, status=RUNNING,
                        detail=detail,
                    )
                    continue

            index += 1
            if ctx.title_id is None:
                # Stage 1 advanced without establishing a title. Nothing can: every later stage
                # needs one, and `stages.place` and `stages.ready` say so by failing.
                continue
            # CHECKED ON THE ADVANCE PATH TOO, which is what `write_board`'s "the caller owes this
            # function a live title" was asserting about `run_task` while `run_task` checked once,
            # at the top, against the payload's title id. A title established by stage 1 can be
            # deleted DURING the walk - an operator repairing a bad mint is the live case - and
            # the next board write is then a `ForeignKeyViolationError` raised from the driver's
            # own bookkeeping, outside every handler: `drain` catches it and closes the task with
            # "the driver failed outside any stage", which names no stage and spends an attempt,
            # where `TITLE_GONE` already names the state and the lever. Skipped for the reason the
            # top-of-function check gives: nothing raised, and no retry can put the title back.
            # [M5.1 review cycle 4, seam322-C4-01]
            if not await _title_exists(conn, ctx.title_id):
                report.status = PARKED
                report.reason = TITLE_GONE.format(ctx.title_id)
                await queue.skip(conn, task.id, report.reason)
                log.warning("acquisition task %d lost title %d mid-walk at stage %d",
                            task.id, ctx.title_id, stage.number)
                return report
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
        # UNCONDITIONALLY, and on every exit including a cancellation. A session-level advisory
        # lock outlives its transaction and travels back into the pool with the connection, which
        # is the cost `sync/seen.py:296-300` states for the same choice - so a walk that forgot to
        # release would wedge that title for the life of the connection with nothing saying so.
        # Suppressed for the same reason the release in `drain` is: an unlock that fails must not
        # replace whatever this walk was already reporting. [M5.1 review cycle 2, seam322-06]
        if held is not None:
            with contextlib.suppress(Exception):
                await _release_title(conn, held)
    return report


TITLE_IN_FLIGHT = (
    "another worker is already walking this title, so this task waited rather than writing "
    "alongside it. It is due again immediately and has spent no attempt (decision 322)"
)


async def _claim_title(conn: asyncpg.Connection, title_id: int) -> bool:
    """Take this title for this walk, or say that someone else has it. Never waits.

    `pg_try_advisory_lock` and not `pg_advisory_lock`: §5.3's loop is sequential, so a drain that
    BLOCKED on another worker's title would hold the whole tick behind it, and a task handed back
    is exactly what the queue is for. Two ints rather than the one-argument `hashtext` form, whose
    lock space is a different one (`api/deps.py`), and a title id is already an int4.
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

    DEFERRED AND NOT SKIPPED: the thing that may change is the other walk finishing, which it will
    - the drain's budget is a fraction of `LEASE_SECONDS` - so this is a wait with a deadline, and
    the deadline is now: the next tick leases it, by which time the other walk is over. No attempt
    is spent, because nothing was attempted.

    AND NO BOARD ROW IS WRITTEN. §6.6's row for this title belongs to the walk that holds it, and
    stamping "parked" over its `running` would show an operator a stalled title while the pipeline
    was moving it. [M5.1 review cycle 2, seam322-06]
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

    Board first, and the order is not arbitrary: the board is what an operator reads and the task
    is what the drain reads, so a crash between the two leaves a row that OVERSTATES how stuck the
    job is rather than one that understates it. The task is then still `leased`, its lease expires,
    `reclaim_expired` returns it, and the next drain re-enters at the stage the board recorded -
    which is the same place it would have resumed from anyway.

    A FAILURE RECORDS WHETHER A RETRY IS COMING, computed here rather than read back from
    `queue.fail`'s return. The queue computes that branch in SQL against the row's own `attempts`
    - one statement rather than a read and a write, so two drains failing one task cannot both
    read one counter - and the same arithmetic is available here because attempts are counted on
    the CLAIM: the leased task already carries the number the SQL will compare. The board's
    `status` is `failed` either way (decision 336 makes `failed` the state that "raised and will
    raise again"); what the detail adds is the one thing an operator cannot infer from it, which
    is whether the machine will try again or whether they must.
    """
    task = ctx.task
    if outcome.verb == stages.FAIL:
        detail = {stage.name: outcome.detail | {
            "attempts": task.attempts, "retrying": task.attempts < task.max_attempts,
        }}
    # CHECKED HERE TOO, and not only at the top of `run_task`, because a title can vanish DURING a
    # walk and `stages.ready` has a guard for exactly that - `fail(f"title {id} no longer exists")`.
    # That guard was dead: its failure routed straight into this function, whose board write is
    # the FK violation, so the one sentence written for the state could only ever be replaced by a
    # traceback. The task keeps the reason either way. [M5.1 review cycle 1, seam322-01]
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
        await queue.fail(conn, task.id, outcome.reason)


def _payload_title_id(task: queue.Task) -> int | None:
    raw = (task.payload or {}).get("title_id")
    return int(raw) if raw is not None else None


async def _title_exists(conn: asyncpg.Connection, title_id: int) -> bool:
    """Does the board's foreign key still have something to point at?

    One `SELECT 1` per stop, and per task that arrived carrying a title id. That is the price of
    the bridge decision 322 built: `acquisition_task` has no foreign key to `title` so that a task
    can name work for a title that does not exist yet, and `acquisition_job.title_id` is a foreign
    key so that a board row cannot describe a title nobody has. The driver is the only thing that
    crosses between them, so the driver is where the check belongs.
    """
    return bool(await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id))


async def drain(
    conn: asyncpg.Connection,
    *,
    limit: int = DRAIN_LIMIT,
    gate: StageGate = refuse_uncapped_spend,
    run_id: int | None = None,
) -> DrainReport:
    """Reclaim what died, lease up to `limit` ready tasks, and walk each one. §5.3's job body.

    RECLAIM FIRST, which is `mdc/runner.py:94-98`'s order and for its reason: a task whose worker
    was killed is `leased` past its expiry and invisible to the lease query, so a drain that
    leased before reclaiming would report an empty queue while holding work nobody is doing.

    SEQUENTIAL, one task after another, and no pool. §5.3's worker loop is sequential
    (`worker.py:987-1006`) and every budget in `JOBS` is at or under its own interval because of
    it; a per-kind worker pool is a real change to what the box does under load and needs its own
    decision (plan §8). The corpus runs a pool of four because it is crawling nineteen thousand
    titles against eleven hosts; a household acquiring a film a week is not that problem.

    `paid` IS NOT PASSED, so `queue.lease` takes free work only - its default, and the WHERE-clause
    refusal its docstring argues for. A paid task kind does not exist at M5.1; when one does, the
    caller that drains it will be the one that knows a cap has been checked.

    ONE TASK'S FAILURE COSTS ONE TASK, which the loop below is written to guarantee and did not.
    `queue.lease` claims the whole batch in one statement and counts an attempt ON THE CLAIM, so a
    raise anywhere in `run_task` used to leave every task ordered after it `leased`, never run, and
    one attempt poorer - and `queue.reclaim_expired` then read that as a killed worker and, four
    ticks later, closed up to `DRAIN_LIMIT` untouched titles for good with a sentence saying a
    worker had been stopped. `mdc/runner.py:161-205` does not have this shape: the corpus wraps the
    whole per-task body, handler AND bookkeeping, and fails that task. The port narrowed the guard
    to `stage.run` alone, and this restores it.

    A CANCELLATION STILL PROPAGATES - `_run_stage`'s reason, and this loop keeps it - BUT IT NO
    LONGER COSTS THE BATCH. `_tick` bounds this job with `asyncio.wait_for`, which cancels, and
    the corpus's own arm for that (`mdc/runner.py:200-202`) was the one thing the port did not
    take. Without it the paragraph above was true of a raise and false of a budget: every task
    ordered after the one in flight stayed `leased`, never run, one attempt poorer, and four such
    ticks closed them for good with `reclaim_expired`'s sentence about a worker that was stopped.
    The arm below hands those back through `queue.release`, refunded, and re-raises; the task that
    actually ate the budget keeps its attempt, so `max_attempts` still bounds a stage that cannot
    finish inside one tick. [M5.1 review cycle 2, port-CANCEL-01]
    [M5.1 review cycle 1, M51-REV-01, M51-CRASH-02]
    """
    report = DrainReport()
    report.reclaimed = await queue.reclaim_expired(conn)
    await close_abandoned_boards(conn)
    await complete_landed_boards(conn)
    leased = await queue.lease(conn, [TASK_KIND], limit=limit)
    report.leased = len(leased)
    for position, task in enumerate(leased):
        try:
            outcome = await run_task(conn, task, gate=gate, run_id=run_id)
        except asyncio.CancelledError:
            # `_tick`'s budget, not a worker dying, and the difference is the rest of this batch.
            # `queue.lease` claims the whole batch in one statement and counts the attempt ON THE
            # CLAIM, so a cancellation here left every task after this one `leased`, never run and
            # one attempt poorer - invisible for `LEASE_SECONDS` and, four ticks later, closed for
            # good by `reclaim_expired` with a sentence saying a worker had been stopped. That is
            # the same collateral M51-REV-01 removed for a raise, through the exit that fix could
            # not cover: a `CancelledError` is not an `Exception`.
            #
            # THE TASK IN FLIGHT IS NOT RELEASED, deliberately. It ran, and it is the one that ate
            # the budget, so it keeps its charged attempt and its lease for `reclaim_expired` -
            # which is what stops a stage that can never finish inside one tick from being
            # refunded for ever, the failure `queue.py`'s change 6 exists to prevent. The tasks
            # behind it did nothing and are charged for nothing.
            #
            # The await is inside the handler on purpose and on this tree's own precedent:
            # `importer/bundle.py:1828` and `backup/movie_data.py:521` both take a cancellation,
            # do their cleanup and re-raise, for the same reason - `asyncio.wait_for` CANCELS, so
            # this is the only place the work can be handed back. Suppressed because a release
            # that itself fails must not replace the cancellation with a different exception; the
            # lease expiry is still the backstop. [M5.1 review cycle 2, port-CANCEL-01]
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
            # THE FOURTH EXIT WRITES BOTH TABLES TOO. `run_task`'s docstring says "the three
            # exits, and each writes BOTH tables"; this one is the fourth and wrote only the
            # queue, so a task closed here left §6.6's board permanently at `status = 'running'`
            # with `reason = NULL` - the operator-facing lie M51-CRASH-03 was filed to remove,
            # reachable through the exit that fix did not cover. `close_abandoned_boards` cannot
            # correct it either: its EXISTS clause matches `last_error = queue.ABANDONED` exactly,
            # and this handler writes a different sentence.
            #
            # The board's own stage is kept rather than guessed: the last `write_board(RUNNING)`
            # named the stage this walk was in, and that is the stage it stopped in. A missing row
            # means stage 1 never established a title, in which case there is nothing for §6.6 to
            # show and the reason lives on the task - `run_task`'s own rule. Board first, for
            # `_record_stop`'s reason: a crash between the two overstates how stuck the job is.
            #
            # READ BACK THROUGH THE TASK ROW and not off `task.payload`: the in-memory `Task` is
            # the row as it was LEASED, and stage 1 writes the title onto the payload afterwards
            # (`_remember_title`), so an item-keyed task that broke after minting carries no title
            # id here at all - which is the common case and the one this arm exists for.
            # [M5.1 review cycle 2, M51-CRASH-09]
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
    if report.leased:
        log.info("acquisition drain: %s", report.as_dict())
    return report


# A board row still reading `running` is the crash marker: `write_board(RUNNING)` is written
# immediately before the stage it names, and every ordinary exit overwrites it. So this statement
# closes exactly the rows whose worker died mid-stage and whose task the reaper has since given up
# on - and nothing else, because `status = 'running'` stops matching the moment it has run once.
#
# AND A PARK THAT NAMED A DATE IS THE SECOND CRASH MARKER, which this statement did not reach.
# `_record_stop` writes the board and then the queue, two autocommit statements, board first so
# that a crash between them "leaves a row that OVERSTATES how stuck the job is rather than one that
# understates it" - and for a park WITH a deadline it understated it permanently. The board read
# `parked at stage 9, import a bundle and this title is placed on the next drain, retry tomorrow`
# while `reclaim_expired` had closed the task for good; `close_abandoned_boards` matched `running`
# only and `complete_landed_boards` matched `ready` only, so neither reached it, `queue.enqueue`'s
# `ON CONFLICT DO NOTHING` refuses to revive the key, and decision 330 defers the revive lever to
# M5.6. The only sentence an operator could act on was the false one.
#
# `retry_after IS NOT NULL` IS WHAT MAKES IT A MARKER rather than a state. Every park this driver
# writes with a deadline is a park whose task it `defer`red, so a TERMINAL task beside one is a
# write that did not land; a park with no deadline is `queue.skip`'s, whose task is `skipped` and
# never matches below, or it is `placement/reconcile._park_thin`'s inbox row - `(stage 2, parked)`,
# no deadline, written for every thin-but-placed title since M4.13 - which is the state a real
# install is full of and which nothing here may touch.
#
# THE PARK IS KEPT IN THE SENTENCE and the date is cleared. "Import a bundle" is still why this
# walk stopped and the abandonment is what happened to it afterwards, so an operator reading §6.6
# needs both; a `retry_after` still naming a date is still promising the drain that will never
# come. [M5.1 review cycle 3, M51-C3-CRASH-02]
#
# The NOT EXISTS is the guard a title with two tasks needs: a `jellyfin:` task from the sweep and a
# `title:` task from §8.4's flywheel are two claims on one title (`enqueue_title`), and a board row
# whose other task is still pending or leased is a job that really is in flight.
_CLOSE_ABANDONED = """
UPDATE acquisition_job j
   SET status = $2,
       reason = CASE WHEN coalesce(j.reason, '') = '' THEN $3
                     ELSE $3 || ' It had parked with: ' || j.reason END,
       retry_after = NULL,
       updated_at = now()
 WHERE (j.status = $1 OR (j.status = $7 AND j.retry_after IS NOT NULL))
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

    THE REAPER CLOSES A TASK AND COULD NOT CLOSE ITS JOB. `queue.reclaim_expired` is the only
    writer for the one state decision 336's two words cannot describe - a worker that died, whose
    lease expired, whose attempts are spent - and it names one table, because decision 322 keeps
    `acquire/queue.py` free of any knowledge of titles. So the board kept whatever the last advance
    wrote, which for a worker killed mid-pipeline is `status = 'running'` with `reason = NULL`: an
    operator opening the Acquisition board after a power cut read a list of titles in flight, none
    of which was running and none of which would ever run again, with the sentence explaining why
    written to a column the list view does not select.

    That is the inverse of the trade `_record_stop` argues for. It writes the board first so a
    crash between the two OVERSTATES how stuck a job is; this understated it, permanently, and
    `placement/reconcile._park_thin`'s `DO NOTHING` means no sweep could ever correct it.

    The write lives here and not in `queue.py` because decision 322 puts the board in this module.
    `_reap_abandoned_import` (`worker.py:797-861`) is the idiom `queue.py`'s own change 6 cites as
    its model, and it exists precisely so "the Data tab would not poll a `running` phase for ever";
    the bundle import has one row, so closing the claim and closing the operator's view are one
    write. Decision 322 made them two tables, and only half the reaper was ported.
    [M5.1 review cycle 1, seam322-03, M51-CRASH-03]
    """
    closed = await conn.fetch(
        _CLOSE_ABANDONED, RUNNING, FAILED, queue.ABANDONED,
        queue.FAILED, queue.PENDING, queue.LEASED, PARKED,
    )
    if closed:
        log.warning(
            "acquisition board: %d job(s) whose worker never came back are closed as failed",
            len(closed),
        )
    return len(closed)


# The other half of the reaper's question, and the half the port left behind. `queue.py`'s change 6
# cites `_reap_abandoned_import` (`worker.py:797-861`) as its model, and that function's own
# docstring is headed "IT REPORTS WHAT IT READ AND NOTHING ELSE" precisely because it once said a
# thing it had not checked: it now calls `_committed_import` to see whether the work actually
# landed before it records a failure. `reclaim_expired` took the closing half and not the reading
# half - correctly, because decision 322 keeps `acquire/queue.py` free of any knowledge of titles -
# so nothing in the tree ever asked it.
#
# The window is real and narrow: `run_task` writes the board `ready` and then calls
# `queue.complete`, two autocommit statements, so a worker that dies between them - or one whose
# `_tick` budget expires between them - leaves a task the reaper will close `failed` with a
# sentence saying a worker abandoned it, beside a board row saying the title is ready. Both are on
# `GET /api/admin/acquisition/{title_id}`'s envelope today, in the same response, contradicting
# each other, with `queue.enqueue`'s `ON CONFLICT DO NOTHING` refusing to revive the task and
# decision 330 deferring the revive lever to M5.6.
#
# `result_note` AND NOT `last_error`: decision 336 puts a sentence about a completed walk in the
# note, and leaving the abandonment in `last_error` would keep a failure showing on a task the
# board says finished. [M5.1 review cycle 2, port-REAP-01]
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

    Asked here and not in `queue.py` for `close_abandoned_boards`'s reason: decision 322 makes the
    board this module's table and the schedule that one's, and the driver is the only thing that
    crosses between them. Run in `drain` immediately after the reaper, so the pair an operator
    reads is consistent by the time anything renders it.

    NARROW ON PURPOSE. Only a task the reaper itself closed (`last_error = queue.ABANDONED`) and
    only against a board reading `ready`, which `run_task` writes at exactly one place and only
    after all ten stages advanced. [M5.1 review cycle 2, port-REAP-01]

    THIS PARAGRAPH USED TO END "every other disagreement between the two tables is a state one of
    them is entitled to be in", and there was one it was wrong about: a board `parked` with a
    deadline beside a task the reaper had closed for good, which is the same two-statement window
    one exit earlier. `close_abandoned_boards` above takes that pair, for the reason stated with
    its statement. What is left is genuinely legitimate - a `skipped` task beside its park, a
    `pending` task beside the stage it is about to re-enter - but "what is left" is a claim that
    has now been wrong once, so a later reader should enumerate rather than trust it.
    [M5.1 review cycle 3, M51-C3-CRASH-02]
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
