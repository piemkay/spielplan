"""The dated price table, the one lookup into it, and the per-title estimate. Spec v2.1 §9, §6.6.

§9 asks for "spend caps + meter" and §6.6 for a "per-title cost estimate before enabling", and both
are a price multiplied by a token count. The count is the adapters' -- `LLMResult.tokens_out` is
BILLED output, reasoning included (§9: "counting visible JSON understates cost ~5x"). The price is
this module's, and it is the half that goes stale: a provider moves its price on its own schedule, a
model released after this table was typed has none, and the spend-cap row in `spec_coverage.toml`
says what follows from pricing a call wrongly -- "a cap computed from the wrong number is not a
cap". So the one rule this module exists to keep is decision 343's: when the table does not know,
it answers None, every caller renders that as "unknown" rather than a figure, and stage 6's gate
parks naming the unpriced model, because a cap cannot be enforced against a price nobody knows.

PURE: no database, no network, no clock but a default day. The admin's override lives in the
provider's `connector_config` row and `llm/spend` reads it and hands it in; the day a price is
asked about is handed in by the caller that knows the install's zone (decision 325's month is a
local one), and defaults to the process's own date, which is the install's under compose's `TZ`.

PORT VERDICT: **ported with named changes** from `mdc/config.py:119-203`. Verbatim: every figure in
`PRICING` and the three comments inside it (`:133-164`), `DEFAULT_MODELS` and the comment above it
(`:166-173`), the reasoning-token paragraph and `MEAN_OUTPUT_TOKENS` (`:181-186`), `ModelPrice`'s
first docstring line (`:121`), and `price_for`'s name-boundary rule and its docstring (`:189-203`).
What changed, each argued where it lands:

  1. **A price carries its date** (decision 343). `ModelPrice` gains `valid_until`, each leaf of
     `PRICING` becomes a tuple in date order, and gemini-3.7-flash and gemini-3.6-flash carry as a
     second row the doubled price the corpus's own comment predicts. One figure typed once is a
     known-wrong number in the meter on a known date.
  2. **`ModelPrice`'s second docstring paragraph is rewritten.** The corpus's says a price is "never
     used for anything but reporting, so a stale number costs a wrong estimate and nothing else".
     Here it is what `llm_call.usd` records and the cap sums (decision 325), and that sentence
     would be false beside it.
  3. **`price_for` answers for a day**, and a matched prefix whose prices have all expired answers
     None rather than falling through to a shorter prefix -- which would be exactly the inherited
     price the boundary rule exists to refuse.
  4. **`effective_price` is new**: the admin override decision 343 grants, both halves or neither.
  5. **`usd` and `estimate_title` are rewritten** from the corpus's two copies of one sum,
     `LLMResult.cost_usd` (`mdc/llm/client.py:60-66`) and `mdc aspects estimate`'s table cell
     (`mdc/aspects/cli.py:159-167`). The arithmetic is theirs, `(tokens_in x input + tokens_out x
     output) / 1M`. What changed: it is exact (`Decimal`, the six places of `llm_call.usd`'s
     `numeric(12,6)`); an unpriced model is None and never `cost_usd`'s `return 0.0`, which a meter
     would sum as a free call; and the estimate multiplies by passes and by providers (plan D4).
  6. **`SPEC_INPUT_TOKENS` is new**: the input an estimate assumes before a title has a pack.
  7. **Not carried:** `:175-179`'s advice to re-run `mdc doctor --models` after a provider
     announcement. It names a corpus command, and the question it tells an operator to re-ask is
     §6.6's test button here (`client.probe`'s `model_listed`, decision 433).
  8. **A written or read prompt token is priced apart** (`ModelPrice.cache_write` and
     `cache_read`, `usd`'s two counts, `ceiling`). OpenAI's GPT-5.6 and later write every prompt to
     their cache by default and bill the write at 1.25x input, and the corpus's one input rate
     metered that write at the flat rate. [M5.5 review cycle 1, M55-DBL-02, M55-DOC-05]
  9. **Anthropic's newer models have rows of their own** (decision 437), because the corpus's three
     family prefixes priced the shipped default `claude-sonnet-5` as Sonnet 4 -- $3/$15 against a
     published $2/$10 -- and every Opus from 4.5 on as Opus 4, three times its price: the confident
     number from a neighbouring name that `price_for`'s docstring refuses, reached through a
     prefix that was true when the corpus typed it. The family prefixes still price a model
     released after this table at its family's figure until somebody types its row; the rows
     below are what keeps every model this table knows off that path. [M5.5 review cycle 1,
     M55-METER-05, M55-DOC-04]
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

log = logging.getLogger("spielplan.llm.pricing")


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens.  Estimates - check the provider's pricing page.

    Named change 2. Here a price is not only reported: it is the figure `llm_call.usd` records for
    every attempt and the cap sums (decision 325), so a stale one is a wrong meter and a cap held
    against the wrong number. Which is why each carries the day it stops being true, and why a
    price past that day with nothing after it is not answered at all (`price_for`).

    `valid_until` is the first day this price no longer applies -- exclusive, because the corpus's
    note reads "from 2027-01-01 these double" -- and None for a price with no announced end.

    `cache_write` and `cache_read` are the per-1M prices of a prompt token the provider wrote to its
    cache and of one it read from it, where the provider bills those apart from input; None bills
    either at `input`. Named change 8.
    """

    input: float
    output: float
    valid_until: date | None = None
    cache_write: float | None = None
    cache_read: float | None = None


