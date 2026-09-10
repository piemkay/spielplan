"""Role gating and identity, at the route. Spec v2.1 §3.1, §3.2, §6.6.

`test_auth_logic.py` proves the predicates and `test_auth_integration.py` proves the rows.
This proves the *wiring*: that every admin path in the running app is actually behind the
dependency, that the navigation payload a member receives does not contain the admin entries
at all, and that the PIN switch changes who you are only when the PIN is right.

The admin-route test enumerates paths by walking the running app's dependency graph rather
than from a list kept by hand, and rather than from the `/api/admin` path prefix it used to
match: a list would be correct on the day it was written, and a prefix says nothing about what
a route is actually behind. `POST /api/setup/connectors` is admin-gated and lives outside the
prefix, so the sweep was not covering it — which is the shape of failure both alternatives
produce and neither reports.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import asyncpg
import httpx
import pytest

import spielplan.api
from spielplan.api import deps
from spielplan.app import create_app
from spielplan.core import auth
from spielplan.core.config import settings

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"


# The count the walk finds today. Stated as a number so that a router accidentally dropped
# from `create_app` — or a `deps.admin_user` quietly swapped for `ActiveUser` — fails here
# with an arithmetic complaint rather than passing a sweep over a shorter list.
#
# Equality rather than a floor, and the number is the walk's own: a floor set one below the
# real count tolerates exactly the loss it was raised to catch, and `>=` cannot tell a dropped
# gate from a number that was never right. Equality also fails when an admin route is *added*,
# which is wanted — re-stating the number by hand is how the two sweeps below become known to
# be covering every route someone meant to gate. What it still cannot see is a new route that
# never had `AdminUser` at all: a walk over gated routes has nothing to enumerate it with.
# 21 until M4.7 added §6.6's System card (`GET /api/admin/system`, decision 182), and 22 until
# M4.9 added the Data card's sources-and-terms list (`GET /api/admin/data/sources`, decisions
# 191 and 193 — the eleven frozen sources' licence terms, and §6.4's unauthored axis artifact as
# an outstanding task). Re-stated by hand, which is what the paragraph above says this number is
# for: the equality failing on the added route is the check working.
ADMIN_ROUTE_COUNT = 23

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def _routes(routes):
    """Every route object carrying a `dependant`, however deeply the app nests its routers.

    FastAPI 0.141 stops flattening `include_router`: an included router is one opaque route
    whose children are reached through `effective_candidates()`, with the prefixes already
    applied. Recursing rather than reading `app.routes` directly is what keeps this from
    finding one route and reporting it as a clean sweep.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _routes(candidates())
        elif getattr(route, "dependant", None) is not None:
            yield route
        else:
            yield from _routes(getattr(route, "routes", ()))


def _behind(dependant, target) -> bool:
    """Whether `target` is anywhere in this route's dependency tree.

    Recursive because nothing declares `admin_user` directly: it depends on `active_user`,
    which depends on `current_user`, and a route asks only for `AdminUser`.
    """
    return any(sub.call is target or _behind(sub, target) for sub in dependant.dependencies)


def paths_behind(target) -> set[tuple[str, str]]:
    """Every (method, path) in the running app whose dependency tree reaches `target`.

    A route that takes no auth dependency at all — `/api/setup/admin`, `/api/auth/login` — is
    behind nothing and so appears in none of these sets, which is how the anonymous surface
    excludes itself rather than by a hand-kept exemption.
    """
    return {
        (method, route.path)
        for route in _routes(create_app().routes)
        if _behind(route.dependant, target)
        for method in (route.methods or ())
        if method in METHODS
    }


def admin_paths() -> list[tuple[str, str]]:
    """Every (method, path) actually behind `deps.admin_user` in the running app.

    Not `path.startswith("/api/admin")`. The prefix is a naming convention and the gate is a
    dependency, so the two can disagree in both directions — and they do: `/api/setup/connectors`
    is admin-gated and outside the prefix, while `/api/setup/admin` takes no auth dependency at
    all and is excluded here automatically rather than by a hand-kept exemption.
    """
    return sorted(paths_behind(deps.admin_user))


