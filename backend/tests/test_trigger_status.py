"""The last `ItemAdded` is not the last delivery (a wrong template delivers `PlaybackStart`), and
the poll's newest run and newest success are separate facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spielplan.acquire import intake
from spielplan.api import admin as admin_api
from spielplan.connectors.registry import save_jellyfin
from tests.helpers import admin_client

JELLYFIN_URL = "http://jellyfin.test"
POLL = "jellyfin-delta-poll"

# jellyfin-plugin-webhook#204's example id, undashed, as `/Items` spells a GUID.
GUID_MOVIE = "6213b704a0d954293110f4d561b0f614"
GUID_OTHER = "0f0e0d0c0b0a49088706050403020100"
GUID_OLD = "11111111222233334444555555555555"


def _added(item_id: str, **extra: object) -> dict[str, object]:
    return {"NotificationType": "ItemAdded", "ItemId": item_id, "ItemType": "Movie", **extra}


async def _aged(db, row_id: int, ago: timedelta) -> datetime:
    """Move one intake row's arrival back, returning the instant it now says it arrived."""
    return await db.fetchval(
        "UPDATE jellyfin_intake SET received_at = now() - $2::interval WHERE id = $1"
        " RETURNING received_at",
        row_id, ago,
    )


async def _run(db, *, ok: bool | None, ago: timedelta, detail=None) -> tuple[datetime, datetime]:
    """One delta-poll `job_run` row, as `worker._record_start`/`_record_finish` leave it."""
    started = datetime.now(UTC) - ago
    finished = started + timedelta(seconds=4)
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok, detail) VALUES ($1, $2, $3, $4, $5)",
        POLL, started, None if ok is None else finished, ok, detail,
    )
    return started, finished


def _close(stamp: str | datetime | None, expected: datetime) -> bool:
    """The route spells instants as ISO strings and the table as timestamptz; equal to the
    microsecond, which is what a row read back through two encoders should be."""
    assert stamp is not None
    value = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    return abs(value - expected) < timedelta(milliseconds=1)


async def test_the_webhook_status_names_the_last_item_added_not_the_last_delivery(db):
    """The newest delivery is a refusal and the one before a `PlaybackStart`; the add eight days ago
    is outside the week's count."""
    pending = await intake.record_event(db, _added(GUID_OTHER))
    enqueued = await intake.record_event(db, _added(GUID_MOVIE))
    playback = await intake.record_event(db, {"NotificationType": "PlaybackStart",
                                              "ItemId": GUID_MOVIE, "ItemType": "Movie"})
    refused = await intake.record_refusal(db, intake.PAYLOAD_TOO_LARGE)
    old = await intake.record_event(db, _added(GUID_OLD))
    assert (pending.state, playback.state, playback.reason) == (
        intake.PENDING, intake.SKIPPED, intake.NOT_ITEM_ADDED
    )
    await db.execute("UPDATE jellyfin_intake SET state = 'enqueued' WHERE id = $1", enqueued.id)

    await _aged(db, pending.id, timedelta(hours=2))
    added_at = await _aged(db, enqueued.id, timedelta(hours=1))
    await _aged(db, playback.id, timedelta(minutes=30))
    refused_at = await _aged(db, refused.id, timedelta(minutes=10))
    await _aged(db, old.id, timedelta(days=8))

    webhook = (await intake.trigger_status(db, poll_job=POLL, watermark=None))["webhook"]

    assert _close(webhook["last_item_added_at"], added_at)
    assert _close(webhook["last_delivery_at"], refused_at)
    assert webhook["deliveries_7d"] == 4
    assert _close(webhook["last_refusal"]["at"], refused_at)
    assert webhook["last_refusal"]["reason"] == intake.PAYLOAD_TOO_LARGE


async def test_a_trigger_that_only_ever_received_the_wrong_template_has_no_item_added(db):
    """Every delivery arrived and none was an `ItemAdded`: no last add at all."""
    await intake.record_event(db, {"NotificationType": "PlaybackStop", "ItemId": GUID_MOVIE})
    await intake.record_event(db, [1, 2, 3])
    await intake.record_refusal(db, intake.DELIVERY_INTERRUPTED)

    webhook = (await intake.trigger_status(db, poll_job=POLL, watermark=None))["webhook"]

    assert webhook["last_item_added_at"] is None
    assert webhook["last_delivery_at"] is not None
    assert webhook["deliveries_7d"] == 3
    assert webhook["last_refusal"]["reason"] == intake.DELIVERY_INTERRUPTED


