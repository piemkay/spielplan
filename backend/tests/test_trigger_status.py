"""§6.6's "webhook status", as the facts of what arrived rather than a mode somebody set.

Spec v2.1 §6.6 (Connectors: "Jellyfin (URL, API key, library pick, user-mapping table, test button,
sync now, webhook status)"), §7.2; decisions 332, 365, 409, 455; proposal 106's sketch;
docs/milestones/M5.7-plan.md Phase D2 ("Do not invent a mode flag nobody sets; report both facts and
let the operator read which path is working").

§7.2 gives Jellyfin two ways to tell this app about an add: the webhook is the trigger and the
fifteen-minute delta poll the fallback, and both end in the same enqueue (decision 366). From
Tonight the two are indistinguishable from each other and from a household that added nothing, so
the card's status is the only place an operator can read which of them is carrying the load. That
makes each fact load-bearing in one direction:

  * the last `ItemAdded` is NOT the last delivery. A Webhook plugin pointed at the wrong template
    delivers `PlaybackStart` bodies every evening, and a status that counted those would report a
    healthy trigger on an install whose trigger has never once fired;
  * a refusal is shown with its reason, because decision 365 records what it cannot act on and
    this card is where the operator reads the record;
  * the delta poll's newest run and its newest success are two rows on the install that needs
    reporting, the one whose poll has been failing since Tuesday.

The rows are written the way the handler writes them (`intake.record_event`, `record_refusal`) and
aged with an UPDATE, because the status reads rows and not payloads: which bodies become which rows
is `test_jellyfin_intake.py`'s, against the double's own template. Skipped without
TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spielplan.acquire import intake
from spielplan.api import admin as admin_api
from spielplan.connectors.registry import save_jellyfin

ADMIN_PASSWORD = "an-admin-password"
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


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


def _close(stamp: str | datetime | None, expected: datetime) -> bool:
    """The route spells instants as ISO strings and the table as timestamptz; equal to the
    microsecond, which is what a row read back through two encoders should be."""
    assert stamp is not None
    value = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    return abs(value - expected) < timedelta(milliseconds=1)


# --- the webhook's facts ------------------------------------------------------------------------


async def test_the_webhook_status_names_the_last_item_added_not_the_last_delivery(db):
    """Five deliveries across eight days, each written by the handler's own two writers.

    The newest delivery is a refusal and the one before it a `PlaybackStart`, and neither is an
    `ItemAdded`: the status names the enqueued add an hour ago as the last one, the refusal as the
    last delivery and the newest refusal, and counts four deliveries in the week - the add from
    eight days ago is history, not this week's traffic.
    """
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
    """The misconfigured plugin, which is the reason the two instants are two keys.

    Every delivery arrived and none was an `ItemAdded`: the card has a last delivery, a refusal
    naming why, and no last add at all - which is the true and actionable reading ("the plugin is
    sending, but not the template §7.2 reads").
    """
    await intake.record_event(db, {"NotificationType": "PlaybackStop", "ItemId": GUID_MOVIE})
    await intake.record_event(db, [1, 2, 3])
    await intake.record_refusal(db, intake.DELIVERY_INTERRUPTED)

    webhook = (await intake.trigger_status(db, poll_job=POLL, watermark=None))["webhook"]

    assert webhook["last_item_added_at"] is None
    assert webhook["last_delivery_at"] is not None
    assert webhook["deliveries_7d"] == 3
    assert webhook["last_refusal"]["reason"] == intake.DELIVERY_INTERRUPTED


# --- the delta poll's facts ---------------------------------------------------------------------


async def test_the_delta_poll_status_is_its_watermark_its_newest_run_and_its_newest_success(db):
    """Decision 455: the watermark, when the poll last ran, whether that run was ok, how many
    titles it filed, and when it last succeeded.

    Asked twice, because the two rows only differ once the poll starts failing. With one good run
    the newest run and the newest success are the same row and it filed three; with a newer
    failure the newest run is the failure - which filed nothing it could report - while the last
    success still names the good run, so the operator can read how long the fallback has been down.
    """
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
    """`worker._record_start` writes the row before the call, so a poll in flight - or one a
    SIGKILL ended - is a row with no outcome, and the status says so rather than calling it a
    failure (the System card's own rule, `test_admin_system.py`)."""
    await _run(db, ok=True, ago=timedelta(minutes=20), detail={"enqueued": 0})
    running, _ = await _run(db, ok=None, ago=timedelta(seconds=30))

    poll = (await intake.trigger_status(db, poll_job=POLL, watermark=None))["delta_poll"]

    assert _close(poll["last_run_at"], running)
    assert poll["last_run_ok"] is None
    assert poll["last_filed"] is None
    assert poll["last_ok_at"] is not None


# --- over HTTP ------------------------------------------------------------------------------------


async def test_an_unconfigured_install_answers_the_trigger_with_nulls_and_zeros(secrets_key, db, app):
    """§3.1's half-configured boot is legal, and the connector card is the screen an admin sets it
    up from, so the status of a trigger nobody has configured is an answer and never a 500."""
    admin = await _admin(app)

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
    """The card's GET gains `trigger` and keeps every key it had.

    The key and the webhook token are both in hand while this runs - the token from the one save
    that mints it (decision 418) - and neither appears anywhere in the body: the status reports
    that a delivery arrived and what became of it, never the header it arrived with (§14.3,
    decision 332). The poll's watermark is the stored one, which the card reads off the same
    `load_jellyfin` its other keys come from.
    """
    admin = await _admin(app)
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
    assert {"url", "has_api_key", "configured", "library_ids", "linked_users",
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
    """`api/admin.py` passes the job's name in rather than `intake.py` spelling it, because the name
    is the worker registry's and `JOB_NAMES` is where the web process spells it (pinned to the
    registry by `test_worker_registry.py`). A drifted name here is a poll the card reports as never
    having run."""
    assert admin_api.DELTA_POLL_JOB == POLL
    assert admin_api.DELTA_POLL_JOB in admin_api.JOB_NAMES
