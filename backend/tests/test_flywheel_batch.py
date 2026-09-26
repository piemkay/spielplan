"""The flywheel batch's arithmetic and refusals, as pure functions (§8.4, decision 441).

Prices are typed here, not read from `pricing.PRICING`: its figures change on calendar dates."""

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

# One pass of one title: 23,500 tokens in and 3,900 out, per 1M: 17,625 + 14,625.
GEMINI_PASS = Decimal("0.032250")
# 23,500 x 2 + 3,900 x 10 = 47,000 + 39,000.
CLAUDE_PASS = Decimal("0.086000")

# Mid-month, so the month `assess` names is September in whichever zone `TZ` resolves to.
START = datetime(2026, 9, 15, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def _meter(cap: str | None, spent: str = "0", unsettled: str = "0") -> dict:
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
    """The premise every test below stands on: the assumed input and output the arithmetic above uses."""
    assert (pricing.SPEC_INPUT_TOKENS, pricing.MEAN_OUTPUT_TOKENS) == (23_500, 3_900)
    assert batch.totals([GEMINI], passes=1, titles=1) == (
        GEMINI_PASS, GEMINI_PASS, GEMINI_PASS * spend.ATTEMPTS
    )


def test_five_titles_at_one_pass_total_exactly_five_per_title_estimates():
    per_title, total, reserved = batch.totals([GEMINI], passes=1, titles=5)

    assert per_title == GEMINI_PASS
    assert total == 5 * GEMINI_PASS == Decimal("0.161250")
    assert reserved == total * spend.ATTEMPTS


def test_a_second_pass_doubles_every_figure():
    """Each run is priced whole and multiplied (decision 324), so the doubling is exact."""
    one = batch.totals([GEMINI], passes=1, titles=5)
    two = batch.totals([GEMINI], passes=2, titles=5)

    assert two == tuple(2 * figure for figure in one)


def test_two_providers_sum_their_own_estimates():
    """Each provider runs every pass at its own price, so two providers sum, not double one."""
    per_title, total, reserved = batch.totals([GEMINI, CLAUDE], passes=2, titles=3)

    assert per_title == 2 * (GEMINI_PASS + CLAUDE_PASS)
    assert total == 3 * per_title
    assert reserved == 2 * total


def test_one_unknown_price_makes_the_whole_batch_unknown():
    """Decision 343: a total that silently left out an unpriceable provider is a confident wrong number."""
    assert batch.totals([GEMINI, None], passes=1, titles=5) is None
    assert batch.totals([None], passes=1, titles=0) is None


def test_the_reservation_is_both_attempts_and_every_figure_is_exact():
    """`Decimal`, the meter's own type, so the comparison against the cap is exact."""
    figures = batch.totals([GEMINI, CLAUDE], passes=1, titles=7)

    assert spend.ATTEMPTS == 2
    assert figures[2] == figures[1] * spend.ATTEMPTS
    assert all(type(figure) is Decimal for figure in figures)


def test_zero_titles_price_to_zero_and_a_negative_count_is_refused():
    assert batch.totals([GEMINI], passes=1, titles=0)[1:] == (Decimal("0"), Decimal("0"))
    with pytest.raises(ValueError):
        batch.assess(titles=-1, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                     meter=_meter("5"))


def test_a_reservation_over_the_room_left_is_refused_naming_its_arithmetic_the_cap_and_the_month():
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
    """Decision 436: unsettled ceilings are inside the spend, and the refusal must say so."""
    quoted = batch.assess(titles=5, providers=["gemini"], passes=1, prices=[GEMINI], refused=None,
                          meter=_meter("1.00", spent="0.90", unsettled="0.40"))

    assert "$0.40 of the month's spend is calls whose answer never arrived" in quoted["reason"]


def test_the_refusals_come_in_the_gates_order_and_each_is_its_own_sentence():
    """The gate's order (decision 348), so no state is
    refused here under a sentence the gate would not use."""
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
    """A zero would render as free."""
    quoted = batch.assess(titles=5, providers=["gemini"], passes=1, prices=None,
                          refused="a refusal", meter=_meter("100"))

    assert (quoted["per_title_usd"], quoted["total_usd"], quoted["reserved_usd"]) == (None, None, None)
    assert quoted["cap_usd"] == Decimal("100") and quoted["remaining_usd"] == Decimal("100")
