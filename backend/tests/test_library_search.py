"""The catalog's search order and genre facet (decisions 472, 473). Needs TEST_DATABASE_URL.

Every case is built so the old year-ordered search and raw genre labels fail it."""

from __future__ import annotations

import pytest

from spielplan.db import genres, library

BUNDLE = "test-search-v1"


async def _bundle(db) -> None:
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )


async def _title(
    db, title_id: int, name: str, year: int | None, *, kind: str = "movie", owned: bool = False,
    item_n: int | None = None, aliases: tuple[str, ...] = (),
) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, $4, $5)",
        title_id, kind, name, year, owned,
    )
    if item_n is not None:
        await db.execute(
            "INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source) "
            "VALUES ($1, $2, 0.5, 0.5, $3, 0.9, 'backbone')",
            title_id, BUNDLE, item_n,
        )
    for alias in aliases:
        await db.execute(
            "INSERT INTO title_alias (title_id, alias) VALUES ($1, $2)", title_id, alias
        )


async def _names(db, q: str, **kwargs) -> list[str]:
    rows, _ = await library.list_titles(db, kinds=kwargs.pop("kinds", ["movie"]), q=q, **kwargs)
    return [r["name"] for r in rows]


async def test_an_exact_title_is_the_first_search_hit(db):
    """"heat" found Heat tenth, behind newer titles that only contain the letters."""
    await _bundle(db)
    await _title(db, 1, "Heat", 1995, item_n=35000)
    await _title(db, 2, "National Theatre Live: Heatwave", 2022, item_n=10)
    await _title(db, 3, "Frozen II", 2019, item_n=9000, aliases=("Regatul de Gheata II",))
    await _title(db, 4, "Theatre of Blood", 2021, item_n=900)
    names = await _names(db, "heat")
    assert names[0] == "Heat", names
    assert set(names) == {"Heat", "National Theatre Live: Heatwave", "Frozen II",
                          "Theatre of Blood"}, "the order changed, the predicate must not"


async def test_search_tiers_run_exact_then_prefix_then_word_then_substring(db):
    """Dated so the year order the search used to keep is the exact reverse of the answer."""
    await _bundle(db)
    expected = [
        "Up",                   # the whole title
        "Up in Smoke",          # the phrase starts it
        "Upgrade",              # a word starting it begins with the phrase
        "Wake Up Dead Man",     # the phrase as a whole word anywhere
        "Start Upright",        # a word anywhere begins with it
        "Supernova",            # anywhere inside a word
    ]
    for i, name in enumerate(expected):
        await _title(db, 10 + i, name, 1990 + i, item_n=100)
    assert await _names(db, "up") == expected


async def test_a_leading_article_does_not_bury_the_title(db):
    """Read with the article removed, "Godfather" and The Godfather are both exact and the crowd decides."""
    await _bundle(db)
    await _title(db, 20, "Godfather", 1991, item_n=25)
    await _title(db, 21, "The Godfather", 1972, item_n=180000)
    await _title(db, 22, "The Godfather Part II", 1974, item_n=120000)
    await _title(db, 23, "Godfather Mendoza", 1934, item_n=23)
    assert await _names(db, "godfather") == [
        "The Godfather", "Godfather", "The Godfather Part II", "Godfather Mendoza",
    ]


async def test_a_name_match_outranks_an_alias_match_of_the_same_quality(db):
    await _bundle(db)
    await _title(db, 30, "Solaris", 1972, item_n=100)
    await _title(db, 31, "Solyaris Remastered", 2002, item_n=90000, aliases=("Solaris",))
    assert await _names(db, "solaris") == ["Solaris", "Solyaris Remastered"]


async def test_the_household_owned_copy_breaks_a_tie_before_the_crowd_does(db):
    await _bundle(db)
    await _title(db, 40, "Heat", 1995, item_n=35000)
    await _title(db, 41, "Heat", 1986, owned=True, item_n=500)
    rows, _ = await library.list_titles(db, kinds=["movie"], q="heat")
    assert [r["id"] for r in rows] == [41, 40]


