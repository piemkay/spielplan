"""§6.2 step 4's adaptive round. Spec v2.1 §6.2 step 4 (rewritten, 54b/54c), §13, §14 risk 6.

Pure and seeded, for the reason `rank/queue.py` is pure and seeded: the round is a function of
a posterior, and "the round stops when the shortlist boundary is resolved" is a claim about
that function which pushing through Postgres would test through a socket.

Four things here are the milestone, and each has a way of going quietly wrong.

  * **Four answers, two of them new.** Decision 154: "`either` lifts both candidates, `neither`
    lowers both." The prototype collected `NO_PULL`, logged it as a `session_answer`, and threw
    it away in the first line of `tilt()`. "Collected but ignored" is the specific regression,
    and it is invisible to any test that only checks the value round-trips to the database.
  * **§13's guard, now binding a stopping rule.** 54b: hold-out pairs are "used for neither
    selection nor stopping". A round that stops when it is confident looks confident whether or
    not it is right, so the guard is checked by *replay*: strip the hold-out answers and the
    round must make the identical choices and stop at the identical point.
  * **Selection by information gain.** "the pair whose answer would most reduce the number of
    titles still straddling" — a rule that is satisfied on most boards by almost any pair, so
    the boards here are built so that one pair is decisively better than the rest.
  * **The cap and the escape are not failure states**, but they must be distinguishable: §14
    risk 6 wants the rate of each, and an implementation that reports `cap` for a converged
    round makes that rate unreadable while every round still ends.
"""

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
    """A board whose candidates are well separated — nothing straddles, so a round over it is
    already converged. σ is small by default so a straddle in a test about something else is
    noise rather than a surprise."""
    return {i + 1: rnd.Belief(mu=1.0 - 0.15 * i, var=var) for i in range(n)}


def arm_key(*, fires=(), quiet=()):
    """A hold-out key whose arm fires exactly where the test asking for it needs it to.

    54b's arm is a rate drawn from a key now, not the tenth slot (decision 223), so `seq=1` no
    longer names the adaptive arm on its own and `seq=10` no longer names the hold-out: the
    question "which arm is this pair" has two inputs and a test about either arm has to name
    both. Searching for a key states that property instead of pinning a magic string, which
    would silently stop meaning what it says the day the draw is re-derived.
    """
    for i in range(100_000):
        key = f"arm-{i}"
        if all(rnd.is_holdout(s, key=key) for s in fires) and not any(
            rnd.is_holdout(s, key=key) for s in quiet
        ):
            return key
    raise AssertionError(f"no key fires at {sorted(fires)} and is quiet at {sorted(quiet)}")


# The key every test about the ADAPTIVE selector below is written with: its arm is quiet for a
# whole round, so the pair those tests get is the one 54c's rule chose and never the uniform draw.
ADAPTIVE_ARM = arm_key(quiet=range(1, rnd.CAP_PAIRS + 1))


# --- decision 154: the four answers, and what each one does ------------------------------


def test_only_the_four_answers_exist():
    assert rnd.ANSWERS == ("A", "B", "EITHER", "NEITHER")
    with pytest.raises(ValueError):
        rnd.update(beliefs(0.5, 0.5), title_a=1, title_b=2, answer="NO_PULL", anchor=0.5)


def test_a_and_b_separate_the_two_posteriors():
    """"`A` and `B` separate two candidates." The winner's mean rises, the loser's falls, and
    the pair is more ordered afterwards than before."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=A, anchor=0.5)

    assert after[1].mu > before[1].mu
    assert after[2].mu < before[2].mu
    assert after[1].mu > after[2].mu

    mirrored = rnd.update(before, title_a=1, title_b=2, answer=B, anchor=0.5)
    assert mirrored[2].mu > mirrored[1].mu


def test_either_raises_both_candidates():
    """Decision 154: "`either` lifts both candidates". Not a shrug and not a no-op: an
    equality constraint that says *both of these would do*, which is information about the
    pool, so both means move UP and neither moves relative to the other."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=EITHER, anchor=0.0)

    assert after[1].mu > before[1].mu
    assert after[2].mu > before[2].mu
    assert after[1].mu == pytest.approx(after[2].mu), "either separates nothing"


def test_neither_lowers_both_candidates():
    """Decision 154: "`neither` is a rejection that lowers both — the strongest signal a
    participant can send about a live pool … it eliminates two candidates at once."""
    before = beliefs(0.50, 0.50)
    after = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.0)

    assert after[1].mu < before[1].mu
    assert after[2].mu < before[2].mu
    assert after[1].mu == pytest.approx(after[2].mu)


