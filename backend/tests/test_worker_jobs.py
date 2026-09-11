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

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import asyncpg
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


async def _wait_out_the_pause(conn, user_id: int) -> None:
    """Make the fold-in tick's debounce window have elapsed, without sleeping through it.

    `foldin._is_stale` refits only a pair that moved AND settled (`foldin.PAUSE_SECONDS`), so a
    test that rates and ticks in the same millisecond is asking for the one thing the debounce
    exists to refuse. Both clocks shift by the same interval, so every ordering between them --
    which is all `_is_stale` compares -- is preserved. [M4.13, perf-04; plan step 22]
    """
    from spielplan.scoring import foldin

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
    # The sitting has to be OVER. The tick is debounced since M4.13's step 22 — §12 M2's sentence
    # is "after a sitting", and a partition rewritten every sixty seconds while somebody is still
    # rating is 14,000 deletes and 14,000 inserts nobody reads — so a label written this instant is
    # deliberately not refit. Moving both stamps back by the same interval is what waiting the
    # pause out looks like without spending it: the ORDER of the two clocks, which is what
    # staleness is about, is untouched. The first sitting above needs nothing, because a member who
    # has never been fitted is stale whatever the clocks say. [M4.13, perf-04]
    await _wait_out_the_pause(db, patrick)
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


# --- M4.11: §5.3's budget, the prune that reaches, and the two failures the log never named ----
#
# All five are about the loop rather than about the work: one job that never returns used to be
# the end of this process as a worker, and the two sweeps it carries could fail completely while
# writing a log indistinguishable from a quiet, healthy household. [M4.11 findings 9, 17, 21]

# A household afternoon, in the shape `_tick` wants it. A fixed offset rather than a named zone:
# a Windows checkout has no system tz database, which is what `worker._now_local`'s fallback is
# for, and `due` reads `.hour` and `.date()` and nothing else.
NOON = datetime(2026, 9, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))

# Far longer than any budget a test declares, so "it stopped" and "it finished" cannot be
# confused: a job that sleeps this long and a tick that returns in a fraction of it is the whole
# assertion.
WEDGED_SECONDS = 30


def test_every_job_this_loop_fires_declares_a_budget_that_fits_inside_its_interval():
    """§5.3 gives every job a budget, and until M4.11 the column was prose.

    Two properties, and the second is the one with teeth. A budget longer than the job's own
    interval is a budget that cannot be kept: `_tick` awaits due jobs one after another, so such a
    job can only hold its cadence by eating the slot of everything behind it — and the three
    60-second rows are what §7.3's prompt timing and §12's M2 exit criterion rest on.

    The backup is checked against the other timeout in the codebase rather than against a number
    written here. `backup/nightly.DUMP_TIMEOUT_SECONDS` kills a blocked `pg_dump` and deletes the
    half-written file; a job budget at or under it would fire first, abandon the job while the
    child process kept running, and make that constant unreachable. The two live in different
    modules, so nothing but this assertion holds them in order.
    """
    from spielplan.backup import nightly

    for job in worker.JOBS:
        if job.run is None:
            continue
        assert job.timeout > 0, f"{job.name} fires with no budget at all"
        assert job.timeout <= job.every, (
            f"{job.name}: a {job.timeout}s budget does not fit inside its own {job.every}s "
            "interval, so keeping its cadence costs every job behind it in the tick"
        )

    backup = next(job for job in worker.JOBS if job.name == "nightly-backup")
    assert backup.timeout > nightly.DUMP_TIMEOUT_SECONDS, (
        f"the backup's {backup.timeout}s budget fires before pg_dump's own "
        f"{nightly.DUMP_TIMEOUT_SECONDS}s timeout, which leaves the child running and the "
        "partial file behind"
    )


