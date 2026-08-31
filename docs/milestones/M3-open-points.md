# M3 — open points

Everything M3 left open, in one place. Written at close (branch merged to `main` at `dc129dd`,
coverage map 15/15, all six test layers green).

Three kinds of thing are collected here and they are **not** interchangeable:

- **§1 Decisions** — questions only the owner can settle. Work is blocked or guessing without them.
- **§2 Spec defects** — places v2.1 is wrong, silent, or self-contradictory. Input to v2.2.
- **§3–4 Known defects** — real problems, found and deliberately not fixed, with the reason.
- **§5–6 Debt and waivers** — weaker-than-they-look tests, and the standing waivers.

Everything in §3 and §4 came out of M3's adversarial review (six lenses, then skeptics told to
refute each finding). What is listed here **survived** a skeptic; the refuted findings are not
here. Where a skeptic shrank a finding, the shrunk version is what is written down.

---

## 1. Decisions needed from the owner

### 1.1 The straddle badge names which tier?

**The conflict.** §6.3 says *"a straddling title shows \"A/S\""* — and in F/D/C/B/A/A+/S, A is
index 4 and S is index 6, so the spec's own example is **two tiers apart**. Proposal 76, cited by
the coverage row `tonight-rank-straddle-equals-eligible`, says the badge *"names the single
**adjacent** tier that exists (S straddles down to A+; F straddles up to D)"*, and the row
repeats the parenthetical: `(S/A+, F/D)`.

**What shipped.** `model.straddle` returns the tier the posterior's end actually lands in. At a
realistic σ that *is* the adjacent tier, so the row's parenthetical holds and its test passes.
At a wide σ — routine on a young board, where the residual prior puts σ near 1.0 against
cutpoint gaps of 1.1–1.3 — it names a tier two or three levels away: `S/C`, `A/D`.

**Why it needs you.** The coverage row is the contract and the code satisfies it only under one
reading of σ. CLAUDE.md's rule is "the row wins or you ask me", and this is a user-facing
contract (what the badge says), so it is an ask rather than a judgement call.

**The options.**

| | behaviour | cost |
|---|---|---|
| A | badge names the **reached** tier (today) | matches §6.3's "A/S" example; produces `S/C` at wide σ |
| B | badge names the **adjacent** tier in the direction reached | matches proposal 76 and the row; contradicts §6.3's example |
| C | badge names the reached tier, but §6.3's example is amended to an adjacent pair | v2.2 text change, code unchanged |

Related and independent of the choice: `model.straddle` prefers the *downward* reach
unconditionally (`if below != tier … elif above != tier`). Where the posterior reaches both
ways it therefore names the lower tier even when the upper one is more probable — at
`s = 0.9, σ = 1.0` on the default cutpoints it reports tier 3 where P(tier 3) = 0.18 and
P(tier 5) = 0.42. That is a defect under any of A/B/C and is listed at §3.4.

### 1.2 Does §6 preamble's "undo everywhere" oblige Rank?

§6 preamble: *"undo everywhere, next card preloaded"*. §6.3 describes no undo and M3 ships
none. A mis-dropped title is corrected by dropping again, which §6.3 calls data rather than
override — so the correction *adds* an observation instead of retracting one. A mis-tapped
comparison-queue answer is permanent.

M2 built decision 35's undo for Rate, block-scoped to a rating session; `observations.undo`
does not generalise to a surface with no block counter.

**Ask:** is "undo everywhere" satisfied on Rank by "drop it again", or does Rank owe a real
retraction? If the latter it is M4-sized work (a Rank-side observation journal) and wants a
coverage row of its own. No row names it today, on any surface but Rate.

### 1.3 Does `tier_edit.via = 'explicit'` get a producer?

§5.2 arm 3 names *"Tier edits (drag-drop, **explicit picks**)"* and §4.2 ships the enum value.
No v2.1 section describes a control that writes one. Both M3 input paths write `drag_drop`.

You answered this once at plan time — *leave it unproduced, report it as a spec defect* — and
that is what shipped. Repeated here because the value has now been dead for two milestones, so
the choice is between giving it a producer (proposal 77 puts a tier picker on the title card) and
removing it from the enum in v2.2. Neither is M3's to make.

### 1.4 Scale of the exit-criterion evidence

