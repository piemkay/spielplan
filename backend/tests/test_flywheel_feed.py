"""§8.4's thin-facet feed through the real driver.
Stages 2 to 7 stand down; each walk is read the moment `run_task` returns. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.acquire import pipeline, queue, stages
from spielplan.core.config import settings
from spielplan.flywheel import store
from tests.test_acquire_pipeline import SHIPPED, STANDS_DOWN, _refuse_to_crawl, _stands_down

TITLE = 7
# §4.1's minting rule: `origin = 'acquired'` and an id at or above 1e9.
ACQUIRED = 1_000_000_007


@pytest.fixture(autouse=True)
def only_the_driver_is_live(monkeypatch):
    """Stages 2 to 7 advance without doing anything, stage
    8 stays live, and nothing reaches the open web."""
    monkeypatch.setattr(pipeline, "_default_fetcher", _refuse_to_crawl)
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        _stands_down(stage) if stage.number in STANDS_DOWN else stage for stage in SHIPPED
    ))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """So stage 9's look for an active bundle finds none."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


async def _vocabulary(db) -> None:
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
    """Two providers naming one term are still one named term (decision 390 counts DISTINCT terms)."""
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
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned, origin)"
        " VALUES ($1, 'movie', 'La Jetee', 1962, true, 'acquired')", ACQUIRED,
    )
    assert await pipeline.enqueue_title(db, ACQUIRED) is True
    await _keyword(db, ACQUIRED)


async def _keyword(db, title_id: int) -> None:
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
    """`from_stage` puts the board where a later walk re-enters, since the first walk parks at stage 9."""
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


async def test_a_thin_title_is_in_the_admin_queue_the_moment_its_walk_finishes_stage_eight(db, data_dir):
    """No drain, job or sweep in between: a sweep is exactly what passes a naive test."""
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
    """The observation belongs to stage 8's FINISH; a feed written on any walk would be a sweep."""
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
    """Presence is the whole test (decision 329); no vocabulary observes nothing."""
    await _title(db)
    await _walk(db)
    assert await store.queue(db) == [], "no vocabulary, no measurement"

    await _vocabulary(db)
    for term in ("mood.bleak", "sound.silence", "themes.revenge"):
        await _tag(db, term)
    await _walk(db, from_stage=8)
    assert await store.queue(db) == []


async def test_a_second_walk_refreshes_the_titles_one_open_row_and_never_adds_one(db, data_dir):
    """0029's partial unique index is the arbiter; `created_at`
    stays, since "queued" is when first found thin."""
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
    """Decision 162: a bundle title's projected tier is seeded once and nothing can restore it."""
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
    assert [s.number for s in SHIPPED if s.observes_coverage] == [8]
    project = next(s for s in SHIPPED if s.observes_coverage)
    assert project.implemented and project.owner == "M5.4"
    assert project.run is stages.project


async def test_the_thin_facet_row_lands_after_stage_eights_projection(db, data_dir):
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
