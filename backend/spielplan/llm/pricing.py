"""The dated price table, the one lookup into it, and the per-title estimate (§9, §6.6).

When the table does not know a price it answers None, never a guess (decision 343). Pure: no DB,
no network; the day is handed in.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

log = logging.getLogger("spielplan.llm.pricing")

# A dated snapshot of a row's model: Anthropic's `-20251001`, OpenAI's `-2025-08-07`.
_SNAPSHOT = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens.  Estimates - check the provider's pricing page.

    The meter and the cap rest on these, so each carries `valid_until` (exclusive; None for no announced
    end). `cache_write`/`cache_read` price cached prompt tokens; None bills them at `input`.
    """

    input: float
    output: float
    valid_until: date | None = None
    cache_write: float | None = None
    cache_read: float | None = None


# Keyed by provider, then by model id, newest first: the order Admin suggests them in. A row also prices
# its dated snapshots (`claude-haiku-4-5-20251001`) at the name boundary (decision 535).
PRICING: dict[str, dict[str, tuple[ModelPrice, ...]]] = {
    # https://platform.claude.com/docs/en/about-claude/pricing, read 2026-09-29. No cache prices: no
    # `cache_control` is sent. Mythos is invitation-only and has no row.
    "anthropic": {
        "claude-sonnet-5-5": (ModelPrice(2.0, 10.0),),
        "claude-opus-5-5": (ModelPrice(4.0, 20.0),),
        "claude-fable-5-1": (ModelPrice(10.0, 50.0),),
        "claude-opus-5": (ModelPrice(5.0, 25.0),),
        "claude-sonnet-5": (ModelPrice(2.0, 10.0),),
        "claude-fable-5": (ModelPrice(10.0, 50.0),),
        "claude-opus-4-8": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-7": (ModelPrice(5.0, 25.0),),
        "claude-sonnet-4-6": (ModelPrice(3.0, 15.0),),
        "claude-opus-4-6": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-5": (ModelPrice(5.0, 25.0),),
        "claude-haiku-4-5": (ModelPrice(1.0, 5.0),),
        "claude-sonnet-4-5": (ModelPrice(3.0, 15.0),),
    },
    # https://developers.openai.com/api/docs/pricing, read 2026-09-29: standard tier, under 272k.
    # GPT-5.6 and later bill a cache write (1.25x input) by default; earlier models bill none.
    "openai": {
        "gpt-6.1-sol": (ModelPrice(2.0, 10.0, cache_write=2.50, cache_read=0.10),),
        "gpt-6-sol": (ModelPrice(2.0, 10.0, cache_write=2.50, cache_read=0.20),),
        "gpt-6-luna": (ModelPrice(0.10, 0.50, cache_write=0.125, cache_read=0.01),),
        "gpt-6-astra": (ModelPrice(10.0, 50.0, cache_write=12.50, cache_read=1.00),),
        # Promotional "at least through November 21, 2026"; no later price is published.
        "gpt-5.6-sol": (ModelPrice(4.0, 20.0, valid_until=date(2026, 11, 22), cache_write=5.00,
                                   cache_read=0.40),),
        "gpt-5.6-terra": (ModelPrice(2.0, 12.0, cache_write=2.50, cache_read=0.20),),
        "gpt-5.6-luna": (ModelPrice(0.20, 1.20, cache_write=0.25, cache_read=0.02),),
        "gpt-5.5": (ModelPrice(5.0, 30.0, cache_read=0.50),),
        "gpt-5.4": (ModelPrice(2.50, 15.0, cache_read=0.25),),
        "gpt-5.4-mini": (ModelPrice(0.75, 4.50, cache_read=0.075),),
        "gpt-5.4-nano": (ModelPrice(0.20, 1.25, cache_read=0.02),),
        "gpt-5.2": (ModelPrice(1.75, 14.0, cache_read=0.175),),
        "gpt-5.1": (ModelPrice(1.25, 10.0, cache_read=0.125),),
        # Shut down 2026-12-11, and unpriced from then on.
        "gpt-5": (ModelPrice(1.25, 10.0, valid_until=date(2026, 12, 11), cache_read=0.125),),
        "gpt-5-mini": (ModelPrice(0.25, 2.0, valid_until=date(2026, 12, 11), cache_read=0.025),),
        "gpt-5-nano": (ModelPrice(0.05, 0.40, valid_until=date(2026, 12, 11), cache_read=0.005),),
        "gpt-4.1": (ModelPrice(2.0, 8.0, cache_read=0.50),),
        "gpt-4.1-mini": (ModelPrice(0.40, 1.60, cache_read=0.10),),
        "gpt-4o": (ModelPrice(2.50, 10.0, cache_read=1.25),),
        "gpt-4o-mini": (ModelPrice(0.15, 0.60, cache_read=0.075),),
    },
    # https://ai.google.dev/gemini-api/docs/pricing, read 2026-09-29: prompts up to 200k tokens.
    "gemini": {
        # Introductory through 2026-12-31; from 2027-01-01 these double to $1.50 / $7.50.
        "gemini-3.8-flash": (ModelPrice(0.75, 3.75, valid_until=date(2027, 1, 1)),
                             ModelPrice(1.50, 7.50)),
        "gemini-3.7-flash": (ModelPrice(0.75, 3.75, valid_until=date(2027, 1, 1)),
                             ModelPrice(1.50, 7.50)),
        "gemini-3.6-flash": (ModelPrice(0.75, 3.75, valid_until=date(2027, 1, 1)),
                             ModelPrice(1.50, 7.50)),
        "gemini-3.5-flash-lite": (ModelPrice(0.30, 2.50),),
        "gemini-3.5-flash": (ModelPrice(1.50, 9.00),),
        "gemini-3.1-pro-preview": (ModelPrice(2.00, 12.00),),
        "gemini-3.1-flash-lite": (ModelPrice(0.25, 1.50),),
        "gemini-3-flash-preview": (ModelPrice(0.50, 3.00),),
        # Open only to a key that has used them before: a new key gets 404 "no longer available to new
        # users".
        "gemini-2.5-pro": (ModelPrice(1.25, 10.0),),
        "gemini-2.5-flash": (ModelPrice(0.30, 2.50),),
        "gemini-2.5-flash-lite": (ModelPrice(0.10, 0.40),),
    },
}

