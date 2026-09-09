"""The instrument the rest of the suite is read through: what `conftest.py` says, and what it
does to the cluster and to the app under test.

Three rules, one per M4.8 coverage row, and each of them exists because the suite was silent
about something that mattered:

  * `platform-the-suite-says-whether-the-integration-layer-ran` -- `conftest` auto-loads an
    untracked `.env.test`, so `pytest backend/tests` silently becomes an integration run against
    whatever host that file names, and 544 passed / 680 skipped reads exactly like 1,222 passed /
    2 skipped. One ASCII line now says which run this was.
  * `platform-orphaned-per-process-test-databases-are-reaped` -- `pg_url` drops its database in a
    `finally` a terminated session never reaches; 34 of them, 4,151 MB, had accumulated in the
    same cluster as the household's own data.
  * `platform-app-fixture-is-isolated-from-the-operators-env` -- the `app` fixture runs the
    genuine lifespan, which seeds connectors from the environment, and `Settings` reads `.env`
    from pytest's working directory.

The arming-line and name-building tests are pure, four more are static reads of `conftest.py`'s
own source and one calls a hook with a config of its own, because the alternative -- asserting
what a run prints, or what a fixture leaves behind after raising -- means running pytest inside
pytest. One guard does exactly that and says why: what `docs/TESTING.md` claims `-q` prints is a
claim about pytest rather than about this suite, so it is measured against two throwaway tests in
a directory of their own. The rest are integration tests and skip with the layer they are about.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests import conftest

CONFTEST = Path(conftest.__file__).resolve()


# --- the arming line ------------------------------------------------------------------------


def test_the_arming_line_names_the_database_and_where_the_url_came_from():
    """The line has to answer the three questions a silent run left open: armed or not, against
    which cluster, and on whose say-so -- the last because an exported URL and one an untracked
    file supplied are indistinguishable by the time anything else can look."""
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

    # "truncation included" needs a base long enough for the cut to bite, or the claim is
    # untested: at fourteen characters the truncating helper and an f-string that inlined the
    # same name produce the same string, so nothing here would notice the line advertising a
    # database `pg_url` never creates -- the one fact the line exists to report.
    long_base = "spielplan_test_" + "b" * 60
    long_line = conftest.arming_line(f"postgresql://u@h:5432/{long_base}", "env")
    assert len(conftest._database_name(long_base)) < len(long_base), "the cut has to bite here"
    assert long_line.endswith(f"/{conftest._database_name(long_base)} (source: env)"), long_line


def test_the_unarmed_line_says_why_it_is_unarmed():
    """An unarmed line without a reason would leave the reader where the silence did: a run of
    680 skips looks like a run of 2 whether the URL was missing or deliberately taken away."""
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
    """Run `conftest.py`'s own module body with `root` standing in for the repository root.

    Run and not read, because the third clause of this row's sentence is three lines of module
    body -- `_URL_SOURCE` assigned from the environment at `:27`, reassigned inside the loader at
    `:36` -- and a static reader would be pinning their spelling rather than their answer, which
    is the shape of guard this milestone spent cycle 3 replacing.

    The cost is the rest of the body running a second time, and here it is nothing: the only
    process-global writes at module level are two `setdefault`s and `SPIELPLAN_INSECURE_DEV = "0"`
    (`:47-63`), every one of which the real `conftest` performed at import in this same session
    and none of which can change a value; the only variable the body can actually move is
    `TEST_DATABASE_URL`, which the caller hands to `monkeypatch` first so pytest puts it back.
    Everything else at module level defines a function or a fixture, into this namespace and not
    into pytest's -- nothing here is collected or registered as a plugin.
    """
    path = root / "backend" / "tests" / "conftest.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    namespace: dict[str, object] = {"__file__": str(path), "__name__": "conftest_under_test"}
    exec(compile(CONFTEST.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace


def test_the_arming_line_learns_the_source_before_the_loader_erases_it(tmp_path, monkeypatch):
    """Where the source is decided is the whole of whether it can be true.

    The three tests above hand `arming_line` a source as a string literal, so all of them hold is
    the rendering: the derivation the line reports had no test at all, and `_URL_SOURCE` is read
    nowhere else in the tree. `conftest.py:21-26` spends six lines saying why that derivation has
    to happen where it happens -- "the last frame that can still tell the two apart: after the
    loader below has run, a URL an operator exported and a URL an untracked file supplied are the
    same string" -- and the tidy-up those six lines anticipate, computing the source once when the
    URL is settled, leaves `arming_line`'s body byte-identical and every string test green while
    every run on the household machine prints `(source: env)` over a URL nobody exported. That is
    the one half of the line an operator acts on: `env` asserts a deliberate export, and the file
    the repository does not ship is what silently turns `pytest backend/tests` into an integration
    run against whatever host it names. The `unset` arm is asserted with the other two because it
    is what keeps them distinguishable -- with it absent, `env` is what a deleted assignment
    degrades into. [M4.8 review cycle 4: M48-C4-conftest-url-source-has-no-test]
    """
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

    # An exported URL wins over the file, and is named as what it is. The file here holds a
    # different database on purpose: with the same one in both, this case passes whichever of the
    # two the loader took.
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
    """Read from the source rather than run, and both hooks rather than either.

    pytest hides `pytest_report_header` under `-q`, which is the verbosity CLAUDE.md's own command
    line and `ci.yml` both use, so the header hook alone is a line nobody sees; and at default
    verbosity the session-start hook stays quiet so the two never both speak. Losing either one
    puts the arming line back in the state this row exists to end -- invisible on the runs people
    actually perform -- and neither loss fails any other test in this suite.
    """
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

    # And on the comparison itself, not on the words in it. `verbose >= 0: return` tidied into
    # `verbose < 0: return` is the regression this row exists to prevent -- it deletes the line
    # from every `-q` run, which is every run CLAUDE.md and ci.yml perform, and makes both hooks
    # speak at default verbosity -- and a substring read of the unparsed body holds either way.
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
    """The line and the state it reports have to be the same fact, and nothing crossed them.

    `_session_arming_line` derives "UNARMED (--no-db)" from `config.getoption`; `pg_url` skips on
    a falsy `TEST_DATABASE_URL` and never reads the option at all. Those are two independent
    readings of one flag, and `pytest_configure`'s two lines are the only place they meet -- so
    the hook renamed away in a merge (with `pytest_addoption` untouched, which is what keeps
    `--no-db` parsing) leaves a run that prints `integration layer: UNARMED (--no-db)` while
    creating `<base>_p<pid>` on whatever host the untracked `.env.test` names, running the
    integration layer and shelling out to `docker exec` from `test_backup.py`. The three tests
    above hold the string and both hooks; not one of them holds the disarming, and
    `docs/TESTING.md:58-86` publishes the flag to operators with "It creates no database".

    Constructed rather than read out of the source, which is the preference
    `test_static_contracts.py:1262` records for this class of guard: a URL placeholder assigned
    instead of `""` fails here and would pass a grep, and a later spelling of the same effect
    passes here and would fail one. [M4.8 review cycle 3: m48-c3-conftest-01]
    """
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


_Q_CLAIM = re.compile(r"`-q` prints ([^.;]+)")


def test_the_ledger_describes_the_skip_pytest_actually_prints(tmp_path):
    """The paragraph that argues why the arming line exists makes a claim about what pytest
    prints, and that claim was never measured: it said `-q` prints skips as dots.

    It prints them as `s`, and counts them in the summary. The paragraph's real argument
    survives without the false clause -- 544 passed / 680 skipped reads exactly like 1,222
    passed / 2 skipped because nobody weighs the number, not because the number is missing --
    and the false version is worse than none, because it tells the maintainer CLAUDE.md sends to
    this file that a skipped run is typographically indistinguishable from a green one, so there
    is nothing to look for. Measured rather than asserted, in the one place in this file where
    that means running pytest inside pytest: two throwaway tests in a directory of their own,
    not this suite. [M4.8 review cycle 3: m48-c3-doc-01]
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n\ndef test_one():\n    pass\n\n\ndef test_two():\n"
        "    pytest.skip('the integration layer is unarmed')\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
        env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    progress = next(line for line in done.stdout.splitlines() if "[100%]" in line)
    summary = next(line for line in done.stdout.splitlines() if "passed" in line)
    # The measurement, and the two halves of it the ledger has to agree with: a skip is not
    # rendered as the character a pass is rendered as, and the run says how many there were.
    assert progress.split()[0] == ".s", progress
    assert re.search(r"\b1 skipped\b", summary), summary

    ledger = (Path(conftest.__file__).resolve().parents[2] / "docs" / "TESTING.md").read_text(
        encoding="utf-8"
    )
    paragraph = next(p for p in ledger.split("\n\n") if "A green UNARMED run" in p)
    claims = _Q_CLAIM.findall(" ".join(paragraph.split()))
    assert claims, (
        "docs/TESTING.md no longer says what `-q` prints, so this guard holds nothing: either "
        "restore the clause or delete this test with the paragraph it is about"
    )
    misread = [claim for claim in claims if "dot" in claim.lower()]
    assert not misread, (
        f"docs/TESTING.md says `-q` prints {misread}; measured just now, one pass and one skip "
        f"print {progress.split()[0]!r} and the run reports {summary!r}"
    )


