# M5 — the umbrella: stages 5, 7 and 8 wired, the criterion measured, the scalar raised

> **This plan is binding once written.** The owner has instructed that every owner decision below be
> taken at its RECOMMENDED option, without asking, and recorded in `docs/spec-v2.2-proposals.md` in
> the register's form. The register is used up to 460; this milestone spends **461-467** (467 was
> taken when the plan was critiqued) and leaves 468-469 for its review cycle. M5.1-M5.7 have landed
> (main @ c3861a7). **This milestone holds `current_milestone`**, so it is the one that edits the
> spec file (decision 288), and it re-opens none of M5.1-M5.7's decisions.

§12's umbrella row (`docs/spielplan-spec_v2.1.md:472`) — "a new Jellyfin add reaches 'ready'
unattended" and decision 331's five clauses, "measured with it by `ops/m5_exit_criterion.py`" — names
a script that does not exist over a pipeline that cannot yet reach `ready`. Three jobs: wire §8
stages 5, 7 and 8; write the script; raise the scalar from "M5.1" to "M5" and close what that
exposes. No feature, no UI.

---

## 1. Why these are one milestone

They are one dependency chain, and each link is the next one's precondition. The umbrella row's
first clause cannot be measured until a title can walk past stage 6; the script cannot measure a
clause the pipeline cannot reach; and the scalar cannot rise until the umbrella row names a test,
whose only honest content is the script's own checks. Splitting them would ship a script that
stands stages down to pass, or a scalar raised over a row closed by something other than its
instrument. No sibling lane is open, so no file here is shared with anyone.

---

## 2. What does not exist today

### 2.1 Three stages are declared no-ops, and stage 6 parks every real title behind them

`acquire/pipeline.py:217`, `:223` and `:226` declare stages 5, 7 and 8 `implemented=False,
owner="M5.4"`; `acquire/stages.py:1452-1458` and `:1618-1634` return the stub marker. M5.4 built
`dna/packs.py`, `dna/verify.py` and `dna/project.py` and deliberately wired nothing (decision 387);
decision 432 recorded the wiring as owed. Stage 6 reads the pack stage 5 would store and parks
without one — its docstring says so at `stages.py:1540-1541`, as do `llm/extract.py:263`,
`flywheel/thin.py:22-23`, `pipeline.py:728`, `docs/RELEASE.md:17`, `:534`, `:548`, `:574-576`
and the §12 M5.6 row's last clause (`spec:470`). So no new add reaches `ready`, the thin-facet
feed (decision 440) never fires on a real install, and a flywheel Launch — which makes titles due at
stage 5 (decision 443, `test_flywheel_launch.py:193`) — parks every launched title at stage 6.

### 2.2 The package is ready, and it holds two traps for whoever wires it

* `craft.augment` (`dna/craft.py:375`) returns a `CraftInfo`, which `packs.store_pack`
  (`dna/packs.py:312`) cannot take, and `store_pack` refuses a `PackInfo` whose `sha`/`chars` are not
  the text's own (`:361`) — its docstring names exactly this mistake. Stage 5 must recompute them.
* `project.project_title` raises for any title whose origin is not `acquired` (`dna/project.py:177`).
  Bundle titles do reach stage 8 — an add resolving to an unplaced bundle title (decision 411 exits
  only placed ones), an admin retry of a `_park_thin` inbox row (decision 444), a Launch of either —
  and `title.origin` defaults to `'bundle'` (`0008_placement.sql:46`), which is what
  `test_llm_stage.py:348-368`'s title is. An unconditional call fails every such walk after stage 6
  has billed for it.

### 2.3 Stage 7 has nothing left to decide

Decision 432 put the verdict inside stage 6, because §9's retry must name its violation to a call
that has not returned: `llm/extract.py` asks `verify_payload` inside the two-attempt loop and writes
the tier only when every run was accepted, after `consensus.store_title`'s merge and
`apply_adjudications` in the same transaction. The board keeps each stage's detail — the upsert is
`detail = acquisition_job.detail || EXCLUDED.detail` (`pipeline.py:373`) — so a stage-7 detail
persists beside stage 6's for the whole walk.

