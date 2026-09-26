"""0037 cannot call `importer/meta.resolve_title_fields`, so the install is staged one migration
short, seeded as the old importer left it, upgraded, and resolved again: nothing may change.
A database of its own, as the `db` fixture guarantees this build's schema."""

from __future__ import annotations

import json

import asyncpg
import pytest

from spielplan.db import migrate
from spielplan.derive import ids
from spielplan.importer import meta
from tests.test_backup import _drop, _recreate, _sibling
from tests.test_upgrade_drill import _complete, _stage

MIGRATION = "0037_title_card_and_alias_kind"

TMDB_POSTER = "https://image.tmdb.org/t/p/w500/kept.jpg"
TVMAZE_POSTER = "https://static.tvmaze.com/uploads/images/original_untouched/8/back.jpg"
IMDB_POSTER = "https://m.media-amazon.com/images/M/refused.jpg"
MOULIN_ROUGE = "A synthetic 2001 plot that MPST attached to two films."
COLLIDED_PAGE = "A synthetic Wikipedia page matched to two titles."

# title id -> {source: payload}, and the card the OLD importer resolved for it, which is the card
# the seeded install holds: (overview, poster_path).
SEEDED = {
    10: ({"mpst": {"plot_full": MOULIN_ROUGE}}, (MOULIN_ROUGE, None)),
    11: ({"mpst": {"plot_full": MOULIN_ROUGE}}, (MOULIN_ROUGE, None)),
    12: ({"mpst": {"plot_full": "A retelling only this title carries."}},
         ("A retelling only this title carries.", None)),
    13: ({"wikipedia": {"plot_full": COLLIDED_PAGE}}, (COLLIDED_PAGE, None)),
    14: ({"wikipedia": {"plot_full": COLLIDED_PAGE}}, (COLLIDED_PAGE, None)),
    15: ({"tmdb": {"plot_full": "Its own plot.", "poster_url": TMDB_POSTER},
          "mpst": {"plot_full": "A retelling beside it."}},
         ("Its own plot.", TMDB_POSTER)),
    16: ({"omdb": {"plot_full": "An omdb plot.", "poster_url": IMDB_POSTER},
          "tvmaze": {"poster_url": TVMAZE_POSTER}},
         ("An omdb plot.", IMDB_POSTER)),
    17: ({"omdb": {"plot_full": "Another omdb plot.", "poster_url": IMDB_POSTER}},
         ("Another omdb plot.", IMDB_POSTER)),
}
UPGRADED = {
    10: (None, None), 11: (None, None), 12: (None, None), 13: (None, None), 14: (None, None),
    15: ("Its own plot.", TMDB_POSTER), 16: ("An omdb plot.", TVMAZE_POSTER),
    17: ("Another omdb plot.", None),
}


def _last_before_the_migration() -> str:
    """Found, not spelled: a sibling migration may land at the merge."""
    earlier = [version for version, _ in migrate.discover() if version < MIGRATION]
    assert earlier, f"no migration sorts before {MIGRATION}"
    return earlier[-1]


@pytest.fixture
async def before_the_migration(pg_url, tmp_path):
    admin, name, url = _sibling(pg_url, "_pre0037")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    try:
        directory = _stage(tmp_path, _last_before_the_migration())
        applied = await migrate.apply_all(conn, directory)
        assert applied and applied[-1] == _last_before_the_migration()
        yield conn, directory
    finally:
        await conn.close()
        await _drop(admin, name)


async def _seed(conn: asyncpg.Connection) -> None:
    for title_id, (by_source, (overview, poster)) in SEEDED.items():
        await conn.execute(
            "INSERT INTO title (id, kind, name, overview, poster_path) VALUES ($1, 'movie', $2, $3, $4)",
            title_id, f"Title {title_id}", overview, poster,
        )
        for source, payload in by_source.items():
            await conn.execute(
                "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
                title_id, source, payload,
            )
    await conn.execute("INSERT INTO person (id, name) VALUES (40, 'Alex Heffes')")
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES (15, 40, 'Sound', 'Original Music Composer', 'crew', 'correction')"
    )
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 2, 3)"
    )
    await conn.executemany(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ('v1', $1, $2, $3)",
        [
            ("cheesy", "register.camp", None),                             # a corpus lexicon row
            ("teenage girl", "characters.teen_protagonist", None),         # decision 500's four
            ("teenage protagonist", "characters.teen_protagonist", None),  # a lead: it projects
            ("campy", "register.camp", "alias"),                           # stored: never overwritten
            ("slow-burn", "pacing.patient", None),                         # nobody's lexicon
        ],
    )


async def _cards(conn: asyncpg.Connection) -> dict[int, tuple]:
    return {
        r["id"]: (r["overview"], r["poster_path"])
        for r in await conn.fetch("SELECT id, overview, poster_path FROM title ORDER BY id")
    }


async def test_0037_writes_what_the_new_walk_answers_and_the_walk_then_changes_nothing(
    before_the_migration,
):
    """The control is the seed read back before the upgrade: a card already at its new value would
    make every assertion true of a migration that did nothing."""
    conn, directory = before_the_migration
    await _seed(conn)
    assert await _cards(conn) == {t: card for t, (_rows, card) in SEEDED.items()}

    assert MIGRATION in _complete(directory)
    assert MIGRATION in await migrate.apply_all(conn, directory)

    assert await _cards(conn) == UPGRADED
    await meta.resolve_title_fields(conn, list(meta.SOURCE_PRIORITY))
    assert await _cards(conn) == UPGRADED, (
        "the importer's walk moved a card 0037 had already written, so the migration and the "
        "function it stands in for disagree"
    )


async def test_0037_classes_the_crew_credit_and_marks_the_lexicon_aliases(before_the_migration):
    """Decision 500: the lexicon row and presence keyword become `lexicon`; the rest stay as they were."""
    conn, directory = before_the_migration
    await _seed(conn)
    _complete(directory)
    await migrate.apply_all(conn, directory)

    kinds = {r["alias"]: r["kind"] for r in await conn.fetch("SELECT alias, kind FROM dna_alias")}
    assert kinds == {
        "cheesy": "lexicon", "teenage girl": "lexicon", "teenage protagonist": None,
        "campy": "alias", "slow-burn": None,
    }
    assert await conn.fetchval("SELECT role_class FROM credit WHERE person_id = 40") == (
        ids.classify_role("Sound", "Original Music Composer")
    ) == "composer"
