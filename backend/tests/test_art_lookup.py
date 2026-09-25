"""The worker's TMDB lookup for a title with no servable poster. Spec v2.1 §1, §6.8, §8's
politeness clause; decision 484.

Against the test database, with TMDB as an `httpx.MockTransport`: the lookup's whole contract is
what it asks and what it writes, and both are rows and requests a test can read. Skipped without
TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import httpx
import pytest

from spielplan.art import lookup, sources
from spielplan.connectors import registry
from spielplan.core.config import settings

KEY = "tmdb-key-not-a-real-one-0484"


class Tmdb:
    """TMDB's API as the lookup meets it: what it was asked, and a table of answers by path."""

    def __init__(self, answers: dict[str, httpx.Response] | None = None) -> None:
        self.asked: list[httpx.URL] = []
        self.answers = answers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url)
        return self.answers.get(request.url.path, httpx.Response(404, json={}))

    @property
    def paths(self) -> list[str]:
        return [url.path for url in self.asked]


async def _title(db, title_id: int, *, kind: str = "movie", tmdb_id=None, imdb_id=None,
                 poster_path=None, placement: str = "unplaced") -> None:
    # A placed title names the basis it was placed in (0023's `title_placement_has_basis`).
    basis = None
    if placement != "unplaced":
        basis = "v-art"
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state, kind) "
            "VALUES ($1, '{}'::jsonb, 'active', 'seed') ON CONFLICT DO NOTHING", basis,
        )
    await db.execute(
        "INSERT INTO title (id, kind, name, year, tmdb_id, imdb_id, poster_path, placement, "
        "                   placement_bundle) VALUES ($1, $2, $3, 2004, $4, $5, $6, $7, $8)",
        title_id, kind, f"Title {title_id}", tmdb_id, imdb_id, poster_path, placement, basis,
    )


async def _file(db, *title_ids: int) -> None:
    for title_id in title_ids:
        await db.execute("INSERT INTO art_lookup (title_id) VALUES ($1)", title_id)


@pytest.fixture
async def keyed(db, secrets_key):
    await registry.save_connector(db, "tmdb", api_key=KEY)
    return db


async def test_a_film_is_asked_for_by_its_tmdb_id_and_a_series_on_tmdb_s_tv_path(keyed):
    """§4.1 rule 6: movie/series pairs share tmdb ids, so `kind` chooses the endpoint."""
    await _title(keyed, 1, kind="movie", tmdb_id=5448)
    await _title(keyed, 2, kind="series", tmdb_id=1420)
    await _file(keyed, 1, 2)
    tmdb = Tmdb({
        "/3/movie/5448": httpx.Response(200, json={"poster_path": "/village.jpg"}),
        "/3/tv/1420": httpx.Response(200, json={"poster_path": "/swat.jpg"}),
    })
    report = await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert sorted(tmdb.paths) == ["/3/movie/5448", "/3/tv/1420"]
    assert all(url.params["api_key"] == KEY for url in tmdb.asked)
    rows = {r["title_id"]: r for r in await keyed.fetch("SELECT * FROM art_lookup")}
    assert rows[1]["poster_url"] == "https://image.tmdb.org/t/p/w342/village.jpg"
    assert rows[2]["poster_url"] == "https://image.tmdb.org/t/p/w342/swat.jpg"
    assert {r["outcome"] for r in rows.values()} == {sources.FOUND}
    assert report["found"] == 2


async def test_an_imdb_only_title_is_found_through_tmdb_s_find_by_its_kind(keyed):
    await _title(keyed, 3, kind="movie", imdb_id="tt0368447")
    await _file(keyed, 3)
    tmdb = Tmdb({"/3/find/tt0368447": httpx.Response(200, json={
        "tv_results": [{"poster_path": "/wrong-kind.jpg"}],
        "movie_results": [{"poster_path": "/the-village.jpg"}],
    })})
    await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert tmdb.asked[0].params["external_source"] == "imdb_id"
    assert await keyed.fetchval("SELECT poster_url FROM art_lookup WHERE title_id = 3") == (
        "https://image.tmdb.org/t/p/w342/the-village.jpg"
    )


async def test_a_tmdb_id_of_the_other_kind_falls_back_to_the_imdb_id(keyed):
    """A 404 on the kind's path is the movie/series duplicate, not the title's absence."""
    await _title(keyed, 4, kind="movie", tmdb_id=77, imdb_id="tt0112453")
    await _file(keyed, 4)
    tmdb = Tmdb({"/3/find/tt0112453": httpx.Response(200, json={
        "movie_results": [{"poster_path": "/outbreak.jpg"}]})})
    await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert tmdb.paths == ["/3/movie/77", "/3/find/tt0112453"]
    assert await keyed.fetchval("SELECT outcome FROM art_lookup WHERE title_id = 4") == "found"


async def test_nothing_found_is_remembered_for_thirty_days(keyed):
    await _title(keyed, 5, imdb_id="tt0000005")
    await _file(keyed, 5)
    tmdb = Tmdb({"/3/find/tt0000005": httpx.Response(200, json={"movie_results": []})})
    await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert await lookup.drain(keyed, transport=httpx.MockTransport(tmdb)) is None
    assert len(tmdb.asked) == 1, "a `none` was asked again inside its thirty days"
    await keyed.execute("UPDATE art_lookup SET looked_up_at = now() - interval '31 days'")
    await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert len(tmdb.asked) == 2


