-- 0046_rate_placement - Rate journals ladder placements (decisions 536, 545) and has no modes (decision
-- 538). The old kinds stay for history. Live sessions end here: their cards are verdict and pair cards,
-- and their journal rows are answers a placement's Undo cannot take back.
UPDATE rate_session SET ended_at = now() WHERE ended_at IS NULL;

ALTER TABLE rate_observation DROP CONSTRAINT rate_observation_kind_of_check;
ALTER TABLE rate_observation ADD CONSTRAINT rate_observation_kind_of_check
    CHECK (kind_of IN ('verdict', 'not_seen', 'skip', 'duel', 'tie', 'correction', 'placement'));
ALTER TABLE rate_observation ADD COLUMN tier_edit_id bigint REFERENCES tier_edit(id) ON DELETE SET NULL;

ALTER TABLE rate_session DROP COLUMN mode;
