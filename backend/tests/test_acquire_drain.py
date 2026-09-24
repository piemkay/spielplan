"""The acquisition drain, inside M4.7's worker registry. Spec v2.1 §8, §5.3, §3.1, §10.

`test_acquire_pipeline.py` proves the driver walks a task through §8's ten stages. What it cannot
see is whether anything ever calls it: a queue with no drain is a table that fills up, and every
other M5 milestone plugs into a job that has to exist first. This file is that seam - the one row
M5.1 adds to §5.3's registry - asserted through `worker.JOBS` and `pool` rather than by calling
`pipeline.drain` directly, because the production call path is what is under test (the same
argument `test_worker_jobs.py`'s `worker_env` makes).

TWO CLAIMS THE REGISTRY CANNOT MAKE ABOUT ITSELF, and they are why this file exists rather than
another parametrize case in `test_worker_jobs.py`:

  * **The budget is bounded by a constant in another module.** `Job.timeout` has to sit well under
    `queue.LEASE_SECONDS`, or the reaper hands a task to a second worker while the first is still
    walking it - two processes, one title, both writing into a spine decision 162 makes permanent.
    `test_worker_jobs.py` holds the backup's budget against `pg_dump`'s own timeout for exactly
    this reason; nothing but an assertion keeps two constants in two files in order.
  * **The batch refuses before it leases.** A broken install must cost no task an attempt, and
    "before" is the whole of that claim: the same code with the refusal moved after the lease
    passes every assertion about the exception and still closes eight acquisitions a tick.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

# The wall clock `_tick` is handed. Any hour will do: this job carries no `anchor_hour`, so `due`
# reads the monotonic interval and never the date.
NOON = datetime(2026, 9, 17, 12, 0, tzinfo=timezone(timedelta(hours=2)))

DRAIN = "acquisition-drain"


# What one first acquisition costs the slowest host §8 stage 2 names by hand, counted off the
# adapters rather than guessed: `metacritic:page` tries the year-qualified candidate and then the
# bare one, `metacritic:reviews` resolves the same way because the page kind writes no slug until a
# candidate is accepted, and the two review views are one request each. Six, and the robots.txt read
# is a seventh that the cache pays once per drain. `_walk_batch` is sequential and `stages.enrich`
# walks its kinds in a plain loop, so a batch costs about the slowest host's queue rather than the
# sum of every host's. [M5.3 review cycle 1, M53-C1-NET-03]
_METACRITIC_REQUESTS_A_TITLE = 6

# The shipped stage table, captured at import. `enrichment_stands_down` below is autouse and
# rebuilds `pipeline.STAGES` without `fetches`, which is right for every test in this file whose
# subject is the worker job - and wrong for the one test whose subject is whether the budget prices
# the crawl those stages really do. Read at module scope, which runs before any fixture.
_SHIPPED_STAGES = pipeline.STAGES


def _job():
    """The registry row, read out of `JOBS` rather than built here.

    A test that constructed its own `Job` would assert that `_acquisition_drain` works and say
    nothing about the budget, the interval or the name the loop fires it under - which is most of
    what this milestone added.
    """
    return next(j for j in worker.JOBS if j.name == DRAIN)


def _item(n: int) -> dict:
    """One Jellyfin item with a provider id, so stage 1 mints rather than parking (decision 323).

    Ids far from `test_acquire_pipeline.py`'s and from the fixture bundle's, because
    `resolve.resolve_title_id` matches on the provider id first and a collision here would be a
    test that proved the resolver instead of the drain.
    """
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
    """The worker's own view of the world: the real pool, and `DATA_DIR` under this test.

    The job takes no connection - it acquires from the pool, exactly as `_tick` calls it - so this
    opens the real pool against the test database rather than handing it `db`. `settings()` is
    `lru_cache`d, so the cache is cleared on the way in and on the way out.
    """
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
    """A real bundle, imported and active, so stage 9 has a basis and a task can reach `ready`.

    Without one every task in this file would park at stage 9 with §3.1's reason, and "the drain
    completes what it leased" - which is the claim - would be unassertable.
    """
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
    """Stages 2, 3 and 4 advance without doing anything. M5.3, for this file's own subject.

    THIS FILE IS ABOUT THE WORKER JOB AND NOT ABOUT THE CRAWL. Its claims are §5.3's - the budget
    sits under `queue.LEASE_SECONDS`, the batch refuses before it leases, a killed worker's task is
    reclaimed and finished once, the `job_run` row looks like every other job's - and every one of
    them is asserted through a task that walks all ten stages and ends at `ready`.

    M5.3 gave stages 2, 3 and 4 bodies, and both halves of that break this file. A title minted
    from a bare Jellyfin item has no plot and no reviews, so the reviews gate parks it with a
    thirty-day window - correctly, and "the drain completes what it leased" then has nothing to
    measure. And stage 2 with no fetcher factory builds the REAL one: five keyless sources times
    eight titles a tick, robots.txt and all, against Wikipedia, Wikidata, TVmaze, Rotten Tomatoes
    and Metacritic, from every machine that runs this suite. One test took 65 seconds doing it.
    `worker._acquisition_drain` takes no factory - it is the production call path, which is what
    this file insists on testing through - so the stand-down is here rather than at a call site.

    `test_acquire_pipeline.py` carries the same fixture and the same argument, and is where the
    three stages are asserted for real, against an `httpx.MockTransport`. No assertion in this
    file changes: the substitutes keep `paid`, `implemented` (stage 6's excepted, below) and
    `owner` and restore exactly the pipeline every test below was written against.

    STAGE 6 STANDS DOWN TOO SINCE M5.5, AS A DECLARED NO-OP (decision 432). It bills, and the
    driver's gate reads `implemented` first: kept True, a substitute would still be a paid stage to
    the gate, which would ask the meter, find no cap on a test database and park every task in this
    file at stage 6 under decision 348's sentence - so "the drain completes what it leased" would
    again have nothing to measure. `implemented=False` is what stage 6 WAS until this milestone,
    the one shape decision 348 says cannot spend, and the pipeline these tests were written
    against. Stage 6 through the driver is `test_llm_stage.py`'s subject.

    STAGES 5 AND 7 STAND DOWN TOO SINCE M5 GAVE THEM BODIES (decisions 461, 462). Every task here
    walks under an imported bundle, whose vocabulary a live stage 5 would build and file a pack
    under for each title - a raw-store write per task, which is not what a test of the worker job
    is about - and stage 7 records what stage 6 filed, which is nothing once 6 stands down.
    Neither is paid, so both keep `implemented=True`. Stage 8 stays live: a minted title has no
    keywords, so it projects no row and advances (decision 463).
    """
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


# --- the registry entry (backend, no DB) ---------------------------------------------------------


def test_the_drains_budget_is_bounded_by_the_lease_it_takes_and_by_its_own_batch():
    """§5.3's budget rule (`worker.py:987-1006`) reaches two constants this row does not own.

    `job.timeout <= job.every` is asserted for every live row by `test_worker_jobs.py`; what only
    this row needs is the OTHER bound. `queue.LEASE_SECONDS` is what lets `queue.complete` write
    without a fence, and that holds only while `_tick`'s `wait_for` cancels an attempt long before
    its lease can expire. A margin of two is the floor rather than the intent - the registry
    comment argues 7.5x - because at parity the reaper and the budget fire within one attempt of
    each other, and the row the reaper then hands to a second worker is one the first is still
    walking.

    The per-task share is the other half of the same arithmetic, and it is a claim about §5.3's
    own "<1 s/title" for the Cold Tower: a batch bound that left less than a second a task would
    be a budget that cannot pay for stage 9 even once.

    ONE TASK IS NOT ONE TITLE PLACED, and this floor should not be read as saying it is.
    `stages.place` calls `reconcile(scope="app_acquired")`, whose work list is every acquired
    title, so one task's stage 9 places the WHOLE acquired set and a tick's placement work is
    `DRAIN_LIMIT x |acquired|`. That is chosen and argued in `stages.place` - a per-title scope
    would be a fifth definition of "who needs placing" in `reconcile.py` - and it is cheap while
    that set is small, which it is on every install today. §5.3's "<1 s/title" is a ceiling rather
    than a measured cost, and the milestone that finds this budget binding is the one that owes
    the measurement, with §6.6's board in front of it. [M5.1 review cycle 1, M51-REV-07]
    """
    job = _job()
    assert job.run is worker._acquisition_drain
    assert job.milestone == "M5.1", "the column is the milestone of the WORK, and this is M5.1's"

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
    """Decision 347's owed measurement, as the two relations that make the number checkable.

    THE REGISTRY COMMENT NAMES THE MILESTONE THAT OWES THIS: "the rest of the headroom is for the
    milestone that gives a stage a fetch, which owes this line a measurement rather than a larger
    number". M5.3 is that milestone - stage 2 crawls eight hosts - and until this cycle the row
    still read "ms of declared no-ops", which is false by two orders of magnitude against a batch
    whose slowest host is paced at seven-tenths of a request a second.

    THE SECOND RELATION IS THE CHOICE THAT SAME PARAGRAPH SAID THIS BUDGET WOULD FORCE: "`fetch.py`
    already sleeps a `Retry-After` in process up to 300 s, so the first milestone to fetch inside a
    stage has to choose between that wait and a `queue.defer`". A budget at or under that ceiling
    is one a SINGLE 429 cancels, and `pipeline._walk_batch` deliberately keeps the cancelled
    in-flight task's charged attempt - so four such ticks close a perfectly good title `failed`
    with no retry surface before M5.6. The ceiling is read off the shipped function rather than
    retyped, so a policy change moves the assertion with it.

    NEITHER HALF IS A CEILING ON THE BUDGET; `test_the_drains_budget_is_bounded_by_the_lease_it_
    takes_and_by_its_own_batch` above is, and the two together are what decision 347 asked for.
    [M5.3 review cycle 1, M53-C1-NET-03]
    """
    job = _job()
    fetching = [stage for stage in _SHIPPED_STAGES if stage.implemented and stage.fetches]
    assert fetching, (
        "no implemented stage fetches, so this test is measuring a pipeline that does not exist"
    )
    assert "no-op" not in job.budget, (
        f"stages {[s.number for s in fetching]} fetch, and the budget still reads {job.budget!r}"
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


# --- one tick (Postgres) -------------------------------------------------------------------------


async def test_one_tick_leases_its_bound_and_completes_what_it_leased(worker_env, bundled, db):
    """The bound is `pipeline.DRAIN_LIMIT`, and it is a bound on the TICK and not on the queue.

    Nine tasks and not eight, because eight would be satisfied by a drain with no limit at all.
    The second tick is what says the ninth was left rather than lost: a queue whose overflow is
    dropped and a queue whose overflow waits look identical after one tick.

    `done` rather than the report alone, because `queue.complete` is the write that makes a task
    non-repeatable and the report is only this process's account of it.
    """
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
    """§8: "paid stages (6) never auto-retry past the spend cap", and this loop knows of no cap.

    The drain leases free work only - `queue.lease`'s default, and a WHERE-clause refusal rather
    than a filter applied after the row is taken. Asserted through the JOB because that is where
    the omission would be made: a future caller that passed `paid=True` from here would be the
    one change able to bill a household from a cron, and it would pass every test in
    `test_acquire_queue.py`.

    `attempts` is the assertion with teeth. A task leased and then put back is `pending` again,
    so state alone cannot tell "never taken" from "taken and returned" - and only the first of
    those is a promise about money.
    """
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
    """A `kill -9` mid-lease, and the one recovery that survives it.

    Nothing writes a terminal state when a worker dies - `_tick` bounds every job with
    `asyncio.wait_for`, which CANCELS, and a `CancelledError` is not an `Exception`, so no handler
    runs. The row left behind is `leased`, owned, past its expiry, and only `queue.reclaim_expired`
    will ever touch it again. The death is simulated by expiring the lease in place because no
    test can wait out `LEASE_SECONDS`, and `freezegun` cannot move Postgres's `now()`.

    EXACTLY ONCE is asserted on the rows and not on the report: one title, one board row, one
    placement in the active basis. "Completed twice" and "completed once" produce the same
    `DrainReport`, and differ only in what the second pass wrote - which is the whole of decision
    162's one-way property, since a duplicate mint above 1e9 can only be undone by dropping the
    database.
    """
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
    """The reason the basis is asked for here as well as inside stage 9.

    An `artifact_bundle` row that is active while its directory is gone is a broken install and
    not a bundle-less one (M4.13 data-03). Left to stage 9, every task in the batch would raise,
    `_run_stage` would turn each raise into `queue.fail`, and an unmounted volume would spend
    `max_attempts` on eight acquisitions a tick - closing them permanently for a fault that is
    the install's and repairable in a minute.

    So the assertions are about what did NOT happen: the task is untouched, its attempt counter is
    still zero, and nothing was minted. The job raising is the least interesting half - `_tick`
    logs it, writes an `ok = false` row and re-arms at `RETRY_AFTER`, which is what every other
    model job does with the same two refusals.
    """
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
    """This row fires 48 times a day and a household acquires a film a week, so the empty tick is
    the one that runs almost every time.

    Cheap is asserted as "the basis was not opened", which is the only part of this job that
    leaves the database: `ArtifactStore.load_active` reads a manifest and stats the bundle's files.
    The saving is small; what it buys is the log. A broken install with an empty queue would
    otherwise put a traceback in `docker compose logs worker` every `RETRY_AFTER` for ever, and
    §6.6 names that log as the operator's data.

    `None` and not an empty report, because `_tick` stores the return in `job_run.detail` and a
    row per tick saying nothing happened is the noise `JOB_RUN_KEEP_DAYS` is sized against.

    THE BOARD IS NOT EMPTY WHEN THIS TEST STARTS, and that is the state a real install is in
    rather than an accident of the fixture: `placement/reconcile._park_thin` has written
    `(stage = 2, status = 'parked')` rows for every thin-but-placed title since M4.13, and the
    import in `bundled` runs it. Those rows are the pipeline's inbox (decision 336), nothing has
    enqueued a task for them, and an empty tick has to leave them exactly as it found them -
    asserted as a snapshot, because "no row was written" and "three rows were rewritten with the
    same values" are the same count.
    """
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

    # A PENDING PAID TASK IS NOT WORK THIS TICK WOULD TAKE, and `queue.pending_count` counted it.
    # `pipeline.drain` leases `paid = false` (decision 348's "a generic drain never leases a paid
    # task"), so a paid row made the queue look non-empty to the one caller asking "is there work
    # I will take" - and on the M4.13 data-03 install this paragraph is about, that is the
    # traceback every `RETRY_AFTER` for ever, for a batch the drain leases nothing from.
    # [M5.1 review cycle 2, M51-C2-PAID-02]
    assert await queue.enqueue(db, pipeline.TASK_KIND, "tmdb:paid-1", paid=True) is True
    assert await _job().run() is None
    assert opened == [], "a paid task this drain cannot lease made the tick ask for the basis"

    # ...and the guard is not simply never calling it: one task makes the same tick ask.
    assert await pipeline.enqueue_item(db, _item(1)) is True
    assert await _job().run() is not None
    assert opened == ["load"]


async def test_the_job_writes_a_job_run_row_like_every_other_job(worker_env, bundled, db, monkeypatch):
    """§6.6's "job health" is the `job_run` table, and a job outside it is a job nobody can see.

    Run through `_tick` rather than by calling `run()`, because the row is the LOOP's write and
    not the job's: `_record_start` opens it, `_record_finish` closes it with whatever the job
    returned. A registry substituted down to this one row is how `test_worker_jobs.py` isolates a
    tick, and it is honest here for the same reason - `_with_queued_import` reads `JOBS` for the
    import row and finds none, so nothing else fires.

    The detail is asserted as well as the row, because that is where an operator reads what the
    tick actually did; a `job_run` row with a null detail would satisfy "the job ran" and answer
    nothing.
    """
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


# --- review cycle 2: the budget expiring is not a worker dying ----------------------------------


async def test_a_drain_cancelled_at_its_budget_hands_back_what_it_had_not_started(
    worker_env, bundled, db, monkeypatch
):
    """`_tick` bounds this job with `asyncio.wait_for`, which CANCELS. The batch paid for that.

    `queue.lease` claims the whole batch in ONE statement and counts the attempt ON THE CLAIM, so
    a cancellation mid-loop left every task ordered after the one in flight `leased`, never run,
    and one attempt poorer - invisible for `LEASE_SECONDS`, reclaimed on the next tick, leased
    again, cancelled again. On the fourth such tick `reclaim_expired` closes all of them for good
    with `queue.ABANDONED`: "a worker was stopped, killed or abandoned this task. Nothing will
    lease it again." Nothing was stopped or killed; this job's own budget expired. And
    `queue.enqueue` is `ON CONFLICT (kind, key) DO NOTHING`, so no sweep can re-enqueue them and
    decision 330 defers the revive lever to M5.6 - a batch of acquisitions lost to the worker's
    own clock with no operator action available.

    That is the same collateral review cycle 1 removed for a RAISE (M51-REV-01), through the exit
    that fix could not cover: a `CancelledError` is not an `Exception`. The corpus has the arm -
    `mdc/runner.py:200-202` - and the port took its neighbours and not it.

    THE TASK IN FLIGHT KEEPS ITS ATTEMPT, which is the half that is not a refund. It ran, and it
    is the one that ate the budget, so `max_attempts` still bounds a stage that can never finish
    inside one tick - the failure `queue.py`'s change 6 exists to prevent. Reproduced through
    `wait_for` rather than by raising `CancelledError` by hand, because the budget is the thing
    under test and `_tick` is where it is spent. [M5.1 review cycle 2, port-CANCEL-01]
    """
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


# --- review cycle 3: the half of the reaper the refusal used to skip ----------------------------


async def test_a_broken_install_still_corrects_the_board_the_reaper_closed(worker_env, bundled, db):
    """The batch refusal is above `pipeline.drain`, and half the reaper is below it.

    `_acquisition_drain` calls `queue.reclaim_expired` itself - M5.1 review cycle 2 moved it there
    so the count that decides the refusal sees reclaimed work - and then asks for the basis, which
    RAISES on an M4.13 data-03 install. `close_abandoned_boards` and `complete_landed_boards` live
    inside `pipeline.drain`, one line further down, and are the only things in the tree that can
    move a board row out of `running`. So every tick of a broken install closed the TASK for good
    and left §6.6's board reading `running` with `reason = NULL` - the operator-facing lie
    `M51-CRASH-03` was filed to remove, re-entered one layer up by the fix for a different defect,
    and standing for exactly as long as the install stays broken, which is the period an operator
    is reading that board.

    The two halves are one reaper, so they are called together: the board correction is pure SQL
    over two tables, needs no bundle and no `ArtifactStore`, and is therefore safe on precisely the
    install where the basis is not. `pipeline.drain` calls them again when it is reached, which
    costs a no-op. [M5.1 review cycle 3, M51-C3-CRASH-03]
    """
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
    # Something plainly due, so the batch refusal fires at all: it is asked for only when the
    # queue has work.
    assert await pipeline.enqueue_item(db, _item(2)) is True

    # And the install breaks under it: the bundle row stays active while its directory is gone.
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


# --- M5 review cycle 1: the tick's run reaches the walk -----------------------------------------


async def test_the_ticks_run_is_the_run_stage_six_files_under_and_stage_seven_reads(
    worker_env, bundled, db, monkeypatch
):
    """Decision 468: the `job_run` row `_tick` opens for the drain is the run its walks carry.

    Stage 7 records the refusals THIS run filed (decision 462), with `run_id = $2` so that a walk
    with no run reads none. The worker's drain handed `pipeline.drain` no run, so on the one path a
    household runs stage 6 filed every refusal under none and stage 7 wrote `rejected: {}` for the
    life of the job - the board saying the trust boundary dropped nothing while `dna_reject` held
    what it dropped - and every walk that proved otherwise (the exit scripts, the stage tests)
    carried a run its test had made, which the worker never did.

    Driven through `_tick`, because the run is the LOOP's row and the claim is that the walk the
    loop fires carries it. Stage 6 stands in with the one write the real one makes for a refusal -
    `verify.record_rejects` under the run the driver handed it, as `llm/extract` files it with the
    `run_id` `stages.dna_extract` passes - so no provider is needed to see the seam; stage 7 is the
    shipped body. [M5 review cycle 1, M5-DNA-01]
    """
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
