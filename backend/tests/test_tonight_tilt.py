"""Additive centring cancels in a difference: (a - m) - (b - m) = a - b. So the tilt standardises
by the pool's spread, which makes the same answer mean different things in different pools."""

from __future__ import annotations

import pytest

from spielplan.tonight import tilt as T

# Two facets is enough: one the pool varies on and one it does not.
POOL = {
    1: {"cosy": 1.0, "dread": 0.0},
    2: {"cosy": 0.0, "dread": 1.0},
    3: {"cosy": 0.5, "dread": 0.5},
}


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


def test_a_separating_answer_tilts_toward_the_chosen_title():
    frame = T.frame(POOL)
    moved = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert moved["cosy"] > 0.0, "the cosy one was chosen"
    assert moved["dread"] < 0.0, "the dreadful one was rejected"


def test_the_same_answer_on_the_same_pair_tilts_differently_in_a_different_pool():
    """A plain `chosen - rejected` returns the same vector for both pools: §0 row 4's 0.000 version."""
    narrow = {1: POOL[1], 2: POOL[2], 3: {"cosy": 0.49, "dread": 0.51}}
    wide = {1: POOL[1], 2: POOL[2], 3: {"cosy": 0.5, "dread": 0.5},
            4: {"cosy": 5.0, "dread": -5.0}, 5: {"cosy": -5.0, "dread": 5.0}}

    in_narrow = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(narrow))
    in_wide = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(wide))

    assert in_narrow["cosy"] != pytest.approx(in_wide["cosy"]), (
        "an additive centring cancels on a difference; the pool must reach the tilt some way "
        "that does not"
    )
    assert in_narrow["cosy"] > in_wide["cosy"], (
        "the same choice is a stronger statement in a pool where the two are the extremes"
    )


def test_centring_on_a_library_mean_instead_of_the_pool_is_a_different_answer():
    """§0 row 4: centring on the shortlist beats a library-wide frame."""
    library = {**POOL, 9: {"cosy": 9.0, "dread": 9.0}, 10: {"cosy": -9.0, "dread": -9.0}}
    on_pool = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(POOL))
    on_library = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(library))

    assert on_pool["cosy"] != pytest.approx(on_library["cosy"])


def test_either_lifts_the_pool_frame_toward_both_and_neither_away_from_both():
    """Decision 154: `either` lifts toward both, `neither` away. Two cosy films, because a film and
    its opposite would make the level answer trivially directionless."""
    frame = T.frame(POOL)
    cosy_pair = dict(first=POOL[1], second={"cosy": 0.9, "dread": 0.1})
    either = T.observe_level({}, **cosy_pair, frame=frame, toward=True)
    neither = T.observe_level({}, **cosy_pair, frame=frame, toward=False)

    for facet in ("cosy", "dread"):
        assert either[facet] == pytest.approx(-neither[facet])
    assert either["cosy"] > 0.0, "a level answer about two cosy films leans cosy"
    assert any(v != 0.0 for v in either.values()), "a level answer is not a no-op"


def test_a_level_answer_about_the_pools_own_centre_says_nothing():
    """Title 3 sits at the pool mean on both facets, so a level answer about it carries nothing."""
    frame = T.frame(POOL)
    moved = T.observe_level({}, first=POOL[3], second=POOL[3], frame=frame, toward=True)
    assert all(v == pytest.approx(0.0, abs=1e-9) for v in moved.values())


def test_the_tilt_accumulates_across_answers():
    """One tilt per participant: answers compound."""
    frame = T.frame(POOL)
    once = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)
    twice = T.observe(once, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert twice["cosy"] == pytest.approx(2 * once["cosy"])


def test_an_empty_tilt_changes_no_score():
    """54f's solo is ranked with no tilt, which relies on this being exactly zero."""
    frame = T.frame(POOL)
    assert T.adjustment({}, POOL[1], frame) == pytest.approx(0.0)


def test_the_tilt_raises_candidates_that_look_like_what_was_chosen():
    """The same frame on both sides, or it would measure absolute DNA, not position in the pool."""
    frame = T.frame(POOL)
    tilted = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert T.adjustment(tilted, POOL[1], frame) > 0.0
    assert T.adjustment(tilted, POOL[2], frame) < 0.0
    assert T.adjustment(tilted, POOL[3], frame) == pytest.approx(0.0, abs=1e-9)


def test_a_facet_the_pool_does_not_vary_on_contributes_nothing():
    """Dividing by a zero spread would be infinity, not insight."""
    flat = {1: {"period": 1.0, "cosy": 1.0}, 2: {"period": 1.0, "cosy": 0.0}}
    frame = T.frame(flat)
    moved = T.observe({}, chosen=flat[1], rejected=flat[2], frame=frame)

    assert moved.get("period", 0.0) == pytest.approx(0.0), (
        "a facet with no spread is dropped rather than divided by zero; either way it moves "
        "the tilt not at all"
    )
    assert moved["cosy"] != pytest.approx(0.0)


def test_the_tilt_round_trips_through_json():
    """Stored as jsonb: a plain mapping of term to float."""
    import json

    frame = T.frame(POOL)
    moved = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)
    assert json.loads(json.dumps(moved)) == pytest.approx(moved)


