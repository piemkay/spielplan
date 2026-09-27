"""Route gating, read twice: from each route's dependency graph, and over HTTP, because a route can have
the dependency and still answer."""

from __future__ import annotations

import re

from spielplan.api import deps
from spielplan.app import create_app
from tests.helpers import concrete, household, resolves, route_table, websocket

# Every route that answers a caller with no session, with what makes that the right answer.
ANONYMOUS = {
    ("POST", "/api/auth/login"): "the password door itself; a session is what it issues",
    ("POST", "/api/auth/passkey/login/options"): "the WebAuthn challenge the door needs first",
    ("POST", "/api/auth/passkey/login"): "the passkey door itself",
    # Decision 179: answers `{"ok": true}` either way.
    ("POST", "/api/auth/logout"): "clears the cookie for whoever holds it, and grants nothing",
    ("GET", "/api/health"): "the container probe, answered before anyone can sign in",
    ("GET", "/api/config"): "the origin and whether a bundle exists, for the shell's first paint",
    ("POST", "/api/setup/admin"): "first boot has no account to authenticate as",
    ("GET", "/api/setup/state"): "whether this box still owes a wizard, cut to that one bit",
}

# No session dependency, yet a stranger gets 401: the handler checks the token of a server plugin that
# cannot hold a cookie (decisions 332, 367).
TOKEN_AUTHED = {("POST", "/events/jellyfin")}

# Behind a session but deliberately NOT behind `active_user`: decision 179's ways out of the lock.
CURRENT_ONLY = {
    ("GET", "/api/auth/me"): "a locked account must be able to see whose lock it is",
    ("POST", "/api/auth/password"): "the way out of the lock",
    ("POST", "/api/auth/switch"): "the shared-device chip, reachable while one profile is locked",
}

GATES = (deps.active_user, deps.admin_user, deps.active_user_ws, deps.active_user_brief)
CHANNEL = ("WS", "/api/tonight/channel")


