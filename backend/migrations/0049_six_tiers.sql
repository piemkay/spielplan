-- 0049_six_tiers - the ladder has six steps, S to E (decision 561). Every member on the old default
-- set moves to the new one at the prior's cutpoints and sets up again: their ladder_setup row goes,
-- so their placements so far are read as earlier ratings, as decision 537 reads any history.
ALTER TABLE ledger_cutpoints ALTER COLUMN tier_set SET DEFAULT ARRAY['E','D','C','B','A','S'];

UPDATE ledger_cutpoints
   SET tier_set = ARRAY['E','D','C','B','A','S'],
       boundaries = ARRAY[-2.944439, -1.386294, 0.0, 1.098612, 2.197225]::float8[],
       refit_requested_at = now(),
       updated_at = now()
 WHERE tier_set = ARRAY['F','D','C','B','A','A+','S'];

DELETE FROM ladder_setup;
DELETE FROM notice_hidden WHERE notice = 'setup';
