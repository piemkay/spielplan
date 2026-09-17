# M5 — seven milestones, and the plan that groups them

This is the handover document for M5. Unlike `ROADMAP-to-M5.md`, it is **not** a partitioning of
reproduced findings: M5 has none, because nothing in it is built. It is a forward decomposition of
roughly eighty lines of normative text — §7.2, §8, §8.4, §9 and one row of §12 — into seven
milestones that can be handed to seven agents, and it says out loud where that instrument is
weaker than the retrospective one the eleven M4.x plans were cut with.

**Nothing here amends the spec.** The spec is amended in place by the milestone that holds
`current_milestone`, per decision 288. This document *proposes*, and it *records* the decisions an
owner must take before the code is written. Where it recommends a ruling it says so as a
recommendation; the numbers 321 onward are questions until somebody answers them.

## Start here

**If you are an implementation agent picking this up, read this section, then your milestone's
plan, then nothing else in this file unless it sends you here.**

### What this is

§12 gives M5 one row — Contents "Acquisition pipeline + admin connector UI + LLM layer + extraction
flywheel", criterion "a new Jellyfin add reaches 'ready' unattended"
(`docs/spielplan-spec_v2.1.md:471`). Four more sections describe it: §7.2 (`:342`), §8 (`:357`),
§8.4 (`:400`) and §9 (`:406`). That is the entire written scope of the largest milestone in the
project. `backend/tests/spec_coverage.toml` already carries **eleven** `milestone = "M5"` rows, all
with `tests = []` (`:1000` and `:3154`-`:3235`), and `docs/TESTING.md:265` prints `M5 0/11 covered`.
Under CLAUDE.md, that red list **is** the test plan.

Two facts make M5 different from every milestone before it, and both should be read before the
decomposition below:

1. **It is a port.** §8's own preamble says so: "Ported skeleton: the corpus project's raw store …
   durable `(kind,key)` queue, per-host rate-limited HTTP layer, and the single-title prototype
   (`mdc probe`)" (`:359`). The thing being ported exists, at
   `C:/Users/pmk/Workspace/movie_data_curator`, and its module boundaries are evidence a forward
   decomposition otherwise would not have.
2. **Its exit criterion covers about a quarter of its own row.** "Unattended" means nobody opens
   the admin view, so a build satisfying that sentence can ship with no flywheel, no admin
   connector UI, one provider, one pass, and the validator's retry path never executed. Section
   "The exit criterion is not a gate" below enumerates fourteen things it never touches.

### The state of the tree

