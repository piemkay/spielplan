"""§6.6's Users row editor over HTTP, including its three floors (§6.6, §3.1, decision 166).
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from spielplan.api import admin as admin_api
from spielplan.core import auth, webauthn
from spielplan.core.config import settings
from tests.fixtures.soft_authenticator import SoftAuthenticator

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"

ROW_EDITOR_ROUTES = [
    ("PATCH", "/api/admin/users/{user_id}", {"name": "nobody"}),
    ("GET", "/api/admin/users/{user_id}/passkeys", None),
    ("POST", "/api/admin/users/{user_id}/reset-password", None),
    ("POST", "/api/admin/users/{user_id}/reset-pin", None),
    ("POST", "/api/admin/users/{user_id}/active", {"is_active": False}),
    ("DELETE", "/api/admin/users/{user_id}", None),
]


async def _admin(app, name: str = "patrick"):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": name, "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _create(admin, name: str, role: str = "member") -> dict:
    made = await admin.post("/api/admin/users", json={"name": name, "role": role})
    assert made.status_code == 201, made.text
    return made.json()


async def _member(app, admin, name: str = "jenny"):
    created = await _create(admin, name)
    client = app()
    otp = created["one_time_password"]
    assert (await client.post("/api/auth/login", json={"name": name, "password": otp})).status_code == 200
    changed = await client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    assert changed.status_code == 200
    return client, created


async def _roster(admin) -> dict[int, dict]:
    listed = await admin.get("/api/admin/users")
    assert listed.status_code == 200
    return {row["id"]: row for row in listed.json()}


def _device() -> SoftAuthenticator:
    cfg = settings()
    return SoftAuthenticator(rp_id=cfg.rp_id, origin=cfg.public_url)


async def _register_passkey(db, user_id: int, device: SoftAuthenticator, label: str = "phone"):
    ceremony = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    return await webauthn.register(
        db, user_id=user_id, handle=ceremony.id, credential=device.register(challenge), label=label
    )


async def _passkey_login(client, device: SoftAuthenticator, name: str = "jenny"):
    options = await client.post("/api/auth/passkey/login/options", json={"name": name})
    assert options.status_code == 200, options.text
    body = options.json()
    challenge = webauthn.base64url_to_bytes(body["options"]["challenge"])
    return await client.post(
        "/api/auth/passkey/login",
        json={"ceremony_id": body["ceremony_id"], "credential": device.authenticate(challenge)},
    )


async def test_creating_an_account_issues_a_one_time_password_and_locks_it(app):
    admin = await _admin(app)
    created = await _create(admin, "jenny")
    assert created["role"] == "member"

    member = app()
    signed_in = await member.post(
        "/api/auth/login", json={"name": "jenny", "password": created["one_time_password"]}
    )
    assert signed_in.status_code == 200
    assert (await member.get("/api/auth/me")).json()["must_change_password"] is True
    # The lock is the auth layer's, not the UI's: the product surfaces stay shut until it clears.
    assert (await member.get("/api/titles")).status_code == 403


async def test_the_one_time_password_is_shown_once_and_read_back_nowhere(app):
    """The OTP is stored as an argon2 hash and no route reports it."""
    admin = await _admin(app)
    otp = (await _create(admin, "jenny"))["one_time_password"]

    for path in ("/api/admin/users", "/api/auth/me", "/api/setup/state"):
        body = (await admin.get(path)).text
        assert otp not in body, f"{path} handed the one-time password back"


async def test_the_create_route_accepts_no_admin_chosen_password(app):
    """§3.1: an admin "never sees, sets or types a member's password", so the field does not exist."""
    admin = await _admin(app)
    made = await admin.post(
        "/api/admin/users",
        json={"name": "jenny", "role": "member", "password": "chosen-by-the-admin"},
    )
    assert made.status_code == 201

    member = app()
    chosen = await member.post(
        "/api/auth/login", json={"name": "jenny", "password": "chosen-by-the-admin"}
    )
    assert chosen.status_code == 401
    issued = await member.post(
        "/api/auth/login",
        json={"name": "jenny", "password": made.json()["one_time_password"]},
    )
    assert issued.status_code == 200


async def test_a_duplicate_name_is_refused_rather_than_answered_as_a_server_error(app):
    """`app_user_name_key` is on lower(name), so the second 'Jenny' collides: a 409, not a 500."""
    admin = await _admin(app)
    await _create(admin, "jenny")
    again = await admin.post("/api/admin/users", json={"name": "JENNY", "role": "member"})
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


async def test_a_guest_role_is_refused_by_the_route_that_makes_accounts(app):
    """Decision 166: a guest is a Tonight seat with `user_id NULL`, never an account."""
    admin = await _admin(app)
    made = await admin.post("/api/admin/users", json={"name": "gast", "role": "guest"})
    assert made.status_code == 422
    jenny = await _create(admin, "jenny")
    edited = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "guest"})
    assert edited.status_code == 422


async def test_a_rename_and_a_re_role_persist_to_the_roster(app):
    admin = await _admin(app)
    jenny = await _create(admin, "jenny")

    renamed = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"name": "jennifer"})
    assert renamed.status_code == 200
    assert (await _roster(admin))[jenny["id"]]["name"] == "jennifer"

    promoted = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert (await _roster(admin))[jenny["id"]]["role"] == "admin"


async def test_an_empty_row_edit_is_refused(app):
    """A PATCH naming neither field is a form that submitted nothing, not a rename to NULL."""
    admin = await _admin(app)
    jenny = await _create(admin, "jenny")
    assert (await admin.patch(f"/api/admin/users/{jenny['id']}", json={})).status_code == 400


async def test_a_password_reset_reissues_re_arms_the_lock_and_ends_the_sessions(db, app):
    """The sessions go because the credential that opened them is being replaced."""
    admin = await _admin(app)
    member, created = await _member(app, admin)

    reset = await admin.post(f"/api/admin/users/{created['id']}/reset-password")
    assert reset.status_code == 200
    fresh = reset.json()["one_time_password"]
    assert fresh != created["one_time_password"]
    assert reset.json()["sessions_revoked"] == 1
    assert await db.fetchval(
        "SELECT count(*) FROM auth_session WHERE user_id = $1", created["id"]
    ) == 0

    # The cookie that was live a moment ago, and the password its holder chose, are both gone.
    assert (await member.get("/api/auth/me")).status_code == 401
    stale = app()
    refused = await stale.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert refused.status_code == 401

    signed_in = await stale.post("/api/auth/login", json={"name": "jenny", "password": fresh})
    assert signed_in.status_code == 200
    assert signed_in.json()["must_change_password"] is True


async def test_a_password_reset_clears_a_standing_lockout(db, app):
    """§3.2's lockout counts guesses, not the credential;
    surviving the reset it would refuse the new OTP."""
    admin = await _admin(app)
    created = await _create(admin, "jenny")
    await db.execute(
        "UPDATE app_user SET password_failed_count = 5, "
        "password_locked_until = now() + interval '1 hour' WHERE id = $1",
        created["id"],
    )
    fresh = (
        await admin.post(f"/api/admin/users/{created['id']}/reset-password")
    ).json()["one_time_password"]

    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": fresh})
    assert signed_in.status_code == 200
    row = await db.fetchrow(
        "SELECT password_failed_count, password_locked_until FROM app_user WHERE id = $1",
        created["id"],
    )
    assert row["password_failed_count"] == 0
    assert row["password_locked_until"] is None


async def test_a_pin_reset_clears_the_pin_and_the_lockout_that_locked_it(db, app):
    """The counters go with the hash, or the account refuses the PIN it is about to be given."""
    admin = await _admin(app)
    member, created = await _member(app, admin)
    assert (
        await member.post(
            "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
        )
    ).status_code == 200

    for _ in range(auth.PIN_ATTEMPT_LIMIT):
        await admin.post("/api/auth/switch", json={"user_id": created["id"], "pin": "0000"})
    locked = await db.fetchrow(
        "SELECT pin_failed_count, pin_locked_until FROM app_user WHERE id = $1", created["id"]
    )
    assert locked["pin_failed_count"] == auth.PIN_ATTEMPT_LIMIT
    assert locked["pin_locked_until"] is not None

    cleared = await admin.post(f"/api/admin/users/{created['id']}/reset-pin")
    assert cleared.status_code == 200
    row = await db.fetchrow(
        "SELECT pin_hash, pin_failed_count, pin_locked_until FROM app_user WHERE id = $1",
        created["id"],
    )
    assert row["pin_hash"] is None
    assert row["pin_failed_count"] == 0
    assert row["pin_locked_until"] is None
    assert (await _roster(admin))[created["id"]]["has_pin"] is False


async def test_an_admin_reads_the_credential_ids_the_revoke_route_needs(db, app):
    """An id the list returns is an id the revoke takes; the projection carries no public key."""
    admin = await _admin(app)
    _member_client, created = await _member(app, admin)
    phone = await _register_passkey(db, created["id"], _device(), label="phone")
    await _register_passkey(db, created["id"], _device(), label="desktop")

    listed = await admin.get(f"/api/admin/users/{created['id']}/passkeys")
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert {c["label"] for c in rows} == {"phone", "desktop"}
    assert phone["credential_id"] in {c["id"] for c in rows}
    # What the row editor renders: which device, when it arrived, when it last answered.
    assert {"id", "label", "created_at", "last_used_at", "usable"} <= set(rows[0])
    assert not any(key.endswith("public_key") for c in rows for key in c)

    revoked = await admin.delete(
        f"/api/admin/users/{created['id']}/passkeys/{phone['credential_id']}"
    )
    assert revoked.status_code == 200
    after = (await admin.get(f"/api/admin/users/{created['id']}/passkeys")).json()
    assert [c["label"] for c in after] == ["desktop"]


async def test_the_credential_list_is_scoped_to_the_account_it_names(db, app):
    """The list is the input to the revoke, so it has to be scoped the same way the revoke is:
    an id read off one roster row must not appear on another's."""
    admin = await _admin(app)
    _member_client, jenny = await _member(app, admin)
    tom = await _create(admin, "tom")
    await _register_passkey(db, jenny["id"], _device())

    assert len((await admin.get(f"/api/admin/users/{jenny['id']}/passkeys")).json()) == 1
    assert (await admin.get(f"/api/admin/users/{tom['id']}/passkeys")).json() == []


