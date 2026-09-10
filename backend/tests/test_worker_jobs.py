"""§5.3's nightly passes, actually run. Spec v2.1 §5.3, §5.2, §5.1, §3.1.

`test_worker_schedule.py` asserts the registry is a faithful copy of §5.3's table — the right
jobs, the right milestones, the right cadences. What it cannot see is whether a job's `run`
callable works, because a registry entry is satisfied by any callable at all.

That gap has teeth here: all three M2 jobs were wired to functions in three modules built in
parallel by three people who never imported each other's code. A wrong keyword, a renamed
dataclass field, an interface that turned out to be async — every one of those is invisible
until 3 a.m. on the night the job first fires, and a background job that raises leaves no
surface anywhere in the product.

So each job is run twice: once against a household with no bundle, which §3.1 makes a legal
state the job must skip rather than crash in, and once against a real imported bundle with real
observations, where it has to produce the writes the surfaces read.
"""

from __future__ import annotations

import numpy as np
import pytest

from spielplan import worker
from spielplan.core.config import settings
from spielplan.db import pool
from spielplan.importer import bundle as bundle_import
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.hyperparams import load as load_hp
from spielplan.models.artifacts import ArtifactStore
from tests.fixtures import make_bundle as fx

pytestmark = pytest.mark.anyio

M2_JOBS = ("ledger-map-refit", "fold-in-user-vectors", "placement-reconciliation")
# M3 adds one, and it earns the same treatment for the same reason: decision 11's "queued
# for that user alone" is serviced by a callable nothing else calls.
M3_JOBS = ("tier-set-refit",)


