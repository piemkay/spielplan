"""From TMDB, the search beyond the library (decision 558), against `ops/fake_tmdb.py` through the web
process's own fetcher. Needs TEST_DATABASE_URL: the key and the titles Spielplan holds are rows."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from spielplan.acquire.hosts import WEB_TMDB_POLICY
from spielplan.art.poster import ArtService
from spielplan.connectors import registry
from spielplan.home import beyond
from spielplan.sources import tmdb

OPS = Path(__file__).resolve().parents[2] / "ops"
ABSENT = {"available": False, "items": []}


@pytest.fixture
def fake_tmdb():
    spec = importlib.util.spec_from_file_location("fake_tmdb", OPS / "fake_tmdb.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: Pydantic resolves the double's annotations through `sys.modules`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Tmdb(httpx.AsyncBaseTransport):
    """The fake over ASGI, or `answer` in its place, recording every request that reached it."""

    def __init__(self, app=None, answer=None) -> None:
        self.inner = httpx.ASGITransport(app=app) if app is not None else None
        self.answer = answer
        self.asked: list[httpx.URL] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url)
        if self.answer is None:
            return await self.inner.handle_async_request(request)
        result = self.answer(request)
        return await result if asyncio.iscoroutine(result) else result


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def nothing_remembered(monkeypatch):
    monkeypatch.setattr(beyond, "RECENT", beyond._Recent())


@pytest.fixture
async def art(tmp_path):
    made: list[ArtService] = []

    async def make(transport: httpx.AsyncBaseTransport) -> ArtService:
        made.append(await ArtService(tmp_path / "art", transport=transport).open())
        return made[-1]

    yield make
    for svc in made:
        await svc.close()


@pytest.fixture
async def keyed(db, secrets_key, fake_tmdb):
    await registry.save_connector(db, "tmdb", api_key=fake_tmdb.API_KEY)
    return db


async def _search(svc: ArtService, db, q: str, *kinds: str) -> dict:
    @asynccontextmanager
    async def connect():
        yield db

    return await beyond.search_tmdb(svc.fetcher, connect, kinds=kinds or ("movie",), q=q)


async def test_a_search_answers_the_kind_shown_and_both_kinds_on_both(keyed, art, fake_tmdb):
    transport = Tmdb(fake_tmdb.app)
    svc = await art(transport)
    assert await _search(svc, keyed, "lights", "movie") == {"available": True, "items": [{
        "tmdb_id": 910001, "kind": "movie", "name": "Harbour Lights", "original_name": "Harbour Lights",
        "year": 2024, "overview": "A lighthouse keeper's daughter returns to the town that forgot her.",
        "genres": ["Drama", "Romance"], "poster": "/api/art/tmdb/harbourlights.jpg",
        "link": "https://www.themoviedb.org/movie/910001",
    }]}
    asked = transport.asked[0]
    assert (asked.path, asked.params["query"], asked.params["include_adult"]) == (
        "/3/search/movie", "lights", "false")

    series = (await _search(svc, keyed, "lights", "series"))["items"]
    assert [(s["name"], s["genres"], s["link"]) for s in series] == [
        ("Northern Lights", ["Drama", "Mystery"], "https://www.themoviedb.org/tv/920001")]

    both = await _search(svc, keyed, "lights", "movie", "series")
    assert [(i["kind"], i["name"]) for i in both["items"]] == [
        ("movie", "Harbour Lights"), ("series", "Northern Lights")]
    assert [u.path for u in transport.asked] == ["/3/search/movie", "/3/search/tv"], (
        "Both is the two typed searches, each already remembered"
    )


async def test_a_hit_spielplan_holds_is_dropped_only_within_its_kind(keyed, art, fake_tmdb):
    svc = await art(Tmdb(fake_tmdb.app))
    await keyed.execute(
        "INSERT INTO title (id, kind, name, tmdb_id) VALUES "
        "(1, 'movie', 'Glass Orchard', 910002), (2, 'series', 'A Series Under The Same Id', 910001)"
    )
    found = await _search(svc, keyed, "har", "movie")
    assert [i["name"] for i in found["items"]] == ["Harbour Lights"]


async def test_a_repeat_is_answered_from_memory_for_ten_minutes(keyed, art, fake_tmdb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(beyond, "RECENT", beyond._Recent(clock=clock))
    transport = Tmdb(fake_tmdb.app)
    svc = await art(transport)
    first = await _search(svc, keyed, "Glass", "movie")
    assert [i["name"] for i in first["items"]] == ["Glass Orchard"]
    assert await _search(svc, keyed, "  glass ", "movie") == first
    assert len(transport.asked) == 1

    await keyed.execute(
        "INSERT INTO title (id, kind, name, tmdb_id) VALUES (1, 'movie', 'Glass Orchard', 910002)"
    )
    assert (await _search(svc, keyed, "glass", "movie"))["items"] == [], (
        "what Spielplan holds is read on every search, not remembered with TMDB's answer"
    )
    assert len(transport.asked) == 1
    clock.now += beyond.CACHE_TTL_S
    await _search(svc, keyed, "glass", "movie")
    assert len(transport.asked) == 2


async def test_the_query_reaches_tmdb_as_typed_but_for_case_and_spacing(keyed, art, fake_tmdb):
    transport = Tmdb(fake_tmdb.app)
    svc = await art(transport)
    await _search(svc, keyed, "  Das  Weiße Band ")
    assert transport.asked[0].params["query"] == "das weiße band"


async def test_no_key_or_a_short_query_asks_nobody(db, secrets_key, art, fake_tmdb):
    transport = Tmdb(fake_tmdb.app)
    svc = await art(transport)
    assert await _search(svc, db, "harbour") == ABSENT
    await registry.save_connector(db, "tmdb", api_key=fake_tmdb.API_KEY)
    assert await _search(svc, db, " ha ") == ABSENT
    assert transport.asked == []


async def test_a_failing_tmdb_is_absent_and_its_open_breaker_is_not_asked(keyed, art):
    transport = Tmdb(answer=lambda request: httpx.Response(503))
    svc = await art(transport)
    for n in range(WEB_TMDB_POLICY.breaker_threshold):
        assert await _search(svc, keyed, f"query {n}") == ABSENT
    assert len(transport.asked) == WEB_TMDB_POLICY.breaker_threshold
    assert await _search(svc, keyed, "one more") == ABSENT
    assert len(transport.asked) == WEB_TMDB_POLICY.breaker_threshold, "a paused host was asked"


async def test_a_tmdb_too_slow_to_answer_is_absent_and_not_remembered(keyed, art, monkeypatch):
    monkeypatch.setattr(tmdb, "WEB_TIMEOUT_S", 0.05)
    never = asyncio.Event()

    async def stall(request):
        await never.wait()

    transport = Tmdb(answer=stall)
    svc = await art(transport)
    assert await _search(svc, keyed, "harbour") == ABSENT
    assert await _search(svc, keyed, "harbour") == ABSENT
    assert len(transport.asked) == 2, "a failure was remembered as an answer"


async def test_the_query_reaches_no_log_line_and_no_raw_document(keyed, art, fake_tmdb, caplog):
    # As `core/logs.configure` holds them in both processes; the root last, since it sets the handler's.
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.WARNING, logger=name)
    caplog.set_level(logging.DEBUG)
    svc = await art(Tmdb(fake_tmdb.app))
    assert (await _search(svc, keyed, "harbour lights"))["available"] is True
    await registry.save_connector(keyed, "tmdb", api_key="a-key-tmdb-refuses")
    assert await _search(svc, keyed, "glass orchard") == ABSENT

    lines = [record.getMessage() for record in caplog.records]
    assert any("TMDB search unavailable" in line for line in lines), lines
    assert [line for line in lines if "harbour" in line.lower() or "glass" in line.lower()] == []
    assert await keyed.fetchval("SELECT count(*) FROM raw_document") == 0
