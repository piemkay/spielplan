-- 0016_users — two roles, and a password lockout beside the PIN's.
-- Spec v2.1 §3.1 (as amended), §3.2, §4.2 app_user; decision 166 (docs/spec-v2.2-proposals.md);
-- docs/milestones/M4.6-plan.md §4.
--
-- Both changes belong to columns and constraints 0002_users.sql created, and that file is
-- applied and sha256-checksummed — a mismatch is a hard startup error — so they land here as
-- new statements rather than as edits there.

-- ---------------------------------------------------------------------------
-- 1. Two roles, and no others. Decision 166.
-- ---------------------------------------------------------------------------
-- §3.1 as amended: a guest is a Tonight session seat with `user_id NULL` (0013_tonight.sql:76
-- and its `session_participant_guest_has_no_user` CHECK at :91-92), never an account. Today
-- `role = 'guest'` mints a full, sign-in-capable, member-equivalent account: it is issued a
-- one-time password, it clears the first-login lock, it reads the household roster and it can
-- PIN-switch — and no dependency, query or route anywhere consults the role at all.
--
-- The assertion is louder than the bare ADD CONSTRAINT, which would also refuse to apply but
-- with a message naming a constraint rather than the rows the operator has to decide about.
-- No conversion rule is invented here (disable them? re-role them?): that is a data decision
-- the review does not settle, and a fresh install has no such rows because the only UI that
-- ever created an account sent 'member'.
DO $$
DECLARE
    offenders text;
BEGIN
    SELECT string_agg(id::text, ', ' ORDER BY id) INTO offenders
      FROM app_user WHERE role = 'guest';
    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION
            'decision 166 leaves two roles, but app_user still holds guest accounts: %',
            offenders
            USING HINT = 'Re-role or delete those accounts by hand, then re-run the migration.';
    END IF;
END $$;

ALTER TABLE app_user DROP CONSTRAINT app_user_role_check;
ALTER TABLE app_user ADD CONSTRAINT app_user_role_check CHECK (role IN ('admin', 'member'));

-- ---------------------------------------------------------------------------
-- 2. A lockout for the credential §3.2 keeps always available.
-- ---------------------------------------------------------------------------
-- §3.2 makes the argon2 password the always-available fallback, and it is the one credential
-- with no throttle: nothing counts a failure, so the 10^4 keyspace the PIN's lockout defends
-- is guarded while an unbounded password guess is not, and every attempt costs 64 MiB of
-- argon2 besides.
--
-- The pair mirrors `pin_failed_count` / `pin_locked_until` (0002_users.sql:17-18) rather than
-- inventing a second scheme, because the PIN's design comment (core/auth.py:128-132) already
-- carries the reason the counter is incremented in SQL: a read-modify-write lets ten
-- concurrent guesses cost one failure. One escalation curve, written once, is also one place
-- for the bound that keeps it from overflowing.
ALTER TABLE app_user
    ADD COLUMN password_failed_count integer NOT NULL DEFAULT 0,
    ADD COLUMN password_locked_until timestamptz;
