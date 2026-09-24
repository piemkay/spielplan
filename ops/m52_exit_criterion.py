"""M5.2's exit criterion: §7.2's two intake paths, measured on a real install.

§12's M5.2 row names this script, and the eleven checks below are `docs/milestones/M5.2-plan.md`
§7's own pass table, numbered as it numbers them and in its order. The row's sentence is what they
add up to:

    twelve `ItemAdded` events for twelve episodes of one series inside the ten-minute window
    yield one acquisition task for the show and none for any episode, and still one when the
    sweep is restarted at minute five. With the webhook plugin absent the delta poll enqueues
    the same set exactly once, enqueues nothing on the following poll, and leaves the watermark
    untouched after a read that failed. A title present in the database and absent from the
    current mirror flips `is_owned = false` through the full sweep and flips back when re-added,
    with no ownership write from the delta path at all. An `ItemAdded` in a library the admin did
    not pick is recorded with that reason and enqueues nothing; a wrong token is 401 with nothing
    recorded as pending; a payload missing a required field is 202, recorded, and no 500 in the
    log.

      script  plan  what it measures
        1       1   twelve ItemAdded for one series in one window are ONE task for the show
        2       2   the same burst, the worker restarted at minute five as a new sweep
                    process, is still one task
        3       3   the webhook silent, one delta poll: the same key set, each key once
        4       4   a second poll with nothing added enqueues nothing
        5       5   a poll whose Jellyfin read failed leaves the watermark exactly where it was
        6       6   a title gone from the mirror is un-owned by the FULL SWEEP
        7       7   the same title re-added is owned again by the next full sweep
        8       8   an add in a library the admin did not pick is recorded and enqueues nothing
        9       9   a wrong token is 401, records nothing, and enqueues nothing
       10      10   a payload missing a required field is 202, recorded, and faults nothing
       11      11   across the whole run, only the full sweep ever wrote the ownership column

ONE CLAUSE OF §12'S SENTENCE IS NOT ONE OF THESE ELEVEN, and decision 184's rule is to say so
rather than let a reader go looking for a twelfth. "A re-stamped title the household already owns
exits at stage 1 without minting a second title, without rewriting its board row and without any
paid work, filed below every genuine add" (decision 411) is measured by
`backend/tests/test_jellyfin_intake.py::test_a_re_scan_of_a_title_the_household_owns_mints_
nothing_and_bills_nothing`, registered under the same coverage row this script's checks 1 to 4
serve. Decision 371 fixes this instrument at the plan's eleven checks in the plan's own order, and
a check invented here that the plan never asked for would be this script grading its own paper.

THE COVERAGE ROWS EACH CHECK ANSWERS FOR, so a reader can walk from the criterion to the map:

    checks 1, 2, 3, 4    jellyfin-acquisition-eval-new-title-trigger-one-job-per-title
    checks 6, 7, 11      the same row's ownership clause, plus
                         jellyfin-sync-ownership-is-falsified-when-the-library-drops-a-title
    check 8              jellyfin-acquisition-eval-an-add-outside-the-picked-libraries-is-
                         recorded-not-acquired
    checks 5, 9, 10      jellyfin-acquisition-eval-the-webhook-survives-what-a-real-server-sends

THE ORDER IS THE PLAN'S AND IS ALSO THE ONLY ORDER THAT MEASURES ANYTHING. Checks 1 and 2 run
first because they are the only two that need an empty queue: "= 1" is a claim about a count, and
a count is only evidence while nothing else has filed anything. Check 3's poll then runs into a
queue that already holds The Bear, which is what makes its "each key once" a measurement rather
than an arithmetic identity -- the webhook's spelling of that key and the poll's are the same
spelling (`pipeline.key_for_item`) or they are not, and nothing else in this run can tell them
apart. Checks 6 and 7 move install state, so they run after every check that reads a count they
would disturb, and check 11 compares three snapshots taken around them.

IT REFUSES TO RUN ON THE FIXTURE (decision 371), for the reason `ops/m45_exit_criterion.py`,
`ops/m412_exit_criterion.py` and `ops/m51_exit_criterion.py` do, and the reason is specific to
what M5.2 claims. Checks 6, 7 and 11 are about `sync/seen._falsify_ownership`, which calls itself
"the most destructive statement in the module -- a bug here un-owns the household's whole library
and empties Tonight's pool". What check 6 asserts is not a count but the exact SET of titles that
stopped being owned when one film left the mirror, and a set is only a claim where there is
something outside the library for it to be wrong about. The double's library is seven items; the
corpus is 19,071 titles, of which the household owns a few hundred that this Jellyfin has never
named. On the fixture those two populations are the same population and the assertion is an
identity. Check 3 needs the corpus for the second reason M5.1's script does: `connectors/resolve`'s
last branch is a name-and-year match, and "the poll files one task per title" is only interesting
on a population where names collide (`resolve.py:189-194`).

NOTHING HERE NEEDS DOCKER, A PORT OR A BROWSER, and that is deliberate rather than lucky. §7.2's
claims are HTTP facts, so the app is driven over `httpx.ASGITransport` exactly as decision 255
asks and `ops/m414_exit_criterion.py` does, and Jellyfin is `ops/fake_jellyfin.py` mounted
in-process exactly as `ops/m411_exit_criterion.py` mounts it. The double is a REFUSER, not a mock:
its `ItemAdded` emitter builds and serialises the Webhook plugin's own template and POSTs it at
the route, and refuses to emit a burst this server could not really produce -- so every payload
below is the double's and none of it is this script's. A harness that wrote those bodies itself
would be proving the handler tolerates a shape nobody sends.

THE TEN MINUTES ARE MOVED, NOT WAITED OUT, and the clock that is not faked is the one that
matters. `not_before` is written by Postgres's `now()` in the same statement that writes
`received_at` (decision 363), so the window belongs to the database; `_age_the_window` below moves
those two columns backwards in that same database, which is the idiom
`backend/tests/test_jellyfin_intake.py:118-124` argues for at length. A Python clock patched
instead would be asserting against an instant the database never saw, and sitting out 600 real
seconds twice would add twenty minutes to a run to measure the same arithmetic.

It connects through `db/pool._init_connection` and never a bare `asyncpg.connect`: without the
json/jsonb codec `title_meta.payload` dies with "expected str, got dict", which is the defect
M4.5's harness spent three runs believing was in the importer (`M4.14-plan.md:911-912`).

It creates and drops its own DATABASE, so it never runs against a household's data by accident
-- 0003 creates schemas of its own, so a search_path would not have isolated it -- and it stages
into a temporary DATA_DIR it removes. Output is ASCII: a Windows console crashes on a decorative
glyph, and every string this script did not author goes through `console()` on the way out --
`acquire/intake.py`'s refusal reasons and the importer's findings both reach the console here.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m52_exit_criterion.py
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s reporting helpers can be imported rather than
# copied, which is what `ops/m51_exit_criterion.py` does and why: M4.5's recorded 18/18 has to
# stay reproducible unchanged, so that file is read and never edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. `setdefault` would read
# a developer's `.env` *after* this and pydantic-settings ranks the environment above that file,
# so these two are set outright: this script must never seal a household's real SECRETS_KEY into
# a scratch database, and a throwaway key is all the encryption here means. It matters twice over
# in this run, because decision 332's webhook token is sealed under that key and check 9's 401 is
# a claim about what the app could open.
os.environ["SESSION_SECRET"] = "m52-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m52-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan.acquire import intake, pipeline  # noqa: E402
from spielplan.connectors import registry  # noqa: E402
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.sync import seen  # noqa: E402

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "twelve ItemAdded for one series in one window are one task for the show"),
    (2, "the same burst, the worker restarted at minute five as a new sweep process, is still one task"),
    (3, "the webhook silent, one delta poll: the same key set, each key once"),
    (4, "a second poll with nothing added enqueues nothing"),
    (5, "a poll whose Jellyfin read failed leaves the watermark exactly where it was"),
    (6, "a title gone from the mirror is un-owned by the full sweep, and only it"),
    (7, "the same title re-added is owned again by the next full sweep"),
    (8, "an add in a library the admin did not pick is recorded and enqueues nothing"),
    (9, "a wrong token is 401, records nothing, and enqueues nothing"),
    (10, "a payload missing a required field is 202, recorded, and faults nothing"),
    (11, "across the whole run, only the full sweep ever wrote the ownership column"),
)

# The floor under which CORPUS_BUNDLE_DIR is the fixture and not the corpus. Read off
# BUNDLE.json's own `total_bytes` rather than off the directory, because a bundle that has been
# half-copied is exactly what this script must be able to tell apart from one that is small by
# design. `ops/m51_exit_criterion.py` uses the same number against the same file.
MIN_REAL_BYTES = 500_000_000

# The origin the stored connector names. Nothing resolves it: every read below travels over an
# `ASGITransport`, which never opens a socket, and a run in which the transport were somehow
# bypassed would fail with a DNS error rather than quietly reaching a real server on the
# household's own network. `ops/m411_exit_criterion.py` names the same host for the same reason.
JELLYFIN_URL = "http://jellyfin.test"
FAKE_CONTROL_URL = "http://fake-jellyfin"
# Where the double is told to push. The host is ignored by the transport and the path is the one
# §7.2 fixes, so this is the operator's "Webhook URL" field with nothing in it that can be wrong
# except the part the criterion is about.
WEBHOOK_TARGET = "http://spielplan.test/events/jellyfin"

ADMIN_NAME = "exit-criterion"
ADMIN_PASSWORD = "an-exit-criterion-password-9times"

# The Bear, which `ops/fake_jellyfin.py` gives twelve episodes precisely so §7.2's burst can be
# emitted in full ("library scans add seasons in bursts; series acquire per-show, not
# per-episode"). Severance keeps its two because `test_jellyfin_client.py` pins them.
BURST_SERIES = "jf-7"
BURST_EPISODES = 12

# Check 8's two items, and the two libraries that make the pick mean something. `jf-x` is the
# household's own footage in `jf-lib-home` -- it resolves to no title at all (§4.2) and is exactly
# the library nobody wants an acquisition job billed for -- while `jf-1` sits in the library that
# IS picked, so one sweep decides two events in opposite directions.
PICKED_LIBRARY = "jf-lib-films"
PICKED_ITEM = "jf-1"
UNPICKED_ITEM = "jf-x"

# The instant check 3 winds the watermark back to. Before every `DateLastSaved` the double carries
# (2019-03-14 is the oldest), so a poll from here reads the whole library -- which is what
# "the webhook plugin absent" means for a household whose adds all predate the app.
BEFORE_THE_LIBRARY = datetime(2019, 1, 1, tzinfo=UTC)

# Minutes the window is moved by, in two halves for check 2. Eleven rather than ten in checks 1, 8
# and 10, because `not_before` is `received_at + 10 minutes` to the microsecond and a move of
# exactly ten leaves the row ripe by whatever the statement's own round trip took.
WINDOW_MINUTES = 11
RESTART_AT_MINUTE = 5

# How long check 2 waits for the restarted worker. Generous, because the child pays torch's and
# numpy's import cost through `spielplan.acquire.pipeline` -> `stages` before it can sweep
# anything, and a deadline that fires is a run with no measurement rather than a failed one.
CHILD_DEADLINE_S = 300.0


# The worker that was not running when the burst arrived. Written into the scratch tree and run as
# a child of this script, so its traceback has a file to name -- `ops/m51_exit_criterion.py`'s
# `_DOOMED_WORKER` established the shape.
#
# IT IS A WHOLE OS PROCESS AND THAT IS THE ENTIRE POINT OF CHECK 2. Decision 363 files the pending
# set as a TABLE because §5.3 files every job as durable, and the failure it is written against is
# a debounce held in a worker's memory: a library scan that delivers a season across ten minutes
# would lose the whole burst to a restart inside them, and the household would never learn that it
# had. The only way to measure "it did not lose it" is to destroy every object that ever saw the
# twelve events -- the connection, the client, the interpreter -- and let something that has never
# heard of them find them and collapse them. A second `sweep_pending` call in the parent process
# would pass identically against an implementation that kept the set in a module-level dict.
#
# It mounts its own copy of the double, so the library it asks about is the server's own state
# rather than anything this script arranged, and it reads the library pick out of the database
# where §6.6 put it.
_RESTARTED_SWEEP = '''"""One worker, started after a burst it never received, sweeping the table.

