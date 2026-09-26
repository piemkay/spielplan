"""The meter, the plan and the cap check stage 6 passes before a provider is called (§8, §6.6).

The meter is `SUM(llm_call.usd)` over the household's local month, never a counter. No default cap.
The reservation covers both attempts (decision 325); overshoot is bounded, not closed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.db.dna_terms import active_version
from spielplan.dna import verify
from spielplan.llm import anthropic, client, contract, pricing
from spielplan.llm.pricing import ModelPrice

if TYPE_CHECKING:
    import asyncpg

log = logging.getLogger("spielplan.llm.spend")

# The `connector_config` row §6.6's settings live in.
SETTINGS = "llm"
TASK = "extraction"

# Every run is reserved at two attempts: the retry is budgeted inside the cap (decision 325).
ATTEMPTS = 2

# Refusal kinds.
NO_CAP = "no_cap"
PLAN = "plan"
OVER_CAP = "over_cap"
OVER_CAP_PREFIX = "over spend cap"

# Equal to `acquire/pipeline.NO_SPEND_CAP` (tested); copied because the import runs the other way.
NO_CAP_REASON = (
    "no spend cap is configured, and §8 says a paid stage never auto-retries past one. Configure "
    "the extraction providers and the cap in Admin, and this title resumes here"
)

_CENT = Decimal("0.01")
_ZERO = Decimal("0.000000")

# Prefix of `llm_call.error` while a row stands at its write-ahead ceiling.
UNSETTLED_PREFIX = "unsettled"


@dataclass(frozen=True)
class ProviderPlan:
    """One provider a title's extraction will call: the model, the key and the price it bills at."""

    provider: str
    model: str
    # `repr=False` so the key never lands in a traceback or log line.
    key: str = field(repr=False)
    price: ModelPrice
    # Kept so each attempt can be priced on its own day (`attempt_price`).
    override: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Plan:
    """Who stage 6 calls and how often. `runs` is one attempt of the whole extraction, in calls."""

    providers: tuple[ProviderPlan, ...]
    passes: int

    @property
    def runs(self) -> int:
        return len(self.providers) * self.passes


@dataclass(frozen=True)
class Refusal:
    """Why stage 6 may not call a provider for this title now: `NO_CAP`, `PLAN` or `OVER_CAP`.

    `reason` is shown verbatim; `detail` carries only JSON values, money as strings.
    """

    kind: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Clock:
    """One reading of decision 325's month: its UTC bounds, the local day, and how a reason names it."""

    start: datetime
    end: datetime
    today: date
    month: str
    rolls_over: str
    tz: str


def local_zone() -> tzinfo | None:
    """§2's `TZ` as a zone, or None when the name does not resolve (logged on every read)."""
    name = settings().tz
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad TZ must not stop a paid stage's check (§3.1)
        log.warning(
            "TZ %r does not resolve to a time zone, so the spend cap's calendar month is bounded on"
            " this process's own clock instead (decision 325)", name,
        )
        return None


def _first_instant(year: int, month: int, zone: tzinfo | None) -> datetime:
    """The first instant of a local month, as UTC; localised from the wall time because of DST."""
    wall = datetime(year, month, 1)
    aware = wall.replace(tzinfo=zone) if zone is not None else wall.astimezone()
    return aware.astimezone(UTC)


def _clock(now: datetime | None) -> _Clock:
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        # A naive instant has no month; guessing its zone silently is not this function's job.
        raise ValueError(f"the spend month needs an aware instant, not the naive {instant!r}")
    zone = local_zone()
    local = instant.astimezone(zone)
    year, month = local.year, local.month
    following = (year + 1, 1) if month == 12 else (year, month + 1)
    return _Clock(
        start=_first_instant(year, month, zone),
        end=_first_instant(*following, zone),
        today=local.date(),
        month=f"{year:04d}-{month:02d}",
        rolls_over=f"{following[0]:04d}-{following[1]:02d}-01",
        tz=settings().tz if zone is not None else f"process local time (TZ {settings().tz!r} unresolved)",
    )


def period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Decision 325's month containing `now`, as aware UTC bounds [start, next start)."""
    clock = _clock(now)
    return clock.start, clock.end


async def _spent_between(conn: asyncpg.Connection, start: datetime, end: datetime) -> Decimal:
    return await conn.fetchval(
        "SELECT COALESCE(SUM(usd), 0) FROM llm_call WHERE at >= $1 AND at < $2", start, end
    )


