"""What `0026_acquisition_sources.sql` refuses and defaults to (§8 stage 2, decisions 326, 372).
No UNIQUE on `wikidata_id` (§4.1 rule 6), `origin` refuses `acquired`, and the DEFAULT is the
backfill over existing rows. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncpg
import pytest

from spielplan.backup import movie_data
from spielplan.db import migrate
from tests.helpers import create_database, drop_database, sibling
from tests.test_upgrade_drill import _complete, _stage

MIGRATION = "0026_acquisition_sources"


def _last_before_the_migration() -> str:
    """Found rather than spelled, with `migrate.discover`'s
    lexicographic cut, so a new sibling migration cannot short it."""
    earlier = [version for version, _ in migrate.discover() if version < "0026"]
    assert earlier, "no migration sorts before 0026; this is not reading the tree's directory"
    return earlier[-1]


@pytest.fixture
async def before_the_migration(pg_url, tmp_path):
    """A database of its own, not `db`'s: the schema is deliberately not this build's."""
    admin, name, url = sibling(pg_url, "_pre0026")
    await create_database(admin, name)
    conn = await asyncpg.connect(url)
    try:
        directory = _stage(tmp_path, _last_before_the_migration())
        applied = await migrate.apply_all(conn, directory)
        assert applied and applied[-1] == _last_before_the_migration()
        yield conn, directory
    finally:
        await conn.close()
        await drop_database(admin, name)


async def test_the_two_keys_stage_two_fetches_by_are_on_the_title_and_are_text(db):
    """TEXT because a QID and an article title are not integers;
    NULLABLE because every source but `tmdb:detail` is best-effort."""
    columns = {
        row["column_name"]: row
        for row in await db.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = 'title'"
        )
    }
    for column in ("wikidata_id", "wikipedia_title"):
        assert column in columns, f"0026 must add title.{column}: stage 2 has nowhere to write"
        assert columns[column]["data_type"] == "text", (
            f"title.{column} is not text. A QID is a letter and digits and an article title is a "
            "display string; an integer column takes neither"
        )
        assert columns[column]["is_nullable"] == "YES", (
            f"title.{column} is NOT NULL, which makes a best-effort source of stage 2 required "
            "(decision 334) and refuses the title row stage 1 mints before any of them has run"
        )

    await db.execute(
        "INSERT INTO title (id, kind, name, wikidata_id, wikipedia_title) "
        "VALUES (900, 'movie', 'Der Zweite', 'Q1050001', 'Der Zweite (1988 film)')"
    )
    row = await db.fetchrow("SELECT wikidata_id, wikipedia_title FROM title WHERE id = 900")
    assert row["wikidata_id"] == "Q1050001"
    assert row["wikipedia_title"] == "Der Zweite (1988 film)", (
        "the article title came back changed; it is a display string and not an identifier the "
        "app may normalise"
    )


async def test_two_titles_may_share_one_wikidata_entity(db):
    """One Wikidata item covers a film and the series beside it, exactly as one tmdb id does."""
    await db.execute(
        "INSERT INTO title (id, kind, name, wikidata_id) VALUES "
        "(901, 'movie', 'Der Zweite', 'Q1050001'), (902, 'series', 'Der Zweite', 'Q1050001')"
    )
    shared = await db.fetchval("SELECT count(*) FROM title WHERE wikidata_id = 'Q1050001'")
    assert shared == 2, (
        "a UNIQUE on title.wikidata_id would refuse the movie/series pair rule 6 measured, and "
        "the refusal lands during an acquisition that cannot be re-run"
    )


async def test_a_curated_row_is_the_bundles_or_the_households_and_nothing_between(db):
    """`title.origin` uses `acquired`; a curated row stamped so
    would be passed over by the importer's DELETE for ever."""
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 1)"
    )
    await db.execute(
        "INSERT INTO dna_adjudication (version, term, verdict) VALUES ('v1', 'melancholy', 'keep')"
    )
    await db.execute(
        "INSERT INTO credit_correction (title_id, field, new_value) VALUES (11, 'job', 'Director')"
    )
    assert await db.fetchval("SELECT origin FROM dna_adjudication") == "bundle", (
        "a row written by a loader that names no origin must read as the bundle's: that is what "
        "makes the backfill free and what keeps decision 171's re-load owning what it shipped"
    )
    assert await db.fetchval("SELECT origin FROM credit_correction") == "bundle"

    statements = (
        "INSERT INTO dna_adjudication (version, term, verdict, origin) "
        "VALUES ('v1', 'rain', 'drop', $1)",
        "INSERT INTO credit_correction (title_id, field, new_value, origin) "
        "VALUES (11, 'job', 'Editor', $1)",
    )
    for statement in statements:
        for refused in ("acquired", "import"):
            with pytest.raises(asyncpg.CheckViolationError):
                await db.execute(statement, refused)


async def test_a_ledger_row_written_before_the_migration_reads_as_the_bundles(
    before_the_migration,
):
    """Every other layer migrates an EMPTY database; this runs the ADD COLUMN DEFAULT over existing rows."""
    conn, directory = before_the_migration
    for table in ("dna_adjudication", "credit_correction"):
        present = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = $1 AND column_name = 'origin'",
            table,
        )
        assert present == 0, (
            f"the staged install already has {table}.origin, so this upgrade proves nothing: "
            f"{MIGRATION} is being staged as part of the 'before' half"
        )

    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 1)"
    )
    await conn.execute(
        "INSERT INTO dna_adjudication (version, term, verdict, note) "
        "VALUES ('v1', 'melancholy', 'keep', 'the ferry scene')"
    )
    await conn.execute(
        "INSERT INTO credit_correction (title_id, field, new_value) VALUES (11, 'job', 'Director')"
    )

    pending = _complete(directory)
    assert MIGRATION in pending, f"{MIGRATION} is not among the migrations this upgrade applies"
    applied = await migrate.apply_all(conn, directory)
    assert MIGRATION in applied

    adjudication = await conn.fetchval(
        "SELECT origin FROM dna_adjudication WHERE term = 'melancholy'"
    )
    correction = await conn.fetchval("SELECT origin FROM credit_correction WHERE title_id = 11")
    assert (adjudication, correction) == ("bundle", "bundle"), (
        "a curated row that predates the provenance column must read as the bundle's: it is one "
        "the import brought, and a NULL or an empty string there is a row the importer's scoped "
        "DELETE would refuse to replace on the next models-only import"
    )


def test_the_two_ledgers_whose_default_keeps_an_old_archive_restorable_are_still_archived():
    """`restore_archive` COPYs with the archive's own column
    list, so an old archive relies on the DEFAULT."""
    archived = {table.qualified for table in movie_data.TABLES}
    missing = {"public.dna_adjudication", "public.credit_correction"} - archived
    assert not missing, (
        f"{sorted(missing)} left the movie-data archive. 0026 argues its NOT NULL DEFAULT from "
        "the fact that these two are archived and restored with the archive's own column list; "
        "if they are leaving the artifact, that paragraph is now false and comes out with them"
    )
