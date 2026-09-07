"""M4.6's account-security rows, at the route. Spec v2.1 §3.1, §3.2, §6.6, §2; decisions 164, 166.

`test_admin_users.py` proves §6.6's lifecycle and its three floors. This file proves the rules
that are *about* the account rather than about the roster: what a PIN session may not do, how a
stale admin stamp is cleared, what repeated guesses cost, what an anonymous caller may see, and
that the writes behind all of it are atomic. They live together because each one is a claim
about an account boundary and none of them belongs to a single router — the two-roles rule is
asserted at a route, in the schema and over the source; the PIN demotion spans auth, passkeys
and push; the anonymous bound spans setup and the WebAuthn ceremonies.

Every one of these was a defect the September 2026 pre-release review found, so each test names
the behaviour that stood before it rather than only the rule that replaces it.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import inspect

import asyncpg
import pytest

from spielplan.app import create_app
from spielplan.core import auth, webauthn
from spielplan.core.config import settings
from tests.fixtures.soft_authenticator import SoftAuthenticator

# The dependency walk and the path-parameter filler, from the file whose subject they are
# (`test_api_gating.py`'s module docstring: it proves the wiring). Imported rather than copied,
# because a second walker is a second thing to keep true of the same app.
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
    """A created account past §3.1's forced first-login change, on its own client."""
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
    """A session minted by §3.2's PIN switch: the handed-over-phone case, on the admin's client."""
    set_pin = await member.post(
        "/api/auth/pin", json={"pin": MEMBER_PIN, "current_password": MEMBER_PASSWORD}
    )
    assert set_pin.status_code == 200, set_pin.text
    jenny = (await member.get("/api/auth/me")).json()["id"]
    switched = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": MEMBER_PIN})
    assert switched.status_code == 200, switched.text
    assert (await admin.get("/api/auth/me")).json()["auth_method"] == "pin"
    return admin, jenny


# --- decision 166: two roles, and one place accounts are made -------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/admin/users", {"name": "ghost", "role": "guest"}),
        ("PATCH", "/api/admin/users/{user_id}", {"role": "guest"}),
    ],
    ids=("create", "re-role"),
)
async def test_a_guest_role_is_refused_at_every_route_that_writes_one(app, method, path, body):
    """Decision 166: "There is no guest account." A guest is a Tonight session seat with
    `user_id NULL` (§4.2), so the role never belonged on `app_user` — and `MemberInit` admitted
    it, issued it a one-time password, and the resulting account read the household roster and
    could PIN-switch. 422 from the schema, not a hand-written check: the route's `Literal` is
    the same rule migration 0016's CHECK states one layer down."""
    admin = await _admin(app)
    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    refused = await admin.request(method, path.format(user_id=made.json()["id"]), json=body)
    assert refused.status_code == 422, refused.text


async def test_the_app_user_check_admits_exactly_two_roles(db):
    """Migration 0016 narrows `0002_users.sql`'s three-value CHECK to decision 166's two. The
    route can only be as good as the column: an importer, a fixture or a psql session that
    wrote 'guest' would otherwise make an account no route can make."""
    for role in ("admin", "member"):
        written = await db.fetchval(
            "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING role", role, role
        )
        assert written == role
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO app_user (name, role) VALUES ('ghost', 'guest')")


async def test_account_creation_exists_at_exactly_one_path_besides_first_boot(app):
    """§6.6 is "the only place accounts are made" (decision 166), and the wizard's member step
    is gone with decision 164 — so the claim is checkable rather than aspirational.

    A walk of the app's *routes*, which is what the claim is about. This globbed `api/*.py` for
    the literal INSERT and compared file names, and that is a weaker claim wearing the same
    words: a second minting route added inside `admin.py` or `setup.py` — the two files already
    on the expected list — passed silently, and so would anything under an `api/` subpackage the
    non-recursive glob never opened. Walking `create_app()` enumerates endpoints, so a second
    route is a second entry however it is filed.

    `api/setup.py`'s INSERT is §3.1's first boot, which refuses once an admin exists and is
    therefore not a second place to make accounts; `api/admin.py`'s is §6.6's. A third is a hole.
    """
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
    """The walk above reads source; this one fires. Between them the row's claim is both halves
    of what "exists at exactly one path" means — no other route says the words, and no other
    route does the thing.

    Every mounted route is asked, as the most privileged caller there is, with the body §6.6's
    creation route takes. A route that mints under some other body or some other role is not
    reached here — that is the source walk's half — but a second minting route wired the way the
    first one is shows up as a second entry, and only `app_user` rows count, so the sessions,
    setup steps and observations the sweep leaves behind are beside the point.
    """
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
        # `/api/auth/logout` ends the session it is called on, and the routes after it in the
        # sweep would then all be probed as an anonymous caller — a sweep that reaches nothing
        # and reports a pass. Signing back in is cheaper than excluding it by name.
        if (await admin.get("/api/auth/me")).status_code != 200:
            back = await admin.post(
                "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
            )
            assert back.status_code == 200, back.text

    assert minted == {("POST", "/api/admin/users"): 1}, (
        f"accounts were created outside §6.6's one route: {sorted(minted)}"
    )


