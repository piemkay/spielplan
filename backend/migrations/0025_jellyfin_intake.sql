-- 0025_jellyfin_intake — the webhook's durable pending set: one row per delivered Jellyfin
-- event, swept ten minutes later into one acquisition task per resolved title.
-- Spec v2.1 §7.2 ("POST /events/jellyfin (ItemAdded), token-authed ... Debounce 10 min; series
-- acquire per-show, not per-episode"), §5.3 (every job durable), §6.6 (the library pick this
-- filter finally reads); decisions 363, 364, 365, 366 and 369;
-- docs/milestones/M5.2-plan.md §5.
--
-- NUMBERING. 0025 is what docs/milestones/ROADMAP-M5.md's migration ledger allocates to M5.2,
-- and the directory confirms it: 0016-0018, 0020-0024 and no 0025. 0024_acquisition.sql is
-- M5.1's and is APPLIED, so it is never edited; 0026 and 0027 belong to the two sibling lanes
-- building beside this one and are not taken here; 0019 was allocated at M4.10, which needed no
-- schema, and stays permanently unused rather than being recycled. `db/migrate.py` keys
-- `schema_migration` on the filename stem and sorts a glob, so a gap costs nothing and a
-- renumber would cost everything: a file already applied under one stem silently re-runs under
-- a new one and dies on its first statement.
--
-- ONE TABLE IN THIS FILE, AND NO WATERMARK COLUMN. §7.2's other path is the 15-minute
-- `DateCreated > last_sync` delta poll, which needs a library-wide "everything created after T"
-- that this schema has never carried — 0006_jellyfin.sql:9-11 declined to add one and argued the
-- omission, and that argument is in an applied, checksummed file and is not edited here.
-- Decision 366 puts the watermark in the `jellyfin` row of `connector_config`
-- (0001_system.sql:22-30) instead: it is connector state rather than household data,
-- `connectors/registry.py` already does the sealed read-modify-write under FOR UPDATE that a
-- watermark needs, and a key in a jsonb column needs no DDL. It is initialised on a fresh
-- install to `SELECT min(applied_at) FROM schema_migration` — this database's own creation
-- instant — and never to epoch, because an epoch watermark enqueues the household's entire
-- corpus on the first poll.

