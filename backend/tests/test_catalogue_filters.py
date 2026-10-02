"""Decision 557's terms and people, and decision 558's owned scope, on the one catalogue filter, against
Postgres 16: includes AND over either tier with quoted rows first, leave-outs over both tiers, people
as AND-ed groups, and what `/api/titles` says about them. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.db import library

VOCAB = "v1"
TITLES = [
    (1, "movie", "Heat", 1995, True),
    (2, "movie", "Paddington 2", 2017, True),
    (3, "movie", "Knives Out", 2019, False),
    (4, "movie", "Se7en", 1995, True),
    (5, "series", "Dark", 2017, True),
    (6, "series", "Broadchurch", 2013, False),
]
QUOTED = [
    (1, "themes.heist"), (2, "themes.heist"), (2, "mood.cozy"), (3, "themes.heist"),
    (4, "mood.bleak"), (5, "mood.bleak"), (6, "themes.heist"),
]
INFERRED = [(1, "mood.bleak"), (3, "mood.cozy"), (5, "themes.heist")]
TMDB_FACE = "https://image.tmdb.org/t/p/w185/caine.jpg"


async def _quote(db, title_id: int, term: str) -> None:
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)"
        " VALUES ($1, $2, $3, $4, 2, '')",
        title_id, VOCAB, term, term.split(".", 1)[0],
    )


async def _infer(db, title_id: int, term: str) -> None:
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) VALUES ($1, $2, $3, $4, 1)",
        title_id, VOCAB, term, term.split(".", 1)[0],
    )


@pytest.fixture
async def catalogue(db):
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, $4, $5)", TITLES
    )
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 2, 3)", VOCAB
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(VOCAB, "mood", 0), (VOCAB, "themes", 1)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, label) VALUES ($1, $2, $3, $4)",
        [(VOCAB, "mood.cozy", "mood", "cozy & mellow"), (VOCAB, "mood.bleak", "mood", None),
         (VOCAB, "themes.heist", "themes", "heist")],
    )
    for title_id, term in QUOTED:
        await _quote(db, title_id, term)
    for title_id, term in INFERRED:
        await _infer(db, title_id, term)
    # One human on two rows whose ids cannot disagree, and a second person.
    await db.executemany(
        "INSERT INTO person (id, name, imdb_id, tmdb_id, profile_path) VALUES ($1, $2, $3, $4, $5)",
        [(10, "Michael Caine", "nm0000323", None, None),
         (11, "Michael Caine", "nm0000323", 3895, TMDB_FACE),
         (12, "Rian Johnson", "nm0426059", None, None)],
    )
    await db.executemany(
        "INSERT INTO credit (title_id, person_id, department, job, source, role_class)"
        " VALUES ($1, $2, $3, $4, 'tmdb', $5)",
        [(1, 10, "Acting", "Actor", "cast"), (3, 11, "Acting", "Actor", "cast"),
         (3, 12, "Directing", "Director", "director"), (2, 12, "Writing", "Writer", "writer")],
    )


async def _listed(db, *, kinds=("movie",), **filters):
    return await library.list_titles(db, kinds=list(kinds), **filters)


async def _ids(db, **filters) -> set[int]:
    rows, _total, _strong = await _listed(db, **filters)
    return {r["id"] for r in rows}


async def test_includes_and_over_either_tier(db, catalogue):
    assert await _ids(db, terms=("themes.heist",)) == {1, 2, 3}
    assert await _ids(db, terms=("themes.heist", "mood.cozy")) == {2, 3}, (
        "Knives Out carries cozy by our read alone and is still a match"
    )
    assert await _ids(db, terms=("mood.cozy", "mood.bleak")) == set()


async def test_quoted_rows_lead_across_kinds_and_are_counted(db, catalogue):
    """Year order alone would put Dark (2017) before Broadchurch (2013); Dark is heist by our read."""
    rows, total, strong = await _listed(db, kinds=("movie", "series"), terms=("themes.heist",))
    assert [r["id"] for r in rows] == [3, 2, 6, 1, 5]
    assert [r["match"] for r in rows] == ["strong"] * 4 + ["weak"]
    assert (total, strong) == (5, 4)

    rows, total, strong = await _listed(db, terms=("themes.heist", "mood.cozy"))
    assert [(r["id"], r["match"]) for r in rows] == [(2, "strong"), (3, "weak")]
    assert (total, strong) == (2, 1)

    rows, total, strong = await _listed(db)
    assert strong is None and "match" not in rows[0]


async def test_for_you_orders_inside_the_quoted_run_and_inside_the_rest(db, catalogue):
    """Dark has the highest score and still waits behind every quoted title, series included."""
    user_id = await db.fetchval("INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id")
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('b1', '{}'::jsonb, 'active')"
    )
    await db.executemany(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf)"
        " VALUES ($1, $2, $3, 'b1', $4, 0)",
        [(user_id, 1, "movie", 0.9), (user_id, 2, "movie", 0.1), (user_id, 3, "movie", 0.5),
         (user_id, 5, "series", 0.99), (user_id, 6, "series", 0.2)],
    )
    rows, _total, _strong = await _listed(
        db, kinds=("movie", "series"), terms=("themes.heist",), user_id=user_id, sort="for_you",
        bundle_version="b1",
    )
    assert [r["id"] for r in rows] == [1, 3, 2, 6, 5]


async def test_under_a_search_a_strong_row_needs_a_strong_text_match_too(db, catalogue):
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, $3, true)",
        [(7, "The Heat", 2013), (8, "Theatre Heist", 2020)],
    )
    await _infer(db, 7, "themes.heist")
    await _quote(db, 8, "themes.heist")

    searched, _total, _strong = await _listed(db, q="heat")
    assert [r["id"] for r in searched][:2] == [7, 1], "the newer exact match leads a plain search"

    rows, total, strong = await _listed(db, q="heat", terms=("themes.heist",))
    assert [(r["id"], r["match"]) for r in rows] == [(1, "strong"), (7, "weak"), (8, "weak")]
    assert (total, strong) == (3, 1)


async def test_a_leave_out_reads_both_tiers(db, catalogue):
    assert await _ids(db, not_terms=("mood.bleak",)) == {2, 3}, "Heat is bleak by our read alone"
    assert await _ids(db, kinds=("series",), not_terms=("mood.bleak",)) == {6}
    assert await _ids(db, terms=("themes.heist",), not_terms=("mood.cozy",)) == {1}


async def test_people_and_across_groups_and_or_within_one(db, catalogue):
    assert await _ids(db, people=[(10, 11)]) == {1, 3}
    assert await _ids(db, people=[(10,)]) == {1}
    assert await _ids(db, people=[(12,)]) == {2, 3}, "any role"
    assert await _ids(db, people=[(10, 11), (12,)]) == {3}
    assert await _ids(db, people=[(10, 11)], terms=("mood.cozy",)) == {3}


async def test_the_owned_scope(db, catalogue):
    assert await _ids(db, owned="only") == {1, 2, 4}
    assert await _ids(db, owned="not") == {3}
    assert await _ids(db, owned="any") == {1, 2, 3, 4}
    assert await library.count_by_kind(db, owned="only") == {"movie": 3, "series": 1}


async def test_eligible_ids_fill_strong_when_terms_filter(db, catalogue):
    """The owned scope is not read: a recipe splits the library and beyond itself."""
    eligible = await library.eligible_ids(
        db, kinds=["movie"], user_id=1, terms=("themes.heist", "mood.cozy"), owned="only"
    )
    assert eligible == library.Eligible(ids=frozenset({2, 3}), strong=frozenset({2}))
    by_person = await library.eligible_ids(db, kinds=["movie"], user_id=1, people=[(12,)])
    assert by_person == library.Eligible(ids=frozenset({2, 3}))
    assert await library.eligible_ids(
        db, kinds=["movie"], user_id=1, terms=(), not_terms=(), people=(), owned="not"
    ) is None


@pytest.fixture
async def client(app, catalogue):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    return client


async def _titles(client, *params):
    answer = await client.get("/api/titles", params=[("kind", "movie"), *params])
    assert answer.status_code == 200, answer.text
    return answer.json()


async def test_the_titles_route_counts_beyond_the_library_under_the_same_filters(client):
    library_only = await _titles(client, ("term", "themes.heist"), ("owned", "only"))
    assert [i["id"] for i in library_only["items"]] == [2, 1]
    assert library_only["strong_total"] == 2
    assert library_only["beyond"] == 1, "Knives Out, the one heist film not owned"
    assert library_only["hidden"] == {"series": 1}, "Dark; Broadchurch is not owned"

    everything = await _titles(client, ("term", "themes.heist"))
    assert [i["id"] for i in everything["items"]] == [3, 2, 1]
    assert everything["beyond"] is None
    assert everything["hidden"] == {"series": 2}

    plain = await _titles(client, ("owned", "only"), ("q", "e"))
    assert plain["strong_total"] is None
    assert plain["beyond"] == 1


async def test_the_titles_route_names_its_chips(client):
    body = await _titles(
        client, ("term", "themes.heist"), ("not_term", "mood.bleak"), ("person", "10,11")
    )
    assert body["applied"] == {
        "terms": [{"term": "themes.heist", "label": "heist", "facet": "themes"}],
        "not_terms": [{"term": "mood.bleak", "label": "bleak", "facet": "mood"}],
        "people": [{"person_ids": [10, 11], "person_id": 11, "name": "Michael Caine", "photo": True}],
    }
    assert [i["id"] for i in body["items"]] == [3]
    assert (await _titles(client))["applied"] == {"terms": [], "not_terms": [], "people": []}


async def test_the_titles_route_refuses_unknown_terms_and_bad_people(client):
    refused = await client.get(
        "/api/titles",
        params=[("kind", "movie"), ("term", "mood.nope"), ("not_term", "themes.nada"),
                ("not_term", "mood.cozy")],
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == {"reason": "unknown_term", "terms": ["mood.nope", "themes.nada"]}

    garbled = await client.get("/api/titles", params=[("kind", "movie"), ("person", "10,x")])
    assert garbled.status_code == 422, garbled.text
