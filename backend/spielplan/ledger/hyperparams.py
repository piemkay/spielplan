"""The tuned constants of the §5.2 recipe, from `ledger_hyperparams.json` (§4.3).

Absent constants fall back to defaults (§3.1) and the fit records which it used. A cached fit is
trusted only under the same `digest()`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

MARGIN_FORMS: tuple[str, ...] = ("margin/mean(margin)", "none")

# §4.3: per-user cutpoints and sensitivities are fitted in-app; shipped ones are ignored and noted.
# Whole-word anchored, or it would swallow `cutpoint_prior_precision`.
PER_USER_KEY = re.compile(
    r"^(cutpoints?|boundaries|boundary|sensitivities|sensitivity|per_user\w*)$", re.I
)

# The corpus's spelling -> this app's field. Without it, shipped values silently fall to defaults.
# Dotted keys come from `_flatten`.
CORPUS_NAMES: dict[str, str] = {
    "anchor_ridge_lambda": "lambda_ridge",
    "bt_weight_lam_bt": "lambda_bt",
    "learning_rate": "lr",
    "margin_weight_form": "margin_form",
    "sigma_inflation.trigger_months": "sigma_inflation_grace_months",
    "sigma_inflation.rate_c_per_sqrt_month": "sigma_inflation_c",
    "sigma_inflation.cap": "sigma_inflation_cap",
}

# Exact matches only, so a genuinely different margin form still hits the `MARGIN_FORMS` refusal.
CORPUS_VALUES: dict[str, dict[Any, Any]] = {
    "margin_form": {"w = margin / mean(margin); 1.0 when disabled": "margin/mean(margin)"},
    "sigma_inflation_cap": {"prior_sigma": "prior"},
}

# Recognised corpus constants this app's fit has no term for (distinct from unknown keys).
NOT_IMPLEMENTED: frozenset[str] = frozenset({
    "logit_clip", "item_prior_shrink", "user_offset_shrink_lam",
    "sigma_inflation.note",
})

# The corpus's do-not-use flag. A provisional or null rate sets `sigma_inflation_c` to 0.0, which
# disables §5.2's inflation rather than running it on an unmeasured default.
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
    # "b_i prior τ". Not shipped; decision 509: at 1.0 a verdict could not outweigh the taste vector.
    b_i_tau: float = 2.0
    sigma_inflation_c: float = 0.05    # "σ-inflation rate constant"
    sigma_inflation_cap: float | Literal["prior"] = "prior"   # "and cap"

    # --- not shipped ------------------------------------------------------------------
    sigma_inflation_grace_months: float = 12.0
    margin_decisive: float = 1.6
    margin_hesitant: float = 1.0
    # Pins μ, otherwise unidentified against free cutpoints, without touching any ordering.
    mu_prior_tau: float = 2.0
    # Keeps an unused tier level's cutpoint finite, at §6.3's measured shape rather than ±∞.
    cutpoint_prior_precision: float = 1.0
    tie_prior_precision: float = 1.0
    # §6.3's straddle badge, in σ; also queue eligibility. Tuned so a typical board badges ~1/4.
    straddle_z: float = 0.15
    # §6.3's "disagrees strongly": the assigned tier lies outside this credible interval.
    tension_credible_mass: float = 0.80
    newton_tol: float = 1e-9
    newton_max_iter: int = 50
    lr_min: float = 1e-6

    # --- the serving stack's constants -------------------------------------------------
    # Their readers bind `DEFAULTS.<field>` at import, so a bundle value is parsed but not served;
    # see `_PARSED_NOT_THREADED`. Grids are tuples because the dataclass is frozen and hashed.
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
        """Davidson's ν at d = 0: P(TIE | d=0) = ν/(2+ν), so ν₀ = 2δ₀/(1−δ₀). A prior mean, not fixed."""
        return 2.0 * self.tie_prior_delta0 / (1.0 - self.tie_prior_delta0)

    def margin_for(self, decisive: bool) -> float:
        return self.margin_decisive if decisive else self.margin_hesitant

    def tension_z(self) -> float:
        """§6.3's "disagrees strongly" as a σ multiple: Φ⁻¹((1+m)/2), 1.2816 at m = 0.80."""
        from statistics import NormalDist

        return float(NormalDist().inv_cdf(0.5 * (1.0 + self.tension_credible_mass)))

    def digest(self) -> str:
        """A stable hash of everything that changes a fit, `source` excluded.

        Numbers are normalised to float, so `12` and `12.0` hash alike.
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
    "gate_k", "blend_beta_max", "rho_noise_floor",
    # `lr_min` at 0 makes `model.py`'s step-halving loop non-terminating.
    "newton_tol", "lr_min",
)
_BOOLEAN = ("margin_weighting",)
# Parsed, range-checked and digested, but their readers bind `DEFAULTS` at import. A bundle that
# ships one is told so in a note rather than left thinking it applied.
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
_POSITIVE_INT = ("steps", "newton_max_iter", "min_labels_for_cv", "loo_below_labels")
_GRIDS = ("blend_beta_grid", "foldin_lambda_grid")


def _numeric(value: Any) -> Any:
    """`digest()`'s normaliser: every number as a float, every sequence elementwise. Not bools."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, list | tuple):
        return [_numeric(v) for v in value]
    return value