async def test_a_job_that_never_returns_is_abandoned_at_its_budget_and_the_tick_goes_on(
    worker_env, db, caplog, monkeypatch
):
    """The failure this whole field exists for, met once instead of never.

    `_tick` awaited `job.run()` with nothing around it, so one job that does not return takes the
    entire worker offline — permanently and invisibly. No finish prompts, no refit, no fold-in, no
    placement sweep, no nightly dump, not even the heartbeat file the compose healthcheck reads,
    with the process alive and `docker compose ps` saying Up. It is not hypothetical: the sweep
    reads the whole library and `all_items` paged for ever against a server that ignores
    `StartIndex` (`connectors/jellyfin.MAX_PAGES` is the other half of the same finding).

    So the assertions are the three facts an operator needs: the tick came back, the job after the
    wedged one ran, and both outcomes are in `job_run` — a row that says "abandoned" and names the
    budget is the difference between a reader who knows and a reader who waits. The log line is
    asserted too, because §6.6 names the log as the operator's data and an ERROR is what a
    `docker compose logs` grep finds. [M4.11 finding 17; §5.3, §8]
    """
    ran: list[str] = []

    async def never_returns() -> dict[str, object]:
        await asyncio.sleep(WEDGED_SECONDS)
        ran.append("wedged")  # unreachable while the budget holds, and that is the point
        return {"finished": True}

    async def the_next_job() -> dict[str, object]:
        ran.append("after")
        return {"ok": True}

    monkeypatch.setattr(
        worker,
        "JOBS",
        (
            worker.Job("t-wedged", "M0", "test", "ms", never_returns, every=60, timeout=0.1),
            worker.Job("t-after", "M0", "test", "ms", the_next_job, every=60, timeout=5),
        ),
    )

    with caplog.at_level(logging.ERROR, logger="spielplan.worker"):
        started = time.monotonic()
        await worker._tick(0.0, NOON, {}, {})
        elapsed = time.monotonic() - started

    assert elapsed < WEDGED_SECONDS / 2, (
        f"the tick waited {elapsed:.1f}s on a job with a 0.1s budget"
    )
    assert ran == ["after"], f"the tick did not get past the job that hung: {ran}"

    rows = {r["name"]: r for r in await db.fetch("SELECT name, ok, detail FROM job_run")}
    assert set(rows) == {"t-wedged", "t-after"}
    assert rows["t-wedged"]["ok"] is False
    assert "0.1s budget" in rows["t-wedged"]["detail"]["error"], rows["t-wedged"]["detail"]
    assert rows["t-after"]["ok"] is True, "the job behind the wedged one never reported"
    assert "t-wedged did not finish within its 0.1s budget" in caplog.text, caplog.text


async def test_a_subscription_that_never_delivered_is_pruned_by_age_and_a_live_one_is_not(
    worker_env, db, two_members
):
    """§4.2's push targets, pruned by the only clock a never-delivered row has.

    This prune filtered on `last_seen_ok IS NOT NULL`, so the row it exists for was the one row
    it could never reach: measured before the fix, a 400-day-old subscription with a NULL
    `last_seen_ok` survived this statement, which is a bearer capability (`push/send.py`) kept for
    ever for a phone whose browser dropped its `PushSubscription` a year ago.

    Four rows, because `COALESCE` has two directions and only one of them is the bug. The
    year-old-but-pushed-to-yesterday row is the assertion that matters most: a bare `created_at`
    filter would delete the household's actual phone.

    What that row cannot say by itself is how it comes to have a `last_seen_ok` at all — while
    `api/push.py` reset the column on every re-post, an /account open turned this exact row into
    the first one and the live phone was deleted that night.
    `test_push.py::test_resubscribing_keeps_the_delivery_mark_so_the_nightly_prune_spares_a_live_phone`
    is that composition, driven through the route; these four are the statement's own arithmetic.
    [M4.11 finding 21; review cycle 1; §4.2]
    """
    patrick = two_members[0]
    for label, created_days, seen_days in (
        ("never-delivered-and-old", 400, None),
        ("never-delivered-and-new", 10, None),
        ("delivered-and-then-silent", 500, 400),
        ("old-but-still-delivering", 400, 1),
    ):
        await db.execute(
            "INSERT INTO push_subscription "
            "  (user_id, device_label, endpoint, p256dh, auth, created_at, last_seen_ok) "
            "VALUES ($1, $2, $3, 'p256dh', 'auth', "
            "        now() - ($4::int * interval '1 day'), "
            "        CASE WHEN $5::int IS NULL THEN NULL "
            "             ELSE now() - ($5::int * interval '1 day') END)",
            patrick, label, f"https://push.example.test/{label}", created_days, seen_days,
        )

    await next(j for j in worker.JOBS if j.name == "push-subscription-prune").run()

    left = {r["device_label"] for r in await db.fetch("SELECT device_label FROM push_subscription")}
    assert left == {"never-delivered-and-new", "old-but-still-delivering"}, left


async def test_a_playback_session_that_matched_no_title_is_named_in_the_log(
    worker_env, caplog, monkeypatch
):
    """§7.3's prompt that never arms, and the one place that could say why.

    `WatchReport.unresolved` has always been filled — a session whose item is in no
    `title_jellyfin_item` row and whose ProviderIds matched nothing (§7.1) — and it went into
    `job_run.detail` and no further. A household whose television plays something this app cannot
    attach to a title simply gets no prompt, and before this line nothing anywhere said so.

    The poll itself is substituted, because the behaviour under test is this loop's logging and
    not the resolver's: `sync/playback.py` is tested against a real fake Jellyfin in
    `test_playback_prompt.py`, and driving a genuinely unresolvable session from here would assert
    the same thing through four more moving parts. [M4.11 finding 9]

    `_last_unresolved` is reset because it is module state that outlives one test: the line is a
    state change now, so a sibling that named the same stranger first would make this one DEBUG.
    """
    from spielplan.sync import playback

    async def poll_with_a_stranger(conn, client=None):
        return playback.WatchReport(watching=1, unresolved=["jf-who-is-this"])

    monkeypatch.setattr(playback, "poll", poll_with_a_stranger)
    monkeypatch.setattr(worker, "_last_unresolved", None)
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        detail = await next(j for j in worker.JOBS if j.name == "jellyfin-sessions-poll").run()

    assert detail is not None and detail["unresolved"] == ["jf-who-is-this"]
    assert "1 session(s) matched no title: jf-who-is-this" in caplog.text, caplog.text


