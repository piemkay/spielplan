"""The ten-stage driver: the names, the park, the resume, the mint and the shipped stages (§8, §4.1, §5.3).
The names and the spend gate need no database; the rest are claims about tables agreeing.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest

from spielplan.acquire import actions, fetch, pipeline, queue, rawstore, stages
from spielplan.connectors import registry, resolve
from spielplan.core import secrets
from spielplan.core.config import settings
from spielplan.db.pool import _init_connection as pool_init
from spielplan.derive import gate
from spielplan.dna import craft, packs, verify
from spielplan.home import shelves
from spielplan.importer import bundle as bundle_import
from spielplan.llm import extract, spend
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from tests.fixtures import make_bundle as fx

# Captured before any test patches it, so a test restoring stages does not read the patch back.
SHIPPED = pipeline.STAGES


def test_stage_six_is_the_only_paid_stage_and_every_stub_names_the_milestone_that_owes_it():
    """§8: "paid stages (6) never auto-retry past the spend cap"; no stage is still a stub."""
    assert [s.number for s in SHIPPED if s.paid] == [6]
    assert {s.number: s.owner for s in SHIPPED if not s.implemented} == {}


async def test_an_implemented_paid_stage_is_refused_while_a_declared_no_op_is_not(monkeypatch):
    """`cap_check` is replaced, so the mapping is the subject: an unset cap and an over-cap refusal both
    park with a deadline, None runs the stage, and a stage that cannot spend is waved through unasked."""
    asked: list[int | None] = []
    answers: list[spend.Refusal | None] = []

    async def cap_check(conn, *, title_id, now=None):
        asked.append(title_id)
        return answers.pop(0)

    monkeypatch.setattr(spend, "cap_check", cap_check)
    ctx = stages.StageContext(conn=None, task=None, title_id=1_000_000_001)
    shipped = next(s for s in SHIPPED if s.paid)
    assert shipped.implemented, "stage 6 has its body (decision 432)"

    answers.append(spend.Refusal(spend.NO_CAP, spend.NO_CAP_REASON, {"cap_usd": None}))
    refusal = await pipeline.refuse_uncapped_spend(shipped, ctx)
    assert refusal is not None
    assert refusal.verb == stages.PARK
    assert refusal.reason == pipeline.NO_SPEND_CAP
    assert refusal.detail == {"paid_stage": "dna extract"}
    # With a deadline: a park without one closes the task on its first refusal.
    assert refusal.until is not None, (
        "a park with no deadline is queue.skip, which closes the task on attempt one - and "
        "nothing in this tree moves a row out of skipped before decision 330 arrives at M5.6"
    )

    over = "over spend cap: $1.00 of the $1.00 monthly cap is spent"
    answers.append(spend.Refusal(spend.OVER_CAP, over, {"spent_usd": "1.00", "cap_usd": "1"}))
    refusal = await pipeline.refuse_uncapped_spend(shipped, ctx)
    assert (refusal.verb, refusal.reason) == (stages.PARK, over)
    assert refusal.until is not None, "decision 325: the month rolls over, so the park re-asks"
    assert refusal.detail == {"paid_stage": "dna extract", "spent_usd": "1.00", "cap_usd": "1"}

    answers.append(None)
    assert await pipeline.refuse_uncapped_spend(shipped, ctx) is None, "room under the cap runs it"
    assert asked == [ctx.title_id] * 3, "the gate asks the cap about the title it is gating"

    declared = pipeline.Stage(6, "dna extract", stages.dna_extract, paid=True, implemented=False)
    assert await pipeline.refuse_uncapped_spend(declared, ctx) is None
    # No shipped stage is a declared no-op any more, so the free one is built by hand.
    free = pipeline.Stage(5, "dna pack", stages.dna_pack, implemented=False)
    assert await pipeline.refuse_uncapped_spend(free, ctx) is None
    assert len(asked) == 3, "a stage that cannot spend was made to read the meter"


MOVIE = {
    "Id": "jf-acq-1", "Name": "The Duellists", "Type": "Movie", "ProductionYear": 1977,
    "RunTimeTicks": 100 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt5000001", "Tmdb": "500001"},
}
SECOND = {
    "Id": "jf-acq-2", "Name": "Sorcerer", "Type": "Movie", "ProductionYear": 1977,
    "RunTimeTicks": 121 * 60 * 10_000_000,
    "ProviderIds": {"Tmdb": "500002"},
}
THIRD = {
    "Id": "jf-acq-3", "Name": "Wages of Fear", "Type": "Movie", "ProductionYear": 1953,
    "RunTimeTicks": 131 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt5000003"},
}
# `ops/fake_jellyfin.py`'s item with no provider id: decision 323 parks it and mints nothing.
NO_IDS = {
    "Id": "jf-acq-4", "Name": "Tampopo", "Type": "Movie", "ProductionYear": 1985,
    "RunTimeTicks": 114 * 60 * 10_000_000, "ProviderIds": {},
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Through the environment, as `stages.active_store` reads
    it; `settings()` is cached, so cleared both ways."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def bundled(db, data_dir, tmp_path):
    """A real active bundle so stage 9 can place; acquired
    titles stay thin, which `_park_thin` reacts to."""
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    return report


async def _leased(db, *, item=None, title_id=None) -> queue.Task:
    """Leased, not constructed: `queue.lease` counts the attempt and sets the expiry."""
    if item is not None:
        assert await pipeline.enqueue_item(db, item) is True
    else:
        assert await pipeline.enqueue_title(db, title_id) is True
    leased = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    assert len(leased) == 1
    return leased[0]


async def _board(db, title_id):
    return await db.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", title_id)


async def _task_row(db, key: str):
    return await db.fetchrow(
        "SELECT * FROM acquisition_task WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, key
    )


# Stages 2-7 stand down so these tests are about the driver; `live`,
# `extraction_live` and `dna_live` put them back. Stage 6 stands down as a
# declared no-op, or the spend gate parks every walk. Stage 8 stays live.
STOOD_DOWN = "stage {} stood down by this file's fixture; the live stages are asserted under `live`"
STANDS_DOWN = (2, 3, 4, 5, 6, 7)


def _stands_down(stage: pipeline.Stage) -> pipeline.Stage:
    async def stood_down(_ctx):
        return stages.advance({"stood_down": STOOD_DOWN.format(stage.number)})

    return pipeline.Stage(stage.number, stage.name, stood_down, stage.paid,
                          stage.implemented and not stage.paid, stage.owner)


async def _refuse_to_crawl(_conn):
    raise AssertionError(
        "this test reached a stage that fetches without supplying a fetcher factory, and the real "
        "one would have crawled eight hosts from this machine. Take the `live` fixture and give "
        "`pipeline.drain` a `fetcher_factory` over an httpx.MockTransport"
    )


@pytest.fixture(autouse=True)
def enrichment_stands_down(monkeypatch):
    """Stages 2 to 7 advance without doing anything, unless a test asks for the real ones."""
    monkeypatch.setattr(pipeline, "_default_fetcher", _refuse_to_crawl)
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        _stands_down(stage) if stage.number in STANDS_DOWN else stage for stage in SHIPPED
    ))


def _put_back(monkeypatch, numbers: tuple[int, ...]) -> None:
    """Stage by stage rather than the whole tuple, so the `live` fixtures commute in either order."""
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        shipped if shipped.number in numbers else current
        for shipped, current in zip(SHIPPED, pipeline.STAGES, strict=True)
    ))


@pytest.fixture
def live(monkeypatch):
    """The shipped stages 2-4. A fixture rather than a `setattr`, because pytest runs autouse
    fixtures first. Stages 5-7 stay stood down: `extraction_live` and `dna_live` put them back."""
    _put_back(monkeypatch, (2, 3, 4))


@pytest.fixture
def extraction_live(monkeypatch):
    """Stage 6's shipped body back, for tests whose subject is stage 6 in the driver."""
    _put_back(monkeypatch, (6,))


@pytest.fixture
def dna_live(monkeypatch):
    """Stages 5 and 7's shipped bodies back; stage 8 is never stood down."""
    _put_back(monkeypatch, (5, 7))


async def test_an_item_with_provider_ids_is_minted_placed_and_badged(db, bundled):
    """§8 stages 1, 9 and 10 end to end. Three items, because §6.0's shelves
    suppress below a floor of three. The badge is asserted through `item_n`
    and `e_source`, which the card draws it from, not as a string."""
    for item in (MOVIE, SECOND, THIRD):
        assert await pipeline.enqueue_item(db, item) is True
    report = await pipeline.drain(db, limit=3)

    assert report.leased == 3
    assert report.ready == 3, report.as_dict()
    assert report.parked == 0 and report.failed == 0, report.as_dict()
    # Every stage ran, in order, on every task: the stubs advance rather than ending the walk.
    for task in report.tasks:
        assert task.stages_run == [s.name for s in pipeline.STAGES]

    rows = await db.fetch(
        "SELECT id, name, year, runtime_min, imdb_id, tmdb_id, jellyfin_id, is_owned, placement, "
        "       placement_bundle, origin"
        "  FROM title WHERE origin = 'acquired' ORDER BY id"
    )
    assert len(rows) == 3
    for row in rows:
        assert row["id"] >= stages.APP_ID_MIN, "a minted id outside §4.1's partition"
        assert row["origin"] == "acquired"
        assert row["is_owned"] is True, "Home's shelf filters on is_owned; §7.2 re-derives it"
        assert row["placement"] == "cold_tower"
        assert row["placement_bundle"] == "test-v1"
    first = rows[0]
    assert (first["name"], first["year"], first["runtime_min"]) == ("The Duellists", 1977, 100)
    assert (first["imdb_id"], first["tmdb_id"]) == ("tt5000001", 500001)
    assert first["jellyfin_id"] == "jf-acq-1"

    placements = await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE bundle_version = 'test-v1'"
        "   AND title_id = ANY($1::int[])",
        [r["id"] for r in rows],
    )
    assert placements == 3

    for row in rows:
        board = await _board(db, row["id"])
        assert (board["stage"], board["status"]) == (10, "ready")
        assert board["reason"] is None, "a ready job carries no park reason"
        assert board["detail"]["identify"]["identified"] == "minted"
        assert board["detail"]["place"]["placement"] == "cold_tower"
        # Stages 2-4 stood down here, and the board says so; stage 2's record is asserted in the live walk.
        assert board["detail"]["enrich"]["stood_down"] == STOOD_DOWN.format(2)
        assert board["detail"]["reviews gate"]["stood_down"] == STOOD_DOWN.format(4)

    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'admin') RETURNING id"
    )
    ctx = shelves.Ctx(
        user_id=user_id, bundle_version="test-v1", version=None, kinds=("movie",)
    )
    section, suppressed = await shelves.new_in_library(db, ctx=ctx, kind="movie")
    assert section is not None, suppressed
    assert section.title == "New in the library"
    # Compared by its two claims rather than as one literal, since the copy has been reworded.
    assert section.why.startswith("no outside ratings yet")
    assert "placed them by what they're about" in section.why
    shown = {card["title_id"]: card for card in section.items}
    for row in rows:
        card = shown[row["id"]]
        assert card["placement"] == "cold_tower"
        assert card["item_n"] is None and card["e_source"] is None, (
            "the badge's own inputs: a title with crowd support is not 'new'"
        )


async def test_an_item_with_no_provider_id_parks_at_stage_one_and_mints_nothing(db, data_dir):
    """Decision 323: no provider id parks and mints nothing, since name-and-year collides 573
    times in the corpus. No board row: its key is `title_id`, so the reason lives on the task."""
    task = await _leased(db, item=NO_IDS)
    report = await pipeline.run_task(db, task)

    assert report.status == "parked"
    assert report.stage == 1
    assert report.stages_run == ["identify"]
    assert report.reason == stages.NO_PROVIDER_ID
    assert report.reason.startswith("no provider id")

    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0

    row = await _task_row(db, "jellyfin:jf-acq-4")
    assert row["state"] == queue.SKIPPED
    assert row["result_note"] == stages.NO_PROVIDER_ID
    # Skipped and not deferred: nothing about the world changes on a timer here.
    assert row["last_error"] is None


def _park_at(monkeypatch, number: int, outcome: stages.Outcome):
    """One stage replaced by a park, the rest of the tuple intact."""
    async def parking(_ctx):
        return outcome

    patched = tuple(
        pipeline.Stage(s.number, s.name, parking, s.paid, s.implemented, s.owner)
        if s.number == number else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)


async def test_a_park_writes_its_reason_verbatim_and_the_resume_re_enters_at_that_stage(
    db, bundled, monkeypatch
):
    """§8: the reason is verbatim, a timed park spends no
    attempt, and the resume re-enters at the parked stage."""
    until = datetime.now(UTC) + timedelta(days=30)
    reason = "thin: 1 review source, 0 words of plot; the window closes in 30 days"
    stood_down = pipeline.STAGES
    _park_at(monkeypatch, 5, stages.park(reason, until=until))

    task = await _leased(db, item=MOVIE)
    first = await pipeline.run_task(db, task)
    assert first.status == "parked"
    assert first.stages_run == ["identify", "enrich", "derive", "reviews gate", "dna pack"]

    title_id = first.title_id
    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (5, "parked")
    assert board["reason"] == reason, "the board shows the stage's own sentence, unedited"
    assert board["retry_after"] == until

    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.PENDING
    assert row["next_attempt_at"] == until
    assert row["attempts"] == 0, "a deferral gives back the attempt the lease consumed"
    assert row["result_note"] == reason
    assert row["last_error"] is None, "waiting is not failing (decision 336)"

    # The window closes; the tuple put back is the fixture's,
    # since a shipped stage 6 parks at the spend gate.
    monkeypatch.setattr(pipeline, "STAGES", stood_down)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second'"
    )
    resumed_task = (await queue.lease(db, [pipeline.TASK_KIND], limit=1))[0]
    second = await pipeline.run_task(db, resumed_task)

    assert second.stages_run[0] == "dna pack", "a resume re-enters at the parked stage, not at 1"
    assert second.stages_run == [s.name for s in pipeline.STAGES if s.number >= 5]
    assert second.status == "ready"
    assert second.title_id == title_id, "the same title, not a second mint"
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1


async def test_running_the_same_task_twice_duplicates_nothing(db, bundled):
    """Re-running a COMPLETED task: the shape of a reclaim after a crash between completion and commit."""
    task = await _leased(db, item=MOVIE)
    first = await pipeline.run_task(db, task)
    assert first.status == "ready"

    before = await db.fetchrow(
        "SELECT (SELECT count(*) FROM title WHERE origin = 'acquired') AS titles,"
        "       (SELECT count(*) FROM acquisition_job) AS jobs,"
        "       (SELECT count(*) FROM title_placement) AS placements,"
        "       (SELECT count(*) FROM acquisition_task) AS tasks"
    )
    second = await pipeline.run_task(db, task)
    after = await db.fetchrow(
        "SELECT (SELECT count(*) FROM title WHERE origin = 'acquired') AS titles,"
        "       (SELECT count(*) FROM acquisition_job) AS jobs,"
        "       (SELECT count(*) FROM title_placement) AS placements,"
        "       (SELECT count(*) FROM acquisition_task) AS tasks"
    )

    assert second.status == "ready"
    assert second.title_id == first.title_id
    # The re-run task's payload predates `_remember_title`,
    # so stage 1 re-resolves and the driver jumps to 10.
    assert second.stages_run == ["identify", "ready"]
    assert dict(after) == dict(before)


