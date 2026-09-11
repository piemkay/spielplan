"""The upgrade drill. Spec v2.1 §2, §10; CLAUDE.md Gotchas; decision 181;
docs/milestones/M4.7-plan.md §2 finding 22, §4, §6 and §8.

Every layer of this suite applies the migrations to an *empty* database. `conftest.py`'s `db`
fixture drops schema `public` and calls `apply_all`; `test_migrations.py` does the same; the
PGlite layer builds from nothing. So no `ALTER TABLE ... ADD COLUMN NOT NULL DEFAULT`, no
`UPDATE ... SET` backfill and no `ADD PRIMARY KEY` in this repository has ever run over a row
that was already there — which is the only state an existing install is ever in. The migration
most obviously exposed says so in its own comment: `0015_seed.sql:108-111` — "Backfill BEFORE
the unique index, or this migration cannot apply to any install that has already run §10's
re-import" — and that argument has never been executed against two `artifact_bundle` rows.

This file runs the upgrade the way `git pull && docker compose up -d --build` runs it: a
database migrated to the release the household is on, with rows in it, and then the newest
migrations applied on top. The pass conditions are §5's row: it succeeds, and it lands the same
schema a fresh install gets — because "the upgrade worked" and "the upgrade produced this
build's schema" are different claims and only the second one is checkable.

It also carries the case plan §8 demands be discovered here rather than on the household's box:
`0017_ops.sql`'s `data_encryption_key_one_active` is a unique index over a property `sec-10`
proves the old code could violate, so on an install that lost that race the first thing this
milestone does is refuse to start. The three tests at the bottom are that install.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import asyncpg
import pytest

from spielplan.core import secrets as sec
from spielplan.core import secrets_cli
from spielplan.core.config import settings
from spielplan.db import migrate
from tests.test_backup import _drop, _recreate, _sibling

# The last migration before 0015's backfill, which is the oldest of the three that alter tables
# rather than only create them. Naming the *first* half rather than a count of the second is
# what keeps this drill honest as migrations are added: 0018 and everything after it lands in
# "the rest" automatically, which is where a new backfill needs to be exercised.
LAST_BEFORE = "0014_tonight_undo"

OLD_KEY = "the-secrets-key-this-install-was-built-with"
NEW_KEY = "a-different-secrets-key-not-a-real-one"

# The schemas this application owns. `information_schema` and `pg_catalog` are the server's.
SCHEMAS = ("public", "display", "review_store")


def _stage(tmp_path: Path, upto: str) -> Path:
    """A migrations directory holding everything up to and including `upto`.

    Copied byte for byte rather than rewritten: `_checksum` hashes `read_text`'s output, so a
    copy that changed a line ending would hash the same but a copy that changed anything else
    would make the second half of the drill reject the first half's work for the wrong reason.
    """
    directory = tmp_path / "migrations"
    directory.mkdir(exist_ok=True)
    for version, _ in migrate.discover():
        shutil.copyfile(migrate.MIGRATIONS_DIR / f"{version}.sql", directory / f"{version}.sql")
        if version == upto:
            return directory
    raise AssertionError(f"{upto} is not a migration in {migrate.MIGRATIONS_DIR}")


def _complete(directory: Path) -> list[str]:
    """Put the rest of the release's migrations in, as `git pull` does. Returns their versions."""
    added = []
    for version, _ in migrate.discover():
        target = directory / f"{version}.sql"
        if not target.exists():
            shutil.copyfile(migrate.MIGRATIONS_DIR / f"{version}.sql", target)
            added.append(version)
    return added