def concrete(path: str) -> str:
    """Fill path parameters with values that exist nowhere — the gate must fire first.

    Every `{...}` rather than a named pair, because the locked sweep below walks routes the
    admin sweep never reaches and a parameter nobody listed would otherwise be probed as the
    literal string `{event_id}` — a request that still routes, and so reports a pass it did not
    earn on the day the route starts reading its parameter before the dependency runs.
    """
    return re.sub(r"\{[^}]+\}", "999999", path)


async def _bootstrap(app):
    """An admin and a member, each on their own client."""
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201

    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
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


def test_a_route_without_conn_still_holds_a_pooled_connection_for_its_session():
    """Dropping `conn: DB` from a handler is tidiness, never pool relief.

    `api/home.py:model_log` reads an in-process ring buffer and took a `conn` it never used;
    M4.9 removed the parameter and argued the removal saved an acquisition. It saved none.
    `ActiveUser` resolves `active_user` -> `current_user`, and `current_user` takes `conn: DB`
    (`api/deps.py:115`) to load the session; FastAPI caches a dependency per request, so the
    route's own parameter was a second reference to the connection the session load had already
    taken. `deps.db` holds it for the whole request by design ("One pooled connection for the
    whole request").

    Asserted on the dependency graph rather than on the docstring, so it is the WIRING that
    keeps the sentence honest: on the day `current_user` stops needing a connection this goes
    red, and the paragraph in `model_log` has to be rewritten rather than quietly become true
    by accident. §7.3's poll and §6.7's drawer are both surfaces a phone reopens repeatedly, and
    `max_size` is 10. [M4.9 review cycle 1: M49-HOME-02]
    """
    behind_db = paths_behind(deps.db)
    assert ("GET", "/api/model-log") in behind_db, (
        "the route that takes no `conn` is not behind `deps.db` either - if that is now true, "
        "`api/home.py:model_log`'s docstring says the opposite and is the thing to fix"
    )
    # Not a property of this one route: every authenticated route is behind it, because the
    # session load is. A sweep, so a future connection-free route cannot be read as free.
    behind_user = paths_behind(deps.active_user)
    assert behind_user <= behind_db, sorted(behind_user - behind_db)


def test_the_app_actually_has_admin_routes_to_gate():
    """A guard against the enumeration silently covering nothing."""
    found = admin_paths()
    assert len(found) == ADMIN_ROUTE_COUNT, (
        f"the dependency walk found {len(found)} admin-gated routes, not the "
        f"{ADMIN_ROUTE_COUNT} that exist - a gate was dropped and the sweep below is passing "
        f"over a short list, or a route was added and this count not re-stated: {found}"
    )


async def test_the_spa_fallback_does_not_answer_for_the_api_namespace(tmp_path):
    """The routing of a *built* app, which no other test in the suite exercises.

    `create_app()` mounts the SPA catch-all only when `SPIELPLAN_STATIC_DIR` names a directory,
    which the container sets (`ops/backend.Dockerfile`) and pytest does not — so every 404 this
    suite asserts on a deleted API route is asserted in the one configuration where the
    catch-all is absent. Registered as a GET, it used to *partially* match a POST to an unrouted
    `/api/...` path, and Starlette answers a partial match with 405: `POST /api/setup/members`,
    the route decision 164 deleted, answered 405 in the shipped app while answering 404 here,
    and no client could tell a deleted route from a mistyped verb.

    Three claims, because the fix is a decline and a decline can be too wide: nothing under
    `/api` reaches the shell, a real route still refuses a wrong verb with 405 rather than
    pretending it is not there, and a client-side route still gets the shell.
    """
    build = tmp_path / "static"
    (build / "_app").mkdir(parents=True)
    (build / "index.html").write_text("<html>shell</html>", encoding="utf-8")

    previous = os.environ.get("SPIELPLAN_STATIC_DIR")
    os.environ["SPIELPLAN_STATIC_DIR"] = str(build)
    settings.cache_clear()
    try:
        built = create_app()
        # No lifespan: every assertion below is decided by the router before a handler runs, and
        # the one that does run (the shell) reads a file. Nothing here touches the database.
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=built), base_url="http://test"
        )
        async with client:
            gone = await client.post("/api/setup/members", json={"name": "x", "role": "member"})
            assert gone.status_code == 404, (
                "a deleted API route must read as deleted in the app that ships, not as a "
                f"method mismatch: {gone.status_code}"
            )
            unknown = await client.get("/api/nope")
            assert unknown.status_code == 404
            assert "html" not in unknown.text.lower(), "an API path was answered with the shell"

            wrong_verb = await client.post("/api/auth/me")
            assert wrong_verb.status_code == 405, (
                "the decline must not swallow a real route: GET /api/auth/me exists, so a POST "
                "to it is a method mismatch and 405 is the honest answer"
            )

            client_route = await client.get("/rank")
            assert client_route.status_code == 200
            assert "shell" in client_route.text, "the fallback must still serve the SPA"
    finally:
        if previous is None:
            os.environ.pop("SPIELPLAN_STATIC_DIR", None)
        else:
            os.environ["SPIELPLAN_STATIC_DIR"] = previous
        settings.cache_clear()


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
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    assert refused.status_code == 401
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