### 2.4 Decision 444's paid-stage pre-check reads the flag this milestone flips

`acquire/actions.py:364` pre-checks the cap when "the first implemented stage at or after N" is the
paid one. `test_acquire_actions.py:493-516` asserts a retry from 5 is refused over the cap *because*
5 is a declared no-op. Once 5 has a body, a retry from 5 would be queued and park at 6 on the next
tick, and the M5.5 row (`spec_coverage.toml:4477`) says a manual retry of a parked stage-6 job over
the cap "is refused with that reason rather than queued".

### 2.5 The umbrella has a row and no instrument, and the scalar exposes exactly that

`spec_coverage.toml:11282` (`jellyfin-acquisition-eval-a-new-add-reaches-ready-unattended`) carries
no `tests`; `ops/` holds no `m5_exit_criterion.py`; `docs/RELEASE.md:618` records M5 `NOT BUILT`,
Blocking `no`. `current_milestone = "M5.1"` (`spec_coverage.toml:20`), and `MILESTONES` already
sorts `"M5"` after `"M5.7"` (`test_spec_coverage.py:416-418`). Simulated at `"M5"` — every read of
the map answered with the scalar replaced, through a scratch pytest plugin, over the fourteen test
files that name the map, nothing in the tree edited — 1041 passed and **two failed**:
`test_shipped_requirements_are_covered` (the umbrella row) and
`test_the_amended_rows_claim_of_the_current_milestone_is_read_by_its_guard` (`docs/TESTING.md`
opens no `**M5` block). That is the whole red list; every M5.1-M5.7 row already names tests.

### 2.6 Registered tests that fail by design the moment the stages get bodies

`test_flywheel_feed.py:244` and `:253`, both on the flywheel row (`spec_coverage.toml:5068-5069`);
the owner maps at `test_acquire_pipeline.py:152-158`, `len(stubs) == 3` at `:1496`, `:256`'s
`next(s for s in SHIPPED if not s.paid and not s.implemented)`, which raises once no stage is a
no-op, and `:2679`'s patch of stage 7, whose doctored stage copies `s.implemented` (`:2702`) and so
stops being a stub; the census at `test_worker_schedule.py:564-572` and `:1036`, which pin
`dna-projection` as awaiting its milestone. And one browser file's comment rests on the no-op:
`e2e/specs/21-connectors.spec.js:33-34`.

### 2.7 A title parked at stage 6 on a missing pack never goes back to stage 5

The board is the resume point, and a park re-enters earlier only through `reask_from`, which only
stage 4 declares (`pipeline.py:161-172`, `:568-570`). So wiring stage 5 helps new walks only. Every
title an M5.5-M5.7 install parked at 6 on "no DNA pack is stored" re-asks at 6 and parks again,
under a reason saying stage 5 "is not wired in this build" (`llm/extract.py:302`); and on any
install a title parked at 6 over the cap meets the same dead end once a bundle import changes the
active vocabulary, because `dna_pack` is keyed by version (0027). `pipeline.py:164-165` names this
case: a park "whose answer can only change if an EARLIER stage runs again".

---

## 3. Owner decisions this milestone cannot take alone

Each is taken at its recommended option and recorded under its number.