async def _schema(conn: asyncpg.Connection) -> dict[str, list[tuple]]:
    """Everything about the shape of this database that a migration can change.

    Columns, indexes, constraints and sequences, all four because each of the three migrations
    under test changes a different one: 0015 renames a column and swaps two primary keys, 0016
    replaces a CHECK constraint, 0017 adds an index. Comparing only `information_schema.columns`
    would have passed on an index that never got built.
    """
    columns = await conn.fetch(
        """
        SELECT table_schema, table_name, column_name, ordinal_position, data_type,
               is_nullable, column_default, character_maximum_length, numeric_precision
          FROM information_schema.columns
         WHERE table_schema = ANY($1::text[])
         ORDER BY table_schema, table_name, ordinal_position
        """,
        list(SCHEMAS),
    )
    indexes = await conn.fetch(
        "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = ANY($1::text[]) ORDER BY schemaname, tablename, indexname",
        list(SCHEMAS),
    )
    constraints = await conn.fetch(
        """
        SELECT n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid) AS definition
          FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY($1::text[])
         ORDER BY n.nspname, c.relname, con.conname
        """,
        list(SCHEMAS),
    )
    sequences = await conn.fetch(
        "SELECT sequence_schema, sequence_name, data_type, start_value, minimum_value, "
        "increment FROM information_schema.sequences WHERE sequence_schema = ANY($1::text[]) "
        "ORDER BY sequence_schema, sequence_name",
        list(SCHEMAS),
    )
    return {
        "columns": [tuple(r) for r in columns],
        "indexes": [tuple(r) for r in indexes],
        "constraints": [tuple(r) for r in constraints],
        "sequences": [tuple(r) for r in sequences],
    }


async def _seed_representative_rows(conn: asyncpg.Connection) -> None:
    """One row in every table the newest migrations rewrite, and no more.

    Chosen by reading the migrations rather than by taste, and the enumeration is the claim:
    a table named here is a table some migration rewrites. `artifact_bundle` because 0015
    backfills it and then builds a partial unique index over the result; `credit` because 0015
    renames a column it holds data in; `title_language`, `title_country` and
    `display.platform_rating` because 0015 drops and rebuilds their primary keys after adding a
    NOT NULL column with a default; `dna_adjudication` because 0015 drops its primary key and
    gives it a `bigserial` one, which has to number rows that already exist; `title_placement`
    because 0015 adds two NOT NULL columns to it; `user_vector` because 0015 swaps its foreign
    key from CASCADE to SET NULL; `app_user` because 0016 replaces its role CHECK and adds two
    NOT NULL columns; `data_encryption_key` and `connector_config` because 0017 indexes the one
    and the other names it.

    0022 is the newest of them and the only one whose statements can fail on *data* rather than
    only on shape, so it gets the same treatment: `ledger_cutpoints` because it rewrites
    `cutpoints_length` and adds `cutpoints_ascend` over whatever rows are there; `tier_edit`
    because it adds `n_levels` and backfills it through the title's kind; `rate_session`,
    `session_participant`, `verdict` and `duel` because each gains a CHECK an existing row has to
    satisfy. `ledger_state` and `user_score` get two rows each -- one agreeing with its title's
    kind, one deliberately disagreeing -- because the composite `(title_id, kind)` foreign key is
    an ADD CONSTRAINT that validates the rows already present, and the two DELETEs before it are
    not a cleanup the milestone wanted but what makes the ADD possible: "one pre-existing
    cross-kind row would make this file fail at startup with nothing an operator could edit"
    (`0022_model_basis.sql`). The five remaining tables whose title FK 0022 re-declares RESTRICT
    -- `user_title`, `session_answer`, `session_ballot`, `session_result`, `session_outcome` --
    are deliberately not seeded: a re-declared foreign key can only fail validation on an orphan
    row, and the CASCADE it replaces already forbade one. [M4.13 cycle 1, M413-R3]
    """
    await conn.execute(
        "INSERT INTO title (id, kind, name, year) VALUES "
        "(11, 'movie', 'Waechter der Naecht', 1979), (12, 'series', 'Der Zweite', 1988)"
    )
    await conn.execute("INSERT INTO person (id, name) VALUES (5, 'Ada Lovelace')")
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, ord) "
        "VALUES (11, 5, 'Directing', 'Director', 3)"
    )
    await conn.execute("INSERT INTO title_language (title_id, language) VALUES (11, 'de')")
    await conn.execute("INSERT INTO title_country (title_id, country) VALUES (11, 'DE')")
    await conn.execute(
        "INSERT INTO display.platform_rating (title_id, platform, score) VALUES (11, 'imdb', 7.4)"
    )
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 1)"
    )
    await conn.execute(
        "INSERT INTO dna_adjudication (version, term, verdict, note) "
        "VALUES ('v1', 'melancholy', 'keep', 'the ferry scene'), "
        "       ('v1', 'rain', 'drop', 'a weather word, not a mood')"
    )
    await conn.execute(
        "INSERT INTO credit_correction (title_id, field, new_value) VALUES (11, 'job', 'Director')"
    )
    # Two bundles, which is the whole point: 0015's backfill stamps the oldest 'seed' and every
    # later one 'model', and an install that has re-imported once is the install that has two.
    await conn.execute(
        "INSERT INTO artifact_bundle (version, imported_at, manifest, state) VALUES "
        "('v20260101', now() - interval '90 days', '{}'::jsonb, 'superseded'), "
        "('v20260801', now() - interval '2 days', '{}'::jsonb, 'active')"
    )
    await conn.execute(
        """
        INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                     tower_sha256, input_dim, blocks_present, blocks_dropped,
                                     blocks_imputed, nnz)
        VALUES (11, 'v20260801', $1, 0.25, 'contract-sha', 'tower-sha', 128,
                '{meta}', '{}', '{}', 64)
        """,
        b"\x00" * 256,
    )
    for name, role in (("patrick", "admin"), ("mira", "member")):
        await conn.execute("INSERT INTO app_user (name, role) VALUES ($1, $2)", name, role)
    await conn.execute(
        "INSERT INTO user_vector (user_id, kind, vec, bundle_version) "
        "SELECT id, 'movie', $1, 'v20260801' FROM app_user WHERE name = 'mira'",
        b"\x00" * 256,
    )
    await _seed_rows_0022_rewrites(
        conn, await conn.fetchval("SELECT id FROM app_user WHERE name = 'mira'")
    )
    await _seed_dek(conn, "the-only-one", OLD_KEY)
    await conn.execute(
        "INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id) "
        "VALUES ('jellyfin', '{\"url\": \"http://jellyfin.test\"}'::jsonb, $1, 'the-only-one')",
        b"a-sealed-blob",
    )


