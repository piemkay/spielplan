"""Which basis is this? Spec v2.1 §10, §5.1, §5.2, §4.3, §3.1; decision 11.

Three questions that look like one and are not, which is why they get one file:

  * **Which bundle was a fit computed in?** Threaded by the caller, stamped on the fit. Nobody
    threaded it, so §10's step 3 -- the step whose entire purpose is to re-express every fitted
    number in the staged basis -- read the OUTGOING bundle's placements and stamped the outgoing
    version, and the first tap after the flip re-fitted on the request path against the old
    in-process Backbone and stamped THAT as the new one.
  * **Which bundle is the app serving?** The `artifact_bundle` active row, and §10's invariant is
    that a process whose loaded store disagrees with it may neither score nor refit. That
    invariant had one enforcement point, no caller, and a docstring claiming otherwise.
  * **Is there a bundle at all?** §3.1 makes a bundle-less household legal (None == None passes
    every guard here), and an active row whose directory is gone is a THIRD state that used to
    load as the second one -- so the worker fitted every board from zero coordinates under
    DEFAULTS and stamped it with the version whose files were missing.

The order of the two guards in `worker._active_store` is the subtlest thing in this milestone and
the reason `test_a_broken_store_carries_the_active_version_and_still_refuses_on_its_own_flag`
exists: carrying a broken install's version is what makes its stamp honest, and it is also what
makes `assert_matches` PASS for it. The refusal has to rest on the flag.

A broken bundle is produced here by pointing the active row at a version that was never staged,
never by deleting a staged directory. The two states are identical to every reader of
`artifact_bundle` -- an active row, no directory -- and the second one cannot be produced
reliably on Windows, where a memory-mapped `backbone.npz` keeps an open handle inside the tree a
test would have to remove.

Integration-kind against a real Postgres; skipped without TEST_DATABASE_URL (see conftest.py).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from spielplan import worker
from spielplan.api import artifacts as artifacts_api
from spielplan.api import home as home_api
from spielplan.api import rank as rank_api
from spielplan.api import rate as rate_api
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

PKG = Path(__file__).resolve().parents[1] / "spielplan"
COVERAGE = Path(__file__).resolve().parent / "spec_coverage.toml"

# Five verdicts over the fixture's Backbone-covered movies, which is enough for §5.2 to produce a
# fit with something in every band. Small on purpose: every assertion below is about the basis a
# fit was computed in, never about its numbers.
LABELS = ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1))
SERIES_LABELS = ((6, 2), (7, 0))


def _e_hat(seed: int) -> bytes:
    """A `title_placement.e_hat` blob in the column's own convention: 64 x float32 LE."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=64).astype("<f4").tobytes()


def _as_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4").astype(float)


async def _user(db, name: str = "Patrick", role: str = "admin") -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


async def _import(db, root: Path, artifacts_root: Path, *, version: str = "test-v1",
                  models_only: bool = False) -> None:
    fx.make_bundle(root, version=version)
    if models_only:
        # decision 162: content seeds once and models re-ship, so this is the re-import §10 is
        # actually about and the only one that can carry a second version at all.
        (root / "content.sqlite").unlink()
        (root / "reviews.sqlite").unlink()
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )
    assert report.ok, report.render()