**461 — stage 5 stores the augmented pack.** Options: store the base pack; or the base with its craft
supplement. **Recommended: the augmented pack**, because §8 stage 5 is "ported packs.py … + craft
supplement" and decision 391 ships both halves. `stages.dna_pack` reads the active vocabulary
(`db/dna_terms.active_version`), calls `packs.build_pack`, then `craft.augment`, then
`packs.store_pack` inside one `conn.transaction()` with `entity_key=ctx.task.key` and
`run_id=ctx.run_id` (decision 345's board lists the document), storing the base `PackInfo` with
`chars` and `sha` recomputed from the augmented text. No active vocabulary parks with
`waiting_on_the_world()`, as stage 6's does; a missing title fails, as stages 3 and 4 do. The row
becomes `implemented=True` and keeps `owner="M5.4"` as provenance (`pipeline.py:200-208`): the body
is a call into M5.4's package.

**462 — stage 7 records the verdict stage 6 reached, and verifies nothing a second time.** Options:
re-check the stored tier; pass through; record the verdict. **Recommended: record it.** The stage
reads the title's extracted-tier tag count under the active version and the `dna_reject` rows this
walk's run filed for it, grouped by `rule_violated`, and advances with them; it calls neither
`verify_payload` nor a provider and writes no row. A re-check is refused on decision 432's ground and
a stronger one: the stored tier is post-merge and post-ledger, so it judges rows stage 6 never
judged, and a disagreement has no action §8 allows ("never repaired"). A pass-through is refused
because it is a stub flagged `implemented=True`, which the stub-marker guard decision 348 relies on
could never see, and because §2.3's merge makes this detail the board's lasting per-title record of
what the trust boundary dropped. §8's stage-7 line is amended to say where the verdict is reached.

**463 — stage 8 projects an acquired title and leaves a bundle title's projected tier alone.**
**Recommended**, and implied by `project_title`'s own refusal: for `origin = 'acquired'` it calls
`project_title`; for any other origin it advances with a detail naming decision 162 (the bundle's
projected rows are seeded once). `observes_coverage` stays on the row and the call stays in
`run_task` after the stage advances (`pipeline.py:942`), so the feed fires on both branches.
`worker.py:1234`'s `dna-projection` row gains `owner="spielplan.dna.project"`, the
`cold-tower-placement` precedent for a §5.3 row reached through the drain. No stage stays a declared
no-op; the stub-marker test asserts none does and still catches a body behind `implemented=False`.

**464 — decision 444's pre-check keeps its outcome once stage 5 has a body.** Options: keep 444's
predicate verbatim, so a retry from 5 is queued and parks at 6 with nothing billed; or restate it
over the same flags. **Recommended: restate it.** The pre-check runs when the first implemented stage
at or after N that is `paid`, `fetches` or declares `reask_from` is the paid one — nothing between the
click and the paid call fetches or holds a re-ask window. Outcomes are byte-identical to today's: 5
and 6 refused over the cap, every other N queued. The driver's gate stays the guarantee (decisions
325, 348); this keeps the M5.5 row true and `test_acquire_actions.py:493-516`'s assertions green and
unedited (its docstring's "a declared no-op" clause cites 464 instead). 444's entry gains one line
citing 464, as 431's does for 439.

**465 — `ops/m5_exit_criterion.py` measures the umbrella on the fixture, in-process, and CI runs its
checks.** Options: refuse the fixture like `m51`-`m53`; or `m55`'s shape. **Recommended: `m55`'s
shape.** Refusing would leave the umbrella unrunnable in any lane, and the population arguments
those scripts make — name collisions, the corpus tower, the real ledger — are theirs and already
measured by them. The umbrella's claims are compositional, and a fixture falsifies a composition.
The checks are functions over an install that both the script's `main()` and a test module build;
the test module's tests close the umbrella row. §12's M5 row gains how it is measured and which
clauses a real install signs; `docs/RELEASE.md`'s M5 row moves to `RUN, OUTPUT NOT COMMITTED` once
the lane has run it (decision 435's rule), Blocking `yes`.

**466 — decision 438's owed measurement is recorded against the real install; the budget does not
move.** 438 makes the milestone that wires stage 5 owe `worker.py`'s 420 s drain budget a stage-6
measurement. Options: measure on the double — a number about nothing, since latency is the
provider's; lower `DRAIN_LIMIT` or park on a worst case — 438 already refused the park (it parks the
default plan at 600 s) and a smaller limit is a guess; or record it. **Recommended: record it** as an
owed real-install check in `docs/RELEASE.md` §7 and the M5 block: time one drain tick with stage 6
live on decision 324's default plan, each attempt's `llm_call.at` against the tick's start, and say
whether `DRAIN_LIMIT` titles fit 420 s. `RELEASE.md:548` points there instead of at this milestone.

**467 — a stage-6 park re-enters at stage 5 once its own deadline has passed (§2.7).** Options:
leave the resume point, so each such title needs an admin retry from stage 5, found by reading a
reason that is false; add a field that re-enters only the no-pack park; or give stage 6's row
`reask_from=5`. **Recommended: `reask_from=5`**, the idiom `pipeline.py:161-172` wrote for this
case, with no new field. An expired stage-6 park then rebuilds the pack before the gate asks again:
one local build and one `raw_document` row per re-ask (the store is content-addressed, so an
unchanged pack writes no new file), never a paid call, since the gate still runs before stage 6. A
park made due early - an operator's retry, a Launch - still resumes at the board's stage (decision
421), so `test_llm_stage.py` and `ops/m55_exit_criterion.py`, which make parks due early over a
pack placed by hand, walk as before. It completes decision 432's "this title resumes here once a
pack is stored" rather than re-opening it; `llm/extract.py:302`'s reason is restated to name stage
5 and this re-entry, keeping the two substrings `test_llm_stage.py:595` and `:703` assert.

---

## 4. The work, in order

**Phase A — the three stages (`acquire/stages.py`, `acquire/pipeline.py`).** Stage 5 per decision
461, stage 7 per 462, stage 8 per 463; the three rows `implemented=True`, owners unchanged,
`observes_coverage` kept on stage 8. Rewrite the docstrings that argue the stubs: the module's "WHY
STAGES 5, 7 AND 8 ARE DECLARED NO-OPS" paragraph, `dna_extract`'s "until M5.4 wires stage 5", the
three stub docstrings, and the `pipeline.py:728` clause. Stages 5 and 7 are neither `paid` nor
`fetches`. Stage 6's row gains `reask_from=5` (decision 467), and the `reask_from` paragraph at
`pipeline.py:161` stops saying stage 4 is the only stage with one.

**Phase B — the pre-check and the registry (`acquire/actions.py`, `worker.py`).** Decision 464's
predicate at `actions.py:364` and the module docstring's sentence about it; decision 463's `owner` on
`dna-projection`. The `llm/extract.py:263` and `flywheel/thin.py:22-23` sentences go to past tense,
and `llm/extract.py:302`'s no-pack reason is restated per decision 467 - it is shown on the board.

**Phase C — the tests the wiring moves.** In `test_acquire_pipeline.py`, `test_acquire_drain.py`
and `test_flywheel_feed.py`, extend the autouse stand-down to stages 5 and 7 where a test is not
about them, in decision 432's pattern, with a `dna_live` fixture that puts them back; keep stage 8
live in `test_flywheel_feed.py`, whose subject it is. Move the owner maps' three stages to the
implemented map; make the stub test assert that no stage is a declared no-op, keeping its name and
its synthetic-body branch: `:2702` must pass `implemented=False` explicitly (it copies
`s.implemented`, True from now on), and the stub test must run its per-stage loop BEFORE its count
assertion, or the doctored build fails on the count and not on the sentence `:2679` asserts. `:256`
builds its free no-op synthetically, as `:254` builds its declared paid one.
Replace `test_flywheel_feed.py:244` and `:253` with tests that stage 8 projects an acquired title,
skips a bundle title, is still the only observing stage, and that the thin-facet row lands after the
projection. Update `test_worker_schedule.py:564-572` and `:1036` to the new census. New
integration tests: stage 5 stores the augmented pack under the task key and stage 6 reads it back
through `read_pack`; stage 7's detail equals the rejects filed by stage 6 and no second verdict is
asked (count `verify_payload` calls; stage 7 reads rejects by `run_id = ctx.run_id`, so a context
with no run reads none rather than every NULL-run reject the title ever had); a stage-6 park with
no pack and a passed deadline re-enters at 5 and stores one, while the same park made due early
resumes at 6 (decision 467). `test_acquire_actions.py:493-516`'s assertions stay unedited and green
(decision 464). `test_llm_stage.py:348-368`'s docstring loses "declared no-ops"; its assertions hold
only because stage 8 takes the bundle branch.

**Phase D — the instrument (`ops/m5_exit_criterion.py`, `backend/tests/test_m5_exit_criterion.py`).**
Section 7's checks, in `m55`'s idiom: one routing transport inside the real `acquire.fetch.Fetcher`
— the three provider hosts to `ops/fake_llm.py` over `httpx.ASGITransport`, every source host to a
subclass of `ops/m53_exit_criterion.py`'s canned `Web`, its host shapes and body builders imported
rather than copied — and the app over ASGI for `/events/jellyfin`, fed by `ops/fake_jellyfin.py`'s
emitter. **`m53`'s routes alone cannot reach `ready`**: its walk film serves exactly one review
source by design and parks at stage 4 (`m53:239-250`), and its one TMDB keyword, "exit criterion"
(`m53:284`), maps to nothing in the fixture's `alias_map_v1.tsv`, so stage 8 would project zero rows.
The subclass serves this script's own films: two review sources over `packs.MIN_WORDS` each, and a
keyword the fixture's map carries (`slow-burn`). **The test module shares no install between
tests**: `-n 16` under xdist's default `load` distribution spreads one module's tests over workers,
so a module-scoped install would be built once per worker and a clause test would run where the
check it depends on did not. Each clause test takes the suite's per-process `db`, builds the
script's install on it and runs the steps its check needs, in `test_m51_exit_criterion.py`'s shape;
only `main()` runs the checks in sequence on one scratch database. Plus `m53`'s statement-prepares
test.

**Phase E — the records.** The spec: §8 stages 7 and 8 per 462 and 463, leaving the `N  name`
columns verbatim (a test reads the ten names back); §12's M5 row per 465; the M5.6 row's "while
stage 8 itself stays a declared no-op" and "cannot run on a real install until stage 5 is wired"
and the M5.5/M5.6 paragraphs' present-tense "until stage 5 is wired"; a v2.1.4 Status line naming
what this wave folds. The register: a block "as M5 opened" with 461-467, no counts (decision 460).
`spec_coverage.toml`: section 6's rows. `docs/RELEASE.md`: the M5 block and §1 row per 465, §7's
owed check per 466, and `:17`, `:534`, `:548`, `:574-576`. `docs/TESTING.md`: a `**M5` block with
the amended-rows banner in the guard's form. The `21-connectors.spec.js:33-34` comment, re-derived
(section 9).

**Phase F — raise the scalar and verify.** `current_milestone = "M5"` in the same change set as
Phase E. Run every touched test file with `-n 16`, one process at a time; ruff; run the script to
exit 0 on the lane's cluster; check CRLF on every written file; then the Green agent's full run,
with `-rs`: none of its skips may be a test a row at or before M5 names, because the release
workflow's coverage gate fails on a named test that was skipped (`ops/coverage_gate.py:23-27`).

---

## 5. Schema changes

**None, and 0030 stays unclaimed.** Stage 5 writes `raw_document` (0024) and `dna_pack` (0027);
stage 7 reads `dna_tag` and `dna_reject` (0004, 0027); stage 8 writes `dna_projected` (0004); the
pre-check and the registry are code. Nothing needs a column.

---

## 6. Tests and the coverage contract

This milestone holds the scalar, so its red gate is §2.5's two failures, closed by the rows below.

**Row to add**, at `"M5"` under decision 321's rule — "a row may be filed at `"M5"` only if it is
measured by `ops/m5_exit_criterion.py`" — so its `tests` name the clause tests for checks 2 and 6
beside Phase C's, and its `what` also carries decision 467's re-entry, which check 2 measures:

```toml
[[requirement]]
id = "jellyfin-acquisition-eval-the-dna-stages-are-wired"
spec = "§8 stages 5, 7 and 8 + decisions 461, 462, 463 and 467"
milestone = "M5"
kind = "integration"
what = "Stage 5 stores the pack with its craft supplement under the task key and the active vocabulary, and stage 6 reads that pack back; stage 7 advances with the verdict stage 6 recorded and asks no second one; stage 8 projects an acquired title's keywords and leaves a bundle title's projected tier untouched; the thin-facet observation still fires after stage 8 advances; a stage-6 park whose deadline has passed re-enters at stage 5; and no stage is a declared no-op."
why = "Decision 387 shipped the DNA half as a package with no caller and decision 432 recorded the wiring as owed, so every real title parked at stage 6 on a missing pack and §12's M5 clause one was false on every install."
```

**Rows to amend**

* `jellyfin-acquisition-eval-a-new-add-reaches-ready-unattended` (M5): `tests` becomes the
  one-test-per-clause ids of `test_m5_exit_criterion.py`, and `why` loses "It lands with no tests".
* `jellyfin-acquisition-eval-flywheel-enqueues-naming-failures` (M5.6): its two stub tests
  (`:5068-5069`) re-pointed at their Phase C replacements, in the same change.

The M5.6 row is the one the `**M5` banner names; the umbrella row sits on the current milestone and
is added to rather than amended. **Raising the scalar turns red exactly** §2.5's two tests (re-run by
the critique over all fourteen files that name the map). After it,
two guards read M5 for the first time: any row carrying an "M5 review cycle" mark must be named in
the `**M5` banner (`test_the_testing_ledger_names_every_row_this_milestone_marked_as_amended`), and
`ops/coverage_gate.py` in the release workflow reads every M5.2-M5.7 row's Playwright ids. Those
are fourteen, in `18-system`, `20-admin-data` and `21-connectors`, and none carries a `test.skip`;
the one conditional skip in those files is the phone-only 48 px test, which no row names.

