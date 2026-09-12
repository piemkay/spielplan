"""§5.2's constants come from the bundle. Spec v2.1 §4.3, §5.2, §3.1.

§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … re-tunable offline
in the corpus project. Per-user cutpoints and per-arm sensitivities are **not** shipped — they
are fitted in-app by design."

Two properties, and both fail quietly rather than loudly: a constant read and then ignored is a
knob the corpus project tunes into a void, and a bundle-less household that silently uses
someone's defaults while reporting them as measured is worse than one that says so.

The last section is integration, against a real Postgres, and has to be: §10 makes a bundle swap
a restart, so "read once" is a claim about the *process* — it lives in `app.state`, which only
the real lifespan fills, from a store only the `artifact_bundle` row can name. Skipped without
TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
from pathlib import Path

import httpx
import pytest

from spielplan.ledger import hyperparams as hp_module
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
        # M4.13's block, refused on the same principle. `warm_gate` is a probability because
        # WARM_SUPPORT is k*g/(1-g) - at 1.0 that is a zero-divide and above it every title is
        # warm; `blend_beta_max` has a ceiling because `0009_scoring.sql` CHECKs the column at
        # 0.8 and a migration cannot be edited, so a bundle raising it would fail the nightly
        # fit's INSERT rather than serve anything; and a grid is the only sequence-valued
        # constant here, so a scalar would reach numpy inside `_cross_validate` instead.
        ({"gate_k": 0}, "positive number"),
        ({"warm_gate": 1.0}, "probability"),
        ({"warm_gate": 0}, "probability"),
        ({"blend_beta_max": 0.9}, "ceiling"),
        ({"min_labels_for_cv": 0}, "positive integer"),
        ({"loo_below_labels": 2.5}, "positive integer"),
        ({"blend_beta_grid": 0.5}, "non-empty list"),
        ({"foldin_lambda_grid": []}, "non-empty list"),
        ({"blend_beta_grid": [0.1, "half"]}, "non-negative numbers"),
        ({"foldin_lambda_grid": [1.0, -3.0]}, "non-negative numbers"),
        # Both solver knobs are strictly positive by construction: a tolerance of 0 or below
        # declares every Newton solve converged, and `model.fit`'s `while eta >= hp.lr_min` does
        # not terminate at 0 once the halving reaches 0.0. [M4.13 cycle 1, M413-R1-HP-03]
        ({"newton_tol": -5.0}, "positive number"),
        ({"newton_tol": 0}, "positive number"),
        ({"lr_min": -1.0}, "positive number"),
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


@pytest.mark.parametrize("payload", ["[]", "null", "3", '"tuned"'])
def test_a_constants_file_that_is_not_an_object_is_a_value_error(tmp_path, payload):
    """The refusal has to be a `ValueError`, because that is the class all four readers catch.

    `json.loads` accepts a top-level array, `null`, a number and a string, so `_flatten`'s
    `raw.items()` raised `AttributeError` for them — not a `ValueError`, and therefore caught by
    none of the four: the lifespan's `except` clauses, so the process failed to start instead of
    degrading; the three routers' `_hyperparams` fallbacks, so the 503 was a 500; and
    `validate_hyperparams`, so the Data tab's Validate button answered 500 with no report line on
    the one surface whose whole job is to report. `importer/validate._read_json` already applies
    this check to `manifest.json` and `BUNDLE.json`. [M4.10 cycle 1, M410-R1-02 / M410-R1-03]
    """
    (tmp_path / "ledger_hyperparams.json").write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        load(_Store(tmp_path))
    assert "must be a JSON object" in str(refused.value), str(refused.value)


def test_a_constants_path_that_is_not_a_file_is_not_read_as_an_absent_one(tmp_path):
    """Present and unopenable is not the same state as absent, and only one of them may default.

    The guard was `is_file()`, which is false for a path that exists as a directory — what a `tar`
    extraction or an out-of-band copy of an artifacts tree can leave behind. Both readers then
    treated the bundle as shipping no constants: DEFAULTS served under that note, and
    `validate_hyperparams` emitting neither a failure nor its "constants read" line. That is the
    silent substitution this milestone refuses by name, because DEFAULTS carry a different
    `hp_digest` and every cached fit in the install is discarded behind it.

    An `OSError` rather than a refusal of its own, because that is what `read_text` raises and what
    the lifespan, the three routers and the validator now all report. [M4.10 cycle 1, M410-R1-06]
    """
    (tmp_path / "ledger_hyperparams.json").mkdir()
    with pytest.raises(OSError):
        load(_Store(tmp_path))


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
    with pytest.raises(dataclasses.FrozenInstanceError):
        Hyperparams().lambda_ridge = 9.0


# --- the corpus's own spellings ---------------------------------------------------------------
#
# The fixture ships `ledger_hyperparams.json` under the corpus's names (M4.5). Before that, the
# fixture and this reader agreed on a spelling the artifact has never used, so every assertion
# below was true of a file no bundle contains.


def _shipped(tmp_path):
    from tests.fixtures import make_bundle as fx

    fx.make_bundle(tmp_path / "b")
    return json.loads(
        (tmp_path / "b" / "artifacts" / "ledger_hyperparams.json").read_text(encoding="utf-8")
    )


def test_the_corpus_spellings_reach_the_fields_they_tune(tmp_path):
    """Rule 1 of the module — "every constant comes from the bundle" — is what fails silently
    here: under this app's spellings the corpus's tuned λ_bt and learning rate both arrive as
    the defaults while `source` still reads "bundle"."""
    hp, _notes = from_mapping(_shipped(tmp_path))
    assert hp.lambda_ridge == 3.0          # anchor_ridge_lambda
    assert hp.lambda_bt == 0.3             # bt_weight_lam_bt — the default is 1.0
    assert hp.lr == 0.5                    # learning_rate — the default is 0.1
    assert hp.steps == 30                  # same name in both, and the default is 200
    assert hp.margin_form == "margin/mean(margin)"          # margin_weight_form, as prose
    assert hp.sigma_inflation_cap == "prior"                # sigma_inflation.cap, "prior_sigma"
    assert hp.sigma_inflation_grace_months == 12            # sigma_inflation.trigger_months


def test_the_prose_form_maps_only_to_itself():
    """The corpus states the margin form as a sentence. Mapping it by prefix would read any
    later form as this one, which is the §5.2 recipe changing without the fit noticing."""
    assert from_mapping({"margin_weight_form": "none"})[0].margin_form == "none"
    with pytest.raises(ValueError, match="margin_form"):
        from_mapping({"margin_weight_form": "w = sqrt(margin); 1.0 when disabled"})


def test_a_constant_the_corpus_tuned_and_this_app_cannot_use_is_named(tmp_path):
    """§4.3's gap, declared. `logit_clip`, `item_prior_shrink` and `user_offset_shrink_lam` are
    tuned upstream and have no term in this app's fit; reporting them as "unknown" would file
    them with typos, and dropping them is the silence rule 1 exists to prevent."""
    _hp, notes = from_mapping(_shipped(tmp_path))
    for key in ("logit_clip", "item_prior_shrink", "user_offset_shrink_lam"):
        assert any(key in n and "no term in this app" in n for n in notes), key
    assert not [n for n in notes if "unknown" in n], notes


def test_no_shipped_key_disappears_without_a_word(tmp_path):
    """The property behind all of the above: every key in the file either lands in a field or
    is accounted for in the notes. A key that does neither is a knob tuned into a void."""
    raw = _shipped(tmp_path)
    hp, notes = from_mapping(raw)
    from spielplan.ledger.hyperparams import CORPUS_NAMES

    for key, value in raw.items():
        leaves = [key] if not isinstance(value, dict) else [f"{key}.{k}" for k in value]
        for leaf in leaves:
            field = CORPUS_NAMES.get(leaf, leaf)
            landed = field in hp.__dataclass_fields__ and field != "source"
            assert landed or any(leaf in n for n in notes), leaf


def test_an_unmeasured_constant_falls_back_instead_of_refusing_the_bundle():
    """JSON null is the corpus saying it has no measurement yet. Passing None through would trip
    the positivity check and refuse a merely incomplete bundle; §3.1 makes a documented default the
    answer, and the note says which.

    The σ-inflation RATE used to be this test's example and is now the one exception, because the
    rule it feeds has an off switch and nothing else here does: a null λ_bt still has to be some
    number for the objective to be written down, so there the default is the only answer
    (`test_a_null_sigma_inflation_rate_disables_the_inflation_and_notes_it` owns the other half).
    [M4.13 step 34b]
    """
    hp, notes = from_mapping({"bt_weight_lam_bt": None, "learning_rate": None})
    assert (hp.lambda_bt, hp.lr) == (DEFAULTS.lambda_bt, DEFAULTS.lr)
    assert sum("unmeasured" in n for n in notes) == 2


def test_the_provenance_string_is_not_read_as_a_constant(tmp_path):
    """`source` in the file names the script that tuned the numbers; `Hyperparams.source`
    records where this app read them from. Two different facts, one word."""
    hp, notes = from_mapping(_shipped(tmp_path))
    assert hp.source == "bundle"
    assert any("'source' is provenance" in n for n in notes)


# --- what the bundle says it has not measured -------------------------------------------------
#
# §4.3 ships nine constants and the shipped file leaves one of them null with `provisional: true`
# and the note "no measurement behind the rate yet - tune before use". This app read the flag,
# filed it as deliberately unused, and ran σ-inflation at its own 0.05 anyway: a default is a
# number somebody measured somewhere, and a null is the corpus saying nobody has.
# [M4.13 step 34b, cs-40]


def test_a_null_sigma_inflation_rate_disables_the_inflation_and_notes_it():
    """The shipped case, and the whole of it: rate null -> c = 0.0, loudly, and §5.2's rule then
    reports itself off rather than running at a rate this file invented.

    `model.inflate_sigma` is asserted here too, because "disabled" is a claim about the fit and not
    about the constant: at c = 0 the growth term is zero and the function returns σ unchanged, so
    no title's σ moves with neglect and none of it needs a second code path.
    """
    import numpy as np

    from spielplan.ledger import model

    hp, notes = from_mapping({"sigma_inflation": {"rate_c_per_sqrt_month": None}})
    assert hp.sigma_inflation_c == 0.0, "a null rate must not become this app's own 0.05"
    loud = [n for n in notes if "SIGMA-INFLATION DISABLED" in n]
    assert loud, f"the disabling has to be loud, not a footnote: {notes}"
    assert "rate_c_per_sqrt_month" in notes[0], "the note must name the key the corpus must fix"

    sigma = np.array([0.31, 0.48])
    prior = np.array([0.90, 0.90])
    neglected = np.array([60.0, 360.0])          # five and thirty years untouched
    held = model.inflate_sigma(sigma, prior, neglected, hp)
    assert held == pytest.approx(sigma), "c = 0 must leave every σ exactly where the fit put it"
    # And the control: the default rate still inflates, so this test cannot pass by the mechanism
    # being broken for everybody.
    grown = model.inflate_sigma(sigma, prior, neglected, DEFAULTS)
    assert grown[1] > sigma[1] and DEFAULTS.sigma_inflation_c == 0.05


def test_a_provisional_constant_disables_its_rule_rather_than_falling_back():
    """The flag alone is enough, even beside a number: `provisional: true` is the corpus saying
    the value it shipped is not a measurement, and the value is the thing this app must not use.

    Also the negative: `provisional: false` is a corpus that HAS measured the rate, and then the
    shipped number is exactly what §5.2 asks this app to run at.
    """
    hp, notes = from_mapping(
        {"sigma_inflation": {"rate_c_per_sqrt_month": 0.07, "provisional": True}}
    )
    assert hp.sigma_inflation_c == 0.0, "a provisional rate is not a rate"
    assert any("SIGMA-INFLATION DISABLED" in n for n in notes), notes
    assert any(hp_module.PROVISIONAL_KEY in n for n in notes), (
        "a flag that changes the fit may not be read silently"
    )

    measured, _notes = from_mapping(
        {"sigma_inflation": {"rate_c_per_sqrt_month": 0.07, "provisional": False}}
    )
    assert measured.sigma_inflation_c == 0.07

    # Refused rather than coerced, for `_BOOLEAN`'s reason: a string is truthy, so `"false"` would
    # disable the rule the corpus had just said was measured.
    with pytest.raises(ValueError, match="must be a boolean"):
        from_mapping({"sigma_inflation": {"provisional": "false"}})


def test_from_mapping_refuses_a_boolean_where_a_number_is_required():
    """`bool` is an `int` subclass, and three checks here asked `isinstance(v, int | float)`.

    `{"anchor_ridge_lambda": true, "steps": true}` therefore arrived as λ = 1.0 with one step, under
    `source = "bundle"` and an `hp_digest` that looks like any other - a fit nobody could explain,
    from a file nobody would suspect, because the report said the constants came from the bundle and
    they did. This module's comment on `margin_weighting` shows the intent it already had: a
    mistyped constant is refused, never coerced. [M4.13 step 34a, ml10]

    Step 34a enumerated THREE checks and there are four numeric gates plus two fields that were in
    no list at all, so the same `true` still got through in three places. `newton_tol = True` is
    1.0, which declares §5.2's Newton cutpoint solve converged on its first step for every
    household; `lr_min = True` is a step-size floor of 1, so the line search takes one halving and
    stops; `sigma_inflation_cap = True` caps every inflated sigma at 1.0 instead of at the title's
    own prior sigma - [0.31, 0.48] at 60 and 360 months comes out [0.4649, 1.0] where the sentinel
    gives [0.4649, 0.9]. The defect id is the class (`ml10-hyperparams-accept-booleans-as-numbers`),
    not the three instances that were listed. [M4.13 cycle 1, M413-R1-HP-03]
    """
    for payload, message in (
        ({"anchor_ridge_lambda": True}, "positive number"),      # the _POSITIVE loop
        ({"steps": True}, "positive integer"),                   # the step count
        ({"newton_max_iter": True}, "positive integer"),         # the solver's iteration cap
        ({"min_labels_for_cv": True}, "positive integer"),        # M4.13's own new count
        ({"gate_k": True}, "positive number"),
        ({"blend_beta_grid": [0.0, True]}, "non-negative numbers"),
        ({"margin_weight_form": True}, "margin_form"),
        # The three the enumeration missed: two fields in no check list, and the fourth gate.
        ({"newton_tol": True}, "positive number"),
        ({"lr_min": True}, "positive number"),
        ({"sigma_inflation_cap": True}, "sigma_inflation_cap"),
    ):
        with pytest.raises(ValueError, match=message):
            from_mapping(payload)
    # The one place a boolean IS the type, so the refusals above are about the mismatch and not
    # about booleans.
    assert from_mapping({"margin_weighting": False})[0].margin_weighting is False


def test_the_digest_is_the_same_for_twelve_and_twelve_point_zero():
    """JSON has one number type and this file has two. `digest()` hashed `json.dumps`, which wrote
    `12` for the int and `12.0` for the float - two digests for one set of constants, and
    `ledger_fit.hp_digest` is a precondition rather than a hint, so every cached fit in the install
    was discarded over a spelling nobody could see. [M4.13 step 34c]
    """
    for field, spellings in (
        ("steps", (12, 12.0)),
        ("newton_max_iter", (50, 50.0)),
        ("sigma_inflation_grace_months", (12, 12.0)),
        ("min_labels_for_cv", (5, 5.0)),
    ):
        digests = {dataclasses.replace(DEFAULTS, **{field: v}).digest() for v in spellings}
        assert len(digests) == 1, f"{field}: int and float spellings produced {digests}"
    # A list and a tuple of the same grid are the same constants too - `from_mapping` normalises to
    # a tuple, and this is what makes that normalisation matter rather than decorate.
    as_list = dataclasses.replace(DEFAULTS, blend_beta_grid=list(DEFAULTS.blend_beta_grid))
    assert as_list.digest() == DEFAULTS.digest()
    # And the digest still MOVES for a real change, or the two assertions above are satisfied by a
    # constant function.
    assert dataclasses.replace(DEFAULTS, steps=13).digest() != DEFAULTS.digest()


# --- the constants that lived in three other modules -------------------------------------------


def test_the_foldin_grid_the_gate_k_and_the_warm_threshold_are_bundle_constants():
    """§5.2: "every constant comes from `ledger_hyperparams.json`" - which is a rule about where
    the number LIVES, and eight of them lived elsewhere.

    `scoring/backbone.py` owned the evidence gate's k and the warm threshold; `scoring/foldin.py`
    owned β's ceiling and grid, λ's grid, §0's noise floor and the two label counts that decide
    whether to cross-validate at all. A corpus-side re-tune of any of them could not reach this
    app: `from_mapping` would have called every one an unknown hyperparameter and applied none.
    Today's values are the defaults, so nothing served moves. [M4.13 step 34d, dd14]
    """
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    assert (DEFAULTS.gate_k, DEFAULTS.warm_gate) == (10.0, 0.9)
    assert DEFAULTS.blend_beta_max == 0.8
    assert DEFAULTS.blend_beta_grid == tuple(i / 10 for i in range(11))
    assert DEFAULTS.foldin_lambda_grid == (1.0, 3.0, 10.0, 30.0, 100.0)
    assert (DEFAULTS.rho_noise_floor, DEFAULTS.min_labels_for_cv, DEFAULTS.loo_below_labels) == (
        0.008, 5, 25
    )

    # The modules read the field rather than carrying a copy of its value.
    assert (DEFAULTS.gate_k, DEFAULTS.warm_gate) == (bb.EVIDENCE_K, bb.WARM_GATE)
    assert DEFAULTS.gate_k * DEFAULTS.warm_gate / (1.0 - DEFAULTS.warm_gate) == bb.WARM_SUPPORT
    # 90 plus one ulp, because it is computed and not written (`test_scoring.py` records the same).
    assert bb.WARM_SUPPORT > 90.0 and bb.WARM_SUPPORT - 90.0 < 1e-9
    assert DEFAULTS.blend_beta_max == foldin.BETA_MAX
    assert DEFAULTS.blend_beta_grid == foldin.BETA_GRID
    assert DEFAULTS.foldin_lambda_grid == foldin.LAMBDA_GRID
    assert DEFAULTS.rho_noise_floor == foldin.NOISE_FLOOR
    assert DEFAULTS.min_labels_for_cv == foldin.MIN_LABELS_FOR_CV
    assert DEFAULTS.loo_below_labels == foldin.LOO_BELOW

    # A bundle that ships one of these names is parsed and range-checked into the field. It is
    # NOT yet served with: this comment claimed it was, and the claim was wrong in both halves.
    # `DEFAULTS = Hyperparams()` is the dataclass's own default instance, so a new process after
    # §10's restart re-binds 10.0 no matter what the bundle says, and no function in
    # `scoring/foldin.py` takes an `hp` at all. What the module does instead is SAY so, once per
    # re-tuned constant, because a knob that is accepted in silence is this file's own named
    # defect ("exactly the kind of thing that looks like it is working").
    # [M4.13 cycle 2, M413-C2-DIM-HP-01]
    tuned, notes = from_mapping({"gate_k": 12.0, "blend_beta_grid": [0.0, 0.25, 0.5]})
    assert tuned.gate_k == 12.0 and tuned.blend_beta_grid == (0.0, 0.25, 0.5)
    assert not [n for n in notes if "unknown hyperparameter" in n], notes
    assert bb.EVIDENCE_K == DEFAULTS.gate_k == 10.0, (
        "the serving constant is an import-time binding of the DEFAULT, not of the loaded bundle"
    )
    assert bb.gate(30) == pytest.approx(0.75), "and the gate is computed with that same 10.0"
    named = [n for n in notes if "NOT YET APPLIED" in n]
    assert sorted(n.split("'")[1] for n in named) == ["blend_beta_grid", "gate_k"], named
    assert all("scoring." in n for n in named), (
        "the note has to name the reader that is still on the default, or it is not actionable"
    )
    assert all(n.isascii() for n in named), "an import report is read on a cp1252 console"
    # Every one of the eight, so a later milestone that threads one has exactly one list to edit.
    _, every = from_mapping({k: getattr(DEFAULTS, k) for k in hp_module._PARSED_NOT_THREADED})
    assert len([n for n in every if "NOT YET APPLIED" in n]) == 8, every
    # And a constant that DOES reach its reader must not be labelled: `lambda_ridge` is model.py's.
    _, ledger_side = from_mapping({"anchor_ridge_lambda": 2.5})
    assert not [n for n in ledger_side if "NOT YET APPLIED" in n], ledger_side
    # Each of the eight changes a fit, so each belongs in the digest `ledger_fit` compares.
    for field, value in (
        ("gate_k", 12.0), ("warm_gate", 0.8), ("blend_beta_max", 0.5),
        ("blend_beta_grid", (0.0, 0.5)), ("foldin_lambda_grid", (2.0,)),
        ("rho_noise_floor", 0.004), ("min_labels_for_cv", 8), ("loo_below_labels", 40),
    ):
        assert dataclasses.replace(DEFAULTS, **{field: value}).digest() != DEFAULTS.digest(), field


def test_the_model_line_reads_the_gate_k_the_scoring_stack_reads():
    """§6.0's why-numbers print `gate_k`, which IS §5.1's evidence k - and it was the literal 10,
    twice, in `home/shelves.py`, beside a `gate` the same cards report out of `title_prior`.

    Two spellings of one quantity: a re-tuned k would have moved every gate on every card and left
    both why-lines naming the old number, on the one surface whose rule is that a shelf cannot say
    why it exists unless the why is true. The source scan is the assertion, because the defect is
    not a wrong value today - the literal WAS 10 - but a second place to change. [M4.13 step 34d]
    """
    from spielplan.home import shelves
    from spielplan.scoring import backbone as bb

    source = Path(shelves.__file__).read_text(encoding="utf-8")
    assert '"gate_k": 10' not in source, "the literal is back; §6.0's k has two spellings again"
    assert source.count('"gate_k": DEFAULTS.gate_k') == 2, (
        "both why-number dicts - `top_of_ledger` and `new_in_library` - read the field"
    )
    assert DEFAULTS.gate_k == bb.EVIDENCE_K
    # And the gate the cards carry is computed from that same k, so the printed number describes
    # the printed gate.
    assert bb.gate(DEFAULTS.gate_k) == pytest.approx(0.5), (
        "gate(k) = 0.5 by construction; if this moves, `gate_k` is not the gate's k"
    )


# --- read once at boot, refused loudly ---------------------------------------------------------
#
# §4.3 makes `ledger_hyperparams.json` the single source of the §5.2 constants and §10 makes a
# bundle swap a restart, so nothing in a running process can change these numbers. Both halves of
# what went wrong follow from nobody acting on that: the lifespan never set `app.state.hyperparams`
# at all, so all three Ledger routers fell back to reading the file per request — a read, a
# `from_mapping` validation and a discarded note list on every tap, every board GET and every duel,
# with `load_cache` re-digesting the result afterwards; and `from_mapping`'s `ValueError` had no
# catcher between the file and the client, so one hand-edited or badly restored constant made the
# Rate and Rank surfaces 500 for everyone with nothing saying why. [M4.10 finding 10; ml06, perf-07]


@pytest.fixture
async def booted(db, pg_url, tmp_path, monkeypatch):
    """The real app over ASGI, with a bundle already active *before* the lifespan runs.

    `conftest.app` boots and then hands the test a client, which is one step too late for this
    property: the read under test happens inside the lifespan, so the staged files and the
    `artifact_bundle` row have to exist before it. The environment munging is conftest's and is
    here for conftest's reasons — `Settings` reads `.env` from the working directory, so a
    developer who followed the README would otherwise boot this test with their own connector
    configured. What is added is the staging.

    Yields `boot(mutate=None)`, where `mutate` is handed the built bundle root so a `break_*`
    helper from the fixture can be applied to it before it is staged — a bundle that was already
    broken when the operator imported it, which is the case §10's validate step misses today.
    """
    from spielplan.core.config import settings
    from tests.fixtures import make_bundle as fx

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    neutral = tmp_path / "no-dot-env"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    settings.cache_clear()

    stack = contextlib.AsyncExitStack()

    async def boot(mutate=None, *, version: str = "hp-v1"):
        from spielplan.app import create_app

        root = tmp_path / "bundle"
        fx.make_bundle(root, version=version)
        if mutate is not None:
            mutate(root)
        shutil.copytree(root / "artifacts", tmp_path / "artifacts" / version)
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) "
            "VALUES ($1, '{}'::jsonb, 'active')",
            version,
        )
        application = create_app()
        await stack.enter_async_context(application.router.lifespan_context(application))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        )
        stack.push_async_callback(client.aclose)
        return application, client

    try:
        yield boot
    finally:
        await stack.aclose()
        settings.cache_clear()


async def _admin(client) -> None:
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text


async def _titles(db, count: int = 8) -> None:
    """Owned films, written straight to the table: these tests are about what the surface reads
    from the *bundle*, and an import would take a minute to say the same thing."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned, overview) "
        "SELECT i, 'movie', 'Title ' || i, true, 'A film about ' || i "
        "FROM generate_series(1, $1) AS i",
        count,
    )


