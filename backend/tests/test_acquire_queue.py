"""The durable (kind, key) queue against Postgres (§8, §5.3). Time is moved by writing the row:
`freezegun` cannot move `now()` inside Postgres. Needs TEST_DATABASE_URL."""

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
    if key is None:
        await db.execute("UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second'")
    else:
        await db.execute(
            "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second' "
            " WHERE key = $1",
            key,
        )


async def test_enqueueing_the_same_kind_and_key_twice_creates_one_task(db):
    """A re-enqueue must not overwrite the payload of a task a worker is already running."""
    assert await queue.enqueue(db, "identify", "jf:a1", {"item": "a1"}) is True
    assert await queue.enqueue(db, "identify", "jf:a1", {"item": "clobbered"}) is False

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 1
    assert (await _row(db))["payload"] == {"item": "a1"}

    # The same key under another kind is another task.
    assert await queue.enqueue(db, "enrich", "jf:a1") is True
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 2


async def test_enqueue_many_reports_only_the_rows_it_created(db):
    """An empty batch issues no statement: `unnest` of two empty arrays still costs a round trip."""
    assert await queue.enqueue_many(db, "identify", [("jf:1", None), ("jf:2", {"item": "2"})]) == 2
    assert await queue.enqueue_many(db, "identify", [("jf:2", {"item": "no"}), ("jf:3", None)]) == 1
    assert await queue.enqueue_many(db, "identify", []) == 0

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 3
    assert (await _row(db, "jf:2"))["payload"] == {"item": "2"}


async def test_the_lease_takes_the_due_task_with_the_lowest_priority_number(db):
    """Ignoring priority starves stage 1; ignoring `next_attempt_at` leases a 30-day deferral next tick."""
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
    """`UPDATE ... RETURNING` has no defined order; `drain` refunds `leased[position + 1:]` positionally."""
    # Priority descending, id ascending, so an unordered RETURNING cannot pass by luck.
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
    """Recovered by lease expiry only (it survives `kill
    -9`); only the reaper decides a task is abandoned."""
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
    """The lease counts the attempt on the claim, so a
    cancelled batch hands back the unstarted tasks unspent."""
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
    """`state = 'leased'` fences terminal states, not another worker's lease: no ownership component."""
    import inspect

    await queue.enqueue(db, "identify", "jf:stranger", {"item": 1})
    [held] = await queue.lease(db, owner="worker-b")
    assert (await _row(db, held.key))["lease_owner"] == "worker-b"

    # A connection that has leased nothing, standing in for a caller with a stale batch.
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
    """The claim is the write (`FOR UPDATE SKIP LOCKED`); the second connection is codec-less on purpose."""
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
    """Equality, not "include paid": the paid drain leases paid work and nothing else."""
    await queue.enqueue(db, "dna-extract", "t:1000000001", paid=True)
    await queue.enqueue(db, "identify", "jf:free")

    leased = await queue.lease(db, limit=5)
    assert [task.kind for task in leased] == ["identify"]
    assert leased[0].paid is False
    assert await queue.lease(db, limit=5) == []

    [billed] = await queue.lease(db, limit=5, paid=True)
    assert billed.kind == "dna-extract"
    assert billed.paid is True

    # `pending_count` asks the lease's question, so a paid task does not make free work look pending.
    await queue.enqueue(db, "identify", "jf:free-2")
    assert await queue.pending_count(db) == 1, "the free task the generic drain would take"
    assert await queue.pending_count(db, paid=True) == 0, "the paid one is leased, not pending"
    await queue.enqueue(db, "dna-extract", "t:1000000002", paid=True)
    assert await queue.pending_count(db) == 1, (
        "a paid task is not work a caller that leases free work would take"
    )
    assert await queue.pending_count(db, paid=True) == 1
    assert await queue.pending_count(db, ["dna-extract"]) == 0


async def test_a_deferred_task_is_not_leased_before_its_time(db):
    """A deferral refunds its attempt and writes `result_note`, not `last_error`: waiting is not failing."""
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
    """Computed in SQL against the row's own `attempts`, so two failing drains cannot double-count."""
    await queue.enqueue(db, "enrich", "t:1000000001", max_attempts=99)
    [task] = await queue.lease(db)

    assert await queue.fail(db, task.id, "tmdb answered 500") == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert timedelta(seconds=30) < delay <= timedelta(seconds=45)

    # Ten attempts in, the uncapped curve asks for 885735 s.
    await db.execute("UPDATE acquisition_task SET attempts = 10")
    assert await queue.fail(db, task.id, "tmdb answered 500") == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert timedelta(seconds=580) < delay <= timedelta(seconds=600)

    assert await queue.fail(db, task.id, "rate limited", retry_in=5) == "pending"
    delay = await db.fetchval("SELECT next_attempt_at - now() FROM acquisition_task")
    assert delay <= timedelta(seconds=5)


async def test_the_last_attempt_fails_the_task_and_keeps_the_error(db):
    """`fail` returns the state it wrote, which the driver needs for the board in the same tick."""
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
    """A task that kills its worker never reaches `fail`, so the reaper closes it after `max_attempts`."""
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
    """Decision 323: no provider id is skipped, not failed; a terminal row must not keep its lease."""
    await queue.enqueue(db, "identify", "jf:noid")
    [task] = await queue.lease(db, owner="worker-a")
    await queue.skip(db, task.id, "no provider id")

    row = await _row(db)
    assert row["state"] == "skipped"
    assert row["result_note"] == "no provider id"
    assert row["lease_owner"] is None and row["lease_expires"] is None
    assert await queue.lease(db) == []


async def test_the_queue_can_be_counted_without_claiming_anything(db):
    """`pending_count` counts what could be leased RIGHT NOW: a deferred task is pending and not work."""
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


async def test_a_deferral_cannot_revive_work_this_queue_has_finished_with(db):
    """`defer` takes a (kind, key) anyone can spell, so it must not revive finished work."""
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

    # The control: a leased task and a pending one a stage moves to a later date.
    await queue.enqueue(db, "acquire", "t:live")
    [live] = await queue.lease(db, ["acquire"])
    assert await queue.defer(db, "acquire", "t:live", soon, reason="thin") is True
    assert (await _row(db, "t:live"))["attempts"] == 0
    assert await queue.defer(db, "acquire", "t:live", soon + timedelta(days=30), reason="thin") is True
    assert (await _row(db, "t:live"))["state"] == "pending"
    assert live.key == "t:live"
