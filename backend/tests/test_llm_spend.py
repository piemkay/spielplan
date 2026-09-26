"""The spend meter, the extraction plan and the cap check (§8, §9, decisions 324, 325).
`zoneinfo.ZoneInfo` is replaced because Windows has no tz database; the stand-in's fixed UTC+1 IS
Berlin for every instant here (DST ended 25 October 2026). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfoNotFoundError

import pytest

from spielplan.acquire import pipeline
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.dna import packs, verify
from spielplan.llm import client, contract, pricing, spend
from spielplan.llm.pricing import ModelPrice

# Mid-November in Berlin, inside the introductory Gemini price (it ends 2027-01-01).
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
DAY = date(2026, 11, 15)
# November 2026 in Europe/Berlin, as UTC instants: an hour before each UTC month starts.
NOVEMBER_STARTS = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
DECEMBER_STARTS = datetime(2026, 11, 30, 23, 0, tzinfo=UTC)

CET = timezone(timedelta(hours=1), "CET")

TITLE = 1
LONG_TITLE = 2
NO_PACK_TITLE = 3
KEY_GEMINI = "GEMINI-KEY-NOT-A-REAL-ONE-0001"
KEY_ANTHROPIC = "ANTHROPIC-KEY-NOT-A-REAL-ONE-0002"


def _berlin_only(name: str):
    if name == "Europe/Berlin":
        return CET
    raise ZoneInfoNotFoundError(f"No time zone found with key {name}")


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setattr(spend, "ZoneInfo", _berlin_only)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`settings()` takes no argument, so the root goes in through the environment."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


async def _vocabulary(db) -> None:
    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) "
                     "VALUES ('v1', 2, 3)")
    await db.execute("INSERT INTO dna_facet (version, facet, ord) "
                     "VALUES ('v1', 'mood', 0), ('v1', 'themes', 1)")
    await db.execute("INSERT INTO dna_term (version, term, facet, gloss) VALUES "
                     "('v1', 'mood.bleak', 'mood', 'without hope'), "
                     "('v1', 'mood.warm', 'mood', 'affectionate'), "
                     "('v1', 'themes.robots', 'themes', 'machines as characters')")


async def _title(db, title_id: int, name: str) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, 2021, true)",
        title_id, name,
    )


async def _packed(db, title_id: int, name: str, words: tuple[str, ...]) -> int:
    await _title(db, title_id, name)
    await db.executemany(
        "INSERT INTO review_store.review (title_id, source, body, is_critic) VALUES ($1, $2, $3, $4)",
        [(title_id, "trakt", " ".join([word] * 60), False) for word in words],
    )
    text, info = await packs.build_pack(db, title_id)
    return await packs.store_pack(db, title_id, "v1", text, info)


@pytest.fixture
async def pack_doc(db, data_dir) -> int:
    await _vocabulary(db)
    doc = await _packed(db, TITLE, "Grey Harbour", ("harbour",))
    await _title(db, NO_PACK_TITLE, "Unpacked")
    return doc


async def _bill(db, doc: int, usd: Decimal, at: datetime = NOW, *, attempt: int = 1,
                tokens_out_billed: int = 0, ok: bool = True) -> int:
    """`llm_call.at` defaults to the server's clock, so the instant is written afterwards."""
    row = await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=TITLE, pass_index=1,
        attempt=attempt, tokens_in=0, tokens_out_billed=tokens_out_billed, usd=usd, ok=ok,
        error=None if ok else "unknown_term: 'themes.mecha' is not in vocabulary v1",
        pack_document_id=doc, response_document_id=None,
    )
    await db.execute("UPDATE llm_call SET at = $1 WHERE id = $2", at, row)
    return row


async def _llm(db, **fields) -> None:
    await registry.save_connector(db, "llm", **fields)


