"""The durable (kind,key) queue, asserted against Postgres. Spec v2.1 §8 preamble, §5.3.

`test_acquire_schema.py` asserts what `acquisition_task` REFUSES; this file asserts what
`acquire/queue.py` does with it. The division is the one that file's head states: a CHECK nobody
tries is a comment with punctuation, and a lease nobody races is a hope.

Four properties carry the coverage row `jellyfin-acquisition-eval-acquisition-work-is-durable`,
and each of them is a property of a CRASH rather than of a happy path, which is why they are
here rather than in a unit test of a pure function:

  * identity is `(kind, key)` and a second enqueue is a no-op, so the nightly sweep and §8.4's
    flywheel can both enqueue the same title without doubling the work;
  * a leased task whose worker is killed is recovered by LEASE EXPIRY and by nothing else --
    `mdc/queue.py:1-8`: "recovered by lease expiry, not by any shutdown handler - which is the
    only way that actually survives `kill -9`" -- and completes exactly once afterwards;
  * a deferred task does not lease before its time, which is the only mechanism §8 stage 4's
    30-day review-accrual window has (plan C3);
  * a task marked `paid` is never leased by a generic drain, because §8's paid stage (6) bills
    the household per title and a drain that ignored the flag would bill it on its first tick.

TIME IS MOVED BY WRITING THE ROW, not by waiting. A lease is 900 s and stage 4's window is 30
days; no test can wait for either, and `freezegun` cannot move `now()` inside Postgres, which is
the clock every statement in the module reads on purpose. So a test that needs the future writes
`next_attempt_at` or `lease_expires` directly and says which real event leaves that row state --
an expired lease is exactly the row a killed worker leaves behind.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg

from spielplan.acquire import queue


async def _row(db, key: str | None = None) -> asyncpg.Record:
    if key is None:
        return await db.fetchrow("SELECT * FROM acquisition_task")
    return await db.fetchrow("SELECT * FROM acquisition_task WHERE key = $1", key)


async def _due_now(db, key: str | None = None) -> None:
    """Bring a scheduled task's time forward. See the module docstring on moving time."""
    if key is None:
        await db.execute("UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second'")
    else:
        await db.execute(
            "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second' "
            " WHERE key = $1",
            key,
        )


# --- identity: enqueueing the same pair twice is a no-op -------------------------------


async def test_enqueueing_the_same_kind_and_key_twice_creates_one_task(db):
    """`mdc/queue.py:1-8`: "Task identity is (kind, key). Enqueueing the same pair twice is a
    no-op, so re-running `mdc plan` after adding titles only adds the new work."

    Two callers will be enqueueing the same title in this app: M4.11's nightly Jellyfin sweep
    over its unmatched report, and §8.4's flywheel. The no-op is what lets either run at any
    cadence without the other's work doubling.

    The second half is the half that matters on a re-enqueue of work already in flight: the
    payload is NOT overwritten. A task that has been leased and is being worked carries the
    payload its worker read; a second enqueue that clobbered it would change the work under a
    running stage, which is the one thing a durable queue may never do.
    """
    assert await queue.enqueue(db, "identify", "jf:a1", {"item": "a1"}) is True
    assert await queue.enqueue(db, "identify", "jf:a1", {"item": "clobbered"}) is False

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 1
    assert (await _row(db))["payload"] == {"item": "a1"}

    # The same key under a different kind is a different task: a title can be at `identify` and
    # at `enrich` in the same table without either collapsing into the other.
    assert await queue.enqueue(db, "enrich", "jf:a1") is True
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 2


async def test_enqueue_many_reports_only_the_rows_it_created(db):
    """The batch enqueue counts what it inserted, which is what the sweep logs.

    The corpus reads SQLite's `total_changes` around an `executemany`; this is one statement
    with `RETURNING`, so the count is the rows that survived the ON CONFLICT rather than a
    difference between two counters -- and an empty batch is a no-op that issues no statement,
    because `unnest` of two empty arrays is a legal INSERT that still costs a round trip.
    """
    assert await queue.enqueue_many(db, "identify", [("jf:1", None), ("jf:2", {"item": "2"})]) == 2
    assert await queue.enqueue_many(db, "identify", [("jf:2", {"item": "no"}), ("jf:3", None)]) == 1
    assert await queue.enqueue_many(db, "identify", []) == 0

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 3
    assert (await _row(db, "jf:2"))["payload"] == {"item": "2"}


