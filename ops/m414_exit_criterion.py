"""M4.14's exit criterion, measured against the 1.04 GB archive the corpus actually built.

§12 gives M4.14 no row of its own until this milestone writes one; what it is measured on is the
plan's own sentence:

    A 1.04 GB real-bundle archive imports through the Data tab with the HTTP request returning
    in under 5 s and progress polled to completion, surviving a proxy that cuts at 100 s; every
    file listed in BUNDLE.json is sha256-verified before a single row is written; and each of a
    truncated archive, a plain file under /data/import, a bundle staged under /data/artifacts, a
    missing vocabulary, an orphan row, an unsafe version token and a second concurrent import
    ends as a report line with the install byte-identical to before -- no reusable `.unpacked-*`
    tree, no half-staged version directory, no 500.

Thirteen checks, and they are the plan's own pass table. This script numbers them in the order it
can honestly run them and names the plan's number in every heading, the way
`ops/m413_exit_criterion.py` does. THE ORDER IS NOT A PREFERENCE. `validate_for_install` returns
the install-state refusal ALONE when there is one ("a bundle that cannot be imported at this
install *at all* gets the one line the operator can act on"), so decision 162's `seed-once`
swallows every content rule the moment this install has seeded. The six refusals that are
statements about a BUNDLE -- the archive door, the path boundary, the vocabulary, an orphan row,
an unsafe token -- are therefore measured against an install that has not seeded yet, which is
also the install a first boot meets them on. The import follows, and the three checks that are
statements about a SEEDED install (the phase, the concurrent refusal, the after-state) follow it.

    script  plan  what it measures
      1       7   README.txt as the path: 400 both times, and no tree left behind
      2       6   a truncated archive leaves nothing, and a good one of that name then validates
      3       8   a bundle under /data/artifacts/<v>/ is refused before anything is deleted
      4       9   a seed with DNA rows and no dna_vocab/ is a validation failure
      5      10   one injected orphan names the table, the column, the count and the first ids
      6      11   bundle_version 'v2026/08' refuses with ONE version on the screen
      7       5   42/42 files sha256-verified before a single row is written, in seconds
      8       1   POST /api/admin/bundle/import answers in under 5 s (measured before: 127 s)
      9       2   /api/health answers every second from the press to the flip, each under 5 s
     10       3   the phase runs queued -> running -> active and ends with report.ok
     11       4   a client that gives up at 5 s changes nothing
     12      12   two imports at once: one ok, one "already running", one directory staged
     13      13   after every refusal: artifact_bundle unchanged, /data/artifacts unchanged, no 500

THE ARCHIVE IS BUILT HERE, AND THAT IS PART OF THE CRITERION. The bundle on disk is a directory;
the `.tar` / `.tar.zst` path had never been exercised at any layer, and half of what M4.14 fixes
lives in `importer/bundle._unpack`. So this script tars `CORPUS_BUNDLE_DIR` into its own scratch
tree first -- the same layout `tar -C <parent> -cf v20260828.tar v20260828` produces -- and every
check afterwards names that archive. The corpus directory is READ-ONLY here and is never written
to: the probes the refusal checks need are hard-link trees over the extraction, with the one file
each probe edits broken out of its link set first.

THE WORKER IS A SECOND PROCESS, AND CHECK 9 IS WHY. §2 runs backend and worker as two services,
and the whole of M4.14's thesis is that the 127 s import is not on the BACKEND's event loop.
`import_bundle` blocks its own loop for minutes -- `shutil.copytree` of 205 MB, `repair_mojibake`
over 485,602 review bodies, `run_rebuild`'s numpy refits and torch forward pass are all
synchronous -- so a harness that pumps `worker._tick` on the same loop as the ASGI app measures
its own collapse of two processes into one and reports a health outage that production does not
have. Decision 255 asks for the app driven over `httpx.ASGITransport` and for the worker's tick
pumped; this pumps `worker._tick`, in the process §5.3 files the job under. The child's own source
is `_WORKER_PUMP` below, written into the scratch tree so its traceback has a file to name.

It connects through `db/pool._init_connection` and never a bare `asyncpg.connect`: without the
json/jsonb codec `title_meta.payload` dies with "expected str, got dict", which is the defect
M4.5's harness spent three runs believing was in the importer.

It refuses to run on the fixture, for the reason `ops/m45_exit_criterion.py` does: the fixture is
a few megabytes, it is written by the same repository that reads it, and not one of the thirteen
measurements below can be falsified by it.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m414_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by accident --
0003 creates schemas of its own, so a search_path would not have isolated it -- and it stages
into a temporary DATA_DIR it removes. Output is ASCII: Windows consoles crash on decorative
glyphs, and every string this script did not author goes through `console()` on the way out --
the importer's own em-dashed finding messages, which it prints on every refused probe, and the
paths and version tokens it is handed by the operator and by BUNDLE.json.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s helpers can be imported rather than copied: M4.5's
# recorded 18/18 has to stay reproducible unchanged, so that file is read and never edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. `setdefault` would read
# a developer's `.env` *after* this and pydantic-settings ranks the environment above that file,
# so these two are set outright: this script must never seal a household's real SECRETS_KEY into
# a scratch database, and a throwaway key is all the encryption here means.
os.environ["SESSION_SECRET"] = "m414-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m414-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.importer import validate as validator  # noqa: E402
from spielplan.importer.report import ImportReport  # noqa: E402

# The plan's own numbers, as constants, so a reader can see what each verdict is against.
#
# POST_BUDGET_S is the criterion's headline and the one number M4.14 exists for: the route used to
# await the whole import, measured at 127 s on this archive. HEALTH_BUDGET_S is the image's
# HEALTHCHECK timeout (`curl -fsS --max-time 5`), which is the bound that decides whether Docker
# calls the backend unhealthy; the app's own per-step bound is tighter (`app._HEALTH_TIMEOUT_S`
# is 2) and a sample between the two would pass here and still be a real regression, so the max
# is printed beside the verdict rather than only compared to it.
POST_BUDGET_S = 5.0
HEALTH_BUDGET_S = 5.0
HEALTH_INTERVAL_S = 1.0

# "seconds, not minutes" as a number. The hash pass over 42 files and 1.04 GB was measured at
# 0.64 s warm plus about 1.0 s for the two `PRAGMA quick_check`s; a minute is two orders of
# magnitude of headroom and still refuses the shape the plan is about, which is an integrity pass
# that costs so much nobody leaves it on.
INTEGRITY_BUDGET_S = 60.0

# §2 puts the origin behind Traefik and Cloudflare, which cut a proxied request with no origin
# response at 100 s. That cannot be reproduced against Cloudflare from here, so the honest
# observable is a client that gives up while `/api/health` is sampled every second: the import
# must complete and flip anyway.
DISCONNECT_AFTER_S = 5.0

# §5.3 budgets this job in "minutes" and `worker.BUNDLE_IMPORT_TIMEOUT` is 600 s (decision 300
# moved it there from 300 s, with the worker's `stop_grace_period`, which `test_box_claims.py`
# pins to it). `release.yml`'s leg 4 is what runs this import through that job, on a
# `[self-hosted, spielplan-corpus]` runner nobody has registered and therefore nobody has timed:
# the 600 s is sized off the 213 s measured plus the unmeasured cost of containerised I/O under a
# hypervisor, and not off any comparison between machines. This comment gave that runner's speed
# as decision 300's reason until M4.16 cycle 4, which is the claim decision 316 refuses -- a
# measured property of a box that has never existed. [decision 316]
# The deadline here is that budget plus the child's own start-up (torch, numpy, the
# pool) and a margin, because a deadline that fires is a run with no measurement rather than a
# failed measurement. It is 2x the budget now rather than 4x, which is still a margin over a job
# measured at 213 s: what it must outlast is the worker's own cancellation, not the import.
IMPORT_DEADLINE_S = 1200.0
PUMP_INTERVAL_S = 1.0

# 90%, because that is the cut that was measured: the first `validate` raised and the SECOND
# returned ok=True over a 332-of-436 MB `reviews.sqlite`, which is the defect A3 closes.
TRUNCATE_FRACTION = 0.9

# The floor under which this is the fixture and not the corpus. The fixture bundle inventories a
# few megabytes; v20260828 inventories 1,042,461,726. Read off BUNDLE.json's own `total_bytes`
# rather than off the directory, because a bundle that has been half-copied is exactly what this
# script must be able to tell apart from one that is small by design.
MIN_REAL_BYTES = 500_000_000

ADMIN_NAME = "patrick"
ADMIN_PASSWORD = "an-exit-criterion-password"

# The bundle path every check names, relative to the scratch import directory.
ARCHIVE_SUFFIX = ".tar"
TRANSFER_NAME = "transfer.tar"

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "a plain file as the path is refused twice and leaves no tree (plan check 7)"),
    (2, "two truncations leave nothing, and a good archive of that name validates (plan check 6)"),
    (3, "a bundle under the artifacts root is refused before anything is deleted (plan check 8)"),
    (4, "a seed with DNA rows and no dna_vocab is a validation failure (plan check 9)"),
    (5, "one injected orphan names the table, the column, the count and the ids (plan check 10)"),
    (6, "an unsafe version token refuses with one version on the screen (plan check 11)"),
    (7, "every listed file is sha256-verified before a row is written (plan check 5)"),
    (8, "POST /api/admin/bundle/import answers in under 5 s (plan check 1)"),
    (9, "/api/health answers every second throughout the import (plan check 2)"),
    (10, "the phase runs queued -> running -> active and ends with report.ok (plan check 3)"),
    (11, "a client that gives up at 5 s changes nothing (plan check 4)"),
    (12, "two imports at once: one ok, one refused, one directory staged (plan check 12)"),
    (13, "after every refusal the install is unchanged and nothing answered 500 (plan check 13)"),
)

# The worker, as the process §5.3 files this job under. Written into the scratch tree and run as a
# child of this script -- see the module docstring's "THE WORKER IS A SECOND PROCESS".
#
# Every other row of the table is stamped as having just run, so `due` returns nothing and the
# only thing that can fire is `_with_queued_import`: the admin action's trigger, which is the
# thing under test. A full tick against a fresh install fires six anchored jobs including §2's
# nightly `pg_dump`, and a run whose import waited behind a dump of its own database would be
# measuring the harness. The pump interval is a second rather than `worker.TICK_SECONDS`' twenty
# for the same reason: what the criterion measures is the import, not the poll that finds it.
_WORKER_PUMP = '''"""One worker process, pumping section 5.3's loop over one scratch install.

