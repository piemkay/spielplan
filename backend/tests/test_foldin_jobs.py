"""The fold-in's jobs: the clock, the transaction, the debounce, one household (§5.3). Waits are simulated
by moving both stamps back together: `_is_stale` reads only their ORDER. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import inspect
import warnings

import numpy as np
import pytest

from spielplan.db.library import household_ids
from spielplan.home import shelves
from spielplan.importer import bundle as bundle_import
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import load as load_hp
from spielplan.models.artifacts import ArtifactStore
from spielplan.scoring import backbone as bb
from spielplan.scoring import foldin, serve
from spielplan.scoring.backbone import EMBED_DIM, Coordinate
from tests.fixtures import make_bundle as fx

pytestmark = pytest.mark.anyio

BUNDLE = "test-v1"

# Five coordinated movies (`MIN_LABELS_FOR_CV`); the sixth
# observation is always a RE-RATING, so only a clock carries it.
SITTING: tuple[tuple[int, int], ...] = ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1))
RE_RATED = 3


@pytest.fixture
async def world(db, tmp_path):
    """Everybody starts unfitted: "never fitted" is the one staleness the debounce may not gate."""
    (tmp_path / "data" / "artifacts").mkdir(parents=True)
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), tmp_path / "data" / "artifacts"
    )
    assert report.ok, report.render()
    await db.execute("UPDATE title SET is_owned = true")
    store = ArtifactStore.open(tmp_path / "data" / "artifacts" / BUNDLE, BUNDLE)
    people: dict[str, int] = {}
    for name, role, active in (
        ("patrick", "admin", True), ("ana", "member", True), ("sam", "member", False)
    ):
        people[name] = await db.fetchval(
            "INSERT INTO app_user (name, role, is_active) VALUES ($1, $2, $3) RETURNING id",
            name, role, active,
        )
    return {"store": store, "backbone": bb.load_for(store), **people}


async def _rate(conn, user_id: int, pairs=SITTING) -> None:
    for title_id, value in pairs:
        await observations.record_verdict(conn, user_id=user_id, title_id=title_id, value=value)


async def _tick(conn, world, *, only_stale: bool = True) -> foldin.FoldInReport:
    return await foldin.run(
        conn, world["backbone"], bundle_version=BUNDLE, only_stale=only_stale, with_priors=False
    )


async def _wait_out_the_pause(conn, user_id: int) -> None:
    """Make `foldin.PAUSE_SECONDS` have elapsed for this person, without spending it."""
    shift = foldin.PAUSE_SECONDS + 10
    await conn.execute(
        "UPDATE verdict SET created_at = created_at - ($2::int * interval '1 second') "
        " WHERE user_id = $1",
        user_id, shift,
    )
    await conn.execute(
        "UPDATE user_vector SET updated_at = updated_at - ($2::int * interval '1 second') "
        " WHERE user_id = $1",
        user_id, shift,
    )
    # A placement newer than the fit is the third clock `_is_stale` reads; shifted too, so the ORDER holds.
    await conn.execute(
        "UPDATE title_placement SET created_at = created_at - ($1::int * interval '1 second')",
        shift,
    )


async def _partition(conn, user_id: int, kind: str = "movie") -> list[tuple]:
    """`xmin` tells a rewrite from a read: `replace_scores` re-inserts the same titles."""
    rows = await conn.fetch(
        "SELECT title_id, xmin::text AS tx, computed_at FROM user_score "
        " WHERE user_id = $1 AND kind = $2 ORDER BY title_id",
        user_id, kind,
    )
    return [(r["title_id"], r["tx"], r["computed_at"]) for r in rows]


async def _fit(conn, user_id: int, kind: str = "movie"):
    return await conn.fetchrow(
        "SELECT label_count, updated_at, bundle_version FROM user_vector "
        " WHERE user_id = $1 AND kind = $2 AND purpose = 'foldin'",
        user_id, kind,
    )


async def test_a_label_recorded_seconds_ago_does_not_rewrite_a_partition_on_the_next_tick(
    db, world
):
    """Every stale tick rewrote the whole partition: 14,000
    DELETEs and INSERTs per pair. The first fit is not gated."""
    patrick = world["patrick"]
    await _rate(db, patrick)
    first = await _tick(db, world)
    assert (patrick, "movie") in first.refit, "a member who has never been fitted must be"
    before = await _partition(db, patrick)
    stamp = await _fit(db, patrick)
    assert before and stamp["label_count"] == len(SITTING)

    # The sitting continues: one re-rating, this instant.
    await observations.record_verdict(db, user_id=patrick, title_id=RE_RATED, value=0)
    second = await _tick(db, world)

    assert (patrick, "movie") not in second.refit, (
        "a label recorded seconds ago triggered a full partition rewrite: the tick is repainting "
        "during the sitting instead of after it"
    )
    assert second.skipped >= 1
    assert await _partition(db, patrick) == before, (
        "the partition was rewritten anyway - the same rows with a new xmin, 14k of them at "
        "corpus scale"
    )
    assert (await _fit(db, patrick))["updated_at"] == stamp["updated_at"]


async def test_the_debounce_window_lets_exactly_one_refit_through_and_it_writes_one_partition(
    db, world
):
    """`replace_scores` is per (user, kind): the other three partitions must keep their rows."""
    patrick, ana = world["patrick"], world["ana"]
    await _rate(db, patrick)
    await _rate(db, ana)
    assert len((await _tick(db, world)).refit) == 4, "two people, two kinds, none of them fitted"
    pairs = [(person, kind) for person in (patrick, ana) for kind in ("movie", "series")]
    before = {pair: await _partition(db, *pair) for pair in pairs}

    await observations.record_verdict(db, user_id=patrick, title_id=RE_RATED, value=0)
    refits = list((await _tick(db, world)).refit)           # inside the window: nothing
    await _wait_out_the_pause(db, patrick)
    refits += list((await _tick(db, world)).refit)          # the pause has passed: one
    refits += list((await _tick(db, world)).refit)          # and it does not repeat

    assert refits == [(patrick, "movie")], f"one re-rating produced {len(refits)}: {refits}"
    after = {pair: await _partition(db, *pair) for pair in pairs}
    assert after[(patrick, "movie")] != before[(patrick, "movie")], "the held-back fit never ran"
    for pair in pairs[1:]:
        assert after[pair] == before[pair], f"{pair} was rewritten by somebody else's re-rating"


async def test_the_hard_cap_forces_a_refit_for_a_member_who_never_stops_rating(db, world):
    """Five minutes without a refit and the next tick takes
    it, pause or not; the control is what fails unfixed."""
    patrick = world["patrick"]
    await _rate(db, patrick)
    await _tick(db, world)
    before = await _partition(db, patrick)

    await observations.record_verdict(db, user_id=patrick, title_id=RE_RATED, value=0)
    assert (patrick, "movie") not in (await _tick(db, world)).refit, (
        "the control: a fit a moment old with a label seconds old waits for the pause"
    )
    assert await _partition(db, patrick) == before

    # Six minutes of uninterrupted rating: the fit is old, the newest label is seconds old.
    await db.execute(
        "UPDATE user_vector SET updated_at = updated_at - ($2::int * interval '1 second') "
        " WHERE user_id = $1",
        patrick, foldin.HARD_CAP_SECONDS + 60,
    )
    assert (patrick, "movie") in (await _tick(db, world)).refit, (
        f"{foldin.HARD_CAP_SECONDS}s without a refit and the tick still declined: a member who "
        "never stops rating never sees a personal shelf"
    )
    assert await _partition(db, patrick) != before


async def test_a_refit_with_unchanged_labels_writes_no_rows(db, world):
    """A person idle for an hour is paused and past the
    cap, so "moved AND settled" must still need "moved"."""
    patrick = world["patrick"]
    await _rate(db, patrick)
    await _tick(db, world)
    await _wait_out_the_pause(db, patrick)
    await _tick(db, world)
    settled = await _partition(db, patrick)
    stamp = await _fit(db, patrick)

    quiet = await _tick(db, world)
    assert quiet.refit == [], f"nothing moved and it refit anyway: {quiet.refit}"
    assert quiet.scores_written == 0
    household = await household_ids(db)
    assert quiet.skipped == 2 * len(household), (
        f"{quiet.skipped} pairs were considered for a household of {len(household)} over two "
        "kinds: the tick is iterating a different set of people than the household is"
    )
    assert await _partition(db, patrick) == settled
    assert (await _fit(db, patrick))["updated_at"] == stamp["updated_at"]

    nightly = await _tick(db, world, only_stale=False)
    assert (patrick, "movie") in nightly.refit
    assert await _partition(db, patrick) != settled, (
        "the nightly pass is the one that rewrites regardless; if it does not, the tick's "
        "restraint has nowhere to hand the work on to"
    )


async def test_a_fit_in_another_coordinate_geometry_is_refitted_at_once_and_the_priors_with_it(
    db, world
):
    """`bundle_version` cannot tell geometries apart, so every fit carries its own."""
    patrick, ana, sam = world["patrick"], world["ana"], world["sam"]
    await _rate(db, patrick)
    await _rate(db, ana)
    assert len((await _tick(db, world)).refit) == 4
    stamps = await db.fetch("SELECT DISTINCT geometry FROM user_vector")
    assert [r["geometry"] for r in stamps] == [bb.COORDINATE_GEOMETRY]

    await db.execute(
        "UPDATE user_vector SET geometry = 'raw' WHERE user_id = $1 AND kind = 'movie'", patrick
    )
    await db.execute("UPDATE title_prior SET b = 9.0 WHERE title_id = 1")
    report = await _tick(db, world)
    assert report.refit == [(patrick, "movie")], report.refit
    assert report.priors is not None
    assert report.priors.written == await db.fetchval("SELECT count(*) FROM title")
    assert await db.fetchval("SELECT b FROM title_prior WHERE title_id = 1") != 9.0
    assert (await db.fetchval(
        "SELECT geometry FROM user_vector WHERE user_id = $1 AND kind = 'movie'", patrick
    )) == bb.COORDINATE_GEOMETRY

    again = await _tick(db, world)
    assert again.refit == [] and again.priors is None

    await db.execute(
        "INSERT INTO user_vector (user_id, kind, purpose, vec, geometry) "
        "VALUES ($1, 'movie', 'foldin', $2, 'raw')",
        sam, bb.pack_vec(np.zeros(EMBED_DIM)),
    )
    quiet = await _tick(db, world)
    assert quiet.refit == [] and quiet.priors is None, (
        "a deactivated account's raw row rewrote the crowd half on an ordinary tick"
    )


async def test_a_title_placed_after_the_fit_is_ranked_by_the_next_tick(db, world):
    """A placement newer than the fit triggers a refit and
    a prior, so acquisitions rank before the night."""
    patrick, ana = world["patrick"], world["ana"]
    await _rate(db, patrick)
    await _tick(db, world)
    acquired = 1_000_000_001
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, origin) "
        "VALUES ($1, 'movie', 'The Apprentice', 2024, true, 'acquired')",
        acquired,
    )
    await db.execute(
        """
        INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                     tower_sha256, input_dim, blocks_present, blocks_dropped,
                                     blocks_imputed, nnz)
        VALUES ($1, $2, $3, 0.2, 'sha-contract', 'sha-tower', 131,
                ARRAY['genre'], ARRAY[]::text[], ARRAY[]::text[], 7)
        """,
        acquired, BUNDLE, bb.pack_vec(np.random.default_rng(1).standard_normal(EMBED_DIM) * 30),
    )
    assert await db.fetchval("SELECT count(*) FROM title_prior WHERE title_id = $1", acquired) == 0

    report = await _tick(db, world)
    assert sorted(report.refit) == sorted([(patrick, "movie"), (ana, "movie")]), report.refit
    assert report.priors is not None and report.priors.written == 1
    prior = await db.fetchrow("SELECT * FROM title_prior WHERE title_id = $1", acquired)
    assert prior["e_source"] == "cold_tower" and prior["bundle_version"] == BUNDLE
    for person in (patrick, ana):
        assert await db.fetchval(
            "SELECT count(*) FROM user_score WHERE user_id = $1 AND title_id = $2", person, acquired
        ) == 1
    top = await serve.top_scored(db, user_id=patrick, kind="movie", bundle_version=BUNDLE, limit=50)
    assert acquired in [item["id"] for item in top["items"]]

    again = await _tick(db, world)
    assert again.refit == [] and again.priors is None


async def test_a_fold_in_whose_score_write_fails_leaves_no_user_vector_row(db, world, monkeypatch):
    """`write_fit` and `replace_scores` are one transaction, or the pair reports fresh over empty scores."""
    patrick = world["patrick"]
    await _rate(db, patrick)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("the partition write failed")

    monkeypatch.setattr(serve, "replace_scores", boom)
    with pytest.raises(RuntimeError):
        await foldin.refit_user(
            db, world["backbone"], user_id=patrick, kind="movie", bundle_version=BUNDLE
        )
    monkeypatch.undo()

    assert await _fit(db, patrick) is None, (
        "the fit's stamp survived the write that failed: this pair now reads as fitted with no "
        "scores, and `_is_stale` will never say otherwise"
    )
    assert await _partition(db, patrick) == []
    assert await foldin._is_stale(
        db, user_id=patrick, kind="movie", bundle_version=BUNDLE
    ) is True
    assert (patrick, "movie") in (await _tick(db, world)).refit

    # Read after the backdating, since `_wait_out_the_pause` moves `updated_at` too.
    await observations.record_verdict(db, user_id=patrick, title_id=RE_RATED, value=0)
    await _wait_out_the_pause(db, patrick)
    stamp = await _fit(db, patrick)
    settled = await _partition(db, patrick)

    monkeypatch.setattr(serve, "replace_scores", boom)
    with pytest.raises(RuntimeError):
        await foldin.refit_user(
            db, world["backbone"], user_id=patrick, kind="movie", bundle_version=BUNDLE
        )
    monkeypatch.undo()

    assert (await _fit(db, patrick))["updated_at"] == stamp["updated_at"], (
        "the stamp advanced past the re-rating on a refit that wrote no scores"
    )
    assert await _partition(db, patrick) == settled
    assert (patrick, "movie") in (await _tick(db, world)).refit


async def test_a_re_rating_committed_inside_the_fit_window_is_picked_up_by_the_next_tick(
    db, world, monkeypatch
):
    """The fit's clock is read BEFORE its labels, or a re-rating committed in between is never seen."""
    patrick = world["patrick"]
    await _rate(db, patrick)
    await _tick(db, world)
    await _wait_out_the_pause(db, patrick)

    real_live_labels = foldin.live_labels

    async def labels_then_a_re_rating(conn, *, user_id, kind):
        out = await real_live_labels(conn, user_id=user_id, kind=kind)
        if user_id == patrick and kind == "movie":
            await observations.record_verdict(conn, user_id=patrick, title_id=RE_RATED, value=0)
        return out

    monkeypatch.setattr(foldin, "live_labels", labels_then_a_re_rating)
    await foldin.refit_user(
        db, world["backbone"], user_id=patrick, kind="movie", bundle_version=BUNDLE
    )
    monkeypatch.undo()

    fit = await _fit(db, patrick)
    newest = await db.fetchval("SELECT max(created_at) FROM verdict WHERE user_id = $1", patrick)
    assert fit["label_count"] == len(SITTING), "a supersede must not move the count"
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1", patrick
    ) == len(SITTING) + 1
    assert newest > fit["updated_at"], (
        "the fit is stamped after the re-rating it never saw, so no later tick can ever notice "
        f"it: label {newest}, fit {fit['updated_at']}"
    )

    await _wait_out_the_pause(db, patrick)
    assert (patrick, "movie") in (await _tick(db, world)).refit


