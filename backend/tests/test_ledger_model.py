"""The Personal Ledger's maths (§5.2, §5.3). Numpy only. A wrong cross-term still converges, to
a slightly wrong answer, so finite differences come first."""

from __future__ import annotations

import dataclasses
import time

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import OUT_A, OUT_B, OUT_TIE, ObservationSet


def synth(n=30, n_verdicts=None, n_duels=40, *, seed=3, tiers=0, embed=True):
    """Verdicts through fixed thresholds, agreeing duels,
    and a tie band wide enough for the Davidson term."""
    rng = np.random.default_rng(seed)
    e = rng.normal(size=(n, 64)) / 8.0 if embed else np.zeros((n, 64))
    truth = 0.3 + (e @ (rng.normal(size=64) / 8.0)) + rng.normal(scale=0.25, size=n)

    n_verdicts = n if n_verdicts is None else n_verdicts
    vi = rng.choice(n, size=n_verdicts, replace=False)
    level = np.searchsorted(np.array([-0.4, 0.4]), truth[vi], side="right")
    ord_index, ord_level, ord_arm = list(vi), list(level), [0] * n_verdicts

    if tiers:
        ti = rng.choice(vi, size=tiers, replace=False)
        tier_cuts = np.quantile(truth, np.linspace(0, 1, 7)[1:-1])
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
        # Unequal margins: the only condition under which normalising them can change anything.
        duel_margin=np.where(rng.random(len(pairs)) < 0.4, 1.6, 1.0),
        n_levels=6,
    )


def spearman(a, b):
    return float(np.corrcoef(np.argsort(np.argsort(a)), np.argsort(np.argsort(b)))[0, 1])


def reach_sigma(reach: float) -> float:
    """The fixture's claim is the REACH (z·σ), so it is written in z, which §4.3 says is tunable."""
    return reach / DEFAULTS.straddle_z


def test_the_analytic_gradient_matches_finite_differences():
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
    """Arrowhead is what makes the solve O(n*p^2); a wrong block makes Newton crawl rather than fail."""
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
    """A duel couples its pair, and for mu the four contributions cancel: diagonal-only overstates sigma."""
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
    gamma, cuts, log_nu = np.array([-0.4, 0.4]), model.initial_cutpoints(6), -0.5
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


def test_a_threshold_can_never_put_a_disliked_title_above_a_liked_one():
    """§5.2: a monotone link cannot invert an ordering across classes. Within a class the priors may swap
    two titles by a hair, so the claim is max-of-lower against MIN-of-higher."""
    truth, base = synth(n=30, n_duels=0, seed=8)
    # Terciles of the latent keep labels monotone and give the disliked class members synth never draws.
    held = truth[base.ord_index]
    level = np.searchsorted(np.quantile(held, [1 / 3, 2 / 3]), held, side="right")
    obs = dataclasses.replace(base, ord_level=level.astype(np.int64))
    counts = np.bincount(obs.ord_level, minlength=3)
    assert counts.min() >= 5, (
        f"the fixture must label titles into all three verdict classes, not two: {counts.tolist()}"
    )

    loose = model.fit(obs, dataclasses.replace(DEFAULTS, cutpoint_prior_precision=0.01))
    tight = model.fit(obs, dataclasses.replace(DEFAULTS, cutpoint_prior_precision=50.0))

    assert not np.allclose(loose.gamma, tight.gamma, atol=1e-3), (
        "the fixture must actually move the thresholds"
    )
    for fitted in (loose, tight):
        for low, high in ((0, 1), (0, 2), (1, 2)):
            a = obs.ord_index[obs.ord_level == low]
            b = obs.ord_index[obs.ord_level == high]
            assert fitted.s[a].max() < fitted.s[b].min(), (
                f"class {low} reached above class {high}: max(s | {low}) = "
                f"{fitted.s[a].max():.3f} >= min(s | {high}) = {fitted.s[b].min():.3f}"
            )
    # ...and within a class, the ordering barely moves: the priors, not the link.
    assert spearman(loose.s, tight.s) > 0.99