Written and run by ops/m414_exit_criterion.py; see its module docstring for why the worker is a
process here rather than a task. Nothing outside that script should run this file.
"""
import asyncio
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.environ["SPIELPLAN_BACKEND"])

from spielplan import worker
from spielplan.db import pool


async def main():
    await pool.open_pool(max_size=4)
    try:
        deadline = time.monotonic() + float(os.environ["SPIELPLAN_PUMP_SECONDS"])
        interval = float(os.environ["SPIELPLAN_PUMP_INTERVAL"])
        print("pump: ticking", flush=True)
        while time.monotonic() < deadline:
            now = time.monotonic()
            local = datetime.now()
            last_run = {job.name: now for job in worker.JOBS}
            last_date = {job.name: local.date() for job in worker.JOBS}
            await worker._tick(now, local, last_run, last_date)
            await asyncio.sleep(interval)
    finally:
        await pool.close_pool()


asyncio.run(main())
'''


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit as the other exit scripts: an untracked file names
    the server, so the line below is the only place this script decides which host it is allowed
    to create a database on.
    """
    path = ROOT / ".env.test"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            return value.strip()
    return None


def _neutralise_connector_env() -> None:
    """Take the operator's own connector credentials away before the app boots.

    `backend/tests/conftest.py` records the incident this repeats: the genuine lifespan calls
    `registry.seed_from_env`, so a household that followed its own README boots this scratch
    install with their real Jellyfin configured -- and their encrypted credentials are then
    written into a throwaway database under a throwaway key. Derived from `Settings` rather than
    listed, so a seventh connector variable cannot be forgotten here.
    """
    from spielplan.core.config import Settings

    for name in Settings.model_fields:
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_")):
            os.environ.pop(name.upper(), None)


# --- the tree the probes are cut from ---------------------------------------------------------


def _build_archive(source: Path, target: Path) -> tuple[float, int]:
    """`tar -C <parent of source> -cf <target> <source.name>`, in Python. Returns (seconds, bytes).

    The layout matters and is the one the plan names: the archive holds ONE top-level directory,
    so `_unpack`'s `_single_child` finds the bundle root inside the extraction. Built with
    `tarfile` rather than shelled out to `tar`, because this script is the thing being measured
    and a bsdtar that is present on one operator's box and absent on the next would make the
    criterion unreproducible. The corpus directory is opened for reading only.
    """
    began = time.perf_counter()
    with tarfile.open(target, "w") as tar:
        tar.add(source, arcname=source.name)
    return time.perf_counter() - began, target.stat().st_size


def _truncated_copy(source: Path, target: Path, fraction: float, *, keep: int | None = None) -> int:
    """The first `fraction` of `source`'s bytes, which is what a transfer that died looks like.

    `keep` names the cut in bytes instead, for the cut that has to land ON a member header rather
    than at a proportion of the file. [M4.14 cycle 1, m414-c1-dim-refusals-01]
    """
    keep = int(source.stat().st_size * fraction) if keep is None else keep
    written = 0
    with source.open("rb") as src, target.open("wb") as dst:
        while written < keep:
            chunk = src.read(min(8 << 20, keep - written))
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
    return written


def _detach(path: Path) -> None:
    """Break one file out of its hard-link set, so editing it cannot reach the original."""
    spare = path.with_name(path.name + ".detached")
    shutil.copy2(path, spare)
    path.unlink()
    os.replace(spare, path)


def _link_tree(source: Path, target: Path) -> Path:
    """A second bundle root over the same bytes: every file hard-linked, BUNDLE.json copied.

    Five of the thirteen checks need a bundle they may edit, and the only real bundle on this
    machine is 995 MB and read-only. Copying it five times is twenty minutes of disk for
    measurements that each change one file, so the probe is a link tree and the one file a probe
    edits is detached first. BUNDLE.json is detached here rather than on demand because every
    probe rewrites it: `_reinventory` below is what keeps the refusal under test from arriving
    underneath a `bundle-integrity` failure the probe itself created.
    """
    target.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
            continue
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        os.link(path, target / relative)
    _detach(target / "BUNDLE.json")
    return target


