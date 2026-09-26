"""§6.6's test buttons for the three keyed sources (decision 453): TMDB, OMDb and Trakt.

The cheapest request that fails on a bad key, through `acquire.fetch`, storing nothing. Keys in
query strings are masked in httpx's log lines and taken out of every error this returns.
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

# Decision 453's three questions; OMDb's asks about a title that has always been on the site.
TMDB_URL = "https://api.themoviedb.org/3/configuration"
OMDB_URL = "https://www.omdbapi.com/"
OMDB_PROBE_ID = "tt0111161"
TRAKT_URL = "https://api.trakt.tv/movies/trending"

# A bad key's refusals, answered in the host's own words.
_ANSWER_STATUS = (400, 401, 403, 404)

# Cut after the key is taken out, or a key straddling the cut survives as its prefix.
_SHOWN = 300

# The value of a query-string key, in any url a line or a message carries.
_QUERY_KEY = re.compile(r"([?&](?:api_key|apikey)=)[^&#\s\"'<>]*", re.IGNORECASE)
_MASK = "[redacted]"


class _MaskQueryKeysInHttpxLogs(logging.Filter):
    """Mask `api_key`/`apikey` values in httpx's per-request INFO lines, keeping the lines."""

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
    """A host's words for the card: the key out in every spelling, query keys masked, then cut."""
    spellings = {key, key.strip(), repr(key)[1:-1],
                 repr(key.encode("utf-8", "backslashreplace"))[2:-1], quote(key, safe="")}
    for spelling in sorted((s for s in spellings if s), key=len, reverse=True):
        text = text.replace(spelling, _MASK)
    return _QUERY_KEY.sub(rf"\1{_MASK}", text)[:_SHOWN]


def _answer(ok: bool, status: int | None, error: str | None) -> dict[str, Any]:
    return {"ok": ok, "status": status, "error": error}


async def _without_a_key(conn: asyncpg.Connection, name: str, missing: str) -> dict[str, Any]:
    """No request: "no key yet", or the rail's sentence when this SECRETS_KEY cannot open one (dd03)."""
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
    """One GET through the shared fetcher, judged by `refused` (None is a good key); nothing stored."""
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
    """TMDB: a bad v3 key is a 401 with `status_message`; a good one returns an `images` block."""
    body = _json(resp)
    if resp.status == 200 and isinstance(body, dict) and "images" in body:
        return None
    if isinstance(body, dict) and isinstance(body.get("status_message"), str):
        return body["status_message"]
    return f"TMDB answered HTTP {resp.status} without its configuration"


def _omdb_refused(resp: fetch.Response) -> str | None:
    """OMDb: `"Response": "True"` when served; a refusal is a 401 or a 200, so the body decides."""
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
    """OMDb's card: one lookup of `OMDB_PROBE_ID`, which spends one request of the daily quota."""
    key = await credentials.omdb_key(conn)
    if key is None:
        return await _without_a_key(conn, credentials.OMDB, "no OMDb API key is configured")
    return await _ask(conn, open_fetcher, OMDB_URL, key=key, headers={},
                      params={"apikey": key, "i": OMDB_PROBE_ID}, refused=_omdb_refused)


async def trakt(conn: asyncpg.Connection, *, open_fetcher: Opener) -> dict[str, Any]:
    """Trakt's card: trending at one item; the client id is plaintext, so a sealed DEK does not stop it."""
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
