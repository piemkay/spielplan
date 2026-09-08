"""What the worker leaves behind, and what it refuses to become.

Spec v2.1 §5.3, §6.6 (System), §2 (Backups), §1, §6.7; decisions 181 and 182;
docs/milestones/M4.7-plan.md §2 findings 10, 12, 13, 15 and 17.

`test_worker_schedule.py` owns `due()` — when a job may fire. This file owns the rest of the
loop: that a job that fires says so in a table an operator can read, that the table does not
grow without bound and neither of its readers pays for its history, that a job that fails is
tried again this evening rather than tomorrow night, that a stuck loop stops looking healthy,
and that the process this state lives in stays one process.

None of those had any evidence at all. `last_run` was an in-process dict; `BackupReport`,
`SyncReport`, `WatchReport` and the refit reports were each logged once and dropped, so a
restart could not know whether last night's dump happened and the nightly backup could fail for
a month with the only signal one ERROR a day in a log with no timestamp. A successful tick
emitted zero records even at DEBUG. `docker compose ps` said "Up" for a worker whose loop was
stuck, because the inherited HTTP healthcheck was disabled and replaced with nothing. And five
pieces of process-global state were correct only because the shipped CMD happens to start one
uvicorn worker, which nothing anywhere enforced.

The integration half drives `_tick` against a real pool, because the row the System card reads
is written by the loop and not by a helper. It skips without TEST_DATABASE_URL (tests/conftest.py).
The rest needs no database and must not skip.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spielplan import worker
from spielplan.api import admin as admin_api
from spielplan.app import create_app
from spielplan.core.config import settings
from spielplan.db import pool
from spielplan.placement import tower

# The compose readers, borrowed rather than re-written: `_mounts` resolves the `*worker-volumes`
# alias and strips comments, which is what tells the worker's own cache mount from the backend's
# copy of the same line. `tests/test_backup.py` is imported the same way by two other files.
from tests.test_static_contracts import _mounts, _nested, _service

REPO = Path(__file__).resolve().parents[2]

# A household afternoon: past every anchor, so an anchored fixture job is due, and unambiguous
# about the date. Fixed offset rather than a named zone — a Windows checkout has no system tz
# database, which is what `worker._now_local`'s fallback exists for.
NOON = datetime(2026, 9, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))

# An attempt that outlasts its own retry window, in a unit a test can wait for. It stands in for
# `nightly.DUMP_TIMEOUT_SECONDS` (1800 s) against `RETRY_AFTER` (300 s) — the pair this milestone
# shipped, and the one that makes a slow failure re-arm instantly. [M4.7 cycle 2 finding 4]
SLOW_FAILURE_SECONDS = 0.25


def _job(name: str, run, *, every: int = 60, anchor_hour: int | None = None) -> worker.Job:
    return worker.Job(name, "M0", "test", "ms", run, every=every, anchor_hour=anchor_hour)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR`, because `_tick` writes the heartbeat under it on every pass.

    Without this the loop would touch the container's `/data/cache` from a test run — which on
    this developer's machine is the root of a drive.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings.cache_clear()
    yield tmp_path
    settings.cache_clear()


@pytest.fixture
async def worker_env(db, pg_url, data_dir, monkeypatch):
    """The worker's own view: a real pool against the test database, and a real data dir.

    `_tick` acquires from the pool exactly as the loop does, so the production call path is what
    is under test rather than a connection handed in.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    await pool.open_pool(pg_url)
    try:
        yield data_dir
    finally:
        await pool.close_pool()
        settings.cache_clear()


# --- ops-11: a job outcome is a row, not a log line -------------------------------------------


async def test_every_job_the_worker_fires_writes_a_job_run_row(worker_env, db, monkeypatch):
    """§6.6 names "job health … backup status" as the System card's data, and there was none.

    Both outcomes in one tick, because the interesting install is the one where some jobs work:
    a table that only records successes cannot answer the question an operator brings to it.
    """
    async def ok() -> dict[str, object]:
        return {"kept": 14, "bytes": 4096}

    async def boom() -> dict[str, object]:
        raise RuntimeError("pg_dump exited 1: could not connect")

    monkeypatch.setattr(worker, "JOBS", (_job("t-ok", ok), _job("t-fail", boom)))
    await worker._tick(0.0, NOON, {}, {})

    rows = {
        r["name"]: r
        for r in await db.fetch(
            "SELECT name, started_at, finished_at, ok, detail FROM job_run ORDER BY name"
        )
    }
    assert set(rows) == {"t-ok", "t-fail"}, "a job that fired left no row"
    assert rows["t-ok"]["ok"] is True
    assert rows["t-ok"]["detail"] == {"kept": 14, "bytes": 4096}, (
        "the table stores what the job's own report produced"
    )
    for row in rows.values():
        assert row["started_at"] is not None and row["finished_at"] is not None
        assert row["finished_at"] >= row["started_at"]


