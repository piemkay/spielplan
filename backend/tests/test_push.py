"""Push subscriptions, over HTTP, against a real database. Spec v2.1 §4.2, §6 preamble, §7.3.

§4.2 shipped the `push_subscription` table in M0 and the worker has been pruning it ever
since; M2 owes the write that fills it, because §7.3's "when undeliverable, the prompt queues
and surfaces as an in-app banner" only means something once there is a deliverable path.

The four properties asserted here are the ones that go wrong quietly rather than loudly:

  * a device that re-registers must not become two devices (§4.2's UNIQUE endpoint);
  * a subscription is the *member's*, so the other phone in the household never receives it;
  * declining stores nothing and still completes §3.1's fifth step, or the wizard blocks
    forever;
  * the endpoint and the auth key are secrets — a push endpoint is a bearer capability — so
    they leave neither in a response body nor in a log line.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import logging

import pytest

from spielplan.api import push

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-real-member-password"

# A subscription shaped exactly like `PushSubscription.toJSON()` — that object is what the
# browser hands the client and what the client posts unchanged.
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
def push_router_registered(monkeypatch):
    """`api/push.py` exports `router`; including it is `spielplan/app.py`'s job.

    That file is registered by another hand this milestone, so wrap `create_app` rather than
    edit it. The wrap is idempotent — once the include lands, this does nothing at all.
    """
    import spielplan.app as app_module

    original = app_module.create_app

    def create():
        application = original()
        if not any(getattr(r, "path", "").startswith("/api/push") for r in application.routes):
            application.include_router(push.router)
        return application

    monkeypatch.setattr(app_module, "create_app", create)


@pytest.fixture
async def household(push_router_registered, app, db):
    """§3.1's household: an admin and a member, each on their own cookie jar.

    The member's forced first-login password change is done here, because §3.1 locks the
    account until it is and onboarding (§6 preamble) is the step *after* it.
    """
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201
    admin_id = (await admin.get("/api/auth/me")).json()["id"]

    made = await admin.post("/api/setup/members", json={"name": "jenny", "role": "member"})
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


# --- §4.2: one row per device ---------------------------------------------------------------


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
    """§4.2 makes `endpoint` UNIQUE, and a phone re-registers its service worker on every app
    update. If that inserted, one phone would collect a row per release and every §7.3 prompt
    would arrive n times."""
    _admin, _admin_id, member, _member_id = household
    first = (await member.post("/api/push/subscribe", json=PHONE)).json()

    rotated = {**PHONE, "keys": {"p256dh": "BJ-rotated", "auth": "rotated-auth"}}
    second = (await member.post("/api/push/subscribe", json=rotated)).json()

    assert await _count(db) == 1
    assert second["id"] == first["id"], "the same device must keep its row"
    row = await db.fetchrow("SELECT p256dh, auth, device_label FROM push_subscription")
    assert (row["p256dh"], row["auth"]) == ("BJ-rotated", "rotated-auth")
    # The label was not resent; it must survive rather than be blanked by the re-registration.
    assert row["device_label"] == "Jenny's iPhone"


async def test_resubscribing_clears_the_stale_delivery_mark(household, db):
    """§4.2: subscriptions are "pruned on 404/410 from the push service", and the worker's
    90-day sweep reads `last_seen_ok`. A device that just re-registered has delivered nothing
    yet — leaving the old timestamp there would let the sweep delete a live phone."""
    _admin, _admin_id, member, _member_id = household
    await member.post("/api/push/subscribe", json=PHONE)
    await db.execute("UPDATE push_subscription SET last_seen_ok = now() - interval '200 days'")

    await member.post("/api/push/subscribe", json=PHONE)
    assert await db.fetchval("SELECT last_seen_ok FROM push_subscription") is None


async def test_two_devices_for_one_member_are_two_rows(household, db):
    """§3.2's "multiple passkeys per user (phone + desktop)" has its counterpart here: the
    person, not the device, is the account — but each device is its own push target."""
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


# --- §4.2 / §7.3: the subscription is the member's, never the household's -------------------


async def test_a_subscription_belongs_to_the_member_who_granted_it(household, db):
    """§7.3 arms a *per-user* prompt. A household-scoped subscription would send Jenny's
    "did you finish X?" to Patrick's phone — the one failure mode that cannot be undone by
    tapping "no"."""
    admin, _admin_id, member, member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    assert await db.fetchval("SELECT user_id FROM push_subscription") == member_id
    assert (await admin.get("/api/push/state")).json()["subscriptions"] == []
    assert len((await member.get("/api/push/state")).json()["subscriptions"]) == 1


async def test_one_member_cannot_unsubscribe_anothers_device(household, db):
    """The DELETE is scoped by user_id as well as endpoint. Without that, an endpoint string —
    which travels, in a shared browser or a pasted bug report — is enough to silence somebody
    else's phone."""
    admin, _admin_id, member, member_id = household
    await member.post("/api/push/subscribe", json=PHONE)

    refused = await admin.request(
        "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
    )
    assert refused.status_code == 404
    assert await _count(db) == 1
    assert await db.fetchval("SELECT user_id FROM push_subscription") == member_id


