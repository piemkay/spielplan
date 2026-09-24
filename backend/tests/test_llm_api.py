"""§6.6's LLM settings read and the one connector test dispatch, over HTTP. Spec v2.1 §6.6, §9;
decisions 324, 325, 338, 343, 433.

Decision 433 gives M5.5 two routes and no write: `GET /api/admin/llm`, the one read M5.7's
provider cards and spend guard render, and `POST /api/admin/connectors/{name}/test`, plan A4's one
dispatch table over `ConnectorSpec.test`. Everything the read reports is a domain answer
(`llm/spend`, `llm/pricing`, `connectors/registry`), so what this file asserts is what only the
route can get wrong:

* **NO SECRET LEAVES.** Each provider's stored key is searched for in the raw response text, not
  in a parsed field a leak could sit beside -- §14.3's argument for the Jellyfin key, applied to
  three keys that each bill the household.
* **THE METER IS THE BILL, SPELLED EXACTLY.** A Gemini call metered at its 1,600 candidate plus
  2,300 thought tokens is what the read's spend says, to the digit, and it says it as a string
  because a JSON number is a binary float to the client that parses it (decision 325).
* **AN UNPRICED MODEL READS "unknown", NEVER A FIGURE** -- its card and the estimate over it
  (decision 343, exit check 14's spelling on the wire).
* **THE CARD AND THE GATE AGREE.** `configured` is the read's own composition, so it is held to
  what `spend.extraction_plan` decides for the same row rather than trusted to stay in step.
* **THE TEST BUTTON REACHES THE PROVIDER THE WAY A PAID CALL DOES** -- through a real Fetcher over
  `ops/fake_llm.py`, with the double's own log showing the key in the documented header and a url
  that never carried it, and httpx's INFO line clean of it.
* **JELLYFIN'S BUTTON IS STILL `api/admin.py`'s**, because that route stores §7.1's verdict as it
  tests; the generic router is mounted after it, and a mount order that let the dispatch answer
  first would 404 the Jellyfin card's own button.

Both routes take `AdminUser`, so `test_api_gating.py`'s dependency sweeps already drive them as a
stranger and as a member; the paths are still spelled here as literals, because
`test_route_inventory.py` asks every route for a test that names it.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import fetch, rawstore
from spielplan.api import llm as llm_api
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.core.config import Settings, settings
from spielplan.llm import anthropic, client, gemini, openai, pricing, spend

REPO = Path(__file__).resolve().parents[2]
DOUBLE = REPO / "ops" / "fake_llm.py"

ADMIN_PASSWORD = "an-admin-password"
OTHER_SECRETS_KEY = "a-different-secrets-key-not-a-real-one"

ADAPTERS = {"anthropic": anthropic, "openai": openai, "gemini": gemini}
# The header each provider documents for its key, as the double records header NAMES.
DOCUMENTED_HEADER = {"anthropic": "x-api-key", "openai": "authorization", "gemini": "x-goog-api-key"}


# --- the install ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_operator_provider_keys(monkeypatch):
    """The three provider seeds taken out of the environment before the app boots.

    The `app` fixture's lifespan runs `registry.seed_from_env`, and conftest's neutraliser read the
    four source prefixes only when this was written -- so an operator with `GEMINI_API_KEY` exported
    would have had it sealed into this file's database and every "unconfigured" assertion below
    would have been about their shell. conftest now lists the three provider prefixes too; this
    stays, as the file's own guarantee rather than a borrowed one. Derived from `Settings` and
    asserted to exist, so a renamed field fails here rather than leaving a real key un-neutralised.
    Autouse, because it must run before `app` does. [M5.5 review cycle 1, KEYS-C1-03] In any
    spelling, as `Settings` reads them: a POSIX environment keeps `gemini_api_key` apart from
    `GEMINI_API_KEY`. [M5.5 review cycle 2, M55-KEYS-C2-04]
    """
    fields = {f"{provider}_api_key".upper() for provider in client.PROVIDERS}
    for field in fields:
        assert field.lower() in Settings.model_fields, field
    for name in [variable for variable in os.environ if variable.upper() in fields]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def double():
    """`ops/fake_llm.py`, loaded the way `test_fake_llm.py` loads it -- registered in `sys.modules`
    before it runs, for Pydantic's sake -- so each test holds a fresh module and a fresh log."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