# --- §3.2: a PIN session is a convenience, not a credential ---------------------------------


async def test_a_pin_session_is_refused_every_route_that_mints_a_credential(app):
    """§3.2 makes the password the account credential and the PIN "for fast user-switching on a
    shared device". A switched-in session that could register a passkey would turn four digits
    into a permanent credential — and on an admin account that passkey is exempt from the 24 h
    re-prompt, so the chain ends at an unprompted admin surface. It stops at the first step."""
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

    # Reading is not minting: §3.2 hands the phone over on purpose, and what this member's own
    # device already holds is not disclosed by listing it.
    assert (await phone.get("/api/auth/passkey/credentials")).status_code == 200
    assert (await phone.get("/api/push/state")).status_code == 200
    # And the product surfaces are exactly what the switch is for: Rate, Rank and Tonight are
    # the reason §3.2 hands the phone over at all.
    for path in ("/api/rate/balance", "/api/rank/tiers", "/api/tonight/rooms"):
        assert (await phone.get(path)).status_code == 200, path


async def test_a_pin_session_on_an_admin_account_never_reaches_the_admin_surface(db, app):
    """The other end of the same chain. `create_session`'s CASE excludes `auth_method='pin'`
    from the stamp, so a switch cannot produce a session §3.2's re-prompt considers fresh."""
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
    """Decision 170. The PIN is derived from the account credential, so setting one costs the
    credential — an unlocked phone left on a table was enough before. The counters clear in the
    same UPDATE that writes the new hash: the old PIN is gone, so the failures against it are
    not evidence about the new one."""
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


# --- §3.2: the 24 h admin re-prompt is cleared in place -------------------------------------


async def _go_stale(db) -> None:
    await db.execute("UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'")


async def test_reauth_clears_the_stamp_on_the_session_in_hand(db, app):
    """§3.2's re-prompt with somewhere to go. `admin_verified_at` was written at INSERT and
    nowhere else, so the only way past a stale stamp was to sign in again — a second session row
    per cycle, the first left live for its full sliding 90 days, and the shell's only affordance
    a link to /login. This clears the clock and mints nothing."""
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
    """A member has nothing to re-authenticate *to* (403), and a PIN session cannot be upgraded
    into one however good the password typed into it is — §3.2 makes the switch a convenience on
    a device someone else is already signed in on."""
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


# --- §3.2 / §2: the login throttle, and what the login body may contain ---------------------


async def test_repeated_wrong_passwords_lock_the_account_with_the_same_refusal(db, app):
    """§2 puts this origin on Tailscale, where Cloudflare's rate limit is not in front of
    anything, on a 4 vCPU box. Only the PIN path had a lockout: a password could be guessed at
    for as long as anyone cared to. The refusal never changes wording — "this account is locked"
    tells the guesser the name is worth guessing at."""
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

    # A refusal made while the lockout stands does not extend it — otherwise a guesser holds
    # the owner out for as long as they keep guessing.
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
    """The other reset §3.2 needs. An account locked out by guesses, whose owner then changes
    the password from an admin-issued one-time password, must not stay locked against the
    credential that replaced the one being guessed at.

    The lockout stands against this route too, which is what routing it through
    `check_password` means: the count is the credential's, not the front door's, and a route
    that verifies the password without honouring the count is where the guessing moves. So the
    live window refuses the change exactly as it refuses a login, and what the change clears is
    the count left behind when the window has lapsed — which is the state the owner actually
    finds the account in on coming back to it.
    """
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


