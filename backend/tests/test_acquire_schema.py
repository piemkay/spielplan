"""The acquisition spine's three tables, asserted by trying to violate them (§8). The absences (no
FK from task to title, no UNIQUE on content_sha256) are asserted in the direction that catches a repair.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncpg
import pytest


async def _title(db, title_id: int = 700, kind: str = "movie") -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, 'x') ON CONFLICT DO NOTHING",
        title_id, kind,
    )
    return title_id


async def _index_defs(db, table: str) -> str:
    rows = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = $1 AND schemaname = 'public'", table
    )
    return " ".join(row["indexdef"] for row in rows)


async def test_enqueueing_the_same_kind_and_key_twice_is_refused(db):
    """A uniqueness enforced by the writer holds only until
    two writers run; §8.4's flywheel is the second."""
    await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a1')")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a1')")
    await db.execute("INSERT INTO acquisition_task (kind, key) VALUES ('enrich', 'jf:a1')")
    assert await db.fetchval("SELECT count(*) FROM acquisition_task WHERE key = 'jf:a1'") == 2


async def test_the_state_check_refuses_a_state_the_drain_cannot_read(db):
    """The board's `status` and the queue's `state` overlap
    on `failed` only; a board word here is never leased."""
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO acquisition_task (kind, key, state) VALUES ('identify', 'jf:a2', $1)",
            "queued",
        )


async def test_a_new_task_is_free_and_immediately_claimable(db):
    """A NULL `next_attempt_at` is enqueued, indexed and never leased by anybody."""
    row = await db.fetchrow(
        "INSERT INTO acquisition_task (kind, key) VALUES ('identify', 'jf:a3') "
        "RETURNING paid, state, attempts, next_attempt_at"
    )
    assert row["paid"] is False, "a new task must be free until the stage that spends says so"
    assert row["state"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is not None, (
        "a NULL next_attempt_at is a task the lease query can never see; it is due now or it is "
        "deferred to a time, never to nothing"
    )


async def test_a_leased_task_cannot_be_written_without_a_lease_to_expire(db):
    """Recovery is by lease expiry (survives `kill -9`), so a lease with no expiry is never reclaimed."""
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO acquisition_task (kind, key, state) "
            "VALUES ('identify', 'jf:a4', 'leased')"
        )
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, state, lease_owner, lease_expires) "
        "VALUES ('identify', 'jf:a5', 'done', 'host:123', now())"
    )


async def test_the_queue_is_indexed_for_the_lease_and_for_the_reaper(db):
    """An index is the first thing a later migration drops when it looks redundant."""
    defs = await _index_defs(db, "acquisition_task")
    assert "(state, next_attempt_at, priority)" in defs, (
        "the lease query has no index for its filter: claimable rows, then the ones whose time "
        "has come"
    )
    assert "(lease_expires)" in defs and "state = 'leased'" in defs, (
        "the reaper has no index; reclaiming abandoned work would scan the whole queue"
    )


async def test_deleting_a_title_keeps_its_task_and_takes_its_board_row(db):
    """`acquisition_task` has no FK: stage 1 is what mints the title (decision 322)."""
    target = await _title(db, 700)
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 2, 'parked')", target
    )
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) "
        "VALUES ('identify', 'jf:item-700', $1)",
        '{"title_id": 700}',
    )

    await db.execute("DELETE FROM title WHERE id = $1", target)

    board = await db.fetchval("SELECT count(*) FROM acquisition_job WHERE title_id = $1", target)
    assert board == 0, "the board row cascades with its title, and that CASCADE is pinned"
    queued = await db.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE key = 'jf:item-700'"
    )
    assert queued == 1, (
        "the queue must not cascade from title: a task is disposable and a title row is not "
        "(decision 162, content seeds once), and the task is what mints the title in the first "
        "place"
    )


async def test_two_fetches_of_identical_bytes_share_a_file_and_keep_two_rows(db):
    """A UNIQUE on `content_sha256` would collapse the fetch history into the storage."""
    for _ in range(2):
        await db.execute(
            "INSERT INTO raw_document (source, kind, entity_key, url, http_status, "
            "content_sha256, content_path, byte_size) "
            "VALUES ('tmdb', 'movie_detail', 'tt0111161', 'https://example.test/x', 200, "
            "'deadbeef', 'tmdb/movie_detail/de/deadbeef.gz', 12)"
        )

    rows = await db.fetch(
        "SELECT content_path FROM raw_document WHERE content_sha256 = 'deadbeef'"
    )
    assert len(rows) == 2, "a UNIQUE on the hash would have made the second fetch unrecordable"
    assert len({row["content_path"] for row in rows}) == 1, (
        "both rows must point at the one file the content address names"
    )


async def test_the_raw_store_is_indexed_for_the_derive_and_for_the_store(db):
    defs = await _index_defs(db, "raw_document")
    assert "(entity_key, source, kind)" in defs, "the per-title re-parse has no index"
    assert "(content_sha256)" in defs, "the store cannot find the file it already holds"


async def test_the_robots_cache_holds_one_row_per_host(db):
    """The robots columns are nullable: a refusing host's breaker counters come before any robots body."""
    await db.execute(
        "INSERT INTO fetch_host_state (host, robots_txt, robots_status, robots_fetched_at) "
        "VALUES ('example.test', 'User-agent: *', 200, now())"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("INSERT INTO fetch_host_state (host) VALUES ('example.test')")

    await db.execute(
        "INSERT INTO fetch_host_state (host, consecutive_failures, paused_until) "
        "VALUES ('slow.test', 3, now() + interval '5 minutes')"
    )
    unasked = await db.fetchval(
        "SELECT robots_txt FROM fetch_host_state WHERE host = 'slow.test'"
    )
    assert unasked is None, "a host can be paused before anyone has asked it for robots.txt"
