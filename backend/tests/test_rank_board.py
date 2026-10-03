"""No database: the board is a pure function of the fit's output."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import MEASURED_TIER_SHARES, OUT_A, OUT_B, OUT_TIE, ObservationSet
from spielplan.rank import board

TIER_SET = ("E", "D", "C", "B", "A", "S")


def items(values, *, sigma=0.01, assigned=None, names=None):
    """σ is tiny by default so nothing straddles unless a test asks for it."""
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


def reach_sigma(reach: float) -> float:
    """Spelled as a reach, not a bare σ: `straddle_z` is tunable (§4.3), and decision 214 moved it."""
    return reach / DEFAULTS.straddle_z


def shares(tiers) -> list[float]:
    total = sum(len(t.entries) for t in tiers)
    return [len(t.entries) / total for t in tiers]


def by_id(tiers) -> dict[int, board.Entry]:
    return {e.title_id: e for t in tiers for e in t.entries}


def test_an_unrated_tier_set_starts_at_the_measured_quantile_shape():
    """The literal percentages: comparing against `MEASURED_TIER_SHARES` only proves that logit and
    sigmoid round-trip."""
    authored = (0.05, 0.15, 0.30, 0.25, 0.15, 0.10)
    assert authored == MEASURED_TIER_SHARES, "§6.3's shape, E first"
    assert sum(authored) == pytest.approx(1.0)

    cuts = model.initial_cutpoints(6)
    implied = np.diff(np.concatenate([[0.0], 1.0 / (1.0 + np.exp(-cuts)), [1.0]]))
    assert np.allclose(implied, authored, atol=1e-9)
    assert np.allclose(
        cuts, [-2.9444, -1.3863, 0.0, 1.0986, 2.1972], atol=1e-4
    )


def test_the_tier_a_title_shows_comes_from_the_cutpoints_it_was_given():
    """Percentile cuts cannot move when boundaries shift; learned ones empty every tier but one."""
    values = np.linspace(-1.0, 1.0, 40)
    cuts = model.initial_cutpoints(6)

    here = board.build(items(values), cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS)
    assert sum(len(t.entries) for t in here if t.entries) > 1, "the board spans several tiers"

    shifted = board.build(items(values), cuts=cuts + 50.0, tier_set=TIER_SET, hp=DEFAULTS)
    assert all(e.tier == 0 for e in by_id(shifted).values())


def test_a_lopsided_board_keeps_its_learned_boundaries():
    """The measured shape is an initialisation, not a rendering rule."""
    values = np.concatenate([np.full(18, -3.0), np.full(18, 3.0)])
    tiers = board.build(items(values), cuts=model.initial_cutpoints(6), tier_set=TIER_SET,
                        hp=DEFAULTS)

    occupied = [len(t.entries) for t in tiers if t.entries]
    assert occupied == [18, 18], "the two piles stay two piles"
    assert not np.allclose(sorted(shares(tiers)), sorted(MEASURED_TIER_SHARES), atol=0.02), (
        "a board that always reproduces the measured shares is a percentile cut"
    )


def test_the_board_renders_best_first_and_keeps_empty_tiers():
    """An empty tier stays on screen: it is still a drop target."""
    tiers = board.build(items([-3.0, 3.0]), cuts=model.initial_cutpoints(6),
                        tier_set=TIER_SET, hp=DEFAULTS)
    assert [t.label for t in tiers] == ["S", "A", "B", "C", "D", "E"]
    assert len(tiers) == len(TIER_SET)
    assert sum(1 for t in tiers if not t.entries) == len(TIER_SET) - 2


def test_each_section_head_names_its_tiers_word():
    """Decision 561's words on the default set; a custom set's labels are their own words."""
    tiers = board.build([], cuts=model.initial_cutpoints(6), tier_set=TIER_SET, hp=DEFAULTS)
    assert [t.word for t in tiers] == [
        "All-time favourite", "Excellent", "Very good", "Good", "OK", "Not for me",
    ]
    three = board.build([], cuts=np.array([-1.0, 1.0]), tier_set=("meh", "ok", "great"), hp=DEFAULTS)
    assert [t.word for t in three] == ["great", "ok", "meh"]


def test_a_row_carries_its_year():
    heat = board.Item(title_id=1, name="Heat", s=0.0, sigma=0.01, year=1995)
    tiers = board.build([heat], cuts=model.initial_cutpoints(6), tier_set=TIER_SET, hp=DEFAULTS)
    assert by_id(tiers)[1].public()["year"] == 1995


def test_within_a_tier_the_board_is_ordered_by_the_ledger():
    tiers = board.build(items([0.10, 0.30, 0.20]), cuts=np.array([-9.0, 9.0]),
                        tier_set=("low", "mid", "high"), hp=DEFAULTS)
    mid = next(t for t in tiers if t.label == "mid")
    assert [e.title_id for e in mid.entries] == [2, 3, 1]


def test_the_badge_names_the_two_neighbours_inside_its_own_tier():
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
    values = np.linspace(-1.0, 1.0, 30)
    tiers = board.build(items(values), cuts=model.initial_cutpoints(6), tier_set=TIER_SET,
                        hp=DEFAULTS)
    for entry in by_id(tiers).values():
        assert entry.above != entry.name
        assert entry.below != entry.name


def test_a_neighbour_is_never_borrowed_from_another_tier():
    tiers = board.build(items([1.0, -1.0]), cuts=np.array([0.0]), tier_set=("F", "S"),
                        hp=DEFAULTS)
    for entry in by_id(tiers).values():
        assert entry.above is None and entry.below is None


@pytest.mark.parametrize("seed", range(6))
def test_the_badged_set_and_the_queue_pool_are_the_same_set(seed):
    """One predicate, two jobs; the prototype's two thresholds made two sets (proposal 157)."""
    from spielplan.rank import queue

    rng = np.random.default_rng(seed)
    n = 60
    values = rng.normal(size=n)
    sigmas = rng.uniform(0.01, 0.6, size=n)
    cuts = model.initial_cutpoints(6)
    pool = items(values, sigma=sigmas)

    badged = {e.title_id for e in by_id(
        board.build(pool, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS)
    ).values() if e.straddle is not None}
    eligible = {i.title_id for i in queue.eligible(pool, cuts=cuts, hp=DEFAULTS)}

    assert badged == eligible
    assert badged, "the fixture has to actually produce straddlers or this proves nothing"

    # A guard, not a regression test: `board.straddles` never reads `assigned_tier`, so this passes
    # either side of finding 14. It catches a reach clamped to the rendered tier.
    dropped = [
        dataclasses.replace(item, assigned_tier=int(rng.integers(0, len(TIER_SET))))
        for item in pool
    ]
    entries = by_id(board.build(dropped, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS))
    badged_after_drops = {e.title_id for e in entries.values() if e.straddle is not None}
    assert badged_after_drops == eligible

    # The CHIP is the half finding 14 broke: §6.3's identity is about what the person sees.
    chipped = {e.title_id for e in entries.values() if e.straddle_badge is not None}
    assert chipped == {
        e.title_id for e in entries.values() if e.straddle is not None and e.tension is None
    }, "a queue-eligible title on a dropped board wears no chip, or wears one it should not"


