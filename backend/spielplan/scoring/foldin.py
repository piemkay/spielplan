"""The 64-d user fold-in and the per-label-count blend weight (§5.1, §5.3).

    score_u(t) = μ_u + (1−β_u)·z(b(t)) + β_u·⟨v_u, d(t)⟩     β is the PERSONAL weight (decision 167)

§5.1's corpus optimum of 0.8 is crowd weight, i.e. β = 0.2 here. One scalar β per (user, kind), no router.
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
from spielplan.ledger.model import class_step
from spielplan.ledger.observations import (
    LIVE_LABEL_SQL,
    cutover_sql,
    latest_tier_edit_sql,
    rescale_level,
    tier_set_of,
)
from spielplan.scoring import serve
from spielplan.scoring.backbone import (
    COORDINATE_GEOMETRY,
    EMBED_DIM,
    Backbone,
    Coordinate,
    directions,
    pack_vec,
)

log = logging.getLogger("spielplan.scoring.foldin")

# A floor of one fifth on the crowd prior, not §5.1's optimum; 0009_scoring.sql CHECKs it.
BETA_MAX = 0.8
BETA_GRID: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

# Cross-validated per user: the Ledger's shipped λ is a different quantity.
LAMBDA_GRID: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0)

# §0: an improvement inside pipeline variance (0.003-0.008) is a tie, and a tie must not buy
# personalisation.
NOISE_FLOOR = 0.008

MIN_LABELS_FOR_CV = 5   # §0/§6.1's learning curve starts at five labels
LOO_BELOW = 25          # leave-one-out under this many, 5 folds at or above

# Seconds. The tick rewrites the whole user_score partition, so it waits for a pause in the sitting,
# but at most HARD_CAP_SECONDS.
PAUSE_SECONDS = 30
HARD_CAP_SECONDS = 300

# Seconds `refit_user` backdates its clock: a write that began before the fit's read may commit
# after it.
CLOCK_MARGIN_SECONDS = 2


@dataclass(frozen=True, eq=False)
class Fit:
    """One (user, kind) fold-in."""

    v: np.ndarray            # (64,) float64, already divided by cf_sd
    mu: float
    beta: float
    lam: float
    cv_rho: float
    cf_sd: float             # the PRE-normalisation sd of ⟨v, e⟩; 0 means "no signal"
    prior_mean: float
    prior_sd: float
    label_count: int         # every title of this kind with a step target; `used` is how many the fit saw
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
    # Split because the ridge solve is milliseconds and the partition rewrite is hundreds of them.
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
    """Ridge normal equations: (XᵀX + λI)⁻¹ Xᵀy, in float64 so a refit report reproduces."""
    xd = np.asarray(x, dtype=np.float64)
    gram = xd.T @ xd + float(lam) * np.eye(EMBED_DIM)
    return np.linalg.solve(gram, xd.T @ np.asarray(y, dtype=np.float64))


def _ranks(a: np.ndarray) -> np.ndarray:
    """Tie-averaged ranks. Ties are not rare here: the target has K levels."""
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
    """A constant vector correlates with nothing: 0.0."""
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
        directions(reference),
        np.asarray([c.b for c in reference], dtype=np.float64),
    )


def fit_user(
    labels: Sequence[tuple[int, int]],
    coords: Mapping[int, Coordinate],
    reference: Sequence[Coordinate],
    *,
    seed: int = 0,
) -> Fit:
    """§5.1's fold-in and blend weight for one (user, kind), fitted to `(title_id, step)` targets.

    Both halves are standardised over `reference`, a fixed population, so a score does not
    depend on the filter the user has typed.
    """
    ref_e, ref_b = _reference_arrays(reference)
    prior_mean = float(ref_b.mean()) if ref_b.size else 0.0
    # np.std of an empty array is NaN, which the `< 1e-9` test below does not catch.
    prior_sd = float(ref_b.std()) if ref_b.size else 1.0
    if prior_sd < 1e-9:                      # one title, or a flat crowd: the prior orders nothing
        prior_sd = 1.0

    # Sorted so the positional CV folds depend on the label set, not on row order.
    ordered = sorted(labels, key=lambda pair: int(pair[0]))
    labelled = [(coords[t], float(step)) for t, step in ordered if t in coords]
    rows = [
        (d, y, c.b)
        for (c, y), d in zip(labelled, directions([c for c, _ in labelled]), strict=True)
    ]
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
        lam, beta, cv_rho, folds = _cross_validate(x, y, y_raw, z_prior, ref_e, seed=seed)

    beta_clamped = beta > BETA_MAX
    beta = min(beta, BETA_MAX)

    v = fold_in(x, y, lam)
    cf_sd = float((ref_e @ v).std()) if ref_e.size else 0.0
    if cf_sd < 1e-9:
        # The personal half orders nothing over the reference; say so with β = 0.
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

    Each fold's personal half is divided by its sd over the reference, as serving does, so the
    β chosen is the β served.
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
            # Zeros rather than NaN when the reference orders nothing, as in `fit_user`.
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
        return LAMBDA_GRID[-1], 0.0, rho0, n_folds

    # Within noise of the best, prefer the smallest β, then the smallest λ at that β.
    within = [k for k, rho in table.items() if best - rho <= NOISE_FLOOR]
    beta = min(k[1] for k in within)
    lam = min(k[0] for k in within if k[1] == beta)
    return lam, beta, table[(lam, beta)], n_folds


def score_many(fit: Fit, coords: Sequence[Coordinate]) -> list[tuple[int, float, float]]:
    """(title_id, score, cf) for a whole reference population in one matvec."""
    if not coords:
        return []
    e = directions(coords)
    b = np.asarray([c.b for c in coords], dtype=np.float64)
    cf = e @ fit.v
    scores = fit.mu + (1.0 - fit.beta) * (b - fit.prior_mean) / fit.prior_sd + fit.beta * cf
    return [(c.title_id, float(s), float(f)) for c, s, f in zip(coords, scores, cf, strict=True)]


# --- the job -----------------------------------------------------------------------------------


# Every title of the kind the person answered since their cut-over, `$1` = user, `$2` = kind.
_ANSWERED_SQL = f"""
    WITH edit AS ({latest_tier_edit_sql()}), label AS ({LIVE_LABEL_SQL})
    SELECT COALESCE(e.title_id, l.title_id) AS title_id, e.tier, e.n_levels, l.value,
           GREATEST(e.created_at, l.created_at) AS created_at
      FROM edit e FULL JOIN label l ON l.title_id = e.title_id
      JOIN title t ON t.id = COALESCE(e.title_id, l.title_id)
     WHERE t.kind = $2