# Stage 6's call is grounded generation from provided text, so a cheap mid-tier model;
# the model tier is most of the bill.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5.6-terra",
    "gemini": "gemini-3.7-flash",
}

# Every model in DEFAULT_MODELS reasons before answering, and reasoning tokens
# are billed as output while never appearing in the response.  Measured on
# this prompt: gpt-5-mini ~3.8k completion tokens, gemini-3.6-flash
# ~1.6k output plus ~2.3k thoughts.  Estimating from the ~780 tokens of JSON
# that actually come back would understate the bill roughly fivefold.
MEAN_OUTPUT_TOKENS = 3900

# Midpoint of §8 stage 6's "~20-27k input tokens/pass/title", used before a title has a pack.
SPEC_INPUT_TOKENS = 23_500

# Decision 343's override in the provider's `connector_config` row: USD per 1M, input and output.
OVERRIDE_FIELDS = ("price_input", "price_output")

# OpenAI's cache-write multiplier, used to price a written token under an override.
OPENAI_CACHE_WRITE = 1.25

_MILLION = Decimal(1_000_000)
_SIX_PLACES = Decimal("0.000001")


def price_for(provider: str, model: str, *, on: date | None = None) -> ModelPrice | None:
    """Price for a model, or None when we genuinely do not know.

    A row prices its own id and its dated snapshots - `gpt-5-mini-2025-08-07` is `gpt-5-mini` - and
    nothing else: `claude-opus-5-6` and `gpt-5-pro` are not the rows they begin with, so a new
    release never inherits another model's price. A row whose prices have all expired answers None.
    """
    for name, prices in (PRICING.get(provider) or {}).items():
        if model == name or (model.startswith(name) and _SNAPSHOT.fullmatch(model[len(name):])):
            return _in_effect(prices, on or date.today())
    return None


def _in_effect(prices: tuple[ModelPrice, ...], day: date) -> ModelPrice | None:
    """The first price in date order that has not ended by `day`."""
    for price in prices:
        if price.valid_until is None or day < price.valid_until:
            return price
    return None