def _reinventory(root: Path) -> int:
    """Rewrite BUNDLE.json's `files` and `total_bytes` over the tree as it is now.

    B1 hashes every listed file before a row is written, which means a probe that removes a
    directory or adds a row to `content.sqlite` IS a bundle whose inventory no longer describes
    it -- correctly, and that failure would then sit on top of the one being measured. The probe
    re-states the corpus's inventory so each refusal check reports on its own subject.
    `backend/tests/fixtures/make_bundle.reinventory` does the same thing for the fixture and for
    the same reason; this is not that function because an ops script must not import the test
    fixtures it is meant to be independent of.

    `validator._sha256` is reached through the module rather than re-implemented, for the reason
    `ops/m45_exit_criterion.py` reaches `pool._init_connection` and `reconcile._vocab_version`: a
    harness measures what the app does, and a second digest written here is a second answer.
    Every declaration the probe planted -- `validations`, `nullable_pk_columns`, the version --
    is left exactly as it is.
    """
    path = root / "BUNDLE.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files: dict[str, dict[str, Any]] = {}
    total = 0
    for entry in sorted(root.rglob("*")):
        if not entry.is_file():
            continue
        name = entry.relative_to(root).as_posix()
        if name == "BUNDLE.json":
            continue
        size = entry.stat().st_size
        files[name] = {"bytes": size, "sha256": validator._sha256(entry)}
        total += size
    manifest["files"] = files
    manifest["total_bytes"] = total
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return len(files)


def _tree(root: Path) -> list[tuple[str, int]]:
    """Every file under `root` as (relative path, size), sorted -- a snapshot to compare against."""
    if not root.exists():
        return []
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )


def _findings(report: dict[str, Any], rule: str, severity: str = "fail") -> list[dict[str, Any]]:
    return [
        f for f in report.get("findings", [])
        if f.get("rule") == rule and f.get("severity") == severity
    ]


def _first_failure(report: dict[str, Any]) -> str:
    for finding in report.get("findings", []):
        if finding.get("severity") == "fail":
            return f"{finding['rule']}: {finding['message'][:150]}"
    return "(no failure in the report)"


# --- the install every check reads ------------------------------------------------------------


class Session:
    """One httpx client over the ASGI app, with every status it saw recorded for check 13.

    `raise_app_exceptions=False` on purpose, and it is the only reason check 13 can say anything:
    with the default, an unhandled exception in a handler is re-raised into the caller and this
    script would report "the check stopped on FileNotFoundError" where the criterion's sentence
    is "no 500". A browser gets the 500 that Starlette's ServerErrorMiddleware has already
    written, so that is what this client must get too.
    """

    def __init__(self, install: Install, timeout: float) -> None:
        self._install = install
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=install.app, raise_app_exceptions=False),
            base_url="http://test",
            timeout=timeout,
        )

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        answer = await self._client.get(path, **kwargs)
        self._install.responses.append(("GET", path, answer.status_code))
        return answer

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        answer = await self._client.post(path, **kwargs)
        self._install.responses.append(("POST", path, answer.status_code))
        return answer

    async def aclose(self) -> None:
        await self._client.aclose()


class Install:
    """The scratch install, the archive, and everything the import records for checks 9 to 12."""

    def __init__(self, conn: asyncpg.Connection, app: Any, work: Path, corpus: Path) -> None:
        self.conn = conn
        self.app = app
        self.work = work
        self.corpus = corpus
        self.import_dir = work / "import"
        self.artifacts_dir = work / "artifacts"
        self.probes = work / "probes"
        self.neutral = work / "no-dot-env"
        self.pump = work / "worker_pump.py"
        self.worker_log = work / "worker.log"
        self.dsn = ""
        self.archive = self.import_dir / "bundle.tar"
        self.extracted = self.import_dir
        self.version = ""
        self.listed_files = 0
        self.responses: list[tuple[str, str, int]] = []
        self.sessions: list[Session] = []
        self.health: list[tuple[float, float, int]] = []
        self.health_stop = asyncio.Event()
        self.worker: subprocess.Popen[bytes] | None = None
        self._worker_out: Any = None
        self.admin: Session | None = None
        self.operator: Session | None = None
        self.rival: Session | None = None
        # What check 8 records and checks 9 to 12 read.
        self.post_seconds: float | None = None
        self.post_status: int | None = None
        # The two instants that divide the health series: when Import was pressed, and when the
        # 202 came back. Everything between them is work the route still does synchronously.
        self.press_at: float | None = None
        self.accepted_at: float | None = None
        self.post_payload: dict[str, Any] | None = None
        self.disconnected_at: float | None = None
        self.phases: list[str] = []
        self.rival_status: int | None = None
        self.rival_detail: str = ""
        self.final_state: dict[str, Any] | None = None
        self.import_seconds: float | None = None
        self.staged_entries: list[str] = []

    def session(self, timeout: float = 300.0) -> Session:
        made = Session(self, timeout)
        self.sessions.append(made)
        return made

    async def login(self) -> Session:
        """A second admin session over the same account, for the reads the operator is not making."""
        client = self.session()
        answer = await client.post(
            "/api/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD}
        )
        if answer.status_code != 200:
            raise RuntimeError(f"the admin could not sign in again: {answer.status_code}")
        return client

    def start_worker(self) -> None:
        """Start the child that pumps `worker._tick`. See the module docstring."""
        self.pump.write_text(_WORKER_PUMP, encoding="ascii")
        env = dict(os.environ)
        env.update({
            "SPIELPLAN_BACKEND": str(ROOT / "backend"),
            "SPIELPLAN_PUMP_SECONDS": str(IMPORT_DEADLINE_S),
            "SPIELPLAN_PUMP_INTERVAL": str(PUMP_INTERVAL_S),
            "SPIELPLAN_ROLE": "worker",
            "DATABASE_URL": self.dsn,
            "DATA_DIR": str(self.work),
            "PYTHONIOENCODING": "utf-8",
        })
        self._worker_out = self.worker_log.open("wb")
        self.worker = subprocess.Popen(
            [sys.executable, str(self.pump)],
            cwd=str(self.neutral), env=env,
            stdout=self._worker_out, stderr=subprocess.STDOUT,
        )

    def stop_worker(self) -> None:
        if self.worker is not None:
            with contextlib.suppress(Exception):
                self.worker.terminate()
            with contextlib.suppress(Exception):
                self.worker.wait(timeout=30)
            with contextlib.suppress(Exception):
                self.worker.kill()
            self.worker = None
        if self._worker_out is not None:
            with contextlib.suppress(Exception):
                self._worker_out.close()
            self._worker_out = None

    def worker_tail(self, lines: int = 12) -> str:
        if not self.worker_log.is_file():
            return "(the worker wrote no log)"
        text = self.worker_log.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    async def bundle_state(self) -> dict[str, Any]:
        client = self.rival or self.admin
        if client is None:
            raise PreconditionFailed("no admin session")
        return (await client.get("/api/admin/bundle/state")).json()


async def _sample_health(install: Install, probe: Session) -> None:
    """GET /api/health once a second until the import is done. Never raises.

    Both numbers are kept, because they answer different questions and only one of them survives
    the failure this exists to catch. A slow ANSWER shows up as a latency; a BLOCKED event loop
    shows up as a gap between two samples, because the sampler is on that loop too and does not
    get to run at all. The second is the shape the 127 s import had.
    """
    while not install.health_stop.is_set():
        started = time.perf_counter()
        try:
            status = (await probe.get("/api/health")).status_code
        except Exception:
            status = 0
        answered = time.perf_counter() - started
        install.health.append((started, answered, status))
        # A period, not a delay. Sleeping a fixed second AFTER each answer makes the sample
        # rate a function of the latency being measured, so a probe that slows down is also
        # sampled less often -- and the count below, which is what catches a loop that went
        # quiet, would then be satisfied by the very thing it exists to report.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                install.health_stop.wait(), timeout=max(0.0, HEALTH_INTERVAL_S - answered)
            )