# --- the reaper -----------------------------------------------------------------------------


@pytest.fixture
def cluster(pg_url):
    """The admin URL and base name behind `pg_url`, for tests that make databases by hand.

    Taking `pg_url` is deliberate twice over: it skips with the integration layer, and it
    guarantees that this session's own `<base>_p<pid>` exists, which is the live case the reaper
    must not touch.
    """
    parts = urlsplit(conftest.test_database_url())
    base = parts.path.lstrip("/") or "postgres"
    return urlunsplit(parts._replace(path="/postgres")), base


def _unreported(line: str) -> None:
    """Where a reap that succeeds sends nothing. The two tests below drive the reaper against a
    reachable cluster, so a call here means the sweep failed and the assertion that follows will
    say so about the databases; this exists to be the caller `pg_url` is, without a config."""
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
    """`count` pids no process holds, asked of the probe rather than assumed.

    The first version of this named pids 1 and 2, which is a Windows fact wearing portable
    clothes: there `OpenProcess` fails with ERROR_INVALID_PARAMETER for both. On Linux -- which
    is what `ci.yml`'s `integration` job runs, with `TEST_DATABASE_URL` set, so this test is
    armed and not skipped -- pid 1 is init and pid 2 is kthreadd, and `_pid_is_alive` reports
    both alive, correctly: as an unprivileged user `os.kill(1, 0)` raises PermissionError, which
    its POSIX arm reads as "someone else's process, which is still a process". So the test failed
    on its first line there, and deleting that guard would only have moved the failure -- the
    reaper would rightly decline to drop a live pid's database and the drop assertion would fail
    instead. Scanning down from a number above any live pid asks the same probe the reaper asks,
    on whatever platform is running, which is the only form of this precondition that is true by
    construction rather than by which desk it was written at. [M4.8 dd29, review cycle 2]
    """
    dead: list[int] = []
    for pid in range(999_999, 990_000, -1):
        if not conftest._pid_is_alive(pid):
            dead.append(pid)
            if len(dead) == count:
                break
    return dead


