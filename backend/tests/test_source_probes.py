"""§6.6's test buttons for TMDB, OMDb and Trakt, against a canned host. Spec v2.1 §6.6, §8 stage 2,
§9; decisions 340, 433, 434, 453.

Decision 453 gives each keyed source one cheap request that fails on a bad key, sent through the
shared fetcher under the host policy `acquire/hosts.py` already declares, and nothing written to the
raw store. TMDB v3 and OMDb take their key only as a query parameter, which is the whole reason this
file exists: httpx writes every request url into an INFO line of its own, so a naive test button
copies a working key into the web process's log on every press, and a host that quotes the url back
in its refusal puts it into the card's error text as well. So what is asserted is the request each
probe puts on the wire, exactly; the three answers (accepted, refused, never asked); and that the key
is in neither the answer nor httpx's line.

Nothing here reaches the network: every request is served by an `httpx.MockTransport`, on a real
`Fetcher` over this test's database, the shape `test_sources_adapters.py` established.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

import httpx
import pytest

from spielplan.acquire import fetch
from spielplan.connectors import probes, registry
from spielplan.core import secrets
from spielplan.core.config import settings
from spielplan.llm import client

KEY_TMDB = "tmdb-probe-key-0123456789abcdef"
KEY_OMDB = "omdbprobe"
TRAKT_ID = "trakt-probe-client-id-0123456789"


class _Host:
    """A canned web: one answer per host, and a record of every request that reached it."""

    def __init__(self, answers: dict[str, httpx.Response]) -> None:
        self.answers = answers
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.answers.get(request.url.host)
        return answer if answer is not None else httpx.Response(404, content=b"not found")


async def _no_sleep(seconds: float) -> None:
    return None


def _opener(host: _Host):
    """`client.open_fetcher`'s shape, over the canned host: the real Fetcher, on the probe's own
    connection, so the host's declared policy, pacing and breaker apply to the button."""
    return lambda conn: fetch.Fetcher(conn=conn, transport=httpx.MockTransport(host.handler),
                                      sleep=_no_sleep, jitter=lambda low, high: 0.0)


@pytest.fixture
async def keyed(db, secrets_key):
    """The three sources configured the way §2 stores them: two sealed keys, and Trakt's client id in
    the plaintext half (`registry.env_seeds`' own split)."""
    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": KEY_TMDB})
    await secrets.put_connector_secrets(db, "omdb", {}, {"api_key": KEY_OMDB})
    await secrets.put_connector_config(db, "trakt", {"client_id": TRAKT_ID})
    return db


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


async def test_each_probe_asks_its_host_the_documented_question(keyed):
    """The three requests decision 453 names, asserted on the wire: TMDB's configuration read with
    the v3 key as its query parameter, one OMDb lookup of a fixed IMDb id, and Trakt's trending list
    at one item with its three headers. Each host answering as it does for a good key is `ok`."""
    host = _Host({
        "api.themoviedb.org": _json(200, {"images": {"base_url": "http://image.tmdb.org/t/p/"}}),
        "www.omdbapi.com": _json(200, {"Title": "The Shawshank Redemption", "Response": "True"}),
        "api.trakt.tv": _json(200, [{"watchers": 12, "movie": {"title": "Heat"}}]),
    })
    opener = _opener(host)

    for probe in (probes.tmdb, probes.omdb, probes.trakt):
        assert await probe(keyed, open_fetcher=opener) == {"ok": True, "status": 200, "error": None}

    tmdb, omdb, trakt = host.seen
    assert (tmdb.method, tmdb.url.scheme, tmdb.url.host, tmdb.url.path) == (
        "GET", "https", "api.themoviedb.org", "/3/configuration")
    assert parse_qs(tmdb.url.query.decode()) == {"api_key": [KEY_TMDB]}
    assert tmdb.headers["accept"] == "application/json"

    assert (omdb.method, omdb.url.host, omdb.url.path) == ("GET", "www.omdbapi.com", "/")
    assert parse_qs(omdb.url.query.decode()) == {"apikey": [KEY_OMDB], "i": [probes.OMDB_PROBE_ID]}

    assert (trakt.method, trakt.url.host, trakt.url.path) == ("GET", "api.trakt.tv", "/movies/trending")
    assert parse_qs(trakt.url.query.decode()) == {"limit": ["1"]}
    assert (trakt.headers["trakt-api-key"], trakt.headers["trakt-api-version"],
            trakt.headers["content-type"]) == (TRAKT_ID, "2", "application/json")


