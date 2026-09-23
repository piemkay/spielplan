"""§8 stage 2's eight source adapters, driven against a canned web. Spec v2.1 §8 stage 2.

Decisions 334, 340, 345, 372, 373, 374, 377.

**Nothing here reaches the network.** Every request is served by an `httpx.MockTransport`, and
the clock and the sleeper are injected, so the two hosts §8 stage 2 scrapes can be asserted at
their real policy - seven-tenths of a request a second, one at a time - without the suite taking
a second and a half per page. That is the shape `test_acquire_fetch.py` established and the rule
its header states: make the TEST tolerant, never the policy faster.

WHAT THIS FILE IS FOR, in one sentence per half.

* **The request each source puts on the wire**, asserted exactly. `mdc`'s request shapes are what
  keep a title to roughly one request - TMDB's two `append_to_response` lists, OMDb's five
  parameters, Trakt's three headers and three sorts, the SPARQL with its five OPTIONAL clauses,
  TVmaze's three embeds. A shape that quietly loses a parameter costs a round trip per title
  across a whole library and shows up nowhere else.
* **The two co-keying rules `rawstore.store` names M5.3 as the shape to get wrong.**
  `entity_key = ctx.task.key`, or §6.6's board can never show the document (decision 345); and
  `url = response.request_url`, or conditional re-fetching is off for that source for ever with
  no error and no log. Both are asserted against a real Postgres and a real store, because both
  are claims about a row.

THE INTEGRATION TESTS TAKE A REAL DATABASE AND A REAL DISK, and they have to: `rawstore.store`
writes a gzip file named by its digest and an INSERT that must agree with it, `_ids.set_ids`
COALESCEs against a row that has to exist, and the conditional request is built from the previous
`raw_document` row. Skipped without TEST_DATABASE_URL; see tests/conftest.py.

`page_belongs_to_title` IS SUBSTITUTED IN THE TWO SCRAPED-SOURCE TESTS. It is `derive/parse.py`'s
(§8 stage 3's), the predicate is tested where it lives, and what is asserted here is the
ADAPTER's half of the refusal - that a page judged to be another film gets no slug, gets no
review fetch, and still leaves its bytes in the store as the honest record of what the guess
returned. The seam itself is held by a static test below: there is one predicate and this
package does not carry a second copy of it.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from spielplan.acquire import fetch, hosts, queue, rawstore, stages
from spielplan.core import secrets
from spielplan.core.config import settings
from spielplan.sources import (
    _ids,
    _views,
    base,
    metacritic,
    omdb,
    rottentomatoes,
    tmdb,
    trakt,
    tvmaze,
    wikidata,
    wikipedia,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "http"

# §4.1's partition: the app's own writes start at 1e9, so every title here is one §8 acquired
# rather than one the bundle imported. Ids are written out rather than minted, because this file
# is about the adapters and a fixture that had to walk stage 1 would fail for the driver's
# reasons. `test_reviews_gate.py` takes the same shape one package over.
HEAT = 1_000_000_901
THRONES = 1_000_000_902
OTHER = 1_000_000_903

IMDB = "tt0113277"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- the canned web ----------------------------------------------------------------------------


class _Clock:
    """A clock that moves only when something sleeps on it. `test_acquire_fetch.py:61-91`.

    Injected in place of `time.monotonic` and `asyncio.sleep` together, because the two are one
    fiction: a pacing assertion is "it slept for exactly this long", and the bucket's refill
    arithmetic then has to see that time actually passed.
    """

    _SPIN_LIMIT = 100

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if len(self.slept) > self._SPIN_LIMIT:
            raise AssertionError(
                f"paced {self._SPIN_LIMIT} times without progress; last wait was {seconds!r}"
            )
        self.now += seconds


def _low(lo: float, _hi: float) -> float:
    """Jitter pinned to the bottom of its band, so a wait is a number and not a distribution."""
    return lo


class _Site:
    """A canned web: a route table keyed on (host, path), and a log of every request made.

    Serves `robots.txt` permissively by default, because two of the eight hosts are crawled with
    `respect_robots=True` (`acquire/hosts.py`) and a test about a scorecard would otherwise be a
    test about a missing robots file. The log is the assertion surface for "issued no request at
    all", which is the shape decision 377 gives an absent credential.
    """

    ROBOTS = b"User-agent: *\nAllow: /\n"

    def __init__(self, routes) -> None:
        # A route value is either a `(status, body, headers)` triple or a callable taking the
        # request and answering one, which is what lets a host with ONE endpoint per source -
        # Wikipedia's `/w/api.php` answers both the search and the extract - be served the way
        # it really behaves rather than by two paths it does not have.
        self.routes = routes
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=self.ROBOTS,
                                  headers={"content-type": "text/plain"})
        route = self.routes.get((request.url.host, request.url.path))
        if callable(route):
            route = route(request)
        if route is None:
            return httpx.Response(404, content=b"not found",
                                  headers={"content-type": "text/html"})
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers)

    @property
    def fetched(self) -> list[httpx.Request]:
        """Every request that was not a robots.txt read."""
        return [r for r in self.seen if r.url.path != "/robots.txt"]

    def one(self, path: str) -> httpx.Request:
        hits = [r for r in self.fetched if r.url.path == path]
        assert len(hits) == 1, f"expected exactly one request to {path}, got {len(hits)}"
        return hits[0]


def _route(body: bytes, *, status: int = 200, content_type: str = "application/json",
           **headers: str) -> tuple[int, bytes, dict[str, str]]:
    return status, body, {"content-type": content_type, **headers}


JSON_ROUTE = "application/json"
HTML_ROUTE = "text/html; charset=utf-8"


def _fetcher(site: _Site, clock: _Clock, conn=None) -> fetch.Fetcher:
    return fetch.Fetcher(conn=conn, transport=httpx.MockTransport(site.handler),
                         clock=clock, sleep=clock.sleep, jitter=_low)


def _task(key: str) -> queue.Task:
    return queue.Task(id=1, kind="acquire", key=key, payload={}, attempts=1,
                      max_attempts=5, priority=50, paid=False)


def _ctx(conn, fetcher: fetch.Fetcher, title_id: int, *, key: str = "") -> stages.StageContext:
    """A stage context with the fetcher decision 373 puts on it.

    The field is set on the instance rather than passed to the constructor: `StageContext` is a
    plain dataclass and decision 373 gives it a `fetcher` field in this milestone's stage-wiring
    step, which is a later step than this one. Reading `ctx.fetcher` is the contract either way,
    so nothing here changes when the field is declared.
    """
    ctx = stages.StageContext(conn=conn, task=_task(key or f"title:{title_id}"),
                              title_id=title_id, run_id=None)
    ctx.fetcher = fetcher
    return ctx


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it.

    `DATA_DIR` and not a parameter, for `test_acquire_rawstore.py`'s reason: `settings().raw_dir`
    is the store's root in production and `rawstore` takes no root argument, because a root that
    can be passed in is a root a caller can pass wrong.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def _title(conn, title_id: int, *, kind: str = "movie", name: str = "Heat",
                 year: int | None = 1995, imdb_id: str | None = IMDB, **columns) -> None:
    keys = ["id", "kind", "name", "origin", "year", "imdb_id", *columns]
    values = [title_id, kind, name, "acquired", year, imdb_id, *columns.values()]
    places = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    await conn.execute(
        f"INSERT INTO title ({', '.join(keys)}) VALUES ({places})", *values
    )


async def _documents(conn, title_id: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT source, kind, entity_key, url, http_status, ok, error, etag, last_modified,"
        "       page, byte_size, request_meta"
        "  FROM raw_document WHERE entity_key = $1 ORDER BY id",
        f"title:{title_id}",
    )
    return [dict(row) for row in rows]


async def _configure(conn, name: str, config: dict, secret: dict | None) -> None:
    await secrets.put_connector_secrets(conn, name, config, secret)


@pytest.fixture
async def keyed(db, secrets_key):
    """All three keyed sources configured the way §2 says they are: in `connector_config`.

    Takes `secrets_key` because two of the three carry a sealed secret and §2 puts every one of
    them behind the DEK. Trakt's client id is in the CONFIG blob instead, which is
    `registry.env_seeds`' own split (`:94-98`) - it travels in a request header on every call and
    is not a credential - so this fixture exercises both shapes rather than one.
    """
    await _configure(db, "tmdb", {}, {"api_key": "tmdb-test-key"})
    await _configure(db, "omdb", {}, {"api_key": "omdb-test-key"})
    await _configure(db, "trakt", {"client_id": "trakt-test-client"}, None)
    return db


# --- the registry: which kinds exist, and in what order -----------------------------------------


def test_stage_two_registers_the_eight_sources_section_eight_names_and_no_ninth():
    """§8 stage 2 names eight sources (`spec:365-368`) and decision 374 refuses a ninth.

    Letterboxd is the one the corpus carries and this app does not: `mdc/sources/letterboxd.py`
    is 63 lines and §8 does not name it, so the host is never crawled - while
    `title.letterboxd_slug` is still filled, by `wikidata:resolve`, which yields P6127 in the one
    answer it was asking for anyway.
    """
    base.load_all()
    assert {spec.source for spec in base.REGISTRY.values()} == {
        "tmdb", "wikidata", "omdb", "trakt", "wikipedia", "tvmaze",
        "rottentomatoes", "metacritic",
    }
    assert "letterboxd" not in base.REGISTRY
    assert not any(spec.source == "letterboxd" for spec in base.REGISTRY.values())


def test_the_kinds_run_in_the_order_section_eight_lists_them():
    """The order inside stage 2 is load-bearing, so it is an assertion and not a comment.

    `spec:365-368` lists them in this sequence and the driver runs what `available_kinds`
    returns. Three of the eleven numbers differ from the corpus's and each is argued where it is
    written: `wikidata:resolve` moves 15 -> 25 and `trakt:comments` 70 -> 45 because the corpus's
    were a wholesale crawl's phases, and both are named changes in their modules.
    """
    base.load_all()
    assert base.available_kinds({"tmdb": True, "omdb": True, "trakt": True}) == [
        "tmdb:resolve", "tmdb:detail", "wikidata:resolve", "omdb:detail",
        "trakt:summary", "trakt:comments", "wikipedia:article", "tvmaze:show",
        "rt:page", "metacritic:page", "metacritic:reviews",
    ]


def test_wikidata_resolves_before_either_scraped_source():
    """§8: `wikidata:resolve` "halves guessing" because it yields the MC/RT/Letterboxd slugs.

    The whole of that clause is an ORDERING: run it first and the two scraped sources read an
    identifier, run it second and they build a url out of a name that cannot tell two films of
    one name apart. Asserted on the numbers as well as on the sequence, because
    `available_kinds` sorts on `default_priority` and a tie would put `metacritic:page` first on
    the alphabet.
    """
    base.load_all()
    order = base.available_kinds({})
    assert order.index("wikidata:resolve") < order.index("rt:page")
    assert order.index("wikidata:resolve") < order.index("metacritic:page")
    assert base.REGISTRY["wikidata:resolve"].default_priority < min(
        base.REGISTRY["rt:page"].default_priority,
        base.REGISTRY["metacritic:page"].default_priority,
    )


def test_an_install_with_no_keys_still_runs_the_five_keyless_sources():
    """§3.1 calls a half-configured boot legal, and five of the eight sources need no key at all.

    `requires` is the capability name for the three that do; `available_kinds` filters on it, so
    an install that has configured nothing still crawls Wikidata, Wikipedia, TVmaze and both
    scraped hosts rather than skipping stage 2 entirely.
    """
    base.load_all()
    assert base.available_kinds({}) == [
        "wikidata:resolve", "wikipedia:article", "tvmaze:show",
        "rt:page", "metacritic:page", "metacritic:reviews",
    ]
    assert {base.REGISTRY[k].requires for k in ("tmdb:resolve", "tmdb:detail")} == {"tmdb"}
    assert base.REGISTRY["omdb:detail"].requires == "omdb"
    assert base.REGISTRY["trakt:summary"].requires == "trakt"


# --- the slug candidates, at the value ---------------------------------------------------------


def _row(**columns) -> dict:
    base_row = {"id": HEAT, "kind": "movie", "name": "Heat", "original_name": None,
                "year": 1995, "rt_slug": None, "metacritic_slug": None}
    return {**base_row, **columns}


def test_a_rotten_tomatoes_slug_wikidata_supplied_is_the_only_candidate():
    """This is "halves guessing" measured: one candidate instead of two, and the right one.

    Wikidata stores these with the type prefix already attached, so both spellings normalise to
    one path rather than producing `m/m/heat` (`mdc/sources/rottentomatoes.py:34-36`).
    """
    assert rottentomatoes.candidate_paths(_row(rt_slug="m/heat")) == ["m/heat"]
    assert rottentomatoes.candidate_paths(_row(rt_slug="heat")) == ["m/heat"]
    assert rottentomatoes.candidate_paths(_row(kind="series", rt_slug="heat")) == ["tv/heat"]


def test_a_guessed_rotten_tomatoes_slug_tries_the_year_qualified_form_first():
    """"Where RT has had to disambiguate two films of one name, the qualified slug is the only
    one of the pair that can be this title" (`:42-44`). So it is tried first, and the bare form
    is the fallback rather than the guess."""
    assert rottentomatoes.candidate_paths(_row()) == ["m/heat_1995", "m/heat"]
    assert rottentomatoes.candidate_paths(_row(year=None)) == ["m/heat"]
    assert rottentomatoes.candidate_paths(
        _row(name="The Hitch-Hiker", year=1953)
    ) == ["m/the_hitch_hiker_1953", "m/the_hitch_hiker"]


def test_a_title_with_no_name_at_all_produces_no_candidate_rather_than_untitled():
    """`_ids.slugify` answers "untitled" for an empty string, which would be a url - and a url
    that RT may well serve. Both scraped adapters check the name before they slugify it."""
    assert rottentomatoes.candidate_paths(_row(name=None, original_name=None)) == []
    assert metacritic.slug_candidates(_row(name=None, original_name=None)) == []


def test_a_guessed_metacritic_slug_tries_the_year_qualified_form_first():
    """Metacritic's own disambiguation is a `-year` suffix rather than RT's `_year`, and the
    argument is the same: "The Beekeeper" (1986) and (2024) both produce `movie/the-beekeeper`,
    "and whichever page exists would hand its reviews to both" (`mdc/sources/metacritic.py:31-34`).
    """
    assert metacritic.slug_candidates(_row()) == ["movie/heat-1995", "movie/heat"]
    assert metacritic.slug_candidates(_row(year=None)) == ["movie/heat"]
    assert metacritic.slug_candidates(_row(metacritic_slug="movie/heat")) == ["movie/heat"]
    assert metacritic.slug_candidates(_row(metacritic_slug="heat")) == ["movie/heat"]
    assert metacritic.slug_candidates(
        _row(kind="series", metacritic_slug="heartland")) == ["tv/heartland"]


# --- the wikipedia search's two refusals, at the value -----------------------------------------


@pytest.mark.parametrize(
    ("article", "title", "overlaps"),
    [
        ("Heat (1995 film)", "Heat", True),
        # Bare containment used to be enough, which is how *Ragnarok* got *Thor: Ragnarok*.
        ("Thor: Ragnarok", "Ragnarok", False),
        ("The Return of the King", "Return", False),
        # Articles that are never a film's own page, however well the words overlap.
        ("List of Heat characters", "Heat", False),
        ("2026 in film", "The Right of Youth", False),
        ("Heat (magazine)", "Heat", False),
        # An all-stopword title can only be taken on an exact match.
        ("The Return (2003 film)", "The Return", True),
    ],
)
def test_an_article_name_must_plausibly_be_this_film(article, title, overlaps):
    """`mdc/sources/wikipedia.py:52-95`, and every row here is a film it got wrong first.

    A search hit is the same class of risk as a guessed slug: the article's *Critical reception*
    section becomes this title's pack, and §8 stage 7 verifies a quote against that pack - so a
    quote from the wrong film verifies perfectly.
    """
    assert wikipedia._title_overlaps(article, title) is overlaps


def test_a_disambiguated_hit_whose_year_contradicts_the_title_is_refused():
    """Taking the top hit blindly gave *Obsession* (2015, Dutch) the article for *Obsession (1976
    film)* - "and with it a reception section describing Columbia's 1976 release" (`:99-106`).

    One year of tolerance, because a release date and an article's chosen year disagree by one
    across a festival premiere often enough that refusing on it would cost more than it saves.
    """
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 2015) is None
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 1976) == "Obsession (1976 film)"
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 1977) == "Obsession (1976 film)"
    # Undisambiguated - no contradicting evidence, so it is taken.
    assert wikipedia._pick(["Obsession"], "Obsession", 2015) == "Obsession"
    # The first hit is refused on the words and the second is taken: order is not the rule.
    assert wikipedia._pick(
        ["Heat (magazine)", "Heat (1995 film)"], "Heat", 1995) == "Heat (1995 film)"


# --- decision 372: an adapter writes bytes and identity, and nothing else -----------------------


@pytest.mark.parametrize(
    "column", ["overview", "kind", "name", "year", "poster_path", "placement", "is_owned"]
)
async def test_the_identity_write_refuses_a_column_outside_the_identity_set(column):
    """Decision 372 in code rather than in a docstring, and a ValueError rather than a filter.

    `kind` is the one worth naming: `mdc/sources/tmdb.py:81-84` flips it when IMDb and TMDB
    disagree, and §4.1 rule 5 makes it the partition every ranking surface reads. A caller that
    silently had the argument dropped would LOOK like it had written it, which is the worse of
    the two failures - so the refusal is raised before any connection is touched, and `None` is
    passed here to prove that.
    """
    with pytest.raises(ValueError, match="may not write"):
        await _ids.set_ids(None, HEAT, **{column: "x"})


# Decision 372's eight, plus `letterboxd_slug` (decision 374 names it: the column is filled by
# `wikidata:resolve` and the host is never crawled) and `trakt_id` (the same `ids` blob as
# `trakt_slug`, on the one request that is made either way). Written out here rather than
# imported, because a test that asserted the list against itself would assert nothing.
_DECISION_372_COLUMNS = frozenset({
    "imdb_id", "tmdb_id", "tvdb_id", "trakt_id", "trakt_slug", "letterboxd_slug",
    "rt_slug", "metacritic_slug", "wikidata_id", "wikipedia_title",
})


def test_the_identity_set_is_section_eight_stage_two_s_and_holds_no_surface_field():
    """Decision 372's list, plus the two it argues for and minus the one §7.1 owns.

    `jellyfin_id` is an identity column and is NOT here: `connectors/resolve.py:306` owns it, no
    third-party source knows it, and an adapter that could write it could point a household's
    library row at a different film.
    """
    assert _ids.ID_COLUMNS == _DECISION_372_COLUMNS
    assert "kind" not in _ids.ID_COLUMNS
    assert "jellyfin_id" not in _ids.ID_COLUMNS


_ADAPTERS = ("tmdb", "wikidata", "omdb", "trakt", "wikipedia", "tvmaze",
             "rottentomatoes", "metacritic")
_PACKAGE = Path(base.__file__).resolve().parent

# What a query looks like on an asyncpg connection. `test_sources_base.py:520-527` uses the same
# list one module over, for `credentials.py`.
_DB_VERBS = ("fetch", "fetchrow", "fetchval", "execute", "executemany", "cursor",
             "copy_records_to_table")


def _db_calls(source: str) -> list[str]:
    return sorted({
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DB_VERBS
    })


def _sql_literals(source: str) -> list[str]:
    """Every string constant in `source` that is not a docstring.

    The exclusion is load-bearing rather than tidy: these modules QUOTE the statement they exist
    to refuse - `tmdb.py`'s port verdict names
    `UPDATE title SET primary_title, original_title, year, runtime_min ...` by hand, because
    decision 372 is only readable if the thing it forbids is on the page. A rule over the whole
    file would be discharged by deleting the argument for it, which is the worst possible
    incentive to put on a comment.
    """
    tree = ast.parse(source)
    docstrings = {id(ast.parse(source))}
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_adapter_writes_to_the_title_table_except_through_the_identity_write():
    """The strongest form decision 372's rule can take: the adapters carry no write at all.

    Every column an adapter may fill is in `_ids.ID_COLUMNS` and `_ids.set_ids` is the only
    statement that touches `title`, so a module that wrote `overview` would have to issue its own
    UPDATE - which this reads the eight files for. The corpus's own
    `UPDATE title SET primary_title=..., year=..., runtime_min=...`
    (`mdc/sources/tmdb.py:154-170`) is exactly the shape being refused, and it is not a
    hypothetical: it is thirteen lines in the file this package was ported from.
    """
    offenders = {}
    for name in _ADAPTERS:
        source = (_PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        writes = [verb for verb in _db_calls(source) if verb in ("execute", "executemany")]
        sql = [s for s in _sql_literals(source)
               if "update title" in s.lower() or "insert into title" in s.lower()]
        if writes or sql:
            offenders[name] = writes + sql
    assert not offenders, (
        f"{offenders} write to the database directly. Decision 372 lets a stage 2 adapter write "
        "the raw bytes and the identity columns and nothing else, and `_ids.set_ids` is the one "
        "place that list is enforced."
    )
    # Both detectors fire on the thing they forbid, and the second one ignores the docstrings
    # that quote it - otherwise the rule would be discharged by deleting its own argument.
    illegal = "async def f(conn):\n    await conn.execute('UPDATE title SET overview=$1')\n"
    assert _db_calls(illegal) == ["execute"]
    assert _sql_literals(illegal) == ["UPDATE title SET overview=$1"]
    assert _sql_literals('"""Never UPDATE title SET overview."""\nx = 1\n') == []


# Every way a module of this package could open a socket without the app's own transport.
# `test_derive_parse.py:1097`'s list, minus the two names that are this package's own business:
# `spielplan.acquire.fetch` is ruled on by module below rather than banned outright, and
# `spielplan.connectors` is not a rule about `sources/` at all. `urllib.request` and never bare
# `urllib`, because `rottentomatoes.py:48` and `wikidata.py:50` import `urllib.parse` for
# `urlparse` and `unquote`, which is string work and not a socket.
_TRANSPORT = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "socket", "aiohttp")

# The app's own transport, and the three modules that may name it. `_views` is the DOOR. `omdb`
# and `wikipedia` name it for its `Response` TYPE, which is a name and not a client - asserted
# below by refusing a CALL through it, rather than left as a claim in this docstring.
_FETCHER_MODULE = "spielplan.acquire.fetch"
_MAY_NAME_THE_FETCHER = {"_views.py", "omdb.py", "wikipedia.py"}

# The modules this package holds that are not adapters, so the walk below can prove it read the
# whole package rather than whatever it happened to find. A ninth adapter is picked up by the
# `rglob` on the day it lands; these five are named because a walk that silently stopped finding
# them would be a green guard over nothing.
_SUPPORT_MODULES = {"_views.py", "base.py", "_ids.py", "_htmlutil.py", "credentials.py"}


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """`from ..acquire import fetch` inside `spielplan.sources` is `spielplan.acquire.fetch`."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    prefix = ".".join(parts[: len(parts) - node.level + 1])
    return f"{prefix}.{node.module}" if node.module else prefix


