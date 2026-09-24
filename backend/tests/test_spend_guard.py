"""§6.6's spend guard, write side: the number before the setting, the cap, and the keys. Spec v2.1
§6.6, §8 stage 6, §9; decisions 324, 325, 343, 339, 450, 451, 452.

§6.6 asks for a "per-title cost estimate before enabling", and plan §7's checks 1-3 put the whole
milestone in three lines: the number comes first, and saying no costs nothing. So what this file
holds is an ORDERING, asserted on the rows rather than on any one page's behaviour:

* **A PREVIEW WRITES NOTHING** (decision 450). Every change the spend guard can propose is previewed
  and `connector_config` is compared row for row, ciphertext and timestamps included, before and
  after -- not "the setting did not change" but "nothing was written", an upsert or a new row for a
  provider nobody configured included.
* **A CONFIRM CARRIES THE FIGURE IT WAS SHOWN.** `PUT /api/admin/llm` without it is 422, with a wrong
  or stale one is 409 carrying the fresh preview and nothing stored, and with the right one stores
  exactly the change and bills nothing.
* **NO PROVIDER ENTERS THE PLAN WITHOUT A USABLE KEY**, whichever way it would enter, because a key
  typed later would otherwise start spend at a figure nobody was shown.
* **THE CAP TAKES EFFECT AT ONCE** (decision 452) and refuses every value that is not a finite
  number of at least zero; zero is a cap (decision 325).
* **A KEY IS WRITE-ONLY** everywhere a response, a 422 or a log line could carry it.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from spielplan.acquire import fetch, intake, pipeline, rawstore
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.core.config import settings
from spielplan.llm import client, pricing, spend

ADMIN_PASSWORD = "an-admin-password"
OTHER_SECRETS_KEY = "a-different-secrets-key-not-a-real-one"

# Distinctive enough that finding one in a response body can only be a leak.
KEY_GEMINI = "AIza-gemini-key-for-the-spend-guard-0001"
KEY_ANTHROPIC = "sk-ant-anthropic-key-for-the-spend-guard-0002"
KEY_OPENAI = "sk-openai-key-for-the-spend-guard-0003"
KEY_TMDB = "tmdb-key-for-the-spend-guard-0004"
KEY_OMDB = "omdb-key-for-the-spend-guard-0005"
TRAKT_ID = "trakt-client-id-for-the-spend-guard-0006"
TRAKT_SECRET = "trakt-client-secret-for-the-spend-guard-0007"
EVERY_KEY = (KEY_GEMINI, KEY_ANTHROPIC, KEY_OPENAI, KEY_TMDB, KEY_OMDB, TRAKT_ID, TRAKT_SECRET)

PREVIEW = "/api/admin/llm/preview"
CONFIRM = "/api/admin/llm"
CAP = "/api/admin/llm/cap"


# --- the install ---------------------------------------------------------------------------------


async def _admin(app) -> httpx.AsyncClient:
    client_ = app()
    created = await client_.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client_


async def _rows(db) -> list[dict]:
    """Every `connector_config` row as stored: plaintext half, sealed half, key id and timestamps."""
    return [dict(row) for row in await db.fetch("SELECT * FROM connector_config ORDER BY name")]


async def _preview(admin, change: dict) -> dict:
    response = await admin.post(PREVIEW, json=change)
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm(admin, change: dict) -> httpx.Response:
    """Preview, then confirm with the figure the preview showed: the one client path decision 450
    leaves to `PUT /api/admin/llm`."""
    shown = (await _preview(admin, change))["estimate"]["per_title_usd"]
    return await admin.put(CONFIRM, json={**change, "accepted_estimate": shown})


async def _keyed(db) -> None:
    """Gemini and Anthropic keyed, extraction assigned to Gemini, and a cap with room."""
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    await registry.save_connector(db, "anthropic", api_key=KEY_ANTHROPIC)
    await registry.save_connector(db, "llm", extraction_provider="gemini", cap_usd=25)


async def _pack_document(db) -> int:
    """A raw document for a metered call to cite (0028: `pack_document_id` is NOT NULL), written
    through the raw store the app fixture pointed at this test's own DATA_DIR."""
    return await rawstore.store(
        db, source="pack", kind="dna", url="pack:title:7", content=b"[source:1] a pack",
        entity_key="title:7", content_type="text/plain; charset=utf-8", http_status=None,
    )


async def _task(db, key: str, *, kind: str = pipeline.TASK_KIND, priority: int = 100,
                age: timedelta = timedelta(0)) -> None:
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, priority, created_at) VALUES ($1, $2, $3, $4)",
        kind, key, priority, datetime.now(UTC) - age,
    )