def test_either_and_neither_are_opposite_and_neither_is_a_no_op():
    """The regression this row exists for: `NEITHER` stored as `EITHER`, or dropped from the
    update entirely. Both are caught by requiring the two to move the pool in opposite
    directions and both to move it at all."""
    before = beliefs(0.50, 0.50)
    either = rnd.update(before, title_a=1, title_b=2, answer=EITHER, anchor=0.0)
    neither = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.0)

    assert either[1].mu > before[1].mu > neither[1].mu
    assert either[2].mu > before[2].mu > neither[2].mu
    assert either[1].mu != pytest.approx(neither[1].mu)


def test_neither_eliminates_two_candidates_at_once():
    """Why the round wants it: one answer moves two candidates below the shortlist boundary,
    which no A/B answer can do."""
    before = {1: rnd.Belief(0.55, 0.05), 2: rnd.Belief(0.54, 0.05),
              3: rnd.Belief(0.53, 0.05), 4: rnd.Belief(0.52, 0.05), 5: rnd.Belief(0.51, 0.05)}
    after = rnd.update(before, title_a=1, title_b=2, answer=NEITHER, anchor=0.53)

    assert after[1].mu < before[1].mu and after[2].mu < before[2].mu
    # The other three did not move: an answer about two titles is evidence about those two.
    for t in (3, 4, 5):
        assert after[t].mu == pytest.approx(before[t].mu)


def test_an_answer_shrinks_the_uncertainty_of_the_titles_it_names():
    """Every answer is evidence; a posterior that never narrows never converges, and the
    stopping rule would then only ever fire on the cap."""
    before = beliefs(0.5, 0.5, var=1.0)
    for answer in (A, B, EITHER, NEITHER):
        after = rnd.update(before, title_a=1, title_b=2, answer=answer, anchor=0.5)
        assert after[1].var < before[1].var, answer
        assert after[2].var < before[2].var, answer


# --- 54c: the shortlist boundary, and who straddles it -----------------------------------


def test_the_boundary_sits_between_the_third_and_fourth_candidate():
    """54d fixes the shortlist at three finalists, so "the shortlist boundary" is the cut
    between rank 3 and rank 4 — the only boundary the round is trying to resolve."""
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


# --- 54c: stopping, the cap, and the escape ----------------------------------------------


def test_a_round_over_a_resolved_pool_converges_rather_than_running_to_the_cap():
    assert rnd.stop_reason(spread(), answered=1, z=1.0) == rnd.CONVERGED


def test_a_round_that_cannot_resolve_ends_at_the_hard_cap_of_twenty():
    """"subject to a **hard cap of 20 pairs**". The board here never resolves — every
    candidate sits on the boundary with a wide posterior — so the cap is the only exit."""
    stuck = {i: rnd.Belief(0.5, 1.0) for i in range(1, 9)}
    assert rnd.stop_reason(stuck, answered=19, z=1.0) is None
    assert rnd.stop_reason(stuck, answered=20, z=1.0) == rnd.CAP
    assert rnd.CAP_PAIRS == 20


def test_convergence_beats_the_cap_when_both_would_fire():
    """§14 risk 6 wants the rate at which the cap fires. A round that reports `cap` for a
    resolved board makes that rate unreadable while still ending every round."""
    assert rnd.stop_reason(spread(), answered=20, z=1.0) == rnd.CONVERGED


def test_the_escape_is_unavailable_through_pair_five_and_available_from_pair_six():
    """"From the sixth pair a persistent 'just pick for us' ends that person's round." A
    participant answering their Nth pair has answered N-1, so the control appears when five
    are behind them."""
    assert rnd.ESCAPE_FROM_PAIR == 6
    for answered in range(0, 5):
        assert not rnd.escape_available(answered), f"pair {answered + 1} is too early"
    for answered in range(5, 21):
        assert rnd.escape_available(answered)


def test_an_early_escape_is_refused_rather_than_quietly_ignored():
    """"invoking it before pair 6 is rejected". Silently ignoring it would leave a person
    tapping a control that does nothing."""
    with pytest.raises(rnd.EscapeTooEarly):
        rnd.escape(answered=4)
    assert rnd.escape(answered=5) == rnd.ESCAPE


def test_every_round_ends_with_exactly_one_of_the_three_reasons():
    assert set(rnd.END_REASONS) == {rnd.CONVERGED, rnd.CAP, rnd.ESCAPE}


# --- 54c: selection by information gain --------------------------------------------------


