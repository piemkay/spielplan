"""M5.5's exit criterion: the LLM connector layer, measured against a server that refuses.

§12's M5.5 row names this script (decision 435), and the fourteen checks below are
`docs/milestones/M5.5-plan.md` §7's own pass table, numbered as it numbers them and in its order.
The row's sentence is what they add up to:

    a provider response that satisfies the request schema but violates the extraction contract -
    a term vocabulary v1 does not carry, an evidence string that is not in the pack, a salience
    outside {1,2,3} - is rejected by M5.4's validator and retried exactly once, with the violated
    rule and the offending value named in the retry prompt; a second violation fails stage 6
    permanently and leaves the title's `dna_tag` rows unchanged; and the outcome is identical
    across the Gemini, Anthropic and OpenAI adapters despite three different structured-output
    mechanisms. The meter charges billed output including thinking tokens, so a Gemini call
    reporting 1,600 candidate and 2,300 thought tokens is metered at 3,900, while OpenAI's
    `completion_tokens` are used as they come. With the calendar month's meter at the cap, a job
    reaching stage 6 parks `over spend cap` without issuing a provider request, is still parked on
    the next tick, and an admin retry over the cap is refused with that reason. No provider key
    appears in any URL the LLM layer builds, in any `raw_document.url` or in any httpx log line;
    `gpt-5.6-terra` is priced as itself rather than as `gpt-5`, and a model the price table does
    not know is unpriced, its estimate "unknown" rather than a number

      script  plan  what it measures
        1       1   three schema-valid, contract-violating answers on each of three providers:
                    each refused by `verify_payload` and retried exactly once
        2       2   each retry carries the violated rule and the offending value, as the
                    provider RECEIVED it
        3       3   the same nine repeated on the retry: stage 6 fails for good, and the title's
                    `SELECT count(*) FROM dna_tag` is what it was
        4       4   the three cases reach one outcome on Gemini, Anthropic and OpenAI
        5       5   Gemini reporting 1,600 candidate + 2,300 thought tokens is metered at 3,900
        6       6   OpenAI's `completion_tokens` are metered as they come
        7       7   the month at its cap: stage 6 parks `over spend cap` and asks no provider
        8       8   the next tick: still parked, nothing asked
        9       9   an admin retry over the cap: refused with that reason, nothing queued
       10      10   no url the LLM layer builds carries a key - on the wire, and in `llm/*.py`
       11      11   no `raw_document.url` of a provider call carries a key
       12      12   no httpx INFO line of a provider call carries a key
       13      13   `price_for('openai', 'gpt-5.6-terra')` is the terra price, not gpt-5's
       14      14   an unknown model is unpriced, and the estimate says "unknown"

THE PROVIDERS ARE `ops/fake_llm.py`, A REFUSER AND NOT A MOCK. It answers in each provider's
PUBLISHED envelope, refuses what each published reference says its server refuses, and builds its
answers from the request alone - terms from the vocabulary block the prompt carries, quotes cut out
of the pack the prompt carries - so a tag that verifies here verified against text `dna/packs.py`
rendered and the raw store holds. Its first answer is wrong ON PURPOSE (plan §9's first risk): one
tag breaks the contract beside three that verify, which is the normal case §9's "the schema is a
cost-saving device, not the guarantee - the guarantee is the validator" is written for. M5.2's last
review cycle is why a double that agreed with the code is not good enough: its Jellyfin fake did,
where the real server did not, and every real webhook add was dropped.

EVERYTHING BELOW IS THE SHIPPED PATH, and nothing this script measures is a row it wrote. The
double is mounted through `httpx.ASGITransport` as the transport of the real `acquire.fetch.Fetcher`
(decision 373's `fetcher_factory` seam), so every provider POST passes the host policy, the pacing
and the breaker production runs; titles are walked by `pipeline.drain`, whose gate is decision
348's `refuse_uncapped_spend` as shipped; keys, models, the assignment and the cap are written
through `registry.save_connector`; and `llm_call`, `dna_tag`, `dna_reject`, `raw_document`, the
board and the queue are read back after the app wrote them.

WHAT IS PUT IN PLACE BY HAND IS WHAT M5.5 DOES NOT OWN. Stage 5 is M5.4's owed wiring (decision
432), so no title reaches stage 6 with a pack on its own: the pack is rendered by
`dna.packs.render_pack` and kept by `dna.packs.store_pack` - the custody function stage 5 will call
- and the board row is written by `pipeline.write_board` at (6, running), exactly what `run_task`
writes the moment stage 5 advances. The vocabulary is the fixture bundle's
(`backend/tests/fixtures/make_bundle.py`, whose files are shaped from a real export), loaded by the
importer's own `load_vocabulary` - so "a term vocabulary v1 does not carry" is judged against v1's
eleven facets as the importer stores them, with the fixture's alias map and adjudication ledger
beside them, and not against a list this file typed. And the title rows are plain inserts: minting
a title is stage 1's, and M5.1's script measures it.

CHECK 3 WALKS THE SAME NINE TITLES AGAIN, deliberately. "Leaves the title's `dna_tag` rows
unchanged" is only a measurement over a tier that HOLDS rows, and the tier worth protecting is the
one the app itself wrote a check earlier - a re-extraction that fails must not take a good tier
with it. The re-arm is the two writes decision 330's retry-from-stage-N will make at M5.6: the
board back to (6, running) through the driver's own writer, and the task made due now, which is
`ops/m53_exit_criterion.py` check 5's admin retry.

CHECK 7'S CAP IS THE MONTH'S OWN SPEND. After checks 1 to 6 the meter holds the calls they made, and
the cap is set to exactly `spend.spent` - decision 325's SUM over `llm_call` for the install's
calendar month - so "the meter at the cap" is the meter's own arithmetic over rows the app wrote,
never a row this script inserted to look like spend. No default cap ships (decision 325); this run
sets one because the criterion is about a cap that exists.

NO REAL KEY AND NO SOCKET (decision 435). Every connector variable `Settings` would seed from - the
three provider keys among them - is removed from the environment unread before anything is built,
and so are the double's own override variables, so every key this run stores is a literal in
`ops/fake_llm.py`. Every transport is ASGI, so a request that somehow escaped the double would fail
on a name that does not resolve rather than reach a provider. NOTHING IS BILLED: the cost table at
the end is what the same calls would have cost at the dated price table (decision 343), read off
`llm_call`, in plain ASCII dollars.

THE MONTH IS THE INSTALL'S (decision 325), from §2's `TZ`. On a machine whose Python carries no tz
database the name does not resolve, `llm/spend` falls back to the process's own clock and logs it on
every read, and this script reports which zone the meter used rather than hiding the fallback.

NO THIRD COLUMN. Every check measures in-process, like `ops/m52_exit_criterion.py`'s, so no check is
one a lane could not measure and the exit codes are 0 (all fourteen held), 1 (a check failed) and 2
(a precondition refused before the run: no database to create a scratch one on, or no double).

It creates and drops its own DATABASE, named with this run's pid, and stages into a temporary
DATA_DIR - the raw store's root - that it removes. It connects through `db/pool._init_connection`,
never a bare `asyncpg.connect`, because the json/jsonb codec is what every reader of `detail` and
`request_meta` depends on. Output is ASCII: every string this script did not author goes through
`console()` on the way out - decision 348's park sentence carries a section sign, the retry prompts
carry the model's own text, and the app's warnings are the app's.

Run it against a live Postgres:

    TEST_DATABASE_URL=postgresql://... backend/.venv/Scripts/python ops/m55_exit_criterion.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s reporting helpers are imported rather than copied,
# which every script since `ops/m414_exit_criterion.py` does and for its reason: M4.5's recorded
# 18/18 has to stay reproducible, so that file is read and never edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181 made
# §2's required config a refusal at construction. Set outright rather than defaulted: this script
# must never seal anything under a household's real SECRETS_KEY, and every provider key it seals is
# the double's own literal, so a throwaway key is all the encryption here means.
os.environ["SESSION_SECRET"] = "m55-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m55-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan.acquire import fetch, pipeline, queue, rawstore  # noqa: E402
from spielplan.api import llm as llm_api  # noqa: E402
from spielplan.connectors import registry  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import dna_terms, migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.dna import packs  # noqa: E402
from spielplan.importer import dna as dna_import  # noqa: E402
from spielplan.importer.report import ImportReport  # noqa: E402
from spielplan.llm import pricing, spend  # noqa: E402

# The fixture bundle's builder, imported rather than copied for the reason `ops/devstub.py` imports
# it: its files are shaped from a real export (`real_bundle_shapes.json`), and a vocabulary this
# script wrote out by hand would be this script's idea of one.
from tests.fixtures import make_bundle as fixture_bundle  # noqa: E402

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "a schema-valid, contract-violating answer is refused and retried exactly once"),
    (2, "the retry prompt names the violated rule and the offending value"),
    (3, "a second violation fails stage 6 for good and leaves the title's dna_tag count as it was"),
    (4, "the three cases reach one outcome on Gemini, Anthropic and OpenAI"),
    (5, "a Gemini call reporting 1,600 + 2,300 tokens is metered at tokens_out_billed 3,900"),
    (6, "OpenAI's completion_tokens are metered as they come"),
    (7, "the month at its cap: stage 6 parks 'over spend cap' and asks no provider"),
    (8, "the next tick: the job is still parked and nothing is asked"),
    (9, "an admin retry over the cap is refused with that reason and nothing is queued"),
    (10, "no url the LLM layer builds carries a key"),
    (11, "no raw_document.url of a provider call carries a key"),
    (12, "no httpx INFO line of a provider call carries a key"),
    (13, "price_for('openai', 'gpt-5.6-terra') is the terra price, not gpt-5's"),
    (14, "an unknown model is unpriced and its estimate is 'unknown'"),
)

# The criterion's three providers, in its order, and the model each is configured with - the
# `DEFAULT_MODELS` of decision 343's table, written into each provider's row the way an admin's
# card would, so the url check 10 expects is built from what THIS script configured.
PROVIDERS = ("gemini", "anthropic", "openai")
MODELS = {"gemini": "gemini-3.7-flash", "anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra"}

# Each provider's documented generation endpoint: the only url a paid call may carry, and so the
# only url `raw_document.url` may hold. No query string on any of them - Gemini's reference
# documents `?key=`, and plan 2.5 is that this is the trap.
ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
}

# The criterion's three contract violations, as `ops/fake_llm.py` names its content scenarios, and
# the rule M5.4's `verify_payload` refuses each under: a term vocabulary v1 does not carry, an
# evidence string that is not in the pack, and a salience of 4, which decision 386 refuses as
# `schema` rather than clamping.
RULES = {"fabricate": "unknown_term", "unquotable": "quote_unverified", "salience": "schema"}
CONTENTS = tuple(RULES)

# The level the double's `salience` scenario states (its `_tags`: "states a level of 4"), and the
# words decision 386's refusal names it in.
STATED_SALIENCE = "stated level 4"

# The corpus's measurement of gemini-3.6-flash on this prompt, which is §9's "~5x" correction and
# check 5's figures: "~1.6k output plus ~2.3k thoughts" (`mdc/config.py:181-185`). Every scenario
# this run sets is pinned to them, so the figures on the console are the criterion's.
CANDIDATE_TOKENS = 1_600
THOUGHT_TOKENS = 2_300

# Room under the cap for checks 1 to 6, in USD for the calendar month. Far above what the run's
# calls cost at the table's prices, so no check before 7 can park for spend.
CAP_WITH_ROOM = 1000

# The criterion's words for the park, matched as its own literal rather than as the app's constant,
# so a reason the app re-worded is caught here and not agreed with.
OVER_CAP = "over spend cap"

# Check 13's point release nobody priced - the name a new OpenAI model would plausibly carry, which a
# bare `startswith` hands `gpt-5`'s price - and check 14's model no table row names at all.
POINT_RELEASE = "gpt-5.7-horizon"
UNPRICED_MODEL = "gpt-9-unreleased"

# The double's control surface. The host is ignored by the ASGI transport; it is named so an httpx
# line about the harness can never be mistaken for one about a provider.
DOUBLE_CONTROL = "http://fake-llm"

# The one film every title in this run is, bar its name. Written for the double to quote from: three
# sections of at least twenty words, so every real tag quotes a different section and the one the
# violation embellishes has a second span to take. No apostrophe anywhere, so the retry's `repr` of a
# refused quote is the quote between two single quotes and check 2 can find it character for
# character.
YEAR = 2021
PLOT = (
    "A retired lighthouse keeper returns to the island town that turned him away and slowly "
    "uncovers who let his brother drown on the night of the winter storm."
)
REVIEWS = [
    ("imdb", "A bleak and unforgiving portrait of a town that has agreed on what it will not "
             "remember, shot in grey light by a director who refuses every easy consolation and "
             "every tidy ending."),
    ("tmdb", "The tension builds patiently across two hours and never once releases, a slow burn "
             "that rewards anyone willing to stay with its long silent scenes and its stubborn "
             "refusal to explain itself."),
]
FIRST_TITLE_ID = 5501

STAGE_SIX = next(stage for stage in pipeline.STAGES if stage.number == 6)


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


class Captured(logging.Handler):
    """Every record one logger emits during the run, kept for check 12 and the closing note.

    Check 12 reads the `httpx` logger at INFO, which is where httpx writes one line per request
    with the full url - the line `push/send.py` had to filter for exactly this reason. The app's own
    `spielplan` logger is captured beside it at the same level, so the key sweep reads every line
    the LLM layer and the driver wrote too, and so its warnings - the TZ fallback above all - are
    summarised once at the end instead of printed on every read.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class Clock:
    """A clock the run advances itself, so the provider hosts' real pacing costs no real time.

    `acquire/hosts.py` paces each provider host at two requests a second with a burst of four, and
    those numbers are not the thing a harness changes: the `Fetcher` takes `clock`, `sleep` and
    `jitter` precisely so a measurement can be tolerant while the policy stays honest - the rule
    `ops/m53_exit_criterion.py`'s `Clock` keeps. The spin limit turns a bucket that never refills
    into a failed check instead of a script that hangs.
    """

    _SPIN_LIMIT = 20_000

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits += 1
        if self.waits > self._SPIN_LIMIT:
            raise PreconditionFailed(
                f"the fetcher paced {self._SPIN_LIMIT} times; last wait {seconds:.2f}s"
            )
        self.now += max(seconds, 0.0)

    @staticmethod
    def jitter(low: float, _high: float) -> float:
        return low


