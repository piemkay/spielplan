"""The Ledger's two jobs, and the state they share. Spec v2.1 §5.2, §5.3, §6.0, §6.3, §12.

§5.3 puts three rows on the Ledger:

    | Ledger incremental update              | every observation | <50 ms  |
    | Ledger full MAP refit + cutpoints + σ  | nightly           | seconds |

`refit_user` is the second and `update_incrementally` the first, and the point of this module
is that they are **the same model at two resolutions, not two models**:

  I1. Same objective. The incremental step minimises `model`'s F — the same F, the same
      `Hyperparams` — restricted to the residuals of the one or two titles the observation
      touched. It calls `model`'s own derivative assembly to do it, so a sign error would have
      to be made in `model.py` and would break the nightly fit too.
  I2. Monotone. The restriction is solved to a stationary point of a strictly convex problem,
      so F never rises. Repeated incremental updates move toward the nightly answer.
  I3. Authoritative reconciliation. The nightly refit overwrites everything the incremental
      path wrote and resets `fit_source` to 'nightly'. What it corrects is exactly the frozen
      block — (μ, v, γ, cuts, ψ) — plus the σ approximation named at `_sherman_morrison`.

What each writes:

  `ledger_state`     one row per title of that kind the household owns, plus any title the
                     person has rated that it no longer owns. §6.0's shelves and §6.3's board
                     read this table and nothing else.
  `ledger_cutpoints` the TIER arm's fitted cutpoints. §5.2: they *are* the displayed
                     boundaries, so there is no second set and no percentile fallback.
  `ledger_fit`       the numpy state the <50 ms path needs in order not to redo the solve.

THE GUARD THAT IS NOT OPTIONAL. Postgres accepts NaN in a `double precision` column and sorts
it **above every real number**. `ledger_state_rank` is `(user_id, kind, s DESC)`. So a single
non-finite `s` does not produce a missing row or an error — it produces a title pinned to the
top of every §6.0 shelf, for as long as it takes someone to notice. Nothing here writes a
non-finite `s` or `σ`: the fit is checked before the transaction opens, and the incremental
path falls back to a full refit rather than persisting one.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import asyncpg
import numpy as np

from spielplan.ledger import model, observations
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.model import EMBED_DIM, ObservationSet
from spielplan.ledger.observations import EmbeddingSource, zero_embeddings

log = logging.getLogger("spielplan.ledger.refit")

# A calendar conversion, not a tuning constant: the mean Gregorian month in days. §5.2's "12
# months untouched" and "rate c per √month" both need months, and `hp` owns the 12 and the c.
DAYS_PER_MONTH = 365.2425 / 12.0


class RefitRefused(Exception):
    """The fit produced something that must not reach `ledger_state`. See the module docstring:
    a NaN `s` is not a missing row, it is the top of every shelf."""


# --- the numpy blobs ---------------------------------------------------------------------------


def _npy(array: np.ndarray) -> bytes:
    """`ledger_fit`'s columns are ".npy payloads, so shape and dtype travel with the bytes
    rather than living in a comment that can drift from the writer"."""
    buffer = io.BytesIO()
    np.lib.format.write_array(buffer, np.ascontiguousarray(array), allow_pickle=False)
    return buffer.getvalue()


def _unnpy(blob: bytes) -> np.ndarray:
    return np.lib.format.read_array(io.BytesIO(blob), allow_pickle=False)


# --- reports and deltas -------------------------------------------------------------------------


@dataclass
class RefitReport:
    user_id: int
    kind: str
    n_titles: int = 0
    n_observed: int = 0
    n_verdicts: int = 0
    n_tier_edits: int = 0
    n_duels: int = 0
    n_held_out: int = 0
    n_reask: int = 0
    fitted: bool = False
    converged: bool = False
    grad_inf: float = 0.0
    objective: float = 0.0
    # §5.2's scar, reported rather than inferred: the largest ratio of duel curvature to
    # anchor curvature over the titles — what a fixed-step method would have had to divide by.
    rho: float = 0.0
    iterations: tuple[int, int] = (0, 0)
    backtracks: int = 0
    seconds: float = 0.0
    cutpoints: list[float] = field(default_factory=list)
    tier_set: tuple[str, ...] = ()
    hyperparams_source: str = "default"
    rejected_nonfinite: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "kind": self.kind,
            "n_titles": self.n_titles,
            "n_observed": self.n_observed,
            "n_verdicts": self.n_verdicts,
            "n_tier_edits": self.n_tier_edits,
            "n_duels": self.n_duels,
            "n_held_out": self.n_held_out,
            "fitted": self.fitted,
            "converged": self.converged,
            "grad_inf": self.grad_inf,
            "rho": self.rho,
            "seconds": round(self.seconds, 3),
            "cutpoints": self.cutpoints,
            "hyperparams_source": self.hyperparams_source,
            "rejected_nonfinite": self.rejected_nonfinite,
            "error": self.error,
        }


@dataclass(frozen=True)
class Row:
    """One `ledger_state` row, as the UI reads it."""

    title_id: int
    s: float
    sigma: float
    sigma_eff: float
    cdf: float | None
    tier: int | None
    straddle: int | None
    observed: bool