"""


async def live_labels(conn, *, user_id: int, kind: Kind) -> list[tuple[int, int]]:
    """§5.1's step targets, `(title_id, step)` in today's K: the placed tier, else the middle tier of
    the live verdict's class (decision 536)."""
    k = len(await tier_set_of(conn, user_id=user_id, kind=kind))
    rows = await conn.fetch(_ANSWERED_SQL, user_id, kind)
    return [
        (
            int(r["title_id"]),
            class_step(int(r["value"]), k)
            if r["tier"] is None
            else rescale_level(int(r["tier"]), k_from=r["n_levels"], k_to=k),
        )
        for r in rows
    ]


async def write_fit(
    conn, *, user_id: int, kind: Kind, bundle_version: str, fit: Fit, updated_at: datetime
) -> None:
    """One `user_vector` row per (user, kind), written even for a zero-label fit.

    `updated_at` must be the instant the labels were read, not `now()`: `_is_stale` compares it
    against label times.
    """
    await conn.execute(
        """
        INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count, mu,
                                 prior_mean, prior_sd, cf_sd, foldin_lambda, cv_rho,
                                 bundle_version, updated_at, geometry)
        VALUES ($1, $2, 'foldin', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (user_id, kind, purpose) DO UPDATE
           SET vec = EXCLUDED.vec, blend_beta = EXCLUDED.blend_beta,
               label_count = EXCLUDED.label_count, mu = EXCLUDED.mu,
               prior_mean = EXCLUDED.prior_mean, prior_sd = EXCLUDED.prior_sd,
               cf_sd = EXCLUDED.cf_sd, foldin_lambda = EXCLUDED.foldin_lambda,
               cv_rho = EXCLUDED.cv_rho, bundle_version = EXCLUDED.bundle_version,
               updated_at = EXCLUDED.updated_at, geometry = EXCLUDED.geometry
        """,
        user_id, kind, pack_vec(fit.v), fit.beta, fit.label_count, fit.mu,
        fit.prior_mean, fit.prior_sd, fit.cf_sd, fit.lam, fit.cv_rho, bundle_version,
        updated_at, COORDINATE_GEOMETRY,
    )


