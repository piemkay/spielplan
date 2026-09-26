"""Account-security rules at the route (§3.1, §3.2, decisions 164, 166): what a PIN session may not do,
the re-prompt, the throttle, the anonymous bound, and atomic writes. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import inspect

import asyncpg
import pytest

from spielplan.app import create_app
from spielplan.core import auth, webauthn
from spielplan.core.config import settings
from tests.fixtures.soft_authenticator import SoftAuthenticator

# Imported rather than copied: a second walker is a second thing to keep true of the same app.
from tests.test_api_gating import _routes, concrete

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"
MEMBER_PIN = "4821"


async def _admin(app, name: str = "patrick"):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": name, "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _member(app, admin, name: str = "jenny"):
    made = await admin.post("/api/admin/users", json={"name": name, "role": "member"})
    assert made.status_code == 201, made.text
    otp = made.json()["one_time_password"]
    client = app()
    assert (
        await client.post("/api/auth/login", json={"name": name, "password": otp})
    ).status_code == 200
    changed = await client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    assert changed.status_code == 200, changed.text
    return client


async def _switched_in(admin, member):
    """A session minted by §3.2's PIN switch: the handed-over-phone case."""
    set_pin = await member.post(
        "/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": MEMBER_PASSWORD}
    )
    assert set_pin.status_code == 200, set_pin.text
    jenny = (await member.get("/api/auth/me")).json()["id"]
    switched = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": MEMBER_PIN})
    assert switched.status_code == 200, switched.text
    assert (await admin.get("/api/auth/me")).json()["auth_method"] == "pin"
    return admin, jenny


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/admin/users", {"name": "ghost", "role": "guest"}),
        ("PATCH", "/api/admin/users/{user_id}", {"role": "guest"}),
    ],
    ids=("create", "re-role"),
)
async def test_a_guest_role_is_refused_at_every_route_that_writes_one(app, method, path, body):
    """Decision 166: a guest is a Tonight seat, never an account; the `Literal` mirrors 0016's CHECK."""
    admin = await _admin(app)
    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    refused = await admin.request(method, path.format(user_id=made.json()["id"]), json=body)
    assert refused.status_code == 422, refused.text


async def test_the_app_user_check_admits_exactly_two_roles(db):
    """The route is only as good as the column: a psql write of 'guest' would bypass it."""
    for role in ("admin", "member"):
        written = await db.fetchval(
            "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING role", role, role
        )
        assert written == role
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO app_user (name, role) VALUES ('ghost', 'guest')")


async def test_account_creation_exists_at_exactly_one_path_besides_first_boot(app):
    """Walks `create_app()`'s endpoints, so a second minting route is caught however it is filed."""
    minting = {
        (method, route.path)
        for route in _routes(create_app().routes)
        if "INSERT INTO app_user" in inspect.getsource(route.endpoint)
        for method in (route.methods or ())
        if method in ("GET", "POST", "PUT", "DELETE", "PATCH")
    }
    assert minting == {("POST", "/api/admin/users"), ("POST", "/api/setup/admin")}, (
        f"a route outside §6.6 and §3.1's first boot inserts into app_user: {sorted(minting)}"
    )
    admin = await _admin(app)
    gone = await admin.post("/api/setup/members", json={"name": "jenny", "role": "member"})
    assert gone.status_code == 404, "the wizard's member step is decision 164's to delete"


async def test_no_other_mounted_route_can_be_made_to_mint_an_account(db, app):
    """The walk above reads source; this one fires every mounted route and counts `app_user` rows."""
    admin = await _admin(app)
    probes = sorted(
        (method, route.path)
        for route in _routes(create_app().routes)
        for method in (route.methods or ())
        if method in ("GET", "POST", "PUT", "DELETE", "PATCH")
    )
    assert len(probes) > 50, f"the walk found {len(probes)} routes — it is sweeping a short list"

    minted: dict[tuple[str, str], int] = {}
    for index, (method, path) in enumerate(probes):
        before = await db.fetchval("SELECT count(*) FROM app_user")
        await admin.request(
            method, concrete(path), json={"name": f"probe-{index}", "role": "member"}
        )
        after = await db.fetchval("SELECT count(*) FROM app_user")
        if after != before:
            minted[(method, path)] = after - before
        # `/api/auth/logout` ends the session, so the sweep signs back in rather than probing anonymously.
        if (await admin.get("/api/auth/me")).status_code != 200:
            back = await admin.post(
                "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
            )
            assert back.status_code == 200, back.text

    assert minted == {("POST", "/api/admin/users"): 1}, (
        f"accounts were created outside §6.6's one route: {sorted(minted)}"
    )