class _Clock:
    """The fetcher's clock and sleeper together, so no pacing is waited out in real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


@pytest.fixture
def wired(double, monkeypatch):
    """The test button's one fetcher, pointed at the double: `client.open_fetcher` is the seam its
    docstring names for this, the way `registry.make_client` is Jellyfin's. The Fetcher is the real
    one, on the route's own connection, so the provider host's policy, pacing and breaker apply."""
    clock = _Clock()
    monkeypatch.setattr(client, "open_fetcher", lambda conn: fetch.Fetcher(
        conn=conn, transport=httpx.ASGITransport(app=double.app), clock=clock, sleep=clock.sleep,
        jitter=lambda low, high: 0.0,
    ))
    return double


async def _admin(app) -> httpx.AsyncClient:
    client_ = app()
    created = await client_.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client_


async def _read(admin) -> dict:
    response = await admin.get("/api/admin/llm")
    assert response.status_code == 200, response.text
    return response.json()


def _cards(body: dict) -> dict[str, dict]:
    return {card["name"]: card for card in body["providers"]}


def _shape(price: pricing.ModelPrice | None) -> dict | str:
    if price is None:
        return "unknown"
    return {"input": price.input, "output": price.output,
            "valid_until": price.valid_until.isoformat() if price.valid_until else None}


async def _pack_document(db) -> int:
    """A raw document for a metered call to cite (0028: `pack_document_id` is NOT NULL). Written
    through the raw store the app fixture pointed at this test's own DATA_DIR."""
    return await rawstore.store(
        db, source="pack", kind="dna", url="pack:title:7", content=b"[source:1] a pack",
        entity_key="title:7", content_type="text/plain; charset=utf-8", http_status=None,
    )


# --- the read ------------------------------------------------------------------------------------


async def test_a_fresh_install_reads_every_provider_unconfigured_no_cap_and_batch_unavailable(
    secrets_key, db, app
):
    """What M5.7 renders on the day nobody has typed anything: every provider named, keyless, on
    its default model with the table's price for it; no setting stored, so none reported (absent is
    null, and what stage 6 makes of an absent row is the estimate's reason, not a default this
    route guesses); a meter at zero with no cap in force (decision 325 ships none); an estimate
    that is "unknown" for the reason stage 6 would give; and batch unavailable with decision 338's
    reason rather than absent, so the toggle ships disabled and says why."""
    before = datetime.now(UTC)
    body = await _read(await _admin(app))
    after = datetime.now(UTC)

    # `projected` and each card's `models` and `price_basis` are M5.7's additions (decisions 343,
    # 451); every key M5.5 shipped is still here and still spelled as it was.
    assert set(body) == {"providers", "settings", "meter", "estimate", "projected", "batch"}
    assert [card["name"] for card in body["providers"]] == list(client.PROVIDERS)
    for name, card in _cards(body).items():
        model = pricing.DEFAULT_MODELS[name]
        assert {key: value for key, value in card.items() if key not in ("models", "price_basis")} == {
            "name": name, "configured": False, "has_api_key": False, "secrets_unreadable": False,
            "model": model, "structured_output": ADAPTERS[name].STRUCTURED_OUTPUT,
            "price": _shape(pricing.price_for(name, model)),
        }, name
        assert card["models"] == sorted(pricing.PRICING[name]), name
        assert card["price"] != "unknown", f"{name}'s default model must be one the table prices"

    assert body["settings"] == {
        "extraction_provider": None, "parallel": None, "parallel_providers": None, "passes": None,
        "cap_usd": None,
    }
    meter = body["meter"]
    assert (meter["spent_usd"], meter["cap_usd"], meter["remaining_usd"]) == ("0", None, None)
    start, end = (datetime.fromisoformat(meter[k]) for k in ("period_start", "period_end"))
    assert start <= before and after < end, meter
    assert meter["tz"]

    estimate = body["estimate"]
    assert (estimate["per_title_usd"], estimate["passes"], estimate["providers"]) == ("unknown", None, [])
    assert estimate["input_tokens_assumed"] == pricing.SPEC_INPUT_TOKENS == 23_500
    assert estimate["reason"].startswith("no extraction provider is assigned"), estimate
    assert body["batch"] == {"available": False, "reason": llm_api.BATCH_UNAVAILABLE}
    assert "decision 338" in llm_api.BATCH_UNAVAILABLE


