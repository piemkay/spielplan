"""§6.3's board and its three badges. Spec v2.1 §6.3, §5.2, §4.3.

No database. The board is a pure function of the fit's output — `s`, `sigma`, the learned
cutpoints, the tier set, and whatever the person last dragged — and every rule §6.3 states
about it is a rule about that function. Pushing these through Postgres would test the same
arithmetic through a socket.

Three of §6.3's sentences are load-bearing here and each has a way of going quietly wrong:

  * "learned cutpoints, **not percentile cuts**". A percentile implementation reproduces the
    measured shape on every board, which looks *more* right than the truth on a lopsided one.
    The two are told apart by their invariances, not by their output on a healthy board.
  * "a straddling title shows 'A/S' **and becomes queue-eligible**" — one predicate doing two
    jobs. The prototype badged at sigma > .13 and queued at sigma > .09, so a title at .11 was
    queue-eligible and unbadged (proposal 157).
  * "if the model disagrees strongly, the title's badge shows the tension **rather than
    snapping back**". Snapping back is the failure; a board that quietly re-sorts a dropped
    title is the exact thing the clause forbids.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import MEASURED_TIER_SHARES
from spielplan.rank import board

TIER_SET = ("F", "D", "C", "B", "A", "A+", "S")


def items(values, *, sigma=0.01, assigned=None, names=None):
    """A board from bare `s` values. σ is tiny by default so nothing straddles unless a test
    asks for it — a straddle badge appearing in a test about neighbourhoods is noise."""
    assigned = assigned or {}
    return [
        board.Item(
            title_id=i + 1,
            name=(names or {}).get(i + 1, f"T{i + 1}"),
            s=float(v),
            sigma=float(sigma[i]) if isinstance(sigma, (list, tuple, np.ndarray)) else float(sigma),
            assigned_tier=assigned.get(i + 1),
        )
        for i, v in enumerate(values)
    ]


def shares(tiers) -> list[float]:
    total = sum(len(t.entries) for t in tiers)
    return [len(t.entries) / total for t in tiers]


def by_id(tiers) -> dict[int, board.Entry]:
    return {e.title_id: e for t in tiers for e in t.entries}


# --- §6.3: learned cutpoints, not percentile cuts ---------------------------------------------


def test_an_unrated_tier_set_starts_at_the_measured_quantile_shape():
    """§6.3: "initialised from DNA_MODEL §4.5's measured quantile shape F 3 / D 7 / C 15 /
    B 25 / A 25 / A+ 17 / S 8 %, then learned".

    The literal percentages, written out. Comparing `initial_cutpoints` against
    `MEASURED_TIER_SHARES` proves only that logit and sigmoid round-trip — the M3 review
    mutation-proved it by swapping S and A+ in the constant and watching all 71 pure tests stay
    green. The authored shape is a measurement from another project, so the number has to
    appear on this side of the assertion too.
    """
    authored = (0.03, 0.07, 0.15, 0.25, 0.25, 0.17, 0.08)
    assert authored == MEASURED_TIER_SHARES, "§6.3's shape, F first"
    assert sum(authored) == pytest.approx(1.0)

    cuts = model.initial_cutpoints(7)
    implied = np.diff(np.concatenate([[0.0], 1.0 / (1.0 + np.exp(-cuts)), [1.0]]))
    assert np.allclose(implied, authored, atol=1e-9)
    # …and the cutpoints themselves, which is what every downstream tier actually reads.
    assert np.allclose(
        cuts, [-3.4761, -2.1972, -1.0986, 0.0, 1.0986, 2.4423], atol=1e-4
    )


def test_the_tier_a_title_shows_comes_from_the_cutpoints_it_was_given():
    """The distinguishing invariance. Percentile cuts are computed *from* the population, so
    shifting the boundaries cannot move anybody; learned cutpoints live on the same scale as
    `s`, so shifting them past the whole population empties every tier but one.

    A percentile implementation passes every "the shape looks right" assertion and fails this
    one, which is why this is the test and not the shape."""
    values = np.linspace(-1.0, 1.0, 40)
    cuts = model.initial_cutpoints(7)

    here = board.build(items(values), cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS)
    assert sum(len(t.entries) for t in here if t.entries) > 1, "the board spans several tiers"

    # Same population, boundaries moved above everything. Percentiles would not notice.
    shifted = board.build(items(values), cuts=cuts + 50.0, tier_set=TIER_SET, hp=DEFAULTS)
    assert all(e.tier == 0 for e in by_id(shifted).values())


def test_a_lopsided_board_keeps_its_learned_boundaries():
    """§6.3's shape is an initialisation and a prior mean, not a rendering rule. A person who
    has dragged everything to the two ends has a board that *is* lopsided, and a renderer that
    re-derives the measured shares every time would hide exactly that."""
    values = np.concatenate([np.full(18, -3.0), np.full(18, 3.0)])
    tiers = board.build(items(values), cuts=model.initial_cutpoints(7), tier_set=TIER_SET,
                        hp=DEFAULTS)

    occupied = [len(t.entries) for t in tiers if t.entries]
    assert occupied == [18, 18], "the two piles stay two piles"
    assert not np.allclose(sorted(shares(tiers)), sorted(MEASURED_TIER_SHARES), atol=0.02), (
        "a board that always reproduces the measured shares is a percentile cut"
    )


def test_the_board_renders_best_first_and_keeps_empty_tiers():
    """§6.3 lists the tiers ascending (F … S); the board renders them best-first, and an empty
    tier stays on screen because it is still a drop target."""
    tiers = board.build(items([-3.0, 3.0]), cuts=model.initial_cutpoints(7),
                        tier_set=TIER_SET, hp=DEFAULTS)
    assert [t.label for t in tiers] == ["S", "A+", "A", "B", "C", "D", "F"]
    assert len(tiers) == len(TIER_SET)
    assert sum(1 for t in tiers if not t.entries) == len(TIER_SET) - 2


def test_within_a_tier_the_board_is_ordered_by_the_ledger():
    """Proposal 78's point restated as a rule the neighbourhood badge depends on: a
    neighbourhood claim over an ordering nobody renders is unverifiable."""
    tiers = board.build(items([0.10, 0.30, 0.20]), cuts=np.array([-9.0, 9.0]),
                        tier_set=("low", "mid", "high"), hp=DEFAULTS)
    mid = next(t for t in tiers if t.label == "mid")
    assert [e.title_id for e in mid.entries] == [2, 3, 1]


# --- §6.3: "Badge shows tier + neighbourhood" -------------------------------------------------


def test_the_badge_names_the_two_neighbours_inside_its_own_tier():
    """§6.3's own example: "A — between Heat and Prisoners"."""
    names = {1: "Heat", 2: "Drive", 3: "Prisoners"}
    tiers = board.build(
        items([0.30, 0.20, 0.10], names=names),
        cuts=np.array([-9.0, 9.0]), tier_set=("F", "A", "S"), hp=DEFAULTS,
    )
    drive = by_id(tiers)[2]
    assert drive.above == "Heat" and drive.below == "Prisoners"
    assert drive.badge == "A — between Heat and Prisoners"


