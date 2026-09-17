# ARCHITECTURE extracts - the fusion math section 5.2 points at

**Status:** vendored extract, not authored here. Two sections of the corpus project's
`ARCHITECTURE.md`, copied verbatim so that a sentence `spielplan-spec_v2.1.md` makes normative can
be read inside this repository. Nothing in this file is edited; where it disagrees with
`spielplan-spec_v2.1.md`, the spec wins and the disagreements are enumerated below.
**Source:** `C:/Users/pmk/Workspace/movie_data_curator/docs/ARCHITECTURE.md`, section 3 (line 94)
and Appendix C (line 276).
**Provenance:** corpus repository HEAD `3666eaa` at the time of vendoring; the file itself was last
written by `2a0df7b` (2026-09-01) and is clean in that working tree; sha256 of the whole source
file `ea5c57a4d7ac8a63abb390731e0210f0955ecdcc7094ad141547944b3dcda7ef` (39,581 bytes). Vendored 2026-09-17 under decision 294.
**Companion to:** `spielplan-spec_v2.1.md` section 5.2, which cites this material by name, and
`media-graph-spec_v1.1.md`, vendored into this repo for the same reason and in this style.

---

## Why this file exists, and what of it is normative

`spielplan-spec_v2.1.md` section 5.2 read "the objective is the four-arm likelihood of
`ARCHITECTURE.md` section 3" over a file `docs/` did not hold. An unvendored pointer into another
repository is a normative sentence nobody in this repository can read - and read, it contradicted
the section citing it. Decision 294 vendors the two sections and makes the split explicit.

**Normative in Spielplan** (section 5.2 says so in place):

- section 3's four-arm likelihood - ordered logit over 3-class verdicts with free per-user
  cutpoints, **minus arm 1's protocol sensitivity `a_r`, which is the one part of it not
  built**: `ledger/model.py`'s layout is `theta = (mu, v[64], gamma[2], cuts[K-1], psi)` and
  `_ordinal_terms` enters the latent with coefficient 1, so a free cutpoint absorbs an
  arm-specific SHIFT and nothing absorbs an arm-specific SCALE. Not superseded, simply
  unbuilt - section 4.3 says so in place, and it is named here rather than three bullets
  down because it sits inside the equation this list promotes. [decision 308; M4.16 cycle 2,
  SPEC-C2-01] Then **the Davidson-with-ties form** over duels (arm 2, the P(i>j)/P(tie) equations at
  section 3 below; `ledger/model.py` ships it in the scale-free parameterisation with nu = exp(psi)),
  the K-level ordered logit whose cutpoints *are* the displayed tier boundaries, and rewatch
  re-ratings as new ordinal observations.
- Appendix C's margin weighting - the tuner chose `margin=True`, and `ledger/model.py`'s
  `_duel_weights` divides each margin by the batch mean - and its **negative** result about sigma:
  precision-weighting rows by 1/sigma^2 HURTS (-0.021), so **sigma is never a sample weight**. The
  positive half of that sentence does not carry over and is not claimed: Appendix C's sigma is the
  rank-Gaussian target's per-level CDF band, and that target is not what shipped (see below), so
  there is no sigma in this app's likelihood to keep there. `ledger/observations.py` writes
  `ord_weight` as ones and says why; sigma here is a Laplace-diagonal OUTPUT, which is what drives
  the straddle badge and the comparison queue. [decision 306; M4.16 cycle 1, SPEC-05]
- Appendix C's comparison fusion: the ridge anchor on the label arm plus the BT perturbation,
  **preconditioned with the ridge Hessian**. The instrument log below records why: fixed-step GD
  diverges on episodes containing one huge-norm popular-title embedding. That is a scar, and the
  preconditioner stays.

**Superseded by section 5.2, and NOT implemented here:**

