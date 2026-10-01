"""Pure and seeded. A seat's films carry their place on the mood directions (`z`); the reach is the
candidates the mood may re-rank, each with its own `z`. The hold-out guard is checked by replay:
change a hold-out's answer and nothing the round reads may move."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from spielplan.tonight import round as rnd
from spielplan.tonight import tilt

A, B, EITHER, NEITHER = rnd.A, rnd.B, rnd.EITHER, rnd.NEITHER
K = rnd.MOOD_DIRECTIONS


def axis(i, size=1.0):
    z = [0.0] * K
    z[i] = size
    return tuple(z)


def film(title_id, *, step=5, runtime=120, z=None):
    return rnd.Film(title_id=title_id, step=step, runtime_min=runtime, z=z or (0.0,) * K)


def films(n=10, *, start=1, step=5, runtime=120, rng=None):
    """`n` level films, scattered on the directions when `rng` is given."""
    return [
        film(
            start + i, step=step, runtime=runtime,
            z=tuple(rng.gauss(0, 1) for _ in range(K)) if rng else None,
        )
        for i in range(n)
    ]


def arm_key(*, fires=(), quiet=()):
    """The arm is a rate drawn from a key (decision 223), so a test names both seq and key."""
    for i in range(100_000):
        key = f"arm-{i}"
        if all(rnd.is_holdout(s, key=key) for s in fires) and not any(
            rnd.is_holdout(s, key=key) for s in quiet
        ):
            return key
    raise AssertionError(f"no key fires at {sorted(fires)} and is quiet at {sorted(quiet)}")


# Quiet for a whole round, so these tests get the pair the adaptive arm draws.
ADAPTIVE_ARM = arm_key(quiet=range(1, rnd.CAP_PAIRS + 1))

# A reach of four: three with no DNA at the top and a fourth that sits on direction 0.
STABLE = {101: 3.0, 102: 2.0, 103: 1.0, 104: 0.9, 105: -1.0}
REACH = {101: (0.0,) * K, 102: (0.0,) * K, 103: (0.0,) * K, 104: axis(0, 3.0)}


def test_the_round_keeps_decision_550s_numbers():
    assert (rnd.CAP_PAIRS, rnd.ESCAPE_FROM_PAIR, rnd.TYPICAL_PAIRS, rnd.MIN_ROUND_FILMS) == (8, 4, 5, 8)
    assert (rnd.PAIR_STEP, rnd.PAIR_RUNTIME_MIN, rnd.MOOD_DIRECTIONS, rnd.TILT_REACH) == (1, 30, 5, 30)
    assert rnd.WELL_KNOWN_CROWD == 30000 and rnd.HOLDOUT_EVERY == 10


def test_only_the_four_answers_exist():
    assert rnd.ANSWERS == ("A", "B", "EITHER", "NEITHER")
    with pytest.raises(ValueError):
        rnd.observe(rnd.prior(), a=axis(0), b=axis(1), answer="MAYBE")


# --- the mood ----------------------------------------------------------------------------------


def test_a_side_moves_the_mood_toward_the_film_chosen():
    """Chosen minus rejected: the mood now favours what looks like the chosen film, and B mirrors A."""
    a, b = axis(0), axis(1)
    chose_a = rnd.observe(rnd.prior(), a=a, b=b, answer=A)
    chose_b = rnd.observe(rnd.prior(), a=a, b=b, answer=B)

    assert chose_a.mean @ np.asarray(a) > chose_a.mean @ np.asarray(b)
    assert chose_a.mean == pytest.approx(-chose_b.mean)
    assert chose_a.mean[2:] == pytest.approx(np.zeros(K - 2)), "directions the pair does not span stay put"


def test_either_says_the_difference_does_not_matter():
    """No lean either way, and less doubt about the direction the two films differ along."""
    a, b = axis(0), axis(1)
    after = rnd.observe(rnd.prior(), a=a, b=b, answer=EITHER)
    d = np.asarray(a) - np.asarray(b)

    assert after.mean == pytest.approx(np.zeros(K))
    assert d @ after.cov @ d < d @ rnd.prior().cov @ d
    assert after.cov[2, 2] == pytest.approx(rnd.MOOD_PRIOR_SD**2), "and nothing about the rest"


def test_neither_says_tonight_lies_away_from_both():
    a, b = axis(0), axis(1)
    after = rnd.observe(rnd.prior(), a=a, b=b, answer=NEITHER)

    assert after.mean @ np.asarray(a) < 0 and after.mean @ np.asarray(b) < 0


def test_an_answer_about_films_with_no_dna_moves_nothing():
    """Decision 218: an untagged film sits at the pool's centre, so it says nothing about the mood."""
    blank = (0.0,) * K
    for answer in rnd.ANSWERS:
        after = rnd.observe(rnd.prior(), a=blank, b=blank, answer=answer)
        assert after.mean == pytest.approx(np.zeros(K))
        assert after.cov == pytest.approx(rnd.prior().cov)