@dataclass
class Run:
    """One drain of one title, and everything it left behind that a check reads."""

    provider: str
    content: str
    posture: str
    title_id: int
    walk: Any
    sent: list[dict[str, Any]]
    tags_before: int
    tags_after: int
    tier_before: list[tuple]
    tier_after: list[tuple]
    # (rule_violated, term, quote) for every refusal `verify.record_rejects` wrote in this drain.
    rejects: list[tuple[str, str | None, str | None]]
    # (attempt, ok, tokens_in, tokens_out_billed, usd, model) for every `llm_call` row it added.
    calls: list[tuple[int, bool, int, int, Decimal, str]]
    board: dict[str, Any]
    task: dict[str, Any]


@dataclass
class Install:
    """The one install every check below measures, and what the checks hand each other."""

    conn: asyncpg.Connection
    work: Path
    double: Any
    clock: Clock
    httpx_log: Captured
    app_log: Captured
    version: str = ""
    next_title: int = FIRST_TITLE_ID
    fetchers_built: int = 0
    keys: dict[int, str] = field(default_factory=dict)
    runs: dict[tuple[str, str, str], Run] = field(default_factory=dict)
    # Check 7's title, the reason it parked under, and the two rows as they stood, for 8 and 9.
    capped_title: int = 0
    capped_reason: str = ""
    capped_board: dict[str, Any] = field(default_factory=dict)
    capped_task: dict[str, Any] = field(default_factory=dict)