# --- the lease: one claim, one winner ---------------------------------------------------


async def test_the_lease_takes_the_due_task_with_the_lowest_priority_number(db):
    """Priority orders the ready set; `next_attempt_at` decides what is in it.

    Both halves are asserted together because they are one query and the failure modes are
    opposite: an ORDER BY that ignored priority would drain the queue in insertion order and
    starve stage 1 behind a backlog of re-derives, while a WHERE that ignored `next_attempt_at`
    would lease a task deferred for thirty days on the next tick -- the bug that makes stage 4's
    window a comment.
    """
    await queue.enqueue(db, "identify", "ordinary", priority=100)
    await queue.enqueue(db, "identify", "urgent", priority=10)
    await queue.enqueue(db, "identify", "deferred", priority=1)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() + interval '1 hour' "
        " WHERE key = 'deferred'"
    )

    assert [task.key for task in await queue.lease(db)] == ["urgent"]
    assert [task.key for task in await queue.lease(db)] == ["ordinary"]
    # `deferred` has the lowest priority number in the table and is still not leased.
    assert await queue.lease(db) == []


async def test_a_batch_comes_back_in_the_order_the_sort_key_chose(db):
    """The property `drain` depends on, which the test above cannot reach at `limit=1`.

    `lease` is one `UPDATE ... WHERE id IN (SELECT ... ORDER BY priority, id ...) RETURNING`. The
    ORDER BY is inside the sub-SELECT and decides SELECTION; `UPDATE ... RETURNING` has no defined
    output order, so the right eight rows came back in whatever order the outer plan yielded -
    measured, 20 of 20 batches in id order, which with an urgent task enqueued last is the exact
    reverse of the sort key. `pipeline.drain` then walks the list positionally and, when `_tick`'s
    budget expires mid-batch, refunds `leased[position + 1:]` - so under budget pressure the drain
    ran the least urgent work and handed back the most urgent, silently, while the Sort
    `queue.lease` spends a paragraph justifying decided nothing the consumer could see.

    Nothing misbehaves today because every enqueue in the tree passes the default `priority=100`,
    but `priority` is a public keyword on four functions and `M5.3-plan.md:218` already ports
    `default_priority` from the corpus. The corpus's own `lease` returns the rows of its ordered
    SELECT (`mdc/queue.py:128-144`); collapsing the two statements into one for the SKIP LOCKED
    claim is what dropped the property, and the port made it load-bearing by leasing eight at a
    time. [M5.1 review cycle 4 second pass, M51-C4-CONC-02]
    """
    # Priority descending with the id ascending, so the two orders disagree on every pair and an
    # unordered RETURNING cannot pass by luck.
    for n in range(12):
        await queue.enqueue(db, "identify", f"jf:p{n}", {"item": n}, priority=40 - n)

    batch = await queue.lease(db, limit=8)
    assert len(batch) == 8
    assert [task.priority for task in batch] == sorted(task.priority for task in batch), (
        f"the batch is walked in this order and its tail is what a cancelled tick refunds: "
        f"{[(task.key, task.priority) for task in batch]}"
    )
    assert [task.key for task in batch] == [f"jf:p{n}" for n in range(11, 3, -1)], (
        "the eight most urgent tasks, most urgent first"
    )