async def test_the_crowd_tie_break_is_read_within_each_kind(db):
    """`item_n` sits on two scales (film median 265, series 0), so it is read as a percentile per kind."""
    await _bundle(db)
    await _title(db, 50, "The Bear", 1988, item_n=4617)
    await _title(db, 51, "The Bear", 2022, kind="series", item_n=47)
    for i in range(4):
        await _title(db, 60 + i, f"Unrelated Film {i}", 2000, item_n=10000 + i)
    for i in range(3):
        await _title(db, 70 + i, f"Unrelated Series {i}", 2000, kind="series", item_n=0)
    rows, _ = await library.list_titles(db, kinds=["movie", "series"], q="the bear")
    assert [r["id"] for r in rows] == [51, 50]


async def test_a_hit_is_looser_only_when_its_name_and_aliases_contain_the_query_inside_a_word(db):
    """Fast Five matches "heist" through an alias as a whole word, so it must not fold as a substring."""
    await _bundle(db)
    await _title(db, 80, "Heist", 2001, item_n=100)
    await _title(db, 81, "Fast Five", 2011, item_n=90000, aliases=("Fast & Furious 5: Rio Heist",))
    await _title(db, 82, "Sheisty Business", 2005, item_n=100)
    await _title(db, 83, "Nothing Alike", 2004, item_n=100, aliases=("Die Sheister",))
    rows, _ = await library.list_titles(db, kinds=["movie"], q="heist")
    assert {r["id"]: r["match"] for r in rows} == {
        80: "strong", 81: "strong", 82: "weak", 83: "weak"
    }
    assert [r["id"] for r in rows][:2] == [80, 81], "and the order still puts them first"
    listed, _ = await library.list_titles(db, kinds=["movie"])
    assert all("match" not in r for r in listed)


async def test_search_order_is_total_across_pages(db):
    """OFFSET paging over a partial order repeats and drops rows: these tie on every key but the id."""
    await _bundle(db)
    for i in range(7):
        await _title(db, 80 + i, "Twin Peaks", 1990, item_n=100)
    seen: list[int] = []
    for offset in range(0, 8, 2):
        rows, total = await library.list_titles(
            db, kinds=["movie"], q="twin", limit=2, offset=offset
        )
        seen += [r["id"] for r in rows]
    assert total == 7
    assert seen == sorted(seen) and len(set(seen)) == 7


async def test_no_query_keeps_the_year_order(db):
    await _bundle(db)
    await _title(db, 90, "Up", 2009, item_n=40000)
    await _title(db, 91, "Supernova", 2024, item_n=10)
    rows, _ = await library.list_titles(db, kinds=["movie"])
    assert [r["name"] for r in rows] == ["Supernova", "Up"]


async def _genre(db, title_id: int, genre: str, source: str) -> None:
    await db.execute(
        "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, $2, $3)",
        title_id, genre, source,
    )


async def test_the_genre_facet_is_the_canonical_vocabulary(db):
    """One "Action" for tmdb's "Action" and trakt's "action"; nothing of Wikidata's free text."""
    await _bundle(db)
    await _title(db, 100, "Heat", 1995)
    await _title(db, 101, "Leon", 1994)
    await _title(db, 102, "Severance", 2022, kind="series")
    await _genre(db, 100, "Action", "tmdb")
    await _genre(db, 101, "action", "trakt")
    await _genre(db, 101, "action film", "wikidata")
    await _genre(db, 101, "pornographic film", "wikidata")
    await _genre(db, 100, "Biography", "omdb")
    await _genre(db, 102, "Sci-Fi & Fantasy", "tmdb")

    assert await library.genres(db, ["movie"]) == ["Action"]
    both = await library.genres(db, ["movie", "series"])
    assert both == ["Action", "Fantasy", "Science Fiction"]
    assert all(g in genres.CANONICAL for g in both)