async def test_an_admin_revokes_one_passkey_on_another_account(db, app):
    """One credential, not the account's set: a lost phone is one row."""
    admin = await _admin(app)
    _member_client, created = await _member(app, admin)
    phone = await _register_passkey(db, created["id"], _device(), label="phone")
    await _register_passkey(db, created["id"], _device(), label="desktop")
    assert (await _roster(admin))[created["id"]]["passkeys"] == 2

    revoked = await admin.delete(f"/api/admin/users/{created['id']}/passkeys/{phone['credential_id']}")
    assert revoked.status_code == 200
    remaining = await webauthn.list_credentials(db, created["id"])
    assert [c["label"] for c in remaining] == ["desktop"]


async def test_revoking_a_passkey_that_belongs_to_another_account_is_a_404(db, app):
    """The route matches on (user_id, credential_id)."""
    admin = await _admin(app)
    _member_client, jenny = await _member(app, admin)
    tom = await _create(admin, "tom")
    hers = await _register_passkey(db, jenny["id"], _device())

    missed = await admin.delete(f"/api/admin/users/{tom['id']}/passkeys/{hers['credential_id']}")
    assert missed.status_code == 404
    assert len(await webauthn.list_credentials(db, jenny["id"])) == 1


async def test_disabling_an_account_ends_its_sessions_and_refuses_the_cookie_and_a_login(db, app):
    admin = await _admin(app)
    member, created = await _member(app, admin)

    disabled = await admin.post(
        f"/api/admin/users/{created['id']}/active", json={"is_active": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["sessions_revoked"] == 1
    assert await db.fetchval(
        "SELECT count(*) FROM auth_session WHERE user_id = $1", created["id"]
    ) == 0
    assert (await member.get("/api/auth/me")).status_code == 401
    assert (await _roster(admin))[created["id"]]["is_active"] is False

    locked_out = app()
    refused = await locked_out.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert refused.status_code == 401

    # ...and the same control brings them back, or a disable would be a delete with extra steps.
    await admin.post(f"/api/admin/users/{created['id']}/active", json={"is_active": True})
    assert (
        await locked_out.post(
            "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
        )
    ).status_code == 200


async def test_a_disabled_account_cannot_answer_a_passkey_assertion(db, app):
    """The passkey is still registered (§3.2 keeps it across a logout); the account is refused."""
    admin = await _admin(app)
    _member_client, created = await _member(app, admin)
    device = _device()
    await _register_passkey(db, created["id"], device)

    anonymous = app()
    assert (await _passkey_login(anonymous, device)).status_code == 200

    await admin.post(f"/api/admin/users/{created['id']}/active", json={"is_active": False})
    refused = await _passkey_login(app(), device)
    assert refused.status_code == 401


async def test_deleting_an_account_removes_the_row(db, app):
    admin = await _admin(app)
    created = await _create(admin, "jenny")

    removed = await admin.delete(f"/api/admin/users/{created['id']}")
    assert removed.status_code == 200
    assert created["id"] not in await _roster(admin)
    assert await db.fetchval("SELECT count(*) FROM app_user WHERE id = $1", created["id"]) == 0
    assert (await admin.delete(f"/api/admin/users/{created['id']}")).status_code == 404


@pytest.mark.parametrize(("method", "path", "body"), ROW_EDITOR_ROUTES, ids=lambda v: str(v))
async def test_every_row_editor_route_answers_404_for_an_account_that_is_not_there(
    app, method, path, body
):
    """A roster can be one delete out of date; each route says so rather than reporting success."""
    admin = await _admin(app)
    response = await admin.request(method, path.format(user_id=999999), json=body)
    assert response.status_code == 404


# The floors are asked of the admin's own row: anyone else acting on an admin IS a second active admin.
# Zero admins would let anyone mint one at `POST /api/setup/admin`, which takes no auth.


async def test_the_last_active_admin_cannot_be_demoted(app, db):
    admin = await _admin(app)
    me = (await admin.get("/api/auth/me")).json()["id"]
    refused = await admin.patch(f"/api/admin/users/{me}", json={"role": "member"})
    assert refused.status_code == 409
    assert "last active admin" in refused.json()["detail"]
    assert await db.fetchval("SELECT role FROM app_user WHERE id = $1", me) == "admin"


async def test_the_last_active_admin_cannot_be_disabled(app, db):
    admin = await _admin(app)
    me = (await admin.get("/api/auth/me")).json()["id"]
    refused = await admin.post(f"/api/admin/users/{me}/active", json={"is_active": False})
    assert refused.status_code == 409
    assert "last active admin" in refused.json()["detail"]
    assert await db.fetchval("SELECT is_active FROM app_user WHERE id = $1", me) is True


async def test_the_last_active_admin_cannot_be_deleted(app, db):
    admin = await _admin(app)
    me = (await admin.get("/api/auth/me")).json()["id"]
    refused = await admin.delete(f"/api/admin/users/{me}")
    assert refused.status_code == 409
    assert "last active admin" in refused.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM app_user WHERE id = $1", me) == 1


async def test_a_disabled_admin_does_not_hold_the_floor(app):
    """A disabled admin cannot sign in, so it must not hold the floor."""
    admin = await _admin(app)
    _other, jenny = await _member(app, admin, name="jenny")
    me = (await admin.get("/api/auth/me")).json()["id"]
    await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})
    await admin.post(f"/api/admin/users/{jenny['id']}/active", json={"is_active": False})

    refused = await admin.patch(f"/api/admin/users/{me}", json={"role": "member"})
    assert refused.status_code == 409
    assert "last active admin" in refused.json()["detail"]