# The four routes that verify `app_user.password_hash`. Each carries its own status code and
# its own wording; what they share is the counter, and until M4.6's second review only the
# first of them armed it.
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
    """§3.2's lockout is a property of the credential, so it cannot live at one door.

    `login` counted; `/api/auth/password`, `/api/auth/reauth` and `/api/auth/pin` verified the
    same `app_user.password_hash` with a bare `verify_password_async` and wrote nothing. 60
    guesses through the first of those left `password_failed_count` at 0 and the front door
    still answering the correct password — an unthrottled, unlogged oracle for anyone holding
    any session on the account, including the four-digit PIN switch §3.2 calls a convenience.

    Parametrised one route per case, so that a single door losing its counter fails on its own
    name rather than being carried by the other three.
    """
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

    # The count is only worth writing if it arms the lockout the front door honours: this is
    # the last step of the measured chain, where the correct password stops being accepted.
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
    """sec-04: a NUL byte in a name reached Postgres and answered 500 — asyncpg is right to
    refuse text Postgres cannot store, and the place to answer for it is the edge. sec-02: an
    unbounded body let an anonymous caller choose how much of §2's 4 vCPU box each argon2
    attempt cost."""
    anonymous = app()
    assert (await anonymous.post("/api/auth/login", json=body)).status_code == 422


async def test_a_name_that_differs_only_by_surrounding_whitespace_still_signs_in(db, app):
    """as-11: creation accepted ' Tom ' and the unique index is on `lower(name)`, so the account
    existed under a name whose spaces nobody can see and nobody retypes. The bound at creation
    stops new ones; the rows already made stay reachable only if both ends are trimmed."""
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
    r"""as-11: `SetPinRequest` carried `\d`, which pydantic reads as Unicode-aware, so these were
    accepted and argon2-hashed — while `PinSwitchRequest` carried no pattern at all. §3.2's PIN
    is four digits on a numeric keypad; both ends now say the same thing."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    setting = await member.post(
        "/api/auth/pin", json={"pin": pin, "current_password": MEMBER_PASSWORD}
    )
    assert setting.status_code == 422
    switching = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": pin})
    assert switching.status_code == 422


# --- §3.1 / §4.2: the writes behind an account are atomic -----------------------------------


async def test_a_first_boot_that_fails_part_way_leaves_no_row_behind(db, app, monkeypatch):
    """as04. The account, the wizard step and the session are three writes; unserialised and
    uncommitted-together, a failure between them left an admin row whose password had been typed
    once into a form that errored, while `/state` already reported `required=false`. That is a
    first boot which can never be re-run, on the one flow a household runs exactly once."""

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
    """The check and the INSERT were three autocommit statements with no lock, so two submits
    with different names each passed the count check and each became "the only admin" —
    decision 166's floor breached at the moment the household is created."""
    one, two = app(), app()
    # A warmed pool, or the loser's first connection cost is what serialises them rather than
    # the advisory lock this test is about — and warming the two clients one after the other is
    # itself that serialisation, which is why this test used to pass with the lock removed. The
    # warm-up is concurrent so that both pool connections are open before either POST starts and
    # `pg_advisory_xact_lock` is the only thing left that can order them.
    await asyncio.gather(one.get("/api/health"), two.get("/api/health"))

    results = await asyncio.gather(
        one.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}),
        two.post("/api/setup/admin", json={"name": "jenny", "password": ADMIN_PASSWORD}),
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    assert await db.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") == 1
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 1


async def test_a_duplicate_name_on_first_boot_is_a_conflict_and_rolls_back(db, app):
    """`app_user_name_key` is the race the pre-read cannot see, and without the catch it reached
    `app.py`'s generic handler as "database error", 500 — on the wizard's first screen."""
    admin = await _admin(app)
    await admin.post("/api/admin/users", json={"name": "sam", "role": "member"})
    # Back to a state the wizard would run in, with a member row already holding the name. The
    # `setup_step` row the first boot wrote stays: what this asserts is that the refused attempt
    # adds nothing, not that the table is empty.
    await db.execute("DELETE FROM app_user WHERE role = 'admin'")
    steps_before = await db.fetchval("SELECT count(*) FROM setup_step")

    clash = await app().post("/api/setup/admin", json={"name": "sam", "password": ADMIN_PASSWORD})
    assert clash.status_code == 409
    assert "already exists" in clash.json()["detail"]
    assert await db.fetchval("SELECT count(*) FROM app_user") == 1
    assert await db.fetchval("SELECT count(*) FROM setup_step") == steps_before
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 0


async def test_a_password_change_that_fails_part_way_changes_nothing(db, app, monkeypatch):
    """§4.2. `POST /api/auth/password` is three writes — the hash, the lockout counters and the
    other sessions — and it used to issue them on the autocommit connection while its admin-side
    twin (`/api/admin/users/{id}/reset-password`) wrapped the identical three in `write_txn`.
    Uncommitted together, a failure on the third left the password changed and every other
    device signed in for its full sliding 90 days, which is the one thing the route promises not
    to do; the admin half, under the same failure, rolled back clean."""

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
    """The same shape as the change above, at the three routes that replace one session with
    another. dd24 has each of them destroy the session their own cookie names before minting
    the new one, and on the autocommit connection that DELETE committed before the INSERT was
    attempted: a `create_session` that failed answered 500 and signed the device out of the
    session it arrived holding — a state it had before the request and cannot get back.

    `/api/auth/switch` is the worst of the three. Decision 179 keeps it reachable precisely for
    the handed-over phone, and there the same failure left the device with no session at all.
    """

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