Written and run by ops/m52_exit_criterion.py; see the `_RESTARTED_SWEEP` comment there for why
check 2 needs a process rather than a second call. Nothing outside that script should run this.
"""
import asyncio
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.environ["SPIELPLAN_BACKEND"])

import asyncpg
import httpx

from spielplan.acquire import intake
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.db import pool


def load_double():
    """`ops/fake_jellyfin.py` in this process, registered before it is executed the way `import`
    itself does: the module carries `from __future__ import annotations`, so Pydantic resolves its
    models through `sys.modules[cls.__module__]` and a missing entry surfaces as a
    `class-not-fully-defined` error naming a model rather than this function."""
    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", os.environ["SPIELPLAN_FAKE_JELLYFIN"]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main():
    conn = await asyncpg.connect(os.environ["SPIELPLAN_SCRATCH_DSN"])
    await pool._init_connection(conn)
    try:
        cfg = await registry.load_jellyfin(conn)
        client = JellyfinClient(
            cfg.url, cfg.api_key, transport=httpx.ASGITransport(app=load_double().app)
        )
        report = await intake.sweep_pending(conn, client, cfg)
    finally:
        await conn.close()
    print("RESTARTED-SWEEP " + json.dumps(report.as_dict()), flush=True)


asyncio.run(main())
'''


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


class LogWatch(logging.Handler):
    """Every WARNING-or-worse this app's own code emits during the run, kept for check 10.

    The criterion's phrase is "no 500 in the log", and half of that is literal here: every
    delivery below goes through `POST /events/jellyfin` over a transport built with
    `raise_app_exceptions=False`, so an unhandled exception in the handler arrives as the 500 a
    browser would get rather than as an exception in this process. The other half is the log,
    because a route can answer 202 and still have faulted on the way: `record_event` keeps the
    body whole with `default=str` precisely so that a field it cannot serialise costs a type
    rather than the row, and a failure there would be an ERROR record under a 202. That belief
    was too small until this milestone's review -- a dump is not the same as a STORABLE dump, and
    the three shapes `intake._storable` now refuses reached the INSERT and answered 500 with
    nothing recorded, which is the pair this handler exists to catch together.

    Attached to the `spielplan` logger rather than to the root, so a warning from asyncio or from
    asyncpg's own machinery is not read as the app faulting.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def faults(records: list[logging.LogRecord]) -> list[str]:
    """The records that say something broke, as lines. Warnings are reported and are not faults.

    A warning is not a fault here on purpose and the distinction is load-bearing for this
    milestone: `intake.sweep_pending` logs a WARNING when Jellyfin could not be read and leaves
    the rows pending, which is the degraded behaviour decision 364 asks for rather than a failure,
    and check 5 provokes exactly that shape one module over.
    """
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
    dsn: str
    log: LogWatch
    app: Any = None
    admin: httpx.AsyncClient | None = None
    jellyfin: Any = None
    transport: Any = None
    webhook_token: str = ""
    version: str = ""
    admin_user_id: int = 0
    # The stored connector as the app re-opened it, so every client this run builds is built from
    # a credential that went through `core.secrets` rather than from this script's own constants.
    cfg_url: str = ""
    cfg_key: str = field(default="", repr=False)
    # The double's library as it was mounted. Check 6 takes a film out of the mirror and check 7
    # puts it back, and "back" has to mean the rows the server started with -- re-executing the
    # module to get them would register a second `fake_jellyfin` in `sys.modules` under the name
    # the first one is still being served from.
    pristine_items: list[dict[str, Any]] = field(default_factory=list)
    # Every HTTP answer this run saw, from the admin's own requests and from the deliveries the
    # double pushed. Check 10's "no 500" half reads it, and it is recorded at the point each
    # answer arrives rather than reconstructed, because a status nobody wrote down is a status
    # this script would be asserting about from memory.
    statuses: list[tuple[str, int]] = field(default_factory=list)
    # The three ownership snapshots check 11 compares. `born` is taken before any check runs;
    # `after_delta` after check 5, which closes the window containing every delta-path and
    # webhook-path check that precedes the sweeps; `after_sweeps` after check 7.
    born: dict[int, tuple[bool, Any]] = field(default_factory=dict)
    after_delta: dict[int, tuple[bool, Any]] = field(default_factory=dict)
    after_sweeps: dict[int, tuple[bool, Any]] = field(default_factory=dict)
    # What check 6 chose to remove and what the sweeps did, so check 7 and check 11 measure the
    # same subject rather than each picking one.
    removed_item: str = ""
    removed_title: int = 0
    sweep_moved: list[int] = field(default_factory=list)


