-- 0018_read_layer — the rows the real export ships, made readable.
-- Spec v2.1 §4.1 (tables mirror the corpus export; one block = one droppable source), §4.3
-- (dna_vocab ids are `facet.term`), §6.6 (parallel extraction mode), §10 (counts per table);
-- decisions 162, 163, 193; docs/milestones/M4.9-plan.md §5.
--
-- Five sections in one file on purpose. §5 of the plan is one migration: every DDL change M4.9
-- needs is authored here, once, and a second numbered file for a change discovered later would
-- collide with whatever milestone takes 0019. Anything found after this file is applied is 0019
-- and needs a recorded decision.
--
-- Everything here is a new file. No applied migration is edited (0001-0017 are sha256-checksummed
-- and a mismatch is a hard startup error).
--
-- Why a migration at all, when four of the five repairs are loader bugs: decision 162 seeds
-- content ONCE. There is no re-import that would repair an install the M4.5 exit criterion has
-- already seeded, so a loader fix alone repairs the next household and no existing one.

-- ---------------------------------------------------------------------------
-- 1. The DNA facet backfill. §4.3, decision 162.
-- ---------------------------------------------------------------------------
-- The corpus keys a vocabulary id as `characters.amateur_sleuth` and files the extraction pass
-- it ran under as `character_dynamics`. `importer/dna.py` derives `dna_facet` from the term
-- prefix and then copies the shipped `facet` column verbatim into both tag tables, so 29,188 of
-- 31,540 `dna_tag` rows and 206,151 of 223,136 `dna_projected` rows hold a label that joins
-- `dna_facet` nowhere -- and `dna_term`, §6.4's axes and §6.8's eleven-colour palette all key on
-- the prefix. Every one of the eleven distinct prefixes in the shipped data IS a vocabulary
-- facet id, which is why the rewrite below can be unconditional rather than a lookup.
--
-- The `term LIKE '%.%'` guard is not decoration, and what it prevents is not a NULL.
-- `split_part(term, '.', 1)` returns the WHOLE string when there is no delimiter -- it is field
-- 2 that answers '' -- so the unguarded form would not blank an undotted vocabulary: it would
-- overwrite every such row's facet with the term id itself, a value that joins no `dna_facet`
-- row, renders `var(--ink-4)` on every chip, and reads as plausible. Measured on the server
-- this ships against (16.15): `split_part('legacyterm', '.', 1)` is `'legacyterm'`, and the
-- unguarded UPDATE reports one row rather than raising. [decision 196]
--
-- A future vocabulary whose ids are not dotted is also why there is NO CHECK constraint pinning
-- `facet = split_part(term, '.', 1)`: it would refuse that vocabulary outright, and it would
-- refuse this bundle at COPY time before the importer change lands.
UPDATE dna_tag
   SET facet = split_part(term, '.', 1)
 WHERE term LIKE '%.%' AND facet <> split_part(term, '.', 1);

UPDATE dna_projected
   SET facet = split_part(term, '.', 1)
 WHERE term LIKE '%.%' AND facet <> split_part(term, '.', 1);