async def _place(db, title_id: int, version: str, seed: int) -> None:
    """One `title_placement` row, written directly. The sweep's own columns, its own values."""
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
    """Flip the active row to `version`, inserting the row if it is not there.

    The partial unique index allows exactly one active row, so the supersede comes first. A
    version with no staged directory is data-03's state: an active row the files are missing for.
    """
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
    """The worker's own view of the world: the real pool, and DATA_DIR where `installed` stages.

    `DATA_DIR` is `tmp_path` itself rather than a subdirectory, so `settings().artifacts_dir` is
    the `artifacts_root` every `_import` below writes to -- the jobs have to find the bundle the
    test staged or a refusal proves nothing about the guard under test.
    """
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
    """A seeded install at `test-v1`, one admin, five verdicts, and the store the app would pin.

    Returns `(store, user_id)`. Every title owned, because §5.2 writes a row for each owned title
    and what is under test is the stamp, not the population.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    return ArtifactStore.open(tmp_path / "artifacts" / "test-v1", "test-v1"), user_id


def _job(name: str):
    return next(j for j in worker.JOBS if j.name == name)


# --- the version is threaded, not inferred (data-01) -----------------------------------------


async def test_standard_embeddings_reads_the_placements_of_the_version_it_was_given(db, tmp_path):
    """`placement_embeddings` has taken a `bundle_version` since M2 and `standard_embeddings`
    never passed one, so every caller got the `$2 IS NULL` branch:
    `JOIN artifact_bundle b ON b.state = 'active'`. During §10's pre-flip rebuild the active row
    is the OUTGOING bundle, and that is the whole of data-01.

    Asserted on the coordinates rather than through a fit, because a coordinate is the thing that
    differs between two bases and a fitted `v` is a function of many of them at once.
    """
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
    assert np.allclose(old[0], _as_vector(_e_hat(11))), (
        "the version asked for is not the version read"
    )
    assert np.allclose(new[0], _as_vector(_e_hat(22)))
    assert not np.allclose(old[0], new[0]), (
        "the two bundles' placements are identical, so this test cannot tell them apart"
    )


async def test_a_source_with_no_version_threaded_falls_back_to_the_active_row_as_the_bundle_less_path(
    db, tmp_path
):
    """The `$2 IS NULL` branch is the bundle-less path, and it stays one.

    Two halves, because "no version" means two different things. A caller with no bundle at all
    (§3.1's install, and every test source) gets the active row, which is the only answer
    available to it. And a fit over `zero_embeddings` is stamped NULL rather than with whatever
    row happens to be active -- dd01's NULL stamp, which is what lets `load_cache` accept a
    bundle-less household's cached fit across a restart instead of refitting it on every boot.
    """
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
    assert np.allclose(matrix[0], _as_vector(_e_hat(22))), (
        "the no-version branch must read the ACTIVE row's placements"
    )

    # The other half, on an install with no bundle row whatsoever.
    await db.execute("DELETE FROM artifact_bundle")
    user_id = await _user(db, name="Ana", role="member")
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
    """§10 step 3, which ran against the bundle it exists to replace.

    The flip happens AFTER `run_rebuild`, by design -- "a rebuild that fails takes the whole
    import down rather than leaving a new basis active with every fitted number still expressed
    in the old one" -- so during the rebuild the active row is the outgoing version.
    `_rebuild_ledger_refit` ignored its `version` argument, `standard_embeddings` passed none, and
    `refit_all` stamped `active_bundle_version`: measured ||v_step3 - v_correct|| = 0.397 against
    ||v_correct|| = 0.782.

    Both halves of the name are asserted, because the version is threaded at TWO independent
    seams -- into `standard_embeddings` for the coordinates the fit READS, and into `refit_all`
    for the version it is STAMPED with -- so a fit can carry the staged stamp over the outgoing
    bundle's coordinates. That mixed-basis write IS data-01 as it was measured, and no assertion
    on a stamp can see it. [M4.13 cycle 1, m413-rev1-cov-01]
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    # The two bases are MADE to differ rather than assumed to: `fx.make_bundle` writes model
    # files that do not depend on the version, so b1 and b2 place every title identically and a
    # fit over either reads the same numbers. Title 8 is the lever -- its Backbone row is
    # cold-masked and therefore read as ABSENT (§5.1's gate -> 0 limit), so its coordinate is its
    # Cold Tower placement and nothing else -- and the OUTGOING version's row for it is
    # overwritten here with a vector the sweep never produces.
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
        """Refit over `version`'s placements, stamped STAGED so `load_cache` hands it back.

        The wrong-basis fit is stamped `test-v2` on purpose: that is the shape data-01 wrote, and
        a fit stamped `test-v1` would be refused after the flip for its version rather than read
        for its numbers -- which would make this a second stamp assertion and not a basis one.
        """
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
    """The cost of the wrong stamp, where a person feels it.

    `load_cache` refuses a fit whose bundle is not the active row -- correctly, §10 -- so a step-3
    fit stamped with the OUTGOING version was refused by the cache the moment the flip made the
    new version active. Every (user, kind)'s first tap then paid §5.3's "seconds" row inside its
    "<50 ms" one; since M4.10 took the inline fit off the request path it pays it as a QUEUED miss
    instead, which is a board that reads `fitting` rather than one that moves. Stamped correctly,
    the rebuild's own fit IS the cache and the first tap is an ordinary incremental update.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
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
    """The window the sibling above opens, and the one `load_cache` could not see.

    Its three preconditions ask whether the fit's stamp is the ACTIVE row. That is the right
    question for the household and the wrong one for the caller: between §10's flip and §10's
    restart this process still holds the outgoing Backbone, and the tap that reaches this line is
    the one that WAITED for the flip. `importer/bundle.py` rebuilds and flips inside one
    transaction, `refit_user` takes the board's advisory lock inside it, and `_update_incrementally`
    takes that same lock before it reads -- so a tap arriving mid-import blocks until the commit
    and then sees the staged stamp and the new active row at once. Both agree, the cache came back,
    and the tap solved its residual with v in the incoming basis against e in the outgoing one:
    data-01's mixed-basis write, inherited, because the accepted branch's UPDATE does not rewrite
    `bundle_version`. The route's pre-write `_assert_active_basis` cannot catch it; it ran minutes
    earlier, which is what makes this a second check and not a duplicate one.

    Asserted through `update_incrementally_reporting` and on `ledger_state`, because what the
    finding is about is a WRITE: refusing the cache is only worth anything if the row does not
    move. QUEUED and not an exception, for `refit.py`'s standing rule -- a fit that refuses is a
    model problem and never a reason to lose the tap. [M4.13 cycle 1, finding 15]
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
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

    # The control, and it is exit-criterion check 1: after §10's restart the caller, the stamp and
    # the active row are all test-v2, and the same tap is an ordinary incremental update.
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
    """Every (user, kind), not the one the test happened to look at.

    `refit_all` loops the household and both kinds and the stamp is handed down through it, so
    this is the assertion that the threading reaches the leaf rather than the entry.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    patrick = await _user(db)
    ana = await _user(db, name="Ana", role="member")
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


# --- §10's invariant has production callers (arch-03, tq1) ------------------------------------


def _function(path: Path, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no {name}")


def test_assert_matches_is_called_from_the_entrypoints_its_docstring_names():
    """tq1 in one test: the docstring said "called by the scoring/refit entrypoints" and
    `grep -rn assert_matches` returned the definition, that claim, one comment and six test lines.

    So the claim is the assertion now. Every function the docstring names has to contain the call,
    and the package has to hold at least three production call sites -- the shape that cannot rot
    back into a comment.
    """
    module = PKG / "models" / "artifacts.py"
    source = module.read_text(encoding="utf-8")
    doc = ast.get_docstring(_function(module, "assert_matches")) or ""
    assert "worker._active_store" in doc, doc
    assert "_assert_active_basis" in doc, doc

    for path, function in (
        (PKG / "worker.py", "_active_store"),
        (PKG / "api" / "rate.py", "_assert_active_basis"),
        (PKG / "api" / "rank.py", "_assert_active_basis"),
    ):
        body = ast.unparse(_function(path, function))
        assert "assert_matches" in body, (
            f"{path.name}::{function} is named as a caller and does not call it"
        )

    callers = [
        path.relative_to(PKG).as_posix()
        for path in PKG.rglob("*.py")
        if "assert_matches(" in path.read_text(encoding="utf-8")
        and path != module
    ]
    assert len(callers) >= 3, f"§10's invariant has {len(callers)} production caller(s): {callers}"
    assert "is called by the scoring/refit entrypoints" not in source, (
        "the docstring is back to claiming a caller instead of describing one"
    )


def test_the_coverage_rows_name_the_operator_signal_the_worker_actually_leaves():
    """Two rows promised a silence the worker does not produce.

    `_active_store` raises -- deliberately, and its docstring argues why -- so `_tick` logs
    "job %s failed" and `_record_finish(run_id, ok=False, ...)` writes a `job_run` row carrying
    the message that names both versions. Three of the jobs that reach it tick every 60 seconds,
    so inside §10's normal window between the flip and the restart the model jobs read RED on
    the System card, which §6.6 builds from the newest `job_run` per job. "Advances nothing"
    and "logged" were both true of the rows' sentence; "no-op" was not -- and this milestone
    spends that word four lines from the raise on the OPPOSITE branch, the bundle-less household
    where None == None and nothing is written because nothing is owed. A reader holding the row as
    the spec of the behaviour reads those red rows as a regression rather than as §10 asking
    for the restart, and the exit criterion's own wording -- "the refit entrypoint raises" -- is
    what shipped. [M4.13 review cycle 2: M413-C2-D1-04]
    """
    rows = {
        row["id"]: row
        for row in tomllib.loads(COVERAGE.read_text(encoding="utf-8"))["requirement"]
        if row["id"] in (
            "jellyfin-acquisition-eval-bundle-swap-and-active-version-invariant",
            "data-rules-section-10-invariant-has-production-callers",
        )
    }
    assert len(rows) == 2, sorted(rows)
    for row_id, row in sorted(rows.items()):
        assert "no-op" not in row["what"], (
            f"{row_id} calls the worker half of the refusal a no-op: it raises, and the tick "
            "records a failed job_run that the System card renders"
        )
        assert "job_run" in row["what"], (
            f"{row_id} promises a refusal production code performs, so it owes what the refusal "
            "leaves behind -- a failed job_run naming both versions, retried on the next tick"
        )

    # The other direction: the rows describe the code only while the code still refuses this way.
    assert not [
        node
        for node in ast.walk(_function(PKG / "worker.py", "_active_store"))
        if isinstance(node, ast.Try)
    ], (
        "`_active_store` catches its own refusal; the rows above say the job is recorded as "
        "failed, which is true only while both guards are allowed to escape into `_tick`"
    )
    assert "_record_finish(run_id, ok=False" in (PKG / "worker.py").read_text(encoding="utf-8"), (
        "the tick no longer stamps a raising job as a failed run, so the rows above describe a "
        "signal the System card cannot show"
    )


async def test_every_read_path_reports_the_one_active_version(db, tmp_path):
    """Four definitions of "the active bundle version", now one resolver and its callers.

    `app.py` pinned a store at boot, `refit.active_bundle_version` read the row,
    `api/home.py::_bundle` read it again with a store fallback, and `api/tonight.py` spelled the
    SELECT a fourth time -- four answers to one question, which inside §10's window between the
    flip and the restart is four chances to disagree.

    Behaviour first, then the static half: the literal query survives in exactly two files, and
    the importer's copy is the `already_active` read inside its own flip transaction, which has to
    run before its two UPDATEs and says so where it is written.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    store = ArtifactStore.open(tmp_path / "artifacts" / "test-v1", "test-v1")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifacts=store)))

    assert await artifacts.active_bundle_version(db) == "test-v1"
    assert await refit.active_bundle_version(db) == "test-v1"
    assert await tonight_api._bundle_version(db) == "test-v1"
    assert await home_api._bundle(request, db) == "test-v1"

    query = "SELECT version FROM artifact_bundle WHERE state = 'active'"
    spellers = sorted(
        path.relative_to(PKG).as_posix()
        for path in PKG.rglob("*.py")
        if query in path.read_text(encoding="utf-8")
    )
    assert spellers == ["importer/bundle.py", "models/artifacts.py"], spellers


