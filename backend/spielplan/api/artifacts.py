"""Artifact-bundle admin routes. Spec v2.1 §6.6 (Data tab), §3.1 (wizard step 3), §10.

§3.1: the wizard's bundle step *is* this importer — "the same importer the §6.6 Data tab
exposes (that one page is M0 scope)". There is one implementation and two entry points.
"""

from __future__ import annotations

import asyncio
import tarfile
import threading
from pathlib import Path
from typing import Any

# Named at module scope rather than inside the handler, which is the opposite of
# `importer/bundle._unpack`'s lazy import and for a different job: that one defers the decoder
# until an archive actually needs decoding, this one names an EXCEPTION CLASS in an `except`
# clause that has to exist before the call it guards. A hard dependency either way
# (`backend/pyproject.toml`), and the C extension costs milliseconds to import.
import zstandard
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from spielplan.api.deps import DB, AdminUser
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import

router = APIRouter(prefix="/api/admin/bundle", tags=["admin", "bundle"])

# §10's step 5, in the operator's words: the one place the 409 and the import RESPONSE share.
#
# The import response has said this since M0 and nothing enforced it. M4.13 gave the invariant
# production callers (`models/artifacts.py::assert_matches`), and the refusal they raise has to say
# the same sentence the import said - a member seeing "restart backend and worker" on a 409 and an
# admin reading a different phrasing on the import screen would be two descriptions of one state,
# which is how an operator comes to believe they are two. Imported by `api/rate.py` and
# `api/rank.py` rather than re-typed; the constant lives here because this is the module that
# already owned the sentence. [M4.13, data-01 mitigation correction]
#
# IT IS NOT THE ONLY PLACE THE WORDS APPEAR, AND SAYING SO IS THE POINT. This comment claimed to be
# the one place and was wrong about the one screen it is about: `BundleImport.svelte` hard-codes
# the sentence instead of rendering the `note` this module already sends it, and `ops/devstub.py`
# carries a harness copy. So an edit here moves the 409 and the response and neither of those. The
# importer's swap note used to be a fourth, DIFFERENT phrasing rendered as a finding on that same
# screen - two descriptions of one state, on one page - and it no longer states the clause at all.
# The remaining duplicate is the client's, which is the client's to remove.
# [M4.13 cycle 1, m413-c1-dim1-restart-sentence-written-in-three-places]
RESTART_REQUIRED = (
    "restart backend and worker — no process may score or refit with a loaded "
    "bundle version different from the active row"
)

# The other refusal §10's invariant needs, and it is a different instruction: a broken install's
# files are gone, so restarting this process changes nothing. `assert_not_broken` raises it inside
# the app with `store.root` attached (the operator needs the path); this is the member's half of
# the same state, and it names the restore. Beside `RESTART_REQUIRED` because the two sentences are
# the two halves of one §10 clause and a reader has to be able to see they are different.
# [M4.13 cycle 1, m413-c1-dim1-broken-bundle-refusal-is-worker-only]
RESTORE_REQUIRED = (
    "the active bundle's files are missing - restore /data/artifacts or import the bundle "
    "again; no process may score or refit in a basis whose files are gone"
)

# The `job_run.name` this route enqueues under, and `worker.JOBS`'s own name for section 5.3's
# ninth row. A literal here rather than `from spielplan.worker import ...`: that module calls
# `logging.basicConfig` at import time, so pulling it into the API process to read one string
# would reconfigure the backend's root logger as a side effect of a route module. Two modules
# holding one value is the shape `worker.JOBS`'s backup budget and
# `backup/nightly.DUMP_TIMEOUT_SECONDS` already have, and it is held the same way - by an
# assertion rather than by a shared import (`test_bundle_import_job.py`).
# [M4.14 step E1, decision 253]
IMPORT_JOB = "bundle-import"

