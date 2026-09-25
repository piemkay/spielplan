"""§6.3's comparison queue, and the guard §13 calls non-negotiable.

Spec v2.1 §6.3, §13 stream (a), §0 row 6; proposals 73, 120, 146, decision 54b.

No database, and for the same reason `rate/battle.py`'s draw is tested here rather than there:
"over a long draw the queue yields 70% / 20% / 10%" is a claim about a distribution, and the
only way to check a distribution is to draw from it twenty thousand times.

The mix is not the interesting half. §13's guard is: "the 10% uniform-random comparison stream
is the *only* data used to evaluate the tier model — adaptively-selected pairs inflate
reliability (measured effect; the guard is non-negotiable)". Everything that could quietly
break it is a test below — a held-out pair labelled as something else, a held-out arm whose
rate moves with the model's own confidence, a held-out draw that is uniform over strata
instead of over pairs.
"""

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
    """The σ whose ±z·σ interval reaches exactly `reach`, at whatever `straddle_z` ships.

    Every board below is built to make a stated number of titles straddle, and what decides that
    is the REACH z·σ, not σ. Decision 214 retunes `straddle_z` from 1.0 to 0.15: written as a bare
    σ, "every title straddles" quietly became 41 of 60 and the two-settled-titles board became
    twenty-one, so the arms these tests are about were being exercised on a different board than
    the docstrings describe. The geometry is pinned to the reach and is now the same at any
    positive threshold.
    """
    return reach / DEFAULTS.straddle_z


STRADDLING = sigma_for(0.35)   # the default board's σ, as the reach it is chosen for


def pool(n=60, *, sigma=STRADDLING, seed=2, comparisons=None):
    """A board wide enough that every arm can actually draw: titles spread across the whole
    cutpoint range with a σ that makes many of them straddle, and every tier populated.

    `sigma` takes a sequence as well as a scalar, because finding 12's failure is a statement
    about a board where *some* titles have settled and the rest have not.
    """
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


# --- §6.3's mix ---------------------------------------------------------------------------


def test_a_long_draw_is_seventy_twenty_ten():
    """§6.3: "70% posterior-straddling pairs / 20% exploration / 10% uniform-random held out"."""
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
    """§4.3 is for constants the corpus project re-tunes offline. These three are §6.3's own
    text, so they are not a bundle knob and must not become one."""
    assert dict(queue.SHARES) == {
        queue.ARM_BOUNDARY: 0.70,
        queue.ARM_EXPLORATION: 0.20,
        queue.ARM_HOLDOUT: 0.10,
    }
    assert sum(share for _, share in queue.SHARES) == pytest.approx(1.0)


# --- §13's guard --------------------------------------------------------------------------


def test_a_held_out_pair_is_never_labelled_boundary_targeted():
    """Proposal 120: the prototype pushed "boundary-targeted pair (70/20/10 policy)"
    unconditionally, so every tenth line asserted boundary-targeting about the one stream that
    must not be adaptively selected. The arm a pair reports is the arm that drew it."""
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
    """A draw that picks a tier first and a pair inside it second is uniform over *strata*,
    which over-samples the sparse ones — `rate/battle.py` argues the same point at length. Over
    a small pool every unordered pair must come up about equally often."""
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
    """§13's whole point, as one measurement: the evaluation stream's *rate* must be
    independent of the model's own confidence.

    The M3 review found the previous version of this test asserting the `len(pool) < 2` arity
    guard — it passed against a `_boundary(...) or _holdout(...)` fallback AND against a
    `_holdout(...) or _exploration(...)` one. Neither of those changes the shares on a healthy
    board; both change them on a degenerate one, which is where a fallback fires. So the pools
    below are the degenerate ones, and the assertion is that 10% holds across all of them.
    """
    rng = random.Random(23)
    arms = Counter(queue.draw(candidates, rng=rng).arm for _ in range(8_000))
    share = arms[queue.ARM_HOLDOUT] / 8_000
    assert share == pytest.approx(0.10, abs=0.015), (
        f"the held-out rate moved with the pool ({label}): {share:.3f}"
    )


