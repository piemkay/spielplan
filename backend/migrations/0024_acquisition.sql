-- 0024_acquisition — the acquisition spine's three tables: the durable (kind,key) queue, the
-- content-addressed raw store's index, and the polite fetcher's per-host memory.
-- Spec v2.1 §8 (the "Ported skeleton" preamble, the ten-stage pipeline, and "All fetched bytes
-- land in the app's own raw store, so re-parsing is free forever"), §5.3 (the worker's jobs),
-- §6.6 (the acquisition board and the Data tab), §1 (/data/raw); decisions 321, 322, 336, 340
-- and 345; docs/milestones/M5.1-plan.md §5.
--
-- NUMBERING. 0024 is what docs/milestones/ROADMAP-M5.md's migration ledger allocates to M5.1,
-- confirmed two ways: the directory holds 0016-0018 and 0020-0023 and no 0024, and
-- test_spec_coverage.py's own comment records why it was free — "0024 is not claimed (decision
-- 292 ...) and 0019 stays permanently unused". 0019 was allocated at M4.10, which needed no
-- schema, and must never be recycled; 0025-0029 are allocated on paper to M5.2 through M5.6 and
-- are not taken here. `db/migrate.py` keys `schema_migration` on the filename stem and sorts a
-- glob, so a gap costs nothing and a renumber would cost everything: a file already applied
-- under one stem silently re-runs under a new one and dies on its first statement.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not touch `acquisition_job`
-- (0005_ledger.sql), and that omission is decision 322 rather than an oversight. That table is
-- §6.6's per-title BOARD and §4.2 names it; this file adds the QUEUE beside it. Reshaping the
-- board would mean a create-copy-drop inside this migration, on a table whose ON DELETE CASCADE
-- to `title` is pinned by test_schema_contracts.py and whose rows a shipped writer has been
-- producing since M4.13 — `placement/reconcile.py`'s `_park_thin` parks thin-but-placed titles
-- at (stage = 2, status = 'parked') with ON CONFLICT (title_id) DO NOTHING. Those rows are the
-- pipeline's inbox, not a backlog of failures (decision 336), and nothing here disturbs them.
--
-- THREE TABLES IN ONE FILE, for 0017_ops.sql's reason: §8's preamble names the raw store, the
-- durable queue and the per-host HTTP layer as one skeleton, M5.1 builds all three, and a second
-- numbered file for the second half would collide with whatever milestone takes 0025.

-- ---------------------------------------------------------------------------
-- 1. The durable (kind,key) queue. Decision 322.
-- ---------------------------------------------------------------------------
-- The port of the corpus project's `task` table, whose own module states the constraint this
-- shape exists to satisfy: tasks in flight are "recovered by lease expiry, not by any shutdown
-- handler - which is the only way that actually survives `kill -9`", and "task identity is
-- (kind, key)", so enqueueing the same pair twice is a no-op. Both are properties of the SCHEMA
-- rather than of the code that reads it, which is why they are here: a UNIQUE that is enforced
-- by the writer is a UNIQUE that holds until two writers run.
--
-- `job_run` (0017_ops.sql) is not this and cannot be made into it. It is an outcome log — name,
-- started_at, finished_at, ok, detail — with no identity a second enqueue could collide with, no
-- attempts counter, no lease and no scheduled retry, and `worker.py`'s `_prune_job_runs` deletes
-- its rows on a retention rule. Work that survives a power cut cannot live in a table that is
-- pruned for being old.
--
-- NO FOREIGN KEY TO `title`, and that absence IS the decision. The task is keyed on the Jellyfin
-- item or the provider id, so it has to be able to hold work for a title that does not exist
-- yet: §8 stage 1 is what mints the row, `connectors/resolve.py` refuses to mint and says
-- acquiring genuinely new titles is §8's pipeline, and a task that could not be enqueued before
-- its title existed would have nothing to drive stage 1 with. A later agent must not "fix" this.
--
-- `paid` is a refusal rather than decoration. The corpus carries the same flag on every handler
-- spec because "at corpus scale the difference between them is roughly a hundred euros a click":
-- every crawl task is free and the LLM passes are not, so a generic drain that ignored the flag
-- would bill the household on its first tick after §8 stage 6 arrives at M5.5. It is false here
-- and stays false until the milestone that spends money sets it.
CREATE TABLE acquisition_task (
    id              bigserial PRIMARY KEY,
    kind            text        NOT NULL,
    key             text        NOT NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    priority        integer     NOT NULL DEFAULT 100,
    state           text        NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'leased', 'done', 'failed', 'skipped')),
    attempts        integer     NOT NULL DEFAULT 0,
    max_attempts    integer     NOT NULL DEFAULT 4,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_owner     text,
    lease_expires   timestamptz,
    last_error      text,
    result_note     text,
    paid            boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT acquisition_task_identity UNIQUE (kind, key),
    -- A leased row with no expiry is work nothing can ever reclaim: the reaper's whole mechanism
    -- is `lease_expires < now()`, so the one state that must never be half-written is this one.
    -- Stated in the direction that is certainly true rather than as a biconditional, because
    -- whether a completed row keeps the owner that completed it is the drain's business and not
    -- the schema's.
    CONSTRAINT acquisition_task_lease_is_reclaimable
        CHECK (state <> 'leased' OR (lease_owner IS NOT NULL AND lease_expires IS NOT NULL))
);

-- The lease query's index, in the order it reads: claimable rows first, then the ones whose time
-- has come, then priority. `FOR UPDATE SKIP LOCKED` over this is the same claim `worker.py`'s
-- `_claim_bundle_import` already works out against a real failure at M4.14.
CREATE INDEX acquisition_task_ready
    ON acquisition_task (state, next_attempt_at, priority);