async def test_with_two_active_admins_each_of_the_three_succeeds(app, db):
    admin = await _admin(app)
    _other, jenny = await _member(app, admin, name="jenny")
    await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})

    demoted = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "member"})
    assert demoted.status_code == 200
    await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})

    disabled = await admin.post(
        f"/api/admin/users/{jenny['id']}/active", json={"is_active": False}
    )
    assert disabled.status_code == 200
    await admin.post(f"/api/admin/users/{jenny['id']}/active", json={"is_active": True})

    assert (await admin.delete(f"/api/admin/users/{jenny['id']}")).status_code == 200
    assert await db.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") == 1


async def test_an_admin_cannot_reset_or_disable_their_own_account_from_this_tab(app, db):
    """Refused even with a second admin standing, so it is the self rule answering and not the floor."""
    admin = await _admin(app)
    _other, jenny = await _member(app, admin, name="jenny")
    await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})
    me = (await admin.get("/api/auth/me")).json()["id"]

    for response in (
        await admin.post(f"/api/admin/users/{me}/reset-password"),
        await admin.post(f"/api/admin/users/{me}/reset-pin"),
        await admin.post(f"/api/admin/users/{me}/active", json={"is_active": False}),
    ):
        assert response.status_code == 409
        assert "their own" in response.json()["detail"]

    row = await db.fetchrow("SELECT is_active, must_change_password FROM app_user WHERE id = $1", me)
    assert row["is_active"] is True
    assert row["must_change_password"] is False



