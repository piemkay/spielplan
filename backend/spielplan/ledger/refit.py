"""The Ledger's two jobs (§5.3): `refit_user` (full MAP, seconds) and `update_incrementally` (<50 ms).

One model at two resolutions: the incremental path minimises the same F over the touched residuals.
Postgres sorts NaN above every number, so nothing here ever writes a non-finite `s` or `σ`.
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

from spielplan.db.library import household_ids
from spielplan.ledger import model, observations
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.model import EMBED_DIM, ObservationSet
from spielplan.ledger.observations import EmbeddingSource, zero_embeddings
from spielplan.models import artifacts
from spielplan.scoring.backbone import COORDINATE_GEOMETRY

log = logging.getLogger("spielplan.ledger.refit")

# The mean Gregorian month in days; a calendar conversion, not a tuning constant.
DAYS_PER_MONTH = 365.2425 / 12.0

# The coordinate's reading (decision 471) plus the tier scale (decision 508): a fit stamped
# otherwise is refused by `load_cache` and refitted by the 60 s tick.
LEDGER_GEOMETRY = f"{COORDINATE_GEOMETRY}+verdict-scale"


# `Delta.fit_source` when no fit ran: the observation is durable and a sweep owes the board a fit.
QUEUED = "queued"

# The namespace half of the (int, int) advisory lock, a key space separate from `write_txn`'s bigint.
LEDGER_LOCK = "spielplan.ledger"

# Newly rated titles on top of a full fit before the `ledger-refresh` tick runs another. The
# incremental path never moves v, so unrated titles keep the last full fit's answer until then.
REFRESH_GROWTH = 5

# Rendered as the tail of the clients' "ledger update refused - ..." line.
QUEUED_REASON = "a full refit is queued; there was no usable cached fit to update"

# Where the full fit logs its size: `model.fit`'s dense inverse is cubic in the observed count.
LOUD_FIT_TITLES = 2000


class _BasisUnstated:
    """"The caller did not say which basis it holds"; `None` already means "no bundle" (§3.1)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid, not behaviour
        return "BASIS_UNSTATED"


BASIS_UNSTATED = _BasisUnstated()


class RefitRefused(Exception):
    """The fit produced something that must not reach `ledger_state`."""


# --- the numpy blobs ---------------------------------------------------------------------------


def _npy(array: np.ndarray) -> bytes:
    """`ledger_fit`'s columns are .npy payloads, so shape and dtype travel with the bytes."""
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
    observed: bool


@dataclass(frozen=True)
class Delta:
    """What one observation moved."""

    user_id: int
    kind: str
    rows: tuple[Row, ...]
    fit_source: str
    # True when the cache was unusable; the fit is then owed (`fit_source == QUEUED`), not run.
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
    """The basis the app is SERVING. Not the stamp for a fit: that is the basis it was computed in."""
    return await artifacts.active_bundle_version(conn)