async def test_a_task_whose_worker_died_resumes_at_the_stage_the_board_records(
    db, bundled, monkeypatch
):
    """A killed worker's task re-enters at the stage the BOARD records and completes exactly once.
    The lease is expired in place: `freezegun` cannot move Postgres's `now()`, which the queue reads."""
    boom = []

    async def dies(_ctx):
        boom.append("reached")
        raise KeyboardInterrupt("the box lost power at stage 7")

    patched = tuple(
        pipeline.Stage(s.number, s.name, dies, s.paid, s.implemented, s.owner)
        if s.number == 7 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    task = await _leased(db, item=MOVIE)
    with pytest.raises(KeyboardInterrupt):
        await pipeline.run_task(db, task)
    assert boom == ["reached"]

    # A `KeyboardInterrupt` is not a stage failure: it leaves a leased row and a board at stage 7.
    title_id = await db.fetchval("SELECT id FROM title WHERE origin = 'acquired'")
    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (7, "running")
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.LEASED

    await db.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 second'"
    )
    monkeypatch.setattr(pipeline, "STAGES", SHIPPED)
    report = await pipeline.drain(db)

    assert report.reclaimed == {queue.PENDING: 1, queue.FAILED: 0}
    assert report.leased == 1 and report.ready == 1
    assert report.tasks[0].stages_run == [s.name for s in pipeline.STAGES if s.number >= 7]
    assert report.tasks[0].title_id == title_id
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1
    # Scoped to this title: the bundle import placed the fixture's own titles.
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", title_id
    ) == 1


async def test_the_driver_refuses_to_run_a_paid_stage_rather_than_running_it(
    db, data_dir, monkeypatch
):
    """The driver consults the gate BEFORE calling the stage, since a billed call cannot be undone."""
    ran: list[str] = []

    async def bills(_ctx):
        ran.append("billed")
        return stages.advance()

    patched = tuple(
        pipeline.Stage(s.number, s.name, bills, True, True, s.owner) if s.number == 6 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert ran == [], "the paid stage ran; the gate is consulted after the call, not before it"
    assert report.status == "parked"
    assert report.stage == 6
    assert report.reason == pipeline.NO_SPEND_CAP

    board = await _board(db, report.title_id)
    assert (board["stage"], board["status"]) == (6, "parked")
    assert board["reason"] == pipeline.NO_SPEND_CAP
    row = await _task_row(db, "jellyfin:jf-acq-1")
    # Deferred, not failed or skipped: a failure spends attempts, and nothing moves a row out of `skipped`.
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 0, "a deferral hands the attempt back (decision 336)"
    assert row["next_attempt_at"] > datetime.now(UTC)
    assert row["last_error"] is None
    assert row["result_note"] == pipeline.NO_SPEND_CAP
    assert board["retry_after"] is not None, "the board shows the wait it is describing"


async def test_placement_parks_rather_than_raising_on_an_install_with_no_bundle(db, data_dir):
    """Park with a deadline, never raise: a raise spends the attempts on a legal bundle-less install,
    and a park without a time is `skipped`, which nothing revives. The mint still happens."""
    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert report.status == "parked"
    assert report.stage == 9
    assert report.reason == stages.NO_ACTIVE_BUNDLE
    assert "no artifact bundle is active" in report.reason

    title_id = report.title_id
    assert title_id >= stages.APP_ID_MIN
    row = await db.fetchrow("SELECT origin, placement FROM title WHERE id = $1", title_id)
    assert (row["origin"], row["placement"]) == ("acquired", "unplaced")
    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (9, "parked")
    assert board["reason"] == stages.NO_ACTIVE_BUNDLE
    assert board["retry_after"] is not None, "a wait with no end date is a wait an operator cannot read"
    assert await db.fetchval("SELECT count(*) FROM title_placement") == 0

    task_row = await _task_row(db, "jellyfin:jf-acq-1")
    assert task_row["state"] == queue.PENDING, (
        "the task is `skipped`, which no lease, reclaim or sweep can move - so importing the "
        "bundle its own reason names would revive nothing"
    )
    assert task_row["attempts"] == 0, "a deferral hands the attempt back (decision 336)"
    assert task_row["next_attempt_at"] > datetime.now(UTC)


async def test_the_bundle_less_household_comes_back_when_the_bundle_arrives(
    db, data_dir, tmp_path
):
    """The household does what the board's reason says: import a bundle, and the title reaches `ready`.
    `next_attempt_at` is moved, or the test would pass against a skipped task too."""
    task = await _leased(db, item=MOVIE)
    parked = await pipeline.run_task(db, task)
    assert (parked.status, parked.stage) == ("parked", 9)
    title_id = parked.title_id

    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()

    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute' "
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, task.key,
    )
    drained = await pipeline.drain(db)
    assert drained.leased == 1, "the parked task was never leasable again"
    assert drained.ready == 1, drained.as_dict()

    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (10, "ready")
    assert board["reason"] is None
    assert (await _task_row(db, task.key))["state"] == queue.DONE


async def test_the_nightly_sweep_cannot_drag_a_title_in_flight_back_to_stage_two(
    db, bundled, monkeypatch
):
    """`_park_thin`'s `ON CONFLICT DO NOTHING` must not move a title the driver holds (A); B, never boarded,
    is the control that the same sweep did write a row."""
    reason = "held at dna pack while the operator looks at it"
    _park_at(monkeypatch, 5, stages.park(reason))
    task = await _leased(db, item=MOVIE)
    held = await pipeline.run_task(db, task)
    assert held.status == "parked" and held.stage == 5
    monkeypatch.setattr(pipeline, "STAGES", SHIPPED)

    loose = await db.fetchval(
        "INSERT INTO title (kind, name, year, is_owned, origin)"
        " VALUES ('movie', 'Not In Flight', 2001, true, 'acquired') RETURNING id"
    )
    store = ArtifactStore.open(settings().artifacts_dir / "test-v1", "test-v1")
    report = await reconcile.reconcile(db, store, scope="owned_missing")

    assert sorted(
        await reconcile.titles_needing_placement(
            db, bundle_version="test-v1", scope="app_acquired"
        )
    ) == sorted([held.title_id, loose])
    assert report.placed == 2, report.as_dict()
    assert report.parked_thin == 1, (
        "the sweep parked neither title or both; one of them is in flight and one is not"
    )

    control = await _board(db, loose)
    assert (control["stage"], control["status"]) == (2, "parked")
    assert control["reason"].startswith("placed with "), control["reason"]
    assert "stage 2 enrichment" in control["reason"]

    board = await _board(db, held.title_id)
    assert (board["stage"], board["status"]) == (5, "parked")
    assert board["reason"] == reason, "the sweep overwrote a reason it has no business touching"


async def test_the_sweeps_parked_rows_are_an_inbox_the_driver_resumes_from(db, bundled):
    """Decision 336: `_park_thin`'s stage-2 parked rows are an inbox; the task re-enters at 2, not 1."""
    thin = await db.fetchval(
        "INSERT INTO title (kind, name, year, is_owned, origin)"
        " VALUES ('movie', 'A Thin Title', 1999, true, 'acquired') RETURNING id"
    )
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status, reason)"
        " VALUES ($1, 2, 'parked', $2)",
        thin, "placed with 3 of 10 feature blocks - missing keyword, credit; queued for §8 stage 2",
    )

    task = await _leased(db, title_id=thin)
    report = await pipeline.run_task(db, task)

    assert report.title_id == thin
    assert report.stages_run[0] == "enrich", "the inbox row is a resume point, not a fresh start"
    assert report.status == "ready"
    board = await _board(db, thin)
    assert (board["stage"], board["status"]) == (10, "ready")
    assert board["reason"] is None, "the park reason is cleared by the stage that moved past it"
    # One row still: the driver UPDATEs the inbox row. Scoped to the title, as the import parked its own.
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", thin
    ) == 1


