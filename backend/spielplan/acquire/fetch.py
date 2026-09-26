"""The polite per-host HTTP layer (§8, decision 340): pacing, backoff, breaker, robots.txt, User-Agent.

Redirects are followed here hop by hop, so each hop meets its host's policy and caller headers never
cross an origin. A non-idempotent request is re-sent only when it provably never arrived.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import asyncpg
import httpx

from spielplan.acquire.hosts import HostPolicy, normalise_host, policy_for

log = logging.getLogger("spielplan.acquire.fetch")

# 520-524 are Cloudflare's origin errors, which the scraped hosts return. Any other 4xx is an answer.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}

# Methods a retry may repeat (RFC 9110 §9.2.2). The one POST this app makes is a paid generation, so it
# is re-sent only via `CONNECT_PHASE` or `UNPROCESSED_STATUS` (decision 436 (1)).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})

# httpx raises these before any byte of the request is written.
CONNECT_PHASE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Statuses meaning the host did not do the work (RFC 9110, RFC 8470, RFC 6585).
UNPROCESSED_STATUS = frozenset({408, 425, 429})


def never_sent(exc: FetchError) -> bool:
    """Whether a failure from `get` provably put no request in front of the host: a breaker refusal, a
    `CONNECT_PHASE` error, or h11 refusing a header. `llm/client.py` may not import httpx to ask.
    """
    return isinstance(exc, HostPaused) or isinstance(
        exc.__cause__, (*CONNECT_PHASE, httpx.LocalProtocolError, httpx.UnsupportedProtocol))

# The statuses `get` follows itself, one hop at a time, so each hop meets its own host's policy.
REDIRECT_STATUS = {301, 302, 303, 307, 308}

# httpx's default 20 is for browsers. Exceeding the cap refuses rather than returning a 3xx body.
MAX_REDIRECTS = 5

# Decision 340: names the app, no contact address. `urllib.robotparser` matches on the token before
# the slash. Version matches `connectors/jellyfin.py`'s CLIENT_VERSION.
USER_AGENT = "Spielplan/1.0 (household media graph; one household, non-commercial)"

# A day, the interval published crawler guidance names.
ROBOTS_TTL_SECONDS = 24 * 3600

# robots.txt is fetched before the host's rules are known.
ROBOTS_TIMEOUT_S = 15.0

# RFC 9309 §2.5's parsing limit. Cut at a line boundary in `_truncate_robots`, since a mid-line cut
# relaxes a `Disallow`.
ROBOTS_MAX_BYTES = 500 * 1024


class FetchError(Exception):
    """Every failure this layer raises, so a caller can catch one type; `retryable` is what the driver
    reads.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = True, url: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.url = url


class HostPaused(FetchError):
    """The circuit breaker is open for this host.

    Retryable, and raised without sleeping: hand the task back rather than hold the loop.
    """

    def __init__(self, host: str, until: float, remaining: float):
        super().__init__(f"host {host} paused for {remaining:.0f}s", retryable=True)
        self.host = host
        self.until = until
        self.remaining = remaining


class RobotsDisallowed(FetchError):
    """The one failure a retry loop must never treat as transient. Decision 340."""

    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows {url}", retryable=False, url=url)


class RobotsUnavailable(FetchError):
    """The host's robots.txt was unreachable (5xx or transport failure), so assume disallow (RFC 9309
    §2.3.1.4).

    Retryable and never cached; a 4xx is not this (it means no rules, allow all).
    """

    def __init__(self, host: str, url: str, status: int):
        super().__init__(
            f"robots.txt for {host} is unreadable (HTTP {status or 'no response'}), so this "
            "fetcher does not assume it is allowed to crawl",
            status=status or None, retryable=True, url=url,
        )
        self.host = host


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: Mapping[str, str]
    # A 304: the stored bytes are current; the caller re-reads the raw store.
    from_cache: bool = False
    elapsed: float = 0.0
    # The url the validators were read under, including the query; callers must store under this, not
    # `url` (the last redirect hop), or conditional re-fetching silently stops.
    request_url: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        import json as _json
        return _json.loads(self.text)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


