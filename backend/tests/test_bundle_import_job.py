"""The bundle import as a job, and the path boundary in front of it (§5.3, §10, decision 253).
The `_resolve` cases need no database; the rest need TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException

from spielplan import worker
from spielplan.api import artifacts as artifacts_api
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import
from spielplan.models.artifacts import ArtifactStore
from tests.fixtures import make_bundle as fx
from tests.helpers import ADMIN_PASSWORD, admin_client, tick_one


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """The chdir keeps a developer's `.env` out of reach, as `no_secrets_key` does."""
    root = tmp_path / "data"
    (root / "import").mkdir(parents=True)
    (root / "artifacts").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    settings.cache_clear()
    yield root
    settings.cache_clear()


def test_a_sibling_directory_sharing_the_data_dir_prefix_is_refused(data_dir, tmp_path):
    """A string prefix admitted `/database` and `/data.bak`;
    the admitted case is asserted beside the refused."""
    sibling = tmp_path / "data-other"
    (sibling / "bundle").mkdir(parents=True)

    with pytest.raises(HTTPException) as refused:
        artifacts_api._resolve(str(sibling / "bundle"))

    assert refused.value.status_code == 400
    assert "must live under" in str(refused.value.detail)

    inside = data_dir / "import" / "v1"
    inside.mkdir()
    assert artifacts_api._resolve(str(inside)) == inside.resolve()


def test_a_target_under_the_artifacts_root_is_refused(data_dir):
    """`import_bundle` rmtree's `artifacts_root / version`
    first, deleting a bundle stored there by its own import."""
    staged = data_dir / "artifacts" / "v20260828"
    staged.mkdir()

    with pytest.raises(HTTPException) as refused:
        artifacts_api._resolve(str(staged))

    assert refused.value.status_code == 400
    detail = str(refused.value.detail)
    assert str(data_dir / "artifacts") in detail
    assert str(data_dir / "import") in detail, (
        f"the refusal has to name the directory a bundle DOES belong in: {detail}"
    )

    # The root itself, not only a version inside it.
    with pytest.raises(HTTPException) as root_refused:
        artifacts_api._resolve(str(data_dir / "artifacts"))
    assert root_refused.value.status_code == 400


def test_a_regular_file_whose_suffix_is_not_an_archive_is_refused(data_dir):
    """Every non-directory went to `tarfile.open`; a directory, `.tar` and `.tar.zst` stay accepted."""
    readme = data_dir / "import" / "README.txt"
    readme.write_text("copy your bundle here", encoding="utf-8")

    with pytest.raises(HTTPException) as refused:
        artifacts_api._resolve(str(readme))

    assert refused.value.status_code == 400
    assert "a directory, a .tar or a .tar.zst" in str(refused.value.detail)

    for name in ("v1.tar", "v1.tar.zst"):
        archive = data_dir / "import" / name
        archive.write_bytes(b"not really a tar, and that is the next guard's question")
        assert artifacts_api._resolve(str(archive)) == archive.resolve()


def test_a_path_that_is_not_there_is_a_four_oh_four_and_not_a_four_hundred(data_dir):
    """Missing, not forbidden: different things to an operator who mistyped a version."""
    with pytest.raises(HTTPException) as missing:
        artifacts_api._resolve(str(data_dir / "import" / "v20260829"))
    assert missing.value.status_code == 404


async def test_an_archive_that_will_not_open_is_a_four_hundred_the_data_tab_can_render(
    data_dir,
):
    """Not zero bytes: `tarfile.is_tarfile` calls those a
    valid empty archive, refused later by another guard."""
    archive = data_dir / "import" / "v1.tar"
    archive.write_bytes(b"not a tar at all " * 256)

    with pytest.raises(HTTPException) as refused:
        # Awaited because `_open` runs `Bundle.open` in a thread (decision 287).
        await artifacts_api._open(artifacts_api._resolve(str(archive)))

    assert refused.value.status_code == 400
    assert "not a tar archive" in str(refused.value.detail)
    assert not list(archive.parent.glob(".unpack*")), (
        "a refused archive must leave no tree behind for the retry to reuse"
    )


