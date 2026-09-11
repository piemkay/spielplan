"""The 64-d user fold-in and the per-label-count blend weight. Spec v2.1 §5.1, §5.3, §10, §12.

§5.3's row: "Fold-in user vectors, blend weights per label count — nightly — seconds."
§5.1's arithmetic:

    score_u(t) = b(t) + μ_u + w_cf·⟨v_u, e(t)⟩,  blended with the crowd prior at β
                 (also exactly where per-user top-10s stop being the global chart:
                 12 → 263 distinct titles)

READ AS. The two sentences are one expression. `w_cf` **is** β, the crowd prior carries (1−β),
and both halves are standardised over the same population so β is a genuine convex weight:

    score_u(t) = μ_u + (1−β_u)·(b(t) − prior_mean)/prior_sd + β_u·⟨v_u, e(t)⟩

with `v_u` scaled at fit time so ⟨v_u, e⟩ has unit sd over that population.

WHICH HALF β WEIGHS IS SETTLED, AND IT IS THIS ONE. Decision 167: **β is the weight on the
PERSONAL half**, and it stays there. The 0.8 §5.1 quotes is the CORPUS's number in the corpus's
coordinates — its table is headed `blend beta (1.0 = crowd only)` — so the corpus optimum of 0.8
crowd is **β = 0.2 here**, and whenever that number is cited it must be converted:
**β_app = 1 − β_corpus**. This is not a ranking bug and never was: `_cross_validate` searches the
full grid and picks per (user, kind) by held-out Spearman using the IDENTICAL orientation as
serving, so the fit absorbs the naming entirely. Re-measured through this app's own `fit_user`
over 150 real raters from the corpus's reviews.sqlite, the population held-out curve peaks at
β_app 0.20 (+0.4324, against +0.4082 at 0.80) and the median fitted β_app is exactly 0.20 — the
corpus's optimum, reproduced in the complementary coordinate by a different pipeline. §5.1's
sentence gains that conversion in M4.16's spec pass (decision 177); nothing here flips.

BLEND, NEVER ROUTE. §5.1: "a learned router was measured to capture 2–3% of the oracle gap and
lose to the flat blend." β is ONE scalar per (user, kind), refit per label count — never a
per-title decision. There is no router class in this package, and no per-title branch that
could grow into one.

NO POPULARITY TERM. §4.1 rule 3 bans aggregate platform scores as model features (measured:
−0.010 Spearman for nothing). Nothing here reads the display schema.

WHAT HAPPENS WITH ZERO LABELS, PLAINLY. β_u = 0, v_u = 0, μ_u = 0, so score_u(t) is the
z-scored crowd prior and identical for every unfitted member of that kind. The ranked lists
still answer — honestly, and labelled `personalised: false` with `label_count: 0`. A
`user_vector` row IS still written, because "fitted to zero labels" and "never fitted" are
different states and §6.0's zero-verdict fallback has to tell them apart.

THE CEILING IS A CONSTRAINT, NOT A CONVENTION — AND NOT THE OPTIMUM EITHER. The β grid searches
up to 1.0 and the result is clamped to 0.8; `0009_scoring.sql` enforces the same ceiling with a
CHECK. 0.8 is NOT "§5.1's measured optimum" — that is 0.2 here (decision 167) — it is a floor of
one fifth on the crowd prior: the household never sees a ranking that is more than 80% its own
labels. The corpus-faithful reading would put the ceiling at 0.2 instead, and that was measured
and is worse: clamping there costs 22 of 150 real raters more than §0's noise floor while
helping 30, a net +0.0070 for the shipped ceiling and inside §0's 0.008 tie band. The 19 fits
that reach 0.8 earn it (+0.065 held-out ρ over β 0.2, 12 of the 19 beyond the noise floor). A fit
that wanted more is recorded (`beta_clamped`) and logged, because a silent clamp is a
measurement nobody ever sees.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from spielplan.db.library import KINDS, Kind, household_ids
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.observations import LIVE_LABEL_SQL
from spielplan.scoring import serve
from spielplan.scoring.backbone import EMBED_DIM, Backbone, Coordinate, pack_vec

log = logging.getLogger("spielplan.scoring.foldin")

# EVERY CONSTANT IN THIS BLOCK IS A `Hyperparams` FIELD NOW, and the values are unchanged. §5.2
# says "every constant comes from `ledger_hyperparams.json`" and `ledger/hyperparams.py` states
# the rule it enforces: "this is the only module in the package allowed to contain a tuning
# number". Six of them sat here as literals, so the corpus project could not re-tune the fold-in
# at all - the grids below are cross-validated per user, but WHICH grid, the noise floor they are
# read against and the label counts that decide whether to search at all were this file's private
# opinion. They are defaults there now; the names and the comments stay here, because the reason
# a number is what it is belongs next to the code that spends it. [M4.13 step 34d, dd14]
#
# What that bought is ONE home and a range check, not yet a delivery path: every name below binds
# `DEFAULTS.<field>` at import, no function in this module takes an `hp`, and §10's restart
# re-evaluates the same dataclass defaults. A bundle that re-tunes `blend_beta_max` or either grid
# is parsed, validated, digested and then not used here; `hyperparams._PARSED_NOT_THREADED` says
# so in the import report rather than leaving the knob looking applied.
# [M4.13 cycle 2, M413-C2-DIM-HP-01]
#
# The floor on the crowd prior, not the optimum: β is the personal weight (decision 167), so
# clamping it at 0.8 is the statement that the crowd keeps at least a fifth of every blend.
# §5.1's own optimum, converted into these coordinates, is 0.2 — see the header.
BETA_MAX = DEFAULTS.blend_beta_max
BETA_GRID: tuple[float, ...] = DEFAULTS.blend_beta_grid

# §5.1's ceiling is storable as itself. It was not always: 0009's CHECK compared a `real`
# column against the numeric literal 0.8, which Postgres resolves through float8 where
# float4(0.8) is 0.800000011920929 — so `SELECT 0.8::real <= 0.8` was FALSE and a fit clamped
# to the ceiling failed its INSERT inside a nightly job. The migration now casts the literal,
# and the write below stores β unmodified. (0009's own comment still calls 0.8 the measured
# optimum; it is applied and sha256-checksummed, so decision 167 rules that correction into the
# amended §5.1 and a comment on the next migration that touches `user_vector` — never an edit.)

# Not shipped. §4.3's `ledger_hyperparams.json` carries the LEDGER's anchor λ (3.0), which is a
# different quantity in a different objective, so borrowing it would be a coincidence dressed as
# a constant. This grid is cross-validated per user instead; if the corpus tuner ever prints a
# fold-in λ, this loop becomes a read.
LAMBDA_GRID: tuple[float, ...] = DEFAULTS.foldin_lambda_grid

# §0: "pipeline variance 0.003–0.008 Spearman; anything smaller is a tie." A tie must not buy
# personalisation, so an improvement inside the noise floor leaves β at 0.
NOISE_FLOOR = DEFAULTS.rho_noise_floor

# §0/§6.1: "personal signal roughly triples from 5 to 100 labels" — below five, a fitted β is
# noise wearing a number.
MIN_LABELS_FOR_CV = DEFAULTS.min_labels_for_cv
LOO_BELOW = DEFAULTS.loo_below_labels   # leave-one-out under this many, 5 folds at or above

# THE DEBOUNCE, IN SECONDS. §5.3 gives the fold-in a nightly cadence and §12's M2 exit criterion
# asks for visibly personal rankings "after a sitting" — so the tick's job is to answer once the
# sitting is over, not to repaint the partition while it is still going on. Measured at 14k
# titles, the old trigger (any label newer than the fit) rewrote all 14,000 rows on every 60 s
# tick for as long as someone kept rating: 5-8 MB of WAL a minute for a table nobody was reading
# between taps. `PAUSE_SECONDS` is what "after" means here, and `HARD_CAP_SECONDS` is the promise
# to the person who never stops: at worst one rewrite every five minutes, so <= 12 per rater-hour
# rather than 60, and still inside the sitting.
#
# Frequency is the only lever there is. The write SHAPE was measured and is already the best of
# the three: `ON CONFLICT DO UPDATE` produced 14,000 non-HOT updates, 11.2 MB of WAL and 459 ms
# against DELETE+INSERT's 7.7 MB and 264 ms (`0009_scoring.sql:40` indexes `score`, so HOT is
# impossible), and an epsilon filter is moot because 13,965 of 14,000 scores move after one more
# verdict. [M4.13, perf-04; plan step 22]
PAUSE_SECONDS = 30
HARD_CAP_SECONDS = 300

# §4.2: verdict value 0 disliked / 1 ok / 2 liked. The regression target is the raw verdict, not
# the Ledger's fitted `s`: anchoring on `s` would make the nightly pass order-dependent and, at
# first fit, circular. Revisit at M3 against §13's held-out stream rather than by assertion.
VERDICT_TO_Y: dict[int, float] = {0: -1.0, 1: 0.0, 2: 1.0}


@dataclass(frozen=True, eq=False)
class Fit:
    """One (user, kind) fold-in. Everything `score()` needs, and everything §6.7 would narrate."""

    v: np.ndarray            # (64,) float64, already divided by cf_sd
    mu: float
    beta: float
    lam: float
    cv_rho: float
    cf_sd: float             # the PRE-normalisation sd of ⟨v, e⟩; 0 means "no signal"
    prior_mean: float
    prior_sd: float
    # Every live verdict this person has given for this kind — the number §6.0's copy and §5.3's
    # staleness check both mean by "label count". `used` is how many of them the fit could see.
    label_count: int
    used: int = 0
    dropped: int = 0         # labels on titles with no coordinate, counted rather than ignored
    beta_clamped: bool = False
    folds: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mu": self.mu, "beta": self.beta, "lambda": self.lam, "cv_rho": self.cv_rho,
            "cf_sd": self.cf_sd, "prior_mean": self.prior_mean, "prior_sd": self.prior_sd,
            "label_count": self.label_count, "used": self.used, "dropped": self.dropped,
            "beta_clamped": self.beta_clamped, "folds": self.folds,
        }


@dataclass
class FoldInReport:
    refit: list[tuple[int, str]] = field(default_factory=list)
    skipped: int = 0
    scores_written: int = 0
    clamped: list[tuple[int, str]] = field(default_factory=list)
    priors: serve.PriorReport | None = None
    ms: float = 0.0
    # One number was hiding two costs that differ by three orders of magnitude, and the job
    # registry inherited the confusion: the fit is a closed-form 64-d ridge solve, 6-7 ms for 100
    # labels, while `serve.replace_scores` rewrites the whole (user, kind) partition — 14,000
    # DELETEs and 14,000 INSERTs, 325-590 ms and 5-8 MB of WAL per stale pair measured at corpus
    # scale, 0.7-1.5 s for two raters in one tick. A tick reporting one `ms` cannot be read
    # against a budget, and `worker.JOBS` called the whole job "ms" because of it. Split here and
    # named there. [M4.13, perf-04-foldin-tick-rewrites-the-whole-user-score-partition-every-minute;
    # plan step 22]
    numpy_ms: float = 0.0
    db_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "refit": [[u, k] for u, k in self.refit],
            "skipped": self.skipped,
            "scores_written": self.scores_written,
            "clamped": [[u, k] for u, k in self.clamped],
            "priors": self.priors.as_dict() if self.priors else None,
            "ms": round(self.ms, 1),
            "numpy_ms": round(self.numpy_ms, 1),
            "db_ms": round(self.db_ms, 1),
        }


# --- the arithmetic (numpy only) ---------------------------------------------------------------


def fold_in(x: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """Ridge normal equations: (XᵀX + λI)⁻¹ Xᵀy. §10 calls the fold-in "closed-form, ms".

    float64 throughout. A float32 solve at λ = 1 with correlated columns loses digits, and two
    people reading the same refit report must see the same number.
    """
    xd = np.asarray(x, dtype=np.float64)
    gram = xd.T @ xd + float(lam) * np.eye(EMBED_DIM)
    return np.linalg.solve(gram, xd.T @ np.asarray(y, dtype=np.float64))


def _ranks(a: np.ndarray) -> np.ndarray:
    """Tie-averaged ranks. Ties are not rare here: the target has three levels."""
    a = np.asarray(a, dtype=np.float64)
    order = np.argsort(a, kind="mergesort")
    ranked = np.empty(a.size, dtype=np.float64)
    sorted_a = a[order]
    i = 0
    while i < a.size:
        j = i
        while j + 1 < a.size and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranked[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranked


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """§13's yardstick is "per-user held-out Spearman", so the selection criterion is the same
    statistic the milestone is judged by. A constant vector correlates with nothing: 0.0."""
    ra, rb = _ranks(a), _ranks(b)
    if ra.size < 2 or ra.std() < 1e-12 or rb.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _fold_assignment(n: int, seed: int) -> np.ndarray:
    """Leave-one-out below 25 labels, 5 folds at or above. Seeded, so a refit reproduces."""
    if n < LOO_BELOW:
        return np.arange(n)
    folds = np.arange(n) % 5
    np.random.default_rng(seed).shuffle(folds)
    return folds


def _reference_arrays(reference: Sequence[Coordinate]) -> tuple[np.ndarray, np.ndarray]:
    if not reference:
        return np.zeros((0, EMBED_DIM)), np.zeros(0)
    return (
        np.ascontiguousarray([c.e for c in reference], dtype=np.float64),
        np.asarray([c.b for c in reference], dtype=np.float64),
    )


def fit_user(
    labels: Sequence[tuple[int, int]],
    coords: Mapping[int, Coordinate],
    reference: Sequence[Coordinate],
    *,
    seed: int = 0,
) -> Fit:
    """§5.1's fold-in and blend weight for one (user, kind).

    `reference` is the population both halves are standardised over — the titles that are ever
    ranked for this kind. Standardising over a FIXED population rather than over the current
    candidate set is what keeps the number on the title card independent of the filter the user
    happens to have typed.
    """
    ref_e, ref_b = _reference_arrays(reference)
    prior_mean = float(ref_b.mean()) if ref_b.size else 0.0
    # The same guard the mean one line up already carries, and it is not symmetry for its own
    # sake: `np.std` over an empty array is NaN, and the `< 1e-9` test below does NOT catch that
    # — NaN < 1e-9 is False — so the NaN was stored in `user_vector.prior_sd` and divided into
    # every score of the kind. A kind with no coordinated titles is an ordinary state here, not a
    # corrupt one (a fresh household, a bundle-less one, §3.1's empty artifact store), and it
    # announced itself as three numpy "Degrees of freedom <= 0" warnings per user per tick in the
    # worker log. An empty population standardises nothing: the scale is 1 and the crowd half is
    # the raw prior. [M4.13, dd16-empty-reference-writes-nan-prior-sd; plan step 19]
    prior_sd = float(ref_b.std()) if ref_b.size else 1.0
    if prior_sd < 1e-9:                      # one title, or a flat crowd: the prior orders nothing
        prior_sd = 1.0

    # Sorted by title_id, so the fit is a function of the label SET and not of the order the
    # rows happened to arrive in: the cross-validation folds are assigned by position, and an
    # order-dependent held-out ρ would make a refit report irreproducible for no reason.
    ordered = sorted(labels, key=lambda pair: int(pair[0]))
    rows = [(coords[t].e, VERDICT_TO_Y[int(v)], coords[t].b) for t, v in ordered if t in coords]
    dropped = len(labels) - len(rows)
    n = len(rows)
    if n == 0:
        return Fit(
            v=np.zeros(EMBED_DIM), mu=0.0, beta=0.0, lam=LAMBDA_GRID[-1], cv_rho=0.0, cf_sd=1.0,
            prior_mean=prior_mean, prior_sd=prior_sd, label_count=len(labels), used=0,
            dropped=dropped,
        )

    x = np.ascontiguousarray([r[0] for r in rows], dtype=np.float64)
    y_raw = np.asarray([r[1] for r in rows], dtype=np.float64)
    z_prior = (np.asarray([r[2] for r in rows], dtype=np.float64) - prior_mean) / prior_sd
    mu = float(y_raw.mean())                 # μ_u shifts every score of this kind and reorders none
    y = y_raw - mu

    lam, beta, cv_rho, folds = LAMBDA_GRID[-1], 0.0, 0.0, 0
    if n >= MIN_LABELS_FOR_CV:
        # `ref_e` travels into the search because the search has to standardise the way serving
        # does; see `_cross_validate`. [M4.13, plan step 18]
        lam, beta, cv_rho, folds = _cross_validate(x, y, y_raw, z_prior, ref_e, seed=seed)

    beta_clamped = beta > BETA_MAX
    beta = min(beta, BETA_MAX)

    v = fold_in(x, y, lam)
    cf_sd = float((ref_e @ v).std()) if ref_e.size else 0.0
    if cf_sd < 1e-9:
        # ⟨v, e⟩ is constant over everything that can be ranked, so the personal half orders
        # nothing. Saying so with β = 0 is honest; dividing by it would be a zero-divide dressed
        # up as personalisation.
        return Fit(
            v=np.zeros(EMBED_DIM), mu=mu, beta=0.0, lam=lam, cv_rho=cv_rho, cf_sd=0.0,
            prior_mean=prior_mean, prior_sd=prior_sd, label_count=len(labels), used=n,
            dropped=dropped, folds=folds,
        )

    return Fit(
        v=v / cf_sd, mu=mu, beta=beta, lam=lam, cv_rho=cv_rho, cf_sd=cf_sd,
        prior_mean=prior_mean, prior_sd=prior_sd, label_count=len(labels), used=n,
        dropped=dropped, beta_clamped=beta_clamped, folds=folds,
    )


def _cross_validate(
    x: np.ndarray, y: np.ndarray, y_raw: np.ndarray, z_prior: np.ndarray, ref_e: np.ndarray,
    *, seed: int
) -> tuple[float, float, float, int]:
    """Choose (λ, β) by held-out Spearman against the user's own labels.

    β = 0 does not depend on λ — the prior-only blend never touches the fold-in — so all five
    λ rows must agree there. They are asserted to, because disagreement is a fold bug and a
    fold bug otherwise shows up as a slightly-too-good β.

    STANDARDISED OVER THE REFERENCE, FOLD BY FOLD, BECAUSE SERVING IS. `fit_user` divides the
    full-data `v` by the sd of ⟨v, e⟩ over the whole reference population (`cf_sd`), so the β
    chosen here is the β in effect only if this search's personal half sits on that same scale.
    It did not: the held-out predictions were standardised over the LABELLED rows, and a labelled
    row's norm runs with its crowd support (0.006 to 5.4 on the real basis), so the served
    personal half had a spread of 0.04x to 5.18x what this table assumed. Two things were wrong
    with that and the smaller one is the loud one: the β printed on §6.0's why-line and §6.7's
    rail was not the weight doing the blending. The larger one is that a grid scored on the wrong
    scale selects a different point on itself — measured on `test_foldin_jobs.py`'s own case, the
    labelled-row spelling picks β 0.5 where the serving scale picks 0.4, which is outside §0's
    0.008 tie band. So each fold's own `v_f` takes the serve-time divisor and nothing is
    re-standardised afterwards. [M4.13, dd14-per-user-cv-beta-loses-to-crowd-at-small-n and
    ml03-foldin-beta-chosen-on-one-scale-served-on-another; plan step 18]
    """
    n = x.shape[0]
    fold = _fold_assignment(n, seed)
    n_folds = int(fold.max()) + 1

    table: dict[tuple[float, float], float] = {}
    rho_at_zero: list[float] = []
    for lam in LAMBDA_GRID:
        z_cf = np.zeros(n)
        for f in range(n_folds):
            held = fold == f
            if held.all():
                continue
            v_f = fold_in(x[~held], y[~held], lam)
            # `fit_user`'s own expression for `cf_sd`, including its answer when the reference
            # orders nothing: a fold that cannot be put on the serving scale contributes zeros
            # rather than a NaN, which also keeps β = 0's row identical across λ below.
            sd_f = float((ref_e @ v_f).std()) if ref_e.size else 0.0
            if sd_f >= 1e-9:
                z_cf[held] = x[held] @ v_f / sd_f
        for beta in BETA_GRID:
            table[(lam, beta)] = spearman((1.0 - beta) * z_prior + beta * z_cf, y_raw)
        rho_at_zero.append(table[(lam, 0.0)])

    assert max(rho_at_zero) - min(rho_at_zero) < 1e-9, "β = 0 must not depend on λ"
    rho0 = rho_at_zero[0]
    best_key = max(table, key=lambda k: table[k])
    best = table[best_key]

    if best - rho0 <= NOISE_FLOOR:
        # Inside the measured pipeline variance. A tie is a tie.
        return LAMBDA_GRID[-1], 0.0, rho0, n_folds

    # Among everything within the noise floor of the best, prefer the smallest β (the prior),
    # then the smallest λ at that β. "Within noise of best" is not "best".
    within = [k for k, rho in table.items() if best - rho <= NOISE_FLOOR]
    beta = min(k[1] for k in within)
    lam = min(k[0] for k in within if k[1] == beta)
    return lam, beta, table[(lam, beta)], n_folds


def score(fit: Fit, c: Coordinate) -> tuple[float, float]:
    """(score_u(t), ⟨v_u, e(t)⟩). Both halves are returned so §6.7 can show them separately."""
    cf = float(fit.v @ c.e)
    z_prior = (c.b - fit.prior_mean) / fit.prior_sd
    return fit.mu + (1.0 - fit.beta) * z_prior + fit.beta * cf, cf


def score_many(fit: Fit, coords: Sequence[Coordinate]) -> list[tuple[int, float, float]]:
    """(title_id, score, cf) for a whole reference population — one matvec, ~50 µs at 839 rows."""
    if not coords:
        return []
    e = np.ascontiguousarray([c.e for c in coords], dtype=np.float64)
    b = np.asarray([c.b for c in coords], dtype=np.float64)
    cf = e @ fit.v
    scores = fit.mu + (1.0 - fit.beta) * (b - fit.prior_mean) / fit.prior_sd + fit.beta * cf
    return [(c.title_id, float(s), float(f)) for c, s, f in zip(coords, scores, cf, strict=True)]


# --- the job -----------------------------------------------------------------------------------


async def live_labels(conn, *, user_id: int, kind: Kind) -> list[tuple[int, int]]:
    """The user's live verdicts on titles of this kind.

    `superseded_by IS NULL` because §4.2 makes a re-rating supersede rather than mutate, and
    `NOT is_reask` because §13's re-ask stream is a silent instrument measuring flip rate — it
    is not a second opinion to be averaged in. DISTINCT ON keeps one row per title even if two
    live rows ever coexist, so the fit is a function of the data and not of the row order.
    """
    rows = await conn.fetch(
        """
        WITH label AS ({LIVE_LABEL})
        SELECT l.title_id, l.value
          FROM label l JOIN title t ON t.id = l.title_id
         WHERE t.kind = $2
        """.replace("{LIVE_LABEL}", LIVE_LABEL_SQL),
        user_id, kind,
    )
    return [(int(r["title_id"]), int(r["value"])) for r in rows]


async def write_fit(
    conn, *, user_id: int, kind: Kind, bundle_version: str, fit: Fit, updated_at: datetime
) -> None:
    """One `user_vector` row per (user, kind). Written even for a zero-label fit.

    `updated_at` is the caller's, not `now()`, and the parameter is required because there is
    exactly one honest value for it: the moment the labels this fit saw were read. Stamped with
    `now()` here, the column meant "when the write landed", and `_is_stale` reads it as "the
    labels this fit saw" — two different instants with the whole fit in between. See `refit_user`.
    [M4.13, dd16-rerating-in-refit-window-never-becomes-stale; plan step 21]
    """
    await conn.execute(
        """
        INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count, mu,
                                 prior_mean, prior_sd, cf_sd, foldin_lambda, cv_rho,
                                 bundle_version, updated_at)
        VALUES ($1, $2, 'foldin', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (user_id, kind, purpose) DO UPDATE
           SET vec = EXCLUDED.vec, blend_beta = EXCLUDED.blend_beta,
               label_count = EXCLUDED.label_count, mu = EXCLUDED.mu,
               prior_mean = EXCLUDED.prior_mean, prior_sd = EXCLUDED.prior_sd,
               cf_sd = EXCLUDED.cf_sd, foldin_lambda = EXCLUDED.foldin_lambda,
               cv_rho = EXCLUDED.cv_rho, bundle_version = EXCLUDED.bundle_version,
               updated_at = EXCLUDED.updated_at
        """,
        user_id, kind, pack_vec(fit.v), fit.beta, fit.label_count, fit.mu,
        fit.prior_mean, fit.prior_sd, fit.cf_sd, fit.lam, fit.cv_rho, bundle_version,
        updated_at,
    )


async def refit_user(
    conn, backbone: Backbone, *, user_id: int, kind: Kind, bundle_version: str,
    report: FoldInReport | None = None,
) -> Fit:
    """Refit one (user, kind) and rewrite its `user_score` rows. §5.3, §10's rebuild set.

    `report`, when given, collects the two halves of what this pass costs — see `FoldInReport`.
    Optional because only `run` reports a tick; a caller that just wants the fit is not made to
    carry a timing object to get one.
    """
    entered = time.perf_counter()
    coords = await serve.coordinates(conn, backbone, bundle_version=bundle_version, kind=kind)
    reference = list(coords.values())
    # THE FIT'S CLOCK, READ BEFORE THE LABELS. `user_vector.updated_at` is read by `_is_stale` as
    # "the labels this fit saw", so it has to be an instant no label this fit saw can be newer
    # than. Stamped at write time instead, a re-rating that committed after the read and before
    # the write was invisible for ever: §4.2 makes changing your mind an INSERT and `LIVE_LABEL_SQL`
    # takes the newest row per title, so the label COUNT does not move — and the new verdict's
    # `created_at` sat before the fit's `updated_at`, which is exactly the comparison that decides
    # staleness. Every later tick then reported that person fresh, indefinitely.
    #
    # `clock_timestamp()`, not `now()`: `now()` is the transaction's start, and this function runs
    # inside the importer's transaction on §10's rebuild path, where that is minutes early. The
    # two seconds cover a verdict whose transaction began before this read and committed after it
    # — a window `read committed` makes real and cheap to over-cover, since a false positive costs
    # one bounded refit on the next tick and a false negative costs the fit for ever. Deliberately
    # not a serializable transaction around the whole refit: §5.3's "seconds" pass must not take a
    # conflict-abort risk on the tap path's writes. [M4.13, dd16; plan step 21]
    fitted_at = await conn.fetchval("SELECT clock_timestamp() - interval '2 seconds'")
    labels = await live_labels(conn, user_id=user_id, kind=kind)
    # Seeded from the identity of the fit, so a refit of the same (user, kind, basis) draws the
    # same folds and the §6.7 log line means the same thing twice.
    #
    # NOT `hash()`. `str.__hash__` is salted per interpreter — PYTHONHASHSEED is random by
    # default — so the seed changed on every worker restart, and with it the 5-fold shuffle, the
    # selected ridge λ and the written vector. Two consecutive nightly runs with no new ratings
    # reordered Home overnight, and the `cv_rho` in the refit report was not reproducible. A
    # digest is stable across processes and across machines, which is what "the same fit draws
    # the same folds" has to mean.
    seed = int.from_bytes(
        hashlib.sha256(f"{user_id}|{kind}|{bundle_version}".encode()).digest()[:4], "big"
    )

    numpy_started = time.perf_counter()
    fit = fit_user(labels, coords, reference, seed=seed)
    rows = score_many(fit, reference)
    numpy_ms = (time.perf_counter() - numpy_started) * 1000.0

    if fit.beta_clamped:
        log.warning(
            "user %s/%s: cross-validation wanted β above the ceiling; clamped to %.2f "
            "(the ceiling is a floor on the crowd prior, not §5.1's optimum — decision 167)",
            user_id, kind, BETA_MAX,
        )
    if fit.dropped:
        log.warning(
            "user %s/%s: %d verdicts sit on titles with no coordinate and cannot inform the fit",
            user_id, kind, fit.dropped,
        )

    # ONE TRANSACTION, because the two writes are one fact. `write_fit` alone said "this person is
    # fitted, as of `fitted_at`" and `replace_scores` alone is what every §6.0 shelf reads: when
    # the second failed and the first had committed, `user_vector.updated_at` was newer than every
    # label, so `_is_stale` reported fresh and the ranked sections returned EMPTY until the nightly
    # pass — up to 24 hours, with no error anywhere. Reproduced with `label_count = 3` against zero
    # `user_score` rows. `replace_scores` keeps its own `conn.transaction()`, which asyncpg nests as
    # a savepoint inside this one. [M4.13, ml08-foldin-partial-write-masks-staleness; plan step 20]
    async with conn.transaction():
        await write_fit(
            conn, user_id=user_id, kind=kind, bundle_version=bundle_version, fit=fit,
            updated_at=fitted_at,
        )
        await serve.replace_scores(
            conn, user_id=user_id, kind=kind, bundle_version=bundle_version, rows=rows,
        )
    if report is not None:
        # Everything in this function that is not the two numpy calls above is Postgres: the
        # coordinate read, the clock, the labels, the fit's own row and the partition rewrite.
        # The two log calls between them are microseconds.
        report.numpy_ms += numpy_ms
        report.db_ms += (time.perf_counter() - entered) * 1000.0 - numpy_ms
    return fit


async def run(
    conn,
    backbone: Backbone,
    *,
    bundle_version: str,
    only_stale: bool = True,
    with_priors: bool = False,
) -> FoldInReport:
    """§5.3's nightly pass, and the cheap tick that keeps M2's exit criterion honest.

    §5.3 says the fold-in is nightly. M2's exit criterion is about what a person sees after a
    sitting of 50–100 verdicts, and a strictly nightly job cannot answer the same evening — so
    the nightly pass runs everything (`only_stale=False`, `with_priors=True`) and a short tick
    runs `only_stale=True`, refitting only what moved *and has settled* — see `_is_stale`, which
    holds the rewrite back until the person has paused, because "after a sitting" is what §12 M2
    asks for and a repaint per minute during one is work nobody reads. Running a millisecond job
    more often is a superset of the spec's cadence, not a change to it.

    Ordering inside the nightly pass matters: priors first, refits second. A prior materialised
    before the night's placements would leave freshly placed titles at `e_source = 'none'` and
    therefore out of every ranked list for a day.
    """
    started = time.perf_counter()
    report = FoldInReport()
    if with_priors:
        report.priors = await serve.materialise_priors(conn, backbone, bundle_version=bundle_version)

    # §5.3's other nightly pass fits the same people, through the same helper. This query read
    # `role IN ('admin', 'member')` while `refit.refit_all` read `is_active`, so a deactivated
    # account was skipped by the Ledger and re-folded and re-scored here on every 60 s tick —
    # paying the whole partition rewrite for nobody, and producing `user_score` rows for a person
    # who cannot sign in. [M4.13, ml04-foldin-and-ledger-disagree-about-the-household; step 23]
    for user_id in await household_ids(conn):
        for kind in KINDS:
            if only_stale and not await _is_stale(
                conn, user_id=user_id, kind=kind, bundle_version=bundle_version
            ):
                report.skipped += 1
                continue
            fit = await refit_user(
                conn, backbone, user_id=user_id, kind=kind, bundle_version=bundle_version,
                report=report,
            )
            report.refit.append((user_id, kind))
            report.scores_written += 1
            if fit.beta_clamped:
                report.clamped.append((user_id, kind))
    report.ms = (time.perf_counter() - started) * 1000.0
    return report


async def _is_stale(conn, *, user_id: int, kind: Kind, bundle_version: str) -> bool:
    """Never fitted, fitted against another basis (§10), or a label moved since the last fit.

    A label COUNT alone is not the trigger, and that was a real gap: re-rating is the second
    half of a rating sitting — §4.2 exists so that changing your mind about a title is an
    INSERT — and it leaves the count exactly where it was. A person who reversed every verdict
    they had ever given would have been skipped by every tick, indefinitely, because the number
    of titles they had labelled had not changed.

    So the trigger is the count OR a label newer than the fit. Both clocks here are Postgres's
    (`user_vector.updated_at` and `verdict.created_at` are both `now()`), so this is not the
    cross-clock comparison §7.3's sweep got wrong.

    It is deliberately not a trigger for what a *placement* changes — a title that gained a
    coordinate today moves nobody's labels — so the nightly `only_stale=False` pass is what
    picks those up, and this says so rather than pretending to cover it.

    AND THE TRIGGER IS DEBOUNCED, because §12's sentence is "after a sitting". Something having
    moved is necessary and no longer sufficient: the person must have put the phone down for
    `PAUSE_SECONDS`, or the fit must have gone `HARD_CAP_SECONDS` without being redone. Without
    that, every tick inside a sitting rewrote all 14,000 rows of the partition — see the
    constants for the measurement. Two things the debounce deliberately does not delay: a fit in
    another basis and a person never fitted at all, both of which return above, because §10's
    invariant and §6.0's zero-verdict fallback are correctness and this is only cost.

    Every clock here is Postgres's own — `now()` inside these two queries, `verdict.created_at`
    from `now()`, `user_vector.updated_at` from `refit_user`'s `clock_timestamp()` read — so this
    is not the cross-clock comparison §7.3's sweep got wrong. `now()` is the transaction's start,
    which is the statement's own clock for the tick (it holds no transaction) and would be stale
    for a caller that wrapped this in one — in that direction only, i.e. declining a refit rather
    than repeating one, which is the safe way for a cost guard to be wrong. The FIT's clock is
    `clock_timestamp()` for the opposite reason: being early there loses work.
    [M4.13, perf-04; plan step 22]
    """
    row = await conn.fetchrow(
        "SELECT label_count, bundle_version, updated_at, "
        "       now() - updated_at > ($3::int * interval '1 second') AS past_cap "
        "  FROM user_vector WHERE user_id = $1 AND kind = $2 AND purpose = 'foldin'",
        user_id, kind, HARD_CAP_SECONDS,
    )
    if row is None or row["bundle_version"] != bundle_version:
        return True
    live = await conn.fetchrow(
        """
        WITH label AS ({LIVE_LABEL})
        SELECT count(*) AS n, max(l.created_at) AS newest,
               max(l.created_at) < now() - ($3::int * interval '1 second') AS paused
          FROM label l JOIN title t ON t.id = l.title_id WHERE t.kind = $2
        """.replace("{LIVE_LABEL}", LIVE_LABEL_SQL),
        user_id, kind, PAUSE_SECONDS,
    )
    newest = live["newest"]
    moved = int(live["n"] or 0) != int(row["label_count"] or 0) or (
        newest is not None and newest > row["updated_at"]
    )
    if not moved:
        return False
    # `newest is None` is the count arm with every label gone (decision 174's Undo hard-DELETEs):
    # there is nothing to wait for, so the board is rewritten at once. `paused` is NULL in exactly
    # that case, which is why it is not asked to carry it.
    return newest is None or bool(live["paused"]) or bool(row["past_cap"])
