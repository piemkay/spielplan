"""The instrument the suite is read through: `conftest.py`'s arming line, the orphan-database reaper,
and the `app` fixture's isolation from the operator's environment."""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests import conftest

CONFTEST = Path(conftest.__file__).resolve()


def test_the_arming_line_names_the_database_and_where_the_url_came_from():
    """An exported URL and one an untracked file supplied are
    indistinguishable later, so the line names the source."""
    line = conftest.arming_line(
        "postgresql://spielplan:a-password@db.example:5433/spielplan_test", ".env.test"
    )

    suffix = conftest._worker_suffix()
    assert line == (
        f"integration layer: ARMED against db.example:5433/spielplan_test_{suffix}"
        " (source: .env.test)"
    )
    assert line.isascii(), line
    # The line is printed on every run, including into logs a household pastes into an issue.
    assert "a-password" not in line

    # The database named is the one `pg_url` will actually create, truncation included, and the
    # port is spelled out even when the URL leaves it to the default.
    assert conftest._database_name("spielplan_test") in line
    plain = conftest.arming_line("postgresql://spielplan@127.0.0.1/spielplan_test", "env")
    assert "127.0.0.1:5432/" in plain and "(source: env)" in plain

    # Long enough for the cut to bite, or the truncation claim is untested.
    long_base = "spielplan_test_" + "b" * 60
    long_line = conftest.arming_line(f"postgresql://u@h:5432/{long_base}", "env")
    assert len(conftest._database_name(long_base)) < len(long_base), "the cut has to bite here"
    assert long_line.endswith(f"/{conftest._database_name(long_base)} (source: env)"), long_line


def test_the_unarmed_line_says_why_it_is_unarmed():
    """680 skips look the same whether the URL was missing or deliberately taken away."""
    assert conftest.arming_line(None, "unset") == (
        "integration layer: UNARMED (TEST_DATABASE_URL is unset) -- db/app/pg_url tests skip"
    )
    assert conftest.arming_line("", "unset") == conftest.arming_line(None, "unset")

    # A URL that is present and deliberately ignored: `--no-db` says so rather than pretending
    # the machine has no database.
    disarmed = conftest.arming_line(
        "postgresql://spielplan@127.0.0.1:5432/spielplan_test", ".env.test", "--no-db"
    )
    assert disarmed == "integration layer: UNARMED (--no-db) -- db/app/pg_url tests skip"
    assert disarmed.isascii(), disarmed