# Decision 218: 32% of the library has no DNA; centring an absent term made every untagged
# candidate the same negative point, moving them as a block.


def test_an_untagged_candidate_is_a_zero_rather_than_the_pools_anti_title():
    """Two untagged titles: the defect is that they move as a BLOCK."""
    mixed = {1: {"cosy": 1.0}, 2: {"cosy": 0.0}, 3: {}, 4: {}}
    f = T.frame(mixed)
    tilted = T.observe({}, chosen=mixed[1], rejected=mixed[2], frame=f)

    assert T.centred({}, f) == {}, "a vector with no terms has no coordinates"
    assert T.adjustment(tilted, {}, f) == pytest.approx(0.0), (
        "54f's 'ranked by the personal Ledger with no tilt' is what an absent vector is owed"
    )
    assert T.adjustment(tilted, mixed[3], f) == pytest.approx(0.0)
    assert T.adjustment(tilted, mixed[4], f) == pytest.approx(0.0)


def test_a_term_the_rejected_title_does_not_carry_is_absent_rather_than_fabricated():
    """Decision 218 changes A/B observations too: the answer moves the tilt by the chosen film's own
    coordinate, not minus one the rejected film never earned."""
    mixed = {1: {"cosy": 1.0, "dread": 0.2}, 2: {"dread": 1.0}, 3: {"cosy": 0.4, "dread": 0.5}}
    f = T.frame(mixed)
    moved = T.observe({}, chosen=mixed[1], rejected=mixed[2], frame=f)

    assert moved["cosy"] == pytest.approx(T.centred(mixed[1], f)["cosy"])
    assert moved["dread"] == pytest.approx(
        T.centred(mixed[1], f)["dread"] - T.centred(mixed[2], f)["dread"]
    ), "a term both titles carry is still the difference of the two"


# Finding 37: one helper for the three call sites that had drifted.


def test_one_answer_reaches_the_tilt_the_same_way_whichever_caller_applies_it():
    """Each of decision 154's four answers equals what the callers spelled out by hand."""
    f = T.frame(POOL)
    vectors = dict(POOL)
    kw = dict(title_a=1, title_b=2, vectors=vectors, frame=f)

    assert T.applied({}, answer="A", **kw) == pytest.approx(
        T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=f)
    )
    assert T.applied({}, answer="B", **kw) == pytest.approx(
        T.observe({}, chosen=POOL[2], rejected=POOL[1], frame=f)
    )
    assert T.applied({}, answer="EITHER", **kw) == pytest.approx(
        T.observe_level({}, first=POOL[1], second=POOL[2], frame=f, toward=True)
    )
    assert T.applied({}, answer="NEITHER", **kw) == pytest.approx(
        T.observe_level({}, first=POOL[1], second=POOL[2], frame=f, toward=False)
    )


def test_an_answer_naming_a_title_that_has_left_the_pool_moves_nothing():
    """The posterior and the tilt must skip the same answers (§10), or they are two histories."""
    f = T.frame(POOL)
    started = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=f)

    assert T.applies(POOL, title_a=1, title_b=2) is True
    assert T.applies(POOL, title_a=1, title_b=99) is False
    assert T.applies(POOL, title_a=99, title_b=1) is False
    assert T.applied(
        started, answer="A", title_a=1, title_b=99, vectors=POOL, frame=f
    ) == pytest.approx(started), "an answer the replay ignored may not move the tilt either"


def test_an_answer_naming_one_title_twice_is_not_two_candidates():
    """Membership alone passes one title named twice."""
    f = T.frame(POOL)
    started = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=f)

    assert T.applies(POOL, title_a=1, title_b=1) is False
    assert T.applied(
        started, answer="A", title_a=1, title_b=1, vectors=POOL, frame=f
    ) == pytest.approx(started), "a title compared with itself may not move the tilt"


def test_the_frame_counts_an_absent_term_as_a_zero_and_centred_gives_it_no_coordinate():
    """Absence is an observation in `frame` and no statement in `centred` (decision 218)."""
    with_an_untagged_title = T.frame({**POOL, 4: {}})

    assert with_an_untagged_title.mean["cosy"] == pytest.approx(0.375), (
        "an untagged title is a fourth observation of zero, not three observations and a gap"
    )
    assert with_an_untagged_title.mean["dread"] == pytest.approx(0.375)
    assert T.frame(POOL).mean["cosy"] == pytest.approx(0.5), "the same pool without it"

    assert T.centred({}, with_an_untagged_title) == {}, "absence is no position to report"
    assert T.adjustment({"cosy": 1.0}, {}, with_an_untagged_title) == 0.0
    assert "dread" not in T.centred({"cosy": 1.0}, with_an_untagged_title)
