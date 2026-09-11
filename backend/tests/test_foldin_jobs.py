"""The fold-in's jobs: the clock, the transaction, the debounce, one household.
Spec v2.1 5.3, 5.1, 4.2, §12 M2, 3.1.

`test_scoring.py` owns §5.1's arithmetic and the ranked read; this file owns what happens to that
arithmetic when a JOB runs it, sixty seconds at a time, against a household that is still rating.
Every defect here was invisible to both existing layers: the numpy half is correct and the route
half never runs a tick, so a fit that stamped the wrong clock, or wrote half of itself, or rewrote
a 14,000-row partition every minute, or fitted somebody the Ledger had skipped, passed the suite.

One idiom, used throughout. Every comparison is between Postgres's own clocks, and where a test
needs the debounce window to have elapsed it moves BOTH stamps back by the same interval
(`_wait_out_the_pause`) rather than sleeping thirty seconds: the ORDER of the two clocks is the
whole of what `_is_stale` reads, and shifting them together preserves it.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

# Five coordinated movies, which is `MIN_LABELS_FOR_CV` exactly. The sixth observation in these
# tests is always a RE-RATING: §4.2 makes changing your mind an INSERT that supersedes, so the label
# count does not move and a clock is the only thing that can carry the change. That is the arm dd16
# lives in, and a test that always rated a new title would never visit it.
SITTING: tuple[tuple[int, int], ...] = ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1))
RE_RATED = 3


@pytest.fixture
async def world(db, tmp_path):
    """A bundle, an owned library, and three accounts: two in the household, one deactivated.

    The import runs §10's own fold-in before any account exists, so everybody here starts genuinely
    unfitted - which matters, because "never fitted" is the one staleness the debounce may not gate.
    """
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
    """What `worker._fold_in_tick` runs, without the pool: `foldin.run` IS the tick's body."""
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


async def _partition(conn, user_id: int, kind: str = "movie") -> list[tuple]:
    """One (user, kind)'s score rows, each with the identity of the transaction that wrote it.

    `xmin` is what tells a rewrite from a read. A count or a set of title_ids cannot:
    `replace_scores` DELETEs and re-INSERTs the same titles with almost the same numbers, so every
    assertion about "did the tick rewrite the partition" that looked only at the contents would
    have passed whatever the tick did.
    """
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


# --- the debounce (perf-04) ---------------------------------------------------------------------


async def test_a_label_recorded_seconds_ago_does_not_rewrite_a_partition_on_the_next_tick(
    db, world
):
    """§12 M2 asks for a personal ranking "after a sitting", and the tick read that as "during".

    Any label newer than the fit made the pair stale, so every sixty seconds of a sitting rewrote
    the person's whole `user_score` partition: measured at 14k titles, 14,000 DELETEs and 14,000
    INSERTs, 325-590 ms and 5-8 MB of WAL per stale pair per tick, 0.7-1.5 s for two raters.
    Nobody reads that table between taps - §6.0's shelves are not on screen while Rate is - so the
    work was pure cost.

    The first fit is not gated, and the first assertion says so: "never fitted" is §6.0's
    zero-verdict state, not a cost question.
    """
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
    """The other half of the sentence: held back, not dropped.

    Three ticks around one re-rating - one inside the window, one after it, one after that - and
    exactly one refit in total. "One partition" is literal: this household has four (two people,
    two kinds) and the other three have to keep their rows, because `replace_scores` is per
    (user, kind) and a pass that rewrote a pair nobody touched would be the same cost somewhere
    else.
    """
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
    """A pause is not something a rating sitting can be relied on to contain.

    Somebody rating steadily for an hour never pauses, and the debounce alone would hold their
    shelves at the first fit all evening - the 24-hour failure this milestone is repairing, reached
    from the other side. So the window has a ceiling: five minutes without a refit and the next
    tick takes it, pause or no pause. Twelve rewrites per rater-hour at worst, against sixty.

    The control is the half that fails against the unfixed code, and it is what makes the cap mean
    anything: a tick that refits on any change satisfies the cap trivially.
    """
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
    """And nothing moving is still nothing to do.

    The debounce's predicate is "moved AND settled", and the cheap way to write it is the wrong
    one: a person who has not rated for an hour is `paused` and past the cap both, so a tick that
    asked only those two questions would rewrite every partition in the household every sixty
    seconds for ever. This is the guard on that - it held before the debounce existed and has to go
    on holding. The nightly pass at the end is the contrast: `only_stale=False` rewrites on
    purpose, because §10's re-import and a new placement are changes no label can report.
    """
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