def test_a_dead_sessions_database_is_reaped(cluster):
    """The whole point: a session that was terminated never ran its `finally`, and nothing else
    in the tree ever comes back for what it left."""
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


# A pid this session may not ask about. `OpenProcess` fails with ERROR_ACCESS_DENIED for a
# process this account has no rights over -- the System process at pid 4 is one on every Windows
# box, another account's pytest is the case that matters -- and on POSIX `os.kill(1, 0)` raises
# PermissionError for exactly the same reason. Both mean "someone else's process, which is still
# a process", which is why the two arms below have to agree about it. [M4.8 dd29, review cycle 2]
UNQUERYABLE_PID = 4 if os.name == "nt" else 1


def test_a_live_sessions_database_and_a_pidless_name_survive(cluster):
    """The three ways a reaper turns into the bug it was meant to fix.

    A live session's database looks exactly like a dead one from outside -- `db` closes its
    connection between tests, so "no connections" is the normal state of a running suite, and a
    reaper that read that would reproduce the cross-process destruction `pg_url`'s docstring
    records. And a name that carries no pid is a name a person chose: `test_backup.py`'s `_pgr`
    sibling, an xdist worker's `_gw0`, the two hand-made ad-hoc databases in this cluster.

    The third is a pid the probe is not allowed to look at. The Windows arm returned False for
    every `OpenProcess` failure, which conflates "there is no such process" with "you may not ask
    about that one" -- and those mean opposite things to a reaper that drops WITH (FORCE). The
    account boundary is not hypothetical here: decision 183's corpus job runs pytest under a
    runner's own service account against the same cluster and base name the household's own
    `.env.test` names, and an elevated run is one right-click away, so a session under either
    would have classified the other's live database as an orphan and terminated it.
    """
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
    """The name is 62 characters at most, and the part that gets cut is never the pid.

    `<base>_p<pid>` truncated as one string cuts the tail, and the tail is the only thing telling
    the reaper whose database this is: at a 56-character base the stored name carries `_p6370`
    for pid 63704, a different -- and by then almost certainly dead -- process, so the next
    session would drop a database a live one is using, which is the cross-process destruction
    `pg_url`'s docstring records rather than the leak `_reap` was written for. So the base is cut
    instead, by a constant that leaves room for the longest suffix this convention writes: the
    reaper has to rebuild the same prefix for names written by processes whose pids are shorter
    than its own, which it cannot do from its own suffix length. No base in the tree is long
    enough today -- `spielplan_test` is fourteen -- and a CI or worktree name built from a branch
    and a job id is one rename away from it.
    """
    base = "spielplan_test_" + "b" * 60
    name = conftest._database_name(base)

    assert len(name) <= 62, name
    assert name.endswith(f"_{conftest._worker_suffix()}"), name

    live = f"{conftest._name_prefix(base)}_p{os.getpid()}"
    match = conftest._reap_pattern(base).match(live)
    assert match and match.group(1) == str(os.getpid()), live