# --- reading the install ----------------------------------------------------------------------


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

    `backend/tests/conftest.py` records the incident this repeats: the genuine lifespan calls
    `registry.seed_from_env`, so a household that followed its own README boots this scratch
    install with their real Jellyfin configured -- and their encrypted credentials are then
    written into a throwaway database under a throwaway key. Derived from the connectors
    `registry.CONNECTORS` seeds, so a connector added there is neutralised here without anyone
    remembering this list. It used to say "derived from `Settings`" over four M0-era prefixes, and
    the three provider keys M5.5 seeds (§2, M5.5 plan A3) passed straight through it into the
    lifespan's `registry.seed_from_env`. [M5.5 review cycle 1, KEYS-C1-03, M55-DOC-07]

    It matters more in this script than in its siblings. A seeded JELLYFIN_URL would point every
    read below at the household's real server, and the first thing check 6 does to a real server's
    mirror is take a film out of it.
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


def _load_double() -> Any:
    """`ops/fake_jellyfin.py` mounted in-process, exactly as `tests/conftest.py` mounts it.

    Registered in `sys.modules` before it is executed, which is what `import` itself does: the
    module carries `from __future__ import annotations`, so Pydantic resolves its models'
    annotations through `sys.modules[cls.__module__]` and a missing entry surfaces as a
    `class-not-fully-defined` error naming a model rather than this function.

    A real HTTP server over ASGI rather than a mock, because §7.2's claims are HTTP facts -- and
    because this fake is a refuser: its emitter 409s a burst of twelve against a series it holds
    two episodes of, which is the only thing that turns "twelve events arrived" from a sentence in
    this script into something the double had to be able to produce.
    """
    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", ROOT / "ops" / "fake_jellyfin.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


async def _emit(ctx: Install, **body: Any) -> dict[str, Any]:
    """Fire the Webhook plugin at the app through the double's own control surface.

    THE PAYLOAD IS THE DOUBLE'S AND NEVER THIS SCRIPT'S. `ops/fake_jellyfin.py:_item_added`
    renders the template decision 365 publishes as `ops/jellyfin-webhook-template.json`: the
    plugin flattens every item's provider ids into `Provider_<lowercase>` keys, an Episode's own
    included, and sends an episode's series identity beside them (M52-C4-TTA-04). A criterion
    that wrote those bodies out by hand would be proving the handler tolerates a shape nobody
    sends, which is the one thing a double exists to prevent.

    A refusal from the emitter is a PRECONDITION and not a verdict: 404 for an item this server
    does not hold, 409 for a burst larger than the season it has, 409 for a field the template
    never carries. Every one of those means the scenario could not be set up, so reporting it as
    a failed check would blame the app for the harness.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ctx.jellyfin.app), base_url=FAKE_CONTROL_URL
    ) as control:
        answer = await control.post("/_test/item-added", json=body)
    if answer.status_code != 200:
        raise PreconditionFailed(
            f"the double refused to emit {body!r}: {answer.status_code} "
            f"{console(answer.text[:200])}"
        )
    sent = answer.json()
    label = f"POST /events/jellyfin ({body.get('item_id', '?')})"
    ctx.statuses.extend((label, int(status)) for status in sent["statuses"])
    return sent


async def _admin(ctx: Install, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One admin request, with its answer recorded for check 10's "no 500" half."""
    if ctx.admin is None:
        raise PreconditionFailed("this run has no admin session")
    answer = await ctx.admin.request(method, path, **kwargs)
    ctx.statuses.append((f"{method} {path}", answer.status_code))
    return answer


def _client(ctx: Install) -> JellyfinClient:
    """A client for the stored connector, pointed at the double with no socket in between.

    Built from `cfg.url` and `cfg.api_key` as the app stored and re-opened them, rather than from
    this script's own constants: the admin key travels through `core.secrets` on the way in and
    back out, and a harness that used its own copy would measure a connector whose credential had
    never been sealed. `registry.make_client` is the app's one construction site and its docstring
    says why it is one -- "so an integration test can point the whole app at `ops/fake_jellyfin.py`
    by replacing this single function" -- which is exactly what `build_install` does to it below;
    the difference here is only that the transport is threaded explicitly.
    """
    return JellyfinClient(ctx.cfg_url, ctx.cfg_key, transport=ctx.transport)


def _unreachable(ctx: Install) -> JellyfinClient:
    """A client whose every read fails: check 5's dead origin, without a dead origin.

    `_request` wraps an httpx transport error in `JellyfinError` (§3.3), so this is the shape a
    Jellyfin outage has from inside this app rather than an exception type invented here -- and it
    is the shape the reachable alternative would produce anyway. Pointing the stored connector at
    a port nothing listens on would measure the same code path minutes later, through a connect
    timeout, and would leave a run that was interrupted between check 5 and its repair with a
    scratch install configured against an origin that does not exist.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the household's Jellyfin is not answering")

    return JellyfinClient(ctx.cfg_url, ctx.cfg_key, transport=httpx.MockTransport(refuse))


async def _age_the_window(ctx: Install, minutes: int) -> None:
    """Move every intake row back in time, which is the only honest way to watch a fixed window
    close.

    The clock is Postgres's: `record_event` writes `received_at` from `now()` and `not_before`
    from the same `now()` in the same statement (decision 363), so a harness that patched a Python
    clock would be asserting against an instant the database never saw, and `sweep_pending` reads
    its own `now()` back out of the same server. Moving the two columns together keeps the row's
    arithmetic exactly as it was written; what changes is only how long ago it was written.
    `backend/tests/test_jellyfin_intake.py`'s `_age_the_window` is the same statement.
    """
    await ctx.conn.execute(
        "UPDATE jellyfin_intake SET received_at = received_at - make_interval(mins => $1),"
        "       not_before = not_before - make_interval(mins => $1)", minutes,
    )


async def _task_keys(ctx: Install) -> list[str]:
    """Every acquisition task this install holds, by key. The criterion's own count."""
    return [
        row["key"] for row in await ctx.conn.fetch(
            "SELECT key FROM acquisition_task WHERE kind = $1 ORDER BY key", pipeline.TASK_KIND
        )
    ]


async def _intake_rows(ctx: Install) -> list[dict[str, Any]]:
    return [
        dict(row) for row in await ctx.conn.fetch(
            "SELECT id, item_id, item_type, resolved_key, state, reason"
            "  FROM jellyfin_intake ORDER BY id"
        )
    ]


async def _ownership(ctx: Install) -> dict[int, tuple[bool, Any]]:
    """Every title's ownership, as the two columns that can record a write to it.

    `owned_checked_at` as well as `is_owned`, because a sweep that re-asserted ownership it
    already held would move only the second -- and decision 362's claim is that the delta path
    writes the COLUMN, not that it changes anybody's mind. A diff over the flag alone would pass
    against a delta poll that stamped every title in the library on every tick.
    """
    return {
        int(row["id"]): (bool(row["is_owned"]), row["owned_checked_at"])
        for row in await ctx.conn.fetch("SELECT id, is_owned, owned_checked_at FROM title")
    }


def _ownership_diff(
    before: dict[int, tuple[bool, Any]], after: dict[int, tuple[bool, Any]]
) -> list[int]:
    return sorted(
        title_id for title_id in set(before) | set(after)
        if before.get(title_id) != after.get(title_id)
    )


async def _owned_ids(ctx: Install) -> set[int]:
    """Every title the household is currently recorded as owning.

    A set and not a count, because checks 6 and 7 are about which titles moved: `unowned = 1` is
    also what a sweep that dropped one title and picked up another would report, and the whole
    anxiety about `_falsify_ownership` is that it can be wrong about a population rather than
    about a number.
    """
    return {
        int(row["id"]) for row in await ctx.conn.fetch("SELECT id FROM title WHERE is_owned")
    }


async def _never_named(ctx: Install) -> set[int]:
    """The titles this household owns that its Jellyfin has never named, as a set of ids.

    The population `_falsify_ownership`'s `jellyfin_id IS NOT NULL` clause exists to protect, and
    the one the corpus supplies and no fixture does. It is read BEFORE the first sweep and checked
    after the last, because the damage a missing clause does is done on the FIRST sweep that runs
    against a mirror this corpus was not exported from -- an instrument that only diffed across
    the deliberate removal would watch the wrong minute and report a clean run, which is what this
    script did until the mutation that deleted the clause passed eleven checks out of eleven.

    How big the population is on any given bundle is that bundle's business: the corpus exports
    `is_owned` and may or may not export a `jellyfin_id` beside it (`importer/load.py:123` maps
    both). Which is why check 6 asserts over the SET rather than over its size -- a count would
    have to be predicted, and a set only has to survive.
    """
    return {
        int(row["id"]) for row in await ctx.conn.fetch(
            "SELECT id FROM title WHERE is_owned AND jellyfin_id IS NULL"
        )
    }


