"""§8.4's thin-facet feed through the real driver, and the two writers M6 will call.

Spec v2.1 §8.4 ("The enqueue is immediate and visible ... A title is thin-facet when at least one
facet the active vocabulary declares carries no extracted-tier term for it ... and its row is
written the moment its walk finishes stage 8"), §6.4, §6.6 Data; decisions 328, 329, 330, 344, 440.

The gate row's `what` is the instrument: a naming failure "appends a flywheel row carrying its reason
at the moment it happens and is readable from the admin queue immediately, not after a nightly
job". So every walk below is `pipeline.run_task` on a leased task, exactly as a drain tick makes
it, and the queue is read with `store.queue` - the admin queue's own read - the moment it returns.
No drain loop, no worker job and no sweep runs in between, because a sweep over the extracted tier
is precisely the implementation that passes a naive test and fails the row (plan §9).

STAGES 2 TO 7 STAND DOWN, as `test_acquire_pipeline.py` stands them down and for its reason: the
subject is the driver's observation, not the crawl, the gate, the pack or the bill, and a walk that
had to supply a canned web and a spend cap would redden for those. Stage 8 is the shipped one,
carrying `observes_coverage` and, since M5, its body (decision 463): title 7 is a bundle title
(`title.origin`'s default), so it takes the branch that leaves the bundle's projected tier alone,
and the acquired title below takes the one that projects. Stage 9 parks for want of a bundle, which
is where a walk past the observation stops on this database.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from spielplan.acquire import pipeline, queue, stages
from spielplan.core.config import settings
from spielplan.flywheel import store
from tests.test_acquire_pipeline import SHIPPED, STANDS_DOWN, _refuse_to_crawl, _stands_down

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"

TITLE = 7
# §4.1's minting rule - `origin = 'acquired'` and an id at or above 1e9 (`0008_placement.sql:46-48`)
# - which is the population stage 8 projects (decision 463).
ACQUIRED = 1_000_000_007
M6_WRITERS = ("enqueue_empty_predicate", "enqueue_uncovered_frontier")


@pytest.fixture(autouse=True)
def only_the_driver_is_live(monkeypatch):
    """`test_acquire_pipeline.py`'s stand-down, restated as a fixture of this file's own so a reader
    sees it: 2 to 7 advance without doing anything - 5 and 7 since M5 gave them bodies (decisions
    461, 462) - stage 8 stays live, and nothing can reach the open web."""
    monkeypatch.setattr(pipeline, "_default_fetcher", _refuse_to_crawl)
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        _stands_down(stage) if stage.number in STANDS_DOWN else stage for stage in SHIPPED
    ))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR` under this test's tmp_path, so stage 9's look for an active bundle finds none."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


async def _vocabulary(db) -> None:
    """Three declared facets - the eleven of v1 at a size a failure message can print."""
    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 3, 5)")
    await db.execute("INSERT INTO dna_facet (version, facet, ord) VALUES "
                     "('v1', 'mood', 0), ('v1', 'sound', 1), ('v1', 'themes', 2)")
    await db.execute(
        "INSERT INTO dna_term (version, term, facet, gloss) VALUES "
        "('v1', 'mood.bleak', 'mood', NULL), ('v1', 'sound.silence', 'sound', NULL), "
        "('v1', 'themes.robots', 'themes', NULL), ('v1', 'themes.gladiatorial', 'themes', NULL), "
        "('v1', 'themes.revenge', 'themes', NULL)"
    )


async def _tag(db, term: str, *, provider: str = "gemini") -> None:
    """One extracted-tier row. Two providers naming one term are still one named term (decision
    390 counts DISTINCT terms), which the thin test must not read as two."""
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES ($1, 'v1', $2, split_part($2, '.', 1), 2, $3)",
        TITLE, term, provider,
    )


async def _title(db) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', 'Grey Harbour', 2021,"
        " true)", TITLE,
    )
    assert await pipeline.enqueue_title(db, TITLE) is True


async def _acquired(db) -> None:
    """A title this pipeline minted, with its task, carrying one keyword the alias map below names."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, origin)"
        " VALUES ($1, 'movie', 'La Jetee', 1962, true, 'acquired')", ACQUIRED,
    )
    assert await pipeline.enqueue_title(db, ACQUIRED) is True
    await _keyword(db, ACQUIRED)


