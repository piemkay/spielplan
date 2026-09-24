"""§6.6's LLM settings, read side, and the one test button every connector card shares. Spec v2.1
§6.6, §9, §8 stage 6; decisions 324, 325, 338, 343, 433.

TWO ROUTES AND NO WRITE, which is decision 433 taken exactly. M5.5 builds no UI (plan §8: "This
milestone ships APIs and a domain estimate function"), and a write route shipped now is one M5.7
rebuilds: its row `map-taste-admin-cost-estimate-before-enabling` orders that write -- the estimate
"before the setting is persisted" -- so provider keys and models, the cap and the mode arrive with
M5.7's cards, through `registry.save_connector`. Until then an in-app install cannot set a cap and
stage 6 stays parked under decision 348's sentence, which is the safe direction to be wrong in.

Its own module rather than two more routes in `api/admin.py`, for `api/acquisition.py`'s reason:
`admin.py` is already the largest entry in the residue ratchet, and a surface whose writes are
M5.7's should grow in a file whose diff is about that surface alone.

THE DISPATCH IS PLAN A4's ONE TABLE, AND IT IS MOUNTED AFTER `admin`. `POST
/api/admin/connectors/{name}/test` is `registry.test_connector` over `ConnectorSpec.test` - "One
dispatch table, not a route per provider" - and `/connectors/jellyfin/test` is one of the paths its
pattern matches. Jellyfin keeps its own route in `api/admin.py`, because that route stores §7.1's
probed verdict as it tests, and the Jellyfin row's `test` is None for exactly that reason. The
router registered first answers a path both match, so `app.py` includes this one after
`admin_api`'s and the Jellyfin card's button still reaches the handler that keeps what it
measured; `test_llm_api.py` holds the order by what only that handler does. A connector with no
test in this build (tmdb, omdb and trakt, until M5.7's source cards) and a name the registry does
not know are 404 with the registry's own sentence.

NO SECRET LEAVES, NOT EVEN AS A MASK. A provider key bills the household, which is §14.3's
argument for the Jellyfin key made about money instead of a media server, so the read reports
`has_api_key` as a boolean in that card's idiom (`api/admin.py`'s docstring) and never a value, a
prefix or a length.

THIN BY CONSTRUCTION. Every rule the read reports is a domain answer over the rows
`connectors/registry` reads: the plan and its refusal are `llm/spend.extraction_plan`'s, the meter
`llm/spend.meter`'s, a price `llm/pricing.effective_price`'s and the estimate
`llm/pricing.estimate_title`'s. This module decides how a Decimal, a date and a missing price are
spelled on the wire, and holds no SQL, so it is absent from `test_layering_guards.py`'s
`ALLOWED_RESIDUE` rather than present with a `0` - the first statement written here fails that
ratchet as `(1, 0)`.

ADMIN, THE WAY EVERY OTHER ADMIN ROUTE IS: both take `AdminUser`, so `test_api_gating.py`'s two
sweeps drive them as a stranger and as a member by walking the dependency graph.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status

from spielplan.api.deps import DB, AdminUser
from spielplan.connectors import registry
from spielplan.llm import anthropic, client, gemini, openai, pricing, spend
from spielplan.llm.pricing import ModelPrice

router = APIRouter(prefix="/api/admin", tags=["admin", "llm"])

# Decision 343's word for a price nobody knows, and for the estimate over one: a string where a
# figure would be, so a client that renders the field cannot render a number the table never held.
UNKNOWN = "unknown"

# Decision 338: sync only at M5. Reported rather than omitted, so M5.7's batch/sync toggle ships
# disabled and says why instead of enabled and inert. `ops/devstub.py` imports it, not a copy.
BATCH_UNAVAILABLE = (
    "batch endpoints are not used at M5: every call is synchronous, because a batch answers in"
    " hours and the acquisition board has no state for a title waiting on one (decision 338)"
)

# §6.6's "structured-output mode" caption, which each adapter states about itself.
_ADAPTERS = {"anthropic": anthropic, "openai": openai, "gemini": gemini}

# The `llm` row's settings decision 433 reports: the per-task assignment and parallel mode and the
# pass count (decision 324), and the monthly cap (decision 325).
_SETTINGS = ("extraction_provider", "parallel", "passes", "cap_usd")


def _money(amount: Decimal | None) -> str | None:
    """A dollar figure as the string of its own digits, or None.

    A JSON number is a binary float to every client that parses one, and the meter is exact on
    purpose: 0028 makes `llm_call.usd` numeric so ten ten-cent calls sum to one dollar (decision
    325). A figure carried as a float would hand the spend guard the drift the column refuses.
    """
    return None if amount is None else str(amount)


def _price(price: ModelPrice | None) -> dict[str, Any] | str:
    """Decision 343's card field: USD per 1M tokens in and out and the first day the figure stops
    being true - the caption's date M5.7 renders, None for a price with no announced end or an
    admin's override - or UNKNOWN. The two rates stay JSON numbers, because they are the figures an
    admin types into `price_input` and `price_output` and not a sum anything adds up."""
    if price is None:
        return UNKNOWN
    return {
        "input": price.input,
        "output": price.output,
        "valid_until": price.valid_until.isoformat() if price.valid_until else None,
    }


def provider_card(name: str, state: registry.ConnectorState, *, on: date) -> dict[str, Any]:
    """One provider's card: its settings as stage 6 would use them, and booleans for its secret.

    `model` is the row's, else `pricing.DEFAULT_MODELS` - `spend.extraction_plan`'s fallback, so
    the card names the model a call would be made on - and `price` is decision 343's effective one
    for it on the install's day `on`. `configured` is the Jellyfin card's one bit asked of this
    card: whether stage 6 could call this provider were extraction assigned to it, which takes a
    key this SECRETS_KEY opens and a price in effect - the two things the plan refuses a provider
    for, reported beside it as `has_api_key` and `price` so the card can say which is missing.
    `secrets_unreadable` is not `has_api_key: false`, for `api/admin.get_jellyfin`'s reason: a key
    that exists and will not open is "restore the env file or re-enter it", not "set one up".
    """
    has_key = bool(state.secrets.get("api_key"))
    model = str(state.config.get("model") or pricing.DEFAULT_MODELS[name])
    price = pricing.effective_price(name, model, override=state.config, on=on)
    return {
        "name": name,
        "configured": has_key and price is not None,
        "has_api_key": has_key,
        "secrets_unreadable": state.secrets_unreadable,
        "model": model,
        "structured_output": _ADAPTERS[name].STRUCTURED_OUTPUT,
        "price": _price(price),
    }


def _meter(reading: dict[str, Any]) -> dict[str, Any]:
    """`spend.meter`'s reading with its Decimals and instants spelled for the wire, which its own
    docstring leaves to `api/`."""
    return {
        "spent_usd": _money(reading["spent_usd"]),
        # The part of the spend standing at write-ahead ceilings (decision 436), which M5.7's guard
        # shows apart so a month spent on unanswered calls does not read as answers.
        "unsettled_usd": _money(reading["unsettled_usd"]),
        "cap_usd": _money(reading["cap_usd"]),
        "remaining_usd": _money(reading["remaining_usd"]),
        "period_start": reading["period_start"].isoformat(),
        "period_end": reading["period_end"].isoformat(),
        "tz": reading["tz"],
    }


def _estimate(plan: spend.Plan | spend.Refusal) -> dict[str, Any]:
    """Plan D4's per-title estimate, over the providers and passes stage 6 would use.

    Priced at `pricing.SPEC_INPUT_TOKENS`, §8 stage 6's 23,500-token midpoint, and labelled as the
    input it assumes: this is the figure before a title has a pack to count, and the gate's own
    reservation counts the real prompt instead. Over `spend.extraction_plan`'s answer rather than
    over the stored settings, so the providers and passes are the ones the gate would call and the
    estimate cannot read a setting the plan would refuse. When the plan refuses, the figure is
    UNKNOWN and `reason` is the refusal's own sentence - the one the board parks the title under -
    because the cost of an extraction that cannot run is not a number (decision 343).
    """
    if isinstance(plan, spend.Refusal):
        return {
            "per_title_usd": UNKNOWN,
            "input_tokens_assumed": pricing.SPEC_INPUT_TOKENS,
            "passes": None,
            "providers": [],
            "reason": plan.reason,
        }
    per_title = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS,
        prices=[chosen.price for chosen in plan.providers],
        passes=plan.passes,
    )
    return {
        "per_title_usd": UNKNOWN if per_title is None else _money(per_title),
        "input_tokens_assumed": pricing.SPEC_INPUT_TOKENS,
        "passes": plan.passes,
        "providers": [chosen.provider for chosen in plan.providers],
        "reason": None,
    }


@router.get("/llm")
async def llm_settings(_: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's provider cards and spend guard, read: what M5.7 renders and nothing it writes.

    `settings` is the `llm` row as stored, absent being null. What stage 6 makes of it - the
    defaults decision 324 gives an absent `parallel` and `passes`, and the refusal a setting that
    does not read earns - is `estimate`'s plan and its reason, so the defaults are stated once, in
    `llm/spend`, and a hand-typed value that does not read is shown as typed rather than hidden
    behind the default it failed to be. `batch` is decision 338's answer and not a setting.

    One instant for the whole read, and the install's own day for every price on it: decision
    325's month and decision 343's dated price both turn over on a local midnight, and
    `spend.extraction_plan` prices on that day, so a card read at 00:30 on the day a price doubles
    shows the figure stage 6 would bill at rather than the process's.
    """
    now = datetime.now(UTC)
    today = now.astimezone(spend.local_zone()).date()
    cards = [
        provider_card(name, await registry.load_connector(conn, name), on=today)
        for name in client.PROVIDERS
    ]
    stored = (await registry.load_connector(conn, spend.SETTINGS)).config
    return {
        "providers": cards,
        "settings": {name: stored.get(name) for name in _SETTINGS},
        "meter": _meter(await spend.meter(conn, now=now)),
        "estimate": _estimate(await spend.extraction_plan(conn, now=now)),
        "batch": {"available": False, "reason": BATCH_UNAVAILABLE},
    }


@router.post("/connectors/{name}/test")
async def connector_test(name: str, _: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's test button for any card whose connector has one: the registry's answer, as is.

    A provider's answer is `client.probe`'s - its free models-list read with the key in a header,
    so pressing it bills nothing and needs no cap - and `ok: false` with the provider's own words
    is a 200, as the Jellyfin button answers a server that refused: the button worked, and what it
    found is the answer.

    404 FOR THE REGISTRY'S TWO REFUSALS AND NOTHING ELSE. `spec_for` and `test_connector` raise
    `LookupError` itself for a name nobody registered and for a connector whose test this build does
    not ship; a `KeyError` is a `LookupError` too, and one raised inside a probe - a provider
    envelope missing a field - is a fault in this build, which a 404 would report to the admin as
    the card they just pressed not existing.
    """
    try:
        return await registry.test_connector(conn, name)
    except LookupError as exc:
        if type(exc) is not LookupError:
            raise
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
