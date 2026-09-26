"""Migrations against PGlite; skips without tests/pglite/node_modules.
The facet-backfill test at the foot needs TEST_DATABASE_URL instead."""

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
    """PGlite applies 0001 raw, so the runner's bootstrap strip is otherwise never exercised."""
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


def test_display_only_schema_exists_and_holds_only_platform_ratings(schema):
    """§4.1 rule 3: a separate schema is what makes display-only grantable."""
    assert _names(schema, "display") == {"platform_rating"}


def test_reviews_live_in_their_own_schema(schema):
    assert "review" in _names(schema, "review_store")


def test_both_dna_tiers_exist_as_separate_tables(schema):
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
    public = _names(schema, "public")
    for table in (
        "user_title", "verdict", "duel", "tier_edit", "ledger_state", "ledger_cutpoints",
        "user_vector", "push_subscription", "playback_event", "acquisition_job",
        "connector_config", "artifact_bundle",
    ):
        assert table in public, f"§4.2 names `{table}` and it is not in the schema"


def test_auth_session_does_not_squat_on_the_tonight_session_name(schema):
    public = _names(schema, "public")
    assert "auth_session" in public
    assert "session" in public, "0013 creates §4.2's Tonight `session`, and the name was held for it"

    auth = _columns(schema, "auth_session")
    tonight = _columns(schema, "session")
    # What carries the cookie: the four session columns, and a text id (a bigserial is guessable).
    assert {"id", "user_id", "expires_at", "auth_method"} <= set(auth), sorted(auth)
    assert auth["id"]["data_type"] == "text", (
        "§3.2's session id is opaque and signed into the cookie, so it is not a serial"
    )
    assert "host_user_id" in tonight and "room_code" in tonight
    assert "host_user_id" not in auth and "room_code" not in auth


def _columns(schema: dict, table: str, table_schema: str = "public") -> dict[str, dict]:
    return {
        r["column_name"]: r
        for r in schema["columns"]
        if r["table_schema"] == table_schema and r["table_name"] == table
    }


def test_the_ledger_output_columns_the_rank_board_reads_exist(schema):
    """`tier`/`straddle` (0005) and `kind`/`sigma_eff` (0010) arrive by ALTER, unseen by `relations`."""
    columns = _columns(schema, "ledger_state")
    for name in ("kind", "s", "sigma", "sigma_eff", "cdf", "tier", "straddle", "observed"):
        assert name in columns, f"ledger_state.{name} is missing"


def test_a_tier_set_change_has_somewhere_to_record_the_refit_it_owes(schema):
    """A nullable column plus a partial index, so the refit sweep never scans the whole table."""
    columns = _columns(schema, "ledger_cutpoints")
    assert "refit_requested_at" in columns, "0012 must add ledger_cutpoints.refit_requested_at"
    assert columns["refit_requested_at"]["is_nullable"] == "YES", (
        "a queued refit is an exception, so its absence must be representable"
    )
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


def test_the_tonight_session_tables_match_the_spec_block(schema):
    """`session_ballot` is not in §4.2; 54g adds it because approval share exists only there."""
    public = _names(schema, "public")
    for table in (
        "session", "session_participant", "session_answer",
        "session_ballot", "session_result", "session_outcome",
    ):
        assert table in public, f"§4.2/54g names `{table}` and it is not in the schema"


def test_a_guest_seat_is_addressable_without_a_user_id(schema):
    """Two guests on one phone is the designed case, so the seat has its own id and
    one-seat-per-member is a partial unique index NULLs skip."""
    columns = _columns(schema, "session_participant")
    assert columns["user_id"]["is_nullable"] == "YES", "a guest seat has no user_id"
    assert "id" in columns, "a seat needs an identity of its own, not (session_id, user_id)"
    for name in ("session_id", "role", "tilt", "answered_count", "joined_at",
                 "converged_at", "ended_by"):
        assert name in columns, f"§4.2/54g names session_participant.{name}"
    assert columns["converged_at"]["is_nullable"] == "YES", (
        "54g: converged_at is stamped only in the converged case"
    )

    member_seat = [
        r for r in schema["indexes"]
        if r["table_name"] == "session_participant"
        and "UNIQUE" in r["indexdef"].upper()
        and "user_id" in r["indexdef"]
    ]
    assert member_seat, "one member may hold at most one seat in a session"
    assert all("WHERE" in r["indexdef"].upper() for r in member_seat), (
        "the unique seat index must be partial, or it cannot admit two NULL guest seats"
    )


def test_the_session_answer_columns_the_round_writes_exist(schema):
    columns = _columns(schema, "session_answer")
    for name in ("session_id", "participant_id", "seq", "title_a", "title_b",
                 "answer", "latency_ms", "selection", "retracted_at"):
        assert name in columns, f"§4.2/54g names session_answer.{name}"


def test_the_held_out_session_answers_are_addressable(schema):
    holdout = [
        r for r in schema["indexes"]
        if r["table_name"] == "session_answer" and "uniform_holdout" in r["indexdef"]
    ]
    assert holdout, "session_answer needs a partial index on the uniform_holdout stream"