async def test_a_pin_session_is_refused_every_route_that_mints_a_credential(app):
    """A switched-in session that could register a passkey would turn four digits into a credential."""
    admin = await _admin(app)
    member = await _member(app, admin)
    phone, _jenny = await _switched_in(admin, member)

    garbage = {"id": "x", "rawId": "x", "type": "public-key", "response": {}}
    refusals = [
        ("POST", "/api/auth/passkey/register/options", None),
        ("POST", "/api/auth/passkey/register", {"ceremony_id": "x", "credential": garbage}),
        ("DELETE", "/api/auth/passkey/credentials/whatever", None),
        ("POST", "/api/auth/pin", {"pin": "1111", "current_password": MEMBER_PASSWORD}),
        (
            "POST",
            "/api/push/subscribe",
            {"endpoint": "https://push.example/x", "keys": {"p256dh": "k", "auth": "a"}},
        ),
        ("DELETE", "/api/push/subscription", {"endpoint": "https://push.example/x"}),
    ]
    for method, path, body in refusals:
        response = await phone.request(method, path, json=body)
        assert response.status_code == 403, f"{method} {path}: {response.text}"

    # Reading is not minting.
    assert (await phone.get("/api/auth/passkey/credentials")).status_code == 200
    assert (await phone.get("/api/push/state")).status_code == 200
    # The product surfaces are exactly what the switch is for.
    for path in ("/api/rate/balance", "/api/rank/tiers", "/api/tonight/rooms"):
        assert (await phone.get(path)).status_code == 200, path


async def test_a_pin_session_on_an_admin_account_never_reaches_the_admin_surface(db, app):
    """`create_session` never stamps `auth_method='pin'`, so a switch cannot look freshly re-prompted."""
    admin = await _admin(app)
    second = await admin.post("/api/admin/users", json={"name": "sam", "role": "admin"})
    otp = second.json()["one_time_password"]
    sam = app()
    await sam.post("/api/auth/login", json={"name": "sam", "password": otp})
    await sam.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    await sam.post("/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": MEMBER_PASSWORD})

    switched = await admin.post(
        "/api/auth/switch", json={"user_id": second.json()["id"], "pin": MEMBER_PIN}
    )
    assert switched.status_code == 200
    refused = await admin.get("/api/admin/users")
    assert refused.status_code == 401
    assert refused.headers.get("X-Spielplan-Reauth") == "admin"
    stamped = await db.fetchval(
        "SELECT count(*) FROM auth_session WHERE auth_method = 'pin' "
        "AND admin_verified_at IS NOT NULL"
    )
    assert stamped == 0


