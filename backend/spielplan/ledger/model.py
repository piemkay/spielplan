"""The Personal Ledger's maths (§5.2): s_i = mu + <v, e_i> + r_i, four arms on one latent. numpy only.

F is jointly convex, so any divergence is a step-size failure. Stage A solves the ordinal arms by
arrowhead Newton; stage B adds the duels, preconditioned by stage A's Hessian (§5.2's scar).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams

EMBED_DIM = 64

# §6.3's tier shape: the prior mean for the K = 6 default tier set (decision 561).
MEASURED_TIER_SHARES: tuple[float, ...] = (0.05, 0.15, 0.30, 0.25, 0.15, 0.10)

# Decision 508: the verdict arm's two cutpoints sit at the shape's E/D and D/C masses, so the tier
# and verdict arms describe one latent (disliked E, fine D, liked C/B/A/S; decision 561).
VERDICT_ANCHOR_SHARES: tuple[float, float] = (0.05, 0.20)

# Outcome codes for the duel arm.
OUT_A, OUT_B, OUT_TIE = 0, 1, 2


# --- small numerically-careful primitives ------------------------------------------------


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return np.where(x >= 0, 1.0 / (1.0 + np.exp(-np.abs(x))),
                    np.exp(-np.abs(x)) / (1.0 + np.exp(-np.abs(x))))


def _log_sigmoid(x: np.ndarray) -> np.ndarray:
    """log σ(x) = −softplus(−x), evaluated without overflowing either tail."""
    return -np.logaddexp(0.0, -x)


@dataclass
class ObservationSet:
    """Everything the fit sees for one (user, kind). An unembedded title still gets an r_i."""

    title_ids: np.ndarray                       # int64[n], ascending
    embeddings: np.ndarray                      # float64[n, 64]
    embedded: np.ndarray                        # bool[n]
    # ordinal arms: one row per observation, `arm` 0 = verdict (3 levels), 1 = tier (K levels)
    ord_index: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    ord_level: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    ord_arm: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    ord_weight: np.ndarray = field(default_factory=lambda: np.zeros(0))
    # duel arm
    duel_a: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    duel_b: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    duel_outcome: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    # The RAW margin, not a weight; `_duel_weights` applies §4.3's margin/mean(margin).
    duel_margin: np.ndarray = field(default_factory=lambda: np.zeros(0))
    # A per-row multiplier on top of the margin weighting (recency); None weighs every row 1.
    duel_weight: np.ndarray | None = None
    n_levels: int = 6                           # K, the size of the user's tier set

    @property
    def n(self) -> int:
        return int(self.title_ids.size)

    def is_empty(self) -> bool:
        return self.ord_index.size == 0 and self.duel_a.size == 0


@dataclass(frozen=True)
class Fit:
    mu: float
    v: np.ndarray
    gamma: np.ndarray
    cuts: np.ndarray
    log_nu: float
    r: np.ndarray
    s: np.ndarray
    sigma: np.ndarray
    sigma_prior: np.ndarray
    z_cov: np.ndarray                 # (65, 65) posterior block for (mu, v)
    anchor_curv: np.ndarray
    duel_curv: np.ndarray
    objective: float
    grad_inf: float
    iterations: tuple[int, int]
    backtracks: int
    converged: bool

    @property
    def n_levels(self) -> int:
        return int(self.cuts.size) + 1


# --- the ordinal arm ----------------------------------------------------------------------


def feasible(gamma: np.ndarray, cuts: np.ndarray) -> bool:
    """Whether both cutpoint sets are ascending. The cone is CLOSED: an unused level has zero width.

    An observed level with zero probability is refused by `_ordinal_terms` (+inf), not here.
    """
    return bool(
        (gamma.size < 2 or np.all(np.diff(gamma) >= 0))
        and (cuts.size < 2 or np.all(np.diff(cuts) >= 0))
    )


def _duel_weights(obs: ObservationSet, hp: Hyperparams) -> np.ndarray:
    """§4.3's margin/mean(margin): total duel evidence is invariant to how often "decisive" is tapped."""
    raw = obs.duel_margin
    weights = np.ones(raw.size)
    usable = np.isfinite(raw) & (raw > 0)
    if np.any(usable):
        weights[usable] = raw[usable] / float(raw[usable].mean())
    return weights if obs.duel_weight is None else weights * obs.duel_weight


