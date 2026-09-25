"""The worker's TMDB lookup for a title with no servable poster. Spec v2.1 §1 (acquisition is the
worker's), §6.8, §8's politeness clause (decision 340); decision 484.

The seed corpus fetched TMDB's detail for about half its titles, and §8 stage 2 runs for acquired
titles only, so a warm seed title Rate serves every week - The Village, Outbreak, Starman - can
carry no poster for the life of the install. Every one of them has a `tmdb_id` or an `imdb_id`,
and TMDB answers either: `/3/{movie|tv}/{tmdb_id}` when the id is known, chosen by `title.kind`
because §4.1 rule 6 records movie/series pairs sharing one; otherwise `/3/find/{imdb_id}`, taking
the result list that matches `kind`.

WHAT THIS WRITES IS `art_lookup` AND NOTHING ELSE. Not `title.poster_path` (decision 372 keeps
card fields as §8 stage 3's), not `tmdb_id` (the stage-2 adapter's `_find` would; this is not
stage 2 and records no identity), not the raw store. The answer is a URL the art route may serve,
kept beside the title and droppable, so decisions 162 and 372 stand exactly as they were.

IN THE WORKER AND ONLY THERE. The web process files a row and never asks: `api.themoviedb.org` is
paced by the worker's drains at the rate `acquire/hosts.py` declares, and a second process asking
the same host would be a second bucket for it. `art-lookup` runs inside the worker's sequential
tick, so it never overlaps a drain and the host sees one bucket whichever of the two is asking,
persisted to `fetch_host_state` like any drain's.
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

# One run's reach. At the declared 18 rps a batch this size is a quarter of a minute of pacing and
# well inside the job's budget, and at a half-hourly cadence the seed's ~9,200 posterless titles
# are asked about inside a day - the ones a member has viewed first, then the placed ones Rate
# will serve, warm before cold.
BATCH = 300

# Asked again after these, decision 484's numbers: TMDB holding no poster is an answer that can
# change (an image is added), a host that did not answer is not an answer at all.
NONE_AFTER = timedelta(days=30)
FAILED_AFTER = timedelta(days=1)


async def _file_placed(conn: asyncpg.Connection, room: int) -> int:
    """File placed titles with no servable poster that nobody has viewed yet, warm first.

    The art route files what a member looks at; this files what Rate is about to show them, so
    its cards have art before the first view rather than after it. Pre-filtered in SQL on the
    allow-list's own hosts (`sources.servable_prefixes`, one list), then held to `servable`.
    """
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

    By `tmdb_id` first, and by `imdb_id` when that id names nothing of this title's kind: a 404
    there is the movie/series duplicate §4.1 rule 6 records, and the IMDb id is a second, separate
    question TMDB can still answer.
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
    """One batch of owed lookups, answered and recorded. None when there was nothing to do."""
    if not settings().art_egress:
        return None
    auth = await credentials.tmdb_auth(conn)
    if auth is None:
        # Decision 484: no key, no lookup - and nothing filed is lost, it waits for one.
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
                    # A key TMDB refuses refuses every title alike, so nothing is recorded against
                    # them and the batch stops: rotating the key is the repair, not a retry.
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
