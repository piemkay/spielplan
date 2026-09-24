"""The meter, the plan and the cap check stage 6 passes before a provider is called. Spec v2.1 §8, §6.6.

§8: "paid stages (6) never auto-retry past the spend cap". §6.6's spend guard: "monthly cap, running
meter reading '$4.12 of $25.00 this month'". §9 lists "spend caps + meter" among the admin's
settings. Decision 348 gave the driver a gate, `refuse_uncapped_spend`, that parks an implemented
paid stage while no cap exists, and left the cap to M5.5. This module is that cap, and decisions
324, 325 and 343 are the rules it keeps.

THE DRIVER IMPORTS THIS MODULE AND NEVER THE REVERSE. Nothing here imports a stage, builds an
`Outcome` or parks anything: it answers with plain values -- a `Refusal`, or None for "run it" --
and `acquire/pipeline.refuse_uncapped_spend`, which keeps decision 348's name and becomes the cap
check rather than being routed around, turns a refusal into a park carrying
`until = stages.waiting_on_the_world()`, because the driver fails a gate park that carries no
deadline. A parked title is therefore re-asked daily with no paid call, and resumes by itself when
the month rolls over or the cap is raised -- which is not a retry past the cap, since nothing is
billed until this check passes (decision 325).

THE METER IS A SUM, NEVER A COUNTER (plan D2, decision 325). A counter is a second copy of what
`llm_call` already says, kept in step only by every writer remembering to bump it: a crash between
the call row and the bump, a retry that bumps twice, a restore of one of the two and not the other,
and the counter and the bill disagree with nothing left to say which is right. A drifted cap is not
a cap. `SUM(llm_call.usd)` over the window IS the bill, read through the `llm_call_at` index, and
it is exact because 0028 makes the column numeric: ten ten-cent calls sum to one dollar, where a
float column sums them to a figure the cap would be compared against instead.

THE MONTH IS THE HOUSEHOLD'S (decision 325): a calendar month in §2's `TZ`, bounded [the first
instant of the local month, the first instant of the next) and handed to the query as UTC
instants, so a `timestamptz` compares against them the same way under any session TimeZone. A zone
that does not resolve is `worker._local_zone`'s case and takes its answer -- the process's own
clock -- for §3.1's reason: a spelling in `TZ` must not stop a loop, and a month bounded on the
wrong clock is a better failure than a paid stage that can never be checked. It is logged here on
every read, because this module has no boot line to say it once. And it is written here rather
than imported, for `worker._now_local`'s own reason turned around: a module the backend's routes
read must not import the worker.

NO DEFAULT CAP SHIPS (decision 325). An unset `cap_usd` answers `no_cap`, whose reason is decision
348's `NO_SPEND_CAP` sentence word for word, so M5.1's park reads exactly as M5.1 shipped it.

ATTEMPT 2 IS BUDGETED INSIDE THE CAP (decision 325). Before attempt 1, `cap_check` reserves
`ATTEMPTS` x passes x providers x the per-call estimate -- the real system and user prompt for this
title's stored pack counted by `client.estimate_tokens`, `MEAN_OUTPUT_TOKENS` out, decision 343's
effective price -- and refuses when the month's spend plus that reservation exceeds the cap. That
reading has exactly the two outcomes the exit criterion names: a park with zero paid calls, or both
attempts run to a verdict. Checking attempt 2 against the cap on its own adds a third state nobody
named, a title that paid for attempt 1 and then parked before the retry attempt 1 needed, holding a
verdict it cannot finish. At or above the cap nothing is estimated at all: no reservation is small
enough to fit into a month with no room left.

AN UNPRICED MODEL IS REFUSED, NOT GUESSED (decision 343). A model neither the dated table nor the
admin's override prices is a model whose calls cannot be held to any cap, so `extraction_plan`
refuses it by provider and model before a reservation is attempted.

EVERY ATTEMPT IS IN THE METER FROM BEFORE IT IS SENT (decision 436). `record_call` writes the row
at the attempt's CEILING -- its input as `client.ceiling_input` bounds it and every token
`max_tokens` allows, at the dearest rate the request can be billed at, `pricing.ceiling` --
committed before the POST, so a concurrent check sees a call on the wire; `settle_call` then moves
it to the usage the provider reported (a refused 200 included) at the multiplier the envelope names,
to zero when the provider's status or a request that never left says no work was done, or leaves it
at the ceiling, marked `UNSETTLED_PREFIX`, when no answer arrived: a cancellation, a crash, a reply
lost after the provider may have generated it. So "what a call really billed lands in `llm_call`" is
true of every attempt, and a ceiling left standing is at least the bill by every margin the
providers publish -- a text that tokenizes denser than `ceiling_input`'s margin is the one way it can
read below it; `meter` names that part apart so an admin can tell spend on answers from spend on
calls nobody heard back from. Before this the row was written after the answer, and a cut-off, a
refusal, a cancelled call and a store that raised were metered at zero or not at all -- every one
invisible to the cap. The ceiling first counted the prompt text alone at 3.6 characters a token,
which sat about 6% below a lost Claude cut-off's bill on a spec-size pack. [M5.5 review cycle 1,
M55-METER-01, M55-METER-03, M55-SPEND-04; review cycle 2, M55-CAP-C2-01, M55-C2-METER-03]

OVERSHOOT IS BOUNDED, NOT CLOSED, and each way the month can end above its cap is accepted and
written down rather than hidden. The reservation is R = `ATTEMPTS` x runs x (estimated prompt x
input price + `MEAN_OUTPUT_TOKENS` x output price), at the dearer of the gate's day's price and the
next day's (`_dearer`); what one title can actually bill is at most its ceiling, W = `ATTEMPTS` x runs
x `pricing.ceiling` of `client.ceiling_input` and `client.MAX_OUTPUT_TOKENS`, because every request
goes out with `max_tokens` = 8,000 and not the 3,900 mean. Worked from the shipped prices and default
models on the largest pack `packs.render_pack` builds (about 26,900 estimated input tokens), W/R is
1.43 on OpenAI, 1.44 on Gemini and 1.83 on Anthropic, whose ceiling carries the tool prompt, the
tokenizer margin and US-only inference's 1.1 that R does not; it rises as the pack shrinks and the
fixed 8,000 dominates, to about 2.0 on a 1,000-token prompt (2.27 on Anthropic). This paragraph first
said "about 1.6 on a full pack and about 2 on a short one", from a W that was not yet a bound.

  * AN ESTIMATE IS AN ESTIMATE, AND THE RESERVATION IS OF THE MEAN. The prompt is counted at ~3.6
    characters a token (which runs short on Anthropic's newer tokenizer, `client.estimate_tokens`),
    the output at a measured mean, and neither count sees the schema an adapter sends beside the
    prompt or the violation list a retry appends (at most `contract.MAX_NAMED` lines). So one title
    can bill up to W - R past its reservation, and with one worker the month can end that far past
    its cap: 0.43 R to 0.83 R on the largest pack and up to 1.27 R on the shortest, by the figures
    above. The check before the last title that fitted passed on R, and the title billed up to W.
    What it really billed lands in `llm_call`, and every later check reads it.
  * THE CHECK IS NOT SERIALISED ACROSS WORKERS. `pipeline.drain` walks one task at a time, so a
    worker cannot race itself; two loops are the ordinary state only during a rolling restart
    (`queue.lease`). A lock held from this check across a provider call measured in minutes would
    stall every other walk. What an extra worker's concurrent title can add is not its reservation
    but its bill: the write-ahead row makes the attempt it has on the wire visible, and nothing
    reserves the attempts and runs it has not started. So with k loops checking at once the month
    can end up to (W - R) + (k - 1) x W past the cap -- with two loops, 2W - R, which is 1.87 R to
    2.66 R on the largest pack and up to 3.54 R on the shortest, where this paragraph used to promise
    one reservation per extra worker and then called 2.2 to 3.0 reservations "measured" -- a figure
    nothing in the tree produces, which was this same bound worked at the two W/R ratios the
    paragraph then gave, from a W that was not yet a bound. That is the whole of it, and it is written
    here rather than closed because the lock that would close it costs more than it saves at a
    household's volume. [M5.5 review cycle 1, M55-SPEND-03; review cycle 2, M55-CAP-C2-01,
    M55-C2-DOC-07]
  * A DATED PRICE THAT TURNS OVER MID-TITLE is held by the reservation, not added to the overshoot:
    each attempt is metered at its own day's price and R is priced at the dearer of the gate's day
    and the next (`_dearer`). [M5.5 review cycle 2, M55-CAP-C2-03]
  * AND PER TITLE, THE QUEUE'S CURVE. A retryable failure keeps decision 431's curve, and each walk
    passes this check afresh and may buy attempt 1 and the named retry again: at most
    `max_attempts` walks x runs x `ATTEMPTS` metered calls per title per task, each within the cap
    at the moment it was asked. A title holds one task per key (decision 322), each on its own curve
    -- `stages._FAILED_FOR_GOOD` stops a title's other keys after a permanent failure, never after an
    exhausted one -- so a title with two library copies can buy twice that, the named retry once per
    walk of each key. A walk the drain's budget cancels mid-call is one of those walks, its call
    left at its ceiling. [M5.5 review cycle 1, M55-BUDGET-05, NBR-01; review cycle 2, C2-PAID-03]
    A breaker pause met before anything in the extraction was billed parks with its attempt refunded
    instead (`extract.PAUSED`), and so adds no walk to this bound: it bought nothing.
  * AND A WALK MAY NOT FIT THE TICK (decision 438). One extraction makes runs x `ATTEMPTS` calls in
    sequence, each up to `client.TIMEOUT_S`, inside the drain's 420 s; a walk the budget cancels has
    paid for what it sent, keeps nothing, and is closed under `queue.ABANDONED` at `max_attempts`.
    The money is inside the bound above; the yield is not, and a plan whose calls outrun the tick
    pays four walks for no tag. Recorded and not enforced here, because the budget is `worker.py`'s
    and plan §8 keeps that file out of this milestone. [M5.5 review cycle 2, NBR-C2-02]
"""

