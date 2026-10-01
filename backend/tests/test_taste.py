"""§6.5's Your taste and Compare (decision 549): the chart, the seats, and what one member may see of
another. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.ledger import observations
from spielplan.taste import chart
from tests.helpers import household, insert_user

MOVIES = range(1, 61)
SERIES = range(101, 131)
FACETS = ("mood", "themes", "pacing", "era")
TERMS = {
    "mood.dark": "Dark",
    "mood.cosy": "Cosy",
    "themes.heist": "Heist",
    "themes.revenge": None,
    "pacing.slow_burn": "Slow burn",
    "era.1990s": "1990s",
    **{f"themes.t{g:02d}": f"T{g:02d}" for g in range(1, 13)},
}

# Twelve groups of four films, group g carrying term t{g+1}. These steps average 3 over all 48.
GROUP_STEPS = (6, 6, 5, 5, 4, 4, 2, 2, 1, 1, 0, 0)


@pytest.fixture
async def world(db):
    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 4, 18)")
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ('v1', $1, $2)",
        [(facet, i) for i, facet in enumerate(FACETS)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, label) VALUES ('v1', $1, $2, $3)",
        [(term, term.split(".")[0], label) for term, label in TERMS.items()],
    )
    await db.executemany(
        "INSERT INTO title (id, kind, name, poster_path, is_owned) VALUES ($1, $2, $3, $4, true)",
        [(i, "movie", f"Film {i:02d}", f"/p{i}.jpg") for i in MOVIES]
        + [(i, "series", f"Series {i}", f"/s{i}.jpg") for i in SERIES],
    )
    return db


async def place(db, user: int, ladder: dict[int, int]) -> None:
    for title_id, tier in ladder.items():
        await observations.record_tier_edit(
            db, user_id=user, title_id=title_id, tier=tier, via="explicit"
        )


async def tag(db, term: str, titles, provider: str = "gemini") -> None:
    await db.executemany(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, provider) "
        "VALUES ($1, 'v1', $2, $3, 2, 0.9, 1, $4)",
        [(t, term, term.split(".")[0], provider) for t in titles],
    )


def groups(steps=GROUP_STEPS) -> dict[int, int]:
    return {1 + 4 * g + i: step for g, step in enumerate(steps) for i in range(4)}


async def tag_groups(db) -> None:
    for g in range(12):
        await tag(db, f"themes.t{g + 1:02d}", range(1 + 4 * g, 5 + 4 * g))


def terms(rows) -> list[str]:
    return [r["term"] for r in rows]


# Eight films, mean step 27/8.
EIGHT = {1: 6, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 0}


async def test_a_term_needs_four_carriers(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, EIGHT)
    await tag(world, "mood.dark", (1, 2, 3, 4))
    await tag(world, "mood.cosy", (5, 6, 7))

    read = await chart.chart(world, user_id=user, kind="movie")

    assert terms(read["all"]) == ["mood.dark"]
    assert read["n_terms"] == 1
    assert read["placed"] == 8


async def test_the_projected_tier_carries_nothing(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, EIGHT)
    await tag(world, "mood.cosy", (5, 6, 7))
    await world.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) "
        "VALUES (8, 'v1', 'mood.cosy', 'mood', 0.9)"
    )

    assert (await chart.chart(world, user_id=user, kind="movie"))["all"] == []


async def test_two_providers_tagging_one_film_count_it_once(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, EIGHT)
    await tag(world, "mood.cosy", (5, 6, 7), provider="gemini")
    await tag(world, "mood.cosy", (5, 6, 7), provider="anthropic")

    assert (await chart.chart(world, user_id=user, kind="movie"))["all"] == []


async def test_a_term_sits_high_or_low_against_the_persons_own_mean(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, EIGHT)
    await tag(world, "mood.dark", (1, 2, 3, 4))          # mean 5.25: the strongest
    await tag(world, "themes.heist", (4, 6, 7, 8))       # mean 1.75
    await tag(world, "pacing.slow_burn", (2, 4, 6, 8))   # mean 3.0, just under 3.375

    read = await chart.chart(world, user_id=user, kind="movie")
    pos = {r["term"]: r["pos"] for r in read["all"]}

    assert pos["mood.dark"] == 0.9
    assert pos["themes.heist"] == pytest.approx(-0.9 * 1.625 / 1.875, abs=1e-4)
    assert pos["pacing.slow_burn"] == pytest.approx(-0.9 * 0.375 / 1.875, abs=1e-4)
    assert terms(read["high"]) == ["mood.dark"]
    assert terms(read["low"]) == ["themes.heist", "pacing.slow_burn"]


async def test_a_tie_goes_to_more_carriers_then_the_label(world):
    user = await insert_user(world, "patrick", "admin")
    # Mean 3: four films at 6, four at 0, four at 3.
    await place(world, user, {**dict.fromkeys(range(1, 5), 6), **dict.fromkeys(range(5, 9), 0),
                              **dict.fromkeys(range(9, 13), 3)})
    await tag(world, "mood.dark", (1, 2, 3, 4))
    await tag(world, "pacing.slow_burn", (1, 9, 10, 11))
    await tag(world, "era.1990s", (2, 9, 10, 12))
    await tag(world, "themes.revenge", (1, 2, 3, 5, 9, 10, 11, 12))

    read = await chart.chart(world, user_id=user, kind="movie")

    assert terms(read["all"]) == ["mood.dark", "themes.revenge", "era.1990s", "pacing.slow_burn"]
    labels = {r["term"]: r["label"] for r in read["all"]}
    assert labels["themes.revenge"] == "revenge"
    assert len({r["pos"] for r in read["all"][1:]}) == 1


async def test_five_sit_high_and_five_land_lowest_first(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, groups())
    await tag_groups(world)

    read = await chart.chart(world, user_id=user, kind="movie")

    assert terms(read["high"]) == [f"themes.t{g:02d}" for g in (1, 2, 3, 4, 5)]
    assert terms(read["low"]) == [f"themes.t{g:02d}" for g in (11, 12, 9, 10, 7)]
    assert terms(read["all"]) == [f"themes.t{g:02d}" for g in range(1, 13)]
    assert read["n_terms"] == 12


async def test_posters_lead_with_the_highest_placed_on_a_high_row_and_the_lowest_on_a_low_one(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, {1: 6, 2: 6, 3: 5, 4: 4, 5: 3, 6: 0, 7: 0, 8: 0, 9: 0, 10: 1})
    # Within a step, the person's own Rank order.
    await world.executemany(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma) VALUES ($1, $2, 'movie', $3, 1)",
        [(user, 1, 0.5), (user, 2, 1.0)],
    )
    await tag(world, "mood.dark", (1, 2, 3, 4, 5, 10))
    await tag(world, "themes.heist", (3, 6, 7, 8, 9, 10))

    rows = {r["term"]: r for r in (await chart.chart(world, user_id=user, kind="movie"))["all"]}

    dark, heist = rows["mood.dark"], rows["themes.heist"]
    assert dark["pos"] > 0 > heist["pos"]
    assert [f["id"] for f in dark["films"]] == [2, 1, 3, 4]
    assert dark["more"] == 2
    assert [f["id"] for f in heist["films"]] == [9, 8, 7, 6]
    assert heist["more"] == 2
    assert dark["films"][0] == {"id": 2, "name": "Film 02", "poster_path": "/p2.jpg"}


async def test_films_and_series_are_read_apart(world):
    user = await insert_user(world, "patrick", "admin")
    await place(world, user, EIGHT)
    await place(world, user, {101: 6, 102: 0, 103: 0, 104: 0, 105: 0})
    await tag(world, "mood.dark", (1, 2, 3, 4))
    await tag(world, "mood.cosy", (102, 103, 104, 105))

    films = await chart.chart(world, user_id=user, kind="movie")
    series = await chart.chart(world, user_id=user, kind="series")

    assert (films["placed"], terms(films["all"])) == (8, ["mood.dark"])
    assert (series["placed"], terms(series["all"])) == (5, ["mood.cosy"])
    assert series["kind"] == "series"


async def test_a_member_under_twenty_is_listed_but_cannot_be_picked(world):
    patrick = await insert_user(world, "patrick", "admin")
    jenny = await insert_user(world, "jenny")
    lena = await insert_user(world, "lena")
    await place(world, patrick, dict.fromkeys(range(1, 21), 3))
    await place(world, jenny, dict.fromkeys(range(1, 21), 3))
    await place(world, lena, dict.fromkeys(range(1, 20), 3))

    listed = (await chart.members(world, viewer_id=patrick, kind="movie"))["members"]
    seats = {m["name"]: (m["pickable"], m["reason"]) for m in listed}

    assert seats == {
        "jenny": (True, None),
        "lena": (False, "Not enough films placed yet"),
        "patrick": (True, None),
    }
    series = (await chart.members(world, viewer_id=patrick, kind="series"))["members"]
    assert {m["reason"] for m in series} == {"Not enough series placed yet"}


async def test_compare_opens_on_the_pickable_member_sharing_most_films_with_the_viewer(world):
    patrick = await insert_user(world, "patrick", "admin")
    jenny = await insert_user(world, "jenny")
    lena = await insert_user(world, "lena")
    sam = await insert_user(world, "sam")
    await place(world, patrick, dict.fromkeys(range(1, 21), 3))
    await place(world, jenny, dict.fromkeys(range(11, 31), 3))   # shares 10
    await place(world, lena, dict.fromkeys(range(1, 21), 3))     # shares 20
    await place(world, sam, dict.fromkeys(range(1, 20), 3))      # shares 19, under 20

    assert (await chart.members(world, viewer_id=patrick, kind="movie"))["default"] == [patrick, lena]
    assert (await chart.members(world, viewer_id=sam, kind="movie"))["default"] == [sam, lena]


async def test_initials_take_a_second_letter_only_where_two_names_start_alike(world):
    patrick = await insert_user(world, "patrick", "admin")
    await insert_user(world, "Paula")
    await insert_user(world, "jenny")

    listed = (await chart.members(world, viewer_id=patrick, kind="movie"))["members"]

    # Patrick and Paula share their first two letters too, and two markers never read alike.
    assert {m["name"]: m["initials"] for m in listed} == {"jenny": "J", "Paula": "Pu", "patrick": "Pa"}


async def pair(db) -> dict[str, int]:
    """Jenny and Lena placed the same 48 films, read on twelve terms; Patrick placed none."""
    who = {name: await insert_user(db, name) for name in ("jenny", "lena", "patrick")}
    await place(db, who["jenny"], groups())
    await place(db, who["lena"], groups((6, 0, 5, 1, 4, 2, 2, 4, 1, 5, 0, 6)))
    await tag_groups(db)
    return who


async def test_compare_names_where_two_members_part_and_where_they_meet(world):
    who = await pair(world)

    read = await chart.compare(world, viewer_id=who["jenny"], kind="movie", a=who["jenny"], b=who["lena"])

    assert terms(read["different"]) == [f"themes.t{g:02d}" for g in (2, 12, 4, 10, 6)]
    assert terms(read["alike"]) == [f"themes.t{g:02d}" for g in (1, 3, 5, 7, 9)]
    assert read["note"] is None
    t02 = next(r for r in read["all"] if r["term"] == "themes.t02")
    assert (t02["pa"], t02["pb"]) == (0.9, -0.9)
    assert len(read["all"]) == 12


async def test_the_films_behind_a_term_go_only_to_the_two_compared_in_the_viewers_order(world):
    who = await pair(world)
    # On t01 Jenny puts film 4 above films 1-3; Lena placed the four alike.
    await place(world, who["jenny"], {1: 5, 2: 5, 3: 5})

    seated = await chart.compare(
        world, viewer_id=who["jenny"], kind="movie", a=who["lena"], b=who["jenny"]
    )
    outside = await chart.compare(
        world, viewer_id=who["patrick"], kind="movie", a=who["jenny"], b=who["lena"]
    )

    t01 = next(r for r in seated["all"] if r["term"] == "themes.t01")
    assert [f["id"] for f in t01["films"]] == [4, 1, 2, 3]
    assert seated["films_visible"] is True
    assert outside["films_visible"] is False
    assert all(r["films"] == [] and r["more"] == 0 for r in outside["all"])


async def test_compare_refuses_a_seat_that_cannot_be_picked(world):
    who = await pair(world)
    with pytest.raises(chart.NotPickable):
        await chart.compare(world, viewer_id=who["jenny"], kind="movie", a=who["jenny"], b=who["patrick"])
    with pytest.raises(chart.NotPickable):
        await chart.compare(world, viewer_id=who["jenny"], kind="movie", a=who["jenny"], b=who["jenny"])


# What a member may receive about another: their name, colour and initials, whether they can be
# picked, and where a term sits for them. No step, letter, count or ladder size.
MEMBER_KEYS = {"members", "default", "id", "name", "role", "colour", "initials", "pickable", "reason"}
COMPARE_KEYS = MEMBER_KEYS | {
    "kind", "a", "b", "alike", "different", "all", "films_visible", "note",
    "term", "label", "facet", "pa", "pb", "films", "more", "poster_path",
}


def keys_of(payload) -> set[str]:
    if isinstance(payload, dict):
        return set(payload) | {k for v in payload.values() for k in keys_of(v)}
    if isinstance(payload, list):
        return {k for item in payload for k in keys_of(item)}
    return set()


async def ids(client) -> int:
    return (await client.get("/api/auth/me")).json()["id"]


async def test_no_payload_carries_another_members_steps_letters_or_counts(world, app):
    patrick, jenny = await household(app)
    p, j = await ids(patrick), await ids(jenny)
    lena = await insert_user(world, "lena")
    await place(world, j, groups())
    await place(world, lena, groups((6, 0, 5, 1, 4, 2, 2, 4, 1, 5, 0, 6)))
    await tag_groups(world)

    for client in (patrick, jenny):
        await client.post("/api/auth/preferences", json={"show_model": True})
        members = (await client.get("/api/taste/members", params={"kind": "movie"})).json()
        compared = await client.get("/api/taste/compare", params={"kind": "movie", "a": j, "b": lena})
        assert compared.status_code == 200, compared.text
        assert keys_of(members) <= MEMBER_KEYS
        assert keys_of(compared.json()) <= COMPARE_KEYS
    assert compared.json()["films_visible"] is True

    refused = await patrick.get("/api/taste/compare", params={"kind": "movie", "a": p, "b": lena})
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "not_pickable"


async def test_the_numbers_behind_a_row_wait_for_show_the_numbers(world, app):
    patrick, _jenny = await household(app)
    p = await ids(patrick)
    await place(world, p, EIGHT)
    await tag(world, "mood.dark", (1, 2, 3, 4))

    off = (await patrick.get("/api/taste", params={"kind": "movie"})).json()
    await patrick.post("/api/auth/preferences", json={"show_model": True})
    on = (await patrick.get("/api/taste", params={"kind": "movie"})).json()

    assert "model" not in off["all"][0]
    assert on["all"][0]["model"] == {"d": 1.875, "carriers": 4}
    assert (await patrick.get("/api/taste", params={"kind": "both"})).status_code == 422
