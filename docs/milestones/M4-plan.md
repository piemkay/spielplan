# M4 — Tonight: lobby, the round, combine, blind reveal, solo

§12's row: **"Tonight: lobby + open-rooms discovery, push join, the ~10-vote round, guest
hand-off, group combine + conflict surfacing, blind reveal, **solo mode** (+ TV route)"**, exit
criterion **"a real Friday night resolved by the app"**. Branch `m4`, off `main` at `19194dd`.

Sections read in full before writing this: §6.2 (all eight steps), §4.1 (all eight rules), §4.2,
§4.3, §5.1, §5.2, §5.3, §6 preamble, §6.0, §6.5, §6.7, §6.8, §7.1, §7.3, §11, §12, §13, §14,
plus §0 rows 3 and 4 (the aggregation null and the mood/centring measurements) and §2's
Configuration paragraph. `docs/milestones/M3-open-points.md` read in full for what M4 inherits.

The v2.2 proposals were read for context. **They are also where this milestone's central
problem lives** — see "The collision" below, which is question 1 and blocks the build.

---

## Pre-flight

Working tree clean, on `main`, Postgres up, `current_milestone = "M3"`:

```
790 passed in 946.74s (0:15:46)
  > M0   33/35  covered (2 waived)   > M2   25/25  covered
  > M1   10/10  covered              > M3   15/15  covered
```

No failures. M3's pre-flight failure (`Settings(...)` reading the developer's `.env`) was fixed
inside M3, so a green local run now means what a green CI run means. Branch `m4` created.

---

## The collision — question 1, and nothing gets built until it is answered

**The 18 M4 rows already in the map are written against v2.2's rewritten §6.2, not against
v2.1's.** Not incidentally, and not in the `why` field where it would be decoration — in the
`spec` and `what` fields, which the hard rules say I may not edit to match what I build.

| row | names a thing v2.1 does not have |
|---|---|
| `data-rules-session-answer-neither` · `tonight-rank-four-answers` | `NEITHER`. v2.1 §4.2's enum is `A \| B \| EITHER`. |
| `data-rules-session-holdout-excluded` · `tonight-rank-holdout-one-in-ten` | `session_answer.selection`. v2.1 has no such column and no hold-out arm in Tonight. |
| `data-rules-session-ended-by` · `tonight-rank-stopping-and-cap` · `tonight-rank-escape-from-pair-six` | `ended_by`, `converged_at`, a hard cap of 20, an escape from pair 6. v2.1's round is **fixed at ~10 votes** with no cap, no escape and no per-participant stopping rule. |
| `tonight-rank-pair-selection-straddle` | a per-candidate posterior and information-gain selection. v2.1 draws "competitive (adjacent in group score), with partial overlap across participants so tallies are comparable". |
| `data-rules-session-ballot-blind` · `tonight-rank-ballot-blind-until-all` · `tonight-rank-winner-approval-share` | `session_ballot`. **v2.1 has no ballot at all** — and §6.2 step 6 demands "approval share" while §13 makes it the headline metric for the whole feature. |
| `tonight-rank-three-finalists-wildcard` · `tonight-rank-split-reserves-third-slot` | a three-finalist shortlist and a reserved third slot. v2.1 deleted the shortlist stage; proposal 54 asked *which slot carries the alternative* and the owner answered by restoring one. |
| `tonight-rank-solo-lands-on-picks` | solo landing directly on picks. v2.1 step 7 says "optionally a few self-administered votes". |

**Fourteen of the eighteen become untestable if I build v2.1.** Closing the milestone would
then require editing those rows' `spec` and `what` — the one thing the hard rules single out.

### What I recommend, and why

**Build to the rows.** Four reasons, in order of weight:

1. **The map is the contract.** `CLAUDE.md` and `docs/TESTING.md` both say so, and your own
   hard rule says "the row wins or you ask me". I am asking; my recommendation is that the row
   wins.
2. **This is not a proposal.** §6.2-rewritten is filed under *"Decisions taken (owner,
   2026-08-29)"* — the same table, the same date, as decisions 11, 18, 35, 117 and 154. **Four
   of those are already shipped in this codebase**: decision 11 is `ledger_cutpoints` per user
   (M3), decision 18 is the two kind toggles (M2), decision 35 is block-scoped undo (M2),
   decision 117 is the model-log toggle (M2). Treating 54 differently from its four siblings
   would be the inconsistency, not the fidelity.
3. **`README.md` already states it** — "**§6.2 — Tonight, rewritten**, which replaces the fixed
   ten-vote round with an adaptive one".