# --- the preview writes nothing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"extraction_provider": "anthropic"}, id="assign"),
        pytest.param({"parallel": True, "parallel_providers": ["gemini", "anthropic"]}, id="parallel"),
        pytest.param({"passes": 2}, id="passes"),
        pytest.param({"providers": {"gemini": {"model": "gemini-3.6-flash"}}}, id="model"),
        pytest.param({"providers": {"anthropic": {"price_input": 1.5, "price_output": 9}}}, id="price"),
        pytest.param({"extraction_provider": None}, id="unassign"),
        pytest.param({"providers": {"gemini": {"model": None, "price_input": None,
                                               "price_output": None}}}, id="unset"),
        # A provider with no row at all: the preview must not create one to read it.
        pytest.param({"extraction_provider": "openai"}, id="unconfigured-provider"),
    ],
)
async def test_a_preview_of_any_change_writes_nothing(secrets_key, db, app, change):
    """Plan §7 checks 1 and 2. The preview answers the three figures -- the per-title estimate, the
    projected month and the meter with its remaining cap -- and the stored configuration is
    byte-identical afterwards: every row, both halves, every timestamp, and no row added."""
    admin = await _admin(app)
    await _keyed(db)
    await registry.save_connector(db, "gemini", price_input=0.5, price_output=2.5)
    before = await _rows(db)

    body = await _preview(admin, change)

    assert set(body) == {"estimate", "projected", "meter", "blocked"}
    assert "per_title_usd" in body["estimate"] and "monthly_usd" in body["projected"]
    assert "remaining_usd" in body["meter"]
    assert await _rows(db) == before


# --- the confirm carries its figure --------------------------------------------------------------


async def test_a_confirm_without_the_figure_is_422_and_with_the_wrong_one_is_409_storing_nothing(
    secrets_key, db, app
):
    """Decision 450: the figure is part of the write, so a client that never showed one cannot write,
    and one that showed a different figure is shown the right one instead of having it accepted."""
    admin = await _admin(app)
    await _keyed(db)
    before = await _rows(db)

    missing = await admin.put(CONFIRM, json={"passes": 2})
    assert missing.status_code == 422, missing.text
    assert await _rows(db) == before

    wrong = await admin.put(CONFIRM, json={"passes": 2, "accepted_estimate": "0.000001"})
    assert wrong.status_code == 409, wrong.text
    refused = wrong.json()
    assert isinstance(refused["detail"], str) and "decision 450" in refused["detail"]
    assert refused["preview"] == await _preview(admin, {"passes": 2})
    assert refused["preview"]["estimate"]["passes"] == 2
    assert await _rows(db) == before


async def test_a_confirmed_change_is_stored_exactly_and_bills_nothing(secrets_key, db, app):
    """Plan §7 check 3: confirming stores the change and leaves the meter where it was. Nothing is
    sent to a provider, so no `llm_call` row appears and the month's spend is unchanged; and only the
    fields the change named move, beside the cap and the key they sit next to."""
    admin = await _admin(app)
    await _keyed(db)
    meter_before = await spend.meter(db)
    calls_before = await db.fetchval("SELECT count(*) FROM llm_call")
    change = {"passes": 2, "providers": {"gemini": {"model": "gemini-3.6-flash"}}}

    confirmed = await _confirm(admin, change)
    assert confirmed.status_code == 200, confirmed.text
    read = await admin.get("/api/admin/llm")
    assert confirmed.json() == read.json()

    llm = await registry.load_connector(db, "llm")
    assert llm.config == {"extraction_provider": "gemini", "cap_usd": 25, "passes": 2}
    gemini = await registry.load_connector(db, "gemini")
    assert gemini.config == {"model": "gemini-3.6-flash"}
    assert gemini.secrets == {"api_key": KEY_GEMINI}
    assert await db.fetchval("SELECT count(*) FROM llm_call") == calls_before
    assert (await spend.meter(db))["spent_usd"] == meter_before["spent_usd"]
    assert confirmed.json()["estimate"]["passes"] == 2


async def test_a_figure_gone_stale_between_preview_and_confirm_is_refused_with_the_new_one(
    secrets_key, db, app
):
    """The case the figure is carried for: a price edited in another tab between the preview and the
    confirm. The confirm is refused 409 with the figure the admin has not seen, and nothing it
    carried is stored."""
    admin = await _admin(app)
    await _keyed(db)
    shown = (await _preview(admin, {"passes": 2}))["estimate"]["per_title_usd"]

    elsewhere = await _confirm(admin, {"providers": {"gemini": {"price_input": 2, "price_output": 8}}})
    assert elsewhere.status_code == 200, elsewhere.text

    stale = await admin.put(CONFIRM, json={"passes": 2, "accepted_estimate": shown})
    assert stale.status_code == 409, stale.text
    fresh = stale.json()["preview"]["estimate"]["per_title_usd"]
    assert fresh != shown
    assert Decimal(fresh) == pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[pricing.ModelPrice(2.0, 8.0)], passes=2)
    assert "passes" not in (await registry.load_connector(db, "llm")).config


