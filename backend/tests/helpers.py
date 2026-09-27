"""Setup the suite repeats: the household's accounts, a user row, a sibling database, one worker tick,
the app's route table."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from fastapi.routing import APIWebSocketRoute

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


async def admin_client(app, name: str = "patrick"):
    client = app()
    created = await client.post("/api/setup/admin", json={"name": name, "password": ADMIN_PASSWORD})
    assert created.status_code == 201, created.text
    return client


async def member_client(app, admin, name: str = "jenny", role: str = "member"):
    """Past §3.1's forced first-login change, so a later 403 means the role."""
    made = await admin.post("/api/admin/users", json={"name": name, "role": role})
    assert made.status_code == 201, made.text
    otp = made.json()["one_time_password"]
    client = app()
    signed_in = await client.post("/api/auth/login", json={"name": name, "password": otp})
    assert signed_in.status_code == 200, signed_in.text
    changed = await client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    assert changed.status_code == 200, changed.text
    return client


async def household(app):
    """(admin patrick, member jenny), both signed in."""
    admin = await admin_client(app)
    return admin, await member_client(app, admin)


async def insert_user(db, name: str, role: str = "member") -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


def sibling(pg_url: str, suffix: str) -> tuple[str, str, str]:
    """(admin url, database name, url) for a database next to the test one."""
    parts = urlsplit(pg_url)
    name = parts.path.lstrip("/") + suffix
    return (
        urlunsplit(parts._replace(path="/postgres")),
        name,
        urlunsplit(parts._replace(path=f"/{name}")),
    )


async def create_database(admin_url: str, name: str, template: str | None = None) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_url)
    try:
        # WITH (FORCE) so a run that died holding a connection cannot wedge this one.
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
    finally:
        await conn.close()


async def drop_database(admin_url: str, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def tick_one(job: str, *, due: bool) -> None:
    """One real worker tick with the registry cut to `job`. Not due, only an admin trigger runs it."""
    import time
    from datetime import UTC, datetime

    from spielplan import worker

    row = next(j for j in worker.JOBS if j.name == job)
    jobs = worker.JOBS
    worker.JOBS = (row,)
    try:
        last_run = {} if due else {job: time.monotonic()}
        await worker._tick(time.monotonic(), datetime.now(UTC), last_run, {})
    finally:
        worker.JOBS = jobs


def route_table(application) -> dict[tuple[str, str], object]:
    """(method, path) -> route for everything `application` serves; a WebSocket's method is "WS"."""

    def leaves(routes):
        # FastAPI 0.141 no longer flattens `include_router`.
        for route in routes:
            included = getattr(route, "original_router", None)
            yield from leaves(included.routes) if included is not None else (route,)

    return {
        (method, route.path): route
        for route in leaves(application.routes)
        for method in (("WS",) if isinstance(route, APIWebSocketRoute) else getattr(route, "methods", ()))
        if method in (*METHODS, "WS")
    }


def resolves(dependant, target) -> bool:
    """Recursive: `admin_user` depends on `active_user`, which depends on `current_user`."""
    return any(sub.call is target or resolves(sub, target) for sub in dependant.dependencies)


def concrete(path: str) -> str:
    """Every path parameter filled with an id that exists nowhere, so the gate must answer first."""
    return re.sub(r"\{[^}]+\}", "999999", path)


async def websocket(client, path: str) -> list[dict]:
    """The messages the app sends one handshake. httpx has no WebSocket transport, so the app is called
    directly; the disconnect lets the handler unsubscribe."""
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
