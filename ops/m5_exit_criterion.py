"""§12's M5 row: a new Jellyfin add reaches "ready" unattended, measured in-process on the fixture.

§12's M5 row names this script (decisions 331 and 465), and the nine checks below are
`docs/milestones/M5-plan.md` §7's own pass table, numbered as it numbers them and in its order. The
row's sentence is what they add up to:

    a new Jellyfin add reaches "ready" unattended - and, measured with it by
    `ops/m5_exit_criterion.py`: the flywheel appends a naming failure at the moment it happens and
    an admin launches exactly the batch they selected; the spend cap parks a paid stage with its
    reason instead of billing for it, and never auto-retries past it; a provider response that
    violates the extraction contract is retried exactly once with the violation named, and a second
    violation fails the stage and writes nothing; a re-derive of a title carrying a curated
    correction still carries it afterwards; and a burst of adds for one series yields one job for
    the show rather than one per episode

      script  plan  what it measures
        1       1   an ItemAdded for a film the bundle lacks, swept and drained, is `ready` at stage
                    10 as an acquired title, with no admin route and no admin action's reason on
                    the way
        2       2   in that walk: the augmented pack, every tag's evidence, stage 7's record of the
                    refusals stage 6 filed, stage 8's projected rows - and an upgraded install's
                    expired stage-6 no-pack park re-entering at stage 5 (decision 467)
        3       3   that walk made two llm_call rows, and its second request named the violated rule
                    and the offending value as the provider received it
        4       4   the violation repeated: stage 6 fails for good, the tier stands, no retry coming
        5       5   the walked title's thin-facet row is in GET /api/admin/flywheel when drain returns
        6       6   a launch of two of three open rows: the only rows running and the only titles
                    due, both re-extracted from stage 5 under the batch plan, the third untouched,
                    the bundle's projected rows byte-identical (decision 463)
        7       7   a shipped and a household credit correction each stand after a board retry from
                    stage 3
        8       8   twelve episode adds for one series in one window file one task, for the show
        9       9   the cap at the month's own spend: a fresh add parks `over spend cap` at stage 6
                    asking no provider, and the next tick the same

WHAT ONLY A REAL INSTALL SIGNS, said before anything else because decision 184's rule is that a
certificate names what it did not measure. Clause one against a real Jellyfin and its Webhook
plugin, real source sites, a real provider's billed call, the corpus tower and a wall-clock window;
clause five against the corpus's own ledger, which `ops/m53_exit_criterion.py` measures on the real
bundle; and decision 466's drain timing - one tick with stage 6 live on decision 324's default plan,
each attempt's `llm_call.at` read against the tick's start, and whether `DRAIN_LIMIT` titles fit the
job's 420 s - because that latency is the provider's, and a figure taken against this script's double
would be about nothing. `docs/RELEASE.md` records it as owed on the owner's box.

IT RUNS ON THE FIXTURE, AND REFUSING THE FIXTURE WOULD MEASURE NOTHING (decision 465). The M5.1 to
M5.3 scripts refuse it for population arguments that are theirs - names that collide under stage 1's
resolver, the corpus tower, the real ledger - and each measures its own on the real bundle. What §12
says of M5 is compositional: that the stages seven milestones shipped hand one title from each to the
next with nobody in between. A fixture falsifies a composition as well as a corpus does, and a script
that refused it would leave the umbrella unrunnable in every lane. So each check is a function over
an `Install` that this `main()` builds once on a scratch database and runs in the plan's order, and
that `backend/tests/test_m5_exit_criterion.py` builds once per test on the suite's own database,
composing only the steps its clause needs - so CI runs every clause on every push.

EVERYTHING BELOW IS THE SHIPPED PATH, and the two doubles are refusers, not mocks. The add is
`ops/fake_jellyfin.py`'s own emitter rendering the Webhook plugin's template and posting it at
`POST /events/jellyfin` over ASGI, under the token the admin PUT minted and showed once (decision
332); `intake.sweep_pending` files it once the window closes, the window moved in the database that
wrote it, as `ops/m52_exit_criterion.py` moves it; and `worker._tick` runs the shipped
`acquisition-drain` job, which walks it under the `job_run` row that tick opened (decision 468) and
decision 348's gate as shipped. Every fetch goes through the real `acquire.fetch.Fetcher`, built by
`pipeline._default_fetcher` - the one the worker's drain reaches - over one routing transport: the three
provider hosts to `ops/fake_llm.py`, the eight source hosts to `ops/m53_exit_criterion.py`'s canned
web, the Jellyfin host to the Jellyfin double - and any other name fails as a name that does not
resolve. Keys, models, the assignment and the cap are written through `registry.save_connector`; the
Launch, the board's retry and the correction editor are their admin routes; and every row a check
reads was written by the app.

WHAT IS PUT IN PLACE BY HAND IS WHAT NO MILESTONE OWNS. Two films in the Jellyfin double's library -
the one clause one adds and the one clause nine adds - because "a film the bundle lacks" is a film
this script names, filed the way `backend/tests/test_jellyfin_intake.py` files its GUID titles. The
fixture bundle is imported by the importer, so the vocabulary, the alias map, the shipped correction
and the Cold Tower stage 9 places against are the fixture's (`backend/tests/fixtures/make_bundle.py`,
shaped from a real export). Clause two's upgraded park is walked into by the driver and then aged,
both columns moved together as `_record_stop` wrote them as one value.

THE CANNED WEB IS EXTENDED AND NOT COPIED. `ops/m53_exit_criterion.py`'s films cannot reach `ready`
by design: its walk film serves one review source and parks at stage 4, and its one TMDB keyword maps
to nothing in the fixture's alias map, so stage 8 would project nothing. So `Web` below subclasses
that one - its route table, its robots file, its request log and its body builders - and serves this
script's own films beside it: a plot, two review sources over `dna/packs.MIN_WORDS` words each, a
Wikipedia article with craft sections so the pack carries its supplement, and `slow-burn`, which the
fixture's alias map carries.

CHECK 9 RUNS LAST BECAUSE IT CLOSES THE MONTH: the cap is set to exactly `spend.spent`, decision 325's
SUM over the `llm_call` rows checks 1 to 7 made, and never to a row this script inserted to look like
spend. No default cap ships (decision 325); this run sets one with room at the start because a Launch
and every walk past stage 6 need one.

NO REAL KEY AND NO SOCKET (decision 435). Every connector variable `Settings` would seed from is
removed from the environment unread, in either spelling, and so are both doubles' override
variables, so every provider key this run seals is a literal in `ops/fake_llm.py` and every source
key a literal here. NOTHING IS BILLED.

The exit codes are 0 (all nine held), 1 (a check failed) and 2 (a precondition refused before the
run: no Postgres to create a scratch database on, or a double that will not import). It creates and
drops its own DATABASE, named with this run's pid, stages into a temporary DATA_DIR that it removes,
and connects through `db/pool._init_connection`, never a bare `asyncpg.connect`, because the
json/jsonb codec is what every reader of `detail` depends on. Output is ASCII: every string this
script did not author goes through `console()` on the way out.

Run it against a live Postgres:

    TEST_DATABASE_URL=postgresql://... backend/.venv/Scripts/python ops/m5_exit_criterion.py
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s reporting helpers and `m53_exit_criterion`'s canned
# web are imported rather than copied: both scripts' recorded runs have to stay reproducible, so
# they are read and never edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181 made
# §2's required config a refusal at construction. Set outright rather than defaulted, for the reason
# every exit script gives: nothing here may be sealed under a household's real SECRETS_KEY.
SESSION_SECRET = "m5-exit-criterion-session-secret-not-a-real-one"
SECRETS_KEY = "m5-exit-criterion-secrets-key-not-a-real-one"
os.environ["SESSION_SECRET"] = SESSION_SECRET
os.environ["SECRETS_KEY"] = SECRETS_KEY
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import m53_exit_criterion as canned  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan import worker  # noqa: E402
from spielplan.acquire import actions, fetch, intake, pipeline, queue, stages  # noqa: E402
from spielplan.connectors import registry  # noqa: E402
from spielplan.connectors.jellyfin import JellyfinClient  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import dna_terms, migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.derive import ledgers  # noqa: E402
from spielplan.dna import craft, packs, verify  # noqa: E402
from spielplan.flywheel import batch, thin  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.llm import spend  # noqa: E402

# The fixture bundle's builder, imported for the reason `ops/m55_exit_criterion.py` imports it: its
# files are shaped from a real export, and a bundle this script wrote out by hand would be this
# script's idea of one. Its rows are read below as data, never retyped.
from tests.fixtures import make_bundle as fixture_bundle  # noqa: E402

# Put back after the import above: `ops/m53_exit_criterion.py` sets both outright when it is first
# imported, as its own comment says it must, and the throwaway key that seals this run's secrets is
# this script's to name.
os.environ["SESSION_SECRET"] = SESSION_SECRET
os.environ["SECRETS_KEY"] = SECRETS_KEY

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "a new Jellyfin add reaches ready unattended, as an acquired title"),
    (2, "the DNA stages run in that walk, and an upgraded no-pack park re-enters at stage 5"),
    (3, "the walk is retried once, with the violated rule and value in the second request"),
    (4, "a second violation fails stage 6 for good and leaves the title's dna_tag rows unchanged"),
    (5, "the thin title's row is in GET /api/admin/flywheel when drain returns"),
    (6, "a launch takes exactly its rows, re-extracts from stage 5, and keeps bundle projection"),
    (7, "a re-derive from stage 3 keeps the shipped and the household credit correction"),
    (8, "twelve episode adds for one series in one window file one task, for the show"),
    (9, "the cap at the month's own spend parks a fresh add at stage 6 and never auto-retries"),
)

# The origin the stored connector names and the double's control host. Nothing resolves either:
# every read travels over an `ASGITransport`, so a transport somehow bypassed fails on DNS rather
# than reaching a server on the household's network (`ops/m52_exit_criterion.py`'s reason).
JELLYFIN_URL = "http://jellyfin.test"
JELLYFIN_CONTROL = "http://fake-jellyfin"
LLM_CONTROL = "http://fake-llm"
# Where the plugin pushes, which is the operator's "Webhook URL" field, and the one request clause
# one allows between the add and `ready`.
WEBHOOK_TARGET = "http://spielplan.test/events/jellyfin"
DELIVERY = "POST /events/jellyfin"
ADMIN_PREFIX = "/api/admin"

ADMIN_NAME = "m5-exit-criterion"
ADMIN_PASSWORD = "an-exit-criterion-password-9times"

# Eleven minutes rather than ten, for `ops/m52_exit_criterion.py`'s reason: `not_before` is
# `received_at` plus ten minutes to the microsecond, and a move of exactly ten leaves the row ripe by
# whatever the statement's own round trip took.
WINDOW_MINUTES = 11

# The Bear, which the Jellyfin double gives twelve episodes so §7.2's burst can be emitted in full.
BURST_SERIES = "jf-7"
BURST_EPISODES = 12

# Decision 343's table models, written into each provider's row the way an admin's card would, as
# `ops/m55_exit_criterion.py` writes them. The stored assignment is Gemini; clause six's batch names
# Anthropic, so a launched walk that asked Gemini would be one that ignored the batch plan.
PROVIDERS = ("gemini", "anthropic", "openai")
MODELS = {"gemini": "gemini-3.7-flash", "anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra"}
ASSIGNED = "gemini"
BATCH_PROVIDER = "anthropic"

# Room under the cap for checks 1 to 8, in USD for the calendar month: far above what the run's calls
# cost at the table's prices, so nothing before check 9 parks for spend.
CAP_WITH_ROOM = 1000

# The criterion's words for the park, matched as its own literal rather than as the app's constant,
# so a reason the app re-worded is caught here and not agreed with.
OVER_CAP = "over spend cap"
# The no-pack reason's own words: one of the two substrings decision 467's restatement of it keeps,
# `stage 5` being the other.
NO_PACK = "no DNA pack is stored"

# The violation the double's first answer carries by default, and the rule M5.4's `verify_payload`
# refuses it under: a term the vocabulary does not carry.
CONTENT = "fabricate"
RULE = "unknown_term"

# The shipped drain's row in `worker.JOBS`, which `drain` has `worker._tick` run alone, and the name
# `_record_start` files that tick's `job_run` row under - the row `drain` reads the run back from.
DRAIN_JOB = "acquisition-drain"

# The hosts `ops/m53_exit_criterion.py`'s `Web._route` answers: §8 stage 2's eight sources.
WEB_HOSTS = frozenset({
    "api.themoviedb.org", "query.wikidata.org", "www.omdbapi.com", "api.trakt.tv",
    "en.wikipedia.org", "api.tvmaze.com", "www.rottentomatoes.com", "www.metacritic.com",
})

# The three keyed sources' credentials, this run's own, so all eight of stage 2's sources are asked
# (decision 377 filters a source whose key is absent). They never leave the process.
TMDB_KEY = "m5-exit-criterion-tmdb-key-not-a-real-one"
OMDB_KEY = "m5-exit-criterion-omdb-key-not-a-real-one"
TRAKT_CLIENT_ID = "m5-exit-criterion-trakt-client-id-not-a-real-one"

# The fixture titles the later checks use, by id, because the fixture fixes them: 4 carries a plot
# and no pack (clause two's upgraded park), 1 to 3 carry a thin tier and projected rows (clause six),
# and 8 is the title the shipped `corrections_v1.tsv` names (clause seven).
UPGRADED_TITLE = 4
LEFT_TITLE = 1
LAUNCHED_TITLES = (2, 3)
SHIPPED_TITLE = 8

# The keyword this script's films carry, which the fixture's alias map carries too, so stage 8 has a
# projection to write.
KEYWORD = "slow-burn"

# Every sentence an admin action writes on the board or the task, cut at its first placeholder, so
# clause one can say none of them was ever there.
ADMIN_REASONS = tuple(
    text.split("{", 1)[0]
    for text in (actions.RETRIED, actions.RETRIED_FAILED, actions.ABANDONED_REASON,
                 actions.ABANDONED_NOTE, batch.LAUNCHED)
)

# §8's stages as the driver's one tuple names them, read off it rather than retyped, and the paid
# stage found by its flag, as `acquire/actions.py`'s pre-check finds it (decision 464).
STAGE_NAMES = tuple(stage.name for stage in pipeline.STAGES)
EXTRACT_STAGE = next(stage.number for stage in pipeline.STAGES if stage.paid)
DERIVE, PACK, EXTRACT, VERIFY, PROJECT = (STAGE_NAMES[number - 1] for number in (3, 5, 6, 7, 8))


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


# --- the films ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Film:
    """One film this run's web serves: its identity, and the text its sources carry.

    A film with no `item_id` is one no Jellyfin add names - the fixture's corrected title, whose
    TMDB document is served so a walk can store it. A film with no `reception` has no Wikipedia
    article and no second review source.
    """

    name: str
    year: int
    imdb: str
    tmdb: int
    plot: str
    review: str | None = None
    reception: str | None = None
    item_id: str | None = None

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "-")

    @property
    def rt_slug(self) -> str:
        return f"m/{self.slug}"

    @property
    def mc_slug(self) -> str:
        return f"movie/{self.slug}"

    @property
    def article(self) -> str:
        return f"{self.name} (film)"


# Each review is over fifty words, `dna/packs.MIN_WORDS`, so both sources are in the pack and not
# only in stage 4's count. No apostrophe anywhere, so a quote the double cuts is the same string in
# every rendering this run prints.
REVIEW = (
    "The tension builds patiently across two hours and never once releases. It is a slow burn in "
    "the truest sense, a film that trusts its audience to wait through long silent scenes on a grey "
    "shore, and it rewards that patience with a final act so quiet and so exact that the whole town "
    "seems to hold its breath with the keeper. Few films this year are as sure of their own pace."
)
RECEPTION = (
    "Critics praised the film for its restraint. One reviewer called it a bleak and unforgiving "
    "portrait of a town that has agreed on what it will not remember, shot in grey light by a "
    "director who refuses every easy consolation. Another wrote that the performances are exact and "
    "that the score never once tells the audience how to feel, which leaves the silence of the "
    "harbour to do the work."
)
FILMING = (
    "Principal photography took place over nine weeks on a working harbour, with most scenes lit "
    "only by the lighthouse lamp and the grey winter sky over the water."
)
MUSIC = (
    "The score is written for a small string section and a foghorn recorded on location, and it "
    "enters only three times, each time after a long stretch of silence."
)

WALK_FILM = Film(
    name="The Keeper of Harbour Light", year=2025, imdb="tt95000001", tmdb=95001,
    plot=("A retired lighthouse keeper returns to the island town that turned him away and slowly "
          "uncovers who let his brother drown on the night of the winter storm."),
    review=REVIEW, reception=RECEPTION,
    item_id=uuid.uuid5(uuid.NAMESPACE_URL, "spielplan-m5-exit/walk").hex,
)
CAPPED_FILM = Film(
    name="The Salt Road Home", year=2025, imdb="tt95000002", tmdb=95002,
    plot=("A ferry mechanic crosses a frozen estuary on foot to reach the sister she has not spoken "
          "to in twenty years, and learns on the way why the ferry stopped running."),
    review=REVIEW, reception=RECEPTION,
    item_id=uuid.uuid5(uuid.NAMESPACE_URL, "spielplan-m5-exit/capped").hex,
)
_SHIPPED_ROW = next(row for row in fixture_bundle.TITLES if row[0] == SHIPPED_TITLE)
SHIPPED_FILM = Film(
    name=_SHIPPED_ROW[2], year=_SHIPPED_ROW[4], imdb=_SHIPPED_ROW[6], tmdb=_SHIPPED_ROW[7],
    plot="A truck driver helps a widow turn her failing noodle shop into the best one in town.",
)
FILMS = (WALK_FILM, CAPPED_FILM, SHIPPED_FILM)


def tmdb_detail_body(film: Film) -> bytes:
    """`ops/m53_exit_criterion.py`'s TMDB detail, re-pointed at one of this run's films.

    The builder is m53's so the shape is the one `derive/parse.parse_tmdb_detail` was measured
    against; what changes is what m53 holds fixed for its own reasons - its overview, its one
    keyword, its empty reviews. The composer is m53's wrong one, so a correction has a credit the
    derive really writes and the ledger really has to replace.
    """
    body = json.loads(canned.tmdb_detail_body(
        tmdb_id=film.tmdb, name=film.name, year=film.year, imdb_id=film.imdb,
        composer=canned.WRONG_COMPOSER,
    ))
    body["overview"] = film.plot
    body["keywords"] = {"keywords": [{"id": 95_100, "name": KEYWORD}]}
    reviews = [] if film.review is None else [{
        "id": f"m5-review-{film.tmdb}", "author": "a household critic",
        "author_details": {"rating": 8}, "content": film.review,
        "created_at": f"{film.year}-07-01T12:00:00.000Z",
        "url": f"https://www.themoviedb.org/review/m5-review-{film.tmdb}",
    }]
    body["reviews"] = {"page": 1, "results": reviews, "total_results": len(reviews),
                       "total_pages": 1 if reviews else 0}
    return json.dumps(body).encode("utf-8")


def wikidata_body(film: Film) -> bytes:
    """The SPARQL row `wikidata:resolve` reads, built with m53's own binding helper."""
    row = {
        "item": canned._binding(f"http://www.wikidata.org/entity/Q95{film.imdb[-6:]}", uri=True),
        "imdb": canned._binding(film.imdb),
        "rt": canned._binding(film.rt_slug),
        "mc": canned._binding(film.mc_slug),
        "article": canned._binding(
            "https://en.wikipedia.org/wiki/" + film.article.replace(" ", "_"), uri=True
        ),
    }
    return json.dumps({
        "head": {"vars": ["item", "imdb", "rt", "mc", "lb", "tmdb", "article"]},
        "results": {"bindings": [row]},
    }).encode("utf-8")