async def test_a_correct_pin_switches_the_session(app):
    admin, member = await _bootstrap(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
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
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    assert [u["name"] for u in (await admin.get("/api/auth/switchable")).json()] == ["jenny"]


async def test_the_switch_route_refuses_an_anonymous_caller(app):
    """A 4-digit PIN accepted from nobody in particular would be the entire authentication
    story for every account that set one — 10,000 guesses against an ungated route."""
    _admin, member = await _bootstrap(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    anonymous = app()
    refused = await anonymous.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401


async def test_a_locked_out_account_refuses_even_the_right_pin(db, app):
    admin, member = await _bootstrap(app)
    await member.post(
        "/api/auth/pin", json={"pin": "4821", "current_password": MEMBER_PASSWORD}
    )
    jenny = (await member.get("/api/auth/me")).json()["id"]

    for _ in range(auth.PIN_ATTEMPT_LIMIT):
        await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "0000"})
    refused = await admin.post("/api/auth/switch", json={"user_id": jenny, "pin": "4821"})
    assert refused.status_code == 401
    assert "too many attempts" in refused.json()["detail"]
    assert (await admin.get("/api/auth/me")).json()["name"] == "patrick"


# --- §3.1: the forced first-login change gates everything else -------------------------------

# Decision 179: "the reachable set while the lock stands is **four** routes — /api/auth/me,
# /api/auth/password, /api/auth/logout and /api/auth/switch. Every other authenticated route in
# the app moves behind ActiveUser." Ordered rather than a set at the call site because /logout
# ends the session it is called on, so it has to be probed last.
REACHABLE_WHILE_LOCKED = (
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/switch"),
    ("POST", "/api/auth/logout"),
)

# /logout is one of the four and takes no auth dependency at all: it opens the cookie itself and
# answers `{"ok": true}` either way, so the dependency walk cannot see it and the walk below
# subtracts it by name. The live sweep is what actually holds it to the decision.
UNGATED_BY_DESIGN = {("POST", "/api/auth/logout")}

# The half of the sweep a dependency walk cannot do. `paths_behind` sees a route's auth only
# where it is *declared*, so a route that reads the session cookie in its own body appears in
# neither set and passes through the subtraction as neither authenticated nor gated. That is how
# both `GET /api/setup/state` — which called `current_user` as a plain function — and the Tonight
# WebSocket, which cannot take an HTTP dependency at all, handed a locked account payloads
# decision 179 puts out of its reach while the sweep still reported the reachable set as four.
SESSION_READERS = ("current_user", "load_session", "open_session_cookie")

# Every hand-rolled reader in `api/`, with what makes it legitimate. A new one is a route the
# walk above is blind to, so it fails the sweep until it is either declared with `Depends` or
# named here alongside the live test that holds it to §3.1's lock.
AUTHENTICATES_BY_HAND = {
    # Both sign-in doors read the *incoming* cookie only to destroy the session this device was
    # already holding (dd24), after the credential check. Nothing is granted on it.
    ("auth", "login"): "destroys the stale session",
    ("passkeys", "login"): "destroys the stale session",
    # One of decision 179's four, and ungated by design — see UNGATED_BY_DESIGN above.
    ("auth", "logout"): "clears the cookie for whoever holds it",
    # Held by test_a_locked_account_sees_only_the_anonymous_setup_state below.
    ("setup", "_optional_user"): "a locked session is served as the stranger it still is",
    # Held by test_the_tonight_channel_refuses_a_locked_account below.
    ("tonight", "channel"): "a WebSocket writes the ActiveUser check out by hand",
}


