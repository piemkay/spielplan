"""The migration runner. The two refusals at the foot need TEST_DATABASE_URL."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spielplan.db import migrate

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE.parent / "migrations"


def test_discovery_returns_every_migration_in_filename_order(tmp_path):
    """Files created out of order: `sorted(x) == x` over sorted() output could never fail."""
    for name in ("0003_c.sql", "0001_a.sql", "0010_j.sql", "0002_b.sql", "notes.txt"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")

    found = migrate.discover(tmp_path)
    assert [v for v, _ in found] == ["0001_a", "0002_b", "0003_c", "0010_j"]
    assert all(sql == "SELECT 1;" for _, sql in found)


def test_the_real_migrations_are_discovered_in_order():
    versions = [v for v, _ in migrate.discover(MIGRATIONS)]
    assert versions == [p.stem for p in sorted(MIGRATIONS.glob("*.sql"))]
    assert versions[0].startswith("0001")


async def test_a_missing_migrations_directory_is_refused_before_the_database_is_touched(tmp_path):
    """`glob` on a missing directory yields nothing, which booted as an empty schema.
    `None` as the connection proves the refusal comes before any query."""
    absent = tmp_path / "migrations-that-were-never-shipped"
    for call in (migrate.apply_all(None, absent), migrate.pending(None, absent)):
        with pytest.raises(RuntimeError, match="no migrations directory"):
            await call


def test_bootstrap_stripping_removes_only_the_schema_migration_table():
    original = (MIGRATIONS / "0001_system.sql").read_text(encoding="utf-8")
    stripped = migrate._strip_bootstrap(original)

    assert "CREATE TABLE schema_migration (" not in stripped
    for statement in (
        "CREATE TABLE data_encryption_key (",
        "CREATE TABLE connector_config (",
        "CREATE TABLE artifact_bundle (",
        "CREATE UNIQUE INDEX artifact_bundle_one_active",
        "CREATE TABLE setup_step (",
    ):
        assert statement in stripped, f"bootstrap stripping ate {statement!r}"
    assert len(stripped) < len(original)


def test_bootstrap_stripping_is_a_no_op_on_a_file_without_the_table():
    body = "CREATE TABLE unrelated (id integer);"
    assert migrate._strip_bootstrap(body) == body


async def test_an_edited_applied_migration_is_refused_by_both_entry_points(db, tmp_path):
    """The backend applies and the worker waits; each must name the edited file."""
    directory = shutil.copytree(MIGRATIONS, tmp_path / "migrations")
    edited = directory / "0014_tonight_undo.sql"
    edited.write_text(
        edited.read_text(encoding="utf-8") + "\n-- an operator fixing a typo in place\n",
        encoding="utf-8",
    )

    for call in (migrate.apply_all(db, directory), migrate.pending(db, directory)):
        with pytest.raises(RuntimeError, match="0014_tonight_undo"):
            await call


async def test_a_renamed_applied_migration_is_named_rather_than_run_again(db, tmp_path):
    directory = shutil.copytree(MIGRATIONS, tmp_path / "migrations")
    (directory / "0014_tonight_undo.sql").rename(directory / "0014_tonight_undo_v2.sql")

    with pytest.raises(RuntimeError, match="0014_tonight_undo"):
        await migrate.apply_all(db, directory)
    assert await db.fetchval(
        "SELECT count(*) FROM schema_migration WHERE version = '0014_tonight_undo_v2'"
    ) == 0