async def _keyword(db, title_id: int) -> None:
    """An alias v1 adopts, and a TMDB keyword of the title's that folds onto it (`alias_key`)."""
    await db.execute(
        "INSERT INTO dna_alias (version, alias, term) VALUES ('v1', 'revenge', 'themes.revenge')"
        " ON CONFLICT DO NOTHING"
    )
    await db.execute(
        "INSERT INTO title_keyword (title_id, keyword, source) VALUES ($1, 'Revenge', 'tmdb')", title_id
    )


async def _board_detail(db, title_id: int) -> dict:
    return await db.fetchval("SELECT detail FROM acquisition_job WHERE title_id = $1", title_id)


async def _walk(db, *, from_stage: int | None = None, title_id: int = TITLE) -> pipeline.TaskReport:
    """One walk of the title's task, leased the way a drain tick leases it. `from_stage` puts the
    board where a later walk re-enters - a board retry or a launch does the same - since the first
    walk parks at stage 9 and a walk resumes at the board's stage."""
    if from_stage is not None:
        await pipeline.write_board(db, title_id, stage=from_stage, status=pipeline.RUNNING)
    await db.execute(
        "UPDATE acquisition_task SET state = 'pending', next_attempt_at = now() WHERE key = $1",
        f"title:{title_id}",
    )
    (task,) = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    return await pipeline.run_task(db, task)


async def _thin_rows(db) -> list:
    return await db.fetch(
        "SELECT id, status, reason, created_at, batch_id FROM flywheel_item WHERE kind = 'thin_facet'"
        " ORDER BY id"
    )


# --- the thin-facet feed ------------------------------------------------------------------------


async def test_a_thin_title_is_in_the_admin_queue_the_moment_its_walk_finishes_stage_eight(db, data_dir):
    """Decision 440, measured from outside the driver. The title names mood and themes and no sound
    term, so one declared facet is unnamed (decision 329); the walk's `run_task` returns and the
    admin queue already holds the title's row, its reason naming the facet and the count out of the
    facets declared - with no drain, no job and no sweep in between."""
    await _vocabulary(db)
    await _title(db)
    await _tag(db, "mood.bleak")
    await _tag(db, "themes.robots")
    await _tag(db, "themes.robots", provider="anthropic")

    walk = await _walk(db)

    assert "project" in walk.stages_run and walk.stage == 9, walk.as_dict()
    (row,) = await store.queue(db)
    assert (row["kind"], row["title_id"], row["status"]) == (store.THIN_FACET, TITLE, "queued")
    assert row["reason"] == (
        "1 of the 3 facets vocabulary v1 declares carry no extracted-tier term for this title: sound."
        " An extraction batch asks its sources again for these, under terms the vocabulary already"
        " has"
    ), row["reason"]
    row["reason"].encode("ascii")
    assert row["detail"] == {"title_id": TITLE, "version": "v1", "unnamed": ["sound"],
                             "coverage": {"mood": 1, "sound": 0, "themes": 1}}, row["detail"]
    assert row["est_titles"] == 1 and row["batch_id"] is None
    assert row["title"] == {"name": "Grey Harbour", "year": 2021}
    assert row["board"]["stage"] == 9 and row["board"]["status"] == pipeline.PARKED, (
        "the queue shows why the title is waiting now, off its own board row"
    )
    assert await db.fetchval("SELECT est_cost_usd FROM flywheel_item") is None, (
        "decision 441: no cost is stored at enqueue - it is stale the moment the pass count changes"
    )


