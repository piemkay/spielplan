"""Every other layer migrates an EMPTY database, so no backfill has run over existing rows. This
file migrates to an older release, seeds rows, applies the rest, and compares with a fresh install."""

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

# The last migration before 0015's backfill. Naming the first half keeps every later migration in
# "the rest" automatically.
LAST_BEFORE = "0014_tonight_undo"

OLD_KEY = "the-secrets-key-this-install-was-built-with"
NEW_KEY = "a-different-secrets-key-not-a-real-one"

# The schemas this application owns.
SCHEMAS = ("public", "display", "review_store")


def _stage(tmp_path: Path, upto: str) -> Path:
    """Copied byte for byte: any other change would fail the checksum for the wrong reason."""
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
    """All four, because the migrations under test each change a different one; columns alone would
    miss an index never built."""
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
    """One row in every table the newest migrations rewrite, chosen by reading them. `ledger_state`
    and `user_score` get a cross-kind row each: 0022's DELETEs are what let its composite FK
    validate. Tables whose FK only moved to RESTRICT are not seeded."""
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
    # Two bundles: 0015 stamps the oldest 'seed' and the rest 'model'.
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
    """Twelve labels, so the backfilled `n_levels` differs from the fallback 7. Title 12 is a series
    with a 'movie' copy: what a corpus reclassification leaves."""
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
    # Canonical order, no repeat: what `rate_session_kinds_distinct` validates.
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
    """`_wrap`, not `ensure_dek`: two un-retired rows need a race `ensure_dek` cannot be made to lose."""
    await conn.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ($1, $2)",
        key_id,
        sec._wrap(os.urandom(32), secrets_key),
    )


@pytest.fixture
async def upgrading(pg_url, tmp_path):
    """A database of its own: the schema is deliberately not this build's."""
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


async def test_the_newest_migrations_apply_over_populated_tables_and_land_this_builds_schema(
    upgrading, fresh, tmp_path
):
    """Applying the sequence over real rows must succeed and land the fresh install's schema."""
    conn, _url, directory = upgrading
    pending = _complete(directory)
    assert pending, "the drill is applying nothing; LAST_BEFORE is the newest migration"

    applied = await migrate.apply_all(conn, directory)

    assert applied == pending
    assert await migrate.pending(conn, directory) == []
    upgraded = await _schema(conn)
    reference = await _schema(fresh)
    # The control: two empty snapshots compare equal.
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
    """0015's `UPDATE artifact_bundle SET kind = 'model'`, finally over rows; the other rows test
    that ADD COLUMN, RENAME and ADD PRIMARY KEY carry data."""
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

    # A rename that dropped the value would still match a fresh schema.
    assert await conn.fetchval("SELECT billing_order FROM credit WHERE title_id = 11") == 3
    # NOT NULL with a default, added over rows that predate it.
    assert await conn.fetchval("SELECT source FROM title_language WHERE title_id = 11") == ""
    assert await conn.fetchval(
        "SELECT metric FROM display.platform_rating WHERE title_id = 11"
    ) == "user_score"
    assert await conn.fetchval(
        "SELECT blocks_unmapped FROM title_placement WHERE title_id = 11"
    ) == "{}"
    # A bigserial numbering existing rows, each keeping its own verdict.
    numbered = await conn.fetch("SELECT id, term, scope FROM dna_adjudication ORDER BY term")
    assert [r["term"] for r in numbered] == ["melancholy", "rain"]
    assert len({r["id"] for r in numbered}) == 2 and all(r["scope"] == "global" for r in numbered)
    # 0016 over accounts that predate its CHECK and columns.
    assert await conn.fetchval(
        "SELECT count(*) FROM app_user WHERE password_failed_count = 0"
    ) == 2

    # 0022's DELETEs, which only run meaningfully over pre-existing cross-kind rows.
    async def kinds(table: str) -> set[tuple[int, str]]:
        return {
            (r["title_id"], r["kind"])
            for r in await conn.fetch(f"SELECT title_id, kind FROM {table}")  # noqa: S608
        }

    assert await kinds("ledger_state") == {(11, "movie")}, (
        "the row whose kind disagreed with its title's is gone and the agreeing one survives"
    )
    assert await kinds("user_score") == {(11, "movie")}
    # Only that the backfill ran over rows it did not create; the value is
    # `test_schema_contracts.py`'s.
    assert await conn.fetchval("SELECT n_levels FROM tier_edit WHERE title_id = 11") == 12


