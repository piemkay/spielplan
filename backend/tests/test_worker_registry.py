"""The worker loop: job outcomes as `job_run` rows, bounded retention, prompt retries, a heartbeat,
and one process. The integration half drives `_tick` against a real pool; the rest needs no
database and must not skip."""

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

# Borrowed: `_mounts` resolves the `*worker-volumes` alias and strips comments, which tells the
# worker's cache mount from the backend's copy.
from tests.test_static_contracts import _mounts, _nested, _service

REPO = Path(__file__).resolve().parents[2]

# Past every anchor, so an anchored job is due. A fixed offset: Windows has no tz database.
NOON = datetime(2026, 9, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))

# Stands in for `DUMP_TIMEOUT_SECONDS` (1800 s) against `RETRY_AFTER` (300 s).
SLOW_FAILURE_SECONDS = 0.25


def _job(name: str, run, *, every: int = 60, anchor_hour: int | None = None) -> worker.Job:
    return worker.Job(name, "M0", "test", "ms", run, every=every, anchor_hour=anchor_hour)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`_tick` writes the heartbeat under `DATA_DIR`; otherwise a test run touches `/data/cache`."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings.cache_clear()
    yield tmp_path
    settings.cache_clear()


@pytest.fixture
async def worker_env(db, pg_url, data_dir, monkeypatch):
    """`_tick` acquires from the pool as the loop does."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    await pool.open_pool(pg_url)
    try:
        yield data_dir
    finally:
        await pool.close_pool()
        settings.cache_clear()


async def test_every_job_the_worker_fires_writes_a_job_run_row(worker_env, db, monkeypatch):
    """Both outcomes in one tick: a table of successes alone cannot answer an operator."""
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
    """The row outlives the rotated log, so it carries the reason, not only the verdict."""
    async def boom() -> dict[str, object]:
        raise RuntimeError("pg_dump exited 1: could not translate host name")

    monkeypatch.setattr(worker, "JOBS", (_job("t-fail", boom),))
    await worker._tick(0.0, NOON, {}, {})

    row = await db.fetchrow("SELECT ok, detail FROM job_run WHERE name = 't-fail'")
    assert row["ok"] is False
    assert "could not translate host name" in row["detail"]["error"]
    assert "RuntimeError" in row["detail"]["error"], "the type is half the diagnosis"


async def test_the_row_is_open_while_the_job_runs(worker_env, db, monkeypatch):
    """Written before the call, so a killed job leaves a started-never-finished row. The job reads
    its own row to prove the ordering."""
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
    """The registry's own callables reach `_tick`."""
    prune = next(j for j in worker.JOBS if j.name == "session-prune")
    # Every other job held off: interval ones by a fresh stamp, anchored ones by today's date.
    await worker._tick(
        0.0,
        NOON,
        {j.name: 0.0 for j in worker.JOBS if j is not prune},
        {j.name: NOON.date() for j in worker.JOBS if j is not prune},
    )

    rows = await db.fetch("SELECT name, ok FROM job_run")
    assert [(r["name"], r["ok"]) for r in rows] == [("session-prune", True)]


async def test_the_schedule_seed_reads_the_newest_successful_run_per_job(worker_env, db):
    """A failed run must not count as the night's dump, and an older success must not shadow a newer one."""
    # A real registry name: the seed asks only about names the registry has.
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
    # Read back from the row: "six hours ago" straddles midnight for two hours a day.
    assert last_date["nightly-backup"] == newest.astimezone(NOON.tzinfo).date()


async def _job_run_tuples_read(conn) -> int:
    """Both calls are needed, or two readings either side of a call are the same number."""
    await conn.execute("SELECT pg_stat_force_next_flush()")
    await conn.execute("SELECT pg_stat_clear_snapshot()")
    return await conn.fetchval(
        "SELECT coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) "
        "  FROM pg_stat_all_tables WHERE relname = 'job_run'"
    )