# --- the install ------------------------------------------------------------------------------------


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit as the other exit scripts: an untracked file names the
    server, so this line is the only place the script decides which host it creates a database on.
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
    """Take every connector credential out of the environment, unread, before anything reads it.

    Derived from the registry rather than listed: every connector `registry.CONNECTORS` marks as
    seeded names the `Settings` fields its env variables arrive in by its own prefix, so the three
    provider keys M5.5 added are removed here without anyone remembering a list - the defect the
    M0-era prefix tuples in the five older exit scripts carried until M5.5's review derived theirs
    this way too (`backend/tests/conftest.py` lists all seven prefixes by hand).
    The double's own override variables go too, so every key this run seals is a literal in
    `ops/fake_llm.py` and none is a value an operator's shell supplied (decision 435).

    BOTH HALVES, for `ops/m53_exit_criterion.py`'s reason: `Settings` also reads `.env` from the
    working directory, so the process moves into a directory of this run's own DATA_DIR, which
    `main` removes after moving back out.
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
    for variable in list(os.environ):
        if variable.upper().startswith("FAKE_LLM_"):
            os.environ.pop(variable, None)
    neutral = Path(os.environ["DATA_DIR"]) / "no-dot-env"
    neutral.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral)


def _load_double() -> Any:
    """`ops/fake_llm.py` mounted in-process, exactly as `backend/tests/test_llm_stage.py` mounts it.

    Registered in `sys.modules` before it is executed, which is what `import` itself does: the module
    carries `from __future__ import annotations`, so Pydantic resolves its control models through
    `sys.modules[cls.__module__]`.
    """
    spec = importlib.util.spec_from_file_location("fake_llm", ROOT / "ops" / "fake_llm.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


def month_of(meter: dict[str, Any]) -> str:
    """The calendar month a `spend.meter` reading covers, named in the zone that bounded it.

    `period_start` is the first instant of the local month as a UTC instant, so read bare it names
    the previous month anywhere east of Greenwich - Europe/Berlin's September starts on 31 August
    at 22:00 UTC. `spend.local_zone()` is the zone the meter used, None meaning the process's own
    clock, which is also what `astimezone(None)` reads.
    """
    return f"{meter['period_start'].astimezone(spend.local_zone()):%Y-%m}"


async def build_install(conn: asyncpg.Connection, work: Path, httpx_log: Captured,
                        app_log: Captured) -> Install:
    """The vocabulary, the three providers keyed, and a cap with room. Raises on a refusal."""
    double = _load_double()
    bundle = fixture_bundle.make_bundle(work / "fixture-bundle")
    vocab_root = bundle / "artifacts" / "dna_vocab"
    shipped = sorted(path for path in vocab_root.iterdir() if path.is_dir())
    if len(shipped) != 1:
        raise PreconditionFailed(f"the fixture bundle ships {len(shipped)} vocabularies, not one")
    version = shipped[0].name
    report = ImportReport()
    await dna_import.load_vocabulary(conn, shipped[0], version, report)
    if await dna_terms.active_version(conn) != version:
        raise PreconditionFailed(f"vocabulary {version} loaded and is not the active one")
    facets = await conn.fetchval("SELECT count(*) FROM dna_facet WHERE version = $1", version)
    terms = await conn.fetchval("SELECT count(*) FROM dna_term WHERE version = $1", version)

    for provider in PROVIDERS:
        await registry.save_connector(
            conn, provider, api_key=double.KEYS[provider], model=MODELS[provider]
        )
    await registry.save_connector(conn, spend.SETTINGS, cap_usd=CAP_WITH_ROOM)
    meter = await spend.meter(conn)
    print(
        f"  vocabulary {console(version)} loaded by the importer: {facets} facets, {terms} terms",
        flush=True,
    )
    print(
        "  providers keyed through registry.save_connector with the double's keys: "
        + ", ".join(f"{p} ({MODELS[p]})" for p in PROVIDERS),
        flush=True,
    )
    print(
        f"  cap {meter['cap_usd']} USD for {month_of(meter)}, month bounded in "
        f"{console(str(meter['tz']))}",
        flush=True,
    )
    return Install(
        conn=conn, work=work, double=double, clock=Clock(), httpx_log=httpx_log,
        app_log=app_log, version=version,
    )


def build_fetcher(ctx: Install):
    """`pipeline.drain`'s `fetcher_factory`: the real Fetcher, the double as its transport.

    It counts as well as builds, because check 7 fails a drain that built one for the title it
    parked: the gate is asked before any Fetcher is built, and stage 6 opens the drain's only when it
    is about to send, so a Fetcher built for a parked title is the ordering M5.5's review cycle 1
    removed coming back. This used to say `run_task` opens the Fetcher before the gate asks, which was
    that defect, and check 7 printed it as the shipped behaviour beside a count it never asserted.
    [M5.5 review cycle 1, NBR-02; review cycle 2, M55-C2-DOC-03]
    """

    async def factory(conn: asyncpg.Connection) -> fetch.Fetcher:
        ctx.fetchers_built += 1
        return fetch.Fetcher(
            conn=conn, transport=httpx.ASGITransport(app=ctx.double.app),
            clock=ctx.clock, sleep=ctx.clock.sleep, jitter=ctx.clock.jitter,
        )

    return factory


async def scenario(ctx: Install, *, provider: str, content: str = "clean",
                   posture: str = "comply", thoughts: bool = True) -> dict[str, Any]:
    """Set how one provider answers, every field pinned, through the double's own control route.

    Every field is sent every time so no scenario leaks from one check into the next. A refusal is a
    precondition and not a verdict: the double refuses an envelope or a content its provider does not
    document, and that is the harness failing, not the app.
    """
    body = {
        "provider": provider, "content": content, "posture": posture, "envelope": "normal",
        "thoughts": thoughts, "prompt_tokens": None,
        "output_tokens": CANDIDATE_TOKENS, "thought_tokens": THOUGHT_TOKENS,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ctx.double.app), base_url=DOUBLE_CONTROL
    ) as control:
        answer = await control.post("/_test/scenario", json=body)
    if answer.status_code != 200:
        raise PreconditionFailed(
            f"the double refused scenario {body!r}: {answer.status_code} {console(answer.text[:200])}"
        )
    return answer.json()["scenarios"][provider]


async def assign(ctx: Install, provider: str) -> None:
    """Decision 324's per-task assignment for the one task M5 has, through the settings row.

    `parallel` and `passes` are left absent on purpose: an absent pair is the fresh install decision
    324 describes - off, one pass - and a run that wrote them would measure a configuration nobody
    ships.
    """
    await registry.save_connector(ctx.conn, spend.SETTINGS, extraction_provider=provider)


async def new_title(ctx: Install) -> int:
    """A title at stage 6 with its pack in custody and its task on the queue.

    The name carries the id, so every pack's bytes differ and each `raw_document` row is its own
    title's - the raw store is content-addressed. The task's key is read back from the queue rather
    than spelled here, and the pack is filed under it, because `store_pack`'s docstring says the
    entity key IS the acquisition task's key and there is no second spelling of it.
    """
    title_id = ctx.next_title
    ctx.next_title += 1
    name = f"The Keeper of Harbour Light No. {title_id}"
    await ctx.conn.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, $3, true)",
        title_id, name, YEAR,
    )
    if not await pipeline.enqueue_title(ctx.conn, title_id):
        raise PreconditionFailed(f"the queue already held a task for title {title_id}")
    key = await ctx.conn.fetchval(
        "SELECT key FROM acquisition_task WHERE kind = $1 AND (payload ->> 'title_id')::int = $2",
        pipeline.TASK_KIND, title_id,
    )
    ctx.keys[title_id] = str(key)
    text, info = packs.render_pack(title_id, name, YEAR, "movie", None, None, PLOT, REVIEWS)
    await packs.store_pack(ctx.conn, title_id, ctx.version, text, info, entity_key=ctx.keys[title_id])
    await pipeline.write_board(ctx.conn, title_id, stage=STAGE_SIX.number, status=pipeline.RUNNING)
    return title_id


async def tier(conn: asyncpg.Connection, title_id: int) -> list[tuple]:
    rows = await conn.fetch(
        "SELECT term, provider, salience, confidence, n_sources FROM dna_tag WHERE title_id = $1"
        " ORDER BY term, provider", title_id,
    )
    return [tuple(row) for row in rows]


async def board_of(conn: asyncpg.Connection, title_id: int) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", title_id)
    return dict(row) if row is not None else {}


async def task_of(ctx: Install, title_id: int) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(
        "SELECT * FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, ctx.keys[title_id],
    )
    return dict(row) if row is not None else {}


async def drain(ctx: Install) -> tuple[pipeline.DrainReport, list[dict[str, Any]]]:
    """One tick of the real drain, and the provider requests the double recorded during it."""
    mark = len(ctx.double.state.requests)
    report = await pipeline.drain(ctx.conn, fetcher_factory=build_fetcher(ctx))
    return report, list(ctx.double.state.requests[mark:])


async def walk_title(ctx: Install, title_id: int, *, provider: str, content: str,
                     posture: str) -> Run:
    """Drain once and keep what the walk of this title left: the double's log, the meter, the
    refusals, the tier before and after, the board and the task."""
    conn = ctx.conn
    before = await tier(conn, title_id)
    rejected_to = await conn.fetchval("SELECT coalesce(max(id), 0) FROM dna_reject")
    metered_to = await conn.fetchval("SELECT coalesce(max(id), 0) FROM llm_call")
    report, sent = await drain(ctx)
    walk = next((task for task in report.tasks if task.title_id == title_id), None)
    if walk is None:
        raise PreconditionFailed(
            f"the drain leased {report.leased} task(s) and none of them was title {title_id}"
        )
    after = await tier(conn, title_id)
    rejects = [
        (row["rule_violated"], row["term"], row["quote"]) for row in await conn.fetch(
            "SELECT rule_violated, term, quote FROM dna_reject WHERE id > $1 AND title_id = $2"
            " ORDER BY id", rejected_to, title_id,
        )
    ]
    calls = [
        (row["attempt"], row["ok"], row["tokens_in"], row["tokens_out_billed"], row["usd"],
         row["model"])
        for row in await conn.fetch(
            "SELECT attempt, ok, tokens_in, tokens_out_billed, usd, model FROM llm_call"
            " WHERE id > $1 AND title_id = $2 ORDER BY id", metered_to, title_id,
        )
    ]
    return Run(
        provider=provider, content=content, posture=posture, title_id=title_id, walk=walk,
        sent=sent, tags_before=len(before), tags_after=len(after), tier_before=before,
        tier_after=after, rejects=rejects, calls=calls, board=await board_of(conn, title_id),
        task=await task_of(ctx, title_id),
    )


def runs_of(ctx: Install, posture: str) -> dict[tuple[str, str], Run]:
    return {
        (provider, content): run for (provider, content, stance), run in ctx.runs.items()
        if stance == posture
    }


def named_rules(retry: str | None) -> list[str]:
    """The rules a retry names, one per `- <rule>: ` line, as the provider received them."""
    return re.findall(r"^- (\w+): ", retry or "", re.MULTILINE)


# --- 1 to 4: the two-attempt pattern ------------------------------------------------------------------


async def check_one(ctx: Install) -> tuple[bool, str]:
    """Plan check 1: a schema-valid, contract-violating answer is rejected; exactly one retry.

    ALL THREE VIOLATIONS ON ALL THREE PROVIDERS, nine walks of stage 6 through the driver. The
    double's first answer is schema-valid in each provider's own mechanism and carries one tag that
    breaks the contract - an invented term, an embellished quote, a salience of 4 - beside three that
    verify; its retry, in the `comply` posture, drops exactly the tags the retry named. So a walk
    that holds is two requests and no third, the first no retry and the second one; one refusal
    recorded by `verify.record_rejects` under the rule the scenario breaks, which is the evidence it
    was M5.4's `verify_payload` that refused it (decision 341); both attempts metered; the clean
    answer written; and the walk gone on past stage 6.
    """
    for provider in PROVIDERS:
        await assign(ctx, provider)
        for content in CONTENTS:
            title_id = await new_title(ctx)
            await scenario(ctx, provider=provider, content=content, posture="comply")
            ctx.runs[(provider, content, "comply")] = await walk_title(
                ctx, title_id, provider=provider, content=content, posture="comply"
            )
    judged: list[bool] = []
    lines: list[str] = []
    for (provider, content), run in runs_of(ctx, "comply").items():
        rule = RULES[content]
        retry = run.sent[1].get("retry") if len(run.sent) > 1 else None
        held = (
            len(run.sent) == 2
            and all(request["provider"] == provider for request in run.sent)
            and run.sent[0].get("retry") is None
            and (retry or "").startswith(ctx.double.RETRY_MARKER)
            and [reject[0] for reject in run.rejects] == [rule]
            and [call[0] for call in run.calls] == [1, 2]
            and run.walk.stage > STAGE_SIX.number
            and run.tags_after > 0
        )
        judged.append(held)
        lines.append(
            f"{provider:9} {content:10} {len(run.sent)} request(s); refused as "
            f"{[reject[0] for reject in run.rejects]}; metered attempts {[c[0] for c in run.calls]}; "
            f"{run.tags_after} tag(s) written; walk went on to stage {run.walk.stage} "
            f"({run.walk.status})" + ("" if held else "  <-- DID NOT HOLD")
        )
    ok = len(judged) == len(PROVIDERS) * len(CONTENTS) and all(judged)
    return ok, "\n".join(lines)


async def check_two(ctx: Install) -> tuple[bool, str]:
    """Plan check 2: the retry prompt contains the violated rule and the offending value.

    READ OFF WHAT THE PROVIDER RECEIVED - the double's own record of the second request - and not off
    the string this app built, because a retry that was built right and sent wrong is the failure
    this measures. The offending value is found without asking the app which one it was: the term is
    the one the double answered on attempt 1 and dropped on attempt 2 because the retry named it,
    and for the fabricated case it is also one no row of `dna_term` holds; the embellished quote is
    the one the validator refused, and it has to carry the double's own embellishment; the salience
    is the level the double's scenario states.
    """
    comply = runs_of(ctx, "comply")
    if len(comply) != len(PROVIDERS) * len(CONTENTS):
        raise PreconditionFailed(f"check 1 completed {len(comply)} of its nine walks")
    vocabulary = {
        row["term"] for row in await ctx.conn.fetch(
            "SELECT term FROM dna_term WHERE version = $1", ctx.version
        )
    }
    judged: list[bool] = []
    lines: list[str] = []
    for (provider, content), run in comply.items():
        rule = RULES[content]
        retry = (run.sent[1].get("retry") if len(run.sent) > 1 else None) or ""
        dropped = sorted(set(run.sent[0].get("terms") or []) - set(run.sent[1].get("terms") or []))
        term = dropped[0] if len(dropped) == 1 else ""
        refused_quote = next((q for r, _t, q in run.rejects if r == rule and q), "")
        if content == "fabricate":
            value, shaped = term, bool(term) and term not in vocabulary
        elif content == "unquotable":
            value, shaped = refused_quote[:60], "the double wrote" in refused_quote
        else:
            value, shaped = STATED_SALIENCE, bool(term) and term in vocabulary
        held = (
            len(dropped) == 1
            and shaped
            and f"- {rule}: " in retry
            and f"'{term}'" in retry
            and bool(value) and value in retry
        )
        judged.append(held)
        named = [line for line in retry.splitlines() if line.startswith("- ")]
        lines.append(
            f"{provider:9} {content:10} names {named_rules(retry)}; the offending term "
            f"{term!r}{'' if content == 'fabricate' else ', value ' + repr(value[:40])}"
            + ("" if held else "  <-- DID NOT HOLD")
        )
        if provider == PROVIDERS[0]:
            lines.extend(f"                     received: {line[:90]}" for line in named)
    ok = len(judged) == len(PROVIDERS) * len(CONTENTS) and all(judged)
    return ok, "\n".join(lines)


async def rearm(ctx: Install, title_id: int) -> None:
    """Put a title back at stage 6 and make its task due: decision 330's retry-from-stage-N, which
    is M5.6's to ship, done with the driver's own board writer and check 5 of M5.3's make-due."""
    await pipeline.write_board(ctx.conn, title_id, stage=STAGE_SIX.number, status=pipeline.RUNNING)
    await ctx.conn.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, ctx.keys[title_id],
    )