async def _seed_rows_0022_rewrites(conn: asyncpg.Connection, user_id: int) -> None:
    """The half of the seed 0022 needs, as one household member's rows.

    Twelve labels rather than the default seven, and eleven ascending boundaries under them,
    because `n_levels` has to come out as something a fallback could not have produced: the
    backfill reads `cardinality(tier_set)` through the title's kind and writes 7 when there is no
    row to read, so a seven-label board here would make the two answers indistinguishable.

    The two cross-kind rows are on title 12, which is a series carrying a `kind` of 'movie'. That
    is not a state the app can be talked into writing today -- it is the state a corpus
    reclassification leaves behind on an install that predates the composite key, because
    `importer/load.py`'s `_upsert_titles` sets every mapped column from EXCLUDED and nothing tied
    the denormalised copy to the title it names.
    """
    await conn.execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) VALUES "
        "($1, 'movie', ARRAY[-2.0,-1.5,-1.0,-0.5,0.0,0.5,1.0,1.5,2.0,2.5,3.0], "
        "ARRAY['1','2','3','4','5','6','7','8','9','10','11','12'])",
        user_id,
    )
    await conn.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 11, 3, 'drag_drop')",
        user_id,
    )
    await conn.execute(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma) VALUES "
        "($1, 11, 'movie', 0.42, 0.90), ($1, 12, 'movie', 0.11, 1.10)",
        user_id,
    )
    await conn.execute(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) VALUES "
        "($1, 11, 'movie', 'v20260801', 0.61, 0.22), ($1, 12, 'movie', 'v20260801', 0.30, 0.10)",
        user_id,
    )
    # Canonical order and no repeat, which is what `rate_session_kinds_distinct` will validate.
    await conn.execute(
        "INSERT INTO rate_session (user_id, kinds) VALUES ($1, ARRAY['movie', 'series'])", user_id
    )
    session = await conn.fetchval(
        "INSERT INTO session (room_code, host_user_id, kind, bundle_version) "
        "VALUES ('MX-2210', $1, 'movie', 'v20260801') RETURNING id",
        user_id,
    )
    await conn.execute(
        "INSERT INTO session_participant (session_id, user_id, role, seat) VALUES ($1, $2, "
        "'host', 1)",
        session, user_id,
    )
    await conn.execute("INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 11, 2)", user_id)
    await conn.execute(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
        "VALUES ($1, 11, 12, 'A', 'tier_queue')",
        user_id,
    )


