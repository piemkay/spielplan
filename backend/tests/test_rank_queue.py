"""No database: the 70/20/10 mix is a distribution, checked by drawing twenty thousand times."""

from __future__ import annotations

import random
from collections import Counter

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import board, queue

TIER_SET = ("F", "D", "C", "B", "A", "A+", "S")
CUTS = model.initial_cutpoints(7)


def sigma_for(reach: float) -> float:
    """Spelled as the reach z·σ, not σ: decision 214 retuned `straddle_z`, reshaping bare-σ boards."""
    return reach / DEFAULTS.straddle_z


STRADDLING = sigma_for(0.35)   # the default board's σ, as the reach it is chosen for


def pool(n=60, *, sigma=STRADDLING, seed=2, comparisons=None):
    """`sigma` takes a sequence too: finding 12 is about a board where only some titles settled."""
    rng = np.random.default_rng(seed)
    values = np.linspace(float(CUTS[0]) - 1.0, float(CUTS[-1]) + 1.0, n)
    values = values + rng.normal(scale=0.05, size=n)
    sigmas = list(sigma) if isinstance(sigma, (list, tuple, np.ndarray)) else [sigma] * n
    items = [
        board.Item(title_id=i + 1, name=f"T{i + 1}", s=float(v), sigma=float(sigmas[i]))
        for i, v in enumerate(values)
    ]
    return queue.candidates(
        items, cuts=CUTS, tier_set=TIER_SET, hp=DEFAULTS, comparisons=comparisons
    )


def test_a_long_draw_is_seventy_twenty_ten():
    candidates = pool()
    rng = random.Random(17)
    draws = [queue.draw(candidates, rng=rng) for _ in range(20_000)]
    assert all(d is not None for d in draws), "every draw returns a pair on a healthy board"
    arms = Counter(d.arm for d in draws)

    total = sum(arms.values())
    assert arms[queue.ARM_BOUNDARY] / total == pytest.approx(0.70, abs=0.015)
    assert arms[queue.ARM_EXPLORATION] / total == pytest.approx(0.20, abs=0.015)
    assert arms[queue.ARM_HOLDOUT] / total == pytest.approx(0.10, abs=0.010)


def test_the_shares_are_the_specs_own_numbers():
    """§6.3's own text, not a §4.3 bundle knob."""
    assert dict(queue.SHARES) == {
        queue.ARM_BOUNDARY: 0.70,
        queue.ARM_EXPLORATION: 0.20,
        queue.ARM_HOLDOUT: 0.10,
    }
    assert sum(share for _, share in queue.SHARES) == pytest.approx(1.0)


def test_a_held_out_pair_is_never_labelled_boundary_targeted():
    """The arm a pair reports is the arm that drew it (proposal 120)."""
    candidates = pool()
    rng = random.Random(5)
    for _ in range(5_000):
        pair = queue.draw(candidates, rng=rng)
        if "held out" in pair.reason:
            assert pair.arm == queue.ARM_HOLDOUT
        if pair.arm == queue.ARM_HOLDOUT:
            assert "held out" in pair.reason
            assert "boundary" not in pair.reason


def test_the_held_out_arm_is_uniform_over_pairs_not_over_strata():
    """Tier first, then pair, is uniform over strata and over-samples the sparse ones."""
    candidates = pool(n=8, sigma=0.35)
    rng = random.Random(3)
    seen = Counter()
    draws = 60_000
    for _ in range(draws):
        pair = queue._holdout(candidates, rng)
        seen[tuple(sorted((pair.title_a, pair.title_b)))] += 1

    expected = draws / (8 * 7 / 2)
    assert len(seen) == 8 * 7 // 2, "every pair has to be reachable"
    assert min(seen.values()) > expected * 0.85
    assert max(seen.values()) < expected * 1.15


