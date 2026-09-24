"""The durable (kind,key) task queue. Spec v2.1 §8 preamble ("durable `(kind,key)` queue"), §5.3.

PORT VERDICT: `mdc/queue.py` (257 lines), **ported with named changes**. The API and the state
machine are verbatim -- `pending|leased|done|failed|skipped`, identity `(kind, key)`,
`LEASE_SECONDS = 900`, an attempt counter with a capped exponential backoff -- and every
statement is rewritten for asyncpg and Postgres. Its own head states the two constraints this
module exists to keep (`mdc/queue.py:1-8`):

    "The queue lives in SQLite so a crash, a Ctrl-C or a power cut costs at most the tasks that
    were in flight. Those are recovered by lease expiry, not by any shutdown handler - which is
    the only way that actually survives `kill -9`."

    "Task identity is ``(kind, key)``. Enqueueing the same pair twice is a no-op."

Both are properties of the table as much as of this module, and `0024_acquisition.sql` carries
them there: `UNIQUE (kind, key)`, and a CHECK that a `leased` row always has the owner and the
expiry the reaper reads. A uniqueness enforced by the writer holds only until two writers run,
and §8.4's flywheel is the second writer.

THE NAMED CHANGES, each with its reason:

 1. **One clock, and it is Postgres's.** The corpus stores `REAL` seconds from `time.time()`;
    every column here is `timestamptz` and every statement reads `now()`. Two processes whose
    clocks disagree by a minute is a lease that expires early on one of them, which is the one
    failure a lease cannot tolerate -- and the worker and the backend are separate containers.

 2. **The claim is the write.** `lease` is a single
    `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` rather than the corpus's SELECT
    followed by an UPDATE inside a transaction. The argument is already worked out in this tree
    at `worker.py:713-745`: "two loops is the ordinary state during a rolling restart", a read
    then a write lets both start the same work, and SKIP LOCKED is what makes a contended queue
    drain rather than deadlock.

 3. **`fail` computes its branch in SQL**, against the row's own `attempts`, for the same
    reason: two drains failing the same task must not both read one counter and both write one
    schedule. It returns the state it wrote, because that branch is no longer recomputable by
    the caller and the driver needs it to write §6.6's board row in the same tick.

 4. **`defer` is keyed on `(kind, key)` and takes an instant**, where the corpus takes a task id
    and a duration. §8 stage 4's window is a DATE -- "retry window 30 days" from a known event
    -- and a duration rounded through two clocks is not that date; identity rather than id
    because the caller that decides to wait is a stage, which holds the key it was given.

 5. **A deferral's reason goes to `result_note`, not to `last_error`.** Decision 336 draws
    exactly this line: "`parked` is waiting on something that may change; `failed` is a stage
    that raised and will raise again". A thirty-day wait written into a column named
    `last_error` reads as a failure on the board and invites a retry that can only re-learn the
    same date.

 6. **`reclaim_expired` closes a task that has spent every attempt** instead of returning it to
    the pool for ever, and drops the corpus's `lease_expires IS NULL` arm (the schema CHECK
    makes that row unwritable, and a dead arm is a comment with punctuation). The addition is
    this app's own idiom: `_reap_abandoned_import` (`worker.py:797-861`) does not requeue a
    claim nobody will finish, it closes it with a sentence saying what was observed. Without
    that arm a task whose work KILLS the worker is abandoned before it can reach `fail`, so
    `max_attempts` -- the only thing that stops a retry loop -- is never consulted, and one
    poisoned title crashes the worker for ever.

 7. **`retry_failed` and `enqueue(reset=True)` are deliberately NOT ported.** They are the
    corpus's admin re-run surface, and decision 336 is explicit that "M5.1 adds no admin
    action": proposal 109's three actions (retry stage / retry from stage N / abandon) are
    adopted or struck by number under decision 330, which is M5.6's to take. A lever that
    revives finished work before anything has decided what revival means is a lever with no
    owner. `summary()` is not ported either -- `stats` below answers the same question at the
    granularity §6.6's board asks it.

 8. **`paid` is a filter on the lease** rather than a fact a caller may consult. §8: "paid
    stages (6) never auto-retry past the spend cap". `mdc/sources/base.py:51-53` carries the
    flag because "at corpus scale the difference between them is roughly a hundred euros a
    click", and the seam M5.1 owes M5.5 is a stage that can refuse to run rather than running
    and billing. The refusal therefore lives in the WHERE clause, where a drain that forgot to
    ask cannot bypass it, and `paid=True` leases paid work and NOTHING else -- a flag meaning
    "also take the paid ones" would hand one caller a mixed batch and move the cap check into a
    per-task branch that can be forgotten.

LEASE_SECONDS AND THE DRAIN'S BUDGET ARE ONE NUMBER SEEN TWICE, and they must not overlap.
`LEASE_SECONDS` is how long a claimed task stays invisible to every other worker; the drain
job's `timeout` is the ceiling `worker.py`'s `_tick` puts on one attempt with `asyncio.wait_for`
(§5.3's rule, `worker.py:987-1006`: every budget is at or under its job's own interval because
the loop is sequential). The lease must be the LONGER of the two, comfortably: then a lease can
only expire after the attempt it belongs to has already been cancelled, and an expired lease
means the process is gone rather than slow. That is what makes `complete` safe to write without
fencing on the owner.

If they disagree -- a drain budget at or past `LEASE_SECONDS` -- a second worker reclaims a task
the first is still running, two processes work one title, and both write. This tree has already
paid for the softer version of that mistake once: at M4.14 the bundle import's reap window and
run window overlapped by exactly the enqueue-to-claim delay, and a second loop's first tick
closed a live import as abandoned (`worker.py:713-745`, cycle 1). Here the blast radius is a
duplicated derived row in the content spine, which decision 162 makes permanent. 900 s against a
drain budget measured in a few minutes is the headroom; a drain that needs longer than 900 s for
ONE task is a stage that should be splitting its work, not a lease that should be growing.

WHAT THE DRIVER DOES WITH THESE FIVE STATES (the seam, so M5.2-M5.7 spell it one way). §8: "a
failure at any stage parks the job with a reason, retryable from admin", and decision 336 splits
that into two: a stage that parks with a time -- the 30-day window, the spend cap, a bundle-less
install -- calls `defer` and the task stays `pending` until the world can have changed; a stage
that parks with no time, because nothing will change without an operator (no provider id,
decision 323), calls `skip`; a stage that RAISED calls `fail`, which retries it until
`max_attempts` and then stops. `acquisition_job` is the board those three write for an operator
to read; this table is the schedule.
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

# The corpus's number, and the argument for it is in the module docstring above: it is the
# ceiling on how long one task may be invisible after a worker dies, so it is bounded below by
# the longest attempt any stage may take and above by how long an operator will wait for a
# killed worker's work to move again.
LEASE_SECONDS = 900.0

PENDING = "pending"
LEASED = "leased"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

# `min(600, 15 * 3 ** attempts)`, the corpus's curve. The cap is what keeps a host that is down
# for a day from pushing a task a week into the future -- past the cap the retry schedule stops
# being a backoff and becomes a silent abandonment with no row saying so.
BACKOFF_BASE_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 600.0

# The corpus's truncations, kept: an error is a sentence on §6.6's board, not a stack trace, and
# a note is shorter than that.
ERROR_LIMIT = 1000
NOTE_LIMIT = 500

# What the reaper writes when it closes a task instead of returning it. It reports what it read
# and nothing else -- the discipline `_reap_abandoned_import` states in capitals for the same
# column -- because an operator acting on a sentence that guessed would be repairing the wrong
# thing. ASCII, like every other line this project can print to a Windows console.
ABANDONED = (
    "the lease expired with no outcome reported and every attempt is spent: a worker was "
    "stopped, killed or abandoned this task. Nothing will lease it again."
)


def worker_id() -> str:
    """Who holds a lease, in a form that survives a restart and distinguishes two workers.

    Host and pid, the corpus's spelling. The pid is the half that matters: two worker processes
    in one container share a hostname, and a lease owner that could not tell them apart would
    make `lease_owner` unreadable exactly when an operator is looking at a stuck task.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class Task:
    """One leased unit of work, as the drain sees it.

    Carries `paid` although the lease has already filtered on it: a drain holding a mixed batch
    is the thing this queue refuses, so the flag on the task is what lets a stage ASSERT the
    batch it was handed is the one it asked for, rather than trusting the call site.
    """

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
        # `db/pool.py` registers json.dumps/json.loads as the jsonb codecs, so a pooled
        # connection hands back a dict -- but a bare `asyncpg.connect` does not, and both open
        # this table (tests, and any operator script). Decoding here rather than at every call
        # site is the same fix that module's comment argues for: "ship a string where a list was
        # expected, and the UI iterates it character by character".
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

    The no-op is the point rather than a convenience: M4.11's nightly sweep and §8.4's flywheel
    will both enqueue the same title, at different cadences, and neither can know what the other
    has done. `ON CONFLICT DO NOTHING` also leaves the PAYLOAD alone, which matters more than
    the row count -- a second enqueue that clobbered the payload of a task already leased would
    change the work under a running stage.

    `$3::text::jsonb` and not `$3::jsonb`: the latter types the parameter as jsonb and
    `db/pool.py` registers `json.dumps` as the jsonb encoder, so the app's own connection would
    encode the payload a second time and store a JSON *string* where every reader expects an
    object -- the same cast, for the same reason, as `backup/movie_data.py:767-770`.
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
    """Add a batch of one kind. Returns how many rows were created, not how many were offered.

    One statement over `unnest`, where the corpus uses `executemany` and reads SQLite's
    `total_changes` around it: the count is then the rows that survived the ON CONFLICT rather
    than a difference between two counters read either side of a loop, and a sweep enqueueing
    nine thousand unmatched items costs one round trip instead of nine thousand.
    """
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

    THE CLAIM IS THE WRITE. `worker.py:713-745` argues this against a real failure and the
    argument transfers whole: two loops polling one table is the ordinary state during a rolling
    restart, a read followed by a write lets both read the same row and start the same work, and
    only a single statement that UPDATEs what it selected can be won by exactly one process.
    `FOR UPDATE SKIP LOCKED` is what makes the loser get an empty list instead of blocking
    behind the winner, so contention costs a tick rather than a job.

    Ready is two conditions, and they fail in opposite directions if either is dropped:
    `state = 'pending'` (an abandoned lease is `leased` until `reclaim_expired` moves it, so
    exactly one function decides a task is abandoned) and `next_attempt_at <= now()` (a task
    deferred for stage 4's thirty days is `pending` and is not work).

    `acquisition_task_ready (state, next_attempt_at, priority)` SERVES THE FILTER AND NOT THE
    SORT, and this paragraph used to claim otherwise - "the first two columns are the filter, the
    third the sort", plus a cross-reference saying `test_acquire_schema.py` pins the ORDER BY to
    the index. Both were false. A btree yields rows in index order only while the leading columns
    are pinned, and `next_attempt_at <= now()` is a RANGE and not an equality, so within
    `state = 'pending'` the scan is ordered by `next_attempt_at` and `priority` is no more sorted
    than `id`, which is in no index at all. `ORDER BY priority, id` therefore costs a Sort under
    every plan shape - as it does in the corpus this is ported from, whose `ix_task_ready` and
    `ORDER BY priority ASC, id ASC` are the same pair over 19,000 titles (`mdc/db.py:735`,
    `mdc/queue.py:132`). That is accepted rather than indexed away: `drain` leases ONCE per tick
    and the job ticks at `every=1800`, so the sort is milliseconds per half hour, and an index
    added to remove it would move `0024`'s checksum and cost every box that has applied it its
    database. What the schema test pins is that the index EXISTS, so a later migration cannot drop
    it as redundant; what pins this ORDER BY is
    `test_acquire_schema.py::test_the_lease_orders_by_a_key_no_index_can_serve`, which reads this
    statement and reddens when the sort key moves - because a change here is a change to the
    argument above and not merely to a line of SQL. [M5.1 review cycle 4, M51-C4-QUEUE-01]

    Attempts are counted on the CLAIM and not on the failure, which is what makes a task that
    kills its worker countable at all: nothing runs in that process afterwards to count it.
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
    # AND THE BATCH IS HANDED BACK IN THE ORDER THE SORT KEY CHOSE. The ORDER BY above sits inside
    # the sub-SELECT, where it decides SELECTION; `UPDATE ... RETURNING` has no defined output
    # order, so the right `limit` rows came back in whatever the outer plan yielded - measured, 20
    # of 20 batches in an order that was not the sort key's. `pipeline.drain` walks this list
    # positionally and refunds `leased[position + 1:]` when `_tick`'s budget expires mid-batch, so
    # the drain ran the least urgent work and handed back the most urgent, while the paragraph
    # above spends its length arguing why a Sort nothing downstream could see is worth paying for.
    # Sorted here rather than by wrapping the statement in a CTE: the claim is the one statement in
    # this module a race is decided by, and re-shaping it to fix an ordering buys a second Sort and
    # a diff over the lines that must not move. The corpus returns the rows of its ordered SELECT
    # (`mdc/queue.py:128-144`) and never noticed, because it leases one task at a time.
    # [M5.1 review cycle 4 second pass, M51-C4-CONC-02]
    return [Task.from_row(row) for row in sorted(claimed, key=lambda row: (row["priority"], row["id"]))]


async def complete(conn: asyncpg.Connection, task_id: int, note: str = "") -> None:
    """The work is done. Clears the lease and the error; the note is what §6.6's board shows.

    Not fenced on the lease owner, deliberately, and the module docstring carries the argument:
    the drain's per-attempt budget is well inside `LEASE_SECONDS`, so a worker still running is
    a worker whose lease has not expired, and the only writer of this row is the one holding it.
    Fencing would cost every caller the owner string and would buy nothing that the budget
    arithmetic does not already buy -- while inviting the belief that an over-long budget is
    survivable, which is the mistake M4.14 cycle 1 actually made.
    """
    await conn.execute(
        "UPDATE acquisition_task SET state = $2, lease_owner = NULL, lease_expires = NULL,"
        "       last_error = NULL, result_note = left($3::text, $4), updated_at = now()"
        " WHERE id = $1",
        task_id, DONE, note, NOTE_LIMIT,
    )


async def skip(conn: asyncpg.Connection, task_id: int, note: str = "") -> None:
    """The task is legitimately not applicable, and no retry can change that today.

    The corpus's cases are "no API key, robots.txt disallowed, no slug yet"; this app's first is
    decision 323's -- an item Jellyfin supplies no provider id for "parks here with the reason
    'no provider id' and mints nothing". Every one of them is a config change or an operator
    away from being applicable, which is why this is not `failed`: nothing raised, and a retry
    today would learn the same thing. Decision 330 decides whether an operator gets a button
    that revives these; until then they sit and are counted.

    `last_error` is left alone rather than cleared: a task skipped after a failure is still
    carrying the sentence that explains how it got here.
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

    None when there is no such task, which is a caller bug rather than a state worth handling:
    nothing deletes rows from this table, so the only way to reach it is to fail an id that was
    never leased.

    The branch and the backoff are computed in SQL against the row's own `attempts` -- the same
    argument as the claim, one statement rather than a read and a write -- so the caller cannot
    know which arm ran without being told, and the driver needs to know: decision 336 makes
    `failed` "a stage that raised and will raise again" and the only state offering a plain
    retry, so the board row's `status` is written from this answer in the same tick.

    `retry_in` is for a stage that KNOWS when to come back: a `Retry-After` header, a rate-limit
    window. `permanent` is for a stage that knows it never should -- a malformed key, a refusal
    that is not a transient. Everything else takes the corpus's curve.
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

    THE ONLY MECHANISM FOR §8 STAGE 4's WINDOW: "if thin, retry window 30 days (new releases
    accrue reviews over weeks)". `acquisition_job.retry_after` is the board's copy of that fact
    -- what §6.6 shows an operator -- and not a schedule; nothing reads it, and a window living
    only there is a date in a column no lease query consults, which is a task that never runs
    again (plan C3).

    The attempt the lease consumed is given back. A task put down because the WORLD is not ready
    yet has not failed at anything, and without the decrement four thin releases in a row would
    exhaust `max_attempts` and fail a title for having been released recently.

    `until` is an instant rather than a duration because the window is a date the caller can
    name from the event it is waiting on, and `result_note` rather than `last_error` because
    decision 336 says waiting and failing are different things.

    AND IT MOVES A LIVE TASK ONLY, which change 4 took away without meaning to. The corpus's
    `defer(conn, task_id, ...)` documented the precondition - "put a LEASED task back" - and was
    enforced by nobody having the id: it is reachable from one place, on a task that runner is
    holding. A `(kind, key)` is a NAME anything can construct, and with no predicate this UPDATE
    moved a `done`, `failed` or `skipped` row back to `pending` with its attempt refunded, where
    the next `lease` picked it up. That is precisely the lever change 7 of this module's docstring
    refuses to port - "a lever that revives finished work before anything has decided what revival
    means is a lever with no owner" - arriving through the function whose docstring did not mention
    it could. Decision 330 gives it an owner at M5.6 as a considered admin action. `release` below
    carries the same clause for the same reason, one function apart.

    PENDING AS WELL AS LEASED, because §8 stage 4's window is what this exists for and a stage may
    move a task that is already waiting to a later date without leasing it first. Both in-tree
    callers hold a leased task, so this predicate changes nothing today; what it changes is what
    M5.2 through M5.7 can do by reaching for the one function in this package that puts work back.
    [M5.1 review cycle 3, port-C3-02]
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

    THE ONE THING `mdc/runner.py` HAS THAT THIS PORT DID NOT, and the port's batch lease is why it
    needs it in a different shape. The corpus leases ONE task per iteration and its `_execute`
    carries `except asyncio.CancelledError: defer(self.conn, task.id, 0, "cancelled"); raise`
    (`mdc/runner.py:200-202`); `pipeline.drain` claims up to `DRAIN_LIMIT` in one statement, and
    `lease` counts the attempt ON THE CLAIM, so a `_tick` budget expiring mid-batch left every
    task after the one in flight `leased`, never run, and one attempt poorer. Four such ticks and
    `reclaim_expired` closes all of them for good with a sentence saying a worker was killed -
    while the worker was alive and its own budget was what ended the attempt.

    THE ATTEMPT IS REFUNDED because these tasks did not get one: nothing ran, so there is nothing
    for `max_attempts` to be counting. That is `defer`'s argument at a different granularity and
    not a softening of change 6 - the task that ACTUALLY ate the budget keeps its charged attempt
    and its lease, so a title whose stage can never finish inside one tick still reaches
    `max_attempts` and is still closed. A refund for the batch and not for the offender is the
    whole distinction, and it is the one `reclaim_expired` cannot draw because by then the
    difference is no longer in the database.

    `state = LEASED` in the WHERE clause, so a caller passing a stale id cannot resurrect a task
    that has since reached a terminal state. THAT IS ALL THE PREDICATE GIVES, and this paragraph
    used to read it as an ownership guarantee as well - that only a row this drain still held could
    move. It cannot be: `state = 'leased'` says a row is held by SOMEONE and carries no ownership
    component, so it does nothing about a task another worker has since leased, which is the state
    a reclaimed and re-leased row is in. This function is unfenced on the owner exactly as
    `complete`, `fail` and `skip` are, for the argument `complete` states from the module head's
    budget arithmetic, and the caller owes it ids it holds - both call sites pass their own
    `leased[position + 1:]`. A later reader reaching for this to hand back a batch it did
    NOT lease - decision 330's abandon action at M5.6 is the obvious one - is taking the lock-free
    position deliberately rather than on a guarantee that was never here.
    [M5.1 review cycle 2, port-CANCEL-01; M5.1 review cycle 4 second pass, M51-C4-CONC-03]
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

    RECOVERY IS BY LEASE EXPIRY AND BY NOTHING ELSE, which `mdc/queue.py:1-8` calls "the only
    way that actually survives `kill -9`" -- and this tree has the same finding one layer up:
    `_tick` bounds every job with `asyncio.wait_for`, which CANCELS, and a `CancelledError` is
    not an `Exception`, so the handler that would write a terminal state never runs
    (`worker.py:797-861`). A SIGKILL past the stop grace, an OOM and a power cut leave one row:
    `leased`, owned, past its expiry, and nothing in the dead process will ever touch it again.

    TWO ARMS, and the second is this app's rather than the corpus's. A task with attempts left
    goes back to the pool. A task that has spent them is CLOSED with a sentence, the way
    `_reap_abandoned_import` closes a claim nobody will finish, because a task whose work kills
    the worker is abandoned before it can ever reach `fail` -- so `max_attempts`, the only thing
    that stops a retry loop, would never be consulted and one poisoned title would crash the
    worker for ever.

    The corpus's `lease_expires IS NULL` arm is dropped: `0024`'s
    `acquisition_task_lease_is_reclaimable` makes a leased row without an expiry unwritable, and
    carrying a branch the schema forbids is a comment with punctuation.
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
    """Queue depth per kind and state, as rows a caller can serialise unchanged.

    A DOMAIN function and not a route: the rules live in the domain packages and `api/` decides
    only HTTP shapes (CLAUDE.md), and M5.1 adds no route at all -- `/api/admin/system` asserts
    exactly three keys in `e2e/specs/18-system.spec.js`, and widening it is M5.7's budgeted
    diff. Per kind AND state rather than per state alone because that is the grouping §6.6's
    board reads: "nine identifies waiting, one extract failed" is an operator's sentence, while
    a single pending count is a number nobody can act on.
    """
    rows = await conn.fetch(
        "SELECT kind, state, count(*)::bigint AS n FROM acquisition_task "
        " GROUP BY kind, state ORDER BY kind, state"
    )
    return [{"kind": r["kind"], "state": r["state"], "count": int(r["n"])} for r in rows]


async def pending_count(
    conn: asyncpg.Connection, kinds: Sequence[str] | None = None, *, paid: bool = False
) -> int:
    """How much work could be leased right now -- the drain's question, not the board's.

    Due-ness is the whole difference between this and a count of `pending` rows: a task deferred
    for stage 4's thirty days is `pending` and is not work, and a tick that confused the two
    would wake the drain every twenty seconds for a month.

    `paid` MIRRORS `lease`'s AND CARRIES ITS DEFAULT, which it did not. The paragraph here used to
    say the flag "is not filtered on purpose -- the question is how deep the queue is", and that
    is `stats` below's question rather than this function's: the only caller is
    `worker._acquisition_drain`, which reads this number to decide whether to ask for the active
    bundle at all, and then drains with `lease`'s `paid = false`. A pending PAID task therefore
    made the queue look non-empty to the one caller asking "is there work I will take", so on an
    install whose `artifact_bundle` row is active while its directory is gone (M4.13 data-03)
    `_active_store` raised every `RETRY_AFTER` for ever, for a batch the drain would have leased
    nothing from - which is exactly the log the "ONLY WHEN THERE IS WORK" paragraph buys
    protection from. Decision 348 makes the column and the lease filter this milestone's, so the
    mismatch is inside M5.1's own decided design. [M5.1 review cycle 2, M51-C2-PAID-02]
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

    §6.6's projected monthly spend is the per-title estimate times this count over the trailing
    thirty days (decision 451): the install's own recent rate, never an invented constant, and "no
    history" when nothing was ever filed rather than a zero that reads like a forecast. `created_at`
    is the filing and not the walk, because the question is how many titles arrive in a month.

    `except_priority` leaves out the tasks filed at one priority, and both answers leave them out.
    The caller passes `intake.RE_OFFER_PRIORITY`: a re-offer is a title the household already owns,
    which stage 1 closes without walking (decision 411), and an upgraded install's first delta poll
    files one for every title added since the install -- a projection counting those bills a month
    for a library that is already paid for. The kind and the priority are the caller's to name
    because the constants live in `acquire/pipeline.py` and `acquire/intake.py`, which import
    `llm/spend` -- and the spend guard that asks this question may import neither back.
    """
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE created_at >= $2)::bigint AS recent,"
        "       count(*) > 0 AS ever"
        "  FROM acquisition_task WHERE kind = $1 AND priority <> $3",
        kind, since, except_priority,
    )
    return int(row["recent"]), bool(row["ever"])