# --- no provider without a usable key ------------------------------------------------------------


@pytest.mark.parametrize(
    "change,named",
    [
        pytest.param({"extraction_provider": "openai"}, "openai", id="keyless"),
        pytest.param({"parallel": True, "parallel_providers": ["gemini", "openai"]}, "openai",
                     id="parallel-entry-keyless"),
        # The plan refuses Gemini's unpriced model first, and the keyless second provider must still
        # be found: a price fixed later through this route would otherwise be the only gate left.
        pytest.param({"parallel": True, "parallel_providers": ["gemini", "openai"],
                      "providers": {"gemini": {"model": "gemini-9-ultra"}}}, "openai",
                     id="keyless-behind-another-refusal"),
        pytest.param({"extraction_provider": "anthropic"}, "anthropic", id="header-unfit"),
    ],
)
async def test_a_provider_without_a_usable_key_is_blocked_and_its_confirm_refused(
    secrets_key, db, app, change, named
):
    """Decision 450's third clause. A change that would put a provider with no usable key into the
    plan is `blocked` in the preview and refused 409 by the confirm even when it carries the figure
    the preview showed -- because that figure is "unknown", and a key typed later would turn it into
    spend nobody was shown. The block names the provider and never a key."""
    admin = await _admin(app)
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    # A key no header can carry: whitespace inside it (`client.header_key`).
    await registry.save_connector(db, "anthropic", api_key="sk-ant two words")
    await registry.save_connector(db, "llm", extraction_provider="gemini", cap_usd=25)
    before = await _rows(db)

    body = await _preview(admin, change)
    assert body["blocked"] and named in body["blocked"], body["blocked"]
    assert "two words" not in body["blocked"]
    refused = await admin.put(
        CONFIRM, json={**change, "accepted_estimate": body["estimate"]["per_title_usd"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["preview"]["blocked"] == body["blocked"]
    assert await _rows(db) == before


async def test_an_unreadable_key_is_blocked_too(secrets_key, db, app, monkeypatch):
    """A key this SECRETS_KEY cannot open is a key stage 6 cannot send (M4.7 dd03), and the one that
    reappears by itself the day the right SECRETS_KEY is restored -- which is exactly the key typed
    later that decision 450 refuses to let start spend at an unseen figure."""
    admin = await _admin(app)
    await registry.save_connector(db, "anthropic", api_key=KEY_ANTHROPIC)
    await registry.save_connector(db, "llm", cap_usd=25)
    monkeypatch.setenv("SECRETS_KEY", OTHER_SECRETS_KEY)
    settings.cache_clear()
    before = await _rows(db)

    body = await _preview(admin, {"extraction_provider": "anthropic"})
    assert body["blocked"] and registry.SECRETS_UNREADABLE_REASON in body["blocked"], body
    refused = await admin.put(CONFIRM, json={"extraction_provider": "anthropic",
                                             "accepted_estimate": body["estimate"]["per_title_usd"]})
    assert refused.status_code == 409, refused.text
    assert await _rows(db) == before


# --- the figure ----------------------------------------------------------------------------------


async def test_the_figure_scales_with_the_passes_and_the_providers(secrets_key, db, app):
    """Plan §7 check 4, the card half (the flywheel half is M5.6's, decision 456). One provider at two
    passes is twice one run, and two providers at two passes -- both priced alike -- four times: the
    "4x the single-run figure" decision 324 keeps parallel mode off by default to spare a household."""
    admin = await _admin(app)
    await _keyed(db)
    alike = {"price_input": 1, "price_output": 5}
    priced = {"providers": {"gemini": alike, "anthropic": alike}}

    one = await _preview(admin, {**priced, "extraction_provider": "gemini", "passes": 1})
    two = await _preview(admin, {**priced, "extraction_provider": "gemini", "passes": 2})
    four = await _preview(admin, {**priced, "parallel": True,
                                  "parallel_providers": ["gemini", "anthropic"], "passes": 2})
    single = Decimal(one["estimate"]["per_title_usd"])
    assert single > 0
    assert Decimal(two["estimate"]["per_title_usd"]) == 2 * single
    assert Decimal(four["estimate"]["per_title_usd"]) == 4 * single
    assert four["estimate"]["providers"] == ["gemini", "anthropic"]
    assert four["estimate"]["passes"] == 2


async def test_a_fresh_install_previews_decision_324s_default_of_one_provider_at_one_pass(
    secrets_key, db, app
):
    """Decision 324, now assertable (the row's old note forbade it until the decision was taken):
    parallel mode is off and extraction runs one pass on a fresh install. Assigning a provider and
    nothing else previews that provider alone at one pass, and confirming it stores the assignment
    and no default beside it -- absent stays the default, so the default lives in `llm/spend` once."""
    admin = await _admin(app)
    await registry.save_connector(db, "gemini", api_key=KEY_GEMINI)
    assert (await registry.load_connector(db, "llm")).config == {}

    body = await _preview(admin, {"extraction_provider": "gemini"})
    assert (body["estimate"]["providers"], body["estimate"]["passes"]) == (["gemini"], 1)
    assert body["blocked"] is None

    confirmed = await _confirm(admin, {"extraction_provider": "gemini"})
    assert confirmed.status_code == 200, confirmed.text
    assert (await registry.load_connector(db, "llm")).config == {"extraction_provider": "gemini"}
    read = confirmed.json()
    assert (read["settings"]["parallel"], read["settings"]["passes"]) == (None, None)
    assert (read["estimate"]["providers"], read["estimate"]["passes"]) == (["gemini"], 1)


async def test_the_projection_counts_what_this_install_filed_in_the_last_thirty_days(
    secrets_key, db, app
):
    """Decision 451. No task ever filed is "no history" and no figure; then the month is the
    per-title estimate times the `acquire` tasks filed in the trailing thirty days -- not the older
    ones, not another kind's, and not the re-offers stage 1 closes unwalked (decision 411)."""
    admin = await _admin(app)
    await _keyed(db)

    empty = (await _preview(admin, {}))["projected"]
    assert (empty["window_days"], empty["titles"], empty["ever_filed"]) == (30, 0, False)
    assert empty["monthly_usd"] is None
    assert "no acquisition history" in empty["reason"]

    for n in range(3):
        await _task(db, f"jf:recent-{n}")
    await _task(db, "jf:old", age=timedelta(days=40))
    await _task(db, "jf:re-offer", priority=intake.RE_OFFER_PRIORITY)
    await _task(db, "jf:other-kind", kind="acquire-something-else")

    body = await _preview(admin, {})
    projected, per_title = body["projected"], Decimal(body["estimate"]["per_title_usd"])
    assert (projected["titles"], projected["ever_filed"]) == (3, True)
    assert Decimal(projected["monthly_usd"]) == 3 * per_title
    assert (projected["remaining_usd"], projected["exceeds_remaining"]) == ("25", False)
    assert projected["reason"] is None

    unknown = (await _preview(admin, {"providers": {"gemini": {"model": "gemini-9-ultra"}}}))
    assert unknown["projected"]["monthly_usd"] == "unknown"

    await db.execute("UPDATE connector_config SET config = config - 'cap_usd' WHERE name = 'llm'")
    uncapped = (await _preview(admin, {}))["projected"]
    assert (uncapped["remaining_usd"], uncapped["exceeds_remaining"]) == (None, None)
    assert "no spend cap" in uncapped["reason"]


# --- the cap -------------------------------------------------------------------------------------


async def test_the_cap_takes_effect_at_once_and_at_the_cap_the_park_names_it(secrets_key, db, app):
    """Decision 452: the cap is written in place and needs no preview, because it is the guard itself.
    The next read of the cap is the new one, the route answers the meter, and with the month spent
    to the cap the preview's meter has nothing left and the gate stage 6 asks parks under the reason
    the board and M5.6's admin retry both show, `over spend cap` (decision 325, `retry_refusal`)."""
    admin = await _admin(app)
    await _keyed(db)

    written = await admin.put(CAP, json={"cap_usd": 1})
    assert written.status_code == 200, written.text
    assert set(written.json()) == {"meter"}
    assert (written.json()["meter"]["cap_usd"], written.json()["meter"]["remaining_usd"]) == ("1", "1")
    assert await spend.cap(db) == Decimal("1")

    doc = await _pack_document(db)
    await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=None, pass_index=1, attempt=1,
        tokens_in=20_000, tokens_out_billed=3_900, usd=Decimal("1.000000"), ok=True, error=None,
        pack_document_id=doc, response_document_id=None,
    )
    meter = (await _preview(admin, {}))["meter"]
    assert Decimal(meter["remaining_usd"]) == 0
    refusal = await spend.cap_check(db, title_id=7)
    assert refusal.kind == spend.OVER_CAP
    assert refusal.reason.startswith(spend.OVER_CAP_PREFIX)
    assert (await spend.retry_refusal(db, title_id=7)).startswith(spend.OVER_CAP_PREFIX)

    raised = await admin.put(CAP, json={"cap_usd": 2.5})
    assert raised.status_code == 200, raised.text
    assert await spend.cap(db) == Decimal("2.5")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"cap_usd": true}', id="bool"),
        pytest.param(b'{"cap_usd": "25"}', id="string"),
        pytest.param(b'{"cap_usd": NaN}', id="nan"),
        pytest.param(b'{"cap_usd": Infinity}', id="infinity"),
        pytest.param(b'{"cap_usd": -1}', id="negative"),
        pytest.param(b'{"cap_usd": null}', id="null-is-no-way-back-to-unset"),
        pytest.param(b"{}", id="absent"),
        pytest.param(b'{"cap_usd": 5, "passes": 2}', id="an-estimate-field-beside-it"),
    ],
)
async def test_the_cap_route_refuses_what_is_not_a_finite_number_of_at_least_zero(
    secrets_key, db, app, raw
):
    """Decision 452's refusals, each one a value that `spend._cap_of` would otherwise read as "no cap"
    and log, or -- for infinity -- as a cap that never binds. Python's JSON reader takes `NaN` and
    `Infinity` literally, so they are sent as bytes to reach the route at all."""
    admin = await _admin(app)
    await registry.save_connector(db, "llm", cap_usd=25)
    before = await _rows(db)

    refused = await admin.put(CAP, content=raw, headers={"content-type": "application/json"})
    assert refused.status_code == 422, refused.text
    assert await _rows(db) == before