async def test_setting_a_pin_costs_the_password_and_clears_the_lockout(db, app):
    """Decision 170: setting a PIN costs the password; the old PIN's failures clear with it."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]

    assert (await member.post("/api/auth/pin", json={"pin": MEMBER_PIN})).status_code == 422
    wrong = await member.post(
        "/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": "not-the-password"}
    )
    assert wrong.status_code == 401
    assert await db.fetchval("SELECT pin_hash FROM app_user WHERE id = $1", jenny) is None

    await db.execute(
        "UPDATE app_user SET pin_failed_count = 4, pin_locked_until = now() + interval '1 hour' "
        "WHERE id = $1",
        jenny,
    )
    ok = await member.post(
        "/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": MEMBER_PASSWORD}
    )
    assert ok.status_code == 200, ok.text
    row = await db.fetchrow(
        "SELECT pin_hash, pin_failed_count, pin_locked_until FROM app_user WHERE id = $1", jenny
    )
    assert row["pin_hash"] is not None
    assert (row["pin_failed_count"], row["pin_locked_until"]) == (0, None)


async def _go_stale(db) -> None:
    await db.execute("UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'")


async def test_reauth_clears_the_stamp_on_the_session_in_hand(db, app):
    """Re-auth clears the stamp on the session in hand and mints nothing."""
    admin = await _admin(app)
    before = await db.fetchval("SELECT count(*) FROM auth_session")
    await _go_stale(db)
    assert (await admin.get("/api/admin/users")).status_code == 401

    cleared = await admin.post("/api/auth/reauth", json={"password": ADMIN_PASSWORD})
    assert cleared.status_code == 200, cleared.text
    # The banner has to clear on the response that cleared it, or the shell asks again.
    assert cleared.json()["admin_reauth_required"] is False
    assert "set-cookie" not in cleared.headers, "re-auth mints no session, so it re-issues none"
    assert await db.fetchval("SELECT count(*) FROM auth_session") == before
    assert (await admin.get("/api/admin/users")).status_code == 200


async def test_a_wrong_password_does_not_clear_the_stamp(db, app):
    admin = await _admin(app)
    await _go_stale(db)
    refused = await admin.post("/api/auth/reauth", json={"password": "not-the-password"})
    assert refused.status_code == 401
    assert (await admin.get("/api/admin/users")).status_code == 401


async def test_reauth_is_refused_to_a_member_and_to_a_pin_session(db, app):
    """A PIN session cannot be upgraded however good the typed password."""
    admin = await _admin(app)
    member = await _member(app, admin)
    denied = await member.post("/api/auth/reauth", json={"password": MEMBER_PASSWORD})
    assert denied.status_code == 403

    phone, _jenny = await _switched_in(admin, member)
    refused = await phone.post("/api/auth/reauth", json={"password": MEMBER_PASSWORD})
    assert refused.status_code == 403
    stamped = await db.fetchval(
        "SELECT count(*) FROM auth_session WHERE auth_method = 'pin' "
        "AND admin_verified_at IS NOT NULL"
    )
    assert stamped == 0


async def test_repeated_wrong_passwords_lock_the_account_with_the_same_refusal(db, app):
    """No Cloudflare rate limit on Tailscale; the refusal
    never says "locked", which would confirm the name."""
    admin = await _admin(app)
    await _member(app, admin)
    stranger = app()

    for _ in range(auth.PASSWORD_ATTEMPT_LIMIT):
        refused = await stranger.post(
            "/api/auth/login", json={"name": "jenny", "password": "wrong"}
        )
        assert refused.status_code == 401
        assert refused.json()["detail"] == auth.PASSWORD_REFUSAL

    locked = await stranger.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert locked.status_code == 401
    assert locked.json()["detail"] == auth.PASSWORD_REFUSAL
    stood = await db.fetchval("SELECT password_locked_until FROM app_user WHERE name = 'jenny'")
    assert stood is not None

    # A refusal during the lockout does not extend it, or a guesser holds the owner out indefinitely.
    await stranger.post("/api/auth/login", json={"name": "jenny", "password": "wrong"})
    still = await db.fetchval("SELECT password_locked_until FROM app_user WHERE name = 'jenny'")
    assert still == stood

    await db.execute("UPDATE app_user SET password_locked_until = NULL WHERE name = 'jenny'")
    signed_in = await stranger.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert signed_in.status_code == 200
    counters = await db.fetchrow(
        "SELECT password_failed_count, password_locked_until FROM app_user WHERE name = 'jenny'"
    )
    assert (counters["password_failed_count"], counters["password_locked_until"]) == (0, None)


async def test_a_password_change_forgets_the_failures_against_the_old_one(db, app):
    """The count is the credential's: the live window
    refuses the change; a lapsed count is cleared by it."""
    admin = await _admin(app)
    member = await _member(app, admin)
    change = {"current_password": MEMBER_PASSWORD, "new_password": "a-third-password"}
    await db.execute(
        "UPDATE app_user SET password_failed_count = 5, "
        "password_locked_until = now() + interval '1 hour' WHERE name = 'jenny'"
    )
    refused = await member.post("/api/auth/password", json=change)
    assert refused.status_code == 401, refused.text
    standing = await db.fetchval(
        "SELECT password_failed_count FROM app_user WHERE name = 'jenny'"
    )
    assert standing == 5, "a refusal made while the lockout stands must not extend the count"

    await db.execute(
        "UPDATE app_user SET password_locked_until = now() - interval '1 minute' "
        "WHERE name = 'jenny'"
    )
    changed = await member.post("/api/auth/password", json=change)
    assert changed.status_code == 200, changed.text
    counters = await db.fetchrow(
        "SELECT password_failed_count, password_locked_until FROM app_user WHERE name = 'jenny'"
    )
    assert (counters["password_failed_count"], counters["password_locked_until"]) == (0, None)


# The four routes that verify `password_hash`; they share the counter.
_PASSWORD_DOORS = (
    ("/api/auth/login", lambda password: {"name": "patrick", "password": password}),
    (
        "/api/auth/password",
        lambda password: {"current_password": password, "new_password": "a-new-one-entirely"},
    ),
    ("/api/auth/reauth", lambda password: {"password": password}),
    ("/api/auth/pin", lambda password: {"pin": "1357", "current_password": password}),
)


@pytest.mark.parametrize(
    "path, body", _PASSWORD_DOORS, ids=("login", "password", "reauth", "pin")
)
async def test_every_route_that_verifies_the_password_counts_the_failure(db, app, path, body):
    """One route per case, so a single door losing its counter fails on its own name."""
    admin = await _admin(app)
    for attempt in range(auth.PASSWORD_ATTEMPT_LIMIT):
        refused = await admin.post(path, json=body("nowhere-near-it"))
        assert refused.status_code == 401, f"attempt {attempt} at {path}: {refused.text}"

    counters = await db.fetchrow(
        "SELECT password_failed_count, password_locked_until FROM app_user WHERE name = 'patrick'"
    )
    assert counters["password_failed_count"] == auth.PASSWORD_ATTEMPT_LIMIT, (
        f"{auth.PASSWORD_ATTEMPT_LIMIT} wrong passwords through {path} were counted "
        f"{counters['password_failed_count']} times"
    )
    assert counters["password_locked_until"] is not None

    # The count must arm the lockout the front door honours.
    stranger = app()
    locked = await stranger.post(
        "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert locked.status_code == 401, (
        f"guessing through {path} armed no lockout: the correct password still signs in"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 129, "password": "p"},
        {"name": "patrick", "password": "p" * 129},
        {"name": "patrick", "password": "p", "device_label": "d" * 257},
        {"name": "pat\x00rick", "password": "p"},
        {"name": "patrick", "password": "p", "device_label": "kitchen\ttablet"},
    ],
    ids=("long-name", "long-password", "long-label", "nul-in-name", "control-in-label"),
)
async def test_the_login_body_is_bounded_and_control_characters_are_refused(app, body):
    """A NUL reached Postgres as a 500; an unbounded body let a caller choose argon2's cost."""
    anonymous = app()
    assert (await anonymous.post("/api/auth/login", json=body)).status_code == 422