async def test_a_configured_install_reads_back_its_settings_its_prices_and_the_estimate(
    secrets_key, db, app
):
    """The settings as stored, the admin's price override winning over the table (decision 343),
    the cap as the meter reads it, and the per-title estimate plan D4 exports -- priced at §8's
    23,500-token midpoint and `MEAN_OUTPUT_TOKENS` out, times the passes, over the providers the
    plan would call, spelled to the six places `llm_call.usd` keeps."""
    admin = await _admin(app)
    await registry.save_connector(db, "gemini", api_key="gemini-key-not-real")
    await registry.save_connector(db, "anthropic", api_key="anthropic-key-not-real",
                                  model="claude-sonnet-5", price_input=2, price_output=12.5)
    await registry.save_connector(db, "llm", extraction_provider="gemini", parallel=False, passes=2,
                                  cap_usd=25)

    body = await _read(admin)
    cards = _cards(body)
    table = pricing.price_for("gemini", "gemini-3.7-flash")
    assert cards["gemini"]["configured"] is cards["gemini"]["has_api_key"] is True
    assert cards["gemini"]["price"] == _shape(table)
    assert cards["anthropic"]["price"] == {"input": 2.0, "output": 12.5, "valid_until": None}
    assert cards["openai"]["configured"] is False

    assert body["settings"] == {
        "extraction_provider": "gemini", "parallel": False, "parallel_providers": None, "passes": 2,
        "cap_usd": 25,
    }
    assert (body["meter"]["cap_usd"], body["meter"]["remaining_usd"]) == ("25", "25")
    expected = pricing.estimate_title(tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[table], passes=2)
    assert {key: value for key, value in body["estimate"].items()
            if key not in ("output_tokens_assumed", "basis")} == {
        "per_title_usd": str(expected), "input_tokens_assumed": 23_500, "passes": 2,
        "providers": ["gemini"], "reason": None,
    }
    assert body["estimate"]["output_tokens_assumed"] == pricing.MEAN_OUTPUT_TOKENS == 3_900
    assert [(b["provider"], b["model"], b["source"]) for b in body["estimate"]["basis"]] == [
        ("gemini", "gemini-3.7-flash", "table")
    ]
    # (23,500 x in + 3,900 x out) / 1M per pass, twice: a string of six places, never a float.
    assert Decimal(body["estimate"]["per_title_usd"]) == 2 * (
        Decimal(23_500) * Decimal(str(table.input)) + Decimal(3_900) * Decimal(str(table.output))
    ) / Decimal(1_000_000)


async def test_no_stored_provider_key_is_ever_in_the_read(secrets_key, db, app):
    """§14.3, and `api/admin.py`'s idiom for the Jellyfin key: a GET that returned a provider key
    would make every admin session a copy of a credential that bills the household. Searched for in
    the raw body, so a key tucked into any field -- or into a reason quoting a setting -- is caught,
    not only one sitting where a parser would look."""
    admin = await _admin(app)
    keys = {name: f"sk-{name}-a-provider-key-that-must-not-leave-{name}" for name in client.PROVIDERS}
    for name, key in keys.items():
        await registry.save_connector(db, name, api_key=key)
    await registry.save_connector(db, "llm", extraction_provider="openai", cap_usd=5)

    response = await admin.get("/api/admin/llm")
    assert response.status_code == 200, response.text
    assert all(card["has_api_key"] for card in response.json()["providers"])
    for name, key in keys.items():
        assert key not in response.text, f"the read carries {name}'s key"


async def test_the_meter_read_includes_a_gemini_calls_billed_thinking_tokens(secrets_key, db, app):
    """Exit check 5 as the spend guard will read it. A Gemini call reporting 1,600 candidate and
    2,300 thought tokens is an `llm_call` row at 3,900 billed output, and the read's month spend is
    that row's cost to the digit: $0.029625 at the introductory gemini-3.7-flash price, where the
    visible 1,600 alone would read $0.021000 (§9: "counting visible JSON understates cost").
    Spelled as the meter's own digits, and the remainder under a one-dollar cap likewise."""
    admin = await _admin(app)
    doc = await _pack_document(db)
    price = pricing.price_for("gemini", "gemini-3.7-flash", on=date(2026, 11, 15))
    charge = pricing.usd(20_000, 1_600 + 2_300, price)
    assert charge == Decimal("0.029625") != pricing.usd(20_000, 1_600, price)
    await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=None, pass_index=1, attempt=1,
        tokens_in=20_000, tokens_out_billed=3_900, usd=charge, ok=True, error=None,
        pack_document_id=doc, response_document_id=None,
    )
    await registry.save_connector(db, "llm", cap_usd=1)

    meter = (await _read(admin))["meter"]
    assert (meter["spent_usd"], meter["cap_usd"], meter["remaining_usd"]) == (
        "0.029625", "1", "0.970375"
    )