async def test_a_cap_of_zero_is_stored_as_a_cap(secrets_key, db, app):
    """Zero is a real cap meaning "spend nothing" (decision 325), not a falsy value read as unset."""
    admin = await _admin(app)
    stored = await admin.put(CAP, json={"cap_usd": 0})
    assert stored.status_code == 200, stored.text
    assert (await registry.load_connector(db, "llm")).config == {"cap_usd": 0}
    assert await spend.cap(db) == Decimal("0")
    assert stored.json()["meter"]["cap_usd"] == "0"


# --- keys ----------------------------------------------------------------------------------------


async def test_an_empty_key_field_keeps_the_stored_key_and_no_answer_carries_one(
    secrets_key, db, app, monkeypatch, caplog
):
    """Plan §7 checks 8 and 9. A key saved through the generic route is sealed and answered as a
    boolean; an empty field and an absent one keep it. Then every route this milestone serves is
    asked something -- the read, the preview, a confirm, the cap, the connectors read, a key write,
    a source test whose host echoes the key back -- and each body, and every log line the app wrote
    meanwhile, is searched for every stored key and client id."""
    caplog.set_level(logging.DEBUG)
    admin = await _admin(app)
    await registry.save_connector(db, "llm", extraction_provider="gemini", cap_usd=25)

    saved = await admin.put("/api/admin/connectors/gemini", json={"api_key": KEY_GEMINI})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"name": "gemini", "has_api_key": True, "secrets_unreadable": False}
    for keep in ({"api_key": ""}, {}):
        kept = await admin.put("/api/admin/connectors/gemini", json=keep)
        assert kept.status_code == 200, kept.text
        assert kept.json()["has_api_key"] is True
    assert (await registry.load_connector(db, "gemini")).secrets == {"api_key": KEY_GEMINI}

    for name, body in (("anthropic", {"api_key": KEY_ANTHROPIC}), ("openai", {"api_key": KEY_OPENAI}),
                       ("tmdb", {"api_key": KEY_TMDB}), ("omdb", {"api_key": KEY_OMDB}),
                       ("trakt", {"client_id": TRAKT_ID, "client_secret": TRAKT_SECRET})):
        response = await admin.put(f"/api/admin/connectors/{name}", json=body)
        assert response.status_code == 200, response.text
    assert (await registry.load_connector(db, "trakt")).config == {"client_id": TRAKT_ID}
    kept = await admin.put("/api/admin/connectors/trakt", json={"client_id": "", "client_secret": ""})
    assert kept.json()["has_client_id"] is kept.json()["has_client_secret"] is True

    # The TMDB host refuses the key and quotes it back, in the body and in a url.
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "status_message": f"Invalid API key {KEY_TMDB}", "url": str(request.url)})

    monkeypatch.setattr(client, "open_fetcher", lambda conn: fetch.Fetcher(
        conn=conn, transport=httpx.MockTransport(echo), sleep=_no_sleep, jitter=lambda a, b: 0.0))

    bodies = [saved.text, kept.text]
    for method, path, payload in (
        ("GET", "/api/admin/llm", None),
        ("POST", PREVIEW, {"passes": 2}),
        ("GET", "/api/admin/connectors", None),
        ("PUT", CAP, {"cap_usd": 30}),
        ("PUT", "/api/admin/connectors/omdb", {"api_key": ""}),
        ("POST", "/api/admin/connectors/tmdb/test", None),
        # A 422 for a key that is too long, and for one holding a control character, must not quote
        # what it refused: FastAPI's own 422 echoes the input, and a card that shows the error
        # would put the key into the DOM.
        ("PUT", "/api/admin/connectors/openai", {"api_key": KEY_OPENAI * 20}),
        ("PUT", "/api/admin/connectors/openai", {"api_key": KEY_OPENAI + "\n"}),
        ("PUT", "/api/admin/connectors/openai", {"apikey": KEY_OPENAI}),
        ("POST", PREVIEW, {"api_key": KEY_OPENAI}),
    ):
        response = await admin.request(method, path, json=payload)
        assert response.status_code in (200, 422), (path, response.status_code, response.text)
        bodies.append(response.text)
    confirmed = await _confirm(admin, {"passes": 2})
    assert confirmed.status_code == 200, confirmed.text
    bodies.append(confirmed.text)

    test_answer = await admin.post("/api/admin/connectors/tmdb/test")
    assert (test_answer.json()["ok"], test_answer.json()["status"]) == (False, 401)
    for key in EVERY_KEY:
        for text in bodies:
            assert key not in text, f"a response carries a stored key: {text[:200]}"
        assert key not in caplog.text, "a log line carries a stored key"


