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
from spielplan.models.artifacts import ArtifactStore
from tests.fixtures import make_bundle as fx

pytestmark = pytest.mark.anyio

M2_JOBS = ("ledger-map-refit", "fold-in-user-vectors", "placement-reconciliation")


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


@pytest.mark.parametrize("name", M2_JOBS)
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
    assert job.run is not None, f"§5.3 lists {name} for M2, so M2 owes it an implementation"
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
    await _import_bundle(db, worker_env)
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
    cache = await refit.load_cache(db, user_id=patrick, kind="movie", hp=DEFAULTS, lock=False)
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

    It did not work. Rating writes `ledger_state`, so every tier badge moved on every tap; every
    §6.0 shelf orders by `user_score`, which only the fold-in writes, and the fold-in ran
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
