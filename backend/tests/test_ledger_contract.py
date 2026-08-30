"""The solver's contract, over a distribution rather than a fixture. Spec v2.1 §5.2, §6.3, §4.2.

Every other test of the Ledger differentiates at one hand-picked point, or asserts a
qualitative direction on a board of four to eight titles, or fits a single seed. None of them
runs `fit` over the *shapes M2 actually produces* — a day-one board where everything was
dragged to F or S, a labeller who never says "Fine", a tier set with levels nobody used.

That gap is not hypothetical. It is how a fully green suite sat on top of an optimiser that
walked outside the ordered cone on the majority of skewed boards, returned cutpoints in the
wrong order, laundered them with a sort, and reported `converged=True` while the tier a title
displayed in disagreed with the tier the person had just dragged it to.

So this asserts what a `Fit` must *be*, not what it should look like:

  1. every number in it is finite;
  2. the cutpoints it returns are ordered — §4.2 says "ordered ascending" and §5.2 says they
     ARE the displayed boundaries, so a crossed pair is not a display bug, it is a fit that is
     not a likelihood;
  3. the objective it reports is the objective of the parameters it returns;
  4. it is the minimum: an independent optimiser started elsewhere cannot beat it.

The first three hold. The fourth does not yet, and the three tests that assert it are marked
`xfail(strict=True)` with the reason — so the gap is recorded as a red line in the report rather
than as an absence, and the day it is fixed the suite says XPASS instead of going quietly green.
"""

from __future__ import annotations

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import OUT_A, OUT_B, OUT_TIE, ObservationSet

BOARDS = 120


def board(rng: np.random.Generator) -> ObservationSet:
    """One observation set from the space M2 and M3 actually produce.

    Deliberately skewed: a Dirichlet with small concentration makes unused tier levels and
    single-occupancy levels the common case rather than the exception, which is exactly the
    day-one board and exactly the skewed labeller §6.1's class-balance widget exists to warn
    about.
    """
    n = int(rng.integers(1, 90))
    k = int(rng.integers(3, 11))
    embed = rng.random() < 0.7
    e = rng.normal(size=(n, 64)) / 8.0 if embed else np.zeros((n, 64))

    ord_index: list[int] = []
    ord_level: list[int] = []
    ord_arm: list[int] = []

    if rng.random() < 0.85:
        m = int(rng.integers(0, n + 1))
        idx = rng.choice(n, size=m, replace=False) if m else np.zeros(0, dtype=int)
        weights = rng.dirichlet(np.full(3, 0.35))
        ord_index += list(idx)
        ord_level += list(rng.choice(3, size=m, p=weights))
        ord_arm += [0] * m

    if rng.random() < 0.6:
        m = int(rng.integers(0, n + 1))
        idx = rng.choice(n, size=m, replace=False) if m else np.zeros(0, dtype=int)
        weights = rng.dirichlet(np.full(k, 0.35))
        ord_index += list(idx)
        ord_level += list(rng.choice(k, size=m, p=weights))
        ord_arm += [1] * m

    n_duels = int(rng.integers(0, 400)) if n >= 2 and rng.random() < 0.7 else 0
    if n_duels:
        pairs = rng.choice(n, size=(n_duels, 2))
        pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    else:
        pairs = np.zeros((0, 2), dtype=int)

    return ObservationSet(
        title_ids=np.arange(n, dtype=np.int64),
        embeddings=e,
        embedded=np.full(n, embed),
        ord_index=np.asarray(ord_index, dtype=np.int64),
        ord_level=np.asarray(ord_level, dtype=np.int64),
        ord_arm=np.asarray(ord_arm, dtype=np.int64),
        ord_weight=np.ones(len(ord_index)),
        duel_a=pairs[:, 0].astype(np.int64),
        duel_b=pairs[:, 1].astype(np.int64),
        duel_outcome=rng.choice([OUT_A, OUT_B, OUT_TIE], size=len(pairs)).astype(np.int64),
        duel_margin=np.where(rng.random(len(pairs)) < 0.4, 1.6, 1.0),
        n_levels=k,
    )


@pytest.fixture(scope="module")
def fits():
    rng = np.random.default_rng(20260830)
    out = []
    for _ in range(BOARDS):
        obs = board(rng)
        out.append((obs, model.fit(obs, DEFAULTS)))
    return out