async def build_install(ctx: Install, bundle_root: Path) -> None:
    """Import the corpus, boot the app, configure the connector, link a member. Raises on refusal.

    Everything the eleven checks need and nothing they do not. The connector is configured through
    §6.6's own PUT rather than by calling `registry.save_jellyfin` from Python, because that route
    is what MINTS the webhook token (decision 332) and shows it exactly once -- so the token this
    script presents in checks 1, 2, 8 and 10 is the one an operator would have pasted into the
    plugin, and the 401 in check 9 is a claim about that same secret rather than about a value the
    harness chose. The member is linked through §7.3's own route for the same reason: it is what
    obtains the per-user token `seen.sync_all` needs, and checks 6 and 7 are sweeps.
    """
    bundle = bundle_import.Bundle.open(bundle_root)
    began = time.perf_counter()
    report = await bundle_import.import_bundle(ctx.conn, bundle, ctx.work / "artifacts")
    fails = [f"{f.rule}: {f.message[:200]}" for f in report.findings if f.severity == "fail"]
    if not report.ok or fails:
        raise PreconditionFailed(
            "the bundle did not import, so there is no install to measure: " + "; ".join(fails[:3])
        )
    version = await ctx.conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    if not version:
        raise PreconditionFailed("the import left no active artifact_bundle row")
    ctx.version = str(version)
    titles = await ctx.conn.fetchval("SELECT count(*) FROM title")
    print(
        f"  imported {console(ctx.version)} in {time.perf_counter() - began:.0f}s: "
        f"{titles:,} titles, {len(await _never_named(ctx)):,} of them owned with no Jellyfin id",
        flush=True,
    )

    ctx.jellyfin = _load_double()
    ctx.pristine_items = copy.deepcopy(ctx.jellyfin.ITEMS)
    ctx.transport = httpx.ASGITransport(app=ctx.jellyfin.app)
    # The app's one construction site, replaced rather than threaded: `make_client`'s own
    # docstring says this is what it is for. Every caller this run reaches through a route -- the
    # PUT's version probe, the link's `authenticate_by_name` -- then talks to the double, with no
    # parameter added to any signature to let a harness in.
    registry.make_client = lambda cfg: (
        JellyfinClient(
            cfg.url, cfg.api_key, transport=ctx.transport,
            server_version=cfg.server_version, server_supported=cfg.server_supported,
        ) if cfg.configured else None
    )

    from spielplan.app import create_app

    ctx.app = create_app()
    ctx.admin = httpx.AsyncClient(
        # `raise_app_exceptions=False` on purpose, and it is the only reason check 10 can say
        # anything: with the default, an unhandled exception in a handler is re-raised into the
        # caller and this script would report "the check stopped on InvalidTag" where the
        # criterion's sentence is "no 500 in the log". A browser gets the 500 Starlette's
        # ServerErrorMiddleware has already written, so that is what this client must get too.
        transport=httpx.ASGITransport(app=ctx.app, raise_app_exceptions=False),
        base_url="http://test",
        timeout=60.0,
    )


async def configure(ctx: Install) -> None:
    """The four gestures an operator makes, in the order §6.6 offers them."""
    created = await _admin(
        ctx, "POST", "/api/setup/admin", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD}
    )
    if created.status_code != 201:
        raise PreconditionFailed(f"no admin account: {console(created.text[:300])}")
    signed_in = await _admin(
        ctx, "POST", "/api/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD}
    )
    if signed_in.status_code != 200:
        raise PreconditionFailed(f"the admin could not sign in: {signed_in.status_code}")

    saved = await _admin(
        ctx, "PUT", "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": ctx.jellyfin.API_KEY, "mint_webhook_token": True},
    )
    if saved.status_code != 200:
        raise PreconditionFailed(f"the connector was refused: {console(saved.text[:300])}")
    # Decision 332 shows the token ONCE, on the save that minted it, and this is that save -- one
    # that ASKS for it, because a Save that does not ask mints nothing (decision 418). Taken
    # out of the answer rather than out of the database on purpose: the operator's only copy is
    # this response, and a criterion that read the column instead would pass on an install whose
    # admin was never shown anything to paste.
    ctx.webhook_token = str(saved.json().get("webhook_token") or "")
    if not ctx.webhook_token:
        raise PreconditionFailed(
            "the save that configures the connector showed no webhook token, so there is nothing "
            "an operator could have pasted into the plugin"
        )

    ctx.admin_user_id = int(await ctx.conn.fetchval(
        "SELECT id FROM app_user WHERE name = $1", ADMIN_NAME
    ))
    linked = await _admin(
        ctx, "POST", f"/api/admin/users/{ctx.admin_user_id}/jellyfin",
        json={
            "jellyfin_user_id": "jf-user-patrick",
            "jellyfin_username": "patrick",
            "jellyfin_password": ctx.jellyfin.PASSWORD,
        },
    )
    if linked.status_code != 200:
        raise PreconditionFailed(f"the member was not linked: {console(linked.text[:300])}")

    cfg = await registry.load_jellyfin(ctx.conn)
    if not cfg.configured or cfg.secrets_unreadable:
        raise PreconditionFailed("the stored connector will not open under this run's key")
    ctx.cfg_url, ctx.cfg_key = cfg.url, cfg.api_key
    if cfg.library_ids:
        raise PreconditionFailed(
            f"this install starts with a library pick of {cfg.library_ids}, and checks 1 to 7 "
            "measure decision 364's empty pick -- the state of every install in existence"
        )

    # Where the plugin pushes, and what it presents. `raise_app_exceptions=False` here as well:
    # the deliveries in checks 1, 2, 8, 9 and 10 travel through this transport and not through
    # `ctx.admin`, so a handler that faulted would otherwise raise inside the DOUBLE and come back
    # as one of its own 500s, which is a status about the wrong server entirely.
    ctx.jellyfin.WEBHOOK_TRANSPORT = httpx.ASGITransport(
        app=ctx.app, raise_app_exceptions=False
    )
    ctx.jellyfin.WEBHOOK_URL = WEBHOOK_TARGET
    ctx.jellyfin.WEBHOOK_TOKEN = ctx.webhook_token
    print(
        f"  admin {ctx.admin_user_id} linked to patrick; the plugin pushes at "
        f"{console(WEBHOOK_TARGET)} under {console(ctx.jellyfin.WEBHOOK_TOKEN_HEADER)}",
        flush=True,
    )


# --- 1 and 2: the burst, and the burst across a restart -----------------------------------------


async def _deliver_the_burst(ctx: Install) -> int:
    """Twelve `ItemAdded` for twelve episodes of one series, and what the app recorded.

    Returns the number of rows left pending, which is the half of the claim that is about the
    handler: decision 369 keys every one of them on the `SeriesId` rather than on the episode, so
    twelve rows carrying twelve distinct `item_id`s share one `resolved_key`. The sweep's collapse
    below is only interesting because the handler did NOT collapse anything.
    """
    sent = await _emit(ctx, item_id=BURST_SERIES, episodes=BURST_EPISODES)
    if int(sent["sent"]) != BURST_EPISODES or set(sent["statuses"]) != {202}:
        raise PreconditionFailed(
            f"the plugin delivered {sent['sent']} event(s) answered {sorted(set(sent['statuses']))}"
        )
    rows = await _intake_rows(ctx)
    pending = [row for row in rows if row["state"] == intake.PENDING]
    keys = {row["resolved_key"] for row in pending}
    items = {row["item_id"] for row in pending}
    if keys != {BURST_SERIES} or len(items) != BURST_EPISODES:
        raise PreconditionFailed(
            f"the twelve deliveries were recorded under keys {sorted(keys)} across "
            f"{len(items)} item id(s), which is not one season of one show"
        )
    return len(pending)