def test_the_ordinal_link_is_monotone():
    cuts = np.array([-0.5, 0.7])
    s = np.linspace(-4, 4, 200)
    upper = 1.0 / (1.0 + np.exp(-(s - cuts[1])))
    assert np.all(np.diff(upper) > 0)
    assert list(model.tier_of(s, cuts)) == sorted(model.tier_of(s, cuts))


def test_duels_add_resolution_within_the_liked_class():
    """Verdicts alone cannot order two titles a person called the same thing; duels can."""
    truth, obs = synth(n=40, n_duels=0, seed=4)
    verdicts_only = model.fit(obs, DEFAULTS)

    with_duels = model.fit(synth(n=40, n_duels=120, seed=4)[1], DEFAULTS)

    liked = obs.ord_index[obs.ord_level == 2]
    assert liked.size >= 5, "the fixture must actually produce a liked class"
    before = spearman(verdicts_only.s[liked], truth[liked])
    after = spearman(with_duels.s[liked], truth[liked])
    assert after > before, f"duels must resolve within a class: {before:.3f} -> {after:.3f}"


def test_a_tie_is_data_and_moves_the_fit():
    """§4.2: 22% of random pairs are genuine ties; a tie is data, not a dropped row."""
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
    """δ₀ = 0.22 is an initialisation, "thereafter fitted"."""
    _truth, obs = synth(n=20, n_duels=60, seed=6)
    decisive = dataclasses.replace(
        obs, duel_outcome=np.where(obs.duel_outcome == OUT_TIE, OUT_A, obs.duel_outcome)
    )
    fitted = model.fit(decisive, DEFAULTS)
    assert np.exp(fitted.log_nu) < DEFAULTS.nu0(), "no ties observed ⇒ ν must fall"


def test_a_decisive_duel_teaches_more_than_a_hesitant_one():
    """Weights are margin/mean(margin), so only duels relative to each other can show the toggle."""
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


def test_a_duel_weight_scales_its_row_on_top_of_the_margin_weighting():
    """Decision 564's recency: None is every row at 1, two halves are one whole, and lighter
    evidence leaves a wider posterior."""
    _truth, obs = synth(n=16, n_duels=30, seed=21)
    plain = model.fit(obs, DEFAULTS)
    ones = model.fit(dataclasses.replace(obs, duel_weight=np.ones(obs.duel_a.size)), DEFAULTS)
    np.testing.assert_allclose(ones.s, plain.s, atol=1e-12)
    np.testing.assert_allclose(ones.sigma, plain.sigma, atol=1e-12)

    doubled = dataclasses.replace(
        obs,
        duel_a=np.concatenate([obs.duel_a, obs.duel_a]),
        duel_b=np.concatenate([obs.duel_b, obs.duel_b]),
        duel_outcome=np.concatenate([obs.duel_outcome, obs.duel_outcome]),
        duel_margin=np.concatenate([obs.duel_margin, obs.duel_margin]),
        duel_weight=np.full(2 * obs.duel_a.size, 0.5),
    )
    halves = model.fit(doubled, DEFAULTS)
    np.testing.assert_allclose(halves.s, plain.s, atol=1e-8)

    faded = model.fit(dataclasses.replace(obs, duel_weight=np.full(obs.duel_a.size, 0.25)), DEFAULTS)
    duelled = np.unique(np.concatenate([obs.duel_a, obs.duel_b]))
    assert np.all(faded.sigma[duelled] > plain.sigma[duelled])


def test_a_tier_edit_is_data_on_the_same_latent():
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


def test_the_preconditioner_survives_one_heavily_duelled_title():
    """§5.2's scar: fixed-step GD diverges when one title has two orders of magnitude more duels."""
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


def test_the_displayed_weight_is_the_users_own_empirical_cdf():
    s = np.array([-2.0, -0.5, 0.0, 0.7, 3.0])
    cdf = model.empirical_cdf(s, s)
    assert cdf[0] == pytest.approx(0.1)
    assert cdf[-1] == pytest.approx(0.9)
    assert list(cdf) == sorted(cdf)


