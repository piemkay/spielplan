-- 0041_duel_context_tier_place - Place with questions records its duels under a context of its own
-- (decision 528, §6.3).
ALTER TABLE duel DROP CONSTRAINT duel_context_check;
ALTER TABLE duel ADD CONSTRAINT duel_context_check
    CHECK (context IN ('profile_battle', 'tier_queue', 'tier_insert', 'tier_place'));
