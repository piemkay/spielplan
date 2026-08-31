"""The mood tilt: chosen-minus-rejected DNA, centred on the candidate-pool mean.

Spec v2.1 §6.2 steps 4-5, §0 row 4, §4.1 rules 1 and 2, §4.2 `session_participant.tilt`.

§0 row 4 is the measurement this exists for: the stored 8-axis mood profile is worth **0.000**
for choose-tonight, while **3 shortlist-anchored answers ≈ +0.088 AUC**, and "centring on the
shortlist matters more than question form". So the centring is not a normalisation detail — it
is the one piece of the deleted mood machinery that earned its place, and a tilt that is not
pool-relative is measurably the worthless version.

THE ARITHMETIC PROBLEM §6.2's WORDING HIDES, stated here because these tests are what force the
resolution. For a *difference*, additive centring cancels exactly:

    (a - m) - (b - m) = a - b

so "chosen-minus-rejected DNA centred on the pool mean", read as subtraction alone, is
literally a no-op on every A/B answer and cannot be the measured lever. The pool has to reach
the tilt some other way, and the coverage row says which: "the identical answer on the identical
pair produces a different tilt when the surrounding pool differs". Standardising — centre on the
pool mean and scale by the pool's own spread — is the reading under which every word of §6.2 is
true and the row's requirement holds. Recorded as a spec defect for v2.2.
"""

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
    """§6.2 step 5: "chosen-minus-rejected DNA"."""
    frame = T.frame(POOL)
    moved = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert moved["cosy"] > 0.0, "the cosy one was chosen"
    assert moved["dread"] < 0.0, "the dreadful one was rejected"


def test_the_same_answer_on_the_same_pair_tilts_differently_in_a_different_pool():
    """The row's own test, and the reason the centring is not decoration: an answer means
    something different in a pool where everything is cosy than in one where nothing is.

    A no-op implementation — plain `chosen - rejected` — returns the identical vector for both,
    which is exactly the version §0 row 4 measured at 0.000.
    """
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
    """§0 row 4: "**centring on the shortlist** matters more than question form". A frame built
    from the whole library is the version that was measured worse."""
    library = {**POOL, 9: {"cosy": 9.0, "dread": 9.0}, 10: {"cosy": -9.0, "dread": -9.0}}
    on_pool = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(POOL))
    on_library = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=T.frame(library))

    assert on_pool["cosy"] != pytest.approx(on_library["cosy"])


def test_either_lifts_the_pool_frame_toward_both_and_neither_away_from_both():
    """Decision 154 reaches the tilt too: `either` says *both of these would do*, which is a
    statement about a region of DNA space, and `neither` says the opposite of it.

    The pair is two cosy films rather than the pool's two extremes: "either of these" between a
    film and its opposite is genuinely directionless, and a fixture that used it would assert
    the level answer is a no-op while proving only that the pair was symmetric.
    """
    frame = T.frame(POOL)
    cosy_pair = dict(first=POOL[1], second={"cosy": 0.9, "dread": 0.1})
    either = T.observe_level({}, **cosy_pair, frame=frame, toward=True)
    neither = T.observe_level({}, **cosy_pair, frame=frame, toward=False)

    for facet in ("cosy", "dread"):
        assert either[facet] == pytest.approx(-neither[facet])
    assert either["cosy"] > 0.0, "a level answer about two cosy films leans cosy"
    assert any(v != 0.0 for v in either.values()), "a level answer is not a no-op"


def test_a_level_answer_about_the_pools_own_centre_says_nothing():
    """Title 3 sits at the pool mean on both facets, so "either of these" between it and itself
    carries no directional information — which is the property centring buys, and the reason a
    pair of unremarkable films is not a mood."""
    frame = T.frame(POOL)
    moved = T.observe_level({}, first=POOL[3], second=POOL[3], frame=frame, toward=True)
    assert all(v == pytest.approx(0.0, abs=1e-9) for v in moved.values())


def test_the_tilt_accumulates_across_answers():
    """§4.2 stores one tilt per participant, not one per answer: the round's answers compound."""
    frame = T.frame(POOL)
    once = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)
    twice = T.observe(once, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert twice["cosy"] == pytest.approx(2 * once["cosy"])


def test_an_empty_tilt_changes_no_score():
    """A participant who has answered nothing is ranked by their Ledger alone — 54f's solo mode
    "ranked by the personal Ledger with **no tilt**" depends on this being exactly zero."""
    frame = T.frame(POOL)
    assert T.adjustment({}, POOL[1], frame) == pytest.approx(0.0)


def test_the_tilt_raises_candidates_that_look_like_what_was_chosen():
    """The tilt is applied against the *centred* candidate, not the raw one — the same frame on
    both sides, or the adjustment would measure a candidate's absolute DNA rather than its
    position in tonight's pool."""
    frame = T.frame(POOL)
    tilted = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)

    assert T.adjustment(tilted, POOL[1], frame) > 0.0
    assert T.adjustment(tilted, POOL[2], frame) < 0.0
    assert T.adjustment(tilted, POOL[3], frame) == pytest.approx(0.0, abs=1e-9)


def test_a_facet_the_pool_does_not_vary_on_contributes_nothing():
    """A facet every candidate shares carries no information about tonight, and dividing by its
    zero spread would be an infinity rather than an insight."""
    flat = {1: {"period": 1.0, "cosy": 1.0}, 2: {"period": 1.0, "cosy": 0.0}}
    frame = T.frame(flat)
    moved = T.observe({}, chosen=flat[1], rejected=flat[2], frame=frame)

    assert moved.get("period", 0.0) == pytest.approx(0.0), (
        "a facet with no spread is dropped rather than divided by zero; either way it moves "
        "the tilt not at all"
    )
    assert moved["cosy"] != pytest.approx(0.0)


def test_the_tilt_round_trips_through_json():
    """§4.2 stores it as jsonb on `session_participant`, so it has to be a plain mapping of
    term to float — not a numpy array, and not a class the column cannot hold."""
    import json

    frame = T.frame(POOL)
    moved = T.observe({}, chosen=POOL[1], rejected=POOL[2], frame=frame)
    assert json.loads(json.dumps(moved)) == pytest.approx(moved)
