"""Setup the suite repeats: the household's accounts, a user row, a sibling database, one worker tick."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"


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
