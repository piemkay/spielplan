"""What `0026_acquisition_sources.sql` refuses and what it defaults to. Spec v2.1 §8 stage 2,
§4.1 rule 6, §6.6 and §10; decisions 326, 334 and 372.

`test_migrations.py` checks that every migration applies and what shape it leaves behind; this
file checks three of that shape's claims which cannot be read off the DDL at all, for the reason
`test_acquire_schema.py` states at its own head: a CHECK constraint that is never tried is a
comment with punctuation.

The three are an absence, a near-miss and a default.

`title.wikidata_id` carries **no UNIQUE**, because §4.1 rule 6 bans one on every provider id --
"315/171/... duplicate values exist, mostly legitimate movie/series pairs"
(`0003_content.sql:24-25`) -- and one Wikidata item covers a film and the series beside it exactly
as one tmdb id does. An absence is the kind of decision a later reader repairs on sight, so it is
asserted here in the direction that catches the repair: two titles keep one entity.

The `origin` CHECK names `bundle` and `household`, while `title.origin` carries the same name and
the same default under a different second literal, `acquired` (`0008_placement.sql:46-47`). The
wrong word is therefore a plausible edit rather than a typo, and it is the expensive one: a
curated row stamped `acquired` is one the importer's `origin = 'bundle'` DELETE passes over for
ever and no editor of §6.6's claims, so it is refused by name below.

And the DEFAULT is the backfill. Every row either curated ledger holds today arrived from a
bundle, so `bundle` is the truth for all of them rather than a convenience -- but every other
layer of this suite migrates an EMPTY database (`test_upgrade_drill.py`'s own opening paragraph),
where an `ADD COLUMN NOT NULL DEFAULT` cannot be wrong. The staged apply below is what runs that
statement over rows that were already there, which is the only state an existing install is in.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncpg
import pytest

from spielplan.backup import movie_data
from spielplan.db import migrate
from tests.test_backup import _drop, _recreate, _sibling
from tests.test_upgrade_drill import _complete, _stage

MIGRATION = "0026_acquisition_sources"


def _last_before_the_migration() -> str:
    """The migration this build applies immediately before 0026, found rather than spelled.

    0025 belongs to a sibling milestone of the same wave and is not in this tree yet; it arrives
    in this directory at the merge, and a hardcoded `0024_acquisition` would then stage an install
    that is one migration short of the one under test while still passing. The cut is
    lexicographic because `migrate.discover` globs and sorts the stems and they are zero-padded,
    so it is the same cut `apply_all` makes rather than a second opinion about migration order.
    """
    earlier = [version for version, _ in migrate.discover() if version < "0026"]
    assert earlier, "no migration sorts before 0026; this is not reading the tree's directory"
    return earlier[-1]


@pytest.fixture
async def before_the_migration(pg_url, tmp_path):
    """The install an operator is upgrading: the release before 0026, with rows in both ledgers.

    A database of its own and not the `db` fixture's, for `test_upgrade_drill.py`'s reason -- the
    point is a schema that is deliberately not this build's, and `db` exists to guarantee the
    opposite. The staging helpers are that file's too, and they copy byte for byte, so the second
    half of the upgrade records the checksums `apply_all` computes over the tree itself.
    """
    admin, name, url = _sibling(pg_url, "_pre0026")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        directory = _stage(tmp_path, _last_before_the_migration())
        applied = await migrate.apply_all(conn, directory)
        assert applied and applied[-1] == _last_before_the_migration()
        yield conn, directory
    finally:
        await conn.close()
        await _drop(admin, name)


# --- decision 372: the two keys an adapter writes, and nothing else --------------------


async def test_the_two_keys_stage_two_fetches_by_are_on_the_title_and_are_text(db):
    """§8 stage 2 names `wikidata:resolve` and `wikipedia:article` among its eight sources, and
    `title` carried no column either one could put its answer in (`0003_content.sql:34-42` is the
    nine identity columns, and neither of these is among them).

    Decision 372 is what makes the pair load-bearing rather than convenient: an adapter writes the
    raw bytes and the identity columns and nothing else, because the identity is what decides
    which URL the NEXT fetch asks for, while every rendered field waits for stage 3's derive.
    Without these two, `wikidata:resolve` has nowhere to put the entity id and the scraped sources
    go back to guessing the slugs §8 says that source exists to stop them guessing (spec:366).

    TEXT, asserted rather than assumed, because "id" is the word in the column name and an integer
    is the obvious thing to reach for: a Wikidata id is a QID -- a letter and digits -- and an
    article title is a display string carrying spaces and a parenthesised disambiguator. Neither
    survives an integer column, and the failure would arrive at the first real fetch.

    NULLABLE for decision 334's reason. Only `tmdb:detail` is required; every other source of
    stage 2 is best-effort, and its 404, refused slug or missing credential is a note on the job
    rather than a park. A NOT NULL here would make `wikidata:resolve` required by the schema,
    which is the opposite ruling, and would refuse §8 stage 1's own title row on the way in.
    """
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
    """§4.1 rule 6, in the direction that catches its repair.

    The rule is quoted in `0003_content.sql:24-25` and it is a measurement, not a preference:
    "do not add UNIQUE constraints on tmdb_id/trakt_id/slugs (315/171/... duplicate values exist,
    mostly legitimate movie/series pairs)". A Wikidata item is the same kind of key -- one item
    covers a work this household holds as a film and as the series beside it -- so the UNIQUE that
    looks missing here would refuse a legitimate acquisition at the moment it is least
    recoverable, since decision 162 makes content seed once and an install cannot re-import to
    repair it.

    The absence cannot be read off the DDL, which is why the assertion is two rows rather than a
    query against `pg_indexes`: an index added later is fine and welcome, a UNIQUE one is not.
    """
    await db.execute(
        "INSERT INTO title (id, kind, name, wikidata_id) VALUES "
        "(901, 'movie', 'Der Zweite', 'Q1050001'), (902, 'series', 'Der Zweite', 'Q1050001')"
    )
    shared = await db.fetchval("SELECT count(*) FROM title WHERE wikidata_id = 'Q1050001'")
    assert shared == 2, (
        "a UNIQUE on title.wikidata_id would refuse the movie/series pair rule 6 measured, and "
        "the refusal lands during an acquisition that cannot be re-run"
    )


# --- decision 326: who may replace a curated row ---------------------------------------


async def test_a_curated_row_is_the_bundles_or_the_households_and_nothing_between(db):
    """Decision 326, both halves: what the column defaults to and what it will not take.

    §6.6 promises that a fix typed into one of the three ledger editors "survives every future
    re-derive", and §8 stage 3 keeps that promise by re-applying both ledgers at every derive.
    Decision 171 settles the other event -- a models-only import re-loads all four curated
    ledgers -- so a household-authored row and an imported one meet in one table, and only this
    column tells the importer's DELETE which of them it owns.

    THE THIRD VALUE THIS REFUSES IS NOT ARBITRARY. `title.origin` (`0008_placement.sql:46-47`) is
    the same column name with the same default and a different second literal, `acquired`, because
    it answers a different question: how the WORK arrived, which is what §10's rebuild set is
    scoped on. A writer who knows that column and reaches for its vocabulary here writes a row the
    `origin = 'bundle'` DELETE passes over for ever and no editor claims -- a row that is
    permanent by accident, which is the one property a curated ledger must never hand out for
    free.
    """
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
    """The DEFAULT, run over rows that were already there. Nothing else in this suite does.

    `test_upgrade_drill.py` opens on the reason: every layer of this suite applies the migrations
    to an empty database, so no `ADD COLUMN NOT NULL DEFAULT` in this repository has ever run over
    a row -- "which is the only state an existing install is ever in". This household's install is
    exactly that: it has imported the bundle, so both ledgers are populated, and a column added
    without a DEFAULT would have failed the ALTER at boot inside `db/migrate.py`, with 0026
    checksummed the moment it landed and nothing an operator could edit.

    The control is the absence checked first. Assert `origin = 'bundle'` on a database that
    already had the column and the test passes while proving nothing about the upgrade -- the same
    trap `test_backup.py` guards with its own "two empty snapshots compare equal".
    """
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
    """The other half of what the DEFAULT buys, and the half no database can show.

    `backup/movie_data.py:149-150` archives both curated ledgers, and `restore_archive` COPYs each
    table with the column list THE ARCHIVE carries rather than this build's. So an archive written
    by any shipped build up to this one restores into a 0026 install with `origin` taking its
    default -- where a CHECK added without one would have refused every row of it, on the one
    recovery gesture decision 162 leaves the household.

    `test_backup.py::test_every_table_is_either_archived_or_deliberately_left_out` cannot see
    this. That sweep admits a table that leaves `TABLES` provided it joins the `EXCLUDED` set with
    a reason, which is precisely the change that would falsify 0026's argument while staying
    green. This asserts the narrower claim the DDL rests on: these two are archived.
    """
    archived = {table.qualified for table in movie_data.TABLES}
    missing = {"public.dna_adjudication", "public.credit_correction"} - archived
    assert not missing, (
        f"{sorted(missing)} left the movie-data archive. 0026 argues its NOT NULL DEFAULT from "
        "the fact that these two are archived and restored with the archive's own column list; "
        "if they are leaving the artifact, that paragraph is now false and comes out with them"
    )
