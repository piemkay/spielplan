"""A broken bundle is made by pointing the active row at a never-staged version: deleting a
staged directory fails on Windows, where the mapped backbone.npz holds a handle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from spielplan import worker
from spielplan.api import artifacts as artifacts_api
from spielplan.api import deps
from spielplan.api import home as home_api
from spielplan.api import tonight as tonight_api
from spielplan.core.config import settings
from spielplan.db import pool
from spielplan.importer import bundle as bundle_import
from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.hyperparams import load as load_hp
from spielplan.models import artifacts
from spielplan.models.artifacts import ArtifactStore
from spielplan.rank import tiers
from spielplan.scoring import backbone as bb
from tests.fixtures import make_bundle as fx
from tests.helpers import insert_user

LABELS = ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1))
SERIES_LABELS = ((6, 2), (7, 0))


def _e_hat(seed: int) -> bytes:
    """A `title_placement.e_hat` blob in the column's own convention: 64 x float32 LE."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=64).astype("<f4").tobytes()


def _as_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4").astype(float)


def _as_fitted(blob: bytes) -> np.ndarray:
    """The placement's unit direction, as the fit reads a tower-only title (decision 471)."""
    vector = _as_vector(blob)
    return vector / np.linalg.norm(vector)


async def _import(db, root: Path, artifacts_root: Path, *, version: str = "test-v1",
                  models_only: bool = False) -> None:
    fx.make_bundle(root, version=version)
    if models_only:
        (root / "content.sqlite").unlink()
        (root / "reviews.sqlite").unlink()
        # BUNDLE.json must still describe the tree, or the import refuses it before writing a row.
        fx.reinventory(root)
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )
    assert report.ok, report.render()


async def _place(db, title_id: int, version: str, seed: int) -> None:
    await db.execute(
        """
        INSERT INTO title_placement
            (title_id, bundle_version, e_hat, b_hat, contract_sha256, tower_sha256, input_dim,
             blocks_present, blocks_dropped, blocks_imputed, nnz)
        VALUES ($1, $2, $3, 0.0, 'sha-contract', 'sha-tower', 64, '{}', '{}', '{}', 64)
        ON CONFLICT (title_id, bundle_version) DO UPDATE SET e_hat = EXCLUDED.e_hat
        """,
        title_id, version, _e_hat(seed),
    )


async def _make_active(db, version: str) -> None:
    """The partial unique index allows one active row, so supersede first. A version with no
    staged directory is the broken-bundle state."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ($1, '{}'::jsonb, 'validated', 'model') ON CONFLICT (version) DO NOTHING",
        version,
    )
    await db.execute("UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active'")
    await db.execute(
        "UPDATE artifact_bundle SET state = 'active', activated_at = now() WHERE version = $1",
        version,
    )


@pytest.fixture
async def worker_env(db, pg_url, tmp_path, monkeypatch):
    """`DATA_DIR` is `tmp_path` itself, so `artifacts_dir` is where `_import` staged the bundle."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    await pool.open_pool(pg_url)
    try:
        yield tmp_path
    finally:
        await pool.close_pool()
        settings.cache_clear()
        bb.forget_cached()


@pytest.fixture
async def installed(db, tmp_path):
    """Returns `(store, user_id)`."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    return ArtifactStore.open(tmp_path / "artifacts" / "test-v1", "test-v1"), user_id


def _job(name: str):
    return next(j for j in worker.JOBS if j.name == name)


async def test_standard_embeddings_reads_the_placements_of_the_version_it_was_given(db, tmp_path):
    """Asserted on coordinates, not through a fit: a fitted `v` blends many of them."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)
    assert await artifacts.active_bundle_version(db) == "test-v2"
    await db.execute("DELETE FROM title_placement WHERE title_id = 8")
    await _place(db, 8, "test-v1", seed=11)
    await _place(db, 8, "test-v2", seed=22)

    old, old_seen = await observations.resolve_embeddings(
        observations.standard_embeddings(db, None, bundle_version="test-v1"), [8]
    )
    new, new_seen = await observations.resolve_embeddings(
        observations.standard_embeddings(db, None, bundle_version="test-v2"), [8]
    )
    assert old_seen[0] and new_seen[0], "a version was threaded and no placement was found"
    assert np.allclose(old[0], _as_fitted(_e_hat(11))), (
        "the version asked for is not the version read"
    )
    assert np.allclose(new[0], _as_fitted(_e_hat(22)))
    assert not np.allclose(old[0], new[0]), (
        "the two bundles' placements are identical, so this test cannot tell them apart"
    )