async def check_one(ctx: Install) -> tuple[bool, str]:
    """Plan check 1: twelve `ItemAdded` for one series inside one window, one acquisition task.

    §7.2's clause is two rules welded into one sentence -- "Debounce 10 min; series acquire
    per-show, not per-episode" -- and this measures both halves at once, because they fail
    differently. Keyed per-episode, the twelve collapse into nothing and the queue holds twelve
    tasks; debounced per-episode but keyed per-show, it holds one but only because
    `UNIQUE (kind, key)` swallowed eleven duplicates, and the board would carry the same work
    twelve times over the life of an install with a slower queue. So the verdict is the key set
    AND the sweep's own report of how many keys it found ripe: one ripe key, one task created,
    and not one task in the table keyed on an episode id.

    THE QUEUE IS ASSERTED EMPTY FIRST. The plan's measure is a count, and `count(*) = 1` on a
    table that already held the row is a certificate rather than a measurement -- which is exactly
    the shape check 3 then has to work around, deliberately, one check later.
    """
    before = await _task_keys(ctx)
    if before:
        raise PreconditionFailed(f"the queue is not empty before the burst: {before}")
    pending = await _deliver_the_burst(ctx)

    await _age_the_window(ctx, WINDOW_MINUTES)
    cfg = await registry.load_jellyfin(ctx.conn)
    report = await intake.sweep_pending(ctx.conn, _client(ctx), cfg)
    keys = await _task_keys(ctx)
    episode_keys = [key for key in keys if key.startswith(f"jellyfin:{BURST_SERIES}-e")]
    ok = (
        keys == [f"jellyfin:{BURST_SERIES}"]
        and report.ripe == 1
        and report.enqueued == 1
        and not episode_keys
        and pending == BURST_EPISODES
    )
    return ok, (
        f"{pending} event(s) recorded under one key; the sweep found {report.ripe} ripe key(s) "
        f"and created {report.enqueued} task(s); the queue holds {keys}"
    )


async def check_two(ctx: Install) -> tuple[bool, str]:
    """Plan check 2: the same burst, with the worker restarted at minute five. Still one task.

    THE SCENARIO IS RE-ARMED AND THAT IS NOT OPTIONAL. Check 1's task is still in the queue, and
    `queue.enqueue` is `ON CONFLICT (kind, key) DO NOTHING`, so a restarted worker that collapsed
    nothing at all would leave exactly the same single row behind. Clearing the task and the
    intake rows is what makes "still one" a statement about this burst rather than about the
    previous one, and the clear is asserted rather than assumed.

    THE RESTART IS A PROCESS. Decision 363 files the pending set as a table because §5.3 files
    every job as durable; a debounce held in a worker's memory would lose a season to a restart
    inside the ten minutes and nobody would ever learn that it had. So the middle of this check is
    a sweep at minute five that must find nothing and leave all twelve rows pending -- the worker
    doing its last honest tick before it dies -- and the end of it is `_RESTARTED_SWEEP` in a
    brand-new interpreter, with no object in it that ever saw an `ItemAdded`, reading the twelve
    out of the table and filing one task.
    """
    await ctx.conn.execute("DELETE FROM acquisition_task WHERE kind = $1", pipeline.TASK_KIND)
    await ctx.conn.execute("DELETE FROM jellyfin_intake")
    if await _task_keys(ctx) or await _intake_rows(ctx):
        raise PreconditionFailed("the scenario could not be re-armed: the run's own state remains")
    pending = await _deliver_the_burst(ctx)

    await _age_the_window(ctx, RESTART_AT_MINUTE)
    cfg = await registry.load_jellyfin(ctx.conn)
    early = await intake.sweep_pending(ctx.conn, _client(ctx), cfg)
    still_pending = len([row for row in await _intake_rows(ctx) if row["state"] == intake.PENDING])

    await _age_the_window(ctx, WINDOW_MINUTES - RESTART_AT_MINUTE)
    restarted = await _run_restarted_worker(ctx)
    keys = await _task_keys(ctx)
    ok = (
        keys == [f"jellyfin:{BURST_SERIES}"]
        and early.ripe == 0
        and early.enqueued == 0
        and still_pending == BURST_EPISODES
        and int(restarted.get("enqueued") or 0) == 1
        and pending == BURST_EPISODES
    )
    return ok, (
        f"at minute {RESTART_AT_MINUTE} the sweep found {early.ripe} ripe key(s) and left "
        f"{still_pending} row(s) pending; a restarted worker then reported {restarted} and the "
        f"queue holds {keys}"
    )


async def _run_restarted_worker(ctx: Install) -> dict[str, Any]:
    """Run `_RESTARTED_SWEEP` as a child and hand back the report it printed.

    A refused child is a precondition and not a verdict: what check 2 measures is what a restarted
    worker does with the table, and a process that could not start measured nothing about it. The
    report is parsed off a tagged line rather than off the whole of stdout, because the child pays
    torch's import cost on the way in and that import writes to stderr and, on some builds, to
    stdout.
    """
    script = ctx.work / "restarted_worker.py"
    script.write_text(_RESTARTED_SWEEP, encoding="ascii")
    env = dict(os.environ)
    env.update({
        "SPIELPLAN_BACKEND": str(ROOT / "backend"),
        "SPIELPLAN_SCRATCH_DSN": ctx.dsn,
        "SPIELPLAN_FAKE_JELLYFIN": str(ROOT / "ops" / "fake_jellyfin.py"),
    })
    finished = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, timeout=CHILD_DEADLINE_S,
    )
    out = finished.stdout.decode("utf-8", "replace")
    line = next(
        (row for row in out.splitlines() if row.startswith("RESTARTED-SWEEP ")), None
    )
    if line is None:
        raise PreconditionFailed(
            f"the restarted worker exited {finished.returncode} without reporting a sweep: "
            f"{console(finished.stderr.decode('utf-8', 'replace')[-400:])}"
        )
    return json.loads(line[len("RESTARTED-SWEEP "):])


# --- 3, 4 and 5: the fallback -------------------------------------------------------------------


async def check_three(ctx: Install) -> tuple[bool, str]:
    """Plan check 3: the webhook silent, one delta poll, the same key set and each key once.

    TWO HALVES, AND THE FIRST IS DECISION 366'S OWN GUARANTEE, AS DECISION 412 PLACES IT. A
    never-polled watermark is 0025's own `applied_at` -- the instant this install gained the
    fallback, which on this scratch database is one migration run from its own creation -- and
    every item the household's server holds was added before that, so the first poll on a real
    install reads NOTHING. That is the whole of what stands between an operator's first boot
    and an acquisition task for every title they own, and a watermark initialised to epoch would
    pass every other assertion in this script. It is measured here rather than assumed because it
    is also the precondition for the second half: the poll only has a library to read once the
    watermark has been wound back behind it.

    THE SECOND HALF IS "EACH ONCE" AND IT IS NOT ARITHMETIC. The queue already holds The Bear,
    filed by the webhook path in check 2. A poll that spelled its key any other way would file a
    second task for one show -- which is precisely what `pipeline.key_for_item` exists to prevent
    and what a "fallback" that duplicated the trigger would be worth nothing as. So the verdict is
    that the task table ends up holding exactly the server's own item set, one row each, with the
    poll reporting the overlap as already queued rather than as new.

    The intake table is emptied first and asserted empty: with the webhook plugin absent there are
    no deliveries, and a pending row left over from check 2 could be swept into a task that this
    check would then read as the poll's work.
    """
    await ctx.conn.execute("DELETE FROM jellyfin_intake")
    if await _intake_rows(ctx):
        raise PreconditionFailed("the intake table would not clear, so the webhook is not silent")

    cfg = await registry.load_jellyfin(ctx.conn)
    floor = await registry.delta_since(ctx.conn, cfg)
    gained = await ctx.conn.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )
    first = await intake.poll_delta(ctx.conn, _client(ctx), cfg)

    await registry.save_jellyfin(ctx.conn, delta_watermark=BEFORE_THE_LIBRARY)
    cfg = await registry.load_jellyfin(ctx.conn)
    report = await intake.poll_delta(ctx.conn, _client(ctx), cfg)
    keys = await _task_keys(ctx)
    expected = sorted(f"jellyfin:{item['Id']}" for item in ctx.jellyfin.ITEMS)
    ok = (
        floor == gained
        and first.read == 0
        and first.enqueued == 0
        and sorted(keys) == expected
        and report.enqueued == len(expected) - 1
        and report.already_queued == 1
    )
    return ok, (
        f"the install's own floor is {console(str(floor))} and a poll from it read "
        f"{first.read} item(s); from before the library the poll read {report.read}, created "
        f"{report.enqueued} task(s) and found {report.already_queued} already queued; the queue "
        f"holds {len(keys)} key(s)"
    )