def _positive_number(value: Any) -> bool:
    """A number the fit can use. JSON `true` is an int subclass and is refused, never coerced."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _positive_int(value: Any) -> bool:
    """The same refusal for the counts. `True` is an `int` whose value is 1."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _grid(key: str, value: Any) -> tuple[float, ...]:
    """A cross-validation grid, as a tuple of floats (the dataclass is frozen and hashed)."""
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{key} must be a non-empty list of numbers, got {value!r}")
    for item in value:
        if not (isinstance(item, int | float) and not isinstance(item, bool) and item >= 0):
            raise ValueError(f"{key} must hold non-negative numbers, got {item!r}")
    return tuple(float(v) for v in value)


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    """One level of the corpus's nesting (the σ-inflation object), as dotted keys."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out.update({f"{key}.{inner}": v for inner, v in value.items()})
        else:
            out[key] = value
    return out


def from_mapping(raw: dict[str, Any], *, source: str = "bundle") -> tuple[Hyperparams, list[str]]:
    """Build hyperparameters from a parsed `ledger_hyperparams.json`. Returns (hp, notes).

    Every refusal is a `ValueError`, the one class all callers catch; unknown keys become notes.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"ledger_hyperparams.json must be a JSON object, got {type(raw).__name__}"
        )
    notes: list[str] = []
    known = {f for f in DEFAULTS.__dataclass_fields__ if f != "source"}
    fields: dict[str, Any] = {}
    # Acted on after the positivity checks, which would refuse the 0.0 they set.
    provisional = False
    unmeasured_rate = False

    for key, value in _flatten(raw).items():
        if PER_USER_KEY.search(key):
            notes.append(f"ignored per-user key {key!r} — §4.3 fits these in-app")
            continue
        if key == "source":
            notes.append(f"{key!r} is provenance, not a constant: tuned by {value}")
            continue
        if key == PROVISIONAL_KEY:
            # Refused, not coerced: the string "false" is truthy.
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
            # JSON null means "not measured yet". Only the σ-inflation rate has an off switch;
            # every other constant falls back to its default.
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
        # The string "false" is truthy and would leave margin weighting silently on.
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
        # At 1.0 the interval is the whole line and the badge is silently disabled.
        raise ValueError("tension_credible_mass is a probability and must lie in (0, 1)")
    if "margin_form" in fields and fields["margin_form"] not in MARGIN_FORMS:
        raise ValueError(
            f"margin_form must be one of {MARGIN_FORMS}, got {fields['margin_form']!r}"
        )
    cap = fields.get("sigma_inflation_cap")
    if cap is not None and cap != "prior" and not _positive_number(cap):
        raise ValueError("sigma_inflation_cap must be 'prior' or a positive number")
    if "warm_gate" in fields and not 0.0 < fields["warm_gate"] < 1.0:
        # `WARM_SUPPORT` is k*g/(1-g): 1.0 divides by zero and above it every title is warm.
        raise ValueError("warm_gate is a probability and must lie in (0, 1)")
    if "blend_beta_max" in fields and not 0.0 < fields["blend_beta_max"] <= DEFAULTS.blend_beta_max:
        # 0009_scoring.sql CHECKs blend_beta <= 0.8; a higher ceiling would fail the nightly INSERT.
        raise ValueError(
            f"blend_beta_max must lie in (0, {DEFAULTS.blend_beta_max}] - 0009_scoring.sql "
            "constrains user_vector.blend_beta to that ceiling and migrations are immutable"
        )

    if provisional or unmeasured_rate:
        # After the checks, because 0.0 is what `_POSITIVE` refuses.
        fields[RATE_FIELD] = 0.0
        notes.append(
            "SIGMA-INFLATION DISABLED: the bundle ships no measured rate "
            "(sigma_inflation.rate_c_per_sqrt_month null or provisional), so sigma_inflation_c "
            "is 0.0 and no title's sigma grows with neglect until the corpus measures one "
            "(spec section 5.2)"
        )

    unthreaded = sorted(set(fields) & set(_PARSED_NOT_THREADED))
    if unthreaded and source == "bundle":
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
    """Read the active bundle's constants, or fall back to the defaults (§3.1).

    Not `is_file()`: a directory or dangling symlink there must fail loudly in `read_text`, not
    read as absent.
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
