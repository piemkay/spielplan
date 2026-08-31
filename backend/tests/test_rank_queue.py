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


def pool(n=60, *, sigma=0.35, seed=2, comparisons=None):
    """A board wide enough that every arm can actually draw: titles spread across the whole
    cutpoint range with a σ that makes many of them straddle, and every tier populated."""
    rng = np.random.default_rng(seed)
    values = np.linspace(float(CUTS[0]) - 1.0, float(CUTS[-1]) + 1.0, n)
    values = values + rng.normal(scale=0.05, size=n)
    items = [
        board.Item(title_id=i + 1, name=f"T{i + 1}", s=float(v), sigma=sigma)
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
    arms = Counter(queue.draw(candidates, rng=rng).arm for _ in range(20_000))

    total = sum(arms.values())
    assert total == 20_000, "every draw returns a pair on a healthy board"
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


def test_the_held_out_arm_never_receives_a_fallback():
    """The rate of the evaluation stream must not depend on the model's own confidence.

    A pool with nothing to sharpen returns nothing rather than manufacturing held-out rows:
    if a failed adaptive draw fell through to the uniform arm, §13's 10% would rise exactly
    when the model was least sure, and the stream would no longer be independent of it."""
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
