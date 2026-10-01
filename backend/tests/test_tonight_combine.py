"""A split is surfaced by person: a seat's reserved slot replaces the third, never appends. The
conflict copy is bounded by a filter, not by a polite prompt."""

from __future__ import annotations

import math

import pytest

from spielplan.tonight import combine as C
from spielplan.tonight import copy as copy_rules
from spielplan.tonight import pool as pool_rules

DNA = {
    1: {"dread": 1.0},
    2: {"bleak": 1.0},
    3: {"dread": 0.9},
    4: {"cosy": 1.0},             # far from 1-3
    5: {"warm": 1.0},
    6: {},                        # no DNA at all
    7: {"dread": 0.95},           # all but a duplicate of title 1
}


def scores(**per_seat):
    """{participant_id: {title_id: tonight score}} from keyword seats p1=..., p2=..."""
    return {int(k[1:]): v for k, v in per_seat.items()}


def test_the_slate_is_exactly_three_finalists_and_one_wildcard():
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 4: 0.6, 5: 0.5}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 6)},
        dna=DNA,
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
        dna=DNA,
    )
    assert slate.finalists[0] == 4, "mean 0.545 beats title 1's 0.40"


def test_the_wildcard_is_a_step_outside_rather_than_the_fourth_best():
    """Title 7 is next in rank and near-duplicates the finalists; title 4 is far from them in DNA.
    A rank-drawn wildcard returns 7; an honest one returns 4."""
    slate = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1},
                               p2={1: 0.9, 2: 0.8, 3: 0.7, 7: 0.6, 4: 0.1}),
        member_ledger={t: [0.5, 0.5] for t in (1, 2, 3, 4, 7)},
        dna=DNA,
    )
    assert slate.finalists == [1, 2, 3]
    assert slate.wildcard == 4, "the furthest in DNA terms, not the next in rank (7)"


def test_every_candidate_lands_in_exactly_one_slot():
    """`session_result` stores one row per candidate, so the slots partition the pool."""
    slate = C.combine(
        per_participant=scores(p1={t: 1.0 - 0.1 * t for t in range(1, 7)},
                               p2={t: 1.0 - 0.1 * t for t in range(1, 7)}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 7)},
        dna=DNA,
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
        dna=DNA,
    )
    assert at.d == pytest.approx(C.D_THRESHOLD)
    assert at.conflict is not None, "D exactly at the threshold is a split"


def test_below_the_threshold_the_split_is_decided_silently():
    """No copy, no reserved slot, and still three."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={1: [1.199, 0.401], 2: [0.5, 0.5], 3: [0.5, 0.5], 4: [0.5, 0.5]},
        dna=DNA,
    )
    assert quiet.d == pytest.approx(C.D_THRESHOLD - 0.001)
    assert quiet.conflict is None
    assert quiet.reserved_for == {}
    assert len(quiet.finalists) == 3


def test_the_fire_rate_is_recoverable_from_the_slate():
    """§14 risk 6 needs the fire rate, so whether it fired is stored, not inferred from copy."""
    quiet = C.combine(
        per_participant=scores(p1={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3},
                               p2={1: 0.9, 2: 0.5, 3: 0.4, 4: 0.3}),
        member_ledger={t: [0.5, 0.5] for t in range(1, 5)},
        dna=DNA,
    )
    assert quiet.conflict is None and quiet.d == pytest.approx(0.0)


def test_the_sanctioned_line_says_only_what_d_supports():
    line = copy_rules.D_LINE.format(d=0.24)
    assert "below your usual" in line


def test_the_wildcard_carries_its_honest_label():
    """The route serves the label (tested there); here only the words' two properties."""
    assert C.WILDCARD_LABEL, "an unlabelled wildcard is just a worse recommendation"
    # It names the cost to the person, in their words, and does not hedge.
    assert "usual" in C.WILDCARD_LABEL
    assert not any(w in C.WILDCARD_LABEL.lower() for w in ("explor", "epsilon", "random"))


