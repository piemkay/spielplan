"""The metadata walk: §8 stages 2 (TMDB's two kinds) and 3 for a bundle title the corpus never
fetched TMDB for. Spec v2.1 §8, §6.0, §6.8; decisions 372, 411, 484, 499, 501 and 522.

Against the test database and a raw store of the test's own, with TMDB as an
`httpx.MockTransport` answering real captured documents (`fixtures/sources/`, `fixtures/http/`):
what the walk asks, what it lets the derive write, and what it leaves alone are all rows and
requests a test can read. Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import backfill, pipeline, queue
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.derive import ledgers
from spielplan.importer import dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARRIVAL = json.loads((FIXTURES / "sources" / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
HEAT = json.loads((FIXTURES / "http" / "tmdb_movie_detail.json").read_text(encoding="utf-8"))
KEY = "tmdb-key-not-a-real-one-0522"


class Tmdb:
    """TMDB's API as the walk meets it: what it was asked, and answers by path."""

    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.asked: list[httpx.URL] = []
        self.answers = answers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url)
        return self.answers.get(request.url.path, httpx.Response(404, json={}))

    @property
    def paths(self) -> list[str]:
        return [url.path for url in self.asked]


def _found(tmdb_id: int) -> httpx.Response:
    return httpx.Response(200, json={"movie_results": [{"id": tmdb_id}], "tv_results": []})


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it
    (`test_derive_rebuild.py`'s fixture, for its reason)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


@pytest.fixture
async def keyed(db, secrets_key, raw_root):
    await registry.save_connector(db, "tmdb", api_key=KEY)
    return db


async def _title(db, title_id: int, *, kind: str = "movie", tmdb_id=None, imdb_id=None,
                 placement: str = "unplaced", origin: str = "bundle", owned: bool = False,
                 name: str | None = None, **columns) -> None:
    # A placed title names the basis it was placed in (0023's `title_placement_has_basis`).
    basis = None
    if placement != "unplaced":
        basis = "v-meta"
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state, kind) "
            "VALUES ($1, '{}'::jsonb, 'active', 'seed') ON CONFLICT DO NOTHING", basis,
        )
    extra = ", ".join(columns)
    marks = ", ".join(f"${i}" for i in range(10, 10 + len(columns)))
    await db.execute(
        "INSERT INTO title (id, kind, name, tmdb_id, imdb_id, placement, placement_bundle, origin,"
        f" is_owned{', ' + extra if extra else ''})"
        f" VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9{', ' + marks if marks else ''})",
        title_id, kind, name or f"Title {title_id}", tmdb_id, imdb_id, placement, basis, origin,
        owned, *columns.values(),
    )


async def _walk(db, tmdb: Tmdb, **kwargs):
    return await backfill.walk(db, transport=httpx.MockTransport(tmdb), **kwargs)


async def _task(db, title_id: int):
    return await db.fetchrow(
        "SELECT state, attempts, next_attempt_at, result_note, last_error FROM acquisition_task"
        " WHERE kind = $1 AND key = $2", backfill.KIND, backfill.key_for(title_id),
    )


# --- the card arrives through the derive --------------------------------------------------------


async def test_a_skeleton_bundle_title_gets_tmdb_s_text_and_poster_through_stage_three(keyed):
    """Moulin Rouge (1952)'s state at the second household test: an imdb id, an MPST synopsis
    decision 499 keeps off the card, no overview and no poster. TMDB is asked twice - resolve,
    then the detail - and the card is what §8 stage 3's resolution makes of the answer."""
    await _title(keyed, 20, imdb_id="tt2543164", placement="warm", year=2016)
    await keyed.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES (20, 'mpst', $1)",
        {"plot_full": "The whole film, retold with its ending."},
    )
    tmdb = Tmdb({"/3/find/tt2543164": _found(329865),
                 "/3/movie/329865": httpx.Response(200, json=ARRIVAL)})

    report = await _walk(keyed, tmdb)

    assert tmdb.paths == ["/3/find/tt2543164", "/3/movie/329865"]
    assert all(url.host == "api.themoviedb.org" for url in tmdb.asked)
    assert report["derived"] == 1 and report["filed"] == 1
    row = await keyed.fetchrow(
        "SELECT overview, tagline, poster_path, tmdb_id FROM title WHERE id = 20")
    assert row["overview"] == ARRIVAL["overview"]
    assert row["tagline"] == ARRIVAL["tagline"]
    assert row["poster_path"] == f"https://image.tmdb.org/t/p/w500{ARRIVAL['poster_path']}"
    assert row["tmdb_id"] == 329865
    assert await keyed.fetchval(
        "SELECT count(*) FROM title_meta WHERE title_id = 20 AND source = 'tmdb'") == 1
    assert (await _task(keyed, 20))["state"] == queue.DONE
    # And it is done: a second run files nothing for it and asks nothing.
    again = Tmdb()
    assert await _walk(keyed, again) is None
    assert again.asked == []