async def test_a_name_that_differs_only_by_surrounding_whitespace_still_signs_in(db, app):
    """The index is on `lower(name)` and old rows may carry spaces, so both ends are trimmed."""
    await _admin(app)
    await db.execute(
        "INSERT INTO app_user (name, role, password_hash, must_change_password) "
        "VALUES (' Tom ', 'member', $1, false)",
        auth.hash_password(MEMBER_PASSWORD),
    )
    for typed in ("tom", " Tom ", "TOM  "):
        client = app()
        response = await client.post(
            "/api/auth/login", json={"name": typed, "password": MEMBER_PASSWORD}
        )
        assert response.status_code == 200, f"{typed!r} did not resolve: {response.text}"


@pytest.mark.parametrize(
    "pin",
    ["١٢٣٤", "１２３４"],
    ids=("arabic-indic", "fullwidth"),
)
async def test_a_non_ascii_digit_is_refused_as_a_pin_at_both_ends(app, pin):
    r"""Pydantic's `\d` is Unicode-aware, so a PIN pattern must name ASCII digits."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    setting = await member.post(
        "/api/auth/pin", json={"pin": pin, "current_password": MEMBER_PASSWORD}
    )
    assert setting.status_code == 422
    switching = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": pin})
    assert switching.status_code == 422


async def test_a_first_boot_that_fails_part_way_leaves_no_row_behind(db, app, monkeypatch):
    """Three writes in one transaction, or a failed first boot can never be re-run."""

    def boom(*_args, **_kwargs):
        raise asyncpg.PostgresError("the session insert failed")

    monkeypatch.setattr(auth, "create_session", boom)
    failed = await app().post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert failed.status_code == 500
    for table in ("app_user", "setup_step", "auth_session"):
        kept = await db.fetchval(f"SELECT count(*) FROM {table}")
        assert kept == 0, f"{table} kept a row"


async def test_two_simultaneous_first_boots_produce_exactly_one_admin(db, app):
    """Without the advisory lock both submits pass the count check and both become the only admin."""
    one, two = app(), app()
    # The warm-up is concurrent, or warming would itself serialise the two requests.
    await asyncio.gather(one.get("/api/health"), two.get("/api/health"))

    results = await asyncio.gather(
        one.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}),
        two.post("/api/setup/admin", json={"name": "jenny", "password": ADMIN_PASSWORD}),
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    assert await db.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") == 1
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 1


async def test_a_duplicate_name_on_first_boot_is_a_conflict_and_rolls_back(db, app):
    """`app_user_name_key` is the race the pre-read cannot see."""
    admin = await _admin(app)
    await admin.post("/api/admin/users", json={"name": "sam", "role": "member"})
    # The first boot's `setup_step` row stays: the refused attempt must add nothing.
    await db.execute("DELETE FROM app_user WHERE role = 'admin'")
    steps_before = await db.fetchval("SELECT count(*) FROM setup_step")

    clash = await app().post("/api/setup/admin", json={"name": "sam", "password": ADMIN_PASSWORD})
    assert clash.status_code == 409
    assert "already exists" in clash.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM app_user") == 1
    assert await db.fetchval("SELECT count(*) FROM setup_step") == steps_before
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 0


async def test_a_password_change_that_fails_part_way_changes_nothing(db, app, monkeypatch):
    """Hash, counters and other sessions in one transaction, like the admin-side twin."""

    def boom(*_args, **_kwargs):
        raise asyncpg.PostgresError("the revoke failed")

    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    other_device = app()
    signed_in = await other_device.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert signed_in.status_code == 200
    sessions_before = await db.fetchval(
        "SELECT count(*) FROM auth_session WHERE user_id = $1", jenny
    )
    assert sessions_before == 2

    monkeypatch.setattr(auth, "destroy_other_sessions", boom)
    failed = await member.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert failed.status_code == 500

    stored = await db.fetchval("SELECT password_hash FROM app_user WHERE id = $1", jenny)
    assert auth.verify_password(stored, MEMBER_PASSWORD), "the old password must still stand"
    assert not auth.verify_password(stored, "a-brand-new-password")
    kept = await db.fetchval("SELECT count(*) FROM auth_session WHERE user_id = $1", jenny)
    assert kept == sessions_before


async def test_a_sign_in_that_fails_part_way_leaves_the_device_holding_its_session(
    db, app, device, monkeypatch
):
    """The old session's DELETE must not commit before the
    new INSERT; `/switch` would leave no session at all."""

    def boom(*_args, **_kwargs):
        raise asyncpg.PostgresError("the session insert failed")

    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    set_pin = await member.post(
        "/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": MEMBER_PASSWORD}
    )
    assert set_pin.status_code == 200, set_pin.text
    await _register_for(db, jenny, device)
    sessions_before = await db.fetchval("SELECT count(*) FROM auth_session")

    monkeypatch.setattr(auth, "create_session", boom)

    failed = await member.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert failed.status_code == 500
    still_signed_in = await member.get("/api/auth/me")
    assert still_signed_in.status_code == 200, "a failed re-login signed the device out"
    assert still_signed_in.json()["name"] == "jenny"

    # §3.2's primary method, at the same seam and for the same reason.
    passkey_failed = await _passkey_login(member, device, "jenny")
    assert passkey_failed.status_code == 500
    assert (await member.get("/api/auth/me")).status_code == 200, (
        "a failed passkey sign-in signed the device out"
    )

    switch_failed = await admin.post(
        "/api/auth/switch", json={"user_id": jenny, "pin": MEMBER_PIN}
    )
    assert switch_failed.status_code == 500
    kept = await admin.get("/api/auth/me")
    assert kept.status_code == 200, "a failed switch left the handed-over phone with no session"
    assert kept.json()["name"] == "patrick"
    assert await db.fetchval("SELECT count(*) FROM auth_session") == sessions_before


@pytest.mark.parametrize(
    ("method", "path", "body", "refusal"),
    [
        ("GET", "/api/admin/users", None, 403),          # deps.admin_user: wrong role
        ("POST", "/api/auth/pin", {"pin": "nope"}, 422),  # the validation handler's path
    ],
)
async def test_a_refused_request_still_carries_the_cookie_its_slide_earned(
    db, app, method, path, body, refusal
):
    """Starlette builds its own error response, dropping the Set-Cookie the slide earned."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    # A day old with ten days left, so the slide is both due and visible.
    await db.execute(
        "UPDATE auth_session SET expires_at = now() + interval '10 days',"
        " last_seen_at = now() - interval '2 days' WHERE user_id = $1",
        jenny,
    )

    refused = await member.request(method, path, json=body)
    assert refused.status_code == refusal, refused.text

    slid = await db.fetchval(
        "SELECT expires_at > now() + interval '80 days' FROM auth_session WHERE user_id = $1",
        jenny,
    )
    assert slid is True, "the row must have slid, or this asserts nothing"
    cookie = refused.headers.get("set-cookie")
    assert cookie is not None and auth.SESSION_COOKIE in cookie, "the row slid; the cookie did not"

    # There is no second chance later in the day.
    ok = await member.get("/api/auth/me")
    assert ok.status_code == 200
    assert "set-cookie" not in ok.headers


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/setup/state"])
async def test_a_refusal_for_a_dead_session_does_not_clear_the_live_one_that_replaced_it(
    db, app, monkeypatch, path
):
    """`Set-Cookie` clears by NAME, so a late 401 for a dead session cleared the live one."""
    admin = await _admin(app)
    browser = await _member(app, admin)  # one cookie jar is one browser
    s1 = auth.open_session_cookie(browser.cookies.get(auth.SESSION_COOKIE))

    arrived, gate = asyncio.Event(), asyncio.Event()
    real_load = auth.load_session

    async def slow_load(conn, sid):
        if sid == s1:
            arrived.set()
            await gate.wait()
        return await real_load(conn, sid)

    monkeypatch.setattr(auth, "load_session", slow_load)

    inflight = asyncio.create_task(browser.get(path))
    await asyncio.wait_for(arrived.wait(), timeout=10)

    assert (await browser.post("/api/auth/logout")).status_code == 200
    relogin = await browser.post(
        "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
    )
    assert relogin.status_code == 200, relogin.text
    s2_cookie = browser.cookies.get(auth.SESSION_COOKIE)
    s2 = auth.open_session_cookie(s2_cookie)
    assert s2 != s1, "the re-login must have minted a different session, or this asserts nothing"

    gate.set()
    landed = await inflight
    assert "set-cookie" not in landed.headers, (
        f"{path} answered for the dead S1 and cleared the cookie by name: "
        f"{landed.headers.get('set-cookie')!r}"
    )
    assert browser.cookies.get(auth.SESSION_COOKIE) == s2_cookie, (
        "the browser lost S2 to a response that was not answering for it"
    )
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", s2) == 1, (
        "S2's row must still be there — the defect is the cookie, not the session"
    )
    after = await browser.get("/api/auth/me")
    assert after.status_code == 200, "signed in, and thrown back to the sign-in page a moment later"


