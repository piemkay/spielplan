"""§6.6's LLM settings and spend guard, the connector keys, and the one test button every connector
card shares. Spec v2.1 §6.6, §9, §8 stage 6; decisions 324, 325, 338, 339, 343, 433, 450-453.

M5.5 shipped the read and the dispatch and no write (decision 433), because M5.7's row
`map-taste-admin-cost-estimate-before-enabling` orders that write: the estimate "before the setting
is persisted". The writes arrive here with M5.7's cards, and each says why it is shaped as it is.

THE NUMBER COMES BEFORE THE SETTING (§6.6: "per-title cost estimate before enabling"; decision
450). `POST /llm/preview` answers what a proposed change would cost -- the per-title estimate with
the price it rests on, the projected month (decision 451) and the meter with its remaining cap --
and writes nothing, not even the upsert a save opens with. `PUT /llm` takes the same change plus
`accepted_estimate`, the `per_title_usd` string the preview showed, recomputes the preview under one
advisory lock and one transaction, and stores the change only while that string still matches;
otherwise it answers 409 with the fresh preview. So the ordering is a property of the API and not of
one page: a stale tab, a price that turned over at its `valid_until`, a hand-typed request -- each is
shown the figure again rather than having it accepted in silence. A proposed plan naming a provider
with no usable key is `blocked` and refused, because a key typed later would otherwise start spend at
a figure nobody was shown. The preview and the read's estimate are one domain function,
`spend.preview`, over the `_plan` stage 6's gate plans with, so the figure accepted and the plan
billed cannot disagree. An optimistic save with a rollback is what this replaces: it shows the
number after the setting persisted, and a drain tick in between spends at it (plan A2).

THE CAP AND THE KEYS NEED NO FIGURE, AND THE HANDLERS SAY WHY (decision 452). `PUT /llm/cap` writes
the guard itself, in place and at once: raising or lowering it enables no provider, and a finite
number of at least zero is all it takes -- zero is "spend nothing" (decision 325), and there is no
way back to unset from here. `PUT /connectors/{name}` writes credentials and nothing else: a model
or a price override changes the estimate and is refused 422 so it can only arrive through the
figure, `llm` is refused 409 naming the route that writes it, and Jellyfin keeps its own save,
mounted first. A key saved later changes no stored plan, because no plan is ever stored naming a
provider without one.

NO BODY IS QUOTED BACK. FastAPI's own 422 carries the input it refused, and a card that renders its
error would put a mistyped or over-long key into the DOM -- or a key sent to the preview by a
confused client, which `extra="forbid"` refuses by quoting it. So the write routes read their body
through `_body`, whose 422 names the field and the rule and never the value (§9: credentials never
hit logs, and §14.3's argument for every other place a key could be printed).

Its own module rather than more routes in `api/admin.py`, for `api/acquisition.py`'s reason:
`admin.py` is already the largest entry in the residue ratchet, and a surface whose writes arrived
with M5.7's cards grows in a file whose diff is about that surface alone.

THE DISPATCH IS PLAN A4's ONE TABLE, AND IT IS MOUNTED AFTER `admin`. `POST
/api/admin/connectors/{name}/test` is `registry.test_connector` over `ConnectorSpec.test` - "One
dispatch table, not a route per provider" - and `/connectors/jellyfin/test` is one of the paths its
pattern matches. Jellyfin keeps its own route in `api/admin.py`, because that route stores §7.1's
probed verdict as it tests, and the Jellyfin row's `test` is None for exactly that reason. The
router registered first answers a path both match, so `app.py` includes this one after
`admin_api`'s and the Jellyfin card's button still reaches the handler that keeps what it
measured; `test_llm_api.py` holds the order by what only that handler does. A connector with no
test (the `llm` settings row) and a name the registry does not know are 404 with the registry's own
sentence; tmdb, omdb and trakt answer through `connectors/probes` since M5.7 (decision 453).

NO SECRET LEAVES, NOT EVEN AS A MASK. A provider key bills the household, which is §14.3's
argument for the Jellyfin key made about money instead of a media server, so the read reports
`has_api_key` as a boolean in that card's idiom (`api/admin.py`'s docstring) and never a value, a
prefix or a length -- and so do the connectors read and the key write, Trakt's client id included.

THIN BY CONSTRUCTION. Every rule the routes report is a domain answer over the rows
`connectors/registry` reads: the plan, its refusal, the estimate and the block are
`llm/spend.preview`'s, the projection `llm/spend.projection`'s over `acquire/queue.filed_since`'s
count, the meter `llm/spend.meter`'s, and a price and its basis `llm/pricing`'s. This module
decides how a Decimal, a date and a missing price are spelled on the wire, and holds no SQL, so it
is absent from `test_layering_guards.py`'s `ALLOWED_RESIDUE` rather than present with a `0` - the
first statement written here fails that ratchet as `(1, 0)`.

ADMIN, THE WAY EVERY OTHER ADMIN ROUTE IS: every route here takes `AdminUser`, so
`test_api_gating.py`'s two sweeps drive them as a stranger and as a member by walking the
dependency graph.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from spielplan.acquire import intake, pipeline, queue, stages
from spielplan.api.deps import DB, AdminUser, write_txn
from spielplan.connectors import registry
from spielplan.llm import anthropic, client, gemini, openai, pricing, spend
from spielplan.llm.pricing import ModelPrice
from spielplan.sources import base as sources

if TYPE_CHECKING:
    import asyncpg

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
# pass count (decision 324), and the monthly cap (decision 325). `parallel_providers` joined at M5.7,
# because the parallel toggle decision 450 previews has to be able to show what it would call.
_SETTINGS = ("extraction_provider", "parallel", "parallel_providers", "passes", "cap_usd")

# The settings a proposed change may name (decisions 324, 339, 450): the one task's assignment and
# the parallel mode with its providers and the pass count. The cap is not one of them -- it enables
# nothing and is written in place (decision 452).
_CHANGEABLE = ("extraction_provider", "parallel", "parallel_providers", "passes")

# Decision 450's one lock around the recompute and the write, so two confirms cannot each check a
# figure against the other's settings and both store. `hashtext` space, `deps.write_txn`'s own.
_SPEND_SETTINGS_LOCK = "llm-spend-settings"

# The credentials `PUT /connectors/{name}` writes, per connector (decision 452): each source's and
# provider's key, and Trakt's client id -- plaintext config, but the value its every request carries
# -- beside its sealed secret. Nothing else, so no estimate input can arrive by this route.
_CREDENTIALS = {
    "anthropic": ("api_key",),
    "openai": ("api_key",),
    "gemini": ("api_key",),
    "tmdb": ("api_key",),
    "omdb": ("api_key",),
    "trakt": ("client_id", "client_secret"),
}

# Proposal 137's point and decision 334's rule, shown on the source cards so a failure points at the
# right one: which stage-2 kinds read a key, and the one source whose absence parks a title.
_SOURCES = ("tmdb", "omdb", "trakt")


# --- the bodies ------------------------------------------------------------------------------------


def _no_control_characters(value: str) -> str:
    """`api/admin._no_control_characters`' rule: a C0 or C1 control arrives only from a paste or a
    crafted body, and jsonb and an HTTP header refuse it further in, so it is a 422 at the edge."""
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _each_once(names: list[str]) -> list[str]:
    if len(set(names)) != len(names):
        raise ValueError("names a provider more than once")
    return names


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


def _sendable(value: str) -> str:
    """`client.header_key`'s rule for every credential this route writes, not only the three a
    provider call sends: a TMDB or OMDb key rides in a query string, where a space inside it is sent
    as `+` and refused by the host, and a Trakt client id in a header h11 refuses. Checked on the
    trimmed value, so blank is the empty field (keep) and anything else no request can carry --
    whitespace or a control inside it, anything past printable ASCII -- is a 422 naming the fault.
    [M5.7 review cycle 1, M57-KEYS-C1-02]"""
    if value and client.header_key(value) is None:
        raise ValueError("must be printable ASCII with no whitespace or control character inside it")
    return value


# Decision 343's providers and nothing else, spelled from the one tuple `llm/client` calls.
ProviderName = Literal[client.PROVIDERS]
# USD, a JSON number of at least zero: never a bool (a checkbox's answer is not one dollar), never a
# string, never NaN or infinity -- each a value `spend._cap_of` and `pricing._is_price` would read as
# "no cap" or "no override" and log, which a write must refuse rather than store (decision 452).
Money = Annotated[StrictInt | StrictFloat, Field(ge=0), AfterValidator(_finite)]
ModelName = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_no_control_characters),
]
# Trimmed before anything else reads it: whitespace around a key is no part of it (decision 452's
# empty field, for one that only LOOKS empty -- three spaces replaced a working key while the card
# still said "(stored)", and a padded TMDB key parked every title at stage 2 on a 401).
Credential = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, max_length=512), AfterValidator(_sendable),
]


class _Forbidding(BaseModel):
    # An unknown field is refused, never ignored: `api_key` or `cap_usd` sent to the preview, or a
    # model sent to the key route, is a client writing through the wrong door (decisions 450, 452).
    model_config = ConfigDict(extra="forbid")


class ProviderChange(_Forbidding):
    """One provider's proposed model and price override (decision 343). Absent keeps; null unsets."""

    model: ModelName | None = None
    price_input: Money | None = None
    price_output: Money | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> ProviderChange:
        # Decision 343 ignores half an override and prices at the table, so a half pair stored here
        # would be a figure the admin typed and the meter never used.
        named = {"price_input", "price_output"} & self.model_fields_set
        if named and (len(named) == 1 or (self.price_input is None) != (self.price_output is None)):
            raise ValueError("price_input and price_output are set together as numbers, or cleared"
                             " together as null (decision 343)")
        return self