async def _seed_dek(conn: asyncpg.Connection, key_id: str, secrets_key: str) -> None:
    """A `data_encryption_key` row wrapped under `secrets_key`, as `ensure_dek` would write it.

    `_wrap` rather than `ensure_dek` because the whole subject below is a table holding two
    un-retired rows, which `ensure_dek` cannot be made to produce on purpose — that took three
    concurrent first boots on separate connections (sec-10).
    """
    await conn.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ($1, $2)",
        key_id,
        sec._wrap(os.urandom(32), secrets_key),
    )


@pytest.fixture
async def upgrading(pg_url, tmp_path):
    """The install the operator is about to upgrade: last release's schema, with rows in it.

    A database of its own, not the `db` fixture's: the point is a schema that is deliberately
    *not* this build's, and `db` exists to guarantee the opposite.
    """
    admin, name, url = _sibling(pg_url, "_upgrade")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        directory = _stage(tmp_path, LAST_BEFORE)
        applied = await migrate.apply_all(conn, directory)
        assert applied and applied[-1] == LAST_BEFORE
        await _seed_representative_rows(conn)
        yield conn, url, directory
    finally:
        await conn.close()
        await _drop(admin, name)


@pytest.fixture
async def fresh(pg_url):
    """A brand-new install on this build, for the schema the upgrade has to arrive at."""
    admin, name, url = _sibling(pg_url, "_fresh")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        await migrate.apply_all(conn)
        yield conn
    finally:
        await conn.close()
        await _drop(admin, name)


# --- the upgrade itself -----------------------------------------------------------------------


async def test_the_newest_migrations_apply_over_populated_tables_and_land_this_builds_schema(
    upgrading, fresh, tmp_path
):
    """§10 and CLAUDE.md's Gotchas: a migration is applied once and never edited, so the only
    thing that can be checked is that applying the sequence lands where a fresh install does.

    Two claims, and the second is the one no other layer makes. That it *succeeds* is what an
    operator finds out at `docker compose up`. That it produces the same columns, indexes,
    constraints and sequences a fresh install has is what decides whether the household's box
    and the developer's are running the same application a month later — a NOT NULL column that
    quietly failed to build over existing rows, or an index skipped because its `CREATE` was
    not reached, is invisible until the query that needs it runs.
    """
    conn, _url, directory = upgrading
    pending = _complete(directory)
    assert pending, "the drill is applying nothing; LAST_BEFORE is the newest migration"

    applied = await migrate.apply_all(conn, directory)

    assert applied == pending
    assert await migrate.pending(conn, directory) == []
    upgraded = await _schema(conn)
    reference = await _schema(fresh)
    # The control, in the shape `test_backup.py` uses for the same reason: two empty snapshots
    # compare equal, so a query that named the wrong schemas would make every assertion free.
    assert len(reference["columns"]) > 400 and len(reference["indexes"]) > 100, {
        part: len(rows) for part, rows in reference.items()
    }
    for part in ("columns", "indexes", "constraints", "sequences"):
        assert upgraded[part] == reference[part], (
            f"the upgraded schema's {part} differ from a fresh install's: "
            f"only in upgrade {sorted(set(upgraded[part]) - set(reference[part]))}; "
            f"only in fresh {sorted(set(reference[part]) - set(upgraded[part]))}"
        )