async def test_the_same_unresolvable_session_is_named_once_and_not_once_a_minute(
    worker_env, caplog, monkeypatch
):
    """The condition is persistent, so the line has to be a state and not a stream.

    `_observe` re-derives `unresolved` from `/Sessions` on every pass and writes nothing, and
    `sessions()` filters on nothing but a `NowPlayingItem` — so a film paused at 95% on the
    living-room client produces the identical line once a minute until that client disconnects.
    The code's own comment concedes the producing condition is "ordinary on a library this app
    has not imported", where every finished playback is unresolvable, so the ordinary case IS the
    repeating case: ~480 identical lines by morning, interleaved with the nightly backup and the
    refit reports §6.6 promises the operator. That is the arithmetic M4.11 finding 18 (`ops-15`)
    used to rate-limit the sibling job in this same module, and `DURATION_LOG_THRESHOLD` states
    the rule for the three 60-second jobs outright.

    A NEW stranger is still loud, which is the half a plain "log it once" would lose.
    [review cycle 1: m411-rev1-unresolved-session-logs-a-line-a-minute]
    """
    from spielplan.sync import playback

    strangers = ["jf-who-is-this"]

    async def poll_with_a_stranger(conn, client=None):
        return playback.WatchReport(watching=1, unresolved=list(strangers))

    monkeypatch.setattr(playback, "poll", poll_with_a_stranger)
    monkeypatch.setattr(worker, "_last_unresolved", None)
    job = next(j for j in worker.JOBS if j.name == "jellyfin-sessions-poll")

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        for _ in range(4):
            await job.run()
        named = [r for r in caplog.records if "matched no title" in r.getMessage()]
        assert len(named) == 1, (
            f"four polls of one paused session put {len(named)} lines in the log the operator is "
            "asked to read; the set never changed"
        )

        strangers.append("jf-and-who-is-this")
        await job.run()
        named = [r for r in caplog.records if "matched no title" in r.getMessage()]
        assert len(named) == 2, "a stranger that was not there before is news"
        assert "jf-and-who-is-this" in named[1].getMessage()


async def test_a_sweep_whose_played_writes_all_failed_is_an_error_in_the_log(
    worker_env, caplog, monkeypatch
):
    """The sweep's worst outcome, which used to produce the log of its best one.

    The INFO line fired on `pushed or adopted or needs_relink`. A server below §7.1's pin, a proxy
    that drops DELETE, a 500 on every write: all of them push nothing, adopt nothing and flag no
    re-link, so the entire app->Jellyfin direction could be dead for the life of an install while
    the log read exactly like a quiet household. `push_failed` is counted now, and an ERROR is
    what makes it findable — §6.6 promises the operator "last syncs", and this is the line that
    tells them the half that is not working.

    `sync_all` is substituted for the same reason the poll is above: the counters are
    `test_seen_sync.py`'s to earn against a real fake, and this asserts only that the loop says
    them out loud. [M4.11 findings 3, 21; §7.3]
    """
    from spielplan.sync import seen

    async def a_sweep_that_could_not_write(conn, client=None):
        report = seen.SyncReport(unchanged=3, push_failed=2, users=["patrick"])
        report._note_push_error("POST /UserPlayedItems/jf-1 -> 404")
        return report

    monkeypatch.setattr(seen, "sync_all", a_sweep_that_could_not_write)
    # Module state that outlives one test: the ERROR is a state change now, so a sibling that
    # named the same reason first would make this one DEBUG. Same reason as `_last_unresolved`.
    monkeypatch.setattr(worker, "_push_failure_reported", None)
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        detail = await next(j for j in worker.JOBS if j.name == "jellyfin-seen-sync").run()

    assert detail is not None and detail["push_failed"] == 2
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"a sweep that wrote nothing it owed logged no error: {caplog.text}"
    assert "2 Played write(s) failed" in errors[0].getMessage()
    assert "-> 404" in errors[0].getMessage()
    # And the report itself still reaches the INFO line, which is where the counters are read.
    assert "'push_failed': 2" in caplog.text


async def test_a_quiet_healthy_sweep_still_says_nothing(worker_env, caplog, monkeypatch):
    """The negative case, and the reason the condition above is a list rather than `if True`.

    Widening the log had to stay a widening: §5.3 fires this job every fifteen minutes, so a
    household where nothing happened must produce no line at all, or the operator's log is 96
    sweeps a day of nothing and the ERROR above is lost in it.
    """
    from spielplan.sync import seen

    async def nothing_to_do(conn, client=None):
        return seen.SyncReport(unchanged=7, users=["patrick"], completed=["patrick"])

    monkeypatch.setattr(seen, "sync_all", nothing_to_do)
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        await next(j for j in worker.JOBS if j.name == "jellyfin-seen-sync").run()

    assert "jellyfin seen sync" not in caplog.text, caplog.text


