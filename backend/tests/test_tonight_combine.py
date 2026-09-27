"""Zeroing an axis cannot by itself put the other pole on the slate; the reserved slot replaces the
third, never appends. The conflict copy is bounded by a filter, not by a polite prompt."""

from __future__ import annotations

import math

import pytest

from spielplan.tonight import combine as C
from spielplan.tonight import copy as copy_rules

# One authored axis, §6.4-shaped: term -> weight in [-1, 1], negative = left pole.
AXES = {"mood": {"dread": -1.0, "bleak": -0.8, "cosy": 1.0, "warm": 0.8}}

DNA = {
    1: {"dread": 1.0},            # hard left
    2: {"bleak": 1.0},            # left
    3: {"dread": 0.9},            # left
    4: {"cosy": 1.0},             # hard right
    5: {"warm": 1.0},             # right
    6: {},                        # off the axis entirely
    7: {"dread": 0.95},           # left, and all but a duplicate of title 1
}


def scores(**per_seat):
    """{participant_id: {title_id: tonight score}} from keyword seats p1=..., p2=..."""
    return {int(k[1:]): v for k, v in per_seat.items()}


def test_the_slate_is_exactly_three_finalists_and_one_wildcard():
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 6)},
        dna=DNA, axes=AXES,
    )
    assert len(slate.finalists) == 3
    assert slate.wildcard is not None
    assert slate.wildcard not in slate.finalists, "the wildcard is never counted among the three"
    assert len(slate.ballot_titles) == 4


def test_the_finalists_are_the_top_three_by_the_plain_average():
    """§0 row 3: averaging, not a dominance rule. Title 4 is p1's worst and the group's best."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.60, 2: 0.55, 3: 0.50, 4: 0.10, 5: 0.05},
                               p2={1: 0.20, 2: 0.25, 3: 0.30, 4: 0.99, 5: 0.05}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 6)},
        dna=DNA, axes=AXES,
    )
    assert slate.finalists[0] == 4, "mean 0.545 beats title 1's 0.40"


def test_the_wildcard_is_a_step_outside_rather_than_the_fourth_best():
    """Title 7 is next in rank and near-duplicates the finalists; title 4 is on the opposite pole.
    A rank-drawn wildcard returns 7; an honest one returns 4."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1}),
        member_ledger={t: [0.5, 0.5] for t in (1, 2, 3, 4, 7)},
        dna=DNA, axes=AXES,
    )
    assert slate.finalists == [1, 2, 3]
    assert slate.wildcard == 4, "the furthest in DNA terms, not the next in rank (7)"


def test_every_candidate_lands_in_exactly_one_slot():
    """`session_result` stores one row per candidate, so the slots partition the pool."""
    slate = C.combine(
        per_participant=scores(p1={t: 1.0 - 0.1 * t for t in range(1, 7)},
                               p2={t: 1.0 - 0.1 * t for t in range(1, 7)}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 7)},
        dna=DNA, axes=AXES,
    )
    assert [r["title_id"] for r in slate.rows] == [t for t, _ in slate.ranked]
    assert [r["rank"] for r in slate.rows] == list(range(1, len(slate.rows) + 1))
    by_slot = {}
    for row in slate.rows:
        by_slot.setdefault(row["slot"], []).append(row["title_id"])
    assert sorted(by_slot[C.SLOT_FINALIST]) == sorted(slate.finalists)
    assert by_slot[C.SLOT_WILDCARD] == [slate.wildcard]


def test_d_is_the_mean_minus_the_minimum_of_the_seated_members():
    """Recovered from the prototype's `spread()`; DNA_MODEL is not vendored here."""
    assert C.divergence([0.6, 0.2]) == pytest.approx(0.2)
    assert C.divergence([0.5, 0.5]) == pytest.approx(0.0)
    assert C.divergence([0.9]) == pytest.approx(0.0)
    assert C.divergence([]) == pytest.approx(0.0)


