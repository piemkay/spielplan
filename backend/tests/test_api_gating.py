"""Role gating and identity, at the route. Spec v2.1 §3.1, §3.2, §6.6.

`test_auth_logic.py` proves the predicates and `test_auth_integration.py` proves the rows.
This proves the *wiring*: that every admin path in the running app is actually behind the
dependency, that the navigation payload a member receives does not contain the admin entries
at all, and that the PIN switch changes who you are only when the PIN is right.

The admin-route test enumerates paths from the app's own OpenAPI schema rather than a list
kept by hand. A list would be correct on the day it was written and would silently stop
covering the route added the following week.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.app import create_app
from spielplan.core import auth

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"


def admin_paths() -> list[tuple[str, str]]:
    """Every (method, path) under /api/admin, straight from the app's schema."""
    schema = create_app().openapi()
    return sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/admin")
        for method in operations
        if method in ("get", "post", "put", "delete", "patch")
    )


def concrete(path: str) -> str:
    """Fill path parameters with values that exist nowhere — the gate must fire first."""
    return path.replace("{user_id}", "999999").replace("{title_id}", "999999")


async def _bootstrap(app):
    """An admin and a member, each on their own client."""
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201

    made = await admin.post("/api/setup/members", json={"name": "jenny", "role": "member"})
    assert made.status_code == 201
    otp = made.json()["one_time_password"]

    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.status_code == 200
    # §3.1: the account is locked to a password change; clear it so later 403s are about role.
    changed = await member.post(
        "/api/auth/password",
        json={"current_password": otp, "new_password": MEMBER_PASSWORD},
    )
    assert changed.status_code == 200
    return admin, member


# --- §6.6: admin routes are admin-only ----------------------------------------------------


def test_the_app_actually_has_admin_routes_to_gate():
    """A guard against the enumeration silently covering nothing."""
    assert len(admin_paths()) >= 8


@pytest.mark.parametrize(("method", "path"), admin_paths(), ids=lambda v: str(v))
async def test_every_admin_route_refuses_a_member(app, method, path):
    _admin, member = await _bootstrap(app)
    response = await member.request(method, concrete(path), json={})
    assert response.status_code == 403, f"{method} {path} let a member through"


@pytest.mark.parametrize(("method", "path"), admin_paths(), ids=lambda v: str(v))
async def test_every_admin_route_refuses_a_signed_out_caller(app, method, path):
    anonymous = app()
    response = await anonymous.request(method, concrete(path), json={})
    assert response.status_code == 401


# --- §6.6 / §3.1: the navigation payload ---------------------------------------------------


async def test_a_member_receives_no_admin_entry_in_its_navigation(app):
    """"Hidden, not merely disabled": the entry must be absent from what the browser receives.
    A client-side role check hides a link from someone looking at the screen and shows it to
    anyone looking at the response — and the prototype hardcoded the capability flag to true."""
    _admin, member = await _bootstrap(app)
    payload = (await member.get("/api/auth/me")).json()
    keys = {entry["key"] for entry in payload["nav"]["account"]}
    assert keys == {"account", "taste"}
    assert "admin" not in str(payload["nav"])


async def test_an_admin_receives_the_admin_entries(app):
    admin, _member = await _bootstrap(app)
    payload = (await admin.get("/api/auth/me")).json()
    keys = {entry["key"] for entry in payload["nav"]["account"]}
    assert {"admin", "setup"} <= keys


async def test_both_roles_see_every_surface(app):
    """§6: the six surface names are normative and none of them is role-gated."""
    admin, member = await _bootstrap(app)
    for client in (admin, member):
        payload = (await client.get("/api/auth/me")).json()
        assert [s["key"] for s in payload["nav"]["surfaces"]] == [
            "home", "rate", "tonight", "rank", "map", "taste"
        ]


# --- §3.2: the 24-hour admin re-prompt ------------------------------------------------------


async def test_a_stale_admin_session_is_re_prompted(db, app):
    """§3.2: "admin routes re-prompt after 24 h"."""
    admin, _member = await _bootstrap(app)
    assert (await admin.get("/api/admin/users")).status_code == 200

    await db.execute(
        "UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'"
    )
    stale = await admin.get("/api/admin/users")
    assert stale.status_code == 401
    assert stale.headers.get("X-Spielplan-Reauth") == "admin"

    # Signing in again with the password is what clears it — that is the whole re-prompt.
    await admin.post("/api/auth/login", json={"name": "patrick", "password": ADMIN_PASSWORD})
    assert (await admin.get("/api/admin/users")).status_code == 200


