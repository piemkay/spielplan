"""The ladder's set-up (§6.1, decisions 547 and 556): its steps, each step's films (the household's watched
films first, a widely seen film every fourth slot), and the finish that is the member's cut-over
(decision 537). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.ledger import ladder, observations
from spielplan.rate import setup
from tests.helpers import household, insert_user

BUNDLE = "test-setup-v1"

# Widely seen by IMDb's count, IMDb 9.0 down to 5.0, the lower scored with more votes.
KNOWN = tuple(range(1, 10))
# Widely seen by TMDB's count alone, 2,000 standing for 100,000; scored between films 6 and 7.
TMDB_WIDE = 10
# Less seen: just under the line by IMDb's count and by TMDB's, a small TMDB count, IMDb's score and
# count winning over TMDB's, and neither IMDb nor TMDB.
OBSCURE_IMDB, TMDB_SHORT, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC = 11, 15, 12, 13, 14
UNSCORED_A, UNSCORED_B = 21, 22
SERIES = 31

WIDE_AT_TOP = [1, 2, 3, 4, 5, 6, TMDB_WIDE, 7, 8, 9]
LESS_SEEN_AT_TOP = [TMDB_SHORT, OBSCURE_IMDB, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC]
TOP = WIDE_AT_TOP + LESS_SEEN_AT_TOP + [UNSCORED_A, UNSCORED_B]

TAP = " Tap the ones you remember well."


async def _seed(db) -> None:
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest) VALUES ($1, '{}'::jsonb)", BUNDLE
    )
    films = KNOWN + (TMDB_WIDE, OBSCURE_IMDB, TMDB_SHORT, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC)
    films += (UNSCORED_A, UNSCORED_B)
    for title_id in films + (SERIES,):
        await db.execute(
            "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, 2000, $4)",
            title_id, "series" if title_id == SERIES else "movie", f"Film {title_id}",
            title_id != UNSCORED_B,
        )
    # Crowd support the set-up no longer reads, highest on the least seen.
    for title_id in films:
        await db.execute(
            "INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source) "
            "VALUES ($1, $2, 0.5, 0.5, $3, 0.9, 'backbone')",
            title_id, BUNDLE, 100 * title_id,
        )
    rows = [
        (t, "imdb", "user_score", 9.0 - 0.5 * i, 10.0, 200_000 + 10_000 * i) for i, t in enumerate(KNOWN)
    ]
    rows += [
        (TMDB_WIDE, "tmdb", "user_score", 6.2, 10.0, 2_000),
        (OBSCURE_IMDB, "imdb", "user_score", 9.5, 10.0, 99_999),
        (TMDB_SHORT, "tmdb", "user_score", 9.8, 10.0, 1_999),
        # Film 5's score, with fewer votes.
        (OBSCURE_TMDB, "tmdb", "user_score", 7.0, 10.0, 100),
        (OBSCURE_BOTH, "imdb", "user_score", 4.0, 10.0, 500),
        (OBSCURE_BOTH, "tmdb", "user_score", 9.9, 10.0, 10_000),
        # The mean of the others, each over its own scale (0.30), and no count.
        (OBSCURE_CRITIC, "metacritic", "critic_score", 20.0, 100.0, None),
        (OBSCURE_CRITIC, "rottentomatoes", "audience_score", 40.0, 100.0, None),
        (OBSCURE_CRITIC, "tmdb", "popularity", 99.0, None, None),
        (SERIES, "imdb", "user_score", 9.9, 10.0, 900_000),
    ]
    await db.executemany(
        "INSERT INTO display.platform_rating (title_id, platform, metric, score, scale, votes) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        rows,
    )


@pytest.fixture
async def house(app, db):
    patrick, jenny = await household(app)
    await _seed(db)
    return {
        "db": db,
        "patrick": patrick,
        "jenny": jenny,
        "patrick_id": await db.fetchval("SELECT id FROM app_user WHERE name = 'patrick'"),
        "jenny_id": await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'"),
    }


async def _films(client, step, **params):
    response = await client.get("/api/ladder/setup/films", params={"step": step, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def _order(client, step, **params):
    return [f["id"] for f in (await _films(client, step, limit=48, **params))["films"]]


async def _mark(db, user_id, *title_ids, state="seen"):
    await db.executemany(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, $3)",
        [(user_id, t, state) for t in title_ids],
    )


async def _inactive(db) -> int:
    olga = await insert_user(db, "olga")
    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", olga)
    return olga


# --- the steps ------------------------------------------------------------------------------


async def test_the_steps_run_best_first_named_by_their_words_with_no_letter(house):
    response = await house["patrick"].get("/api/ladder/setup")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["done"] is False and payload["earlier_ratings"] == 0
    steps = payload["steps"]
    assert [s["tier"] for s in steps] == [6, 5, 4, 3, 2, 1, 0]
    assert [s["word"] for s in steps] == [
        "All-time favourite", "Loved it", "Liked it", "It was fine",
        "Not really for me", "Didn't like it", "Hated it",
    ]
    assert [s["hint"] for s in steps] == ["Popular films first." + TAP] * 7
    assert all(set(s) == {"tier", "word", "hint"} for s in steps)


async def test_the_hint_says_whose_watched_films_lead_the_page(house):
    db, patrick, jenny = house["db"], house["patrick_id"], house["jenny_id"]

    async def hints():
        return {s["hint"] for s in (await house["patrick"].get("/api/ladder/setup")).json()["steps"]}

    await _mark(db, await _inactive(db), 1)
    await _mark(db, jenny, SERIES, 3)
    await _mark(db, patrick, 3, state="unseen")
    assert await hints() == {"Popular films first." + TAP}, (
        "an inactive member's film, a series and a film marked not seen lead nothing"
    )
    await _mark(db, jenny, 4)
    assert await hints() == {"Films your household has watched first, then popular ones." + TAP}
    await _mark(db, patrick, 5)
    assert await hints() == {"Films you've watched first, then popular ones." + TAP}


async def test_a_custom_set_has_a_step_per_label(house):
    await house["db"].execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, tier_set, boundaries) "
        "VALUES ($1, 'movie', ARRAY['Bad', 'Okay', 'Good'], ARRAY[-1.0, 1.0]::double precision[])",
        house["patrick_id"],
    )
    steps = (await house["patrick"].get("/api/ladder/setup")).json()["steps"]
    assert [(s["tier"], s["word"]) for s in steps] == [(2, "Good"), (1, "Okay"), (0, "Bad")]


def test_each_step_opens_where_its_tier_begins_in_the_measured_shape():
    assert [setup.start_of(tier, 7) for tier in reversed(range(7))] == [
        0.0, 0.08, 0.25, 0.5, 0.75, 0.9, 1.0,
    ]
    assert [setup.start_of(tier, 5) for tier in reversed(range(5))] == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_each_step_reads_its_tiers_slice_of_the_measured_shape():
    assert [setup.band_of(tier, 7) for tier in reversed(range(7))] == [
        (0.0, 0.08), (0.08, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 0.97), (0.97, 1.0),
    ]
    assert [setup.band_of(tier, 5) for tier in reversed(range(5))] == [
        (0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0),
    ]


# --- the films ------------------------------------------------------------------------------


async def test_widely_seen_films_come_before_the_less_seen_and_unscored_films_last(house):
    order = await _order(house["patrick"], 6)
    assert order == TOP, "100,000 votes, IMDb's or 50 times TMDB's, make a film widely seen"
    assert SERIES not in order, "the set-up is for films only"


async def test_a_step_reads_its_band_most_voted_first_then_the_films_nearest_it(house):
    # Liked it reads 25-50% of the widely seen order: films 4 and 5, then 3 and 6 just outside it.
    assert (await _order(house["patrick"], 4))[:4] == [5, 4, 3, 6]


async def test_own_watched_films_lead_then_another_members_with_a_widely_seen_one_every_fourth(house):
    db = house["db"]
    await _mark(db, house["patrick_id"], 5, OBSCURE_TMDB, UNSCORED_A)
    await _mark(db, house["jenny_id"], OBSCURE_TMDB, 2, OBSCURE_CRITIC, UNSCORED_B)
    order = await _order(house["patrick"], 6)
    assert order[:12] == [
        5, OBSCURE_TMDB, UNSCORED_A, 1,
        2, OBSCURE_CRITIC, UNSCORED_B, 3,
        4, 6, TMDB_WIDE, 7,
    ], "more votes first on a tie, unscored last, and the widely seen fill in once the household's end"
    assert order[12:] == [8, 9, TMDB_SHORT, OBSCURE_IMDB, OBSCURE_BOTH]


async def test_a_film_the_member_marked_not_seen_stays_with_the_crowd(house):
    db = house["db"]
    await _mark(db, house["jenny_id"], 2, 9)
    await _mark(db, house["patrick_id"], 9, state="unseen")
    assert await _order(house["patrick"], 6) == [2] + [t for t in TOP if t != 2]


async def test_an_inactive_members_watched_films_do_not_count(house):
    await _mark(house["db"], await _inactive(house["db"]), 9, 8)
    assert await _order(house["patrick"], 6) == TOP


async def test_the_bottom_step_opens_on_the_members_lowest_scored_watched_films(house):
    await _mark(house["db"], house["patrick_id"], 3, 5, OBSCURE_TMDB)
    assert (await _order(house["patrick"], 0))[:4] == [OBSCURE_TMDB, 5, 3, 9]


async def test_a_wished_stub_never_shows(house):
    db = house["db"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES (41, 'movie', 'Wished', 2020, 'wished')"
    )
    await db.execute(
        "INSERT INTO display.platform_rating (title_id, platform, metric, score, scale, votes) "
        "VALUES (41, 'imdb', 'user_score', 9.9, 10.0, 900000)"
    )
    await _mark(db, house["patrick_id"], 41)
    assert await _order(house["patrick"], 6) == TOP


async def test_a_step_pages_twelve_at_a_time_without_the_other_steps_picks(house):
    db, client = house["db"], house["patrick"]
    await _mark(db, house["patrick_id"], 1, 2, 3, 4)
    await _mark(db, house["jenny_id"], 5, 6, 7, 8, 9, TMDB_WIDE, OBSCURE_IMDB)
    first = await _films(client, 6)
    assert [f["id"] for f in first["films"]] == [
        1, 2, 3, TMDB_SHORT, 4, OBSCURE_IMDB, 5, OBSCURE_TMDB, 6, TMDB_WIDE, 7, OBSCURE_BOTH,
    ]
    assert first["more"] is True
    assert set(first["films"][0]) == {
        "id", "name", "original_name", "original_language", "year", "poster_path", "seen",
    }
    rest = await _films(client, 6, offset=12)
    assert [f["id"] for f in rest["films"]] == [8, 9, OBSCURE_CRITIC, UNSCORED_A, UNSCORED_B]
    assert rest["more"] is False

    # The picks leave before the slots are counted, so a page keeps its widely seen at 4, 8 and 12.
    out = f"1,{TMDB_SHORT}"
    first = await _films(client, 6, exclude=out)
    assert [f["id"] for f in first["films"]] == [
        2, 3, 4, OBSCURE_TMDB, OBSCURE_IMDB, 5, 6, OBSCURE_BOTH, TMDB_WIDE, 7, 8, OBSCURE_CRITIC,
    ]
    rest = await _films(client, 6, offset=12, exclude=out)
    assert [f["id"] for f in rest["films"]] == [9, UNSCORED_A, UNSCORED_B]
    assert rest["more"] is False


async def test_a_film_carries_the_members_own_watched_mark(house):
    await house["db"].execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen'), ($1, 2, 'unseen')",
        house["patrick_id"],
    )
    films = {f["id"]: f["seen"] for f in (await _films(house["patrick"], 6))["films"]}
    assert (films[1], films[2], films[3]) == (True, False, False)
    jenny = {f["id"]: f["seen"] for f in (await _films(house["jenny"], 6))["films"]}
    assert jenny[1] is False, "another member's Watched mark is theirs alone"


async def test_a_step_outside_the_set_is_a_422(house):
    for step in (-1, 7):
        response = await house["patrick"].get("/api/ladder/setup/films", params={"step": step})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "bad_step"


# --- the finish -----------------------------------------------------------------------------


async def _finish(client, picks):
    return await client.post(
        "/api/ladder/setup/finish",
        json={"picks": [{"title_id": t, "tier": tier} for t, tier in picks]},
    )


async def test_finishing_is_the_cut_over_and_each_pick_a_placement(house):
    db, patrick = house["db"], house["patrick_id"]
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 3, 'unseen')", patrick
    )
    response = await _finish(house["patrick"], [(1, 6), (2, 6), (3, 3), (OBSCURE_IMDB, 0)])
    assert response.status_code == 200, response.text

    assert await ladder.set_up_at(db, user_id=patrick) is not None
    edits = await db.fetch(
        "SELECT title_id, tier, via FROM tier_edit WHERE user_id = $1 ORDER BY title_id", patrick
    )
    assert [(r["title_id"], r["tier"], r["via"]) for r in edits] == [
        (1, 6, "explicit"), (2, 6, "explicit"), (3, 3, "explicit"), (OBSCURE_IMDB, 0, "explicit"),
    ]
    live = await db.fetch(
        f"SELECT title_id, value FROM ({observations.LIVE_LABEL_SQL}) l ORDER BY title_id", patrick
    )
    assert [(r["title_id"], r["value"]) for r in live] == [(1, 2), (2, 2), (3, 1), (OBSCURE_IMDB, 0)]
    assert set(
        await db.fetchval("SELECT array_agg(DISTINCT source) FROM verdict WHERE user_id = $1", patrick)
    ) == {"setup"}
    seen = await db.fetch(
        "SELECT title_id FROM user_title WHERE user_id = $1 AND state = 'seen' ORDER BY title_id",
        patrick,
    )
    assert [r["title_id"] for r in seen] == [1, 2, 3, OBSCURE_IMDB], "a pick implies seen"


async def test_the_done_screen_counts_each_step_and_names_its_first_pick(house):
    db, patrick = house["db"], house["patrick_id"]
    # Answered before the set-up: one film picked again, one series left waiting.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 5, 'seen'), ($1, $2, 'seen')",
        patrick, SERIES,
    )
    await observations.record_verdict(db, user_id=patrick, title_id=5, value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=SERIES, value=1)

    done = (await _finish(house["patrick"], [(2, 6), (1, 6), (5, 4)])).json()
    assert done["done"] is True and done["placed"] == 3
    assert (done["earlier_ratings"], done["rated_before"]) == (2, 1)
    tiers = done["tiers"]
    assert [t["tier"] for t in tiers] == [6, 5, 4, 3, 2, 1, 0]
    assert [t["word"] for t in tiers][:3] == ["All-time favourite", "Loved it", "Liked it"]
    assert [t["count"] for t in tiers] == [2, 0, 1, 0, 0, 0, 0]
    assert tiers[0]["first"] == {"id": 2, "name": "Film 2", "poster_path": None}
    assert tiers[2]["first"]["id"] == 5
    assert tiers[1]["first"] is None


async def test_finishing_ends_the_live_rate_session(house):
    db, patrick = house["db"], house["patrick_id"]
    await db.execute("INSERT INTO rate_session (user_id, kinds) VALUES ($1, ARRAY['movie'])", patrick)
    assert (await _finish(house["patrick"], [(1, 6)])).status_code == 200
    assert await db.fetchval(
        "SELECT count(*) FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", patrick
    ) == 0


async def test_a_second_finish_is_refused_and_the_films_route_closes(house):
    client = house["patrick"]
    assert (await _finish(client, [(1, 6)])).status_code == 200
    again = await _finish(client, [(2, 5)])
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["reason"] == "already_set_up"
    films = await client.get("/api/ladder/setup/films", params={"step": 6})
    assert films.status_code == 409 and films.json()["detail"]["reason"] == "already_set_up"
    assert (await client.get("/api/ladder/setup")).json()["done"] is True
    assert await house["db"].fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1", house["patrick_id"]
    ) == 1


@pytest.mark.parametrize(
    ("picks", "reason"),
    [
        ([], "empty"),
        ([(1, 6), (1, 5)], "duplicate"),
        ([(1, 6), (SERIES, 6)], "not_a_film"),
        ([(1, 7)], "bad_tier"),
        ([(1, -1)], "bad_tier"),
    ],
)
async def test_a_refused_finish_writes_nothing(house, picks, reason):
    response = await _finish(house["patrick"], picks)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == reason
    assert await ladder.set_up_at(house["db"], user_id=house["patrick_id"]) is None
    assert await house["db"].fetchval("SELECT count(*) FROM tier_edit") == 0


async def test_a_wished_stub_is_no_pick(house):
    """The search names a row's origin so the set-up leaves a stub out; a pick of one is refused."""
    db = house["db"]
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES (41, 'movie', 'Wished', 2020, 'wished')"
    )
    found = (await house["patrick"].get("/api/titles", params={"kind": "movie", "q": "wished"})).json()
    assert [(i["id"], i["origin"]) for i in found["items"]] == [(41, "wished")]

    response = await _finish(house["patrick"], [(1, 6), (41, 5)])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "not_a_film"
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0


async def test_one_members_set_up_leaves_the_others_untouched(house):
    assert (await _finish(house["patrick"], [(1, 6)])).status_code == 200
    jenny = (await house["jenny"].get("/api/ladder/setup")).json()
    assert jenny["done"] is False
    assert (await _films(house["jenny"], 6))["films"][0]["id"] == 1, "picks are per member"