async def test_a_failing_job_leaves_ok_false_and_the_error_text(worker_env, db, monkeypatch):
    """The nightly backup can fail for a month. The row is what says so after the log has
    rotated away — so it has to carry the reason, not only the verdict."""
    async def boom() -> dict[str, object]:
        raise RuntimeError("pg_dump exited 1: could not translate host name")

    monkeypatch.setattr(worker, "JOBS", (_job("t-fail", boom),))
    await worker._tick(0.0, NOON, {}, {})

    row = await db.fetchrow("SELECT ok, detail FROM job_run WHERE name = 't-fail'")
    assert row["ok"] is False
    assert "could not translate host name" in row["detail"]["error"]
    assert "RuntimeError" in row["detail"]["error"], "the type is half the diagnosis"


async def test_the_row_is_open_while_the_job_runs(worker_env, db, monkeypatch):
    """Written before the call, not after it.

    A job killed mid-flight — a SIGKILL past the grace period, an OOM, a power cut — then leaves
    a row saying it started and never finished, which is a different and far more useful fact
    than no row at all. The job reads its own row to prove the ordering.
    """
    observed: dict[str, object] = {}

    async def look_at_myself() -> dict[str, object]:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT ok, finished_at FROM job_run WHERE name = 't-open'"
            )
        observed["row"] = row
        return {}

    monkeypatch.setattr(worker, "JOBS", (_job("t-open", look_at_myself),))
    await worker._tick(0.0, NOON, {}, {})

    assert observed["row"] is not None, "the row was written after the job, not before it"
    assert observed["row"]["ok"] is None and observed["row"]["finished_at"] is None
    assert await db.fetchval("SELECT ok FROM job_run WHERE name = 't-open'") is True


async def test_a_real_job_from_the_registry_records_itself(worker_env, db):
    """The fixture jobs above prove `_tick`. This proves the registry's own callables reach it,
    which is the wiring that silently would not exist if `run` returned something `_tick` could
    not store."""
    prune = next(j for j in worker.JOBS if j.name == "session-prune")
    # Every other job held off explicitly: the interval ones by a fresh stamp, the anchored ones
    # by today's date. Only the one under test has no memory, which is what makes it due.
    await worker._tick(
        0.0,
        NOON,
        {j.name: 0.0 for j in worker.JOBS if j is not prune},
        {j.name: NOON.date() for j in worker.JOBS if j is not prune},
    )

    rows = await db.fetch("SELECT name, ok FROM job_run")
    assert [(r["name"], r["ok"]) for r in rows] == [("session-prune", True)]


async def test_the_schedule_seed_reads_the_newest_successful_run_per_job(worker_env, db):
    """`main` seeds the loop from this, and without the seed every restart spends a night.

    "Newest successful" is the whole specification: a failed run must not count as the night's
    dump, and an older success must not shadow a newer one.
    """
    # `session-prune` rather than a made-up name for the job with no success: the seed asks only
    # about names the registry still has, so a fictional one would pass this for the wrong reason.
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok) VALUES"
        " ('nightly-backup', now() - interval '30 hours', now() - interval '30 hours', true),"
        " ('nightly-backup', now() - interval '6 hours',  now() - interval '6 hours',  true),"
        " ('nightly-backup', now() - interval '1 hour',   now() - interval '1 hour',   false),"
        " ('session-prune',  now() - interval '2 hours',  now() - interval '2 hours',  false)"
    )
    async with pool.acquire() as conn:
        last_run, last_date = await worker._seed_schedule(conn, loop_now=0.0, local=NOON)

    assert set(last_run) == {"nightly-backup"}, "a job with no successful run has no memory"
    # The six-hour-old success, not the thirty-hour-old one and not the failure.
    assert -6.5 * 3600 < last_run["nightly-backup"] < -5.5 * 3600
    newest = await db.fetchval(
        "SELECT started_at FROM job_run WHERE name = 'nightly-backup' AND ok "
        " ORDER BY started_at DESC LIMIT 1"
    )
    # Read back from the row rather than recomputed from the test's own clock: the date is the
    # household's, and "six hours ago" straddles midnight for two hours out of every twenty-four.
    assert last_date["nightly-backup"] == newest.astimezone(NOON.tzinfo).date()