async def test_a_stage_that_raises_is_failed_and_the_board_says_whether_it_will_retry(
    db, data_dir, monkeypatch
):
    """Decision 336: a raise is `failed`; whether a retry
    is coming is computed from the leased `attempts`."""
    async def raises(_ctx):
        raise RuntimeError("the parser hit markup it has never seen")

    patched = tuple(
        pipeline.Stage(s.number, s.name, raises, s.paid, s.implemented, s.owner)
        if s.number == 3 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert report.status == "failed"
    assert report.stage == 3
    assert report.reason == "RuntimeError: the parser hit markup it has never seen"

    board = await _board(db, report.title_id)
    assert (board["stage"], board["status"]) == (3, "failed")
    assert board["reason"] == report.reason
    assert board["detail"]["derive"] == {"attempts": 1, "retrying": True}

    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.PENDING, "one attempt of four: the queue scheduled the retry"
    assert row["last_error"] == report.reason
    assert row["next_attempt_at"] > datetime.now(UTC), "the backoff is in the future"


async def test_a_stage_that_fails_for_good_closes_its_task_and_the_board_says_no_retry_is_coming(
    db, data_dir, monkeypatch
):
    """Decision 431: a stage that knows a retry cannot help closes its task on the first attempt."""
    assert stages.fail("boom").permanent is False, "a stage that says nothing keeps the curve"
    reason = "the provider broke the contract again after the retry that named it"

    async def final(_ctx):
        return stages.fail(reason, detail={"calls": 2}, permanent=True)

    patched = tuple(
        pipeline.Stage(s.number, s.name, final, s.paid, s.implemented, s.owner)
        if s.number == 5 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert (report.status, report.stage, report.reason) == ("failed", 5, reason)
    board = await _board(db, report.title_id)
    assert (board["stage"], board["status"], board["reason"]) == (5, "failed", reason)
    assert board["detail"]["dna pack"] == {"calls": 2, "attempts": 1, "retrying": False}
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.FAILED, "closed on attempt one of four: nothing will re-run it"
    assert row["attempts"] == 1 and row["attempts"] < row["max_attempts"]
    assert row["last_error"] == reason
    assert await queue.lease(db, [pipeline.TASK_KIND], limit=1) == []


async def test_stage_one_never_mints_a_second_row_for_a_title_the_resolver_can_find(db, data_dir):
    """§7.1's resolver runs first, always: a matching provider id mints nothing and rewrites nothing."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, imdb_id, is_owned) "
        "VALUES (1, 'movie', 'Heat', 1995, 'tt0113277', true)"
    )
    item = dict(MOVIE, Id="jf-heat", ProviderIds={"Imdb": "tt0113277"})
    ctx = stages.StageContext(conn=db, task=await _leased(db, item=item))
    outcome = await stages.identify(ctx)

    assert outcome.verb == stages.ADVANCE
    assert outcome.title_id == 1
    assert outcome.detail["identified"] == "resolved to an existing title"
    assert await db.fetchval("SELECT count(*) FROM title") == 1
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0
    row = await db.fetchrow("SELECT name, jellyfin_id FROM title WHERE id = 1")
    assert (row["name"], row["jellyfin_id"]) == ("Heat", None), (
        "stage 1 wrote to a row it did not mint"
    )


async def test_the_mint_refuses_to_write_below_the_apps_own_id_range(db, data_dir):
    """§4.1: a mint below the app's range must be loud;
    defeated by a reset column default, it leaves no row."""
    await db.execute("ALTER TABLE title ALTER COLUMN id SET DEFAULT 42")
    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert report.status == "failed"
    assert report.stage == 1
    assert "below the app's id range" in report.reason
    assert await db.fetchval("SELECT count(*) FROM title") == 0, (
        "the refusal left a half-minted row behind"
    )


async def test_the_task_key_is_one_spelling_for_the_sweep_and_the_flywheel(db, data_dir):
    """Both enqueuers must spell `UNIQUE (kind, key)` one way;
    prefixed, so item and provider ids cannot collide."""
    assert pipeline.key_for_item(MOVIE) == "jellyfin:jf-acq-1"
    assert pipeline.key_for_item(dict(MOVIE, Id="")) == "imdb:tt5000001"
    assert pipeline.key_for_item({"ProviderIds": {"Tmdb": "500002"}}) == "tmdb:500002"
    with pytest.raises(ValueError, match="neither a Jellyfin id nor a provider id"):
        pipeline.key_for_item({"Name": "nothing identifies me"})

    assert await pipeline.enqueue_item(db, MOVIE) is True
    assert await pipeline.enqueue_item(db, MOVIE) is False
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 1


def test_the_stage_contract_is_a_return_value_and_not_an_exception():
    """§8's normal outcomes (a thirty-day window) are values,
    not exceptions an `except Exception` would swallow."""
    assert stages.advance().verb == stages.ADVANCE
    assert stages.advance(title_id=1_000_000_001).title_id == 1_000_000_001
    assert stages.advance().until is None and stages.advance().reason == ""

    when = datetime.now(UTC) + timedelta(days=30)
    waiting = stages.park("thin", until=when)
    assert (waiting.verb, waiting.reason, waiting.until) == (stages.PARK, "thin", when)
    assert stages.park("no provider id").until is None

    broken = stages.fail("boom")
    assert (broken.verb, broken.reason, broken.until) == (stages.FAIL, "boom", None)

    with pytest.raises(FrozenInstanceError):
        stages.advance().verb = stages.FAIL          # frozen: the board and the queue read one


# Transport modules a stage machine or parser may not import; `spielplan.connectors` is allowed, for §7.1.
TRANSPORT = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "socket",
             "aiohttp", "spielplan.acquire.fetch")


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """Relative imports resolve to absolute names, or `from .fetch import ...` reports nothing."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    prefix = ".".join(parts[: len(parts) - node.level + 1])
    return f"{prefix}.{node.module}" if node.module else prefix


def _transport_imports(source: str, *, package: str = "spielplan.acquire") -> list[str]:
    """Every transport module `source` imports; repeated from
    `test_derive_parse.py` so neither can weaken the other."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            modules.add(module)
            # `from spielplan.acquire import fetch` records
            # `spielplan.acquire.fetch` too, the likeliest spelling.
            modules.update(f"{module}.{alias.name}" for alias in node.names if module)
    return sorted(
        m for m in modules
        if any(m == bad or m.startswith(f"{bad}.") for bad in TRANSPORT)
    )


def _fetcher_uses(source: str) -> list[str]:
    """Every use of `.fetcher` other than a presence test: `ctx.fetcher.get(url)`
    needs no import at all. One hand-off is allowed, spelled exactly:
    `extract.extract_title(..., fetcher=ctx.fetcher)` (decision 432)."""
    tree = ast.parse(source)
    presence = {
        id(node.left)
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Is | ast.IsNot)
        and isinstance(node.comparators[0], ast.Constant) and node.comparators[0].value is None
    }
    handed = {
        id(keyword.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "extract.extract_title"
        for keyword in node.keywords
        if keyword.arg == "fetcher"
    }
    return sorted(
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "fetcher"
        and id(node) not in presence | handed
    )


def test_the_transport_guard_reports_every_spelling_of_the_violation():
    """Every spelling is shown failing, relative and stdlib
    ones included: `from . import fetch` is the shortest."""
    for spelling in (
        "import httpx",
        "import httpx as h",
        "from httpx import AsyncClient",
        "from spielplan.acquire import fetch",
        "from spielplan.acquire.fetch import Fetcher",
        "import spielplan.acquire.fetch",
        "import urllib.request",
        "from urllib.request import urlopen",
        "import socket",
        "from socket import create_connection",
        "import http.client",
        "import aiohttp",
        "import urllib3",
        "from . import fetch",
        "from .fetch import HostPaused",
    ):
        assert _transport_imports(f"{spelling}\n"), f"{spelling!r} walked past the guard"
    assert _transport_imports("from spielplan.acquire import queue, rawstore\n") == []
    assert _transport_imports("# from spielplan.acquire import fetch\n") == [], (
        "a comment naming the module is not an import, and every guarded file has one"
    )
    # The import-free spellings, and the one use of the handle the stage machine is allowed.
    for spelling in (
        "async def f(ctx):\n    return await ctx.fetcher.get('u')\n",
        "def f(ctx):\n    client = ctx.fetcher\n",
        "def f(ctx):\n    return helper(ctx.fetcher)\n",
        # Stage 6's hand-off passes only by callee AND keyword, so each half alone is refused.
        "async def f(ctx):\n    return await other.extract_title(ctx.conn, fetcher=ctx.fetcher)\n",
        "async def f(ctx):\n    return await extract.extract_title(ctx.conn, ctx.fetcher)\n",
        "async def f(ctx):\n    return await extract.extract_title(fetcher=ctx.fetcher.client)\n",
    ):
        assert _fetcher_uses(spelling), f"{spelling!r} walked past the guard"
    assert _fetcher_uses("def f(ctx):\n    if ctx.fetcher is None:\n        return 1\n") == []
    assert _fetcher_uses('"""`ctx.fetcher.get` is the adapters\' call."""\n') == []
    assert _fetcher_uses(
        "async def f(ctx):\n    return await extract.extract_title(ctx.conn, fetcher=ctx.fetcher)\n"
    ) == [], "stage 6 hands the drain's one Fetcher to the LLM layer by keyword (decision 373)"


def test_the_stage_machine_and_the_derive_do_not_reach_for_the_fetcher():
    """`acquire/stages.py` and everything under `derive/` import no transport: a parser that could reach
    a host would make a bad derive cost a crawl. `pipeline.py` imports `acquire.fetch` on purpose."""
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    driver = Path(stages.__file__).read_text(encoding="utf-8")
    # The guard sees something: both files really do import from the package it is scanning.
    assert "from spielplan.acquire import fetch, queue, stages" in source, (
        "decision 373 puts the drain's one Fetcher in pipeline.py; this test is re-pointed, not "
        "relaxed, so it has to fail if that import goes away"
    )
    assert "from spielplan.connectors import resolve" in driver

    # Each file's package, carried with its text, since `from
    # . import fetch` means a different module in each.
    guarded = {"acquire/stages.py": (driver, "spielplan.acquire")}
    derive_dir = Path(stages.__file__).resolve().parents[1] / "derive"
    for path in sorted(derive_dir.glob("*.py")):
        guarded[f"derive/{path.name}"] = (path.read_text(encoding="utf-8"), "spielplan.derive")
    assert len(guarded) >= 6, f"the walk found only {sorted(guarded)}; it is looking in the wrong place"

    for name, (text, package) in sorted(guarded.items()):
        # `ast`, not a substring: these files name the fetcher in prose explaining their own guard.
        offenders = _transport_imports(text, package=package)
        assert offenders == [], (
            f"{name} imports transport {offenders}: stage 2 drives its adapters "
            "through a handle it never names and stage 3 re-reads the content-addressed raw "
            "store, which is the whole of \"re-parsing is free forever\" (spec:398, decision 373)"
        )
        used = _fetcher_uses(text)
        assert used == [], (
            f"{name} uses `.fetcher` at {used}: the handle reaches every stage on the context, so "
            "a request through it needs no import - and a request made here rather than by an "
            "adapter through `sources/_views` is one no board row records (decisions 345, 373)"
        )


async def test_a_malformed_provider_id_parks_at_stage_one_and_mints_nothing(db, data_dir):
    """Decision 323 mints only on a provider id: `Tmdb: "0"`, an imdb id under `Tmdb` and
    whitespace are not ids. A skip, not a deferral: the id will be as malformed tomorrow."""
    junk = [
        ("jf-bad-1", {"Tmdb": "0"}),
        ("jf-bad-2", {"Tmdb": "tt0113277"}),
        ("jf-bad-3", {"Imdb": "   "}),
        ("jf-bad-4", {"Imdb": "0113277"}),
        ("jf-bad-5", {"Tmdb": "-5"}),
        ("jf-bad-6", {"Tvdb": "https://www.thetvdb.com/series/603"}),
    ]
    for item_id, ids in junk:
        item = {"Id": item_id, "Name": f"Mis-scraped {item_id}", "Type": "Movie",
                "ProductionYear": 1974, "ProviderIds": ids}
        report = await pipeline.run_task(db, await _leased(db, item=item))
        assert report.status == "parked", (item_id, report.as_dict())
        assert report.stage == 1
        assert report.reason.startswith("malformed provider id"), report.reason
        row = await _task_row(db, f"jellyfin:{item_id}")
        assert row["state"] == queue.SKIPPED

    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0

    # The control: a well-formed id of each kind still mints.
    good = {"Id": "jf-good-1", "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
            "ProviderIds": {"Imdb": "tt0113277", "Tmdb": "113277"}}
    minted = await pipeline.run_task(db, await _leased(db, item=good))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN
    ids = await db.fetchrow("SELECT imdb_id, tmdb_id FROM title WHERE id = $1", minted.title_id)
    assert (ids["imdb_id"], ids["tmdb_id"]) == ("tt0113277", 113277)


async def test_a_task_naming_a_title_that_is_gone_is_closed_on_the_queue_not_on_the_board(
    db, data_dir
):
    """Decision 322: the queue has no foreign key to `title` and the board's
    key is one, so a task naming a gone title is closed on the queue; the
    reason names the `(kind, key)` conflict that blocks re-enqueueing."""
    gone = 1_000_000_777
    assert await pipeline.enqueue_title(db, gone) is True
    assert await db.fetchval("SELECT count(*) FROM title WHERE id = $1", gone) == 0

    report = await pipeline.drain(db)

    assert report.leased == 1
    assert report.parked == 1, report.as_dict()
    assert report.tasks[0].reason.startswith(f"title {gone} no longer exists")
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0
    row = await _task_row(db, f"title:{gone}")
    assert row["state"] == queue.SKIPPED
    assert row["result_note"].startswith(f"title {gone} no longer exists")

    assert "will not re-enqueue a key it has already closed" in row["result_note"], (
        "the advice does not say that the key this task is on is now closed against it"
    )
    assert await pipeline.enqueue_title(db, gone) is False, (
        "re-enqueueing the closed key is a no-op, which is what the reason has to say"
    )
    assert (await _task_row(db, f"title:{gone}"))["state"] == queue.SKIPPED
    assert (await pipeline.drain(db)).leased == 0


async def test_a_title_that_vanishes_mid_walk_is_reported_and_not_raised(db, bundled, monkeypatch):
    """A title deleted mid-walk must end in `stages.ready`'s sentence, not a foreign key traceback."""
    seen: list[int] = []

    async def vanishes(ctx):
        seen.append(ctx.title_id)
        await ctx.conn.execute("DELETE FROM title WHERE id = $1", ctx.title_id)
        return stages.fail("the title went away under me")

    patched = tuple(
        pipeline.Stage(s.number, s.name, vanishes, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    report = await pipeline.drain(db)

    assert seen and seen[0] >= stages.APP_ID_MIN, "stage 1 minted and stage 2 removed it"
    assert report.failed == 1, report.as_dict()
    assert report.tasks[0].reason == "the title went away under me"
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", seen[0]
    ) == 0, "the board row went with the title; nothing tried to write it back"
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] in (queue.PENDING, queue.FAILED)
    assert row["last_error"] == "the title went away under me"


async def test_one_tasks_unserialisable_detail_does_not_cost_the_rest_of_the_batch(
    db, bundled, monkeypatch
):
    """`drain` leases the whole batch first, so one
    unserialisable `detail` must not strand the rest leased."""
    class Unserialisable:
        def __repr__(self) -> str:
            return "<a value json.dumps has never heard of>"

    async def poisons(ctx):
        if ctx.item.get("Id") == "jf-acq-2":
            return stages.advance({"window_closes": datetime.now(UTC), "n": Unserialisable()})
        return stages.advance({"stub": "fine"})

    patched = tuple(
        pipeline.Stage(s.number, s.name, poisons, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    for item in (MOVIE, SECOND, THIRD):
        assert await pipeline.enqueue_item(db, item) is True
    report = await pipeline.drain(db, limit=3)

    assert report.leased == 3
    assert report.ready == 3, report.as_dict()
    board = await db.fetchrow(
        "SELECT j.detail FROM acquisition_job j JOIN title t ON t.id = j.title_id "
        " WHERE t.jellyfin_id = 'jf-acq-2'"
    )
    detail = board["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert "window_closes" in detail["enrich"]
    assert detail["enrich"]["n"] == "<a value json.dumps has never heard of>"


async def test_a_task_that_breaks_the_driver_fails_alone(db, bundled, monkeypatch):
    """A raising gate is `failed` for its task alone, like a raising stage."""
    async def breaks(ctx):
        if ctx.item.get("Id") == "jf-acq-2":
            raise RuntimeError("boom")
        return stages.advance({"stub": "fine"})

    async def gate(stage, ctx):
        if stage.number == 3 and ctx.item.get("Id") == "jf-acq-3":
            raise RuntimeError("the spend cap could not be read")
        return None

    patched = tuple(
        pipeline.Stage(s.number, s.name, breaks, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    for item in (MOVIE, SECOND, THIRD):
        assert await pipeline.enqueue_item(db, item) is True
    report = await pipeline.drain(db, limit=3, gate=gate)

    assert report.leased == 3
    assert report.ready == 1, report.as_dict()
    assert report.failed == 2, report.as_dict()
    reasons = sorted(t.reason for t in report.tasks if t.status == "failed")
    assert "RuntimeError: boom" in reasons[0]
    assert "spend cap could not be read" in reasons[1]
    for task in report.tasks:
        row = await _task_row(db, task.key)
        assert row["state"] != queue.LEASED, f"{task.key} was left holding a lease"


async def test_a_stage_that_returns_something_other_than_an_outcome_fails_that_task(
    db, bundled, monkeypatch
):
    """A stage returning a non-`Outcome` fails its task with the stage and value named, not the tick."""
    async def returns_nothing(_ctx):
        return None

    patched = tuple(
        pipeline.Stage(s.number, s.name, returns_nothing, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)

    assert await pipeline.enqueue_item(db, MOVIE) is True
    report = await pipeline.drain(db)

    assert report.failed == 1, report.as_dict()
    assert "stage 2 (enrich) returned NoneType and not an Outcome" in report.tasks[0].reason
    board = await _board(db, report.tasks[0].title_id)
    assert board["status"] == "failed"
    assert (await _task_row(db, "jellyfin:jf-acq-1"))["state"] != queue.LEASED


async def test_a_job_whose_worker_never_came_back_stops_reading_running_on_the_board(
    db, bundled, monkeypatch
):
    """`reclaim_expired` closes the task; the board must not keep reading `running` for a dead worker."""
    async def dies(_ctx):
        # A BaseException, which `_run_stage` does not catch: a cancellation leaves a lease for the reaper.
        raise KeyboardInterrupt("the process was killed mid-stage")

    patched = tuple(
        pipeline.Stage(s.number, s.name, dies, s.paid, s.implemented, s.owner)
        if s.number == 3 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)
    killed = await _leased(db, item=MOVIE)
    with pytest.raises(KeyboardInterrupt):
        await pipeline.run_task(db, killed)

    title_id = await db.fetchval("SELECT id FROM title WHERE jellyfin_id = 'jf-acq-1'")
    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (3, "running"), "the crash marker"
    await db.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 second', "
        "       attempts = max_attempts WHERE id = $1", killed.id,
    )

    report = await pipeline.drain(db)
    assert report.reclaimed.get(queue.FAILED) == 1
    closed = await _board(db, title_id)
    assert closed["status"] == "failed", (
        "the board still says `running` for a job the reaper has closed for good"
    )
    assert closed["reason"] == queue.ABANDONED
    assert int(closed["stage"]) == 3, "the stage it stopped at is the stage it stopped at"


async def test_a_board_row_whose_other_task_is_still_in_flight_is_left_alone(db, bundled):
    """A title can carry two tasks; a board row whose other task is still pending really is in flight."""
    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)
    title_id = report.title_id

    await db.execute(
        "UPDATE acquisition_job SET status = 'running', reason = NULL WHERE title_id = $1",
        title_id,
    )
    await db.execute(
        "UPDATE acquisition_task SET state = $1, last_error = $2 WHERE id = $3",
        queue.FAILED, queue.ABANDONED, task.id,
    )
    assert await pipeline.enqueue_title(db, title_id) is True

    assert await pipeline.close_abandoned_boards(db) == 0
    assert (await _board(db, title_id))["status"] == "running"


async def test_a_title_the_cold_tower_did_not_place_waits_rather_than_being_closed(
    db, bundled, monkeypatch
):
    """`place_titles` continues past a non-finite placement, so the title is re-read; unplaced, it parks
    WITH a deadline, since a skip is never revived. The second half changes the world and drains again."""
    import numpy as np

    from spielplan.placement import tower as tower_module

    def no_finite_placement(self, x):
        n = x.shape[0]
        return np.full((n, 64), np.nan, dtype=np.float32), np.full(n, np.nan, dtype=np.float32)

    answers = tower_module.Tower.place
    monkeypatch.setattr(tower_module.Tower, "place", no_finite_placement)
    await db.execute(
        "INSERT INTO title (id, kind, name, year, imdb_id, is_owned, owned_checked_at) "
        "VALUES (900000001, 'movie', 'The Duellists', 1977, 'tt5000001', true, now())"
    )
    report = await pipeline.run_task(db, await _leased(db, item=MOVIE))

    assert (report.status, report.stage, report.title_id) == ("parked", 9, 900000001)
    assert report.reason == stages.NOT_PLACED
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0

    board = await _board(db, 900000001)
    assert (board["stage"], board["status"]) == (9, "parked")
    assert board["retry_after"] is not None, (
        "a park with no deadline is `queue.skip`, and nothing in this tree revives a skipped task"
    )
    task_row = await _task_row(db, "jellyfin:jf-acq-1")
    assert task_row["state"] == queue.PENDING, task_row["state"]
    assert task_row["attempts"] == 0, "a deferral hands the attempt back (decision 336)"

    # The world changes as the reason says: the tower answers, and the nightly reconciliation places.
    monkeypatch.setattr(tower_module.Tower, "place", answers)
    store = await stages.active_store(db)
    swept = await reconcile.reconcile(db, store, scope="owned_missing")
    assert swept.placed >= 1, swept.as_dict()
    assert await db.fetchval("SELECT placement FROM title WHERE id = 900000001") == "cold_tower"

    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, "jellyfin:jf-acq-1",
    )
    drained = await pipeline.drain(db)
    assert drained.leased == 1, "the parked task was never leasable again"
    assert drained.ready == 1, drained.as_dict()
    assert (await _board(db, 900000001))["status"] == "ready"


async def test_a_bundle_title_jellyfin_adds_is_placed_at_stage_nine(db, bundled):
    """§8 stage 9 runs the sweep's own scope for a corpus
    title Jellyfin just added, rather than waiting a night."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, imdb_id, is_owned, owned_checked_at) "
        "VALUES (900000001, 'movie', 'The Duellists', 1977, 'tt5000001', true, now())"
    )
    report = await pipeline.run_task(db, await _leased(db, item=MOVIE))

    assert report.title_id == 900000001
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0
    assert await db.fetchval("SELECT placement FROM title WHERE id = 900000001") == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = 900000001"
    ) == 1
    assert report.reason != stages.NOT_PLACED
    board = await _board(db, 900000001)
    assert board["stage"] == 10 and board["status"] in ("ready", "parked"), dict(board)


async def test_a_mint_for_an_item_jellyfin_never_showed_us_claims_no_ownership(db, bundled):
    """§7.2: a mint for an item with no `Id` claims no ownership,
    since `jellyfin_id IS NULL` escapes every un-own."""
    absent = {"Name": "Never In This House", "Type": "Movie", "ProductionYear": 2026,
              "ProviderIds": {"Imdb": "tt5009001", "Tmdb": "509001"}}
    assert pipeline.key_for_item(absent) == "imdb:tt5009001", "the flywheel's own key"
    report = await pipeline.run_task(db, await _leased(db, item=absent))

    row = await db.fetchrow(
        "SELECT id, jellyfin_id, is_owned, owned_checked_at, placement, origin"
        "  FROM title WHERE origin = 'acquired'"
    )
    assert row["jellyfin_id"] is None
    assert row["is_owned"] is False, (
        "a title Jellyfin has never shown us was minted as owned, and the one statement that can "
        "un-own a title cannot see a row with no jellyfin_id"
    )
    assert row["owned_checked_at"] is None, "a stamp beside a derivation that said no"

    assert (report.status, report.stage) == ("parked", 10)
    assert "no ownership flag" in report.reason
    board = await _board(db, row["id"])
    assert board["retry_after"] is not None, "the flag flips when a sweep resolves the item"

    # The control: the same walk for an item Jellyfin DID show us still reaches `ready` owned.
    owned = await pipeline.run_task(db, await _leased(db, item=MOVIE))
    assert owned.status == "ready", owned.as_dict()
    mine = await db.fetchrow(
        "SELECT is_owned, owned_checked_at, jellyfin_id FROM title WHERE id = $1", owned.title_id
    )
    assert (mine["is_owned"], mine["jellyfin_id"]) == (True, "jf-acq-1")
    assert mine["owned_checked_at"] is not None


async def test_a_provider_id_the_content_spines_columns_cannot_hold_parks_rather_than_failing(
    db, data_dir
):
    """An id past `integer`'s range parks naming the field,
    rather than failing four times on a `DataError`."""
    assert stages._mintable_ids({"ProviderIds": {"Tmdb": "99999999999999999999"}})["tmdb_id"] is None
    assert stages._mintable_ids({"ProviderIds": {"Tvdb": "2147483648"}})["tvdb_id"] is None
    assert stages._mintable_ids({"ProviderIds": {"Tmdb": "2147483647"}})["tmdb_id"] == 2147483647
    assert stages._year({"ProductionYear": 999999}) is None
    assert stages._year({"ProductionYear": 1977}) == 1977

    for item_id, ids, year in (
        ("jf-huge-1", {"Tmdb": "99999999999999999999"}, 1974),
        ("jf-huge-2", {"Tvdb": "2147483648"}, 1974),
        ("jf-huge-3", {"Imdb": "tt5009999"}, 999999),
    ):
        item = {"Id": item_id, "Name": f"Runaway {item_id}", "Type": "Movie",
                "ProductionYear": year, "ProviderIds": ids}
        report = await pipeline.run_task(db, await _leased(db, item=item))
        assert report.status == "parked", (item_id, report.as_dict())
        assert report.stage == 1
        assert report.reason.startswith("unusable item metadata"), report.reason
        assert "revive this task from the acquisition board" in report.reason
        assert (await _task_row(db, f"jellyfin:{item_id}"))["state"] == queue.SKIPPED

    assert await db.fetchval("SELECT count(*) FROM title") == 0
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0

    # An item the resolver never asks about the year for still mints, with the year dropped.
    nameless = {"Id": "jf-huge-4", "Name": "", "Type": "Movie", "ProductionYear": 999999,
                "ProviderIds": {"Tmdb": "509999"}}
    minted = await pipeline.run_task(db, await _leased(db, item=nameless))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN
    row = await db.fetchrow("SELECT name, year, tmdb_id FROM title WHERE id = $1", minted.title_id)
    assert (row["name"], row["year"], row["tmdb_id"]) == ("(untitled)", None, 509999)


async def test_two_workers_cannot_walk_one_title_at_the_same_time(db, bundled, pg_url):
    """Decision 322's two keys for one title lease to two workers, and the board is not a lock. The second
    connection carries the pool's codecs, which a bare `asyncpg.connect` does not."""
    first = await _leased(db, item=MOVIE)
    minted = await pipeline.run_task(db, first)
    title_id = minted.title_id
    assert minted.status == "ready"

    other = await asyncpg.connect(pg_url)
    await pool_init(other)
    try:
        # Both keys, for one title, both leasable at once.
        assert await pipeline.enqueue_title(db, title_id) is True
        await db.execute(
            "UPDATE acquisition_task SET state = $1, attempts = 0, lease_owner = NULL,"
            "       lease_expires = NULL WHERE id = $2", queue.PENDING, first.id,
        )
        by_item, by_title = await queue.lease(db, [pipeline.TASK_KIND], limit=2)
        assert {by_item.key, by_title.key} == {"jellyfin:jf-acq-1", f"title:{title_id}"}

        # Gated, not gathered: the first walk is held in stage 10
        # until the second completes, so the overlap is a fact.
        ran: list[str] = []
        inside, carry_on = asyncio.Event(), asyncio.Event()
        original = stages.ready

        async def gated(ctx):
            ran.append(ctx.task.key)
            inside.set()
            await carry_on.wait()
            return await original(ctx)

        patched = tuple(
            pipeline.Stage(s.number, s.name, gated, s.paid, s.implemented, s.owner)
            if s.number == 10 else s
            for s in pipeline.STAGES
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pipeline, "STAGES", patched)
            winner = asyncio.create_task(pipeline.run_task(db, by_item))
            await asyncio.wait_for(inside.wait(), 10)
            # Bounded: without exclusion both walks wait at the gate, which unbounded is a hung suite.
            try:
                loser = await asyncio.wait_for(pipeline.run_task(other, by_title), 10)
            except TimeoutError:
                raise AssertionError(
                    "the second walk entered the pipeline while the first still held the title"
                ) from None
            finally:
                carry_on.set()
            won = await winner
    finally:
        await other.close()

    assert ran == ["jellyfin:jf-acq-1"], f"one title, two walks of stage 10 at once: {ran}"
    assert won.status == "ready", won.as_dict()
    assert loser.status == "parked", loser.as_dict()
    assert loser.reason == pipeline.TITLE_IN_FLIGHT
    assert "another worker is already walking this title" in loser.reason

    held = await _task_row(db, loser.key)
    assert held["state"] == queue.PENDING, "the loser must come back rather than be closed"
    assert held["attempts"] == 0, "yielding is not an attempt (decision 336)"
    board = await _board(db, title_id)
    assert board["status"] == "ready", (
        "the loser stamped its own park over the board row the winner owns"
    )

    # And the exclusion is a lock and not a refusal: with the winner finished, the same task walks.
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 second' WHERE id = $1",
        held["id"],
    )
    again = await pipeline.drain(db)
    assert again.leased == 1 and again.ready == 1, again.as_dict()


async def test_a_task_that_breaks_the_driver_closes_its_board_row_too(db, bundled, monkeypatch):
    """`drain`'s catch-all is a fourth exit and closes the board
    too; the break is in `write_board`, past `_run_stage`."""
    real = pipeline.write_board

    async def breaks(conn, title_id, **kwargs):
        if kwargs.get("stage") == 5 and kwargs.get("status") == pipeline.RUNNING:
            raise RuntimeError("connection reset by peer")
        return await real(conn, title_id, **kwargs)

    monkeypatch.setattr(pipeline, "write_board", breaks)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    report = await pipeline.drain(db)

    assert report.failed == 1, report.as_dict()
    assert "the driver failed outside any stage" in report.tasks[0].reason
    title_id = await db.fetchval("SELECT id FROM title WHERE jellyfin_id = 'jf-acq-1'")
    board = await _board(db, title_id)
    assert board["status"] == "failed", (
        "the board still reads `running` for a task the drain has already closed"
    )
    assert board["reason"] == report.tasks[0].reason
    assert int(board["stage"]) == 4, "the stage the last advance named is the stage it stopped in"

    monkeypatch.undo()
    assert await pipeline.close_abandoned_boards(db) == 0, (
        "the reaper matches queue.ABANDONED exactly and cannot be the repair for this exit"
    )


async def test_a_task_abandoned_after_the_board_reached_ready_is_completed_not_failed(db, bundled):
    """Board `ready` and `queue.complete` are two statements;
    a task abandoned between them is completed, not failed."""
    task = await _leased(db, item=MOVIE)
    done = await pipeline.run_task(db, task)
    assert done.status == "ready"

    # The state the crash leaves: the board written, the completion lost, the attempts spent.
    await db.execute(
        "UPDATE acquisition_task SET state = $1, lease_owner = 'worker-gone',"
        "       lease_expires = now() - interval '1 second', attempts = max_attempts,"
        "       result_note = NULL WHERE id = $2", queue.LEASED, task.id,
    )
    report = await pipeline.drain(db)
    assert report.reclaimed.get(queue.FAILED) == 1, report.as_dict()

    row = await _task_row(db, task.key)
    assert row["state"] == queue.DONE, (
        "the queue says a worker abandoned this title while the board says it is ready, and "
        "nothing in the tree can revive it"
    )
    assert row["last_error"] is None
    assert "only the report of it was lost" in row["result_note"]
    assert (await _board(db, done.title_id))["status"] == "ready"

    # And the narrowness: a board that is NOT ready is left exactly as the reaper wrote it.
    second = await _leased(db, item=SECOND)
    parked = await pipeline.run_task(db, second)
    assert parked.status == "ready"
    await db.execute("UPDATE acquisition_job SET status = 'running' WHERE title_id = $1",
                     parked.title_id)
    await db.execute(
        "UPDATE acquisition_task SET state = $1, last_error = $2 WHERE id = $3",
        queue.FAILED, queue.ABANDONED, second.id,
    )
    assert await pipeline.complete_landed_boards(db) == 0
    assert (await _task_row(db, second.key))["state"] == queue.FAILED


async def test_two_workers_cannot_mint_one_film_twice(db, bundled, pg_url):
    """Two library items of one film, before either walk holds a title id, and `title` has no
    UNIQUE. Gated once inside the mint, so an unfixed build duplicates rather than deadlocks."""
    four_k = {
        "Id": "jf-copy-4k", "Name": "Barry Lyndon", "Type": "Movie", "ProductionYear": 1975,
        "RunTimeTicks": 185 * 60 * 10_000_000,
        "ProviderIds": {"Imdb": "tt5000901", "Tmdb": "500901"},
    }
    hd = {**four_k, "Id": "jf-copy-hd"}

    assert await pipeline.enqueue_item(db, four_k) is True
    assert await pipeline.enqueue_item(db, hd) is True
    leased = await queue.lease(db, [pipeline.TASK_KIND], limit=2)
    assert {t.key for t in leased} == {"jellyfin:jf-copy-4k", "jellyfin:jf-copy-hd"}
    first, second = leased

    other = await asyncpg.connect(pg_url)
    await pool_init(other)
    try:
        inside, carry_on = asyncio.Event(), asyncio.Event()
        original = stages._mint

        async def gated(conn, item, **kwargs):
            if not inside.is_set():
                inside.set()
                await carry_on.wait()
            return await original(conn, item, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(stages, "_mint", gated)
            winner = asyncio.create_task(pipeline.run_task(db, first))
            await asyncio.wait_for(inside.wait(), 10)
            try:
                loser = await asyncio.wait_for(pipeline.run_task(other, second), 10)
            except TimeoutError:
                raise AssertionError(
                    "the second walk blocked on the first rather than being handed back; "
                    "stage 1 must try for the claim and yield, never wait for it"
                ) from None
            finally:
                carry_on.set()
            won = await winner
    finally:
        await other.close()

    acquired = await db.fetch(
        "SELECT id, jellyfin_id, imdb_id FROM title WHERE origin = 'acquired' ORDER BY id"
    )
    assert len(acquired) == 1, (
        f"one film, {len(acquired)} rows in a spine decision 162 cannot rewrite: "
        f"{[dict(row) for row in acquired]}"
    )
    assert won.status == "ready", won.as_dict()
    assert loser.status == "parked", loser.as_dict()
    assert loser.reason == stages.FILM_IN_FLIGHT
    assert loser.title_id is None, "the loser minted nothing, so it names no title"

    held = await _task_row(db, loser.key)
    assert held["state"] == queue.PENDING, "the loser must come back rather than be closed"
    assert held["attempts"] == 0, "yielding is not an attempt (decision 336)"

    # A lock, not a refusal: the loser's second walk resolves onto the winner's row and mints nothing.
    again = await pipeline.drain(db)
    assert again.leased == 1 and again.ready == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1
    assert (await _task_row(db, loser.key))["state"] == queue.DONE


async def test_stage_one_refuses_an_identity_it_could_not_look_up_again(db, data_dir):
    """The mint must write the value the resolver looks up
    with; an unstripped id is refused, not normalised."""
    for expected, item in (
        ("malformed provider id", {
            "Id": "jf-pad-1", "Name": "Padded", "Type": "Movie",
            "ProviderIds": {"Imdb": " tt5000123 "},
        }),
        ("malformed provider id", {
            "Id": "jf-pad-2", "Name": "Solaris", "Type": "Movie",
            "ProviderIds": {"Imdb": "tt5000124\n"},
        }),
        ("unusable Jellyfin item id", {
            "Id": "  jf-pad-3  ", "Name": "Stalker", "Type": "Movie",
            "ProviderIds": {"Imdb": "tt5000125"},
        }),
    ):
        task = await _leased(db, item=item)
        report = await pipeline.run_task(db, task)
        assert report.status == "parked", f"{item['Id']}: {report.as_dict()}"
        assert report.reason.startswith(expected), report.reason
        assert report.title_id is None
        assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0, (
            f"{item['Id']!r} minted a title nothing can resolve onto again"
        )
        assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0

    # The control: a padded `tmdb` id mints, since both sides
    # reduce it; no year, so the first branch is asserted.
    clean = {
        "Id": "jf-pad-9", "Name": "Andrei Rublev", "Type": "Movie",
        "ProviderIds": {"Imdb": "tt5000129", "Tmdb": " 500129 "},
    }
    minted = await pipeline.run_task(db, await _leased(db, item=clean))
    assert minted.title_id is not None, minted.as_dict()
    assert await resolve.resolve_title_id(db, clean) == minted.title_id, (
        "the mint wrote an identity the resolver cannot look up, so a reclaim mints a second row"
    )
    row = await db.fetchrow("SELECT jellyfin_id, imdb_id, tmdb_id FROM title WHERE id = $1",
                            minted.title_id)
    assert (row["jellyfin_id"], row["imdb_id"], row["tmdb_id"]) == ("jf-pad-9", "tt5000129", 500129)


async def test_a_parked_board_promising_a_retry_is_closed_when_nothing_will_lease_it(db, bundled):
    """A park with a deadline beside a terminal task is a crash marker: closed, reason kept,
    `retry_after` cleared. A park with no deadline is `_park_thin`'s inbox and is left alone."""
    task = await _leased(db, item=MOVIE)
    walked = await pipeline.run_task(db, task)
    title_id = walked.title_id

    await db.execute(
        "UPDATE acquisition_job SET stage = 9, status = 'parked', reason = $2,"
        "       retry_after = now() + interval '1 day' WHERE title_id = $1",
        title_id, stages.NO_ACTIVE_BUNDLE,
    )
    await db.execute(
        "UPDATE acquisition_task SET state = $1, last_error = $2 WHERE id = $3",
        queue.FAILED, queue.ABANDONED, task.id,
    )

    assert await pipeline.close_abandoned_boards(db) == 1
    closed = await _board(db, title_id)
    assert closed["status"] == "failed", (
        "the board promises a retry for a task the reaper has closed for good"
    )
    assert queue.ABANDONED in closed["reason"]
    assert "no artifact bundle is active" in closed["reason"], (
        "the park it stopped in is why the operator is being asked to do anything"
    )
    assert closed["retry_after"] is None, "a date nothing will honour is a date not to show"
    assert int(closed["stage"]) == 9

    # The inbox is not a crash marker: `_park_thin` writes stage-2 parks with no deadline, and they stay.
    inbox = await db.fetchrow(
        "SELECT title_id, reason FROM acquisition_job"
        " WHERE status = 'parked' AND retry_after IS NULL AND title_id <> $1 LIMIT 1",
        title_id,
    )
    assert inbox is not None, "the fixture bundle parked no thin titles, so this arm proves nothing"
    assert await pipeline.enqueue_title(db, inbox["title_id"]) is True
    await db.execute(
        "UPDATE acquisition_task SET state = $1, last_error = $2 WHERE kind = $3 AND key = $4",
        queue.FAILED, queue.ABANDONED, pipeline.TASK_KIND, f"title:{inbox['title_id']}",
    )
    assert await pipeline.close_abandoned_boards(db) == 0
    still = await _board(db, inbox["title_id"])
    assert (still["status"], still["reason"]) == ("parked", inbox["reason"])


async def test_two_workers_cannot_mint_one_film_twice_on_disjoint_provider_ids(db, bundled, pg_url):
    """Items with DISJOINT provider ids for one film must still exclude each other, or the name-and-year arm
    resolves one row sequentially and two concurrently. Gated inside the mint, as the sibling test is."""
    imdb_only = {
        "Id": "jf-disjoint-hd", "Name": "The Long Corridor", "Type": "Movie",
        "ProductionYear": 1988, "RunTimeTicks": 170 * 60 * 10_000_000,
        "ProviderIds": {"Imdb": "tt5000902"},
    }
    tmdb_only = {**imdb_only, "Id": "jf-disjoint-4k", "ProviderIds": {"Tmdb": "500902"}}
    assert await pipeline.enqueue_item(db, imdb_only) is True
    assert await pipeline.enqueue_item(db, tmdb_only) is True
    first, second = await queue.lease(db, [pipeline.TASK_KIND], limit=2)

    other = await asyncpg.connect(pg_url)
    await pool_init(other)
    try:
        inside, carry_on = asyncio.Event(), asyncio.Event()
        original = stages._mint

        async def gated(conn, item, **kwargs):
            if not inside.is_set():
                inside.set()
                await carry_on.wait()
            return await original(conn, item, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(stages, "_mint", gated)
            winner = asyncio.create_task(pipeline.run_task(db, first))
            await asyncio.wait_for(inside.wait(), 10)
            try:
                loser = await asyncio.wait_for(pipeline.run_task(other, second), 10)
            except TimeoutError:
                raise AssertionError(
                    "the second walk blocked on the first rather than being handed back; "
                    "stage 1 must try for the claim and yield, never wait for it"
                ) from None
            finally:
                carry_on.set()
            won = await winner
    finally:
        await other.close()

    acquired = await db.fetch(
        "SELECT id, jellyfin_id, imdb_id, tmdb_id FROM title WHERE origin = 'acquired' ORDER BY id"
    )
    assert len(acquired) == 1, (
        f"one film, {len(acquired)} rows in a spine decision 162 cannot rewrite: "
        f"{[dict(row) for row in acquired]}"
    )
    shared = set(stages._mint_claims(imdb_only)) & set(stages._mint_claims(tmdb_only))
    assert shared == {"movie:name:the long corridor:1988"}, (
        f"the two items must agree on the name-and-year claim and nothing else: {shared}"
    )
    assert won.status == "ready", won.as_dict()
    assert loser.status == "parked", loser.as_dict()
    assert loser.reason == stages.FILM_IN_FLIGHT
    assert loser.title_id is None, "the loser minted nothing, so it names no title"
    assert (await _task_row(db, loser.key))["attempts"] == 0, "yielding is not an attempt"

    # A lock, not a refusal: the loser resolves onto the winner's row through the arm the claim covers. That
    # holds only while the name arm is unambiguous; the colliding case has its own test.
    again = await pipeline.drain(db)
    assert again.leased == 1 and again.ready == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1


async def test_a_provider_id_python_cannot_parse_parks_rather_than_raising(db, data_dir):
    """`str.isdigit()` accepts superscripts that `int()`
    refuses; the mint and the lookup side both park them."""
    assert stages._mintable_ids({"ProviderIds": {"Tmdb": "12345\u00b2"}})["tmdb_id"] is None
    assert stages._mintable_ids({"ProviderIds": {"Tvdb": "\u00b2"}})["tvdb_id"] is None
    assert stages._mintable_ids({"ProviderIds": {"Tmdb": "\u0669\u0664\u0669"}})["tmdb_id"] == 949
    assert stages._year({"ProductionYear": "1995\u00b2"}) == 1995
    assert stages._mint_claims(
        {"Type": "Movie", "Name": "Heat", "ProductionYear": "1995\u00b2",
         "ProviderIds": {"Tmdb": "949"}}
    ) == ["movie:tmdb_id:949", "movie:name:heat:1995"]

    for item_id, ids in (("jf-sup-1", {"Tmdb": "12345\u00b2"}), ("jf-sup-2", {"Tvdb": "\u00b2"})):
        item = {"Id": item_id, "Name": f"Footnoted {item_id}", "Type": "Movie",
                "ProductionYear": 1974, "ProviderIds": ids}
        report = await pipeline.run_task(db, await _leased(db, item=item))
        assert report.status == "parked", (item_id, report.as_dict())
        assert report.stage == 1
        assert "revive this task from the acquisition board" in report.reason, report.reason
        assert (await _task_row(db, f"jellyfin:{item_id}"))["state"] == queue.SKIPPED

    assert await db.fetchval("SELECT count(*) FROM title") == 0
    assert await db.fetchval("SELECT count(*) FROM acquisition_job") == 0


async def test_a_gate_that_answers_with_anything_but_none_or_an_outcome_costs_only_its_task(
    db, bundled
):
    """A gate answering anything but None or an `Outcome` fails
    its task; `advance()` would silently skip the stage."""
    answers = ("over spend cap", {}, {"cap_cents": 500}, stages.advance({"gate": "fine"}))
    for n, answer in enumerate(answers):
        async def gate(stage, _ctx, answer=answer):
            return answer if stage.number == 1 else None

        task = await _leased(db, item={**MOVIE, "Id": f"jf-gate-{n}"})
        report = await pipeline.run_task(db, task, gate=gate)
        assert report.status == "failed", (answer, report.as_dict())
        assert report.stage == 1, report.as_dict()
        assert "the gate on stage 1 (identify)" in report.reason, report.reason
        assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0, (
            "a refused gate must not have run the stage it was gating"
        )
        assert (await _task_row(db, task.key))["last_error"] == report.reason

    # The control: a gate that answers the way decision 348 says runs every stage.
    ran: list[str] = []

    async def counting(stage, _ctx):
        ran.append(stage.name)
        return None

    ok = await pipeline.run_task(
        db, await _leased(db, item={**MOVIE, "Id": "jf-gate-ok"}), gate=counting
    )
    assert ok.status == "ready", ok.as_dict()
    assert len(ran) == len(pipeline.STAGES), ran


async def test_a_title_deleted_while_a_stage_advances_parks_rather_than_raising(
    db, bundled, monkeypatch
):
    """A title deleted while a stage ADVANCES parks under
    `TITLE_GONE` rather than raising from a board write."""
    seen: list[int] = []

    async def deletes_and_advances(ctx):
        seen.append(ctx.title_id)
        await ctx.conn.execute("DELETE FROM title WHERE id = $1", ctx.title_id)
        return stages.advance({"removed": ctx.title_id})

    patched = tuple(
        pipeline.Stage(s.number, s.name, deletes_and_advances, s.paid, s.implemented, s.owner)
        if s.number == 2 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    report = await pipeline.drain(db)

    assert seen and seen[0] >= stages.APP_ID_MIN, "stage 1 minted and stage 2 removed it"
    assert report.parked == 1, report.as_dict()
    assert report.tasks[0].reason.startswith(f"title {seen[0]} no longer exists"), (
        report.tasks[0].reason
    )
    assert "the driver failed outside any stage" not in report.tasks[0].reason
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.SKIPPED, "no retry can put the title back"
    assert row["attempts"] == 1, "the claim counted one attempt and the park spent no more"


async def test_a_name_and_year_the_spine_cannot_tell_apart_parks_rather_than_minting(db):
    """The name-and-year arm answers None for "none" and for "several", so at two colliders
    the walk parks rather than minting a third row; at one collider it resolves."""
    async def seed(name: str, year: int, title_id: int) -> None:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, origin) VALUES ($1, 'movie', $2, $3, "
            "'bundle')", title_id, name, year,
        )

    item = {
        "Id": "jf-ambig-1", "Name": "The Long Corridor", "Type": "Movie",
        "ProductionYear": 1988, "ProviderIds": {"Imdb": "tt7000001"},
    }

    # One collider: the resolver answers, so this walk resolves rather than minting.
    await seed("The Long Corridor", 1988, 7001)
    report = await pipeline.run_task(db, await _leased(db, item=item))
    assert report.title_id == 7001, report.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0

    # Two colliders: it cannot, and the walk that would have minted a third row parks instead.
    await seed("The Long Corridor", 1988, 7002)
    second = {**item, "Id": "jf-ambig-2"}
    parked = await pipeline.run_task(db, await _leased(db, item=second))
    assert parked.status == "parked", parked.as_dict()
    assert parked.stage == 1
    assert parked.title_id is None, "a park at stage 1 names no title, because none was written"
    assert "2 titles from 1988" in parked.reason, parked.reason
    assert "revive this task from the acquisition board" in parked.reason, parked.reason
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0, (
        "a film the app cannot tell from two it already holds was written into the spine anyway"
    )
    assert await db.fetchval("SELECT count(*) FROM acquisition_job WHERE title_id >= $1",
                             stages.APP_ID_MIN) == 0, (
        "a park at stage 1 writes no board row, because the reason lives on the task and there is "
        "no title for section 6.6 to show a row about"
    )
    assert (await _task_row(db, "jellyfin:jf-ambig-2"))["state"] == queue.SKIPPED

    # The second walk of the same film mints nothing either: the claim serialises the pair.
    third = {**item, "Id": "jf-ambig-3", "ProviderIds": {"Tmdb": "700001"}}
    again = await pipeline.run_task(db, await _leased(db, item=third))
    assert again.status == "parked" and again.stage == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0

    # The control at the other end: no collision at all is an ordinary acquisition.
    clean = {**item, "Id": "jf-ambig-4", "Name": "A Corridor Nobody Else Named",
             "ProviderIds": {"Imdb": "tt7000004"}}
    minted = await pipeline.run_task(db, await _leased(db, item=clean))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN, minted.as_dict()


async def test_an_original_title_the_resolver_never_probes_does_not_mint_a_second_row(db):
    """The resolver never probes the item's `OriginalTitle`, which
    `_mint` writes, so both copy orders must give one title."""
    english = {
        "Id": "jf-orig-en", "Name": "The Long Corridor", "OriginalTitle": "Der lange Gang",
        "Type": "Movie", "ProductionYear": 1988, "ProviderIds": {"Imdb": "tt6000100"},
    }
    german = {
        "Id": "jf-orig-de", "Name": "Der lange Gang", "Type": "Movie",
        "ProductionYear": 1988, "ProviderIds": {"Tmdb": "600100"},
    }
    assert set(stages._mint_claims(english)) & set(stages._mint_claims(german)) == set(), (
        "the two items must share no claim, or the lock would be doing the work this tests"
    )

    # The ordering that already worked: the German copy's Name matches the English row's `original_name`.
    first = await pipeline.run_task(db, await _leased(db, item=english))
    assert first.title_id is not None and first.title_id >= stages.APP_ID_MIN
    second = await pipeline.run_task(db, await _leased(db, item=german))
    assert second.title_id == first.title_id, "the German copy is the same film"
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1

    # The other ordering, on a clean spine: the English `Name`
    # matches neither `name` nor a NULL `original_name`.
    await db.execute("DELETE FROM title WHERE origin = 'acquired'")
    await db.execute("DELETE FROM acquisition_task")
    de_first = await pipeline.run_task(
        db, await _leased(db, item={**german, "Id": "jf-orig-de2"})
    )
    assert de_first.title_id is not None
    en_second = await pipeline.run_task(
        db, await _leased(db, item={**english, "Id": "jf-orig-en2"})
    )
    assert en_second.status == "parked", en_second.as_dict()
    assert en_second.stage == 1
    assert "1 title from 1988" in en_second.reason, en_second.reason
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1, (
        "one film, two rows above 1e9, both placed and both badged - and decision 162 keeps them"
    )


async def test_a_runaway_runtime_drops_the_value_rather_than_losing_the_title(db, bundled):
    """A runaway or non-numeric runtime is dropped, not parked:
    the column is nullable and a runtime is no identity."""
    assert stages._runtime_min({"RunTimeTicks": 120 * 60 * 10_000_000}) == 120
    assert stages._runtime_min({"RunTimeTicks": 9 * 10**18}) is None, "int32 is the column"
    assert stages._runtime_min({"RunTimeTicks": "not a number"}) is None
    assert stages._runtime_min({"RunTimeTicks": -5 * 60 * 10_000_000}) is None, (
        "a negative runtime is truthy and fits in int32, so a bound alone would store it"
    )
    assert stages._runtime_min({"RunTimeTicks": 0}) is None
    assert stages._runtime_min({}) is None
    assert stages._runtime_min({"RunTimeTicks": float("inf")}) is None, (
        "int() raises OverflowError and not ValueError for an infinity, and Python's json decoder "
        "accepts the Infinity literal - which is the decoder M5.2's webhook body goes through"
    )
    assert stages._runtime_min({"RunTimeTicks": str(90 * 60 * 10_000_000)}) == 90, (
        "a string of digits is what a webhook body carries, and it is a perfectly good tick count"
    )

    item = {
        "Id": "jf-runtime-1", "Name": "A Film ffprobe Could Not Measure", "Type": "Movie",
        "ProductionYear": 1979, "RunTimeTicks": 9 * 10**18,
        "ProviderIds": {"Imdb": "tt5000801"},
    }
    report = await pipeline.run_task(db, await _leased(db, item=item))
    assert report.status == "ready", report.as_dict()
    assert report.title_id is not None
    assert await db.fetchval(
        "SELECT runtime_min FROM title WHERE id = $1", report.title_id
    ) is None, "the value is dropped; the title is not"


async def test_a_gate_that_parks_with_no_deadline_is_refused_rather_than_closing_the_task(db):
    """A gate parking with no deadline is refused: a park without
    `until` is `queue.skip`, closing the task for good."""
    async def no_deadline(stage, _ctx):
        return stages.park("over spend cap") if stage.number == 6 else None

    task = await _leased(db, item={**MOVIE, "Id": "jf-nocap-1"})
    report = await pipeline.run_task(db, task, gate=no_deadline)
    assert report.status == "failed", report.as_dict()
    assert report.stage == 6, report.as_dict()
    assert "the gate on stage 6 (dna extract)" in report.reason, report.reason
    assert "no deadline" in report.reason, report.reason
    row = await _task_row(db, task.key)
    assert row["state"] != queue.SKIPPED, (
        "one refusal from a gate closed the task for good, and nothing in this tree reopens it"
    )
    assert row["state"] == queue.PENDING, "a failure that has attempts left comes back"

    # The control: the same refusal with a deadline is a `defer`, no attempt spent.
    async def dated(stage, _ctx):
        return (
            stages.park("over spend cap", until=stages.waiting_on_the_world())
            if stage.number == 6 else None
        )

    second = await _leased(db, item={**MOVIE, "Id": "jf-nocap-2", "Name": "The Duellists II",
                                     "ProviderIds": {"Imdb": "tt5000777"}})
    parked = await pipeline.run_task(db, second, gate=dated)
    assert parked.status == "parked", parked.as_dict()
    assert parked.stage == 6 and parked.reason == "over spend cap"
    held = await _task_row(db, second.key)
    assert held["state"] == queue.PENDING and held["attempts"] == 0, (
        "a park with a deadline is a re-ask and never a retry budget (decision 336)"
    )
    assert held["next_attempt_at"] > datetime.now(UTC), "and it comes back by itself"


# The shipped stages 2-4 through `live`, against a canned web that 404s any unrouted host, so every route
# is a source the test makes a claim about.

TMDB_HOST = "api.themoviedb.org"
TRAKT_HOST = "api.trakt.tv"
MC_HOST = "www.metacritic.com"


class _Clock:
    """Replaces `time.monotonic` and `asyncio.sleep`, so the scraped
    hosts keep their declared rate without taking seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if len(self.slept) > 200:
            raise AssertionError(f"paced 200 times without progress; last wait was {seconds!r}")
        self.now += seconds


class _CannedWeb:
    """Requests are logged for the measures about requests NOT made; robots.txt is served permissively."""

    ROBOTS = b"User-agent: *\nAllow: /\n"

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=self.ROBOTS,
                                  headers={"content-type": "text/plain"})
        route = self.routes.get((request.url.host, request.url.path))
        if callable(route):
            route = route(request)
        if route is None:
            return httpx.Response(404, content=b"not found",
                                  headers={"content-type": "text/html"})
        status, body, content_type = route
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    @property
    def fetched(self) -> list[httpx.Request]:
        """Every request that was not a robots.txt read. Robots is politeness, not enrichment."""
        return [r for r in self.seen if r.url.path != "/robots.txt"]

    def hosts(self) -> set:
        return {r.url.host for r in self.fetched}


def _json_route(payload, *, status: int = 200):
    return status, json.dumps(payload).encode("utf-8"), "application/json"


def _factory(site: _CannedWeb, clock: _Clock):
    """A `FetcherFactory` over a canned web, in `StageGate`'s shape: the seam a test fills."""
    async def make(conn):
        return fetch.Fetcher(conn=conn, transport=httpx.MockTransport(site.handler),
                             clock=clock, sleep=clock.sleep, jitter=lambda lo, _hi: lo)
    return make


# Sized clear of decision 335's floor rather than at it; the boundary is asserted in `test_reviews_gate.py`.
PLOT = (
    "Two men who are very good at their work circle one another across a city that neither of "
    "them can leave, and the film gives each of them exactly as much sympathy as the other."
)
TMDB_REVIEW = (
    "A heist picture that is really about labour: every character is introduced by what they are "
    "competent at, and the film's sympathy runs to whoever is doing the work in the frame. The "
    "coffee shop scene earns its reputation because it is the only time either man is idle."
)
TRAKT_COMMENT = (
    "The shootout is staged for legibility rather than for spectacle, which is why it still works "
    "thirty years on. You always know where everyone is standing, and that is the whole trick."
)


def _tmdb_detail(tmdb_id: int, imdb_id: str, *, title: str = "The Duellists") -> dict:
    """`reviews` is appended to the detail document, as
    `tmdb.MOVIE_APPEND` asks: one request, the required one."""
    return {
        "id": tmdb_id, "title": title, "original_title": title, "overview": PLOT,
        "release_date": "1977-01-01", "runtime": 100, "original_language": "en",
        "external_ids": {"imdb_id": imdb_id},
        "credits": {
            "cast": [{"id": 1, "name": "Keith Carradine", "character": "Armand", "order": 0}],
            "crew": [{"id": 2, "name": "Ridley Scott", "job": "Director",
                      "department": "Directing"}],
        },
        "keywords": {"keywords": [{"id": 9, "name": "duel"}]},
        "reviews": {"total_results": 1, "results": [
            {"id": f"r-{tmdb_id}", "author": "a reviewer", "content": TMDB_REVIEW,
             "created_at": "2020-01-01T00:00:00.000Z", "author_details": {"rating": 8}},
        ]},
    }


def _enrichable(tmdb_id: int = 500001, imdb_id: str = "tt5000001") -> dict:
    """The routes that let one title clear §8 stage 4. Everything else 404s, on purpose."""
    comment = [{"id": 77, "comment": TRAKT_COMMENT, "user_rating": 9, "likes": 3,
                "created_at": "2020-02-02T00:00:00.000Z", "spoiler": False, "review": True,
                "user": {"username": "someone"}}]
    routes = {
        (TMDB_HOST, f"/3/movie/{tmdb_id}"): _json_route(_tmdb_detail(tmdb_id, imdb_id)),
        (TRAKT_HOST, f"/movies/{imdb_id}"): _json_route(
            {"ids": {"trakt": 42, "slug": "the-duellists"}}
        ),
    }
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/{imdb_id}/comments/{sort}")] = (
            _json_route(comment) if sort == "likes" else _json_route([])
        )
    return routes


@pytest.fixture
async def keyed(db, secrets_key):
    """OMDb is left out: decision 377 makes an absent key
    a note, and half-configured is the common install."""
    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": "tmdb-test-key"})
    await secrets.put_connector_secrets(db, "trakt", {"client_id": "trakt-test"}, None)
    return db


async def _documents(db, title_id: int) -> list[dict]:
    """Through the task key, not `title_id`: `raw_document.entity_key`
    is the join the board uses (decision 345)."""
    rows = await db.fetch(
        "SELECT d.source, d.kind, d.ok, d.http_status, d.byte_size"
        "  FROM raw_document d JOIN acquisition_task t ON t.key = d.entity_key"
        " WHERE t.payload ->> 'title_id' = $1 ORDER BY d.id",
        str(title_id),
    )
    return [dict(row) for row in rows]


async def test_a_title_walks_all_ten_stages_with_the_crawl_the_derive_and_the_gate_live(
    db, bundled, keyed, live
):
    """§8's ten stages with 2, 3 and 4 live. Five of eight sources 404 or are absent and
    the walk still reaches `ready`, since the quality bar is stage 4's (decision 334)."""
    clock = _Clock()
    site = _CannedWeb(_enrichable())
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    assert report.ready == 1, report.as_dict()
    walk = report.tasks[0]
    assert walk.stages_run == [s.name for s in SHIPPED], walk.as_dict()
    title_id = walk.title_id

    board = await _board(db, title_id)
    assert (board["stage"], board["status"]) == (10, "ready")
    assert board["reason"] is None, "a ready job carries no park reason"

    # Stage 2, as an operator reads it: what answered, and one note per source that did not.
    enrich = board["detail"]["enrich"]
    assert "tmdb:detail" in enrich["answered"], enrich
    assert set(enrich["answered"]) >= {"tmdb:resolve", "tmdb:detail", "trakt:summary",
                                       "trakt:comments"}, enrich
    assert enrich["notes"]["omdb:detail"] == "omdb is not configured, so this source was not asked"
    assert "metacritic:page" in enrich["notes"] and "rt:page" in enrich["notes"], enrich
    assert enrich["documents"] >= 4

    # Both ledgers ran though this title carries neither:
    # §14.5's failure is a derive that quietly skips them.
    derived = board["detail"]["derive"]
    assert derived["rows"]["credit"] >= 2, derived
    assert "adjudications" in derived and "corrections" in derived, derived

    # Stage 4 cleared, and the counts it cleared on are on the board either way.
    passed = board["detail"]["reviews gate"]
    assert passed == {"plot": True, "sources": 2, "words": passed["words"]}, passed
    assert passed["words"] >= gate.MIN_TOTAL_WORDS

    row = await db.fetchrow(
        "SELECT overview, tmdb_id, imdb_id, trakt_slug, placement, origin FROM title WHERE id = $1",
        title_id,
    )
    assert row["overview"] == PLOT, "stage 3 resolved the card's plot out of the raw document"
    assert row["trakt_slug"] == "the-duellists", "stage 2 wrote the identity it was handed"
    assert (row["placement"], row["origin"]) == ("cold_tower", "acquired")

    # The household's own server was never in the batch: §8's exemption is for the Jellyfin host.
    assert site.hosts() <= {TMDB_HOST, TRAKT_HOST, "query.wikidata.org", "en.wikipedia.org",
                            "www.rottentomatoes.com", MC_HOST, "www.omdbapi.com",
                            "api.tvmaze.com"}, site.hosts()


async def test_every_stage_two_response_is_in_the_raw_store_before_stage_three_reads_one(
    db, bundled, keyed, live, monkeypatch
):
    """Measured from INSIDE stage 3's first statement: every
    non-robots request already has a `raw_document` row."""
    clock = _Clock()
    site = _CannedWeb(_enrichable())
    seen: dict = {}
    real_derive = stages.derive

    async def watched(ctx):
        seen["requests"] = [str(r.url) for r in site.fetched]
        seen["stored"] = await ctx.conn.fetchval(
            "SELECT count(*) FROM raw_document WHERE entity_key = $1", ctx.task.key
        )
        return await real_derive(ctx)

    # Over `live`'s tuple, not `SHIPPED`, which would park the walk at the spend gate.
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        pipeline.Stage(s.number, s.name, watched, s.paid, s.implemented, s.owner, s.fetches)
        if s.number == 3 else s
        for s in pipeline.STAGES
    ))
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    assert report.ready == 1, report.as_dict()

    assert seen["requests"], "stage 2 made no request at all, so this proves nothing"
    assert seen["stored"] == len(seen["requests"]), (
        f"stage 3 began with {seen['stored']} raw_document rows for "
        f"{len(seen['requests'])} requests: {seen['requests']}"
    )
    # Failure rows are stored too, readable off the board.
    documents = await _documents(db, report.tasks[0].title_id)
    assert any(d["ok"] for d in documents) and any(not d["ok"] for d in documents), documents
    assert {d["source"] for d in documents} >= {"tmdb", "trakt", "metacritic"}, documents


async def test_a_metacritic_404_with_tmdb_answering_is_a_note_and_the_walk_reaches_stage_three(
    db, bundled, keyed, live
):
    """Decision 334: a Metacritic 404 with TMDB answering is a note, and the walk gets PAST stage 3."""
    clock = _Clock()
    site = _CannedWeb(_enrichable())
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    assert report.parked == 0 and report.failed == 0, report.as_dict()
    walk = report.tasks[0]
    assert "derive" in walk.stages_run, walk.as_dict()
    board = await _board(db, walk.title_id)
    notes = board["detail"]["enrich"]["notes"]
    assert "metacritic:page" in notes, notes
    assert board["status"] != "parked" or board["stage"] > 2

    # The refused host is still in the raw store as the honest record of what the guess returned.
    documents = await _documents(db, walk.title_id)
    metacritic = [d for d in documents if d["source"] == "metacritic"]
    assert metacritic and all(not d["ok"] for d in metacritic), documents
    assert all(d["http_status"] == 404 for d in metacritic), metacritic


async def test_a_paused_best_effort_host_is_a_sentence_per_view_and_costs_no_request(
    db, bundled, keyed, live
):
    """Decision 422: an open breaker on a best-effort host is a note per view, and the host is not asked."""
    await db.execute(
        "INSERT INTO fetch_host_state (host, paused_until) VALUES ($1, now() + interval '900 s')",
        MC_HOST,
    )
    clock = _Clock()
    site = _CannedWeb(_enrichable())
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = report.tasks[0]
    assert "derive" in walk.stages_run, "a paused best-effort host parked stage 2"
    assert not any(r.url.host == MC_HOST for r in site.seen), "a paused host was asked"
    board = await _board(db, walk.title_id)
    notes = board["detail"]["enrich"]["notes"]
    for kind in ("metacritic:page", "metacritic:reviews"):
        assert f"host {MC_HOST} paused for" in notes[kind], notes
        assert not notes[kind].startswith("HostPaused"), (
            "a class name and an exception message is a log line on the board, not a note"
        )
    assert "so it was not asked" in notes["metacritic:page"], notes


async def test_the_required_source_failing_parks_at_stage_two_and_names_it(
    db, bundled, keyed, live
):
    """Decision 334: a configured TMDB answering 500 parks at stage 2 naming it, with a deadline."""
    clock = _Clock()
    routes = _enrichable()
    routes[(TMDB_HOST, "/3/movie/500001")] = (500, b"upstream is having a day", "text/html")
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    assert report.parked == 1 and report.failed == 0, report.as_dict()
    walk = report.tasks[0]
    assert walk.stage == 2, walk.as_dict()
    assert walk.stages_run == ["identify", "enrich"], "stage 3 must not run on a parked stage 2"
    assert "tmdb:detail" in walk.reason, walk.reason
    assert "acquisition board" in walk.reason, "a reason on §6.6's board names a lever"

    board = await _board(db, walk.title_id)
    assert (board["stage"], board["status"]) == (2, "parked")
    assert board["reason"] == walk.reason, "§6.6 shows the reason verbatim (0005_ledger.sql:138)"
    assert board["retry_after"] is not None, "the board shows the wait it is describing"
    # A park is not a rollback: the other sources' bytes are on disk for the next walk.
    assert board["detail"]["enrich"]["answered"], board["detail"]["enrich"]
    documents = await _documents(db, walk.title_id)
    assert {d["source"] for d in documents} >= {"trakt", "metacritic"}, documents
    # TMDB's 500 left no row: only non-retryable failures are stored; Metacritic's 404 is an answer.
    assert not any(d["source"] == "tmdb" for d in documents), documents

    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["state"] == queue.PENDING and row["attempts"] == 0, (
        "a park with a deadline is a re-ask and never a retry budget (decision 336)"
    )


async def test_a_title_tmdb_holds_no_record_of_walks_on_rather_than_parking_stage_two(
    db, bundled, keyed, live
):
    """TMDB answering "no record" is a fact about the film: the
    title walks on to stage 4 rather than re-walking daily."""
    clock = _Clock()
    routes = _enrichable()
    routes[(TMDB_HOST, "/3/find/tt5000003")] = _json_route({"movie_results": [], "tv_results": []})
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, THIRD) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = report.tasks[0]
    assert walk.stage == 4, walk.as_dict()
    assert walk.status == "parked" and "reviews gate" in walk.reason, walk.as_dict()
    board = await _board(db, walk.title_id)
    enrich = board["detail"]["enrich"]
    assert "tmdb:detail" not in enrich["answered"], enrich
    assert enrich["notes"]["tmdb:detail"] == "no tmdb id on the title, so TMDB could not be asked"
    # `tmdb:resolve` is answered, which is what walks this title on; the next test is the failing case.
    assert "tmdb:resolve" in enrich["answered"], enrich
    assert "TMDB has no record for tt5000003" in enrich["notes"]["tmdb:resolve"], enrich


@pytest.mark.parametrize("status", [401, 500])
async def test_a_refused_or_failed_tmdb_resolve_parks_stage_two_naming_it(
    db, bundled, keyed, live, status
):
    """The same title with TMDB refusing or down: it RAN and
    did not answer, so it parks at stage 2 naming TMDB."""
    clock = _Clock()
    routes = _enrichable()
    routes[(TMDB_HOST, "/3/find/tt5000003")] = _json_route({"status_message": "no"}, status=status)
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, THIRD) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = report.tasks[0]
    assert (walk.stage, walk.status) == (2, "parked"), walk.as_dict()
    assert walk.stages_run == ["identify", "enrich"], "stage 3 must not run on a parked stage 2"
    assert "tmdb:detail" in walk.reason and "tmdb:resolve" in walk.reason, walk.reason
    assert str(status) in walk.reason, "the note saying WHICH way it failed is in the sentence"
    assert "could not be asked" in walk.reason, "tmdb:detail was never asked, so it never answered"
    board = await _board(db, walk.title_id)
    assert (board["stage"], board["status"]) == (2, "parked"), dict(board)
    assert board["reason"] == walk.reason, "the board shows the reason verbatim (0005_ledger.sql:138)"
    assert board["retry_after"] is not None, "a park with no deadline is `queue.skip`"
    assert "tmdb:resolve" not in board["detail"]["enrich"]["answered"], board["detail"]