def test_the_threshold_is_inclusive_at_exactly_its_value():
    """`>` would fire just above the threshold and not on it. 0.40 since decision 478; the name no
    longer carries the value."""
    assert C.D_THRESHOLD == 0.40
    at = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={1: [1.2, 0.4], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert at.d == pytest.approx(C.D_THRESHOLD)
    assert at.contested is not None, "D exactly at the threshold is a split"


def test_below_the_threshold_the_split_is_decided_silently():
    """No copy, no zeroed facet, no reserved slot, and still three."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={1: [1.199, 0.401], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert quiet.d == pytest.approx(C.D_THRESHOLD - 0.001)
    assert quiet.contested is None
    assert quiet.conflict is None
    assert len(quiet.finalists) == 3


def test_the_fire_rate_is_recoverable_from_the_slate():
    """§14 risk 6 needs the fire rate, so whether it fired is stored, not inferred from copy."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 5)},
        dna=DNA, axes=AXES,
    )
    assert quiet.conflict is None and quiet.d == pytest.approx(0.0)


def test_divergent_answers_surface_a_split_even_when_d_is_zero():
    """Identical Ledgers can still answer tonight in opposite directions."""
    assert C.divergent_answers(
        [{1: 0.9, 2: 0.1}, {1: 0.1, 2: 0.9}], leading=[1, 2]
    )
    assert not C.divergent_answers(
        [{1: 0.9, 2: 0.1}, {1: 0.8, 2: 0.2}], leading=[1, 2]
    ), "agreeing about the order but not the amount is not a divergence"


def test_a_surfaced_split_reserves_the_third_slot_for_the_opposite_pole():
    """Every high-scoring title is on the left pole, so zeroing alone gives three left-pole films."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [1.3, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert slate.contested == "mood"

    poles = [C.axis_position(DNA[t], AXES["mood"]) for t in slate.finalists]
    assert any(p < 0 for p in poles) and any(p > 0 for p in poles), (
        "the slate must actually contain one of each, not merely say so"
    )


def test_the_reserved_slot_goes_to_the_best_title_on_the_opposite_pole():
    """Built so the reservation must CHOOSE between 2 (0.90) and 3 (0.85); `opposite[-1]` picks 3."""
    per = {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.40, 5: 0.20}
    slate = C.combine(
        per_participant=scores(p1=per, p2=per),
        member_ledger={1: [1.3, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert slate.contested == "mood", "the split has to be surfaced for the reservation to run"

    reserved = [t for t in slate.finalists if t in (2, 3)]
    assert reserved == [2], (
        f"the reservation took {reserved}, and 2 outscores 3 on the same pole"
    )


def test_the_reserved_slot_replaces_the_third_rather_than_being_appended():
    """Four finalists is a different promise, and appending is the shorter implementation."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [1.3, 0.1], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5], 5: [0.5, 0.5]},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    )
    assert len(slate.finalists) == 3
    assert 3 not in slate.finalists, "the third-ranked left-pole title is the one displaced"


def test_the_contested_axis_stops_explaining_the_ranking():
    """The axis's INFLUENCE is removed. Subtracting the [-1, 1] position from a §5.1-scale score
    multiplied its influence with the sign flipped."""
    # A pool the axis explains completely: score rises with the mood position.
    base = {1: 0.10, 3: 0.15, 2: 0.20, 5: 0.60, 4: 0.70}
    poles = {t: C.axis_position(DNA[t], AXES["mood"]) for t in base}
    out = C.zeroed(base, facet="mood", dna=DNA, axes=AXES)

    def covariance(y):
        mx = sum(poles.values()) / len(poles)
        my = sum(y.values()) / len(y)
        return sum((poles[t] - mx) * (y[t] - my) for t in y)

    assert covariance(base) > 0.0, "the fixture is only meaningful while the axis explains it"
    assert covariance(out) == pytest.approx(0.0, abs=1e-9), (
        "after zeroing, the axis explains none of the ranking"
    )
    assert sum(out.values()) == pytest.approx(sum(base.values())), (
        "removing an influence is not moving the whole pool"
    )


