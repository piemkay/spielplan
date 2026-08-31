"""§6.2 step 3's candidate pool, and the two rules it is built out of.

Spec v2.1 §6.2 steps 1 and 3, §0 row 3, §4.1 rule 5, §5.1.

Pure: the pool's *membership* is a query and lives in the integration tests, but its
**arithmetic** — which title outranks which, and how far over budget a title runs — is a
function of numbers, and pushing it through Postgres would test the same arithmetic through a
socket. Same split `rank/board.py` and `rank/queue.py` already use.

Two sentences of §6.2 are load-bearing here, and each has a way of going quietly wrong.

  * "ranked by the **plain average** of member Ledger scores (measured: nothing dominates
    averaging; dominance rules cost −0.012)". Every aggregation rule agrees with the mean on
    most inputs — max-min, the Nash product and the mean give the same order on any set where
    one title simply beats another for everybody. They are told apart only on the sets where
    they disagree, so those are the sets these tests are built from.
  * "a **runtime budget slider** (soft — the pool admits up to budget + 40 min; over-budget
    results are labelled 'runs N min over')". A hard cut passes every test that only asks
    whether short films are admitted, and a label computed off the wrong end passes every test
    that only checks a label exists.
"""

from __future__ import annotations

import pytest

from spielplan.tonight import pool

# §6.2 step 1's default, and the slider's own bounds.
BUDGET = 130


def candidate(title_id, scores, *, runtime=100, kind="movie"):
    """A candidate whose seats are 1..n. A seat id is not a user id — a session has seats, and
    a guest seat has no user at all."""
    return pool.Candidate(
        title_id=title_id,
        kind=kind,
        name=f"T{title_id}",
        runtime_min=runtime,
        scores={i + 1: float(s) for i, s in enumerate(scores)},
    )


# --- §0 row 3: the plain average, and the rules it is not ---------------------------------


def test_the_group_score_is_the_unweighted_arithmetic_mean():
    assert pool.group_score({1: 1.0, 2: 0.0}) == pytest.approx(0.5)
    assert pool.group_score({1: 0.2, 2: 0.4, 3: 0.9}) == pytest.approx(0.5)
    # One seat is the degenerate case and must not be special-cased into something else.
    assert pool.group_score({1: 0.37}) == pytest.approx(0.37)


def test_the_pool_is_ordered_by_the_mean_and_not_by_the_minimum():
    """The set that tells them apart. `low` is the max-min winner (its worst score is 0.45);
    `high` is the mean winner (0.55 against 0.475). A "protect the least happy person" rule —
    the intuitive one, and the one v1.1's fairness ledger encoded — puts them the other way
    round, and §0 row 3 measured that family at −0.012 against a 0.003–0.008 noise floor."""
    low = candidate(1, [0.45, 0.50])       # min 0.45, mean 0.475
    high = candidate(2, [0.20, 0.90])      # min 0.20, mean 0.55
    ordered = pool.order([low, high])

    assert [c.title_id for c in ordered] == [2, 1]
    assert min(low.scores.values()) > min(high.scores.values()), (
        "the fixture is only meaningful while max-min prefers the other title"
    )


def test_no_dominance_rule_survives_in_the_ordering():
    """The Nash product is the other rule v1.1 proposed, and it is not the arithmetic mean:
    it prefers balance multiplicatively. Here it ranks `even` first (0.36 vs 0.09) while the
    mean ranks `lopsided` first (0.65 vs 0.60)."""
    even = candidate(1, [0.60, 0.60])       # mean 0.60, product 0.36
    lopsided = candidate(2, [0.10, 1.20])   # mean 0.65, product 0.12
    ordered = pool.order([even, lopsided])

    assert [c.title_id for c in ordered] == [2, 1]


def test_every_member_counts_equally_regardless_of_who_they_are():
    """"Plain" is the load-bearing word: no seat is weighted by its label count, its seniority,
    or by being the host. Symmetry is the whole claim, so it is asserted as symmetry —
    permuting which seat holds which score cannot move the group score."""
    assert pool.group_score({1: 0.2, 2: 0.8}) == pytest.approx(pool.group_score({1: 0.8, 2: 0.2}))
    assert pool.group_score({7: 0.2, 3: 0.8}) == pytest.approx(pool.group_score({1: 0.2, 2: 0.8}))


def test_a_guest_seat_contributes_no_term_to_the_average():
    """§6.2 step 3: "Guests contribute no taste term unless they have a grid profile." The
    guest is seated, is served pairs, and votes — but the pool's own order is the members'."""
    members = [
        pool.Seat(participant_id=1, user_id=100, is_member=True),
        pool.Seat(participant_id=2, user_id=None, is_member=False),
    ]
    scored = pool.score_for_seats({1: 0.9}, members)
    assert scored == {1: 0.9}, "a guest seat must not appear in the scored map at all"
    assert pool.group_score(scored) == pytest.approx(0.9), (
        "a guest counted as a zero would drag every title toward the bottom equally, which is "
        "a different bug from contributing nothing and looks identical on one title"
    )