§12's M3 exit criterion was measured (`ops/m3_exit_criterion.py`, results in
[`M3-plan.md`](M3-plan.md)) against the **8-title synthetic fixture**, which is six films. The
board is six rows. §6.3's own budgets — "~10–20 comparisons place a new title", "the first
*stable* tier list arrives at ~1,500–3,000 comparisons" — are untouched by anything at that
scale, and §13's agreement figure read 1.00 on n = 1 and n = 3.

**Ask:** is that acceptable as M3's evidence, or should the criterion be re-measured against a
real corpus bundle before M4 builds Tonight on top of these rankings? The machinery is tested;
the *numbers* are not evidence about a household.

---

## 2. Spec defects for v2.2

Places v2.1 is wrong, silent where it needed to speak, or in conflict with itself. Each was hit
while building M3.

### 2.1 `tier_edit.tier` records an index into a mutable set — and not which set

**Severity: high.** §4.2 defines `tier_edit.tier` as *"index into the user's configured tier
set"*, and decision 11 makes that set a per-user preference the person can change. No column
records the K the row was written under.

- **Shrink 7 → 4:** a level-6 edit is clamped to 3 by `observations.load_observations`. "Top of
  seven" becomes "top of four", which is roughly right by luck.
- **Grow 7 → 12:** nothing rescales. Two hundred drops that meant *S* (6 of 7, the 93rd
  percentile) are refitted as level 6 of 12 — the middle. Every title the person loved slides
  toward average with no observation having changed.

Decision 11's guarantee that *"tier edits are observations and survive the change"* is honoured
in row count and violated in meaning. **v2.2 should either record the tier-set size on the row
(and rescale at read), or state that the set is fixed after first use.**

### 2.2 §6.3 gives the queue's shares and not its construction

§6.3: *"boundary-targeted active selection (70% posterior-straddling pairs / 20% exploration /
10% uniform-random held out…)"*. It never says what a *posterior-straddling pair* is (a
straddling title paired with **what**?) or what an *exploration* pair is. Both were invented,
and the reasoning is in `rank/queue.py`'s docstrings — but they are inventions, and §3.2/§3.3
below argue the invented boundary arm is measurably weak.

### 2.3 §6.3's straddle example contradicts proposal 76

See §1.1. `"A/S"` is two tiers apart; `(S/A+, F/D)` is adjacent.

### 2.4 `via = 'explicit'` has no producer

See §1.3.

### 2.5 §6.3 is silent about the ends of a tier

*"dropping it **between** two titles emits that edit plus two margin-less duels"* says nothing
about the first and last slot of a tier, which have one neighbour. M3 writes one duel there,
argued in `rank/drop.py`; the alternative readings (refuse the drop, or invent a second duel)
are both worse, but the spec should say so rather than leave it to a comment.

### 2.6 Decision 11 adds a §5.3 job that §5.3's table does not have

Decision 11 says a tier-set change *"queues a refit for that user alone"*. §5.3's job table has
no row for it. M3 added `tier-set-refit` to `worker.JOBS` and says so out loud in its docstring
rather than smuggling it in. §5.3's table should gain the row.

### 2.7 §13's re-ask stream has no owner on the Rank surface

§13 mandates *"a separate silent re-ask stream — ~10% of **comparisons**/verdicts re-asked after
≥3 days"* from day one. `rate/reask.py` (M2) samples §6.1's battles and verdicts. The tier queue
— which §6.3 expects to become the household's main source of comparisons at "~30–50/week" —
has no re-ask path, and §13 does not say which surface owns the stream. So the flip rate σ that
*"sets the tier budget"* will be measured on §6.1's random band-drawn pairs and applied to
§6.3's boundary-targeted ones.

### 2.8 The measured tier shape is placed on an unstated scale

§6.3: *"initialised from DNA_MODEL §4.5's measured quantile shape F 3 / D 7 / C 15 / B 25 /
A 25 / A+ 17 / S 8 %"*. `model.initial_cutpoints` realises it as logit(cumulative share), i.e. on
a **standard logistic** scale (sd ≈ 1.814). The latent's own residual prior is `b_i_tau = 1.0`.
Measured:

```
cuts                [-3.476 -2.197 -1.099  0.000  1.099  2.442]
implied at sd 1.814  [.028 .085 .160 .228 .228 .183 .089]   ~ the authored shape
implied at sd 1.0    [.000 .014 .122 .364 .364 .129 .007]   F is 100x too small, S 11x
```

