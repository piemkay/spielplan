"""Pure and seeded. The hold-out guard is checked by replay: strip the hold-out answers and the
round must choose and stop identically. Boards are built so one pair is decisively best."""

from __future__ import annotations

import inspect
import math
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.tonight import round as rnd

A, B, EITHER, NEITHER = rnd.A, rnd.B, rnd.EITHER, rnd.NEITHER


def beliefs(*mus, var=1.0):
    """A posterior straight from a list of means. Title ids are 1..n."""
    return {i + 1: rnd.Belief(mu=float(m), var=float(var)) for i, m in enumerate(mus)}


def spread(n=8, var=0.0001):
    """Well separated, so nothing straddles; σ is small so a stray straddle is noise."""
    return {i + 1: rnd.Belief(mu=1.0 - 0.15 * i, var=var) for i in range(n)}


def arm_key(*, fires=(), quiet=()):
    """54b's arm is a rate drawn from a key (decision 223), so a test names both seq and key; searching
    for a key states the property instead of pinning a magic string."""
    for i in range(100_000):
        key = f"arm-{i}"
        if all(rnd.is_holdout(s, key=key) for s in fires) and not any(
            rnd.is_holdout(s, key=key) for s in quiet
        ):
            return key
    raise AssertionError(f"no key fires at {sorted(fires)} and is quiet at {sorted(quiet)}")


# Its arm is quiet for a whole round, so these tests get the pair 54c's rule chose.
ADAPTIVE_ARM = arm_key(quiet=range(1, rnd.CAP_PAIRS + 1))


def test_only_the_four_answers_exist():
    assert rnd.ANSWERS == ("A", "B", "EITHER", "NEITHER")
    with pytest.raises(ValueError):
        rnd.update(beliefs(0.5, 0.5), title_a=1, title_b=2, answer="NO_PULL", anchor=0.5)


def test_a_and_b_separate_the_two_posteriors():
    """The winner rises, the loser falls, and the pair is more ordered afterwards."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=A, anchor=0.5)

    assert after[1].mu > before[1].mu
    assert after[2].mu < before[2].mu
    assert after[1].mu > after[2].mu

    mirrored = rnd.update(before, title_a=1, title_b=2, answer=B, anchor=0.5)
    assert mirrored[2].mu > mirrored[1].mu


def test_either_raises_both_candidates():
    """Decision 154: `either` is information about the pool, so both means move UP, neither relative
    to the other."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=EITHER, anchor=0.0)

    assert after[1].mu > before[1].mu
    assert after[2].mu > before[2].mu
    assert after[1].mu == pytest.approx(after[2].mu), "either separates nothing"


def test_neither_lowers_both_candidates():
    """Decision 154: `neither` lowers both."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.0)

    assert after[1].mu < before[1].mu
    assert after[2].mu < before[2].mu
    assert after[1].mu == pytest.approx(after[2].mu)


def test_either_and_neither_are_opposite_and_neither_is_a_no_op():
    """`NEITHER` stored as `EITHER`, or dropped: the two must move the pool in opposite directions."""
    before = beliefs(0.50, 0.50)
    either = rnd.update(before, title_a=1, title_b=2, answer=EITHER, anchor=0.0)
    neither = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.0)

    assert either[1].mu > before[1].mu > neither[1].mu
    assert either[2].mu > before[2].mu > neither[2].mu
    assert either[1].mu != pytest.approx(neither[1].mu)


def test_neither_eliminates_two_candidates_at_once():
    """One answer moves two candidates below the boundary, which no A/B answer can."""
    before = {1: rnd.Belief(0.55, 0.05), 2: rnd.Belief(0.54, 0.05),
              3: rnd.Belief(0.53, 0.05), 4: rnd.Belief(0.52, 0.05), 5: rnd.Belief(0.51, 0.05)}
    after = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.53)

    assert after[1].mu < before[1].mu and after[2].mu < before[2].mu
    # The other three did not move: an answer about two titles is evidence about those two.
    for t in (3, 4, 5):
        assert after[t].mu == pytest.approx(before[t].mu)


def test_an_answer_shrinks_the_uncertainty_of_the_titles_it_names():
    """A posterior that never narrows converges only at the cap."""
    before = beliefs(0.5, 0.5, var=1.0)
    for answer in (A, B, EITHER, NEITHER):
        after = rnd.update(before, title_a=1, title_b=2, answer=answer, anchor=0.5)
        assert after[1].var < before[1].var, answer
        assert after[2].var < before[2].var, answer


def test_the_boundary_sits_between_the_third_and_fourth_candidate():
    """54d fixes three finalists, so the boundary is between ranks 3 and 4."""
    assert rnd.boundary(beliefs(1.0, 0.9, 0.8, 0.4, 0.3)) == pytest.approx(0.6)


def test_a_pool_smaller_than_the_shortlist_has_no_boundary_to_resolve():
    """Three candidates *are* the shortlist. A round over them has nothing to ask."""
    assert rnd.boundary(beliefs(1.0, 0.5, 0.2)) is None
    assert rnd.straddlers(beliefs(1.0, 0.5, 0.2), z=1.0) == set()


def test_a_candidate_straddles_when_its_interval_crosses_the_boundary():
    state = {1: rnd.Belief(1.00, 0.0001), 2: rnd.Belief(0.90, 0.0001),
             3: rnd.Belief(0.62, 0.09), 4: rnd.Belief(0.58, 0.09),
             5: rnd.Belief(0.10, 0.0001)}
    straddling = rnd.straddlers(state, z=1.0)

    assert straddling == {3, 4}, "only the two either side of the cut are unresolved"


def test_a_round_over_a_resolved_pool_converges_rather_than_running_to_the_cap():
    assert rnd.stop_reason(spread(), answered=1, z=1.0) == rnd.CONVERGED


def test_a_round_that_cannot_resolve_ends_at_the_hard_cap_of_twenty():
    """Every candidate sits on the boundary with a wide posterior, so the cap is the only exit."""
    stuck = {i: rnd.Belief(0.5, 1.0) for i in range(1, 9)}
    assert rnd.stop_reason(stuck, answered=19, z=1.0) is None
    assert rnd.stop_reason(stuck, answered=20, z=1.0) == rnd.CAP
    assert rnd.CAP_PAIRS == 20


def test_convergence_beats_the_cap_when_both_would_fire():
    """§14 risk 6 needs the cap's rate, so a resolved board must report `converged`."""
    assert rnd.stop_reason(spread(), answered=20, z=1.0) == rnd.CONVERGED


