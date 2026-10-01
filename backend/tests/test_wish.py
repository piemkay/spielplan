"""The household's wish list (decision 544, §4.2 `wish`): each person's own rows, one list everyone sees,
and the arrival when a wanted title becomes owned. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from spielplan.home import wish
from spielplan.push import keys
from tests.helpers import household
from tests.test_push_sender import FakePushService, _decrypt, _device

BUNDLE = "test-wish-v1"

# Unowned unless named in OWNED. Collateral carries no id at all, so its line has no link.
PRISONERS, SEVERANCE, COLLATERAL, HEAT = 501, 502, 503, 504
TITLES = (
    (PRISONERS, "movie", "Prisoners", 2013, "tt1392214", 146233),
    (SEVERANCE, "series", "Severance", 2022, None, 95396),
    (COLLATERAL, "movie", "Collateral", 2004, None, None),
    (HEAT, "movie", "Heat", 1995, "tt0113277", 949),
)
OWNED = {HEAT}


@dataclass
class House:
    db: object
    patrick: object      # signed-in clients
    jenny: object
    patrick_id: int
    jenny_id: int


@pytest.fixture
async def house(app, db) -> House:
    patrick, jenny = await household(app)
    for title_id, kind, name, year, imdb, tmdb in TITLES:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, imdb_id, tmdb_id, is_owned) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            title_id, kind, name, year, imdb, tmdb, title_id in OWNED,
        )
    return House(
        db, patrick, jenny,
        await db.fetchval("SELECT id FROM app_user WHERE name = 'patrick'"),
        await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'"),
    )


async def _put(client, title_id: int, state: str):
    return await client.put(f"/api/wish/{title_id}", json={"state": state})


async def _card_state(client, title_id: int):
    response = await client.get(f"/api/titles/{title_id}")
    assert response.status_code == 200, response.text
    return response.json()["wish"]


async def _list(client) -> dict:
    response = await client.get("/api/wish")
    assert response.status_code == 200, response.text
    return response.json()


def _groups(listing: dict) -> list[tuple[list[str], list[str]]]:
    return [
        ([w["name"] for w in g["wanters"]], [i["name"] for i in g["items"]])
        for g in listing["groups"]
    ]


async def test_want_me_too_and_remove_each_touch_only_the_persons_own_row(house):
    assert (await _put(house.patrick, PRISONERS, "want")).json() == {"state": "want"}
    assert await _card_state(house.patrick, PRISONERS) == {"state": "want"}
    assert await _card_state(house.jenny, PRISONERS) == {"state": None}

    assert (await _put(house.jenny, PRISONERS, "want")).status_code == 200
    listing = await _list(house.jenny)
    assert _groups(listing) == [(["jenny", "patrick"], ["Prisoners"])], "the viewer is named first"
    assert listing["groups"][0]["items"][0]["mine"] is True

    removed = await house.patrick.delete(f"/api/wish/{PRISONERS}")
    assert removed.json() == {"state": None}
    assert await _card_state(house.patrick, PRISONERS) == {"state": None}
    item = (await _list(house.patrick))["groups"][0]["items"][0]
    assert item["mine"] is False
    assert [o["name"] for o in item["others_likely"]] == ["patrick"], "the viewer reads as 'you'"


async def test_not_for_me_is_a_pressed_answer_the_same_route_undoes(house):
    assert (await _put(house.patrick, COLLATERAL, "not_for_me")).json() == {"state": "not_for_me"}
    assert await _card_state(house.patrick, COLLATERAL) == {"state": "not_for_me"}
    assert (await _list(house.patrick))["groups"] == [], "Not for me is no want"
    await house.patrick.delete(f"/api/wish/{COLLATERAL}")
    assert await _card_state(house.patrick, COLLATERAL) == {"state": None}


async def test_want_is_refused_on_an_owned_title_and_on_no_title(house):
    owned = await _put(house.patrick, HEAT, "want")
    assert owned.status_code == 422
    assert owned.json()["detail"]["reason"] == "owned"
    assert await _card_state(house.patrick, HEAT) == {"state": None}
    assert (await _put(house.patrick, 999_999, "want")).status_code == 404
    assert (await _put(house.patrick, PRISONERS, "maybe")).status_code == 422


async def test_a_repeated_want_keeps_the_date_it_was_first_wanted(house):
    await _put(house.patrick, PRISONERS, "want")
    first = await house.db.fetchval("SELECT created_at FROM wish WHERE title_id = $1", PRISONERS)
    await _put(house.patrick, PRISONERS, "want")
    assert await house.db.fetchval(
        "SELECT created_at FROM wish WHERE title_id = $1", PRISONERS
    ) == first


async def test_the_list_groups_by_who_wants_most_wanters_first_and_copies_with_links(house):
    await _put(house.patrick, PRISONERS, "want")
    await _put(house.jenny, PRISONERS, "want")
    await _put(house.patrick, SEVERANCE, "want")
    await _put(house.jenny, COLLATERAL, "want")

    mine = await _list(house.patrick)
    assert _groups(mine) == [
        (["patrick", "jenny"], ["Prisoners"]),
        (["patrick"], ["Severance"]),
        (["jenny"], ["Collateral"]),
    ]
    assert mine["copy_text"].splitlines() == [
        "Prisoners (2013) https://www.imdb.com/title/tt1392214/",
        "Severance (2022) https://www.themoviedb.org/tv/95396",
        "Collateral (2004)",
    ]
    theirs = await _list(house.jenny)
    assert [g[0] for g in _groups(theirs)] == [["jenny", "patrick"], ["jenny"], ["patrick"]]
    assert theirs["copy_text"].splitlines()[0].startswith("Prisoners (2013)")


async def test_another_members_likely_reads_their_own_score_among_unowned_titles(house):
    """Likely too at the 70th percentile, maybe from the 50th, nothing below or without a ranking."""
    db = house.db
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )
    fillers = list(range(600, 607))
    for title_id in fillers:
        await db.execute(
            "INSERT INTO title (id, kind, name) VALUES ($1, 'movie', $2)", title_id, f"Filler {title_id}"
        )
    # Jenny's own order over the ten unowned films: Prisoners top, Collateral mid, the fillers between.
    scores = {PRISONERS: 0.9, COLLATERAL: 0.35, **{t: 0.1 * i for i, t in enumerate(fillers)}}
    for user_id in (house.jenny_id, house.patrick_id):
        for title_id, score in scores.items():
            await db.execute(
                "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
                "VALUES ($1, $2, 'movie', $3, $4, 0.0)",
                user_id, title_id, BUNDLE, score,
            )
    await db.execute(
        "INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count, bundle_version) "
        "VALUES ($1, 'movie', 'foldin', $2, 0.4, 25, $3)",
        house.jenny_id, b"\x00" * 256, BUNDLE,
    )
    await _put(house.patrick, PRISONERS, "want")
    await _put(house.patrick, COLLATERAL, "want")
    await _put(house.jenny, fillers[0], "want")

    listing = await _list(house.patrick)
    likely = {
        item["name"]: {o["name"]: o["likely"] for o in item["others_likely"]}
        for group in listing["groups"]
        for item in group["items"]
    }
    # Percentiles among the nine unowned films: Prisoners 1.0, Collateral 0.5.
    assert likely["Prisoners"] == {"jenny": "likely"}
    assert likely["Collateral"] == {"jenny": "maybe"}
    assert likely[f"Filler {fillers[0]}"] == {"patrick": None}, "Patrick has no ranking of his own"


async def test_a_wanted_title_leaves_the_list_and_arrives_for_its_wanters_only(house):
    db = house.db
    await _put(house.patrick, PRISONERS, "want")
    await _put(house.patrick, COLLATERAL, "want")
    home = (await house.patrick.get("/api/home", params={"kind": "movie"})).json()
    assert home["wish"] == {"wanted": 2, "both": 0}
    assert home["arrived"] == []

    await db.execute("UPDATE title SET is_owned = true WHERE id = $1", PRISONERS)
    assert _groups(await _list(house.patrick)) == [(["patrick"], ["Collateral"])]
    home = (await house.patrick.get("/api/home", params={"kind": "movie"})).json()
    assert home["wish"] == {"wanted": 1, "both": 0}
    assert [(a["title_id"], a["name"], a["play_url"]) for a in home["arrived"]] == [
        (PRISONERS, "Prisoners", None)
    ]
    assert home["arrived"][0]["since"]
    jenny_home = (await house.jenny.get("/api/home", params={"kind": "movie"})).json()
    assert jenny_home["arrived"] == []

    # Seen stops the banner; the row itself stays until dismissed.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')",
        house.patrick_id, PRISONERS,
    )
    home = (await house.patrick.get("/api/home", params={"kind": "movie"})).json()
    assert home["arrived"] == []


async def test_dismissing_an_arrival_deletes_its_want_and_nothing_else(house):
    await _put(house.patrick, PRISONERS, "want")
    assert (await house.patrick.post(f"/api/wish/{PRISONERS}/dismiss")).status_code == 404, (
        "a title still on the list has not arrived"
    )
    await house.db.execute("UPDATE title SET is_owned = true WHERE id = $1", PRISONERS)
    dismissed = await house.patrick.post(f"/api/wish/{PRISONERS}/dismiss")
    assert dismissed.json() == {"dismissed": True}
    assert await _card_state(house.patrick, PRISONERS) == {"state": None}
    assert (await house.patrick.post(f"/api/wish/{PRISONERS}/dismiss")).status_code == 404


async def test_an_arrival_is_pushed_to_each_member_who_wanted_it(house, secrets_key):
    db = house.db
    await keys.ensure_keypair(db)
    patricks = await _device(db, house.patrick_id, "https://push.example.test/f/patrick-phone")
    jennys = await _device(db, house.jenny_id, "https://push.example.test/f/jenny-phone")
    await _put(house.patrick, PRISONERS, "want")
    await _put(house.jenny, COLLATERAL, "want")
    # A disabled member keeps their devices; an arrival is not theirs to hear about.
    await _put(house.jenny, PRISONERS, "want")
    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", house.jenny_id)

    service = FakePushService()
    sent = await wish.announce_arrivals(db, {PRISONERS}, transport=service)
    assert sent == 1
    assert [str(r.url) for r in service.requests] == [patricks.endpoint]
    assert _decrypt(service.request_to(patricks).content, patricks) == {
        "kind": "wish.arrived",
        "title_id": PRISONERS,
        "title": "Spielplan",
        "body": "Prisoners is here",
        "tag": f"arrived:{PRISONERS}",
        "url": "/",
    }
    assert jennys.endpoint not in {str(r.url) for r in service.requests}
    assert await wish.announce_arrivals(db, set(), transport=service) == 0
