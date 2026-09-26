"""Shared fixtures. Postgres tests read `TEST_DATABASE_URL` (or `.env.test`) and skip when it is unset."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Decided before the loader below runs: afterwards an exported URL and a file's URL are the same string.
_URL_SOURCE = "env" if os.environ.get("TEST_DATABASE_URL") else "unset"

# Load .env.test if present, so the URL does not have to be exported by hand every time.
_env_test = ROOT.parent / ".env.test"
if _env_test.is_file() and "TEST_DATABASE_URL" not in os.environ:
    for line in _env_test.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            os.environ["TEST_DATABASE_URL"] = value.strip()
            _URL_SOURCE = ".env.test"


# Set at import, not in a fixture: `settings()` is cached and modules construct `Settings` during
# collection. `setdefault`, so CI's and a developer's values win.
os.environ.setdefault("SESSION_SECRET", "pytest-session-secret-not-a-real-one")
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

# An explicit "0", not `pop`: `Settings` also reads `.env`, which the environment outranks. Left set,
# the flag disarms every §2 refusal test.
os.environ["SPIELPLAN_INSECURE_DEV"] = "0"


def test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def _worker_suffix() -> str:
    """The pid, never `PYTEST_XDIST_WORKER`: `gw0` is not
    process-unique, and `_reap_pattern` parses a pid."""
    return f"p{os.getpid()}"


# Postgres truncates identifiers at 63 bytes; the base is cut, never the suffix `_reap` reads back.
_NAME_LIMIT = 62
_SUFFIX_RESERVE = 10  # "p" plus the `\d{1,9}` `_reap` parses back out


def _name_prefix(base: str) -> str:
    """As much of `base` as fits once room is left for the longest suffix this convention writes."""
    return base[: _NAME_LIMIT - _SUFFIX_RESERVE - 1]


def _database_name(base: str) -> str:
    """The database `pg_url` creates, so the arming line names the same one."""
    return f"{_name_prefix(base)}_{_worker_suffix()}"[:_NAME_LIMIT]


def arming_line(url: str | None, source: str, unarmed_reason: str | None = None) -> str:
    """A pure function, so it is testable without running pytest in pytest. ASCII for cp1252 consoles."""
    if unarmed_reason or not url:
        reason = unarmed_reason or "TEST_DATABASE_URL is unset"
        return f"integration layer: UNARMED ({reason}) -- db/app/pg_url tests skip"

    from urllib.parse import urlsplit

    parts = urlsplit(url)
    where = f"{parts.hostname or '?'}:{parts.port or 5432}"
    base = parts.path.lstrip("/") or "postgres"
    return f"integration layer: ARMED against {where}/{_database_name(base)} (source: {source})"


def pytest_addoption(parser) -> None:
    """Legal only because `backend/tests` is always on the command line."""
    parser.addoption(
        "--no-db",
        action="store_true",
        default=False,
        help="disarm the integration layer: db/app/pg_url tests skip instead of making a database",
    )


def pytest_configure(config) -> None:
    # An empty TEST_DATABASE_URL is already the unarmed state `pg_url` skips on.
    if config.getoption("--no-db"):
        os.environ["TEST_DATABASE_URL"] = ""


def _session_arming_line(config) -> str:
    reason = "--no-db" if config.getoption("--no-db", default=False) else None
    return arming_line(os.environ.get("TEST_DATABASE_URL"), _URL_SOURCE, reason)


def pytest_report_header(config) -> str:
    return _session_arming_line(config)


def _report_line(config, line: str) -> None:
    """Never `print`: capture discards it on a green run; the terminal reporter writes past the capture."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)


def pytest_sessionstart(session) -> None:
    """pytest suppresses `pytest_report_header` under `-q`, so this speaks there and only there."""
    if session.config.option.verbose >= 0:
        return
    _report_line(session.config, _session_arming_line(session.config))


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


def _pid_is_alive(pid: int) -> bool:
    """Never `os.kill(pid, 0)` on Windows: it is `TerminateProcess`. Only ERROR_INVALID_PARAMETER (87)
    means gone; access denied is another account's live process."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, which is still a process.
        return True
    return True


def _reap_pattern(base: str) -> re.Pattern[str]:
    """`\\d{1,9}`, built from `_name_prefix`: only names this convention wrote."""
    return re.compile(rf"^{re.escape(_name_prefix(base))}_p(\d{{1,9}})$")


async def _reap(admin_url: str, base: str) -> list[str]:
    import asyncpg

    prefix = _name_prefix(base)
    pattern = _reap_pattern(base)
    conn = await asyncpg.connect(admin_url)
    dropped: list[str] = []
    try:
        rows = await conn.fetch(
            "SELECT datname FROM pg_database WHERE datname LIKE $1", f"{prefix}%"
        )
        for record in rows:
            name = record["datname"]
            match = pattern.match(name)
            if match is None or _pid_is_alive(int(match.group(1))):
                continue
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            dropped.append(name)
    finally:
        await conn.close()
    return dropped


def _reap_orphaned_databases(admin_url: str, base: str, report) -> list[str]:
    """Drops every `<base>_p<pid>` whose process is gone:
    liveness, not connection count. Never raises, never silent."""
    import asyncio

    try:
        return asyncio.run(_reap(admin_url, base))
    except Exception as exc:
        # OS text is localised; forced to ASCII so reporting a failed tidy-up cannot raise.
        detail = str(exc).encode("ascii", "replace").decode("ascii")
        report(f"conftest: orphaned test databases not reaped ({type(exc).__name__}: {detail})")
        return []


@pytest.fixture(scope="session")
def pg_url(request) -> str:
    """A database this process owns: `db` drops schema `public`
    per test, so a shared one breaks concurrent runs."""
    import asyncio
    from urllib.parse import urlsplit, urlunsplit

    url = test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset (see tests/conftest.py)")

    parts = urlsplit(url)
    base = parts.path.lstrip("/") or "postgres"
    name = _database_name(base)
    admin = urlunsplit(parts._replace(path="/postgres"))
    mine = urlunsplit(parts._replace(path=f"/{name}"))

    # Before taking a database, give back the ones no live process still owns. [M4.8 dd29]
    _reap_orphaned_databases(admin, base, lambda line: _report_line(request.config, line))
    asyncio.run(_make_database(admin, name))
    try:
        yield mine
    finally:
        asyncio.run(_drop_database(admin, name))


@pytest.fixture
async def db(pg_url):
    """A connection to a freshly migrated, empty database; the schema is dropped per test."""
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
async def app_client(app):
    return app()


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