async def test_a_worker_model_job_whose_bundle_is_not_the_active_row_refuses_and_advances_nothing(
    db, worker_env, installed, monkeypatch
):
    """§10's window, in the worker, through a job that would have written in it.

    The interleaving is injected where it really happens rather than stubbed: `_active_store`
    loads the store and then resolves the active row, and an import running in the backend
    process can flip that row in between. Before this guard nothing anywhere in the app could
    detect a stale process at all.

    Through the placement sweep, which does not fit, so "advances nothing" is a statement about
    the refusal and not about a fit that happened to produce no rows.
    """
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
    """The same window, on the Ledger refit -- the job whose writes are the expensive ones.

    Both versions in the message, because an operator reading one line has to know which process
    is behind AND which bundle it is behind on; "bundle mismatch" sends them to a second service's
    log to find out.
    """
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
    """The request half of §10's window: 409, carrying the sentence the import screen showed.

    The process pins its store and its Backbone at boot because §10 makes a swap a restart, so
    after a flip `app.state.backbone` is the outgoing basis while every row the flip made visible
    is the incoming one. Unguarded, the tap fitted over half-and-half coordinates and stamped the
    result with the NEW version: ||v_tap - v_correct|| = 0.643.

    Both surfaces, because both fit, and asserted on `reason` rather than on the status alone --
    Rate already answers 409 for a stale card, so a test that accepted any 409 would pass on the
    wrong one.
    """
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    await db.execute("UPDATE title SET is_owned = true")
    # The store the app pinned at boot is the empty one (the import came after), so pin the one it
    # WOULD have loaded: the disagreement under test is a version against a version.
    client._transport.app.state.artifacts = ArtifactStore.open(
        tmp_path / "artifacts" / "test-v1", "test-v1"
    )
    await _make_active(db, "test-v2")

    verdict = await client.post(
        "/api/rate/verdict", json={"card_token": "whatever", "value": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_swapped", "message": artifacts_api.RESTART_REQUIRED
    }, verdict.json()

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.status_code == 409, drop.text
    assert drop.json()["detail"] == {
        "reason": "bundle_swapped", "message": artifacts_api.RESTART_REQUIRED
    }, drop.json()
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0, (
        "the refusal has to land before the write, never after it (M4.10 finding 8)"
    )


async def test_a_bundle_less_install_passes_the_invariant_rather_than_refusing(db, worker_env):
    """§3.1: "a bundle-less app is a legal state". None == None, through every new guard.

    The guards raise, so the bundle-less case is what proves they are guards on a disagreement and
    not a second `is_empty` check: there is nothing here to be wrong about, and a household can
    run for a week before the first corpus export exists.

    The request-path guard is called directly with the empty store the app would pin, because the
    property is the guard's and not any one route's -- the 409 those routes raise has its own test
    above.
    """
    ArtifactStore.empty().assert_matches(None)
    ArtifactStore.empty().assert_not_broken()
    assert await artifacts.active_bundle_version(db) is None

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(artifacts=ArtifactStore.empty()))
    )
    await rate_api._assert_active_basis(request, db)
    await rank_api._assert_active_basis(request, db)

    await _user(db)
    for name in ("ledger-map-refit", "fold-in-user-vectors", "placement-reconciliation",
                 "tier-set-refit"):
        await _job(name).run()


