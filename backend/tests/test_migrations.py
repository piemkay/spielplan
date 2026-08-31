"""Migrations apply cleanly, and the structure they produce is the one §4.1 requires.

Runs against PGlite — a real Postgres compiled to wasm — so DDL errors are caught on a machine
with no Docker. Skips when `tests/pglite/node_modules` is absent; see tests/pglite/README.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from spielplan.db import migrate

HERE = Path(__file__).resolve().parent
PGLITE = HERE / "pglite"
MIGRATIONS = HERE.parent / "migrations"


def _run() -> dict:
    node = shutil.which("node")
    if node is None or not (PGLITE / "node_modules").is_dir():
        pytest.skip("node + tests/pglite/node_modules required (see tests/pglite/README.md)")
    out = subprocess.run(
        [node, str(PGLITE / "apply.mjs"), str(MIGRATIONS)],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def schema() -> dict:
    return _run()


def _names(schema: dict, table_schema: str) -> set[str]:
    return {r["table_name"] for r in schema["relations"] if r["table_schema"] == table_schema}


def test_every_migration_applies(schema):
    assert schema["ok"]
    assert schema["applied"] == [p.name for p in sorted(MIGRATIONS.glob("*.sql"))]


def test_discovery_returns_every_migration_in_filename_order(tmp_path):
    """`assert sorted(x) == x` against a function that returns sorted() cannot fail. Feed it
    files created out of order and check the ORDER, and that nothing is dropped."""
    for name in ("0003_c.sql", "0001_a.sql", "0010_j.sql", "0002_b.sql", "notes.txt"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")

    found = migrate.discover(tmp_path)
    assert [v for v, _ in found] == ["0001_a", "0002_b", "0003_c", "0010_j"]
    assert all(sql == "SELECT 1;" for _, sql in found)


def test_the_real_migrations_are_discovered_in_order():
    versions = [v for v, _ in migrate.discover(MIGRATIONS)]
    assert versions == [p.stem for p in sorted(MIGRATIONS.glob("*.sql"))]
    assert versions[0].startswith("0001")


def test_bootstrap_stripping_removes_only_the_schema_migration_table():
    """The production runner rewrites 0001 before executing it (the runner creates
    `schema_migration` itself with IF NOT EXISTS). PGlite applies the raw file, so this rewrite
    is otherwise never exercised — and a bad strip would silently delete real DDL."""
    original = (MIGRATIONS / "0001_system.sql").read_text(encoding="utf-8")
    stripped = migrate._strip_bootstrap(original)

    assert "CREATE TABLE schema_migration (" not in stripped
    # Everything else in the file survives, byte for byte.
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


def test_display_only_schema_exists_and_holds_only_platform_ratings(schema):
    """§4.1 rule 3: platform_rating lives in a display-only schema the feature builder cannot
    import from. A separate schema is what makes that grantable rather than aspirational."""
    assert _names(schema, "display") == {"platform_rating"}


def test_reviews_live_in_their_own_schema(schema):
    """§4.1: 'review (separate schema or DB — 312 MB with bodies)'."""
    assert "review" in _names(schema, "review_store")


def test_both_dna_tiers_exist_as_separate_tables(schema):
    """§4.1 rule 1: never merged, never unioned."""
    public = _names(schema, "public")
    assert "dna_tag" in public
    assert "dna_projected" in public
    assert "dna_evidence" in public


def test_the_only_view_is_the_tier_labelled_one(schema):
    views = {r["table_name"] for r in schema["relations"] if r["table_type"] == "VIEW"}
    assert views == {"dna_tagged"}, (
        "the tier discriminator is enforced by there being exactly one sanctioned way to read "
        f"both DNA tiers together; found views: {sorted(views)}"
    )


def test_user_state_tables_match_the_spec_block(schema):
    """§4.2 names these tables explicitly; the names are part of the contract."""
    public = _names(schema, "public")
    for table in (
        "user_title", "verdict", "duel", "tier_edit", "ledger_state", "ledger_cutpoints",
        "user_vector", "push_subscription", "playback_event", "acquisition_job",
        "connector_config", "artifact_bundle",
    ):
        assert table in public, f"§4.2 names `{table}` and it is not in the schema"


def test_auth_session_does_not_squat_on_the_tonight_session_name(schema):
    """§4.2 reserves the bare name `session` for a Tonight session."""
    public = _names(schema, "public")
    assert "auth_session" in public
    assert "session" not in public


def _columns(schema: dict, table: str, table_schema: str = "public") -> dict[str, dict]:
    return {
        r["column_name"]: r
        for r in schema["columns"]
        if r["table_schema"] == table_schema and r["table_name"] == table
    }


def test_the_ledger_output_columns_the_rank_board_reads_exist(schema):
    """§6.3's board and its badges are read off `ledger_state`, and both of the columns that
    carry a badge arrived by ALTER rather than in the CREATE — which `relations` cannot see.

    `tier` and `straddle` come from 0005, `kind` and `sigma_eff` from 0010. A migration that
    applied cleanly and added none of them would have passed every other assertion in this file.
    """
    columns = _columns(schema, "ledger_state")
    for name in ("kind", "s", "sigma", "sigma_eff", "cdf", "tier", "straddle", "observed"):
        assert name in columns, f"ledger_state.{name} is missing"


def test_a_tier_set_change_has_somewhere_to_record_the_refit_it_owes(schema):
    """Decision 11: changing the tier set "queues a Ledger refit for that user alone".

    0012 adds `ledger_cutpoints.refit_requested_at` for exactly that, with a partial index so
    the worker's sweep does not scan a table that grows with the household. Both halves are
    asserted: a nullable column nobody indexed would make the sweep a table scan, and an index
    over every row would defeat the point of the column being empty almost always.
    """
    columns = _columns(schema, "ledger_cutpoints")
    assert "refit_requested_at" in columns, "0012 must add ledger_cutpoints.refit_requested_at"
    assert columns["refit_requested_at"]["is_nullable"] == "YES", (
        "a queued refit is an exception, so its absence must be representable"
    )
    # §4.2 already keyed the row (user_id, kind) and already held the tier set; 0012 adds one
    # column and nothing else, which is what "decision 11 needs no new table" means.
    for name in ("user_id", "kind", "boundaries", "tier_set"):
        assert name in columns

    owed = [
        r for r in schema["indexes"]
        if r["table_name"] == "ledger_cutpoints" and "refit_requested_at" in r["indexdef"]
    ]
    assert owed, "the refit sweep needs an index on ledger_cutpoints.refit_requested_at"
    assert any("WHERE" in r["indexdef"].upper() for r in owed), (
        "the index must be partial: the answer is empty almost always"
    )
