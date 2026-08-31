# M3 — Rank: tiers, filters, drag-drop, comparison queue

§12's row: **"Rank view: tiers, filters, drag-drop, comparison queue"**, exit criterion
**"stable tier lists both users endorse"**. Branch `m3`, off `main` at `95f1b0f`.

Sections read in full before writing this: §6.3, §4.1, §4.2, §5.2, §5.3, §6.7, §12, §13, plus
§0 rows 6 and 21 (the measured null on profile pair-selection, and the tier list as learned
cutpoints). Proposals were read for context only; where a coverage row already cites one, the
row is the contract and is built to (listed below).

---

## Pre-flight

Working tree clean, on `main`, `pytest backend/tests` with Postgres up and
`current_milestone = "M2"`: **678 passed, 1 failed in 13m 35s**.

The one failure is not M2 work coming apart. `test_connector_registry.py::test_a_secret_
without_secrets_key_refuses_rather_than_falls_back` constructs `Settings(...)` directly to
prove §2's "refuses rather than falls back", but `Settings.model_config` carries
`env_file=".env"`, and this machine has the `.env` the README tells you to create — so
`SECRETS_KEY` arrives from the dotenv file, the connector seeds happily, and the expected
`RuntimeError` never comes. With `SECRETS_KEY=` blanked in the environment the whole file
passes (15 passed). The test is green in CI, which has no `.env`, and red on every developer
machine that followed the README. It is a hermeticity defect in an M0 test, not a regression;
the fix is `Settings(_env_file=None, ...)` in that one constructor. **Question 1 below.**

`test_spec_coverage.py` was green at M2 (33/35 · 10/10 · 25/25).

---

## Opening the milestone

`current_milestone = "M3"` → the gate lists the nine rows the map already owed. Reading §6.3
and §12's M3 row clause by clause against the map found **four more**, added before any code:

| added row | the clause the map had no row for |
|---|---|
| `tonight-rank-board-filters` | §12 names "filters" among M3's contents; §6.3 names six dimensions, two of which (runtime, DNA facet/term predicates) the M0 catalog filter does not have |
| `tonight-rank-neighbourhood-badge` | §6.3: "Badge shows tier + neighbourhood (\"A — between Heat and Prisoners\")" |
| `tonight-rank-cutpoints-learned-not-percentile` | §6.3: "learned cutpoints, **not percentile cuts** — initialised from … the measured quantile shape, then learned" |
| `tonight-rank-queue-answer-sharpens` | §6.3 names the control ("sharpen my ranking") and §12 lists the queue as M3 contents; the selection policy and the stored arm each had a row, nothing asserted a person can reach the queue at all |

**M3 is 13 rows.** Same routine that added M1's tenth row and M2's two.

### Waivers re-read

All three standing waivers still hold, checked rather than assumed:

- `platform-backup-rotation-and-ciphertext` (M0) — `grep -rn "backup\|pg_dump" ops/ docker-compose.yml worker.py .github/` finds only the two volume mounts. Still no job to assert against.
- `data-rules-platform-rating-display-only` (M0) — `grep -rni "create role\|grant \|revoke " backend/migrations/` is still empty. No feature-builder role exists, so nothing can raise `insufficient_privilege`.
- `library-rate-model-line-no-bundle` (M0) — still unreachable: with no bundle there are no titles, so there is no card. M5.

**Nothing in M3 is proposed for waiver.**

---

## The rows, grouped by kind, in build order

Cheapest layer that can falsify the rule. The pure-numpy rows first, because every surface
below them reads their output; the queue's selection policy is pure and gets tested pure.

### Slice 0 — schema and constants (no row of its own; everything below needs it)