from __future__ import annotations

import logging
import math
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
    from collections.abc import Mapping

    import asyncpg

log = logging.getLogger("spielplan.llm.spend")

# The `connector_config` row §6.6's settings live in (decisions 324, 325), and the one task M5 has
# a caller for (0028's CHECK on `llm_call.task`; decision 339 is M5.7's).
SETTINGS = "llm"
TASK = "extraction"

# §9's two-attempt pattern, which 0028's CHECK bounds the same way: every run is reserved at two
# attempts, because decision 325 budgets the retry inside the cap.
ATTEMPTS = 2

# Refusal kinds. The driver keeps decision 348's sentence for the first, and M5.7's card and the
# exit criterion match the second's reason on `OVER_CAP_PREFIX`.
NO_CAP = "no_cap"
PLAN = "plan"
OVER_CAP = "over_cap"
OVER_CAP_PREFIX = "over spend cap"

# `acquire/pipeline.NO_SPEND_CAP`, character for character, and `test_llm_spend.py` holds the two
# equal. A copy rather than an import because the import runs the other way -- the driver reads
# this module -- and the sentence has to be this module's answer as well as the driver's: the
# admin retry M5.6 builds shows `retry_refusal`'s string, and a refusal that read one way on the
# board and another on the retry would be two answers to one state.
NO_CAP_REASON = (
    "no spend cap is configured, and §8 says a paid stage never auto-retries past one. Configure "
    "the extraction providers and the cap in Admin, and this title resumes here"
)