def test_the_pair_served_comes_from_the_straddling_set():
    """"**Among candidates whose posterior interval still straddles the shortlist boundary**,
    the round picks the pair…". Asking about a title the round has already placed spends one of
    twenty pairs on a question whose answer changes nothing."""
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
    """"the round picks the pair whose answer would most reduce the number of titles still
    straddling it".

    Stated as a comparison against the *least* informative pair the same board offers, rather
    than as a hard-coded expected winner: the claim in the spec is that the round prefers the
    question that settles more, and a "first two straddlers" or "a random straddling pair"
    implementation fails exactly that comparison.
    """
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
    """"ties are broken toward the pair spanning the widest DNA axis, because a pair of
    near-identical titles teaches nothing about the tilt."

    "Near-identical" is about DNA, not about score — a pair can be perfectly informative about
    the *cut* and still teach nothing about the *tilt*, and the tilt is half of what the round
    is for. So the four straddlers here are identical in the posterior, which makes every pair
    among them exactly equally informative and leaves the axis as the only thing that can
    decide. 3 and 4 span the whole mood axis; 5 and 6 are all but the same film.
    """
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

    # And without the axes it still returns a straddling pair rather than failing: the tie-break
    # is a preference, not a precondition.
    bare = rnd.select(state, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)
    assert {bare.title_a, bare.title_b} <= {3, 4, 5, 6}


def test_selection_never_serves_a_pair_the_participant_has_already_answered():
    """M3-open-points §3.1: the Rank queue's boundary arm re-serves about five distinct pairs
    forever, because nothing consults what was already asked. Ten repeats of one judgement
    shrink that pair's posterior by √10 on the strength of one answer — the reliability
    inflation §13 guards against, arriving by a different door. The round must not repeat it.
    """
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


# --- 54b: §13's held-out arm -------------------------------------------------------------


def test_one_pair_in_ten_is_the_uniform_hold_out_and_it_is_a_rate_not_a_slot():
    """54b: "one pair in ten", which is a RATE and was implemented as `seq % 10 == 0`.

    A slot schedule and a rate agree on the long-run figure and disagree about every round that
    does not reach pair 10 — and 54c's escape from pair 6 is the humane exit, so the evenings a
    household cuts short were exactly the ones contributing nothing to §13's only admissible
    stream. Both halves are asserted here: the rate over a population of seats, and that the
    seqs it fires on are the seat's own rather than everybody's tenth. [finding 30; decision 223]
    """
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
    # The half a schedule cannot have: a round 54c's escape ends at pair 6 carries hold-outs.
    early = [f for f in fired if any(seq <= rnd.ESCAPE_FROM_PAIR - 1 for seq in f)]
    assert len(early) > len(seats) / 4, (
        f"only {len(early)} of {len(seats)} seats draw a hold-out inside an escaped round"
    )


def test_the_arm_is_the_same_answer_in_a_second_process():
    """The property that makes a rate safe to re-derive: the draw is a function of the key and
    the seq and of nothing else in the process asking.

    `api/tonight.py`'s solo route classifies an answer the CLIENT is holding — 54f mints no row,
    so the arm is re-derived on every request from `user.id` and the seq — and the server restarts
    between two of those requests as a matter of course. A draw keyed through `hash()` would be
    salted per process and would classify the same stored answer both ways across a restart,
    putting a hold-out into the adaptive stream and an adaptive answer into §13's. `random.Random`
    seeds a string through sha512, which is what makes this test pass under three hash seeds.
    [decision 223]
    """
    here = [seq for seq in range(1, 41) if rnd.is_holdout(seq, key="seat-19")]
    assert here, "pick a key whose arm fires, or this asserts that nothing equals nothing"
    src = (
        "from spielplan.tonight import round as rnd;"
        "print([s for s in range(1, 41) if rnd.is_holdout(s, key='seat-19')])"
    )
    # PYTHONPATH from this module's own tree rather than inherited alone, which is the pattern
    # `test_static_contracts.py::_imports_fastapi` and `test_boot_logging.py` both use: the venv
    # holds an editable install pointing at the primary checkout, so a worktree's subprocess would
    # otherwise answer about somebody else's `round.py`.
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
    """Not from the straddling set: "a uniform sample restricted to what the model is unsure
    about is not a uniform sample" — the same sentence `rank/queue.py` is built around."""
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
    """`rank/queue.py`'s rule, restated for a round: "the evaluation stream's rate would then
    depend on the model's own confidence, which is precisely the coupling the guard exists to
    break." A pool with nothing left to resolve still serves the pair its arm drew as a hold-out,
    and a pool that cannot draw an adaptive pair never borrows the hold-out arm to fill in."""
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
    """54b, and the reason the guard exists at all: "hold-out pairs are used for neither
    selection nor stopping". Replaying a full round with those answers stripped must produce
    the identical sequence of served pairs and the identical stopping point — anything else
    means the evaluation stream is steering the thing it exists to audit.
    """
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
    """It is a pair the person actually answered, so it costs them one of their twenty — it is
    excluded from the *model*, not from their evening."""
    candidates = {i: 1.0 - 0.02 * i for i in range(1, 13)}
    answers = [
        rnd.Answered(seq=s, title_a=1, title_b=2, answer=A,
                     selection=rnd.SELECTION_HOLDOUT if s % 10 == 0 else rnd.SELECTION_ADAPTIVE)
        for s in range(1, 21)
    ]
    assert rnd.replay(candidates, answers, z=1.0, holdout_key=ADAPTIVE_ARM).answered == 20