Branch `claude/m5`, from `main @ 0b71b10` ("feat(M4.16): the documents, the coverage contract and
the release gate"). Worktree at `.claude/worktrees/m412`. Nothing in `backend/`, `frontend/`,
`ops/` or `e2e/` has been touched by this planning pass; the only new files are this one and
`M5.1-plan.md` … `M5.7-plan.md` in this directory.

`current_milestone = "M4.16"` (`backend/tests/spec_coverage.toml:20`). `MILESTONES` in
`backend/tests/test_spec_coverage.py:285-286` is
`["M0","M1","M2","M3","M4","M4.5","M4.6" … "M4.16","M5","M6","M7"]` — M5 is already in the list,
and `_at_or_before` gates on `MILESTONES.index`, so inserting `"M5.1"`…`"M5.7"` between `"M4.16"`
and `"M5"` is mechanically identical to what M4.6…M4.16 already did.

### The rules, before you write a line

1. **Take your migration number from the ledger in this file.** `0024` is next free, confirmed two
   ways: `ls backend/migrations/` shows `0016`-`0018` and `0020`-`0023` and no `0024`, and
   `test_spec_coverage.py:283-284` records the reason — "0024 is not claimed (decision 292 …) and
   0019 stays permanently unused". If you land out of order, claim the next free number and say so
   in the commit body.
2. **`spec_coverage.toml` carries one global `current_milestone`.** Only the milestone holding it
   has a red gate; every other in-flight milestone appends rows without raising the scalar. **M5.1
   holds the scalar** and owns every structural edit to the coverage instrument and to the spec.
   Adding `"M5.1"` to `MILESTONES` and adding at least one M5.1 row must be **one commit**, or the
   build is red for everyone. This is the M4.6 rule, unchanged.
3. **Two M5 coverage rows cite bare proposals and will fail decision 295's guard the moment the
   scalar reaches them.** `spec_coverage.toml:3207` reads `"§8 stage 6 + §9 (with decision-doc
   proposals 107, 109)"` and `:3225` reads `"§8.4 (with decision-doc proposals 135, 104)"`. The
   guard was taught in M4.16 cycle 2 to read a **list**, precisely so a one-number repair cannot
   turn a row green over the number nobody read (`test_spec_coverage.py:1092-1094`), and
   `test_spec_coverage.py:1123-1124` names the consequence in advance: *"M5 opens with exactly two
   rows citing bare proposals, and it has no plan document to argue with."* This is that document.
   **Decision 330 is what discharges it**; renaming `proposals` to `decisions` is explicitly
   foreclosed by `_laundered_decision_citations` (`test_spec_coverage.py:1116-1126`).
4. **`CLAUDE.md` still governs.** Surgical diffs, cite the clause, never edit an applied migration,
   no speculative abstractions, ASCII in console output, line length 108.
5. **The corpus project is read-only.** It is a separate repository. Port from it; never write into
   it, and never make a Spielplan requirement depend on a change landing there.

### What to do first

**M5.1, alone, and nothing else concurrently.** It is this decomposition's M4.7: it owns the seams
every other lane plugs into (`worker.py`, `app.py`, the queue, the fetcher, the raw store, the
pipeline driver) and it holds `current_milestone`. Starting a second lane before it lands means
that lane has nothing to enqueue into and must invent a second job mechanism — which is exactly the
trap `M4.14-plan.md:955-958` names for its own Wave E.

Before M5.1 opens, get **decisions 321, 322, 323 and 331 answered**. 322 in particular cannot be
deferred: it decides the shape of migration `0024`, and an applied migration is checksummed and
cannot be revised (`CLAUDE.md`, and `db/migrate.py`'s hard startup error).

---

## How the grouping was chosen, and where the instrument is weaker

`ROADMAP-to-M5.md:8-14` fixes the method: produce three partitionings — by subsystem/file locality,
by root cause/invariant, by household-visible promise — judge them against a coupling map, let file
locality draw the boundaries, let the invariant partitioning supply each milestone's language and
the household partitioning supply the ordering and the exit criteria.

The method applies here. **One input is different and it must be said out loud.** M4's partitioning
was over 439 reproduced findings — a coupling map of files that *exist*. M5 has no findings. A
forward decomposition is a weaker instrument than a retrospective one, and this document should be
read as a proposal that will be corrected by contact with the code, not as a measurement.

What rescues it is specific to this milestone: **the corpus has already built this system and its
import graph is the coupling map.** Measured by reading the import block of each module:

```
mdc/http.py         -> config, db                       (nothing else)
mdc/rawstore.py     -> config, db                       (nothing else)
mdc/queue.py        -> db                               (nothing else)
mdc/llm/client.py   -> config, http                     (NOT dna, NOT sources, NOT queue)
mdc/dna/packs.py    -> config                           (nothing else)
mdc/dna/store.py    -> config, db, dna/{adjudication,packs,similarity,vocab}
mdc/dna/project.py  -> config, db, dna/vocab, aspects/prompt
mdc/parse/titles.py -> ids, sources/_htmlutil           (NOT http, NOT queue, NOT rawstore)
mdc/sources/base.py -> config, http, queue
mdc/sources/tmdb.py -> rawstore, config, ids, queue, sources/base
```

That is a DAG with a narrow waist (`config` + `db`, both of which this app already has as
`core/config.py` and `db/pool.py`) and four independent trees above it. **The verifier does not
import the LLM client** — which is §9's "the schema is a cost-saving device, the guarantee is the
validator" (`spec:412`) expressed as a module boundary rather than as a sentence. The parsers are
pure functions over bytes and import no transport. These seams were not designed for this plan;
they were arrived at by building the thing, which is the strongest evidence a file-locality
partitioning can have short of findings.

### Size, measured

Ported corpus surface, counted with `wc -l`:

| area | corpus lines |
|---|---|
| plumbing: `http.py` 404, `rawstore.py` 170, `queue.py` 257, `config.HOST_POLICIES` ~150 | ~980 |
| 8 sources + `base.py` 129 + `_htmlutil.py` 225 | ~1,470 |
| parsers: `parse/titles.py` 1032, `parse/reviews.py` 490, `ids.py` 347, `corrections.py` 137 | ~2,010 |
| DNA: `packs.py` 236, `craft.py` 356, `store.py` 688, `project.py` 375, `adjudication.py` 252, `prompt.py` 180, `vocab.py` 393 | ~2,480 |
| LLM: `llm/client.py` 324, `llm/batch.py` 488, `sources/llm.py` 223 | ~1,035 |
| **total port surface** | **~7,975** |

None of it is a copy: sqlite to Postgres, sync to async, CLI to worker job. On top sits genuinely
new code the spec itself flags as new — the webhook and its debounce, the delta poll, the per-title
incremental derive replacing wholesale corpus steps (§8's preamble and §14 risk 5 both say so), the
ten-stage driver, the flywheel, the spend meter — plus six §6.6 admin surfaces that do not exist at
all.

Calibration against what a milestone has actually been in this repository (production lines under
`backend/spielplan` + `frontend/src`, excluding tests and docs, as recorded by the preceding plans):
M4.14 5,558 production / 7,872 test; M4.15 4,121 / 5,768; M4.16 607 / 13,154.

**M5 is four to six M4.14s.** One §12 row hiding four-to-six milestones is precisely the condition
that turned M4 into eleven.

### The exit criterion is not a gate

§12's M5 row names four Contents and one criterion. "Unattended" means nobody opens the admin view.
A build that satisfies that sentence can ship with:

1. no extraction flywheel at all (one of the four Contents);
2. no admin connector UI at all (a second of the four);
3. one LLM provider, one pass, no parallel mode, no batch mode, no consensus arithmetic;
4. the two-attempt validator retry never executed — a happy-path response violates no contract;
5. reasoning-token accounting 5x wrong; the title still reaches ready;
6. the spend cap's park behaviour never taken; the criterion is under the cap by definition;
7. no park/retry path, no 30-day review window, no admin retry;
8. re-derive idempotence untested — the criterion derives **once**, and §14 risk 5's
   787-rows-reverted-twice scar is about the *second* derive;
9. stage 3's "applies BOTH ledgers, corrections last" satisfied **vacuously**, because a brand-new
   title carries no adjudication and no correction, so code that does nothing passes;
10. the 10-minute debounce and the per-show-not-per-episode rule untested — one film add exercises
    neither;
11. six of stage 2's eight sources never called;
12. `is_owned = false` never exercised — the criterion only adds;
13. the three ledger editors and the DNA-reject review absent;
14. the whole delta-poll path absent if the webhook is used, or vice versa.

A single milestone whose only gate can be closed while three of its four named Contents are absent
is not a gate. Seven milestones means seven criteria, each of which is a sentence a person can fail
on a real install — the register §12 already keeps. **Decision 331** is whether §12's row is
amended in place to name its second half, the way M4.11's and M4.12's rows gained clauses.

### The three arguments against splitting, answered

**"M5 is one coherent pipeline with a single exit criterion."** The coherence is real at runtime and
false at the file level; the corpus's own import graph shows four independent trees. And the single
exit criterion is the problem, not the defence.

**"A split multiplies merge surface and ceremony."** The merge surface that actually hurt in M4 is
named in the repository: `test_spec_coverage.py:275-278` — *"eleven agents amending one normative
file in parallel is the worst merge surface in this repository, and a conflicted paragraph in a
spec is two readings of a requirement rather than a merge conflict."* The mitigation is already
designed and shipped: one milestone owns all structural edits; every other appends coverage rows
without raising the scalar. Seven is not eleven.

**"Parallel lanes cost real integration work."** True, and it is an argument for fewer *concurrent*
lanes, not for one milestone. The sequencing below has at most four that could run concurrently and
recommends **two at a time**.

### Why seven and not eleven

Because the hotspot count is smaller. M5's work is overwhelmingly in **new packages**; the
contention is concentrated in nine existing files. Seven owners cover nine hotspots with no
overlap. Going finer forces two owners onto `worker.py` or onto `admin/data/+page.svelte`, which is
the failure the file-locality test exists to catch.

---

## The milestones

| # | Milestone | Effort | Depends on | Parallel with | Migration |
|---|---|---|---|---|---|
| **M5.1** | The spine: queue, raw store, polite fetcher, ten-stage driver | 12-16 working days | — | **nothing** | `0024` |
| **M5.2** | The trigger: every add the household told us about | 6-9 working days | M5.1 | M5.3, M5.4, M5.5 | `0025` |
| **M5.3** | The sources and the derive: crawl once, re-parse forever | 14-18 working days | M5.1 | M5.2, M5.4, M5.5 | `0026` |
| **M5.4** | The DNA half: the pack, the trust boundary, the projection | 10-14 working days | M5.1 | M5.2, M5.3, M5.5 | `0027` |
| **M5.5** | The LLM connector layer | 9-12 working days | M5.1, M5.4 (runtime) | M5.2, M5.3 | `0028` |
| **M5.6** | Admin · Data: board, flywheel, ledger editors, rejects | 8-11 working days | M5.1, M5.3, M5.4, M5.5 | M5.7 | `0029` or none |
| **M5.7** | Admin · Connectors and System | 6-8 working days | M5.1, M5.2, M5.5 | M5.6 | none |

**Parallel safety, stated in both directions.** A pair is parallel-safe only when *neither* member
writes a file the other writes. The table below is the claim; the file-locality test two sections
down is the evidence.

| | M5.1 | M5.2 | M5.3 | M5.4 | M5.5 | M5.6 | M5.7 |
|---|---|---|---|---|---|---|---|
| **M5.1** | — | no (dep) | no (dep) | no (dep) | no (dep) | no (dep) | no (dep) |
| **M5.2** | no | — | **yes** | **yes** | **yes** | no (dep) | no (dep) |
| **M5.3** | no | **yes** | — | **yes** | **yes** | no (dep) | yes |
| **M5.4** | no | **yes** | **yes** | — | **yes** | no (dep) | yes |
| **M5.5** | no | **yes** | **yes** | **yes** | — | no (dep) | no (dep) |
| **M5.6** | no | no | no | no | no | — | **yes** |
| **M5.7** | no | no | yes | yes | no | **yes** | — |

"no (dep)" is a dependency rather than a file collision: the later milestone can be *written*
against the earlier one's interfaces, but its exit criterion cannot be run until the earlier one
lands.

### M5.1 — The spine: the queue, the raw store, the polite fetcher, and the ten-stage driver

Nothing in §8 is buildable until work is durable, bytes are kept once, and the network is
approached politely — so the milestone that owns those three owns the seams and lands first.

**Exit criterion.** On a real install with stages 2-8 declared no-ops, a task injected for a
Jellyfin item carrying provider ids walks stage 1 to 9 to 10, mints a title above 1e9, is placed by
the Cold Tower and appears on Home carrying "new — model placement, no crowd data"; a worker killed
mid-lease has its task reclaimed and completed by the next worker without duplicating a derived
row; a fetch of the same URL twice writes one file and two `raw_document` rows; and `/data/raw` is
readable by the worker and absent from the backend container.

**Principal files.** `backend/spielplan/worker.py`, `backend/spielplan/app.py`,
`backend/spielplan/placement/reconcile.py`, `backend/tests/test_static_contracts.py`,
`backend/tests/test_layering_guards.py`, `backend/tests/test_spec_coverage.py`,
`backend/tests/spec_coverage.toml` (structural), `docs/spielplan-spec_v2.1.md`,
`backend/migrations/0024_acquisition.sql` (new), `backend/spielplan/acquire/*` (new),
`backend/spielplan/api/events.py` + `api/acquisition.py` (new).

Full plan: [M5.1-plan.md](M5.1-plan.md)

### M5.2 — The trigger: every add the household told us about

§7.2's owner requirement is a promise about *arrival*, and ownership is re-derived rather than
trusted — so the two intake paths and the sweep that falsifies ownership are one milestone.

**Exit criterion.** A burst of `ItemAdded` events for many episodes of one series inside the
10-minute window yields one job for the show; with the webhook plugin absent, the delta poll
enqueues the same set once and does not re-enqueue on the following poll; a title absent from the
current mirror flips `is_owned = false` and flips back when re-added; an add in a library the admin
did not pick is recorded and not enqueued.

**Principal files.** `backend/spielplan/connectors/jellyfin.py`,
`backend/spielplan/connectors/resolve.py`, `backend/spielplan/sync/seen.py`,
`ops/fake_jellyfin.py`, `backend/spielplan/acquire/intake.py` (new), the `/events` router body.

Full plan: [M5.2-plan.md](M5.2-plan.md)

### M5.3 — The sources and the derive: crawl once, re-parse forever, corrections last

§14 risk 5 is M5's largest named risk, and its whole content is that a per-title derive which
regenerates rows without re-applying both curated ledgers silently reverts curated fixes — so the
eight fetchers, the parsers and the two ledger appliers are one milestone and its exit criterion is
the scar.

**Exit criterion.** A title carrying a curated credit correction is re-derived, its rows
regenerated, and the correction is still there afterwards — with the two ledgers applied at their
own points rather than merged into one pass; a title with a plot but fewer than two review sources
parks at `reviews gate` with the counts in its reason and a 30-day window; the admin retry resumes
from the parked stage, re-reads the content-addressed raw store instead of re-fetching, and
duplicates no derived row.

**Principal files.** `backend/spielplan/importer/dna.py`, `backend/spielplan/importer/meta.py`,
`backend/spielplan/sources/*` (new, 10 modules), `backend/spielplan/derive/*` (new).

Full plan: [M5.3-plan.md](M5.3-plan.md)

### M5.4 — The DNA half: the pack, the trust boundary, the projection

§8 stage 7 is the only place in this app where an LLM's output becomes a fact, and the ported rule
is that failures drop and are never repaired — so packing, verifying and projecting are one
milestone, and it is deliberately the one that never calls a provider.

**Exit criterion.** A fabricated tag inside an otherwise well-formed provider response is absent
from the verifier's output; no tag is ever repaired, only dropped, and the drop is recorded with
the rule it violated; every passed tag carries its evidence quote; and a per-title projection of
one acquired title completes in under 1 s on CPU.

**Principal files.** `backend/spielplan/db/dna_terms.py`, `backend/spielplan/dna/*` (new:
`norm.py`, `packs.py`, `craft.py`, `verify.py`, `project.py`, `adjudicate.py`).

Full plan: [M5.4-plan.md](M5.4-plan.md)

### M5.5 — The LLM connector layer: the validator is the guarantee, and the meter counts what is billed

§9's two hard-won corrections are that a schema is a cost-saving device rather than a guarantee, and
that counting visible JSON understates the bill roughly fivefold — so the three adapters, the
two-attempt pattern and the spend meter are one milestone.

**Exit criterion.** A response that satisfies the request schema but violates the extraction
contract is rejected and retried exactly once with the violation named; a second violation fails
the stage and writes nothing; the behaviour is identical across all three adapters despite three
different structured-output mechanisms; the meter charges thinking tokens; at the cap, stage 6
parks `over spend cap` without issuing a paid call and never auto-retries.

**Principal files.** `backend/spielplan/connectors/registry.py`, `backend/spielplan/core/config.py`,
`backend/spielplan/llm/*` (new), `backend/spielplan/api/llm.py` (new).

Full plan: [M5.5-plan.md](M5.5-plan.md)

### M5.6 — Admin · Data: the board, the flywheel, the ledger editors, the rejects

§6.6's Data card is four promises about one page, and a queue fed automatically is only an
instrument if a person can read it and spend against it.

**Exit criterion.** A thin-facet title appends a flywheel row carrying its reason **at the moment it
happens**, readable from the admin queue immediately and not after a nightly job; a launched batch
targets exactly the selected rows and no others; three separate editors write three separate
artifacts with no merged write path; and a job parked at stage 4 is retried from the board and
resumes from that stage.

**Principal files.** `frontend/src/routes/admin/data/+page.svelte`, `backend/spielplan/home/rail.py`,
`backend/spielplan/flywheel/*` (new), `backend/spielplan/api/flywheel.py` (new), four new Svelte
components.

Full plan: [M5.6-plan.md](M5.6-plan.md)

### M5.7 — Admin · Connectors and System: the providers, the spend guard, and what the box is doing

§6.6's spend guard is the only surface in this app where a household can be surprised by a bill, so
it is the one that must show the number before the setting persists.

**Exit criterion.** Enabling an extraction provider returns its per-title cost estimate and the
projected monthly figure against the remaining cap **before** the setting persists; flywheel
estimates recompute when the pass count changes; an admin retry that would breach the cap is
refused with that reason; and §6.6's System card gains queue depth, last syncs and logs.

**Principal files.** `frontend/src/routes/admin/connectors/+page.svelte`,
`frontend/src/routes/admin/system/+page.svelte`, `backend/spielplan/api/admin.py`, three new Svelte
components.

Full plan: [M5.7-plan.md](M5.7-plan.md)

---

## The file-locality test, applied explicitly

Every file more than one M5 milestone would touch, and its single owner:

| hotspot file | owner | why no one else touches it |
|---|---|---|
| `backend/spielplan/worker.py` | **M5.1** | the spine registers every M5 job; other lanes export a callable and register nothing. This is M4.7's role |
| `backend/spielplan/app.py` | **M5.1** | the `/events` namespace decline in `SpaFallback.matches` (`app.py:522`); `api/events.py` is mounted empty for M5.2 to fill |
| `backend/spielplan/placement/reconcile.py` | **M5.1** | the `app_acquired` caller (`:208-212`) and reconciling the existing `_park_thin` writer (`:368-403`) with the pipeline's |
| `docker-compose.yml`, `test_static_contracts.py` | **M5.1** | the `/data/raw` mount already exists (`docker-compose.yml:45`) and its guard pins the worker's whole list (`test_static_contracts.py:1117-1119`); only M5.1 may touch either |
| `test_layering_guards.py` (`ALLOWED_RESIDUE`, `:421-434`) | **M5.1** establishes; each lane adds its own router module's entry | `artifacts.py: 6` is the precedent: M4.14 gave its admin routes their own module rather than growing `admin.py` |
| `connectors/jellyfin.py`, `connectors/resolve.py`, `sync/seen.py` | **M5.2** | — |
| `ops/fake_jellyfin.py` | **M5.2** | — |
| `importer/dna.py`, `importer/meta.py` | **M5.3** | — |
| `db/dna_terms.py` | **M5.4** | — |
| `connectors/registry.py`, `core/config.py` | **M5.5** | the generic `ConnectorSpec` serves M5.3's source credentials too, but M5.5 writes it and M5.3 consumes it |
| `home/rail.py` | **M5.6** | — |
| `frontend/.../admin/data/+page.svelte` | **M5.6** | board, flywheel, editors and rejects are four cards on one page; cards are components |
| `frontend/.../admin/connectors/+page.svelte`, `.../system/+page.svelte` | **M5.7** | — |
| `api/admin.py` | **M5.7** | every other lane gets its own router module |
| `docs/spielplan-spec_v2.1.md`, `spec_coverage.toml` structural edits, the §12 table, `MILESTONES` | **M5.1** | the M4.16 rule, unchanged: only the milestone holding `current_milestone` has a red gate |

**No two milestones own the same hotspot file.** Two collisions had to be resolved:

1. **`connectors/registry.py`** is wanted by M5.2 (Jellyfin's library pick and webhook token), M5.3
   (tmdb/omdb/trakt) and M5.5 (three LLM providers). Resolved by giving M5.5 the file: it writes
   the generic `ConnectorSpec` that `ROADMAP-to-M5.md:578` (DEFERRED-B) assigns to M5, and M5.2 and
   M5.3 consume it. If M5.5 has not landed, M5.2 uses the shipped `load_jellyfin`/`save_jellyfin`
   (`registry.py:233`, `:245`) unchanged.
2. **`admin/connectors/+page.svelte`** is wanted by M5.2 (webhook status, library pick) and M5.5
   (provider cards). Resolved by moving **all** frontend admin files into M5.6 and M5.7 — the M4.15
   precedent, where the shell restyled surfaces only after the backends stopped moving. M5.2 and
   M5.5 ship APIs and prove themselves through the fake Jellyfin and the refusing LLM double,
   exactly as M4.11's sync exit criterion does.

---

## The migration ledger

`0024` is next free. `0019` was allocated to M4.10 and deliberately never written
(`0020_jellyfin_items.sql:24-27`); `0024` was reserved for M4.16, which took none — recorded in
`test_spec_coverage.py:283-284` ("0024 is not claimed … and 0019 stays permanently unused") and
confirmed by `ls backend/migrations/`. **Take the number from this table, not from the plan you are
holding.**

| Migration | Milestone | Contents |
|---|---|---|
| `0024` | M5.1 | Durable task queue, `raw_document`, robots cache, the `acquisition_job` board/history reshape decision 322 settles |
| `0025` | M5.2 | The library-wide `DateCreated` watermark and the webhook intake table — **or none**, if decision 327 puts the watermark in `connector_config` |
| `0026` | M5.3 | `title.wikidata_id`, `title.wikipedia_title` (the corpus carries both; `0003_content.sql:27-59` carries neither, and `wikidata:resolve` / `wikipedia:article` key on them), plus the ledger `origin` provenance column decision 326 needs |
| `0027` | M5.4 | `dna_reject` (absent — grep returns nothing), pack custody (`pack_sha`), so a verification is reproducible against the text it was made from |
| `0028` | M5.5 | `llm_call` (provider, model, task, `tokens_in`, `tokens_out_billed`, `usd`, `at`), so the meter is a `SUM` and not a counter that can drift |
| `0029` | M5.6 | The flywheel batch join. Possibly none |
| — | M5.7 | None |

M5.7 writes no migration and says so, which is the behaviour to copy.

---

## Decisions the owner must take, numbered from 321

**321 is the next free number.** The register's last entry is 320
(`docs/spec-v2.2-proposals.md:7232`), and 288-320 are M4.16's. Entries 1-161 are dated reasoning
citable as provenance only; 162 onward are owner decisions, normative from the day each is taken.

Each entry below is written in the register's form — `**What the record says.**`, `**Why it
changes.**`, `**The decision.**`, `**Cost.**` — with the recommendation marked as such. **They are
not taken.** Transcribing them into `docs/spec-v2.2-proposals.md` is an owner act.

### Blocking — settle before the owning milestone opens

| # | question | blocks |
|---|---|---|
| **321** | Does M5 ship as one milestone or as M5.1-M5.7? | everything |
| **322** | Is the acquisition queue `acquisition_job` itself, or a separate durable `(kind,key)` queue with `acquisition_job` kept as the per-title board §6.6 reads? | M5.1 / `0024` |
| **323** | Does stage 1 mint a title row from Jellyfin alone, or only on a provider id? | M5.1, M5.2 |
| **324** | Is parallel/consensus mode on or off on a fresh install, and is the default pass count one or two? | M5.5, M5.7 |
| **325** | Is the spend cap a calendar month in the install's `TZ` or a rolling 30 days, and what is the default cap? | M5.5 / `0028` |
| **326** | Where do the in-app ledger editors write, and what stops the next models-only import from wiping household-authored rows? | M5.3 / `0026`, M5.6 |
| **327** | Which path owns `is_owned = false`? | M5.2 |
| **328** | What is a title's "unnamed taste residual share", and what threshold queues it — or is feed 3 struck from §8.4 and from `flywheel_item`'s CHECK? | M5.6 |
| **329** | What makes a title "thin-facet", and is it the same test as `features.BuiltVector.is_thin`? | M5.6 |
| **330** | Are proposals **104, 107, 109** and **135** adopted or struck, by number? | M5.6, M5.7 |
| **331** | Is §12's M5 exit criterion amended in place to name its second half? | all seven |

### 321. M5 ships as a numbered set of seven rather than as one milestone

**What the record says.** §12 gives M5 one row (`spec:471`). `MILESTONES` in
`test_spec_coverage.py:285-286` carries `"M5"` as one entry between `"M4.16"` and `"M6"`, and
`spec_coverage.toml` carries eleven `milestone = "M5"` rows.

**Why it changes.** The row's Contents name four subsystems and its criterion can be closed while
three of them are absent (see "The exit criterion is not a gate"). Measured port surface is ~7,975
corpus lines plus genuinely new code, against M4.14's 5,558 production lines — four to six M4.14s
behind one gate. And the mechanism for splitting is already built and exercised eleven times.

**The decision (recommended).** Seven: M5.1 … M5.7, inserted into `MILESTONES` between `"M4.16"`
and `"M5"`, with the eleven existing M5 rows re-pointed at the milestone that owns each. `"M5"`
itself **stays in the list** as the umbrella §12 names, holding the amended criterion decision 331
settles; a row may be filed at `"M5"` only if it is measured by `ops/m5_exit_criterion.py`.

**Cost.** Seven §12 rows instead of one, seven exit scripts or one with seven sections, and the
`MILESTONES` edit. What is given up: the simplicity of one row. What is bought: seven sentences a
person can fail.

### 322. The acquisition queue is a new table; `acquisition_job` stays the board

**What the record says.** §8's preamble names "durable `(kind,key)` queue" as ported skeleton
(`spec:359`). §6.6 names "acquisition pipeline monitor (per-title stage board from
`acquisition_job`)" (`spec:322`). `0005_ledger.sql:133-142`:

```sql
CREATE TABLE acquisition_job (
    title_id   integer PRIMARY KEY REFERENCES title(id) ON DELETE CASCADE,
    stage      smallint NOT NULL CHECK (stage BETWEEN 1 AND 10),
    status     text NOT NULL CHECK (status IN ('queued','running','parked','ready','failed')),
    ...
```

**Why it changes.** `title_id` is the **PRIMARY KEY**, so one title holds at most one job ever —
which forecloses a re-added title's fresh job, a flywheel re-extraction of a ready title, and all
job history. The FK to `title(id)` means a job cannot exist before its title row does, while stage
1's job is what mints the title (`resolve.py:182-188` refuses to mint and says acquiring new titles
is M5's). And the table is not empty: `placement/reconcile.py:368-403` already parks thin titles at
`PARK_STAGE = 2` with `ON CONFLICT (title_id) DO NOTHING` (`:249-253`), so M5 inherits rows rather
than starting clean. Migrations are checksummed; this shape cannot be revised later.

**The decision (recommended).** Keep `acquisition_job` as the board exactly as §4.2 and §6.6
describe it, and add in `0024` a separate `acquisition_task(kind, key, payload, priority, state,
attempts, max_attempts, next_attempt_at, lease_owner, lease_expires, last_error, paid, ...)` with
`UNIQUE (kind, key)` — the port of `mdc/queue.py` (identity `(kind,key)`, "recovered by lease
expiry, not by any shutdown handler — which is the only way that actually survives `kill -9`",
`mdc/queue.py:1-9`). The task is keyed on the **Jellyfin item or provider id**, so it can hold work
for a title that does not exist yet; stage 1 mints the `title` row and the board row appears then.
Add an `acquisition_run` history table if the audit is wanted, or append superseded runs to
`acquisition_job.detail`.

**Cost.** One more table and one more concept on the Data tab. What is bought: a re-added title can
be re-acquired, a flywheel can re-extract a ready title, and stage 6's "never auto-retries past the
spend cap" has a place to record what was attempted.

### 323. Stage 1 mints only on a provider id

**What the record says.** §8 stage 1 is "identify | Jellyfin ProviderIds -> title row
(fill-never-clobber)" (`spec:364`). §4.1: "every later id is Spielplan's, minted at or above 1e9
and below 2^31" (`spec:93`), implemented as `title_id_seq` with `MINVALUE 1000000000`
(`0015_seed.sql:24`) and a comment naming "§8 stage 1" as the write path the partition backstops
(`0015_seed.sql:39-44`).

**Why it changes.** Nothing says what identity a *minted* row gets when Jellyfin supplies only a
name and a year. `ops/fake_jellyfin.py:52-55` ships exactly such an item ("Tampopo", empty
`ProviderIds`). `resolve.py:145-179` refuses a name+year match and states the measurement: *"2,438
titles share `(kind, lower(name))` and 573 groups still collide with the year applied"*. A row
minted off a name and a year is that same wrong match, written instead of refused.

**The decision (recommended).** Stage 1 mints only on a provider id (imdb/tmdb/tvdb). An item with
none is parked at stage 1 with the reason "no provider id" and is an admin's problem. §8's stage-1
line gains that clause.

**Cost.** Titles Jellyfin cannot identify never acquire automatically. That is the honest outcome:
the alternative writes a wrong `is_owned`, `owned_checked_at` and deep link onto a film the
household does not have, and nothing ever revisits it.

### 324. Parallel mode's default and the default pass count

**What the record says.** §8 stage 6: "1..N providers per admin config; two passes recommended
(union +13pp recall)" (`spec:382-384`). §6.6 makes parallel mode a toggle and gives the consensus
numbers (`spec:321`). No section states either default.

**Why it changes.** `spec_coverage.toml:3159` forbids a test: *"Whether parallel mode defaults on
or off is still an open question, so no test may assert the default."* Proposal 160
(`spec-v2.2-proposals.md:1391`, provenance only) records that the prototype ships `parallel: true`
and argues it is "the only defaulted spend decision in the app". Two providers at two passes is 4x
the single-run bill.

**The decision (recommended).** Off, one pass. §6.6's own "per-title cost estimate **before
enabling**" reads as an off default that the admin turns on, and a household that never opens the
toggle must never double its bill. If the owner prefers on — the recall case is 93% vs 67% — §6.6
must say so and pair it with a cap the default cannot exceed.

**Cost.** Lower recall out of the box. Either answer must be written into §6.6, because the
coverage row cannot go green while the default is unstated.

### 325. The spend cap's period and default

**What the record says.** §6.6: "monthly cap, running meter reading '$4.12 of $25.00 this month'
(corpus baseline: ~$0.005-0.01/title/pass on Gemini batch)" (`spec:321`). §8: "paid stages (6) never
auto-retry past the spend cap" (`spec:398`).

**Why it changes.** "$25.00" is an example inside a meter caption, not a stated default. Nothing
says whether the month is a calendar month or a rolling 30 days, what timezone bounds it, or what
happens to the meter at the boundary. A cap with no period is not a cap, and two coverage rows rest
on it (`spec_coverage.toml:3171`, `:3207`).

**The decision (recommended).** Calendar month in the install's `TZ`, over the new `llm_call` table
(`0028`), so the meter is a `SUM` over `date_trunc('month', at AT TIME ZONE tz)`. Default cap: a
number the owner picks; the arithmetic below says the unattended path costs ~$1.30/month at 20
titles, so any default above ~$5 never binds the criterion and the cap exists for the flywheel's
Launch button.

**Cost.** One column set and one query. The decision must also say whether the two-attempt retry's
second call is budgeted inside the cap or checked against it.

### 326. Where the in-app ledger editors write, and what survives a re-import

**What the record says.** §6.6: ledger editors "writing the TSV formats the corpus project already
uses … **so app-side fixes survive every future re-derive**" (`spec:322`).

**Why it changes.** **Decision 171 already recorded the collision**, verbatim in its Cost paragraph
(`ROADMAP-to-M5.md:390-412`): *"`DELETE FROM credit_correction` is unscoped, so when §6.6's ledger
editors land, an in-app-authored correction absent from the next bundle's TSV is wiped (probed: P4
removed the app row)."* Confirmed in the tree: `importer/dna.py:759` is `DELETE FROM
credit_correction` with no scope, and decision 162 makes a models-only re-import the only import
that recurs. So an app-authored fix survives every *re-derive* and is destroyed by the next
*re-import*. The clause is literally true and reads as a stronger promise than it is. Separately:
"writing the TSV formats" does not say *where* — `/data/artifacts/<version>/` is read-only artifact
custody replaced on hot-swap, and the app's stores are Postgres tables.

**The decision (recommended).** The editors write Postgres rows carrying a provenance column
(`origin` in `{'bundle','household'}`, migration `0026`); the importer's `DELETE` is scoped to
`origin = 'bundle'`; the app exports household rows as TSV on demand so they can be folded back
into the corpus. §6.6's sentence is amended to say "survive every future re-derive **and every
re-import**", or narrowed to what is true.

**Cost.** One column, one scoped DELETE, one export route. Without it §6.6's clause is false and
decision 171 already says so.

### 327. Which path owns `is_owned = false`

**What the record says.** §7.2's third bullet makes *both* trigger paths responsible: "Both paths
enqueue an **acquisition job** (§8) per new title and mark removed titles `is_owned = false`"
(`spec:346`).

**Why it changes.** A delta poll on `DateCreated >` is an add detector and by construction never
sees a removal. And `connectors/resolve.py:311-324` (`prune_missing_items`) plus
`sync/seen.py:1117-1156` (`_falsify_ownership`) already refuse to falsify ownership on anything but
a complete library read, because "a Jellyfin outage, or a page-set truncated at the client's page
cap, is indistinguishable here from a library that genuinely shrank". So the delta path cannot
discharge the ownership half of its own bullet.

**The decision (recommended).** Ownership falsification stays with the existing full sweep alone
(shipped at M4.11, and named in §12's M4.11 criterion). §7.2's third bullet is amended in place to
say so, because a delta poll cannot observe an absence.

**Cost.** One sentence. The alternative is a second, weaker falsifier that the M4.11 work exists to
prevent.

### 328. "Unnamed taste residual share" — define it or strike feed 3

**What the record says.** §8.4 lists four feeds, the third being "titles whose 'unnamed taste'
residual share is high" (`spec:402`). `flywheel_item`'s CHECK already freezes the four kind values
including `unnamed_residual` (`0004_dna.sql:158-160`).

**Why it changes.** The phrase "unnamed" appears in the whole normative file **exactly once**, in
§8.4 itself. "Residual" appears at `spec:223` meaning something else entirely (the per-title Ledger
residual `b_i^u`). The vendored `ARCHITECTURE-extracts.md` carries §3 and Appendix C only (decision
294) and no nameable-variance metric. The corpus's own definition lives in a document this
repository cannot read, and CLAUDE.md's rule is that a requirement nobody here can read is not
normative.

**The decision.** Either vendor the corpus's nameable-variance definition into
`ARCHITECTURE-extracts.md` the way decision 294 vendored §3, or strike feed 3 from §8.4 and from
`flywheel_item`'s CHECK. **Do not implement a guess** — the kind value already ships in a
checksummed CHECK, and an invented definition is exactly what "a requirement resting only on a
proposal rests on nothing" exists to stop. Striking the CHECK value needs a new migration; leaving
it unwritten is free.

**Cost.** If struck: M5 feeds two of four kinds (`thin_facet` at M5.6, `empty_predicate` and
`uncovered_frontier` at M6) and §8.4 loses a sentence. If vendored: one document extract and one
threshold.

### 329. What makes a title "thin-facet"

**What the record says.** §8.4's fourth feed is "thin-facet titles" (`spec:402`). §14 risk 2 names
the measurement — "measure facet coverage of post-2025 titles" (`spec:485`) — and no threshold
exists anywhere.

**Why it changes.** Four different "thin"s are in play and they are not the same test:
`placement/features.py:90-109`'s per-block `is_thin` ("at least one block dropped, or one that hit
none of its declared columns", with `UNENRICHABLE_BLOCKS` excluded); §5.3's "2 lack keywords, 3 lack
any DNA row"; §4.1's forward reference to "§8's thinness test" which §8 never defines; and §8.4's
per-facet coverage.

**The decision (recommended).** `thin_facet` is a **different** test from `features.is_thin` —
extracted-tier facet coverage below N of the eleven v1 facets — with N stated so the queue is
falsifiable. §4.1's "§8's thinness test" reference is repointed at `features.is_thin` in the same
amendment, because it is talking about the block test.

**Cost.** One threshold and one amended cross-reference. Without it the fourth feed cannot be
implemented, and it is the one feed M5 can definitely supply.

### 330. Proposals 104, 107, 109 and 135 — adopted or struck, by number

**What the record says.** `spec_coverage.toml:3207` reads `spec = "§8 stage 6 + §9 (with
decision-doc proposals 107, 109)"` and `:3225` reads `"§8.4 (with decision-doc proposals 135,
104)"`. The guard's own note (`:7957-7958`) scopes itself around them: *"The two M5 rows still
citing bare proposals are past `current_milestone` and stay for the milestone that owns them."*

**Why it changes.** This is mechanical, not stylistic. Decision 295's guard fails a row at or
before `current_milestone` citing a bare proposal; decision 315 requires the escape to record a
debt rather than a form of words; and the guard reads a **list**, so a one-number repair cannot
turn green a row still resting on the other (`test_spec_coverage.py:1092-1094`). Renaming
`proposals` to `decisions` is foreclosed by `_laundered_decision_citations`
(`test_spec_coverage.py:1116-1126`). These two rows cannot go green until the numbers are adopted
or struck. The proposals' texts are at `spec-v2.2-proposals.md:1261` (104, the flywheel's batch
controls), `:1291` (107, the spend cap is display-only), `:1311` (109, parked jobs need retry /
retry-from-N / abandon) and `:1607` (135, the flywheel enqueues immediately and visibly).

**The decision.** Adopt or strike each by number, and where adopted, fold the clause into §6.6 or
§8.4 so the coverage row can cite the section instead. Proposal 135's substance is already
asserted by coverage row `...flywheel-enqueues-naming-failures` ("at the moment it happens …
not after a nightly job"), so striking 135 without amending §8.4 would leave the row's `what`
resting on nothing.

**Cost.** Four short clauses in §6.6 and §8.4. Not taking it means two M5 rows cannot go green,
which blocks M5.6's and M5.7's gates.

### 331. §12's M5 exit criterion is amended in place to name its second half

**What the record says.** "a new Jellyfin add reaches 'ready' unattended" (`spec:471`).
`docs/RELEASE.md:291-295` carries M5 as `NOT BUILT / none / no`.

**Why it changes.** The fourteen items under "The exit criterion is not a gate". M4.11's, M4.12's
and M4.14's rows all gained clauses in exactly this way, and M4.12's and M4.14's each name their
measuring script — "Measured end to end by `ops/m412_exit_criterion.py`, which refuses to run on a
fixture pool" and "`ops/m414_exit_criterion.py`, thirteen numbered checks, which refuses to run on
the fixture" (`spec:466`, `:470`).

**The decision (recommended).** Yes. Clause one stays verbatim; the row gains the clauses
`ops/m5_exit_criterion.py` will measure — the flywheel's enqueue-and-launch, the spend cap's
refusal, the two-attempt retry, the re-derive that keeps a curated correction, and the debounce
that yields one job for a burst. If decision 321 splits M5, each sub-milestone also gets a §12 row
in the shape M4.6, M4.7, M4.9-M4.12 and M4.14 already use.

**Cost.** Seven §12 rows and one exit script. What is bought: the release gate at
`docs/RELEASE.md:65` can be filled in by measurement rather than by "suite green" — the failure
M4.16 was written to close.

### Takeable as the plan executes

These do not block a milestone opening, but each must be answered before the step that needs it.

| # | question | owner |
|---|---|---|
| **332** | Where does `/events/jellyfin`'s token live, what does the route answer to a bad or absent one, and how is the `/events` namespace routed past the SPA fallback? | M5.1/M5.2 |
| **333** | Is the 10-minute debounce a fixed window per resolved title, a sliding window, or a per-scan quiet period, and where does its pending set survive a worker restart? | M5.2 |
| **334** | Which of §8 stage 2's eight sources are required for the stage to pass? | M5.3 |
| **335** | The exact reviews-gate predicate: which plot field, >=2 sources *and* >=50 words, 50 words of what, measured before or after `norm()` | M5.3 |
| **336** | What distinguishes `parked` from `failed`, and which admin actions exist on each? | M5.1/M5.6 |
| **337** | The map from run-agreement to `dna_tag.confidence`, stated so §4.1 rule 2 can be enforced against it — and whether agreement counts passes, providers, or runs | M5.5 |
| **338** | Does batch mode ship at M5? | M5.5 |
| **339** | Does §6.6's per-task assignment ship with all three tasks or with extraction alone? | M5.7 |
| **340** | Does §8's fetcher honour robots.txt and carry a declared User-Agent, as the ported layer does? | M5.1 |
| **341** | Does stage 7 record what it drops, and what does "low-evidence" mean given §4.1 rule 2's ban on a confidence filter? | M5.4/M5.6 |
| **342** | Is the third ledger editor (§6.4's per-facet axis TSVs) in M5 at all? | M5.6 |
| **343** | Where do model prices live — admin-entered or shipped — and what happens when they go stale? | M5.5/M5.7 |
| **344** | What does the flywheel do with a naming failure whose term is not in vocabulary v1 at all? | M5.6 |
| **345** | How does §6.6's board show a raw document without re-mounting `/data/raw` on the backend? | M5.1/M5.6 |
| **346** | Decision 171's spec amendment (§4.3's `dna_vocab/v1/` bullet, and a §10 sentence) never landed and is still owed. M5.3 touches exactly those clauses | M5.3 |

Each of these is argued in the plan that owns it, with the evidence and a recommended ruling. Two
are worth previewing here because they are easy to get wrong:

**332.** `SpaFallback.matches` declines only the `api` namespace (`app.py:520-524`), and the
fallback is registered `methods=["GET"]` (`app.py:545`). A POST to an unrouted `/events/...` path
is therefore a *partial* match and Starlette answers **405**, not 404 — the exact failure
`test_api_gating.py::test_the_spa_fallback_does_not_answer_for_the_api_namespace` was written to
catch for `/api`. §7.3 (`spec:353`) and §11 (`spec:438`) both also put a route under `/events`, so
this is a namespace and not one route.

**340.** The spec never mentions robots.txt or a User-Agent, and stage 2 names two *scraped*
sources (`rt:page`, `metacritic:page->reviews`, `spec:368`). The ported layer carries the
politeness — `mdc/http.py:5-7` lists "robots.txt fetch + honouring, per host, cached in the DB" —
and `mdc/config.py`'s `HOST_POLICIES` runs RT and Metacritic deliberately slowly. Not a defect to
fix; a clause to add, so the behaviour is falsifiable.

---

## What the paid stage costs, and what the cap actually gates

Stage 6 is the only paid stage. §8 gives ~20-27k input tokens per pass per title (`spec:384`); the
corpus measured billed output at **`MEAN_OUTPUT_TOKENS = 3900`** (`mdc/config.py:186`), which
includes reasoning tokens that never appear in the response — *"Estimating from the ~780 tokens of
JSON that actually come back would understate the bill roughly fivefold"* (`mdc/config.py:181-185`).

At the midpoint (23.5k in, 3.9k billed out), against `mdc/config.py:134-163`'s `PRICING`:

| provider / model | $/M in | $/M out | per title per pass | two passes |
|---|---|---|---|---|
| gemini-2.5-flash, **batch** | 0.30 | 2.50 | **$0.008** | $0.017 |
| gemini-2.5-flash, sync | 0.30 | 2.50 | $0.017 | $0.034 |
| gpt-5-mini | 0.25 | 2.00 | $0.014 | $0.027 |
| gemini-3.7-flash, batch | 0.75 | 3.75 | $0.016 | $0.032 |
| **gemini-3.7-flash, sync** (the corpus default) | 0.75 | 3.75 | **$0.032** | **$0.065** |
| claude-haiku | 1.00 | 5.00 | $0.043 | $0.086 |
| gpt-5.6-terra (OpenAI default) | 2.00 | 12.00 | $0.094 | $0.188 |
| claude-sonnet-5 (Anthropic default) | 3.00 | 15.00 | $0.129 | $0.258 |

**Two findings fall out of this table and both belong in the plan.**

1. **§6.6's published baseline rests on a retired model.** "~$0.005-0.01/title/pass on Gemini batch"
   reproduces only as gemini-2.5-flash at batch pricing — and `mdc/config.py:151-153` records that
   the 2.5 family *"still appears in ListModels but `generateContent` answers 404 'no longer
   available to new users'"*. Against the current default (gemini-3.7-flash, sync) the real figure
   is **~$0.032**, three to six times the number §6.6 prints. A cost estimator seeded from the
   spec's caption would be wrong by that factor on its first run. **Decision 343.**
2. **The Gemini price is dated.** `mdc/config.py:157-159`: gemini-3.7-flash's introductory pricing
   *"expires: from 2027-01-01 these double to $1.50 / $7.50. A corpus pass costed today and run in
   January would be twice the estimate."* That is roughly fifteen weeks from this document's date.
   Hard-coding prices puts a known-wrong number in the meter on a known date.

**Scenario arithmetic** (gemini-3.7-flash sync, two passes, $0.065/title):

| what | titles | cost |
|---|---|---|
| the unattended path — a household adding ~20 titles/month | 20/mo | **~$1.30/month** |
| a modest flywheel batch | 500 | **$32** |
| re-extracting the whole **owned** library | 839 | **$55** |
| a flywheel "select all" over the corpus | 19,071 | **$1,240** |
| the same, on claude-sonnet-5 | 19,071 | **$4,920** |

**What the cap actually gates — and it is not the exit criterion.** §12's criterion runs at roughly
a dollar a month; a household could leave the cap unset for a year and never notice. The cap exists
for **the flywheel's Launch button and the admin's manual retry**, where one click can select
19,071 rows. That asymmetry should shape the surface: the per-title estimate §6.6 demands "before
enabling" is reassurance, while the **batch total against the remaining cap** is the control, and
Launch must be disabled with its reason rather than warning after the fact.

Three second-order costs the estimator must carry or it will under-report:

- **The two-attempt retry is a paid call.** A contract violation costs a second full input pass, up
  to +100% on that title. Decisions 324 and 325 must agree on whether attempt 2 is budgeted inside
  the cap or checked against it.
- **Parallel mode multiplies by provider count, and pass count multiplies again.** Two providers at
  two passes is 4x the single-run figure — which is why decision 324 blocks.
- **Reasoning tokens are roughly half the billed output** on every model in `DEFAULT_MODELS`
  (`mdc/config.py:181-185` measures gemini-3.6-flash at ~1.6k output plus ~2.3k thoughts). A meter
  that counts the visible JSON reports about a fifth of the truth — which is coverage row
  `...spend-cap-meters-billed-tokens` stating the same thing from the test side: *"a cap computed
  from the wrong number is not a cap."*

---

## What M5 does not do

**Belongs to M6.** Compositional search — which is the *producer* of `flywheel_item.kind =
'empty_predicate'`, so M5 builds the feed's consumer and M6 supplies its input. The explore-frontier
cache (`Job("explore-frontier-cache", "M6", "nightly", "minutes", every=86400)` at `worker.py:1057`
is registered with no callable), which is the producer of `uncovered_frontier`. The Map's axis
scatter and the taste-comparison viz. The "query parsing" LLM task slot. §6.4's per-facet axis
surface. §13's "DNA coverage of naming events (queries answered vs sent to flywheel)" — the
numerator is compositional search, so the metric is uncomputable in M5 even once the flywheel
exists.

**Belongs to M7.** `POST /events/playback` and §11's other two seams (decision 290, `spec:353`). The
acquisition/"wanted" lens and the UMAP similarity lens. The Home Assistant hooks. The "conflict
phrasing" LLM task.

**Belongs to nobody — name it and move on.**

* **Re-crawling raw bytes for the ~19,000 corpus titles.** The export bundle at
  `data/export_bundle/v20260828/` contains exactly four things — `BUNDLE.json`, `artifacts/`,
  `content.sqlite`, `reviews.sqlite` — and **no `data/raw/`** (verified by listing it). §8's "All
  fetched bytes land in the app's own raw store, so re-parsing is free forever" (`spec:398`) is a
  promise about bytes M5 fetches *from M5 onward*; coverage row
  `...stage-park-retry-idempotent`'s "re-uses the content-addressed raw store" describes a store M5
  creates and fills, not one it inherits. Say so in the plan and in the row's `why`.
* **Vocabulary v2.** Decision 163 refuses two vocabularies in the tables; §14 risk 7 (`spec:491`)
  names §8.4's naming-failure queue as what will eventually ask for the migration, and decision 344
  is where that gets an answer.
* **The genome slice** (decision 170) and `ml_genome_*` re-import.

**A fact worth stating because it is not obvious and the spec never says it.** `themes.robots` **is**
a real vocabulary-v1 term. `artifacts/dna_vocab/v1/vocab_v1_all.tsv:166` carries it (582 terms plus
a header), split from `themes.artificial_intelligence` in the "gap-fill v1.1 2026-08-21" pass. So
§8.4's "breadth" means **coverage of existing terms across titles**, not new terms, and the apparent
circularity between a frozen vocabulary (decision 163) and a flywheel that grows breadth does not
exist. Without this sentence the flywheel reads as impossible.

---

## Sequencing

**Wave 1, serial: M5.1 alone.** It owns the seams, holds `current_milestone`, and writes `0024`.
Nothing runs beside it.

**Wave 2, four file-disjoint lanes, run two at a time.** The road to M5 has just demonstrated that
more concurrency costs integration work.

* **Lane A: M5.2 -> M5.3.** Both live in the Jellyfin/derive territory and M5.3 consumes M5.2's
  unmatched report shape.
* **Lane B: M5.4 -> M5.5.** M5.5's two-attempt retry needs M5.4's validator at runtime, though not
  at the file level.

**Wave 3: M5.6 in parallel with M5.7.** Both are frontend-and-its-router milestones over backends
that have stopped moving — the M4.15 precedent.

**Two serialisers that override the lanes**, unchanged from `ROADMAP-to-M5.md`:

1. **`spec_coverage.toml` carries one global `current_milestone`.** M5.1 holds it and owns every
   structural edit — `MILESTONES`, waivers, the §12 table, the spec file. Every other lane appends
   rows without raising the scalar.
2. **Migrations are numbered, applied and checksummed.** Take the number from the ledger above. No
   two migration-bearing milestones should be open at once inside one lane; across lanes A and B
   the numbers are pre-allocated (`0025`/`0026` to lane A, `0027`/`0028` to lane B) precisely so
   they cannot collide.

**One diff that fails the build by design**, budgeted inside the milestone that trips it:
`e2e/specs/18-system.spec.js:83-85` asserts `Object.keys(body).sort()` over `/api/admin/system`
equals exactly three facts, with the comment *"§6.6 also names queue depth, last syncs and logs, and
those are M5's"*. **M5.7 trips it** the moment it adds a fourth key. That is the contract working,
not a flake, and it may not be silenced with a waiver.

`e2e/specs/05-milestones.spec.js` is a **partial** exception to the usual "fails by design" rule and
the difference matters: its `PENDING` list (`:15-18`) carries only `/map` and `/taste`, both M6, so
**M5 has no surface placeholder to invert**. What it does carry is
`:96-118`'s "the admin Data, Connectors, Users and System tabs are all real", which asserts
`.tabs` renders no `/^M\d/` token — already true. So M5's e2e coverage is additive: the milestone
adds its own specs from scratch rather than inheriting a red one.

---

## What this plan could not determine

Stated rather than guessed, because an honest unknown is worth more than a confident guess.

1. **Proposal 135's and 104's adoption status.** Both texts were read (`:1607`, `:1261`) and both
   are provenance-only under CLAUDE.md. Decision 330 is the only way the two coverage rows resting
   on them go green; nothing in this document can take it.
2. **The real install's `acquisition_job` backlog.** `placement/reconcile._park_thin` is the table's
   only writer, so the parked set is however many titles the M4.13 sweep found thin on the 19,071-
   title bundle. That number was not measured here and should be counted on the real install before
   M5.3 sizes stage 2 — it is M5's stage-2 inbox on day one. Note also that
   `placement/features.py:49-52` records a measurement contradicting §5.3's "5 of 19" statement of
   the same backlog, so the spec's number is not a safe proxy.
3. **How far `mdc probe` reaches.** §8 calls it "the single-title prototype" (`spec:359`) without
   saying whether it covers all ten stages or only 1-5. `mdc/cli.py` carries a `probe` command; its
   body was not read. Worth reading before M5.1 designs the driver, because it is the closest thing
   to a reference implementation of the whole pipeline.
4. **Whether `§8 stage N` citations resolve for the coverage guard.** §8 has no §8.1/§8.2/§8.3 —
   only §8.4 (`spec:400`) — so "§8 stage 3" resolves to a code block rather than a heading, and the
   guard's "a §N the normative file has no heading for" rule (`spec_coverage.toml:7942`) may or may
   not accept it. Four shipped M5 rows already use the spelling, so it probably passes; confirm
   before the plan's other rows adopt it wholesale.
5. **Facet axis poles for `sensibility` and `era`.** §6.4 says they "need freshly authored poles"
   and the bundle ships none — `artifacts/dna_vocab/v1/` holds `vocab_<facet>_v1.tsv` and
   `vocab_pacing_axes_v1.tsv` and no per-facet axis file. Relevant only if decision 342 puts the
   third ledger editor in M5.
6. **The size of M5 against its predecessors, honestly.** The port surface is measured; the new code
   is not, because it has not been written. The estimate of "four to six M4.14s" is arithmetic over
   corpus line counts plus a judgement about the new half, and it is the least defensible number in
   this document. Treat the effort column as an ordering, not a schedule.
