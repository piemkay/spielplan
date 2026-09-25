-- 0030_dna_term_label - the human name the corpus ships for every vocabulary term.
-- Spec v2.1 §6.8 ("a one-line why in vocabulary terms"), §4.3 (`dna_vocab/<version>/`);
-- decision 486 (a member surface names a term by its label, never by its id); owner instruction of
-- 2026-09-25 after the first household user test.
--
-- Every `vocab_<facet>_<version>.tsv` carries a `label` column, and `importer/dna.py` required it
-- and then dropped it on the claim that a label is the id minus its facet prefix. The shipped v1
-- vocabulary refutes that: about 200 of its 582 labels are not a respelling of the id's leaf
-- (`era.wwii` is "World War II", `themes.love_romance` is "love & romance"), so every member
-- surface printed `era.wwii` because the one column that names it was never stored.
--
-- NULLABLE, WITH NO DEFAULT. An install seeded before this file holds no label until the worker's
-- boot backfill reads the active bundle's TSVs, and a restored install whose /data/artifacts is
-- gone may never hold one; every reader falls back to the leaf with underscores as spaces
-- (`db/dna_terms.label_of`). Nothing here can refuse a row that already exists, and no content row
-- is written: the vocabulary tier stays what decision 162 says it is.

ALTER TABLE dna_term ADD COLUMN label text;