async def test_a_source_with_no_version_threaded_falls_back_to_the_active_row_as_the_bundle_less_path(
    db, tmp_path
):
    """No version threaded: the active row supplies coordinates, and a fit stamps NULL so
    `load_cache` accepts a bundle-less fit across restarts."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)
    await db.execute("DELETE FROM title_placement WHERE title_id = 8")
    await _place(db, 8, "test-v1", seed=11)
    await _place(db, 8, "test-v2", seed=22)

    matrix, embedded = await observations.resolve_embeddings(
        observations.standard_embeddings(db, None), [8]
    )
    assert embedded[0], "the fallback found no placement at all"
    assert np.allclose(matrix[0], _as_fitted(_e_hat(22))), (
        "the no-version branch must read the ACTIVE row's placements"
    )

    # Superseded, not deleted: a row that was ever a basis is never deleted (decision 249).
    await db.execute("UPDATE artifact_bundle SET state = 'superseded'")
    user_id = await insert_user(db, "Ana")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    report = await refit.refit_user(db, user_id=user_id, kind="movie", hp=DEFAULTS)
    assert report.fitted, report.as_dict()
    assert await db.fetchval(
        "SELECT bundle_version FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    ) is None, "a fit in no basis must stamp NULL, not the active row"
    assert await refit.load_cache(
        db, user_id=user_id, kind="movie", hp=DEFAULTS, lock=False
    ) is not None, "NULL == NULL: a bundle-less household keeps its cached fit (§3.1)"


async def test_the_rebuild_fits_against_the_staged_bundle_and_stamps_the_staged_version(
    db, tmp_path
):
    """The version is threaded at two seams, coordinates read and stamp written; a fit can carry
    the staged stamp over the outgoing coordinates, so both are asserted."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    # `make_bundle` places every title identically in both versions, so title 8 (cold-masked,
    # tower-only) is overwritten in the outgoing version to make the bases differ.
    await observations.record_verdict(db, user_id=user_id, title_id=8, value=1)
    await _place(db, 8, "test-v1", seed=11)

    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)

    stamped = await db.fetchval(
        "SELECT bundle_version FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    )
    assert stamped == "test-v2", (
        f"step 3 stamped {stamped!r}: it fits against the STAGED bundle and has to say so"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM user_vector WHERE bundle_version = 'test-v2'"
    ) > 0, "step 1 wrote no fold-in vector against the staged basis"

    store = ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2")
    hp, _notes = load_hp(store)
    bb.forget_cached()
    basis = bb.load_for(store)

    async def _v_over(version: str) -> np.ndarray:
        """Stamped `test-v2` on purpose: `test-v1` would be refused for its version, not its numbers."""
        report = await refit.refit_user(
            db, user_id=user_id, kind="movie", hp=hp, bundle_version="test-v2",
            embeddings=observations.standard_embeddings(db, basis, bundle_version=version),
        )
        assert report.fitted, report.as_dict()
        cached = await refit.load_cache(db, user_id=user_id, kind="movie", hp=hp, lock=False)
        assert cached is not None, f"the cache refused the {version} refit this test just made"
        return np.asarray(cached.v, dtype=float)

    step3 = await refit.load_cache(db, user_id=user_id, kind="movie", hp=hp, lock=False)
    assert step3 is not None, "the stamp is right and the cache still refused the rebuild's fit"
    v_step3 = np.asarray(step3.v, dtype=float)
    v_staged = await _v_over("test-v2")
    v_outgoing = await _v_over("test-v1")

    assert not np.allclose(v_staged, v_outgoing), (
        "the two bundles' coordinates are identical, so this test cannot tell the bases apart"
    )
    assert np.allclose(v_step3, v_staged), (
        "step 3 stamped the staged version over another basis' coordinates: "
        f"||v_step3 - v_staged|| = {float(np.linalg.norm(v_step3 - v_staged)):.4f} against "
        f"||v_step3 - v_outgoing|| = {float(np.linalg.norm(v_step3 - v_outgoing)):.4f}"
    )


async def test_the_cache_accepts_the_staged_fit_after_the_flip_and_the_first_tap_refits_nothing(
    db, tmp_path
):
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)

    store = ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2")
    hp, _notes = load_hp(store)
    cache = await refit.load_cache(db, user_id=user_id, kind="movie", hp=hp, lock=False)
    assert cache is not None, (
        "the cache refused the fit the rebuild had just made: its stamp does not name the "
        "active row"
    )

    bb.forget_cached()
    applied = await refit.update_incrementally_reporting(
        db, user_id=user_id, kind="movie", title_ids=[1], hp=hp,
        embeddings=observations.standard_embeddings(
            db, bb.load_for(store), bundle_version="test-v2"
        ),
    )
    assert applied is not None and applied["applied"] is True, applied
    assert applied["refit"] is False, f"the first tap after the flip refitted: {applied}"


