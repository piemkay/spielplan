"""§6.3's Place with questions (decision 528): the binary search, then its routes over HTTP. The
route tests need TEST_DATABASE_URL."""

from __future__ import annotations

import math

import pytest

from spielplan.ledger import observations
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import place
from tests.test_rank_routes import ranked  # noqa: F401  (a fixture)


def _placed_at(others: tuple[int, ...], target: int) -> place.Search:
    """Answers as a person whose title belongs at insertion point `target`."""
    search = place.Search(title_id=0, tier=0, low=0, high=len(others))
    while (i := search.probe(others)) is not None:
        search = search.answered(i, "A" if target <= i else "B")
    return search


def test_the_search_finds_every_spot_in_about_log2_questions():
    for n in (1, 2, 5, 149, 749):
        others = tuple(range(1, n + 1))
        estimate = place.View(place.Search(0, 0, 0, n), "A", others).progress()["estimate"]
        assert estimate == math.ceil(math.log2(n + 1))
        for target in {0, 1, n // 2, n - 1, n}:
            found = _placed_at(others, target)
            assert (found.low, found.high) == (target, target)
            assert found.asked <= estimate
    assert place.View(place.Search(0, 0, 0, 149), "A", tuple(range(149))).progress()[
        "estimate"
    ] == 8, "§6.3: 8 questions in a tier of 150"


def test_a_tie_ends_the_search_just_below_that_neighbour():
    others = (11, 12, 13, 14, 15)
    search = place.Search(0, 0, 0, len(others))
    tied = search.probe(others)
    done = search.answered(tied, "TIE")
    assert done.probe(others) is None and done.asked == 1
    above, below, _around = place.View(done, "A", others).spot()
    assert (above, below) == (others[tied], others[tied + 1])


def test_a_neighbour_not_seen_hands_the_question_to_the_one_beside_it():
    others = (11, 12, 13, 14, 15)
    search = place.Search(0, 0, 0, len(others))
    assert others[search.probe(others)] == 13
    skipped = place.Search(0, 0, 0, len(others), skipped=(13,))
    assert others[skipped.probe(others)] == 12
    assert place.Search(0, 0, 1, 2, skipped=(12,)).probe(others) is None


def test_the_probe_weighs_rest_then_genre_then_comparisons_among_the_three_nearest_the_middle():
    """Decision 538: the middle is taken loosely, and never further than its three nearest."""
    others = (11, 12, 13, 14, 15, 16, 17)
    search = place.Search(title_id=0, tier=0, low=0, high=len(others))
    assert others[search.probe(others)] == 14
    counts = {14: 5, 13: 2, 15: 1, 11: 0}
    assert others[search.probe(others, comparisons=counts)] == 15
    crime = {0: ("Crime",), 13: ("Crime",), 11: ("Crime",)}
    assert others[search.probe(others, genres=crime, comparisons=counts)] == 13
    assert others[search.probe(others, recent=frozenset({13}), genres=crime, comparisons=counts)] == 15
    assert search.probe(others, recent=frozenset(others)) == 3, "with every title resting, it still asks"


def test_a_loosely_taken_middle_still_places_in_about_log2_questions():
    """Comparison counts that pull every probe off the middle cost a question or two, not a walk."""
    for n in (5, 16, 149):
        others = tuple(range(1, n + 1))
        counts = {t: 0 if t % 2 else 9 for t in others}
        estimate = math.ceil(math.log2(n + 1))
        for target in {0, 1, n // 2, n - 1, n}:
            search = place.Search(title_id=0, tier=0, low=0, high=n)
            while (i := search.probe(others, comparisons=counts)) is not None:
                search = search.answered(i, "A" if target <= i else "B")
            assert (search.low, search.high) == (target, target)
            assert search.asked <= estimate + 2, (n, target, search.asked)


def test_the_neighbourhood_rings_the_title_in_two_rows_of_four():
    others = tuple(range(1, 11))
    view = place.View(place.Search(99, 0, 5, 5, asked=4), "A", others)
    assert view.spot() == (5, 6, [4, 5, 99, 6, 7, 8, 9, 10])
    top = place.View(place.Search(99, 0, 0, 0), "A", others[:2])
    assert top.spot() == (None, 1, [99, 1, 2])


@pytest.fixture
async def tier(db, ranked):  # noqa: F811
    """The six rated titles, all put in B, in the order the board reads them."""
    client, user_id = ranked
    for title_id in range(1, 7):
        await observations.record_tier_edit(db, user_id=user_id, title_id=title_id, tier=3)
    board = (await client.get("/api/rank?kind=movie")).json()
    entries = next(t for t in board["tiers"] if t["label"] == "B")["entries"]
    assert len(entries) == 6
    return client, user_id, [e["title_id"] for e in entries]


async def _duels(db, user_id: int) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_place'", user_id
    )


async def test_placing_opens_on_the_middle_of_the_titles_tier(db, tier):
    client, _user_id, order = tier
    placed, others = order[0], order[1:]
    opened = await client.post("/api/rank/place", json={"title_id": placed, "kind": "movie"})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert (body["left"]["id"], body["left"]["outcome"]) == (placed, "A")
    assert (body["right"]["id"], body["right"]["outcome"]) == (others[2], "B")
    assert body["tier"] == "B"
    assert body["progress"] == {"low": 1, "high": 6, "size": 6, "asked": 0, "estimate": 3}

    unrated = await client.post("/api/rank/place", json={"title_id": 7, "kind": "movie"})
    assert unrated.status_code == 422
    other_kind = await client.post("/api/rank/place", json={"title_id": placed, "kind": "series"})
    assert other_kind.status_code == 422


async def test_placing_asks_a_genre_sharer_near_the_middle_and_rests_the_last_two_pairs(db, tier):
    """Decision 538 over the route: genres, comparisons and the last pairs are read at each step."""
    client, user_id, order = tier
    placed, others = order[0], order[1:]
    await db.executemany(
        "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, 'Crime', 'tmdb')",
        [(placed,), (others[3],)],
    )

    async def opened():
        return (await client.post("/api/rank/place", json={"title_id": placed, "kind": "movie"})).json()

    async def asked(neighbour):
        await observations.record_duel(
            db, user_id=user_id, title_a=placed, title_b=neighbour, outcome="A",
            context=place.CONTEXT, decisive=False, hp=DEFAULTS,
        )

    assert (await opened())["right"]["id"] == others[3], "the crime film one off the middle"
    await asked(others[3])
    assert (await opened())["right"]["id"] == others[2], "the last pair's neighbour rests"
    await asked(others[0])
    await asked(others[0])
    assert (await opened())["right"]["id"] == others[3], "two pairs on, it serves again"


async def test_a_placement_records_one_duel_per_question_and_ends_between_two_titles(db, tier):
    client, user_id, order = tier
    placed = order[0]
    body = (await client.post("/api/rank/place", json={"title_id": placed, "kind": "movie"})).json()
    asked = 0
    while not body.get("done"):
        answered = await client.post(
            "/api/rank/place/answer", json={"token": body["token"], "outcome": "B"}
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        asked += 1
    assert asked <= 3
    assert (body["tier"], body["asked"], body["below"]) == ("B", asked, None)
    assert body["above"]["id"] != placed
    assert placed in [t["id"] for t in body["around"]]
    rows = await db.fetch(
        "SELECT title_a, outcome, margin FROM duel WHERE user_id = $1 AND context = 'tier_place'",
        user_id,
    )
    assert [(r["title_a"], r["outcome"]) for r in rows] == [(placed, "B")] * asked
    assert [r["margin"] for r in rows] == [pytest.approx(DEFAULTS.margin_for(False))] * asked


async def test_much_more_is_weighted_and_a_tie_never_is(db, tier):
    client, user_id, order = tier
    body = (await client.post("/api/rank/place", json={"title_id": order[0], "kind": "movie"})).json()
    body = (
        await client.post(
            "/api/rank/place/answer",
            json={"token": body["token"], "outcome": "A", "decisive": True},
        )
    ).json()
    tied = await client.post(
        "/api/rank/place/answer", json={"token": body["token"], "outcome": "TIE", "decisive": True}
    )
    assert tied.status_code == 200, tied.text
    assert tied.json()["done"] is True
    margins = await db.fetch(
        "SELECT outcome, margin FROM duel WHERE user_id = $1 AND context = 'tier_place' "
        "ORDER BY id",
        user_id,
    )
    assert [(r["outcome"], pytest.approx(r["margin"])) for r in margins] == [
        ("A", DEFAULTS.margin_for(True)),
        ("TIE", DEFAULTS.margin_for(False)),
    ]


async def test_not_seen_marks_the_neighbour_and_asks_about_the_one_beside_it(db, tier):
    client, user_id, order = tier
    others = order[1:]
    body = (await client.post("/api/rank/place", json={"title_id": order[0], "kind": "movie"})).json()
    skipped = await client.post("/api/rank/place/skip", json={"token": body["token"]})
    assert skipped.status_code == 200, skipped.text
    after = skipped.json()
    assert after["right"]["id"] == others[1]
    assert after["progress"]["asked"] == 0
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, others[2]
    ) == "unseen"
    assert await _duels(db, user_id) == 0


async def test_a_tampered_replayed_or_borrowed_token_is_refused_and_writes_nothing(db, app, tier):
    client, user_id, order = tier
    body = (await client.post("/api/rank/place", json={"title_id": order[0], "kind": "movie"})).json()
    queue_token = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]["token"]
    for token in ("not-a-token", body["token"][:-3] + "aaa", queue_token):
        refused = await client.post(
            "/api/rank/place/answer", json={"token": token, "outcome": "A"}
        )
        assert refused.status_code == 409, token
    assert await _duels(db, user_id) == 0

    first = await client.post("/api/rank/place/answer", json={"token": body["token"], "outcome": "A"})
    assert first.status_code == 200, first.text
    replayed = await client.post(
        "/api/rank/place/answer", json={"token": body["token"], "outcome": "B"}
    )
    assert replayed.status_code == 409
    assert await _duels(db, user_id) == 1

    otp = (
        await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]
    other = app()
    await other.post("/api/auth/login", json={"name": "jenny", "password": otp})
    await other.post(
        "/api/auth/password", json={"current_password": otp, "new_password": "jennys-password"}
    )
    await db.execute("INSERT INTO ladder_setup (user_id) SELECT id FROM app_user WHERE name = 'jenny'")
    borrowed = await other.post(
        "/api/rank/place/answer", json={"token": first.json()["token"], "outcome": "A"}
    )
    assert borrowed.status_code == 403
    assert await _duels(db, user_id) == 1
