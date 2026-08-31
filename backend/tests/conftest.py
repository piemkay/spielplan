"""Shared fixtures.

Tests that need a real Postgres read `TEST_DATABASE_URL` and skip when it is unset, so the
suite stays runnable on a machine with no Docker. To run them:

    docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db
    createdb spielplan_test      # or: docker compose exec db createdb -U spielplan spielplan_test
    TEST_DATABASE_URL=postgresql://spielplan:...@127.0.0.1:5432/spielplan_test pytest
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Load .env.test if present, so the URL does not have to be exported by hand every time.
_env_test = ROOT.parent / ".env.test"
if _env_test.is_file() and "TEST_DATABASE_URL" not in os.environ:
    for line in _env_test.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            os.environ["TEST_DATABASE_URL"] = value.strip()


def test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def _worker_suffix() -> str:
    """What makes this pytest process distinct from every other one on the machine.

    `PYTEST_XDIST_WORKER` under -n, the pid otherwise. The pid is enough because the database
    is dropped at the end of the session and recreated at the start of the next one.
    """
    return os.environ.get("PYTEST_XDIST_WORKER") or f"p{os.getpid()}"


async def _make_database(admin_url: str, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_url)
    try:
        # WITH (FORCE) so a previous run that died holding a connection cannot wedge this one.
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_database(admin_url: str, name: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(admin_url)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_url() -> str:
    """A database this pytest process owns outright.

    `db` below drops and recreates schema `public` for every test. On one shared database that
    makes two concurrent pytest processes destroy each other's schema mid-test — which is not a
    hypothetical: three agents building M2 in parallel each hit it, and the failures
    (DuplicateTable, UndefinedTable, unique violations on pg_namespace) look like product bugs
    while having nothing to do with the code under test.

    So the URL from the environment names a *template*: the real database is per process, made
    at session start and dropped at the end. One extra CREATE DATABASE per session buys a suite
    that can be run concurrently by anything.
    """
    import asyncio
    from urllib.parse import urlsplit, urlunsplit

    url = test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset (see tests/conftest.py)")

    parts = urlsplit(url)
    base = parts.path.lstrip("/") or "postgres"
    name = f"{base}_{_worker_suffix()}"[:62]
    admin = urlunsplit(parts._replace(path="/postgres"))
    mine = urlunsplit(parts._replace(path=f"/{name}"))

    asyncio.run(_make_database(admin, name))
    try:
        yield mine
    finally:
        asyncio.run(_drop_database(admin, name))


@pytest.fixture
async def db(pg_url):
    """A connection to a freshly migrated, empty database.

    Each test gets a clean schema: the fixtures are small and dropping the schema is faster
    and far less surprising than trying to unwind an importer that writes fourteen tables.
    """
    import asyncpg

    from spielplan.db import migrate, pool

    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        import json as _json

        await conn.set_type_codec(
            typename, encoder=_json.dumps, decoder=_json.loads, schema="pg_catalog"
        )
    try:
        await conn.execute(
            "DROP SCHEMA IF EXISTS public CASCADE;"
            "DROP SCHEMA IF EXISTS display CASCADE;"
            "DROP SCHEMA IF EXISTS review_store CASCADE;"
            "CREATE SCHEMA public;"
        )
        await migrate.apply_all(conn)
        yield conn
    finally:
        await conn.close()
        await pool.close_pool()


@pytest.fixture
async def fake_jellyfin():
    """`ops/fake_jellyfin.py` mounted in-process.

    Returns (app, transport). The transport goes into `JellyfinClient`, so the client's real
    request building — headers, query parameters, the >= 10.9 routes — is exercised end to
    end with no socket. §7.1's field list and §7.3's per-user write are both HTTP facts, and
    a mock would only assert that we call ourselves.
    """
    import importlib.util

    import httpx

    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", ROOT.parent / "ops" / "fake_jellyfin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.state.reset()
    yield module, httpx.ASGITransport(app=module.app)


@pytest.fixture
async def app(db, pg_url, tmp_path):
    """The real FastAPI app, over ASGI, against the test database.

    Routes are where §3.1's role gating and §3.2's re-prompt actually live — a unit test of the
    `admin_user` dependency proves the dependency, not that every admin route is behind it. This
    runs the genuine lifespan, so migrations and §2's env seeding happen the way they do at boot.

    Yields a factory: each call returns a fresh client with its own cookie jar, which is how one
    test can hold an admin session and a member session at the same time.
    """
    import contextlib
    import os

    import httpx

    from spielplan.core.config import settings

    previous = {k: os.environ.get(k) for k in ("DATABASE_URL", "DATA_DIR")}
    os.environ["DATABASE_URL"] = pg_url
    os.environ["DATA_DIR"] = str(tmp_path)
    settings.cache_clear()

    from spielplan.app import create_app

    application = create_app()
    opened: list[httpx.AsyncClient] = []

    def make() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        )
        opened.append(client)
        return client

    try:
        async with application.router.lifespan_context(application):
            yield make
    finally:
        for client in opened:
            with contextlib.suppress(Exception):
                await client.aclose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.cache_clear()


@pytest.fixture
async def app_client(app):
    """One anonymous client against the app fixture, for the common single-identity case."""
    return app()


@pytest.fixture
def secrets_key(monkeypatch):
    """§2: connector secrets are AEAD-encrypted under a DEK wrapped by SECRETS_KEY, and the app
    refuses to start secret-dependent connectors without it. Tests that store a connector
    secret therefore need one; tests that assert the refusal deliberately do not take this."""
    from spielplan.core.config import settings

    monkeypatch.setenv("SECRETS_KEY", "test-secrets-key-not-a-real-one")
    settings.cache_clear()
    yield "test-secrets-key-not-a-real-one"
    settings.cache_clear()


@pytest.fixture
def no_secrets_key(monkeypatch, tmp_path):
    """The inverse of `secrets_key`, and it needs both halves to be honest.

    §2's refusal is checked against the process-wide `settings()` on purpose (custody is a
    process fact, not an argument — see `registry.seed_from_env`), and `Settings.model_config`
    reads `.env` from the working directory. The README tells every developer to create one
    with a real `SECRETS_KEY` in it, so "no key" was true in CI and false on every machine that
    followed the setup instructions: the seed succeeded and the test failed for a reason that
    had nothing to do with the rule. Unsetting the variable is not enough on its own, hence the
    chdir into an empty directory.
    """
    from spielplan.core.config import settings

    monkeypatch.delenv("SECRETS_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    settings.cache_clear()
    yield
    settings.cache_clear()