def wikipedia_article_body(film: Film) -> bytes:
    """An `explaintext` extract with two craft sections and a Reception section.

    m53's article has neither on purpose, so its walk film is left with one review source. This one
    carries both: `dna/craft.py` takes `Filming` and `Music` into the pack's supplement, and
    `derive/reviews.parse_wikipedia_reception` files `Reception` as the second review source.
    """
    text = (
        f"{film.article} is a film used by an exit criterion.\n\n\n"
        "== Plot ==\n" + film.plot + "\n\n\n"
        "== Production ==\nIt was produced for a measurement and released nowhere.\n\n"
        "=== Filming ===\n" + FILMING + "\n\n"
        "=== Music ===\n" + MUSIC + "\n\n\n"
        "== Reception ==\n" + (film.reception or "") + "\n"
    )
    return json.dumps({
        "batchcomplete": True,
        "query": {"pages": [{"pageid": film.tmdb, "title": film.article, "extract": text}]},
    }).encode("utf-8")


class Web(canned.Web):
    """`ops/m53_exit_criterion.py`'s canned web, with this run's films served beside its own.

    Every route falls through to m53's for anything that is not one of these films, so the robots
    file, the request log and the 404 for an unknown id are m53's; OMDb, Trakt and TVmaze answer
    these films exactly as m53 answers an id it does not hold, with a 404 that is a note on the job
    (decision 334).
    """

    def __init__(self, films: tuple[Film, ...]) -> None:
        super().__init__()
        self.by_imdb = {film.imdb: film for film in films}
        self.by_tmdb = {film.tmdb: film for film in films}
        self.by_article = {film.article: film for film in films if film.reception}
        self.by_rt = {film.rt_slug: film for film in films if film.reception}
        self.by_mc = {film.mc_slug: film for film in films if film.reception}

    def _tmdb(self, path: str):
        tail = path.rsplit("/", 1)[-1]
        if path.startswith("/3/find/") and tail in self.by_imdb:
            film = self.by_imdb[tail]
            body = {"movie_results": [{"id": film.tmdb, "title": film.name}], "tv_results": [],
                    "person_results": []}
            return 200, json.dumps(body).encode("utf-8"), canned.JSON_TYPE
        if path.startswith("/3/movie/") and tail.isdigit() and int(tail) in self.by_tmdb:
            return 200, tmdb_detail_body(self.by_tmdb[int(tail)]), canned.JSON_TYPE
        return super()._tmdb(path)

    def _wikidata(self, request: httpx.Request):
        query = request.url.params.get("query") or ""
        film = next((f for imdb, f in self.by_imdb.items() if imdb in query and f.reception), None)
        if film is not None:
            return 200, wikidata_body(film), canned.SPARQL_TYPE
        return super()._wikidata(request)

    def _wikipedia(self, request: httpx.Request):
        film = self.by_article.get(request.url.params.get("titles") or "")
        if film is not None:
            return 200, wikipedia_article_body(film), canned.JSON_TYPE
        return super()._wikipedia(request)

    def _rottentomatoes(self, path: str):
        film = self.by_rt.get(path.strip("/"))
        if film is not None:
            return 200, canned.rt_page_body(film.name, film.year, director=canned.DIRECTOR,
                                            cast=(canned.LEAD,)), canned.HTML_TYPE
        return super()._rottentomatoes(path)

    def _metacritic(self, path: str):
        parts = [p for p in path.strip("/").split("/") if p]
        film = self.by_mc.get("/".join(parts[:2])) if len(parts) >= 2 else None
        if film is None:
            return super()._metacritic(path)
        if parts[-1] == "critic-reviews":
            return 200, canned.metacritic_critics_body(), canned.HTML_TYPE
        if parts[-1] == "user-reviews":
            return None
        return 200, canned.metacritic_page_body(film.name, film.year), canned.HTML_TYPE


