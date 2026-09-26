"""The ast helpers copy `test_layering_guards.py`'s but also record `from x import y` as `x.y`,
so `from spielplan.acquire import fetch` cannot slip past."""

from __future__ import annotations

import ast
import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from spielplan.core import secrets
from spielplan.derive.reviews import ParsedReview, review_row
from spielplan.importer.reviews import repair_mojibake
from spielplan.sources import _htmlutil, base, credentials


@pytest.fixture(autouse=True)
def _registry_is_restored():
    """Emptied before the test too: once the adapters are imported, a fake under a real kind would
    be refused. Restored after, for `test_sources_adapters.py`."""
    saved = dict(base.REGISTRY)
    base.REGISTRY.clear()
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(saved)


async def _noop(ctx):
    """A source that does nothing."""
    return base.SourceResult(source="x", kind="x:y", ok=True)


def test_the_registry_holds_what_the_driver_needs_to_decide_with():
    """`mdc/sources/base.py:42-54`'s eight fields, including `paid` (`:51-53`)."""
    @base.handler("tmdb:detail", source="tmdb", requires="tmdb", priority=20, phase="enrich")
    async def detail(ctx):
        """Metadata, credits and keywords.

        Second paragraph, which the description must not take.
        """
        return base.SourceResult(source="tmdb", kind="tmdb:detail", ok=True)

    spec = base.REGISTRY["tmdb:detail"]
    assert (spec.kind, spec.source, spec.requires) == ("tmdb:detail", "tmdb", "tmdb")
    assert (spec.default_priority, spec.phase, spec.paid) == (20, "enrich", False)
    assert spec.fn is detail
    # The corpus's fallback: the first line of the docstring, not the whole of it.
    assert spec.description == "Metadata, credits and keywords."


def test_an_explicit_description_wins_over_the_docstring():
    @base.handler("omdb:detail", source="omdb", requires="omdb",
                  description="RT + Metacritic + IMDb scores, awards, plot")
    async def detail(ctx):
        """Something else entirely."""
        return base.SourceResult(source="omdb", kind="omdb:detail", ok=True)

    assert base.REGISTRY["omdb:detail"].description == "RT + Metacritic + IMDb scores, awards, plot"


def test_a_second_handler_for_one_kind_is_refused():
    """The corpus overwrites silently; here the driver selects by kind, so an overwrite is unreportable."""
    @base.handler("rt:page", source="rt")
    async def first(ctx):
        """The real one."""
        return base.SourceResult(source="rt", kind="rt:page", ok=True)

    with pytest.raises(RuntimeError, match="two handlers claim the source kind 'rt:page'"):
        @base.handler("rt:page", source="rt")
        async def second(ctx):
            """The one that would have replaced it."""
            return base.SourceResult(source="rt", kind="rt:page", ok=True)

    assert base.REGISTRY["rt:page"].fn is first


def test_registering_the_same_handler_again_is_not_a_duplicate():
    """A module reload builds a new function with the same qualified name; `load_all` relies on it."""
    async def resolve(ctx):
        """Find the tmdb id."""
        return base.SourceResult(source="tmdb", kind="tmdb:resolve", ok=True)

    base.handler("tmdb:resolve", source="tmdb")(resolve)

    reloaded = type(resolve)(resolve.__code__, resolve.__globals__, resolve.__name__,
                             resolve.__defaults__, resolve.__closure__)
    reloaded.__qualname__ = resolve.__qualname__
    assert reloaded is not resolve

    base.handler("tmdb:resolve", source="tmdb")(reloaded)
    assert base.REGISTRY["tmdb:resolve"].fn is reloaded


def test_available_kinds_filters_by_phase():
    base.handler("tmdb:detail", source="tmdb", phase="enrich")(_noop)
    base.handler("mc:reviews", source="metacritic", phase="reviews")(_noop)

    assert base.available_kinds({}, phase="enrich") == ["tmdb:detail"]
    assert base.available_kinds({}, phase="reviews") == ["mc:reviews"]
    assert base.available_kinds({}) == ["mc:reviews", "tmdb:detail"]