@pytest.fixture
def device():
    cfg = settings()
    return SoftAuthenticator(rp_id=cfg.rp_id, origin=cfg.public_url)


async def _register_for(db, user_id: int, device: SoftAuthenticator) -> None:
    ceremony = await webauthn.registration_options(db, user_id=user_id, user_name="patrick")
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    await webauthn.register(
        db,
        user_id=user_id,
        handle=ceremony.id,
        credential=device.register(challenge),
        label="phone",
    )


async def _passkey_login(client, device: SoftAuthenticator, name: str, **kwargs):
    options = await client.post("/api/auth/passkey/login/options", json={"name": name})
    assert options.status_code == 200, options.text
    body = options.json()
    challenge = webauthn.base64url_to_bytes(body["options"]["challenge"])
    return await client.post(
        "/api/auth/passkey/login",
        json={
            "ceremony_id": body["ceremony_id"],
            "credential": device.authenticate(challenge, **kwargs),
        },
    )


@pytest.mark.parametrize("uv", [True, False], ids=("verified", "presence-only"))
async def test_only_a_user_verified_assertion_stamps_the_admin_clock(db, app, device, uv):
    """A merely touched roaming key must not satisfy the 24 h admin re-prompt."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")

    signed_in = await _passkey_login(admin, device, "patrick", uv=uv)
    assert signed_in.status_code == 200, signed_in.text
    reached = await admin.get("/api/admin/users")
    if uv:
        assert reached.status_code == 200
    else:
        assert reached.status_code == 401
        assert reached.headers.get("X-Spielplan-Reauth") == "admin"


async def test_an_assertion_whose_counter_has_not_advanced_is_refused(db, app, device):
    """A refusal must leave the stored counter where it was."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")
    await db.execute("UPDATE webauthn_credential SET sign_count = 99")

    refused = await _passkey_login(admin, device, "patrick")
    assert refused.status_code == 401
    assert await db.fetchval("SELECT sign_count FROM webauthn_credential") == 99


