"""The 64-d user fold-in and the per-label-count blend weight. Spec v2.1 §5.1, §5.3, §10, §12.

§5.3's row: "Fold-in user vectors, blend weights per label count — nightly — seconds."
§5.1's arithmetic:

    score_u(t) = b(t) + μ_u + w_cf·⟨v_u, e(t)⟩,  blended with the crowd prior at β = 0.8
                 (measured optimum; also exactly where per-user top-10s stop being the global
                 chart: 12 → 263 distinct titles)

READ AS. The two sentences are one expression. `w_cf` **is** β, the crowd prior carries (1−β),
and both halves are standardised over the same population so β is a genuine convex weight:

    score_u(t) = μ_u + (1−β_u)·(b(t) − prior_mean)/prior_sd + β_u·⟨v_u, e(t)⟩

with `v_u` scaled at fit time so ⟨v_u, e⟩ has unit sd over that population. β sits on the
PERSONAL side because the same sentence's evidence — per-user top-10s ceasing to be the global
chart — describes a blend that has become personal, which 0.8 on the crowd prior cannot produce.
The direction is genuinely ambiguous in §5.1; if the corpus project settles it the other way it
is one constant and one string here, not a re-architecture.

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

THE CEILING IS A CONSTRAINT, NOT A CONVENTION. The β grid searches up to 1.0 and the result is
clamped to §5.1's 0.8; `0009_scoring.sql` enforces the same ceiling with a CHECK. A fit that
wanted more is recorded (`beta_clamped`) and logged, because a silent clamp is a measurement
nobody ever sees.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spielplan.db.library import KINDS, Kind
from spielplan.scoring import serve
from spielplan.scoring.backbone import EMBED_DIM, Backbone, Coordinate, pack_vec

log = logging.getLogger("spielplan.scoring.foldin")

# §5.1: "Blend with the crowd prior at β = 0.8 (measured optimum)".
BETA_MAX = 0.8
BETA_GRID: tuple[float, ...] = tuple(i / 10 for i in range(11))

# §5.1's ceiling is storable as itself. It was not always: 0009's CHECK compared a `real`
# column against the numeric literal 0.8, which Postgres resolves through float8 where
# float4(0.8) is 0.800000011920929 — so `SELECT 0.8::real <= 0.8` was FALSE and a fit clamped
# to the measured optimum failed its INSERT inside a nightly job. The migration now casts the
# literal, and the write below stores β unmodified.

# Not shipped. §4.3's `ledger_hyperparams.json` carries the LEDGER's anchor λ (3.0), which is a
# different quantity in a different objective, so borrowing it would be a coincidence dressed as
# a constant. This grid is cross-validated per user instead; if the corpus tuner ever prints a
# fold-in λ, this loop becomes a read.
LAMBDA_GRID: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0)

# §0: "pipeline variance 0.003–0.008 Spearman; anything smaller is a tie." A tie must not buy
# personalisation, so an improvement inside the noise floor leaves β at 0.
NOISE_FLOOR = 0.008

# §0/§6.1: "personal signal roughly triples from 5 to 100 labels" — below five, a fitted β is
# noise wearing a number.
MIN_LABELS_FOR_CV = 5
LOO_BELOW = 25            # leave-one-out under this many labels, 5 folds at or above it

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "refit": [[u, k] for u, k in self.refit],
            "skipped": self.skipped,
            "scores_written": self.scores_written,
            "clamped": [[u, k] for u, k in self.clamped],
            "priors": self.priors.as_dict() if self.priors else None,
            "ms": round(self.ms, 1),
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
    prior_sd = float(ref_b.std())
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
        lam, beta, cv_rho, folds = _cross_validate(x, y, y_raw, z_prior, seed=seed)

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
    x: np.ndarray, y: np.ndarray, y_raw: np.ndarray, z_prior: np.ndarray, *, seed: int
) -> tuple[float, float, float, int]:
    """Choose (λ, β) by held-out Spearman against the user's own labels.

    β = 0 does not depend on λ — the prior-only blend never touches the fold-in — so all five
    λ rows must agree there. They are asserted to, because disagreement is a fold bug and a
    fold bug otherwise shows up as a slightly-too-good β.
    """
    n = x.shape[0]
    fold = _fold_assignment(n, seed)
    n_folds = int(fold.max()) + 1

    table: dict[tuple[float, float], float] = {}
    rho_at_zero: list[float] = []
    for lam in LAMBDA_GRID:
        preds = np.zeros(n)
        for f in range(n_folds):
            held = fold == f
            if held.all():
                continue
            preds[held] = x[held] @ fold_in(x[~held], y[~held], lam)
        sd = preds.std()
        z_cf = preds / sd if sd > 1e-9 else np.zeros(n)
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
        SELECT DISTINCT ON (v.title_id) v.title_id, v.value
          FROM verdict v JOIN title t ON t.id = v.title_id
         WHERE v.user_id = $1 AND t.kind = $2 AND v.superseded_by IS NULL AND NOT v.is_reask
         ORDER BY v.title_id, v.created_at DESC, v.id DESC
        """,
        user_id, kind,
    )
    return [(int(r["title_id"]), int(r["value"])) for r in rows]