def test_a_pool_the_axis_does_not_explain_is_left_alone():
    """A title off the axis must not move because two others disagree about mood."""
    flat = {6: 0.5, 1: 0.4}
    assert C.zeroed(flat, facet="pacing", dna=DNA, axes=AXES) == pytest.approx(flat)


def test_a_pool_with_nothing_on_the_other_pole_does_not_promise_one():
    """A split whose alternative does not exist must never ship bare."""
    one_sided = {t: {"dread": 1.0} for t in (1, 2, 3, 4, 5)}
    slate = C.combine(
        per_participant=scores(p1={t: 1.0 - 0.05 * t for t in (1, 2, 3, 4, 5)},
                               p2={t: 1.0 - 0.05 * t for t in (1, 2, 3, 4, 5)}),
        member_ledger={1: [1.3, 0.1], **{t: [0.5, 0.5] for t in (2, 3, 4, 5)}},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=one_sided, axes=AXES,
    )
    assert slate.contested is None
    assert slate.conflict is None
    assert len(slate.finalists) == 3


def test_the_contested_facet_needs_two_people_pulling_opposite_ways():
    assert C.contested_facet([{"dread": 1.0}, {"cosy": 1.0}], AXES) == "mood"
    assert C.contested_facet([{"dread": 1.0}, {"dread": 0.5}], AXES) is None, (
        "leaning the same way by different amounts is agreement"
    )
    assert C.contested_facet([{"dread": 1.0}], AXES) is None


def test_the_sanctioned_line_says_only_what_d_supports():
    line = copy_rules.D_LINE.format(d=0.24)
    assert "below your usual" in line


def test_the_headline_is_the_specs_own_sentence_and_never_a_models():
    """The headline and the explanation are the spec's verbatim."""
    block = C.combine(
        per_participant=scores(p1={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20},
                               p2={1: 0.90, 2: 0.85, 3: 0.80, 4: 0.30, 5: 0.20}),
        member_ledger={1: [1.3, 0.1], **{t: [0.5, 0.5] for t in (2, 3, 4, 5)}},
        tilts=[{"dread": 1.0}, {"cosy": 1.0}], dna=DNA, axes=AXES,
    ).conflict

    assert block["headline"] == (
        "You're split on mood — here's one of each. The axis is zeroed, not averaged."
    )
    assert block["explanation"] == copy_rules.D_LINE.format(d=block["d"])


def test_the_wildcard_carries_its_honest_label():
    """The route serves the label (tested there); here only the words' two properties."""
    assert C.WILDCARD_LABEL, "an unlabelled wildcard is just a worse recommendation"
    # It names the cost to the person, in their words, and does not hedge.
    assert "usual" in C.WILDCARD_LABEL
    assert not any(w in C.WILDCARD_LABEL.lower() for w in ("explor", "epsilon", "random"))


# Decision 173 ships no `dna_axis_weight` rows, so these hand-seeded axes test the RULE; no
# real evening reaches this branch yet.

# A title's position is exactly the sign of its term.
PACE = {"pace": {"slow": -1.0, "fast": 1.0}}
# Two people pulling opposite ways on it, which is all `contested_facet` asks for.
PULLING_APART = [{"slow": 1.0}, {"fast": 1.0}]


def _split(dna, per, *, top):
    """D carries the split (mean minus min of [1.3, 0.1] = 0.60 > 0.40), so only the DNA varies."""
    return C.combine(
        per_participant={10: per, 20: per},
        member_ledger={top: [1.3, 0.1], **{t: [0.5, 0.5] for t in per if t != top}},
        tilts=PULLING_APART, dna=dna, axes=PACE,
    )