async def test_a_field_the_title_holds_is_replaced_only_by_tmdb_s_own_value(keyed):
    """Decision 522's condition, "never overwrites a better existing field". The year and the
    runtime the title carries are filled and never overwritten (they differ from TMDB's here), the
    overview TMDB leads the corpus's order for is TMDB's, and a field TMDB answers empty keeps the
    value another source gave it - a TVmaze poster stays when TMDB has none."""
    await _title(keyed, 21, imdb_id="tt2543164", placement="warm", year=2015, runtime_min=111)
    tvmaze = "https://static.tvmaze.com/uploads/images/medium_portrait/1/2.jpg"
    await keyed.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES (21, 'omdb', $1),"
        " (21, 'tvmaze', $2)",
        {"plot_full": "OMDb's plot."}, {"poster_url": tvmaze},
    )
    await keyed.execute(
        "UPDATE title SET overview = 'OMDb''s plot.', poster_path = $1 WHERE id = 21", tvmaze)
    answer = {**ARRIVAL, "poster_path": None, "backdrop_path": None}
    tmdb = Tmdb({"/3/find/tt2543164": _found(329865),
                 "/3/movie/329865": httpx.Response(200, json=answer)})

    await _walk(keyed, tmdb)

    row = await keyed.fetchrow(
        "SELECT year, runtime_min, overview, poster_path FROM title WHERE id = 21")
    assert (row["year"], row["runtime_min"]) == (2015, 111)
    assert row["overview"] == ARRIVAL["overview"]
    assert row["poster_path"] == tvmaze


async def test_the_walk_leaves_the_extracted_dna_tier_alone(keyed, tmp_path):
    """The owner's second condition. The bundle's shipped per-title verdict drops `mood.cosy` on
    title 1 whenever the adjudication ledger is applied (`test_derive_ledgers.py`), and §8 stage 3
    applies it; this walk's derive does not, so the tag and its quote survive byte for byte - and
    the ledger is shown live afterwards, so the survival is not a vacuous one."""
    fx.make_bundle(tmp_path / "bundle")
    vocab = tmp_path / "bundle" / "artifacts" / "dna_vocab" / "v1"
    imported = ImportReport()
    await dna.load_vocabulary(keyed, vocab, "v1", imported)
    await dna.load_adjudications(keyed, vocab, "v1", imported)
    assert imported.ok, imported.render()
    await _title(keyed, 1, tmdb_id=949, placement="warm", name="Heat")
    tag = await keyed.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, n_sources, provider)"
        " VALUES (1, 'v1', 'mood.cosy', 'mood', 2, 1, '') RETURNING id")
    await keyed.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a quote', 'trakt:comment')",
        tag)
    tier = "SELECT t.term, t.salience, e.quote FROM dna_tag t JOIN dna_evidence e ON e.dna_tag_id = t.id"
    before = [tuple(r) for r in await keyed.fetch(tier + " WHERE t.title_id = 1")]
    tmdb = Tmdb({"/3/movie/949": httpx.Response(200, json=HEAT)})

    report = await _walk(keyed, tmdb)

    assert report["derived"] == 1
    assert [tuple(r) for r in await keyed.fetch(tier + " WHERE t.title_id = 1")] == before
    assert (await ledgers.apply_adjudications(keyed, 1))["dropped"] == 1