async def _real_tokens_in(db, title_id: int) -> int:
    voc = await contract.load_prompt_vocabulary(db, "v1")
    pack = await verify.read_pack(db, title_id, "v1")
    return (client.estimate_tokens(contract.system_prompt(voc))
            + client.estimate_tokens(contract.user_prompt(pack)))


async def test_the_meter_sums_the_households_calendar_month_and_not_the_utc_one(db, pack_doc):
    """Each row's amount is a different power of ten, so the sum says exactly which rows were counted."""
    assert spend.period(NOW) == (NOVEMBER_STARTS, DECEMBER_STARTS)

    second = timedelta(seconds=1)
    await _bill(db, pack_doc, Decimal("1.00"), NOVEMBER_STARTS - second)     # October, in Berlin
    await _bill(db, pack_doc, Decimal("10.00"), NOVEMBER_STARTS)             # November's first instant
    await _bill(db, pack_doc, Decimal("100.00"), DECEMBER_STARTS - second)   # November's last second
    await _bill(db, pack_doc, Decimal("1000.00"), DECEMBER_STARTS)           # December's first instant

    assert await spend.spent(db, now=NOW) == Decimal("110.00")
    assert await spend.spent(db, now=NOVEMBER_STARTS) == Decimal("110.00"), (
        "the month's own first instant belongs to it"
    )
    assert await spend.spent(db, now=DECEMBER_STARTS - second) == Decimal("110.00")
    assert await spend.spent(db, now=DECEMBER_STARTS) == Decimal("1000.00")


def test_an_unresolvable_tz_bounds_the_month_on_the_process_clock_and_says_so(monkeypatch, caplog):
    """Logged on every read, because this module has no boot line to say it once."""
    monkeypatch.setenv("TZ", "Europe/Berln")
    settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="spielplan.llm.spend"):
        start, end = spend.period(NOW)

    assert any("Europe/Berln" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert start.tzinfo is UTC and end.tzinfo is UTC
    assert start <= NOW < end
    local_start = start.astimezone()
    assert (local_start.day, local_start.hour, local_start.minute) == (1, 0, 0)


async def test_a_gemini_call_is_charged_for_the_thinking_tokens_it_was_billed_for(db, pack_doc):
    """§9: counting the visible JSON "understates cost ~5x"."""
    price = pricing.price_for("gemini", "gemini-3.7-flash", on=DAY)
    assert (price.input, price.output) == (0.75, 3.75)
    billed = 1_600 + 2_300
    charge = pricing.usd(20_000, billed, price)

    row_id = await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=TITLE, pass_index=1, attempt=1,
        tokens_in=20_000, tokens_out_billed=billed, usd=charge, ok=True, error=None,
        pack_document_id=pack_doc, response_document_id=None,
    )
    await db.execute("UPDATE llm_call SET at = $1 WHERE id = $2", NOW, row_id)

    row = await db.fetchrow("SELECT * FROM llm_call WHERE id = $1", row_id)
    assert (row["tokens_out_billed"], row["usd"], row["task"]) == (3_900, Decimal("0.029625"), "extraction")
    assert await spend.spent(db, now=NOW) == Decimal("0.029625")
    assert charge - pricing.usd(20_000, 1_600, price) == Decimal("0.008625")


async def test_both_attempts_of_a_run_are_metered(db, pack_doc):
    await _bill(db, pack_doc, Decimal("0.020000"), attempt=1, ok=False)
    await _bill(db, pack_doc, Decimal("0.021000"), attempt=2)

    assert await spend.spent(db, now=NOW) == Decimal("0.041000")
    assert await db.fetchval("SELECT array_agg(attempt ORDER BY attempt) FROM llm_call") == [1, 2]


