-- 0014_tonight_undo — the index that made §6's "undo everywhere" a one-way door.
-- Spec v2.1 §6 preamble, §6.2 step 4 (rewritten, 54c), §14 risk 6.
--
-- 0013 gave `session_answer` a NON-PARTIAL unique index on (participant_id, seq), one line
-- above a partial one, and the two are not interchangeable here.
--
-- §6's preamble puts undo on every surface and `play.retract` implements it the way §4.2's
-- tables demand — as a TOMBSTONE rather than a DELETE, because §14 risk 6 says log every vote
-- and a retraction is itself a fact. So after an undo the row for seq N is still there, and
-- `record_answer` then requires the replacement answer at `seq = answered_count + 1`, which is
-- N again. The insert collides with the tombstone, the route answers 500, and the participant's
-- round cannot advance — ever. One tap on Undo ends that person's evening, and because the
-- reveal waits for every seat (54e), it ends the household's.
--
-- The index is still needed: it is what makes a double-submit at one seq impossible even when
-- two taps race the counter. It only has to be scoped to the rows that are still live.
DROP INDEX session_answer_seq;
CREATE UNIQUE INDEX session_answer_seq ON session_answer (participant_id, seq)
    WHERE retracted_at IS NULL;
