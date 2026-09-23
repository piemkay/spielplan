-- 0027_dna_extraction — what §8 stage 7 refuses, and the pack it refused it against.
-- Spec v2.1 §8 stages 5, 7 and 8 ("ported packs.py (interleaving, caps, norm())", "ported trust
-- boundary verbatim … Failures drop, never repaired", "per-title alias-map projection"), §6.6 Data
-- ("review of DNA rejects and low-evidence tags"), §4.1 rules 1 and 2, §9 ("the schema is a
-- cost-saving device, not the guarantee — the guarantee is the validator"); decisions 341, 382 and
-- 383; docs/milestones/M5.4-plan.md §5.
--
-- NUMBERING. 0027 is what docs/milestones/ROADMAP-M5.md's migration ledger allocates to M5.4.
-- 0024 is M5.1's and is APPLIED, so it is checksummed and unreachable (`db/migrate.py` raises on a
-- mismatch at startup); 0025 and 0026 are allocated to M5.2 and M5.3, which are building in
-- parallel worktrees as this lands and may or may not spend them; 0019 stays permanently unused.
-- `db/migrate.py` keys `schema_migration` on the filename stem and sorts a glob, so a gap costs
-- nothing and a renumber would cost everything.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not touch `dna_tag`, `dna_evidence`,
-- `dna_projected` or the `dna_tagged` view: all four already have the shapes this milestone
-- writes, and `dna_tag.salience smallint NOT NULL CHECK (salience IN (1, 2, 3))` is stage 7's
-- third rule stated as a constraint (0004_dna.sql:78). It adds no view, because `dna_tagged` is
-- the ONE sanctioned place the two tiers may be read together (§4.1 rule 1, 0004_dna.sql:120-128,
-- test_landmine_guards.py's SANCTIONED_VIEW). And it carries no spend or provider-call bookkeeping
-- at all: `llm_call` is 0028's and M5.5's, which is the other half of decision 382.

-- ---------------------------------------------------------------------------
-- 1. What the trust boundary refused. Decision 341.
-- ---------------------------------------------------------------------------
-- §6.6 Data promises "review of DNA rejects and low-evidence tags" and §8 stage 7 says "Failures
-- drop, never repaired". Both are true and they need this table between them: a dropped tag that
-- is never written cannot be reviewed, and before this file `grep -rn dna_reject backend/`
-- returned nothing. The three manifest tables that might have served are declined by the importer
-- on purpose and it says why (importer/load.py:370-372) — `dna_annotation` is curator working
-- notes, `dna_term_signal` is vocabulary-building telemetry superseded by the shipped vocabulary,
-- and `dna_exclusion` is the corpus's own exclusions applied before export. None of the three is a
-- record of what THIS install's verifier refused.
--
-- `rule_violated` is the ported REASONS tuple verbatim (mdc/dna/store.py:60-61) and is CHECKed
-- against it, so §6.6's filter has a closed set and a spike in one reason is readable as itself.
-- The split between `adjudicated` and `unknown_term` is the one that earns its keep: a curated
-- retirement and an extractor emitting garbage look identical in a count that merges them, and
-- the corpus keeps them apart for exactly that reason (decision 389).
--
-- "LOW-EVIDENCE" IS AN ORDERING AND NEVER A FILTER, which is why no `confidence` column appears
-- here. §4.1 rule 2 makes `salience`, `confidence` and `n_sources` weights and never filters — "no
-- `WHERE confidence > x` anywhere" — so the second half of §6.6's sentence is the `dna_tag` rows
-- ORDERED by ascending confidence, and this table is the first half. A screen built over it may
-- ORDER BY; it may not WHERE.
--
-- AND WHAT THE STATIC GUARD ENFORCES IS COMPARISONS, WHICH IS LESS THAN THIS PARAGRAPH USED TO
-- CLAIM FOR IT (decision 401). test_landmine_guards.py reads every module and every migration for a
-- weight compared to a value after a clause keyword, across the whole operator family, through a
-- cast or an aggregate or a coalesce wrapper, for the bare predicate this package appends in
-- Python, for a ranking on a weight that is then truncated, and for the Python equivalents of all
-- of them. That is a comparison scan, and two shapes select rows on a weight while comparing it
-- to nothing. Both were measured past it: over SQL,
-- WHERE salience IS NOT NULL — which against `dna_tagged` deletes the whole projected tier, since
-- 0004_dna.sql:127 emits `NULL::real AS salience` for it — and over Python,
-- if row["confidence"]: — invisible because `_COMPARISON_OPS` carries no `Is`/`IsNot` and the scan
-- walks only `ast.Compare`. Widening that guard is not this file's to do: it belongs to no
-- milestone in this wave and no code in the tree writes either shape today. So the two are
-- recorded here as owed against M5.6, the milestone that writes §6.6's screen and stands on the
-- claim. The test named for this paragraph in test_dna_verify.py holds the sentence and the
-- guard to each other: it fails if the guard learns to see either shape, which is when this
-- claim has to be read again, and it fails if this paragraph stops naming them.
-- [M5.4 review cycle 2, M54-DIM3-07; decision 401]
--
-- `salience` carries NO CHECK, and that absence is the point rather than an oversight. `dna_tag`
-- constrains it to {1,2,3} because a tag that passed must satisfy stage 7's third rule; this table
-- exists to record the values that did NOT, so a 0 and a 4 have to be storable verbatim. The
-- corpus clamps them instead (max(1, min(3, sal)), mdc/dna/store.py:214) and that is not ported:
-- a clamp is a repair, and the ported rule is that failures drop (decision 386).
--
-- `title_id` is NULLABLE with its foreign key kept, because `unknown_title` is one of the seven
-- reasons: a payload naming a title this install does not hold is refused, and the refusal is the
-- row. ON DELETE CASCADE for the rows that do name a title — a reject is a fact about a title's
-- extraction and has no meaning once the title is gone. `run_id` carries no foreign key, for
-- 0024's reason one table over: `worker.py`'s `_prune_job_runs` deletes ops rows on a retention
-- rule, so a constraint would either block the prune or take the refusal history with it.
CREATE TABLE dna_reject (
    id            bigserial PRIMARY KEY,
    title_id      integer     REFERENCES title(id) ON DELETE CASCADE,
    run_id        bigint,
    term          text,
    facet         text,
    salience      smallint,
    quote         text,
    rule_violated text        NOT NULL
                  CHECK (rule_violated IN ('schema', 'unknown_term', 'adjudicated',
                                           'quote_unverified', 'unknown_title', 'no_pack',
                                           'duplicate')),
    provider      text,
    at            timestamptz NOT NULL DEFAULT now()
);
-- The board reads a title's refusals, and the reviewer reads the newest of one kind: §6.6's screen
-- opens on "what has this install been dropping lately", which is the second index, and drills into
-- one title, which is the first.
CREATE INDEX dna_reject_title ON dna_reject (title_id, at DESC);
CREATE INDEX dna_reject_rule ON dna_reject (rule_violated, at DESC);

-- ---------------------------------------------------------------------------
-- 2. The pack a verdict was reached against. Decision 382.
-- ---------------------------------------------------------------------------
-- Stage 7's second rule is that the quote is a substring of THAT TITLE'S PACK under norm(), so a
-- verification that cannot be reproduced against the text it was made from is not auditable — and
-- nothing in the spec says where the app keeps a pack. The corpus keeps them in files
-- (mdc/dna/packs.py's pack_path); this app's equivalent is M5.1's raw store, which §8's own
-- preamble already designates for bytes ("All fetched bytes land in the app's own raw store, so
-- re-parsing is free forever") and which is content-addressed, immutable and absent from the
-- backend container. The pack goes in as source='pack', kind='dna', url='pack:title:<id>', and
-- this row is its index.
--
-- The corpus's scar is the argument for the sha being a property of the EVIDENCE rather than of
-- whatever is on disk at ingest time: 825 titles were marked current against a pack no pass had
-- seen, which hid 652. `pack_sha` is therefore the digest of the pack text the tags were extracted
-- from, written by the same call that built it.
--
-- UNIQUE ON (`version`, `pack_sha`) so that 0028's `llm_call` can reference it and need not
-- re-carry custody of the pack (decision 382: one of the two migrations holds it and the other
-- cites it), and SCOPED TO THE VERSION for §14 risk 7 (`spec:503`): "Two vocabularies in the
-- tables is a state this build does not create and must still survive." A column-level UNIQUE did
-- not survive it. `render_pack` takes no version and emits none, so one title's pack under v1 and
-- under v2 is byte-identical whenever its plot and reviews have not changed -- which decision 162
-- makes the ordinary case, content being seeded once -- and the second version's INSERT was
-- refused for the whole unchanged library while `read_pack` scopes its read to the version and so
-- demanded exactly the row the writer could not create. Every other table in this family is
-- version-scoped for the same clause (`dna_term`, `dna_facet`, `dna_alias`, `dna_tag`,
-- `dna_projected`); this one now is too. [M5.4 review cycle 1, M54-C1-PACK-01; decision 395]
--
-- AND SCOPED TO THE TITLE: THE KEY IS (`title_id`, `version`, `pack_sha`), CORRECTED IN PLACE A
-- THIRD TIME. This file is applied to no durable install -- only to M5.4's own lane cluster --
-- which is the one condition under which an edit here rather than a new migration is legitimate,
-- and it is said loudly for that reason. The (version, pack_sha) key argued that two titles could
-- only collide through byte-identical packs and that stage 4's reviews gate made that
-- unreachable. Neither half held. `render_pack` writes the title's NAME and YEAR into the header
-- and never its id, and `build_pack` gives a title with no plot and no reviews a two-line pack on
-- purpose, so any two unenriched titles sharing a name, a year and a kind hold one pack and one
-- sha -- an ambiguity real enough that decision 360's `_indistinguishable_titles` exists to refuse
-- a mint over it -- and the reviews gate is a declared no-op M5.3 owns, with decision 387 wiring
-- nothing in front of `store_pack` at all.
-- Measured: the second title's INSERT raised a bare `UniqueViolationError` that the upsert's
-- ON CONFLICT (title_id, version) does not cover, leaving it `no_pack` for good; and a title whose
-- rebuilt pack came to equal another's kept a row naming its OLD pack, which `read_pack` returned
-- without raising because the row and its bytes still agreed. The collision was never a park
-- either: `acquire/pipeline.py` turns a raised stage into a failure that spends attempts. An
-- `llm_call` is about one title, so (title, version, sha) is the key it wants to reference, and
-- the key it references can no longer be spent by a different title. What stays open is M5.5's to
-- answer in 0028: what an `llm_call` whose pack has since been rebuilt points at, given that this
-- table keeps one row per (title, version) rather than a pack history.
-- [M5.4 review cycle 3, M54-C3-PACK-02; decision 403]
--
-- ON DELETE RESTRICT AND NOT SET NULL, because the two states SET NULL produces were the same state
-- to every reader this app had. `read_pack` answered None on a NULL `raw_document_id` exactly as it
-- did on a missing row, and `verify_payload` recorded `no_pack` — so an operator or a later
-- retention prune removing a `raw_document` row turned "the evidence for verdicts already reached
-- has been deleted, while this row still names its sha" into "this install has never packed these
-- titles", which is the reading §6.6's reject review would show. The plan's own risk is the rule:
-- the pack has to be retained or stage 7 is unauditable. RESTRICT protects exactly the document
-- this row names — `store_pack` upserts the column to the newest build, so the earlier packs stay
-- prunable — and nothing in the tree deletes from `raw_document` at all today, so it blocks no
-- path that exists. 0023's decision 249 closed a SET NULL of the same shape one table over, after
-- a nulled pointer made §12's M2 index report zero owned titles waiting to be placed.
-- [M5.4 review cycle 1, M54-C1-PACK-06; decision 395]
--
-- AND NOT NULL, CORRECTED IN PLACE A FOURTH TIME, on this file's one legitimate ground: it is
-- applied to M5.4's own lane cluster and to no durable install. RESTRICT closed the one ROUTE to a
-- NULL and left the column able to hold one, so the state the paragraph above calls
-- indistinguishable from "never packed" was still one UPDATE away -- a retention job, an operator's
-- repair, 0028 itself -- and `read_pack` answered None for it. The schema refuses the state now,
-- and `read_pack` no longer carries a branch reading it as no pack. `store_pack` always binds a
-- document id, so no write this tree makes is refused. [M5.4 review cycle 3, M54-C3-PACK-04]
--
-- `n_reviews`, `n_sources` and `chars` are the pack's shape, not weights on a tag: they say what
-- went into the text (how many reviews survived the caps, how many distinct sources they came
-- from, how long the result is) and are what §6.6's screen shows beside a refusal, because decision
-- 345 keeps the bytes off the backend. They are never read as a filter on anything.
CREATE TABLE dna_pack (
    title_id        integer     NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    version         text        NOT NULL REFERENCES dna_vocabulary(version) ON DELETE CASCADE,
    pack_sha        text        NOT NULL,
    raw_document_id bigint      NOT NULL REFERENCES raw_document(id) ON DELETE RESTRICT,
    n_reviews       integer     NOT NULL,
    n_sources       integer     NOT NULL,
    chars           integer     NOT NULL,
    built_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (title_id, version),
    UNIQUE (title_id, version, pack_sha)
);

-- ---------------------------------------------------------------------------
-- 3. The alias map's own `kind`, so a lexicon row can be told from a projecting one. Decision 383.
-- ---------------------------------------------------------------------------
-- §8 stage 8 projects a title's keywords through the alias map, and the corpus's own loader skips
-- `kind='lexicon'` rows for a measured reason: "the mood twins' keyword surfaces cannot walk
-- projected rows into the register facet through the back door (measured: Django and Hostel both
-- inheriting register.pulp, cos 0.894)" (mdc/dna/project.py:144-149). This app's loader reads
-- `raw_term` and `vocab_term` only and drops the column, so a lexicon row is today stored
-- indistinguishably from a projecting one — a rule that cannot be applied because the data was
-- dropped is not a rule, which is why the column lands now.
--
-- NULLABLE, and NULL means "not known to be lexicon" rather than "not lexicon". Every shipped row
-- is NULL until a loader writes the column, and the one-line edit to `importer/dna._load_aliases`
-- that would fill it is NOT taken here: `importer/dna.py` is M5.3's file under ROADMAP-M5's
-- file-locality table and that lane is building in parallel. The debt is recorded on the coverage
-- row `data-rules-vocabulary-layer-loads-the-files-the-corpus-ships`, whose sentence about what
-- the loader reads therefore does NOT widen yet.
--
-- The alias map's other two fidelity gaps need no schema help and are closed in the reader
-- (`dna/aliases.py`): the map keys on the NORMALISED raw term, and a row whose vocabulary term
-- `dna_term` does not carry is skipped. That second one is a real case rather than a defensive
-- one, and the loader's own comment claims otherwise: `importer/dna.py:207-208` says an unadopted
-- term is "a constraint violation mid-transaction rather than a row", and `dna_alias` has no
-- foreign key from `term` to `dna_term` at all (0004_dna.sql:42-47 keys only `version`). Do not
-- trust the comment; check the row.
ALTER TABLE dna_alias ADD COLUMN kind text;