async def test_the_backfill_runs_over_the_rows_that_were_already_there(upgrading):
    """`0015_seed.sql:115`'s `UPDATE artifact_bundle SET kind = 'model'`, executed at last.

    The migration's own comment is the specification: "an install with two bundle rows then
    fails the index with a duplicate key — at boot, inside `db/migrate.py`, with no way forward
    because 0015 is checksummed the moment it lands". Every install that has ever re-imported
    §10's bundle is that install, and until now the statement had only ever run over zero rows,
    where it cannot be wrong.

    The other rows are here for the same reason one layer down: `ALTER TABLE ... ADD COLUMN
    NOT NULL DEFAULT`, `RENAME COLUMN` and `ADD PRIMARY KEY` all carry data forward, and each
    of them has only ever been asked to carry none.
    """
    conn, _url, directory = upgrading
    _complete(directory)
    await migrate.apply_all(conn, directory)

    kinds = dict(
        (r["version"], r["kind"])
        for r in await conn.fetch("SELECT version, kind FROM artifact_bundle ORDER BY imported_at")
    )
    assert kinds == {"v20260101": "seed", "v20260801": "model"}, (
        "the oldest bundle is the seed by construction: it is the one that brought content into "
        "an empty install"
    )
    assert await conn.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'artifact_bundle_one_seed'"
    ) == 1

    # 0015 renamed the column the row's value lives in; a rename that dropped it would still
    # leave a schema identical to a fresh install's.
    assert await conn.fetchval("SELECT billing_order FROM credit WHERE title_id = 11") == 3
    # NOT NULL with a default, added over rows that predate it.
    assert await conn.fetchval("SELECT source FROM title_language WHERE title_id = 11") == ""
    assert await conn.fetchval(
        "SELECT metric FROM display.platform_rating WHERE title_id = 11"
    ) == "user_score"
    assert await conn.fetchval(
        "SELECT blocks_unmapped FROM title_placement WHERE title_id = 11"
    ) == "{}"
    # A bigserial primary key numbering rows that already existed, and the two of them keep
    # their own verdicts rather than one of them keeping both.
    numbered = await conn.fetch("SELECT id, term, scope FROM dna_adjudication ORDER BY term")
    assert [r["term"] for r in numbered] == ["melancholy", "rain"]
    assert len({r["id"] for r in numbered}) == 2 and all(r["scope"] == "global" for r in numbered)
    # 0016 over two accounts that predate its CHECK and its two new columns.
    assert await conn.fetchval(
        "SELECT count(*) FROM app_user WHERE password_failed_count = 0"
    ) == 2

    # 0022's two DELETEs, which are the half of that file nothing else can execute. Their own
    # comment states the stake -- "one pre-existing cross-kind row would make this file fail at
    # startup with nothing an operator could edit" -- and every other layer applies 0022 to an
    # empty `ledger_state` and an empty `user_score`, where a DELETE cannot be wrong and the
    # composite FK it clears the way for validates nothing. `load.py`'s `_upsert_titles` sets
    # every mapped column from EXCLUDED, `kind` included, so a corpus reclassification is how a
    # pre-0022 install acquires exactly the row seeded here. [M4.13 cycle 1, M413-R3]
    async def kinds(table: str) -> set[tuple[int, str]]:
        return {
            (r["title_id"], r["kind"])
            for r in await conn.fetch(f"SELECT title_id, kind FROM {table}")  # noqa: S608
        }

    assert await kinds("ledger_state") == {(11, "movie")}, (
        "the row whose kind disagreed with its title's is gone and the agreeing one survives"
    )
    assert await kinds("user_score") == {(11, "movie")}
    # And the backfill, over a row that was already there. One assertion, because what the value
    # MEANS -- the person's own board rather than the default set, read through the title's kind --
    # belongs to `test_schema_contracts.py`, whose
    # `test_the_tier_edit_k_column_is_backfilled_from_the_users_own_tier_set` stages a database
    # for exactly that; here it is the statement this file makes about every other backfill,
    # which is that it ran at all over rows it did not create.
    assert await conn.fetchval("SELECT n_levels FROM tier_edit WHERE title_id = 11") == 12