async def test_the_meter_reports_the_month_and_what_is_left_of_the_cap(db, pack_doc):
    """"$4.12 of $25.00" is §6.6's example, not a default;
    an overshoot never shows as a negative remainder."""
    await _bill(db, pack_doc, Decimal("4.12"))
    uncapped = await spend.meter(db, now=NOW)
    assert (uncapped["spent_usd"], uncapped["cap_usd"], uncapped["remaining_usd"]) == (
        Decimal("4.12"), None, None
    )

    await _llm(db, cap_usd=25)
    reading = await spend.meter(db, now=NOW)
    assert reading == {
        "spent_usd": Decimal("4.12"),
        "unsettled_usd": Decimal("0"),
        "cap_usd": Decimal("25"),
        "remaining_usd": Decimal("20.88"),
        "period_start": NOVEMBER_STARTS,
        "period_end": DECEMBER_STARTS,
        "tz": "Europe/Berlin",
    }

    await _bill(db, pack_doc, Decimal("21.00"))
    over = await spend.meter(db, now=NOW)
    assert (over["spent_usd"], over["remaining_usd"]) == (Decimal("25.12"), Decimal("0"))


@pytest.mark.parametrize("spoiled", [f"{KEY_GEMINI[:6]}\n{KEY_GEMINI[6:]}", f"{KEY_GEMINI}\u200b"])
async def test_a_key_no_header_can_carry_is_refused_by_name_and_never_quoted(db, secrets_key, spoiled):
    """h11's bytes repr put the whole key into `llm_call.error`;
    the plan parks naming the fault, not the key."""
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=spoiled)
    refusal = await spend.extraction_plan(db, now=NOW)
    assert refusal.kind == spend.PLAN
    assert "the gemini key holds a character no HTTP header can carry" in refusal.reason
    assert KEY_GEMINI[:6] not in refusal.reason and KEY_GEMINI[6:] not in refusal.reason

    await registry.save_connector(db, "gemini", api_key=f"  {KEY_GEMINI}\n")
    assert isinstance(await spend.extraction_plan(db, now=NOW), spend.Plan), "space around a key is trimmed"


async def test_a_call_left_at_its_ceiling_is_named_apart_in_the_meter_and_the_over_cap_reason(
    db, pack_doc, secrets_key
):
    """Decision 436: a lost call stays at its ceiling; the meter says how much of the spend is ceilings."""
    await _bill(db, pack_doc, Decimal("0.60"))
    lost = await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=TITLE, pass_index=1, attempt=1,
        tokens_in=25_000, tokens_out_billed=8_000, usd=Decimal("0.40"), ok=False,
        error=f"{spend.UNSETTLED_PREFIX}: ReadTimeout: the reply was lost",
        pack_document_id=pack_doc, response_document_id=None,
    )
    await db.execute("UPDATE llm_call SET at = $1 WHERE id = $2", NOW, lost)
    await _llm(db, cap_usd=1, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)

    reading = await spend.meter(db, now=NOW)
    assert (reading["spent_usd"], reading["unsettled_usd"]) == (Decimal("1.00"), Decimal("0.40"))
    refusal = await spend.cap_check(db, title_id=TITLE, now=NOW)
    assert refusal.kind == spend.OVER_CAP
    assert "$0.40 of it is calls whose answer never arrived" in refusal.reason, refusal.reason
    assert refusal.detail["unsettled_usd"] == "0.400000"


async def test_an_uncapped_install_is_refused_with_decision_348s_sentence_before_anything_else(db):
    assert spend.NO_CAP_REASON == pipeline.NO_SPEND_CAP

    refusal = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert (refusal.kind, refusal.reason) == (spend.NO_CAP, pipeline.NO_SPEND_CAP)
    assert await spend.cap(db) is None
    assert await spend.retry_refusal(db, title_id=NO_PACK_TITLE, now=NOW) == pipeline.NO_SPEND_CAP