# --- the install ----------------------------------------------------------------------------------


class Clock:
    """A clock the run advances itself, so the hosts' real pacing costs no real time.

    `acquire/hosts.py` paces Rotten Tomatoes and Metacritic at seven tenths of a request a second and
    each provider at two, and those numbers are not the thing a harness changes: the `Fetcher` takes
    `clock`, `sleep` and `jitter` so a measurement can be tolerant while the policy stays honest (the
    rule `ops/m53_exit_criterion.py`'s `Clock` keeps). The spin limit turns a bucket that never refills
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
class Walk:
    """One add, swept and drained, and everything it left that a check reads."""

    film: Film
    key: str
    title_id: int
    run_id: int
    report: pipeline.TaskReport
    # Every app request from the add to the moment the drain returned, as "METHOD /path".
    app_requests: list[str]
    # The provider requests `ops/fake_llm.py` recorded during the drain, as it received them.
    sent: list[dict[str, Any]]
    board: dict[str, Any]
    task: dict[str, Any]
    # `GET /api/admin/flywheel` as it answered the moment the drain returned (clause five).
    flywheel_status: int
    flywheel: dict[str, Any]


@dataclass
class Install:
    """The one install every check measures, and what the checks hand each other."""

    conn: asyncpg.Connection
    work: Path
    llm: Any
    jellyfin: Any
    web: Web
    clock: Clock
    version: str = ""
    app: Any = None
    admin: httpx.AsyncClient | None = None
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)
    # Every request the app received, in order, from the admin client and the plugin alike.
    requests: list[str] = field(default_factory=list)
    # Every host a fetch asked for that this run does not resolve. Empty on a run that held.
    unresolved: list[str] = field(default_factory=list)
    walk: Walk | None = None


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it."""
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

    Derived from the registry, as `ops/m55_exit_criterion.py` derives it: every connector
    `registry.CONNECTORS` marks as seeded names the `Settings` fields its variables arrive in by its
    own prefix, matched case-insensitively because pydantic-settings reads the environment that way
    and a POSIX environment keeps the two spellings apart. [M5.5 review cycle 2, M55-KEYS-C2-04] Both
    doubles' override variables go too, so every key this run seals is a literal in `ops/` and none
    is a value an operator's shell supplied (decision 435).

    BOTH HALVES, for `ops/m53_exit_criterion.py`'s reason: `Settings` also reads `.env` from the
    working directory, so the process moves into a directory of this run's own DATA_DIR, which
    `main` removes after moving back out.
    """
    seeded = tuple(f"{name}_" for name, spec in registry.CONNECTORS.items() if spec.seeded)
    wanted = {name.upper() for name in core_config.Settings.model_fields if name.startswith(seeded)}
    for variable in list(os.environ):
        if variable.upper() in wanted or variable.upper().startswith(("FAKE_LLM_", "FAKE_JELLYFIN_")):
            os.environ.pop(variable, None)
    neutral = Path(os.environ["DATA_DIR"]) / "no-dot-env"
    neutral.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral)


def _load_double(name: str) -> Any:
    """One of the two doubles mounted in-process, the way the suite's fixtures mount them.

    Registered in `sys.modules` before it is executed, which is what `import` itself does: both carry
    `from __future__ import annotations`, so Pydantic resolves their control models through
    `sys.modules[cls.__module__]`.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


def _hold(jellyfin: Any, film: Film) -> None:
    """Put one film in the Jellyfin double's library, as a scan that just saved it would.

    Into this run's own copy of the double, in the server's own "N" spelling of a GUID, which the
    emitter dashes on the way out as the plugin does - `test_jellyfin_intake.py`'s GUID titles, filed
    the same way and for its reason.
    """
    jellyfin.ITEMS.append({
        "Id": film.item_id, "Name": film.name, "Type": "Movie", "ProductionYear": film.year,
        "RunTimeTicks": 104 * jellyfin.TICKS_PER_MINUTE,
        "DateCreated": "2026-09-20T20:00:00.0000000Z",
        "DateLastSaved": "2026-09-20T20:04:00.0000000Z",
        "ProviderIds": {"Imdb": film.imdb, "Tmdb": str(film.tmdb)},
    })
    jellyfin.LIBRARY_MEMBERS["jf-lib-films"] += (film.item_id,)


def _recording(app: Any, seen: list[str]) -> Any:
    """The app, wrapped so every request it receives is written down before it is answered."""

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            seen.append(f"{scope['method']} {scope['path']}")
        await app(scope, receive, send)

    return asgi