def test_the_route_and_the_registry_name_one_job_and_one_set_of_phases():
    """`api/artifacts.py` may not import `spielplan.worker`,
    whose import reconfigures logging; a test can import both."""
    assert artifacts_api.IMPORT_JOB == worker.BUNDLE_IMPORT_JOB
    assert artifacts_api.QUEUED == worker.PHASE_QUEUED
    assert artifacts_api.RUNNING == worker.PHASE_RUNNING
    # A budget drifted from the worker's would either wedge the queue or re-open a live import.
    assert artifacts_api.IMPORT_CLAIM_BUDGET_S == worker.BUNDLE_IMPORT_TIMEOUT

    row = next((j for j in worker.JOBS if j.name == worker.BUNDLE_IMPORT_JOB), None)
    assert row is not None, (
        "the route enqueues under a name this loop does not run: section 5.3's ninth row is "
        "the job"
    )

    # `PHASE_FAILED`'s only reader is the frontend map, so the map is read to keep the phases in step.
    assert _phase_keys_of_the_screen() == {
        worker.PHASE_QUEUED, worker.PHASE_RUNNING, worker.PHASE_ACTIVE, worker.PHASE_FAILED,
    }


def _phase_keys_of_the_screen() -> set[str]:
    """The keys of `bundleImport.svelte.js`'s `PHASE_OF_JOB`, the one reader outside this package."""
    module = (
        Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "lib" / "bundleImport.svelte.js"
    )
    block = re.search(r"PHASE_OF_JOB\s*=\s*\{(.*?)\}", module.read_text(encoding="utf-8"), re.S)
    assert block, "no `PHASE_OF_JOB = {...}` in bundleImport.svelte.js to read the phases from"
    return set(re.findall(r"(\w+)\s*:", block.group(1)))


@pytest.fixture
def bundle_at(tmp_path):
    """`tmp_path` IS DATA_DIR under the `app` fixture, so `import/` here is `settings().import_dir`."""
    def make(version: str = "test-v1") -> Path:
        return fx.make_bundle(tmp_path / "import" / version, version=version)
    return make


async def _job_row(db, version: str | None = None):
    return await db.fetchrow(
        "SELECT id, name, ok, finished_at, detail FROM job_run "
        " WHERE name = $1 AND detail ? 'phase' "
        "   AND ($2::text IS NULL OR detail->>'bundle_version' = $2) "
        " ORDER BY started_at DESC, id DESC LIMIT 1",
        worker.BUNDLE_IMPORT_JOB, version,
    )


async def test_the_import_route_returns_202_and_the_version_to_poll(app, db, bundle_at):
    """The import was a 127 s await on the event loop; now
    202, and NOTHING is written until the worker runs."""
    admin = await admin_client(app)
    root = bundle_at()

    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})

    assert answer.status_code == 202, answer.text
    payload = answer.json()
    assert payload["bundle_version"] == "test-v1"
    assert payload["phase"] == "queued"
    assert isinstance(payload["job_id"], int)
    assert payload["report"]["ok"] is True
    assert payload["poll"] == "/api/admin/bundle/state"

    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0, (
        "the 202 wrote a bundle row: decision 253 says the row is written inside the import "
        "transaction and nowhere else"
    )
    assert not (settings().artifacts_dir / "test-v1").exists(), "the 202 staged the artifacts"

    queued = await _job_row(db)
    assert queued["id"] == payload["job_id"]
    assert queued["detail"]["phase"] == "queued"
    assert queued["detail"]["path"] == str(root.resolve())
    assert queued["finished_at"] is None and queued["ok"] is None


async def test_the_load_and_the_flip_run_in_the_worker_tick(app, db, bundle_at):
    """`due` can only ask a clock, so the import happening proves `_with_queued_import` asked."""
    admin = await admin_client(app)
    root = bundle_at()
    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text

    now = time.monotonic()
    assert worker.BUNDLE_IMPORT_JOB not in {
        job.name for job in worker.due(now, {worker.BUNDLE_IMPORT_JOB: now})
    }, "this test proves the admin action fires the job, so the clock must not"
    await tick_one(worker.BUNDLE_IMPORT_JOB, due=False)

    row = await db.fetchrow("SELECT version, state, report FROM artifact_bundle")
    assert row is not None, "the worker tick did not import the queued bundle"
    assert (row["version"], row["state"]) == ("test-v1", "active")
    assert (settings().artifacts_dir / "test-v1").is_dir(), (
        "section 10 step 2 staged nothing"
    )
    # §10's rebuild set runs inside the flip's transaction, so the stored report carries it.
    rules = {finding["rule"] for finding in row["report"]["findings"]}
    assert {"stage", "rebuild", "swap", "rebuild-set"} <= rules, sorted(rules)

    done = await _job_row(db, "test-v1")
    assert done["detail"]["phase"] == "active"
    assert done["ok"] is True and done["finished_at"] is not None
    assert done["detail"]["report"]["ok"] is True