async def test_the_delta_poll_status_is_its_watermark_its_newest_run_and_its_newest_success(db):
    """Decision 455. The two rows differ only once the poll starts failing, so it is asked twice."""
    watermark = datetime(2026, 9, 24, 7, 55, tzinfo=UTC)
    good_started, good_finished = await _run(db, ok=True, ago=timedelta(minutes=20),
                                             detail={"read": 5, "enqueued": 3})

    first = (await intake.trigger_status(db, poll_job=POLL, watermark=watermark))["delta_poll"]

    assert first["watermark"] == watermark
    assert _close(first["last_run_at"], good_started)
    assert first["last_run_ok"] is True
    assert first["last_filed"] == 3
    assert _close(first["last_ok_at"], good_finished)

    failed_started, _ = await _run(db, ok=False, ago=timedelta(minutes=5),
                                   detail={"error": "JellyfinError: GET /Items -> 400"})

    second = (await intake.trigger_status(db, poll_job=POLL, watermark=watermark))["delta_poll"]

    assert _close(second["last_run_at"], failed_started)
    assert second["last_run_ok"] is False
    assert second["last_filed"] is None
    assert _close(second["last_ok_at"], good_finished)


async def test_a_poll_still_running_is_neither_ok_nor_failed(db):
    """A row with no outcome (in flight, or SIGKILLed) is neither ok nor failed."""
    await _run(db, ok=True, ago=timedelta(minutes=20), detail={"enqueued": 0})
    running, _ = await _run(db, ok=None, ago=timedelta(seconds=30))

    poll = (await intake.trigger_status(db, poll_job=POLL, watermark=None))["delta_poll"]

    assert _close(poll["last_run_at"], running)
    assert poll["last_run_ok"] is None
    assert poll["last_filed"] is None
    assert poll["last_ok_at"] is not None


async def test_an_unconfigured_install_answers_the_trigger_with_nulls_and_zeros(secrets_key, db, app):
    """A half-configured boot is legal (§3.1), so this is an answer, never a 500."""
    admin = await admin_client(app)

    got = await admin.get("/api/admin/connectors/jellyfin")

    assert got.status_code == 200, got.text
    assert got.json()["configured"] is False
    assert got.json()["trigger"] == {
        "webhook": {"last_item_added_at": None, "last_delivery_at": None, "deliveries_7d": 0,
                    "last_refusal": None},
        "delta_poll": {"watermark": None, "last_run_at": None, "last_run_ok": None,
                       "last_ok_at": None, "last_filed": None},
    }


async def test_the_connector_card_carries_the_trigger_and_never_its_credentials(secrets_key, db, app):
    """Neither the key nor the webhook token appears in the body (§14.3, decision 332)."""
    admin = await admin_client(app)
    saved = await admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": "JF-ADMIN-KEY-UNSCOPED-7731",
              "mint_webhook_token": True},
    )
    assert saved.status_code == 200, saved.text
    token = saved.json()["webhook_token"]
    assert token
    watermark = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)
    await save_jellyfin(db, delta_watermark=watermark, watermark_origin=JELLYFIN_URL)
    added = await intake.record_event(db, _added(GUID_MOVIE))
    await _run(db, ok=True, ago=timedelta(minutes=12), detail={"enqueued": 1})

    got = await admin.get("/api/admin/connectors/jellyfin")

    assert got.status_code == 200, got.text
    body = got.json()
    assert {"url", "has_api_key", "configured", "library_ids",
            "secrets_unreadable", "has_webhook_token", "server_version",
            "server_supported"} < set(body)
    assert body["has_webhook_token"] is True
    trigger = body["trigger"]
    assert _close(trigger["webhook"]["last_item_added_at"],
                  await db.fetchval("SELECT received_at FROM jellyfin_intake WHERE id = $1",
                                    added.id))
    assert trigger["webhook"]["deliveries_7d"] == 1
    assert datetime.fromisoformat(trigger["delta_poll"]["watermark"]) == watermark
    assert trigger["delta_poll"]["last_run_ok"] is True
    assert trigger["delta_poll"]["last_filed"] == 1
    assert "JF-ADMIN-KEY-UNSCOPED-7731" not in got.text
    assert token not in got.text


def test_the_card_asks_for_the_poll_the_worker_registers():
    """The name is the worker registry's (`JOB_NAMES`); a drifted one reads as a poll that never ran."""
    assert admin_api.DELTA_POLL_JOB == POLL
    assert admin_api.DELTA_POLL_JOB in admin_api.JOB_NAMES
