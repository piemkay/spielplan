"""No database: decision 564's move rule, and how many answers it takes on a simulated board."""

from __future__ import annotations

import numpy as np
import pytest

from spielplan.ledger import model
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.ledger.model import OUT_A, ObservationSet
from spielplan.rank import board, moves

K = 6
CUTS = model.initial_cutpoints(K)   # C/B at 0.0, B/A at 1.099, A/S at 2.197


def item(s, sigma, assigned, title_id=1):
    return board.Item(title_id=title_id, name="Dune", s=s, sigma=sigma, assigned_tier=assigned)


def test_a_title_moves_when_seventy_five_percent_of_it_lies_beyond_one_edge_of_its_shown_step():
    # B is [0, 1.099): 1.6 - 0.67 * 1.0 = 0.93 is inside it, 1.94 - 0.67 = 1.27 is past it.
    assert moves.due([item(1.6, 1.0, 3)], cuts=CUTS, answers_since={1: 5}) == []
    [move] = moves.due([item(1.94, 1.0, 3)], cuts=CUTS, answers_since={1: 5})
    assert (move.title_id, move.source, move.target) == (1, 3, 4)
    [down] = moves.due([item(-0.9, 0.1, 3)], cuts=CUTS, answers_since={1: 2})
    assert down.target == 2


def test_no_move_before_two_answers_or_without_a_placement():
    far = item(4.0, 0.1, 3)
    assert moves.due([far], cuts=CUTS, answers_since={1: 1}) == []
    # A newer placement resets the count the reads hand in.
    assert moves.due([far], cuts=CUTS, answers_since={}) == []
    assert moves.due([item(4.0, 0.1, None)], cuts=CUTS, answers_since={1: 9}) == []


def test_a_title_two_steps_off_moves_both_steps_at_once():
    [move] = moves.due([item(1.6, 0.2, 2)], cuts=CUTS, answers_since={1: 3})
    assert (move.source, move.target) == (2, 4)


def test_a_move_goes_only_as_far_as_the_evidence_reaches():
    # s = 1.3 sits in A, but 1.3 - 0.67 * 0.5 = 0.97 only clears C's edge: placed in C, it moves to B.
    [move] = moves.due([item(1.3, 0.5, 2)], cuts=CUTS, answers_since={1: 4})
    assert (move.source, move.target) == (2, 3)


def answers_to_move(true_tier, placed_off, *, per_tier=8, background=60, seed=0):
    """A board placed where it belongs but for one title `placed_off` steps low; it then beats the
    titles of its true step, lowest first, until the rule fires. Returns (answers, target)."""
    rng = np.random.default_rng(seed)
    edges = np.concatenate([[CUTS[0] - 1.0], CUTS, [CUTS[-1] + 1.0]])
    truth, placed = [], []
    for tier in range(K):
        for q in np.linspace(edges[tier], edges[tier + 1], per_tier + 2)[1:-1]:
            truth.append(q)
            placed.append(tier)
    title = len(truth)
    truth.append(edges[true_tier] + 0.8 * (edges[true_tier + 1] - edges[true_tier]))
    placed.append(true_tier - placed_off)
    truth = np.asarray(truth)
    n = truth.size
    duels = []
    for _ in range(background):
        a, b = rng.choice(title, size=2, replace=False)
        duels.append((a, b) if truth[a] > truth[b] else (b, a))
    partners = sorted((i for i in range(title) if placed[i] == true_tier), key=lambda i: truth[i])
    for asked, partner in enumerate(partners, start=1):
        duels.append((title, partner))
        d = np.asarray(duels, dtype=np.int64)
        fit = model.fit(
            ObservationSet(
                title_ids=np.arange(n, dtype=np.int64), embeddings=np.zeros((n, 64)),
                embedded=np.zeros(n, bool), ord_index=np.arange(n, dtype=np.int64),
                ord_level=np.asarray(placed, dtype=np.int64), ord_arm=np.ones(n, dtype=np.int64),
                ord_weight=np.ones(n), duel_a=d[:, 0], duel_b=d[:, 1],
                duel_outcome=np.full(len(d), OUT_A, dtype=np.int64), duel_margin=np.ones(len(d)),
                n_levels=K,
            ),
            DEFAULTS,
        )
        due = moves.due(
            [item(float(fit.s[title]), float(fit.sigma[title]), placed[title], title_id=title)],
            cuts=fit.cuts,
            answers_since={title: asked},
        )
        if due:
            return asked, due[0].target
    return None, None


@pytest.mark.parametrize(
    ("true_tier", "placed_off", "fewest", "most"),
    [(3, 1, 2, 6), (4, 1, 2, 6), (3, 2, 2, 3), (4, 2, 2, 3)],
)
def test_consistent_answers_move_an_adjacent_error_in_two_to_six_and_a_two_step_one_in_two_or_three(
    true_tier, placed_off, fewest, most
):
    asked, target = answers_to_move(true_tier, placed_off)
    assert asked is not None and fewest <= asked <= most, asked
    assert target > true_tier - placed_off