def test_available_kinds_filters_by_the_capability_a_kind_requires():
    """Keyless sources declare no `requires` and must survive a map that says nothing about them."""
    base.handler("tmdb:detail", source="tmdb", requires="tmdb")(_noop)
    base.handler("omdb:detail", source="omdb", requires="omdb")(_noop)
    base.handler("wikipedia:article", source="wikipedia")(_noop)

    assert base.available_kinds({}) == ["wikipedia:article"]
    assert base.available_kinds({"tmdb": False}) == ["wikipedia:article"]
    assert base.available_kinds({"tmdb": True}) == ["tmdb:detail", "wikipedia:article"]
    assert base.available_kinds({"tmdb": True, "omdb": True}) == [
        "omdb:detail", "tmdb:detail", "wikipedia:article",
    ]


def test_paid_kinds_are_opt_in_by_name():
    """`mdc/sources/base.py:80-89`: "drain everything" is reasonable while every task is free."""
    base.handler("tmdb:detail", source="tmdb")(_noop)
    base.handler("llm:extract", source="llm", priority=200, paid=True)(_noop)

    assert base.paid_kinds() == {"llm:extract"}
    assert base.available_kinds({}, include_paid=False) == ["tmdb:detail"]
    assert base.available_kinds({}) == ["tmdb:detail", "llm:extract"]


def test_available_kinds_puts_the_cheap_resolve_before_the_scrape():
    """By name, `metacritic:page` and `rt:page` precede `wikidata:resolve`; the corpus's priorities
    (15, 76, 77) show the intent."""
    base.handler("metacritic:page", source="metacritic", priority=77)(_noop)
    base.handler("rt:page", source="rt", priority=76)(_noop)
    base.handler("wikidata:resolve", source="wikidata", priority=15)(_noop)
    base.handler("tmdb:resolve", source="tmdb", priority=10)(_noop)

    kinds = base.available_kinds({})
    assert kinds == ["tmdb:resolve", "wikidata:resolve", "rt:page", "metacritic:page"]
    assert kinds != sorted(kinds), "alphabetical order would put the scrapers first"


def test_a_tie_on_priority_breaks_on_the_kind():
    """Total order, so a failure message and a board's detail are stable across runs."""
    base.handler("b:one", source="b", priority=50)(_noop)
    base.handler("a:one", source="a", priority=50)(_noop)

    assert base.available_kinds({}) == ["a:one", "b:one"]


def test_the_source_result_cannot_be_edited_after_the_driver_reads_it():
    """`stages.Outcome`'s reason: the driver writes the board from these and then decides."""
    result = base.SourceResult(source="tmdb", kind="tmdb:detail", ok=True, doc_id=7)
    assert (result.note, result.doc_id) == ("", 7)
    with pytest.raises(FrozenInstanceError):
        result.ok = False


def test_json_get_answers_the_default_for_every_way_a_path_can_miss():
    """`mdc/sources/base.py:117-129`, ported verbatim: five misses and one answer."""
    assert base.json_get({"a": [{"b": 3}]}, "a", 0, "b") == 3

    # None mid-path - the provider sent the key and set it to null.
    assert base.json_get({"a": None}, "a", "b", default="miss") == "miss"
    # KeyError - an integer key into a dict that has no such key.
    assert base.json_get({"a": 1}, 0, default="miss") == "miss"
    # IndexError - past the end of a list.
    assert base.json_get([1, 2], 5, default="miss") == "miss"
    # TypeError - subscripting something that is not subscriptable.
    assert base.json_get(5, 0, default="miss") == "miss"
    # AttributeError - a string key into a list, which has no .get.
    assert base.json_get([1, 2], "a", default="miss") == "miss"

    # And a present-but-null leaf is a miss too, which is the last line of the port.
    assert base.json_get({"a": None}, "a", default="miss") == "miss"
    assert base.json_get({"a": 0}, "a", default="miss") == 0


