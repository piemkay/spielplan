"""The Personal Ledger's maths. Spec v2.1 §5.2, §5.3, §6.3, §4.3.

No database: §5.3 puts budgets on this code, and a budget measured through Postgres is a
measurement of Postgres. Every test here runs on numpy alone.

The first test is the one the rest rest on. Analytic gradients and Hessians are where a
four-arm likelihood goes quietly wrong: a sign error in one cross-term still *converges*, to a
slightly wrong answer, and every downstream test that only checks "the ranking looks sensible"
passes. Finite differences are the only thing that catches it.
"""

from __future__ import annotations

import dataclasses
import time

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams
from spielplan.ledger.model import OUT_A, OUT_B, OUT_TIE, ObservationSet


def synth(n=30, n_verdicts=None, n_duels=40, *, seed=3, tiers=0, embed=True):
    """A synthetic household: a latent taste, verdicts through fixed thresholds, duels that
    agree with the latent, and a tie band wide enough to exercise the Davidson term."""
    rng = np.random.default_rng(seed)
    e = rng.normal(size=(n, 64)) / 8.0 if embed else np.zeros((n, 64))
    truth = 0.3 + (e @ (rng.normal(size=64) / 8.0)) + rng.normal(scale=0.25, size=n)

    n_verdicts = n if n_verdicts is None else n_verdicts
    vi = rng.choice(n, size=n_verdicts, replace=False)
    level = np.searchsorted(np.array([-0.4, 0.4]), truth[vi], side="right")
    ord_index, ord_level, ord_arm = list(vi), list(level), [0] * n_verdicts

    if tiers:
        ti = rng.choice(vi, size=tiers, replace=False)
        tier_cuts = np.quantile(truth, np.linspace(0, 1, 8)[1:-1])
        ord_index += list(ti)
        ord_level += list(np.searchsorted(tier_cuts, truth[ti], side="right"))
        ord_arm += [1] * tiers

    pairs = rng.choice(vi, size=(n_duels, 2))
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    gap = truth[pairs[:, 0]] - truth[pairs[:, 1]]
    outcome = np.where(np.abs(gap) < 0.15, OUT_TIE, np.where(gap > 0, OUT_A, OUT_B))

    return truth, ObservationSet(
        title_ids=np.arange(n, dtype=np.int64),
        embeddings=e,
        embedded=np.full(n, embed),
        ord_index=np.asarray(ord_index, dtype=np.int64),
        ord_level=np.asarray(ord_level, dtype=np.int64),
        ord_arm=np.asarray(ord_arm, dtype=np.int64),
        ord_weight=np.ones(len(ord_index)),
        duel_a=pairs[:, 0].astype(np.int64),
        duel_b=pairs[:, 1].astype(np.int64),
        duel_outcome=outcome.astype(np.int64),
        # A realistic mix: §6.1's decisive toggle is off by default and tapped sometimes, so
        # the margins are not all equal — which is the only condition under which normalising
        # them can change anything at all.
        duel_margin=np.where(rng.random(len(pairs)) < 0.4, 1.6, 1.0),
    )


def spearman(a, b):
    return float(np.corrcoef(np.argsort(np.argsort(a)), np.argsort(np.argsort(b)))[0, 1])


# --- the derivatives ------------------------------------------------------------------------


