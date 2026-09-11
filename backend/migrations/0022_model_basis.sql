-- 0022_model_basis — the structural sweep. Spec v2.1 §4.2 (user state), §4.1 rule 5 (the kind
-- partition), §10 ("Ledger observations always survive re-import"); decision 239;
-- docs/milestones/M4.13-plan.md §5.
--
-- Six repairs, and every one of them turns a rule the code already relies on into a rule the
-- database holds. That is the through-line: M4.13 is the milestone about a fit knowing the basis
-- it was computed in, and four of these are the schema half of that — the K a tier edit was
-- written under, the kind a score was computed for, the shape a cutpoint list is allowed to have,
-- and the observations a re-import is not allowed to lose.
--
-- What is deliberately NOT here, so a later reader does not go looking:
--   * no `retracted_at` on verdict/duel/tier_edit. Decision 237 executes decision 174: Undo
--     hard-DELETEs the row it compensates, §4.2 now says so, and a tombstone would need a
--     `retracted_at IS NULL` predicate at 29 read sites across 8 modules.
--   * no fourth `title.placement` state. Decision 238 leaves 'blended' to the milestone that owns
--     §8 stage 10's badge input and §6.6's counters; `0003_content.sql`'s CHECK is untouched.
--   * no `blend_beta` rewrite and no widening of `0009_scoring.sql`'s CHECK. Decision 234: β stays
--     the personal weight (decision 167), so the second migration this milestone's plan sketched
--     is cancelled and M4.13 takes exactly one number.
--   * no cross-schema FK from `display.platform_rating` to `title`. `0003:177-184` argues against
--     it; decision 239 puts the reaping in `importer/load.py`'s reload path instead, which is the
--     one place that can see a title id disappear.
--
-- A new numbered file, as always: 0001-0020 are sha256-checksummed from their first apply and a
-- mismatch is a hard startup error. 0019 was allocated to M4.10 and stayed unused because that
-- milestone needed no schema; 0021 belongs to M4.12, which is being built in parallel in another
-- worktree. The runner sorts a glob, so the gap costs nothing and the ledger keeps mapping a
-- number to the milestone that owns it.
--
-- CORRECTED IN PLACE AFTER FIRST DRAFTING, announced here so no reader assumes otherwise.
-- Decision 245 allows it for as long as this file is untracked and has been applied to no durable
-- database, and it is a permission to correct rather than a ration of one: what the decision fixes
-- is that M4.13 takes exactly one migration NUMBER under any finding. Both corrections are prose
-- only and not one DDL statement changed; `test_migrations.py`, `test_schema_contracts.py` and the
-- PGlite schema tests were re-run immediately after each.
--   1. Section 2's count read "Eight observation tables and §13's outcome row", i.e. nine, where
--      the ALTERs below cover ten columns over EIGHT tables and `session_outcome` is one of the
--      eight. [M4.13 cycle 1, M413-R4]
--   2. The sentence that correction displaced was left unwrapped and ran to 159 characters -- the
--      only line over 108 in any migration in this repo, in the block whose header says it was
--      corrected. Re-wrapped, same words. [M4.13 cycle 2, M413-C2-DIM7-03]
--
-- Every constraint name dropped below was read off a database migrated to 0020 rather than
-- guessed from Postgres's `<table>_<column>_fkey` convention (a wrong DROP CONSTRAINT fails the
-- whole file at startup, which is a boot the operator cannot get past). One of them is not what
-- the convention would predict: `cutpoints_length` is named in `0005_ledger.sql:97-99`.
--
-- Every literal in every CHECK below is either text, already spelled `::text`, or an exact integer
-- against an integer column. That is not an accident and it is why none of them carries a float
-- cast: `0009_scoring.sql:56-65` records what happens when a CHECK compares a `real` column with
-- an uncast decimal — `float4(0.8)` widens to 0.800000011920929, so the ceiling rejected the one
-- value §5.1 had measured and a nightly fold-in failed its INSERT inside a background job.
-- `seat >= 1::smallint` is spelled out for that convention's sake even though int2-against-int4
-- cannot lose a value. Nothing here refuses a number a scheduled job can produce.

