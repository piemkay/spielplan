"""The ten-stage driver: the names, the park, the resume, the mint, and the two shipped stages.

Spec v2.1 §8 (the pipeline, "Failure at any stage parks the job with a reason, retryable from
admin", "paid stages (6) never auto-retry past the spend cap"), §4.1 (the id partition), §5.3,
§6.0 row 6; decisions 162, 322, 323, 336.

TWO HALVES, and the split is the one the plan's "What must be asserted where" draws.

  * **No database.** The ten names, read back out of `docs/spielplan-spec_v2.1.md`'s own §8 block
    rather than out of a list retyped here, and the spend gate's refusal - which is a pure
    decision about a `Stage` and needs nothing but the stage.
  * **Postgres.** Everything else, because everything else is a claim about two tables agreeing:
    the mint lands above 1e9 and the board row appears with it; a park writes its reason verbatim
    and the resume re-enters at that stage; a second run duplicates nothing. None of those is
    checkable against a stub connection, and the one that matters most - the nightly sweep cannot
    drag a title in flight backwards - is a claim about an `ON CONFLICT` clause.

STAGES 2-8 ARE DECLARED NO-OPS AT M5.1, which is what makes the walk from 1 to 9 to 10 runnable
at all. That is the point of D3 rather than a limitation of these tests: the spine is provable
before any lane opens, and every test below that walks the whole pipeline is also a test that the
stubs advance rather than silently ending the run.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from spielplan.acquire import pipeline, queue, stages
from spielplan.connectors import resolve
from spielplan.core.config import settings
from spielplan.db.pool import _init_connection as pool_init
from spielplan.home import shelves
from spielplan.importer import bundle as bundle_import
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from tests.fixtures import make_bundle as fx

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spielplan-spec_v2.1.md"

# The tuple as this build ships it, captured before any test patches it. A test that put the
# stages back by reading `pipeline.STAGES` would read whatever the patch left there.
SHIPPED = pipeline.STAGES

# The §8 block's own shape: a stage number, the name, then at least two spaces before the prose.
# Two spaces and not one, because "reviews gate", "dna pack" and "dna extract" carry a single
# space INSIDE the name -- a one-space separator would read three of the ten as "reviews" and
# lose the word the board is supposed to show.
_STAGE_LINE = re.compile(r"^(\d{1,2})\s+(\S+(?: \S+)*?)\s{2,}\S")


def _stage_names_from(text: str) -> list[tuple[int, str]]:
    """Parse §8's fenced pipeline block out of a spec document. See the test that feeds it a
    doctored one."""
    start = text.index("## 8. New-title acquisition workflow")
    fence = re.search(r"```\r?\n(.*?)```", text[start:], re.S)
    assert fence is not None, "§8 has no fenced pipeline block"
    found = []
    for line in fence.group(1).splitlines():
        match = _STAGE_LINE.match(line)
        if match:
            found.append((int(match.group(1)), match.group(2)))
    return found


# --- the names (backend, no DB) -----------------------------------------------------------------


def test_the_ten_stage_names_are_read_back_out_of_the_spec_file():
    """§8's ten stages, verbatim and in order, against the normative document itself.

    The whole value of this test is the SOURCE of the expected list. A test carrying its own copy
    of the ten names asserts that `pipeline.STAGES` equals a list a test author typed, which is
    true of two files that drifted from the spec together. Reading the spec's own block means an
    amendment to §8 that renames a stage reddens this build, which is CLAUDE.md's rule in a test:
    "where code and spec disagree, the code is the bug".

    Proposal 136 argues §6.6's board should show these strings verbatim; whether that clause is
    adopted is decision 330's, which is M5.6's to take. The tuple is one spelling either way,
    because two spellings is the state where a board label and a park reason name different
    stages.
    """
    from_spec = _stage_names_from(SPEC.read_text(encoding="utf-8"))
    from_code = [(s.number, s.name) for s in pipeline.STAGES]
    assert from_spec == from_code
    assert from_code == [
        (1, "identify"), (2, "enrich"), (3, "derive"), (4, "reviews gate"), (5, "dna pack"),
        (6, "dna extract"), (7, "verify"), (8, "project"), (9, "place"), (10, "ready"),
    ]


def test_the_stage_name_reader_is_reading_the_spec_and_not_agreeing_with_itself():
    """The guard above is only worth having if it can fail, and it has one way to be vacuous:
    a parser that found nothing would compare two empty lists and pass.

    So feed it a doctored §8 - one stage renamed, one dropped - and show it reports the document
    it was given rather than the tuple the module holds. Same shape as the self-checks in
    `test_acquire_rawstore.py`, and for the same reason: a static guard that never looked is
    indistinguishable from one that looked and approved.
    """
    doctored = (
        "## 8. New-title acquisition workflow\n\n"
        "```\n"
        "1  identify      resolve it\n"
        "2  enrichment    fetch it\n"
        "9  place         place it\n"
        "```\n"
    )
    assert _stage_names_from(doctored) == [(1, "identify"), (2, "enrichment"), (9, "place")]
    assert _stage_names_from(doctored) != [(s.number, s.name) for s in pipeline.STAGES]


def test_stage_six_is_the_only_paid_stage_and_every_stub_names_the_milestone_that_owes_it():
    """§8: "paid stages (6) never auto-retry past the spend cap" - the number is in the spec and
    it is the only one there.

    The second half is D3's visibility rule. A stub that survives into M5.6 has to be findable,
    so each declares its owner twice: in the tuple, where this test reads it, and in its own
    docstring, where a person greps for it. The owners are `ROADMAP-M5.md`'s allocation of the
    work and not a guess - M5.3 has the sources, the parsers and the reviews gate, M5.4 the pack,
    the trust boundary and the projection, M5.5 the LLM extraction.
    """
    assert [s.number for s in pipeline.STAGES if s.paid] == [6]
    assert {s.number: s.owner for s in pipeline.STAGES if not s.implemented} == {
        2: "M5.3", 3: "M5.3", 4: "M5.3", 5: "M5.4", 6: "M5.5", 7: "M5.4", 8: "M5.4",
    }
    for stage in pipeline.STAGES:
        if stage.implemented:
            continue
        doc = stage.run.__doc__ or ""
        assert stage.owner in doc, f"stage {stage.number} does not name its owner in its docstring"

    # AND THE PAID STAGE NAMES THE SAME MILESTONE FOR THE CAP that decision 348's own title does:
    # "M5.1 owns the refusal, M5.5 owns the cap". Its docstring said the pipeline parks at stage 6
    # "until M5.7 supplies the cap" - the one line in the tree that files the cap under M5.7, where
    # four others (decision 348, `refuse_uncapped_spend`, `ROADMAP-M5.md:314-329` and the roadmap's
    # question ledger, which puts decision 325 under "M5.5 / 0028") say M5.5, and where the roadmap
    # gives M5.7 no migration at all. M5.7 owns the SURFACE for setting a spend guard. This is the
    # docstring an M5.5 author reads first, so under the old sentence the correct outcome of their
    # own commit was a pipeline parked for two further milestones - a state M5.5's own exit
    # criterion forbids. [M5.1 review cycle 4 second pass, M51-C4-PAID-06]
    paid = next(s for s in pipeline.STAGES if s.paid)
    owes = re.search(r"until (M5\.\d) supplies the cap", paid.run.__doc__ or "")
    assert owes and owes.group(1) == paid.owner, (
        f"the paid stage hands the cap to {owes and owes.group(1)} and its own owner is "
        f"{paid.owner}; decision 348 says the milestone that fills the stage is the one that owes "
        "it a cap"
    )


async def test_an_implemented_paid_stage_is_refused_while_a_declared_no_op_is_not():
    """The seam M5.1 owes M5.5, asserted on the gate itself.

    §8 says a paid stage "never auto-retries past the spend cap", which is a rule about a stage
    that BILLS - so the contract has to carry a stage that refuses to run rather than one that
    runs and then checks. `refuse_uncapped_spend` asks exactly that question, and the two answers
    below are the two halves of it:

      * a declared no-op spends nothing, so refusing it would park every task at stage 6 today
        and make M5.1's own exit criterion unsatisfiable;
      * an implemented paid stage can spend, and no cap exists yet, so it parks - never fails,
        because a failure spends attempts and four of them would lose the title over a setting
        nobody has configured (decision 336).

    The day M5.5 gives `stages.dna_extract` a body, `implemented` becomes True and this refusal
    starts firing. That is the seam holding rather than a test of a flag.
    """
    ctx = stages.StageContext(conn=None, task=None)
    shipped = next(s for s in pipeline.STAGES if s.paid)
    assert await pipeline.refuse_uncapped_spend(shipped, ctx) is None

    billing = pipeline.Stage(6, "dna extract", stages.dna_extract, paid=True, implemented=True)
    refusal = await pipeline.refuse_uncapped_spend(billing, ctx)
    assert refusal is not None
    assert refusal.verb == stages.PARK
    assert "spend cap" in refusal.reason
    # WITH A DEADLINE, which is the one property of this gate nothing asserted while its own
    # paragraph called a deadline-less park "strictly worse than the failure this paragraph
    # rejects". Deleting the `until=` from the shipped gate passed every test in the tree and
    # started closing tasks on their first refusal. [M5.1 review cycle 4 second pass,
    # M51-C4-PAID-04]
    assert refusal.until is not None, (
        "a park with no deadline is queue.skip, which closes the task on attempt one - and "
        "nothing in this tree moves a row out of skipped before decision 330 arrives at M5.6"
    )

    free = next(s for s in pipeline.STAGES if not s.paid and not s.implemented)
    assert await pipeline.refuse_uncapped_spend(free, ctx) is None


# --- the integration fixtures --------------------------------------------------------------------


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
# `ops/fake_jellyfin.py:52-53`'s own awkward fixture, restated: an item Jellyfin gives no provider
# id for. Decision 323 says this one parks with the reason "no provider id" and mints nothing.
NO_IDS = {
    "Id": "jf-acq-4", "Name": "Tampopo", "Type": "Movie", "ProductionYear": 1985,
    "RunTimeTicks": 114 * 60 * 10_000_000, "ProviderIds": {},
}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR`, and therefore `settings().artifacts_dir`, under this test's own tmp_path.

    Through the environment rather than a parameter, because that is how `stages.active_store`
    reaches it in production - `ArtifactStore.load_active(conn, settings().artifacts_dir)`,
    `worker.py:390`'s own call. A test that passed a path in would be exercising an argument that
    does not exist on the real path. `settings()` is `lru_cache`d, so the cache is cleared on the
    way in and on the way out (`test_acquire_rawstore.py` does the same for `raw_dir`).
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def bundled(db, data_dir, tmp_path):
    """A real bundle, imported and active, so stage 9 has a basis to place against.

    The fixture contract's column NAMES are the shipped placeholders rather than
    `test_placement.py`'s realistic rewrite, and that is deliberate here: an acquired title has no
    genres, keywords, credits or DNA rows of its own at M5.1 because stages 2 and 3 are declared
    no-ops, so it is thin whatever the contract says. Thin is the state this milestone's titles
    are actually in on a real install today, and it is the state `_park_thin` reacts to - which is
    the collision this file has to prove is harmless.
    """
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    return report


async def _leased(db, *, item=None, title_id=None) -> queue.Task:
    """Enqueue one task and lease it, the way a drain tick would.

    Leased rather than constructed, because `queue.lease` is what counts the attempt and sets the
    expiry, and a driver test that handed itself a `Task` it built would be testing the driver
    against a row no worker could ever hold.
    """
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


# --- the mint, the placement and the badge -------------------------------------------------------


async def test_an_item_with_provider_ids_is_minted_placed_and_badged(db, bundled):
    """§8 stages 1, 9 and 10 end to end, which is M5.1's exit criterion in one test.

    THREE ITEMS AND NOT ONE, because the surface this ends at has a floor. §6.0's shelves suppress
    below `SECTION_FLOOR` (proposal 28's floor of three), so a single minted title would prove the
    board and prove nothing about Home - and "appears on Home" is half of what §8 stage 10 claims.
    Three also makes the drain do its real job: one tick, three tasks, sequential.

    What each assertion is for:

      * `id >= 1e9` and `origin = 'acquired'` - §4.1's partition, which `0015_seed.sql:39-44`
        names this write path as the beneficiary of. Nothing in the tree wrote `'acquired'` before
        this milestone.
      * `placement = 'cold_tower'` with a `title_placement` row in the ACTIVE bundle's basis -
        stage 9 ran through `reconcile`'s `app_acquired` scope, which had no caller anywhere in
        the tree until now.
      * the board at stage 10, `status = 'ready'` - stage 10's state, and the one an operator
        reads.
      * the shelf - `home/shelves.py:1015-1053`, built at M4.9 with no producer because nothing
        ever stamped `origin = 'acquired'`. This is that producer.

    THE BADGE IS ASSERTED THROUGH ITS INPUTS AND NOT THROUGH A STRING, because that is where it
    lives: `shelves.py:474-484` computes `item_n` and `e_source` OUTSIDE the card's `model` block
    on purpose, so decision 117's gate cannot make the badge vanish, and `PosterCard.svelte:47-55`
    draws it "off `e_source`/`item_n`, NOT off `title.placement`". A card carrying `placement =
    'cold_tower'` with neither is a card that badges; asserting a rendered sentence here would be
    asserting the front end from the wrong side of the API.
    """
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
        assert board["detail"]["enrich"]["stub"] == "not implemented at M5.1 - owned by M5.3"

    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'admin') RETURNING id"
    )
    ctx = shelves.Ctx(
        user_id=user_id, bundle_version="test-v1", version=None, kinds=("movie",)
    )
    section, suppressed = await shelves.new_in_library(db, ctx=ctx, kind="movie")
    assert section is not None, suppressed
    assert section.title == "New in the library"
    # The why-line is compared by its two claims rather than as one literal: the shipped copy
    # carries a typographic dash, and every printable line in this package is kept to the
    # characters a Windows cp1252 console can render (CLAUDE.md).
    assert section.why.startswith("placed by the Cold Tower")
    assert "no crowd data yet" in section.why
    shown = {card["title_id"]: card for card in section.items}
    for row in rows:
        card = shown[row["id"]]
        assert card["placement"] == "cold_tower"
        assert card["item_n"] is None and card["e_source"] is None, (
            "the badge's own inputs: a title with crowd support is not 'new'"
        )


async def test_an_item_with_no_provider_id_parks_at_stage_one_and_mints_nothing(db, data_dir):
    """Decision 323, and §8 stage 1's amended clause: an item Jellyfin supplies no provider id for
    "parks here with the reason 'no provider id' and mints nothing, because a name-and-year mint
    is the wrong match the resolver already refuses".

    Measured on the corpus this resolves against: 2,438 titles share `(kind, lower(name))` and 573
    groups still collide with the year applied (`connectors/resolve.py:155-160`), so a
    name-and-year mint is a silent wrong write into a spine decision 162 makes permanent.

    NO BOARD ROW, and that is the point rather than an omission. `acquisition_job`'s primary key is
    `title_id`; there is no title, and inventing one to hang a reason on would be the mint this
    decision forbids. The reason lives on the TASK, where `queue.skip` puts it, and `skipped` is
    the state for work that is legitimately not applicable until an operator changes something.
    """
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


# --- park, resume and idempotence ----------------------------------------------------------------


def _park_at(monkeypatch, number: int, outcome: stages.Outcome):
    """Replace one shipped stage with one that parks, keeping the rest of the tuple intact.

    Stages 2 through 8 are declared no-ops at M5.1, so the only shipped parks are stage 9's and
    stage 10's - §8 stage 4's thirty-day window is M5.3's, and a park at an arbitrary stage is
    what a resume has to be asserted against. The park/resume contract is M5.1's and has to be
    asserted now, against the driver rather than against a stage: a contract discovered by M5.3 is
    a contract M5.2 was already written against.
    """
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
    """§8: "Failure at any stage parks the job with a reason, retryable from admin."

    THREE CLAIMS, and the third is the one the raw store's whole value rests on.

      * the reason is written VERBATIM. `acquisition_job.reason`'s own comment says "shown
        verbatim on the admin board" (`0005_ledger.sql:138`), so a driver that summarised,
        truncated or prefixed it would put a sentence in front of an operator that no stage wrote.
      * a park with a time defers the task to that instant and spends no attempt, and the board
        carries the same instant in `retry_after` - decision 336's "waiting on something that may
        change", which never auto-fails.
      * the resume RE-ENTERS AT THE PARKED STAGE. §8's other promise is "All fetched bytes land in
        the app's own raw store, so re-parsing is free forever" (`spec:398`), and a resume that
        restarted at stage 1 would re-fetch by construction - the promise would be cashed by
        nobody. Here that shows as stage 5 being the first stage of the second run.
    """
    until = datetime.now(UTC) + timedelta(days=30)
    reason = "thin: 1 review source, 0 words of plot; the window closes in 30 days"
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

    # The stage stops parking and the window closes. Nothing else about the world is moved by
    # hand -- see the note on moving time in `test_acquire_queue.py`.
    monkeypatch.setattr(pipeline, "STAGES", SHIPPED)
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
    """D5, which is §14 risk 5's invariant applied to the driver rather than to the derive.

    Idempotence is cheap to hold here and expensive to retrofit, and the reason it holds is worth
    naming because a later stage could break it without noticing: stage 1 resolves before it mints
    and the mint sets `jellyfin_id`, so the second run finds the first run's row on the resolver's
    first branch; stage 9's upsert is `ON CONFLICT (title_id, bundle_version) DO UPDATE`; stage 10
    is a status. Every one of those is a property of a statement rather than of a guard, which is
    what makes it survive a stage being rewritten.

    Re-running a task the first run COMPLETED, deliberately. That is the shape a reclaim after a
    crash between the completion and the commit takes, and it is also what an operator's retry
    will be at M5.6.
    """
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
    # `identify` runs again and `ready` runs again; the eight stages between them do not. The
    # leased `Task` this test re-runs carries the payload the LEASE handed back, which predates
    # `_remember_title`'s write, so the second run arrives knowing no title -- exactly the state a
    # worker killed between the mint and that write leaves behind. Stage 1 finds the first run's
    # row on the resolver's first branch (the mint set `jellyfin_id`), the board says 10, and the
    # driver jumps there rather than walking 2 through 9 a second time. Two stages ran and nothing
    # in the four tables moved.
    assert second.stages_run == ["identify", "ready"]
    assert dict(after) == dict(before)


async def test_a_task_whose_worker_died_resumes_at_the_stage_the_board_records(
    db, bundled, monkeypatch
):
    """The crash path, end to end through the driver rather than through the queue alone.

    `test_acquire_queue.py` proves a killed worker's lease is reclaimed; this proves what the NEXT
    worker then does with it, which is the half the driver owns: it re-enters at the stage the
    BOARD records and completes the title exactly once, with no duplicated derived row.

    The kill is simulated by expiring the lease in place. No test can wait 900 s, and `freezegun`
    cannot move `now()` inside Postgres - which is the clock every statement in `acquire/queue.py`
    reads on purpose, because the worker and the backend are separate containers.

    The board is written before the crash by the stages that advanced, so the resume point survives
    a process that never ran another line. That is the ordering argument in `_record_stop` and in
    `run_task`'s per-stage board write, asserted rather than asserted-about.
    """
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

    # A `KeyboardInterrupt` is a `BaseException` and is NOT written to the board as a stage
    # failure: the driver's `except Exception` lets it through, for the same reason `_tick`'s
    # cancellation must propagate. What is left behind is a leased row and a board at stage 7.
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
    # Scoped to this title: the bundle import placed the fixture's own titles, and a bare count
    # would be asserting how many rows that import wrote rather than how many this task did.
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", title_id
    ) == 1


async def test_the_driver_refuses_to_run_a_paid_stage_rather_than_running_it(
    db, data_dir, monkeypatch
):
    """§8: "paid stages (6) never auto-retry past the spend cap." The driver half of the seam.

    The gate's own decision is asserted without a database above; this asserts that the DRIVER
    consults it, and that it does so BEFORE calling the stage. The difference is the whole point:
    a driver that ran the stage and then asked has already spent the money, and there is no shape
    of cap that can undo a billed call.

    Stage 6 is made `implemented=True` with a callable that records having run. It must not run.
    """
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
    # A park and not a failure: a failure spends attempts, and four of them would lose the title
    # over a setting nobody has configured yet (decision 336). DEFERRED and not skipped, which is
    # review cycle 1's correction and is the same argument one step further: `queue.skip` writes
    # `skipped`, `queue.lease` claims `pending` only, and nothing in the tree moves a row out of
    # `skipped` - so the state chosen to avoid losing the title closed it on attempt one, and
    # supplying the cap would have revived nothing. [M51-CRASH-01]
    assert row["state"] == queue.PENDING
    assert row["attempts"] == 0, "a deferral hands the attempt back (decision 336)"
    assert row["next_attempt_at"] > datetime.now(UTC)
    assert row["last_error"] is None
    assert row["result_note"] == pipeline.NO_SPEND_CAP
    assert board["retry_after"] is not None, "the board shows the wait it is describing"


# --- stage 9's two refusals ----------------------------------------------------------------------


async def test_placement_parks_rather_than_raising_on_an_install_with_no_bundle(db, data_dir):
    """§3.1 makes a bundle-less install legal, and `title_placement.bundle_version` is `NOT NULL
    REFERENCES artifact_bundle(version)` (`0008_placement.sql:14`), so an acquired title on such
    an install genuinely cannot be placed.

    PARK, DO NOT RAISE. A raise here would be `failed` under decision 336 - "this stage raised and
    will raise again" - and would spend the title's attempts against a state the household is
    entitled to be in, so the title would be closed by `max_attempts` before anyone imported a
    bundle. Importing a bundle is the thing that may change, and it is a thing a person does.

    AND THE PARK CARRIES A DEADLINE, which is review cycle 1's correction and is asserted here
    rather than left to the reason string to promise. A park with no time is `queue.skip`, and
    nothing in this tree moves a row out of `skipped`: no lease, no reclaim, no sweep, and
    `queue.enqueue`'s `ON CONFLICT DO NOTHING` refuses to revive the key. So the state chosen to
    protect the title closed it on attempt one, while the reason shown verbatim on §6.6's board
    told the household "Import a bundle from Admin and this title is placed on the next drain".
    [M51-CRASH-01, M51-REV-03]

    The mint still happens, which is the second half of the claim: stage 1 does not need a bundle,
    so the household's new film is recorded and placed later rather than refused at the door.
    """
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
    """The half the test above was missing: the household does what the reason tells them to.

    A reason shown verbatim on §6.6's board is a promise about what the machine will do, and the
    only way to hold a promise like that is to make the machine do it in a test. So: park on a
    bundle-less install, import a bundle, put the clock forward past the deferral the park wrote,
    and drain. The title must reach `ready`.

    THE DEFERRAL IS STEPPED OVER RATHER THAN WAITED OUT. `queue.defer` writes an instant into
    `next_attempt_at` and `queue.lease` claims `next_attempt_at <= now()`, so a test that did not
    move that column would prove only that a day is longer than a test run - and would pass just
    as happily against a task that had been skipped. Moving it is the same manoeuvre
    `test_acquire_queue.py` uses for the same reason. [M51-CRASH-01, M51-REV-03]
    """
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
    """`placement/reconcile._park_thin` is the other writer of `acquisition_job` in this tree, and
    its insert is `ON CONFLICT (title_id) DO NOTHING` with its own reason: "a title already moving
    through the pipeline must not be dragged back to stage 2 by a nightly sweep"
    (`reconcile.py:250-254`, `:368-376`).

    That arrangement is asserted here rather than assumed, and asserted WITHOUT editing
    `reconcile.py`: §5.3's sweep is run for real, and two thin acquired titles go into it.

      * A is held by the driver at stage 5. Its board row must not move.
      * B is not held: it has no board row at all. It must GET one, at stage 2.

    B is the control, and it is what stops this test passing for the wrong reason. `DO NOTHING`
    also does nothing when the sweep never reaches the title, when nothing is thin, and when
    `_park_thin` is never called - three ways for a green assertion about A to mean nothing. B
    proves the same sweep, in the same call, wrote a board row it was entitled to write.

    Both are genuinely thin: stages 2 and 3 are declared no-ops at M5.1, so an acquired title has
    no genres, keywords, credits or DNA rows and drops those blocks.

    THE DRIVER MUST NEVER RELY ON THE SWEEP TO ADVANCE ANYTHING, which is the converse and the
    reason this matters: the sweep's insert is a no-op on every row this pipeline has written, so
    work left for it would be left for ever.
    """
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
    """Decision 336: the `(stage = 2, status = 'parked')` rows `_park_thin` has been writing since
    M4.13 "are the pipeline's inbox, not a backlog of failures".

    `acquisition_job` is not empty on a real install and never has been - §5.3 says thin titles are
    "placed, badged, and parked as acquisition jobs for M5 enrichment" - and until this milestone
    nothing drained them. A task enqueued against such a title re-enters at the stage the board
    records, which is 2, and not at 1: there is nothing to identify about a title that already
    exists, and stage 1 says so rather than resolving a Jellyfin item it was never given.
    """
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
    # One row for this title, still: the driver UPDATEs the inbox row rather than racing a second
    # one beside it. Scoped to the title because the bundle import parked the fixture's own thin
    # titles, and a bare count would be asserting that import's arithmetic rather than this walk's.
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", thin
    ) == 1


# --- a stage that raises --------------------------------------------------------------------------


async def test_a_stage_that_raises_is_failed_and_the_board_says_whether_it_will_retry(
    db, data_dir, monkeypatch
):
    """Decision 336: `failed` is "a stage that raised and will raise again" and is the only state
    offering a plain retry. An unhandled exception is exactly that sentence, so the driver writes
    it rather than letting the task die leased.

    The board also carries whether a retry is coming, which is the one thing an operator cannot
    infer from `failed` itself. It is computed from the leased task's own `attempts` rather than
    read back from `queue.fail`: the queue computes that branch in SQL against the row - one
    statement, so two drains failing one task cannot both read one counter - and attempts are
    counted on the CLAIM, so the number is already in hand.
    """
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


# --- the seams the stage contract has to keep ----------------------------------------------------


async def test_stage_one_never_mints_a_second_row_for_a_title_the_resolver_can_find(db, data_dir):
    """Fill-never-clobber, from the driver's side. §7.1's resolver is the one implementation of
    identity in this app and stage 1 calls it FIRST, always.

    A second implementation here would disagree with M4.11's nightly sweep about which title a
    library item is, and the two disagreeing is how a household ends up with two rows for one film
    in a spine decision 162 makes unrewritable: "a corrupted write into the content spine can only
    be undone by dropping the database".

    The bundle title carries `tt0113277`; the item does too, under a Jellyfin item id the title
    has never seen. The resolver matches on the provider id, so nothing is minted and nothing
    about the existing row is rewritten - `identify` is a READ of `title` and never a write to one.
    """
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
    """§4.1's partition, asserted in the direction that catches its defeat.

    `0015_seed.sql:20-25`: "A disjoint range makes the collision arithmetically impossible instead
    of contingent on the corpus standing still", and `:39-44` names this write path as the
    beneficiary - "this is the backstop for every other write path, including §8 stage 1". The
    backstop is the sequence's `MINVALUE`, which nothing in this module sets; what this module
    owes is to be LOUD when it has been defeated, because a title minted into the corpus's half is
    a collision the next bundle import cannot resolve and decision 162 cannot undo.

    The defeat is reproduced the way it would actually happen - a repair script that reset the
    column default - rather than by patching the assertion's input, and the refusal is checked to
    have left no row behind: the mint and its assertion share one transaction for exactly that.
    """
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
    """`UNIQUE (kind, key)` is only worth having if both enqueuers spell the key the same way.

    M4.11's nightly sweep and §8.4's flywheel will both enqueue the same title at different
    cadences and neither can know what the other has done, which is the no-op `queue.enqueue`
    exists for. A second enqueuer that built its key its own way would defeat it silently - two
    tasks, two walks of the pipeline, two writers on one title.

    The item id first because it is the household's own identity for the thing and the first
    branch `resolve.resolve_title_id` tries; a provider id as the fallback, so the flywheel can
    enqueue work for something Jellyfin has never shown us. Prefixed, because a bare `tt0113277`
    and a bare `500001` in one column is a key space where two namespaces can collide.
    """
    assert pipeline.key_for_item(MOVIE) == "jellyfin:jf-acq-1"
    assert pipeline.key_for_item(dict(MOVIE, Id="")) == "imdb:tt5000001"
    assert pipeline.key_for_item({"ProviderIds": {"Tmdb": "500002"}}) == "tmdb:500002"
    with pytest.raises(ValueError, match="neither a Jellyfin id nor a provider id"):
        pipeline.key_for_item({"Name": "nothing identifies me"})

    assert await pipeline.enqueue_item(db, MOVIE) is True
    assert await pipeline.enqueue_item(db, MOVIE) is False
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 1


def test_the_stage_contract_is_a_return_value_and_not_an_exception():
    """The one named change from the corpus's handler contract, kept honest.

    `mdc/runner.py:161-205` catches five exception types to decide what to tell the queue, which
    works when every handler is one HTTP fetch and its failure modes are the fetcher's. §8's
    stages fail in ways that are not exceptional at all - "if thin, retry window 30 days" is the
    normal outcome of stage 4 on a film released last week - and a normal outcome raised as an
    exception is one a later `except Exception` swallows into a failure.

    So the three verbs are values, and this asserts the shape every one of M5.2 through M5.7 will
    write against: `until` only on a park, and `title_id` carried out of band because stage 1 is
    the only stage that can establish one.
    """
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


def test_this_module_does_not_reach_for_the_fetcher():
    """Nothing fetches at M5.1, so the driver must not import the layer that does.

    Stages 2 and 3 are M5.3's, and `acquire/fetch.py` ships here for them rather than for this
    file. An import of it would make the driver depend on a module it does not use - and, worse,
    would make a later reader believe a stage of this pipeline fetches, which is the one thing the
    raw store exists to make unnecessary on a re-parse (`spec:398`).
    """
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    driver = Path(stages.__file__).read_text(encoding="utf-8")
    # The guard sees something: both files really do import from the package it is scanning.
    assert "from spielplan.acquire import queue" in source
    assert "from spielplan.connectors import resolve" in driver
    for text, name in ((source, "pipeline.py"), (driver, "stages.py")):
        assert "acquire.fetch" not in text, f"{name} imports the fetcher"
        assert "acquire import fetch" not in text, f"{name} imports the fetcher"
        assert "import httpx" not in text, f"{name} acquired an HTTP client"


# --- review cycle 1: the seams M5.2 through M5.7 are written against -----------------------------
#
# Every test below reddens against the code as this milestone first shipped it. They are grouped
# here rather than filed among the tests above because they share one subject: the driver is the
# bridge between a queue with no foreign key and a board whose primary key IS one, and it is the
# only thing in the tree that crosses between them. Eighteen stage bodies are still to be written
# against this shape, so what the driver does with a stage that misbehaves is a published contract
# and not an implementation detail.


async def test_every_stage_declared_a_no_op_returns_its_stub_marker():
    """`Stage.implemented` is a hand-written literal, and this is what ties it to reality.

    The spend gate reads that flag and nothing else: `refuse_uncapped_spend` refuses a paid stage
    only when `implemented` is True. So the day M5.5 writes the real LLM call into
    `stages.dna_extract` and does not also edit a boolean in `pipeline.STAGES`, the gate returns
    None, the driver calls the stage, and the household is billed with no cap in existence - while
    `ruff` is clean and the suite is green, because the two assertions that read the flag
    (`test_stage_six_is_the_only_paid_stage...`) are satisfied precisely by leaving it False.

    The refusal's own docstring used to claim that "neither milestone has to remember, because
    neither can forget". Nothing made that true. This does: a declared no-op is a function whose
    whole body is `advance({"stub": NOT_IMPLEMENTED.format(owner)})`, so giving one a body stops
    it returning that marker and reddens the build AT THE STAGE THAT GOT ONE. The only way to
    green it again is to set `implemented=True`, which is the moment the gate starts firing.

    No database and no context: a stub reads nothing, which is the property being asserted.
    [M5.1 review cycle 1, M51-REV-04, M51-REV-PAID-01]

    AND A BODY THAT RAISES SAYS THE SAME SENTENCE, because the paragraph above promised a message
    naming the stage and delivered one only for a body that RETURNS. A real stage body's first
    line touches the context - `await ctx.conn.fetchval(...)`, `ctx.task.payload` - so the likely
    shape of M5.5's mistake reddened this test with `AttributeError: 'NoneType' object has no
    attribute 'fetchval'`, which names neither the stage nor the flag and reads like a fixture
    that needs a connection. The build still goes red either way; what was missing is the sentence
    that says why, and a maintainer who reads "the test needs a context" is one step from giving
    it one. [M5.1 review cycle 4, M51-C4-PAID-03]
    """
    ctx = stages.StageContext(conn=None, task=None)
    stubs = [s for s in pipeline.STAGES if not s.implemented]
    assert len(stubs) == 7, "stages 2-8 are the declared no-ops (D3)"
    stale = (
        "is declared `implemented=False` and no longer returns the stub marker - it has a body, "
        "and the flag the spend gate reads is stale"
    )
    for stage in stubs:
        try:
            outcome = await stage.run(ctx)
        except Exception as exc:                                         # noqa: BLE001
            raise AssertionError(
                f"stage {stage.number} ({stage.name}) {stale}: it raised "
                f"{type(exc).__name__}: {exc} on an empty context, which a stub cannot do"
            ) from exc
        assert outcome.verb == stages.ADVANCE
        assert outcome.detail == {"stub": stages.NOT_IMPLEMENTED.format(stage.owner)}, (
            f"stage {stage.number} ({stage.name}) {stale}"
        )


async def test_a_malformed_provider_id_parks_at_stage_one_and_mints_nothing(db, data_dir):
    """Decision 323 says a row is minted "only on a provider id". A value is not an id.

    `resolve.identity` was built for LOOKUP, where a junk key merely fails to match; `_mint` used
    its output as the thing it WROTE and as the join key every later lookup resolves against.
    Three measured consequences, each permanent under decision 162:

      * `{"Tmdb": "0"}` - what Kodi and Emby NFO writers emit for a file they failed to scrape -
        minted a title with `tmdb_id = 0`, and every later zero-id item in the library then
        resolved onto that one row. One title standing for N different films, owned and placed.
      * `{"Tmdb": "tt0113277"}` - an imdb id filed under the wrong key - minted `tmdb_id = 113277`
        because `resolve._as_int` keeps the digits and drops the rest, so the real tmdb 113277
        resolved onto the wrong film and minted nothing of its own.
      * `{"Imdb": "   "}` survived `provider_ids`, which drops only falsy values, and became a
        title whose identity is whitespace.

    `title` carries no UNIQUE on these columns (0003 rule 6), so nothing below the driver refuses
    any of it. The park is a skip and not a deferral: an id nobody has corrected in Jellyfin will
    be exactly as malformed tomorrow. [M5.1 review cycle 1, M51-REV-05]
    """
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

    # The control, without which "mints nothing" is also what a broken stage 1 looks like: a
    # well-formed id of each kind still mints.
    good = {"Id": "jf-good-1", "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
            "ProviderIds": {"Imdb": "tt0113277", "Tmdb": "113277"}}
    minted = await pipeline.run_task(db, await _leased(db, item=good))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN
    ids = await db.fetchrow("SELECT imdb_id, tmdb_id FROM title WHERE id = $1", minted.title_id)
    assert (ids["imdb_id"], ids["tmdb_id"]) == ("tt0113277", 113277)


async def test_a_task_naming_a_title_that_is_gone_is_closed_on_the_queue_not_on_the_board(
    db, data_dir
):
    """Decision 322 gives this queue no foreign key to `title`, and the board's primary key IS one.

    The driver is the only thing that crosses between them, and it crossed without looking. A task
    whose payload names a title that is no longer there walks stage 1 - `stages.identify`
    short-circuits on a payload title id without touching the database, "there is nothing to
    identify" - and the first thing the driver then does is INSERT the stage-2 board row, which is
    a `ForeignKeyViolationError` raised from the driver's own bookkeeping, outside `_run_stage`'s
    only handler and therefore outside `run_task` and `drain` as well. The tick died, and every
    other task the batch had already leased went with it.

    `test_acquire_schema.py::test_deleting_a_title_keeps_its_task_and_takes_its_board_row` builds
    exactly this state on purpose and stops one step short of driving it. This drives it.

    The reason lives on the TASK, which has no foreign key - the same shape `run_task`'s docstring
    already argues for an item with no provider id, and for the same reason: there is no title for
    §6.6 to show a row about. [M5.1 review cycle 1, seam322-01, M51-REV-06]

    AND THE REASON NAMES ITS OWN CONSTRAINT, which is the rule `stages.NO_PROVIDER_ID` states and
    this sentence broke: "a reason shown verbatim to an operator has to name the lever that
    exists". The park is a `queue.skip`, so THIS key is closed and `queue.enqueue`'s
    `ON CONFLICT (kind, key) DO NOTHING` makes re-enqueueing the same item a no-op against it -
    measured below rather than argued, because an operator who followed the old advice would have
    watched nothing happen and had nothing telling them why.
    [M5.1 review cycle 2, M51-C2-322-05]
    """
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
    """`stages.ready` carries `fail(f"title {id} no longer exists")`, and that guard was dead.

    `_record_stop` writes the board BEFORE it writes the task, and the board write is the foreign
    key violation - so the one sentence the module wrote for this state could only ever be
    replaced by a traceback, and the task was left `leased` with the sentence recorded nowhere.
    Either the guard was needless and should have gone, or the driver had to be able to honour it.

    Driven through a stage that removes the title under the pipeline, which is what an operator
    with psql does after a mis-matched acquisition. [M5.1 review cycle 1, seam322-01]
    """
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
    """`drain` leases the whole batch before it runs any of it, and counts an attempt on the claim.

    So a raise that escaped `run_task` left every task ordered after the broken one `leased`, never
    run, and one attempt poorer - and `queue.reclaim_expired` then read that as a killed worker
    and, four cycles later, closed them all for good with a sentence saying a worker had been
    stopped. Up to `DRAIN_LIMIT` titles closed by a defect in one of them, none of the innocents
    carrying a board row, and nothing naming the cause.

    THE POISON IS A `detail` THE BOARD COULD NOT SERIALISE, which is the shape a stage-4 author
    reaches for first: `Outcome.detail` is published as `dict[str, Any]` with no serialisation
    contract anywhere in the package, and `park(until=...)` hands stage authors a datetime in the
    same breath. `write_board` dumped it with a bare `json.dumps`.
    [M5.1 review cycle 1, M51-REV-01, M51-CRASH-02]
    """
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
    """The other half of the guard above: one task's failure costs one task.

    Two ways in, because both are outside the one handler the driver had. A stage that RAISES was
    always caught; a GATE that raises was not - `_run_stage` called it one line above its own
    `try`, and `StageGate` is exactly the seam M5.5 fills with a spend cap read out of the
    database, which is an await that can fail. A cap that cannot be read is decision 336's `failed`
    ("this stage raised and will raise again"), not the end of the tick.
    [M5.1 review cycle 1, M51-CRASH-02]
    """
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
    """The driver's contract with a stage author is that a stage cannot break the tick.

    That contract stopped at the `return`. `_run_stage` turned a RAISE into `fail`, and `run_task`
    then dereferenced `outcome.detail` and `outcome.verb` outside any handler - so a stage with a
    branch that falls off the end raised `AttributeError: 'NoneType' object has no attribute
    'detail'` from the driver, left the task `leased`, froze the board at `running`, and named
    nothing. Eighteen stage bodies are still to be written against this shape, so the refusal says
    which stage and what it returned. [M5.1 review cycle 1, seam322-05]
    """
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
    """`queue.reclaim_expired` closes the task and could not close the job.

    It is the only writer for the one state decision 336's two words cannot describe - a worker
    that died, whose lease expired, whose attempts are spent - and it names one table, because
    decision 322 keeps `queue.py` free of any knowledge of titles. So §6.6's board kept whatever
    the last advance wrote, which for a worker killed mid-pipeline is `status = 'running'` with
    `reason = NULL`: an operator reading a list of titles in flight, none of which was running and
    none of which ever would be again, with the sentence explaining why written to a column the
    list view does not select.

    `_record_stop` argues the opposite trade in its own docstring - a crash should leave a row that
    OVERSTATES how stuck a job is - and `worker.py`'s `_reap_abandoned_import`, which `queue.py`
    cites as this reaper's model, exists precisely so "the Data tab would not poll a `running`
    phase for ever". The bundle import has one row, so closing the claim and closing the operator's
    view are one write; decision 322 made them two tables and only half the reaper was ported.
    [M5.1 review cycle 1, seam322-03, M51-CRASH-03]
    """
    async def dies(_ctx):
        # A BaseException, which `_run_stage` declines to catch on purpose: `_tick` bounds every
        # job with `asyncio.wait_for`, which CANCELS, and a cancellation must leave a lease for
        # the reaper rather than a board row claiming the stage failed.
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
    """The guard the close above needs, because a title can carry two tasks.

    `pipeline.enqueue_title`'s own docstring says so: the sweep's `jellyfin:<item>` task and §8.4's
    flywheel `title:<id>` task are two claims on one title, and a board row whose other task is
    still pending is a job that really is in flight. Closing it would be the inverse of the defect.
    [M5.1 review cycle 1, seam322-03]
    """
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


# --- review cycle 2: the three writes stage 1 and stage 9 could not take back ---------------------


async def test_a_title_the_cold_tower_did_not_place_waits_rather_than_being_closed(db, bundled):
    """Stage 9's second refusal, and it was the one park in the file carrying no deadline.

    `place` reads the title back because `place_titles` reports a non-finite placement per title
    and CONTINUES (`reconcile.py:298-301`), so a report with `placed > 0` does not say THIS title
    was placed. The handling it chose for that was `park(NOT_PLACED)` with no `until` - which
    `_record_stop` turns into `queue.skip`, and nothing in this tree moves a row out of `skipped`:
    no lease, no reclaim, no sweep, and `queue.enqueue`'s `ON CONFLICT DO NOTHING` refuses to
    revive the key. The task was closed for ever on a condition that clears itself, and stage 10
    reads the IDENTICAL predicate one call later and parks it WITH a deadline.

    THE PATH HERE NEEDS NO BROKEN VECTOR, which is why it is the one worth asserting: stage 1
    resolves onto an existing corpus title, `app_acquired` is `WHERE origin = 'acquired'`, so that
    title is structurally absent from the work list stage 9 just ran and the readback returns
    whatever §5.3's nightly sweep has not yet done. A Jellyfin add whose corpus title the 03:00
    sweep has not reached is exactly decision 336's "waiting on something that may change".

    So the second half changes the world the reason names - `scope="owned_missing"`, the sweep's
    own scope - and drains again. [M5.1 review cycle 2, d323-park-02]
    """
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

    # The world changes exactly as the reason says it does: §5.3's nightly reconciliation.
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


async def test_a_mint_for_an_item_jellyfin_never_showed_us_claims_no_ownership(db, bundled):
    """§7.2: `is_owned` is "re-derived from Jellyfin, never trusted stale". The mint asserted it.

    `_mint` wrote `is_owned` and `owned_checked_at` as SQL LITERALS - `true, now()` - and argued
    that "seeing the item in the library IS the derivation". That argument does not reach the path
    this same module publishes: `pipeline.key_for_item` falls back to a provider id precisely "so
    §8.4's flywheel can enqueue work for something Jellyfin has never shown us", and an item with
    no `Id` mints `jellyfin_id = NULL`. `sync/seen._falsify_ownership` is the ONE statement in the
    codebase that can un-own a title and it is scoped `WHERE is_owned AND jellyfin_id IS NOT NULL`
    (`seen.py:1145`), so no sweep can ever see that row: under decision 162 the household's spine
    would permanently claim a film it does not have, on the flag Home's shelf, §6.2's candidate
    pool and Tonight's pool all filter.

    Stage 10 then does the right thing with the honest flag, which is the second assertion: it
    parks with "no ownership flag" and a deadline rather than stamping `ready` over a title Home
    cannot show. [M5.1 review cycle 2, d323-owned-03]
    """
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
    """`_mintable_ids` asked for shape and sign and never for the one bound the column has.

    `title.tmdb_id` and `title.tvdb_id` are `integer` and `title.year` is `smallint`, and asyncpg
    binds a Python int against the column's own type. A runaway numeric id out of a mis-scraped
    NFO therefore passed the validator whose own docstring says it exists to ask "is this a value
    I am willing to make a title's permanent identity", and the refusal arrived from the database
    instead: `resolve.resolve_title_id` looks up on the same unvalidated value thirteen lines
    earlier, so stage 1 came back `DataError: invalid input for query argument $1 ... (value out
    of int32 range)` - decision 336's `failed`, which burns all four attempts re-learning the same
    thing and writes a database's internal message onto the column `0005_ledger.sql:138` says is
    "shown verbatim on the admin board", where the identical class of input one line further on is
    told exactly which field to correct in Jellyfin.

    Nothing is written either way, which is the half that was already right and is asserted so a
    repair cannot trade one defect for a breach of decision 162.
    [M5.1 review cycle 2, d323-int32-04]
    """
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

    # And an item the RESOLVER never asks about the year for - no name to fall back on - still
    # mints, with the year dropped rather than the mint raising from inside its own transaction.
    nameless = {"Id": "jf-huge-4", "Name": "", "Type": "Movie", "ProductionYear": 999999,
                "ProviderIds": {"Tmdb": "509999"}}
    minted = await pipeline.run_task(db, await _leased(db, item=nameless))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN
    row = await db.fetchrow("SELECT name, year, tmdb_id FROM title WHERE id = $1", minted.title_id)
    assert (row["name"], row["year"], row["tmdb_id"]) == ("(untitled)", None, 509999)


# --- review cycle 2: the seams two workers and a killed one share -------------------------------


async def test_two_workers_cannot_walk_one_title_at_the_same_time(db, bundled, pg_url):
    """Decision 322 gives one title two keys on purpose, and nothing stopped both from running.

    `enqueue_item` keys `jellyfin:<item>` and `enqueue_title` keys `title:<id>`; `UNIQUE (kind,
    key)` does not relate them, `acquisition_task` has no title column, and `queue.lease` filters
    on `(state, next_attempt_at, paid, kind)` with `FOR UPDATE SKIP LOCKED` - which is exactly
    what hands the two rows to two workers. `queue.lease`'s own docstring calls two loops "the
    ordinary state during a rolling restart" and this milestone ships a test for it, so the
    operating model is the code's and not this test's invention.

    THE BOARD IS NOT A LOCK, which is what `enqueue_title`'s docstring implied by saying
    "`run_task` reconciles them through the board". `_resume_index` is a `SELECT` with no
    `FOR UPDATE`, read at most twice per walk, with no transaction around the walk: it decides
    where a walk STARTS. Under concurrency both walks started at 0 and both ran every stage, which
    at M5.3 is two derives racing into a spine decision 162 makes permanently unrewritable and at
    M5.5 is two billed extractions for one title - invisible today only because every shipped
    stage is a no-op or an idempotent upsert, which is exactly why it would have shipped.

    The second connection carries the pool's own codecs, because `db/pool._init_connection`
    registers them and a bare `asyncpg.connect` does not - a walk on a codec-less connection would
    be testing asyncpg rather than the driver. [M5.1 review cycle 2, seam322-06]
    """
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

        # GATED RATHER THAN GATHERED, so the overlap is a fact and not a scheduling accident: the
        # first walk is held inside stage 10 until the second has run to completion, which is the
        # window a real drain's fetches and forward passes make wide. Two `gather`ed walks of a
        # ten-stub pipeline can finish in whichever order the sockets answer, and a test that
        # passed on that would be asserting the event loop.
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
            # BOUNDED, because the failure this asserts is a walk that proceeds rather than one
            # that raises: with no exclusion the second walk reaches the same gated stage 10 and
            # both sit there, which without a bound is a hung suite with no message rather than a
            # test that says what happened.
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
    """`run_task`'s own docstring: "the three exits, and each writes BOTH tables". There is a
    fourth, and it wrote one.

    `drain`'s `except Exception` is the catch-all review cycle 1 added for the errors that escape
    `run_task` itself - a raise in the driver's own bookkeeping rather than in a stage - and it
    called `queue.fail` and nothing else. So a task closed through it left §6.6's board
    permanently at `status = 'running'` with `reason = NULL`: the operator-facing lie
    M51-CRASH-03 was filed to remove, reached through the exit that fix did not cover.
    `close_abandoned_boards` cannot correct it either, because its EXISTS clause matches
    `last_error = queue.ABANDONED` exactly and this handler writes a different sentence - so the
    row renders as in flight on `GET /api/admin/acquisition` for ever, with no lever behind it.

    The break is planted in `write_board` rather than in a stage, because a stage raise is caught
    by `_run_stage` and never reaches this handler - which is precisely why the handler had no
    test: `test_a_task_that_breaks_the_driver_fails_alone` raises from a stage body and from a
    gate, and both of those are inside `_run_stage`'s guard. [M5.1 review cycle 2, M51-CRASH-09]
    """
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
    """The half of the reaper's question `queue.py`'s change 6 cited and did not port.

    Its model is `worker.py`'s `_reap_abandoned_import`, whose own docstring is headed "IT REPORTS
    WHAT IT READ AND NOTHING ELSE" because it once said a thing it had not checked - it now asks
    `_committed_import` whether the work actually landed before it records a failure.
    `reclaim_expired` took the closing half alone, correctly (decision 322 keeps `queue.py` free
    of any knowledge of titles), and nothing else asked.

    `run_task` writes the board `ready` and then calls `queue.complete`: two autocommit
    statements. A worker that dies between them - or one whose `_tick` budget expires between them
    - leaves a task the reaper closes `failed` with a sentence saying a worker abandoned it,
    beside a board row saying the title is ready. Both are on `GET /api/admin/acquisition/{id}`'s
    envelope today, in one response, contradicting each other, with `queue.enqueue`'s
    `ON CONFLICT DO NOTHING` refusing to revive the task and decision 330 deferring the revive
    lever to M5.6 - so the one sentence an operator could act on was about work that was done.
    [M5.1 review cycle 2, port-REAP-01]
    """
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


# --- review cycle 3: the mint, under two workers and under a value it cannot look up again -------


async def test_two_workers_cannot_mint_one_film_twice(db, bundled, pg_url):
    """The arm `test_two_workers_cannot_walk_one_title_at_the_same_time` structurally cannot reach.

    That test mints with `run_task` FIRST and then races two tasks against the title that exists,
    so both walks enter carrying a title id and meet `_TITLE_LOCK`. The window BEFORE one exists
    was covered by nothing: `_claim_title` is taken only once `stages.identify` has returned, so
    `resolve.resolve_title_id` and `_mint` ran with no mutual exclusion at all.

    ONE FILM, TWO LIBRARY ITEMS is the ordinary case and this app's own documented one:
    `connectors/resolve.upsert_item` says "a household whose libraries ship 'Movies' and
    'Movies 4K' gives one film several items", `key_for_item` keys on the item id, so those are two
    `(kind, key)` rows that `UNIQUE (kind, key)` deliberately does not relate, and `queue.lease`'s
    `FOR UPDATE SKIP LOCKED` is what hands them to two workers. Under READ COMMITTED neither walk
    can see the other's uncommitted mint, so both resolved to None and both minted - and `title`
    carries no UNIQUE on `imdb_id`, `tmdb_id` or `jellyfin_id` (§4.1 rule 6, `0003_content.sql:24`),
    so nothing below the driver refused the second row. Under decision 162 it cannot be taken back.

    GATED INSIDE THE MINT, so the overlap is a fact rather than a scheduling accident, and gated
    ONCE so that an unfixed build produces the duplicate this asserts against rather than a
    deadlock with no message. [M5.1 review cycle 3, d322-MINT-RACE-01, M51-C3-CRASH-01]
    """
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

    # And it is a lock and not a refusal: with the winner committed, the loser's own second walk
    # resolves onto the row the winner minted and mints nothing - which is the whole reason a park
    # with a deadline is the right verb here.
    again = await pipeline.drain(db)
    assert again.leased == 1 and again.ready == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1
    assert (await _task_row(db, loser.key))["state"] == queue.DONE


async def test_stage_one_refuses_an_identity_it_could_not_look_up_again(db, data_dir):
    """`identify`'s own idempotence claim: "the mint below sets `jellyfin_id`, so a second run of
    this task finds the row the first run minted on the resolver's first branch and mints nothing.
    A worker killed between the mint and the board write leaves exactly that state, and the reclaim
    is a no-op rather than a duplicate."

    It was a duplicate for any item whose identity is not already in its canonical form. `_mint`
    wrote `str(item["Id"]).strip()` and `(offered["imdb"] or "").strip()`; `resolve.resolve_title_id`
    looks the SAME item up with `str(item["Id"])` and `provider_ids`' un-stripped string. So the
    mint wrote a value the resolver can never find again, and `_remember_title` is a separate
    autocommit statement AFTER the mint's transaction commits - a window `_run_stage`'s own
    `asyncio.wait_for` budget can land in without any worker dying. Under decision 162 the second
    title is permanent.

    REFUSED RATHER THAN NORMALISED, which is decision 323's own side of the door. Canonicalising
    the item here and handing that to the resolver would make this module and the nightly sweep
    resolve the same item differently, which is the second identity implementation property 2 of
    this file's header exists to forbid. `tmdb`/`tvdb` need no refusal and get none: `resolve._as_int`
    reduces the lookup side to digits exactly as `_mintable_ids` reduces the write side, so those
    two agree already - the control below proves the rule is about the gap and not about
    whitespace. [M5.1 review cycle 3, d323-C3-MINT-01]
    """
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

    # THE CONTROL, and it is the property the paragraph above claims rather than a re-statement of
    # the refusal: a mint the resolver finds again. A `tmdb` id arrives padded and is minted,
    # because both sides of that column reduce it the same way; no `ProductionYear`, so the
    # resolver's name-and-year fallback cannot rescue either item and what is asserted is the
    # first branch.
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
    """`complete_landed_boards`'s claim that "every other disagreement between the two tables is a
    state one of them is entitled to be in" is false for exactly one pair.

    `_record_stop` writes the board and then the queue, two autocommit statements: board first, so
    that a crash between them "leaves a row that OVERSTATES how stuck the job is rather than one
    that understates it". For a park WITH a deadline it understates it, permanently. The board says
    parked at stage 9 with §3.1's reason and a date; `reclaim_expired` closes the task with
    `queue.ABANDONED`; `close_abandoned_boards` matched `running` only and `complete_landed_boards`
    matched `ready` only, so neither reached it, `queue.enqueue`'s `ON CONFLICT DO NOTHING` refuses
    to revive the key, and decision 330 defers the revive lever to M5.6. The one sentence the
    operator can act on is the false one.

    THE PARK IS KEPT IN THE SENTENCE rather than overwritten, because "import a bundle" is still
    why this walk stopped and the abandonment is what happened to it afterwards; and `retry_after`
    is cleared, because a board that still names a date is still promising the drain that will
    never come.

    A DEADLINE IS WHAT MAKES IT A CRASH MARKER, which is the second arm. Every park this pipeline
    writes with a deadline is a park whose task was `defer`red, so a terminal task beside one is a
    write that did not land; a park with NO deadline is `reconcile._park_thin`'s inbox row, which
    is the state a real install is full of and which nothing here may touch.
    [M5.1 review cycle 3, M51-C3-CRASH-02]
    """
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

    # The inbox is not a crash marker. `_park_thin` writes `(stage 2, parked)` with no deadline for
    # every thin-but-placed title, the fixture's import has just written a page of them, and an
    # abandoned task for one of those titles must leave its row exactly as it found it.
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


# --- review cycle 4: the claim set, the gate's return, and the title deleted mid-advance ---------


async def test_two_workers_cannot_mint_one_film_twice_on_disjoint_provider_ids(db, bundled, pg_url):
    """The arm `test_two_workers_cannot_mint_one_film_twice` structurally cannot reach.

    That test builds its second item as `{**four_k, "Id": ...}`, so the two items carry IDENTICAL
    ProviderIds and their claim sets are equal by construction - the fully-overlapping half of the
    race, and the only half it pins. The disjoint half is the ordinary one: a household ships
    "Movies" and "Movies 4K" (`resolve.upsert_item`'s own example) where one copy's NFO carries
    only an imdb id and the other only a tmdb id, or a series scraped by Sonarr in one library
    (tvdb) and by the TMDb plugin in another.

    `_mint_claims` built its claims from `_mintable_ids` alone, so those two items took DISJOINT
    claims, both passed `pg_try_advisory_xact_lock`, and both read nothing under READ COMMITTED -
    while `resolve.resolve_title_id` has a fourth matching branch on kind + year + name that no
    claim covered, which is exactly the branch that makes the SEQUENTIAL pair resolve onto one
    row. Measured against a scratch database with no monkeypatching: sequential one title,
    concurrent two, in 1 of 20 runs of a real `asyncio.gather` over two connections. Under
    decision 162 the second row cannot be taken back.

    GATED INSIDE THE MINT for the sibling test's reason: the overlap has to be a fact rather than
    a scheduling accident, and an unfixed build then produces the duplicate this asserts against
    rather than passing on the luck of the sockets. [M5.1 review cycle 4, d322-C4-MINT-01]
    """
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

    # And the claim is a lock and not a refusal: the loser's own second walk resolves onto the
    # winner's row through the very branch the claim now covers, and mints nothing.
    #
    # THAT HOLDS BECAUSE THIS PAIR'S NAME ARM IS UNAMBIGUOUS, and the comment above used to state
    # it as a general property of the claim. `resolve_title_id`'s fourth arm answers only for
    # exactly one candidate, so the loser resolves onto the winner only while the spine holds no
    # other title of that name and year - and the winner's mint has just added one. Where two
    # already collided, this second drain minted a second row with the lock working perfectly:
    # serialising a pair does not reconcile it. That case is
    # `test_a_name_and_year_the_spine_cannot_tell_apart_parks_rather_than_minting`, which seeds
    # the colliders this fixture deliberately has none of.
    # [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01]
    again = await pipeline.drain(db)
    assert again.leased == 1 and again.ready == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1


async def test_a_provider_id_python_cannot_parse_parks_rather_than_raising(db, data_dir):
    """`str.isdigit()` is TRUE for characters `int()` refuses, and the guard used the wrong one.

    `_mintable_ids` asked `digits.isdigit() and 0 < int(digits) <= _INT32_MAX`, which admits the
    superscripts and subscripts - `"12345\u00b2"`, a tmdb id copied out of a page carrying a
    footnote marker - and then raised `ValueError` out of the function whose whole job is to
    refuse what it will not make permanent. It raised from `_mint_claims`, BEFORE `identify` has
    taken a claim, and `_run_stage` turns a raise into decision 336's `failed`: four attempts
    spent re-learning a value only an editor can change, with a Python exception message as the
    sentence `0005_ledger.sql:138` shows verbatim on section 6.6's board.

    TWO GUARDS, BECAUSE THERE ARE TWO READERS. `isdecimal` is exactly what `int()` accepts, so the
    mint side refuses the value; the LOOKUP side is `resolve._as_int`, in a file this milestone
    does not touch (`connectors/resolve.py`), and it raises the same `ValueError` from inside
    `resolve.resolve_title_id` one line before the mint validator runs. That raise is caught where
    `DataError` already was, for the identical reason: same cause, same remedy, same person.

    The Arabic-Indic control is the half that says the fix is a narrowing and not a new refusal -
    `int()` parses those digits, so the value that minted 949 yesterday mints 949 today.
    [M5.1 review cycle 4, d323-C4-MINT-02]
    """
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
    """`_run_stage`'s contract is that a stage cannot break the tick - only its own task.

    That contract was enforced on `stage.run`'s return and not on the gate's: `return refusal` left
    the `try` three lines above the `isinstance` guard, so a gate answering with a string, a dict
    or a cap record landed on `run_task`'s own `outcome.detail` with no handler between. Through
    `drain` that is "the driver failed outside any stage", which names no stage and spends an
    attempt; through `run_task`, which every M5.2-M5.7 test and `ops/` script calls directly, the
    task is left `leased` with nothing written at all.

    AND `advance()` IS THE WORSE SHAPE, which is why it is refused rather than obeyed. It is the
    obvious reading of an `Outcome | None` signature for "the cap is fine", and it does not crash:
    the stage is SKIPPED, the walk runs on with that stage's name in its report and its body never
    called, and the board shows a clean walk. Decision 348 gives a gate two answers - None means
    run it - and this is where that is enforced instead of hoped for. M5.5 fills this seam with
    code that bills real money. [M5.1 review cycle 4, M51-C4-PAID-01]
    """
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
    """`write_board`'s "THE CALLER OWES THIS FUNCTION A LIVE TITLE", on the path that did not pay.

    `run_task` checked once, at the top, against the payload's title id; `_record_stop` checks
    before every stop write. The three advance-path writes checked nothing, and a title stage 1
    established can be deleted DURING the walk - an operator repairing a bad mint with psql, which
    `test_a_title_that_vanishes_mid_walk_is_reported_and_not_raised` already treats as the
    realistic trigger. That test's stage FAILS, so it routes through the guarded stop path; this
    one ADVANCES, which is the arm nothing covered: the next board write raised
    `ForeignKeyViolationError` out of every handler, `drain`'s outer arm closed the task with "the
    driver failed outside any stage" naming no stage, and an attempt was spent - where
    `TITLE_GONE` already names the state and tells the operator what to do about it.
    [M5.1 review cycle 4, seam322-C4-01]
    """
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


async def test_the_stub_marker_check_names_the_stage_when_a_body_raises(monkeypatch):
    """The diagnostic `refuse_uncapped_spend` cites is a published contract, so it is asserted.

    `pipeline.py`'s spend gate says its flag is held to reality by a test that makes "a stage that
    gains a body redden the build with a message naming the stage that got one". That held for a
    body that RETURNS and not for one that touches the context on its first line - which is what a
    real stage body does - where the message was `AttributeError: 'NoneType' object has no
    attribute 'fetchval'`, naming neither the stage nor the flag and reading like a fixture that
    needs a connection. The build went red either way; what was missing is the sentence saying
    why, and a maintainer who reads "the test needs a context" is one step from giving it one.
    [M5.1 review cycle 4, M51-C4-PAID-03]
    """
    async def bodied(ctx):
        return await ctx.conn.fetchval("SELECT 1")

    patched = tuple(
        pipeline.Stage(s.number, s.name, bodied, s.paid, s.implemented, s.owner)
        if s.number == 6 else s
        for s in pipeline.STAGES
    )
    monkeypatch.setattr(pipeline, "STAGES", patched)
    with pytest.raises(AssertionError) as caught:
        await test_every_stage_declared_a_no_op_returns_its_stub_marker()
    message = str(caught.value)
    assert "stage 6 (dna extract)" in message, message
    assert "the flag the spend gate reads is stale" in message, message


# --- review cycle 4, second pass: the mint's last question, and the gate's deadline --------------


async def test_a_name_and_year_the_spine_cannot_tell_apart_parks_rather_than_minting(db):
    """`resolve_title_id` returning None means two different things and stage 1 read one of them.

    Its fourth arm takes `LIMIT 2` and answers only for exactly ONE candidate, so None is "there is
    no such title" OR "there are several and I will not guess" - and the mint treated both as
    licence to write. The consequence is arithmetic rather than a race: with two pre-existing rows
    sharing `(kind, lower(name), year)`, walk A sees two candidates, gets None and mints, and walk
    B then sees THREE and mints again. Every mint adds a candidate, so the winner's commit makes
    the arm more ambiguous rather than less - which is why `_MINT_LOCK` cannot close it. The loser
    parks `FILM_IN_FLIGHT` with a deadline of now, comes back on the next tick, and mints beside
    the row it was waiting for.

    `connectors/resolve.py:155-160` measures the trigger on the corpus this resolves against -
    2,438 titles share `(kind, lower(name))` and 573 groups still collide with the year applied -
    and `pipeline.enqueue_item`'s documented input is `ResolveReport.unmatched`, which
    `resolve.py:198` appends to on exactly that refusal. The queue's input is enriched for this
    state by construction.

    THE CONTROL IS THE ARM WITH ONE COLLIDER, and it is what says this is a refusal to guess
    rather than a refusal to acquire: at one matching row the resolver answers, the walk resolves
    onto it and mints nothing, which is decision 323 working. At two it cannot answer, and decision
    360 declines to write a third row into a spine decision 162 cannot rewrite.
    [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01]
    """
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

    # And the second walk of the SAME film mints nothing either, which is the shape the double
    # mint actually took: the claim serialises the pair and the loser comes back to a spine that
    # is MORE ambiguous than the one it left.
    third = {**item, "Id": "jf-ambig-3", "ProviderIds": {"Tmdb": "700001"}}
    again = await pipeline.run_task(db, await _leased(db, item=third))
    assert again.status == "parked" and again.stage == 1, again.as_dict()
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0

    # The control at the other end: no collision at all is an ordinary acquisition, so this is not
    # a refusal of the mint.
    clean = {**item, "Id": "jf-ambig-4", "Name": "A Corridor Nobody Else Named",
             "ProviderIds": {"Imdb": "tt7000004"}}
    minted = await pipeline.run_task(db, await _leased(db, item=clean))
    assert minted.title_id is not None and minted.title_id >= stages.APP_ID_MIN, minted.as_dict()


async def test_an_original_title_the_resolver_never_probes_does_not_mint_a_second_row(db):
    """The resolver's fourth arm is DIRECTIONAL, and `_mint` writes the column it never probes.

    `resolve.py:161-179` binds the ITEM's `Name` and compares it against the candidate's `name`,
    `original_name` and aliases; the item's `OriginalTitle` is never a probe. `_mint` writes
    `original_name` from exactly that field (`stages.py`'s INSERT), so the arm answers in one
    direction and not the other - and which direction a household gets is decided by which of its
    two library copies Jellyfin returns first. A German and an English copy of one film with
    disjoint provider ids therefore produced one title in one ordering and TWO in the other, with
    no concurrency at all: their `_mint_claims` sets do not even intersect, so there is nothing for
    the claim to serialise and nothing a wider claim set could repair. A lock cannot make a
    non-matching lookup match.

    `_mint_claims`' own residue paragraph filed this under the claim discipline and justified it by
    saying the uncovered arms "are lookups into rows that already exist". That is true of
    `title_alias`, which nothing in M5.1 writes, and false of `original_name`, which this pipeline
    is the thing that creates - which is why the paragraph was corrected rather than kept.
    [M5.1 review cycle 4 second pass, M51-C4-MINT-ORIGINAL-03]
    """
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

    # The ordering that already worked: the English copy mints `original_name`, and the German
    # copy's Name matches it through the arm the resolver does probe.
    first = await pipeline.run_task(db, await _leased(db, item=english))
    assert first.title_id is not None and first.title_id >= stages.APP_ID_MIN
    second = await pipeline.run_task(db, await _leased(db, item=german))
    assert second.title_id == first.title_id, "the German copy is the same film"
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 1

    # The ordering that did not, on a clean spine: the German copy mints first, and the English
    # copy's probe - its `Name` - matches neither the row's `name` nor its NULL `original_name`.
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
    """The doctrine at the head of this module named two functions and `_mint` binds three values.

    `_year` and `_mintable_ids` were bounded by their columns in review cycle 2 under
    d323-int32-04, on the argument that "a shape check that does not ask the one question the
    column actually asks is a validator that passes the value it exists to refuse".
    `_runtime_min` is the third value `_mint` binds against a bounded column - `runtime_min
    integer`, at `$5` - and it was `int(round(int(ticks) / ...))` with no bound and no guard on the
    parse. Jellyfin declares `RunTimeTicks` as an int64; M5.2's `/events` webhook will hand this
    same dict out of a Handlebars-rendered body where a number commonly arrives as a string.

    Both shapes raise from inside `_mint`'s own transaction, outside `_resolve_or_mint`'s handler -
    which wraps the resolver call alone - so `_run_stage` records decision 336's `failed` with a
    database's or Python's internal sentence on the board column `0005_ledger.sql:138` shows
    verbatim, four attempts re-learn a value only a re-encode can change, `ON CONFLICT (kind, key)
    DO NOTHING` makes a re-enqueue a no-op, and decision 330's revive is M5.6's. The film is
    unacquirable for good over a duration.

    DROPPED AND NOT PARKED, which is `_year`'s disposal: the column is nullable and a runtime is
    not an identity, so the title is kept and the value is not.
    [M5.1 review cycle 4 second pass, M51-C4-MINT-RUNTIME-02]
    """
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
    """`_run_stage` guarded the two gate mistakes that cost a stage and let through the one that
    costs the task.

    A gate answering `stages.park("over spend cap")` is well typed and is not `advance`, so it
    passed both existing arms untouched. `_record_stop` turns a park with no `until` into
    `queue.skip`, and `stages.waiting_on_the_world`'s own docstring says what `skipped` means here:
    `lease` claims `pending` only, `defer` is fenced on pending-or-leased, neither reaper matches,
    `enqueue` is `ON CONFLICT DO NOTHING`, and decision 330's revive is M5.6's. So the first
    refusal closes the task for good while section 6.6's board shows "parked at 6" with
    `retry_after = NULL` - a wait that will never end, under a sentence promising otherwise.

    IT IS THE SPELLING M5.5 IS BEING POINTED AT. `ROADMAP-M5.md:324` words the cap refusal as
    "stage 6 parks `over spend cap` ... and never auto-retries", `stages.park` makes `until`
    keyword-optional, and in this tree a park WITH a deadline is `queue.defer` - so the roadmap's
    own prose reads as an instruction to omit the one keyword that makes the refusal survivable.
    M5.1 made this mistake twice inside its own milestone, in `refuse_uncapped_spend` and in
    `stages.place`, which is why the extension point it publishes gets the guard rather than the
    hope. [M5.1 review cycle 4 second pass, M51-C4-PAID-04]
    """
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

    # The control: the same refusal carrying the deadline decision 336 gives a park. That one is a
    # `defer` - no attempt spent, the task due again - which is what the shipped gate does today.
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