# --- who first, and who not at all ----------------------------------------------------------------


async def test_owned_then_seen_or_rated_then_the_seed_list_then_placed_then_the_rest(keyed):
    """The owner's order, as the filing priority `queue.lease` sorts by. A title with TMDB's block
    already, an acquired title and a title with no id to ask by are not filed at all."""
    await _title(keyed, 31, imdb_id="tt0000031")                                   # the rest
    await _title(keyed, 32, imdb_id="tt0000032", placement="warm")                 # placed, warm
    await _title(keyed, 33, imdb_id="tt0000033", placement="cold_tower")           # placed
    await _title(keyed, 34, imdb_id="tt0000034")                                   # seed list
    await keyed.execute("INSERT INTO seed_list (position, title_id) VALUES (1, 34)")
    await _title(keyed, 35, imdb_id="tt0000035")                                   # rated
    user = await keyed.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('member', 'member') RETURNING id")
    await keyed.execute("INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 35, 2)", user)
    await _title(keyed, 36, imdb_id="tt0000036")                                   # seen
    await keyed.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 36, 'seen')", user)
    await _title(keyed, 37, imdb_id="tt0000037", placement="cold_tower", owned=True)  # owned
    await _title(keyed, 38, imdb_id="tt0000038")
    await keyed.execute("INSERT INTO title_meta (title_id, source, payload) VALUES (38, 'tmdb', '{}')")
    await _title(keyed, 39, imdb_id="tt0000039", origin="acquired")
    await _title(keyed, 40)

    assert await backfill.file_candidates(keyed, room=100) == 7
    order = [r["key"] for r in await keyed.fetch(
        "SELECT key FROM acquisition_task WHERE kind = $1 ORDER BY priority, id", backfill.KIND)]
    assert order == [backfill.key_for(i) for i in (37, 35, 36, 34, 32, 33, 31)]
    assert await backfill.file_candidates(keyed, room=100) == 0, "a filed title was filed twice"


async def test_the_drain_never_leases_the_walk_s_work(keyed):
    """Its own queue kind: `pipeline.drain` leases `acquire` tasks and the board's actions revive
    them, so neither can take a title into the ten-stage walk this one exists to stay out of."""
    await _title(keyed, 41, imdb_id="tt0000041", placement="warm")
    await backfill.file_candidates(keyed, room=10)
    assert await queue.lease(keyed, [pipeline.TASK_KIND], limit=10) == []


# --- what TMDB says, and what the walk does with it -----------------------------------------------


async def test_tmdb_holding_no_record_is_asked_again_after_thirty_days(keyed):
    await _title(keyed, 50, imdb_id="tt0000050", placement="warm")
    tmdb = Tmdb({"/3/find/tt0000050": httpx.Response(200, json={"movie_results": []})})

    report = await _walk(keyed, tmdb)

    assert report["none"] == 1
    task = await _task(keyed, 50)
    assert task["state"] == queue.PENDING and task["attempts"] == 0
    assert task["next_attempt_at"] > datetime.now(UTC) + timedelta(days=29)
    assert "no record" in task["result_note"]
    again = Tmdb()
    assert await _walk(keyed, again) is None
    assert again.asked == []


async def test_a_refused_key_stops_the_walk_and_records_nothing_against_the_titles(keyed):
    await _title(keyed, 51, tmdb_id=51, placement="warm")
    await _title(keyed, 52, tmdb_id=52, placement="warm")
    tmdb = Tmdb({"/3/movie/51": httpx.Response(401, json={"status_message": "Invalid API key"})})

    report = await _walk(keyed, tmdb)

    assert "refused" in report["stopped"]
    assert tmdb.paths == ["/3/movie/51"], "the walk asked on after TMDB refused the key"
    for title_id in (51, 52):
        task = await _task(keyed, title_id)
        assert (task["state"], task["attempts"]) == (queue.PENDING, 0)
    assert await keyed.fetchval("SELECT count(*) FROM title_meta") == 0