async def check_four(ctx: Install) -> tuple[bool, str]:
    """Plan check 4: a second poll with nothing added enqueues nothing.

    §7.2's fallback runs every fifteen minutes for the life of an install, so "does not re-enqueue
    what it already saw" is the difference between a quiet background job and a household billed
    for its whole library ninety-six times a day. TWO MECHANISMS CARRY IT and only one of them is
    this poll's: `UNIQUE (kind, key)` absorbs the overlap whatever the poll does, and the watermark
    is what stops the poll reading the library at all. They are measured apart here -- `read == 0`
    is the watermark's half, `enqueued == 0` beside an unchanged key set is the queue's -- because
    a milestone that shipped only the second would have a poll re-reading eleven thousand titles
    every quarter of an hour while the queue stayed correct and nothing ever said so.
    """
    before = await _task_keys(ctx)
    cfg = await registry.load_jellyfin(ctx.conn)
    report = await intake.poll_delta(ctx.conn, _client(ctx), cfg)
    after = await _task_keys(ctx)
    ok = report.read == 0 and report.enqueued == 0 and after == before
    return ok, (
        f"the second poll read {report.read} item(s) and created {report.enqueued} task(s); "
        f"the queue holds {len(after)} key(s), as it did before"
    )


async def check_five(ctx: Install) -> tuple[bool, str]:
    """Plan check 5: a poll whose Jellyfin read failed leaves the watermark exactly where it was.

    THE ASYMMETRY IS THE WHOLE DECISION. An advanced watermark over a read that never happened is
    an add nobody will ever look for again -- the poll has promised to have seen instants it did
    not see, and no later tick asks about them; a watermark left where it was costs one re-read
    the queue's `(kind, key)` identity absorbs for free. Decision 366 therefore advances it ONLY
    on a completed read, and `items_created_since` raises rather than returning short so that "the
    read completed" is simply "it returned".

    THE RAISE IS PART OF THE VERDICT, not something this check tolerates. `poll_delta` lets a
    `JellyfinError` escape on purpose so that `worker.py`'s job runner records the failure; a poll
    that swallowed it would make a Jellyfin outage look exactly like a healthy quiet household,
    which is M4.11 finding 3's shape one connector over. So a run in which nothing was raised
    fails here even if the watermark did not move.
    """
    before = (await registry.load_jellyfin(ctx.conn)).delta_watermark
    keys_before = await _task_keys(ctx)
    cfg = await registry.load_jellyfin(ctx.conn)
    raised = ""
    try:
        await intake.poll_delta(ctx.conn, _unreachable(ctx), cfg)
    except JellyfinError as exc:
        raised = str(exc)
    after = (await registry.load_jellyfin(ctx.conn)).delta_watermark
    keys_after = await _task_keys(ctx)
    ok = bool(raised) and after == before and keys_after == keys_before
    return ok, (
        f"the failed read reported {console(raised[:120]) or 'NOTHING AT ALL'}; the watermark "
        f"reads {console(str(after))} and read {console(str(before))} before it"
    )


# --- 6 and 7: ownership, which only the full sweep writes ---------------------------------------


async def _sweep(ctx: Install) -> seen.SyncReport:
    """One §7.3 sweep through `seen.sync_all`, refusing to report a sweep that did not run.

    `sync_all` returns rather than raising when nobody is linked or when the library read failed,
    and both of those answers look from the outside exactly like a sweep that ran and found
    nothing to un-own -- which is the one confusion checks 6, 7 and 11 cannot survive, because
    `_falsify_ownership`'s own gate is "this sweep resolved no titles at all".
    """
    report = await seen.sync_all(ctx.conn, _client(ctx))
    if report.skipped_no_link or report.already_running or report.failed_users:
        raise PreconditionFailed(
            f"the sweep did not run: skipped={report.skipped_no_link} "
            f"locked={report.already_running} failed={report.failed_users}"
        )
    if not report.resolve.get("matched"):
        raise PreconditionFailed("the sweep resolved no titles, so it falsifies nothing by design")
    return report


async def check_six(ctx: Install) -> tuple[bool, str]:
    """Plan check 6: a title gone from the mirror is un-owned by the FULL SWEEP -- and only it.

    TWO CLAIMS, AND THE SECOND IS THE ONE NO FIXTURE CAN POSE. The first is §7.2's third bullet:
    the film the household deleted stops being owned, with a fresh `owned_checked_at`, so it
    leaves §6.2's candidate pool and stops deep-linking to an item id the server no longer has.
    The second is that the statement stopped there. `sync/seen._falsify_ownership` calls itself
    "the most destructive statement in the module -- a bug here un-owns the household's whole
    library and empties Tonight's pool", so what is asserted is not a COUNT but the exact SET of
    titles that stopped being owned across the removal, which must be the one film. A count of 1
    is satisfied by a falsifier that un-owned one title and re-owned another in the same sweep; a
    set is not, and on a fixture of seven titles the two are indistinguishable because there is
    nothing else in the library for a set to be wrong about.

    TWO SWEEPS BEFORE ANYTHING IS REMOVED, and the second is the measurement. The FIRST sweep on a
    freshly imported bundle legitimately un-owns whatever the corpus shipped `jellyfin_id` values
    for, because those ids belong to the household that built the export and not to this one --
    that is "re-derived from Jellyfin, never trusted stale" doing exactly its job. The SECOND runs
    against a mirror that did not change, and it must un-own NOTHING: a falsifier that fires
    unconditionally is caught there rather than read as a correct removal one paragraph later, and
    that reading holds whatever the corpus ships.

    The subject is chosen from what actually resolved rather than named as a constant. §7.1's
    resolver is fill-never-clobber and ends in a name-and-year branch, so which of the double's
    seven items reaches a real corpus title is a fact about v20260828 and not about this script --
    and a hard-coded id that failed to resolve would make this check fail for the wrong reason.

    THE THIRD CLAUSE IS READ FROM BEFORE THE FIRST SWEEP, and it is the one this check shipped
    without. `jellyfin_id IS NOT NULL` is what keeps the statement off the titles the corpus owns
    that this Jellyfin has never named, and a falsifier missing that clause un-owns all of them on
    the FIRST sweep -- minutes before any removal, and outside a window that only spans the
    removal. Measured against this script by deleting the clause: eleven checks out of eleven
    passed. So `_never_named` is read before anything sweeps, and every title in it must still be
    owned at the end, excepting only the one film this check deliberately deleted -- which may be
    one of them, if the mirror happened to hold it.
    """
    never_named = await _never_named(ctx)
    await _sweep(ctx)
    steady = await _sweep(ctx)
    owned_before = await _owned_ids(ctx)
    candidates = await ctx.conn.fetch(
        "SELECT id, jellyfin_id FROM title WHERE is_owned AND jellyfin_id IS NOT NULL ORDER BY id"
    )
    if not candidates:
        raise PreconditionFailed(
            "no title is owned through a Jellyfin id after two sweeps, so there is nothing whose "
            "removal from the mirror could be measured"
        )
    ctx.removed_title = int(candidates[0]["id"])
    ctx.removed_item = str(candidates[0]["jellyfin_id"])

    ctx.jellyfin.ITEMS = [
        item for item in copy.deepcopy(ctx.pristine_items) if str(item["Id"]) != ctx.removed_item
    ]
    shrunk = await _sweep(ctx)
    row = await ctx.conn.fetchrow(
        "SELECT is_owned, owned_checked_at FROM title WHERE id = $1", ctx.removed_title
    )
    owned_after = await _owned_ids(ctx)
    lost = owned_before - owned_after
    abandoned = never_named - owned_after - {ctx.removed_title}
    ok = (
        steady.unowned == 0
        and shrunk.unowned == 1
        and lost == {ctx.removed_title}
        and not abandoned
        and row is not None
        and not row["is_owned"]
        and row["owned_checked_at"] is not None
    )
    return ok, (
        f"a sweep of the unchanged mirror un-owned {steady.unowned}; removing "
        f"{console(ctx.removed_item)} un-owned {shrunk.unowned}, and the titles that stopped "
        f"being owned are {sorted(lost)[:8]} against a household of {len(owned_before):,}; title "
        f"{ctx.removed_title} reads is_owned={row['is_owned'] if row else None} checked "
        f"{console(str(row['owned_checked_at'] if row else None))}; of the {len(never_named):,} "
        f"owned titles this Jellyfin never named, {len(abandoned)} were abandoned "
        f"{sorted(abandoned)[:8]}"
    )