def test_a_neutral_leader_still_gets_the_counterweight_the_split_promises():
    """40.6% of the corpus has no DNA, so the leader often sits off the axis; the reservation must not
    key off its pole."""
    dna = {1: {}, 2: {"slow": 1.0}, 3: {"slow": 0.9}, 4: {"fast": 1.0}, 5: {"fast": 0.8}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.40, 5: 0.30}, top=1)

    assert C.axis_position(dna[1], PACE["pace"]) == 0.0, "the fixture's leader is off the axis"
    assert slate.contested == "pace", "the pool has both poles, so the split is surfaceable"
    assert slate.conflict is not None, "§0: a surfaced split must never ship bare"
    poles = [C.axis_position(dna[t], PACE["pace"]) for t in slate.finalists]
    assert any(p < 0 for p in poles) and any(p > 0 for p in poles), (
        "the slate must hold one of each, not merely say so"
    )
    assert slate.reserved == 2, "the highest-scoring title on the far side, not merely one of them"


def test_a_pool_with_no_counterweight_ships_the_ranking_its_scores_show():
    """The else branch must ship the ranking the scores show, not the unshown zeroed one."""
    dna = {
        1: {"slow": 1.0}, 2: {"slow": 1.0, "fast": 0.5}, 3: {"slow": 1.0, "fast": 0.8},
        4: {"slow": 1.0, "fast": 0.2}, 5: {"slow": 1.0, "fast": 0.9},
    }
    per = {1: 0.90, 2: 0.70, 3: 0.55, 4: 0.80, 5: 0.50}
    assert all(C.axis_position(v, PACE["pace"]) < 0 for v in dna.values()), (
        "the fixture is only meaningful while the far pole is genuinely empty"
    )
    adjusted = C.ranked(
        C.zeroed(C.group_scores({10: per, 20: per}), facet="pace", dna=dna, axes=PACE)
    )
    assert [t for t, _ in adjusted[:3]] != [1, 4, 2], "the two rankings have to disagree"

    slate = _split(dna, per, top=1)
    assert slate.contested is None and slate.conflict is None
    assert slate.finalists == [1, 4, 2], "the top three by the group score the reveal displays"
    assert slate.reserved is None


def test_a_split_over_two_candidates_returns_a_slate_rather_than_raising():
    """On a pool of two the free slots consume both; `next()` without a default was a 500."""
    dna = {1: {"slow": 1.0}, 2: {"fast": 1.0}}
    slate = C.combine(
        per_participant={10: {1: 0.9, 2: 0.1}, 20: {1: 0.1, 2: 0.9}},
        member_ledger={1: [0.5, 0.5], 2: [0.5, 0.5]},
        tilts=PULLING_APART, dna=dna, axes=PACE,
    )
    assert slate.contested == "pace", "the two answers were opposite, so the split is real"
    assert slate.finalists == [1, 2], "both candidates, spanning the axis, and no third to find"
    assert slate.wildcard is None, "nothing is left outside the finalists to explore towards"
    assert [r["rank"] for r in slate.rows] == [1, 2]


def test_neither_free_finalist_on_the_reference_pole_reserves_slot_two_as_well():
    """Two neutral free finalists: slot 2 takes the best on the reference pole and slot 3 the best on
    the opposite one, still three (decision 221)."""
    dna = {1: {}, 2: {}, 3: {"slow": 1.0}, 4: {"fast": 1.0}, 5: {"fast": 0.5}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.60, 4: 0.50, 5: 0.40}, top=1)

    assert slate.contested == "pace"
    assert len(slate.finalists) == C.FINALISTS, "still three; no fourth is appended"
    poles = [C.axis_position(dna[t], PACE["pace"]) for t in slate.finalists]
    assert sorted((p > 0) - (p < 0) for p in poles) == [-1, 0, 1], (
        f"one of each beside the neutral leader, not {slate.finalists} at poles {poles}"
    )
    assert slate.finalists[0] == 1, "the zeroed leader keeps slot 1"
    assert slate.finalists[1] == 4, "slot 2: the best title on the reference pole"
    assert slate.finalists[2] == 3, "slot 3: the best title on the opposite pole"
    assert slate.reserved == 3, "the counterweight is the far-pole card, and only that one"