async def test_the_lifespan_reads_the_constants_once_and_the_rate_route_never_again(
    db, booted, monkeypatch
):
    """§4.3 + §10: one read per process, because a swap is a restart.

    The counter is on `hyperparams.load` rather than on the file, because the file read is only
    the cheapest third of what the per-request path did — `from_mapping` re-validates every
    constant and rebuilds the note list each time, and the caller then re-digests the result.
    """
    reads: list[object] = []
    real = hp_module.load

    def counting(store):
        reads.append(getattr(store, "version", None))
        return real(store)

    # Patched before the boot: the read under test is the lifespan's, and it is the one that must
    # happen exactly once.
    monkeypatch.setattr(hp_module, "load", counting)
    application, client = await booted()

    assert reads == ["hp-v1"], "the lifespan reads the active bundle's constants, once"
    cached = getattr(application.state, "hyperparams", None)
    assert cached is not None, "the boot must leave the constants on app.state"
    assert cached.source == "bundle"
    assert cached.lambda_bt == 0.3, "the fixture's tuned lambda_bt, not the 1.0 default"

    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    first = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 2}
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/api/rate/verdict", json={"card_token": first.json()["card"]["token"], "value": 1}
    )
    assert second.status_code == 200, second.text

    assert await db.fetchval("SELECT count(*) FROM verdict") == 2, "two real taps, not two no-ops"
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_an_unusable_basis_does_not_cost_the_constants_their_one_read(
    db, booted, monkeypatch
):
    """The per-request read came back for a reason that has nothing to do with the constants.

    `BackboneError` is a `RuntimeError`, and the lifespan read the basis and the constants in ONE
    `try` whose first handler takes it — so an unusable `backbone.npz`, a state `app.py`'s own
    comment declares supported ("a Backbone that fails to load degrades the scoring surfaces rather
    than stopping a boot the admin needs in order to fix the bundle"), skipped the constants read
    entirely. `app.state.hyperparams` stayed unset and all three Ledger routers went back to
    reading, re-validating and re-digesting the file on every tap, every board GET and every duel,
    with §4.3's provenance notes never logged either. The ordering argument survives the split: the
    basis is still read first, so a bad constant cannot degrade scoring.

    `break_backbone_ids_unsorted` is one of several faults `Backbone.open` refuses and
    `importer/validate.py` does not check, so this is reachable from a bundle the importer accepts.
    [M4.10 cycle 1, M410-R1-04 / M410-C1-HP-1]
    """
    from tests.fixtures import make_bundle as fx

    reads: list[object] = []
    real = hp_module.load

    def counting(store):
        reads.append(getattr(store, "version", None))
        return real(store)

    monkeypatch.setattr(hp_module, "load", counting)
    application, client = await booted(fx.break_backbone_ids_unsorted)

    assert application.state.backbone.is_empty, (
        "the fixture's mutation did not make the basis unusable, so this test measured nothing"
    )
    assert reads == ["hp-v1"], "the lifespan never read the constants at all"
    cached = getattr(application.state, "hyperparams", None)
    assert cached is not None and cached.source == "bundle"
    assert cached.lambda_bt == 0.3, "the fixture's tuned lambda_bt, not the 1.0 default"

    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 200, tap.text
    assert reads == ["hp-v1"], f"the routes re-read the file {len(reads) - 1} more time(s)"


