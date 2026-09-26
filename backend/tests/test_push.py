from __future__ import annotations

import logging

import pytest

from spielplan.api import push

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-real-member-password"

# Shaped like `PushSubscription.toJSON()`, which the client posts unchanged.
PHONE = {
    "endpoint": "https://push.example.test/f/jenny-phone-1",
    "keys": {"p256dh": "BJ-test-public-key", "auth": "test-auth-secret"},
    "device_label": "Jenny's iPhone",
}
LAPTOP = {
    "endpoint": "https://push.example.test/f/jenny-laptop-9",
    "keys": {"p256dh": "BJ-other-public-key", "auth": "other-auth-secret"},
    "device_label": "Laptop",
}


@pytest.fixture
async def household(app, db):
    """The member's forced password change is done here: §3.1 locks the account until it is."""
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201
    admin_id = (await admin.get("/api/auth/me")).json()["id"]

    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    otp = made.json()["one_time_password"]

    member = app()
    await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    await member.post(
        "/api/auth/password",
        json={"current_password": otp, "new_password": MEMBER_PASSWORD},
    )
    member_id = (await member.get("/api/auth/me")).json()["id"]
    return admin, admin_id, member, member_id


async def _count(db) -> int:
    return await db.fetchval("SELECT count(*) FROM push_subscription")


async def test_granting_push_permission_stores_exactly_one_subscription(household, db):
    _admin, _admin_id, member, member_id = household
    stored = await member.post("/api/push/subscribe", json=PHONE)
    assert stored.status_code == 201

    row = await db.fetchrow("SELECT * FROM push_subscription")
    assert await _count(db) == 1
    assert row["user_id"] == member_id
    assert row["endpoint"] == PHONE["endpoint"]
    assert row["p256dh"] == PHONE["keys"]["p256dh"]
    assert row["auth"] == PHONE["keys"]["auth"]
    assert row["device_label"] == "Jenny's iPhone"


async def test_resubscribing_the_same_endpoint_updates_the_row_rather_than_adding_a_second(
    household, db
):
    """A phone re-registers on every app update; an insert would multiply every prompt."""
    _admin, _admin_id, member, _member_id = household
    first = (await member.post("/api/push/subscribe", json=PHONE)).json()

    rotated = {**PHONE, "keys": {"p256dh": "BJ-rotated", "auth": "rotated-auth"}}
    second = (await member.post("/api/push/subscribe", json=rotated)).json()

    assert await _count(db) == 1
    assert second["id"] == first["id"], "the same device must keep its row"
    row = await db.fetchrow("SELECT p256dh, auth, device_label FROM push_subscription")
    assert (row["p256dh"], row["auth"]) == ("BJ-rotated", "rotated-auth")
    # The label was not resent; it must survive.
    assert row["device_label"] == "Jenny's iPhone"


async def test_resubscribing_keeps_the_delivery_mark_so_the_nightly_prune_spares_a_live_phone(
    household, db
):
    """The prune reads `COALESCE(last_seen_ok, created_at)`; a re-post that reset `last_seen_ok`
    put a live phone back on `created_at`, and the 90-day prune deleted it."""
    from spielplan import worker

    _admin, _admin_id, member, _member_id = household
    await member.post("/api/push/subscribe", json=PHONE)
    await db.execute(
        "UPDATE push_subscription SET created_at = now() - interval '400 days', "
        "last_seen_ok = now() - interval '1 day'"
    )

    await member.post("/api/push/subscribe", json=PHONE)
    kept = await db.fetchval("SELECT last_seen_ok FROM push_subscription")
    assert kept is not None, "the re-post must not erase the only clock that spares this row"

    await worker._prune_dead_push_subscriptions()
    assert await _count(db) == 1, (
        "opening /account is not evidence that a phone is dead -- §4.2 lets a 404/410 take a "
        "device, and this prune take one that has been silent for ninety days, and nothing else"
    )


async def test_resubscribing_a_device_that_never_received_a_push_spares_it_too(household, db):
    """A household that never sent a push keeps `last_seen_ok` NULL, so a re-post must move the
    prune's clock too. A conflicting endpoint IS the live device."""
    from spielplan import worker

    _admin, _admin_id, member, _member_id = household
    await member.post("/api/push/subscribe", json=PHONE)
    await db.execute(
        "UPDATE push_subscription SET created_at = now() - interval '91 days', last_seen_ok = NULL"
    )

    stored = await member.post("/api/push/subscribe", json=PHONE)
    assert stored.status_code == 201, stored.text

    await worker._prune_dead_push_subscriptions()
    assert await _count(db) == 1, (
        "a device that re-registered this morning is not one that has been silent for ninety days"
    )
    assert await db.fetchval("SELECT last_seen_ok FROM push_subscription") is None, (
        "and it is still true that nothing was ever delivered -- that column means a delivery"
    )


