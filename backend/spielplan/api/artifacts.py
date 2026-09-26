"""Artifact-bundle admin routes (§6.6 Data, §10). The wizard's bundle step (§3.1) is this same importer."""

from __future__ import annotations

import asyncio
import tarfile
import threading
from pathlib import Path
from typing import Any

# Module scope: `zstandard.ZstdError` is named in an `except` below.
import zstandard
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from spielplan.api.deps import DB, AdminUser
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import
from spielplan.models import basis

router = APIRouter(prefix="/api/admin/bundle", tags=["admin", "bundle"])

# The member-facing 409 during a swap window, shared with `api/rate.py` and `api/rank.py`: the
# backend re-pins within seconds (decision 497). `BundleImport.svelte` hard-codes its own copy.
RESTART_REQUIRED = (
    "Spielplan is switching to newly imported library data. Try again in a few seconds - if "
    "this keeps happening, it needs a restart."
)

# The member's half of a broken install: a restart cannot help, the files must be restored (§10).
# The operator's half is the log line.
RESTORE_REQUIRED = (
    "Spielplan's movie data is missing on this server, so nothing can be saved right now. An admin "
    "needs to restore it."
)

# `worker.JOBS`' name, as a literal: importing `spielplan.worker` runs its `logging.basicConfig`.
# `test_bundle_import_job.py` holds the two equal.
IMPORT_JOB = "bundle-import"

# `job_run.detail->>'phase'` values, not `artifact_bundle` states (decision 253): a placeholder row
# would claim the one seed slot and need `assert_staged` widened.
QUEUED = "queued"
RUNNING = "running"

# Seconds; mirrors `worker.BUNDLE_IMPORT_TIMEOUT`. A `running` claim older than this was abandoned.
IMPORT_CLAIM_BUDGET_S = 600.0

IMPORT_IN_FLIGHT = (
    "another bundle import is already running on this install - wait for it to finish and read "
    "/api/admin/bundle/state"
)


class BundleRef(BaseModel):
    path: str | None = None      # defaults to /data/import


def _resolve(path: str | None) -> Path:
    """The one boundary an admin-supplied path is held to (§6.6, §10 step 2)."""
    cfg = settings()
    target = Path(path) if path else cfg.import_dir
    target = target.resolve()
    # Under DATA_DIR by path, not by string prefix: `startswith('/data')` admits `/database`.
    if not target.is_relative_to(cfg.data_dir.resolve()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"bundle path must live under {cfg.data_dir}",
        )
    # Never the staging tree: `import_bundle` rmtrees `artifacts/<version>` before copying, deleting a
    # bundle that lives there.
    if target.is_relative_to(cfg.artifacts_dir.resolve()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{target} is inside {cfg.artifacts_dir}, which this app stages INTO - a bundle "
            f"imported from there is deleted by its own import. Put it under {cfg.import_dir}",
        )
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no bundle at {target}")
    # Only the three shapes `Bundle.open` knows: a directory, `.tar`, `.tar.zst`.
    if target.is_file() and not target.name.endswith((".tar", ".tar.zst")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{target.name} is not a bundle: a bundle is a directory, a .tar or a .tar.zst",
        )
    return target


# One extraction at a time: `_unpack` reuses one staging dir, and two concurrent presses corrupt it.
# A threading.Lock, held in the worker thread and bound to no event loop.
_EXTRACTING = threading.Lock()


def _extract(target: Path) -> bundle_import.Bundle:
    with _EXTRACTING:
        return bundle_import.Bundle.open(target)


async def _open(target: Path) -> bundle_import.Bundle:
    """Off the event loop: the first open extracts the archive (~0.6 s per GB). Every refusal becomes a
    400 that says so; anything wider (a KeyError) stays a bug."""
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
    """The newest import row that carries a phase (the loop's own rows do not), whatever its outcome:
    after a failure its report is the only copy."""
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
    """Active and loaded versions are reported apart: they disagree between §10's flip and the re-pin,
    which runs first (decision 497) so the read that reports the flip also loads it. Job health is
    the System card's (decision 182); `import_job` is only this import's phase."""
    await basis.refresh(request.app.state, conn)
    rows = await conn.fetch(
        "SELECT version, state, imported_at, activated_at FROM artifact_bundle "
        "ORDER BY imported_at DESC"
    )
    active = next((r["version"] for r in rows if r["state"] == "active"), None)
    store = request.app.state.artifacts
    # From the follower's memory, as `/api/config` reads it: a failed re-pin after a restage leaves the
    # version unchanged (decision 497).
    failed = basis.unloaded(request.app.state)
    return {
        "bundles": [dict(r) for r in rows],
        "active": active,
        "loaded": store.summary() if not store.is_empty else None,
        # §10's invariant, surfaced. Not while broken: the restore is then the only instruction that helps.
        "restart_required": failed or (active != store.version and not store.broken),
        # A broken store matches the active version but loaded nothing (data-03). Suppressed while a re-pin
        # has failed, where the restart is the one instruction.
        "broken": store.broken and not failed,
        "missing_path": str(store.root) if store.broken and not failed else None,
        "import_dir": str(settings().import_dir),
        "rebuild_set": list(bundle_import.REBUILD_SET),
        # The import the Data tab waits on, or the last one; see `_running_import`.
        "import_job": await _running_import(conn),
    }


@router.post("/validate")
async def validate_bundle(body: BundleRef, conn: DB, _: AdminUser) -> dict[str, Any]:
    """§10 step 1; writes nothing. Takes the connection because decisions 162 and 163 refuse on facts
    about this install, not the bundle."""
    b = await _open(_resolve(body.path))
    # `artifacts_dir`, so the staging-tree refusal is reachable here, where the decision is made.
    report = await bundle_import.validate_for_install(conn, b, settings().artifacts_dir)
    # The report's version: the one derivation (`validate.safe_version`).
    return {
        "bundle_version": report.bundle_version,
        "report": report.as_dict(),
        "text": report.render(),
    }


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_bundle(body: BundleRef, conn: DB, _: AdminUser) -> dict[str, Any]:
    """Validates here (in a thread, decision 287) and enqueues the minutes-long import as a worker job;
    202 with the `job_run` id. One import at a time: the advisory lock, then any queued or freshly
    claimed row, which covers the gap before the worker takes the lock."""
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
        # The path the operator named, not `b.root`, so the worker's report states the same provenance.
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
        "bundle_version": report.bundle_version,
        "job_id": job_id,
        "phase": QUEUED,
        "report": report.as_dict(),
        "text": report.render(),
        # The outcome is no longer in this response.
        "poll": "/api/admin/bundle/state",
    }
