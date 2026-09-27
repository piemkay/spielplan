-- 0038_drop_ledger_state_straddle - the refit wrote it and nothing read it: the board recomputes the
-- straddle from `s`, `sigma_eff` and the cutpoints (rank/board.py).
ALTER TABLE ledger_state DROP COLUMN straddle;