async def test_an_install_with_no_tmdb_key_notes_it_and_walks_on(db, bundled, live):
    """Decision 377: an absent TMDB key filters the kind
    out as a note; the title parks at stage 4 instead."""
    clock = _Clock()
    site = _CannedWeb({})
    assert await pipeline.enqueue_item(db, MOVIE) is True

    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = report.tasks[0]
    assert walk.stage == 4, walk.as_dict()
    assert walk.status == "parked" and "reviews gate" in walk.reason, walk.as_dict()
    board = await _board(db, walk.title_id)
    notes = board["detail"]["enrich"]["notes"]
    for kind in ("tmdb:resolve", "tmdb:detail", "omdb:detail", "trakt:summary"):
        assert notes[kind].endswith("is not configured, so this source was not asked"), notes
    assert not any(r.url.host in (TMDB_HOST, TRAKT_HOST) for r in site.fetched), (
        "an absent credential costs no request at all, not even a robots.txt read"
    )


async def test_a_thin_title_parks_at_the_reviews_gate_with_both_counts_and_a_thirty_day_window(
    db, bundled, keyed, live
):
    """Decision 335: one source with plenty of words is thin. The board's and the queue's dates are asserted
    equal, since `_record_stop` writes one instant to both."""
    clock = _Clock()
    routes = _enrichable()
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/tt5000001/comments/{sort}")] = _json_route([])
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True

    before = datetime.now(UTC)
    report = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    after = datetime.now(UTC)

    assert report.parked == 1 and report.failed == 0, report.as_dict()
    walk = report.tasks[0]
    assert walk.stage == 4, walk.as_dict()
    assert walk.stages_run == ["identify", "enrich", "derive", "reviews gate"], walk.as_dict()

    # Both counts tell an operator how close the title is; the plot is named only when missing.
    assert walk.reason.startswith("reviews gate: 1 source, "), walk.reason
    assert "retry window 30 days" in walk.reason, walk.reason
    assert "no plot" not in walk.reason, "the plot is there; naming it reads as a complaint"

    board = await _board(db, walk.title_id)
    assert (board["stage"], board["status"]) == (4, "parked")
    assert board["reason"] == walk.reason, "§6.6 shows the reason verbatim"
    assert board["detail"]["reviews gate"] == {"plot": True, "sources": 1,
                                               "words": board["detail"]["reviews gate"]["words"]}

    window = board["retry_after"]
    assert window is not None, (
        "the gate parked with no deadline, which is `queue.skip`: the task is closed for good and "
        "the thirty-day window it told an operator about can never come (decision 336)"
    )
    assert before + gate.REVIEW_WINDOW <= window <= after + gate.REVIEW_WINDOW, window
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["next_attempt_at"] == window, (
        "the queue leases on one instant and the board shows another: two writes, one truth"
    )
    # No attempt spent: decision 336's timed park is a re-ask.
    assert row["state"] == queue.PENDING and row["attempts"] == 0, dict(row)
    assert row["last_error"] is None, "nothing raised, so nothing is recorded as an error"