def effective_price(
    provider: str,
    model: str,
    *,
    override: Mapping[str, Any] | None = None,
    on: date | None = None,
) -> ModelPrice | None:
    """The price a call to `model` is metered and estimated at: the admin's, else the table's.

    Only a complete override (both fields, each a number >= 0) wins; a partial one is ignored and logged.
    """
    given = {name: (override or {}).get(name) for name in OVERRIDE_FIELDS}
    if all(value is None for value in given.values()):
        return price_for(provider, model, on=on)
    if all(_is_price(value) for value in given.values()):
        price_in = float(given["price_input"])
        # OpenAI states the cache write as a multiple of input, so it carries over to an override.
        written = price_in * OPENAI_CACHE_WRITE if provider == "openai" else None
        return ModelPrice(price_in, float(given["price_output"]), cache_write=written)
    log.warning(
        "price override for %s ignored: price_input and price_output are set together, as numbers"
        " of at least 0 (USD per 1M tokens), or not at all (decision 343); got %r and %r, so %s's"
        " table price applies", provider, given["price_input"], given["price_output"], model,
    )
    return price_for(provider, model, on=on)


@dataclass(frozen=True)
class PriceBasis:
    """What an estimate was priced at, as §6.6's caption names it (decision 343).

    `then` is the table price once `price.valid_until` passes; None for overrides and open-ended prices.
    """

    provider: str
    model: str
    source: str
    price: ModelPrice
    then: ModelPrice | None


BASIS_TABLE = "table"
BASIS_OVERRIDE = "override"


def price_basis(
    provider: str,
    model: str,
    *,
    override: Mapping[str, Any] | None = None,
    on: date | None = None,
) -> PriceBasis | None:
    """The price `effective_price` answers for `model` on `on`, with its source and what it becomes,
    or None, which every caller renders "unknown" (decision 343).
    """
    price = effective_price(provider, model, override=override, on=on)
    if price is None:
        return None
    if all(_is_price((override or {}).get(name)) for name in OVERRIDE_FIELDS):
        return PriceBasis(provider, model, BASIS_OVERRIDE, price, None)
    then = None if price.valid_until is None else price_for(provider, model, on=price.valid_until)
    return PriceBasis(provider, model, BASIS_TABLE, price, then)


def _is_price(value: Any) -> bool:
    """A JSON number an admin could mean as a price; `bool` is refused."""
    return (
        isinstance(value, int | float) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def usd(tokens_in: int, tokens_out_billed: int, price: ModelPrice, *, cache_written: int = 0,
        cache_read: int = 0, rate: float = 1.0) -> Decimal:
    """What one call cost: `llm_call.usd`, the figure decision 325's meter sums.

    Prices enter through `str` and the sum is rounded once to six places, half away from zero, as
    Postgres rounds into numeric(12,6). Cache tokens and `rate` are applied before that rounding.
    """
    plain = max(tokens_in - cache_written - cache_read, 0)
    write = price.cache_write if price.cache_write is not None else price.input
    read = price.cache_read if price.cache_read is not None else price.input
    cost = (Decimal(plain) * Decimal(str(price.input))
            + Decimal(cache_written) * Decimal(str(write))
            + Decimal(cache_read) * Decimal(str(read))
            + Decimal(tokens_out_billed) * Decimal(str(price.output))) / _MILLION
    if rate != 1:
        cost *= Decimal(str(rate))
    return cost.quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)


def _written_in(tokens_in: int, price: ModelPrice) -> int:
    """How much of a prompt a price expects billed as written: all, where writes cost more than input."""
    return tokens_in if price.cache_write is not None and price.cache_write > price.input else 0


def ceiling(tokens_in: int, max_tokens: int, price: ModelPrice, *, rate: float = 1.0) -> Decimal:
    """The most one attempt can bill, which decision 436 (2) meters before sending: the bounded input
    priced as written where writes cost more, all of `max_tokens`, at the dearest `rate`.
    """
    return usd(tokens_in, max_tokens, price, cache_written=_written_in(tokens_in, price), rate=rate)


def estimate_title(
    *,
    tokens_in: int,
    prices: Sequence[ModelPrice | None],
    passes: int,
    tokens_out: int = MEAN_OUTPUT_TOKENS,
) -> Decimal | None:
    """One title's extraction, priced before it runs: runs = providers x passes.

    Any unknown price makes the whole estimate None. A pass count below one is refused.
    """
    if passes < 1:
        raise ValueError(f"an estimate needs at least one pass per provider, not {passes}")
    if any(price is None for price in prices):
        return None
    # The prompt priced as written where the provider bills a write above input.
    return sum((usd(tokens_in, tokens_out, price, cache_written=_written_in(tokens_in, price)) * passes
                for price in prices), Decimal("0.000000"))