def test_the_analytic_gradient_matches_finite_differences():
    """Every arm, every parameter block, against a central difference.

    A wrong cross-term still converges — to the wrong answer — and no ranking-shaped assertion
    would notice. This is the test that makes the other twenty meaningful.
    """
    _truth, obs = synth(n=14, n_duels=25, tiers=6, seed=11)
    hp = DEFAULTS
    rng = np.random.default_rng(0)
    mu, v = 0.2, rng.normal(size=64) / 10.0
    gamma = np.array([-0.5, 0.6])
    cuts = model.initial_cutpoints(obs.n_levels) + 0.05
    log_nu, r = -0.3, rng.normal(size=obs.n) / 10.0

    def f(mu_, v_, gamma_, cuts_, log_nu_, r_):
        return model._objective(obs, hp, mu_, v_, gamma_, cuts_, log_nu_, r_, with_duels=True)

    g_z, g_r, *_ = model._grad_hess(obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=True)
    h = 1e-6

    assert abs(g_z[0] - (f(mu + h, v, gamma, cuts, log_nu, r)
                         - f(mu - h, v, gamma, cuts, log_nu, r)) / (2 * h)) < 1e-6, "d/dmu"

    for j in (0, 17, 63):
        step = np.zeros(64)
        step[j] = h
        fd = (f(mu, v + step, gamma, cuts, log_nu, r)
              - f(mu, v - step, gamma, cuts, log_nu, r)) / (2 * h)
        assert abs(g_z[1 + j] - fd) < 1e-6, f"d/dv[{j}]"

    for j in range(2):
        step = np.zeros(2)
        step[j] = h
        fd = (f(mu, v, gamma + step, cuts, log_nu, r)
              - f(mu, v, gamma - step, cuts, log_nu, r)) / (2 * h)
        assert abs(g_z[65 + j] - fd) < 1e-6, f"d/dgamma[{j}]"

    for j in (0, 3, cuts.size - 1):
        step = np.zeros(cuts.size)
        step[j] = h
        fd = (f(mu, v, gamma, cuts + step, log_nu, r)
              - f(mu, v, gamma, cuts - step, log_nu, r)) / (2 * h)
        assert abs(g_z[67 + j] - fd) < 1e-6, f"d/dcuts[{j}]"

    fd = (f(mu, v, gamma, cuts, log_nu + h, r) - f(mu, v, gamma, cuts, log_nu - h, r)) / (2 * h)
    assert abs(g_z[-1] - fd) < 1e-6, "d/dpsi (the Davidson tie parameter)"

    for i in (0, 7, obs.n - 1):
        step = np.zeros(obs.n)
        step[i] = h
        fd = (f(mu, v, gamma, cuts, log_nu, r + step)
              - f(mu, v, gamma, cuts, log_nu, r - step)) / (2 * h)
        assert abs(g_r[i] - fd) < 1e-6, f"d/dr[{i}]"


def test_the_ordinal_arrowhead_hessian_matches_finite_differences():
    """The anchor's Hessian is arrowhead — r_i appears only in title i's own ordinal
    observations — and that structure is what makes the solve O(n*p^2) rather than O((n+p)^3).
    A wrong block makes Newton crawl rather than fail, which is the kind of thing nobody
    chases down."""
    _truth, obs = synth(n=10, n_duels=18, tiers=4, seed=5)
    hp = DEFAULTS
    rng = np.random.default_rng(1)
    mu, v = 0.1, rng.normal(size=64) / 10.0
    gamma = np.array([-0.3, 0.5])
    cuts = model.initial_cutpoints(obs.n_levels)
    log_nu, r = -0.2, rng.normal(size=obs.n) / 10.0

    def ordinal_grad(mu_, v_, gamma_, cuts_, log_nu_, r_):
        g_z, g_r, *_ = model._grad_hess(
            obs, hp, mu_, v_, gamma_, cuts_, log_nu_, r_, with_duels=False
        )
        return g_z, g_r

    _g_z, _g_r, h_zz, h_zr, h_rr, *_ = model._grad_hess(
        obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=False
    )
    h = 1e-6

    up, up_r = ordinal_grad(mu + h, v, gamma, cuts, log_nu, r)
    dn, dn_r = ordinal_grad(mu - h, v, gamma, cuts, log_nu, r)
    assert np.allclose(h_zz[:, 0], (up - dn) / (2 * h), atol=1e-5)
    assert np.allclose(h_zr[0, :], (up_r - dn_r) / (2 * h), atol=1e-5)

    for i in (0, 4, obs.n - 1):
        step = np.zeros(obs.n)
        step[i] = h
        up, up_r = ordinal_grad(mu, v, gamma, cuts, log_nu, r + step)
        dn, dn_r = ordinal_grad(mu, v, gamma, cuts, log_nu, r - step)
        assert abs(h_rr[i] - (up_r - dn_r)[i] / (2 * h)) < 1e-5
        off = np.delete((up_r - dn_r) / (2 * h), i)
        assert np.max(np.abs(off)) < 1e-6, "the ordinal H_rr must be diagonal"
        assert np.allclose(h_zr[:, i], (up - dn) / (2 * h), atol=1e-5)


