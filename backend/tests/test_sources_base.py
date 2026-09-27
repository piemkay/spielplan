"""The ast helpers copy `test_layering_guards.py`'s but also record `from x import y` as `x.y`,
so `from spielplan.acquire import fetch` cannot slip past."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError

import pytest

from spielplan.core import secrets
from spielplan.derive.reviews import ParsedReview, review_row
from spielplan.importer.reviews import repair_mojibake
from spielplan.sources import _htmlutil, base, credentials


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