class LlmChange(_Forbidding):
    """A proposed change to §6.6's spend settings (decision 450): absent keeps, null unsets."""

    extraction_provider: ProviderName | None = None
    parallel: StrictBool = False
    parallel_providers: Annotated[
        list[ProviderName], Field(min_length=1, max_length=3), AfterValidator(_each_once)
    ] | None = None
    passes: Annotated[StrictInt, Field(ge=1, le=10)] = 1
    providers: dict[ProviderName, ProviderChange] = Field(default_factory=dict)


class LlmConfirm(LlmChange):
    """The same change and the figure it was shown: `per_title_usd` as the preview spelled it."""

    accepted_estimate: Annotated[str, Strict(), Field(min_length=1, max_length=64)]


class CapChange(_Forbidding):
    cap_usd: Money


class Credentials(_Forbidding):
    """A card's credential fields; empty or absent keeps the stored one (the Jellyfin card's idiom)."""

    api_key: Credential | None = None
    client_id: Credential | None = None
    client_secret: Credential | None = None


async def _body[Body: BaseModel](request: Request, model: type[Body]) -> Body:
    """The request body, validated, with a 422 that never quotes a value.

    FastAPI's own 422 is `exc.errors()` with each refused `input` in it, so a key over 512
    characters, a key with a newline in it, or a key sent to a route that does not take one would
    come back in the error -- and from there into the card that renders it. This answers the same
    shape with the field and the rule and nothing the client sent.
    """
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "the body is not JSON") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"type": error["type"], "loc": ["body", *error["loc"]], "msg": error["msg"]}
             for error in exc.errors(include_url=False, include_context=False, include_input=False)],
        ) from None