@dataclass(frozen=True)
class Delta:
    """What one observation moved. §6.1 wants this back inside a card's budget."""

    user_id: int
    kind: str
    rows: tuple[Row, ...]
    fit_source: str
    # True when the cache was cold or stale and the whole fit was redone. Not the <50 ms path:
    # a cache miss is a cache miss, and reporting it as an incremental update would be the one
    # way to make §5.3's budget unfalsifiable.
    refit: bool = False
    iterations: int = 0
    micros: int = 0


# --- the fit cache -------------------------------------------------------------------------------


@dataclass
class FitCache:
    user_id: int
    kind: str
    title_ids: np.ndarray
    r: np.ndarray
    sigma: np.ndarray
    sigma_prior: np.ndarray
    anchor_curv: np.ndarray
    duel_curv: np.ndarray
    cdf_reference: np.ndarray
    z_cov: np.ndarray
    mu: float
    v: np.ndarray
    gamma: np.ndarray
    cuts: np.ndarray
    log_nu: float
    n_observed: int

    @property
    def n_levels(self) -> int:
        return int(self.cuts.size) + 1


def _pack_theta(fit: model.Fit) -> np.ndarray:
    return np.concatenate(
        [[fit.mu], fit.v, fit.gamma, fit.cuts, [fit.log_nu]]
    ).astype(float)


def _unpack_theta(theta: np.ndarray) -> tuple[float, np.ndarray, np.ndarray, np.ndarray, float]:
    mu = float(theta[0])
    v = theta[1 : 1 + EMBED_DIM]
    gamma = theta[1 + EMBED_DIM : 3 + EMBED_DIM]
    cuts = theta[3 + EMBED_DIM : -1]
    return mu, v, gamma, cuts, float(theta[-1])