4. **v2.1 is internally incomplete exactly here.** Step 6 requires the winner card to carry an
   approval share; nothing in v2.1 produces one, because tallies say which of two a person
   preferred, never whether they would be *happy* with a title. §13 evaluates M4 on that number
   and §14 risk 6 forbids tuning the round before it is instrumented. Building v2.1 literally
   ships a milestone whose own exit metric has no data path.

**If you say "v2.1 anyway":** that is a legitimate answer — but it requires you to authorise
rewriting the `spec` and `what` of fourteen rows, because I will not do that on my own
judgement. Say so and I will bring you the rewritten rows for approval before any code.

Everything below assumes the recommendation. The 23 rows I *added* are almost all sourced from
v2.1 sentences and survive either answer.

---

## Opening the milestone

`current_milestone = "M4"` → the gate lists the 18 rows the map already owed. Reading §12's M4
row, §6.2, §4.2, §4.1 and §13 clause by clause against the map found **23 more**, added before
any code. §12's M4 row names eight things; the existing 18 cover four of them.

**M4 is 41 rows.** That is large — M2 was 25, M3 15 — and it is large because §12's M4 row is
the longest contents list in the build order, and because Tonight is the first surface that is
multi-user, real-time, push-carrying, guest-bearing and TV-projected all at once.

### The 23 added, and the clause each one exists for

