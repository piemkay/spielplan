"""A registry entry is satisfied by any callable, so each job is run twice: bundle-less (it must
skip, §3.1) and over a real bundle (it must produce the writes the surfaces read)."""

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
# Decision 11's "queued for that user alone" is serviced by a callable nothing else calls.
M3_JOBS = ("tier-set-refit",)


@pytest.fixture
async def worker_env(db, pg_url, tmp_path, monkeypatch):
    """The jobs acquire from the pool as the loop calls them, so this opens the real pool."""
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
    """The tick refits only a pair that moved AND settled; shifting both clocks keeps their order."""
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


@pytest.mark.parametrize("name", M2_JOBS + M3_JOBS)
async def test_a_nightly_job_skips_a_household_with_no_bundle_rather_than_failing(
    name, worker_env, two_members
):
    """The Ledger job must still run: §5.2's fit works with no embeddings."""
    job = next(j for j in worker.JOBS if j.name == name)
    assert job.run is not None, f"the registry lists {name}, so its milestone owes it code"
    await job.run()


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
    """Every owned title carries a ledger state, not only the rated ones."""
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
    """A fold-in writing one half without the other leaves every score null."""
    _store, patrick = rated
    job = next(j for j in worker.JOBS if j.name == "fold-in-user-vectors")
    await job.run()

    vec = await db.fetchrow(
        "SELECT vec, blend_beta, label_count FROM user_vector WHERE user_id = $1 AND kind = 'movie'",
        patrick,
    )
    assert vec is not None and vec["label_count"] > 0
    # `blend_beta` is `real`, so 0.8 reads back as 0.800000011920929; exact comparison is 0009's bug.
    assert 0.0 <= vec["blend_beta"] <= float(np.float32(0.8)), "§5.1 caps β at the optimum"
    assert await db.fetchval("SELECT count(*) FROM title_prior") > 0


async def test_the_nightly_sweep_leaves_no_owned_title_without_a_coordinate(rated, db):
    """Run through the job, not the function it calls."""
    job = next(j for j in worker.JOBS if j.name == "placement-reconciliation")
    await job.run()
    assert await db.fetchval(
        "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
    ) == 0


async def test_the_nightly_refit_generalises_to_titles_the_person_never_rated(
    db, worker_env, two_members
):
    """Unrated titles must be told APART: a fit with e = 0 for warm titles wrote one identical `s`."""
    store = await _import_bundle(db, worker_env)
    patrick = two_members[0]
    await db.execute("UPDATE title SET is_owned = true")

    # Two warm titles rated and the rest left: the vector must carry the ordering.
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

    # Under the bundle's constants, as the job fitted; `DEFAULTS` would be a cache miss by design.
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
    """Rating writes `ledger_state` but the shelves read `user_score`, which only the fold-in writes;
    the tick must carry a sitting's verdicts to the shelves."""
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

    # A contradicting second sitting: a personal ranking must notice.
    for title_id, value in ((1, 0), (2, 0), (3, 0), (4, 2), (5, 2)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
    # The tick is debounced, so the sitting must be OVER. A never-fitted member is stale regardless.
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

    # And the read the shelves make sees it.
    section = await serve.ranked_section(
        db, user_id=patrick, kind="movie", bundle_version="test-v1"
    )
    assert [item["id"] for item in section["items"]] == [r["title_id"] for r in after]
    assert order_before != [r["title_id"] for r in after], (
        "the reversed sitting left the shelf in the same order"
    )


async def test_the_tier_set_refit_job_fits_the_person_who_asked_and_clears_the_request(rated, db):
    """The save re-initialises boundaries at once; the job does the fit. Dropping the request
    unfitted would look identical on the board."""
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
    """It runs every minute; work on an empty queue would be a MAP fit sixty times an hour."""
    from spielplan.rank import tiers

    _store, patrick = rated
    assert await tiers.refits_owed(db) == []
    before = await db.fetchval("SELECT count(*) FROM ledger_state")
    await (next(j for j in worker.JOBS if j.name == "tier-set-refit").run())
    assert await db.fetchval("SELECT count(*) FROM ledger_state") == before


# Finding 5 (the compare-and-set) and finding 6 (per-item isolation) live in different files;
# these two tests check they meet.

NINE_TIERS = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]


