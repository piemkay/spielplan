"""The bundle import as a job, and the path boundary in front of it.

Spec v2.1 §5.3 (the jobs table and its "Bundle import validation + hot swap | admin action |
minutes" row), §10 (the swap sequence), §6.6 (the Data tab), §3.1 (a bundle-less install is
legal); decisions 253 and 258.

Two layers in one file, because they are two halves of one route. The `_resolve` cases need no
database at all - they are a function over a path and a `Settings` - and they are written that
way on purpose, since the boundary they assert is what stops a request from reading a directory
this app has no business in, and a test that needs a Postgres to say so would not run in the
place that matters. Everything else needs an install: what a 202 leaves unwritten, what the
worker's tick then writes, and what `GET /api/admin/bundle/state` says in between.

Skipped without TEST_DATABASE_URL; see tests/conftest.py. The `_resolve` cases run either way.
"""

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

ADMIN_PASSWORD = "an-admin-password"


# --- the path boundary (no database) ----------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """A DATA_DIR with the two directories §2 gives it, and no `.env` in reach.

    The chdir is `no_secrets_key`'s, for its reason: `Settings.model_config` reads `.env` from the
    working directory, and a developer who followed the README has one. A boundary test that read
    the household's real DATA_DIR would be asserting something about that machine.
    """
    root = tmp_path / "data"
    (root / "import").mkdir(parents=True)
    (root / "artifacts").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    settings.cache_clear()
    yield root
    settings.cache_clear()


def test_a_sibling_directory_sharing_the_data_dir_prefix_is_refused(data_dir, tmp_path):
    """finding 2.7: the boundary was a string prefix under a comment promising a path.

    `str(target).startswith(str(cfg.data_dir.resolve()))` admits every sibling whose name begins
    with DATA_DIR's own characters, so the shipped `DATA_DIR=/data` admits `/database` and
    `/data.bak`. Reproduced end to end with an admin session before this: validate and import of
    a bundle under `<DATA_DIR>-other` both answered 200 and the bundle was flipped active.

    The admitted case is asserted beside the refused one, because a boundary that refuses
    everything passes the first half of this test and breaks the Data tab.
    """
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
    """finding 2.8: a bundle under `/data/artifacts/<version>/` is deleted by its own import.

    `import_bundle` rmtree's `artifacts_root / version` before it copies, so for a bundle sitting
    at exactly that path - the one directory the Data tab names by version - `content.sqlite`,
    `reviews.sqlite`, `BUNDLE.json` and `artifacts/` are removed, `copytree` then raises
    `FileNotFoundError`, and the gigabyte is gone with validation already passed.

    The refusal names where a bundle belongs, because "refused" without that is a dead end for an
    operator who has just put a restored bundle in the only directory this app shows them.
    """
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
    """finding 2.5's front door: every non-directory was handed to `tarfile.open`.

    `README.txt` as the path therefore raised a `ReadError` that the app answered with a 500 - a
    bare string on the Data tab - left an empty `.unpacked-README/` behind, and had its SECOND
    attempt diagnosed as "this is a models-only bundle and the install has no content", which is
    a decision-162 refusal for a typo.

    Both archive suffixes are asserted as accepted, and a directory too: the guard is about what
    a bundle IS (a directory, a .tar, a .tar.zst) and a guard that only refuses is not that.
    """
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
    """The one refusal that was already right, kept honest while three were added around it: a
    path inside DATA_DIR that does not exist is missing, not forbidden, and the two are different
    things for the operator who mistyped a version."""
    with pytest.raises(HTTPException) as missing:
        artifacts_api._resolve(str(data_dir / "import" / "v20260829"))
    assert missing.value.status_code == 404


async def test_an_archive_that_will_not_open_is_a_four_hundred_the_data_tab_can_render(
    data_dir,
):
    """A4: `Bundle.open`'s four failure classes, mapped where the operator reads them.

    `BundleImport.svelte` renders `err.message`, and an exception escaping the handler arrives
    there as "Internal Server Error" - the one sentence that cannot be acted on. This case is a
    file that carries the right suffix and is not an archive, which `_unpack` answers with
    `BundleOpenError` before it creates anything.

    Deliberately NOT 4,096 zero bytes: `tarfile.is_tarfile` calls that a valid empty archive (two
    zero blocks are the end-of-archive marker), so it opens, extracts nothing, and is refused one
    step later as "no BUNDLE.json and no artifacts/" - a different guard, with a different
    message, for a different reader.
    """
    archive = data_dir / "import" / "v1.tar"
    archive.write_bytes(b"not a tar at all " * 256)

    with pytest.raises(HTTPException) as refused:
        # Awaited because `_open` hands `Bundle.open` to a thread under decision 287's
        # reasoning - the extraction is the other half of the press that owned the loop.
        # [M4.14 cycle 4, m414-c4-waveE-02]
        await artifacts_api._open(artifacts_api._resolve(str(archive)))

    assert refused.value.status_code == 400
    assert "not a tar archive" in str(refused.value.detail)
    assert not list(archive.parent.glob(".unpack*")), (
        "a refused archive must leave no tree behind for the retry to reuse"
    )


def test_the_route_and_the_registry_name_one_job_and_one_set_of_phases():
    """Two modules hold these strings and neither imports the other, so this is what holds them
    in order - the shape `test_worker_jobs.py` already uses for the backup budget and
    `backup/nightly.DUMP_TIMEOUT_SECONDS`.

    `api/artifacts.py` may not import `spielplan.worker`: that module calls `logging.basicConfig`
    at import time and would reconfigure the backend's root logger as a side effect of a route
    module being imported. A test can import both, and does.
    """
    assert artifacts_api.IMPORT_JOB == worker.BUNDLE_IMPORT_JOB
    assert artifacts_api.QUEUED == worker.PHASE_QUEUED
    assert artifacts_api.RUNNING == worker.PHASE_RUNNING
    # The door refuses a claim younger than the budget and lets the reaper have an older one, so
    # a budget that drifted from the worker's would either wedge the queue or re-open a live
    # import to a second press. [M4.14 cycle 1, m414-c1-dim-waveE-02]
    assert artifacts_api.IMPORT_CLAIM_BUDGET_S == worker.BUNDLE_IMPORT_TIMEOUT

    row = next((j for j in worker.JOBS if j.name == worker.BUNDLE_IMPORT_JOB), None)
    assert row is not None and row.run is not None, (
        "the route enqueues under a name this loop does not run: section 5.3's ninth row is "
        "the job"
    )
    assert row.owner is None, "a live job carrying an owner is counted twice by the boot census"

    # THE FOURTH PHASE, which was held by nothing. `PHASE_QUEUED`, `PHASE_RUNNING` and
    # `PHASE_ACTIVE` are each pinned to their literal by a test in this file or in
    # `test_devstub_contract.py`; `PHASE_FAILED` appears twice in the whole tree - its definition
    # and `PHASE_ACTIVE if ok else PHASE_FAILED` - and its only reader anywhere is the map below,
    # in a language no Python test reads. Rename it and the suite stays green while every refused
    # and every reaped import writes a phase the Data tab has never heard of, which `phaseOfJob`
    # then answers `UNKNOWN` for: the screen tells the operator it does not know what happened
    # about an import whose report is on the same screen. The reader is what is read, for the
    # reason `test_static_contracts.py` reads `home.svelte.js`'s FACETS.
    # [M4.14 cycle 3, m414-c3-waveE-phase-failed-is-unpinned]
    assert _phase_keys_of_the_screen() == {
        worker.PHASE_QUEUED, worker.PHASE_RUNNING, worker.PHASE_ACTIVE, worker.PHASE_FAILED,
    }


def _phase_keys_of_the_screen() -> set[str]:
    """The keys of `bundleImport.svelte.js`'s `PHASE_OF_JOB`, the one reader of these four
    strings outside this package."""
    module = (
        Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "lib" / "bundleImport.svelte.js"
    )
    block = re.search(r"PHASE_OF_JOB\s*=\s*\{(.*?)\}", module.read_text(encoding="utf-8"), re.S)
    assert block, "no `PHASE_OF_JOB = {...}` in bundleImport.svelte.js to read the phases from"
    return set(re.findall(r"(\w+)\s*:", block.group(1)))


# --- the install (Postgres) --------------------------------------------------------------------


@pytest.fixture
def bundle_at(tmp_path):
    """A fixture bundle under DATA_DIR, where the route's boundary requires it to be.

    `tmp_path` IS DATA_DIR under the `app` fixture, so `import/` here is `settings().import_dir`.
    """
    def make(version: str = "test-v1") -> Path:
        return fx.make_bundle(tmp_path / "import" / version, version=version)
    return make


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _tick() -> None:
    """One pass of the real loop, with the registry cut to the row under test.

    The tick itself is production code and is what the assertions are about - `due`, the trigger,
    the per-job budget, the `job_run` bookkeeping and the call. What is substituted is the rest of
    §5.3's table: a full tick with an empty `last_run` fires every interval job at once, so a
    failure in this file would be about the Jellyfin poll or the fold-in as often as about the
    import. `worker.JOBS` is what `_with_queued_import` reads, so the substitution is honest about
    what it changes.
    """
    row = next(j for j in worker.JOBS if j.name == worker.BUNDLE_IMPORT_JOB)
    jobs = worker.JOBS
    worker.JOBS = (row,)
    try:
        # `last_run` says this job ran a moment ago, so `due` returns nothing and anything that
        # happens is the admin action's trigger rather than the fallback poll.
        await worker._tick(
            time.monotonic(), datetime.now(UTC), {row.name: time.monotonic()}, {}
        )
    finally:
        worker.JOBS = jobs


async def _job_row(db, version: str | None = None):
    return await db.fetchrow(
        "SELECT id, name, ok, finished_at, detail FROM job_run "
        " WHERE name = $1 AND detail ? 'phase' "
        "   AND ($2::text IS NULL OR detail->>'bundle_version' = $2) "
        " ORDER BY started_at DESC, id DESC LIMIT 1",
        worker.BUNDLE_IMPORT_JOB, version,
    )


async def test_the_import_route_returns_202_and_the_version_to_poll(app, db, bundle_at):
    """finding 2.1: the route was a 127 s await on the backend's event loop.

    Inside it: the rmtree and copytree of the artifacts tree, the COPY generators,
    `repair_mojibake` over 485,602 review bodies and `placement.run_rebuild`'s numpy refits and
    torch forward pass, all in one transaction. `/api/health` acquires a pooled connection on that
    same loop, so it did not answer for those two minutes - against a 5 s HEALTHCHECK, behind a
    proxy that cuts at 100 s. The operator was told the import had failed while it completed and
    flipped, and the retry met `seed-once`, which reads as corruption.

    So: the answer is 202, it carries the version and the job id to poll, and NOTHING IS WRITTEN.
    The last clause is the one with teeth - decision 253 keeps `artifact_bundle` exactly as it is
    and writes no placeholder row, because `artifact_bundle_one_seed` would give that row the one
    and only seed slot and make a failed attempt unrepeatable.
    """
    admin = await _admin(app)
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
    """§5.3 files this work as a job, and this is the job running it.

    The trigger is the interesting half. §5.3's column says "admin action", and `due` can only ask
    a clock - it is pure so the schedule stays testable without one - so `last_run` here says the
    row ran a moment ago and `due` returns nothing. The import still happens, which is
    `_with_queued_import` asking the question a clock cannot answer.
    """
    admin = await _admin(app)
    root = bundle_at()
    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text

    now = time.monotonic()
    assert worker.BUNDLE_IMPORT_JOB not in {
        job.name for job in worker.due(now, {worker.BUNDLE_IMPORT_JOB: now})
    }, "this test proves the admin action fires the job, so the clock must not"
    await _tick()

    row = await db.fetchrow("SELECT version, state, report FROM artifact_bundle")
    assert row is not None, "the worker tick did not import the queued bundle"
    assert (row["version"], row["state"]) == ("test-v1", "active")
    assert (settings().artifacts_dir / "test-v1").is_dir(), (
        "section 10 step 2 staged nothing"
    )
    # §10's rebuild set runs before the flip and inside the same transaction, so the stored report
    # carries it - which is what the Data tab renders now that it polls rather than reading a
    # response body. [M4.14 step C4]
    rules = {finding["rule"] for finding in row["report"]["findings"]}
    assert {"stage", "rebuild", "swap", "rebuild-set"} <= rules, sorted(rules)

    done = await _job_row(db, "test-v1")
    assert done["detail"]["phase"] == "active"
    assert done["ok"] is True and done["finished_at"] is not None
    assert done["detail"]["report"]["ok"] is True


async def test_the_state_route_reports_the_phase_until_the_bundle_is_active(
    app, db, bundle_at, monkeypatch
):
    """§6.6's Data tab polls one route, and this is what it reads while it waits.

    The middle reading is the point and it is taken deterministically rather than by racing the
    import: the real `import_bundle` is wrapped so the test can stop it after the claim has been
    committed and before the transaction ends, which is exactly the window the Data tab spends
    polling. A `running` phase that is only visible to the connection doing the work would be no
    phase at all.
    """
    admin = await _admin(app)
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

    tick = asyncio.create_task(_tick())
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
    # §10 step 5: this process still holds the store it pinned at boot, and says so.
    assert done["restart_required"] is True


async def test_a_client_that_disconnects_does_not_change_the_outcome(app, db, bundle_at):
    """§5.3 budgets this work in minutes and §2 puts the origin behind a proxy that cuts at 100 s,
    so the request ending before the work does is the EXPECTED shape rather than an edge case.

    The client is closed before the tick runs, which is as disconnected as an ASGI transport
    gets: the exchange is over, the cookie jar is gone, and nothing about the import can be
    reached from it. The import then happens anyway and a second admin reads the outcome, which
    is the property a 202 plus a poll is for.
    """
    admin = await _admin(app)
    root = bundle_at()
    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0

    await admin.aclose()
    await _tick()

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
    """The route's half of "one import at a time". [M4.14 step D1, finding 2.2]

    Two windows and two questions, because one mechanism cannot see both: a RUNNING import holds
    `bundle.IMPORT_LOCK` for its whole run, and a QUEUED row that nothing has claimed holds no
    lock at all. The lock is taken around the check-and-enqueue as well, so two requests that
    arrive together cannot both find the queue empty.

    Neither question can wedge the queue, which is the reason the running case is asked with the
    lock rather than with the row: a worker killed mid-import leaves a `running` row for ever and
    releases its session lock with its connection, so an operator's retry is accepted.
    """
    admin = await _admin(app)
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

    # The running half, with the lock held by another session exactly as a worker mid-import
    # holds it.
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


async def _queue(db, version: str, *, phase: str, age_s: int, claimed_s: int | None = None):
    """A `job_run` row in whatever phase and at whatever age the case under test needs.

    `started_at` is the enqueue instant and `claimed_at` the instant a loop took the row, which
    is the distinction the reaper turns on: they are the same number only when a worker was
    standing at the queue when the operator pressed Import.
    """
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
    """The terminal report a crash produces may state only what the writer observed.

    `import_bundle` COMMITS - the row is `active`, every fitted number is expressed in the new
    basis - and only then does `_clean_unpacked` rmtree the 790 MB extraction, the session
    advisory lock come off and a SECOND `pool.acquire()` write the terminal phase. A SIGKILL past
    the stop grace, an OOM or a power cut anywhere in that multi-second tail leaves the bundle
    ACTIVE and the job row `running`, and the reaper then told the operator "Nothing is
    half-written - the database transaction rolled back with the process - and the bundle can be
    imported again" about an import that had completed. Acting on that sentence means either a
    retry that meets "already the active bundle" and seed-once - which `api/artifacts.py`'s own
    docstring says reads as corruption - or restoring /data from backup, which rolls a successful
    import back.

    The reaper reads no `artifact_bundle`, so the sentence was never observed. It reads one row
    now, and says one of two things it can see. [M4.14 cycle 1, m414-c1-dim-lock-01]
    """
    admin = await _admin(app)
    accepted = await admin.post("/api/admin/bundle/import", json={"path": str(bundle_at())})
    assert accepted.status_code == 202, accepted.text
    await _tick()
    assert await db.fetchval("SELECT state FROM artifact_bundle WHERE version = 'test-v1'") == (
        "active"
    )

    # The kill: a claim past the budget for the import that has already flipped.
    killed = await _queue(db, "test-v1", phase=worker.PHASE_RUNNING, age_s=400, claimed_s=400)
    await worker._reap_abandoned_import(db)

    row = await db.fetchrow("SELECT ok, finished_at, detail FROM job_run WHERE id = $1", killed)
    assert row["finished_at"] is not None, "the claim was left open for the Data tab to poll"
    assert row["ok"] is True, row["detail"]["text"]
    text = row["detail"]["text"]
    assert "Nothing is half-written" not in text, text
    assert "active" in text and "restart" in text, text

    # And with no flip to find, the failure states what it observed and nothing more.
    orphan = await _queue(db, "test-v9", phase=worker.PHASE_RUNNING, age_s=400, claimed_s=400)
    await worker._reap_abandoned_import(db)
    lost = await db.fetchrow("SELECT ok, detail FROM job_run WHERE id = $1", orphan)
    assert lost["ok"] is False
    assert "Nothing is half-written" not in lost["detail"]["text"]
    assert "no process reported" in lost["detail"]["text"], lost["detail"]["text"]


async def test_a_crash_after_the_flip_is_reported_as_the_flip_and_not_as_a_failed_import(
    app, db, bundle_at, monkeypatch
):
    """The reaper's question, asked at the other terminal exit.

    `import_bundle` COMMITS and then does three more things on that connection: `_clean_unpacked`
    rmtree-ing 790 MB of a 1.04 GB extraction, the session advisory unlock, and the return. A
    connection lost anywhere in that tail - a `pg_terminate_backend`, a pooler cutting an idle
    session, a fast restart - therefore raises out of an import that HAPPENED, and
    `_bundle_import`'s `except Exception` arm wrote "the import did not run to a report" with
    `ok=false` having read no row at all. The Data tab then renders a failure for a bundle that is
    active, and the operator's retry meets "already the active bundle" and seed-once, which reads
    as corruption: verbatim the outcome decision 253 exists to remove, reached through the code
    that removed it. `_reap_abandoned_import` was taught to read the row in cycle 1; one
    derivation now serves both writers. [M4.14 cycle 3, m414-c3-dimlock-03]
    """
    admin = await _admin(app)
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

    # The boundary, because an arm that called every crash a success would pass the half above:
    # a claim whose bundle is not there never flipped anything, and stays a failed import.
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
    """The reaper had one production call site and nothing could reach it after a restart.

    `_bundle_import` reads the reaper on the branch where `_claim_bundle_import` returns None, so
    it runs only on a tick where the job FIRES and finds nothing queued - and `_import_is_queued`
    asked for `phase = 'queued'`, which an abandoned claim is not. That left the row's own hourly
    fallback, which a restarted worker does not have either: `_seed_schedule` seeds `last_run`
    from the newest `ok` row and every hourly poll that finds nothing queued writes one, so after
    a SIGKILL past the stop grace, an OOM or a power cut `due` refuses to fire the job for up to
    an hour. Meanwhile `_running_import` hands the stale `running` row to the Data tab, which
    adopts and polls it on every visit for eleven minutes before printing an unknown outcome -
    and the reaper's sentence ("this import committed and <v> is the active bundle ... restart
    the backend and the worker") is the one line nothing else can write.

    Both halves are asserted: that the clock CANNOT do it, and that the tick does.
    All four shipped reaper tests call `_reap_abandoned_import(db)` directly, so the branch that
    reads it executed in no test at all and would have passed against a worker that never called
    it. [M4.14 cycle 2, m414-c2-waveE-02]
    """
    killed = await _queue(db, "test-v9", phase=worker.PHASE_RUNNING, age_s=400, claimed_s=400)
    now = time.monotonic()
    for age in (60.0, 300.0, 1800.0, 3500.0):
        assert worker.BUNDLE_IMPORT_JOB not in {
            job.name for job in worker.due(now, {worker.BUNDLE_IMPORT_JOB: now - age})
        }, f"the fallback fired at {age:g}s, so this test would prove nothing"

    await _tick()

    row = await db.fetchrow("SELECT ok, finished_at, detail FROM job_run WHERE id = $1", killed)
    assert row["finished_at"] is not None, (
        "a claim no process will finish stayed open on a tick that could have closed it"
    )
    assert row["ok"] is False
    assert "no process reported" in row["detail"]["text"], row["detail"]["text"]


async def test_the_import_leads_the_tick_even_when_the_clock_produced_it(app, db, bundle_at):
    """`_with_queued_import`'s ordering is a guarantee its docstring makes, and the guard skipped

    it in exactly the case `due` produces. `due` sorts stably by `stage` alone and the registry
    declares `placement-reconciliation` before the import at the same stage, so once the row's
    hourly fallback has elapsed - which is every tick of a restarted worker, `_seed_schedule`
    seeding from the newest successful run - the job was already in `ready` and the guard
    returned `ready` untouched, with the sweep leading after all. The `MODEL_JOBS` skip cannot
    compensate: `_import_in_flight` is False while the import is only queued.

    The registry ordering is pinned here as well as the fix, because the fix is only interesting
    while `due`'s own answer puts the sweep first. [M4.14 cycle 2,
    m414-c2-dimlock-import-not-in-front-when-due]
    """
    await _queue(db, "test-v1", phase=worker.PHASE_QUEUED, age_s=1)
    # A box switched on after its night window: the anchored sweep is unspent for today and the
    # import's hourly fallback has elapsed, which is the tick a restart produces.
    ready = worker.due(
        time.monotonic(), {}, local=datetime(2026, 1, 1, 23, 0, tzinfo=UTC), last_date={}
    )
    stage_zero = [j.name for j in ready if j.stage == 0]
    assert stage_zero.index("placement-reconciliation") < stage_zero.index(
        worker.BUNDLE_IMPORT_JOB
    ), "registry order no longer puts the sweep first, so this test asserts nothing"

    ordered = [job.name for job in await worker._with_queued_import(ready)]

    assert ordered[0] == worker.BUNDLE_IMPORT_JOB, ordered
    assert ordered.count(worker.BUNDLE_IMPORT_JOB) == 1, ordered
    assert sorted(ordered) == sorted(j.name for j in ready), "a job left the tick"


async def test_the_in_flight_probe_reads_the_lock_and_never_takes_it(app, db, monkeypatch):
    """A reader may not be able to refuse the thing it is asking about.

    The probe was `pg_try_advisory_lock` plus an immediate unlock, and for the ~1.4 ms it held
    the key any real writer's own try-lock returned false - which both writers turn into "an
    import is already running": the route into a 409 and `import_bundle` into the report failure
    `_finish_bundle_import` records as a FAILED import of a bundle that would have imported
    perfectly well. That is the outcome `_claim_bundle_import`'s SKIP LOCKED claim exists to
    avoid, and it is cross-process - a rolling restart's second worker, or this loop against the
    backend's own route.

    The statements are recorded rather than the race being run, because the race is ~1e-4 per
    import and a test that reproduces it is a test that fails one time in ten thousand. What is
    asserted is the property: the probe answers correctly in both directions and takes nothing.
    [M4.14 cycle 2, m414-c2-dimlock-probe-takes-the-writers-lock]
    """
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
    """`started_at` is the instant the operator pressed Import; the budget runs from the claim.

    `_claim_bundle_import` leaves `started_at` alone on purpose - it is what the Data tab measures
    from - while `_tick` bounds the attempt with `asyncio.wait_for` from the moment the job
    starts. The reaper argued from the claim ("a claim younger than the budget may still be
    running") and its SQL compared `started_at`, so the two windows overlapped by exactly the
    enqueue-to-claim delay: a row queued while the worker was down, or behind the 2100 s nightly
    backup this same sequential loop runs, is already past the budget at the instant it is
    claimed, and a second loop's first tick reaps a live import - publishing a terminal failure
    for an import that is mid-flight and about to flip.
    [M4.14 cycle 1, m414-c1-dim-lock-04]
    """
    queued = await _queue(db, "test-v1", phase=worker.PHASE_QUEUED, age_s=400)

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
        "to_jsonb(now() - interval '400 seconds')) WHERE id = $1",
        queued,
    )
    await worker._reap_abandoned_import(db)
    assert await db.fetchval("SELECT finished_at FROM job_run WHERE id = $1", queued) is not None


async def test_a_second_import_in_the_window_between_the_claim_and_the_lock_is_refused(
    app, db, bundle_at
):
    """The door asks two questions and there is a window in which neither is true.

    `_claim_bundle_import` commits `phase='running'` and releases its connection; `import_bundle`
    takes `IMPORT_LOCK` as its first statement. Between them the worker is in `Bundle.open` and a
    second `pool.acquire()`, holding no lock and owning no `queued` row - so a second press was
    accepted, `_running_import` then answered with the NEWER row, and the first operator's tab
    polled its own job id for the full deadline and reported `unknown` for an import that
    succeeded. That is the defect this milestone exists to remove, one layer up.

    The row is still not asked about as `running` alone: a `running` row a kill left behind must
    stay retryable, which is what the reaper's budget decides. A claim younger than the budget is
    an import in flight; one older belongs to the reaper. [M4.14 cycle 1, m414-c1-dim-waveE-02]
    """
    admin = await _admin(app)
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

    # A claim older than the budget is the reaper's, and the retry an operator makes next is
    # accepted rather than wedged behind it.
    await db.execute(
        "UPDATE job_run SET detail = jsonb_set(detail, '{claimed_at}', "
        "to_jsonb(now() - interval '400 seconds')) WHERE id = $1",
        claimed["id"],
    )
    accepted = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert accepted.status_code == 202, accepted.text


async def test_the_worker_model_jobs_do_not_write_across_the_flip(app, db, monkeypatch):
    """§10's invariant, in the one process that could break it while an import is in flight.

    The fold-in, the tier-set refit and the two Ledger fits all acquire their basis through
    `_active_store` and then WRITE numbers expressed in it. Started against v1 and committed
    after the swap to v2, those numbers are stamped with a version this install no longer serves -
    and the invariant ("no process may score or refit with a loaded bundle version different from
    the active row") cannot catch it, because the row moved after the job read it.

    The lock is held by another session here, which is the case the skip exists for: this loop is
    sequential, so its own import cannot overlap its own jobs, and what can is an import running
    in the backend process, in a second worker during a rolling restart, or in an ops script. The
    substituted registry is two rows of the real `Job` so the skip is decided by `MODEL_JOBS` and
    the tick, rather than by anything this test arranges.
    """
    ran: list[str] = []

    def spy(name: str):
        async def run() -> dict[str, object] | None:
            ran.append(name)
            return None
        return run

    fitting = worker.Job(
        "fold-in-tick", "M2", "after each sitting's writes", "ms", spy("fold-in-tick"),
        every=60, timeout=55,
    )
    other = worker.Job(
        "session-prune", "M0", "hourly", "ms", spy("session-prune"), every=3600, timeout=60,
    )
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
    """The other half of the same sentence, in the process that writes it to a log.

    The worker's model jobs log "skipped: no active bundle (section 3.1)" - the clause that makes
    the state LEGAL - and dd01's reading was that they said it for a broken install too. They
    cannot, and the reason is worth a test rather than a comment: `_active_store` asks
    `assert_not_broken` BEFORE it maps an empty store to None, so a job whose row exists and whose
    files are gone raises out of that guard and never reaches its own skip line. The line is only
    reached when there is no row at all, which is exactly when it is true.

    What DID say it for both was the boot line, which reads `store.is_empty` and nothing else
    (`test_worker_schedule.py::test_the_boot_line_does_not_call_a_broken_install_legal`). Both
    halves are asserted so that a future refactor cannot move the guard and quietly make the
    remaining lines dishonest.

    SO THIS ONE PASSES AGAINST THE CODE AS IT WAS, and says so rather than being presented as a
    fix: M4.13 put `assert_not_broken` where it is and this milestone measured what that already
    buys. It is the boundary of D3's change, and an unasserted boundary is how the next reader
    concludes the two lines were part of it. [M4.14 step D3, decision 258, finding 2.17]
    """
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
    """dd01 and decision 258: two banners, two instructions, and only one of them can be right.

    `/data/artifacts` is a host bind mount outside §2's nightly `pg_dump`, so "database restored,
    files missing" is a realistic state rather than a hypothetical one. The store then carries the
    active row's version with `broken = True` - which is what stamps a fit honestly - and that in
    turn makes `active != store.version` False, so the page reported neither a restart nor a
    restore and rendered an active bundle with nothing loaded and nothing to do.

    The second half is the state where BOTH would render: broken on the outgoing version and stale
    against a newer active row. A restart is the answer to a swap and reloads the same empty store
    here, so the restore is the only instruction while the files are gone.
    """
    admin = await _admin(app)
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