@pytest.mark.parametrize("stored", ["25", -1, True, [25], {"usd": 25}])
async def test_a_cap_that_is_not_a_number_of_at_least_zero_is_no_cap_and_says_so(db, caplog, stored):
    """Never zero (every title parks) and never infinity (no limit): unset, logged, and said."""
    await _llm(db, cap_usd=stored)
    with caplog.at_level(logging.WARNING, logger="spielplan.llm.spend"):
        assert await spend.cap(db) is None
    assert any("cap_usd" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert (await spend.cap_check(db, title_id=TITLE, now=NOW)).kind == spend.NO_CAP


async def test_a_cap_of_zero_is_a_cap_and_a_fraction_is_kept_exactly(db):
    await _llm(db, cap_usd=0)
    assert await spend.cap(db) == Decimal("0")
    await _llm(db, cap_usd=25.5)
    assert await spend.cap(db) == Decimal("25.5")


async def test_a_capped_install_with_no_extraction_provider_is_refused_naming_the_assignment(db):
    await _llm(db, cap_usd=25)

    refusal = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert refusal.kind == spend.PLAN
    assert "no extraction provider is assigned" in refusal.reason
    assert refusal.detail == {"setting": "extraction_provider"}


@pytest.mark.parametrize(
    ("settings_row", "named"),
    [
        ({"extraction_provider": "mistral"}, "'mistral'"),
        ({"parallel": True}, "parallel_providers"),
        ({"parallel": True, "parallel_providers": ["gemini", "llama"]}, "'llama'"),
        ({"parallel": "yes", "extraction_provider": "gemini"}, "parallel is 'yes'"),
        ({"extraction_provider": "gemini", "passes": 0}, "passes is 0"),
        ({"extraction_provider": "gemini", "passes": 1.5}, "passes is 1.5"),
    ],
)
async def test_a_setting_the_plan_cannot_read_is_refused_by_name(db, settings_row, named):
    """The `llm` row may be written by hand, so a setting is
    read as written or refused by name, never guessed."""
    await _llm(db, cap_usd=25, **settings_row)

    refusal = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert refusal.kind == spend.PLAN
    assert named in refusal.reason


async def test_a_provider_with_no_key_or_an_unreadable_one_is_refused_by_name(
    db, secrets_key, monkeypatch
):
    """A key this SECRETS_KEY cannot open is not "no key": telling the admin to type one would mislead."""
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    keyless = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert (keyless.kind, keyless.detail) == (spend.PLAN, {"provider": "gemini"})
    assert "no API key is configured for gemini" in keyless.reason

    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    unreadable = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert unreadable.kind == spend.PLAN
    assert registry.SECRETS_UNREADABLE_REASON in unreadable.reason
    assert "no API key" not in unreadable.reason


async def test_an_unpriced_model_is_refused_naming_the_provider_and_the_model(db, secrets_key):
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI, model="gemini-9-ultra")

    refusal = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert refusal.kind == spend.PLAN
    assert "gemini model 'gemini-9-ultra'" in refusal.reason
    assert refusal.detail == {"provider": "gemini", "model": "gemini-9-ultra"}

    await registry.save_connector(db, "gemini", price_input=0.5, price_output=2.5)
    plan = await spend.extraction_plan(db, now=NOW)
    assert plan.providers[0].price == ModelPrice(0.5, 2.5)
    assert await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW) is None


async def test_the_defaults_are_one_provider_and_one_pass(db, secrets_key):
    """With `parallel` off `parallel_providers` is not read: the keyless anthropic entry costs nothing."""
    await _llm(db, cap_usd=25, extraction_provider="gemini", parallel_providers=["anthropic"])
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)

    plan = await spend.extraction_plan(db, now=NOW)
    assert [(p.provider, p.model, p.key) for p in plan.providers] == [
        ("gemini", pricing.DEFAULT_MODELS["gemini"], KEY_GEMINI)
    ]
    assert (plan.passes, plan.runs) == (1, 1)
    assert plan.providers[0].price == pricing.price_for("gemini", "gemini-3.7-flash", on=DAY)


