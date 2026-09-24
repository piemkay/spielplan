"""The flywheel batch's arithmetic and its refusals, as pure functions. Spec v2.1 §8.4, §6.6 Data;
decisions 325, 343 and 441.

Plan §6 asks for "the batch-total arithmetic as a pure function of rows x passes x providers", and
plan §7 checks 3 and 4 are its two observable facts: five rows at one pass total five times the
per-title estimate, and two passes double it. Decision 441 adds the third line - the reservation is
both attempts, because decision 325 budgets attempt 2 inside the cap - and the four sentences a
Launch is disabled under, in the gate's order.

THE PRICES ARE TYPED HERE, NOT READ FROM THE TABLE. `pricing.PRICING`'s figures carry dates (the
Gemini row doubles on 2027-01-01, decision 343), so a test that read them would change its answer
on a calendar day; these are `ModelPrice`s built by hand, and the expected per-title figure is
worked out in this file from them rather than asked of the function under test.

No database, no clock: `batch.totals` and `batch.assess` are the pure halves of the quote, and
`ops/devstub.py` quotes its fixture through the same two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from spielplan.flywheel import batch
from spielplan.llm import pricing, spend
from spielplan.llm.pricing import ModelPrice

# gemini-3.7-flash's introductory figures and claude-sonnet-5's, typed rather than looked up.
GEMINI = ModelPrice(0.75, 3.75)
CLAUDE = ModelPrice(2.0, 10.0)

# One pass of one title, by hand: 23,500 tokens in and 3,900 billed out (§8 stage 6's midpoint and
# `pricing.MEAN_OUTPUT_TOKENS`), per 1M tokens. 23,500 x 0.75 + 3,900 x 3.75 = 17,625 + 14,625.
GEMINI_PASS = Decimal("0.032250")
# 23,500 x 2 + 3,900 x 10 = 47,000 + 39,000.
CLAUDE_PASS = Decimal("0.086000")

# Mid-month, so the month `assess` names is September in whichever zone `TZ` resolves to.
START = datetime(2026, 9, 15, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def _meter(cap: str | None, spent: str = "0", unsettled: str = "0") -> dict:
    """`spend.meter`'s reading, typed: the shape `assess` is handed by `quote` and by the harness."""
    limit = None if cap is None else Decimal(cap)
    used = Decimal(spent)
    return {
        "spent_usd": used,
        "unsettled_usd": Decimal(unsettled),
        "cap_usd": limit,
        "remaining_usd": None if limit is None else max(limit - used, Decimal("0")),
        "period_start": START,
        "period_end": END,
        "tz": "UTC",
    }


def test_the_hand_worked_figure_is_the_one_the_estimate_prices():
    """The premise every test below stands on, checked once: the assumed input and output are the
    ones the arithmetic above uses, so a change to either is caught here and not as a puzzling
    failure four tests down."""
    assert (pricing.SPEC_INPUT_TOKENS, pricing.MEAN_OUTPUT_TOKENS) == (23_500, 3_900)
    assert batch.totals([GEMINI], passes=1, titles=1) == (
        GEMINI_PASS, GEMINI_PASS, GEMINI_PASS * spend.ATTEMPTS
    )


def test_five_titles_at_one_pass_total_exactly_five_per_title_estimates():
    """Plan §7 check 3: "select 5 rows, 1 pass | total = 5 x per-title estimate"."""
    per_title, total, reserved = batch.totals([GEMINI], passes=1, titles=5)

    assert per_title == GEMINI_PASS
    assert total == 5 * GEMINI_PASS == Decimal("0.161250")
    assert reserved == total * spend.ATTEMPTS


def test_a_second_pass_doubles_every_figure():
    """Plan §7 check 4: "change to 2 passes | total doubles" - and the per-title figure and the
    reservation with it, exactly, because each run is priced whole and multiplied (decision 324)."""
    one = batch.totals([GEMINI], passes=1, titles=5)
    two = batch.totals([GEMINI], passes=2, titles=5)

    assert two == tuple(2 * figure for figure in one)


def test_two_providers_sum_their_own_estimates():
    """Each provider runs every pass at its own price, so two providers are the sum of the two, not
    twice either one - "rows x passes x providers" with the providers priced apart."""
    per_title, total, reserved = batch.totals([GEMINI, CLAUDE], passes=2, titles=3)

    assert per_title == 2 * (GEMINI_PASS + CLAUDE_PASS)
    assert total == 3 * per_title
    assert reserved == 2 * total


def test_one_unknown_price_makes_the_whole_batch_unknown():
    """Decision 343: a total that quietly left out the provider nobody could price is the confident
    wrong number `pricing.price_for` refuses, only summed. So one None is no figure at all."""
    assert batch.totals([GEMINI, None], passes=1, titles=5) is None
    assert batch.totals([None], passes=1, titles=0) is None


