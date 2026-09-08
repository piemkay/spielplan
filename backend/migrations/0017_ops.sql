-- 0017_ops — the one DEK row, and a place for a job to say what happened.
-- Spec v2.1 §2 (Configuration, Backups), §6.6 System, §5.3; decision 181;
-- docs/milestones/M4.7-plan.md §4.
--
-- Two unrelated-looking statements in one file on purpose: §4 of the plan is one migration, and
-- a second numbered file for the second half would collide with whatever milestone takes 0018.

-- ---------------------------------------------------------------------------
-- 1. "The one DEK row" made true by the schema. sec-10.
-- ---------------------------------------------------------------------------
-- §2: "rotating SECRETS_KEY is an explicit admin action that re-wraps **the one DEK row**".
-- `core/secrets.ensure_dek` was SELECT-then-INSERT with no lock and 0001_system.sql carried no
-- constraint, so three concurrent first boots on separate connections produced two active rows.
-- Reads survived that (every ciphertext names its own key_id), but the rewrap and reset executors
-- this milestone adds both have to know which row they are acting on, and "the newest" is not an
-- answer an operator can check.
--
-- ((true)) is the same idiom as `artifact_bundle_one_active` (0001_system.sql:44-45): a constant
-- expression index whose WHERE clause is the real subject, so at most one row may have
-- retired_at IS NULL. Rotation is unaffected — `rewrap` re-wraps in place and never inserts, and
-- `reset` stamps retired_at before anything mints a replacement.
--
-- THIS STATEMENT CAN FAIL ON A REAL INSTALL, and that is the point: an install that already lost
-- the race carries two active rows and this index refuses to build, aborting the migration and so
-- the boot. The failure is a duplicate-key error naming this index. The repair is to decide which
-- row is current and stamp the other:
--     UPDATE data_encryption_key SET retired_at = now() WHERE key_id = '<the loser>';
-- Retiring is safe: `load_dek` finds a retired row by id, so every ciphertext that names the
-- loser still opens. Only new seals go to the survivor.
CREATE UNIQUE INDEX data_encryption_key_one_active
    ON data_encryption_key ((true)) WHERE retired_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Job outcomes, persisted. ops-11.
-- ---------------------------------------------------------------------------
-- §6.6 names "job health, queue depth, last syncs, backup status, logs" as the System card's
-- data, and §2 promises a nightly dump with rotation 14. Neither can be reported: `worker.py`'s
-- `last_run` is an in-process dict, and every report (`BackupReport`, `SyncReport`, `WatchReport`,
-- the refits) is logged and dropped. So a restart cannot know whether last night's dump happened,
-- and the nightly backup can fail for a month with no signal an operator ever meets.
--
-- `detail` is jsonb because the reports already produce `as_dict()`: the table stores what they
-- return rather than a schema that has to be widened for every new job. Nullable finished_at/ok
-- are the running state — the row is written before the job runs, so a job killed mid-flight
-- leaves the evidence that it started and never finished.
CREATE TABLE job_run (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    detail      jsonb
);

-- The only query the System card and the worker's own restart-seeding make: the newest rows for
-- one job name, newest first.
CREATE INDEX job_run_name_started ON job_run (name, started_at DESC);