def test_the_escape_is_unavailable_through_pair_five_and_available_from_pair_six():
    """Answering the Nth pair means N-1 are behind, so the control appears at five."""
    assert rnd.ESCAPE_FROM_PAIR == 6
    for answered in range(0, 5):
        assert not rnd.escape_available(answered), f"pair {answered + 1} is too early"
    for answered in range(5, 21):
        assert rnd.escape_available(answered)


def test_an_early_escape_is_refused_rather_than_quietly_ignored():
    """A silently ignored tap is a control that does nothing."""
    with pytest.raises(rnd.EscapeTooEarly):
        rnd.escape(answered=4)
    assert rnd.escape(answered=5) == rnd.ESCAPE


def test_every_round_ends_with_exactly_one_of_the_three_reasons():
    assert set(rnd.END_REASONS) == {rnd.CONVERGED, rnd.CAP, rnd.ESCAPE}


def test_the_pair_served_comes_from_the_straddling_set():
    """A title already placed wastes one of twenty pairs."""
    state = {
        1: rnd.Belief(1.00, 0.0001),   # safely in
        2: rnd.Belief(0.90, 0.0001),   # safely in
        3: rnd.Belief(0.75, 0.09),
        4: rnd.Belief(0.45, 0.09),
        5: rnd.Belief(0.601, 0.09),
        6: rnd.Belief(0.599, 0.09),
        7: rnd.Belief(0.10, 0.0001),   # safely out
    }
    unresolved = rnd.straddlers(state, z=1.0)
    assert unresolved == {3, 4, 5, 6}, "the fixture is only meaningful while these four straddle"

    pair = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)
    assert pair.selection == rnd.SELECTION_ADAPTIVE
    assert {pair.title_a, pair.title_b} <= unresolved


def test_the_pair_served_is_the_one_that_resolves_the_most_straddlers():
    """Compared against the least informative pair on the board, not a hard-coded winner."""
    state = {
        1: rnd.Belief(1.00, 0.0001), 2: rnd.Belief(0.90, 0.0001),
        3: rnd.Belief(0.75, 0.09), 4: rnd.Belief(0.45, 0.09),
        5: rnd.Belief(0.601, 0.09), 6: rnd.Belief(0.599, 0.09),
        7: rnd.Belief(0.10, 0.0001),
    }
    anchor = rnd.anchor_of(state)
    pair = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)

    served = rnd.expected_straddlers(
        state, title_a=pair.title_a, title_b=pair.title_b, anchor=anchor, z=1.0
    )
    for a, b in ((3, 5), (5, 6), (3, 4), (4, 5)):
        assert served <= rnd.expected_straddlers(
            state, title_a=a, title_b=b, anchor=anchor, z=1.0
        ) + 1e-9, f"({a}, {b}) would have settled more of the shortlist"


def test_a_tie_on_information_breaks_toward_the_widest_dna_axis():
    """The four straddlers are identical in the posterior, so only the DNA axis decides. 3 and 4 span
    the whole mood axis; 5 and 6 are all but the same film."""
    state = {
        1: rnd.Belief(1.00, 0.0001),
        2: rnd.Belief(0.90, 0.0001),
        3: rnd.Belief(0.70, 0.04), 4: rnd.Belief(0.70, 0.04),
        5: rnd.Belief(0.70, 0.04), 6: rnd.Belief(0.70, 0.04),
        7: rnd.Belief(0.05, 0.0001),
    }
    anchor = rnd.anchor_of(state)
    tied = {
        rnd.expected_straddlers(state, title_a=a, title_b=b, anchor=anchor, z=1.0)
        for a, b in ((3, 4), (3, 5), (4, 6), (5, 6))
    }
    assert len(tied) == 1, "the fixture is only meaningful while the pairs are a genuine tie"

    axes = {
        3: {"mood": -1.0}, 4: {"mood": 1.0},     # spans the whole mood axis
        5: {"mood": 0.1}, 6: {"mood": 0.0},      # spans almost none of it
    }
    pair = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0, axes=axes)
    assert {pair.title_a, pair.title_b} == {3, 4}

    # Without axes it still returns a straddling pair: the tie-break is a preference.
    bare = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)
    assert {bare.title_a, bare.title_b} <= {3, 4, 5, 6}


def test_selection_never_serves_a_pair_the_participant_has_already_answered():
    """A repeat is an independent observation of one judgement, inflating reliability (§13)."""
    state = {
        1: rnd.Belief(1.00, 0.0001), 2: rnd.Belief(0.90, 0.0001),
        3: rnd.Belief(0.62, 0.09), 4: rnd.Belief(0.58, 0.09),
        5: rnd.Belief(0.60, 0.09), 6: rnd.Belief(0.59, 0.09),
        7: rnd.Belief(0.05, 0.0001),
    }
    asked = {frozenset({3, 4})}
    pair = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0, asked=asked)
    assert frozenset({pair.title_a, pair.title_b}) not in asked