def test_the_ends_of_a_tier_name_only_the_neighbour_that_exists():
    names = {1: "Heat", 2: "Drive", 3: "Prisoners"}
    tiers = board.build(
        items([0.30, 0.20, 0.10], names=names),
        cuts=np.array([-9.0, 9.0]), tier_set=("F", "A", "S"), hp=DEFAULTS,
    )
    top, bottom = by_id(tiers)[1], by_id(tiers)[3]
    assert top.above is None and top.below == "Drive"
    assert bottom.above == "Drive" and bottom.below is None
    assert top.badge == "A — just above Drive"
    assert bottom.badge == "A — just below Drive"


def test_a_title_alone_in_its_tier_claims_no_neighbours():
    tiers = board.build(items([0.0]), cuts=np.array([-9.0, 9.0]), tier_set=("F", "A", "S"),
                        hp=DEFAULTS)
    only = by_id(tiers)[1]
    assert only.above is None and only.below is None
    assert only.badge == "A — the only one"


def test_the_badge_never_names_the_title_it_is_attached_to():
    """The off-by-one this guards is the same family as proposal 76's "S/S": an index clamp
    that returns the title's own position at the end of a list."""
    values = np.linspace(-1.0, 1.0, 30)
    tiers = board.build(items(values), cuts=model.initial_cutpoints(7), tier_set=TIER_SET,
                        hp=DEFAULTS)
    for entry in by_id(tiers).values():
        assert entry.above != entry.name
        assert entry.below != entry.name


def test_a_neighbour_is_never_borrowed_from_another_tier():
    """The badge already names the tier, so a neighbour from a different one contradicts the
    letter beside it."""
    tiers = board.build(items([1.0, -1.0]), cuts=np.array([0.0]), tier_set=("F", "S"),
                        hp=DEFAULTS)
    for entry in by_id(tiers).values():
        assert entry.above is None and entry.below is None


