"""§6.6 Data's extraction queue over HTTP: the read, the quote and Launch. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from decimal import Decimal

import pytest

from spielplan.acquire import pipeline
from spielplan.flywheel import batch, store
from spielplan.llm import client, pricing, spend
from tests.test_acquire_pipeline import SHIPPED, STANDS_DOWN, _refuse_to_crawl, _stands_down
from tests.test_acquisition_board import _bootstrap
from tests.test_flywheel_feed import TITLE, _tag, _title, _vocabulary, _walk
from tests.test_flywheel_launch import BIG_CAP, A, B, _cap, _install

QUOTE = "/api/admin/flywheel/quote"


@pytest.fixture
def only_the_driver_is_live(monkeypatch):
    """Stages 2, 3, 4 and 6 advance without doing anything, and nothing can reach the open web."""
    monkeypatch.setattr(pipeline, "_default_fetcher", _refuse_to_crawl)
    monkeypatch.setattr(pipeline, "STAGES", tuple(
        _stands_down(stage) if stage.number in STANDS_DOWN else stage for stage in SHIPPED
    ))


async def _quote(admin, *, titles: int, providers: str, passes: int) -> dict:
    response = await admin.get(QUOTE, params={"titles": titles, "providers": providers, "passes": passes})
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_thin_facet_row_is_readable_from_the_route_the_moment_the_walk_returns(
    db, app, only_the_driver_is_live
):
    """No drain, job or sweep between the walk and the read: the observation commits inside the walk."""
    admin, _member = await _bootstrap(app)
    await _vocabulary(db)
    await _title(db)
    await _tag(db, "mood.bleak")
    await _tag(db, "themes.robots")
    assert (await admin.get("/api/admin/flywheel")).json()["items"] == []

    walk = await _walk(db)

    assert "project" in walk.stages_run, walk.as_dict()
    (row,) = (await admin.get("/api/admin/flywheel")).json()["items"]
    assert (row["kind"], row["title_id"], row["status"]) == (store.THIN_FACET, TITLE, "queued")
    assert row["reason"] == (await store.queue(db))[0]["reason"]
    assert row["reason"].startswith("1 of the 3 facets vocabulary v1 declares carry no extracted-tier")
    assert row["board"]["stage"] == 9 and row["est_titles"] == 1


async def test_the_queue_read_carries_the_rows_the_providers_the_defaults_and_the_meter(
    db, secrets_key, app
):
    """Money travels as strings: a JSON number is a binary float to the client."""
    admin, _member = await _bootstrap(app)
    queued = await _install(db)
    await _cap(db, BIG_CAP)

    payload = (await admin.get("/api/admin/flywheel")).json()

    assert set(payload) == {"items", "providers", "defaults", "meter", "input_tokens_assumed"}
    assert sorted(row["id"] for row in payload["items"]) == sorted(queued.values())
    cards = {card["name"]: card for card in payload["providers"]}
    assert list(cards) == list(client.PROVIDERS)
    assert (cards["gemini"]["configured"], cards["gemini"]["reason"]) == (True, None)
    assert (cards["anthropic"]["configured"], cards["anthropic"]["reason"]) == (True, None)
    assert cards["openai"]["configured"] is False
    assert cards["openai"]["reason"].startswith("no API key is configured for openai"), cards["openai"]
    assert payload["defaults"] == {"providers": ["gemini"], "passes": 1, "reason": None}
    assert payload["meter"]["cap_usd"] == "25" and payload["meter"]["spent_usd"] == "0"
    assert payload["meter"]["remaining_usd"] == "25"
    assert payload["input_tokens_assumed"] == pricing.SPEC_INPUT_TOKENS


async def test_the_defaults_are_null_beside_the_stored_plans_refusal(db, secrets_key, app):
    """No provider assigned means no default plan, and the sentence says why rather than guessing one."""
    admin, _member = await _bootstrap(app)

    defaults = (await admin.get("/api/admin/flywheel")).json()["defaults"]

    assert (defaults["providers"], defaults["passes"]) == (None, None)
    assert defaults["reason"].startswith("no extraction provider is assigned"), defaults


async def test_the_quote_doubles_when_the_passes_go_from_one_to_two(db, secrets_key, app):
    admin, _member = await _bootstrap(app)
    await _install(db)
    await _cap(db, BIG_CAP)

    one = await _quote(admin, titles=5, providers="gemini", passes=1)
    two = await _quote(admin, titles=5, providers="gemini", passes=2)
    both = await _quote(admin, titles=5, providers="gemini, anthropic", passes=1)

    money = ("per_title_usd", "total_usd", "reserved_usd")
    assert all(isinstance(one[key], str) for key in (*money, "cap_usd", "remaining_usd")), one
    assert Decimal(one["total_usd"]) == 5 * Decimal(one["per_title_usd"])
    assert Decimal(one["reserved_usd"]) == spend.ATTEMPTS * Decimal(one["total_usd"])
    assert all(Decimal(two[key]) == 2 * Decimal(one[key]) for key in money), (one, two)
    assert Decimal(both["per_title_usd"]) > Decimal(one["per_title_usd"])
    assert both["providers"] == ["gemini", "anthropic"]
    assert (one["launchable"], one["reason"], one["titles"], one["passes"]) == (True, None, 5, 1)


async def test_the_quote_over_the_cap_is_disabled_with_the_reason_naming_the_cap(db, secrets_key, app):
    admin, _member = await _bootstrap(app)
    await _install(db)
    await _cap(db, 0.01)

    quoted = await _quote(admin, titles=5, providers="gemini", passes=2)

    assert quoted["launchable"] is False
    assert quoted["reason"].startswith(f"{spend.OVER_CAP_PREFIX}: this batch reserves up to")
    assert "of the $0.01 monthly cap is left for" in quoted["reason"]


async def test_the_quote_refuses_a_count_or_a_pass_it_cannot_price(db, secrets_key, app):
    admin, _member = await _bootstrap(app)

    assert (await admin.get(QUOTE, params={"titles": 1, "providers": "gemini"})).status_code == 422
    assert (await admin.get(QUOTE, params={"titles": -1, "providers": "gemini", "passes": 1})
            ).status_code == 422
    assert (await admin.get(QUOTE, params={"titles": 1, "providers": "gemini", "passes": 0})
            ).status_code == 422


async def test_launch_answers_200_with_the_batch_and_409_with_the_domains_sentence(db, secrets_key, app):
    admin, _member = await _bootstrap(app)
    queued = await _install(db)
    await _cap(db, BIG_CAP)
    body = {"item_ids": [queued[A]], "providers": ["gemini"], "passes": 1}

    launched = await admin.post("/api/admin/flywheel/launch", json=body)

    assert launched.status_code == 200, launched.text
    made = launched.json()
    assert made["items"] == [{"id": queued[A], "title_id": A, "stage": 5}]
    assert made["batch"]["providers"] == ["gemini"] and made["batch"]["est_titles"] == 1
    assert Decimal(made["batch"]["reserved_usd"]) == 2 * Decimal(made["batch"]["est_cost_usd"])
    assert await db.fetchval("SELECT batch_id FROM flywheel_item WHERE id = $1", queued[A]) == (
        made["batch"]["id"]
    )

    again = await admin.post("/api/admin/flywheel/launch", json=body)
    assert again.status_code == 409
    assert again.json()["detail"].startswith(f"row {queued[A]} is running and not queued")

    await _cap(db, 0.000001)
    over = await admin.post("/api/admin/flywheel/launch",
                            json={"item_ids": [queued[B]], "providers": ["gemini"], "passes": 1})
    assert over.status_code == 409
    assert over.json()["detail"].startswith(spend.OVER_CAP_PREFIX)
    assert await db.fetchval("SELECT status FROM flywheel_item WHERE id = $1", queued[B]) == "queued"
    assert await db.fetchval("SELECT count(*) FROM flywheel_batch") == 1


async def test_a_launch_body_carries_no_figure_a_page_could_hold_the_cap_with(db, secrets_key, app):
    """Decision 441: the server is the gate; a figure a page sends is dropped unread."""
    admin, _member = await _bootstrap(app)
    queued = await _install(db)

    refused = await admin.post("/api/admin/flywheel/launch", json={
        "item_ids": [queued[A]], "providers": ["gemini"], "passes": 1, "reserved_usd": "0.00",
    })

    assert refused.status_code == 409
    assert refused.json()["detail"] == spend.NO_CAP_REASON
    assert await db.fetchval("SELECT count(*) FROM flywheel_batch") == 0


async def test_a_batch_naming_no_provider_asks_for_one_and_never_for_a_relaunch(db, secrets_key, app):
    """Decision 442 refuses a batch naming no provider with the picker's sentence, not a relaunch's."""
    admin, _member = await _bootstrap(app)
    queued = await _install(db)

    uncapped = await _quote(admin, titles=2, providers="", passes=1)
    assert (uncapped["launchable"], uncapped["reason"]) == (False, spend.NO_CAP_REASON)

    await _cap(db, BIG_CAP)
    quoted = await _quote(admin, titles=2, providers="", passes=1)
    assert quoted["launchable"] is False
    assert "launched in" not in quoted["reason"], quoted["reason"]
    assert quoted["reason"] == batch.NO_PROVIDER
    assert (quoted["providers"], quoted["per_title_usd"], quoted["reserved_usd"]) == ([], None, None)

    launched = await admin.post("/api/admin/flywheel/launch",
                                json={"item_ids": [queued[A]], "providers": [], "passes": 1})
    assert launched.status_code == 409
    assert launched.json()["detail"] == quoted["reason"]
    assert await db.fetchval("SELECT status FROM flywheel_item WHERE id = $1", queued[A]) == "queued"
    assert await db.fetchval("SELECT count(*) FROM flywheel_batch") == 0


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/admin/flywheel", None),
        ("GET", "/api/admin/flywheel/quote?titles=1&providers=gemini&passes=1", None),
        ("POST", "/api/admin/flywheel/launch", {"item_ids": [1], "providers": ["gemini"], "passes": 1}),
    ],
)
async def test_every_flywheel_route_refuses_a_member_and_a_stranger(app, db, method, path, body):
    _admin, member = await _bootstrap(app)
    kwargs = {} if body is None else {"json": body}

    assert (await member.request(method, path, **kwargs)).status_code == 403
    assert (await app().request(method, path, **kwargs)).status_code == 401


def test_the_router_declares_exactly_the_three_flywheel_paths():
    """From the router's own table, so a renamed route fails here and not only in the harness comparison."""
    from spielplan.api import flywheel as flywheel_api

    assert {route.path for route in flywheel_api.router.routes} == {
        "/api/admin/flywheel", "/api/admin/flywheel/quote", "/api/admin/flywheel/launch"
    }
