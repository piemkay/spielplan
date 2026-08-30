"""The Personal Ledger's maths. Spec v2.1 §5.2, §5.3, §6.3.

numpy only — no database, no clock, no torch. §5.3 puts budgets on this code ("Ledger
incremental update <50 ms", "full MAP refit … seconds"), and a budget you can only measure
through a database is a budget you are measuring something else with.

PARAMETERISATION (per user, per kind)

    s_i = mu + <v, e_i> + r_i          i indexes the user's *observed* titles of that kind

`mu` is the user's location, `v` is §5.2's "generalisation via the 64-d user vector", and `r`
is "per-title residuals b_i^u capture direct effects". An *un*observed title has no r, so its
coordinate is s = mu + <v, e> — which is how §12's M2 exit criterion ("every owned title has a
coordinate") is met at zero extra parameters.

    theta = (mu, v[64], gamma[2], cuts[K-1], psi)      dense
    r[n]                                               diagonal

`gamma` are the verdict arm's two cutpoints and `cuts` the tier arm's K-1 — free per user,
which §5.2 requires, and which is why §4.3 does not ship them.

THE FOUR ARMS, ONE LIKELIHOOD

    verdicts   ordered logit over 3 levels with cutpoints `gamma`
    tier edits ordered logit over K levels with cutpoints `cuts`, on the SAME latent — which is
               what makes "drag-and-drop is data, not override" true rather than asserted
    rewatch    no new term: a re-rating is another ordinal row, superseded and live both in
    duels      Davidson Bradley-Terry with a fitted tie parameter nu = exp(psi), margin-weighted

F is jointly convex: the ordered-logit NLL is convex in (s, cutpoints) because the logistic
density is log-concave (Pratt 1981, Burridge 1981); each Davidson term is a log-sum-exp minus a
linear term; s and d are linear in the parameters; every prior is strictly convex. So the
minimiser is unique and **any divergence is a step-size failure, never a landscape failure** —
which is exactly what §5.2's scar is about.

THE SOLVE — §5.2's Appendix C fusion

  Stage A, the ridge anchor: minimise the ordinal arms plus the priors by damped Newton. Its
  Hessian is an *arrowhead* — r_i appears only in observations of title i, so H_rr is diagonal —
  and the Schur complement solves it in O(n·p^2 + p^3) with p ≈ 74. Exactly, in single-digit
  iterations.

  Stage B, the BT perturbation, **preconditioned with the ridge Hessian**. §5.2: "fixed-step GD
  measurably diverges on episodes containing one popular title — this is a scar, keep the
  preconditioner". The reason is curvature spread: a title with 200 duels has a duel-arm
  curvature two orders of magnitude above one with two, and a single step size cannot serve
  both — it either crawls for the sparse titles or overshoots the dense one and oscillates.
  Preconditioning by the anchor Hessian divides each coordinate by its own curvature, so one
  step size is right everywhere. The anchor's arrowhead factors are reused, not rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from spielplan.ledger.hyperparams import Hyperparams

EMBED_DIM = 64

# §6.3's measured tier shape, used as the *prior mean* for the K = 7 default tier set rather
# than merely as a starting point — so a level nobody has used sits where the crowd puts it.
MEASURED_TIER_SHARES: tuple[float, ...] = (0.03, 0.07, 0.15, 0.25, 0.25, 0.17, 0.08)

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
    """Everything the fit sees for one (user, kind).

    `embeddings` is the Backbone row (warm) or the Cold Tower placement (cold) per observed
    title; `embedded` is False where neither exists, which §3.1 makes a legal state — those
    titles still get an r_i, they just do not inform v.
    """

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
    # The RAW margin (§6.1: ~1.6 decisive, ~1.0 hesitant), not a weight. Normalising it is
    # §4.3's `margin_form`, so it happens where that constant is read.
    duel_margin: np.ndarray = field(default_factory=lambda: np.zeros(0))
    n_levels: int = 7                           # K, the size of the user's tier set

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
    rho: float
    converged: bool

    @property
    def n_levels(self) -> int:
        return int(self.cuts.size) + 1


# --- the ordinal arm ----------------------------------------------------------------------


def _duel_weights(obs: ObservationSet, hp: Hyperparams) -> np.ndarray:
    """§4.3 ships "margin-weighting flag + functional form (weights normalised as
    margin/mean(margin))", so the form is applied here rather than baked into whatever wrote
    the row.

    The mean-normalisation is load-bearing: it keeps a user's total duel evidence invariant to
    how often they tap the decisive toggle, so λ_bt means the same thing for someone who always
    taps it and someone who never does.
    """
    raw = obs.duel_margin
    if raw.size == 0:
        return raw
    if not hp.margin_weighting or hp.margin_form == "none":
        return np.ones(raw.size)
    usable = np.isfinite(raw) & (raw > 0)
    if not np.any(usable):
        return np.ones(raw.size)
    weights = np.ones(raw.size)
    weights[usable] = raw[usable] / float(raw[usable].mean())
    return weights


def _ordinal_terms(s: np.ndarray, level: np.ndarray, cuts: np.ndarray):
    """Per-observation value and (a, b) derivatives of −log P(level | s, cuts).

    With sentinels c_{-1} = −inf and c_{L-1} = +inf, P(y) = σ(c_y − s) − σ(c_{y−1} − s), so the
    NLL depends on the parameters only through a = c_y − s and b = c_{y−1} − s, both linear.
    Working in (a, b) keeps the boundary levels from needing their own code path: the missing
    cutpoint simply contributes a zero derivative.
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

    # log P, in the stable form for each of the three shapes.
    with np.errstate(over="ignore", invalid="ignore"):
        interior = upper & lower
        log_p = np.empty_like(s, dtype=float)
        only_upper = upper & ~lower
        only_lower = lower & ~upper
        log_p[only_upper] = _log_sigmoid(a[only_upper])
        log_p[only_lower] = _log_sigmoid(-b[only_lower])
        if np.any(interior):
            ai, bi = a[interior], b[interior]
            gap = np.maximum(ai - bi, 1e-12)
            log_p[interior] = (
                _log_sigmoid(ai) + _log_sigmoid(-bi) + np.log(-np.expm1(-gap))
            )
    p = np.exp(log_p)
    p = np.maximum(p, 1e-300)

    f_a = -phi_a / p
    f_b = phi_b / p
    dphi_a = phi_a * (1.0 - 2.0 * sa)
    dphi_b = phi_b * (1.0 - 2.0 * sb)
    f_aa = -dphi_a / p + (phi_a / p) ** 2
    f_bb = dphi_b / p + (phi_b / p) ** 2
    f_ab = -(phi_a * phi_b) / p**2
    return -log_p, f_a, f_b, f_aa, f_ab, f_bb