# --- §6.3: the straddle badge IS queue eligibility --------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_the_badged_set_and_the_queue_pool_are_the_same_set(seed):
    """§6.3: "a straddling title shows \"A/S\" and becomes queue-eligible" — one predicate,
    two jobs. The prototype had two thresholds and therefore two sets (proposal 157)."""
    from spielplan.rank import queue

    rng = np.random.default_rng(seed)
    n = 60
    values = rng.normal(size=n)
    sigmas = rng.uniform(0.01, 0.6, size=n)
    cuts = model.initial_cutpoints(7)
    pool = items(values, sigma=sigmas)

    badged = {e.title_id for e in by_id(
        board.build(pool, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS)
    ).values() if e.straddle is not None}
    eligible = {i.title_id for i in queue.eligible(pool, cuts=cuts, hp=DEFAULTS)}

    assert badged == eligible
    assert badged, "the fixture has to actually produce straddlers or this proves nothing"


def test_moving_the_straddle_threshold_moves_both_sets_together():
    """§4.3 / proposal 157: the threshold is a bundle constant, not a literal in a renderer.
    If either side hard-coded it, the two sets would come apart here."""
    from spielplan.rank import queue

    rng = np.random.default_rng(11)
    pool = items(rng.normal(size=80), sigma=rng.uniform(0.01, 0.5, size=80))
    cuts = model.initial_cutpoints(7)

    seen = []
    for z in (0.25, 1.0, 3.0):
        hp = dataclasses.replace(DEFAULTS, straddle_z=z)
        badged = {e.title_id for e in by_id(
            board.build(pool, cuts=cuts, tier_set=TIER_SET, hp=hp)
        ).values() if e.straddle is not None}
        eligible = {i.title_id for i in queue.eligible(pool, cuts=cuts, hp=hp)}
        assert badged == eligible, f"the two sets disagree at straddle_z={z}"
        seen.append(badged)

    assert seen[0] < seen[1] < seen[2], "a wider threshold has to admit strictly more titles"


def test_the_top_and_bottom_tiers_never_straddle_into_themselves():
    """Proposal 76: the prototype rendered "S/S" because it clamped an index instead of asking
    which tier the posterior reached. At the ends there is only one direction to reach in, so
    the badge names the tier on that side — "S/A+", "F/D" — and never the title's own."""
    cuts = model.initial_cutpoints(7)
    # Just inside the top and bottom tiers, with a σ that reaches exactly one tier over.
    tiers = board.build(
        items([float(cuts[-1]) + 0.15, float(cuts[0]) - 0.15], sigma=0.4),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    )
    top, bottom = by_id(tiers)[1], by_id(tiers)[2]
    assert top.tier == len(TIER_SET) - 1 and bottom.tier == 0
    assert top.straddle_badge == "S/A+"
    assert bottom.straddle_badge == "F/D"


def test_a_straddle_badge_never_repeats_the_titles_own_tier():
    """The invariant behind proposal 76, asserted where the clamp bug would live: a posterior
    so wide it spans the whole scale still has to name a *different* tier or none at all.

    §6.3's own example is "A/S", two levels apart, so the badge names the tier the posterior
    actually reaches rather than the neighbouring one — the rule is that it is never the same
    tier, not that it is the next one."""
    cuts = model.initial_cutpoints(7)
    rng = np.random.default_rng(7)
    pool = items(rng.normal(scale=2.0, size=120), sigma=rng.uniform(0.01, 4.0, size=120))
    # Assigned tiers too: the "S/S" family the test is named for is an index taken from the
    # wrong place, and with every tier model-derived there is no wrong place to take it from.
    rng2 = np.random.default_rng(8)
    assigned = {i + 1: int(rng2.integers(0, len(TIER_SET))) for i in range(120)}
    for board_pool in (pool, items(rng.normal(scale=2.0, size=120), sigma=1.2, assigned=assigned)):
        entries = by_id(board.build(board_pool, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS))
        for entry in entries.values():
            assert entry.straddle != entry.model_tier, "never the tier it was computed against"
            if entry.straddle_badge is not None:
                head, _, tail = entry.straddle_badge.partition("/")
                assert head != tail, f"a badge naming one tier twice: {entry.straddle_badge}"
                assert head == TIER_SET[entry.tier], "the badge leads with the rendered tier"


# --- §6.3: tension, not snapping back ----------------------------------------------------------