def test_the_wildcard_comes_from_the_ranking_the_finalists_came_from():
    """8 and 9 mirror the finalists' DNA centre, so distance ties; the wildcard must come from the
    slate's own (zeroed) ranking."""
    dna = {1: {"slow": 1.0}, 2: {}, 3: {"fast": 1.0}, 8: {"slow": 0.6}, 9: {"fast": 0.6}}
    per = {3: 0.95, 2: 0.85, 1: 0.60, 9: 0.52, 8: 0.50}
    slate = _split(dna, per, top=3)
    assert slate.contested == "pace" and slate.finalists == [3, 2, 1]

    centre = {}
    for t in slate.finalists:
        for term, value in dna[t].items():
            centre[term] = centre.get(term, 0.0) + value / len(slate.finalists)

    def distance(title_id):
        vec = dna[title_id]
        return sum((vec.get(x, 0.0) - centre.get(x, 0.0)) ** 2 for x in set(vec) | set(centre))

    assert distance(8) == distance(9), "the fixture is only decidable while the distances tie"
    assert C.ranked(C.group_scores({10: per, 20: per}))[3][0] == 9, "the group score prefers 9"
    assert slate.wildcard == 8, (
        "the wildcard was drawn from the unzeroed ranking the finalists did not come from"
    )


def test_the_persisted_ranks_read_in_slate_order_on_a_surfaced_split():
    """The reservation reaches down the ranking, so ranks are re-stamped as a permutation of 1..n."""
    dna = {t: {"slow": 1.0} for t in (1, 2, 3, 4)} | {5: {"fast": 1.0}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80, 5: 0.30}, top=1)

    assert slate.contested == "pace" and slate.finalists == [1, 2, 5]
    assert [t for t, _ in slate.ranked][:3] == [1, 2, 3], (
        "the fixture is only meaningful while the reservation reaches past the group's top three"
    )
    by_rank = {r["rank"]: r for r in slate.rows}
    assert [r["rank"] for r in slate.rows] == sorted(by_rank), "the rows come out in rank order"
    assert sorted(by_rank) == list(range(1, len(slate.rows) + 1)), "a permutation of 1..n"
    assert [by_rank[i]["title_id"] for i in (1, 2, 3)] == slate.finalists
    last_finalist = max(r["rank"] for r in slate.rows if r["slot"] == C.SLOT_FINALIST)
    wildcard_rank = next(r["rank"] for r in slate.rows if r["slot"] == C.SLOT_WILDCARD)
    assert last_finalist < wildcard_rank, "the wildcard reads after all three finalists"
    assert by_rank[3]["group_score"] == pytest.approx(0.30), (
        "the score on the row stays the plain average; only the reading order moved"
    )


def test_exactly_the_reserved_finalist_is_labelled_as_such():
    """Only the counterweight is labelled: slot 2 may be placed by construction too (decision 221)."""
    dna = {t: {"slow": 1.0} for t in (1, 2, 3, 4)} | {5: {"fast": 1.0}}
    slate = _split(dna, {1: 0.95, 2: 0.90, 3: 0.85, 4: 0.80, 5: 0.30}, top=1)
    assert slate.reserved == 5
    assert [r["title_id"] for r in slate.rows if r["reserved"]] == [5]

    per = {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6}
    quiet = C.combine(
        per_participant={10: per, 20: per},
        member_ledger={t: [0.5, 0.5] for t in per},
        dna=dna, axes=PACE,
    )
    assert quiet.contested is None and quiet.reserved is None
    assert not any(r["reserved"] for r in quiet.rows), (
        "no reservation happened, so no card claims to be the far side of anything"
    )


# Decision 479: with no axis artifact (decision 173), a split is surfaced by person.