async def test_the_same_failed_played_write_is_an_error_once_and_not_ninety_six_times_a_day(
    worker_env, caplog, monkeypatch
):
    """The condition this ERROR reports is permanent by construction, so the line has to be a
    state and not a stream.

    A sweep pushes only rows with `jf_synced_at IS NULL` and `_push` stamps only on success, so a
    refused write stays owed and is re-attempted -- and re-counted -- every fifteen minutes for the
    life of the install. The milestone's most-cited cause for it is exactly such a permanent state:
    a server below §7.1's 10.9 pin has no `/UserPlayedItems` route to take, so `docker compose logs
    worker` for one day held 96 copies of the same ERROR, interleaved with the nightly backup and
    the refit reports §6.6 promises the operator. That is the same arithmetic this milestone used
    three times over to turn a stream back into a state change (`playback._note_unreachable`,
    `seen._failed_users_logged`, `worker._last_unresolved`), and `_failed_users_logged`'s own
    comment gives the deciding argument: "the state now has a surface -- §6.6's card names the
    member -- so the log does not have to repeat it 96 times a day". `push_failed` has that
    surface too and is one level louder.

    Nothing is hidden by the down-level: the INFO summary carries `push_failed` and its reasons on
    every single sweep, which the last assertion pins. A reason that was not there before is loud
    again, which is the half a plain "log it once" would lose.
    [M4.11 review cycle 2: m411-c2-worker-03; M4.7 ops-15]
    """
    from spielplan.sync import seen

    reasons = ["POST /UserPlayedItems/jf-1 -> 404"]

    async def a_sweep_that_could_not_write(conn, client=None):
        report = seen.SyncReport(unchanged=3, push_failed=len(reasons), users=["patrick"])
        for reason in reasons:
            report._note_push_error(reason)
        return report

    monkeypatch.setattr(seen, "sync_all", a_sweep_that_could_not_write)
    monkeypatch.setattr(worker, "_push_failure_reported", None)
    job = next(j for j in worker.JOBS if j.name == "jellyfin-seen-sync")

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        for _sweep in range(4):
            await job.run()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, (
            f"one owed row on a server below the pin put {len(errors)} ERRORs in a single hour of "
            "the log the operator is asked to read; the reason never changed"
        )
        assert caplog.text.count("'push_failed': 1") == 4, (
            "the figures still reach the INFO summary every sweep -- only the ERROR is rationed"
        )

        reasons.append("POST /UserPlayedItems/jf-2 -> 500")
        await job.run()
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 2, "a refusal that was not there before is news"
        assert "-> 500" in errors[1].getMessage()


async def test_a_television_session_whose_series_could_not_be_listed_gets_its_own_sentence(
    worker_env, caplog, monkeypatch
):
    """The other reason a television session arms nothing, and it is not the one the log said.

    `observe` put two different outcomes into one list: a session that resolved to no title, and a
    session that resolved to a title whose series episode list could not be read (decision 210(c),
    "undecidable is not yes"). `report.unresolved` is the only thing that leaves the module, and
    this loop states the first meaning as fact -- "matched no title", with a comment prescribing
    the repair as "an import or a provider id, not an outage". For the second, every clause of that
    is false: the title is imported, the ProviderIds matched, and the episode id the operator is
    handed to paste into Jellyfin resolves there perfectly. The actual cause was written once, at
    DEBUG, from `sync/playback.py` -- and `worker.py` runs at INFO, so it was not emitted at all.

    Rationed the same way and by its own memo, because the producing condition is a standing proxy
    rule or a server error: without that this would be 1,440 lines a day.
    [M4.11 review cycle 2: m411-rev2-pb-02; decision 210(c), ops-15]
    """
    from spielplan.sync import playback

    async def poll_with_an_unlistable_series(conn, client=None):
        return playback.WatchReport(watching=1, undecided=["jf-6-e2"])

    monkeypatch.setattr(playback, "poll", poll_with_an_unlistable_series)
    monkeypatch.setattr(worker, "_last_unresolved", None)
    monkeypatch.setattr(worker, "_last_undecided", None)
    job = next(j for j in worker.JOBS if j.name == "jellyfin-sessions-poll")

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        detail = await job.run()
        assert detail is not None and detail["undecided"] == ["jf-6-e2"]
        named = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
        assert any("series could not be listed: jf-6-e2" in line for line in named), named
        assert not any("matched no title" in line for line in named), (
            "the session resolved to a title; that sentence sends the operator to import it"
        )

        await job.run()
        again = [r for r in caplog.records if "series could not be listed" in r.getMessage()]
        assert len(again) == 1, "a standing proxy rule is a state, not a line a minute"


# --- M4.13: the tick's budget names the two costs it actually pays -----------------------------