async def test_two_assertions_carrying_the_same_advanced_counter_leave_one_winner(
    db, app, device, monkeypatch
):
    """Both requests are held after reading `sign_count`, so the conditional UPDATE alone orders them."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")
    # Non-zero first: at zero the UPDATE deliberately accepts a tie (synced passkeys).
    assert (await _passkey_login(admin, device, "patrick")).status_code == 200
    await admin.post("/api/auth/logout")
    assert await db.fetchval("SELECT sign_count FROM webauthn_credential") == 1

    one, two = app(), app()
    ceremonies = []
    for client in (one, two):
        opened = await client.post("/api/auth/passkey/login/options", json={})
        assert opened.status_code == 200, opened.text
        ceremonies.append(opened.json())
    challenges = [
        webauthn.base64url_to_bytes(ceremony["options"]["challenge"]) for ceremony in ceremonies
    ]
    # The key and its clone: two valid signatures, both reporting counter 2.
    assertions = [
        device.authenticate(challenges[0]),
        device.authenticate(challenges[1], advance=False),
    ]
    assert device.sign_count == 2

    barrier = asyncio.Barrier(2)
    read_row = asyncpg.Connection.fetchrow

    async def held(self, query, *args, **kwargs):
        row = await read_row(self, query, *args, **kwargs)
        if "FROM webauthn_credential c JOIN app_user u" in query:
            await asyncio.wait_for(barrier.wait(), 10)
        return row

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", held)

    results = await asyncio.gather(
        *(
            client.post(
                "/api/auth/passkey/login",
                json={"ceremony_id": ceremony["ceremony_id"], "credential": assertion},
            )
            for client, ceremony, assertion in zip(
                (one, two), ceremonies, assertions, strict=True
            )
        )
    )
    assert sorted(response.status_code for response in results) == [200, 401], (
        f"both assertions read sign_count 1 and carried 2; the row must pick one — "
        f"{[response.status_code for response in results]}"
    )
    assert await db.fetchval("SELECT sign_count FROM webauthn_credential") == 2


async def test_a_synced_passkey_that_never_counts_signs_in_every_time(db, app, device):
    """iCloud and Google report 0 forever; the replay guard is the single-use challenge."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    device.sign_count = 0
    await db.execute("UPDATE webauthn_credential SET sign_count = 0")
    await admin.post("/api/auth/logout")

    for attempt in range(3):
        signed_in = await _passkey_login(admin, device, "patrick", advance=False)
        assert signed_in.status_code == 200, f"assertion {attempt + 1}: {signed_in.text}"
        await admin.post("/api/auth/logout")


