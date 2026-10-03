"""Decision 551: each shelf shows the person's own placed films most like the film on top, ties to a
shared genre and then the higher-placed, never the film itself, always its kind, and no rotation."""

from __future__ import annotations

import pytest

from spielplan.ledger import ladder
from spielplan.rate import shelves
from tests.helpers import insert_user

VOCAB = "v1"
HEIST, TENSE, LA, RARE = "themes.heist", "mood.tense", "place.los_angeles", "themes.rare"


async def film(db, title_id: int, *terms: str, owned: bool = True, kind: str = "movie",
               genre: str | None = None) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES ($1, $2, $3, $4)",
        title_id, kind, f"Film {title_id}", owned,
    )
    await db.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1, $2, $3, $4, 1, 'movielens_tags')",
        [(title_id, VOCAB, term, term.split(".")[0]) for term in terms],
    )
    if genre is not None:
        await db.execute(
            "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, $2, 'tmdb')",
            title_id, genre,
        )


@pytest.fixture
async def person(db):
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 3, 4)", VOCAB
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(VOCAB, "themes", 0), (VOCAB, "mood", 1), (VOCAB, "place", 2)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, gloss, label) VALUES ($1, $2, $3, '', NULL)",
        [(VOCAB, term, term.split(".")[0]) for term in (HEIST, TENSE, LA, RARE)],
    )
    user = await insert_user(db, "patrick", "admin")
    # A week back, so a placement backdated a day is still after the cut-over.
    await db.execute(
        "INSERT INTO ladder_setup (user_id, finished_at) VALUES ($1, now() - interval '7 days')", user
    )
    return user


async def place(db, user: int, tiers: dict[int, int]) -> None:
    for title_id, tier in tiers.items():
        await ladder.place(db, user_id=user, title_id=title_id, tier=tier)


async def shown(db, user: int, title_id: int, *, kind: str = "movie", version=VOCAB):
    found = await shelves.shelves_for(db, user_id=user, title_id=title_id, kind=kind, version=version)
    return {shelf.tier: [f.id for f in shelf.films] for shelf in found}, found


async def test_a_shelf_shows_the_most_alike_placed_films_first_and_four_at_most(db, person):
    """Nested term sets make the cosine order plain: all three terms, then two, then one."""
    await film(db, 10, HEIST, TENSE, LA, genre="Crime")
    await film(db, 11, HEIST, TENSE, LA)
    await film(db, 12, HEIST)
    await film(db, 13)
    await film(db, 15, HEIST, TENSE)
    await film(db, 18)
    await place(db, person, {12: 5, 13: 5, 11: 5, 15: 5, 18: 5})

    by_tier, found = await shown(db, person, 10)
    assert by_tier[5] == [11, 15, 12, 18], "the two that share nothing go newest-placed first"
    assert found[0].count == 5 and found[0].word == "All-time favourite"
    assert [shelf.tier for shelf in found] == [5, 4, 3, 2, 1, 0], "best first, every tier"
    assert all(by_tier[tier] == [] for tier in range(5))
    assert {shelf.word for shelf in found if shelf.count == 0} == {
        "Excellent", "Very good", "Good", "OK", "Not for me",
    }, "an empty shelf keeps its word"


async def test_ties_go_to_a_shared_genre_then_the_higher_placed_film(db, person):
    """`ledger_state.s` says higher-placed; with no fit yet, the newer placement (plan reading 19)."""
    await film(db, 10, TENSE, genre="Crime")
    await film(db, 20, TENSE, genre="Comedy")
    await film(db, 21, TENSE, genre="Crime")
    for title_id in (22, 23, 24, 25, 26):
        await film(db, title_id)
    await place(db, person, {20: 2, 21: 2, 22: 1, 23: 1, 26: 1, 24: 0, 25: 0})
    await db.executemany(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma, tier) "
        "VALUES ($1, $2, 'movie', $3, 0.3, 1)",
        [(person, 22, 0.1), (person, 23, 0.9)],
    )
    await db.execute("UPDATE tier_edit SET created_at = now() - interval '1 day' WHERE title_id = 24")

    by_tier, _ = await shown(db, person, 10)
    assert by_tier[2] == [21, 20], "the same likeness: the one sharing the film's genre first"
    assert by_tier[1] == [23, 22, 26], "the higher-placed, and one with no fit yet after both"
    assert by_tier[0] == [25, 24], "neither fitted: the newer placement"


async def test_never_the_film_itself_and_always_its_kind(db, person):
    await film(db, 10, HEIST, TENSE)
    await film(db, 19, HEIST)
    await film(db, 30, HEIST, TENSE, kind="series")
    await place(db, person, {10: 3, 19: 3, 30: 5})

    by_tier, found = await shown(db, person, 10)
    assert by_tier[3] == [19] and found[2].count == 1, "a placed film is not its own neighbour"
    assert by_tier[5] == [], "a series never sits on a film's shelf"
    series, _ = await shown(db, person, 30, kind="series")
    assert series[5] == [] and series[3] == []


async def test_an_unowned_film_on_top_and_unowned_placed_films_take_part(db, person):
    """The set-up places films from the whole catalogue, so neither side need be owned."""
    await film(db, 12, HEIST)
    await film(db, 40, HEIST, RARE, owned=False)
    await film(db, 16, HEIST, RARE, owned=False)
    await place(db, person, {12: 4, 16: 4})

    by_tier, found = await shown(db, person, 40)
    assert by_tier[4] == [16, 12]
    assert found[1].films[0].name == "Film 16"


async def test_the_same_film_always_shows_the_same_shelves(db, person):
    """No rotation (decision 551); with no vocabulary yet, likeness is even and the ties decide."""
    await film(db, 10, HEIST, genre="Crime")
    for title_id, terms in ((11, (HEIST,)), (12, ()), (13, (TENSE,)), (14, (HEIST, TENSE))):
        await film(db, title_id, *terms)
    await place(db, person, {11: 3, 12: 3, 13: 3, 14: 3})

    first, _ = await shown(db, person, 10)
    again, _ = await shown(db, person, 10)
    assert first == again and first[3][0] in (11, 14)
    blind, _ = await shown(db, person, 10, version=None)
    assert sorted(blind[3]) == [11, 12, 13, 14] and blind[3] == (await shown(
        db, person, 10, version=None
    ))[0][3]


async def test_the_sheet_names_the_current_step_by_its_word(db, person):
    await film(db, 10, HEIST)
    await film(db, 11, HEIST)
    await place(db, person, {10: 1, 11: 5})
    sheet = await shelves.sheet(db, user_id=person, title_id=10)
    assert sheet["current"] == {"tier": 1, "word": "OK"}
    assert sheet["shelves"][0] == {
        "tier": 5, "word": "All-time favourite", "count": 1,
        "films": [{"id": 11, "name": "Film 11", "original_name": None, "original_language": None,
                   "poster_path": None}],
    }
    with pytest.raises(LookupError):
        await shelves.sheet(db, user_id=person, title_id=999)
