"""§6.6's test buttons for the three keyed sources. Spec v2.1 §6.6, §8 stage 2, §9; decisions 340,
433, 434, 453.

§6.6: "TMDB / OMDb / Trakt keys with test buttons". Decision 433 left the three to M5.7's source
cards and decision 453 says what each asks: the cheapest request its host answers that fails on a
bad key -- TMDB's configuration read, one OMDb lookup of a fixed IMDb id, Trakt's trending list at
one item -- through `acquire.fetch.Fetcher` under the host policies `acquire/hosts.py` already
declares, with nothing written to the raw store. Each answers `{ok, status, error}`, and
`registry.CONNECTORS` registers them as the three `ConnectorSpec.test` entries, so plan A4's one
dispatch serves the source cards as it serves the provider cards.

HERE AND NOT IN `sources/`, because that package is §8 stage 2's adapters and its guards say what an
adapter is: `test_sources_adapters.py` holds that no module there constructs a fetcher or imports
`acquire.fetch` but `_views` (the one door, where a response is co-keyed into the raw store) and two
that name its `Response` type, and `sources/base.load_all` imports every public module in the
package as an adapter. A probe is none of that -- it is a question about a key, stores nothing a
title's board could show, and has to catch `fetch.FetchError` to answer at all -- so it is a
connector concern beside `registry`, reading its keys through `sources/credentials`, which decision
434 keeps as the three sources' one reader.

A KEY IN A QUERY STRING IS A KEY IN A LOG LINE, which is the half of decision 453 this module exists
for. TMDB v3 and OMDb accept a key only as a query parameter, and httpx writes every request url
into an INFO line of its own -- so a naive test press copies a working key into the web process's
log, the exposure `push/send.py` already guards its own urls against. The filter below masks the
value of any `api_key` or `apikey` parameter in httpx's lines, installed when this module loads,
which `registry._probe_source` makes happen before the first probe's request. And a host that
echoes the request in its refusal would put the key into the card's error text, so every error
this module returns has the key taken out first, in every spelling a message can give it
(`llm/client._redacted`'s rule, named change 8).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from spielplan.acquire import fetch
from spielplan.connectors import registry
from spielplan.sources import credentials

if TYPE_CHECKING:
    import asyncpg

# Decision 453's three questions. OMDb's is one lookup of a title that has been on the site for as
# long as the site has existed, so a good key cannot be told "not found" by it.
TMDB_URL = "https://api.themoviedb.org/3/configuration"
OMDB_URL = "https://www.omdbapi.com/"
OMDB_PROBE_ID = "tt0111161"
TRAKT_URL = "https://api.trakt.tv/movies/trending"

# The refusals a bad key earns, handed back as answers so the card can quote the host's own words;
# anything else the fetcher raises is answered from the `FetchError`.
_ANSWER_STATUS = (400, 401, 403, 404)

# `llm/client._SHOWN`'s cut, applied after the key is taken out of the whole text (named change 8:
# cut first, and a key straddling the cut survives as its prefix).
_SHOWN = 300

# The value of a query-string key, in any url a line or a message carries.
_QUERY_KEY = re.compile(r"([?&](?:api_key|apikey)=)[^&#\s\"'<>]*", re.IGNORECASE)
_MASK = "[redacted]"


class _MaskQueryKeysInHttpxLogs(logging.Filter):
    """httpx logs one INFO line per request with the full url in it, and there is no per-client
    switch for that line (`push/send.py`'s own argument). The line is kept -- it is the trace an
    operator reads when a source stops answering -- with the key's value masked, for every caller:
    no query parameter named `api_key` or `apikey` is anything but a credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a filter that raised would fail the request it logged
            return True
        masked = _QUERY_KEY.sub(rf"\1{_MASK}", message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


logging.getLogger("httpx").addFilter(_MaskQueryKeysInHttpxLogs())

Opener = Callable[["asyncpg.Connection"], AbstractAsyncContextManager[fetch.Fetcher]]


def _shown(text: str, key: str) -> str:
    """A host's words as the card shows them: the key out of the whole text in every spelling --
    as typed, trimmed, a str or bytes repr, percent-encoded inside a url -- any query-string key
    masked, and only then cut to `_SHOWN`."""
    spellings = {key, key.strip(), repr(key)[1:-1],
                 repr(key.encode("utf-8", "backslashreplace"))[2:-1], quote(key, safe="")}
    for spelling in sorted((s for s in spellings if s), key=len, reverse=True):
        text = text.replace(spelling, _MASK)
    return _QUERY_KEY.sub(rf"\1{_MASK}", text)[:_SHOWN]


def _answer(ok: bool, status: int | None, error: str | None) -> dict[str, Any]:
    return {"ok": ok, "status": status, "error": error}


async def _without_a_key(conn: asyncpg.Connection, name: str, missing: str) -> dict[str, Any]:
    """The answer a sealed key's absence earns, with no request: "no key yet", or -- when a key
    exists and this SECRETS_KEY cannot open it -- the rail's own sentence, because "type one" would
    send the admin to replace a key that only needs its env file back (M4.7 dd03)."""
    state = await registry.load_connector(conn, name)
    return _answer(False, None, registry.SECRETS_UNREADABLE_REASON if state.secrets_unreadable
                   else missing)


def _json(resp: fetch.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


async def _ask(
    conn: asyncpg.Connection,
    open_fetcher: Opener,
    url: str,
    *,
    key: str,
    headers: dict[str, str],
    params: dict[str, str],
    refused: Callable[[fetch.Response], str | None],
) -> dict[str, Any]:
    """One GET through the shared fetcher, judged by `refused`: None is a good key, a string is the
    host's refusal. Nothing is stored -- the response is read and dropped -- because a probe is not
    a document of any title (decision 453)."""
    try:
        async with open_fetcher(conn) as fetcher:
            resp = await fetcher.get(url, headers=headers, params=params,
                                     allow_status=_ANSWER_STATUS)
    except fetch.FetchError as exc:
        return _answer(False, exc.status, _shown(str(exc), key))
    why = refused(resp)
    if why is not None:
        return _answer(False, resp.status, _shown(why, key))
    return _answer(True, resp.status, None)


def _tmdb_refused(resp: fetch.Response) -> str | None:
    """TMDB answers a bad v3 key 401 with its own `status_message`; a good one with the image
    configuration, whose `images` block is what separates it from a portal page served as a 200."""
    body = _json(resp)
    if resp.status == 200 and isinstance(body, dict) and "images" in body:
        return None
    if isinstance(body, dict) and isinstance(body.get("status_message"), str):
        return body["status_message"]
    return f"TMDB answered HTTP {resp.status} without its configuration"


def _omdb_refused(resp: fetch.Response) -> str | None:
    """OMDb says `"Response": "True"` for a lookup it served, and `"False"` with an `Error` for one it
    refused -- as a 401 for a bad key on some paths and as a 200 on others, so the body decides."""
    body = _json(resp)
    if resp.status == 200 and isinstance(body, dict) and body.get("Response") == "True":
        return None
    if isinstance(body, dict) and isinstance(body.get("Error"), str):
        return body["Error"]
    return f"OMDb answered HTTP {resp.status} without a lookup"


def _trakt_refused(resp: fetch.Response) -> str | None:
    """Trakt answers its trending list as a JSON array, and a bad client id 403 with a line of text."""
    if resp.status == 200 and isinstance(_json(resp), list):
        return None
    words = resp.text.strip()
    return f"Trakt answered HTTP {resp.status}" + (f": {words}" if words else "")


async def tmdb(conn: asyncpg.Connection, *, open_fetcher: Opener) -> dict[str, Any]:
    """TMDB's card: `GET /3/configuration` with the v3 key as `credentials.tmdb_auth` builds it."""
    auth = await credentials.tmdb_auth(conn)
    if auth is None:
        return await _without_a_key(conn, credentials.TMDB, "no TMDB API key is configured")
    headers, params = auth
    return await _ask(conn, open_fetcher, TMDB_URL, key=params["api_key"], headers=headers,
                      params=params, refused=_tmdb_refused)


async def omdb(conn: asyncpg.Connection, *, open_fetcher: Opener) -> dict[str, Any]:
    """OMDb's card: one lookup of `OMDB_PROBE_ID`, which spends one request of the key's daily
    quota -- the price decision 453 records, and the card says so."""
    key = await credentials.omdb_key(conn)
    if key is None:
        return await _without_a_key(conn, credentials.OMDB, "no OMDb API key is configured")
    return await _ask(conn, open_fetcher, OMDB_URL, key=key, headers={},
                      params={"apikey": key, "i": OMDB_PROBE_ID}, refused=_omdb_refused)


async def trakt(conn: asyncpg.Connection, *, open_fetcher: Opener) -> dict[str, Any]:
    """Trakt's card: the trending list at one item, with `credentials.trakt_headers`' three headers.

    The client id is plaintext config (`registry.env_seeds`' split), so an unreadable DEK does not
    stop this button, as it does not stop the adapters: the probe asks what stage 2 would send."""
    headers = await credentials.trakt_headers(conn)
    if headers is None:
        return _answer(False, None, "no Trakt client id is configured")
    return await _ask(conn, open_fetcher, TRAKT_URL, key=headers["trakt-api-key"], headers=headers,
                      params={"limit": "1"}, refused=_trakt_refused)


PROBES: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    credentials.TMDB: tmdb,
    credentials.OMDB: omdb,
    credentials.TRAKT: trakt,
}

__all__ = ["OMDB_PROBE_ID", "OMDB_URL", "PROBES", "TMDB_URL", "TRAKT_URL", "omdb", "tmdb", "trakt"]
