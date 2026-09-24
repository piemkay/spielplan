"""What `0029_flywheel.sql` refuses, and what it keeps from the install it upgrades. Spec v2.1 §8,
§8.4 and §6.6 Data as amended by v2.1.3; decisions 326, 328, 329, 342, 440, 441, 443 and 444.

`test_migrations.py` checks that every migration applies; this file checks what 0029's shape
refuses and admits, for the reason `test_acquire_schema.py` states at its own head: a CHECK
constraint that is never tried is a comment with punctuation.

Every refusal is asserted by the NAME of the constraint that refuses it, `test_llm_schema.py`'s
rule: a write refused by the wrong constraint is a constraint missing, and three of the ones here
share a table and a column with a neighbour that would refuse the same row for a different reason
(the kind CHECK and the thin-facet CHECK both read `kind`; the unique index and the running CHECK
both read `status`).

Two of the file's claims are about rows that were already there, which no other layer of this
suite can see: every layer migrates an EMPTY database (`test_upgrade_drill.py`'s opening
paragraph), where a widened CHECK and an `ADD COLUMN NOT NULL DEFAULT` cannot be wrong. The staged
apply at the foot of the file is what runs 0029 over a board row and an authored axis the install
already holds, which is the only state a household's install is ever in.

Rows are written by direct INSERT rather than through the flywheel's writers: the columns under
test are constraints, and the writers that honour them land in later stages of this milestone.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from spielplan.db import migrate
from tests.test_backup import _drop, _recreate, _sibling
from tests.test_upgrade_drill import _complete, _stage

MIGRATION = "0029_flywheel"


async def _title(db, title_id: int) -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, 'movie', 'x') ON CONFLICT DO NOTHING",
        title_id,
    )
    return title_id


async def _batch(db, **overrides) -> int:
    """One `flywheel_batch` row as decision 443's launch writes it, any column overridden."""
    row = {
        "providers": ["gemini"],
        "passes": 1,
        "est_titles": 2,
        "est_cost_usd": Decimal("0.047250"),
        "reserved_usd": Decimal("0.094500"),
        **overrides,
    }
    columns = ", ".join(row)
    params = ", ".join(f"${n}" for n in range(1, len(row) + 1))
    return await db.fetchval(
        f"INSERT INTO flywheel_batch ({columns}, launched_at) VALUES ({params}, now()) RETURNING id",
        *row.values(),
    )


async def _item(db, kind: str = "thin_facet", **overrides) -> int:
    """One `flywheel_item` row with every NOT NULL column filled.

    `detail` is a literal in the statement rather than a parameter: the `db` fixture's jsonb codec
    is `json.dumps`, so a parameter would have to be a dict here and a string in `db/pool.py`'s
    connections, and neither spelling is what is under test.
    """
    row = {"kind": kind, "reason": "no extracted term names its pacing", **overrides}
    columns = ", ".join(row)
    params = ", ".join(f"${n}" for n in range(1, len(row) + 1))
    return await db.fetchval(
        f"INSERT INTO flywheel_item (detail, {columns}) VALUES ('{{}}'::jsonb, {params}) "
        "RETURNING id",
        *row.values(),
    )


async def _refused_by(write, error: type[Exception], constraint: str) -> None:
    with pytest.raises(error) as refused:
        await write
    assert refused.value.constraint_name == constraint, (
        f"refused by {refused.value.constraint_name}, not {constraint}: the constraint this test "
        "is about did not do the refusing, which is the same as it missing"
    )


# --- decision 328: §8.4's third feed is struck ----------------------------------------------