def _ordinal_terms(s: np.ndarray, level: np.ndarray, cuts: np.ndarray):
    """Per-observation value and (a, b) derivatives of −log P(level | s, cuts).

    P(y) = σ(a) − σ(b) with a = c_y − s, b = c_{y−1} − s; a missing edge cutpoint contributes zero.
    """
    levels = cuts.size + 1
    upper = level < levels - 1
    lower = level > 0
    a = np.where(upper, cuts[np.clip(level, 0, cuts.size - 1)] - s, np.inf)
    b = np.where(lower, cuts[np.clip(level - 1, 0, cuts.size - 1)] - s, -np.inf)

    sa = np.where(upper, _sigmoid(np.where(upper, a, 0.0)), 1.0)
    sb = np.where(lower, _sigmoid(np.where(lower, b, 0.0)), 0.0)
    phi_a = np.where(upper, sa * (1.0 - sa), 0.0)
    phi_b = np.where(lower, sb * (1.0 - sb), 0.0)

    # A crossed pair (gap <= 0) must be +inf, a branch and not a clamp: a finite penalty lets the
    # minimiser leave the cone, and log(-expm1(-gap)) is NaN there, not +inf.
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        interior = upper & lower
        log_p = np.empty_like(s, dtype=float)
        only_upper = upper & ~lower
        only_lower = lower & ~upper
        log_p[only_upper] = _log_sigmoid(a[only_upper])
        log_p[only_lower] = _log_sigmoid(-b[only_lower])
        if np.any(interior):
            ai, bi = a[interior], b[interior]
            gap = ai - bi
            ordered = gap > 0
            block = np.full(ai.shape, -np.inf)
            block[ordered] = (
                _log_sigmoid(ai[ordered])
                + _log_sigmoid(-bi[ordered])
                + np.log(-np.expm1(-gap[ordered]))
            )
            log_p[interior] = block
    p = np.exp(log_p)
    p = np.maximum(p, 1e-300)

    f_a = -phi_a / p
    f_b = phi_b / p
    dphi_a = phi_a * (1.0 - 2.0 * sa)
    dphi_b = phi_b * (1.0 - 2.0 * sb)
    f_aa = -dphi_a / p + (phi_a / p) ** 2
    f_bb = dphi_b / p + (phi_b / p) ** 2
    # Divide first: `-(phi_a * phi_b) / p**2` overflows to NaN for a tiny p.
    f_ab = -(phi_a / p) * (phi_b / p)
    return -log_p, f_a, f_b, f_aa, f_ab, f_bb


# --- the duel arm -------------------------------------------------------------------------


def _duel_terms(d: np.ndarray, outcome: np.ndarray, log_nu: float):
    """Davidson (1970) with ties: P(A) = e^{d/2}/Z, P(B) = e^{-d/2}/Z, P(TIE) = nu/Z."""
    half = 0.5 * d
    logits = np.stack([half, -half, np.full_like(d, log_nu)], axis=1)
    m = logits.max(axis=1)
    log_z = m + np.log(np.exp(logits - m[:, None]).sum(axis=1))
    p = np.exp(logits - log_z[:, None])
    p_a, p_b, p_t = p[:, 0], p[:, 1], p[:, 2]

    chosen = np.where(outcome == OUT_A, half, np.where(outcome == OUT_B, -half, log_nu))
    nll = log_z - chosen

    dt_dd = np.where(outcome == OUT_A, 0.5, np.where(outcome == OUT_B, -0.5, 0.0))
    g_d = 0.5 * (p_a - p_b) - dt_dd
    g_psi = p_t - (outcome == OUT_TIE).astype(float)

    h_dd = 0.25 * (p_a + p_b - (p_a - p_b) ** 2)
    h_dpsi = -0.5 * (p_a - p_b) * p_t
    h_psipsi = p_t * (1.0 - p_t)
    return nll, g_d, g_psi, h_dd, h_dpsi, h_psipsi


# --- assembly ------------------------------------------------------------------------------


def initial_cutpoints(k: int) -> np.ndarray:
    """Cutpoints whose level shares match §6.3's shape at K = 6, else equal mass."""
    shares = MEASURED_TIER_SHARES if k == len(MEASURED_TIER_SHARES) else (1.0 / k,) * k
    cumulative = np.cumsum(np.asarray(shares, dtype=float))[:-1]
    return np.log(cumulative / (1.0 - cumulative))


def verdict_cutpoints() -> np.ndarray:
    """The verdict arm's prior cutpoints: the shape's E/D and D/C masses (decisions 508, 561)."""
    shares = np.asarray(VERDICT_ANCHOR_SHARES, dtype=float)
    return np.log(shares / (1.0 - shares))


def anchored_cuts(k: int) -> tuple[int | None, int]:
    """Decision 508: the cut indices nearest the anchor masses (ties go to the fine class).

    Two tiers have one cut, the fine/liked one.
    """
    if k < 3:
        return None, 0
    shares = np.asarray(
        MEASURED_TIER_SHARES if k == len(MEASURED_TIER_SHARES) else (1.0 / k,) * k, dtype=float
    )
    mass = np.cumsum(shares)[:-1]
    low, high = VERDICT_ANCHOR_SHARES
    # Float sums must not decide a tie: six equal tiers put two cuts 1/12 either side of 25%.
    near_low = np.abs(mass - low)
    lower = int(np.flatnonzero(near_low <= near_low.min() + 1e-9)[0])
    above = np.arange(lower + 1, k - 1)
    near_high = np.abs(mass[above] - high)
    upper = int(above[np.flatnonzero(near_high <= near_high.min() + 1e-9)[-1]])
    return lower, upper


def _anchor_map(k: int) -> tuple[np.ndarray, np.ndarray]:
    """`cut_prior_mean` as an affine map (A, c): mean = A @ gamma + c.

    Cuts between the anchors interpolate on the logit of cumulative mass; outside them they keep
    the shape's logistic distance from the nearer anchor.
    """
    shape = initial_cutpoints(k)
    lower, upper = anchored_cuts(k)
    a = np.zeros((k - 1, 2))
    c = np.zeros(k - 1)
    for j, at in enumerate(shape):
        if lower is not None and j <= lower:
            a[j, 0], c[j] = 1.0, at - shape[lower]
        elif lower is None or j >= upper:
            a[j, 1], c[j] = 1.0, at - shape[upper]
        else:
            t = (at - shape[lower]) / (shape[upper] - shape[lower])
            a[j, 0], a[j, 1] = 1.0 - t, t
    return a, c


