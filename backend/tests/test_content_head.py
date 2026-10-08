"""§5.1's content head: the solve, the weight gate, and the member-to-member read (568-571).

No database: the arithmetic is the contract, and a `Features` is a plain dataclass, so the whole
head can be driven from numpy. Decision 571 removed the era band, so the only member-relative
columns left are the person affinity and `cf`.
"""

from __future__ import annotations

import numpy as np
import pytest

from spielplan.scoring import content
from spielplan.scoring.foldin import NOISE_FLOOR

TERMS = ("bleak", "sweeping")
GENRES = ("Action", "Drama")


def layout() -> content.Layout:
    return content.layout_for(TERMS, GENRES)


def make_features(n: int = 240, *, seed: int = 3) -> tuple[content.Features, np.ndarray]:
    """`n` synthetic titles: column 1 is a term the member loves, column 0 one they do not."""
    lay = layout()
    rng = np.random.default_rng(seed)
    x = np.zeros((n, lay.member_from))
    loved = rng.random(n)
    hated = rng.random(n)
    x[:, lay.keys.index("dna:sweeping")] = loved
    x[:, lay.keys.index("dna:bleak")] = hated
    ids = np.arange(1, n + 1, dtype=np.int64)
    features = content.Features(
        layout=lay, title_ids=ids, x=x,
        people={}, row_of={int(t): i for i, t in enumerate(ids)},
    )
    return features, loved - hated


# --- the solve ---------------------------------------------------------------------------------


def test_dual_ridge_is_the_primal_ridge():
    rng = np.random.default_rng(1)
    x, y, lam = rng.normal(size=(12, 40)), rng.normal(size=12), 7.0
    primal = np.linalg.solve(x.T @ x + lam * np.eye(40), x.T @ y)
    assert np.allclose(content.ridge(x, y, lam), primal, atol=1e-8)


def test_person_affinity_is_shrunk_and_reads_only_the_labelled_films():
    people = {1: ((10, 3.0),), 2: ((10, 3.0),), 3: ((10, 3.0),), 4: ((99, 3.0),)}
    labelled = [(1, 5.0), (2, 5.0), (3, 5.0), (4, 1.0)]
    out = content.person_affinity(people, labelled, [1, 4, 777])
    assert out[0] > 0.0                 # person 10 only ever appears on films placed at S
    assert out[1] < 0.0
    assert out[2] == 0.0                # a title with nobody the member has rated stays at zero
    # Shrinkage: three films at +1 over the mean cannot reach the full +1.
    assert out[0] < 1.0


def test_affinity_ignores_a_person_the_member_has_not_rated():
    people = {1: ((10, 3.0),), 2: ((20, 3.0),)}
    out = content.person_affinity(people, [(1, 5.0)], [2])
    assert out[0] == 0.0


# --- fitting and serving -----------------------------------------------------------------------