# --- the duel arm -------------------------------------------------------------------------


def _duel_terms(d: np.ndarray, outcome: np.ndarray, log_nu: float):
    """Davidson (1970) with ties, in the scale-free parameterisation.

    P(A) = e^{d/2}/Z, P(B) = e^{-d/2}/Z, P(TIE) = nu/Z with Z = e^{d/2} + e^{-d/2} + nu. This
    is pi_i/(pi_i + pi_j + nu*sqrt(pi_i pi_j)) with pi = e^s, divided through by
    e^{(s_a+s_b)/2}; nu -> 0 recovers plain Bradley-Terry.
    """
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
    """Cutpoints whose implied level shares match §6.3's measured tier distribution.

    For the default K = 7 that is the measured shape; for any other tier set the household
    configured, equal mass — there is no measurement for a set nobody has used, and inventing
    one would be a number with no provenance.
    """
    shares = MEASURED_TIER_SHARES if k == len(MEASURED_TIER_SHARES) else (1.0 / k,) * k
    cumulative = np.cumsum(np.asarray(shares, dtype=float))[:-1]
    return np.log(cumulative / (1.0 - cumulative))


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

    cuts_init = initial_cutpoints(obs.n_levels)
    total += 0.5 * hp.lambda_ridge * float(v @ v)
    total += 0.5 * float(r @ r) / hp.b_i_tau**2
    total += 0.5 * mu**2 / hp.mu_prior_tau**2
    total += 0.5 * hp.cutpoint_prior_precision * float(np.sum((cuts - cuts_init) ** 2))
    total += 0.5 * hp.tie_prior_precision * (log_nu - np.log(hp.nu0())) ** 2
    total += 0.5 * hp.cutpoint_prior_precision * float(np.sum((gamma - initial_cutpoints(3)) ** 2))
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

    # The duel arm's curvature is NOT diagonal in s: a duel couples its two titles with an
    # off-diagonal -w·h_dd, and for the shared coordinates those cancel exactly — shifting
    # every title equally changes no difference, so a duel contributes nothing to the
    # curvature of mu. Folding h_dd into the diagonal and stopping there overstates H by up to
    # a factor of two in a duel-heavy fit, and silently corrupts sigma. So the coupling is
    # returned separately: the arrowhead blocks below stay the ORDINAL Hessian (genuinely
    # diagonal in r, which is what makes the anchor solve exact and fast), and the duel term
    # is assembled where it is actually needed.
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

    # priors
    cuts_init = initial_cutpoints(obs.n_levels)
    gamma_init = initial_cutpoints(3)
    g_extra[: lay.n_gamma] += hp.cutpoint_prior_precision * (gamma - gamma_init)
    g_extra[lay.n_gamma : lay.n_gamma + lay.n_cuts] += hp.cutpoint_prior_precision * (
        cuts - cuts_init
    )
    g_extra[-1] += hp.tie_prior_precision * (log_nu - np.log(hp.nu0()))
    for j in range(lay.n_gamma + lay.n_cuts):
        d_extra[j, j] += hp.cutpoint_prior_precision
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