def test_the_reservation_is_both_attempts_and_every_figure_is_exact():
    """Decision 441: reserved = total x `spend.ATTEMPTS`, which is decision 325's two attempts. All
    three are `Decimal`, the meter's own type, so a comparison against the cap is exact and not a
    float's nearest neighbour of it."""
    figures = batch.totals([GEMINI, CLAUDE], passes=1, titles=7)

    assert spend.ATTEMPTS == 2
    assert figures[2] == figures[1] * spend.ATTEMPTS
    assert all(type(figure) is Decimal for figure in figures)


def test_zero_titles_price_to_zero_and_a_negative_count_is_refused():
    assert batch.totals([GEMINI], passes=1, titles=0)[1:] == (Decimal("0"), Decimal("0"))
    with pytest.raises(ValueError):
        batch.assess(titles=-1, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                     meter=_meter("5"))


# --- the four refusals (decision 441) ------------------------------------------------------------


def test_a_reservation_over_the_room_left_is_refused_naming_its_arithmetic_the_cap_and_the_month():
    """Plan §7 check 5: "cap set below the total | Launch disabled; the reason names the cap". The
    sentence opens on `spend.OVER_CAP_PREFIX` like every over-cap reason in the app, and carries the
    reservation, how it was reached, what is left, the cap and the month it is left in."""
    quoted = batch.assess(titles=5, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                          meter=_meter("1.00", spent="0.75"))

    assert quoted["launchable"] is False
    reason = quoted["reason"]
    assert reason.startswith(f"{spend.OVER_CAP_PREFIX}: this batch reserves up to $0.3225 "), reason
    assert "(5 title(s) x $0.03225 a title x 2 attempts, at 1 pass(es) x 1 provider(s))" in reason
    assert "only $0.25 of the $1.00 monthly cap is left for 2026-09" in reason, reason
    reason.encode("ascii")


def test_a_reservation_exactly_equal_to_the_room_left_is_launchable():
    """`spend.cap_check`'s reading: a month that ends on its cap has not passed it."""
    reserved = GEMINI_PASS * 5 * spend.ATTEMPTS
    at = batch.assess(titles=5, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                      meter=_meter(str(reserved)))
    over = batch.assess(titles=5, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                        meter=_meter(str(reserved - Decimal("0.000001"))))

    assert (at["launchable"], at["reason"], at["reserved_usd"]) == (True, None, reserved)
    assert over["launchable"] is False and over["reason"].startswith(spend.OVER_CAP_PREFIX)


def test_the_unsettled_part_of_the_month_is_named_when_it_is_what_took_the_room():
    """Decision 436: ceilings standing for calls nobody heard back from are inside the spend, and a
    refusal that did not say so would send the admin looking for answers that never came."""
    quoted = batch.assess(titles=5, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                          meter=_meter("1.00", spent="0.90", unsettled="0.40"))

    assert "$0.40 of the month's spend is calls whose answer never arrived" in quoted["reason"]


def test_the_refusals_come_in_the_gates_order_and_each_is_its_own_sentence():
    """No cap before anything (decision 348's sentence, `spend.cap_check`'s step 1), then the plan's
    own refusal (step 2), then a price nobody knows, then a selection of nothing - so a state the
    gate would park under one sentence is not refused here under another."""
    plan_refusal = "no API key is configured for anthropic, which extraction is assigned to."

    def reason(**given):
        args = {"titles": 5, "providers": ["gemini"], "passes": 1, "prices": [GEMINI],
                "refused": None, "meter": _meter("100")} | given
        return batch.assess(**args)["reason"]

    assert reason(meter=_meter(None), prices=None, refused=plan_refusal, titles=0) == (
        spend.NO_CAP_REASON
    )
    assert reason(prices=None, refused=plan_refusal, titles=0) == plan_refusal
    assert reason(prices=[GEMINI, None], titles=0) == batch.UNKNOWN_PRICE
    assert reason(titles=0) == batch.NOTHING_SELECTED
    assert reason() is None
    for sentence in (batch.UNKNOWN_PRICE, batch.NOTHING_SELECTED):
        sentence.encode("ascii")


def test_an_unknown_figure_is_none_and_never_zero():
    """What the wire spells as null: a plan that could not price the batch leaves every figure
    None rather than a zero a surface would render as free."""
    quoted = batch.assess(titles=5, providers=["gemini"], passes=1, prices=None,
                          refused="a refusal", meter=_meter("100"))

    assert (quoted["per_title_usd"], quoted["total_usd"], quoted["reserved_usd"]) == (None, None, None)
    assert quoted["cap_usd"] == Decimal("100") and quoted["remaining_usd"] == Decimal("100")