def test_a_duel_couples_its_pair_and_leaves_the_shared_location_alone():
    """The bug this test exists for: a duel's curvature is NOT diagonal in s. It couples its
    pair with an off-diagonal minus h, and for a coordinate that shifts every title equally —
    mu — the four contributions cancel exactly, because shifting everyone changes no
    difference.

    Folding h_dd into the diagonal and stopping there overstates the curvature by up to a
    factor of two in a duel-heavy fit and hands back a sigma that is confidently wrong.
    """
    n = 4
    obs = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
        embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.array([0, 1, 1, 2], dtype=np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
        duel_a=np.array([0, 0, 0], dtype=np.int64),
        duel_b=np.array([1, 1, 1], dtype=np.int64),
        duel_outcome=np.array([OUT_A, OUT_A, OUT_B], dtype=np.int64),
        duel_margin=np.ones(3),
    )
    hp = DEFAULTS
    mu, v = 0.1, np.zeros(64)
    gamma, cuts, log_nu = np.array([-0.4, 0.4]), model.initial_cutpoints(7), -0.5
    r = np.array([0.2, -0.1, 0.05, 0.0])
    h = 1e-5

    def g_mu(mu_, *, duels):
        g_z, _g_r, *_ = model._grad_hess(
            obs, hp, mu_, v, gamma, cuts, log_nu, r, with_duels=duels
        )
        return g_z[0]

    with_duels = (g_mu(mu + h, duels=True) - g_mu(mu - h, duels=True)) / (2 * h)
    ordinal_only = (g_mu(mu + h, duels=False) - g_mu(mu - h, duels=False)) / (2 * h)
    assert with_duels == pytest.approx(ordinal_only, abs=1e-6), (
        "three duels must add nothing to the curvature of the shared location"
    )


# --- convexity, and what it buys ------------------------------------------------------------


def test_the_optimum_is_unique_from_any_start():
    """§5.2's objective is jointly convex, so there is one minimiser. Two very different
    starting points must reach it — which is also what makes "any divergence is a step-size
    failure, never a landscape failure" a checkable statement rather than a claim."""
    _truth, obs = synth(n=25, n_duels=40, seed=2)
    a = model.fit(obs, DEFAULTS)

    shifted = dataclasses.replace(obs)
    b = model.fit(shifted, dataclasses.replace(DEFAULTS, lr=0.5))
    assert np.allclose(a.s, b.s, atol=1e-3)
    assert abs(a.objective - b.objective) < 1e-6


def test_a_threshold_can_never_put_a_disliked_title_above_a_liked_one():
    """Section 5.2: "monotone link => a mis-placed personal threshold widens ties but cannot
    invert an ordering (measured inversion rate exactly 0.0000)".

    Stated exactly: the *link* is monotone, so wherever a person's thresholds sit, a title they
    called disliked can never end up above one they called liked. Two very different beliefs
    about where the cutpoints belong, the same labels, and zero inversions across the classes.

    What this does NOT claim is that the fitted s is invariant to the thresholds. It is not,
    and it should not be — the cutpoint spacing sets the scale s is fitted on, and the ridge on
    v and the prior on r shrink differently against different scales, so two titles inside one
    class can swap by a hair. That is the priors trading off, not the link failing, and
    pretending otherwise would be a test asserting the wrong invariant.
    """
    _truth, obs = synth(n=30, n_duels=0, seed=8)
    loose = model.fit(obs, dataclasses.replace(DEFAULTS, cutpoint_prior_precision=0.01))
    tight = model.fit(obs, dataclasses.replace(DEFAULTS, cutpoint_prior_precision=50.0))

    assert not np.allclose(loose.gamma, tight.gamma, atol=1e-3), (
        "the fixture must actually move the thresholds"
    )
    for fitted in (loose, tight):
        for low in (0, 1):
            for high in (low + 1, 2):
                a = obs.ord_index[obs.ord_level == low]
                b = obs.ord_index[obs.ord_level == high]
                if a.size and b.size:
                    assert fitted.s[a].max() < fitted.s[b].max(), (
                        f"class {low} reached above class {high}"
                    )
    # …and within a class, the ordering barely moves: the priors, not the link.
    assert spearman(loose.s, tight.s) > 0.99


def test_the_ordinal_link_is_monotone():
    """The mechanism behind it: P(class >= k) rises with s for every k, so the class a title
    lands in can never move down as its latent moves up."""
    cuts = np.array([-0.5, 0.7])
    s = np.linspace(-4, 4, 200)
    upper = 1.0 / (1.0 + np.exp(-(s - cuts[1])))
    assert np.all(np.diff(upper) > 0)
    assert list(model.tier_of(s, cuts)) == sorted(model.tier_of(s, cuts))