-- ---------------------------------------------------------------------------
-- 1. A tier edit records the K it was written under.
-- ---------------------------------------------------------------------------
-- Decision 11 keeps `tier_edit` rows across a tier-set change — the edit is an observation, and an
-- observation does not stop being true because the board was re-labelled. But nothing recorded
-- which board it was made on, so every reader took the raw index: a drop into tier 6 of 7 read as
-- tier 6 of 12 after the set grew, which left every loved title mid-board, emptied the top five
-- tiers, and put 36 of 60 later drops into tension. Simulated meaning shift 20-28 percentile
-- points, against 3.0 for a mass-preserving rescale.
--
-- `tier_edit` has no `kind` of its own, and `ledger_cutpoints` is keyed `(user_id, kind)`, so the
-- backfill joins through the title: the board a drop was made on is the board for that title's
-- kind, and there is no other honest reading. 7 where the person has no cutpoints row yet, which
-- is §4.2's default set (F/D/C/B/A/A+/S).
--
-- Nullable, and deliberately so: a NOT NULL with a default would let a writer that forgets the
-- value record a 7 that looks like a measurement, where a NULL is a row whose board is genuinely
-- unknown and can be read as one.
--
-- Pre-release rows written BEFORE an earlier K change are backfilled with the person's CURRENT K
-- and are therefore mislabelled. That is stated rather than papered over: there are no real
-- households yet, this is the only information the database holds, and a migration nobody can
-- edit later is the wrong place for a fiction.
ALTER TABLE tier_edit ADD COLUMN n_levels smallint;

UPDATE tier_edit e
   SET n_levels = COALESCE((
           SELECT cardinality(c.tier_set)
             FROM ledger_cutpoints c
             JOIN title t ON t.id = e.title_id
            WHERE c.user_id = e.user_id
              AND c.kind = t.kind
       ), 7);

-- ---------------------------------------------------------------------------
-- 2. Observations refuse a title delete.
-- ---------------------------------------------------------------------------
-- §10: "Ledger observations always survive re-import." Seven observation tables and §13's outcome
-- row -- eight tables and ten foreign-key columns, re-declared below -- carried ON DELETE CASCADE,
-- so that guarantee was kept only by the convention that no code deletes a title. The first admin
-- action that removes a mis-matched or rolled-back acquisition would erase every member's verdicts
-- on it with no error and no log line — and taste data is the one thing this app cannot
-- re-derive. RESTRICT makes the database say no.
--
-- The derived tables stay on CASCADE on purpose: `ledger_state`, `user_score`, `title_prior`,
-- `title_placement` and `acquisition_job` are recomputed outputs, and a delete that had to be
-- hand-cleared of them before it could proceed would be an obstruction, not a guarantee. The line
-- between the two lists is exactly "was this asserted by a person".
ALTER TABLE verdict DROP CONSTRAINT verdict_title_id_fkey;
ALTER TABLE verdict ADD CONSTRAINT verdict_title_id_fkey
    FOREIGN KEY (title_id) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE duel DROP CONSTRAINT duel_title_a_fkey;
ALTER TABLE duel ADD CONSTRAINT duel_title_a_fkey
    FOREIGN KEY (title_a) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE duel DROP CONSTRAINT duel_title_b_fkey;
ALTER TABLE duel ADD CONSTRAINT duel_title_b_fkey
    FOREIGN KEY (title_b) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE tier_edit DROP CONSTRAINT tier_edit_title_id_fkey;
ALTER TABLE tier_edit ADD CONSTRAINT tier_edit_title_id_fkey
    FOREIGN KEY (title_id) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE user_title DROP CONSTRAINT user_title_title_id_fkey;
ALTER TABLE user_title ADD CONSTRAINT user_title_title_id_fkey
    FOREIGN KEY (title_id) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE session_answer DROP CONSTRAINT session_answer_title_a_fkey;
ALTER TABLE session_answer ADD CONSTRAINT session_answer_title_a_fkey
    FOREIGN KEY (title_a) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE session_answer DROP CONSTRAINT session_answer_title_b_fkey;
ALTER TABLE session_answer ADD CONSTRAINT session_answer_title_b_fkey
    FOREIGN KEY (title_b) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE session_ballot DROP CONSTRAINT session_ballot_title_id_fkey;
ALTER TABLE session_ballot ADD CONSTRAINT session_ballot_title_id_fkey
    FOREIGN KEY (title_id) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE session_result DROP CONSTRAINT session_result_title_id_fkey;
ALTER TABLE session_result ADD CONSTRAINT session_result_title_id_fkey
    FOREIGN KEY (title_id) REFERENCES title(id) ON DELETE RESTRICT;

ALTER TABLE session_outcome DROP CONSTRAINT session_outcome_chosen_title_id_fkey;
ALTER TABLE session_outcome ADD CONSTRAINT session_outcome_chosen_title_id_fkey
    FOREIGN KEY (chosen_title_id) REFERENCES title(id) ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- 3. The indexes the RESTRICT check will scan.