async def check_three(ctx: Install) -> tuple[bool, str]:
    """Plan check 3: a second violation fails the stage and `dna_tag`'s count is unchanged.

    THE SAME NINE TITLES, each holding the tier check 1 wrote, walked again with the double in its
    `stubborn` posture - it repeats the violation the retry named. Decision 431: that is final, so
    each walk is two requests and never a third, stage 6 FAILED, the task closed on the queue with
    the board saying no retry is coming, and `SELECT count(*) FROM dna_tag WHERE title_id = ...`
    - with the tier itself - exactly what it was. Both attempts are metered. Then one more tick,
    which leases none of the nine and asks nobody: a permanent failure is not a failure on the
    queue's curve.
    """
    comply = runs_of(ctx, "comply")
    if len(comply) != len(PROVIDERS) * len(CONTENTS):
        raise PreconditionFailed(f"check 1 completed {len(comply)} of its nine walks")
    for (provider, content), first in comply.items():
        await assign(ctx, provider)
        await rearm(ctx, first.title_id)
        await scenario(ctx, provider=provider, content=content, posture="stubborn")
        ctx.runs[(provider, content, "stubborn")] = await walk_title(
            ctx, first.title_id, provider=provider, content=content, posture="stubborn"
        )
    tick, asked = await drain(ctx)
    stubborn_titles = {run.title_id for run in runs_of(ctx, "stubborn").values()}
    leased_again = [task for task in tick.tasks if task.title_id in stubborn_titles]

    judged: list[bool] = []
    lines: list[str] = []
    for (provider, content), run in runs_of(ctx, "stubborn").items():
        rule = RULES[content]
        stage_detail = (run.board.get("detail") or {}).get(STAGE_SIX.name) or {}
        held = (
            run.walk.stage == STAGE_SIX.number
            and run.walk.status == pipeline.FAILED
            and len(run.sent) == 2
            and named_rules(run.sent[1].get("retry")) == [rule]
            and run.tags_before > 0
            and run.tags_after == run.tags_before
            and run.tier_after == run.tier_before
            and [call[0] for call in run.calls] == [1, 2]
            and stage_detail.get("retrying") is False
            and run.task.get("state") == queue.FAILED
        )
        judged.append(held)
        lines.append(
            f"{provider:9} {content:10} stage {run.walk.stage} {run.walk.status} after "
            f"{len(run.sent)} request(s); dna_tag count {run.tags_before} -> {run.tags_after}; "
            f"task {run.task.get('state')}, retrying {stage_detail.get('retrying')}"
            + ("" if held else "  <-- DID NOT HOLD")
        )
    lines.append(
        f"the next tick leased {len(leased_again)} of the nine and sent {len(asked)} provider "
        "request(s)"
    )
    ok = (
        len(judged) == len(PROVIDERS) * len(CONTENTS)
        and all(judged)
        and not leased_again
        and not asked
    )
    return ok, "\n".join(lines)