def test_the_weight_is_stable_under_monotone_rescaling():
    """The owner's "always-preferred -> 1.0" is independent of the scale s is fitted on."""
    s = np.array([-2.0, -0.5, 0.0, 0.7, 3.0])
    assert np.allclose(model.empirical_cdf(s, s), model.empirical_cdf(3 * s + 11, 3 * s + 11))
    warped = np.tanh(s)
    assert np.allclose(model.empirical_cdf(s, s), model.empirical_cdf(warped, warped))


def test_a_single_title_has_no_meaningful_weight():
    """Inventing 0.5 would be a number with no evidence behind it."""
    assert np.isnan(model.empirical_cdf(np.array([1.0]), np.array([1.0]))).all()


def test_ties_share_a_weight():
    cdf = model.empirical_cdf(np.array([0.0, 1.0, 1.0, 2.0]), np.array([1.0, 1.0]))
    assert cdf[0] == cdf[1]


def test_the_fitted_cutpoints_are_the_displayed_boundaries():
    cuts = np.array([-1.0, -0.4, 0.0, 0.4, 1.0, 1.6])
    s = np.array([-2.0, -0.7, 0.2, 1.2, 2.0])
    assert list(model.tier_of(s, cuts)) == [0, 1, 3, 5, 6]


def test_the_initial_cutpoints_carry_the_measured_tier_shape():
    """A level nobody has used sits where the crowd puts it rather than at infinity."""
    cuts = model.initial_cutpoints(6)
    assert cuts.size == 5
    assert list(cuts) == sorted(cuts)
    shares = np.diff(np.concatenate([[0.0], 1 / (1 + np.exp(-cuts)), [1.0]]))
    assert np.allclose(shares, model.MEASURED_TIER_SHARES, atol=1e-9)

    # Equal mass for any other tier set: there is no measurement for a set nobody has used.
    five = model.initial_cutpoints(5)
    five_shares = np.diff(np.concatenate([[0.0], 1 / (1 + np.exp(-five)), [1.0]]))
    assert np.allclose(five_shares, 0.2)


def test_a_posterior_that_reaches_the_next_tier_is_flagged():
    cuts = np.array([-1.0, 0.0, 1.0])
    s = np.array([0.95, 0.20])
    sigma = np.array([reach_sigma(0.30), reach_sigma(0.02)])
    flags = model.straddle(s, sigma, cuts, DEFAULTS)
    assert flags[0] == 3, "0.95 ± 0.30 reaches the tier above"
    assert flags[1] == -1, "0.20 ± 0.02 does not"


def test_a_straddle_never_names_a_tier_that_is_not_adjacent():
    """Decision 205: the badge names only an adjacent tier, however many cuts the interval crosses."""
    cuts = model.initial_cutpoints(6)
    rng = np.random.default_rng(19)
    s = rng.normal(scale=2.0, size=600)
    sigma = rng.uniform(0.01, 3.0, size=600)
    tier = model.tier_of(s, cuts)
    reached = model.straddle(s, sigma, cuts, DEFAULTS)
    named = reached >= 0
    assert named.sum() > 100, "the fixture has to produce straddlers or this proves nothing"
    assert np.all(np.abs(reached[named] - tier[named]) == 1), (
        "a badge naming a tier the posterior neither occupies nor borders is a claim about "
        "nothing"
    )


def test_restricting_the_named_tier_does_not_narrow_the_straddling_set():
    """The badge and queue eligibility are one predicate: some cutpoint in (s - zσ, s + zσ]."""
    cuts = model.initial_cutpoints(6)
    rng = np.random.default_rng(23)
    s = rng.normal(scale=2.0, size=400)
    sigma = rng.uniform(0.01, 3.0, size=400)
    reach = DEFAULTS.straddle_z * sigma
    crosses = np.array(
        [bool(np.any((cuts > s[i] - reach[i]) & (cuts <= s[i] + reach[i]))) for i in range(s.size)]
    )
    assert np.array_equal(model.straddle(s, sigma, cuts, DEFAULTS) >= 0, crosses)