def test_load_all_imports_the_adapters_and_none_of_the_scaffolding():
    """A property, not a count: the adapters land later."""
    assert base._is_adapter("tmdb") and base._is_adapter("metacritic")
    for scaffolding in ("base", "credentials", "_htmlutil", "__init__", "__main__"):
        assert not base._is_adapter(scaffolding), scaffolding

    loaded = base.load_all()
    assert loaded == sorted(loaded)
    assert not set(loaded) & {"base", "credentials", "_htmlutil"}
    # Idempotent: `handler` refuses only a DIFFERENT function for a kind it already holds.
    assert base.load_all() == loaded


_RT_SNIPPET = (
    b'<html><head>'
    b'<script type="application/ld+json">{"@type":"Movie","name":"Tampopo"}</script>'
    b'<script type="application/ld+json">{"@type":"Movie",,,}</script>'
    b'<script type="application/ld+json">{"@type":"Review","reviewBody":"good"}</script>'
    b'</head><body>nothing to see</body></html>'
)

_MC_SNIPPET = (
    b'<html><body>'
    b'<script id="__NEXT_DATA__" type="application/json">'
    b'{"props":{"pageProps":{"item":{"title":"Tampopo","criticScoreSummary":{"score":91}}}}}'
    b'</script></body></html>'
)


def test_ld_json_takes_every_well_formed_block_and_skips_the_broken_one():
    """A site that starts serving one malformed block must not cost the other two."""
    blocks = _htmlutil.ld_json(_RT_SNIPPET)
    assert [b["@type"] for b in blocks] == ["Movie", "Review"]
    assert _htmlutil.ld_json(b"<html><body>nothing</body></html>") == []


def test_next_data_answers_none_for_an_absent_or_broken_payload():
    data = _htmlutil.next_data(_MC_SNIPPET)
    assert base.json_get(data, "props", "pageProps", "item", "criticScoreSummary", "score") == 91
    assert _htmlutil.next_data(b"<html><body>no next data here</body></html>") is None
    assert _htmlutil.next_data(
        b'<html><script id="__NEXT_DATA__">{"props":,}</script></html>'
    ) is None


def test_walk_finds_a_review_shaped_node_wherever_it_sits():
    """Shape rather than path: it is what makes a parser survive the next markup change."""
    payload = {
        "props": {"pageProps": {"reviews": {"items": [
            {"quote": "a delight", "score": 90},
            {"quote": "less so", "score": 40},
        ]}}},
        "unrelated": [{"score": 1}],
    }
    found = list(_htmlutil.walk(payload, lambda n: "quote" in n and "score" in n))
    assert sorted(n["quote"] for n in found) == ["a delight", "less so"]


def test_unescape_peels_two_layers_and_no_more():
    """`&amp;#x27;` is one escaping pass too many upstream; a third peel would eat real text."""
    assert _htmlutil.unescape("Hello&amp;#x27;s") == "Hello's"
    assert _htmlutil.unescape("&#x27;quoted&#x27;") == "'quoted'"
    # Two passes and no more: a third peel would eat text that legitimately reads "&amp;".
    assert _htmlutil.unescape("AT&amp;amp;amp;T") == "AT&amp;T"
    assert _htmlutil.unescape(None) == ""


def test_clean_text_strips_markup_and_keeps_the_paragraph_break():
    raw = "<p>Bold&amp;#x27;s  <b>claim</b><br/>on the<span>&nbsp;</span>next line</p>"
    assert _htmlutil.clean_text(raw) == "Bold's claim \non the next line"
    assert _htmlutil.clean_text(None) == ""
    assert _htmlutil.clean_text("") == ""


def test_clean_text_normalises_the_punctuation_that_breaks_a_quote_match():
    """§8 stage 7 verifies a quote is a substring of the pack, so one curly apostrophe matters."""
    assert _htmlutil.clean_text("it\u2019s \u201cfine\u201d") == "it's \"fine\""


def test_clean_text_repairs_one_mojibake_run_and_leaves_real_accents_alone():
    """§4.1 rule 8: never "clean" non-ASCII. The repair acts only where it round-trips."""
    assert _htmlutil.clean_text("the caf\u00c3\u00a9 sc\u00c3\u00a8ne") == "the caf\u00e9 sc\u00e8ne"
    for untouched in ("S\u00e3o Paulo", "\u00c7a va", "\u6620\u753b", "\u0645\u0631\u062d\u0628\u0627"):
        assert _htmlutil.clean_text(untouched) == untouched


