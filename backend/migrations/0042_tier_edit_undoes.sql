-- 0042_tier_edit_undoes - a Rank Undo names the tier_edit it takes back (decision 534, §4.2). Recorded
-- only: the fit reads every tier_edit as before.
ALTER TABLE tier_edit ADD COLUMN undoes bigint REFERENCES tier_edit(id) ON DELETE SET NULL;