@pytest.mark.parametrize(
    ("label", "candidates"),
    [
        ("every title straddles", pool(sigma=sigma_for(3.0))),
        ("no title straddles", pool(sigma=1e-6)),
        ("two titles", pool(n=2, sigma=0.35)),
        ("one tier occupied", pool(n=20, sigma=1e-6, seed=5)),
    ],
)
def test_the_held_out_share_does_not_move_with_the_pool(label, candidates):
    """The evaluation stream's rate must not depend on the model's confidence. Degenerate pools,
    because that is where a fallback fires."""
    rng = random.Random(23)
    arms = Counter(queue.draw(candidates, rng=rng).arm for _ in range(8_000))
    share = arms[queue.ARM_HOLDOUT] / 8_000
    assert share == pytest.approx(0.10, abs=0.015), (
        f"the held-out rate moved with the pool ({label}): {share:.3f}"
    )


def test_the_held_out_arm_never_receives_a_fallback():
    single = pool(n=1)
    rng = random.Random(1)
    assert queue.draw(single, rng=rng) is None

    # And the reverse: an exhausted held-out roll never re-rolls into an adaptive arm.
    assert queue._holdout(single, rng) is None


def test_a_pool_with_no_straddler_falls_back_to_exploration_and_says_so():
    """No straddler: exploration under its own name, never a "boundary" label (proposal 120)."""
    candidates = pool(sigma=1e-6)
    assert not [c for c in candidates if c.straddle is not None]

    rng = random.Random(9)
    arms = Counter(queue.draw(candidates, rng=rng).arm for _ in range(4_000))
    assert arms[queue.ARM_BOUNDARY] == 0
    assert arms[queue.ARM_EXPLORATION] / 4_000 == pytest.approx(0.90, abs=0.02)
    # The held-out share is untouched by the fallback, which is the whole point.
    assert arms[queue.ARM_HOLDOUT] / 4_000 == pytest.approx(0.10, abs=0.02)


def test_a_boundary_pair_crosses_the_cutpoint_the_title_straddles():
    """A straddler's pair settles a boundary only if it spans it."""
    candidates = pool()
    by_id = {c.title_id: c for c in candidates}
    rng = random.Random(31)

    checked = 0
    for _ in range(400):
        pair = queue._boundary(candidates, rng)
        if pair is None:
            continue
        anchor, partner = by_id[pair.title_a], by_id[pair.title_b]
        assert anchor.straddle is not None
        assert partner.tier == anchor.straddle, "the partner is across the boundary, not beside it"
        assert anchor.tier != partner.tier
        checked += 1
    assert checked > 0


def test_exploration_reaches_the_least_compared_title():
    candidates = pool(sigma=1e-6, comparisons={i: 50 for i in range(1, 61)} | {42: 0})
    rng = random.Random(4)
    pair = queue._exploration(candidates, rng)
    assert 42 in (pair.title_a, pair.title_b)
    assert pair.arm == queue.ARM_EXPLORATION


def test_exploration_anchors_on_the_least_compared_title_of_the_whole_pool():
    """The anchor is drawn from the whole pool (finding 12): a non-straddler filter inverted into
    the handful the model is most sure about."""
    counts = {i: 50 for i in range(1, 61)} | {42: 0}
    candidates = pool(sigma=STRADDLING, comparisons=counts)
    by_id = {c.title_id: c for c in candidates}
    assert by_id[42].straddle is not None, "the least-compared title is one the model is unsure of"
    assert [c for c in candidates if c.straddle is None], "and some of the board has settled"

    rng = random.Random(4)
    pair = queue._exploration(candidates, rng)
    assert 42 in (pair.title_a, pair.title_b), (
        "the anchor is the least-compared title on the board, not on the settled corner of it"
    )
    assert pair.arm == queue.ARM_EXPLORATION