async def test_the_job_run_table_has_a_prune_and_it_keeps_a_fortnight(worker_env, db):
    """Three jobs fire every minute (~4,300 rows a day). A fortnight, like §2's fourteen dumps."""
    prune = next((j for j in worker.JOBS if j.name == "job-run-prune"), None)
    assert prune is not None and prune.run is not None, "nothing deletes from job_run"

    keep = worker.JOB_RUN_KEEP_DAYS
    # Failures outside the window: a job's newest success is exempt at any age.
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
    """Age-only pruning deleted the last successful backup row, and the card then claimed no dump had
    ever run. The newest success per job is kept; `_seed_schedule` reads the same row."""
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
    """Retention shorter than a job's interval would erase `_seed_schedule`'s memory between runs."""
    longest = max(job.every for job in worker.JOBS if job.run is not None)
    assert longest < worker.JOB_RUN_KEEP_DAYS * 86400


async def test_neither_reader_of_job_run_pays_for_the_rows_it_is_not_reading(db):
    """`DISTINCT ON (name)` reads the whole history (7.1 s at a year). Asserted as rows read, not
    time. Rows carry a reached report, as `last_syncs` reads them (decision 454)."""
    names = [job.name for job in worker.JOBS if job.run is not None]
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok, detail) "
        " SELECT n, now() - (g * interval '1 minute'), now(), true, '{\"reached\": true}'::jsonb "
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
    """The date must come from the household's clock, as `Job.anchor_hour` does; a range because it is
    read inside the job."""
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
    """`api/admin.py` spells the names so the web process never imports torch; this pins them."""
    live = {j.name for j in worker.JOBS if j.run is not None}
    assert set(admin_api.JOB_NAMES) == live
    assert len(admin_api.JOB_NAMES) == len(set(admin_api.JOB_NAMES))
    assert admin_api.BACKUP_JOB in live


@pytest.fixture
def no_bookkeeping(monkeypatch):
    """Silence `job_run` for tests about the schedule."""
    async def start(name: str) -> int | None:
        return None

    async def finish(run_id, *, ok, detail) -> None:
        return None

    monkeypatch.setattr(worker, "_record_start", start)
    monkeypatch.setattr(worker, "_record_finish", finish)


async def test_a_failed_interval_job_is_retried_within_retry_after(data_dir, monkeypatch,
                                                                   no_bookkeeping):
    """The stamp went on before the call, so a failed nightly job was not due for 24 hours."""
    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    job = _job("t-daily", boom, every=86400)
    monkeypatch.setattr(worker, "JOBS", (job,))
    last_run: dict[str, float] = {}
    await worker._tick(0.0, NOON, last_run, {})

    # A second past the boundary: the wait runs from when the attempt ended.
    assert not worker.due(worker.RETRY_AFTER - 1, last_run), "retried before RETRY_AFTER"
    assert [j.name for j in worker.due(worker.RETRY_AFTER + 1, last_run)] == ["t-daily"]


async def test_the_retry_never_delays_a_job_past_its_own_interval(data_dir, monkeypatch,
                                                                  no_bookkeeping):
    """RETRY_AFTER is a ceiling: the 60 s playback poll must not wait five minutes."""
    async def boom() -> dict[str, object]:
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "JOBS", (_job("t-minutely", boom, every=60),))
    last_run: dict[str, float] = {}
    await worker._tick(0.0, NOON, last_run, {})

    assert [j.name for j in worker.due(61.0, last_run)] == ["t-minutely"]
    # The retry must not make the job due sooner than its interval either.
    assert not worker.due(30.0, last_run), "the retry rewound the stamp past the job's interval"


