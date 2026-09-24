"""M5.1's exit criterion: the acquisition spine, measured on a real install.

§12's M5.1 row names this script, and the twelve checks below are `docs/milestones/M5.1-plan.md`
§7's own pass table, numbered as it numbers them. The row's sentence is what they add up to:

    with stages 2-8 declared no-ops, a task injected for a Jellyfin item carrying provider ids
    walks stage 1 to 9 to 10: it mints a title above 1e9 with `origin = 'acquired'`, is placed
    by the Cold Tower and appears on Home carrying the "new - model placement, no crowd data"
    badge. A worker killed mid-lease has its task reclaimed by the next worker and completed
    exactly once, with no duplicated derived row; one URL fetched twice writes one file under
    `/data/raw` and two `raw_document` rows, and the second parse issues no request; an item
    with no provider id parks at stage 1 with that reason and mints nothing; and `/data/raw` is
    readable by the worker and absent from the backend container.

      script  plan  what it measures
        1       1   an injected task walks the ten stages to stage 10, status 'ready'
        2       2   the minted id is at or above 1e9 and the row says origin = 'acquired'
        3       3   a title_placement row exists, stamped with the version that is active
        4       4   the title is on Home's "New in the library" shelf carrying the badge
        5       5   a worker killed mid-lease: reclaimed, completed once, nothing duplicated
        6       6   the same url twice: one file under /data/raw, two raw_document rows
        7       7   a re-parse issues no request, on the fetcher's own counter
        8       8   an item with no provider id parks at stage 1 and mints nothing
        9       9   /data/raw is absent from the backend container and present in the worker
       10      10   placement with no active bundle parks with that reason and logs no fault
       11      11   POST /events/nothing is 404 -- not 405, and not the SPA shell
       12      12   after every refusal: acquisition_job and title unchanged, no fault logged

THE ORDER IS THE PLAN'S AND IS ALSO THE ONLY ORDER THAT MEASURES ANYTHING. Checks 1 to 7 run
against an install whose bundle is active, because the criterion's first clause is a walk that
ENDS in a Cold Tower placement; check 12 then compares the install to a snapshot taken after
them, so "unchanged" has something to be unchanged against. Check 10 is the only one that has
to move install state -- §3.1's bundle-less household cannot be reached by asking politely --
so it supersedes the active row, measures the park, and restores it inside a `finally`, which
is `ops/m413_exit_criterion.py`'s own manoeuvre at `:557-560`. Check 12 asserts the restore.

TWO OF THE TWELVE CANNOT BE MEASURED WITHOUT A RUNNING STACK, and they say so rather than
passing. Check 9 needs two containers and check 11 needs an app answering on a port, and neither
exists in a lane with no Docker and no server. Decision 184 is the rule: a published figure is
one a run produced, so those two report "not measured here" with the reason, are counted in
neither the numerator nor the "failed" list, and make the process exit 3 -- an incomplete
measurement is not a pass and is not a failure, and a script whose only two codes are those has
to lie in one direction. Give them what they need and they measure:

    SPIELPLAN_EXIT_BASE_URL=http://localhost:8080     check 11 probes this origin
    docker on PATH, `docker compose ps` showing backend and worker running   check 9 runs
    SPIELPLAN_EXIT_COMPOSE_DIR=/srv/spielplan         where that compose project lives
    SPIELPLAN_EXIT_NO_DOCKER=1                        skip check 9 outright

IT REFUSES TO RUN ON THE FIXTURE, for the reason `ops/m45_exit_criterion.py` and
`ops/m412_exit_criterion.py` do, and the reason is specific to what M5.1 claims. Check 1 begins
by asserting that `connectors/resolve.resolve_title_id` does NOT find the injected item, which
is what makes the mint that follows a mint rather than a lookup -- and the resolver's last
branch is a name-and-year match. Against the fixture's handful of uniquely-named titles that
assertion is free; against the 19,000 the corpus ships, where 2,438 share `(kind, lower(name))`
and 573 groups still collide with the year applied (`connectors/resolve.py:189-194`), it is the
only population on which decision 323's refusal to mint on a name means anything. Checks 3 and 4
need a real basis for the same kind of reason: a minted title carries no DNA row at all, and
whether the Cold Tower produces a coordinate for it is a question about the tower the corpus
trained, not about a fixture's stand-in.

THE FETCHER IS DRIVEN AGAINST A LOCAL DOUBLE AND THIS SCRIPT SENDS NOTHING TO A THIRD PARTY.
Checks 6 and 7 are claims about the raw store's own arithmetic -- identical bytes resolve to a
path that already exists, and a read of that path issues no request -- and the way to falsify
them is a transport whose every call this process counts. `httpx.MockTransport` over a host in
the reserved `.invalid` TLD is that transport; nothing leaves the box, and the plan says so in
its own §8 ("M5.1's tests drive the fetcher against a local double"). The one probe that does
leave the process is check 11's, and it goes to an origin the operator named.

STAGES 2-8 ARE DECLARED NO-OPS IN THIS RUN, BECAUSE THE CRITERION IS STATED WITH THEM SO. The row
reads "with stages 2-8 declared no-ops", and they were when this script was written. Since then
M5.3 gave stages 2, 3 and 4 bodies and M5.5 gave stage 6 one, and this script went on draining
the shipped pipeline with the real default fetcher: stage 2 asked Wikidata, Wikipedia, TVmaze,
Rotten Tomatoes and Metacritic about a film nobody made, from the household's own address, and
stage 4 then parked it for want of reviews -- so check 1 could not reach stage 10 on any real
bundle, checks 3, 4, 5 and 10 inherited the park, and the paragraph above was false. Stage 6
stood behind that, parked by decision 348's no-cap refusal. `build_install` now stands every
stage from 2 to 8 down as a declared no-op, `implemented=False` -- the one shape decision 348
says cannot spend, and the pipeline the checks measure the spine through -- and replaces the
drain's default fetcher with one that refuses, so a stage that still asked for it would fail a
check rather than open a socket. The test suite does the same for its walk-to-ready tests
(`backend/tests/test_acquire_pipeline.py`'s `enrichment_stands_down`). The stages this stands
down are measured by their own milestones' instruments. [M5.5 review cycle 1, NBR-03, M55-DOC-06]

It connects through `db/pool._init_connection` and never a bare `asyncpg.connect`: without the
json/jsonb codec `title_meta.payload` dies with "expected str, got dict", which is the defect
M4.5's harness spent three runs believing was in the importer (`M4.14-plan.md:911-912`).

It creates and drops its own DATABASE, so it never runs against a household's data by accident
-- 0003 creates schemas of its own, so a search_path would not have isolated it -- and it stages
into a temporary DATA_DIR it removes. Output is ASCII: a Windows console crashes on a decorative
glyph, and every string this script did not author goes through `console()` on the way out --
the park reasons `acquire/stages.py` writes for an operator and Home's own why-line both carry
typographic dashes, and both reach the console here.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m51_exit_criterion.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s reporting helpers can be imported rather than
# copied, which is what `ops/m414_exit_criterion.py` does and why: M4.5's recorded 18/18 has to
# stay reproducible unchanged, so that file is read and never edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. `setdefault` would read
# a developer's `.env` *after* this and pydantic-settings ranks the environment above that file,
# so these two are set outright: this script must never seal a household's real SECRETS_KEY into
# a scratch database, and a throwaway key is all the encryption here means.
os.environ["SESSION_SECRET"] = "m51-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m51-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan.acquire import fetch, pipeline, queue, rawstore, stages  # noqa: E402
from spielplan.connectors import registry, resolve  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.home import shelves  # noqa: E402
from spielplan.home import why as why_mod  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "an injected task walks the ten stages and the board reads stage 10, ready"),
    (2, "the minted title is at or above 1e9 and carries origin 'acquired'"),
    (3, "a title_placement row exists, stamped with the version that is active"),
    (4, "the minted title is on Home's New in the library shelf, carrying the badge"),
    (5, "a worker killed mid-lease is reclaimed, completed once, and duplicates nothing"),
    (6, "the same url fetched twice leaves one file under /data/raw and two rows"),
    (7, "a re-parse issues no request, measured on the fetcher's own counter"),
    (8, "an item with no provider id parks at stage 1 and mints nothing"),
    (9, "/data/raw is absent from the backend container and present in the worker"),
    (10, "placement with no active bundle parks with that reason and logs no fault"),
    (11, "POST /events/nothing is 404 - not 405, and not the SPA shell"),
    (12, "after every refusal, acquisition_job and title are unchanged and nothing faulted"),
)

# §4.1's partition, as `acquire/stages.py` restates it from `0015_seed.sql:20-27`. Named here
# rather than imported so a reader of check 2 can see the number the verdict is against; the
# check itself compares against `stages.APP_ID_MIN`, so the two cannot drift apart silently.
APP_ID_FLOOR = 1_000_000_000

# The floor under which CORPUS_BUNDLE_DIR is the fixture and not the corpus. Read off
# BUNDLE.json's own `total_bytes` rather than off the directory, because a bundle that has been
# half-copied is exactly what this script must be able to tell apart from one that is small by
# design. `ops/m414_exit_criterion.py` uses the same number against the same file.
MIN_REAL_BYTES = 500_000_000

# How long check 5 waits for the doomed worker to say it has the lease. Generous, because the
# child pays torch's and numpy's import cost through `spielplan.acquire.stages` before it can
# lease anything, and a deadline that fires is a run with no measurement rather than a failed
# measurement.
CHILD_DEADLINE_S = 180.0

# Check 9's two commands are `ls` inside a container and answer in milliseconds; a minute is two
# orders of magnitude of headroom and still bounds a Docker daemon that has stopped answering.
DOCKER_TIMEOUT_S = 60.0

# The items this run injects. Every one of them is deliberately unresolvable against the corpus:
# the names are sentences no distributor has released, the years are past every ProductionYear
# v20260828 carries, and the provider ids sit far above any real imdb or tmdb id. That is not
# belt and braces -- check 1 asserts the resolver returns None BEFORE enqueueing, and an item
# that resolved would turn "stage 1 mints" into "stage 1 found the row the bundle shipped",
# which is a different measurement wearing the same verdict.
ITEM_ONE = {
    "Id": "jf-m51-exit-a", "Name": "An Exit Criterion Acquisition", "Type": "Movie",
    "ProductionYear": 2087, "RunTimeTicks": 101 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt91000001", "Tmdb": "91000001"},
}
ITEM_TWO = {
    "Id": "jf-m51-exit-b", "Name": "A Second Exit Criterion Acquisition", "Type": "Movie",
    "ProductionYear": 2088, "RunTimeTicks": 102 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt91000002"},
}
# `ops/fake_jellyfin.py:52-53`'s awkward fixture in this run's own namespace: an item Jellyfin
# gives no provider id for. Decision 323 parks it at stage 1 and mints nothing.
ITEM_NO_IDS = {
    "Id": "jf-m51-exit-c", "Name": "A Third Exit Criterion Acquisition", "Type": "Movie",
    "ProductionYear": 2089, "RunTimeTicks": 103 * 60 * 10_000_000, "ProviderIds": {},
}
ITEM_BUNDLELESS = {
    "Id": "jf-m51-exit-d", "Name": "A Fourth Exit Criterion Acquisition", "Type": "Movie",
    "ProductionYear": 2090, "RunTimeTicks": 104 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt91000004"},
}

# Checks 6 and 7's double. `.invalid` is reserved by RFC 2606 and resolves nowhere, so a run in
# which `MockTransport` were somehow bypassed fails with a DNS error rather than quietly reaching
# a real host -- which is the property that lets this script be run on a household's own box.
DOC_URL = "https://exit-criterion.invalid/title/9001.json"
SECOND_URL = "https://exit-criterion.invalid/title/9002.json"
DOC_BYTES = b'{"id": 9001, "title": "an exit criterion document", "reviews": []}'
SECOND_BYTES = b'{"id": 9002, "title": "a second exit criterion document", "reviews": []}'
ROBOTS_BODY = "User-agent: *\nDisallow:\n"

# The source the raw store files these two under. A namespace of this run's own, so a store that
# survived cleanup is identifiable on sight and is never mistaken for something a real adapter
# wrote -- the eight of those are M5.3's.
DOC_SOURCE = "m51-exit-criterion"


# The worker that dies holding a lease. Written into the scratch tree and run as a child of this
# script, so its traceback has a file to name -- `ops/m414_exit_criterion.py`'s `_WORKER_PUMP`
# established the shape.
#
# IT STOPS BETWEEN THE MINT AND EVERY RECORD OF IT, which is the worst window there is and
# therefore the one worth measuring. `pipeline.run_task` writes the title id onto the task
# (`_remember_title`) and then writes the board row; a process killed before either leaves a
# `title` row committed with nothing in the queue or on the board pointing at it, so the
# reclaimed run arrives knowing only the Jellyfin item. If stage 1 were not idempotent that run
# would mint a SECOND row for one film, into a spine decision 162 makes permanent. It is
# idempotent because `_mint` writes `jellyfin_id` and `resolve.resolve_title_id` tries that
# first, and this check is what makes that sentence falsifiable.
#
# It reaches `stages.identify` directly rather than through `run_task`, because the driver
# offers no seam that stops after stage 1: the private call is the only way to put the process
# in the state the criterion names, and it is the same call `run_task`'s first iteration makes.
_DOOMED_WORKER = '''"""One worker that leases an acquisition task, mints its title, and is
then killed holding the lease.