def _outcome(run: Run) -> tuple:
    """What one walk came to, with every trace of which provider it was taken out: the stages it
    ran and where it stopped, the requests and the rules each retry named, the refusals, the calls,
    and the tier - by term, salience and weights - or, for a second violation, whether it held."""
    written = tuple(row[:1] + row[2:] for row in run.tier_after)
    tier_part = written if run.posture == "comply" else (run.tier_after == run.tier_before,)
    return (
        tuple(run.walk.stages_run), run.walk.stage, run.walk.status, len(run.sent),
        tuple(tuple(named_rules(request.get("retry"))) for request in run.sent),
        tuple((rule, term) for rule, term, _quote in run.rejects),
        tuple((attempt, ok) for attempt, ok, *_rest in run.calls),
        tier_part,
    )


async def check_four(ctx: Install) -> tuple[bool, str]:
    """Plan check 4: the same three cases on all three adapters give identical outcomes.

    Forced tool-use, a strict schema and `responseSchema` are three mechanisms and §9 makes them one
    enforcement, so each case's outcome on Gemini, Anthropic and OpenAI is compared as ONE tuple
    rather than as three passing lines - checks 1 and 3 each passing on every provider would still
    allow the three to reach different verdicts, write different tiers or name different rules.
    """
    comply, stubborn = runs_of(ctx, "comply"), runs_of(ctx, "stubborn")
    judged: list[bool] = []
    lines: list[str] = []
    for content in CONTENTS:
        seen = {
            provider: (_outcome(comply[(provider, content)]), _outcome(stubborn[(provider, content)]))
            for provider in PROVIDERS
            if (provider, content) in comply and (provider, content) in stubborn
        }
        same = len(seen) == len(PROVIDERS) and len(set(seen.values())) == 1
        judged.append(same)
        first = next(iter(seen.values()), None)
        if same and first is not None:
            retried, repeated = first
            lines.append(
                f"{content:10} one outcome on {', '.join(PROVIDERS)}: the retry names "
                f"{', '.join(retried[4][-1]) or 'nothing'}, {len(retried[7])} tag(s) written, stage "
                f"{retried[1]} {retried[2]}; repeated, it ends at stage {repeated[1]} {repeated[2]}"
            )
        else:
            lines.append(f"{content:10} DIFFERS across {sorted(seen)}:")
            lines.extend(f"    {provider}: {outcome}" for provider, outcome in seen.items())
    ok = len(judged) == len(CONTENTS) and all(judged)
    return ok, "\n".join(lines)


# --- 5 and 6: the meter charges what is billed --------------------------------------------------------


async def single_call(ctx: Install, provider: str, *, thoughts: bool) -> tuple[Run, dict[str, Any]]:
    """One title, one clean answer from `provider`, and the usage the double was set to report."""
    await assign(ctx, provider)
    title_id = await new_title(ctx)
    reported = await scenario(ctx, provider=provider, content="clean", thoughts=thoughts)
    run = await walk_title(ctx, title_id, provider=provider, content="clean", posture="comply")
    return run, reported


async def check_five(ctx: Install) -> tuple[bool, str]:
    """Plan check 5: Gemini usage 1,600 + 2,300 gives `llm_call.tokens_out_billed` = 3,900.

    §9: Gemini "bills thinking tokens as output - counting visible JSON understates cost ~5x". The
    double reports `candidatesTokenCount` and `thoughtsTokenCount` apart, as the thinking page
    documents, and the row must hold their sum and be charged at it. The control is the same call
    from a model that did not think, whose response carries no `thoughtsTokenCount` at all and whose
    row must say 1,600 - so a meter that wrote a constant, or added thoughts that were never
    reported, fails one of the two.
    """
    thinking, reported = await single_call(ctx, "gemini", thoughts=True)
    plain, _ = await single_call(ctx, "gemini", thoughts=False)
    price = pricing.price_for("gemini", MODELS["gemini"])
    billed = reported["output_tokens"] + reported["thought_tokens"]
    held = [
        len(run.calls) == 1 and run.calls[0][1] and run.calls[0][2] > 0
        and run.calls[0][3] == expected
        and price is not None and run.calls[0][4] == pricing.usd(run.calls[0][2], expected, price)
        for run, expected in ((thinking, billed), (plain, reported["output_tokens"]))
    ]
    ok = billed == CANDIDATE_TOKENS + THOUGHT_TOKENS and all(held)
    return ok, "\n".join([
        f"reported {reported['output_tokens']} candidate + {reported['thought_tokens']} thought "
        f"tokens; the row says tokens_out_billed {thinking.calls[0][3] if thinking.calls else None}, "
        f"usd {thinking.calls[0][4] if thinking.calls else None}",
        f"the same call with no thoughtsTokenCount: tokens_out_billed "
        f"{plain.calls[0][3] if plain.calls else None}",
    ])


async def check_six(ctx: Install) -> tuple[bool, str]:
    """Plan check 6: OpenAI's `completion_tokens` are used as-is - they already fold reasoning.

    The reasoning guide documents `completion_tokens` as INCLUDING the reasoning and itemises it
    under `completion_tokens_details.reasoning_tokens`, so the bill is the first figure as it comes.
    Both wrong readings are named: adding the itemised reasoning charges it twice, and subtracting
    it charges only the visible answer.

    The dollars are priced with the prompt's cache breakdown the double reported beside them:
    gpt-5.6-terra writes the prompt to OpenAI's cache by default and bills the write at the
    published $2.50 rather than the $2.00 input rate, and the meter charges what it bills (decision
    437, M5.5 review cycle 1's M55-DBL-02).
    """
    run, reported = await single_call(ctx, "openai", thoughts=True)
    completion = reported["output_tokens"] + reported["thought_tokens"]
    reasoning = reported["thought_tokens"]
    price = pricing.price_for("openai", MODELS["openai"])
    billed = run.calls[0][3] if len(run.calls) == 1 else None
    written, read = _cache_reported(run.sent[0] if len(run.sent) == 1 else {})
    ok = (
        len(run.calls) == 1
        and run.calls[0][1]
        and billed == completion
        and price is not None
        and run.calls[0][4] == pricing.usd(run.calls[0][2], completion, price,
                                           cache_written=written, cache_read=read)
    )
    return ok, (
        f"reported completion_tokens {completion} (reasoning_tokens {reasoning} inside it); the row "
        f"says tokens_out_billed {billed} - not {completion + reasoning} (reasoning twice) and not "
        f"{completion - reasoning} (visible only); {written} prompt token(s) written to the cache, "
        f"{read} read from it, each at its published rate"
    )


def _cache_reported(record: dict[str, Any]) -> tuple[int, int]:
    """The cache writes and reads an OpenAI usage block reported inside `prompt_tokens`."""
    details = ((record.get("usage") or {}).get("prompt_tokens_details") or {})
    return details.get("cache_write_tokens", 0), details.get("cached_tokens", 0)


# --- 7 to 9: the cap ------------------------------------------------------------------------------------