def test_moving_the_straddle_threshold_moves_both_sets_together():
    """The threshold is a bundle constant (§4.3); a hard-coded side would split the two sets."""
    from spielplan.rank import queue

    rng = np.random.default_rng(11)
    pool = items(rng.normal(size=80), sigma=rng.uniform(0.01, 0.5, size=80))
    cuts = model.initial_cutpoints(6)

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


def test_a_fitted_boards_straddle_badge_is_a_minority_of_the_board():
    """Hand-built `s`/`sigma` cannot show whether the badge discriminates; a fitted board can (at
    `straddle_z` 1.0 every title badged). A minority, not a number: §4.3 retunes the value."""
    from spielplan.rank import queue

    rng = np.random.default_rng(5)
    n = 120
    embeddings = rng.normal(size=(n, 64)) / 8.0
    truth = 0.3 + (embeddings @ (rng.normal(size=64) / 8.0)) + rng.normal(scale=0.25, size=n)
    # A maturer board than the release ships, so the generous case.
    verdicts = np.searchsorted(np.array([-0.4, 0.4]), truth, side="right")
    dropped = rng.choice(n, size=90, replace=False)
    tier_cuts = np.quantile(truth, np.linspace(0, 1, len(TIER_SET) + 1)[1:-1])
    duels = rng.choice(n, size=(200, 2))
    duels = duels[duels[:, 0] != duels[:, 1]]
    gap = truth[duels[:, 0]] - truth[duels[:, 1]]

    fit = model.fit(
        ObservationSet(
            title_ids=np.arange(n, dtype=np.int64),
            embeddings=embeddings,
            embedded=np.full(n, True),
            ord_index=np.concatenate([np.arange(n), dropped]).astype(np.int64),
            ord_level=np.concatenate([
                verdicts, np.searchsorted(tier_cuts, truth[dropped], side="right")
            ]).astype(np.int64),
            ord_arm=np.concatenate([np.zeros(n), np.ones(dropped.size)]).astype(np.int64),
            ord_weight=np.ones(n + dropped.size),
            duel_a=duels[:, 0].astype(np.int64),
            duel_b=duels[:, 1].astype(np.int64),
            duel_outcome=np.where(
                np.abs(gap) < 0.15, OUT_TIE, np.where(gap > 0, OUT_A, OUT_B)
            ).astype(np.int64),
            duel_margin=np.where(rng.random(len(duels)) < 0.4, 1.6, 1.0),
            n_levels=len(TIER_SET),
        ),
        DEFAULTS,
    )
    assert fit.converged, "a fit that did not converge measures nothing about a real board"

    fitted = [
        board.Item(title_id=i + 1, name=f"T{i + 1}", s=float(fit.s[i]), sigma=float(fit.sigma[i]))
        for i in range(n)
    ]
    entries = by_id(board.build(fitted, cuts=fit.cuts, tier_set=TIER_SET, hp=DEFAULTS))
    badged = {t for t, e in entries.items() if e.straddle is not None}

    assert 0 < len(badged) < n / 2, (
        f"{len(badged)} of {n} titles straddle at straddle_z={DEFAULTS.straddle_z}; the badge "
        "has to single titles out, and a threshold that badges the board singles out nothing"
    )
    # The queue's 70% arm draws from this set and its 20% arm from all, so it must be a proper subset.
    eligible = {i.title_id for i in queue.eligible(fitted, cuts=fit.cuts, hp=DEFAULTS)}
    assert eligible == badged
    assert eligible < {i.title_id for i in fitted}


