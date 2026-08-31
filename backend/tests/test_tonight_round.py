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
    the pool prior and is carried entirely by their answers." The prototype's bug was literal
    substitution — `const u = guest ? 'p' : who` — which is a privacy-shaped defect, not
    "contributes no taste term"."""
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert [t for t, _ in sorted(guest.items(), key=lambda kv: -kv[1].mu)] == [5, 6, 7]
    assert len({b.mu for b in guest.values()}) == 1, (
        "a guest's own posterior starts flat — the pool's ORDER is what they are shown, not a "
        "member's scores wearing their name"
    )
    assert len({b.mu for b in member.values()}) == 3


def test_a_guest_starts_wider_than_a_member():
    """"which naturally makes a guest's round a little longer" — the only thing the spec says
    about how a guest's round differs, and it follows from knowing nothing about them."""
    pool_order = {5: 0.9, 6: 0.5, 7: 0.1}
    member = rnd.initial(pool_order, prior_var=1.0)
    guest = rnd.initial(pool_order, prior_var=1.0, has_profile=False)

    assert all(guest[t].var > member[t].var for t in pool_order)