async def test_no_key_no_lookup(db):
    await _title(db, 6, tmdb_id=6)
    await _file(db, 6)
    tmdb = Tmdb()
    assert await lookup.drain(db, transport=httpx.MockTransport(tmdb)) is None
    assert tmdb.asked == []
    assert await db.fetchval("SELECT outcome FROM art_lookup WHERE title_id = 6") is None


async def test_with_egress_off_tmdb_is_asked_nothing(keyed, monkeypatch):
    await _title(keyed, 6, tmdb_id=6)
    await _file(keyed, 6)
    monkeypatch.setenv("SPIELPLAN_ART_EGRESS", "false")
    settings.cache_clear()
    try:
        tmdb = Tmdb()
        assert await lookup.drain(keyed, transport=httpx.MockTransport(tmdb)) is None
        assert tmdb.asked == []
    finally:
        settings.cache_clear()


async def test_a_refused_key_stops_the_batch_and_records_nothing_against_the_titles(keyed):
    await _title(keyed, 7, tmdb_id=7)
    await _title(keyed, 8, tmdb_id=8)
    await _file(keyed, 7, 8)
    tmdb = Tmdb({"/3/movie/7": httpx.Response(401, json={"status_message": "Invalid API key"})})
    report = await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    assert "refused" in report["stopped"]
    assert len(tmdb.asked) == 1
    assert await keyed.fetchval("SELECT count(*) FROM art_lookup WHERE outcome IS NOT NULL") == 0


async def test_the_lookup_writes_art_lookup_and_nothing_on_the_title(keyed):
    """Decisions 162 and 372: `title`, `title_meta` and the identity columns are what they were,
    byte for byte, after the answer lands - the answer lives beside the title and is droppable."""
    await _title(keyed, 9, kind="movie", imdb_id="tt0087004", placement="warm")
    await keyed.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES (9, 'omdb', $1)",
        {"poster_url": "https://m.media-amazon.com/images/M/starman.jpg"},
    )
    before = (
        await keyed.fetchval("SELECT row_to_json(t)::text FROM title t WHERE id = 9"),
        await keyed.fetchval("SELECT payload::text FROM title_meta WHERE title_id = 9"),
    )
    tmdb = Tmdb({"/3/find/tt0087004": httpx.Response(200, json={
        "movie_results": [{"id": 9663, "poster_path": "/starman.jpg"}]})})
    await lookup.drain(keyed, transport=httpx.MockTransport(tmdb))
    after = (
        await keyed.fetchval("SELECT row_to_json(t)::text FROM title t WHERE id = 9"),
        await keyed.fetchval("SELECT payload::text FROM title_meta WHERE title_id = 9"),
    )
    assert after == before
    assert await keyed.fetchval("SELECT outcome FROM art_lookup WHERE title_id = 9") == "found"


async def test_the_worker_fires_the_lookup_from_its_own_registry_and_pool(
    keyed, pg_url, monkeypatch
):
    """§1: acquisition is the worker's. The job is a row in `worker.JOBS`, on the drain's
    half-hour, and its body is the drain above over the worker's own pool - the web process
    never reaches `lookup.drain` at all (`api/` imports nothing from it)."""
    from spielplan import worker
    from spielplan.db import pool

    job = next(j for j in worker.JOBS if j.name == "art-lookup")
    assert (job.every, job.run is not None) == (1800, True)
    await _title(keyed, 30, tmdb_id=30)
    await _file(keyed, 30)
    tmdb = Tmdb({"/3/movie/30": httpx.Response(200, json={"poster_path": "/p.jpg"})})
    real = lookup.drain

    async def through_the_mock(conn, **kwargs):
        return await real(conn, transport=httpx.MockTransport(tmdb), **kwargs)

    monkeypatch.setattr(lookup, "drain", through_the_mock)
    await pool.open_pool(pg_url)
    try:
        report = await job.run()
    finally:
        await pool.close_pool()
    assert report["found"] == 1 and tmdb.paths == ["/3/movie/30"]


async def test_placed_titles_nobody_viewed_are_filed_warm_first_and_viewed_ones_lead(keyed):
    """What a member looked at is asked first; then what Rate is about to show them."""
    await _title(keyed, 20, imdb_id="tt0000020", placement="cold_tower")
    await _title(keyed, 21, imdb_id="tt0000021", placement="warm")
    await _title(keyed, 22, imdb_id="tt0000022", placement="unplaced")
    await _title(keyed, 23, imdb_id="tt0000023", placement="warm",
                 poster_path="https://image.tmdb.org/t/p/w500/has-one.jpg")
    await _title(keyed, 24, imdb_id="tt0000024", placement="unplaced")
    await _file(keyed, 24)
    tmdb = Tmdb()
    report = await lookup.drain(keyed, limit=3, transport=httpx.MockTransport(tmdb))
    assert tmdb.paths == ["/3/find/tt0000024", "/3/find/tt0000021", "/3/find/tt0000020"]
    assert report["filed"] == 2
    filed = {r["title_id"] for r in await keyed.fetch("SELECT title_id FROM art_lookup")}
    assert filed == {20, 21, 24}, "an unplaced title nobody viewed, or one with art, was filed"