async def test_an_edited_applied_migration_stops_the_upgrade_from_both_entry_points(upgrading):
    """CLAUDE.md's absolute rule, asserted for the first time: `grep checksum backend/tests` was
    empty, so neither refusal had a test.

    Both entry points, because they are two processes: the backend applies (`apply_all`) and the
    worker waits (`pending`), and a build where only one of them notices is a rolling restart in
    which the worker runs happily against a schema the backend refuses to start on.
    """
    conn, _url, directory = upgrading
    edited = directory / f"{LAST_BEFORE}.sql"
    edited.write_text(
        edited.read_text(encoding="utf-8") + "\n-- an operator fixing a typo in place\n",
        encoding="utf-8",
    )

    for call in (migrate.apply_all(conn, directory), migrate.pending(conn, directory)):
        with pytest.raises(RuntimeError, match=LAST_BEFORE):
            await call


async def test_a_renamed_applied_migration_is_named_rather_than_dying_on_its_own_ddl(upgrading):
    """The other direction of the checksum guard: the row with no file. [M4.7 data-08]

    `applied - discovered` was never inspected, so a rename was invisible from the side that
    could explain it and loud from the side that could not: the file re-runs under its new
    version, and the operator gets a DuplicateTableError naming a table it never occurred to
    them to connect with the migration they renamed, while the old version row sits in
    `schema_migration` with nothing reporting it. Reproduced exactly that way against
    `0015_seed.sql` before this refusal existed.

    Only `apply_all` refuses, because only `apply_all` executes DDL: `pending` is the worker
    waiting, and a worker that declines to start over a rename the backend is about to reject
    anyway adds nothing. What is asserted is that the run stops before the renamed file is
    treated as new work.
    """
    conn, _url, directory = upgrading
    renamed = f"{LAST_BEFORE}_v2"
    (directory / f"{LAST_BEFORE}.sql").rename(directory / f"{renamed}.sql")

    with pytest.raises(RuntimeError, match=LAST_BEFORE):
        await migrate.apply_all(conn, directory)

    assert await conn.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = $1", renamed
    ) == 0, "the renamed file must not have been applied as though it were a new migration"


async def test_two_concurrent_applies_both_complete_and_leave_one_row_per_migration(pg_url):
    """Compose starts backend and worker together, and this is what that used to do.

    Reproduced with no lock at all: two `apply_all` calls against one fresh database left one
    of them dead with a unique violation on `pg_type_typname_nsp_index` — two sessions issuing
    the same CREATE TABLE — and the release half applied. The advisory lock is session-level
    and held across the whole run precisely because each migration commits separately, so the
    split asserted here is total: one caller does every migration and the other, arriving after
    the last COMMIT, correctly finds nothing to do. An interleaved split would mean the lock
    was released between migrations, which is what `pg_advisory_xact_lock` would have done.
    [M4.7 data-08]
    """
    admin, name, url = _sibling(pg_url, "_concurrent")
    await _recreate(admin, name)
    first = await asyncpg.connect(url)
    second = await asyncpg.connect(url)
    try:
        both = await asyncio.gather(migrate.apply_all(first), migrate.apply_all(second))

        release = [version for version, _ in migrate.discover()]
        assert sorted(both[0] + both[1]) == release, "neither caller may skip or repeat one"
        assert sorted(len(half) for half in both) == [0, len(release)], (
            f"the lock did not hold across the run: {[len(half) for half in both]}"
        )
        recorded = [
            r["version"]
            for r in await first.fetch("SELECT version FROM schema_migration ORDER BY version")
        ]
        assert recorded == release
        assert await migrate.pending(first) == []
    finally:
        await first.close()
        await second.close()
        await _drop(admin, name)


