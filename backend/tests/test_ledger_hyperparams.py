"""§5.2's constants come from the bundle. Spec v2.1 §4.3, §5.2, §3.1.

§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … re-tunable offline
in the corpus project. Per-user cutpoints and per-arm sensitivities are **not** shipped — they
are fitted in-app by design."

Two properties, and both fail quietly rather than loudly: a constant read and then ignored is a
knob the corpus project tunes into a void, and a bundle-less household that silently uses
someone's defaults while reporting them as measured is worse than one that says so.
"""

from __future__ import annotations

import json

import pytest

from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams, from_mapping, load


class _Store:
    """The two things `load` asks an ArtifactStore for."""

    def __init__(self, root=None):
        self.root = root
        self.is_empty = root is None

    def path(self, name):
        return self.root / name


# --- the numbers §4.3 names -----------------------------------------------------------------


def test_the_spec_own_numbers_are_the_defaults():
    """§4.3 gives two of them in the prose itself. If the file drifts from the spec, the drift
    should be visible here rather than in a fit nobody can explain."""
    assert DEFAULTS.lambda_ridge == 3.0        # "anchor (ridge) strength λ (currently 3.0)"
    assert DEFAULTS.tie_prior_delta0 == 0.22   # "tie-prior initialisation δ₀ = 0.22"
    assert DEFAULTS.source == "default"


def test_the_tie_prior_converts_to_davidsons_nu():
    """δ₀ is a tie *rate*; ν is Davidson's parameter. P(TIE | d=0) = ν/(2+ν), so ν = 2δ/(1−δ) —
    and 0.22 is the measured share of random pairs that are genuine ties (§4.2)."""
    assert DEFAULTS.nu0() == pytest.approx(2 * 0.22 / 0.78)
    tie_rate = DEFAULTS.nu0() / (2 + DEFAULTS.nu0())
    assert tie_rate == pytest.approx(0.22)


def test_the_decisive_toggle_carries_the_numbers_the_copy_promises():
    """§6.1: "a persistent decisive toggle sets the margin weight (~1.6 vs 1.0)"."""
    assert DEFAULTS.margin_for(decisive=True) == 1.6
    assert DEFAULTS.margin_for(decisive=False) == 1.0


# --- reading a bundle -------------------------------------------------------------------------


def test_a_bundle_constant_replaces_the_default():
    hp, notes = from_mapping({"lambda_ridge": 7.5, "steps": 40})
    assert hp.lambda_ridge == 7.5
    assert hp.steps == 40
    assert hp.source == "bundle"
    assert any("omits" in n for n in notes), "a partial bundle should say what it left out"


def test_a_bundle_that_omits_everything_still_fits():
    """§3.1 makes an empty artifact store legal, and a bundle may ship a subset."""
    hp, notes = from_mapping({})
    assert hp.lambda_ridge == DEFAULTS.lambda_ridge
    assert hp.source == "bundle"
    assert notes


def test_per_user_keys_are_ignored_and_reported():
    """§4.3: per-user cutpoints and per-arm sensitivities "are fitted in-app by design". Taking
    them from a bundle would replace one household's fitted thresholds with another's."""
    hp, notes = from_mapping(
        {"lambda_ridge": 4.0, "cutpoints": [1, 2, 3], "per_user_sensitivity": 0.5}
    )
    assert hp.lambda_ridge == 4.0
    assert not hasattr(hp, "cutpoints")
    assert sum("ignored per-user key" in n for n in notes) == 2


