"""Artifact-bundle admin routes. Spec v2.1 §6.6 (Data tab), §3.1 (wizard step 3), §10.

§3.1: the wizard's bundle step *is* this importer — "the same importer the §6.6 Data tab
exposes (that one page is M0 scope)". There is one implementation and two entry points.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from spielplan.api.deps import DB, AdminUser
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import

router = APIRouter(prefix="/api/admin/bundle", tags=["admin", "bundle"])


class BundleRef(BaseModel):
    path: str | None = None      # defaults to /data/import


def _resolve(path: str | None) -> Path:
    cfg = settings()
    target = Path(path) if path else cfg.import_dir
    target = target.resolve()
    # An admin-only route that takes a filesystem path still gets a boundary: the bundle must
    # live under DATA_DIR, so a typo cannot make the app read arbitrary host files.
    if not str(target).startswith(str(cfg.data_dir.resolve())):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"bundle path must live under {cfg.data_dir}",
        )
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no bundle at {target}")
    return target


@router.get("/state")
async def bundle_state(conn: DB, _: AdminUser, request: Request) -> dict[str, Any]:
    """The Data tab's state.

    Reports the DB-active version and the *loaded* version separately, because §10's swap
    sequence ends in "restart backend + worker" and between the flip and that restart they
    legitimately disagree. Collapsing them into one field made the page say "no bundle is
    active" immediately after a successful import — which is the opposite of what happened.
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
        "restart_required": active != store.version,
        "import_dir": str(settings().import_dir),
        "rebuild_set": list(bundle_import.REBUILD_SET),
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
    b = bundle_import.Bundle.open(_resolve(body.path))
    report = await bundle_import.validate_for_install(conn, b)
    return {"bundle_version": b.version, "report": report.as_dict(), "text": report.render()}


@router.post("/import")
async def import_bundle(body: BundleRef, conn: DB, _: AdminUser) -> dict[str, Any]:
    """Validate -> load -> stage -> flip. On any validation failure nothing is written."""
    b = bundle_import.Bundle.open(_resolve(body.path))
    report = await bundle_import.import_bundle(conn, b, settings().artifacts_dir)
    if not report.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"report": report.as_dict(), "text": report.render()},
        )
    return {
        "bundle_version": b.version,
        "report": report.as_dict(),
        "text": report.render(),
        # §10 swap sequence step 5. Said plainly because the operator has to do it.
        "restart_required": True,
        "note": "restart backend and worker — no process may score or refit with a loaded "
                "bundle version different from the active row",
    }
