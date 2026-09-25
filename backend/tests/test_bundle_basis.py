"""Decision 497: the backend follows the active bundle, and §10's restart is owed only when a load
fails. Spec v2.1 §2 ("`docker compose up` + the setup wizard is the whole install"), §3.1, §4.3,
§10; owner instruction of 2026-09-25 after the first household user test.

The first household's wizard ended on "no bundle imported", a 409 on every Rate and Rank fit and
`loaded: none` on the Data tab, until somebody with a shell restarted two containers. These tests
drive the three imports decision 497 names - the first one, a models-only re-import of a new
version, and decision 253's restage - and read the answer off the surfaces that told the household
the wrong thing: the Data tab's state, the shell's `/api/config`, `/api/health` and a Rate tap.

The `app` fixture turns the five-second timer off (tests/conftest.py), so everything here is
driven by the on-demand re-pin or by `basis.refresh` itself, except the one test whose subject is
the timer. Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from spielplan import worker
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import
from spielplan.ledger import hyperparams
from spielplan.models import basis
from spielplan.models.artifacts import active_bundle_key
from spielplan.scoring import backbone
from tests.fixtures import make_bundle as fx

ADMIN_PASSWORD = "an-admin-password"


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _tick() -> None:
    """One pass of the real loop with the registry cut to the import, as `test_bundle_import_job`
    drives it: the tick is production code, the rest of §5.3's table is not the subject."""
    row = next(j for j in worker.JOBS if j.name == worker.BUNDLE_IMPORT_JOB)
    jobs = worker.JOBS
    worker.JOBS = (row,)
    try:
        await worker._tick(time.monotonic(), datetime.now(UTC), {row.name: time.monotonic()}, {})
    finally:
        worker.JOBS = jobs


async def _import(db, root: Path) -> None:
    """The importer called the way the worker calls it, for the tests whose subject is the re-pin
    rather than the route that queues it."""
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()


def _models_only(root: Path) -> Path:
    """decision 162's re-import shape: the seed bundle minus its two content databases, with the
    inventory re-written so BUNDLE.json still describes the tree."""
    (root / "content.sqlite").unlink()
    (root / "reviews.sqlite").unlink()
    fx.reinventory(root)
    return root