async def build_install(conn: asyncpg.Connection, bundle_root: Path, work: Path) -> Install:
    """The fixture imported, the films held, every connector keyed, the app booted. Raises on refusal.

    The caller owns `DATA_DIR` (the raw store's root and, as `work/artifacts`, the artifacts'),
    `DATABASE_URL` for the app's pool, and the environment's credentials; `close_install` undoes what
    this opens.
    """
    report = await bundle_import.import_bundle(conn, bundle_import.Bundle.open(bundle_root),
                                               work / "artifacts")
    fails = [f"{f.rule}: {f.message[:200]}" for f in report.findings if f.severity == "fail"]
    if not report.ok or fails:
        raise PreconditionFailed("the fixture bundle did not import: " + "; ".join(fails[:3]))
    version = await dna_terms.active_version(conn)
    if version is None:
        raise PreconditionFailed("the fixture bundle's import left no active vocabulary")
    llm, jellyfin = _load_double("fake_llm"), _load_double("fake_jellyfin")
    for film in FILMS:
        if film.item_id is not None:
            _hold(jellyfin, film)
    ctx = Install(conn=conn, work=work, llm=llm, jellyfin=jellyfin, web=Web(FILMS), clock=Clock(),
                  version=version)

    await registry.save_connector(conn, "tmdb", api_key=TMDB_KEY)
    await registry.save_connector(conn, "omdb", api_key=OMDB_KEY)
    await registry.save_connector(conn, "trakt", client_id=TRAKT_CLIENT_ID)
    for provider in PROVIDERS:
        await registry.save_connector(conn, provider, api_key=llm.KEYS[provider],
                                      model=MODELS[provider])
    await registry.save_connector(conn, spend.SETTINGS, cap_usd=CAP_WITH_ROOM,
                                  extraction_provider=ASSIGNED)

    # The app's one Jellyfin construction site, replaced rather than threaded, as its own docstring
    # invites: the admin PUT's version probe reaches the double with no parameter added anywhere.
    transport = httpx.ASGITransport(app=jellyfin.app)
    registry.make_client = lambda cfg: (
        JellyfinClient(cfg.url, cfg.api_key, transport=transport,
                       server_version=cfg.server_version, server_supported=cfg.server_supported)
        if cfg.configured else None
    )
    # The drain's one Fetcher construction site, replaced for the same reason: the worker's drain
    # passes `pipeline.drain` no factory, so the routing transport reaches the shipped job this way
    # or through a call the worker never makes (M5 review cycle 1, M5-EXIT-C1-01).
    pipeline._default_fetcher = build_fetcher(ctx)
    from spielplan.app import create_app

    ctx.app = create_app()
    recorded = _recording(ctx.app, ctx.requests)
    await ctx.stack.enter_async_context(ctx.app.router.lifespan_context(ctx.app))
    # `raise_app_exceptions=False` on both transports, for `ops/m52_exit_criterion.py`'s reason: a
    # handler that faulted must arrive as the 500 a browser or the plugin would get.
    ctx.admin = await ctx.stack.enter_async_context(httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorded, raise_app_exceptions=False),
        base_url="http://spielplan.test", timeout=60.0,
    ))
    created = await ctx.admin.post("/api/setup/admin",
                                   json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD})
    if created.status_code != 201:
        raise PreconditionFailed(f"no admin account: {console(created.text[:300])}")
    signed_in = await ctx.admin.post("/api/auth/login",
                                     json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD})
    if signed_in.status_code != 200:
        raise PreconditionFailed(f"the admin could not sign in: {signed_in.status_code}")
    # §6.6's own PUT, because that save is what MINTS the webhook token and shows it once (decisions
    # 332, 418): the token the plugin presents below is the one an operator would have pasted.
    saved = await ctx.admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": JELLYFIN_URL, "api_key": jellyfin.API_KEY, "mint_webhook_token": True},
    )
    token = str(saved.json().get("webhook_token") or "") if saved.status_code == 200 else ""
    if not token:
        raise PreconditionFailed(f"the connector save minted no webhook token: {saved.status_code} "
                                 f"{console(saved.text[:300])}")
    jellyfin.WEBHOOK_TRANSPORT = httpx.ASGITransport(app=recorded, raise_app_exceptions=False)
    jellyfin.WEBHOOK_URL = WEBHOOK_TARGET
    jellyfin.WEBHOOK_TOKEN = token
    print(f"  fixture bundle imported, vocabulary {console(version)} active; films held in the "
          f"Jellyfin double: {console(WALK_FILM.name)}, {console(CAPPED_FILM.name)}", flush=True)
    print(f"  sources and providers keyed through registry.save_connector; extraction assigned to "
          f"{ASSIGNED}; cap {CAP_WITH_ROOM} USD; the plugin pushes at {WEBHOOK_TARGET}", flush=True)
    return ctx


async def close_install(ctx: Install) -> None:
    """Close the admin client and the app's lifespan. A shutdown that fails is no verdict."""
    with contextlib.suppress(Exception):
        await ctx.stack.aclose()


class Routes(httpx.AsyncBaseTransport):
    """The Fetcher's one transport: each host to the server that answers it, any other refused.

    A name no route claims fails as a name that does not resolve, and is written down: a request
    that escaped the doubles is the failure decision 435 exists to make impossible, so it has to be
    visible in a verdict and not only as a note on some job.
    """

    def __init__(self, ctx: Install) -> None:
        self._ctx = ctx
        self._llm = httpx.ASGITransport(app=ctx.llm.app)
        self._jellyfin = httpx.ASGITransport(app=ctx.jellyfin.app)
        self._web = httpx.MockTransport(ctx.web.handler)
        self._llm_hosts = frozenset(ctx.llm.HOSTS.values())
        self._jellyfin_host = httpx.URL(JELLYFIN_URL).host

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self._llm_hosts:
            return await self._llm.handle_async_request(request)
        if host == self._jellyfin_host:
            return await self._jellyfin.handle_async_request(request)
        if host in WEB_HOSTS:
            return await self._web.handle_async_request(request)
        self._ctx.unresolved.append(host)
        raise httpx.ConnectError(f"{host} does not resolve: this run routes no such name",
                                 request=request)


def build_fetcher(ctx: Install):
    """`pipeline.drain`'s `fetcher_factory`: the real Fetcher, the routing transport inside it."""

    async def factory(conn: asyncpg.Connection) -> fetch.Fetcher:
        return fetch.Fetcher(conn=conn, transport=Routes(ctx), clock=ctx.clock,
                             sleep=ctx.clock.sleep, jitter=ctx.clock.jitter)

    return factory


async def scenario(ctx: Install, *, posture: str) -> None:
    """How all three providers answer, every field pinned, through the double's own control route.

    The content is the double's default, a schema-valid answer carrying one invented term beside
    real tags; the posture says whether a retry drops it (`comply`) or repeats it (`stubborn`). A
    refusal is a precondition and not a verdict: that is the harness failing, not the app.
    """
    body = {"content": CONTENT, "posture": posture, "envelope": "normal", "thoughts": True,
            "prompt_tokens": None, "fault": None, "fault_when": "always"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.llm.app),
                                 base_url=LLM_CONTROL) as control:
        answer = await control.post("/_test/scenario", json=body)
    if answer.status_code != 200:
        raise PreconditionFailed(
            f"the double refused scenario {body!r}: {answer.status_code} {console(answer.text[:200])}"
        )


async def emit(ctx: Install, **body: Any) -> dict[str, Any]:
    """Fire the Webhook plugin at the app through the Jellyfin double's own control surface.

    THE PAYLOAD IS THE DOUBLE'S AND NEVER THIS SCRIPT'S (`ops/m52_exit_criterion.py`'s `_emit`), and
    a refusal from the emitter is a precondition: the scenario could not be set up.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.jellyfin.app),
                                 base_url=JELLYFIN_CONTROL) as control:
        answer = await control.post("/_test/item-added", json=body)
    if answer.status_code != 200:
        raise PreconditionFailed(
            f"the double refused to emit {body!r}: {answer.status_code} {console(answer.text[:200])}"
        )
    return answer.json()


async def age_the_window(ctx: Install) -> None:
    """Move every intake row back past its window, in the database that wrote it (decision 363)."""
    await ctx.conn.execute(
        "UPDATE jellyfin_intake SET received_at = received_at - make_interval(mins => $1),"
        "       not_before = not_before - make_interval(mins => $1)", WINDOW_MINUTES,
    )


async def sweep(ctx: Install) -> intake.SweepReport:
    """§7.2's sweep over the stored connector, its client pointed at the double."""
    cfg = await registry.load_jellyfin(ctx.conn)
    client = JellyfinClient(cfg.url, cfg.api_key, transport=httpx.ASGITransport(app=ctx.jellyfin.app))
    return await intake.sweep_pending(ctx.conn, client, cfg)


async def task_keys(ctx: Install) -> set[str]:
    return {
        row["key"] for row in await ctx.conn.fetch(
            "SELECT key FROM acquisition_task WHERE kind = $1", pipeline.TASK_KIND
        )
    }


async def board_of(ctx: Install, title_id: int) -> dict[str, Any]:
    row = await ctx.conn.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", title_id)
    return dict(row) if row is not None else {}


async def task_of(ctx: Install, key: str) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(
        "SELECT * FROM acquisition_task WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, key
    )
    return dict(row) if row is not None else {}


async def drain(ctx: Install) -> tuple[pipeline.DrainReport, list[dict[str, Any]], int]:
    """One tick of the worker's own drain job, and what the provider double saw.

    THE WORKER'S ENTRY, NOT `pipeline.drain` CALLED FROM HERE. The run id is the seam stage 6 files
    its refusals under and stage 7 reads them back by (decision 462), and this function used to open
    a `job_run` row of its own and hand it to `pipeline.drain` - a run `worker._acquisition_drain`,
    the one production caller, never passed. So check 2 printed PASS over a stage-7 record that was
    `rejected: {}` on every real install (M5 review cycle 1, M5-EXIT-C1-01, M5-C1-GATE-01). Now the
    tick is the loop's own: `_record_start` opens the row, `_JOB_RUN` carries it into the job
    (decision 468), the shipped job calls `pipeline.drain` with its own arguments under its own
    budget, and `_record_finish` closes the row. What a caller of `_tick` decides is which jobs are
    due, as `worker.main` decides it from `_seed_schedule`; here every other job is stamped as having
    just run, so this tick is due for the drain alone.

    WHAT THE CHECKS READ IS WHAT THE TICK RECORDED: the run is the one row the tick wrote, and the
    report is rebuilt from the `detail` `_record_finish` stored, which is `DrainReport.as_dict`. A
    job that raised is recorded `ok = false` with its reason and never propagates out of `_tick`, so
    it is raised here as that reason rather than read as a drain that leased nothing.
    """
    since = int(await ctx.conn.fetchval("SELECT coalesce(max(id), 0) FROM job_run"))
    mark = len(ctx.llm.state.requests)
    now = asyncio.get_running_loop().time()
    await worker._tick(now, worker._now_local(),
                       {job.name: now for job in worker.JOBS if job.name != DRAIN_JOB}, {})
    runs = await ctx.conn.fetch(
        "SELECT id, name, ok, detail FROM job_run WHERE id > $1 ORDER BY id", since
    )
    if [run["name"] for run in runs] != [DRAIN_JOB]:
        raise RuntimeError(f"the tick recorded {[run['name'] for run in runs]}, not the drain alone")
    run = runs[0]
    recorded = run["detail"] or {}
    if run["ok"] is not True:
        raise RuntimeError(f"the worker's drain job recorded ok={run['ok']}: "
                           f"{console(str(recorded.get('error'))[:300])}")
    tick = pipeline.DrainReport(
        leased=recorded.get("leased", 0), ready=recorded.get("ready", 0),
        parked=recorded.get("parked", 0), failed=recorded.get("failed", 0),
        reclaimed=dict(recorded.get("reclaimed") or {}),
        tasks=[
            pipeline.TaskReport(task_id=task["task"], key=task["key"], title_id=task["title_id"],
                                stage=task["stage"], status=task["status"], reason=task["reason"],
                                stages_run=list(task["stages"]))
            for task in recorded.get("tasks") or []
        ],
    )
    return tick, list(ctx.llm.state.requests[mark:]), int(run["id"])