# --- 54c: guests -------------------------------------------------------------------------


def test_a_guest_starts_from_the_pool_prior_rather_than_a_borrowed_ledger():
    """54c: "A participant with no Ledger (a guest, a member with too few labels) starts from
    the pool prior and is carried entirely by their answers", and the coverage row: "ranked by
    the candidate pool's own **member-average order**".

    An earlier version of this test asserted the opposite — that the guest's prior is FLAT —
    and the review found both halves of what that costs. It does not rank the pool, which the
    row requires; and with every mean equal, every candidate straddles the shortlist boundary
    at once, so the selection rule searches every pair in the pool instead of the handful the
    boundary separates. The assertion is corrected here rather than loosened: the property the
    row states is stronger than the one it replaced.

    What a guest does NOT get is a member's Ledger under their name — the prototype's `const u
    = guest ? 'p' : who`. The pool average is nobody's scores, which is what the caller hands
    in (`play.Snapshot.member_average`).
    """
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert [t for t, _ in sorted(guest.items(), key=lambda kv: -kv[1].mu)] == [5, 6, 7]
    assert len({b.mu for b in guest.values()}) == 3, "a flat prior ranks nothing"
    assert {t: b.mu for t, b in guest.items()} == {t: b.mu for t, b in member.items()}, (
        "the ORDER is the pool's; only the confidence differs"
    )


def test_a_guest_starts_wider_than_a_member():
    """"which naturally makes a guest's round a little longer" — the only thing the spec says
    about how a guest's round differs, and it follows from knowing nothing about them."""
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert all(guest[t].var > member[t].var for t in pool_order)


def test_a_round_that_runs_out_of_distinct_pairs_ends_rather_than_deadlocking():
    """§6.2 describes the happy path and never a pool small enough to exhaust its own pairs —
    but a household library can be, and `select` refuses to re-serve a pair the participant has
    already answered (M3-open-points §3.1). A round with nothing left to ask and no way to end
    is a deadlock: the person taps and nothing happens, forever.

    It is the same terminal state as the cap — the round ended without resolving the boundary —
    and 54g fixes `ended_by` at three values, so that is what it records. Reported as a v2.2
    spec defect rather than smuggled in as a fourth value.
    """
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
    """`tonight-rank-stopping-and-cap`: "'cap' at the 20th answered pair".

    `select` searches among the straddlers when there are two or more of them, and falls back to
    the whole pool when there are fewer than two. It did not fall back when there were exactly
    two and their one pair had already been answered — it returned None, and `replay` reads a
    None pair as the exhaustion ending. So a forty-title pool ended, recorded as `cap`, at pair
    fourteen: six pairs of the person's budget unspent, two candidates still unplaced, and
    §14 risk 6's rate of each ending reporting `cap` for something that was not the cap.

    A straddler against a well-placed neighbour is still an informative question — that is
    already why the fallback exists at all. Asking it is what the round is for.
    """
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
    """The same claim from the other end, over whole rounds rather than one selection.

    §14 risk 6 reads the rate of each ending, so an ending has to mean what it says: `cap` at
    twenty answered pairs, `converged` when the boundary resolves, and short of either only a
    pool with no distinct pair left to serve. Simulated because the failure was statistical —
    it fired on most seeds and on none of the hand-built boards above.
    """
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
    """The weighting the selection rule multiplies by, on its own.

    `test_the_pair_served_is_the_one_that_resolves_the_most_straddlers` compares the served pair
    against alternatives using `expected_straddlers` — the same function the selector minimises.
    That catches a selector that does not call it and nothing else: a wrong weighting picks a
    different pair and the comparison follows it there. `expected_straddlers` is already a
    direct simulation (it applies the real update for each answer and counts), so the weighting
    is the one part of the rule with no independent check, and it is the part 54c's "would most
    reduce" leans on — "a pair whose answer is a foregone conclusion reduces nothing however
    dramatic the hypothetical would be".
    """
    even = rnd._answer_probabilities(rnd.Belief(1.0, 0.05), rnd.Belief(1.0, 0.05), anchor=1.0)
    assert sum(even.values()) == pytest.approx(1.0)
    assert set(even) == set(rnd.ANSWERS)
    assert even[A] == pytest.approx(even[B]), "a tied pair leans neither way"

    # A pair the model is sure about draws its answer with near-certainty, which is what makes
    # it a bad question: there is almost no mass on the outcome that would change anything.
    sure = rnd._answer_probabilities(rnd.Belief(3.0, 0.01), rnd.Belief(-3.0, 0.01), anchor=0.0)
    unsure = rnd._answer_probabilities(rnd.Belief(0.02, 0.01), rnd.Belief(0.0, 0.01), anchor=0.0)
    assert sure[A] > 0.99
    assert unsure[A] < 0.6, "a coin flip is a coin flip"
    assert max(unsure.values()) < max(sure.values())

    # Decision 154's level split: the same pair, moved across the anchor, swaps which of the
    # two level answers the model expects. An implementation that ignored `neither` would rate
    # a pair of weak candidates as uninformative, when it is the pair one answer eliminates.
    high = rnd._answer_probabilities(rnd.Belief(1.0, 0.25), rnd.Belief(1.0, 0.25), anchor=0.0)
    low = rnd._answer_probabilities(rnd.Belief(-1.0, 0.25), rnd.Belief(-1.0, 0.25), anchor=0.0)
    assert high[EITHER] > high[NEITHER]
    assert low[NEITHER] > low[EITHER]
    assert high[EITHER] == pytest.approx(low[NEITHER]), "symmetric about the anchor"