async def test_a_dated_price_is_asked_for_on_the_households_day(db, secrets_key):
    """Half past midnight on New Year's Day in Berlin is still 31 December in UTC."""
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)

    new_year_in_berlin = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    plan = await spend.extraction_plan(db, now=new_year_in_berlin)
    assert plan.providers[0].price == ModelPrice(1.50, 7.50)
    earlier = await spend.extraction_plan(db, now=new_year_in_berlin - timedelta(hours=1))
    assert (earlier.providers[0].price.input, earlier.providers[0].price.output) == (0.75, 3.75)


async def test_a_plan_never_prints_the_keys_it_carries(db, secrets_key):
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)

    plan = await spend.extraction_plan(db, now=NOW)
    assert plan.providers[0].key == KEY_GEMINI
    assert KEY_GEMINI not in repr(plan) and KEY_GEMINI not in str(plan)


async def test_at_the_cap_the_title_is_refused_without_estimating_anything(db, pack_doc, secrets_key,
                                                                          monkeypatch):
    async def must_not_estimate(*_args, **_kwargs):
        raise AssertionError("a month at its cap has no room for any reservation to be priced")

    monkeypatch.setattr(spend, "reservation", must_not_estimate)
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    await _bill(db, pack_doc, Decimal("25.000000"))

    refusal = await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW)
    assert refusal.kind == spend.OVER_CAP
    assert refusal.reason.startswith("over spend cap")
    assert "$25.00 of the $25.00 monthly cap is spent for 2026-11 (Europe/Berlin)" in refusal.reason
    assert "2026-12-01" in refusal.reason and "cap is raised in Admin" in refusal.reason
    refusal.reason.encode("ascii")
    assert refusal.detail["reserved_usd"] is None
    assert await spend.retry_refusal(db, title_id=NO_PACK_TITLE, now=NOW) == refusal.reason


