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

# --- the yardstick the fold-in's rho is read against -------------------------------------------
#
# §14's first risk states its own mitigation as "expectations instrumented, not assumed", and
# `user_vector.cv_rho` was neither: the fold-in computes a held-out Spearman per (user, kind),
# stores it, and nothing in the app knew what a good one looked like. The corpus ships the
# reference in `cold_eval.json` - cold 0.35225 against a ceiling of 0.39193 on v20260828 - and it
# was in no file list and read nowhere. The boot says it once, beside the constants, because §10
# makes a bundle swap a restart and the pair is therefore a property of the process.
# [M4.13 step 35, cs-31]

COLD_EVAL = {
    "cold": {"spearman": 0.35225, "alpha": 0.4, "alpha0_spearman": 0.33, "partial_personal": 0.67},
    "ceiling": {"spearman": 0.39193, "alpha": 0.4, "alpha0_spearman": 0.36,
                "partial_personal": 1.0},
    "hybrid": {"spearman": 0.37},
    "cold:tunedblend_vs_prior": {"delta": 0.0191, "ci95": [0.0043, 0.0339]},
    "n_test": 1876,
}


async def _stage_active_bundle(db, tmp_path: Path, *, version: str, files: dict) -> None:
    """A bundle directory and the `artifact_bundle` row that makes it active.

    Hand-written rather than built with `make_bundle`, because what is under test is one log line
    about one file: a real fixture bundle would drag torch and a minute of import through a test
    whose subject is a string. `ArtifactStore.open` needs no manifest to read this file.
    """
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
    """The numbers, the interval and the noise floor, in one line an operator can grep.

    The floor is printed because it is what makes a difference a difference: at §0's measured
    pipeline variance (0.003-0.008 Spearman) a rho of 0.355 against the corpus's 0.35225 is a tie,
    and a log line that named the two numbers without the band would invite exactly the reading
    this milestone exists to prevent.
    """
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
    """The other arm, and the reason it exists: "instrumented, not assumed" fails the same way in
    both directions. A bundle predating `cold_eval.json` is legal - the file is optional in
    `BUNDLE_FILES` - and an install with no reference must say that, or an operator reading a
    `cv_rho` later cannot tell a missing yardstick from a silent one.

    A bundle-LESS install says nothing here, deliberately: the lifespan's "no artifact bundle
    active" line already covers it, and §3.1's first-week household does not need a second line
    about a file in a bundle it has not imported.
    """
    await _stage_active_bundle(
        db, tmp_path, version="yard-v2", files={"manifest.json": {"vocabulary_version": "v1"}}
    )

    with caplog.at_level(logging.INFO, logger="spielplan"):
        await _boot(pg_url, tmp_path)

    assert any(
        "ships no cold_eval.json" in m and "yard-v2" in m for m in _spielplan_lines(caplog)
    ), f"the absent reference is not reported: {_spielplan_lines(caplog)}"