def test_a_foregone_pair_is_passed_over_for_one_the_round_is_unsure_about():
    """54c's weighting where it bites: "a pair whose answer is a foregone conclusion reduces
    nothing however dramatic the hypothetical would be."

    Four straddlers, alike in variance and all sitting on the boundary, so nothing about the
    board separates them except how predictable each pair's answer is. Predictability is
    measured here as the entropy of the answer distribution — computed in this test, not by the
    function the selector minimises, which is the whole point: the selection test above compares
    the served pair against alternatives using `expected_straddlers` itself, so a wrong
    weighting picks a different pair and the comparison follows it there.

    The tied pair is the foregone one, which is not the obvious way round. Two candidates the
    round cannot order draw a level answer almost surely (decision 154: `either` lifts both,
    `neither` lowers both) — and a level answer says nothing about which side of the cut they
    fall on. The informative question is the one where all four answers are live.
    """
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
    """The failure mode BOUNDARY_Z's value is chosen against, at its limit.

    At z = 0 no posterior reaches the boundary, so nothing straddles, so `stop_reason` converges
    on the empty set: every participant's round ends at pair zero, `ended_by` reads `converged`
    for a shortlist nobody was asked about, and the evening looks like it worked. There is no
    error anywhere — which is why it is asserted rather than reasoned about.

    Until decision 214 the round read §4.3's `straddle_z` out of the bundle, and this test was
    about the bundle validator's refusal of a non-positive one (`break_straddle_z`, refused at
    import). That is now §6.3's business alone: the round's threshold is a module constant no
    bundle can set, and `test_bundle_validation.py` keeps the import-time refusal for the badge.
    What survives here is the *shape* of the failure and the reason the round's own constant is
    nowhere near it: at 0.6 this board has five candidates the round cannot place, and every
    step toward zero takes some of them away.
    """
    board = beliefs(1.0, 0.9, 0.8, 0.7, 0.6, var=0.25)
    assert rnd.BOUNDARY_Z > 0.0, "a non-positive boundary is the bug, not a setting"
    assert rnd.straddlers(board), "at the round's own threshold it has something to ask"

    assert rnd.straddlers(board, z=0.0) == set()
    assert rnd.stop_reason(board, answered=0, z=0.0) == rnd.CONVERGED
    played = rnd.replay({i: 1.0 - 0.1 * i for i in range(5)}, [], z=0.0,
                        holdout_key=ADAPTIVE_ARM)
    assert played.next_pair is None and played.answered == 0

    # And the same ending approached with a positive number. `straddle_z` is read here and
    # nowhere in `round.py`: this is the one place the two constants are allowed to meet, and
    # what is asserted is that they are not interchangeable (decisions 175, 205, 214). Five
    # unplaced candidates at the round's threshold, two at §6.3's badge — on a pool shaped like
    # the shipped one that gap is a resolved shortlist against an evening over at pair two,
    # which `test_the_badge_threshold_and_the_rounds_boundary_cannot_be_one_constant` measures.
    assert len(rnd.straddlers(board, z=DEFAULTS.straddle_z)) < len(rnd.straddlers(board)), (
        "if the badge threshold ever grows past the round's, re-measure BOUNDARY_Z rather than "
        "sharing it again"
    )


# --- decision 214: the boundary is Tonight's own constant ------------------------------------
#
# §6.2 step 4 ends a round "when the shortlist boundary is resolved … subject to a hard cap of 20
# pairs", and quantifies the intent one line earlier: "~10 candidate votes per participant". The
# round borrowed §6.3's `straddle_z` for the multiple, on the argument that "still straddles the
# boundary" is one predicate. It is one predicate over two scales, and at the borrowed value
# `converged` was unreachable: every ending was the cap, so 54c's "adaptive length" was a fixed
# length and §14 risk 6's rate of each ending had one entry. The tests below are the measurement
# decisions 175 and 205 asked for, run on this side of the code rather than in a notebook.


