"""Want it on a From TMDB title (decision 558, §4.2 `wish`): resolve before minting, mint one wished row,
and wish it. Over HTTP against `ops/fake_tmdb.py`. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from spielplan.acquire.stages import APP_ID_MIN
from spielplan.art.poster import ArtService
from spielplan.connectors import registry
from spielplan.home import beyond
from spielplan.placement import reconcile
from tests.helpers import household
from tests.test_tmdb_search import Tmdb, fake_tmdb  # noqa: F401 - the fixture

CHUNGKING = 4


@dataclass
class House:
    db: object
    patrick: httpx.AsyncClient
    jenny: httpx.AsyncClient
    transport: Tmdb


@pytest.fixture(autouse=True)
def nothing_remembered(monkeypatch):
    monkeypatch.setattr(beyond, "RECENT", beyond._Recent())


async def _serve_tmdb(client: httpx.AsyncClient, tmp_path, transport: httpx.AsyncBaseTransport) -> None:
    """The lifespan closes whichever service is on `app.state.art`, so the replaced one is closed here."""
    application = client._transport.app
    await application.state.art.close()
    application.state.art = await ArtService(tmp_path / "art-under-test", transport=transport).open()


@pytest.fixture
async def house(secrets_key, app, db, tmp_path, fake_tmdb) -> House:  # noqa: F811
    patrick, jenny = await household(app)
    transport = Tmdb(fake_tmdb.app)
    await _serve_tmdb(patrick, tmp_path, transport)
    await registry.save_connector(db, "tmdb", api_key=fake_tmdb.API_KEY)
    # The fixture bundle's Chungking Express: unowned, under a TMDB id the fake does not use.
    await db.execute(
        "INSERT INTO title (id, kind, name, year, imdb_id, tmdb_id) "
        "VALUES ($1, 'movie', 'Chungking Express', 1994, 'tt0109424', 11104)",
        CHUNGKING,
    )
    return House(db, patrick, jenny, transport)


async def _want(client: httpx.AsyncClient, kind: str, tmdb_id: int) -> httpx.Response:
    return await client.put(f"/api/wish/tmdb/{kind}/{tmdb_id}")


async def test_want_it_mints_one_wished_row_whose_card_renders_from_the_database(house):
    answer = await _want(house.patrick, "movie", 910001)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["state"], body["owned"], body["minted"]) == ("want", False, True)
    title_id = body["title_id"]
    assert title_id >= APP_ID_MIN, "a minted id outside decision 162's range"

    row = await house.db.fetchrow("SELECT * FROM title WHERE id = $1", title_id)
    assert (row["origin"], row["is_owned"], row["placement"]) == ("wished", False, "unplaced")
    assert (row["kind"], row["name"], row["original_name"], row["year"], row["runtime_min"]) == (
        "movie", "Harbour Lights", "Harbour Lights", 2024, 118)
    assert (row["imdb_id"], row["tmdb_id"], row["tvdb_id"]) == ("tt9100001", 910001, None)
    assert row["overview"].startswith("A lighthouse keeper's daughter")
    assert row["poster_path"] == "https://image.tmdb.org/t/p/w342/harbourlights.jpg"
    genres = await house.db.fetch(
        "SELECT genre, source FROM title_genre WHERE title_id = $1 ORDER BY genre", title_id
    )
    assert [(g["genre"], g["source"]) for g in genres] == [("Drama", "tmdb"), ("Romance", "tmdb")]

    card = (await house.patrick.get(f"/api/titles/{title_id}")).json()
    assert (card["title"]["origin"], card["title"]["is_owned"]) == ("wished", False)
    assert card["genres"] == ["Drama", "Romance"] and card["wish"]["state"] == "want"
    listed = (await house.patrick.get("/api/wish")).json()
    assert [i["title_id"] for i in listed["mine"]] == [title_id]

    # Held now: later searches find it in the catalogue and no longer under From TMDB.
    found = (await house.patrick.get("/api/wish/tmdb", params={"kind": "movie", "q": "harbour"})).json()
    assert found == {"available": True, "items": []}
    catalogue = (await house.patrick.get("/api/titles", params={"kind": "movie", "q": "harbour"})).json()
    assert [i["id"] for i in catalogue["items"]] == [title_id]
    assert title_id not in await reconcile.titles_needing_placement(
        house.db, bundle_version="test-v1", scope="all_missing"
    ), "a wished row carries nothing the Cold Tower could place it from"


async def test_a_rated_wished_row_is_still_never_placed(house):
    """Rated by anyone puts a title on the sweep (decision 470), but a wished row has nothing to place."""
    title_id = (await _want(house.patrick, "movie", 910001)).json()["title_id"]
    jenny = await house.db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await house.db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, 2)", jenny, title_id
    )
    for scope in ("owned_missing", "reimport", "all_missing"):
        assert title_id not in await reconcile.titles_needing_placement(
            house.db, bundle_version="test-v1", scope=scope
        ), scope


async def test_a_second_member_wants_the_same_row(house):
    first = (await _want(house.patrick, "movie", 910001)).json()
    second = (await _want(house.jenny, "movie", 910001)).json()
    assert second == {"title_id": first["title_id"], "state": "want", "owned": False, "minted": False}
    assert await house.db.fetchval("SELECT count(*) FROM title WHERE tmdb_id = 910001") == 1
    assert await house.db.fetchval(
        "SELECT count(*) FROM wish WHERE title_id = $1 AND state = 'want'", first["title_id"]
    ) == 2


async def test_a_series_is_minted_with_its_tvdb_id(house):
    body = (await _want(house.patrick, "series", 920001)).json()
    row = await house.db.fetchrow(
        "SELECT kind, name, runtime_min, tvdb_id, origin FROM title WHERE id = $1", body["title_id"]
    )
    assert dict(row) == {"kind": "series", "name": "Northern Lights", "runtime_min": 52,
                         "tvdb_id": 9200001, "origin": "wished"}


async def test_want_it_resolves_by_imdb_id_before_it_mints(house):
    """The fake's Chungking Express carries a TMDB id the catalogue's does not: the IMDb id finds it."""
    body = (await _want(house.patrick, "movie", 910003)).json()
    assert body == {"title_id": CHUNGKING, "state": "want", "owned": False, "minted": False}
    assert await house.db.fetchval("SELECT count(*) FROM title WHERE origin = 'wished'") == 0
    assert await house.db.fetchval("SELECT tmdb_id FROM title WHERE id = $1", CHUNGKING) == 11104


async def test_want_it_resolves_by_tmdb_id_within_the_kind_only(house):
    await house.db.execute(
        "INSERT INTO title (id, kind, name, tmdb_id) VALUES "
        "(10, 'movie', 'Harbour Lights', 910001), (11, 'series', 'Same Id, Other Kind', 910002)"
    )
    assert (await _want(house.patrick, "movie", 910001)).json()["title_id"] == 10
    minted = (await _want(house.patrick, "movie", 910002)).json()
    assert minted["minted"] is True and minted["title_id"] not in (10, 11)


async def test_an_owned_match_opens_its_card_and_wishes_nothing(house):
    await house.db.execute(
        "INSERT INTO title (id, kind, name, imdb_id, is_owned) "
        "VALUES (12, 'movie', 'Glass Orchard', 'tt9100002', true)"
    )
    body = (await _want(house.patrick, "movie", 910002)).json()
    assert body == {"title_id": 12, "state": None, "owned": True, "minted": False}
    assert await house.db.fetchval("SELECT count(*) FROM wish") == 0


async def test_two_wants_at_once_mint_one_row(house, tmp_path, fake_tmdb):  # noqa: F811
    """Both details are in hand before either wish reaches Postgres: the lock, not the timing, decides."""
    both = asyncio.Event()
    arrived = 0

    class Together(Tmdb):
        async def handle_async_request(self, request):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)
            return await super().handle_async_request(request)

    await _serve_tmdb(house.patrick, tmp_path, Together(fake_tmdb.app))
    first, second = await asyncio.gather(
        _want(house.patrick, "movie", 910002), _want(house.jenny, "movie", 910002)
    )
    answers = [first.json(), second.json()]
    assert {a["title_id"] for a in answers} == {answers[0]["title_id"]}
    assert sorted(a["minted"] for a in answers) == [False, True]
    assert await house.db.fetchval("SELECT count(*) FROM title WHERE tmdb_id = 910002") == 1
    assert await house.db.fetchval("SELECT count(*) FROM wish") == 2


async def test_an_id_tmdb_does_not_hold_is_a_404_and_tmdb_unreachable_a_503(house, tmp_path):
    assert (await _want(house.patrick, "movie", 999999)).status_code == 404
    assert (await _want(house.patrick, "tv", 920001)).status_code == 422

    def unreachable(request):
        raise httpx.ConnectError("no route to host")

    await _serve_tmdb(house.patrick, tmp_path, Tmdb(answer=unreachable))
    refused = await _want(house.patrick, "movie", 910001)
    assert refused.status_code == 503
    assert refused.json()["detail"] == {"reason": "tmdb_unavailable"}
    assert await house.db.fetchval("SELECT count(*) FROM title WHERE tmdb_id = 910001") == 0


async def test_with_no_key_want_it_is_a_503_and_from_tmdb_is_absent(secrets_key, app, db, tmp_path,
                                                                     fake_tmdb):  # noqa: F811
    patrick, _jenny = await household(app)
    transport = Tmdb(fake_tmdb.app)
    await _serve_tmdb(patrick, tmp_path, transport)
    refused = await _want(patrick, "movie", 910001)
    assert (refused.status_code, refused.json()["detail"]) == (503, {"reason": "tmdb_unavailable"})
    found = await patrick.get("/api/wish/tmdb", params={"kind": ["movie", "series"], "q": "lights"})
    assert found.json() == {"available": False, "items": []}
    assert transport.asked == []


async def test_the_search_route_takes_both_kinds_and_refuses_none(house):
    found = await house.patrick.get(
        "/api/wish/tmdb", params={"kind": ["movie", "series"], "q": "lights"}
    )
    assert [(i["kind"], i["tmdb_id"]) for i in found.json()["items"]] == [
        ("movie", 910001), ("series", 920001)]
    assert (await house.patrick.get("/api/wish/tmdb", params={"q": "lights"})).status_code == 422