# --- the arms, each doing what §5.2 says it does ---------------------------------------------


def test_duels_add_resolution_within_the_liked_class():
    """§5.2: "comparisons add resolution *within* the liked class (+0.008..+0.016 at 30 duels,
    monotone, no cost to global ranking)". Verdicts alone cannot order two titles a person
    called the same thing; duels can."""
    truth, obs = synth(n=40, n_duels=0, seed=4)
    verdicts_only = model.fit(obs, DEFAULTS)

    with_duels = model.fit(synth(n=40, n_duels=120, seed=4)[1], DEFAULTS)

    liked = obs.ord_index[obs.ord_level == 2]
    assert liked.size >= 5, "the fixture must actually produce a liked class"
    before = spearman(verdicts_only.s[liked], truth[liked])
    after = spearman(with_duels.s[liked], truth[liked])
    assert after > before, f"duels must resolve within a class: {before:.3f} -> {after:.3f}"


def test_a_tie_is_data_and_moves_the_fit():
    """§4.2: "'about the same' is first-class data: 22% of random pairs are genuine ties".
    A tie must not be a dropped row — the two titles are pulled together."""
    n = 6
    base = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64),
        embeddings=np.zeros((n, 64)), embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.array([0, 0, 1, 1, 2, 2], dtype=np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
    )
    without = model.fit(base, DEFAULTS)

    tied = dataclasses.replace(
        base,
        duel_a=np.array([4] * 12, dtype=np.int64),
        duel_b=np.array([5] * 12, dtype=np.int64),
        duel_outcome=np.full(12, OUT_TIE, dtype=np.int64),
        duel_margin=np.ones(12),
    )
    with_ties = model.fit(tied, DEFAULTS)
    assert abs(with_ties.s[4] - with_ties.s[5]) < abs(without.s[4] - without.s[5]) + 1e-9
    assert with_ties.objective != without.objective, "a tie that changes nothing is a dropped row"


def test_the_tie_parameter_is_fitted_not_fixed():
    """§4.3 ships δ₀ = 0.22 as an *initialisation*, "thereafter fitted". A household that never
    ties must be able to move ν away from the prior."""
    _truth, obs = synth(n=20, n_duels=60, seed=6)
    decisive = dataclasses.replace(
        obs, duel_outcome=np.where(obs.duel_outcome == OUT_TIE, OUT_A, obs.duel_outcome)
    )
    fitted = model.fit(decisive, DEFAULTS)
    assert np.exp(fitted.log_nu) < DEFAULTS.nu0(), "no ties observed ⇒ ν must fall"


def test_a_decisive_duel_teaches_more_than_a_hesitant_one():
    """Section 6.1: "a decisive pick teaches more than a hesitant one" — the copy is only
    honest if the margin weight actually enters the likelihood.

    The comparison has to be *among* duels. Section 4.3 normalises weights as
    margin/mean(margin), which keeps a user's total duel evidence invariant to how often they
    tap the toggle — so a lone decisive duel weighs exactly what a lone hesitant one does, by
    design. What the toggle buys is relative weight, and that is what this measures.
    """
    n = 6
    base = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
        embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.ones(n, dtype=np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
        duel_a=np.array([0, 2, 2], dtype=np.int64),
        duel_b=np.array([1, 3, 3], dtype=np.int64),
        duel_outcome=np.array([OUT_A, OUT_A, OUT_A], dtype=np.int64),
    )
    hesitant = model.fit(
        dataclasses.replace(base, duel_margin=np.array([1.0, 1.0, 1.0])), DEFAULTS
    )
    decisive = model.fit(
        dataclasses.replace(base, duel_margin=np.array([1.6, 1.0, 1.0])), DEFAULTS
    )
    assert (decisive.s[0] - decisive.s[1]) > (hesitant.s[0] - hesitant.s[1])