def test_exploration_never_re_serves_a_pair_it_has_already_asked():
    """Each repeat is an independent Davidson row, so repeats inflate reliability (§13)."""
    sigmas = [sigma_for(3.0)] * 60
    sigmas[6] = sigmas[49] = 1e-6
    assert len([c for c in pool(sigma=sigmas) if c.straddle is None]) == 2

    counts = {i: 0 for i in range(1, 61)}
    asked: set[frozenset[int]] = set()
    rng = random.Random(8)
    for draw in range(200):
        pair = queue._exploration(pool(sigma=sigmas, comparisons=counts), rng, asked=asked)
        assert pair is not None, f"the pool has 1770 distinct pairs and ran out at {draw}"
        key = frozenset((pair.title_a, pair.title_b))
        assert key not in asked, f"re-served a pair already answered: {sorted(key)}"
        asked.add(key)
        counts[pair.title_a] += 1
        counts[pair.title_b] += 1
    assert len(asked) == 200

    # Through `draw`, and counted over two hundred draws: a single roll landed in the held-out band
    # and never ran the branch.
    explored = 0
    for draw in range(200):
        served = queue.draw(pool(sigma=sigmas, comparisons=counts), rng=rng, asked=asked)
        assert served is not None, f"the board ran out of pairs at draw {draw}"
        if served.arm != queue.ARM_EXPLORATION:
            continue
        explored += 1
        assert frozenset((served.title_a, served.title_b)) not in asked, (
            f"draw {draw}: `draw` served a pair already answered, so it did not hand `asked` to "
            f"the arm: {sorted((served.title_a, served.title_b))}"
        )
    assert explored > 0, "no exploration draw came up, so the clause above asserted nothing"


def test_exploration_rotates_as_comparisons_accrue():
    """Answering increments both counts, so the least-compared title moves on."""
    counts = {i: 5 for i in range(1, 61)}
    rng = random.Random(6)
    served = set()
    for _ in range(6):
        pair = queue._exploration(
            pool(sigma=1e-6, comparisons=counts), rng
        )
        served.add(tuple(sorted((pair.title_a, pair.title_b))))
        counts[pair.title_a] += 1
        counts[pair.title_b] += 1
    assert len(served) > 1


def test_a_pair_is_never_a_title_against_itself():
    candidates = pool()
    rng = random.Random(13)
    for _ in range(3_000):
        pair = queue.draw(candidates, rng=rng)
        assert pair.title_a != pair.title_b


def test_the_selector_reads_no_held_out_comparison():
    """Nothing in a `Candidate` carries a duel row, so there is no second path to the stream."""
    candidate = pool(n=2)[0]
    fields = set(vars(candidate))
    assert fields == {"item", "comparisons", "straddle", "tier"}
    assert not hasattr(candidate.item, "duels")


def board_of(spec, *, comparisons=None):
    """`(title_id, s, reach)`, reach 0 for a settled title. The cuts are §6.3's prior: B/A at 0.0,
    A+/S at 2.442."""
    items = [
        board.Item(
            title_id=title_id, name=f"T{title_id}", s=float(s),
            sigma=sigma_for(reach) if reach else 1e-6,
        )
        for title_id, s, reach in spec
    ]
    return queue.candidates(
        items, cuts=CUTS, tier_set=TIER_SET, hp=DEFAULTS, comparisons=comparisons
    )


# Three straddlers at B/A, three at A+/S, and settled titles across each cut.
TWO_BOUNDARIES = [
    (1, -0.01, 0.05), (2, -0.02, 0.05), (3, -0.03, 0.05),
    (4, 2.43, 0.05), (5, 2.42, 0.05), (6, 2.41, 0.05),
    (11, 0.3, 0), (12, 0.5, 0), (13, 0.7, 0),
    (14, 3.0, 0), (15, 3.2, 0), (16, 3.4, 0),
]


def test_the_boundary_arm_favours_boundaries_near_the_top():
    """Decision 494: anchors are weighted by boundary height, so A+/S (6) is drawn 1.5 times as
    often as B/A (4)."""
    candidates = board_of(TWO_BOUNDARIES)
    by_id = {c.title_id: c for c in candidates}
    assert {by_id[t].straddle for t in (1, 2, 3)} == {4}
    assert {by_id[t].straddle for t in (4, 5, 6)} == {6}
    assert [queue.boundary_height(by_id[t]) for t in (1, 4)] == [4, 6]

    rng = random.Random(41)
    anchors = Counter(queue._boundary(candidates, rng).title_a for _ in range(20_000))
    top = sum(anchors[t] for t in (4, 5, 6)) / 20_000
    assert top == pytest.approx(6 / (6 + 4), abs=0.015), f"the A+/S share was {top:.3f}"
    # Weighted, never restricted: every straddler is still an anchor (M4.10 finding 12).
    assert set(anchors) == {1, 2, 3, 4, 5, 6}