# --- a broken basis refuses rather than fitting at zero (data-03) -----------------------------


async def test_a_broken_store_carries_the_active_version_and_still_refuses_on_its_own_flag(
    db, tmp_path
):
    """THE TRAP, asserted so that nobody closes it the easy way.

    Carrying the active row's version is what makes a broken install's stamp honest -- and it is
    also what makes `store.version == active_version`, so §10's invariant PASSES for a store that
    cannot produce one coordinate. The refusal therefore cannot be the version comparison; it has
    to be the flag. And `is_empty` has to stay True, so every §3.1 surface keeps rendering.
    """
    await _make_active(db, "test-v1")
    store = await ArtifactStore.load_active(db, tmp_path / "artifacts")

    assert store.version == "test-v1", "a broken install's stamp has to name the active row"
    assert store.broken is True
    assert store.is_empty is True, (
        "§3.1's surfaces read is_empty and have to keep their no-bundle state"
    )
    # The trap, as an assertion rather than as a comment: this call must NOT raise.
    store.assert_matches(await artifacts.active_bundle_version(db))
    with pytest.raises(RuntimeError, match="does not exist"):
        store.assert_not_broken()
    with pytest.raises(RuntimeError, match="no artifact bundle loaded"):
        store.path("backbone.npz")


async def test_a_fitting_request_on_a_broken_bundle_answers_409_and_writes_nothing(
    db, app, tmp_path
):
    """The half of data-03 that had no caller: the routes fit too.

    `assert_not_broken` shipped with exactly one production caller, `worker._active_store`. So on a
    broken install the model jobs refused every sweep while `POST /api/rate/verdict`, `/duel`,
    `/undo` and `POST /api/rank/drop`, `/queue/answer` kept fitting -- and the version comparison
    cannot catch them, because a broken store carries the active row's own version on purpose. The
    only thing standing in front of those five was an accident: `hyperparams.load` returns DEFAULTS
    for an empty store, so the digest matched whenever the active bundle shipped no
    `ledger_hyperparams.json` (legal, `BUNDLE_FILES` marks it optional) and `load_cache` handed
    back a fit computed in a REAL basis for a tap to update with `e = 0`. Measured on the fixture:
    s 1.5735 / 1.5699 / 0.0787 healthy against 0.8001 / 0.8001 / 0.1799 broken -- two rated titles
    collapsed onto one number, in rows nothing corrects until the directory is back and a nightly
    runs, while the Data tab tells the operator "the model jobs refuse rather than refitting in a
    zero basis".

    Asserted on `reason` and on the absence of the row: 409 before the write is a refusal, and a
    refusal after one would be the loss M4.10 finding 8 costed. The wording is the restore's, not
    the swap's, because restarting this process would fix nothing.
    [M4.13 cycle 1, m413-c1-dim1-broken-bundle-refusal-is-worker-only]
    """
    client = app()
    assert (await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )).status_code == 201
    await _make_active(db, "test-v1")
    store = await ArtifactStore.load_active(db, tmp_path / "artifacts")
    assert store.broken and store.version == "test-v1"
    # The trap, restated where it bites: §10's comparison passes for this store.
    store.assert_matches(await artifacts.active_bundle_version(db))
    client._transport.app.state.artifacts = store

    verdict = await client.post(
        "/api/rate/verdict", json={"card_token": "whatever", "value": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_broken", "message": artifacts_api.RESTORE_REQUIRED
    }, verdict.json()

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.status_code == 409, drop.text
    assert drop.json()["detail"] == {
        "reason": "bundle_broken", "message": artifacts_api.RESTORE_REQUIRED
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
    """The one state in which the ORDER of the two guards decides the answer, pinned per caller.

    `models/artifacts.py`'s module docstring used to claim `assert_not_broken` is asked first, full
    stop; the grep it cites shows one caller doing that and two doing the reverse. Levelling them
    to match the sentence is the natural repair, and it would change what an operator is told in
    exactly this state: a process pinned at boot to a version whose directory is gone, while the
    active row has since moved on. Both facts are true, both refusals fire, and only the order
    picks which 409 the Rate surface returns.

    The request path is right to answer the SWAP: its store was pinned once (`app.py`) and is
    never re-pinned, so the restart §10 already asks for loads the new active bundle, whose
    directory exists. Telling the operator to restore the superseded version's directory instead
    would send them at the one thing that does not need to be there. The worker asks the flag
    first for the opposite reason: it RELOADS the store per job, so what it holds is the active
    row itself, and a missing directory is the only fact left to report.
    [M4.13 cycle 2, M413-C2-D1-02]
    """
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
        "/api/rate/verdict", json={"card_token": "whatever", "value": 2}
    )
    assert verdict.status_code == 409, verdict.text
    assert verdict.json()["detail"] == {
        "reason": "bundle_swapped", "message": artifacts_api.RESTART_REQUIRED
    }, "the request path holds a pinned store, so the restart is the action that fixes it"

    drop = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 3})
    assert drop.json()["detail"]["reason"] == "bundle_swapped", drop.json()

    # The worker reloads, so it holds gone-v2 against gone-v2: only the flag is left to report.
    with pytest.raises(RuntimeError, match="does not exist"):
        await worker._active_store(db)