async def check_seven(ctx: Install) -> tuple[bool, str]:
    """Plan check 7: the title re-added is owned again by the next full sweep.

    §7.2's "re-derived from Jellyfin, never trusted stale" is a claim in both directions, and only
    this one is about `resolve.upsert_item`'s `is_owned = true`. Without it a household that moved
    a film between libraries, or restored it from a backup, would have a row the app had decided
    was gone and no gesture anywhere that could change its mind -- ownership would be a one-way
    door with the sweep's own removal on the far side of it.

    The subject is check 6's, deliberately: re-adding some other title would prove that the sweep
    can own things, which check 6's first sweep already proved, rather than that THIS one came
    back. And the set is asserted in this direction too -- exactly one title regained, and the
    sweep un-owning nothing on the way -- because a re-derivation that owned the whole mirror back
    would satisfy "this title is owned again" while having stopped reading the library at all.
    """
    if not ctx.removed_item:
        raise PreconditionFailed("check 6 did not get as far as removing a title")
    before = await _owned_ids(ctx)
    ctx.jellyfin.ITEMS = copy.deepcopy(ctx.pristine_items)
    restored = await _sweep(ctx)
    row = await ctx.conn.fetchrow(
        "SELECT is_owned, owned_checked_at, jellyfin_id FROM title WHERE id = $1",
        ctx.removed_title,
    )
    regained = await _owned_ids(ctx) - before
    ok = (
        row is not None
        and bool(row["is_owned"])
        and str(row["jellyfin_id"]) == ctx.removed_item
        and restored.unowned == 0
        and regained == {ctx.removed_title}
    )
    return ok, (
        f"{console(ctx.removed_item)} is back in the mirror; the sweep un-owned {restored.unowned}"
        f", the titles that became owned again are {sorted(regained)[:8]}, and title "
        f"{ctx.removed_title} reads is_owned={row['is_owned'] if row else None} on "
        f"{console(str(row['jellyfin_id'] if row else None))}"
    )


# --- 8, 9 and 10: the boundary, the credential and the operator's template -----------------------


async def check_eight(ctx: Install) -> tuple[bool, str]:
    """Plan check 8: an add in a library the admin did not pick is recorded and enqueues nothing.

    "ZERO TASKS" IS NOT MEASURED AS A COUNT HERE, AND IT CANNOT BE. Every one of the double's
    seven items is already in the queue -- check 3's poll put them there while the pick was empty
    and the whole server counted -- so `enqueue_item` would answer False for any of them through
    `ON CONFLICT DO NOTHING`, and a check whose verdict was "no new rows appeared" would pass
    against a sweep with the library filter deleted. What falsifies that sweep is the INTAKE ROW:
    an event the boundary refused reads `skipped` with the reason `library not picked`, and an
    event it admitted reads `enqueued`. So this fires two events in one window, one inside the
    pick and one outside it, and requires the sweep to have decided them in opposite directions --
    which is a claim no `ON CONFLICT` can satisfy by accident.

    THE PICK IS MADE THROUGH §6.6'S OWN PUT. Decision 364 gives `JellyfinConfig.library_ids` its
    first reader ever, and until the admin route learned to accept the field the picked state was
    reachable only by calling `save_jellyfin` from Python. A criterion that reached for the
    function would be measuring a boundary no admin can draw.
    """
    saved = await _admin(
        ctx, "PUT", "/api/admin/connectors/jellyfin", json={"library_ids": [PICKED_LIBRARY]},
    )
    if saved.status_code != 200:
        raise PreconditionFailed(f"the library pick was refused: {console(saved.text[:300])}")
    cfg = await registry.load_jellyfin(ctx.conn)
    if list(cfg.library_ids) != [PICKED_LIBRARY]:
        raise PreconditionFailed(f"the stored pick is {cfg.library_ids}, not the one just saved")

    await ctx.conn.execute("DELETE FROM jellyfin_intake")
    before = await _task_keys(ctx)
    await _emit(ctx, item_id=PICKED_ITEM)
    await _emit(ctx, item_id=UNPICKED_ITEM)
    await _age_the_window(ctx, WINDOW_MINUTES)
    report = await intake.sweep_pending(ctx.conn, _client(ctx), cfg)
    rows = {str(row["resolved_key"]): row for row in await _intake_rows(ctx)}
    outside = rows.get(UNPICKED_ITEM)
    inside = rows.get(PICKED_ITEM)
    after = await _task_keys(ctx)
    ok = (
        outside is not None
        and outside["state"] == intake.SKIPPED
        and outside["reason"] == intake.LIBRARY_NOT_PICKED
        and inside is not None
        and inside["state"] == intake.ENQUEUED
        and report.skipped == 1
        and report.enqueued == 0
        and report.already_queued == 1
        and after == before
    )
    return ok, (
        f"with {PICKED_LIBRARY} picked, {UNPICKED_ITEM} was recorded "
        f"{console(str(outside['state']) + '/' + str(outside['reason'])) if outside else 'NOT AT ALL'}"
        f" and {PICKED_ITEM} {console(str(inside['state'])) if inside else 'NOT AT ALL'}; the sweep "
        f"reported {report.as_dict()} and the queue holds {len(after)} key(s), as before"
    )


async def check_nine(ctx: Install) -> tuple[bool, str]:
    """Plan check 9: a wrong token is 401, records nothing, and enqueues nothing.

    BOTH SPELLINGS OF THE MISTAKE, because the operator makes both and the route answers them the
    same on purpose: a header carrying the wrong value, and no header at all. Distinguishing them
    in the response would tell an unauthenticated caller which half of the credential they had
    guessed, and the repair is identical either way -- re-paste what §6.6 showed once.

    "NOTHING RECORDED AS PENDING" IS THE PART WITH TEETH and it is a claim about the ORDER of the
    handler's first two statements. The token is checked before the body is even read, so a caller
    who finds the URL cannot write a row; a handler that recorded first and refused second would
    answer 401 to every one of these deliveries while handing anyone on the network an unbounded
    writer into `jellyfin_intake`. The verdict is therefore the row count over the WHOLE table and
    not over pending rows alone -- a refusal that was written down as a refusal would still be a
    write this route must not make.
    """
    rows_before = len(await _intake_rows(ctx))
    keys_before = await _task_keys(ctx)
    wrong = await _emit(ctx, item_id=PICKED_ITEM, token="not-the-token-the-operator-was-shown")
    absent = await _emit(ctx, item_id=PICKED_ITEM, token="")
    rows_after = len(await _intake_rows(ctx))
    keys_after = await _task_keys(ctx)
    answered = list(wrong["statuses"]) + list(absent["statuses"])
    ok = (
        answered == [401, 401]
        and rows_after == rows_before
        and keys_after == keys_before
    )
    return ok, (
        f"a wrong token and an absent header answered {answered}; the intake table holds "
        f"{rows_after} row(s) and held {rows_before}, and the queue is unchanged at "
        f"{len(keys_after)} key(s)"
    )


async def check_ten(ctx: Install) -> tuple[bool, str]:
    """Plan check 10: a payload missing a required field is 202, recorded, and faults nothing.

    THE TOLERANCE IS ASYMMETRIC ON PURPOSE (decision 365). The Webhook plugin renders an
    OPERATOR-AUTHORED template, so every field this app requires is a field an operator can
    forget, and the plugin fires and forgets -- no retry, no queue, nowhere to put a failure
    except a log nobody is reading. A 400 would therefore be the app keeping the diagnosis to
    itself: the add is lost either way, and only the 202-and-record path leaves §6.6 able to show
    the operator the body that arrived and what was wrong with it.

    "NO 500 IN THE LOG" IS MEASURED AS TWO THINGS because it is two things here. No 5xx status
    from any request this run made -- the transports are built with `raise_app_exceptions=False`
    precisely so a fault arrives as the status a browser would get rather than as an exception in
    this process -- and no ERROR-or-worse record out of the `spielplan` logger, because a route
    can answer 202 and still have faulted on the way to it.

    `omit` is the double's own refusal surface: it 409s a field the template never sends, so this
    can only take away something an operator could really have left out.
    """
    rows_before = len(await _intake_rows(ctx))
    sent = await _emit(ctx, item_id=PICKED_ITEM, omit=["ItemId"])
    rows = await _intake_rows(ctx)
    newest = rows[-1] if rows else None
    faulted = faults(ctx.log.records)
    server_errors = [(what, code) for what, code in ctx.statuses if code >= 500]
    ok = (
        list(sent["statuses"]) == [202]
        and len(rows) == rows_before + 1
        and newest is not None
        and newest["state"] == intake.SKIPPED
        and newest["reason"] == intake.NO_ITEM_ID
        and not faulted
        and not server_errors
    )
    return ok, (
        f"a body with no ItemId answered {sent['statuses']} and was recorded as "
        f"{console(str(newest['state']) + '/' + str(newest['reason'])) if newest else 'NOTHING'}; "
        f"{len(server_errors)} response(s) of 5xx and {len(faulted)} fault(s) in the log "
        f"({len(warnings_of(ctx.log.records))} warning(s), which are not faults)"
    )