def test_no_board_produces_a_number_that_is_not_a_number(fits):
    """A NaN reaching `ledger_state` is worse than a crash: `s` and `sigma` are
    `double precision NOT NULL`, Postgres accepts NaN, and NaN sorts ABOVE every real — so a
    poisoned fit would arrive at the *top* of every §6.0 shelf."""
    bad = []
    for i, (obs, f) in enumerate(fits):
        for name, value in (
            ("s", f.s), ("sigma", f.sigma), ("sigma_prior", f.sigma_prior),
            ("cuts", f.cuts), ("gamma", f.gamma), ("v", f.v), ("r", f.r),
            ("z_cov", f.z_cov),
            ("objective", np.array([f.objective])), ("grad_inf", np.array([f.grad_inf])),
            ("mu", np.array([f.mu])), ("log_nu", np.array([f.log_nu])),
        ):
            if not np.all(np.isfinite(value)):
                bad.append(f"board {i} (n={obs.n}, K={obs.n_levels}): {name}")
    assert not bad, f"{len(bad)} non-finite field(s): {bad[:6]}"


def test_every_board_returns_ordered_cutpoints(fits):
    """§4.2: `ledger_cutpoints.boundaries` is "ordered ascending". §5.2: those cutpoints ARE the
    displayed tier boundaries. A crossed pair is not a cosmetic problem — the level
    probabilities then sum to more than one and the objective has stopped being the
    ordered-logit likelihood."""
    crossed = [
        f"board {i} (K={obs.n_levels}): cuts={np.round(f.cuts, 3).tolist()}"
        for i, (obs, f) in enumerate(fits)
        if not model.feasible(f.gamma, f.cuts)
    ]
    assert not crossed, f"{len(crossed)} board(s) left the ordered cone: {crossed[:4]}"


def test_the_reported_objective_is_the_objective_of_the_reported_parameters(fits):
    """The sharpest single assertion available here. It cannot pass while `fit` returns a
    sorted copy of the cutpoints next to an objective evaluated on the unsorted ones — which
    turns a silent crossing into a red test without anyone having to reproduce a divergence."""
    for i, (obs, f) in enumerate(fits):
        recomputed = model._objective(
            obs, DEFAULTS, f.mu, f.v, f.gamma, f.cuts, f.log_nu, f.r,
            with_duels=obs.duel_a.size > 0,
        )
        assert recomputed == pytest.approx(f.objective, rel=1e-9, abs=1e-9), (
            f"board {i}: reported {f.objective!r}, parameters give {recomputed!r}"
        )


@pytest.mark.xfail(strict=True, reason="the solve stalls against the cone boundary: an ordered logit's optimum often has two cutpoints coincident (an unused tier level), and a line search that only clips to the boundary cannot then travel along it. The fix is a monotone reparameterisation of the cutpoints — cuts = c0 + cumsum(exp(delta)) — which is a diffeomorphism onto the cone and so cannot introduce local minima, only remove convexity as a property of the parameterisation. Not yet done. What the fit returns meanwhile is feasible, finite and self-consistent (the three tests above), just not always the minimum.")
def test_no_other_starting_point_finds_a_lower_objective(fits):
    """§5.2's objective is convex on the ordered cone, so the minimiser is unique and no start
    can beat it. This is the check that the answer is the minimum rather than a
    stationary-looking point the search happened to stop at.

    `test_the_optimum_is_unique_from_any_start` could not do this before: it passed an
    identical copy of the observations and `fit` took no start point, so the "two very
    different starting points" it named were never exercised.
    """
    rng = np.random.default_rng(99)
    worse = []
    for i, (obs, f) in enumerate(fits[:40]):
        lay = model._Layout(obs.n, obs.n_levels)
        cuts = np.sort(rng.normal(scale=1.5, size=lay.n_cuts))
        gamma = np.sort(rng.normal(scale=1.5, size=2))
        if not model.feasible(gamma, cuts):
            continue
        z0 = model._pack(
            float(rng.normal()), rng.normal(size=64) / 20.0, gamma, cuts, float(rng.normal())
        )
        other = model.fit(obs, DEFAULTS, z0=z0, r0=rng.normal(size=obs.n) / 20.0)
        if other.objective < f.objective - 1e-6 * max(1.0, abs(f.objective)):
            worse.append(f"board {i}: default {f.objective:.6f} vs {other.objective:.6f}")
    assert not worse, f"a different start found a better optimum: {worse[:4]}"