-- ---------------------------------------------------------------------------
-- 2. `dna_tag.provider`, so the arbiter index fires. §6.6, §4.1 rule 1.
-- ---------------------------------------------------------------------------
-- `0004_dna.sql:83` declares `UNIQUE (title_id, version, term, provider)` and the importer never
-- writes `provider`, so every row carries NULL -- and NULLs are distinct in a unique index. The
-- constraint that is supposed to keep one provider's opinion to one row per term therefore
-- matched nothing at all, and §6.6's parallel extraction mode would write a second row per term
-- with nothing raised anywhere.
--
-- `''` rather than `UNIQUE NULLS NOT DISTINCT`: the importer then writes a real value, M5's
-- per-provider extraction gets an upsert key, and the semantics of "no provider recorded" stay
-- one value instead of two.
--
-- THIS STATEMENT CAN FAIL ON A REAL INSTALL, and that is the point. An install already carrying
-- two extracted rows for one (title_id, version, term) has been silently accepting what rule 1
-- forbids, and collapsing NULL to '' makes the index see them. The failure is a duplicate-key
-- error naming `dna_tag_title_id_version_term_provider_key`; the repair is to decide which row
-- is the extraction of record and delete the other, never to widen the key. Verified safe
-- against v20260828 and against `backend/tests/fixtures/make_bundle.py`: zero duplicate
-- (title_id, term) groups in `dna_tag` and zero in `dna_projected`.
UPDATE dna_tag SET provider = '' WHERE provider IS NULL;
ALTER TABLE dna_tag ALTER COLUMN provider SET DEFAULT '';
ALTER TABLE dna_tag ALTER COLUMN provider SET NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. `title_company`, keyed the way the corpus keys it. Decision 193.
-- ---------------------------------------------------------------------------
-- Exactly the repair `0015_seed.sql` section 9 applied to `title_language`, `title_country` and
-- `platform_rating` on 2026-09-02, arriving late for the fourth sibling. §4.1 opens "tables
-- mirror the corpus export" and says of `title_meta`: "multi-source, per-source rows kept -- one
-- block = one droppable source". `0003_content.sql:120-125` keyed this table
-- (title_id, company, role), one component coarser than the corpus: 47,607 shipped rows, 8,594
-- duplicate groups under the app's key, 11,654 rows discarded. Stated in `0015_seed.sql` section
-- 9's own two-column form (rows, then groups), because a group count is not a row count and
-- per-source row multiplicity is this whole file's subject. [decision 195] So `importer/load.py`
-- names the table in `SKIPPED_TABLES` and none of the 47,607 rows land.
--
-- What that costs is NOT a zeroed input to the Cold Tower. `placement/features.py:403` counts
-- company rows into the thin-title meta block and `'companies'` sits in `_COUNT_KEYS`, but
-- `n_companies_log` is a column of no feature contract this app has ever loaded -- v20260828's
-- `meta` block is 57 one-hots (13 `decade:`, 2 `kind:`, 5 `runtime:`, 37 `lang:`) -- so
-- `build_vector` counts the key as unmapped and the number reaches no coordinate and no
-- checkpoint. Decision 194 records the measurement. The table loads on §4.1's own terms
-- instead: "tables mirror the corpus export", and 47,607 rows of the spine are missing.
--
-- `role` survives beside `source` for `title_language`'s reason: it is what the company did on
-- the film, a different fact from which source said so.
ALTER TABLE title_company DROP CONSTRAINT title_company_pkey;
ALTER TABLE title_company ADD COLUMN source text NOT NULL DEFAULT '';
ALTER TABLE title_company ADD PRIMARY KEY (title_id, source, company, role);

-- ---------------------------------------------------------------------------
-- 4. `title_video`, keyed the way the corpus keys it. §4.1.
-- ---------------------------------------------------------------------------
-- The corpus's key is (title_id, source, key); `0003_content.sql:127-133` kept the app's own
-- (title_id, site, key) and dropped `source` entirely. The shipped bundle has one video source
-- and zero collisions, so nothing is wrong today -- which is precisely the shape that took the
-- three tables in 0015 section 9 down: the first export in which a second source reports a
-- trailer the first already lists turns a green `validate()` into a 500 on COPY, at the one
-- moment a household is watching an import run.
--
-- The corpus's own primary key already guarantees no two rows share (title_id, source, key), so
-- `site` becomes an ordinary column rather than a key component. It is not dropped: it is what
-- §6.0's card would link out to.
ALTER TABLE title_video DROP CONSTRAINT title_video_pkey;
ALTER TABLE title_video ADD COLUMN source text NOT NULL DEFAULT '';
ALTER TABLE title_video ADD PRIMARY KEY (title_id, source, key);

-- ---------------------------------------------------------------------------
-- 5. `rating_source`'s terms, which the mapping dropped. §4.1 rule 4, §10.
-- ---------------------------------------------------------------------------
-- The bundle's `rating_source` is the one place the corpus recorded per-dataset terms: the
-- Netflix Prize's research-use-only clause, the CC BY attributions naming their authors, the
-- version of each dataset the ids were frozen against. `0003_content.sql:219-224` keeps `id`,
-- `name` and `scale` and the importer maps nothing else, so no surface can print the attribution
-- those licences require and no operator can tell which of the eleven frozen sources bars
-- redistribution of a movie-data archive -- the question §6.6's Data card exists to answer.
--
-- All four nullable with no defaults: a source that shipped no terms must read as "not stated"
-- rather than as permissively licensed. The frozen id set (rule 4) is untouched.
ALTER TABLE rating_source ADD COLUMN url     text;
ALTER TABLE rating_source ADD COLUMN license text;
ALTER TABLE rating_source ADD COLUMN version text;
ALTER TABLE rating_source ADD COLUMN notes   text;

-- Nothing else. In particular: NO `assigned_tier` column on `ledger_state` (a second home for a
-- value the append-only `tier_edit` already derives -- decision 187 keeps the card badge on
-- `ledger_state.tier` and repairs only the headline, in the read layer); NO `model_event` table
-- (§6.7's "never persisted" is correct, and decision 189 declares the worker-side kinds awaiting
-- a producer rather than inventing a channel); NO UNIQUE on `title.imdb_id` (§4.1 forbids it as
-- a join key, and the ml_link resolution reports duplicates rather than the schema refusing
-- them); and NO `episode_count`/`season_count` on `title` (decision 192 makes the mixed runtime
-- semantics a §10 report note instead of a column the app would derive arithmetic from).