async def _no_sleep(seconds: float) -> None:
    return None


async def test_the_key_route_refuses_estimate_fields_and_the_llm_row_and_leaves_jellyfin_alone(
    secrets_key, db, app, fake_jellyfin, monkeypatch
):
    """Decision 452. The generic route writes credentials and nothing else: a model or a price
    override would change the estimate outside decision 450's figure, so each is 422 and nothing in
    the same body is stored; a field another connector owns is 422; the `llm` row is 409 naming the
    route that writes it; an unknown name is the registry's 404. And `PUT /connectors/jellyfin` is
    still `api/admin.put_jellyfin`, mounted first -- asserted by the fields only that handler
    answers with."""
    admin = await _admin(app)
    for body in ({"model": "gemini-3.6-flash"}, {"price_input": 1, "price_output": 2},
                 {"api_key": KEY_GEMINI, "model": "gemini-3.6-flash"}):
        refused = await admin.put("/api/admin/connectors/gemini", json=body)
        assert refused.status_code == 422, (body, refused.text)
    assert (await registry.load_connector(db, "gemini")).secrets == {}

    other = await admin.put("/api/admin/connectors/tmdb", json={"client_id": "not-tmdbs"})
    assert other.status_code == 422, other.text
    settings_row = await admin.put("/api/admin/connectors/llm", json={"api_key": KEY_GEMINI})
    assert settings_row.status_code == 409, settings_row.text
    assert "PUT /api/admin/llm" in settings_row.json()["detail"]
    unknown = await admin.put("/api/admin/connectors/nope", json={})
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"].startswith("no connector named 'nope'")

    module, transport = fake_jellyfin
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(cfg.url, cfg.api_key, transport=transport) if cfg.configured else None,
    )
    jellyfin = await admin.put("/api/admin/connectors/jellyfin",
                               json={"url": "http://jellyfin.test", "api_key": module.API_KEY})
    assert jellyfin.status_code == 200, jellyfin.text
    assert {"url", "library_ids", "webhook_token", "server_supported"} <= set(jellyfin.json())
    assert module.API_KEY not in jellyfin.text


