"""§6.6's Users row editor, over HTTP. Spec v2.1 §6.6, §3.1, §3.2; decision 166.

Every duty §6.6 names for the Users card is a write on `app_user`, `auth_session` or
`webauthn_credential`, and none of them existed: `is_active` was read in six places and written
in none, and the only cure for a forgotten password was `psql` with a hand-made argon2 hash. So
these tests are at the route and against a real database — a unit test of the SQL would prove
the statement and not that an admin can reach it, and the three floors §6.6 *enforces* are
refusals a caller has to be able to hit.

The floors, in the spec's own words: "the last active `admin` can be neither demoted nor
disabled nor deleted; an admin cannot reset their own credentials from this tab; and a one-time
password is shown exactly once, at the moment it is issued."

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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
    """The first-boot admin, signed in. §3.1's only account-minting path before this milestone."""
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
    """A created account that has been through §3.1's forced first-login change."""
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


# --- §6.6 create, and §3.1's one-time password ----------------------------------------------


async def test_creating_an_account_issues_a_one_time_password_and_locks_it(app):
    """§3.1: "a one-time password is issued, the account is locked to a password change at
    first login"."""
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
    """§6.6's third floor. The OTP is stored as an argon2 hash and no route reports it, which is
    what makes "an admin never sees, sets or types a member's password" (§3.1) true of the
    reissue as well as of the first issue."""
    admin = await _admin(app)
    otp = (await _create(admin, "jenny"))["one_time_password"]

    for path in ("/api/admin/users", "/api/auth/me", "/api/setup/state"):
        body = (await admin.get(path)).text
        assert otp not in body, f"{path} handed the one-time password back"


async def test_the_create_route_accepts_no_admin_chosen_password(app):
    """§3.1: an admin "never sees, sets or types a member's password". A password in the body is
    not an error the admin has to be told about — it is a field this route does not have, and
    the account is reachable by the issued one-time password and nothing else."""
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
    """`app_user_name_key` is on lower(name), so the second 'Jenny' is the index's collision
    (as04) — a 409 the double-tapped form can render, not a 500 "database error"."""
    admin = await _admin(app)
    await _create(admin, "jenny")
    again = await admin.post("/api/admin/users", json={"name": "JENNY", "role": "member"})
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


async def test_a_guest_role_is_refused_by_the_route_that_makes_accounts(app):
    """Decision 166: two roles and no others. A guest is a Tonight seat with `user_id NULL`
    (§4.2), never an account — so the role is not spellable at the surface that mints them."""
    admin = await _admin(app)
    made = await admin.post("/api/admin/users", json={"name": "gast", "role": "guest"})
    assert made.status_code == 422
    jenny = await _create(admin, "jenny")
    edited = await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "guest"})
    assert edited.status_code == 422


# --- §6.6 rename and change role --------------------------------------------------------------


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


# --- §6.6 password reset ----------------------------------------------------------------------


async def test_a_password_reset_reissues_re_arms_the_lock_and_ends_the_sessions(db, app):
    """§6.6: "password reset (reissues the one-time password and re-arms the first-login
    change)". The sessions go because the credential that opened them is the one being
    replaced — a device still holding a cookie would otherwise keep the account for the rest of
    its sliding 90 days (§3.2)."""
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
    """The account being reset is often the one someone has been guessing at, and §3.2's lockout
    counts the guesses, not the credential. A lockout that survived the reset would refuse the
    one-time password the admin just read out."""
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


# --- §6.6 PIN reset -----------------------------------------------------------------------------


async def test_a_pin_reset_clears_the_pin_and_the_lockout_that_locked_it(db, app):
    """§6.6's "PIN reset". Whoever needs it is usually locked out of the PIN (§3.2's escalating
    lockout is the defence for a 10^4 keyspace), so the counters go with the hash or the account
    refuses the PIN it is about to be given."""
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


# --- §6.6 passkey list with per-credential revoke -------------------------------------------------