async def refit_user(
    conn, backbone: Backbone, *, user_id: int, kind: Kind, bundle_version: str,
    report: FoldInReport | None = None,
) -> Fit:
    """Refit one (user, kind) and rewrite its `user_score` rows. §5.3, §10's rebuild set."""
    entered = time.perf_counter()
    # Read before the coordinates and the labels, so nothing this fit saw is newer than its stamp.
    # clock_timestamp(), not now(): on §10's rebuild path this runs inside a long transaction.
    fitted_at = await conn.fetchval(
        "SELECT clock_timestamp() - ($1::int * interval '1 second')", CLOCK_MARGIN_SECONDS
    )
    coords = await serve.coordinates(conn, backbone, bundle_version=bundle_version, kind=kind)
    reference = list(coords.values())
    labels = await live_labels(conn, user_id=user_id, kind=kind)
    # Not `hash()`: str hashing is salted per process, which reshuffled the folds on every restart.
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
            "user %s/%s: %d step targets sit on titles with no coordinate and cannot inform the fit",
            user_id, kind, fit.dropped,
        )

    # One transaction: a committed fit with no scores reads as fresh and serves empty shelves.
    async with conn.transaction():
        await write_fit(
            conn, user_id=user_id, kind=kind, bundle_version=bundle_version, fit=fit,
            updated_at=fitted_at,
        )
        await serve.replace_scores(
            conn, user_id=user_id, kind=kind, bundle_version=bundle_version, rows=rows,
        )
    if report is not None:
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
    """§5.3's nightly pass (`only_stale=False, with_priors=True`) and the cheap tick.

    The tick answers "after a sitting" (§12 M2) the same evening. Priors come before refits, or
    freshly placed titles miss every ranked list for a day.
    """
    started = time.perf_counter()
    report = FoldInReport()
    if with_priors:
        report.priors = await serve.materialise_priors(conn, backbone, bundle_version=bundle_version)
    else:
        # The tick rewrites all priors after a geometry change, and otherwise only the priors of
        # newly placed titles, which no ranked read shows until they have a `title_prior` row.
        if await _fitted_under_another_geometry(conn):
            report.priors = await serve.materialise_priors(
                conn, backbone, bundle_version=bundle_version
            )
        else:
            owed = await serve.priors_owed(conn, bundle_version=bundle_version)
            if owed:
                report.priors = await serve.materialise_priors(
                    conn, backbone, bundle_version=bundle_version, title_ids=owed
                )

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


async def _fitted_under_another_geometry(conn) -> bool:
    """Whether any household member's stored fold-in predates this reading of the coordinate.

    Household only: a deactivated account is never refitted and would re-trigger every tick.
    """
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM user_vector WHERE purpose = 'foldin' "
            "   AND user_id = ANY($1::bigint[]) AND geometry <> $2)",
            list(await household_ids(conn)), COORDINATE_GEOMETRY,
        )
    )


async def _is_stale(conn, *, user_id: int, kind: Kind, bundle_version: str) -> bool:
    """Never fitted, another basis or geometry, a newer placement, or a label moved since the fit.

    "Moved" is the count, a verdict or tier edit newer than the fit (a re-rating or a move leaves the
    count alone), or a cut-over since it. Label moves are debounced by PAUSE_SECONDS /
    HARD_CAP_SECONDS; the other triggers are not.
    """
    row = await conn.fetchrow(
        "SELECT label_count, bundle_version, geometry, updated_at, "
        "       now() - updated_at > ($3::int * interval '1 second') AS past_cap "
        "  FROM user_vector WHERE user_id = $1 AND kind = $2 AND purpose = 'foldin'",
        user_id, kind, HARD_CAP_SECONDS,
    )
    if row is None or row["bundle_version"] != bundle_version:
        return True
    if row["geometry"] != COORDINATE_GEOMETRY:
        return True
    placed_since = await conn.fetchval(
        "SELECT max(p.created_at) > $3::timestamptz + ($4::int * interval '1 second') "
        "  FROM title_placement p JOIN title t ON t.id = p.title_id "
        " WHERE p.bundle_version = $1 AND t.kind = $2",
        bundle_version, kind, row["updated_at"], CLOCK_MARGIN_SECONDS,
    )
    if placed_since:
        return True
    live = await conn.fetchrow(
        f"""
        SELECT count(*) AS n, max(a.created_at) AS newest,
               max(a.created_at) < now() - ($3::int * interval '1 second') AS paused,
               {cutover_sql()} > $4::timestamptz AS cut_since
          FROM ({_ANSWERED_SQL}) a
        """,
        user_id, kind, PAUSE_SECONDS, row["updated_at"],
    )
    newest = live["newest"]
    moved = (
        int(live["n"] or 0) != int(row["label_count"] or 0)
        or (newest is not None and newest > row["updated_at"])
        or bool(live["cut_since"])
    )
    if not moved:
        return False
    # `newest is None`: every label was undone, so there is nothing to wait for (`paused` is NULL).
    return newest is None or bool(live["paused"]) or bool(row["past_cap"])