# Nine titles; the members' tops are disjoint, so the plain top three is Patrick's alone.
PATRICK = pool_rules.rank_normal({1: .9, 2: .8, 3: .7, 4: .6, 5: .5, 6: .4, 9: .3, 8: .2, 7: .1})
JENNY = pool_rules.rank_normal({7: .9, 8: .8, 9: .7, 1: .6, 2: .5, 3: .4, 4: .3, 5: .2, 6: .1})


def _household(patrick=PATRICK, jenny=JENNY, *, per=None, **kw):
    return C.combine(
        per_participant=per or {10: patrick, 20: jenny},
        member_ledger={t: [patrick[t], jenny[t]] for t in patrick},
        **kw,
    )


def test_a_split_is_surfaced_by_person_not_silenced():
    """D ≥ the threshold: the slate carries one of each seat's own top three, the missing one by a
    reserved slot, and the reveal says so in the fixed sentence."""
    slate = _household()
    assert slate.d >= C.D_THRESHOLD
    assert slate.finalists == [1, 2, 7], "the leader, the next by group score, and Jenny's pick"
    assert slate.reserved_for == {7: 20}, "reserved FOR the seat none of whose top three made it"
    assert slate.conflict is not None, "surfaced, never silently averaged"
    assert slate.conflict["headline"] == copy_rules.PERSON_SPLIT_LINE
    assert slate.conflict["explanation"] == copy_rules.D_LINE.format(d=slate.conflict["d"])
    row = next(r for r in slate.rows if r["title_id"] == 7)
    assert (row["slot"], row["reserved_for"], row["rank"]) == (C.SLOT_FINALIST, 20, 3), (
        "the pick is the third finalist and is read last, as 54d's third slot is"
    )


def test_a_person_split_keeps_three_finalists():
    slate = _household()
    assert len(slate.finalists) == C.FINALISTS
    assert sum(r["reserved_for"] is not None for r in slate.rows) == 1


def test_the_persisted_ranks_read_in_slate_order_on_a_surfaced_split():
    """The reservation reaches down the ranking, so ranks are re-stamped as a permutation of 1..n."""
    slate = _household()
    assert [t for t, _ in slate.ranked][:3] != slate.finalists, (
        "the fixture is only meaningful while the reservation reaches past the group's top three"
    )
    by_rank = {r["rank"]: r for r in slate.rows}
    assert [r["rank"] for r in slate.rows] == sorted(by_rank) == list(range(1, len(slate.rows) + 1))
    assert [by_rank[i]["title_id"] for i in (1, 2, 3)] == slate.finalists
    wildcard_rank = next(r["rank"] for r in slate.rows if r["slot"] == C.SLOT_WILDCARD)
    assert wildcard_rank == 4, "the wildcard reads after all three finalists"
    assert by_rank[3]["group_score"] == pytest.approx(C.group_scores({10: PATRICK, 20: JENNY})[7]), (
        "the score on the row stays the plain average; only the reading order moved"
    )


def test_below_the_threshold_disjoint_tonight_scores_stay_silent():
    """Agreeing Ledgers carry no conflict, however disjoint tonight's scores."""
    slate = C.combine(
        per_participant={10: PATRICK, 20: JENNY},
        member_ledger={t: [PATRICK[t], PATRICK[t]] for t in PATRICK},
    )
    assert slate.d == pytest.approx(0.0)
    assert slate.conflict is None and slate.reserved_for == {}
    assert slate.finalists == [1, 2, 3], "the plain top three by group score"


def test_opposed_tonight_orders_alone_never_surface_a_split():
    """Decision 217 measured opposed answers firing on 84-97% of evenings, so D alone decides."""
    ledger = {t: [0.5, 0.5] for t in range(1, 6)}
    slate = C.combine(
        per_participant={10: {1: 0.9, 2: 0.8, 3: 0.7, 4: 0.1, 5: 0.0},
                         20: {1: 0.7, 2: 0.8, 3: 0.9, 4: 0.1, 5: 0.0}},
        member_ledger=ledger,
    )
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
    """Decision 486: D is model vocabulary; `for_member` strips it."""
    person = copy_rules.for_member(copy_rules.person_conflict(d=0.61, one_for_each=True))
    assert "d" not in person
    assert person["explanation"] == copy_rules.D_LINE_PLAIN
    assert "0.61" not in repr(person)
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
