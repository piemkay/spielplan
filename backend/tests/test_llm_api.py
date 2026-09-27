"""§6.6's LLM settings read and the connector test dispatch, over HTTP (§9, decision 433).
No secret leaves; the meter is spelled exactly, in strings. Needs TEST_DATABASE_URL."""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _no_operator_provider_keys(monkeypatch):
    """The provider seeds are removed before the app boots, so an operator's exported key cannot be
    sealed into this database. Any spelling: a POSIX environment keeps `gemini_api_key` apart."""
    fields = {f"{provider}_api_key".upper() for provider in client.PROVIDERS}
    for field in fields:
        assert field.lower() in Settings.model_fields, field
    for name in [variable for variable in os.environ if variable.upper() in fields]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def double():
    """Registered in `sys.modules` before it runs, for Pydantic's sake."""
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
    """0028: `pack_document_id` is NOT NULL."""
    return await rawstore.store(
        db, source="pack", kind="dna", url="pack:title:7", content=b"[source:1] a pack",
        entity_key="title:7", content_type="text/plain; charset=utf-8", http_status=None,
    )


async def test_a_fresh_install_reads_every_provider_unconfigured_no_cap_and_batch_unavailable(
    secrets_key, db, app
):
    """Absent settings are null: what stage 6 makes of an
    absent row is its reason, not a default guessed here."""
    before = datetime.now(UTC)
    body = await _read(await _admin(app))
    after = datetime.now(UTC)

    # `projected`, `models` and `price_basis` were added
    # later; every original key is still spelled as it was.
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
    """Searched for in the raw body, so a key tucked into any field is caught."""
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
    """1,600 candidate + 2,300 thought tokens bill as 3,900 output: $0.029625, not $0.021000."""
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
    """It is not `configured`, because a cap cannot be held against a price nobody knows."""
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
    """A restored dump under a changed SECRETS_KEY: the card says "re-enter the key", not 500."""
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


@pytest.mark.parametrize("provider", client.PROVIDERS)
async def test_the_test_button_reaches_the_provider_with_the_key_in_its_header_and_never_in_a_url(
    secrets_key, db, app, wired, provider, caplog
):
    """The double's recorded request is the evidence: the key in the documented header, never in the url."""
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
    """A `KeyError` inside a probe is a `LookupError` too, but it is a fault, not a missing connector."""
    async def broken(conn):
        raise KeyError("models")

    monkeypatch.setitem(registry.TESTS, "tmdb", broken)
    admin = await _admin(app)
    with pytest.raises(KeyError, match="models"):
        await admin.post("/api/admin/connectors/tmdb/test")


async def test_the_jellyfin_test_button_is_still_its_own_route_which_keeps_the_verdict(
    secrets_key, db, app, fake_jellyfin, monkeypatch
):
    """The Jellyfin button reaches its own handler only while
    `app.py` mounts the generic router after `admin`'s."""
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
    """A second route that wrote a model or a price would
    bypass the figure decision 450 makes the write carry."""
    declared = {
        (method, route.path) for route in llm_api.router.routes for method in route.methods
    }
    assert declared == {
        ("GET", "/api/admin/llm"), ("POST", "/api/admin/llm/preview"), ("PUT", "/api/admin/llm"),
        ("PUT", "/api/admin/llm/cap"), ("GET", "/api/admin/connectors"),
        ("PUT", "/api/admin/connectors/{name}"), ("POST", "/api/admin/connectors/{name}/test"),
    }
