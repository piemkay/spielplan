-- 0040_drop_rate_session_decisive - "Much more" weights one answer, so no switch is stored
-- (decision 528).
ALTER TABLE rate_session DROP COLUMN decisive;