def cut_prior_mean(gamma: np.ndarray, k: int) -> np.ndarray:
    """Decision 508: the tier arm's prior mean, anchored on the verdict arm's fitted cutpoints.

    Linear in gamma, so the prior stays a convex quadratic in (gamma, cuts).
    """
    a, c = _anchor_map(k)
    return a @ np.asarray(gamma, dtype=float) + c


def verdict_tiers(k: int) -> np.ndarray:
    """(3, 2): the lowest and highest tier each verdict class renders in (decision 508).

    On two tiers the lower one holds both disliked and fine.
    """
    lower, upper = anchored_cuts(k)
    if lower is None:
        return np.array([[0, 0], [0, 0], [1, 1]], dtype=np.int64)
    return np.array([[0, lower], [lower + 1, upper], [upper + 1, k - 1]], dtype=np.int64)


def verdict_class_of_tier(tier: int, k: int) -> int:
    """The verdict a tier stands for: the class whose band owns it (decision 508)."""
    bands = verdict_tiers(k)
    for label in (1, 0, 2):
        if bands[label, 0] <= tier <= bands[label, 1]:
            return label
    return 0 if tier < bands[1, 0] else 2


def class_step(cls: int, k: int) -> int:
    """The step a verdict class stands for: its tier nearest the middle, E, D or C at K = 6 (§5.1).

    At K = 2 disliked and fine share the lower tier.
    """
    low, high = verdict_tiers(k)[cls]
    return int(min(max((k - 1) // 2, int(low)), int(high)))


def guess_tier(tier: np.ndarray, k: int) -> np.ndarray:
    """Decision 510: an unrated title wears its guessed class's tier nearest the middle (E, D or C)."""
    steps = np.array([class_step(verdict_class_of_tier(t, k), k) for t in range(k)], dtype=np.int64)
    return steps[np.clip(np.asarray(tier, dtype=np.int64), 0, k - 1)]


def live_verdicts(obs: ObservationSet) -> np.ndarray:
    """Per title of `obs`, the verdict its tier is held to (decision 508), or -1.

    Rows arrive in id order, so the last verdict per title is live. A `tier_edit` title is -1.
    """
    out = np.full(obs.n, -1, dtype=np.int64)
    rows = obs.ord_arm == 0
    for index, level in zip(obs.ord_index[rows], obs.ord_level[rows], strict=True):
        out[int(index)] = int(level)
    out[np.unique(obs.ord_index[obs.ord_arm == 1])] = -1
    return out


def hold_to_verdict(
    tier: np.ndarray, straddle_to: np.ndarray, verdict: np.ndarray, k: int
) -> tuple[np.ndarray, np.ndarray]:
    """Decision 508: a rated title renders inside the tiers its live verdict names.

    Where the hold binds, the straddle names the next tier toward `s`. `verdict` -1 is not held.
    """
    tier = np.asarray(tier, dtype=np.int64).copy()
    straddle_to = np.asarray(straddle_to, dtype=np.int64).copy()
    bands = verdict_tiers(k)
    for i, label in enumerate(np.asarray(verdict, dtype=np.int64)):
        if label < 0:
            continue
        low, high = bands[int(label)]
        held = min(max(int(tier[i]), int(low)), int(high))
        if held != int(tier[i]):
            straddle_to[i] = held + (1 if tier[i] > held else -1)
            tier[i] = held
    return tier, straddle_to


@dataclass
class _Layout:
    n: int
    k: int

    @property
    def n_gamma(self) -> int:
        return 2

    @property
    def n_cuts(self) -> int:
        return self.k - 1

    @property
    def n_extra(self) -> int:                    # gamma, cuts, psi
        return self.n_gamma + self.n_cuts + 1

    @property
    def p(self) -> int:                          # mu, v, then the extras
        return 1 + EMBED_DIM + self.n_extra


def _objective(
    obs: ObservationSet, hp: Hyperparams, mu, v, gamma, cuts, log_nu, r, *, with_duels: bool
) -> float:
    if not feasible(gamma, cuts):
        return np.inf
    s = mu + obs.embeddings @ v + r
    total = 0.0
    if obs.ord_index.size:
        for arm, c in ((0, gamma), (1, cuts)):
            m = obs.ord_arm == arm
            if not np.any(m):
                continue
            nll, *_ = _ordinal_terms(s[obs.ord_index[m]], obs.ord_level[m], c)
            total += float(np.sum(obs.ord_weight[m] * nll))
    if with_duels and obs.duel_a.size:
        d = s[obs.duel_a] - s[obs.duel_b]
        nll, *_ = _duel_terms(d, obs.duel_outcome, log_nu)
        total += hp.lambda_bt * float(np.sum(_duel_weights(obs, hp) * nll))

    cuts_init = cut_prior_mean(gamma, obs.n_levels)
    total += 0.5 * hp.lambda_ridge * float(v @ v)
    total += 0.5 * float(r @ r) / hp.b_i_tau**2
    total += 0.5 * mu**2 / hp.mu_prior_tau**2
    total += 0.5 * hp.cutpoint_prior_precision * float(np.sum((cuts - cuts_init) ** 2))
    total += 0.5 * hp.tie_prior_precision * (log_nu - np.log(hp.nu0())) ** 2
    total += 0.5 * hp.cutpoint_prior_precision * float(np.sum((gamma - verdict_cutpoints()) ** 2))
    return total


def _grad_hess(
    obs: ObservationSet, hp: Hyperparams, mu, v, gamma, cuts, log_nu, r, *, with_duels: bool
):
    """Gradient, and the arrowhead Hessian blocks, of the objective at one point."""
    lay = _Layout(obs.n, obs.n_levels)
    s = mu + obs.embeddings @ v + r

    g_s = np.zeros(obs.n)
    h_ss = np.zeros(obs.n)
    g_extra = np.zeros(lay.n_extra)
    d_extra = np.zeros((lay.n_extra, lay.n_extra))
    c_cross = np.zeros((obs.n, lay.n_extra))     # d2 NLL / (ds_i dtheta_j)

    for arm, cut_vec, offset in ((0, gamma, 0), (1, cuts, lay.n_gamma)):
        m = obs.ord_arm == arm
        if not np.any(m):
            continue
        idx = obs.ord_index[m]
        level = obs.ord_level[m]
        w = obs.ord_weight[m]
        _nll, f_a, f_b, f_aa, f_ab, f_bb = _ordinal_terms(s[idx], level, cut_vec)

        np.add.at(g_s, idx, w * (-(f_a + f_b)))
        np.add.at(h_ss, idx, w * (f_aa + 2.0 * f_ab + f_bb))

        levels = cut_vec.size + 1
        upper = level < levels - 1
        lower = level > 0
        col_u = offset + np.clip(level, 0, cut_vec.size - 1)
        col_l = offset + np.clip(level - 1, 0, cut_vec.size - 1)

        np.add.at(g_extra, col_u[upper], (w * f_a)[upper])
        np.add.at(g_extra, col_l[lower], (w * f_b)[lower])
        np.add.at(d_extra, (col_u[upper], col_u[upper]), (w * f_aa)[upper])
        np.add.at(d_extra, (col_l[lower], col_l[lower]), (w * f_bb)[lower])
        both = upper & lower
        np.add.at(d_extra, (col_u[both], col_l[both]), (w * f_ab)[both])
        np.add.at(d_extra, (col_l[both], col_u[both]), (w * f_ab)[both])
        np.add.at(c_cross, (idx[upper], col_u[upper]), (w * (-(f_aa + f_ab)))[upper])
        np.add.at(c_cross, (idx[lower], col_l[lower]), (w * (-(f_ab + f_bb)))[lower])

    # A duel couples its two titles off-diagonally, so its curvature is returned separately and
    # the arrowhead blocks below stay the ordinal Hessian (diagonal in r).
    duel_curv = np.zeros(obs.n)
    coupling = None
    if with_duels and obs.duel_a.size:
        col_psi = lay.n_extra - 1
        d = s[obs.duel_a] - s[obs.duel_b]
        _nll, g_d, g_psi, h_dd, h_dpsi, h_psipsi = _duel_terms(d, obs.duel_outcome, log_nu)
        w = hp.lambda_bt * _duel_weights(obs, hp)
        np.add.at(g_s, obs.duel_a, w * g_d)
        np.add.at(g_s, obs.duel_b, -w * g_d)
        np.add.at(duel_curv, obs.duel_a, w * h_dd)
        np.add.at(duel_curv, obs.duel_b, w * h_dd)
        g_extra[col_psi] += float(np.sum(w * g_psi))
        coupling = (obs.duel_a, obs.duel_b, w * h_dd, w * h_dpsi, float(np.sum(w * h_psipsi)))

    # priors. The cuts' prior mean is A @ gamma + c (decision 508), which couples the two sets.
    anchor, _offset = _anchor_map(obs.n_levels)
    cuts_init = cut_prior_mean(gamma, obs.n_levels)
    gamma_init = verdict_cutpoints()
    precision = hp.cutpoint_prior_precision
    g_extra[: lay.n_gamma] += precision * (gamma - gamma_init) - precision * anchor.T @ (
        cuts - cuts_init
    )
    g_extra[lay.n_gamma : lay.n_gamma + lay.n_cuts] += precision * (cuts - cuts_init)
    g_extra[-1] += hp.tie_prior_precision * (log_nu - np.log(hp.nu0()))
    for j in range(lay.n_gamma + lay.n_cuts):
        d_extra[j, j] += precision
    on_gamma = slice(0, lay.n_gamma)
    on_cuts = slice(lay.n_gamma, lay.n_gamma + lay.n_cuts)
    d_extra[on_gamma, on_gamma] += precision * anchor.T @ anchor
    d_extra[on_gamma, on_cuts] -= precision * anchor.T
    d_extra[on_cuts, on_gamma] -= precision * anchor
    d_extra[-1, -1] += hp.tie_prior_precision

    anchor_curv = h_ss + 1.0 / hp.b_i_tau**2
    g_r = g_s + r / hp.b_i_tau**2
    h_rr = h_ss + 1.0 / hp.b_i_tau**2

    jac = np.concatenate([np.ones((obs.n, 1)), obs.embeddings], axis=1)   # n x 65
    g_z = np.concatenate([jac.T @ g_s, g_extra])
    g_z[1 : 1 + EMBED_DIM] += hp.lambda_ridge * v
    g_z[0] += mu / hp.mu_prior_tau**2

    h_zz = np.zeros((lay.p, lay.p))
    h_zz[:65, :65] = jac.T @ (jac * h_ss[:, None])
    h_zz[:65, 65:] = jac.T @ c_cross
    h_zz[65:, :65] = h_zz[:65, 65:].T
    h_zz[65:, 65:] = d_extra
    h_zz[0, 0] += 1.0 / hp.mu_prior_tau**2
    for j in range(1, 1 + EMBED_DIM):
        h_zz[j, j] += hp.lambda_ridge

    h_zr = np.concatenate([jac.T * h_ss[None, :], c_cross.T], axis=0)     # p x n
    return g_z, g_r, h_zz, h_zr, h_rr, anchor_curv, duel_curv, coupling



# --- the monotone parameterisation ---------------------------------------------------------
# The search runs in cuts_j = c0 + sum_{i<=j} exp(delta_i), a bijection onto the open cone, so an
# optimum on the constraint (an unused level) is approached as delta -> -inf instead of hit.


def _to_raw(values: np.ndarray) -> np.ndarray:
    """(v_0, v_1, ...) ascending  ->  (v_0, log gaps)."""
    if values.size == 0:
        return values.copy()
    gaps = np.maximum(np.diff(values), 1e-12)
    return np.concatenate([[values[0]], np.log(gaps)])


def _from_raw(raw: np.ndarray) -> np.ndarray:
    """(a_0, deltas) -> ascending values. Clipped only to stop exp from overflowing."""
    if raw.size == 0:
        return raw.copy()
    return raw[0] + np.concatenate([[0.0], np.cumsum(np.exp(np.clip(raw[1:], -60.0, 30.0)))])


def _raw_jacobian(raw: np.ndarray) -> np.ndarray:
    """d values / d raw. Lower-triangular in the delta block, with a column of ones for a_0."""
    m = raw.size
    jac = np.zeros((m, m))
    jac[:, 0] = 1.0
    if m > 1:
        gaps = np.exp(np.clip(raw[1:], -60.0, 30.0))
        for i in range(1, m):
            jac[i:, i] = gaps[i - 1]
    return jac


def _raw_curvature(raw: np.ndarray, grad_values: np.ndarray) -> np.ndarray:
    """The second-derivative term of the change of basis: d2 v_j / d delta_i^2 = exp(delta_i)."""
    m = raw.size
    extra = np.zeros(m)
    if m > 1:
        gaps = np.exp(np.clip(raw[1:], -60.0, 30.0))
        for i in range(1, m):
            extra[i] = float(np.sum(grad_values[i:])) * gaps[i - 1]
    return extra


def _schur_solve(h_zz, h_zr, h_rr, g_z, g_r):
    """One arrowhead solve via the Schur complement: O(n·p^2 + p^3), since H_rr is diagonal."""
    inv_rr = 1.0 / h_rr
    schur = h_zz - (h_zr * inv_rr[None, :]) @ h_zr.T
    rhs = g_z - (h_zr * inv_rr[None, :]) @ g_r
    # With no observations the problem is only positive semi-definite.
    schur = schur + 1e-10 * np.eye(schur.shape[0])
    dz = np.linalg.solve(schur, rhs)
    dr = inv_rr * (g_r - h_zr.T @ dz)
    return dz, dr, schur


def _unpack(z, lay: _Layout):
    mu = float(z[0])
    v = z[1 : 1 + EMBED_DIM]
    extra = z[1 + EMBED_DIM :]
    gamma = extra[: lay.n_gamma]
    cuts = extra[lay.n_gamma : lay.n_gamma + lay.n_cuts]
    log_nu = float(extra[-1])
    return mu, v, gamma, cuts, log_nu


def _pack(mu, v, gamma, cuts, log_nu):
    return np.concatenate([[mu], v, gamma, cuts, [log_nu]])


def _unpack_raw(z, lay: _Layout):
    """Raw coordinates -> the values the maths is written in."""
    mu = float(z[0])
    v = z[1 : 1 + EMBED_DIM]
    extra = z[1 + EMBED_DIM :]
    gamma = _from_raw(extra[: lay.n_gamma])
    cuts = _from_raw(extra[lay.n_gamma : lay.n_gamma + lay.n_cuts])
    return mu, v, gamma, cuts, float(extra[-1])


def _pack_raw(mu, v, gamma, cuts, log_nu):
    return np.concatenate([[mu], v, _to_raw(gamma), _to_raw(cuts), [log_nu]])


def _to_raw_space(z, lay: _Layout, g_values, h_zz, h_zr):
    """Move a gradient and Hessian from value space into the raw coordinates the search uses."""
    extra = z[1 + EMBED_DIM :]
    raw_gamma = extra[: lay.n_gamma]
    raw_cuts = extra[lay.n_gamma : lay.n_gamma + lay.n_cuts]

    transform = np.eye(lay.p)
    off = 1 + EMBED_DIM
    transform[off : off + lay.n_gamma, off : off + lay.n_gamma] = _raw_jacobian(raw_gamma)
    off2 = off + lay.n_gamma
    transform[off2 : off2 + lay.n_cuts, off2 : off2 + lay.n_cuts] = _raw_jacobian(raw_cuts)

    g_raw = transform.T @ g_values
    h_raw = transform.T @ h_zz @ transform
    # …plus the curvature of the map itself, which is diagonal in the delta coordinates.
    bend = np.zeros(lay.p)
    bend[off : off + lay.n_gamma] = _raw_curvature(
        raw_gamma, g_values[off : off + lay.n_gamma]
    )
    bend[off2 : off2 + lay.n_cuts] = _raw_curvature(
        raw_cuts, g_values[off2 : off2 + lay.n_cuts]
    )
    h_raw = h_raw + np.diag(bend)
    return g_raw, h_raw, transform.T @ h_zr


# Iterations between re-deriving the ridge-Hessian preconditioner at the current point; a frozen
# one falls short of the optimum within the step budget.
PRECONDITIONER_REFRESH = 5

# A search guard, not a model constant: a gap may change by at most e^8 per iteration.
MAX_LOG_GAP_STEP = 8.0


def _log_gap_positions(lay: _Layout) -> np.ndarray:
    """Where the monotone parameterisation keeps its log gaps: gamma's one, then the cuts'."""
    start = 1 + EMBED_DIM
    cut_gaps = np.arange(start + lay.n_gamma + 1, start + lay.n_gamma + lay.n_cuts)
    return np.concatenate([[start + 1], cut_gaps]).astype(np.int64)


def _minimise(obs, hp, *, with_duels, z0, r0, precondition_from=None, max_iter=None,
              step0=1.0, refresh_preconditioner=False):
    lay = _Layout(obs.n, obs.n_levels)
    z, r = z0.copy(), r0.copy()
    backtracks = 0
    iterations = 0
    grad0 = None

    limit = max_iter if max_iter is not None else hp.newton_max_iter
    eta = step0
    for iterations in range(1, limit + 1):
        mu, v, gamma, cuts, log_nu = _unpack_raw(z, lay)
        g_v, g_r, h_zz, h_zr, h_rr, anchor_curv, duel_curv, _cpl = _grad_hess(
            obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels
        )
        g_z, h_zz, h_zr = _to_raw_space(z, lay, g_v, h_zz, h_zr)
        grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))
        if grad0 is None:
            grad0 = grad_inf
        if grad_inf < hp.newton_tol * (1.0 + grad0):
            break

        if refresh_preconditioner and iterations % PRECONDITIONER_REFRESH == 1 and iterations > 1:
            a_g, _agr, a_zz, a_zr, a_rr, *_ = _grad_hess(
                obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=False
            )
            _ga, a_zz, a_zr = _to_raw_space(z, lay, a_g, a_zz, a_zr)
            precondition_from = (a_zz, a_zr, a_rr)

        if precondition_from is None:
            solve_zz, solve_zr, solve_rr = h_zz, h_zr, h_rr
        else:
            # Stage B: step with the anchor's curvature (§5.2's scar).
            solve_zz, solve_zr, solve_rr = precondition_from

        # Levenberg damping until the step points downhill: the parameterisation's curvature term
        # can be negative where a gap is closing.
        damping = 0.0
        for _attempt in range(24):
            trial_zz = solve_zz + damping * np.eye(solve_zz.shape[0])
            trial_rr = solve_rr + damping
            dz, dr, _ = _schur_solve(trial_zz, solve_zr, trial_rr, g_z, g_r)
            slope = float(g_z @ dz + g_r @ dr)
            if slope > 0 and np.all(np.isfinite(dz)) and np.all(np.isfinite(dr)):
                break
            damping = max(damping * 4.0, 1e-6)
        else:
            # Nothing worked; the gradient itself always descends.
            dz, dr = g_z.copy(), g_r.copy()
            slope = float(g_z @ dz + g_r @ dr)

        # A closing gap is nearly flat in its log coordinate, so the step along it can be huge
        # enough to overflow every trial. Scale the direction, don't change it.
        reach = float(np.max(np.abs(dz[_log_gap_positions(lay)]), initial=0.0))
        if reach > MAX_LOG_GAP_STEP:
            shrink = MAX_LOG_GAP_STEP / reach
            dz, dr, slope = dz * shrink, dr * shrink, slope * shrink

        f0 = _objective(obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels)
        # Try twice the last accepted step first rather than re-paying the halvings from step0.
        eta = min(1.0, max(step0, eta * 2.0))
        accepted = False
        while eta >= hp.lr_min:
            z_try, r_try = z - eta * dz, r - eta * dr
            m2, v2, ga2, c2, ln2 = _unpack_raw(z_try, lay)
            f1 = _objective(obs, hp, m2, v2, ga2, c2, ln2, r_try, with_duels=with_duels)
            if np.isfinite(f1) and f1 <= f0 - 1e-4 * eta * slope:
                accepted = True
                break
            eta *= 0.5
            backtracks += 1
        if not accepted:
            # Keep the last point that decreased the objective; never apply the rejected step.
            break
        z, r = z - eta * dz, r - eta * dr

    mu, v, gamma, cuts, log_nu = _unpack_raw(z, lay)
    g_v, g_r, h_zz, h_zr, h_rr, anchor_curv, duel_curv, _cpl = _grad_hess(
        obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels
    )
    g_z, _hraw, _hzr_raw = _to_raw_space(z, lay, g_v, h_zz, h_zr)
    grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))
    return z, r, (h_zz, h_zr, h_rr), grad_inf, iterations, backtracks, anchor_curv, duel_curv