def _schur_solve(h_zz, h_zr, h_rr, g_z, g_r):
    """One arrowhead solve. H_rr is diagonal because r_i appears only in title i's own
    observations, which is what makes this O(n·p^2 + p^3) rather than O((n+p)^3)."""
    inv_rr = 1.0 / h_rr
    schur = h_zz - (h_zr * inv_rr[None, :]) @ h_zr.T
    rhs = g_z - (h_zr * inv_rr[None, :]) @ g_r
    # A tiny jitter keeps Cholesky from failing on a problem that is convex but, with no
    # observations at all, only positive *semi*-definite.
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


# §5.2 mandates preconditioning by the ridge Hessian. It does not mandate evaluating it once
# and never again — and at 839 titles a preconditioner frozen at the anchor leaves the fit
# short of the optimum inside the bundle's step budget. Re-deriving it costs one O(n·p²) pass
# (a few milliseconds at household scale), so it is refreshed periodically at the current
# point. Still the ridge Hessian; still not the true curvature; just not stale.
PRECONDITIONER_REFRESH = 5


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
        mu, v, gamma, cuts, log_nu = _unpack(z, lay)
        g_z, g_r, h_zz, h_zr, h_rr, anchor_curv, duel_curv, _cpl = _grad_hess(
            obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels
        )
        grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))
        if grad0 is None:
            grad0 = grad_inf
        if grad_inf < hp.newton_tol * (1.0 + grad0):
            break

        if refresh_preconditioner and iterations % PRECONDITIONER_REFRESH == 1 and iterations > 1:
            a_zz, a_zr, a_rr = _grad_hess(
                obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=False
            )[2:5]
            precondition_from = (a_zz, a_zr, a_rr)

        if precondition_from is None:
            dz, dr, _ = _schur_solve(h_zz, h_zr, h_rr, g_z, g_r)
        else:
            # Stage B: step with the *anchor's* curvature, not the current one. This is the
            # scar §5.2 names — the duel arm's curvature spread is what a fixed step cannot
            # serve, and dividing by the anchor's curvature makes one step size right for a
            # title with two duels and one with two hundred alike.
            p_zz, p_zr, p_rr = precondition_from
            dz, dr, _ = _schur_solve(p_zz, p_zr, p_rr, g_z, g_r)

        f0 = _objective(obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels)
        slope = float(g_z @ dz + g_r @ dr)
        # Carry the accepted step across iterations, trying twice the last one first. The
        # anchor preconditioner's mismatch with the true curvature is roughly constant along
        # the path, so restarting the search at step0 every iteration re-pays the same
        # halvings hundreds of times over.
        eta = min(1.0, max(step0, eta * 2.0))
        while True:
            z_try, r_try = z - eta * dz, r - eta * dr
            m2, v2, ga2, c2, ln2 = _unpack(z_try, lay)
            f1 = _objective(obs, hp, m2, v2, ga2, c2, ln2, r_try, with_duels=with_duels)
            if f1 <= f0 - 1e-4 * eta * slope or eta < hp.lr_min:
                break
            eta *= 0.5
            backtracks += 1
        z, r = z - eta * dz, r - eta * dr
        if eta < hp.lr_min:
            break

    mu, v, gamma, cuts, log_nu = _unpack(z, lay)
    g_z, g_r, h_zz, h_zr, h_rr, anchor_curv, duel_curv, _cpl = _grad_hess(
        obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=with_duels
    )
    grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))
    return z, r, (h_zz, h_zr, h_rr), grad_inf, iterations, backtracks, anchor_curv, duel_curv