async def test_whitespace_around_a_key_is_no_part_of_it_and_whitespace_alone_keeps_the_stored_one(
    secrets_key, db, app
):
    """Decision 452's "an empty field keeps the stored value", for a field that only LOOKS empty, and
    `client.header_key`'s rule for every keyed connector rather than for the three providers alone.

    A stray space and Save replaced the household's working key with three spaces while the card
    still said "(stored)", and a key copied with a trailing space was stored with it: TMDB and OMDb
    send it as `api_key=KEY+`, which the host refuses, so stage 2 parked every title on a key the
    admin believed they had pasted correctly. Whitespace around a key is trimmed, whitespace alone
    keeps what is stored, and a character no request can carry -- whitespace inside, anything past
    printable ASCII -- is a 422 that names the fault and never the value. [M5.7 review cycle 1,
    M57-KEYS-C1-02]"""
    admin = await _admin(app)
    stored = {
        ("gemini", "api_key"): KEY_GEMINI, ("anthropic", "api_key"): KEY_ANTHROPIC,
        ("openai", "api_key"): KEY_OPENAI, ("tmdb", "api_key"): KEY_TMDB,
        ("omdb", "api_key"): KEY_OMDB, ("trakt", "client_id"): TRAKT_ID,
        ("trakt", "client_secret"): TRAKT_SECRET,
    }

    async def held(name: str, field: str) -> str | None:
        state = await registry.load_connector(db, name)
        return (state.config if field == "client_id" else state.secrets).get(field)

    for (name, field), key in stored.items():
        path = f"/api/admin/connectors/{name}"
        assert (await admin.put(path, json={field: key})).status_code == 200
        for blank in ("   ", "\t \n", " "):
            kept = await admin.put(path, json={field: blank})
            assert kept.status_code == 200, (name, field, kept.text)
            assert await held(name, field) == key, f"{name}.{field}: a blank field replaced the key"
        padded = await admin.put(path, json={field: f"  {key}-two \t\r\n"})
        assert padded.status_code == 200, (name, field, padded.text)
        assert await held(name, field) == f"{key}-two", f"{name}.{field} was stored with its padding"
        for inner in (f"{key[:9]} {key[9:]}-three", f"{key}\u200b-three", f"{key}-thr\u00e9e"):
            refused = await admin.put(path, json={field: inner})
            assert refused.status_code == 422, (name, field, refused.text)
            assert key[:9] not in refused.text, "a refused key was quoted back"
            assert await held(name, field) == f"{key}-two"