def test_a_posterior_reaching_both_neighbours_names_the_nearer_cut():
    """The nearer cut wins; a tie keeps the downward choice, so the answer is not iteration order."""
    cuts = model.initial_cutpoints(6)
    wide = np.array([reach_sigma(1.0)])   # an interval reaching one unit either way
    assert model.tier_of(np.array([0.9]), cuts)[0] == 3, "s = 0.9 sits in B on the measured set"
    assert model.straddle(np.array([0.9]), wide, cuts, DEFAULTS)[0] == 4

    nearer_below = float(cuts[2]) + 0.1
    assert model.straddle(np.array([nearer_below]), wide, cuts, DEFAULTS)[0] == 2

    midway = (float(cuts[2]) + float(cuts[3])) / 2.0
    assert model.straddle(np.array([midway]), wide, cuts, DEFAULTS)[0] == 2


def test_sigma_does_not_move_inside_the_grace_period():
    sigma = np.array([0.3, 0.3])
    prior = np.array([1.0, 1.0])
    assert np.allclose(model.inflate_sigma(sigma, prior, np.array([0.0, 11.9]), DEFAULTS), sigma)


def test_sigma_inflates_with_the_square_root_of_neglect_and_stops_at_the_prior():
    sigma = np.array([0.3, 0.3, 0.3])
    prior = np.array([1.0, 1.0, 1.0])
    grown = model.inflate_sigma(sigma, prior, np.array([13.0, 24.0, 100_000.0]), DEFAULTS)
    assert grown[0] > sigma[0]
    assert grown[1] > grown[0], "more neglect, more uncertainty"
    assert grown[2] == pytest.approx(prior[2]), "capped at the prior σ, never beyond it"


def test_an_already_uncertain_title_is_not_shrunk_by_the_cap():
    sigma = np.array([2.0])
    out = model.inflate_sigma(sigma, np.array([1.0]), np.array([48.0]), DEFAULTS)
    assert out[0] == pytest.approx(2.0)


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
    """On a convex objective a converged fit must not depend on `steps` or `lr`; only the work does."""
    _truth, obs = synth(n=20, n_duels=40, seed=15)
    patient = model.fit(obs, dataclasses.replace(DEFAULTS, steps=400, lr=0.5))
    hurried = model.fit(obs, dataclasses.replace(DEFAULTS, steps=6, lr=0.01))

    assert hurried.iterations[1] < patient.iterations[1], "`steps` caps the work"
    assert hurried.grad_inf > patient.grad_inf, "a hurried fit is measurably less converged"
    assert not hurried.converged, "and it says so rather than claiming otherwise"
    assert patient.converged


def test_a_full_refit_over_a_whole_owned_library_is_seconds_not_minutes():
    """§5.3: "Ledger full MAP refit ... nightly ... seconds" over 839+ owned titles, CPU only."""
    _truth, obs = synth(n=839, n_verdicts=300, n_duels=400, seed=21)
    started = time.perf_counter()
    fitted = model.fit(obs, DEFAULTS)
    elapsed = time.perf_counter() - started
    assert np.all(np.isfinite(fitted.s))
    assert elapsed < 20.0, f"nightly refit took {elapsed:.1f}s — §5.3 says seconds"


def test_the_fit_scales_to_a_library_nobody_has_rated():
    """An unobserved title's s is mu + <v, e>, and it still gets a σ from the (mu, v) block."""
    _truth, obs = synth(n=200, n_verdicts=12, n_duels=6, seed=17)
    fitted = model.fit(obs, DEFAULTS)
    assert fitted.s.shape == (200,)
    assert np.all(np.isfinite(fitted.sigma))
    assert fitted.z_cov.shape == (65, 65)


def test_a_household_with_no_bundle_can_still_rate():
    """No Backbone means no e_i, but the residuals still order what the person rated."""
    _truth, obs = synth(n=15, n_duels=20, seed=13, embed=False)
    fitted = model.fit(obs, DEFAULTS)
    assert np.all(np.isfinite(fitted.s))
    assert np.ptp(fitted.s) > 0, "verdicts alone must still produce an ordering"
    assert np.allclose(fitted.v, 0.0, atol=1e-6), "no embeddings ⇒ nothing for v to learn"


