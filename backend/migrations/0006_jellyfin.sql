-- 0006_jellyfin — what M1 needs that §4.2's block did not name. Spec v2.1 §3.3, §7.1, §7.3.
--
-- Two additions, each answering a rule the spec states and nothing enforced:
--   1. a link *state*, because §7.3's "a 401 on write → re-link prompt" needs somewhere to
--      record that the per-user token went stale;
--   2. the Jellyfin session id on playback_event, because "arms a per-user prompt" has to
--      arm it once per viewing and not once per poll — the poll runs every minute.
--
-- The sync watermark that looks like it belongs here is deliberately absent: user_title
-- carries `jf_synced_at` per title already, and that column is what separates "the app has an
-- explicit action Jellyfin has not seen" from "Jellyfin changed after we last agreed".

-- §7.3: "at user-link time obtain per-user access tokens … a 401 on write → re-link prompt."
-- The token itself lives AEAD-encrypted in connector_config (§2 — connector secrets never get
-- their own plaintext column); this records only whether it still works.
ALTER TABLE app_user
    ADD COLUMN jellyfin_link_state text
        CHECK (jellyfin_link_state IN ('linked', 'needs_relink'));

-- The link and its state are one fact: a state without an id is unreadable, an id without a
-- state is a link nothing can mark broken.
ALTER TABLE app_user
    ADD CONSTRAINT app_user_link_state_matches_link
        CHECK ((jellyfin_user_id IS NULL) = (jellyfin_link_state IS NULL));

-- §7.3: the /Sessions poll runs on a 1-minute trigger and a film sits above 90% for its last
-- ten minutes. Without a guard, every poll in that window arms another prompt and the user
-- answers the same question ten times.
--
-- The guard is "one OPEN prompt per (user, title)", not "one per Jellyfin session id".
-- Jellyfin's `SessionInfo.Id` is derived from the client and device, so it is stable for as
-- long as that device keeps talking to the server — deduplicating on it would suppress the ten
-- polls inside one viewing and *also* suppress the rewatch three months later on the same
-- television, which is a fair question the household would never be asked.
--
-- `jf_session_id` is still recorded: it is what says which viewing armed a prompt, and §11's
-- external-playback path (which has no session) needs the column to be nullable.
ALTER TABLE playback_event ADD COLUMN jf_session_id text;
CREATE UNIQUE INDEX playback_event_one_open_prompt
    ON playback_event (user_id, title_id)
    WHERE finished AND prompt_state IN ('armed', 'shown');