async def test_the_fold_in_budget_string_names_what_the_tick_measures(rated, db):
    """§5.3's budget column is the number a job is held to, and this job's said "ms".

    The ridge solve earns that word -- 6-7 ms for 100 labels -- but `serve.replace_scores` behind
    it rewrites the whole (user, kind) partition: 14,000 DELETEs and 14,000 INSERTs, 325-590 ms
    and 5-8 MB of WAL per stale pair measured at corpus scale, 0.7-1.5 s for two raters in one
    tick. A reader holding that against "ms" cannot tell a job inside its budget from one four
    orders outside it, so `FoldInReport` splits the two halves and the registry names both.

    The split is asserted as an ORDERING, not against a literal: on this fixture both numbers are
    tiny (eight titles), and what has to stay true is which half grows -- four round trips and a
    partition rewrite against two numpy calls. Measured here: see the assertion messages, which
    print what this run actually spent. [M4.13, perf-04; plan step 22]
    """
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    store, patrick = rated
    # The import itself ran a full fold-in (§10 step 1), so these pairs are fitted and the
    # verdicts the fixture records afterwards are seconds old: the tick declines them, which is
    # the debounce doing its job rather than a broken fixture. The sitting is over here.
    await _wait_out_the_pause(db, patrick)
    job = next(j for j in worker.JOBS if j.name == "fold-in-tick")
    assert job.budget != "ms", "the budget is still the solve's word for the whole job"
    for half in ("numpy", "write"):
        assert half in job.budget, f"the budget names no {half} half: {job.budget!r}"
    assert job.budget.isascii(), "a cp1252 console reads this column"

    detail = await job.run()
    assert detail is not None and detail["refit"], "nothing was refit, so nothing was measured"
    assert set(detail) >= {"ms", "numpy_ms", "db_ms"}, sorted(detail)

    # Again, unrounded, because `as_dict` rounds to a tenth of a millisecond and the fit over
    # seven labels lands under that. `only_stale=False` so the debounce is not what is being
    # measured: this is the cost of a pass that does the work, not of one that declines to.
    report = await foldin.run(
        db, bb.load_for(store), bundle_version="test-v1", only_stale=False, with_priors=False
    )
    assert report.numpy_ms > 0.0, "the fit itself was not timed at all"
    assert report.db_ms > report.numpy_ms, (
        f"Postgres is the cheaper half here: numpy {report.numpy_ms:.3f} ms against db "
        f"{report.db_ms:.3f} ms -- the budget string is then naming the wrong cost"
    )
    assert report.ms + 1.0 >= report.numpy_ms + report.db_ms, (
        f"the two halves ({report.numpy_ms:.3f} + {report.db_ms:.3f} ms) exceed the pass they "
        f"are halves of ({report.ms:.3f} ms)"
    )


# --- M4.13: the board a sitting leaves behind, and the loop that finishes it ---------------------
#
# §5.2 gives the fit two cadences, "nightly" and "incrementally on each new observation", and this
# module's own docstring calls them "the same model at two resolutions, not two models". The
# resolution the incremental row cannot reach is `v`: it re-solves the TOUCHED titles' residuals
# against the cached fit's (mu, v), so every title the person has not rated keeps whatever the last
# full fit said about it - and after M4.10 took the full fit off the tap path, that fit is the one
# over the single first verdict. Measured on the real bundle after 50 verdicts and ~41 battles per
# member: the 715 unrated owned movies had sd(s) 0.070 against 2.77-3.27 after a full refit, and
# their order correlated -0.135 with a taste the full refit recovers at +0.57. The nightly then
# moved 252 and then 724 of 765 tier badges at once, which is the snap §6.3 says the design avoids,
# delivered by the job that exists to prevent it. [M4.13, dd22; plan step 27]

# Enough generated owned movies that "the spread over the unrated library" is a measurement rather
# than an anecdote. `make_bundle`'s own parameter, drawn entirely from the authored vocabulary, so
# the contract and the tower are the ones the rest of the suite loads.
POOL_TITLES = 40

# The first tap, then six more, which is one past `REFRESH_GROWTH` rather than exactly on it: a
# fixture sitting on the threshold would pass whatever the constant meant. The authored movies are
# 1-5 and 8 (6 and 7 are the two series) and `POOL_ID_BASE` is 1001, so the last one is a pool
# title - which also makes the sitting reach past the eight titles every other test rates.
FIRST_TAP = (1, 2)
THE_REST = ((2, 2), (3, 1), (4, 0), (5, 1), (8, 2), (1001, 0))