async def check_seven(ctx: Install) -> tuple[bool, str]:
    """Plan check 7: meter at the cap, a job reaching stage 6 parks `over spend cap` with zero
    outbound provider requests.

    §8: "paid stages (6) never auto-retry past the spend cap", and decision 348 puts the question in
    the driver's gate so it is asked BEFORE stage 6 runs. The cap is set to the month's own spend, the
    title walks into stage 6, and the double's request log for that drain must be empty, the meter
    unchanged and no Fetcher built - the review's "the fetcher factory is never called for a title
    the gate parks", held by this instrument and not only by the suite. The park is decision 336's
    deferral: the board shows the wait's end and the task is pending until then with its attempt
    handed back.
    """
    spent = await spend.spent(ctx.conn)
    if not spent > 0:
        raise PreconditionFailed(f"checks 1 to 6 metered {spent} USD, so there is no month to cap")
    await assign(ctx, "gemini")
    await registry.save_connector(ctx.conn, spend.SETTINGS, cap_usd=float(spent))
    if await spend.cap(ctx.conn) != spent:
        raise PreconditionFailed(f"the cap reads {await spend.cap(ctx.conn)} after setting {spent}")
    title_id = await new_title(ctx)
    metered = await ctx.conn.fetchval("SELECT count(*) FROM llm_call")
    built = ctx.fetchers_built
    report, sent = await drain(ctx)
    walk = next((task for task in report.tasks if task.title_id == title_id), None)
    if walk is None:
        raise PreconditionFailed(f"the drain leased {report.leased} task(s) and not title {title_id}")
    board = await board_of(ctx.conn, title_id)
    task = await task_of(ctx, title_id)
    ctx.capped_title, ctx.capped_reason = title_id, walk.reason
    ctx.capped_board, ctx.capped_task = board, task
    now = datetime.now(UTC)
    ok = (
        walk.stage == STAGE_SIX.number
        and walk.status == pipeline.PARKED
        and walk.reason.startswith(OVER_CAP)
        and sent == []
        and ctx.fetchers_built == built
        and await ctx.conn.fetchval("SELECT count(*) FROM llm_call") == metered
        and board.get("reason") == walk.reason
        and board.get("retry_after") is not None and board["retry_after"] > now
        and task.get("state") == queue.PENDING
        and task.get("attempts") == 0
        and task.get("next_attempt_at") == board.get("retry_after")
    )
    return ok, "\n".join([
        f"cap set to the month's own spend: {spent} USD",
        f"title {title_id}: stage {walk.stage} {walk.status}: {walk.reason}",
        f"{len(sent)} provider request(s); llm_call rows {metered} before and "
        f"{await ctx.conn.fetchval('SELECT count(*) FROM llm_call')} after; task "
        f"{task.get('state')} with {task.get('attempts')} attempt(s) spent, due "
        f"{task.get('next_attempt_at')}",
        f"fetcher(s) built for the drain: {ctx.fetchers_built - built} - the gate is asked before any "
        f"Fetcher is built - and it sent {len(sent)} request(s)",
    ])


async def check_eight(ctx: Install) -> tuple[bool, str]:
    """Plan check 8: the same job on the next tick is still parked; no auto-retry.

    The park deferred the task to its deadline, and `queue.lease` claims `next_attempt_at <= now()`
    only, so the next tick does not hand it out at all: no walk, no gate asked, no request, and the
    board and the task exactly as the park left them. "Still parked" is read off the board as well
    as compared with it - at stage 6, under the over-cap sentence - so a title check 7 let through
    and parked somewhere else cannot pass here by standing still.
    """
    if not ctx.capped_title:
        raise PreconditionFailed("check 7 parked no title, so there is nothing to watch")
    metered = await ctx.conn.fetchval("SELECT count(*) FROM llm_call")
    report, sent = await drain(ctx)
    walked = [task for task in report.tasks if task.title_id == ctx.capped_title]
    board = await board_of(ctx.conn, ctx.capped_title)
    task = await task_of(ctx, ctx.capped_title)
    ok = (
        not walked
        and sent == []
        and await ctx.conn.fetchval("SELECT count(*) FROM llm_call") == metered
        and board.get("stage") == STAGE_SIX.number
        and board.get("status") == pipeline.PARKED
        and str(board.get("reason") or "").startswith(OVER_CAP)
        and board == ctx.capped_board
        and task == ctx.capped_task
    )
    return ok, (
        f"the tick leased {report.leased} task(s), walked title {ctx.capped_title} "
        f"{len(walked)} time(s) and sent {len(sent)} provider request(s); board and task "
        f"{'unchanged' if board == ctx.capped_board and task == ctx.capped_task else 'CHANGED'}: "
        f"{board.get('status')} at stage {board.get('stage')}"
    )


async def check_nine(ctx: Install) -> tuple[bool, str]:
    """Plan check 9: an admin retry over the cap is refused with that reason; nothing queued.

    Decision 330's revive button is M5.6's, and `spend.retry_refusal` is the advice its route calls
    before it makes a task due - so the refusal is read from there, and must be the board's sentence
    word for word, because one state has one answer. Then the retry is made anyway, the way
    `ops/m53_exit_criterion.py` check 5 makes one - the task due now - which is the guarantee half:
    however a task is made due, the drain walks it back into the gate, it re-parks under the same
    sentence with nothing asked, and afterwards no task is due and none was added.
    """
    if not ctx.capped_title:
        raise PreconditionFailed("check 7 parked no title, so there is nothing to retry")
    refusal = await spend.retry_refusal(ctx.conn, title_id=ctx.capped_title)
    tasks = await ctx.conn.fetchval("SELECT count(*) FROM acquisition_task")
    metered = await ctx.conn.fetchval("SELECT count(*) FROM llm_call")
    await ctx.conn.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, ctx.keys[ctx.capped_title],
    )
    report, sent = await drain(ctx)
    walk = next((task for task in report.tasks if task.title_id == ctx.capped_title), None)
    due = await ctx.conn.fetchval(
        "SELECT count(*) FROM acquisition_task WHERE state = $1 AND next_attempt_at <= now()",
        queue.PENDING,
    )
    ok = (
        refusal is not None
        and refusal.startswith(OVER_CAP)
        and refusal == ctx.capped_reason
        and walk is not None
        and walk.status == pipeline.PARKED
        and walk.reason == refusal
        and sent == []
        and await ctx.conn.fetchval("SELECT count(*) FROM llm_call") == metered
        and await ctx.conn.fetchval("SELECT count(*) FROM acquisition_task") == tasks
        and due == 0
    )
    same = "the same sentence" if refusal == ctx.capped_reason else "NOT the sentence"
    return ok, "\n".join([
        f"spend.retry_refusal: {refusal}",
        f"{same} the board shows for title {ctx.capped_title}",
        f"made due anyway: the drain re-parked it "
        f"({walk.status if walk else 'not leased'}, same reason: "
        f"{walk is not None and walk.reason == refusal}); {len(sent)} provider request(s); "
        f"{due} task(s) due now; acquisition_task rows {tasks} before, "
        f"{await ctx.conn.fetchval('SELECT count(*) FROM acquisition_task')} after",
    ])


# --- 10 to 12: no key in a url, a stored row or a log line ----------------------------------------------


def _key_spellings(ctx: Install) -> list[str]:
    """Every key the run stored, raw and percent-encoded, which is how a url would carry one."""
    return sorted({form for key in ctx.double.KEYS.values() for form in (key, quote(key, safe=""))})


def _carries_a_key(text: str, keys: list[str]) -> bool:
    return any(key in text or key in unquote(text) for key in keys)


def _redacted(text: str, keys: list[str]) -> str:
    """`text` with every key taken out, for a failure line that has to show WHERE a key landed.

    These keys are the double's literals and bill nobody, but a report that printed them would be
    teaching the shape the criterion forbids: the double's own log redacts for that reason, and a
    failing run's console is copied into issues and chat as readily as a log is.
    """
    shown = unquote(text) if _carries_a_key(text, keys) else text
    for key in keys:
        shown = shown.replace(key, "[redacted]")
    return shown


# A url whose query names a key, in every spelling Gemini's reference and a "simplification" would
# reach for: `?key=`, `&key=`, `?api_key=`, `?apikey=`.
_KEY_IN_URL = re.compile(r"[?&](?:api_?key|key)\b", re.IGNORECASE)


def url_builders_naming_a_key(source: str) -> list[str]:
    """Every string literal or f-string in `source` that builds a url naming a key, and every call
    passing `params=` - which is how a query is added without writing a `?` at all.

    A bare string statement is prose and builds no url, so a module may still SAY that Gemini's
    documented `?key=` is the trap. `test_llm_adapters.py` reads the package the same way on every
    run of the suite; this is the criterion's own reading, restated so the script depends on no
    test module.
    """
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(kw.arg == "params" for kw in node.keywords):
            hits.append(f"line {node.lineno}: params=")
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(part.value if isinstance(part, ast.Constant) else "{}"
                           for part in node.values)
        else:
            continue
        if _KEY_IN_URL.search(text):
            hits.append(f"line {node.lineno}: {text[:60]!r}")
    return hits


def provider_requests(ctx: Install) -> list[dict[str, Any]]:
    return [request for request in ctx.double.state.requests if request.get("provider") in PROVIDERS]