# The phases a queued import passes through, as `job_run.detail->>'phase'`. Two in-flight names
# and no more, because each is a different thing for the Data tab to render: `queued` is "the
# worker has not picked it up yet" and `running` is "the load is happening and nothing is
# flipped"; the terminal pair the worker writes is the outcome.
#
# They are `job_run` values and NOT `artifact_bundle` states, which is decision 253 and a
# correction to perf-02's published mitigation (a new 'importing' state behind a widened CHECK).
# Two reasons, both verified against this tree: `artifact_bundle.kind` is NOT NULL DEFAULT 'seed'
# under the partial unique index `artifact_bundle_one_seed` (`0015_seed.sql:105-121`), so a
# placeholder row written for a seed import claims the one and only seed slot and a failed
# attempt can never be retried; and `placement.assert_staged` (`placement/reconcile.py:520-533`)
# rebuilds only for a row whose state is already 'validated' or 'active', so a new state would
# have to widen that too. The row therefore stays exactly where it is - written inside the
# import transaction at 'validated' and flipped to 'active' - and the `job_run` row carries the
# phase. No migration. [M4.14 step E1, decision 253]
QUEUED = "queued"
RUNNING = "running"

# `worker.BUNDLE_IMPORT_TIMEOUT`, mirrored for the reason `IMPORT_JOB` is mirrored and held in
# order by the same assertion. It is what separates a claim that is an import IN FLIGHT from one
# a kill left behind, which is the question the door below has to ask about a `running` row:
# younger than this is a worker mid-import, older than this is `_reap_abandoned_import`'s.
# [M4.14 cycle 1, m414-c1-dim-waveE-02]
IMPORT_CLAIM_BUDGET_S = 600.0

IMPORT_IN_FLIGHT = (
    "another bundle import is already running on this install - wait for it to finish and read "
    "/api/admin/bundle/state"
)


class BundleRef(BaseModel):
    path: str | None = None      # defaults to /data/import


def _resolve(path: str | None) -> Path:
    """The one boundary an admin-supplied filesystem path is held to. Spec v2.1 section 6.6,
    section 10 step 2.

    Four refusals, and three of them are paths this route accepted while the comment inside it
    promised it did not.
    """
    cfg = settings()
    target = Path(path) if path else cfg.import_dir
    target = target.resolve()
    # An admin-only route that takes a filesystem path still gets a boundary: the bundle must
    # live under DATA_DIR, so a typo cannot make the app read arbitrary host files.
    #
    # A PATH, and no longer a string prefix. `str(target).startswith(str(cfg.data_dir.resolve()))`
    # WAS that boundary, and with the shipped `DATA_DIR=/data` it admits `/database`, `/data.bak`
    # and every other sibling whose name begins with those five characters. Reproduced end to end
    # with an admin session: validate and import of a bundle under `<DATA_DIR>-other` both
    # answered 200, and the bundle was flipped active. `is_relative_to` asks the question the
    # sentence above has always claimed to ask, and it is the idiom `importer/bundle.py` already
    # uses on the staging side four hundred lines away ("escapes the artifacts root").
    # [M4.14 step A1, finding 2.7]
    if not target.is_relative_to(cfg.data_dir.resolve()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"bundle path must live under {cfg.data_dir}",
        )
    # Section 10 step 2 stages a bundle TO `/data/artifacts/<version>/`, so that tree is this
    # app's output and never its input. A bundle unpacked there is deleted by its own import:
    # `import_bundle` rmtree's `artifacts_root / version` before it copies, so for a bundle at
    # `/data/artifacts/v20260828/` - the one directory the Data tab names by version, and
    # therefore the obvious place to put one while restoring - `content.sqlite`, `reviews.sqlite`,
    # `BUNDLE.json` and `artifacts/` are removed before the first byte is copied, `copytree` then
    # raises `FileNotFoundError` as a 500, and the gigabyte on the box is gone with validation
    # already passed.
    #
    # Refused here as well as beside the `rmtree` (`bundle._refuse_self_staging`), because the two
    # refusals are for two readers: that one guards the destructive statement, this one is the
    # sentence the operator can act on, since it names where a bundle belongs. No allow-list and
    # no override - section 10 gives the staging tree one writer. [M4.14 step A2, finding 2.8]
    if target.is_relative_to(cfg.artifacts_dir.resolve()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{target} is inside {cfg.artifacts_dir}, which this app stages INTO - a bundle "
            f"imported from there is deleted by its own import. Put it under {cfg.import_dir}",
        )
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no bundle at {target}")
    # A bundle is a directory, a `.tar` or a `.tar.zst` - the three shapes `Bundle.open` knows.
    # Every other file was handed to `tarfile.open` regardless, so `README.txt` as the path raised
    # a `ReadError` the app answered with an HTTP 500 rendered as a bare string, left an empty
    # `.unpacked-README/` behind, and had its SECOND attempt diagnosed as "this is a models-only
    # bundle and the install has no content" - a decision-162 refusal for a typo. `_unpack`
    # refuses it now too (`BundleOpenError`); this is the same refusal one step earlier, where the
    # answer is about the path rather than about the archive. [M4.14 step A2, finding 2.5]
    if target.is_file() and not target.name.endswith((".tar", ".tar.zst")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{target.name} is not a bundle: a bundle is a directory, a .tar or a .tar.zst",
        )
    return target


