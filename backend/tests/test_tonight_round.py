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

import math
import random

import pytest

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

    pair = rnd.select(state, seq=1, rng=random.Random(0), z=1.0)
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
    pair = rnd.select(state, seq=1, rng=random.Random(0), z=1.0)

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
    pair = rnd.select(state, seq=1, rng=random.Random(0), z=1.0, axes=axes)
    assert {pair.title_a, pair.title_b} == {3, 4}

    # And without the axes it still returns a straddling pair rather than failing: the tie-break
    # is a preference, not a precondition.
    bare = rnd.select(state, seq=1, rng=random.Random(0), z=1.0)
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
    pair = rnd.select(state, seq=1, rng=random.Random(0), z=1.0, asked=asked)
    assert frozenset({pair.title_a, pair.title_b}) not in asked


def test_a_pair_never_names_one_title_twice():
    for seed in range(20):
        pair = rnd.select(spread(6, var=0.5), seq=1, rng=random.Random(seed), z=1.0)
        assert pair.title_a != pair.title_b


# --- 54b: §13's held-out arm -------------------------------------------------------------


def test_every_tenth_pair_is_the_uniform_hold_out():
    """54c: "Every tenth pair is the uniform-random hold-out and is chosen by none of this"."""
    assert rnd.HOLDOUT_EVERY == 10
    served = [rnd.is_holdout(seq) for seq in range(1, 21)]
    assert served.count(True) == 2
    assert [i + 1 for i, h in enumerate(served) if h] == [10, 20]


def test_the_hold_out_pair_is_drawn_uniformly_from_the_whole_pool():
    """Not from the straddling set: "a uniform sample restricted to what the model is unsure
    about is not a uniform sample" — the same sentence `rank/queue.py` is built around."""
    state = {
        1: rnd.Belief(1.00, 0.0001), 2: rnd.Belief(0.90, 0.0001),
        3: rnd.Belief(0.62, 0.09), 4: rnd.Belief(0.58, 0.09),
        5: rnd.Belief(0.05, 0.0001), 6: rnd.Belief(0.02, 0.0001),
    }
    seen = set()
    for seed in range(400):
        pair = rnd.select(state, seq=10, rng=random.Random(seed), z=1.0)
        assert pair.selection == rnd.SELECTION_HOLDOUT
        seen |= {pair.title_a, pair.title_b}
    assert seen == set(state), "the hold-out arm must be able to reach every candidate"


def test_the_hold_out_arm_never_receives_a_fallback():
    """`rank/queue.py`'s rule, restated for a round: "the evaluation stream's rate would then
    depend on the model's own confidence, which is precisely the coupling the guard exists to
    break." A pool with nothing left to resolve still serves its tenth pair as a hold-out, and
    a pool that cannot draw an adaptive pair never borrows the hold-out arm to fill in."""
    resolved = spread()
    assert rnd.select(resolved, seq=10, rng=random.Random(0), z=1.0).selection == rnd.SELECTION_HOLDOUT
    for seq in (1, 5, 9, 11):
        pair = rnd.select(resolved, seq=seq, rng=random.Random(0), z=1.0)
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
    full = rnd.replay(candidates, answers, z=1.0)
    stripped = rnd.replay(
        candidates, [a for a in answers if a.selection != rnd.SELECTION_HOLDOUT], z=1.0
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
    assert rnd.replay(candidates, answers, z=1.0).answered == 20


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
    played = rnd.replay(four, answers, z=1.0)

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
    pair = rnd.select(board, seq=2, rng=random.Random(0), z=1.0, asked=spent)

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
            played = rnd.replay(pool, answers, z=1.0, rng=random.Random(seed))
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
            played = rnd.replay(pool, answers, z=1.0, rng=random.Random(seed))

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

    pair = rnd.select(state, seq=1, rng=random.Random(0), z=1.0)

    assert {pair.title_a, pair.title_b} != set(foregone), (
        "the round asked the question whose answer it could already name"
    )
    assert entropy(*sorted((pair.title_a, pair.title_b))) == pytest.approx(
        max(entropy(*ab) for ab in pairs)
    ), "and it asked the one it was least able to predict"


def test_a_non_positive_straddle_threshold_would_end_every_round_before_it_started():
    """§4.3's `straddle_z` is a §6.3 constant that §6.2's round now also reads, so the bundle
    validator's refusal of a non-positive one (rule: `break_straddle_z`, refused at import) is
    what keeps Tonight from a failure with no error anywhere.

    At z = 0 no posterior reaches the boundary, so nothing straddles, so `stop_reason` converges
    on the empty set: every participant's round ends at pair zero, `ended_by` reads `converged`
    for a shortlist nobody was asked about, and the evening looks like it worked. M4 adds no new
    rule a *bundle* can violate — the pool is built from ownership, kind and scores, all of them
    already covered — but it does give this one a second way to hurt, which is why it is pinned
    here rather than left to `test_bundle_validation.py` alone.
    """
    board = beliefs(1.0, 0.9, 0.8, 0.7, 0.6, var=0.25)
    assert rnd.straddlers(board, z=1.0), "at a real threshold the round has something to ask"

    assert rnd.straddlers(board, z=0.0) == set()
    assert rnd.stop_reason(board, answered=0, z=0.0) == rnd.CONVERGED
    played = rnd.replay({i: 1.0 - 0.1 * i for i in range(5)}, [], z=0.0)
    assert played.next_pair is None and played.answered == 0
