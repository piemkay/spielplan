# Spielplan — App Specification v2.1

*The app is named **Spielplan** (owner, 2026-08-29). Earlier drafts and the design prototype carry the working title "Media Graph".*

**Status:** implementation spec for the new standalone app (new git repo, from scratch). **v2.1 (2026-08-29)** folds in the UI-prototype review (`spec-v2.1-proposals.md`; design project "Media graph app mockups") and two owner decisions: **no "don't remember" state** (a title you can't recall is plain `unseen` — one control, one sync rule), and the Tonight flow elicits **~10 candidate votes per participant** instead of a visible shortlist + mood-question round. Prototype-adopted additions: Home shelves with mandatory why-lines, axis-scatter exploration (UMAP demoted), solo Tonight, open-rooms discovery, Mix rating mode, decisive toggle, tap-to-tier on phones, Play-on-Jellyfin deep links, PIN-gated user switching, one-time-password user creation, the model-log rail, and a normative design language (§6.7–6.8).
**Supersedes:** `media-graph-spec_v1.1.md` — v1.1's interaction designs survive where they were validated; its modelling core is replaced by the measured architecture (below). Where the two disagree, this spec wins.
**Companions:** every non-obvious modelling decision carries a pointer into the corpus project's evidence: `ARCHITECTURE.md` (the model), `APP_SPEC.md` + `WATCH_NOW.md` + `LABELLING.md` + `DNA_MODEL.md` (measured product decisions), `RATINGS_PREP.md` (data provenance). `media-graph-spec_v1.1.md` is **vendored into the new repo's `docs/`** (superseded, but this spec cites its surviving interaction designs by section — v1.1 §4.3's sweep/battle prose and §5.4.5's social design are normative where cited). Schema and acceptance criteria are always inlined here; only UI prose remains by pointer.

---

## 0. What changed since v1.1, and why

v1.1 was written before the corpus existed. Since then the corpus project (this repo) built and **measured**:

| v1.1 assumption | Measured reality | Consequence for v2.x |
|---|---|---|
| "No cross-user collaborative filtering" (non-goal) | A 95M-rating collaborative backbone is the strongest single signal. Protocol-matched numbers: on the crowd learning curve the item prior alone holds ρ ≈ 0.44 flat while prior + backbone fold-in grows 0.45 → 0.50 as labels go 5 → 100; at 60 labels on the owned library the ladder reads prior 0.4221 → co-rating 0.5190 → co-rating + content 0.5267 | The crowd model is the core; the app imports its frozen artifacts |
| Aspect vocabulary to be built in 4 passes | Built: 578-term / 11-facet DNA vocabulary, extracted on 2,016 titles (824 owned) with quote-verified evidence, projected on 11,324 | The app imports it; only the incremental single-title extractor ships in-app |
| Mood: 8 axes, 8–13 questions, Bayesian posterior | Sessions are real (ICC 0.17) but the stored profile is worth **0.000** for choose-tonight; **3 shortlist-anchored answers ≈ +0.088 AUC**, and centring on the shortlist matters more than question form; within-evening re-ranking is worth 0.000 | v2.1: participants cast **~10 quick candidate votes** instead of a visible shortlist + question round (owner decision). The measured tilt machinery survives underneath — each vote is an item-anchored answer, and the tilt is chosen-minus-rejected DNA centred on the candidate-pool mean; no 8-axis posterior machinery |
| Group: Nash product + fairness ledger | Measured: no aggregation rule dominates plain averaging, and dominance rules cost −0.012. Zero-the-axis-and-surface is an **adopted design** (repair-explanation register), bound by the one measured rule on surfacing: a surfaced split must never ship bare (DNA_MODEL §5.3) | Average scores; zero the contested axis and *tell the group, alternative in hand*; ledger becomes an optional hidden nicety, not core |
| Tiers derived from duels only | The validated Personal Ledger fuses **four** observation arms in one likelihood — 3-class verdicts, margin-weighted duels with ties, explicit tier assignments, rewatch re-ratings — comparisons add within-liked resolution monotonically (+0.008..+0.016) at zero cost to global ranking | Tier list = learned cutpoints on the Ledger latent; drag-and-drop is an observation, not an override |
| Pair-selection heuristics for profile duels | For *profiles*, no selection rule beats random (best +0.0013, CI spans 0); for *ranking*, boundary-targeted selection does help | Random pairs for profile battles; active selection only in the tier-list queue |
| Guests: household-mean taste | Pairs-only guest models stay **below** the bare crowd chart at every tested budget (−0.060 Spearman at 30 pairs, −0.033 at 80; parity ≈ the complete 1,770-pair ordering of a 60-title pool); a 60-title pick-your-favourites grid ≈ 46 pairwise questions for ~10 taps | Guest cold start = the grid, never pairs-only |

Two v1.1 principles are **promoted**, not replaced: *"the graph explains itself"* (now backed by the DNA naming layer with evidence quotes) and *"measured beats asserted"* (now the project's operating law, including a documented noise floor: pipeline variance 0.003–0.008 Spearman; anything smaller is a tie).

---

## 1. System overview

```
┌────────────────────────────── docker-compose ──────────────────────────────┐
│                                                                            │
│  frontend      SvelteKit PWA (static build served by backend or node)      │
│  backend       FastAPI + uvicorn  — REST + WebSocket, auth, scoring        │
│  worker        same codebase, queue consumer — sync, acquisition,          │
│                extraction, nightly refits (CPU-only torch/numpy)           │
│  db            Postgres 16                                                 │
│                                                                            │
│  volumes:  /data/pg  /data/raw  /data/artifacts  /data/cache               │
└────────────────────────────────────────────────────────────────────────────┘
        │ HTTPS via existing Traefik + Cloudflare certs (LAN + Tailscale)
        ▼
  phones (PWA, passkeys)   desktop browser   TV kiosk route   Home Assistant
```

**The model, in one paragraph** (full detail: `ARCHITECTURE.md`). One frozen 64-d collaborative item space (**Backbone**) learned from 95M crowd ratings in the corpus project. A content encoder (**Cold Tower**, torch CPU) places titles *without* ratings into that space from their DNA + metadata — validated: it recovers 67% of the oracle's personal signal on unrated titles. Per person, a **Personal Ledger** holds one latent score per title, fed by four observation arms (verdicts, duels, tier edits, rewatch re-ratings) and refit nightly in seconds by preconditioned optimisation. Ranking = clean item prior + user fold-in over the Backbone, blended β≈0.8. Everything the UI *says* — query predicates, conflict explanations, mood axes, explore frontiers — routes through the DNA vocabulary (the naming layer), never through raw embeddings.

**Hard constraint honoured throughout:** every in-app model update runs on CPU. Measured budgets: Ledger refit for 2 users over 839+ titles — seconds (LBFGS, 64-d); Cold Tower inference — sub-ms/title; nightly jobs — minutes. Backbone (re)training never happens in-app; it stays in the corpus project and arrives as a versioned artifact bundle (§10).

---

## 2. Deployment

- **docker-compose**, four services as above. `docker compose up` + the setup wizard is the whole install.
- **TLS/ingress:** the app itself serves plain HTTP on one internal port; the operator's existing **Traefik + Cloudflare** terminates TLS. `PUBLIC_URL` (e.g. `https://spielplan.example.tld`) is required config because WebAuthn binds credentials to the origin. The same origin must be reachable via **Tailscale** (already true in the operator's setup) so passkeys work identically at home and remote. Document: changing `PUBLIC_URL` later invalidates registered passkeys.
- **No GPU anywhere.** Torch CPU wheels only. The image must build and run on a GPU-less VM (reference: 4 vCPU, 6–8 GB RAM, 25 GB disk + caches).
- **Configuration:** env vars with sane defaults (`DATABASE_URL`, `PUBLIC_URL`, `SESSION_SECRET`, `SECRETS_KEY`, `TZ`). `SECRETS_KEY` (generated by the setup wizard / compose template) wraps a random 256-bit data-encryption key created at first boot and stored in the DB; connector secrets are AEAD-encrypted under that DEK, each ciphertext carrying a `key_id` for rotation/agility. Rotating `SESSION_SECRET` invalidates sessions only and never touches stored secrets; rotating `SECRETS_KEY` is an explicit admin action that re-wraps the one DEK row. The app refuses to start secret-dependent connectors without `SECRETS_KEY` rather than falling back to `SESSION_SECRET`. A web-push VAPID keypair is generated at first boot and stored the same way. Everything connector-related (Jellyfin, LLM, TMDB, OMDb, Trakt) is configured **in the admin UI** and stored in `connector_config` — not env vars, because the owner explicitly wants connector setup in the admin view; env vars may *seed* connector config on first boot for automated installs.
- **Backups:** nightly `pg_dump` to `/data/backups`, rotation 14; the artifact bundle and raw store are already immutable files. Dumps contain ciphertext only — back up the env file (`SECRETS_KEY`) alongside them, or a restored dump cannot decrypt connector config.

---

## 3. Users, auth, identity

### 3.1 Accounts and roles

Local user accounts: `admin` (full access incl. Admin view), `member` (full product, no admin — initially Patrick and Jenny; membership is open-ended, a third member and persistent guests are first-class throughout), `guest` (ephemeral or persistent, limited — see §6.2). User creation (wizard or §6.6): a **one-time password** is issued, the account is locked to a password change at first login, and passkey registration is prompted afterwards.

**First boot is a defined sequence, and a bundle-less app is a legal state:** the app boots with `/data/artifacts` and `artifact_bundle` empty, serving the setup wizard and admin routes; artifact-dependent surfaces render an explicit "no bundle imported" state instead of erroring. The wizard runs: create admin → optional env-seeded connector config (§2) → bundle import as the final step (the same importer the §6.6 Data tab exposes — that one page is M0 scope) → member-account creation (needed before M2, whose exit criterion requires both members' verdicts). Member first-run onboarding then walks each phone through PWA install and push permission (§6 preamble).

### 3.2 Authentication — passkeys first

- **Primary: WebAuthn passkeys** (Face ID / Touch ID / Android biometrics) — viable because the Traefik+Cloudflare origin gives real TLS. Registration from the profile page; multiple passkeys per user (phone + desktop).
- **Fallbacks:** password login (argon2) always available; per-device long-lived session cookies after first auth (the v1.1 "appliance" feel — you authenticate rarely); optional 4-digit PIN for fast user-switching on a shared/TV device.
- Sessions: HttpOnly cookies, 90-day sliding; admin routes re-prompt after 24 h.
- **Shared devices:** the account chip switches between member profiles, gated by the per-user PIN (the chip reads "member · passkey + PIN"). Logout clears the session cookie only — passkeys remain registered.

### 3.3 Jellyfin account linking

Admin view maps each app user ↔ one Jellyfin user (`GET /Users`), optional, one-to-one. The link drives: per-user played-state sync (§7.3), playback-event attribution, and the P(seen) prior in rating mode. Authentication is **never** delegated to Jellyfin (v1.1's reasoning stands: the app must work when Jellyfin is down).

---

## 4. Data model

### 4.1 Content spine (imported from the corpus bundle, then maintained by the app)

Tables mirror the corpus export (§10 manifest): `title` (canonical key: **`title.id` integer** — carried over verbatim; `imdb_id` is NULL on 21% of titles and must never be the join key), `title_meta` (multi-source, per-source rows kept — "one block = one droppable source"), `title_alias/genre/keyword/language/country/company/video`, `person`, `credit` (dedupe at read time, never at import), `award`, `platform_rating`, `ml_genome_score/tag/link`, `review` (separate schema or DB — 312 MB with bodies).

**Schema rules imported from measured landmines** (validation in the importer, constraints in the DB):

1. `dna_tag` (extracted, quote-verified; 2,016 titles) and `dna_projected` (inferred; 11,324 titles) are **separate tables, never merged, never unioned**; every read joins carry a `tier` discriminator; 14,181 (title,term) pairs exist in both and must stay distinguishable. `dna_evidence` ships with the extracted tier — a tag without its quote is unfalsifiable.
2. `salience`, `confidence`, `n_sources` are **weights, never filters**. No `WHERE confidence > x` anywhere (a 0.5 cut would delete 44% of the extracted tier; union recalls 93%, intersection 67%).
3. `platform_rating` lives in a **display-only schema** the feature builder cannot import from. Aggregate platform scores are a popularity conduit and are banned as model features (measured: popularity penalty −0.010 Spearman for nothing).
4. `rating_source.id` values (1,2,3,4,7,11,21,23,26,28,31) are **frozen** — they key `fitted_cuts`, `equating_map`, and the dataset arrays. Never renumber.
5. `kind` (movie/series) is non-null, indexed, and **every ranking surface partitions by it** (measured: the unpartitioned crowd top-10 is 8/10 TV series).
6. Postgres PK fixes on import: coalesce NULLable PK components (`title_alias.region` etc.) to `''`; do **not** add UNIQUE constraints on `tmdb_id`/`trakt_id`/slugs (315/171/… duplicate values exist, mostly legitimate movie/series pairs).
7. Deny-list `%_bak%` / `%_good` tables and every stale JSONL in `data/export/` — export reads live tables only (the JSONLs predate the adjudication repairs).
8. UTF-8 everywhere; never "clean" non-ASCII (the corpus legitimately contains CJK, RTL scripts, ZWSP, emoji); the 73 known-mojibake review rows are fixed individually in the importer.

### 4.2 User state

```sql
user(id, name, role, avatar, jellyfin_user_id NULL, created_at)
webauthn_credential(user_id, credential_id, public_key, sign_count, label, created_at)

user_title(user_id, title_id, state, state_changed_at, jf_synced_at)
    -- state: unseen | seen  (owner decision 2026-08-29: no 'forgotten' state —
    --   "seen, don't remember" is marked plain unseen; verdict/duel history is
    --   append-only and survives the flip)

verdict(id, user_id, title_id, value smallint, created_at, superseded_by NULL)
    -- value: 0 disliked / 1 ok / 2 liked  — the 3-class arm
duel(id, user_id, title_a, title_b, outcome, margin NULL, context, created_at)
    -- outcome: A | B | TIE ("about the same" is first-class data: 22% of random
    --   pairs are genuine ties; margin optional: decisive vs hesitant)
    -- context: profile_battle | tier_queue | tier_insert
tier_edit(id, user_id, title_id, tier smallint, via, created_at)
    -- via: drag_drop | explicit  — an OBSERVATION into the Ledger (§5.2)
ledger_state(user_id, title_id, s float, sigma float, tier smallint, updated_at)
    -- nightly MAP output; displayed 0..1 via posterior CDF (the "relative
    --   0..1 weight" the owner asked for)
ledger_cutpoints(user_id, boundaries float[])   -- learned tier cutpoints; length = |tier set| − 1 (default 6, ordered ascending)
user_vector(user_id, kind, vec bytea, updated_at)  -- 64-d fold-in, mood tilt cache

session(id, host_user_id, state, started_at, ended_at, context jsonb)
session_participant(session_id, user_id NULL,   -- NULL = guest slot on the host phone
    role, tilt jsonb, answered_count, joined_at)
    -- v1.1 §6's mu/sigma/tolerance/phase columns do NOT survive (8-axis
    --   posterior machinery deleted per §0); fairness_ledger omitted in v1
    --   (optional hidden nicety); concession/tolerance machinery deleted
session_answer(session_id, participant, seq, title_a, title_b, answer, latency_ms)
    -- one of the participant's ~10 candidate votes (§6.2 step 4);
    --   answer: A | B | EITHER — "either" is first-class
session_result(session_id, title_id, rank, group_score, per_user_match jsonb, conflict jsonb)
session_outcome(session_id, chosen_title_id, approval_share, participants)   -- feeds §13
push_subscription(user_id, device_label, endpoint UNIQUE, p256dh, auth, created_at, last_seen_ok)
    -- web-push targets; pruned on 404/410 from the push service
playback_event(id, source, title_id, user_id NULL, finished bool, at)
acquisition_job(title_id, stage, status, detail jsonb, updated_at)   -- §8
connector_config(name, config jsonb, secrets_encrypted bytea, updated_at)
artifact_bundle(version, imported_at, manifest jsonb)                -- §10
```

### 4.3 The artifact store (`/data/artifacts/<bundle-version>/`)

Read-only files from the corpus project, loaded at boot **when present** (an empty store is legal — §3.1), hot-swapped on bundle import (§10 swap protocol):

- `backbone.npz` — E, E_full, b_i, μ, plus the per-title support counts `item_n` (the §5.1 gate input) — from `cold_tower_artifacts.npz` + the item stats of the slimmed `content.npz`.
- `cold_tower.pt` — from `data/prep/cold_tower2.pt` (the live model; the earlier `cold_tower` run is superseded — the exporter must ship v2).
- `feature_contract.json` — the **exhaustive** definition of the tower's input: the nine content blocks in order with sizes (dna_x 433, dna_p 556, genome 983, genre 179, keyword 3,884, credit 244, country 97, award 2, meta 57 = 6,435 columns), per-column `feature_names`, then the review-text block = columns 0..63 of the 256-d SVD embedding (singular-value order) multiplied by a frozen scalar `text_scale = 1/(max|emb[:, :64]| + ε)` computed over the corpus at export time and stored in this JSON. The contract *references* the review-text SVD components (which ship as their own file) and records all placement-time preprocessing: genome zero-imputation, text truncation + scaling. §8 stage 9 builds vectors from this file and nothing else.
- `content_X.npz`, `review_text_emb.npz` (+ optional SVD `components` for embedding newly arrived reviews).
- `ledger_hyperparams.json` — the tuned constants of the §5.2 recipe: anchor (ridge) strength λ (currently 3.0), BT weight λ_bt, step count, learning rate, margin-weighting flag + functional form (weights normalised as margin/mean(margin)), tie-prior initialisation δ₀ = 0.22 (thereafter fitted), b_i prior τ (or its CV grid), σ-inflation rate constant and cap. Per-user cutpoints and per-arm sensitivities are **not** shipped — they are fitted in-app by design.
- `dna_vocab/v1/` (vocabulary TSVs, alias map, S matrix, adjudications) + `corrections_v1.tsv` (the credit-corrections ledger — travels with the bundle and is applied at every derive, §8 stage 3).
- `manifest.json` (fitted 3-class cut-points per source), `equating_map.json`, `seed_list.json` (the 100-title decade-stratified onboarding list), `judgement_set_v1.tsv`, `audit.json`.

---

## 5. The model layer (in-app)

### 5.1 Scoring stack (serving)

```
score_u(t) = b(t) + μ_u + w_cf · ⟨v_u, e(t)⟩            e(t) = E[t]      if rated (warm)
                                                        e(t) = gate·E[t] + (1-gate)·ê(t)  else
             b(t) = shrunk item prior; b̂(t) from the Cold Tower for cold titles
             gate = n_t / (n_t + k)                     evidence gating, k≈10
```

Blend with the crowd prior at **β = 0.8** (measured optimum; also exactly where per-user top-10s stop being the global chart: 12 → 263 distinct titles). **Blend, never route** — a learned router was measured to capture 2–3% of the oracle gap and lose to the flat blend. No popularity penalty. Films and series ranked as **separate surfaces**.

### 5.2 The Personal Ledger (the one write-path for taste)

One latent `s_u(t)` per (user, title). Observation arms, all in a single likelihood, refit nightly (full-history MAP; seconds at this scale) and incrementally on each new observation for instant UI feedback:

| Arm | Model | Notes |
|---|---|---|
| 3-class verdicts | ordered logit, free per-user cutpoints | monotone link ⇒ a mis-placed personal threshold widens ties but cannot invert an ordering (measured inversion rate exactly 0.0000) |
| Duels | Davidson Bradley–Terry with ties; **margin-weighted** | tie prior δ from the measured 22% tie rate; margin weighting was selected by tuning, backing the "about the same" button |
| Tier edits (drag-drop, explicit picks) | K-level ordered logit, K = size of the configured tier set (default 7: F/D/C/B/A/A+/S ⇒ 6 learned cutpoints), whose cutpoints **are** the displayed tier boundaries | drag-and-drop = data, not override; the model re-fits around it |
| Rewatch re-ratings | new ordinal observation | drift signal for free |

Fusion math is normative by pointer: the objective is the four-arm likelihood of `ARCHITECTURE.md` §3 (the equations live there) with the Appendix C fusion — ridge anchor on the label arm + BT perturbation, **preconditioned with the ridge Hessian** (fixed-step GD measurably diverges on episodes containing one popular title — this is a scar, keep the preconditioner) — and every constant comes from `ledger_hyperparams.json` (§4.3), re-tunable offline in the corpus project. Generalisation via the 64-d user vector; per-title residuals `b_i^u` capture direct effects. Uncertainty σ per title (Laplace diagonal) drives tier badges ("A/S straddle") and the comparison queue. Freshness: after 12 months untouched, a title's σ inflates Glicko-style at rate c per √month, capped at the prior σ (c and cap from `ledger_hyperparams.json`) — the owner's "keep ratings up to date over time" requirement as ambient recalibration rather than chores.

Display: the 0..1 weight is the **empirical CDF of the user's own fitted `s` values, computed per kind** (their best-ranked title → ~1.0, worst → ~0.0) — the owner's "always-preferred → 1.0, always-rejected → 0.0" definition exactly, and stable under monotone rescaling of `s`.

**Measured expectations to encode in UX copy:** comparisons add resolution *within* the liked class (+0.008..+0.016 at 30 duels, monotone, no cost to global ranking); a decisive pick teaches more than a hesitant one; spreading verdicts across all three classes matters ~5× more than anything the corpus side can tune (a 60%-"liked" labeller gives up ~0.07 ρ) → the rating UI shows a running class balance.

### 5.3 Jobs (worker, all CPU)

| Job | Trigger | Budget |
|---|---|---|
| Ledger incremental update | every observation | <50 ms |
| Ledger full MAP refit + cutpoints + σ | nightly | seconds |
| Fold-in user vectors, blend weights per label count | nightly | seconds |
| Cold Tower placement of new/changed titles | acquisition pipeline (§8) | <1 s/title |
| Placement reconciliation: any owned title lacking a coordinate gets a feature vector built from DB data per the feature contract (absent blocks dropped — the tower's dropout training anticipates this; genome zero-imputed) and runs §8 stages 9–10 only. 19 such titles arrive with the initial bundle; thin ones (2 lack keywords, 3 lack any DNA row) are still placed, badged, and parked as acquisition jobs for M5 enrichment | bundle import + nightly sweep | seconds |
| DNA projection for a new title (per-title incremental — new code; the corpus `dna project` is deliberately wholesale) | acquisition | <1 s |
| Seen-state sync with Jellyfin | 15 min + webhook | — |
| Explore-frontier + taste-viz caches | nightly | minutes |
| Bundle import validation + hot swap | admin action | minutes |

---

## 6. Surfaces

All surfaces: responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as progressive enhancement, installable, service-worker shell cache. v1.1's throughput budgets stand: **<2 s per sweep card, <1.5 s per battle**, undo everywhere, next card preloaded.

Surface names (prototype, normative): **Home / Rate / Tonight / Rank / Map / Taste** (+ Admin).

**iOS web-push constraint (load-bearing):** on iPhone, Web Push works only for a PWA added to the home screen (iOS 16.4+), and the permission request must run inside a user gesture; iOS has no programmatic install prompt, so member first-run onboarding *guides* Share → Add to Home Screen, detects standalone mode, and nags until push is granted. Push is therefore always **best-effort**: every push-carried prompt also exists as an in-app banner, and sessions additionally as a room code/QR.

### 6.0 Home & Library

**M0 — the catalog:** a paginated list over `title`, partitioned by kind (§4.1 rule 5), filter/search on title/alias/genre/decade/seen-state, and the **title detail card**: metadata; credits, each person tappable → filters the library to their filmography; trailer key; platform scores (display-only schema, labelled as such); the DNA card — tags with evidence quotes, extracted/projected tier visibly distinct (§4.1 rule 1); the model line in the data voice (`b(t) 0.52 · β 0.8 · gate 0.93`); and two actions — **Play on Jellyfin** (§7.1) and **Show on map** (§6.4). §6.4's wander mode reuses this same card.

**M2 — Home (prototype-adopted):** the default surface becomes personalized shelves over the catalog. A greeting; a **pending-verdicts banner** ("You've watched *X* and *Y* recently — rate them?" → the §6.1 queue; §7.3's capture prompt given a permanent surface); then shelves, each with a **mandatory one-line why in vocabulary terms** — a shelf that cannot say why it exists doesn't ship:

| Shelf | why-line |
|---|---|
| Because you put *{anchor}* in {tier} | "shares {term} + {term} with it" |
| Top of your ledger | "clean item prior + your fold-in, blended at β 0.8" |
| You've never watched anything *{term}* | "unvisited region of DNA space next to what you like" (§6.4 frontier as a shelf) |
| You and {other} both rate these highly | "the shared sweet spot — doubles as the Tonight prior" |
| Under 110 minutes | "for a school night" |
| New in the library | "placed by the Cold Tower — no crowd data yet" (§8 stage-10 badge) |

Search or an active person-filter switches Home into the catalog grid; clearing it returns the shelves.

### 6.1 Rating view

v1.1 §4.3's sweep/battle design survives nearly intact — it was ahead of its time and the measurements vindicated it:

- **Modes:** **Mix** (default — alternates sweep and battle), Sweep, Battle; blocks of 15.
- **Sweep:** one title card → `Liked / Fine / Disliked` + **`Not seen`** + `Skip` + persistent Undo (owner decision 2026-08-29: one seen-state control — a title you can't remember is plain `unseen`, §4.2). Verdict implies `seen`. Running **class-balance widget** with its warning copy ("Heavy on 'liked'. Spreading across all three classes matters about five times more than anything else you can do here." — the measured 5× lever). Prediction reveal strictly *after* the tap (anchoring; Cosley 2003), phrased "we'd have guessed the same" / "we'd have guessed {class}". The side rail carries the learning-curve copy ("Personal signal roughly triples from 5 to 100 labels. Aim for 50–100 in the first sitting or two.").
- **Battle:** two posters are the buttons; `Tie` (feeds the Davidson tie term); a persistent **decisive toggle** sets the margin weight (~1.6 vs 1.0) with the copy "a decisive pick teaches more than a hesitant one" (long-press stays as an optional accelerator only). Pairs drawn **at random** from the user's seen titles within verdict bands — no clever selection for profiles (measured null; the reason ships as UI copy: "For profiles no selection rule beats random — the clever ones only pay off in the tier queue."). Corrections zone at the bottom (nothing tappable inside the poster cards), one row: `not seen: [left] [both] [right]` → sets that side `unseen`, swaps it out of the pair (`both` swaps the whole pair), writes no duel row, syncs per §7.3, covered by the persistent Undo.
- **Queue:** P(seen)-ordered (Jellyfin history, popularity, household co-seen), seeded first run from the imported 100-title decade-stratified `seed_list`. Blocks of 15; each card shows its queue reason ("queued because: 72% likely you have seen it"). Target: 50–100 verdicts in the first sitting or two — every measured curve says the model is still improving at 100 and beyond.
- Onboarding copy sets expectations from the learning curve: personal signal roughly triples from 5 → 100 labels.

### 6.2 Tonight (what to watch now)

**v2.1 redesign (owner decision 2026-08-29):** the visible shortlist stage and the mood-question round are gone. Participants each cast **~10 quick candidate votes**; the votes both decide the evening and carry the mood signal. The measured constraints that survive are marked inline; the round itself is owner-designed, not corpus-measured (§14 risk 7 — instrument it at M4).

1. **Initiator opens a session** and picks participants: members and/or N guests. Session controls: kind (film/series), a **runtime budget slider** (soft — the pool admits up to budget + 40 min; over-budget results are labelled "runs N min over"), and a **rewatch toggle** (default: exclude titles *every* participant has seen; "include rewatches" flips it).
2. **Join channels, all equivalent:** push to members' phones (best-effort, §6 preamble); **room code / QR** in the lobby; a live in-app lobby banner over the WebSocket; the **open-rooms list** — active sessions are visible to every household device ("MX-2210 · hosted by Mia · 3 min ago · Film · 60 min · skips seen") with tappable empty seats; and the TV route. **Guests use the initiator's phone after the initiator finishes** (hand-the-phone, sequential turns).
3. **Candidate pool (internal — never shown as a step):** owned titles passing the kind/budget/rewatch filters, ranked by the **plain average** of member Ledger scores (measured: nothing dominates averaging; dominance rules cost −0.012). Guests contribute no taste term unless they have a grid profile.
4. **The vote round (~10 votes per participant):** each participant answers ~10 this-or-that pairs of real candidates on their own device — "Which one tonight?" `A` / `B` / `either`. Pairs are drawn competitive (adjacent in group score), with partial overlap across participants so tallies are comparable, and a few pairs are spent spanning the pool's widest DNA axes — each vote is an **item-anchored comparison**, the same measured mechanism as v2.0's mood questions with candidates as the anchors. Guests' pairs are not seen-filtered (their seen-state is unknown; a persistent guest's grid picks can anchor instead). All votes stay **hidden until every participant has finished** — the blind-vote social property, preserved by construction.
5. **Combine:** per-title tallies averaged across participants. Each vote also yields a tilt observation — chosen-minus-rejected DNA, **centred on the candidate-pool mean** (the measured centring lever). A hard split — divergent votes on the top title, or Ledger divergence **D ≥ 0.20** (~14.5% of nights; below that, decide silently) — is **surfaced with the alternative in hand** ("You're split on light vs heavy — here's one of each"), never silently averaged. Conflict copy obeys the measured constraint (DNA_MODEL §5.3): D predicts "one of you is likely to land below your usual tonight" (AUC 0.610), never "someone will hate this" — a hard rule on the §6.6 conflict-phrasing LLM task.
6. **Result:** winner card — approval share, per-person match lines in DNA terms including the honest negative ("nothing here is their pull — *bleak* works against them"), and **Play on Jellyfin** as the primary CTA — plus runners-up and one **wildcard** ("a step outside your usual, honestly labelled"). Votes *choose*; nothing re-ranks within the evening by predicted enjoyment (measured: worth 0.000).
7. **Solo mode** ("Tonight, for {name}"): no session row. Three picks + wildcard from the same pool ranked by the personal Ledger, each with a one-line why ("pulls you with {terms}" / "a stretch — outside your usual") and a budget-fit line ("fits your 130 min" / "runs 21 min over"); a **reshuffle** control; optionally a few self-administered votes to sharpen the tilt.
8. Optional **TV kiosk route** (`/tv`, room code): lobby, progress, result. Carried from v1.1, nice-to-have.

**Persistent guests** (a friend who visits often): one 60-title grid, "pick ~10 favourites" ≈ 46 pairwise questions of information for 10 taps; never a pairs-only interview. Until they pick, a guest is scored by their session votes alone.

### 6.3 Rank view (the tier list)

- Per-user table/board of every rated title in tiers **F, D, C, B, A, A+, S** (configurable set; learned cutpoints, not percentile cuts — initialised from DNA_MODEL §4.5's measured quantile shape F 3 / D 7 / C 15 / B 25 / A 25 / A+ 17 / S 8 %, then learned). Badge shows tier + neighbourhood ("A — between Heat and Prisoners"); a straddling title shows "A/S" and becomes queue-eligible.
- **Filters:** genre, kind (movie/series — separate by default), decade, runtime, seen-state, DNA facet/term predicates ("show only `mood.cosy`").
- **Drag-and-drop rearrange** — the owner's requirement, implemented as Ledger observations: dropping a title into a tier emits a `tier_edit`; dropping it *between* two titles emits that edit **plus two margin-less duels** against its new neighbours. The model refits (incremental immediately, exact nightly); if the model disagrees strongly, the title's badge shows the tension rather than snapping back — the user is a data source, not a tyrant, and vice versa.
- **On phones:** tap a title (it lifts), tap a tier (it drops) — the same `tier_edit` semantics; drag-and-drop stays for pointer devices.
- **Comparison queue** ("sharpen my ranking"): boundary-targeted active selection (70% posterior-straddling pairs / 20% exploration / 10% uniform-random held out for honest evaluation — the adaptive-inflation guard). ~10–20 comparisons place a new title; a motivated ~500-comparison weekend yields a defensible first cut; the first *stable* tier list arrives at ~1,500–3,000 comparisons (rating-seeded estimate — the unseeded 55%-exact budget is ~3,000 with a measured 1,800–5,800 span controlled by judgement noise σ, unmeasured for these users until §13's re-ask stream runs); ~30–50/week maintains it.

### 6.4 Map (exploration)

- **v1 map = axis scatter (prototype-adopted; replaces the UMAP layout):** titles plotted on **two user-selectable bipolar facet axes with named poles** (mood: heavy ↔ light · pacing: patient ↔ propulsive · structure: linear ↔ fractured · visual: naturalistic ↔ composed · sound: designed ↔ musical · …), with zoom/pan, three lenses (**facet colour / seen-state / match-for-you**), and a **Show on map** jump from every title card. **Axis definitions are a shipped, authored artifact**: one TSV per vocabulary-v1 facet (left pole, right pole, term → weight ∈ [−1, 1]), authored the way the register antipode ledger was, shipped in `dna_vocab/v1/`, editable in the §6.6 ledger editor. Deterministic — no nightly rebuild, no Procrustes anchoring, no map shift on bundle re-import. **Binding note:** the prototype's facet set is not vocabulary v1 (it invents dialogue/tone/setting/craft and lacks place/era/sensibility/register) — everything facet-shaped binds to the real 11 facets at build time; era and sensibility need freshly authored poles. A UMAP similarity layout returns later (M7) as a fourth lens if wanted.
- **Wander:** click a title → its DNA card (tags with evidence quotes) and its neighbours by shared terms, each neighbour edge **labelled with the shared term**; follow edges term-by-term. Every connection is *nameable* — edges are DNA terms, never opaque similarity. The flywheel queue (§8.4) is docked on this surface.
- **Compositional search:** "Gladiator but with robots" = LLM parse → predicate (`has(robots) AND NOT has(gladiatorial)`) over `dna_tag ∪ title_keyword`, survivors ranked by similarity then personal score — the measured winner over embedding arithmetic (p@10 0.52 vs 0.46). The bound predicate is shown ("parse → has(robots) AND has(gladiatorial) · 3 survivors"); extracted-tier results first, projected-tier in a labelled second section (two-tier rule). Empty predicates land in the flywheel with their reason ("no owned title carries robots + gladiatorial").
- **Explore recommendations:** the *adjacent possible* — regions of DNA space near the user's liked regions but unvisited; policy from the measured explore analysis (~1 exploratory slot in 6, ranked by prior + proximity; cost ≈ −1 pp top-hit rate, honestly labelled). Also surfaced as a Home shelf (§6.0).

### 6.5 Taste comparison (any two profiles)

Compares **any two profiles** — members and persistent guests — via a two-slot picker, defaulting to Patrick vs Jenny. Four tabs: **Facet silhouette / The seven axes / Divisive / Shared sweet spot**.

- **Facet silhouette:** butterfly/radar over the 11 DNA facets — each person's Ledger-weighted affinity, overlaid; agreement shaded, divergence highlighted.
- **The 7 shipped taste axes** from the verified axis analysis (CONTENT_TASTE §5: "Ship 7, not 8"): Demanding↔Easy, Cold↔Warm, Playful↔Weighty, Another-world↔This-world-rude, Head-trip↔Joke-delivery, Horror↔Big-franchise, Enchantment↔Speculation — each with its calibration film pair, both users plotted per axis. The 8th, Small-and-observed↔Big-and-staged, is **held back**: it carries the generosity/coverage confound (r = −0.80 with the user's mean-affinity offset) and merges into axis 1 under a row-centred refit; it ships only after re-derivation from an offset-free factorisation. (K = 8 stays the right factorisation dimension — the hold-back is a surfacing rule.)
- **Divisive-title list:** where the two Ledgers disagree most, with the disagreement *explained in DNA terms* ("Jenny likes the slow pacing; Patrick doesn't") — contestedness must beat the "crowd-divisive" baseline so it reflects *these two people*, not divisive films in general. The §6.2 copy rule applies here too: divergence copy predicts a relatively worse night, never active hate.
- **Shared sweet spot:** the region both like — doubles as the couple's watch-now prior, and as an acquisition lens.

### 6.6 Admin view (admin role only)

- **Connectors:** Jellyfin (URL, API key, library pick, user-mapping table, test button, sync now, webhook status); **LLM providers — Gemini, Anthropic, OpenAI**: per-provider key + model pick, a per-task model assignment (extraction / query parsing / conflict phrasing), and **parallel mode**: run extraction on N selected models and merge by the measured consensus rule (union with per-tag agreement as confidence — union recalls 93% vs 67% for intersection; agreement is a weight, never a filter; the numbers ship as the toggle's caption). Provider cards caption their structured-output mode (Gemini "batch", Anthropic "forced tool-use", OpenAI "strict schema"). Spend guard: per-title cost estimate before enabling, monthly cap, running meter reading "$4.12 of $25.00 this month" (corpus baseline: ~$0.005–0.01/title/pass on Gemini batch). TMDB / OMDb / Trakt keys with test buttons.
- **Data:** artifact-bundle import wizard (validate → report → hot-swap; §10), acquisition pipeline monitor (per-title stage board from `acquisition_job`), extraction queue (§8.4) with approve/spend controls, review of DNA rejects and low-evidence tags — ledger editors writing the TSV formats the corpus project already uses: `adjudications_v1.tsv` for DNA verdicts *and* `corrections_v1.tsv` for credit facts, so app-side fixes survive every future re-derive.
- **Users:** create/edit, roles, passkey management, Jellyfin links, guest profiles. Creation issues a one-time password; the account is locked to a password change at first login; passkey registration is prompted afterwards (§3.1).
- **System:** job health, queue depth, last syncs, backup status, logs.

---

### 6.7 Model log (transparency rail)

A per-user toggle (default off) reveals an ephemeral log (last ~15 events, never persisted) narrating every model write in one human-readable line: `verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms` · `tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours` · `session_answer(p, pair 4) = A — pool-centred tilt` · `parse → predicate has(robots) · 0 survivors → flywheel`. It is "drag-and-drop is data, not override" made visible, and the primary M2 debugging instrument.

### 6.8 Design language (from the prototype, normative)

Dark-first single-theme UI: ground `#0d0d0f`, cards `#141416`, one ember accent `#c8613a` spent on selection and primary actions. Type: Space Grotesk (display/body) + JetBrains Mono for every model number, ID and data annotation (the "data voice"). A fixed colour per vocabulary facet (11). Poster-forward 2:3 cards. The copy register is **"quiet reasons"**: every shelf, recommendation, question and conflict carries a one-line why in vocabulary terms, and model numbers appear in the data voice next to their name (`b(t) 0.52 · β 0.8 · gate 0.93`), never bare.

## 7. Jellyfin integration

### 7.1 Connector

Ported auth + field list from the corpus connector (`X-Emby-Token`, `/Users`, `/Users/{id}/Items` with the proven FIELDS set incl. ProviderIds, MediaStreams, DateCreated, UserData). Pin Jellyfin ≥ 10.9; new read code prefers the modern `/Items?userId=` route (the corpus connector's `/Users/{id}/Items` is the legacy alias). Identity: ProviderIds → `imdb/tmdb/tvdb` + `jellyfin_id`; upsert into `title` by the ported fill-never-clobber resolver. **Play on Jellyfin** (title card, Tonight winner): deep-link to the server's web player (`{jf_url}/web/#/details?id={jellyfin_id}`); direct playback is a later refinement.

### 7.2 New-title trigger (owner requirement: every Jellyfin add triggers acquisition)

- **Primary:** the Jellyfin **Webhook plugin** → `POST /events/jellyfin` (`ItemAdded`), token-authed. Debounce 10 min (library scans add seasons in bursts; series acquire per-show, not per-episode).
- **Fallback:** 15-minute delta poll on `DateCreated > last_sync` (the corpus connector is full-mirror only — the delta path is new code, flagged as such).
- Both paths enqueue an **acquisition job** (§8) per new title and mark removed titles `is_owned = false` (flag re-derived from Jellyfin, never trusted stale — the corpus flag is equivalent to `jellyfin_id IS NOT NULL` today but goes stale the moment the library changes).

### 7.3 Seen-state sync (two-way; new relative to the corpus project's read-only stance)

- **App is authoritative for explicit user actions.** `seen`/`unseen` set in the app writes Jellyfin's per-user Played flag via `POST/DELETE /UserPlayedItems/{itemId}?userId=` (the ≥10.9 route). **Jellyfin API keys are unscoped and admin-equivalent — no read-only variant exists** — so the setup copy says exactly that ("the Jellyfin API key grants full server access; this app uses it for reads and per-user Played writes only") and write restraint is enforced by this app's code alone. Preferred least-privilege write path: at user-link time obtain per-user access tokens (`POST /Users/AuthenticateByName`) for the linked users, store them in `connector_config`, and use each user's own token for their Played writes (a user token can only set its own state); the admin key stays read-only-by-policy for library/user enumeration. Costs: one-time password entry per linked user; a 401 on write → re-link prompt. The mapping is the plain boolean: `seen` → Played = true, `unseen` → Played = false (v2.1 — the `forgotten` state is gone).
- **Jellyfin playback is a suggestion, never a silent write:** ≥90% playback (poll `/Sessions` + `IsPlayed` delta) arms a per-user prompt — "Did you finish X?" → one tap sets `seen` and offers the verdict flow (v1.1 §4.1 capture, kept). Push notification if the user isn't in the app — best-effort (§6 preamble); when undeliverable, the prompt queues and surfaces as an in-app banner on next open. The banner path is the whole M1 behaviour; push arrives with the M4 stack.
- Conflict rule: last-writer-wins with the app's explicit action outranking Jellyfin's inferred state; `jf_synced_at` prevents loops.
- **External playback events** (v1.1 §6.1, kept verbatim): `POST /events/playback` token-authed, for the theater path that bypasses Jellyfin — and this is the designated Home Assistant hook (§11).

---

## 8. New-title acquisition workflow

Ported skeleton: the corpus project's raw store (content-addressed, immutable — crawl once, re-parse forever), durable `(kind,key)` queue, per-host rate-limited HTTP layer, and the single-title prototype (`mdc probe`). New code: per-title incremental derive (the corpus `rebuild`/`project`/`ingest` are deliberately wholesale).

**Pipeline per title** (stages visible in the admin board):

```
1  identify      Jellyfin ProviderIds → title row (fill-never-clobber)
2  enrich        tmdb:resolve → tmdb:detail (metadata, credits, keywords)
                 wikidata:resolve (→ MC/RT/Letterboxd slugs — halves guessing)
                 omdb:detail · trakt:summary→comments · wikipedia:article
                 tvmaze:show (series) · rt:page · metacritic:page→reviews
3  derive        per-title parse of raw docs → title_meta/credit/review/…
                 (single-title version of the corpus rebuild, keyed entity_key);
                 ends by applying BOTH curated ledgers — adjudications_v1.tsv
                 and corrections_v1.tsv — exactly as the corpus rebuild runs
                 corrections last: a derive that regenerates rows without
                 re-applying them silently reverts curated fixes (§14.5)
4  reviews gate  pack requires plot + multi-source reviews ≥50 words; if thin,
                 retry window 30 days (new releases accrue reviews over weeks)
5  dna pack      ported packs.py (interleaving, caps, norm()) + craft supplement
                 (wikipedia craft sections; NOTE: the RT critic-blurb pool is a
                 frozen 2020 dataset → genuinely new releases get thinner
                 sound/visual facets — known gap, mitigation: wiki sections +
                 metacritic critic excerpts, revisit if coverage measures poor)
6  dna extract   LLM structured call(s) against the frozen vocabulary —
                 1..N providers per admin config; two passes recommended
                 (union +13pp recall); ~20–27k input tokens/pass/title
7  verify        ported trust boundary verbatim: term-in-vocabulary (after
                 alias repair + adjudication rename), quote-substring-of-pack
                 via norm(), salience ∈ {1,2,3}. 100% catch rate on fabricated
                 tags in the corpus pilot. Failures drop, never repaired.
8  project       per-title alias-map projection of its keywords (incremental —
                 new code, same alias map)
9  place         feature vector per the feature contract → Cold Tower →
                 ê(t), b̂(t); genome block zero-imputed (unavailable for new
                 titles by construction)
10 ready         appears in ranking/search/explore with a "new — model
                 placement, no crowd data" badge until ratings accrue
```

Failure at any stage parks the job with a reason, retryable from admin; paid stages (6) never auto-retry past the spend cap. All fetched bytes land in the app's own raw store, so re-parsing is free forever.

### 8.4 The extraction flywheel

A queue of *naming failures* that future LLM spend should fix, fed automatically: compositional queries hitting empty predicates, explore frontiers with no vocabulary coverage, titles whose "unnamed taste" residual share is high, thin-facet titles. Admin reviews the queue, picks a batch and providers, sees the cost estimate, launches. This is how DNA breadth grows *where it's needed* — breadth being the measured binding constraint (zero owned titles carry `themes.robots`).

---

## 9. LLM connector layer

Ported design (no vendor SDKs; one POST per provider through the rate-limited fetcher; batch endpoints supported):

- **Providers: Gemini, Anthropic, OpenAI** — the three adapters exist in the corpus project with the per-provider structured-output quirks already solved (Anthropic forced tool-use; OpenAI strict-schema keyword stripping; Gemini responseSchema + header-only API key so credentials never hit logs). Port verbatim, including the reasoning-token accounting corrections (Gemini bills thinking tokens as output — counting visible JSON understates cost ~5×).
- **Admin-configurable:** per-provider key/model, per-task assignment, **parallel/consensus mode** (N models extract independently → union-merge with agreement-as-confidence, the corpus pass-0/pass-consensus pattern), batch-vs-sync toggle, spend caps + meter.
- **The schema is a cost-saving device, not the guarantee — the guarantee is the validator** (ported principle). Two-attempt pattern: retry once with the specific contract violation named.

---

## 10. Data migration (corpus project → app)

**The bundle** (produced by an export script in the corpus project; versioned; the app's importer validates then loads):

| Part | Contents | Size |
|---|---|---|
| `content.sqlite` (or pg_dump) | title spine, meta, aliases, genres, keywords, credits/people, awards, platform ratings (display schema), DNA layer (tag + projected + evidence + annotation + term_signal + exclusion), genome + link slice, `rating_source` (mandatory always), `rating_title_map`, seed_list tables, watchlist | ~750 MB |
| `reviews.sqlite` | all 485,602 reviews with bodies (needed for future re-extraction and text embedding) | ~312 MB |
| `artifacts/` | backbone + item stats (slimmed `content.npz`) + cold tower (`cold_tower2.pt`) + feature contract + content_X + review-text emb (+ SVD components) + `ledger_hyperparams.json` + calibration files (`manifest.json` cut-points, `equating_map.json`, shares grids, `audit.json`) + `dna_vocab/v1/` + `corrections_v1.tsv` + `seed_list.json` + `judgement_set_v1.tsv` | ~60 MB (+200 MB with text components) |
| *(optional, later)* `ratings.npz` | the cleaned 94.8M-row corpus — only if in-app retraining is ever wanted | +2.4 GB |

Total ≈ **1.15 GB** uncompressed (~0.35 GB compressed). Importer enforces every §4.1 landmine rule and produces a migration report (counts per table, validation failures, vocabulary version). Bundle re-import (new vocabulary, retrained backbone) is a planned admin event with a diff report — never a silent sync. Ledger *observations* always survive re-import (they reference `title.id` and vocabulary-independent facts) — but the rebuild set is larger than the DNA caches: **everything expressed in the old Backbone's basis is garbage against a new one**. Re-import therefore recomputes: user fold-in vectors (closed-form, ms), per-label-count blend weights, a full Ledger MAP refit, Cold Tower re-placement of every app-acquired title (feature vectors rebuilt from the staged bundle's feature contract, whose column set may change). (The v1 Map is a deterministic axis scatter and needs no rebuild — a future UMAP lens would recompute here.) Swap sequence: validate → stage to `/data/artifacts/<version>/` → recompute the rebuild set against the staged bundle → transactionally flip `artifact_bundle.active` → restart backend + worker (simplest correct option at household scale). Invariant: no process may score or refit with a loaded bundle version different from the active row.

**Corpus-project deliverable to build:** `mdc export-bundle` implementing this manifest — deny-listing `%_bak%`/`%_good`, live tables only, `content.npz` slimmed by dropping the per-user affinity arrays (`aff` + `aff_den`, ~1.29 GB) while keeping the item stats (incl. `item_n`), filtered `imdb_ratings`/`ml_link` joins, both curated ledgers, the feature contract with its frozen `text_scale`, and the tuned ledger hyperparameters persisted to `ledger_hyperparams.json` (today the tuner only prints them).

---

## 11. Home Assistant (later; independence guaranteed)

The app is fully standalone; HA integration is additive via three existing seams: `POST /events/playback` (theater finished a film → arm rating prompts), presence hints for session lobbies (optional `person.*` states → suggested participants), and a webhook/service to launch a watch-now session from an HA dashboard (returns the session URL/room code; phones join via push as normal). No HA dependency anywhere in core flows.

---

## 12. Build order

| M | Contents | Exit criterion |
|---|---|---|
| **M0** | Compose skeleton, Postgres schema, first-boot wizard (§3.1 sequence), auth (passwords + sessions), bundle importer + validation report, artifact loading, Library view (§6.0) | bundle imports clean; Library list and title card render imported titles |
| **M1** | Jellyfin connector + user linking + seen-state two-way sync (finish-prompts as in-app banners; push waits for M4); passkeys | seen states flow both ways for both users |
| **M2** | Rating view (mix/sweep/battle, class balance, undo, queue, seed list) + Personal Ledger (all four arms) + nightly refit; scoring stack + per-user ranked lists; **Home shelves (§6.0)**; model-log rail (§6.7); member PWA-install/push onboarding; placement reconciliation (§5.3) | 50–100 verdicts each produce visibly personal rankings — the first real-user validation of the whole corpus project; every owned title has a coordinate (warm Backbone row or Cold Tower placement) |
| **M3** | Rank view: tiers, filters, drag-drop, comparison queue | stable tier lists both users endorse |
| **M4** | Tonight: lobby + open-rooms discovery, push join, the ~10-vote round, guest hand-off, group combine + conflict surfacing, blind reveal, **solo mode** (+ TV route) | a real Friday night resolved by the app |
| **M5** | Acquisition pipeline + admin connector UI + LLM layer + extraction flywheel | a new Jellyfin add reaches "ready" unattended |
| **M6** | Map (axis scatter) + compositional search + taste comparison viz | — |
| **M7** | Guest grid profiles, acquisition/"wanted" lens, UMAP similarity lens, HA hooks, comfort-shelf carryover | — |

M2 is the gate — and the moment the corpus project's biggest open caveat ("does any of this transfer to two real people?") gets its answer. Instrument it: per-user held-out accuracy from day one, compared against the crowd-measured expectations.

## 13. Evaluation

Surviving v1.1 §8 rows, inlined with their targets: pick-through > 60%; post-watch quality ≥ manual picking; rating capture > 70% of finished playbacks; winner approval share (target: the winner appears in a majority of every participant's favourable votes); satisfaction spread < 0.3; diversity drift — no sustained narrowing. Dropped rows: profile coverage over n_u, ledger drift over deficit_i, session length ≤ 10 (all tied to the deleted 8-axis/fairness machinery; the mood round is 3–5 questions by design). Added: per-user held-out Spearman vs the crowd-derived expectation curves (§0 row 1's numbers are the M2 yardstick); within-liked resolution as duels accrue; not-seen rate in the rating queue (>50% = queue bug); cold-title badge accuracy once new titles get rated; DNA coverage of naming events (queries answered vs sent to flywheel).

Two mandated data streams from day one: (a) the 10% uniform-random comparison stream is the *only* data used to evaluate the tier model — adaptively-selected pairs inflate reliability (measured effect; the guard is non-negotiable); (b) a separate silent **re-ask stream** — ~10% of comparisons/verdicts re-asked after ≥3 days; ~200 re-asks measure the flip rate σ that sets the tier budget (DNA_MODEL §4.2 build-order #1) and settles the corpus's zero-test-retest-data unknown. The two streams are different instruments: the uniform-random stream cannot measure test–retest consistency.

## 14. Risks

1. **Two-person reality vs crowd proxies** — every number is a crowd measurement; M2 is the test. Mitigation: expectations instrumented, not assumed.
2. **New-release DNA thinness** (craft-blurb gap, review-accrual lag) — mitigations in §8 stage 4/5; measure facet coverage of post-2025 titles.
3. **Jellyfin credential custody** — API keys are unscoped and admin-equivalent (no read-only variant exists), so the stored connector secret can administer the whole media server; write restraint is app-side policy only. This raises the stakes on §2's secrets encryption, and is why §7.3 prefers per-user tokens for the write path.
4. **WebAuthn origin coupling** — `PUBLIC_URL` change invalidates passkeys; wizard warns loudly.
5. **Per-title incremental derive is new code** replacing deliberately-wholesale corpus steps — port the invariants (raw/derived split, idempotent re-ingest, adjudication- **and corrections-**at-derivation — two distinct ledgers: DNA verdicts at ingest, source-credit facts at rebuild) or inherit the bugs they were built to kill (the 787-rows-reverted-twice scar).
6. **The vote round is unmeasured** — the ~10-candidate-vote elicitation (v2.1, owner decision) replaces the corpus-measured shortlist + question instrument. The surviving measured constraints are encoded in §6.2; the round itself must be instrumented at M4 (log every vote; compare winner satisfaction against the solo baseline) before anyone tunes it.
7. **Vocabulary evolution across the project boundary** — vocabulary changes happen in the corpus project and arrive as bundle re-imports with migration reports (v1.1 §10.3-5 stands).

---

## Appendix A — requirement coverage

| Owner requirement | Where |
|---|---|
| Standalone app: backend, db, frontend | §1, §2 |
| Dockerized | §2 |
| Jellyfin connector via API + key | §7.1 |
| Admin UI for connectors (Jellyfin, LLM) | §6.6 |
| Extract all relevant data from this project into the new DB | §10 (+ corpus-side `mdc export-bundle`) |
| Data workflow for new entries, triggered on Jellyfin add | §7.2, §8 |
| Later HA integration, everything independent | §11 |
| Desktop + iPhone + Android browser UI | §6 preamble (PWA, phone-first) |
| Views: Home/Library, Rate, Tonight, Rank, Map, Taste, Admin | §6.0–6.6 |
| User management + biometric login | §3 (passkeys) |
| Unseen marking in rating & pairwise UIs (owner 2026-08-29: one `not seen` control — "don't remember" is plain unseen) | §6.1, §4.2 |
| Unseen state synced with Jellyfin; account linking | §7.3, §3.3 |
| Tonight elicitation (owner 2026-08-29: ~10 candidate votes supersede the seen-only mood pairs) | §6.2 steps 4–5 |
| Participant flow: count prompt, push to named users' phones, guests on initiator's phone | §6.2 steps 1–2 |
| Taste comparison visualization (Patrick vs Jenny) | §6.5 |
| Rank view: tier table F–A+–S, filters, drag-drop that updates the model | §6.3, §5.2 |
| Comparisons increase resolution; relative 0–1 weights; rankings stay current | §5.2 (measured), σ-inflation refresh |
| CPU-only model updates | §1 constraint, §5.3 budgets |
| Explore view (connections, wandering) | §6.4 |