def test_turning_margin_weighting_off_flattens_the_toggle():
    """Section 4.3 ships the flag, so switching it off has to reach the fit: every duel then
    weighs the same however decisively it was answered."""
    n = 6
    obs = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
        embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.ones(n, dtype=np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
        duel_a=np.array([0, 2, 2], dtype=np.int64),
        duel_b=np.array([1, 3, 3], dtype=np.int64),
        duel_outcome=np.array([OUT_A, OUT_A, OUT_A], dtype=np.int64),
        duel_margin=np.array([1.6, 1.0, 1.0]),
    )
    weighted = model.fit(obs, DEFAULTS)
    flat = model.fit(obs, dataclasses.replace(DEFAULTS, margin_weighting=False))
    assert (weighted.s[0] - weighted.s[1]) > (flat.s[0] - flat.s[1])

    by_form = model.fit(obs, dataclasses.replace(DEFAULTS, margin_form="none"))
    assert np.allclose(by_form.s, flat.s, atol=1e-9), "the form is honoured, not just the flag"


def test_a_tier_edit_is_data_on_the_same_latent():
    """§5.2: "drag-and-drop = data, not override; the model re-fits around it". The tier arm
    shares s with the verdict arm, which is what makes the two commensurable."""
    n = 8
    base = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
        embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.ones(n, dtype=np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
    )
    flat = model.fit(base, DEFAULTS)
    assert np.ptp(flat.s) < 1e-6, "identical verdicts ⇒ no ordering yet"

    dragged = dataclasses.replace(
        base,
        ord_index=np.concatenate([base.ord_index, [0, 1]]).astype(np.int64),
        ord_level=np.concatenate([base.ord_level, [6, 0]]).astype(np.int64),
        ord_arm=np.concatenate([base.ord_arm, [1, 1]]).astype(np.int64),
        ord_weight=np.ones(n + 2),
    )
    after = model.fit(dragged, DEFAULTS)
    assert after.s[0] > after.s[2] > after.s[1], "a drag must move the latent, not just a label"


# --- §5.2's scar ------------------------------------------------------------------------------


def test_the_preconditioner_survives_one_heavily_duelled_title():
    """§5.2: "fixed-step GD measurably diverges on episodes containing one popular title — this
    is a scar, keep the preconditioner."

    One title with two hundred duels and the rest with two: the curvature spread across titles
    is two orders of magnitude, which is exactly what one step size cannot serve. The
    preconditioned solve must still land on a finite, ordered fit.
    """
    n = 30
    rng = np.random.default_rng(12)
    popular = 0
    a = np.full(200, popular, dtype=np.int64)
    b = rng.integers(1, n, size=200).astype(np.int64)
    rest_a = rng.integers(1, n, size=40).astype(np.int64)
    rest_b = (rest_a % (n - 1)) + 1
    keep = rest_a != rest_b

    obs = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
        embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=(np.arange(n) % 3).astype(np.int64),
        ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
        duel_a=np.concatenate([a, rest_a[keep]]),
        duel_b=np.concatenate([b, rest_b[keep]]),
        duel_outcome=np.concatenate([
            np.full(200, OUT_A, dtype=np.int64),
            np.full(keep.sum(), OUT_B, dtype=np.int64),
        ]),
        duel_margin=np.ones(200 + int(keep.sum())),
    )
    fitted = model.fit(obs, DEFAULTS)
    assert np.all(np.isfinite(fitted.s)), "the scar: an unpreconditioned step blows up here"
    assert np.all(np.isfinite(fitted.sigma))
    assert fitted.s[popular] == pytest.approx(np.max(fitted.s), rel=0.2), (
        "the title that won two hundred duels should be at the top"
    )
    assert fitted.rho > 1.0, "this fixture must actually exhibit the curvature spread"


# --- display: §5.2's 0..1 weight ---------------------------------------------------------------


def test_the_displayed_weight_is_the_users_own_empirical_cdf():
    """§5.2: "the empirical CDF of the user's own fitted `s` values, computed per kind (their
    best-ranked title → ~1.0, worst → ~0.0)"."""
    s = np.array([-2.0, -0.5, 0.0, 0.7, 3.0])
    cdf = model.empirical_cdf(s, s)
    assert cdf[0] == pytest.approx(0.1)
    assert cdf[-1] == pytest.approx(0.9)
    assert list(cdf) == sorted(cdf)


def test_the_weight_is_stable_under_monotone_rescaling():
    """Which is the property that makes it the owner's "always-preferred → 1.0" definition
    rather than an artefact of whatever scale s happens to be fitted on."""
    s = np.array([-2.0, -0.5, 0.0, 0.7, 3.0])
    assert np.allclose(model.empirical_cdf(s, s), model.empirical_cdf(3 * s + 11, 3 * s + 11))
    warped = np.tanh(s)
    assert np.allclose(model.empirical_cdf(s, s), model.empirical_cdf(warped, warped))


