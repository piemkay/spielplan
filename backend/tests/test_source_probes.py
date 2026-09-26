"""TMDB v3 and OMDb take their key as a query parameter and httpx logs every url at INFO, so the
wire request, the three answers, and the key's absence from answer and log are asserted."""

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
    """The real Fetcher on the probe's own connection, so host policy, pacing and breaker apply."""
    return lambda conn: fetch.Fetcher(conn=conn, transport=httpx.MockTransport(host.handler),
                                      sleep=_no_sleep, jitter=lambda low, high: 0.0)


@pytest.fixture
async def keyed(db, secrets_key):
    """Two sealed keys, and Trakt's client id in the plaintext half (`registry.env_seeds`)."""
    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": KEY_TMDB})
    await secrets.put_connector_secrets(db, "omdb", {}, {"api_key": KEY_OMDB})
    await secrets.put_connector_config(db, "trakt", {"client_id": TRAKT_ID})
    return db


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


async def test_each_probe_asks_its_host_the_documented_question(keyed):
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
    """The refusal is rendered on the card, so a quoted key (prose or url) must be taken out."""
    result = await probe(keyed, open_fetcher=_opener(_Host({host: answer})))
    assert (result["ok"], result["status"]) == (False, answer.status_code), result
    assert result["error"], result
    assert key not in result["error"], result


async def test_a_probe_with_no_key_asks_nobody_and_says_what_is_missing(db, secrets_key, monkeypatch):
    """Trakt's client id is plaintext, so an unreadable DEK does not stop its button."""
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
    """Decision 453: a probe is a question about a key, so nothing lands in `raw_document`."""
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
    """Decision 453's logging filter masks `api_key` and `apikey` in httpx's INFO line."""
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
