"""The backend's log records reach the container (§3.1, §6.6). A subprocess, because uvicorn runs
`dictConfig` before importing the app; the boot-line half needs TEST_DATABASE_URL."""

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

# The container's boot order: uvicorn configures logging, then loads the app. No Postgres.
_PROBE = """
import logging
import uvicorn

config = uvicorn.Config("spielplan.app:app")
config.load()
log = logging.getLogger("spielplan")
log.info("m4.7 logging probe info")
log.error("m4.7 logging probe error")
"""

# Concatenated rather than `str.format`, because the timestamp's `{4}`/`{3}` are regex quantifiers.
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
    """INFO is the level the runbook greps, stderr is where Docker collects it."""
    done = _run_probe()
    assert done.returncode == 0, f"probe failed:\n{done.stdout}\n{done.stderr}"
    assert re.search(
        _line("INFO", "info"), done.stderr, re.MULTILINE
    ), f"no formatted INFO record on stderr:\n{done.stderr}"


def test_an_error_record_carries_the_same_shape_rather_than_the_bare_message():
    """ERROR used to reach `logging.lastResort`, which prints the bare message."""
    done = _run_probe()
    assert done.returncode == 0, f"probe failed:\n{done.stdout}\n{done.stderr}"
    assert re.search(
        _line("ERROR", "error"), done.stderr, re.MULTILINE
    ), f"no formatted ERROR record on stderr:\n{done.stderr}"


_HTTPX_PROBE = """
import logging
import spielplan.{module}
logging.getLogger("httpx").info("HTTP Request: GET https://api.themoviedb.org/3/x?api_key=LEAKME")
logging.getLogger("spielplan").info("still logging")
"""


def test_neither_process_logs_httpx_request_urls():
    """httpx puts query keys in its INFO line; the worker once leaked the TMDB key that way."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.setdefault("SESSION_SECRET", "pytest-session-secret-not-a-real-one")
    env.setdefault("PUBLIC_URL", "http://localhost:8080")
    for module in ("app", "worker"):
        done = subprocess.run(
            [sys.executable, "-c", _HTTPX_PROBE.format(module=module)],
            capture_output=True, text=True, env=env, cwd=str(BACKEND), timeout=300,
        )
        assert done.returncode == 0, f"{module} probe failed:\n{done.stderr}"
        assert "still logging" in done.stderr
        assert "LEAKME" not in done.stderr, f"{module} logged an httpx request URL"


async def _boot(pg_url: str, tmp_path: Path) -> None:
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
    """An empty grep must not be indistinguishable from a crash-looping backend."""
    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    assert "applied migrations: (none pending)" in _spielplan_lines(caplog), (
        "a boot with nothing pending says nothing about migrations, so the runbook's grep "
        "cannot tell a healthy restore from a backend that never got that far"
    )


async def test_a_boot_that_applies_migrations_still_names_every_version(
    db, pg_url, tmp_path, caplog
):
    """So a constant "(none pending)" cannot replace the list."""
    # All three schemas: a `public` dropped alone leaves `display` standing and 0004's CREATE SCHEMA fails.
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