async def test_a_constants_file_that_is_not_an_object_degrades_the_boot_rather_than_killing_it(
    db, booted, caplog
):
    """§3.1 keeps a half-configured boot legal, and that is what this file may cost at most.

    A top-level array or `null` raised `AttributeError` out of `from_mapping`, which neither of the
    lifespan's handlers catches, so uvicorn logged "Application startup failed" and exited: the
    container crash-loops and `/api/admin/*`, `/api/setup/*` and the Data tab — the only in-app
    route to importing a bundle that parses — are all unreachable. That is strictly worse than what
    the same file cost before this milestone read it at boot, and it contradicts the promise written
    three lines above the read. The shape check in `from_mapping` makes the refusal a `ValueError`,
    which is the class this clause was written for. [M4.10 cycle 1, M410-R1-02]
    """

    def not_an_object(root):
        (root / "artifacts" / "ledger_hyperparams.json").write_text("[]", encoding="utf-8")

    application, client = await booted(not_an_object)

    assert (await client.get("/api/health")).status_code == 200, "the boot is not refused"
    assert getattr(application.state, "hyperparams", None) is None, (
        "a file `from_mapping` refused must not be cached as if it had loaded"
    )
    assert "ledger_hyperparams.json" in caplog.text
    # And the routes refuse the way the designed refusal does, rather than 500-ing.
    await _admin(client)
    await _titles(db)
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0


