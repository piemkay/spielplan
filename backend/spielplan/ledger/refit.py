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
path queues a full refit rather than persisting one.

HOW OFTEN THE NIGHTLY ROW ACTUALLY RUNS. §5.2's two cadences are "nightly" and "on each new
observation", and the second one moves r and not v: `_update_incrementally` re-solves the touched
titles' residuals against the cached fit's (mu, v), so every title the person has not rated keeps
whatever the last FULL fit said about it. After M4.10 that fit is the one over the very first
verdict, so a whole evening's board can rest on n = 1 and the nightly then moves hundreds of tier
badges at once - the snap §6.3 says the design avoids, delivered by the job that exists to prevent
it. `refreshes_owed` and the worker's `ledger-refresh` tick are the answer: the same full fit, asked
for more often than §5.3's table asks, which is a superset of the cadence and not a change to it.

WHAT THE REQUEST PATH IS ALLOWED TO COST. §5.3 budgets the incremental row at "<50 ms" and the
full fit at "seconds", so the <50 ms path must never reach for the seconds one. It used to, on
every cache miss: the fit was run inline, measured at 0.39 s over 300 titles and 33.4 s over
4000, on the backend event loop, for an observation the caller had already committed. A miss now
stamps `ledger_cutpoints.refit_requested_at` and returns — the column and the 60 s sweep exist
from 0012 and decision 11 — so the fit happens where its budget lives. The cache-miss *rate* is
a different defect in a different theme (the refit stamped with the active bundle version rather
than the one it was computed against); this module makes a miss cheap, not rare.
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

log = logging.getLogger("spielplan.ledger.refit")

# A calendar conversion, not a tuning constant: the mean Gregorian month in days. §5.2's "12
# months untouched" and "rate c per √month" both need months, and `hp` owns the 12 and the c.
DAYS_PER_MONTH = 365.2425 / 12.0


# `Delta.fit_source` when no fit ran at all: the observation is durable, the board has not
# moved, and a sweep owes it a fit. Named rather than spelled "nightly", because "nightly" is a
# claim that the rows in the delta came from one.
QUEUED = "queued"

# The namespace half of this module's advisory lock. A *name* hashed by the server rather than a
# literal, because the name is what the reader of either call site needs; it never travels, since
# both the fit and the tap that waits on it compute `hashtext` inside the same cluster.
#
# A different lock space from `api/deps.py`'s `write_txn`, whose single-argument
# `hashtext(...)::bigint` form Postgres keeps separate from the (int, int) pairs the domain
# packages take (`tonight/play.py`'s `_FINISH_LOCK`) — so a name there cannot collide with a number
# here, which is exactly what `write_txn`'s docstring says of the trade. [M4.13, data-05]
LEDGER_LOCK = "spielplan.ledger"

# How much work has to have accumulated on top of a full fit before the `ledger-refresh` tick runs
# another. Five, because §5.2's incremental row is not a second model — it re-solves the touched
# titles' residuals against the last full fit's (mu, v) — so the rest of the library keeps whatever
# that fit said about it. Measured after 50 verdicts and ~41 battles per member on the real bundle:
# the 715 unrated owned movies had sd(s) 0.070 against 2.77-3.27 after a full refit and their order
# correlated -0.135 with a taste the full refit recovers at +0.57. The nightly then moved 252 and
# 724 of 765 tier badges at once, which is the snap §6.3 says the design avoids. Against that, a
# full fit costs 0.11-0.14 s per (user, kind) at this scale, so five is chosen low: it is about one
# §6.1 block's worth of taps, which is the grain a person experiences the board at -- of the taps
# this predicate can SEE, which is the qualification `refreshes_owed` states below: a §6.1 block
# alternates sweep and battle (`rate/session.py`), and only the sweep half lands on a title the
# person has not rated before. [M4.13, dd22; cycle 2, M413-D6-03]
REFRESH_GROWTH = 5