@pytest.mark.parametrize(
    "probe,host,answer,key",
    [
        pytest.param(probes.tmdb, "api.themoviedb.org",
                     _json(401, {"status_code": 7, "success": False,
                                 "status_message": f"Invalid API key: {KEY_TMDB} was refused"}),
                     KEY_TMDB, id="tmdb-401"),
        # OMDb answers a bad key 401 on some paths and 200 with Response=False on others.
        pytest.param(probes.omdb, "www.omdbapi.com",
                     _json(401, {"Response": "False", "Error": f"Invalid API key! ({KEY_OMDB})"}),
                     KEY_OMDB, id="omdb-401"),
        pytest.param(probes.omdb, "www.omdbapi.com",
                     _json(200, {"Response": "False",
                                 "Error": f"Invalid API key! /?apikey={KEY_OMDB}&i=tt0111161"}),
                     KEY_OMDB, id="omdb-200-response-false"),
        pytest.param(probes.trakt, "api.trakt.tv",
                     httpx.Response(403, content=f"Forbidden - invalid API key {TRAKT_ID}".encode()),
                     TRAKT_ID, id="trakt-403"),
    ],
)
async def test_a_refused_key_is_not_ok_and_the_refusal_never_quotes_it(keyed, probe, host, answer, key):
    """A host refusing the key is `ok: false` with its own words, and those words with the key taken
    out wherever the host quoted it -- in prose or inside a url -- because the answer is rendered on
    the card and a quoted key there is the credential in the DOM."""
    result = await probe(keyed, open_fetcher=_opener(_Host({host: answer})))
    assert (result["ok"], result["status"]) == (False, answer.status_code), result
    assert result["error"], result
    assert key not in result["error"], result


async def test_a_probe_with_no_key_asks_nobody_and_says_what_is_missing(db, secrets_key, monkeypatch):
    """The answers known without a request are given without one: no key typed yet, per source; and,
    for the two sealed keys, a key this SECRETS_KEY cannot open, in the rail's own sentence. Trakt's
    client id is plaintext, so an unreadable DEK does not stop its button -- the adapters read it
    the same way (`credentials.trakt_headers`)."""
    host = _Host({})
    opener = _opener(host)
    assert await probes.tmdb(db, open_fetcher=opener) == {
        "ok": False, "status": None, "error": "no TMDB API key is configured"}
    assert await probes.omdb(db, open_fetcher=opener) == {
        "ok": False, "status": None, "error": "no OMDb API key is configured"}
    assert await probes.trakt(db, open_fetcher=opener) == {
        "ok": False, "status": None, "error": "no Trakt client id is configured"}

    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": KEY_TMDB})
    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    assert await probes.tmdb(db, open_fetcher=opener) == {
        "ok": False, "status": None, "error": registry.SECRETS_UNREADABLE_REASON}
    assert host.seen == []


async def test_a_probe_stores_nothing_in_the_raw_store(keyed):
    """Decision 453: a test press is a question about a key, not a document of a title, so nothing it
    reads lands in `raw_document` -- where §6.6's board would show it against no title at all."""
    host = _Host({
        "api.themoviedb.org": _json(200, {"images": {}}),
        "www.omdbapi.com": _json(200, {"Response": "True"}),
        "api.trakt.tv": _json(200, []),
    })
    before = await keyed.fetchval("SELECT count(*) FROM raw_document")
    for probe in (probes.tmdb, probes.omdb, probes.trakt):
        assert (await probe(keyed, open_fetcher=_opener(host)))["ok"] is True
    assert await keyed.fetchval("SELECT count(*) FROM raw_document") == before


async def test_httpx_logs_the_probe_request_with_the_query_key_masked(keyed, caplog):
    """Decision 453's logging filter, installed when the probe module loads: httpx still writes its
    INFO line -- the trace an operator reads -- with the value of `api_key` and `apikey` masked."""
    caplog.set_level(logging.INFO, logger="httpx")
    host = _Host({
        "api.themoviedb.org": _json(200, {"images": {}}),
        "www.omdbapi.com": _json(200, {"Response": "True"}),
    })
    await probes.tmdb(keyed, open_fetcher=_opener(host))
    await probes.omdb(keyed, open_fetcher=_opener(host))

    lines = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    assert [line for line in lines if "api.themoviedb.org" in line], lines
    assert [line for line in lines if "www.omdbapi.com" in line], lines
    assert not [line for line in lines if KEY_TMDB in line or f"apikey={KEY_OMDB}" in line], lines
    assert any("api_key=[redacted]" in line for line in lines), lines
    assert any("apikey=[redacted]" in line for line in lines), lines
    assert KEY_TMDB not in caplog.text


async def test_the_dispatch_serves_the_three_source_buttons(keyed, app, monkeypatch):
    """The three probes are the three `ConnectorSpec.test` entries, so plan A4's one dispatch serves
    them (decision 433): the source cards' buttons are `POST /api/admin/connectors/{name}/test`,
    reaching the host through the one fetcher built outside a drain."""
    host = _Host({
        "api.themoviedb.org": _json(200, {"images": {}}),
        "www.omdbapi.com": _json(200, {"Response": "True"}),
        "api.trakt.tv": _json(200, []),
    })
    monkeypatch.setattr(client, "open_fetcher", _opener(host))
    admin = app()
    created = await admin.post("/api/setup/admin", json={"name": "patrick", "password": "a-password-1"})
    assert created.status_code == 201, created.text

    for name in ("tmdb", "omdb", "trakt"):
        response = await admin.post(f"/api/admin/connectors/{name}/test")
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "status": 200, "error": None}
    assert [request.url.host for request in host.seen] == [
        "api.themoviedb.org", "www.omdbapi.com", "api.trakt.tv"]