async def test_the_struck_feed_is_a_kind_the_queue_refuses(db):
    """Decision 328: "titles whose 'unnamed taste' residual share is high" is defined nowhere this
    repository can read, so the feed is struck from §8.4 and its kind from the CHECK 0004 froze.

    Striking it from the spec and leaving it in the CHECK would keep a value any later writer
    could fill with a threshold nobody agreed - the invented definition the decision refuses to
    make. So the struck kind is refused by the CHECK's own name, and the three §8.4 still names are
    admitted beside it: the two whose producer is M6's (empty predicates, the uncovered frontier)
    name no title, and the thin-facet kind decision 329 defines names one.
    """
    title = await _title(db, 701)
    await _refused_by(
        _item(db, "unnamed_residual"), asyncpg.CheckViolationError, "flywheel_item_kind_check"
    )
    await _item(db, "empty_predicate")
    await _item(db, "uncovered_frontier")
    await _item(db, "thin_facet", title_id=title)
    kinds = await db.fetch("SELECT kind FROM flywheel_item ORDER BY kind")
    assert [row["kind"] for row in kinds] == ["empty_predicate", "thin_facet", "uncovered_frontier"]


# --- decisions 329 and 440: the thin-facet row names its title, once --------------------------


async def test_a_thin_facet_row_must_name_the_title_it_is_about(db):
    """Decision 329: a title is thin_facet when a facet `dna_facet` declares has no extracted term
    for it, so the row IS a statement about one title - and decision 443's launch acts on it by
    making that title due at the pack stage. A thin-facet row with no title is a queue entry whose
    only control, Launch, has nothing to launch.

    The other half is what keeps the column nullable: the two kinds M6 produces describe a query
    and a frontier, and a CHECK written as "every row names a title" would refuse the first row
    §6.4's compositional search writes.
    """
    await _refused_by(
        _item(db, "thin_facet"), asyncpg.CheckViolationError,
        "flywheel_item_thin_facet_names_a_title",
    )
    await _item(db, "empty_predicate")
    assert await db.fetchval("SELECT count(*) FROM flywheel_item WHERE title_id IS NULL") == 1


async def test_a_title_holds_one_open_thin_facet_row_however_often_it_is_observed(db):
    """Decision 440: the driver observes a title each time its walk finishes stage 8, and a title
    walks more than once. The observer closes the running rows and refreshes the open one, so the
    queue holds one row per title - and the partial unique index is what keeps that true under two
    walks at once, rather than true of the code that is supposed to keep it.

    "Open" is every status that is not closed. A second QUEUED row beside a RUNNING one is the
    refusal that matters most: it would let a second launch select the title while the first
    batch's walk is still under way, and bill it twice. Closed rows are history and accumulate
    freely - `done` and `dismissed` beside a queued row are admitted - and the index binds per
    title, so a second title's open row is admitted too.
    """
    title, other = await _title(db, 702), await _title(db, 703)
    await _item(db, title_id=title)
    await _refused_by(
        _item(db, title_id=title), asyncpg.UniqueViolationError,
        "flywheel_item_one_open_thin_facet",
    )
    batch = await _batch(db)
    await _refused_by(
        _item(db, title_id=title, status="running", batch_id=batch), asyncpg.UniqueViolationError,
        "flywheel_item_one_open_thin_facet",
    )
    await _item(db, title_id=title, status="done")
    await _item(db, title_id=title, status="dismissed")
    await _item(db, title_id=other)
    open_rows = await db.fetch(
        "SELECT title_id, count(*) AS n FROM flywheel_item "
        " WHERE status IN ('queued', 'approved', 'running') GROUP BY title_id ORDER BY title_id"
    )
    assert [(row["title_id"], row["n"]) for row in open_rows] == [(title, 1), (other, 1)]