def _conftest_body_under(root: Path) -> dict[str, object]:
    """Runs `conftest.py`'s body with `root` as the repository
    root; its module-level writes are idempotent."""
    path = root / "backend" / "tests" / "conftest.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    namespace: dict[str, object] = {"__file__": str(path), "__name__": "conftest_under_test"}
    exec(compile(CONFTEST.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace


def test_the_arming_line_learns_the_source_before_the_loader_erases_it(tmp_path, monkeypatch):
    """Computing the source after the loader would keep every
    string test green while printing `env` for a file's URL."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    supplied = tmp_path / "an-untracked-file"
    supplied.mkdir()
    (supplied / ".env.test").write_text(
        "TEST_DATABASE_URL=postgresql://spielplan@127.0.0.1:5432/from_the_file\n", encoding="utf-8"
    )
    namespace = _conftest_body_under(supplied)
    assert namespace["_URL_SOURCE"] == ".env.test", (
        "a URL an untracked .env.test supplied is reported as one an operator exported: the "
        "arming line now says an integration run was asked for when nobody asked for it"
    )
    assert os.environ["TEST_DATABASE_URL"].endswith("/from_the_file")

    # The file names a different database on purpose, or this passes whichever the loader took.
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://spielplan@127.0.0.1:5432/from_the_env")
    namespace = _conftest_body_under(supplied)
    assert namespace["_URL_SOURCE"] == "env", namespace["_URL_SOURCE"]
    assert os.environ["TEST_DATABASE_URL"].endswith("/from_the_env"), (
        "the .env.test loader overwrote a URL the operator exported"
    )

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    namespace = _conftest_body_under(tmp_path / "no-file-at-all")
    assert namespace["_URL_SOURCE"] == "unset", namespace["_URL_SOURCE"]
    assert conftest.arming_line(None, str(namespace["_URL_SOURCE"])).startswith(
        "integration layer: UNARMED"
    )


def _module_functions(source: str) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    }


def _names_called(func: ast.FunctionDef) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_both_report_hooks_emit_the_arming_line():
    """pytest hides `pytest_report_header` under `-q`, so both hooks are needed."""
    functions = _module_functions(CONFTEST.read_text(encoding="utf-8"))

    for hook in ("pytest_report_header", "pytest_sessionstart"):
        assert hook in functions, f"conftest.py no longer defines {hook}"
        assert "_session_arming_line" in _names_called(functions[hook]), (
            f"{hook} no longer reports the arming line"
        )
    assert "arming_line" in _names_called(functions["_session_arming_line"])

    # `-q` is verbosity -1, so the terminal reporter is how the line reaches a quiet run at all.
    assert "_report_line" in _names_called(functions["pytest_sessionstart"])
    assert "write_line" in ast.unparse(functions["_report_line"]), (
        "the session-start hook no longer writes past pytest's capture, so its line joins the "
        "buffer a green run discards"
    )

    # On the comparison itself: `verbose < 0: return` would delete the line from every `-q` run.
    guard = next(node for node in functions["pytest_sessionstart"].body if isinstance(node, ast.If))
    assert isinstance(guard.test, ast.Compare), ast.unparse(guard)
    assert "verbose" in ast.unparse(guard.test.left), ast.unparse(guard.test)
    assert [type(op) for op in guard.test.ops] == [ast.GtE], ast.unparse(guard.test)
    assert [getattr(c, "value", None) for c in guard.test.comparators] == [0], ast.unparse(guard.test)
    assert isinstance(guard.body[0], ast.Return), ast.unparse(guard)


class _ConfigThatAnswers:
    """The one method `pytest_configure` asks of a config, and nothing else."""

    def __init__(self, no_db: bool) -> None:
        self._no_db = no_db

    def getoption(self, name: str, default: object = None) -> object:
        assert name == "--no-db", f"pytest_configure read an option this stub does not hold: {name}"
        return self._no_db


def test_the_no_db_flag_disarms_the_layer_the_line_says_it_disarmed(monkeypatch):
    """`pg_url` never reads `--no-db`; `pytest_configure` is the only place the flag and the state meet."""
    live = "postgresql://spielplan@127.0.0.1:5432/spielplan_test"
    monkeypatch.setenv("TEST_DATABASE_URL", live)

    conftest.pytest_configure(_ConfigThatAnswers(no_db=False))
    assert conftest.test_database_url() == live, (
        "a run without --no-db lost the URL the operator supplied"
    )

    conftest.pytest_configure(_ConfigThatAnswers(no_db=True))
    assert conftest.test_database_url() == "", (
        "--no-db left TEST_DATABASE_URL set: pg_url takes it and makes a database on the host "
        ".env.test names, under a line saying the integration layer is unarmed"
    )


@pytest.fixture
def cluster(pg_url):
    """Taking `pg_url` skips with the layer and guarantees this session's own live database exists."""
    parts = urlsplit(conftest.test_database_url())
    base = parts.path.lstrip("/") or "postgres"
    return urlunsplit(parts._replace(path="/postgres")), base


def _unreported(line: str) -> None:
    """A successful reap reports nothing, so a call here means the sweep failed."""
    raise AssertionError(f"the reap reported a failure: {line}")


def _make(admin: str, name: str) -> None:
    asyncio.run(conftest._make_database(admin, name))


def _drop(admin: str, name: str) -> None:
    asyncio.run(conftest._drop_database(admin, name))


def _exists(admin: str, name: str) -> bool:
    async def ask() -> bool:
        import asyncpg

        conn = await asyncpg.connect(admin)
        try:
            return await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name) == 1
        finally:
            await conn.close()

    return asyncio.run(ask())


def _dead_pids(count: int) -> list[int]:
    """Scanned down from above any live pid with the reaper's own probe: pids 1 and 2 are alive on Linux."""
    dead: list[int] = []
    for pid in range(999_999, 990_000, -1):
        if not conftest._pid_is_alive(pid):
            dead.append(pid)
            if len(dead) == count:
                break
    return dead


def test_a_dead_sessions_database_is_reaped(cluster):
    admin, base = cluster
    pids = _dead_pids(2)
    assert len(pids) == 2, "no pid in the scanned range reads dead: the names below are not orphans"
    dead = [f"{base}_p{pid}" for pid in pids]

    for name in dead:
        _make(admin, name)
    try:
        dropped = conftest._reap_orphaned_databases(admin, base, _unreported)
        assert set(dead) <= set(dropped), dropped
        for name in dead:
            assert not _exists(admin, name), f"{name} survived the reap"
    finally:
        for name in dead:
            _drop(admin, name)


# The System process (pid 4) on Windows, init on POSIX: someone else's process, still a process.
UNQUERYABLE_PID = 4 if os.name == "nt" else 1


def test_a_live_sessions_database_and_a_pidless_name_survive(cluster):
    """A live database looks idle between tests, a pidless name
    was chosen by a person, and an unqueryable pid is alive."""
    admin, base = cluster
    mine = conftest._database_name(base)
    keep = [f"{base}_p2_pgr", f"{base}_gw9"]
    unqueryable = f"{base}_p{UNQUERYABLE_PID}"
    assert mine not in keep and mine != unqueryable, mine
    assert conftest._pid_is_alive(UNQUERYABLE_PID), (
        f"pid {UNQUERYABLE_PID} reads as dead: a process this session may not open is still a "
        "process, and the database named after it belongs to whoever holds that pid"
    )

    for name in [*keep, unqueryable]:
        _make(admin, name)
    try:
        dropped = conftest._reap_orphaned_databases(admin, base, _unreported)
        assert mine not in dropped and _exists(admin, mine), (
            "the reaper dropped the database of a session that is still running"
        )
        for name in keep:
            assert name not in dropped and _exists(admin, name), f"{name} was not left for a human"
        assert unqueryable not in dropped and _exists(admin, unqueryable), (
            f"{unqueryable} was reaped: its pid is held by a process this session may not open, "
            "which is a live process and not a free name"
        )
    finally:
        for name in [*keep, unqueryable]:
            _drop(admin, name)


def test_a_long_base_name_does_not_shear_the_pid_the_reaper_parses():
    """At a 56-character base, truncating the whole name would store pid 63704 as `_p6370`."""
    base = "spielplan_test_" + "b" * 60
    name = conftest._database_name(base)

    assert len(name) <= 62, name
    assert name.endswith(f"_{conftest._worker_suffix()}"), name

    live = f"{conftest._name_prefix(base)}_p{os.getpid()}"
    match = conftest._reap_pattern(base).match(live)
    assert match and match.group(1) == str(os.getpid()), live


def test_a_reap_that_fails_does_not_fail_the_session(capsys):
    """The reap runs in session-fixture setup, inside capture, so `print` was invisible on a green run."""
    assert "print(" not in CONFTEST.read_text(encoding="utf-8"), (
        "conftest.py reports through print again: stdout written during fixture setup is "
        "captured and discarded on a green run, so nothing reaches the console"
    )

    unreachable = "postgresql://nobody@127.0.0.1:1/postgres"
    reported: list[str] = []

    assert conftest._reap_orphaned_databases(unreachable, "spielplan_test", reported.append) == []

    assert reported and "not reaped" in reported[0], reported
    # The exception text comes from the OS, which localises it.
    assert reported[0].isascii(), reported[0]
    assert capsys.readouterr().out == "", "the reap wrote to the buffer pytest discards"


def test_the_session_fixture_is_what_runs_the_sweep():
    """The reap tests call the helper themselves, so only this
    pins that `pg_url` calls it first, with a real report."""
    fixture = _module_functions(CONFTEST.read_text(encoding="utf-8"))["pg_url"]
    calls = {
        node.func.id: node
        for node in ast.walk(fixture)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for name in ("_reap_orphaned_databases", "_make_database"):
        assert name in calls, f"pg_url no longer calls {name}: the sweep is a helper nothing runs"
    assert calls["_reap_orphaned_databases"].lineno < calls["_make_database"].lineno, (
        "pg_url creates its own database before it sweeps, so a session that terminates between "
        "the two leaves the orphan this row exists to collect"
    )
    call = calls["_reap_orphaned_databases"]
    reporting = [*call.args[2:], *(keyword.value for keyword in call.keywords)]
    assert reporting and "_report_line" in " ".join(ast.unparse(node) for node in reporting), (
        "pg_url no longer hands the reap a way to speak: a sweep that fails during session-fixture "
        "setup then says nothing a console can see, and the leak comes back with nothing to "
        "point at"
    )


def test_the_app_fixture_mutates_the_process_only_through_monkeypatch():
    """`monkeypatch` unwinds even when `create_app()` raises before a hand-written `try`."""
    fixture = next(
        node
        for node in ast.parse(CONFTEST.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "app"
    )
    assert "monkeypatch" in {argument.arg for argument in fixture.args.args}, (
        "the app fixture no longer takes monkeypatch, so nothing unwinds its mutations when a "
        "statement between them and the yield raises"
    )
    body = ast.unparse(fixture)
    for by_hand in ("os.chdir", "os.environ"):
        assert by_hand not in body, (
            f"the app fixture mutates the process with {by_hand} again: a raise before its try "
            "leaves the rest of the session in a tmp_path with the connector seeds cleared"
        )


@pytest.fixture
def a_dot_env_in_the_working_directory(tmp_path, monkeypatch):
    """Never the repository root: this fixture must not overwrite a real `.env`."""
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".env").write_text(
        "JELLYFIN_URL=http://jellyfin.invalid\n"
        "JELLYFIN_API_KEY=an-operators-real-admin-key\n"
        "SECRETS_KEY=an-operators-secrets-key-long-enough-to-pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(home)
    return home


async def test_the_app_fixture_ignores_a_dot_env_in_the_working_directory(
    a_dot_env_in_the_working_directory, db, app
):
    """The `app` fixture runs the genuine lifespan, which seeds connectors from the environment."""
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded a connector from the .env in pytest's working directory"
    )


@pytest.fixture
def every_connector_seeded_in_the_environment(monkeypatch):
    """Both spellings: a POSIX environment keeps a lower-case seed apart."""
    names = conftest._connector_seed_env_names()
    assert names, "Settings should declare the connector seed fields"
    monkeypatch.setattr(os, "environ", dict(os.environ))
    spellings = [*names, *(name.lower() for name in names)]
    for name in spellings:
        monkeypatch.setenv(name, f"set-by-the-operators-environment-{name}")
    return spellings


async def test_the_app_fixture_clears_every_connector_seed_variable(
    secrets_key, every_connector_seeded_in_the_environment, db, app
):
    """Derived from `Settings`, so a new connector variable arrives here already neutralised."""
    for name in every_connector_seeded_in_the_environment:
        assert os.environ.get(name) is None, f"{name} reached the app fixture"
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded connectors from the environment pytest inherited"
    )