-- ---------------------------------------------------------------------------
-- A RESTRICT check is one `SELECT 1 FROM <referencing table> WHERE <column> = $1 FOR KEY SHARE`
-- per referencing table per deleted row. `pg_indexes` was read against a database migrated to
-- 0020 and not one of these ten columns leads an index: `verdict_user_title`, `verdict_live`,
-- `duel_user_a`, `duel_user_b`, `tier_edit_user`, `user_title_pkey`, `session_answer_live`,
-- `session_ballot_one_per_title`, `session_result_pkey` and `session_outcome_pkey` all lead with
-- a user, session or participant id. So the plan's guess that verdict and duel "may already be
-- covered" is wrong, and all ten get one — an incomplete set would leave the largest observation
-- table as the single sequential scan in a set built to avoid them.
CREATE INDEX verdict_title_id ON verdict (title_id);
CREATE INDEX duel_title_a ON duel (title_a);
CREATE INDEX duel_title_b ON duel (title_b);
CREATE INDEX tier_edit_title_id ON tier_edit (title_id);
CREATE INDEX user_title_title_id ON user_title (title_id);
CREATE INDEX session_answer_title_a ON session_answer (title_a);
CREATE INDEX session_answer_title_b ON session_answer (title_b);
CREATE INDEX session_ballot_title_id ON session_ballot (title_id);
CREATE INDEX session_result_title_id ON session_result (title_id);
CREATE INDEX session_outcome_chosen_title_id ON session_outcome (chosen_title_id);

-- ---------------------------------------------------------------------------
-- 4. `ledger_cutpoints` says what §4.2 says.
-- ---------------------------------------------------------------------------
-- §4.2: "learned tier cutpoints; length = |tier set| - 1, ordered ascending". The shipped CHECK
-- held neither half. `array_length('{}', 1)` is NULL, not 0, so `cutpoints_length` evaluated to
-- NULL — which a CHECK accepts — for exactly the degenerate rows it exists to refuse: an empty
-- tier set with empty boundaries, and a one-level set. `cardinality()` returns 0 for an empty
-- array and has no such hole. The `>= 2` floor is the same one `rank/tiers.py`'s MIN_TIERS
-- already enforces on the write side, so no path the app has can produce a row this refuses.
ALTER TABLE ledger_cutpoints DROP CONSTRAINT cutpoints_length;
ALTER TABLE ledger_cutpoints ADD CONSTRAINT cutpoints_length
    CHECK (cardinality(tier_set) >= 2
           AND cardinality(boundaries) = cardinality(tier_set) - 1);

