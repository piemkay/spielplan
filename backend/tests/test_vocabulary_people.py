"""The two pickers' reads (decision 557): the vocabulary with its library counts, and the people
typeahead folded as the title card folds credits. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.db import dna_terms, people

VOCAB = "v1"
TMDB_FACE = "https://image.tmdb.org/t/p/w185/caine.jpg"


async def _titles(db) -> None:
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, 2000, $4)",
        [(1, "movie", "Owned One", True), (2, "movie", "Owned Two", True),
         (3, "movie", "Not Owned", False), (4, "series", "A Series", True)],
    )


@pytest.fixture
async def vocabulary(db):
    await _titles(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 2, 3)", VOCAB
    )
    # `ord` against the names' order, so a listing by name fails.
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord, colour) VALUES ($1, $2, $3, $4)",
        [(VOCAB, "themes", 0, "#2d6a4f"), (VOCAB, "mood", 1, "#c8613a")],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, label, gloss) VALUES ($1, $2, $3, $4, $5)",
        [(VOCAB, "mood.cozy", "mood", "cozy & mellow", "warm and unhurried"),
         (VOCAB, "mood.slow_burn", "mood", None, None),
         (VOCAB, "themes.heist", "themes", "heist", None)],
    )
    await db.executemany(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ($1, $2, $3, $4)",
        [(VOCAB, "cosy", "mood.cozy", None), (VOCAB, "comfy", "mood.cozy", "lexicon"),
         (VOCAB, "caper", "themes.heist", None)],
    )
    # Title 1 carries cozy in both tiers and counts once; title 3 is not owned.
    await db.executemany(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES ($1, $2, $3, $4, 2, '')",
        [(1, VOCAB, "mood.cozy", "mood"), (3, VOCAB, "mood.cozy", "mood"),
         (4, VOCAB, "mood.cozy", "mood")],
    )
    await db.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) VALUES ($1, $2, $3, $4, 1)",
        [(1, VOCAB, "mood.cozy", "mood"), (2, VOCAB, "mood.cozy", "mood"),
         (2, VOCAB, "themes.heist", "themes")],
    )


async def test_the_vocabulary_lists_facets_in_order_and_counts_terms_in_the_library(db, vocabulary):
    films = await dna_terms.vocabulary(db, kinds=["movie"])
    assert films["version"] == VOCAB
    assert films["facets"] == [
        {"facet": "themes", "colour": "#2d6a4f"}, {"facet": "mood", "colour": "#c8613a"}
    ]
    assert [t["term"] for t in films["terms"]] == ["themes.heist", "mood.cozy", "mood.slow_burn"]
    cozy = next(t for t in films["terms"] if t["term"] == "mood.cozy")
    assert cozy == {
        "term": "mood.cozy", "facet": "mood", "label": "cozy & mellow", "gloss": "warm and unhurried",
        "aliases": ["comfy", "cosy"], "owned": 2,
    }
    unlabelled = next(t for t in films["terms"] if t["term"] == "mood.slow_burn")
    assert (unlabelled["label"], unlabelled["aliases"], unlabelled["owned"]) == ("slow burn", [], 0)

    by_kind = {
        kinds: {t["term"]: t["owned"] for t in (await dna_terms.vocabulary(db, kinds=kinds))["terms"]}
        for kinds in (("series",), ("movie", "series"))
    }
    assert by_kind[("series",)]["mood.cozy"] == 1
    assert by_kind[("movie", "series")]["mood.cozy"] == 3
    assert by_kind[("movie", "series")]["themes.heist"] == 1


async def test_the_vocabulary_route(app, db, vocabulary):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    answer = await client.get("/api/vocabulary", params=[("kind", "movie"), ("kind", "series")])
    assert answer.status_code == 200, answer.text
    assert {t["term"]: t["owned"] for t in answer.json()["terms"]}["mood.cozy"] == 3
    assert (await client.get("/api/vocabulary")).status_code == 422


async def test_an_empty_vocabulary_answers_an_empty_payload(db):
    assert await dna_terms.vocabulary(db, kinds=["movie"]) == {
        "version": None, "facets": [], "terms": []
    }


@pytest.fixture
async def credited(db):
    await _titles(db)
    await db.executemany(
        "INSERT INTO person (id, name, imdb_id, tmdb_id, profile_path) VALUES ($1, $2, $3, $4, $5)",
        [(10, "Michael Caine", "nm0000323", None, None),
         (11, "Michael Caine", "nm0000323", 3895, TMDB_FACE),
         (12, "Michaela Coel", None, None, None),
         (13, "Jean-Michel Jarre", None, None, None),
         (14, "Anne Emichson", None, None, None),
         (15, "John Williams", "nm0002354", None, None),
         (16, "John Williams", "nm9999999", None, None),
         (17, "Hans Zimmer", None, None, None)],
    )
    await db.executemany(
        "INSERT INTO credit (title_id, person_id, job, source, role_class) VALUES ($1, $2, $3, $4, $5)",
        [(1, 10, "Actor", "tmdb", "cast"), (2, 10, "Actor", "tmdb", "cast"),
         (1, 11, "Actor", "imdb", "cast"), (3, 11, "Actor", "tmdb", "cast"),
         (4, 12, "Actor", "tmdb", "cast"),
         (3, 13, "Original Music Composer", "tmdb", "composer"),
         (1, 14, "Actor", "tmdb", "cast"),
         (1, 15, "Original Music Composer", "tmdb", "composer"),
         (3, 16, "Original Music Composer", "tmdb", "composer"),
         (1, 17, "Original Music Composer", "tmdb", "composer"),
         (2, 17, "Original Music Composer", "tmdb", "composer"),
         (3, 17, "Actor", "tmdb", "cast")],
    )


async def test_the_people_typeahead_matches_word_starts_and_folds_one_human(db, credited):
    found = await people.search_people(db, q="Mich", kinds=["movie"])
    assert found == [
        {"person_ids": [10, 11], "person_id": 11, "name": "Michael Caine", "photo": True,
         "role": "cast", "owned": 2, "titles": 3},
        {"person_ids": [13], "person_id": 13, "name": "Jean-Michel Jarre", "photo": False,
         "role": "composer", "owned": 0, "titles": 1},
    ], "Michaela Coel is credited on a series alone, and Emichson only contains the letters"
    assert [p["name"] for p in await people.search_people(db, q="mich", kinds=["series"])] == [
        "Michaela Coel"
    ]
    assert len(await people.search_people(db, q="mich", kinds=["movie"], limit=1)) == 1


async def test_the_people_typeahead_keeps_apart_what_the_ids_say_are_two(db, credited):
    williams = await people.search_people(db, q="williams", kinds=["movie"])
    assert [(p["person_ids"], p["owned"]) for p in williams] == [([15], 1), ([16], 0)]


async def test_a_person_is_named_by_their_most_frequent_role(db, credited):
    [zimmer] = await people.search_people(db, q="zim", kinds=["movie"])
    assert (zimmer["role"], zimmer["owned"], zimmer["titles"]) == ("composer", 2, 3)


async def test_the_people_route_answers_no_one_under_two_characters(app, db, credited):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    short = await client.get("/api/people", params={"q": "m", "kind": "movie"})
    assert short.status_code == 200 and short.json() == {"people": []}
    named = await client.get("/api/people", params={"q": "jean mi", "kind": "movie"})
    assert [p["person_id"] for p in named.json()["people"]] == [13]