async def test_a_tier_set_put_that_lands_during_the_sweep_is_not_reverted_and_is_still_owed(
    rated, db, monkeypatch
):
    """The PUT is issued from `load_observations`' return, so the fit has READ K = 5 before K = 9
    lands. On `db`, a different connection: a settings save is a different request."""
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

    # Both rows read the person's last choice.
    assert await tiers.tier_set_of(db, user_id=patrick, kind="movie") == tuple(NINE_TIERS)
    assert await tiers.tier_set_of(db, user_id=patrick, kind="series") == tuple(NINE_TIERS)

    # The request the person is waiting on survived the sweep that did not serve it.
    still_owed = {(u, k) for u, k, _t in await tiers.refits_owed(db)}
    assert still_owed == {(patrick, "movie"), (patrick, "series")}, still_owed

    # Tier 7 of nine exists only if the PUT held.
    await drop_rules.drop(db, user_id=patrick, title_id=1, tier=7, title_name="Heat")
    await drop_rules.drop(db, user_id=patrick, title_id=6, tier=7, title_name="Severance")
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND tier = 7", patrick
    ) == 2


async def test_one_members_failing_refit_does_not_strand_another_members(
    rated, db, two_members, monkeypatch
):
    """Refusal injected at `refit_user`, the call this loop makes; Ana's fit is genuine."""
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

    # Cleared even where it failed: the nightly pass fits the same (user, kind) anyway.
    assert await tiers.refits_owed(db) == [], (
        "a fit that raises leaves its request owed, so the tick re-runs it a minute later"
    )
    assert await db.fetchval(
        "SELECT max(tier) FROM ledger_state WHERE user_id = $1 AND kind = 'movie'", ana
    ) <= 3, "Ana's board is still indexed against the seven-level set the fit replaced"


# The loop, not the work: a wedged job used to end the worker, and failing sweeps logged like
# quiet ones.

# A fixed offset, not a named zone: Windows has no tz database, and `due` reads only `.hour`
# and `.date()`.
NOON = datetime(2026, 9, 7, 12, 0, tzinfo=timezone(timedelta(hours=2)))

# Far longer than any budget, so "stopped" and "finished" cannot be confused.
WEDGED_SECONDS = 30


def test_every_job_this_loop_fires_declares_a_budget_that_fits_inside_its_interval():
    """`_tick` runs due jobs in sequence, so a budget over the interval eats the next job's slot.
    The backup budget must exceed `backup/nightly.DUMP_TIMEOUT_SECONDS`."""
    from spielplan.backup import nightly

    for job in worker.JOBS:
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
    """One job that never returns used to take the whole worker offline, healthcheck included.
    The tick returns, the next job runs, and `job_run` says "abandoned" with the budget."""
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
            worker.Job("t-wedged", never_returns, every=60, timeout=0.1),
            worker.Job("t-after", the_next_job, every=60, timeout=5),
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
    """`COALESCE(last_seen_ok, created_at)`: a never-delivered row ages by `created_at`; the year-old
    row pushed to yesterday must survive."""
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
    """The poll is substituted: this tests the loop's logging. `_last_unresolved` is module state,
    so it is reset."""
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
    """A paused session repeats every minute, so the line is a state change; a NEW stranger is loud."""
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
    """All writes failing used to log like a quiet household; `push_failed` is an ERROR now.
    `sync_all` is substituted: the counters are `test_seen_sync.py`'s."""
    from spielplan.sync import seen

    async def a_sweep_that_could_not_write(conn, client=None):
        report = seen.SyncReport(unchanged=3, push_failed=2, users=["patrick"])
        report._note_push_error("POST /UserPlayedItems/jf-1 -> 404")
        return report

    monkeypatch.setattr(seen, "sync_all", a_sweep_that_could_not_write)
    # Module state that outlives one test, like `_last_unresolved`.
    monkeypatch.setattr(worker, "_push_failure_reported", None)
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        detail = await next(j for j in worker.JOBS if j.name == "jellyfin-seen-sync").run()

    assert detail is not None and detail["push_failed"] == 2
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"a sweep that wrote nothing it owed logged no error: {caplog.text}"
    assert "2 Played write(s) failed" in errors[0].getMessage()
    assert "-> 404" in errors[0].getMessage()
    # The report still reaches the INFO line.
    assert "'push_failed': 2" in caplog.text


