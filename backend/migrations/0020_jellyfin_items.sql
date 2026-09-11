-- 0020_jellyfin_items — one title, many copies. Spec v2.1 §7.1 (identity), §7.3 (seen sync);
-- decisions 210 and 211; docs/milestones/M4.11-plan.md §5.
--
-- Why a table at all, when every other repair in M4.11 is code: knowledge of WHICH Jellyfin items
-- are one title has nowhere to live today. `title.jellyfin_id` is a single column and a household
-- whose libraries ship 'Movies' and 'Movies 4K' gives one film several items, so that column
-- became a pointer that followed whichever copy the last sweep happened to read — flipping every
-- fifteen minutes, losing the finish prompt for whichever copy it was not on, and letting a
-- duplicate's Played flag revert the explicit "not seen" the person typed. §7.1's "one
-- `jellyfin_id` per title" still holds and the column stays: it is the representative copy. This
-- table is the SET that copy represents, which is what an `unseen` has to clear Played on (or the
-- next sweep re-adopts it from the other copy) and what a `/Sessions` row has to be resolved
-- through before a prompt can be armed for the right title.
--
-- What is deliberately NOT here: the `playback_event.prompt_state` CHECK extension the plan's §5
-- sketched under its option (A). Decision 211 takes (B) instead — while a finish prompt is open
-- the sweep adopts nothing for that (user, title) — so there is no sync-closed prompt left to
-- label, and `0005_ledger.sql`'s constraint is untouched. Dropping and recreating an applied
-- CHECK to add a state whose only reader would be `sync/playback.py` is a migration bought for
-- nothing.
--
-- A new numbered file, as always: 0001-0019 are sha256-checksummed from their first apply and a
-- mismatch is a hard startup error. 0019 was allocated to M4.10 and stayed unused because that
-- milestone needed no schema; it stays unused, because the ledger maps a number to the milestone
-- that owns it and a later reader has to be able to make that map.

CREATE TABLE title_jellyfin_item (
    jellyfin_id text PRIMARY KEY,
    title_id    integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    seen_at     timestamptz NOT NULL DEFAULT now()
);

-- The lookup the sweep makes once per title — "every copy of this one" — for the push that must
-- reach all of them. The other direction, an item id from `/Items` or `/Sessions` to its title,
-- is served by the primary key. `seen_at` records when the sweep last saw the copy in the
-- library; §7.2's ownership pass is what acts on a copy that stops appearing, not this table.
CREATE INDEX title_jellyfin_item_title ON title_jellyfin_item (title_id);