# --- `mdc/config.py:133-173`: figures and comments verbatim, leaves dated (named change 1) ----

# Keyed by provider, then by model id prefix (longest prefix wins).
PRICING: dict[str, dict[str, tuple[ModelPrice, ...]]] = {
    "anthropic": {
        "claude-opus": (ModelPrice(15.0, 75.0),),
        "claude-sonnet": (ModelPrice(3.0, 15.0),),
        "claude-haiku": (ModelPrice(1.0, 5.0),),
        # Named change 9 (decision 437): the models released since the corpus typed its three family
        # prefixes, each at the figure Anthropic's page gives it, read 2026-09-24
        # (https://platform.claude.com/docs/en/about-claude/pricing). The prefixes above stay for
        # the models they still price correctly -- Sonnet 4.x at $3/$15, Opus 4 and 4.1 at $15/$75,
        # Haiku at $1/$5 -- and every newer model has its own row, so the boundary rule finds it
        # before it can fall to the family's.
        "claude-sonnet-5": (ModelPrice(2.0, 10.0),),
        "claude-opus-5-5": (ModelPrice(4.0, 20.0),),
        "claude-opus-5": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-5": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-6": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-7": (ModelPrice(5.0, 25.0),),
        "claude-opus-4-8": (ModelPrice(5.0, 25.0),),
    },
    "openai": {
        "gpt-5-mini": (ModelPrice(0.25, 2.0),),
        "gpt-5-nano": (ModelPrice(0.05, 0.4),),
        "gpt-5": (ModelPrice(1.25, 10.0),),
        "gpt-4.1-mini": (ModelPrice(0.4, 1.6),),
        "gpt-4.1": (ModelPrice(2.0, 8.0),),
        # Standard (short-context) rate as of the 2026-07-30 update.  Prompts
        # here are ~800 tokens, nowhere near the 272k long-context tier that
        # would bill $4/$18.
        # (Named change 8, decision 437: the page's row is "$2.00 | $0.20 | $2.50 | $12.00" --
        # input, cached input, cache writes, output -- https://developers.openai.com/api/docs/pricing,
        # and GPT-5.6 writes the prompt to its cache by default; see `llm/openai.py`.)
        "gpt-5.6-terra": (ModelPrice(2.0, 12.0, cache_write=2.50, cache_read=0.20),),
    },
    "gemini": {
        # The 2.5 family is retired: it still appears in ListModels but
        # `generateContent` answers 404 "no longer available to new users",
        # so these prices only apply to a key old enough to keep access.
        "gemini-2.5-pro": (ModelPrice(1.25, 10.0),),
        "gemini-2.5-flash-lite": (ModelPrice(0.10, 0.40),),
        "gemini-2.5-flash": (ModelPrice(0.30, 2.50),),
        # Introductory pricing, and it is worth knowing it expires: from
        # 2027-01-01 these double to $1.50 / $7.50.  A corpus pass costed
        # today and run in January would be twice the estimate.
        "gemini-3.7-flash": (ModelPrice(0.75, 3.75, valid_until=date(2027, 1, 1)),
                             ModelPrice(1.50, 7.50)),
        "gemini-3.6-flash": (ModelPrice(0.75, 3.75, valid_until=date(2027, 1, 1)),
                             ModelPrice(1.50, 7.50)),
    },
}

# What each provider gets if LLM_MODEL is unset.  Spec 3.3 is explicit that
# pass 1 is *grounded generation from provided text* and should run on a cheap
# mid-tier model - at 10.8k titles the price difference is the entire bill.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.6-terra",
    "gemini": "gemini-3.7-flash",
}

# --- end of the ported table ------------------------------------------------------------------