# One extraction at a time in this process, because the offload below is what takes that away.
# `_unpack` stages every extraction into ONE `.unpacking-<name>/` and `rmtree`s that path before it
# begins, so two extractions of one archive walk over each other - and a synchronous `_open` could
# not overlap with itself, since the loop ran it to completion before it read the next request.
# Measured on a 1.04 GB archive, two presses 250 ms apart, no lock, three runs of two: the
# second press raised `FileExistsError` out of `staging.mkdir` onto the remains of the first's
# tree, and the first press then `os.replace`d a 2-entry staging tree onto `.unpacked-` and
# returned a bundle whose version was `unknown` - `_unpack`'s own rule ("a `.unpacked-<name>/`
# directory is a COMPLETE extraction, always") broken by the repair. Neither route serialises it:
# `POST /validate` takes no lock at all and `POST /import` takes `IMPORT_LOCK` only after this
# call, so two presses on the Data tab are enough.
#
# A `threading.Lock` and not an `asyncio.Lock`: it is taken inside the worker thread, where it can
# hold across the whole extraction, and it is not bound to the first loop that awaited it the way a
# module-level `asyncio.Lock` is (`asyncio.mixins._LoopBoundMixin`) - this module is imported once
# per process and the suite runs a loop per test. It costs a pool thread while a second press
# waits, which is exactly what that press did before: it waited, and the loop waited with it.
# [M4.14 cycle 4, m414-c4-waveE-02]
_EXTRACTING = threading.Lock()


def _extract(target: Path) -> bundle_import.Bundle:
    """`Bundle.open` under `_EXTRACTING`, on the thread `_open` hands it to."""
    with _EXTRACTING:
        return bundle_import.Bundle.open(target)


async def _open(target: Path) -> bundle_import.Bundle:
    """`Bundle.open`, with every way an archive can refuse turned into a 400 that says so.

    OFF THE EVENT LOOP, for the reason decision 287 moved the validation off it one call later.
    `Bundle.open` EXTRACTS when the archive has no `.unpacked-<name>/` tree beside it yet, and on
    the 1.04 GB corpus archive that is 0.61 s of `tarfile.extractall` with the loop stopped for all
    of it: one 10 ms heartbeat sample across the call and a worst gap of 0.620 s, against 55
    samples and 0.012 s for the same call in a thread. It is paid on the FIRST press of Validate or
    Import and on no other, which is why no recorded run of `ops/m414_exit_criterion.py` contains
    it - that script tars the corpus and opens it before `create_app()`, so every request it
    measures takes `_unpack`'s reuse path and the press an operator actually makes was never
    measured. Driven through the app it was 5.88-6.27 s for `POST /import` against check 8's 5 s
    budget, with `/api/health` taking 5.2 s to answer - past the HEALTHCHECK's own 5 s timeout,
    which is the failure this milestone exists to remove. The four refusals below are unaffected:
    an exception raised in the thread propagates out of the await unchanged.
    [M4.14 cycle 4, m414-c4-waveE-02; decision 287]

    Section 6.6 makes the Data tab where an operator meets the importer and
    `BundleImport.svelte` renders `err.message`, so a refusal that arrives as a 500 arrives as
    "Internal Server Error" - the one sentence nobody can act on. The four classes are the four
    that were measured: `BundleOpenError` for a file that is not an archive this app opens and
    for an extraction it could not clean up, `TarError` for a truncated or corrupt tar,
    `ZstdError` for a `.tar.zst` whose frame will not decode, and `OSError` for the disk
    underneath all three. Nothing wider: a `KeyError` out of the importer is a bug and has to go
    on arriving as one. [M4.14 step A4, finding 2.5]
    """
    try:
        return await asyncio.to_thread(_extract, target)
    except bundle_import.BundleOpenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (tarfile.TarError, zstandard.ZstdError, OSError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{target} could not be opened as a bundle: {type(exc).__name__}: {exc}",
        ) from exc