def _session_cookie(response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{auth.SESSION_COOKIE}="):
            return header
    return None


async def test_the_cookie_carries_the_window_and_is_re_issued_at_most_once_a_day(db, app):
    """`max_age`, not `expires`: an absolute date drifts
    with the phone's clock. Time moves through the row."""
    window = f"Max-Age={settings().session_days * 24 * 3600}"
    admin = await _admin(app)
    signed_in = await admin.post(
        "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    minted = _session_cookie(signed_in)
    assert minted is not None and window in minted, f"login minted: {minted}"

    # A day has passed, so the next request slides the row and the cookie goes with it.
    await db.execute("UPDATE auth_session SET last_seen_at = now() - interval '25 hours'")
    first = await admin.get("/api/auth/me")
    # ...and a few hours later, inside the same day, neither moves.
    await db.execute("UPDATE auth_session SET last_seen_at = now() - interval '4 hours'")
    second = await admin.get("/api/auth/me")
    assert first.status_code == second.status_code == 200

    issued = [cookie for cookie in (_session_cookie(first), _session_cookie(second)) if cookie]
    assert len(issued) == 1, (
        f"two requests four hours apart re-issued {len(issued)} cookies; §3.2's slide is once"
        " a day in the row and in the cookie alike"
    )
    assert window in issued[0], f"the slide re-issued: {issued[0]}"


async def test_a_login_destroys_the_session_its_own_cookie_named_and_a_refusal_does_not(db, app):
    """Destroying the named session before the credential check would let a guesser sign a device out."""
    admin = await _admin(app)
    held = await db.fetchval("SELECT id FROM auth_session")

    refused = await admin.post(
        "/api/auth/login", json={"name": "patrick", "password": "not-the-password"}
    )
    assert refused.status_code == 401
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 1, (
        "a wrong password must not be able to sign a device out"
    )

    signed_in = await admin.post(
        "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 0
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 1


async def test_a_passkey_login_destroys_the_session_its_own_cookie_named(db, app, device):
    """Both doors must be asserted: they are the same six lines in two modules."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    held = await db.fetchval("SELECT id FROM auth_session")

    opened = await admin.post("/api/auth/passkey/login/options", json={"name": "patrick"})
    refused = await admin.post(
        "/api/auth/passkey/login",
        json={
            "ceremony_id": opened.json()["ceremony_id"],
            # A challenge the server never issued: a ceremony that fails at verification.
            "credential": device.authenticate(b"a challenge nobody issued".ljust(32, b"-")),
        },
    )
    assert refused.status_code == 401, refused.text
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 1

    signed_in = await _passkey_login(admin, device, "patrick")
    assert signed_in.status_code == 200, signed_in.text
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 0
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 1


async def test_a_password_change_rotates_the_session_it_was_made_from(db, app):
    """Decision 208: the session the leaked password opened must stop working."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    other_device = app()
    elsewhere = await other_device.post(
        "/api/auth/login",
        json={"name": "jenny", "password": MEMBER_PASSWORD, "device_label": "the kitchen tablet"},
    )
    assert elsewhere.status_code == 200, elsewhere.text
    arrived = auth.open_session_cookie(member.cookies.get(auth.SESSION_COOKIE))
    await db.execute(
        "UPDATE auth_session SET device_label = $2 WHERE id = $1", arrived, "jenny's phone"
    )

    changed = await member.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["sessions_revoked"] == 1, (
        "`sessions_revoked` names the other devices ended, and the rotation must not be counted"
        f" into it: {changed.json()}"
    )

    issued = [
        header
        for header in changed.headers.get_list("set-cookie")
        if header.startswith(f"{auth.SESSION_COOKIE}=")
    ]
    assert len(issued) == 1, f"one rotation, one cookie; this response carried {issued}"
    assert f"Max-Age={settings().session_days * 24 * 3600}" in issued[0], issued[0]
    minted = auth.open_session_cookie(member.cookies.get(auth.SESSION_COOKIE))
    assert minted is not None and minted != arrived, "the jar still names the rotated session"

    rows = await db.fetch("SELECT id, device_label FROM auth_session WHERE user_id = $1", jenny)
    assert [row["id"] for row in rows] == [minted], (
        f"one device, one row: {[row['id'] for row in rows]} against the minted {minted}"
    )
    assert rows[0]["device_label"] == "jenny's phone", (
        f"the rotation lost the label that describes the device: {rows[0]['device_label']!r}"
    )

    # Asserted on a second jar, because the caller's own jar has moved on.
    stale = app()
    stale.cookies.set(auth.SESSION_COOKIE, auth.seal_session_id(arrived))
    assert (await stale.get("/api/auth/me")).status_code == 401, (
        "the session the old password opened still authenticates after the change"
    )
    # Decision 179 sends the forced first-login flow here next.
    back = await member.get("/api/auth/me")
    assert back.status_code == 200, back.text
    assert back.json()["name"] == "jenny"


async def test_the_rotation_replaces_the_cookie_the_slide_earned_rather_than_adding_to_it(db, app):
    """Two `Set-Cookie`s for one name would leave the window to header order."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    # Aged after that read, because the read would otherwise slide the row it is about.
    await db.execute("UPDATE auth_session SET last_seen_at = now() - interval '25 hours'")

    changed = await member.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert changed.status_code == 200, changed.text
    issued = [
        header
        for header in changed.headers.get_list("set-cookie")
        if header.startswith(f"{auth.SESSION_COOKIE}=")
    ]
    assert len(issued) == 1, f"the slide and the rotation both answered: {issued}"
    live = await db.fetchval("SELECT id FROM auth_session WHERE user_id = $1", jenny)
    assert auth.open_session_cookie(member.cookies.get(auth.SESSION_COOKIE)) == live
    assert (await member.get("/api/auth/me")).status_code == 200


async def test_a_password_change_from_a_pin_session_is_still_a_pin_session(db, app):
    """Minting 'password' would let a PIN session stamp the admin clock and register a passkey."""
    admin = await _admin(app)
    member = await _member(app, admin)
    phone, jenny = await _switched_in(admin, member)

    changed = await phone.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert changed.status_code == 200, changed.text
    rows = await db.fetch(
        "SELECT auth_method, admin_verified_at FROM auth_session WHERE user_id = $1", jenny
    )
    assert [row["auth_method"] for row in rows] == ["pin"], (
        f"the switched-in phone walked out of the rotation holding: {[r['auth_method'] for r in rows]}"
    )
    assert rows[0]["admin_verified_at"] is None, "§3.2's re-prompt cannot be answered by a PIN"
    refused = await phone.post(
        "/api/auth/pin", json={"pin": "1111", "current_password": "a-brand-new-password"}
    )
    assert refused.status_code == 403, f"a PIN session minted a credential: {refused.text}"


async def test_a_rotation_that_fails_leaves_the_device_holding_the_session_it_arrived_with(
    db, app, monkeypatch
):
    """The rotation's DELETE must not commit before its INSERT."""

    def boom(*_args, **_kwargs):
        raise asyncpg.PostgresError("the rotation's session insert failed")

    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    other_device = app()
    assert (
        await other_device.post(
            "/api/auth/login", json={"name": "jenny", "password": MEMBER_PASSWORD}
        )
    ).status_code == 200
    arrived = auth.open_session_cookie(member.cookies.get(auth.SESSION_COOKIE))

    monkeypatch.setattr(auth, "create_session", boom)
    failed = await member.post(
        "/api/auth/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert failed.status_code == 500

    held = await member.get("/api/auth/me")
    assert held.status_code == 200, "a failed rotation signed the device out"
    assert auth.open_session_cookie(member.cookies.get(auth.SESSION_COOKIE)) == arrived
    stored = await db.fetchval("SELECT password_hash FROM app_user WHERE id = $1", jenny)
    assert auth.verify_password(stored, MEMBER_PASSWORD), "the old password must still stand"
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE user_id = $1", jenny) == 2, (
        "the revoke rolled back with the rotation, so the other device is still signed in"
    )


async def test_the_setup_state_an_anonymous_caller_sees_is_two_fields(app):
    """The full payload fingerprints the install; `required` is the same bit either way."""
    anonymous = app()
    virgin = await anonymous.get("/api/setup/state")
    assert set(virgin.json()) == {"required", "note"}
    assert virgin.json()["required"] is True

    admin = await _admin(app)
    after = await anonymous.get("/api/setup/state")
    assert set(after.json()) == {"required", "note"}
    assert after.json()["required"] is False

    signed_in = (await admin.get("/api/setup/state")).json()
    assert set(signed_in) == {"required", "steps", "has_admin", "member_count", "bundle", "note"}
    assert signed_in["has_admin"] is True


async def test_a_lapsed_session_still_gets_the_anonymous_state_and_keeps_its_cookie(db, app):
    """A clear-by-name on a 200 could end the session the browser had just moved to."""
    admin = await _admin(app)
    held = admin.cookies.get(auth.SESSION_COOKIE)
    await db.execute("DELETE FROM auth_session")
    lapsed = await admin.get("/api/setup/state")
    assert lapsed.status_code == 200
    assert set(lapsed.json()) == {"required", "note"}
    assert "set-cookie" not in lapsed.headers
    assert admin.cookies.get(auth.SESSION_COOKIE) == held


async def test_open_sign_ins_are_capped_and_expired_ones_swept_in_the_same_call(
    db, app, device, monkeypatch
):
    """Eviction, not refusal: refusing the 21st made a flood a
    household-wide passkey denial. The cap is patched down."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")

    # Sized so a burst cannot reach it by accident.
    assert webauthn.MAX_OPEN_SIGN_INS >= 200, (
        "the cap is not a security boundary — an anonymous endpoint that allocates state needs a "
        "per-caller limit at the ingress (§2, M4.7) — so its whole job is to be unreachable by "
        "accident while still bounding the table"
    )
    cap = 5
    monkeypatch.setattr(webauthn, "MAX_OPEN_SIGN_INS", cap)

    anonymous = app()
    burst = cap * 3
    for attempt in range(burst):
        opened = await anonymous.post("/api/auth/passkey/login/options", json={})
        assert opened.status_code == 200, f"burst call {attempt}: {opened.text}"
    held = await db.fetchval(
        "SELECT count(*) FROM webauthn_challenge WHERE purpose = 'authenticate'"
    )
    assert held == cap, f"{burst} anonymous calls left {held} open challenges, cap {cap}"

    signed_in = await _passkey_login(admin, device, "patrick")
    assert signed_in.status_code == 200, (
        f"a household passkey sign-in was denied by an anonymous burst: {signed_in.text}"
    )

    await db.execute("UPDATE webauthn_challenge SET expires_at = now() - interval '1 minute'")
    assert (await anonymous.post("/api/auth/passkey/login/options", json={})).status_code == 200
    assert await db.fetchval("SELECT count(*) FROM webauthn_challenge") == 1


async def test_a_malformed_credential_is_refused_before_the_challenge_is_spent(db, app):
    """The model makes it a 422 before the single-use challenge is spent."""
    anonymous = app()
    options = await anonymous.post("/api/auth/passkey/login/options", json={})
    ceremony_id = options.json()["ceremony_id"]

    refused = await anonymous.post(
        "/api/auth/passkey/login", json={"ceremony_id": ceremony_id, "credential": {"id": "x"}}
    )
    assert refused.status_code == 422
    kept = await db.fetchval(
        "SELECT count(*) FROM webauthn_challenge WHERE id = $1", ceremony_id
    )
    assert kept == 1