async def add_and_walk(ctx: Install, film: Film) -> Walk:
    """One film added in Jellyfin, filed by the sweep once its window closes, walked by one drain.

    Nothing between the add and the drain's return is a person: the plugin's POST, the sweep and the
    drain are the webhook route and the two jobs a real install runs on its own. The flywheel read is
    made the moment the drain returns and is outside the walk, which clause one's request list ends
    before.
    """
    await scenario(ctx, posture="comply")
    before = await task_keys(ctx)
    mark = len(ctx.requests)
    sent = await emit(ctx, item_id=film.item_id)
    if sent.get("statuses") != [202]:
        raise PreconditionFailed(f"the plugin's delivery was answered {sent.get('statuses')}")
    await age_the_window(ctx)
    await sweep(ctx)
    filed = sorted(await task_keys(ctx) - before)
    if len(filed) != 1:
        raise PreconditionFailed(f"the sweep filed {filed} for {film.name}, not one task")
    tick, llm_sent, run_id = await drain(ctx)
    walked = list(ctx.requests[mark:])
    report = next((task for task in tick.tasks if task.key == filed[0]), None)
    if report is None or report.title_id is None:
        raise PreconditionFailed(
            f"the drain leased {tick.leased} task(s) and walked no title for {filed[0]}"
        )
    answer = await ctx.admin.get(f"{ADMIN_PREFIX}/flywheel")
    return Walk(
        film=film, key=filed[0], title_id=int(report.title_id), run_id=run_id, report=report,
        app_requests=walked, sent=llm_sent, board=await board_of(ctx, int(report.title_id)),
        task=await task_of(ctx, filed[0]), flywheel_status=answer.status_code,
        flywheel=answer.json() if answer.status_code == 200 else {},
    )


async def walk_the_add(ctx: Install) -> Walk:
    """Clause one's add, which clauses two to five and seven measure the walk of."""
    ctx.walk = await add_and_walk(ctx, WALK_FILM)
    return ctx.walk


def walked(ctx: Install) -> Walk:
    if ctx.walk is None:
        raise PreconditionFailed("check 1 walked no add, so there is no walk to measure")
    return ctx.walk


def admin_reasons(*texts: Any) -> list[str]:
    """Which admin action's sentence appears anywhere in these values."""
    blob = json.dumps(texts, default=str)
    return [reason.strip() for reason in ADMIN_REASONS if reason.strip() in blob]


async def tier(ctx: Install, title_id: int) -> list[tuple]:
    rows = await ctx.conn.fetch(
        "SELECT term, provider, salience, confidence, n_sources FROM dna_tag WHERE title_id = $1"
        " ORDER BY term, provider", title_id,
    )
    return [tuple(row) for row in rows]


async def stored_tier(ctx: Install, title_id: int) -> list[tuple]:
    """Every extracted-tier row of a title, every column including its id and its stamp.

    `tier` reads values only, and a tier deleted and re-inserted with the same values - which is
    what `consensus.store_title`'s `_REPLACE` does with the double's pack-fixed tags - matches it.
    Only a row's id and `created_at` say it was written again (M5 review cycle 1, M5-EXIT-C1-02).
    """
    rows = await ctx.conn.fetch(
        "SELECT id, version, term, facet, provider, salience, confidence, n_sources, created_at"
        " FROM dna_tag WHERE title_id = $1 ORDER BY id", title_id,
    )
    return [tuple(row) for row in rows]


async def projected(ctx: Install, title_id: int) -> list[tuple]:
    """Every projected-tier row of a title, every column including its id and its stamp."""
    rows = await ctx.conn.fetch(
        "SELECT id, version, term, facet, weight, via, created_at FROM dna_projected"
        " WHERE title_id = $1 ORDER BY id", title_id,
    )
    return [tuple(row) for row in rows]


async def music(ctx: Install, title_id: int) -> list[tuple[str, str]]:
    """The music credits a card would show, as `ops/m53_exit_criterion.py` check 1 reads them."""
    rows = await ctx.conn.fetch(
        "SELECT p.name, c.source FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')"
        " ORDER BY p.name, c.source", title_id,
    )
    return [(str(row["name"]), str(row["source"])) for row in rows]


def retried_terms(sent: list[dict[str, Any]]) -> list[str]:
    """The terms the first request's answer carried and the second's dropped: what the retry named."""
    if len(sent) != 2:
        return []
    return sorted(set(sent[0].get("terms") or []) - set(sent[1].get("terms") or []))


# --- 1 to 5: the walk ---------------------------------------------------------------------------------


async def check_one(ctx: Install) -> tuple[bool, str]:
    """Plan check 1: a new Jellyfin add reaches `ready` at stage 10, unattended.

    AN ADD THE BUNDLE LACKS, SO STAGE 1 MINTS. The film is one no fixture row names, so the walk is
    the whole pipeline and not decision 411's exit for a title the bundle already placed: stage 1
    mints it above the corpus's half of the id space, stages 2 and 3 fetch and derive it, 4 passes
    it, 5 to 8 give it DNA, 9 places it against the fixture's Cold Tower and 10 stamps it.

    UNATTENDED IS TWO FACTS AND BOTH ARE READ. Between the add and `ready` the app received exactly
    one request, the plugin's delivery - no `/api/admin` route, no person - and no admin action's
    sentence (a retry, an abandon, a launch) is on the board or the task, so nothing made the walk
    due but the webhook and the two jobs. And no fetch escaped to a name this run does not resolve.
    """
    walk = ctx.walk or await walk_the_add(ctx)
    title = await ctx.conn.fetchrow(
        "SELECT origin, is_owned, placement FROM title WHERE id = $1", walk.title_id
    )
    between = walk.app_requests
    routed = [request for request in between if request.split(" ", 1)[-1].startswith(ADMIN_PREFIX)]
    stamped = admin_reasons(walk.board.get("reason"), walk.board.get("detail"),
                            walk.task.get("result_note"), walk.task.get("last_error"))
    ok = (
        walk.report.status == pipeline.READY
        and walk.report.stage == pipeline.STAGES[-1].number
        and tuple(walk.report.stages_run) == STAGE_NAMES
        and walk.board.get("stage") == pipeline.STAGES[-1].number
        and walk.board.get("status") == pipeline.READY
        and walk.task.get("state") == queue.DONE
        and title is not None and title["origin"] == "acquired"
        and walk.title_id >= stages.APP_ID_MIN
        and between == [DELIVERY]
        and not routed
        and not stamped
        and not ctx.unresolved
    )
    return ok, "\n".join([
        f"{walk.film.name}: ItemAdded delivered, swept into task {walk.key}, walked by one drain "
        f"(job_run {walk.run_id})",
        f"stages run: {', '.join(walk.report.stages_run)}",
        f"board: stage {walk.board.get('stage')} {walk.board.get('status')}; task "
        f"{walk.task.get('state')}; title {walk.title_id}, origin "
        f"{title['origin'] if title else None}, placement {title['placement'] if title else None}, "
        f"owned {title['is_owned'] if title else None}",
        f"app requests from the add to ready: {between}; admin routes among them: "
        f"{routed or 'none'}; admin reasons on the board or the task: {stamped or 'none'}",
        f"fetches to a name this run does not resolve: {ctx.unresolved or 'none'}",
    ])


async def upgraded_park(ctx: Install) -> tuple[bool, list[str]]:
    """Clause two's second half: an install upgraded from M5.7, and decision 467's re-entry.

    M5.7 parked every title at stage 6 on a missing pack. The state is walked into by the driver,
    not written: a fixture title with a plot and no pack is queued at (6, running) - what `run_task`
    wrote when a stub stage 5 advanced - and stage 6 parks it under the no-pack reason with its
    deadline. Then the deadline passes: `retry_after` and `next_attempt_at` move into the past
    together, as `_record_stop` wrote them as one value. The next drain must re-enter at stage 5,
    store a pack under the task's key, and reach stage 6 with it.
    """
    conn = ctx.conn
    if not await pipeline.enqueue_title(conn, UPGRADED_TITLE):
        raise PreconditionFailed(f"the queue already held a task for title {UPGRADED_TITLE}")
    key = await conn.fetchval(
        "SELECT key FROM acquisition_task WHERE kind = $1 AND (payload ->> 'title_id')::int = $2",
        pipeline.TASK_KIND, UPGRADED_TITLE,
    )
    await pipeline.write_board(conn, UPGRADED_TITLE, stage=EXTRACT_STAGE, status=pipeline.RUNNING)
    first, asked_first, _ = await drain(ctx)
    parked = next((t for t in first.tasks if t.key == key), None)
    held = (
        parked is not None and parked.stage == EXTRACT_STAGE and parked.status == pipeline.PARKED
        and NO_PACK in parked.reason and "stage 5" in parked.reason and not asked_first
    )
    past = await conn.fetchval("SELECT now() - make_interval(mins => 1)")
    await conn.execute("UPDATE acquisition_job SET retry_after = $2 WHERE title_id = $1",
                       UPGRADED_TITLE, past)
    await conn.execute(
        "UPDATE acquisition_task SET next_attempt_at = $3 WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, key, past,
    )
    second, asked, _ = await drain(ctx)
    resumed = next((t for t in second.tasks if t.key == key), None)
    pack = await conn.fetchrow(
        "SELECT p.pack_sha, d.entity_key FROM dna_pack p JOIN raw_document d ON d.id = p.raw_document_id"
        " WHERE p.title_id = $1 AND p.version = $2", UPGRADED_TITLE, ctx.version,
    )
    ok = (
        held
        and resumed is not None
        and resumed.stages_run[:2] == [PACK, EXTRACT]
        and pack is not None and pack["entity_key"] == key
        and bool(asked)
    )
    return ok, [
        f"title {UPGRADED_TITLE} at (6, running) with no pack: "
        f"{parked.status if parked else 'not leased'} at stage {parked.stage if parked else None} - "
        f"{console((parked.reason if parked else '')[:110])}; {len(asked_first)} provider request(s)",
        f"its deadline passed, the next drain ran {', '.join(resumed.stages_run) if resumed else '-'}; "
        f"pack stored under {pack['entity_key'] if pack else None}; stage 6 then sent "
        f"{len(asked)} provider request(s)",
    ]