def test_a_pair_never_names_one_title_twice():
    for seed in range(20):
        pair = rnd.select(
            spread(6, var=0.5), seq=1, rng=random.Random(seed), holdout_key=ADAPTIVE_ARM, z=1.0
        )
        assert pair.title_a != pair.title_b


def test_one_pair_in_ten_is_the_uniform_hold_out_and_it_is_a_rate_not_a_slot():
    """A rate, not `seq % 10 == 0`: a slot schedule gives rounds escaped before pair 10 no hold-outs.
    Asserted over a population of seats, and on each seat's own seqs (decision 223)."""
    assert rnd.HOLDOUT_EVERY == 10
    seats = [str(i) for i in range(2000)]
    fired = [
        [seq for seq in range(1, rnd.CAP_PAIRS + 1) if rnd.is_holdout(seq, key=k)] for k in seats
    ]

    drawn = sum(len(f) for f in fired)
    rate = drawn / (len(seats) * rnd.CAP_PAIRS)
    assert 0.085 < rate < 0.115, f"the arm fired {drawn} times in {len(seats) * rnd.CAP_PAIRS}"

    schedule = {rnd.HOLDOUT_EVERY, 2 * rnd.HOLDOUT_EVERY}
    assert sum(1 for f in fired if set(f) == schedule) < len(seats) / 20, (
        "the arm is still the tenth slot for most seats"
    )
    # A round escaped at pair 6 still carries hold-outs.
    early = [f for f in fired if any(seq <= rnd.ESCAPE_FROM_PAIR - 1 for seq in f)]
    assert len(early) > len(seats) / 4, (
        f"only {len(early)} of {len(seats)} seats draw a hold-out inside an escaped round"
    )


