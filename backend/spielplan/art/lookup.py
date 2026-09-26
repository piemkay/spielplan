"""The worker's TMDB lookup for a title with no servable poster (decision 484).

Writes `art_lookup` only. Worker-only, so `api.themoviedb.org` sees one token bucket (§8).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import asyncpg
import httpx

from spielplan.acquire.fetch import Fetcher, FetchError, HostPaused
from spielplan.art import sources
from spielplan.art.hosts import servable
from spielplan.core.config import settings
from spielplan.sources import _ids, credentials

log = logging.getLogger("spielplan.art.lookup")

API = "https://api.themoviedb.org/3"
IMAGE = "https://image.tmdb.org/t/p/w342"

# At 18 rps a batch is ~15 s of pacing.
BATCH = 300

# Decision 484's retry intervals.
NONE_AFTER = timedelta(days=30)
FAILED_AFTER = timedelta(days=1)


async def _file_placed(conn: asyncpg.Connection, room: int) -> int:
    """File placed, never-viewed titles with no servable poster, warm first."""
    if room <= 0:
        return 0
    rows = await conn.fetch(
        """
        SELECT t.id, t.poster_path
          FROM title t
         WHERE t.placement <> 'unplaced'
           AND (t.tmdb_id IS NOT NULL OR t.imdb_id IS NOT NULL)
           AND NOT (coalesce(t.poster_path, '') LIKE ANY ($1::text[]))
           AND NOT EXISTS (SELECT 1 FROM art_lookup l WHERE l.title_id = t.id)
         ORDER BY t.placement = 'warm' DESC, t.id
         LIMIT $2
        """,
        sources.servable_prefixes(), room,
    )
    ids = [r["id"] for r in rows if not servable(r["poster_path"])]
    if ids:
        await conn.execute(
            "INSERT INTO art_lookup (title_id) SELECT unnest($1::int[]) ON CONFLICT DO NOTHING",
            ids,
        )
    return len(ids)


async def _owed(conn: asyncpg.Connection, limit: int) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT l.title_id, t.kind, t.tmdb_id, t.imdb_id
          FROM art_lookup l JOIN title t ON t.id = l.title_id
         WHERE l.outcome IS NULL
            OR (l.outcome = 'failed' AND l.looked_up_at < now() - $2::interval)
            OR (l.outcome = 'none' AND l.looked_up_at < now() - $3::interval)
         ORDER BY l.outcome IS NOT NULL, l.requested_at, t.placement = 'warm' DESC, l.title_id
         LIMIT $1
        """,
        limit, FAILED_AFTER, NONE_AFTER,
    )


def _poster(payload: Any) -> str | None:
    path = payload.get("poster_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path.startswith("/") or "/" in path[1:]:
        return None
    return f"{IMAGE}{path}"


async def ask(
    fetcher: Fetcher, auth: tuple[dict[str, str], dict[str, str]], row: Any
) -> tuple[str, str | None, str]:
    """`(outcome, poster_url, note)` for one title. Raises `HostPaused` and a refused key.

    Falls back to `imdb_id` when `tmdb_id` 404s for this kind (§4.1 rule 6 movie/series pairs).
    """
    headers, params = auth
    kind = "movie" if row["kind"] == "movie" else "tv"
    note = "no id TMDB can be asked by"
    if row["tmdb_id"]:
        try:
            response = await fetcher.get(
                f"{API}/{kind}/{int(row['tmdb_id'])}", headers=headers, params=params,
                max_attempts=2,
            )
        except FetchError as exc:
            if exc.status != 404:
                raise
            note = f"TMDB has no {kind} {row['tmdb_id']}"
        else:
            url = _poster(response.json())
            if url:
                return sources.FOUND, url, f"{kind}/{row['tmdb_id']}"
            note = f"{kind}/{row['tmdb_id']} carries no poster"

    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return sources.NONE, None, note
    response = await fetcher.get(
        f"{API}/find/{imdb_id}", headers=headers,
        params={**params, "external_source": "imdb_id"}, max_attempts=2,
    )
    hits = response.json().get(f"{kind}_results") or []
    url = _poster(hits[0]) if hits else None
    if url:
        return sources.FOUND, url, f"find/{imdb_id}"
    return sources.NONE, None, f"TMDB has no {kind} poster for {imdb_id}"


async def drain(
    conn: asyncpg.Connection,
    *,
    limit: int = BATCH,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object] | None:
    """One batch of owed lookups; None when there was nothing to do."""
    if not settings().art_egress:
        return None
    auth = await credentials.tmdb_auth(conn)
    if auth is None:
        # No key, no lookup; filed rows wait for one.
        return None
    owed = await _owed(conn, limit)
    filed = await _file_placed(conn, limit - len(owed))
    if filed:
        owed = await _owed(conn, limit)
    if not owed:
        return None

    counts = {sources.FOUND: 0, sources.NONE: 0, sources.FAILED: 0}
    stopped = None
    async with Fetcher(conn=conn, transport=transport) as fetcher:
        for row in owed:
            try:
                outcome, url, note = await ask(fetcher, auth, row)
            except HostPaused as exc:
                stopped = str(exc)
                break
            except FetchError as exc:
                if exc.status in (401, 403):
                    # A refused key refuses every title: stop the batch, record nothing.
                    stopped = f"TMDB refused the configured key (HTTP {exc.status})"
                    log.warning("art lookup stopped: %s", stopped)
                    break
                outcome, url, note = sources.FAILED, None, str(exc)[:300]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                outcome, url, note = sources.FAILED, None, f"unreadable answer: {exc}"[:300]
            if url is not None and not servable(url):
                outcome, url, note = sources.NONE, None, f"answer off the allow-list: {url}"[:300]
            counts[outcome] += 1
            await conn.execute(
                "UPDATE art_lookup SET outcome = $2, poster_url = $3, note = $4, "
                "       looked_up_at = now() WHERE title_id = $1",
                row["title_id"], outcome, url, note,
            )
    report: dict[str, object] = {"asked": sum(counts.values()), **counts, "filed": filed}
    if stopped:
        report["stopped"] = stopped
    return report


__all__ = ["BATCH", "ask", "drain"]