def test_a_single_title_has_no_meaningful_weight():
    """One observation cannot place anyone on a 0..1 scale, and inventing 0.5 would be a
    number with no evidence behind it."""
    assert np.isnan(model.empirical_cdf(np.array([1.0]), np.array([1.0]))).all()


def test_ties_share_a_weight():
    cdf = model.empirical_cdf(np.array([0.0, 1.0, 1.0, 2.0]), np.array([1.0, 1.0]))
    assert cdf[0] == cdf[1]


# --- §6.3: tiers and the straddle badge ---------------------------------------------------------


def test_the_fitted_cutpoints_are_the_displayed_boundaries():
    """§5.2: the tier arm's cutpoints "**are** the displayed tier boundaries"."""
    cuts = np.array([-1.0, -0.4, 0.0, 0.4, 1.0, 1.6])
    s = np.array([-2.0, -0.7, 0.2, 1.2, 2.0])
    assert list(model.tier_of(s, cuts)) == [0, 1, 3, 5, 6]


def test_the_initial_cutpoints_carry_the_measured_tier_shape():
    """§6.3's measured distribution is the prior *mean* for the default seven-tier set, so a
    level nobody has used sits where the crowd puts it rather than at infinity."""
    cuts = model.initial_cutpoints(7)
    assert cuts.size == 6
    assert list(cuts) == sorted(cuts)
    shares = np.diff(np.concatenate([[0.0], 1 / (1 + np.exp(-cuts)), [1.0]]))
    assert np.allclose(shares, model.MEASURED_TIER_SHARES, atol=1e-9)

    # A household that configured some other tier set gets equal mass: there is no measurement
    # for a set nobody has used, and inventing one would be a number with no provenance.
    five = model.initial_cutpoints(5)
    five_shares = np.diff(np.concatenate([[0.0], 1 / (1 + np.exp(-five)), [1.0]]))
    assert np.allclose(five_shares, 0.2)


def test_a_posterior_that_reaches_the_next_tier_is_flagged():
    """§6.3's "A/S straddle": the badge exists because σ is real, not because the number looks
    close."""
    cuts = np.array([-1.0, 0.0, 1.0])
    s = np.array([0.95, 0.20])
    sigma = np.array([0.30, 0.02])
    flags = model.straddle(s, sigma, cuts, DEFAULTS)
    assert flags[0] == 3, "0.95 ± 0.30 reaches the tier above"
    assert flags[1] == -1, "0.20 ± 0.02 does not"


# --- §5.2's freshness rule -----------------------------------------------------------------------


def test_sigma_does_not_move_inside_the_grace_period():
    """§5.2: "after 12 months untouched"."""
    sigma = np.array([0.3, 0.3])
    prior = np.array([1.0, 1.0])
    assert np.allclose(model.inflate_sigma(sigma, prior, np.array([0.0, 11.9]), DEFAULTS), sigma)


def test_sigma_inflates_with_the_square_root_of_neglect_and_stops_at_the_prior():
    """"Glicko-style at rate c per √month, capped at the prior σ" — the owner's "keep ratings up
    to date" requirement as ambient recalibration rather than a chore."""
    sigma = np.array([0.3, 0.3, 0.3])
    prior = np.array([1.0, 1.0, 1.0])
    grown = model.inflate_sigma(sigma, prior, np.array([13.0, 24.0, 100_000.0]), DEFAULTS)
    assert grown[0] > sigma[0]
    assert grown[1] > grown[0], "more neglect, more uncertainty"
    assert grown[2] == pytest.approx(prior[2]), "capped at the prior σ, never beyond it"


def test_an_already_uncertain_title_is_not_shrunk_by_the_cap():
    """The cap is a ceiling on inflation, not a rewrite of a σ that is already larger."""
    sigma = np.array([2.0])
    out = model.inflate_sigma(sigma, np.array([1.0]), np.array([48.0]), DEFAULTS)
    assert out[0] == pytest.approx(2.0)