async def _both_nightly_passes(db, world) -> None:
    hp, _notes = load_hp(world["store"])
    await _tick(db, world, only_stale=False)
    await refit.refit_all(
        db, hp,
        embeddings=observations.standard_embeddings(
            db, world["backbone"], bundle_version=BUNDLE
        ),
        bundle_version=BUNDLE,
    )


async def _distinct_users(db, table: str) -> set[int]:
    return {
        int(r["user_id"])
        for r in await db.fetch(f"SELECT DISTINCT user_id FROM {table} ORDER BY user_id")
    }


async def test_the_nightly_refit_and_the_fold_in_fit_the_same_accounts(db, world):
    for person in ("patrick", "ana", "sam"):
        await _rate(db, world[person])
    await _both_nightly_passes(db, world)

    household = set(await household_ids(db))
    assert household == {world["patrick"], world["ana"]}
    assert await _distinct_users(db, "user_vector") == household, (
        "the fold-in fitted a different set of accounts than the household"
    )
    assert await _distinct_users(db, "ledger_state") == household, (
        "the Ledger fitted a different set of accounts than the household"
    )
    assert await _distinct_users(db, "user_score") == household


async def test_a_deactivated_account_is_folded_in_by_neither_pass(db, world):
    for person in ("ana", "sam"):
        await _rate(db, world[person])
    await _both_nightly_passes(db, world)

    for table in ("user_vector", "user_score", "ledger_state", "ledger_fit"):
        assert await db.fetchval(
            f"SELECT count(*) FROM {table} WHERE user_id = $1", world["sam"]
        ) == 0, f"a deactivated account has {table} rows"
        assert await db.fetchval(
            f"SELECT count(*) FROM {table} WHERE user_id = $1", world["ana"]
        ) > 0, f"the active member has no {table} rows either, so this proves nothing"