These are the two clauses section 5.2's own "What this section supersedes" sentence names, and
they are the only two it names (decision 294). The three below them are filed apart because a
heading is what an index reads, and "marked unbuilt" is not "narrowed away" - the distinction
decision 307 spent a whole entry establishing. [M4.16 cycle 4, M416-C4-SPEC-04]

- **section 3's Crowd Head with a per-user low-rank head** (head parameters W0 + sum_r w_ur B_r,
  R = 16 shared basis deltas). The app ships no crowd head and no basis deltas, section 4.3 carries
  an artifact for neither, and generalisation is through the 64-d user vector instead.
- **section 3's random-walk prior on b_i.** The shipped model applies a **static ridge** under
  section 4.3's tau.

**Superseded by decision 166 and section 0's deletion of the mood round, not by section 5.2:**

- **section 3's "Guests"** paragraph in both halves: there are no mood answers (the mood round was
  deleted in v2.1 section 0) and no `w_guest` fit from comparisons - under decision 166 a guest is
  a session seat with no stored profile at all.

**Not superseded by anything - simply unbuilt, and NOT implemented here:**

Nobody has decided against these. They are debts nobody has scheduled, and section 5.2 never
promised either of them, which is why they are recorded here rather than there.

- **Appendix C's rank-Gaussian regression target** - z = Phi^-1(mid-CDF_s(r)) per source, with each
  discrete level's CDF band supplying the sigma. Appendix C's own table marks the ternary target as
  the shipped one, and that is still true: `ledger/model.py` fits an ordered logit over 3-class
  verdicts and nothing in this repository computes a mid-CDF. It is listed here because it is
  Appendix C's whole subject and its absence is what makes the sigma clause above a negative rather
  than a positive one. Not superseded by a better answer - within-liked resolution was the one
  place it beat ternary (+0.0151) - simply unbuilt. Section 5.2's arms table does specify the
  ternary target positively, so the two are not in conflict; what is not true of it is the word
  *superseded*, which claims a decision nobody took.
- **section 3's protocol-reversal guard** - the per-arm scale-and-shift bias term and the
  standing |z| > 3 disagreement diagnostic - is not built. It is not superseded, it is simply
  unbuilt, and it is recorded here rather than in section 5.2 because section 5.2 never promised it.

Appendix C's tables are corpus measurements, quoted as evidence and not as app targets: they
measured **generalisation only** (a 64-d taste vector, no per-title residuals), which is why
Appendix C calls its own numbers floors.

---

## 3. The user side: the Personal Ledger

One latent per (user, title): **s_i^u = μ_u(x_i) + b_i^u**, displayed 0–1 via posterior CDF.

- **μ_u** — the crowd-informed prior: the Crowd Head evaluated with user u's fold-in vector and a **per-user low-rank head**: head parameters = W₀ + Σ_r w_{u,r} B_r with R=16 shared basis deltas trained on crowd users, w_u ∈ R^16 fit per owner (LoRe / PReF reward-factorization pattern; PReF personalizes from ~10 responses). This is the DPO insight transplanted: the crowd model is the reference policy; personal data perturbs it, never replaces it.
- **b_i^u ~ N(0, τ²)** — per-title free residual, shrunk (τ by CV): the owners are allowed to disagree with everything on specific titles, at a price.

