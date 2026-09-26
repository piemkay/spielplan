"""A title's poster, from the cache or fetched once (§6.8, decisions 483-485).

Never await upstream while holding a pooled connection; concurrent misses share one shielded task;
one process-lifetime Fetcher is each image host's single bucket.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx

from spielplan.acquire.fetch import Fetcher, FetchError
from spielplan.acquire.hosts import JELLYFIN_POLICY
from spielplan.art import cache, sources
from spielplan.art.hosts import servable
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinError

log = logging.getLogger("spielplan.art")

Connect = Callable[[], AbstractAsyncContextManager[asyncpg.Connection]]

DAY = 86400.0

# Server-side TTLs, decision 483. An errored preferred source's fallback is kept one day.
THIRD_PARTY_TTL = 180 * DAY
JELLYFIN_TTL = 7 * DAY
STAND_IN_TTL = 1 * DAY
# No image anywhere: a week. Host did not answer: ten minutes.
MISSING_TTL = 7 * DAY
ERROR_TTL = 600.0

# Browser cache, not the server TTL: `private` (session-gated), never `immutable` (URL names a title).
# 404s are cacheable too, or every posterless card re-asks on each render.
BROWSER_MAX_AGE = 15552000
BROWSER_NONE = 86400
# Matches the half-hourly `art-lookup` job.
BROWSER_PENDING = 1800
BROWSER_TRANSIENT = 600

# Sniffed from the bytes, not the host's Content-Type; responses carry `nosniff`.
MAX_BYTES = 2 * 1024 * 1024
JELLYFIN_WIDTH = 342
# Seconds, retries and `Retry-After` included.
UPSTREAM_DEADLINE_S = 20.0
UPSTREAM_TIMEOUT_S = 8.0


def sniff(data: bytes) -> str | None:
    """The image type these bytes are, or None when they are not one this route serves."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def acceptable(data: bytes, declared: str | None) -> str | None:
    """The type to serve these bytes as, or None: an image of a named type, under the cap, whose
    host did not call it something else."""
    if not data or len(data) > MAX_BYTES:
        return None
    declared = (declared or "").split(";")[0].strip().lower()
    if declared and not declared.startswith("image/"):
        return None
    return sniff(data)


@dataclass(frozen=True)
class Answer:
    """What the route sends: bytes and their type, or a 404, with how long a browser keeps it."""

    status: int
    max_age: int
    body: bytes = b""
    content_type: str | None = None
    etag: str | None = None

    @classmethod
    def none(cls, max_age: int) -> Answer:
        return cls(404, max_age)


async def url_epoch(conn: asyncpg.Connection) -> str | None:
    """Poster URL version for app-minted ids: a digest of this database's birth.

    App-minted ids restart on a re-seed while browsers keep a 200 for 180 days, so the URL must change.
    """
    born = await conn.fetchval("SELECT min(applied_at) FROM schema_migration")
    if born is None:
        return None
    return hashlib.sha256(born.isoformat().encode("utf-8")).hexdigest()[:12]


class _Outcome:
    OK, MISSING, ERROR, SKIPPED = "ok", "missing", "error", "skipped"