So on a board whose `s` spreads like the prior, F and S are near-empty and B/A bulge. §6.3 says
"then learned", and the cutpoint prior has precision 1.0 per cutpoint — which means on a sparse
board the *prior* dominates and this is not merely a starting point. **v2.2 should say on which
scale the shape is to be realised.** Not changed in M3: re-tuning a corpus-derived constant
without a measurement is the corpus project's call (§4.3), not the app's.

---

## 3. Known defects — the tier model and the selector

Found by the review's domain lens, survived a skeptic, **not fixed in M3**. These are the ones
most likely to matter once a real bundle and a real household are behind the board.

### 3.1 The boundary arm has no memory and re-serves the same few pairs

**Severity: high.** `rank/queue.py::_boundary` shuffles the straddlers but picks the partner
deterministically (`_nearest` inside the reached tier). The title just above a cutpoint and the
one just below are each other's nearest neighbour and both straddle — so the arm generates
roughly two pairs per boundary and re-emits them indefinitely. Measured on a 10-title board with
7 straddlers: **2000 boundary draws produced about 5 distinct unordered pairs.**

Nothing in this arm consults `Candidate.comparisons`. Re-asking an answered pair adds nothing
about `s_a − s_b` beyond judgement noise, but `_duel_terms` counts each row as an independent
Davidson observation, so ten repeats shrink that pair's posterior by √10 on the strength of one
judgement. The straddle then resolves through fabricated precision — the same reliability
inflation §13 guards against, arriving by a different door.

**Fix shape:** the arm should read `comparisons` (it already exists on `Candidate`) and
deprioritise pairs it has already asked, or draw the partner from the k nearest across the
boundary rather than the single nearest.

### 3.2 A boundary nobody straddles is never targeted

**Severity: medium.** `_boundary` enumerates *straddlers*, not *boundaries*. A cutpoint sitting
in a gap between two clusters of `s` has no straddler by construction — and that is precisely
the cutpoint the likelihood does not determine, because the ordinal NLL is flat across an empty
interval and the cut is pinned by its prior. The arm therefore starves the boundaries whose
position is least identified and spends its budget on the ones already surrounded by data.

Reproduced: `s = [-2.0, -1.9, 1.5, 1.6, 1.7]`, σ 0.05 → four of six boundaries separate the two
clusters, `_boundary` returns `None`, and the whole 70% falls through to exploration.

### 3.3 The exploration arm pairs two equally-uncertain titles

**Severity: medium.** `_exploration` takes the least-compared title and pairs it with its
*nearest neighbour in `s`*. A duel observes `s_a − s_b` only, so two titles with equal σ give a
posterior that is tight in the contrast and barely moved in either marginal. Arithmetic at the
fitted Davidson (δ₀ = 0.22): an anchor at σ = 2.0 paired with a near-duplicate reaches posterior
σ 1.668; paired with a well-anchored title at σ = 0.05 it reaches **1.513** — better, despite
carrying slightly less per-pair information. The nearest-neighbour rule maximises information
about a difference nobody asked about, and at Δs ≈ 0 the answer is a coin flip with a 22% tie
rate.

### 3.4 `model.straddle` prefers the downward reach unconditionally

**Severity: medium.** See §1.1's tail. Independent of which tier the badge should name, the
tie-break between "reaches down" and "reaches up" is arbitrary and undocumented, and picks the
less probable tier roughly half the time.

### 3.5 The equal-mass re-initialisation is discarded by the refit it queues

**Severity: medium.** `tiers.save_tier_set` writes `equal_mass_quantiles(s, K)` to
`ledger_cutpoints.boundaries` and sets `refit_requested_at`. But `model.fit` always starts from
`initial_cutpoints(K)` and centres the cutpoint prior there — it never reads the stored
boundaries. So decision 11's re-initialisation survives only in the window between the settings
save and the refit that save queued, which is the opposite of what decision 11 describes.

`TierSetReport.initialised == "quantile"` is true for about a minute and false thereafter.

### 3.6 Tension can be erased by time but never created by it