def test_the_top_and_bottom_tiers_never_straddle_into_themselves():
    """At the ends there is one direction to reach, so the badge names that side, never its own tier."""
    cuts = model.initial_cutpoints(6)
    tiers = board.build(
        items([float(cuts[-1]) + 0.15, float(cuts[0]) - 0.15], sigma=reach_sigma(0.4)),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    )
    top, bottom = by_id(tiers)[1], by_id(tiers)[2]
    assert top.tier == len(TIER_SET) - 1 and bottom.tier == 0
    assert top.straddle_badge == "S or A?"
    assert bottom.straddle_badge == "E or D?"


def test_a_straddle_badge_never_repeats_the_titles_own_tier():
    """Both halves of the badge come from the posterior; the rendered tier is the person's drop."""
    cuts = model.initial_cutpoints(6)
    rng = np.random.default_rng(7)
    pool = items(rng.normal(scale=2.0, size=120), sigma=rng.uniform(0.01, 4.0, size=120))
    # Assigned tiers too, so an index taken from the wrong place would show.
    rng2 = np.random.default_rng(8)
    assigned = {i + 1: int(rng2.integers(0, len(TIER_SET))) for i in range(120)}
    for board_pool in (pool, items(rng.normal(scale=2.0, size=120), sigma=1.2, assigned=assigned)):
        entries = by_id(board.build(board_pool, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS))
        for entry in entries.values():
            assert entry.straddle != entry.model_tier, "never the tier it was computed against"
            if entry.straddle_badge is not None:
                head, _, tail = entry.straddle_badge.removesuffix("?").partition(" or ")
                assert head != tail, f"a badge naming one tier twice: {entry.straddle_badge}"
                assert head == TIER_SET[entry.model_tier], (
                    "the badge leads with the posterior's own tier, not the rendered one"
                )
                assert tail == TIER_SET[entry.straddle]


def test_the_straddle_chip_is_built_from_the_posteriors_own_placement():
    """The drop decides where the row renders and nothing about the chip (finding 14)."""
    cuts = model.initial_cutpoints(6)
    entry = by_id(board.build(
        items([0.9], sigma=reach_sigma(1.0), assigned={1: 4}),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    ))[1]
    assert entry.tier == 4, "§6.3: it stays where it was put"
    assert entry.model_tier == 3 and entry.straddle == 4
    assert entry.tension is None, "the bands meet, so the straddle chip is the one on screen"
    assert entry.straddle_badge == "B or A?"


def test_a_queue_eligible_title_dropped_into_the_tier_it_reaches_still_wears_a_chip():
    """Dropping a straddler into the tier it reaches must keep the chip: the queue still counts it."""
    cuts = model.initial_cutpoints(6)
    entry = by_id(board.build(
        items([0.9], sigma=reach_sigma(1.0), assigned={1: 2}),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    ))[1]
    assert entry.straddle is not None, "still queue-eligible"
    assert entry.tier == 2 and entry.tension is None
    assert entry.straddle_badge == "B or A?"


