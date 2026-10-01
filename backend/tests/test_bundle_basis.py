"""Decision 497: the backend follows the active bundle; a restart is owed only when a load fails.
The `app` fixture turns the follow timer off, so all but one test re-pin on demand.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from spielplan import worker
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import
from spielplan.ledger import hyperparams
from spielplan.models import basis
from spielplan.models.artifacts import active_bundle_key
from spielplan.scoring import backbone
from tests.fixtures import make_bundle as fx
from tests.helpers import admin_client, tick_one


async def _import(db, root: Path) -> None:
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()


def _models_only(root: Path) -> Path:
    """Decision 162's re-import shape: the seed bundle minus its content databases, re-inventoried."""
    (root / "content.sqlite").unlink()
    (root / "reviews.sqlite").unlink()
    fx.reinventory(root)
    return root


async def test_a_first_import_is_served_without_a_restart(app, db, tmp_path):
    """Every surface is read after the poll that reports the
    flip, because that poll is where the re-pin runs."""
    admin = await admin_client(app)
    state = admin._transport.app.state
    assert state.artifacts.is_empty, "the app under test has to boot bundle-less (section 3.1)"
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")

    answer = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert answer.status_code == 202, answer.text
    await tick_one(worker.BUNDLE_IMPORT_JOB, due=False)

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

    tap = await admin.post("/api/rate/place", json={"card_token": "whatever", "tier": 2})
    reason = tap.json().get("detail", {}).get("reason")
    assert reason not in ("bundle_swapped", "bundle_broken"), (
        f"a Rate tap after the import is still refused for the basis: {tap.text}"
    )


async def test_the_repin_replaces_store_backbone_and_constants_together(app, db, tmp_path):
    """A re-pin that moved only the store would be worse than none: default constants change `hp_digest`
    and invalidate every cached fit. `load_for` is cached per file stamp, so identity is comparable."""
    admin = await admin_client(app)
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
    admin = await admin_client(app)
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
    """A restage keeps the version, which is why the re-pin compares `activated_at` as well."""
    admin = await admin_client(app)
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


async def test_a_restage_whose_load_fails_is_reported_as_the_restart_it_owes(
    app, db, tmp_path, monkeypatch
):
    """A restage moves `activated_at` and not the version,
    so a version comparison missed the owed restart."""
    admin = await admin_client(app)
    state = admin._transport.app.state
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")
    await _import(db, root)
    shutil.rmtree(settings().artifacts_dir / "test-v1")
    basis.pin(state, await basis.load(db, settings().artifacts_dir))
    assert state.artifacts.broken and state.artifacts.version == "test-v1"

    real = basis.load

    async def refuses(conn, artifacts_dir):
        raise OSError("simulated: manifest.json is unreadable")

    monkeypatch.setattr(basis, "load", refuses)
    await _import(db, root)

    stuck = (await admin.get("/api/admin/bundle/state")).json()
    assert stuck["active"] == "test-v1" and state.artifacts.version == "test-v1"
    assert stuck["restart_required"] is True, "the one state decision 497 owes a restart for"
    assert stuck["broken"] is False and stuck["missing_path"] is None, (
        "the page asked for the restore the restage had just done"
    )
    assert (await admin.get("/api/config")).json()["restart_required"] is True

    monkeypatch.setattr(basis, "load", real)
    settled = (await admin.get("/api/admin/bundle/state")).json()
    assert settled["restart_required"] is False and settled["broken"] is False
    assert settled["loaded"]["version"] == "test-v1"


async def test_a_load_that_fails_keeps_serving_and_says_a_restart_is_owed(
    app, db, tmp_path, monkeypatch, caplog
):
    """The failure is logged once per row, not once per tick, and the next ask after the fix loads."""
    admin = await admin_client(app)
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

    # Put back by hand: `monkeypatch.undo()` would also unwind
    # the `app` fixture's DATA_DIR under a running app.
    monkeypatch.setattr(basis, "load", real)
    assert await basis.refresh(state) is True
    assert (await admin.get("/api/config")).json()["restart_required"] is False
    assert state.artifacts.version == "test-v1"


async def test_the_timer_repins_without_any_request(app, db, tmp_path, monkeypatch):
    """A flip nobody polls for is loaded within `FOLLOW_SECONDS`,
    so a first tap does not meet the swap's 409."""
    admin = await admin_client(app)
    state = admin._transport.app.state
    monkeypatch.setattr(basis, "FOLLOW_SECONDS", 0.05)
    basis.start(state, settings().artifacts_dir)
    await _import(db, fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1"))

    deadline = time.monotonic() + 10
    while state.artifacts.version != "test-v1" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert state.artifacts.version == "test-v1", "the timer never loaded the flipped bundle"
    assert not state.artifacts.is_empty