# --- the checks --------------------------------------------------------------------------------


async def check_one(ctx: Install) -> tuple[bool, str, str]:
    """plan check 7: README.txt as the path is 400 both times, and leaves no directory.

    Both times, because the second attempt is where the defect was: `Bundle.open` handed every
    non-directory to `tarfile.open`, so the `ReadError` was a 500 rendered as a bare string, an
    empty `.unpacked-README.txt/` was left behind -- and the retry was then diagnosed as "this is
    a models-only bundle and the install has no content", a decision-162 refusal for a typo.
    """
    readme = ctx.import_dir / "README.txt"
    readme.write_text("copy your bundle or its .tar into this directory", encoding="utf-8")
    answers = []
    for _ in range(2):
        answers.append((await ctx.admin.post(
            "/api/admin/bundle/validate", json={"path": str(readme)}
        )).status_code)
    left = sorted(
        p.name for p in ctx.import_dir.iterdir()
        if p.name.startswith((".unpacked-README", ".unpacking-README"))
    )
    ok = answers == [400, 400] and not left and readme.is_file()
    return (
        ok,
        f"both attempts answered {answers}, and the import directory holds no README tree",
        f"left behind: {left}" if left else "",
    )


async def check_two(ctx: Install) -> tuple[bool, str, str]:
    """plan check 6: a truncated archive leaves nothing, and a good one of that name then validates.

    The 90% cut is the one that was measured on this archive: the first `validate` raised, and
    the SECOND returned ok=True over a 332-of-436 MB `reviews.sqlite`, after which decision 162
    and `artifact_bundle_one_seed` make that seed the household's only content import for ever.
    The second half of the check is the half with teeth -- a door that refuses and leaves a
    poisoned tree behind has fixed nothing -- so the good archive is put at the SAME path and
    validated from there.

    TWO cuts and a whole archive, because 0.9 can only reach one of the two truncation
    classes. On this archive 90% lands inside `reviews.sqlite` -- the last member, 416 MB,
    starting past 58% -- so the read fails mid-DATA and `tarfile` raises, which is the class
    that was always refused. The other class lands ON a 512-byte member header, where
    `tarfile` swallows `EOFHeaderError`, `TruncatedHeaderError` and `EmptyHeaderError` at
    every offset but 0 and returns from `next()` as if the archive had ended: `extractall`
    returned NORMALLY, `os.replace` promoted a partial tree, and the retry reused it because
    `BUNDLE.json` is the first member in the tar and therefore always survives. Members are
    512-aligned by construction, which is exactly what a block-aligned short write produces
    -- `dd`, `head -c`, ENOSPC on whole blocks, an interrupted `rsync --partial`.
    [M4.14 cycle 1, m414-c1-dim-refusals-01]
    """
    transfer = ctx.import_dir / TRANSFER_NAME
    cut = _truncated_copy(ctx.archive, transfer, TRUNCATE_FRACTION)
    refused = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(transfer)})
    kind = refused.headers.get("content-type", "")
    body = refused.json() if kind.startswith("application/json") else {}
    reported = bool(body.get("report")) and not body["report"].get("ok", True)
    left = sorted(
        p.name for p in ctx.import_dir.iterdir()
        if p.name.startswith((".unpacked-" + TRANSFER_NAME, ".unpacking-" + TRANSFER_NAME))
    )
    # The same name, now carrying the whole archive. A hard link rather than a copy: the bytes
    # are already on this disk and what the check is about is the NAME the operator retries.
    transfer.unlink()
    os.link(ctx.archive, transfer)
    began = time.perf_counter()
    good = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(transfer)})
    took = time.perf_counter() - began
    retried = good.status_code == 200 and good.json()["report"].get("ok") is True
    detail = "" if retried else f"the retry answered {good.status_code}: {console(good.text[:200])}"
    # The 1.04 GB second extraction has done its job; the checks below read this directory.
    shutil.rmtree(ctx.import_dir / (".unpacked-" + TRANSFER_NAME), ignore_errors=True)

    # The third cut: on the last member's header block, which is where the class that extracted
    # silently lives. The offsets are read with seeks rather than by reading the archive through.
    with tarfile.open(ctx.archive) as tar:
        boundary = [member.offset for member in tar][-1]
    transfer.unlink()
    aligned_bytes = _truncated_copy(ctx.archive, transfer, 0.0, keep=boundary)
    aligned = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(transfer)})
    aligned_kind = aligned.headers.get("content-type", "")
    aligned_body = aligned.json() if aligned_kind.startswith("application/json") else {}
    aligned_reported = (
        bool(aligned_body.get("report")) and not aligned_body["report"].get("ok", True)
    )
    aligned_left = sorted(
        p.name for p in ctx.import_dir.iterdir()
        if p.name.startswith((".unpacked-" + TRANSFER_NAME, ".unpacking-" + TRANSFER_NAME))
    )
    shutil.rmtree(ctx.import_dir / (".unpacked-" + TRANSFER_NAME), ignore_errors=True)
    transfer.unlink(missing_ok=True)

    aligned_ok = (aligned.status_code == 400 or aligned_reported) and not aligned_left
    ok = (refused.status_code == 400 or reported) and not left and retried and aligned_ok
    if not detail and not aligned_ok:
        detail = (
            f"the header-aligned cut answered {aligned.status_code} and left "
            f"{aligned_left or 'nothing'}: {console(aligned.text[:200])}"
        )
    return (
        ok,
        f"the {cut:,}-byte truncation answered {refused.status_code} and left {left or 'nothing'}; "
        f"the whole archive at the same path validated in {took:.1f}s; the {aligned_bytes:,}-byte "
        f"cut on a member header answered {aligned.status_code} and left "
        f"{aligned_left or 'nothing'}",
        detail,
    )


async def check_three(ctx: Install) -> tuple[bool, str, str]:
    """plan check 8: a bundle under /data/artifacts/<v>/ is refused before anything is deleted.

    Two doors and both are asserted, because they are for two readers. The route's is the
    sentence the operator can act on (it names where a bundle DOES belong); the importer's is the
    one that guards the `rmtree`, and `validate_for_install` is where the Data tab reads it. What
    the check proves is the same either way: `content.sqlite` and `BUNDLE.json` are still there.
    Reproduced before M4.14 as a validated import that deleted its own gigabyte and then raised
    `FileNotFoundError` as a 500.
    """
    staged = ctx.artifacts_dir / ctx.version
    _link_tree(ctx.extracted, staged)
    try:
        answer = await ctx.admin.post("/api/admin/bundle/import", json={"path": str(staged)})
        report = await bundle_import.validate_for_install(
            ctx.conn, bundle_import.Bundle.open(staged), ctx.artifacts_dir
        )
        stated = [
            f for f in report.as_dict()["findings"]
            if f["severity"] == "fail" and "stages into" in f["message"]
        ]
        survived = (staged / "content.sqlite").is_file() and (staged / "BUNDLE.json").is_file()
        ok = answer.status_code == 400 and bool(stated) and survived
        return (
            ok,
            f"the route answered {answer.status_code} and the report carried "
            f"{len(stated)} staging refusal(s); content.sqlite and BUNDLE.json survived: {survived}",
            console(str(answer.json().get("detail"))[:200]),
        )
    finally:
        shutil.rmtree(staged, ignore_errors=True)