async def test_the_state_route_reports_the_phase_until_the_bundle_is_active(
    app, db, bundle_at, monkeypatch
):
    """The import is paused inside its transaction so the
    `running` phase is read from another connection."""
    admin = await admin_client(app)
    root = bundle_at()

    inside = asyncio.Event()
    resume = asyncio.Event()
    real = bundle_import.import_bundle

    async def paused(conn, bundle, artifacts_root, **kwargs):
        inside.set()
        await resume.wait()
        return await real(conn, bundle, artifacts_root, **kwargs)

    monkeypatch.setattr(bundle_import, "import_bundle", paused)

    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text

    queued = (await admin.get("/api/admin/bundle/state")).json()
    assert queued["import_job"]["phase"] == "queued"
    assert queued["import_job"]["bundle_version"] == "test-v1"
    assert queued["active"] is None and queued["restart_required"] is False

    tick = asyncio.create_task(tick_one(worker.BUNDLE_IMPORT_JOB, due=False))
    try:
        await asyncio.wait_for(inside.wait(), 20)
        running = (await admin.get("/api/admin/bundle/state")).json()
        assert running["import_job"]["phase"] == "running", (
            "the phase a poll reads while the import runs is the whole of what a 202 promises"
        )
        assert running["active"] is None, "nothing is flipped until the transaction commits"
    finally:
        resume.set()
        await asyncio.wait_for(tick, 120)

    done = (await admin.get("/api/admin/bundle/state")).json()
    assert done["import_job"]["phase"] == "active"
    assert done["import_job"]["ok"] is True
    assert done["import_job"]["report"]["ok"] is True
    assert done["active"] == "test-v1"
    # Decision 497: the read that reports the flip is the read that loads it; no restart is asked.
    assert done["restart_required"] is False
    assert done["loaded"]["version"] == "test-v1"


async def test_a_client_that_disconnects_does_not_change_the_outcome(app, db, bundle_at):
    """The proxy cuts at 100 s and the work takes minutes,
    so a disconnected client is the expected shape."""
    admin = await admin_client(app)
    root = bundle_at()
    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0

    await admin.aclose()
    await tick_one(worker.BUNDLE_IMPORT_JOB, due=False)

    assert await db.fetchval("SELECT state FROM artifact_bundle WHERE version = 'test-v1'") == (
        "active"
    )
    later = app()
    signed_in = await later.post(
        "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    state = (await later.get("/api/admin/bundle/state")).json()
    assert state["import_job"]["phase"] == "active"
    assert state["import_job"]["report"]["ok"] is True, (
        "the report of an import nobody was watching is still readable afterwards"
    )


async def test_a_second_import_while_one_is_queued_is_refused_rather_than_queued_twice(
    app, db, bundle_at
):
    """A running import holds `IMPORT_LOCK`; a queued row holds none, so both are asked, under the lock."""
    admin = await admin_client(app)
    root = bundle_at()
    first = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert first.status_code == 202, first.text

    second = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert second.status_code == 409, second.text
    assert "already queued" in second.json()["detail"]
    assert await db.fetchval(
        "SELECT count(*) FROM job_run WHERE name = $1 AND detail ? 'phase'",
        worker.BUNDLE_IMPORT_JOB,
    ) == 1

    # The lock held by another session, exactly as a worker mid-import holds it.
    other = await asyncpg.connect(settings().database_url)
    try:
        assert await other.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", bundle_import.IMPORT_LOCK
        )
        refused = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
        assert refused.status_code == 409, refused.text
        assert "already running" in refused.json()["detail"]
    finally:
        await other.close()


# Derived from the budget: a literal broke four tests when the budget moved.
ABANDONED_S = int(worker.BUNDLE_IMPORT_TIMEOUT) + 100


async def _queue(db, version: str, *, phase: str, age_s: int, claimed_s: int | None = None):
    """`started_at` is the enqueue instant, `claimed_at` when
    a loop took it; the reaper turns on the difference."""
    detail = {"phase": phase, "bundle_version": version, "path": "/data/import/" + version}
    row = await db.fetchrow(
        "INSERT INTO job_run (name, started_at, detail) "
        "VALUES ($1, now() - ($2::float8 * interval '1 second'), $3) RETURNING id",
        worker.BUNDLE_IMPORT_JOB, float(age_s), detail,
    )
    if claimed_s is not None:
        await db.execute(
            "UPDATE job_run SET detail = jsonb_set(detail, '{claimed_at}', "
            "to_jsonb(now() - ($2::float8 * interval '1 second'))) WHERE id = $1",
            row["id"], float(claimed_s),
        )
    return row["id"]