-- "Ordered ascending" had no constraint at all. It needs a function because a CHECK cannot carry
-- a subquery, and the comparison must ACCEPT equal neighbours: the ordered logit's cone is closed
-- and coincident cutpoints are the honest answer for a person whose whole board sits on one value
-- (`rank/tiers.py:105-108` refuses to nudge them apart, `ledger/model.py:151-155` admits the
-- state). So this is "sorted ascending", not "strictly increasing", and a fit that produces two
-- equal cuts still commits — never write a CHECK the nightly job can violate.
--
-- `array_agg` over an empty array returns NULL, so this constraint is NULL-permissive for exactly
-- one input: `boundaries = '{}'`. That is the hole the rewritten `cutpoints_length` above closes
-- from the other side — an empty boundary array needs a one-level tier set, and a one-level tier
-- set is now refused — and the pair is said out loud because leaving the NULL-permissiveness this
-- section exists to remove unremarked is how the first version of it survived six releases.
CREATE FUNCTION boundaries_ascend(cuts double precision[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $fn$ SELECT cuts = (SELECT array_agg(c ORDER BY c) FROM unnest(cuts) AS c) $fn$;

ALTER TABLE ledger_cutpoints ADD CONSTRAINT cutpoints_ascend
    CHECK (boundaries_ascend(boundaries));

-- ---------------------------------------------------------------------------
-- 5. Three CHECKs that mean their names.
-- ---------------------------------------------------------------------------
-- `rate_session_kinds_valid` reads `kinds <@ ARRAY['movie','series']`, which is containment and
-- not membership: `['movie','movie']` is a subset of that array and passed, giving a session two
-- of one kind and none of the other. Enumerating the three legal values is also the only form a
-- CHECK can take (a subquery is not allowed), and it pins `normalise_kinds`' canonical order —
-- `db/library.py:61-70` builds its answer by filtering KINDS = ('movie', 'series'), so every
-- writer produces that order and a row in the other one is a writer that bypassed the helper.
-- `rate_session_kinds_nonempty` and `rate_session_kinds_valid` stay: both are correct, both are
-- applied, and dropping a constraint this one subsumes buys nothing.
ALTER TABLE rate_session ADD CONSTRAINT rate_session_kinds_distinct
    CHECK (kinds = ARRAY['movie'::text]
        OR kinds = ARRAY['series'::text]
        OR kinds = ARRAY['movie'::text, 'series'::text]);

-- `seat` is the 1-based position §6.2 hands out and `session_participant_seat` is unique on
-- `(session_id, seat)`, so a seat of -3 was as unique as any other and the uniqueness read like a
-- validity check without being one.
ALTER TABLE session_participant ADD CONSTRAINT session_participant_seat_positive
    CHECK (seat >= 1::smallint);

-- A verdict that supersedes itself is a cycle of length one in the chain §4.2 makes append-only,
-- and `observations.py`'s walk of that chain has no terminating case for it. Same for a re-ask
-- that re-asks itself. Both columns are nullable, hence IS DISTINCT FROM rather than <>.
ALTER TABLE verdict ADD CONSTRAINT verdict_not_self
    CHECK (superseded_by IS DISTINCT FROM id AND reask_of IS DISTINCT FROM id);
ALTER TABLE duel ADD CONSTRAINT duel_not_self
    CHECK (reask_of IS DISTINCT FROM id);

-- ---------------------------------------------------------------------------
-- 6. The kind partition becomes a database fact.
-- ---------------------------------------------------------------------------
-- §4.1 rule 5 partitions the catalogue by kind, and `0009_scoring.sql:27-33`'s own comment says
-- the rule "lives in the data" — but `ledger_state.kind` and `user_score.kind` are denormalised
-- copies with nothing tying them to the title they name. A CHECK on the column's spelling is not
-- that: it accepts 'series' on a row whose title is a movie, which is a board silently mixing the
-- two partitions every read is written to keep apart.
--
-- The composite FK is one constraint doing the work of two — it holds the reference AND the
-- agreement — which is why the single-column FK is dropped rather than kept beside it. `title`
-- needs UNIQUE (id, kind) for it to point at; that index is redundant against the primary key and
-- is the price of the constraint, paid once on a table with one row per title.
--
-- ON UPDATE CASCADE is not decoration and it is not symmetry with the DELETE: `title.kind` is
-- mutable. §10's re-import upserts it (`load.py`'s `_upsert_titles` sets every mapped column from
-- EXCLUDED), so a corpus reclassification — miniseries to series — moves a title between
-- partitions. Under the default NO ACTION the import would fail on any reclassified title a
-- member has a board row for, which turns a schema repair into an outage of the one job §10
-- promises. CASCADE moves the derived row with its title, and the next refit — which a re-import
-- queues anyway — rewrites the value it carries.
--
-- The two DELETEs are not a cleanup this milestone wanted; they are what makes the ADD safe. An
-- ADD CONSTRAINT validates the rows already there, so one pre-existing cross-kind row would make
-- this file fail at startup with nothing an operator could edit. Both tables are derived —
-- `ledger_state` is the nightly MAP output and `user_score` the fold-in's, and the next refit
-- rewrites every row of both — so a row whose kind disagrees with its title's is corruption rule 5
-- forbids, and taking the boot down over rows the next tick would rewrite anyway is the worse of
-- the two failures.
DELETE FROM ledger_state ls USING title t WHERE t.id = ls.title_id AND t.kind <> ls.kind;
DELETE FROM user_score us USING title t WHERE t.id = us.title_id AND t.kind <> us.kind;

ALTER TABLE title ADD CONSTRAINT title_id_kind_key UNIQUE (id, kind);

ALTER TABLE ledger_state DROP CONSTRAINT ledger_state_title_id_fkey;
ALTER TABLE ledger_state ADD CONSTRAINT ledger_state_title_kind_fkey
    FOREIGN KEY (title_id, kind) REFERENCES title (id, kind)
    ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE user_score DROP CONSTRAINT user_score_title_id_fkey;
ALTER TABLE user_score ADD CONSTRAINT user_score_title_kind_fkey
    FOREIGN KEY (title_id, kind) REFERENCES title (id, kind)
    ON DELETE CASCADE ON UPDATE CASCADE;

-- `session.kind` is left alone: it is a property of the evening, not of a title, so there is
-- nothing for it to agree with.