async def test_an_unknown_models_price_and_the_estimate_over_it_read_unknown(secrets_key, db, app):
    """Decision 343 on the wire: a model neither the table nor an override prices is "unknown" on
    its card and in the estimate, never a number -- and the estimate's reason names the model,
    which is the sentence stage 6 would park the title under. The provider still has its key; it
    is not `configured`, because a cap cannot be held against a price nobody knows."""
    admin = await _admin(app)
    await registry.save_connector(db, "gemini", api_key="gemini-key-not-real", model="gemini-9-ultra")
    await registry.save_connector(db, "llm", extraction_provider="gemini", cap_usd=10)

    body = await _read(admin)
    card = _cards(body)["gemini"]
    assert (card["model"], card["price"], card["has_api_key"], card["configured"]) == (
        "gemini-9-ultra", "unknown", True, False
    )
    assert _cards(body)["openai"]["price"] != "unknown", "an unknown model is one card's answer"
    estimate = body["estimate"]
    assert (estimate["per_title_usd"], estimate["passes"], estimate["providers"]) == ("unknown", None, [])
    assert "no price is known for gemini model 'gemini-9-ultra'" in estimate["reason"], estimate


async def test_the_card_calls_a_provider_configured_exactly_when_the_gate_would_call_it(
    secrets_key, db, app
):
    """`configured` is this read's composition of two domain answers -- a key this SECRETS_KEY
    opens, and a price in effect for the provider's model -- so it is held to the gate it claims to
    summarise: with extraction assigned to each provider in turn, the card says configured exactly
    when `spend.extraction_plan` would call it. Three states, one of each: keyed and priced,
    keyed on a model nobody priced, and keyless."""
    admin = await _admin(app)
    await registry.save_connector(db, "anthropic", api_key="anthropic-key-not-real")
    await registry.save_connector(db, "openai", api_key="openai-key-not-real", model="gpt-9-nova")

    seen = {}
    for name in client.PROVIDERS:
        await registry.save_connector(db, "llm", extraction_provider=name)
        plan = await spend.extraction_plan(db)
        card = _cards(await _read(admin))[name]
        assert card["configured"] is isinstance(plan, spend.Plan), (name, card, plan)
        seen[name] = card["configured"]
    assert seen == {"anthropic": True, "openai": False, "gemini": False}


async def test_an_unreadable_provider_key_degrades_the_read_rather_than_failing_it(
    secrets_key, db, app, monkeypatch
):
    """M4.7 dd03 for the provider cards: a restored dump under a changed SECRETS_KEY leaves a row
    whose key will not open, and the card has to say "re-enter the key" rather than 500 or read as
    never set up. The model the admin typed survives, and the estimate names the unreadable key."""
    admin = await _admin(app)
    await registry.save_connector(db, "anthropic", api_key="anthropic-key-not-real",
                                  model="claude-sonnet-5")
    await registry.save_connector(db, "llm", extraction_provider="anthropic", cap_usd=10)

    monkeypatch.setenv("SECRETS_KEY", OTHER_SECRETS_KEY)
    settings.cache_clear()
    body = await _read(admin)
    card = _cards(body)["anthropic"]
    assert (card["secrets_unreadable"], card["has_api_key"], card["configured"], card["model"]) == (
        True, False, False, "claude-sonnet-5"
    )
    assert body["estimate"]["per_title_usd"] == "unknown"
    assert "the anthropic key cannot be read" in body["estimate"]["reason"], body["estimate"]


# --- the test dispatch ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", client.PROVIDERS)
async def test_the_test_button_reaches_the_provider_with_the_key_in_its_header_and_never_in_a_url(
    secrets_key, db, app, wired, provider, caplog
):
    """§6.6's test button for a provider card, end to end: the route, `registry.test_connector`,
    the provider's `ConnectorSpec.test`, `client.probe` and one GET through a real Fetcher to the
    double. What the double RECORDED is the evidence -- one request to the documented models-list
    url, the key in the header the provider documents, and nothing key-shaped in the url it
    received -- and the same holds for httpx's INFO line and for the response the admin sees. The
    model is typed rather than left to the default, because the probe asks the list about the row's
    own `model` and asks about none when the row names none."""
    caplog.set_level(logging.INFO, logger="httpx")
    admin = await _admin(app)
    key = wired.KEYS[provider]
    await registry.save_connector(db, provider, api_key=key, model=pricing.DEFAULT_MODELS[provider])

    response = await admin.post(f"/api/admin/connectors/{provider}/test")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert (answer["ok"], answer["status"], answer["model"], answer["model_listed"]) == (
        True, 200, pricing.DEFAULT_MODELS[provider], True
    ), answer
    assert key not in response.text

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wired.app)) as http:
        state = (await http.get("http://fake-llm/_test/state")).json()
    (sent,) = state["requests"]
    assert (sent["provider"], sent["method"], sent["url"]) == (
        provider, "GET", ADAPTERS[provider].MODELS_URL
    )
    assert (sent["key_header"], sent["key_in_url"]) == (True, False), sent
    assert DOCUMENTED_HEADER[provider] in sent["headers"], sent
    assert key not in json.dumps(state)

    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    host = httpx.URL(ADAPTERS[provider].MODELS_URL).host
    assert [line for line in lines if host in line], "httpx logged no line for the provider call"
    assert not [line for line in lines if key in line], lines


