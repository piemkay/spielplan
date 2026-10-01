"""The ladder's set-up (§6.1, decision 547): its steps, each step's films in platform-score order, and the
finish that is the member's cut-over (decision 537). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.ledger import ladder, observations
from spielplan.rate import setup
from tests.helpers import household

BUNDLE = "test-setup-v1"

# Well-known films (crowd support >= 1000), IMDb 9.0 down to 5.0: an exact 1/8 between neighbours.
KNOWN = tuple(range(1, 10))
# Less known, each scored another way, the first above every well-known film.
OBSCURE_IMDB, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC = 11, 12, 13, 14
# No platform score at all: the better known first.
UNSCORED_KNOWN, UNSCORED = 21, 22
SERIES = 31


async def _seed(db) -> None:
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest) VALUES ($1, '{}'::jsonb)", BUNDLE
    )
    films = KNOWN + (OBSCURE_IMDB, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC, UNSCORED_KNOWN, UNSCORED)
    for title_id in films + (SERIES,):
        await db.execute(
            "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, 2000, $4)",
            title_id, "series" if title_id == SERIES else "movie", f"Film {title_id}",
            title_id != UNSCORED,
        )
    support = {t: 5000 for t in KNOWN} | {UNSCORED_KNOWN: 2000, UNSCORED: 50, SERIES: 9000}
    for title_id in films + (SERIES,):
        await db.execute(
            "INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source) "
            "VALUES ($1, $2, 0.5, 0.5, $3, 0.9, 'backbone')",
            title_id, BUNDLE, support.get(title_id, 100),
        )
    rows = [(t, "imdb", "user_score", 9.0 - 0.5 * i, 10.0) for i, t in enumerate(KNOWN)]
    rows += [
        (OBSCURE_IMDB, "imdb", "user_score", 9.5, 10.0),
        (OBSCURE_TMDB, "tmdb", "user_score", 7.0, 10.0),
        # IMDb wins over TMDB, so this one sits low.
        (OBSCURE_BOTH, "imdb", "user_score", 4.0, 10.0),
        (OBSCURE_BOTH, "tmdb", "user_score", 9.9, 10.0),
        # Neither IMDb nor TMDB: the mean of the others, each over its own scale (0.30).
        (OBSCURE_CRITIC, "metacritic", "critic_score", 20.0, 100.0),
        (OBSCURE_CRITIC, "rottentomatoes", "audience_score", 40.0, 100.0),
        (OBSCURE_CRITIC, "tmdb", "popularity", 99.0, None),
        (SERIES, "imdb", "user_score", 9.9, 10.0),
    ]
    await db.executemany(
        "INSERT INTO display.platform_rating (title_id, platform, metric, score, scale) "
        "VALUES ($1, $2, $3, $4, $5)",
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


LESS_KNOWN_BY_SCORE = [OBSCURE_IMDB, OBSCURE_TMDB, OBSCURE_BOTH, OBSCURE_CRITIC]


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
    tap = " Tap the ones you remember well."
    assert [s["hint"] for s in steps] == (
        ["Highest rated first." + tap] * 3 + ["From the middle." + tap]
        + ["Lowest rated first." + tap] * 3
    )
    assert all(set(s) == {"tier", "word", "hint"} for s in steps)


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


# --- the films ------------------------------------------------------------------------------


async def test_the_top_step_lists_well_known_films_best_first_then_the_rest_then_no_score(house):
    order = await _order(house["patrick"], 6)
    assert order == list(KNOWN) + LESS_KNOWN_BY_SCORE + [UNSCORED_KNOWN, UNSCORED]
    assert SERIES not in order, "the set-up is for films only"


async def test_the_bottom_step_lists_each_group_lowest_first_and_no_score_still_last(house):
    order = await _order(house["patrick"], 0)
    assert order == list(reversed(KNOWN)) + LESS_KNOWN_BY_SCORE[::-1] + [UNSCORED_KNOWN, UNSCORED]


async def test_a_middle_step_opens_at_its_start_and_breaks_ties_toward_the_middle(house):
    # A+ opens at 8%: the second-best well-known film is nearer it than the best.
    assert (await _order(house["patrick"], 5))[:3] == [2, 1, 3]
    # A opens at 25%, on the third; films 4 and 2 sit an eighth either side, 5 and 1 a quarter.
    assert (await _order(house["patrick"], 4))[:9] == [3, 4, 2, 5, 1, 6, 7, 8, 9]


async def test_picks_of_the_other_steps_are_left_out(house):
    order = await _order(house["patrick"], 6, exclude=f"1,{OBSCURE_IMDB},{UNSCORED}")
    assert order == list(KNOWN[1:]) + LESS_KNOWN_BY_SCORE[1:] + [UNSCORED_KNOWN]


async def test_a_step_pages_twelve_at_a_time_and_says_when_the_list_ends(house):
    client = house["patrick"]
    first = await _films(client, 6)
    assert len(first["films"]) == 12 and first["more"] is True
    rest = await _films(client, 6, offset=12)
    assert len(rest["films"]) == 3 and rest["more"] is False
    ids = [f["id"] for f in first["films"] + rest["films"]]
    assert ids == await _order(client, 6)
    film = first["films"][0]
    assert set(film) == {
        "id", "name", "original_name", "original_language", "year", "poster_path", "seen",
    }


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


async def test_one_members_set_up_leaves_the_others_untouched(house):
    assert (await _finish(house["patrick"], [(1, 6)])).status_code == 200
    jenny = (await house["jenny"].get("/api/ladder/setup")).json()
    assert jenny["done"] is False
    assert (await _films(house["jenny"], 6))["films"][0]["id"] == 1, "picks are per member"