def test_the_held_out_arm_never_receives_a_fallback():
    """A pool with nothing to sharpen returns nothing rather than manufacturing held-out rows."""
    single = pool(n=1)
    rng = random.Random(1)
    assert queue.draw(single, rng=rng) is None

    # And the reverse: an exhausted held-out roll never re-rolls into an adaptive arm.
    assert queue._holdout(single, rng) is None


def test_a_pool_with_no_straddler_falls_back_to_exploration_and_says_so():
    """§6.3 gives shares for a board that has straddlers. One that has none has nothing to
    target, and the honest answer is exploration under its own name — a fallback that reported
    "boundary" would put the lie proposal 120 names into the other 70%."""
    candidates = pool(sigma=1e-6)
    assert not [c for c in candidates if c.straddle is not None]

    rng = random.Random(9)
    arms = Counter(queue.draw(candidates, rng=rng).arm for _ in range(4_000))
    assert arms[queue.ARM_BOUNDARY] == 0
    assert arms[queue.ARM_EXPLORATION] / 4_000 == pytest.approx(0.90, abs=0.02)
    # The held-out share is untouched by the fallback, which is the whole point.
    assert arms[queue.ARM_HOLDOUT] / 4_000 == pytest.approx(0.10, abs=0.02)


# --- what each arm actually draws ----------------------------------------------------------


def test_a_boundary_pair_crosses_the_cutpoint_the_title_straddles():
    """§6.3 gives the share and not the construction. A straddler paired with an arbitrary
    partner settles no boundary; the pair that settles one is the pair that spans it."""
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
    """The arm that is neither boundary-targeted nor uniform reduces uncertainty where no
    cutpoint is at stake. It has to actually find the title nobody has compared."""
    candidates = pool(sigma=1e-6, comparisons={i: 50 for i in range(1, 61)} | {42: 0})
    rng = random.Random(4)
    pair = queue._exploration(candidates, rng)
    assert 42 in (pair.title_a, pair.title_b)
    assert pair.arm == queue.ARM_EXPLORATION


def test_exploration_anchors_on_the_least_compared_title_of_the_whole_pool():
    """Finding 12. The arm used to pick its anchor out of `[c for c in pool if c.straddle is
    None] or list(pool)`, and §6.3 licenses no such restriction — it names the share and calls
    the arm "exploration".

    On a young board nothing is in that list, so the arm silently drew straddlers; the moment one
    title settled, the list inverted into the handful the model is *most* sure about, and the
    least-compared title among THOSE was served while `pair.reason` said "the least-compared
    title on your board". Simulated over 500 answers with the route's own semantics, |away| was
    1-2 of 900 from answer 110 on and its anchor ended with the most comparisons on the board.
    """
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
    """Finding 12's other half. Each repeat counts as an independent Davidson row, so ten
    repeats of one judgement shrink that pair's posterior by √10 on the strength of one answer
    — the reliability inflation §13 guards against, arriving by a different door
    (M3-open-points §3.1, which `tonight/round.py`'s `select` already answers with `asked`).

    The board here is the one that broke it: two titles settled, everything else straddling, so
    the old filter left an `away` of exactly two and one pair served 78 of 109 draws.
    """
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

    # And through `draw`, because the route hands the set to the selector and not to the arm.
    #
    # Counted rather than guarded by a bare `if`: the clause that used to stand here ran a single
    # `draw` behind `if served.arm == queue.ARM_EXPLORATION:`, and off this generator's state that
    # roll is 0.968 -- the held-out band. The branch never executed, so a `draw` that dropped
    # `asked=asked` on its way to `_exploration` passed it, which is the only forwarding the route
    # depends on. Two hundred draws over §6.3's mix give the arm its 20%, and the count says so
    # rather than hoping. The other two arms are deliberately not asserted here: the held-out arm
    # must not consult `asked` at all (§13), and the boundary arm's no-repeat rule has tests of
    # its own below (decision 494). [M4.10 cycle 1, M410-REV3]
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
    """It has to move on its own, or the queue serves one pair forever: answering increments
    both titles' counts, so the least-compared title is a different one next time."""
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
    """§13: the held-out stream feeds neither the selection rule nor any quality figure.

    The selector's only view of a title's comparison history is `Candidate.comparisons`, and
    the query that fills it excludes `selection = 'uniform_holdout'` (asserted against a real
    database in test_rank_integration.py). What is asserted here is the other half — that
    there is no second path: nothing in a `Candidate` carries a duel row, so a selector that
    wanted to read one would have to be given it.
    """
    candidate = pool(n=2)[0]
    fields = set(vars(candidate))
    assert fields == {"item", "comparisons", "straddle", "tier"}
    assert not hasattr(candidate.item, "duels")