# --- the fit that loses its work (ml08, dd16) ---------------------------------------------------


async def test_a_fold_in_whose_score_write_fails_leaves_no_user_vector_row(db, world, monkeypatch):
    """`write_fit` and `replace_scores` were two transactions on one connection, and the failure
    mode was silence.

    `user_vector` said "fitted, as of now"; `user_score` was empty; `_is_stale` compares the first
    against the labels and therefore reported fresh - so every §6.0 shelf returned EMPTY and every
    later tick declined to repair it, for up to 24 hours until the nightly pass. Nothing logged it,
    because by then nothing had failed. Reproduced with `label_count = 3` against zero score rows.

    Both directions are asserted, because a first fit and a later one fail differently: the first
    must leave NO row, and a later one must leave the OLD row exactly as it was. Either way the
    pair still reports stale, which is the property the shelves actually depend on.
    """
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

    # And again, over a fit that did land. Both stamps are read after the backdating, because
    # `_wait_out_the_pause` moves `updated_at` too and a value read before it would differ by the
    # shift rather than by anything this test is about.
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
    """The fit's clock has to be read BEFORE its labels, or a re-rating can vanish for ever.

    §4.2 makes changing your mind an INSERT and `LIVE_LABEL_SQL` takes the newest row per title, so
    the label COUNT does not move and the clock is the only thing that can carry it. Stamped with
    `now()` at write time, a verdict that committed after the labels were read and before the row
    was written had `created_at` BEFORE `updated_at` - which is exactly the comparison `_is_stale`
    makes. Not "late": never. Every later tick reported that pair fresh.

    The re-rating is committed from inside `live_labels`, which is the window, and the durable
    evidence is the ORDER of the two stamps rather than the tick that follows: a tick can be rerun,
    a stamp cannot be unwritten.
    """
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


# --- one household (ml04, decision 166) ---------------------------------------------------------


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
    """§5.3's two nightly passes iterate the same people, and they did not.

    `foldin.run` spelled `role IN ('admin', 'member')` and `refit.refit_all` spelled `is_active`.
    Decision 166 closes the other half - a guest is a Tonight session seat with no account - so the
    two predicates have exactly one way left to disagree, and it is the deactivated member below.
    """
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
    """The one account the two predicates disagreed about, and it was the expensive direction.

    A deactivated member got no Ledger from the nightly refit and a fresh `user_vector` plus a whole
    `user_score` partition from the fold-in - on every sixty-second tick, for somebody who cannot
    sign in. The active member beside them is the control: "skipped by both" has to mean this
    account, not this code path.
    """
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
    """Three readers, one sentence: `is_active AND role IN ('admin', 'member')`.

    §6.0's partner query already spelled the intersection, which is what makes it the household
    rather than a third opinion about it. It keeps its inline clause - it is one row of a co-seen
    ranking, not an id list - so the agreement is asserted here, behaviourally and in the source,
    rather than assumed from two queries that happen to read alike today.
    """
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


# --- the arithmetic the jobs rest on (dd16, ml03) -----------------------------------------------


