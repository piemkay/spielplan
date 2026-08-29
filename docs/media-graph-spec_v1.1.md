# Media Graph — Discovery & Recommendation System

**Status:** design specification, v1.10 (reviewed)
**Scope:** a personal movie/TV discovery system built on a single weighted knowledge graph, running on top of a Jellyfin library (~600 titles).
**Deployment:** one standalone Docker container (frontend + backend + database), connecting to Jellyfin over its API with an API key (§6.1).

---

## 1. Overview

The system maintains one weighted graph in which titles, people, genres and LLM-derived *aspects* are nodes. All features are views or traversals of this graph.

Two user-facing parts are specified here:

| Part | Purpose | Input | Output |
|---|---|---|---|
| **A. Taste profiling** | Learn what a user likes, permanently | A 3-way verdict plus a few A-or-B duels per watched title, plus bulk **rating mode** | Per-user weights on graph nodes + a per-title quality ranking |
| **B. Tonight's pick** | Find what to watch *now*, alone or as a group | ~10 A-or-B comparisons per participant | Ranked shortlist with reasons, resolved by a group vote |

Part A is slow, cumulative and per-user. Part B is fast, disposable and per-session. Part B consumes Part A's output but never writes to it.

Part A has two entry points: rating a title right after watching it (§4.1), and **rating mode** (§4.3) — a dedicated bulk flow where the user is shown one title at a time and either rates it or marks it not seen. Rating mode is what populates the database at the start; without it the system takes months to become useful.

A third component, the **3D explorer**, is a visual traversal of the same graph and is specified in section 7.

### 1.1 Design principles

1. **Rating must cost one or two taps.** Anything heavier will not be done consistently and the profile starves.
2. **The graph explains itself.** Every recommendation must be renderable as a path or a list of contributing aspects. No opaque scores.
3. **Quality and appetite are different things.** A film can be excellent and still be the wrong film for a Tuesday.
4. **Measured beats asserted.** Where a signal can be computed from the media file, prefer it over an LLM claim.
5. **One container, one port, one volume.** This is a household appliance, not a platform. Setup is an API key and a URL.

### 1.2 Non-goals (v1)

- No social features, no cross-user collaborative filtering (the user base is a household).
- No streaming-availability aggregation. Titles outside the library are modelled (§3.5) for profiling and acquisition, but tonight's pick only ever recommends something that can actually be played.
- No automatic playback. The system recommends; the user presses play.

---

## 2. Seen state

A title has exactly **two states per user**: `seen` or `unseen`.

- `unseen` is the default.
- Marking a title `seen` is what unlocks rating.
- A user may flip a title back to `unseen` at any time, with the meaning *"I don't remember this well enough to have an opinion."*

Consequences of flipping back to `unseen`:

- The title becomes eligible for recommendation again.
- Any existing rating is **retained but marked `lapsed`**. Lapsed ratings still contribute to the taste profile at reduced credit (factor `0.5`), because the opinion was real when it was formed — it is only the memory of the title that has gone.
- If the title is watched and rated again, the new rating supersedes the lapsed one entirely.

Jellyfin playback state may be used to *suggest* the `seen` flag (prompt: "Did you finish this?"), but never to set it silently.

---

## 3. The graph

### 3.1 Node types

| Type | Source | Approx. count |
|---|---|---|
| `title` | Jellyfin (`owned`) + TMDB (`catalog`, §3.5) | 600 owned + 5–10k catalog |
| `person` | TMDB, role-filtered | 1,500 (owned) |
| `genre` | TMDB | 25 |
| `aspect` | LLM, canonicalised (§3.3) — final count determined by analysis | ~400–600 expected |
| `franchise` | TMDB collections | 60 |
| `era` | derived from release year, decade buckets | 10 |

Person nodes are filtered by role. Only these roles create nodes: director, writer, DP, composer, editor, production designer, and the top 6 billed cast. Everyone else is dropped — a graph that includes the second unit gaffer is noise.

### 3.2 Edge types and weights — the title DNA

Every title carries a **DNA vector**: a sparse, interpretable embedding over the aspect vocabulary. Most entries are zero — absence is the natural zero for aspects, and an aspect a title doesn't have carries no value at all. This is deliberately an *interpretable* embedding rather than a learned one: every dimension has a name, a definition, and an evidence string, which is what chips, explanations, mood axes and the explorer all hang from. A learned latent embedding would be strictly worse at everything this system does except raw similarity.

**`title —[has_aspect]— aspect`** — the load-bearing edge type.

```
w(t,a) = salience(t,a) × idf(a)
idf(a) = log(N_titles / df(a))
```

then L1-normalised per title so `Σ_a w(t,a) = 1`.

**Salience is derived, never asked for as a number.** An LLM asked to emit an absolute salience score has exactly the failure mode that disqualified absolute ratings for humans (§4.1): compression toward the top of the range, drift between calls, no cross-title comparability. Instead, strength is composed from judgements the model is actually reliable at, plus things that are simply measured:

```
salience(t,a) = percentile_a( level(t,a) × coverage(t,a) )
```

- **`level(t,a)`** — a coarse ordinal from pass 4, three anchored options only: `defining` (1.0) / `major` (0.6) / `present` (0.3). Three levels is the granularity at which an LLM's absolute judgement holds up — the tagging-side analogue of Liked/Fine/Disliked.
- **`coverage(t,a)`** — the fraction of independent input sources (synopsis, each review excerpt, measured signals) whose evidence supports the aspect. Measured, not judged: the model attributes evidence, arithmetic does the rest. The most trustworthy signal in the composite.
- **`percentile_a(·)`** — the raw product is calibrated **per aspect** across the corpus: a title's final salience for `slow burn` is its percentile among slow-burn titles. This is what makes `0.9 slow burn` mean the same thing everywhere. Calibration parameters ship in the vocabulary file (§3.3.2) so the incremental tagger applies the identical mapping.
- Pass 1's salience *ordering* of the 30 extracted phrases (rank decay `1/√rank`) weights phrases entering the clustering, and is not used at runtime.

Division of labour: salience says how strongly *this title* expresses the aspect; `idf` says how much the aspect distinguishes *any* title; neither does the other's job. An aspect present in >60% of the library is dropped from the vocabulary at build time (a cheap pre-filter; the binding upper bound is pass 3's `0.4·N` discriminativeness cut). Per-title L1 normalisation means every title distributes exactly one unit of credit, so a densely-tagged title cannot dominate the profile.

**`title —[has_person]— person`**

```
w(t,p) = role_weight(role) × billing_decay
```

| Role | `role_weight` |
|---|---|
| director | 1.00 |
| writer | 0.65 |
| DP | 0.60 |
| composer | 0.55 |
| lead actor (billing 1–2) | 0.70 |
| supporting (billing 3–6) | 0.35 × (1/billing) |
| editor, production designer | 0.30 |

**`title —[has_genre]— genre`** — weight 1.0 for primary genre, 0.5 for others. Genres are kept mostly for filtering and for the explorer; aspects carry the real signal.

**`aspect —[co_occurs]— aspect`** — derived, `w = PMI(a1,a2)` normalised. Used for graph walks and layout, not for taste updates.

**`title —[similar_to]— title`** — derived and cached: cosine similarity of DNA vectors, top-20 per title, threshold 0.25. Purely a performance shortcut for the explorer.

### 3.3 Aspect vocabulary construction

Free-form LLM tagging produces thousands of near-synonyms and destroys the graph's connective value. Build the vocabulary in four passes. The final vocabulary size is **not** fixed up front — it falls out of the analysis in pass 3.

**Scope split: the corpus work is a separate project, not part of the app.** Passes 1–3 and the initial pass-4 tagging of the full corpus are one-off development work with no reason to live inside the appliance. They run as a standalone **corpus project** — scripts, notebooks, whatever gets it done — whose deliverables are two flat files (§3.3.2): the aspect vocabulary and the tagged title corpus. The app **imports** those files as a one-off step and never contains clustering, embedding, or vocabulary-sizing code. The only LLM machinery the app itself ships is the incremental tagger: the pass-4 call, applied to new titles as they appear, against the imported vocabulary. The rest of this section is therefore the corpus project's method spec; the app's obligations are §3.3.2.

**Pass 1 — extract (per title, fixed budget).**
Input to the LLM: title, year, runtime, genres, full plot synopsis, 3–5 review excerpts, and the measured signals from §3.4. Explicitly *not* the LLM's own memory of the film — the prompt instructs it to tag only from provided material, to limit confabulation.
Output: **exactly 30 free-form aspect phrases** with a salience score each, ordered by salience. The count is enforced mechanically, not rhetorically: the call uses **structured output** (a JSON schema with `minItems = maxItems = 30` and bounded salience), the payload is validated before anything is written, and a failed validation retries with the violation named in the prompt. A plaintext instruction to "produce exactly 30" will drift to 28 or 34 often enough to reintroduce exactly the per-title count variance the fixed budget exists to remove.

A fixed count per title rather than a range keeps the corpus balanced — an LLM left to choose will emit 12 aspects for a simple film and 45 for a dense one, which biases IDF toward whatever it found interesting. 30 is deliberately generous: over-extraction is corrected downstream by clustering and pruning, under-extraction is not recoverable without re-running the whole pass.

**Corpus scope.** Run extraction over a corpus substantially larger than the local library — TMDB's top 5,000–10,000 titles by popularity, plus everything owned. Rationale:

- IDF over 600 titles is noisy; over 8,000 it is meaningful.
- Cluster structure only becomes clean with enough phrases per concept.
- The resulting vocabulary is then stable when the library grows, and it is a prerequisite for the catalog mode in §3.5 anyway.
- Cost: 8,000 titles × ~1.5k output tokens is a one-off batch job, cheap enough to redo if the prompt changes.

All LLM stages run against a **hosted API** — there is no local model anywhere in the pipeline, which is what keeps the GPU-less host (§6.1) viable. Three consequences worth designing in:

- **Use the provider's batch endpoint** for the corpus-wide passes (extraction, re-tag). Batch APIs typically halve the price and remove rate-limit management entirely — the tagger worker submits, polls, and ingests results idempotently, with per-title checkpointing so a partial batch is never wasted.
- **Tier the models by leverage, not habit.** Pass 1 extraction is grounded generation from provided text — a cheap mid-tier model does it well, and at 8k titles the price difference is the whole bill. The small-volume, high-leverage steps — cluster canonicalisation (pass 2), aspect typing, and especially the mood-axis loadings (§5.1), where one bad judgement corrupts an entire axis — get the strongest available model; their token count is trivial.
- **Ballpark:** pass 1 over 8k titles ≈ 8k × (~2k in + ~1.5k out) ≈ 28M tokens — tens of euros on a mid-tier model via batch, low hundreds on a frontier model. Pass 4 re-tag is similar. Affordable to redo, which is the property that matters when the prompt inevitably changes.

Corpus selection needs one guard: TMDB popularity alone skews recent and blockbuster-heavy, which starves exactly the vocabulary a cinephile household needs. **Stratify by decade** — top-N by popularity *per decade* plus top-rated-per-decade lists — so that a 1970s-heavy taste can be expressed and rating mode's catalog draw (§4.3.2) isn't a parade of last year's franchises.