from spielplan.tonight import pool as pool_rules  # noqa: E402

# Nine titles; the members' tops are disjoint, so the plain top three is Patrick's alone.
PATRICK = pool_rules.rank_normal({1: .9, 2: .8, 3: .7, 4: .6, 5: .5, 6: .4, 9: .3, 8: .2, 7: .1})
JENNY = pool_rules.rank_normal({7: .9, 8: .8, 9: .7, 1: .6, 2: .5, 3: .4, 4: .3, 5: .2, 6: .1})


def _household(patrick=PATRICK, jenny=JENNY, *, per=None, **kw):
    return C.combine(
        per_participant=per or {10: patrick, 20: jenny},
        member_ledger={t: [patrick[t], jenny[t]] for t in patrick},
        **kw,
    )


def test_an_axisless_split_is_surfaced_by_person_not_silenced():
    """D ≥ the threshold with no axis loaded: the slate carries one of each seat's own top three,
    the missing one by a reserved slot, and the reveal says so in the fixed sentence."""
    slate = _household()
    assert slate.d >= C.D_THRESHOLD
    assert slate.finalists == [1, 2, 7], "the leader, the next by group score, and Jenny's pick"
    assert slate.reserved_for == {7: 20}, "reserved FOR the seat none of whose top three made it"
    assert slate.conflict is not None, "surfaced, never silently averaged"
    assert slate.conflict["headline"] == copy_rules.PERSON_SPLIT_LINE
    assert slate.conflict["by"] == "person" and slate.conflict["facet"] is None
    assert slate.conflict["explanation"] == copy_rules.D_LINE.format(d=slate.conflict["d"])
    row = next(r for r in slate.rows if r["title_id"] == 7)
    assert (row["slot"], row["reserved_for"], row["rank"]) == (C.SLOT_FINALIST, 20, 3), (
        "the pick is the third finalist and is read last, as 54d's third slot is"
    )


def test_a_person_split_keeps_three_finalists_and_never_claims_the_axis_counterweight():
    """`reserved` is the axis counterweight; a seat's pick is `reserved_for`. Still exactly three."""
    slate = _household()
    assert len(slate.finalists) == C.FINALISTS
    assert slate.reserved is None
    assert not any(r["reserved"] for r in slate.rows)
    assert sum(r["reserved_for"] is not None for r in slate.rows) == 1


def test_below_the_threshold_an_axisless_evening_stays_silent():
    """Agreeing Ledgers carry no conflict, however disjoint tonight's scores."""
    slate = C.combine(
        per_participant={10: PATRICK, 20: JENNY},
        member_ledger={t: [PATRICK[t], PATRICK[t]] for t in PATRICK},
    )
    assert slate.d == pytest.approx(0.0)
    assert slate.conflict is None and slate.reserved_for == {}
    assert slate.finalists == [1, 2, 3], "the plain top three by group score"


def test_divergent_answers_alone_never_surface_an_axisless_split():
    """Decision 217 measured `divergent_answers` firing on 84-97% of evenings, so D alone decides."""
    ledger = {t: [0.5, 0.5] for t in range(1, 6)}
    slate = C.combine(
        per_participant={10: {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.1, 5: 0.0},
                         20: {1: 0.7, 2: 0.8, 3: 0.9, 4: 0.1, 5: 0.0}},
        member_ledger=ledger,
    )
    assert C.divergent_answers(
        [{1: 0.9, 2: 0.8, 3: 0.7}, {1: 0.7, 2: 0.8, 3: 0.9}], [1, 2, 3]
    ), "the fixture does diverge on the leading candidates"
    assert slate.conflict is None and slate.reserved_for == {}