async def test_a_killed_workers_lease_is_reclaimed_and_the_task_completes_exactly_once(db):
    """The `kill -9` proof: recovery is by lease expiry and by nothing else.

    `mdc/queue.py:1-8` states the constraint this asserts -- tasks in flight are "recovered by
    lease expiry, not by any shutdown handler - which is the only way that actually survives
    `kill -9`". A SIGKILL past the stop grace, an OOM and a power cut all leave the same row: a
    `leased` one whose owner will never write to it again. Nothing runs in that process
    afterwards, so every mechanism that needs the dying process to cooperate is unavailable, and
    this is the shape `worker.py`'s `_reap_abandoned_import` already works out at M4.14.

    THE EXPIRED LEASE IS NOT SELF-SERVING. The second worker's lease finds nothing before the
    reclaim: the lease query reads `state = 'pending'`, and an abandoned row is `leased` until
    the reaper moves it. That is deliberate -- it means exactly one function decides that a task
    is abandoned, and a drain cannot quietly step over a lease that has merely gone quiet.

    And exactly once at the end: one `done` row, the attempt counter reading both leases, and
    the note of the worker that actually finished.
    """
    await queue.enqueue(db, "identify", "jf:a1", {"item": "a1"})
    [task] = await queue.lease(db, owner="worker-a")
    assert task.attempts == 1

    # The row a killed worker leaves behind: leased, owned, and past its expiry.
    await db.execute("UPDATE acquisition_task SET lease_expires = now() - interval '1 second'")
    assert await queue.lease(db, owner="worker-b") == []

    assert await queue.reclaim_expired(db) == {"pending": 1, "failed": 0}

    [again] = await queue.lease(db, owner="worker-b")
    assert again.id == task.id
    assert again.attempts == 2
    await queue.complete(db, again.id, "identified")

    row = await _row(db)
    assert row["state"] == "done"
    assert row["attempts"] == 2
    assert row["result_note"] == "identified"
    assert row["lease_owner"] is None and row["lease_expires"] is None
    assert await db.fetchval("SELECT count(*) FROM acquisition_task WHERE state = 'done'") == 1


async def test_a_batch_claimed_and_never_started_is_handed_back_unspent(db):
    """The arm the port did not take, in the shape this queue's batch lease needs.

    `mdc/runner.py:200-202` is `except asyncio.CancelledError: defer(self.conn, task.id, 0,
    "cancelled"); raise`, and the corpus can spell it per task because it leases ONE task per
    iteration. `lease` here claims up to `DRAIN_LIMIT` in a single statement and counts the
    attempt ON THE CLAIM, so when `_tick`'s `asyncio.wait_for` cancels a drain mid-batch every
    task behind the one in flight is `leased`, never run, and one attempt poorer. Four such ticks
    and `reclaim_expired` closes all of them with a sentence saying a worker was stopped or
    killed - while the worker was alive and its own budget was what ended the attempt - after
    which `ON CONFLICT (kind, key) DO NOTHING` means no sweep can re-enqueue them and decision 330
    defers the revive lever to M5.6.

    THE REFUND IS NOT A SOFTENING OF CHANGE 6, which is the second half of what is asserted: a
    stale id cannot resurrect a task that has reached a terminal state, and the task that actually
    ate the budget is not in the list at all - it keeps its attempt, so `max_attempts` still closes
    a stage that can never finish inside one tick. That is what `state = LEASED` gives and it is
    all it gives; the sibling test below pins the part this one used to overclaim.
    [M5.1 review cycle 2, port-CANCEL-01; M5.1 review cycle 4 second pass, M51-C4-CONC-03]
    """
    for n in range(3):
        await queue.enqueue(db, "identify", f"jf:b{n}", {"item": n})
    batch = await queue.lease(db, limit=3, owner="worker-a")
    assert [task.attempts for task in batch] == [1, 1, 1]

    in_flight, *untouched = batch
    moved = await queue.release(
        db, [task.id for task in untouched], note="the drain's budget expired",
    )
    assert moved == 2

    for task in untouched:
        row = await _row(db, task.key)
        assert row["state"] == queue.PENDING
        assert row["attempts"] == 0, "a task that never started was charged for the claim"
        assert row["lease_owner"] is None and row["lease_expires"] is None
        assert row["result_note"] == "the drain's budget expired"
    held = await _row(db, in_flight.key)
    assert (held["state"], held["attempts"]) == (queue.LEASED, 1)
    assert await queue.pending_count(db) == 2, "the handed-back work is due now, not after a wait"

    # A task that has reached a TERMINAL state is not this drain's to hand back.
    await queue.complete(db, untouched[0].id, "done elsewhere")
    assert await queue.release(db, [untouched[0].id]) == 0
    assert (await _row(db, untouched[0].key))["state"] == queue.DONE
    assert await queue.release(db, []) == 0