async def test_no_key_no_walk(db, raw_root):
    await _title(db, 53, imdb_id="tt0000053", placement="warm")
    tmdb = Tmdb()
    assert await _walk(db, tmdb) is None
    assert tmdb.asked == []
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 0


async def test_an_id_another_title_already_carries_is_given_back(keyed):
    """The corpus's split rows: an imdb-only row and a tmdb-only row for one film. TMDB answers the
    first with the second's id, and the card is rightly the same film's - but a second holder of
    one id makes the resolver's provider arms answer arbitrarily, so the id the walk filled goes
    back to NULL. The same for an imdb id `tmdb:detail` offers that another row already holds."""
    await _title(keyed, 60, tmdb_id=329865, placement="warm", name="Arrival")
    await keyed.execute("INSERT INTO title_meta (title_id, source, payload) VALUES (60, 'tmdb', '{}')")
    await _title(keyed, 61, imdb_id="tt2543164", placement="warm", name="Arrival")
    await _title(keyed, 62, imdb_id="tt0113277", name="Heat", origin="acquired")
    await _title(keyed, 63, tmdb_id=949, placement="warm", name="Heat")
    tmdb = Tmdb({
        "/3/find/tt2543164": _found(329865),
        "/3/movie/329865": httpx.Response(200, json=ARRIVAL),
        "/3/movie/949": httpx.Response(200, json=HEAT),
    })

    report = await _walk(keyed, tmdb)

    assert report["derived"] == 2
    split = await keyed.fetchrow("SELECT tmdb_id, overview FROM title WHERE id = 61")
    assert split["tmdb_id"] is None
    assert split["overview"] == ARRIVAL["overview"]
    assert await keyed.fetchval("SELECT tmdb_id FROM title WHERE id = 60") == 329865
    heat = await keyed.fetchrow("SELECT imdb_id, tvdb_id FROM title WHERE id = 63")
    assert heat["imdb_id"] is None, "title 62 already answers tt0113277"
    assert heat["tvdb_id"] == 70328
    assert "tmdb_id given back, title 60" in (await _task(keyed, 61))["result_note"]


async def test_a_title_a_pipeline_walk_holds_is_handed_back_untouched(keyed, pg_url):
    """The pipeline's per-title lock: a title an acquisition walk is writing is not written here
    at the same time. It is due again at once and spends no attempt."""
    import asyncpg

    await _title(keyed, 70, imdb_id="tt0000070", placement="warm")
    other = await asyncpg.connect(pg_url)
    try:
        assert await other.fetchval(
            "SELECT pg_try_advisory_lock($1, $2)", pipeline._TITLE_LOCK, 70)
        tmdb = Tmdb()
        report = await _walk(keyed, tmdb)
    finally:
        await other.close()
    assert report["yielded"] == 1
    assert tmdb.asked == []
    task = await _task(keyed, 70)
    assert (task["state"], task["attempts"]) == (queue.PENDING, 0)


async def test_the_worker_fires_the_walk_from_its_own_registry(keyed, pg_url, monkeypatch):
    """§1: acquisition is the worker's. The job is a row in `worker.JOBS` on the drain's half
    hour, and the web process's job list names it for §6.6's System card."""
    from spielplan import worker
    from spielplan.api import admin
    from spielplan.db import pool

    job = next(j for j in worker.JOBS if j.name == "metadata-backfill")
    assert (job.every, job.run is not None) == (1800, True)
    assert "metadata-backfill" in admin.JOB_NAMES
    names = [j.name for j in worker.JOBS]
    assert names.index("metadata-backfill") < names.index("art-lookup")
    await _title(keyed, 80, tmdb_id=329865, placement="warm")
    tmdb = Tmdb({"/3/movie/329865": httpx.Response(200, json=ARRIVAL)})
    real = backfill.walk

    async def through_the_mock(conn, **kwargs):
        return await real(conn, transport=httpx.MockTransport(tmdb), **kwargs)

    monkeypatch.setattr(backfill, "walk", through_the_mock)
    await pool.open_pool(pg_url)
    try:
        report = await job.run()
    finally:
        await pool.close_pool()
    assert report["derived"] == 1