def test_answers_accumulate():
    once = rnd.observe(rnd.prior(), a=axis(0), b=axis(1), answer=A)
    twice = rnd.observe(once, a=axis(0), b=axis(2), answer=A)
    assert twice.mean[0] > once.mean[0] > 0


# --- what the mood does to the candidates ------------------------------------------------------


def test_the_mood_re_ranks_only_the_reach():
    """Decision 550: the top 30 unseen by stable taste move; everything else keeps its own score."""
    mood = np.asarray(axis(0, 1.0))
    scores = rnd.tonight(STABLE, REACH, mood)

    assert scores[105] == STABLE[105], "outside the reach the mood changes nothing"
    assert sorted(scores[t] for t in REACH) == sorted(STABLE[t] for t in REACH), (
        "the reach keeps its own stable values, so it stays on the room's scale"
    )
    assert scores[104] == 3.0, "the film the mood favours rises to the top of the reach"
    assert min(scores[t] for t in REACH) > scores[105], "and the reach stays above the rest"


def test_with_no_mood_the_order_is_stable_taste():
    assert rnd.tonight(STABLE, REACH, np.zeros(K)) == STABLE
    assert rnd.top_three(STABLE, REACH, np.zeros(K)) == frozenset({101, 102, 103})


def test_the_adjustment_is_the_mood_against_each_candidates_place():
    adjust = rnd.adjustments(np.asarray(axis(0, 0.5)), REACH)
    assert adjust == {101: 0.0, 102: 0.0, 103: 0.0, 104: pytest.approx(1.5)}


# --- which pairs may be asked ------------------------------------------------------------------


def test_fewer_than_eight_films_is_no_round():
    """Decision 539: the seat ends at once, with no tilt and nothing asked."""
    seven = films(7)
    played = rnd.replay(seven, STABLE, REACH, [], holdout_key=ADAPTIVE_ARM)
    assert played.stop_reason == rnd.CONVERGED and played.next_pair is None
    assert played.adaptive == 0

    played = rnd.replay(films(8), STABLE, REACH, [], holdout_key=ADAPTIVE_ARM)
    assert played.stop_reason is None and played.next_pair is not None


def test_a_pair_is_at_most_one_step_apart():
    pairs = rnd.askable([film(1, step=6), film(2, step=5), film(3, step=4)], [])
    assert (1, 2) in pairs and (2, 3) in pairs
    assert (1, 3) not in pairs, "S against A is two steps"


def test_a_pair_is_within_thirty_minutes_and_both_runtimes_known():
    pairs = rnd.askable(
        [film(1, runtime=100), film(2, runtime=130), film(3, runtime=131), film(4, runtime=None)], []
    )
    assert (1, 2) in pairs and (2, 3) in pairs
    assert (1, 3) not in pairs, "31 minutes apart"
    assert all(4 not in p for p in pairs), "an unknown runtime cannot be held level"