async def test_a_walk_that_stops_before_stage_eight_finishes_writes_nothing(db, data_dir, monkeypatch):
    """The observation belongs to stage 8's FINISH and to nothing earlier: a walk parked at stage 7
    has not measured anything a batch could act on, and a feed written on any walk would be a
    sweep with extra steps."""
    await _vocabulary(db)
    await _title(db)

    async def parks(_ctx):
        return stages.park("verify parked by this test", until=stages.waiting_on_the_world())

    monkeypatch.setattr(pipeline, "STAGES", tuple(
        pipeline.Stage(s.number, s.name, parks, s.paid, s.implemented, s.owner) if s.number == 7 else s
        for s in pipeline.STAGES
    ))
    walk = await _walk(db)

    assert (walk.stage, walk.status) == (7, pipeline.PARKED), walk.as_dict()
    assert await store.queue(db) == []


async def test_a_title_naming_every_facet_or_an_install_with_no_vocabulary_is_not_queued(db, data_dir):
    """Presence is the whole test (decision 329): a title that names all three facets is not thin,
    whatever its counts. And an install with no active vocabulary - §3.1's legal bundle-less state -
    has nothing to measure against, so it observes nothing rather than calling every facet
    unnamed."""
    await _title(db)
    await _walk(db)
    assert await store.queue(db) == [], "no vocabulary, no measurement"

    await _vocabulary(db)
    for term in ("mood.bleak", "sound.silence", "themes.revenge"):
        await _tag(db, term)
    await _walk(db, from_stage=8)
    assert await store.queue(db) == []


async def test_a_second_walk_refreshes_the_titles_one_open_row_and_never_adds_one(db, data_dir):
    """One open row per title however often it is walked (decision 440; 0029's partial unique
    index is the arbiter). The second walk finds the title thinner - its themes term withdrawn -
    and the SAME row now says two facets, keeping its `created_at`, because "queued just now" is
    when the title was first found thin."""
    await _vocabulary(db)
    await _title(db)
    await _tag(db, "mood.bleak")
    await _tag(db, "themes.robots")
    await _walk(db)
    (first,) = await _thin_rows(db)

    await db.execute("DELETE FROM dna_tag WHERE term = 'themes.robots'")
    await _walk(db, from_stage=8)

    (again,) = await _thin_rows(db)
    assert (again["id"], again["status"], again["created_at"]) == (
        first["id"], "queued", first["created_at"]
    )
    assert again["reason"].startswith("2 of the 3 facets vocabulary v1 declares"), again["reason"]
    assert again["reason"].endswith("for this title: sound, themes. An extraction batch asks its"
                                    " sources again for these, under terms the vocabulary already has")


async def test_a_running_row_closes_done_at_the_next_finish_and_a_still_thin_title_is_queued_again(
    db, data_dir
):
    """A launched batch's row is `running` until the title's next stage-8 observation, and that
    observation is how it ends (decision 443): the walk the launch made due has been measured. A
    title still thin afterwards gets a new queued row, so the queue says what the next batch would
    buy; one that is no longer thin gets none."""
    await _vocabulary(db)
    await _title(db)
    await _tag(db, "mood.bleak")
    await _walk(db)
    (launched,) = await _thin_rows(db)
    batch = await db.fetchval(
        "INSERT INTO flywheel_batch (providers, passes, est_titles) VALUES ('{gemini}', 1, 1) RETURNING id"
    )
    await db.execute("UPDATE flywheel_item SET status = 'running', batch_id = $2 WHERE id = $1",
                     launched["id"], batch)

    await _walk(db, from_stage=8)

    rows = await _thin_rows(db)
    assert [(r["id"], r["status"], r["batch_id"]) for r in rows][0] == (launched["id"], "done", batch)
    assert [r["status"] for r in rows[1:]] == ["queued"], [dict(r) for r in rows]
    assert [r["id"] for r in await store.queue(db)] == [rows[1]["id"]]

    await db.execute("UPDATE flywheel_item SET status = 'running', batch_id = $2 WHERE id = $1",
                     rows[1]["id"], batch)
    for term in ("sound.silence", "themes.revenge"):
        await _tag(db, term)
    await _walk(db, from_stage=8)

    assert [r["status"] for r in await _thin_rows(db)] == ["done", "done"]
    assert await store.queue(db) == []