async def test_the_retry_is_budgeted_inside_the_cap(db, pack_doc, secrets_key):
    """The month has room for one attempt and not two, so the title is refused before attempt 1."""
    await _llm(db, cap_usd=100, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    plan = await spend.extraction_plan(db, now=NOW)
    # Computed here, not read from the module, so a one-attempt reservation is caught.
    one_attempt = pricing.usd(
        await _real_tokens_in(db, TITLE), pricing.MEAN_OUTPUT_TOKENS, plan.providers[0].price
    )
    already = Decimal("0.010000")
    await _bill(db, pack_doc, already)

    room_for_one = (already + one_attempt * Decimal("1.5")).quantize(Decimal("0.000001"))
    await _llm(db, cap_usd=float(room_for_one))
    refusal = await spend.cap_check(db, title_id=TITLE, now=NOW)
    assert refusal is not None, "the month had room for attempt 1 only, and the title was let through"
    assert refusal.kind == spend.OVER_CAP
    need = await spend.reservation(db, plan, title_id=TITLE)
    assert need == 2 * one_attempt
    assert refusal.reason.startswith("over spend cap: $0.01 of the")
    assert "2 attempts x 1 pass(es) x 1 provider(s)" in refusal.reason
    refusal.reason.encode("ascii")
    assert refusal.detail["reserved_usd"] == str(need)
    assert refusal.detail["spent_usd"] == str(already)
    assert await spend.retry_refusal(db, title_id=TITLE, now=NOW) == refusal.reason

    await _llm(db, cap_usd=float(already + need))
    assert await spend.cap_check(db, title_id=TITLE, now=NOW) is None
    await _llm(db, cap_usd=float(already + need - Decimal("0.000001")))
    assert (await spend.cap_check(db, title_id=TITLE, now=NOW)).kind == spend.OVER_CAP


async def test_a_gate_asked_just_before_a_dated_price_turns_over_reserves_at_the_dearer_price(
    db, pack_doc, secrets_key
):
    """The gate reserves at the dearer of its own day's price and the next day's."""
    await _llm(db, cap_usd=100, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    last_second = datetime(2026, 12, 31, 22, 59, 59, tzinfo=UTC)
    tokens_in = await _real_tokens_in(db, TITLE)
    old = 2 * pricing.usd(tokens_in, pricing.MEAN_OUTPUT_TOKENS, ModelPrice(0.75, 3.75))
    new = 2 * pricing.usd(tokens_in, pricing.MEAN_OUTPUT_TOKENS, ModelPrice(1.50, 7.50))

    await _llm(db, cap_usd=float(old))
    refusal = await spend.cap_check(db, title_id=TITLE, now=last_second)
    assert refusal is not None, "the reservation was priced at a figure that ends in one second"
    assert (refusal.kind, refusal.detail["reserved_usd"]) == (spend.OVER_CAP, str(new))
    await _llm(db, cap_usd=float(new))
    assert await spend.cap_check(db, title_id=TITLE, now=last_second) is None
    # Mid-month nothing turns over, and the reservation is the day's own.
    await _llm(db, cap_usd=float(old))
    assert await spend.cap_check(db, title_id=TITLE, now=NOW) is None


async def test_comfortably_under_the_cap_the_title_may_run(db, pack_doc, secrets_key):
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    await _bill(db, pack_doc, Decimal("4.12"))

    assert await spend.cap_check(db, title_id=TITLE, now=NOW) is None
    assert await spend.retry_refusal(db, title_id=TITLE, now=NOW) is None


async def test_a_title_with_no_stored_pack_is_left_to_stage_six_under_the_cap(db, pack_doc, secrets_key):
    """Decision 432: stage 6 parks a packless title itself; the cap invents no figure for it."""
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)

    plan = await spend.extraction_plan(db, now=NOW)
    assert await spend.reservation(db, plan, title_id=NO_PACK_TITLE) is None
    assert await spend.cap_check(db, title_id=NO_PACK_TITLE, now=NOW) is None


async def test_the_reservation_prices_the_real_prompt_at_two_attempts(db, pack_doc, secrets_key):
    """The prompt stage 6 would send for THIS title's pack, not §8's 23,500-token midpoint."""
    await _packed(db, LONG_TITLE, "Long Harbour", ("harbour", "lighthouse", "fog", "gulls"))
    await _llm(db, cap_usd=25, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    plan = await spend.extraction_plan(db, now=NOW)
    price = plan.providers[0].price

    tokens_in = await _real_tokens_in(db, TITLE)
    assert tokens_in != pricing.SPEC_INPUT_TOKENS
    per_call = pricing.usd(tokens_in, pricing.MEAN_OUTPUT_TOKENS, price)
    assert await spend.reservation(db, plan, title_id=TITLE) == 2 * per_call

    longer = await spend.reservation(db, plan, title_id=LONG_TITLE)
    assert longer == 2 * pricing.usd(
        await _real_tokens_in(db, LONG_TITLE), pricing.MEAN_OUTPUT_TOKENS, price
    )
    assert longer > 2 * per_call


async def test_parallel_mode_with_two_providers_at_two_passes_reserves_eight_calls(
    db, pack_doc, secrets_key
):
    """Decision 337: a run is a provider at a pass, and each run reserves two attempts."""
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    await registry.save_connector(db, "anthropic", api_key=KEY_ANTHROPIC)
    await _llm(db, cap_usd=25, parallel=True, parallel_providers=["gemini", "anthropic"], passes=2)

    plan = await spend.extraction_plan(db, now=NOW)
    assert [p.provider for p in plan.providers] == ["gemini", "anthropic"]
    assert (plan.passes, plan.runs) == (2, 4)

    tokens_in = await _real_tokens_in(db, TITLE)
    gemini, anthropic = (pricing.usd(tokens_in, pricing.MEAN_OUTPUT_TOKENS, p.price)
                         for p in plan.providers)
    assert gemini != anthropic
    assert await spend.reservation(db, plan, title_id=TITLE) == 2 * 2 * (gemini + anthropic)