# The corpus's "have I got a token" test compares at full precision and can livelock: the refill
# lands ~1e-14 short and the wait becomes unrepresentable. A nanosecond of slack ends it.
_TOKEN_EPSILON = 1e-9


class TokenBucket:
    """Per-host pacing. The corpus's arithmetic, with the clock and the sleeper injected.

    Capped at `burst` so an idle hour is not a thundering herd; jittered so waiters do not collide.
    """

    def __init__(self, rps: float, burst: int, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[float, float], float] = random.uniform):
        self.rps = max(rps, 0.01)
        self.capacity = max(burst, 1)
        self.tokens = float(self.capacity)
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self.updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rps)
                self.updated = now
                if self.tokens >= 1.0 - _TOKEN_EPSILON:
                    # The epsilon's other half: forgive the remainder rather than carry it as a debt.
                    self.tokens = max(0.0, self.tokens - 1.0)
                    return
                need = (1.0 - self.tokens) / self.rps
                await self._sleep(need + self._jitter(0, 0.08))


@dataclass
class HostRuntime:
    """One host's in-process state. Everything here is rebuilt on a restart except what
    `fetch_host_state` holds, which is the robots answer and the breaker's refusal.
    """

    policy: HostPolicy
    bucket: TokenBucket
    sem: asyncio.Semaphore
    consecutive_failures: int = 0
    paused_until: float = 0.0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_checked: bool = False
    # Unreachable robots.txt this drain, so one request per drain rather than per url.
    robots_unavailable: int = 0
    requests: int = 0
    errors: int = 0
    # What `persist_host_state` already wrote, so it writes deltas and `host_report()` stays valid.
    flushed_requests: int = 0
    flushed_errors: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Fetcher:
    """One instance per drain; shared by every stage that reaches the network in that drain."""

    def __init__(
        self,
        *,
        conn: asyncpg.Connection | None = None,
        jellyfin_host: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.conn = conn
        self.jellyfin_host = jellyfin_host
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._transport = transport
        self._hosts: dict[str, HostRuntime] = {}
        # `_runtime` awaits Postgres between miss and insert; without the lock two buckets could pace a
        # host at twice its rate.
        self._hosts_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self.total_requests = 0
        self.total_errors = 0
        self.total_bytes = 0

    async def __aenter__(self) -> Fetcher:
        # `follow_redirects=False`: httpx's own hops would skip robots, policy, bucket and breaker. `get`
        # drives the hops.
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=15.0, read=45.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self.persist_host_state()

    # -- host bookkeeping ---------------------------------------------------------------

    async def _runtime(self, host: str) -> HostRuntime:
        rt = self._hosts.get(host)
        if rt is not None:
            return rt
        async with self._hosts_lock:
            rt = self._hosts.get(host)
            if rt is not None:
                return rt
            policy = policy_for(host, jellyfin_host=self.jellyfin_host)
            rt = HostRuntime(
                policy=policy,
                bucket=TokenBucket(policy.rps, policy.burst, clock=self._clock,
                                   sleep=self._sleep, jitter=self._jitter),
                sem=asyncio.Semaphore(policy.max_concurrency),
            )
            self._hosts[host] = rt
            if self.conn is not None:
                await self._load_host_state(host, rt)
            return rt

    async def _load_host_state(self, host: str, rt: HostRuntime) -> None:
        """Seed this drain's runtime from what the last one left behind.

        Intervals against the database's `now()`, so no wall-clock/monotonic reconciliation is needed.
        """
        row = await self.conn.fetchrow(
            "SELECT robots_txt, robots_status, consecutive_failures, "
            "       EXTRACT(EPOCH FROM (now() - robots_fetched_at)) AS robots_age_s, "
            "       EXTRACT(EPOCH FROM (paused_until - now())) AS paused_remaining_s "
            "  FROM fetch_host_state WHERE host = $1",
            host,
        )
        if row is None:
            return
        remaining = row["paused_remaining_s"]
        if remaining is not None and float(remaining) > 0:
            rt.paused_until = self._clock() + float(remaining)
        # Continue the host's failure run across restarts, so the breaker can trip across drains.
        rt.consecutive_failures = int(row["consecutive_failures"] or 0)
        age = row["robots_age_s"]
        # Only a real robots answer counts as cache, and a negative age (future timestamp) is not fresh.
        if age is not None and 0 <= float(age) < ROBOTS_TTL_SECONDS and _is_robots_answer(
            row["robots_status"]
        ):
            rt.robots = _parse_robots(row["robots_txt"] or "", row["robots_status"])
            rt.robots_checked = True
            # Honour `Crawl-delay` from the cache too, not only on a fresh fetch.
            self._pace_from_robots(host, rt)

    def _pace_from_robots(self, host: str, rt: HostRuntime) -> None:
        """Take the host's own published rate when it is stricter than the one this app declared.

        Only ever lowers the rate (burst goes to 1); never clamped, even if it outlasts the drain's
        budget.
        The rate in use is reported as `effective_rps`.
        """
        if rt.robots is None:
            return
        asked = _requested_rps(rt.robots)
        if asked is None or asked >= rt.policy.rps:
            return
        # Carry at most one token, so the first page still waits the published delay.
        carried = min(rt.bucket.tokens, 1.0)
        rt.bucket = TokenBucket(asked, 1, clock=self._clock, sleep=self._sleep,
                                jitter=self._jitter)
        rt.bucket.tokens = carried
        log.info(
            "robots.txt for %s asks for at most %.4f requests a second; pacing this host at its "
            "own rate rather than the declared %.4f", host, asked, rt.policy.rps,
        )

    async def _check_robots(self, host: str, rt: HostRuntime, url: str, scheme: str) -> None:
        if not rt.policy.respect_robots:
            return
        if scheme != "https":
            # RFC 9309 scopes robots.txt to scheme+host but this layer keys it by host, so a
            # robots-respecting
            # host is https-only. Hosts with robots off (the household Jellyfin) are exempt.
            raise FetchError(
                f"this fetcher reads robots.txt per authority and {host} was reached over "
                f"{scheme}: a host's http and https sites publish different files (RFC 9309 "
                "section 2.3), so a host whose robots.txt is honoured is fetched over https only",
                retryable=False, url=url,
            )
        async with rt.lock:
            if not rt.robots_checked:
                rt.robots_checked = True
                rt.robots, rt.robots_unavailable = await self._fetch_robots(host, rt, scheme)
                self._pace_from_robots(host, rt)
        if rt.robots is None:
            raise RobotsUnavailable(host, url, rt.robots_unavailable)
        if not rt.robots.can_fetch(USER_AGENT, url):
            raise RobotsDisallowed(url)

    async def _fetch_robots(
        self, host: str, rt: HostRuntime, scheme: str
    ) -> tuple[urllib.robotparser.RobotFileParser | None, int]:
        """Read this host's robots.txt. None means the host did not answer at all.

        A 2xx or 4xx is an answer, cached (a 4xx means allow all). A 5xx, transport failure, 3xx or
        damaged
        body is a non-answer: never cached, never read as permission.
        """
        assert self._client is not None
        robots_url = f"{scheme}://{host}/robots.txt"
        body, status = "", 0
        # Paced like any other request to this host; the breaker was already consulted.
        await rt.bucket.acquire()
        # Counted on both counters; not fed to the breaker.
        self.total_requests += 1
        rt.requests += 1
        chunks: list[bytes] = []
        try:
            # Streamed, stopping at the first chunk past the limit (a small gzip can inflate enormously).
            # Not
            # sliced exactly, so `_truncate_robots` still cuts at a line boundary.
            async with self._client.stream("GET", robots_url, timeout=ROBOTS_TIMEOUT_S) as resp:
                status = resp.status_code
                if 200 <= status < 300:
                    read = 0
                    async for part in resp.aiter_bytes():
                        chunks.append(part)
                        read += len(part)
                        if read >= ROBOTS_MAX_BYTES:
                            break
            # The whole 2xx class, truncated at RFC 9309 §2.5's limit before parsing or storing.
            raw = b"".join(chunks).decode("utf-8", "replace") if 200 <= status < 300 else ""
            body = _truncate_robots(raw)
            if raw and not body:
                # A first line longer than the limit leaves nothing: treat it as unreachable, not as an
                # empty file
                # (which would read as allow-all).
                log.info("robots.txt for %s is one line past the parsing limit, so this app read "
                         "no rule out of it; refusing this host for now", host)
                status = 0
        except Exception as exc:
            # A failure while reading the body means unreachable, even though headers said 200.
            status = 0
            log.info("robots.txt unreadable for %s (%s); refusing this host for now", host, exc)
        if not _is_robots_answer(status):
            rt.errors += 1
            self.total_errors += 1
            if status:
                log.info("robots.txt for %s answered HTTP %d; refusing this host for now",
                         host, status)
            return None, status
        if self.conn is not None:
            await self.conn.execute(
                "INSERT INTO fetch_host_state (host, robots_txt, robots_status, robots_fetched_at) "
                "VALUES ($1, $2, $3, now()) "
                "ON CONFLICT (host) DO UPDATE SET robots_txt = excluded.robots_txt, "
                "  robots_status = excluded.robots_status, "
                "  robots_fetched_at = excluded.robots_fetched_at",
                host, body, status,
            )
        return _parse_robots(body, status), status

    # `requests` counts everything that went on the wire; `errors` is a subset of it.
    def _note_success(self, rt: HostRuntime) -> None:
        rt.consecutive_failures = 0
        rt.requests += 1
        self.total_requests += 1

    async def _note_failure(self, host: str, rt: HostRuntime) -> None:
        rt.consecutive_failures += 1
        rt.requests += 1
        self.total_requests += 1
        rt.errors += 1
        self.total_errors += 1
        if rt.consecutive_failures >= rt.policy.breaker_threshold:
            rt.paused_until = self._clock() + rt.policy.breaker_cooldown_s
            rt.consecutive_failures = 0
            log.warning(
                "circuit breaker opened for %s; paused %.0fs after %d consecutive failures",
                host, rt.policy.breaker_cooldown_s, rt.policy.breaker_threshold,
            )
            if self.conn is not None:
                await self.conn.execute(
                    "INSERT INTO fetch_host_state (host, consecutive_failures, paused_until, "
                    "                              last_request_at) "
                    "VALUES ($1, 0, now() + make_interval(secs => $2::double precision), now()) "
                    "ON CONFLICT (host) DO UPDATE SET consecutive_failures = 0, "
                    "  paused_until = excluded.paused_until, "
                    "  last_request_at = excluded.last_request_at",
                    host, float(rt.policy.breaker_cooldown_s),
                )

    def _raise_if_paused(self, host: str, rt: HostRuntime) -> None:
        remaining = rt.paused_until - self._clock()
        if remaining > 0:
            raise HostPaused(host, rt.paused_until, remaining)

    async def persist_host_state(self) -> None:
        """Flush this drain's per-host counters into `fetch_host_state`, once.

        Writes the delta since the last flush; failures are logged, not raised. `consecutive_failures` is
        written absolutely: it is the host's current run.
        """
        if self.conn is None:
            return
        for host, rt in sorted(self._hosts.items()):
            requests = rt.requests - rt.flushed_requests
            errors = rt.errors - rt.flushed_errors
            if not requests and not errors:
                continue
            # Marked before the write, so a failed write is not counted twice.
            rt.flushed_requests, rt.flushed_errors = rt.requests, rt.errors
            try:
                await self.conn.execute(
                    "INSERT INTO fetch_host_state (host, requests, errors, consecutive_failures, "
                    "                              last_request_at) "
                    "VALUES ($1, $2, $3, $4, now()) "
                    "ON CONFLICT (host) DO UPDATE SET "
                    "  requests = fetch_host_state.requests + excluded.requests, "
                    "  errors = fetch_host_state.errors + excluded.errors, "
                    "  consecutive_failures = excluded.consecutive_failures, "
                    "  last_request_at = excluded.last_request_at",
                    host, requests, errors, rt.consecutive_failures,
                )
            except Exception as exc:
                log.warning("could not persist host counters for %s: %s", host, exc)

    # -- the request --------------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        max_attempts: int = 4,
        allow_status: tuple[int, ...] = (),
        conditional: bool = False,
        json_body: Any = None,
        method: str = "GET",
        timeout: float | None = None,
    ) -> Response:
        """One request, paced, gated and retried - with every hop of it treated as its own host.

        `allow_status` only lets the caller read that status's body once attempts are spent; it never
        skips the pause or the breaker count.
        """
        assert self._client is not None, "use `async with Fetcher() as f`"
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        host = normalise_host(parsed.netloc, scheme=scheme)
        rt = await self._runtime(host)
        # The url actually requested, query included: the validators' key and what robots judges.
        request_url = str(httpx.URL(url, params=params)) if params else url

        self._raise_if_paused(host, rt)
        await self._check_robots(host, rt, request_url, scheme)

        req_headers = dict(headers or {})
        if any(key.lower() == "user-agent" for key in req_headers):
            # Decision 340: the declared User-Agent may not be overridden per request.
            raise ValueError(
                "a caller may not replace this fetcher's User-Agent: spec section 8 says the app "
                "declares one that names it, and the robots decision is taken against that name"
            )
        conditioned = False
        if conditional and self.conn is not None:
            etag, last_modified = await self._validators(request_url)
            if etag:
                req_headers["If-None-Match"] = etag
            if last_modified:
                req_headers["If-Modified-Since"] = last_modified
            conditioned = bool(etag or last_modified)

        last_exc: Exception | None = None
        # A request that may not be repeated gets one send unless it provably never landed.
        repeatable = method.upper() in IDEMPOTENT_METHODS
        for attempt in range(1, max_attempts + 1):
            hop_url, hop_host, hop_rt, hop_scheme = url, host, rt, scheme
            hop_headers, hop_method, hop_json, hop_params = dict(req_headers), method, json_body, params
            hop_conditioned, hops, started = conditioned, 0, self._clock()
            resp: httpx.Response | None = None
            while True:
                # Re-checked each attempt: another task may have opened the breaker.
                self._raise_if_paused(hop_host, hop_rt)
                await hop_rt.bucket.acquire()
                started = self._clock()
                try:
                    async with hop_rt.sem:
                        resp = await self._client.request(
                            hop_method, hop_url, headers=hop_headers, params=hop_params,
                            json=hop_json,
                            timeout=(httpx.Timeout(timeout, connect=15.0)
                                     if timeout else httpx.USE_CLIENT_DEFAULT),
                        )
                except httpx.RequestError as exc:
                    # `RequestError`, not `TransportError`: redirect loops and bad gzip must be
                    # `FetchError`s and feed
                    # the breaker. `InvalidURL`/`StreamError` stay caller bugs.
                    last_exc = exc
                    await self._note_failure(hop_host, hop_rt)
                    resp = None
                    break

                location = resp.headers.get("location", "") if (
                    resp.status_code in REDIRECT_STATUS) else ""
                if not location:
                    break

                # A hop is an answer from this host and a request to the next.
                self._note_success(hop_rt)
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise FetchError(
                        f"more than {MAX_REDIRECTS} redirects from {url}",
                        status=resp.status_code, url=url,
                    )
                from_scheme, from_host = hop_scheme, hop_host
                hop_url = str(httpx.URL(hop_url).join(location))
                hop_parsed = urlparse(hop_url)
                hop_scheme = hop_parsed.scheme or scheme
                hop_host = normalise_host(hop_parsed.netloc, scheme=hop_scheme)
                hop_rt = await self._runtime(hop_host)
                # Breaker before robots, as on entry, so a paused host is not asked for robots.txt.
                self._raise_if_paused(hop_host, hop_rt)
                await self._check_robots(hop_host, hop_rt, hop_url, hop_scheme)
                # The query is in the Location. Validators are dropped (they belong to the original url).
                # 303, and
                # 301/302 on a POST, become GET.
                hop_params = None
                if not _same_origin(from_scheme, from_host, hop_scheme, hop_host):
                    # Caller headers do not cross an origin (credentials); the client's defaults remain.
                    hop_headers = {}
                hop_headers.pop("If-None-Match", None)
                hop_headers.pop("If-Modified-Since", None)
                hop_conditioned = False
                if resp.status_code == 303 or (
                    resp.status_code in (301, 302) and hop_method.upper() == "POST"
                ):
                    hop_method, hop_json = "GET", None

            if resp is None:
                if attempt >= max_attempts or not (repeatable or isinstance(last_exc, CONNECT_PHASE)):
                    raise FetchError(
                        f"{type(last_exc).__name__}: {last_exc}", url=url
                    ) from last_exc
                await self._sleep(_backoff(attempt, self._jitter))
                continue

            elapsed = self._clock() - started
            status = resp.status_code

            if status == 304 and hop_conditioned:
                # The stored bytes are still current.
                self._note_success(hop_rt)
                return Response(str(resp.url), 304, b"", resp.headers, from_cache=True,
                                elapsed=elapsed, request_url=request_url)

            if 300 <= status < 400 and status not in allow_status:
                # Every 3xx is followed or refused, never returned as an answer. Retryable.
                self._note_success(hop_rt)
                raise FetchError(
                    f"HTTP {status} with no usable redirect", status=status, url=url
                )

            if status in RETRYABLE_STATUS:
                await self._note_failure(hop_host, hop_rt)
                if attempt >= max_attempts or not (repeatable or status in UNPROCESSED_STATUS):
                    if status not in allow_status:
                        raise FetchError(f"HTTP {status}", status=status, url=url)
                    # The caller named this status as readable; the pauses and breaker count already
                    # happened.
                    content = resp.content
                    self.total_bytes += len(content)
                    return Response(str(resp.url), status, content, resp.headers,
                                    elapsed=elapsed, request_url=request_url)
                delay = _retry_after(resp.headers) or _backoff(attempt, self._jitter)
                if status == 429:
                    # Never shortened: the longer of the host's ask and three backoffs.
                    delay = max(delay, _backoff(attempt, self._jitter) * 3)
                await self._sleep(delay)
                continue

            if status >= 400 and status not in allow_status:
                # A 4xx outside the retry set is an answer and not the host's fault; any 5xx counts toward
                # the
                # breaker (e.g. Anthropic's 529).
                if status >= 500:
                    await self._note_failure(hop_host, hop_rt)
                else:
                    self._note_success(hop_rt)
                raise FetchError(f"HTTP {status}", status=status, retryable=False, url=url)

            self._note_success(hop_rt)
            content = resp.content
            self.total_bytes += len(content)
            return Response(str(resp.url), status, content, resp.headers,
                            elapsed=elapsed, request_url=request_url)

        raise FetchError(f"exhausted retries: {last_exc}", url=url)

    async def _validators(self, url: str) -> tuple[str | None, str | None]:
        """The ETag and Last-Modified this app last saw at this url, off `raw_document`.

        Newest successful row, `id DESC` as tiebreaker (rows in one transaction share `now()`). Keyed on
        the url including the query.
        """
        row = await self.conn.fetchrow(
            "SELECT etag, last_modified FROM raw_document "
            " WHERE url = $1 AND ok ORDER BY fetched_at DESC, id DESC LIMIT 1",
            url,
        )
        if row is None:
            return None, None
        return row["etag"], row["last_modified"]

    # -- reporting ----------------------------------------------------------------------

    def host_report(self) -> list[dict[str, Any]]:
        """What this drain did, per host, for §6.6 and for the drain's own job detail.

        Valid after the context closes. `rps` is the configured ceiling, `effective_rps` the rate in use.
        """
        now = self._clock()
        return [
            {
                "host": host,
                "requests": rt.requests,
                "errors": rt.errors,
                "paused_for": max(0.0, rt.paused_until - now),
                "rps": rt.policy.rps,
                "effective_rps": rt.bucket.rps,
            }
            for host, rt in sorted(self._hosts.items())
        ]


