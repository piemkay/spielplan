"""No network: requests go to an `httpx.MockTransport`, and the clock and sleeper are injected so
scraped hosts are asserted at their real policy. `page_belongs_to_title` is substituted in the
scraped-source tests; it is tested where it lives, in `derive/parse.py`."""

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

# §4.1's partition: app writes start at 1e9. Ids are written out so no stage-1 walk is needed.
HEAT = 1_000_000_901
THRONES = 1_000_000_902
OTHER = 1_000_000_903

IMDB = "tt0113277"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Clock:
    """Moves only when something sleeps on it: the sleeper and the bucket's refill must share one clock."""

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
    """Jitter pinned low, so a wait is a number and not a distribution."""
    return lo


class _Site:
    """Serves `robots.txt` permissively: two hosts are crawled with `respect_robots=True`."""

    ROBOTS = b"User-agent: *\nAllow: /\n"

    def __init__(self, routes) -> None:
        # A route is a `(status, body, headers)` triple or a callable, so one endpoint can answer two
        # questions (Wikipedia's `/w/api.php`).
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
                      max_attempts=5, priority=50)


def _ctx(conn, fetcher: fetch.Fetcher, title_id: int, *, key: str = "") -> stages.StageContext:
    """Set on the instance: `StageContext` is a plain dataclass, and `ctx.fetcher` is the contract."""
    ctx = stages.StageContext(conn=conn, task=_task(key or f"title:{title_id}"),
                              title_id=title_id, run_id=None)
    ctx.fetcher = fetcher
    return ctx


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """`DATA_DIR`, not a parameter: `rawstore` takes no root argument, so it cannot be passed wrong."""
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
    """Takes `secrets_key`: two keys are sealed; Trakt's client id is in the config blob."""
    await _configure(db, "tmdb", {}, {"api_key": "tmdb-test-key"})
    await _configure(db, "omdb", {}, {"api_key": "omdb-test-key"})
    await _configure(db, "trakt", {"client_id": "trakt-test-client"}, None)
    return db


def test_stage_two_asks_the_eight_sources_section_eight_names_in_its_order():
    """Letterboxd is not crawled (decision 374); `wikidata:resolve` precedes the scrapers it feeds."""
    order = stages.enrich_sources()
    assert [kind for kind, *_ in order] == [
        "tmdb:resolve", "tmdb:detail", "wikidata:resolve", "omdb:detail",
        "trakt:summary", "trakt:comments", "wikipedia:article", "tvmaze:show",
        "rt:page", "metacritic:page", "metacritic:reviews",
    ]
    assert {source for _kind, source, *_ in order} == {
        "tmdb", "wikidata", "omdb", "trakt", "wikipedia", "tvmaze", "rottentomatoes", "metacritic",
    }


def _row(**columns) -> dict:
    base_row = {"id": HEAT, "kind": "movie", "name": "Heat", "original_name": None,
                "year": 1995, "rt_slug": None, "metacritic_slug": None}
    return {**base_row, **columns}


def test_a_rotten_tomatoes_slug_wikidata_supplied_is_the_only_candidate():
    """Wikidata stores these with the type prefix, so both spellings normalise to one path."""
    assert rottentomatoes.candidate_paths(_row(rt_slug="m/heat")) == ["m/heat"]
    assert rottentomatoes.candidate_paths(_row(rt_slug="heat")) == ["m/heat"]
    assert rottentomatoes.candidate_paths(_row(kind="series", rt_slug="heat")) == ["tv/heat"]


def test_a_guessed_rotten_tomatoes_slug_tries_the_year_qualified_form_first():
    """The qualified slug is the only one of an RT pair that can be this title, so it is tried first."""
    assert rottentomatoes.candidate_paths(_row()) == ["m/heat_1995", "m/heat"]
    assert rottentomatoes.candidate_paths(_row(year=None)) == ["m/heat"]
    assert rottentomatoes.candidate_paths(
        _row(name="The Hitch-Hiker", year=1953)
    ) == ["m/the_hitch_hiker_1953", "m/the_hitch_hiker"]