def fit(
    obs: ObservationSet,
    hp: Hyperparams,
    *,
    z0: np.ndarray | None = None,
    r0: np.ndarray | None = None,
) -> Fit:
    """The full MAP fit: ridge anchor, then the preconditioned BT perturbation.

    `z0` (in value space) and `r0` let a test check that every start reaches the same optimum.
    """
    lay = _Layout(obs.n, obs.n_levels)
    z = _pack_raw(0.0, np.zeros(EMBED_DIM), verdict_cutpoints(),
                  cut_prior_mean(verdict_cutpoints(), obs.n_levels), float(np.log(hp.nu0())))
    if z0 is not None:
        z0 = np.asarray(z0, dtype=float)
        mu0, v0, gamma0, cuts0, nu0 = _unpack(z0, lay)
        z = _pack_raw(mu0, v0, gamma0, cuts0, nu0)
    r = np.zeros(obs.n) if r0 is None else np.asarray(r0, dtype=float).copy()

    # Stage A — the ordinal arms alone, solved exactly.
    z, r, blocks_a, _g_a, it_a, bt_a, anchor_curv, _ = _minimise(
        obs, hp, with_duels=False, z0=z, r0=r
    )

    # Stage B — add the duel arm, stepping with stage A's curvature; §4.3's `lr` is the first step.
    it_b = bt_b = 0
    if obs.duel_a.size:
        z, r, blocks_b, _g_b, it_b, bt_b, anchor_curv, duel_curv = _minimise(
            obs, hp, with_duels=True, z0=z, r0=r,
            precondition_from=blocks_a, max_iter=hp.steps, step0=float(hp.lr),
            refresh_preconditioner=True,
        )
        blocks = blocks_b
    else:
        duel_curv = np.zeros(obs.n)
        blocks = blocks_a

    mu, v, gamma, cuts, log_nu = _unpack_raw(z, lay)
    s = mu + obs.embeddings @ v + r
    h_zz, h_zr, h_rr = blocks
    g_v, g_r, oh_zz, oh_zr, oh_rr, _ac, _dc, coupling = _grad_hess(
        obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=obs.duel_a.size > 0
    )
    # Measured in raw coordinates: at a constrained optimum the value-space gradient is nonzero.
    g_z, _h_raw, _hzr_raw = _to_raw_space(
        _pack_raw(mu, v, gamma, cuts, log_nu), lay, g_v, oh_zz, oh_zr
    )
    grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))

    n_obs = obs.ord_index.size + obs.duel_a.size
    sigma, sigma_prior, z_cov = _laplace(obs, hp, oh_zz, oh_zr, oh_rr, coupling)
    # Not np.sort: the cone keeps them ordered, and sorting would report parameters never fitted.
    return Fit(
        mu=mu, v=v, gamma=gamma, cuts=cuts, log_nu=log_nu, r=r, s=s,
        sigma=sigma, sigma_prior=sigma_prior, z_cov=z_cov,
        anchor_curv=anchor_curv, duel_curv=duel_curv,
        objective=_objective(obs, hp, mu, v, gamma, cuts, log_nu, r,
                             with_duels=obs.duel_a.size > 0),
        grad_inf=float(grad_inf), iterations=(it_a, it_b), backtracks=bt_a + bt_b,
        # Relative, since ||grad||_inf grows with the number of observations.
        converged=bool(
            grad_inf <= 1e-3 * max(1.0, n_obs)
            and np.all(np.isfinite(s))
            and np.all(np.isfinite(sigma))
        ),
    )