async def test_a_phone_handed_to_another_member_moves_rather_than_duplicating(household, db):
    """§4.2's endpoint is UNIQUE across the household, and an endpoint is minted per browser
    profile: the only way an existing one arrives under a different member is that the same
    browser is now signed in as that member. The notification must follow the person holding
    the phone, so the row moves — and the previous owner's list loses it."""
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


async def test_the_push_routes_refuse_a_caller_with_no_session(push_router_registered, app):
    """A push endpoint is a bearer capability; an unauthenticated write would let anyone
    register a target that then receives a member's §7.3 prompts."""
    anonymous = app()
    assert (await anonymous.get("/api/push/state")).status_code == 401
    assert (await anonymous.post("/api/push/subscribe", json=PHONE)).status_code == 401
    assert (
        await anonymous.request(
            "DELETE", "/api/push/subscription", json={"endpoint": PHONE["endpoint"]}
        )
    ).status_code == 401


# --- §3.1 fifth step / §6 preamble: declining is a completion --------------------------------


async def test_declining_stores_nothing_and_still_completes_onboarding(household, db):
    """§6's preamble makes onboarding a guided act, and §3.1 makes it the wizard's fifth step.
    Someone who says no to notifications has finished that step — treating "declined" as
    "unfinished" is what blocks the wizard forever."""
    _admin, _admin_id, member, member_id = household
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is False

    done = await member.post("/api/setup/onboarding/complete")
    assert done.status_code == 200

    assert await _count(db) == 0
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True
    detail = await db.fetchval("SELECT detail FROM setup_step WHERE step = 'onboarding'")
    assert detail == {str(member_id): True}


async def test_onboarding_is_recorded_per_member_so_the_other_phone_is_still_asked(household):
    """"Member first-run onboarding then walks *each phone*" (§3.1). One member finishing it
    must not silence the prompt for the other — install and push permission are per-device
    acts, and there is no household-wide way to grant them."""
    admin, _admin_id, member, _member_id = household
    await member.post("/api/setup/onboarding/complete")

    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True
    assert (await admin.get("/api/push/state")).json()["onboarding_complete"] is False

    await admin.post("/api/setup/onboarding/complete")
    assert (await admin.get("/api/push/state")).json()["onboarding_complete"] is True
    assert (await member.get("/api/push/state")).json()["onboarding_complete"] is True


# --- the keys are secrets --------------------------------------------------------------------


async def test_the_endpoint_and_auth_key_never_come_back_out_of_the_api(household):
    """The endpoint URL is a bearer capability — anyone holding it can push to that device —
    and `auth` is the message-encryption key. Neither is needed by any screen, so neither is
    returned; devices are named to the UI by a hash of the endpoint instead."""
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
    # The device handle is what an operator correlates on instead, so it had better be there.
    assert push.device_handle(PHONE["endpoint"]) in caplog.text


def test_the_device_handle_is_stable_and_does_not_contain_the_endpoint():
    handle = push.device_handle(PHONE["endpoint"])
    assert handle == push.device_handle(PHONE["endpoint"])
    assert handle != push.device_handle(LAPTOP["endpoint"])
    assert len(handle) == 12
    assert PHONE["endpoint"] not in handle


# --- the key the browser needs at subscribe time ----------------------------------------------


async def test_the_state_route_reports_no_application_server_key_until_one_is_configured(
    household, monkeypatch
):
    """§12 puts the push *sender* in M4. Chrome refuses `pushManager.subscribe()` without an
    application server key, so the screen has to know the difference between "not configured
    yet" and "your browser said no" — answering null is how it can say so honestly."""
    _admin, _admin_id, member, _member_id = household
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    assert (await member.get("/api/push/state")).json()["vapid_public_key"] is None

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BFakePublicKeyForTests")
    assert (
        await member.get("/api/push/state")
    ).json()["vapid_public_key"] == "BFakePublicKeyForTests"