**Severity: medium.** `board.tension_of` reads the *freshness-inflated* σ (`sigma_eff`), and
§5.2's inflation is monotone in months untouched. The tension test is an overlap test, so a
wider interval can only switch tension **off**. A person who mis-placed a title three years ago
and never touched it since watches the badge fade with the calendar rather than with evidence.

It also makes the badge's stated meaning false: `hyperparams.tension_z()` computes the exact
80%-central multiplier for a Laplace posterior sd and then applies it to a quantity that is not
one. The straddle badge has a defensible reason to use `sigma_eff` (§5.2 names σ as driving
badges *and* the queue, and staleness genuinely widens what you should re-ask); "the model
disagrees strongly with you" does not — that is a claim about the fit.

### 3.7 On a young board everything straddles

**Severity: medium.** `straddle_z = 1.0` against default cutpoint gaps of 1.10–1.34, with a
residual prior giving σ ≈ (1 + 0.2k)^−0.5 for k observations on a title: σ 0.913 at one
observation, 0.791 at three, 0.674 at six. A ±1σ interval spans 2σ, so a title stops straddling
only past roughly **12 observations on that title**. Until then every poster wears a badge and
`queue_eligible == rated_total`.

The consequence in the selector is concrete: `_exploration`'s `away = [c for c in pool if
c.straddle is None] or list(pool)` falls through to the full pool, so the 20% arm draws
straddlers and still reports `arm="exploration"` into `duel.selection` and the §6.7 log. The arm
labels stop describing what was drawn.

### 3.8 The held-out agreement figure is a bare conditional accuracy

**Severity: medium.** `evaluation.held_out_agreement` excludes ties from the rate. Under the
fitted Davidson, P(tie) is maximal at Δ = 0 and falls with |Δ| — so dropping ties conditions the
estimator on the *large-|Δ| pairs*, exactly where `sign(s_a − s_b)` is most reliable. The rate
therefore exceeds the model's accuracy on a uniform pair, and moves with the person's tie rate
rather than with the model: a compressed board yields more ties, a smaller and more extreme
denominator, and a **higher** number. It is not comparable across users or across time.

Two smaller problems in the same figure: `_holdout` has no memory, so the same unordered pair
recurs (100 titles, 50 held-out draws → ~22% chance of at least one repeat) and a repeat is
counted as an independent observation; and the figure ships as a point estimate with no interval,
when at §6.3's own "~500-comparison weekend" the decisive sample is about 39 rows (SE ≈ 0.073 at
a rate of 0.7). §0 fixes a noise floor and calls anything smaller a tie.

### 3.9 `equal_mass_quantiles` can put the whole board in the top tier

**Severity: low.** `s = [0.4] * 10, k = 7` returns six cutpoints all equal to 0.4, and
`tier_of(0.4)` is then 6 for every title — the whole board in S, six empty tiers, and the report
saying `initialised: quantile`. The docstring defends coincident cuts by pointing at
`model.feasible`, but that is a statement about the *optimiser's* feasible set, not about a cut
set fit to display.

### 3.10 `model.straddle`'s two ends use different strictness

**Severity: low.** Both ends use `searchsorted(..., side="right")`. A lower end landing exactly
on a cutpoint does not count as reaching down (consistent with the half-open band); an upper end
landing exactly on one does count as reaching up. The predicate is not symmetric under negating
`s`. Measure-zero in floating point; it matters only to tests that construct exact boundary
cases, which will pass in one direction and fail in the other.

---

## 4. Known defects — data layer and surface

### 4.1 The DNA predicate does not pin the vocabulary version

**Severity: medium.** `db/library.py::_filters`' `dna` clause and `dna_tiers_for` read
`dna_tagged` without a `version` scope. Every other DNA read in the codebase carries one —
`home/why.py` three times, `placement/features.py` threads `vocab_version` through every block.
`dna_tag` is `UNIQUE (title_id, version, term, provider)` and `importer/dna.py` only upserts, so
v1 and v2 rows coexist after a re-import. `GET /api/rank?dna=mood.cosy` after a v2 import that
renamed or dropped `cosy` still matches stale v1 rows, and reports the title as `extracted` on
the strength of a tag the active vocabulary no longer contains. The test fixture inserts only
`'v1'`, so no test can see it.

### 4.2 `cutpoints_length` is satisfied by an empty array

**Severity: low.** 0005's `CHECK (array_length(boundaries, 1) = array_length(tier_set, 1) - 1)`
— which 0012's header cites as the reason decision 11 needs no new table — passes when either
array is empty, because `array_length` of an empty array is `NULL` and `3 = NULL - 1` is `NULL`.
Verified against the live database. Not reachable through the app (`tiers.validate` refuses
K < 2), but the constraint does not hold the line the migration and its test claim it does.
`CHECK (coalesce(array_length(boundaries,1),0) = coalesce(array_length(tier_set,1),0) - 1)` in a
**new** migration closes it — 0005 must not be edited.

### 4.3 The tier-set refit sweep can swallow a concurrent request

**Severity: low.** `worker._tier_set_refits` reads `refits_owed`, runs `refit_user` (§5.3:
"seconds"), then calls `clear_refit_request` with no time predicate. A second
`PUT /api/rank/tiers` landing in that window sets `refit_requested_at = now()` and the clear
wipes it — so that change keeps its equal-mass boundaries until the nightly. The worker's own
docstring says "a queue that forgets what it dropped is worse than one that retries". Fix: carry
the observed timestamp through and clear with `AND refit_requested_at <= $3`.

### 4.4 `comparison_counts` includes re-asks the fit excludes

**Severity: low.** `rank/read.py::comparison_counts` filters `selection <> 'uniform_holdout'`
but not `NOT is_reask`. `load_observations` and `held_out_agreement` both exclude re-asks, on the
argument that a re-ask is the same judgement posed twice. Once §13's re-ask scheduler covers the
queue (§2.7), a re-asked title will look more compared than the model has evidence for and
`_exploration` will deprioritise it.

### 4.5 The Rank search box misstates what it filters

**Severity: low.** `frontend/src/routes/rank/+page.svelte` — placeholder
`"filter — title or DNA term, e.g. cosy"`. The `q` predicate matches `title.name` and
`title_alias.alias` only; DNA is the *separate* box next to it. Type `cosy` into the first box
as instructed and you get an empty board plus "Nothing matches search cosy". §6.8's register
makes a control that misstates itself the opposite of a quiet reason.

### 4.6 `rank.tierSet` is dead state

**Severity: low.** Assigned in `apply()` and read nowhere — the board renders tier labels from
`tiers[].label`. Either render it or remove it.

### 4.7 Deferred surface work (proposals not cited by any coverage row)

Each of these survived a skeptic as *real but not normative* — the only source asking for them
is a v2.2 proposal that no M3 row cites, so they are v2.2 scope rather than M3 omissions.

- **The decisive toggle in the comparison queue** (proposals 73, 79). The wire field exists
  (`AnswerBody.decisive`) and `api/rank.py` comments that §5.2's margin weighting applies, but
  the client always sends `false`. Every tier-queue duel is written at the hesitant margin.
- **Proposal 79's resolution readout** — `N rated · M comparisons this session · +0.012
  within-liked`. `rank.queueEligible` is fetched and now used in the sharpen why-line, but the
  gauge itself is absent.
- **The badge as the queue's entry point** (proposals 71, 157). The chip is a `<span>` inside the
  poster `<button>`, so it cannot be tapped separately, and `openQueue()` takes no seed. Both
  cited proposals lean on this; neither cited *row* tests it.
- **Dialog semantics for the queue panel** (proposal 131) — no `role="dialog"`, no Escape, no
  outside-click, board stays tabbable underneath.
- **`--ink-4` at 10 px is ≈2.9:1 contrast** on §6.3's neighbourhood badge. That is `design.css`'s
  global quiet-reasons primitive used as intended, so it is a §6.8 question affecting every
  surface rather than an M3 defect — but §6.3's badge is *required content*, which is an argument
  for `--ink-3` there.

---

## 5. Test-quality debt

Tests that pass, are registered against a row, and are weaker than they read. Found by the
review's test-quality lens; the ones it found that were **fixed** in M3 are not listed.

| where | what it asserts vs what it claims |
|---|---|
| `test_rank_integration.py::test_a_drop_is_one_transaction` | Both refusals are raised *above* the `async with conn.transaction()`, so nothing is ever written and rolled back. It tests the guard clauses. Verified: replacing the transaction with `if True:` leaves it green. |
| `…::test_tap_to_tier_writes_exactly_what_the_pointer_path_writes` | Calls `drop.drop` twice and asserts the two `via` values agree — which one function necessarily satisfies. The real claim (both *input paths* reach this function) is a docstring argument, and now rests on the e2e. |
| `test_worker_jobs.py::test_the_tier_set_refit_job_is_a_no_op_when_nobody_asked` | Asserts `count(*) FROM ledger_state` is unchanged. `refit_user` upserts existing rows, so a job that refit every user every minute would pass. Should assert `ledger_fit.fitted_at` is unmoved. |
| `test_rank_integration.py` filters | `test_each_rank_filter_narrows_the_board_on_its_own` covers runtime and seen only. Genre has no query-layer test at all; decade appears only in the intersection test, which holds trivially if the decade predicate matches everything. Verified: replacing the decade clause with `pass` leaves both green. The UI half is covered by the e2e control test. |
| straddle_z "from the bundle" | The row ends *"the threshold constant is read from `ledger_hyperparams.json` rather than hard-coded"*. The tests pass `hp` in via `dataclasses.replace`, which proves the pure functions honour a passed value — not that the bundle's value reaches them. The fixture now ships `straddle_z` and `tension_credible_mass`, but at the app defaults, so bundle and default stay indistinguishable. Making `api/rank.py::_hyperparams` return `DEFAULTS` unconditionally would fail nothing. |
| `break_straddle_z` / `break_tension_credible_mass` | Added to `make_bundle.py` for §4.3's two new constants; **no caller**. The validation branches they target have no row in `test_a_nonsensical_constant_is_refused_at_the_boundary`. |
| `…::test_the_queue_pool_is_the_whole_board_and_not_the_filtered_view` | Never passes `rows=` to `read.candidates` — the parameter that exists precisely so a caller *can* hand it a filtered list. Passing the filtered rows in `api/rank.py::next_pair` would leave it green. |
| §4.1 rule 2 static guard | `test_landmine_guards.py`'s `FILTER_PATTERN` requires the comparison operator to be followed by `[\d.$]`, but `db/library.py::_filters` emits every predicate as `f"… {arg(value)}"`, i.e. followed by `{`. M3's code is clean, but the guard would not catch a weight threshold *added in the function the DNA filter lives in*. Widening the trailing class and adding `=`/`IN`/`BETWEEN`, plus a self-test in the `{arg(...)}` form, restores its reach. |

---

## 6. Standing waivers

Unchanged by M3. Both re-read at milestone open and re-checked by grep rather than assumed;
**M3 added no waiver and removed none.**

- **`platform-backup-rotation-and-ciphertext`** (M0, §2 Backups) — no nightly `pg_dump` job
  exists. `grep -rn "backup\|pg_dump"` over `ops/`, `docker-compose.yml` and `worker.py` still
  finds only the two volume mounts. Also needs a restore-into-a-second-database harness the
  `db` fixture does not provide.
- **`data-rules-platform-rating-display-only`** (M0, §4.1 rule 3) — the privilege half. No
  separate feature-builder DB role exists; `grep -rni "create role\|grant \|revoke "` over the
  migrations is empty, so nothing can raise `insufficient_privilege`. The schema-separation half
  *is* asserted.
- **`library-rate-model-line-no-bundle`** (M0, §6.0 + §3.1) — only the bundle-loaded branch is
  asserted. With no bundle there are no titles, so there is no card to open. Becomes reachable
  at **M5**, when a locally acquired title can outlive a deactivated bundle. **Re-read this one
  when M5 opens.**

---

## 7. Carried into M4

- §12's M4 row builds Tonight on the rankings this milestone produces. §3.1–3.3 are the reason
  to look at the selector before that: a queue that re-serves five pairs and never targets an
  unstraddled boundary produces a *stable-looking* tier list that has not converged.
- **`session_answer` needs the same `selection` discriminator `duel` has** (decision 54b) —
  M4's rows already name it. The four seams M3 had to plug for `duel` (the fit, the incremental
  fit, the selector's counts, the evaluation) are the checklist for doing it once rather than
  four times.
- **The lesson from M3's review, for the map:** a row whose `what` names a *surface* has to be
  tested *through* that surface. M3's two worst findings — a permanent 500 on the board, and an
  HTTP seam with no test at all — were both rows whose named tests exercised a domain function
  directly and never reached the layer the row is about.