def test_the_result_slate_and_its_outcome_are_two_tables(schema):
    result = _columns(schema, "session_result")
    for name in ("session_id", "title_id", "rank", "group_score", "per_user_match", "conflict"):
        assert name in result, f"§4.2 names session_result.{name}"
    assert result["conflict"]["is_nullable"] == "YES", (
        "§6.2 step 5: below D = 0.20 the split is decided silently, so no conflict is stored"
    )
    # 54d's label is a boolean orthogonal to `slot`, so slot filters still count the reserved title.
    assert "reserved" in result, "54d's reserved slot has to be labelled somewhere"
    assert result["reserved"]["is_nullable"] == "NO", (
        "a card either is the far side of the split or is not; there is no unknown"
    )
    outcome = _columns(schema, "session_outcome")
    for name in ("session_id", "chosen_title_id", "approval_share", "participants"):
        assert name in outcome, f"§4.2 names session_outcome.{name}"


def test_no_v11_posterior_columns_survive_anywhere_in_the_schema(schema):
    public = _names(schema, "public")
    assert "fairness_ledger" not in public
    banned = {"mu", "sigma", "tolerance", "phase"}
    for table in ("session", "session_participant", "session_answer",
                  "session_ballot", "session_result", "session_outcome"):
        present = set(_columns(schema, table)) & banned
        assert not present, f"{table} carries deleted v1.1 machinery: {sorted(present)}"


def test_a_retracted_answer_does_not_block_its_own_replacement(schema):
    """The (participant_id, seq) index must be partial: undo tombstones the row, and a full
    index would block the replacement answer forever."""
    seq = [
        r for r in schema["indexes"]
        if r["table_name"] == "session_answer" and r["indexname"] == "session_answer_seq"
    ]
    assert seq, "the double-submit guard still has to exist"
    assert "UNIQUE" in seq[0]["indexdef"].upper()
    assert "retracted_at IS NULL" in seq[0]["indexdef"], (
        "a non-partial unique index here turns undo into a one-way door"
    )


def _primary_key(schema: dict, table: str, table_schema: str = "public") -> list[str]:
    """pg_indexes, because the indexdef carries key ORDER, which decides whether it prefixes lookups."""
    for row in schema["indexes"]:
        if row["table_schema"] == table_schema and row["indexname"] == f"{table}_pkey":
            inner = row["indexdef"][row["indexdef"].index("(") + 1: row["indexdef"].rindex(")")]
            return [c.strip() for c in inner.split(",")]
    raise AssertionError(f"{table_schema}.{table} has no primary key index")


def test_the_two_per_source_tables_carry_the_corpus_key_and_not_the_apps(schema):
    """Per-source tables key on the corpus's (title_id, source, key), not the app's columns
    (§4.1: per-source rows kept); a second source would otherwise 500 on COPY."""
    assert _primary_key(schema, "title_company") == ["title_id", "source", "company", "role"]
    assert _primary_key(schema, "title_video") == ["title_id", "source", "key"]

    video = _columns(schema, "title_video")
    assert "site" in video, "0018 demotes `site` out of the key; it does not drop it"
    assert video["source"]["is_nullable"] == "NO"
    company = _columns(schema, "title_company")
    assert company["source"]["is_nullable"] == "NO", (
        "an unattributed row must have one spelling, not NULL beside '': it is a key component"
    )


def test_rating_source_can_hold_the_terms_each_dataset_ships_with(schema):
    """Nullable and defaultless: a source that shipped no terms reads "not stated", not permissive."""
    columns = _columns(schema, "rating_source")
    for name in ("url", "license", "version", "notes"):
        assert name in columns, f"0018 must add rating_source.{name}"
        assert columns[name]["is_nullable"] == "YES", (
            f"rating_source.{name} unstated must stay distinguishable from {name} being empty"
        )
    # Rule 4's frozen-id CHECK must survive the column additions.
    assert columns["id"]["is_nullable"] == "NO"


def test_a_tier_edit_records_the_board_it_was_made_on(schema):
    """Nullable on purpose: a default would record a K that looks measured; NULL means unknown."""
    columns = _columns(schema, "tier_edit")
    assert "n_levels" in columns, "0022 must add tier_edit.n_levels"
    assert columns["n_levels"]["data_type"] == "smallint"
    assert columns["n_levels"]["is_nullable"] == "YES"


def test_every_observation_table_can_be_searched_by_the_title_it_names(schema):
    """0022's RESTRICT FKs scan the referencing table per deleted row; these keep that off seq scans."""
    wanted = {
        "verdict": "title_id",
        "tier_edit": "title_id",
        "user_title": "title_id",
        "session_ballot": "title_id",
        "session_result": "title_id",
        "session_outcome": "chosen_title_id",
    }
    for table, column in wanted.items():
        leading = [
            r["indexdef"] for r in schema["indexes"]
            if r["table_name"] == table and f"({column})" in r["indexdef"]
        ]
        assert leading, f"the RESTRICT check on {table}.{column} has no index to read"
    for table in ("duel", "session_answer"):
        for column in ("title_a", "title_b"):
            sided = [
                r["indexdef"] for r in schema["indexes"]
                if r["table_name"] == table and f"({column})" in r["indexdef"]
            ]
            assert sided, f"{table} carries a title on both sides and {column} has no index"


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
