"""Tonight's mood space: each title centred on the pool and scaled by its spread (an additive
centring cancels in a difference), then projected on the pool's own principal directions."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from spielplan.tonight import round as rnd
from spielplan.tonight import tilt as T

# Two terms is enough for the frame: one the pool varies on and one it does not.
POOL = {
    1: {"cosy": 1.0, "dread": 0.0},
    2: {"cosy": 0.0, "dread": 1.0},
    3: {"cosy": 0.5, "dread": 0.5},
}


def clustered(n=240, clusters=6, seed=0):
    """Titles drawn from a few groups of terms that travel together, as real DNA does."""
    rng = random.Random(seed)
    pool = {}
    for t in range(n):
        c = t % clusters
        pool[t] = {f"c{c}_{j}": 0.6 + 0.4 * rng.random() for j in range(5)}
    return pool


def test_the_pool_frame_is_the_pools_own_mean_and_spread():
    frame = T.frame(POOL)
    assert frame.mean["cosy"] == pytest.approx(0.5)
    assert frame.mean["dread"] == pytest.approx(0.5)
    assert frame.spread["cosy"] > 0.0


def test_a_candidate_is_expressed_as_its_deviation_from_the_pool():
    frame = T.frame(POOL)
    assert T.centred(POOL[3], frame) == pytest.approx({"cosy": 0.0, "dread": 0.0}, abs=1e-9)
    assert T.centred(POOL[1], frame)["cosy"] > 0.0
    assert T.centred(POOL[2], frame)["cosy"] < 0.0


def test_the_mood_has_five_directions_each_with_unit_spread_over_the_pool():
    pool = clustered()
    space = T.space(pool)
    z = np.asarray([space.project(v) for v in pool.values()])

    assert z.shape == (len(pool), rnd.MOOD_DIRECTIONS)
    assert z.std(axis=0) == pytest.approx(np.ones(rnd.MOOD_DIRECTIONS), rel=1e-6)


def test_a_rare_term_is_damped():
    """A term one title carries is a spike five times a common term once centred and scaled, yet it
    moves that title on the directions by less than half what its common terms do."""
    pool = clustered()
    plain = dict(pool[0])
    pool[0] = {**plain, "rare": 1.0}
    space = T.space(pool)
    centred = T.centred(pool[0], space.frame)
    assert centred["rare"] > 5 * centred["c0_0"]

    with_rare, without = np.asarray(space.project(pool[0])), np.asarray(space.project(plain))
    assert np.linalg.norm(with_rare - without) < 0.5 * np.linalg.norm(without)


def test_the_same_film_sits_differently_in_a_different_pool():
    """A plain `chosen - rejected` reads the same in every pool: §0 row 4's 0.000 version."""
    film = {"c0_0": 0.9, "c1_0": 0.7}
    narrow, wide = clustered(clusters=6), clustered(clusters=8, seed=1)
    assert T.space(narrow).project(film) != pytest.approx(T.space(wide).project(film))


def test_a_term_the_pool_does_not_vary_on_contributes_nothing():
    """Dividing by a zero spread would be infinity, not insight."""
    flat = {1: {"period": 1.0, "cosy": 1.0}, 2: {"period": 1.0, "cosy": 0.0}, 3: {"period": 1.0}}
    space = T.space(flat)
    assert "period" not in space.terms
    assert space.project({"period": 1.0}) == pytest.approx((0.0,) * rnd.MOOD_DIRECTIONS)


def test_a_pool_with_no_dna_has_no_directions_and_moves_nothing():
    space = T.space({1: {}, 2: {}})
    assert space.terms == ()
    assert space.project({"cosy": 1.0}) == (0.0,) * rnd.MOOD_DIRECTIONS
    assert T.back(space.terms, space.axes, np.ones(rnd.MOOD_DIRECTIONS)) == {}


def test_a_mood_written_back_as_terms_adjusts_a_title_as_the_mood_does():
    """`session_participant.tilt` holds the terms; their dot with a title's centred vector is the
    mood against that title's place on the directions."""
    pool = clustered()
    space = T.space(pool)
    mood = np.asarray([0.4, -0.2, 0.1, 0.0, 0.3])
    terms = T.back(space.terms, space.axes, mood)
    for title in (pool[0], pool[7], {"c2_1": 0.8}):
        centred = T.centred(title, space.frame)
        by_terms = sum(w * centred.get(t, 0.0) for t, w in terms.items())
        assert by_terms == pytest.approx(float(mood @ np.asarray(space.project(title))))
    assert json.loads(json.dumps(terms)) == pytest.approx(terms), "stored as jsonb"


# Decision 218: 32% of the library has no DNA; centring an absent term made every untagged
# candidate the same negative point, moving them as a block.


def test_an_untagged_title_sits_at_zero_on_every_direction():
    pool = {**clustered(), 900: {}, 901: {}}
    space = T.space(pool)
    assert T.centred({}, space.frame) == {}, "a vector with no terms has no coordinates"
    assert space.project(pool[900]) == pytest.approx((0.0,) * rnd.MOOD_DIRECTIONS)


def test_the_frame_counts_an_absent_term_as_a_zero_and_centred_gives_it_no_coordinate():
    """Absence is an observation in `frame` and no statement in `centred` (decision 218)."""
    with_an_untagged_title = T.frame({**POOL, 4: {}})

    assert with_an_untagged_title.mean["cosy"] == pytest.approx(0.375), (
        "an untagged title is a fourth observation of zero, not three observations and a gap"
    )
    assert T.frame(POOL).mean["cosy"] == pytest.approx(0.5), "the same pool without it"
    assert T.centred({}, with_an_untagged_title) == {}, "absence is no position to report"
    assert "dread" not in T.centred({"cosy": 1.0}, with_an_untagged_title)


def test_a_term_one_film_does_not_carry_is_absent_rather_than_fabricated():
    """The difference of two films is the chosen film's own coordinate where only it carries the term."""
    mixed = {1: {"cosy": 1.0, "dread": 0.2}, 2: {"dread": 1.0}, 3: {"cosy": 0.4, "dread": 0.5}}
    f = T.frame(mixed)
    assert "cosy" not in T.centred(mixed[2], f)
    assert T.centred(mixed[1], f)["cosy"] > 0.0