async def check_ten(ctx: Install) -> tuple[bool, str]:
    """Plan check 10: no URL built by `llm/` carries a key.

    TWO READINGS, and each covers what the other cannot. Every url the double received across the
    whole run - every paid call checks 1 to 9 made - is the provider's documented endpoint exactly,
    with no query string, and arrived with the key in the header that provider documents
    (`x-goog-api-key`, `x-api-key`, `Authorization: Bearer`); the double records whether a key was
    in the url, decoded, without ever keeping the key. That is every url a run produced. The read of
    `backend/spielplan/llm/*.py` is every url the package COULD build, a branch no scenario reached
    included: no literal or f-string naming a key in a query, and no `params=`.
    """
    requests = provider_requests(ctx)
    by_provider = {p: [r for r in requests if r["provider"] == p] for p in PROVIDERS}
    keys = _key_spellings(ctx)
    wrong = [
        f"{r['provider']} {r['method']} {r['url']} (key in url {r.get('key_in_url')}, "
        f"header {r.get('key_header')})"
        for r in requests
        if r.get("key_in_url") or not r.get("key_header") or _carries_a_key(r["url"], keys)
        or r["url"] != ENDPOINTS[r["provider"]].format(model=MODELS[r["provider"]])
    ]
    modules = sorted((ROOT / "backend" / "spielplan" / "llm").glob("*.py"))
    builders = {
        path.name: hits for path in modules
        if (hits := url_builders_naming_a_key(path.read_text(encoding="utf-8")))
    }
    stray = [r for r in ctx.double.state.requests if r.get("provider") not in PROVIDERS]
    ok = (
        all(by_provider[p] for p in PROVIDERS)
        and not wrong
        and not stray
        and len(modules) >= 4
        and not builders
    )
    return ok, "\n".join(
        [f"{len(requests)} provider request(s) over the run: "
         + ", ".join(f"{p} {len(by_provider[p])}" for p in PROVIDERS)
         + f"; {len(wrong)} with a key in the url, off the documented endpoint or without the "
         "documented key header; "
         f"{len(stray)} to a path no provider serves"]
        + [f"  {_redacted(line, keys)[:150]}" for line in wrong[:6]]
        + [f"read {len(modules)} module(s) under backend/spielplan/llm/: "
           + (f"url builders naming a key: {builders}" if builders else
              "no url naming a key and no params=")]
    )


async def check_eleven(ctx: Install) -> tuple[bool, str]:
    """Plan check 11: `raw_document.url` for a provider call carries no key.

    Every answer stage 6 received is kept in the raw store under the task's key (§8's preamble), and
    `raw_document.url` is where a key in a query string would have been written for good - into the
    database, the nightly dump and every restore of it. So every provider row is read: the documented
    endpoint exactly, no query, no key raw or percent-encoded. One row per answered request, so the
    reading covers every call and not a sample.
    """
    rows = await ctx.conn.fetch(
        "SELECT id, source, url FROM raw_document WHERE source LIKE 'llm:%' ORDER BY id"
    )
    keys = _key_spellings(ctx)
    answered = [r for r in provider_requests(ctx) if r.get("status") == 200]
    wrong = [
        f"raw_document {row['id']} ({row['source']}): {row['url']}"
        for row in rows
        if _carries_a_key(row["url"], keys) or "?" in row["url"]
        or row["url"] != ENDPOINTS.get(row["source"].split(":", 1)[1], "").format(
            model=MODELS.get(row["source"].split(":", 1)[1], ""))
    ]
    stored = {p: sum(1 for row in rows if row["source"] == f"llm:{p}") for p in PROVIDERS}
    ok = bool(rows) and len(rows) == len(answered) and all(stored.values()) and not wrong
    return ok, "\n".join(
        [f"{len(rows)} provider answer(s) in the raw store for {len(answered)} answered request(s) "
         f"({', '.join(f'{p} {n}' for p, n in stored.items())}); {len(wrong)} url(s) off the "
         "documented endpoint or carrying a key"]
        + [f"  {_redacted(line, keys)[:150]}" for line in wrong[:6]]
    )


async def check_twelve(ctx: Install) -> tuple[bool, str]:
    """Plan check 12: the httpx INFO line for a provider call carries no key.

    httpx writes one INFO line per request with the full url, and this app already had to install a
    filter for that line once (`push/send.py`). Every line the `httpx` logger wrote for a provider
    host is read, and there must be one per provider request the double recorded - fewer would mean
    the capture missed calls, and a reading of none would pass anything. The app's own log is swept
    beside it: every record the `spielplan` logger wrote during the run, the LLM layer's and the
    driver's, holds no key either.
    """
    keys = _key_spellings(ctx)
    hosts = list(ctx.double.HOSTS.values())
    lines = [record.getMessage() for record in ctx.httpx_log.records]
    provider_lines = [line for line in lines if any(host in line for host in hosts)]
    leaking = [line for line in provider_lines if _carries_a_key(line, keys)]
    app_lines = [record.getMessage() for record in ctx.app_log.records]
    app_leaking = [line for line in app_lines if _carries_a_key(line, keys)]
    requests = provider_requests(ctx)
    ok = (
        bool(provider_lines)
        and len(provider_lines) == len(requests)
        and not leaking
        and not app_leaking
    )
    return ok, "\n".join(
        [f"{len(provider_lines)} httpx INFO line(s) for {len(requests)} provider request(s); "
         f"{len(leaking)} carrying a key",
         f"{len(app_lines)} line(s) from the app's own logger over the run; {len(app_leaking)} "
         "carrying a key"]
        + [f"  e.g. {console(_redacted((leaking or provider_lines)[0], keys)[:150])}"
           if provider_lines else "  no line captured"]
    )


# --- 13 and 14: prices --------------------------------------------------------------------------------


async def check_thirteen(ctx: Install) -> tuple[bool, str]:
    """Plan check 13: `price_for("openai", "gpt-5.6-terra")` is the terra price, NOT `gpt-5`'s.

    The corpus's docstring calls a bare `startswith` "how a cost estimate quietly invents a number",
    and decision 343 ports its boundary rule verbatim. So three answers are read: terra is the
    table's own terra row, gpt-5 is its own row and a different one, and a point release nobody has
    priced is None rather than gpt-5's price inherited. And the rule is read where it costs money:
    every OpenAI call this run metered on gpt-5.6-terra was charged at the terra price, and at gpt-5's
    it would have been a different figure.
    """
    terra = pricing.price_for("openai", "gpt-5.6-terra")
    gpt5 = pricing.price_for("openai", "gpt-5")
    point_release = pricing.price_for("openai", POINT_RELEASE)
    rows = await ctx.conn.fetch(
        "SELECT tokens_in, tokens_out_billed, usd, response_document_id FROM llm_call"
        " WHERE provider = 'openai' AND model = $1 AND ok ORDER BY id", MODELS["openai"],
    )
    priced = terra is not None and gpt5 is not None
    # Each row priced with the cache breakdown its own stored envelope reported (check 6's note).
    caches = [_cache_reported(await rawstore.read_json(ctx.conn, row["response_document_id"]))
              for row in rows]
    charged = priced and all(
        row["usd"] == pricing.usd(row["tokens_in"], row["tokens_out_billed"], terra,
                                  cache_written=written, cache_read=read)
        and row["usd"] != pricing.usd(row["tokens_in"], row["tokens_out_billed"], gpt5,
                                      cache_written=written, cache_read=read)
        for row, (written, read) in zip(rows, caches, strict=True)
    )
    ok = (
        priced
        and terra == pricing.PRICING["openai"]["gpt-5.6-terra"][0]
        and gpt5 == pricing.PRICING["openai"]["gpt-5"][0]
        and terra != gpt5
        and point_release is None
        and bool(rows)
        and charged
    )

    def shown(price: pricing.ModelPrice | None) -> str:
        return "None" if price is None else f"{price.input:.2f} in / {price.output:.2f} out"

    return ok, "\n".join([
        f"gpt-5.6-terra -> {shown(terra)} USD per 1M tokens; gpt-5 -> {shown(gpt5)}; "
        f"{POINT_RELEASE} -> {shown(point_release)}",
        f"{len(rows)} metered gpt-5.6-terra call(s), every one charged at the terra price and "
        f"none at gpt-5's: {charged}",
    ])


