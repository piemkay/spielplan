"""The route inventory: every path the app registers, driven as a stranger over HTTP.

Spec v2.1 §3.2 (the session cookie is the door). `test_layering_guards.py` asserts every route
resolves a gate through its dependant; this asks the question over HTTP instead, because a route
can be behind the dependency and still answer, and a route can authenticate by hand and be
correct. So the allow-list below is measured from what a stranger actually receives.
[M4.10 finding 34]

Skipped without TEST_DATABASE_URL for the two live sweeps; see tests/conftest.py.
"""

from __future__ import annotations

import re

from fastapi.routing import APIWebSocketRoute

from spielplan.app import create_app

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

WEBSOCKET = "WEBSOCKET"

# `GET /{path}`: the SPA fallback, registered only when `SPIELPLAN_STATIC_DIR` names a real build,
# anonymous on purpose, and the one route allowed outside the namespaces below.
SPA_FALLBACK = "/{path}"

# The namespaces this file sweeps: `/api`, and `/events` since decision 332. A route outside both
# would be swept by nothing, which `test_the_inventory_is_the_apps_own_route_table` refuses.
NAMESPACES = ("/api/", "/events/")

# What a stranger is served on purpose. `POST /events/jellyfin` is not here: it is token-authed and
# answers a stranger 401, so rule 1 sweeps it honestly. [decision 367]
ANONYMOUS = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/config"),
        ("GET", "/api/setup/state"),
        ("POST", "/api/setup/admin"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/passkey/login/options"),
        ("POST", "/api/auth/passkey/login"),
        # Decision 179: clears the cookie for whoever holds it and answers `{"ok": true}` either way.
        ("POST", "/api/auth/logout"),
    }
)


def _leaf_routes(routes):
    """Every route object the app will actually match, unwrapped (FastAPI 0.141 wraps them)."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _leaf_routes(candidates())
            continue
        original = getattr(route, "original_route", None)
        yield original if original is not None else route
        yield from _leaf_routes(getattr(route, "routes", ()))


def http_routes(application) -> set[tuple[str, str]]:
    """Every `(METHOD, path)` in one of the app's `NAMESPACES` it serves, from its own schema."""
    return {
        (method.upper(), path)
        for path, operations in application.openapi()["paths"].items()
        if path.startswith(NAMESPACES)
        for method in operations
        if method.upper() in METHODS
    }


def websocket_routes(application) -> set[tuple[str, str]]:
    return {
        (WEBSOCKET, route.path)
        for route in _leaf_routes(application.routes)
        if isinstance(route, APIWebSocketRoute)
    }


def inventory(application) -> set[tuple[str, str]]:
    return http_routes(application) | websocket_routes(application)


def outside_the_namespaces(application) -> set[str]:
    return {path for path in application.openapi()["paths"] if not path.startswith(NAMESPACES)}


def concrete(path: str) -> str:
    """Fill path parameters with a value that exists nowhere, so the gate must fire first."""
    return re.sub(r"\{[^}]+\}", "999999", path)


def test_the_inventory_is_the_apps_own_route_table():
    """Everything below is a subtraction from this set, so an empty or partial one passes by
    being unable to fail."""
    application = create_app()
    found = inventory(application)

    assert len(found) > 80, f"the inventory found {len(found)} routes, so it is sweeping a stub"
    assert (WEBSOCKET, "/api/tonight/channel") in found, (
        "the WebSocket is the one route openapi() cannot see, and the blind vote depends on it"
    )
    stale = ANONYMOUS - found
    assert not stale, f"ANONYMOUS names routes the app does not serve: {sorted(stale)}"
    assert outside_the_namespaces(application) <= {SPA_FALLBACK}, (
        "a route is served outside the /api and /events namespaces and the inventory is skipping "
        f"it: {sorted(outside_the_namespaces(application) - {SPA_FALLBACK})}"
    )


async def _handshake(client, path: str) -> list[str]:
    """Drive one WebSocket handshake over ASGI and return the frame types the app sent."""
    cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 51000),
        "server": ("test", 80), "subprotocols": [],
        "headers": [(b"host", b"test"), (b"cookie", cookies.encode())],
    }
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]
    sent: list[str] = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message["type"])

    await client._transport.app(scope, receive, send)
    return sent


async def _answered_without_a_session(client, routes) -> list[str]:
    served = []
    for method, path in sorted(routes):
        answer = await client.request(method, concrete(path), json={})
        if answer.status_code != 401:
            served.append(f"{method} {path} -> {answer.status_code}")
    return served


async def test_every_route_outside_the_anonymous_allow_list_refuses_a_signed_out_caller(app):
    """§3.2 over HTTP, for every route at once; every offender is collected, not just the first."""
    anonymous = app()
    served = await _answered_without_a_session(anonymous, http_routes(create_app()) - ANONYMOUS)

    assert not served, (
        "these routes answered a caller holding no session, and the cookie is the door "
        f"- add the session dependency, or add the route to ANONYMOUS with its reason: {served}"
    )

    frames = await _handshake(anonymous, "/api/tonight/channel")
    assert frames == ["websocket.close"], (
        "the Tonight channel carries who is in the room and each seat's progress; an anonymous "
        f"handshake was answered with {frames}"
    )


async def test_every_route_on_the_anonymous_allow_list_is_still_served_to_a_stranger(app):
    """The other direction: a gate added to one of these leaves its entry behind as a permanent
    exemption. "Not 401" rather than 200, because an empty body's 422 is itself the proof."""
    for method, path in sorted(ANONYMOUS):
        # A fresh client per probe: /api/auth/logout clears the cookie jar it is called on.
        answer = await app().request(method, path, json={})
        assert answer.status_code != 401, (
            f"{method} {path} is on the anonymous allow-list and refuses a stranger - if the gate "
            "is deliberate, take the entry out so the sweep covers the route"
        )