def _laplace(obs: ObservationSet, hp: Hyperparams, h_zz, h_zr, h_rr, coupling):
    """σ per title, from the diagonal of the Laplace covariance (§5.2).

    Dense, because the duel coupling is off the arrowhead. The (mu, v) block gives unobserved titles a σ.
    """
    p = h_zz.shape[0]
    n = obs.n
    full = np.zeros((p + n, p + n))
    full[:p, :p] = h_zz
    full[:p, p:] = h_zr
    full[p:, :p] = h_zr.T
    full[p:, p:] = np.diag(h_rr)

    if coupling is not None:
        a, b, h_dd, h_dpsi, h_psipsi = coupling
        jac = np.concatenate([np.ones((n, 1)), obs.embeddings], axis=1)      # n x 65
        col_psi = p - 1
        # One duel adds h_dd · (ea - eb)(ea - eb)^T over s, inherited through the chain rule.
        np.add.at(full, (p + a, p + a), h_dd)
        np.add.at(full, (p + b, p + b), h_dd)
        np.add.at(full, (p + a, p + b), -h_dd)
        np.add.at(full, (p + b, p + a), -h_dd)
        np.add.at(full, (p + a, col_psi), h_dpsi)
        np.add.at(full, (col_psi, p + a), h_dpsi)
        np.add.at(full, (p + b, col_psi), -h_dpsi)
        np.add.at(full, (col_psi, p + b), -h_dpsi)
        full[col_psi, col_psi] += h_psipsi
        # …and the same contribution projected onto (mu, v).
        diff = jac[a] - jac[b]                                              # m x 65
        block = diff.T @ (diff * h_dd[:, None])
        full[:65, :65] += block
        cross = np.zeros((65, n))
        np.add.at(cross.T, a, diff * h_dd[:, None])
        np.add.at(cross.T, b, -diff * h_dd[:, None])
        full[:65, p:] += cross
        full[p:, :65] += cross.T
        psi_cross = np.zeros(65)
        np.add.at(psi_cross, np.arange(65), (diff * h_dpsi[:, None]).sum(axis=0))
        full[:65, col_psi] += psi_cross
        full[col_psi, :65] += psi_cross

    cov = np.linalg.inv(full + 1e-10 * np.eye(p + n))

    jac = np.concatenate([np.ones((n, 1)), obs.embeddings], axis=1)
    load = np.zeros((n, p + n))
    load[:, :65] = jac
    load[np.arange(n), p + np.arange(n)] = 1.0
    var = np.einsum("ij,jk,ik->i", load, cov, load)
    sigma = np.sqrt(np.maximum(var, 1e-12))

    # A title with no observations of its own; §5.2's freshness rule caps inflation here.
    z_cov = cov[:65, :65]
    prior_var = np.einsum("ij,jk,ik->i", jac, z_cov, jac) + hp.b_i_tau**2
    return sigma, np.sqrt(np.maximum(prior_var, 1e-12)), z_cov