async def test_a_quiet_healthy_sweep_still_says_nothing(worker_env, caplog, monkeypatch):
    """Every fifteen minutes, so a quiet household must log nothing."""
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
    """A refused write is re-counted every sweep, so the ERROR is a state change, not 96 a day. The
    INFO summary still carries `push_failed` every sweep; a new reason is loud again."""
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
    """A series whose episodes could not be listed is not "matched no title": its own sentence,
    rationed by its own memo (decision 210(c))."""
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


async def test_the_fold_in_tick_reports_both_of_its_costs(rated, db):
    """The partition rewrite, not the ridge solve, is the cost; `FoldInReport` splits them.
    Asserted as an ORDERING: on eight titles both are tiny."""
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    store, patrick = rated
    # The import's fold-in makes the new verdicts seconds old, so the tick would decline them.
    await _wait_out_the_pause(db, patrick)
    job = next(j for j in worker.JOBS if j.name == "fold-in-tick")

    detail = await job.run()
    assert detail is not None and detail["refit"], "nothing was refit, so nothing was measured"
    assert set(detail) >= {"ms", "numpy_ms", "db_ms"}, sorted(detail)

    # Unrounded, and `only_stale=False`, so the measured pass does the work.
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


# The incremental path cannot move `v`, so after a sitting unrated titles keep the n = 1 fit's
# estimate; a refresh tick restores a full fit.

# Enough generated movies that the unrated spread is a measurement.
POOL_TITLES = 40

# One past `REFRESH_GROWTH`, not on it. Movies are 1-5 and 8; `POOL_ID_BASE` is 1001.
FIRST_TAP = (1, 2)
THE_REST = ((2, 2), (3, 1), (4, 0), (5, 1), (8, 2), (1001, 0))


@pytest.fixture
async def sitting(db, worker_env, two_members):
    """The first tap queues the only full fit (n = 1); six taps then land on it."""
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
    """A full fit measures 0.11-0.14 s here, cheaper than the fold-in's partition rewrite."""
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
    """`n_observed` counts TITLES, so re-ratings and battles never move the trigger. Asserted so
    this limit is not rediscovered; widening it is the owner's call."""
    patrick, hp, emb = sitting
    assert await refit.refreshes_owed(db) == [(patrick, "movie", len(THE_REST))]
    await _refresh_job().run()
    assert await refit.refreshes_owed(db) == [], "the tick did not clear the work it was owed"

    rated = [FIRST_TAP[0], *(t for t, _ in THE_REST)]
    before = await db.fetchval(
        "SELECT n_observed FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", patrick
    )

    # Every one a title the board already holds.
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
    """The direction is the claim, printed rather than thresholded: a fixture threshold means nothing."""
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
    """`LinAlgError` IS a `ValueError`; what aborted the night was a `MemoryError` and others.
    Injected inside `refit_user`'s transaction, so no partial write leaks."""
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
    """This tick isolates like the other two loops but had no test. It clears nothing, so work stays
    owed; what is lost is the report."""
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
    """A dead connection fails every remaining pair alike, so it must raise, not report a skip."""
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

    # The same shape in the sweep, which catches `Exception` per item.
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