async def test_a_tap_holding_the_outgoing_basis_queues_rather_than_updating_the_staged_fit(
    db, tmp_path
):
    """The tap waited on the import's advisory lock, so it sees the staged stamp and new active row
    while this process still holds the outgoing Backbone. It must queue, not write."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    outgoing = ArtifactStore.open(tmp_path / "artifacts" / "test-v1", "test-v1")
    hp, _notes = load_hp(outgoing)

    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)
    assert await db.fetchval(
        "SELECT bundle_version FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    ) == "test-v2", "the rebuild did not stamp the staged version; this test proves nothing"

    before = await db.fetchval(
        "SELECT s FROM ledger_state WHERE user_id=$1 AND kind='movie' AND title_id=1", user_id
    )
    await db.execute("UPDATE ledger_cutpoints SET refit_requested_at = NULL WHERE user_id = $1",
                     user_id)
    bb.forget_cached()
    stale = await refit.update_incrementally_reporting(
        db, user_id=user_id, kind="movie", title_ids=[1], hp=hp,
        embeddings=observations.standard_embeddings(
            db, bb.load_for(outgoing), bundle_version="test-v1"
        ),
        bundle_version="test-v1",
    )
    assert stale == {"applied": False, "reason": refit.QUEUED_REASON}, (
        f"a tap in the outgoing basis was applied against the staged fit: {stale}"
    )
    assert await db.fetchval(
        "SELECT s FROM ledger_state WHERE user_id=$1 AND kind='movie' AND title_id=1", user_id
    ) == before, "the mixed-basis update was refused and the row moved anyway"
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'",
        user_id,
    ) is not None, "the refused tap owes a full refit and nobody was told"

    staged = ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2")
    bb.forget_cached()
    fresh = await refit.update_incrementally_reporting(
        db, user_id=user_id, kind="movie", title_ids=[1], hp=load_hp(staged)[0],
        embeddings=observations.standard_embeddings(
            db, bb.load_for(staged), bundle_version="test-v2"
        ),
        bundle_version="test-v2",
    )
    assert fresh is not None and fresh["applied"] is True and fresh["refit"] is False, fresh


async def test_every_fitted_pair_carries_the_version_its_basis_came_from_after_a_models_only_reimport(
    db, tmp_path
):
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    patrick = await insert_user(db, "Patrick", "admin")
    ana = await insert_user(db, "Ana")
    await db.execute("UPDATE title SET is_owned = true")
    for user_id in (patrick, ana):
        for title_id, value in LABELS + SERIES_LABELS:
            await observations.record_verdict(
                db, user_id=user_id, title_id=title_id, value=value
            )

    await _import(db, tmp_path / "b2", tmp_path / "artifacts",
                  version="test-v2", models_only=True)

    rows = await db.fetch("SELECT user_id, kind, bundle_version FROM ledger_fit ORDER BY 1, 2")
    assert {(r["user_id"], r["kind"]) for r in rows} == {
        (patrick, "movie"), (patrick, "series"), (ana, "movie"), (ana, "series")
    }, [dict(r) for r in rows]
    assert {r["bundle_version"] for r in rows} == {"test-v2"}, [dict(r) for r in rows]


async def test_every_read_path_reports_the_one_active_version(db, tmp_path):
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    store = ArtifactStore.open(tmp_path / "artifacts" / "test-v1", "test-v1")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifacts=store)))

    assert await artifacts.active_bundle_version(db) == "test-v1"
    assert await refit.active_bundle_version(db) == "test-v1"
    assert await tonight_api._bundle_version(db) == "test-v1"
    assert await home_api._bundle(request, db) == "test-v1"


async def test_a_worker_model_job_whose_bundle_is_not_the_active_row_refuses_and_advances_nothing(
    db, worker_env, installed, monkeypatch
):
    """The flip is injected between `_active_store` loading the store and resolving the active row."""
    _store, _user_id = installed
    flipped: list[str] = []
    real = ArtifactStore.load_active

    async def flips_under_us(conn, artifacts_dir):
        store = await real(conn, artifacts_dir)
        if not flipped:
            flipped.append(str(store.version))
            await _make_active(db, "test-v2")
        return store

    monkeypatch.setattr(ArtifactStore, "load_active", flips_under_us)
    placed = await db.fetchval("SELECT count(*) FROM title_placement")
    with pytest.raises(RuntimeError, match="restart backend and worker"):
        await _job("placement-reconciliation").run()

    assert flipped == ["test-v1"], (
        f"the flip has to land inside the job, after its load: {flipped}"
    )
    assert await db.fetchval("SELECT count(*) FROM title_placement") == placed


async def test_the_refit_entrypoint_refuses_a_stale_bundle_and_names_both_versions(
    db, worker_env, installed, monkeypatch
):
    """Both versions in the message: an operator needs which process is behind, and on what."""
    _store, user_id = installed
    await _job("ledger-map-refit").run()
    before = await db.fetchval(
        "SELECT fitted_at FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    )
    assert before is not None, "the first refit wrote no fit, so nothing here can be stale"

    real = ArtifactStore.load_active

    async def loads_the_outgoing_bundle(conn, artifacts_dir):
        store = await real(conn, artifacts_dir)
        await _make_active(db, "test-v2")
        return store

    monkeypatch.setattr(ArtifactStore, "load_active", loads_the_outgoing_bundle)
    with pytest.raises(RuntimeError) as caught:
        await _job("ledger-map-refit").run()

    message = str(caught.value)
    assert "test-v1" in message and "test-v2" in message, message
    assert await db.fetchval(
        "SELECT fitted_at FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    ) == before, "a refused refit advanced the fit it refused to make"


async def test_a_scoring_request_on_a_stale_bundle_answers_409_with_the_restart_wording(
    db, app, tmp_path
):
    """Asserted on `reason`: Rate already answers 409 for a stale card."""
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    await db.execute("UPDATE title SET is_owned = true")
    # The app pinned the empty store at boot; pin the one it would have loaded.
    client._transport.app.state.artifacts = ArtifactStore.open(
        tmp_path / "artifacts" / "test-v1", "test-v1"
    )
    await _make_active(db, "test-v2")

    verdict = await client.post(
        "/api/rate/place", json={"card_token": "whatever", "tier": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_swapped", "message": deps.RESTART_REQUIRED
    }, verdict.json()

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.status_code == 409, drop.text
    assert drop.json()["detail"] == {
        "reason": "bundle_swapped", "message": deps.RESTART_REQUIRED
    }, drop.json()
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0, (
        "the refusal has to land before the write, never after it (M4.10 finding 8)"
    )


async def test_a_bundle_less_install_passes_the_invariant_rather_than_refusing(db, worker_env):
    """§3.1: None == None passes every guard."""
    ArtifactStore.empty().assert_matches(None)
    ArtifactStore.empty().assert_not_broken()
    assert await artifacts.active_bundle_version(db) is None

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(artifacts=ArtifactStore.empty()))
    )
    await deps.assert_active_basis(request, db)

    await insert_user(db, "Patrick", "admin")
    for name in ("ledger-map-refit", "fold-in-user-vectors", "placement-reconciliation",
                 "tier-set-refit"):
        await _job(name).run()


async def test_a_broken_store_carries_the_active_version_and_still_refuses_on_its_own_flag(
    db, tmp_path
):
    """THE TRAP: a broken store carries the active version, so `assert_matches` passes; the refusal
    must rest on the flag, and `is_empty` stays True."""
    await _make_active(db, "test-v1")
    store = await ArtifactStore.load_active(db, tmp_path / "artifacts")

    assert store.version == "test-v1", "a broken install's stamp has to name the active row"
    assert store.broken is True
    assert store.is_empty is True, (
        "§3.1's surfaces read is_empty and have to keep their no-bundle state"
    )
    # This call must NOT raise.
    store.assert_matches(await artifacts.active_bundle_version(db))
    with pytest.raises(RuntimeError, match="does not exist"):
        store.assert_not_broken()
    with pytest.raises(RuntimeError, match="no artifact bundle loaded"):
        store.path("backbone.npz")


def test_the_two_refusals_rate_and_rank_render_speak_the_member_register():
    """Rate and Rank show a 409's `message` verbatim to the member (decision 486)."""
    for message in (deps.RESTART_REQUIRED, deps.RESTORE_REQUIRED):
        for noun in ("bundle", "basis", "refit", "process", "/data", "ledger", "fold-in"):
            assert noun not in message.lower(), f"{noun!r} reaches a member in {message!r}"
    assert "restore" in deps.RESTORE_REQUIRED
    assert "restart" in deps.RESTART_REQUIRED