def _imported_modules(source: str, *, package: str = "spielplan.sources") -> set[str]:
    """Every module `source` imports, plus each `from x import y` recorded as `x.y` as well."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names if module)
    return modules


def _transport(source: str) -> list[str]:
    return sorted(
        m for m in _imported_modules(source)
        if any(m == bad or m.startswith(f"{bad}.") for bad in _TRANSPORT)
    )


def _reaches_for_the_fetcher(source: str) -> bool:
    """Whether `source` touches a `.fetcher` attribute - `ctx.fetcher`, and any spelling of it.

    AST and not a substring, because every module in this package ARGUES about the fetcher in
    prose and a rule over the bare word would be discharged by deleting its own explanation.
    """
    return any(
        isinstance(node, ast.Attribute) and node.attr == "fetcher"
        for node in ast.walk(ast.parse(source))
    )


def _calls_through(source: str, name: str) -> list[str]:
    """`name.something(...)`, which is what separates naming a module from using one."""
    return sorted({
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == name
    })


def test_only_one_module_in_the_package_reaches_for_the_fetcher():
    """Decision 340: every request goes through `acquire/fetch.Fetcher`, and there is one door.

    `_views` is that door - it is where `entity_key` and `request_url` are decided - so an
    adapter importing `httpx` or `acquire.fetch` for itself would be an adapter that could open
    a socket the per-host bucket, the breaker and robots.txt never saw. `omdb` and `wikipedia`
    import `acquire.fetch` for its `Response` TYPE only, which is a name and not a client.

    WHAT THIS USED TO ASSERT WAS TWO THINGS OVER EIGHT FILES, and neither was the door. It refused
    `httpx` and the literal `Fetcher(` across a hardcoded adapter list, which let through every
    other spelling of a socket - `import socket`, `import http.client`, `urllib.request.urlopen`,
    `aiohttp` - and never read `_ids.py`, `credentials.py` or `__init__.py` at all. Worse, it
    could not see the CHEAPEST bypass there is: decision 373 hands the fetcher to a stage on the
    context, so `await ctx.fetcher.get(url)` inside an adapter needs no import whatsoever. That
    call is polite - it goes through the bucket, the breaker and robots.txt - and it still skips
    `_views`, which is where `entity_key=ctx.task.key` and `url=response.request_url` are decided.
    Bytes fetched around it are bytes §6.6's board can never show and stage 3 can never re-parse,
    which is `_views.py`'s own header ("the co-keying is written ONCE, here") and decision 345.

    So the rule is now the one the test's NAME claims: the whole package, every transport
    spelling, and `.fetcher` named nowhere but the door. `test_sources_base.py`'s narrower guard
    over `base.py` and `_htmlutil.py` stays as it is - it is a statement about what the PARSERS'
    dependencies import, with a self-test of its own, and this walk being a superset of it is not
    a reason to leave either unable to fail.
    [decisions 340, 345, 372, 373; M5.3 review cycle 1, M53-C1-NET-05, M53-COV-02]
    """
    walked = sorted(_PACKAGE.rglob("*.py"))
    owed = ({f"{name}.py" for name in _ADAPTERS} | _SUPPORT_MODULES) - {p.name for p in walked}
    assert not owed, f"the walk did not read {sorted(owed)}; it is looking in the wrong place"

    for path in walked:
        name = path.name
        source = path.read_text(encoding="utf-8")
        assert not _transport(source), (
            f"sources/{name} reaches for transport {_transport(source)}: decision 340 puts "
            "robots.txt, the token bucket, the concurrency cap and the breaker in one layer, and "
            "a second client is polite to nobody"
        )
        assert "Fetcher(" not in source, (
            f"sources/{name} constructs a Fetcher: decision 373 builds one per drain and "
            "hands it to every stage on the context"
        )
        if name not in _MAY_NAME_THE_FETCHER:
            assert _FETCHER_MODULE not in _imported_modules(source), (
                f"sources/{name} imports {_FETCHER_MODULE}: `_views` is the one door, and the "
                "two modules that name it do so for its Response type"
            )
        if name != "_views.py":
            assert not _reaches_for_the_fetcher(source), (
                f"sources/{name} reaches for `ctx.fetcher` itself. That request is polite and it "
                "is still wrong: `_views` is where `entity_key` and `request_url` are decided, "
                "and a response fetched around it is a document section 6.6 can never show and "
                "stage 3 can never re-parse (decisions 345, 372)"
            )
    for name in ("omdb.py", "wikipedia.py"):
        called = _calls_through((_PACKAGE / name).read_text(encoding="utf-8"), "fetch")
        assert not called, (
            f"sources/{name} calls {called} through `fetch`, which this guard exempts it for "
            "naming as a TYPE only; a call through it is a second client"
        )


def test_the_fetcher_guard_can_report_every_way_in():
    """A guard that cannot report a violation is a green line rather than a proof.

    The last two are the ones the guard was rebuilt for: the import-free bypass, and the relative
    spelling that `node.module` alone reports as nothing.
    """
    for illegal in ("import httpx\n", "from httpx import AsyncClient\n", "import requests\n",
                    "from urllib.request import urlopen\n", "import socket\n",
                    "from socket import create_connection\n", "import http.client\n",
                    "import aiohttp\n", "import urllib3\n"):
        assert _transport(illegal), f"the guard missed: {illegal.strip()}"
    # The two names this package legitimately imports from the same top-level packages.
    assert not _transport("from urllib.parse import urlparse\nfrom urllib.parse import unquote\n")
    assert not _transport("import json\nfrom spielplan.sources import _ids, _views\n")

    for spelling in ("from spielplan.acquire import fetch\n", "from ..acquire import fetch\n",
                     "from spielplan.acquire.fetch import Fetcher\n",
                     "import spielplan.acquire.fetch\n"):
        assert _FETCHER_MODULE in _imported_modules(spelling), f"the guard missed: {spelling!r}"

    assert _reaches_for_the_fetcher("async def f(ctx):\n    return await ctx.fetcher.get('u')\n")
    assert not _reaches_for_the_fetcher('"""The fetcher is handed to the stage."""\nx = 1\n')
    assert _calls_through("x = fetch.get('u')\n", "fetch") == ["get"]
    assert _calls_through("def f(r: fetch.Response) -> None:\n    raise fetch.HostPaused\n",
                          "fetch") == []


def test_the_identity_check_is_the_parsers_one_and_not_a_second_copy():
    """`page_belongs_to_title` is `derive/parse.py`'s, borrowed by the two scraped adapters.

    A second copy here would be the failure that matters: a wrong page writes another film's
    reviews into this title's pack, §8 stage 7's quote verification passes them because the
    quote really is in the pack, and two implementations of the refusal means one of them is the
    one that was not fixed. Read statically because the call is a deferred import - see
    `_ids.belongs_to_title` for why it is deferred - so importing this module does not prove it.
    """
    body = inspect.getsource(_ids.belongs_to_title)
    assert "from spielplan.derive.parse import page_belongs_to_title" in body, (
        "sources/_ids.belongs_to_title no longer resolves to the parsers' predicate"
    )
    for name in ("rottentomatoes", "metacritic"):
        source = (_PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        assert "_ids.belongs_to_title" in source, f"sources/{name}.py stopped checking the page"
        assert "def page_belongs_to_title" not in source, (
            f"sources/{name}.py carries its own copy of the identity check"
        )


# --- the requests each source makes ------------------------------------------------------------


async def test_tmdb_resolve_exchanges_the_imdb_id_and_writes_only_the_tmdb_id(db, raw_root, keyed):
    """One request to `/find/{imdb}?external_source=imdb_id`, and one column filled."""
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(_fixture("tmdb_find.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.resolve(_ctx(db, fetcher, HEAT))

    request = site.one(f"/3/find/{IMDB}")
    assert parse_qs(request.url.query.decode()) == {
        "api_key": ["tmdb-test-key"], "external_source": ["imdb_id"]
    }
    assert result.ok and result.note == "tmdb_id=949"
    row = await db.fetchrow(
        "SELECT tmdb_id, name, year, overview FROM title WHERE id = $1", HEAT)
    assert row["tmdb_id"] == 949
    # Decision 372: everything a card renders is stage 3's, out of the raw store.
    assert (row["name"], row["year"], row["overview"]) == ("Heat", 1995, None)


async def test_tmdb_detail_asks_for_the_append_list_that_keeps_a_title_to_one_request(db, raw_root, keyed):
    """`append_to_response` is the whole argument for this source's request count.

    Asserted as the exact list rather than as "contains credits", because an entry quietly lost
    is another round trip per title across a whole library, and nothing else in the system would
    notice. The identity half - `imdb_id`, `tvdb_id`, `wikidata_id` out of `external_ids` - is
    the only write, and `overview` staying NULL is decision 372.
    """
    await _title(db, HEAT, tmdb_id=949, imdb_id=None)
    site = _Site({("api.themoviedb.org", "/3/movie/949"):
                  _route(_fixture("tmdb_movie_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.detail(_ctx(db, fetcher, HEAT))

    query = parse_qs(site.one("/3/movie/949").url.query.decode())
    assert query["append_to_response"] == [
        "credits,keywords,external_ids,release_dates,reviews,videos,alternative_titles,"
        "recommendations,similar,watch/providers"
    ]
    assert query["language"] == ["en-US"]
    assert result.ok and "37 reviews" in result.note
    row = await db.fetchrow(
        "SELECT imdb_id, tvdb_id, wikidata_id, overview, runtime_min FROM title WHERE id = $1",
        HEAT)
    assert (row["imdb_id"], row["tvdb_id"], row["wikidata_id"]) == (IMDB, 70328, "Q124070")
    assert (row["overview"], row["runtime_min"]) == (None, None)


async def test_an_identity_write_fills_and_never_clobbers(db, raw_root, keyed):
    """Decision 372 says `COALESCE(existing, new)`, and §4.1 rule 6 is why it is not tidiness.

    These columns carry legitimate duplicates - "315/171/... duplicate values exist, mostly
    legitimate movie/series pairs" (`0003_content.sql:24-25`) - so "TMDB now says 949" is not
    evidence that the 7 on the row is wrong; it is evidence that one of the two is a different
    work. Under decision 162 content seeds once, so a clobber is unrecoverable by any gesture the
    household has. The empty column beside it is filled in the same statement, which is the half
    that proves the write happened at all rather than being skipped.
    """
    await _title(db, HEAT, tmdb_id=949, tvdb_id=7, wikidata_id=None)
    site = _Site({("api.themoviedb.org", "/3/movie/949"):
                  _route(_fixture("tmdb_movie_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.detail(_ctx(db, fetcher, HEAT))

    row = await db.fetchrow(
        "SELECT imdb_id, tvdb_id, wikidata_id FROM title WHERE id = $1", HEAT)
    # The response says tvdb_id 70328 and the row already says 7. The row wins.
    assert row["tvdb_id"] == 7, "an identity column already filled is never overwritten"
    assert row["imdb_id"] == IMDB, "the row already carried this one, and it is unchanged"
    assert row["wikidata_id"] == "Q124070", "the empty one is filled in the same statement"
    assert result.ok


async def test_a_series_detail_asks_for_the_television_append_list_and_the_tv_path(db, raw_root, keyed):
    """The two lists differ - `aggregate_credits` and `content_ratings` where a film has
    `credits` and `release_dates` - because TMDB's television endpoints do
    (`mdc/sources/tmdb.py:26-31`). A series fetched down the movie path returns a 404."""
    await _title(db, THRONES, kind="series", name="Game of Thrones", year=2011,
                 imdb_id="tt0944947", tmdb_id=1399)
    site = _Site({("api.themoviedb.org", "/3/tv/1399"):
                  _route(_fixture("tmdb_movie_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await tmdb.detail(_ctx(db, fetcher, THRONES))

    query = parse_qs(site.one("/3/tv/1399").url.query.decode())
    assert query["append_to_response"] == [
        "aggregate_credits,content_ratings,external_ids,keywords,reviews,videos,"
        "alternative_titles,recommendations,similar,watch/providers"
    ]
    assert (await _documents(db, THRONES))[0]["kind"] == "tv_detail"


async def test_tmdb_never_flips_the_kind_when_the_provider_disagrees(db, raw_root, keyed):
    """`mdc/sources/tmdb.py:79-84` flips `title.kind` and this does not (decision 372).

    §4.1 rule 5 makes `kind` the partition every ranking surface reads - "the unpartitioned crowd
    top-10 is 8/10 TV series" - and decision 162 makes the write permanent, so the disagreement
    is a note an operator can act on rather than a silent move between two ranking universes.
    """
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(_fixture("tmdb_find_other_kind.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.resolve(_ctx(db, fetcher, HEAT))

    # `ok`, WHICH THIS LINE USED TO DENY: TMDB answered the question it was asked, and what it said
    # is a fact about the film. Reported as a failure it was the same value a refused key is, and
    # decision 334's park could not tell the two apart. [M5.3 review cycle 2, m53-c2-334-01]
    assert result.ok and result.ran
    assert "files tt0113277 as a series" in result.note
    assert "kind is not a stage 2 write" in result.note
    row = await db.fetchrow("SELECT kind, tmdb_id FROM title WHERE id = $1", HEAT)
    assert (row["kind"], row["tmdb_id"]) == ("movie", None)


async def test_a_title_tmdb_has_never_heard_of_is_an_answer_and_not_a_failure(db, raw_root, keyed):
    """21% of this schema's titles carry no IMDb id (`0003_content.sql:20-22`) and the ones that
    do are not all in TMDB. The answer is stored - a `find` that came back empty is the record
    that the question was asked - and the note names the id that was asked about, because that is
    what an operator needs to look the film up by hand.

    AND THE RESULT SAYS `ok`, which is this test's own title and which its assertion used to
    contradict. `ok = tmdb_id is not None` put this answer and a 401 into one value, so
    `stages.enrich` could not park on the second without parking on the first; `ok=False` on a
    tmdb kind now means a request that failed. [M5.3 review cycle 2, m53-c2-334-01]"""
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(_fixture("tmdb_find_empty.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.resolve(_ctx(db, fetcher, HEAT))

    assert result.ok and result.ran and result.note == f"TMDB has no record for {IMDB}"
    assert [d["ok"] for d in await _documents(db, HEAT)] == [True]
    assert await db.fetchval("SELECT tmdb_id FROM title WHERE id = $1", HEAT) is None


@pytest.mark.parametrize("status", [401, 500])
async def test_a_resolve_tmdb_refused_or_failed_is_a_failure_and_not_an_answer(
    db, raw_root, keyed, status
):
    """The other half of the test above, and the half decision 334's park reads.

    A refused key and a host having a bad hour leave `title.tmdb_id` exactly as empty as a film
    TMDB has never heard of, so the column cannot be what tells them apart - `ok` is. This kind
    RAN and did not answer, which is decision 334's park condition for the required source, and
    `stages.enrich` can only see it if the value here differs from the no-record one.
    [M5.3 review cycle 2, m53-c2-334-01]
    """
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(b'{"status_message": "no"}', status=status)})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.resolve(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.ran, result
    assert str(status) in result.note, result.note
    assert await db.fetchval("SELECT tmdb_id FROM title WHERE id = $1", HEAT) is None


async def test_a_title_wikidata_holds_no_item_for_leaves_every_slug_unguessed(db, raw_root):
    """An empty binding list is a 200 with a well-formed body, so nothing in the transport can
    tell. The consequence is the one §8 cares about: both scraped sources fall back to guessing,
    which is exactly the state `wikidata:resolve` halves - so the note says which property found
    nothing rather than reporting a generic miss."""
    await _title(db, HEAT)
    site = _Site({("query.wikidata.org", "/sparql"):
                  _route(_fixture("wikidata_empty.json"),
                         content_type="application/sparql-results+json")})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikidata.resolve(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == f"wikidata holds no item with P345 = {IMDB}"
    assert rottentomatoes.candidate_paths(
        await _ids.title_row(db, HEAT)) == ["m/heat_1995", "m/heat"]


async def test_wikidata_resolves_one_title_with_one_binding_and_fills_four_slugs(db, raw_root):
    """Decision 374: the batch shape is not ported, so `VALUES ?imdb` holds exactly one id.

    The four identifiers are the whole of "halves guessing": RT's and Metacritic's slugs stop
    both scraped sources guessing, the article title stops Wikipedia searching, and Letterboxd's
    is filled although that host is never crawled because P6127 arrives in the same answer.
    """
    await _title(db, HEAT)
    site = _Site({("query.wikidata.org", "/sparql"):
                  _route(_fixture("wikidata_resolve.json"),
                         content_type="application/sparql-results+json")})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikidata.resolve(_ctx(db, fetcher, HEAT))

    request = site.one("/sparql")
    query = parse_qs(request.url.query.decode())["query"][0]
    assert query.count(IMDB) == 1, "one title, one binding - the batch shape is not ported"
    assert "wdt:P345" in query and "VALUES ?imdb" in query
    for prop in ("P1258", "P1712", "P6127", "P4947"):
        assert f"OPTIONAL {{ ?item wdt:{prop}" in query
    assert "schema:isPartOf <https://en.wikipedia.org/>" in query
    assert request.headers["accept"] == "application/sparql-results+json"

    assert result.ok
    row = await db.fetchrow(
        "SELECT wikidata_id, rt_slug, metacritic_slug, letterboxd_slug, wikipedia_title,"
        "       tmdb_id FROM title WHERE id = $1", HEAT)
    assert row["wikidata_id"] == "Q124070"
    assert (row["rt_slug"], row["metacritic_slug"]) == ("m/heat", "movie/heat")
    assert row["letterboxd_slug"] == "heat-1995"
    assert row["wikipedia_title"] == "Heat (1995 film)"
    # P4947 is in the query because the query is ported verbatim, and is deliberately not
    # written: `tmdb:resolve` answers that question from TMDB itself, fifty priority points up.
    assert row["tmdb_id"] is None


async def test_omdb_asks_for_the_full_plot_and_the_tomatoes_block(db, raw_root, keyed):
    """Five parameters, and `plot=full` is the one §8 stage 4's gate depends on: the short plot
    is a sentence where the full one is a paragraph, and decision 335 requires a plot."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert parse_qs(site.one("/").url.query.decode()) == {
        "apikey": ["omdb-test-key"], "i": [IMDB], "plot": ["full"],
        "tomatoes": ["true"], "r": ["json"],
    }
    assert result.ok and "Rotten Tomatoes=87%" in result.note