async def test_the_reaper_reports_the_flip_it_can_see_rather_than_asserting_nothing_was_written(
    app, db, bundle_at
):
    """A crash after COMMIT leaves the bundle active; the
    reaper reads `artifact_bundle` and says what it saw."""
    admin = await admin_client(app)
    accepted = await admin.post("/api/admin/bundle/import", json={"path": str(bundle_at())})
    assert accepted.status_code == 202, accepted.text
    await tick_one(worker.BUNDLE_IMPORT_JOB, due=False)
    assert await db.fetchval("SELECT state FROM artifact_bundle WHERE version = 'test-v1'") == (
        "active"
    )

    # The kill: a claim past the budget for the import that has already flipped.
    killed = await _queue(db, "test-v1", phase=worker.PHASE_RUNNING,
                          age_s=ABANDONED_S, claimed_s=ABANDONED_S)
    await worker._reap_abandoned_import(db)

    row = await db.fetchrow("SELECT ok, finished_at, detail FROM job_run WHERE id = $1", killed)
    assert row["finished_at"] is not None, "the claim was left open for the Data tab to poll"
    assert row["ok"] is True, row["detail"]["text"]
    text = row["detail"]["text"]
    assert "Nothing is half-written" not in text, text
    assert "active" in text and "restart" in text, text

    # And with no flip to find, the failure states what it observed and nothing more.
    orphan = await _queue(db, "test-v9", phase=worker.PHASE_RUNNING,
                          age_s=ABANDONED_S, claimed_s=ABANDONED_S)
    await worker._reap_abandoned_import(db)
    lost = await db.fetchrow("SELECT ok, detail FROM job_run WHERE id = $1", orphan)
    assert lost["ok"] is False
    assert "Nothing is half-written" not in lost["detail"]["text"]
    assert "no process reported" in lost["detail"]["text"], lost["detail"]["text"]


async def test_a_crash_after_the_flip_is_reported_as_the_flip_and_not_as_a_failed_import(
    app, db, bundle_at, monkeypatch
):
    """A connection lost after COMMIT raises out of an import
    that happened; one derivation serves both writers."""
    admin = await admin_client(app)
    accepted = await admin.post("/api/admin/bundle/import", json={"path": str(bundle_at())})
    assert accepted.status_code == 202, accepted.text
    real_import = bundle_import.import_bundle

    async def commits_then_loses_the_connection(*args, **kwargs):
        await real_import(*args, **kwargs)
        raise asyncpg.ConnectionDoesNotExistError(
            "connection was closed in the middle of operation"
        )

    monkeypatch.setattr(bundle_import, "import_bundle", commits_then_loses_the_connection)
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        await worker._bundle_import()

    assert await db.fetchval("SELECT state FROM artifact_bundle WHERE version = 'test-v1'") == (
        "active"
    ), "the import did not commit, so this test is not measuring what it says it is"
    row = await _job_row(db, "test-v1")
    assert row["ok"] is True, row["detail"]["text"]
    assert row["detail"]["phase"] == worker.PHASE_ACTIVE, row["detail"]
    assert "this import committed" in row["detail"]["text"], row["detail"]["text"]
    assert "did not run to a report" not in row["detail"]["text"], row["detail"]["text"]
    assert "restart" in row["detail"]["text"], row["detail"]["text"]

    # A claim whose bundle is not there never flipped anything, and stays a failed import.
    monkeypatch.undo()
    await _queue(db, "test-v9", phase=worker.PHASE_QUEUED, age_s=0)
    with pytest.raises(bundle_import.BundleOpenError):
        await worker._bundle_import()

    lost = await _job_row(db, "test-v9")
    assert lost["ok"] is False
    assert lost["detail"]["phase"] == worker.PHASE_FAILED, lost["detail"]
    assert "did not run to a report" in lost["detail"]["text"], lost["detail"]["text"]


