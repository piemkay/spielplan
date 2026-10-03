-- 0051_sharpen - Sharpen's cross-tier check arm and the moves it writes (decision 564).
ALTER TABLE duel DROP CONSTRAINT duel_selection_check;
ALTER TABLE duel ADD CONSTRAINT duel_selection_check
  CHECK (selection IN ('random', 'boundary', 'exploration', 'cross_tier', 'uniform_holdout'));
ALTER TABLE tier_edit DROP CONSTRAINT tier_edit_via_check;
ALTER TABLE tier_edit ADD CONSTRAINT tier_edit_via_check
  CHECK (via IN ('drag_drop', 'explicit', 'sharpen'));