async def test_a_retry_of_a_parked_gate_resumes_at_stage_four_and_makes_no_request(
    db, bundled, keyed, live
):
    """Zero requests asserted three ways: an empty log, an
    uncalled factory, and `stages_run` starting at the gate."""
    clock = _Clock()
    routes = _enrichable()
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/tt5000001/comments/{sort}")] = _json_route([])
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    first = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    assert first.parked == 1, first.as_dict()
    title_id = first.tasks[0].title_id
    assert site.fetched, "the first walk made no request, so the second proves nothing"

    counted = await _derived_counts(db, title_id)
    assert counted["review"] >= 1 and counted["credit"] >= 1, counted

    # The admin retry is a task due now; the window, not the state, is what makes it wait.
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, "jellyfin:jf-acq-1",
    )
    made: list[str] = []
    site.seen.clear()

    async def counting(conn):
        made.append("built")
        return await _factory(site, clock)(conn)

    second = await pipeline.drain(db, limit=1, fetcher_factory=counting)

    assert second.leased == 1, second.as_dict()
    walk = second.tasks[0]
    assert walk.stages_run[0] == "reviews gate", walk.as_dict()
    assert "identify" not in walk.stages_run and "enrich" not in walk.stages_run, walk.as_dict()
    assert site.fetched == [], [str(r.url) for r in site.fetched]
    assert made == [], "an HTTP client was built for a walk that never reaches a fetching stage"
    assert await _derived_counts(db, title_id) == counted, "the resume duplicated a derived row"