# --- ops-11: the table is bounded, and so is the cost of reading it ----------------------------


async def _job_run_tuples_read(conn) -> int:
    """How many `job_run` rows this database has handed to a scan, cumulatively.

    `pg_stat_force_next_flush` pushes the backend's pending counts into the shared statistics and
    `pg_stat_clear_snapshot` drops the transaction's cached view of them — without both, two
    readings either side of a call are the same number twice rather than a measurement. Every
    reader below runs on this same connection, so what is flushed is exactly what they did.
    """
    await conn.execute("SELECT pg_stat_force_next_flush()")
    await conn.execute("SELECT pg_stat_clear_snapshot()")
    return await conn.fetchval(
        "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) "
        "  FROM pg_stat_all_tables WHERE relname = 'job_run'"
    )


async def test_the_job_run_table_has_a_prune_and_it_keeps_a_fortnight(worker_env, db):
    """Nothing ever deleted from `job_run`.

    `_tick` writes a row for every job it fires and three of them fire every sixty seconds, so
    the table this milestone introduced grew by about 4,300 rows a day for ever — the one table
    in the schema with no retention, beside three prunes that keep sessions, challenges and dead
    push targets bounded. The fortnight is §2's own: these rows are what account for the fourteen
    dumps on disk, so a shorter window would leave dumps nothing can explain.
    """
    prune = next((j for j in worker.JOBS if j.name == "job-run-prune"), None)
    assert prune is not None and prune.run is not None, "nothing deletes from job_run"

    keep = worker.JOB_RUN_KEEP_DAYS
    # The two rows outside the window are failures on purpose: a job's newest *successful* row is
    # exempt from the prune at any age (the test below is what that is for), so a success is the
    # one thing the window cannot be measured with.
    await db.executemany(
        "INSERT INTO job_run (name, started_at, finished_at, ok) "
        "VALUES ($1, now() - ($2::int * interval '1 day'), now(), $3)",
        [("t-tonight", 0, True), ("t-yesterday", 1, True), ("t-inside", keep - 1, True),
         ("t-past-the-window", keep + 1, False), ("t-ancient", keep * 4, False)],
    )

    await prune.run()

    left = {row["name"] for row in await db.fetch("SELECT name FROM job_run")}
    assert left == {"t-tonight", "t-yesterday", "t-inside"}, (
        "the prune took a row inside the fortnight, or left one outside it"
    )


async def test_the_prune_never_takes_a_jobs_last_successful_row(worker_env, db):
    """The retention that replaced unbounded growth with a wrong answer.

    §6.6's backup fact is the newest `ok` `nightly-backup` row, and this prune deleted by age
    alone. So on the one install the card exists for — the household whose dumps have been
    failing since Tuesday — the fifteenth night deleted the last successful row, and the card
    stopped saying "last backup: the 24th, 15 days ago, and it is stale" and started saying that
    no dump has ever completed on this install. That is worse than the growth it replaced,
    because it is wrong rather than slow: the operator is told backups were never set up, on a
    box holding fourteen restorable dumps, and `backup/nightly.prune` only rotates after a
    *successful* dump so those files are all still there to contradict it.

    Exempting a job's newest success rather than special-casing `nightly-backup`, because
    `_seed_schedule` reads the same row for the same reason: it is the memory that stops a
    restart spending a retention slot. One kept row per name is what it costs.
    [M4.7 cycle 2 finding 7]
    """
    keep = worker.JOB_RUN_KEEP_DAYS
    await db.executemany(
        "INSERT INTO job_run (name, started_at, finished_at, ok) "
        "VALUES ($1, now() - ($2::int * interval '1 day'), now(), $3)",
        [("t-night", keep + 20, True),    # an older success: superseded, and prunable
         ("t-night", keep + 1, True),     # the last dump that worked, outside the window
         ("t-night", keep + 3, False),    # a failed night, outside the window
         ("t-night", 1, False)],          # last night's failure, inside it
    )

    await worker._prune_job_runs()

    rows = await db.fetch(
        "SELECT ok, round(extract(epoch from now() - started_at) / 86400) AS days "
        "  FROM job_run ORDER BY started_at DESC"
    )
    assert [(int(r["days"]), r["ok"]) for r in rows] == [(1, False), (keep + 1, True)], (
        "the prune deleted the last successful run, so the card reports a household that has "
        "never backed up"
    )


