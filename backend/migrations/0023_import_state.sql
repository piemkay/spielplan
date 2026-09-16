-- 0023_import_state — one rule for deleting an artifact_bundle row, and a placement that cannot
-- claim a coordinate it has no basis for. Spec v2.1 §4.2 (artifact_bundle), §10 (the swap
-- sequence and its active-version invariant), §12 M2 ("every owned title has a coordinate");
-- decisions 249 and 250; docs/milestones/M4.14-plan.md §5.
--
-- NUMBERING, because this file's number is not the one its plan names. 0023 is what
-- `docs/milestones/ROADMAP-to-M5.md`'s migration ledger allocates to M4.14, and decision 250
-- takes it: the plan says "write this file as 0016_*.sql, assign its real number at merge",
-- which was true when the plan was written and is six migrations stale against the tree.
-- 0016-0018 and 0020-0022 are applied and sha256-checksummed — a mismatch is a hard startup
-- error — so none of them may be edited. 0019 was allocated to M4.10, which needed no schema,
-- and stays unused rather than being recycled, because the ledger maps a number to the milestone
-- that owns it; 0024 is reserved for M4.16. `db/migrate.py` keys `schema_migration` on the
-- filename stem and sorts a glob, so the gap costs nothing and renumbering later would not: a
-- file already applied under one stem silently re-runs under a new one and dies on its first
-- statement.
--
-- THE RULE THIS FILE ESTABLISHES, written here because a rule the schema enforces and no prose
-- states is a rule the next operator meets as an error message: **an artifact_bundle row is
-- provenance, and is deleted only while it is still `staged` or `failed` and nothing cites it.**
-- README's Backups section and `docs/TESTING.md` repeat it. 0015_seed.sql:131-135 calls itself
-- "the migration that makes them prunable" and the DDL gives three different answers about what
-- pruning would mean: `session` RESTRICTs even for an ended session, `title_prior` /
-- `user_score` / `title_placement` CASCADE, and `title.placement_bundle` and
-- `ledger_fit.bundle_version` SET NULL. Nothing prunes today — there is no `DELETE FROM
-- artifact_bundle` anywhere in the tree — so the cheapest honest rule is the one that matches
-- what the app actually does, and decision 249 takes it. `user_vector.bundle_version`'s SET
-- NULL (0015_seed.sql:143) stays the documented exception: §10 says a vector expressed in the
-- old basis is garbage and a NULL stamp is how every read already recognises that, while
-- `label_count` on the same row is vocabulary-independent and expensive to recover.
--
-- THE REPRODUCTION, because "three answers" understates what running one of them does. Deleting
-- the active row was refused by `session_bundle_version_fkey` both mid-session and after the
-- session had ended, which reads as a schema that already has an opinion — but it is an accident
-- of a session existing, and the refusal names a constraint rather than a rule. With the session
-- row removed the DELETE went through, took every `user_score` and `title_prior` for that basis
-- with it, and left a title at `placement = 'cold_tower', placement_bundle = NULL`: §12's M2
-- exit-criterion index then reported 0 owned titles waiting to be placed, for a title with no
-- coordinate at all. Nothing guarded the `state = 'active'` row either. Both halves are closed
-- below, and the second is the one reachable without any delete.