async def test_the_window_closing_asks_the_sources_again_and_counts_what_accrued(
    db, bundled, keyed, live
):
    """Decision 421: when the window itself closes the walk
    re-enters at stage 2, so accrued reviews are fetched."""
    clock = _Clock()
    routes = _enrichable()
    accrued = dict(routes)
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/tt5000001/comments/{sort}")] = _json_route([])
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    first = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    assert (first.tasks[0].stage, first.tasks[0].status) == (4, "parked"), first.as_dict()
    assert first.tasks[0].reason.startswith("reviews gate: 1 source, "), first.tasks[0].reason
    title_id = first.tasks[0].title_id

    site.routes = accrued
    site.seen.clear()
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, "jellyfin:jf-acq-1",
    )
    await db.execute(
        "UPDATE acquisition_job SET retry_after = now() - interval '1 minute' WHERE title_id = $1",
        title_id,
    )

    second = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = second.tasks[0]
    assert walk.stages_run[:3] == ["enrich", "derive", "reviews gate"], walk.as_dict()
    assert "identify" not in walk.stages_run, "the window re-asks the sources, not the mint"
    assert any(r.url.path == "/movies/tt5000001/comments/likes" for r in site.fetched), (
        "the window closed and no host was asked, so a review written since cannot be counted"
    )
    assert walk.status == "ready", walk.as_dict()
    board = await _board(db, title_id)
    assert board["detail"]["reviews gate"]["sources"] == 2, board["detail"]["reviews gate"]
    assert await db.fetchval(
        "SELECT count(*) FROM review_store.review WHERE title_id = $1 AND source = 'trakt'",
        title_id,
    ) == 1, "the accrued comment is in the store exactly once"