async def check_two(ctx: Install) -> tuple[bool, str]:
    """Plan check 2: stages 5, 7 and 8 run in the walk, and an expired no-pack park re-enters at 5.

    STAGE 5 (decision 461): `verify.read_pack` hands back the AUGMENTED text - the base pack with
    `dna/craft.py`'s sentinel and supplement after it - whose digest is the `dna_pack` row's, filed
    under the walk's task key and run, the only spelling §6.6's board lists documents by.

    STAGE 6's OUTPUT, AS §4.1 RULE 1 REQUIRES IT: every extracted-tier row carries its evidence.

    STAGE 7 (decision 462): its detail is exactly `SELECT rule_violated, count(*) FROM dna_reject`
    for this title under this walk's run, and it is not empty - the double's first answer violates
    one tag on purpose - so a stage that recorded nothing, or read another walk's refusals, fails.

    STAGE 8 (decision 463): the acquired title's keywords projected through the fixture's own alias
    map, to exactly the term that map gives this film's keyword.
    """
    walk = walked(ctx)
    conn = ctx.conn
    detail = walk.board.get("detail") or {}
    text = await verify.read_pack(conn, walk.title_id, ctx.version)
    pack = await conn.fetchrow(
        "SELECT p.pack_sha, d.entity_key, d.run_id FROM dna_pack p"
        " JOIN raw_document d ON d.id = p.raw_document_id WHERE p.title_id = $1 AND p.version = $2",
        walk.title_id, ctx.version,
    )
    tags = await conn.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND version = $2", walk.title_id, ctx.version
    )
    bare = await conn.fetchval(
        "SELECT count(*) FROM dna_tag t WHERE t.title_id = $1"
        " AND NOT EXISTS (SELECT 1 FROM dna_evidence e WHERE e.dna_tag_id = t.id)", walk.title_id,
    )
    filed = {
        row["rule_violated"]: int(row["n"]) for row in await conn.fetch(
            "SELECT rule_violated, count(*) AS n FROM dna_reject WHERE title_id = $1 AND run_id = $2"
            " GROUP BY rule_violated", walk.title_id, walk.run_id,
        )
    }
    expected = await conn.fetchval(
        "SELECT term FROM dna_alias WHERE version = $1 AND alias = $2", ctx.version, KEYWORD
    )
    if expected is None:
        raise PreconditionFailed(f"the fixture's alias map does not carry {KEYWORD!r}")
    terms = [row[2] for row in await projected(ctx, walk.title_id)]
    stored, recorded, projection = (detail.get(name) or {} for name in (PACK, VERIFY, PROJECT))
    first_half = (
        text is not None and craft.SENTINEL in text and craft.base_pack(text) != text
        and pack is not None and pack["pack_sha"] == packs.sha(text)
        and stored.get("pack_sha") == pack["pack_sha"]
        and pack["entity_key"] == walk.key and pack["run_id"] == walk.run_id
        and tags > 0 and bare == 0
        and bool(filed) and recorded.get("rejected") == filed and recorded.get("tags") == tags
        and terms == [expected]
        and projection.get("origin") == "acquired" and projection.get("projected") == len(terms)
    )
    second_half, lines = await upgraded_park(ctx)
    ok = first_half and second_half
    return ok, "\n".join([
        f"stage 5: read_pack returned {len(text or '')} chars, supplement "
        f"{'present' if text and craft.SENTINEL in text else 'ABSENT'}, digest "
        f"{'matches' if pack and text and pack['pack_sha'] == packs.sha(text) else 'DIFFERS'}; "
        f"filed under {pack['entity_key'] if pack else None}, run {pack['run_id'] if pack else None}",
        f"stage 6: {tags} extracted-tier tag(s), {bare} without evidence",
        f"stage 7: detail {recorded.get('rejected')} beside dna_reject for the run {filed}",
        f"stage 8: projected {terms} (the alias map gives {KEYWORD} -> {expected}); detail "
        f"{projection}",
        *lines,
    ])


async def check_three(ctx: Install) -> tuple[bool, str]:
    """Plan check 3: the walk was retried exactly once, the violation named as it arrived.

    READ OFF THE DOUBLE'S OWN LOG OF THE SECOND REQUEST, and not off the string the app built, for
    `ops/m55_exit_criterion.py` check 2's reason: a retry built right and sent wrong is the failure.
    The offending value is found without asking the app which one it was - the term the first
    answer carried and the second dropped because the retry named it, and one no `dna_term` row
    holds - and the rule is the one `verify.record_rejects` filed under this walk's run.
    """
    walk = walked(ctx)
    calls = await ctx.conn.fetch(
        "SELECT attempt, provider, ok FROM llm_call WHERE title_id = $1 ORDER BY id", walk.title_id
    )
    vocabulary = {
        row["term"] for row in await ctx.conn.fetch(
            "SELECT term FROM dna_term WHERE version = $1", ctx.version
        )
    }
    refused = [
        (row["rule_violated"], row["term"]) for row in await ctx.conn.fetch(
            "SELECT rule_violated, term FROM dna_reject WHERE title_id = $1 AND run_id = $2 ORDER BY id",
            walk.title_id, walk.run_id,
        )
    ]
    sent = walk.sent
    retry = (sent[1].get("retry") if len(sent) == 2 else None) or ""
    dropped = retried_terms(sent)
    term = dropped[0] if len(dropped) == 1 else ""
    ok = (
        len(sent) == 2
        and all(request.get("provider") == ASSIGNED for request in sent)
        and sent[0].get("retry") is None
        and retry.startswith(ctx.llm.RETRY_MARKER)
        and [(row["attempt"], row["provider"]) for row in calls] == [(1, ASSIGNED), (2, ASSIGNED)]
        and bool(term) and term not in vocabulary
        and f"- {RULE}: " in retry and f"'{term}'" in retry
        and refused == [(RULE, term)]
    )
    named = [line for line in retry.splitlines() if line.startswith("- ")]
    return ok, "\n".join([
        f"{len(sent)} provider request(s) to {sorted({r.get('provider') for r in sent})}; llm_call "
        f"rows: attempts {[row['attempt'] for row in calls]}",
        f"the term answered first and dropped on the retry: {term!r} (in the vocabulary: "
        f"{term in vocabulary}); dna_reject for the run: {refused}",
        *(f"received: {console(line[:100])}" for line in named),
    ])


async def check_four(ctx: Install) -> tuple[bool, str]:
    """Plan check 4: a second violation fails stage 6 for good and writes nothing.

    RE-ARMED THROUGH THE BOARD'S OWN RETRY. `ops/m55_exit_criterion.py` check 3 stood decision 330's
    retry-from-stage-N in with two writes because M5.6 had not shipped it; it has (decision 444), and
    the walked title's task is `done`, which no `next_attempt_at` alone could make due again. So the
    title is retried from stage 6 on the Acquisition board - over a cap with room, which the retry's
    pre-check reads (decision 464) - and walked with the double `stubborn`: decision 431 makes that
    final, so two requests and never a third, stage 6 failed with the board saying no retry is
    coming, the task closed, and the tier exactly as the walk left it - the same rows, read by id
    and stamp, since a rewrite with the walk's own values is still a write. Then one more tick,
    which leases nothing of this title and asks nobody.
    """
    walk = walked(ctx)
    before = await stored_tier(ctx, walk.title_id)
    retried = await ctx.admin.post(f"{ADMIN_PREFIX}/acquisition/{walk.title_id}/retry-from",
                                   json={"stage": EXTRACT_STAGE})
    if retried.status_code != 200:
        raise PreconditionFailed(
            f"the board refused the retry from stage 6: {retried.status_code} "
            f"{console(retried.text[:200])}"
        )
    await scenario(ctx, posture="stubborn")
    try:
        tick, sent, _ = await drain(ctx)
        after_tick, asked, _ = await drain(ctx)
    finally:
        await scenario(ctx, posture="comply")
    report = next((t for t in tick.tasks if t.title_id == walk.title_id), None)
    after = await stored_tier(ctx, walk.title_id)
    board = await board_of(ctx, walk.title_id)
    # The task the retry walked, which is not the add's: that one finished at `ready`, and a board
    # retry revives no finished task - it walks the title's `title:<id>` task (decision 444).
    task = await task_of(ctx, report.key) if report is not None else {}
    stage_detail = (board.get("detail") or {}).get(EXTRACT) or {}
    retries = [request.get("retry") or "" for request in sent]
    again = [t for t in after_tick.tasks if t.title_id == walk.title_id]
    ok = (
        report is not None
        and report.stages_run == [EXTRACT]
        and report.stage == EXTRACT_STAGE and report.status == pipeline.FAILED
        and len(sent) == 2 and not retries[0] and f"- {RULE}: " in retries[1]
        and bool(before) and after == before
        and board.get("status") == pipeline.FAILED and stage_detail.get("retrying") is False
        and task.get("state") == queue.FAILED
        and not again and not asked
    )
    return ok, "\n".join([
        f"retried from stage 6 on the board; walked {report.stages_run if report else None}: "
        f"stage {report.stage if report else None} {report.status if report else 'not leased'} after "
        f"{len(sent)} request(s)",
        f"dna_tag rows {len(before)} before and {len(after)} after, read by id and stamp: "
        f"{'identical' if after == before else 'CHANGED'}; board {board.get('status')}, retrying "
        f"{stage_detail.get('retrying')}; task {task.get('key')} {task.get('state')}",
        f"the next tick leased this title {len(again)} time(s) and sent {len(asked)} request(s)",
    ])