async def test_release_is_fenced_on_the_state_and_not_on_the_owner(db):
    """What `state = LEASED` gives, and the stronger thing the paragraph above it claimed.

    `release`'s docstring said the predicate meant "this can only ever move a row this drain is
    holding". It does not: `state = 'leased'` says a row is held by SOMEONE and carries no
    ownership component at all, so the clause refuses a task that has since reached a terminal
    state - which is the true half, asserted in the sibling test above - and does nothing about a
    task another worker has since leased, which is precisely the state the sentence's own words
    claim to exclude. `complete`, `fail` and `skip` are unfenced too and say so honestly, arguing
    it from the module head's budget arithmetic: the drain's per-attempt budget is well inside
    `LEASE_SECONDS`, so the only writer of a leased row is the worker holding it, and the caller
    is the one that must not pass an id it does not hold.

    The defect was the guarantee rather than a live write - both in-tree call sites pass their own
    `leased[position + 1:]` - and the reach is M5.6's abandon action, which wants to hand back a
    batch of ids and would read this sentence as the reason it needs no owner fence. So this test
    pins BOTH: what the statement actually does, and that no paragraph over it promises more.
    [M5.1 review cycle 4 second pass, M51-C4-CONC-03]
    """
    import inspect

    await queue.enqueue(db, "identify", "jf:stranger", {"item": 1})
    [held] = await queue.lease(db, owner="worker-b")
    assert (await _row(db, held.key))["lease_owner"] == "worker-b"

    # A connection that has leased nothing at all, standing in for a caller with a stale batch.
    assert await queue.release(db, [held.id], note="a stranger") == 1
    row = await _row(db, held.key)
    assert (row["state"], row["attempts"], row["lease_owner"]) == (queue.PENDING, 0, None), (
        "release is unfenced on the owner, and the contract has to say the thing it does"
    )

    doc = inspect.getdoc(queue.release) or ""
    assert "can only ever move" not in doc and "a row this drain is holding" not in doc, (
        "release's docstring credits `state = LEASED` with an ownership guarantee the predicate "
        "does not carry; the assertion above is the behaviour it actually has"
    )
    assert "unfenced on" in doc, (
        "the correction is the argument and not the deletion: a reader reaching for release from "
        "M5.6's abandon action has to be told the family is lock-free on purpose"
    )


async def test_two_leasers_racing_for_one_task_produce_one_completion(db, pg_url):
    """Two loops is the ordinary state during a rolling restart, and both poll the same table.

    The argument is `_claim_bundle_import`'s, verbatim (`worker.py:713-745`): a read followed by
    a write lets both processes read the same row and start the same work, so THE CLAIM IS THE
    WRITE -- `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` -- and exactly one
    process can win it. Two real connections and `asyncio.gather` rather than one connection
    twice, because a single connection serialises statements and would assert nothing about
    locking at all.

    The loser gets an empty list rather than an error: SKIP LOCKED is what makes a contended
    queue drain instead of deadlock, and a drain that raised on contention would turn a rolling
    restart into a failed `job_run`.

    The second connection is also codec-less -- `db/pool.py` registers the json/jsonb codecs and
    a bare `asyncpg.connect` does not -- so the payload comes back as text on one side and as a
    dict on the other. Both are asserted to be a dict here, because a queue whose payload
    decoding depended on who opened the connection would hand one worker a string to iterate
    character by character.
    """
    await queue.enqueue(db, "identify", "jf:race", {"item": "race"})
    other = await asyncpg.connect(pg_url)
    try:
        first, second = await asyncio.gather(
            queue.lease(db, owner="worker-a"), queue.lease(other, owner="worker-b")
        )
    finally:
        await other.close()

    won = [task for batch in (first, second) for task in batch]
    assert len(won) == 1
    assert won[0].payload == {"item": "race"}

    await queue.complete(db, won[0].id, "identified")
    assert await db.fetchval("SELECT count(*) FROM acquisition_task WHERE state = 'done'") == 1
    assert await db.fetchval("SELECT attempts FROM acquisition_task") == 1