@pytest.fixture
async def worker_env(db, pg_url, tmp_path, monkeypatch):
    """The worker's own view of the world: a real pool and a real data dir.

    The jobs take no connection — they acquire from the pool, exactly as the loop calls them —
    so this opens the real pool against the test database rather than handing them `db`. That is
    the point: the production call path is what is under test.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    (tmp_path / "data" / "artifacts").mkdir(parents=True)
    await pool.open_pool(pg_url)
    try:
        yield tmp_path
    finally:
        await pool.close_pool()
        settings.cache_clear()


async def _import_bundle(conn, tmp_path):
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        conn, bundle_import.Bundle.open(root), tmp_path / "data" / "artifacts"
    )
    assert report.ok, report.render()
    return ArtifactStore.open(tmp_path / "data" / "artifacts" / "test-v1", "test-v1")


@pytest.fixture
async def two_members(db):
    rows = []
    for name, role in (("Patrick", "admin"), ("Ana", "member")):
        rows.append(
            await db.fetchval(
                "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
            )
        )
    return rows


# --- the bundle-less household (§3.1) ---------------------------------------------------------


@pytest.mark.parametrize("name", M2_JOBS + M3_JOBS)
async def test_a_nightly_job_skips_a_household_with_no_bundle_rather_than_failing(
    name, worker_env, two_members
):
    """§3.1: "an empty artifact store is legal" — a household can run for a week before any
    corpus export exists. A nightly job that raises on that is a job that fills the log with a
    stack trace every night for a state the spec calls normal.

    The Ledger job is the interesting one: it must still *run*, because §5.2's fit works with no
    embeddings at all (that is what `embedded=False` is for), and skipping it would leave a
    bundle-less household with no tiers.
    """
    job = next(j for j in worker.JOBS if j.name == name)
    assert job.run is not None, f"the registry lists {name}, so its milestone owes it code"
    await job.run()


# --- the real thing ---------------------------------------------------------------------------


@pytest.fixture
async def rated(db, worker_env, two_members):
    """A bundle, and one member with enough observations for every arm to have something."""
    store = await _import_bundle(db, worker_env)
    patrick = two_members[0]
    await db.execute("UPDATE title SET is_owned = true")
    for title_id, value in ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1), (6, 2), (7, 0)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
    await observations.record_duel(
        db, user_id=patrick, title_a=1, title_b=2,
        outcome="A", context="profile_battle", decisive=True, hp=DEFAULTS,
    )
    await observations.record_tier_edit(db, user_id=patrick, title_id=1, tier=6)
    return store, patrick


async def test_the_nightly_refit_writes_a_tier_for_every_owned_title(rated, db):
    """§5.2: "refit nightly (full-history MAP)". §12's M2 exit criterion is that every owned
    title carries a ledger state, not only the ones that were rated."""
    _store, patrick = rated
    job = next(j for j in worker.JOBS if j.name == "ledger-map-refit")
    await job.run()

    rows = await db.fetch(
        "SELECT title_id, s, sigma, tier, observed FROM ledger_state WHERE user_id = $1",
        patrick,
    )
    assert rows, "the nightly refit wrote nothing"
    assert all(r["s"] == r["s"] for r in rows), "a NaN `s` sorts above every real on §6.0's shelves"
    assert any(r["observed"] for r in rows) and any(not r["observed"] for r in rows), (
        "both branches must be exercised, or this passes on a board of one kind of row"
    )


async def test_the_nightly_fold_in_writes_a_user_vector_and_the_priors_it_needs(rated, db):
    """§5.3: "User fold-in + blend weights — nightly". §5.1's score needs both halves: the
    per-user vector and `title_prior`, and a fold-in that wrote one without the other would
    leave every score null while looking like it had run."""
    _store, patrick = rated
    job = next(j for j in worker.JOBS if j.name == "fold-in-user-vectors")
    await job.run()

    vec = await db.fetchrow(
        "SELECT vec, blend_beta, label_count FROM user_vector WHERE user_id = $1 AND kind = 'movie'",
        patrick,
    )
    assert vec is not None and vec["label_count"] > 0
    # `blend_beta` is `real`, so §5.1's ceiling reads back as float4(0.8) = 0.800000011920929.
    # Comparing it to 0.8 exactly is the same widening bug 0009's CHECK had.
    assert 0.0 <= vec["blend_beta"] <= float(np.float32(0.8)), "§5.1 caps β at the optimum"
    assert await db.fetchval("SELECT count(*) FROM title_prior") > 0


async def test_the_nightly_sweep_leaves_no_owned_title_without_a_coordinate(rated, db):
    """§12's M2 exit criterion, run through the job that is supposed to guarantee it rather than
    through the function the job calls."""
    job = next(j for j in worker.JOBS if j.name == "placement-reconciliation")
    await job.run()
    assert await db.fetchval(
        "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
    ) == 0


async def test_the_nightly_refit_generalises_to_titles_the_person_never_rated(
    db, worker_env, two_members
):
    """§5.2's whole point: "Generalisation via the 64-d user vector". §12's exit criterion is
    that 50-100 verdicts produce *visibly personal rankings*, and a ranking is only personal
    over titles the person has not themselves rated.

    This is the test the previous one could not be. It asserted that `ledger_state` rows exist
    and that `s` is not NaN — both true when the fit is degenerate. The nightly job passed the
    Cold Tower placement source alone, and `classify_warm` deliberately writes no
    `title_placement` row for a warm title, so every Backbone-covered title entered the MAP fit
    with e = 0. `v` came out at zero, and `refit` then wrote every unrated owned title as
    `s = mu + 0`: one identical score, one identical tier, one identical straddle badge across
    the whole library, behind numbers that all looked well-formed.

    So the assertion is that the unrated titles are told APART, which no degenerate fit can do.
    """
    store = await _import_bundle(db, worker_env)
    patrick = two_members[0]
    await db.execute("UPDATE title SET is_owned = true")

    # Rate two of the warm titles and leave the rest — the shape §12 actually describes, where
    # most of the library is unrated and the vector has to carry the ordering.
    await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=6, value=0)

    job = next(j for j in worker.JOBS if j.name == "ledger-map-refit")
    await job.run()

    warm = [
        r["id"] for r in await db.fetch(
            "SELECT id FROM title WHERE is_owned AND placement = 'warm' ORDER BY id"
        )
    ]
    assert len(warm) >= 3, "the fixture must leave warm titles for this to say anything"

    rows = await db.fetch(
        "SELECT title_id, s, tier FROM ledger_state "
        " WHERE user_id = $1 AND NOT observed AND title_id = ANY($2::int[])",
        patrick, warm,
    )
    assert len(rows) >= 2, "at least two unrated warm titles are needed to compare"
    scores = {round(float(r["s"]), 9) for r in rows}
    assert len(scores) > 1, (
        "every unrated warm title got the same score — the fit saw no coordinates for them, so "
        f"the user vector carries no information: {sorted(scores)}"
    )

    # And the vector itself is non-trivial. A zero `v` is exactly what a placement-only basis
    # produces on a Backbone-covered library, and it is invisible in `s` alone.
    # Under the constants the JOB fitted with, which are the bundle's — `_ledger_map_refit`
    # calls `hyperparams.load(store)` and §4.3 makes those numbers the corpus's to tune. Read
    # back under `DEFAULTS` this is a cache miss by design (`ledger_fit.hp_digest` is a
    # precondition, not a hint), and the fixture ships the corpus's own λ_bt, learning rate and
    # step count rather than this app's fallbacks since M4.5.
    hp, _notes = load_hp(store)
    assert hp.source == "bundle" and hp.digest() != DEFAULTS.digest(), (
        "the fixture bundle must tune something, or this asserts nothing about the digest"
    )
    cache = await refit.load_cache(db, user_id=patrick, kind="movie", hp=hp, lock=False)
    assert cache is not None
    assert float(np.linalg.norm(cache.v)) > 1e-6, (
        "the 64-d user vector is zero: §5.2's generalisation arm never ran"
    )


async def test_a_sitting_of_verdicts_moves_the_ranking_the_shelves_are_built_from(
    db, worker_env, two_members
):
    """§12's M2 exit criterion, end to end: "50-100 verdicts each produce **visibly personal
    rankings**".

    Nothing tested this loop. `test_home.py` hand-writes `user_score` and `user_vector` rows, so
    every shelf assertion is made against a fabricated fold-in; `test_rate_session.py` stops at
    `ledger_state`; `test_scoring.py` starts from labels already in the database. Each half was
    correct against an interface the other half never called, and the join between them — the
    one thing the milestone exists to demonstrate — was not exercised anywhere.

    It did not work. Rating writes `ledger_state`, so the tier badges moved within the sitting;
    every §6.0 shelf orders by `user_score`, which only the fold-in writes, and the fold-in ran
    nightly. A household could rate all evening and watch the badges change while the shelves
    stayed in the order they had that morning.
    """
    from spielplan.scoring import serve

    await _import_bundle(db, worker_env)
    patrick = two_members[0]
    await db.execute("UPDATE title SET is_owned = true")

    for title_id, value in ((1, 2), (2, 2), (3, 1), (4, 0), (5, 0)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
    await next(j for j in worker.JOBS if j.name == "fold-in-tick").run()

    before = await db.fetch(
        "SELECT title_id, score FROM user_score WHERE user_id = $1 AND kind = 'movie'"
        " ORDER BY score DESC, title_id",
        patrick,
    )
    assert before, "the fold-in wrote no scores at all, so no shelf can be built"
    order_before = [r["title_id"] for r in before]

    # A second sitting that contradicts the first: what was liked is now disliked. If the
    # ranking the shelves read cannot notice that, it is not personal.
    for title_id, value in ((1, 0), (2, 0), (3, 0), (4, 2), (5, 2)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
    await next(j for j in worker.JOBS if j.name == "fold-in-tick").run()

    after = await db.fetch(
        "SELECT title_id, score FROM user_score WHERE user_id = $1 AND kind = 'movie'"
        " ORDER BY score DESC, title_id",
        patrick,
    )
    scores_before = {r["title_id"]: float(r["score"]) for r in before}
    scores_after = {r["title_id"]: float(r["score"]) for r in after}
    assert scores_before != scores_after, (
        "a sitting that reversed every verdict moved no score — the table every §6.0 shelf "
        "orders by is not reachable from the Rate surface"
    )

    # And the read the shelves actually make sees it.
    section = await serve.ranked_section(
        db, user_id=patrick, kind="movie", bundle_version="test-v1"
    )
    assert [item["id"] for item in section["items"]] == [r["title_id"] for r in after]
    assert order_before != [r["title_id"] for r in after], (
        "the reversed sitting left the shelf in the same order"
    )


# --- M3: decision 11's second trigger for §5.3's nightly fit -----------------------------------


async def test_the_tier_set_refit_job_fits_the_person_who_asked_and_clears_the_request(rated, db):
    """Decision 11: "a Ledger refit is queued for that user alone".

    The save re-initialises the boundaries to equal-mass quantiles immediately, so the board is
    usable at once; the *fit* is what this job does, and until it runs those boundaries are
    quantiles of the old `s` rather than cutpoints of the new likelihood. A job that dropped the
    request without fitting would look identical on the board and be wrong in the model.
    """
    from spielplan.rank import tiers

    _store, patrick = rated
    await (next(j for j in worker.JOBS if j.name == "ledger-map-refit").run())
    await tiers.save_tier_set(db, user_id=patrick, tier_set=["bad", "ok", "good"])
    assert await tiers.refits_owed(db), "the save has to queue something for the job to service"

    job = next(j for j in worker.JOBS if j.name == "tier-set-refit")
    await job.run()

    assert await tiers.refits_owed(db) == [], "a serviced request is cleared, not left to loop"
    boundaries = await db.fetchval(
        "SELECT boundaries FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'", patrick
    )
    assert len(boundaries) == 2, "§4.2: length = |tier set| - 1"
    tier = await db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id = $1 AND title_id = 1", patrick
    )
    assert tier is not None and 0 <= tier <= 2, (
        "the board was re-tiered against the new set, not left indexing the old one"
    )


async def test_the_tier_set_refit_job_is_a_no_op_when_nobody_asked(rated, db):
    """It runs every minute. A job that did work on an empty queue would be a nightly MAP fit
    sixty times an hour."""
    from spielplan.rank import tiers

    _store, patrick = rated
    assert await tiers.refits_owed(db) == []
    before = await db.fetchval("SELECT count(*) FROM ledger_state")
    await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())
    assert await db.fetchval("SELECT count(*) FROM ledger_state") == before


# --- M4.10: the sweep, per item ----------------------------------------------------------------
#
# Two properties, and they belong together because each one is what makes the other observable.
# The tier-set half (finding 5) lives in `rank/tiers.py` and `ledger/refit.py`: `refits_owed`
# hands out the stamp it read, `clear_refit_request` clears only what it was handed, and
# `refit_user` refuses to write cutpoints for a tier set it did not fit against. The isolation
# half (finding 6) lives here: without a per-item `try` the sweep dies on the first refusal, so
# the second owed row is never reached and nothing downstream of it can be asserted at all.
#
# Both halves shipped in the same milestone and in different files. Nobody else checks that they
# meet, which is what these two tests are for. [M4.10 findings 5 and 6; decision 11]

NINE_TIERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]


async def test_a_tier_set_put_that_lands_during_the_sweep_is_not_reverted_and_is_still_owed(
    rated, db, monkeypatch
):
    """The measured failure, through the job that produced it.

    Reproduced before the fix: PUT K = 5 queued a refit, PUT K = 9 landed while the fit was
    running, and after the sweep the movie row read K = 5 with `refit_requested_at` NULL - the
    person's second choice reverted and its request discarded - while the series row read K = 9.
    `GET /api/rank/tiers` then answered five labels and a legal drop into tier 7 came back 422.
    Decision 11's control is a settings pill that invites exactly this ("how many tiers do I
    want?" is answered by trying one), so a change made twice inside a minute is ordinary use.

    The PUT is issued from `load_observations`' return rather than from its entry, and the
    ordering is the whole fixture: the fit has to have READ K = 5 before the person chooses
    K = 9, or the compare-and-set sees no conflict and this asserts nothing. A second connection
    (`db`) rather than the sweep's own, because a settings save is a different request.
    """
    from spielplan.rank import drop as drop_rules
    from spielplan.rank import tiers

    _store, patrick = rated
    await (next(j for j in worker.JOBS if j.name == "ledger-map-refit").run())
    await tiers.save_tier_set(db, user_id=patrick, tier_set=["F", "D", "C", "B", "A"])
    owed = await tiers.refits_owed(db)
    assert [k for _u, k, _t in owed] == ["movie", "series"], owed

    real = observations.load_observations
    landed: list[str] = []

    async def a_put_lands_mid_fit(conn, **kwargs):
        loaded = await real(conn, **kwargs)
        if not landed:
            landed.append(kwargs["kind"])
            await tiers.save_tier_set(db, user_id=patrick, tier_set=NINE_TIERS)
        return loaded

    monkeypatch.setattr(observations, "load_observations", a_put_lands_mid_fit)
    detail = await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())

    assert landed == ["movie"], f"the PUT has to land inside the first fit, not {landed}"
    assert detail is not None and len(detail["refits"]) == 2, (
        "the sweep stopped at the item that refused, so the second owed row was never visited"
    )
    refused = [r for r in detail["refits"] if r["error"]]
    assert len(refused) == 1 and refused[0]["kind"] == "movie", detail["refits"]
    assert "tier set changed" in refused[0]["error"], refused[0]["error"]

    # Both rows read the person's last choice. The films row is the one the fit was about to
    # overwrite; the series row is the one that never disagreed and must not start now, because
    # every caller that asks for a set asks for one kind's.
    assert await tiers.tier_set_of(db, user_id=patrick, kind="movie") == tuple(NINE_TIERS)
    assert await tiers.tier_set_of(db, user_id=patrick, kind="series") == tuple(NINE_TIERS)

    # And the request the person is actually waiting on survived the sweep that did not serve it.
    still_owed = {(u, k) for u, k, _t in await tiers.refits_owed(db)}
    assert still_owed == {(patrick, "movie"), (patrick, "series")}, still_owed

    # §6.3's board, at the top of a set that only exists if the PUT held: tier 7 of nine.
    await drop_rules.drop(db, user_id=patrick, title_id=1, tier=7, title_name="Heat")
    await drop_rules.drop(db, user_id=patrick, title_id=6, tier=7, title_name="Severance")
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND tier = 7", patrick
    ) == 2


async def test_one_members_failing_refit_does_not_strand_another_members(
    rated, db, two_members, monkeypatch
):
    """Decision 11 queues a refit "for that user alone". A loop with no per-item guard made one
    person's bad fit everybody's.

    Reproduced: the job raised on the first owed row and all four stayed owed, so the tick
    re-ran sixty seconds later, and again, on the loop that also carries §7.3's playback poll and
    §2's nightly dump - one member burning a full MAP fit a minute while everyone behind them in
    `ORDER BY refit_requested_at` waited for ever. `refit_all` has isolated per user since M2
    ("One person's bad fit must not stop the others'"); this loop is the one that did not.

    The refusal is injected at `refit_user` because that is the call this loop makes and the
    behaviour under test is this loop's. The real raise is `ledger/refit.py`'s non-finite guard,
    and Ana's fit is the genuine article - the wrapper delegates for her - so the test is not two
    stubs agreeing with each other.
    """
    from spielplan.rank import tiers

    _store, patrick = rated
    ana = two_members[1]
    for title_id, value in ((1, 2), (2, 0), (3, 1), (6, 2), (7, 0)):
        await observations.record_verdict(db, user_id=ana, title_id=title_id, value=value)
    await (next(j for j in worker.JOBS if j.name == "ledger-map-refit").run())

    await tiers.save_tier_set(db, user_id=patrick, tier_set=["bad", "ok", "good"])
    await tiers.save_tier_set(db, user_id=ana, tier_set=["bad", "ok", "good", "great"])
    owed = await tiers.refits_owed(db)
    assert [u for u, _k, _t in owed] == [patrick, patrick, ana, ana], (
        f"the failing member has to be at the head of the queue or this asserts nothing: {owed}"
    )

    real = refit.refit_user

    async def not_finite_for_patrick(conn, *, user_id, kind, **kwargs):
        if user_id == patrick:
            raise refit.RefitRefused(
                f"user {user_id}/{kind}: the fit's dense block is not finite; ledger_state and "
                "ledger_cutpoints keep their previous values"
            )
        return await real(conn, user_id=user_id, kind=kind, **kwargs)

    monkeypatch.setattr(refit, "refit_user", not_finite_for_patrick)
    detail = await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())

    assert detail is not None, "a sweep with four owed rows reported nothing"
    reports = detail["refits"]
    assert len(reports) == 4, f"the sweep visited {len(reports)} of 4 owed rows"
    assert {r["user_id"] for r in reports if r["error"]} == {patrick}
    ana_fits = [r for r in reports if r["user_id"] == ana]
    assert all(r["error"] is None for r in ana_fits), ana_fits
    ana_movies = next(r for r in ana_fits if r["kind"] == "movie")
    assert ana_movies["fitted"] and len(ana_movies["cutpoints"]) == 3, (
        "Ana's fit has to have actually run against her four-level set, not merely been reached"
    )

    # Cleared even where it failed. A permanently failing fit that stays owed is a full MAP fit
    # every sixty seconds for ever; §5.3's nightly pass fits the same (user, kind) anyway, so the
    # work moves to the cadence such a failure deserves rather than being lost.
    assert await tiers.refits_owed(db) == [], (
        "a fit that raises leaves its request owed, so the tick re-runs it a minute later"
    )
    assert await db.fetchval(
        "SELECT max(tier) FROM ledger_state WHERE user_id = $1 AND kind = 'movie'", ana
    ) <= 3, "Ana's board is still indexed against the seven-level set the fit replaced"