async def test_a_fitting_request_on_a_broken_bundle_answers_409_and_writes_nothing(
    db, app, tmp_path
):
    """The version check cannot catch a broken store, which carries the active version on purpose.
    Asserted on the absent row too: a refusal after the write would lose the tap."""
    client = app()
    assert (await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )).status_code == 201
    await _make_active(db, "test-v1")
    store = await ArtifactStore.load_active(db, tmp_path / "artifacts")
    assert store.broken and store.version == "test-v1"
    # §10's comparison passes for this store.
    store.assert_matches(await artifacts.active_bundle_version(db))
    client._transport.app.state.artifacts = store

    verdict = await client.post(
        "/api/rate/place", json={"card_token": "whatever", "tier": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_broken", "message": deps.RESTORE_REQUIRED
    }, verdict.json()

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.status_code == 409, drop.text
    assert drop.json()["detail"] == {
        "reason": "bundle_broken", "message": deps.RESTORE_REQUIRED
    }, drop.json()
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0, (
        "the refusal has to land before the write, never after it (M4.10 finding 8)"
    )
    assert await db.fetchval("SELECT count(*) FROM ledger_state") == 0, (
        "a route fitted in a basis whose files are gone"
    )


async def test_a_process_that_is_both_stale_and_broken_is_diagnosed_by_the_caller_that_asks(
    db, app, tmp_path, worker_env
):
    """Stale AND broken: the request path answers the swap (its store is pinned once, a restart
    fixes it); the worker reloads per job, so only the missing directory is left to report."""
    client = app()
    assert (await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )).status_code == 201
    await _make_active(db, "gone-v1")
    pinned = await ArtifactStore.load_active(db, tmp_path / "artifacts")
    assert pinned.broken and pinned.version == "gone-v1"
    await _make_active(db, "gone-v2")
    client._transport.app.state.artifacts = pinned

    verdict = await client.post(
        "/api/rate/place", json={"card_token": "whatever", "tier": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_swapped", "message": deps.RESTART_REQUIRED
    }, "the request path holds a pinned store, so the restart is the action that fixes it"

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.json()["detail"]["reason"] == "bundle_swapped", drop.json()

    # The worker reloads, so it holds gone-v2 against gone-v2: only the flag is left to report.
    with pytest.raises(RuntimeError, match="does not exist"):
        await worker._active_store(db)


async def test_the_model_jobs_refuse_a_broken_bundle_and_leave_ledger_fit_where_it_was(
    db, worker_env, installed
):
    _store, user_id = installed
    await _job("ledger-map-refit").run()
    before = dict(
        await db.fetchrow(
            "SELECT fitted_at, n_observed, bundle_version FROM ledger_fit "
            " WHERE user_id = $1 AND kind = 'movie'",
            user_id,
        )
    )
    board = [
        (r["title_id"], r["s"])
        for r in await db.fetch(
            "SELECT title_id, s FROM ledger_state WHERE user_id = $1 AND kind = 'movie' "
            "ORDER BY 1", user_id,
        )
    ]
    assert before["fitted_at"] is not None and len(board) > 1
    assert len({s for _t, s in board}) > 1, (
        "the first fit already gives every title one score, so a zero-basis refit would be "
        "indistinguishable from it"
    )

    await _make_active(db, "never-staged")

    with pytest.raises(RuntimeError, match="is active but"):
        await _job("ledger-map-refit").run()

    after = dict(
        await db.fetchrow(
            "SELECT fitted_at, n_observed, bundle_version FROM ledger_fit "
            " WHERE user_id = $1 AND kind = 'movie'",
            user_id,
        )
    )
    assert after == before, f"the fit moved under a basis that is not there: {before} -> {after}"
    assert [
        (r["title_id"], r["s"])
        for r in await db.fetch(
            "SELECT title_id, s FROM ledger_state WHERE user_id = $1 AND kind = 'movie' "
            "ORDER BY 1", user_id,
        )
    ] == board, "the board was rewritten from coordinates that do not exist"


async def test_a_refused_job_leaves_the_refit_owed_rather_than_swallowing_it(
    db, worker_env, installed
):
    """A broken basis is the operator's to fix, so the refit stays owed rather than being cleared
    like a failing fit (M4.10 finding 6)."""
    _store, user_id = installed
    await tiers.save_tier_set(db, user_id=user_id, tier_set=["F", "D", "C", "B", "A"])
    owed = {(u, k) for u, k, _t in await tiers.refits_owed(db)}
    assert owed == {(user_id, "movie"), (user_id, "series")}, owed

    await _make_active(db, "never-staged")
    with pytest.raises(RuntimeError, match="is active but"):
        await _job("tier-set-refit").run()

    assert {(u, k) for u, k, _t in await tiers.refits_owed(db)} == owed, (
        "a sweep that refused its basis discarded the requests it never serviced"
    )


async def test_the_admin_bundle_state_reports_broken_and_names_the_missing_path(db, tmp_path):
    """`restart_required` cannot say it: a broken store carries the active version."""
    await _make_active(db, "test-v1")
    store = await ArtifactStore.load_active(db, tmp_path / "artifacts")
    payload = await artifacts_api.bundle_state(
        db, None, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifacts=store)))
    )

    assert payload["active"] == "test-v1"
    assert payload["loaded"] is None
    assert payload["broken"] is True
    assert payload["missing_path"] == str(tmp_path / "artifacts" / "test-v1")


