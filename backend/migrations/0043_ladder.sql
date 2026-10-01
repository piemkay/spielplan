-- 0043_ladder - a member's cut-over (decision 537) and the placement re-ask (§13 stream b).
-- A table, not an `app_user` column: the 1.1.0 re-seed restores `app_user`, and a column would leave
-- a member set up over a wiped ladder.
CREATE TABLE ladder_setup (
    user_id     bigint PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    finished_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE tier_edit ADD COLUMN reask_of bigint REFERENCES tier_edit(id) ON DELETE SET NULL;