**Asserted where.** Integration: the stage wiring, the pre-check, the clause tests. Static: the
statement-prepares test; the exit-script sweeps in `test_static_contracts.py` apply to the new script
the moment it exists. E2E: none added — no surface changes, and `05-milestones.spec.js` holds no M5
placeholder to invert (`PENDING` is `/map` and `/taste`).

---

## 7. The exit criterion, as something to run

`TEST_DATABASE_URL=… backend/.venv/Scripts/python ops/m5_exit_criterion.py`

1. **Ready, unattended.** An `ItemAdded` for a film the bundle lacks, posted by the fake's emitter,
   filed by `intake.sweep_pending` once the window closes and walked by `pipeline.drain`, ends with
   the board `ready` at stage 10 and the title `acquired` above 1e9, with no admin route called
   between the add and `ready` and no admin action's reason ever on the board.
2. **The DNA stages, in that walk.** `read_pack` returns the augmented text; every tag carries its
   evidence; stage 7's detail equals the rejects stage 6 filed; stage 8 wrote projected rows. And a
   title parked at stage 6 with no pack under the active version and its deadline passed - an
   install upgraded from M5.7 - re-enters at stage 5 on the next drain and stores a pack (467).
3. **Retried once, named.** The same walk made two `llm_call` rows, and the double's log shows the
   violated rule and the offending value in the second request as it arrived.