async def test_stage_eight_projects_an_acquired_titles_keywords(db, data_dir):
    """Decision 463's first branch: §8 stage 8 is "per-title alias-map projection of its keywords
    ... for an acquired title". The title's TMDB keyword folds onto an alias v1 adopts, so one
    projected row is written - its weight the count of inventories naming the term, its `via` the
    spelling that reached it - and the stage's own detail on the board says how many."""
    await _vocabulary(db)
    await _acquired(db)

    walk = await _walk(db, from_stage=8, title_id=ACQUIRED)

    assert walk.stages_run[:1] == ["project"] and walk.stage == 9, walk.as_dict()
    rows = await db.fetch(
        "SELECT version, term, facet, weight, via FROM dna_projected WHERE title_id = $1", ACQUIRED
    )
    assert [tuple(row) for row in rows] == [("v1", "themes.revenge", "themes", 1.0, "keyword:Revenge")]
    assert (await _board_detail(db, ACQUIRED))["project"]["projected"] == len(rows)


async def test_stage_eight_leaves_a_bundle_titles_projected_rows_untouched(db, data_dir):
    """Decision 463's second branch, and decision 162 behind it: a bundle title's projected tier is
    content the import seeds once and nothing can restore, and `project_title` refuses such a title
    for that reason - so stage 8 must not call it, or every bundle walk a Launch or a retry makes
    would fail after stage 6 had billed for it. Title 7 carries a projected row the bundle wrote and
    a keyword that WOULD project a second term: after the walk its rows are byte for byte what
    they were, `created_at` included, and the board names decision 162."""
    await _vocabulary(db)
    await _title(db)
    await _keyword(db, TITLE)
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES ($1, 'v1', 'mood.bleak', 'mood', 3.0, 'keyword:bleak')", TITLE,
    )
    before = [dict(row) for row in await db.fetch(
        "SELECT * FROM dna_projected WHERE title_id = $1 ORDER BY id", TITLE
    )]

    walk = await _walk(db, from_stage=8)

    assert walk.stages_run[:1] == ["project"] and walk.stage == 9, walk.as_dict()
    after = [dict(row) for row in await db.fetch(
        "SELECT * FROM dna_projected WHERE title_id = $1 ORDER BY id", TITLE
    )]
    assert after == before, "stage 8 re-derived a bundle title's projected tier"
    detail = (await _board_detail(db, TITLE))["project"]
    assert detail["origin"] == "bundle" and "decision 162" in detail["kept"], detail


def test_stage_eight_is_still_the_only_stage_that_observes():
    """Decision 440 puts the observation on a flag the stage row carries, and decision 463 gives
    the stage its body without moving it: one row carries the flag, it is the stage whose finish
    §8.4 names, and it now ships implemented with M5.4 kept as provenance."""
    assert [s.number for s in SHIPPED if s.observes_coverage] == [8]
    project = next(s for s in SHIPPED if s.observes_coverage)
    assert project.implemented and project.owner == "M5.4"
    assert project.run is stages.project