-- ---------------------------------------------------------------------------
-- 1. The row is provenance. Decision 249.
-- ---------------------------------------------------------------------------
-- A trigger rather than a revoked privilege or an application rule, for the reason
-- `artifact_bundle_one_seed` is an index rather than a check in `import_bundle`: the rule is
-- about the whole table's history and has to survive a restart, a concurrent import and a
-- developer with psql. `validated`, `active` and `superseded` each name a row that a placement,
-- a score, a prior, a fit or an ended session is entitled to cite as the basis it was computed
-- in, so the rule refuses on all three whatever else is true.
--
-- `'staged'` and `'failed'` are the two decision 249 leaves deletable, because they name an
-- import which never became anybody's basis. Nothing in this tree writes either: the importer
-- inserts `validated` inside the transaction that flips (decision 253) and a failed import rolls
-- that row back with everything else, so the hatch is for a row a psql operator made by hand and
-- for a state a later milestone may start writing — not for anything this app produces. `staged`
-- survives as the column default (0001_system.sql:40) and no INSERT in the tree omits `state`.
--
-- Which is why the hatch asks the question instead of assuming the answer. "Nothing downstream
-- can be pointing at them" is a claim about the rows this app makes, and a hand-built row
-- falsifies it: measured on a fresh 0001-0023 database, a title at `placement_bundle` of a
-- `failed` row made the DELETE reach `title`'s SET NULL and come back as
-- `title_placement_has_basis` with the whole title row in the DETAIL, and a session citing a
-- `staged` row made it come back as `session_bundle_version_fkey` — the two error shapes the
-- reproduction above names as the defect, on the only two states the rule leaves open. The six
-- tables below are the six the header enumerates as giving three different answers, and they are
-- what the message below means by a placement, a score, a prior, a fit or a session — a
-- placement being both halves, `title` and `title_placement`. `user_vector` is absent
-- deliberately: §10's documented exception (0015_seed.sql:138-143) says a vector in the old
-- basis is garbage and a NULL stamp is how every read recognises that, so a vector is not
-- something that keeps a row alive.
--
-- In the function and not in the trigger's WHEN clause because a WHEN clause may not contain a
-- subquery ("cannot use subquery in trigger WHEN condition"), and a WHEN that reads only
-- OLD.state is precisely the assumption being replaced.
--
-- The message names the version and the state rather than the constraint, because the operator
-- who reaches this is holding a psql prompt and a reason, and "violates foreign key constraint
-- session_bundle_version_fkey" is what they get today — which says nothing about why the row
-- exists.
CREATE FUNCTION artifact_bundle_is_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('staged', 'failed') AND NOT EXISTS (
        SELECT 1 FROM title           WHERE placement_bundle = OLD.version
        UNION ALL
        SELECT 1 FROM title_placement WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM user_score      WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM title_prior     WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM ledger_fit      WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM session         WHERE bundle_version = OLD.version
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'artifact_bundle row % is state % and is provenance, not garbage: it is the basis a '
        'placement, a score, a prior, a fit or a session was computed in (decision 249)',
        OLD.version, OLD.state
        USING HINT = 'Only a staged or failed row that nothing still cites may be deleted. '
                     'Supersede it instead.';
END $$;

CREATE TRIGGER artifact_bundle_no_delete
    BEFORE DELETE ON artifact_bundle
    FOR EACH ROW EXECUTE FUNCTION artifact_bundle_is_provenance();

-- ---------------------------------------------------------------------------
-- 2. A placed title states the basis it was placed in. Decision 249.
-- ---------------------------------------------------------------------------
-- The hole the reproduction fell through, and it is reachable without deleting anything: the
-- SET NULL on `title.placement_bundle` (0008_placement.sql:59-60) leaves `title.placement` at
-- 'warm' or 'cold_tower' with no version naming the basis. `title_unplaced_owned`
-- (0008_placement.sql:64) is §12's M2 exit-criterion index —
-- `SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'`, which must be 0 — so a
-- title with no coordinate at all is counted as placed, and `reconcile.py`'s sweep only resets
-- rows it can SEE are stale ('warm' with no Backbone row). A 'cold_tower' row in that state is
-- never re-examined by anything.
--
-- The backfill runs first, or the constraint cannot apply to an install that has already been
-- through §10's re-import: the same ordering `0015_seed.sql:110-116` argues for, and for the
-- same reason — a migration that fails at boot inside `db/migrate.py` has no way forward,
-- because this file is checksummed the moment it lands.
UPDATE title SET placement = 'unplaced', placement_at = NULL
 WHERE placement_bundle IS NULL AND placement <> 'unplaced';

-- The biconditional rather than two one-way checks, which is the idiom
-- `connector_secret_has_key` (0001_system.sql:28-29) already uses on this schema: it forbids
-- both halves of the contradiction at once — a placed title with no basis, and an unplaced title
-- still naming one — in a form a reader can check by eye.
--
-- `placement/reconcile.py` already writes the pair together in all three of its statements
-- (`:152`, `:160`, `:335`), so this constrains no production path; what it constrains is a
-- fixture that stamps `placement` and nothing else, which is precisely the state the
-- reproduction produced. `backup/movie_data.py`'s DROPPED_COLUMNS already drops
-- `placement_bundle`, `placement` and `placement_at` on restore (M4.7, data-12), so a restored
-- archive satisfies this by arriving with every title unplaced.
ALTER TABLE title ADD CONSTRAINT title_placement_has_basis
    CHECK ((placement = 'unplaced') = (placement_bundle IS NULL));