def fit(obs: ObservationSet, hp: Hyperparams) -> Fit:
    """The full MAP fit: ridge anchor, then the preconditioned BT perturbation."""
    lay = _Layout(obs.n, obs.n_levels)
    z = _pack(0.0, np.zeros(EMBED_DIM), initial_cutpoints(3), initial_cutpoints(obs.n_levels),
              float(np.log(hp.nu0())))
    r = np.zeros(obs.n)

    # Stage A — the ordinal arms alone, solved exactly.
    z, r, blocks_a, _g_a, it_a, bt_a, anchor_curv, _ = _minimise(
        obs, hp, with_duels=False, z0=z, r0=r
    )

    # Stage B — add the duel arm, stepping with stage A's curvature.
    rho = 0.0
    it_b = bt_b = 0
    if obs.duel_a.size:
        mu, v, gamma, cuts, log_nu = _unpack(z, lay)
        _gz, _gr, _hzz, _hzr, _hrr, anchor_diag, duel_diag, _cpl = _grad_hess(
            obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=True
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(anchor_diag > 0, duel_diag / anchor_diag, 0.0)
        rho = float(np.max(ratio, initial=0.0))
        # §4.3's `lr` is the first trial step; Armijo grows it (x2 per iteration, capped at a
        # unit step, since the anchor-preconditioned direction is approximately Newton) and
        # shortens it when the objective disagrees. `rho` — the largest ratio of duel curvature
        # to anchor curvature over the titles — is what a *fixed-step* method would have had to
        # divide by. It is reported rather than applied: it measures the mismatch the
        # preconditioner absorbs, and watching it is how a later reader sees the scar §5.2
        # describes without having to reproduce the divergence.
        step0 = float(hp.lr)
        z, r, blocks_b, _g_b, it_b, bt_b, anchor_curv, duel_curv = _minimise(
            obs, hp, with_duels=True, z0=z, r0=r,
            precondition_from=blocks_a, max_iter=hp.steps, step0=step0,
            refresh_preconditioner=True,
        )
        blocks = blocks_b
    else:
        duel_curv = np.zeros(obs.n)
        blocks = blocks_a

    mu, v, gamma, cuts, log_nu = _unpack(z, lay)
    s = mu + obs.embeddings @ v + r
    h_zz, h_zr, h_rr = blocks
    g_z, g_r, oh_zz, oh_zr, oh_rr, _ac, _dc, coupling = _grad_hess(
        obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=obs.duel_a.size > 0
    )
    grad_inf = max(np.abs(g_z).max(initial=0.0), np.abs(g_r).max(initial=0.0))

    n_obs = obs.ord_index.size + obs.duel_a.size
    sigma, sigma_prior, z_cov = _laplace(obs, hp, oh_zz, oh_zr, oh_rr, coupling)
    return Fit(
        mu=mu, v=v, gamma=np.sort(gamma), cuts=np.sort(cuts), log_nu=log_nu, r=r, s=s,
        sigma=sigma, sigma_prior=sigma_prior, z_cov=z_cov,
        anchor_curv=anchor_curv, duel_curv=duel_curv,
        objective=_objective(obs, hp, mu, v, gamma, cuts, log_nu, r,
                             with_duels=obs.duel_a.size > 0),
        grad_inf=float(grad_inf), iterations=(it_a, it_b), backtracks=bt_a + bt_b,
        rho=rho,
        # Relative to the problem's size, not absolute: ||grad||_inf grows with the number of
        # observations, so an absolute threshold would call a 50-verdict fit converged and an
        # identically-good 800-verdict one diverged. `n_obs` is the scale the gradient is
        # measured against.
        converged=bool(grad_inf <= 1e-3 * max(1.0, n_obs)),
    )


def _laplace(obs: ObservationSet, hp: Hyperparams, h_zz, h_zr, h_rr, coupling):
    """σ per title, from the diagonal of the Laplace covariance (§5.2's "Laplace diagonal").

    Assembled densely, and deliberately. The ordinal arms alone give an arrowhead Hessian whose
    r block is diagonal — which is what makes the anchor solve exact and fast — but the duel arm
    couples the two titles of every pair, so the true Hessian is arrowhead PLUS a sparse
    symmetric coupling. Pretending otherwise inflates the curvature by up to a factor of two in
    a duel-heavy fit and hands back a σ that is confidently wrong. At household scale the dense
    assembly is a (p+n)² matrix with n ≤ ~839, which is one nightly inversion, so correctness
    costs nothing worth having.

    The (mu, v) block is returned too: it is what gives an *unobserved* title a σ, since its s
    is mu + ⟨v, e⟩ with no residual at all.
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
        # d in s-space: +1 on a, -1 on b. The Hessian contribution of one duel is
        # h_dd · (ea - eb)(ea - eb)^T over the s coordinates, which every parameter that
        # moves s inherits through the chain rule.
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

    # The prior σ is what a title with no observations of its own would carry: the (mu, v)
    # uncertainty plus the residual prior. §5.2's freshness rule caps inflation there.
    z_cov = cov[:65, :65]
    prior_var = np.einsum("ij,jk,ik->i", jac, z_cov, jac) + hp.b_i_tau**2
    return sigma, np.sqrt(np.maximum(prior_var, 1e-12)), z_cov


# --- display and freshness -----------------------------------------------------------------


def empirical_cdf(reference: np.ndarray, values: np.ndarray) -> np.ndarray:
    """§5.2's displayed 0..1 weight: "the empirical CDF of the user's own fitted `s` values,
    computed per kind (their best-ranked title → ~1.0, worst → ~0.0)".

    Mid-rank, so ties share a value and the mapping is stable under any monotone rescaling of
    s — which is the property that makes it the owner's "always-preferred → 1.0" definition
    rather than an artefact of the scale s happens to be fitted on.
    """
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
    """§6.3's "A/S straddle" badge: the adjacent tier the posterior also reaches, or −1."""
    ordered = np.sort(cuts)
    tier = tier_of(s, ordered)
    out = np.full(s.shape, -1, dtype=np.int64)
    for i in range(s.size):
        lo, hi = s[i] - hp.straddle_z * sigma[i], s[i] + hp.straddle_z * sigma[i]
        below = int(np.searchsorted(ordered, lo, side="right"))
        above = int(np.searchsorted(ordered, hi, side="right"))
        if below != tier[i]:
            out[i] = below
        elif above != tier[i]:
            out[i] = above
    return out


def inflate_sigma(
    sigma: np.ndarray, sigma_prior: np.ndarray, months_untouched: np.ndarray, hp: Hyperparams
) -> np.ndarray:
    """§5.2: "after 12 months untouched, a title's σ inflates Glicko-style at rate c per √month,
    capped at the prior σ" — "ambient recalibration rather than chores"."""
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
    "Fit",
    "ObservationSet",
    "empirical_cdf",
    "fit",
    "inflate_sigma",
    "initial_cutpoints",
    "straddle",
    "tier_of",
]