def test_the_retention_outlasts_the_interval_of_every_job_it_keeps():
    """The binding constraint on `JOB_RUN_KEEP_DAYS`, which is not the fortnight.

    `_seed_schedule` reads the newest *successful* run per job, and that row is the whole memory
    that stops `docker compose up` — README's install and upgrade gesture — spending one of §2's
    fourteen retention slots. A retention shorter than a job's own interval would delete that row
    between two runs and hand every restart a blank schedule: ops-03 reintroduced by the prune
    that fixes ops-11.
    """
    longest = max(job.every for job in worker.JOBS if job.run is not None)
    assert longest < worker.JOB_RUN_KEEP_DAYS * 86400


async def test_neither_reader_of_job_run_pays_for_the_rows_it_is_not_reading(db):
    """Both readers asked for "the newest row per job" as `DISTINCT ON (name)` over the table.

    Postgres 16 has no way to skip within an index for that shape: it reads every row the loop has
    ever written and discards all but one per name. So the cost of rendering §6.6's card, and the
    cost of *starting the worker* — `_seed_schedule` runs at every boot — grew with the age of the
    install. Measured on a seeded table before the fix: 2.1 ms at one day of rows, 34 ms at ninety
    days, 7.1 s and 770,000 buffers at a year; 0.7 ms flat afterwards.

    Asserted as rows read rather than as elapsed time, because a timing threshold on a developer
    box is a flake and the defect is not about speed — it is about a read whose work is
    proportional to history rather than to the twelve jobs it answers for.
    """
    names = [job.name for job in worker.JOBS if job.run is not None]
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok) "
        " SELECT n, now() - (g * interval '1 minute'), now(), true "
        "   FROM unnest($1::text[]) AS n, generate_series(1, 500) AS g",
        names,
    )
    written = await db.fetchval("SELECT count(*) FROM job_run")

    before = await _job_run_tuples_read(db)
    health = await admin_api.job_health(db)
    seeded, _dates = await worker._seed_schedule(db, loop_now=0.0, local=NOON)
    read = await _job_run_tuples_read(db) - before

    assert len(health["jobs"]) == len(names) and set(seeded) == set(names)
    assert read <= 4 * len(names), (
        f"the two readers read {read} of {written} rows to answer for {len(names)} jobs"
    )


async def test_the_backup_job_hands_the_dump_the_households_own_clock(data_dir, monkeypatch):
    """The other half of `nightly.todays_dump`'s local-date fix (see `tests/test_backup.py`).

    The guard can only agree with `Job.anchor_hour` if it is asked about the same date, and the
    only caller that knows which date the job was fired for is this one. Asserted as a range
    around `_now_local()` rather than as an equality because the reading is taken inside the job:
    what matters is that it comes from the household's clock and not from `datetime.now(UTC)`,
    which for a household far enough from UTC names a different date for much of the day.
    [M4.7 ops-03]
    """
    from spielplan.backup import nightly

    seen: dict[str, object] = {}

    async def fake_run(local):
        seen["local"] = local
        return nightly.BackupReport(path=Path("none"), bytes=0, pruned=(), kept=0, skipped=True)

    monkeypatch.setattr(nightly, "run", fake_run)
    job = next(j for j in worker.JOBS if j.name == "nightly-backup")

    before = worker._now_local()
    await job.run()
    after = worker._now_local()

    assert before <= seen["local"] <= after, "the dump was handed a clock that is not the household's"


def test_the_job_names_the_card_reads_are_the_registry_s():
    """`api/admin.py` spells the job names rather than importing the registry, so that the web
    process does not import torch. That was one string's worth of drift risk while only the
    backup was named; the names are now the System card's query parameter, so a name that drifts
    is a job the card silently stops reporting. This is the drift that trade buys, closed."""
    live = {j.name for j in worker.JOBS if j.run is not None}
    assert set(admin_api.JOB_NAMES) == live
    assert len(admin_api.JOB_NAMES) == len(set(admin_api.JOB_NAMES))
    assert admin_api.BACKUP_JOB in live


# --- ops-04: a failed job is retried, not exiled ----------------------------------------------


@pytest.fixture
def no_bookkeeping(monkeypatch):
    """Silence `job_run` for the tests that are about the schedule rather than the table."""
    async def start(name: str) -> int | None:
        return None

    async def finish(run_id, *, ok, detail) -> None:
        return None

    monkeypatch.setattr(worker, "_record_start", start)
    monkeypatch.setattr(worker, "_record_finish", finish)