def _simulate(z=None, *, prior_var=1.0, seeds=20, n_pool=40, score_sd=0.5):
    """Whole rounds at the shipped scale, against a noisy oracle.

    The pool means are drawn at the measured owned-pool spread — `user_score` sd 0.50 over the
    696 titles the shipped bundle leaves owned — and the simulated participant answers by their
    own latent order plus a mood-sized wobble, because a perfectly consistent answerer resolves
    any boundary and would make every threshold look reasonable. Returns (converged, median).
    """
    kwargs = {} if z is None else {"z": z}
    reasons: list[str | None] = []
    lengths: list[int] = []
    for seed in range(seeds):
        rng = random.Random(seed)
        truth = {i: rng.gauss(0.0, score_sd) for i in range(n_pool)}
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
    """The wiring half of decision 214: one constant, one owner, no argument from outside.

    `api/tonight.py` used to read the bundle's `straddle_z` per request and pass it down four
    call paths, which is how a badge threshold came to decide how long an evening is. The
    threshold is not a constant of §5.2's recipe, so §4.3 is not where it belongs — the same
    reasoning `CAP_PAIRS` and §6.3's 70/20/10 shares already carry — and with the defaults here
    a caller who says nothing gets the rule the spec describes.
    """
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
    """§6.2 step 4's "~10 candidate votes per participant", as a measurement.

    `tonight-rank-stopping-and-cap` states the requirement — a round ends when the leading
    candidates separate, and *otherwise* at the cap — and the pure tests above assert it on
    hand-built boards where the answer is arranged. This is the same claim on a pool shaped like
    the shipped one, which is the only place the constant's value shows: at the borrowed z = 1.0
    convergence fired 0 times in these 20 rounds and every median was the full 20.
    """
    converged, median = _simulate()

    assert converged >= 12, f"only {converged} of 20 rounds resolved their shortlist"
    assert 5 <= median <= 15, (
        f"median round of {median} pairs is not §6.2's ~10 candidate votes per participant"
    )


def test_the_badge_threshold_and_the_rounds_boundary_cannot_be_one_constant():
    """Why decision 214 is a second constant rather than a retune of the first.

    §6.3 measures a Ledger posterior in logit units against learned cutpoints; §6.2 measures a
    standardised §5.1 score against the midpoint of ranks 3 and 4. Both sentences read "the
    interval still reaches across", and the multiple that satisfies one destroys the other in
    the opposite direction — which is the whole argument, so it is asserted at both ends.
    """
    at_one, median_at_one = _simulate(1.0)
    assert at_one <= 1, f"{at_one} of 20 rounds converged at z = 1.0; `converged` was dead code"
    assert median_at_one == rnd.CAP_PAIRS, "and every round ran the cap out"

    _, median_at_badge = _simulate(DEFAULTS.straddle_z)
    assert median_at_badge <= 3, (
        f"median round of {median_at_badge} pairs at the badge threshold - borrowing §6.3's "
        "retuned constant ends the evening before it starts"
    )


def test_narrowing_the_prior_does_not_make_a_round_converge():
    """Finding 32, refuted rather than acted on (decision 214).

    `M4-open-points §1.1` offers "narrow the member prior" as the remedy for a round that never
    converges, on the reading that `prior_var = 1.0` is four times the measured owned-pool
    variance. It is not a remedy: at the borrowed threshold a quarter of the prior converges
    exactly as often as the full one — zero — and at the round's own threshold it converges
    LESS, because the boundary tightens as fast as the intervals do. `prior_var` stays at 1.0,
    where §5.1's standardisation puts it.
    """
    assert _simulate(1.0, prior_var=0.25)[0] <= 1, "the prior was never the controlling variable"
    assert _simulate(prior_var=0.25)[0] < _simulate()[0], (
        "a narrower prior resolves fewer shortlists, not more"
    )