# --- 11: the column only one path writes ---------------------------------------------------------


async def check_eleven(ctx: Install) -> tuple[bool, str]:
    """Plan check 11: across the whole run, only the full sweep ever wrote the ownership column.

    THIS IS DECISION 362, MEASURED. §7.2's third bullet made all three intake paths responsible
    for removals until v2.1.2, and two of them cannot discharge it: a `DateCreated >` read is an
    ADD DETECTOR BY CONSTRUCTION and never observes an absence, so the set it returns is a partial
    library by definition -- the truncated page `_falsify_ownership` already refuses, with the
    truncation designed in rather than suffered -- and one `ItemAdded` says nothing whatever about
    what the library no longer holds. So the falsifier keeps its two gates and its one caller, and
    the other two paths write the column never.

    IT IS MEASURED IN THREE WINDOWS AND ONE OF THEM MUST BE NON-EMPTY. The delta window runs from
    before check 1 to the end of check 5 and covers every poll and every delivery that precedes
    the sweeps; the webhook window runs from the end of check 7 to here and covers the pick, the
    two refusals and the tolerated payload. Both must be empty. The third window is checks 6 and 7
    themselves, and it must NOT be -- a differ that reported no change everywhere would satisfy
    the first two conditions by being blind, and this is the assertion that says the instrument
    can see an ownership write at all.
    """
    now = await _ownership(ctx)
    delta_window = _ownership_diff(ctx.born, ctx.after_delta)
    sweep_window = _ownership_diff(ctx.after_delta, ctx.after_sweeps)
    webhook_window = _ownership_diff(ctx.after_sweeps, now)
    ctx.sweep_moved = sweep_window
    ok = not delta_window and not webhook_window and bool(sweep_window)
    return ok, (
        f"checks 1-5 (the polls and the deliveries) moved {len(delta_window)} title(s): "
        f"{delta_window[:8]}; checks 6-7 (the full sweep) moved {len(sweep_window)}: "
        f"{sweep_window[:8]}; checks 8-10 (the pick and the refusals) moved "
        f"{len(webhook_window)}: {webhook_window[:8]}"
    )


# --- the run ---------------------------------------------------------------------------------------


RUNNERS = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five, 6: check_six,
    7: check_seven, 8: check_eight, 9: check_nine, 10: check_ten, 11: check_eleven,
}
RECORDED: set[int] = set()


async def measure(number: int, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's
    eleven: a run that stops at three and prints "3/3 checks passed" is the failure mode an exit
    criterion exists to rule out. A refused precondition is reported without its traceback -- the
    sentence IS the diagnosis, and a stack trace over it is what `ops/m4_exit_criterion.py`'s
    seeding taught this project to stop printing.
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
    check(verdict, label, detail)


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL export bundle on purpose:")
        print("checks 6, 7 and 11 are about the one clause that keeps the ownership falsifier to")
        print("the titles Jellyfin named, and on a fixture whose library IS its corpus there is")
        print("nothing outside the library for that clause to protect.")
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
        print("Refusing on purpose: every check below would still print, and the three about")
        print("ownership would mean nothing that their sentences say.")
        return 2
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print(
        f"\nM5.2 exit criterion -- bundle {console(root.name)} ({inventoried:,} bytes)\n",
        flush=True,
    )

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    scratch = f"spielplan_m52_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE -- the mkdtemp, the connect, the codec registration this project has been
    # bitten by once, the import, the app's boot, the child process -- happens inside the `try`,
    # because the scratch name carries this run's pid: pid-suffixing it (which is what stops two
    # concurrent runs dropping each other's database) removed the accidental second chance the
    # next run's `DROP DATABASE IF EXISTS` used to be. An orphan nothing will ever name again is a
    # leak on the household's own server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    neutral: Path | None = None
    ctx: Install | None = None
    lifespan: Any = None
    try:
        work = Path(tempfile.mkdtemp(prefix="spielplan-m52-exit-"))
        # A temporary DATA_DIR, removed in the `finally`. Staging into `ROOT/data` would leave
        # hundreds of megabytes inside the working tree per run, which the next run neither reuses
        # nor removes.
        os.environ["DATA_DIR"] = str(work)
        os.environ["DATABASE_URL"] = scratch_dsn
        _neutralise_connector_env()
        # A directory with no `.env` in it, entered before `settings()` is ever built.
        # `_neutralise_connector_env` removes the operator's connector variables from the
        # ENVIRONMENT, and pydantic-settings ranks a `.env` file below the environment but above
        # nothing -- so a run started from the repository root with a real JELLYFIN_URL in `.env`
        # would still boot this scratch install against the household's own server, which check 6
        # then deletes a film from. [ops/m414_exit_criterion.py's `no-dot-env`]
        neutral = Path(tempfile.mkdtemp(prefix="spielplan-m52-neutral-"))
        os.chdir(neutral)
        core_config.settings.cache_clear()

        conn = await asyncpg.connect(scratch_dsn)
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. Without it `title_meta.payload` fails with "expected str, got dict" --
        # which M4.5's harness found, and which was a defect in the script rather than in the
        # importer.
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The install this run measures (spec section 10; sections 6.6 and 7.3)",
              flush=True)
        ctx = Install(conn=conn, work=work, dsn=scratch_dsn, log=LogWatch())
        await build_install(ctx, root)
        lifespan = ctx.app.router.lifespan_context(ctx.app)
        await lifespan.__aenter__()
        await configure(ctx)
        # Attached AFTER the import and the boot, so that a finding the importer logged about the
        # corpus is not read by check 10 as this milestone's route faulting. Everything the eleven
        # checks provoke happens on the far side of this line.
        logging.getLogger("spielplan").addHandler(ctx.log)
        ctx.born = await _ownership(ctx)
        print(f"  ownership snapshot: {len(ctx.born):,} title(s)", flush=True)

        print("\n1. The burst: twelve episodes, one window (plan check 1)", flush=True)
        await measure(1, ctx)
        print("\n2. The burst across a restart at minute five (plan check 2)", flush=True)
        await measure(2, ctx)
        print("\n3. The fallback: one delta poll with the webhook silent (plan check 3)",
              flush=True)
        await measure(3, ctx)
        print("\n4. The next poll, with nothing added (plan check 4)", flush=True)
        await measure(4, ctx)
        print("\n5. The poll whose read failed (plan check 5)", flush=True)
        await measure(5, ctx)

        # Taken here and not at the end: check 11's first window is "every path but the sweep",
        # and the sweeps start on the next line. A snapshot taken after them would be comparing
        # the delta path against a library the falsifier had already been through.
        ctx.after_delta = await _ownership(ctx)

        print("\n6. The mirror shrinks: the full sweep un-owns (plan check 6)", flush=True)
        await measure(6, ctx)
        print("\n7. The mirror is restored: the full sweep re-owns (plan check 7)", flush=True)
        await measure(7, ctx)

        ctx.after_sweeps = await _ownership(ctx)

        print("\n8. The boundary: an add in a library nobody picked (plan check 8)", flush=True)
        await measure(8, ctx)
        print("\n9. The credential: a wrong token and no token (plan check 9)", flush=True)
        await measure(9, ctx)
        print("\n10. The operator's template, missing a field (plan check 10)", flush=True)
        await measure(10, ctx)
        print("\n11. The ownership column, across the whole run (plan check 11)", flush=True)
        await measure(11, ctx)
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
            if ctx.admin is not None:
                await ctx.admin.aclose()
        if lifespan is not None:
            # A shutdown that failed is not a verdict about anything the criterion asked, and the
            # database below still has to be dropped. `ops/m414_exit_criterion.py` closes its
            # lifespan the same way and for the same reason.
            with contextlib.suppress(Exception):
                await lifespan.__aexit__(None, None, None)
        if conn is not None:
            await conn.close()
        os.chdir(ROOT)
        discard_staged_artifacts(work, None)
        discard_staged_artifacts(neutral, None)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    failed = len(results) - passed
    print(f"\n{passed}/{len(CHECKS)} checks passed, {failed} failed", flush=True)
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