async def test_two_devices_for_one_member_are_two_rows(household, db):
    _admin, _admin_id, member, member_id = household
    await member.post("/api/push/subscribe", json=PHONE)
    await member.post("/api/push/subscribe", json=LAPTOP)

    assert await _count(db) == 2
    assert await db.fetchval("SELECT count(DISTINCT user_id) FROM push_subscription") == 1
    listed = (await member.get("/api/push/state")).json()["subscriptions"]
    assert [s["device_label"] for s in listed] == ["Jenny's iPhone", "Laptop"]
    assert all(s["last_seen_ok"] is None for s in listed)
    assert await db.fetchval("SELECT count(*) FROM push_subscription WHERE user_id = $1",
                             member_id) == 2


# §14.3: `push/send.py` POSTs to the stored string verbatim, with the VAPID JWT attached.


async def test_a_plain_http_endpoint_is_refused_and_stores_nothing(household, db):
    """The push service is always https; §2's plain-HTTP port is about the app, not the endpoint."""
    _admin, _admin_id, member, _member_id = household
    refused = await member.post(
        "/api/push/subscribe", json={**PHONE, "endpoint": "http://push.example.test/f/x"}
    )

    assert refused.status_code == 422
    assert "https" in refused.text, "the reason has to be readable by whoever sees the log"
    assert await _count(db) == 0


async def test_an_endpoint_on_this_machine_or_the_private_network_is_refused(household, db):
    """IPv6 forms because `urlsplit` strips the brackets; `localhost.` resolves like `localhost`.
    100.64.0.0/10 (Tailscale) left `is_private` in 3.12.4, hence `is_global`."""
    _admin, _admin_id, member, _member_id = household
    for host in (
        "127.0.0.1", "localhost", "localhost.", "LocalHost", "[::1]",
        "192.168.1.9", "10.0.0.4", "172.16.4.4", "169.254.169.254", "[fd00::1]", "0.0.0.0",
        "100.64.0.1", "100.101.102.103",
    ):
        refused = await member.post(
            "/api/push/subscribe", json={**PHONE, "endpoint": f"https://{host}/f/x"}
        )
        assert refused.status_code == 422, f"{host}: {refused.text}"
        assert await _count(db) == 0, f"{host} was stored"


async def test_an_endpoint_naming_the_households_own_servers_is_refused(household, db, monkeypatch):
    """Written without secrets: a boot whose DEK will not open is legal, so the URL is read
    straight from `connector_config`."""
    from spielplan.core.config import settings

    _admin, _admin_id, member, _member_id = household
    # A dict, not a JSON string: the `db` fixture's jsonb codec would store a JSON *string*.
    await db.execute(
        "INSERT INTO connector_config (name, config) VALUES ('jellyfin', $1::jsonb)",
        {"url": "https://jellyfin.example.test:8096"},
    )
    refused = await member.post(
        "/api/push/subscribe",
        json={**PHONE, "endpoint": "https://jellyfin.example.test/Sessions"},
    )
    assert refused.status_code == 422, refused.text
    assert await _count(db) == 0

    monkeypatch.setenv("PUBLIC_URL", "https://spielplan.example.tld")
    settings.cache_clear()
    try:
        ours = await member.post(
            "/api/push/subscribe",
            json={**PHONE, "endpoint": "https://spielplan.example.tld/api/push/subscribe"},
        )
    finally:
        settings.cache_clear()
    assert ours.status_code == 422, ours.text
    assert await _count(db) == 0


async def test_an_ordinary_push_service_endpoint_is_still_stored_exactly_once(household, db):
    _admin, _admin_id, member, _member_id = household
    stored = await member.post("/api/push/subscribe", json=PHONE)

    assert stored.status_code == 201, stored.text
    assert await _count(db) == 1
    assert await db.fetchval("SELECT endpoint FROM push_subscription") == PHONE["endpoint"]


async def test_a_subscription_belongs_to_the_member_who_granted_it(household, db):
    admin, _admin_id, member, member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    assert await db.fetchval("SELECT user_id FROM push_subscription") == member_id
    assert (await admin.get("/api/push/state")).json()["subscriptions"] == []
    assert len((await member.get("/api/push/state")).json()["subscriptions"]) == 1