async def test_an_edited_applied_migration_stops_the_upgrade_from_both_entry_points(upgrading):
    """Both entry points: the backend applies and the worker waits."""
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
    """A renamed file re-ran as new and died on its own DDL; now `apply_all` names the orphan first."""
    conn, _url, directory = upgrading
    renamed = f"{LAST_BEFORE}_v2"
    (directory / f"{LAST_BEFORE}.sql").rename(directory / f"{renamed}.sql")

    with pytest.raises(RuntimeError, match=LAST_BEFORE):
        await migrate.apply_all(conn, directory)

    assert await conn.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = $1", renamed
    ) == 0, "the renamed file must not have been applied as though it were a new migration"


async def test_two_concurrent_applies_both_complete_and_leave_one_row_per_migration(pg_url):
    """The advisory lock is session-level across the whole run, so one caller does every migration."""
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
    """`--clean` drops only what the archive holds, so a newer release's objects survive and the next
    boot fails on them. Going back to the older release refuses on the orphan."""
    # 0016 and 0017 by name: the release pair README's paragraph was written against.
    older = _stage(tmp_path, "0016_users")

    # 1. The older release's build, first thing.
    with pytest.raises(RuntimeError, match="0017_ops"):
        await migrate.apply_all(fresh, older)

    # 2. The restore reloads `schema_migration` ending at 0016, whatever shipped since.
    await fresh.execute("DELETE FROM schema_migration WHERE version > '0016_users'")
    await fresh.execute("DROP INDEX data_encryption_key_one_active")
    assert await fresh.fetchval("SELECT to_regclass('public.job_run')") is not None, (
        "the leftover table is the whole subject: --clean drops only what the archive carries"
    )
    assert await migrate.apply_all(fresh, older) == [], "the older release now starts, and is fine"

    # 3. ...until this build comes back.
    with pytest.raises(asyncpg.DuplicateTableError, match="job_run"):
        await migrate.apply_all(fresh)

    assert await fresh.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = '0017_ops'"
    ) == 0, "the failed migration must not be recorded as applied"


async def _run_cli(*argv: str) -> int:
    """In a thread because `main` calls `asyncio.run`."""
    return await asyncio.to_thread(secrets_cli.main, list(argv))


@pytest.fixture
async def raced(pg_url, tmp_path, monkeypatch):
    """Only constructible before 0017, whose index then makes it unreachable."""
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
    """Returns what `_complete` staged: the repair must let the run finish, whatever comes after 0017."""
    rest = _complete(directory)
    with pytest.raises(asyncpg.UniqueViolationError, match="data_encryption_key_one_active"):
        await migrate.apply_all(conn, directory)
    # Each migration is its own transaction, so a failure leaves no half of 0017.
    assert await conn.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = '0017_ops'"
    ) == 0
    assert await conn.fetchval("SELECT to_regclass('public.job_run')") is None
    return rest


async def test_two_active_dek_rows_abort_the_boot_and_the_documented_update_repairs_it(raced):
    """0017 aborts at boot and prints its repair; this executes both."""
    conn, directory = raced
    await _seed_dek(conn, "the-winner", OLD_KEY)
    await _seed_dek(conn, "the-loser", OLD_KEY)

    rest = await _refuses_at_the_index(conn, directory)

    # Retire, never delete: `load_dek` finds a retired row by id.
    await conn.execute(
        "UPDATE data_encryption_key SET retired_at = now() WHERE key_id = 'the-loser'"
    )
    assert await migrate.apply_all(conn, directory) == rest
    assert await conn.fetchval("SELECT count(*) FROM data_encryption_key") == 2


async def test_reset_clears_the_way_when_the_racing_rows_are_the_ones_it_can_recognise(
    raced, monkeypatch, capsys
):
    """With a foreign `.env`, `reset` retires what it cannot open and the index has nothing to refuse."""
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
    """`reset` cannot choose between two rows it can open; the repair is 0017's printed UPDATE. Recorded
    so the gap with plan §8's wording is not rediscovered."""
    conn, _directory = raced
    await _seed_dek(conn, "the-winner", OLD_KEY)
    await _seed_dek(conn, "the-loser", OLD_KEY)

    assert await _run_cli("reset") == 0
    assert "Custody is intact" in capsys.readouterr().out
    assert await conn.fetchval(
        "SELECT count(*) FROM data_encryption_key WHERE retired_at IS NULL"
    ) == 2
