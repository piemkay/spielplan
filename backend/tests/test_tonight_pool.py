"""Pure: the pool's arithmetic. Aggregation rules agree with the mean on most inputs, so the
fixtures are the sets where they disagree."""

from __future__ import annotations

import pytest

from spielplan.tonight import pool

# §6.2 step 1's default, and the slider's own bounds.
BUDGET = 130


def candidate(title_id, scores, *, runtime=100, kind="movie"):
    """A seat id is not a user id: a guest seat has no user at all."""
    return pool.Candidate(
        title_id=title_id,
        kind=kind,
        name=f"T{title_id}",
        runtime_min=runtime,
        scores={i + 1: float(s) for i, s in enumerate(scores)},
    )


def test_the_group_score_is_the_unweighted_arithmetic_mean():
    assert pool.group_score({1: 1.0, 2: 0.0}) == pytest.approx(0.5)
    assert pool.group_score({1: 0.2, 2: 0.4, 3: 0.9}) == pytest.approx(0.5)
    # One seat is the degenerate case and must not be special-cased.
    assert pool.group_score({1: 0.37}) == pytest.approx(0.37)


def test_the_pool_is_ordered_by_the_mean_and_not_by_the_minimum():
    """`low` wins max-min (worst 0.45); `high` wins the mean (0.55 vs 0.475). §0 row 3 measured the
    max-min family at -0.012."""
    low = candidate(1, [0.45, 0.50])       # min 0.45, mean 0.475
    high = candidate(2, [0.20, 0.90])      # min 0.20, mean 0.55
    ordered = pool.order([low, high])

    assert [c.title_id for c in ordered] == [2, 1]
    assert min(low.scores.values()) > min(high.scores.values()), (
        "the fixture is only meaningful while max-min prefers the other title"
    )


def test_no_dominance_rule_survives_in_the_ordering():
    """The Nash product ranks `even` first (0.36 vs 0.12); the mean ranks `lopsided` (0.65 vs 0.60)."""
    even = candidate(1, [0.60, 0.60])       # mean 0.60, product 0.36
    lopsided = candidate(2, [0.10, 1.20])   # mean 0.65, product 0.12
    ordered = pool.order([even, lopsided])

    assert [c.title_id for c in ordered] == [2, 1]


def test_every_member_counts_equally_regardless_of_who_they_are():
    """Symmetry is the claim: permuting seats cannot move the group score."""
    assert pool.group_score({1: 0.2, 2: 0.8}) == pytest.approx(pool.group_score({1: 0.8, 2: 0.2}))
    assert pool.group_score({7: 0.2, 3: 0.8}) == pytest.approx(pool.group_score({1: 0.2, 2: 0.8}))


def test_a_guest_seat_contributes_no_term_to_the_average():
    """The guest is seated, served pairs and votes, but the pool's order is the members'."""
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
    """Computed once and carried, so equal scores must not shuffle between builds."""
    a = candidate(1, [0.5, 0.5])
    b = candidate(2, [0.5, 0.5])          # the tie: a and b must not swap between builds
    c = candidate(3, [0.9, 0.9])
    assert [x.title_id for x in pool.order([b, a, c])] == [3, 1, 2]
    assert [x.title_id for x in pool.order([c, b, a])] == [3, 1, 2]


def test_the_budget_is_soft_by_exactly_forty_minutes():
    """Both edges: a hard cut and an unbounded one each pass a middle-only test."""
    assert pool.admits(runtime_min=BUDGET, budget_min=BUDGET)
    assert pool.admits(runtime_min=BUDGET + 40, budget_min=BUDGET)
    assert not pool.admits(runtime_min=BUDGET + 41, budget_min=BUDGET)
    assert pool.admits(runtime_min=30, budget_min=BUDGET)


def test_a_title_of_unknown_runtime_is_admitted_rather_than_dropped():
    """The budget is soft; an unmeasured title is not evidence it runs long."""
    assert pool.admits(runtime_min=None, budget_min=BUDGET)
    assert pool.over_budget_by(runtime_min=None, budget_min=BUDGET) is None


def test_an_over_budget_title_says_how_far_over_and_a_fitting_one_says_nothing():
    """N is measured from the budget the person set, not the +40 bound."""
    assert pool.over_budget_by(runtime_min=151, budget_min=BUDGET) == 21
    assert pool.over_budget_by(runtime_min=BUDGET + 40, budget_min=BUDGET) == 40
    assert pool.over_budget_by(runtime_min=BUDGET, budget_min=BUDGET) is None
    assert pool.over_budget_by(runtime_min=90, budget_min=BUDGET) is None


def test_the_fit_line_reads_the_way_the_spec_writes_it():
    """§6.2 step 8 fixes both branches verbatim: "Fits your time" / "21 min over"."""
    assert pool.fit_line(runtime_min=110, budget_min=BUDGET, kind="movie") == "Fits your time"
    assert pool.fit_line(runtime_min=151, budget_min=BUDGET, kind="movie") == "21 min over"
    assert pool.fit_line(runtime_min=None, budget_min=BUDGET, kind="movie") == "Runtime unknown"


def test_a_series_label_says_which_minutes_it_is_counting():
    """Decision 219: a series budget is per episode, and a label with a number says so; the
    arithmetic is unchanged."""
    assert pool.fit_line(runtime_min=45, budget_min=60, kind="series") == "Fits your time"
    assert pool.fit_line(runtime_min=81, budget_min=60, kind="series") == (
        "21 min over per episode"
    )
    # "Runtime unknown" measures nothing, so no per-episode note.
    assert pool.fit_line(runtime_min=None, budget_min=60, kind="series") == "Runtime unknown"
    # The bound is untouched by the label, on either kind.
    assert pool.admits(runtime_min=45, budget_min=60)
    assert pool.over_budget_by(runtime_min=81, budget_min=60) == 21