4. **A second violation.** Under the double's `stubborn` posture stage 6 fails for good, the title's
   `dna_tag` count is unchanged and the board says no retry is coming.
5. **A naming failure, at the moment.** Check 1's thin title has its `thin_facet` row in
   `GET /api/admin/flywheel` when `drain` returns, with no job between.
6. **Exactly the selection.** Of three open rows, two launched through the route are the only rows
   `running` and the only titles due; the next drain re-extracts both from stage 5 under the batch
   plan, and the third row and its title are untouched. The three are fixture BUNDLE titles, their
   rows written by `flywheel.thin.observe_title` (the driver's own call) since no fixture title can
   pass stage 4 - every review is under the 50-word floor (`dna/craft.py`'s docstring) - and a
   Launch enters at 5; so the walk crosses stage 8's bundle branch, and both launched titles'
   `dna_projected` rows are byte-identical afterwards (463).
7. **A re-derive keeps the correction.** The fixture's shipped correction (title 8, its documents
   first fetched by a walk from stage 2) and a household one written through
   `POST /api/admin/curated/corrections` on check 1's title each stand after a retry from stage 3.
8. **One job for the show.** Twelve episode `ItemAdded`s for one series in one window file one task
   for the show and none for any episode.
9. **The cap parks and never auto-retries** — last, because it closes the month. With the cap set to
   the month's own spend (`m55`'s idiom), a fresh add parks `over spend cap` at stage 6 with zero
   requests to the double, and the next tick the same.

