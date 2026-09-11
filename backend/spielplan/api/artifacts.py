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

    Bundles and nothing else. This payload briefly also carried the worker's `job_run` outcomes,
    written while owner decision 2 was still open and its option (A) — "Data tab + Connectors card
    only" — was live. Decision 182 took option (B) instead: §6.6's System card, at
    `GET /api/admin/system`, is where an operator reads job health and backup status, and this
    page renders none of it (`frontend/src/routes/admin/data/+page.svelte` reads `bundles`,
    `active`, `loaded`, `restart_required` and `rebuild_set`). A payload with no reader is a
    second definition of the 36-hour rule waiting to disagree with the first, on a route the Data
    tab polls on a timer. [M4.7 ops-11; decision 182]
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
        "note": RESTART_REQUIRED,
    }