def hand_rolled_session_readers() -> set[tuple[str, str]]:
    """(module, function) for every call to a session reader across `spielplan/api/`.

    Source rather than dependency graph, because the two forms are exactly what the graph cannot
    tell apart: `Depends(current_user)` names the function and the walk follows it, while
    `await current_user(...)` calls it and leaves no trace in `route.dependant` at all. `deps.py`
    is skipped — it *is* the declared dependency every other module is measured against.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(Path(spielplan.api.__file__).parent.glob("*.py")):
        if path.name == "deps.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in SESSION_READERS:
                    found.add((path.stem, node.name))
    return found


async def _locked_member(app):
    """A member session sitting in §3.1's forced first-login change, and the admin that made it."""
    admin = app()
    await admin.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD})
    otp = (
        await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]

    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.status_code == 200
    assert signed_in.json()["must_change_password"] is True
    return member


async def test_a_locked_account_cannot_reach_the_app(app):
    """§3.1: an account created with a one-time password is locked to a password change, and
    the auth layer enforces it — not the UI."""
    member = await _locked_member(app)
    assert (await member.get("/api/titles")).status_code == 403
    assert (await member.get("/api/prompts/finish")).status_code == 403
    # …but /me and the password route stay reachable, or there would be no way out.
    assert (await member.get("/api/auth/me")).status_code == 200


def test_the_forced_change_gate_covers_every_authenticated_route_but_four():
    """Decision 179's set is four, and this is the walk that says so — not a docstring.

    The row `platform-forced-first-login-change` promised "a sweep over every other
    authenticated route in the app" for two milestones while naming two column-level database
    tests, and the gate only checks that a named test exists. What that hid: `POST
    /api/auth/reauth` sat on `CurrentUser`, a fifth route, letting an account locked to a
    password change stamp `admin_verified_at`. This is the half a hand-picked probe list cannot
    do — it fails for a route nobody thought to add to a list.

    And the walk on its own was not enough either: it can only see auth that is *declared*, so
    `GET /api/setup/state` and `GET /api/tonight/channel` were invisible to both sides of the
    subtraction below and served a locked account regardless. The source scan is the second half
    — a route the graph cannot classify fails here rather than passing unseen.
    """
    hand_rolled = hand_rolled_session_readers()
    assert hand_rolled == set(AUTHENTICATES_BY_HAND), (
        "a route in api/ reads the session cookie where the dependency walk cannot see it: "
        f"{sorted(hand_rolled ^ set(AUTHENTICATES_BY_HAND))} — declare the auth with Depends, or "
        "name it in AUTHENTICATES_BY_HAND with the live test that holds it to §3.1's lock"
    )

    authenticated = paths_behind(deps.current_user)
    gated = paths_behind(deps.active_user)
    assert gated, "the walk found nothing behind active_user — it is sweeping an empty set"
    # `active_user` depends on `current_user`, so the gated set is a strict subset by construction;
    # asserting it catches a future gate wired around the session loader rather than through it.
    assert gated < authenticated

    assert authenticated - gated == set(REACHABLE_WHILE_LOCKED) - UNGATED_BY_DESIGN, (
        "the routes reachable while §3.1's lock stands are not the four decision 179 names — "
        f"the walk found {sorted(authenticated - gated)}"
    )


async def test_every_other_authenticated_route_refuses_a_locked_account(app):
    """The other half of the sweep: the dependency is wired *and* it fires.

    One session against every gated route rather than a parametrised case each, because all 76
    of them today are refused inside the dependency before a route body or a body validator
    runs — nothing is written, so nothing needs a clean database between them.
    """
    member = await _locked_member(app)
    for method, path in sorted(paths_behind(deps.active_user)):
        response = await member.request(method, concrete(path), json={})
        assert response.status_code == 403, (
            f"{method} {path} let an account locked to a first-login change through "
            f"with {response.status_code}"
        )