@pytest.fixture
async def sitting(db, worker_env, two_members):
    """One member's evening, through the real request path: (patrick, hp, embeddings).

    The first tap is the one M4.10 made a *queue* rather than a fit, so the 60 s `tier-set-refit`
    sweep is what produces the only full fit this board has ever had - at n = 1. Six taps then land
    on top of it, which is what an ordinary sitting looks like from `ledger_fit`'s point of view.
    """
    from spielplan.scoring import backbone as bb

    root = fx.make_bundle(worker_env / "bundle", pool_titles=POOL_TITLES)
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), worker_env / "data" / "artifacts"
    )
    assert report.ok, report.render()
    store = ArtifactStore.open(worker_env / "data" / "artifacts" / "test-v1", "test-v1")
    patrick = two_members[0]
    hp, _notes = load_hp(store)
    emb = observations.standard_embeddings(db, bb.load_for(store), bundle_version="test-v1")

    async def tap(title_id: int, value: int):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
        return await refit.update_incrementally(
            db, user_id=patrick, kind="movie", title_ids=[title_id], hp=hp, embeddings=emb
        )

    first = await tap(*FIRST_TAP)
    assert first.fit_source == refit.QUEUED, (
        "the first tap fitted inline, so M4.10's queue is gone and this fixture is not the real path"
    )
    await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())
    for title_id, value in THE_REST:
        assert (await tap(title_id, value)).fit_source == "incremental"
    return patrick, hp, emb


def _refresh_job():
    return next(j for j in worker.JOBS if j.name == "ledger-refresh")


async def test_a_sittings_worth_of_verdicts_returns_the_board_to_a_full_fit_within_one_tick(
    sitting, db
):
    """§12's M2 criterion is about what a person sees within a sitting, and `fit_source` is the
    honest name for what they are looking at.

    The budget is what makes a tick possible at all: §5.3 gives the full fit "seconds" and it
    measures 0.11-0.14 s per (user, kind) at this scale, so asking for it once a minute while
    somebody is rating is cheaper than the `user_score` partition rewrite the fold-in tick beside it
    already pays. §5.3's nightly row is untouched - this is the same job at a cadence §5.3 permits
    because more often than nightly is a superset of nightly, which is the argument
    `scoring.foldin.run` makes for the fold-in.
    """
    patrick, _hp, _emb = sitting
    before = await db.fetchrow(
        "SELECT fit_source, n_observed FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'",
        patrick,
    )
    assert before["fit_source"] == "incremental"
    assert before["n_observed"] == 1 + len(THE_REST)

    owed = await refit.refreshes_owed(db)
    assert owed == [(patrick, "movie", len(THE_REST))], owed

    job = _refresh_job()
    assert job.every == 60 and 0 < job.timeout <= job.every, (
        "a tick that cannot finish inside its own interval eats the slot of every job behind it"
    )
    detail = await job.run()

    assert detail is not None, "the tick reported nothing about work it was owed"
    assert [r["kind"] for r in detail["refits"]] == ["movie"], detail
    done = detail["refits"][0]
    assert done["error"] is None and done["fitted"] is True, done
    assert done["n_observed"] == 1 + len(THE_REST)
    assert done["grown"] == len(THE_REST), "the tick does not report the work it was called for"

    after = await db.fetchrow(
        "SELECT fit_source, n_observed FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'",
        patrick,
    )
    assert after["fit_source"] == "nightly", (
        "the board is still resting on the residual solve, so the nightly job will do the whole "
        "move at once"
    )
    assert await refit.refreshes_owed(db) == [], (
        "the tick left the same work owed, so it refits the same board every sixty seconds"
    )
    assert await _refresh_job().run() is None, "a quiet household is not a no-op"


async def test_a_sitting_of_re_ratings_and_battles_does_not_move_the_refresh_trigger(
    sitting, db
):
    """The other half of a §6.1 sitting, and the limit of the trigger this milestone chose.

    `refreshes_owed` compares `ledger_fit.n_observed` against `cdf_reference.size`, and BOTH count
    TITLES: `_merge_cache` increments `n_observed` only on the branch that inserts a title the
    cache did not already hold. A §4.2 supersede is an additional ordinal row in the likelihood
    (`load_observations` loads superseded verdicts on purpose) and a §6.1 battle pairs two titles
    the person has already rated the same way, so neither can reach that branch -- and "battle" is
    one of `rate/session.MODES`, a whole sitting a member can choose. Both move `v` for the full
    fit and neither moves the predicate, so an evening spent changing one's mind keeps the board
    the previous full fit produced until the nightly.

    Asserted rather than left to be rediscovered, because the registered pair above only ever
    exercises the newly-rated arm (`THE_REST` is six DISTINCT ids) and this is the sentence that
    keeps the row's "a sitting's worth of verdicts" honest about which sitting. Widening the
    trigger to count OBSERVATIONS, or to fire on `max(created_at) > fitted_at` the way
    `foldin._is_stale` does, is a different trigger and the owner's call; `refreshes_owed`'s
    docstring records both options. [M4.13 cycle 2, M413-D6-03]
    """
    patrick, hp, emb = sitting
    assert await refit.refreshes_owed(db) == [(patrick, "movie", len(THE_REST))]
    await _refresh_job().run()
    assert await refit.refreshes_owed(db) == [], "the tick did not clear the work it was owed"

    rated = [FIRST_TAP[0], *(t for t, _ in THE_REST)]
    before = await db.fetchval(
        "SELECT n_observed FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", patrick
    )

    # Every one of them a title the board already holds: re-rate all seven, then fight duels
    # between them. Nothing here is a new title, and nothing here is invisible to the full fit.
    for title_id in rated:
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=1)
        await refit.update_incrementally(
            db, user_id=patrick, kind="movie", title_ids=[title_id], hp=hp, embeddings=emb
        )
    for a, b in zip(rated, rated[1:], strict=False):
        await observations.record_duel(
            db, user_id=patrick, title_a=a, title_b=b, outcome="A",
            context="profile_battle", decisive=False, hp=hp,
        )
        await refit.update_incrementally(
            db, user_id=patrick, kind="movie", title_ids=[a, b], hp=hp, embeddings=emb
        )

    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND superseded_by IS NOT NULL", patrick
    ) == len(rated), "the re-ratings did not supersede, so this measures nothing"
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", patrick) > 0

    after = await db.fetchrow(
        "SELECT fit_source, n_observed FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'",
        patrick,
    )
    assert after["n_observed"] == before, (
        "n_observed counts TITLES, so a re-rating or a duel cannot move it - if this changed, the "
        "trigger has been widened and `refreshes_owed`'s docstring is owed the correction"
    )
    assert after["fit_source"] == "incremental"
    assert await refit.refreshes_owed(db) == [], (
        "the known limit: an evening of re-ratings and battles owes no refresh until the nightly"
    )
    assert await _refresh_job().run() is None