def test_an_empty_ledger_is_not_an_error():
    empty = ObservationSet(
        title_ids=np.zeros(0, dtype=np.int64),
        embeddings=np.zeros((0, 64)), embedded=np.zeros(0, bool),
    )
    fitted = model.fit(empty, DEFAULTS)
    assert fitted.s.shape == (0,)
    assert fitted.converged


def test_the_tier_prior_starts_on_the_verdict_prior_for_every_tier_set():
    """Decision 508: where the shape has cuts at exactly 5%
    and 20%, the prior mean is `initial_cutpoints(K)`."""
    prior = model.verdict_cutpoints()
    for k in (6, 20):
        assert np.allclose(model.cut_prior_mean(prior, k), model.initial_cutpoints(k)), f"K = {k}"
    for k in (3, 4, 5, 7, 8, 9, 10, 11, 12):
        mean, shape = model.cut_prior_mean(prior, k), model.initial_cutpoints(k)
        lower, upper = model.anchored_cuts(k)
        assert mean[lower] == pytest.approx(prior[0]) and mean[upper] == pytest.approx(prior[1])
        assert np.allclose(np.diff(mean[: lower + 1]), np.diff(shape[: lower + 1])), f"K = {k}"
        assert np.allclose(np.diff(mean[upper:]), np.diff(shape[upper:])), f"K = {k}"
        assert np.all(np.diff(mean) > 0), f"K = {k}"
    # The verdict prior is the measured shape's E/D and D/C masses, 5% and 20%.
    assert np.allclose(prior, np.log([0.05 / 0.95, 0.20 / 0.80]))


def test_with_no_tier_edit_the_e_d_and_d_c_boundaries_are_the_verdict_cutpoints():
    _truth, obs = synth(n=40, n_duels=30, seed=21)
    fitted = model.fit(obs, DEFAULTS)
    assert fitted.cuts[0] == pytest.approx(fitted.gamma[0], abs=1e-6)
    assert fitted.cuts[1] == pytest.approx(fitted.gamma[1], abs=1e-6)
    assert np.allclose(fitted.cuts, model.cut_prior_mean(fitted.gamma, 6), atol=1e-6)


def test_the_coupled_cutpoint_prior_has_the_curvature_it_claims():
    """A wrong cross-term still converges, so the coupled block is checked against a central difference."""
    _truth, obs = synth(n=12, n_duels=10, tiers=5, seed=5)
    hp = DEFAULTS
    rng = np.random.default_rng(1)
    mu, v = 0.1, rng.normal(size=64) / 10.0
    gamma = np.array([-1.2, 0.3])
    cuts = model.cut_prior_mean(gamma, obs.n_levels) + rng.normal(scale=0.05, size=5)
    log_nu, r = -0.2, rng.normal(size=obs.n) / 10.0
    _g, _gr, h_zz, *_ = model._grad_hess(obs, hp, mu, v, gamma, cuts, log_nu, r, with_duels=False)
    h = 1e-6
    for j in range(7):
        step = np.zeros(7)
        step[j] = h
        plus = model._grad_hess(obs, hp, mu, v, gamma + step[:2], cuts + step[2:], log_nu, r,
                                with_duels=False)[0]
        minus = model._grad_hess(obs, hp, mu, v, gamma - step[:2], cuts - step[2:], log_nu, r,
                                 with_duels=False)[0]
        column = (plus[65:72] - minus[65:72]) / (2 * h)
        assert np.allclose(h_zz[65:72, 65 + j], column, atol=1e-5), f"column {j}"


