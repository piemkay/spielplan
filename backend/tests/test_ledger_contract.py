"""The solver's contract over a distribution of skewed boards, not a fixture (§5.2, §4.2): every number
finite, cutpoints ordered, the reported objective the returned parameters', and no start beats it."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import OUT_A, OUT_B, OUT_TIE, ObservationSet

BOARDS = 120



def board(rng: np.random.Generator) -> ObservationSet:
    """Skewed on purpose: a small-concentration Dirichlet
    makes unused and single-occupancy levels common."""
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
    """`s` and `sigma` accept NaN in Postgres, and NaN sorts ABOVE every real: the top of every shelf."""
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
    """§5.2: the cutpoints ARE the displayed boundaries; crossed, the probabilities sum past one."""
    crossed = [
        f"board {i} (K={obs.n_levels}): cuts={np.round(f.cuts, 3).tolist()}"
        for i, (obs, f) in enumerate(fits)
        if not model.feasible(f.gamma, f.cuts)
    ]
    assert not crossed, f"{len(crossed)} board(s) left the ordered cone: {crossed[:4]}"


def test_the_reported_objective_is_the_objective_of_the_reported_parameters(fits):
    """It cannot pass while `fit` returns sorted cutpoints beside an objective of the unsorted ones."""
    for i, (obs, f) in enumerate(fits):
        recomputed = model._objective(
            obs, DEFAULTS, f.mu, f.v, f.gamma, f.cuts, f.log_nu, f.r,
            with_duels=obs.duel_a.size > 0,
        )
        assert recomputed == pytest.approx(f.objective, rel=1e-9, abs=1e-9), (
            f"board {i}: reported {f.objective!r}, parameters give {recomputed!r}"
        )


def test_no_other_starting_point_finds_a_lower_objective(fits):
    """§5.2's objective is convex on the ordered cone, so no start can beat the minimiser. `fit` is
    deterministic, so the zero-budget fit first proves the start arrives (`mu` IS `mu0`)."""
    rng = np.random.default_rng(99)
    # A budget of nothing, which `hyperparams.load` would refuse: it only answers "did the start arrive".
    idle = dataclasses.replace(DEFAULTS, newton_max_iter=0, steps=0)
    # Optima, not budgets: stage B's default budget stops some duel-heavy boards short.
    full = dataclasses.replace(DEFAULTS, steps=1000)
    worse, unread = [], []
    for i, (obs, _) in enumerate(fits[:40]):
        lay = model._Layout(obs.n, obs.n_levels)
        cuts = np.sort(rng.normal(scale=1.5, size=lay.n_cuts))
        gamma = np.sort(rng.normal(scale=1.5, size=2))
        if not model.feasible(gamma, cuts):
            continue
        mu0 = float(rng.normal())
        v0 = rng.normal(size=64) / 20.0
        z0 = model._pack(mu0, v0, gamma, cuts, float(rng.normal()))
        r0 = rng.normal(size=obs.n) / 20.0

        idled = model.fit(obs, idle, z0=z0, r0=r0)
        if idled.mu != mu0 or not np.array_equal(idled.s, mu0 + obs.embeddings @ v0 + r0):
            unread.append(
                f"board {i}: started at mu={mu0!r}, a fit of no steps reports {idled.mu!r}"
            )

        best = model.fit(obs, full)
        other = model.fit(obs, full, z0=z0, r0=r0)
        if other.objective < best.objective - 1e-6 * max(1.0, abs(best.objective)):
            worse.append(f"board {i}: default {best.objective:.6f} vs {other.objective:.6f}")
    assert not unread, f"fit did not start where it was told to: {unread[:4]}"
    assert not worse, f"a different start found a better optimum: {worse[:4]}"


def test_a_board_dragged_only_to_the_extremes_orders_the_two_piles(fits):
    """The *latent* separates; the piles need not reach tiers
    0 and 6, because τ keeps the MAP near the middle."""
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


def test_a_labeller_who_never_says_fine_still_gets_ordered_thresholds(fits):
    """§6.1's class-balance widget exists because skewed labelling is common."""
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
    """A fit that stops early must stop at the last point that *decreased* the objective."""
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


def test_a_verdict_band_that_closes_is_walked_to_rather_than_overflowed():
    """A closing verdict band makes the log-gap step overflow; `model.MAX_LOG_GAP_STEP` caps it."""
    rng = np.random.default_rng(20260830)
    obs = [board(rng) for _ in range(26)][25]
    budget = dataclasses.replace(DEFAULTS, steps=2000)
    walked = model.fit(obs, budget)
    assert walked.converged, (walked.iterations, walked.grad_inf)
    assert walked.gamma[1] - walked.gamma[0] > 1.0, "the band reopened where its data puts it"

    default = model.fit(obs, DEFAULTS)
    assert default.iterations[1] == DEFAULTS.steps, (
        "the search stopped before its budget, which is the stall and not a slow walk"
    )