# --- §3.2 / §4.2: the slide and the cookie move together ------------------------------------


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
    """core/auth.py states §3.2's contract as "slides in the row and in the cookie together or
    not at all", and the slide broke it on every refusal: `load_session` commits the row on an
    autocommit connection before the route body runs, then Starlette builds its own error
    response and discards the one the dependency wrote the Set-Cookie onto. `last_seen_at` is
    fresh by then, so nothing that day re-issues it either — the browser's cookie ends the day
    a day staler than the row it names, and an admin whose first request of the day meets the
    24 h re-prompt hits this every time."""
    admin = await _admin(app)
    member = await _member(app, admin)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    # A session a day old with only ten days left to run, so the slide is both due and visible.
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

    # And the harm the invariant exists to prevent: there is no second chance later in the day.
    ok = await member.get("/api/auth/me")
    assert ok.status_code == 200
    assert "set-cookie" not in ok.headers


# --- §3.2 / §4.2: user verification is what stamps the admin clock --------------------------


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
    """§3.2's passkey is "Face ID / Touch ID / Android biometrics". A roaming key that was merely
    touched is a valid sign-in and is not that, so it must not satisfy the 24 h re-prompt — the
    stamp was written for every assertion, which made a tap on a security key the one credential
    in the app exempt from §3.2's admin prompt."""
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
    """§4.2 stores `sign_count` for the cloned-hardware-key case. py_webauthn compares against
    the value read a statement earlier, so two assertions in flight both landed; the conditional
    UPDATE is the check, and a refusal must leave the stored counter where it was."""
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
    """The case the conditional UPDATE exists for, and the one nothing was reaching.

    The test above measures py_webauthn, not the guard: it hands the library a stored counter
    the assertion does not beat, and `verify_authentication_response` raises three statements
    before the UPDATE — delete the `RETURNING 1` check and that test still passes. What the
    UPDATE is actually for is the cloned hardware key (core/webauthn.py's comment above it):
    two assertions in flight that both read the same pre-UPDATE `sign_count` and both carry a
    counter that beats it, which the library passes twice because it compares against the value
    each request read a statement earlier. Making the row the arbiter (§4.2) is what collapses
    that to one winner.

    The barrier is what makes this the race rather than a coin flip: both requests are held
    immediately after the SELECT that reads `sign_count` and released together, so the UPDATE
    is the only thing left that can order them. Under a timeout, because a barrier one side
    never reaches would otherwise hang the suite instead of failing it.
    """
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")
    # One uncontested assertion first, so the stored counter is non-zero: at zero the UPDATE
    # deliberately accepts a tie — the synced-passkey case the test below documents — and §4.2
    # stores the counter for the hardware key that does count.
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
    # The key, and its clone: two valid signatures over two different challenges, both reporting
    # counter 2. This is what a duplicated authenticator looks like on the wire.
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
    """The accepted case, documented rather than tolerated: iCloud Keychain and Google Password
    Manager report 0 forever, so every assertion of theirs ties the stored value. §3.2 targets
    exactly those, which is why the replay guard is the single-use challenge and not this."""
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


# --- §3.2 / dd24: the cookie's window, and one cookie naming one session --------------------