# --- display and freshness -----------------------------------------------------------------


def empirical_cdf(reference: np.ndarray, values: np.ndarray) -> np.ndarray:
    """§5.2's displayed 0..1 weight: the mid-rank empirical CDF of the user's own `s` per kind."""
    if reference.size < 2:
        return np.full(values.shape, np.nan)
    ordered = np.sort(reference)
    left = np.searchsorted(ordered, values, side="left")
    right = np.searchsorted(ordered, values, side="right")
    return (left + right) / (2.0 * ordered.size)


def tier_of(s: np.ndarray, cuts: np.ndarray) -> np.ndarray:
    """§5.2: the tier arm's cutpoints ARE the displayed boundaries."""
    return np.searchsorted(np.sort(cuts), s, side="right").astype(np.int64)


def straddle(s: np.ndarray, sigma: np.ndarray, cuts: np.ndarray, hp: Hyperparams) -> np.ndarray:
    """§6.3's "A/S straddle" badge: the ADJACENT tier the ±z·σ interval also reaches, or −1.

    When it reaches both neighbours, the nearer cut wins; a tie goes down.
    """
    ordered = np.sort(cuts)
    tier = tier_of(s, ordered)
    out = np.full(s.shape, -1, dtype=np.int64)
    for i in range(s.size):
        here = int(tier[i])
        lo, hi = s[i] - hp.straddle_z * sigma[i], s[i] + hp.straddle_z * sigma[i]
        # Tier `here` is [ordered[here-1], ordered[here]); the end tiers have one neighbour.
        down = float(ordered[here - 1]) if here > 0 else None
        up = float(ordered[here]) if here < ordered.size else None
        reaches_down = down is not None and lo < down
        reaches_up = up is not None and hi >= up
        if reaches_down and reaches_up:
            out[i] = here - 1 if (s[i] - down) <= (up - s[i]) else here + 1
        elif reaches_down:
            out[i] = here - 1
        elif reaches_up:
            out[i] = here + 1
    return out