class ArtService:
    def __init__(
        self,
        root: Path,
        *,
        egress: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache = cache.ArtCache(root, clock=clock)
        # Egress off (e2e, CI) means no internet fetch; the household Jellyfin is asked either way.
        self.egress = egress
        self._fetcher = Fetcher(transport=transport)
        self._opened = False
        self._inflight: dict[int, asyncio.Task[Answer]] = {}
        self._jellyfin = asyncio.Semaphore(JELLYFIN_POLICY.max_concurrency)

    async def open(self) -> ArtService:
        await self._fetcher.__aenter__()
        self._opened = True
        return self

    async def close(self) -> None:
        if self._opened:
            self._opened = False
            await self._fetcher.__aexit__(None, None, None)

    async def poster(self, title_id: int, *, connect: Connect) -> Answer:
        async with connect() as conn:
            row = await sources.read(conn, title_id)
        if row is None:
            return Answer.none(BROWSER_NONE)
        candidates = row.candidates()
        if not candidates:
            return Answer.none(BROWSER_PENDING if row.lookup_owed else BROWSER_NONE)

        sig = row.signature
        hit = await asyncio.to_thread(self.cache.read, title_id, sig)
        if hit is not None:
            return await self._from_cache(title_id, hit, row)

        task = self._inflight.get(title_id)
        if task is None:
            task = asyncio.get_running_loop().create_task(
                self._fill(title_id, row, candidates, sig, connect)
            )
            self._inflight[title_id] = task
            task.add_done_callback(lambda _t, key=title_id: self._inflight.pop(key, None))
        # Shielded: the first phone closing its tab must not cancel the fetch for the others.
        return await asyncio.shield(task)

    async def _from_cache(self, title_id: int, entry: cache.Entry, row) -> Answer:
        if entry.status != cache.OK:
            if entry.status == cache.ERROR:
                return Answer.none(BROWSER_TRANSIENT)
            return Answer.none(BROWSER_PENDING if row.lookup_owed else BROWSER_NONE)
        try:
            body = await asyncio.to_thread(self.cache.bytes_of, title_id)
        except OSError:
            return Answer.none(BROWSER_TRANSIENT)
        return Answer(200, BROWSER_MAX_AGE, body, entry.content_type, entry.etag)

    async def _fill(self, title_id, row, candidates, sig, connect: Connect) -> Answer:
        outcomes: list[str] = []
        for candidate in candidates:
            if candidate.source == sources.JELLYFIN:
                outcome, got = await self._from_jellyfin(candidate, connect)
            else:
                outcome, got = await self._from_host(candidate)
            outcomes.append(outcome)
            if outcome != _Outcome.OK:
                continue
            data, content_type = got
            if candidate.source == sources.JELLYFIN:
                ttl = JELLYFIN_TTL
            elif _Outcome.ERROR in outcomes[:-1]:
                ttl = STAND_IN_TTL
            else:
                ttl = THIRD_PARTY_TTL
            try:
                entry = await asyncio.to_thread(
                    self.cache.store, title_id, sig, data,
                    content_type=content_type, source=candidate.source, ttl=ttl,
                )
                etag = entry.etag
            except OSError as exc:
                # A cache write failure costs the next view a fetch, not this one its poster.
                log.warning("poster for title %s not cached: %s", title_id, exc)
                etag = None
            return Answer(200, BROWSER_MAX_AGE, data, content_type, etag)

        if all(o == _Outcome.SKIPPED for o in outcomes):
            # Nothing was asked (egress off, no Jellyfin), so nothing to remember.
            return Answer.none(BROWSER_PENDING)
        failed = _Outcome.ERROR in outcomes
        with contextlib.suppress(OSError):
            await asyncio.to_thread(
                self.cache.store_negative, title_id, sig,
                status=cache.ERROR if failed else cache.MISSING,
                ttl=ERROR_TTL if failed else MISSING_TTL,
            )
        if failed:
            return Answer.none(BROWSER_TRANSIENT)
        return Answer.none(BROWSER_PENDING if row.lookup_owed else BROWSER_NONE)

    async def _from_jellyfin(self, candidate, connect: Connect):
        async with connect() as conn:
            cfg = await registry.load_jellyfin(conn)
        client = registry.make_client(cfg)
        if client is None:
            return _Outcome.SKIPPED, None
        client.timeout = UPSTREAM_TIMEOUT_S
        try:
            async with self._jellyfin:
                got = await client.primary_image(candidate.jellyfin_id, max_width=JELLYFIN_WIDTH)
        except JellyfinError as exc:
            # §3.3: Jellyfin down means the next source answers.
            log.info("jellyfin poster for %s unavailable: %s", candidate.jellyfin_id, exc)
            return _Outcome.ERROR, None
        if got is None:
            return _Outcome.MISSING, None
        data, declared = got
        content_type = acceptable(data, declared)
        if content_type is None:
            return _Outcome.MISSING, None
        return _Outcome.OK, (data, content_type)

    async def _from_host(self, candidate):
        if not self.egress:
            return _Outcome.SKIPPED, None
        try:
            response = await asyncio.wait_for(
                self._fetcher.get(candidate.url, max_attempts=2, timeout=UPSTREAM_TIMEOUT_S),
                UPSTREAM_DEADLINE_S,
            )
        except TimeoutError:
            return _Outcome.ERROR, None
        except FetchError as exc:
            if exc.status in (404, 410):
                return _Outcome.MISSING, None
            log.info("poster %s unavailable: %s", candidate.url, exc)
            return _Outcome.ERROR, None
        # Redirects may land on a host the allow-list never named; nothing from there is served.
        if not servable(response.url):
            log.warning("poster %s redirected off the allow-list to %s", candidate.url,
                        response.url)
            return _Outcome.MISSING, None
        content_type = acceptable(response.content, response.content_type)
        if content_type is None:
            return _Outcome.MISSING, None
        return _Outcome.OK, (response.content, content_type)


__all__ = ["Answer", "ArtService", "acceptable", "sniff", "url_epoch"]