async def test_a_failed_nightly_job_is_retried_tonight_rather_than_tomorrow(
    data_dir, monkeypatch, no_bookkeeping
):
    """The date stamp stops a fast failure spinning; clearing it on failure retries tonight."""
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
    """The retry was measured from before `job.run()`, so a failure longer than RETRY_AFTER was due at
    once. Both branches; a real clock, because the defect is that difference."""
    async def slow_boom() -> dict[str, object]:
        await asyncio.sleep(SLOW_FAILURE_SECONDS)
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "RETRY_AFTER", SLOW_FAILURE_SECONDS / 5)
    job = _job("t-slow", slow_boom, every=86400, anchor_hour=anchor_hour)
    monkeypatch.setattr(worker, "JOBS", (job,))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}

    await worker._tick(0.0, NOON, last_run, last_date)

    # Interval jobs are held by the rewound stamp, anchored ones by the RETRY_AFTER floor.
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
    """`now` is read once per tick, so time spent on earlier jobs was missing from the wait. A fresh
    box finds all anchored jobs due at once, with the dump last."""
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
    """So the retry floor cannot become a second schedule."""
    async def fine() -> dict[str, object]:
        return {}

    monkeypatch.setattr(worker, "JOBS", (_job("t-night", fine, every=86400, anchor_hour=6),))
    last_run: dict[str, float] = {}
    last_date: dict[str, object] = {}
    await worker._tick(0.0, NOON, last_run, last_date)

    assert last_date["t-night"] == NOON.date()
    assert not worker.due(1e6, last_run, local=NOON, last_date=last_date)


async def test_a_job_still_runs_when_its_bookkeeping_cannot_be_written(data_dir, monkeypatch):
    """A `job_run` insert that took the loop down would turn reporting into an outage."""
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
    """A read-only mount is a reporting problem, not a reason to stop."""
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
    """A multiple of `TICK_SECONDS`, not 120. The mount is read from the worker's resolved list: the
    backend's copy of the same line satisfied a substring check."""
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
    """Drops the cache line from `x-worker-volumes` only; every substring guard stays green on the
    backend's copy."""
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    # The worker's anchor is declared first, so a count of one takes its line.
    dropped = compose.replace("  - ./data/cache:/data/cache\n", "", 1)
    assert "./data/cache:/data/cache" in dropped, "the backend's copy is what hid this"
    assert ("/data/cache", "rw") not in _mounts(dropped, "worker")
    assert ("/data/cache", "rw") in _mounts(dropped, "backend")


async def test_only_the_jobs_that_are_not_minutely_narrate_their_duration(
    data_dir, monkeypatch, caplog, no_bookkeeping
):
    """The 60-second jobs stay silent: 4,320 lines a day otherwise."""
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
    """15 minutes and above is rare enough that the line is the evidence it ran."""
    loud = {j.name for j in worker.JOBS if j.run and j.every >= worker.DURATION_LOG_THRESHOLD}
    assert "nightly-backup" in loud and "jellyfin-seen-sync" in loud
    assert "jellyfin-sessions-poll" not in loud and "fold-in-tick" not in loud


@pytest.mark.parametrize(
    ("cores", "threads"), [(None, 1), (1, 1), (2, 1), (4, 2), (8, 2), (16, 2)]
)
def test_the_cold_tower_leaves_the_request_path_its_cores(monkeypatch, cores, threads):
    """The sweep runs on the loop thread; the cores torch does not take are the request path's."""
    monkeypatch.setattr(tower.os, "cpu_count", lambda: cores)
    assert tower.tower_threads() == threads


def test_the_reference_box_keeps_more_cores_than_the_tower_takes(monkeypatch):
    """Stated as the property rather than the number, which is what the docstring promises."""
    monkeypatch.setattr(tower.os, "cpu_count", lambda: 4)
    assert tower.tower_threads() < 4


@pytest.fixture
def one_process(monkeypatch):
    """Neither signal set, whatever the developer's shell holds."""
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
    """`--workers 2` splits the household into two lobbies and two rails. All four forms: catching one
    teaches the operator a check exists."""
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
    """An app that will not start because it mis-parsed argv is worse than the failure prevented."""
    monkeypatch.setattr(sys, "argv", argv)
    assert create_app().title == "Spielplan"


def test_a_single_worker_env_setting_is_not_a_refusal(monkeypatch, one_process):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert create_app().title == "Spielplan"