def test_a_title_with_no_name_at_all_produces_no_candidate_rather_than_untitled():
    """`_ids.slugify("")` answers "untitled", which RT may serve."""
    assert rottentomatoes.candidate_paths(_row(name=None, original_name=None)) == []
    assert metacritic.slug_candidates(_row(name=None, original_name=None)) == []


def test_a_guessed_metacritic_slug_tries_the_year_qualified_form_first():
    """Metacritic disambiguates with `-year`; both Beekeepers share `movie/the-beekeeper`."""
    assert metacritic.slug_candidates(_row()) == ["movie/heat-1995", "movie/heat"]
    assert metacritic.slug_candidates(_row(year=None)) == ["movie/heat"]
    assert metacritic.slug_candidates(_row(metacritic_slug="movie/heat")) == ["movie/heat"]
    assert metacritic.slug_candidates(_row(metacritic_slug="heat")) == ["movie/heat"]
    assert metacritic.slug_candidates(
        _row(kind="series", metacritic_slug="heartland")) == ["tv/heartland"]


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
    """The article's reception section becomes the pack, and a quote from the wrong film verifies."""
    assert wikipedia._title_overlaps(article, title) is overlaps


def test_a_disambiguated_hit_whose_year_contradicts_the_title_is_refused():
    """One year of tolerance: festival premieres shift the year by one often enough."""
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 2015) is None
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 1976) == "Obsession (1976 film)"
    assert wikipedia._pick(["Obsession (1976 film)"], "Obsession", 1977) == "Obsession (1976 film)"
    # Undisambiguated - no contradicting evidence, so it is taken.
    assert wikipedia._pick(["Obsession"], "Obsession", 2015) == "Obsession"
    # The first hit is refused on the words and the second is taken: order is not the rule.
    assert wikipedia._pick(
        ["Heat (magazine)", "Heat (1995 film)"], "Heat", 1995) == "Heat (1995 film)"


@pytest.mark.parametrize(
    "column", ["overview", "kind", "name", "year", "poster_path", "placement", "is_owned"]
)
async def test_the_identity_write_refuses_a_column_outside_the_identity_set(column):
    """Raised before any connection (`None` is passed): a silently dropped argument would LOOK written.
    `kind` matters most: it is §4.1 rule 5's partition."""
    with pytest.raises(ValueError, match="may not write"):
        await _ids.set_ids(None, HEAT, **{column: "x"})


# Decision 372's eight plus `letterboxd_slug` and `trakt_id`. Written out, not imported.
_DECISION_372_COLUMNS = frozenset({
    "imdb_id", "tmdb_id", "tvdb_id", "trakt_id", "trakt_slug", "letterboxd_slug",
    "rt_slug", "metacritic_slug", "wikidata_id", "wikipedia_title",
})


def test_the_identity_set_is_section_eight_stage_two_s_and_holds_no_surface_field():
    """`jellyfin_id` is §7.1's (`connectors/resolve.py`): an adapter writing it could repoint a row."""
    assert _ids.ID_COLUMNS == _DECISION_372_COLUMNS
    assert "kind" not in _ids.ID_COLUMNS
    assert "jellyfin_id" not in _ids.ID_COLUMNS


_ADAPTERS = ("tmdb", "wikidata", "omdb", "trakt", "wikipedia", "tvmaze",
             "rottentomatoes", "metacritic")
_PACKAGE = Path(base.__file__).resolve().parent

# The same list `test_sources_base.py` uses for `credentials.py`.
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
    """Docstrings excluded: these modules quote the statement they refuse."""
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
    """`_ids.set_ids` is the only statement touching `title`; the corpus's own UPDATE is what is refused."""
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
    # Both detectors fire, and the second ignores docstrings that quote the statement.
    illegal = "async def f(conn):\n    await conn.execute('UPDATE title SET overview=$1')\n"
    assert _db_calls(illegal) == ["execute"]
    assert _sql_literals(illegal) == ["UPDATE title SET overview=$1"]
    assert _sql_literals('"""Never UPDATE title SET overview."""\nx = 1\n') == []