async def test_a_title_still_thin_when_the_window_closes_parks_for_another_window(
    db, bundled, keyed, live
):
    """Still thin after the re-ask parks for a NEW window, with no attempt spent."""
    clock = _Clock()
    routes = _enrichable()
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/tt5000001/comments/{sort}")] = _json_route([])
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    first = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    title_id = first.tasks[0].title_id
    counted = await _derived_counts(db, title_id)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, "jellyfin:jf-acq-1",
    )
    await db.execute(
        "UPDATE acquisition_job SET retry_after = now() - interval '1 minute' WHERE title_id = $1",
        title_id,
    )
    site.seen.clear()

    before = datetime.now(UTC)
    second = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    walk = second.tasks[0]
    assert walk.stages_run == ["enrich", "derive", "reviews gate"], walk.as_dict()
    assert site.fetched, "the window closed, so the sources were asked again"
    assert (walk.stage, walk.status) == (4, "parked"), walk.as_dict()
    board = await _board(db, title_id)
    assert board["retry_after"] >= before + gate.REVIEW_WINDOW, board["retry_after"]
    row = await _task_row(db, "jellyfin:jf-acq-1")
    assert row["next_attempt_at"] == board["retry_after"], "two writes, one truth"
    assert row["state"] == queue.PENDING and row["attempts"] == 0, dict(row)
    assert await _derived_counts(db, title_id) == counted, "the re-derive duplicated a row"


async def test_a_key_typed_in_during_the_window_is_asked_when_it_closes(
    db, bundled, live, secrets_key
):
    """A key typed in during the window is asked when it
    closes, as the gate's reason promises (decision 421)."""
    clock = _Clock()
    site = _CannedWeb(_enrichable())
    assert await pipeline.enqueue_item(db, MOVIE) is True
    first = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    walk = first.tasks[0]
    assert (walk.stage, walk.status) == (4, "parked"), walk.as_dict()
    assert walk.reason.startswith("reviews gate: no plot yet, 0 sources, 0 words"), walk.reason
    assert "sources are asked again" in walk.reason, walk.reason
    assert not any(r.url.host in (TMDB_HOST, TRAKT_HOST) for r in site.fetched)
    title_id = walk.title_id

    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": "tmdb-test-key"})
    await secrets.put_connector_secrets(db, "trakt", {"client_id": "trakt-test"}, None)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, "jellyfin:jf-acq-1",
    )
    await db.execute(
        "UPDATE acquisition_job SET retry_after = now() - interval '1 minute' WHERE title_id = $1",
        title_id,
    )
    site.seen.clear()

    second = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))

    assert any(r.url.host == TMDB_HOST for r in site.fetched), (
        "the window closed after a key was typed in and TMDB was not asked, so the reason told "
        "the operator to wait for something no walk does"
    )
    assert second.tasks[0].status == "ready", second.tasks[0].as_dict()
    board = await _board(db, title_id)
    assert board["detail"]["reviews gate"]["plot"] is True, board["detail"]["reviews gate"]


async def _derived_counts(db, title_id: int) -> dict:
    """One count per table stage 3 writes: the three places a derive can duplicate unrefused."""
    return {
        "credit": await db.fetchval("SELECT count(*) FROM credit WHERE title_id = $1", title_id),
        "review": await db.fetchval(
            "SELECT count(*) FROM review_store.review WHERE title_id = $1", title_id
        ),
        "title_meta": await db.fetchval(
            "SELECT count(*) FROM title_meta WHERE title_id = $1", title_id
        ),
        "people": await db.fetchval(
            "SELECT count(DISTINCT person_id) FROM credit WHERE title_id = $1", title_id
        ),
    }


async def test_a_drain_whose_tasks_never_reach_stage_two_opens_no_socket(db, data_dir, live):
    """Decision 373: a walk that never reaches stage 2 constructs no client and makes no connector read."""
    built: list[str] = []

    async def never(conn):
        built.append("built")
        return fetch.Fetcher(conn=conn, transport=httpx.MockTransport(_CannedWeb({}).handler))

    assert await pipeline.enqueue_item(db, NO_IDS) is True
    report = await pipeline.drain(db, limit=1, fetcher_factory=never)

    assert report.parked == 1, report.as_dict()
    assert report.tasks[0].stages_run == ["identify"], report.tasks[0].as_dict()
    assert built == [], "a fetcher was built for a walk that stopped at stage 1"


async def test_only_the_stage_that_declares_it_fetches_is_given_the_fetcher(
    db, bundled, keyed, live, monkeypatch
):
    """`Stage.fetches` is hand-written: the Fetcher is built AT the first stage declaring it, recorded
    per stage. It stays on the context afterwards by design; `derive/` imports no transport."""
    handed: dict = {}

    def watching(stage):
        async def run(ctx):
            handed[stage.number] = ctx.fetcher is not None
            return await stage.run(ctx)
        return pipeline.Stage(stage.number, stage.name, run, stage.paid, stage.implemented,
                              stage.owner, stage.fetches)

    # Over `live`'s tuple: a shipped stage 6 parks this walk at the spend gate.
    monkeypatch.setattr(pipeline, "STAGES", tuple(watching(s) for s in pipeline.STAGES))
    assert await pipeline.enqueue_item(db, MOVIE) is True
    report = await pipeline.drain(db, limit=1,
                                  fetcher_factory=_factory(_CannedWeb(_enrichable()), _Clock()))

    assert report.ready == 1, report.as_dict()
    assert handed == {n: n >= 2 for n in range(1, 11)}, handed
    assert handed[1] is False, (
        "stage 1 ran on a context that already carried a Fetcher, so it was built before the loop "
        "rather than at the stage that declared it - and a walk that parks at stage 1 then pays "
        "for an HTTP client it never uses"
    )
    # Stage 6 posts to its provider through the drain's one Fetcher (decisions 373, 432).
    assert [s.number for s in SHIPPED if s.fetches] == [2, 6], (
        "§8 stages 2 and 6 are the stages that reach the network; another one sets the same "
        "flag on the same line rather than teaching the driver a stage number"
    )