def _labels_from(features, signal, *, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    raw = 2.5 + 2.0 * signal + noise * rng.normal(size=signal.size)
    steps = np.clip(np.rint(raw), 0, 5).astype(int)
    return [(int(t), int(s)) for t, s in zip(features.title_ids, steps, strict=True)]


def test_a_real_content_signal_earns_weight_and_beats_the_baseline():
    features, signal = make_features()
    labels = _labels_from(features, signal)
    ids = [int(t) for t in features.title_ids]
    # A fold-in that says nothing: the head has to carry the ranking on its own.
    base = dict.fromkeys(ids, 0.0)
    cf = dict.fromkeys(ids, 0.0)
    fit = content.fit_user(labels, features, base, cf, seed=11)
    assert fit.weight > 0.0
    assert fit.cv_rho - fit.base_rho > NOISE_FLOOR
    assert fit.used == len(labels)
    assert fit.digest == features.layout.digest


def test_a_head_that_adds_nothing_over_a_real_fold_in_takes_no_weight():
    """The baseline that matters is a fold-in with signal in it, not a flat one.

    The content columns here are pure noise while the fold-in already orders the member's top band
    well, which is the state this gate exists for: §0's "a tie must not buy personalisation".
    """
    features, signal = make_features()
    rng = np.random.default_rng(5)
    # Labels the design cannot see: the signal is in `base`, and the columns know nothing about it.
    hidden = rng.normal(size=signal.size)
    labels = [
        (int(t), int(s))
        for t, s in zip(
            features.title_ids, np.clip(np.rint(2.5 + 2.0 * hidden), 0, 5).astype(int), strict=True
        )
    ]
    ids = [int(t) for t in features.title_ids]
    base = {int(t): float(h) for t, h in zip(features.title_ids, hidden, strict=True)}
    fit = content.fit_user(labels, features, base, dict.fromkeys(ids, 0.0), seed=11)
    assert fit.weight == 0.0, "a tie must not buy personalisation (§0's noise floor)"
    assert fit.base_rho > 0.9, "the baseline here should already be near-perfect"


def test_the_weight_is_gated_on_how_many_answers_stand_behind_it():
    """n/(n+k), as §5.1 gates a coordinate: a short ladder buys only a small share of the score."""
    features, signal = make_features(n=60)
    labels = _labels_from(features, signal)
    ids = [int(t) for t in features.title_ids]
    fit = content.fit_user(labels, features, dict.fromkeys(ids, 0.0), dict.fromkeys(ids, 0.0),
                           seed=7)
    ceiling = min(content.WEIGHT_MAX, 60 / (60 + content.WEIGHT_GATE_K))
    assert fit.weight <= ceiling + 1e-9
    assert fit.weight > 0.0, "a clean signal should still earn something"


def test_at_zero_weight_the_score_is_the_fold_ins_own_score():
    features, _signal = make_features(n=40)
    ids = [int(t) for t in features.title_ids]
    base = {t: float(t) for t in ids}
    zero = content.Fit(
        w=np.zeros(features.layout.width), weight=0.0, lam=100.0, cv_rho=0.2, base_rho=0.2,
        con_sd=1.0, con_mean=0.0, digest=features.layout.digest, label_count=0,
    )
    rows = content.score_many(zero, features, base, dict.fromkeys(ids, 0.0), [], ids)
    assert [(t, s) for t, s, _c in rows] == [(t, float(t)) for t in ids]
    assert all(c == 0.0 for _t, _s, c in rows)


def test_a_title_with_no_design_row_keeps_its_fold_in_score():
    features, signal = make_features(n=60)
    labels = _labels_from(features, signal)
    ids = [int(t) for t in features.title_ids]
    fit = content.fit_user(labels, features, dict.fromkeys(ids, 0.0), dict.fromkeys(ids, 0.0),
                           seed=2)
    stranger = 10_000
    rows = content.score_many(
        fit, features, {**dict.fromkeys(ids, 0.0), stranger: 4.25},
        dict.fromkeys([*ids, stranger], 0.0), [(t, float(s)) for t, s in labels], [*ids, stranger],
    )
    last = rows[-1]
    assert last[0] == stranger
    assert last[1] == pytest.approx(4.25)
    assert last[2] == 0.0


def test_no_labels_leaves_a_fitted_but_silent_head():
    features, _signal = make_features(n=30)
    ids = [int(t) for t in features.title_ids]
    fit = content.fit_user([], features, dict.fromkeys(ids, 0.0), dict.fromkeys(ids, 0.0))
    assert (fit.weight, fit.used, fit.con_sd) == (0.0, 0, 0.0)


def test_labels_on_titles_outside_the_design_are_counted_not_ignored():
    features, signal = make_features(n=30)
    labels = [*_labels_from(features, signal), (99_999, 5)]
    ids = [int(t) for t in features.title_ids]
    fit = content.fit_user(labels, features, dict.fromkeys(ids, 0.0), dict.fromkeys(ids, 0.0))
    assert fit.dropped == 1
    assert fit.label_count == len(labels)


# --- the layout and the member-to-member read (decision 570) -----------------------------------


def test_the_member_tail_is_last_and_named():
    lay = layout()
    assert lay.keys[lay.member_from:] == content.MEMBER_KEYS
    assert "people:affinity" not in lay.keys[: lay.member_from]


def test_the_digest_moves_with_the_vocabulary():
    assert layout().digest != content.layout_for((*TERMS, "tense"), GENRES).digest
    assert layout().digest == content.layout_for(tuple(reversed(TERMS)), GENRES).digest


def test_pack_round_trips_and_refuses_a_wrong_width():
    w = np.linspace(-1.0, 1.0, 24)
    assert np.allclose(content.unpack(content.pack(w), 24), w, atol=1e-6)
    with pytest.raises(ValueError, match="expected 7 weights"):
        content.unpack(content.pack(w), 7)


def _fit_with(w_title: dict[str, float], digest: str, lay: content.Layout) -> content.Fit:
    w = np.zeros(lay.width)
    for key, value in w_title.items():
        w[lay.keys.index(key)] = value
    return content.Fit(
        w=w, weight=0.5, lam=10.0, cv_rho=0.5, base_rho=0.4, con_sd=1.0, con_mean=0.0,
        digest=digest, label_count=50,
    )


def test_match_separates_what_two_members_share_from_what_splits_them():
    lay = layout()
    a = _fit_with({"dna:sweeping": 1.0, "dna:bleak": 0.5, "genre:Action": -0.8}, lay.digest, lay)
    b = _fit_with({"dna:sweeping": 0.9, "dna:bleak": -0.6, "genre:Action": -0.7}, lay.digest, lay)
    out = content.match(a, b, lay, ([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 4.0, 3.0]))
    assert out.comparable
    assert out.agreement > 0.0
    shared = {k for k, _p, _q in out.shared}
    opposed = {k for k, _p, _q in out.opposed}
    assert "dna:sweeping" in shared and "genre:Action" in shared
    assert "dna:bleak" in opposed
    assert shared.isdisjoint(opposed)


def test_match_leaves_the_member_relative_tail_out():
    lay = layout()
    a = _fit_with({"dna:sweeping": 1.0, "people:affinity": 2.0}, lay.digest, lay)
    b = _fit_with({"dna:sweeping": 1.0, "people:affinity": 2.0}, lay.digest, lay)
    out = content.match(a, b, lay)
    named = {k for k, _p, _q in (*out.shared, *out.opposed)}
    assert "people:affinity" not in named, (
        "an affinity is read off each member's own placements and means a different thing in each"
    )
    assert "dna:sweeping" in named


def test_match_refuses_two_fits_from_different_layouts():
    lay = layout()
    a = _fit_with({"dna:sweeping": 1.0}, lay.digest, lay)
    b = _fit_with({"dna:sweeping": 1.0}, "something-else", lay)
    out = content.match(a, b, lay, ([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))
    assert not out.comparable
    assert out.shared == () and out.opposed == ()
    # The agreement still stands: it is read off the scores, not off the weights.
    assert out.agreement == pytest.approx(1.0)


def test_match_is_unmoved_by_one_member_having_a_louder_fit():
    lay = layout()
    a = _fit_with({"dna:sweeping": 1.0, "dna:bleak": -0.5}, lay.digest, lay)
    loud = _fit_with({"dna:sweeping": 50.0, "dna:bleak": -25.0}, lay.digest, lay)
    quiet = _fit_with({"dna:sweeping": 0.02, "dna:bleak": -0.01}, lay.digest, lay)
    first = content.match(a, loud, lay).shared
    second = content.match(a, quiet, lay).shared
    assert [k for k, _p, _q in first] == [k for k, _p, _q in second]
    assert first[0][2] == pytest.approx(second[0][2], abs=1e-9)