Ingestion itself is a throttled, **resumable** batch job: one `append_to_response` call per title fetches details, credits and reviews together, so the full corpus is ~8–10k requests — under an hour even at a polite 5 req/s. Progress is checkpointed and upserts are idempotent, so a `429` backs off and resumes rather than restarting; after first build, TMDB's *changes* API supplies deltas instead of re-pulls.

**Pass 2 — cluster.**
1. Embed all extracted phrases (~240k phrases at 8k titles).
2. Agglomerative clustering with cosine distance. Sweep the threshold from 0.75 to 0.90 rather than fixing it — the right value is whatever produces the cleanest silhouette at a usable granularity.
3. For each cluster, the LLM produces a canonical name, a one-sentence definition, and an aspect *type* (§3.3.1).

**Pass 3 — prune and size the vocabulary.**
This is the decision step. Score every candidate cluster on three axes and keep the top X:

| Criterion | Measure | Why |
|---|---|---|
| **Discriminativeness** | `df` in `[0.02·N, 0.4·N]` | Present in too few titles → connects nothing. Present in too many → separates nothing. |
| **Distinctiveness** | max cosine to any other retained cluster centroid `< 0.75` | Two aspects that always co-occur carry one bit between them; keep the better-defined one and drop the other. |
| **Predictive value** | mutual information between the aspect and held-out user ratings, once ratings exist | The only criterion that measures whether the aspect predicts anything a person cares about |

The third criterion is unavailable at first build. So: **build v1 of the vocabulary on the first two criteria, then refit after ~200 ratings exist** and drop aspects that carry no predictive signal.

Choosing X: plot retained-aspect count against (a) mean pairwise title-similarity separation and (b) mean credit accumulated per aspect node at a simulated 100 ratings. The first rises with X, the second falls. Take the knee. Expect it to land somewhere around 400–600, but the analysis decides, not the guess.

Then a manual review pass over the retained list. At this size it is a few hours of work and it is worth doing — this vocabulary is the system's backbone.

**Pass 4 — tag the DNA (per title, against the fixed vocabulary).**
Present the vocabulary (chunked) and ask, per applicable aspect, for exactly three things: the **level** (`defining` / `major` / `present` — the coarse ordinal from §3.2, structured output, no free numbers), the **evidence** attribution per input source (which of the synopsis / review excerpts / measured signals support it — this is what `coverage` is computed from), and a short evidence quote. Salience is then *derived* per §3.2, never emitted by the model. Evidence strings are stored and shown in the UI as justification, and make bad tags easy to spot. After the corpus run, the per-aspect percentile calibration is fitted and written into the vocabulary file.

**Validation against the MovieLens Tag Genome.** The corpus project gets a free external QA target: the MovieLens 25M dataset ships `genome-scores` — dense, community-derived relevance scores for 1,128 tags across ~13k movies (Vig, Sen & Riedl 2012). For vocabulary aspects with a matching genome tag (map by embedding similarity, confirm by hand — expect a few hundred matches) and titles present in both corpora, compute per-aspect rank correlation between derived salience and genome relevance. Low-correlation aspects are where the LLM tagging is drifting from human consensus and deserve a look; systematically low correlation means the extraction prompt is broken. Research-use license: validation artifact only, never shipped or imported into the app.

Re-tagging on vocabulary change is idempotent. The initial corpus-wide run is the corpus project's job; **the app implements the identical call as its incremental tagger** — when Jellyfin sync detects a new title, fetch its TMDB metadata, run the pass-4 prompt against the imported vocabulary, derive salience via the shipped calibration, write the edges. One title, one call, no pipeline. A corpus-wide re-tag after a vocabulary revision happens back in the corpus project, followed by a fresh import.

#### 3.3.0 TV shows

Aspects are tagged **per show**, not per season or episode. Input to the extractor is the series synopsis, the season-by-season summary, and review excerpts covering the run as a whole. Measured signals (§3.4) are averaged over a sample of episodes spread across the run.

Consequences to accept: a show that changes substantially across its run gets a blurred tag set. This is the right trade for v1 — per-season tagging multiplies extraction cost by ~5, complicates the seen/rating model (a rating would need a season scope), and matters for only a handful of titles. The schema keeps a nullable `season` column on `title` so per-season nodes can be added later without migration.

Runtime for a series is the mean episode runtime, used for the time-budget filter; episode count is a separate attribute shown in the UI.

#### 3.3.1 Aspect types

Every aspect carries exactly one type. Types are what allow taste and mood to be reasoned about separately.

| Type | Examples | Used by |
|---|---|---|
| `tone` | bleak, wry, cosy, sardonic | taste + mood |
| `demand` | harrowing, requires full attention, comfort-watch, emotionally exhausting | mood (dominant) |
| `pacing` | slow burn, relentless, episodic, meditative | mood (dominant) |
| `structure` | nonlinear, anthology, single location, unreliable narrator | taste |
| `craft` | long takes, practical effects, painterly, sound-design-forward, handheld | taste (dominant) |
| `theme` | grief, bureaucracy, coming of age, revenge, class | taste |
| `subject` | heist, courtroom, submarine, first contact | taste |
| `setting` | cold war europe, near-future, small-town americana | taste |
| `presentation` | 4K HDR, dolby vision, object audio mix, lossless audio | filter + showcase boost (§3.4.1) — **never taste** |

The split matters: `craft` and `theme` aspects describe *what you like*; `demand` and `pacing` aspects describe *what you can handle tonight*. The Requiem-for-a-Dream problem is exactly a high `craft` score colliding with a high `demand` score.

#### 3.3.2 Corpus project deliverables and import

The contract between the corpus project and the app is three flat files (CSV or JSONL — nothing cleverer is needed):

```
vocabulary   aspect_id, canonical_name, definition, type, df,
             calib_quantiles,                   # per-aspect percentile calibration (§3.2)
             loading_1 … loading_8              # the §5.1 axis loadings
titles       tmdb_id, kind, year, runtime_min, episode_count, …
tags         tmdb_id, aspect_id, level, coverage, salience, evidence
             # sparse: only aspects the title has; salience derived per §3.2
```

Notes on the contract:

- **`df` and the calibration quantiles ship in the file.** IDF must stay calibrated to the corpus it was measured on, and the percentile mapping must be the identical transform for corpus and incremental titles — the app applies both, it never recomputes them from its own 600 titles.
- **Axis loadings ship in the vocabulary file.** They are assigned and reviewed in the corpus project (§5.1); the app treats them as data.
- The files carry a **vocabulary version**; every tag row and the app's imported state reference it.

Import is a one-off admin step (`POST /admin/import`, or a wizard page): validate, upsert, mark matching Jellyfin titles `owned`, build edges, done. Re-running an import with a new vocabulary version replaces the aspect layer wholesale — which is exactly the honest semantics, since taste weights keyed to renamed clusters would be fiction anyway; the migration report lists which aspects survived by ID so `θ_u` can be carried over where it legitimately can.

In-app vocabulary editing is deliberately **display-level only** (rename a label, hide an aspect). Structural changes — merging clusters, retyping, resizing — happen in the corpus project and arrive as a re-import. Two sources of truth for the vocabulary is how the graph rots.

### 3.4 Measured signals

Computed directly from the local media files. These become aspect edges with `salience` derived from percentile rank across the library, and they are hallucination-proof.

| Signal | Method | Feeds aspect |
|---|---|---|
| Average shot length | GPU shot-boundary detection (histogram/feature diff) | `fast cutting` / `long takes`, pacing axis |
| Colour palette & saturation | Frame sampling, k-means in Lab | `desaturated`, `high-contrast colour` |
| Average luminance | Frame sampling | `dark cinematography` |
| Loudness range, LUFS | ffmpeg `ebur128` | `dynamic sound mix`, `quiet film` |
| Dialogue density | Subtitle track: words per minute of runtime | `dialogue-driven`, `sparse dialogue` |
| Vocabulary complexity | Subtitle track: type-token ratio, rare-word rate | `dense script` |
| Profanity / intensity markers | Subtitle keyword lists | `harsh language` |
| Music-to-dialogue ratio | Rough source separation or subtitle gaps vs. audio energy | `score-forward` |

Where a measured signal contradicts an LLM tag, the measured one wins and the LLM tag is logged for review.

**Cost tiers.** The signals split cleanly by what they have to decode:

- **CPU-cheap (audio + subtitles):** loudness/LUFS, dialogue density, vocabulary complexity, profanity markers, music-to-dialogue ratio. Audio decode and subtitle parsing are trivial even on a modest VM — the whole library is an overnight job. These run wherever the container runs.
- **Decode-bound (visual):** shot length, palette, luminance. These require video decode, and on 4K HEVC sources without hardware decode that is the entire cost — ~1,200 hours of content decoded in software is a multi-week background crawl, not an overnight job. These are GPU work in practice.

`ENABLE_SIGNALS` therefore splits into `ENABLE_AUDIO_SIGNALS` (default **on** — there is no reason not to) and `ENABLE_VISUAL_SIGNALS` (default **off**). The visual worker is a detachable batch job keyed by file hash: it can run on any machine that has the media mounted and a GPU — a workstation, a temporary passthrough — and `POST` its results to the API. The appliance never needs the GPU itself.

Measured signals exist only for `owned` titles. `catalog` titles carry LLM tags only, and their aspect vectors are marked lower-confidence for it.

#### 3.4.1 Container metadata

A second class of hallucination-proof signal costs nothing at all: Jellyfin's `MediaStreams` already reports resolution, HDR format (HDR10 / HDR10+ / Dolby Vision), audio codec and channel layout (Atmos, DTS:X, lossless), and bitrate — no `MEDIA_PATH` mount, no GPU, available from the very first sync.

These map to aspects of a dedicated type, **`presentation`** (`4K HDR`, `Dolby Vision`, `object audio mix`, `lossless audio`, `reference-grade AV`). In a dedicated theater, *"push the hardware tonight"* is a real and recurring mood that no narrative axis captures.

`presentation` aspects are deliberately **excluded from taste**: they sit outside the per-title L1 aspect pool and receive no `θ` credit. Rating a film says nothing about liking Atmos, HDR correlates with recency rather than preference, and under L1 normalisation they would silently steal credit from the aspects that actually carry taste. They exist for three uses only: the **showcase night** session boost (§5.3), hard filtering, and an explorer lens.

### 3.5 Beyond the local library

Title nodes carry a `source` flag:

| Source | Meaning | Has media file | Has measured signals |
|---|---|---|---|
| `owned` | in the Jellyfin library | yes | yes |
| `catalog` | known to the graph, not owned | no | no |
| `wanted` | catalog title the user has flagged to acquire | no | no |

`wanted` is household-global in v1 — one acquisition list, not one per user; the per-user view is just the per-user ranking of it.

The extraction corpus (§3.3) already produces catalog titles as a by-product, so this costs almost nothing extra. What it buys:

**1. A denser, better-calibrated graph.** IDF, aspect co-occurrence and person edges are all computed over the full corpus, which makes the owned library's weights more meaningful than they could ever be at N=600.

**2. Ratings for films not in the library.** Users have seen thousands of films they don't own. Rating mode (§4.3) can therefore draw on catalog titles, which is the difference between a profile built from 600 candidate films and one built from 8,000. This is the single biggest lever on cold-start quality.

**3. Acquisition mode.** A separate scoring pass, run on demand or weekly:

```
acquire_score(t) = T(t) · maturity_factor − owned_similarity_penalty(t)
```

restricted to `source = catalog`, `seen = false`, ranked per user or by the household's geometric-mean taste score. `owned_similarity_penalty` suppresses titles whose nearest owned neighbours are already numerous — the point is to fill gaps, not to deepen a well-covered corner. Output is a "next 20 to get" list, exportable and flaggable as `wanted`.

**Exclusion from tonight's pick.** `catalog` and `wanted` titles are hard-excluded from Part B candidate generation and from comparison pairs — recommending something that cannot be played tonight is worse than useless. They participate in taste profiling and the explorer only. When a `wanted` title later appears in the Jellyfin library, ingest promotes it to `owned` and it enters the pool automatically.

---

## 4. Part A — Personal taste profiling

### 4.1 Rating capture

Triggered when a title is marked `seen`, when Jellyfin reports ≥90% playback and the user confirms, or when an external playback event arrives (§6.1 *External playback events* — covers players that bypass Jellyfin, delivered as a push notification). The prompt appears once; dismissing it is free and it is not re-asked.

**Absolute ratings are rejected as the input mechanism.** A six-tier strip is a 1–6 scale wearing letters, and people are bad at absolute scales: centres drift over time, granularity is illusory ("was that an A or a B?" has no stable answer), and two ratings made a year apart are not comparable. What people *are* good at is exactly two judgements: a coarse verdict ("did I like it?") and a relative one ("which of these two did I like more?"). Part A is built from those two primitives and nothing else. The S–F tiers survive — but as **derived display buckets** computed from the comparison model (§4.1.2), never as something a human is asked to emit.

**Step 1 — Verdict. One tap. Required.**

```
  Liked     Fine     Disliked
```

Three options, because three is the granularity at which human absolute judgement is actually reliable, and a plain tap keeps sweep throughput honest — anything continuous reintroduces a "how far?" deliberation on every card. The verdict anchors valence — a pure comparison model can rank 300 films perfectly and still not know whether the user enjoyed *any* of them. (A continuous slider variant was considered and rejected: fine strength distinctions belong to the duels, which measure them better than any absolute gesture can.)

**Step 2 — Placement. 2–3 taps. Optional, encouraged.**

> *Which did you like more?*
> [ poster: this title ] [ poster: a placed title ] · `Can't say`

Two or three A-or-B duels against titles the model has already placed, chosen by binary search within the verdict band — each answer roughly halves the region where this title can sit. Skippable; a skipped placement just leaves the title coarsely placed until battle mode (§4.3.1) refines it. The wording is deliberately past-tense and quality-shaped (*"which did you like more"*), in contrast to Part B's *"which sounds better right now"* — one measures the permanent, the other the ephemeral, and the phrasing is what keeps them from contaminating each other.

**Step 3 — Appetite. One tap. Optional, defaulted.**

> *Would you put this on again?*
> `Anytime` · `Only in the right mood` · `Never again`

Values: `+1`, `0`, `−1`. Default if skipped: derived from the verdict (`Liked → +1`, `Fine → 0`, `Disliked → −1`) — with one exception: **a high-demand title never gets an inferred `+1`.** If the title sits in the library's top Load-axis quartile (equivalently: high `demand`/`pacing` aspect mass), `Liked` infers `0` instead. A loved *Requiem for a Dream* is precisely the film whose rewatch appetite must not be guessed from its quality. Stored with `appetite_explicit = false` so an inferred value can be given lower confidence; an explicit answer always overrides the inference.

This is the single most valuable extra bit in the whole system, and it costs one tap. It is what separates *good* from *watchable tonight*.

**Step 4 — Attribution chips. Optional, zero or more taps, skippable.**

Show 6 chips = the title's 6 highest-weight aspects, phrased in plain language:

> *What stood out?*
> `slow burn` · `bleak` · `gorgeous cinematography` · `grief` · `nonlinear` · `dense script`

Taps are read as *"this is why"*, and multiply that aspect's credit (§4.2). A long-press flips a chip to negative (*"this is why not"*), which is how the system learns dislikes precisely. Skipping is normal and expected; chips are a bonus channel, not a requirement.

**Total interaction cost: one tap minimum, up to six when the user feels like it.**

Nothing else is asked. No sliders, no sub-scores, no text.

**Chip frequency is adaptive.** Chips are shown on every rating initially. The client tracks a rolling tap rate over the last 20 ratings; when it falls below 20%, chips drop to a random 30% of ratings, and below 10% they drop to 15%. If the rate recovers, frequency climbs back. The user never sees this happen, and the aspects chosen for the reduced sampling are biased toward nodes where the user's weight is still uncertain — so the surviving chips carry more information than the ones that were dropped.

#### 4.1.1 Rating edit

Ratings are editable forever from the title page. Changing the verdict re-anchors the title's band prior and replays the taste update with the previous contribution reversed (contributions are stored per rating event, §4.2.5). Individual duels are append-only; a verdict change outweighs old duels via the band prior rather than rewriting history.

#### 4.1.2 From comparisons to a quality score

Per user, every seen title carries a latent quality `q_u(t)` with uncertainty `σ_q` — a Bradley–Terry model over that user's duels, TrueSkill-style:

```
prior:    q ~ N(band(verdict), 0.8²)     band: Liked +0.7, Fine 0, Disliked −0.7
duel:     P(A ≻ B) = σ( q(A) − q(B) )
update:   online logistic update on both titles, uncertainty-weighted
tie:      "Can't say" = half-win each — pulls the two titles together slightly
dynamics: σ_q grows with time since the title's last duel
          (τ ≈ +0.05/month, capped at the prior's 0.8)
```

Properties that matter:

- The verdict is a **soft** prior, not a wall. A `Fine` film can out-duel a `Liked` one — that is signal, not error — but it takes consistent evidence to cross bands.
- Intransitivity (A > B > C > A) is absorbed probabilistically instead of being an error state.
- Duels involving a lapsed title (§2) are down-weighted by the same `0.5` factor as its rating credit.
- **Ambient recalibration.** The `dynamics` term is the answer to the range-development problem that kills absolute-rating systems: users only develop their internal scale over time, so early ratings are systematically miscalibrated — and nobody ever goes back to fix old numbers. Here nobody has to. A long-undueled title's `σ_q` drifts back up, matchmaking (§4.3.1) pulls it into fresh battles against the user's *current* frame of reference, and its position corrects as a side effect of play. Recalibration is ambient, not a chore.
- **The verdict prior washes out.** Bayesian updating means the absolute component (the band prior) dominates only while a title has few duels; with each duel the posterior is carried more by comparisons. The one absolute judgement in the system is deliberately temporary scaffolding.
- The taste score fed to §4.2 is the standardised quality: `r = clamp(ẑ_u(t), −1.25, +1.25)`, where `ẑ` is `q` standardised over the user's placed titles. While a user has **fewer than 10 placements**, `r` falls back to bare verdict values (`+0.7 / 0 / −0.7`) — coarse but honest.

A duel moves `q` for *both* titles involved, so their `r` values and taste contributions are replayed with the new values (cheap — contributions are stored per event, §4.2.5). In practice this replay is batched nightly, except for the title just rated.

#### 4.1.3 Derived tiers — the S–F display

The tier badges wanted for the library view are **computed, not asked**: percentile cuts over `q_u` within the user's rated set, verdict-consistent by construction of the bands.

| Tier | Default cut |
|---|---|
| S | top 5% |
| A | next 15% |
| B | next 25% |
| C | next 30% |
| D | next 15% |
| F | bottom 10% |

Cuts are household-configurable. All downstream rules that reference tiers — the `F` hard exclusion (§5.3, §5.4.3), the `D/F` affinity down-weight (§5.2), the post-watch-tier metric (§8) — operate on these **derived** tiers unchanged. The badge on a title's page shows the tier plus its rank neighbourhood (*"A — between Heat and Prisoners"*), which is more honest and more fun than a letter alone. A tier computed from comparisons is stable in a way a tapped tier never was: it cannot drift with the user's mood on rating day, and it re-calibrates automatically as the ranked set grows.

### 4.2 Rating → user weights

The taste profile is a sparse vector `θ_u` over aspect, person, genre and era nodes, plus a bookkeeping vector `n_u` of accumulated credit per node.

#### 4.2.1 The rating signal

`r` comes standardised from the placement model (§4.1.2): `q_u` is fitted per user and standardised over that user's own placed titles, so different rating centres — the drift problem absolute scales suffer from — cannot arise by construction. No running-mean correction is needed; the old `μ_u` centring existed to patch a defect of tier input that no longer exists.

#### 4.2.2 Distribute credit

Base credit per node is the edge weight (already summing to 1 across aspects). Chips modulate it:

```
c(t,n) = w(t,n) × chip_multiplier(n)

chip_multiplier = 3.0   if tapped positive
                = 1.0   if not tapped
                = 0.2   if long-pressed negative   (and the sign of r is flipped for that node)
```

Then renormalise so `Σ_n c(t,n) = 1` over aspect nodes; person, genre and era nodes are normalised separately in their own groups so that a big cast cannot swamp the aspect signal.

Apply the lapsed factor: `c ← 0.5 · c` if the rating is on a title now marked `unseen`.

#### 4.2.3 Update rule

Update on the **residual**, not the raw rating. A rating that the model already predicted correctly carries no information, and residual updates prevent correlated aspects (`space` and `sci-fi`) from double-counting.

```
r̂(t) = Σ_n w(t,n) · θ_u[n]                     # current prediction
e    = r − r̂(t)                                # surprise

for each node n with c(t,n) > 0:
    η_n   = η₀ / (1 + n_u[n] / 8)              # per-node learning rate decay
    θ_u[n] += η_n · c(t,n) · e
    n_u[n] += c(t,n)
```

with `η₀ = 0.6`. Clamp `θ_u[n]` to `[−1.5, +1.5]`.

**Scale note.** `w(t,n)` in the prediction means the **group-normalised** weights, not the raw edge weights: aspects already sum to 1 per title; person, genre and era weights are normalised within their own groups and scaled by fixed group coefficients (aspects `0.70`, persons `0.20`, genre + era `0.10`). Raw role weights would let a handful of person edges (director 1.0 + two leads 0.7 + …) outvote the entire aspect vector, and `r̂` would then drift outside the range of `r`. The same group-normalised `w` is used for `T(t)` in §5.3 — this mirrors the per-group credit normalisation in §4.2.2, so prediction and update live on the same scale.