# The comment on DEFAULT_MODELS is the corpus's own, so its `LLM_MODEL` is the corpus's env var and
# its "Spec 3.3" the corpus's spec. Here the model is the provider row's `model` in
# `connector_config` (§2 keeps connector settings out of the environment), and the pass it
# describes is §8 stage 6's extraction, which is the same grounded-from-provided-text call.

# --- `mdc/config.py:181-186`, verbatim ---------------------------------------------------------

# Every model in DEFAULT_MODELS reasons before answering, and reasoning tokens
# are billed as output while never appearing in the response.  Measured on
# this corpus's prompt: gpt-5-mini ~3.8k completion tokens, gemini-3.6-flash
# ~1.6k output plus ~2.3k thoughts.  Estimating from the ~780 tokens of JSON
# that actually come back would understate the bill roughly fivefold.
MEAN_OUTPUT_TOKENS = 3900

# --- end of the ported text; everything below is this port's ------------------------------------

# Named change 6. §8 stage 6: "~20-27k input tokens/pass/title" (`spec:389`), and 23,500 is its
# midpoint -- the input a per-title estimate assumes before a title has a pack to count, and the
# figure ROADMAP-M5.md's "What the paid stage costs" table is costed at. Once a pack exists the caller
# counts the real prompt with `client.estimate_tokens` instead and labels which of the two it used.
SPEC_INPUT_TOKENS = 23_500

# Decision 343's override, in the provider's `connector_config` row: USD per 1M tokens, input and
# output, the same two numbers a `ModelPrice` holds.
OVERRIDE_FIELDS = ("price_input", "price_output")

# Named change 8. "For GPT-5.6 and later, cache writes cost 1.25x the standard, uncached input-token
# rate" (https://developers.openai.com/api/docs/guides/prompt-caching), which is how an OpenAI
# override prices a written token; the table's own rows carry the published figure instead.
OPENAI_CACHE_WRITE = 1.25

_MILLION = Decimal(1_000_000)
_SIX_PLACES = Decimal("0.000001")


def price_for(provider: str, model: str, *, on: date | None = None) -> ModelPrice | None:
    """Price for a model, or None when we genuinely do not know.

    A prefix only matches at a name boundary - `gpt-5-mini-2025-08-07` matches
    `gpt-5-mini`, but `gpt-5.6-terra` does **not** match `gpt-5`.  A bare
    ``startswith`` looks harmless and is how a cost estimate quietly invents a
    number: a new point release inherits the price of a different model and
    reports a confident total nobody checked.  Returning None makes the gap
    visible instead.

    Named change 3: the price is the one in effect on `on` (default: today). A prefix whose every
    price has passed its `valid_until` answers None, and the search stops at the prefix that
    matched rather than trying a shorter one: `gemini-3.7-flash` past its last dated price is a
    model whose price nobody has typed, not a model priced as whatever shorter name it starts with.
    """
    table = PRICING.get(provider) or {}
    for prefix in sorted(table, key=len, reverse=True):
        if model == prefix or model.startswith(prefix + "-"):
            return _in_effect(table[prefix], on or date.today())
    return None