| added row | kind | the clause the map had no row for |
|---|---|---|
| `tonight-rank-pool-filters-owned-kind-budget-rewatch` | integration | §6.2 step 3's pool: "owned titles passing the kind/budget/rewatch filters" + step 1's soft budget ("up to budget + 40 min", "runs N min over") and the rewatch default ("exclude titles *every* participant has seen") |
| `tonight-rank-pool-order-plain-average` | backend | §6.2 step 3: "ranked by the **plain average** of member Ledger scores" (§0 row 3's measured null) |
| `tonight-rank-pool-never-shown-as-a-step` | integration | §6.2 step 3: "(internal — **never shown as a step**)" |
| `tonight-rank-join-channels-equivalent` | integration | §6.2 step 2: "**Join channels, all equivalent**" — plus the room code, which §11 also hands to Home Assistant |
| `tonight-rank-open-rooms-discovery` | e2e | §12 M4 "open-rooms discovery"; §6.2 step 2's list "visible to every household device … **with tappable empty seats**" |
| `tonight-rank-lobby-live-over-the-session-channel` | e2e | §6.2 step 2: "a live in-app lobby banner **over the WebSocket**"; §1's "REST + WebSocket" |
| `tonight-rank-guest-hand-off-sequential-and-blind` | e2e | §12 M4 "guest hand-off"; §6.2 step 2: "Guests use the initiator's phone after the initiator finishes (hand-the-phone, sequential turns)" |
| `platform-vapid-keypair-first-boot` | integration | §2: "A web-push VAPID keypair is **generated at first boot and stored the same way**" — no row anywhere named it |
| `tonight-rank-push-join-is-best-effort` | integration | §12 M4 "**push join**"; §7.3: "The banner path is the whole M1 behaviour; **push arrives with the M4 stack**" |
| `data-rules-push-subscription-pruned-on-gone` | integration | §4.2: "pruned on **404/410** from the push service" — only reachable once a sender exists |
| `tonight-rank-tilt-centred-on-the-pool-mean` | backend | §6.2 step 5: "chosen-minus-rejected DNA, **centred on the candidate-pool mean** (the measured centring lever)" — §0 row 4 |
| `tonight-rank-no-within-evening-reranking` | backend | §6.2 step 6: "Votes *choose*; nothing re-ranks within the evening by predicted enjoyment (measured: worth 0.000)" |
| `tonight-rank-guest-pairs-not-seen-filtered` | backend | §6.2 step 4: "Guests' pairs are not seen-filtered (their seen-state is unknown …)" |
| `data-rules-session-answer-logs-every-vote` | integration | §14 risk 6: "**log every vote**"; §4.2's `session_answer` columns |
| `data-rules-session-result-slate-persisted` | integration | §4.2's `session_result(session_id, title_id, rank, group_score, per_user_match jsonb, conflict jsonb)` — a whole table no row named |
| `data-rules-session-guest-slot-is-a-null-user` | integration | §4.2: "`user_id NULL`, — NULL = guest slot on the host phone" |
| `data-rules-session-no-v11-posterior-columns` | static | §4.2: "v1.1 §6's mu/sigma/tolerance/phase columns **do NOT survive**; fairness_ledger omitted" |
| `tonight-rank-split-threshold-and-silence-below` | backend | §6.2 step 5: "(~14.5% of nights; **below that, decide silently**)" |
| `tonight-rank-match-line-terms-are-carried` | integration | §6.2 step 6/7 + §6.8: the match line's terms must be terms the title carries — the invariant `home/why.py` was inverted to make structural |
| `map-taste-admin-log-narrates-session-answers` | integration | §6.7's own worked example: `session_answer(p, pair 4) = A — pool-centred tilt` |
| `tonight-rank-solo-picks-carry-why-and-fit` | backend | §6.2 step 7's solo inventory: the one-line whys, the wildcard's stretch label, the budget-fit line |
| `tonight-rank-solo-writes-no-session-row` | integration | §6.2 step 7: "**no session row**"; and step 2's open-rooms list, which a solo evening must stay out of |
| `tonight-rank-tv-kiosk-route` | e2e | §12 M4 "(+ TV route)"; §6.2 step 8's "lobby, progress, result" |

Ten of these are §12's own M4 sentence, clause by clause. That is the same routine that added
M1's tenth row and M3's four — with one difference worth stating: unlike M2's two, none of
these widens the milestone beyond §12's row. They are the parts of that row nobody had written
down.

### Gaps found outside M4's region — reported, not added

The reconciliation ran wider than M4 and surfaced 28 further requirements that survived a
skeptic but belong to **another milestone's region**. Adding them at M4 would make them
instantly owed and turn this milestone into a retro-fit, so they are listed here for you rather
than added. The notable ones:

- **§4.1 content spine, M0 region (4):** `title.id` carried verbatim across a re-import;
  `title_meta` per-source rows kept; `credit` deduped at read and never at import; reviews
  outside the content schema. All four are §4.1 sentences with no row.
- **§5.1/§5.2, M2 region (3):** "**Blend, never route**"; the evidence gate `n/(n+k)` for warm
  vs cold; the 0..1 weight as the **per-kind** empirical CDF.
- **§5.3, M2 region (1):** `platform-job-triggers-match-the-spec-table` — the map covers the
  budget column and not the trigger column.
- **§13, M2 region (4):** rating capture > 70%; not-seen rate > 50% = queue bug; per-user
  held-out Spearman instrumented; and "the two streams are different instruments" (no
  observation may be both a hold-out and a re-ask).
- **§6.0/§7.1, M0/M1 region (2):** the Play-on-Jellyfin deep-link shape
  `{jf_url}/web/#/details?id={jellyfin_id}`, and the title card carrying **both** of §6.0's two
  actions. The prototype shipped Play as an inert `div`; this repo's card may have the same gap.
- **§6 preamble, M0 region (1):** the 48 px touch floor asserted on every shipped surface.
- **M3 debt, not M4 rows (3):** the boundary arm re-serving ~5 distinct pairs
  (M3-open-points §3.1), the re-ask stream not covering queue comparisons (§2.7), and the DNA
  reads that do not pin the vocabulary version (§4.1). The first is the one I would fix soonest
  — see "What M4 inherits" below.

Refuted by a skeptic and therefore **not** listed as gaps: `pick-through > 60%` and "compare
winner satisfaction against the solo baseline" (both are claims about a real household, not
about the app); "satisfaction spread per evening" (v2.2 mechanism); a Tonight no-bundle state
(already covered twice by `platform-bundleless-boot-is-legal` and
`data-rules-empty-artifact-store-legal`); a Tonight bare-model-number rule (subsumed by
`tonight-rank-result-card-inventory`'s "approval share as a data-voice count").

### Waivers re-read

All three standing waivers still hold, checked by grep rather than assumed:

- **`platform-backup-rotation-and-ciphertext`** (M0, §2 Backups) —
  `grep -rn "backup\|pg_dump" ops/ docker-compose.yml backend/spielplan/worker.py .github/`
  returns exactly two lines, both volume mounts (`docker-compose.yml:22`, `:33`). No job.
- **`data-rules-platform-rating-display-only`** (M0, §4.1 rule 3) —
  `grep -rni "create role\|grant \|revoke " backend/migrations/` returns **0 hits**. No
  feature-builder role exists, so nothing can raise `insufficient_privilege`.
- **`library-rate-model-line-no-bundle`** (M0, §6.0 + §3.1) — still M5. M4 acquires no titles,
  so no locally-acquired title can outlive a deactivated bundle yet. **Re-read at M5.**

**M4 proposes no waiver.** The one place a waiver could be argued is the TV route — §6.2 step 8
calls it "Optional … nice-to-have" — but §12's M4 row lists "(+ TV route)" in the milestone's
contents, and §12 is the milestone contract. If you want it out, the honest instrument is
re-milestoning that row to M7, not a waiver; question 4.

---

## The rows, grouped by kind, in build order

Cheapest layer that can falsify the rule. The pure modules first, because every surface above
them reads their output — the same order M3 used.

### Slice 0 — schema and the domain skeleton

- **`backend/migrations/0013_tonight.sql`** — a new file, never an edit to an applied one.
  Six tables: `session`, `session_participant`, `session_answer`, `session_ballot`,
  `session_result`, `session_outcome`, plus the VAPID keypair's storage. `0002_users.sql:48`
  already reserves the bare name: *"§4.2 reserves the bare name `session` for a *Tonight*
  session, so the auth session table is `auth_session`."*
  CHECK constraints carry the enums: `session_answer.answer IN ('A','B','EITHER','NEITHER')`,
  `session_answer.selection IN ('adaptive','uniform_holdout')` — **spelled `uniform_holdout`,
  the same string `duel.selection` and `observations.HELD_OUT` already use**
  (`0005_ledger.sql:50`), because a second spelling is how an exclusion silently stops
  matching — and `session_participant.ended_by IN ('converged','cap','escape')`.
  Asserted at **both** layers, the way `data-rules-seen-state-binary` is: PGlite
  (`test_migrations.py`) for shape — `tests/pglite/apply.mjs` reports columns and indexes since
  M3, so ALTERs are visible — and `test_schema_contracts.py` for behaviour, because *"a CHECK
  constraint that is never tried is a comment with punctuation"*.
- **`backend/spielplan/tonight/`** — the domain package. `api/tonight.py` decides only HTTP
  shapes, exactly as `api/rank.py` and `api/rate.py` do.

Rows closed here: `data-rules-session-guest-slot-is-a-null-user`,
`data-rules-session-no-v11-posterior-columns`, and the schema halves of
`data-rules-session-answer-neither` and `data-rules-session-ended-by`.

### static — 1 row

1. **`data-rules-session-no-v11-posterior-columns`** — §4.2's deletion, guarded the way
   `user_title`'s missing `forgotten` is (`test_landmine_guards.py::test_user_title_state_has_no_forgotten_value`).
   *Touches:* `test_landmine_guards.py`, plus **a self-test feeding it a synthetic violation**,
   which is the house rule for a new guard.

### backend (pytest, no database) — 14 rows

Pure and seeded, for the reason `rank/queue.py` is: a claim about a distribution is measured by
drawing from it twenty thousand times, and that is not a thing to do through Postgres.

2. **`tonight-rank-pool-order-plain-average`** — *new* `tonight/pool.py::group_score`.
   Mirrors `home/shelves.py:740-765`'s arithmetic, whose docstring already says it *"is the same
   rule §6.2 step 3 ranks the Tonight pool by … what makes 'doubles as the Tonight prior' a
   shared arithmetic rather than a claim"*. Generalises its hard-coded two-user self-join to N
   seats.
3. **`tonight-rank-pool-filters-owned-kind-budget-rewatch`** (the pure half — the `+40`
   admission and the `runs N min over` arithmetic) — `tonight/pool.py`.
4. **`tonight-rank-four-answers`** / 5. **`data-rules-session-answer-neither`** — *new*
   `tonight/round.py::update`. The two rows are the same update rule seen from the schema and
   from the model.
6. **`tonight-rank-pair-selection-straddle`** — `tonight/round.py::select`. Reads
   `dna_axis`/`dna_axis_weight` (0004) for the widest-axis tie-break.
7. **`tonight-rank-holdout-one-in-ten`** / 8. **`data-rules-session-holdout-excluded`** —
   `tonight/round.py`. **The guard from `rank/queue.py:14-27` is ported verbatim in spirit:**
   the hold-out arm never receives a fallback, and an arm is reported as the arm that drew it.
9. **`tonight-rank-stopping-and-cap`** / 10. **`tonight-rank-escape-from-pair-six`** /
   11. **`data-rules-session-ended-by`** — `tonight/round.py::state`.
12. **`tonight-rank-guest-pairs-not-seen-filtered`** — `tonight/round.py`, same module.
13. **`tonight-rank-tilt-centred-on-the-pool-mean`** — *new* `tonight/tilt.py`. Reads DNA
   through the sanctioned `dna_tagged` view and nowhere else (§4.1 rule 1), with salience and
   confidence in the ORDER BY and in no predicate (§4.1 rule 2) — `home/why.py`'s discipline.
14. **`tonight-rank-three-finalists-wildcard`** / 15. **`tonight-rank-split-reserves-third-slot`**
   / 16. **`tonight-rank-split-threshold-and-silence-below`** — *new* `tonight/combine.py`.
17. **`tonight-rank-conflict-copy-bounds`** — *new* `tonight/copy.py`. A sanctioned-string
   allowlist, not a filter over candidate phrasings: the row says the sanctioned string is
   *emitted in its place*.
18. **`tonight-rank-guest-not-borrowed-ledger`** — `tonight/pool.py` + `tonight/round.py`.
19. **`tonight-rank-no-within-evening-reranking`** — asserted across `tonight/` as a whole:
   the pool order is computed once at open and carried, and no round path calls
   `ledger.refit` or `scoring.serve`.
20. **`tonight-rank-solo-picks-carry-why-and-fit`** — *new* `tonight/solo.py`.

### integration (pytest against Postgres) — 17 rows

21. **`data-rules-session-guest-slot-is-a-null-user`** — the key decision, made before the
    migration rather than after it.
22. **`data-rules-session-answer-logs-every-vote`** — `tonight/round.py` write path.
23. **`data-rules-session-result-slate-persisted`** — `tonight/combine.py` → `session_result`.
24. **`data-rules-session-ballot-blind`** / 25. **`tonight-rank-ballot-blind-until-all`** /
    26. **`tonight-rank-winner-approval-share`** — *new* `tonight/ballot.py` → `session_ballot`,
    `session_outcome`. The blindness is a property of the **read query**, not of the client.
27. **`tonight-rank-waiting-shows-progress-only`** — *new* `tonight/channel.py`. The row's
    words are "the payload cannot carry the answers, not that the UI declines to draw them",
    so the assertion is over the serialised frame.
28. **`tonight-rank-join-channels-equivalent`** — *new* `tonight/rooms.py`.
29. **`tonight-rank-pool-filters-owned-kind-budget-rewatch`** (the query half) —
    `tonight/pool.py` against real rows, because §7.2 re-derives `is_owned` and a stale flag
    puts an unplayable title behind a Play CTA.
30. **`tonight-rank-pool-never-shown-as-a-step`** — asserted over every pre-reveal payload.
31. **`tonight-rank-match-line-terms-are-carried`** — `tonight/copy.py` + the `dna_tagged` read.
32. **`map-taste-admin-log-narrates-session-answers`** — `home/rail.py` gains a
    `session_answer_line` shape, the way M3 gave it `duel_line`.
33. **`platform-vapid-keypair-first-boot`** — *new* `spielplan/push/keys.py`, wrapping under the
    existing DEK (`core/secrets.py`). `api/push.py:69-78`'s `vapid_public_key()` currently reads
    `os.environ` and says in its own docstring that *"the key pair belongs to that half"* —
    M4 is that half, and this row is the reason it stops being an env var.
34. **`tonight-rank-push-join-is-best-effort`** / 35. **`data-rules-push-subscription-pruned-on-gone`**
    — *new* `spielplan/push/send.py`. **No new dependency:** `cryptography>=43` (already present
    for the DEK and passkeys) supplies P-256 ECDH, HKDF and AES-GCM for RFC 8291, and ES256 for
    the VAPID JWT; `httpx` does the POST. Verified in the venv.
36. **`tonight-rank-solo-writes-no-session-row`** — `tonight/solo.py`.
37. **`data-rules-session-ended-by`** (the persisted half, paired with the pure test above).

### e2e (Playwright, real stack) — 6 rows

New spec files numbered into the existing sequence — specs are filename-ordered, stateful, one
worker: **`e2e/specs/14-tonight.spec.js`** (solo, lobby, round, reveal),
**`e2e/specs/15-tonight-group.spec.js`** (two members in two browser contexts),
**`e2e/specs/16-tonight-tv.spec.js`**.

38. **`tonight-rank-solo-lands-on-picks`** — the phone project first; §6 preamble makes it the
    primary form factor.
39. **`tonight-rank-result-card-inventory`**
40. **`tonight-rank-open-rooms-discovery`**
41. **`tonight-rank-lobby-live-over-the-session-channel`**
42. **`tonight-rank-guest-hand-off-sequential-and-blind`**
43. **`tonight-rank-tv-kiosk-route`**

**The e2e cost worth naming up front:** Tonight is the first surface where one browser context
is not enough. Per-device blindness and "votes hidden until every participant has finished" are
claims about *two clients*, so 15-tonight-group runs two `browser.newContext()` sessions signed
in as two different members. `e2e/specs/13-rank.spec.js:43-59` already shows the member-seeding
idiom (`POST /api/setup/members` → OTP → password change); it will move into `e2e/helpers.js`
so two specs can share it.

`e2e/specs/05-milestones.spec.js`'s `/tonight` placeholder assertions are deleted and replaced
by a "Tonight is built — M4 landed" test, exactly as M1 did for Account, M2 for Rate and M3 for
Rank. Two assertions there fail by design the day this ships: the `PENDING` loop's
`Not built yet — this surface arrives with M4.` and `a placeholder still describes what the
surface will do`, which greps `/roughly ten candidate votes|adaptive|blind/i` on `/tonight`.

### The two contracts every new route drags in

- **`ops/devstub.py`** — `test_devstub_contract.py` fails the build if the harness misses a real
  route *or invents one*. Every new `/api/tonight/*` path lands there too. The WebSocket route
  is not in the OpenAPI path set, so the stub owes it nothing.
- **`frontend/src/lib/tonight.svelte.js`** + colocated `tonight.svelte.test.js` (Svelte 5 runes,
  JS not TS), `frontend/src/routes/tonight/+page.svelte` replacing the `Milestone` placeholder,
  and `frontend/src/routes/tv/+page.svelte`.

---

## Spec ambiguities, and the resolution proposed for each

§6.2-rewritten specifies a mechanism and stops short in ten places. Each resolution is the
smallest choice consistent with the spec's register; the two that would change the data model
or a user-facing contract are pulled out as questions instead.

1. **What *is* the per-candidate posterior?** 54c says "candidates whose posterior interval
   still straddles the shortlist boundary" and never names a distribution. → A Gaussian per
   candidate on the tonight-score scale, mean initialised at that participant's §5.1 score and
   variance from the pool spread, updated by **the Davidson-with-ties likelihood
   `ledger/model.py` already implements**. A/B/EITHER is exactly Davidson's win/loss/tie, so
   this borrows a fitted, tested form rather than inventing one. Pure numpy, per participant,
   tens of candidates — microseconds, well inside §6's "<1.5 s per battle".
2. **What does `NEITHER` do to a posterior?** 54c says only "lowers both". → Model it as both
   candidates *losing* a duel against a virtual anchor pinned at the pool median, and `EITHER`
   as both *beating* it while tying each other. That makes "lifts both" / "lowers both" a
   likelihood term in the same family instead of an ad-hoc additive nudge with an invented
   magnitude, and it is what makes `NEITHER` "the most informative answer available" — it moves
   two candidates at once.
3. **What is "the shortlist boundary"?** → The boundary between rank 3 and rank 4 by current
   posterior mean, because 54d fixes the shortlist at three finalists. A candidate straddles
   when its credible interval crosses that cut, reusing `rank/board.straddles`' predicate and
   `hyperparams.straddle_z`.
4. **"the pair whose answer would most reduce the number still straddling".** → Over pairs of
   straddlers, take the expected count still straddling after each of the four answers, weighted
   by their probability under the current posterior, and pick the argmin. Exhaustive is cheap at
   this scale. Ties break to the pair spanning the widest **DNA axis**, which is 54c's own
   tie-break, computed from the shipped `dna_axis_weight` artifact.
5. **"the leading candidates' intervals separated from the rest"** (stopping). → Converged when
   **no candidate straddles the 3/4 boundary** — the same predicate as selection, so the round
   stops exactly when it has nothing left to ask. This is M3's "one predicate does both jobs"
   applied again (`badge` and `queue.eligible` are one function for the same reason).
6. **Do hold-out answers move the tonight score?** 54b says they are used for "neither selection
   nor stopping"; `tonight-rank-holdout-one-in-ten` says replaying without them must produce
   "the identical shortlist". → **No.** They are stored, excluded from the posterior entirely,
   and read only by the evaluation path. Any other reading makes the shortlist depend on them
   and the row unfalsifiable.
7. **"divergent answers on the leading candidates"** — the other split trigger. → Two
   participants' answers order the same pair of leading candidates in opposite directions.
8. **"the contested axis/facet".** → The `dna_axis` on which the participants' pool-centred
   tilts point in opposite directions with the largest magnitude. Uses the shipped, authored
   axis artifact (§6.4: "Deterministic — no nightly rebuild"), not an invented facet set.
9. **The wildcard.** §6.4 gives the policy: "~1 exploratory slot in 6, ranked by prior +
   proximity … cost ≈ −1 pp top-hit rate, honestly labelled". → Drawn from the pool outside the
   finalists, maximising DNA distance from the finalists' centroid, labelled "a step outside
   your usual".
10. **Room code shape and session lifecycle.** §6.2's example is `MX-2210`; nothing else is
    said. → Two letters, a dash, four digits, from a non-confusable alphabet, unique among
    *active* sessions. States `open → voting → ballot → resolved`, plus `abandoned`; a worker
    job expires sessions idle beyond 6 h. **That job is not in §5.3's table** — named out loud
    in its docstring rather than smuggled in, exactly as M3 did for `tier-set-refit`, and
    reported as a spec defect.
11. **The runtime slider's bounds.** §6.2 gives none, and a slider needs them. → 60–200 min in
    steps of 5, default 130 — proposal 57's numbers, used because they are the only recorded
    ones and inventing a second set helps nobody. Flagged as proposal-sourced.

Two more are questions rather than resolutions, because they change a user-facing contract:
**D's formula** (question 5) and **whether the round owes an undo** (question 7).

---

## v2.2 proposals that collide with this milestone

**Adopted because a coverage row already cites them** — the row is the contract:
**54b** (§13's guard binds Tonight), **54c** (the adaptive round), **54d** (shortlist + reserved
third slot), **54e** (the blind approval ballot), **54f** (solo lands on picks), **54g** (the
schema), **decision 154** (`NEITHER`), **58** (the winner card's layout), **59** (guests are not
ranked by a borrowed Ledger), **60** (the reveal beat), **68** (match lines, incl. a guest's).

**Honoured because the surface is unbuildable otherwise**, not because the proposal says so:
- **56 — the lobby screen.** §12 scopes "lobby" into M4 and v2.1 describes no lobby at all.
- **61 — session outcome capture.** §4.2 has the tables; nothing in v2.1 says when they are
  written. §14 risk 6 makes it mandatory.
- **156 — per-device fan-out and the blind progress view.** Named in 142's own M4 amendment;
  it is the only part of the round with no drawn screen, and `tonight-rank-waiting-shows-progress-only`
  is that requirement already written as a row.
- **70 — the empty and failure inventory.** §3.1's register makes an explicit "nothing here
  yet, and why" state the house style; an empty pool with no copy is a blank screen.
- **57 — the slider constants.** See ambiguity 11.

**Not building** (proposals, not spec; recorded so the omission is a decision):
**63** (recover D's formula — *cannot*: `DNA_MODEL.md` is not vendored in this repo, so the
proposal's own escalation clause fires → question 5), **62** (the split banner's second
sentence — I will use §6.2's own quoted copy), **64** (the QR caption verbatim), **65**
(reshuffle's no-repeat walk), **66** (solo tilt chips), **67** (the group tilt reveal before the
winner), **69**'s "New session inherits controls", **155** (the join window closing at Start —
v2.1 never says joining stops, and asserting either behaviour would invent the requirement).

---

## What M4 inherits from M3

`M3-open-points.md` §7 names three things; two of them bite here.

- **The Rank selector's boundary arm re-serves ~5 distinct pairs** (§3.1, severity high) and
  **never targets an unstraddled boundary** (§3.2). Tonight is ranked from the Ledger those
  comparisons fit. This does not block M4 and I am not fixing it inside M4 — but it is the
  reason M4's *own* selector must not repeat the pattern, so `tonight/round.py` reads its
  answered-pair set and never re-serves one.
- **`session_answer` needs the same `selection` discriminator `duel` has.** M3's four seams for
  `duel` — the fit, the incremental fit, the selector's counts, the evaluation — are the
  checklist, and 54g's column lands in 0013 rather than in a later migration.
- **M3's lesson, which is the one I am most likely to repeat:** *a row whose `what` names a
  surface has to be tested through that surface.* Eleven of M4's 41 rows name a surface. Every
  one of them gets a test at the layer the row is about, not a domain-function test wearing its
  name.

---

## What I am not touching

- `main`. Everything lands on `m4`.
- Any applied migration. `0013_tonight.sql` is a new file.
- The static guards. Tonight's DNA reads go through the sanctioned `dna_tagged` view, and no
  weight column appears in a predicate — `tilt.py` and `copy.py` are written under both rules,
  and `data-rules-session-no-v11-posterior-columns` adds a third guard in the same register.
- Existing tests. Nothing gets loosened. The only existing test that *changes* is
  `05-milestones.spec.js`'s `/tonight` placeholder, which fails by design when the surface
  ships and is replaced rather than deleted.
- The Rank selector defects (M3-open-points §3.1–3.3). They are M3 debt with no M4 row.

---

## Questions — one batch

**1. The collision (blocking; nothing is built until this is answered).** Build to the 18 rows
as written, i.e. v2.2's rewritten §6.2 — adaptive round, cap 20, escape from pair 6, four
answers, three finalists, blind approval ballot, `session_ballot` — or build v2.1's fixed
~10-vote round and authorise me to rewrite fourteen rows' `spec` and `what` first?
**Recommendation: build to the rows.** Reasons above; the short version is that decision 54 is
an owner decision from the same table as four decisions this codebase already ships, and v2.1's
step 6 asks for an approval share that v2.1 supplies no instrument to produce.

**2. Push scope.** §12 says "push join", §7.3 says "push arrives with the M4 stack", §2 says
the VAPID keypair is generated at first boot. Building the **sender** means VAPID ES256 JWTs
and RFC 8291 payload encryption. **Recommendation: build it** — it needs no new dependency
(`cryptography` and `httpx` are already in), it closes three rows, and without it "push join"
and §7.3's M4 clause stay prose. The alternative is re-milestoning those three rows to M5.

**3. The session channel: WebSocket or polling?** §6.2 step 2 says "over the WebSocket" and §1's
diagram says "REST + WebSocket"; two rows say "over the session channel", which polling would
technically satisfy. No realtime machinery exists anywhere in the repo today.
**Recommendation: a real WebSocket** — `uvicorn[standard]` already carries `websockets`, FastAPI
routes them natively, and `tonight-rank-lobby-live-over-the-session-channel` asserts "without
the client polling for it", which is the honest reading of the spec's own word.

**4. The TV route.** §6.2 step 8 calls it "Optional … nice-to-have"; §12's M4 row lists
"(+ TV route)". **Recommendation: build it minimally** — it re-renders lobby/progress/result
over the same channel, so it is small once the channel exists, and it is the only screen where
the blind rule is visible to the whole room. If you would rather defer, say so and I will
re-milestone `tonight-rank-tv-kiosk-route` to M7 rather than waive it.

**5. D's formula.** §6.2 gives the threshold (**D ≥ 0.20**) and the frequency (~14.5% of
nights) and never defines D. Proposal 63 says to recover it from `DNA_MODEL` §5.3 — **but
`DNA_MODEL.md` is not vendored in this repo**, so the proposal's own escalation clause fires.
The risk is concrete: mean-minus-min and |Δ| differ by exactly 2× for a couple, so the wrong
choice fires the split at half or double the intended rate.
**Recommendation: mean-minus-min over the seated members' §5.1 scores, per candidate**, guests
without a grid profile excluded — the prototype's `spread()`, which is the only artifact in
this repo that carries a formula at all. I will record it as a decision and flag it as a spec
defect for v2.2. Tell me if you can supply `DNA_MODEL` §5.3 instead.

**6. Exit-criterion scale.** §12's M4 criterion is "a real Friday night resolved by the app".
The synthetic fixture is **8 titles, 6 of them films** — so a "Friday night" is a pool of six,
a shortlist of three, and a wildcard, with a 20-pair cap that can never bind. M3 asked the same
question (open-points §1.4) and it is still open. **Recommendation: same as M3** — measure the
machinery end to end against the fixture, print the numbers, and state plainly what a six-title
pool cannot show. If you want it measured against a real corpus bundle instead, that bundle has
to arrive first.

**7. M3's carried-over decisions.** Three are open; only one blocks me.
   - **(a) §1.2, "undo everywhere" on the round** — §6's preamble says undo everywhere; §6.2
     describes none, and the round has a hard cap and a blind reveal, so a mis-tap is
     permanent. **This one I need**: it decides whether a 24th row (`tonight-rank-vote-undo`)
     goes in and whether `session_answer` gets a retraction path. **Recommendation: yes, but
     narrow** — retract only the answer just given, only while your own round is in progress,
     removing it from the tally and the tilt without advancing `answered_count`.
   - **(b) §1.1, which tier the straddle badge names** — Rank, not Tonight. Not blocking.
   - **(c) §1.3, `tier_edit.via = 'explicit'`** — still no producer, still not M4's. Not
     blocking; it has now been dead for two milestones and wants either a producer or removal
     in v2.2.

**8. Scope, if 41 rows is too many.** I recommend all 41 and would not cut any of them on my
own judgement, because each traces to a clause of §12's M4 row or of a section it references.
If you want M4 smaller, the cleanest cuts — in the order I would make them — are the TV route
(question 4), the push sender (question 2), and `tonight-rank-pool-never-shown-as-a-step`. Each
is a re-milestoning of a row, not a waiver.