async def test_an_abandoned_claim_is_reaped_on_a_tick_and_not_only_by_the_hourly_fallback(
    app, db, bundle_at
):
    """After a restart `due` will not fire the job for up
    to an hour, so the tick must reap abandoned claims."""
    killed = await _queue(db, "test-v9", phase=worker.PHASE_RUNNING,
                          age_s=ABANDONED_S, claimed_s=ABANDONED_S)
    now = time.monotonic()
    for age in (60.0, 300.0, 1800.0, 3500.0):
        assert worker.BUNDLE_IMPORT_JOB not in {
            job.name for job in worker.due(now, {worker.BUNDLE_IMPORT_JOB: now - age})
        }, f"the fallback fired at {age:g}s, so this test would prove nothing"

    await tick_one(worker.BUNDLE_IMPORT_JOB, due=False)

    row = await db.fetchrow("SELECT ok, finished_at, detail FROM job_run WHERE id = $1", killed)
    assert row["finished_at"] is not None, (
        "a claim no process will finish stayed open on a tick that could have closed it"
    )
    assert row["ok"] is False
    assert "no process reported" in row["detail"]["text"], row["detail"]["text"]


async def test_the_import_leads_the_tick_even_when_the_clock_produced_it(app, db, bundle_at):
    """`due` keeps registry order, so once the fallback elapses the sweep would lead; the import must."""
    await _queue(db, "test-v1", phase=worker.PHASE_QUEUED, age_s=1)
    # A box switched on after its night window, which is the tick a restart produces.
    ready = worker.due(
        time.monotonic(), {}, local=datetime(2026, 1, 1, 23, 0, tzinfo=UTC), last_date={}
    )
    names = [j.name for j in ready]
    assert names.index("placement-reconciliation") < names.index(worker.BUNDLE_IMPORT_JOB), (
        "registry order no longer puts the sweep first, so this test asserts nothing"
    )

    ordered = [job.name for job in await worker._with_queued_import(ready)]

    assert ordered[0] == worker.BUNDLE_IMPORT_JOB, ordered
    assert ordered.count(worker.BUNDLE_IMPORT_JOB) == 1, ordered
    assert sorted(ordered) == sorted(j.name for j in ready), "a job left the tick"


async def test_the_in_flight_probe_reads_the_lock_and_never_takes_it(app, db, monkeypatch):
    """A try-lock-and-release probe makes a real writer's try-lock fail; the probe must take nothing."""
    import contextlib

    statements: list[str] = []

    class _Spy:
        async def fetchval(self, sql, *args):
            statements.append(sql)
            return await db.fetchval(sql, *args)

        async def execute(self, sql, *args):
            statements.append(sql)
            return await db.execute(sql, *args)

    @contextlib.asynccontextmanager
    async def acquire():
        yield _Spy()

    monkeypatch.setattr(worker.pool, "acquire", acquire)

    assert await worker._import_in_flight() is False

    held = await asyncpg.connect(settings().database_url)
    try:
        assert await held.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", bundle_import.IMPORT_LOCK
        )
        assert await worker._import_in_flight() is True
    finally:
        await held.close()

    assert await worker._import_in_flight() is False
    took = [s for s in statements if "advisory_lock" in s]
    assert not took, f"the probe took the lock it was asking about: {took}"


async def test_a_claim_is_reaped_from_when_it_was_claimed_and_not_from_when_it_was_queued(
    app, db, bundle_at
):
    """The budget runs from the claim, not the enqueue, or a queued-behind-backup import is reaped live."""
    queued = await _queue(db, "test-v1", phase=worker.PHASE_QUEUED, age_s=ABANDONED_S)

    claimed = await worker._claim_bundle_import(db)
    assert claimed["id"] == queued
    await worker._reap_abandoned_import(db)

    row = await db.fetchrow("SELECT finished_at, detail FROM job_run WHERE id = $1", queued)
    assert row["finished_at"] is None, (
        "a claim made a moment ago was reaped on the strength of when it was ENQUEUED"
    )
    assert row["detail"]["phase"] == worker.PHASE_RUNNING

    # Aged past the budget from the CLAIM, it is the reaper's after all.
    await db.execute(
        "UPDATE job_run SET detail = jsonb_set(detail, '{claimed_at}', "
        "to_jsonb(now() - ($2::float8 * interval '1 second'))) WHERE id = $1",
        queued, float(ABANDONED_S),
    )
    await worker._reap_abandoned_import(db)
    assert await db.fetchval("SELECT finished_at FROM job_run WHERE id = $1", queued) is not None