def _same_origin(from_scheme: str, from_host: str, to_scheme: str, to_host: str) -> bool:
    """Is this hop staying with the party the caller's headers were meant for?

    httpx's rule: same scheme, host and port, except a host upgrading its own http to https.
    """
    if from_host != to_host:
        return False
    return from_scheme == to_scheme or (from_scheme == "http" and to_scheme == "https")


def _is_robots_answer(status: int | None) -> bool:
    """Did the host actually answer the robots.txt request? RFC 9309 §2.3.1.

    2xx is a file; 4xx means no rules (allow all); 3xx, 5xx and status 0 are non-answers, never cached.
    """
    if status is None:
        return False
    status = int(status)
    return 200 <= status < 300 or 400 <= status < 500


def _parse_robots(body: str, status: int | None) -> urllib.robotparser.RobotFileParser:
    """A parser over this body, or an allow-all one for a host that published no rules.

    Only called with an answer. Any 2xx is parsed; a 4xx parses as empty (allow all).
    """
    served = status is not None and 200 <= int(status) < 300
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines() if served else [])
    return parser


def _truncate_robots(body: str) -> str:
    """At most `ROBOTS_MAX_BYTES` of a host's robots.txt, cut between lines.

    Measured in bytes (RFC 9309 §2.5 is in KiB); the character count may only short-circuit a
    rejection, never an acceptance.
    """
    if len(body) <= ROBOTS_MAX_BYTES and len(body.encode("utf-8", "replace")) <= ROBOTS_MAX_BYTES:
        return body
    kept: list[str] = []
    size = 0
    for line in body.splitlines():
        size += len(line.encode("utf-8", "replace")) + 1
        if size > ROBOTS_MAX_BYTES:
            break
        kept.append(line)
    return "\n".join(kept)