async def _running_import(conn) -> dict[str, Any] | None:
    """The newest bundle-import `job_run` row that carries a phase, rendered for the Data tab.

    Filtered on the phase key rather than on the name alone, because `_tick` writes a row of its
    own for every job it fires and the poll that finds nothing to claim is one of them: those
    rows are the loop's bookkeeping and carry no `phase`, while these are the operator's import.
    One query, newest first, whatever its outcome - after the flip the Data tab still has to
    render what happened, and after a failure the report stored on this row is the ONLY copy,
    since a failed import rolls its `artifact_bundle` row back with the transaction.
    [M4.14 step E3, decision 253]
    """
    row = await conn.fetchrow(
        "SELECT id, started_at, finished_at, ok, detail FROM job_run "
        " WHERE name = $1 AND detail ? 'phase' ORDER BY started_at DESC, id DESC LIMIT 1",
        IMPORT_JOB,
    )
    if row is None:
        return None
    detail = row["detail"] or {}
    return {
        "job_id": row["id"],
        "phase": detail.get("phase"),
        "bundle_version": detail.get("bundle_version"),
        "path": detail.get("path"),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "ok": row["ok"],
        "report": detail.get("report"),
        "text": detail.get("text"),
    }


@router.get("/state")
async def bundle_state(conn: DB, _: AdminUser, request: Request) -> dict[str, Any]:
    """The Data tab's state.

    Reports the DB-active version and the *loaded* version separately, because §10's swap
    sequence ends in "restart backend + worker" and between the flip and that restart they
    legitimately disagree. Collapsing them into one field made the page say "no bundle is
    active" immediately after a successful import — which is the opposite of what happened.

    Bundles and nothing else. This payload briefly also carried the worker's `job_run` outcomes,
    written while owner decision 2 was still open and its option (A) — "Data tab + Connectors card
    only" — was live. Decision 182 took option (B) instead: §6.6's System card, at
    `GET /api/admin/system`, is where an operator reads job health and backup status, and this
    page renders none of it (`frontend/src/routes/admin/data/+page.svelte` reads `bundles`,
    `active`, `loaded`, `restart_required` and `rebuild_set`). A payload with no reader is a
    second definition of the 36-hour rule waiting to disagree with the first, on a route the Data
    tab polls on a timer. [M4.7 ops-11; decision 182]

    `import_job` is not a re-opening of that argument and the boundary it draws is the reason it
    can be here. Decision 182 put JOB HEALTH - which jobs ran, when, whether the dump is stale -
    on section 6.6's System card at `GET /api/admin/system`, and none of that is in this key: it
    carries one running import's phase and that import's own report. Section 10 makes the import
    a planned admin event with a migration report, and after M4.14 the event outlives the request that
    started it, so the page that started it needs ONE endpoint to poll - and it already polls
    this one. The alternative was a second route beside the one the Data tab is written against,
    which is how two screens come to disagree about one import. [M4.14 step E3, decision 253]
    """
    rows = await conn.fetch(
        "SELECT version, state, imported_at, activated_at FROM artifact_bundle "
        "ORDER BY imported_at DESC"
    )
    active = next((r["version"] for r in rows if r["state"] == "active"), None)
    store = request.app.state.artifacts
    return {
        "bundles": [dict(r) for r in rows],
        "active": active,
        "loaded": store.summary() if not store.is_empty else None,
        # §10's invariant, surfaced: "no process may score or refit with a loaded bundle
        # version different from the active row."
        #
        # `and not store.broken`, because this flag and `broken` below drive two banners that are
        # two DIFFERENT instructions, and a page rendering both tells an operator to do two things
        # of which only one can help. The state that produces both is ordinary: boot with the
        # active version's directory already gone (`broken`, carrying that version), then let an
        # import flip the row to a new version - now `active != store.version` is True as well,
        # and the page says "restart backend and worker to load v2" beside "restore
        # /data/artifacts". A restart is the answer to a swap; it reloads the same empty store for
        # a broken install, which is what dd01 measured. One state, one instruction: while the
        # files are gone the restore is the only banner, and the swap's restart is still owed
        # after it and is still reported by this flag once the store is whole.
        # [M4.14 step D3, decision 258, finding 2.17]
        "restart_required": active != store.version and not store.broken,
        # The third state this pair could not express. `active != store.version` is False when the
        # active row's DIRECTORY is gone, because `load_active` now carries that row's version with
        # `broken = True` - correctly, so the fit is stamped honestly - and `loaded` above is None
        # because `is_empty` stays True. An operator reading this page would then see an active
        # bundle, no loaded bundle and no restart required, which describes nothing. The only other
        # report of this state is one ERROR line at boot, and §6.6 makes the Data tab the place an
        # operator finds out. [M4.13, data-03]
        "broken": store.broken,
        "missing_path": str(store.root) if store.broken else None,
        "import_dir": str(settings().import_dir),
        "rebuild_set": list(bundle_import.REBUILD_SET),
        # The import the Data tab is waiting on, or the last one it ran. None on an install that
        # has never imported through the route. See `_running_import`.
        "import_job": await _running_import(conn),
    }