async def test_a_refit_over_an_empty_observation_set_empties_the_board_it_cannot_justify(
    db, tmp_path
):
    """The tier set survives (decision 11); boundaries return to the prior. The tier edits are
    what make the fitted boundaries differ from the prior at all."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    for title_id, tier in ((1, 6), (2, 5), (3, 3), (4, 0), (5, 2)):
        await observations.record_tier_edit(db, user_id=user_id, title_id=title_id, tier=tier)
    assert (await refit.refit_user(db, user_id=user_id, kind="movie", hp=DEFAULTS)).fitted
    fitted_cuts = [
        float(c) for c in await db.fetchval(
            "SELECT boundaries FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
            user_id,
        )
    ]
    prior = [float(c) for c in model.initial_cutpoints(len(observations.DEFAULT_TIER_SET))]
    assert fitted_cuts != pytest.approx(prior), (
        "the fit never moved the cutpoints, so this cannot tell the two apart"
    )

    await db.execute("DELETE FROM verdict WHERE user_id = $1", user_id)
    await db.execute("DELETE FROM tier_edit WHERE user_id = $1", user_id)
    report = await refit.refit_user(db, user_id=user_id, kind="movie", hp=DEFAULTS)
    assert report.fitted is False, report.as_dict()

    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND kind = 'movie'", user_id
    ) == 0, "the board is still ordered by a fit over observations that are gone"
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user_id
    ) == 0, "the next tap would solve incrementally from a cache of a fit over nothing"
    row = await db.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        user_id,
    )
    assert list(row["tier_set"]) == list(observations.DEFAULT_TIER_SET), (
        "decision 11's preference belongs to the person, not to the fit"
    )
    assert [float(b) for b in row["boundaries"]] == pytest.approx(prior)


async def test_the_cache_refuses_a_fit_whose_k_no_longer_matches_the_tier_set(db, tmp_path):
    """Between a tier-set PUT and the refit sweep, a cached fit at the old K clamped drops wrongly."""
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await insert_user(db, "Patrick", "admin")
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    # No `bundle_version`: a NULL stamp would be refused on version before K is reached.
    await db.execute("DELETE FROM title_placement")
    await db.execute("UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active'")
    assert (await refit.refit_user(db, user_id=user_id, kind="movie", hp=DEFAULTS)).fitted
    assert await refit.load_cache(
        db, user_id=user_id, kind="movie", hp=DEFAULTS, lock=False
    ) is not None, "the cache refuses this fit for a reason that is not K"

    await tiers.save_tier_set(db, user_id=user_id, tier_set=[f"T{i}" for i in range(1, 13)])
    assert await refit.load_cache(
        db, user_id=user_id, kind="movie", hp=DEFAULTS, lock=False
    ) is None, "a fit at the old K is not stale, it means something else"
