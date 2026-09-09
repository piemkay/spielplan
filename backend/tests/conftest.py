"""Shared fixtures.

Tests that need a real Postgres read `TEST_DATABASE_URL` and skip when it is unset, so the
suite stays runnable on a machine with no Docker. To run them:

    docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db
    createdb spielplan_test      # or: docker compose exec db createdb -U spielplan spielplan_test
    TEST_DATABASE_URL=postgresql://spielplan:...@127.0.0.1:5432/spielplan_test pytest
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Where the URL came from, decided here because this is the last frame that can still tell the two
# apart: after the loader below has run, a URL an operator exported and a URL an untracked file
# supplied are the same string. The arming line names the source because the convenience below is
# what silently turns `pytest backend/tests` into an integration run -- creating and dropping
# databases, and (via test_backup.py) shelling out to `docker exec` -- against whatever host a file
# the repository does not track happens to name. [M4.8 test-02]
_URL_SOURCE = "env" if os.environ.get("TEST_DATABASE_URL") else "unset"

# Load .env.test if present, so the URL does not have to be exported by hand every time.
_env_test = ROOT.parent / ".env.test"
if _env_test.is_file() and "TEST_DATABASE_URL" not in os.environ:
    for line in _env_test.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            os.environ["TEST_DATABASE_URL"] = value.strip()
            _URL_SOURCE = ".env.test"


# M4.7 (decision 181) made §2's required config a refusal in `Settings` rather than a compose-only
# `${VAR:?}`, which means the suite now has to supply what an install supplies. Until then the
# `or "insecure-dev-secret"` fallback in `core/auth`, `api/rank` and `api/tonight` is what let
# 1100+ tests construct `Settings` out of a blank environment; deleting it without this makes every
# one of them a ValidationError. Set at import rather than in a fixture on purpose: `settings()` is
# `lru_cache`d and modules construct `Settings` during collection, before any fixture has run.
# `setdefault`, so CI's own values (`.github/workflows/ci.yml:17-19`) and a developer's exported
# ones still win, and the value is a plausible-length secret because the refusal has a floor.
os.environ.setdefault("SESSION_SECRET", "pytest-session-secret-not-a-real-one")
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

# And one variable taken away, for the same reason the two above are supplied: an install does not
# have it. `SPIELPLAN_INSECURE_DEV` is decision 181's single off-switch for every §2 refusal, and
# README's "Developing" paragraph hands it to the developer two lines after saying a hand-run
# backend reads `.env` from its own working directory — which is the directory pytest runs from.
# Left alone, a flag set there or exported disarms `test_config.py`'s eight refusal tests, the
# anonymous-schema assertion in `test_http_seam.py` four files away, and this suite's own leak
# detector in `test_devstub_contract.py`, all of which then pass by being unable to fail.
# Not `pop`: `Settings.model_config` is `SettingsConfigDict(env_file=".env", ...)`, so a `.env`
# line survives being removed from `os.environ` (the `no_secrets_key` fixture below records the
# same discovery about SECRETS_KEY). The environment is the higher-precedence source, so an
# explicit "0" is the one spelling that closes both routes in — and it is the spelling
# `ops/devstub.py:44` already documents for a developer who wants the refusals back. A test that
# wants the flag sets it itself, with `monkeypatch.setenv`. [M4.7 spec-04; decision 181]
os.environ["SPIELPLAN_INSECURE_DEV"] = "0"


def test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def _worker_suffix() -> str:
    """What makes this pytest process distinct from every other one on the machine.

    `PYTEST_XDIST_WORKER` under -n, the pid otherwise. The pid is enough because a session that
    ends normally drops its database and a session that does not is swept by
    `_reap_orphaned_databases` at the start of the next one -- which is the invariant this
    docstring used to assert on the strength of the `finally` in `pg_url` alone, a frame a
    terminated process never reaches. [M4.8 dd29]
    """
    return os.environ.get("PYTEST_XDIST_WORKER") or f"p{os.getpid()}"


# Postgres truncates an identifier at 63 bytes, so the name is cut to fit -- and what gets cut
# is the base, never the suffix. Truncating `<base>_p<pid>` as one string shears the tail, and
# the tail is the whole of what `_reap` reads: at a 56-character base a pid of 63704 is stored
# as `_p6370`, a different and by then almost certainly dead process, so the next session drops
# a database a live one is using. That is the cross-process destruction `pg_url`'s docstring
# records, arriving through the tidy-up written to prevent the leak. The reserve is a constant
# rather than this process's own suffix length because the reaper has to rebuild the same prefix
# for names written by processes whose pids are shorter than its own. [M4.8 dd29]
_NAME_LIMIT = 62
_SUFFIX_RESERVE = 10  # "p" plus the `\d{1,9}` `_reap` parses back out


def _name_prefix(base: str) -> str:
    """As much of `base` as fits once room is left for the longest suffix this convention writes."""
    return base[: _NAME_LIMIT - _SUFFIX_RESERVE - 1]


def _database_name(base: str) -> str:
    """The database this process will own, so the arming line can name the same one `pg_url`
    creates rather than a plausible-looking reconstruction of it."""
    return f"{_name_prefix(base)}_{_worker_suffix()}"[:_NAME_LIMIT]


def arming_line(url: str | None, source: str, unarmed_reason: str | None = None) -> str:
    """The one line every run says about its integration layer.

    A pure function and not an f-string inside the hooks, because the only other way to test what
    a run prints is to run pytest inside pytest. ASCII on purpose (CLAUDE.md): this is the first
    line a cp1252 console sees, and a run that crashes on its own banner reports nothing at all.
    [M4.8 test-02]
    """
    if unarmed_reason or not url:
        reason = unarmed_reason or "TEST_DATABASE_URL is unset"
        return f"integration layer: UNARMED ({reason}) -- db/app/pg_url tests skip"

    from urllib.parse import urlsplit

    parts = urlsplit(url)
    where = f"{parts.hostname or '?'}:{parts.port or 5432}"
    base = parts.path.lstrip("/") or "postgres"
    return f"integration layer: ARMED against {where}/{_database_name(base)} (source: {source})"


def pytest_addoption(parser) -> None:
    """`--no-db`: run with the integration layer deliberately disarmed, and say so.

    Registering an option from `backend/tests/conftest.py` is legal only because that directory is
    always on the command line -- which CLAUDE.md already mandates for a different reason ("never
    bare `pytest` from repo root": the pytest config lives in `backend/pyproject.toml`). Someone
    will eventually try it from the root and be told "unrecognized arguments: --no-db", which is a
    confusing way to learn that the path argument was missing. [M4.8 test-02]
    """
    parser.addoption(
        "--no-db",
        action="store_true",
        default=False,
        help="disarm the integration layer: db/app/pg_url tests skip instead of making a database",
    )


def pytest_configure(config) -> None:
    # An empty TEST_DATABASE_URL is already the unarmed state `pg_url` skips on, so `--no-db`
    # needs no branch of its own anywhere else in this file. [M4.8 test-02]
    if config.getoption("--no-db"):
        os.environ["TEST_DATABASE_URL"] = ""


def _session_arming_line(config) -> str:
    reason = "--no-db" if config.getoption("--no-db", default=False) else None
    return arming_line(os.environ.get("TEST_DATABASE_URL"), _URL_SOURCE, reason)


def pytest_report_header(config) -> str:
    return _session_arming_line(config)


def _report_line(config, line: str) -> None:
    """Say something to the console from inside a hook or a fixture.

    Never `print`: pytest's capture is on during fixture setup and a green run discards that
    buffer, so a `print` from `pg_url` is invisible at `-q` -- the verbosity CLAUDE.md and both
    `ci.yml` steps use -- and at default verbosity too. The terminal reporter writes past the
    capture and lands before the progress line; a run with no terminal plugin at all has nowhere
    for a console line to go, and staying quiet there is not the silence this exists to end.
    """
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)


def pytest_sessionstart(session) -> None:
    """The same line again, for the verbosity this project actually runs at.

    pytest suppresses `pytest_report_header` under `-q`, which is what CLAUDE.md's own command
    line and `ci.yml` both use -- so the header hook alone would say nothing on every run anyone
    actually performs. Written through the terminal reporter rather than `print` so it lands
    before the progress line instead of in captured output, and only below default verbosity so
    the two hooks never both speak. [M4.8 test-02]
    """
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
    """Is a process with this pid running right now?

    Never `os.kill(pid, 0)` on Windows: `os.kill` there is `TerminateProcess`, so the POSIX no-op
    liveness probe would kill whatever it asked about -- including the concurrent pytest run this
    whole mechanism exists to protect. `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` is the
    read-only question, and the handle has to be closed or the probe leaks one per name.

    A failed `OpenProcess` is not the same answer as a missing process, and the two mean opposite
    things to a caller that drops WITH (FORCE): ERROR_INVALID_PARAMETER (87) says the pid is gone,
    ERROR_ACCESS_DENIED (5) says this session may not ask about a process that is very much there.
    Reading the second as death is how a reaper becomes the cross-process destruction it exists to
    prevent -- a pytest run under another account (decision 183's corpus runner installs as a
    service, against the same cluster and base name `.env.test` names) or from an elevated shell
    owns pids this one cannot open, and its live database would be taken for an orphan. So only
    87 is death and every other failure is treated as life, which is the same call the
    PermissionError arm below makes on POSIX and errs the same way: an orphan left behind costs
    disk, a live one dropped costs the run. `use_last_error` because `ctypes.windll`'s shared
    handle keeps no reliable per-call error -- the private copy is the supported read.
    [M4.8 dd29, review cycle 2]
    """
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
    """The names this convention wrote, and only those.

    `\\d{1,9}` and not `\\d+`: every pid on every platform this runs on fits, and a longer run of
    digits is by construction not a pid this convention wrote -- which puts it in the same class
    as `_pgr` and `_ledger`, left alone. Built from `_name_prefix` and not from `base`, because
    that is where `_database_name` stopped: for a base long enough to be cut, a pattern anchored
    on the whole base matches nothing this suite ever created and the leak comes back silently.
    """
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
    """Drop every `<base>_p<pid>` whose named process is gone. Returns what it dropped.

    `pg_url` drops its own database in a `finally`, which is precisely the frame a terminated
    session does not run; by the time anyone counted, 34 of them and 4,151 MB had accumulated in
    the same cluster as the household's own `spielplan` database, where the dump/restore tests
    need the free space.

    Liveness and not connection count: `db` closes its connection between tests, so an idle
    database is the normal state of a *running* session, and reaping on that would reproduce the
    cross-process destruction `pg_url`'s docstring exists to prevent. Names that are not
    `_p<digits>` are left for a human -- `_pgr` from `test_backup.py`, an xdist `_gw0` that
    carries no pid at all, a hand-made `_ledger`: a name without a pid is a name a person chose.

    Never raises, and never silently: a tidy-up that could not run is not a suite that failed,
    but one nobody is told about is the leak coming back with nothing to point at. `report` is
    the caller's way of speaking -- `pg_url` hands it the terminal reporter, because this runs
    inside session-fixture setup, where a `print` goes into a buffer a green run discards at
    every verbosity the project uses. And there is a failure worth hearing about: the sweep
    itself is one loop inside one `try`, so a single undroppable orphan (one owned by another
    role, one holding a backend a non-superuser cannot terminate) abandons the rest of the
    sweep, and a session that says nothing about that accumulates exactly what this exists to
    prevent. [M4.8 dd29]
    """
    import asyncio

    try:
        return asyncio.run(_reap(admin_url, base))
    except Exception as exc:
        # The exception text comes from the OS and is localised on a German Windows, so it is
        # forced into ASCII rather than trusted: CLAUDE.md's console rule, and a UnicodeEncodeError
        # raised while reporting a failed tidy-up would fail the session this line promises not to.
        detail = str(exc).encode("ascii", "replace").decode("ascii")
        report(f"conftest: orphaned test databases not reaped ({type(exc).__name__}: {detail})")
        return []


@pytest.fixture(scope="session")
def pg_url(request) -> str:
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
    import sys

    import httpx

    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", ROOT.parent / "ops" / "fake_jellyfin.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, which is what `import` itself does and what
    # `test_devstub_contract.py:31-38` spends seven lines explaining: `ops/fake_jellyfin.py`
    # carries `from __future__ import annotations`, so Pydantic resolves its models' annotations
    # through `sys.modules[cls.__module__]`, and a missing entry surfaces as a
    # `class-not-fully-defined` error naming a model rather than this fixture. [M4.8 ti-09]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    yield module, httpx.ASGITransport(app=module.app)


def _connector_seed_env_names() -> tuple[str, ...]:
    """The env vars §2 lets seed connector config on first boot, derived rather than listed.

    Same derivation as `test_static_contracts.py`'s `_seed_fields`, for the same reason: a seventh
    connector variable added to `Settings` has to be neutralised here without anyone remembering
    that a second copy of the list exists in a fixture. [M4.8 ti-app-fixture-inherits-the-dot-env]
    """
    from spielplan.core.config import Settings

    return tuple(
        name.upper()
        for name in Settings.model_fields
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_"))
    )


@pytest.fixture
async def app(db, pg_url, tmp_path, monkeypatch):
    """The real FastAPI app, over ASGI, against the test database.

    Routes are where §3.1's role gating and §3.2's re-prompt actually live — a unit test of the
    `admin_user` dependency proves the dependency, not that every admin route is behind it. This
    runs the genuine lifespan, so migrations and §2's env seeding happen the way they do at boot.

    Yields a factory: each call returns a fresh client with its own cookie jar, which is how one
    test can hold an admin session and a member session at the same time.

    Both halves of the operator's environment are taken away first, and the reason is the whole
    reason this fixture is worth writing: the genuine lifespan calls `registry.seed_from_env`
    (`app.py`), and `Settings.model_config` reads `.env` from the working directory, which is the
    directory pytest runs from. On a household that followed its own README -- `JELLYFIN_URL` and
    `JELLYFIN_API_KEY` in `.env`, exactly as `.env.example` documents -- the app under test boots
    with a real connector already configured: `test_jellyfin_link.py`'s two unconfigured-state
    assertions go red for a reason that has nothing to do with the code, and the household's
    encrypted credentials are written into a per-process test database. `no_secrets_key` below
    already records this incident and fixes it with a chdir; the second site did not get the fix.
    A test that wants a seeded connector says so, the way `test_connector_registry.py` does.

    Through `monkeypatch` and not by hand, for the second half of the same reason `no_secrets_key`
    uses it. Five statements sit between the mutations and the `yield`, and two of them raise:
    `create_app()` opens with `_refuse_multiple_workers()` and then `settings()`, whose §2
    validator is a refusal under decision 181 -- and the `cache_clear()` below makes that a fresh
    construction, resolved from the directory this fixture has just moved to. A hand-rolled
    `finally` does not run when the body raises before reaching its `try`, and the process would
    then spend the rest of the session in a pytest tmp_path with the connector seeds gone: one
    red test becoming a session of failures that name a temp directory. pytest unwinds
    `monkeypatch` either way. [M4.8 ti-app-fixture-inherits-the-developers-dot-env]
    """
    import contextlib

    import httpx

    from spielplan.core.config import settings

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in _connector_seed_env_names():
        monkeypatch.delenv(name, raising=False)
    # A subdirectory and not `tmp_path` itself: DATA_DIR points there, so the app writes into it.
    neutral = tmp_path / "no-dot-env"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
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

    monkeypatch.setenv("SECRETS_KEY", "test-secrets-key-not-a-real-one-at-all")
    settings.cache_clear()
    yield "test-secrets-key-not-a-real-one-at-all"
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