- `backend/migrations/0012_rank.sql` — a new file, never an edit to an applied one.
  `ledger_cutpoints.refit_requested_at timestamptz` (decision 11's "queues a refit for that
  user alone", made observable) and a partial index for the worker's sweep. Asserted by
  `test_schema_contracts.py` (PGlite) the way 0010 and 0011 are.
- `ledger/hyperparams.py` — `straddle_z` already exists (1.0). Add `tension_credible_mass`
  (0.80) beside it, with its reason, so §6.3's "disagrees strongly" is one re-tunable number
  and not a literal in a board renderer. Both land in `_POSITIVE` validation.
- `tests/fixtures/make_bundle.py` — ship `straddle_z` and `tension_credible_mass` in the
  fixture's `ledger_hyperparams.json` (it ships neither today, so "read from the bundle" is
  currently unexercised), plus `break_straddle_z()` writing a non-positive value.

### backend (pytest, no database) — 5 rows

1. **`tonight-rank-cutpoints-learned-not-percentile`** — §6.3 tiers, §5.2 arm 3.
   *Touches:* `ledger/model.py` (`initial_cutpoints`, `MEASURED_TIER_SHARES`, `tier_of`) —
   read-only; the test asserts the existing behaviour and pins it. A lopsided board keeps its
   fitted boundaries instead of re-deriving the measured shares.
2. **`tonight-rank-neighbourhood-badge`** — §6.3 badges.
   *Touches:* new `backend/spielplan/rank/board.py` (pure badge assembly over
   `refit.Row`), `ledger/model.py` untouched.