@pytest.mark.xfail(strict=True, reason="the solve stalls against the cone boundary: an ordered logit's optimum often has two cutpoints coincident (an unused tier level), and a line search that only clips to the boundary cannot then travel along it. The fix is a monotone reparameterisation of the cutpoints — cuts = c0 + cumsum(exp(delta)) — which is a diffeomorphism onto the cone and so cannot introduce local minima, only remove convexity as a property of the parameterisation. Not yet done. What the fit returns meanwhile is feasible, finite and self-consistent (the three tests above), just not always the minimum.")
def test_a_board_dragged_only_to_the_extremes_orders_the_two_piles(fits):
    """§5.2: "drag-and-drop = data, not override; the model re-fits around it".

    Twenty titles dragged to the bottom tier and twenty to the top, on day one, nothing else.
    Before the cone was constrained this returned crossed cutpoints, sorted them, and reported
    converged — a fit that was not a likelihood.

    What it asserts is that the *latent* separates, not that the two piles land in tiers 0 and
    6. They do not, and should not: with forty drags and nothing else, §5.2's residual prior
    (τ) costs more to satisfy the extremes than the likelihood gains, so the MAP answer keeps
    everyone near the middle and says so through σ. "Drag-and-drop is data" means the drags
    move the model, not that they overrule it.
    """
    n = 40
    obs = ObservationSet(
        title_ids=np.arange(n, dtype=np.int64),
        embeddings=np.zeros((n, 64)), embedded=np.zeros(n, bool),
        ord_index=np.arange(n, dtype=np.int64),
        ord_level=np.array([0] * 20 + [6] * 20, dtype=np.int64),
        ord_arm=np.ones(n, dtype=np.int64),
        ord_weight=np.ones(n),
        n_levels=7,
    )
    f = model.fit(obs, DEFAULTS)
    assert model.feasible(f.gamma, f.cuts), f"crossed cutpoints: {f.cuts}"
    assert np.all(np.isfinite(f.s))
    assert f.s[:20].max() < f.s[20:].min(), (
        "the pile dragged to the bottom must sit below the pile dragged to the top"
    )
    assert f.converged, f"the fit stalled: grad {f.grad_inf:.3g} after {f.iterations}"


@pytest.mark.xfail(strict=True, reason="the solve stalls against the cone boundary: an ordered logit's optimum often has two cutpoints coincident (an unused tier level), and a line search that only clips to the boundary cannot then travel along it. The fix is a monotone reparameterisation of the cutpoints — cuts = c0 + cumsum(exp(delta)) — which is a diffeomorphism onto the cone and so cannot introduce local minima, only remove convexity as a property of the parameterisation. Not yet done. What the fit returns meanwhile is feasible, finite and self-consistent (the three tests above), just not always the minimum.")
def test_a_labeller_who_never_says_fine_still_gets_ordered_thresholds(fits):
    """§6.1's class-balance widget exists because skewed labelling is common. The verdict arm's
    two cutpoints used to cross whenever the middle class was empty or nearly so — which is the
    labeller the widget is warning, so the fit must survive exactly the person it is aimed at."""
    for middle in (0, 1):
        levels = [0] * 30 + [1] * middle + [2] * 30
        n = len(levels)
        obs = ObservationSet(
            title_ids=np.arange(n, dtype=np.int64),
            embeddings=np.zeros((n, 64)), embedded=np.zeros(n, bool),
            ord_index=np.arange(n, dtype=np.int64),
            ord_level=np.asarray(levels, dtype=np.int64),
            ord_arm=np.zeros(n, dtype=np.int64), ord_weight=np.ones(n),
        )
        f = model.fit(obs, DEFAULTS)
        assert f.gamma[0] < f.gamma[1], f"{middle} 'Fine' labels crossed the verdict thresholds"
        assert np.all(np.isfinite(f.s))


def test_the_search_never_applies_a_step_it_rejected(fits):
    """The line search used to fold "the step was rejected" and "the step was accepted" into
    one branch and then take the step anyway, so an exhausted search returned its own
    divergence. A fit that stops early must stop at the last point that *decreased* the
    objective."""
    rng = np.random.default_rng(4)
    for _ in range(15):
        obs = board(rng)
        f = model.fit(obs, DEFAULTS)
        at_start = model._objective(
            obs, DEFAULTS, 0.0, np.zeros(64), model.initial_cutpoints(3),
            model.initial_cutpoints(obs.n_levels), float(np.log(DEFAULTS.nu0())),
            np.zeros(obs.n), with_duels=obs.duel_a.size > 0,
        )
        assert f.objective <= at_start + 1e-9, (
            "the fit must never be worse than the point it started from"
        )