async def test_a_generic_drain_never_leases_a_paid_task(db):
    """`paid` is a refusal, not decoration. §8: "paid stages (6) never auto-retry past the spend
    cap."

    `mdc/sources/base.py:51-53` carries the same flag for the reason M5.1 has to respect before
    the milestone that spends money arrives: at corpus scale the difference between a free
    source and a paid one is roughly a hundred euros a click. The spend cap itself is M5.5's;
    what M5.1 owes is the seam -- a stage that refuses to run rather than running and billing --
    and the refusal lives in the lease's WHERE clause, where a drain that forgot to ask cannot
    bypass it.

    Equality rather than "include paid": a boolean meaning "also take the paid ones" hands one
    caller a mixed batch and moves the cap check to a per-task branch inside the drain, which is
    a branch that can be forgotten. The paid drain leases paid work and nothing else.
    """
    await queue.enqueue(db, "dna-extract", "t:1000000001", paid=True)
    await queue.enqueue(db, "identify", "jf:free")

    leased = await queue.lease(db, limit=5)
    assert [task.kind for task in leased] == ["identify"]
    assert leased[0].paid is False
    assert await queue.lease(db, limit=5) == []

    [billed] = await queue.lease(db, limit=5, paid=True)
    assert billed.kind == "dna-extract"
    assert billed.paid is True

    # AND THE COUNT ASKS THE SAME QUESTION AS THE LEASE. `pending_count` carried no `paid`
    # predicate, while its one production caller - `worker._acquisition_drain` - reads it to decide
    # whether there is work worth opening the active bundle for and then drains free work only. A
    # pending paid task therefore made the queue look non-empty to the caller asking "is there work
    # I will take", which on a broken install is a traceback every RETRY_AFTER for a batch the
    # drain leases nothing from. [M5.1 review cycle 2, M51-C2-PAID-02]
    await queue.enqueue(db, "identify", "jf:free-2")
    assert await queue.pending_count(db) == 1, "the free task the generic drain would take"
    assert await queue.pending_count(db, paid=True) == 0, "the paid one is leased, not pending"
    await queue.enqueue(db, "dna-extract", "t:1000000002", paid=True)
    assert await queue.pending_count(db) == 1, (
        "a paid task is not work a caller that leases free work would take"
    )
    assert await queue.pending_count(db, paid=True) == 1
    assert await queue.pending_count(db, ["dna-extract"]) == 0


# --- the schedule: defer, fail, reclaim -------------------------------------------------


async def test_a_deferred_task_is_not_leased_before_its_time(db):
    """§8 stage 4: "if thin, retry window 30 days (new releases accrue reviews over weeks)".

    `defer` is the ONLY mechanism for that window (plan C3). `acquisition_job.retry_after` is
    the board's copy of the fact -- what §6.6 shows an operator -- and not a schedule: nothing
    reads it, and a window that lived only there would be a date in a column no lease query
    consults, which is a task that never runs again.

    The deferral does not consume an attempt. `lease` increments `attempts` when it claims, and
    a task put back because the WORLD is not ready yet has not failed at anything; without the
    decrement, four thin releases in a row would exhaust `max_attempts` and fail a title for
    having been released recently.

    The reason goes to `result_note` and not to `last_error`, which is decision 336 in a column:
    "parked is waiting on something that may change; failed is a stage that raised and will
    raise again". A thirty-day wait written into a column named `last_error` reads as a failure
    on the board and invites a retry that can only re-learn the same date.
    """
    await queue.enqueue(db, "reviews-gate", "t:1000000001")
    [task] = await queue.lease(db)
    assert task.attempts == 1

    until = datetime.now(UTC) + timedelta(days=30)
    assert await queue.defer(db, "reviews-gate", "t:1000000001", until, reason="reviews thin") is True
    assert await queue.lease(db) == []

    row = await _row(db)
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert abs((row["next_attempt_at"] - until).total_seconds()) < 1
    assert row["result_note"] == "reviews thin"
    assert row["last_error"] is None
    assert row["lease_owner"] is None and row["lease_expires"] is None

    await _due_now(db)
    [resumed] = await queue.lease(db)
    assert resumed.id == task.id
    assert resumed.attempts == 1


async def test_a_failure_with_attempts_left_returns_the_task_after_a_capped_backoff(db):
    """The corpus's arithmetic, ported: `min(600, 15 * 3 ** attempts)`, or the caller's number.

    Computed in SQL against the row's own `attempts` rather than read-then-written in Python,
    for the reason the claim is one statement: two drains failing the same task would otherwise
    both read the same counter and both write the same schedule.

    The cap is what keeps a host that is down for a day from pushing a task a week into the
    future; `retry_in` is what lets a stage that KNOWS when to come back -- a `Retry-After`
    header, a rate-limit window -- say so instead of guessing.
    """
    await queue.enqueue(db, "enrich", "t:1000000001", max_attempts=99)
    [task] = await queue.lease(db)

    assert await queue.fail(db, task.id, "tmdb answered 500") == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert timedelta(seconds=30) < delay <= timedelta(seconds=45)

    # Ten attempts in, the uncapped curve asks for 885735 s -- ten days of nothing happening.
    await db.execute("UPDATE acquisition_task SET attempts = 10")
    assert await queue.fail(db, task.id, "tmdb answered 500") == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert timedelta(seconds=580) < delay <= timedelta(seconds=600)

    assert await queue.fail(db, task.id, "rate limited", retry_in=5) == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert delay <= timedelta(seconds=5)