def test_the_per_run_repair_is_why_this_tree_carries_a_second_mojibake_function():
    """The corpus's per-run repair recovers bodies where one byte of a pair was lost, which the
    whole-string repair gives up on; kept only because inputs and outcomes differ."""
    damaged = "d\u00c3tail caf\u00c3\u00a9"
    assert _htmlutil.fix_mojibake(damaged) == "d\u00c3tail caf\u00e9"
    assert repair_mojibake(damaged) == (damaged, False)


def test_a_scraped_review_body_goes_through_both_repairs_and_that_is_the_deliberate_shape():
    """`review_row` runs the importer's repair after `clean_text`'s per-run one: the second pass
    finishes doubly-encoded bodies. The last assertion pins the cost (a marker before C1
    punctuation); if it reddens, re-measure."""
    truth = "Th\u00e9r\u00e8se"
    doubly = truth.encode("utf-8").decode("cp1252").encode("utf-8").decode("cp1252")
    once = _htmlutil.clean_text(doubly)
    assert once == "Th\u00c3\u00a9r\u00c3\u00a8se", (
        "the per-run repair is expected to take exactly one level off a doubly-encoded body"
    )

    stored = review_row(ParsedReview(body=once, source="metacritic"), 1)["body"]
    assert stored == truth, (
        "a scraped body reaches the store through both repairs; dropping the second one leaves "
        "every doubly-encoded review mangled"
    )
    # And the pass that is already correct is left alone - here.
    assert review_row(ParsedReview(body=truth, source="metacritic"), 1)["body"] == truth
    # Â before an ellipsis is correct text; the whole-string pass turns it into U+0085.
    marker_then_ellipsis = _htmlutil.clean_text("Â…")
    assert marker_then_ellipsis == "Â…", "the per-run guard is expected to decline it"
    stored = review_row(ParsedReview(body=marker_then_ellipsis, source="metacritic"), 1)["body"]
    assert stored == "\x85", (
        "the composition's recorded cost is gone; re-measure it and correct _htmlutil's paragraph"
    )


class _Conn:
    """The two calls `secrets.get_connector_secrets` makes, and nothing else."""

    def __init__(self, rows: dict[str, dict] | None = None) -> None:
        self.rows = rows or {}
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *args):
        self.queries.append(sql)
        return self.rows.get(args[0])


async def test_a_credential_that_is_not_configured_is_none_rather_than_an_exception():
    """Decision 334: an absent credential is a note, not a park. Through the shipped reader."""
    conn = _Conn()
    assert await credentials.tmdb_auth(conn) is None
    assert await credentials.omdb_key(conn) is None
    assert await credentials.trakt_headers(conn) is None
    assert len(conn.queries) == 3


async def test_a_trakt_client_id_is_read_from_the_config_blob():
    """`registry.env_seeds:94-98` puts the id in config and only the secret behind the DEK."""
    conn = _Conn({"trakt": {
        "config": {"client_id": "abc123"},
        "secrets_encrypted": None,
        "secrets_key_id": None,
    }})
    assert await credentials.trakt_headers(conn) == {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": "abc123",
    }


async def test_each_credential_carries_the_shape_its_source_asks_for(monkeypatch):
    """The corpus's request shapes: `tmdb.py:34-41`, `omdb.py:31`, `trakt.py:22-29`."""
    blobs = {
        "tmdb": ({}, {"api_key": "tk"}),
        "omdb": ({}, {"api_key": "ok"}),
        "trakt": ({"client_id": "ci"}, {"client_secret": "cs"}),
    }

    async def fake(conn, name):
        return blobs[name]

    monkeypatch.setattr(secrets, "get_connector_secrets", fake)

    assert await credentials.tmdb_auth(None) == (
        {"accept": "application/json"}, {"api_key": "tk"},
    )
    assert await credentials.omdb_key(None) == "ok"
    headers = await credentials.trakt_headers(None)
    assert headers["trakt-api-key"] == "ci"
    assert "cs" not in str(headers), "the client secret is not a header any stage-2 call carries"