async def write_fit(conn, *, user_id: int, kind: Kind, bundle_version: str, fit: Fit) -> None:
    """One `user_vector` row per (user, kind). Written even for a zero-label fit."""
    await conn.execute(
        """
        INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count, mu,
                                 prior_mean, prior_sd, cf_sd, foldin_lambda, cv_rho,
                                 bundle_version, updated_at)
        VALUES ($1, $2, 'foldin', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now())
        ON CONFLICT (user_id, kind, purpose) DO UPDATE
           SET vec = EXCLUDED.vec, blend_beta = EXCLUDED.blend_beta,
               label_count = EXCLUDED.label_count, mu = EXCLUDED.mu,
               prior_mean = EXCLUDED.prior_mean, prior_sd = EXCLUDED.prior_sd,
               cf_sd = EXCLUDED.cf_sd, foldin_lambda = EXCLUDED.foldin_lambda,
               cv_rho = EXCLUDED.cv_rho, bundle_version = EXCLUDED.bundle_version,
               updated_at = now()
        """,
        user_id, kind, pack_vec(fit.v), fit.beta, fit.label_count, fit.mu,
        fit.prior_mean, fit.prior_sd, fit.cf_sd, fit.lam, fit.cv_rho, bundle_version,
    )


async def refit_user(
    conn, backbone: Backbone, *, user_id: int, kind: Kind, bundle_version: str
) -> Fit:
    """Refit one (user, kind) and rewrite its `user_score` rows. §5.3, §10's rebuild set."""
    coords = await serve.coordinates(conn, backbone, bundle_version=bundle_version, kind=kind)
    reference = list(coords.values())
    labels = await live_labels(conn, user_id=user_id, kind=kind)
    # Seeded from the identity of the fit, so a refit of the same (user, kind, basis) draws the
    # same folds and the §6.7 log line means the same thing twice.
    seed = abs(hash((user_id, kind, bundle_version))) % (2**32)

    fit = fit_user(labels, coords, reference, seed=seed)
    if fit.beta_clamped:
        log.warning(
            "user %s/%s: cross-validation wanted β above §5.1's measured optimum; clamped to %.2f",
            user_id, kind, BETA_MAX,
        )
    if fit.dropped:
        log.warning(
            "user %s/%s: %d verdicts sit on titles with no coordinate and cannot inform the fit",
            user_id, kind, fit.dropped,
        )

    await write_fit(conn, user_id=user_id, kind=kind, bundle_version=bundle_version, fit=fit)
    await serve.replace_scores(
        conn, user_id=user_id, kind=kind, bundle_version=bundle_version,
        rows=score_many(fit, reference),
    )
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
    runs `only_stale=True`, refitting only what moved. Running a millisecond job more often is a
    superset of the spec's cadence, not a change to it.

    Ordering inside the nightly pass matters: priors first, refits second. A prior materialised
    before the night's placements would leave freshly placed titles at `e_source = 'none'` and
    therefore out of every ranked list for a day.
    """
    started = time.perf_counter()
    report = FoldInReport()
    if with_priors:
        report.priors = await serve.materialise_priors(conn, backbone, bundle_version=bundle_version)

    users = await conn.fetch("SELECT id FROM app_user WHERE role IN ('admin', 'member') ORDER BY id")
    for row in users:
        user_id = int(row["id"])
        for kind in KINDS:
            if only_stale and not await _is_stale(
                conn, user_id=user_id, kind=kind, bundle_version=bundle_version
            ):
                report.skipped += 1
                continue
            fit = await refit_user(
                conn, backbone, user_id=user_id, kind=kind, bundle_version=bundle_version
            )
            report.refit.append((user_id, kind))
            report.scores_written += 1
            if fit.beta_clamped:
                report.clamped.append((user_id, kind))
    report.ms = (time.perf_counter() - started) * 1000.0
    return report


async def _is_stale(conn, *, user_id: int, kind: Kind, bundle_version: str) -> bool:
    """Never fitted, fitted against another basis (§10), or the live label count moved.

    A label count is a cheap and complete trigger for what a *rating sitting* changes. It is
    deliberately not a trigger for what a *placement* changes — a title that gained a coordinate
    today does not move anyone's label count — so the nightly `only_stale=False` pass is what
    picks those up, and the docstring says so rather than the count pretending to cover it.
    """
    row = await conn.fetchrow(
        "SELECT label_count, bundle_version FROM user_vector "
        "WHERE user_id = $1 AND kind = $2 AND purpose = 'foldin'",
        user_id, kind,
    )
    if row is None or row["bundle_version"] != bundle_version:
        return True
    live = await conn.fetchval(
        """
        SELECT count(DISTINCT v.title_id) FROM verdict v JOIN title t ON t.id = v.title_id
         WHERE v.user_id = $1 AND t.kind = $2 AND v.superseded_by IS NULL AND NOT v.is_reask
        """,
        user_id, kind,
    )
    return int(live or 0) != int(row["label_count"] or 0)