3. **`tonight-rank-straddle-equals-eligible`** — §6.3 badges/queue (proposals 157, 76).
   *Touches:* `ledger/model.py::straddle` (already returns −1 for "no neighbour" and never
   returns the title's own tier, so proposal 76 falls out); new `rank/queue.py::eligible`,
   which is the *same* predicate — one function, called by both, which is what makes the row's
   "same set" true rather than asserted.
4. **`tonight-rank-tension-not-snapback`** — §6.3 drag-drop (proposal 71).
   *Touches:* new `rank/board.py::tension`, `ledger/hyperparams.py`.
5. **`tonight-rank-queue-selection-mix`** — §6.3 comparison queue.
   *Touches:* new `rank/queue.py::draw` (seeded `random.Random`, the way
   `rate/battle.py::draw` is, so a long draw is testable without a database).

### integration (pytest against Postgres) — 6 rows

6. **`tonight-rank-drop-writes-observations`** — §6.3 drag-and-drop.
   *Touches:* `ledger/observations.py` (`record_tier_edit`, `record_duel`) unchanged; new
   `rank/drop.py` composing them — exactly one `tier_edit(via='drag_drop')`, and for a
   drop *between*, two `duel(context='tier_insert', margin=NULL)` against the two new
   neighbours. `observations.record_tier_edit`'s docstring already says the neighbour duels
   are the caller's job "because whether there were neighbours is a fact about the board".
7. **`data-rules-cutpoints-tier-set-per-user`** — §4.2 (decision 11).
   *Touches:* `0005_ledger.sql`'s existing `cutpoints_length` CHECK (asserted, not changed);
   new `rank/tiers.py::save_tier_set`.
8. **`tonight-rank-tier-set-per-user-reinit`** — §6.3 tiers (decision 11). Same code, the
   other half of the sentence: equal-mass quantiles of *that user's* fitted `s`, refit queued
   for that user alone, `tier_edit` rows intact, other users untouched.
9. **`jellyfin-acquisition-eval-uniform-holdout-stream-never-tunes`** — §13 stream (a).
   *Touches:* `ledger/observations.py::load_observations` already excludes
   `selection = 'uniform_holdout'` (and `test_the_uniform_random_held_out_duels_never_reach_
   the_fit` already proves it — an M2-era test with no row, which this row will finally name).
   What is missing is the other three clauses: the selector must not read held-out rows,
   **an evaluation read path must exist and admit only them**, and no held-out pair may be
   stored or narrated as boundary-targeted. New `rank/evaluation.py`.
10. **`map-taste-admin-log-names-selection-arm`** — §6.7 + §13.
    *Touches:* `home/rail.py` (a `duel_line` shape, which the module does not have — the
    duel line lives inline in `observations.record_duel`'s `Write.log` today and already
    names `{context}/{selection}`; this row makes it a named shape with a test).
11. **`tonight-rank-board-filters`** — §6.3 Filters (+ §4.1 rules 1, 2, 5).
    *Touches:* `db/library.py::_filters` — extended with `runtime_max` and a DNA predicate
    over the sanctioned `dna_tagged` view (0004), which is the only union that keeps the
    `tier` discriminator. Extending the *shared* builder rather than writing a second one is
    deliberate: §6.0's count/listing invariant ("the same predicate") is why `_filters` was
    extracted, and a Rank-only copy would re-open exactly that bug.

### e2e (Playwright, real stack) — 2 rows

12. **`tonight-rank-tap-to-tier-and-cancel`** — §6.3 phones (proposals 74, 75). `phone`
    project (iPhone 13, WebKit) is the primary form factor.
13. **`tonight-rank-queue-answer-sharpens`** — §6.3 comparison queue + §12 M3.

New spec file `e2e/specs/13-rank.spec.js`, numbered into the existing sequence (specs are
filename-ordered, stateful, one worker). `e2e/specs/05-milestones.spec.js`'s `/rank`
placeholder assertion is deleted and replaced by a "Rank is built — M3 landed" test, exactly
as M1 did for Account and M2 for Rate.

### The surface, and two contracts it drags in

- `backend/spielplan/api/rank.py` — thin, like `api/rate.py`: HTTP shapes only, every rule in
  `spielplan/rank/`.
- `ops/devstub.py` — **`test_devstub_contract.py` fails the build** if the harness misses a
  real route or invents one. Every new `/api/rank/*` path has to land there too.
- `frontend/src/routes/rank/+page.svelte` + `frontend/src/lib/rank.svelte.js` (Svelte 5 runes,
  colocated `rank.svelte.test.js`), and the tier-set control on `/account`.

---

## Spec ambiguities, and the resolution proposed for each

§6.3 is four bullets long and specifies a surface; these are the places where it stops short.
Each resolution is the smallest choice consistent with the spec's register.

1. **What makes a pair "posterior-straddling"?** §6.3 gives the 70/20/10 mix and never says how
   a straddling *title* becomes a *pair*. → Draw a straddling title uniformly from the eligible
   set, then pair it with the nearest title in `s` **on the other side of the cutpoint it
   straddles**. A straddling title paired with an arbitrary partner tests nothing about the
   boundary, so this is the only reading under which "boundary-targeted" means anything.
2. **What is "exploration"?** §6.3 names the 20% and nothing else. → The title with the fewest
   comparisons among the *non*-straddling eligible titles (ties broken by largest σ), paired
   with its nearest neighbour in `s`. It is σ-reducing like the boundary arm but away from a
   cutpoint, and it is distinct from the uniform arm, which is σ-blind by construction.
3. **What pool is the uniform 10% uniform over?** → Unordered *pairs*, weighted the way
   `rate/battle.py::draw` weights strata by `n(n−1)/2`. Uniform over strata is a different and
   wrong distribution; `battle.py`'s docstring already argues this at length.
4. **"tier + neighbourhood" — neighbours within the tier, or across the board?** → Within the
   tier. The badge already names the tier; a neighbour from another tier would contradict the
   letter beside it. Intra-tier order is by ledger `s`, which is the order the board renders.
5. **"disagrees strongly" = outside the 80% credible interval** (the row's words, from
   proposal 71). A tier is an interval between cutpoints and the posterior is an interval on
   `s`, so → *disjoint*: the assigned tier's `[cut_{k−1}, cut_k)` does not intersect
   `[s − z·σ_eff, s + z·σ_eff]` with `z = Φ⁻¹(0.9) ≈ 1.2816`. A one-level disagreement whose
   bands still overlap is not tension, which is exactly what the row's second clause says.
6. **Where a title renders when the model and the person disagree.** §6.3: "shows the tension
   rather than snapping back"; the row: "stays in the assigned tier". → **The most recent
   `tier_edit` decides placement; the model's `tier_of(s)` decides it otherwise.** When they
   agree — the normal case once the refit absorbs the edit — they are the same number and
   nothing is decided. When they disagree the person's placement stands, weakly with no badge
   and strongly with one. Snapping back is the failure the clause forbids.
7. **"queues a refit for that user alone".** No generic job queue exists (`worker.py` runs
   interval jobs; there is no job table but `acquisition_job`). → `ledger_cutpoints.
   refit_requested_at`, set on save, cleared by the refit, swept by a short-interval worker
   job. The *re-initialisation* to equal-mass quantiles is immediate arithmetic over the
   already-fitted `s`, so the board is correct the moment the save returns; only the refit is
   queued. **Question 2 — the alternative is a synchronous refit inside the save.**
8. **Where the tier-set control lives.** Decision 11 says "the per-user settings page, not
   Admin"; v2.1 has no §6.9. → `/account`, which is the only per-user settings page that
   exists and already holds passkeys, PIN, onboarding and the Jellyfin link.
   **Question 3.**
9. **Board membership: "every rated title".** → `ledger_state.observed`, which is exactly "the
   person has an observation of some arm on this title". Unrated owned titles have a
   coordinate (M2's exit criterion) but are not on the board.

---

## v2.2 proposals that collide with this milestone

**The map's M3 rows already adopt eight of them**, and since a row's `spec`/`what` is the
contract, those are built rather than deferred: **157** (one predicate for badge and queue,
thresholds in `ledger_hyperparams.json`), **76** (no `S/S` at the ends of the scale), **71**
(the tension badge and its 80% CI), **74** (the phone lift is cancellable), **75** (the lift's
affordances), **146** (the held-out arm identifiable end to end), **54b** (§13's guard is
non-negotiable), **decision 11** (tier set per user, and the re-init rule).

Two more are load-bearing and I intend to honour them because §6.3 is unbuildable otherwise,
not because the proposal says so:

- **73 — the comparison queue's screen.** The document's own "prototype holes" list ranks this
  #1: policy, handler and pair data exist in the prototype with *zero* template bindings. §6.3
  names the control and §12 lists the queue, so a screen has to be invented either way.
- **120 — the log line that lies.** The prototype's `cqAnswer` asserts boundary-targeting
  unconditionally; row `map-taste-admin-log-names-selection-arm` is that defect written as a
  requirement.

**Not building** (proposals, not spec; recorded here so the omission is a decision):
**72** (facet-qualified predicates — actually *subsumed*: my added filters row requires
`mood.cosy` to match, because §6.3's own example is written that way), **77** (`via='explicit'`
needs a producer — **question 4**), **78/79/81/82/83** (badge placement, resolution readout,
board heading, best-first order, filter control shape — cosmetic copy; I will render the board
best-first with empty tiers kept, because §6.3 lists tiers ascending and a board that hides
empty tiers has no drop target for them, and I will not add the `+0.012 within-liked` gauge),
**80** (empty and not-yet-tiered states — I *will* build these two; §3.1's register makes an
explicit "nothing here yet, and why" state the house style, and the M2→M3 handoff is otherwise
a page of seven empty strips).

---

## What I am not touching

- `main`. Everything lands on `m3`.
- Any applied migration. `0012_rank.sql` is a new file.
- The two static guards (weights are never filters; the two DNA tiers are never unioned). The
  new Rank SQL is written under both — the DNA predicate goes through the sanctioned
  `dna_tagged` view, and no weight column appears in a comparison.
- Existing tests. Nothing gets loosened; the only existing test I propose to *change* is the
  hermeticity fix in question 1, which strengthens it.

---

## Decisions taken with you

Four questions went out with the plan; all four came back on the recommendation.

1. **The pre-flight failure is fixed inside M3** — `Settings(_env_file=None, ...)` in
   `test_a_secret_without_secrets_key_refuses_rather_than_falls_back`. It strengthens the
   assertion; a green local run now means what a green CI run means.
2. **"Queues a refit" is a marker column plus a worker job** — `ledger_cutpoints.
   refit_requested_at` in 0012, swept on a short interval. Re-initialisation stays immediate.
3. **The tier-set control lives on `/account`** — the only per-user settings page there is.
4. **`via = 'explicit'` keeps no producer at M3** — no §6.3 clause asks for one and no row
   names it; it is reported as a spec defect instead of invented here.

## Decisions made without you

The nine ambiguities above were resolved as proposed. These are the ones that came up during
the build and were too small to stop on.

1. **A failed boundary draw falls through to exploration, and never into the held-out arm.**
   A pool with no straddler has nothing to target, so exploration is the honest answer and it
   reports itself as exploration. The held-out arm receives no fallback in either direction:
   its *rate* has to be independent of the model's confidence, or §13's stream stops being
   independent of the thing it audits.
2. **Badge precedence: tension replaces the straddle chip while it holds** (proposal 71's
   rule). Eligibility is untouched — that is a property of `straddles()`, not of which string
   rendered.
3. **A drop's neighbour duels carry outcomes.** `above` beats the dropped title and it beats
   `below`. A pair written without a winner stores the geometry and throws the judgement away,
   which is the only reason §6.3 asks for them.
4. **One neighbour is one duel.** §6.3 says "between two titles" and is silent about the first
   and last slot of a tier. Refusing those would make them undroppable; inventing a second duel
   would put a comparison in the Ledger nobody made.
5. **`duel.selection` stays at 0005's default for a drop's neighbour duels.** The column's
   values are statements about *adaptive* selection and a drop is not adaptively selected;
   `context = 'tier_insert'` is what says where it came from.
6. **A relabel at the same K keeps the learned boundaries and queues nothing.** Decision 11's
   re-init exists because "changing K invalidates that user's boundaries"; discarding a fitted
   board because somebody preferred a different letter would be the rule doing more than it says.
7. **Tier sets are 2–12 levels, and labels at most 24 characters.** Neither bound is in the
   spec. Two because a one-level set orders nothing; twelve because every level needs
   observations before its cutpoint is anything but its prior. The label bound came out of the
   review — see below. All three are refusals, not clamps.
8. **The queue pair is sealed, not stored.** `itsdangerous` under `SESSION_SECRET` with its own
   salt, rather than a row: the client cannot name its own §13 arm, and no table is invented for
   a value the observations already imply. The review then made it single-use the same way — see
   below.
9. **The queue's pool is the whole rated board, unfiltered.** §6.3's filters are a way of
   looking at the board; a queue restricted to what the person last typed would sharpen one
   corner of it, and proposal 157's badged-set-is-queue-set identity would hold only there.
10. **`tests/pglite/apply.mjs` now reports columns and indexes.** It could not see an `ALTER`,
    so 0010's six columns and 0012's one were invisible to the schema layer — a migration that
    applied cleanly and added nothing would have passed every assertion in the file.
11. **`13-rank.spec.js` runs on both e2e projects**, seeding a member per project, because §6.3's
    tap-to-tier is a phone gesture and its drag-and-drop is a pointer one.
12. **The drop route carries the board's filters**, so a drop under a filter answers with that
    board rather than silently clearing it.
13. **`TIER_THRESHOLD = 30`** for proposal 80's "not yet tiered" copy. Invented (the proposal's
    own number); the board renders regardless, so it is a nudge toward Rate and not a gate.

### Added by the adversarial review

14. **The board clamps an out-of-range `assigned_tier` once, at the edge.** Decision 11 keeps
    `tier_edit` rows across a change in K, so a level outside `0..K-1` is guaranteed; the fit
    clamped it and the board did not, and `_band` indexed off the end of a shrunk cutpoint
    array. Clamped in one place because the bucket and the badge must not disagree.
15. **The queue seal is single-use**, via the count of comparisons the person had answered when
    it was drawn. A replay is §6.1's 409. No column: the observations already carry the number.
16. **`held_out_agreement` abstains on a model tie and joins both sides on kind.** `s_a == s_b`
    is the model declining to order the pair, and folding it into "B" is the same unmeasured
    threshold the module already refuses for the person's tie. Both sides on kind because
    §10's re-import can flip `title.kind` under a duel that was same-kind when written.
17. **Proposal 75's footnote is amended rather than quoted.** "each move writes a tier_edit plus
    two duels" is true only of a drop between two titles; the surface now says "plus a duel
    against each new neighbour", which is true of every move it can make.
18. **The board's title chips are `.tile`, not `.poster`.** `design.css`'s global `.poster` is
    the 2:3 card, and borrowing the name gave every text chip `aspect-ratio: 2/3` and
    `overflow: hidden`.
19. **The kind switcher is the house `.pill` with `aria-pressed`,** not a `role="tab"` with no
    tabpanel; the tier rows are `role="group"`, not `role="listbox"` over buttons.


---

## Exit-criterion evidence

§12's M3 row: **"stable tier lists both users endorse"**. Measured by
`ops/m3_exit_criterion.py` against the stack `node e2e/run.mjs` had just built and imported a
bundle into. Two members were created and rated in deliberately opposed patterns
(`[2,2,1,0]` and `[0,1,2,2]` cycled over the queue).

```
  liked-first    6 titles on the board, tiers occupied: [4, 5]
  disliked-first 6 titles on the board, tiers occupied: [2, 3]

  1. PERSONAL      Spearman rho between the two orderings = -0.657 over 6 shared titles
  2. STABLE        same request twice, no observations between: 0 titles moved, both people
  3. SHARPENING    liked-first    first 10 comparisons moved 0 titles; next 10 moved 2
                   disliked-first first 10 comparisons moved 1 title;  next 10 moved 0
  4. §13's FIGURE  liked-first    held-out pairs 1, decisive 1, agreement 1.00
                   disliked-first held-out pairs 3, decisive 3, agreement 1.00
```

**What this shows.** The board is *per person*: two people rating in opposite orders get
orderings that anti-correlate at ρ = −0.657, which is the property §5.1's β = 0.8 blend and the
per-user Ledger exist to produce — a household with one board would read ρ = +1.0. And it is
*stable* in the narrow sense the word can be tested in: re-reading moves nothing, which is not
free (a board that re-derived its cutpoints per render, or sorted client-side, would drift).

**What this does not show, and why.** Three things, stated rather than glossed:

1. **The board is six titles.** The synthetic fixture carries eight titles, six of them films —
   `tests/fixtures/make_bundle.py` reproduces §4.1's landmines, not a library. §6.3's own
   budgets are "~10–20 comparisons place a new title" and "the first *stable* tier list arrives
   at ~1,500–3,000 comparisons"; nothing at this scale speaks to either. Row 3's numbers (0
   then 2, 1 then 0) are noise on a six-row board, not a convergence curve.
2. **§13's agreement figure reads 1.00 on n = 1 and n = 3.** That is why the script prints the
   n beside it and why `Agreement.rate` is `None` rather than `0.0` on an empty sample. §0
   fixes a noise floor of 0.003–0.008 Spearman and calls anything smaller a tie; a rate over
   three pairs is not a measurement.
3. **"Endorse" is not measurable here at all.** It is two people looking at their own board and
   saying it is right. §12's M2 row has the identical limit — "50–100 verdicts each produce
   visibly personal rankings" is a claim about a household — and M2 shipped the machinery and
   the test that the loop closes rather than pretending otherwise. This does the same: what is
   above is the machinery being personal and stable, and the endorsement is the household's to
   give against a real corpus bundle.