async def test_a_dump_from_an_older_release_leaves_ddl_the_next_boot_cannot_apply(fresh, tmp_path):
    """README's Recovery block: "a dump restores only into the image that wrote it".

    `pg_restore --clean --if-exists` is what makes a restore into a booted target safe, and it is
    only half a rule: `--clean` drops what the *archive* holds. An object a later release added is
    not in an older archive, is not dropped, and is still there when the app comes back up and the
    migration runner reaches the file that creates it. Nothing in this repository said so, and the
    restore is the one procedure read at a moment when nothing else is.

    Both halves of README's paragraph are asserted here, because the obvious way back is the one
    that does not work. Going back to the release the dump was taken on refuses at the door: that
    build has no file for a migration the database records, which is the orphan refusal. And
    restoring first does not rescue it either — the newer release's tables are still standing, so
    the second upgrade dies exactly where the first one did. What does work is the third path, a
    database with nothing newer in it, and it is
    `test_the_newest_migrations_apply_over_populated_tables_and_land_this_builds_schema` above:
    the restored schema is simply an older release's, which is the case that drill already covers.

    The restore itself is built from its two observable effects rather than through `pg_dump`:
    `schema_migration` comes back from the archive ending at 0016, `data_encryption_key` is
    dropped and recreated so 0017's index on it goes too, and `job_run` — which the archive never
    heard of — survives untouched. The orphan refusal cannot catch that one: `applied -
    discovered` is empty, because this database is *behind* the build rather than ahead of it.
    [M4.7 ops-01, data-08]
    """
    # 0016 and 0017 by name rather than "the newest two": this is the release pair the README
    # paragraph was written against, and re-pointing the drill at whichever pair is newest would
    # keep rewriting it instead of holding it. What a later migration DOES move is the restore
    # below rather than the pair, and the original comment here claimed otherwise. [M4.9]
    older = _stage(tmp_path, "0016_users")

    # 1. `git checkout <the older release> && docker compose up -d --build`, first thing.
    with pytest.raises(RuntimeError, match="0017_ops"):
        await migrate.apply_all(fresh, older)

    # 2. The restore that was supposed to precede it, and what it leaves behind. Keyed on "newer
    # than the archive" rather than on 0017 by name, because that is what `pg_restore --clean`
    # does to `schema_migration`: it drops the table and reloads the archive's copy, which ends
    # at 0016 however many releases have shipped since. Naming one version left M4.9's 0018
    # recorded here, and the older release then refused on an orphan the restore had removed --
    # a failure of the model, not of the runner, which was reporting the state it was handed.
    await fresh.execute("DELETE FROM schema_migration WHERE version > '0016_users'")
    await fresh.execute("DROP INDEX data_encryption_key_one_active")
    assert await fresh.fetchval("SELECT to_regclass('public.job_run')") is not None, (
        "the leftover table is the whole subject: --clean drops only what the archive carries"
    )
    assert await migrate.apply_all(fresh, older) == [], "the older release now starts, and is fine"

    # 3. ...until this build comes back, which is where the operator finds out.
    with pytest.raises(asyncpg.DuplicateTableError, match="job_run"):
        await migrate.apply_all(fresh)

    assert await fresh.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = '0017_ops'"
    ) == 0, "the failed migration must not be recorded as applied"


# --- plan section 8: the index that can refuse to start on a real install ----------------------


async def _run_cli(*argv: str) -> int:
    """`spielplan-secrets` as an operator runs it, argparse and `asyncio.run` included.

    In a thread because `main` calls `asyncio.run`, which refuses to nest inside the loop pytest
    is already running.
    """
    return await asyncio.to_thread(secrets_cli.main, list(argv))


@pytest.fixture
async def raced(pg_url, tmp_path, monkeypatch):
    """The install that lost sec-10's race: everything but 0017, and two un-retired DEK rows.

    Reachable only before 0017 exists, which is exactly why it has to be constructed here — the
    index this milestone adds is what makes it unreachable afterwards, and the migration is the
    moment the existing rows are examined for the first time.
    """
    admin, name, url = _sibling(pg_url, "_raced")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    directory = _stage(tmp_path, "0016_users")
    await migrate.apply_all(conn, directory)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SECRETS_KEY", OLD_KEY)
    settings.cache_clear()
    try:
        yield conn, directory
    finally:
        await conn.close()
        await _drop(admin, name)
        settings.cache_clear()


async def _refuses_at_the_index(conn, directory) -> list[str]:
    """Apply the rest and assert 0017 aborts, leaving nothing of itself behind.

    Returns what `_complete` staged in -- the rest of the release, 0017 first -- because the
    claim the two callers make afterwards is that the documented repair lets the run finish,
    not that 0017 is the last migration in the tree. It was when this drill was written and
    stopped being when M4.9 added 0018, so the assertion is restated against the release the
    fixture actually staged rather than against a version number. [M4.9]
    """
    rest = _complete(directory)
    with pytest.raises(asyncpg.UniqueViolationError, match="data_encryption_key_one_active"):
        await migrate.apply_all(conn, directory)
    # Each migration is its own transaction (db/migrate.py:119), so the failure leaves no half of
    # 0017: no version row, and the table its second half creates does not exist.
    assert await conn.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = '0017_ops'"
    ) == 0
    assert await conn.fetchval("SELECT to_regclass('public.job_run')") is None
    return rest