def _change(body: LlmChange) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The body in `spend.proposed`'s grammar: only the fields the client sent, None meaning unset.

    `model_fields_set` is what tells "absent" from "null" (decision 450): a default value is a field
    the client never named, and it must keep what is stored rather than overwrite it.
    """
    settings_change = {name: getattr(body, name) for name in _CHANGEABLE
                       if name in body.model_fields_set}
    providers = {
        name: {field: getattr(proposed, field) for field in proposed.model_fields_set}
        for name, proposed in body.providers.items()
        if proposed.model_fields_set
    }
    return settings_change, providers


async def _save(conn: asyncpg.Connection, name: str, fields: dict[str, Any]) -> None:
    """One row's share of a confirmed change: values saved, nulls unset (`registry.save_connector`)."""
    await registry.save_connector(
        conn, name, unset=[field for field, value in fields.items() if value is None],
        **{field: value for field, value in fields.items() if value is not None},
    )


def _credentials(name: str, state: registry.ConnectorState) -> dict[str, Any]:
    """A card's credentials as booleans -- whether each is stored, never a value, a prefix or a
    length (§14.3) -- and whether the sealed half would not open under this SECRETS_KEY."""
    if name == "trakt":
        held = {"has_client_id": bool(state.config.get("client_id")),
                "has_client_secret": bool(state.secrets.get("client_secret"))}
    else:
        held = {"has_api_key": bool(state.secrets.get("api_key"))}
    return {"name": name, **held, "secrets_unreadable": state.secrets_unreadable}