async def test_a_failed_interval_job_is_retried_within_retry_after(data_dir, monkeypatch,
                                                                   no_bookkeeping):
    """Measured before the fix: a failed job ran once and was not due again an hour later.

    The stamp went on before the call, so a nightly job that raised was not due for 24 hours —
    while the comment beside it claimed "the next tick retries it".
    """
    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    job = _job("t-daily", boom, every=86400)
    monkeypatch.setattr(worker, "JOBS", (job,))
    last_run: dict[str, float] = {}
    await worker._tick(0.0, NOON, last_run, {})

    # A second past the boundary rather than exactly on it, in all three of these: the wait runs
    # from the moment the attempt *ended* and raising is not instantaneous, so the true boundary
    # is RETRY_AFTER plus a few microseconds. A second is well inside the claim being made (five
    # minutes, not a day) and outside the noise. `..._outlasts_retry_after...` below is where
    # that difference stops being noise.
    assert not worker.due(worker.RETRY_AFTER - 1, last_run), "retried before RETRY_AFTER"
    assert [j.name for j in worker.due(worker.RETRY_AFTER + 1, last_run)] == ["t-daily"]


async def test_the_retry_never_delays_a_job_past_its_own_interval(data_dir, monkeypatch,
                                                                  no_bookkeeping):
    """RETRY_AFTER is a ceiling on the wait, not a floor under it.

    §7.3's playback poll runs every 60 s because the window between crossing 90% and the credits
    ending is minutes long. Applying a flat five-minute back-off to it would mean one transient
    Jellyfin error costs the prompt its moment — a job made *worse* by the fix to the jobs whose
    interval is a day.
    """
    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "JOBS", (_job("t-minutely", boom, every=60),))
    last_run: dict[str, float] = {}
    await worker._tick(0.0, NOON, last_run, {})

    assert [j.name for j in worker.due(61.0, last_run)] == ["t-minutely"]
    # The other side of the same boundary, which the second above no longer pins: the retry must
    # not make the job due *sooner* than its own interval either.
    assert not worker.due(30.0, last_run), "the retry rewound the stamp past the job's interval"


async def test_a_failed_nightly_job_is_retried_tonight_rather_than_tomorrow(
    data_dir, monkeypatch, no_bookkeeping
):
    """The anchored half of the same rule. Stamping the local date before the call is what stops
    a job that fails in milliseconds from spinning at the tick rate; clearing it on failure is
    what stops a backup that failed at 06:00 from being tomorrow's problem."""
    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "JOBS", (_job("t-night", boom, every=86400, anchor_hour=6),))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}
    await worker._tick(0.0, NOON, last_run, last_date)

    assert "t-night" not in last_date, "a failure must not count as the night being spent"
    soon = worker.due(worker.RETRY_AFTER - 1, last_run, local=NOON, last_date=last_date)
    assert not soon, "a job failing in milliseconds would spin at the tick rate"
    later = worker.due(worker.RETRY_AFTER + 1, last_run, local=NOON, last_date=last_date)
    assert [j.name for j in later] == ["t-night"]


@pytest.mark.parametrize("anchor_hour", [None, 6])
async def test_a_failure_that_outlasts_retry_after_still_waits_the_retry_out(
    data_dir, monkeypatch, no_bookkeeping, anchor_hour
):
    """The retry was measured from the top of the tick, so a slow failure bought no wait at all.

    Every stamp in the `except` branch was computed from `now` — the clock read *before*
    `job.run()` — so `RETRY_AFTER` bounded the gap from when the attempt started. A failure that
    itself takes longer than five minutes was therefore due again on the very next tick, with no
    back-off whatsoever. That is not hypothetical here: this milestone gave the dump a
    `DUMP_TIMEOUT_SECONDS` of 1800 against a `RETRY_AFTER` of 300, so a `pg_dump` blocked behind
    an import's lock fires at 06:00, times out half an hour later, and is immediately due again.
    Modelled over one day: 36 back-to-back attempts, zero idle seconds between them, and because
    `_tick` is sequential the §7.3 poll, the fold-in tick and the heartbeat never run from 06:00
    to midnight — the exact starvation the timeout was added to prevent, met once instead of
    never.

    Both branches, because the arithmetic is per branch and only one of them is the backup: an
    interval job whose failure outlives `min(every, RETRY_AFTER)` spins the same way. The clock
    is real rather than faked because `_tick` measures the attempt with the running loop's own
    clock, and the whole defect is the difference between that and the `now` it is handed.
    [M4.7 cycle 2 finding 4]
    """
    async def slow_boom() -> dict[str, object]:
        await asyncio.sleep(SLOW_FAILURE_SECONDS)
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "RETRY_AFTER", SLOW_FAILURE_SECONDS / 5)
    job = _job("t-slow", slow_boom, every=86400, anchor_hour=anchor_hour)
    monkeypatch.setattr(worker, "JOBS", (job,))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}

    await worker._tick(0.0, NOON, last_run, last_date)

    # When `due` will next let it fire, in the tick's own frame: an interval job is held by its
    # rewound stamp, an anchored one by the RETRY_AFTER floor over the stamp `_tick` left.
    fires_at = last_run["t-slow"] + (job.every if anchor_hour is None else worker.RETRY_AFTER)
    assert fires_at >= SLOW_FAILURE_SECONDS + worker.RETRY_AFTER, (
        "the wait was measured from the start of the attempt, so a failure that outlasts "
        "RETRY_AFTER is re-armed the instant it fails"
    )
    assert not worker.due(fires_at - 0.01, last_run, local=NOON, last_date=last_date)
    due_now = worker.due(fires_at + 0.01, last_run, local=NOON, last_date=last_date)
    assert [j.name for j in due_now] == ["t-slow"]