async def test_the_last_attempt_fails_the_task_and_keeps_the_error(db):
    """`max_attempts` is what stops a broken stage from retrying for ever, and the error is what
    an operator reads on §6.6's board instead of the stack trace nobody kept.

    `fail` returns the state it wrote. The branch is decided in SQL against the row's own
    counter, so a caller that wanted to know whether this failure was the terminal one could not
    recompute it without re-reading the row -- and the driver needs exactly that to write the
    board row's `status` in the same tick (decision 336: `failed` is "a stage that raised and
    will raise again", and is the only state offering a plain retry).
    """
    await queue.enqueue(db, "identify", "jf:bad", max_attempts=2)

    [first] = await queue.lease(db)
    assert await queue.fail(db, first.id, "jellyfin answered 500") == "pending"
    await _due_now(db)

    [second] = await queue.lease(db)
    assert second.attempts == 2
    assert await queue.fail(db, second.id, "jellyfin answered 500 again") == "failed"

    row = await _row(db)
    assert row["state"] == "failed"
    assert row["last_error"] == "jellyfin answered 500 again"
    assert row["lease_owner"] is None and row["lease_expires"] is None
    assert await queue.lease(db) == []


async def test_a_task_that_kills_its_worker_is_closed_rather_than_reclaimed_for_ever(db):
    """The reaper's second arm, which the corpus does not have and this app already argues for.

    `reclaim_expired` returns an abandoned lease to the pool, and a task whose work kills the
    process is abandoned every time: the drain never reaches `fail`, so `max_attempts` -- the
    only thing that stops a retry loop -- is never consulted, and one poisoned title can keep a
    worker crashing for ever. `_reap_abandoned_import` (`worker.py:797-861`) does not requeue
    either; it closes the claim with a sentence saying what was observed. Same shape here: the
    attempt the lease consumed is kept precisely so this arm can fire.

    The sentence is what an operator gets, so it says what happened rather than what to do about
    it -- the discipline `_reap_abandoned_import` states in capitals: it reports what it read.
    """
    await queue.enqueue(db, "identify", "jf:poison", max_attempts=1)
    [task] = await queue.lease(db)
    assert task.attempts == 1

    await db.execute("UPDATE acquisition_task SET lease_expires = now() - interval '1 second'")
    assert await queue.reclaim_expired(db) == {"pending": 0, "failed": 1}

    row = await _row(db)
    assert row["state"] == "failed"
    assert "lease expired" in row["last_error"]
    assert await queue.lease(db) == []


async def test_a_skipped_task_keeps_its_note_and_releases_its_lease(db):
    """Decision 323: an item with no provider id "is parked at stage 1 with the reason 'no
    provider id' and is an admin's problem".

    `skipped` is the queue's word for the corpus's "legitimately not applicable" -- no API key,
    robots.txt disallowed, no slug yet, and here no provider id. Every one of those is a config
    change or an operator away from being applicable, which is why it is not `failed`: nothing
    is wrong with the task, and a retry today would learn the same thing.

    The lease is released either way. A terminal state that kept its owner would be a row the
    reaper reads for ever.
    """
    await queue.enqueue(db, "identify", "jf:noid")
    [task] = await queue.lease(db, owner="worker-a")
    await queue.skip(db, task.id, "no provider id")

    row = await _row(db)
    assert row["state"] == "skipped"
    assert row["result_note"] == "no provider id"
    assert row["lease_owner"] is None and row["lease_expires"] is None
    assert await queue.lease(db) == []


# --- the read side the board and the drain share ----------------------------------------