def test_an_empty_reference_population_standardises_nothing_rather_than_storing_nan():
    """`np.std` over an empty array is NaN, and `if prior_sd < 1e-9` does not catch NaN.

    So a kind with no coordinated titles - a fresh household, §3.1's legal empty artifact store -
    stored NaN in `user_vector.prior_sd` and divided every score of that kind by it, while the
    worker log carried three numpy "Degrees of freedom <= 0" warnings per user per tick. The
    warning is turned into an error here, because that is the form the evidence arrived in.
    """
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
    """A household-shaped fold-in case: a few labelled titles, many unlabelled ones, two scales.

    The scale gap is the point and it is not invented: a Backbone row's norm runs with the crowd
    support behind it (0.006 to 5.4 on the real basis) and the titles a person has rated are the
    popular ones, so the labelled rows are long and the population they are standardised over is
    short. Measured on this fixture, the sd of the personal half over the labelled rows is 2.73x
    the sd over the reference.
    """
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
        # A crowd prior that is half taste and half noise, so neither half of §5.1's blend wins
        # outright and the chosen beta lands inside the grid rather than on its ceiling.
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
                b=float(rng.standard_normal()), gate=0.4, item_n=30, e_source="blended",
            )
        )
    return coords, reference, labels


def _held_out_table(
    coords, reference, labels, *, seed: int, over: str
) -> dict[tuple[float, float], float]:
    """§5.1's blend scored by held-out Spearman, with the personal half standardised `over` either
    the reference population (what serving does) or the labelled rows (what the search did).

    `fit_user`'s preprocessing is restated rather than reached into, because the claim under test
    is an equality between two independent spellings of one piece of arithmetic.
    """
    ref_e = np.ascontiguousarray([c.e for c in reference], dtype=np.float64)
    ref_b = np.asarray([c.b for c in reference], dtype=np.float64)
    prior_mean, prior_sd = float(ref_b.mean()), float(ref_b.std())
    ordered = sorted(labels, key=lambda pair: int(pair[0]))
    rows = [(coords[t].e, foldin.VERDICT_TO_Y[int(v)], coords[t].b) for t, v in ordered]
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
    """`_cross_validate`'s own tie-breaking, applied to both tables so the comparison isolates the
    arithmetic: a tie buys no personalisation, and within noise of best the smallest beta wins."""
    rho0 = table[(foldin.LAMBDA_GRID[0], 0.0)]
    best = max(table.values())
    if best - rho0 <= foldin.NOISE_FLOOR:
        return foldin.LAMBDA_GRID[-1], 0.0, rho0
    within = [key for key, rho in table.items() if best - rho <= foldin.NOISE_FLOOR]
    beta = min(key[1] for key in within)
    lam = min(key[0] for key in within if key[1] == beta)
    return lam, beta, table[(lam, beta)]


def test_the_cross_validation_standardises_each_fold_the_way_serving_will():
    """The same fold has to produce the same held-out prediction here and at serve time.

    It did not. `fit_user` divides the full-data `v` by the sd of the personal half over the
    reference population, while the search standardised its held-out predictions over the LABELLED
    rows - 2.73x apart on this fixture, 0.04x to 5.18x apart measured over 168 real raters on the
    real Backbone. So the beta this search reported was chosen against a personal half the app
    never serves, and it is a printed number: §6.0's why-line and §6.7's rail both carry it.

    Measured here: the fold-consistent arithmetic chooses beta 0.4 at rho 0.6792 and the old
    spelling chooses beta 0.5 at rho 0.6745. Not a tie under §0's 0.008 floor, and a different
    weight - which is why this is a repair and not a rounding.
    """
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


# --- the lock the two Ledger jobs share (data-05) ------------------------------------------------
#
# The defect this section exists for is the one shape a suite of single-connection tests cannot
# reach: two callers in flight at the same moment. `refit_user` read its observations outside any
# transaction and before any lock, fitted for seconds, and then wrote with `prune = True` - so a
# verdict recorded in between was reverted to the unobserved prior (measured: `s -0.5504
# observed=True` -> `s -0.0004 observed=False`) and dropped from `ledger_fit.title_ids`, where the
# next tap could not find it either. `load_cache`'s `FOR UPDATE` serialised taps against each other
# and nothing against the fit. [M4.13, data-05; plan step 24]

NINE_TIERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]

# The title the tap in these tests rates: `Tampopo`, the one movie the SITTING above leaves alone (6
# and 7 are the fixture's two series), so the fit that is in flight has never seen it and "did the
# fit prune it away" has an answer. 0022's composite (title_id, kind) FK refuses a series id here,
# which is how the first draft of this section found out.
UNRATED = 8