async def check_four(ctx: Install) -> tuple[bool, str, str]:
    """plan check 9: a seed with DNA rows and no dna_vocab/ is a validation failure.

    `dna_tag` and `dna_projected` both carry a foreign key to `dna_vocabulary(version)`, so a
    bundle with rows and no vocabulary to create that row from used to validate 200 and die
    mid-transaction on a raw foreign-key violation -- an HTTP 500 where §10 promises a page.
    """
    probe = _link_tree(ctx.extracted, ctx.probes / "no-vocabulary")
    shutil.rmtree(probe / "artifacts" / "dna_vocab")
    _reinventory(probe)
    before = _tree(ctx.artifacts_dir)
    answer = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(probe)})
    report = answer.json()["report"]
    named = [f for f in _findings(report, "vocabulary") if "dna_vocab" in f["message"]]
    rows = await ctx.conn.fetchval("SELECT count(*) FROM artifact_bundle")
    ok = (
        answer.status_code == 200 and report["ok"] is False and bool(named)
        and rows == 0 and _tree(ctx.artifacts_dir) == before
    )
    return (
        ok,
        f"validate answered {answer.status_code} with ok={report['ok']}, "
        f"{len(named)} vocabulary refusal(s), {rows} bundle row(s) written",
        console(named[0]["message"][:200]) if named else _first_failure(report),
    )


async def check_five(ctx: Install) -> tuple[bool, str, str]:
    """plan check 10: one injected orphan names the table, the column, the count and the first ids.

    Every one of these violations used to reach Postgres as an exception inside the single
    transaction that carries the whole seed, and §10's report -- the one thing the operator is
    standing in front of -- could not name a single offending row. The orphans are planted in
    `title_genre`, whose `title_id` is one of the twenty foreign keys `_validate_integrity`
    mirrors, at ids far above the corpus's own maximum and far below decision 162's mint floor,
    so what fails is the referential rule and not the id partition.
    """
    probe = _link_tree(ctx.extracted, ctx.probes / "orphan")
    _detach(probe / "content.sqlite")
    planted = [999001, 999002, 999003]
    db = sqlite3.connect(probe / "content.sqlite")
    try:
        db.executemany(
            "INSERT INTO title_genre (title_id, source, genre, position) VALUES (?, ?, ?, ?)",
            [(i, "jellyfin", "Drama", 0) for i in planted],
        )
        db.commit()
    finally:
        db.close()
    _reinventory(probe)
    before = _tree(ctx.artifacts_dir)
    answer = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(probe)})
    report = answer.json()["report"]
    orphans = _findings(report, "integrity-orphan")
    named = [
        f for f in orphans
        if f["detail"].get("table") == "title_genre"
        and f["detail"].get("column") == "title_id"
        and int(f["detail"].get("rows") or 0) >= len(planted)
        and [int(i) for i in f["detail"].get("ids") or []][:3] == planted
    ]
    ok = (
        answer.status_code == 200 and report["ok"] is False and bool(named)
        and _tree(ctx.artifacts_dir) == before
    )
    return (
        ok,
        f"validate answered {answer.status_code} with ok={report['ok']} and "
        f"{len(orphans)} orphan finding(s), {len(named)} of them naming table, column and ids",
        console(orphans[0]["message"][:200]) if orphans else _first_failure(report),
    )


