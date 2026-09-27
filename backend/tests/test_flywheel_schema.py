"""What `0029_flywheel.sql` refuses (§8.4), asserted by constraint NAME. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest


async def _title(db, title_id: int) -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, 'movie', 'x') ON CONFLICT DO NOTHING",
        title_id,
    )
    return title_id


async def _batch(db, **overrides) -> int:
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
    """`detail` is a literal: the `db` fixture's jsonb codec differs from `db/pool.py`'s connections."""
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


async def test_the_struck_feed_is_a_kind_the_queue_refuses(db):
    """Decision 328 strikes the "unnamed taste" feed, so its kind must not stay fillable in the CHECK."""
    title = await _title(db, 701)
    await _refused_by(
        _item(db, "unnamed_residual"), asyncpg.CheckViolationError, "flywheel_item_kind_check"
    )
    await _item(db, "empty_predicate")
    await _item(db, "uncovered_frontier")
    await _item(db, "thin_facet", title_id=title)
    kinds = await db.fetch("SELECT kind FROM flywheel_item ORDER BY kind")
    assert [row["kind"] for row in kinds] == ["empty_predicate", "thin_facet", "uncovered_frontier"]


async def test_a_thin_facet_row_must_name_the_title_it_is_about(db):
    """The column stays nullable: M6's two kinds describe a query and a frontier, not a title."""
    await _refused_by(
        _item(db, "thin_facet"), asyncpg.CheckViolationError,
        "flywheel_item_thin_facet_names_a_title",
    )
    await _item(db, "empty_predicate")
    assert await db.fetchval("SELECT count(*) FROM flywheel_item WHERE title_id IS NULL") == 1


async def test_a_title_holds_one_open_thin_facet_row_however_often_it_is_observed(db):
    """A second QUEUED row beside a RUNNING one would let a second launch bill the title twice."""
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


async def test_a_title_that_goes_takes_its_queue_rows_with_it(db):
    """A SET NULL would leave a thin-facet row naming no title, which the CHECK above refuses."""
    title = await _title(db, 704)
    await _item(db, title_id=title)
    await _item(db, title_id=title, status="done")
    await db.execute("DELETE FROM title WHERE id = $1", title)
    assert await db.fetchval("SELECT count(*) FROM flywheel_item") == 0


async def test_a_row_runs_only_inside_a_batch_that_exists(db):
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
    """Money is numeric at six places, never a float: the cap is compared against sums of these."""
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


async def test_the_board_admits_abandoned_and_still_refuses_a_status_nobody_reads(db):
    """Decision 444: abandon is neither parked nor failed;
    the widened CHECK still refuses unknown statuses."""
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


async def _axis_vocabulary(conn, facet: str = "pacing") -> None:
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 2)"
    )
    await conn.execute(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ('v1', $1, 1)", facet
    )


async def test_an_axis_is_the_bundles_unless_the_household_wrote_it(db):
    """'acquired' is the plausible wrong word from `title.origin`,
    and the loader would pass it over for ever."""
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
