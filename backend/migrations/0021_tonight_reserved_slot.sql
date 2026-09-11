-- 0021_tonight_reserved_slot — 54d's counterweight, labelled. Spec v2.1 §6.2 step 5 (rewritten,
-- 54d); decisions 220 and 221; docs/milestones/M4.12-plan.md §4.
--
-- 54d says the third finalist slot "is reserved for the highest-scoring title on the opposite pole
-- of the contested axis, **labelled as such**", and §6.2 step 5 prints "You're split on {facet} —
-- here's one of each" over the slate that carries it. Nothing labelled it: `combine.py` gives the
-- reserved title `SLOT_FINALIST` like the other two, and no string `reserved` or `opposite` exists
-- anywhere in the router, the domain package or the Tonight page — so the household is told "one of
-- each" and cannot see which card is the counterweight, which is the whole content of the clause.
--
-- Why a boolean and not a fourth `slot` value: 0013's CHECK is `slot IN ('finalist', 'wildcard',
-- 'runner_up')` and the reveal filters on `slot in ('finalist','wildcard')` in two places. A
-- `SLOT_RESERVED` constant would make every one of those filters a place to forget a finalist,
-- silently, in a branch that cannot execute on the shipped bundle (decision 173 ships no axes, so
-- `contested` is None on every real night and no row is written with `reserved = true` today).
-- Orthogonal to the slot is the safer shape: the reserved title is still a finalist, and this says
-- which one it is. Decision 220.
--
-- NOT NULL DEFAULT false because every existing row is a slate that had no reservation — either it
-- surfaced no split, or it surfaced one and the label did not exist to write. Neither is a
-- reservation, so false is the truth about the rows already stored, not a placeholder.
--
-- A new numbered file, as always: 0001-0020 are sha256-checksummed from their first apply and a
-- mismatch is a hard startup error. 0019 was allocated to M4.10 and stayed unused because that
-- milestone needed no schema; it stays unused, because the ledger maps a number to the milestone
-- that owns it. 0022 is M4.13's and is not taken here.

ALTER TABLE session_result
    ADD COLUMN reserved boolean NOT NULL DEFAULT false;

-- No index, and `reserved` is never a filter: the column is read with the row it labels.
-- `tonight/result.slate` selects the rows of one session whose slot is on the ballot, which 54d
-- fixes at four (three finalists and a wildcard), so there is nothing here to narrow. The table
-- itself is NOT four rows per session — `combine.sequence` is a permutation of the whole
-- candidate pool and `play.finish` writes a row per element, so §14 risk 6 can compare the slate
-- against what it beat; on the shipped 696-title owned pool that is 696 rows for one evening.
-- Said here because a migration's closing note is what the next person sizes a table from, and
-- this one first shipped saying the session's whole stored result was the slate.
-- [M4.12 review cycle 1: M412-RESULT-4]