def _requested_rps(parser: urllib.robotparser.RobotFileParser) -> float | None:
    """The rate this host asked THIS agent to crawl at in its robots.txt, or None if it did not.

    The stricter of `Crawl-delay` and `Request-rate`, looked up against `USER_AGENT`.
    """
    rates: list[float] = []
    delay = parser.crawl_delay(USER_AGENT)
    if delay and float(delay) > 0:
        rates.append(1.0 / float(delay))
    rate = parser.request_rate(USER_AGENT)
    if rate is not None and rate.requests > 0 and rate.seconds > 0:
        rates.append(float(rate.requests) / float(rate.seconds))
    return min(rates) if rates else None


def _backoff(attempt: int, jitter: Callable[[float, float], float] = random.uniform) -> float:
    """The corpus's curve: 1.6s, 3.2s, 6.4s ... capped at a minute, spread +/-30 percent."""
    return min(60.0, (2 ** attempt) * 0.8) * jitter(0.7, 1.3)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in either form RFC 9110 §10.2.3 permits, clamped to [0, 300] seconds.

    Unparseable is None, sending the caller to the backoff curve.
    """
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        # Clamped at zero here too: a negative or `nan` value would skip the pause or raise.
        return min(max(0.0, float(raw)), 300.0)
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # RFC 9110 dates are GMT, never local time.
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return min(max(0.0, (deadline - datetime.now(UTC)).total_seconds()), 300.0)
