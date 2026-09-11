"""The tuned constants of the §5.2 recipe. Spec v2.1 §4.3, §5.2.

§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … Per-user cutpoints
and per-arm sensitivities are **not** shipped — they are fitted in-app by design."

Two rules give this module its shape:

  1. **Every constant comes from the bundle.** This is the only module in the package allowed
     to contain a tuning number. If a λ appears anywhere else, it has escaped the file the
     corpus project re-tunes it in, and re-tuning stops reaching the app. Eight such numbers had:
     the evidence gate's k and the warm threshold in `scoring/backbone.py`, the fold-in's β and
     λ grids, its noise floor and its two label counts in `scoring/foldin.py`, and §6.0's `gate_k`
     twice over in `home/shelves.py` as a second spelling of the first. They are fields below
     now, with today's values as their defaults, and those three modules read them from here --
     from `DEFAULTS`, at import time, which is the half of rule 1 that landed. A bundle that
     re-tunes one of the eight is still not served with it; the block beside the fields says
     exactly what is and is not delivered, and `from_mapping` notes the gap rather than letting
     the knob look applied. [M4.13 step 34d, dd14/ml10; cycle 2, M413-C2-DIM-HP-01]
  2. **A bundle-less household can still rate.** §3.1 makes an empty artifact store legal, so
     absent constants fall back to documented defaults rather than refusing to fit. The fit
     records *which* it used, because a number from a default and the same number from a
     bundle mean different things when someone is reading a refit report.

The digest is a precondition, not a hint: a cached fit built under other constants is wrong,
not stale, so `ledger_fit.hp_digest` is compared before the cache is trusted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

# §4.3 names the shipped constants. Every default here is either the spec's own number or a
# value chosen once, in this file, with its reason next to it.
MARGIN_FORMS: tuple[str, ...] = ("margin/mean(margin)", "none")

# §4.3: "Per-user cutpoints and per-arm sensitivities are not shipped — they are fitted in-app
# by design." A bundle that ships them anyway is not rejected (it is a corpus-side mistake, not
# a landmine), but the values are ignored and the fact is reported.
# Anchored on whole words, because `cutpoint_prior_precision` IS one of this app's own
# constants — a substring match on "cutpoint" swallowed it, so the one knob the corpus
# project would reach for to damp a crossing could never arrive from a bundle.
PER_USER_KEY = re.compile(
    r"^(cutpoints?|boundaries|boundary|sensitivities|sensitivity|per_user\w*)$", re.I
)

# The corpus's own spelling for the constants this app already has a field for. Rule 1 above —
# "every constant comes from the bundle" — fails *silently* without this map: the shipped
# `ledger_hyperparams.json` tunes λ_bt to 0.3 and the learning rate to 0.5, and under this
# app's spellings both arrive as the defaults 1.0 and 0.1 while the fit reports source="bundle".
# Dotted keys are the corpus's one nested object, flattened by `_flatten` below.
CORPUS_NAMES: dict[str, str] = {
    "anchor_ridge_lambda": "lambda_ridge",
    "bt_weight_lam_bt": "lambda_bt",
    "learning_rate": "lr",
    "margin_weight_form": "margin_form",
    "sigma_inflation.trigger_months": "sigma_inflation_grace_months",
    "sigma_inflation.rate_c_per_sqrt_month": "sigma_inflation_c",
    "sigma_inflation.cap": "sigma_inflation_cap",
}

# The corpus states two of the values as prose where this app states them as an identifier.
# Mapped exactly, never by prefix: a corpus-side change to a genuinely different margin form
# must still hit the `MARGIN_FORMS` refusal rather than be read as this one.
CORPUS_VALUES: dict[str, dict[Any, Any]] = {
    "margin_form": {"w = margin / mean(margin); 1.0 when disabled": "margin/mean(margin)"},
    "sigma_inflation_cap": {"prior_sigma": "prior"},
}

# Constants of the corpus's §5.2 recipe that this app's fit has no term for. They are named
# here rather than reported as unknown because the two facts are different: "unknown" means a
# key nobody recognises (a typo, or a knob tuned into a void), while these are recognised and
# deliberately unused. Naming them keeps rule 1 honest — the gap is declared, finite and
# reviewable — without inventing dataclass fields that no line of `model.py` reads.
NOT_IMPLEMENTED: frozenset[str] = frozenset({
    "logit_clip", "item_prior_shrink", "user_offset_shrink_lam",
    "sigma_inflation.note",
})

# The corpus's own do-not-use flag, and the one key in that object which is neither a constant
# nor unused. It sat in `NOT_IMPLEMENTED` above, so the shipped bundle's
# `{"rate_c_per_sqrt_month": null, "provisional": true, "note": "no measurement behind the rate
# yet - tune before use"}` was READ, filed as deliberately unused, and sigma-inflation went on
# running at this file's own 0.05 - rule 2's "records *which* it used" inverted, because a
# default is a number somebody measured somewhere and a null is the corpus saying nobody has.
# `from_mapping` therefore sets `sigma_inflation_c` to 0.0 and says so loudly; §5.2's rule then
# reports itself disabled rather than inventing its own rate, and `model.inflate_sigma` returns
# sigma unchanged at c = 0 already, so no other line changes.
#
# What the disabled rule costs is bounded and measured: at 0.05 a title needs 221-394 months to
# reach the cap, and one year past §5.2's 12-month grace period moves sigma by at most 0.012 -
# less than the width of a tier boundary. [M4.13 step 34b, cs-40]
PROVISIONAL_KEY = "sigma_inflation.provisional"
RATE_FIELD = "sigma_inflation_c"


@dataclass(frozen=True)
class Hyperparams:
    # --- shipped by §4.3 ---------------------------------------------------------------
    lambda_ridge: float = 3.0          # "anchor (ridge) strength λ (currently 3.0)"
    lambda_bt: float = 1.0             # "BT weight λ_bt"
    steps: int = 200                   # "step count"
    lr: float = 0.1                    # "learning rate"
    margin_weighting: bool = True      # "margin-weighting flag"
    margin_form: str = "margin/mean(margin)"   # "+ functional form"
    tie_prior_delta0: float = 0.22     # "tie-prior initialisation δ₀ = 0.22 (thereafter fitted)"
    b_i_tau: float = 1.0               # "b_i prior τ (or its CV grid)"
    sigma_inflation_c: float = 0.05    # "σ-inflation rate constant"
    sigma_inflation_cap: float | Literal["prior"] = "prior"   # "and cap"

    # --- not shipped; fixed here, once, with the reason --------------------------------
    # §5.2: "after 12 months untouched, a title's σ inflates". The grace period is the spec's.
    sigma_inflation_grace_months: float = 12.0
    # §6.1: "a persistent decisive toggle sets the margin weight (~1.6 vs 1.0)".
    margin_decisive: float = 1.6
    margin_hesitant: float = 1.0
    # μ is otherwise unidentified against a free cutpoint set; this pins the location without
    # touching any ordering.
    mu_prior_tau: float = 2.0
    # Keeps the cutpoint of a tier level nobody has used finite. §6.3's measured tier shape is
    # the prior *mean*, so an unused level sits where the crowd puts it rather than at ±∞.
    cutpoint_prior_precision: float = 1.0
    tie_prior_precision: float = 1.0
    # §6.3: a posterior within this many σ of a boundary is an "A/S straddle".
    straddle_z: float = 1.0
    # §6.3: "if the model disagrees strongly, the title's badge shows the tension rather than
    # snapping back". "Strongly" is operationally the 80% credible interval — the tier the
    # person assigned and the posterior's interval are disjoint. A probability rather than a σ
    # multiple because that is the form the rule is stated in; `straddle_z` is a σ multiple
    # because §6.3 states *that* one as "the posterior reaches the next tier". Both live here
    # so neither is a literal inside a board renderer.
    tension_credible_mass: float = 0.80
    newton_tol: float = 1e-9
    newton_max_iter: int = 50
    lr_min: float = 1e-6

    # --- the serving stack's constants, moved here and not shipped either ----------------
    # Fields, exactly like `straddle_z` above: §5.2 says "every constant comes from
    # `ledger_hyperparams.json`", and a number that lives as a module literal in another package
    # cannot come from anywhere. Every default is today's value, so nothing served moves; what
    # changes is that the eight numbers have ONE home and are range-checked on the way in, and
    # that §6.0's `gate_k` is the same object as the gate's own k rather than a literal 10
    # standing beside it. The grids are tuples because `Hyperparams` is frozen and hashed.
    # [M4.13 step 34d]
    #
    # WHAT IT IS NOT, YET. This block used to claim that "a bundle which ships one of these names
    # now reaches the stack that uses it". It does not, and §10's restart does not make it so:
    # `backbone.EVIDENCE_K`, `WARM_GATE`, `WARM_SUPPORT`, `foldin.BETA_MAX`/`BETA_GRID`/
    # `LAMBDA_GRID`/`NOISE_FLOOR`/`MIN_LABELS_FOR_CV`/`LOO_BELOW` and both of `home/shelves.py`'s
    # why-number dicts bind `DEFAULTS.<field>` at IMPORT time, and a new process re-evaluates
    # `DEFAULTS = Hyperparams()`, which is the dataclass's own defaults and never the bundle's.
    # No function in `scoring/foldin.py` takes an `hp` at all. So a bundle shipping `gate_k: 12.0`
    # is parsed, validated, digested -- and served at k = 10. Threading `hp` to those eight
    # readers is a change of shape (a parameter on `backbone.gate`, a `warm_support(hp)` helper
    # for `backbone`/`reconcile`/`observations`, an `hp` on `fit_user`/`_cross_validate`/
    # `refit_user`/`run`), which step 34d's "they are dataclass fields ... not a new abstraction"
    # rules out for this milestone; until it happens `from_mapping` SAYS so in a note rather than
    # accepting the knob in silence, which is rule 1's whole point. `app.py:279` is the one
    # reader already on `hp` -- it prints the boot yardstick against `hp.rho_noise_floor` while
    # `shelves.py` reads `DEFAULTS.rho_noise_floor` for the same band -- and that split is the
    # shape of the gap rather than an inconsistency to level.
    # [M4.13 cycle 2, M413-C2-DIM-HP-01]
    gate_k: float = 10.0               # §5.1: "gate = n_t / (n_t + k)", k ~ 10
    warm_gate: float = 0.9             # where "rated (warm)" starts; n_t = k*g/(1-g) = 90
    blend_beta_max: float = 0.8        # §5.1's ceiling, i.e. a floor of a fifth on the crowd
    blend_beta_grid: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    foldin_lambda_grid: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0)
    rho_noise_floor: float = 0.008     # §0: "pipeline variance 0.003-0.008; smaller is a tie"
    min_labels_for_cv: int = 5         # §0/§6.1's learning curve starts at five labels
    loo_below_labels: int = 25         # leave-one-out under this many, 5 folds at or above

    source: Literal["bundle", "default"] = "default"

    # --- derived -----------------------------------------------------------------------

    def nu0(self) -> float:
        """Davidson's ν at d = 0 for the measured tie rate.

        P(TIE | d=0) = ν/(2+ν), so ν₀ = 2δ₀/(1−δ₀). §4.3 ships δ₀ = 0.22 as an
        *initialisation*: ν is fitted thereafter, which is why this is a starting point and a
        prior mean rather than a constant in the objective.
        """
        return 2.0 * self.tie_prior_delta0 / (1.0 - self.tie_prior_delta0)

    def margin_for(self, decisive: bool) -> float:
        return self.margin_decisive if decisive else self.margin_hesitant

    def tension_z(self) -> float:
        """§6.3's "disagrees strongly", as a σ multiple.

        The Ledger's σ is a Laplace (Gaussian) posterior sd, so a central credible interval of
        mass `m` is ±Φ⁻¹((1+m)/2)·σ — 1.2816 at the default 0.80. `statistics` rather than
        scipy because scipy is not a dependency and this is one stdlib call, not a numerics
        library; `model.py` stays numpy-only for the arithmetic that matters.
        """
        from statistics import NormalDist

        return float(NormalDist().inv_cdf(0.5 * (1.0 + self.tension_credible_mass)))

    def digest(self) -> str:
        """A stable hash of everything that changes a fit. `source` is excluded on purpose: the
        same constants from a bundle and from the defaults produce the same fit, and a cache
        invalidated by provenance alone would be thrown away for no numerical reason.

        Numerics are normalised to float first, because JSON has one number type and this file
        has two: `{"steps": 12}` and `{"steps": 12.0}` are the same constant, and `json.dumps`
        wrote `12` for one and `12.0` for the other - two digests, and `ledger_fit.hp_digest` is
        a precondition, so every cached fit in the install was discarded over a spelling nobody
        could see. A re-tune that changes no number must change no digest. [M4.13 step 34c]

        THE EIGHT UNTHREADED SERVING CONSTANTS ARE IN, deliberately, even though none of them can
        move a `ledger_fit` number today (see `_PARSED_NOT_THREADED`). Excluding them would make
        this hash a statement about what the code currently happens to read, and the next
        milestone that threads `gate_k` into the gate would have to remember to put it back -- a
        silent wrong cache, against a wasted one. The wasted one is also bounded: §10 makes a
        bundle swap a restart and `load_cache` refuses on `bundle_version` first, so the only path
        where a changed digest costs an identical refit is a hand-edit of the ACTIVE bundle's
        `ledger_hyperparams.json` in place. [M4.13 cycle 2, M413-C2-DIM-HP-01]
        """
        payload = {k: _numeric(v) for k, v in asdict(self).items() if k != "source"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]


DEFAULTS = Hyperparams()

_POSITIVE = (
    "lambda_ridge", "lambda_bt", "b_i_tau", "mu_prior_tau", "lr",
    "cutpoint_prior_precision", "tie_prior_precision", "sigma_inflation_grace_months",
    "sigma_inflation_c", "margin_decisive", "margin_hesitant", "straddle_z",
    # M4.13's block: `gate_k` divides into (n_t + k), `blend_beta_max` is a weight and
    # `rho_noise_floor` is a tie band - a zero in any of the three disables a rule in silence.
    "gate_k", "blend_beta_max", "rho_noise_floor",
    # The two solver knobs that were in NO list at all, so `known` applied them from a bundle
    # unchecked: `{"newton_tol": true}` is 1.0, which declares §5.2's cutpoint solve converged on
    # its first step for every household, and `lr_min` at 0 makes `model.py`'s
    # `while eta >= hp.lr_min` non-terminating once the halving reaches 0.0. Both are strictly
    # positive by construction - a tolerance and a floor on a step size.
    # [M4.13 cycle 1, M413-R1-HP-03]
    "newton_tol", "lr_min",
)
_BOOLEAN = ("margin_weighting",)
# Parsed, validated, hashed - and read by nobody outside this module. §5.2's rule 1 is "every
# constant comes from the bundle", and step 34d delivered the first half of it for these eight:
# they have one home here instead of being literals in `scoring/` and `home/`. The second half,
# a loaded `Hyperparams` actually reaching those readers, is a threading change step 34d's own
# "not a new abstraction" rules out - so every one of them still binds `DEFAULTS.<field>` at
# import, and §10's restart re-binds the same default rather than the bundle's number.
#
# Until that lands, a bundle that ships one is told. These names used to produce "unknown
# hyperparameter 'gate_k' - not applied", which was true; making them fields removed the note and
# left the knob looking applied, which is the exact silence this module's docstring names as
# "the kind of thing that looks like it is working". Not a refusal: the value is still parsed,
# range-checked and digested, so the moment the threading lands the note is the only line that
# has to go. [M4.13 cycle 2, M413-C2-DIM-HP-01]
_PARSED_NOT_THREADED: dict[str, str] = {
    "gate_k": "scoring.backbone.EVIDENCE_K",
    "warm_gate": "scoring.backbone.WARM_GATE",
    "blend_beta_max": "scoring.foldin.BETA_MAX",
    "blend_beta_grid": "scoring.foldin.BETA_GRID",
    "foldin_lambda_grid": "scoring.foldin.LAMBDA_GRID",
    "rho_noise_floor": "scoring.foldin.NOISE_FLOOR",
    "min_labels_for_cv": "scoring.foldin.MIN_LABELS_FOR_CV",
    "loo_below_labels": "scoring.foldin.LOO_BELOW",
}
# One tuple and one loop, where there were two hand-written checks and two names with none. The
# refusal is a single sentence; spelling it per field is how `True` got past two of them.
_POSITIVE_INT = ("steps", "newton_max_iter", "min_labels_for_cv", "loo_below_labels")
_GRIDS = ("blend_beta_grid", "foldin_lambda_grid")


def _numeric(value: Any) -> Any:
    """`digest()`'s normaliser: every number as a float, every sequence elementwise.

    `bool` is excluded explicitly rather than by luck. It is an `int` subclass, so float(True)
    would hash `margin_weighting` as 1.0 and collide a flag with a count.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list | tuple):
        return [_numeric(v) for v in value]
    return value