def test_a_tier_outside_the_eighty_percent_interval_is_tension():
    """Proposal 71's "disagrees strongly": the assigned tier and the 80% interval are disjoint."""
    tiers = board.build(
        items([0.0], sigma=0.01, assigned={1: 5}),
        cuts=model.initial_cutpoints(6), tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.tension is not None
    assert entry.tier == 5, "and it stays where it was put"


def test_a_one_level_disagreement_inside_the_interval_is_not_tension():
    """A neighbouring tier the posterior reaches is not tension; badging it would badge most rows."""
    cuts = model.initial_cutpoints(6)
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
    # s = -0.7 sits inside C's band, so the two tiers in the line are genuinely different.
    tiers = board.build(
        items([-0.7], sigma=0.01, assigned={1: 5}, names={1: "Drive"}),
        cuts=model.initial_cutpoints(6), tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert entry.model_tier == 2 and entry.tier == 5
    # Member register (decision 486): no model nouns on a chip every member reads.
    assert entry.tension == "You put it in S — your other answers still point to C"
    assert "ledger" not in entry.tension


def test_the_board_never_moves_a_title_out_of_the_tier_it_was_dropped_in():
    cuts = model.initial_cutpoints(6)
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
    cuts = model.initial_cutpoints(6)
    entry = by_id(board.build(items([0.42]), cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS))[1]
    assert entry.assigned_tier is None
    assert entry.tier == entry.model_tier == model.tier_of(np.array([0.42]), cuts)[0]
    assert entry.tension is None


def test_the_tension_threshold_comes_from_the_bundle():
    """A wider credible interval must make tension strictly rarer; a hard-coded 80% would not move."""
    cuts = model.initial_cutpoints(6)
    rng = np.random.default_rng(4)
    values = rng.normal(size=50)
    assigned = {i + 1: int(rng.integers(0, len(TIER_SET))) for i in range(50)}
    pool = items(values, sigma=0.25, assigned=assigned)

    counts = []
    for mass in (0.50, 0.80, 0.999):
        hp = dataclasses.replace(DEFAULTS, tension_credible_mass=mass)
        tiers = board.build(pool, cuts=cuts, tier_set=TIER_SET, hp=hp)
        counts.append(sum(1 for e in by_id(tiers).values() if e.tension is not None))
    assert counts[0] > counts[1] > counts[2]


def test_a_tier_edit_above_the_new_tier_set_renders_instead_of_crashing():
    """Decision 11 keeps `tier_edit` rows across a K change, and the board indexes the cutpoint
    array directly, so it must clamp."""
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


@pytest.mark.parametrize("assigned", [-3, -1, 6, 40])
def test_no_out_of_range_assignment_can_take_the_board_down(assigned):
    """`tier_edit.tier` has no CHECK against the tier set, so survive anything a smallint holds."""
    cuts = model.initial_cutpoints(6)
    tiers = board.build(
        items([0.0], sigma=0.4, assigned={1: assigned}),
        cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS,
    )
    entry = by_id(tiers)[1]
    assert 0 <= entry.tier < len(TIER_SET)
    assert entry.assigned_tier is not None and 0 <= entry.assigned_tier < len(TIER_SET)


def test_a_rated_title_renders_inside_the_tiers_its_verdict_names():
    """Decision 508: a rated title stays inside its verdict's tiers (disliked E, fine D, liked
    C/B/A/S), ordered by `s`; its straddle names the next tier toward the fit. Drops are left alone."""
    cuts = model.initial_cutpoints(6)                     # E/D at -2.94, D/C at -1.39
    rows = [
        board.Item(title_id=1, name="La La Land", s=0.5, sigma=0.01, verdict=0),
        board.Item(title_id=2, name="Psycho", s=0.4, sigma=0.01, verdict=1),
        board.Item(title_id=3, name="Heat", s=-2.0, sigma=0.01, verdict=2),
        board.Item(title_id=4, name="Twilight", s=-3.5, sigma=0.01, verdict=0),
        board.Item(title_id=5, name="Saw", s=0.6, sigma=0.01, verdict=0, assigned_tier=4),
    ]
    tiers = {t.label: t for t in board.build(rows, cuts=cuts, tier_set=TIER_SET, hp=DEFAULTS)}
    placed = {e.name: (label, e) for label, t in tiers.items() for e in t.entries}
    assert placed["La La Land"][0] == "E" and placed["La La Land"][1].straddle == 1
    assert placed["La La Land"][1].straddle_badge == "E or D?"
    assert [e.name for e in tiers["E"].entries] == ["La La Land", "Twilight"]
    assert placed["Psycho"][0] == "D" and placed["Heat"][0] == "C"
    assert placed["Saw"][0] == "A" and placed["Saw"][1].model_tier == 3, "a drop is not held"
    eligible = [i.title_id for i in rows if board.straddles(i, cuts=cuts, hp=DEFAULTS) is not None]
    assert 1 in eligible, "the held title's chip and its queue eligibility are one predicate"
