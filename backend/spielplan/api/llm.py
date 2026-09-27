"""§6.6's LLM settings, spend guard, connector keys and shared test button (§9; decisions 433, 450-453).
The figure comes before the setting: `PUT /llm` stores a change only while the per-title estimate
the preview showed still matches, else 409 with a fresh preview. No body is quoted back (`_body`)."""

from __future__ import annotations

import math
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
from spielplan.api.deps import DB, AdminUser, printable, write_txn
from spielplan.connectors import registry
from spielplan.llm import anthropic, client, gemini, openai, pricing, spend
from spielplan.llm.pricing import ModelPrice
from spielplan.sources import base as sources

if TYPE_CHECKING:
    import asyncpg

router = APIRouter(prefix="/api/admin", tags=["admin", "llm"])

# Decision 343: a string where a figure would be, so no client renders a number the table never held.
UNKNOWN = "unknown"

# Decision 338: reported, so the batch toggle ships disabled and says why.
BATCH_UNAVAILABLE = (
    "batch endpoints are not used at M5: every call is synchronous, because a batch answers in"
    " hours and the acquisition board has no state for a title waiting on one (decision 338)"
)

_ADAPTERS = {"anthropic": anthropic, "openai": openai, "gemini": gemini}

_SETTINGS = ("extraction_provider", "parallel", "parallel_providers", "passes", "cap_usd")

# What a proposed change may name. Not the cap: it enables nothing and is written in place (decision 452).
_CHANGEABLE = ("extraction_provider", "parallel", "parallel_providers", "passes")

# Decision 450: one lock around recompute and write, so two confirms cannot cross.
_SPEND_SETTINGS_LOCK = "llm-spend-settings"

_SOURCES = ("tmdb", "omdb", "trakt")

# The only fields `PUT /connectors/{name}` writes (decision 452): a source's every field and a
# provider's key, so no estimate input can arrive here.
_CREDENTIALS = {
    **{name: sum(registry.FIELDS[name], ()) for name in _SOURCES},
    **{name: registry.FIELDS[name][1] for name in client.PROVIDERS},
}


def _each_once(names: list[str]) -> list[str]:
    if len(set(names)) != len(names):
        raise ValueError("names a provider more than once")
    return names


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


def _sendable(value: str) -> str:
    """`client.header_key`'s rule for every credential: TMDB/OMDb keys ride in a query string and
    Trakt's id in a header. Checked trimmed, so blank means keep."""
    if value and client.header_key(value) is None:
        raise ValueError("must be printable ASCII with no whitespace or control character inside it")
    return value


ProviderName = Literal[client.PROVIDERS]
# USD >= 0; never a bool, string, NaN or infinity, each of which would read as "no cap" (decision 452).
Money = Annotated[StrictInt | StrictFloat, Field(ge=0), AfterValidator(_finite)]
ModelName = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(printable),
]
# Trimmed first: whitespace is no part of a key, and a key of spaces must mean keep.
Credential = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, max_length=512), AfterValidator(_sendable),
]


class _Forbidding(BaseModel):
    # Unknown fields are refused: a key sent to the preview is a client writing through the wrong door.
    model_config = ConfigDict(extra="forbid")


class ProviderChange(_Forbidding):
    """Decision 343: one provider's model and price override. Absent keeps; null unsets."""

    model: ModelName | None = None
    price_input: Money | None = None
    price_output: Money | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> ProviderChange:
        # Decision 343 ignores half an override, so a half pair would be a figure the meter never used.
        named = {"price_input", "price_output"} & self.model_fields_set
        if named and (len(named) == 1 or (self.price_input is None) != (self.price_output is None)):
            raise ValueError("price_input and price_output are set together as numbers, or cleared"
                             " together as null (decision 343)")
        return self


class LlmChange(_Forbidding):
    """Decision 450: absent keeps, null unsets."""

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
    """FastAPI's 422 quotes refused input, which could put a key in the DOM; this names field and rule."""
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
    """Only the fields the client sent: `model_fields_set` tells absent (keep) from null (unset)."""
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
    """Booleans only, never a value, prefix or length (§14.3), plus whether the sealed half opens."""
    if name == "trakt":
        held = {"has_client_id": bool(state.config.get("client_id")),
                "has_client_secret": bool(state.secrets.get("client_secret"))}
    else:
        held = {"has_api_key": bool(state.secrets.get("api_key"))}
    return {"name": name, **held, "secrets_unreadable": state.secrets_unreadable}