def test_a_candidate_carries_its_own_over_budget_label():
    """Three surfaces render it, so the label travels with the candidate."""
    long = candidate(1, [0.5, 0.5], runtime=151)
    short = candidate(2, [0.5, 0.5], runtime=100)
    built = pool.with_budget([long, short], budget_min=BUDGET)
    by_id = {c.title_id: c for c in built}

    assert by_id[1].over_budget_min == 21
    assert by_id[1].fit_line == "21 min over"
    assert by_id[2].over_budget_min is None
    assert by_id[2].fit_line == "Fits your time"


def test_a_series_candidate_is_stamped_with_the_qualifier_and_a_film_is_not():
    """Stamped once off the candidate's kind, so all three surfaces agree (decision 219)."""
    built = pool.with_budget(
        [candidate(1, [0.5], runtime=150, kind="series"), candidate(2, [0.5], runtime=150)],
        budget_min=BUDGET,
    )
    by_id = {c.title_id: c for c in built}

    assert by_id[1].fit_line == "20 min over per episode"
    assert by_id[2].fit_line == "20 min over"


def test_the_budget_filter_drops_only_what_it_must():
    """One pass, so admission and label cannot disagree about the boundary."""
    built = pool.with_budget(
        [candidate(i, [0.5], runtime=r) for i, r in enumerate([100, 170, 171, 200], start=1)],
        budget_min=BUDGET,
    )
    assert [c.title_id for c in built] == [1, 2]


def test_each_members_scores_are_rank_standardised_over_the_frozen_pool():
    """Decision 477: `rank_normal` maps each member's order onto the same normal quantiles: order
    kept, spread equal, ties broken by title id."""
    import statistics

    heavy = {1: 13.28, 2: 9.43, 3: 6.52, 4: 5.82, **{t: 0.01 * t for t in range(5, 205)}}
    std = pool.rank_normal(heavy)

    assert sorted(std, key=std.__getitem__) == sorted(heavy, key=lambda t: (heavy[t], t)), (
        "a member's own order is untouched"
    )
    assert statistics.pstdev(std.values()) == pytest.approx(pool.SCALE_SD, abs=0.02)
    assert max(std.values()) < 3.0, "the runaway favourite counts for the top quantile, not 13"
    assert statistics.mean(std.values()) == pytest.approx(0.0, abs=1e-9)

    tied = pool.rank_normal({9: 0.5, 3: 0.5, 5: 0.5})
    assert tied[3] < tied[5] < tied[9], "ties go by title id, the same way on every read"
    assert pool.rank_normal({7: 42.0}) == {7: 0.0}, "a pool of one has no spread to map"
    assert pool.rank_normal({}) == {}


def test_two_members_on_different_scales_count_the_same_after_standardising():
    """Raw, the wide member's favourite wins by units alone."""
    wide = {1: 13.0, 2: 0.2, 3: 0.1, 4: 0.0}
    narrow = {1: 0.0, 2: 0.3, 3: 0.2, 4: 0.1}
    raw_winner = max(wide, key=lambda t: (wide[t] + narrow[t]) / 2)
    assert raw_winner == 1, "the defect: one member's scale decides"

    a, b = pool.rank_normal(wide), pool.rank_normal(narrow)
    assert a[1] == pytest.approx(b[2]), "each member's first choice is worth the same"
    assert pool.group_score({1: a[1], 2: b[1]}) < pool.group_score({1: a[2], 2: b[2]}), (
        "and the title both rank near the top beats the one only the wide member loves"
    )


def test_a_veto_names_vocabulary_terms_and_nothing_else():
    """Fixed vocabulary-v1 ids per chip; an unknown key vetoes nothing."""
    assert pool.veto_terms(["violence"]) == ["mood.gory", "mood.violent", "themes.violence"]
    assert pool.veto_terms(["no-such-chip"]) == []
    assert pool.veto_labels(["harrowing", "violence"]) == ["violence", "harrowing"]
    assert all("." in term for _, terms in pool.VETOES.values() for term in terms)
    assert pool.MAX_VETOES == 3


def test_the_pool_excludes_the_union_of_every_members_own_vetoes():
    """Decision 505: the union of each member's own three. A pre-decision room-wide set still counts,
    and both jsonb shapes read the same."""
    import json

    from spielplan.tonight import rooms

    context = {
        "vetoes_by": {"11": ["violence", "horror", "harrowing"], "12": ["sexual_violence"]},
    }
    assert rooms.vetoes_by_seat(context) == {
        11: ["violence", "horror", "harrowing"], 12: ["sexual_violence"],
    }
    assert rooms.vetoes_of(context) == ["violence", "sexual_violence", "horror", "harrowing"]
    assert rooms.vetoes_of(json.dumps(context)) == rooms.vetoes_of(context)

    legacy = {"vetoes": ["horror"], "vetoes_by": {"12": ["violence", "retired-chip"]}}
    assert rooms.vetoes_of(legacy) == ["violence", "horror"]
    assert rooms.vetoes_by_seat({"vetoes_by": {"13": ["retired-chip"]}}) == {}
    assert rooms.vetoes_of({}) == [] and rooms.vetoes_of(None) == []


def test_the_mood_reaches_a_seats_top_thirty_unseen_by_stable_taste():
    """Decision 550: the mood re-ranks only these; a film the seat has seen is never in reach."""
    stable = {t: -float(t) for t in range(1, 50)}
    assert pool.reach(stable) == list(range(1, 31))
    assert pool.reach(stable, seen={1, 2}) == list(range(3, 33))
    assert pool.reach({1: 0.5, 2: 0.5}) == [1, 2], "ties by id, so two builds agree"