async def test_the_queue_is_indexed_for_its_read_and_for_its_one_open_row(db):
    """The two reads the flywheel makes, asserted because an index is the first thing a later
    migration drops when it looks redundant.

    The queue's read - by status, newest first - is served by 0004's `flywheel_status`, which 0029
    deliberately does not duplicate: a second copy would be written on every enqueue and read by
    nothing. So what is pinned here is that the index 0029 leans on is still there. The observer's
    read - this title's open thin-facet row - is served by decision 440's partial unique index,
    and its predicate is pinned with it, because an index that dropped `running` from the set
    would still build, still be unique, and stop refusing the double launch the test above names.
    """
    rows = await db.fetch(
        "SELECT indexname, indexdef FROM pg_indexes "
        " WHERE schemaname = 'public' AND tablename = 'flywheel_item'"
    )
    defs = {row["indexname"]: row["indexdef"] for row in rows}
    assert "(status, created_at DESC)" in defs.get("flywheel_status", ""), (
        "0004's queue index is gone or changed, and 0029 builds none of its own because it relied "
        "on this one; the admin queue's newest-first read now scans the table"
    )
    unique = defs.get("flywheel_item_one_open_thin_facet", "")
    assert unique.startswith("CREATE UNIQUE INDEX") and "(title_id)" in unique, unique
    for status in ("queued", "approved", "running"):
        assert f"'{status}'" in unique, f"an open {status} row escapes decision 440's index"
    assert "'thin_facet'" in unique, "the index binds kinds whose rows name no title"


async def test_a_title_that_goes_takes_its_queue_rows_with_it(db):
    """`acquisition_job.title_id`'s reason, one table over: a queue row about a title the install
    no longer holds describes nothing, and a launch that selected it would make a missing title
    due. ON DELETE CASCADE, asserted over an open row and a closed one, because the closed row is
    the one a SET NULL would have left behind as a thin-facet row naming no title - which the
    CHECK above would then refuse, failing the title's own delete.
    """
    title = await _title(db, 704)
    await _item(db, title_id=title)
    await _item(db, title_id=title, status="done")
    await db.execute("DELETE FROM title WHERE id = $1", title)
    assert await db.fetchval("SELECT count(*) FROM flywheel_item") == 0


# --- decision 443: the batch, and the rows it runs ---------------------------------------------


async def test_a_row_runs_only_inside_a_batch_that_exists(db):
    """Plan C5: "A launched batch targets exactly the selected rows and no others". Decision 443's
    launch sets `status = 'running'` and `batch_id` in one UPDATE, and these two refusals make
    that readable off the table afterwards rather than only asserted by the launch at the time: a
    row cannot name a batch nobody launched, and a row cannot be running outside a batch.

    A queued row names no batch and is admitted without one, which is every row the observer
    writes (decision 440).
    """
    title = await _title(db, 705)
    await _refused_by(
        _item(db, title_id=title, batch_id=999_999), asyncpg.ForeignKeyViolationError,
        "flywheel_item_batch_id_fkey",
    )
    await _refused_by(
        _item(db, title_id=title, status="running"), asyncpg.CheckViolationError,
        "flywheel_item_running_names_its_batch",
    )
    batch = await _batch(db)
    await _item(db, title_id=title, status="running", batch_id=batch)
    await _item(db, "empty_predicate")
    assert await db.fetchval(
        "SELECT count(*) FROM flywheel_item WHERE batch_id = $1", batch
    ) == 1


async def test_a_batch_is_a_plan_and_a_bill_and_refuses_what_no_launch_writes(db):
    """Decisions 441, 442 and 443: the batch row records what the admin chose - providers and
    passes, the plan the gate, stage 6 and the retry pre-check all read - and what it was priced
    at, both figures of it.

    Money is numeric at `llm_call.usd`'s six places and never a float, for decision 325's reason:
    the cap is compared against sums of these, and a float that rounds is a cap that drifts. So a
    six-place figure is asserted to come back as the same Decimal, not a nearby one.

    The refusals are the plans no stage can run - no provider, a provider this build does not call
    (0028's set), no pass - a negative title count, and a reservation below the total it doubles
    (decision 441: reserved = total x the two attempts decision 325 budgets). `created_at` is the
    row's own stamp and `launched_at` carries no default, so a row that did not come through the
    launch says so by a NULL rather than by a time nobody chose.
    """
    batch = await _batch(db, est_cost_usd=Decimal("0.123457"), reserved_usd=Decimal("0.246914"))
    row = await db.fetchrow(
        "SELECT est_cost_usd, reserved_usd, created_at, launched_at FROM flywheel_batch "
        " WHERE id = $1",
        batch,
    )
    assert (row["est_cost_usd"], row["reserved_usd"]) == (Decimal("0.123457"), Decimal("0.246914"))
    assert row["created_at"] is not None and row["launched_at"] is not None

    for column, bad, constraint in (
        ("providers", [], "flywheel_batch_providers_check"),
        ("providers", ["gemini", "mistral"], "flywheel_batch_providers_check"),
        ("passes", 0, "flywheel_batch_passes_check"),
        ("est_titles", -1, "flywheel_batch_est_titles_check"),
        ("reserved_usd", Decimal("0.010000"), "flywheel_batch_reserves_at_least_its_total"),
    ):
        await _refused_by(
            _batch(db, **{column: bad}), asyncpg.CheckViolationError, constraint
        )

    bare = await db.fetchrow(
        "INSERT INTO flywheel_batch (providers, passes, est_titles) "
        "VALUES (ARRAY['anthropic', 'openai'], 2, 0) RETURNING created_at, launched_at"
    )
    assert bare["created_at"] is not None and bare["launched_at"] is None