async def check_six(ctx: Install) -> tuple[bool, str, str]:
    """plan check 11: an unsafe version token refuses with ONE version on the screen.

    `Bundle.open` reduced an unusable token to 'unknown' while `_read_bundle_identity` wrote the
    raw string into the report, so `/validate` answered 200 with `report.bundle_version` of
    'v2026/08' beside a top-level `bundle_version: unknown` -- two versions of one bundle, on one
    screen -- and `import_bundle` refused after the operator had committed. The version is both a
    path segment under /data/artifacts and an `rmtree` target, which is why the token rule exists
    at all.
    """
    probe = _link_tree(ctx.extracted, ctx.probes / "unsafe-version")
    manifest = json.loads((probe / "BUNDLE.json").read_text(encoding="utf-8"))
    manifest["bundle_version"] = "v2026/08"
    (probe / "BUNDLE.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    answer = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(probe)})
    body = answer.json()
    report = body["report"]
    refusals = [f for f in _findings(report, "bundle-identity") if "v2026/08" in f["message"]]
    agree = body["bundle_version"] == report["bundle_version"]
    ok = answer.status_code == 200 and report["ok"] is False and bool(refusals) and agree
    return (
        ok,
        f"ok={report['ok']}, top-level bundle_version {body['bundle_version']!r} and the "
        f"report's {report['bundle_version']!r} agree: {agree}",
        console(refusals[0]["message"][:200]) if refusals else _first_failure(report),
    )


async def check_seven(ctx: Install) -> tuple[bool, str, str]:
    """plan check 5: every file BUNDLE.json lists is sha256-verified before a single row is written.

    The corpus shipped the evidence and nothing read it: `files` is a 42-entry map of
    {bytes, sha256}, `validations` is 68 rows with an `ok` flag, `total_bytes` is 1,042,461,726 --
    and only `bundle_version`, `vocabulary_version` and `tables.title` were ever looked at.

    Two readings, because the criterion has two halves. The isolated pass is the COST -- the
    number that decides whether an integrity check survives contact with an operator -- and the
    route's report is the PLACE: the same verification, inside §10's step 1, with the bundle row
    count and the artifacts tree read immediately afterwards to show that nothing was written.
    """
    began = time.perf_counter()
    isolated = ImportReport(bundle_version=ctx.version)
    validator._verify_bundle_files(ctx.extracted, isolated)
    cost = time.perf_counter() - began
    verified = next(
        (int(f["detail"].get("files") or 0) for f in isolated.as_dict()["findings"]
         if f["rule"] == "bundle-integrity" and f["severity"] == "note"),
        0,
    )
    began = time.perf_counter()
    answer = await ctx.admin.post("/api/admin/bundle/validate", json={"path": str(ctx.archive)})
    whole = time.perf_counter() - began
    report = answer.json()["report"]
    through_the_route = [
        f for f in report.get("findings", [])
        if f["rule"] == "bundle-integrity" and f["severity"] == "note"
    ]
    rows = await ctx.conn.fetchval("SELECT count(*) FROM artifact_bundle")
    staged = _tree(ctx.artifacts_dir)
    ok = (
        verified == ctx.listed_files and verified > 0 and cost < INTEGRITY_BUDGET_S
        and bool(through_the_route) and report["ok"] is True and rows == 0 and not staged
    )
    return (
        ok,
        f"{verified}/{ctx.listed_files} files verified in {cost:.2f}s; the route's whole "
        f"validation took {whole:.2f}s and wrote {rows} bundle row(s) and {len(staged)} file(s)",
        "" if ok else _first_failure(report),
    )


async def check_eight(ctx: Install) -> tuple[bool, str, str]:
    """plan check 1: POST /api/admin/bundle/import answers in under 5 s. Measured before: 127 s.

    THIS CHECK DRIVES THE IMPORT, and checks 9 to 12 read what it records. That is deliberate:
    there is one import in this run, and a phase sequence, a health series and a concurrent
    refusal are four readings of it rather than four imports. A failure here therefore leaves
    those four with nothing to read, and they say so as a precondition rather than inventing a
    verdict.

    The sequence is the operator's: press Import, watch the page, give up at five seconds, and
    come back later. `/api/health` is sampled throughout from an unauthenticated client, the
    worker is started only after the queued phase has been read (so the sequence below is a
    measurement and not a race), and a second admin fires the rival import the moment the phase
    reads `running`.
    """
    probe = ctx.session(timeout=30.0)
    sampler = asyncio.create_task(_sample_health(ctx, probe))
    began = time.perf_counter()
    ctx.press_at = began
    try:
        answer = await ctx.operator.post(
            "/api/admin/bundle/import", json={"path": str(ctx.archive)}
        )
        ctx.post_seconds = time.perf_counter() - began
        ctx.accepted_at = time.perf_counter()
        ctx.post_status = answer.status_code
        ctx.post_payload = answer.json()
        if answer.status_code != 202:
            raise RuntimeError(f"the import was not accepted: {console(answer.text[:300])}")

        # The phase before any worker has looked at the row. Read on the watching session, not on
        # the one that is about to give up.
        queued = (await ctx.bundle_state())["import_job"] or {}
        ctx.phases.append(str(queued.get("phase")))

        # §2's proxy cuts a request with no origin response at 100 s; this is the operator doing
        # the same thing by hand, five seconds in.
        await asyncio.sleep(max(0.0, DISCONNECT_AFTER_S - (time.perf_counter() - began)))
        await ctx.operator.aclose()
        ctx.disconnected_at = time.perf_counter() - began

        ctx.start_worker()
        deadline = time.monotonic() + IMPORT_DEADLINE_S
        while time.monotonic() < deadline:
            state = await ctx.bundle_state()
            job = state.get("import_job") or {}
            phase = str(job.get("phase"))
            if not ctx.phases or ctx.phases[-1] != phase:
                ctx.phases.append(phase)
            if phase == "running" and ctx.rival_status is None:
                rival = await ctx.rival.post(
                    "/api/admin/bundle/import", json={"path": str(ctx.archive)}
                )
                ctx.rival_status = rival.status_code
                ctx.rival_detail = str(rival.json().get("detail"))
            if phase not in ("queued", "running"):
                ctx.final_state = state
                ctx.import_seconds = time.perf_counter() - began
                break
            if ctx.worker is not None and ctx.worker.poll() is not None:
                raise RuntimeError(
                    f"the worker process exited with {ctx.worker.returncode} while the import "
                    f"was {phase}; its log ends:\n{console(ctx.worker_tail())}"
                )
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError(
                f"the import was still {ctx.phases[-1]} after {IMPORT_DEADLINE_S:g}s; the "
                f"worker log ends:\n{console(ctx.worker_tail())}"
            )
    finally:
        ctx.health_stop.set()
        with contextlib.suppress(Exception):
            await sampler
        ctx.staged_entries = sorted(p.name for p in ctx.artifacts_dir.iterdir())

    ok = ctx.post_status == 202 and ctx.post_seconds < POST_BUDGET_S
    return (
        ok,
        f"the route answered 202 in {ctx.post_seconds:.2f}s (budget {POST_BUDGET_S:g}s) and the "
        f"import finished {ctx.import_seconds:.0f}s after the press",
        f"job {ctx.post_payload.get('job_id')}, poll {ctx.post_payload.get('poll')}",
    )


async def check_nine(ctx: Install) -> tuple[bool, str, str]:
    """plan check 2: /api/health answers every second throughout the import, each under 5 s.

    This is the check the whole milestone is for. `/api/health` acquires a pooled connection on
    the backend's event loop, so while the import ran inside the request that loop was blocked
    and the probe did not answer -- against a HEALTHCHECK with a 5 s timeout. Docker called the
    backend unhealthy while it was importing perfectly well.

    THE GAP IS THE MEASUREMENT, not the latency. A blocked loop does not answer slowly; it does
    not answer at all, and the sampler that would have measured it is on the same loop. So a run
    that produced twenty samples over two minutes with a 40 ms worst case is the defect, not a
    pass -- which is why the count and the worst gap are both in the predicate.

    THE WINDOW STARTS AT THE PRESS AND NOT AT THE 202, because the operator's outage does not
    begin when the worker picks the job up. It used to end there too: `validate_for_install` ran
    the 1.04 GB sha256 pass and the Cold Tower's torch load synchronously on this loop, so the
    request that starts the import blocked it past `app._HEALTH_TIMEOUT_S` on its own and this
    check was the one red one of thirteen. Decision 287 runs that synchronous work in a worker
    thread, which is what closed it. The window still starts at the press, because that is the
    span the operator experiences and because a regression there would be invisible measured from
    the 202. Which half a bad sample fell in is part of the verdict's text rather than a
    footnote, because the two halves send a reader to two different processes.
    """
    if not ctx.health or ctx.accepted_at is None or ctx.press_at is None:
        raise PreconditionFailed("the import never ran, so /api/health was never sampled")
    stamps = [s for s, _, _ in ctx.health]
    latencies = [lat for _, lat, _ in ctx.health]
    statuses = {code for _, _, code in ctx.health}
    span = stamps[-1] - stamps[0]
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    worst_gap = max(gaps) if gaps else span
    # One sample a second across the window, less two for the edges: a sampler that stopped and
    # a sampler that never started look the same in a maximum.
    expected = max(1, int(span / HEALTH_INTERVAL_S) - 2)
    # Which half a bad sample fell in, because the two halves are two different claims. Before the
    # 202 the backend is running `validate_for_install` itself; after it, the work is the worker's
    # and the loop is supposed to be free. A verdict that did not say which one it failed in
    # would send a reader to the wrong process.
    refused_in_request = [c for s, _, c in ctx.health if s < ctx.accepted_at and c != 200]
    refused_in_import = [c for s, _, c in ctx.health if s >= ctx.accepted_at and c != 200]
    reasons = []
    if refused_in_request:
        reasons.append(
            f"{len(refused_in_request)} sample(s) answered {sorted(set(refused_in_request))} "
            "WHILE THE ROUTE WAS VALIDATING, before the 202: decision 287 runs that work in a "
            "worker thread, so this means the loop was blocked anyway and "
            "app._HEALTH_TIMEOUT_S is 2 s"
        )
    if refused_in_import:
        reasons.append(
            f"{len(refused_in_import)} sample(s) answered {sorted(set(refused_in_import))} while "
            "the worker held the import, which is the outage M4.14 exists to remove"
        )
    if max(latencies) >= HEALTH_BUDGET_S:
        reasons.append(f"the slowest answer took {max(latencies):.1f}s")
    if worst_gap >= HEALTH_BUDGET_S:
        reasons.append(f"{worst_gap:.1f}s passed with no sample at all: a blocked loop")
    if len(ctx.health) < expected:
        reasons.append(f"{len(ctx.health)} samples where at least {expected} were due")
    return (
        not reasons,
        f"{len(ctx.health)} samples over {span:.0f}s (at least {expected} expected), "
        f"worst answer {max(latencies) * 1000:.0f} ms, worst gap {worst_gap:.2f}s, "
        f"statuses {sorted(statuses)}; non-200: {len(refused_in_request)} in the "
        f"{ctx.accepted_at - ctx.press_at:.1f}s the route spent validating and "
        f"{len(refused_in_import)} in the import that followed",
        "\n".join(reasons),
    )


async def check_ten(ctx: Install) -> tuple[bool, str, str]:
    """plan check 3: the phase runs queued -> running -> active and ends with report.ok.

    The phases are `job_run.detail->>'phase'` and not `artifact_bundle` states, which is decision
    253: `artifact_bundle.kind` is NOT NULL DEFAULT 'seed' under a partial unique index, so a
    placeholder row written for a queued import claims the one and only seed slot and a failed
    attempt could never be retried.

    `restart_required` is read beside them because §10's swap sequence ends there: this process
    pinned an empty store at boot and the active row has just moved, so the page owes the
    operator a restart and must say so.
    """
    if ctx.final_state is None:
        raise PreconditionFailed("the import never reached a terminal phase")
    job = ctx.final_state.get("import_job") or {}
    report = job.get("report") or {}
    row = await ctx.conn.fetchrow(
        "SELECT version, state FROM artifact_bundle ORDER BY imported_at DESC LIMIT 1"
    )
    ok = (
        ctx.phases == ["queued", "running", "active"]
        and job.get("ok") is True and report.get("ok") is True
        and (row["version"], row["state"]) == (ctx.version, "active")
        and ctx.final_state.get("active") == ctx.version
        and ctx.final_state.get("restart_required") is True
    )
    return (
        ok,
        f"phases {ctx.phases}, job ok={job.get('ok')}, report ok={report.get('ok')}, "
        f"artifact_bundle {row['version']}/{row['state']}, "
        f"restart_required={ctx.final_state.get('restart_required')}",
        "" if ok else _first_failure(report),
    )


async def check_eleven(ctx: Install) -> tuple[bool, str, str]:
    """plan check 4: a client that gives up at 5 s changes nothing.

    §5.3 budgets this work in minutes and §2 puts the origin behind a proxy that cuts at 100 s,
    so the request ending before the work does is the EXPECTED shape rather than an edge case.
    The session that pressed Import is closed and gone; a NEW sign-in reads the outcome, because
    "the report of an import nobody was watching is still readable afterwards" is the property a
    202 plus a poll is for.
    """
    if ctx.disconnected_at is None or ctx.final_state is None:
        raise PreconditionFailed("the import never ran, so no client had anything to give up on")
    later = await ctx.login()
    state = (await later.get("/api/admin/bundle/state")).json()
    job = state.get("import_job") or {}
    report = job.get("report") or {}
    titles = await ctx.conn.fetchval("SELECT count(*) FROM title")
    ok = (
        ctx.disconnected_at >= DISCONNECT_AFTER_S
        and state.get("active") == ctx.version
        and job.get("phase") == "active" and report.get("ok") is True
        and titles > 0
    )
    return (
        ok,
        f"the client closed {ctx.disconnected_at:.1f}s after the press; a fresh session reads "
        f"active={state.get('active')} phase={job.get('phase')} over {titles:,} titles",
        "",
    )


async def check_twelve(ctx: Install) -> tuple[bool, str, str]:
    """plan check 12: two imports at once give one ok and one "already running", and stage once.

    Unserialised, two concurrent imports of one bundle both rmtree and copytree the same staging
    directory and both COPY the corpus's rows: measured as
    `UniqueViolationError: duplicate key value violates unique constraint "title_alias_pkey"`,
    and with two different versions as `artifact_bundle` holding one version while
    /data/artifacts held two. `bundle.IMPORT_LOCK` is one advisory lock over this loop, the
    route's check-and-enqueue and an ops script alike, and the rival request below is refused at
    the door by the same lock the running import holds.
    """
    if ctx.rival_status is None:
        raise PreconditionFailed("no second import was fired: the phase never read running")
    winner = (ctx.final_state or {}).get("import_job") or {}
    ok = (
        ctx.rival_status == 409
        and "already running" in ctx.rival_detail
        and winner.get("ok") is True
        and ctx.staged_entries == [ctx.version]
    )
    return (
        ok,
        f"the rival import answered {ctx.rival_status}, the first reported ok={winner.get('ok')}, "
        f"and /data/artifacts holds {ctx.staged_entries}",
        console(ctx.rival_detail[:200]),
    )


async def check_thirteen(ctx: Install) -> tuple[bool, str, str]:
    """plan check 13: after every refusal the install is unchanged, and nothing answered 500.

    Three refusals against the SEEDED install, which is the state the first twelve checks could
    not put them in: the path boundary that used to be a string prefix (so `<DATA_DIR>-other` was
    accepted), a plain file as the path, and the retry M4.5's close-out recorded -- an operator
    who was told the import failed while it completed, pressing Import again and meeting
    `seed-once`, which reads as corruption. Every one has to leave the active row and the staged
    tree exactly as they are.

    "No 500 in the log" is read off this run rather than off a file: every status every session
    in this script saw is recorded, and the worker's own log is read for a traceback, because the
    process that did the work is the one whose crash would not appear in any response at all.
    The clause is scoped to the bundle routes; see the comment where it is measured.
    """
    before_rows = [dict(r) for r in await ctx.conn.fetch(
        "SELECT version, state, imported_at, activated_at FROM artifact_bundle ORDER BY version"
    )]
    before_tree = _tree(ctx.artifacts_dir)

    outside = ctx.work.parent / (ctx.work.name + "-other")
    (outside / "bundle").mkdir(parents=True, exist_ok=True)
    try:
        boundary = await ctx.admin.post(
            "/api/admin/bundle/import", json={"path": str(outside / "bundle")}
        )
        plain = await ctx.admin.post(
            "/api/admin/bundle/import", json={"path": str(ctx.import_dir / "README.txt")}
        )
        again = await ctx.admin.post(
            "/api/admin/bundle/import", json={"path": str(ctx.archive)}
        )
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    seed_once = []
    if again.status_code == 422:
        seed_once = _findings(again.json()["detail"]["report"], "seed-once")

    after_rows = [dict(r) for r in await ctx.conn.fetch(
        "SELECT version, state, imported_at, activated_at FROM artifact_bundle ORDER BY version"
    )]
    unpacking = sorted(
        p.name for p in ctx.import_dir.iterdir() if p.name.startswith(".unpacking-")
    )
    partial = sorted(
        p.name for p in ctx.import_dir.iterdir()
        if p.name.startswith(".unpacked-") and not any(p.rglob("BUNDLE.json"))
    )
    # The bundle routes, and only those. The criterion's clause is about REFUSALS -- every one
    # of findings 2.5, 2.6 and 2.8 reached the operator as a 500 where section 10 promises a
    # page -- and the health probe's own statuses are check 9's subject, measured there against
    # the two halves of its window. Counting them here as well would put one fact behind two
    # verdicts and make a probe that timed out during a validation read as a destructive import.
    # They are reported below, so a reader can see there were some.
    served = sorted({c for _, path, c in ctx.responses if c >= 500 and 'bundle' in path})
    probed = sorted({c for _, path, c in ctx.responses if c >= 500 and 'health' in path})
    crashed = (
        "Traceback" in ctx.worker_log.read_text(encoding="utf-8", errors="replace")
        if ctx.worker_log.is_file() else False
    )
    ok = (
        boundary.status_code == 400 and plain.status_code == 400
        and again.status_code == 422 and bool(seed_once)
        and after_rows == before_rows and _tree(ctx.artifacts_dir) == before_tree
        and not unpacking and not partial and not served and not crashed
    )
    return (
        ok,
        f"the three refusals answered {boundary.status_code}, {plain.status_code}, "
        f"{again.status_code}; artifact_bundle held {len(before_rows)} row(s) before and after, "
        f"/data/artifacts {len(before_tree)} file(s) before and after",
        f"5xx on a bundle route: {served}; .unpacking-* left: {unpacking}; incomplete "
        f"extractions: {partial}; a traceback in the worker log: {crashed}; 5xx on "
        f"/api/health (check 9's subject, not this one): {probed}",
    )


RUNNERS: dict[int, Callable[[Install], Awaitable[tuple[bool, str, str]]]] = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five,
    6: check_six, 7: check_seven, 8: check_eight, 9: check_nine, 10: check_ten,
    11: check_eleven, 12: check_twelve, 13: check_thirteen,
}


