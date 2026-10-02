-- 0048_notices - a Home notice put away (decision 554). A sticky one hides until the next local
-- midnight or until something new joins it; the finish prompt's x closes it for good, and the seen
-- sync keeps away from a closed prompt's title as it does from an open one (§7.3).
CREATE TABLE notice_hidden (
    user_id   bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    notice    text NOT NULL CHECK (notice IN ('pending', 'setup', 'wish_list')),
    hidden_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, notice)
);

ALTER TABLE playback_event DROP CONSTRAINT playback_event_prompt_state_check;
ALTER TABLE playback_event ADD CONSTRAINT playback_event_prompt_state_check
    CHECK (prompt_state IN ('armed', 'shown', 'answered', 'dismissed', 'closed'));
