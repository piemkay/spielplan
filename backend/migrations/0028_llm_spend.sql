-- 0028_llm_spend — what a provider call cost, and the pack and the response it is the record of.
-- Spec v2.1 §8 ("paid stages (6) never auto-retry past the spend cap"), §9 ("Port verbatim,
-- including the reasoning-token accounting corrections (Gemini bills thinking tokens as output —
-- counting visible JSON understates cost ~5×)"), §6.6's spend guard ("monthly cap, running
-- meter"), §4.1 rule 2; decisions 325, 337, 348, 382, 403, 430 and 436; docs/milestones/M5.5-plan.md §5.
--
-- NUMBERING. 0028 is what docs/milestones/ROADMAP-M5.md's migration ledger allocates to M5.5.
-- 0024-0027 are M5.1's, M5.2's, M5.3's and M5.4's and are APPLIED, so each is checksummed and
-- unreachable (`db/migrate.py` raises on a mismatch at startup); 0019 stays permanently unused.
-- `db/migrate.py` keys `schema_migration` on the filename stem and sorts a glob, so a gap costs
-- nothing and a renumber would cost everything.
--
-- PORT VERDICT: WITH NAMED CHANGES. The corpus's `llm_call` (mdc/db.py:429-458) is "the cost
-- ledger and the validation record", one row per LLM response, and "Failed calls are kept - a
-- run's failure modes are the most interesting thing about it". Both halves are kept, and so is
-- the table's name. What changes, each for the reason written where it applies below:
-- `tokens_out` becomes `tokens_out_billed`; `cost_usd REAL` becomes `usd numeric`; the one
-- `raw_document_id` becomes two references, the pack the call read and the response it got back;
-- `run`, `pass_kind` and `prompt_version` give way to `task`, `pass_index` and `attempt`, the three
-- things this app's meter, merge and board ask of a call; `created_at REAL` becomes
-- `at timestamptz`; and `ok`/`error`, unpaired there, are paired here by a constraint. `latency_ms`,
-- `prompt_sha` and `n_phrases` are not carried, because plan §5 lists no reader for them.
--
-- WHAT THIS FILE DELIBERATELY DOES NOT DO. It carries no cap, no mode and no per-task assignment:
-- those are connector configuration and `connector_config` (0001_system.sql:22-30) already holds
-- it - the `llm` row's `cap_usd`, `parallel`, `passes`, `extraction_provider` and
-- `parallel_providers` (decisions 324 and 325), and each provider's own row its key, its model
-- and its price override (decision 343). It carries no `pack_sha` of its own (decisions 382 and
-- 430). It adds no counter anywhere: the meter is a SUM over this table, because a counter can
-- drift and a drifted cap is not a cap (decision 325). And it carries no weight of any kind - no
-- confidence, no salience, no source count. Agreement is a property of a merged tag, computed over
-- runs when the runs are merged and written to `dna_tag` (decision 337); §4.1 rule 2 makes those
-- three weights on a tag, and a bill is not a tag.

-- ---------------------------------------------------------------------------
-- 1. One row per paid attempt. Decisions 325, 337 and 430.
-- ---------------------------------------------------------------------------
-- ONE ROW PER ATTEMPT, AND BOTH ATTEMPTS ARE ROWS. §9's two-attempt pattern makes the retry a
-- second full input pass, and plan C3 says whatever the cap rules, both attempts are metered: a
-- second call the meter did not see is a cap that under-reports by up to 100% on exactly the
-- titles whose first answer was bad. `attempt` is CHECKed to the two §9 allows, so a third call is
-- a write the schema refuses rather than a loop nobody bounded.
--
-- AND ONE ROW PER (`provider`, `pass_index`) WITHIN THAT, BECAUSE THAT PAIR IS WHAT A RUN IS.
-- Decision 337 pools runs across providers and passes - a two-pass single-provider union and a
-- two-provider union are the same arithmetic - so the call row carries both halves of the run it
-- belongs to. Without `pass_index` a two-pass extraction's retry is unplaceable: pass 1's second
-- attempt and pass 2's first are one provider and one title, and nothing says which first answer a
-- second attempt was correcting. Decision 325's reservation is counted in the same units (2 x
-- passes x providers), so a meter that could not tell the passes apart could not be read against
-- it. Numbered from 1, as the reservation counts them; no upper bound, because `passes` is the
-- admin's setting (decision 324) and a ceiling here would be the schema guessing one.
--
-- `tokens_out_billed` RATHER THAN `tokens_out`, BECAUSE THE NAME IS THE WHOLE POINT. It is what the
-- provider BILLED as output: Gemini's `candidatesTokenCount` plus its `thoughtsTokenCount`, which
-- the response reports apart and bills as one - "measured at 59% of billable output missing on
-- this prompt" when only the first is counted (mdc/llm/client.py:293-297) - and OpenAI's
-- `completion_tokens`, which already fold the reasoning in. A column named `tokens_out` invites
-- the visible JSON, and §9 says that reading understates the bill about fivefold.
--
-- `usd` IS numeric AND NOT A FLOAT, AND IT IS NOT NULL. The meter is a SUM of it and the cap is a
-- comparison against that SUM, so the column is the one type whose SUM is exact: ten calls at ten
-- cents are a dollar in numeric and 0.9999999999999999 in double precision, and a cap checked
-- against a sum that drifts with the row count is a cap computed from the wrong number. Six
-- places, because a short call at an introductory price bills fractions of a cent and each of them
-- is money. And decision 343 gives no row a guessed price: a model nobody has priced parks stage 6
-- at the gate before any call is made, so every row that exists was priced by the table or by the
-- admin's override. Every attempt is a row from before it is sent - written at its ceiling, the
-- prompt and schema at its adapter's published margin and every token `max_tokens` allows - and
-- then settled: to the usage the provider reported,
-- a refused or cut-off answer included; to zero when the provider's own error status, or a request
-- that never left, says no work was done; and left at the ceiling when the answer never arrived
-- (decision 436). So a call that billed nothing is a row at zero rather than no row, for the
-- corpus's reason above, and a call nobody heard back from is a row at its ceiling: the most it
-- could have cost by every margin its provider publishes, and not a figure it provably cannot pass.
--
-- `at` IS timestamptz, AN INSTANT. Decision 325's month is bounded in the install's `TZ` - the
-- first instant of the local month to the first instant of the next - and those bounds are
-- computed once, by the reader, as instants; a timestamptz compares against them the same way
-- whatever TimeZone a session happens to carry, where a local timestamp would move the month's
-- edge by the session's offset and put a call on either side of the cap.
--
-- `pack_document_id` CITES THE PACK BY THE RAW DOCUMENT THE CALL READ (decision 430). `dna_pack`
-- holds one row per (title, version), upserted IN PLACE by `store_pack` on every rebuild, so a
-- foreign key to its (title_id, version, pack_sha) could only refuse the rebuild of any title a
-- provider had read (ON UPDATE NO ACTION) or rewrite the call to claim it read the new pack (ON
-- UPDATE CASCADE). The raw document is immutable and content-addressed, and the pack's sha stays
-- derivable from it - the first sixteen hex digits of `content_sha256` are `packs.sha()` of the
-- same bytes - so custody is cited and not re-carried, which is decision 382's rule. RESTRICT for
-- 0027's reason on `dna_pack.raw_document_id`: a deleted or nulled pointer would turn "this call
-- read that pack" into "this call read nothing", and nothing in the tree deletes from
-- `raw_document` today. NOT NULL, because a paid call with no pack is not a call stage 6 makes.
--
-- `response_document_id` CITES WHAT CAME BACK, stored in the raw store before anything parses it,
-- as §8's preamble puts every fetched byte. NULL while the call is in flight and when nothing came
-- back to store; NULL too when the provider answered with an error status, whose body is never
-- stored because it can echo the request, and when a 200's usage block did not read, which leaves
-- the attempt at its ceiling rather than at a figure half made up (decision 436); and when the walk
-- ended between an answer and its store -- a cancellation or a database fault there -- which leaves
-- the row where its last settle put it and the bytes unkept. RESTRICT for the same reason: a paid
-- response is the evidence a meter row is the index of.
--
-- `title_id` IS SET NULL ON DELETE, AND THAT IS WHERE THIS TABLE DEPARTS FROM ITS NEIGHBOURS.
-- `dna_reject` and `dna_pack` cascade with their title because each is a fact about one title's
-- extraction. This is a fact about money: the meter is a SUM, and a row that cascaded away with
-- its title would hand the month back what it spent. A NULL title is a call whose subject was
-- deleted, never a call that had none (decision 430).
--
-- `provider` is CHECKed to §9's three. `task` names the §6.6 per-task assignment the call ran
-- under and is CHECKed to the one task M5 has a caller for: plan §8 adds no LLM call anywhere
-- outside stage 6, and §6.6's other two slots, query parsing and conflict phrasing, have no
-- caller before M6 (plan 2.8; decision 339 is M5.7's). A row for a task nothing calls cannot be
-- written, and the milestone that gives another task a caller widens the CHECK in its own
-- migration.
--
-- `ok` AND `error` ARE PAIRED BY A CONSTRAINT: a failed call names its failure and a good one
-- names none. They are the attempt's own outcome, kept for the corpus's reason - a failed call
-- still cost money, and a run's failure modes are the most informative thing about it - and the
-- corpus leaves them unpaired, as 0024's `raw_document` does. A meter row is read by §6.6's board
-- and by whoever audits a cap, and a row claiming a failure with no reason, or a success with
-- one, is a row neither can read.
CREATE TABLE llm_call (
    id                   bigserial      PRIMARY KEY,
    at                   timestamptz    NOT NULL DEFAULT now(),
    provider             text           NOT NULL
                         CHECK (provider IN ('anthropic', 'openai', 'gemini')),
    model                text           NOT NULL,
    task                 text           NOT NULL CHECK (task IN ('extraction')),
    title_id             integer        REFERENCES title(id) ON DELETE SET NULL,
    pass_index           smallint       NOT NULL CHECK (pass_index >= 1),
    attempt              smallint       NOT NULL CHECK (attempt IN (1, 2)),
    tokens_in            integer        NOT NULL CHECK (tokens_in >= 0),
    tokens_out_billed    integer        NOT NULL CHECK (tokens_out_billed >= 0),
    usd                  numeric(12, 6) NOT NULL CHECK (usd >= 0),
    ok                   boolean        NOT NULL,
    error                text,
    pack_document_id     bigint         NOT NULL REFERENCES raw_document(id) ON DELETE RESTRICT,
    response_document_id bigint         REFERENCES raw_document(id) ON DELETE RESTRICT,
    CONSTRAINT llm_call_error_names_a_failure CHECK (ok = (error IS NULL))
);
-- The meter's SUM reads one local month of `at` (plan §5). §6.6's board reads one title's calls
-- newest first, which is 0027's `dna_reject_title` shape one table over: an index leading on
-- `title_id` serves every lookup plan §5's single-column one would and hands them back ordered.
CREATE INDEX llm_call_at ON llm_call (at);
CREATE INDEX llm_call_title ON llm_call (title_id, at DESC);