async def test_the_thin_facet_row_lands_after_stage_eights_projection(db, data_dir):
    """The observation is the driver's and follows the stage's advance (decision 440), so it fires
    on both of stage 8's branches (decision 463). An acquired thin title: when `run_task` returns,
    its projected row and its thin-facet row both exist, the projection first. Then a bundle
    title's walk, which projects nothing: its row lands just the same."""
    await _vocabulary(db)
    await _acquired(db)

    walk = await _walk(db, from_stage=8, title_id=ACQUIRED)

    assert walk.stages_run[:1] == ["project"], walk.as_dict()
    projected_at = await db.fetchval(
        "SELECT created_at FROM dna_projected WHERE title_id = $1", ACQUIRED
    )
    (row,) = await _thin_rows(db)
    assert projected_at is not None, "stage 8 projected nothing for an acquired title"
    assert projected_at <= row["created_at"], "the thin-facet row was written before the projection"
    assert (await store.queue(db))[0]["title_id"] == ACQUIRED

    await _title(db)
    walk = await _walk(db, from_stage=8)

    assert walk.stages_run[:1] == ["project"], walk.as_dict()
    assert sorted(row["title_id"] for row in await store.queue(db)) == [TITLE, ACQUIRED]


# --- the two feeds M6 produces --------------------------------------------------------------------


async def test_an_empty_predicate_is_queued_with_its_reason_as_the_spec_writes_it(db):
    """§6.4: "Empty predicates land in the flywheel with their reason ("no owned title carries robots
    + gladiatorial")" - that sentence, from the vocabulary ids the predicate names, read back out of
    the admin queue verbatim, with no title and no board because a query is not a title."""
    await _vocabulary(db)

    written = await store.enqueue_empty_predicate(
        db, query="Gladiator but with robots", predicate="has(robots) AND has(gladiatorial)",
        terms=["themes.robots", "themes.gladiatorial"],
    )

    (row,) = await store.queue(db)
    assert row["id"] == written and row["kind"] == store.EMPTY_PREDICATE
    assert row["reason"] == "no owned title carries robots + gladiatorial"
    assert row["detail"]["query"] == "Gladiator but with robots"
    assert (row["title_id"], row["title"], row["board"]) == (None, None, None)


@pytest.mark.parametrize("writer", M6_WRITERS)
async def test_a_naming_failure_the_vocabulary_cannot_name_is_not_enqueued(db, writer):
    """Decision 344: no batch can add a term (decision 163), so a failure naming one v1 does not
    carry is not a row whose only control is Spend. `themes.mecha` is not a v1 term; neither is
    anything on an install with no vocabulary, and a failure naming no term names nothing a batch
    could fix. Each writes nothing and says so with None."""
    enqueue = getattr(store, writer)
    context = {"query": "mecha gladiators", "predicate": "has(mecha)"} if writer == M6_WRITERS[0] else {
        "region": "north-east of Pacific Rim"
    }

    assert await enqueue(db, terms=["themes.robots"], **context) is None, "no vocabulary is active"
    await _vocabulary(db)
    assert await enqueue(db, terms=["themes.robots", "themes.mecha"], **context) is None
    assert await enqueue(db, terms=[], **context) is None
    assert await db.fetchval("SELECT count(*) FROM flywheel_item") == 0

    assert await enqueue(db, terms=["themes.robots"], **context) is not None, (
        "the refusal above is about the term, not a writer that writes nothing at all"
    )


def _callers(source: str, names: tuple[str, ...]) -> list[int]:
    """The lines of every CALL to one of `names`, however it is reached - never a definition."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in names:
                found.append(node.lineno)
    return found


def test_no_app_code_calls_either_writer_whose_producer_is_m6():
    """Plan B4: the writers ship and M6 calls them. A call from M5 code would be an M5 producer of a
    feed §8.4 gives to §6.4's search and frontier, which is the "invented definition" decision 328
    refused for the struck feed, one feed over."""
    assert _callers("store.enqueue_empty_predicate(conn)\nx = 1", M6_WRITERS) == [1], (
        "the scanner sees a call"
    )
    assert all(callable(getattr(store, name)) for name in M6_WRITERS)
    calling = {
        path.relative_to(PACKAGE).as_posix(): lines
        for path in sorted(PACKAGE.rglob("*.py"))
        if (lines := _callers(path.read_text(encoding="utf-8"), M6_WRITERS))
    }
    assert calling == {}, f"M5 code calls a writer whose producer is M6's: {calling}"