async def load_cache(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    lock: bool = True,
    bundle_version: str | None | _BasisUnstated = BASIS_UNSTATED,
) -> FitCache | None:
    """The cached nightly fit, or None when it must not be trusted.

    Refused on another `hp_digest`, another active bundle, another basis than the caller holds
    (mid-import), another geometry, or another tier-set K (decision 11). `FOR UPDATE` serialises
    one person's taps.
    """
    row = await conn.fetchrow(
        # LEFT JOIN: a NULL `n_levels` means no K was ever stated, which is not a disagreement.
        """
        SELECT f.*, array_length(c.tier_set, 1) AS n_levels
          FROM ledger_fit f
          LEFT JOIN ledger_cutpoints c
                 ON c.user_id = f.user_id AND c.kind = f.kind
         WHERE f.user_id = $1 AND f.kind = $2
        """
        + (" FOR UPDATE OF f" if lock else ""),
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
    if not isinstance(bundle_version, _BasisUnstated) and row["bundle_version"] != bundle_version:
        # A warning: §10's restart is owed and somebody is still tapping.
        log.warning(
            "ledger_fit for user %d/%s is in bundle %r, this process holds %r - queueing a refit "
            "rather than mixing bases",
            user_id,
            kind,
            row["bundle_version"],
            bundle_version,
        )
        return None
    if row["geometry"] != LEDGER_GEOMETRY:
        log.info(
            "ledger_fit for user %d/%s was fitted to %r coordinates, the app reads %r - refitting",
            user_id,
            kind,
            row["geometry"],
            LEDGER_GEOMETRY,
        )
        return None
    mu, v, gamma, cuts, log_nu = _unpack_theta(_unnpy(row["theta"]))
    n_levels = row["n_levels"]
    if n_levels is not None and int(cuts.size) + 1 != int(n_levels):
        log.info(
            "ledger_fit for user %d/%s holds %d tier level(s), the tier set has %d - refitting",
            user_id,
            kind,
            int(cuts.size) + 1,
            int(n_levels),
        )
        return None
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
    """σ_prior² = τ_b² + [1, e] Σ_(μ,v) [1, e]ᵀ: the σ of a title never rated."""
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


async def _take_board_lock(conn: asyncpg.Connection, *, user_id: int, kind: str) -> None:
    """Serialise the full fit and a tap for one board (user, kind), until the transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1), ($2::bigint * 2 + ($3 = 'series')::int)::int)",
        LEDGER_LOCK,
        user_id,
        kind,
    )


async def refreshes_owed(
    conn: asyncpg.Connection, *, growth: int = REFRESH_GROWTH
) -> list[tuple[int, str, int]]:
    """The boards whose fit is older than the observations standing on it: (user, kind, grown).

    `grown` is newly rated TITLES (`n_observed` minus the last full fit's `cdf_reference` size), so
    re-ratings, battles and tier edits on rated titles do not trigger it.
    """
    # Another geometry is owed at once: `load_cache` refuses it, so no tap moves it until a refit.
    rows = await conn.fetch(
        "SELECT user_id, kind, n_observed, cdf_reference, geometry <> $1 AS regeometry "
        "  FROM ledger_fit "
        " WHERE fit_source = 'incremental' OR geometry <> $1 ORDER BY user_id, kind",
        LEDGER_GEOMETRY,
    )
    owed: list[tuple[int, str, int]] = []
    for row in rows:
        grown = int(row["n_observed"]) - int(_unnpy(row["cdf_reference"]).size)
        if grown >= growth or row["regeometry"]:
            owed.append((int(row["user_id"]), str(row["kind"]), grown))
    return owed


# --- the nightly job -----------------------------------------------------------------------------


async def refit_user(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: str | None = None,
    now: datetime | None = None,
) -> RefitReport:
    """§5.3's "Ledger full MAP refit + cutpoints + σ", for one (user, kind).

    `bundle_version` is the basis `embeddings` was built from, which the fit is stamped with; None
    means no bundle, never "look it up". Read and write are one transaction under the board lock,
    so a tap arriving mid-fit waits rather than being pruned.
    """
    async with conn.transaction():
        await _take_board_lock(conn, user_id=user_id, kind=kind)
        return await _refit_user(
            conn,
            user_id=user_id,
            kind=kind,
            hp=hp,
            embeddings=embeddings,
            bundle_version=bundle_version,
            now=now,
        )


async def _refit_user(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
    bundle_version: str | None,
    now: datetime | None,
) -> RefitReport:
    """`refit_user`'s body, with its transaction and its lock already held by the caller."""
    embeddings = embeddings or zero_embeddings
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    report = RefitReport(user_id=user_id, kind=kind, hyperparams_source=hp.source)

    loaded = await observations.load_observations(
        conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings, now=now
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
        # Nothing to fit (e.g. everything was undone): empty the board and the cache, and reset
        # the boundaries to the prior while keeping the tier set.
        async with conn.transaction():
            await _write_state(
                conn,
                user_id=user_id,
                kind=kind,
                title_ids=np.zeros(0, dtype=np.int64),
                s=np.zeros(0),
                sigma=np.zeros(0),
                sigma_prior=np.zeros(0),
                sigma_eff=np.zeros(0),
                cdf=np.zeros(0),
                tier=np.zeros(0, dtype=np.int64),
                observed=np.zeros(0, dtype=bool),
                stamps=[],
                fit_source="nightly",
                prune=True,
            )
            await conn.execute(
                "DELETE FROM ledger_fit WHERE user_id = $1 AND kind = $2", user_id, kind
            )
            await conn.execute(
                "UPDATE ledger_cutpoints SET boundaries = $3::float8[], updated_at = now() "
                "WHERE user_id = $1 AND kind = $2",
                user_id,
                kind,
                [float(c) for c in model.initial_cutpoints(len(loaded.tier_set))],
            )
        report.seconds = time.perf_counter() - started
        return report

    if obs.n > LOUD_FIT_TITLES:
        log.info("full MAP fit for user %d/%s over %d observed titles", user_id, kind, obs.n)

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
    report.iterations = fit.iterations
    report.backtracks = fit.backtracks
    report.cutpoints = [float(c) for c in fit.cuts]

    # ---- the rest of the library: s = μ + ⟨v, e⟩ for every unobserved owned title --------
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

    # §5.2's freshness rule, on σ_eff only; `ledger_state.sigma` keeps the fitted value.
    sigma_eff = model.inflate_sigma(sigma, sigma_prior, _months_since(stamps, now), hp)

    # The CDF reference is the OBSERVED block, so library growth moves no displayed number.
    cdf = model.empirical_cdf(fit.s, s)
    tier = model.tier_of(s, fit.cuts)
    # Decision 508's hold; unrated titles are padded with -1.
    held = np.concatenate([model.live_verdicts(obs), np.full(len(extra), -1, dtype=np.int64)])
    tier, _ = model.hold_to_verdict(tier, np.full_like(tier, -1), held, fit.cuts.size + 1)
    tier[obs.n :] = model.guess_tier(tier[obs.n :], fit.cuts.size + 1)

    finite = np.isfinite(s) & np.isfinite(sigma) & np.isfinite(sigma_eff)
    report.rejected_nonfinite = int((~finite).sum())
    if report.rejected_nonfinite:
        log.error(
            "user %d/%s: %d title(s) produced a non-finite s or σ and were not written",
            user_id,
            kind,
            report.rejected_nonfinite,
        )

    # NOT `active_bundle_version(conn)`: during §10's pre-flip rebuild the active row is the old basis.
    bundle = bundle_version
    async with conn.transaction():
        # Compare-and-set on the tier set: a PUT that landed mid-fit wins, and its queued refit
        # fits the new set.
        current = await conn.fetchval(
            "SELECT tier_set FROM ledger_cutpoints WHERE user_id = $1 AND kind = $2 FOR UPDATE",
            user_id,
            kind,
        )
        if current is not None and tuple(current) != tuple(loaded.tier_set):
            raise RefitRefused(
                f"user {user_id}/{kind}: the tier set changed to "
                f"{len(current)} levels while this fit was running against "
                f"{len(loaded.tier_set)}; ledger_state and ledger_cutpoints keep their "
                "previous values and the refit stays owed"
            )
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
            observed=observed[finite],
            stamps=[st for st, keep in zip(stamps, finite, strict=True) if keep],
            fit_source="nightly",
            prune=True,
        )
        # §5.2: the tier arm's cutpoints ARE the displayed boundaries.
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
    bundle_version: str | None = None,
    now: datetime | None = None,
) -> list[RefitReport]:
    """§5.3's nightly row, over the household. One person's bad fit must not stop the others'.

    `bundle_version` and `embeddings` must describe the same bundle.
    """
    reports: list[RefitReport] = []
    for user_id in await household_ids(conn):
        for kind in observations.KINDS:
            try:
                reports.append(
                    await refit_user(
                        conn,
                        user_id=user_id,
                        kind=kind,
                        hp=hp,
                        embeddings=embeddings,
                        bundle_version=bundle_version,
                        now=now,
                    )
                )
            except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError):
                # A dead connection fails every remaining fit; raise so `_tick` records a failure.
                raise
            except Exception as exc:
                # Any failure is isolated; the transaction inside `refit_user` rolls it back.
                # CancelledError is a BaseException and still passes, so `_tick` can abandon the job.
                log.exception("refit failed for user %s/%s", user_id, kind)
                reports.append(
                    RefitReport(user_id=user_id, kind=kind, error=str(exc))
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
    observed: np.ndarray,
    stamps: Sequence[datetime | None],
    fit_source: str,
    prune: bool = False,
) -> None:
    """One `unnest` statement for the whole board, not a round trip per title."""
    if title_ids.size == 0:
        if prune:
            await conn.execute(
                "DELETE FROM ledger_state WHERE user_id = $1 AND kind = $2", user_id, kind
            )
        return
    ids = [int(t) for t in title_ids]
    # The CDF is undefined below two observed titles; NULL, never NaN (which sorts above all).
    cdf_out = [None if not np.isfinite(c) else float(c) for c in cdf]
    await conn.execute(
        """
        INSERT INTO ledger_state
            (user_id, title_id, kind, s, sigma, sigma_prior, sigma_eff, cdf, tier,
             observed, last_observed_at, fit_source, updated_at)
        SELECT $1, x.title_id, $2, x.s, x.sigma, x.sigma_prior, x.sigma_eff, x.cdf, x.tier,
               x.observed, x.last_observed_at, $3, now()
        FROM unnest($4::int[], $5::float8[], $6::float8[], $7::float8[], $8::float8[],
                    $9::float8[], $10::smallint[], $11::boolean[], $12::timestamptz[])
             AS x(title_id, s, sigma, sigma_prior, sigma_eff, cdf, tier, observed,
                  last_observed_at)
        ON CONFLICT (user_id, title_id) DO UPDATE
          SET kind = EXCLUDED.kind, s = EXCLUDED.s, sigma = EXCLUDED.sigma,
              sigma_prior = EXCLUDED.sigma_prior, sigma_eff = EXCLUDED.sigma_eff,
              cdf = EXCLUDED.cdf, tier = EXCLUDED.tier,
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
        [bool(x) for x in observed],
        list(stamps),
    )
    if prune:
        # Scoped to this kind, so the two partitions never delete each other's rows.
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
             fit_source, objective, grad_inf, converged, fitted_at, geometry)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19, now(), $20)
        ON CONFLICT (user_id, kind) DO UPDATE SET
            theta = EXCLUDED.theta, title_ids = EXCLUDED.title_ids,
            residuals = EXCLUDED.residuals, sigma = EXCLUDED.sigma,
            sigma_prior = EXCLUDED.sigma_prior, anchor_curv = EXCLUDED.anchor_curv,
            duel_curv = EXCLUDED.duel_curv, cdf_reference = EXCLUDED.cdf_reference,
            z_cov = EXCLUDED.z_cov, n_observed = EXCLUDED.n_observed,
            hp_digest = EXCLUDED.hp_digest, hp_source = EXCLUDED.hp_source,
            bundle_version = EXCLUDED.bundle_version, fit_source = EXCLUDED.fit_source,
            objective = EXCLUDED.objective, grad_inf = EXCLUDED.grad_inf,
            converged = EXCLUDED.converged, fitted_at = now(), geometry = EXCLUDED.geometry
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
        LEDGER_GEOMETRY,
    )


# --- the <50 ms path -------------------------------------------------------------------------


async def _load_local(
    conn: asyncpg.Connection, *, user_id: int, kind: str, title_ids: Sequence[int], hp: Hyperparams
) -> tuple[list[Any], list[Any], list[Any], float]:
    """Only the rows that touch these titles, plus the GLOBAL mean margin the weighting divides by.

    The same rows `load_observations` reads, so this path agrees with the full fit.
    """
    ids = list(title_ids)
    cutover = observations.cutover_sql()
    verdicts = await conn.fetch(
        f"""
        SELECT v.title_id, v.value, v.created_at FROM verdict v JOIN title t ON t.id = v.title_id
        WHERE v.user_id = $1 AND t.kind = $2 AND NOT v.is_reask AND v.title_id = ANY($3::int[])
          AND {observations.NOT_SET_UP_SQL}
        ORDER BY v.id
        """,
        user_id,
        kind,
        ids,
    )
    tier_edits = await conn.fetch(
        f"""
        SELECT e.title_id, e.tier, e.n_levels, e.created_at
        FROM tier_edit e JOIN title t ON t.id = e.title_id
        WHERE e.user_id = $1 AND t.kind = $2 AND e.title_id = ANY($3::int[])
          AND e.created_at >= {cutover} AND e.via <> 'sharpen' AND NOT {observations.SAME_ANSWER_SQL}
        ORDER BY e.id
        """,
        user_id,
        kind,
        ids,
    )
    duels = await conn.fetch(
        f"""
        SELECT d.title_a, d.title_b, d.outcome, d.margin, d.created_at
        FROM duel d JOIN title ta ON ta.id = d.title_a JOIN title tb ON tb.id = d.title_b
        WHERE d.user_id = $1 AND ta.kind = $2 AND tb.kind = $2
          AND d.selection <> $4 AND NOT d.is_reask AND d.created_at >= {cutover}
          AND (d.title_a = ANY($3::int[]) OR d.title_b = ANY($3::int[]))
        ORDER BY d.id
        """,
        user_id,
        kind,
        ids,
        observations.HELD_OUT,
    )
    # Key lookups, not joins: on tables not yet analysed the planner rescans the kind's titles per duel.
    mean_margin = await conn.fetchval(
        f"""
        SELECT avg(coalesce(d.margin, $3::float8))
        FROM duel d
        WHERE d.user_id = $1 AND d.selection <> $4 AND NOT d.is_reask AND d.created_at >= {cutover}
          AND (SELECT ta.kind FROM title ta WHERE ta.id = d.title_a) = $2
          AND (SELECT tb.kind FROM title tb WHERE tb.id = d.title_b) = $2
        """,
        user_id,
        kind,
        hp.margin_hesitant,
        observations.HELD_OUT,
    )
    return list(verdicts), list(tier_edits), list(duels), float(mean_margin or hp.margin_hesitant)


def _local_hp(hp: Hyperparams, local_margins: np.ndarray, mean_margin: float) -> Hyperparams:
    """Scale λ_bt by mean_local/mean_global so `model`'s own weighting reproduces the nightly one."""
    if local_margins.size == 0:
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
    """Newton on F restricted to `r[block]`, everything else frozen.

    Uses `model`'s private derivatives on purpose, so both paths minimise the same F.
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
    # Re-evaluated: σ needs the curvature at the answer, and the loop breaks after moving r.
    *_, anchor, duel, _coupling = model._grad_hess(obs, hp, *args, r, with_duels=with_duels)
    return r, iterations, anchor, duel


def _sherman_morrison(
    sigma: np.ndarray, curvature_delta: np.ndarray, sigma_prior: np.ndarray
) -> np.ndarray:
    """σ after a rank-1 change of curvature: (H + q·eᵢeᵢᵀ)⁻¹ᵢᵢ = H⁻¹ᵢᵢ / (1 + q·H⁻¹ᵢᵢ).

    Approximate for duels (Cov(sₐ, s_b) is taken as zero); the nightly refit is exact. Capped at
    σ_prior, since an undo can make the denominator small or negative.
    """
    denominator = 1.0 + curvature_delta * sigma**2
    with np.errstate(divide="ignore", invalid="ignore"):
        updated = np.where(denominator > 1e-12, sigma**2 / denominator, np.inf)
    return np.sqrt(np.clip(updated, 1e-12, sigma_prior**2))


async def update_incrementally_reporting(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: Sequence[int],
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
    bundle_version: str | None | _BasisUnstated = BASIS_UNSTATED,
) -> dict[str, Any] | None:
    """§5.3's "<50 ms" row after the write has committed: reports a refusal rather than raising."""
    try:
        delta = await update_incrementally(
            conn,
            user_id=user_id,
            kind=kind,
            title_ids=list(title_ids),
            hp=hp,
            embeddings=embeddings,
            bundle_version=bundle_version,
        )
    except (RefitRefused, ValueError) as exc:
        log.warning("incremental update refused for user %d/%s: %s", user_id, kind, exc)
        return {"applied": False, "reason": str(exc)}
    if delta.fit_source == QUEUED:
        # A queued fit is not an applied one; §6.7's rail must not report a refit nobody ran.
        return {"applied": False, "reason": QUEUED_REASON}
    return {
        "applied": True,
        "kind": delta.kind,
        "refit": delta.refit,
        "ms": round(delta.micros / 1000.0, 1),
        "rows": [{"title_id": r.title_id, "cdf": r.cdf, "tier": r.tier} for r in delta.rows],
    }


async def update_incrementally(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: Sequence[int],
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    now: datetime | None = None,
    bundle_version: str | None | _BasisUnstated = BASIS_UNSTATED,
) -> Delta:
    """§5.3's "<50 ms" row: re-place the one or two titles an observation touched.

    Re-reads their observations, so it serves a write and an undo alike. The caller guarantees
    every title is of `kind`, and names the basis `embeddings` is in via `bundle_version`.
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
            bundle_version=bundle_version,
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
    bundle_version: str | None | _BasisUnstated = BASIS_UNSTATED,
) -> Delta:
    embeddings = embeddings or zero_embeddings
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    targets = [int(t) for t in dict.fromkeys(title_ids)]

    # Before `load_cache`'s FOR UPDATE, or a tap and a running fit deadlock on the `ledger_fit` row.
    await _take_board_lock(conn, user_id=user_id, kind=kind)
    # After the lock: what the tap waited out may have been the import that flipped the basis.
    cache = await load_cache(
        conn, user_id=user_id, kind=kind, hp=hp, bundle_version=bundle_version
    )
    if cache is None:
        # Queue the full fit rather than run §5.3's "seconds" row inside the "<50 ms" one.
        await _queue_full_refit(conn, user_id=user_id, kind=kind)
        return Delta(
            user_id=user_id,
            kind=kind,
            rows=(),
            fit_source=QUEUED,
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
    ord_at = [row["created_at"] for row in verdicts] + [row["created_at"] for row in tier_edits]
    for row in verdicts:
        ord_index.append(position[int(row["title_id"])])
        ord_level.append(int(row["value"]))
        ord_arm.append(observations.ARM_VERDICT)
    n_levels = cache.n_levels
    for row in tier_edits:
        ord_index.append(position[int(row["title_id"])])
        # Not a no-op: an edit written under an older K still flows through after a refit at the new K.
        ord_level.append(
            observations.rescale_level(
                int(row["tier"]), k_from=row["n_levels"], k_to=n_levels
            )
        )
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
        ord_weight=observations.recency_weight(ord_at, now, hp),
        duel_a=np.asarray([position[int(r["title_a"])] for r in duels], dtype=np.int64),
        duel_b=np.asarray([position[int(r["title_b"])] for r in duels], dtype=np.int64),
        duel_outcome=np.asarray(
            [observations.OUTCOMES[r["outcome"]] for r in duels], dtype=np.int64
        ),
        duel_margin=margins,
        duel_weight=observations.recency_weight([r["created_at"] for r in duels], now, hp),
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
    # A never-rated title starts from σ_prior and the residual prior's curvature.
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
        # No NaN reaches `ledger_state`; the exact fit is queued instead.
        log.error(
            "incremental update for user %d/%s went non-finite - full refit queued", user_id, kind
        )
        await _queue_full_refit(conn, user_id=user_id, kind=kind)
        return Delta(
            user_id=user_id,
            kind=kind,
            rows=(),
            fit_source=QUEUED,
            refit=True,
            micros=int((time.perf_counter() - started) * 1e6),
        )

    # §5.2's freshness clock is the newest OBSERVATION, not the wall clock: after an undo, what
    # remains may be an old verdict.
    last_at: dict[int, datetime] = {}

    def _touch(title_id: int, at: datetime) -> None:
        current = last_at.get(title_id)
        if current is None or at > current:
            last_at[title_id] = at

    for row in verdicts:
        _touch(int(row["title_id"]), row["created_at"])
    for row in tier_edits:
        _touch(int(row["title_id"]), row["created_at"])
    for row in duels:
        _touch(int(row["title_a"]), row["created_at"])
        _touch(int(row["title_b"]), row["created_at"])
    observed = np.array([t in last_at for t in targets])
    stamps: list[datetime | None] = [last_at.get(t) for t in targets]

    sigma_eff = model.inflate_sigma(sigma, sigma_prior, _months_since(stamps, now), hp)
    # Against the cached reference: the same definition, one night stale.
    cdf = model.empirical_cdf(cache.cdf_reference, s)
    tier = model.tier_of(s, cache.cuts)
    # Decision 508's hold, as the nightly applies it.
    tier, _ = model.hold_to_verdict(
        tier, np.full_like(tier, -1), model.live_verdicts(local)[block], cache.n_levels
    )
    # A title whose last observation was undone is unrated again (decision 510).
    tier = np.where(observed, tier, model.guess_tier(tier, cache.n_levels))

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
            observed=observed,
            stamps=stamps,
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


async def _queue_full_refit(conn: asyncpg.Connection, *, user_id: int, kind: str) -> None:
    """Ask the 60 s sweep for the fit this request is not going to run.

    An upsert, because the very first tap has no `ledger_cutpoints` row; it gets the prior shape.
    """
    await conn.execute(
        """
        INSERT INTO ledger_cutpoints
            (user_id, kind, boundaries, tier_set, refit_requested_at, updated_at)
        VALUES ($1, $2, $3::float8[], $4::text[], now(), now())
        ON CONFLICT (user_id, kind) DO UPDATE
           SET refit_requested_at = COALESCE(
                   ledger_cutpoints.refit_requested_at, EXCLUDED.refit_requested_at
               )
        """,
        user_id,
        kind,
        [float(c) for c in model.initial_cutpoints(len(observations.DEFAULT_TIER_SET))],
        list(observations.DEFAULT_TIER_SET),
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
    """Fold the block's new values into the cache. A new title is INSERTED: `title_ids` is ascending."""
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


__all__ = [
    "BASIS_UNSTATED",
    "DAYS_PER_MONTH",
    "LOUD_FIT_TITLES",
    "QUEUED",
    "Delta",
    "FitCache",
    "RefitRefused",
    "RefitReport",
    "Row",
    "active_bundle_version",
    "load_cache",
    "refit_all",
    "refit_user",
    "update_incrementally",
    "update_incrementally_reporting",
]