async def test_two_active_dek_rows_abort_the_boot_and_the_documented_update_repairs_it(raced):
    """Plan §8: "the first thing this milestone does on a real box is refuse to start" — found
    here, on purpose, rather than there.

    `0017_ops.sql` says out loud that the statement can fail and prints the repair in its own
    comment, because a migration that aborts at boot leaves an operator with a crash-looping
    container and a duplicate-key error naming an index they have never heard of. This is that
    error, and that repair, executed.
    """
    conn, directory = raced
    await _seed_dek(conn, "the-winner", OLD_KEY)
    await _seed_dek(conn, "the-loser", OLD_KEY)

    rest = await _refuses_at_the_index(conn, directory)

    # The repair 0017's comment gives, verbatim in shape: retire, never delete — `load_dek`
    # finds a retired row by id, so every ciphertext naming the loser still opens.
    await conn.execute(
        "UPDATE data_encryption_key SET retired_at = now() WHERE key_id = 'the-loser'"
    )
    assert await migrate.apply_all(conn, directory) == rest
    assert await conn.fetchval("SELECT count(*) FROM data_encryption_key") == 2


async def test_reset_clears_the_way_when_the_racing_rows_are_the_ones_it_can_recognise(
    raced, monkeypatch, capsys
):
    """The other half of the same box: it lost the race *and* the `.env` is not the one that
    wrapped those rows — a restored dump, or a regenerated `SECRETS_KEY`.

    Here `spielplan-secrets reset` is the whole repair, and it is the repair plan §8 asks for:
    it retires what it cannot open, so the index has nothing left to refuse, and it says which
    ciphertexts it emptied before they are gone.
    """
    conn, directory = raced
    await _seed_dek(conn, "the-winner", OLD_KEY)
    await _seed_dek(conn, "the-loser", OLD_KEY)
    await conn.execute(
        "INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id) "
        "VALUES ('jellyfin', '{}'::jsonb, $1, 'the-winner')",
        b"a-sealed-blob",
    )
    rest = await _refuses_at_the_index(conn, directory)

    monkeypatch.setenv("SECRETS_KEY", NEW_KEY)
    settings.cache_clear()
    assert await _run_cli("reset") == 0
    printed = capsys.readouterr().out
    assert "the-winner" in printed and "the-loser" in printed, printed
    assert "connector_config/jellyfin" in printed, printed

    assert await conn.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 0
    assert await migrate.apply_all(conn, directory) == rest


async def test_reset_declines_the_race_it_was_asked_to_repair_when_both_rows_still_open(
    raced, capsys
):
    """Recorded because it is what the code does, and it is not what plan §8 asked for.

    §8: "give `spielplan-secrets reset` the ability to retire the loser — otherwise the first
    thing this milestone does on a real box is refuse to start." `reset` retires the rows this
    `SECRETS_KEY` *cannot* open, and sec-10's race produced two rows minted by `ensure_dek`
    under the same key, so on the install §8 is describing both rows open and `reset` correctly
    declines to destroy either. It cannot choose between them, and neither could anything else
    without asking: the two rows are equally valid and only the operator knows which one the
    ciphertexts they care about name.

    So the repair path on that box is the `UPDATE` above, printed by the migration that
    refused — which works, and which the test above executes. This test exists so that the gap
    between §8's sentence and the shipped behaviour is written down where the next person to
    read the milestone will find it, rather than discovered a second time.
    """
    conn, _directory = raced
    await _seed_dek(conn, "the-winner", OLD_KEY)
    await _seed_dek(conn, "the-loser", OLD_KEY)

    assert await _run_cli("reset") == 0
    assert "Custody is intact" in capsys.readouterr().out
    assert await conn.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 2