def _positive_number(value: Any) -> bool:
    """A number the fit can use - and JSON `true` is not one.

    `bool` is an `int` subclass, so `isinstance(True, int | float) and True > 0` holds:
    `{"anchor_ridge_lambda": true, "steps": true}` arrived as lambda = 1.0 with one step, under
    `source = "bundle"` and a digest that looks like every other. The comment on `_BOOLEAN` below
    records the intent this file already had - a mistyped constant is refused, never coerced -
    and the three numeric checks did not carry it. [M4.13 step 34a, ml10]
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _positive_int(value: Any) -> bool:
    """The same refusal for the counts. `True` is an `int` whose value is 1."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _grid(key: str, value: Any) -> tuple[float, ...]:
    """A cross-validation grid, as a tuple of numbers.

    The two grids are the only sequence-valued constants here, and that is exactly why they are
    checked: a scalar or a string would otherwise reach numpy inside `_cross_validate`, a stack
    away from the file that read it and hours after the boot that accepted it. Normalised to a
    tuple of floats because this dataclass is frozen and `digest()` hashes what it holds.
    """
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{key} must be a non-empty list of numbers, got {value!r}")
    for item in value:
        if not (isinstance(item, int | float) and not isinstance(item, bool) and item >= 0):
            raise ValueError(f"{key} must hold non-negative numbers, got {item!r}")
    return tuple(float(v) for v in value)


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    """One level of the corpus's nesting, as dotted keys.

    §5.2's σ-inflation ships as a single object (`trigger_months`, `rate_c_per_sqrt_month`,
    `cap`) where this app carries three flat fields. Flattening beats a special case because
    the whole object was otherwise one `unknown hyperparameter 'sigma_inflation'` note hiding
    three constants — the exact silence rule 1 exists to prevent.
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out.update({f"{key}.{inner}": v for inner, v in value.items()})
        else:
            out[key] = value
    return out


def from_mapping(raw: dict[str, Any], *, source: str = "bundle") -> tuple[Hyperparams, list[str]]:
    """Build hyperparameters from a parsed `ledger_hyperparams.json`.

    Returns (hyperparams, notes). Unknown keys are reported rather than dropped silently — a
    constant the corpus project tuned and this app ignores is exactly the kind of thing that
    looks like it is working.

    The shape check is first, and it raises `ValueError` like every other refusal here because
    that is the class all four readers of this file catch: the lifespan (`app.py`), the three
    routers' `_hyperparams` fallbacks, and `importer/validate.validate_hyperparams`. `json.loads`
    accepts a top-level array, `null`, a number and a string, so `_flatten`'s `raw.items()` raised
    `AttributeError` for those — which is not a `ValueError`, and therefore escaped all four:
    the boot failed instead of degrading, and the Data tab's Validate button answered 500 with no
    report line on the one surface whose job is to report. `importer/validate._read_json` already
    applies exactly this check to `manifest.json` and `BUNDLE.json`; this file is read by
    `load` below rather than through it, so it needs its own.
    [M4.10 cycle 1, M410-R1-02 / M410-R1-03]
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"ledger_hyperparams.json must be a JSON object, got {type(raw).__name__}"
        )
    notes: list[str] = []
    known = {f for f in DEFAULTS.__dataclass_fields__ if f != "source"}
    fields: dict[str, Any] = {}
    # Two facts about one constant, collected in the loop and acted on after the checks below:
    # either of them disables sigma-inflation, and the positivity check would refuse the 0.0 that
    # does it. See `PROVISIONAL_KEY`.
    provisional = False
    unmeasured_rate = False

    for key, value in _flatten(raw).items():
        if PER_USER_KEY.search(key):
            # §4.3 is explicit that these are fitted in-app. Taking them from a bundle would
            # replace a per-user fit with somebody else's thresholds.
            notes.append(f"ignored per-user key {key!r} — §4.3 fits these in-app")
            continue
        if key == "source":
            # The corpus's provenance string, not a constant. `Hyperparams.source` records
            # where this app read the numbers from, which is a different fact and is set below.
            notes.append(f"{key!r} is provenance, not a constant: tuned by {value}")
            continue
        if key == PROVISIONAL_KEY:
            # Refused rather than coerced, for `_BOOLEAN`'s reason: a string is truthy, so
            # `"false"` here would read as "do not use this rate" and `"true"` as the same thing,
            # and the one spelling that means the opposite would be silently agreed with.
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean, got {value!r}")
            provisional = value
            notes.append(f"bundle marks {key!r} {'true' if value else 'false'}")
            continue
        if key in NOT_IMPLEMENTED:
            notes.append(f"{key!r} is tuned by the corpus and has no term in this app's fit")
            continue
        field = CORPUS_NAMES.get(key, key)
        if field not in known:
            notes.append(f"unknown hyperparameter {key!r} — not applied")
            continue
        if value is None:
            # JSON null is the corpus saying it has no measurement yet (the shipped bundle's
            # σ-inflation rate is exactly this). Overwriting the default with None would fail
            # the positivity check below and refuse a bundle that is merely incomplete.
            #
            # The rate is the one null this app can answer without guessing, because the rule it
            # feeds has an off switch: see `PROVISIONAL_KEY`. Every other unmeasured constant is
            # a term in the objective with no neutral value, so there the documented default is
            # the only answer and the note is what keeps it honest.
            if field == RATE_FIELD:
                unmeasured_rate = True
                notes.append(f"bundle leaves {key!r} unmeasured; the rule it feeds is disabled")
                continue
            notes.append(f"bundle leaves {key!r} unmeasured; default used")
            continue
        spellings = CORPUS_VALUES.get(field, {})
        fields[field] = spellings.get(value, value) if isinstance(value, str) else value

    for key in _POSITIVE:
        if key in fields and not _positive_number(fields[key]):
            raise ValueError(f"{key} must be a positive number, got {fields[key]!r}")
    for key in _BOOLEAN:
        # `"false"` is a string, and a string is truthy. A corpus-side decision to turn margin
        # weighting off would otherwise leave it silently on — precisely the "knob tuned into a
        # void" this module exists to prevent.
        if key in fields and not isinstance(fields[key], bool):
            raise ValueError(f"{key} must be a boolean, got {fields[key]!r}")
    for key in _POSITIVE_INT:
        if key in fields and not _positive_int(fields[key]):
            raise ValueError(f"{key} must be a positive integer, got {fields[key]!r}")
    for key in _GRIDS:
        if key in fields:
            fields[key] = _grid(key, fields[key])
    if "tie_prior_delta0" in fields and not 0.0 < fields["tie_prior_delta0"] < 1.0:
        raise ValueError("tie_prior_delta0 is a probability and must lie in (0, 1)")
    if "tension_credible_mass" in fields and not 0.0 < fields["tension_credible_mass"] < 1.0:
        # Not merely positive: at 1.0 the interval is the whole line and no title is ever in
        # tension, which is a silently disabled badge rather than a loud misconfiguration.
        raise ValueError("tension_credible_mass is a probability and must lie in (0, 1)")
    if "margin_form" in fields and fields["margin_form"] not in MARGIN_FORMS:
        raise ValueError(
            f"margin_form must be one of {MARGIN_FORMS}, got {fields['margin_form']!r}"
        )
    cap = fields.get("sigma_inflation_cap")
    # `_positive_number` and not a fourth hand-written `isinstance(...) and > 0`: this was the one
    # numeric gate step 34a's enumeration missed, and `cap = true` therefore capped every inflated
    # sigma at 1.0 instead of at the title's prior sigma - measured on [0.31, 0.48] at 60 and 360
    # months as [0.4649, 1.0] against the correct [0.4649, 0.9]. One refusal, four gates.
    # [M4.13 cycle 1, M413-R1-HP-03]
    if cap is not None and cap != "prior" and not _positive_number(cap):
        raise ValueError("sigma_inflation_cap must be 'prior' or a positive number")
    if "warm_gate" in fields and not 0.0 < fields["warm_gate"] < 1.0:
        # A probability, and not merely positive: `WARM_SUPPORT` is k*g/(1-g), so 1.0 is a
        # zero-divide and anything above it is a negative support threshold that makes every
        # title warm. Both are a disabled blend rather than a loud refusal.
        raise ValueError("warm_gate is a probability and must lie in (0, 1)")
    if "blend_beta_max" in fields and not 0.0 < fields["blend_beta_max"] <= DEFAULTS.blend_beta_max:
        # The ceiling is in the schema too: `0009_scoring.sql:65` is
        # `CHECK (blend_beta <= 0.8::real)`, and a migration is sha256-checksummed. A bundle that
        # raised this would not serve more personal rankings - it would fail the nightly fit's
        # INSERT for every person whose cross-validation reached the new ceiling, which is the
        # failure mode 0009's own comment was written about. Refused here, where the operator is
        # reading an import report, rather than at 03:00 in a job nobody is watching.
        raise ValueError(
            f"blend_beta_max must lie in (0, {DEFAULTS.blend_beta_max}] - 0009_scoring.sql "
            "constrains user_vector.blend_beta to that ceiling and migrations are immutable"
        )

    if provisional or unmeasured_rate:
        # After the checks, because 0.0 is what `_POSITIVE` exists to refuse. Loud because the
        # alternative was silent: §5.2's rule ran at this file's 0.05 while the bundle said, in
        # two keys, that nobody had measured one.
        fields[RATE_FIELD] = 0.0
        notes.append(
            "SIGMA-INFLATION DISABLED: the bundle ships no measured rate "
            "(sigma_inflation.rate_c_per_sqrt_month null or provisional), so sigma_inflation_c "
            "is 0.0 and no title's sigma grows with neglect until the corpus measures one "
            "(spec section 5.2)"
        )

    unthreaded = sorted(set(fields) & set(_PARSED_NOT_THREADED))
    if unthreaded and source == "bundle":
        # One line per constant rather than one summary line: the operator reading an import
        # report is looking for the name they re-tuned, and a count tells them nothing.
        notes.extend(
            f"{key!r} is parsed and range-checked but NOT YET APPLIED: "
            f"{_PARSED_NOT_THREADED[key]} binds the default {getattr(DEFAULTS, key)!r} at import "
            "and no loaded Hyperparams reaches it (spec section 5.2)"
            for key in unthreaded
        )

    missing = sorted(known - set(fields))
    if missing and source == "bundle":
        notes.append(f"bundle omits {len(missing)} constant(s); defaults used: {', '.join(missing)}")
    return replace(DEFAULTS, **fields, source=source), notes