def test_the_sweep_this_constant_was_calibrated_against_still_reads_this_way():
    """The measurement written beside `BOUNDARY_Z`, held against the harness that produced it.

    The two tests above assert the ARGUMENT — that the threshold is the controlling variable and
    that one multiple cannot calibrate both scales — at whichever parameters make each point, and
    both survive a record that is merely approximate. The paragraph beside the constant is a
    different kind of claim: it quotes ranges over three named pool sizes and closes with "do not
    narrow this without a new measurement", so it is the evidence anyone re-deriving the value
    will run the sweep against. Two of its four ranges did not reproduce (0-1 at z = 1.0 where
    pool 12 gives 2; 14-16 at z = 0.6 where the three pools give 13, 17 and 16), and the prior
    docstring's "from 14/20 to 7/20" is produced by no pool size it names. A reader who cannot
    reproduce a figure cannot tell a stale record from a constant that has drifted under them,
    which in a comment whose numbers ARE the argument is the whole of its usefulness.

    So the comment and this test are one record. Re-tuning the round moves both, deliberately.
    Fifteen simulated sweeps, ~2 s: the cost of a paragraph that can be checked.
    [M4.12 review cycle 1: M412-RND-02]
    """
    pools = (12, 20, 40)
    at_one = [_simulate(1.0, n_pool=n) for n in pools]
    assert all(0 <= c <= 2 for c, _ in at_one), f"z = 1.0 is written as 0-2 in 20: {at_one}"
    assert all(m == rnd.CAP_PAIRS for _, m in at_one), f"and every median as the cap: {at_one}"

    at_boundary = [_simulate(rnd.BOUNDARY_Z, n_pool=n) for n in pools]
    assert all(13 <= c <= 17 for c, _ in at_boundary), (
        f"z = {rnd.BOUNDARY_Z} is written as 13-17 in 20: {at_boundary}"
    )
    assert all(8.5 <= m <= 13 for _, m in at_boundary), (
        f"with a median of 8.5-13 pairs, which is step 4's ~10: {at_boundary}"
    )

    at_badge = [_simulate(DEFAULTS.straddle_z, n_pool=n) for n in pools]
    assert all(c == 20 for c, _ in at_badge), f"the badge threshold: 20 in 20, {at_badge}"
    assert all(1 <= m <= 2 for _, m in at_badge), f"with a median of 1-2 pairs: {at_badge}"

    # `initial`'s own paragraph, which quotes the same sweep at a quarter of the prior.
    narrow_at_one = [_simulate(1.0, n_pool=n, prior_var=0.25)[0] for n in pools]
    assert narrow_at_one == [0, 1, 0], f"written as 0, 1 and 0 over 12/20/40: {narrow_at_one}"
    narrow = [_simulate(rnd.BOUNDARY_Z, n_pool=n, prior_var=0.25)[0] for n in pools]
    assert [c for c, _ in at_boundary] == [17, 13, 16], "the figures the drop is quoted from"
    assert narrow == [13, 9, 10], f"and the narrowed prior drops each of them: {narrow}"


# --- perf-01: the fast search is the same search -------------------------------------------
#
# 54c's rule is `expected_straddlers` above it; `select` evaluates it through `_Board`, which
# does the same arithmetic for every pair at once. Decision 168 ships that rewrite and rules
# out the K-nearest-straddler bound, so the thing to assert is that nothing about the rule
# moved — equality, not a tolerance — and then that the result arrives inside §6's budget.
#
# Every pool in the rest of this file is four to eight titles. The defect these two guards
# exist for is only visible at the real one: 696 owned titles at the default 130-minute budget,
# a measured `user_score` sd of 0.504, and `prior_var` 1.0 for a member or 4.0 for a guest.


def _scalar_select(beliefs_, *, z, axes=None, asked=None):
    """54c's selection written the slow way: `expected_straddlers` for every pair of the same
    three searches, argmin over the same key. This is the reference `select` must reproduce —
    the value equality below is necessary and not sufficient, because a rule change can hide in
    the argmin rather than in the expectation."""
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
    """A board the shape a real one has: §5.1 scores standardised to roughly unit variance, at
    the sd perf-01 measured over the shipped 696-title owned pool.

    `answers` replays that many pairs through `update`, which is the only way to get the mixed
    variances every board past the first answer actually has; `mixed_var` gets there directly.
    """
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