async def check_fourteen(ctx: Install) -> tuple[bool, str]:
    """Plan check 14: `price_for` for an unknown model is None, and the estimator says "unknown".

    Decision 343: an estimator that invents a number is worse than one that says it does not know.
    Read at three altitudes. The table answers None for a model it has never heard of, and the
    per-title estimate over that None is None rather than a sum that quietly left a provider out.
    Then the shipped read M5.7's card will render - `GET /api/admin/llm`'s handler, called on this
    run's connection, since who may call it is `test_api_gating.py`'s question and not this one's -
    with OpenAI configured on that model and assigned the extraction: its card's price and the
    estimate both "unknown", and the estimate's reason naming the model. And the gate: stage 6
    cannot hold a cap against a price nobody knows, so `spend.cap_check` refuses naming the model.
    """
    table = pricing.price_for("openai", UNPRICED_MODEL)
    estimate = pricing.estimate_title(tokens_in=pricing.SPEC_INPUT_TOKENS, prices=[table], passes=1)
    await registry.save_connector(ctx.conn, "openai", model=UNPRICED_MODEL)
    await assign(ctx, "openai")
    read = await llm_api.llm_settings(None, ctx.conn)
    card = next((c for c in read["providers"] if c["name"] == "openai"), {})
    shown = read["estimate"]
    gate = await spend.cap_check(ctx.conn, title_id=ctx.next_title - 1)
    ok = (
        table is None
        and estimate is None
        and card.get("price") == "unknown"
        and card.get("configured") is False
        and shown.get("per_title_usd") == "unknown"
        and UNPRICED_MODEL in (shown.get("reason") or "")
        and gate is not None
        and gate.kind == spend.PLAN
        and UNPRICED_MODEL in gate.reason
    )
    return ok, "\n".join([
        f"price_for('openai', {UNPRICED_MODEL!r}) -> {table}; estimate_title over it -> {estimate}",
        f"GET /api/admin/llm: openai card price {card.get('price')!r}, configured "
        f"{card.get('configured')}; per-title estimate {shown.get('per_title_usd')!r}",
        f"the gate: {gate.kind if gate else None}: {(gate.reason if gate else '')[:150]}",
    ])


# --- the run ------------------------------------------------------------------------------------------


RUNNERS = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five, 6: check_six,
    7: check_seven, 8: check_eight, 9: check_nine, 10: check_ten, 11: check_eleven,
    12: check_twelve, 13: check_thirteen, 14: check_fourteen,
}
RECORDED: set[int] = set()


async def measure(number: int, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's
    fourteen: a run that stops at three and prints "3/3 checks passed" is the failure mode an exit
    criterion exists to rule out. A refused precondition is reported without its traceback - the
    sentence IS the diagnosis.
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


async def report_cost(ctx: Install) -> None:
    """What this run's calls would have cost, read off `llm_call` - plan §9: "keep the currency
    plain". Nothing was billed; the figures are the dated table's prices over the usage the double
    reported, which is exactly what the meter sums for a real month."""
    rows = await ctx.conn.fetch(
        "SELECT provider, model, count(*) AS calls, count(*) FILTER (WHERE ok) AS answered,"
        "       sum(tokens_in) AS tokens_in, sum(tokens_out_billed) AS tokens_out, sum(usd) AS usd"
        "  FROM llm_call GROUP BY provider, model ORDER BY provider, model"
    )
    meter = await spend.meter(ctx.conn)
    print("\nWhat these calls would have cost (llm_call; USD at the dated price table; no money was "
          "spent - every answer came from ops/fake_llm.py)", flush=True)
    print(f"  {'provider':10} {'model':18} {'calls':>5} {'answered':>8} {'tokens in':>10} "
          f"{'tokens out':>10} {'USD':>10}", flush=True)
    for row in rows:
        print(f"  {row['provider']:10} {console(row['model'])[:18]:18} {row['calls']:>5} "
              f"{row['answered']:>8} {row['tokens_in']:>10,} {row['tokens_out']:>10,} "
              f"{row['usd']:>10}", flush=True)
    print(f"  meter for {month_of(meter)} ({console(str(meter['tz']))}): "
          f"{meter['spent_usd']} USD spent against a cap of {meter['cap_usd']} USD", flush=True)


def report_app_warnings(ctx: Install) -> None:
    """The app's own warnings over the run, once each with a count, instead of once per read."""
    counted: dict[str, int] = {}
    for record in ctx.app_log.records:
        if record.levelno >= logging.WARNING:
            line = f"{record.name} {record.levelname}: {record.getMessage()}"
            counted[line[:200]] = counted.get(line[:200], 0) + 1
    print(f"\nThe app logged {sum(counted.values())} warning(s) or worse during the run, "
          f"{len(counted)} distinct:", flush=True)
    for line, times in sorted(counted.items(), key=lambda item: -item[1])[:6]:
        print(f"  x{times}  {console(line)}", flush=True)


async def main() -> int:
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it: this script creates a")
        print("scratch database on that server and drops it again, and names no other.")
        return 2
    if not (ROOT / "ops" / "fake_llm.py").is_file():
        print("ops/fake_llm.py is missing: every check below is measured against that double, and")
        print("this script will not stand in a server of its own for it.")
        return 2

    print("\nM5.5 exit criterion -- the LLM connector layer, against ops/fake_llm.py\n", flush=True)

    # Made before the CREATE, because nothing that can fail may run between the CREATE and the block
    # whose finally drops the database (`test_static_contracts.py`'s scratch-window rule). The two
    # captures are attached as the first thing inside it, before anything the app logs: check 12
    # sweeps every line, and a warning logged with no handler attached would reach the console
    # through `logging`'s last-resort handler, unescaped and out of place.
    started_in = Path.cwd()
    httpx_log, app_log = Captured(), Captured()
    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global. Named with this run's pid so two concurrent runs cannot drop each other's.
    scratch = f"spielplan_m55_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it: everything that can fail after
    # the CREATE - the mkdtemp, the connect, the codec registration, the migrations, the fixture
    # build - happens inside the `try`, because the scratch name carries this run's pid and an orphan
    # nothing will ever name again is a leak on the household's own server.
    # [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    ctx: Install | None = None
    try:
        logging.getLogger("httpx").addHandler(httpx_log)
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("spielplan").addHandler(app_log)
        logging.getLogger("spielplan").setLevel(logging.INFO)
        work = Path(tempfile.mkdtemp(prefix="spielplan-m55-exit-"))
        # The raw store's root as well as the artifacts': every provider answer stage 6 keeps lands
        # under `work/raw` exactly as `rawstore.resolve` puts it there in production.
        os.environ["DATA_DIR"] = str(work)
        os.environ["DATABASE_URL"] = scratch_dsn
        _neutralise_connector_env()
        core_config.settings.cache_clear()
        conn = await asyncpg.connect(scratch_dsn)
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The install this run measures (spec sections 9 and 8 stage 6; decision 435)",
              flush=True)
        ctx = await build_install(conn, work, httpx_log, app_log)

        print("\n1. Three violations on three providers: refused, retried once (plan check 1)",
              flush=True)
        await measure(1, ctx)
        print("\n2. The retry as each provider received it (plan check 2)", flush=True)
        await measure(2, ctx)
        print("\n3. The violation repeated: stage 6 fails for good, the tier stands (plan check 3)",
              flush=True)
        await measure(3, ctx)
        print("\n4. One outcome across three structured-output mechanisms (plan check 4)",
              flush=True)
        await measure(4, ctx)
        print("\n5. Gemini's thinking tokens are billed output (plan check 5)", flush=True)
        await measure(5, ctx)
        print("\n6. OpenAI's completion tokens as they come (plan check 6)", flush=True)
        await measure(6, ctx)
        print("\n7. The month at its cap: parked before the paid call (plan check 7)", flush=True)
        await measure(7, ctx)
        print("\n8. The next tick: no auto-retry (plan check 8)", flush=True)
        await measure(8, ctx)
        print("\n9. The admin retry over the cap (plan check 9)", flush=True)
        await measure(9, ctx)
        print("\n10. Every url the LLM layer built or could build (plan check 10)", flush=True)
        await measure(10, ctx)
        print("\n11. The raw store's url column (plan check 11)", flush=True)
        await measure(11, ctx)
        print("\n12. The httpx INFO line, and the app's own log (plan check 12)", flush=True)
        await measure(12, ctx)
        print("\n13. gpt-5.6-terra priced as itself (plan check 13)", flush=True)
        await measure(13, ctx)
        print("\n14. A model nobody priced (plan check 14)", flush=True)
        await measure(14, ctx)

        await report_cost(ctx)
        report_app_warnings(ctx)
    except Exception as exc:
        # Everything outside a check: the connect, the migrations, the fixture build, the vocabulary
        # load. Reported as the failures they are, so the exit code stays non-zero and the run still
        # ends in a score rather than a traceback where the sentence naming the cause belongs. It
        # catches everything on purpose, for the reason `ops/m51_exit_criterion.py` states: narrowing
        # it to `asyncpg.PostgresError` would send an `OSError` out of the fixture build back to the
        # bare traceback that loses every section below it and the tally.
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
        logging.getLogger("httpx").removeHandler(httpx_log)
        logging.getLogger("spielplan").removeHandler(app_log)
        if conn is not None:
            await conn.close()
        # Out of the neutral directory before the tree holding it is removed: Windows refuses to
        # delete the directory a process stands in. [M5.3 review cycle 1, M53-EXIT-04]
        os.chdir(started_in)
        discard_staged_artifacts(work, None)
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