async def test_a_configured_connector_with_an_empty_key_is_still_none(monkeypatch):
    """"Configured empty" must read as unconfigured, or a stage-2 call goes out with no key."""
    async def fake(conn, name):
        return {}, {"api_key": ""}

    monkeypatch.setattr(secrets, "get_connector_secrets", fake)
    assert await credentials.tmdb_auth(None) is None
    assert await credentials.omdb_key(None) is None


async def test_a_secret_that_will_not_open_is_none_and_is_logged(monkeypatch, caplog):
    """A drain that died on this would take the keyless sources down too; the repair is an admin's."""
    async def fake(conn, name):
        raise secrets.SecretsUnreadable("the stored secret does not open", "dek-1")

    monkeypatch.setattr(secrets, "get_connector_secrets", fake)
    caplog.set_level(logging.ERROR, logger="spielplan.sources.credentials")

    assert await credentials.tmdb_auth(None) is None
    assert await credentials.omdb_key(None) is None
    assert await credentials.trakt_headers(None) is None

    logged = [r.getMessage() for r in caplog.records]
    assert len(logged) == 3
    assert "connector tmdb secrets are unreadable" in logged[0]


_PACKAGE = Path(base.__file__).resolve().parent

# Either turns "re-parsing is free forever" into another crawl (decision 373).
TRANSPORT = ("httpx", "spielplan.acquire.fetch")

# The modules the PARSERS import; `credentials.py` is an adapter dependency.
GUARDED = ("base.py", "_htmlutil.py")


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """`from ._htmlutil import x` inside `spielplan.sources` is `spielplan.sources._htmlutil`."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    prefix = ".".join(parts[: len(parts) - node.level + 1])
    return f"{prefix}.{node.module}" if node.module else prefix


def _imported_modules(source: str, *, package: str = "spielplan.sources") -> set[str]:
    """Also `from x import y` as `x.y`: the layering helper records only `x`."""
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
    """The forbidden imports `source` makes, sorted so a failure message is stable."""
    return sorted(
        m for m in _imported_modules(source)
        if any(m == bad or m.startswith(f"{bad}.") for bad in TRANSPORT)
    )


def test_the_registry_and_the_markup_helpers_import_no_transport():
    """Decision 373: the adapters fetch; the parsers get bytes."""
    for name in GUARDED:
        source = (_PACKAGE / name).read_text(encoding="utf-8")
        imports = _imported_modules(source)
        # The guard is reading the file rather than measuring the parser.
        assert imports, f"sources/{name}: the guard parsed no import at all"
        assert not _transport(source), f"sources/{name} reaches for transport: {_transport(source)}"


def test_the_transport_guard_can_report_every_way_in():
    """A guard that cannot report a violation is a green line rather than a proof."""
    for illegal in (
        "import httpx\n",
        "from httpx import AsyncClient\n",
        "from spielplan.acquire import fetch\n",
        "from spielplan.acquire.fetch import Fetcher\n",
        "from spielplan.acquire import fetch as f\n",
    ):
        assert _transport(illegal), f"the guard missed: {illegal.strip()}"
    # And the imports these two modules legitimately make are not reported.
    assert not _transport("import json\nfrom spielplan.acquire.stages import StageContext\n")


_DB_VERBS = (
    "fetch", "fetchrow", "fetchval", "execute", "executemany", "cursor",
    "copy_records_to_table",
)


def _db_calls(source: str) -> list[str]:
    """Method calls on anything that look like a query against a connection."""
    return sorted({
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DB_VERBS
    })


def test_the_credential_read_issues_no_sql_of_its_own():
    """Decision 377: a second SELECT here would answer "what is configured" without the DEK."""
    source = Path(credentials.__file__).resolve().read_text(encoding="utf-8")
    assert "get_connector_secrets" in source, "the guard is reading the wrong file"
    assert _db_calls(source) == [], f"sources/credentials.py queries directly: {_db_calls(source)}"
    # The detector fires on the thing it forbids.
    assert _db_calls("async def f(conn):\n    return await conn.fetchrow('SELECT 1')\n") == [
        "fetchrow"
    ]