async def test_an_unknown_connector_and_one_with_no_test_are_404_with_the_registrys_reason(
    secrets_key, db, app
):
    """Plan A4's one table refuses by name rather than guessing a probe: a connector the registry
    does not know, and `llm`, the settings row, which has nothing to test. TMDB was this test's
    example until M5.7's source cards gave it a button (decision 453)."""
    admin = await _admin(app)
    unknown = await admin.post("/api/admin/connectors/nope/test")
    assert unknown.status_code == 404
    assert unknown.json()["detail"].startswith("no connector named 'nope'"), unknown.text
    untested = await admin.post("/api/admin/connectors/llm/test")
    assert untested.status_code == 404
    assert untested.json()["detail"] == "connector llm has no test in this build"


async def test_a_fault_inside_a_probe_is_a_server_error_and_never_a_404(
    secrets_key, db, app, monkeypatch
):
    """The 404 is for the registry's two refusals, which raise `LookupError` itself. A `KeyError`
    is a `LookupError` too, and one raised inside a probe -- a provider envelope missing a field --
    is a fault in this build, not a connector that does not exist: reported as 404 it would tell
    the admin the card they pressed is not there."""
    async def broken(conn):
        raise KeyError("models")

    monkeypatch.setitem(
        registry.CONNECTORS, "tmdb", dataclasses.replace(registry.CONNECTORS["tmdb"], test=broken)
    )
    admin = await _admin(app)
    with pytest.raises(KeyError, match="models"):
        await admin.post("/api/admin/connectors/tmdb/test")


async def test_the_jellyfin_test_button_is_still_its_own_route_which_keeps_the_verdict(
    secrets_key, db, app, fake_jellyfin, monkeypatch
):
    """Decision 433 keeps `api/admin.test_jellyfin`, because it stores §7.1's probed verdict as it
    tests, and the dispatch's path matches `/connectors/jellyfin/test` too -- so the Jellyfin card's
    button reaches its own handler only while `app.py` mounts the generic router after `admin`'s.
    Asserted by what only that handler does: the server moves below the pin under a configuration
    that did not change, and the button both reports it and stores it. Mounted the other way round,
    this same request is the dispatch's 404 for a connector with no test in its table."""
    module, transport = fake_jellyfin
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(cfg.url, cfg.api_key, transport=transport) if cfg.configured else None,
    )
    admin = await _admin(app)
    saved = await admin.put("/api/admin/connectors/jellyfin",
                            json={"url": "http://jellyfin.test", "api_key": module.API_KEY})
    assert saved.status_code == 200, saved.text

    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    probe = await admin.post("/api/admin/connectors/jellyfin/test")
    assert probe.status_code == 200, probe.text
    assert (probe.json()["ok"], probe.json()["server_name"], probe.json()["supported"]) == (
        True, "Fake Jellyfin", False
    )
    stored = await registry.load_jellyfin(db)
    assert (stored.server_version, stored.server_supported) == ("10.8.13", False)


def test_the_llm_router_declares_the_read_the_gated_write_the_cap_the_keys_and_the_dispatch():
    """Decision 433's two routes, and the five M5.7's cards brought (decisions 450, 452): the preview
    and the confirm that must carry its figure, the cap written in place, and the credentials read and
    write that never change an estimate. Exactly these: a second route that wrote a model or a price
    would be a way round the figure decision 450 makes the write carry."""
    declared = {
        (method, route.path) for route in llm_api.router.routes for method in route.methods
    }
    assert declared == {
        ("GET", "/api/admin/llm"), ("POST", "/api/admin/llm/preview"), ("PUT", "/api/admin/llm"),
        ("PUT", "/api/admin/llm/cap"), ("GET", "/api/admin/connectors"),
        ("PUT", "/api/admin/connectors/{name}"), ("POST", "/api/admin/connectors/{name}/test"),
    }