# --- decision 494: which pair inside an adaptive arm ------------------------------------------


def board_of(spec, *, comparisons=None):
    """A board laid out title by title: `(title_id, s, reach)`, reach 0 for a settled title.

    The value-weight and partner tests are claims about WHERE on the board a straddler sits and
    which titles are near it across the cut, so the board is written out rather than spread by
    `pool()`. The cuts are §6.3's prior shape: B/A at 0.0, A+/S at 2.442.
    """
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


# Three straddlers at B/A (tier 3 reaching 4), three at A+/S (5 reaching 6), and settled titles in
# A and S for each of them to be paired across the cut with.
TWO_BOUNDARIES = [
    (1, -0.01, 0.05), (2, -0.02, 0.05), (3, -0.03, 0.05),
    (4, 2.43, 0.05), (5, 2.42, 0.05), (6, 2.41, 0.05),
    (11, 0.3, 0), (12, 0.5, 0), (13, 0.7, 0),
    (14, 3.0, 0), (15, 3.2, 0), (16, 3.4, 0),
]


def test_the_boundary_arm_favours_boundaries_near_the_top():
    """Decision 494: the anchor is drawn in proportion to the height of the boundary it straddles
    (the index of the cut's upper tier), so A+/S (6) is drawn 1.5 times as often as B/A (4).

    Drawn uniformly, the two groups split 50/50 - and on the first household's boards the B/A cut
    held most of the straddlers, because while nobody has moved a title the cut sits at s = 0 in
    the middle of the verdict arm's "fine" band: 15 of Patrick's 21 straddlers were films he had
    rated fine, and the boundary arm spent itself on pairs of them."""
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
    """Decision 494's partner, which is M3-open-points §3.1's fix shape ("draw the partner from
    the k nearest"). The nearest title across the cut is the most informative one and also the one
    every draw lands on - Patrick's first sitting partnered Ready Player One in four of six boundary
    draws. The sixth-nearest is never reached for, however rarely it has been compared."""
    spec = [(1, -0.01, 0.05)] + [(10 + i, 0.1 * i, 0) for i in range(1, 7)]
    comparisons = {11: 10, 12: 3, 13: 0, 14: 5, 15: 5, 16: 0}
    candidates = board_of(spec, comparisons=comparisons)
    rng = random.Random(3)
    partners = Counter(queue._boundary(candidates, rng).title_b for _ in range(300))
    assert partners == Counter({13: 300}), partners


def test_the_boundary_arm_never_re_serves_a_pair_it_has_already_asked():
    """M3-open-points §3.1's remaining half, closed by decision 494. The partner used to be the
    nearest title across the cut with no memory, so whenever the shuffle landed on the same anchor
    it served the same pair: Jenny answered the same pair twice in four answers, and each repeat is
    an independent Davidson row - one judgement asked twice shrinks that pair's posterior by root
    two (§13's reliability inflation, reached through the selector)."""
    spec = [(1, -0.01, 0.05), (11, 0.3, 0), (12, 0.5, 0), (13, 0.7, 0)]
    candidates = board_of(spec)
    asked = {frozenset((1, 11)), frozenset((1, 12))}
    rng = random.Random(19)
    for _ in range(2_000):
        pair = queue._boundary(candidates, rng, asked=asked)
        assert frozenset((pair.title_a, pair.title_b)) == frozenset((1, 13))

    # And over a whole board, answering each pair as it comes until the arm has nothing left:
    # nothing comes back, and the arm ends rather than repeating itself.
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


