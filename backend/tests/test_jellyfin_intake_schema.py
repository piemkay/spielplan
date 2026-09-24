"""The webhook's durable pending set, asserted by trying to violate it. Spec v2.1 §7.2.

`test_migrations.py` checks that every migration applies and what shape it leaves behind; this
file checks what that shape *refuses*, for the reason `test_schema_contracts.py` states at its
own head: a CHECK constraint that is never tried is a comment with punctuation.

`0025_jellyfin_intake.sql` rests on three claims that cannot be read off the DDL, and each is the
kind a later agent repairs on sight. The table carries **no UNIQUE on `resolved_key`**, because
many events for one key is the entire subject of a debounce (decision 363) and a UNIQUE would
make the second episode of a season unrecordable. It carries **no foreign key to
`title_jellyfin_item`**, whose `jellyfin_id text PRIMARY KEY` (`0020_jellyfin_items.sql:28`) is
type-compatible and therefore the repair a reader will actually reach for -- and that is
`acquisition_task`'s reason one table over (decision 322), one step earlier: §7.2's whole subject
is the title this install does NOT hold yet. And its one index is **partial on
`state = 'pending'`** rather than the `(state, ...)` shape `acquisition_task_ready` uses next
door, which is the other half of the same repair: an index that looks wrong beside its neighbour
is one a later migration drops as redundant.

Each is asserted in the direction that catches its repair: twelve rows share one key, an event
outlives the title it was about, and the sweep's index is still partial.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncpg
import pytest


async def _index_defs(db, table: str) -> str:
    rows = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = $1 AND schemaname = 'public'", table
    )
    return " ".join(row["indexdef"] for row in rows)


async def _pending(db, key: str, item_id: str, minutes: int = 10) -> None:
    """One row in the shape the handler writes it: resolved at insert, ripe ten minutes on.

    `not_before` is spelled out here rather than defaulted because the schema deliberately gives
    it no DEFAULT -- see `test_a_row_whose_window_is_nothing_is_refused`.
    """
    await db.execute(
        "INSERT INTO jellyfin_intake (item_id, item_type, resolved_key, not_before, raw) "
        f"VALUES ($1, 'Episode', $2, now() + interval '{minutes} minutes', $3)",
        item_id, key, '{"NotificationType": "ItemAdded"}',
    )


# --- decision 363: the vocabulary the sweep reads -------------------------------------


async def test_the_state_check_refuses_a_state_the_sweep_cannot_read(db):
    """Three tables in this tree now carry a column called `state` or `status`, and no two of
    them spell it the same way.

    `acquisition_job.status` is `queued|running|parked|ready|failed` (`0005_ledger.sql:136`) and
    is §6.6's board; `acquisition_task.state` is the corpus's `pending|leased|done|failed|skipped`
    (`0024_acquisition.sql`), which is what the drain's `WHERE state = 'pending'` reads; this one
    is `pending|enqueued|skipped`, because an intake row is not work -- it is the record of an
    event, and the only questions asked of it are "will the sweep act on this" and "why did it
    not". A writer holding an intake row and reaching for a neighbour's word has to be refused
    here: `done` would produce a row that no sweep ever reads again and that §6.6's "recorded
    with that reason" read cannot classify, which is work that is silently invisible rather than
    loudly wrong. [decisions 363, 365]
    """
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO jellyfin_intake (item_id, resolved_key, not_before, state) "
            "VALUES ('jf-1', 'jf-1', now(), $1)",
            "done",
        )


async def test_a_row_whose_window_is_nothing_is_refused(db):
    """`not_before` is NOT NULL and carries no DEFAULT, and both halves of that are deliberate.

    No default, because the value is `received_at + 10 minutes` -- a pure function of this row's
    own arrival, which is what makes the window FIXED rather than sliding (decision 363). A
    default of `now() + interval '10 minutes'` would be very nearly the same number and would
    quietly move the clause from the writer to the schema, where a later change of the window's
    length becomes a migration.

    NOT NULL, because `min(not_before)` is how the sweep decides ripeness and SQL's `min()`
    ignores NULLs: a group of one NULL row has `min(not_before) = NULL`, `NULL <= now()` is NULL,
    and the row is pending forever -- recorded, indexed, and swept by nobody. That is
    `acquisition_task.next_attempt_at`'s failure one table over, and it is refused here for the
    same reason: a row is due at an instant or it is deferred to one, never to nothing.
    """
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.execute(
            "INSERT INTO jellyfin_intake (item_id, resolved_key, not_before) "
            "VALUES ('jf-2', 'jf-2', NULL)"
        )


async def test_a_pending_row_carries_a_key_and_a_refusal_does_not_have_to(db):
    """`jellyfin_intake_pending_rows_have_a_key`, in all three directions it decides.

    A pending row is one the sweep will act on, and the sweep groups by `resolved_key`: a pending
    row without one is invisible to `GROUP BY resolved_key HAVING min(not_before) <= now()` in
    the sense that matters -- it forms a group of its own that nothing can enqueue, because there
    is no key to enqueue. So the constraint refuses it.

    The two shapes that ARE written stay legal, and they are the handler's two exits. A body the
    handler can act on is recorded pending with its key already resolved -- for an `Episode` that
    key is the payload's `SeriesId` and never the episode's own id (decision 369), because the
    webhook is the only direction an episode id enters this app at all. A body it cannot act on
    -- no `ItemId`, an `Episode` with no `SeriesId`, a `NotificationType` that is not `ItemAdded`
    -- is answered 202 and recorded `skipped` with the reason, which is decision 365's whole
    point: a webhook the app rejects is one the operator never learns about. That row has no key
    and must still be writable, or the events this milestone most needs to show an operator are
    the ones it cannot store.

    Stated as an implication rather than a biconditional on purpose: whether a decided row keeps
    the key it was decided on is the sweep's business, not the schema's.
    """
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


# --- §7.2: twelve episodes are one show -----------------------------------------------


async def test_twelve_events_for_one_series_are_twelve_rows_and_one_key(db):
    """No UNIQUE on `resolved_key`, asserted in the direction that catches its addition.

    §7.2's observable is that a burst of `ItemAdded` for twelve episodes of one series inside the
    window yields ONE acquisition task for the show. The collapse happens at SWEEP time, over the
    rows -- which means every one of the twelve has to land first. A UNIQUE here would look like
    the same rule stated a table earlier and would instead throw away eleven events, taking with
    them the one record that says how a burst actually arrived; and the debounce would then be
    enforced by whichever writer got there first rather than by the window.

    The second half is the half that says what the key IS: each row keeps its own `item_id`, so
    the twelve episodes are still individually visible, while `resolved_key` -- the `SeriesId`
    (decision 369) -- is the one value the sweep groups by.
    """
    for episode in range(12):
        await _pending(db, "jf-series-7", f"jf-episode-{episode}")

    rows = await db.fetch("SELECT item_id, resolved_key FROM jellyfin_intake")
    assert len(rows) == 12, "a UNIQUE on resolved_key would have made the second episode unrecordable"
    assert len({row["resolved_key"] for row in rows}) == 1, "twelve events, one show"
    assert len({row["item_id"] for row in rows}) == 12, (
        "each event keeps its own item id: the collapse is the sweep's, not the schema's"
    )


async def test_an_event_can_name_a_title_this_install_has_never_held(db):
    """No foreign key, and this is the absence with a type-compatible repair waiting for it.

    `title_jellyfin_item.jellyfin_id` is a `text PRIMARY KEY` (`0020_jellyfin_items.sql:28`), so
    `REFERENCES title_jellyfin_item(jellyfin_id)` would apply cleanly and would read like the
    missing link a normalised schema wants. It would also delete this milestone: §7.2's subject
    is the title the household has just added and the app does NOT have yet, so the very events
    that matter most are the ones whose key has no mapping row. §8 stage 1 is what mints the
    title (decision 322, and `connectors/resolve.py`'s refusal to invent one), and it is driven
    by the task this row's sweep enqueues -- a constraint here would require the thing the enqueue
    exists to create to already exist.

    The second half catches the other shape of the same repair. A reader adding the foreign key
    would copy `0020`'s `ON DELETE CASCADE` from the line above it, and then deleting a title
    would erase the record of the events that asked for it -- silently, and precisely for the
    titles an operator is most likely to be asking about.
    """
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


async def test_the_pending_set_is_indexed_for_the_one_read_the_sweep_makes(db):
    """Asserted because an index is the first thing a later migration drops when it looks wrong
    beside its neighbour -- and this one does look wrong beside `acquisition_task_ready`.

    The sweep makes one read: pending rows, grouped by the key, filtered on the instant that
    decides ripeness. `(resolved_key, not_before)` serves it in that order, and the partiality is
    what keeps the index the size of the PENDING set rather than of every event this install has
    ever received -- nothing prunes this table, so the two diverge without bound.

    `state` is deliberately NOT a column of the index, which is what makes it read as a
    contradiction of `acquisition_task_ready (state, next_attempt_at, priority)` twelve hundred
    lines away. It is not one: `state` is constant inside a partial index over
    `state = 'pending'`, so leading with it would be a column with one distinct value.

    WHAT THIS DOES NOT PIN is that the sweep actually uses the index, for the reason
    `test_acquire_schema.py::test_the_lease_orders_by_a_key_no_index_can_serve` gives at length: a
    plan read against a unit-sized fixture measures the planner's cost model, which correctly
    prefers a sequential scan of four rows. This says the index is there and is still partial.
    """
    defs = await _index_defs(db, "jellyfin_intake")
    assert "(resolved_key, not_before)" in defs, (
        "the sweep has no index for its one read: the key it groups by, then the instant that "
        "decides ripeness"
    )
    assert "state = 'pending'" in defs, (
        "the sweep's index is no longer partial, so it now carries every event this install has "
        "ever received -- and nothing prunes this table"
    )