def test_a_boundary_partner_is_the_least_compared_of_the_five_nearest_across_the_cut():
    """Decision 494: the least-compared of the five nearest across the cut; the sixth is never reached."""
    spec = [(1, -0.01, 0.05)] + [(10 + i, 0.1 * i, 0) for i in range(1, 7)]
    comparisons = {11: 10, 12: 3, 13: 0, 14: 5, 15: 5, 16: 0}
    candidates = board_of(spec, comparisons=comparisons)
    rng = random.Random(3)
    partners = Counter(queue._boundary(candidates, rng).title_b for _ in range(300))
    assert partners == Counter({13: 300}), partners


def test_the_boundary_arm_never_re_serves_a_pair_it_has_already_asked():
    """Each repeat is an independent Davidson row (decision 494)."""
    spec = [(1, -0.01, 0.05), (11, 0.3, 0), (12, 0.5, 0), (13, 0.7, 0)]
    candidates = board_of(spec)
    asked = {frozenset((1, 11)), frozenset((1, 12))}
    rng = random.Random(19)
    for _ in range(2_000):
        pair = queue._boundary(candidates, rng, asked=asked)
        assert frozenset((pair.title_a, pair.title_b)) == frozenset((1, 13))

    candidates = pool()
    served: set[frozenset[int]] = set()
    rng = random.Random(29)
    for draw in range(5_000):
        pair = queue._boundary(candidates, rng, asked=served)
        if pair is None:
            break
        key = frozenset((pair.title_a, pair.title_b))
        assert key not in served, f"draw {draw} re-served {sorted(key)}"
        served.add(key)
    else:
        raise AssertionError("the boundary arm never ran out on a sixty-title board")
    assert len(served) > 100, f"the arm gave up after {len(served)} pairs"


def test_a_title_its_verdict_holds_is_a_candidate_in_the_tier_the_board_renders():
    """Decision 508 rule 5: the queue's tier is the board's held tier, not the raw `tier_of`."""
    settled = 1e-6
    items = [
        board.Item(title_id=1, name="L1", s=-0.5, sigma=settled, verdict=2),
        board.Item(title_id=2, name="L2", s=-0.45, sigma=settled, verdict=2),
        board.Item(title_id=3, name="F1", s=-0.3, sigma=settled, verdict=1),
        board.Item(title_id=4, name="D1", s=0.5, sigma=settled, verdict=0),
        board.Item(title_id=5, name="A1", s=0.6, sigma=settled, verdict=2),
    ]
    rendered = {
        e.title_id: e.model_tier
        for t in board.build(items, cuts=CUTS, tier_set=TIER_SET, hp=DEFAULTS)
        for e in t.entries
    }
    candidates = queue.candidates(items, cuts=CUTS, tier_set=TIER_SET, hp=DEFAULTS)
    by_id = {c.title_id: c for c in candidates}
    assert {t: c.tier for t, c in by_id.items()} == rendered == {1: 4, 2: 4, 3: 3, 4: 2, 5: 4}
    assert all(c.straddle != c.tier for c in candidates), "a straddle names another tier"
    # B/A weighs as B/A (4) for the held liked films, and C/B (3) for the held disliked one.
    assert [queue.boundary_height(by_id[t]) for t in (1, 4)] == [4, 3]

    rng = random.Random(7)
    for _ in range(500):
        pair = queue._boundary(candidates, rng)
        anchor, partner = by_id[pair.title_a], by_id[pair.title_b]
        assert rendered[partner.title_id] == anchor.straddle, (pair, "not across the cut")
        assert partner.title_id == 3, "the one title the board renders in B"


def test_an_exhausted_boundary_arm_falls_through_to_exploration_and_says_so():
    """An exhausted boundary arm draws exploration under its own name, never the held-out arm."""
    spec = [(1, -0.01, 0.05), (11, 0.3, 0), (12, 0.5, 0), (21, -0.5, 0), (22, -0.7, 0)]
    candidates = board_of(spec)
    asked = {frozenset((1, 11)), frozenset((1, 12))}
    assert queue._boundary(candidates, random.Random(1), asked=asked) is None

    rng = random.Random(9)
    arms = Counter()
    for _ in range(4_000):
        pair = queue.draw(candidates, rng=rng, asked=asked)
        arms[pair.arm] += 1
        if pair.arm == queue.ARM_EXPLORATION:
            assert frozenset((pair.title_a, pair.title_b)) not in asked
    assert arms[queue.ARM_BOUNDARY] == 0
    assert arms[queue.ARM_EXPLORATION] / 4_000 == pytest.approx(0.90, abs=0.02)
    assert arms[queue.ARM_HOLDOUT] / 4_000 == pytest.approx(0.10, abs=0.02)


