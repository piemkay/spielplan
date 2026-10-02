"""Decisions 559 and 560 over Postgres: the term table, its invalidation, and `/api/mix`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spielplan.db import library
from spielplan.home import mix, mix_table, why
from tests.helpers import household

VOCAB = "v1"
BUNDLE = "b-mix"
FACETS = ("mood", "sensibility", "register", "visual", "sound", "pacing", "structure", "place", "era",
          "characters", "themes")
COMMON = ["mood.calm", "themes.life", "register.plain"]
HEIST = ["themes.heist", "structure.whodunit"]
ANCHOR, LIKED, THIN, SERIES = 100, 300, 400, 500
LIBRARY = list(range(101, 113))
BEYOND, OBSCURE = [201, 202, 203], 204
LABELS = {"structure.whodunit": "murder mystery", "themes.heist": "heist"}


async def _terms(db, title_id: int, terms, *, extracted=()) -> None:
    for term in terms:
        if term in extracted:
            await db.execute(
                "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources,"
                " provider) VALUES ($1, $2, $3, $4, 3, 0.9, 2, '')",
                title_id, VOCAB, term, term.split(".")[0],
            )
        else:
            await db.execute(
                "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
                " VALUES ($1, $2, $3, $4, 1, 'test')",
                title_id, VOCAB, term, term.split(".")[0],
            )


async def _seed(db) -> None:
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 11, 0)", VOCAB
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord, colour) VALUES ($1, $2, $3, $4)",
        [(VOCAB, f, i, f"#00000{i % 10}") for i, f in enumerate(FACETS)],
    )
    vocabulary = [*COMMON, *HEIST, "mood.dark", "visual.neon", "visual.rain", "pacing.fast",
                  "pacing.kinetic", "pacing.slow", "sound.drone", "sound.choral", "mood.warm"]
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, label) VALUES ($1, $2, $3, $4)",
        [(VOCAB, t, t.split(".")[0], LABELS.get(t)) for t in vocabulary],
    )
    titles = [(i, "movie", f"Filler {i}", 1980, True) for i in range(1, 61)]
    titles += [(ANCHOR, "movie", "Anchor", 2019, True), (113, "movie", "Half", 2001, True),
               (LIKED, "movie", "Liked", 2015, True), (THIN, "movie", "Thin", 2002, True),
               (SERIES, "series", "Heist Show", 2020, True), (OBSCURE, "movie", "Obscure", 2010, False)]
    titles += [(t, "movie", f"Match {t}", 1995 if t < 107 else 2005, True) for t in LIBRARY]
    titles += [(t, "movie", f"Known {t}", 2010, False) for t in BEYOND]
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, $4, $5)", titles
    )
    for i in range(1, 61):
        await _terms(db, i, [*COMMON, "pacing.slow"])
    await _terms(db, ANCHOR, [*COMMON, *HEIST, "mood.dark", "visual.neon", "visual.rain"], extracted=HEIST)
    for t in LIBRARY:
        await _terms(db, t, [*HEIST, "mood.calm", "pacing.fast"])
    await _terms(db, 113, ["themes.heist", "mood.calm"])
    await _terms(db, LIKED, ["pacing.fast", "pacing.kinetic", "sound.drone", "sound.choral", "mood.warm"])
    await _terms(db, THIN, ["mood.calm"])
    await _terms(db, SERIES, [*HEIST, "mood.dark"])
    for t in [*BEYOND, OBSCURE]:
        await _terms(db, t, HEIST)
    # Decision 556's votes: IMDb's count, which the well-known floor reads.
    await db.executemany(
        "INSERT INTO display.platform_rating (title_id, platform, metric, score, scale, votes)"
        " VALUES ($1, 'imdb', 'user_score', 7.5, 10, $2)",
        [(t, 30_000) for t in BEYOND] + [(OBSCURE, 1_000)],
    )


@pytest.fixture
async def world(db, app):
    await _seed(db)
    _admin, member = await household(app)
    member_id = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    return SimpleNamespace(db=db, client=member, user_id=member_id)


async def _mix(client, path="/api/mix/titles", **params):
    kinds = params.pop("kind", "movie")
    query = [("kind", k) for k in ([kinds] if isinstance(kinds, str) else kinds)]
    for key in ("like", "less"):
        query += [(key, v) for v in params.pop(key, [])]
    query += list(params.items())
    return await client.get(path, params=query)


async def _ok(client, path="/api/mix/titles", **params):
    response = await _mix(client, path, **params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body) -> list[int]:
    out = []
    for item in body["items"]:
        out += [i["id"] for i in item["fold"]["items"]] if "fold" in item else [item["id"]]
    return out


async def test_the_tables_whole_film_cosine_is_the_cards_likeness(db):
    """Decision 559: a film's likeness to a recipe film is decision 513's, as Shares a lot with reads it."""
    await _seed(db)
    table = await mix_table.table_for(SimpleNamespace(), db, "movie")
    recipe = [mix.Ingredient(ANCHOR)]
    score, _gate = mix.dna_score(table, recipe, {ANCHOR: table.operand(ANCHOR)})
    candidates = [101, 113, 201, OBSCURE, 7, LIKED]
    likes = await why.likeness(db, title_id=ANCHOR, candidate_ids=candidates, kind="movie", version=VOCAB)
    assert set(likes) == {101, 113, 201, OBSCURE, 7}
    for title_id, like in likes.items():
        assert score[table.row_of[title_id]] == pytest.approx(like.likeness, rel=1e-5), title_id
    assert score[table.row_of[LIKED]] == 0


async def test_the_table_follows_dna_writes_and_owned_flips(db, monkeypatch):
    await _seed(db)
    state = SimpleNamespace()
    builds = []
    real = mix_table._build

    async def counted(*args, **kwargs):
        builds.append(args[2])
        return await real(*args, **kwargs)

    monkeypatch.setattr(mix_table, "_build", counted)
    first = await mix_table.table_for(state, db, "movie")
    assert await mix_table.table_for(state, db, "movie") is first
    assert builds == ["movie"]

    whodunit, heist = first.terms.index("structure.whodunit"), first.terms.index("themes.heist")
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES (113, $1, 'structure.whodunit', 'structure', 2, ''),"
        " (113, $1, 'themes.heist', 'themes', 2, '')",
        VOCAB,
    )
    tagged = await mix_table.table_for(state, db, "movie")
    assert whodunit in tagged.operand(113).cols and len(builds) == 2
    # Heist is now in both tiers: one term, quoted.
    op = tagged.operand(113)
    assert list(op.cols).count(heist) == 1 and op.quoted[list(op.cols).index(heist)]

    # Stage 8's own upsert.
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES (113, $1, 'visual.neon', 'visual', 1, 'test')"
        " ON CONFLICT (title_id, version, term) DO UPDATE SET weight = EXCLUDED.weight",
        VOCAB,
    )
    projected = await mix_table.table_for(state, db, "movie")
    assert projected.terms.index("visual.neon") in projected.operand(113).cols and len(builds) == 3

    await db.execute("UPDATE title SET is_owned = false WHERE id = 105")
    flipped = await mix_table.table_for(state, db, "movie")
    assert len(builds) == 3, "an owned flip re-derives the library without refetching the terms"
    assert flipped.cols is projected.cols
    assert not flipped.owned[flipped.row_of[105]] and flipped.n_owned == projected.n_owned - 1

    # A curator's repoint renames a tag in place: same count, same ids.
    await db.execute(
        "UPDATE dna_tag SET term = 'mood.dark', facet = 'mood'"
        " WHERE title_id = 113 AND version = $1 AND term = 'structure.whodunit'",
        VOCAB,
    )
    repointed = await mix_table.table_for(state, db, "movie")
    assert len(builds) == 4 and whodunit not in repointed.operand(113).cols


async def test_a_recipe_lists_the_library_first_and_only_well_known_titles_beyond(world):
    await world.db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 101, 'seen')", world.user_id
    )
    body = await _ok(world.client, like=[str(ANCHOR)])
    assert (body["kind"], body["pool"], body["sort"]) == ("movie", "library", "match")
    assert body["library_total"] == body["total"] == len(LIBRARY)
    assert body["beyond_total"] == len(BEYOND), "1,000 votes is not well known"
    assert body["strong_total"] is None
    assert sorted(_ids(body)) == LIBRARY
    seen = next(i for i in body["items"] if i["id"] == 101)
    assert seen["seen_state"] == "seen", "a seen film stays, with its mark"
    assert seen["is_owned"] is True and "match" not in seen
    assert seen["why"] == [{
        "title_id": ANCHOR, "name": "Anchor", "groups": [], "like": True,
        "terms": [
            {"term": "structure.whodunit", "label": "murder mystery", "facet": "structure",
             "quoted": False},
            {"term": "themes.heist", "label": "heist", "facet": "themes", "quoted": False},
        ],
    }]

    beyond = await _ok(world.client, like=[str(ANCHOR)], pool="beyond")
    assert sorted(_ids(beyond)) == BEYOND and beyond["total"] == len(BEYOND)
    assert all(not i["is_owned"] for i in beyond["items"])


async def test_the_recipe_head_carries_each_film_its_sheet_and_the_derived_terms(world):
    body = await _ok(world.client, like=[str(ANCHOR), f"{LIKED}:pace"], less=[str(SERIES)])
    recipe = body["recipe"]
    assert recipe["asks_for_like"] is False
    anchor, liked, series = recipe["ingredients"]
    assert (anchor["title_id"], anchor["like"], anchor["groups"]) == (ANCHOR, True, [])
    assert (liked["groups"], series["like"], series["kind"]) == (["pace"], False, "series")
    assert [t["term"] for t in liked["terms"]] == ["pacing.kinetic", "pacing.fast"], "the rarer first"
    assert [s["group"] for s in anchor["sheet"]] == list(mix.GROUPS)
    look = next(s for s in anchor["sheet"] if s["group"] == "look")
    assert (look["name"], look["offered"], look["colour"]) == ("Look", True, "#000003")
    assert {t["term"] for t in look["inferred"]} == {"visual.neon", "visual.rain"} and look["quoted"] == []
    sound = next(s for s in liked["sheet"] if s["group"] == "sound")
    mood = next(s for s in liked["sheet"] if s["group"] == "mood")
    assert sound["offered"] is True and mood["offered"] is False
    assert [t["term"] for t in recipe["more"][:2]] == ["structure.whodunit", "themes.heist"]
    assert [t["quoted"] for t in recipe["more"]] == [True, True, False], "quoted terms first"
    assert recipe["less"] == [], "the series carries nothing the liked films do not"


async def test_a_recipe_film_of_the_other_kind_ranks_this_kind(world):
    body = await _ok(world.client, like=[str(SERIES)])
    assert sorted(_ids(body)) == [ANCHOR, *LIBRARY]


async def test_no_liked_film_asks_for_one(world):
    body = await _ok(world.client, less=[str(ANCHOR)])
    assert body["recipe"]["asks_for_like"] is True
    assert body["items"] == [] and body["total"] == body["library_total"] == body["beyond_total"] == 0


async def test_a_catalogue_filter_narrows_the_recipe_in_its_order(world):
    everything = _ids(await _ok(world.client, like=[str(ANCHOR)]))
    nineties = await _ok(world.client, like=[str(ANCHOR)], decade=1990)
    assert _ids(nineties) == [t for t in everything if t < 107]
    assert nineties["library_total"] == 6 and nineties["beyond_total"] == 0


async def test_an_include_folds_inside_the_librarys_list(db):
    """Decision 559 item 2: strong first, then weak, each in recipe order; `match` only then."""
    await _seed(db)
    eligible = library.Eligible(ids=frozenset([*LIBRARY, *BEYOND]), strong=frozenset({110, 111, 201}))
    page = await mix_table.recipe_page(
        SimpleNamespace(), db, user_id=1, kind="movie", recipe=[mix.Ingredient(ANCHOR)], eligible=eligible
    )
    assert page["strong_total"] == 2
    assert _ids(page) == [110, 111] + [t for t in LIBRARY if t not in (110, 111)]
    assert [i["match"] for i in page["items"]] == ["strong"] * 2 + ["weak"] * 10
    beyond = await mix_table.recipe_page(
        SimpleNamespace(), db, user_id=1, kind="movie", recipe=[mix.Ingredient(ANCHOR)], eligible=eligible,
        pool="beyond",
    )
    assert beyond["strong_total"] is None and _ids(beyond) == BEYOND
    assert all("match" not in i for i in beyond["items"])


async def test_for_you_reorders_only_when_the_members_ratings_rank_the_kind(world):
    match = _ids(await _ok(world.client, like=[str(ANCHOR)]))
    unavailable = await _ok(world.client, like=[str(ANCHOR)], sort="for_you")
    assert (unavailable["sort"], unavailable["for_you_available"]) == ("match", False)
    assert _ids(unavailable) == match

    db = world.db
    await db.execute("INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}', 'active')",
                     BUNDLE)
    await db.execute(
        "INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count, bundle_version)"
        " VALUES ($1, 'movie', 'foldin', $2, 0.5, 10, $3)",
        world.user_id, b"\x00" * 8, BUNDLE,
    )
    await db.executemany(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf)"
        " VALUES ($1, $2, 'movie', $3, $4, 0)",
        [(world.user_id, 112, BUNDLE, 0.9), (world.user_id, 101, BUNDLE, 0.5)],
    )
    personal = await _ok(world.client, like=[str(ANCHOR)], sort="for_you")
    assert (personal["sort"], personal["for_you_available"]) == ("for_you", True)
    assert _ids(personal) == [112, 101] + [t for t in match if t not in (101, 112)]


async def test_the_first_page_caps_a_director_at_two_films(world):
    db = world.db
    await db.execute("INSERT INTO person (id, name) VALUES (901, 'Joel Coen'), (902, 'Ethan Coen')")
    await db.executemany(
        "INSERT INTO credit (title_id, person_id, role_class, job) VALUES ($1, $2, 'director', 'Director')",
        [(t, p) for t in (102, 104, 106, 108) for p in (901, 902)],
    )
    first = await _ok(world.client, like=[str(ANCHOR)])
    folds = [i["fold"] for i in first["items"] if "fold" in i]
    assert len(folds) == 1
    assert (folds[0]["person_ids"], folds[0]["name"]) == ([901, 902], "Joel Coen & Ethan Coen")
    assert [i["id"] for i in folds[0]["items"]] == [106, 108]
    assert len(_ids(first)) == len(LIBRARY)
    later = await _ok(world.client, like=[str(ANCHOR)], offset=2, limit=10)
    assert not any("fold" in i for i in later["items"]), "only the first page is capped"
    thin = await _ok(world.client, like=[str(ANCHOR)], decade=1990)
    assert not any("fold" in i for i in thin["items"]) and sorted(_ids(thin)) == LIBRARY[:6], (
        "under ten fits each shows, the Coens' third film of the nineties too"
    )


@pytest.mark.parametrize(
    ("like", "less", "reason"),
    [
        ([str(ANCHOR), "101", "102", "103", "104"], [], "too_many_films"),
        ([f"{ANCHOR}:look", f"{LIKED}:pace", f"{SERIES}:storytelling"], [], "too_many_lending"),
        ([f"{ANCHOR}:look"], [f"{LIKED}:look"], "group_taken_twice"),
        ([f"{LIKED}:mood"], [], "group_too_thin"),
        ([str(THIN)], [], "no_dna"),
        ([f"{ANCHOR}:colour"], [], "unknown_group"),
        (["heat"], [], "bad_ingredient"),
    ],
)
async def test_each_refusal_names_its_limit(world, like, less, reason):
    for path in ("/api/mix/titles", "/api/mix/twists"):
        response = await _mix(world.client, path, like=like, less=less)
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["reason"] == reason
        assert detail["message"]
        assert set(detail) == {"reason", "message", "title_id", "group"}


async def test_a_twist_takes_a_group_of_a_film_placed_high(world):
    assert (await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)]))["twists"] == []

    db = world.db
    await db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, n_levels, via) VALUES ($1, $2, 6, 7, 'explicit')",
        world.user_id, LIKED,
    )
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')", world.user_id, LIKED
    )
    body = await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)], seed=3)
    assert body["seed"] == 3
    (twist,) = body["twists"]
    assert (twist["kind"], twist["title_id"], twist["name"], twist["group"], twist["group_name"]) == (
        "movie", LIKED, "Liked", "pace", "Pace"
    )
    assert [t["term"] for t in twist["terms"]] == ["pacing.kinetic", "pacing.fast"]
    assert twist["library_n"] == len(LIBRARY), "sound would leave none; mood is one term"
    narrowed = await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)], decade=1990)
    assert narrowed["twists"] == [], "six films of the nineties fit, under ten"


async def _placed_high(world, title_id: int) -> None:
    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, n_levels, via) VALUES ($1, $2, 6, 7, 'explicit')",
        world.user_id, title_id,
    )
    await world.db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')", world.user_id, title_id
    )


async def test_on_both_a_series_placed_high_is_a_twist_counted_in_the_series_library(world):
    """Ten owned series share the anchor's heist and the show's pace, among fifty that make heist rare."""
    db, show = world.db, 501
    shows, filler = list(range(510, 520)), list(range(520, 570))
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'series', $2, 2010, true)",
        [(t, f"Show {t}") for t in [show, *shows, *filler]],
    )
    await _terms(db, show, ["pacing.fast", "pacing.kinetic", "mood.warm"])
    for t in shows:
        await _terms(db, t, [*HEIST, "pacing.fast"])
    for t in filler:
        await _terms(db, t, COMMON)
    await _placed_high(world, LIKED)
    await _placed_high(world, show)

    def offered(body):
        return {(t["kind"], t["title_id"], t["group"], t["library_n"]) for t in body["twists"]}

    both = await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)], kind=["movie", "series"])
    assert offered(both) == {("movie", LIKED, "pace", len(LIBRARY)), ("series", show, "pace", len(shows))}
    films = await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)])
    assert offered(films) == {("movie", LIKED, "pace", len(LIBRARY))}, "a series only where series show"

    await db.execute("DELETE FROM dna_projected WHERE title_id = $1 AND term = 'pacing.fast'", shows[0])
    both = await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)], kind=["movie", "series"])
    assert offered(both) == {("movie", LIKED, "pace", len(LIBRARY))}, "nine series would fit, under ten"


async def test_the_picker_finds_either_kind_with_two_terms(world):
    found = await _ok(world.client, "/api/mix/films", q="heist")
    assert [i["id"] for i in found["items"]] == [SERIES]
    assert set(found["items"][0]) == {"id", "kind", "name", "year", "poster_path", "is_owned",
                                      "original_name", "original_language"}
    assert [i["id"] for i in (await _ok(world.client, "/api/mix/films", q="thin"))["items"]] == []
    known = await _ok(world.client, "/api/mix/films", q="known", limit=2)
    assert [i["id"] for i in known["items"]] == BEYOND[:2]
    assert (await _ok(world.client, "/api/mix/films", q=" "))["items"] == []


async def test_the_picker_never_offers_a_wished_stub(world):
    """Even one carrying terms; it is offered once it arrives (decision 559 item 7)."""
    await world.db.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES (600, 'movie', 'Heist', 2024, 'wished')"
    )
    await _terms(world.db, 600, HEIST)
    found = await _ok(world.client, "/api/mix/films", q="heist")
    assert [i["id"] for i in found["items"]] == [SERIES]
    await world.db.execute("UPDATE title SET origin = 'acquired' WHERE id = 600")
    found = await _ok(world.client, "/api/mix/films", q="heist")
    assert sorted(i["id"] for i in found["items"]) == [SERIES, 600]


async def test_no_payload_carries_a_score_or_a_model_number(world):
    bodies = [
        await _ok(world.client, like=[str(ANCHOR), f"{LIKED}:pace"], less=[str(SERIES)]),
        await _ok(world.client, "/api/mix/twists", like=[str(ANCHOR)]),
    ]

    def keys(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield key
                yield from keys(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from keys(inner)

    for body in bodies:
        found = set(keys(body))
        assert not found & {"score", "likeness", "idf", "cosine", "weight", "rank", "b", "beta", "gate"}