def _source_card(name: str, state: registry.ConnectorState, *, used_by: list[str]) -> dict[str, Any]:
    """`required`: the one source whose absence parks stage 2 (decision 334)."""
    return {
        **_credentials(name, state),
        "required": name == stages.REQUIRED_SOURCE,
        "used_by": ", ".join(used_by),
    }


def _money(amount: Decimal | None) -> str | None:
    """A string: JSON numbers are floats to clients, and the meter is exact (decision 325)."""
    return None if amount is None else str(amount)


def _price(price: ModelPrice | None) -> dict[str, Any] | str:
    """USD per 1M tokens in and out, as the numbers an admin types, or UNKNOWN (decision 343)."""
    if price is None:
        return UNKNOWN
    return {
        "input": price.input,
        "output": price.output,
        "valid_until": price.valid_until.isoformat() if price.valid_until else None,
    }


def provider_card(name: str, state: registry.ConnectorState, *, on: date) -> dict[str, Any]:
    """`model` falls back as `spend.extraction_plan` does; `configured` means stage 6 could call it (an
    openable key and a price in effect). `secrets_unreadable` is not `has_api_key: false`."""
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
    """Decision 343's caption: model, price, `table` or `override`, a dated price's end and successor."""
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
    return {
        "spent_usd": _money(reading["spent_usd"]),
        # Decision 436: spend at write-ahead ceilings, shown apart from answered calls.
        "unsettled_usd": _money(reading["unsettled_usd"]),
        "cap_usd": _money(reading["cap_usd"]),
        "remaining_usd": _money(reading["remaining_usd"]),
        "period_start": reading["period_start"].isoformat(),
        "period_end": reading["period_end"].isoformat(),
        "tz": reading["tz"],
    }


def _estimate(preview: spend.Preview) -> dict[str, Any]:
    """Priced at `SPEC_INPUT_TOKENS` over the plan stage 6 would use; UNKNOWN with the refusal's reason
    when it refuses. One spelling, so the confirm compares the same string (decision 450)."""
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
    """Decision 451's month: null without history, UNKNOWN when the per-title figure is."""
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
    """The per-title estimate, projected month, meter and `blocked`; reads only. Wired here because
    `llm/spend` cannot import `acquire/pipeline` without a cycle."""
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
    """One instant and the install's local day for every price: months and prices turn at local midnight."""
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
    """`settings` is the row as stored; what stage 6 makes of it is `estimate`'s plan and reason, so the
    defaults are stated once, in `llm/spend`."""
    return await _read(conn)


@router.post("/llm/preview")
async def llm_preview(request: Request, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Decision 450's preview. Writes nothing; an empty change previews the stored plan."""
    change, providers = _change(await _body(request, LlmChange))
    return await _preview_body(conn, now=datetime.now(UTC), change=change, providers=providers)


@router.put("/llm", response_model=None)
async def llm_confirm(request: Request, _: AdminUser, conn: DB) -> dict[str, Any] | JSONResponse:
    """Recomputed under the lock inside the storing transaction; a stale figure or a `blocked` change is
    409 with the fresh preview. An empty change is 422."""
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
    """Decision 452: in place and at once, with no figure, since the cap enables nothing. Zero means spend
    nothing; null is refused (there is no way back to "no cap" here)."""
    body = await _body(request, CapChange)
    await registry.save_connector(conn, spend.SETTINGS, cap_usd=body.cap_usd)
    return {"meter": _meter(await spend.meter(conn))}


@router.get("/connectors")
async def connectors(_: AdminUser, conn: DB) -> dict[str, Any]:
    """`required` is decision 334's rule and `used_by` comes from the adapters' registry; `keyless` is
    the rest of stage 2's sources, in asking order."""
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
    """Decision 452: credentials only, empty keeps. `llm` is 409, an unknown name 404, a model or price
    422 (it changes the estimate). Jellyfin's own route is mounted first."""
    if name == spend.SETTINGS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the llm settings are written by PUT /api/admin/llm, with the estimate the preview showed"
            " (decision 450), and the cap by PUT /api/admin/llm/cap (decision 452)",
        )
    try:
        registry.fields_of(name)
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
    """`ok: false` with the provider's words is a 200: the button worked. Only a bare LookupError is a
    404; a KeyError inside a probe is a fault."""
    try:
        return await registry.test_connector(conn, name)
    except LookupError as exc:
        if type(exc) is not LookupError:
            raise
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