# What the routers report when the delta carries no fit. It has to survive being read as the
# tail of the clients' refused line ("ledger update refused - ..."), so it says what is true of
# both queueing branches: the cached fit was missing or came back non-finite, and a full one is
# owed to the 60 s sweep rather than lost.
# [M4.10 cycle 2, m410-c2-cache-miss-reports-a-refit-that-did-not-run]
QUEUED_REASON = "a full refit is queued; there was no usable cached fit to update"

# Where the full fit stops being quiet. §5.3's "seconds" is a budget, not a promise, and
# `model.fit`'s dense inverse is cubic in the observed count.
LOUD_FIT_TITLES = 2000


class _BasisUnstated:
    """"The caller did not say which basis it holds", which `None` cannot mean.

    §3.1's bundle-less install has no bundle, and `None` is its honest answer — `assert_matches`
    passes on `None == None` for exactly that reason. So the absence of a claim needs a value of
    its own, or the two would be one, and every caller that simply does not know (a unit test
    handing `zero_embeddings`, `rate/session.py`'s preview read) would be refused as though it
    were serving a bundle-less install against a fitted one. [M4.13 cycle 1, finding 15]
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid, not behaviour
        return "BASIS_UNSTATED"


BASIS_UNSTATED = _BasisUnstated()


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
    # True when the cache was cold, stale or unusable — whatever happened here, it was not the
    # <50 ms path, and reporting a miss as an incremental update would be the one way to make
    # §5.3's budget unfalsifiable. Since finding 9 it no longer implies the whole fit was redone
    # in this call: `fit_source` is what separates a fit that ran from one that is owed, and the
    # only two sites that set this flag now set `QUEUED` beside it.
    # [M4.10 cycle 2, m410-c2-cache-miss-reports-a-refit-that-did-not-run]
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
    """Delegates to `models.artifacts.active_bundle_version`, which is now the one resolver.

    Kept as a name because `load_cache` below, `api/home.py` and this module's own callers all
    reach the question through it, and because the two uses of "the active bundle version" inside
    this file answer DIFFERENT questions that happened to share a call: the stamp on a fit must
    describe the basis the fit was computed IN (threaded by the caller), while the comparison in
    `load_cache` must describe the basis the app is SERVING (this row). Conflating them is data-01.
    [M4.13, arch-03]
    """
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

    THREE preconditions, and all three are correctness rather than freshness. §4.3: "every
    constant comes from `ledger_hyperparams.json`" — a cache built under other constants is
    *wrong*, so `hp_digest` is compared, not logged. §10: "everything expressed in the old
    Backbone's basis is garbage against a new one" — so a fit whose bundle is no longer the active
    one is not stale either, and returning it would put a title's coordinate in a basis nobody
    uses. And K: the cut-points in this blob index a tier set of a particular length, so a fit
    whose K no longer matches `ledger_cutpoints.tier_set` is not merely out of date, it MEANS
    something else.

    §10'S HALF IS TWO QUESTIONS AND USED TO BE ONE. The active row says which basis the household
    SERVES; `bundle_version` says which basis the caller is holding, and those diverge for the
    whole of an import. `importer/bundle.py` runs the §10 rebuild and the flip inside one
    transaction, and `refit_user` takes the board's `pg_advisory_xact_lock` inside it — so a tap
    that arrives mid-import blocks on that lock until the flip COMMITS, and wakes to find the
    staged stamps and the new active row visible in the same instant. It had passed
    `_assert_active_basis` before the wait; this process still holds the outgoing Backbone until
    §10's restart. Comparing the stamp to the active row alone, that cache came back accepted and
    the tap solved a residual with outgoing coordinates against a v expressed in the incoming
    basis — data-01's mixed-basis write, arriving through the lock step 24 added. Measured on the
    fixture at |Δs| = 0.038 for one title, and inherited: the accepted branch's UPDATE names eight
    columns and `bundle_version` is not one of them, so the polluted residual sits under the
    incoming stamp until the next full refit. So the caller's basis is compared too, and a
    disagreement takes the same branch a missing cache takes — `_queue_full_refit` and QUEUED,
    which loses no tap. `BASIS_UNSTATED` for a caller that does not know one, which is not the
    same as `None`. [M4.13 cycle 1, finding 15; §10, data-01]

    K is the one of the three that had no guard, and decision 11 is why it needs one: a tier-set
    change keeps the `tier_edit` rows and queues a refit, so between the PUT and the 60 s sweep
    every drop went through `_update_incrementally` at `cache.n_levels` from the OLD set.
    Reproduced twice: growing 7 -> 12, a drop into tier 7 of 12 was clamped to 6 of 7 and written
    as `ledger_state.tier = 4` while the displayed K = 12 boundaries give 8 - Home showing T4 and
    Rank T7 for one title; shrinking to 3, a drop to the TOP of 3 was fitted as level 2 of 7 and s
    fell from 0.3588 to -0.1133 where a correct K = 3 refit gives 1.1933. Refused HERE and not by a
    `DELETE FROM ledger_fit` inside `save_tier_set`, deliberately and not both: one statement of a
    precondition beside the two it belongs with, in the function that already has to read the tier
    set, rather than a second write on the settings path that a future caller of `save_tier_set`
    could forget. [M4.13, ml01; decision 11]

    A miss is cheap by design: M4.10 took the full refit off the tap path, so the caller queues it
    (`_queue_full_refit`) and returns `fit_source = QUEUED`. Nothing here puts §5.3's "seconds" row
    back inside its "<50 ms" one.

    `FOR UPDATE` because two observations for one person must serialise: §6.1's block counter
    and decision 35's Undo depth are both defined over a sequence, not over a race.
    """
    row = await conn.fetchrow(
        # The tier set comes from the same round trip as the fit, and by LEFT JOIN because a fit
        # can exist with no cut-point row at all (a bundle-less first fit writes `ledger_fit`
        # before anyone has saved a set): a NULL `n_levels` means "nobody has stated a K", which
        # is not a disagreement and must not invalidate the cache.
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
        # A WARNING and not an info: the row above it is the ordinary post-import state every
        # household passes through, while this one means a caller woke up in a world whose active
        # row it no longer matches — §10's restart is owed and somebody is still tapping.
        log.warning(
            "ledger_fit for user %d/%s is in bundle %r, this process holds %r - queueing a refit "
            "rather than mixing bases",
            user_id,
            kind,
            row["bundle_version"],
            bundle_version,
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


async def _take_board_lock(conn: asyncpg.Connection, *, user_id: int, kind: str) -> None:
    """Serialise the two jobs of this module against each other, for one board.

    One lock per BOARD and not per person: §4.1 rule 5 keeps the two kinds apart everywhere else,
    and somebody rating films while their series fit runs has nothing to wait for. `kind` enters the
    key as a bit, so the pair is (namespace, user_id * 2 + is_series) and no two boards share a
    number.

    `pg_advisory_xact_lock` rather than a row lock, because the thing being serialised is not a row:
    it is "the whole of one fit against the whole of one tap", which spans `load_observations`,
    seconds of numpy and three writes. It is released with the transaction whichever way that ends,
    so a refused fit cannot leave the board locked. [M4.13, data-05; plan step 24]
    """
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

    §5.2 says "refit nightly (full-history MAP; seconds at this scale) and incrementally on each
    new observation", and the two halves are "the same model at two resolutions" (this module's
    own opening). What the shipped incremental path cannot do at any resolution is move `v`: it
    re-solves the touched titles' residuals against the cached fit's (mu, v), so every title the
    person has NOT rated keeps the estimate the first full fit made — which, on the real path after
    M4.10, is the fit over the single first verdict. `REFRESH_GROWTH` carries the measurement.

    A tick rather than a nightly-only pass is the same argument `scoring.foldin.run` already makes
    for `user_score`: running a spec cadence more often than the spec asks is a superset of what
    §5.3 requires, and §12's M2 exit criterion is a claim about what a person sees *within* a
    sitting. The full fit is the one §5.3 budgets at "seconds"; this only changes how often it is
    asked for.

    THE BASELINE IS ALREADY IN THE ROW, which is why this needs no column. §5.2 defines the CDF
    reference as "the empirical CDF of the user's own fitted s values", so `ledger_fit.cdf_reference`
    is the observed block AT THE LAST FULL FIT — and the incremental path does not rewrite it
    (`_update_incrementally`'s UPDATE names eight columns and this is not one of them, because the
    reference is "the same definition, one night stale" by design). `n_observed` is the live count,
    which `_merge_cache` grows per newly rated title. The difference is therefore exactly "how many
    titles the person has rated since the fit this board rests on", read off state that already
    means that.

    WHAT IT COUNTS IS NEW TITLES, NOT NEW OBSERVATIONS, and that is a hole in the trigger rather
    than in the implementation -- plan step 27 specifies "whose `n_observed` has grown by at least
    5 since the last full fit" and this is that, faithfully. Both sides of the subtraction count
    TITLES: `Observations.n` is `title_ids.size` and `_merge_cache` reaches `n_observed += 1` only
    on the branch that inserts a title the cache did not have. So three kinds of evening move `v`
    for the full fit and move this predicate not at all:

      * a §4.2 supersede -- `load_observations` deliberately loads "every verdict row, superseded
        and live alike", so a re-rating is an ADDITIONAL ordinal row in the likelihood;
      * a §6.1 battle, which by construction pairs two titles the person has already rated the
        same way, so it can never reach the insert branch -- and "battle" is a whole session mode,
        not an incidental card;
      * a tier edit on a title that is already observed.

    A member who spends an evening changing their mind and fighting duels therefore keeps the
    board the last full fit gave them until the nightly, which is the 24-hour snap §6.3 says the
    design avoids and the thing dd22 exists to close for the newly-rated arm. Widening the
    predicate means counting OBSERVATIONS (verdict + duel + tier_edit rows since `fitted_at`) or
    firing on `max(observation.created_at) > ledger_fit.fitted_at` the way `foldin._is_stale`
    already does -- a different trigger, and the owner's call rather than this cycle's. Recorded
    here, and asserted by
    `test_worker_jobs.py::test_a_sitting_of_re_ratings_and_battles_does_not_move_the_refresh_trigger`
    so the limit is a measured property and not a thing to be rediscovered.
    [M4.13, dd22; cycle 2, M413-D6-03]
    """
    rows = await conn.fetch(
        "SELECT user_id, kind, n_observed, cdf_reference FROM ledger_fit "
        " WHERE fit_source = 'incremental' ORDER BY user_id, kind"
    )
    owed: list[tuple[int, str, int]] = []
    for row in rows:
        grown = int(row["n_observed"]) - int(_unnpy(row["cdf_reference"]).size)
        if grown >= growth:
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

    `bundle_version` is the basis `embeddings` was built from, and it is what the fit is stamped
    with. It is the caller's to supply because the caller is the only frame that knows: §10's step
    3 holds the STAGED version while the active row is still the outgoing one, the worker holds
    `store.version`, the request path holds `app.state.artifacts.version`. None means "no bundle",
    which is §3.1's install and dd01's NULL stamp - not "look it up", because looking it up is
    precisely the bug. [M4.13, data-01]

    THE READ AND THE WRITE ARE ONE TRANSACTION, AND THE LOCK IS ABOVE BOTH. `load_observations` used
    to run outside any transaction and before any lock while the write opened seconds later with
    `prune = True`, so a verdict recorded in between was reverted to the unobserved prior - measured
    at `s -0.5504 observed=True` -> `s -0.0004 observed=False`, with the title dropped from the
    cached `title_ids` so the next tap could not find it either. `load_cache`'s `FOR UPDATE`
    serialised taps against each other and nothing against this. A tap arriving mid-fit now WAITS
    the seconds §5.3 budgets this row at and then solves against a cache that exists, which is the
    one outcome in which neither observation is lost. `refit_all` keeps its per-user loop, so one
    held lock is one board's wait and not the household's.

    READ COMMITTED is deliberate (the plan: "do not move to SERIALIZABLE"): the snapshot is not the
    mechanism. A verdict committed after `load_observations` read is still outside this fit either
    way - what the lock buys is that the TAP which will pick it up runs afterwards rather than
    underneath. The importer calls this inside its own transaction, so during a re-import the lock
    is held to the end of it; that is correct rather than unfortunate, because what a tap would be
    racing there is a change of basis that would discard it (§10 already tells the operator to
    restart both processes around one). [M4.13, data-05; plan step 24]
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
    """`refit_user`'s body, with its transaction and its lock already held by the caller.

    Split out rather than indented under an `async with`, which is the shape `update_incrementally`
    and `_update_incrementally` already have in this file for the same reason: the public name owns
    the transaction, the private one owns the work, and the inner `conn.transaction()` blocks below
    become savepoints inside it.
    """
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
        #
        # "Better empty" is what this branch SAID while leaving the board exactly as it was.
        # Reproduced on a household whose every observation had been undone: nightly with zero
        # observations left `fitted = False`, `ledger_state` at 6 rows with one still
        # `observed = True`, the learned cutpoints in place and the `ledger_fit` cache holding the
        # old `n_observed` - which `load_cache` then accepted, so the next first tap started its
        # incremental solve from residuals for verdicts that no longer exist. Decision 35's Undo
        # and decision 174's hard-DELETE are exactly how a person reaches this state, so it is
        # not hypothetical.
        #
        # Emptied rather than zeroed, in one transaction: `_write_state`'s prune path already
        # knows how to clear a board (it is the same DELETE the fitted path runs before it
        # writes), `ledger_fit` goes because a cache of a fit over nothing is not a cache, and
        # `ledger_cutpoints` keeps decision 11's `tier_set` while its boundaries go back to
        # `initial_cutpoints(K)` - the prior, which is the only honest answer once the labels that
        # moved them are gone, and which keeps `cutpoints_length`'s CHECK satisfied.
        # [M4.13, ml02; plan step 26]
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
                straddle=np.zeros(0, dtype=np.int64),
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
        # §5.3 gives the full fit "seconds" and `model.fit` inverts a dense (p+n)x(p+n) matrix,
        # so the cost is cubic in exactly this number: 6.96 s at n = 2000 and 33.4 s at n = 4000
        # on the measurement behind finding 9. One line, so a slow night is legible in the log
        # rather than inferred from a gap between two timestamps.
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

    # NOT `active_bundle_version(conn)`. The stamp answers "which basis is this fit expressed
    # in?" and the answer is the one the embeddings were built from; the active row answers "which
    # basis is the app serving?" and the two differ for the whole of §10's pre-flip rebuild, which
    # is the one caller this stamp exists for. Stamped with the active row, step 3's fit claimed
    # the outgoing bundle, `load_cache` refused it after the flip, and the first tap per
    # (user, kind) re-fitted on the request path against the old in-process Backbone and stamped
    # THAT as the new version. [M4.13, data-01]
    bundle = bundle_version
    async with conn.transaction():
        # A COMPARE-AND-SET ON THE TIER SET, because the fit read it minutes ago and §5.3 gives
        # itself "seconds" to run. Decision 11 makes the set a per-user preference with a control
        # that invites a second try, so a PUT landing mid-fit is ordinary: the fit then holds
        # cutpoints of the wrong length and a tier set nobody chose, and writing them back
        # reverted the person's change. Reproduced before the fix: PUT K = 5 queued a refit, PUT
        # K = 9 landed during it, and the movie row came out at K = 5 with the request cleared
        # while the series row read K = 9 — one board refusing legal drops into its upper tiers.
        #
        # Refused rather than repaired: re-deriving cutpoints for a set this fit never saw would
        # be inventing boundaries, and `refit_requested_at` is still set by the PUT, so the next
        # sweep fits the set that is actually on the row. `FOR UPDATE` makes the window the
        # transaction rather than the statement; the raise unwinds `_write_state` with it, so the
        # board is never left with tiers indexed against a tier set of another length.
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
    bundle_version: str | None = None,
    now: datetime | None = None,
) -> list[RefitReport]:
    """§5.3's nightly row, over the household. One person's bad fit must not stop the others'.

    `bundle_version` travels with `embeddings` and describes the same bundle: they are the two
    halves of one basis, and a caller that passes one without the other has produced a fit whose
    stamp does not describe its own coordinates. See `refit_user`. [M4.13, data-01]
    """
    reports: list[RefitReport] = []
    # The same household the fold-in fits, through the same helper: this pass read `is_active` and
    # `scoring/foldin.run` read `role IN ('admin', 'member')`, so a deactivated account was skipped
    # here and re-folded there every sixty seconds. [M4.13, ml04; plan step 23; decision 166]
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
                # The one failure that must NOT be isolated. The connection is unusable, so every
                # remaining (user, kind) would fail the same way and the job would hand `_tick` a
                # full report of a night nobody ran - one dead connection becoming a fortnight of
                # silently skipped fits, since `_tick` only records a job as failed if it raises.
                # Re-raised rather than logged: the next tick acquires a fresh connection from the
                # pool and `RETRY_AFTER` decides when. [M4.13, plan step 29]
                raise
            except Exception as exc:
                # Widened from `(RefitRefused, ValueError)`, and NOT for the reason the finding
                # gave: `np.linalg.LinAlgError` IS a `ValueError`, so a singular matrix was already
                # isolated by the narrow clause (probed: `report.error = 'Singular matrix'` with the
                # other member fitted). That claim is refuted and is not a reason to widen anything.
                # What actually aborted the loop is everything else a real night can raise - an
                # asyncpg `DataError` from a row the schema admits and numpy does not, a lock
                # timeout raised behind an import, a `MemoryError` in `_laplace`'s dense
                # (p+n)x(p+n) inverse - and "One person's bad fit must not stop the others'" is a
                # claim about failures, not about exception classes. The transaction stays INSIDE
                # `refit_user`, so an isolated failure cannot leak a partial write.
                # `asyncio.CancelledError` is a `BaseException` and still passes through, which is
                # what lets `_tick` abandon this job at its budget. [M4.13, plan step 29]
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

    Every row carries its `created_at`, because §5.2's freshness clock belongs to the OBSERVATION
    and this path used to read it off the wall clock instead. See `_update_incrementally`.
    [M4.13, ml06; plan step 28]
    """
    ids = list(title_ids)
    verdicts = await conn.fetch(
        """
        SELECT v.title_id, v.value, v.created_at FROM verdict v JOIN title t ON t.id = v.title_id
        WHERE v.user_id = $1 AND t.kind = $2 AND NOT v.is_reask AND v.title_id = ANY($3::int[])
        ORDER BY v.id
        """,
        user_id,
        kind,
        ids,
    )
    tier_edits = await conn.fetch(
        """
        SELECT e.title_id, e.tier, e.n_levels, e.created_at
        FROM tier_edit e JOIN title t ON t.id = e.title_id
        WHERE e.user_id = $1 AND t.kind = $2 AND e.title_id = ANY($3::int[])
        ORDER BY e.id
        """,
        user_id,
        kind,
        ids,
    )
    duels = await conn.fetch(
        """
        SELECT d.title_a, d.title_b, d.outcome, d.margin, d.created_at
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
    """§5.3's "<50 ms" row, run after the person's write has committed.

    A fit that refuses is a model problem, not a reason to lose the tap: the observation is
    already durable and the nightly refit will pick it up, so this reports rather than raises.

    It lives here, and not in one of the two routers, because both of them call it and they used
    to disagree: the Rate path caught these exceptions on purpose while `api/rank.py` called the
    fit bare after `drop_rules.drop` and `record_duel` had committed — so the same refusal lost
    the tap on one surface and not on the other, answered 500 over a row that was in fact
    written, and every retry of the lost one wrote another append-only row. `rank/tiers.py`'s
    `MAX_LABEL` comment records the same failure for tier labels. The shape is the one the Rate
    surface already ships under the gated `ledger` key, so the clients need nothing.
    """
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
        # A queued fit is not an applied one, and the two fields that would otherwise say it was
        # are the two the surfaces read: `RateModelLog.svelte` renders `refit` and `ms` as
        # "ledger movie - refit 12.6 ms" and `rate/session.py` passes the same `ms` into §6.7's
        # rail as ", incremental refit 13 ms". Nothing had been fitted — `ledger_fit` empty,
        # `ledger_state` unmoved, the board unchanged until the 60 s sweep — so both sentences
        # described work nobody did, on the one surface §6.7 exists to make honest. This is the
        # plan's own letter for the branch ("return `Delta(refit=True, rows=[])` so the caller
        # reports `applied=False`", step 3), and it needs no client change: the refused branch
        # the clients already render is the branch that says the update did not happen, and the
        # reason says which of the two ways it did not.
        # [M4.10 finding 9, cycle 2 m410-c2-cache-miss-reports-a-refit-that-did-not-run]
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

    It re-reads those titles' observations rather than being told what changed, so the same
    call serves a write and decision 35's undo: after either, the truth is in the tables and
    this solves for it.

    One precondition the caller still owns, because checking it would cost a query inside a
    50 ms budget to catch a mistake the caller cannot make by accident: every title in
    `title_ids` is of `kind` (`Write.kind` comes from `observations.kind_of`, so a write path
    gets this right by construction).

    THE OTHER ONE IS NO LONGER A PRECONDITION, IT IS AN ARGUMENT. `embeddings` is the source the
    cached fit was built with — v lives in that basis — and this used to be a sentence in a
    docstring, unchecked, because nothing in the database recorded which basis a caller held.
    `bundle_version` is that record, threaded from the route that pinned the Backbone: it names
    the basis `embeddings` is expressed in, `load_cache` refuses a fit stamped with any other,
    and a caller that cannot name one passes `BASIS_UNSTATED` and gets the old reading. It is
    stated here rather than inferred because the inference is wrong for the seconds that matter:
    see `load_cache`. [M4.13 cycle 1, finding 15]

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

    # BEFORE `load_cache`, and the order is the whole of it. `load_cache` takes `FOR UPDATE` on the
    # `ledger_fit` row; a tap that held that row lock while queueing behind this advisory lock would
    # be waiting for a fit whose own last statement is an UPDATE of that same row, which is a
    # deadlock this file can write in one line and Postgres would have to break. Taken here, the tap
    # waits the seconds §5.3 gives the full fit and then reads a cache the fit has committed --
    # rather than solving against the one it is replacing and being pruned away by it (data-05,
    # argued at `refit_user`). [M4.13, data-05; plan step 24]
    await _take_board_lock(conn, user_id=user_id, kind=kind)
    # AFTER the lock, and `bundle_version` is why that placement is load-bearing rather than
    # incidental: the tap's route checked §10's invariant before it queued here, and what it waited
    # out may have been the import that flipped it. This read is the re-check. [M4.13 cycle 1]
    cache = await load_cache(
        conn, user_id=user_id, kind=kind, hp=hp, bundle_version=bundle_version
    )
    if cache is None:
        # Finding 9. The observation is already committed by the caller, so there is nothing to
        # save by fitting now — and the fit is §5.3's "seconds" row running inside its "<50 ms"
        # one, on the event loop, hit on the first tap ever per (user, kind), on every tap after
        # a bundle import, on an `hp_digest` change and on the NaN fallback below.
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
    for row in verdicts:
        ord_index.append(position[int(row["title_id"])])
        ord_level.append(int(row["value"]))
        ord_arm.append(observations.ARM_VERDICT)
    n_levels = cache.n_levels
    for row in tier_edits:
        ord_index.append(position[int(row["title_id"])])
        # The same reading as `observations.load_observations`: decision 11 keeps a `tier_edit`
        # row across a change in K, and `n_levels` is the set the index was chosen under, so the
        # level is mapped by cumulative prior mass and clamped only as the helper's last step.
        # This copy used to clamp, and silently — a drop into tier 7 of a set of 12 became level 6
        # of 7 on the interactive path with no line anywhere. There is no log line here now and
        # that is deliberate, for the reason `observations.py` gives beside the warning it does
        # carry: the fit is where the WHOLE history passes through, and this path sees one or two
        # rows per tap, so a count is only meaningful at the other end.
        #
        # It is not because the rescale here is a no-op, which this comment used to claim.
        # `load_cache`'s K precondition compares the FIT's K against TODAY's tier set and says
        # nothing about `tier_edit.n_levels`, which is the K the individual edit was written
        # under -- and nothing ever rewrites that column, so after the 60 s sweep has refitted at
        # the new K `load_cache` ACCEPTS and rows carrying the old K flow through this loop. That
        # is the whole reason 0022 added the column, and it is exactly what
        # `test_the_loader_the_incremental_path_and_the_board_rescale_through_one_helper` measures:
        # level 6 written under K=7 reads as 11 against a cache at K=12. A reader who took "the
        # identity" at face value would delete this call and restore the silent clamp step 15
        # removed. [M4.13, ml01 and dd06; cycle 2, M413-C2-DIM5-02]
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
        # The guard still has a recovery rather than only a refusal — the exact fit is asked
        # for rather than run, which is the same recovery one sweep later and does not spend
        # §5.3's "seconds" inside a request that has already committed its observation. What
        # matters here is unchanged: no NaN reaches `ledger_state`, so nothing is pinned to the
        # top of every §6.0 shelf while the fix is owed.
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

    # §5.2's freshness clock belongs to the OBSERVATION, not to the tick that reads it. "Just
    # observed, so no freshness inflation applies" was true of the arm this path was written for and
    # false of the one decision 35 added: `update_incrementally` serves an undo with the same call,
    # and after undoing a re-rating what remains is the older verdict. Stamped from the wall clock,
    # a 400-day-old verdict was restamped as if it had just been made - reproduced as
    # `last_observed_at 2025-07-30` with `sigma_eff 1.4276` becoming `2026-09-03` with
    # `sigma_eff == sigma`, i.e. §5.2's inflation silently switched off by the act of retracting
    # something. `touched` was already derived from the rows; this derives the clock from the same
    # pass, as `max(created_at)` over the rows that touch the title, which is the definition
    # `load_observations` uses for the nightly path. The two paths now agree by construction rather
    # than by resemblance. [M4.13, ml06; plan step 28]
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
    # None where the title has no live observation left, which is what an undone block looks like
    # and is the same absence the nightly path writes for a title nobody has rated.
    stamps: list[datetime | None] = [last_at.get(t) for t in targets]

    sigma_eff = model.inflate_sigma(sigma, sigma_prior, _months_since(stamps, now), hp)
    # §5.2's CDF against the cached reference: the same definition, one night stale. Rebuilding
    # it per observation is O(library) and is what the nightly job is for.
    cdf = model.empirical_cdf(cache.cdf_reference, s)
    tier = model.tier_of(s, cache.cuts)
    straddle = model.straddle(s, sigma_eff, cache.cuts, hp)

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


async def _queue_full_refit(conn: asyncpg.Connection, *, user_id: int, kind: str) -> None:
    """Ask the 60 s sweep for the fit this request is not going to run.

    `ledger_cutpoints.refit_requested_at` and the job that reads it are 0012's and decision 11's,
    and nothing about the column says the only thing allowed to ask is a tier-set change.

    INSERT rather than UPDATE, because the first tap of all is precisely a (user, kind) with no
    row: an UPDATE would have queued nothing and the person's first sitting would have waited for
    the nightly job. The row it creates carries §6.3's prior shape — the same boundaries
    `read.cutpoints_of` already falls back to when there is no row at all, so the board reads the
    same numbers either way and the queued fit overwrites them.
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
    "read_board",
    "refit_all",
    "refit_user",
    "update_incrementally",
    "update_incrementally_reporting",
]