def test_a_tie_in_s_is_broken_by_the_draw_and_not_by_the_lowest_id():
    """Films with no coordinate share one verdict-set `s`; an id tie-break always picked the same one."""
    spec = [(1, -0.01, 0.05)] + [(10 + i, 0.5, 0) for i in range(1, 5)]
    candidates = board_of(spec)
    rng = random.Random(2)
    partners = Counter(queue._boundary(candidates, rng).title_b for _ in range(400))
    assert set(partners) == {11, 12, 13, 14}, partners
    assert max(partners.values()) < 400 * 0.4, partners


def test_the_recent_window_holds_a_title_back_while_another_can_take_its_place():
    """Soft, so a board with nothing else still draws."""
    spec = [(1, -0.01, 0.05), (2, -0.02, 0.05), (11, 0.1, 0), (12, 0.2, 0)]
    candidates = board_of(spec)
    rng = random.Random(5)
    for _ in range(500):
        pair = queue._boundary(candidates, rng, recent={1, 11})
        assert (pair.title_a, pair.title_b) == (2, 12)

    everything = {1, 2, 11, 12}
    assert queue._boundary(candidates, rng, recent=everything) is not None
    assert queue._exploration(candidates, rng, recent=everything) is not None

    settled = board_of([(1, 0.5, 0), (2, 0.6, 0), (3, 0.7, 0)])
    for _ in range(200):
        pair = queue._exploration(settled, rng, recent={3})
        assert pair.title_a != 3


def test_exploration_breaks_ties_toward_the_top_of_the_board():
    """Decision 494: the widest σ used to decide, and it belonged to off-scale tails no answer moves."""
    spec = [(i, s, 0) for i, s in enumerate((-4.0, -3.0, -1.5, -0.5, 0.5, 1.5, 3.0, 3.5), start=1)]
    candidates = board_of(spec)
    by_id = {c.title_id: c for c in candidates}
    top = max(c.tier for c in candidates)
    for seed in range(200):
        pair = queue._exploration(candidates, random.Random(seed))
        assert by_id[pair.title_a].tier == top, f"seed {seed} anchored on tier {pair.title_a}"


def test_exploration_still_explores_a_board_that_is_all_bottom_tiers():
    """The weighting orders anchors and removes none, so an all-F/D board is still explored."""
    spec = [(i, -4.0 + 0.2 * i, 0) for i in range(1, 7)]
    candidates = board_of(spec)
    assert {c.tier for c in candidates} <= {0, 1}
    asked: set[frozenset[int]] = set()
    rng = random.Random(7)
    for draw in range(15):
        pair = queue._exploration(candidates, rng, asked=asked)
        assert pair is not None, f"the all-bottom board stopped being explored at {draw}"
        asked.add(frozenset((pair.title_a, pair.title_b)))
    assert queue._exploration(candidates, rng, asked=asked) is None


def test_a_board_with_every_adaptive_pair_asked_still_draws_the_held_out_tenth():
    """The held-out arm reads neither `asked` nor the window (§13), so its tenth survives."""
    candidates = pool(n=4, sigma=STRADDLING)
    asked = {frozenset((a, b)) for a in range(1, 5) for b in range(a + 1, 5)}
    rng = random.Random(11)
    outcomes = Counter()
    for _ in range(6_000):
        pair = queue.draw(candidates, rng=rng, asked=asked, recent={1, 2, 3, 4})
        outcomes[None if pair is None else pair.arm] += 1
    assert set(outcomes) == {None, queue.ARM_HOLDOUT}, outcomes
    assert outcomes[queue.ARM_HOLDOUT] / 6_000 == pytest.approx(0.10, abs=0.015)