def test_the_arm_is_the_same_answer_in_a_second_process():
    """The draw is a function of key and seq only: solo re-derives it after restarts, and `hash()` is
    salted per process. `random.Random` seeds a string through sha512."""
    here = [seq for seq in range(1, 41) if rnd.is_holdout(seq, key="seat-19")]
    assert here, "pick a key whose arm fires, or this asserts that nothing equals nothing"
    src = (
        "from spielplan.tonight import round as rnd;"
        "print([s for s in range(1, 41) if rnd.is_holdout(s, key='seat-19')])"
    )
    # PYTHONPATH from this module's own tree: the venv's editable install points at the primary
    # checkout, so a worktree's subprocess would import somebody else's `round.py`.
    env = dict(os.environ)
    backend = str(Path(rnd.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join([backend, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    for hash_seed in ("0", "1", "4242"):
        env["PYTHONHASHSEED"] = hash_seed
        out = subprocess.run(
            [sys.executable, "-c", src], capture_output=True, text=True, check=True, env=env,
        )
        assert out.stdout.strip() == str(here), (
            f"PYTHONHASHSEED={hash_seed} draws {out.stdout.strip()}, this process draws {here}"
        )


def test_the_hold_out_pair_is_drawn_uniformly_from_the_whole_pool():
    """A uniform sample restricted to what the model is unsure about is not uniform."""
    state = {
        1: rnd.Belief(1.00, 0.0001), 2: rnd.Belief(0.90, 0.0001),
        3: rnd.Belief(0.62, 0.09), 4: rnd.Belief(0.58, 0.09),
        5: rnd.Belief(0.05, 0.0001), 6: rnd.Belief(0.02, 0.0001),
    }
    seen = set()
    drawn = arm_key(fires=[10])
    for seed in range(400):
        pair = rnd.select(state, seq=10, rng=random.Random(seed), holdout_key=drawn, z=1.0)
        assert pair.selection == rnd.SELECTION_HOLDOUT
        seen |= {pair.title_a, pair.title_b}
    assert seen == set(state), "the hold-out arm must be able to reach every candidate"


def test_the_hold_out_arm_never_receives_a_fallback():
    """The arm's rate must not depend on the model's confidence: no fallback in either direction."""
    resolved = spread()
    quiet = (1, 5, 9, 11)
    key = arm_key(fires=[10], quiet=quiet)
    held = rnd.select(resolved, seq=10, rng=random.Random(0), holdout_key=key, z=1.0)
    assert held.selection == rnd.SELECTION_HOLDOUT
    for seq in quiet:
        pair = rnd.select(resolved, seq=seq, rng=random.Random(0), holdout_key=key, z=1.0)
        assert pair.selection == rnd.SELECTION_ADAPTIVE, (
            "an adaptive slot that cannot find a straddler must still report itself as adaptive"
        )


def test_the_round_replays_identically_with_the_hold_out_answers_removed():
    """Hold-out answers must not steer selection or stopping: the replay is identical without them."""
    candidates = {i: 1.0 - 0.05 * i for i in range(1, 13)}
    answers = [
        rnd.Answered(seq=s, title_a=a, title_b=b, answer=ans, selection=sel)
        for s, (a, b, ans, sel) in enumerate(
            [
                (1, 2, A, rnd.SELECTION_ADAPTIVE),
                (3, 4, B, rnd.SELECTION_ADAPTIVE),
                (5, 6, EITHER, rnd.SELECTION_ADAPTIVE),
                (7, 8, NEITHER, rnd.SELECTION_ADAPTIVE),
                (9, 10, A, rnd.SELECTION_ADAPTIVE),
                (11, 12, B, rnd.SELECTION_ADAPTIVE),
                (2, 3, A, rnd.SELECTION_ADAPTIVE),
                (4, 5, B, rnd.SELECTION_ADAPTIVE),
                (6, 7, A, rnd.SELECTION_ADAPTIVE),
                (1, 12, B, rnd.SELECTION_HOLDOUT),      # the tenth
                (8, 9, A, rnd.SELECTION_ADAPTIVE),
            ],
            start=1,
        )
    ]
    full = rnd.replay(candidates, answers, z=1.0, holdout_key=ADAPTIVE_ARM)
    stripped = rnd.replay(
        candidates, [a for a in answers if a.selection != rnd.SELECTION_HOLDOUT], z=1.0,
        holdout_key=ADAPTIVE_ARM,
    )

    assert full.beliefs == stripped.beliefs, "a hold-out answer moved the posterior"
    assert full.straddlers == stripped.straddlers
    assert full.next_pair == stripped.next_pair, "a hold-out answer changed what is asked next"
    assert full.stop_reason == stripped.stop_reason


def test_a_hold_out_answer_still_counts_toward_the_cap():
    """Excluded from the model, not from the evening: it still costs one of twenty."""
    candidates = {i: 1.0 - 0.02 * i for i in range(1, 13)}
    answers = [
        rnd.Answered(seq=s, title_a=1, title_b=2, answer=A,
                     selection=rnd.SELECTION_HOLDOUT if s % 10 == 0 else rnd.SELECTION_ADAPTIVE)
        for s in range(1, 21)
    ]
    assert rnd.replay(candidates, answers, z=1.0, holdout_key=ADAPTIVE_ARM).answered == 20


def test_a_guest_starts_from_the_pool_prior_rather_than_a_borrowed_ledger():
    """54c: a guest starts from the pool's member-average order, not a flat prior (which makes every
    candidate straddle) and not a member's Ledger."""
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert [t for t, _ in sorted(guest.items(), key=lambda kv: -kv[1].mu)] == [5, 6, 7]
    assert len({b.mu for b in guest.values()}) == 3, "a flat prior ranks nothing"
    assert {t: b.mu for t, b in guest.items()} == {t: b.mu for t, b in member.items()}, (
        "the ORDER is the pool's; only the confidence differs"
    )


def test_a_guest_starts_wider_than_a_member():
    """Knowing nothing makes a guest's round a little longer."""
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert all(guest[t].var > member[t].var for t in pool_order)


def test_a_round_that_runs_out_of_distinct_pairs_ends_rather_than_deadlocking():
    """A pool that exhausts its pairs must end; it records `cap`, since 54g fixes three values."""
    four = {i: 0.5 for i in range(1, 5)}
    answers = [
        rnd.Answered(seq=s, title_a=a, title_b=b, answer=EITHER)
        for s, (a, b) in enumerate(
            [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)], start=1
        )
    ]
    played = rnd.replay(four, answers, z=1.0, holdout_key=ADAPTIVE_ARM)

    assert played.next_pair is None
    assert played.stop_reason == rnd.CAP, "a round that cannot ask must still end"
    assert played.answered == 6, "and it ends short of the twenty-pair cap"


def test_selection_falls_back_to_the_pool_when_the_straddlers_own_pair_is_spent():
    """Two straddlers whose one pair is answered must fall back to the pool, not end as a false `cap`."""
    board = {
        1: rnd.Belief(mu=2.00, var=0.0001),
        2: rnd.Belief(mu=1.80, var=0.0001),
        3: rnd.Belief(mu=1.00, var=0.2500),
        4: rnd.Belief(mu=0.95, var=0.2500),
        5: rnd.Belief(mu=0.20, var=0.0001),
        6: rnd.Belief(mu=0.10, var=0.0001),
    }
    assert rnd.straddlers(board, z=1.0) == {3, 4}, "the board this test is about"

    spent = {frozenset({3, 4})}
    pair = rnd.select(board, seq=2, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0, asked=spent)

    assert pair is not None, "the round had nineteen pairs of budget and something left to ask"
    assert frozenset({pair.title_a, pair.title_b}) not in spent
    assert {pair.title_a, pair.title_b} & {3, 4}, "and it is still about the unresolved pair"
    assert pair.selection == rnd.SELECTION_ADAPTIVE


def test_the_cap_is_the_only_ending_that_fires_short_of_a_spent_pool():
    """Simulated: the failure was statistical and hit no hand-built board."""
    for seed in range(40):
        rng = random.Random(seed)
        pool = {i: rng.gauss(0.0, 1.0) for i in range(30)}
        answers: list[rnd.Answered] = []
        for seq in range(1, rnd.CAP_PAIRS + 1):
            played = rnd.replay(
                pool, answers, z=1.0, rng=random.Random(seed), holdout_key=ADAPTIVE_ARM
            )
            if played.stop_reason is not None:
                break
            pair = played.next_pair
            assert pair is not None
            answers.append(
                rnd.Answered(
                    seq=seq, title_a=pair.title_a, title_b=pair.title_b,
                    answer=A if pool[pair.title_a] > pool[pair.title_b] else B,
                    selection=pair.selection,
                )
            )
        else:
            played = rnd.replay(
                pool, answers, z=1.0, rng=random.Random(seed), holdout_key=ADAPTIVE_ARM
            )

        assert played.stop_reason in (rnd.CAP, rnd.CONVERGED)
        if played.stop_reason == rnd.CAP and played.answered < rnd.CAP_PAIRS:
            distinct = {frozenset({x.title_a, x.title_b}) for x in answers}
            possible = len(pool) * (len(pool) - 1) // 2
            assert len(distinct) == possible, (
                f"seed {seed}: ended as `cap` at {played.answered} pairs with "
                f"{len(distinct)} of {possible} distinct pairs asked"
            )


def test_the_selection_weighting_is_a_distribution_over_the_four_answers():
    """The weighting is the one part of the rule with no independent check: the selection test
    compares pairs through `expected_straddlers` itself."""
    even = rnd._answer_probabilities(rnd.Belief(1.0, 0.05), rnd.Belief(1.0, 0.05), anchor=1.0)
    assert sum(even.values()) == pytest.approx(1.0)
    assert set(even) == set(rnd.ANSWERS)
    assert even[A] == pytest.approx(even[B]), "a tied pair leans neither way"

    # A foregone pair has almost no mass on an outcome that would change anything.
    sure = rnd._answer_probabilities(rnd.Belief(3.0, 0.01), rnd.Belief(-3.0, 0.01), anchor=0.0)
    unsure = rnd._answer_probabilities(rnd.Belief(0.02, 0.01), rnd.Belief(0.0, 0.01), anchor=0.0)
    assert sure[A] > 0.99
    assert unsure[A] < 0.6, "a coin flip is a coin flip"
    assert max(unsure.values()) < max(sure.values())

    # Decision 154's level split swaps across the anchor; ignoring `neither` would undervalue a weak pair.
    high = rnd._answer_probabilities(rnd.Belief(1.0, 0.25), rnd.Belief(1.0, 0.25), anchor=0.0)
    low = rnd._answer_probabilities(rnd.Belief(-1.0, 0.25), rnd.Belief(-1.0, 0.25), anchor=0.0)
    assert high[EITHER] > high[NEITHER]
    assert low[NEITHER] > low[EITHER]
    assert high[EITHER] == pytest.approx(low[NEITHER]), "symmetric about the anchor"


def test_a_foregone_pair_is_passed_over_for_one_the_round_is_unsure_about():
    """Predictability is the answer distribution's entropy, computed here, not by the selector. The
    tied pair is the foregone one: it draws a level answer, which places neither side of the cut."""
    state = {
        1: rnd.Belief(3.0, 0.0001), 2: rnd.Belief(2.9, 0.0001), 3: rnd.Belief(2.0, 0.0001),
        10: rnd.Belief(1.90, 0.25), 11: rnd.Belief(1.89, 0.25),
        12: rnd.Belief(1.95, 0.25), 13: rnd.Belief(1.55, 0.25),
        20: rnd.Belief(-1.0, 0.0001),
    }
    assert rnd.straddlers(state, z=1.0) == {10, 11, 12, 13}, "the board this test is about"
    anchor = rnd.anchor_of(state)

    def entropy(a: int, b: int) -> float:
        probs = rnd._answer_probabilities(state[a], state[b], anchor)
        return -sum(p * math.log(p) for p in probs.values() if p > 0)

    pairs = [(10, 11), (10, 12), (10, 13), (11, 12), (11, 13), (12, 13)]
    foregone = min(pairs, key=lambda ab: entropy(*ab))
    assert foregone == (10, 11), "the board is built wrong"

    pair = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)

    assert {pair.title_a, pair.title_b} != set(foregone), (
        "the round asked the question whose answer it could already name"
    )
    assert entropy(*sorted((pair.title_a, pair.title_b))) == pytest.approx(
        max(entropy(*ab) for ab in pairs)
    ), "and it asked the one it was least able to predict"


def test_a_non_positive_straddle_threshold_would_end_every_round_before_it_started():
    """At z = 0 nothing straddles and every round "converges" at pair zero with no error anywhere.
    The round's own 0.6 leaves five unplaced candidates on this board."""
    board = beliefs(1.0, 0.9, 0.8, 0.7, 0.6, var=0.25)
    assert rnd.BOUNDARY_Z > 0.0, "a non-positive boundary is the bug, not a setting"
    assert rnd.straddlers(board), "at the round's own threshold it has something to ask"

    assert rnd.straddlers(board, z=0.0) == set()
    assert rnd.stop_reason(board, answered=0, z=0.0) == rnd.CONVERGED
    played = rnd.replay({i: 1.0 - 0.1 * i for i in range(5)}, [], z=0.0,
                        holdout_key=ADAPTIVE_ARM)
    assert played.next_pair is None and played.answered == 0

    # The one place the two constants meet: they are not interchangeable (decisions 175, 205, 214).
    assert len(rnd.straddlers(board, z=DEFAULTS.straddle_z)) < len(rnd.straddlers(board)), (
        "if the badge threshold ever grows past the round's, re-measure BOUNDARY_Z rather than "
        "sharing it again"
    )


# Decision 214: at §6.3's borrowed `straddle_z` every round ended at the cap. These tests are the
# measurement decisions 175 and 205 asked for.


def _heavy_tailed(rng, n_pool):
    """Shaped like the first household evening's raw §5.1 scores: a body of sd ~0.45 and a few
    blockbusters in the tens (decision 477)."""
    raw = {i: rng.gauss(0.0, 0.45) for i in range(n_pool)}
    for i, lift in enumerate((13.28, 9.43, 6.52, 5.82, 4.1, 3.6, 3.2, 2.9)):
        raw[i] = lift
    return raw


def _simulate(z=None, *, prior_var=1.0, seeds=20, n_pool=700, score_sd=1.0,
              standardise=True, draw=None):
    """Handed over through `pool.rank_normal` at 700 titles, as `play.Snapshot` does; `standardise=False`
    is a pre-marker room. The answerer wobbles, or every threshold would look fine.
    Returns (converged, median)."""
    from spielplan.tonight import pool as pool_rules

    kwargs = {} if z is None else {"z": z}
    reasons: list[str | None] = []
    lengths: list[int] = []
    for seed in range(seeds):
        rng = random.Random(seed)
        raw = (draw or (lambda r, n: {i: r.gauss(0.0, score_sd) for i in range(n)}))(rng, n_pool)
        truth = pool_rules.rank_normal(raw) if standardise else raw
        answers: list[rnd.Answered] = []
        for seq in range(1, rnd.CAP_PAIRS + 1):
            played = rnd.replay(
                truth, answers, prior_var=prior_var, rng=random.Random(seed),
                holdout_key=ADAPTIVE_ARM, **kwargs
            )
            if played.stop_reason is not None:
                break
            pair = played.next_pair
            assert pair is not None
            prefers_a = (
                truth[pair.title_a] + rng.gauss(0.0, 0.3)
                > truth[pair.title_b] + rng.gauss(0.0, 0.3)
            )
            answers.append(
                rnd.Answered(
                    seq=seq, title_a=pair.title_a, title_b=pair.title_b,
                    answer=A if prefers_a else B, selection=pair.selection,
                )
            )
        else:
            played = rnd.replay(
                truth, answers, prior_var=prior_var, rng=random.Random(seed),
                holdout_key=ADAPTIVE_ARM, **kwargs
            )
        reasons.append(played.stop_reason)
        lengths.append(played.answered)
    return sum(1 for r in reasons if r == rnd.CONVERGED), statistics.median(lengths)


def test_the_round_reads_its_own_boundary_and_no_route_hands_it_one():
    """Decision 214: one constant, one owner, no argument from outside."""
    from spielplan.api import tonight as tonight_routes

    assert rnd.BOUNDARY_Z == 0.6
    assert "BOUNDARY_Z" in rnd.__all__, "the round's constants are its public surface"
    for fn in (rnd.straddles, rnd.straddlers, rnd.stop_reason, rnd.select, rnd.replay):
        assert inspect.signature(fn).parameters["z"].default == rnd.BOUNDARY_Z, fn.__name__

    assert not hasattr(tonight_routes, "_z"), "the per-request threshold reader is gone"
    assert not hasattr(tonight_routes, "hyperparams"), (
        "a route reading §4.3's constants to decide how long an evening is is the defect "
        "decision 214 closes"
    )
    assert "z=round_rules.BOUNDARY_Z" in inspect.getsource(tonight_routes), (
        "the routes hand `play` the round's own constant and choose nothing"
    )


def test_a_round_over_a_realistic_pool_converges_rather_than_always_reaching_the_cap():
    """At the borrowed z = 1.0 convergence fired 0 of 20 times."""
    converged, median = _simulate()

    assert converged >= 12, f"only {converged} of 20 rounds resolved their shortlist"
    assert 5 <= median <= 15, (
        f"median round of {median} pairs is not §6.2's ~10 candidate votes per participant"
    )


def test_the_badge_threshold_and_the_rounds_boundary_cannot_be_one_constant():
    """§6.3 measures logit posteriors against cutpoints, §6.2 standardised scores against a rank
    midpoint; one multiple cannot serve both, asserted at both ends."""
    at_one, median_at_one = _simulate(1.0)
    assert at_one <= 1, f"{at_one} of 20 rounds converged at z = 1.0; `converged` was dead code"
    assert median_at_one == rnd.CAP_PAIRS, "and every round ran the cap out"

    _, median_at_badge = _simulate(DEFAULTS.straddle_z)
    assert median_at_badge <= 3, (
        f"median round of {median_at_badge} pairs at the badge threshold - borrowing §6.3's "
        "retuned constant ends the evening before it starts"
    )


def test_narrowing_the_prior_does_not_make_a_round_converge():
    """Finding 32 refuted: narrowing the prior does not help, and at the round's threshold hurts."""
    assert _simulate(1.0, prior_var=0.25)[0] <= 1, "the prior was never the controlling variable"
    assert _simulate(prior_var=0.25)[0] < _simulate()[0], (
        "a narrower prior resolves fewer shortlists, not more"
    )


def test_the_sweep_this_constant_was_calibrated_against_still_reads_this_way():
    """The sweep `BOUNDARY_Z` was calibrated against; re-tuning moves this test. Pools of 120, 300 and
    700 on the rank-standardised scale (decision 477). `TYPICAL_PAIRS` is the film night's median."""
    pools = (120, 300, 700)
    at_one = [_simulate(1.0, n_pool=n) for n in pools]
    assert all(0 <= c <= 3 for c, _ in at_one), f"z = 1.0 is written as 0-3 in 20: {at_one}"
    assert all(m == rnd.CAP_PAIRS for _, m in at_one), f"and every median as the cap: {at_one}"

    at_boundary = [_simulate(rnd.BOUNDARY_Z, n_pool=n) for n in pools]
    assert [c for c, _ in at_boundary] == [17, 13, 19], (
        f"z = {rnd.BOUNDARY_Z} is written as 17, 13 and 19 in 20: {at_boundary}"
    )
    assert [m for _, m in at_boundary] == [9.0, 11.0, 10.0], (
        f"with medians of 9, 11 and 10 pairs, which is step 4's ~10: {at_boundary}"
    )
    assert round(at_boundary[-1][1]) == rnd.TYPICAL_PAIRS, (
        "the waiting line's estimate is the film night's median, read off this same sweep"
    )

    at_badge = [_simulate(DEFAULTS.straddle_z, n_pool=n) for n in pools]
    assert all(c == 20 for c, _ in at_badge), f"the badge threshold: 20 in 20, {at_badge}"
    assert all(m == 1 for _, m in at_badge), f"with a median of 1 pair: {at_badge}"

    # `initial`'s own paragraph quotes this sweep at a quarter of the prior.
    narrow_at_one = [_simulate(1.0, n_pool=n, prior_var=0.25)[0] for n in pools]
    assert narrow_at_one == [0, 0, 0], f"written as 0, 0 and 0 over 120/300/700: {narrow_at_one}"
    narrow = [_simulate(rnd.BOUNDARY_Z, n_pool=n, prior_var=0.25)[0] for n in pools]
    assert narrow == [11, 15, 13], (
        f"and at 0.6 the narrowed prior is lower on two pools and higher on one: {narrow}"
    )


def test_a_heavy_tailed_ledger_does_not_end_the_round_at_one_pair():
    """Raw, a heavy-tailed Ledger converged at pair one; standardised it asks about ten. The raw half is
    what a pre-marker room still reads."""
    raw_converged, raw_median = _simulate(draw=_heavy_tailed, standardise=False)
    assert raw_median == 1, "the defect this decision closes, reproduced"
    converged, median = _simulate(draw=_heavy_tailed)
    assert median > rnd.ESCAPE_FROM_PAIR, (
        f"a standardised heavy-tailed pool ends at a median of {median} pairs"
    )
    assert converged >= 12, f"and still resolves its shortlist ({converged} of 20)"


# `select` is the same rule as `expected_straddlers`, evaluated in bulk: equality, not tolerance.
# The defect is only visible at the real size: 696 titles, sd 0.504.


def _scalar_select(beliefs_, *, z, axes=None, asked=None):
    """The reference `select` must reproduce; a rule change can hide in the argmin."""
    pool = sorted(beliefs_)
    already = set(asked or ())
    anchor = rnd.anchor_of(beliefs_)
    unresolved = sorted(rnd.straddlers(beliefs_, z=z))

    def best(pairs):
        out = None
        for a, b in pairs:
            if frozenset({a, b}) in already:
                continue
            expected = rnd.expected_straddlers(
                beliefs_, title_a=a, title_b=b, anchor=anchor, z=z
            )
            key = (round(expected, 9), -rnd._axis_span(axes, a, b), a, b)
            if out is None or key < out:
                out = key
        return out

    def within(xs):
        return ((xs[i], xs[j]) for i in range(len(xs)) for j in range(i + 1, len(xs)))

    def touching(xs):
        return ((min(x, o), max(x, o)) for x in xs for o in pool if o != x)

    found = best(within(unresolved)) if len(unresolved) >= 2 else None
    if found is None and unresolved:
        found = best(touching(unresolved))
    if found is None:
        found = best(within(pool))
    return None if found is None else (found[2], found[3])


def _random_board(n, seed, *, sd=0.504, var=1.0, quantise=None, mixed_var=False, answers=0):
    """At perf-01's measured sd. `answers` replays pairs for mixed variances; `mixed_var` sets them."""
    rng = random.Random(seed)
    mus = [rng.gauss(0.0, sd) for _ in range(n)]
    if quantise:                      # coarse means, so the boundary lands ON a candidate
        mus = [round(m * quantise) / quantise for m in mus]
    board = {
        i + 1: rnd.Belief(mu=m, var=var * (rng.uniform(0.25, 4.0) if mixed_var else 1.0))
        for i, m in enumerate(mus)
    }
    for k in range(answers):
        a, b = rng.sample(sorted(board), 2)
        board = rnd.update(
            board, title_a=a, title_b=b,
            answer=rnd.ANSWERS[k % len(rnd.ANSWERS)], anchor=rnd.anchor_of(board),
        )
    return board


# Title 5's `mu - z*sigma` equals the cut once it leaves the top four; the first fast evaluator
# served (5, 6) here where the rule serves (7, 8).
_ENDPOINT_IS_THE_CUT = {
    1: rnd.Belief(mu=1.5619113767768298, var=1.0),
    2: rnd.Belief(mu=1.5525702385052618, var=1.0),
    3: rnd.Belief(mu=-1.1996394974254523, var=1.0),
    4: rnd.Belief(mu=-1.2725001850735600, var=1.0),
    5: rnd.Belief(mu=-0.5060783124024407, var=0.5328876321884761),
    6: rnd.Belief(mu=-1.3144645816104796, var=1.0),
    7: rnd.Belief(mu=-1.3742132546834656, var=1.0),
    8: rnd.Belief(mu=-1.4838865572118705, var=1.0),
}


def test_a_moved_titles_own_interval_endpoint_landing_on_the_cut_changes_nothing():
    """Moved titles must come out of the count by the expression they went in with."""
    board = _ENDPOINT_IS_THE_CUT
    pool = sorted(board)
    anchor = rnd.anchor_of(board)
    assert (board[5].mu - math.sqrt(board[5].var)) == (board[3].mu + board[4].mu) / 2.0

    fast = rnd._Board.of(board, pool, anchor=anchor, z=1.0)
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            got = float(fast.expected(
                np.asarray([i], dtype=np.int64), np.asarray([j], dtype=np.int64)
            )[0])
            want = rnd.expected_straddlers(
                board, title_a=pool[i], title_b=pool[j], anchor=anchor, z=1.0
            )
            assert got == want, (pool[i], pool[j], got, want)

    assert _pair_of(rnd.select(board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)) == \
        _scalar_select(board, z=1.0)


def test_the_fast_pair_evaluator_reproduces_the_scalar_expectation_exactly():
    """Not `approx`. Four candidates, quantised means, zero variance and non-positive z are the
    awkward shapes."""
    shapes = [dict(n=n, seed=s) for n in (4, 5, 7, 12, 40, 100) for s in range(4)]
    shapes += [dict(n=60, seed=s, var=4.0) for s in range(3)]           # a guest seat
    shapes += [dict(n=60, seed=s, sd=0.05) for s in range(3)]           # a tight board
    shapes += [dict(n=60, seed=s, quantise=4) for s in range(3)]        # ties on the cut
    shapes += [dict(n=30, seed=s, var=0.0) for s in range(3)]           # no reach at all
    # Boards past the first answer have mixed variances, so some replay answers.
    shapes += [dict(n=40, seed=s, answers=k) for s in range(3) for k in (1, 3, 8)]
    shapes += [dict(n=40, seed=s, mixed_var=True) for s in range(3)]

    for shape in shapes:
        for z in (1.0, 0.6, 0.0, -1.0):
            board_beliefs = _random_board(**shape)
            pool = sorted(board_beliefs)
            anchor = rnd.anchor_of(board_beliefs)
            board = rnd._Board.of(board_beliefs, pool, anchor=anchor, z=z)
            rng = random.Random(shape["seed"] * 31 + 7)
            picks = {
                (min(i, j), max(i, j))
                for i, j in ((rng.randrange(len(pool)), rng.randrange(len(pool)))
                             for _ in range(60))
                if i != j
            }
            picks = sorted(picks)
            fast = board.expected(
                np.asarray([i for i, _ in picks], dtype=np.int64),
                np.asarray([j for _, j in picks], dtype=np.int64),
            )
            for (i, j), got in zip(picks, fast, strict=True):
                want = rnd.expected_straddlers(
                    board_beliefs, title_a=pool[i], title_b=pool[j], anchor=anchor, z=z
                )
                assert float(got) == want, (shape, z, pool[i], pool[j], float(got), want)


def test_the_pair_search_evaluates_the_error_function_three_times_per_pair(monkeypatch):
    """Three erf passes per pair, as the comment above `_Board` says; the sizes prove they are per pair."""
    board_beliefs = _random_board(n=40, seed=3, var=4.0)
    pool = sorted(board_beliefs)
    board = rnd._Board.of(
        board_beliefs, pool, anchor=rnd.anchor_of(board_beliefs), z=rnd.BOUNDARY_Z
    )
    ia = np.asarray([0, 1, 2, 3], dtype=np.int64)
    ib = np.asarray([7, 8, 9, 10], dtype=np.int64)

    # Patched after the board is built: `_Board.of` makes two per-select calls.
    sizes: list[int] = []
    real = rnd._Phi_many

    def counted(x):
        sizes.append(int(np.asarray(x).size))
        return real(x)

    monkeypatch.setattr(rnd, "_Phi_many", counted)

    out = board._block(ia, ib)

    assert out.shape == (ia.size,), "the block still evaluates every pair it was given"
    assert len(sizes) == 3, (
        f"{len(sizes)} erf passes per pair, and the comment above `_Board` says three: "
        f"`_v_w_many`'s denominator is `Phi(t)` and `_block` recomputes it as `p_a`"
    )
    assert sizes == [ia.size] * 3, f"a pass over something other than the pairs: {sizes}"


def test_the_fast_pair_search_serves_the_pair_the_scalar_argmin_serves():
    """`_axis_span` is 0.0 on release data (decision 173), so authored axes exercise the tie-break."""
    axes = {i + 1: {"pace": (i % 5) / 4.0, "warmth": (i % 3) / 2.0} for i in range(60)}
    for n in (4, 6, 9, 20, 45):
        for seed in range(4):
            for z in (1.0, 0.6, 0.15):
                board = _random_board(n, seed)
                got = rnd.select(board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=z)
                assert _pair_of(got) == _scalar_select(board, z=z), (n, seed, z)
                got = rnd.select(
                    board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=z, axes=axes
                )
                assert _pair_of(got) == _scalar_select(board, z=z, axes=axes), (n, seed, z)

    # Every pair between straddlers answered, so the round falls through.
    for n in (6, 9, 20):
        for seed in range(3):
            board = _random_board(n, seed)
            unresolved = sorted(rnd.straddlers(board, z=1.0))
            asked = {
                frozenset({unresolved[i], unresolved[j]})
                for i in range(len(unresolved))
                for j in range(i + 1, len(unresolved))
            }
            got = rnd.select(
                board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0, asked=asked
            )
            assert _pair_of(got) == _scalar_select(board, z=1.0, asked=asked), (n, seed)


def _pair_of(pair):
    return None if pair is None else (pair.title_a, pair.title_b)


def test_a_guest_seat_gets_its_first_pair_inside_the_battle_budget():
    """A fresh household's guest at 200 min: 716 candidates, most straddling. A third of §6's budget,
    since a POST replays twice."""
    board = _random_board(716, 11, var=1.0 * rnd.GUEST_VAR_FACTOR)
    assert len(rnd.straddlers(board, z=1.0)) > 500, "the guard is only a guard on a wide board"
    started = time.perf_counter()
    pair = rnd.select(board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)
    elapsed = time.perf_counter() - started
    assert pair is not None
    assert elapsed < 0.5, f"first pair took {elapsed:.2f} s on a 716-candidate guest board"


def test_the_costlier_fallback_arm_is_inside_the_budget_too():
    """The fallback reaches only m(n-m) cross pairs, so it is cheaper; the guard is that it is reached."""
    board = _random_board(696, 11, var=1.0 * rnd.GUEST_VAR_FACTOR)
    unresolved = sorted(rnd.straddlers(board, z=1.0))
    asked = {
        frozenset({unresolved[i], unresolved[j]})
        for i in range(len(unresolved))
        for j in range(i + 1, len(unresolved))
    }
    started = time.perf_counter()
    pair = rnd.select(board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0, asked=asked)
    elapsed = time.perf_counter() - started
    assert pair is not None and frozenset({pair.title_a, pair.title_b}) not in asked
    assert elapsed < 0.5, f"the fallback arm took {elapsed:.2f} s"