async def test_a_second_import_in_the_window_between_the_claim_and_the_lock_is_refused(
    app, db, bundle_at
):
    """Between the claim and `IMPORT_LOCK` neither question
    is true, so a young `running` claim refuses too."""
    admin = await admin_client(app)
    root = bundle_at()
    await _queue(db, "test-v1", phase=worker.PHASE_QUEUED, age_s=1)
    claimed = await worker._claim_bundle_import(db)
    assert claimed is not None

    refused = await admin.post("/api/admin/bundle/import", json={"path": str(root)})

    assert refused.status_code == 409, refused.text
    assert "already running" in refused.json()["detail"]
    assert await db.fetchval(
        "SELECT count(*) FROM job_run WHERE name = $1 AND detail ? 'phase'",
        worker.BUNDLE_IMPORT_JOB,
    ) == 1, "a second row was queued behind an import that had already been claimed"

    # A claim older than the budget is the reaper's, and the operator's retry is accepted.
    await db.execute(
        "UPDATE job_run SET detail = jsonb_set(detail, '{claimed_at}', "
        "to_jsonb(now() - ($2::float8 * interval '1 second'))) WHERE id = $1",
        claimed["id"], float(ABANDONED_S),
    )
    accepted = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert accepted.status_code == 202, accepted.text


async def test_the_worker_model_jobs_do_not_write_across_the_flip(app, db, monkeypatch):
    """§10: a model job writing across the flip stamps a version the install no longer serves."""
    ran: list[str] = []

    def spy(name: str):
        async def run() -> dict[str, object] | None:
            ran.append(name)
            return None
        return run

    fitting = worker.Job("fold-in-tick", spy("fold-in-tick"), every=60, timeout=55)
    other = worker.Job("session-prune", spy("session-prune"), every=3600, timeout=60)
    monkeypatch.setattr(worker, "JOBS", (fitting, other))

    held = await asyncpg.connect(settings().database_url)
    try:
        assert await held.fetchval(
            "SELECT pg_try_advisory_lock(hashtext($1))", bundle_import.IMPORT_LOCK
        )
        await worker._tick(time.monotonic(), datetime.now(UTC), {}, {})
        assert ran == ["session-prune"], (
            "a job that fits against the active bundle ran while an import held the lock: "
            f"{ran}"
        )
    finally:
        await held.close()

    ran.clear()
    await worker._tick(time.monotonic(), datetime.now(UTC), {}, {})
    assert ran == ["fold-in-tick", "session-prune"], (
        f"the skip outlived the import that justified it: {ran}"
    )


async def test_the_jobs_that_cite_a_legal_no_bundle_state_never_meet_a_broken_one(
    app, db, caplog
):
    """`_active_store` asks `assert_not_broken` before mapping
    an empty store to None, so the skip line is honest."""
    import logging

    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ('v-gone', '{}'::jsonb, 'active', 'seed')"
    )

    with (
        caplog.at_level(logging.INFO, logger="spielplan.worker"),
        pytest.raises(RuntimeError) as refused,
    ):
        await worker._fold_in_tick()

    assert "does not exist" in str(refused.value)
    assert "section 3.1" not in caplog.text and "\u00a73.1" not in caplog.text, (
        f"a broken install was told its state is legal: {caplog.text}"
    )


async def test_a_missing_artifacts_directory_is_reported_and_restart_required_is_not(app, db):
    """Broken and stale at once: a restart reloads the same
    empty store, so restore is the only instruction."""
    admin = await admin_client(app)
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ('v-gone', '{}'::jsonb, 'active', 'seed')"
    )
    application = admin._transport.app
    application.state.artifacts = await ArtifactStore.load_active(db, settings().artifacts_dir)
    assert application.state.artifacts.broken is True

    state = (await admin.get("/api/admin/bundle/state")).json()

    assert state["active"] == "v-gone"
    assert state["loaded"] is None
    assert state["broken"] is True
    assert state["missing_path"] == str(settings().artifacts_dir / "v-gone")
    assert state["restart_required"] is False, (
        "a restart reloads the same empty store: the instruction for a broken install is the "
        "restore, and dd01 is that the page gave the other one"
    )

    # Now a newer bundle is active while this process still holds the broken one.
    await db.execute("UPDATE artifact_bundle SET state = 'superseded' WHERE version = 'v-gone'")
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ('v-next', '{}'::jsonb, 'active', 'model')"
    )

    both = (await admin.get("/api/admin/bundle/state")).json()

    assert both["active"] == "v-next"
    assert both["broken"] is True
    assert both["restart_required"] is False, (
        "the restart banner and the restore banner rendered together, telling one operator to do "
        "two things of which only one can help (decision 258)"
    )