async def active_bundle_version(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")


async def load_cache(
    conn: asyncpg.Connection, *, user_id: int, kind: str, hp: Hyperparams, lock: bool = True
) -> FitCache | None:
    """The cached nightly fit, or None when it must not be trusted.

    Two preconditions, and both are correctness rather than freshness. §4.3: "every constant
    comes from `ledger_hyperparams.json`" — a cache built under other constants is *wrong*, so
    `hp_digest` is compared, not logged. §10: "everything expressed in the old Backbone's basis
    is garbage against a new one" — so a fit whose bundle is no longer the active one is not
    stale either, and returning it would put a title's coordinate in a basis nobody uses.

    `FOR UPDATE` because two observations for one person must serialise: §6.1's block counter
    and decision 35's Undo depth are both defined over a sequence, not over a race.
    """
    row = await conn.fetchrow(
        "SELECT * FROM ledger_fit WHERE user_id = $1 AND kind = $2"
        + (" FOR UPDATE" if lock else ""),
        user_id,
        kind,
    )
    if row is None:
        return None
    if row["hp_digest"] != hp.digest():
        log.info(
            "ledger_fit for user %d/%s was built under other hyperparameters — refitting",
            user_id,
            kind,
        )
        return None
    active = await active_bundle_version(conn)
    if row["bundle_version"] != active:
        log.info(
            "ledger_fit for user %d/%s is in bundle %r, active is %r — refitting",
            user_id,
            kind,
            row["bundle_version"],
            active,
        )
        return None
    mu, v, gamma, cuts, log_nu = _unpack_theta(_unnpy(row["theta"]))
    return FitCache(
        user_id=user_id,
        kind=kind,
        title_ids=_unnpy(row["title_ids"]).astype(np.int64),
        r=_unnpy(row["residuals"]),
        sigma=_unnpy(row["sigma"]),
        sigma_prior=_unnpy(row["sigma_prior"]),
        anchor_curv=_unnpy(row["anchor_curv"]),
        duel_curv=_unnpy(row["duel_curv"]),
        cdf_reference=_unnpy(row["cdf_reference"]),
        z_cov=_unnpy(row["z_cov"]),
        mu=mu,
        v=v,
        gamma=gamma,
        cuts=cuts,
        log_nu=log_nu,
        n_observed=int(row["n_observed"]),
    )


# --- the σ a title would have if it had never been rated -------------------------------------


def _prior_sigma(embeddings: np.ndarray, z_cov: np.ndarray, hp: Hyperparams) -> np.ndarray:
    """`model._laplace`'s unobserved formula, evaluated off the observed block.

    σ_prior² = τ_b² + [1, e] Σ_(μ,v) [1, e]ᵀ — "the σ this title would have if you had never
    rated it". §5.2 caps freshness inflation there, and §12's M2 exit criterion ("every owned
    title has a coordinate") means the *un*rated titles need one too.
    """
    jac = np.concatenate([np.ones((embeddings.shape[0], 1)), embeddings], axis=1)
    var = np.einsum("ij,jk,ik->i", jac, z_cov, jac) + hp.b_i_tau**2
    return np.sqrt(np.maximum(var, 1e-12))


def _months_since(stamps: Sequence[datetime | None], now: datetime) -> np.ndarray:
    out = np.zeros(len(stamps))
    for i, stamp in enumerate(stamps):
        if stamp is None:
            continue
        out[i] = max((now - stamp).total_seconds() / 86400.0, 0.0) / DAYS_PER_MONTH
    return out


# --- the nightly job -----------------------------------------------------------------------------


async def refit_user(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    now: datetime | None = None,
) -> RefitReport:
    """§5.3's "Ledger full MAP refit + cutpoints + σ", for one (user, kind)."""
    embeddings = embeddings or zero_embeddings
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    report = RefitReport(user_id=user_id, kind=kind, hyperparams_source=hp.source)

    loaded = await observations.load_observations(
        conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings
    )
    obs = loaded.obs
    report.n_observed = obs.n
    report.n_verdicts = loaded.n_verdicts
    report.n_tier_edits = loaded.n_tier_edits
    report.n_duels = loaded.n_duels
    report.n_held_out = loaded.n_held_out
    report.n_reask = loaded.n_reask
    report.tier_set = loaded.tier_set

    if obs.is_empty():
        # Nothing to fit. Writing 900 identical rows at s = 0 would be inventing a ranking out
        # of a prior and calling it the person's taste; §6.0's shelves are better empty.
        report.seconds = time.perf_counter() - started
        return report

    fit = model.fit(obs, hp)
    if not (
        np.isfinite(fit.mu)
        and np.all(np.isfinite(fit.v))
        and np.all(np.isfinite(fit.cuts))
        and np.all(np.isfinite(fit.gamma))
    ):
        raise RefitRefused(
            f"user {user_id}/{kind}: the fit's dense block is not finite; ledger_state and "
            "ledger_cutpoints keep their previous values"
        )

    report.fitted = True
    report.converged = fit.converged
    report.grad_inf = fit.grad_inf
    report.objective = fit.objective
    report.rho = fit.rho
    report.iterations = fit.iterations
    report.backtracks = fit.backtracks
    report.cutpoints = [float(c) for c in fit.cuts]

    # ---- the rest of the library --------------------------------------------------------
    # §12's M2 exit criterion is "every owned title has a coordinate", and §5.2 gives an
    # unobserved title one at zero extra parameters: it has no r, so s = μ + ⟨v, e⟩. Titles
    # the household no longer owns but the person has rated keep their row — §7.2 re-derives
    # `is_owned` from Jellyfin, and a title that leaves the library for a week should not take
    # somebody's rating history off their board with it.
    owned = [
        int(r["id"])
        for r in await conn.fetch(
            "SELECT id FROM title WHERE is_owned AND kind = $1 ORDER BY id", kind
        )
    ]
    observed_ids = set(int(t) for t in obs.title_ids)
    extra = [t for t in owned if t not in observed_ids]
    extra_e, _extra_mask = await observations.resolve_embeddings(embeddings, extra)

    title_ids = np.concatenate([obs.title_ids, np.asarray(extra, dtype=np.int64)])
    s = np.concatenate([fit.s, fit.mu + extra_e @ fit.v])
    sigma_prior = np.concatenate(
        [fit.sigma_prior, _prior_sigma(extra_e, fit.z_cov, hp)]
    )
    # An unobserved title's σ *is* its prior σ: that is what "never rated" means.
    sigma = np.concatenate([fit.sigma, sigma_prior[obs.n :]])
    observed = np.concatenate([np.ones(obs.n, dtype=bool), np.zeros(len(extra), dtype=bool)])
    stamps: list[datetime | None] = list(loaded.last_observed_at) + [None] * len(extra)

    # §5.2's freshness rule, on σ_eff only. Letting it back into the likelihood would be
    # re-weighting history by the calendar, which the spec's freshness sentence deliberately
    # does not do — `ledger_state.sigma` keeps the fitted value.
    sigma_eff = model.inflate_sigma(sigma, sigma_prior, _months_since(stamps, now), hp)

    # §5.2: "the empirical CDF of the user's own fitted s values, computed per kind". The
    # reference is the OBSERVED block — the owner's "always-preferred → 1.0" is about titles
    # the person has judged, and a reference over the whole library would move every displayed
    # number whenever the library grew.
    cdf = model.empirical_cdf(fit.s, s)
    tier = model.tier_of(s, fit.cuts)
    straddle = model.straddle(s, sigma_eff, fit.cuts, hp)

    finite = np.isfinite(s) & np.isfinite(sigma) & np.isfinite(sigma_eff)
    report.rejected_nonfinite = int((~finite).sum())
    if report.rejected_nonfinite:
        log.error(
            "user %d/%s: %d title(s) produced a non-finite s or σ and were not written",
            user_id,
            kind,
            report.rejected_nonfinite,
        )

    bundle = await active_bundle_version(conn)
    async with conn.transaction():
        await _write_state(
            conn,
            user_id=user_id,
            kind=kind,
            title_ids=title_ids[finite],
            s=s[finite],
            sigma=sigma[finite],
            sigma_prior=sigma_prior[finite],
            sigma_eff=sigma_eff[finite],
            cdf=cdf[finite],
            tier=tier[finite],
            straddle=straddle[finite],
            observed=observed[finite],
            stamps=[st for st, keep in zip(stamps, finite, strict=True) if keep],
            fit_source="nightly",
            prune=True,
        )
        # §5.2: the tier arm's cutpoints ARE the displayed boundaries. One row per (user, kind),
        # and `ledger_cutpoints`' own CHECK ties its length to the tier set.
        await conn.execute(
            """
            INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (user_id, kind) DO UPDATE
              SET boundaries = EXCLUDED.boundaries, tier_set = EXCLUDED.tier_set,
                  updated_at = now()
            """,
            user_id,
            kind,
            [float(c) for c in fit.cuts],
            list(loaded.tier_set),
        )
        await _write_fit(
            conn,
            user_id=user_id,
            kind=kind,
            hp=hp,
            bundle=bundle,
            fit=fit,
            obs=obs,
            fit_source="nightly",
        )

    report.n_titles = int(finite.sum())
    report.seconds = time.perf_counter() - started
    return report


async def refit_all(
    conn: asyncpg.Connection,
    hp: Hyperparams,
    *,
    embeddings: EmbeddingSource | None = None,
    now: datetime | None = None,
) -> list[RefitReport]:
    """§5.3's nightly row, over the household. One person's bad fit must not stop the others'."""
    reports: list[RefitReport] = []
    users = await conn.fetch("SELECT id FROM app_user WHERE is_active ORDER BY id")
    for row in users:
        for kind in observations.KINDS:
            try:
                reports.append(
                    await refit_user(
                        conn,
                        user_id=int(row["id"]),
                        kind=kind,
                        hp=hp,
                        embeddings=embeddings,
                        now=now,
                    )
                )
            except (RefitRefused, ValueError) as exc:
                log.exception("refit failed for user %s/%s", row["id"], kind)
                reports.append(
                    RefitReport(user_id=int(row["id"]), kind=kind, error=str(exc))
                )
    return reports


# --- writing -----------------------------------------------------------------------------------


async def _write_state(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: np.ndarray,
    s: np.ndarray,
    sigma: np.ndarray,
    sigma_prior: np.ndarray,
    sigma_eff: np.ndarray,
    cdf: np.ndarray,
    tier: np.ndarray,
    straddle: np.ndarray,
    observed: np.ndarray,
    stamps: Sequence[datetime | None],
    fit_source: str,
    prune: bool = False,
) -> None:
    """One statement for the whole board. `unnest` rather than a loop because the nightly job
    writes every owned title of the kind and a round trip per title is the difference between
    §5.3's "seconds" and a minute of latency."""
    if title_ids.size == 0:
        if prune:
            await conn.execute(
                "DELETE FROM ledger_state WHERE user_id = $1 AND kind = $2", user_id, kind
            )
        return
    ids = [int(t) for t in title_ids]
    # NaN never reaches the column: §5.2's CDF is undefined for fewer than two observed titles,
    # and `cdf` is nullable precisely so "we cannot place you yet" has a representation that is
    # not a number sorting above every other number.
    cdf_out = [None if not np.isfinite(c) else float(c) for c in cdf]
    await conn.execute(
        """
        INSERT INTO ledger_state
            (user_id, title_id, kind, s, sigma, sigma_prior, sigma_eff, cdf, tier, straddle,
             observed, last_observed_at, fit_source, updated_at)
        SELECT $1, x.title_id, $2, x.s, x.sigma, x.sigma_prior, x.sigma_eff, x.cdf, x.tier,
               x.straddle, x.observed, x.last_observed_at, $3, now()
        FROM unnest($4::int[], $5::float8[], $6::float8[], $7::float8[], $8::float8[],
                    $9::float8[], $10::smallint[], $11::smallint[], $12::boolean[],
                    $13::timestamptz[])
             AS x(title_id, s, sigma, sigma_prior, sigma_eff, cdf, tier, straddle, observed,
                  last_observed_at)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET kind = EXCLUDED.kind, s = EXCLUDED.s, sigma = EXCLUDED.sigma,
              sigma_prior = EXCLUDED.sigma_prior, sigma_eff = EXCLUDED.sigma_eff,
              cdf = EXCLUDED.cdf, tier = EXCLUDED.tier, straddle = EXCLUDED.straddle,
              observed = EXCLUDED.observed, last_observed_at = EXCLUDED.last_observed_at,
              fit_source = EXCLUDED.fit_source, updated_at = now()
        """,
        user_id,
        kind,
        fit_source,
        ids,
        [float(x) for x in s],
        [float(x) for x in sigma],
        [float(x) for x in sigma_prior],
        [float(x) for x in sigma_eff],
        cdf_out,
        [int(x) for x in tier],
        # §6.3: −1 means "the posterior does not reach a neighbour", which is an absence and is
        # stored as one. Proposal 76's "S never renders S/S" falls out of that.
        [None if int(x) < 0 else int(x) for x in straddle],
        [bool(x) for x in observed],
        list(stamps),
    )
    if prune:
        # A title the household no longer owns and nobody has rated should not linger on a
        # shelf. Scoped to this kind, so the two partitions never delete each other's rows.
        await conn.execute(
            "DELETE FROM ledger_state WHERE user_id = $1 AND kind = $2 "
            "AND title_id <> ALL($3::int[])",
            user_id,
            kind,
            ids,
        )


async def _write_fit(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    bundle: str | None,
    fit: model.Fit,
    obs: ObservationSet,
    fit_source: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO ledger_fit
            (user_id, kind, theta, title_ids, residuals, sigma, sigma_prior, anchor_curv,
             duel_curv, cdf_reference, z_cov, n_observed, hp_digest, hp_source, bundle_version,
             fit_source, objective, grad_inf, converged, fitted_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19, now())
        ON CONFLICT (user_id, kind) DO UPDATE SET
            theta = EXCLUDED.theta, title_ids = EXCLUDED.title_ids,
            residuals = EXCLUDED.residuals, sigma = EXCLUDED.sigma,
            sigma_prior = EXCLUDED.sigma_prior, anchor_curv = EXCLUDED.anchor_curv,
            duel_curv = EXCLUDED.duel_curv, cdf_reference = EXCLUDED.cdf_reference,
            z_cov = EXCLUDED.z_cov, n_observed = EXCLUDED.n_observed,
            hp_digest = EXCLUDED.hp_digest, hp_source = EXCLUDED.hp_source,
            bundle_version = EXCLUDED.bundle_version, fit_source = EXCLUDED.fit_source,
            objective = EXCLUDED.objective, grad_inf = EXCLUDED.grad_inf,
            converged = EXCLUDED.converged, fitted_at = now()
        """,
        user_id,
        kind,
        _npy(_pack_theta(fit)),
        _npy(obs.title_ids.astype(np.int32)),
        _npy(fit.r),
        _npy(fit.sigma),
        _npy(fit.sigma_prior),
        _npy(fit.anchor_curv),
        _npy(fit.duel_curv),
        _npy(np.sort(fit.s)),
        _npy(fit.z_cov),
        int(obs.n),
        hp.digest(),
        hp.source,
        bundle,
        fit_source,
        float(fit.objective),
        float(fit.grad_inf),
        bool(fit.converged),
    )


# --- the <50 ms path -------------------------------------------------------------------------


async def _load_local(
    conn: asyncpg.Connection, *, user_id: int, kind: str, title_ids: Sequence[int], hp: Hyperparams
) -> tuple[list[Any], list[Any], list[Any], float]:
    """Only the rows that touch these titles — plus the fit set's mean margin.

    The mean is what makes the restricted problem the *same* problem: §4.3's `margin_form` is
    "margin/mean(margin)", and a mean taken over four local duels is a different weighting from
    the one the nightly fit used. One indexed aggregate is cheaper than being subtly wrong.
    """
    ids = list(title_ids)
    verdicts = await conn.fetch(
        """
        SELECT v.title_id, v.value FROM verdict v JOIN title t ON t.id = v.title_id
        WHERE v.user_id = $1 AND t.kind = $2 AND NOT v.is_reask AND v.title_id = ANY($3::int[])
        ORDER BY v.id
        """,
        user_id,
        kind,
        ids,
    )
    tier_edits = await conn.fetch(
        """
        SELECT e.title_id, e.tier FROM tier_edit e JOIN title t ON t.id = e.title_id
        WHERE e.user_id = $1 AND t.kind = $2 AND e.title_id = ANY($3::int[])
        ORDER BY e.id
        """,
        user_id,
        kind,
        ids,
    )
    duels = await conn.fetch(
        """
        SELECT d.title_a, d.title_b, d.outcome, d.margin
        FROM duel d JOIN title ta ON ta.id = d.title_a JOIN title tb ON tb.id = d.title_b
        WHERE d.user_id = $1 AND ta.kind = $2 AND tb.kind = $2
          AND d.selection <> $4 AND NOT d.is_reask
          AND (d.title_a = ANY($3::int[]) OR d.title_b = ANY($3::int[]))
        ORDER BY d.id
        """,
        user_id,
        kind,
        ids,
        observations.HELD_OUT,
    )
    mean_margin = await conn.fetchval(
        """
        SELECT avg(coalesce(d.margin, $3::float8))
        FROM duel d JOIN title ta ON ta.id = d.title_a JOIN title tb ON tb.id = d.title_b
        WHERE d.user_id = $1 AND ta.kind = $2 AND tb.kind = $2
          AND d.selection <> $4 AND NOT d.is_reask
        """,
        user_id,
        kind,
        hp.margin_hesitant,
        observations.HELD_OUT,
    )
    return list(verdicts), list(tier_edits), list(duels), float(mean_margin or hp.margin_hesitant)


def _local_hp(hp: Hyperparams, local_margins: np.ndarray, mean_margin: float) -> Hyperparams:
    """Make `model`'s own weighting produce the *global* normalisation on a local subset.

    `model._duel_weights` divides by the mean of the margins it is given, and `_grad_hess`
    multiplies by λ_bt. Scaling λ_bt by mean_local/mean_global therefore reproduces
    λ_bt · margin_j / mean_global exactly — which is the weight the nightly fit used. The
    alternative was a second assembly of the duel arm here, i.e. a second chance at a sign
    error in the one place §5.2 already has a scar.
    """
    if not hp.margin_weighting or hp.margin_form == "none" or local_margins.size == 0:
        return hp
    usable = local_margins[np.isfinite(local_margins) & (local_margins > 0)]
    if usable.size == 0 or mean_margin <= 0:
        return hp
    return replace(hp, lambda_bt=hp.lambda_bt * float(usable.mean()) / mean_margin)


def _restricted_solve(
    obs: ObservationSet,
    hp: Hyperparams,
    cache: FitCache,
    r: np.ndarray,
    block: np.ndarray,
) -> tuple[np.ndarray, int, np.ndarray, np.ndarray]:
    """Newton on F restricted to `r[block]`, everything else frozen. Invariant I1.

    One or two coordinates of a strictly convex function, so Newton converges in three to five
    steps; the Armijo guard is there because the ordinal NLL's barrier is infinite and a full
    step from a cold start can land on the wrong side of it.

    It reaches into `model`'s private derivative assembly deliberately. Invariant I1 says the
    incremental path minimises the *same* F, and the only way to make that true rather than
    intended is for both paths to call one implementation of the gradient — §5.2's fusion
    already carries one scar, and a second assembly here would be a second place to put a sign
    error that still converges.
    """
    args = (cache.mu, cache.v, cache.gamma, cache.cuts, cache.log_nu)
    iterations = 0
    with_duels = obs.duel_a.size > 0
    for _ in range(hp.newton_max_iter):
        iterations += 1
        _gz, g_r, _hzz, _hzr, h_rr, _anchor, duel, coupling = model._grad_hess(
            obs, hp, *args, r, with_duels=with_duels
        )
        hessian = np.diag(h_rr[block] + duel[block])
        if coupling is not None:
            a, b, w_hdd, _hdpsi, _hpsi = coupling
            where = {int(t): i for i, t in enumerate(block)}
            for ai, bi, w in zip(a, b, w_hdd, strict=True):
                if int(ai) in where and int(bi) in where:
                    hessian[where[int(ai)], where[int(bi)]] -= w
                    hessian[where[int(bi)], where[int(ai)]] -= w
        gradient = g_r[block]
        step = np.linalg.solve(hessian, gradient)

        base = model._objective(obs, hp, *args, r, with_duels=with_duels)
        slope = float(gradient @ step)
        eta = 1.0
        while True:
            trial = r.copy()
            trial[block] -= eta * step
            value = model._objective(obs, hp, *args, trial, with_duels=with_duels)
            if value <= base - 1e-4 * eta * slope or eta < hp.lr_min:
                break
            eta *= 0.5
        r = trial
        if float(np.max(np.abs(eta * step))) < hp.newton_tol:
            break
    # The curvature σ is updated from must be the curvature AT the answer, not one Newton step
    # behind it — the loop above breaks after moving r, so this is a fresh evaluation.
    *_, anchor, duel, _coupling = model._grad_hess(obs, hp, *args, r, with_duels=with_duels)
    return r, iterations, anchor, duel


def _sherman_morrison(
    sigma: np.ndarray, curvature_delta: np.ndarray, sigma_prior: np.ndarray
) -> np.ndarray:
    """σ after a rank-1 change of curvature at one coordinate.

    (H + q·eᵢeᵢᵀ)⁻¹ᵢᵢ = H⁻¹ᵢᵢ / (1 + q·H⁻¹ᵢᵢ), which is exact for the marginal variance under
    a bump confined to coordinate i — a verdict or a tier edit. A duel's curvature is
    j = eₐ − e_b, and the exact 2×2 form needs Cov(sₐ, s_b), which the cache does not hold; it
    is evaluated with that covariance taken as zero. **So the incremental σ is an
    approximation**, and the nightly refit replaces it with the exact Laplace marginal (I3).
    The cap at σ_prior is not cosmetic: an undo can make the denominator small or negative, and
    a title can never be *less* certain than one nobody has ever rated.
    """
    denominator = 1.0 + curvature_delta * sigma**2
    with np.errstate(divide="ignore", invalid="ignore"):
        updated = np.where(denominator > 1e-12, sigma**2 / denominator, np.inf)
    return np.sqrt(np.clip(updated, 1e-12, sigma_prior**2))


async def update_incrementally(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: Sequence[int],
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    now: datetime | None = None,
) -> Delta:
    """§5.3's "<50 ms" row: re-place the one or two titles an observation touched.

    It re-reads those titles' observations rather than being told what changed, so the same
    call serves a write and decision 35's undo: after either, the truth is in the tables and
    this solves for it.

    Two preconditions the caller owns, because checking either would cost a query inside a
    50 ms budget to catch a mistake the caller cannot make by accident: every title in
    `title_ids` is of `kind` (`Write.kind` comes from `observations.kind_of`, so a write path
    gets this right by construction), and `embeddings` is the source the cached fit was built
    with — v lives in that basis, and nothing in the database records which one it was.

    The whole update is one transaction. §6.1's block counter and decision 35's Undo depth are
    defined over a *sequence* of observations, so two taps by one person must serialise —
    `load_cache`'s `FOR UPDATE` is the gate, and a `FOR UPDATE` outside a transaction is a lock
    that is released before it is used.
    """
    async with conn.transaction():
        return await _update_incrementally(
            conn,
            user_id=user_id,
            kind=kind,
            title_ids=title_ids,
            hp=hp,
            embeddings=embeddings,
            now=now,
        )


async def _update_incrementally(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: Sequence[int],
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
    now: datetime | None,
) -> Delta:
    embeddings = embeddings or zero_embeddings
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    targets = [int(t) for t in dict.fromkeys(title_ids)]

    cache = await load_cache(conn, user_id=user_id, kind=kind, hp=hp)
    if cache is None:
        await refit_user(
            conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings, now=now
        )
        rows = await _read_rows(conn, user_id=user_id, title_ids=targets)
        return Delta(
            user_id=user_id,
            kind=kind,
            rows=rows,
            fit_source="nightly",
            refit=True,
            micros=int((time.perf_counter() - started) * 1e6),
        )

    verdicts, tier_edits, duels, mean_margin = await _load_local(
        conn, user_id=user_id, kind=kind, title_ids=targets, hp=hp
    )

    # The local index: the touched titles first (so `block` is a prefix), then their opponents,
    # whose residuals are held at the cached value.
    opponents = [
        int(t)
        for row in duels
        for t in (row["title_a"], row["title_b"])
        if int(t) not in targets
    ]
    local_ids = targets + list(dict.fromkeys(opponents))
    position = {tid: i for i, tid in enumerate(local_ids)}
    cached_at = {int(t): i for i, t in enumerate(cache.title_ids)}

    matrix, embedded = await observations.resolve_embeddings(embeddings, local_ids)
    r_local = np.array(
        [cache.r[cached_at[t]] if t in cached_at else 0.0 for t in local_ids]
    )

    ord_index, ord_level, ord_arm = [], [], []
    for row in verdicts:
        ord_index.append(position[int(row["title_id"])])
        ord_level.append(int(row["value"]))
        ord_arm.append(observations.ARM_VERDICT)
    n_levels = cache.n_levels
    for row in tier_edits:
        ord_index.append(position[int(row["title_id"])])
        ord_level.append(min(max(int(row["tier"]), 0), n_levels - 1))
        ord_arm.append(observations.ARM_TIER)
    margins = np.asarray(
        [hp.margin_hesitant if row["margin"] is None else float(row["margin"]) for row in duels]
    )
    local = ObservationSet(
        title_ids=np.asarray(local_ids, dtype=np.int64),
        embeddings=matrix,
        embedded=embedded,
        ord_index=np.asarray(ord_index, dtype=np.int64),
        ord_level=np.asarray(ord_level, dtype=np.int64),
        ord_arm=np.asarray(ord_arm, dtype=np.int64),
        ord_weight=np.ones(len(ord_index)),
        duel_a=np.asarray([position[int(r["title_a"])] for r in duels], dtype=np.int64),
        duel_b=np.asarray([position[int(r["title_b"])] for r in duels], dtype=np.int64),
        duel_outcome=np.asarray(
            [observations.OUTCOMES[r["outcome"]] for r in duels], dtype=np.int64
        ),
        duel_margin=margins,
        n_levels=n_levels,
    )
    hp_local = _local_hp(hp, margins, mean_margin)
    block = np.arange(len(targets))
    r_local, iterations, anchor, duel_curv = _restricted_solve(
        local, hp_local, cache, r_local, block
    )

    s = cache.mu + matrix[block] @ cache.v + r_local[block]
    sigma_prior = np.array(
        [
            cache.sigma_prior[cached_at[t]]
            if t in cached_at
            else _prior_sigma(matrix[position[t] : position[t] + 1], cache.z_cov, hp)[0]
            for t in targets
        ]
    )
    # A title with no cached row has never been rated, so its curvature is the residual prior
    # alone and its σ is σ_prior — which is exactly what `model._laplace` says of an
    # unobserved title, so the Sherman-Morrison bump below starts from the right place.
    sigma_before = np.array(
        [cache.sigma[cached_at[t]] if t in cached_at else sigma_prior[i]
         for i, t in enumerate(targets)]
    )
    curvature_before = np.array(
        [
            cache.anchor_curv[cached_at[t]] + cache.duel_curv[cached_at[t]]
            if t in cached_at
            else 1.0 / hp.b_i_tau**2
            for t in targets
        ]
    )
    sigma = _sherman_morrison(
        sigma_before, (anchor[block] + duel_curv[block]) - curvature_before, sigma_prior
    )

    if not (np.all(np.isfinite(s)) and np.all(np.isfinite(sigma))):
        # The guard has a recovery, not just a refusal: redo the fit exactly rather than
        # persisting a NaN or leaving the person's tap with nothing to show for it.
        log.error("incremental update for user %d/%s went non-finite — full refit", user_id, kind)
        await refit_user(
            conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings, now=now
        )
        return Delta(
            user_id=user_id,
            kind=kind,
            rows=await _read_rows(conn, user_id=user_id, title_ids=targets),
            fit_source="nightly",
            refit=True,
            micros=int((time.perf_counter() - started) * 1e6),
        )

    # Just observed, so no freshness inflation applies; the call is kept rather than assumed so
    # the two paths agree by construction if §5.2's rule ever gains a term.
    sigma_eff = model.inflate_sigma(sigma, sigma_prior, np.zeros(len(targets)), hp)
    # §5.2's CDF against the cached reference: the same definition, one night stale. Rebuilding
    # it per observation is O(library) and is what the nightly job is for.
    cdf = model.empirical_cdf(cache.cdf_reference, s)
    tier = model.tier_of(s, cache.cuts)
    straddle = model.straddle(s, sigma_eff, cache.cuts, hp)
    touched = {int(row["title_id"]) for row in verdicts} | {
        int(row["title_id"]) for row in tier_edits
    }
    for row in duels:
        touched.add(int(row["title_a"]))
        touched.add(int(row["title_b"]))
    observed = np.array([t in touched for t in targets])

    async with conn.transaction():
        await _write_state(
            conn,
            user_id=user_id,
            kind=kind,
            title_ids=np.asarray(targets, dtype=np.int64),
            s=s,
            sigma=sigma,
            sigma_prior=sigma_prior,
            sigma_eff=sigma_eff,
            cdf=cdf,
            tier=tier,
            straddle=straddle,
            observed=observed,
            stamps=[now if o else None for o in observed],
            fit_source="incremental",
        )
        _merge_cache(cache, targets, r_local[block], sigma, sigma_prior,
                     anchor[block], duel_curv[block], observed)
        await conn.execute(
            """
            UPDATE ledger_fit
               SET title_ids = $3, residuals = $4, sigma = $5, sigma_prior = $6,
                   anchor_curv = $7, duel_curv = $8, n_observed = $9,
                   fit_source = 'incremental'
             WHERE user_id = $1 AND kind = $2
            """,
            user_id,
            kind,
            _npy(cache.title_ids.astype(np.int32)),
            _npy(cache.r),
            _npy(cache.sigma),
            _npy(cache.sigma_prior),
            _npy(cache.anchor_curv),
            _npy(cache.duel_curv),
            int(cache.n_observed),
        )

    rows = tuple(
        Row(
            title_id=t,
            s=float(s[i]),
            sigma=float(sigma[i]),
            sigma_eff=float(sigma_eff[i]),
            cdf=None if not np.isfinite(cdf[i]) else float(cdf[i]),
            tier=int(tier[i]),
            straddle=None if int(straddle[i]) < 0 else int(straddle[i]),
            observed=bool(observed[i]),
        )
        for i, t in enumerate(targets)
    )
    return Delta(
        user_id=user_id,
        kind=kind,
        rows=rows,
        fit_source="incremental",
        iterations=iterations,
        micros=int((time.perf_counter() - started) * 1e6),
    )


def _merge_cache(
    cache: FitCache,
    targets: Sequence[int],
    r: np.ndarray,
    sigma: np.ndarray,
    sigma_prior: np.ndarray,
    anchor: np.ndarray,
    duel: np.ndarray,
    observed: np.ndarray,
) -> None:
    """Fold the block's new values back into the cached arrays, extending them for a title the
    person has just rated for the first time.

    A new title is *inserted*, not appended. 0010 says `ledger_fit.title_ids` is "int32[n],
    ascending — the row order of everything below", and `load_observations` returns its titles
    sorted, so appending would re-key six blobs against a seventh and every subsequent read of
    the cache would be one title out of step in a way nothing would raise on.
    """
    at = {int(t): i for i, t in enumerate(cache.title_ids)}
    for j, title_id in enumerate(targets):
        if title_id in at:
            i = at[title_id]
            cache.r[i] = r[j]
            cache.sigma[i] = sigma[j]
            cache.sigma_prior[i] = sigma_prior[j]
            cache.anchor_curv[i] = anchor[j]
            cache.duel_curv[i] = duel[j]
            continue
        if not observed[j]:
            continue
        pos = int(np.searchsorted(cache.title_ids, title_id))
        cache.title_ids = np.insert(cache.title_ids, pos, np.int64(title_id))
        cache.r = np.insert(cache.r, pos, r[j])
        cache.sigma = np.insert(cache.sigma, pos, sigma[j])
        cache.sigma_prior = np.insert(cache.sigma_prior, pos, sigma_prior[j])
        cache.anchor_curv = np.insert(cache.anchor_curv, pos, anchor[j])
        cache.duel_curv = np.insert(cache.duel_curv, pos, duel[j])
        cache.n_observed += 1


async def _read_rows(
    conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int]
) -> tuple[Row, ...]:
    found = await conn.fetch(
        """
        SELECT title_id, s, sigma, sigma_eff, cdf, tier, straddle, observed
        FROM ledger_state WHERE user_id = $1 AND title_id = ANY($2::int[])
        """,
        user_id,
        [int(t) for t in title_ids],
    )
    by_id = {
        int(r["title_id"]): Row(
            title_id=int(r["title_id"]),
            s=float(r["s"]),
            sigma=float(r["sigma"]),
            sigma_eff=float(r["sigma_eff"]) if r["sigma_eff"] is not None else float(r["sigma"]),
            cdf=float(r["cdf"]) if r["cdf"] is not None else None,
            tier=int(r["tier"]) if r["tier"] is not None else None,
            straddle=int(r["straddle"]) if r["straddle"] is not None else None,
            observed=bool(r["observed"]),
        )
        for r in found
    }
    return tuple(by_id[int(t)] for t in title_ids if int(t) in by_id)


async def read_board(
    conn: asyncpg.Connection, *, user_id: int, kind: str, limit: int = 100, offset: int = 0
) -> list[Row]:
    """§4.1 rule 5's ranked read, straight off `ledger_state_rank (user_id, kind, s DESC)`."""
    rows = await conn.fetch(
        """
        SELECT title_id, s, sigma, sigma_eff, cdf, tier, straddle, observed
        FROM ledger_state WHERE user_id = $1 AND kind = $2
        ORDER BY s DESC, title_id LIMIT $3 OFFSET $4
        """,
        user_id,
        kind,
        limit,
        offset,
    )
    return [
        Row(
            title_id=int(r["title_id"]),
            s=float(r["s"]),
            sigma=float(r["sigma"]),
            sigma_eff=float(r["sigma_eff"]) if r["sigma_eff"] is not None else float(r["sigma"]),
            cdf=float(r["cdf"]) if r["cdf"] is not None else None,
            tier=int(r["tier"]) if r["tier"] is not None else None,
            straddle=int(r["straddle"]) if r["straddle"] is not None else None,
            observed=bool(r["observed"]),
        )
        for r in rows
    ]


__all__ = [
    "DAYS_PER_MONTH",
    "Delta",
    "FitCache",
    "RefitRefused",
    "RefitReport",
    "Row",
    "active_bundle_version",
    "load_cache",
    "read_board",
    "refit_all",
    "refit_user",
    "update_incrementally",
]