async def check_five(ctx: Install) -> tuple[bool, str]:
    """Plan check 5: the walk's thin-facet row was readable the moment its drain returned.

    §8.4 writes the row "the moment its walk finishes stage 8", and decision 440 put that write in
    the walk; so the route is read the instant `pipeline.drain` returns, before any job runs, and the
    row must be there, open, naming as unnamed exactly the declared facets the walk's tier left
    without a term. The double answers with the first term of three facets, and the fixture declares
    more, so the walked title is thin by construction.
    """
    walk = walked(ctx)
    items = walk.flywheel.get("items") or []
    row = next((item for item in items if item.get("title_id") == walk.title_id
                and item.get("kind") == "thin_facet"), None)
    declared = {
        record["facet"] for record in await ctx.conn.fetch(
            "SELECT facet FROM dna_facet WHERE version = $1", ctx.version
        )
    }
    named = {term.split(".", 1)[0] for term, *_rest in await tier(ctx, walk.title_id)}
    unnamed = set(((row or {}).get("detail") or {}).get("unnamed") or [])
    ok = (
        walk.flywheel_status == 200
        and row is not None
        and row.get("status") == "queued"
        and bool(unnamed) and unnamed == declared - named
    )
    return ok, "\n".join([
        f"GET {ADMIN_PREFIX}/flywheel answered {walk.flywheel_status} when the drain returned, with "
        f"{len(items)} open row(s)",
        f"this title's row: {row.get('status') if row else 'ABSENT'}; unnamed {sorted(unnamed)} "
        f"against the {len(declared)} declared facet(s) less the {len(named)} its tier names",
    ])


# --- 6 to 9: the launch, the correction, the burst and the cap ------------------------------------


async def untouched(ctx: Install, title_id: int) -> dict[str, Any]:
    """Everything a walk of a title would change, as it stands."""
    conn = ctx.conn
    return {
        "tier": await tier(ctx, title_id),
        "projected": await projected(ctx, title_id),
        "board": await board_of(ctx, title_id),
        "calls": await conn.fetchval("SELECT count(*) FROM llm_call WHERE title_id = $1", title_id),
        "pack": await conn.fetchval("SELECT pack_sha FROM dna_pack WHERE title_id = $1", title_id),
    }


async def flywheel_rows(ctx: Install) -> dict[int, tuple]:
    rows = await ctx.conn.fetch(
        "SELECT id, title_id, status, reason, detail, batch_id FROM flywheel_item ORDER BY id"
    )
    return {int(row["id"]): tuple(row) for row in rows}


async def check_six(ctx: Install) -> tuple[bool, str]:
    """Plan check 6: a Launch takes exactly the rows selected, and stage 8 leaves bundle rows alone.

    THREE ROWS WRITTEN BY THE DRIVER'S OWN CALL. No fixture title can pass stage 4 - every review the
    fixture ships is under the fifty-word floor - so the three open rows are written by
    `flywheel.thin.observe_title`, the call `run_task` makes after stage 8, over three bundle titles
    whose tier names some facets and not others. Two are launched through the Launch route with a
    provider the stored assignment does not name, and before anything drains they are the only rows
    `running` and their titles the only ones due.

    THE NEXT DRAIN RE-EXTRACTS BOTH FROM STAGE 5 UNDER THE BATCH PLAN: each walk starts at the pack,
    every provider request and every `llm_call` row is the batch's provider, and the walk crosses
    stage 8's bundle branch, so both titles' projected rows are byte-identical afterwards - ids and
    stamps included (decisions 162, 463). The third row, its title and every other open row are
    exactly as they stood.

    DECISION 463 IS READ TWICE, because each reading fails a different wrong stage 8. The stage's own
    record says it kept the bundle's rows and projected nothing, which fails a stage that projects a
    bundle title whatever the projection happens to write - over these titles' keywords
    `dna/project.project_title` would re-derive nothing, since none is one the fixture's alias map
    maps. The rows being byte-identical fails a stage that removes or rewrites them.
    """
    conn = ctx.conn
    await scenario(ctx, posture="comply")
    three = (LEFT_TITLE, *LAUNCHED_TITLES)
    for title_id in three:
        await thin.observe_title(conn, title_id)
    opened = {
        int(row["title_id"]): int(row["id"]) for row in await conn.fetch(
            "SELECT id, title_id FROM flywheel_item WHERE kind = 'thin_facet' AND status = 'queued'"
            " AND title_id = ANY($1::int[])", list(three),
        )
    }
    if set(opened) != set(three):
        raise PreconditionFailed(f"observe_title opened rows for {sorted(opened)}, not {list(three)}")
    chosen = sorted(opened[t] for t in LAUNCHED_TITLES)
    kept_before = {t: await projected(ctx, t) for t in LAUNCHED_TITLES}
    left_before = await untouched(ctx, LEFT_TITLE)
    others_before = {i: row for i, row in (await flywheel_rows(ctx)).items() if i not in chosen}
    metered = await conn.fetchval("SELECT coalesce(max(id), 0) FROM llm_call")
    launched = await ctx.admin.post(f"{ADMIN_PREFIX}/flywheel/launch", json={
        "item_ids": chosen, "providers": [BATCH_PROVIDER], "passes": 1,
    })
    if launched.status_code != 200:
        raise PreconditionFailed(f"the launch was refused: {launched.status_code} "
                                 f"{console(launched.text[:200])}")
    running = sorted(int(row["id"]) for row in await conn.fetch(
        "SELECT id FROM flywheel_item WHERE status = 'running'"
    ))
    due = sorted(int(row["title_id"]) for row in await conn.fetch(
        "SELECT DISTINCT (payload ->> 'title_id')::int AS title_id FROM acquisition_task"
        " WHERE kind = $1 AND state = $2 AND next_attempt_at <= now()",
        pipeline.TASK_KIND, queue.PENDING,
    ))
    tick, sent, _ = await drain(ctx)
    walks = {t.title_id: t for t in tick.tasks if t.title_id in LAUNCHED_TITLES}
    calls = await conn.fetch(
        "SELECT title_id, provider FROM llm_call WHERE id > $1 ORDER BY id", metered
    )
    kept_after = {t: await projected(ctx, t) for t in LAUNCHED_TITLES}
    kept = {t: ((await board_of(ctx, t)).get("detail") or {}).get(PROJECT) or {}
            for t in LAUNCHED_TITLES}
    left_after = await untouched(ctx, LEFT_TITLE)
    others_after = {i: row for i, row in (await flywheel_rows(ctx)).items() if i in others_before}
    ok = (
        running == chosen
        and due == sorted(LAUNCHED_TITLES)
        and set(walks) == set(LAUNCHED_TITLES)
        and all(w.stages_run[:4] == [PACK, EXTRACT, VERIFY, PROJECT] for w in walks.values())
        and bool(sent) and all(request.get("provider") == BATCH_PROVIDER for request in sent)
        and {row["title_id"] for row in calls} == set(LAUNCHED_TITLES)
        and all(row["provider"] == BATCH_PROVIDER for row in calls)
        and all("kept" in kept[t] and "projected" not in kept[t] for t in LAUNCHED_TITLES)
        and all(kept_before[t] and kept_after[t] == kept_before[t] for t in LAUNCHED_TITLES)
        and left_after == left_before
        and others_after == others_before
    )
    return ok, "\n".join([
        f"launched rows {chosen} of {sorted(opened.values())}; running {running}; titles due {due}",
        *(f"title {t}: walked {', '.join(walks[t].stages_run)}; stage 8 "
          f"{'kept the bundle rows' if 'kept' in kept[t] else kept[t]}; projected rows "
          f"{'byte-identical' if kept_after[t] == kept_before[t] else 'CHANGED'} "
          f"({len(kept_after[t])})" for t in sorted(walks)),
        f"{len(sent)} provider request(s) to {sorted({r.get('provider') for r in sent})}; llm_call "
        f"rows for titles {sorted({row['title_id'] for row in calls})}",
        f"title {LEFT_TITLE} {'untouched' if left_after == left_before else 'CHANGED'}; the other "
        f"{len(others_before)} flywheel row(s) "
        f"{'unchanged' if others_after == others_before else 'CHANGED'}",
    ])


async def check_seven(ctx: Install) -> tuple[bool, str]:
    """Plan check 7: a re-derive keeps the shipped and the household correction.

    §14.5's scar is invisible unless the derive has something to bury, so both titles' TMDB
    documents credit `ops/m53_exit_criterion.py`'s wrong composer: every derive writes it out of the
    raw store and the ledger has to replace it, which the derive's own `replaced` count says it did.
    The shipped correction is the fixture ledger's, read from the install rather than typed here,
    on the title the fixture names; its documents are fetched first by a walk of its own, since a
    bundle title arrives with no raw store. The household one is written through the correction
    editor's route onto the walked title. Both are then retried from stage 3 on the board, which
    runs nothing before the derive (decision 424), and after that walk each title's music credit is
    its correction's name and nothing else.
    """
    walk = walked(ctx)
    conn = ctx.conn
    await scenario(ctx, posture="comply")
    shipped = await conn.fetchval(
        "SELECT new_value FROM credit_correction WHERE title_id = $1 AND field = 'composer'"
        " AND origin = 'bundle' AND btrim(coalesce(evidence, '')) <> '' ORDER BY id LIMIT 1",
        SHIPPED_TITLE,
    )
    if shipped is None:
        raise PreconditionFailed(f"the fixture ships no composer correction for title {SHIPPED_TITLE}")
    if not await pipeline.enqueue_title(conn, SHIPPED_TITLE):
        raise PreconditionFailed(f"the queue already held a task for title {SHIPPED_TITLE}")
    first, _, _ = await drain(ctx)
    fetched = next((t for t in first.tasks if t.title_id == SHIPPED_TITLE), None)
    if fetched is None or "enrich" not in fetched.stages_run:
        raise PreconditionFailed(f"no walk fetched title {SHIPPED_TITLE}'s documents")
    wrote = await ctx.admin.post(f"{ADMIN_PREFIX}/curated/corrections", json={
        "title_id": walk.title_id, "kind": "composer", "value": canned.HOUSEHOLD_COMPOSER,
        "evidence": canned.HOUSEHOLD_EVIDENCE,
    })
    if wrote.status_code != 201:
        raise PreconditionFailed(f"the correction editor refused: {wrote.status_code} "
                                 f"{console(wrote.text[:200])}")
    held = {SHIPPED_TITLE: str(shipped), walk.title_id: canned.HOUSEHOLD_COMPOSER}
    for title_id in held:
        retried = await ctx.admin.post(f"{ADMIN_PREFIX}/acquisition/{title_id}/retry-from",
                                       json={"stage": 3})
        if retried.status_code != 200:
            raise PreconditionFailed(f"the board refused title {title_id}'s retry from stage 3: "
                                     f"{retried.status_code} {console(retried.text[:200])}")
    tick, _, _ = await drain(ctx)
    walks = {t.title_id: t for t in tick.tasks if t.title_id in held}
    credits = {title_id: await music(ctx, title_id) for title_id in held}
    replaced = {
        title_id: (((await board_of(ctx, title_id)).get("detail") or {}).get(DERIVE) or {})
        .get("corrections", {}).get("replaced", 0)
        for title_id in held
    }
    ok = all(
        walks.get(title_id) is not None
        and walks[title_id].stages_run[:1] == [DERIVE]
        and [name for name, _source in credits[title_id]] == [name]
        and any(source == ledgers.CORRECTION_SOURCE for _name, source in credits[title_id])
        and replaced[title_id] >= 1
        for title_id, name in held.items()
    )
    return ok, "\n".join(
        f"title {title_id} ({'shipped' if title_id == SHIPPED_TITLE else 'household'} correction "
        f"{console(name)}): the retry walked "
        f"{', '.join(walks[title_id].stages_run) if title_id in walks else 'nothing'}; the derive "
        f"replaced {replaced[title_id]} music credit(s); music credits now "
        f"{console(str(credits[title_id]))}"
        for title_id, name in held.items()
    )