def test_the_order_is_stable_and_deterministic():
    """§6.2's pool is computed once at session open and carried (nothing re-ranks within the
    evening). Two builds over the same numbers must not disagree, and equal scores must not
    shuffle — a pool that re-sorts on a tie moves titles under the round's feet."""
    a = candidate(1, [0.5, 0.5])
    b = candidate(2, [0.5, 0.5])          # the tie: a and b must not swap between builds
    c = candidate(3, [0.9, 0.9])
    assert [x.title_id for x in pool.order([b, a, c])] == [3, 1, 2]
    assert [x.title_id for x in pool.order([c, b, a])] == [3, 1, 2]


def test_no_tilt_can_reach_the_pool_prior():
    """§0 row 4: the stored mood profile is worth **0.000** for choose-tonight. The pool is
    ranked by the Ledger alone; the tilt is a thing the *round* learns and applies to the
    tonight score, never to the prior the round starts from.

    Structural, deliberately: `order` and `group_score` take scores and nothing else, so a
    tilt cannot be passed even by mistake."""
    import inspect

    for fn in (pool.group_score, pool.order):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"tilt", "tilts", "mood"}), (
            f"{fn.__name__} must not accept a tilt: §0 row 4 measured the stored profile at 0.000"
        )


# --- §6.2 step 1: the soft runtime budget --------------------------------------------------


def test_the_budget_is_soft_by_exactly_forty_minutes():
    """"the pool admits up to budget + 40 min". Both edges, because a hard cut and an
    unbounded one each pass a test that only checks the middle."""
    assert pool.admits(runtime_min=BUDGET, budget_min=BUDGET)
    assert pool.admits(runtime_min=BUDGET + 40, budget_min=BUDGET)
    assert not pool.admits(runtime_min=BUDGET + 41, budget_min=BUDGET)
    assert pool.admits(runtime_min=30, budget_min=BUDGET)


def test_a_title_of_unknown_runtime_is_admitted_rather_than_dropped():
    """`title.runtime_min` is nullable and the corpus has gaps. The budget is soft by design,
    so a title nobody can measure is not evidence that it runs long — dropping it would remove
    a watchable film from the evening over a missing metadata field."""
    assert pool.admits(runtime_min=None, budget_min=BUDGET)
    assert pool.over_budget_by(runtime_min=None, budget_min=BUDGET) is None


def test_an_over_budget_title_says_how_far_over_and_a_fitting_one_says_nothing():
    """§6.2 step 1: "over-budget results are labelled 'runs N min over'". N is measured from
    the *budget*, not from the +40 admission bound — the label a person reads has to be about
    the number they set on the slider."""
    assert pool.over_budget_by(runtime_min=151, budget_min=BUDGET) == 21
    assert pool.over_budget_by(runtime_min=BUDGET + 40, budget_min=BUDGET) == 40
    assert pool.over_budget_by(runtime_min=BUDGET, budget_min=BUDGET) is None
    assert pool.over_budget_by(runtime_min=90, budget_min=BUDGET) is None


def test_the_fit_line_reads_the_way_the_spec_writes_it():
    """§6.2 step 7 fixes both branches verbatim: "fits your 130 min" / "runs 21 min over"."""
    assert pool.fit_line(runtime_min=110, budget_min=BUDGET) == "fits your 130 min"
    assert pool.fit_line(runtime_min=151, budget_min=BUDGET) == "runs 21 min over"
    assert pool.fit_line(runtime_min=None, budget_min=BUDGET) == "runtime unknown"


def test_a_candidate_carries_its_own_over_budget_label():
    """The pool is built once and carried, so the label travels with the candidate rather than
    being recomputed by each surface that renders it — three surfaces render it (the round's
    pair, the result card, solo) and three implementations would drift."""
    long = candidate(1, [0.5, 0.5], runtime=151)
    short = candidate(2, [0.5, 0.5], runtime=100)
    built = pool.with_budget([long, short], budget_min=BUDGET)
    by_id = {c.title_id: c for c in built}

    assert by_id[1].over_budget_min == 21
    assert by_id[1].fit_line == "runs 21 min over"
    assert by_id[2].over_budget_min is None
    assert by_id[2].fit_line == "fits your 130 min"


def test_the_budget_filter_drops_only_what_it_must():
    """Admission and labelling are one pass, so a title cannot be admitted by one rule and
    labelled by another that disagrees about where the boundary is."""
    built = pool.with_budget(
        [candidate(i, [0.5], runtime=r) for i, r in enumerate([100, 170, 171, 200], start=1)],
        budget_min=BUDGET,
    )
    assert [c.title_id for c in built] == [1, 2]