def test_a_guests_films_carry_no_step_and_pair_on_runtime_alone():
    guest = [film(1, step=None), film(2, step=None, runtime=140), film(3, step=None, runtime=200)]
    assert rnd.askable(guest, []) == [(1, 2)]


def test_no_pair_repeats_in_an_evening():
    pairs = rnd.askable(films(2), [frozenset({1, 2})])
    assert pairs == []


def test_a_shown_pair_naming_a_film_the_seat_no_longer_has_blocks_no_other_pair():
    """§10 can take a film out from under a stored answer; that row must not rule out a live pair."""
    assert (2, 3) in rnd.askable(films(10), [frozenset({1, 14})])


def test_a_film_returns_only_once_every_film_has_shown():
    """Decision 550: with 1-2 shown, the next pair is two fresh films; once all four have shown, a
    film returns with a new partner."""
    four = films(4)
    assert set(rnd.askable(four, [frozenset({1, 2})])) == {(3, 4)}
    after_all = rnd.askable(four, [frozenset({1, 2}), frozenset({3, 4})])
    assert after_all and all(p not in {(1, 2), (3, 4)} for p in after_all)


def test_a_film_with_no_level_partner_does_not_hold_the_others_back():
    """Film 5 runs far longer than the rest; the round still rotates through films 1-4."""
    five = [*films(4), film(5, runtime=300)]
    assert set(rnd.askable(five, [frozenset({1, 2})])) == {(3, 4)}


# --- selection ---------------------------------------------------------------------------------


def test_the_adaptive_pair_is_the_one_expected_to_teach_the_most():
    """One pair differs along a direction; every other pair differs along nothing."""
    flat = [*films(8), film(9, z=axis(0, 2.0)), film(10, z=axis(0, -2.0))]
    for seed in range(20):
        pair = rnd.select(
            flat, rnd.prior(), shown=[], seq=1, rng=random.Random(seed), holdout_key=ADAPTIVE_ARM
        )
        assert {pair.title_a, pair.title_b} == {9, 10}
        assert pair.selection == rnd.SELECTION_ADAPTIVE


def test_the_draw_is_sealed_but_evenings_differ():
    """Equal information everywhere: one seed always draws one pair, and seeds differ (decision 550)."""
    level_films = films(10)
    draw = [
        rnd.select(level_films, rnd.prior(), shown=[], seq=1, rng=random.Random(s),
                   holdout_key=ADAPTIVE_ARM)
        for s in (7, 7, 8, 9, 10, 11)
    ]
    assert (draw[0].title_a, draw[0].title_b) == (draw[1].title_a, draw[1].title_b)
    assert len({frozenset({p.title_a, p.title_b}) for p in draw}) > 1
    assert {p.title_a < p.title_b for p in draw} == {True, False}, "neither side is always the lower id"


def test_a_pair_never_names_one_film_twice():
    rng = random.Random(3)
    some = films(12, rng=rng)
    for seed in range(20):
        pair = rnd.select(
            some, rnd.prior(), shown=[], seq=1, rng=random.Random(seed), holdout_key=ADAPTIVE_ARM
        )
        assert pair.title_a != pair.title_b


# --- the hold-out ------------------------------------------------------------------------------


def test_one_pair_in_ten_is_the_uniform_hold_out_and_it_is_a_rate_not_a_slot():
    """A rate, not `seq % 10 == 0`: a slot schedule gives rounds that end before pair 10 none."""
    seats = [str(i) for i in range(2000)]
    fired = [
        [seq for seq in range(1, rnd.CAP_PAIRS + 1) if rnd.is_holdout(seq, key=k)] for k in seats
    ]
    drawn = sum(len(f) for f in fired)
    rate = drawn / (len(seats) * rnd.CAP_PAIRS)
    assert 0.085 < rate < 0.115, f"the arm fired {drawn} times in {len(seats) * rnd.CAP_PAIRS}"
    early = [f for f in fired if any(seq <= rnd.ESCAPE_FROM_PAIR for seq in f)]
    assert len(early) > len(seats) / 5, "rounds escaped at pair 4 still carry hold-outs"