# The five routes data-03's row calls "the five fitting routes", named here because the row's
# sentence is a claim about an inventory, and an inventory nobody wrote down is a grep.
_FITTING_ROUTES = (
    "rate.py::verdict", "rate.py::duel", "rate.py::undo", "rank.py::drop", "rank.py::answer",
)


def test_every_fitting_route_awaits_the_basis_guard_as_its_first_statement():
    """The row says five; the two 409 tests above drive two of them.

    Both window tests -- the swap arm at
    `test_a_scoring_request_on_a_stale_bundle_answers_409_with_the_restart_wording` and the broken
    arm above -- post `/api/rate/verdict` and `POST /api/rank/drop`, and `app.state.artifacts` is
    assigned in no other test. Measured by deleting `await _assert_active_basis(request, conn)`
    from `rate.py::duel`, `rate.py::undo` and `rank.py::answer`: this file stayed green, and so did
    `ops/m413_exit_criterion.py`, whose check 2 calls the helper on a stub request and counts
    `assert_matches` call SITES rather than routes. Three of the five were held by nothing.

    Driving the other three through both arms would cost a fixture per arm and buy the same fact
    twice; what rots is the WIRING -- a refactored route body, a merge, a sixth fitting route --
    so the wiring is what is asserted. A route that fits is one that threads `_basis(request)` into
    the write, which is the expression the row's sentence is about, and the guard has to be its
    FIRST statement: that is the placement `_assert_active_basis`'s own docstring claims ("before
    `_resume` and before any write") and the one M4.10 finding 8 costed, because a refusal raised
    after the tap has committed loses the tap and invites a retry that writes a second row.

    The inventory is frozen at the five names the row publishes, so a sixth fitting route cannot
    ship without this test being read. Static rather than integration for the reason
    `test_assert_matches_is_called_from_the_entrypoints_its_docstring_names` is: the property is
    that no route is MISSING the call, and a test that can only see the routes it remembers to
    post is exactly how three of them came to be claimed and not checked.
    [M4.13 cycle 2, M413-C2-D1-01]
    """
    guarded: dict[str, bool] = {}
    for module in ("rate.py", "rank.py"):
        tree = ast.parse((PKG / "api" / module).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_basis"
                for call in ast.walk(node)
            ):
                continue
            statements = node.body[1:] if ast.get_docstring(node) else node.body
            first = statements[0] if statements else None
            guarded[f"{module}::{node.name}"] = (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Await)
                and isinstance(first.value.value, ast.Call)
                and getattr(first.value.value.func, "id", None) == "_assert_active_basis"
            )

    unguarded = sorted(name for name, ok in guarded.items() if not ok)
    assert not unguarded, (
        "these routes thread _basis(request) into a fit without awaiting _assert_active_basis "
        f"as their first statement: {unguarded}"
    )
    assert sorted(guarded) == sorted(_FITTING_ROUTES), (
        f"data-03 names {len(_FITTING_ROUTES)} fitting routes and api/ holds {sorted(guarded)}; "
        "a route that fits is one this guard has to cover, so amend the row and the list together"
    )