def _verdict(route) -> str:
    """A route that authenticates in its own body reads as unguarded: nothing is in `route.dependant`."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return "unguarded"
    if any(resolves(dependant, gate) for gate in GATES):
        return "guarded"
    if resolves(dependant, deps.current_user):
        return "session-only"
    return "unguarded"


def _verdicts() -> dict[tuple[str, str], str]:
    return {key: _verdict(route) for key, route in route_table(create_app()).items()}


def _admin_routes() -> set[tuple[str, str]]:
    return {
        key
        for key, route in route_table(create_app()).items()
        if resolves(route.dependant, deps.admin_user)
    }


def _named(keys) -> str:
    return "\n".join(f"    {method:<7} {path}" for method, path in sorted(keys))


def test_the_inventory_is_the_apps_own_route_table():
    """Every sweep here walks one table, so it is held to openapi(), which FastAPI builds on its own."""
    application = create_app()
    table = route_table(application)
    walked = {(method, re.sub(r":\w+\}", "}", path)) for method, path in table if method != "WS"}
    documented = {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        for method in operations
    }
    assert walked == documented, f"the walk and openapi() disagree:\n{_named(walked ^ documented)}"
    assert CHANNEL in table, (
        "the WebSocket is the one route openapi() cannot see, and the blind vote depends on it"
    )


def test_every_route_the_app_registers_is_behind_a_session_or_named_anonymous():
    verdicts = _verdicts()
    assert verdicts.get(CHANNEL) == "guarded", (
        f"the Tonight channel is {verdicts.get(CHANNEL, 'not registered at all')}: the session "
        "socket is what the blind vote's integrity rests on (decision 225)"
    )

    unguarded = {key for key, verdict in verdicts.items() if verdict == "unguarded"}
    unguarded -= ANONYMOUS.keys() | TOKEN_AUTHED
    assert not unguarded, (
        "these routes resolve no session gate and are not named anonymous:\n"
        + _named(unguarded)
        + "\nAdd the gate (§3.2 puts every route behind a session), or - if a stranger who "
        "can reach the origin really may have this - name it in ANONYMOUS with the reason."
    )

    session_only = {key for key, verdict in verdicts.items() if verdict == "session-only"}
    assert not session_only - CURRENT_ONLY.keys(), (
        "these routes are behind a session but not behind §3.1's first-login lock:\n"
        + _named(session_only - CURRENT_ONLY.keys())
        + "\nThe set that may skip the lock is decision 179's ways out of it. Use ActiveUser, or "
        "name the route in CURRENT_ONLY with what makes it a way out."
    )


def test_the_admin_gate_and_the_admin_namespace_are_the_same_routes():
    """A route that keeps `active_user` but loses `admin_user` would drop out of the member sweep
    quietly; the namespace is what notices."""
    admin = _admin_routes()
    namespace = {
        key for key in route_table(create_app()) if key[1].startswith("/api/admin/")
    }
    assert admin, "the walk found no admin-gated route - it is measuring itself"
    assert admin == namespace, (
        "admin-gated but outside /api/admin, or under /api/admin without admin_user:\n"
        + _named(admin ^ namespace)
    )


def test_neither_route_allow_list_outlives_the_routes_it_names():
    """A stale allow-list entry silently exempts whatever route is next written at that path."""
    verdicts = _verdicts()
    stale_anonymous = {key for key in ANONYMOUS.keys() | TOKEN_AUTHED if verdicts.get(key) != "unguarded"}
    assert not stale_anonymous, (
        "ANONYMOUS or TOKEN_AUTHED names routes that are no longer anonymous (gated since, or "
        "deleted):\n" + _named(stale_anonymous)
    )
    stale_current_only = {key for key in CURRENT_ONLY if verdicts.get(key) != "session-only"}
    assert not stale_current_only, (
        "CURRENT_ONLY names routes that no longer sit between the two gates:\n"
        + _named(stale_current_only)
    )


async def test_every_route_outside_the_anonymous_allow_list_refuses_a_signed_out_caller(app):
    anonymous = app()
    served = []
    for method, path in sorted(route_table(create_app()).keys() - ANONYMOUS.keys()):
        if method == "WS":
            continue
        answer = await anonymous.request(method, concrete(path), json={})
        if answer.status_code != 401:
            served.append(f"{method} {path} -> {answer.status_code}")
    assert not served, (
        "these routes answered a caller holding no session, and the cookie is the door "
        f"- add the session dependency, or add the route to ANONYMOUS with its reason: {served}"
    )

    frames = await websocket(anonymous, CHANNEL[1])
    assert [frame["type"] for frame in frames] == ["websocket.close"], (
        "the Tonight channel carries who is in the room and each seat's progress; an anonymous "
        f"handshake was answered with {frames}"
    )
    assert frames[0].get("code") == 1008, f"the anonymous socket was closed with {frames[0]}"


async def test_every_route_on_the_anonymous_allow_list_is_still_served_to_a_stranger(app):
    """A gate added to one of these leaves a permanent exemption. "Not 401": an empty body's 422 is
    proof enough."""
    for method, path in sorted(ANONYMOUS):
        # A fresh client per probe: /api/auth/logout clears the cookie jar it is called on.
        answer = await app().request(method, path, json={})
        assert answer.status_code != 401, (
            f"{method} {path} is on the anonymous allow-list and refuses a stranger - if the gate "
            "is deliberate, take the entry out so the sweep covers the route"
        )


async def test_every_admin_route_refuses_a_member(app):
    _admin, member = await household(app)
    let_through = []
    for method, path in sorted(_admin_routes()):
        answer = await member.request(method, concrete(path), json={})
        if answer.status_code != 403:
            let_through.append(f"{method} {path} -> {answer.status_code}")
    assert not let_through, f"these admin routes let a member through: {let_through}"