@pytest.mark.parametrize("anchor_hour", [None, 6])
async def test_a_failure_after_a_slow_sibling_still_waits_the_retry_out(
    data_dir, monkeypatch, no_bookkeeping, anchor_hour
):
    """The same arithmetic, one job later in the same tick — where the failing job is never alone.

    The wait was measured as `now` plus the failing attempt's *own* duration, and `now` is read
    once per tick, before the first job of it. So everything the tick already spent on the jobs
    ahead of this one was missing from the sum, and the stamp landed at the instant the tick
    opened rather than at the failure. Sibling runtimes longer than RETRY_AFTER therefore erased
    the back-off completely, exactly as a slow failure did before cycle 2's fix.

    That ordering is the schedule's, not an accident: a worker with no `job_run` history starting
    after 06:00 — a fresh box, or the Recovery path that drops and recreates the database — finds
    all six anchored jobs due in one tick, and `ANCHOR_BACKUP` puts the dump last so it captures
    the night's work. A `/data/backups` the new image's uid cannot write then fails after the
    sweep and both fits, and was re-armed on the very next 20-second tick.

    One slow *success* ahead of the failure rather than two of them, because the property is
    per-tick elapsed time and one sibling is enough to separate it from `started`. Both branches,
    for the reason the test above takes both. [cycle 3 finding 9]
    """
    async def slow_ok() -> dict[str, object]:
        await asyncio.sleep(SLOW_FAILURE_SECONDS)
        return {}

    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "RETRY_AFTER", SLOW_FAILURE_SECONDS / 5)
    job = _job("t-fail", boom, every=86400, anchor_hour=anchor_hour)
    monkeypatch.setattr(worker, "JOBS", (_job("t-slow-ok", slow_ok, every=60), job))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}

    await worker._tick(0.0, NOON, last_run, last_date)

    fires_at = last_run["t-fail"] + (job.every if anchor_hour is None else worker.RETRY_AFTER)
    assert fires_at >= SLOW_FAILURE_SECONDS + worker.RETRY_AFTER, (
        "the wait was measured from the top of the tick's clock reading, so the time the tick "
        "had already spent on the job before this one bought no back-off"
    )
    assert not worker.due(fires_at - 0.01, last_run, local=NOON, last_date=last_date)
    due_now = worker.due(fires_at + 0.01, last_run, local=NOON, last_date=last_date)
    assert [j.name for j in due_now] == ["t-fail"]


async def test_a_successful_nightly_job_is_not_retried_at_all(data_dir, monkeypatch,
                                                              no_bookkeeping):
    """The counterpart, so the retry floor cannot quietly become a second schedule."""
    async def fine() -> dict[str, object]:
        return {}

    monkeypatch.setattr(worker, "JOBS", (_job("t-night", fine, every=86400, anchor_hour=6),))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}
    await worker._tick(0.0, NOON, last_run, last_date)

    assert last_date["t-night"] == NOON.date()
    assert not worker.due(1e6, last_run, local=NOON, last_date=last_date)


async def test_a_job_still_runs_when_its_bookkeeping_cannot_be_written(data_dir, monkeypatch):
    """Bookkeeping is evidence, not a dependency.

    A database that cannot accept the row is a database the job is about to fail against, and
    that failure is the one worth reporting — a `job_run` insert that took the loop down would
    turn a reporting feature into an outage.
    """
    ran: list[str] = []

    def no_pool():
        raise RuntimeError("database pool not open; call open_pool() during startup")

    async def job() -> dict[str, object]:
        ran.append("yes")
        return {}

    monkeypatch.setattr(worker, "JOBS", (_job("t-nodb", job),))
    monkeypatch.setattr(worker.pool, "acquire", no_pool)
    await worker._tick(0.0, NOON, {}, {})
    assert ran == ["yes"]