async def test_the_queue_can_be_counted_without_claiming_anything(db):
    """`stats` is published as a DOMAIN function for M5.7 to read from `/api/admin/system`.

    A domain function and not a route: the rules live in the domain packages and `api/` decides
    only HTTP shapes (CLAUDE.md), and M5.1 adds no route at all -- `/api/admin/system` asserts
    exactly three keys in `e2e/specs/18-system.spec.js` and widening it is M5.7's budgeted diff.
    Shaped as JSON-ready dicts for the same reason: the caller adds a key, not a serialiser.

    `pending_count` answers the drain's question rather than the board's: how much work could be
    leased RIGHT NOW. Due-ness is the whole difference -- a task deferred for thirty days is
    `pending` and is not work -- and a tick that confused the two would wake the drain every
    twenty seconds for a month.
    """
    await queue.enqueue_many(db, "identify", [("jf:1", None), ("jf:2", None)])
    await queue.enqueue(db, "enrich", "t:1000000001")
    [task] = await queue.lease(db, kinds=["enrich"])
    await queue.complete(db, task.id)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() + interval '1 day' WHERE key = 'jf:2'"
    )

    assert await queue.stats(db) == [
        {"kind": "enrich", "state": "done", "count": 1},
        {"kind": "identify", "state": "pending", "count": 2},
    ]
    assert await queue.pending_count(db) == 1
    assert await queue.pending_count(db, kinds=["enrich"]) == 0


# --- review cycle 3: the lever change 7 refuses is not one this module hands out ----------------


async def test_a_deferral_cannot_revive_work_this_queue_has_finished_with(db):
    """Change 4 re-keyed `defer` from the corpus's task id to `(kind, key)`, and a `(kind, key)` is
    a NAME anything can construct.

    The corpus's `defer(conn, task_id, ...)` was reachable from one place, `mdc/runner.py:200-202`,
    on a task that runner was holding; its documented precondition - "put a LEASED task back" - was
    enforced by nobody having the id. A name is different: every stage and every route in M5.2
    through M5.7 can spell one, and the UPDATE carried no state predicate at all, so
    `defer(kind, key, now())` moved a `done`, `failed` or `skipped` row back to `pending` with its
    attempt refunded and the next `lease` picked it up.

    That is the lever change 7 of this module's docstring says is deliberately NOT ported: "a lever
    that revives finished work before anything has decided what revival means is a lever with no
    owner". Decision 330 gives it one at M5.6, as a considered admin action. `release`, written one
    cycle later for this same table, already carries the predicate and states the hazard - "a
    caller passing a stale id cannot resurrect a task another worker has since completed" - so the
    two functions disagreed about the same table.

    PENDING IS IN THE PREDICATE AND NOT ONLY LEASED, because §8 stage 4's window is the thing
    `defer` exists for and a stage may move a task that is already waiting to a later date without
    leasing it first. [M5.1 review cycle 3, port-C3-02]
    """
    soon = datetime.now(UTC) + timedelta(seconds=30)

    for key, close in (
        ("t:done", lambda task: queue.complete(db, task.id, "walked")),
        ("t:skipped", lambda task: queue.skip(db, task.id, "waiting on an operator")),
        ("t:failed", lambda task: db.execute(
            "UPDATE acquisition_task SET state = $1, attempts = max_attempts WHERE id = $2",
            queue.FAILED, task.id,
        )),
    ):
        await queue.enqueue(db, "acquire", key)
        [task] = await queue.lease(db, ["acquire"])
        await close(task)
        settled = await _row(db, key)

        assert await queue.defer(db, "acquire", key, soon, reason="revived by name") is False, (
            f"a {settled['state']} task was put back on the queue by a caller that named it"
        )
        after = await _row(db, key)
        assert (after["state"], after["attempts"]) == (settled["state"], settled["attempts"])
        assert after["result_note"] == settled["result_note"]
        assert await queue.lease(db, ["acquire"]) == []

    # THE CONTROL, and it is both states a caller may legitimately hold: the leased task the two
    # in-tree callers pass, and a pending one a stage moves to a later date.
    await queue.enqueue(db, "acquire", "t:live")
    [live] = await queue.lease(db, ["acquire"])
    assert await queue.defer(db, "acquire", "t:live", soon, reason="thin") is True
    assert (await _row(db, "t:live"))["attempts"] == 0
    assert await queue.defer(db, "acquire", "t:live", soon + timedelta(days=30), reason="thin") is True
    assert (await _row(db, "t:live"))["state"] == "pending"
    assert live.key == "t:live"