def test_on_every_tier_count_a_tier_stands_for_the_class_the_verdict_cutpoints_give_its_s():
    gamma = np.array([-1.2, 0.5])
    grid = np.linspace(-4.0, 4.0, 1601)
    said = np.searchsorted(gamma, grid, side="right").tolist()
    for k in range(3, 13):
        cuts = model.cut_prior_mean(gamma, k)
        lower, upper = model.anchored_cuts(k)
        assert (cuts[lower], cuts[upper]) == (pytest.approx(gamma[0]), pytest.approx(gamma[1]))
        assert model.verdict_tiers(k).tolist() == [
            [0, lower], [lower + 1, upper], [upper + 1, k - 1]
        ], f"K = {k}"
        stands_for = [model.verdict_class_of_tier(int(t), k) for t in model.tier_of(grid, cuts)]
        assert stands_for == said, f"K = {k}"
    # Two tiers cannot hold three classes: the one cut is fine/liked, and the lower tier stands for fine.
    assert model.cut_prior_mean(gamma, 2).tolist() == pytest.approx([gamma[1]])
    assert [model.verdict_class_of_tier(t, 2) for t in (0, 1)] == [1, 2]


def test_each_verdict_names_the_tiers_it_renders_in():
    assert model.verdict_tiers(6).tolist() == [[0, 0], [1, 1], [2, 5]]
    assert [model.verdict_class_of_tier(t, 6) for t in range(6)] == [0, 1, 2, 2, 2, 2]
    for k in range(2, 13):
        bands = model.verdict_tiers(k)
        assert bands[0, 0] == 0 and bands[2, 1] == k - 1, f"K = {k}"
        assert np.all(bands[:, 0] <= bands[:, 1]), f"K = {k}"
        assert bands[0, 1] <= bands[1, 0] and bands[1, 1] <= bands[2, 0], f"K = {k}"


def test_a_rated_title_is_held_inside_its_verdicts_tiers_and_reaches_toward_its_s():
    tier = np.array([3, 1, 0, 4, 1])
    straddle = np.array([-1, 2, -1, -1, -1])
    verdict = np.array([0, 2, 0, 1, -1])
    held, reach = model.hold_to_verdict(tier, straddle, verdict, 6)
    assert held.tolist() == [0, 2, 0, 1, 1]
    assert reach.tolist() == [1, 1, -1, 2, -1]


def test_the_live_verdict_is_the_last_one_and_a_drop_holds_nothing():
    """A rewatch re-rating supersedes (§5.2 arm 4); a `tier_edit` decides placement on its own."""
    obs = ObservationSet(
        title_ids=np.arange(4, dtype=np.int64),
        embeddings=np.zeros((4, 64)),
        embedded=np.zeros(4, dtype=bool),
        ord_index=np.array([0, 1, 0, 2, 2], dtype=np.int64),
        ord_level=np.array([2, 1, 0, 2, 6], dtype=np.int64),
        ord_arm=np.array([0, 0, 0, 0, 1], dtype=np.int64),
        ord_weight=np.ones(5),
    )
    assert model.live_verdicts(obs).tolist() == [0, 1, -1, -1]


def _household():
    n = 24
    axis = np.zeros(64)
    axis[0] = 1.0
    x = np.linspace(-1.0, 1.0, n)
    e = np.outer(x, axis)
    level = np.searchsorted(np.array([-0.35, 0.3]), x, side="right")
    return x, e, level


def test_the_second_households_three_complaints_do_not_happen_on_a_board_like_theirs():
    """Titles and verdicts shaped like the second household's
    board, which failed all three before 508/509."""
    x, e, level = _household()
    n = x.size
    # La La Land: the second-best title on the taste axis, disliked.
    e = np.vstack([e, np.eye(64)[0] * 0.9])
    level = np.concatenate([level, [0]])
    liked = np.flatnonzero(level == 2)
    fine = np.flatnonzero(level == 1)
    disliked = np.flatnonzero(level[:n] == 0)
    winner, loser = int(disliked[0]), n                      # picked over La La Land
    tie_a, tie_b = int(fine[0]), int(fine[-1])
    obs = ObservationSet(
        title_ids=np.arange(n + 1, dtype=np.int64),
        embeddings=e,
        embedded=np.ones(n + 1, dtype=bool),
        ord_index=np.arange(n + 1, dtype=np.int64),
        ord_level=level.astype(np.int64),
        ord_arm=np.zeros(n + 1, dtype=np.int64),
        ord_weight=np.ones(n + 1),
        duel_a=np.array([winner, tie_a], dtype=np.int64),
        duel_b=np.array([loser, tie_b], dtype=np.int64),
        duel_outcome=np.array([OUT_A, OUT_TIE], dtype=np.int64),
        duel_margin=np.array([1.0, 1.0]),
        n_levels=6,
    )
    fitted = model.fit(obs, DEFAULTS)
    tiers, _reach = model.hold_to_verdict(
        model.tier_of(fitted.s, fitted.cuts), np.full(n + 1, -1), model.live_verdicts(obs), 6
    )
    la_la_land = n
    assert tiers[la_la_land] == 0, "a disliked title rendered above the disliked tier"
    assert fitted.s[la_la_land] < fitted.s[liked].min(), "a disliked title above a liked one"
    assert fitted.s[winner] > fitted.s[loser], "the pick did not move the winner up"
    assert tiers[tie_a] == tiers[tie_b] == 1, "about the same, and two tiers apart"
    assert set(tiers[liked].tolist()) <= {2, 3, 4, 5} and set(tiers[fine].tolist()) == {1}