# --- decision 444: the board's fourth outcome --------------------------------------------------


async def test_the_board_admits_abandoned_and_still_refuses_a_status_nobody_reads(db):
    """Decision 444: an admin's abandon is neither of decision 336's two outcomes. `parked` is
    "waiting on something that may change" and would promise a resume nobody asked for; `failed`
    is "this stage raised and will raise again" and would tell the next reader a healthy title is
    broken - `placement/reconcile.py`'s thin-but-placed titles, abandoned from the board, are the
    case that makes it concrete.

    The widening admits every status the old CHECK admitted, and the CHECK is still a CHECK: a
    status the board has no rendering for is refused by the same constraint name, which is the
    name 0005's column CHECK carried and 0029's DROP/ADD pair keeps.
    """
    statuses = ("queued", "running", "parked", "ready", "failed", "abandoned")
    for offset, status in enumerate(statuses):
        title = await _title(db, 710 + offset)
        await db.execute(
            "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 3, $2)",
            title, status,
        )
    title = await _title(db, 720)
    await _refused_by(
        db.execute(
            "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 3, 'cancelled')",
            title,
        ),
        asyncpg.CheckViolationError,
        "acquisition_job_status_check",
    )
    stored = await db.fetch("SELECT status FROM acquisition_job ORDER BY title_id")
    assert tuple(row["status"] for row in stored) == statuses


# --- decisions 326, 342 and 423: the third curated ledger's author --------------------------


async def _axis_vocabulary(conn, facet: str = "pacing") -> None:
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 2)"
    )
    await conn.execute(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ('v1', $1, 1)", facet
    )


async def test_an_axis_is_the_bundles_unless_the_household_wrote_it(db):
    """Decision 342 ships the third ledger editor, which writes `dna_axis` for the active
    vocabulary as the household's, and decision 423's rule - the household's curated row takes
    effect - needs the importer's axis loader to tell that row from the bundle's. That is decision
    326's `origin`, in 0026's vocabulary and with 0026's DEFAULT.

    'acquired' is refused by name for 0026's reason: `title.origin` carries the same column name
    with 'acquired' as its second literal, so it is the plausible wrong word, and an axis stamped
    with it is one the loader's bundle-scoped replace passes over for ever and no editor claims.
    """
    await _axis_vocabulary(db)
    await db.execute(
        "INSERT INTO dna_axis (version, facet, left_pole, right_pole) "
        "VALUES ('v1', 'pacing', 'slow', 'fast')"
    )
    assert await db.fetchval("SELECT origin FROM dna_axis WHERE facet = 'pacing'") == "bundle"
    await db.execute("UPDATE dna_axis SET origin = 'household' WHERE facet = 'pacing'")
    await _refused_by(
        db.execute("UPDATE dna_axis SET origin = 'acquired' WHERE facet = 'pacing'"),
        asyncpg.CheckViolationError,
        "dna_axis_origin_check",
    )
    assert await db.fetchval("SELECT origin FROM dna_axis WHERE facet = 'pacing'") == "household"


# --- the upgrade: 0029 over rows the install already holds ------------------------------------