async def test_trakt_sends_its_three_headers_and_reads_both_of_its_own_ids(db, raw_root, keyed):
    """Trakt's client id travels in a header on every call, which is why `registry.env_seeds`
    keeps it in the CONFIG blob rather than the sealed one - and why this read answers on an
    install whose DEK will not open (decision 377)."""
    await _title(db, HEAT)
    site = _Site({
        ("api.trakt.tv", f"/movies/{IMDB}"): _route(_fixture("trakt_summary.json")),
        ("api.trakt.tv", f"/movies/{IMDB}/ratings"): _route(_fixture("trakt_ratings.json")),
        ("api.trakt.tv", f"/movies/{IMDB}/stats"): _route(_fixture("trakt_stats.json")),
    })
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await trakt.summary(_ctx(db, fetcher, HEAT))

    request = site.one(f"/movies/{IMDB}")
    assert request.headers["trakt-api-version"] == "2"
    assert request.headers["trakt-api-key"] == "trakt-test-client"
    assert request.headers["content-type"] == "application/json"
    assert parse_qs(request.url.query.decode()) == {"extended": ["full"]}
    assert result.ok
    row = await db.fetchrow("SELECT trakt_id, trakt_slug FROM title WHERE id = $1", HEAT)
    assert (row["trakt_id"], row["trakt_slug"]) == (806, "heat-1995")
    # The rating distribution and the watch statistics are documents of their own, because they
    # are separate answers: the corpus stores them under their own `kind` for the same reason.
    assert {d["kind"] for d in await _documents(db, HEAT)} == {"summary", "ratings", "stats"}