async def test_the_four_routes_that_are_the_way_out_stay_reachable(app):
    """A gate that refused all 79 of them would pass the sweep above and lock the household out.

    "Not 403" rather than 200: /password and /switch are probed with an empty body, and the 422
    that answers is itself the proof — `active_user` raises before FastAPI validates a body, so
    a refusal would have arrived instead.
    """
    member = await _locked_member(app)
    for method, path in REACHABLE_WHILE_LOCKED:
        response = await member.request(method, path, json={})
        assert response.status_code != 403, f"{method} {path} is the way out and it is shut"


# --- §3.1: the two routes that authenticate by hand, probed live ----------------------------


async def test_a_locked_account_sees_only_the_anonymous_setup_state(app):
    """`GET /api/setup/state` serves a first-booting stranger and a signed-in operator both, so
    it cannot take `ActiveUser` — and that is how it became a fifth reachable route.

    Compared field for field rather than by status code, because this route answers 200 either
    way: what decision 179 denies a locked account is the privileged half — the install
    fingerprint sec-14 took off the anonymous surface — and not the two fields the first-boot
    redirect reads. `test_the_setup_state_an_anonymous_caller_sees_is_two_fields` holds the other
    end: an unlocked operator still gets all six.
    """
    member = await _locked_member(app)
    locked = await member.get("/api/setup/state")
    assert locked.status_code == 200
    assert set(locked.json()) == {"required", "note"}, (
        "an account locked to §3.1's first-login change was handed the privileged wizard state: "
        f"{sorted(locked.json())}"
    )
    assert locked.json() == (await app().get("/api/setup/state")).json()


async def _websocket(client, path: str) -> list[dict]:
    """Drive one WebSocket handshake over ASGI and return what the app sent.

    httpx has no WebSocket transport, so the app is called directly rather than through the
    client — the cookie jar the fixture's client holds is what makes it the locked member's
    socket. `websocket.disconnect` follows the connect so the handler's read loop ends and its
    `finally` unsubscribes instead of leaving the hub holding a socket.
    """
    cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 51000),
        "server": ("test", 80), "subprotocols": [],
        "headers": [(b"host", b"test"), (b"cookie", cookies.encode())],
    }
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]
    sent: list[dict] = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await client._transport.app(scope, receive, send)
    return sent


async def test_the_tonight_channel_refuses_a_locked_account(app):
    """Decision 179 is about the account, not the transport: `GET /api/tonight/rooms` refuses a
    locked session and the socket carrying the same rooms — and each room's per-seat progress —
    must refuse it too.

    Both halves, because closing every socket would satisfy the first assertion and lock the
    household out of the lobby banner instead.
    """
    admin, member = await _bootstrap(app)
    otp = (
        await admin.post("/api/admin/users", json={"name": "kim", "role": "member"})
    ).json()["one_time_password"]
    locked = app()
    signed_in = await locked.post("/api/auth/login", json={"name": "kim", "password": otp})
    assert signed_in.json()["must_change_password"] is True

    refused = await _websocket(locked, "/api/tonight/channel")
    assert [frame["type"] for frame in refused] == ["websocket.close"], (
        "an account locked to §3.1's first-login change was served the Tonight channel: "
        f"{[frame['type'] for frame in refused]}"
    )

    served = await _websocket(member, "/api/tonight/channel")
    assert [frame["type"] for frame in served][:2] == ["websocket.accept", "websocket.send"]


# --- §3.2: the refusal must not leak which names exist ---------------------------------------