def _in_effect(prices: tuple[ModelPrice, ...], day: date) -> ModelPrice | None:
    """The first price in date order that has not ended by `day`; each takes over where the one
    before it stops, so the table needs an end date and never a start."""
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

    Named change 4. `override` is the provider's `connector_config` row as `llm/spend` read it;
    only `OVERRIDE_FIELDS` are looked at. Both set, each a number of at least zero, and the override
    wins -- over a table price and over a table None, since pricing a model this table has never
    heard of is what an override is for. Neither set, and the table answers.

    Anything between is ignored, and says so in the log on every read, the way `secrets_unreadable`
    does: an input price with no output price is not half an answer to "what does a call cost",
    and completing it from the table would bill at a figure the admin was in the middle of
    replacing. So it falls back to the whole table price -- or to None, and a park naming the
    unpriced model, when the table has none either.
    """
    given = {name: (override or {}).get(name) for name in OVERRIDE_FIELDS}
    if all(value is None for value in given.values()):
        return price_for(provider, model, on=on)
    if all(_is_price(value) for value in given.values()):
        price_in = float(given["price_input"])
        # OpenAI states the cache write as a multiple of the input price the override names, so it
        # carries over; a model that writes nothing reports no written tokens and is unaffected.
        written = price_in * OPENAI_CACHE_WRITE if provider == "openai" else None
        return ModelPrice(price_in, float(given["price_output"]), cache_write=written)
    log.warning(
        "price override for %s ignored: price_input and price_output are set together, as numbers"
        " of at least 0 (USD per 1M tokens), or not at all (decision 343); got %r and %r, so %s's"
        " table price applies", provider, given["price_input"], given["price_output"], model,
    )
    return price_for(provider, model, on=on)


def _is_price(value: Any) -> bool:
    """A JSON number an admin could mean as a price. `bool` is refused although Python counts it an
    `int`: `true` is a checkbox's answer, not one dollar."""
    return (
        isinstance(value, int | float) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def usd(tokens_in: int, tokens_out_billed: int, price: ModelPrice, *, cache_written: int = 0,
        cache_read: int = 0, rate: float = 1.0) -> Decimal:
    """What one call cost: `llm_call.usd`, the figure decision 325's meter sums.

    Named change 5. Each price enters through `str`, so a 0.30 is the 0.30 the table or the admin
    wrote and not the binary fraction nearest it, and the sum is rounded once, to the column's six
    places, ties away from zero -- which is what Postgres does to a value cast into numeric(12,6)
    (measured on this project's cluster: 0.0000025 stores as 0.000003), so the figure returned here
    is the figure the row keeps and a SUM of rows equals a sum of these.

    Named change 8: `cache_written` and `cache_read` are the parts of `tokens_in` the provider wrote
    to its cache and read from it, as OpenAI reports them inside `prompt_tokens`, and each is priced
    at its own rate -- the rest at `input`. A price with no cache rate charges them at `input`.

    `rate` is the multiplier the envelope says the whole call was billed at over the table's standard
    row -- Anthropic's US-only inference at 1.1, OpenAI's Fast tier at 2 (`client.LLMResult.rate`) --
    applied before the one rounding, since each provider states it "across all token pricing
    categories". [M5.5 review cycle 2, M55-C2-METER-01]
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
    """How much of a prompt a price expects to be billed as written: all of it, where the provider
    bills a write above input -- the default breakpoint writes a stage-6 prompt whole, and it is new
    for every title -- and none elsewhere."""
    return tokens_in if price.cache_write is not None and price.cache_write > price.input else 0


def ceiling(tokens_in: int, max_tokens: int, price: ModelPrice, *, rate: float = 1.0) -> Decimal:
    """The most one attempt can bill: its input as `client.ceiling_input` bounds it and every token
    `max_tokens` allows, every input token priced as written where writes cost more, at the dearest
    `rate` the request can be billed at (`client.ceiling_rate`). Decision 436 (2) meters an attempt at
    this before it is sent, and leaves it here when no answer arrives to settle it. It bounds the bill
    as far as its input count does, which is by each adapter's published margin and no further: a text
    that tokenizes denser than that margin allows is the one way a ceiling left standing reads below
    what was billed. [M5.5 review cycle 1, M55-METER-03; review cycle 2, M55-CAP-C2-01]"""
    return usd(tokens_in, max_tokens, price, cache_written=_written_in(tokens_in, price), rate=rate)


def estimate_title(
    *,
    tokens_in: int,
    prices: Sequence[ModelPrice | None],
    passes: int,
    tokens_out: int = MEAN_OUTPUT_TOKENS,
) -> Decimal | None:
    """One title's extraction, priced before it runs: plan D4, exported for M5.6's flywheel total
    and M5.7's guard, and rendered by nothing this milestone ships (plan §8: no UI).

    Every run is `tokens_in` in and `tokens_out` billed out -- `MEAN_OUTPUT_TOKENS` unless a caller
    has a better measurement, because the reasoning is billed and never seen -- at one provider's
    price; `prices` holds one entry per provider, and each provider runs `passes` times (decision
    324). So two providers at two passes is four runs, the "4x the single-run figure" the roadmap
    warns of. Any price None and the whole estimate is None, which the caller renders "unknown"
    (decision 343): a total that quietly left out the provider nobody could price is the confident
    wrong number `price_for`'s docstring refuses, only summed.

    A pass count below one is refused rather than priced: a run is numbered from one
    (`llm_call.pass_index`), and a zero or negative multiplier would make the estimate the gate
    reserves against the cap a figure that admits calls it never priced.
    """
    if passes < 1:
        raise ValueError(f"an estimate needs at least one pass per provider, not {passes}")
    if any(price is None for price in prices):
        return None
    # The prompt priced as written where the provider bills a write above input (named change 8):
    # the default breakpoint writes each title's prompt whole, and the estimate is of that bill.
    return sum((usd(tokens_in, tokens_out, price, cache_written=_written_in(tokens_in, price)) * passes
                for price in prices), Decimal("0.000000"))