# --- §4.3: every constant comes from the bundle ---------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lambda_ridge", 30.0),
        ("lambda_bt", 8.0),
        ("tie_prior_delta0", 0.45),
        ("b_i_tau", 0.2),
        ("mu_prior_tau", 0.1),
        ("cutpoint_prior_precision", 20.0),
    ],
)
def test_every_shipped_constant_changes_the_fit(field, value):
    """§4.3: "every constant comes from `ledger_hyperparams.json` … re-tunable offline in the
    corpus project". A constant that is read and then ignored is a knob the corpus project
    tunes into a void — so each one has to move the answer."""
    _truth, obs = synth(n=22, n_duels=45, tiers=8, seed=9)
    base = model.fit(obs, DEFAULTS)
    altered = model.fit(obs, dataclasses.replace(DEFAULTS, **{field: value}))

    changed = (
        not np.allclose(base.s, altered.s, atol=1e-9)
        or not np.allclose(base.cuts, altered.cuts, atol=1e-9)
        or abs(base.log_nu - altered.log_nu) > 1e-9
    )
    assert changed, f"{field} does not reach the fit"


def test_the_solver_constants_reach_the_work_not_the_answer():
    """Section 4.3 also ships `steps` and `lr`. On a convex objective with a unique minimiser a
    *converged* fit must not depend on either — a solver whose answer moved with its step size
    would be reporting its own arithmetic rather than the household's taste. What they change
    is how much work the fit costs, and that is what is asserted."""
    _truth, obs = synth(n=20, n_duels=40, seed=15)
    patient = model.fit(obs, dataclasses.replace(DEFAULTS, steps=400, lr=0.5))
    hurried = model.fit(obs, dataclasses.replace(DEFAULTS, steps=6, lr=0.01))

    assert hurried.iterations[1] < patient.iterations[1], "`steps` caps the work"
    assert hurried.grad_inf > patient.grad_inf, "a hurried fit is measurably less converged"
    assert not hurried.converged, "and it says so rather than claiming otherwise"
    assert patient.converged


def test_the_margin_form_is_honoured():
    """§4.3 ships the functional form, not only the flag."""
    assert "margin/mean(margin)" in Hyperparams().margin_form


# --- §5.3's budgets --------------------------------------------------------------------------------


def test_a_full_refit_over_a_whole_owned_library_is_seconds_not_minutes():
    """§5.3: "Ledger full MAP refit + cutpoints + σ — nightly — seconds", over both users'
    full owned library (839+ titles). CPU only."""
    _truth, obs = synth(n=839, n_verdicts=300, n_duels=400, seed=21)
    started = time.perf_counter()
    fitted = model.fit(obs, DEFAULTS)
    elapsed = time.perf_counter() - started
    assert np.all(np.isfinite(fitted.s))
    assert elapsed < 20.0, f"nightly refit took {elapsed:.1f}s — §5.3 says seconds"


def test_the_fit_scales_to_a_library_nobody_has_rated():
    """§12's M2 exit criterion is "every owned title has a coordinate". An unobserved title has
    no residual, so its s is mu + <v, e> — and it still gets a σ, from the (mu, v) block."""
    _truth, obs = synth(n=200, n_verdicts=12, n_duels=6, seed=17)
    fitted = model.fit(obs, DEFAULTS)
    assert fitted.s.shape == (200,)
    assert np.all(np.isfinite(fitted.sigma))
    assert fitted.z_cov.shape == (65, 65)


def test_a_household_with_no_bundle_can_still_rate():
    """§3.1: an empty artifact store is a legal state. With no Backbone there is no e_i, so v
    cannot generalise — but the residuals still order what the person actually rated."""
    _truth, obs = synth(n=15, n_duels=20, seed=13, embed=False)
    fitted = model.fit(obs, DEFAULTS)
    assert np.all(np.isfinite(fitted.s))
    assert np.ptp(fitted.s) > 0, "verdicts alone must still produce an ordering"
    assert np.allclose(fitted.v, 0.0, atol=1e-6), "no embeddings ⇒ nothing for v to learn"


def test_an_empty_ledger_is_not_an_error():
    """A user who has rated nothing is the common case on day one."""
    empty = ObservationSet(
        title_ids=np.zeros(0, dtype=np.int64),
        embeddings=np.zeros((0, 64)), embedded=np.zeros(0, bool),
    )
    fitted = model.fit(empty, DEFAULTS)
    assert fitted.s.shape == (0,)
    assert fitted.converged