def test_an_exhausted_boundary_arm_falls_through_to_exploration_and_says_so():
    """A board whose every straddler has been asked against every partner across its cut has no
    boundary pair left. The roll that picked the boundary arm then draws an exploration pair and
    reports it as exploration - never a boundary label on a pair that is not one (proposal 120),
    and never a fall into the held-out arm, whose rate must not move (§13)."""
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
    """About ten films on each first-household board have no coordinate at all and sit on one `s`
    set by their verdict alone (five of Jenny's fine films at 0.224). `_nearest` broke every such
    tie by title id, so the same film won it on every draw."""
    spec = [(1, -0.01, 0.05)] + [(10 + i, 0.5, 0) for i in range(1, 5)]
    candidates = board_of(spec)
    rng = random.Random(2)
    partners = Counter(queue._boundary(candidates, rng).title_b for _ in range(400))
    assert set(partners) == {11, 12, 13, 14}, partners
    assert max(partners.values()) < 400 * 0.4, partners


def test_the_recent_window_holds_a_title_back_while_another_can_take_its_place():
    """Decision 494's window: a title from one of the last few answered pairs does not come
    straight back as anchor or partner while another can serve (Meet Joe Black was in three of
    Jenny's ten answers). Soft, so a board with nothing else still draws."""
    spec = [(1, -0.01, 0.05), (2, -0.02, 0.05), (11, 0.1, 0), (12, 0.2, 0)]
    candidates = board_of(spec)
    rng = random.Random(5)
    for _ in range(500):
        pair = queue._boundary(candidates, rng, recent={1, 11})
        assert (pair.title_a, pair.title_b) == (2, 12)

    everything = {1, 2, 11, 12}
    assert queue._boundary(candidates, rng, recent=everything) is not None
    assert queue._exploration(candidates, rng, recent=everything) is not None

    # The exploration arm's anchor: equally compared, the recent one waits.
    settled = board_of([(1, 0.5, 0), (2, 0.6, 0), (3, 0.7, 0)])
    for _ in range(200):
        pair = queue._exploration(settled, rng, recent={3})
        assert pair.title_a != 3


def test_exploration_breaks_ties_toward_the_top_of_the_board():
    """Decision 494: among equally compared titles the higher tier anchors first. The widest
    posterior used to decide, and on a real board the widest σ belonged to off-scale embeddings at
    the far tails - Grease against Miss Congeniality at s = -17 and -14, which no answer could move
    out of F."""
    spec = [(i, s, 0) for i, s in enumerate((-4.0, -3.0, -1.5, -0.5, 0.5, 1.5, 3.0, 3.5), start=1)]
    candidates = board_of(spec)
    by_id = {c.title_id: c for c in candidates}
    top = max(c.tier for c in candidates)
    for seed in range(200):
        pair = queue._exploration(candidates, random.Random(seed))
        assert by_id[pair.title_a].tier == top, f"seed {seed} anchored on tier {pair.title_a}"


def test_exploration_still_explores_a_board_that_is_all_bottom_tiers():
    """The weighting orders the anchors and removes none. A restricted anchor set is how M4.10's
    finding 12 served one pair 78 times in 109, so a board that is all F and D - somebody who has
    so far rated only what they disliked - must still be explored until its pairs run out."""
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
    """The held-out arm reads neither `asked` nor the window (§13): with every pair answered and
    every title recent, the adaptive arms have nothing, `draw` is None nine times in ten, and the
    tenth is still a uniform held-out pair. Its rate did not move with what the model was told."""
    candidates = pool(n=4, sigma=STRADDLING)
    asked = {frozenset((a, b)) for a in range(1, 5) for b in range(a + 1, 5)}
    rng = random.Random(11)
    outcomes = Counter()
    for _ in range(6_000):
        pair = queue.draw(candidates, rng=rng, asked=asked, recent={1, 2, 3, 4})
        outcomes[None if pair is None else pair.arm] += 1
    assert set(outcomes) == {None, queue.ARM_HOLDOUT}, outcomes
    assert outcomes[queue.ARM_HOLDOUT] / 6_000 == pytest.approx(0.10, abs=0.015)