**Observation arms (one latent, many protocols — the Perez-Ortiz TIP'19 fusion pattern):**
1. *3-class labels*: ordered logit P(y≤c) = σ(κ_c − a_r·s), free per-user cutpoints κ, protocol sensitivity a_r. Free cutpoints are a monotone link — fact 3's licensed fix, again.
2. *Pairwise comparisons*: Davidson model with ties — P(i≻j) = e^{s_i}/Z, P(tie) = δ·e^{(s_i+s_j)/2}/Z, δ initialized from the measured 22% tie rate (fact 5). "Can't split them" is a first-class UI answer flowing into δ, not the bin (2409.17431: labelled ties add regularization; forcing ties into plain BT hurts).
3. *Tier assignments*: 7-level ordered logit whose cutpoints t₁..t₆ **are** the displayed tier boundaries (§5; tier set F/D/C/B/A/A+/S per DNA_MODEL §4.5's measured banding).
4. *Rewatch re-ratings*: new ordinal observations — drift signal for free.

**Protocol-reversal guard** (from the pairwise-ranking fact-check's strongest objection): a per-arm scale-and-shift bias term, plus a standing diagnostic — per-title standardized disagreement between the rating-arm and comparison-arm expectations; |z|>3 titles surface as "your ratings and duels disagree here" cards. Fifty years of preference-reversal literature says ratings and choices can systematically diverge; we measure it on our own data instead of assuming a monotone link absorbs it.

**Dynamics**: nightly **full-history MAP refit** (WHR — exact batch beats every incremental scheme, and at 839×2 titles plus a few thousand comparisons it is seconds of LBFGS), random-walk prior on b_i, Glicko-style σ inflation for titles untouched >12 months (re-eligible for the comparison queue). Laplace diagonal gives per-title σ for tier badges and queue selection. Per-user centring kills BT shift-ambiguity (VPO).

**Guests**: 3 mood answers are their profile for tonight (§4); if they stick around, w_guest fit from 10–20 comparisons via the shared basis — few-shot by construction.

---

## Appendix C: the regression target and comparison fusion (2026-08-28) - VALIDATED

The owner proposed replacing the 3-class target with a continuous score via a
non-linear mapping plus Gaussian uncertainty, refined by pairwise comparisons
within the liked class.  Tested (`scripts/exp_regression_target.py`,
`scripts/exp_regression_partc2.py`; 2,332 held-out episodes):

**The mapping.**  Rank-Gaussianisation per source: z = Phi^-1(mid-CDF_s(r)),
CDFs from training rows only; per-user offset shrunk (lam=60), for held-out
users computed from their 30 support labels ONLY.  Each discrete level's CDF
band supplies a natural sigma (the requested Gaussian uncertainty, derived).

| target | all-query rho | within-liked rho |
|---|---|---|
| ternary (shipped) | 0.4253 | -0.009 |
| naive linear min-max | 0.4232 | -0.000 |
| rank-Gaussian + centring | 0.4238 | +0.006 |

* Overall: tie with ternary (-0.0015, CI spans 0).  Within-liked: rank-Gaussian
  beats ternary by **+0.0151 [+0.0115, +0.0188]** - resolution among liked
  films, exactly where 3 classes are blind by construction.
* Precision-weighting rows by 1/sigma^2 HURTS (-0.021): keep sigma in the
  likelihood, never as sample weights.

**Comparison fusion** (ridge anchor + margin-weighted Bradley-Terry over the
user's 64-d vector; hyperparameters dev-tuned; preconditioned natural-gradient
steps).  Within-liked resolution, monotone in m, all-query unharmed:

| target | m=0 | m=30 | gain |
|---|---|---|---|
| ternary | 0.0043 | 0.0198 | +0.0155 [+0.0116,+0.0194] |
| rank-Gaussian | 0.0279 | **0.0361** | +0.0082 [+0.0050,+0.0115] |

* The continuous target alone is worth more than 30 comparisons on ternary;
  together they reach ~8x the ternary-only resolution.
* The tuner chose margin=True: decisive picks count more - supports the
  "about the same" button and margin capture in the comparison UI.
* This measured GENERALISATION only (a 64-d taste vector, no per-title
  residuals); deployment adds the direct effect on compared titles, so these
  are floors.

**Instrument log, because it keeps mattering:** four failures preceded the
valid reading - dropout live at eval, a 2-episode filter, an untuned fusion
weight, and fixed-step GD diverging on episodes containing one huge-norm
popular-title embedding (fix: precondition with the ridge Hessian).  And a
silent patch failure reran old code, caught only because results identical to
five decimals across a changed optimiser are impossible.  Chain patch->verify->
run with &&, always.
