"""The migration runner. The facet-backfill test at the foot needs TEST_DATABASE_URL."""

from __future__ import annotations

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


def _facet_backfill_statements() -> list[str]:
    """Section 1's UPDATEs read from the shipped file. Comments are stripped before splitting on `;`
    because header prose contains semicolons."""
    body = (MIGRATIONS / "0018_read_layer.sql").read_text(encoding="utf-8")
    code = " ".join(
        line for line in body.splitlines() if not line.strip().startswith("--")
    )
    found = [s.strip() + ";" for s in code.split(";") if "split_part(term" in s]
    assert len(found) == 2 and all(s.startswith("UPDATE") for s in found), (
        f"0018 section 1 is two UPDATEs, one per DNA tier; found {found}"
    )
    return found


async def test_the_dna_facet_backfill_repairs_each_row_once_and_then_changes_nothing(db):
    """Runs the shipped UPDATEs twice; the second must read `UPDATE 0`. The `term LIKE '%.%'` guard
    matters because split_part returns the whole string when there is no dot."""
    statements = _facet_backfill_statements()
    await db.execute("INSERT INTO title (id, kind, name) VALUES (1, 'movie', 'x')")
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 3)"
    )
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) VALUES "
        "(1, 'v1', 'characters.morally_grey', 'character_dynamics', 2), "
        "(1, 'v1', 'mood.dread', 'mood', 3), "
        "(1, 'v1', 'undotted_legacy_term', 'legacy', 1)"
    )
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) VALUES "
        "(1, 'v1', 'themes.obsession', 'narrative_themes', 0.5), "
        "(1, 'v1', 'undotted_legacy_term', 'legacy', 0.5)"
    )

    first = [await db.execute(s) for s in statements]
    assert first == ["UPDATE 1", "UPDATE 1"], (
        f"one mismatched row per tier, and neither the already-correct nor the undotted: {first}"
    )
    assert await db.fetchval(
        "SELECT facet FROM dna_tag WHERE term = 'characters.morally_grey'"
    ) == "characters"
    assert await db.fetchval(
        "SELECT facet FROM dna_projected WHERE term = 'themes.obsession'"
    ) == "themes"
    kept = await db.fetch(
        "SELECT facet FROM dna_tag WHERE term = 'undotted_legacy_term' "
        "UNION ALL SELECT facet FROM dna_projected WHERE term = 'undotted_legacy_term'"
    )
    assert [r["facet"] for r in kept] == ["legacy", "legacy"], (
        "the LIKE '%.%' guard is what keeps split_part from rewriting an undotted vocabulary's "
        "facet to the term id itself"
    )

    again = [await db.execute(s) for s in statements]
    assert again == ["UPDATE 0", "UPDATE 0"], f"the backfill is not idempotent: {again}"

    transaction = db.transaction()
    await transaction.start()
    try:
        assert await db.fetchval("SELECT split_part('undotted_legacy_term', '.', 1)") == (
            "undotted_legacy_term"
        ), "split_part returns the whole string with no delimiter; field 2 is the empty one"
        await db.execute("UPDATE dna_tag SET facet = split_part(term, '.', 1)")
        assert await db.fetchval(
            "SELECT facet FROM dna_tag WHERE term = 'undotted_legacy_term'"
        ) == "undotted_legacy_term", (
            "the unguarded form writes the term id into `facet`, which is a value that joins no "
            "`dna_facet` row -- not the NULL a NOT NULL column would have refused"
        )
    finally:
        await transaction.rollback()