@pytest.fixture
async def second(db, pg_url):
    """A second connection, because an advisory lock is about two callers.

    `db` is one connection, and a lock taken on it is one its own next statement already holds - so
    nothing about serialisation can be asserted through it. The jsonb codec mirrors `db`'s for the
    same reason the fixture sets it there: a connection that returns jsonb as text is a different
    connection from the one the app uses.
    """
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
    """§5.1's composed source, on the connection that will read it. See `standard_embeddings`."""
    return observations.standard_embeddings(conn, world["backbone"], bundle_version=BUNDLE)


async def _a_tap_arrives_while_a_fit_is_running(db, second, world, monkeypatch, hp):
    """The choreography both tests below need, run once: (still_waiting, delta).

    The fit is held open at the point the defect needs it held - after `load_observations` has
    returned and before anything is written - which is the same hook `test_worker_jobs.py` uses for
    finding 5's tier-set race. A synchronous `model.fit` has no await point, so patching the read is
    the only way to produce the window without sleeping inside the arithmetic.

    The fit runs on the second connection and the tap on `db`, which is the right way round: the tap
    is the one whose outcome is asserted, and a task cancelled by a timeout must not be the one
    holding the transaction the assertions read.
    """
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

        # The person rates one more title while the fit is in the air. The observation is committed
        # by the route before the tap is called (`update_incrementally_reporting`'s docstring), so
        # this is two statements and not one.
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
    """§5.3's two rows are "<50 ms" and "seconds", and until M4.13 they shared no lock at all.

    Waiting is the whole repair. The alternative designs both lose something: refusing the tap
    throws away an observation the route has already committed, and letting it through is the race
    itself. A tap that waits out a nightly fit costs the person a slow card once a night and keeps
    every verdict - and on the interactive path it is the 60 s `tier-set-refit` sweep, not the
    nightly, that it is most likely to meet.

    The lock is taken BEFORE `load_cache` in the tap, which this test also pins by construction: a
    tap holding `ledger_fit` `FOR UPDATE` while queueing behind the fit would deadlock against the
    fit's own closing UPDATE of that row, and this would hang rather than pass.
    """
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
    """The outcome half, and the measurement the finding reported.

    Reproduced before the lock: the tap wrote the new verdict's row, the fit's `_write_state` then
    ran with `prune = True` over the title set it had read seconds earlier, and the title came back
    as `observed = False` at the unobserved prior - `s -0.5504` to `s -0.0004`. Worse than a stale
    number: `ledger_fit.title_ids` lost the title too, so the next tap on it found no cached
    residual and started from zero, and nothing anywhere raised.

    Both halves are asserted, because they fail in different places. `ledger_state` is what §6.0's
    shelves and §6.3's board read; `ledger_fit.title_ids` is what the NEXT tap reads.
    """
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
    """Decision 11's control invites a second change inside the minute, and the sweep cleared it.

    The fix is M4.10 finding 5's and is already shipped: `refits_owed` hands out the stamp it read,
    `clear_refit_request` clears only `refit_requested_at <= $3`, and `refit_user` refuses to write
    cutpoints for a tier set it did not fit against. `test_worker_jobs.py` asserts that through the
    60 s job; this asserts it at the functions, which is where M4.13's step 24 could have broken it
    and did not: the board lock is taken at the top of the fit's transaction, but the cutpoints row
    is still only locked in the WRITE phase - so a settings PUT made while the fit is running lands
    immediately instead of hanging for the seconds §5.3 gives the fit. A control that blocks is a
    control nobody tries twice, which is exactly the behaviour decision 11 is built around.
    """
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
            # On `db`, i.e. a different connection: a settings save is a different request, and it
            # must not be waiting for the fit that is reading.
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
    """The title ids in the cached fit - what the NEXT tap can find a residual for."""
    cache = await refit.load_cache(conn, user_id=user_id, kind="movie", hp=hp, lock=False)
    assert cache is not None, "there is no cached fit at all"
    return {int(t) for t in cache.title_ids}