-- §6.6's queue-depth read is per kind, and so is the drain's fairness between kinds.
CREATE INDEX acquisition_task_kind ON acquisition_task (kind, state);
-- The reaper's own index. Partial, because a row that is not leased can never be reclaimed and
-- the table is expected to be mostly `done`.
CREATE INDEX acquisition_task_expired
    ON acquisition_task (lease_expires) WHERE state = 'leased';

-- ---------------------------------------------------------------------------
-- 2. The raw store's index. §8, and decision 345.
-- ---------------------------------------------------------------------------
-- §8: "All fetched bytes land in the app's own raw store, so re-parsing is free forever." The
-- bytes live under /data/raw, gzipped and named by the SHA-256 of their content; these rows are
-- what points at them. The corpus's own module says why the split is the layer worth protecting:
-- "parsers, filters and schemas are all expected to change, and none of those changes may
-- require another crawl."
--
-- `content_sha256` IS DELIBERATELY NOT UNIQUE. Two fetches that return identical bytes share one
-- file and get two rows, so the fetch history stays visible without duplicating bulk — which is
-- the invariant the coverage row `data-rules-fetched-bytes-are-kept-once-and-re-read` asserts in
-- both directions: one file under /data/raw, two rows in here. A UNIQUE here would collapse the
-- history into the storage and make "when did we last see this" unanswerable.
--
-- THE CUSTODY BOUNDARY IS NOT IN THIS TABLE, and this is where a reader will look for it.
-- docker-compose.yml mounts ./data/raw:/data/raw on the WORKER only, and states the reason in
-- its own words: "nothing in the backend reads /data/backups or /data/raw at all, and a file
-- that is not in the container cannot be served out of it. [M4.7 sec-08]" — pinned by
-- test_static_contracts.py. So §6.6's board, which the backend serves, shows this ROW and never
-- the document: url, http status, sha256, byte size, fetched_at, all of them in Postgres.
-- That is decision 345, and M5.1 is the mount's first user without moving it.
--
-- `run_id` carries NO foreign key to `job_run`, for the reason that table is excluded from the
-- queue above: `worker.py`'s `_prune_job_runs` deletes ops rows on a retention rule, so a
-- constraint would either block the prune or take irreplaceable fetch history with it. The
-- column is provenance — which drain wrote this — and a dangling number is the honest outcome
-- when the ops log has rolled over.
CREATE TABLE raw_document (
    id             bigserial PRIMARY KEY,
    source         text        NOT NULL,
    kind           text        NOT NULL,
    entity_key     text,
    url            text        NOT NULL,
    http_status    integer,
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    content_sha256 text        NOT NULL,
    content_path   text        NOT NULL,
    content_type   text,
    byte_size      bigint,
    page           integer     NOT NULL DEFAULT 0,
    -- The validators a conditional re-fetch sends back. The corpus keeps them in a separate
    -- `http_cache` keyed on the url's own hash; they are columns here instead because this row
    -- is already the record of what the app last saw at this url, and a second table keyed on
    -- the same url would be a second answer to that question — the shape §14 risk 5 is about.
    etag           text,
    last_modified  text,
    request_meta   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ok             boolean     NOT NULL DEFAULT true,
    error          text,
    run_id         bigint
);

-- The four the corpus carries, in its own order: the derive reads by entity, the board reads by
-- source, the operator reads by time, and the store reads by hash to find the file.
CREATE INDEX raw_document_entity ON raw_document (entity_key, source, kind);
CREATE INDEX raw_document_source ON raw_document (source, kind, fetched_at DESC);
CREATE INDEX raw_document_fetched ON raw_document (fetched_at DESC);
CREATE INDEX raw_document_hash ON raw_document (content_sha256);
-- And one the corpus does not need. A conditional request asks "what did we last see at THIS
-- url", which is a lookup the four above answer only by scan.
CREATE INDEX raw_document_url ON raw_document (url, fetched_at DESC);

-- ---------------------------------------------------------------------------
-- 3. The fetcher's per-host memory. Decision 340.
-- ---------------------------------------------------------------------------
-- §8 gains the clause this table serves: the fetcher declares a User-Agent naming the app,
-- honours each host's robots.txt with the response cached, and carries per-host rate and
-- concurrency policies as data. The policies themselves are config (`acquire/hosts.py`) because
-- they are measured constants an operator reads rather than state the app writes; what is state,
-- and what therefore has to survive a restart, is this: the robots.txt body the app is honouring,
-- when it was fetched, and whether the circuit breaker has this host paused.
--
-- Per host and not per url, which is what robots.txt is. One row per host, so a breaker opened by
-- a hostile host cannot burn the whole run and cannot leak into another host's pacing — the
-- property the coverage row `jellyfin-acquisition-eval-the-fetcher-is-polite-per-host` asserts
-- when it says other hosts are unaffected.
--
-- The token bucket is NOT here. It is per-process, in-memory and sub-second; a row per request
-- would be a write amplification of the thing being rate-limited, and the worker's loop is one
-- process (§5.3, worker.py). What must be durable is the refusal, not the pacing.
CREATE TABLE fetch_host_state (
    host                 text PRIMARY KEY,
    consecutive_failures integer     NOT NULL DEFAULT 0,
    paused_until         timestamptz,
    requests             bigint      NOT NULL DEFAULT 0,
    errors               bigint      NOT NULL DEFAULT 0,
    last_request_at      timestamptz,
    robots_txt           text,
    robots_status        integer,
    robots_fetched_at    timestamptz
);