async def test_the_board_spreads_unrated_titles_rather_than_holding_the_first_taps_estimate(
    sitting, db
):
    """The measurement, not the bookkeeping: what the person's board actually says.

    `s` for an unrated title is `mu + <v, e>` (§5.2: "no r, so s = mu + <v, e>"), so the spread over
    the unrated library is a direct reading of how much the 64-d vector has learned. Fitted over one
    verdict it has learned almost nothing, and the incremental path cannot change that however many
    taps follow - every row below was written by that one fit and not one of them moved during the
    sitting, which the `fit_source` assertion pins before anything else.

    The numbers are printed rather than asserted against a literal: the DIRECTION is the claim (a
    board that tells unrated titles apart, and an order that is not the one an n = 1 fit produced),
    and a threshold tuned on a fixture bundle is a threshold that means nothing on the real one.
    """
    patrick, _hp, _emb = sitting

    async def unrated():
        return await db.fetch(
            "SELECT title_id, s, tier, fit_source FROM ledger_state "
            " WHERE user_id = $1 AND kind = 'movie' AND NOT observed ORDER BY title_id",
            patrick,
        )

    held = await unrated()
    assert len(held) >= 20, (
        f"only {len(held)} unrated owned movies: the pool did not import, so a spread over them "
        "measures nothing"
    )
    assert {r["fit_source"] for r in held} == {"nightly"}, (
        "an unrated title's row moved during the sitting, so the premise of this test is wrong"
    )
    before = np.asarray([float(r["s"]) for r in held])
    order_before = [r["title_id"] for r in sorted(held, key=lambda r: (-float(r["s"]), r["title_id"]))]

    await _refresh_job().run()

    fresh = await unrated()
    after = np.asarray([float(r["s"]) for r in fresh])
    order_after = [r["title_id"] for r in sorted(fresh, key=lambda r: (-float(r["s"]), r["title_id"]))]
    print(
        f"unrated owned movies: {len(before)}; sd(s) {float(before.std()):.4f} on the one-verdict "
        f"fit -> {float(after.std()):.4f} after the tick; distinct tiers "
        f"{len({r['tier'] for r in held})} -> {len({r['tier'] for r in fresh})}"
    )

    assert len(after) == len(before)
    assert float(after.std()) > float(before.std()), (
        f"the refitted board is no more spread than the one-verdict one: {float(before.std()):.4f} "
        f"-> {float(after.std()):.4f}"
    )
    assert len({round(float(x), 9) for x in after}) > 1, (
        "every unrated title came out at the same score, which is a degenerate fit"
    )
    assert order_after != order_before, (
        "the board the person reads is in exactly the order the single first verdict put it in"
    )
    assert {r["fit_source"] for r in fresh} == {"nightly"}