async def test_the_model_jobs_refuse_a_broken_bundle_and_leave_ledger_fit_where_it_was(
    db, worker_env, installed
):
    """data-03, measured where it hurt: every board refitted in a zero basis, and stamped.

    `load_active` returned `empty()`, the worker mapped it to None, and `_ledger_map_refit` fitted
    from `zero_embeddings` under DEFAULTS, pruned and rewrote `ledger_state`, rewrote
    `ledger_cutpoints`, and stamped `ledger_fit` with the version whose files were gone -- so every
    unrated owned title came out at s = mu with one score, one tier and one badge, and
    `load_cache` accepted it. Recovery was an accident of the digest happening to differ.
    """
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
    """Decision 11's queue, across the refusal.

    `_tier_set_refits` clears a request even when the fit raised, deliberately (M4.10 finding 6):
    a fit that fails inside the person's own data fails again in sixty seconds, and §5.3's nightly
    pass fits the same (user, kind) anyway. A broken BASIS is the other kind of failure -- nothing
    is wrong with the person's data and the operator can fix it -- so the refusal comes before the
    loop, and the work stays owed for the tick after the restore instead of being cleared by a
    sweep that did nothing.
    """
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
    """§6.6: the Data tab is where an operator reads this, not the backend log.

    `restart_required` cannot express it. `active != store.version` is False for a broken install
    -- because the store now carries the active row's own version -- and `loaded` is None because
    `is_empty` stays True, so the page said: a bundle is active, none is loaded, no restart is
    needed. Which describes nothing that can happen.
    """
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
    """ml02: the branch whose comment said the shelves are "better empty" and left them full.

    Reproduced before the fix: a nightly with zero observations returned `fitted = False` with
    `ledger_state` still at 6 rows, one of them `observed = True`, the learned cutpoints in place
    and a `ledger_fit` row holding the old `n_observed` -- which `load_cache` accepted, so the next
    first tap solved incrementally from residuals for verdicts that no longer exist. Decision 35's
    Undo and decision 174's hard-DELETE are how a household reaches this state.

    The tier SET survives, because decision 11 makes it a preference; the BOUNDARIES go back to
    the prior, which is the only thing left to say once the labels that moved them are gone.

    TIER EDITS as well as verdicts, and they are not decoration: §5.2's cut-points are fitted by
    the TIER arm alone, so a fit with verdicts only leaves them exactly at `initial_cutpoints(K)` --
    the prior is the optimum when the arm has no data -- and the boundary half of this test would
    then be asserting that an unchanged vector is unchanged. Measured: with five verdicts the
    fitted boundaries sit within 1e-15 of the prior; the five edits below move them.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
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


# --- K is the third correctness precondition on the cache (ml01) ------------------------------


async def test_the_cache_refuses_a_fit_whose_k_no_longer_matches_the_tier_set(db, tmp_path):
    """`load_cache` validated `hp_digest` and `bundle_version` and not K.

    Decision 11 keeps the `tier_edit` rows across a tier-set change and queues a refit, so between
    the PUT and the 60 s sweep every drop went through `_update_incrementally` at the OLD K:
    growing 7 -> 12, a drop into tier 7 of 12 was clamped to 6 of 7 and written as
    `ledger_state.tier = 4` while the displayed K = 12 boundaries give 8 -- Home showing T4 and
    Rank T7 for one title.

    The K row registers its route-level assertion in `test_rank_integration.py`; this is the same
    precondition at the function that holds it, and it lives here because `load_cache` is this
    stage's file.
    """
    await _import(db, tmp_path / "b1", tmp_path / "artifacts")
    user_id = await _user(db)
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in LABELS:
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    # Without `bundle_version`, deliberately: the stamp is data-01's concern and this test is
    # ml01's. A bundle-less fit stamps NULL and `load_cache` compares NULL to the active row, so
    # the version arm would refuse first and K would never be reached.
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