def test_an_unknown_constant_is_reported_rather_than_dropped():
    """A constant the corpus project tuned and this app silently ignores is exactly the kind of
    thing that looks like it is working."""
    _hp, notes = from_mapping({"lambda_lunar": 1.0})
    assert any("unknown hyperparameter 'lambda_lunar'" in n for n in notes)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"lambda_ridge": -1.0}, "positive"),
        ({"lambda_ridge": 0}, "positive"),
        ({"b_i_tau": "big"}, "positive"),
        ({"steps": 0}, "positive integer"),
        ({"steps": 2.5}, "positive integer"),
        ({"tie_prior_delta0": 0.0}, "probability"),
        ({"tie_prior_delta0": 1.0}, "probability"),
        ({"margin_form": "margin^2"}, "margin_form"),
        ({"sigma_inflation_cap": -3}, "sigma_inflation_cap"),
    ],
)
def test_a_nonsensical_constant_is_refused_at_the_boundary(payload, message):
    """Not clamped, not defaulted — refused. A λ of −1 is a corpus-side bug, and a fit that
    quietly substitutes 3.0 for it hides the bug in a number nobody will question."""
    with pytest.raises(ValueError, match=message):
        from_mapping(payload)


def test_sigma_inflation_cap_accepts_the_spec_word_and_a_number():
    """§4.3 ships "σ-inflation rate constant and cap"; §5.2 says the cap is the prior σ, which
    is per title and so cannot be a constant — hence the sentinel."""
    assert from_mapping({"sigma_inflation_cap": "prior"})[0].sigma_inflation_cap == "prior"
    assert from_mapping({"sigma_inflation_cap": 2.5})[0].sigma_inflation_cap == 2.5


# --- the digest -------------------------------------------------------------------------------


def test_the_digest_changes_with_any_constant_that_changes_a_fit():
    """`ledger_fit.hp_digest` is a precondition, not a hint: a cached fit built under other
    constants is wrong rather than stale."""
    base = DEFAULTS.digest()
    for field, value in (
        ("lambda_ridge", 4.0), ("lambda_bt", 2.0), ("steps", 5), ("lr", 0.2),
        ("margin_weighting", False), ("margin_form", "none"), ("tie_prior_delta0", 0.3),
        ("b_i_tau", 0.5), ("sigma_inflation_c", 0.1), ("sigma_inflation_cap", 3.0),
    ):
        import dataclasses

        assert dataclasses.replace(DEFAULTS, **{field: value}).digest() != base, field


def test_provenance_alone_does_not_invalidate_a_cache():
    """The same constants from a bundle and from the defaults produce the same fit, so a cache
    thrown away over provenance would be thrown away for no numerical reason."""
    import dataclasses

    assert dataclasses.replace(DEFAULTS, source="bundle").digest() == DEFAULTS.digest()


# --- loading from a store ----------------------------------------------------------------------


def test_a_bundle_less_household_gets_defaults_and_is_told_so(tmp_path):
    """§3.1: an empty artifact store is legal, so a household can rate before any corpus export
    exists. What it must not do is present this app's guesses as the corpus's measurements."""
    hp, notes = load(_Store(None))
    assert hp is DEFAULTS
    assert any("no artifact bundle" in n for n in notes)


def test_a_bundle_with_no_hyperparams_file_is_not_an_error(tmp_path):
    hp, notes = load(_Store(tmp_path))
    assert hp is DEFAULTS
    assert any("ships no ledger_hyperparams.json" in n for n in notes)


def test_the_shipped_fixture_bundle_parses(tmp_path):
    """The fixture writes the file §4.3 describes; if the two drift apart, every M2 fit is
    running on defaults while the report claims otherwise."""
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    hp, notes = load(_Store(tmp_path / "b" / "artifacts"))
    assert hp.source == "bundle"
    assert hp.lambda_ridge == 3.0
    assert hp.tie_prior_delta0 == 0.22
    assert not [n for n in notes if "unknown" in n], f"fixture ships a key the app ignores: {notes}"


def test_the_fixture_ships_no_per_user_constants(tmp_path):
    """§4.3 is explicit that per-user cutpoints and per-arm sensitivities are not shipped."""
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    raw = json.loads(
        (tmp_path / "b" / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )
    assert not [k for k in raw if "cutpoint" in k.lower() or "sensitiv" in k.lower()]


def test_hyperparams_are_frozen():
    """A fit that mutates its own constants half way through is a fit nobody can reproduce."""
    with pytest.raises(Exception):
        Hyperparams().lambda_ridge = 9.0
