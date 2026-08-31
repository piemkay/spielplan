"""The tuned constants of the §5.2 recipe. Spec v2.1 §4.3, §5.2.

§4.3: "`ledger_hyperparams.json` — the tuned constants of the §5.2 recipe … Per-user cutpoints
and per-arm sensitivities are **not** shipped — they are fitted in-app by design."

Two rules give this module its shape:

  1. **Every constant comes from the bundle.** This is the only module in the package allowed
     to contain a tuning number. If a λ appears anywhere else, it has escaped the file the
     corpus project re-tunes it in, and re-tuning stops reaching the app.
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
        invalidated by provenance alone would be thrown away for no numerical reason."""
        payload = {k: v for k, v in asdict(self).items() if k != "source"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]


DEFAULTS = Hyperparams()

_POSITIVE = (
    "lambda_ridge", "lambda_bt", "b_i_tau", "mu_prior_tau", "lr",
    "cutpoint_prior_precision", "tie_prior_precision", "sigma_inflation_grace_months",
    "sigma_inflation_c", "margin_decisive", "margin_hesitant", "straddle_z",
)
_BOOLEAN = ("margin_weighting",)


def from_mapping(raw: dict[str, Any], *, source: str = "bundle") -> tuple[Hyperparams, list[str]]:
    """Build hyperparameters from a parsed `ledger_hyperparams.json`.

    Returns (hyperparams, notes). Unknown keys are reported rather than dropped silently — a
    constant the corpus project tuned and this app ignores is exactly the kind of thing that
    looks like it is working.
    """
    notes: list[str] = []
    known = {f for f in DEFAULTS.__dataclass_fields__ if f != "source"}
    fields: dict[str, Any] = {}

    for key, value in raw.items():
        if PER_USER_KEY.search(key):
            # §4.3 is explicit that these are fitted in-app. Taking them from a bundle would
            # replace a per-user fit with somebody else's thresholds.
            notes.append(f"ignored per-user key {key!r} — §4.3 fits these in-app")
            continue
        if key not in known:
            notes.append(f"unknown hyperparameter {key!r} — not applied")
            continue
        fields[key] = value

    for key in _POSITIVE:
        if key in fields and not (isinstance(fields[key], int | float) and fields[key] > 0):
            raise ValueError(f"{key} must be a positive number, got {fields[key]!r}")
    for key in _BOOLEAN:
        # `"false"` is a string, and a string is truthy. A corpus-side decision to turn margin
        # weighting off would otherwise leave it silently on — precisely the "knob tuned into a
        # void" this module exists to prevent.
        if key in fields and not isinstance(fields[key], bool):
            raise ValueError(f"{key} must be a boolean, got {fields[key]!r}")
    if "newton_max_iter" in fields and not (
        isinstance(fields["newton_max_iter"], int) and fields["newton_max_iter"] > 0
    ):
        raise ValueError("newton_max_iter must be a positive integer")
    if "steps" in fields and not (isinstance(fields["steps"], int) and fields["steps"] > 0):
        raise ValueError(f"steps must be a positive integer, got {fields['steps']!r}")
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
    if cap is not None and cap != "prior" and not (isinstance(cap, int | float) and cap > 0):
        raise ValueError("sigma_inflation_cap must be 'prior' or a positive number")

    missing = sorted(known - set(fields))
    if missing and source == "bundle":
        notes.append(f"bundle omits {len(missing)} constant(s); defaults used: {', '.join(missing)}")
    return replace(DEFAULTS, **fields, source=source), notes


def load(store: Any) -> tuple[Hyperparams, list[str]]:
    """Read the active bundle's constants, or fall back to the documented defaults.

    §3.1 makes a bundle-less app a legal state, so a household can rate before any corpus
    export exists. What it must not do is pretend the numbers came from somewhere.
    """
    if store is None or getattr(store, "is_empty", True):
        return DEFAULTS, ["no artifact bundle — §5.2 constants are this app's defaults"]
    path = store.path("ledger_hyperparams.json")
    if not path.is_file():
        return DEFAULTS, ["bundle ships no ledger_hyperparams.json — defaults used"]
    return from_mapping(json.loads(path.read_text(encoding="utf-8")), source="bundle")


__all__ = ["DEFAULTS", "MARGIN_FORMS", "Hyperparams", "from_mapping", "load"]
