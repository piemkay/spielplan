-- 0031_coordinate_geometry - which reading of e(t) a stored fit was made in.
-- Spec v2.1 §5.1 (the fold-in), §5.2 (the Ledger's MAP fit), §5.3, §10; decisions 469 and 471;
-- owner instruction of 2026-09-25 after the first household user test.
--
-- Decision 469 makes the fold-in's personal half read each title's DIRECTION, weighted by the
-- evidence behind it, instead of the raw coordinate, and decision 471 gives the Ledger's fit the
-- same reading. A `user_vector.vec` or a `ledger_fit.theta` fitted against raw coordinates is a
-- vector in a different space from the one the app now scores in: served as it stands, it
-- multiplies unit-length rows by a v that was scaled for rows of norm 0.01 to 127, which is not
-- stale but wrong. `bundle_version` cannot say so - the basis is the same bundle, read another
-- way - so each fit records its geometry, and a row that disagrees with the code's is refitted
-- by the next tick rather than served until the nightly pass (`scoring/foldin._is_stale`,
-- `ledger/refit.load_cache` and `refreshes_owed`).
--
-- NOT NULL DEFAULT 'raw': every row an upgraded install already holds was written by the raw
-- reading, and saying so is what makes the first tick after the upgrade refit it. A fresh install
-- has no rows, and every writer states its geometry explicitly.

ALTER TABLE user_vector ADD COLUMN geometry text NOT NULL DEFAULT 'raw';
ALTER TABLE ledger_fit ADD COLUMN geometry text NOT NULL DEFAULT 'raw';