async def test_an_admin_reads_the_credential_ids_the_revoke_route_needs(db, app):
    """The *list* half of §6.6's "passkey list with per-credential revoke".

    The revoke below shipped without it, which made it unreachable from any client: the roster
    carries an integer count, `GET /api/auth/passkey/credentials` is scoped to the caller, and
    nothing else in the app could name another account's credential id. So this asserts the
    pair — an id the list returns is an id the revoke takes — and that the projection carries
    no public key, which a client has nothing to do with and §14.3's rule about secrets that
    never come back out covers by the same argument.
    """
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
    """§6.6: "passkey list with per-credential revoke". One credential, not the account's set —
    a lost phone is one row and the desktop key beside it is still good."""
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
    """The route matches on (user_id, credential_id), so an id read off one roster row cannot
    reach across to another account's key."""
    admin = await _admin(app)
    _member_client, jenny = await _member(app, admin)
    tom = await _create(admin, "tom")
    hers = await _register_passkey(db, jenny["id"], _device())

    missed = await admin.delete(f"/api/admin/users/{tom['id']}/passkeys/{hers['credential_id']}")
    assert missed.status_code == 404
    assert len(await webauthn.list_credentials(db, jenny["id"])) == 1


# --- §6.6 disable and delete ----------------------------------------------------------------------


async def test_disabling_an_account_ends_its_sessions_and_refuses_the_cookie_and_a_login(db, app):
    """§6.6: "disable (`is_active = false`: every session and passkey assertion refused, the
    account and its Ledger kept)"."""
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

    # …and the same control brings them back, or a disable would be a delete with extra steps.
    await admin.post(f"/api/admin/users/{created['id']}/active", json={"is_active": True})
    assert (
        await locked_out.post(
            "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
        )
    ).status_code == 200


async def test_a_disabled_account_cannot_answer_a_passkey_assertion(db, app):
    """The other half of §6.6's disable clause. The passkey is still registered — §3.2 keeps
    them across a logout — and the assertion is refused because the account is."""
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
    """§6.6's "delete", the destructive half of the pair whose other half keeps the Ledger."""
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
    """A roster the admin is looking at can be one delete out of date. Each route says so
    rather than writing nothing and reporting success."""
    admin = await _admin(app)
    response = await admin.request(method, path.format(user_id=999999), json=body)
    assert response.status_code == 404


# --- §6.6's floors --------------------------------------------------------------------------------
#
# All three are asked of the admin's own row, because that is the only way to ask them: anyone
# else demoting, disabling or deleting an admin *is* a second active admin, so the floor does
# not bind. That is also why the floor is a security rule and not politeness — an install that
# reaches zero admins lets anyone who can see the origin mint one at `POST /api/setup/admin`,
# which takes no auth dependency at all (§3.1, decision 166).


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
    """The floor counts *active* admins. A disabled admin cannot sign in, so an install whose
    only other admin is disabled is one demotion away from nobody being able to reach §6.6."""
    admin = await _admin(app)
    _other, jenny = await _member(app, admin, name="jenny")
    me = (await admin.get("/api/auth/me")).json()["id"]
    await admin.patch(f"/api/admin/users/{jenny['id']}", json={"role": "admin"})
    await admin.post(f"/api/admin/users/{jenny['id']}/active", json={"is_active": False})

    refused = await admin.patch(f"/api/admin/users/{me}", json={"role": "member"})
    assert refused.status_code == 409
    assert "last active admin" in refused.json()["detail"]


async def test_with_two_active_admins_each_of_the_three_succeeds(app, db):
    """The floor is a floor, not a prohibition: demote, disable and delete all work on an admin
    while another active one stands."""
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
    """§6.6: "an admin cannot reset their own credentials from this tab (§3.2's 24-hour
    re-prompt governs the rest)". Refused even with a second admin standing, so it is the self
    rule answering and not the floor."""
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
    """The floor is a read followed by a write, so it holds only while the writers cannot
    interleave. `_ROSTER_LOCK` (api/admin.py) is what stops them, and its comment names this
    exact pair: "demoting A while disabling B is the race".

    Every other test of the floor is sequential, and each asks its refusal of a household that
    already has exactly one admin — which no lock is needed to refuse. All six therefore pass
    with the lock deleted, leaving the one mechanism the floor rests on with no test at all.

    Deterministic rather than raced for: both requests are held at the point where they have
    read the roster and not yet written, so the outcome is decided by the lock and not by which
    coroutine the event loop happened to resume. With the lock the second request cannot reach
    that point until the first has committed, so the hold simply lapses.
    """
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
        """Let the floor's own read happen, then wait for the other request to reach here too.

        A lapsing wait rather than a barrier, because the passing case is the one where the
        second request never arrives: it is still blocked on `pg_advisory_xact_lock`, and a
        barrier would deadlock exactly the behaviour under test.
        """
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