async def test_a_wrong_name_and_a_wrong_password_cost_the_same(app, monkeypatch):
    """One message for both cases is not enough on its own. argon2 costs tens of milliseconds
    and an index miss costs none, so short-circuiting on "no such account" answers in the
    timing what the body refuses to say — and the names it leaks are exactly the ones worth
    guessing passwords against.

    Counted, not clocked. A wall-clock comparison on a box that also runs Postgres and a
    browser is a coin flip dressed as a proof: it passed while a real short-circuit was in
    place and it failed while the code was right, so the number it reported meant nothing
    either way. What the rule actually says is that both requests do the same argon2 work,
    and that is a call count.

    The patch goes on the *sync* primitive because `verify_password_async` hands that global
    to `asyncio.to_thread` and resolves it at call time (step 14). Patching the async wrapper
    instead would leave every real caller unmeasured and count zero, which is why the
    assertion below refuses a zero.

    Round trips are counted alongside the verifies, because argon2 parity alone was not the
    whole rule and the shipped code did not have it: the present-name path paid a second SELECT
    and a failure-increment UPDATE the absent path skipped, which measured as a systematic
    ~3.6 ms head start for a name nobody holds. A locked account is measured too — it used to
    skip the increment and so answer at the absent path's speed, reading lock state off the
    clock for anyone who could already tell the two apart.

    Statements rather than a count of them, and the rows they write rather than the statements,
    because equal counts are not equal work: the increment matches one row for a name that
    exists and is not locked out and none for the other two, which is a heap tuple and its WAL
    against nothing, and measures at ~1.5 ms on a ~37 ms refusal. That residual is asserted
    below as the number it is rather than smoothed over — `check_password`'s docstring says why
    `app_user` has nowhere to keep a sentinel row for the absent path to write to, and the
    coverage row says the same. If the three ever do write the same number of rows, this
    assertion is where that shows up, and the three places that call the gap open are what the
    change has to close with it.
    """
    admin, _member = await _bootstrap(app)

    calls: list[str] = []
    real = auth.verify_password

    def counted(stored_hash, password):
        calls.append(password)
        return real(stored_hash, password)

    monkeypatch.setattr(auth, "verify_password", counted)

    # On `asyncpg.Connection` rather than the pool proxy the route actually holds: the proxy
    # resolves each method with `getattr(con, name)` at call time, so patching the class is what
    # sees every query, and patching the proxy would see the wrapper and not the work.
    # The command tag comes back from `execute` only, and it is what says how much work the
    # statement did: `UPDATE 1` writes a row and `UPDATE 0` writes none. `fetchval` returns
    # strings of its own (a password hash is one), so tags are read from the one method that
    # reports them.
    queries: list[tuple[str, str | None]] = []
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        real_query = getattr(asyncpg.Connection, name)

        async def counted_query(self, query, *args, _real=real_query, _name=name, **kwargs):
            result = await _real(self, query, *args, **kwargs)
            queries.append((query, result if _name == "execute" else None))
            return result

        monkeypatch.setattr(asyncpg.Connection, name, counted_query)

    def rows_written() -> int:
        return sum(
            int(str(tag).rsplit(" ", 1)[-1])
            for _statement, tag in queries
            if tag is not None and str(tag).startswith("UPDATE ")
        )

    async def refusal_cost(name: str) -> tuple[int, tuple[str, ...], int]:
        calls.clear()
        queries.clear()
        response = await admin.post(
            "/api/auth/login", json={"name": name, "password": "definitely-wrong"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == auth.PASSWORD_REFUSAL
        return len(calls), tuple(statement for statement, _tag in queries), rows_written()

    existing = await refusal_cost("patrick")
    absent = await refusal_cost("nobody-lives-here")
    # `existing` above was one of them, so this reaches the limit and no further.
    for _ in range(auth.PASSWORD_ATTEMPT_LIMIT - 1):
        await refusal_cost("patrick")
    locked = await refusal_cost("patrick")

    assert existing[:2] == absent[:2] == locked[:2], (
        f"a wrong password against an existing name made {existing[0]} argon2 verifications "
        f"and issued {list(existing[1])}, a missing name {absent[0]} and {list(absent[1])}, a "
        f"locked-out one {locked[0]} and {list(locked[1])} - the difference is an "
        "account-enumeration oracle a stopwatch can read"
    )
    assert existing[0] > 0, "the counter was installed somewhere the login path does not reach"
    assert existing[1], "the query counter was installed somewhere the login path misses"

    # The residual, stated as a measurement rather than claimed away. See the docstring above.
    assert (existing[2], absent[2], locked[2]) == (1, 0, 0), (
        f"the failure increment wrote {existing[2]} rows for an existing name, {absent[2]} for "
        f"an absent one and {locked[2]} for a locked-out one. (1, 0, 0) is the known gap this "
        "test, `core.auth.check_password`'s docstring and the coverage row "
        "`platform-login-throttle-and-input-bounds` each name as open; anything else means the "
        "work moved, and all three have to be rewritten to whatever it moved to"
    )