def inflate_sigma(
    sigma: np.ndarray, sigma_prior: np.ndarray, months_untouched: np.ndarray, hp: Hyperparams
) -> np.ndarray:
    """§5.2: after the grace period σ inflates Glicko-style at rate c per √month, capped at the prior σ."""
    over = np.maximum(months_untouched - hp.sigma_inflation_grace_months, 0.0)
    grown = np.sqrt(sigma**2 + (hp.sigma_inflation_c**2) * over)
    cap = sigma_prior if hp.sigma_inflation_cap == "prior" else np.full_like(
        sigma, float(hp.sigma_inflation_cap)
    )
    return np.minimum(grown, np.maximum(cap, sigma))


__all__ = [
    "EMBED_DIM",
    "MEASURED_TIER_SHARES",
    "OUT_A",
    "OUT_B",
    "OUT_TIE",
    "VERDICT_ANCHOR_SHARES",
    "Fit",
    "ObservationSet",
    "anchored_cuts",
    "cut_prior_mean",
    "empirical_cdf",
    "fit",
    "guess_tier",
    "hold_to_verdict",
    "inflate_sigma",
    "initial_cutpoints",
    "live_verdicts",
    "straddle",
    "tier_of",
    "verdict_class_of_tier",
    "verdict_cutpoints",
    "verdict_tiers",
]