# The board the pre-release review's adversarial pass used to break the first cut of the fast
# evaluator: title 5's `mu - z*sigma` is bit-for-bit the cut the board takes once title 5 moves
# out of the top four. The evaluator counted the whole board through the interval rewrite and
# then removed the two moved titles with `straddles()`' exact expression, so on this board the
# two disagreed by a whole straddler and `select` served (5, 6) where 54c's rule serves (7, 8).
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
    """The regression the adversarial pass found, kept as a board rather than as an argument.

    `_Board` counts the whole board through the interval rewrite `mu - d < cut < mu + d`, which
    means the two titles an answer moves are in that total under their OLD intervals. They have
    to come out the same way they went in; taking them out with `straddles()`' `|mu - cut| < d`
    instead is a different expression, and on this board it costs a whole straddler on five of
    the twenty-eight pairs and changes which pair the round serves.
    """
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
    """Not `approx`. The fast evaluator maintains the count locally instead of rescanning, so
    its only licence is that it computes the same number — and decision 168 measured that it
    does, over twelve pools and two hundred pairs each, at max |difference| 0.0. The awkward
    shapes are here on purpose: four candidates is the smallest board with a boundary at all,
    quantised means put the cut exactly on a candidate, zero variance makes every interval a
    point, and a non-positive z is the one case `straddles` answers False for everything."""
    shapes = [dict(n=n, seed=s) for n in (4, 5, 7, 12, 40, 100) for s in range(4)]
    shapes += [dict(n=60, seed=s, var=4.0) for s in range(3)]           # a guest seat
    shapes += [dict(n=60, seed=s, sd=0.05) for s in range(3)]           # a tight board
    shapes += [dict(n=60, seed=s, quantise=4) for s in range(3)]        # ties on the cut
    shapes += [dict(n=30, seed=s, var=0.0) for s in range(3)]           # no reach at all
    # Every board after the first answer has two variances the rest do not, which a board built
    # from one prior cannot express — so some are built by replaying answers, and some are given
    # a spread of variances outright.
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
    """The module's own performance record, asserted rather than asserted-in-prose.

    The block comment above `_Board` concludes "`_answer_probabilities`' own `p_a` is `Phi(t)`,
    the same value `_v_w` needs. That leaves three `erf` and three `exp` per pair" — and for a
    milestone the code did not do it: `_v_w_many` computed `denom = _Phi_many(t)` and discarded
    it, and `_block` recomputed the identical array as `p_a`. Four erf passes per pair, not three,
    on the one function that is a Python-level loop (`np.frompyfunc(math.erf, ...)` plus an object
    cast) and is therefore the dominant cost of the whole search: one full `_PAIR_BLOCK` measured
    8 ms at four passes against 4 ms at three.

    Nothing a household can see failed — a 700-title guest board selects in 26 ms against §6's
    1.5 s — which is exactly why this is worth a test. A maintainer who re-derives the search's
    cost from that paragraph budgets three quarters of the erf work the code does, and the same
    class of drift is what cycle 1 repaired for `BOUNDARY_Z`. The count is the assertion and the
    sizes are the second half of it: three passes over something other than the pairs would be a
    different function being counted. [M4.12 review cycle 2: M412-RND-03]
    """
    board_beliefs = _random_board(n=40, seed=3, var=4.0)
    pool = sorted(board_beliefs)
    board = rnd._Board.of(
        board_beliefs, pool, anchor=rnd.anchor_of(board_beliefs), z=rnd.BOUNDARY_Z
    )
    ia = np.asarray([0, 1, 2, 3], dtype=np.int64)
    ib = np.asarray([7, 8, 9, 10], dtype=np.int64)

    # Patched AFTER the board is built: `_Board.of` moves every title against the anchor and its
    # two `_v_w_many` calls are per-`select`, not per-pair.
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
    """The whole of `select`, not one expectation: all three searches, the `asked` filter and
    54c's tie-break toward the widest DNA axis. `_axis_span` is 0.0 on release data (decision
    173 ships without axes), so the axes half is exercised with an authored set here or the
    tie-break would go untested in exactly the arm that decides ties."""
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

    # The second and third searches: every pair BETWEEN straddlers already answered, so the
    # round falls through to a straddler against anyone.
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
    """§6 preamble: "<1.5 s per battle". The board is the worst realistic one — a guest of a
    fresh household at the 200-minute budget, 716 candidates with `GUEST_VAR_FACTOR` applied
    and no answers behind them, which is where 574-591 of them straddle. The shipped scalar
    search takes over a minute on it; the budget asserted here is a third of §6's, because a
    GET of the round replays it and a POST replays it twice."""
    board = _random_board(716, 11, var=1.0 * rnd.GUEST_VAR_FACTOR)
    assert len(rnd.straddlers(board, z=1.0)) > 500, "the guard is only a guard on a wide board"
    started = time.perf_counter()
    pair = rnd.select(board, seq=1, rng=random.Random(0), holdout_key=ADAPTIVE_ARM, z=1.0)
    elapsed = time.perf_counter() - started
    assert pair is not None
    assert elapsed < 0.5, f"first pair took {elapsed:.2f} s on a 716-candidate guest board"


def test_the_costlier_fallback_arm_is_inside_the_budget_too():
    """The second search: a straddler against ANYONE, once every pair between straddlers has
    been answered. Decision 168 names it as the arm the guard must exercise, on the ground that
    its m x n = 398,930 pairs cost more than the 164,451 of the search it falls back from.

    Measured, that premise is wrong and the guard is still worth having. Reaching this arm
    requires every within-pair to be blocked, and blocking them removes them from this arm too,
    so what it actually evaluates is the m(n-m) cross pairs — 70,028 on this board, and at most
    n^2/4 = 121,104 for any m. The arm is therefore cheaper than the first search, not dearer.
    What the guard covers is that the fallback is reached at all and returns inside the budget;
    the ceiling on the whole selector is the guard above it."""
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