# Every way to open a socket without the app's transport. `urllib.request`, not bare `urllib`:
# two adapters import `urllib.parse`.
_TRANSPORT = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "socket", "aiohttp")

# `_views` is the door. `omdb` and `wikipedia` name it for its `Response` type only.
_FETCHER_MODULE = "spielplan.acquire.fetch"
_MAY_NAME_THE_FETCHER = {"_views.py", "omdb.py", "wikipedia.py"}

# Named, so a walk that stopped finding them is not a green guard over nothing.
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
    """AST, not a substring: every module argues about the fetcher in prose."""
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
    """Decision 340: one door, `_views`, where `entity_key` and `request_url` are decided. The whole
    package, every transport spelling, and `.fetcher` named nowhere else (it needs no import)."""
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
    """The last two: the import-free `ctx.fetcher` bypass, and the relative import spelling."""
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
    """One refusal, one implementation. Read statically: the call is a deferred import."""
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
    # Decision 372: everything a card renders is stage 3's.
    assert (row["name"], row["year"], row["overview"]) == ("Heat", 1995, None)


async def test_tmdb_detail_asks_for_the_append_list_that_keeps_a_title_to_one_request(db, raw_root, keyed):
    """The exact list: a lost entry is a round trip per title, noticed nowhere else."""
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
    """§4.1 rule 6: external ids carry legitimate duplicates, so COALESCE(existing, new). The empty
    column beside it proves the write happened."""
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
    """TMDB's TV endpoints take different appends; a series down the movie path 404s."""
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
    """`kind` is §4.1 rule 5's partition and content seeds once (decision 162): noted, not flipped."""
    await _title(db, HEAT)
    site = _Site({("api.themoviedb.org", f"/3/find/{IMDB}"):
                  _route(_fixture("tmdb_find_other_kind.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await tmdb.resolve(_ctx(db, fetcher, HEAT))

    # `ok`: TMDB answered, so a refused key and a disagreement are distinguishable (decision 334).
    assert result.ok and result.ran
    assert "files tt0113277 as a series" in result.note
    assert "kind is not a stage 2 write" in result.note
    row = await db.fetchrow("SELECT kind, tmdb_id FROM title WHERE id = $1", HEAT)
    assert (row["kind"], row["tmdb_id"]) == ("movie", None)


async def test_a_title_tmdb_has_never_heard_of_is_an_answer_and_not_a_failure(db, raw_root, keyed):
    """A `find` that came back empty is stored and is `ok`; the note names the id asked about."""
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
    """A refused or failed call is not `ok`: that is decision 334's park condition."""
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
    """An empty binding list is a well-formed 200, so the note names the property that found nothing."""
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
    """Decision 374: one id per query, not the batch shape."""
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
    # P4947 is queried (verbatim port) but not written: `tmdb:resolve` owns that column.
    assert row["tmdb_id"] is None


async def test_omdb_asks_for_the_full_plot_and_the_tomatoes_block(db, raw_root, keyed):
    """`plot=full`: §8 stage 4's gate needs a paragraph, not a sentence (decision 335)."""
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
    """The client id travels in a header, so it is config, not sealed, and reads without the DEK."""
    await _title(db, HEAT)
    site = _Site({
        ("api.trakt.tv", f"/movies/{IMDB}"): _route(_fixture("trakt_summary.json")),
        ("api.trakt.tv", f"/movies/{IMDB}/ratings"): _route(_fixture("trakt_ratings.json")),
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
    # Separate answers, stored under their own `kind` as the corpus does.
    assert {d["kind"] for d in await _documents(db, HEAT)} == {"summary", "ratings"}


async def test_trakt_comments_keeps_the_three_sorts_apart_in_the_store(db, raw_root, keyed):
    """Page numbers keep the sorts apart: the store's newest-per-page view would collapse them."""
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
    """Three embeds in one call keep this source at two requests."""
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
    # The lookup answer is stored so "TVmaze lacks it" differs from "never asked".
    assert {d["kind"] for d in await _documents(db, THRONES)} == {"lookup", "show"}


async def test_tvmaze_is_not_applicable_to_a_movie_and_spends_no_request(db, raw_root):
    """A movie skipped here is not a gap, so it is `ok`."""
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
    """The search is the evidence for a permanent `set_ids` choice, so it is stored."""
    await _title(db, HEAT)
    site = _Site({("en.wikipedia.org", "/w/api.php"): _wikipedia_api})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    searches = [r for r in site.fetched if b"list=search" in r.url.query]
    assert len(searches) == 1
    query = parse_qs(searches[0].url.query.decode())
    assert query["srsearch"] == ["Heat 1995 film"] and query["srlimit"] == ["5"]
    # Written before the extract is fetched, so a failed extract does not search again.
    assert await db.fetchval(
        "SELECT wikipedia_title FROM title WHERE id = $1", HEAT) == "Heat (1995 film)"
    assert {d["kind"] for d in await _documents(db, HEAT)} == {"search", "article"}
    assert result.ok and result.note.endswith("(searched: Heat (1995 film))")


async def test_a_search_hit_that_names_another_year_leaves_the_title_with_no_article(
    db, raw_root
):
    """A wrong article cannot be un-written (decision 162); no article is the right answer."""
    await _title(db, HEAT, name="Obsession", year=2015, imdb_id="tt3703836")
    site = _Site({("en.wikipedia.org", "/w/api.php"):
                  _route(_fixture("wikipedia_search_wrong_year.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    assert not result.ok
    # The hit's words match, so the refusal is on the year alone.
    assert wikipedia._title_overlaps("Obsession (1976 film)", "Obsession")
    assert result.note == (
        "no wikipedia hit for 'Obsession 2015 film' is this film: refused Obsession (1976 film)"
    )
    assert await db.fetchval("SELECT wikipedia_title FROM title WHERE id = $1", HEAT) is None
    assert len(site.fetched) == 1, "the extract is not asked for when no hit is this film"


async def test_wikipedia_asks_for_the_plain_text_extract_of_the_article_it_holds(db, raw_root):
    """`explaintext` and `exsectionformat=wiki` make sections readable; `redirects=1` follows renames."""
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


async def test_every_document_is_filed_under_the_task_key_and_never_a_provider_id(db, raw_root, keyed):
    """The board joins `entity_key` to `acquisition_task.key` (decision 345). The task key is NOT the
    title id, so a key rebuilt from `ctx.title_id` fails."""
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
    """`Response.url` is the last hop; `request_url` is what `_validators` reads. OMDb shares one
    endpoint across titles, so the full url must key the validator."""
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
    """A 304 stores nothing and skips the identity write: the bytes held are still current."""
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


async def test_a_404_is_a_note_and_a_failure_row_rather_than_a_raise(db, raw_root, keyed):
    """Decision 334: only `tmdb:detail` can park stage 2. The failure row is fetch history for the board."""
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
    """A captive portal's 200 HTML must not be filed `ok` with its ETag: it would displace the last
    good document. The raise is caught upstream and is not the harm."""
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
    """Trakt's comment pages are arrays, so that kind passes `json_body`; all else must be an object."""
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
    """`detail` answers before any request when no `tmdb_id` exists; RAN distinguishes that from a 500.
    The note states the empty column, not a cause."""
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
    """`rawstore.store` raises on an empty body; the adapter must turn that into a note."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(b"")})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "empty body (HTTP 200, 0b)"
    assert [d["ok"] for d in await _documents(db, HEAT)] == [False]


async def test_an_absent_credential_is_a_note_and_issues_no_request_at_all(db, raw_root):
    """The "no request" half matters: OMDb's free tier is a thousand calls a day."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_detail.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "no OMDb credential configured"
    assert site.seen == [], "not even a robots.txt read"
    assert await _documents(db, HEAT) == []


async def test_omdb_saying_no_inside_a_200_is_stored_as_a_document_that_is_not_good(db, raw_root, keyed):
    """OMDb says no inside a 200; the bytes are stored, but `ok` is false."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_not_found.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert not result.ok and result.note == "OMDb: Incorrect IMDb ID."
    stored = (await _documents(db, HEAT))[0]
    assert stored["ok"] is False and stored["http_status"] == 200
    assert stored["byte_size"] > 0, "the bytes are the record of what OMDb said"
    assert not any(document["ok"] for document in await _documents(db, HEAT))


async def test_a_spent_omdb_key_reads_differently_from_a_title_omdb_has_never_held(db, raw_root, keyed):
    """A spent key and an unknown title call for different actions, so the notes differ."""
    await _title(db, HEAT)
    site = _Site({("www.omdbapi.com", "/"): _route(_fixture("omdb_quota.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await omdb.detail(_ctx(db, fetcher, HEAT))

    assert result.note == "OMDb refused the key rather than the title: Request limit reached!"
    assert "refused the key rather than the title" in result.note


async def test_a_wikipedia_article_that_is_not_there_is_a_note_and_not_a_raise(db, raw_root):
    """`missing: true` inside a 200, like OMDb's."""
    await _title(db, HEAT, wikipedia_title="Heat (1995 film)")
    site = _Site({("en.wikipedia.org", "/w/api.php"):
                  _route(_fixture("wikipedia_missing.json"))})
    clock = _Clock()
    async with _fetcher(site, clock, db) as fetcher:
        result = await wikipedia.article(_ctx(db, fetcher, HEAT))

    assert not result.ok
    assert result.note == "Heat (1995 film): wikipedia holds no such article"
    assert [d["ok"] for d in await _documents(db, HEAT)] == [False]


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
    """Wired to REFUSE: the only way to prove a supplied slug skips the identity check."""
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
    """No slug and no second chance, but the bytes stay: a wrong page's reviews would verify."""
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
    """On a guess, a 304 means a previous drain fetched and refused it; the review fetch is the half
    that reaches a pack."""
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
    """RT runs the loop for supplied slugs too, so only the guess is corrected here."""
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
    """Wired to REFUSE, to show the conditional arm does not consult it for a supplied slug."""
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
    """The slug comes off the LAST hop (RT's canonical form); the bytes stay under the request url."""
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
    """`MIN_HTML_BYTES` is 512 and the stub is 80: a stub filed ok would hide the wall."""
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
    """Refused before any request: the page would look valid."""
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
    """The filtered and paged views are hydrated client-side, so they are wasted requests."""
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
    """Either page can 404 independently; one of two beats neither."""
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
    """`role_class` is set: every production writer sets it, and `known_people` needs it."""
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
    """Only the cast varies: the year (1970) is outside `PAGE_YEAR_TOLERANCE`, so only a shared name
    accepts. The set is `derive/ids.known_people`'s."""
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
    """The year is deliberately wrong, so only the co-director credit can vouch; stage 2 and stage 3
    must agree on who counts."""
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
    """`tmdb:detail` runs first (priority 20), so its cast is in the raw store when the scraped sources
    ask. Without that document the page is still refused: the control."""
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
    """Two RT candidates at `rps=0.7, burst=1`, plus robots.txt. Make the test tolerant, never the
    policy faster."""
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
    # Derived from the declared policy, so a slow run cannot be fixed by editing it.
    assert len(clock.slept) == len(site.seen) - policy.burst
    assert max(clock.slept) == pytest.approx(1 / policy.rps, rel=1e-6)
