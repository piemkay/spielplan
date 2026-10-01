-- 0045_wish - the household's wish list and each person's Not for me (decision 544, §4.2). A want row
-- whose title is owned is an arrival; nothing here is fetched, bought or requested elsewhere.
CREATE TABLE wish (
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id   int NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    state      text NOT NULL CHECK (state IN ('want', 'not_for_me')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, title_id)
);
CREATE INDEX wish_wanted ON wish (title_id) WHERE state = 'want';