def test_a_seat_already_holding_one_of_its_own_needs_no_reservation():
    """The split is surfaced, and the slate is already true: Jenny's own #3 is the group's #2, so
    no slot is reserved and nothing is labelled as anybody's pick."""
    jenny = pool_rules.rank_normal({7: .9, 8: .8, 2: .7, 1: .6, 9: .5, 3: .4, 4: .3, 5: .2, 6: .1})
    slate = _household(jenny=jenny)
    assert slate.d >= C.D_THRESHOLD and slate.conflict is not None
    assert slate.reserved_for == {}
    assert slate.conflict["headline"] == copy_rules.PERSON_SPLIT_LINE


def test_a_room_the_slots_cannot_serve_gets_the_headline_without_the_promise():
    """Four seats outnumber the two slots after the leader, so "one for each of you" would be false."""
    n = 12
    tops = {10: [1, 2, 3], 20: [4, 5, 6], 30: [7, 8, 9], 40: [10, 11, 12]}
    per = {}
    for seat, mine in tops.items():
        # Each seat's own three on top, and the rest in an order of its own below them.
        raw = {t: (1.0 if t in mine else 0.0) - 0.01 * ((t * seat) % 13) for t in range(1, n + 1)}
        per[seat] = pool_rules.rank_normal(raw)
    slate = C.combine(
        per_participant=per,
        member_ledger={t: [per[s][t] for s in per] for t in range(1, n + 1)},
    )
    assert slate.d >= C.D_THRESHOLD
    assert len(slate.finalists) == C.FINALISTS
    served = [s for s, mine in tops.items() if set(mine) & set(slate.finalists)]
    assert len(served) < len(tops), "the fixture really does outnumber the slots"
    assert slate.conflict["headline"] == copy_rules.PERSON_SPLIT_SHORT


def test_the_wildcard_is_drawn_from_near_the_top_of_the_ranking_not_its_tail():
    """Bounded to the best twentieth (at least twelve), distance decides only among favoured titles."""
    n = 40
    per = {t: 1.0 - t / n for t in range(1, n + 1)}
    dna = {t: {"slow": 1.0} for t in range(1, n + 1)}
    dna[6] = {"fast": 1.0}             # a step outside, near the top
    dna[n] = {"fast": 9.0}             # the farthest thing in the pool, at the very bottom
    slate = C.combine(
        per_participant={10: per, 20: per},
        member_ledger={t: [0.5, 0.5] for t in per},
        dna=dna,
    )
    assert slate.wildcard == 6, f"drawn from rank {slate.wildcard}, not from the top of the ranking"
    reach = max(C.WILDCARD_FLOOR, math.ceil(n * C.WILDCARD_SHARE))
    assert [t for t, _ in slate.ranked].index(slate.wildcard) < reach


def test_a_member_reads_the_plain_sentence_and_never_the_number():
    """Decision 486: D and "the axis is zeroed" are model vocabulary; `for_member` strips them."""
    person = copy_rules.for_member(copy_rules.person_conflict(d=0.61, one_for_each=True))
    assert "d" not in person
    assert person["explanation"] == copy_rules.D_LINE_PLAIN
    assert "0.61" not in repr(person)

    axis = copy_rules.for_member(copy_rules.conflict("pace", d=0.61))
    assert axis["headline"] == "You're split on pace — here's one of each."
    assert "zeroed" not in repr(axis) and "d" not in axis
    assert copy_rules.for_member(None) is None


def test_the_pull_lines_say_what_their_branch_establishes_in_plain_words():
    """Each pull branch claims only what it establishes, in plain words."""
    leaned = copy_rules.leaned("Patrick", ["pulp", "escapist"])
    assert leaned == "Patrick leaned toward pulp and escapist tonight"
    usual = copy_rules.usual("Jenny", ["escapist", "charismatic lead"])
    assert usual == "suits Jenny's usual taste — escapist and charismatic lead"
    assert copy_rules.leaned("Mia", ["dark"]) == "Mia leaned toward dark tonight"
    assert copy_rules.usual("Mia", ["a", "b", "c"]).endswith("a, b and c")
    for line in (leaned, usual):
        assert "+" not in line and "pulls" not in line
