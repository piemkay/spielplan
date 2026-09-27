"""What `0026_acquisition_sources.sql` refuses and defaults to (§8 stage 2, decisions 326, 372).
No UNIQUE on `wikidata_id` (§4.1 rule 6), `origin` refuses `acquired`, and the DEFAULT is the
backfill over existing rows. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncpg
import pytest


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
