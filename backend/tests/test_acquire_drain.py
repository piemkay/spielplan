"""The acquisition drain inside the worker registry, through `worker.JOBS`
and `pool` (§8, §5.3). The budget must sit under `queue.LEASE_SECONDS`,
and a broken install refuses before it leases. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from spielplan import worker
from spielplan.acquire import fetch, hosts, pipeline, queue, stages
from spielplan.core.config import settings
from spielplan.db import pool
from spielplan.dna import verify
from spielplan.importer import bundle as bundle_import
from tests.fixtures import make_bundle as fx

# This job has no `anchor_hour`, so `due` reads the monotonic interval, never the date.
NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone(timedelta(hours=2)))

DRAIN = "acquisition-drain"


# One acquisition's requests to the slowest hand-paced host (Metacritic), counted off the adapters.
_METACRITIC_REQUESTS_A_TITLE = 6

# Captured at import: the autouse stand-down below rebuilds `pipeline.STAGES` without `fetches`.
_SHIPPED_STAGES = pipeline.STAGES


def _job():
    """Read out of `JOBS`: a hand-built `Job` would say nothing about the budget, interval or name."""
    return next(j for j in worker.JOBS if j.name == DRAIN)


def _item(n: int) -> dict:
    """Ids far from other files': the resolver matches the provider id first."""
    return {
        "Id": f"jf-drain-{n}", "Name": f"Drain Title {n}", "Type": "Movie",
        "ProductionYear": 1970 + n, "RunTimeTicks": 100 * 60 * 10_000_000,
        "ProviderIds": {"Imdb": f"tt62{n:05d}"},
    }


async def _task_row(db, key: str):
    return await db.fetchrow(
        "SELECT * FROM acquisition_task WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, key
    )


async def _acquired(db) -> int:
    return await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'")


