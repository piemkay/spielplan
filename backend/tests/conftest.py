"""Shared fixtures. Postgres tests read `TEST_DATABASE_URL` (or `.env.test`) and skip when it is unset."""

from __future__ import annotations

import hashlib
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


# Set at import, not in a fixture: `settings()` is cached and modules construct `Settings` during
# collection. `setdefault`, so CI's and a developer's values win.
os.environ.setdefault("SESSION_SECRET", "pytest-session-secret-not-a-real-one")
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

# An explicit "0", not `pop`: `Settings` also reads `.env`, which the environment outranks. Left set,
# the flag disarms every §2 refusal test.
os.environ["SPIELPLAN_INSECURE_DEV"] = "0"


@pytest.fixture(scope="session")
def pg_url() -> str:
    """This checkout's and xdist worker's database, so the next run reclaims what a killed one left.
    `db` clones it per test from a template migrated once per session."""
    import asyncio
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg

    from spielplan.db import migrate
    from tests.helpers import create_database, drop_database, sibling

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset (see tests/conftest.py)")

    parts = urlsplit(url)
    checkout = hashlib.sha1(str(ROOT).encode()).hexdigest()[:6]
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    mine = urlunsplit(parts._replace(path=f"/{parts.path.lstrip('/') or 'postgres'}_{checkout}_{worker}"))
    admin, template, template_url = sibling(mine, "_tpl")

    async def migrate_template() -> None:
        await create_database(admin, template)
        conn = await asyncpg.connect(template_url)
        try:
            await migrate.apply_all(conn)
        finally:
            await conn.close()

    asyncio.run(migrate_template())
    try:
        yield mine
    finally:
        asyncio.run(drop_database(admin, urlsplit(mine).path.lstrip("/")))
        asyncio.run(drop_database(admin, template))


@pytest.fixture
async def db(pg_url):
    """A connection to a fresh clone of the migrated template."""
    import json
    from urllib.parse import urlsplit

    import asyncpg

    from spielplan.db import pool
    from tests.helpers import create_database, sibling

    admin, template, _ = sibling(pg_url, "_tpl")
    await create_database(admin, urlsplit(pg_url).path.lstrip("/"), template=template)
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    try:
        yield conn
    finally:
        await conn.close()
        await pool.close_pool()


@pytest.fixture
async def fake_jellyfin():
    """Returns (app, transport), so the client's real request building runs end to end with no socket."""
    import importlib.util
    import sys

    import httpx

    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", ROOT.parent / "ops" / "fake_jellyfin.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: Pydantic resolves the double's annotations through `sys.modules`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    yield module, httpx.ASGITransport(app=module.app)


def _connector_seed_env_names() -> tuple[str, ...]:
    """Derived from `Settings`, so a new connector variable is neutralised without a second list."""
    from spielplan.core.config import Settings

    return tuple(
        name.upper()
        for name in Settings.model_fields
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_", "gemini_", "anthropic_", "openai_"))
    )


def _connector_seed_variables_present() -> list[str]:
    """`Settings` matches case-insensitively and a POSIX environment keeps both spellings apart."""
    seeds = set(_connector_seed_env_names())
    return [variable for variable in os.environ if variable.upper() in seeds]


@pytest.fixture
async def app(db, pg_url, tmp_path, monkeypatch):
    """The real app over ASGI with the genuine lifespan. Yields a factory: each call is a client with its
    own cookies. The operator's env and `.env` are removed via `monkeypatch`, which unwinds on any exit."""
    import contextlib

    import httpx

    from spielplan.core.config import settings

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in _connector_seed_variables_present():
        monkeypatch.delenv(name, raising=False)
    # A subdirectory and not `tmp_path` itself: DATA_DIR points there, so the app writes into it.
    neutral = tmp_path / "no-dot-env"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    settings.cache_clear()
    # Decision 497's follow timer, off: the window tests pin `app.state.artifacts` by hand.
    from spielplan.models import basis

    monkeypatch.setattr(basis, "FOLLOW_SECONDS", None)

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
        settings.cache_clear()


@pytest.fixture
def secrets_key(monkeypatch):
    """Tests that assert the refusal deliberately do not take this."""
    from spielplan.core.config import settings

    monkeypatch.setenv("SECRETS_KEY", "test-secrets-key-not-a-real-one-at-all")
    settings.cache_clear()
    yield "test-secrets-key-not-a-real-one-at-all"
    settings.cache_clear()


@pytest.fixture
def no_secrets_key(monkeypatch, tmp_path):
    """`Settings` reads `.env` from the working directory,
    so unsetting the variable is not enough: chdir too."""
    from spielplan.core.config import settings

    monkeypatch.delenv("SECRETS_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    settings.cache_clear()
    yield
    settings.cache_clear()
