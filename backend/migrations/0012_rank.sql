-- 0012_rank — the one piece of state the Rank surface needs that §4.2 does not already carry.
-- Spec v2.1 §6.3, §5.2, §5.3, §4.2 `ledger_cutpoints`; decision 11.
--
-- Almost nothing, and that is the point. §4.2 already gives Rank everything else: `tier_edit`
-- is the drag-drop arm, `duel.context = 'tier_insert' | 'tier_queue'` are the neighbour and
-- queue comparisons, `duel.selection` already carries §13 stream (a)'s `uniform_holdout`
-- discriminator, `ledger_state.tier` and `.straddle` are the board and its badges, and
-- `ledger_cutpoints` is already keyed `(user_id, kind)` and already holds `tier_set` with the
-- CHECK that ties `array_length(boundaries) = array_length(tier_set) - 1`.
--
-- What is missing is a home for one sentence. Decision 11: changing the tier set
-- "re-initialises [that user's boundaries] to the equal-mass quantiles … and a Ledger refit is
-- queued **for that user alone**". Re-initialisation is immediate arithmetic over the already
-- fitted `s`, so the board is correct the moment the save returns; the refit is not — §5.3
-- budgets a full MAP refit at "seconds", which does not belong inside a settings save. So the
-- request is recorded and the worker sweeps it, and "queued for that user and no other"
-- becomes something a test can read rather than something a comment claims.

ALTER TABLE ledger_cutpoints
    ADD COLUMN refit_requested_at timestamptz;

-- The sweep asks "whose fit is owed?" and must not scan a table that grows with the household.
-- Partial, because the answer is empty almost always — a tier set is changed once and then
-- never again for months.
CREATE INDEX ledger_cutpoints_refit_owed ON ledger_cutpoints (refit_requested_at)
    WHERE refit_requested_at IS NOT NULL;