async def test_trakt_comments_keeps_the_three_sorts_apart_in_the_store(db, raw_root, keyed):
    """`COMMENT_SORTS` is the one API-sanctioned way to get a rating SPREAD per title rather than
    hoping the default ordering contains a dissenter (`mdc/sources/trakt.py:75-79`).

    The page numbers are what make the spread survive the store: the raw store's newest-per-page
    view would collapse two sorts onto one document, so `likes` page 1 is 1, `lowest` page 1 is
    11 and `highest` page 1 is 21. The corpus computes the same numbers by `.index()` on a value,
    which two sorts sharing a page budget would collide on.
    """
    await _title(db, HEAT)
    routes = {("api.trakt.tv", f"/movies/{IMDB}/comments/{sort}"):
              _route(_fixture("trakt_comments.json"))
              for sort, _pages in trakt.COMMENT_SORTS}
    site = _Site(routes)
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await trakt.comments(_ctx(db, fetcher, HEAT))

    assert result.ok and result.note == "6 comments across 3 sorts"
    pages = sorted(d["page"] for d in await _documents(db, HEAT))
    assert pages == [1, 11, 21], "two sorts sharing a page key would hide one of them"
    sorts = {d["request_meta"]["sort"] for d in await _documents(db, HEAT)}
    assert sorts == {"likes", "lowest", "highest"}


