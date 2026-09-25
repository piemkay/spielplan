"""A title's poster, answered from the cache or fetched once. Spec v2.1 §6.8, §8's politeness
clause (decision 340); decisions 483, 484 and 485.

`ArtService` is process-lifetime, held on `app.state.art` from the lifespan, and it is where three
promises decision 483 makes about the route are kept:

  * **No pooled connection across upstream I/O.** The web pool holds ten connections and Home asks
    for sixty posters at once, so every database read here happens inside a `connect()` the caller
    hands in - `api/deps.brief_connection`, which releases on exit - and nothing awaits Jellyfin or
    TMDB while one is held. A route that fetched on a held connection would answer every other
    surface 503 while a cold Home filled.
  * **One upstream request per title, however many phones ask.** Concurrent misses for one title
    await one shielded task, so the second phone neither doubles the host's load nor cancels the
    first phone's fetch by closing its tab.
  * **One bucket per third-party host** (decision 485). `image.tmdb.org` and `static.tvmaze.com`
    are reached by this process and by no other - the worker's §8 stage 2 and decision 484's
    lookup call `api.themoviedb.org`, never the image CDN - so a single `acquire.fetch.Fetcher`
    held for the process's life is the one token bucket, semaphore and breaker each host has, at
    the rate `acquire/hosts.py` declares. That is the Fetcher used outside its "one instance per
    drain": this process has no drains, and the unit of pacing a host sees is the process.

The household's own Jellyfin is §8's exemption and goes through §7.1's client, bounded only by
`JELLYFIN_POLICY`'s concurrency so a cold Home does not ask the media server to resize sixty
images at once.
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

# How long each answer is trusted on the server, decision 483's numbers. TMDB and TVmaze files
# are re-fetched after 180 days; the household's Jellyfin after seven, because its art is the one
# an admin changes on purpose. A third-party file served because the preferred source ERRORED is
# kept a day, so Jellyfin is asked again tomorrow rather than in half a year.
THIRD_PARTY_TTL = 180 * DAY
JELLYFIN_TTL = 7 * DAY
STAND_IN_TTL = 1 * DAY
# The negative answers: every source said it holds no image (a week), or a host did not answer
# (ten minutes - long enough that a phone scrolling a shelf is not a request storm, short enough
# that a blip is not a day of tinted panels).
MISSING_TTL = 7 * DAY
ERROR_TTL = 600.0

# What the browser may keep, which is not the server's TTL. A 200 is decision 483's
# `private, max-age=15552000` - the 180 days of decision 178, `private` so no edge cache holds a
# session-gated answer, and never `immutable` because the URL names a title and not a file. A
# 404 is cacheable too, by the same decision: every card draws its `<img>` whether or not a poster
# is known (decision 484 may find one), and an uncached 404 would re-ask for every posterless card
# on every render, each ask a session read.
BROWSER_MAX_AGE = 15552000
BROWSER_NONE = 86400
# A lookup is filed and the worker's `art-lookup` runs every half hour, so the tinted panel is
# asked about again after one run rather than tomorrow.
BROWSER_PENDING = 1800
BROWSER_TRANSIENT = 600

# The formats a phone decodes and that nothing executes, told apart by their first bytes rather
# than by the host's Content-Type: the bytes leave this origin under this app's name, so what they
# ARE is checked here, and the response carries `nosniff` so a browser believes the check.
MAX_BYTES = 2 * 1024 * 1024
JELLYFIN_WIDTH = 342
# Seconds for one third-party answer, retries and a `Retry-After` included. The fetcher will sleep
# a 429's `Retry-After` in process, which is right in a drain and wrong under a waiting `<img>`.
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
    """The version an app-minted title's poster URL carries: a digest of this database's birth.

    Decision 483 lets a browser keep a 200 for 180 days on a URL that names only the title id, and
    within that window it never asks again, so the ETag is never read. A corpus id names one title
    in every database seeded from the corpus. An app-minted id does not: a re-seed is a fresh
    database, `position_id_sequences` restarts `title_id_seq` at `APP_ID_MIN`, and the drain mints
    1000000000 onward again in whatever order intake and retries reach the Jellyfin items - so a
    phone kept showing the previous database's poster under another title's name. The URL is
    therefore versioned by what changes exactly when an id can be handed out again (the first
    migration's time), and the 200 keeps decision 483's header. A restore keeps the rows, and the
    digest with them. `art.js` appends it to app-minted ids only; the route ignores it.
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
        # Decision 483's no-egress switch: e2e and CI run with it off, so a harness run makes no
        # internet image fetch whatever poster paths its fixture bundle carries. The household's
        # Jellyfin is not the internet (§8) and is asked either way.
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
        # Shielded: the phone that asked first may close its tab, and the phones waiting on the
        # same title must not lose the fetch with it.
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
                # A full or read-only /data/cache costs the next view a fetch, not this one its
                # poster: the bytes are in hand and already checked.
                log.warning("poster for title %s not cached: %s", title_id, exc)
                etag = None
            return Answer(200, BROWSER_MAX_AGE, data, content_type, etag)

        if all(o == _Outcome.SKIPPED for o in outcomes):
            # Nothing was asked - egress off, Jellyfin unconfigured - so there is no answer to
            # remember, only one to give.
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
            # §3.3: the app works when Jellyfin is down, and a poster is the smallest part of
            # that - the next source answers instead.
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
        # The fetcher follows redirects itself, each hop under its own host's policy, so the answer
        # can come from a host the allow-list never named. Nothing from there is stored or served:
        # "IMDb-hosted art is never fetched, proxied or stored" has to survive a CDN's 302 too.
        if not servable(response.url):
            log.warning("poster %s redirected off the allow-list to %s", candidate.url,
                        response.url)
            return _Outcome.MISSING, None
        content_type = acceptable(response.content, response.content_type)
        if content_type is None:
            return _Outcome.MISSING, None
        return _Outcome.OK, (response.content, content_type)


__all__ = ["Answer", "ArtService", "acceptable", "sniff", "url_epoch"]