def load(store: Any) -> tuple[Hyperparams, list[str]]:
    """Read the active bundle's constants, or fall back to the documented defaults.

    §3.1 makes a bundle-less app a legal state, so a household can rate before any corpus
    export exists. What it must not do is pretend the numbers came from somewhere.

    `exists()` and not `is_file()`, which is the distinction between "the bundle ships no
    constants" and "the constants are there and unreadable". `is_file()` is false for a path that
    is a directory or a dangling symlink — a `tar` extraction or an out-of-band copy makes both —
    and it read those as absent: DEFAULTS served under a note saying the bundle ships no file, and
    `validate_hyperparams` emitting no line at all. That is the silent substitution §4.3 and this
    milestone refuse by name, because a different `hp_digest` discards every cached fit in the
    install. Any path that is there at all therefore falls through to `read_text`, whose `OSError`
    the lifespan and the validator both report — `is_symlink` beside `exists` because `exists`
    follows the link and a dangling one is present on disk while naming nothing.
    [M4.10 cycle 1, M410-R1-06]
    """
    if store is None or getattr(store, "is_empty", True):
        return DEFAULTS, ["no artifact bundle — §5.2 constants are this app's defaults"]
    path = store.path("ledger_hyperparams.json")
    if not (path.exists() or path.is_symlink()):
        return DEFAULTS, ["bundle ships no ledger_hyperparams.json — defaults used"]
    return from_mapping(json.loads(path.read_text(encoding="utf-8")), source="bundle")


__all__ = [
    "CORPUS_NAMES", "CORPUS_VALUES", "DEFAULTS", "MARGIN_FORMS", "NOT_IMPLEMENTED",
    "PROVISIONAL_KEY", "RATE_FIELD", "Hyperparams", "from_mapping", "load",
]