async def _unsettled_between(conn: asyncpg.Connection, start: datetime, end: datetime) -> Decimal:
    """The month's spend still standing at write-ahead ceilings; part of the sum, never subtracted."""
    return await conn.fetchval(
        "SELECT COALESCE(SUM(usd), 0) FROM llm_call"
        " WHERE at >= $1 AND at < $2 AND NOT ok AND error LIKE $3",
        start, end, f"{UNSETTLED_PREFIX}%",
    )


async def spent(conn: asyncpg.Connection, *, now: datetime | None = None) -> Decimal:
    """What this month has cost: the SUM of every metered attempt's `usd` in the local month."""
    start, end = period(now)
    return await _spent_between(conn, start, end)


async def record_call(
    conn: asyncpg.Connection,
    *,
    provider: str,
    model: str,
    title_id: int | None,
    pass_index: int,
    attempt: int,
    tokens_in: int,
    tokens_out_billed: int,
    usd: Decimal,
    ok: bool,
    error: str | None,
    pack_document_id: int,
    response_document_id: int | None,
) -> int:
    """Meter one paid attempt; returns the `llm_call` row's id.

    One row per attempt, written before the POST at its ceiling and settled after (`settle_call`).
    """
    return await conn.fetchval(
        """
        INSERT INTO llm_call (provider, model, task, title_id, pass_index, attempt, tokens_in,
                              tokens_out_billed, usd, ok, error, pack_document_id,
                              response_document_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        provider, model, TASK, title_id, pass_index, attempt, tokens_in, tokens_out_billed, usd,
        ok, error, pack_document_id, response_document_id,
    )


async def settle_call(
    conn: asyncpg.Connection,
    call_id: int,
    *,
    error: str | None,
    tokens_in: int | None = None,
    tokens_out_billed: int | None = None,
    usd: Decimal | None = None,
    response_document_id: int | None = None,
) -> None:
    """Settle a written-ahead attempt (decision 436 (3)), or with no counts leave it at its ceiling.

    Never touches `title_id`: a deleted title's row was set NULL by the FK.
    """
    await conn.execute(
        """
        UPDATE llm_call
           SET tokens_in = coalesce($2, tokens_in),
               tokens_out_billed = coalesce($3, tokens_out_billed),
               usd = coalesce($4, usd),
               ok = $5,
               error = $6,
               response_document_id = coalesce($7, response_document_id)
         WHERE id = $1
        """,
        call_id, tokens_in, tokens_out_billed, usd, error is None, error, response_document_id,
    )


def attempt_price(planned: ProviderPlan, *, now: datetime | None = None) -> ModelPrice:
    """The price one attempt is metered at: decision 343's effective price on the attempt's own local day.
    """
    if planned.override is None:
        return planned.price
    today = pricing.effective_price(planned.provider, planned.model, override=planned.override,
                                    on=_clock(now).today)
    return today if today is not None else planned.price


async def _settings(conn: asyncpg.Connection) -> dict[str, Any]:
    return (await registry.load_connector(conn, SETTINGS)).config


def _cap_of(config: Mapping[str, Any]) -> Decimal | None:
    """`cap_usd`, read defensively: a number >= 0, or unset (never zero, never infinity, for a typo)."""
    value = config.get("cap_usd")
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not math.isfinite(value) or value < 0):
        log.warning(
            "the llm connector's cap_usd is %r, which is not a number of at least 0 (USD per"
            " calendar month), so no spend cap is in force and stage 6 parks as if none were set"
            " (decision 325)", value,
        )
        return None
    return Decimal(str(value))


async def cap(conn: asyncpg.Connection) -> Decimal | None:
    """The monthly cap in USD, or None when none is in force. No default ships (decision 325)."""
    return _cap_of(await _settings(conn))


def _refused(reason: str, **detail: Any) -> Refusal:
    return Refusal(PLAN, reason, detail)


def _passes_of(config: Mapping[str, Any]) -> int | Refusal:
    passes = config.get("passes", 1)
    if isinstance(passes, bool) or not isinstance(passes, int) or passes < 1:
        return _refused(
            f"the llm connector's passes is {passes!r}; it is a whole number of at least 1, and"
            " absent means 1 (decision 324). Correct it in Admin, and this title resumes here",
            setting="passes", value=passes,
        )
    return passes


def _providers_of(config: Mapping[str, Any]) -> tuple[str, ...] | Refusal:
    """The providers decision 324's settings name; never guessed, never one this build cannot call."""
    parallel = config.get("parallel", False)
    if not isinstance(parallel, bool):
        return _refused(
            f"the llm connector's parallel is {parallel!r}; it is true or false, and absent means"
            " false (decision 324). Correct it in Admin, and this title resumes here",
            setting="parallel", value=parallel,
        )
    if not parallel:
        named = config.get("extraction_provider")
        if not named:
            return _refused(
                "no extraction provider is assigned, and stage 6 never guesses one (decision 324)."
                f" Assign one of {', '.join(client.PROVIDERS)} in Admin, and this title resumes"
                " here",
                setting="extraction_provider",
            )
        chosen = [named]
        setting = "extraction_provider"
    else:
        chosen = config.get("parallel_providers")
        setting = "parallel_providers"
        if not isinstance(chosen, list) or not chosen:
            return _refused(
                f"parallel mode is on and parallel_providers is {chosen!r}, which names no provider"
                f" to call (decision 324). Choose from {', '.join(client.PROVIDERS)} in Admin, or"
                " turn parallel mode off, and this title resumes here",
                setting=setting, value=chosen,
            )
    unknown = [name for name in chosen if name not in client.PROVIDERS]
    if unknown:
        return _refused(
            f"the llm connector's {setting} names {', '.join(repr(n) for n in unknown)}, which this"
            f" build does not call; it calls {', '.join(client.PROVIDERS)} (spec section 9)",
            setting=setting, value=chosen,
        )
    return tuple(dict.fromkeys(chosen))


def proposed(stored: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    """A connector row's settings as a change would leave them, written nowhere (decision 450).

    An unnamed field keeps its value, a value replaces it, None removes it.
    """
    merged = dict(stored)
    for name, value in change.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


_BATCH_ADVICE = "Launch it again from the extraction queue in Admin, and this title resumes here"


def _batched(config: Mapping[str, Any], batch: Any) -> Mapping[str, Any] | Refusal:
    """The stored settings with a launched batch's plan merged over them, or why the plan cannot be.

    Merges only providers, `parallel` and `passes`, never the cap, keys or models. A batch that does
    not read is refused; the stored settings never run in its place (decision 442).
    """
    if batch is None:
        return config
    if not isinstance(batch, Mapping):
        return _refused(
            f"the flywheel batch this title was launched in is a {type(batch).__name__} and not a"
            f" plan of providers and passes (decision 442). {_BATCH_ADVICE}",
            setting="batch",
        )
    providers, passes = batch.get("providers"), batch.get("passes")
    if (not isinstance(providers, list) or not providers
            or not all(isinstance(name, str) and name in client.PROVIDERS for name in providers)):
        return _refused(
            f"the flywheel batch this title was launched in names providers {providers!r}; a batch"
            f" names one or more of {', '.join(client.PROVIDERS)} (decision 442). {_BATCH_ADVICE}",
            setting="batch.providers", value=providers,
        )
    if isinstance(passes, bool) or not isinstance(passes, int) or passes < 1:
        return _refused(
            f"the flywheel batch this title was launched in runs {passes!r} pass(es); a batch runs a"
            f" whole number of at least 1 (decision 442). {_BATCH_ADVICE}",
            setting="batch.passes", value=passes,
        )
    chosen = list(dict.fromkeys(providers))
    merged = dict(config)
    merged["passes"] = passes
    if len(chosen) == 1:
        merged.update(parallel=False, extraction_provider=chosen[0])
    else:
        merged.update(parallel=True, parallel_providers=chosen)
    return merged


async def _plan(
    conn: asyncpg.Connection,
    config: Mapping[str, Any],
    *,
    on: date,
    providers: Mapping[str, Mapping[str, Any]] | None = None,
) -> Plan | Refusal:
    """`providers` is a proposed change laid over each stored row, so `preview` and the gate plan
    through one function (decision 450). The gate passes none.
    """
    passes = _passes_of(config)
    if isinstance(passes, Refusal):
        return passes
    names = _providers_of(config)
    if isinstance(names, Refusal):
        return names
    planned = []
    for name in names:
        state = await registry.load_connector(conn, name)
        if providers and providers.get(name):
            state = replace(state, config=proposed(state.config, providers[name]))
        # Unreadable before keyless, so the admin is not sent to retype a key that exists.
        if state.secrets_unreadable:
            return _refused(
                f"the {name} key cannot be read: {registry.SECRETS_UNREADABLE_REASON}. Restore"
                f" the SECRETS_KEY it was sealed under, or type the {name} key again in Admin",
                provider=name,
            )
        key = str(state.secrets.get("api_key") or "")
        if not key:
            return _refused(
                f"no API key is configured for {name}, which extraction is assigned to. Add it in"
                " Admin, and this title resumes here",
                provider=name,
            )
        if client.header_key(key) is None:
            # A key no header can carry parks naming the fault, never the key.
            return _refused(
                f"the {name} key holds a character no HTTP header can carry (whitespace inside it,"
                " a control character or a non-ASCII letter). Type it again in Admin, and this"
                " title resumes here",
                provider=name,
            )
        model = str(state.config.get("model") or pricing.DEFAULT_MODELS[name])
        if name == "anthropic" and anthropic.refuses_forced_tool(model):
            # A model refusing forced tool use is a setting to correct, parked before anything is sent.
            return _refused(
                f"{name} model {model!r} refuses forced tool use, which is how this app asks Anthropic"
                " for a structured answer (spec section 9), so every call to it would be refused."
                f" Choose another {name} model in Admin, and this title resumes here",
                provider=name, model=model,
            )
        price = pricing.effective_price(name, model, override=state.config, on=on)
        if price is None:
            return _refused(
                f"no price is known for {name} model {model!r}, and a spend cap cannot be held"
                " against a price nobody knows (decision 343). Set price_input and price_output"
                f" for {name} in Admin, or choose a priced model, and this title resumes here",
                provider=name, model=model,
            )
        planned.append(ProviderPlan(
            provider=name, model=model, key=key, price=price,
            override={field_: state.config.get(field_) for field_ in pricing.OVERRIDE_FIELDS},
        ))
    return Plan(providers=tuple(planned), passes=passes)


async def extraction_plan(
    conn: asyncpg.Connection, *, now: datetime | None = None, batch: Mapping[str, Any] | None = None
) -> Plan | Refusal:
    """Who stage 6 calls, with which model and key and at what price, or why it cannot.

    Every refusal is a `PLAN` naming the one thing to fix.
    """
    config = _batched(await _settings(conn), batch)
    if isinstance(config, Refusal):
        return config
    return await _plan(conn, config, on=_clock(now).today)


@dataclass(frozen=True)
class Preview:
    """§6.6's "per-title cost estimate before enabling", as plain values (decision 450)."""

    plan: Plan | Refusal
    per_title: Decimal | None
    basis: tuple[pricing.PriceBasis, ...]
    blocked: str | None


def _key_fault(name: str, state: registry.ConnectorState) -> str | None:
    """Why `_plan` would refuse this provider's key, or None when stage 6 could send it."""
    if state.secrets_unreadable:
        return f"the {name} key cannot be read: {registry.SECRETS_UNREADABLE_REASON}"
    key = str(state.secrets.get("api_key") or "")
    if not key:
        return f"no API key is configured for {name}"
    if client.header_key(key) is None:
        return (f"the {name} key holds a character no HTTP header can carry (whitespace inside it, a"
                " control character or a non-ASCII letter)")
    return None


async def _blocked(conn: asyncpg.Connection, config: Mapping[str, Any]) -> str | None:
    """The first planned provider whose key stage 6 could not send, as a sentence.

    Checks every planned provider, not just the plan's first refusal (decision 450).
    """
    names = _providers_of(config)
    if isinstance(names, Refusal):
        return None
    for name in names:
        fault = _key_fault(name, await registry.load_connector(conn, name))
        if fault is not None:
            return (f"{fault}, so {name} cannot be put into the extraction plan: a key saved later"
                    " would start spend at a figure nobody was shown (decision 450). Save a usable"
                    f" {name} key on its card first")
    return None


async def preview(
    conn: asyncpg.Connection,
    *,
    change: Mapping[str, Any] | None = None,
    providers: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> Preview:
    """What stage 6 would cost per title if `change` and `providers` were stored. Reads only."""
    clock = _clock(now)
    config = proposed(await _settings(conn), change or {})
    plan = await _plan(conn, config, on=clock.today, providers=providers)
    blocked = await _blocked(conn, config)
    if isinstance(plan, Refusal):
        return Preview(plan=plan, per_title=None, basis=(), blocked=blocked)
    per_title = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS,
        prices=[chosen.price for chosen in plan.providers],
        passes=plan.passes,
    )
    basis = tuple(
        found for chosen in plan.providers
        if (found := pricing.price_basis(chosen.provider, chosen.model, override=chosen.override,
                                         on=clock.today)) is not None
    )
    return Preview(plan=plan, per_title=per_title, basis=basis, blocked=blocked)


# Decision 451's trailing window.
PROJECTION_DAYS = 30

NO_HISTORY_REASON = (
    "there is no acquisition history yet: this install has filed no title, so there is no rate to"
    " project a month from (decision 451)"
)
UNKNOWN_MONTH_REASON = (
    "the per-title figure is unknown, so the month is too (decision 343)"
)
NO_CAP_PROJECTION_REASON = (
    "no spend cap is configured, so stage 6 parks every title until one is set (decisions 325, 348)"
)


@dataclass(frozen=True)
class Projection:
    """Decision 451's projected month. `ever_filed` tells "nothing filed" from "unknown figure"."""

    titles: int
    ever_filed: bool
    monthly: Decimal | None
    remaining: Decimal | None
    exceeds_remaining: bool | None
    reason: str | None


def projection(
    per_title: Decimal | None, *, titles: int, ever_filed: bool, remaining: Decimal | None
) -> Projection:
    """The per-title figure times the titles filed in the window, beside the month's remaining cap.

    No history is no figure, never zero (decision 451).
    """
    reasons = []
    monthly = None
    if not ever_filed:
        reasons.append(NO_HISTORY_REASON)
    elif per_title is None:
        reasons.append(UNKNOWN_MONTH_REASON)
    else:
        monthly = per_title * titles
    if remaining is None:
        reasons.append(NO_CAP_PROJECTION_REASON)
    return Projection(
        titles=titles,
        ever_filed=ever_filed,
        monthly=monthly,
        remaining=remaining,
        exceeds_remaining=None if monthly is None or remaining is None else monthly > remaining,
        reason="; ".join(reasons) or None,
    )


async def reservation(conn: asyncpg.Connection, plan: Plan, *, title_id: int) -> Decimal | None:
    """What this title's extraction may cost, both attempts of every run, priced before any runs.

    None when the title has no stored pack under the active vocabulary.
    """
    version = await active_version(conn)
    if version is None:
        return None
    pack = await verify.read_pack(conn, title_id, version)
    if pack is None:
        return None
    voc = await contract.load_prompt_vocabulary(conn, version)
    tokens_in = (client.estimate_tokens(contract.system_prompt(voc))
                 + client.estimate_tokens(contract.user_prompt(pack)))
    per_attempt = pricing.estimate_title(
        tokens_in=tokens_in, prices=[p.price for p in plan.providers], passes=plan.passes,
    )
    # A Plan built by hand around an unpriced provider; no cap holds against it.
    return None if per_attempt is None else per_attempt * ATTEMPTS


def _dearer(plan: Plan, day: date) -> Plan:
    """The plan with each provider priced at the dearer of its own price and the one in effect on
    `day`, so a price turning over at midnight mid-title stays inside the reservation.
    """
    dearer = []
    for planned in plan.providers:
        then = None if planned.override is None else pricing.effective_price(
            planned.provider, planned.model, override=planned.override, on=day)
        price = planned.price if then is None else ModelPrice(
            max(planned.price.input, then.input), max(planned.price.output, then.output),
            cache_write=_most(planned.price.cache_write, then.cache_write),
            cache_read=_most(planned.price.cache_read, then.cache_read))
        dearer.append(replace(planned, price=price))
    return replace(plan, providers=tuple(dearer))


def _most(one: float | None, other: float | None) -> float | None:
    """The larger of two optional cache rates; None only when both are."""
    known = [rate for rate in (one, other) if rate is not None]
    return max(known) if known else None


def _money(amount: Decimal) -> str:
    """Whole cents when exact, every digit otherwise, so a real fraction never shows as "$0.00"."""
    if amount == amount.quantize(_CENT):
        return f"${amount.quantize(_CENT)}"
    return f"${amount.normalize():f}"


def _over(clock: _Clock, used: Decimal, limit: Decimal, need: Decimal | None, plan: Plan,
          unsettled: Decimal = _ZERO) -> Refusal:
    spent_part = (f"{_money(used)} of the {_money(limit)} monthly cap is spent for {clock.month}"
                  f" ({clock.tz})")
    if unsettled > 0:
        spent_part += (f" -- {_money(unsettled)} of it is calls whose answer never arrived, counted"
                       " at the most they could have billed (decision 436)")
    if need is None:
        middle = ", so no provider is called for this title."
    else:
        middle = (
            f", and this title's extraction reserves up to {_money(need)} ({ATTEMPTS} attempts x"
            f" {plan.passes} pass(es) x {len(plan.providers)} provider(s)), which would take the"
            " month past the cap. No provider is called for it."
        )
    return Refusal(
        OVER_CAP,
        f"{OVER_CAP_PREFIX}: {spent_part}{middle} It resumes by itself once the month rolls over on"
        f" {clock.rolls_over}, or once the cap is raised in Admin (decision 325)",
        {
            "spent_usd": str(used),
            "unsettled_usd": str(unsettled),
            "cap_usd": str(limit),
            "reserved_usd": None if need is None else str(need),
            "period_start": clock.start.isoformat(),
            "period_end": clock.end.isoformat(),
        },
    )


async def cap_check(
    conn: asyncpg.Connection,
    *,
    title_id: int,
    now: datetime | None = None,
    batch: Mapping[str, Any] | None = None,
) -> Refusal | None:
    """None when stage 6 may call its providers for this title now; otherwise why not.

    In this order: no cap, no plan, month at or above cap, no stored pack (None), month plus
    reservation above cap. Ending exactly on the cap is within it.
    """
    clock = _clock(now)
    config = await _settings(conn)
    limit = _cap_of(config)
    if limit is None:
        return Refusal(NO_CAP, NO_CAP_REASON, {"cap_usd": None})
    config = _batched(config, batch)
    if isinstance(config, Refusal):
        return config
    plan = await _plan(conn, config, on=clock.today)
    if isinstance(plan, Refusal):
        return plan
    used = await _spent_between(conn, clock.start, clock.end)
    if used >= limit:
        return _over(clock, used, limit, None, plan,
                     await _unsettled_between(conn, clock.start, clock.end))
    need = await reservation(conn, _dearer(plan, clock.today + timedelta(days=1)), title_id=title_id)
    if need is None:
        return None
    if used + need > limit:
        return _over(clock, used, limit, need, plan,
                     await _unsettled_between(conn, clock.start, clock.end))
    return None


async def retry_refusal(
    conn: asyncpg.Connection,
    *,
    title_id: int,
    now: datetime | None = None,
    batch: Mapping[str, Any] | None = None,
) -> str | None:
    """The reason an admin retry of this title's parked stage 6 must be refused, or None.

    Advice only: `cap_check` still gates stage 6 however a task is made due.
    """
    refusal = await cap_check(conn, title_id=title_id, now=now, batch=batch)
    return None if refusal is None else refusal.reason


async def meter(conn: asyncpg.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """What M5.7's spend guard renders. `remaining_usd` is None with no cap and never below zero."""
    clock = _clock(now)
    used = await _spent_between(conn, clock.start, clock.end)
    limit = await cap(conn)
    return {
        "spent_usd": used,
        "unsettled_usd": await _unsettled_between(conn, clock.start, clock.end),
        "cap_usd": limit,
        "remaining_usd": None if limit is None else max(limit - used, _ZERO),
        "period_start": clock.start,
        "period_end": clock.end,
        "tz": clock.tz,
    }


__all__ = [
    "ATTEMPTS",
    "NO_CAP",
    "NO_CAP_REASON",
    "OVER_CAP",
    "OVER_CAP_PREFIX",
    "PLAN",
    "PROJECTION_DAYS",
    "UNSETTLED_PREFIX",
    "Plan",
    "Preview",
    "Projection",
    "ProviderPlan",
    "Refusal",
    "attempt_price",
    "cap",
    "cap_check",
    "extraction_plan",
    "local_zone",
    "meter",
    "period",
    "preview",
    "projection",
    "proposed",
    "record_call",
    "reservation",
    "retry_refusal",
    "settle_call",
    "spent",
]
