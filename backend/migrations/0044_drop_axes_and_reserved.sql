-- 0044_drop_axes_and_reserved - the per-facet axis machinery is deleted and Tonight's split is by
-- person alone (decision 542). The movie-data archive names the axis tables in RETIRED, so an archive
-- written before this file still restores. Dropping `reserved` drops 0033's CHECK with it.

DROP TABLE dna_axis_weight;
DROP TABLE dna_axis;
ALTER TABLE session_result DROP COLUMN reserved;