**Confidence.** A node's weight is only trustworthy once it has accumulated credit. For display and for scoring, use the shrunk weight:

```
θ̃_u[n] = θ_u[n] × n_u[n] / (n_u[n] + k)        # k = 1.5
```

This is what stops one film from making a director your defining preference.

#### 4.2.4 Appetite channel

An identical, parallel vector `φ_u` is updated the same way using the appetite value instead of `r`, with `η₀ = 0.4` and half credit when `appetite_explicit = false`.

`θ_u` answers *"is this good?"*; `φ_u` answers *"do I actually reach for this?"*. Part B uses both.

#### 4.2.5 Bookkeeping and reversibility

Every rating event stores its full contribution vector (`node → Δθ`, `Δn`). Editing or deleting a rating subtracts the stored deltas and replays. This also makes the profile auditable: *"why does the system think I love Deakins?"* → list the rating events that contributed.

#### 4.2.6 Decay

Taste drifts. Nightly job:

```
θ_u[n] *= 0.5 ^ (Δdays / 1095)      # 3-year half-life
n_u[n] *= 0.5 ^ (Δdays / 1095)
```

applied per node based on the timestamp of the last contribution to that node.

### 4.3 Rating mode

Post-watch rating (§4.1) accumulates roughly one rating per week per user. That is far too slow to bootstrap the system — Part B needs a warm profile before it is worth using at all. **Rating mode** is the dedicated bulk-population flow, and it is the most important screen in the product for the first month of its life.

It is not onboarding-only. It stays permanently available as a "rate some films" activity users dip into.

#### 4.3.1 Interaction — sweeps and battles

Rating mode is two interleaved activities, both one-tap-per-card:

**Sweep** — coverage. One title fills the screen: poster, title, year, runtime, one-line synopsis, director. Below it:

```
   Liked     Fine     Disliked        [ Not seen ]        [ Skip ]
```

- **Verdict tap** → records a rating, implicitly sets `seen = true`, advances immediately. One tap, no placement, no appetite step, no chips. Appetite is stored via the verdict-derived default from §4.1 with `appetite_explicit = false`. Three targets instead of six makes each tap *faster and more reliable* than the old tier strip — there is nothing to deliberate.
- **Not seen** → sets `seen = false` explicitly and advances. The escape hatch that makes the whole flow work; it must be as large and as easy to hit as the verdict buttons. Users must never feel they have to invent an opinion.
  - Its second meaning is *"I saw it but don't remember it well enough"* — same state, per §2. The button label reads **"Not seen / don't remember"**.
- **Skip** → no state change, title returns to the queue much later. For "I remember it but I genuinely can't decide."
- **Undo** — a persistent control that reverts the last action (verdict or duel). Mis-taps are frequent at this speed and an un-undoable mis-tap poisons the profile.

**Battle** — precision. Two posters, one question, three targets:

> *Which did you like more?* — `A` · `B` · `Can't say`