Written and run by ops/m51_exit_criterion.py; see the `_DOOMED_WORKER` comment there for why it
stops where it stops. Nothing outside that script should run this file.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ["SPIELPLAN_BACKEND"])

import asyncpg

from spielplan.acquire import pipeline, queue, stages
from spielplan.db import pool


async def main():
    conn = await asyncpg.connect(os.environ["SPIELPLAN_SCRATCH_DSN"])
    await pool._init_connection(conn)
    leased = await queue.lease(conn, [pipeline.TASK_KIND], limit=1)
    if not leased:
        print("doomed: nothing to lease", flush=True)
        return
    task = leased[0]
    outcome = await stages.identify(stages.StageContext(conn=conn, task=task))
    print(f"doomed: task {task.id} key {task.key} title {outcome.title_id}", flush=True)
    # Nothing is recorded after this line, on purpose. The parent kills the process here.
    while True:
        await asyncio.sleep(3600)


asyncio.run(main())
'''


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


class LogWatch(logging.Handler):
    """Every WARNING-or-worse this app's own code emits during the run, kept for 10 and 12.

    The criterion's phrase is "no 500 in the log", and on an install driven through routes that
    is literally what it means. This harness drives the domain packages, so the honest analogue
    has to be named rather than assumed: a 500 is a request that ended in an unhandled
    exception, and the one place this pipeline turns an exception into state is
    `pipeline._run_stage`, which logs it with `log.exception` and returns `fail`. So a fault
    here is a record carrying `exc_info` or at ERROR or worse, and a probe answering 5xx is the
    other half -- check 11's request is the only one this run makes to an app at all.

    Attached to the `spielplan` logger rather than to the root, so a warning from asyncio or
    from asyncpg's own machinery is not read as the app faulting.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def faults(records: list[logging.LogRecord]) -> list[str]:
    """The records that say something broke, as lines. Warnings are reported and are not faults."""
    return [
        f"{record.name} {record.levelname}: {record.getMessage()}"
        for record in records
        if record.exc_info is not None or record.levelno >= logging.ERROR
    ]


def warnings_of(records: list[logging.LogRecord]) -> list[str]:
    return [
        f"{record.name} {record.levelname}: {record.getMessage()}"
        for record in records
        if record.exc_info is None and record.levelno < logging.ERROR
    ]


@dataclass
class Install:
    """The one install every check below measures, and what the checks hand each other."""

    conn: asyncpg.Connection
    work: Path
    artifacts_root: Path
    bundle_root: Path
    log: LogWatch
    version: str = ""
    vocabulary: str | None = None
    user_id: int = 0
    # Stage 1's output, per injected item, so check 2 measures the row check 1 produced rather
    # than whatever `max(id)` happens to be by the time it runs.
    minted: dict[str, int] = field(default_factory=dict)
    documents: list[int] = field(default_factory=list)
    # Every request that left this process, recorded by the transport itself, and every HTTP
    # status this run saw. Check 12 reads the second for its "no 500" half.
    requests: list[str] = field(default_factory=list)
    statuses: list[tuple[str, int]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


# --- reporting ----------------------------------------------------------------------------------


UNMEASURED: list[tuple[str, str]] = []


def not_measured(number: int, reason: str) -> tuple[None, str]:
    """Record a check this run could not measure. Not a pass, not a failure, and never silent.

    Decision 184's rule applied to a harness: a published figure is one a run produced. Checks 9
    and 11 need a container and a port, and a lane that has neither must not report the absence
    as a verdict in either direction -- a green 12/12 from a run that never opened a socket is
    the certificate this whole family of guards exists to refuse, and a red one would send an
    operator looking for a regression in a behaviour nobody exercised.

    The number is carried rather than the label so the wording lives in `CHECKS` alone, which is
    the list the tally and the failure summary both read.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    UNMEASURED.append((label, reason))
    print(f"  [----] {console(label)}", flush=True)
    print(f"         NOT MEASURED HERE: {console(reason)}", flush=True)
    return None, ""


# --- the install ----------------------------------------------------------------------------------


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
    """Take the operator's own connector credentials away before anything reads them.

    `backend/tests/conftest.py` records the incident this repeats: a household that followed its
    own README runs this script with their real Jellyfin configured, and their encrypted
    credentials would be written into a throwaway database under a throwaway key. Derived from the
    connectors `registry.CONNECTORS` seeds, so a connector added there is neutralised here without
    anyone remembering this list. It used to say "derived from `Settings`" over four M0-era
    prefixes, and the three provider keys M5.5 seeds (§2, M5.5 plan A3) passed straight through it,
    into `os.environ` and so into check 5's child. [M5.5 review cycle 1, KEYS-C1-03, M55-DOC-07]

    It matters twice over in this script. `acquire/hosts.policy_for` takes a `jellyfin_host` and
    exempts it from the throttle and the robots check, and a run that inherited a real one could
    exempt a host checks 6 and 7 believe they are pacing.

    BOTH HALVES, for `ops/m53_exit_criterion.py`'s reason: `Settings` also reads `.env` from the
    working directory, so the process moves into a directory of this run's own DATA_DIR, which
    `main` sets on the line before this call and removes after moving back out.
    """
    seeded = tuple(f"{name}_" for name, spec in registry.CONNECTORS.items() if spec.seeded)
    # Matched case-insensitively, as `Settings` matches it: pydantic-settings reads the environment
    # with `case_sensitive` False, and a POSIX environment keeps `openai_api_key` apart from
    # `OPENAI_API_KEY`, so popping the upper-case spelling alone left a lower-case key for the seed.
    # [M5.5 review cycle 2, M55-KEYS-C2-04]
    wanted = {name.upper() for name in core_config.Settings.model_fields if name.startswith(seeded)}
    for variable in list(os.environ):
        if variable.upper() in wanted:
            os.environ.pop(variable, None)
    neutral = Path(os.environ["DATA_DIR"]) / "no-dot-env"
    neutral.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral)


async def _refuse_to_fetch(_conn: asyncpg.Connection) -> fetch.Fetcher:
    """The drain's default fetcher for this run: refused, because every stage that fetches is a
    declared no-op here and nothing this criterion names is fetched from a third party."""
    raise PreconditionFailed(
        "a stage asked for the drain's fetcher, and every stage from 2 to 8 is declared a no-op in "
        "this run (spec section 12, M5.1): nothing this criterion measures is fetched from a third "
        "party, so the request was refused rather than sent"
    )


def _stand_down_stages_two_to_eight() -> None:
    """§12's "with stages 2-8 declared no-ops", made true of the pipeline this run drains. See the
    module docstring: each stage keeps its number, name, `paid` and `owner`, advances without
    running, fetches nothing and is `implemented=False`; the drain's default fetcher refuses."""

    def declared_no_op(stage: pipeline.Stage) -> pipeline.Stage:
        async def stood_down(_ctx: stages.StageContext) -> stages.Outcome:
            return stages.advance({"stood_down": f"stage {stage.number} is declared a no-op by "
                                                 "ops/m51_exit_criterion.py (spec section 12, M5.1)"})

        return pipeline.Stage(stage.number, stage.name, stood_down, paid=stage.paid,
                              implemented=False, owner=stage.owner)

    pipeline.STAGES = tuple(
        declared_no_op(stage) if 2 <= stage.number <= 8 else stage for stage in pipeline.STAGES
    )
    pipeline._default_fetcher = _refuse_to_fetch


async def build_install(conn: asyncpg.Connection, bundle_root: Path, work: Path) -> Install:
    """Import the corpus bundle and make the household this run measures. Raises on a refusal.

    Everything the twelve checks need and nothing they do not: the content, the artifacts staged
    under the temporary DATA_DIR, the active `artifact_bundle` row, and one `app_user` for
    check 4's shelf. No admin account and no HTTP client, because no check here goes through a
    route -- check 11 is the exception and it probes an install this script did not build. And
    stages 2-8 declared no-ops, as the criterion states the install (see the module docstring).
    """
    _stand_down_stages_two_to_eight()
    bundle = bundle_import.Bundle.open(bundle_root)
    began = time.perf_counter()
    report = await bundle_import.import_bundle(conn, bundle, work / "artifacts")
    fails = [f"{f.rule}: {f.message[:200]}" for f in report.findings if f.severity == "fail"]
    if not report.ok or fails:
        raise PreconditionFailed(
            "the bundle did not import, so there is no install to measure: " + "; ".join(fails[:3])
        )
    version = await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    if not version:
        raise PreconditionFailed("the import left no active artifact_bundle row")
    titles = await conn.fetchval("SELECT count(*) FROM title")
    print(
        f"  imported {console(str(version))} in {time.perf_counter() - began:.0f}s: "
        f"{titles:,} titles, artifacts under {console(str(work / 'artifacts'))}",
        flush=True,
    )
    user_id = await conn.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('exit-criterion', 'admin') RETURNING id"
    )
    ctx = Install(
        conn=conn,
        work=work,
        artifacts_root=work / "artifacts",
        bundle_root=bundle_root,
        log=LogWatch(),
        version=str(version),
        # The app's own read of the vocabulary, not this script's idea of one:
        # `home/shelves.build_home` resolves `Ctx.version` with exactly this call, and a shelf
        # built against a different vocabulary is a shelf nobody serves.
        vocabulary=await why_mod.vocabulary_version(conn),
        user_id=int(user_id),
    )
    logging.getLogger("spielplan").addHandler(ctx.log)
    return ctx


async def _acquired(conn: asyncpg.Connection) -> dict[int, tuple[Any, ...]]:
    rows = await conn.fetch(
        "SELECT id, origin, placement, placement_bundle, is_owned, jellyfin_id"
        "  FROM title WHERE origin = 'acquired' ORDER BY id"
    )
    return {
        int(r["id"]): (
            r["origin"], r["placement"], r["placement_bundle"], r["is_owned"], r["jellyfin_id"]
        )
        for r in rows
    }


async def _board_rows(conn: asyncpg.Connection) -> dict[int, tuple[Any, ...]]:
    rows = await conn.fetch(
        "SELECT title_id, stage, status, reason FROM acquisition_job ORDER BY title_id"
    )
    return {int(r["title_id"]): (r["stage"], r["status"], r["reason"]) for r in rows}


async def snapshot(ctx: Install) -> dict[str, Any]:
    """`acquisition_job` and `title` as they stand, for check 12 to compare against.

    The acquired half is taken IN FULL and the bundle half as a count, and the asymmetry is the
    argument rather than a shortcut. This pipeline's only write into `title` is `stages._mint`,
    which INSERTs a new row above 1e9 and never updates one -- decision 162 in a line of SQL --
    so the whole surface a refusal could change is the acquired set plus the size of the table.
    `acquisition_job` is small enough on any install to take whole, and it is the row §6.6 shows
    an operator, so "unchanged" there has to mean every column a person would read.
    """
    return {
        "acquired": await _acquired(ctx.conn),
        "jobs": await _board_rows(ctx.conn),
        "titles": int(await ctx.conn.fetchval("SELECT count(*) FROM title")),
        "placements": int(await ctx.conn.fetchval("SELECT count(*) FROM title_placement")),
        "log_mark": len(ctx.log.records),
        "status_mark": len(ctx.statuses),
    }


def _raw_files() -> list[Path]:
    """Every file under the raw root, which is what check 6's `ls` means in Python."""
    root = core_config.settings().raw_dir
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _transport(ctx: Install) -> httpx.MockTransport:
    """The local double checks 6 and 7 fetch through, counting every call it is handed.

    It serves robots.txt as well as the document, because `acquire/fetch.Fetcher` asks for it
    before the first request to a host (decision 340) and a double that 404ed it would be
    measuring the fetcher's fail-open path instead of its normal one. The counter here is the
    ground truth check 7 holds `total_requests` against: the fetcher's own number is what the
    plan asks for, and a number a module keeps about itself is worth one independent witness.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        ctx.requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            ctx.statuses.append((str(request.url), 200))
            return httpx.Response(200, text=ROBOTS_BODY)
        body = SECOND_BYTES if request.url.path.endswith("9002.json") else DOC_BYTES
        ctx.statuses.append((str(request.url), 200))
        return httpx.Response(
            200, content=body,
            headers={"Content-Type": "application/json", "ETag": '"exit-criterion"'},
        )

    return httpx.MockTransport(handler)


async def _require_new(ctx: Install, item: dict[str, Any]) -> None:
    """Refuse to measure a mint against an item the resolver can already place.

    §7.1's resolver is fill-never-clobber and ends in a name-and-year branch, so on the corpus
    this is a real possibility rather than a formality -- and an item it resolved would make
    stage 1 return "resolved to an existing title" while every assertion below still passed.
    """
    found = await resolve.resolve_title_id(ctx.conn, item)
    if found is not None:
        raise PreconditionFailed(
            f"{item['Name']!r} already resolves to title {found}, so a mint cannot be measured "
            "against it"
        )


async def _drain_for(ctx: Install, item: dict[str, Any]) -> pipeline.TaskReport:
    """Enqueue one item, drain, and hand back that item's own report.

    By key and not by position: `drain` leases up to eight and the batch may carry a task an
    earlier check left pending, so a report picked off the front of the list is a report about
    whatever the queue's ORDER BY chose.
    """
    key = pipeline.key_for_item(item)
    if not await pipeline.enqueue_item(ctx.conn, item):
        raise PreconditionFailed(f"the queue already held a task keyed {key!r}")
    report = await pipeline.drain(ctx.conn)
    found = next((task for task in report.tasks if task.key == key), None)
    if found is None:
        raise PreconditionFailed(
            f"the drain leased {report.leased} task(s) and none of them was {key!r}"
        )
    return found


# --- 1: the walk ---------------------------------------------------------------------------------


async def check_one(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 1: an injected task walks §8's ten stages to `stage = 10, status = 'ready'`.

    THE STAGES ARE COUNTED AS WELL AS THE DESTINATION. A driver that jumped from 1 to 9 would
    leave the same board row as one that walked, and stages 2-8 being declared no-ops is exactly
    what makes the difference invisible in the result -- so the report's `stages_run` is asserted
    against §8's ten names in order. That is also the one assertion here that would catch a
    resume index computed off the wrong row.

    The queue is asserted beside the board because the criterion's later half depends on it:
    "completed exactly once" in check 5 is a claim about `acquisition_task.state`, and a task
    left `leased` behind a finished board row would satisfy every visible clause of this one.
    """
    await _require_new(ctx, ITEM_ONE)
    task = await _drain_for(ctx, ITEM_ONE)
    if task.title_id is None:
        raise PreconditionFailed("stage 1 established no title, so there is no board row to read")
    ctx.minted["one"] = int(task.title_id)
    board = await ctx.conn.fetchrow(
        "SELECT stage, status, reason FROM acquisition_job WHERE title_id = $1", task.title_id
    )
    state = await ctx.conn.fetchval(
        "SELECT state FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, task.key,
    )
    walked = list(task.stages_run)
    expected = [stage.name for stage in pipeline.STAGES]
    ok = (
        board is not None
        and int(board["stage"]) == 10
        and board["status"] == pipeline.READY
        and board["reason"] is None
        and task.status == pipeline.READY
        and walked == expected
        and state == queue.DONE
    )
    detail = (
        f"board: stage {board['stage'] if board else '-'} "
        f"status {board['status'] if board else '-'}; "
        f"queue: {state}; stages: {', '.join(walked)}"
    )
    return ok, detail


# --- 2: the id partition -------------------------------------------------------------------------


async def check_two(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 2: the minted `title.id` is >= 1,000,000,000 and the row says `origin =
    'acquired'`.

    Decision 162 is why this is a check rather than a comment. The corpus mints below 1e9 and
    this app at or above it; a title minted into the corpus's half is a row the next bundle
    import collides with, and the collision cannot be undone without dropping the database. The
    comparison is made against `stages.APP_ID_MIN` -- the constant the mint itself asserts
    against -- so a repair that moved the floor in one place is caught here rather than agreed
    with.

    `is_owned` is read with them because it is not decoration: §7.2 says the flag is
    "re-derived from Jellyfin, never trusted stale", seeing the item in the library IS that
    derivation, and Home's shelf filters on it. A title minted without it is invisible to
    check 4 for a reason that has nothing to do with placement.
    """
    title_id = ctx.minted.get("one")
    if title_id is None:
        raise PreconditionFailed("check 1 minted nothing, so there is no id to measure")
    row = await ctx.conn.fetchrow(
        "SELECT id, origin, kind, name, year, runtime_min, imdb_id, tmdb_id, jellyfin_id,"
        "       is_owned FROM title WHERE id = $1",
        title_id,
    )
    if row is None:
        raise PreconditionFailed(f"title {title_id} is gone")
    ok = (
        int(row["id"]) >= stages.APP_ID_MIN
        and stages.APP_ID_MIN == APP_ID_FLOOR
        and row["origin"] == "acquired"
        and row["is_owned"] is True
        and row["jellyfin_id"] == ITEM_ONE["Id"]
        and row["imdb_id"] == ITEM_ONE["ProviderIds"]["Imdb"]
        and row["kind"] == "movie"
        and row["year"] == ITEM_ONE["ProductionYear"]
    )
    detail = (
        f"id {int(row['id']):,} (floor {APP_ID_FLOOR:,}); origin {row['origin']}; "
        f"is_owned {row['is_owned']}; imdb {row['imdb_id']}; jellyfin {row['jellyfin_id']}; "
        f"{console(str(row['name']))} ({row['year']}, {row['runtime_min']} min)"
    )
    return ok, detail


# --- 3: the placement ----------------------------------------------------------------------------


async def check_three(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 3: a `title_placement` row exists, `placement = 'cold_tower'`, stamped with the
    active version.

    §8 stage 9 is shipped code with no caller until M5.1 (`placement/reconcile.py:208-212`'s
    `app_acquired` scope), so what this measures is the wiring rather than the tower. Both
    stamps are read, because they are written by different statements and §10 is the section
    that makes their disagreement expensive: `title_placement.bundle_version` is the row's own
    basis and `title.placement_bundle` is the denormalised copy every serving path reads, and a
    coordinate computed in one basis and labelled with another is "garbage against a new one"
    with nothing to say so.

    `nnz` is reported and deliberately not asserted. A minted title carries no DNA row at all,
    so its genome block is zero-imputed by construction (§8 stage 9's own parenthesis), and a
    threshold on it here would be this script inventing a quality bar the spec does not set.
    """
    title_id = ctx.minted.get("one")
    if title_id is None:
        raise PreconditionFailed("check 1 minted nothing, so there is nothing to place")
    row = await ctx.conn.fetchrow(
        "SELECT p.bundle_version, p.dim, p.nnz, p.blocks_present, p.blocks_imputed,"
        "       t.placement, t.placement_bundle"
        "  FROM title_placement p JOIN title t ON t.id = p.title_id"
        " WHERE p.title_id = $1",
        title_id,
    )
    rows = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", title_id
    ))
    if row is None:
        return False, f"no title_placement row for {title_id}; the active bundle is {ctx.version}"
    ok = (
        rows == 1
        and row["bundle_version"] == ctx.version
        and row["placement_bundle"] == ctx.version
        and row["placement"] == "cold_tower"
        and int(row["dim"]) == 64
    )
    detail = (
        f"{rows} row(s); bundle_version {console(str(row['bundle_version']))} "
        f"(active {console(ctx.version)}); title.placement {row['placement']} stamped "
        f"{console(str(row['placement_bundle']))}; dim {row['dim']}, nnz {row['nnz']}, "
        f"{len(row['blocks_present'])} block(s) present, {len(row['blocks_imputed'])} imputed"
    )
    return ok, detail


# --- 4: Home ------------------------------------------------------------------------------------


async def check_four(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 4: the minted title is on Home's "New in the library" shelf, with the badge.

    §6.0 row 6's shelf is the other §8 stage that shipped with no producer, and `title.origin =
    'acquired'` is the thing nothing in the tree wrote until stage 1. So this check is the
    second half of the same wiring claim check 3 makes, seen from the surface an actual person
    looks at.

    THE BADGE IS RESTATED HERE AND NOT READ OFF THE COMPONENT. `PosterCard.svelte:12-16`'s
    `isColdPlaced` is the rule -- `e_source === 'cold_tower'` where the payload has it, and
    `item_n === 0 || (item_n == null && placement === 'cold_tower')` where it does not -- and
    reading that file to confirm it would be reading a component's comments as if they were its
    markup, which `test_no_exit_measure_decides_on_a_component_it_read_with_the_comments_in`
    exists to refuse. What this asserts is the STATE the rule reads: a card carrying no crowd
    support and a Cold Tower placement. The card's own §6.7 gating cannot hide it, because
    `shelves.py:474-484` computes those two fields outside `model` for exactly that reason.

    The shelf is asked for through `shelves.new_in_library` with the `Ctx` the app builds
    (`shelves.py:1184-1189`), not through a query written here: the suppression rules, the
    kind partition and the unseen filter are all the shelf's, and a harness that reimplemented
    them would pass on a shelf that no longer ships.
    """
    title_id = ctx.minted.get("one")
    if title_id is None:
        raise PreconditionFailed("check 1 minted nothing, so no card can be looked for")
    shelf_ctx = shelves.Ctx(
        user_id=ctx.user_id, bundle_version=ctx.version, version=ctx.vocabulary, kinds=("movie",)
    )
    section, suppressed = await shelves.new_in_library(ctx.conn, ctx=shelf_ctx, kind="movie")
    if section is None:
        return False, f"the shelf was suppressed: {console(str(suppressed))}"
    shown = {int(card["title_id"]): card for card in section.items}
    card = shown.get(title_id)
    badged = card is not None and (
        card["e_source"] == "cold_tower"
        or card["item_n"] == 0
        or (card["item_n"] is None and card["placement"] == "cold_tower")
    )
    ok = (
        card is not None
        and badged
        and section.title == "New in the library"
        and section.why.startswith("placed by the Cold Tower")
        and "no crowd data yet" in section.why
    )
    detail = (
        f"shelf {console(section.title)} carries {len(section.items)} card(s); "
        f"{title_id} present: {card is not None}; "
        f"e_source {card['e_source'] if card else '-'}, item_n {card['item_n'] if card else '-'}, "
        f"placement {card['placement'] if card else '-'}; why: {console(section.why)}"
    )
    return ok, detail


# --- 5: the killed worker ------------------------------------------------------------------------


def _start_doomed_worker(ctx: Install, dsn: str) -> subprocess.Popen[str]:
    path = ctx.work / "doomed_worker.py"
    path.write_text(_DOOMED_WORKER, encoding="utf-8")
    env = dict(os.environ)
    env["SPIELPLAN_BACKEND"] = str(ROOT / "backend")
    env["SPIELPLAN_SCRATCH_DSN"] = dsn
    # `cwd` is the scratch tree and that is protection rather than tidiness, for the reason
    # `ops/m414_exit_criterion.py` chdirs its own child into a directory it made: pydantic-
    # settings reads `.env` relative to the working directory, and a child started in the
    # repository would boot this throwaway install against a developer's real configuration.
    return subprocess.Popen(
        [sys.executable, "-u", str(path)],
        env=env, cwd=str(ctx.work), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


async def check_five(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 5: a worker killed mid-lease is reclaimed, completed exactly once, with no
    duplicated derived row.

    A REAL CHILD PROCESS AND A REAL KILL. `queue.reclaim_expired`'s docstring is a claim about
    what survives `kill -9` -- "a SIGKILL past the stop grace, an OOM and a power cut leave one
    row: `leased`, owned, past its expiry" -- and a harness that simulated the death by updating
    the row would be asserting the sentence with the sentence. The child leases through the same
    `queue.lease` the drain uses, mints through the same `stages.identify`, and is killed by this
    process while holding the connection.

    THE LEASE IS THEN FORCED TO EXPIRE, AND THAT HALF IS SIMULATED -- said plainly rather than
    buried. `LEASE_SECONDS` is 900 and no criterion run can wait fifteen minutes, and Postgres's
    `now()` is not a clock a harness can move (`test_acquire_queue.py` records the same
    limitation and the same manoeuvre). What is NOT simulated is everything the check is about:
    the window is measured before it is closed, so the report says how long the reclaim would
    really have waited, and the row that is reclaimed is the row a killed process actually left.

    "EXACTLY ONCE" IS FOUR COUNTS, because one of them alone is satisfiable by the wrong
    machine. One `title` row for the item -- the mint did not happen twice, which is the
    duplicate decision 162 makes permanent. One `acquisition_task` row -- the reclaim returned
    the task rather than enqueueing a second. One `acquisition_job` row at stage 10 -- the board
    has one story about this title. One `title_placement` row for the active basis -- stage 9's
    `ON CONFLICT (title_id, bundle_version) DO UPDATE` rewrote rather than appended. And
    `attempts` reads 2: two claims, one completion, which is the number that would read 1 if the
    reclaim had silently re-enqueued instead.
    """
    await _require_new(ctx, ITEM_TWO)
    key = pipeline.key_for_item(ITEM_TWO)
    if not await pipeline.enqueue_item(ctx.conn, ITEM_TWO):
        raise PreconditionFailed(f"the queue already held a task keyed {key!r}")

    proc = _start_doomed_worker(ctx, os.environ["SPIELPLAN_SCRATCH_DSN"])
    spoken, rest = "(silent)", ""
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline), timeout=CHILD_DEADLINE_S
        )
        spoken = line.strip()
    except TimeoutError:
        spoken = f"(nothing within {CHILD_DEADLINE_S:.0f}s)"
    finally:
        # The kill the criterion names. Everything the child was going to record, it has not.
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
            rest, _ = proc.communicate(timeout=30)

    if " title " not in spoken:
        raise PreconditionFailed(
            f"the doomed worker never leased and minted: it said {console(spoken)!r}, then "
            f"{console((rest or '').strip()[:400])!r}"
        )
    child_title = int(spoken.rsplit(" title ", 1)[1].split()[0])

    held = await ctx.conn.fetchrow(
        "SELECT id, state, attempts, lease_owner,"
        "       EXTRACT(EPOCH FROM (lease_expires - now())) AS remaining"
        "  FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, key,
    )
    if held is None or held["state"] != queue.LEASED:
        raise PreconditionFailed(
            f"the killed worker did not leave a leased row: {held and dict(held)}"
        )
    window = float(held["remaining"] or 0.0)
    owner = str(held["lease_owner"])

    # The fifteen minutes, spent. Strictly in the past, because `reclaim_expired` asks for
    # `lease_expires < now()` and a row expiring exactly now is a row it does not see.
    await ctx.conn.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 second' WHERE id = $1",
        held["id"],
    )
    report = await pipeline.drain(ctx.conn)
    task = next((t for t in report.tasks if t.key == key), None)
    if task is None:
        raise PreconditionFailed(
            f"the next drain did not lease the reclaimed task: {report.as_dict()}"
        )

    titles = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM title WHERE jellyfin_id = $1", ITEM_TWO["Id"]
    ))
    tasks = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, key,
    ))
    final = await ctx.conn.fetchrow(
        "SELECT state, attempts, lease_owner FROM acquisition_task WHERE id = $1", held["id"]
    )
    boards = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", task.title_id
    ))
    board = await ctx.conn.fetchrow(
        "SELECT stage, status FROM acquisition_job WHERE title_id = $1", task.title_id
    )
    placements = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", task.title_id
    ))
    ctx.minted["two"] = int(task.title_id) if task.title_id is not None else 0
    ok = (
        report.reclaimed.get(queue.PENDING) == 1
        and report.reclaimed.get(queue.FAILED, 0) == 0
        and task.title_id == child_title
        and titles == 1
        and tasks == 1
        and placements == 1
        and boards == 1
        and board is not None
        and (int(board["stage"]), board["status"]) == (10, pipeline.READY)
        and final is not None
        and final["state"] == queue.DONE
        and int(final["attempts"]) == 2
        and final["lease_owner"] is None
        and owner != queue.worker_id()
    )
    detail = (
        f"the child minted {child_title} and was killed holding a lease with {window:.0f}s of "
        f"{queue.LEASE_SECONDS:.0f}s left (owner {console(owner)}); that window was then forced "
        "shut because no run can wait it out. "
        f"reclaimed {report.reclaimed}; task {final['state'] if final else '-'} after "
        f"{final['attempts'] if final else '-'} attempt(s); counts: {titles} title, {tasks} task, "
        f"{boards} board row, {placements} placement"
    )
    return ok, detail


# --- 6 and 7: the raw store ----------------------------------------------------------------------


async def check_six(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 6: the same url fetched twice leaves ONE file under `/data/raw` and TWO
    `raw_document` rows.

    §8's closing sentence is what this is: "All fetched bytes land in the app's own raw store,
    so re-parsing is free forever" (`spec:402`). The store is content-addressed and write-once,
    so identical bytes resolve to a path that already exists and are not written again -- while
    the INSERT happens both times, because the row is the only record of WHEN this app last saw
    this url, which is the question §6.6's board and a conditional re-fetch both ask. That is
    why `0024` leaves `raw_document.content_sha256` deliberately not unique, and a reader who
    "fixes" that has broken this check and the board together.

    TWO FETCHERS AND NOT ONE, because the criterion's two fetches are two drains: `Fetcher` is
    built per drain and rebuilt from `fetch_host_state` at the top of the next one. Running them
    inside one instance would measure a per-instance memo rather than the store.

    The file count is asserted as a DELTA over the whole raw tree and again against the digest,
    so neither a store that was already dirty nor a later check's document can make it true by
    accident.
    """
    before = _raw_files()
    async with fetch.Fetcher(conn=ctx.conn, transport=_transport(ctx)) as first:
        one = await first.get(DOC_URL)
        doc_one = await rawstore.store(
            ctx.conn, source=DOC_SOURCE, kind="title", url=DOC_URL, content=one.content,
            entity_key="9001", http_status=one.status, content_type=one.content_type,
            etag=one.headers.get("etag"),
        )
    async with fetch.Fetcher(conn=ctx.conn, transport=_transport(ctx)) as second:
        two = await second.get(DOC_URL)
        doc_two = await rawstore.store(
            ctx.conn, source=DOC_SOURCE, kind="title", url=DOC_URL, content=two.content,
            entity_key="9001", http_status=two.status, content_type=two.content_type,
            etag=two.headers.get("etag"),
        )
    ctx.documents = [int(doc_one), int(doc_two)]

    after = _raw_files()
    digest = rawstore.sha256_bytes(DOC_BYTES)
    named = [path for path in after if digest in path.name]
    rows = await ctx.conn.fetch(
        "SELECT id, content_path, content_sha256, byte_size, http_status FROM raw_document"
        " WHERE url = $1 ORDER BY id", DOC_URL,
    )
    paths = {row["content_path"] for row in rows}
    ok = (
        len(after) - len(before) == 1
        and len(named) == 1
        and len(rows) == 2
        and len(paths) == 1
        and {row["content_sha256"] for row in rows} == {digest}
        and all(int(row["byte_size"]) == len(DOC_BYTES) for row in rows)
        and rawstore.resolve(next(iter(paths))).is_file()
    )
    root = console(str(core_config.settings().raw_dir))
    detail = (
        f"{len(after) - len(before)} new file(s) under {root}, {len(named)} of them named for "
        f"the digest; {len(rows)} raw_document row(s) sharing {len(paths)} content_path; "
        f"sha256 {digest[:16]}..., {len(DOC_BYTES)} bytes each"
    )
    return ok, detail


async def check_seven(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 7: a re-parse issues zero outbound requests, asserted on the fetcher's own
    counter.

    THE COUNTER IS SHOWN TO MOVE BEFORE IT IS SHOWN TO HOLD STILL, which is the whole design of
    this check. `Fetcher.total_requests` starts at zero, so a run that read the store with a
    fresh fetcher would assert `0 == 0` and pass against a module that had stopped counting
    entirely. So one real fetch happens first, on a SECOND url so check 6's two-rows claim about
    the first is left exact; the counter is read; then both of check 6's documents are re-parsed
    off the disk and the counter is read again. The transport's own list is checked alongside it,
    because a counter a module keeps about itself deserves one independent witness.

    The re-parse goes through `rawstore.read` and `rawstore.read_path` -- the two entry points
    §8 stage 3 will use -- and not through a `gzip.open` written here. That module imports no
    HTTP client by construction and a test that bypassed it would be asserting nothing about the
    code M5.3 is going to call.
    """
    async with fetch.Fetcher(conn=ctx.conn, transport=_transport(ctx)) as fetcher:
        live = await fetcher.get(SECOND_URL)
        await rawstore.store(
            ctx.conn, source=DOC_SOURCE, kind="title", url=SECOND_URL, content=live.content,
            entity_key="9002", http_status=live.status, content_type=live.content_type,
        )
        moved = fetcher.total_requests
        seen = len(ctx.requests)

        if len(ctx.documents) != 2:
            raise PreconditionFailed(
                "check 6 stored no pair of documents, so there is nothing to re-parse"
            )
        first = json.loads(await rawstore.read(ctx.conn, ctx.documents[0]))
        second = json.loads(await rawstore.read(ctx.conn, ctx.documents[1]))
        row = await rawstore.latest_for_url(ctx.conn, DOC_URL)
        if row is None:
            raise PreconditionFailed(f"the store holds nothing for {DOC_URL}")
        by_path = json.loads(rawstore.read_path(row["content_path"]))
        held = fetcher.total_requests
        still = len(ctx.requests)

    expected = json.loads(DOC_BYTES)
    ok = (
        moved > 0
        and held == moved
        and still == seen
        and first == second == by_path == expected
        and live.content == SECOND_BYTES
    )
    detail = (
        f"one live fetch moved the fetcher's counter to {moved} and the transport's to {seen}; "
        f"three re-parses of two stored documents left them at {held} and {still}; the bytes "
        "read back off the disk equal the bytes that were fetched"
    )
    return ok, detail


# --- 8: no provider id ---------------------------------------------------------------------------


async def check_eight(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 8: an item with empty `ProviderIds` parks at stage 1 with the reason "no
    provider id", and `count(*) FROM title WHERE origin = 'acquired'` is unchanged.

    Decision 323 in a measurement. The mint is the one write this pipeline cannot take back, so
    it happens only on an imdb, tmdb or tvdb id; a name-and-year mint is the silent wrong match
    `connectors/resolve.py` already refuses.

    WHERE THE REASON LIVES IS NOT WHERE THE PLAN'S TABLE LOOKED, and this check measures the
    code rather than the table. Plan §7 row 8 expects `acquisition_job` at stage 1 with decision
    336's status; that row cannot exist, and the reason is decision 322. The board's primary key
    is `title_id` and decision 323 forbids the mint, so there is no title for §6.6 to show a row
    about -- `pipeline.run_task`'s docstring states it outright: "a task that parks at stage 1
    therefore writes no board row at all, and the reason lives on the TASK, where `queue.skip`
    puts it". Inventing a title to hang a reason on would be the mint the decision forbids. So
    the assertions are: the queue row is `skipped` (which is decision 336's park with no time --
    "waiting on something that may change" and never auto-failing, where the thing that may
    change is an operator adding the id in Jellyfin) and carries the reason verbatim; the board
    gained nothing; and no title was minted. An operator looking for this title on the board
    will not find one, which is a gap M5.6 inherits with the board's read side.
    """
    await _require_new(ctx, ITEM_NO_IDS)
    before_titles = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM title WHERE origin = 'acquired'"
    ))
    before_jobs = int(await ctx.conn.fetchval("SELECT count(*) FROM acquisition_job"))
    task = await _drain_for(ctx, ITEM_NO_IDS)
    row = await ctx.conn.fetchrow(
        "SELECT state, result_note, last_error FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, task.key,
    )
    after_titles = int(await ctx.conn.fetchval(
        "SELECT count(*) FROM title WHERE origin = 'acquired'"
    ))
    after_jobs = int(await ctx.conn.fetchval("SELECT count(*) FROM acquisition_job"))
    note = str(row["result_note"] or "") if row is not None else ""
    ok = (
        task.title_id is None
        and task.stage == 1
        and task.status == pipeline.PARKED
        and task.reason == stages.NO_PROVIDER_ID
        and row is not None
        and row["state"] == queue.SKIPPED
        and note.startswith("no provider id")
        and after_titles == before_titles
        and after_jobs == before_jobs
    )
    detail = (
        f"parked at stage {task.stage}; queue state {row['state'] if row else '-'}; "
        f"reason: {console(note[:140])}; acquired titles {before_titles} -> {after_titles}; "
        f"acquisition_job rows {before_jobs} -> {after_jobs} (the board cannot carry this one: "
        "its primary key is title_id and decision 323 forbids the mint)"
    )
    return ok, detail


# --- 9: the custody boundary ---------------------------------------------------------------------


def _compose(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=project, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S, check=False,
    )


async def check_nine(_ctx: Install) -> tuple[bool | None, str]:
    """Plan check 9: `docker compose exec backend ls /data/raw` fails while `exec worker ls
    /data/raw` succeeds.

    Decision 345 and M4.7's sec-08, measured from outside the app. `docker-compose.yml:45` puts
    `./data/raw:/data/raw` under `x-worker-volumes` and the backend's anchor omits it, which is
    what makes the board's refusal to hand back a `content_path` a boundary rather than a
    convention: the container that serves §6.6 cannot open the file even if a future route
    decided to.

    THIS CANNOT BE MEASURED WITHOUT THE STACK, and it does not pretend otherwise. Two things are
    checked before either `exec` runs -- docker on PATH, and `compose ps` reporting both services
    running -- because an `exec` against a stopped container also exits non-zero, and a check
    that read that as "the backend cannot see /data/raw" would pass on an install with no
    containers at all. That is the false pass this whole family of guards exists to refuse.
    """
    if os.environ.get("SPIELPLAN_EXIT_NO_DOCKER"):
        return not_measured(9, "SPIELPLAN_EXIT_NO_DOCKER is set, so no container was asked")
    if shutil.which("docker") is None:
        return not_measured(9, "docker is not on PATH, so there is no container to ask")
    project = os.environ.get("SPIELPLAN_EXIT_COMPOSE_DIR") or str(ROOT)
    try:
        listed = _compose(project, "ps", "--status", "running", "--format", "{{.Service}}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return not_measured(9, f"docker compose ps did not answer from {project}: {exc}")
    running = set(listed.stdout.split())
    if listed.returncode != 0 or not {"backend", "worker"} <= running:
        return not_measured(
            9,
            f"docker compose ps in {project} reports running services {sorted(running) or 'none'}"
            ", so the stack this check measures is not up",
        )
    try:
        worker = _compose(project, "exec", "-T", "worker", "ls", "/data/raw")
        backend = _compose(project, "exec", "-T", "backend", "ls", "/data/raw")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return not_measured(9, f"docker compose exec did not answer: {exc}")
    ok = worker.returncode == 0 and backend.returncode != 0
    detail = (
        f"worker ls /data/raw exited {worker.returncode}; backend ls /data/raw exited "
        f"{backend.returncode} saying {console(backend.stderr.strip()[:120] or '(nothing)')}"
    )
    return ok, detail


# --- 10: the bundle-less install -----------------------------------------------------------------


async def check_ten(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 10: placement with no active bundle parks with that reason, and nothing faults.

    §3.1 makes a bundle-less household legal and `title_placement.bundle_version` is `NOT NULL
    REFERENCES artifact_bundle(version)` (`0008_placement.sql:14`), so an acquired title on such
    an install genuinely cannot be placed. The plan's own risks section gives the rule in three
    words -- park, do not raise -- and the difference is not cosmetic: `queue.fail` spends an
    attempt and four of them close the task for good, so a household that has not imported a
    bundle yet would LOSE the title rather than wait for the import that is coming.

    AND THE WAIT HAS TO BE A WAIT, which is what this check asserts about the queue row and what
    review cycle 1 corrected. It asserted `skipped`, which is decision 336's park with no time --
    and nothing in the tree moves a row out of `skipped`: `queue.lease` claims `pending` only, the
    reaper touches `leased` only, `retry_failed` was deliberately not ported, and
    `queue.enqueue`'s `ON CONFLICT DO NOTHING` refuses to revive the key. So the state chosen to
    avoid losing the title lost it on attempt one, while the reason shown verbatim on §6.6's board
    promised the household that importing a bundle would place it "on the next drain". The park
    now carries `stages.waiting_on_the_world()` and the task is `pending` with its attempt handed
    back, which is the state that sentence describes. [M51-CRASH-01, M51-REV-03]

    THE ACTIVE ROW IS MOVED AND PUT BACK, which is the one piece of install state this script
    changes. There is no other way to reach §3.1's household from an install that has seeded --
    decision 162 makes content seed once, so this run cannot build a second install cheaply --
    and `ops/m413_exit_criterion.py:557-560` takes the same manoeuvre for the same reason. The
    restore is in a `finally` and is asserted here as well as in check 12, because a criterion
    that left the install it measured in a state it created would be measuring its own wreckage
    from the next check onwards. Decision 249 is untouched: the row is superseded and restored,
    never deleted.
    """
    await _require_new(ctx, ITEM_BUNDLELESS)
    mark = len(ctx.log.records)
    await ctx.conn.execute(
        "UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active' AND version = $1",
        ctx.version,
    )
    try:
        task = await _drain_for(ctx, ITEM_BUNDLELESS)
        board = await ctx.conn.fetchrow(
            "SELECT stage, status, reason FROM acquisition_job WHERE title_id = $1", task.title_id
        )
        placements = int(await ctx.conn.fetchval(
            "SELECT count(*) FROM title_placement WHERE title_id = $1", task.title_id
        ))
        row = await ctx.conn.fetchrow(
            "SELECT state, attempts, next_attempt_at > now() AS in_future "
            "  FROM acquisition_task WHERE kind = $1 AND key = $2",
            pipeline.TASK_KIND, task.key,
        )
        state = row["state"] if row is not None else None
        deferred = bool(row is not None and row["in_future"] and int(row["attempts"]) == 0)
    finally:
        await ctx.conn.execute(
            "UPDATE artifact_bundle SET state = 'active' WHERE version = $1", ctx.version
        )
    ctx.minted["bundleless"] = int(task.title_id) if task.title_id is not None else 0
    restored = await ctx.conn.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = $1", ctx.version
    )
    broke = faults(ctx.log.records[mark:])
    ok = (
        task.stage == 9
        and task.status == pipeline.PARKED
        and task.reason == stages.NO_ACTIVE_BUNDLE
        and board is not None
        and int(board["stage"]) == 9
        and board["status"] == pipeline.PARKED
        and board["reason"] == stages.NO_ACTIVE_BUNDLE
        and placements == 0
        # PENDING with the attempt handed back, and not `skipped`. Review cycle 1's correction:
        # `queue.skip` writes a state no lease, no reclaim and no sweep can move, so the park
        # chosen to protect this title from `max_attempts` closed it on attempt one, and importing
        # the bundle its own reason names would have revived nothing. [M51-CRASH-01]
        and state == queue.PENDING
        and deferred
        and restored == "active"
        and not broke
    )
    detail = (
        f"parked at stage {task.stage} with the board reading {board['status'] if board else '-'}; "
        f"reason: {console(str(task.reason)[:140])}; {placements} placement row(s); queue "
        f"{state}, re-asks later: {deferred}; the active row is {console(str(restored))} again; "
        f"{len(broke)} fault(s) logged: {console('; '.join(broke[:2]) or 'none')}"
    )
    return ok, detail


# --- 11: the /events namespace -------------------------------------------------------------------


async def check_eleven(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 11: `POST /events/nothing` is 404 -- not 405, and not `index.html`.

    Decision 332. `/events` is a server namespace and `SpaFallback.matches` declines it exactly as
    it declines `/api`, one rule over `SpaFallback.SERVER_NAMESPACES`, so an unrouted path under it
    is a 404 rather than the shell with a 200. The three wrong answers are all reachable and all
    differ: the shell means the fallback answered, a 405 means a route exists and refused the
    method, and a 200 of any kind means M5.2's webhook is shadowed by static serving.

    It is measured over HTTP against an install the operator names, and not against an app this
    script builds. The behaviour is a property of the mounted application -- `SpaFallback` only
    has anything to decline when a static directory is configured -- and an ASGI app assembled
    here with a directory this script made would be measuring its own assembly.
    """
    base = os.environ.get("SPIELPLAN_EXIT_BASE_URL", "").strip().rstrip("/")
    if not base:
        return not_measured(
            11, "SPIELPLAN_EXIT_BASE_URL is unset, so there is no running app to ask"
        )
    url = f"{base}/events/nothing"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.post(url)
    except httpx.HTTPError as exc:
        return not_measured(11, f"{url} did not answer: {type(exc).__name__}: {exc}")
    ctx.statuses.append((url, resp.status_code))
    body = resp.text[:400]
    shell = "<html" in body.lower() or "<!doctype" in body.lower()
    ok = resp.status_code == 404 and not shell
    detail = (
        f"POST {console(url)} answered {resp.status_code} "
        f"({console(resp.headers.get('content-type', '-'))}); the SPA shell: {shell}; "
        f"body starts {console(body[:80])!r}"
    )
    return ok, detail


# --- 12: the install after every refusal ---------------------------------------------------------


async def check_twelve(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 12: after every refusal above, `acquisition_job` and `title` are unchanged and
    nothing faulted.

    "UNCHANGED" IS SAID PRECISELY, because the loose reading is false and the check would be
    worthless if it were written loosely. Check 10's item carries a provider id, so stage 1
    mints it and the board gains its parked row -- that is the pipeline working, not a refusal
    leaking. What must hold is the narrower and stronger claim: every row that existed when the
    successful walks finished is byte-identical now, and the only rows that appeared are the
    ones exactly one refusal was entitled to add.

    Four assertions, then:

      * every acquired `title` row in the snapshot still reads the same five columns, so no
        refusal rewrote a title that was already placed and badged;
      * every `acquisition_job` row in the snapshot still reads the same stage, status and
        reason, so no refusal dragged a finished job backwards -- which is the failure mode
        `reconcile._park_thin`'s `ON CONFLICT (title_id) DO NOTHING` exists to prevent and which
        a driver could reintroduce from the other side;
      * the rows that appeared are check 10's one minted title and its one parked board row, and
        check 8's item added neither;
      * no `title_placement` row was written while the bundle was superseded, nothing logged a
        fault, and no request this run made answered 5xx.
    """
    before = ctx.snapshot
    if not before:
        raise PreconditionFailed("no snapshot was taken, so there is nothing to compare against")
    acquired = await _acquired(ctx.conn)
    jobs = await _board_rows(ctx.conn)
    placements = int(await ctx.conn.fetchval("SELECT count(*) FROM title_placement"))
    titles = int(await ctx.conn.fetchval("SELECT count(*) FROM title"))

    moved_titles = [
        f"title {title_id}: {was} -> {acquired.get(title_id)}"
        for title_id, was in before["acquired"].items()
        if acquired.get(title_id) != was
    ]
    moved_jobs = [
        f"job {title_id}: {was} -> {jobs.get(title_id)}"
        for title_id, was in before["jobs"].items()
        if jobs.get(title_id) != was
    ]
    added_titles = sorted(set(acquired) - set(before["acquired"]))
    added_jobs = sorted(set(jobs) - set(before["jobs"]))
    allowed = [ctx.minted.get("bundleless", 0)]
    broke = faults(ctx.log.records[before["log_mark"]:])
    warned = warnings_of(ctx.log.records[before["log_mark"]:])
    bad = [f"{url} -> {status}" for url, status in ctx.statuses[before["status_mark"]:]
           if status >= 500]
    ok = (
        not moved_titles
        and not moved_jobs
        and added_titles == allowed
        and added_jobs == allowed
        and titles == before["titles"] + 1
        and placements == before["placements"]
        and not broke
        and not bad
    )
    detail = (
        f"{len(before['acquired'])} acquired title(s) and {len(before['jobs'])} board row(s) "
        f"carried into the refusals: {len(moved_titles)} moved, {len(moved_jobs)} rewritten. "
        f"added: titles {added_titles}, jobs {added_jobs} (allowed: {allowed}, check 10's mint). "
        f"title_placement {before['placements']} -> {placements}. "
        f"{len(broke)} fault(s), {len(warned)} warning(s), {len(bad)} response(s) at 5xx. "
        + console("; ".join(moved_titles[:2] + moved_jobs[:2] + broke[:2] + bad[:2]))
    )
    return ok, detail


# --- the run ---------------------------------------------------------------------------------------


RUNNERS = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five, 6: check_six,
    7: check_seven, 8: check_eight, 9: check_nine, 10: check_ten, 11: check_eleven,
    12: check_twelve,
}
RECORDED: set[int] = set()


async def measure(number: int, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's
    twelve: a run that stops at three and prints "3/3 checks passed" is the failure mode an exit
    criterion exists to rule out. A refused precondition is reported without its traceback --
    the sentence IS the diagnosis, and a stack trace over it is what `ops/m4_exit_criterion.py`'s
    seeding taught this project to stop printing.

    A runner that answers None has already reported itself as not measured, and nothing is
    appended to `results` for it: it belongs in neither half of a tally of verdicts.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    RECORDED.add(number)
    try:
        verdict, detail = await RUNNERS[number](ctx)
    except PreconditionFailed as exc:
        check(False, label, f"PRECONDITION FAILED: {exc}")
        return
    except Exception as exc:                       # reported, not propagated
        check(
            False, label,
            f"the check stopped on {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
        return
    if verdict is None:
        return
    check(verdict, label, detail)


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL export bundle on purpose:")
        print("a mint is only a mint if the resolver could not place the item, and that question")
        print("is only interesting against the 19,000 titles the corpus ships. It also needs a")
        print("real basis, because a minted title carries no DNA row and stage 9 has to place it")
        print("anyway.")
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
        print("Refusing on purpose: every check below would still print, and none of them would")
        print("mean what its sentence says.")
        return 2
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print(
        f"\nM5.1 exit criterion -- bundle {console(root.name)} ({inventoried:,} bytes)\n",
        flush=True,
    )

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    scratch = f"spielplan_m51_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE -- the mkdtemp, the connect, the codec registration this project has been
    # bitten by once, the import, the child process -- happens inside the `try`, because the
    # scratch name carries this run's pid: pid-suffixing it (which is what stops two concurrent
    # runs dropping each other's database) removed the accidental second chance the next run's
    # `DROP DATABASE IF EXISTS` used to be. An orphan nothing will ever name again is a leak on
    # the household's own server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    ctx: Install | None = None
    # Resolved before anything moves this process: `_neutralise_connector_env` chdirs into the
    # scratch tree, and Windows will not delete the tree a process is standing in.
    started_in = Path.cwd()
    try:
        work = Path(tempfile.mkdtemp(prefix="spielplan-m51-exit-"))
        # A temporary DATA_DIR, removed in the `finally`. It is the artifacts root AND the raw
        # root: `settings.artifacts_dir` is `data_dir / "artifacts"` and `settings.raw_dir` is
        # `data_dir / "raw"`, and checks 3, 6 and 7 reach both through `settings()` exactly as
        # `stages.active_store` and `rawstore.resolve` do in production. Staging into `ROOT/data`
        # would leave hundreds of megabytes inside the working tree per run.
        os.environ["DATA_DIR"] = str(work)
        os.environ["SPIELPLAN_SCRATCH_DSN"] = scratch_dsn
        _neutralise_connector_env()
        core_config.settings.cache_clear()
        conn = await asyncpg.connect(scratch_dsn)
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. Without it `title_meta.payload` fails with "expected str, got dict" --
        # which M4.5's harness found, and which was a defect in the script rather than in the
        # importer.
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The install this run measures (spec section 10; decision 162)", flush=True)
        ctx = await build_install(conn, root, work)

        print("\n1. The walk: an injected task through the ten stages (plan check 1)", flush=True)
        await measure(1, ctx)
        print("\n2. The id partition: decision 162's floor (plan check 2)", flush=True)
        await measure(2, ctx)
        print("\n3. The placement: stage 9's first caller (plan check 3)", flush=True)
        await measure(3, ctx)
        print("\n4. Home: stage 10's shelf and its badge (plan check 4)", flush=True)
        await measure(4, ctx)
        print("\n5. The killed worker: reclaimed, completed once (plan check 5)", flush=True)
        await measure(5, ctx)
        print("\n6. The raw store: one url, two fetches (plan check 6)", flush=True)
        await measure(6, ctx)
        print("\n7. The re-parse: no request issued (plan check 7)", flush=True)
        await measure(7, ctx)

        # Taken here and not in `build_install`: check 12's sentence is "after every REFUSAL", so
        # the state it compares against is the state the successful walks left. A snapshot taken
        # before them would be comparing the whole run, and one taken after the refusals would
        # be comparing nothing at all.
        ctx.snapshot = await snapshot(ctx)
        print(
            f"\n   snapshot: {len(ctx.snapshot['acquired'])} acquired title(s), "
            f"{len(ctx.snapshot['jobs'])} board row(s), "
            f"{ctx.snapshot['placements']} placement(s)",
            flush=True,
        )

        print("\n8. The refusal: an item with no provider id (plan check 8)", flush=True)
        await measure(8, ctx)
        print("\n9. The custody boundary: /data/raw per container (plan check 9)", flush=True)
        await measure(9, ctx)
        print("\n10. The refusal: placement with no active bundle (plan check 10)", flush=True)
        await measure(10, ctx)
        print("\n11. The namespace: POST /events/nothing (plan check 11)", flush=True)
        await measure(11, ctx)
        print("\n12. The install after every refusal (plan check 12)", flush=True)
        await measure(12, ctx)
    except Exception as exc:
        # Everything outside a check: the connect, the migration, the import, the app's boot.
        # Reported as the failures they are, so the exit code stays non-zero and the run still
        # ends in a score rather than in a traceback where the sentence naming the cause belongs.
        # The handler catches everything on purpose: narrowing it to `asyncpg.PostgresError` --
        # the narrowing a reviewer proposes for an async DB harness -- would leave the guard green
        # while an `OSError` out of the staging copy or a `subprocess` failure reverted this run
        # to the bare traceback that loses every section below it and the tally.
        # [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        first = True
        for number, title in CHECKS:
            if number in RECORDED:
                continue
            check(False, f"{number}. {title}", stopped + ("\n" + trace if first else ""))
            first = False
    finally:
        if ctx is not None:
            logging.getLogger("spielplan").removeHandler(ctx.log)
        if conn is not None:
            await conn.close()
        os.chdir(started_in)
        discard_staged_artifacts(work, None)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    failed = len(results) - passed
    unmeasured = len(UNMEASURED)
    print(
        f"\n{passed}/{len(CHECKS)} checks passed, {failed} failed, "
        f"{unmeasured} not measured here",
        flush=True,
    )
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    for label, reason in UNMEASURED:
        print(f"  NOT MEASURED: {console(label)} -- {console(reason)}", flush=True)
    # Three codes and not two. 1 is a criterion that was measured and came out no; 3 is a run
    # that measured everything it could and could not measure all twelve, which is neither a
    # pass nor a failure and must not be reported as either (decision 184).
    return 1 if failed else (0 if not unmeasured else 3)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