async def test_a_constant_the_fit_cannot_use_is_a_503_with_a_reason_and_not_a_500(
    db, booted, caplog
):
    """§4.3's refusal has to reach the person, and it may not reach them as a substituted number.

    `fx.break_straddle_z` is §6.3's threshold at zero — `from_mapping` refuses it, and until now
    that `ValueError` travelled from a file read inside a request handler all the way out: every
    Rate write and every Rank board answered 500 to everyone in the household, with the key named
    in no message anybody could see. Defaulting instead would be worse than either: the defaults
    carry a different `hp_digest`, so every cached fit in the install would be discarded and
    re-fitted behind a constant nobody chose.

    Rate goes over HTTP because the tap is the thing that must not write. Tonight had a reader of
    its own and it was asserted here beside Rate's; decision 214 deleted it, because §6.2's round
    now tests its shortlist boundary against `round.BOUNDARY_Z` and reads no bundle constant at
    all — so there is no longer a Tonight surface this refusal can reach, which is a narrowing of
    what the constants file can break rather than a hole in what is asserted about it.
    (`api/rank.py`'s copy of the same reader belongs to M4.10's Rank-route step and is asserted
    beside that change.)
    """
    from tests.fixtures import make_bundle as fx

    application, client = await booted(fx.break_straddle_z)

    assert getattr(application.state, "hyperparams", None) is None, (
        "a constant set `from_mapping` refused must not be cached as if it had loaded"
    )
    assert "straddle_z" in caplog.text, "the boot log has to name the key the operator must fix"

    await _admin(client)
    await _titles(db)

    # A real card, so this is a real tap being refused rather than a token the route would have
    # rejected anyway — and §4.2's journal stays empty, because the refusal precedes the write.
    card = (await client.get("/api/rate")).json()["card"]
    tap = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    assert tap.status_code == 503, tap.text
    assert "ledger constants" in tap.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0