async def test_the_me_payload_reports_the_re_prompt(app, db):
    """The shell has to know before it renders the admin link, not after a 401."""
    admin, _member = await _bootstrap(app)
    await db.execute("UPDATE auth_session SET admin_verified_at = now() - interval '25 hours'")
    assert (await admin.get("/api/auth/me")).json()["admin_reauth_required"] is True


# --- §3.2: the PIN switch -------------------------------------------------------------------


async def test_a_wrong_pin_leaves_the_session_identity_unchanged(db, app):
    """§3.2: the account chip "switches between member profiles, gated by the per-user PIN"."""
    admin, member = await _bootstrap(app)
    await member.post("/api/auth/pin", json={"pin": "4821"})
    jenny = (await member.get("/api/auth/me")).json()["id"]

    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    assert refused.status_code == 401
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


async def test_a_correct_pin_switches_the_session(app):
    admin, member = await _bootstrap(app)
    await member.post("/api/auth/pin", json={"pin": "4821"})
    jenny = (await member.get("/api/auth/me")).json()["id"]

    switched = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert switched.status_code == 200
    me = (await admin.get("/api/auth/me")).json()
    assert me["name"] == "jenny"
    assert me["auth_method"] == "pin"
    # The device was handed over: the admin session it held does not travel with it.
    assert "admin" not in {entry["key"] for entry in me["nav"]["account"]}


async def test_switching_to_an_account_with_no_pin_is_refused(app):
    """The chip only offers profiles that set one — otherwise it is a door with no lock."""
    admin, member = await _bootstrap(app)
    jenny = (await member.get("/api/auth/me")).json()["id"]
    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401
    assert "no switch PIN" in refused.json()["detail"]


async def test_the_switch_list_only_names_profiles_with_a_pin(app):
    admin, member = await _bootstrap(app)
    assert (await admin.get("/api/auth/switchable")).json() == []
    await member.post("/api/auth/pin", json={"pin": "4821"})
    assert [u["name"] for u in (await admin.get("/api/auth/switchable")).json()] == ["jenny"]


async def test_the_switch_route_refuses_an_anonymous_caller(app):
    """A 4-digit PIN accepted from nobody in particular would be the entire authentication
    story for every account that set one — 10,000 guesses against an ungated route."""
    _admin, member = await _bootstrap(app)
    await member.post("/api/auth/pin", json={"pin": "4821"})
    jenny = (await member.get("/api/auth/me")).json()["id"]

    anonymous = app()
    refused = await anonymous.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401


async def test_a_locked_out_account_refuses_even_the_right_pin(db, app):
    admin, member = await _bootstrap(app)
    await member.post("/api/auth/pin", json={"pin": "4821"})
    jenny = (await member.get("/api/auth/me")).json()["id"]

    for _ in range(auth.PIN_ATTEMPT_LIMIT):
        await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401
    assert "too many attempts" in refused.json()["detail"]
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


# --- §3.1: the forced first-login change gates everything else -------------------------------


async def test_a_locked_account_cannot_reach_the_app(app):
    """§3.1: an account created with a one-time password is locked to a password change, and
    the auth layer enforces it — not the UI."""
    admin = app()
    await admin.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD})
    otp = (
        await admin.post("/api/setup/members", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]

    member = app()
    await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert (await member.get("/api/titles")).status_code == 403
    assert (await member.get("/api/prompts/finish")).status_code == 403
    # …but /me and the password route stay reachable, or there would be no way out.
    assert (await member.get("/api/auth/me")).status_code == 200


# --- §3.2: the refusal must not leak which names exist ---------------------------------------


async def test_a_wrong_name_and_a_wrong_password_cost_the_same(app):
    """One message for both cases is not enough on its own. argon2 costs tens of milliseconds
    and an index miss costs none, so short-circuiting on "no such account" answers in the
    timing what the body refuses to say — and the names it leaks are exactly the ones worth
    guessing passwords against.
    """
    import time

    admin, _member = await _bootstrap(app)

    async def elapsed(name: str) -> float:
        # Two calls, take the faster: this box also runs Postgres and a browser.
        best = float("inf")
        for _ in range(2):
            start = time.perf_counter()
            response = await admin.post(
                "/api/auth/login", json={"name": name, "password": "definitely-wrong"}
            )
            assert response.status_code == 401
            best = min(best, time.perf_counter() - start)
        return best

    existing = await elapsed("patrick")
    absent = await elapsed("nobody-lives-here")
    assert absent > existing * 0.5, (
        f"a missing name answered in {absent * 1000:.0f}ms against {existing * 1000:.0f}ms "
        "for an existing one — the difference is an account-enumeration oracle"
    )