**It refuses (exit 2)** with no Postgres to create a scratch database on, or without either double;
it creates and drops its own database and data directory, removes every connector and provider key
from the environment unread, and routes every transport in-process, so an escaped request fails on a
name that does not resolve. Exit 0 all held, 1 one failed. **Only a real install signs:** clause one
against a real Jellyfin and its Webhook plugin, real source sites, a real provider's billed call, the
corpus tower and a wall-clock window; clause five against the corpus's own ledger, which
`ops/m53_exit_criterion.py` measures on the real bundle; and decision 466's drain timing.

---

## 8. What this milestone deliberately does NOT do

* **No feature and no UI.** No "pack unchanged, skip the extraction" short-circuit: a retry from any
  stage at or before 6 re-extracts and bills under the cap, as M5.6 designed it.
* **It re-opens no M5.1-M5.7 decision.** 432 stands (the verdict is stage 6's, and 467 completes its
  "resumes here once a pack is stored"), 438 stands (the budget does not move; 466 defers its owed
  measurement to the real install), 444's rule stands (464 keeps its outcome), 162 stands (no bundle
  title is re-projected).
* **It does not fold the owed §6.6 and §9 sentences** decisions 324, 337 and 338 mandate; they stay
  normative from the register under CLAUDE.md. That is prose, and the owner bounded this run.
