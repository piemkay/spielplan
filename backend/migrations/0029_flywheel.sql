-- 0029_flywheel - the extraction flywheel's batch and its one producer's key, the board's fourth
-- state, and the third curated ledger's author.
-- Spec v2.1 §8.4 ("Admin reviews the queue, picks a batch and providers, sees the cost estimate,
-- launches"), §6.6 Data (the stage board, the extraction queue, the three ledger editors), §8
-- ("Failure at any stage parks the job with a reason, retryable from admin"), all as amended by
-- v2.1.3; decisions 326, 328, 329, 336, 342, 423, 440, 441, 442, 443 and 444;
-- docs/milestones/M5.6-plan.md §5.
--
-- NUMBERING. 0029 is what docs/milestones/ROADMAP-M5.md's migration ledger allocates to M5.6.
-- 0024-0028 are M5.1's to M5.5's and are APPLIED, so each is checksummed and unreachable
-- (`db/migrate.py` raises on a mismatch at startup); 0019 stays permanently unused. M5.7 takes
-- none. `db/migrate.py` keys `schema_migration` on the filename stem and sorts a glob, so a gap
-- costs nothing and a renumber would cost everything.
--
-- NOTHING HERE CAN REFUSE A ROW THAT ALREADY EXISTS, and each section says why for its own table.
-- That is the property an ALTER on a durable install has to have: a migration that raises at boot
-- over a row the household already holds is an app that does not start.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO. It stores no cost on `flywheel_item`: the column
-- `est_cost_usd` stays and stays unwritten, because a figure stored at enqueue is stale the moment
-- the admin changes the pass count (plan C6), and the batch records its own figures at launch
-- (decision 441). It gives the board no action table: retry, retry from stage N and abandon are
-- writes to rows that exist, and what each state admits is a rule in the domain module rather than
-- a row a stale migration could disagree with (decision 444). It adds no `absent_term` kind, because
-- a naming failure whose term the vocabulary lacks is not enqueued at all (decision 344). It builds
-- no index for the admin queue's read, which is by status and newest first: `0004_dna.sql` built
-- `flywheel_status ON flywheel_item (status, created_at DESC)` beside the table, and a second copy
-- would be written on every enqueue and read by nothing. And it carries no weight of any kind: a
-- batch is a bill and a plan, and the thin-facet test counts distinct terms per facet and reads no
-- weight (decision 329).

-- ---------------------------------------------------------------------------
-- 1. §8.4's third feed is struck. Decision 328.
-- ---------------------------------------------------------------------------
-- `0004_dna.sql` froze four kinds in its CHECK, one per feed §8.4 then listed, and the third -
-- "titles whose 'unnamed taste' residual share is high" - is defined nowhere this repository can
-- read: the phrase occurs once in the normative file, in §8.4 itself, and the vendored
-- `ARCHITECTURE-extracts.md` carries §3 and Appendix C only (decision 294), neither of which has a
-- metric for nameable variance. So the feed is struck from §8.4 in v2.1.3 and the kind from this
-- CHECK, rather than kept as a value a later milestone could fill with a threshold nobody agreed.
--
-- NARROWING IS SAFE BECAUSE NOTHING HAS EVER WRITTEN THIS TABLE. `flywheel_item` has had no INSERT
-- anywhere in the tree since 0004 created it (plan §2.2), so no stored row can carry the struck
-- value. The DROP/ADD pair keeps the constraint's name, `flywheel_item_kind_check`, which is the
-- name Postgres gave 0004's column CHECK, so the catalogue reads the same on an install that
-- applied this file and on one built from scratch.
ALTER TABLE flywheel_item DROP CONSTRAINT flywheel_item_kind_check;
ALTER TABLE flywheel_item ADD CONSTRAINT flywheel_item_kind_check
    CHECK (kind IN ('empty_predicate', 'uncovered_frontier', 'thin_facet'));

-- ---------------------------------------------------------------------------
-- 2. The batch an admin launches. Decisions 441, 442 and 443.
-- ---------------------------------------------------------------------------
-- Plan C5: "A launched batch targets exactly the selected rows and no others", which needs the
-- batch to have an identity. Plan §5 prefers a table and a column to a batch id inside
-- `flywheel_item.detail`, because a jsonb key is a join nobody can index or constrain; this is
-- that preference.
--
-- `providers` AND `passes` ARE THE BATCH'S PLAN, recorded as launched. The same two values travel
-- in the launched tasks' payload, where the spend gate, stage 6 and the board's retry pre-check all
-- read them (decision 442); the row here is what the admin chose and what it was priced at, kept
-- after the tasks are done. The provider CHECK is `llm_call.provider`'s set from 0028, for 0028's
-- reason: a provider this build does not call is a plan no stage can run.
--
-- THREE FIGURES, AND THE RESERVATION IS THE ONE LAUNCH WAS HELD AGAINST. `est_titles` is the sum
-- over the selected rows; `est_cost_usd` is that many titles at the per-title estimate for these
-- providers and passes; `reserved_usd` is that total at both attempts, because decision 325
-- budgets the retry inside the cap and a total compared over one attempt would enable a batch the
-- gate then parks half of (decision 441). The table CHECK says only what is true by construction -
-- the reservation is never less than the total it doubles - so a row written by any other
-- arithmetic is refused rather than recorded. numeric and not a float, at `llm_call.usd`'s
-- precision, for 0028's reason: the cap is compared against sums of these, and a float that
-- rounds is a cap that drifts. `est_titles` is a count and refuses only what no count can be; an
-- empty or unmatched selection is the launch's refusal to make (decision 443), where it can answer
-- with a sentence rather than a constraint error.
--
-- TWO TIMESTAMPS, AND ONLY ONE OF THEM HAS A DEFAULT. `created_at` is the stamp every table in this
-- schema carries and plan §5's shape names. `launched_at` is the launch's own record: decision
-- 443's first step writes it beside the figures, in the transaction that creates the row. Neither
-- it nor the two money columns is defaulted, because a default would make any insert read as
-- launched and priced; a row that did not come through the launch says so by its NULLs rather than
-- by a time and a bill nobody chose. A batch is created by the launch that runs it, so there is no
-- drafted-but-unlaunched state for a row to wait in, and `approved` on `flywheel_item` stays unused
-- for that reason (decision 443).
CREATE TABLE flywheel_batch (
    id            bigserial      PRIMARY KEY,
    created_at    timestamptz    NOT NULL DEFAULT now(),
    launched_at   timestamptz,
    providers     text[]         NOT NULL
                  CHECK (cardinality(providers) >= 1
                         AND providers <@ ARRAY['anthropic', 'openai', 'gemini']::text[]),
    passes        integer        NOT NULL CHECK (passes >= 1),
    est_titles    integer        NOT NULL CHECK (est_titles >= 0),
    est_cost_usd  numeric(12, 6) CHECK (est_cost_usd >= 0),
    reserved_usd  numeric(12, 6),
    CONSTRAINT flywheel_batch_reserves_at_least_its_total CHECK (reserved_usd >= est_cost_usd)
);

-- ---------------------------------------------------------------------------
-- 3. Which title a row names, and which batch is running it. Decisions 329, 440 and 443.
-- ---------------------------------------------------------------------------
-- `title_id` IS A COLUMN AND NOT A KEY IN `detail`, because the one invariant the feed needs -
-- one open thin-facet row per title, section 4 - is an index over it. Nullable, because the two
-- kinds M6 produces name a query and a frontier rather than a title; the CHECK makes the thin-facet
-- kind name one, since a thin-facet row with no title is a row no launch can act on. ON DELETE
-- CASCADE for `acquisition_job.title_id`'s reason: a row about a title the install no longer holds
-- describes nothing.
--
-- `batch_id` IS SET BY THE LAUNCH THAT MARKS THE ROW `running`, and the CHECK ties the two: a row
-- is running only inside a batch, so "exactly the selected rows and no others" is readable off the
-- table afterwards as well as asserted by the launch at the time. No ON DELETE action: nothing
-- deletes a batch, and a delete that orphaned its rows would erase what they were launched under.
--
-- BOTH CHECKS ARE SAFE ON EVERY INSTALL for section 1's reason - the table holds no row - and both
-- columns are nullable, so the ALTER has nothing to backfill.
ALTER TABLE flywheel_item
    ADD COLUMN title_id integer REFERENCES title(id) ON DELETE CASCADE,
    ADD COLUMN batch_id bigint REFERENCES flywheel_batch(id),
    ADD CONSTRAINT flywheel_item_thin_facet_names_a_title
        CHECK (kind <> 'thin_facet' OR title_id IS NOT NULL),
    ADD CONSTRAINT flywheel_item_running_names_its_batch
        CHECK (status <> 'running' OR batch_id IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 4. One open thin-facet row per title. Decision 440.
-- ---------------------------------------------------------------------------
-- The driver observes a title each time its walk finishes stage 8, and a title walks more than
-- once - a board retry, a launched batch, a re-add. Each observation closes the title's running
-- rows, then appends a queued row or refreshes the open one, so the queue holds one row per title
-- however often it is observed. This index is what makes that true under two walks at once rather
-- than true of the code that is supposed to keep it: a second open row is a write Postgres refuses.
--
-- "OPEN" IS EVERY STATUS THAT IS NOT CLOSED - queued, approved, running - and not only `queued`.
-- A title whose batch is running has its open row already, and a second, queued row beside it
-- would let a second launch select the same title while the first one's walk is still under way.
-- `approved` is unused (decision 443) and is in the set so that the day a milestone uses it, it
-- cannot open a second row by the back door.
CREATE UNIQUE INDEX flywheel_item_one_open_thin_facet ON flywheel_item (title_id)
    WHERE kind = 'thin_facet' AND status IN ('queued', 'approved', 'running');

-- ---------------------------------------------------------------------------
-- 5. The board's fourth outcome. Decisions 336 and 444.
-- ---------------------------------------------------------------------------
-- Decision 336 made `parked` "waiting on something that may change" and `failed` "this stage
-- raised and will raise again", and proposal 109, adopted under decision 330, gives the admin
-- abandon. An abandoned job is neither: nothing is waiting and nothing raised - an operator decided
-- to stop - so writing it as `failed` would tell the next reader a healthy title is broken, which
-- is the confusion decision 336 was taken to end, and writing it as `parked` would promise a
-- resume nobody asked for. `placement/reconcile.py`'s thin-but-placed titles are the case that
-- makes this concrete: parked at stage 2 and ready on every surface, and abandoned from the board
-- they must still not read as failures.
--
-- WIDENING ADMITS EVERY ROW THE OLD CHECK ADMITTED, so no stored board row is refused. The name is
-- the one Postgres gave 0005's column CHECK, `acquisition_job_status_check`, kept for section 1's
-- reason.
ALTER TABLE acquisition_job DROP CONSTRAINT acquisition_job_status_check;
ALTER TABLE acquisition_job ADD CONSTRAINT acquisition_job_status_check
    CHECK (status IN ('queued', 'running', 'parked', 'ready', 'failed', 'abandoned'));

-- ---------------------------------------------------------------------------
-- 6. The third curated ledger's author. Decisions 326, 342 and 423.
-- ---------------------------------------------------------------------------
-- §6.4 makes the per-facet axis files "Editable in the §6.6 ledger editor", and decision 342 ships
-- that editor: it writes `dna_axis` and its `dna_axis_weight` rows for the active vocabulary as
-- the household's. Decision 326 gave the two other curated ledgers exactly this column in 0026, so
-- that the importer's reload could tell the rows it owns from the rows it must not touch, and the
-- axis is the same question: `importer/dna.py`'s axis loader replaces a facet's rows from the
-- bundle's file, and without this column it would overwrite a household axis on the next
-- models-only import - the collision decision 171's Cost paragraph recorded for corrections, one
-- ledger over. The loader leaves a household facet in place, which is decision 423's rule: the
-- household's curated row takes effect.
--
-- ON THE AXIS AND NOT ON ITS TERMS. `dna_axis_weight` references `dna_axis` by (version, facet)
-- with ON DELETE CASCADE, so a facet's terms belong to exactly one axis row and that row's author
-- is theirs; a second column there could disagree with the first and nothing would say which won.
--
-- 0026's vocabulary and 0026's DEFAULT, for 0026's reasons: 'household' is the only author the
-- editors write, and every axis already stored came from a bundle - on this repository's own
-- bundle there is none (decision 173) - so the backfill is free. `backup/movie_data.py` archives
-- `dna_axis`, and `restore_archive` COPYs with the column list the archive carries, so an archive
-- written before this file restores with `origin` taking its default.
ALTER TABLE dna_axis ADD COLUMN origin text NOT NULL DEFAULT 'bundle'
    CHECK (origin IN ('bundle', 'household'));
