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
    assert pool.fit_line(runtime_min=110, budget_min=BUDGET, kind="movie") == "fits your 130 min"
    assert pool.fit_line(runtime_min=151, budget_min=BUDGET, kind="movie") == "runs 21 min over"
    assert pool.fit_line(runtime_min=None, budget_min=BUDGET, kind="movie") == "runtime unknown"


def test_a_series_label_says_which_minutes_it_is_counting():
    """54h, amending §6.2 step 1: "On a **series** session the budget is **per episode** … and
    every label that states a number on a series card says so ("fits your 60 min per episode")".

    The arithmetic does not move and is not meant to: `title.runtime_min` is per-episode for a
    series, so the bound was always per-episode and only the label was silent about it. Measured
    against the shipped bundle the series pool is 121 of 121 owned titles at budget 60, 130 and
    200 alike — the slider narrows nothing on a series night, and a bare "fits your 60 min" on a
    24 min/ep, 293-episode show reads as a promise about the evening. [decision 219]
    """
    assert pool.fit_line(runtime_min=45, budget_min=60, kind="series") == (
        "fits your 60 min per episode"
    )
    assert pool.fit_line(runtime_min=81, budget_min=60, kind="series") == (
        "runs 21 min over per episode"
    )
    # No number, no qualifier: "runtime unknown" measures nothing, and a per-episode note on it
    # would be precision about an absence.
    assert pool.fit_line(runtime_min=None, budget_min=60, kind="series") == "runtime unknown"
    # And the bound itself is untouched by the label, on either kind.
    assert pool.admits(runtime_min=45, budget_min=60)
    assert pool.over_budget_by(runtime_min=81, budget_min=60) == 21


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


def test_a_series_candidate_is_stamped_with_the_qualifier_and_a_film_is_not():
    """The one pass stamps 54h's qualifier too, off the candidate's own kind — so the three
    surfaces that render `fit_line` (the pair card, the reveal, solo) all say the same thing
    without any of them knowing what kind of evening it is. [decision 219]"""
    built = pool.with_budget(
        [candidate(1, [0.5], runtime=45, kind="series"), candidate(2, [0.5], runtime=110)],
        budget_min=BUDGET,
    )
    by_id = {c.title_id: c for c in built}

    assert by_id[1].fit_line == "fits your 130 min per episode"
    assert by_id[2].fit_line == "fits your 130 min"


def test_the_budget_filter_drops_only_what_it_must():
    """Admission and labelling are one pass, so a title cannot be admitted by one rule and
    labelled by another that disagrees about where the boundary is."""
    built = pool.with_budget(
        [candidate(i, [0.5], runtime=r) for i, r in enumerate([100, 170, 171, 200], start=1)],
        budget_min=BUDGET,
    )
    assert [c.title_id for c in built] == [1, 2]


# --- decision 477: one scale for every member ----------------------------------------------


def test_each_members_scores_are_rank_standardised_over_the_frozen_pool():
    """The first household evening, in miniature (decision 477).

    One member's raw scores ran to 13.28 against the other's 3.13 because §5.1's cf half is
    standardised over a population the owned pool is not drawn from, so the plain average was one
    person's Ledger. `rank_normal` maps each member's own order onto the same normal quantiles:
    monotone, so nobody's order moves; the same spread for everyone, so nobody's units outvote
    anybody's; and ties broken by title id, so two reads of one frozen pool agree to the bit.
    """
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
    """The plain average stays plain (§0 row 3) — it is taken over scores that mean the same
    thing for each member. Raw, the wide member's favourite wins the average by their units
    alone; standardised, each member's own first choice is worth the same."""
    wide = {1: 13.0, 2: 0.2, 3: 0.1, 4: 0.0}
    narrow = {1: 0.0, 2: 0.3, 3: 0.2, 4: 0.1}
    raw_winner = max(wide, key=lambda t: (wide[t] + narrow[t]) / 2)
    assert raw_winner == 1, "the defect: one member's scale decides"

    a, b = pool.rank_normal(wide), pool.rank_normal(narrow)
    assert a[1] == pytest.approx(b[2]), "each member's first choice is worth the same"
    assert pool.group_score({1: a[1], 2: b[1]}) < pool.group_score({1: a[2], 2: b[2]}), (
        "and the title both rank near the top beats the one only the wide member loves"
    )


# --- decision 480: "not tonight" ------------------------------------------------------------


def test_a_veto_names_vocabulary_terms_and_nothing_else():
    """Each chip is a fixed set of vocabulary-v1 ids, so a veto is a presence predicate over the
    same terms every other DNA read uses; an unknown key vetoes nothing rather than failing."""
    assert pool.veto_terms(["violence"]) == ["mood.gory", "mood.violent", "themes.violence"]
    assert pool.veto_terms(["no-such-chip"]) == []
    assert pool.veto_labels(["harrowing", "violence"]) == ["violence", "harrowing"]
    assert all("." in term for _, terms in pool.VETOES.values() for term in terms)
    assert pool.MAX_VETOES == 3


def test_a_veto_reads_both_tiers_by_name_and_never_a_weight():
    """§4.1 rules 1 and 2 on the predicate (decisions 480 and 504): it names the tiers, so the
    discriminator is kept, and it compares no salience, confidence or weight — a threshold on a
    weight is the cut §4.1 rule 2 forbids. Both tiers, because the quote-verified one alone served
    John Wick, Transformers: Revenge of the Fallen and In Bruges to the member of the second
    household evening who had ruled out violence: each carries the term by projection alone."""
    import inspect
    import re

    assert pool.VETO_TIERS == ("extracted", "projected")
    source = inspect.getsource(pool.build)
    predicate = source[source.index("FROM dna_tagged d"):source.index("list(vetoed_terms)")]
    assert "d.tier = ANY($7::text[])" in predicate
    assert not re.search(r"salience|confidence|weight", predicate), predicate
    assert "list(VETO_TIERS)" in source, "the tiers are named where the statement is bound"


def test_the_pool_excludes_the_union_of_every_members_own_vetoes():
    """Decision 505: each seated member holds up to three, and a title any of them ruled out is out.
    On the second household evening the first member to tap took the room's three and the other
    could add none. A room opened before the decision wrote one room-wide set, which still counts,
    so an evening in flight across the deploy keeps what it ruled out; a retired key vetoes
    nothing, and both jsonb shapes read the same."""
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
