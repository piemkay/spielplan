"""The dated price table, the name-boundary lookup and the per-title estimate. Spec v2.1 §9, §6.6.

Exit-criterion checks 13 and 14 of `docs/milestones/M5.5-plan.md` §7 are the first two tests, and
the plan says why they are not trivia: "13/14 are how a cost estimate quietly invents a number". A
price the table does not hold is None and an estimate over it is None (decision 343), because the
spend-cap row's whole claim is that "a cap computed from the wrong number is not a cap" -- and a
price inherited from a neighbouring model name, or carried past the day the provider changed it, is
the wrong number with nothing about it that looks wrong.

The cost figures are `docs/milestones/ROADMAP-M5.md`'s "What the paid stage costs" table, reproduced
from the shipped table rather than restated: its sync rows are 23,500 input tokens (§8 stage 6's
midpoint) and 3,900 billed output tokens (the corpus's measurement) at `mdc/config.py`'s prices, and
if the port had moved a figure or dropped the reasoning tokens those rows would no longer come out.

NO DATABASE, NO NETWORK. The module is pure, so every test here is a function of its arguments;
the one day that matters, 2027-01-01, is passed in rather than waited for.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from spielplan.llm import pricing
from spielplan.llm.pricing import ModelPrice

# This document's date, fixed so no assertion moves when the calendar does; the introductory Gemini
# price is in effect on it and ends fifteen weeks later.
TODAY = date(2026, 9, 24)


def test_gpt_5_6_terra_is_the_terra_price_and_not_gpt_5s(monkeypatch):
    """Exit check 13. `gpt-5.6-terra` starts with `gpt-5`, and a bare `startswith` would bill it at
    $1.25/$10 - a confident total at 62% of the input rate and 83% of the output rate, with nothing
    about it that looks wrong. The table's own terra row hides that trap while it is there, so the
    second half takes the row away: the name must then be unpriced, not handed to `gpt-5`."""
    terra = pricing.price_for("openai", "gpt-5.6-terra", on=TODAY)
    assert (terra.input, terra.output) == (2.0, 12.0)
    gpt5 = pricing.price_for("openai", "gpt-5", on=TODAY)
    assert (gpt5.input, gpt5.output) == (1.25, 10.0)
    assert terra != gpt5

    without_terra = {prefix: prices for prefix, prices in pricing.PRICING["openai"].items()
                     if prefix != "gpt-5.6-terra"}
    monkeypatch.setitem(pricing.PRICING, "openai", without_terra)
    assert pricing.price_for("openai", "gpt-5.6-terra", on=TODAY) is None


def test_an_unknown_model_is_unpriced_and_its_estimate_is_unknown():
    """Exit check 14. A model the table never heard of answers None, and so does an estimate that
    needs it -- including one where the other provider IS priced, because a total that quietly
    leaves out the provider nobody could price is the same invented number, summed."""
    assert pricing.price_for("openai", "gpt-7", on=TODAY) is None
    assert pricing.price_for("mistral", "mistral-large", on=TODAY) is None
    # The point release a bare prefix match would have priced as gpt-5: `gpt-5.6` is not `gpt-5`
    # followed by a name boundary.
    assert pricing.price_for("openai", "gpt-5.6", on=TODAY) is None
    # A model name typed under the wrong provider is unknown there, not borrowed from the other.
    assert pricing.price_for("gemini", "claude-sonnet-5", on=TODAY) is None
    # The Anthropic ids decision 437 names as unpriced: the Claude 3.x ids begin `claude-3-`, not any
    # family prefix, and Fable and Mythos have no row. Decision 437 said the `claude-haiku` prefix would
    # price Haiku 3.5 at $1/$5 and left Mythos 5 off its list. [M5.5 review cycle 2, M55-C2-DOC-04]
    for model in ("claude-3-5-haiku-20241022", "claude-3-5-haiku-latest", "claude-fable-5-1",
                  "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5"):
        assert pricing.price_for("anthropic", model, on=TODAY) is None, model

    known = pricing.price_for("gemini", "gemini-3.7-flash", on=TODAY)
    assert pricing.estimate_title(tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[None], passes=1) is None
    assert pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[known, None], passes=2) is None
    assert pricing.effective_price("openai", "gpt-7", on=TODAY) is None


@pytest.mark.parametrize(("provider", "model", "prefix"), [
    ("openai", "gpt-5-mini-2025-08-07", "gpt-5-mini"),
    ("openai", "gpt-5-2025-08-07", "gpt-5"),
    ("openai", "gpt-5", "gpt-5"),
    ("openai", "gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
    # Sonnet 5 has its own row since decision 437, and a family prefix still prices the older models
    # it prices correctly: Sonnet 4.x at $3/$15 and Opus 4/4.1 at $15/$75.
    ("anthropic", "claude-sonnet-5", "claude-sonnet-5"),
    ("anthropic", "claude-sonnet-4-5-20250929", "claude-sonnet"),
    ("anthropic", "claude-opus-4-1-20250805", "claude-opus"),
    ("gemini", "gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
    ("gemini", "gemini-2.5-flash-preview-09-2025", "gemini-2.5-flash"),
])
def test_price_for_matches_a_prefix_only_at_a_name_boundary(provider, model, prefix):
    """`mdc/config.py:189-203`'s rule, kept verbatim: a prefix matches the whole name or the name up
    to a hyphen, and the longest matching prefix wins -- so a dated snapshot takes its model's price,
    `gemini-2.5-flash-lite` is not priced as `gemini-2.5-flash`, and `gpt-5-mini` is not `gpt-5`."""
    assert pricing.price_for(provider, model, on=TODAY) == pricing.PRICING[provider][prefix][0]


@pytest.mark.parametrize(("provider", "model"), [
    ("anthropic", "claude-sonnet5"),
    ("openai", "gpt-5x"),
    ("openai", "gpt-4.10"),
    ("gemini", "gemini-3.7-flashy"),
])
def test_a_name_that_runs_on_past_a_prefix_without_a_boundary_is_unpriced(provider, model):
    """The other half of the rule: `claude-sonnet5` is not `claude-sonnet` with a suffix, it is a name
    nobody priced, and `gpt-4.10` is not `gpt-4.1`."""
    assert pricing.price_for(provider, model, on=TODAY) is None


def test_the_gemini_introductory_price_ends_on_the_day_the_provider_said():
    """`mdc/config.py:158-160`: "from 2027-01-01 these double to $1.50 / $7.50. A corpus pass costed
    today and run in January would be twice the estimate." Both figures are the corpus's; the date
    beside the first is decision 343's, so the meter changes price on the provider's day and not on
    the day somebody next reads the comment."""
    for model in ("gemini-3.7-flash", "gemini-3.6-flash"):
        last_intro = pricing.price_for("gemini", model, on=date(2026, 12, 31))
        first_doubled = pricing.price_for("gemini", model, on=date(2027, 1, 1))
        assert (last_intro.input, last_intro.output) == (0.75, 3.75)
        assert last_intro.valid_until == date(2027, 1, 1)
        assert (first_doubled.input, first_doubled.output) == (1.50, 7.50)
        assert first_doubled.valid_until is None
        assert pricing.price_for("gemini", model, on=date(2031, 6, 1)) == first_doubled

    # The corpus's "twice the estimate", as the estimator computes it on either side of the day.
    before = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, passes=1,
        prices=[pricing.price_for("gemini", "gemini-3.7-flash", on=date(2026, 12, 31))])
    after = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, passes=1,
        prices=[pricing.price_for("gemini", "gemini-3.7-flash", on=date(2027, 1, 1))])
    assert after == 2 * before


def test_a_price_past_its_valid_until_with_no_successor_answers_none(monkeypatch):
    """Decision 343: a price whose day has passed with nothing typed after it is not a price. And the
    search stops at the prefix that matched: `m-pro`'s expired figure does not hand the model to `m`,
    which would be the inherited price the boundary rule refuses, reached by a calendar instead of a
    name."""
    monkeypatch.setattr(pricing, "PRICING", {"openai": {
        "m": (ModelPrice(1.0, 2.0),),
        "m-pro": (ModelPrice(5.0, 20.0, valid_until=date(2026, 1, 1)),),
    }})
    assert pricing.price_for("openai", "m-pro", on=date(2025, 12, 31)) == ModelPrice(
        5.0, 20.0, valid_until=date(2026, 1, 1))
    assert pricing.price_for("openai", "m-pro", on=date(2026, 1, 1)) is None
    assert pricing.price_for("openai", "m-pro-2025-06-01", on=date(2026, 1, 1)) is None
    # With no day given the answer is today's, and today is past the end.
    assert pricing.price_for("openai", "m-pro") is None
    assert pricing.price_for("openai", "m-lite", on=date(2026, 1, 1)) == ModelPrice(1.0, 2.0)
    assert pricing.estimate_title(
        tokens_in=1000, passes=1, prices=[pricing.price_for("openai", "m-pro")]) is None


def test_the_shipped_table_is_in_date_order_and_every_default_model_is_priced():
    """A dated row after an open one could never be reached, and a default model with no price
    would park stage 6 on every fresh install that took the default (decision 343's gate). Checked on
    both sides of the one date the table carries."""
    for provider, table in pricing.PRICING.items():
        for prefix, prices in table.items():
            assert prices, (provider, prefix)
            ends = [price.valid_until for price in prices]
            assert None not in ends[:-1], (provider, prefix, ends)
            dated = [end for end in ends if end is not None]
            assert dated == sorted(set(dated)), (provider, prefix, ends)
    assert set(pricing.DEFAULT_MODELS) == set(pricing.PRICING)
    for day in (TODAY, date(2027, 1, 1)):
        for provider, model in pricing.DEFAULT_MODELS.items():
            assert pricing.price_for(provider, model, on=day) is not None, (provider, model, day)


# Anthropic's published page, read on this document's date
# (https://platform.claude.com/docs/en/about-claude/pricing): "Claude Sonnet 5 | $2 / MTok ... | $10 /
# MTok", with the footnote "The $2/$10 ... pricing for Claude Sonnet 5 ... is now the standard price.
# The previously scheduled increase to $3/$15 ... will not occur"; Opus 5.5 at $4/$20; Opus 5 and Opus
# 4.8, 4.7, 4.6 and 4.5 at $5/$25; Opus 4.1 and 4 at $15/$75; Sonnet 4.6, 4.5 and 4 at $3/$15; Haiku
# 4.5 at $1/$5.
ANTHROPIC_PUBLISHED = [
    ("claude-sonnet-5", 2.0, 10.0),
    ("claude-opus-5-5", 4.0, 20.0),
    ("claude-opus-5", 5.0, 25.0),
    ("claude-opus-4-8", 5.0, 25.0),
    ("claude-opus-4-7", 5.0, 25.0),
    ("claude-opus-4-6", 5.0, 25.0),
    ("claude-opus-4-5-20251101", 5.0, 25.0),
    ("claude-opus-4-1-20250805", 15.0, 75.0),
    ("claude-opus-4-20250514", 15.0, 75.0),
    ("claude-sonnet-4-6", 3.0, 15.0),
    ("claude-sonnet-4-5-20250929", 3.0, 15.0),
    ("claude-haiku-4-5-20251001", 1.0, 5.0),
]


@pytest.mark.parametrize(("model", "price_in", "price_out"), ANTHROPIC_PUBLISHED)
def test_every_anthropic_model_is_priced_at_its_published_figure(model, price_in, price_out):
    """Decision 437. The table carried the corpus's three family prefixes, so the shipped default
    `claude-sonnet-5` inherited Sonnet 4's $3/$15 and every Opus from 4.5 on inherited Opus 4's $15/$75
    -- the confident number from a neighbouring name that `price_for`'s own docstring says it refuses,
    reached through a prefix that was true of the models the corpus priced and of none released since.
    The meter read 50% over the bill on the default model, and 3x on Opus. Each model now has the row
    the page gives it, and the default is pinned to the page's figure.
    [M5.5 review cycle 1, M55-METER-05, M55-DOC-04]"""
    price = pricing.price_for("anthropic", model, on=TODAY)
    assert (price.input, price.output) == (price_in, price_out), model
    if model == pricing.DEFAULT_MODELS["anthropic"]:
        assert pricing.usd(742, 1_600, price) == Decimal("0.017484")


def test_a_cache_write_and_a_cache_read_are_priced_at_their_published_rates():
    """OpenAI's gpt-5.6-terra row is "$2.00 | $0.20 | $2.50 | $12.00" -- input, cached input, cache
    writes, output (https://developers.openai.com/api/docs/pricing) -- and "For GPT-5.6 and later, cache
    writes cost 1.25x the standard, uncached input-token rate" while "Prompt caching is enabled by
    default" (https://developers.openai.com/api/docs/guides/prompt-caching). A stage-6 prompt is new
    for every title, so the implicit breakpoint writes all of it: 23,500 in and 3,900 out bills
    $0.105550, which the flat-rate meter read as $0.093800. Written and read tokens are inside the
    prompt count, so they are priced apart from it rather than beside it; a price with no cache rate
    charges both at its input rate, which over-reads a cached read and never under-reads a write the
    provider does not charge. The ceiling and the estimate price every input token as written,
    because that is what the default breakpoint does. [M5.5 review cycle 1, M55-DBL-02, M55-DOC-05]"""
    terra = pricing.price_for("openai", "gpt-5.6-terra", on=TODAY)
    assert (terra.input, terra.cache_read, terra.cache_write, terra.output) == (2.0, 0.2, 2.5, 12.0)
    assert pricing.usd(23_500, 3_900, terra) == Decimal("0.093800")
    assert pricing.usd(23_500, 3_900, terra, cache_written=23_500) == Decimal("0.105550")
    assert pricing.usd(23_500, 3_900, terra, cache_read=20_000) == Decimal("0.057800")
    assert pricing.ceiling(23_500, 8_000, terra) == Decimal("0.154750")
    assert pricing.estimate_title(tokens_in=23_500, prices=[terra], passes=1) == Decimal("0.105550")
    # An admin override on an OpenAI model keeps the published write multiplier, since the override
    # names an input price and OpenAI states the write as a multiple of it.
    override = pricing.effective_price("openai", "gpt-5.7", override={"price_input": 4, "price_output": 16})
    assert (override.cache_write, override.cache_read) == (5.0, None)
    flash = pricing.price_for("gemini", "gemini-3.7-flash", on=TODAY)
    assert pricing.usd(1_000, 0, flash, cache_read=400) == pricing.usd(1_000, 0, flash)


def test_an_admin_override_wins_and_a_half_override_does_not(caplog):
    """Decision 343: `price_input` and `price_output` in the provider's row, both or neither. Both, and
    they win over the table -- including over the table's None, which is what an override is for.
    One, or one that is not a price, and the table answers and the log says why."""
    row = {"model": "gemini-3.7-flash", "price_input": 1, "price_output": 2.5}
    assert pricing.effective_price("gemini", "gemini-3.7-flash", override=row, on=TODAY) == ModelPrice(
        1.0, 2.5)
    # An OpenAI override keeps the published cache-write multiplier over the input price it names.
    assert pricing.effective_price("openai", "gpt-7", override=row, on=TODAY) == ModelPrice(
        1.0, 2.5, cache_write=1.25)
    # A free local endpoint is a real price.
    free = {"price_input": 0, "price_output": 0.0}
    assert pricing.effective_price("openai", "gpt-7", override=free, on=TODAY) == ModelPrice(
        0.0, 0.0, cache_write=0.0)

    table = pricing.price_for("gemini", "gemini-3.7-flash", on=TODAY)
    caplog.set_level(logging.WARNING, logger="spielplan.llm.pricing")
    for none_set in (None, {}, {"model": "gemini-3.7-flash"}, {"price_input": None, "price_output": None}):
        assert pricing.effective_price("gemini", "gemini-3.7-flash", override=none_set, on=TODAY) == table
    assert not caplog.records, "an absent override is the ordinary case and says nothing"

    for bad in ({"price_input": 1.0}, {"price_output": 2.0}, {"price_input": 1.0, "price_output": None},
                {"price_input": -1, "price_output": 2}, {"price_input": True, "price_output": 2},
                {"price_input": "1.0", "price_output": "2.0"},
                {"price_input": float("nan"), "price_output": 2}):
        caplog.clear()
        assert pricing.effective_price("gemini", "gemini-3.7-flash", override=bad, on=TODAY) == table, bad
        assert pricing.effective_price("openai", "gpt-7", override=bad, on=TODAY) is None, bad
        messages = [record.getMessage() for record in caplog.records]
        assert messages and all("decision 343" in m and "ignored" in m for m in messages), (bad, messages)
        assert all(m.isascii() for m in messages)


def test_a_call_costs_exactly_what_the_meter_column_keeps():
    """`llm_call.usd` is numeric(12,6) and the meter is its SUM (decision 325), so the per-call figure
    is exact to six places, rounded once, ties away from zero as Postgres rounds a cast into that
    column. The corpus summed floats: ten gemini-2.5-flash runs come to 0.16799999999999998 that way,
    and ten one-micro-dollar calls to 9.999999999999999e-06 -- a meter a few ulps short of its own
    rows, which is the drift decision 325 chose a SUM over a counter to be rid of."""
    assert pricing.usd(12_000, 3_900, ModelPrice(0.75, 3.75)) == Decimal("0.023625")
    assert str(pricing.usd(12_000, 3_900, ModelPrice(0.75, 3.75))) == "0.023625"
    # Gemini's 1,600 candidate + 2,300 thought tokens are one billed figure (exit check 5's number).
    assert pricing.usd(0, 1_600 + 2_300, ModelPrice(0.75, 3.75)) == Decimal("0.014625")

    flash25 = pricing.price_for("gemini", "gemini-2.5-flash", on=TODAY)
    assert sum(pricing.usd(23_500, 3_900, flash25) for _ in range(10)) == Decimal("0.168000")
    tenth = ModelPrice(0.1, 0.3)
    assert pricing.usd(10, 0, tenth) == Decimal("0.000001")
    assert sum(pricing.usd(10, 0, tenth) for _ in range(10)) == Decimal("0.000010")
    assert pricing.usd(0, 10, tenth) == Decimal("0.000003")
    # Ties: 0.0000025 stores as 0.000003 in Postgres (measured on this project's cluster), not as
    # banker's rounding's 0.000002; below the half it stores as nothing.
    assert pricing.usd(5, 0, ModelPrice(0.5, 0.0)) == Decimal("0.000003")
    assert pricing.usd(1, 0, ModelPrice(0.4, 0.0)) == Decimal("0.000000")


# `docs/milestones/ROADMAP-M5.md`'s sync rows: (provider, model, per title per pass, two passes),
# each figure as the roadmap prints it, to three places -- except the last two, which
# ROADMAP-M5.md:762 costed before decision 437 re-read the published pages: claude-sonnet-5 at the
# $3/$15 its footnote now says "will not occur" (published $2/$10 gives 0.086), and gpt-5.6-terra
# with its default cache write at the flat input rate (published $2.50 written gives 0.106). The
# roadmap is a milestone document this lane does not edit; the figures below are the pages'.
ROADMAP_SYNC_ROWS = [
    ("gemini", "gemini-2.5-flash", "0.017", "0.034"),
    ("openai", "gpt-5-mini", "0.014", "0.027"),
    ("gemini", "gemini-3.7-flash", "0.032", "0.065"),
    ("anthropic", "claude-haiku", "0.043", "0.086"),
    ("openai", "gpt-5.6-terra", "0.106", "0.211"),
    ("anthropic", "claude-sonnet-5", "0.086", "0.172"),
]


@pytest.mark.parametrize(("provider", "model", "one_pass", "two_passes"), ROADMAP_SYNC_ROWS)
def test_the_roadmap_cost_table_reproduces_from_the_shipped_prices(provider, model, one_pass, two_passes):
    """23,500 in (`SPEC_INPUT_TOKENS`, §8 stage 6's midpoint) and 3,900 billed out
    (`MEAN_OUTPUT_TOKENS`, reasoning included) at the shipped price comes out at the roadmap's figure
    for one pass and for two."""
    assert pricing.SPEC_INPUT_TOKENS == 23_500
    assert pricing.MEAN_OUTPUT_TOKENS == 3_900
    price = pricing.price_for(provider, model, on=TODAY)
    for passes, printed in ((1, one_pass), (2, two_passes)):
        estimate = pricing.estimate_title(
            tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[price], passes=passes)
        assert estimate.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP) == Decimal(printed), passes


def test_the_estimate_multiplies_by_passes_and_by_providers():
    """Plan D4: tokens in x price in + MEAN_OUTPUT_TOKENS x price out, times passes, summed over
    providers. Exactly, in the column's six places: gemini-3.7-flash is $0.032250 a run and
    claude-sonnet-5 $0.086000 at the $2/$10 decision 437 read off Anthropic's page, so the two at
    two passes are four runs and $0.236500. And the output default is the billed figure, not the
    ~780 visible tokens the corpus's comment warns about."""
    gemini = pricing.price_for("gemini", "gemini-3.7-flash", on=TODAY)
    sonnet = pricing.price_for("anthropic", "claude-sonnet-5", on=TODAY)
    tokens = pricing.SPEC_INPUT_TOKENS

    assert pricing.estimate_title(tokens_in=tokens, prices=[gemini], passes=1) == Decimal("0.032250")
    assert pricing.estimate_title(tokens_in=tokens, prices=[sonnet], passes=1) == Decimal("0.086000")
    assert pricing.estimate_title(tokens_in=tokens, prices=[gemini, sonnet], passes=2) == Decimal(
        "0.236500")
    # "Two providers at two passes is 4x the single-run figure."
    assert pricing.estimate_title(tokens_in=tokens, prices=[gemini, gemini], passes=2) == 4 * Decimal(
        "0.032250")
    visible_only = pricing.estimate_title(tokens_in=tokens, prices=[gemini], passes=1, tokens_out=780)
    assert pricing.estimate_title(tokens_in=tokens, prices=[gemini], passes=1) > visible_only
    assert pricing.estimate_title(tokens_in=tokens, prices=[gemini], passes=1) == pricing.estimate_title(
        tokens_in=tokens, prices=[gemini], passes=1, tokens_out=1_600 + 2_300)


@pytest.mark.parametrize("passes", [0, -1])
def test_an_estimate_refuses_a_pass_count_below_one(passes):
    """A run is numbered from one (`llm_call.pass_index`); a zero or negative multiplier would make
    the gate's reservation a figure that admits calls it never priced."""
    gemini = pricing.price_for("gemini", "gemini-3.7-flash", on=TODAY)
    with pytest.raises(ValueError, match="at least one pass"):
        pricing.estimate_title(tokens_in=1000, prices=[gemini], passes=passes)


# --- the price basis a caption names (M5.7, decision 343) ----------------------------------------


def test_a_table_basis_names_the_price_its_end_and_the_price_after_it():
    """Plan §2.7's second trap, as the caption has to state it: gemini-3.7-flash's introductory price
    doubles on 2027-01-01, so an estimate costed today names the price it used, the day it stops
    being true, and what the table says it becomes -- the one sentence that keeps a figure accepted
    in December from reading as January's. On the day itself the doubled price is the price and has
    no announced end, so the caption has nothing after it to name."""
    basis = pricing.price_basis("gemini", "gemini-3.7-flash", on=TODAY)
    assert (basis.provider, basis.model, basis.source) == ("gemini", "gemini-3.7-flash", "table")
    assert (basis.price.input, basis.price.output, basis.price.valid_until) == (
        0.75, 3.75, date(2027, 1, 1))
    assert (basis.then.input, basis.then.output) == (1.5, 7.5)

    doubled = pricing.price_basis("gemini", "gemini-3.7-flash", on=date(2027, 1, 1))
    assert (doubled.price.input, doubled.price.output, doubled.price.valid_until) == (1.5, 7.5, None)
    assert doubled.then is None
    # A price with no announced end has nothing to say after it either.
    sonnet = pricing.price_basis("anthropic", "claude-sonnet-5", on=TODAY)
    assert (sonnet.source, sonnet.price.valid_until, sonnet.then) == ("table", None, None)


def test_an_override_basis_says_override_and_a_half_one_is_the_tables():
    """Decision 343's override wins when both halves are numbers, and the caption says so: the figure
    is the admin's and carries no table date, because the table's end is not the override's. Half an
    override is ignored by `effective_price`, so the basis it prices at is the table's and says so
    rather than claiming a figure the admin was still typing."""
    both = pricing.price_basis(
        "gemini", "gemini-3.7-flash", override={"price_input": 1, "price_output": 4.5}, on=TODAY)
    assert (both.source, both.price.input, both.price.output) == ("override", 1.0, 4.5)
    assert (both.price.valid_until, both.then) == (None, None)

    half = pricing.price_basis("gemini", "gemini-3.7-flash", override={"price_input": 1}, on=TODAY)
    assert (half.source, half.price.input, half.price.valid_until) == ("table", 0.75, date(2027, 1, 1))


def test_an_unpriced_model_has_no_basis_until_an_override_gives_it_one():
    """Decision 343's "unknown" at the source: a model neither the table nor an override prices has no
    basis at all, so no caller can print a price for it; the override is what gives it one."""
    assert pricing.price_basis("gemini", "gemini-9-ultra", on=TODAY) is None
    assert pricing.price_basis("openai", "gpt-5.6", on=TODAY) is None
    priced = pricing.price_basis(
        "gemini", "gemini-9-ultra", override={"price_input": 2, "price_output": 8}, on=TODAY)
    assert (priced.source, priced.model, priced.price.input) == ("override", "gemini-9-ultra", 2.0)