async def test_a_stage_two_handed_no_fetcher_fails_and_names_the_driver(db, bundled, keyed, live):
    """A stage handed no Fetcher fails, never parks or builds
    its own: a second bucket would double the rate."""
    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert report.status == "failed", report.as_dict()
    assert report.stage == 2
    assert report.reason == stages.NO_FETCHER
    assert "pipeline.drain" in report.reason and "decision 373" in report.reason
    board = await _board(db, report.title_id)
    assert (board["stage"], board["status"]) == (2, "failed")
    assert board["detail"]["enrich"]["retrying"] is True, (
        "a failure records whether the machine will try again; this one is a code defect and "
        "will not fix itself, but the board must still say which it is"
    )


async def test_a_stage_six_handed_no_fetcher_fails_and_names_the_driver(
    db, data_dir, secrets_key, extraction_live
):
    """Stage 6 likewise, reached through the gate with a cap set; no provider is asked."""
    await registry.save_connector(db, "gemini", api_key="GEMINI-KEY-NOT-A-REAL-ONE-0006")
    await registry.save_connector(db, "llm", extraction_provider="gemini", cap_usd=100)

    task = await _leased(db, item=MOVIE)
    report = await pipeline.run_task(db, task)

    assert report.status == "failed", report.as_dict()
    assert report.stage == 6
    assert report.reason == stages.NO_EXTRACTION_FETCHER
    assert "pipeline.drain" in report.reason and "decision 373" in report.reason
    board = await _board(db, report.title_id)
    assert (board["stage"], board["status"]) == (6, "failed")
    assert board["detail"]["dna extract"]["retrying"] is True
    assert await db.fetchval("SELECT count(*) FROM llm_call") == 0


# Stages 5 and 7 put back with `dna_live` and walked by the driver on a leased task.
# Stage 8's two branches are `test_flywheel_feed.py`'s.

DNA_VERSION = "v1"
DNA_RUN = 4601
# A Wikipedia craft section, so the pack has a supplement: without one the base `PackInfo` is right anyway.
DNA_PLOT = "Two officers of Napoleon's cavalry fight a string of duels across sixteen years."
DNA_REVIEWS = (("tmdb", " ".join(["duel"] * 60)), ("trakt", " ".join(["sabre"] * 60)))
DNA_ARTICLE = (
    "== Plot ==\nTwo officers duel.\n"
    "== Music ==\nHoward Blake's score is a spare chamber piece for strings, held back through the "
    "long rides and let loose only at the duels, which it scores as ceremony rather than action.\n"
)
# What an M5.5-M5.7 install wrote for titles reaching stage 6, restated: `llm/extract.py` no longer says it.
UNWIRED_NO_PACK = (
    "no DNA pack is stored for this title under vocabulary v1, so there is nothing to extract from "
    "and no provider is called. Section 8 stage 5 builds the pack, and stage 5 is not wired in this "
    "build (decision 387); this title resumes here once a pack is stored (decision 432)"
)


async def _dna_vocabulary(db) -> None:
    """An active vocabulary with nothing in it: stage 5 needs a version to key the pack by."""
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 0, 0)", DNA_VERSION
    )


async def _dna_title(db) -> int:
    """An acquired title with a plot and two review sources; no task and no board row yet."""
    title_id = await db.fetchval(
        "INSERT INTO title (kind, name, year, is_owned, origin)"
        " VALUES ('movie', 'The Duellists', 1977, true, 'acquired') RETURNING id"
    )
    await db.execute("INSERT INTO title_meta (title_id, source, payload) VALUES ($1, 'tmdb', $2)",
                     title_id, {"plot_full": DNA_PLOT})
    await db.executemany(
        "INSERT INTO review_store.review (title_id, source, body, is_critic) VALUES ($1, $2, $3, true)",
        [(title_id, source, body) for source, body in DNA_REVIEWS],
    )
    return title_id


async def _filed(db, title_id: int) -> None:
    """What stage 6 leaves behind: two extracted-tier rows, one with its quote, and refusals filed
    under this walk's run, under another run, and under none."""
    tag = None
    for term in ("mood.bleak", "themes.revenge"):
        tag = await db.fetchval(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
            " VALUES ($1, $2, $3, split_part($3, '.', 1), 2, 'gemini') RETURNING id",
            title_id, DNA_VERSION, term,
        )
    await db.execute("INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'duel', 'tmdb:1')",
                     tag)
    await db.executemany(
        "INSERT INTO dna_reject (title_id, run_id, term, rule_violated, provider)"
        " VALUES ($1, $2, $3, $4, 'gemini')",
        [
            (title_id, DNA_RUN, "mood.invented", "unknown_term"),
            (title_id, DNA_RUN, "mood.bleak", "quote_unverified"),
            (title_id, DNA_RUN, "themes.revenge", "quote_unverified"),
            (title_id, DNA_RUN + 1, "mood.bleak", "schema"),
            (title_id, None, "themes.revenge", "adjudicated"),
        ],
    )


async def _dna_rows(db) -> tuple:
    return tuple([
        await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in ("dna_tag", "dna_evidence", "dna_reject", "dna_pack")
    ])


async def test_stage_five_stores_the_augmented_pack_under_the_task_key_and_stage_six_reads_it_back(
    db, data_dir, dna_live
):
    """Decision 461: the stored pack is the AUGMENTED text
    with a recomputed digest, filed under the task key."""
    await _dna_vocabulary(db)
    title_id = await _dna_title(db)
    task = await _leased(db, title_id=title_id)
    await rawstore.store(
        db, source="wikipedia", kind="article", url=f"https://en.wikipedia.org/w/api.php?{task.key}",
        entity_key=task.key,
        content=json.dumps({"query": {"pages": [{"extract": DNA_ARTICLE}]}}).encode("utf-8"),
    )
    await pipeline.write_board(db, title_id, stage=5, status=pipeline.RUNNING)

    report = await pipeline.run_task(db, task, run_id=DNA_RUN)

    assert report.stages_run[:2] == ["dna pack", "dna extract"], report.as_dict()
    assert report.stage == 9, "stage 5 advanced and the walk stopped at stage 9's bundle-less park"
    row = await db.fetchrow("SELECT * FROM dna_pack WHERE title_id = $1 AND version = $2",
                            title_id, DNA_VERSION)
    assert row is not None, "stage 5 advanced and stored no pack"
    pack = await verify.read_pack(db, title_id, DNA_VERSION)
    base, base_info = await packs.build_pack(db, title_id)
    assert craft.SENTINEL in pack and "spare chamber piece" in pack, (
        "the pack stage 6 reads carries no craft supplement"
    )
    assert craft.base_pack(pack) == base, "the base pack is not an exact prefix of the stored one"
    assert (row["pack_sha"], row["chars"]) == (packs.sha(pack), len(pack))
    assert row["chars"] > base_info.chars
    assert (row["n_reviews"], row["n_sources"]) == (base_info.n_reviews, base_info.n_sources) == (2, 2)
    document = await db.fetchrow(
        "SELECT source, entity_key, run_id FROM raw_document WHERE id = $1", row["raw_document_id"]
    )
    assert tuple(document) == ("pack", task.key, DNA_RUN), dict(document)
    detail = (await _board(db, title_id))["detail"]["dna pack"]
    assert {key: detail[key] for key in ("version", "pack_sha", "chars", "base_chars",
                                         "raw_document_id", "n_sections", "n_rt")} == {
        "version": DNA_VERSION, "pack_sha": row["pack_sha"], "chars": len(pack),
        "base_chars": base_info.chars, "raw_document_id": row["raw_document_id"], "n_sections": 1,
        "n_rt": 0,
    }, detail


async def test_stage_five_parks_with_a_deadline_when_no_vocabulary_is_active(db, data_dir, dna_live):
    """No active vocabulary parks stage 5 with a deadline, not a failure (decisions 336, 461)."""
    title_id = await _dna_title(db)
    task = await _leased(db, title_id=title_id)
    await pipeline.write_board(db, title_id, stage=5, status=pipeline.RUNNING)

    report = await pipeline.run_task(db, task)

    assert (report.stage, report.status, report.stages_run) == (5, "parked", ["dna pack"]), (
        report.as_dict()
    )
    assert report.reason == stages.NO_PACK_VOCABULARY
    report.reason.encode("ascii")
    board = await _board(db, title_id)
    assert (board["stage"], board["status"], board["reason"]) == (5, "parked", report.reason)
    assert board["retry_after"] is not None and board["retry_after"] > datetime.now(UTC)
    row = await _task_row(db, task.key)
    assert row["state"] == queue.PENDING, "a park with no deadline closes the task for good"
    assert row["next_attempt_at"] == board["retry_after"]
    assert await _dna_rows(db) == (0, 0, 0, 0)
    assert await db.fetchval("SELECT count(*) FROM raw_document WHERE source = 'pack'") == 0


async def test_stage_seven_advances_with_the_rejects_stage_six_filed_and_asks_no_second_verdict(
    db, data_dir, dna_live, monkeypatch
):
    """Decision 462: stage 7 records stage 6's verdict and this run's refusals only, and writes nothing."""
    await _dna_vocabulary(db)
    title_id = await _dna_title(db)
    task = await _leased(db, title_id=title_id)
    await _filed(db, title_id)
    asked: list[str] = []

    async def second_verdict(*_args, **_kwargs):
        asked.append("asked")
        raise AssertionError("stage 7 asked for a verdict stage 6 already reached (decision 462)")

    monkeypatch.setattr(verify, "verify_payload", second_verdict)
    monkeypatch.setattr(extract, "extract_title", second_verdict)
    before = await _dna_rows(db)
    await pipeline.write_board(db, title_id, stage=7, status=pipeline.RUNNING)

    report = await pipeline.run_task(db, task, run_id=DNA_RUN)

    assert report.stages_run[:1] == ["verify"] and report.stage == 9, report.as_dict()
    assert asked == []
    assert (await _board(db, title_id))["detail"]["verify"] == {
        "version": DNA_VERSION, "tags": 2, "rejected": {"quote_unverified": 2, "unknown_term": 1},
    }
    assert await _dna_rows(db) == before, "stage 7 wrote a row"


async def test_stage_seven_with_no_run_reads_no_reject(db, data_dir, dna_live):
    """`run_id = $2`, not `IS NOT DISTINCT FROM`, so a run-less walk reads no refusal as its own."""
    await _dna_vocabulary(db)
    title_id = await _dna_title(db)
    task = await _leased(db, title_id=title_id)
    await _filed(db, title_id)
    await pipeline.write_board(db, title_id, stage=7, status=pipeline.RUNNING)

    report = await pipeline.run_task(db, task)

    assert report.stages_run[:1] == ["verify"], report.as_dict()
    assert (await _board(db, title_id))["detail"]["verify"] == {
        "version": DNA_VERSION, "tags": 2, "rejected": {},
    }


async def test_an_expired_stage_six_park_re_enters_at_stage_five_and_stores_a_pack(
    db, data_dir, dna_live, extraction_live
):
    """Decision 467: an expired stage-6 park re-enters at stage 5 and stores the pack before the gate."""
    await _dna_vocabulary(db)
    title_id = await _dna_title(db)
    assert await pipeline.enqueue_title(db, title_id) is True
    await pipeline.write_board(db, title_id, stage=6, status=pipeline.PARKED, reason=UNWIRED_NO_PACK,
                               retry_after=datetime.now(UTC) - timedelta(minutes=1))

    report = await pipeline.drain(db, limit=1)

    walk = report.tasks[0]
    assert walk.stages_run == ["dna pack", "dna extract"], walk.as_dict()
    assert (walk.stage, walk.status, walk.reason) == (6, "parked", pipeline.NO_SPEND_CAP)
    assert await db.fetchval("SELECT count(*) FROM dna_pack WHERE title_id = $1 AND version = $2",
                             title_id, DNA_VERSION) == 1
    assert await db.fetchval("SELECT count(*) FROM llm_call") == 0


async def test_a_stage_six_park_made_due_early_resumes_at_stage_six(
    db, data_dir, dna_live, extraction_live
):
    """Decision 467: the same park made due early resumes at stage 6 and builds nothing."""
    await _dna_vocabulary(db)
    title_id = await _dna_title(db)
    assert await pipeline.enqueue_title(db, title_id) is True
    await pipeline.write_board(db, title_id, stage=6, status=pipeline.PARKED, reason=UNWIRED_NO_PACK,
                               retry_after=datetime.now(UTC) + timedelta(days=1))

    report = await pipeline.drain(db, limit=1)

    walk = report.tasks[0]
    assert walk.stages_run == ["dna extract"], walk.as_dict()
    assert (walk.stage, walk.status) == (6, "parked"), walk.as_dict()
    assert await db.fetchval("SELECT count(*) FROM dna_pack") == 0


async def test_a_launched_bundle_title_nobody_owns_parks_at_stage_ten_and_resumes_there(
    db, bundled, dna_live
):
    """A launched bundle title nobody owns parks at stage 10 and is re-asked there alone."""
    title_id = await db.fetchval(
        "SELECT t.id FROM title t WHERE t.origin = 'bundle' AND t.placement IN ('cold_tower', 'warm')"
        "   AND NOT EXISTS (SELECT 1 FROM acquisition_job j WHERE j.title_id = t.id)"
        " ORDER BY t.id LIMIT 1"
    )
    assert title_id is not None, "the fixture bundle places no title that has no board row"
    await db.execute("UPDATE title SET is_owned = false, owned_checked_at = NULL WHERE id = $1",
                     title_id)
    async with db.transaction():
        assert await actions.make_due(db, title_id, stage=5, reason="launched by this test") == 1

    first = next(t for t in (await pipeline.drain(db)).tasks if t.title_id == title_id)

    assert first.stages_run == [s.name for s in SHIPPED if s.number >= 5], first.as_dict()
    assert (first.stage, first.status) == (10, "parked"), first.as_dict()
    assert "no ownership flag" in first.reason, first.reason
    board = await _board(db, title_id)
    assert board["retry_after"] is not None and board["retry_after"] > datetime.now(UTC)
    assert "decision 162" in board["detail"]["project"]["kept"], board["detail"]["project"]
    packed = await db.fetchval("SELECT count(*) FROM raw_document WHERE source = 'pack'")
    assert packed == 1

    await db.execute(
        "UPDATE acquisition_job SET retry_after = now() - interval '1 minute' WHERE title_id = $1",
        title_id,
    )
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'"
        " WHERE kind = $1 AND payload ->> 'title_id' = $2", pipeline.TASK_KIND, str(title_id),
    )
    again = next(t for t in (await pipeline.drain(db)).tasks if t.title_id == title_id)

    assert again.stages_run == ["ready"], again.as_dict()
    assert (again.stage, again.status) == (10, "parked"), again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM raw_document WHERE source = 'pack'") == packed