async def test_the_setup_route_writes_no_spend_setting_and_no_provider(secrets_key, db, app):
    """Decision 450's ordering is a property of the API, so it holds on the one other route that writes
    `connector_config`: `POST /api/setup/connectors` takes any name and any config and stores them
    whole, and it stays mounted after first boot. Through it a plan at three passes over two
    providers, a provider priced at zero -- whose calls then meter $0, so the cap never binds -- and
    a keyless assignment that a key saved later on the card turns billable were each stored with no
    figure ever shown. The spend settings and the three providers are 409 there, naming the routes
    that write them, and nothing is stored; a source's key still seeds through it, as
    `test_secrets_custody.py` relies on. [M5.7 review cycle 1, M57-THESIS-01]"""
    admin = await _admin(app)
    await _keyed(db)
    before = await _rows(db)

    for name, config, secrets_ in (
        ("llm", {"extraction_provider": "gemini", "parallel": True,
                 "parallel_providers": ["gemini", "anthropic"], "passes": 3, "cap_usd": 25}, None),
        ("llm", {"extraction_provider": "openai", "cap_usd": 25}, None),
        ("anthropic", {"model": "claude-opus-5", "price_input": 0, "price_output": 0},
         {"api_key": KEY_ANTHROPIC}),
        ("gemini", {"price_input": 0, "price_output": 0}, None),
        ("openai", {}, {"api_key": KEY_OPENAI}),
    ):
        refused = await admin.post(
            "/api/setup/connectors", json={"name": name, "config": config, "secrets": secrets_}
        )
        assert refused.status_code == 409, (name, refused.text)
        detail = refused.json()["detail"]
        for route in ("PUT /api/admin/llm ", "PUT /api/admin/llm/cap", "PUT /api/admin/connectors/"):
            assert route in detail, (route, detail)
        assert KEY_ANTHROPIC not in refused.text and KEY_OPENAI not in refused.text
    assert await _rows(db) == before, "the setup route wrote past the figure"
    assert await spend.cap_check(db, title_id=7) is None

    seeded = await admin.post("/api/setup/connectors",
                              json={"name": "tmdb", "secrets": {"api_key": KEY_TMDB}})
    assert seeded.status_code == 200, seeded.text


# --- unset ---------------------------------------------------------------------------------------


