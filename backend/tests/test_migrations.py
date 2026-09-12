"""Migrations apply cleanly, and the structure they produce is the one §4.1 requires.

Runs against PGlite — a real Postgres compiled to wasm — so DDL errors are caught on a machine
with no Docker. Skips when `tests/pglite/node_modules` is absent; see tests/pglite/README.md.

One exception, at the foot of the file: 0018's DNA facet backfill is a data rewrite, and
`apply.mjs` reports catalogue shape over an empty database. That one test takes the `db` fixture
and so needs TEST_DATABASE_URL, for the reason `test_schema_contracts.py` gives — a migration
whose UPDATE is never run against rows is a comment with punctuation.
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


def test_no_migration_line_runs_past_the_house_limit():
    """CLAUDE.md's 108 columns, over the one file type ruff never reads.

    The rule is honoured in every `.sql` here without anything enforcing it, which is exactly how
    it stops being honoured: `ruff check .` passes over a 159-character SQL comment, and the one
    migration a reader cannot fit in a 108-column window is then the newest one. Prose, not DDL --
    a statement that has to be long is still legal, because this measures COMMENT lines only.
    The overrun that prompted this was in the block of `0022_model_basis.sql` whose own header
    announces it was corrected in place. [M4.13 cycle 2, M413-C2-DIM7-03]
    """
    over = [
        f"{path.name}:{i}: {len(line)} chars"
        for path in sorted(MIGRATIONS.glob("*.sql"))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.lstrip().startswith("--") and len(line) > 108
    ]
    assert not over, "migration comment lines past 108 columns: " + "; ".join(over)


def test_the_real_migrations_are_discovered_in_order():
    versions = [v for v, _ in migrate.discover(MIGRATIONS)]
    assert versions == [p.stem for p in sorted(MIGRATIONS.glob("*.sql"))]
    assert versions[0].startswith("0001")


async def test_a_missing_migrations_directory_is_refused_before_the_database_is_touched(tmp_path):
    """An absent directory is a packaging error, and `Path.glob` cannot tell you so.

    `glob` on a directory that does not exist yields nothing rather than raising, so both entry
    points used to read "no migrations to apply" out of it: `apply_all` created
    `schema_migration`, returned `[]`, and the app came up with exactly one table and 404s
    everywhere — which is what a Dockerfile that stops copying `backend/migrations` produces,
    reported as a healthy boot. [M4.7 data-08]

    `None` for the connection is the second half of the assertion: if either function reached
    the database before checking, this would fail with an AttributeError instead. The refusal
    has to come first, because the empty schema is the damage.
    """
    absent = tmp_path / "migrations-that-were-never-shipped"
    for call in (migrate.apply_all(None, absent), migrate.pending(None, absent)):
        with pytest.raises(RuntimeError, match="no migrations directory"):
            await call


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
    """§4.2 reserves the bare name `session` for a Tonight session.

    Written at M0 as `"session" not in public`, because the name was reserved and nothing had
    claimed it. M4 claims it, so the reservation is now *kept* rather than merely respected:
    both tables exist, they are distinct, and the auth one is still the one carrying the
    cookie. Inverting this assertion is the same routine `05-milestones.spec.js` runs — the
    M0 form failed the day 0013 landed, and that failure was the reminder to write this.
    """
    public = _names(schema, "public")
    assert "auth_session" in public
    assert "session" in public, "0013 creates §4.2's Tonight `session`, and the name was held for it"

    auth = _columns(schema, "auth_session")
    tonight = _columns(schema, "session")
    # Written as `"token_hash" in auth or "id" in auth`, and `auth_session` has never had a
    # `token_hash` — `0002_users.sql` gives it an opaque text `id` that is signed into the
    # cookie, and a grep for the name across `backend/` found only that assertion. So the
    # disjunction rested entirely on the table's primary key, which is to say on nothing: every
    # table in this schema has an id. What carries the cookie is asserted instead — the four
    # columns a session needs to *be* one, and the id's type, because a bigserial id would mean
    # the cookie carries a guessable number. [M4.7 tq2-migrations-token-hash]
    assert {"id", "user_id", "expires_at", "auth_method"} <= set(auth), sorted(auth)
    assert auth["id"]["data_type"] == "text", (
        "§3.2's session id is opaque and signed into the cookie, so it is not a serial"
    )
    # The distinguishing column, not merely a different row count: a Tonight session has a host
    # and a room; an auth session has neither and must never acquire them.
    assert "host_user_id" in tonight and "room_code" in tonight
    assert "host_user_id" not in auth and "room_code" not in auth


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


# --- §4.2's Tonight session block (0013) ------------------------------------------------


def test_the_tonight_session_tables_match_the_spec_block(schema):
    """§4.2 names five of these and 54g adds the sixth; the names are part of the contract.

    `session_ballot` is the one §4.2 does not carry — 54g adds it because approval share is
    §13's headline metric for the whole feature and the ballot is the only place it exists.
    """
    public = _names(schema, "public")
    for table in (
        "session", "session_participant", "session_answer",
        "session_ballot", "session_result", "session_outcome",
    ):
        assert table in public, f"§4.2/54g names `{table}` and it is not in the schema"


def test_a_guest_seat_is_addressable_without_a_user_id(schema):
    """§4.2: "session_participant(session_id, user_id NULL, …) — NULL = guest slot on the
    host phone".

    Two guests on one phone is the designed case (§6.2 step 2's hand-the-phone), so the seat
    cannot be keyed on (session_id, user_id): that key seats one guest and silently drops the
    second. The seat therefore carries its own id, and the "one seat per member" rule lives in
    a *partial* unique index that NULLs do not participate in.
    """
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
    """§4.2 + 54g: seq, the two titles, the answer, latency_ms and the `selection`
    discriminator §13's guard needs. `retracted_at` carries §6's "undo everywhere" as a
    tombstone rather than a delete, the way `rate_observation.undone_at` does."""
    columns = _columns(schema, "session_answer")
    for name in ("session_id", "participant_id", "seq", "title_a", "title_b",
                 "answer", "latency_ms", "selection", "retracted_at"):
        assert name in columns, f"§4.2/54g names session_answer.{name}"


def test_the_held_out_session_answers_are_addressable(schema):
    """§13 stream (a) via 54b: the hold-out arm is the only data admissible for evaluating the
    round, so it has to be selectable without scanning every answer — the same partial index
    `duel_holdout` gives the tier queue (0005)."""
    holdout = [
        r for r in schema["indexes"]
        if r["table_name"] == "session_answer" and "uniform_holdout" in r["indexdef"]
    ]
    assert holdout, "session_answer needs a partial index on the uniform_holdout stream"


def test_the_result_slate_and_its_outcome_are_two_tables(schema):
    """§4.2 gives the round both: `session_result` is per candidate in the slate,
    `session_outcome` is the one chosen title and the approval share §13 evaluates on."""
    result = _columns(schema, "session_result")
    for name in ("session_id", "title_id", "rank", "group_score", "per_user_match", "conflict"):
        assert name in result, f"§4.2 names session_result.{name}"
    assert result["conflict"]["is_nullable"] == "YES", (
        "§6.2 step 5: below D = 0.20 the split is decided silently, so no conflict is stored"
    )
    # Not §4.2's column but 54d's: "the third finalist slot is reserved for the highest-scoring
    # title on the opposite pole of the contested axis, **labelled as such**". Nothing carried the
    # label, so migration 0021 added it as a boolean orthogonal to the slot rather than as a fourth
    # `slot` value — the reserved title is still a finalist and every filter on the slot keeps
    # counting it. NOT NULL because every row already stored is a slate that had no reservation.
    # [M4.12 finding 24; decision 220]
    assert "reserved" in result, "54d's reserved slot has to be labelled somewhere"
    assert result["reserved"]["is_nullable"] == "NO", (
        "a card either is the far side of the split or is not; there is no unknown"
    )
    outcome = _columns(schema, "session_outcome")
    for name in ("session_id", "chosen_title_id", "approval_share", "participants"):
        assert name in outcome, f"§4.2 names session_outcome.{name}"


def test_no_v11_posterior_columns_survive_anywhere_in_the_schema(schema):
    """§4.2: "v1.1 §6's mu/sigma/tolerance/phase columns do NOT survive (8-axis posterior
    machinery deleted per §0); fairness_ledger omitted".

    The static guard reads the migration text; this reads the *applied* schema, so a column
    added by a later ALTER cannot slip past the regex. Both halves are cheap and neither
    subsumes the other.
    """
    public = _names(schema, "public")
    assert "fairness_ledger" not in public
    banned = {"mu", "sigma", "tolerance", "phase"}
    for table in ("session", "session_participant", "session_answer",
                  "session_ballot", "session_result", "session_outcome"):
        present = set(_columns(schema, table)) & banned
        assert not present, f"{table} carries deleted v1.1 machinery: {sorted(present)}"


def test_a_retracted_answer_does_not_block_its_own_replacement(schema):
    """§6 preamble's "undo everywhere", reaching `session_answer`.

    `play.retract` tombstones rather than deletes (§14 risk 6: log every vote), and the next
    answer used to arrive at the seq the retraction freed. A NON-partial unique index on
    `(participant_id, seq)` makes that insert collide with the tombstone forever — one tap on
    Undo ends that participant's round, and because the reveal waits for every seat (54e), the
    household's evening with it. 0014 scopes it to the live rows.

    M4.12 mints the replacement's seq from every row there has ever been instead (finding 11), so
    the collision this index forgives is no longer reachable from `record_answer` — and the index
    stays exactly as 0014 wrote it, because it is the backstop under a repair that lives in
    application code, and 0014 is applied and sha256-checksummed either way. A partial index is
    strictly the more permissive of the two, so nothing about the assertion below weakens.
    """
    seq = [
        r for r in schema["indexes"]
        if r["table_name"] == "session_answer" and r["indexname"] == "session_answer_seq"
    ]
    assert seq, "the double-submit guard still has to exist"
    assert "UNIQUE" in seq[0]["indexdef"].upper()
    assert "retracted_at IS NULL" in seq[0]["indexdef"], (
        "a non-partial unique index here turns undo into a one-way door"
    )


# --- M4.9's read layer (0018) -----------------------------------------------------------


def _primary_key(schema: dict, table: str, table_schema: str = "public") -> list[str]:
    """The primary key's columns, in key order, read off the index PGlite reports.

    `information_schema.table_constraints` would name the constraint; `pg_indexes` is what
    `apply.mjs` already collects, and the indexdef carries the ORDER — which is the half that
    matters here, because (title_id, source, key) and (title_id, key, source) are the same set
    and only one of them prefixes the lookups the read layer makes.
    """
    for row in schema["indexes"]:
        if row["table_schema"] == table_schema and row["indexname"] == f"{table}_pkey":
            inner = row["indexdef"][row["indexdef"].index("(") + 1: row["indexdef"].rindex(")")]
            return [c.strip() for c in inner.split(",")]
    raise AssertionError(f"{table_schema}.{table} has no primary key index")


def test_the_two_per_source_tables_carry_the_corpus_key_and_not_the_apps(schema):
    """§4.1 opens "tables mirror the corpus export" and says of `title_meta`: "multi-source,
    per-source rows kept — one block = one droppable source". `0015_seed.sql` section 9 applied
    that to `title_language`, `title_country` and `platform_rating`; 0018 finishes the sweep.

    `title_company` is the measured half, in 0015's own two-column form: 47,607 shipped rows,
    8,594 duplicate groups under the app's `(title_id, company, role)` key, 11,654 rows
    discarded. A group count is not a row count, and this file's whole subject is per-source row
    multiplicity (decision 195). That is why `importer/load.py` names the whole table in
    `SKIPPED_TABLES`; decision 193 re-keys it and loads it on §4.1's spine-list terms.
    `placement/features.py` does count company rows into the thin-title meta block, but
    `n_companies_log` is a column of no contract this app has loaded, so that count is produced
    and discarded rather than fed to the tower (decision 194).

    `title_video` is the unmeasured half, and is asserted for the reason 0015's three were: the
    shipped bundle has one video source and zero collisions, so nothing is wrong until a second
    source reports a trailer the first already lists, at which point a green `validate()` becomes
    a 500 on COPY. The key is the corpus's `(title_id, source, key)` and NOT the app's `site`
    plus `source` — `site` stays an ordinary column (it is what §6.0's card would link out to)
    and carries no key role, because the corpus's own primary key already guarantees that no two
    rows share (title_id, source, key).
    """
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
    """§4.1 rule 4's eleven frozen ids are the only place the corpus recorded per-dataset terms:
    the Netflix Prize's research-use-only clause, the CC BY attributions naming their authors,
    the version each dataset's ids were frozen against. `0003_content.sql` kept `id`, `name` and
    `scale` alone, so no surface could print an attribution those licences require and §6.6's
    Data card could not answer the question it exists to answer.

    All four nullable and defaultless on purpose: a source that shipped no terms must read as
    "not stated" rather than as permissively licensed, and a default would erase the difference.
    """
    columns = _columns(schema, "rating_source")
    for name in ("url", "license", "version", "notes"):
        assert name in columns, f"0018 must add rating_source.{name}"
        assert columns[name]["is_nullable"] == "YES", (
            f"rating_source.{name} unstated must stay distinguishable from {name} being empty"
        )
    # Rule 4's frozen-id CHECK is the one thing here that must NOT have moved; adding columns to
    # a table is exactly the edit that quietly rewrites it.
    # `test_the_database_refuses_a_renumbered_rating_source` proves it still bites.
    assert columns["id"]["is_nullable"] == "NO"


# --- M4.13's structural sweep (0022) -----------------------------------------------------


def test_a_tier_edit_records_the_board_it_was_made_on(schema):
    """Decision 11 keeps `tier_edit` rows across a tier-set change, so the K has to be in the row.

    `n_levels` arrives by ALTER, which `relations` cannot see -- the same blind spot
    `test_the_ledger_output_columns_the_rank_board_reads_exist` exists for. Nullable on purpose: a
    NOT NULL with a default would let a writer that forgets the value record a 7 that looks like a
    measurement, where a NULL is a row whose board is genuinely unknown. The backfill's arithmetic
    is asserted where it can be, against rows that were already there:
    `test_schema_contracts.py::test_the_tier_edit_k_column_is_backfilled_from_the_users_own_tier_set`.
    """
    columns = _columns(schema, "tier_edit")
    assert "n_levels" in columns, "0022 must add tier_edit.n_levels"
    assert columns["n_levels"]["data_type"] == "smallint"
    assert columns["n_levels"]["is_nullable"] == "YES"


def test_every_observation_table_can_be_searched_by_the_title_it_names(schema):
    """0022 moved ten foreign keys from CASCADE to RESTRICT, and a RESTRICT check reads the
    referencing table once per deleted row. Not one of those ten columns led an index -- every
    index over them starts with a user, session or participant id -- so without these the refusal
    §10 now gets would be bought with ten sequential scans.

    Asserted here rather than inferred from the migration's text because a `CREATE INDEX` that
    named a column wrongly would still apply.
    """
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
    """0018 section 1's two UPDATEs, read out of the shipped file rather than restated here.

    A restatement would test a paraphrase; the assertion below is about the SQL that actually
    runs on every existing install. Section 2's `UPDATE dna_tag SET provider = ''` must not be
    swept in with them, so the filter is the `split_part` only section 1 uses.

    Comments come off before the split rather than after: this file's headers quote spec
    sentences, several of which carry a semicolon, so splitting the raw text first cuts a
    statement's own chunk open at a clause boundary in prose.
    """
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
    """§4.3: a `dna_vocab/v1` id is `facet.term`, and decision 162 seeds content ONCE.

    That second clause is why this is a migration and not only a loader fix. The install the
    M4.5 exit criterion already seeded has no re-import, so `dna_tag.facet` stays `mood_tone`
    for ever unless something rewrites it in place: 29,188 `dna_tag` rows and 206,151
    `dna_projected` rows on the shipped bundle carry the label the extraction pass ran under
    (`character_dynamics`) where `dna_facet`, `dna_term`, §6.4's axes and §6.8's eleven-colour
    palette all key on the vocabulary facet id (`characters`).

    Idempotence is asserted by running the shipped statements a second time and reading the
    command tag, because a backfill nobody can re-run is a backfill nobody can verify: an
    operator unsure whether 0018 reached their install has to be able to execute section 1 by
    hand and read `UPDATE 0`, rather than wonder what a second pass just touched.

    The undotted row is the `term LIKE '%.%'` guard, asserted rather than trusted — and what the
    guard prevents is not a NULL. `split_part(term, '.', 1)` returns the WHOLE string when there
    is no delimiter (it is field 2 that answers `''`), so the unguarded form would overwrite
    every row of a future undotted vocabulary with the term id itself: a facet that joins no
    `dna_facet` row, renders `var(--ink-4)` on every chip, and reads as plausible. The last
    block below runs the unguarded form in a transaction it rolls back and reads that value
    out, so the reason recorded here is a measurement rather than a belief about `split_part`.
    An undotted vocabulary is also why 0018 adds no CHECK pinning
    `facet = split_part(term, '.', 1)`. [M4.9 review cycle 1: M49-FACET-02]
    """
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

    # What the guard actually prevents, run rather than asserted from memory. Rolled back, so
    # the rows above stay as the assertions left them.
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