def test_a_ladder_with_no_verdict_arm_keeps_ordered_cuts_on_the_shape_prior():
    """After a cut-over the fit reads no verdict (decision 537): gamma is held by its own prior and
    still anchors the tier cuts, so placements alone fit ordered cuts near the measured shape and the
    class bands stay the ones `verdict_tiers` names."""
    rng = np.random.default_rng(1)
    n = 40
    e = rng.normal(size=(n, 64)) / 8.0
    truth = e @ (rng.normal(size=64) / 8.0) + rng.normal(scale=0.5, size=n)
    shape = np.quantile(truth, np.cumsum(model.MEASURED_TIER_SHARES)[:-1])
    level = np.searchsorted(shape, truth, side="right")
    obs = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64), embeddings=e, embedded=np.ones(n, dtype=bool),
        ord_index=np.arange(n, dtype=np.int64), ord_level=level.astype(np.int64),
        ord_arm=np.ones(n, dtype=np.int64), ord_weight=np.ones(n), n_levels=6,
    )
    fitted = model.fit(obs, DEFAULTS)

    assert fitted.converged
    assert np.all(np.diff(fitted.cuts) > 0)
    assert np.abs(fitted.cuts - model.initial_cutpoints(6)).max() < 1.0
    assert np.abs(fitted.gamma - model.verdict_cutpoints()).max() < 0.5
    assert np.all(np.diff([fitted.s[level == t].mean() for t in range(6)]) > 0)
    fitted_tier = model.tier_of(fitted.s, fitted.cuts)
    same_class = [
        model.verdict_class_of_tier(int(a), 6) == model.verdict_class_of_tier(int(b), 6)
        for a, b in zip(fitted_tier, level, strict=True)
    ]
    assert np.mean(same_class) >= 0.8


def test_a_verdict_class_stands_for_its_tier_nearest_the_middle():
    """§5.1's step for a verdict: E, D or C at K = 6, the tier decision 510 guesses a class at."""
    assert [model.class_step(c, 6) for c in (0, 1, 2)] == [0, 1, 2]
    assert [model.class_step(c, 2) for c in (0, 1, 2)] == [0, 0, 1]
    for k in range(2, 13):
        bands = model.verdict_tiers(k)
        for c in (0, 1, 2):
            assert bands[c, 0] <= model.class_step(c, k) <= bands[c, 1], f"K = {k}"
        for c in (0, 2):
            band = np.arange(bands[c, 0], bands[c, 1] + 1)
            assert set(model.guess_tier(band, k).tolist()) == {model.class_step(c, k)}, f"K = {k}"


def test_an_unrated_title_is_guessed_a_class_and_not_a_grade():
    """Decision 510: an unrated title wears its guessed class's middle tier, never a grade."""
    assert model.guess_tier(np.arange(6), 6).tolist() == [0, 1, 2, 2, 2, 2]
    for k in range(2, 13):
        for tier in range(k):
            guessed = int(model.guess_tier(np.array([tier]), k)[0])
            assert model.verdict_class_of_tier(guessed, k) == model.verdict_class_of_tier(tier, k)
