"""The webhook's durable pending set, asserted by trying to violate it (§7.2). No UNIQUE on
`resolved_key` and no FK to `title_jellyfin_item`: each checked against its repair.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncpg
import pytest


async def _pending(db, key: str, item_id: str, minutes: int = 10) -> None:
    """`not_before` is spelled out: the schema deliberately gives it no DEFAULT."""
    await db.execute(
        "INSERT INTO jellyfin_intake (item_id, item_type, resolved_key, not_before, raw) "
        f"VALUES ($1, 'Episode', $2, now() + interval '{minutes} minutes', $3)",
        item_id, key, '{"NotificationType": "ItemAdded"}',
    )


async def test_the_state_check_refuses_a_state_the_sweep_cannot_read(db):
    """Intake `state` is `pending|enqueued|skipped`; a
    neighbour table's `done` would be a row no sweep reads."""
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO jellyfin_intake (item_id, resolved_key, not_before, state) "
            "VALUES ('jf-1', 'jf-1', now(), $1)",
            "done",
        )


async def test_a_row_whose_window_is_nothing_is_refused(db):
    """No DEFAULT: the window is a function of `received_at`, fixed rather than sliding (decision 363).
    NOT NULL: the sweep reads `min(not_before)`, and a NULL group is pending forever."""
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.execute(
            "INSERT INTO jellyfin_intake (item_id, resolved_key, not_before) "
            "VALUES ('jf-2', 'jf-2', NULL)"
        )


async def test_a_pending_row_carries_a_key_and_a_refusal_does_not_have_to(db):
    """The sweep groups by `resolved_key`; a `skipped` row records a refusal and needs no key."""
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO jellyfin_intake (item_id, item_type, not_before) "
            "VALUES ('jf-3', 'Movie', now() + interval '10 minutes')"
        )

    await db.execute(
        "INSERT INTO jellyfin_intake (item_type, not_before, state, reason, raw) "
        "VALUES ('Episode', now(), 'skipped', 'episode without series id', $1)",
        '{"ItemType": "Episode"}',
    )
    await _pending(db, "jf-series-1", "jf-episode-1")

    recorded = await db.fetch("SELECT state, resolved_key, reason FROM jellyfin_intake")
    assert len(recorded) == 2
    assert {row["state"] for row in recorded} == {"skipped", "pending"}
    unkeyed = [row for row in recorded if row["resolved_key"] is None]
    assert len(unkeyed) == 1 and unkeyed[0]["reason"] == "episode without series id", (
        "a refusal the handler can already see must be recordable without a key, or the "
        "operator is never shown the webhook that did nothing"
    )


async def test_twelve_events_for_one_series_are_twelve_rows_and_one_key(db):
    """The collapse happens at SWEEP time, so all twelve events must land first."""
    for episode in range(12):
        await _pending(db, "jf-series-7", f"jf-episode-{episode}")

    rows = await db.fetch("SELECT item_id, resolved_key FROM jellyfin_intake")
    assert len(rows) == 12, "a UNIQUE on resolved_key would have made the second episode unrecordable"
    assert len({row["resolved_key"] for row in rows}) == 1, "twelve events, one show"
    assert len({row["item_id"] for row in rows}) == 12, (
        "each event keeps its own item id: the collapse is the sweep's, not the schema's"
    )


async def test_an_event_can_name_a_title_this_install_has_never_held(db):
    """§7.2's subject is the title this install does NOT
    hold yet, so an FK would refuse exactly those events."""
    await _pending(db, "jf-never-seen", "jf-episode-x")

    await db.execute("INSERT INTO title (id, kind, name) VALUES (7700, 'series', 'x')")
    await db.execute(
        "INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ('jf-known', 7700)"
    )
    await _pending(db, "jf-known", "jf-episode-y")

    await db.execute("DELETE FROM title WHERE id = 7700")

    kept = await db.fetchval("SELECT count(*) FROM jellyfin_intake")
    assert kept == 2, (
        "the intake row must not cascade from title: it is the record of what a server told this "
        "app, and it outlives whatever the app decided to do about it"
    )