# --- ops-07: the loop says it is alive ---------------------------------------------------------


async def test_the_tick_touches_the_heartbeat_the_healthcheck_reads(data_dir, monkeypatch,
                                                                    no_bookkeeping):
    monkeypatch.setattr(worker, "JOBS", ())
    beat = data_dir / "cache" / worker.HEARTBEAT_NAME
    assert not beat.exists()

    await worker._tick(0.0, NOON, {}, {})
    assert beat.is_file(), "a tick that fired no job still went round the loop"

    first = beat.stat().st_mtime_ns
    await worker._tick(60.0, NOON, {}, {})
    assert beat.stat().st_mtime_ns >= first


async def test_a_heartbeat_that_cannot_be_written_does_not_stop_the_loop(
    data_dir, monkeypatch, caplog, no_bookkeeping
):
    """A read-only mount, or a `cache` path that is not a directory, is a reporting problem and
    not a reason to stop doing the work."""
    ran: list[str] = []

    async def job() -> dict[str, object]:
        ran.append("yes")
        return {}

    (data_dir / "cache").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(worker, "JOBS", (_job("t-beat", job),))
    monkeypatch.setattr(worker, "_heartbeat_failed", False)
    with caplog.at_level(logging.WARNING, logger="spielplan.worker"):
        await worker._tick(0.0, NOON, {}, {})
    assert ran == ["yes"]
    assert any("heartbeat" in r.getMessage() for r in caplog.records), (
        "an unwritable heartbeat is silent, and the healthcheck it feeds will say unhealthy"
    )


def test_the_compose_healthcheck_reads_the_file_the_loop_touches():
    """The two halves live in different files and drift silently: a rename here and the check
    passes for ever against a path nothing writes, which is `disable: true` with extra steps.

    The threshold is asserted as a multiple of `TICK_SECONDS` rather than as 120, because what
    makes it right is that it is several ticks of slack and not a number.

    The mount is asked of the worker's own resolved list, not of the file text. M4.7 split
    `x-app-volumes` into `x-worker-volumes` and `x-backend-volumes` and both carry
    `./data/cache:/data/cache`, so a substring over the whole file was satisfied by the
    *backend's* copy — the worker losing the directory its heartbeat is written to and its
    healthcheck is read from was invisible to the guard written to protect it. `_mounts`
    resolves the alias and drops comments; it is imported rather than copied so that a third
    split of the anchor cannot leave one of the two readings behind. [M4.7 ops-07, ddocs-08]
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    block = _nested(_service(compose, "worker"), "healthcheck")
    assert f"/data/cache/{worker.HEARTBEAT_NAME}" in block
    assert ("/data/cache", "rw") in _mounts(compose, "worker"), (
        "the heartbeat needs its own mount, writable, on the service that writes it"
    )

    threshold = re.search(r"st_mtime\s*<\s*(\d+)", block)
    assert threshold, "the healthcheck no longer measures the heartbeat's age"
    seconds = int(threshold.group(1))
    assert seconds >= 3 * worker.TICK_SECONDS, f"{seconds}s is fewer than three ticks of slack"


def test_the_heartbeat_mount_guard_sees_the_worker_lose_the_mount():
    """docs/TESTING.md's rule, owed by the assertion above and missing from it.

    The synthetic violation is the split itself, run one step further: drop the cache line from
    `x-worker-volumes` and leave `x-backend-volumes` alone. The worker then writes its heartbeat
    into the container's own filesystem, the healthcheck reads a path the bind mount no longer
    backs, and every substring form of this guard stays green on the backend's copy.
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    # The worker's anchor is declared first, so the count of one takes its line and not the
    # backend's — which is the whole point of the case.
    dropped = compose.replace("  - ./data/cache:/data/cache\n", "", 1)
    assert "./data/cache:/data/cache" in dropped, "the backend's copy is what hid this"
    assert ("/data/cache", "rw") not in _mounts(dropped, "worker")
    assert ("/data/cache", "rw") in _mounts(dropped, "backend")