@pytest.fixture
async def worker_env(db, pg_url, tmp_path, monkeypatch):
    """The job takes no connection, so the real pool is opened; `settings()` is cleared both ways."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    (tmp_path / "data" / "artifacts").mkdir(parents=True)
    await pool.open_pool(pg_url)
    try:
        yield tmp_path
    finally:
        await pool.close_pool()
        settings.cache_clear()


@pytest.fixture
async def bundled(db, worker_env, tmp_path):
    """Without an active bundle every task parks at stage 9 and completion is unassertable."""
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    return report


async def _refuse_to_crawl(_conn):
    raise AssertionError(
        "this test reached §8 stage 2, whose real fetcher would crawl five hosts per title from "
        "whatever machine is running the suite. The drain's fetcher is injectable "
        "(`pipeline.drain(fetcher_factory=...)`); this file's subject is the worker job, so it "
        "stands the fetching stages down instead"
    )


@pytest.fixture(autouse=True)
def enrichment_stands_down(monkeypatch):
    """Stages 2-4 and 5, 7 advance without doing anything and stage 6 is
    `implemented=False`: this file is about the worker job, not the crawl,
    and a live stage 2 would crawl real hosts from every test machine."""
    monkeypatch.setattr(pipeline, "_default_fetcher", _refuse_to_crawl)

    def stands_down(stage):
        async def stood_down(_ctx):
            return stages.advance({"stood_down": f"stage {stage.number} stood down by this file"})
        return pipeline.Stage(stage.number, stage.name, stood_down, stage.paid,
                              stage.implemented and not stage.paid, stage.owner)

    monkeypatch.setattr(pipeline, "STAGES", tuple(
        stands_down(stage) if stage.number in (2, 3, 4, 5, 6, 7) else stage
        for stage in pipeline.STAGES
    ))


def test_the_drains_budget_is_bounded_by_the_lease_it_takes_and_by_its_own_batch():
    """A margin of two under the lease is the floor: at parity
    the reaper hands a live task to a second worker."""
    job = _job()
    assert job.run is worker._acquisition_drain

    assert job.timeout * 2 <= queue.LEASE_SECONDS, (
        f"a {job.timeout}s attempt against a {queue.LEASE_SECONDS}s lease leaves the reaper and "
        "the budget within one attempt of each other"
    )
    per_task = job.timeout / pipeline.DRAIN_LIMIT
    assert per_task >= 1.0, (
        f"{pipeline.DRAIN_LIMIT} tasks in {job.timeout}s is {per_task:.2f}s each, and section "
        "5.3 prices the Cold Tower's forward pass alone at under a second a title"
    )


def test_the_drains_budget_prices_the_crawl_its_stages_now_do():
    """A budget at or under the 300 s `Retry-After` ceiling is one a single 429 cancels."""
    job = _job()
    fetching = [stage for stage in _SHIPPED_STAGES if stage.implemented and stage.fetches]
    assert fetching, (
        "no implemented stage fetches, so this test is measuring a pipeline that does not exist"
    )

    ceiling = fetch._retry_after({"retry-after": "100000"})
    assert job.timeout > ceiling, (
        f"a {job.timeout}s batch against a {ceiling}s in-process Retry-After ceiling is a batch "
        "one 429 cancels, and the cancelled task keeps its attempt"
    )
    slowest = min(hosts.HOST_POLICIES[host].rps
                  for host in ("www.rottentomatoes.com", "www.metacritic.com"))
    assert job.timeout >= pipeline.DRAIN_LIMIT * _METACRITIC_REQUESTS_A_TITLE / slowest, (
        f"{pipeline.DRAIN_LIMIT} titles at {_METACRITIC_REQUESTS_A_TITLE} requests each against "
        f"{slowest} rps does not fit in {job.timeout}s of pure pacing, before any latency"
    )
    assert job.name in worker.MODEL_JOBS, (
        "stage 9 places against the active bundle, so this job must be skipped across section "
        "10's flip like every other job that fits in a basis"
    )


async def test_one_tick_leases_its_bound_and_completes_what_it_leased(worker_env, bundled, db):
    """Nine tasks, not eight: eight would pass with no limit; the second tick shows the ninth waited."""
    for n in range(1, pipeline.DRAIN_LIMIT + 2):
        assert await pipeline.enqueue_item(db, _item(n)) is True

    first = await _job().run()
    assert first is not None
    assert first["leased"] == pipeline.DRAIN_LIMIT, first
    assert first["ready"] == pipeline.DRAIN_LIMIT, first
    assert first["parked"] == 0 and first["failed"] == 0, first
    assert first["basis"] == "test-v1", "the batch was drained against the active bundle"

    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE state = $1", queue.DONE
    ) == pipeline.DRAIN_LIMIT
    assert await queue.pending_count(db, [pipeline.TASK_KIND]) == 1, (
        "the ninth task is still waiting; a tick that took it would not be bounded"
    )

    second = await _job().run()
    assert second is not None
    assert (second["leased"], second["ready"]) == (1, 1), second
    assert await _acquired(db) == pipeline.DRAIN_LIMIT + 1
    assert await queue.pending_count(db, [pipeline.TASK_KIND]) == 0


async def test_a_paid_task_is_left_for_a_caller_that_knows_a_cap_has_been_checked(
    worker_env, bundled, db
):
    """`attempts` is the teeth: "never taken" and "taken and returned" are both `pending`."""
    assert await queue.enqueue(
        db, pipeline.TASK_KIND, "imdb:tt6299999", {"item": _item(99)}, paid=True
    ) is True
    assert await pipeline.enqueue_item(db, _item(1)) is True

    detail = await _job().run()
    assert detail is not None
    assert detail["leased"] == 1, detail
    assert [task["key"] for task in detail["tasks"]] == ["jellyfin:jf-drain-1"]

    paid = await _task_row(db, "imdb:tt6299999")
    assert paid["state"] == queue.PENDING
    assert paid["attempts"] == 0, "the paid task was leased and put back, which is a bill"


async def test_a_task_whose_worker_died_is_reclaimed_on_the_next_tick_and_finished_once(
    worker_env, bundled, db
):
    """A cancelled job runs no handler; the lease is expired in place, and once is asserted on rows."""
    assert await pipeline.enqueue_item(db, _item(1)) is True
    dead = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    assert len(dead) == 1
    await db.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 minute' WHERE id = $1",
        dead[0].id,
    )
    assert await queue.pending_count(db, [pipeline.TASK_KIND]) == 0, (
        "a task held by a dead worker is invisible to a count of pending rows, which is why the "
        "job reclaims before it counts"
    )

    detail = await _job().run()
    assert detail is not None
    assert detail["reclaimed"] == {queue.PENDING: 1, queue.FAILED: 0}, detail
    assert (detail["leased"], detail["ready"]) == (1, 1), detail
    assert detail["basis"] == "test-v1", (
        "the reclaimed task was counted as work, so the batch was refused against a basis"
    )

    row = await _task_row(db, dead[0].key)
    assert row["state"] == queue.DONE
    assert row["attempts"] == 2, "the dead worker's attempt and this one, and no third"

    title_id = await db.fetchval("SELECT id FROM title WHERE origin = 'acquired'")
    assert title_id is not None
    assert await _acquired(db) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", title_id
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", title_id
    ) == 1


async def test_a_broken_install_refuses_the_batch_before_it_spends_an_attempt(worker_env, db):
    """Left to stage 9, a missing bundle directory would spend `max_attempts` on eight titles a tick."""
    assert await pipeline.enqueue_item(db, _item(1)) is True
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) "
        "VALUES ('gone-v9', '{}'::jsonb, 'active')"
    )

    with pytest.raises(RuntimeError, match="does not exist"):
        await _job().run()

    row = await _task_row(db, "jellyfin:jf-drain-1")
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 0, "the batch was leased before the basis was asked for"
    assert row["last_error"] is None
    assert await _acquired(db) == 0
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0


async def test_a_tick_with_an_empty_queue_is_a_cheap_no_op(worker_env, bundled, db, monkeypatch):
    """Cheap is "the basis was not opened"; the inbox board rows must stay exactly as they were."""
    opened: list[str] = []
    real = worker._active_store

    async def counting(conn):
        opened.append("load")
        return await real(conn)

    monkeypatch.setattr(worker, "_active_store", counting)
    inbox = "SELECT title_id, stage, status, updated_at FROM acquisition_job ORDER BY title_id"
    before = [tuple(row) for row in await db.fetch(inbox)]
    assert before, "the import parked no thin title, so this test cannot see the inbox at all"

    assert await _job().run() is None
    assert opened == [], "the basis was opened for a tick with nothing to drain"
    assert await _acquired(db) == 0
    assert [tuple(row) for row in await db.fetch(inbox)] == before

    # A pending PAID task is not work this tick takes, so it must not open the basis.
    assert await queue.enqueue(db, pipeline.TASK_KIND, "tmdb:paid-1", paid=True) is True
    assert await _job().run() is None
    assert opened == [], "a paid task this drain cannot lease made the tick ask for the basis"

    # ...and the guard is not simply never calling it: one task makes the same tick ask.
    assert await pipeline.enqueue_item(db, _item(1)) is True
    assert await _job().run() is not None
    assert opened == ["load"]


async def test_the_job_writes_a_job_run_row_like_every_other_job(worker_env, bundled, db, monkeypatch):
    """Through `_tick`, because the `job_run` row is the loop's write, not the job's."""
    assert await pipeline.enqueue_item(db, _item(1)) is True
    monkeypatch.setattr(worker, "JOBS", (_job(),))

    await worker._tick(1e9, NOON, {}, {})

    row = await db.fetchrow("SELECT * FROM job_run WHERE name = $1", DRAIN)
    assert row is not None, "the loop fired the drain and recorded nothing"
    assert row["ok"] is True
    assert row["finished_at"] is not None
    assert row["detail"]["ready"] == 1, row["detail"]
    assert row["detail"]["basis"] == "test-v1"
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE state = $1", queue.DONE
    ) == 1


async def test_a_drain_cancelled_at_its_budget_hands_back_what_it_had_not_started(
    worker_env, bundled, db, monkeypatch
):
    """The lease counts the attempt on the claim, so a budget cancel must hand back unstarted tasks."""
    for n in (1, 2, 3):
        assert await pipeline.enqueue_item(db, _item(n)) is True

    async def crawls(_ctx):
        await asyncio.sleep(30)
        return None

    patched = tuple(
        pipeline.Stage(s.number, s.name, crawls, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_job().run(), 1.0)

    rows = {row["key"]: row for row in await db.fetch(
        "SELECT key, state, attempts, result_note FROM acquisition_task ORDER BY id"
    )}
    assert len(rows) == 3
    started = [r for r in rows.values() if r["state"] == queue.LEASED]
    assert len(started) == 1, (
        "the batch was claimed in one statement and the cancellation left all of it leased"
    )
    assert started[0]["attempts"] == 1, "the task that ate the budget keeps its attempt"

    handed_back = [r for r in rows.values() if r["state"] == queue.PENDING]
    assert len(handed_back) == 2
    for row in handed_back:
        assert row["attempts"] == 0, (
            "a task that was claimed and never started was charged an attempt it did not use"
        )
        assert "budget expired before this task was started" in row["result_note"]

    # And they are due now rather than after a backoff: nothing failed.
    assert await queue.pending_count(db, [pipeline.TASK_KIND]) == 2


async def test_a_broken_install_still_corrects_the_board_the_reaper_closed(worker_env, bundled, db):
    """The board correction is pure SQL and needs no bundle, so it runs before the basis refusal."""
    assert await pipeline.enqueue_item(db, _item(1)) is True
    first = (await queue.lease(db, [pipeline.TASK_KIND], limit=1))[0]
    walked = await pipeline.run_task(db, first)
    assert walked.status == "ready", walked.as_dict()
    title_id = walked.title_id

    # The crash marker: a worker killed mid-stage on its last attempt.
    await db.execute(
        "UPDATE acquisition_job SET stage = 3, status = 'running', reason = NULL"
        " WHERE title_id = $1", title_id,
    )
    await db.execute(
        "UPDATE acquisition_task SET state = $1, lease_owner = 'dead-worker',"
        "       lease_expires = now() - interval '1 second', attempts = max_attempts"
        " WHERE id = $2", queue.LEASED, first.id,
    )
    # Something plainly due: the batch refusal is asked for only when the queue has work.
    assert await pipeline.enqueue_item(db, _item(2)) is True

    # The bundle row stays active while its directory is gone.
    shutil.rmtree(settings().artifacts_dir)

    with pytest.raises(RuntimeError, match="does not exist"):
        await _job().run()

    assert (await _task_row(db, first.key))["state"] == queue.FAILED, (
        "the queue half of the reaper did not run, so this test is asserting nothing"
    )
    board = await db.fetchrow(
        "SELECT status, reason FROM acquisition_job WHERE title_id = $1", title_id
    )
    assert board["status"] == "failed", (
        "the board reads `running` for a task this same tick closed for good"
    )
    assert queue.ABANDONED in board["reason"]

    # The refusal itself is untouched: the second task keeps its attempt and nothing was placed.
    waiting = await _task_row(db, "jellyfin:jf-drain-2")
    assert (waiting["state"], waiting["attempts"]) == (queue.PENDING, 0)


async def test_the_ticks_run_is_the_run_stage_six_files_under_and_stage_seven_reads(
    worker_env, bundled, db, monkeypatch
):
    """Decision 468: the `job_run` row `_tick` opens is the
    run the walks carry, so stage 7 finds its refusals."""
    assert await pipeline.enqueue_item(db, _item(1)) is True

    async def refuses_one_tag(ctx):
        await verify.record_rejects(
            ctx.conn, [verify.Rejection(ctx.title_id, "pass-0", "themes.invented", "unknown_term")],
            run_id=ctx.run_id, provider="stand-in",
        )
        return stages.advance({"stood_in": "stage 6 filed one refusal under the walk's run"})

    shipped_verify = next(stage for stage in _SHIPPED_STAGES if stage.number == 7)
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        pipeline.Stage(6, stage.name, refuses_one_tag, stage.paid, False, stage.owner)
        if stage.number == 6 else shipped_verify if stage.number == 7 else stage
        for stage in pipeline.STAGES
    ))
    monkeypatch.setattr(worker, "JOBS", (_job(),))

    await worker._tick(1e9, NOON, {}, {})

    run = await db.fetchrow("SELECT id, ok, detail FROM job_run WHERE name = $1", DRAIN)
    assert run["ok"] is True and run["detail"]["ready"] == 1, dict(run)
    title_id = await db.fetchval("SELECT id FROM title WHERE origin = 'acquired'")
    filed = [tuple(row) for row in await db.fetch(
        "SELECT run_id, rule_violated FROM dna_reject WHERE title_id = $1", title_id
    )]
    assert filed == [(run["id"], "unknown_term")], (
        f"stage 6 filed {filed} and the tick's run is {run['id']}: the drain handed its walk no run"
    )
    verdict = (await db.fetchval(
        "SELECT detail FROM acquisition_job WHERE title_id = $1", title_id
    ))["verify"]
    assert verdict["rejected"] == {"unknown_term": 1}, (
        f"stage 7 recorded {verdict} beside a refusal stage 6 filed in the same walk"
    )
