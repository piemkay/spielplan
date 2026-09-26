"""The backend's log records reach the container (§3.1, §6.6). A subprocess, because uvicorn runs
`dictConfig` before importing the app; the boot-line half needs TEST_DATABASE_URL."""

from __future__ import annotations

import json
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

# §14's first risk: "expectations instrumented, not assumed". The corpus's `cold_eval.json` reference.

COLD_EVAL = {
    "cold": {"spearman": 0.35225, "alpha": 0.4, "alpha0_spearman": 0.33, "partial_personal": 0.67},
    "ceiling": {"spearman": 0.39193, "alpha": 0.4, "alpha0_spearman": 0.36,
                "partial_personal": 1.0},
    "hybrid": {"spearman": 0.37},
    "cold:tunedblend_vs_prior": {"delta": 0.0191, "ci95": [0.0043, 0.0339]},
    "n_test": 1876,
}


async def _stage_active_bundle(db, tmp_path: Path, *, version: str, files: dict) -> None:
    """Hand-written, not `make_bundle`: a real bundle drags torch and a minute of import in."""
    root = tmp_path / "artifacts" / version
    root.mkdir(parents=True)
    for name, payload in files.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) "
        "VALUES ($1, '{}'::jsonb, 'active')",
        version,
    )


async def test_the_boot_states_the_reference_a_fitted_rho_is_read_against(
    db, pg_url, tmp_path, caplog
):
    """The noise floor is printed because at §0's variance a rho of 0.355 against 0.35225 is a tie."""
    await _stage_active_bundle(db, tmp_path, version="yard-v1", files={"cold_eval.json": COLD_EVAL})

    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    lines = [m for m in _spielplan_lines(caplog) if "cold_eval.json" in m]
    assert lines, (
        "the boot read the bundle and said nothing about the one reference value in it: "
        f"{_spielplan_lines(caplog)}"
    )
    line = lines[0]
    assert "0.35225" in line and "0.39193" in line, f"neither figure is in the line: {line}"
    assert "0.0043" in line and "0.0339" in line, f"the interval is missing: {line}"
    assert "0.008" in line, f"the noise floor is what makes a difference real: {line}"
    assert line.isascii(), f"a Windows cp1252 console cannot print this line: {line!r}"


async def test_a_bundle_with_no_cold_eval_says_so_rather_than_saying_nothing(
    db, pg_url, tmp_path, caplog
):
    """A bundle-LESS install says nothing here: the lifespan's "no artifact bundle active" covers it."""
    await _stage_active_bundle(
        db, tmp_path, version="yard-v2", files={"manifest.json": {"vocabulary_version": "v1"}}
    )

    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    assert any(
        "ships no cold_eval.json" in m and "yard-v2" in m for m in _spielplan_lines(caplog)
    ), f"the absent reference is not reported: {_spielplan_lines(caplog)}"