-- ---------------------------------------------------------------------------
-- The intake set. Decision 363.
-- ---------------------------------------------------------------------------
-- WHY A TABLE AND NOT M5.1'S QUEUE. `acquisition_task` is durable and keyed on (kind, key), so a
-- task deferred to now() + 10 min would be a fixed-window debounce with no new table, and the
-- plan names that alternative first. It cannot hold what this milestone has to record: a row in
-- that queue IS an enqueue, and three of §7.2's observable outcomes are events deliberately NOT
-- enqueued — an add in a library the admin deselected, a payload that arrived without the fields
-- the handler acts on, an Episode event with no SeriesId. None of those has a key the queue
-- could take, and the last two may have no item id at all. So the pending set is a record of
-- what arrived, and the queue stays a record of what was decided.
--
-- WHY IT IS DURABLE AT ALL. §5.3 files every job as durable, and a library scan delivers a
-- season in a burst: a pending set living in the worker's memory would drop the whole scan to a
-- restart inside the window, and the household would never learn that it had. That is the one
-- reason this is a table rather than a dict, and it is why the coverage row
-- `jellyfin-acquisition-eval-the-webhook-survives-what-a-real-server-sends` names the restart.
--
-- THE WINDOW IS FIXED AND PER KEY. `not_before` is written at INSERT as a pure function of this
-- row's own arrival — received_at + 10 minutes — so a later event for the same key extends
-- nothing; a key is ripe when min(not_before) over its pending rows has passed. A sliding window
-- would let a long library scan postpone acquisition indefinitely, which is the failure this
-- shape trades away; what it costs instead is that a scan running longer than ten minutes starts
-- acquiring while it is still adding, and that is the honest side of the trade.
--
-- `resolved_key` IS THE TITLE, NOT THE EVENT. For an `Episode` it is the payload's `SeriesId`
-- (decision 369), and the resolution happens before anything else touches the row, because the
-- webhook is the only direction an episode id enters this app at all — `ITEM_TYPES` is
-- "Movie,Series" and no read of this client's own ever returns one. An Episode event with no
-- SeriesId is recorded with that reason and never keyed on the episode: the fallback that looks
-- harmless here is exactly the twelve jobs §7.2 forbids.
--
-- NO FOREIGN KEY, for `acquisition_task`'s reason one table over and one step earlier: the key
-- is a Jellyfin item id for a title this install may not hold yet, and §8 stage 1 is what mints
-- the row. `title` cannot be referenced at all — its id is an integer — but
-- `title_jellyfin_item.jellyfin_id` is a `text PRIMARY KEY` (0020_jellyfin_items.sql:28), so it
-- would apply cleanly and read like the missing link a normalised schema wants, which makes it
-- the repair a later reader actually reaches for. It would delete this milestone: §7.2's subject
-- is the title the household has just added and this app does NOT have, so the events that
-- matter most are precisely the ones whose key has no mapping row to point at.
--
-- AND NO UNIQUE ON `resolved_key`, which is that same sentence from the other side. Many events
-- for one key is the entire subject of a debounce, and the collapse to one acquisition task
-- happens at SWEEP time, over the rows. A UNIQUE here would read like §7.2's per-show rule
-- stated a table too early and would instead discard eleven of a season's twelve events — with
-- them the only record of how the burst arrived — and hand the debounce to whichever writer got
-- there first rather than to the window.
CREATE TABLE jellyfin_intake (
    id           bigserial PRIMARY KEY,
    received_at  timestamptz NOT NULL DEFAULT now(),
    -- The payload as delivered. The Jellyfin Webhook plugin sends an operator-configured
    -- template, so no clause fixes its fields (decision 365): the handler requires `ItemId` and
    -- `ItemType`, treats the rest as advisory, and keeps the body whole so that an operator
    -- whose template is wrong can be shown what actually arrived rather than told 400.
    raw          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    item_id      text,
    item_type    text,
    resolved_key text,
    not_before   timestamptz NOT NULL,
    state        text        NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'enqueued', 'skipped')),
    -- Why a row was not enqueued, in the app's own words: 'library not picked', 'no item id',
    -- 'episode without series id', 'not an ItemAdded'. §6.6's card renders it at M5.7; until
    -- then it is what an operator reads in the table when a webhook they configured did nothing.
    reason       text,
    -- A pending row is one the sweep will act on, and it cannot act without a key. The refusals
    -- the handler can already see are recorded as 'skipped' at insert; the one it cannot -- is
    -- this item inside a library the admin picked, which is a server read (decision 364) --
    -- stays pending, and a membership read that FAILS leaves it pending for the next sweep
    -- rather than dropping it. Stated as an implication rather than a biconditional: what a
    -- decided row keeps is the sweep's business, not the schema's.
    CONSTRAINT jellyfin_intake_pending_rows_have_a_key
        CHECK (state <> 'pending' OR resolved_key IS NOT NULL)
);

-- The sweep's only index, in the order it reads: the key it groups by, then the instant that
-- decides ripeness. One read is all the sweep makes — pending rows, grouped by `resolved_key`,
-- a key ripe when `min(not_before)` over its group has passed (decision 363) — and both halves
-- of it are served here, so the second index this table looks like it wants, sized for the
-- per-key collapse alone, would be a prefix of this one.
--
-- PARTIAL, AND THAT IS WHY `state` IS NOT A COLUMN OF IT. Beside `acquisition_task_ready (state,
-- next_attempt_at, priority)` one file back, this reads as the same index written wrongly, and a
-- reader repairing it on sight would take away the sweep's only read. It is not the same index:
-- inside `WHERE state = 'pending'` that column is constant, so leading with it would index one
-- distinct value. What the partiality buys is size — this index is the PENDING set, not every
-- event the install has ever received, and nothing prunes this table because §6.6's "recorded
-- with that reason" is a record an operator reads months later. The two therefore diverge
-- without bound, which is the divergence `acquisition_task_expired` is partial for.
CREATE INDEX jellyfin_intake_ripe
    ON jellyfin_intake (resolved_key, not_before) WHERE state = 'pending';