async def test_two_admins_removing_each_other_at_once_cannot_empty_the_floor(app, db, monkeypatch):
    """The floor is a read then a write, so only `_ROSTER_LOCK` holds it under interleaving. Both
    requests are held after the read, so the lock and not the scheduler decides."""
    active_admins = "SELECT count(*) FROM app_user WHERE role = 'admin' AND is_active"
    admin = await _admin(app)
    patrick = (await admin.get("/api/auth/me")).json()["id"]
    jenny_client, jenny = await _member(app, admin, name="jenny")
    promoted = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    assert await db.fetchval(active_admins) == 2

    both_arrived = asyncio.Event()
    arrivals = 0
    check = admin_api._refuse_if_last_active_admin

    async def hold(conn, row, verb):
        """A lapsing wait, not a barrier: with the lock the second
        request never arrives, and a barrier would deadlock."""
        nonlocal arrivals
        await check(conn, row, verb)
        arrivals += 1
        if arrivals == 2:
            both_arrived.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_arrived.wait(), 0.5)

    monkeypatch.setattr(admin_api, "_refuse_if_last_active_admin", hold)

    demote, disable = await asyncio.gather(
        admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "member"}),
        jenny_client.post(f"/api/admin/users/{patrick}/active", json={"is_active": False}),
    )

    left = await db.fetchval(active_admins)
    assert left == 1, (
        f"one admin was demoted while the other was disabled and the household kept {left} "
        "active admins - POST /api/setup/admin takes no auth dependency, so anyone who can "
        "reach the origin now mints one"
    )
    assert sorted([demote.status_code, disable.status_code]) == [200, 409], (
        f"the loser of the race was not refused: demote {demote.status_code}, "
        f"disable {disable.status_code}"
    )