def _session_cookie(response) -> str | None:
    """The Set-Cookie this response carries for §3.2's session, or None if it re-issued none."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{auth.SESSION_COOKIE}="):
            return header
    return None


async def test_the_cookie_carries_the_window_and_is_re_issued_at_most_once_a_day(db, app):
    """§3.2's 90 days are the browser's too, and the slide costs one Set-Cookie a day.

    `max_age`, not `expires`: an absolute date is computed against the server's clock and read
    against the phone's, so a few minutes of drift expires a 90-day cookie early or keeps a dead
    one alive, while a relative age is the same window on both sides. Nothing asserted the
    header at all — a swap to `expires`, or a slide that re-issued on every request in a page's
    fan-out, would both have gone out unremarked.

    now() moves through the session row rather than through the clock, because `load_session`'s
    window is `last_seen_at < now() - interval '1 day'`: ageing the row is the same experiment
    as waiting, and it is the one a test can run in a second.
    """
    window = f"Max-Age={settings().session_days * 24 * 3600}"
    admin = await _admin(app)
    signed_in = await admin.post(
        "/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    minted = _session_cookie(signed_in)
    assert minted is not None and window in minted, f"login minted: {minted}"

    # A day has passed, so the next request slides the row — and the cookie goes with it.
    await db.execute("UPDATE auth_session SET last_seen_at = now() - interval '25 hours'")
    first = await admin.get("/api/auth/me")
    # …and a few hours after that, still inside the same day, neither of them moves.
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
    """dd24: one cookie names one session row, and a device that signs in again — as a second
    person, or after losing track of its cookie — otherwise leaves the row it stops naming live
    for its full sliding 90 days, with nothing that can reach it and nothing that will end it.

    The refusal is the other half and it is the half with teeth: destroying the named session
    before the credential is checked would let anyone who can reach the origin sign a household
    device out by guessing at a password.
    """
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
    """dd24 again, at the other door. Step 13 shipped the same six lines in `api/auth.py` and
    `api/passkeys.py`, so the rule holds only if both are asserted — a passkey sign-in on a
    device that already holds a session is the ordinary case on a shared phone, not a corner."""
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    held = await db.fetchval("SELECT id FROM auth_session")

    opened = await admin.post("/api/auth/passkey/login/options", json={"name": "patrick"})
    refused = await admin.post(
        "/api/auth/passkey/login",
        json={
            "ceremony_id": opened.json()["ceremony_id"],
            # Signed over a challenge the server never issued: a ceremony that fails at
            # verification, which is where a device must keep the session it arrived with.
            "credential": device.authenticate(b"a challenge nobody issued".ljust(32, b"-")),
        },
    )
    assert refused.status_code == 401, refused.text
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 1

    signed_in = await _passkey_login(admin, device, "patrick")
    assert signed_in.status_code == 200, signed_in.text
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE id = $1", held) == 0
    assert await db.fetchval("SELECT count(*) FROM auth_session") == 1


# --- §14.4 / sec-14: what an anonymous caller may reach -------------------------------------


async def test_the_setup_state_an_anonymous_caller_sees_is_two_fields(app):
    """sec-14: the full payload fingerprints the install — how many members, which bundle
    version and when, how far the operator got — and §2 puts the origin on Tailscale, where
    Cloudflare's rate limit does not stand in front of a prober. `required` is the same bit
    either way, so the first-boot redirect still works before anyone can sign in."""
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


async def test_a_lapsed_session_still_gets_the_anonymous_state_and_loses_its_cookie(db, app):
    """The route is the one place both callers meet, so its optional dependency must not lose
    what `current_user` does on the way past: a dead cookie is cleared rather than left to be
    sent for the rest of its 90 days."""
    admin = await _admin(app)
    await db.execute("DELETE FROM auth_session")
    lapsed = await admin.get("/api/setup/state")
    assert lapsed.status_code == 200
    assert set(lapsed.json()) == {"required", "note"}
    assert not admin.cookies.get(auth.SESSION_COOKIE)


async def test_open_sign_ins_are_capped_and_expired_ones_swept_in_the_same_call(
    db, app, device, monkeypatch
):
    """The one anonymous write in the app. 60 calls left 60 rows until the hourly worker prune,
    which is a floor and not a ceiling — sweeping the same purpose inline makes the table's size
    a function of the five-minute TTL instead of the schedule.

    The cap is held by evicting the oldest rows, and the burst below is why. Refusing the 21st
    caller made a global count on an anonymous route into a household-wide denial: twenty
    unexpired rows, which the expiry sweep by definition does not touch, answered every passkey
    sign-in 429 for the full five-minute TTL and renewably for as long as a loop cared to keep
    them there — against §3.2's primary authentication, leaving only the password fallback the
    login throttle locks after five wrong guesses per name.

    THE CAP IS PATCHED DOWN HERE, and that is the point rather than a shortcut. What must hold
    is the MECHANISM — the table is bounded by eviction and a household sign-in still answers
    after a flood — and a test that bursts past the shipped 500 would spend a minute of the
    suite proving the same thing about a number the module already argues for separately. The
    shipped value is pinned below, so lowering it back to a reachable one still fails here.
    """
    admin = await _admin(app)
    user_id = (await admin.get("/api/auth/me")).json()["id"]
    await _register_for(db, user_id, device)
    await admin.post("/api/auth/logout")

    # Sized so a burst cannot reach it by accident: the flood must land inside the seconds
    # between a tap and a touch to displace anything, and the table stays trivially small.
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
    """sec-04: the body was a bare `dict`, so the ceremony consumed its single-use challenge and
    only then failed on the missing key — a 500 with a traceback, and the honest user's retry
    refused as expired too. The model makes it FastAPI's 422 before the route body runs."""
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