async def test_the_household_predicate_is_the_one_the_home_partner_query_spells(db, world):
    """§6.0's partner query spells the intersection: `is_active AND role IN ('admin', 'member')`."""
    patrick, ana = world["patrick"], world["ana"]
    assert await household_ids(db) == sorted([patrick, ana])
    partner = await shelves.partner_for(db, user_id=patrick)
    assert partner is not None and partner["user_id"] == ana

    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", ana)
    assert await household_ids(db) == [patrick], "a deactivated member is still in the household"
    assert await shelves.partner_for(db, user_id=patrick) is None, (
        "§6.0 and the two nightly passes now disagree about who is here"
    )

    assert "u.is_active AND u.role IN ('admin', 'member')" in inspect.getsource(
        shelves.partner_for
    )
    assert "is_active AND role IN ('admin', 'member')" in inspect.getsource(household_ids)
    for func in (foldin.run, refit.refit_all):
        source = inspect.getsource(func)
        assert "household_ids(" in source, f"{func.__qualname__} does not ask the helper"
        assert "FROM app_user" not in source, (
            f"{func.__qualname__} still spells its own household predicate"
        )


def test_an_empty_reference_population_standardises_nothing_rather_than_storing_nan():
    """`np.std` of nothing is NaN, and `prior_sd < 1e-9` does not catch NaN."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        fit = foldin.fit_user([], {}, [])

    assert fit.prior_sd == 1.0 and np.isfinite(fit.prior_sd)
    assert fit.prior_mean == 0.0
    assert fit.beta == 0.0 and fit.cf_sd == 1.0
    assert fit.label_count == 0


def _synthetic_fold_in_case(
    *, n_labelled: int = 30, n_population: int = 200, seed: int = 11
) -> tuple[dict[int, Coordinate], list[Coordinate], list[tuple[int, int]]]:
    """Labelled rows are long and the population short (gate 0.9 against 0.09): the scale gap is real."""
    rng = np.random.default_rng(seed)
    taste = rng.standard_normal(EMBED_DIM)
    taste /= np.linalg.norm(taste)
    labelled = []
    for _ in range(n_labelled):
        e = rng.standard_normal(EMBED_DIM)
        labelled.append(2.0 * e / np.linalg.norm(e))
    latent = np.asarray([float(e @ taste) for e in labelled])
    latent = (latent - latent.mean()) / latent.std() + 0.6 * rng.standard_normal(n_labelled)
    cuts = np.quantile(latent, [1 / 3, 2 / 3])

    coords: dict[int, Coordinate] = {}
    reference: list[Coordinate] = []
    labels: list[tuple[int, int]] = []
    for i, e in enumerate(labelled):
        # Half taste, half noise, so beta lands inside the grid rather than on its ceiling.
        b = 0.5 * latent[i] + 0.5 * float(rng.standard_normal())
        c = Coordinate(title_id=i + 1, e=e, b=b, gate=0.9, item_n=500, e_source="backbone")
        coords[i + 1] = c
        reference.append(c)
        labels.append((i + 1, int(np.searchsorted(cuts, latent[i]))))
    for j in range(n_population):
        e = rng.standard_normal(EMBED_DIM)
        reference.append(
            Coordinate(
                title_id=1000 + j, e=0.2 * e / np.linalg.norm(e),
                b=float(rng.standard_normal()), gate=bb.gate(1), item_n=1, e_source="backbone",
            )
        )
    return coords, reference, labels


def _held_out_table(
    coords, reference, labels, *, seed: int, over: str
) -> dict[tuple[float, float], float]:
    """Restated rather than reached into: the claim is an equality between two independent spellings."""
    # Each row's gate-weighted direction, which is what `fit_user` reads since decision 469.
    ref_e = bb.directions(reference)
    ref_b = np.asarray([c.b for c in reference], dtype=np.float64)
    prior_mean, prior_sd = float(ref_b.mean()), float(ref_b.std())
    ordered = sorted(labels, key=lambda pair: int(pair[0]))
    rows = [
        (bb.directions([coords[t]])[0], foldin.VERDICT_TO_Y[int(v)], coords[t].b)
        for t, v in ordered
    ]
    x = np.ascontiguousarray([r[0] for r in rows], dtype=np.float64)
    y_raw = np.asarray([r[1] for r in rows], dtype=np.float64)
    z_prior = (np.asarray([r[2] for r in rows], dtype=np.float64) - prior_mean) / prior_sd
    y = y_raw - y_raw.mean()

    n = len(rows)
    fold = foldin._fold_assignment(n, seed)
    table: dict[tuple[float, float], float] = {}
    for lam in foldin.LAMBDA_GRID:
        raw, z_cf = np.zeros(n), np.zeros(n)
        for f in range(int(fold.max()) + 1):
            held = fold == f
            if held.all():
                continue
            v_f = foldin.fold_in(x[~held], y[~held], lam)
            raw[held] = x[held] @ v_f
            sd_f = float((ref_e @ v_f).std())
            if sd_f >= 1e-9:
                z_cf[held] = raw[held] / sd_f
        if over == "labels":
            sd = raw.std()
            z_cf = raw / sd if sd > 1e-9 else np.zeros(n)
        for beta in foldin.BETA_GRID:
            table[(lam, beta)] = foldin.spearman((1.0 - beta) * z_prior + beta * z_cf, y_raw)
    return table


def _select(table) -> tuple[float, float, float]:
    """A tie buys no personalisation, and within noise of best the smallest beta wins."""
    rho0 = table[(foldin.LAMBDA_GRID[0], 0.0)]
    best = max(table.values())
    if best - rho0 <= foldin.NOISE_FLOOR:
        return foldin.LAMBDA_GRID[-1], 0.0, rho0
    within = [key for key, rho in table.items() if best - rho <= foldin.NOISE_FLOOR]
    beta = min(key[1] for key in within)
    lam = min(key[0] for key in within if key[1] == beta)
    return lam, beta, table[(lam, beta)]


def test_the_cross_validation_standardises_each_fold_the_way_serving_will():
    """The search must standardise over the reference
    population, as serving does; the printed beta differed."""
    coords, reference, labels = _synthetic_fold_in_case()
    fit = foldin.fit_user(labels, coords, reference, seed=9)
    assert 0.0 < fit.beta < foldin.BETA_MAX, (
        f"beta {fit.beta} is on the grid's edge, where the two standardisations are "
        "indistinguishable and this test asserts nothing"
    )

    serving = _held_out_table(coords, reference, labels, seed=9, over="reference")
    labelled = _held_out_table(coords, reference, labels, seed=9, over="labels")
    lam, beta, rho = _select(serving)

    assert (fit.lam, fit.beta) == (lam, beta), (
        f"the search chose (lambda {fit.lam}, beta {fit.beta}) where the serving scale gives "
        f"(lambda {lam}, beta {beta}): the beta in the report is not the beta in effect"
    )
    assert fit.cv_rho == pytest.approx(rho), f"cv_rho {fit.cv_rho} is not {rho}"

    old_lam, old_beta, old_rho = _select(labelled)
    assert (old_beta, old_rho) != (beta, rho), (
        "the two standardisations agree on this fixture, so it cannot tell them apart"
    )
    assert abs(rho - labelled[(lam, beta)]) > foldin.NOISE_FLOOR, (
        f"the gap between the two spellings ({abs(rho - labelled[(lam, beta)]):.4f}) is inside "
        "§0's tie band here, so the fixture is not evidence of anything"
    )
    assert old_lam in foldin.LAMBDA_GRID


# A verdict recorded while `refit_user` fitted was pruned back to the prior; the lock serialises them.

NINE_TIERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]

# `Tampopo`, the one movie the SITTING leaves alone; 0022's (title_id, kind) FK refuses a series id.
UNRATED = 8


@pytest.fixture
async def second(db, pg_url):
    """A second connection, because an advisory lock is about two callers."""
    import json as _json

    import asyncpg

    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=_json.dumps, decoder=_json.loads, schema="pg_catalog"
        )
    try:
        yield conn
    finally:
        await conn.close()


def _basis(conn, world):
    return observations.standard_embeddings(conn, world["backbone"], bundle_version=BUNDLE)


async def _a_tap_arrives_while_a_fit_is_running(db, second, world, monkeypatch, hp):
    """The fit is held after `load_observations`: a synchronous `model.fit` has no await point."""
    patrick = world["patrick"]
    reading = asyncio.Event()
    release = asyncio.Event()
    real = observations.load_observations

    async def held_after_the_read(conn, **kwargs):
        loaded = await real(conn, **kwargs)
        reading.set()
        await release.wait()
        return loaded

    monkeypatch.setattr(observations, "load_observations", held_after_the_read)
    fit = asyncio.create_task(
        refit.refit_user(
            second, user_id=patrick, kind="movie", hp=hp,
            embeddings=_basis(second, world), bundle_version=BUNDLE,
        )
    )
    try:
        await asyncio.wait_for(reading.wait(), timeout=20)

        # The route commits the observation before the tap is called, so this is two statements.
        await observations.record_verdict(db, user_id=patrick, title_id=UNRATED, value=2)
        tap = asyncio.create_task(
            refit.update_incrementally(
                db, user_id=patrick, kind="movie", title_ids=[UNRATED], hp=hp,
                embeddings=_basis(db, world),
            )
        )
        done, _pending = await asyncio.wait({tap}, timeout=3.0)
        still_waiting = not done
    finally:
        release.set()
    await asyncio.wait_for(fit, timeout=60)
    delta = await asyncio.wait_for(tap, timeout=60)
    return still_waiting, delta


async def test_the_tap_waits_on_the_lock_the_fit_holds_rather_than_racing_it(
    db, world, second, monkeypatch
):
    """Waiting is the repair; the lock is taken before `load_cache`, or this would deadlock."""
    patrick = world["patrick"]
    hp, _notes = load_hp(world["store"])
    await _rate(db, patrick)
    await refit.refit_user(
        db, user_id=patrick, kind="movie", hp=hp, embeddings=_basis(db, world),
        bundle_version=BUNDLE,
    )
    assert await _cache_holds(db, patrick, hp) == set(t for t, _v in SITTING), (
        "the tap needs a cache to hit, or it queues a refit and this measures nothing"
    )

    still_waiting, delta = await _a_tap_arrives_while_a_fit_is_running(
        db, second, world, monkeypatch, hp
    )

    assert still_waiting, (
        "the tap ran straight through a fit that had already read the observations it is about to "
        "prune: whichever of the two writes lands second silently discards the other"
    )
    assert delta.fit_source == "incremental", (
        f"the tap that waited did not then fit: {delta.fit_source}"
    )
    assert delta.rows and delta.rows[0].title_id == UNRATED


async def test_a_verdict_recorded_during_a_refit_survives_into_the_fit_that_follows(
    db, world, second, monkeypatch
):
    """`ledger_state` is what the shelves read; `ledger_fit.title_ids` is what the NEXT tap reads."""
    patrick = world["patrick"]
    hp, _notes = load_hp(world["store"])
    await _rate(db, patrick)
    await refit.refit_user(
        db, user_id=patrick, kind="movie", hp=hp, embeddings=_basis(db, world),
        bundle_version=BUNDLE,
    )

    _still_waiting, delta = await _a_tap_arrives_while_a_fit_is_running(
        db, second, world, monkeypatch, hp
    )

    row = await db.fetchrow(
        "SELECT s, observed, fit_source FROM ledger_state WHERE user_id = $1 AND title_id = $2",
        patrick, UNRATED,
    )
    assert row["observed"] is True, (
        "the nightly fit pruned the verdict the person made while it was running: the board says "
        "they have never rated the title"
    )
    assert row["fit_source"] == "incremental"
    assert float(row["s"]) == pytest.approx(delta.rows[0].s, rel=1e-9), (
        "the board does not hold the number the tap computed, so some other write landed on top"
    )
    assert UNRATED in await _cache_holds(db, patrick, hp), (
        "ledger_fit.title_ids lost the title, so the next tap on it starts from no residual at all"
    )


async def test_a_tier_set_change_made_during_the_sweeps_fit_is_not_cleared(
    db, world, second, monkeypatch
):
    """The cutpoints row is locked only in the write phase, so a settings save lands at once."""
    from spielplan.rank import tiers

    patrick = world["patrick"]
    hp, _notes = load_hp(world["store"])
    await _rate(db, patrick)
    await refit.refit_user(
        db, user_id=patrick, kind="movie", hp=hp, embeddings=_basis(db, world),
        bundle_version=BUNDLE,
    )

    await tiers.save_tier_set(db, user_id=patrick, tier_set=["F", "D", "C", "B", "A"])
    first = {k: t for _u, k, t in await tiers.refits_owed(db)}
    assert set(first) == {"movie", "series"}, first

    landed: list[str] = []
    real = observations.load_observations

    async def a_put_lands_mid_fit(conn, **kwargs):
        loaded = await real(conn, **kwargs)
        if not landed:
            landed.append(kwargs["kind"])
            # A different connection: a settings save is a different request and must not wait for the fit.
            await tiers.save_tier_set(db, user_id=patrick, tier_set=NINE_TIERS)
        return loaded

    monkeypatch.setattr(observations, "load_observations", a_put_lands_mid_fit)
    with pytest.raises(refit.RefitRefused, match="tier set changed"):
        await asyncio.wait_for(
            refit.refit_user(
                second, user_id=patrick, kind="movie", hp=hp,
                embeddings=_basis(second, world), bundle_version=BUNDLE,
            ),
            timeout=30,
        )
    assert landed == ["movie"], f"the PUT has to land inside the fit, not {landed}"

    # The sweep clears the request it was handed, which is the older one.
    await tiers.clear_refit_request(
        db, user_id=patrick, kind="movie", requested_at=first["movie"]
    )
    still = {k for _u, k, _t in await tiers.refits_owed(db)}
    assert "movie" in still, (
        "the clear discarded a request made during the fit, so the person's second choice waits "
        "for the nightly job with nothing on screen saying so"
    )
    assert await tiers.tier_set_of(db, user_id=patrick, kind="movie") == tuple(NINE_TIERS)
    boundaries = await db.fetchval(
        "SELECT boundaries FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'", patrick
    )
    assert len(boundaries) == len(NINE_TIERS) - 1, (
        "the refused fit wrote cutpoints for the five-level set it fitted against"
    )


async def _cache_holds(conn, user_id: int, hp) -> set[int]:
    """What the NEXT tap can find a residual for."""
    cache = await refit.load_cache(conn, user_id=user_id, kind="movie", hp=hp, lock=False)
    assert cache is not None, "there is no cached fit at all"
    return {int(t) for t in cache.title_ids}