def test_a_tier_outside_the_eighty_percent_interval_is_tension():
    """Proposal 71's operational reading of §6.3's "disagrees strongly": the tier the person
    assigned and the posterior's 80% credible interval are disjoint."""
    tiers = board.build(
        items([0.0], sigma=0.01, assigned={1: 6}),
        cuts=model.initial_cutpoints(7), tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.tension is not None
    assert entry.tier == 6, "and it stays where it was put"


def test_a_one_level_disagreement_inside_the_interval_is_not_tension():
    """§6.3's rule is "disagrees *strongly*". A neighbouring tier the posterior still reaches
    is a difference, not a disagreement — badging it would badge most of the board."""
    cuts = model.initial_cutpoints(7)
    # Sit just below a boundary with a σ wide enough to cross it, and assign the tier above.
    s = float(cuts[3]) - 0.05
    below = model.tier_of(np.array([s]), cuts)[0]
    tiers = board.build(
        items([s], sigma=0.5, assigned={1: int(below) + 1}),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.tension is None
    assert entry.tier == int(below) + 1, "no badge, and no move either"


def test_a_tension_badge_names_both_tiers():
    """§6.7's register: the rail exists so "drag-and-drop is data, not override" is legible,
    and a badge that says only "disagrees" is not."""
    # s = -1.5 sits inside C's band (the cutpoints are logits of the measured shares), so the
    # two tiers in the line are genuinely different and the copy is checked against real ones.
    tiers = board.build(
        items([-1.5], sigma=0.01, assigned={1: 6}, names={1: "Drive"}),
        cuts=model.initial_cutpoints(7), tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.model_tier == 2 and entry.tier == 6
    assert entry.tension == "you put it in S — the ledger still reads C"


def test_the_board_never_moves_a_title_out_of_the_tier_it_was_dropped_in():
    """§6.3: "shows the tension rather than **snapping back**". Across the whole range of
    disagreement — none, mild, extreme — the rendered tier is the assigned one."""
    cuts = model.initial_cutpoints(7)
    for assigned_tier in range(len(TIER_SET)):
        tiers = board.build(
            items([0.0], sigma=0.2, assigned={1: assigned_tier}),
            cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
        )
        entry = by_id(tiers)[1]
        assert entry.tier == assigned_tier
        assert entry.assigned_tier == assigned_tier
        assert entry.model_tier == model.tier_of(np.array([0.0]), cuts)[0]


def test_an_untouched_title_is_placed_by_the_model():
    """The other half of the same rule: with no `tier_edit` there is nothing to override, so
    the ledger decides and `assigned_tier` is absent rather than invented."""
    cuts = model.initial_cutpoints(7)
    entry = by_id(board.build(items([0.42]), cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS))[1]
    assert entry.assigned_tier is None
    assert entry.tier == entry.model_tier == model.tier_of(np.array([0.42]), cuts)[0]
    assert entry.tension is None


def test_the_tension_threshold_comes_from_the_bundle():
    """§4.3 owns the constant. A wider credible interval must make tension strictly rarer;
    a hard-coded 80% would not move at all."""
    cuts = model.initial_cutpoints(7)
    rng = np.random.default_rng(4)
    values = rng.normal(size=50)
    assigned = {i + 1: int(rng.integers(0, 7)) for i in range(50)}
    pool = items(values, sigma=0.25, assigned=assigned)

    counts = []
    for mass in (0.50, 0.80, 0.999):
        hp = dataclasses.replace(DEFAULTS, tension_credible_mass=mass)
        tiers = board.build(pool, cuts=cuts, tier_set=TIER_SET, hp=hp)
        counts.append(sum(1 for e in by_id(tiers).values() if e.tension is not None))
    assert counts[0] > counts[1] > counts[2]


# --- decision 11's leftovers: a tier edit that outlived its tier set ---------------------------


def test_a_tier_edit_above_the_new_tier_set_renders_instead_of_crashing():
    """Decision 11 keeps `tier_edit` rows across a change in K, so the board is guaranteed to
    meet a level that no longer exists — and it is the *only* consumer that indexes the
    cutpoint array directly.

    `ledger.observations.load_observations` already clamps this case and logs that it did, so
    the fit survives a shrink; the board did not, and `_band(6, cuts)` walked off a two-element
    array. The whole surface 500ed for that person until they re-dropped every affected title.
    Found by the M3 review, reproduced here first.
    """
    cuts = model.initial_cutpoints(3)
    tiers = board.build(
        items([0.1], sigma=0.3, assigned={1: 6}),
        cuts=cuts, tier_set=("bad", "ok", "good"), hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.tier == 2, "clamped to the top of the set they now have, as the fit clamps it"
    assert entry.assigned_tier == 2, (
        "the clamp is applied once, at the edge — a board whose bucket and whose badge "
        "disagreed about the assigned tier would be worse than the crash"
    )


@pytest.mark.parametrize("assigned", [-3, -1, 7, 40])
def test_no_out_of_range_assignment_can_take_the_board_down(assigned):
    """Every level outside 0..K-1, from both ends. `tier_edit.tier` is a `smallint` with no
    CHECK against the tier set (§4.2 makes it "an index into the user's configured tier set",
    and 0005 cannot express that), so the board has to survive anything the column can hold."""
    cuts = model.initial_cutpoints(7)
    tiers = board.build(
        items([0.0], sigma=0.4, assigned={1: assigned}),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert 0 <= entry.tier < len(TIER_SET)
    assert entry.assigned_tier is not None and 0 <= entry.assigned_tier < len(TIER_SET)