async def test_a_first_import_is_served_without_a_restart(app, db, tmp_path):
    """The household's own sequence: wizard, import, and the bundle in use - with no shell.

    The import goes through the route and the worker's tick exactly as the Data tab drives it, and
    nothing restarts: the app below is the one that booted bundle-less. Every surface that told the
    household "no bundle" is read after the poll that reports the flip, because that poll is where
    decision 497 re-pins.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    assert state.artifacts.is_empty, "the app under test has to boot bundle-less (section 3.1)"
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")

    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text
    await _tick()

    done = (await admin.get("/api/admin/bundle/state")).json()
    assert done["import_job"]["phase"] == "active", done["import_job"]
    assert done["active"] == "test-v1"
    assert done["loaded"] is not None and done["loaded"]["version"] == "test-v1", done["loaded"]
    assert done["restart_required"] is False, "the Data tab still asks for a restart"

    config = (await admin.get("/api/config")).json()
    assert config["has_bundle"] is True, "the header would still say 'no bundle imported'"
    assert config["bundle"]["version"] == "test-v1"
    assert config["restart_required"] is False

    assert (await admin.get("/api/health")).json()["bundle"] == "test-v1"

    tap = await admin.post("/api/rate/verdict", json={"card_token": "whatever", "value": 2})
    reason = tap.json().get("detail", {}).get("reason")
    assert reason not in ("bundle_swapped", "bundle_broken"), (
        f"a Rate tap after the import is still refused for the basis: {tap.text}"
    )


async def test_the_repin_replaces_store_backbone_and_constants_together(app, db, tmp_path):
    """The three attributes are one basis, and a re-pin that moved only the store would be worse
    than none: the constants left at the boot's DEFAULTS give a different `hp_digest` from the
    worker's fits, which invalidates every cached fit in the install, and a Backbone left empty
    scores every title from the Cold Tower alone.

    Compared by identity against the loaders the boot uses (`load_for` is cached per file stamp,
    so the same file is the same object) and by value for the constants, and the key against the
    row, which is what the next comparison is made against.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    await _import(db, fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1"))

    assert await basis.refresh(state) is True
    assert state.artifacts.version == "test-v1" and not state.artifacts.is_empty
    assert state.backbone is backbone.load_for(state.artifacts)
    assert state.hyperparams == hyperparams.load(state.artifacts)[0]
    assert state.basis_key == await active_bundle_key(db)

    assert await basis.refresh(state) is False, "a second ask with nothing flipped re-pinned"


async def test_a_models_only_reimport_of_a_new_version_is_served_without_a_restart(
    app, db, tmp_path
):
    """The owner's next step on the live install, and the case the draft decision left to a
    restart: a LOADED bundle replaced by another. Decision 162's shape - models, no content - and
    the backend moves from the outgoing basis to the incoming one on the Data tab's read.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    await _import(db, fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1"))
    await basis.refresh(state)
    outgoing = state.backbone
    assert state.artifacts.version == "test-v1"

    second = fx.make_bundle(tmp_path / "import" / "test-v2", version="test-v2")
    await _import(db, _models_only(second))

    swapped = (await admin.get("/api/admin/bundle/state")).json()
    assert swapped["active"] == "test-v2"
    assert swapped["loaded"]["version"] == "test-v2", swapped["loaded"]
    assert swapped["restart_required"] is False
    assert state.backbone is backbone.load_for(state.artifacts)
    assert state.backbone is not outgoing, "the store moved and the Backbone stayed behind"
    assert (await admin.get("/api/config")).json()["bundle"]["version"] == "test-v2"


async def test_a_restaged_broken_install_is_served_without_a_restart(app, db, tmp_path):
    """Decision 253's repair re-imports the ACTIVE version into its own directory, so the row's
    version never moves - which is why the re-pin compares `activated_at` as well. A process that
    pinned the broken store would otherwise keep serving it after the files came back.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")
    await _import(db, root)
    shutil.rmtree(settings().artifacts_dir / "test-v1")
    # The boot of an install whose files are gone: a broken store carrying the active version.
    basis.pin(state, await basis.load(db, settings().artifacts_dir))
    assert state.artifacts.broken and state.artifacts.version == "test-v1"

    await _import(db, root)

    restaged = (await admin.get("/api/admin/bundle/state")).json()
    assert restaged["broken"] is False, "the restaged files are there and this process says not"
    assert restaged["loaded"]["version"] == "test-v1"
    assert not state.artifacts.broken and not state.artifacts.is_empty


async def test_a_load_that_fails_keeps_serving_and_says_a_restart_is_owed(
    app, db, tmp_path, monkeypatch, caplog
):
    """The one case the restart is still for, reported where the household reads it, and retried.

    While the load raises, the Data tab reports `restart_required` and the shell's `/config` says
    so rather than "no bundle imported"; the failure is logged once for the row and not once per
    tick; and the next ask after the cause is gone loads the bundle with no restart at all.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    await _import(db, fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1"))

    real = basis.load

    async def refuses(conn, artifacts_dir):
        raise OSError("simulated: manifest.json is unreadable")

    monkeypatch.setattr(basis, "load", refuses)
    with caplog.at_level(logging.ERROR, logger="spielplan"):
        assert await basis.refresh(state) is False
        assert await basis.refresh(state) is False
    failures = [r for r in caplog.records if "could not load it" in r.getMessage()]
    assert len(failures) == 1, "a failing load is logged once per row, not once per tick"

    stuck = (await admin.get("/api/admin/bundle/state")).json()
    assert stuck["active"] == "test-v1" and stuck["loaded"] is None
    assert stuck["restart_required"] is True
    config = (await admin.get("/api/config")).json()
    assert config["has_bundle"] is False and config["restart_required"] is True, config

    # Put back by hand and not with `monkeypatch.undo()`, which would also unwind the `app`
    # fixture's own DATA_DIR and working directory under a running app.
    monkeypatch.setattr(basis, "load", real)
    assert await basis.refresh(state) is True
    assert (await admin.get("/api/config")).json()["restart_required"] is False
    assert state.artifacts.version == "test-v1"


async def test_the_timer_repins_without_any_request(app, db, tmp_path, monkeypatch):
    """The other trigger: a flip nobody polls for - an import started from a phone that was then
    put away - is loaded within `FOLLOW_SECONDS` anyway, so a member's first tap does not meet the
    swap's 409. Armed here at a test-sized interval; the lifespan stops it on the way out.
    """
    admin = await _admin(app)
    state = admin._transport.app.state
    monkeypatch.setattr(basis, "FOLLOW_SECONDS", 0.05)
    basis.start(state, settings().artifacts_dir)
    await _import(db, fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1"))

    deadline = time.monotonic() + 10
    while state.artifacts.version != "test-v1" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert state.artifacts.version == "test-v1", "the timer never loaded the flipped bundle"
    assert not state.artifacts.is_empty