def test_a_reap_that_fails_does_not_fail_the_session(capsys):
    """A tidy-up that could not run is not a suite that failed -- and it is not a silent one
    either, or the leak comes back with nothing to point at.

    Silent is exactly what `print` was here. `pg_url` is session-scoped, so the reap runs during
    fixture SETUP, inside pytest's capture, and a green run throws that buffer away: the warning
    was invisible at `-q` -- the verbosity CLAUDE.md and both `ci.yml` pytest steps use -- and at
    default verbosity too, while this test read the same discarded buffer through `capsys` and
    passed. The mechanism is asserted first below because the message's wording is the lesser
    half: a correctly worded warning nobody can see is the state this row was written about.
    """
    assert "print(" not in CONFTEST.read_text(encoding="utf-8"), (
        "conftest.py reports through print again: stdout written during fixture setup is "
        "captured and discarded on a green run, so nothing reaches the console"
    )

    unreachable = "postgresql://nobody@127.0.0.1:1/postgres"
    reported: list[str] = []

    assert conftest._reap_orphaned_databases(unreachable, "spielplan_test", reported.append) == []

    assert reported and "not reaped" in reported[0], reported
    # CLAUDE.md: the console is cp1252/cp850 on this project's machines, and the exception text
    # here comes from the OS, which localises it.
    assert reported[0].isascii(), reported[0]
    assert capsys.readouterr().out == "", "the reap wrote to the buffer pytest discards"