async def test_refit_all_isolates_a_failure_that_is_not_a_value_error(
    rated, db, two_members, monkeypatch
):
    """"One person's bad fit must not stop the others'" is a claim about failures, not classes.

    The clause was `(RefitRefused, ValueError)`, and the finding's reason for widening it - a
    singular matrix escaping - is REFUTED: `np.linalg.LinAlgError` IS a `ValueError`, and probed
    against the narrow clause the report carried `error = 'Singular matrix'` with the other member
    fitted. What actually aborted the night is everything else: an asyncpg `DataError` from a row
    the schema admits and numpy does not, a lock timeout raised behind an import, a `MemoryError` in
    `_laplace`'s dense (p+n)x(p+n) inverse - which is the one injected here, because it is the only
    one of the three whose cause is the library size §5.3 budgets for.

    The refusal is injected at `_refit_user`, i.e. inside the transaction `refit_user` opens, so the
    test also says that an isolated failure cannot leak a partial write. [M4.13, plan step 29]
    """
    store, patrick = rated
    ana = two_members[1]
    for title_id, value in ((1, 2), (2, 0), (3, 1), (6, 2), (7, 0)):
        await observations.record_verdict(db, user_id=ana, title_id=title_id, value=value)
    hp, _notes = load_hp(store)

    real = refit._refit_user

    async def out_of_memory_for_patrick(conn, *, user_id, kind, **kwargs):
        if user_id == patrick:
            raise MemoryError("Unable to allocate 14.1 GiB for the dense (p+n)x(p+n) inverse")
        return await real(conn, user_id=user_id, kind=kind, **kwargs)

    monkeypatch.setattr(refit, "_refit_user", out_of_memory_for_patrick)
    reports = await refit.refit_all(db, hp)

    assert len(reports) == 4, f"the loop visited {len(reports)} of 4 (user, kind) pairs"
    failed = [r for r in reports if r.error]
    assert {r.user_id for r in failed} == {patrick} and len(failed) == 2
    assert "dense" in failed[0].error, failed[0].error
    ana_reports = [r for r in reports if r.user_id == ana]
    assert all(r.error is None for r in ana_reports), ana_reports
    assert next(r for r in ana_reports if r.kind == "movie").fitted, (
        "Ana's fit has to have actually run, not merely been reached"
    )
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1", patrick
    ) == 0, "the member whose fit raised has board rows, so a partial write escaped"


async def test_a_dead_connection_stops_the_ledger_refresh_tick_rather_than_reporting_a_skip(
    sitting, db, monkeypatch
):
    """The third copy of step 29's shape, which had the shape and no guard.

    `_ledger_refresh_tick` re-raises `PostgresConnectionError`/`InterfaceError` and isolates every
    other exception into a `RefitReport(error=...)`, exactly as `refit.refit_all` and
    `worker._tier_set_refits` do -- and those two are named by
    `test_a_dead_connection_stops_the_night_rather_than_being_reported_as_a_skip` below while this
    one was named nowhere. The only three references to this job in the suite run it on a happy
    path and on the empty queue, so folding the three copies together, or dropping the asyncpg arm
    because "log.exception covers it", passed the whole suite.

    What it costs here is smaller than in the other two loops and worth stating so the next reader
    does not over-correct: this tick reads state rather than a queue and clears nothing, so the
    work is still owed sixty seconds later. What is lost is the report -- `_tick` records a job as
    failed only when it raises, so §6.6's System card would read green for a minute of fits that
    did not happen, and `RETRY_AFTER` would never be consulted. [M4.13 cycle 2, M413-D6-05]
    """
    patrick, _hp, _emb = sitting
    assert await refit.refreshes_owed(db), "nothing is owed, so the tick returns before it can raise"

    async def the_interface_is_closed(conn, **kwargs):
        raise asyncpg.InterfaceError("cannot perform operation: another operation is in progress")

    monkeypatch.setattr(refit, "refit_user", the_interface_is_closed)
    with pytest.raises(asyncpg.InterfaceError):
        await _refresh_job().run()

    assert await refit.refreshes_owed(db) == [(patrick, "movie", len(THE_REST))], (
        "the tick reported work on a connection it could not do it with"
    )


async def test_a_dead_connection_stops_the_night_rather_than_being_reported_as_a_skip(
    rated, db, monkeypatch
):
    """The one failure the widened clause must NOT swallow, on both loops that widened.

    A connection that is gone fails every remaining (user, kind) the same way, so an isolated
    report would hand `_tick` a full account of a night nobody ran - and `_tick` records a job as
    failed only when it raises, so §6.6's System card would show green. `RETRY_AFTER` then decides
    when the next tick tries with a fresh connection from the pool. [M4.13, plan step 29]
    """
    from spielplan.rank import tiers

    store, patrick = rated
    hp, _notes = load_hp(store)

    async def the_connection_is_gone(conn, *, user_id, kind, **kwargs):
        raise asyncpg.PostgresConnectionError(
            "terminating connection due to administrator command"
        )

    monkeypatch.setattr(refit, "_refit_user", the_connection_is_gone)
    with pytest.raises(asyncpg.PostgresConnectionError):
        await refit.refit_all(db, hp)

    # The same shape in the sweep beside it, which catches `Exception` per item for finding 6.
    async def the_interface_is_closed(conn, **kwargs):
        raise asyncpg.InterfaceError("cannot perform operation: another operation is in progress")

    await tiers.save_tier_set(db, user_id=patrick, tier_set=["bad", "ok", "good"])
    assert await tiers.refits_owed(db), "nothing is owed, so the sweep returns before it can raise"
    monkeypatch.setattr(refit, "refit_user", the_interface_is_closed)
    with pytest.raises(asyncpg.InterfaceError):
        await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())
    assert await tiers.refits_owed(db), (
        "the sweep cleared a request it never serviced, on a connection it could not service it with"
    )