def _source_card(name: str, state: registry.ConnectorState, *, used_by: list[str]) -> dict[str, Any]:
    """A keyed source's card: its credentials as booleans, whether stage 2 parks without it
    (decision 334: only `tmdb:detail` can), and which stage-2 kinds read it."""
    return {
        **_credentials(name, state),
        "required": name == stages.REQUIRED_SOURCE,
        "used_by": ", ".join(used_by),
    }


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

    M5.7 adds `models`, the table's prefixes as the model pick's suggestions -- suggestions, since
    an override prices a model the table never heard of (decision 343) -- and `price_basis`, the
    caption's source and the price after `valid_until`, from the same one `effective_price` read.
    """
    has_key = bool(state.secrets.get("api_key"))
    model = str(state.config.get("model") or pricing.DEFAULT_MODELS[name])
    basis = pricing.price_basis(name, model, override=state.config, on=on)
    price = None if basis is None else basis.price
    return {
        "name": name,
        "configured": has_key and price is not None,
        "has_api_key": has_key,
        "secrets_unreadable": state.secrets_unreadable,
        "model": model,
        "structured_output": _ADAPTERS[name].STRUCTURED_OUTPUT,
        "price": _price(price),
        "models": sorted(pricing.PRICING[name]),
        "price_basis": _basis(basis),
    }


def _basis(basis: pricing.PriceBasis | None) -> dict[str, Any] | str:
    """Decision 343's caption, spelled: which model and price a figure rests on, `table` or
    `override`, and -- for a dated table price -- the day it ends and the price it becomes, so a
    figure accepted in December says it is not January's. UNKNOWN for a model nobody priced."""
    if basis is None:
        return UNKNOWN
    return {
        "provider": basis.provider,
        "model": basis.model,
        "source": basis.source,
        "input": basis.price.input,
        "output": basis.price.output,
        "valid_until": basis.price.valid_until.isoformat() if basis.price.valid_until else None,
        "then": None if basis.then is None else {"input": basis.then.input, "output": basis.then.output},
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


def _estimate(preview: spend.Preview) -> dict[str, Any]:
    """Plan D4's per-title estimate, over the providers and passes stage 6 would use.

    Priced at `pricing.SPEC_INPUT_TOKENS`, §8 stage 6's 23,500-token midpoint, and labelled as the
    input it assumes: this is the figure before a title has a pack to count, and the gate's own
    reservation counts the real prompt instead. Over `spend.preview`'s plan rather than over the
    settings as typed, so the providers and passes are the ones the gate would call and the
    estimate cannot read a setting the plan would refuse. When the plan refuses, the figure is
    UNKNOWN and `reason` is the refusal's own sentence - the one the board parks the title under -
    because the cost of an extraction that cannot run is not a number (decision 343).

    One spelling for the read, the preview and the confirm's comparison, so the string an admin
    accepted is the string the confirm recomputes (decision 450). `output_tokens_assumed` and
    `basis` are M5.7's: the caption names both counts it assumed and the price each provider was
    priced at, and never §6.6's corpus baseline, which rests on the retired 2.5 family (decision 343).
    """
    plan = preview.plan
    if isinstance(plan, spend.Refusal):
        return {
            "per_title_usd": UNKNOWN,
            "input_tokens_assumed": pricing.SPEC_INPUT_TOKENS,
            "output_tokens_assumed": pricing.MEAN_OUTPUT_TOKENS,
            "passes": None,
            "providers": [],
            "reason": plan.reason,
            "basis": [],
        }
    return {
        "per_title_usd": UNKNOWN if preview.per_title is None else _money(preview.per_title),
        "input_tokens_assumed": pricing.SPEC_INPUT_TOKENS,
        "output_tokens_assumed": pricing.MEAN_OUTPUT_TOKENS,
        "passes": plan.passes,
        "providers": [chosen.provider for chosen in plan.providers],
        "reason": None,
        "basis": [_basis(basis) for basis in preview.basis],
    }


def _projected(projection: spend.Projection) -> dict[str, Any]:
    """Decision 451's month, spelled: null with its reason when there is no history, UNKNOWN when the
    per-title figure is, and otherwise the figure beside what is left of the cap."""
    if not projection.ever_filed:
        monthly: str | None = None
    elif projection.monthly is None:
        monthly = UNKNOWN
    else:
        monthly = _money(projection.monthly)
    return {
        "window_days": spend.PROJECTION_DAYS,
        "titles": projection.titles,
        "ever_filed": projection.ever_filed,
        "monthly_usd": monthly,
        "remaining_usd": _money(projection.remaining),
        "exceeds_remaining": projection.exceeds_remaining,
        "reason": projection.reason,
    }


async def _preview_body(
    conn: asyncpg.Connection,
    *,
    now: datetime,
    change: dict[str, Any] | None = None,
    providers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The three figures plan §7 check 1 names -- the per-title estimate, the projected month and the
    meter's remaining cap -- and `blocked`, for a change as proposed. Reads only.

    The projection counts `pipeline.TASK_KIND` tasks and leaves out `intake.RE_OFFER_PRIORITY`'s
    (decision 451): wired here because `llm/spend` may import neither module -- `acquire/pipeline`
    imports it, and the cycle would close through the gate.
    """
    preview = await spend.preview(conn, change=change, providers=providers, now=now)
    reading = await spend.meter(conn, now=now)
    titles, ever = await queue.filed_since(
        conn, pipeline.TASK_KIND, now - timedelta(days=spend.PROJECTION_DAYS),
        except_priority=intake.RE_OFFER_PRIORITY,
    )
    projection = spend.projection(
        preview.per_title, titles=titles, ever_filed=ever, remaining=reading["remaining_usd"],
    )
    return {
        "estimate": _estimate(preview),
        "projected": _projected(projection),
        "meter": _meter(reading),
        "blocked": preview.blocked,
    }


async def _read(conn: asyncpg.Connection) -> dict[str, Any]:
    """`GET /llm`'s body, which the confirm answers too, so a client re-renders from one shape.

    One instant for the whole read, and the install's own day for every price on it: decision
    325's month and decision 343's dated price both turn over on a local midnight, and `_plan`
    prices on that day, so a card read at 00:30 on the day a price doubles shows the figure stage 6
    would bill at rather than the process's.
    """
    now = datetime.now(UTC)
    today = now.astimezone(spend.local_zone()).date()
    cards = [
        provider_card(name, await registry.load_connector(conn, name), on=today)
        for name in client.PROVIDERS
    ]
    stored = (await registry.load_connector(conn, spend.SETTINGS)).config
    figures = await _preview_body(conn, now=now)
    return {
        "providers": cards,
        "settings": {name: stored.get(name) for name in _SETTINGS},
        "meter": figures["meter"],
        "estimate": figures["estimate"],
        "projected": figures["projected"],
        "batch": {"available": False, "reason": BATCH_UNAVAILABLE},
    }


@router.get("/llm")
async def llm_settings(_: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's provider cards and spend guard, read: what M5.7 renders and nothing it writes.

    `settings` is the `llm` row as stored, absent being null. What stage 6 makes of it - the
    defaults decision 324 gives an absent `parallel` and `passes`, and the refusal a setting that
    does not read earns - is `estimate`'s plan and its reason, so the defaults are stated once, in
    `llm/spend`, and a hand-typed value that does not read is shown as typed rather than hidden
    behind the default it failed to be. `batch` is decision 338's answer and not a setting.
    `projected` is decision 451's month over the stored plan, M5.7's addition beside M5.5's keys.
    """
    return await _read(conn)


@router.post("/llm/preview")
async def llm_preview(request: Request, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Decision 450's preview: what the proposed change would cost, before anything is stored.

    Answers `estimate` (the per-title figure with its price basis), `projected` (the month, decision
    451), `meter` (with its remaining cap) and `blocked` (a provider the change would plan with no
    usable key). WRITES NOTHING -- no save, no upsert, no timestamp -- which is plan §7 check 2 and
    the whole of "saying no costs nothing". An empty change previews the stored plan.
    """
    change, providers = _change(await _body(request, LlmChange))
    return await _preview_body(conn, now=datetime.now(UTC), change=change, providers=providers)


@router.put("/llm", response_model=None)
async def llm_confirm(request: Request, _: AdminUser, conn: DB) -> dict[str, Any] | JSONResponse:
    """Decision 450's write: the change the preview showed, stored only with the figure it showed.

    The preview is recomputed for the same change, under `_SPEND_SETTINGS_LOCK` and inside the one
    transaction that stores it, through the same functions and the same spelling -- so the string
    compared is the string a preview would print this instant. A stale or wrong figure, or a
    change `blocked` for a keyless provider, is 409 with that fresh preview and nothing stored;
    otherwise every set and unset field is saved through `registry.save_connector` and the answer is
    the read's body. An empty change is 422: a confirm that stores nothing is a client mistake, and
    answering 200 would tell it a setting was saved.
    """
    body = await _body(request, LlmConfirm)
    change, providers = _change(body)
    if not change and not providers:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "the change names no setting; a confirm stores the change a preview showed")
    refused: str | None = None
    async with write_txn(conn, lock=_SPEND_SETTINGS_LOCK):
        fresh = await _preview_body(conn, now=datetime.now(UTC), change=change, providers=providers)
        if fresh["blocked"] is not None:
            refused = (f"{fresh['blocked']}. Nothing was stored; the fresh preview is attached"
                       " (decision 450)")
        elif body.accepted_estimate != fresh["estimate"]["per_title_usd"]:
            refused = (f"the per-title estimate is now {fresh['estimate']['per_title_usd']}, not the"
                       f" {body.accepted_estimate} this confirm carried, so nothing was stored: show"
                       " the fresh preview attached and confirm that figure (decision 450)")
        else:
            for name, fields in providers.items():
                await _save(conn, name, fields)
            if change:
                await _save(conn, spend.SETTINGS, change)
    if refused is not None:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"detail": refused, "preview": fresh})
    return await _read(conn)


@router.put("/llm/cap")
async def llm_cap(request: Request, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Decision 452: the monthly cap, written in place and in force at once, answering the meter.

    No preview and no figure, because the cap is the guard itself: raising it enables no provider
    and lowering it can only park sooner (proposal 107 as decision 330 adopts it: "editable in place
    and takes effect immediately"). Zero is a cap -- "spend nothing" (decision 325) -- and null is
    refused, because this card offers no way back to "no cap"; `_cap_of` would read a stored null
    as unset and park every title under decision 348's sentence, which is a state an install starts
    in and not one an admin chooses here.
    """
    body = await _body(request, CapChange)
    await registry.save_connector(conn, spend.SETTINGS, cap_usd=body.cap_usd)
    return {"meter": _meter(await spend.meter(conn))}


@router.get("/connectors")
async def connectors(_: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's source cards, read: booleans for each keyed source and the five that need no key.

    `required` is decision 334's rule -- `stages.REQUIRED_SOURCE` is the one source whose missing
    key parks a title at stage 2 -- and `used_by` the stage-2 kinds that read the key, from the
    adapters' own registry rather than a list typed here (proposal 137: a failure should point at
    the right card). `keyless` is the rest of §8 stage 2's eight, in the order stage 2 asks them.
    """
    sources.load_all()
    order = sources.available_kinds({name: True for name in _SOURCES})
    keyless = list(dict.fromkeys(
        sources.REGISTRY[kind].source for kind in order if sources.REGISTRY[kind].requires is None))
    return {
        "sources": [
            _source_card(name, await registry.load_connector(conn, name),
                         used_by=[kind for kind in order if sources.REGISTRY[kind].requires == name])
            for name in _SOURCES
        ],
        "keyless": keyless,
    }


@router.put("/connectors/{name}")
async def connector_credentials(name: str, request: Request, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Decision 452: a card's credentials and nothing else, empty keeping the stored value.

    `llm` is 409 naming the route that writes it, and a name the registry does not know is its 404.
    Jellyfin never reaches here: `api/admin.put_jellyfin` is mounted first and answers its own path.
    A field this connector does not carry is 422, and so is a model or a price override, which
    change the estimate and so arrive only with decision 450's figure. The answer is booleans.
    """
    if name == spend.SETTINGS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the llm settings are written by PUT /api/admin/llm, with the estimate the preview showed"
            " (decision 450), and the cap by PUT /api/admin/llm/cap (decision 452)",
        )
    try:
        registry.spec_for(name)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    carried = _CREDENTIALS.get(name)
    if carried is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"connector {name} is saved by its own card, not by this route")
    body = await _body(request, Credentials)
    stray = sorted(body.model_fields_set - set(carried))
    if stray:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"connector {name} takes {', '.join(carried)}; not {', '.join(stray)}")
    typed = {field: getattr(body, field) for field in carried if getattr(body, field)}
    if typed:
        await registry.save_connector(conn, name, **typed)
    return _credentials(name, await registry.load_connector(conn, name))


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