async def test_an_explicit_null_returns_each_setting_to_its_default(secrets_key, db, app):
    """Decision 450: in the body an absent field keeps and an explicit null unsets. A model returns to
    `pricing.DEFAULT_MODELS`, a null price pair returns the provider to the table, and an unassigned
    extraction provider parks stage 6 naming the assignment rather than guessing one (decision 324).
    Half a price pair is refused, because decision 343 reads half an override as none at all."""
    admin = await _admin(app)
    await _keyed(db)
    await registry.save_connector(db, "gemini", model="gemini-3.6-flash", price_input=1, price_output=4)

    for half in ({"price_input": 1}, {"price_input": 1, "price_output": None}):
        refused = await admin.post(PREVIEW, json={"providers": {"gemini": half}})
        assert refused.status_code == 422, (half, refused.text)

    model = await _confirm(admin, {"providers": {"gemini": {"model": None}}})
    assert model.status_code == 200, model.text
    card = next(c for c in model.json()["providers"] if c["name"] == "gemini")
    assert card["model"] == pricing.DEFAULT_MODELS["gemini"]
    assert card["price_basis"]["source"] == "override"

    price = await _confirm(admin, {"providers": {"gemini": {"price_input": None, "price_output": None}}})
    assert price.status_code == 200, price.text
    assert (await registry.load_connector(db, "gemini")).config == {}
    assert price.json()["estimate"]["basis"][0]["source"] == "table"

    unassigned = await _confirm(admin, {"extraction_provider": None})
    assert unassigned.status_code == 200, unassigned.text
    assert (await registry.load_connector(db, "llm")).config == {"cap_usd": 25}
    refusal = await spend.cap_check(db, title_id=7)
    assert (refusal.kind, refusal.detail) == (spend.PLAN, {"setting": "extraction_provider"})
    assert refusal.reason.startswith("no extraction provider is assigned")

    empty = await admin.put(CONFIRM, json={"accepted_estimate": "unknown"})
    assert empty.status_code == 422, empty.text


# --- the price basis -----------------------------------------------------------------------------


def _spelled(basis: pricing.PriceBasis) -> dict:
    return {
        "provider": basis.provider, "model": basis.model, "source": basis.source,
        "input": basis.price.input, "output": basis.price.output,
        "valid_until": basis.price.valid_until.isoformat() if basis.price.valid_until else None,
        "then": None if basis.then is None else {"input": basis.then.input, "output": basis.then.output},
    }


async def test_every_estimate_names_its_model_and_its_price_basis(secrets_key, db, app):
    """The new row's first clause. The estimate, and each card, name the model and the price basis the
    figure was computed from -- `table` with its `valid_until` and the price after it, or `override`
    -- so the caption can say which price and which date an accepted figure rested on. The expected
    basis is `pricing.price_basis`'s on the install's own day, the day the route prices on."""
    admin = await _admin(app)
    await _keyed(db)
    today = datetime.now(UTC).astimezone(spend.local_zone()).date()

    read = (await admin.get("/api/admin/llm")).json()
    table = pricing.price_basis("gemini", "gemini-3.7-flash", on=today)
    assert read["estimate"]["basis"] == [_spelled(table)]
    assert read["estimate"]["output_tokens_assumed"] == pricing.MEAN_OUTPUT_TOKENS
    cards = {card["name"]: card for card in read["providers"]}
    assert cards["gemini"]["price_basis"] == _spelled(table)
    assert cards["gemini"]["models"] == sorted(pricing.PRICING["gemini"])
    if today < table.price.valid_until:
        assert read["estimate"]["basis"][0]["valid_until"] == "2027-01-01"
        assert read["estimate"]["basis"][0]["then"] == {"input": 1.5, "output": 7.5}

    body = await _preview(admin, {"extraction_provider": "anthropic",
                                  "providers": {"anthropic": {"price_input": 2, "price_output": 11}}})
    assert body["estimate"]["basis"] == [{
        "provider": "anthropic", "model": pricing.DEFAULT_MODELS["anthropic"], "source": "override",
        "input": 2, "output": 11, "valid_until": None, "then": None,
    }]


async def test_an_unknown_model_estimates_to_unknown_and_prints_no_figure_for_it(
    secrets_key, db, app
):
    """Plan §7 check 5 through the new route: a model nobody priced previews as "unknown" with no basis
    and a projected month that is "unknown" too -- never a number. Confirming that figure stores the
    model, and stage 6 then parks naming it rather than billing at a guess (decision 343)."""
    admin = await _admin(app)
    await _keyed(db)
    await _task(db, "jf:one")
    change = {"providers": {"gemini": {"model": "gemini-9-ultra"}}}

    body = await _preview(admin, change)
    assert (body["estimate"]["per_title_usd"], body["estimate"]["basis"]) == ("unknown", [])
    assert "gemini-9-ultra" in body["estimate"]["reason"]
    assert body["projected"]["monthly_usd"] == "unknown"
    assert body["projected"]["exceeds_remaining"] is None
    assert body["blocked"] is None

    confirmed = await admin.put(CONFIRM, json={**change, "accepted_estimate": "unknown"})
    assert confirmed.status_code == 200, confirmed.text
    card = next(c for c in confirmed.json()["providers"] if c["name"] == "gemini")
    assert (card["model"], card["price"], card["price_basis"]) == ("gemini-9-ultra", "unknown", "unknown")
    refusal = await spend.cap_check(db, title_id=7)
    assert refusal.kind == spend.PLAN and "gemini-9-ultra" in refusal.reason