_CENT = Decimal("0.01")
_ZERO = Decimal("0.000000")

# How an `llm_call.error` opens while the row stands at its write-ahead ceiling (decision 436): the
# call is in flight, or its answer never arrived to settle it. `meter` sums these rows apart, and a
# row settled to the provider's answer or to zero never carries it.
UNSETTLED_PREFIX = "unsettled"


@dataclass(frozen=True)
class ProviderPlan:
    """One provider a title's extraction will call: the model, the key and the price it bills at."""

    provider: str
    model: str
    # `registry.ConnectorState`'s rule: a provider key bills the household, and a default repr
    # would copy it into any traceback or `%r` line that happened to hold a plan (§9: "credentials
    # never hit logs").
    key: str = field(repr=False)
    price: ModelPrice
    # Decision 343's override as the provider's row held it when the plan was made, so each attempt
    # can be priced on its own day (`attempt_price`). None for a plan built by hand around a price.
    override: dict[str, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Plan:
    """Who stage 6 calls and how often (decision 324). A run is one provider at one pass (decision
    337), so `runs` is what one attempt of the whole extraction costs in calls."""

    providers: tuple[ProviderPlan, ...]
    passes: int

    @property
    def runs(self) -> int:
        return len(self.providers) * self.passes


@dataclass(frozen=True)
class Refusal:
    """Why stage 6 may not call a provider for this title now: `NO_CAP`, `PLAN` or `OVER_CAP`.

    `reason` is written for §6.6's board, verbatim, and from M5.6 for the admin retry; `detail` is
    written for the board row's jsonb detail, so it carries only JSON values -- money as strings,
    whose digits are the ones the meter summed.
    """

    kind: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Clock:
    """One reading of decision 325's month: its UTC bounds, the local day prices are asked for,
    and the words a reason names the month and its zone in."""

    start: datetime
    end: datetime
    today: date
    month: str
    rolls_over: str
    tz: str


def local_zone() -> tzinfo | None:
    """§2's `TZ` as a zone, or None when this process cannot resolve the name it was given.

    `worker._local_zone`'s rule and its fallback (§3.1: a typo in `TZ` must not stop the loop), and
    unlike it this logs, on every read, because no boot line here reports the fallback once.
    """
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
    """The first instant of a local month, as UTC.

    Localised from the wall time rather than by shifting another instant's offset, because a month
    can start on one side of a DST change and end on the other. With no zone, a naive datetime's
    `astimezone()` is the process's own local clock -- the fallback `worker._now_local` takes.
    """
    wall = datetime(year, month, 1)
    aware = wall.replace(tzinfo=zone) if zone is not None else wall.astimezone()
    return aware.astimezone(UTC)


def _clock(now: datetime | None) -> _Clock:
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        # A naive instant has no month until someone guesses its zone, and the guess would be
        # this function's to make silently. The callers pass None or an aware instant.
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
    """Decision 325's month containing `now` (default: this instant), as aware UTC bounds
    [start, next start)."""
    clock = _clock(now)
    return clock.start, clock.end


async def _spent_between(conn: asyncpg.Connection, start: datetime, end: datetime) -> Decimal:
    return await conn.fetchval(
        "SELECT COALESCE(SUM(usd), 0) FROM llm_call WHERE at >= $1 AND at < $2", start, end
    )


async def _unsettled_between(conn: asyncpg.Connection, start: datetime, end: datetime) -> Decimal:
    """The part of the month's spend that is attempts standing at their write-ahead ceiling -- in
    flight, or never answered (decision 436). Inside the SUM above and never subtracted from it."""
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

    ONE ROW PER ATTEMPT, AND BOTH ATTEMPTS ARE ROWS (plan C3): the retry is a second full input
    pass, and a meter that did not see it would under-report by up to 100% on exactly the titles
    whose first answer was bad. `tokens_out_billed` is what the adapter reported as BILLED output --
    Gemini's candidates plus its thoughts, OpenAI's completion tokens as they come (§9) -- and
    `usd` is `pricing.usd` of it at the attempt's own price (`attempt_price`) once the row is
    settled; a failed call that still billed is a row with its cost, one that billed nothing a row
    at zero, and one nobody heard back from a row at its ceiling (decision 436), for the corpus's
    reason that a run's failure modes are the most informative thing about it. This sentence used
    to be true only of answered calls: every refused or cut-off 200 was a row at zero, whatever the
    provider billed. [M5.5 review cycle 1, M55-DOC-02] 0028's constraints refuse a third attempt,
    a run numbered below one and an `ok` that disagrees with `error`.

    STAGE 6 CALLS THIS BEFORE THE POST, at the attempt's ceiling with `ok` false and an error opening
    `UNSETTLED_PREFIX`, and settles the row afterwards (`settle_call`); see the module docstring. On
    the stage's autocommit connection the INSERT is committed as it returns, which is what lets a
    concurrent cap check, and a cancellation, see it.
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
    """Settle a written-ahead attempt (decision 436 (3)): its tokens and cost as the provider
    reported them, or zero, with its outcome -- or, with no counts given, only its outcome, which
    leaves it at its ceiling. `response_document_id` cites the stored response once it is stored,
    which is after the tokens are settled so a store that raises cannot take the bill with it.

    Never `title_id`: the row was written naming the title, and a title deleted while its call was
    out has had it set NULL by 0028's `ON DELETE SET NULL` -- which only protects a row that already
    exists, and is the reason the row is written before the call (decision 430).
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
    """The price one attempt is metered at: decision 343's effective price on the attempt's OWN local
    day. The plan was priced once, when it was made, and every attempt used to be metered at that
    price -- so a title whose calls ran past a dated price's midnight was metered at the price the
    provider had stopped charging (the introductory Gemini figure on 2027-01-01). A price the table
    no longer answers for keeps the plan's, which the gate already held the cap against; a plan
    built by hand around a price keeps that price. [M5.5 review cycle 1, M55-SPEND-06]"""
    if planned.override is None:
        return planned.price
    today = pricing.effective_price(planned.provider, planned.model, override=planned.override,
                                    on=_clock(now).today)
    return today if today is not None else planned.price


async def _settings(conn: asyncpg.Connection) -> dict[str, Any]:
    return (await registry.load_connector(conn, SETTINGS)).config


def _cap_of(config: Mapping[str, Any]) -> Decimal | None:
    """`cap_usd`, read defensively: a number of at least zero, or no cap at all.

    ANYTHING ELSE IS UNSET, AND SAID SO -- never zero and never infinity. Until M5.7's guard ships
    an input the row is written by hand (decision 433), which is where a typo lives, and each of
    the two readings a typo could be given fails the wrong way: infinity bills without a limit,
    and zero parks every title under a sentence claiming the month is spent. Unset parks under
    decision 348's sentence, which tells the admin the one true thing -- no cap is in force. A
    string is refused as decision 343's price override refuses one, and `true` because a checkbox's
    answer is not one dollar. Zero itself is a cap: an admin may mean "spend nothing".
    """
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
    """The providers decision 324's settings name: the extraction assignment, or, in parallel mode,
    `parallel_providers` -- never a guessed one, and never one this build cannot call."""
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


async def _plan(conn: asyncpg.Connection, config: Mapping[str, Any], *, on: date) -> Plan | Refusal:
    passes = _passes_of(config)
    if isinstance(passes, Refusal):
        return passes
    names = _providers_of(config)
    if isinstance(names, Refusal):
        return names
    planned = []
    for name in names:
        state = await registry.load_connector(conn, name)
        # Unreadable before keyless, `registry._probe_provider`'s order and for its reason: an
        # unreadable row's secrets are empty, and "no key" would send the admin to type a key that
        # exists and only needs the right SECRETS_KEY back.
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
            # A key no header can carry is a setting to correct, not a provider's refusal, so it
            # parks naming the fault -- and never the key -- before anything is reserved or sent.
            # Whitespace around a key is taken off by `client.complete`, not refused here.
            # [M5.5 review cycle 1, KEYS-C1-01]
            return _refused(
                f"the {name} key holds a character no HTTP header can carry (whitespace inside it,"
                " a control character or a non-ASCII letter). Type it again in Admin, and this"
                " title resumes here",
                provider=name,
            )
        model = str(state.config.get("model") or pricing.DEFAULT_MODELS[name])
        if name == "anthropic" and anthropic.refuses_forced_tool(model):
            # A model that refuses the adapter's one mechanism answers every call with a 400, which
            # decision 431 makes a permanent failure -- so it is a setting to correct, parked naming
            # the model before anything is reserved or sent, and never a title failed for good.
            # [M5.5 review cycle 1, M55-DBL-04]
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


async def extraction_plan(conn: asyncpg.Connection, *, now: datetime | None = None) -> Plan | Refusal:
    """Who stage 6 calls, with which model and key and at what price -- or why it cannot.

    Decision 324's settings from the `llm` row: one provider, `extraction_provider`, while
    `parallel` is off (its default), `parallel_providers` while it is on; `passes` defaults to 1.
    Each provider's model is its row's `model` or `pricing.DEFAULT_MODELS`, and its price is
    decision 343's effective one -- the admin's override, else the table's -- on the install's own
    day, since a dated price turns over on a local midnight like the month does. Every refusal is a
    `PLAN` naming the one thing to fix: no assignment, a setting that does not read, a provider
    this build cannot call, a key that is missing or unreadable, a model that refuses the adapter's
    mechanism, a model nobody priced.
    """
    return await _plan(conn, await _settings(conn), on=_clock(now).today)


async def reservation(conn: asyncpg.Connection, plan: Plan, *, title_id: int) -> Decimal | None:
    """What this title's extraction may cost, both attempts of every run, priced before any runs.

    The input is the real prompt stage 6 would send -- the system prompt over the active
    vocabulary and the user prompt over this title's STORED pack -- counted by
    `client.estimate_tokens`; the output is `MEAN_OUTPUT_TOKENS`, because the reasoning is billed and
    never seen. `pricing.estimate_title` multiplies by passes and sums over providers, and
    `ATTEMPTS` budgets the retry inside the cap (decision 325). None when the title has no stored
    pack under the active vocabulary: no call can be made without one, and stage 6 parks on that
    itself naming stage 5 (decision 432) rather than this module inventing a figure for it.

    A pack whose bytes are not the pack its row names raises, as `verify.read_pack` argues, and the
    driver fails the gate's stage on it like any raise inside a gate.
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
    # `_plan` refuses an unpriced provider before a Plan exists, so a None here is a Plan built by
    # hand around that refusal -- and a cap cannot be held against it either.
    return None if per_attempt is None else per_attempt * ATTEMPTS


def _dearer(plan: Plan, day: date) -> Plan:
    """The plan with each provider priced at the dearer, rate by rate, of its own price and the one in
    effect on `day`.

    THE RESERVATION IS HELD AT WHAT THE ATTEMPTS WILL BE METERED AT, and each attempt is metered at its
    own local day's price (`attempt_price`) while the gate priced the reservation at the gate's. A dated
    price turns over at a local midnight (decision 343: the shipped default gemini-3.7-flash doubles on
    2027-01-01), a gate asked a second before it lets a title through on the old price, and its two
    attempts run minutes later on the new one -- measured at 1.77 R past the cap with one worker,
    where decision 325 states W - R. `cap_check` passes the gate's next day, since one title's walk is
    bounded by the drain's budget and cannot span two midnights. A price with no successor keeps the
    plan's, as `attempt_price` does. [M5.5 review cycle 2, M55-CAP-C2-03]"""
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
    """The larger of two optional cache rates; None, which bills at `input`, only when both are."""
    known = [rate for rate in (one, other) if rate is not None]
    return max(known) if known else None


def _money(amount: Decimal) -> str:
    """Plain ASCII dollars: cents when the figure is whole cents, every metered digit when not, so
    a reason never rounds a real fraction of a cent to "$0.00"."""
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
    conn: asyncpg.Connection, *, title_id: int, now: datetime | None = None
) -> Refusal | None:
    """None when stage 6 may call its providers for this title now; otherwise why not.

    In this order, and the order is the rule:

      1. NO CAP -> `NO_CAP`, before anything else is read: with no cap in force there is nothing
         for "past the spend cap" to mean, and decision 348's sentence says so whatever else is
         missing.
      2. A PLAN THAT CANNOT BE MADE -> `PLAN` (decisions 324 and 343).
      3. THE MONTH AT OR ABOVE THE CAP -> `OVER_CAP`, without estimating anything.
      4. NO STORED PACK -> None: nothing can be called, and stage 6 parks on that itself.
      5. THE MONTH PLUS THIS TITLE'S RESERVATION ABOVE THE CAP -> `OVER_CAP` (decision 325:
         the retry is budgeted inside).

    Exactly at the cap is over it in step 3 -- there is no room left for any call -- and exactly
    at it after the reservation is within it in step 5, because a month that ends on its cap has
    not passed it.
    """
    clock = _clock(now)
    config = await _settings(conn)
    limit = _cap_of(config)
    if limit is None:
        return Refusal(NO_CAP, NO_CAP_REASON, {"cap_usd": None})
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
    conn: asyncpg.Connection, *, title_id: int, now: datetime | None = None
) -> str | None:
    """The reason an admin retry of this title's parked stage 6 must be refused, or None.

    §8's clause and the coverage row's: a manual retry that would breach the cap "is refused with
    that reason rather than queued". It is `cap_check`'s arithmetic and `cap_check`'s sentence, so
    the retry and the board can never give one state two answers. NO RETRY ROUTE EXISTS YET:
    decision 330's revive is M5.6's, and it calls this before it makes the task due. Nothing is
    lost in the meantime, because this is advice and not the guarantee -- a task made due by any
    other means, a raw UPDATE included, walks back into the driver's gate, which asks
    `cap_check` again before stage 6 can call anyone.
    """
    refusal = await cap_check(conn, title_id=title_id, now=now)
    return None if refusal is None else refusal.reason


async def meter(conn: asyncpg.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """What M5.7's spend guard renders -- "$4.12 of $25.00 this month" is §6.6's example of it and
    not a default (decision 325).

    Plain values, not an HTTP shape: `api/` decides how Decimals and instants are spelled.
    `remaining_usd` is None with no cap in force, and never below zero -- a month that overshot
    (see the module docstring) still shows its whole spend in `spent_usd`, and "minus four cents
    remaining" would be a second way of saying what that figure already says. `unsettled_usd` is
    the part of `spent_usd` standing at write-ahead ceilings (decision 436), so a month spent on
    calls nobody heard back from reads as that and not as answers. [M5.5 review cycle 1,
    M55-SPEND-04]
    """
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
    "UNSETTLED_PREFIX",
    "Plan",
    "ProviderPlan",
    "Refusal",
    "attempt_price",
    "cap",
    "cap_check",
    "extraction_plan",
    "local_zone",
    "meter",
    "period",
    "record_call",
    "reservation",
    "retry_refusal",
    "settle_call",
    "spent",
]