async def test_tvmaze_asks_for_its_three_embeds_in_one_request(db, raw_root):
    """Three embeds in one call is the whole reason this source costs two requests and not five
    (`mdc/sources/tvmaze.py:40-43`)."""
    await _title(db, THRONES, kind="series", name="Game of Thrones", year=2011,
                 imdb_id="tt0944947")
    site = _Site({
        ("api.tvmaze.com", "/lookup/shows"): _route(_fixture("tvmaze_lookup.json")),
        ("api.tvmaze.com", "/shows/82"): _route(_fixture("tvmaze_show.json")),
    })
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tvmaze.show(_ctx(db, fetcher, THRONES))

    assert parse_qs(site.one("/lookup/shows").url.query.decode()) == {"imdb": ["tt0944947"]}
    assert parse_qs(site.one("/shows/82").url.query.decode()) == {
        "embed[]": ["cast", "crew", "seasons"]
    }
    assert result.ok and result.note == "tvmaze 82"
    # Named change 1: the lookup answer is stored too, so a derive can tell "TVmaze does not hold
    # this series" from "nobody has run stage 2 on it".
    assert {d["kind"] for d in await _documents(db, THRONES)} == {"lookup", "show"}


async def test_tvmaze_is_not_applicable_to_a_movie_and_spends_no_request(db, raw_root):
    """"Movies only exist on tvmaze as specials" (`:24`). Reported ok, because the kind did the
    right thing: a movie skipped here is not a gap in this title's enrichment."""
    await _title(db, HEAT)
    site = _Site({})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tvmaze.show(_ctx(db, fetcher, HEAT))

    assert result.ok and result.note == "not applicable: tvmaze carries series"
    assert site.fetched == []


def _wikipedia_api(request: httpx.Request):
    """One endpoint, two questions - which is how the action API really works."""
    if b"list=search" in request.url.query:
        return _route(_fixture("wikipedia_search.json"))
    return _route(_fixture("wikipedia_article.json"))


async def test_wikipedia_searches_when_the_title_carries_no_article_and_stores_the_search(
    db, raw_root
):
    """Named change 1: the search answer is the EVIDENCE for a choice `set_ids` makes permanent.

    The corpus fetches the search and stores nothing (`:109-112`), so an operator asking why a
    title carries the wrong article has nothing to read. Here both documents land, and the top
    hit - `Heat (magazine)`, which `_NOT_A_WORK` catches - is refused in favour of the second.
    """
    await _title(db, HEAT)
    site = _Site({("en.wikipedia.org", "/w/api.php"): _wikipedia_api})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    searches = [r for r in site.fetched if b"list=search" in r.url.query]
    assert len(searches) == 1
    query = parse_qs(searches[0].url.query.decode())
    assert query["srsearch"] == ["Heat 1995 film"] and query["srlimit"] == ["5"]
    # The article title is written before the extract is fetched, so a failed extract does not
    # make the next drain search again.
    assert await db.fetchval(
        "SELECT wikipedia_title FROM title WHERE id = $1", HEAT) == "Heat (1995 film)"
    assert {d["kind"] for d in await _documents(db, HEAT)} == {"search", "article"}
    assert result.ok and result.note.endswith("(searched: Heat (1995 film))")