async def check_eight(ctx: Install) -> tuple[bool, str]:
    """Plan check 8: twelve episode adds for one series in one window file one task for the show.

    `ops/m52_exit_criterion.py` check 1's burst, through the same emitter and the same sweep, into a
    queue that already holds this run's other work - so the measure is the keys the sweep ADDED:
    exactly the show's, none of any episode's.
    """
    before = await task_keys(ctx)
    sent = await emit(ctx, item_id=BURST_SERIES, episodes=BURST_EPISODES)
    if int(sent.get("sent") or 0) != BURST_EPISODES or set(sent.get("statuses") or []) != {202}:
        raise PreconditionFailed(
            f"the plugin delivered {sent.get('sent')} event(s) answered {sent.get('statuses')}"
        )
    pending = await ctx.conn.fetch(
        "SELECT item_id, resolved_key FROM jellyfin_intake WHERE state = $1", intake.PENDING
    )
    await age_the_window(ctx)
    swept = await sweep(ctx)
    after = await task_keys(ctx)
    added = sorted(after - before)
    episodes = sorted(key for key in after if key.startswith(f"jellyfin:{BURST_SERIES}-e"))
    ok = (
        len(pending) == BURST_EPISODES
        and {row["resolved_key"] for row in pending} == {BURST_SERIES}
        and added == [f"jellyfin:{BURST_SERIES}"]
        and swept.enqueued == 1
        and not episodes
    )
    return ok, (
        f"{len(pending)} event(s) pending under {sorted({row['resolved_key'] for row in pending})}; "
        f"the sweep found {swept.ripe} ripe key(s) and created {swept.enqueued} task(s); keys added "
        f"{added}; episode keys anywhere in the queue {episodes or 'none'}"
    )


async def check_nine(ctx: Install) -> tuple[bool, str]:
    """Plan check 9: the cap parks a fresh add at stage 6 and it never auto-retries past it.

    THE CAP IS THE MONTH'S OWN SPEND (`ops/m55_exit_criterion.py` check 7's idiom), so "at the cap"
    is decision 325's arithmetic over rows the app wrote. A fresh add then walks stages 1 to 5 and
    parks at 6 `over spend cap` before any provider is asked: no request reaches the double, no
    `llm_call` row is written, and the park carries its deadline on both tables. On the next tick
    the task is made due again with `next_attempt_at` alone and its `retry_after` untouched, so the
    walk resumes at the board's stage (decision 421) - and parks there again, asking nobody.
    """
    conn = ctx.conn
    spent = await spend.spent(conn)
    if not spent > 0:
        raise PreconditionFailed(f"the month has metered {spent} USD, so there is no month to cap")
    await registry.save_connector(conn, spend.SETTINGS, cap_usd=float(spent))
    if await spend.cap(conn) != spent:
        raise PreconditionFailed(f"the cap reads {await spend.cap(conn)} after setting {spent}")
    metered = await conn.fetchval("SELECT count(*) FROM llm_call")
    capped = await add_and_walk(ctx, CAPPED_FILM)
    now = datetime.now(UTC)
    first = (
        capped.report.stage == EXTRACT_STAGE and capped.report.status == pipeline.PARKED
        and capped.report.reason.startswith(OVER_CAP)
        and capped.sent == []
        and capped.board.get("reason") == capped.report.reason
        and capped.board.get("retry_after") is not None and capped.board["retry_after"] > now
        and capped.task.get("state") == queue.PENDING
        and capped.task.get("next_attempt_at") == capped.board.get("retry_after")
    )
    after_first = await conn.fetchval("SELECT count(*) FROM llm_call")
    await conn.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, capped.key,
    )
    tick, sent, _ = await drain(ctx)
    again = next((t for t in tick.tasks if t.key == capped.key), None)
    board = await board_of(ctx, capped.title_id)
    task = await task_of(ctx, capped.key)
    later = datetime.now(UTC)
    second = (
        again is not None and again.stages_run == [EXTRACT]
        and again.status == pipeline.PARKED and again.reason.startswith(OVER_CAP)
        and sent == []
        and board.get("stage") == EXTRACT_STAGE and board.get("status") == pipeline.PARKED
        and board.get("retry_after") is not None and board["retry_after"] > later
        and task.get("state") == queue.PENDING
    )
    after_second = await conn.fetchval("SELECT count(*) FROM llm_call")
    ok = first and second and after_first == metered and after_second == metered
    return ok, "\n".join([
        f"cap set to the month's own spend: {spent} USD",
        f"{capped.film.name}: walked {', '.join(capped.report.stages_run)}; stage "
        f"{capped.report.stage} {capped.report.status}: {console(capped.report.reason[:120])}",
        f"{len(capped.sent)} provider request(s); llm_call rows {metered} before, {after_first} after; "
        f"task {capped.task.get('state')}, due {capped.task.get('next_attempt_at')}",
        f"the next tick, the task made due and retry_after untouched: walked "
        f"{again.stages_run if again else 'nothing'}, {again.status if again else '-'}; "
        f"{len(sent)} provider request(s); llm_call rows {after_second}",
    ])


# --- the run ------------------------------------------------------------------------------------------


RUNNERS = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five, 6: check_six,
    7: check_seven, 8: check_eight, 9: check_nine,
}
HEADINGS = {
    1: "A new add, unattended, to ready (plan check 1)",
    2: "The DNA stages in that walk, and an upgraded park (plan check 2; decisions 461-463, 467)",
    3: "Retried once, named as it arrived (plan check 3)",
    4: "A second violation (plan check 4; decision 431)",
    5: "A naming failure, at the moment (plan check 5; decision 440)",
    6: "Exactly the selection (plan check 6; decisions 443, 463)",
    7: "A re-derive keeps the correction (plan check 7)",
    8: "One job for the show (plan check 8)",
    9: "The cap parks and never auto-retries (plan check 9)",
}
RECORDED: set[int] = set()


async def measure(number: int, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's nine.
    A refused precondition is reported without its traceback - the sentence IS the diagnosis.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    RECORDED.add(number)
    try:
        verdict, detail = await RUNNERS[number](ctx)
    except PreconditionFailed as exc:
        check(False, label, f"PRECONDITION FAILED: {exc}")
        return
    except Exception as exc:                       # reported, not propagated
        check(False, label,
              f"the check stopped on {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return
    check(verdict, label, detail)


async def main() -> int:
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it: this script creates a")
        print("scratch database on that server and drops it again, and names no other.")
        return 2
    for name in ("fake_llm", "fake_jellyfin"):
        try:
            _load_double(name)
        except Exception as exc:
            print(f"ops/{name}.py will not import ({console(type(exc).__name__)}: {console(str(exc))}):")
            print("every check below is measured against that double, and this script stands in no")
            print("server of its own for it.")
            return 2

    print("\nM5 exit criterion -- a new add to ready, unattended, on the fixture\n", flush=True)

    started_in = Path.cwd()
    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global. Named with this run's pid so two concurrent runs cannot drop each other's.
    scratch = f"spielplan_m5_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    try:
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"no Postgres to create a scratch database on ({console(type(exc).__name__)}: "
              f"{console(str(exc))}), so nothing was measured")
        return 2
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it: everything that can fail after
    # the CREATE happens inside the `try`, because an orphan named with a pid nothing will ever name
    # again is a leak on the household's own server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    ctx: Install | None = None
    try:
        work = Path(tempfile.mkdtemp(prefix="spielplan-m5-exit-"))
        # The raw store's root as well as the artifacts': every document stage 2 fetches and every
        # pack stage 5 stores lands under `work/raw` exactly as `rawstore.resolve` puts it there.
        os.environ["DATA_DIR"] = str(work)
        os.environ["DATABASE_URL"] = scratch_dsn
        _neutralise_connector_env()
        core_config.settings.cache_clear()
        # One INFO line per request, with the url, over a run that makes some hundreds: the verdicts
        # below are what this run reports, and the app's own log still reaches the console.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        conn = await asyncpg.connect(scratch_dsn)
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The install this run measures (spec sections 7.2, 8 and 9; decision 465)", flush=True)
        ctx = await build_install(conn, fixture_bundle.make_bundle(work / "fixture-bundle"), work)
        for number, heading in HEADINGS.items():
            print(f"\n{number}. {heading}", flush=True)
            await measure(number, ctx)
    except Exception as exc:
        # Everything outside a check: the connect, the migrations, the fixture build, the boot.
        # Reported as the failures they are, so the exit code stays non-zero and the run still ends
        # in a score rather than a traceback where the sentence naming the cause belongs.
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
            await close_install(ctx)
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
