"""From TMDB (decision 558, §6.0): the films and series of a search Spielplan does not hold, and Want it on
one, which wishes the title its details resolve to or mints one (§4.2).

TMDB is asked with no pooled connection held, under the household's key, which never leaves this process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import asyncpg

from spielplan.acquire.fetch import Fetcher, FetchError
from spielplan.acquire.stages import _MINT_LOCK, APP_ID_MIN
from spielplan.art.poster import TMDB_FILE, TMDB_W342
from spielplan.connectors import resolve
from spielplan.db import genres as genre_vocab
from spielplan.db import library
from spielplan.home import wish
from spielplan.sources import credentials, tmdb

log = logging.getLogger("spielplan.home.beyond")

Connect = Callable[[], AbstractAsyncContextManager[asyncpg.Connection]]

MIN_QUERY = 3
CACHE_TTL_S = 600.0
CACHE_SIZE = 300


class TmdbUnavailable(RuntimeError):
    """No key is configured, or TMDB did not answer."""


class NoSuchTmdbTitle(LookupError):
    pass


class _Recent:
    """TMDB's answers by (kind, query) for a few minutes, so a repeat costs no request."""

    def __init__(self, *, ttl: float = CACHE_TTL_S, size: int = CACHE_SIZE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.size, self.clock = ttl, size, clock
        self._rows: OrderedDict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = OrderedDict()

    def get(self, key: tuple[str, str]) -> list[dict[str, Any]] | None:
        hit = self._rows.get(key)
        if hit is None or hit[0] <= self.clock():
            self._rows.pop(key, None)
            return None
        self._rows.move_to_end(key)
        return hit[1]

    def put(self, key: tuple[str, str], rows: list[dict[str, Any]]) -> None:
        self._rows[key] = (self.clock() + self.ttl, rows)
        self._rows.move_to_end(key)
        while len(self._rows) > self.size:
            self._rows.popitem(last=False)


RECENT = _Recent()


def _why(exc: Exception) -> str:
    """Never the message: an httpx error may spell the URL, which carries the query."""
    status = getattr(exc, "status", None)
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__


def _file(poster_path: str | None) -> str | None:
    name = (poster_path or "").removeprefix("/")
    return name if TMDB_FILE.fullmatch(name) else None


async def _search(fetcher: Fetcher, auth: Any, kind: str, query: str) -> list[dict[str, Any]]:
    rows = RECENT.get((kind, query))
    if rows is None:
        rows = await tmdb.search(fetcher, auth, kind=kind, q=query)
        RECENT.put((kind, query), rows)
    return rows


def _item(hit: dict[str, Any]) -> dict[str, Any]:
    file = _file(hit["poster_path"])
    return {
        "tmdb_id": hit["tmdb_id"],
        "kind": hit["kind"],
        "name": hit["name"],
        "original_name": hit["original_name"],
        "year": hit["year"],
        "overview": hit["overview"],
        "genres": genre_vocab.facet({g.lower() for g in hit["genres"]}),
        "poster": f"/api/art/tmdb/{file}" if file else None,
        "link": wish.link_for(hit["kind"], None, hit["tmdb_id"]),
    }


async def search_tmdb(
    fetcher: Fetcher, connect: Connect, *, kinds: Sequence[str], q: str
) -> dict[str, Any]:
    """From TMDB's hits of the kinds, less those Spielplan holds; absent with no key, a query under three
    characters, or TMDB paused or failing."""
    absent: dict[str, Any] = {"available": False, "items": []}
    query = " ".join(q.split()).casefold()
    if len(query) < MIN_QUERY:
        return absent
    kinds = library.normalise_kinds(kinds)
    async with connect() as conn:
        auth = await credentials.tmdb_auth(conn)
    if auth is None:
        return absent
    try:
        found = await asyncio.gather(*(_search(fetcher, auth, kind, query) for kind in kinds))
    except (FetchError, TimeoutError, ValueError) as exc:
        log.info("TMDB search unavailable: %s", _why(exc))
        return absent
    hits = [hit for rows in found for hit in rows]
    if len(kinds) > 1:
        # One list from two searches: TMDB's popularity interleaves them.
        hits.sort(key=lambda hit: -hit["popularity"])
    async with connect() as conn:
        rows = await conn.fetch(
            "SELECT kind, tmdb_id FROM title WHERE tmdb_id = ANY($1::int[]) AND kind = ANY($2::text[])",
            sorted({hit["tmdb_id"] for hit in hits}), kinds,
        )
    held = {(r["kind"], int(r["tmdb_id"])) for r in rows}
    return {
        "available": True,
        "items": [_item(hit) for hit in hits if (hit["kind"], hit["tmdb_id"]) not in held],
    }


async def _mint(conn: asyncpg.Connection, detail: dict[str, Any]) -> int:
    """The wished row §4.2 describes: enough for its card to render from the database alone."""
    file = _file(detail["poster_path"])
    title_id = int(await conn.fetchval(
        """
        INSERT INTO title (kind, name, original_name, year, runtime_min, imdb_id, tmdb_id, tvdb_id,
                           overview, poster_path, origin)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'wished')
        RETURNING id
        """,
        detail["kind"], detail["name"] or "(untitled)", detail["original_name"], detail["year"],
        detail["runtime_min"], detail["imdb_id"], detail["tmdb_id"], detail["tvdb_id"],
        detail["overview"], f"{TMDB_W342}{file}" if file else None,
    ))
    if title_id < APP_ID_MIN:
        raise RuntimeError(
            f"title {title_id} was minted below the app's id range ({APP_ID_MIN}): title_id_seq has "
            "moved, and that range is what keeps a minted title from a corpus id"
        )
    await conn.execute(
        "INSERT INTO title_genre (title_id, genre, source) SELECT $1, unnest($2::text[]), 'tmdb' "
        "ON CONFLICT DO NOTHING",
        title_id, detail["genres"],
    )
    return title_id


async def want_tmdb(
    fetcher: Fetcher, connect: Connect, *, user_id: int, kind: str, tmdb_id: int
) -> dict[str, Any]:
    """Want it on a From TMDB title: the Spielplan title of the kind its details resolve to, by IMDb id and
    then TMDB id, or one minted for it, is wished. `owned` means the library holds it and nothing was."""
    async with connect() as conn:
        auth = await credentials.tmdb_auth(conn)
    if auth is None:
        raise TmdbUnavailable("no TMDB key is configured")
    try:
        detail = await tmdb.detail_for_wish(fetcher, auth, kind=kind, tmdb_id=tmdb_id)
    except (FetchError, TimeoutError, ValueError) as exc:
        log.info("TMDB details for Want it unavailable: %s", _why(exc))
        raise TmdbUnavailable(_why(exc)) from exc
    if detail is None:
        raise NoSuchTmdbTitle(tmdb_id)

    item = {"Type": "Movie" if kind == "movie" else "Series",
            "ProviderIds": {"Imdb": detail["imdb_id"] or "", "Tmdb": str(tmdb_id)}}
    async with connect() as conn, conn.transaction():
        # Stage 1's claims, so neither a second Want it nor Jellyfin's arrival mints the film twice.
        claims = [f"imdb:{detail['imdb_id']}"] if detail["imdb_id"] else []
        for claim in (*claims, f"{kind}:tmdb_id:{tmdb_id}"):
            await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", _MINT_LOCK, claim)
        found = await resolve.resolve_title_id(conn, item)
        title_id = int(found) if found is not None else await _mint(conn, detail)
        try:
            await wish.set_state(conn, user_id=user_id, title_id=title_id, state="want")
        except wish.Owned:
            return {"title_id": title_id, "state": None, "owned": True, "minted": False}
    return {"title_id": title_id, "state": "want", "owned": False, "minted": found is None}


__all__ = [
    "CACHE_SIZE", "CACHE_TTL_S", "MIN_QUERY", "RECENT", "NoSuchTmdbTitle", "TmdbUnavailable",
    "search_tmdb", "want_tmdb",
]