async def measure(number: int, ctx: Install) -> None:
    """Run one check and report it, whatever it does.

    A check that raises fails THAT check and no other: a run that stops at six and prints
    "6/6 checks passed" is the failure mode an exit criterion exists to rule out. A precondition
    its predecessor did not produce gets a sentence rather than a traceback, because the sentence
    IS the diagnosis.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    try:
        ok, measured, detail = await RUNNERS[number](ctx)
    except PreconditionFailed as exc:
        check(False, label, f"PRECONDITION FAILED: {exc}")
        return
    except Exception as exc:
        check(
            False, label, f"the check stopped on {type(exc).__name__}: {exc}\n"
            + traceback.format_exc(),
        )
        return
    check(ok, label, "\n".join(part for part in (measured, detail) if part))


async def build_install(conn: asyncpg.Connection, corpus: Path, work: Path, dsn: str) -> Install:
    """The scratch install, the archive, and the one extraction every probe is cut from.

    The archive is built here rather than assumed, because building it is part of the criterion:
    the bundle on disk is a directory and the `.tar` path had never been exercised at any layer.
    It is then opened ONCE -- which is the extraction `_unpack` writes through `.unpacking-` and
    renames onto `.unpacked-` -- so that the seconds the later checks report are the seconds of
    the thing they are about and not of a gigabyte coming off the disk again.
    """
    for directory in (work / "import", work / "artifacts", work / "probes", work / "no-dot-env"):
        directory.mkdir(parents=True, exist_ok=True)

    archive = work / "import" / (corpus.name + ARCHIVE_SUFFIX)
    took, size = _build_archive(corpus, archive)
    print(f"  built {console(archive.name)}: {size:,} bytes in {took:.0f}s", flush=True)

    began = time.perf_counter()
    opened = bundle_import.Bundle.open(archive)
    print(
        f"  extracted and opened it in {time.perf_counter() - began:.0f}s: "
        f"version {console(opened.version)}, kind {opened.kind}, "
        f"vocabulary {console(str(opened.vocabulary_version))}",
        flush=True,
    )

    os.environ["DATABASE_URL"] = dsn
    os.environ["DATA_DIR"] = str(work)
    _neutralise_connector_env()
    os.chdir(work / "no-dot-env")
    core_config.settings.cache_clear()

    from spielplan.app import create_app

    install = Install(conn, create_app(), work, corpus)
    install.dsn = dsn
    install.archive = archive
    install.extracted = opened.root
    install.version = opened.version
    install.listed_files = len(
        json.loads((opened.root / "BUNDLE.json").read_text(encoding="utf-8")).get("files") or {}
    )
    return install


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL export bundle on purpose:")
        print("the fixture is megabytes written by this repository, so it can falsify neither the")
        print("5-second answer, nor the integrity pass, nor one refusal M4.14 exists to fix.")
        return 2
    root = Path(bundle_dir)
    if not (root / "BUNDLE.json").is_file() or not (root / "artifacts").is_dir():
        print(f"CORPUS_BUNDLE_DIR={console(str(root))} is not an export bundle: it must carry")
        print("BUNDLE.json and an artifacts/ directory. It refuses to run on the fixture on")
        print("purpose.")
        return 2
    inventoried = int(
        (json.loads((root / "BUNDLE.json").read_text(encoding="utf-8")).get("total_bytes")) or 0
    )
    if inventoried < MIN_REAL_BYTES:
        print(
            f"{console(str(root))} inventories {inventoried:,} bytes -- the fixture, not the "
            "corpus."
        )
        print("Refusing on purpose: a 5-second budget measured on megabytes measures nothing.")
        return 2
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print(
        f"\nM4.14 exit criterion -- bundle {console(root.name)} ({inventoried:,} bytes)\n",
        flush=True,
    )

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    scratch = f"spielplan_m414_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE -- the mkdtemp, the connect, the codec registration this project has been
    # bitten by once, the archive, the app -- happens inside the `try`, because the scratch name
    # carries this run's pid: pid-suffixing it (which is what stops two concurrent runs dropping
    # each other's database) removed the accidental second chance the next run's `DROP DATABASE
    # IF EXISTS` used to be. An orphan nothing will ever name again is a leak on the household's
    # own server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    ctx: Install | None = None
    lifespan: Any = None
    try:
        work = Path(tempfile.mkdtemp(prefix="spielplan-m414-exit-"))
        conn = await asyncpg.connect(scratch_dsn)
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. Without it `title_meta.payload` fails with "expected str, got dict" --
        # which M4.5's harness found, and which was a defect in the script rather than in the
        # importer.
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The archive, the extraction and the install this run measures", flush=True)
        ctx = await build_install(conn, root, work, scratch_dsn)
        lifespan = ctx.app.router.lifespan_context(ctx.app)
        await lifespan.__aenter__()
        ctx.admin = ctx.session()
        created = await ctx.admin.post(
            "/api/setup/admin", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD}
        )
        if created.status_code != 201:
            raise RuntimeError(f"no admin account: {console(created.text[:300])}")
        ctx.operator = await ctx.login()
        ctx.rival = await ctx.login()
        print(f"  {ctx.listed_files} files inventoried; the install has no bundle yet", flush=True)

        print("\n1. A plain file as the path (plan check 7)", flush=True)
        await measure(1, ctx)
        print("\n2. A truncated archive, then a good one of that name (plan check 6)", flush=True)
        await measure(2, ctx)
        print("\n3. A bundle under the artifacts root (plan check 8)", flush=True)
        await measure(3, ctx)
        print("\n4. A seed with DNA rows and no vocabulary (plan check 9)", flush=True)
        await measure(4, ctx)
        print("\n5. One injected orphan row (plan check 10)", flush=True)
        await measure(5, ctx)
        print("\n6. An unsafe version token (plan check 11)", flush=True)
        await measure(6, ctx)
        print("\n7. The integrity pass, before a row is written (plan check 5)", flush=True)
        await measure(7, ctx)
        print("\n8. The import, off the request path (plan check 1)", flush=True)
        await measure(8, ctx)
        print("\n9. The health probe, throughout (plan check 2)", flush=True)
        await measure(9, ctx)
        print("\n10. The phase, polled to completion (plan check 3)", flush=True)
        await measure(10, ctx)
        print("\n11. The client that gave up (plan check 4)", flush=True)
        await measure(11, ctx)
        print("\n12. Two imports at once (plan check 12)", flush=True)
        await measure(12, ctx)
        print("\n13. The install after every refusal (plan check 13)", flush=True)
        await measure(13, ctx)
    except Exception as exc:
        # Everything outside a check: the connect, the migration, the archive, the app's boot.
        # Reported as the failures they are, so the exit code stays non-zero and the run still
        # ends in a score rather than in a traceback where the sentence naming the cause belongs.
        # The handler catches everything on purpose: narrowing it to `asyncpg.PostgresError` --
        # the narrowing a reviewer proposes for an async DB harness -- would leave the guard green
        # while a `tarfile.ReadError` or an `OSError` out of the archive reverted this run to the
        # bare traceback that loses every section below it and the tally.
        # [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        for index, (number, title) in enumerate(CHECKS[len(results):]):
            check(False, f"{number}. {title}", stopped + ("\n" + trace if index == 0 else ""))
    finally:
        if ctx is not None:
            ctx.stop_worker()
            for session in ctx.sessions:
                with contextlib.suppress(Exception):
                    await session.aclose()
        if lifespan is not None:
            with contextlib.suppress(Exception):
                await lifespan.__aexit__(None, None, None)
        if conn is not None:
            await conn.close()
        os.chdir(ROOT)
        discard_staged_artifacts(work, None)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed", flush=True)
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