def test_the_session_fixture_is_what_runs_the_sweep():
    """The helper is not the mechanism: one line in `pg_url` is, and nothing held it.

    All four tests above drive `_reap_orphaned_databases` themselves, each with a `report` of its
    own, and they have to -- `cluster` takes `pg_url`, so the session's own sweep has already run
    by the time any of them creates a hand-made orphan, and the fixture's call is structurally
    incapable of seeing what they make. Which means deleting `pg_url`'s call left the whole set
    green while the leak came back: measured, a session that uses `pg_url` and is not one of the
    reap tests left two dead-pid orphans standing where the shipped fixture drops them. The
    ordering is half the row's own sentence ("BEFORE creating its own database"), and the `report`
    argument is the other half of `test_a_reap_that_fails_does_not_fail_the_session`: that test
    hands in its own `reported.append`, so a `lambda line: None` here would leave it green and the
    warning unreachable -- the silence it was written to end. `assert "print(" not in ...` above
    is file-wide and cannot see either loss. [M4.8 review cycle 3: m48-c3-conftest-02]
    """
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


# --- the app fixture and the operator's environment -------------------------------------------


def test_the_app_fixture_mutates_the_process_only_through_monkeypatch():
    """Whatever the fixture takes away from the process, pytest has to give back on any exit.

    The isolation is four process-global mutations -- `DATABASE_URL`, `DATA_DIR`, the six
    connector seed variables and the working directory -- and they were made by hand, above a
    `try` whose `finally` restored them. Five statements sat in between, two of which raise
    today: `create_app()` opens with `_refuse_multiple_workers()` (RuntimeError on an exported
    `WEB_CONCURRENCY`) and `settings()`, whose §2 validator refuses under decision 181 -- and
    `settings.cache_clear()` immediately above guarantees that construction is a fresh one. One
    of those raises and the finally never runs: every later test in the session resolves `.env`,
    `Path(".")` and its relative fixtures inside a pytest tmp_path with the connector seeds
    gone, one red test turns into a session of failures naming a temp directory, and on Windows
    pytest cannot even delete that tree because it is a live process's cwd.

    `monkeypatch` unwinds whether or not the fixture body completed, which is why
    `no_secrets_key` fifty lines below has used it since the day the same incident was recorded
    there. A guard rather than a comment because the manual spelling reads perfectly correct.
    """
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
    """The `.env` `.env.example` documents and the README tells every developer to write, in the
    directory pytest runs from. Never the repository root: this fixture must not be able to
    overwrite a real one."""
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
    """§2 lets env vars seed connector config on first boot, and the `app` fixture runs the
    genuine lifespan -- so on the machine where the release is cut, the app under test booted
    with the household's real Jellyfin already configured and its API key encrypted into a
    per-process test database. Two committed unconfigured-state tests in
    `test_jellyfin_link.py` went red for a reason that had nothing to do with the code.
    """
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded a connector from the .env in pytest's working directory"
    )


@pytest.fixture
def every_connector_seeded_in_the_environment(monkeypatch):
    names = conftest._connector_seed_env_names()
    assert names, "Settings should declare the connector seed fields"
    for name in names:
        monkeypatch.setenv(name, f"set-by-the-operators-environment-{name.lower()}")
    return names


async def test_the_app_fixture_clears_every_connector_seed_variable(
    secrets_key, every_connector_seeded_in_the_environment, db, app
):
    """The other half of the same door. Clearing the variables `Settings` declares, rather than a
    hand-written list of the two Jellyfin ones, is what makes a seventh connector added to
    `Settings` arrive here already neutralised."""
    for name in every_connector_seeded_in_the_environment:
        assert os.environ.get(name) is None, f"{name} reached the app fixture"
    assert await db.fetchval("SELECT count(*) FROM connector_config") == 0, (
        "the lifespan seeded connectors from the environment pytest inherited"
    )