* **It adds no count of the tree** to README, the register, `docs/TESTING.md` or `docs/RELEASE.md`,
  and no guard holding one (decision 460). It re-pastes no coverage block.
* **It does not weaken either refusing double**, and it does not touch M5.5's cap gate (decisions
  325, 348, 436) or the layering baseline.

---

## 9. Risks and gotchas

**The two traps of §2.2 are the likeliest defects.** Stage 5 passing `craft.augment`'s info, or the
base `PackInfo` unchanged, makes `store_pack` raise for every title; stage 8 calling `project_title`
unconditionally fails every bundle walk after stage 6 has billed, starting with
`test_llm_stage.py`'s.

**The first walks past stage 6 on a real install will be Launches of bundle titles**, through stages
9 and 10 that were only ever exercised on acquired titles. An unowned launched title parks at 10 on
"no ownership flag" and is re-asked daily at the cost of two column reads, never re-billed, because
the board resumes it at 10. Worth one test; not worth a decision.

**The e2e stack's premise moves.** `21-connectors.spec.js` stores a fake Gemini key and a cap and
says no provider is called because stage 5 is not wired. After this milestone what keeps a provider
uncalled there is that no title passes stage 4 on that stack; confirm it from the fake's library and
the e2e bundle before restating the comment. If it holds, **phase 1 prints 8 passed and phase 2 234
passed / 0 failed / 10 skipped, unchanged.** If a title can pass, stage 6 sends a fake key to Google.

**Wiring stage 5 lets stage 6's replace reach a bundle title's extracted tier unattended.**
`consensus.store_title` replaces a title's tier (decision 337), and decision 411 lets an unplaced
bundle title walk - which the first add of an unowned cold corpus title is, since the nightly sweep
places owned titles only (`worker.py:697`). So that add now bills an extraction and replaces the
corpus's rows, which decision 162 seeds once. A Launch or a retry does the same by design (443,
444); the unattended add is new with this milestone. Not re-opened here: it is the owner's call.

**Money on a real install is bounded but real.** Decision 438's cancelled walk pays for calls it
cannot keep, and wiring stage 5 makes that path reachable for the first time; the cap bounds it and
466 measures it on the owner's box.

**Importing `ops/m53_exit_criterion.py`** runs its module-level environment setup; set the same
variables first, as `m55` does before importing `m45`. The exit-script sweeps and the
env-neutralisation guard read the new script the moment it exists. Every file here is CRLF.