async def test_a_search_hit_that_names_another_year_leaves_the_title_with_no_article(
    db, raw_root
):
    """The refusal reaching the adapter's return, not only `_pick`'s.

    *Obsession* (2015, Dutch) took the article for *Obsession (1976 film)* in the corpus "and with
    it a reception section describing Columbia's 1976 release". Refusing means the title carries
    no article at all - which is the right answer, because §8 stage 4's gate can be cleared from
    a plot the other seven sources supply, and a wrong article cannot be un-written under
    decision 162.
    """
    await _title(db, HEAT, name="Obsession", year=2015, imdb_id="tt3703836")
    site = _Site({("en.wikipedia.org", "/w/api.php"):
                  _route(_fixture("wikipedia_search_wrong_year.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    assert not result.ok
    # The hit's WORDS are this film's - it is refused on the year alone, which is the whole
    # point: a refusal on the words would happen without `_pick`'s date check existing.
    assert wikipedia._title_overlaps("Obsession (1976 film)", "Obsession")
    assert result.note == (
        "no wikipedia hit for 'Obsession 2015 film' is this film: refused Obsession (1976 film)"
    )
    assert await db.fetchval("SELECT wikipedia_title FROM title WHERE id = $1", HEAT) is None
    assert len(site.fetched) == 1, "the extract is not asked for when no hit is this film"


async def test_wikipedia_asks_for_the_plain_text_extract_of_the_article_it_holds(db, raw_root):
    """`explaintext` plus `exsectionformat=wiki` is what makes the *Plot* and *Critical
    reception* sections readable at all; `redirects=1` is what makes a renamed article still
    resolve. A title that already carries an article makes ONE request, not two."""
    await _title(db, HEAT, wikipedia_title="Heat (1995 film)")
    site = _Site({("en.wikipedia.org", "/w/api.php"):
                  _route(_fixture("wikipedia_article.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    assert len(site.fetched) == 1, "the article was known, so no search was needed"
    query = parse_qs(site.one("/w/api.php").url.query.decode())
    assert query["prop"] == ["extracts|pageprops"]
    assert query["explaintext"] == ["1"] and query["exsectionformat"] == ["wiki"]
    assert query["redirects"] == ["1"] and query["titles"] == ["Heat (1995 film)"]
    assert result.ok and result.note.startswith("Heat (1995 film): ")


# --- the two co-keying rules, against a real store ----------------------------------------------


async def test_every_document_is_filed_under_the_task_key_and_never_a_provider_id(db, raw_root, keyed):
    """Decision 345 makes §6.6's board the only window onto these bytes, and `acquire/board.py`
    finds them by joining `entity_key` to `acquisition_task.key`.

    So a document filed under the IMDb id - which is what `mdc/sources/tmdb.py:73-74` does, and
    the obvious thing to reach for in a handler that was just handed one - is a document the
    board can never show. The task key here is deliberately NOT the title id, so a module that
    reconstructed the key from `ctx.title_id` would fail this rather than pass it by accident.
    """
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(_fixture("tmdb_find.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await tmdb.resolve(_ctx(db, fetcher, HEAT, key="jellyfin:abc123"))

    rows = await db.fetch("SELECT entity_key, source, kind FROM raw_document")
    assert [r["entity_key"] for r in rows] == ["jellyfin:abc123"]
    assert IMDB not in {r["entity_key"] for r in rows}


async def test_a_document_is_filed_under_the_url_the_request_was_made_against(db, raw_root, keyed):
    """`Response.url` is the LAST hop; `Response.request_url` is the string `_validators` reads.

    OMDb is the example `rawstore.store`'s co-keying paragraph names: one url for every title in
    the library, with the query as the only thing distinguishing them. Store the bare endpoint
    and one title's ETag conditions another title's request; store the final url of a redirect
    chain and the validator is filed under a name no request is ever made against again.
    """
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"):
                  _route(_fixture("omdb_detail.json"), etag='W/"omdb-1"',
                         **{"last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"})})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await omdb.detail(_ctx(db, fetcher, HEAT))

    stored = (await _documents(db, HEAT))[0]
    assert stored["url"] != "https://www.omdbapi.com/", (
        "the bare endpoint is one url for every title: `_validators` would condition one "
        "document's request on another document's ETag"
    )
    assert urlparse(stored["url"]).path == "/"
    assert parse_qs(urlparse(stored["url"]).query)["i"] == [IMDB]
    # Both validators, because they are one fact about one document.
    assert stored["etag"] == 'W/"omdb-1"'
    assert stored["last_modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"


async def test_the_stored_validators_condition_the_next_fetch_of_the_same_url(db, raw_root, keyed):
    """The whole point of the pair above, cashed: a second stage 2 asks whether the bytes it
    holds are still current instead of asking for them again.

    A 304 carries no body, so nothing is stored and the identity write is skipped - the bytes
    that produced it are the ones this app still holds (`rawstore.store` refuses a 304 as a good
    document outright). This is the cheapest politeness available to a milestone that crawls the
    open web on a household's own IP address.
    """
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"):
                  _route(_fixture("omdb_detail.json"), etag='W/"omdb-1"')})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await omdb.detail(_ctx(db, fetcher, HEAT))
        site.routes[("www.omdbapi.com", "/")] = _route(b"", status=304)
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert site.fetched[1].headers["if-none-match"] == 'W/"omdb-1"'
    assert result.ok and result.note == "unchanged since the last fetch"
    assert len(await _documents(db, HEAT)) == 1, "a 304 carries no bytes to store"


# --- decision 334: a note, not a park -----------------------------------------------------------


async def test_a_404_is_a_note_and_a_failure_row_rather_than_a_raise(db, raw_root, keyed):
    """Decision 334: only `tmdb:detail` can park stage 2; every other source's 404 is a note.

    The row is written because it is fetch history an operator chasing a stuck title reads off
    §6.6's board, and `rawstore.latest` filters on `ok` so it never reaches a parser. Asserted
    for a source the driver will advance past, which is the case the decision is about.
    """
    await _title(db, HEAT)
    site = _Site({})       # every path 404s
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await trakt.summary(_ctx(db, fetcher, HEAT))

    assert not result.ok and "404" in result.note
    stored = await _documents(db, HEAT)
    assert [d["ok"] for d in stored] == [False]
    assert stored[0]["http_status"] == 404 and "404" in stored[0]["error"]
    assert stored[0]["byte_size"] == 0


PORTAL = (b"<!doctype html><html><head><title>Sign in</title></head><body>"
          + b"<p>Accept the terms of this network to continue browsing.</p>" * 20
          + b"</body></html>")


@pytest.mark.parametrize("source", ["tmdb", "wikidata"])
async def test_a_json_source_answering_with_an_interception_page_files_no_good_document(
    db, raw_root, keyed, source
):
    """A captive portal, a transparent proxy or an ISP hijack page: 200, HTML, on every host.

    THE ONLY NON-FIXTURE ANSWER ON A HOME NETWORK THAT IS A 200, and therefore the only one
    `MIN_HTML_BYTES` and the failure arms both walk past. Until M5.3's first review cycle the
    decode was a per-source `verdict` passed at two of eight call sites - OMDb's and Wikipedia's
    extract hop - so the other six stored the portal's body `ok = true` WITH its ETag and then
    raised on `.json()`. `rawstore.store`'s own rule is the one being kept here: "a row that says
    `ok` says there are bytes to parse". A junk row filed good becomes the newest document for its
    `(source, kind, page)`, displacing the last good one for both `rawstore.latest` and the
    derive, and its label still enters decision 375's delete scope - so the next re-derive drops
    that source's rows and re-inserts nothing.

    THE RAISE WAS NEVER THE HARM, and this test does not assert one. `stages.enrich` catches it
    and records it as that source's note, which is decision 334's own sentence; what was wrong was
    the row and the validator. [M5.3 review cycle 1, M53-C1-NET-02]
    """
    portal = _route(PORTAL, content_type=HTML_ROUTE, etag='W/"portal"')
    await _title(db, HEAT, tmdb_id=949)
    site = _Site({("api.themoviedb.org", "/3/movie/949"): portal,
                  ("query.wikidata.org", "/sparql"): portal})
    handler = tmdb.detail if source == "tmdb" else wikidata.resolve
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await handler(_ctx(db, fetcher, HEAT))

    assert not result.ok and "not JSON" in result.note, result.note
    stored = await _documents(db, HEAT)
    assert [d["ok"] for d in stored] == [False], "a body nothing can parse is not a document"
    assert stored[0]["etag"] is None, (
        "a portal's validator filed as good conditions every later request for that url"
    )


async def test_a_trakt_comment_page_is_a_json_array_and_is_still_a_good_document(
    db, raw_root, keyed
):
    """The one JSON shape in §8 stage 2 that is not an object, and the whole of the exception.

    `capture`'s default requires an object because every other adapter reads the answer with
    `.get(...)`, and a top-level array reaching those lines is an `AttributeError` out of a source
    decision 334 says may never raise. Trakt's comment pages are arrays, so that kind passes
    `json_body` instead - and a guard that filed every good page of every sort as not-a-document
    would cost §8 stage 4 two thirds of its rating-stratified review text.
    [M5.3 review cycle 1, M53-C1-NET-02]
    """
    await _title(db, HEAT)
    site = _Site({("api.trakt.tv", f"/movies/{IMDB}/comments/{sort}"):
                  _route(b'[{"id": 1, "comment": "a sentence about the film"}]')
                  for sort in ("likes", "lowest", "highest")})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await trakt.comments(_ctx(db, fetcher, HEAT))

    assert result.ok, result.note
    stored = await _documents(db, HEAT)
    assert stored and all(d["ok"] for d in stored), [dict(d) for d in stored]


async def test_the_required_source_with_no_tmdb_id_to_ask_about_reports_that_it_never_ran(
    db, raw_root, keyed
):
    """Decision 334's word RAN, as the flag `stages.enrich` reads it off.

    `detail` has three arms that answer before any request is built, and the reachable one is
    this: `_find` writes no `tmdb_id` both for a film TMDB has no record of and for the kind
    disagreement this module refuses to act on, and no other adapter writes that column. Without
    the flag those arms arrived at the driver as the same value a TMDB 500 does, and the title
    parked at stage 2 under a reason naming a connector that was working - re-crawling eight
    hosts once a day, for ever. [M5.3 review cycle 1, M53-334-01]

    THE NOTE STATES THE EMPTY COLUMN AND NOT A CAUSE. It read "tmdb:resolve found none", which is
    false when that kind was refused a key or got a 500 - the column is equally empty then - and
    `tmdb:resolve`'s own note beside it is what says which. [M5.3 review cycle 2, m53-c2-334-01]
    """
    await _title(db, HEAT)
    site = _Site({})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and not result.ran
    assert result.note == "no tmdb id on the title, so TMDB could not be asked"
    assert site.seen == [], "an arm that answers without asking costs no request at all"


async def test_a_two_hundred_carrying_no_bytes_at_all_is_a_note_and_never_a_raise(
    db, raw_root, keyed
):
    """The one HTTP answer a bot wall gives most often, and the one that could still raise.

    `rawstore.store` refuses to write an empty document as good, by raising - "an empty gzip
    stream decompresses to b'' with no error, so a derive would read it as the document". Right
    for the store and wrong as an adapter's exit: decision 334 makes a source that did not answer
    a note under its own name, and `acquire/stages.py:20-29` says what becomes of an ordinary
    outcome raised as an exception. Asserted on a JSON source, which passes no `min_bytes` at
    all, because that is where the floor had to be unconditional rather than opt-in.
    """
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(b"")})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "empty body (HTTP 200, 0b)"
    assert [d["ok"] for d in await _documents(db, HEAT)] == [False]


async def test_an_absent_credential_is_a_note_and_issues_no_request_at_all(db, raw_root):
    """Decision 377, and the "no request" half is the one that matters.

    An install that configured TMDB and not OMDb is §3.1's legal half-configured boot. A source
    that asked anyway would spend a request on every title in the library to be told it has no
    key, on a host whose free tier is a thousand calls a day.
    """
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "no OMDb credential configured"
    assert site.seen == [], "not even a robots.txt read"
    assert await _documents(db, HEAT) == []


async def test_omdb_saying_no_inside_a_200_is_stored_as_a_document_that_is_not_good(db, raw_root, keyed):
    """OMDb answers `{"Response": "False"}` with HTTP 200, so the transport cannot tell.

    The bytes are stored either way - they are the honest record of what the host said - and the
    `ok` flag is chosen by a verdict that reads one field. `rawstore.latest` filters on `ok`, so
    a derive never parses `{"Response": "False"}` as this title's OMDb document.
    """
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_not_found.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "OMDb: Incorrect IMDb ID."
    stored = (await _documents(db, HEAT))[0]
    assert stored["ok"] is False and stored["http_status"] == 200
    assert stored["byte_size"] > 0, "the bytes are the record of what OMDb said"
    assert await rawstore.latest(db, "omdb", "detail", f"title:{HEAT}") is None


async def test_a_spent_omdb_key_reads_differently_from_a_title_omdb_has_never_held(db, raw_root, keyed):
    """`mdc/sources/omdb.py:43-48` separates these two because they call for different actions:
    one is a key to top up and the other is a fact about this title. The corpus answers the first
    by pausing the host, which is the circuit breaker's state and not an adapter's to fabricate -
    so it is a distinguished note here, and the module's port verdict says what would be needed
    to do better."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_quota.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert result.note == "OMDb refused the key rather than the title: Request limit reached!"
    assert "refused the key rather than the title" in result.note


async def test_a_wikipedia_article_that_is_not_there_is_a_note_and_not_a_raise(db, raw_root):
    """The action API says `missing: true` inside a 200, which is the same shape as OMDb's."""
    await _title(db, HEAT, wikipedia_title="Heat (1995 film)")
    site = _Site({("en.wikipedia.org", "/w/api.php"):
                  _route(_fixture("wikipedia_missing.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    assert not result.ok
    assert result.note == "Heat (1995 film): wikipedia holds no such article"
    assert [d["ok"] for d in await _documents(db, HEAT)] == [False]


# --- the two scraped sources --------------------------------------------------------------------


@pytest.fixture
def accepts_every_page(monkeypatch):
    """`page_belongs_to_title` says yes. The predicate is `derive/parse.py`'s; see the header."""
    monkeypatch.setattr(_ids, "belongs_to_title", lambda *a, **k: True)


@pytest.fixture
def refuses_every_page(monkeypatch):
    """`page_belongs_to_title` says no - the guessed slug landed on a different film."""
    monkeypatch.setattr(_ids, "belongs_to_title", lambda *a, **k: False)


async def test_a_supplied_rotten_tomatoes_slug_is_fetched_once_and_never_verified(
    db, raw_root, refuses_every_page
):
    """"A slug Wikidata supplied is an IDENTIFIER and is taken as given" (`metacritic.py:57-58`).

    Asserted with the identity check wired to REFUSE, because that is the only way to prove the
    check was not consulted: a supplied slug that still had to pass it would make
    `wikidata:resolve`'s answer worth nothing.
    """
    await _title(db, HEAT, rt_slug="m/heat")
    site = _Site({("www.rottentomatoes.com", "/m/heat"):
                  _route(_fixture("rt_page.html"), content_type=HTML_ROUTE)})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert [r.url.path for r in site.fetched] == ["/m/heat"], "one candidate, not two"
    assert result.ok and result.note == "path=m/heat"


async def test_a_guessed_rotten_tomatoes_page_for_a_different_film_writes_no_slug(
    db, raw_root, refuses_every_page
):
    """The refusal's adapter half: no slug, no second chance, and the bytes still in the store.

    "The bytes stay in the store either way - it is append-only, and a page that turned out to be
    another film is still the honest record of what the guess returned. What it does not get is
    the slug" (`mdc/sources/metacritic.py:87-90`). Without this, a wrong page's reviews become
    this title's pack and §8 stage 7 verifies every quote in it, because the quotes really are
    there.
    """
    await _title(db, HEAT)
    site = _Site({
        ("www.rottentomatoes.com", "/m/heat_1995"):
            _route(_fixture("rt_page.html"), content_type=HTML_ROUTE),
        ("www.rottentomatoes.com", "/m/heat"):
            _route(_fixture("rt_page.html"), content_type=HTML_ROUTE),
    })
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert not result.ok
    assert "a different title of the same name" in result.note
    assert await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT) is None
    stored = await _documents(db, HEAT)
    assert len(stored) == 2 and all(d["ok"] for d in stored), (
        "the bytes are the honest record of what the guess returned"
    )


async def test_a_guessed_metacritic_candidate_answering_304_is_still_not_this_titles_page(
    db, raw_root, refuses_every_page
):
    """A conditional answer is not the identity check, and on a GUESS it is the sign of a refusal.

    `resolve_path` returns above the candidate loop whenever the row carries a slug, and the only
    writer of that column is the accept arm at the foot of the loop - so a guessed candidate can
    only ever reach a 304 because a PREVIOUS drain fetched it and refused it. `capture` files a
    refused page `ok = true` with its validators on purpose, and `fetch._validators` keys on the
    url alone, so the refusal is exactly what arms the conditional request. The branch used to
    return that path as proved, on a comment asserting the negation of its own precondition -
    and `metacritic:reviews` then resolved the same way and put two more requests on a 0.7 rps
    host to file another film's critic and user pages under this title's entity key.

    THE REVIEW FETCH IS THE HALF WORTH ASSERTING, because it is the one that reaches a pack.
    [M5.3 review cycle 1, m53-c1-slug-01, M53-C1-NET-01]
    """
    await _title(db, HEAT)
    page = _route(_fixture("metacritic_page.html"), content_type=HTML_ROUTE, etag='W/"mc-1"')
    paths = ("/movie/heat-1995/", "/movie/heat/")
    site = _Site({("www.metacritic.com", path): page for path in paths})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        first = await metacritic.page(_ctx(db, fetcher, HEAT))
        assert not first.ok and "a different film of the same name" in first.note
        for path in paths:
            site.routes[("www.metacritic.com", path)] = _route(b"", status=304)
        second = await metacritic.page(_ctx(db, fetcher, HEAT))
        reviews = await metacritic.reviews(_ctx(db, fetcher, HEAT))

    assert site.fetched[2].headers["if-none-match"] == 'W/"mc-1"', (
        "the refused page is stored ok with its validators, so the next request is conditional"
    )
    assert not second.ok, second.note
    assert "unchanged since a fetch that was never accepted" in second.note
    assert await db.fetchval("SELECT metacritic_slug FROM title WHERE id = $1", HEAT) is None
    assert not reviews.ok, reviews.note
    assert not [r for r in site.fetched if "reviews" in r.url.path], (
        "a path this app never accepted must not cost two more requests on a 0.7 rps host"
    )


async def test_a_guessed_rotten_tomatoes_candidate_answering_304_is_still_refused(
    db, raw_root, refuses_every_page
):
    """The same branch on the other scraped source, where the loop also runs for a SUPPLIED slug.

    Rotten Tomatoes computes `guessed` and enters the loop either way, so its conditional arm has
    to split: a supplied slug is an identifier `belongs_to_title` is not run against on the fresh
    path either, and a guess is a guess. Only the guess is corrected here, and the test below
    holds the other half so the correction cannot quietly become a refusal of Wikidata's answer.
    [M5.3 review cycle 1, m53-c1-slug-01, M53-C1-NET-01]
    """
    await _title(db, HEAT)
    page = _route(_fixture("rt_page.html"), content_type=HTML_ROUTE, etag='W/"rt-1"')
    paths = ("/m/heat_1995", "/m/heat")
    site = _Site({("www.rottentomatoes.com", path): page for path in paths})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        first = await rottentomatoes.page(_ctx(db, fetcher, HEAT))
        assert not first.ok and "a different title of the same name" in first.note
        for path in paths:
            site.routes[("www.rottentomatoes.com", path)] = _route(b"", status=304)
        second = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert not second.ok, second.note
    assert "unchanged since a fetch that was never accepted" in second.note
    assert await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT) is None
    assert [r.url.path for r in site.fetched] == list(paths) * 2, (
        "a 304 on the first candidate must not abandon the remaining ones"
    )


async def test_a_supplied_rotten_tomatoes_slug_answering_304_is_still_this_titles_page(
    db, raw_root, refuses_every_page
):
    """The other half of the split above: Wikidata's answer keeps costing one request and no proof.

    Asserted with the identity check wired to REFUSE, because that is the only way to show the
    conditional arm did not start consulting it for a slug §8 stage 2 takes as given.
    [M5.3 review cycle 1, m53-c1-slug-01]
    """
    await _title(db, HEAT, rt_slug="m/heat")
    site = _Site({("www.rottentomatoes.com", "/m/heat"):
                  _route(_fixture("rt_page.html"), content_type=HTML_ROUTE, etag='W/"rt-1"')})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await rottentomatoes.page(_ctx(db, fetcher, HEAT))
        site.routes[("www.rottentomatoes.com", "/m/heat")] = _route(b"", status=304)
        second = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert second.ok and second.note == "path=m/heat: unchanged since the last fetch"


async def test_a_guessed_rotten_tomatoes_page_that_is_this_film_writes_the_canonical_slug(
    db, raw_root, accepts_every_page
):
    """`/m/the_matrix` 302s to `/m/matrix`, and the canonical form is the only one RT's own
    sub-paths accept - so the slug comes off the LAST hop while the bytes stay filed under the
    url the request was made against. That is the one place in the package where
    `response.url` is the right string, and both halves are asserted here."""
    await _title(db, HEAT, name="The Matrix", year=1999)
    site = _Site({
        ("www.rottentomatoes.com", "/m/the_matrix_1999"):
            (302, b"", {"location": "https://www.rottentomatoes.com/m/matrix"}),
        ("www.rottentomatoes.com", "/m/matrix"):
            _route(_fixture("rt_page.html"), content_type=HTML_ROUTE),
    })
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert result.ok and result.note == "path=m/matrix (from m/the_matrix_1999)"
    assert await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT) == "m/matrix"
    stored = (await _documents(db, HEAT))[0]
    assert stored["url"] == "https://www.rottentomatoes.com/m/the_matrix_1999"


async def test_a_scraped_stub_body_is_a_soft_block_and_is_not_stored_as_good(
    db, raw_root, accepts_every_page
):
    """"A 2xx with an empty (or stub) body is a soft block, not a success" - recording it as ok
    would hide the wall from §6.6's board, let the derive parse nothing, and stop the host's
    breaker tripping, "so the crawl would burn thousands of requests on a wall"
    (`mdc/sources/_htmlutil.py:74-79`). `MIN_HTML_BYTES` is 512 and the stub here is 80."""
    await _title(db, HEAT)
    stub = _route(_fixture("scraped_stub.html"), content_type=HTML_ROUTE)
    site = _Site({("www.rottentomatoes.com", "/m/heat_1995"): stub,
                  ("www.rottentomatoes.com", "/m/heat"): stub})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert _views.MIN_HTML_BYTES == 512
    assert len(_fixture("scraped_stub.html")) < _views.MIN_HTML_BYTES
    assert not result.ok and "empty body (HTTP 200," in result.note
    assert [d["ok"] for d in await _documents(db, HEAT)] == [False, False]
    # A stub is not a page, so it does not get to name this title's slug either.
    assert await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT) is None


async def test_a_metacritic_guess_another_title_already_holds_is_refused_unfetched(db, raw_root):
    """""The Beekeeper" (1986) and (2024) both produce `movie/the-beekeeper`, and whichever page
    exists would hand its reviews to both. If another title already holds this slug, refuse to
    guess" (`mdc/sources/metacritic.py:31-40`).

    Refused before any request, which is the point: the page would look perfectly valid and the
    identity check would have to catch it on a cast list Metacritic may not publish.
    """
    await _title(db, OTHER, name="The Beekeeper", year=1986,
                 imdb_id="tt0091083", metacritic_slug="movie/the-beekeeper")
    await _title(db, HEAT, name="The Beekeeper", year=2024, imdb_id="tt15314262")
    site = _Site({})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await metacritic.page(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "no slug candidate"
    assert site.seen == []
    assert await db.fetchval("SELECT metacritic_slug FROM title WHERE id = $1", HEAT) is None


async def test_metacritic_reviews_fetches_the_two_server_rendered_views_only(
    db, raw_root, accepts_every_page
):
    """Two views and not four. "Verified against the live site: `?filter=Positive|Negative` and
    `?page=N` both return a shell whose review list is hydrated client-side (0 server-rendered
    reviews), so they are pure wasted requests" (`mdc/sources/metacritic.py:137-141`). At
    seven-tenths of a request a second, a third view costs another second and a half per title
    across the library and returns nothing."""
    await _title(db, HEAT, metacritic_slug="movie/heat")
    body = _fixture("metacritic_page.html")
    site = _Site({
        ("www.metacritic.com", "/movie/heat/critic-reviews/"):
            _route(body, content_type=HTML_ROUTE),
        ("www.metacritic.com", "/movie/heat/user-reviews/"):
            _route(body, content_type=HTML_ROUTE),
    })
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await metacritic.reviews(_ctx(db, fetcher, HEAT))

    assert sorted(r.url.path for r in site.fetched) == [
        "/movie/heat/critic-reviews/", "/movie/heat/user-reviews/"
    ]
    assert result.ok and result.note == "2/2 views"
    assert {d["kind"] for d in await _documents(db, HEAT)} == {
        "reviews:critics", "reviews:users"
    }


async def test_one_review_view_failing_still_keeps_the_other(db, raw_root, accepts_every_page):
    """"Independently" is the corpus's own word (`_htmlutil.py:41`): either page can 404, and a
    title with one of the two is worth more than a title with neither."""
    await _title(db, HEAT, metacritic_slug="movie/heat")
    site = _Site({("www.metacritic.com", "/movie/heat/critic-reviews/"):
                  _route(_fixture("metacritic_page.html"), content_type=HTML_ROUTE)})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await metacritic.reviews(_ctx(db, fetcher, HEAT))

    assert result.ok and result.note.startswith("1/2 views; users: HTTP 404")
    kinds = {d["kind"]: d["ok"] for d in await _documents(db, HEAT)}
    assert kinds == {"reviews:critics": True, "reviews:users": False}


async def _credit(conn, title_id: int, person_id: int, name: str, *,
                  department: str = "Acting", job: str = "Actor",
                  role_class: str = "cast") -> None:
    """One credit, written the way every writer in this tree writes one.

    `role_class` IS SET, AND UNTIL M5.3'S FIRST REVIEW CYCLE THIS HELPER LEFT IT NULL. Every
    `rows.emit("credit", ...)` in `derive/parse.py` passes it, `derive/ids.keep_credit` drops a
    credit without one, `importer/load.py:235-238` maps the bundle's column, and
    `placement/features.py` records that all 281,655 shipped bundle credit rows carry one - so a
    row without it is a shape neither the importer nor the derive can produce, and a fixture built
    out of one measures a query no production row would match.
    [M5.3 review cycle 1, m53-c1-slug-03]
    """
    await conn.execute(
        "INSERT INTO person (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        person_id, name)
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, source, role_class)"
        " VALUES ($1, $2, $3, $4, 'tmdb', $5)",
        title_id, person_id, department, job, role_class)


@pytest.mark.parametrize(
    ("cast", "accepted"),
    [("Al Pacino", True), ("Someone Else Entirely", False)],
)
async def test_the_borrowed_identity_check_really_decides_a_guessed_page(
    db, raw_root, cast, accepted
):
    """The seam, wired end to end: `_ids.known_people` into `derive/parse`'s predicate.

    Every other scraped-source test here substitutes the predicate, because the predicate is §8
    stage 3's and is tested where it lives. This one does not, and that is the point: a shared
    cast is the stronger of the two signals `page_belongs_to_title` weighs, and this and the
    co-director test below are the two that prove the set this package hands it is a set it can
    read.

    ONLY THE CAST VARIES, AND FOR ONE REVIEW CYCLE BOTH THE CAST AND THE YEAR DID. The rows were
    1995 against the page's 1995 and 2024 against it, so the year decided each on its own, and
    the fixture's cast was invisible besides: it said `"actors"`, which is not what Rotten
    Tomatoes serves - every real capture under `fixtures/sources/` says `"actor"` - and not a key
    `page_people` reads. `_ids.known_people` answering the empty set left both rows green,
    measured. The title's year is now 1970 on both rows, outside `PAGE_YEAR_TOLERANCE` of the
    page's 1995, so the page is accepted only through a name stage 2's set shares with it; and a
    Rotten Tomatoes cast cannot refuse on its own (`people_decide=False`), so the stranger's row is
    the year refusing once the cast has declined to vouch. [M5.3 review cycle 2, m53-c2-slug-03]

    THE SET IS `derive/ids.known_people`'S AND NOT A RECONSTRUCTION OF IT. This docstring used to
    say `known_people` rebuilt the corpus's `role_class IN ('director','cast')` out of `department`
    and `job`, "which is the named change `importer/load.py:236` forced by mapping three of the
    four columns across". That mapping maps four, `role_class` among them, and the reconstruction
    was a second implementation of one refusal - see the test below this one for what the two
    disagreed about. [M5.3 review cycle 1, m53-c1-slug-03]
    """
    await _title(db, HEAT, name="Heat", year=1970)
    await _credit(db, HEAT, 1_000_000_911, cast)
    site = _Site({("www.rottentomatoes.com", "/m/heat_1970"):
                  _route(_fixture("rt_page.html"), content_type=HTML_ROUTE),
                  ("www.rottentomatoes.com", "/m/heat"):
                  _route(_fixture("rt_page.html"), content_type=HTML_ROUTE)})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert result.ok is accepted, result.note
    slug = await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT)
    assert (slug is not None) is accepted
    if not accepted:
        assert "a different title of the same name" in result.note


async def test_a_co_director_vouches_for_a_page_at_stage_two_as_it_does_at_stage_three(
    db, raw_root
):
    """One refusal, one implementation of "is this the same human". `derive/ids.py`'s change note 4.

    THE TITLE'S YEAR IS DELIBERATELY WRONG so that only the credit can vouch: the fixture page is
    dated 1995 and the row says 1970, which is well outside `PAGE_YEAR_TOLERANCE`. So the page is
    accepted if and only if stage 2's set reaches the director it names. `sources/_ids.known_people`
    used to filter `job = 'Director' OR department IN ('Acting','Actor')`, and
    `derive/ids.known_people` filters `role_class IN ('director','cast')`; `classify_role` maps
    "Co-Director" and "Series Director" onto `role_class = 'director'` with a job neither literal
    matches, so stage 2's set was strictly narrower than stage 3's. This page names our
    co-director and no billed cast: stage 3 accepts it and stage 2 refused it, which is exactly
    the state change note 4 names - "a page starts being refused for one source and accepted for
    the other" - and it costs the title both scraped sources plus, through decision 335's source
    count, a thirty-day park at the reviews gate. [M5.3 review cycle 1, m53-c1-slug-03]
    """
    await _title(db, HEAT, year=1970)
    await _credit(db, HEAT, 1_000_000_912, "Michael Mann",
                  department="Directing", job="Co-Director", role_class="director")
    page = _route(_fixture("rt_page.html"), content_type=HTML_ROUTE)
    site = _Site({("www.rottentomatoes.com", "/m/heat_1970"): page,
                  ("www.rottentomatoes.com", "/m/heat"): page})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    assert await _ids.known_people(db, HEAT) == {"michaelmann"}
    assert result.ok, result.note
    assert await db.fetchval("SELECT rt_slug FROM title WHERE id = $1", HEAT) == "m/heat_1970"


@pytest.mark.parametrize("fetched", [True, False], ids=["tmdb-in-the-store", "no-tmdb-document"])
async def test_a_restoration_page_is_judged_at_stage_two_with_the_cast_tmdb_already_sent(
    db, raw_root, fetched
):
    """The Black Orpheus page, at stage 2, on a first acquisition - the only acquisition there is.

    `known_people` read `credit` alone, and on a title stage 1 has just minted `credit` is empty:
    stage 3 is what writes it. So the identity check fell through to `abs(page_year - year) <= 2`,
    and the real Metacritic page for this 1959 film is the 2006 Criterion restoration - refused,
    with `metacritic:reviews` resolving the same path and asking for no review at all. The helper
    called that "the ordinary case rather than a defect" because the set would be full "on a
    re-run"; stage 2 does not re-run for a title past it, so the empty set was the only case. And
    stage 3 then ACCEPTED the same bytes, because `rebuild._refuses` unions this run's parsed TMDB
    cast in - two stages disagreeing about one page, in the direction that costs the review text.

    `tmdb:detail` runs at priority 20 and the scraped sources at 76 and 77, so the cast is already
    in the raw store when they ask. Without that document the check is what it always was, and the
    page is still refused: the second case is the control. [M5.3 review cycle 2, m53-c2-slug-01]
    """
    await _title(db, OTHER, name="Black Orpheus", year=1959, imdb_id="tt0053146")
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ('acquire', $1, $2)",
        f"title:{OTHER}", {"title_id": OTHER},
    )
    if fetched:
        detail = {
            "id": 5_000_107, "title": "Black Orpheus", "release_date": "1959-06-12",
            "credits": {
                "cast": [{"id": 1, "name": "Breno Mello", "character": "Orfeu", "order": 0}],
                "crew": [{"id": 2, "name": "Marcel Camus", "job": "Director",
                          "department": "Directing"}],
            },
        }
        await rawstore.store(
            db, source="tmdb", kind="movie_detail", url="https://api.themoviedb.org/3/movie/1",
            content=json.dumps(detail).encode("utf-8"), entity_key=f"title:{OTHER}",
            content_type="application/json",
        )
    # The derive's own capture of the real page, which is where the corpus's acceptance of it is kept.
    capture = FIXTURES.parent / "sources" / "metacritic_page_black_orpheus_2006.html"
    page = _route(capture.read_bytes(), content_type=HTML_ROUTE)
    site = _Site({("www.metacritic.com", "/movie/black-orpheus-1959/"): page,
                  ("www.metacritic.com", "/movie/black-orpheus/"): page})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await metacritic.page(_ctx(db, fetcher, OTHER))

    slug = await db.fetchval("SELECT metacritic_slug FROM title WHERE id = $1", OTHER)
    if fetched:
        assert result.ok, result.note
        assert slug == "movie/black-orpheus-1959"
        assert await _ids.known_people(db, OTHER) >= {"brenomello", "marcelcamus"}
    else:
        assert not result.ok and "a different film of the same name" in result.note
        assert slug is None


async def test_the_two_scraped_hosts_are_paced_at_the_rate_their_policy_declares(
    db, raw_root, accepts_every_page
):
    """Politeness asserted as arithmetic, which is what decision 340 exists for.

    Two candidates against Rotten Tomatoes is two requests at `rps=0.7, burst=1`, so the second
    one waits about one and three-sevenths of a second - and `robots.txt` is a third request on
    this host, because `respect_robots` is True for it and for Metacritic and False for the six
    API hosts. The clock is injected, so this runs in microseconds and asserts the pause exactly.
    Making the test tolerant is the repair here; making the policy faster is not.
    """
    await _title(db, HEAT)
    site = _Site({("www.rottentomatoes.com", "/m/heat"):
                  _route(_fixture("rt_page.html"), content_type=HTML_ROUTE)})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        await rottentomatoes.page(_ctx(db, fetcher, HEAT))

    policy = hosts.HOST_POLICIES["www.rottentomatoes.com"]
    assert [r.url.path for r in site.seen][0] == "/robots.txt", (
        "this host is one of the two crawled with respect_robots=True"
    )
    # Three requests against a bucket of one token: the robots read takes the burst and each
    # later request pays the full interval. The number is DERIVED from the declared policy, so a
    # test that ran slowly could never be repaired by editing the policy - the assertion would
    # simply follow it. `test_acquire_fetch.py` holds the policy itself to its four numbers.
    assert len(clock.slept) == len(site.seen) - policy.burst
    assert max(clock.slept) == pytest.approx(1 / policy.rps, rel=1e-6)