def _last_before_the_migration() -> str:
    """The migration this build applies immediately before 0029, found rather than spelled, for
    `test_derive_schema.py`'s reason: a sibling lane's migration may land in this directory at the
    merge, and a hardcoded stem would then stage an install one migration short of the one under
    test while still passing. The cut is lexicographic because `migrate.discover` sorts the
    zero-padded stems, so it is the cut `apply_all` makes."""
    earlier = [version for version, _ in migrate.discover() if version < "0029"]
    assert earlier, "no migration sorts before 0029; this is not reading the tree's directory"
    return earlier[-1]


@pytest.fixture
async def before_the_migration(pg_url, tmp_path):
    """The install an operator is upgrading: the release before 0029.

    A database of its own and not the `db` fixture's, for `test_upgrade_drill.py`'s reason - the
    point is a schema that is deliberately not this build's, and `db` exists to guarantee the
    opposite.
    """
    admin, name, url = _sibling(pg_url, "_pre0029")
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


async def test_an_install_upgraded_to_0029_keeps_its_board_and_reads_its_axis_as_the_bundles(
    before_the_migration,
):
    """Both ALTERs over rows that were already there. Nothing else in this suite runs them so.

    `dna_axis.origin` is `ADD COLUMN NOT NULL DEFAULT`, and on an install that holds an axis the
    DEFAULT is the backfill: every stored axis came from a bundle, so 'bundle' is the truth for it
    and not a convenience, and a column added without a DEFAULT would fail the ALTER at boot inside
    `db/migrate.py` with 0029 checksummed and nothing an operator could edit. The axis's weight row
    is asserted beside it, because the column is on the axis and not on its terms (0029 section 6),
    and an ALTER that rebuilt the axis would cascade them away.

    The board's CHECK is a DROP/ADD pair, and ADD validates every stored row: a household whose
    board holds each of the five statuses 0005 admitted must boot through it unchanged.

    The control is the absence checked first, `test_derive_schema.py`'s: assert `origin = 'bundle'`
    on a database that already had the column and the test passes while proving nothing about the
    upgrade.
    """
    conn, directory = before_the_migration
    present = await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        " WHERE table_schema = 'public' AND table_name = 'dna_axis' AND column_name = 'origin'"
    )
    assert present == 0, (
        f"the staged install already has dna_axis.origin, so this upgrade proves nothing: "
        f"{MIGRATION} is being staged as part of the 'before' half"
    )

    await _axis_vocabulary(conn)
    await conn.execute(
        "INSERT INTO dna_axis (version, facet, left_pole, right_pole) "
        "VALUES ('v1', 'pacing', 'slow', 'fast')"
    )
    await conn.execute(
        "INSERT INTO dna_axis_weight (version, facet, term, weight) "
        "VALUES ('v1', 'pacing', 'languid', -0.8)"
    )
    before = ("queued", "running", "parked", "ready", "failed")
    for offset, status in enumerate(before):
        await _title(conn, 730 + offset)
        await conn.execute(
            "INSERT INTO acquisition_job (title_id, stage, status, reason) VALUES ($1, 4, $2, $3)",
            730 + offset, status, f"held at {status}",
        )

    pending = _complete(directory)
    assert MIGRATION in pending, f"{MIGRATION} is not among the migrations this upgrade applies"
    applied = await migrate.apply_all(conn, directory)
    assert MIGRATION in applied

    axis = await conn.fetchrow("SELECT origin, left_pole FROM dna_axis WHERE facet = 'pacing'")
    assert (axis["origin"], axis["left_pole"]) == ("bundle", "slow"), (
        "an axis that predates the provenance column must read as the bundle's: a NULL or an empty "
        "string there is an axis the loader would neither replace nor leave as the household's"
    )
    assert await conn.fetchval("SELECT count(*) FROM dna_axis_weight WHERE facet = 'pacing'") == 1
    board = await conn.fetch("SELECT status, reason FROM acquisition_job ORDER BY title_id")
    assert [(row["status"], row["reason"]) for row in board] == [
        (status, f"held at {status}") for status in before
    ]