def test_solo_asks_no_hold_out():
    assert not any(rnd.is_holdout(seq, key=None) for seq in range(1, 200))


def test_the_arm_is_the_same_answer_in_a_second_process():
    """A function of key and seq only: `hash()` is salted per process, `random.Random(str)` is not."""
    here = [seq for seq in range(1, 41) if rnd.is_holdout(seq, key="seat-19")]
    assert here, "pick a key whose arm fires, or this asserts that nothing equals nothing"
    src = (
        "from spielplan.tonight import round as rnd;"
        "print([s for s in range(1, 41) if rnd.is_holdout(s, key='seat-19')])"
    )
    # This module's own tree: the venv's editable install points at the primary checkout.
    env = dict(os.environ)
    backend = str(Path(rnd.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join([backend, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    for hash_seed in ("0", "1", "4242"):
        env["PYTHONHASHSEED"] = hash_seed
        out = subprocess.run(
            [sys.executable, "-c", src], capture_output=True, text=True, check=True, env=env,
        )
        assert out.stdout.strip() == str(here)


def test_the_hold_out_pair_is_drawn_uniformly_from_the_seats_own_pairs():
    """Uniform over what the seat could be asked, whatever the mood knows: the decisive pair is
    drawn no more often than any other."""
    flat = [*films(4), film(5, z=axis(0, 2.0)), film(6, z=axis(0, -2.0))]
    held = arm_key(fires=[1])
    counts: dict[frozenset[int], int] = {}
    for seed in range(3000):
        pair = rnd.select(flat, rnd.prior(), shown=[], seq=1, rng=random.Random(seed), holdout_key=held)
        assert pair.selection == rnd.SELECTION_HOLDOUT
        key = frozenset({pair.title_a, pair.title_b})
        counts[key] = counts.get(key, 0) + 1
    assert len(counts) == 15, "every pair the seat could be asked is reachable"
    assert max(counts.values()) < 2 * min(counts.values())


def test_the_hold_out_arm_never_falls_back():
    key = arm_key(fires=[2], quiet=[1, 3])
    some = films(8)
    assert rnd.select(some, rnd.prior(), shown=[], seq=2, rng=random.Random(0),
                      holdout_key=key).selection == rnd.SELECTION_HOLDOUT
    for seq in (1, 3):
        assert rnd.select(some, rnd.prior(), shown=[], seq=seq, rng=random.Random(0),
                          holdout_key=key).selection == rnd.SELECTION_ADAPTIVE


def test_a_hold_out_answer_moves_nothing_the_round_reads():
    """§13: change a hold-out's answer and the mood, the stop and the next pair stay put."""
    seat = films(10, rng=random.Random(5))
    reach = {t: tuple(random.Random(t).gauss(0, 1) for _ in range(K)) for t in range(201, 231)}
    stable = {t: -0.01 * t for t in reach}
    rows = [
        rnd.Answered(seq=1, title_a=1, title_b=2, answer=A),
        rnd.Answered(seq=2, title_a=3, title_b=4, answer=A, selection=rnd.SELECTION_HOLDOUT),
    ]
    flipped = [rows[0], rnd.Answered(seq=2, title_a=3, title_b=4, answer=B,
                                     selection=rnd.SELECTION_HOLDOUT)]
    one = rnd.replay(seat, stable, reach, rows, holdout_key=ADAPTIVE_ARM)
    other = rnd.replay(seat, stable, reach, flipped, holdout_key=ADAPTIVE_ARM)

    assert one.mood.mean == pytest.approx(other.mood.mean)
    assert one.adaptive == other.adaptive == 1, "a hold-out is not an answer the stop rule counts"
    assert one.answered == 2, "but it costs the seat one of its eight"
    assert (one.next_pair.title_a, one.next_pair.title_b) == (other.next_pair.title_a,
                                                              other.next_pair.title_b)


# --- stopping, the cap and the escape ----------------------------------------------------------


def test_the_round_stops_once_three_answers_are_in_and_the_top_three_held_for_two():
    same, moved = frozenset({1, 2, 3}), frozenset({1, 2, 4})
    assert rnd.stop_reason([same, same, same], answered=2) is None, "two answers are too few"
    assert rnd.stop_reason([moved, same, same, same], answered=3) == rnd.CONVERGED
    assert rnd.stop_reason([same, same, moved, same], answered=3) is None, (
        "the third answer moved it back, so it has held for one answer, not two"
    )
    assert rnd.stop_reason([same, moved, same, same, same], answered=4) == rnd.CONVERGED


def test_the_cap_counts_every_pair_shown_and_convergence_wins_a_tie():
    same, moved = frozenset({1, 2, 3}), frozenset({1, 2, 4})
    churn = [same, moved, same, moved, same, moved]
    assert rnd.stop_reason(churn, answered=rnd.CAP_PAIRS - 1) is None
    assert rnd.stop_reason(churn, answered=rnd.CAP_PAIRS) == rnd.CAP
    assert rnd.stop_reason([same] * 4, answered=rnd.CAP_PAIRS) == rnd.CONVERGED


def test_a_round_whose_answers_move_nothing_stops_at_three():
    seat = films(10)
    rows = [rnd.Answered(seq=i + 1, title_a=2 * i + 1, title_b=2 * i + 2, answer=A) for i in range(3)]
    assert rnd.replay(seat, STABLE, REACH, rows[:2], holdout_key=ADAPTIVE_ARM).stop_reason is None
    played = rnd.replay(seat, STABLE, REACH, rows, holdout_key=ADAPTIVE_ARM)
    assert played.stop_reason == rnd.CONVERGED and played.next_pair is None


def test_titles_outside_the_reach_do_not_hold_the_top_three_still():
    """A seen favourite above the reach is one the mood never moves: the stop rule watches the reach,
    so a round whose answers keep reshuffling it runs on whatever sits above."""
    seat = [film(i + 1, z=axis(i)) for i in range(4)] + films(6, start=5)
    reach = {200 + i: axis(i - 1, 3.0) for i in range(1, 5)}
    reach.update({t: (0.0,) * K for t in range(205, 231)})
    stable = {t: (-0.1 if t < 205 else 0.0) - 0.001 * t for t in reach}
    seen_above = {101: 5.0, 102: 4.0, 103: 3.0}
    # Each answer favours a new direction, so a new film of the reach climbs into its top three.
    rows = [
        rnd.Answered(seq=1, title_a=1, title_b=2, answer=A),
        rnd.Answered(seq=2, title_a=3, title_b=1, answer=A),
        rnd.Answered(seq=3, title_a=4, title_b=3, answer=A),
    ]
    alone = rnd.replay(seat, stable, reach, rows, holdout_key=ADAPTIVE_ARM)
    assert alone.stop_reason is None, "the reach's top three moved on the third answer"

    played = rnd.replay(seat, {**stable, **seen_above}, reach, rows, holdout_key=ADAPTIVE_ARM)
    assert played.stop_reason is None and played.next_pair is not None


def test_eight_pairs_end_the_round_hold_outs_included():
    seat = films(10)
    rows = [
        rnd.Answered(seq=i + 1, title_a=1 + i % 9, title_b=2 + i % 9, answer=A,
                     selection=rnd.SELECTION_HOLDOUT)
        for i in range(rnd.CAP_PAIRS)
    ]
    played = rnd.replay(seat, STABLE, REACH, rows, holdout_key=ADAPTIVE_ARM)
    assert played.stop_reason == rnd.CAP and played.adaptive == 0


def test_the_round_ends_sooner_only_when_no_level_pair_is_left():
    """Eight films an hour or more apart but for two pairs: two answers, then nothing left to ask,
    recorded as the cap, its nearest ending."""
    seat = [film(1, runtime=100), film(2, runtime=100), film(3, runtime=200), film(4, runtime=200),
            *(film(5 + i, runtime=300 + 100 * i) for i in range(4))]
    assert sorted(rnd.askable(seat, [])) == [(1, 2), (3, 4)]
    rows = [
        rnd.Answered(seq=1, title_a=1, title_b=2, answer=A),
        rnd.Answered(seq=2, title_a=3, title_b=4, answer=A),
    ]
    assert rnd.replay(seat, STABLE, REACH, rows[:1], holdout_key=ADAPTIVE_ARM).next_pair is not None
    played = rnd.replay(seat, STABLE, REACH, rows, holdout_key=ADAPTIVE_ARM)
    assert (played.stop_reason, played.next_pair) == (rnd.CAP, None)


def test_every_round_ends_with_one_of_the_three_reasons():
    assert set(rnd.END_REASONS) == {"converged", "cap", "escape"}
    rng = random.Random(11)
    seat = films(12, rng=rng)
    reach = {t: tuple(rng.gauss(0, 1) for _ in range(K)) for t in range(201, 231)}
    stable = {t: -0.01 * t for t in reach}
    rows = []
    for seq in range(1, 20):
        played = rnd.replay(seat, stable, reach, rows, holdout_key="seat-3", rng=random.Random(seq))
        if played.stop_reason:
            break
        p = played.next_pair
        rows.append(rnd.Answered(seq=seq, title_a=p.title_a, title_b=p.title_b,
                                 answer=rng.choice(rnd.ANSWERS), selection=p.selection))
    assert played.stop_reason in rnd.END_REASONS
    assert played.answered <= rnd.CAP_PAIRS


def test_the_escape_opens_at_pair_four():
    assert not rnd.escape_available(2), "the third pair is on screen"
    assert rnd.escape_available(3), "the fourth pair is on screen"


def test_an_early_escape_is_refused_by_the_controls_name():
    with pytest.raises(rnd.EscapeTooEarly) as early:
        rnd.escape(answered=2)
    assert "just pick for us" in str(early.value) and "pair 4" in str(early.value)
    assert rnd.escape(answered=3) == rnd.ESCAPE
    played = rnd.replay(films(10), STABLE, REACH, [], holdout_key=ADAPTIVE_ARM, escaped=True)
    assert played.stop_reason == rnd.ESCAPE and played.next_pair is None


def test_a_stale_row_counts_toward_the_cap_and_moves_nothing():
    """§10 can take a film out from under a stored answer; one film named twice compares nothing."""
    seat = films(10, rng=random.Random(2))
    rows = [
        rnd.Answered(seq=1, title_a=1, title_b=999, answer=A),
        rnd.Answered(seq=2, title_a=3, title_b=3, answer=A),
    ]
    played = rnd.replay(seat, STABLE, REACH, rows, holdout_key=ADAPTIVE_ARM)
    assert played.answered == 2 and played.adaptive == 0
    assert played.mood.mean == pytest.approx(np.zeros(K))


# --- the budget --------------------------------------------------------------------------------


def test_a_guest_seat_gets_its_first_pair_inside_the_budget():
    """A household's library at 200 min: the mood space over 2,000 candidates, then a first pair for
    a guest among 700 well-known films. A third of §6's 1.5 s, since a write replays too."""
    rng = random.Random(4)
    terms = [f"t{i}" for i in range(400)]
    pool_dna = {t: {x: rng.random() for x in rng.sample(terms, 30)} for t in range(2000)}
    started = time.perf_counter()
    space = tilt.space(pool_dna)
    guest = [
        rnd.Film(title_id=5000 + i, step=None, runtime_min=rng.randrange(80, 180),
                 z=space.project({x: rng.random() for x in rng.sample(terms, 30)}))
        for i in range(700)
    ]
    pair = rnd.select(guest, rnd.prior(), shown=[], seq=1, rng=random.Random(0),
                      holdout_key=ADAPTIVE_ARM)
    elapsed = time.perf_counter() - started
    assert pair is not None
    assert elapsed < 0.5, f"the first pair took {elapsed:.2f} s"