@router.post("/validate")
async def validate_bundle(body: BundleRef, conn: DB, _: AdminUser) -> dict[str, Any]:
    """Step 1 of the §10 swap sequence. Writes nothing.

    Takes the connection because §10 makes this step the decision point and three of the
    importer's refusals — a second content seed, a model bundle with no content under it, a
    vocabulary change (decisions 162 and 163) — are facts about *this install* rather than about
    the bundle. Validated without them, the Data tab reported "ok" for an import that was then
    refused at the flip, which is after the operator has committed.
    """
    b = await _open(_resolve(body.path))
    # `settings().artifacts_dir` threaded in so that step A7's refusal - a bundle sitting under
    # the tree its own import rmtree's - is reachable HERE, where the Data tab makes its decision,
    # rather than only beside the delete. `validate_for_install` takes it as a keyword default so
    # the pre-flight tools, which validate with no install behind them, keep their two-argument
    # call. [M4.14 step A7, finding 2.8]
    report = await bundle_import.validate_for_install(conn, b, settings().artifacts_dir)
    # The REPORT's version, not `b.version`. The two are derived from the same BUNDLE.json by two
    # functions, and when they disagreed this payload showed both: `bundle_version: 'unknown'` at
    # the top level beside `report.bundle_version: 'v2026/08'` in the findings below it, for one
    # bundle, on one screen. `_read_bundle_identity` is now the refusal AND the single answer
    # (`validate.safe_version`), so reading the report is reading the one derivation.
    # [M4.14 step B7, finding 2.11]
    return {
        "bundle_version": report.bundle_version,
        "report": report.as_dict(),
        "text": report.render(),
    }


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_bundle(body: BundleRef, conn: DB, _: AdminUser) -> dict[str, Any]:
    """Validate here, and enqueue the rest. §5.3's ninth row, off the request path.

    This route used to `await bundle_import.import_bundle(...)` outright, and that await is
    measured at 127 s on the real bundle (M4.5's own close-out): the rmtree and copytree of the
    artifacts tree, the COPY generators, `repair_mojibake` over 485,602 review bodies and
    `placement.run_rebuild`'s numpy refits and torch forward pass, all on the backend's event
    loop, inside one transaction. For those two minutes `/api/health` does not answer - it
    acquires a pooled connection on the same loop - against a HEALTHCHECK with a 5 s timeout; and
    section 2 puts the origin behind the operator's Traefik and Cloudflare, which cut a proxied
    request with no origin response at 100 s. So the operator was told an import had failed while
    it completed and flipped, and the retry then met `seed-once` - "movie data was already seeded
    by bundle ..." - which reads as corruption. Section 5.3 files this work as a JOB with a
    "minutes" budget, and the worker is the process that runs jobs; it already holds torch and
    the Backbone.

    What stays on the request is the decision: `validate_for_install` is 1.0 s on v20260828 and
    ~4 s with M4.14's hashing and integrity passes, and it is the step the operator is standing
    in front of. What leaves is everything that writes. The answer is 202 with the version and
    the `job_run` id to poll, and `GET /api/admin/bundle/state` reports the phase from there.

    THAT ~4 s IS NO LONGER ON THIS LOOP, and until decision 287 it was the one measure of the
    exit criterion this milestone did not meet: `app._HEALTH_TIMEOUT_S` is 2, so `/api/health`
    answered 503 once inside this route's validation window - `ops/m414_exit_criterion.py`'s
    check 9, red in every run recorded before 287, and never during the import itself.
    `validate_for_install` now runs the synchronous `validate` in a worker thread, which moves
    the whole window rather than the ~1.2 s of Cold Tower construction decision 252's one-shot
    child would have moved. The route is unchanged from the operator's side: it still blocks on
    that await, so the decision is still made here before anything is enqueued.
    [decisions 252 and 287]

    It was the one measure that instrument SAW, which is a different sentence and is why `_open`
    above is now awaited too: on the first press against an archive the line below also extracts
    it, and no recorded run contains that because the script tars and opens the corpus before the
    app exists. Driven through the app with nothing extracted, this route answered 202 in 5.88 s
    and 6.27 s against the criterion's "under 5 s". [M4.14 cycle 4, m414-c4-waveE-02]

    NO `artifact_bundle` ROW IS INSERTED HERE - see `QUEUED` above for decision 253's two
    reasons, both of which are properties of this tree rather than of the design.

    One import at a time, said at the door and said twice, because the two windows are different.
    The advisory lock is `importer/bundle.IMPORT_LOCK`, the one the import itself holds for its
    whole run, so failing to take it means an import is RUNNING - in this process, in the worker,
    or in an ops script. A row nothing is holding a lock for is the second question.
    [M4.14 step E1, decision 253, findings 2.1 and 2.2]

    That second question is asked of a CLAIMED row as well as of a queued one, and there is a
    window that needs it: `_claim_bundle_import` commits `phase='running'` and releases its
    connection, and `import_bundle` takes the lock as its first statement - so between them the
    worker is in `Bundle.open` and a second `pool.acquire()`, holding no lock and owning no
    queued row, and a second press was accepted. `_running_import` then answers the Data tab with
    the NEWER row, so the first operator's tab polls its own job id until the deadline and
    reports `unknown` for an import that succeeded: this milestone's own defect, one layer up.

    Neither question can wedge the queue, which is why the claimed row is asked about with the
    reaper's budget rather than by its phase alone: a worker killed mid-import leaves a `running`
    row for ever and releases its session lock with its connection, so a claim OLDER than
    `worker.BUNDLE_IMPORT_TIMEOUT` belongs to `_reap_abandoned_import` and the retry an operator
    makes next is accepted. [M4.14 cycle 1, m414-c1-dim-waveE-02]
    """
    b = await _open(_resolve(body.path))
    if not await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1))", bundle_import.IMPORT_LOCK
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, IMPORT_IN_FLIGHT)
    try:
        queued = await conn.fetchrow(
            "SELECT id, detail->>'phase' AS phase, detail->>'bundle_version' AS version "
            "  FROM job_run "
            " WHERE name = $1 AND finished_at IS NULL "
            "   AND (detail->>'phase' = $2 "
            "        OR (detail->>'phase' = $3 "
            "            AND coalesce((detail->>'claimed_at')::timestamptz, started_at) "
            "                >= now() - ($4::float8 * interval '1 second'))) "
            " ORDER BY started_at LIMIT 1",
            IMPORT_JOB, QUEUED, RUNNING, IMPORT_CLAIM_BUDGET_S,
        )
        if queued is not None:
            state = "already running" if queued["phase"] != QUEUED else "already queued for import"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"bundle {queued['version']} is {state} as job {queued['id']} - read "
                "/api/admin/bundle/state",
            )
        report = await bundle_import.validate_for_install(conn, b, settings().artifacts_dir)
        if not report.ok:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"report": report.as_dict(), "text": report.render()},
            )
        # The path the operator named and not `b.root`, so the job imports the thing the request
        # was about. They differ for an archive and for decision 257's directory-holding-one-
        # archive, where `Bundle.open` returns the extracted tree - and the worker re-opening the
        # NAMED path reproduces those findings in the report it stores, rather than importing a
        # tree whose provenance the stored report no longer states. Re-opening costs nothing: an
        # extraction that carries BUNDLE.json is reused rather than redone.
        job_id = await conn.fetchval(
            "INSERT INTO job_run (name, detail) VALUES ($1, $2) RETURNING id",
            IMPORT_JOB,
            {"phase": QUEUED, "path": str(_resolve(body.path)),
             "bundle_version": report.bundle_version},
        )
    finally:
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtext($1))", bundle_import.IMPORT_LOCK
        )
    return {
        # The validation report's version, for `validate_bundle`'s reason: one derivation.
        "bundle_version": report.bundle_version,
        "job_id": job_id,
        "phase": QUEUED,
        "report": report.as_dict(),
        "text": report.render(),
        # Where the rest of this import is reported. Named in the payload rather than known by
        # the client alone, because the answer to "did it work?" is no longer in this response.
        "poll": "/api/admin/bundle/state",
    }