async def test_only_the_jobs_that_are_not_minutely_narrate_their_duration(
    data_dir, monkeypatch, caplog, no_bookkeeping
):
    """A successful tick logged nothing at all, so the log could not say when a job started, how
    long it took, or that the loop was alive — and 5d's measurement has nowhere to come from.

    The 60-second jobs stay silent on purpose: three of them would put 4320 lines a day into the
    log §6.6 asks an operator to read.
    """
    async def job() -> dict[str, object]:
        return {}

    monkeypatch.setattr(worker, "JOBS", (
        _job("t-loud", job, every=worker.DURATION_LOG_THRESHOLD),
        _job("t-quiet", job, every=60),
    ))
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        await worker._tick(0.0, NOON, {}, {})

    lines = [r.getMessage() for r in caplog.records]
    assert "job t-loud started" in lines
    assert any(re.fullmatch(r"job t-loud done in \d+\.\ds", line) for line in lines), lines
    assert not any("t-quiet" in line for line in lines)


def test_the_jobs_that_narrate_are_the_ones_worth_narrating():
    """The threshold is a claim about §5.3's table: everything at 15 minutes and above is rare
    enough that its line is the only evidence it ran."""
    loud = {j.name for j in worker.JOBS if j.run and j.every >= worker.DURATION_LOG_THRESHOLD}
    assert "nightly-backup" in loud and "jellyfin-seen-sync" in loud
    assert "jellyfin-sessions-poll" not in loud and "fold-in-tick" not in loud


# --- ops-14: the request path keeps two cores --------------------------------------------------


@pytest.mark.parametrize(
    ("cores", "threads"), [(None, 1), (1, 1), (2, 1), (4, 2), (8, 2), (16, 2)]
)
def test_the_cold_tower_leaves_the_request_path_its_cores(monkeypatch, cores, threads):
    """`tower_threads`'s own docstring says letting torch claim every core makes the nightly
    sweep starve the request path, and the body was `min(4, cpu_count())` — every core on §2's
    4-vCPU reference box. The sweep runs on the loop thread inside a sequential tick, so the
    cores it does not take are the only ones §7.3's minute poll and a phone's request have."""
    monkeypatch.setattr(tower.os, "cpu_count", lambda: cores)
    assert tower.tower_threads() == threads


def test_the_reference_box_keeps_more_cores_than_the_tower_takes(monkeypatch):
    """Stated as the property rather than the number, which is what the docstring promises."""
    monkeypatch.setattr(tower.os, "cpu_count", lambda: 4)
    assert tower.tower_threads() < 4


# --- arch-09: one process, and a refusal that says why -----------------------------------------


@pytest.fixture
def one_process(monkeypatch):
    """A clean starting point: neither signal set, whatever the developer's shell holds."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "spielplan.app:app"])


@pytest.mark.parametrize(
    "setting",
    [
        {"env": "2"},
        {"argv": ["--workers", "2"]},
        {"argv": ["--workers=4"]},
        {"argv": ["-w", "3"]},
    ],
)
def test_the_app_refuses_to_boot_with_a_multi_worker_setting(monkeypatch, one_process, setting):
    """A `--workers 2` an operator adds for a Friday night splits the household into two lobbies
    and two rails: half the phones join a session the other half cannot see. Every failure that
    follows looks like a product bug, and nothing logs a thing.

    All four forms, because catching one of them is worse than catching none: it teaches the
    operator that the app checks.
    """
    if "env" in setting:
        monkeypatch.setenv("WEB_CONCURRENCY", setting["env"])
    else:
        monkeypatch.setattr(sys, "argv", ["uvicorn", "spielplan.app:app", *setting["argv"]])

    with pytest.raises(RuntimeError) as refused:
        create_app()
    message = str(refused.value)
    assert "single-process" in message
    assert "lobbies" in message and "rail" in message, "the refusal has to say what breaks"
    assert message.isascii(), "log and error text is ASCII (CLAUDE.md)"


@pytest.mark.parametrize(
    "argv",
    [
        ["uvicorn", "spielplan.app:app"],
        ["uvicorn", "spielplan.app:app", "--workers", "1"],
        ["uvicorn", "spielplan.app:app", "-w", "1"],
        ["uvicorn", "spielplan.app:app", "--workers"],          # a truncated command line
        ["uvicorn", "spielplan.app:app", "-w", "not-a-number"],
    ],
)
def test_one_worker_still_boots(monkeypatch, one_process, argv):
    """The refusal must not fire on the shipped CMD, nor on a malformed one it cannot read —
    an app that will not start because it mis-parsed its own argv is a worse failure than the
    one being prevented."""
    monkeypatch.setattr(sys, "argv", argv)
    assert create_app().title == "Spielplan"


def test_a_single_worker_env_setting_is_not_a_refusal(monkeypatch, one_process):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert create_app().title == "Spielplan"