async def test_a_canonical_genre_matches_every_structured_source(db):
    await _bundle(db)
    await _title(db, 110, "Arrival", 2016)
    await _title(db, 111, "Moon", 2009)
    await _title(db, 112, "Gattaca", 1997)
    await _genre(db, 110, "science-fiction", "trakt")
    await _genre(db, 111, "Sci-Fi", "omdb")
    await _genre(db, 112, "science fiction film", "wikidata")
    rows, total = await library.list_titles(db, kinds=["movie"], genre="Science Fiction")
    assert total == 2 and {r["id"] for r in rows} == {110, 111}, (
        "a Wikidata label answered a facet the control never offers"
    )


async def test_a_combined_tmdb_tv_genre_answers_both_halves(db):
    await _bundle(db)
    await _title(db, 120, "Severance", 2022, kind="series")
    await _genre(db, 120, "Sci-Fi & Fantasy", "tmdb")
    for genre in ("Science Fiction", "Fantasy"):
        _, total = await library.list_titles(db, kinds=["series"], genre=genre)
        assert total == 1, genre


async def test_an_unknown_genre_is_a_422_not_an_empty_grid(app, db):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    refused = await client.get(
        "/api/titles", params=[("kind", "movie"), ("genre", "heist film")]
    )
    assert refused.status_code == 422, refused.text
    # The canonical spelling in any case is the same question.
    assert (
        await client.get("/api/titles", params=[("kind", "movie"), ("genre", "science fiction")])
    ).status_code == 200


def test_every_genre_mapping_names_the_canonical_vocabulary():
    for raw, names in genres.GENRE_CANON.items():
        assert raw == raw.lower(), raw
        assert names and set(names) <= set(genres.CANONICAL), raw
    assert "TV Movie" not in genres.CANONICAL
    assert genres.raw_labels("heist film") == []
    with pytest.raises(ValueError, match="unknown genre"):
        genres.canonical("heist film")
    assert genres.canonical("science FICTION") == "Science Fiction"


async def test_a_folded_credit_filters_the_library_by_every_person_it_names(app, db):
    """One credit row can stand for several person rows of one human; a tap filters by all of them."""
    await _bundle(db)
    for title_id, name, kind in (
        (1, "Jaws", "movie"), (2, "Schindler's List", "movie"), (3, "Heat", "movie"),
        (4, "Amazing Stories", "series"),
    ):
        await _title(db, title_id, name, 1990, kind=kind)
    await db.executemany(
        "INSERT INTO person (id, name, imdb_id, tmdb_id) VALUES ($1, $2, $3, $4)",
        [(80, "John Williams", "nm0002354", None), (81, "John Williams", None, 491),
         (90, "Elliot Goldenthal", None, None)],
    )
    await db.executemany(
        "INSERT INTO credit (title_id, person_id, department, job, source, role_class)"
        " VALUES ($1, $2, 'Sound', 'Original Music Composer', 'tmdb', 'composer')",
        [(1, 80), (2, 81), (3, 90), (4, 81)],
    )

    rows, total = await library.list_titles(db, kinds=["movie"], person_id=[80, 81])
    assert {r["id"] for r in rows} == {1, 2} and total == 2
    assert await library.count_by_kind(db, exclude=["movie"], person_id=[80, 81]) == {"series": 1}
    lead_only, _ = await library.list_titles(db, kinds=["movie"], person_id=80)
    assert {r["id"] for r in lead_only} == {1}

    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    both = await client.get(
        "/api/titles", params=[("kind", "movie"), ("person_id", "80"), ("person_id", "81")]
    )
    assert both.status_code == 200, both.text
    assert {i["id"] for i in both.json()["items"]} == {1, 2}
    assert both.json()["hidden"] == {"series": 1}
    one = await client.get("/api/titles", params=[("kind", "movie"), ("person_id", "80")])
    assert {i["id"] for i in one.json()["items"]} == {1}