**Battle view design.** The posters *are* the buttons — two full-height cards, tap anywhere on one to pick it; no separate "this one" buttons, no confirmation, auto-advance in ~150 ms with the persistent Undo as the safety net. Everything below the cards is organised by **consequence severity**, top to bottom: the strong answer (the posters), then the weak answer (one centred `Can't say` pill), then the corrections. Because battles draw only from already-swept titles, "haven't seen" is a *correction*, not an answer — it lives in a single quiet row at the very bottom, fully outside every pick target: `haven't seen: [title A] [either] [title B]`. A single-title tap replaces only that side of the pair (the opponent stays); `either` marks both unseen and swaps the whole pair. **Nothing tappable may sit inside a card** — a mis-aimed tap at a small control inside a pick target registers as a duel answer, the one mis-tap that silently corrupts data; grouping all corrections in their own zone means any stray tap lands within the same low-consequence class, and everything remains undoable. Two hygiene rules: the cards show **title and year only — never the current derived tier or quality** (showing the model's opinion before the answer is exactly the anchoring effect Cosley 2003 measured), and **matchmaking internals (P(A≻B), σ_q) never appear in the UI** — they're debug output, reachable behind a developer toggle. Keyboard: `←`/`→` pick, `↓` can't say.

Duels are drawn from the user's already-swept titles by the placement model's matchmaking: prefer pairs with `P(A≻B) ≈ 0.5` and high combined `σ_q`, mostly within the same verdict band (a loved-vs-hated duel teaches nothing). A **recalibration quota** reserves ~20% of battles for stale titles — longest since last duel, `σ_q` re-inflated by the dynamics term (§4.1.2) — so early placements keep getting re-tested against the user's current frame of reference instead of fossilising. Each answer feeds §4.1.2. Battles are *faster* than sweeps — a binary preference between two films you remember is a sub-second judgement — and they are the genuinely fun part; expect users to binge them.

**Rhythm.** Default interleave: 10 sweep cards, then 5 battles, repeat — sweeps generate the pool, battles sharpen it. A mode toggle lets a user do either exclusively. Long-press on a verdict opens the full §4.1 flow (placement + appetite + chips) for a title the user feels strongly about.

**Prediction reveal.** Once the profile is `warm`, each verdict tap briefly flashes the model's prediction — *"predicted: you'd like this one"*, or after battles exist, *"predicted: top quarter"* — as a passive toast that never blocks the next card. It costs nothing (`r̂` is already computed for the residual update, §4.2.3), it makes bulk rating a game against the model, and it is a live calibration display: systematically wrong predictions are visible weeks before the offline metrics say so. Strictly **after** the tap, never before — a visible prediction before the answer anchors the judgement and poisons the data. Suppressed while the profile is `cold`.

Throughput target: **under 2 seconds per sweep card and under 1.5 per battle**. Everything else in the design serves those numbers — no page transitions, next card preloaded, keyboard shortcuts (`1/2/3` or `L/F/D` for verdicts, `←/→/↓` for battles, `N` not seen, `space` skip, `⌘Z` undo), and swipe gestures on mobile.

#### 4.3.2 Queue construction

The queue is the intelligence in this screen. Naively showing the library in alphabetical order wastes most of the user's effort on titles they haven't seen.

Ordering score per candidate title:

```
queue_score(t) = P(seen | t) · info_gain(t) · freshness(t)
```

**`P(seen | t)`** — probability the user has seen it. Estimated from:
- Jellyfin playback history (strongest signal, near-certain)
- TMDB popularity and vote count
- release era vs. the user's age bracket
- similarity to titles this user has already marked seen
- the other household members' seen sets (people watch together)

Getting this right is the difference between a useful queue and one where every third card gets a *Not seen*. A **20–30% *Not seen* rate is healthy** — it means the queue is stretching appropriately. Above 50%, the estimator is wrong and the queue should shift toward popularity.

**`info_gain(t)`** — how much a rating for this title would sharpen the profile. Early on this is aspect coverage: greedy set cover over the aspect vocabulary, weighted by aspects the user has no credit on yet. Later it becomes prediction uncertainty: prefer titles where the model's predicted quality has the widest posterior, because a confidently-predicted rating teaches nothing (§4.2.3).

**`freshness(t)`** — decays titles shown and skipped recently.

**Source mix.** The queue draws from both `owned` and `catalog` titles (§3.5), by default 60/40. Catalog titles are what make the profile broad — a user's taste is not confined to what they happen to own — and they cost nothing but a poster. A toggle lets a user restrict to the local library if they find rating unowned films pointless.

**Batching.** Serve the queue in blocks of 25 with a progress indicator and a natural stopping point at the end of each block ("25 done — keep going?"). An infinite queue produces guilt; a finite block produces completion.

#### 4.3.3 Progress and stopping

Show the user what their effort is buying, in terms of the system's own confidence:

```
Profile strength   ████████░░░░░░░░   warm
Vocabulary covered 34%
Ranking sharpness  ██████░░░░░░░░░░   (mean σ_q over placed titles)
Ready for group sessions: yes
```

Thresholds:

| State | Condition | Behaviour |
|---|---|---|
| **cold** | `Σ n_u < 15` | Part B runs mood-only and says so; rating mode is pushed hard in the UI |
| **warm** | `Σ n_u ≥ 15` | Part B fully enabled |
| **mature** | `Σ n_u ≥ 60` | Quick mode (§5.4.2) unlocked; rating mode demoted to a background activity |

Realistically: ~60 sweeps and ~30 battles in the first sitting gets a user to warm in well under ten minutes of tapping, which is the target for first-run — and the battles are the part people won't want to stop.

#### 4.3.4 Household seeding

The first user to complete rating mode makes it easier for everyone after them: their seen set is a strong prior for `P(seen | t)` for other household members. A second user's queue should be noticeably better targeted than the first's.

A separate **couple sweep** mode shows one title to two logged-in devices at once for households that want to rate together on the TV — same interaction, two independent records.

---

## 5. Part B — Movie for tonight

A session is ephemeral. It produces a mood estimate, a shortlist, and nothing that persists except an anonymised log for evaluation.

### 5.1 Mood space

Mood is **not** estimated in the full DNA space — ten answers cannot locate a point there. Instead, eight fixed axes, each defined as a weighted combination of aspects. Loadings `L[a, axis] ∈ [−1, +1]` are assigned once during vocabulary construction and reviewed by hand — a corpus-project deliverable, shipped in the vocabulary file (§3.3.2). The axes are where bipolarity lives in this design: the DNA itself stays unipolar-sparse, and opposed aspects (`slow burn` vs `relentless`) express their opposition through loadings of opposite sign on the same axis.

| # | Axis | − pole | + pole |
|---|---|---|---|
| 1 | Load | light | heavy / demanding |
| 2 | Tension | calm | tense |
| 3 | Pace | slow | fast |
| 4 | Register | visceral | cerebral |
| 5 | Familiarity | comfort / rewatch-like | novel / challenging |
| 6 | Warmth | cold | warm |
| 7 | Reality | grounded | fantastical |
| 8 | Humour | serious | funny |

A title's mood coordinate:

```
M(t)[axis] = Σ_a w(t,a) · L[a, axis]
```

standardised across the **owned** library to zero mean, unit variance per axis — sessions only ever score owned titles, so owned defines the transform; catalog titles get coordinates through the same transform but don't shape it.

The session state is a diagonal Gaussian posterior over the user's desired point `m ∈ R⁸`:

```
m ~ N(μ_s, diag(σ_s²))
```

**Prior.** `μ_s` is initialised from context rather than zero:
- Time of day and weekday (late weeknight → lower Load, lower Pace).
- Available time, if the user gave one.
- The mean mood of what this user rated highly *and* gave `Anytime` appetite (i.e. `φ_u`-weighted) — a weak pull toward their comfort zone.
- `σ_s` initialised to 1.0 on every axis.

### 5.2 Comparison questions

Between 8 and 12 questions. **Early exit is decided on the shortlist, not the posterior:** after each answer, recompute the provisional top-3; if the set has been unchanged for 2 consecutive answers and at least 6 questions have been asked, stop. A σ-based criterion (`max σ_s < x`) is deliberately not used — with roughly one question per probed axis, eight axes cannot all converge inside the budget, and they don't need to: `ρ_j = 1/σ_j²` in the mood-fit term already discounts axes that were never probed. An unprobed axis isn't wrong, it just counts for less; what matters is whether more answers would still change the recommendation.

**Question form.** Two titles, posters, one line each. *"Which sounds better right now?"* Answers: **A**, **B**, **Neither**, **Both fine**.

**Candidate pool.** Only titles the user has marked `seen`. Asking about unseen titles measures poster appeal, not mood. Titles the user marked `unseen` because they forgot them are excluded for the same reason.

**Pair selection — contrast pairs.** An unstructured pair (`Heat` vs `Amélie`) differs on every axis at once and its answer is uninterpretable. Each pair must be a clean probe:

1. Pick the target axis `j = argmax σ_s[j]`.
2. Score all candidate pairs `(A,B)`:

```
contrast(A,B) = |ΔM[j]|  /  (0.5 + Σ_{k≠j} |ΔM[k]|)
balance(A,B)  = 1 − |P(A≻B | current posterior) − 0.5| · 2
score         = contrast × balance × affinity
```

where `affinity` down-weights pairs whose titles sit in the user's derived `D`/`F` tiers (§4.1.3) — asking which of two disliked films you'd prefer is noise.
3. Take the top pair, subject to a no-repeat constraint within the session and a soft penalty on titles used in the last 3 sessions.

Precompute the top ~2,000 candidate pairs offline per axis; per-question selection is then a cheap re-rank.

**Update.** Bradley–Terry with a logistic link over the mood distance:

```
u(t | m) = − Σ_j ρ_j · (M(t)[j] − m[j])²        # mood fit, ρ = axis precision
P(A ≻ B) = σ( κ · [ u(A|m) − u(B|m) + λ·(T(A) − T(B)) ] )
```

where `T(t)` is the taste score (§5.3) and `λ ≈ 0.35` accounts for the fact that people cannot fully separate *"which do I want now"* from *"which is better"*. Fitting with the taste term present is what stops the mood estimate from simply re-learning the taste profile.

Posterior update per answer: online Bayesian logistic regression, diagonal Laplace approximation.

**Calibration of `κ` and `λ`.** Ship with `κ = 2.0`, `λ = 0.35`. Every answer is logged with the full state at question time (`μ_s`, `σ_s`, both titles' mood coordinates and taste scores), which makes refitting a straightforward offline maximum-likelihood problem over the logged pairs. Refit as a scheduled job once ≥200 answers exist for a user, then monthly, with per-user values falling back to the household-wide fit until a user has ≥200 of their own. Guard rails: `κ ∈ [0.8, 5]`, `λ ∈ [0, 0.8]`; a fit outside the range is rejected and the previous value kept. Track predictive log-loss on held-out answers as the acceptance criterion — a refit that does not beat the incumbent is discarded.

- **Neither** → contrast pairs agree on the non-target axes by construction, so *Neither* mostly indicts what the pair **shares**: shift `μ_s` a small step away from the pair's common position on the axes where `|ΔM|` is small, and mildly inflate `σ_s` on the target axis (the probe produced no usable sign there). Shifting "away from the midpoint" *along the target axis* is undefined — the two titles bracket the midpoint on that axis, so there is no direction to move in.
- **Both fine** → no directional update, but reduces `σ_s` on the target axis (the region between them is acceptable).

**Multi-user sessions.** Each participant answers on their own device and maintains their own posterior `m_i`. Disagreement is not noise — it is exactly the information the joint scorer needs. Question strategy, synchronisation and group scoring are specified in §5.4.

### 5.3 Scoring and shortlist

**Candidate generation.** Personalised random walk with restart over the graph, seeded by:
- the user's top-weighted aspect and person nodes (mass ∝ `θ̃_u`),
- the aspects nearest the mood posterior mean (mass ∝ mood fit),
- optionally, a title the user names as an anchor (*"something like Sicario"*).

Take the top 150 `title` nodes by accumulated mass. This is not primarily a performance measure at 600 titles — it exists because **the walk paths are the explanations**.

**Re-rank.**

```
score(t) = α · T(t)  +  β · Mfit(t)  +  γ · Nov(t)  −  penalties

T(t)    = Σ_n w(t,n) · θ̃_u[n]                       # taste
Mfit(t) = − Σ_j ρ_j · (M(t)[j] − μ_s[j])² / Σ_j ρ_j  # mood fit
          ρ_j = 1/σ_s[j]²  — uncertain axes count less
Nov(t)  = 1 if unseen, 0.35 if seen with appetite +1, −0.4 if seen with appetite 0,
          −1.5 if seen with appetite −1   # "never again" is honoured — effectively
                                          # excluded until the rating is edited
```

Defaults: `α = 0.45`, `β = 0.45`, `γ = 0.10`. `α:β` is exposed as a single UI slider — *"surprise me ⟷ safe bet"* — because the right balance is a mood in itself.

Penalties:
- watched in the last 90 days: `−0.8` (unless appetite `+1` and >30 days)
- runtime exceeds the stated time budget: `−1.5` (hard, effectively excluded)
- shown and rejected in a previous session this week: `−0.3`
- rated `F`: excluded entirely

One optional bonus: if the host enabled **showcase night** in the lobby (§5.4.1), titles with high `presentation` mass (§3.4.1) receive `+0.3`. The theater-hardware itch is a legitimate session mood; it just must never leak into the permanent taste profile.

For sessions with more than one participant, `score(t)` is computed independently per user and combined by the group rule in §5.4.4. It is never averaged at the parameter level.

**Output (single user).** Three titles, not a list of thirty. For each:
- poster, title, year, runtime
- one-line reason built from the top 2 contributing aspects and, if a person node contributed >15%, that person
  → *"Slow-burn procedural, cold and precise — and you rate Deakins highly."*
- a mood-fit indicator: which axes matched, which are a stretch
  → *"Heavier than you asked for."*
- **Not tonight** button, which applies the session penalty and slides the next candidate in.

A fourth slot, **wildcard**, is always shown: the highest-scoring title with the *lowest* taste score among decent mood fits. This is the anti-filter-bubble valve and it should be labelled as such.

### 5.4 Multi-user sessions

The group case is the primary case, not an extension — the original problem is two people failing to agree. This section covers 2 to 5 participants; 5 is a hard cap (§5.4.7).

Three rules govern the whole design:

1. **Never merge posteriors.** Combine at the *score* level, not the parameter level. Averaging two mood vectors produces a mood nobody has; averaging two score vectors produces the film nobody wants. Each user keeps their own `m_i` and their own `score_i(t)` to the very end.
2. **Question budget is per-user, not per-group.** Adding a third person must not make the session 50% longer. Total per-user questions stay in the 8–12 band regardless of group size.
3. **Nobody waits for anybody.** Users answer at their own pace. There is exactly one synchronisation barrier in the session.

#### 5.4.1 Lifecycle

```
CREATED → LOBBY → CALIBRATION → [barrier] → PERSONAL → RECONCILE → VOTE → RESOLVED
```

**Lobby.** The host opens a session; the app proposes participants from presence (Home Assistant `person.*` entities that are `home`) and the household roster, and others join by push notification or a short room code on the TV screen. Lobby shows avatars, profile maturity, and a ready toggle. Host sets the shared context once: time budget, movie / series / either, and optionally **showcase night** (§3.4.1).

**Participant roles:**

| Role | Has profile | Answers questions | Counts in scoring |
|---|---|---|---|
| `active` | yes | yes | full weight |
| `passive` | yes | no — joined but isn't answering | taste term only, mood prior wide (`σ = 1.0`) |
| `guest` | no | shared questions only | mood term only; taste replaced by the household mean profile |
| `observer` | — | no | not scored (someone watching along who doesn't get a vote) |

`passive` matters in practice: it covers the person who is in the room but on their phone. They still get a say through their taste profile, which is exactly the outcome that avoids resentment.

**Dropout and stalls.** If a participant stops answering for 90 s, the session proceeds with their partial posterior (wider `σ`, so their mood counts for less but their taste still counts fully). Leaving the session mid-way demotes to `passive` rather than removing them.

#### 5.4.2 Question strategy

Three phases. Shared questions establish *where the group differs*; personal questions establish *where each individual sits*. Doing only one or the other is wasteful in opposite ways: all-shared spends N×Q answers learning N posteriors inefficiently, all-personal never measures divergence and lands the group in a false consensus.

**Phase 1 — Calibration (4 shared pairs, everyone answers the same pairs).**

- Pairs are chosen for **maximum spread across all eight axes**, not to probe a single one: greedy selection of 4 pairs whose `ΔM` vectors are as close to mutually orthogonal as possible, covering the axes with the highest population variance.
- Candidate pool: the **intersection of the `active` participants' `seen` sets** — `guest` and `passive` roles have no usable seen set and must not constrain the pool (a single guest would otherwise empty the intersection). Guests answer the same pairs, with **"Haven't seen"** as a free skip. If that intersection has fewer than 40 titles, fall back to titles seen by ≥⅔ of participants and let the rest answer **"Haven't seen"** — which is a free skip that does not consume their question budget.
- These four answers give, per axis, both a rough position per user *and* a divergence estimate for the group.
- **This phase ends at the one barrier in the session.** The UI shows a live "3 of 4 ready" indicator; the wait is short because it is only four questions.

**Phase 2 — Personal (4–6 pairs, each user gets their own).**

- Standard single-user pair selection (§5.2), each user probing their own highest-variance axis.
- Candidate pool is that user's own `seen` set, so no intersection constraint.
- Runs fully in parallel; fast answerers simply finish early and land in a waiting screen with the live agreement view (§5.4.6).
- Per-user early exit by the same shortlist-stability rule as §5.2, evaluated on that user's own provisional top-3.

**Phase 3 — Reconciliation (0–3 shared pairs, conditional).**

Triggered only for axes where the group genuinely conflicts after phase 2:

```
divergence(j) = max_i μ_i[j] − min_i μ_i[j]
conflict if divergence(j) > 1.2 and both extremes have σ_i[j] < 0.6
```

(i.e. the users are far apart *and* confident — a wide gap between two uncertain estimates is not worth resolving with questions.)

For each conflicted axis, ask a **concession probe** rather than another preference pair. The wording is deliberately neutral and does not mention the other participants — framing it as *you versus them* invites strategic answers and social friction:

> *How set are you on something slower tonight?*
> `Not very — faster is fine` · `Fairly` · `Very — really not tonight`

This is deliberately not an A/B comparison. Once a conflict is identified, what the scorer needs is not a sharper position estimate but an **elasticity** estimate — how much this user is willing to bend on this axis. The answer maps to a per-user, per-axis tolerance:

```
tolerance_i[j] = 1.6 (yes) | 1.0 (prefer not) | 0.35 (really not)
```

which scales that axis's precision in that user's mood-fit term. `Really not tonight` on an axis is a soft veto: it makes the mismatched region effectively unreachable for that user, and therefore for the group.

**Budget summary per user:** 4 shared + 4–6 personal + 0–3 concession = **8–13**, independent of group size.

**Quick mode.** If every participant has a mature profile and the group has run a session in the last 4 hours, offer a zero-question path: reuse the last session's posteriors with inflated `σ` and go straight to the shortlist. One tap to fall through to the full flow if the results look wrong.

#### 5.4.3 Per-user candidate scoring

Compute `score_i(t)` for every participant exactly as in §5.3, with two group modifications:

- **Tolerance-scaled precision.** `ρ_ij = tolerance_i[j] / σ_i[j]²`, so concession answers directly widen or tighten how much each axis matters for that person.
- **Normalisation.** Raw scores are not comparable across users (different profile maturity, different rating variance). Convert each user's scores over the candidate set to a **percentile within that user's own candidate distribution**, then map to satisfaction `s_i(t) ∈ [0,1]`. This is what makes the group rule fair between a heavy rater and a light one.

Hard exclusions, applied per user and unioned:

- rated `F` by any participant
- a `really not tonight` axis mismatch beyond 2σ for any participant
- above the shared time budget
- (optional per-user, set in profile) permanent "never show me" tags — e.g. body horror

#### 5.4.4 Group scoring rule

Weighted Nash — the geometric mean of satisfactions:

```
joint(t) = ( Π_i  (s_i(t) + ε)^{w_i} ) ^ ( 1 / Σ_i w_i )        ε = 0.05
```

Why this rather than the alternatives:

| Rule | Behaviour | Verdict |
|---|---|---|
| Mean | Reliably surfaces the film nobody hates and nobody wants | Rejected — this is the failure mode being solved |
| Min (maximin) | Fair, but one constrained participant flattens the whole group to their comfort zone, and it is insensitive to how happy everyone else is | Rejected as the primary rule |
| Nash product / geometric mean | Any near-zero satisfaction collapses the product, so it protects the least happy; but above that floor it still rewards total enthusiasm | **Chosen** |

The geometric mean is the standard bargaining solution and it behaves correctly at both ends: it will not hand a 0.9/0.1 split to the group, and among options where everyone is above ~0.5 it picks the one with the most collective enthusiasm.

**Participant weights `w_i` — the fairness ledger.** Households remember who compromised last time. After each session, log each participant's realised satisfaction `s_i(chosen)` and maintain a running deficit:

```
deficit_i ← 0.7 · deficit_i + (s̄_session − s_i(chosen))
w_i = 1 + clamp(0.5 · deficit_i, −0.35, +0.6)
```

Someone who took the hit last Friday carries a slightly heavier weight this Friday. The decay factor stops the ledger from becoming a permanent grievance. This is cheap to implement, invisible when the group is balanced, and it is the single feature most likely to make people trust the system over the long run.

**Ledger scope: `active` participants only.** A `passive` participant's existing `w_i` still applies in scoring — their taste counts — but their deficit is **frozen** for that session, and their `s_i` is excluded from the `s̄_session` used to update everyone else. Compromise credit must be earned by having actually expressed a preference and not gotten it; a taste-only satisfaction estimate is too noisy to bank, and someone scrolling their phone through three sessions must not arrive at the fourth with inflated voting power. `guest` and `observer` roles have no ledger at all.

**Visibility: hidden by default.** The deficit value and the weights are not shown in the session UI — a visible score turns a quiet fairness mechanism into a leaderboard and invites gaming. What *is* available is an on-demand explanation attached to any finalist: *"weighted slightly toward Anna — the last two picks landed closer to yours."* A household-settings toggle can reveal the raw ledger for anyone who wants it, and it can be reset to zero there.

#### 5.4.5 Output and the final vote

Present **three finalists**, each annotated per participant:

```
  Sicario                                    2h 01m
  Cold, precise, relentless procedural
  ● Patrick   strong match
  ● Anna      good — a little tenser than she asked for
  ● Jonas     ok — slower than his pick would be
```

Per-participant annotation is derived from `s_i` bucketed into four labels, plus the single worst-matching axis named in plain language. Showing *where* each person is compromising is what makes the group accept the result; a bare ranked list reads as arbitrary.

Also shown:

- **Agreement summary** — the group's axis-by-axis consensus: *"You all want something light. You're split on pace."*
- **Consensus pick badge** on any title where every `s_i > 0.6`.
- **Wildcard** slot, as in the single-user case, scored by the same group rule but drawn from low-taste / good-mood-fit candidates.

**Blind approval vote.** The final step, and the one that resolves the room:

1. All three finalists are shown on every device simultaneously.
2. Each participant taps thumbs-up / thumbs-down on each — approval voting, multiple approvals allowed.
3. Votes are hidden until everyone has submitted, then revealed together.
4. The title with the most approvals wins; ties break by `joint(t)`. If nothing gets unanimous approval, the highest-approval title wins and is labelled as such.
5. If *nothing* clears half the room, the session offers **one more round** with three fresh finalists (the rejected ones take a session penalty), or a fall-through to the wildcard.

Blind and simultaneous matters. Sequential or visible voting turns into deference to whoever votes first or whoever is loudest, which is the social problem underneath the original technical one.

**Trailers.** The TV finalist screen offers a trailer per finalist (TMDB video links → YouTube embed), played on the shared screen on request before votes are cast — this is exactly where the person who half-remembers a title needs help deciding honestly. Trailers are kept **off** the phone question flow and off rating mode's happy path, where they would destroy the throughput budget; in rating mode a long-press on the poster offers the trailer for genuine *don't-remember* cases without slowing anyone else down.

**Outcome logging.** Record the finalists, the votes, the chosen title, and each `s_i` — this feeds the fairness ledger and is the only reliable data for tuning the group rule.

#### 5.4.6 Live session UI

- **TV screen** (the shared surface): lobby, progress dots per participant, the agreement summary as it forms, the finalists, and the vote reveal. Never shows a participant's individual answers while questions are in flight.
- **Phones**: questions only, one at a time, large tap targets, no scrolling.
- **Waiting screens are not dead time**: a user who finishes early sees the group's forming agreement view, which is genuinely interesting and stops them from putting the phone down.

#### 5.4.7 Degenerate cases

| Case | Handling |
|---|---|
| One participant has an empty profile | Treated as `guest`: mood term only, household mean for taste |
| `seen` intersection is tiny (new housemate) | Phase 1 falls back to ≥⅔ coverage with "Haven't seen" skips; if still under 15 pairs, skip Phase 1 and rely on personal phases plus a wider group prior |
| Group of 5 | Cap Phase 1 at 3 pairs, raise the Nash `ε` to 0.08 (with more participants the product gets brittle), present 4 finalists instead of 3 |
| More than 5 join the lobby | Hard cap: the 6th person is admitted as `observer`. Beyond 5 the Nash rule is dominated by whoever is least satisfied and the mood questions stop earning their time — a party needs a different mode, not this one |
| Everyone agrees on everything | Phase 3 skipped, session ends after ~8 questions, finalists are all consensus-badged |
| Irreconcilable — no title clears `joint > 0.35` | Say so honestly rather than serving a bad pick: offer the best split-the-difference title, the best title for the person with the highest deficit, and a "watch something short instead" option filtered to <100 min |

### 5.5 Comfort shelf

Quick mode (§5.4.2) needs a recent session; the tired-Tuesday case usually doesn't have one. The **comfort shelf** is a standing, zero-question surface on the home screen — the appetite channel finally paying rent on its own:

```
comfort(t) = Σ_n w(t,n) · φ̃_u[n]      # appetite-weighted, shrunk like θ̃
```

ranked over `owned` titles with recorded appetite `+1` or unseen titles whose `comfort(t)` is high, filtered to runtime < 130 min, top 8 shown as a poster row. Titles in the top Load-axis quartile require an **explicit** `+1` to appear — inferred appetite is not enough to put a devastating masterpiece on the comfort row. Per-user by default; a household toggle switches it to the geometric mean over selected members.

No questions, no session, no mood estimate — this is deliberately the anti-Part-B: *"the thing I always want"* rather than *"the right thing for tonight"*. It shares the 90-day rewatch penalty from §5.3 (with the same appetite-`+1` exemption) so it rotates instead of showing the same three films forever. One tap on a poster opens the title page; the system still never presses play.

---

## 6. Data model

```sql
-- graph
node(id, type, name, canonical_name, definition, aspect_type, metadata jsonb)
edge(src, dst, type, weight, salience, idf, evidence text)

-- titles
title(node_id, source, jellyfin_id, tmdb_id, kind, year, runtime_min,
      episode_count, season, path, poster_url, added_at)
      -- source: owned | catalog | wanted
      -- kind:   movie | series
      -- season: NULL in v1; reserved for per-season nodes (§3.3.0)
title_signal(title_id, signal, value, percentile)     -- measured signals §3.4, owned only
title_mood(title_id, axis, value)                     -- precomputed M(t)

-- vocabulary
aspect_loading(aspect_id, axis, loading)

-- user state
user_title(user_id, title_id, seen bool, seen_at, lapsed bool)
rating(id, user_id, title_id, verdict, appetite, appetite_explicit,
       chips jsonb, created_at, superseded_by)
taste_duel(id, user_id, title_a, title_b, outcome, created_at)  -- A|B|TIE, append-only
user_title_quality(user_id, title_id, q, sigma_q, updated_at)   -- §4.1.2; derived tier
                                                                --  computed on read
rating_contribution(rating_id, node_id, delta_theta, delta_n, channel)
user_weight(user_id, node_id, channel, theta, n, last_update)

-- sessions
session(id, host_user_id, state, started_at, ended_at, context jsonb)
session_participant(session_id, user_id, role, weight, joined_at,
                    mu jsonb, sigma jsonb, tolerance jsonb, phase, answered_count)
session_answer(session_id, user_id, seq, phase, title_a, title_b,
               answer, axis_probed, latency_ms)
session_concession(session_id, user_id, axis, answer, tolerance)
session_result(session_id, title_id, rank, joint_score,
               per_user_satisfaction jsonb, reason jsonb)
session_vote(session_id, user_id, title_id, approve bool, submitted_at)
session_outcome(session_id, chosen_title_id, unanimous bool, rounds)
fairness_ledger(user_id, deficit, last_session_id, updated_at)
```

Storage: plain PostgreSQL — `pgvector` is no longer needed in the app, since phrase embedding and clustering live in the corpus project (§3.3.2) and the `similar_to` cosines are trivial in NumPy. At this scale (several thousand title nodes once the catalog corpus is in, and a few hundred thousand edges) the graph fits comfortably in process memory; walks run in NumPy/SciPy sparse and take milliseconds. A dedicated graph database is not warranted and adds an operational component for nothing.

### 6.1 Deployment

**One standalone Docker container** holding frontend, backend and database. It connects out to an existing Jellyfin instance over HTTP using an API key. Nothing is installed into Jellyfin.

A Jellyfin *plugin* is explicitly rejected: plugins are confined to Jellyfin's .NET runtime and UI shell, and this system needs a Python/LLM pipeline, ffmpeg workers, a WebGL frontend and its own database. A thin companion plugin may be added later purely to surface the tier badge and a "rate this" prompt inside Jellyfin's own UI, but it is not on the critical path.

#### Container layout

```
mediagraph:latest
├── s6-overlay (or supervisord) — process supervision
├── postgres 16                  → /data/postgres
├── backend  (FastAPI + uvicorn, :8000)
├── worker   (arq/celery: ingest, tagging, signals, layout)
├── frontend (built SPA, served static by the backend)
└── caddy/nginx (:8080)         → SPA + /api reverse proxy + WebSocket upgrade
```

One image, one port, one volume. `docker run -p 8080:8080 -v mediagraph:/data`.

Single-container is the right call here despite the usual "one process per container" orthodoxy: this is a household appliance with one operator, and a compose file with four services is a worse experience than a container that just runs. A `docker-compose.yml` is shipped alongside for anyone who wants Postgres split out — the backend takes `DATABASE_URL`, so an external database is a config change, not a code change.

#### Configuration

All via environment variables, all with sane defaults:

| Variable | Purpose |
|---|---|
| `JELLYFIN_URL` | e.g. `http://jellyfin:8096` |
| `JELLYFIN_API_KEY` | API key created in Jellyfin → Dashboard → API Keys |
| `JELLYFIN_LIBRARY_IDS` | optional; restrict to specific libraries |
| `TMDB_API_KEY` | metadata, posters, catalog corpus |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` | **incremental tagging only** — one structured call per new title against the imported vocabulary (§3.3.2); hosted API, provider-agnostic |
| `DATABASE_URL` | defaults to the bundled Postgres |
| `MEDIA_PATH` | optional read-only mount of the library for measured signals (§3.4) |
| `ENABLE_AUDIO_SIGNALS` | default `true` — audio/subtitle signals, CPU-cheap |
| `ENABLE_VISUAL_SIGNALS` | default `false` — decode-bound visual signals; GPU work in practice |
| `TZ`, `SESSION_SECRET`, `PUBLIC_URL` | standard |

First run opens a setup wizard: paste the Jellyfin URL and API key → test connection → pick libraries → import → create household users. No config file editing.

#### Volumes and resources

| Path | Contents |
|---|---|
| `/data/postgres` | database |
| `/data/cache` | posters, embeddings, graph layout |
| `/media` (optional, `ro`) | library mount, only for §3.4 signals |

**Reference host: a Proxmox VM without a GPU.** Everything on the critical path is comfortable there: the graph, walks, posterior updates and layout are milliseconds-to-seconds of NumPy/SciPy, and the app's only LLM work — one incremental tagging call per new title — is API-bound, not compute-bound. The heavy one-off work (corpus extraction, embedding, clustering) lives in the external corpus project (§3.3.2) and never touches this host. Suggested sizing: 4 vCPU, 4–6 GB RAM, 20 GB disk plus poster cache. Baseline footprint at idle: ~1 GB RAM, negligible CPU.

The one thing a GPU-less VM cannot do sensibly is the **visual** signal tier (§3.4): software-decoding the library's 4K HEVC is weeks of CPU, so `ENABLE_VISUAL_SIGNALS` stays off and those signals are either deferred or produced by the detachable worker on a GPU machine with the media mounted (`--gpus all`, NVDEC), posting results back to the API. Audio and subtitle signals run on the VM as a matter of course. None of this is load-bearing — measured signals sharpen the graph, they don't carry it.

#### Jellyfin integration

Read-only, polling, over the API key:

| Endpoint | Use |
|---|---|
| `GET /Items` | library inventory: titles, IDs, runtimes, genres, people, provider IDs |
| `GET /Items/{id}/Images` | posters (cached locally) |
| `GET /Users` | map Jellyfin users to household users at setup |
| `GET /Users/{id}/Items?IsPlayed=true` | playback history → the `P(seen)` prior and the post-watch rating trigger |
| `GET /Sessions` | optional: detect a title finishing, to fire the rating prompt live |

Sync runs every 15 minutes and on webhook if Jellyfin's webhook plugin is present. **Nothing is ever written back to Jellyfin** in v1 — the API key can be read-only, and Jellyfin's own data stays untouched. User identity is covered in the next subsection.

#### Users, identity and Jellyfin linking

Household users are **linked to Jellyfin users, but not delegated to them**. The two concerns are kept separate on purpose:

**Identity linking — yes.** At setup, `GET /Users` lists the Jellyfin accounts and the wizard maps each household user to one (optional, one-to-one). The link is what makes per-user data flow correctly:

- that user's `IsPlayed` history feeds *their* `P(seen)` prior, not the household's,
- a playback session attributable to a Jellyfin user arms the rating prompt for the right person,
- a `wanted` title appearing in the library is announced to whoever flagged it.

Users without a Jellyfin account (a guest, a child) exist locally with no link and simply have no playback signal. The link is an ID mapping and nothing more — this system never creates, modifies or deletes Jellyfin users, consistent with the read-only stance above.

**Authentication delegation — no.** Logging in with Jellyfin credentials (`AuthenticateByName`) is rejected for v1:

- It couples login availability to Jellyfin being reachable, which contradicts the failure behaviour above — the app must work fully from cache when Jellyfin is down.
- It cannot cover local-only users, so a second auth path would be needed anyway.
- The threat model is a household LAN, not the internet. The cost of real credential handling buys nothing here.

Instead, the appliance model: the login screen is an **avatar picker**, the device remembers the last user, and a profile can opt into a **4-digit PIN** (protects ratings and the profile view from siblings, nothing more). Session cookies are long-lived per device. If the app is ever exposed beyond the LAN, that is a reverse-proxy/auth-gateway concern (Authelia, Tailscale, …) in front of the container — not something this system reimplements.

Failure behaviour: if Jellyfin is unreachable, the system runs entirely on its cached inventory. Rating mode, profiles and sessions all continue to work; only new-title detection and playback triggers pause.

#### External playback events

Jellyfin's `IsPlayed` only sees plays that went through Jellyfin. In a setup where the main screen plays through a dedicated player reading the library directly (e.g. a media player importing from the NAS), the most attentive watching produces no playback signal at all — the rating prompt would never fire for exactly the room it matters most in.

So playback detection is an **open ingestion point**, not a Jellyfin exclusive:

```
POST /events/playback    {title | jellyfin_id | tmdb_id | path,
                          user_id?, finished: bool, source: string}
```

authenticated with a token from the admin UI. Matching is by provider ID first, then file path, then fuzzy title+year. A `finished: true` event does exactly what Jellyfin's ≥90% signal does: feeds the `P(seen)` prior and arms the rating prompt — delivered as a push notification if the user isn't in the app, e.g. when the theater powers down. Home automation (Home Assistant, a player integration, a script) is the expected caller; the system itself stays read-only toward everything.

Events with no `user_id` arm the prompt for all household members whose presence the caller asserts, or fall back to a "who watched this?" chooser on next app open.

#### Internal components

| Component | Runs in | Responsibility |
|---|---|---|
| `ingest` | worker | Jellyfin + TMDB sync, catalog corpus import, new title detection |
| `signals` | worker (optional) | ffmpeg / GPU shot detection, subtitle parsing |
| `tagger` | worker | corpus/vocabulary import, incremental tagging of new titles against the fixed vocabulary |
| `graph` | backend | build, weight, cache adjacency, walks, layout |
| `profile` | backend | rating ingest, weight updates, nightly decay |
| `session` | backend | pair selection, posterior updates, group scoring, WebSocket state |
| `api` | backend | REST/JSON + WS |
| `web` | static | SPA: rating mode, sessions, explorer |

#### Frontend surfaces

One **responsive web app** — a single SPA and a single codebase, adapting by viewport and input method. No native apps, no separate builds.

**Mobile phones are priority 1.** Every core loop — rating mode, session questions, voting, the comfort shelf — is designed at phone width first and must be fully usable one-handed: single-column layouts, tap targets ≥ 48 px, swipe gestures in rating mode, no hover-dependent UI anywhere. Larger viewports are progressive enhancement of the same screens, never separate implementations:

| Viewport | Adaptation |
|---|---|
| **Phone** (default) | Single column, bottom navigation, swipe + tap. The reference design. |
| **Tablet** | Same layouts with more air: rating mode gains a larger poster and the keyboard shortcuts if a keyboard is attached; the explorer becomes genuinely usable. |
| **Desktop** | Multi-column where it earns it (title page, profile view), keyboard shortcuts throughout, plus the surfaces that only make sense here: admin/setup and the vocabulary review tool. |
| **TV / big screen** | Not a breakpoint but a **mode**: a kiosk-style route (`/tv`, joinable by room code) showing the shared session view — lobby, progress, agreement summary, finalists, trailers, vote reveal — and the 3D explorer. Read-mostly, rendered large, driven by the phones. |

The app ships as an installable **PWA**: manifest, home-screen icon, and a service worker that caches the shell and posters — so on a phone it opens like an app and rating mode works instantly. No offline mutation queue in v1; the container is on the same LAN and the complexity isn't warranted.

The throughput budget in §4.3.1 (< 2 s per title) is a *phone* number and is the bar the responsive design is tested against — a rating mode that only hits it with a keyboard has failed the requirement.

### 6.2 API sketch

```
POST /titles/{id}/seen            {seen: bool}
POST /titles/{id}/rating          {verdict, appetite?, chips?}
POST /titles/{id}/duel            {opponent_id, outcome: A|B|TIE}
DELETE /titles/{id}/rating
GET  /profile                     → top nodes by θ̃, confidence, coverage, state
GET  /profile/why/{node_id}       → contributing rating events

GET  /rate/queue                  {block_size, source_mix?} → 25 titles + chips
POST /rate/answer                 {title_id, action: VERDICT|NOT_SEEN|SKIP, verdict?}
GET  /rate/battle                 → {title_a, title_b}   (placement matchmaking)
POST /rate/battle                 {winner: A|B|TIE}
POST /rate/undo
GET  /rate/progress               → strength bar, coverage %, not-seen rate
POST /rate/couple                 start a couple sweep (§4.3.4); both devices
                                  consume one shared queue, answers recorded per user

GET  /acquire                     {user_id? | household} → ranked catalog titles
POST /titles/{id}/want            {wanted: bool}

GET  /comfort                     {user_id | household} → comfort shelf (§5.5)
POST /events/playback             external playback event (§6.1), token-authed

GET  /admin/jellyfin/test         → connection check
POST /admin/sync                  force a Jellyfin resync
POST /admin/import                load corpus-project files (§3.3.2): vocabulary,
                                  titles, tags; returns a migration report
GET  /admin/vocabulary            → aspect list with df, version
POST /admin/vocabulary/{id}       → display-level only: rename label, hide
                                    (structural changes arrive via re-import)

POST /session                     {time_budget?, kind?, anchor_title?} → {id, room_code}
POST /session/{id}/join           {user_id | guest_name, role}
POST /session/{id}/ready          {ready: bool}
POST /session/{id}/start          host only
GET  /session/{id}/question       → {title_a, title_b, phase, index, total}
                                    | {type: CONCESSION, axis, prompt}
                                    | {type: WAIT, waiting_on: [user]}
POST /session/{id}/answer         {answer: A|B|NEITHER|BOTH|UNSEEN|YES|PREFER_NOT|REALLY_NOT}
GET  /session/{id}/agreement      → per-axis consensus + divergence
GET  /session/{id}/result         → finalists + per-user satisfaction + wildcard
POST /session/{id}/vote           {votes: [{title_id, approve}]}
GET  /session/{id}/outcome        → chosen title, vote reveal
POST /session/{id}/reround        discard finalists, generate a fresh set

WS   /session/{id}/live           lobby state, progress dots, phase changes,
                                  barrier release, vote reveal

GET  /graph/layout                → cached 3D coordinates
GET  /graph/neighbourhood/{id}    {depth, types[], min_weight}
GET  /graph/path                  {from, to, max_hops}
```

---

## 7. 3D explorer

A WebGL view (three.js) of the same graph.

**Layout is precomputed and frozen.** Node2vec embeddings over the weighted graph → UMAP to 3D → coordinates cached in the database. Recomputed only when the vocabulary changes, and then anchored to the previous layout via Procrustes alignment so the map stays recognisable. Live force-directed layout is explicitly rejected: a graph that rearranges itself on every visit can never be learned.

Features:

- **Lenses** — recolour by aspect type, decade, seen state, tier, or distance from the current mood.
- **You are here** — the taste profile and the session mood projected into the same space as coloured volumes.
- **Paths** — *"show me the route from Blade Runner to Arrival"* — the shortest weighted path traversing intermediate aspect nodes, animated. Both a discovery mechanism and the clearest possible explanation of the graph's structure.
- **Walk visualisation** — replay a recommendation as mass spreading from seed nodes.
- **Filtering by weight** — a global edge-weight threshold slider, since the graph is only legible when sparse.

Node budget on screen: ≤3,000. Beyond that, aggregate aspect nodes into their type clusters and expand on zoom.

---

## 8. Evaluation

Without measurement this is unfalsifiable. Minimum instrumentation:

| Metric | Definition | Target |
|---|---|---|
| Pick-through rate | Sessions where a recommended title was watched | >60% |
| Post-watch quality | Mean derived tier / quality percentile of titles picked via the system vs. picked manually | ≥ manual |
| Session length | Questions until early exit | ≤10 |
| Rating capture rate | Watched titles that got rated | >70% |
| Profile coverage | Fraction of vocabulary with `n_u > 0.5` | >40% at maturity |
| Unanimity rate | Group sessions where a finalist got every approval | >50% |
| Re-round rate | Group sessions needing a second set of finalists | <15% |
| Satisfaction spread | `max s_i − min s_i` on the chosen title | <0.3 |
| Ledger drift | `max deficit_i` across the household over 20 sessions | <0.4 |
| Diversity drift | Rolling mean pairwise DNA distance of watched titles, 90-day window | no sustained narrowing |

Offline: hold out 20% of duels, measure how often predicted `T(t)` ordering agrees with the held-out duel outcomes (pairwise accuracy), plus rank correlation between `T(t)` and `q_u`. This validates Part A independently of Part B.

Also worth logging: how often the **wildcard** slot is chosen. If it is never chosen, the scorer is well-calibrated and boring; if it is often chosen, `α` is too high.

---

## 9. Build order

| Milestone | Contents | Value at completion |
|---|---|---|
| **M0** | Container skeleton (Postgres + backend + SPA + proxy), setup wizard, Jellyfin API-key sync, TMDB enrichment, node/edge schema, seen state | `docker run` works; library mirrored and browsable |
| **M1** | Corpus/vocabulary **import** (§3.3.2): file validation, upsert, edge build, migration report; incremental tagger for new titles; read-only vocabulary browser | The graph exists and is inspectable |
| **M2** | **Rating mode** (queue, sweep verdicts, battles + placement model, not-seen, undo, progress, prediction reveal) + post-watch rating, external playback events (§6.1), profile update, decay | Part A complete; the database can be populated and the derived tier ranking is useful on its own |
| **M3** | Mood axes, loadings, pair selection, posterior, scorer, shortlist, comfort shelf (§5.5) | Part B single-user — the system is useful |
| **M3b** | Multi-user sessions: lobby, phased questions, group rule, blind vote, fairness ledger, finalist trailers | The actual problem is solved |
| **M4** | Catalog corpus in rating mode + acquisition mode | Profiles get broad; "what to get next" |
| **M5** | Measured signals: audio + subtitle tier on the VM; visual tier via the detachable GPU worker (§3.4) | Tags get sharper, pacing axis becomes real |
| **M6** | 3D explorer | Exploration and explanation |

M2 is the milestone that gates everything in the app; M1 is now mostly plumbing, because the hard part — building a good vocabulary and tagged corpus — has moved to the **external corpus project**, which runs in parallel and is the true critical path. M2 delivers standalone value — the tier ranking that was wanted anyway — before any recommendation machinery has to work, and it is the only way to get enough ratings for M3 to be worth switching on. Expect to sit in M2 for a couple of weeks of real use before M3 produces good picks. The app can be developed against a small hand-made sample of the import files long before the real corpus is ready.

---

## 10. Decisions and remaining risks

### 10.1 Decided

| Question | Decision |
|---|---|
| Vocabulary size | Not fixed up front. Extract a fixed **30 aspects per title**, cluster, then size the vocabulary from a discriminativeness/knee analysis (§3.3 pass 3). Near-duplicate clusters are dropped rather than kept. Expected landing zone ~400–600. |
| Extraction corpus | Larger than the local library — TMDB top 5–10k plus everything owned, so IDF and cluster structure are meaningful (§3.3). |
| TV granularity | **Per show.** Nullable `season` column reserved for later (§3.3.0). |
| `κ` / `λ` | Ship defaults, log full state per answer, refit offline at ≥200 answers with log-loss acceptance and guard rails (§5.2). |
| Chip fatigue | Adaptive frequency driven by rolling tap rate, biased toward uncertain nodes (§4.1). |
| Beyond the local library | Yes — `source` flag, catalog titles in rating mode, acquisition mode, hard-excluded from tonight's pick (§3.5). |
| Fairness ledger visibility | Hidden by default; on-demand per-finalist explanation; revealable in household settings (§5.4.4). |
| Concession wording | Neutral, no reference to the other participants (§5.4.2). |
| Group size | **Max 5** active participants; a 6th joins as `observer` (§5.4.7). |
| Deployment | Single Docker container: frontend + backend + Postgres, connecting to Jellyfin by API key, read-only (§6.1). |
| User accounts | Local household users, **linked** to Jellyfin user IDs (playback attribution, per-user `P(seen)`) but **not authenticated** through Jellyfin. Avatar-picker login, optional per-profile PIN; external exposure is a reverse-proxy concern (§6.1). |
| UI | One responsive web app, **mobile-first**; tablet and desktop are progressive enhancements of the same screens, TV is a kiosk mode. Installable PWA (§6.1 *Frontend surfaces*). |
| Host | **Proxmox VM without a GPU.** Audio/subtitle signals run on-VM (default on); visual signals default off, produced later by a detachable GPU worker if at all (§3.4, §6.1). |
| LLM | **Hosted API only**, no local model. In-app LLM work is reduced to incremental tagging; corpus passes happen in the external project. |
| Corpus scope split | **Vocabulary construction and initial corpus tagging are a separate one-off project**, coordinated independently; the app imports its two deliverables as flat files and ships only the incremental tagger (§3.3.2). |
| Title DNA | **Fully sparse** interpretable embedding (§3.2): unipolar aspects, absence = 0. Salience is **derived, never LLM-scored**: 3-level ordinal × measured source coverage, percentile-calibrated per aspect across the corpus (params shipped in the vocabulary file). Bipolarity lives in the mood-axis loadings, not the DNA. |
| Rating model | **No absolute rating input.** 3-way verdict (Liked/Fine/Disliked, plain tap — slider variant considered and rejected) + adaptive pairwise duels → per-user Bradley–Terry quality score (§4.1.2). The S–F tiers are **derived display buckets** from percentile cuts (§4.1.3), never tapped by a human. |

### 10.2 Prior art worth keeping at hand

The design's core bets are not novel — most were validated on MovieLens/GroupLens over two decades, which is reassuring:

- Vig, Sen & Riedl, *The Tag Genome* (TiiS 2012) — derived, never-asked-for tag relevance scores; the closest relative of the DNA and the corpus project's validation target (pass 4).
- Rashid et al., *Getting to Know You* (IUI 2002) — popularity×informativeness item selection for new users; validates the §4.3.2 queue.
- Cosley et al., *Is Seeing Believing?* (CHI 2003) — displayed predictions bias entered ratings; the empirical basis for the strictly-after-the-tap prediction reveal.
- Sparling & Sen, *Rating: How Difficult Is It?* (RecSys 2011) — coarse scales are faster and barely lossier; supports the 3-way verdict.
- Nguyen et al., *Rating Support Interfaces* (RecSys 2013) — anchor-based rating improves consistency; supports duel-based placement.
- O'Connor et al., *PolyLens* (ECSCW 2001) + Masthoff's group-aggregation work — least-misery fits small groups; family evidence for the Nash-with-veto group rule.
- Nguyen et al., *Exploring the Filter Bubble* (WWW 2014) — content narrowing is measurable and real; the reason the diversity-drift metric (§8) exists.
- McNee, Riedl & Konstan, *Being Accurate Is Not Enough* (CHI 2006) — the case for the novelty term and explanation cards over raw accuracy.

### 10.3 Remaining risks

1. **Vocabulary quality is the single point of failure — and it now lives in the corpus project, not the app.** If the aspect set is mushy, everything downstream — taste weights, mood axes, explanations — is mushy too, and no amount of in-app tuning recovers it. Budget real time for the manual review there, be willing to redo the clustering pass, and treat the import contract (§3.3.2) as the quality gate: the app should refuse files that fail validation, not paper over them.
2. **`P(seen | t)` estimation in rating mode.** If it's poor, users spend their patience tapping *Not seen* and never reach a warm profile. Instrument the not-seen rate from day one and treat >50% as a bug, not a fact of life.
3. **Mood axis loadings are LLM-assigned and unvalidated.** An axis whose loadings are wrong produces confidently wrong mood estimates. Sanity-check by ranking the library on each axis and reading the extremes — if the "heaviest" ten films aren't obviously the heaviest ten films, fix the loadings before shipping M3.
4. **Multi-user sessions have no dry-run path.** The group rule can only be validated with a real household in a real room. Ship M3b early to the actual users rather than tuning it in the abstract.
5. **Vocabulary revisions now cross a project boundary.** Re-tagging after a vocabulary change happens in the corpus project and arrives as a re-import — cheap in compute, but it resets the aspect layer, and taste credit only survives for aspects whose IDs persist. Version the vocabulary from day one, keep the corpus project's scripts runnable (not a one-time notebook graveyard), and treat a re-import as a planned event with a migration report, not a routine sync.
