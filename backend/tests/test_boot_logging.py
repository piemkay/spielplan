"""The backend's log records actually reach the container. Spec v2.1 §3.1, §6.6; decision 181.

`worker.py:23-25`'s `logging.basicConfig` was the only logging configuration in the repository.
`app.py` had `log = logging.getLogger("spielplan")` and nothing else, so under uvicorn's
`dictConfig` — which leaves the root logger at WARNING with no handlers — every `log.info` in the
backend was dropped and every WARNING/ERROR fell through to `logging.lastResort`, which prints the
message alone: no timestamp, no level, no logger name. A real boot that applied all 15 migrations
printed none of `lifespan`'s lines.

That is a spec failure twice over: §3.1 makes a bundle-less boot an explicitly *reported* state,
and §6.6 names logs as operator data. It is also what M4.7's upgrade runbook rests on, which tells
the operator to confirm an upgrade by grepping `docker compose logs backend` for
"applied migrations".

A subprocess, because the property is about process-wide logging configuration in the order
uvicorn establishes it: `uvicorn.Config(...)` runs `dictConfig` in its constructor and only then
imports the application. Asserting it in-process would assert whatever the pytest logging plugin
has already installed. [M4.7 ops-05]

The second half of the file is about *what the boot says* rather than whether it can say
anything: the one line the runbook greps for has to be written on both branches of
`apply_all`, or an empty grep means either "nothing was pending" or "the process died before it
got there". That needs a real database and the real lifespan, so those tests skip without
TEST_DATABASE_URL while the two above must not.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from spielplan.app import create_app
from spielplan.core.config import settings

BACKEND = Path(__file__).resolve().parents[1]

# The boot order the container has: uvicorn configures logging, then loads the app. Nothing here
# touches Postgres — `create_app()` builds the routes, and the lifespan is never entered.
_PROBE = """
import logging
import uvicorn

config = uvicorn.Config("spielplan.app:app")
config.load()
log = logging.getLogger("spielplan")
log.info("m4.7 logging probe info")
log.error("m4.7 logging probe error")
"""

# "2026-09-07 12:34:56,789 INFO    spielplan <message>" — the format `worker.py` already uses.
# Built by concatenation rather than `str.format`, because the timestamp's own `{4}`/`{3}` are
# regex quantifiers.
def _line(level: str, tag: str) -> str:
    return (
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
        + level + r"\s+spielplan m4\.7 logging probe " + tag + r"$"
    )


def _run_probe() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # `spielplan` is importable from `backend/`; the suite may be running from the repo root.
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    # §2's required config, which `create_app`'s `settings()` call now refuses to do without.
    env.setdefault("SESSION_SECRET", "pytest-session-secret-not-a-real-one")
    env.setdefault("PUBLIC_URL", "http://localhost:8080")
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, env=env, cwd=str(BACKEND), timeout=300,
    )


def test_an_info_record_reaches_stderr_with_a_timestamp_a_level_and_a_logger_name():
    """The whole of `ops-05`. INFO is the level every line the runbook greps for is emitted at,
    stderr is where Docker collects it, and the three fields are what make a log line usable a
    week later: without them "no artifact bundle active" is a sentence with no time on it."""
    done = _run_probe()
    assert done.returncode == 0, f"probe failed:\n{done.stdout}\n{done.stderr}"
    assert re.search(
        _line("INFO", "info"), done.stderr, re.MULTILINE
    ), f"no formatted INFO record on stderr:\n{done.stderr}"


def test_an_error_record_carries_the_same_shape_rather_than_the_bare_message():
    """The other half of the defect, and the more misleading one: ERROR was *visible* before this
    milestone — `logging.lastResort` prints it — so an operator reading the container log saw
    "database error" with no time, no level and no source and had no reason to suspect the rest
    of the boot was missing entirely."""
    done = _run_probe()
    assert done.returncode == 0, f"probe failed:\n{done.stdout}\n{done.stderr}"
    assert re.search(
        _line("ERROR", "error"), done.stderr, re.MULTILINE
    ), f"no formatted ERROR record on stderr:\n{done.stderr}"


# --- the line the runbook greps for, on the boot that applies nothing --------------------------
#
# These two need a real Postgres and run the genuine lifespan, because what is under test is what
# `apply_all` makes the lifespan say — not that a record can reach stderr, which is the half
# above. Skipped without TEST_DATABASE_URL (tests/conftest.py).


async def _boot(pg_url: str, tmp_path: Path) -> None:
    """One boot, exactly as the container performs it: `create_app` then the lifespan."""
    previous = {key: os.environ.get(key) for key in ("DATABASE_URL", "DATA_DIR")}
    os.environ["DATABASE_URL"] = pg_url
    os.environ["DATA_DIR"] = str(tmp_path)
    settings.cache_clear()
    try:
        application = create_app()
        async with application.router.lifespan_context(application):
            pass
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        settings.cache_clear()


def _spielplan_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "spielplan"]


async def test_a_boot_with_nothing_pending_still_writes_the_line_the_runbook_greps_for(
    db, pg_url, tmp_path, caplog
):
    """The restore case, which is the one the runbook measures — and the one that said nothing.

    `docker compose logs backend | grep 'applied migrations'` is step 1 of README's Recovery
    block and the pass condition for this milestone's own exit criterion. A dump taken from the
    running image has nothing pending, so the grep printed *silence* — the same output an
    operator gets from a backend crash-looping on a config refusal, from the wrong service name,
    and from the logging regression this file's other half exists to catch. An instrument whose
    "all is well" reading is indistinguishable from "the instrument is broken" measures nothing.
    The console script this milestone added had the branch from the start
    (`db/migrate._main` prints "(none pending)"); the boot path, which is the one the runbook
    greps, did not. [M4.7 cycle 2 findings 1 and 8; decision 181]
    """
    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    assert "applied migrations: (none pending)" in _spielplan_lines(caplog), (
        "a boot with nothing pending says nothing about migrations, so the runbook's grep "
        "cannot tell a healthy restore from a backend that never got that far"
    )


async def test_a_boot_that_applies_migrations_still_names_every_version(
    db, pg_url, tmp_path, caplog
):
    """The other half, so the constant above cannot replace the list.

    README's Upgrade block reads the same grep the opposite way round: the line "should name
    every migration written since that dump". A change that made the boot always say
    "(none pending)" would satisfy the test above and destroy the only thing the line is for.
    """
    # All three schemas, the way tests/conftest.py's `db` fixture makes an empty database: a
    # `public` dropped on its own leaves `display` standing and 0004's CREATE SCHEMA fails.
    await db.execute(
        "DROP SCHEMA public CASCADE;"
        "DROP SCHEMA IF EXISTS display CASCADE;"
        "DROP SCHEMA IF EXISTS review_store CASCADE;"
        "CREATE SCHEMA public;"
    )

    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    applied = [m for m in _spielplan_lines(caplog) if m.startswith("applied migrations: ")]
    assert applied, "a boot that applied the whole schema said nothing about it"
    assert "0001_system" in applied[0] and "0017_ops" in applied[0]