async def test_one_member_cannot_unsubscribe_anothers_device(household, db):
    """An endpoint string travels (shared browsers, bug reports), so the DELETE is scoped by user too."""
    admin, _admin_id, member, member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    refused = await admin.request(
        "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
    )
    assert refused.status_code == 404
    assert await _count(db) == 1
    assert await db.fetchval("SELECT user_id FROM push_subscription") == member_id


async def test_a_phone_handed_to_another_member_moves_rather_than_duplicating(household, db):
    """An endpoint is minted per browser profile, so it moves with the member signed in there."""
    admin, admin_id, member, _member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    moved = await admin.post("/api/push/subscribe", json=PHONE)
    assert moved.status_code == 201
    assert await _count(db) == 1
    assert await db.fetchval("SELECT user_id FROM push_subscription") == admin_id
    assert (await member.get("/api/push/state")).json()["subscriptions"] == []


async def test_unsubscribing_removes_the_device(household, db):
    _admin, _admin_id, member, _member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    gone = await member.request(
        "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
    )
    assert gone.status_code == 200
    assert gone.json()["subscriptions"] == []
    assert await _count(db) == 0


async def test_the_push_routes_refuse_a_caller_with_no_session(app):
    anonymous = app()
    assert (await anonymous.get("/api/push/state")).status_code == 401
    assert (await anonymous.post("/api/push/subscribe", json=PHONE)).status_code == 401
    assert (
        await anonymous.request(
            "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
        )
    ).status_code == 401


async def test_declining_stores_nothing_and_still_completes_onboarding(household, db):
    """Treating "declined" as "unfinished" blocks the wizard forever."""
    _admin, _admin_id, member, member_id = household
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is False

    done = await member.post("/api/setup/onboarding/complete")
    assert done.status_code == 200

    assert await _count(db) == 0
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True
    detail = await db.fetchval("SELECT detail FROM setup_step WHERE step = 'onboarding'")
    assert detail == {str(member_id): True}


async def test_onboarding_is_recorded_per_member_so_the_other_phone_is_still_asked(household):
    """Install and push permission are per-device acts (§3.1)."""
    admin, _admin_id, member, _member_id = household
    await member.post("/api/setup/onboarding/complete")

    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True
    assert (await admin.get("/api/push/state")).json()["onboarding_complete"] is False

    await admin.post("/api/setup/onboarding/complete")
    assert (await admin.get("/api/push/state")).json()["onboarding_complete"] is True
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True


async def test_the_endpoint_and_auth_key_never_come_back_out_of_the_api(household):
    """The endpoint is a bearer capability and `auth` the encryption key; the UI gets a hash."""
    _admin, _admin_id, member, _member_id = household
    stored = (await member.post("/api/push/subscribe", json=PHONE)).text
    listed = (await member.get("/api/push/state")).text

    for leak in (PHONE["endpoint"], PHONE["keys"]["auth"], PHONE["keys"]["p256dh"]):
        assert leak not in stored
        assert leak not in listed
    assert push.device_handle(PHONE["endpoint"]) in listed


async def test_the_endpoint_and_auth_key_never_reach_the_log(household, caplog):
    _admin, _admin_id, member, _member_id = household
    with caplog.at_level(logging.DEBUG):
        await member.post("/api/push/subscribe", json=PHONE)
        await member.request(
            "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
        )

    assert PHONE["endpoint"] not in caplog.text
    assert PHONE["keys"]["auth"] not in caplog.text
    assert PHONE["keys"]["p256dh"] not in caplog.text
    # The device handle is what an operator correlates on instead.
    assert push.device_handle(PHONE["endpoint"]) in caplog.text


def test_the_device_handle_is_stable_and_does_not_contain_the_endpoint():
    handle = push.device_handle(PHONE["endpoint"])
    assert handle == push.device_handle(PHONE["endpoint"])
    assert handle != push.device_handle(LAPTOP["endpoint"])
    assert len(handle) == 12
    assert PHONE["endpoint"] not in handle


async def test_the_state_route_reports_no_application_server_key_until_one_is_configured(
    household, db
):
    """Chrome refuses `subscribe()` without a key, so null means "not configured", not "denied".
    The pair is generated at first boot and is absent without SECRETS_KEY."""
    _admin, _admin_id, member, _member_id = household
    stored = await db.fetchval(
        "SELECT value ->> 'public_key' FROM app_setting WHERE key = 'push.vapid'"
    )
    assert (await member.get("/api/push/state")).json()["vapid_public_key"] == stored

    await db.execute("DELETE FROM app_setting WHERE key = 'push.vapid'")
    assert (await member.get("/api/push/state")).json()["vapid_public_key"] is None
