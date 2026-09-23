"""The polite per-host HTTP layer. Spec v2.1 §8, decision 340.

§8 names this one of the three things ported rather than invented - "per-host rate-limited HTTP
layer" (`spec:359`) - and decision 340 gave §8 the clause that says what "polite" has to mean, so
that a reviewer can fail the behaviour instead of debating it (`spec:404`): a declared User-Agent
naming the app, each host's `robots.txt` honoured and cached in Postgres, and per-host rate and
concurrency policies carried as data. The data is `acquire/hosts.py`; this is the machine that
obeys it.

**Port verdict: `mdc/http.py`, taken WITH NAMED CHANGES.** Four algorithms are verbatim and are
the reason to port rather than write: the token bucket's refill arithmetic, the exponential
backoff with jitter, the `Retry-After` rule on a 429, and the consecutive-failure breaker with its
per-host cooldown. Each is tuned against a real crawl of the same hosts, and each has a failure
mode that only shows up at scale - a bucket that refills past its burst hands a host a thundering
herd after an idle hour; a backoff without jitter re-synchronises every retrying task onto the
same second; a breaker counting cumulative rather than consecutive failures parks a merely flaky
host for ever. What changed, and why:

  1. **The persistence seam is asyncpg, not sqlite3.** The corpus passes a `sqlite3.Connection`
     and writes a separate `http_cache` table keyed on the url's hash. There is no such table
     here, deliberately: the validators a conditional request sends back are columns on
     `raw_document` (`0024_acquisition.sql`), because that row is already the record of what this
     app last saw at a url and a second table keyed on the same url would be a second answer to
     one question. So `_cache_store` is NOT ported at all - this layer never writes
     `raw_document`; `acquire/rawstore` owns that write - and `_cache_lookup` becomes one indexed
     read of the newest good row for the url.
  2. **The clock, the sleeper and the jitter are injected.** The corpus calls `time.monotonic`,
     `asyncio.sleep` and `random.uniform` directly, which makes its pacing arithmetic testable
     only by waiting for it. The numbers here are the thing worth asserting - a backoff that is
     silently three times too short is the defect this layer exists to prevent - so they are
     parameters with the corpus's own defaults.
  3. **robots.txt is cached in Postgres**, per decision 340, not only in the process. The corpus
     is a CLI whose process outlives the crawl; this runs inside §5.3's worker, which restarts,
     and re-fetching every host's robots.txt on every restart is itself impolite.
  4. **The breaker's refusal is persisted.** A worker killed after opening a breaker must not come
     back in twenty seconds and resume hammering the host that just refused it. `fetch_host_state`
     holds `paused_until`; the token bucket stays in memory, because what must survive a restart
     is the refusal and not the pacing (`0024_acquisition.sql` argues this at the table).
  5. **`http2=True` is dropped.** It needs the `h2` package, which this app does not depend on,
     and M5.1 adds no dependency (`backend/pyproject.toml` is another lane's file). httpx falls
     back to HTTP/1.1 for every host here anyway.
  6. **`cfg` becomes an explicit `jellyfin_host`.** The corpus reads its Jellyfin URL from a
     process-wide config object; this app keeps it in `connector_config` behind the registry, and
     reaching for it here would couple the fetcher to the connector layer and to the secrets
     boundary for one hostname comparison.
  7. **`upload()` and `download_file()` are not ported.** `upload` exists in the corpus for one
     endpoint - an LLM provider's batch API, which takes its input as a multipart file - and both
     the endpoint and the spend cap that gates it are M5.5's. `download_file` streams the IMDb and
     MovieLens bulk dumps, which this app receives in the export bundle rather than crawling.
     Porting either now would be a code path with no caller and no test.
  8. **Robots fetches are counted.** The corpus's counters skip them; they are outbound requests,
     and `total_requests` is what the exit criterion reads when it asserts that a re-parse costs
     nothing, so a counter that quietly omits a class of request is a counter that can lie.
  9. **One correction to the bucket**, argued at `_TOKEN_EPSILON` below: the corpus's "have I got
     a token yet" test compares at full float precision and can therefore never be satisfied,
     which is a livelock rather than a slow crawl. THE CLAMP ON THE SUBTRACTION IS THE OTHER HALF
     OF THAT ONE CORRECTION and not a second one, which this list used to imply by ending "the
     arithmetic is otherwise untouched": a grant taken at `1.0 - epsilon` leaves a remainder the
     corpus's `self.tokens -= 1.0` would carry forward as a debt, and `max(0.0, ...)` is what says
     the epsilon is forgiven rather than borrowed. Nothing else in `acquire` is changed.
     [M5.1 review cycle 2, port-BUCKET-01]
 10. **Redirects are followed by this module and not by httpx**, and this is the one place the
     port contradicts the corpus rather than extending it. `mdc/http.py:128` sets
     `follow_redirects=True`, which was harmless for a developer's CLI and is not harmless here:
     every hop httpx takes inside one `request` call skips the robots check, the declared policy,
     the bucket and the breaker, and is invisible to every counter. See `__aenter__`.
     **AND IT TAKES OVER WHAT httpx WAS ALSO DOING FOR THE CORPUS**, which this entry did not say
     and which is the exact shape of a port regression: httpx pops `Authorization` when a redirect
     changes origin and `Cookie` on every redirect at all (`_client.py:552-556`), so driving the
     hops here re-sent every header a caller passed to whatever host a `Location` named. An entry
     that enumerates what a change GAINS and is silent about what it loses is how that survived
     two review cycles. See the hop block in `get`.
     [M5.1 review cycle 3, M51-C3-340-01]
 11. **A robots.txt this app could not read refuses the request** rather than allowing it, and is
     not written to the cache. The corpus fails open in process for one crawl; persisting that
     non-answer, as change 3 did, turned one blip into a day of ignoring a host's rules. See
     `_fetch_robots`.
 12. **A host whose robots.txt is honoured is fetched over https only.** This layer files one
     robots answer per HOST - one field on `HostRuntime`, one row in `fetch_host_state`, and
     `normalise_host` collapses `http://x` and `https://x` onto one key - while RFC 9309 §2.3
     scopes a robots.txt to the URI AUTHORITY, which includes the scheme. The corpus has the same
     shape and never meets it, because every url it fetches is https; here the refusal says so
     rather than leaving which of a host's two published files is obeyed to be decided by the url
     a drain happened to lease first. A policy that turns robots OFF is exempt, which is what
     keeps the household's own Jellyfin on `http://box:8096` reachable through this layer. See
     `_check_robots`. [M5.1 review cycle 3, M51-C3-340-04]

Changes 9 to 11 and the widened `except` in `get` are review cycle 1's and 2's, change 12 is
review cycle 3's, and each is argued at the line it changes rather than only here.

**Everything is asyncio and nothing sleeps the thread.** This runs inside the worker's sequential
tick (§5.3, `worker.py`), where one blocking call stops every other job in the process.
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

# The corpus's set, verbatim. 408/425 are the client-timeout and too-early cases, 429 is the rate
# limit, 5xx are the server's own, and 520-524 are Cloudflare's origin-side family - which matters
# because the two HTML hosts §8 stage 2 names sit behind it and answer 520 rather than 502 when
# their origin is unhappy. Everything else in 4xx is a real answer and is raised at once: retrying
# a 404 is how a crawl turns one wrong url into four.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}

# The statuses `get` follows itself, one hop at a time, so each hop meets its own host's policy.
REDIRECT_STATUS = {301, 302, 303, 307, 308}

# How many hops one call may take. httpx's own default is 20, which is a number for a browser and
# not for an unattended appliance: every hop is a request on the household's IP, and a chain that
# needs more than a handful is a loop or a redirector, neither of which §8 stage 2 has any use
# for. The cap is a refusal rather than a silent stop, because a truncated chain returned as an
# answer is a 3xx body handed to a parser.
MAX_REDIRECTS = 5

# Decision 340: "It declares a User-Agent naming the app." It names the app, its version and what
# it is, and it carries no contact address on purpose - the corpus puts an operator's email here,
# which this app has nowhere to read from, and PUBLIC_URL is the household's own origin and is not
# a third party's business. `urllib.robotparser` matches on the token before the slash, so a
# robots.txt addressed to `User-agent: Spielplan` applies to this agent exactly as intended.
# The version matches `connectors/jellyfin.py`'s CLIENT_VERSION, the only other string this app
# already puts on the wire to identify itself.
USER_AGENT = "Spielplan/1.0 (household media graph; one household, non-commercial)"

# How long a cached robots.txt is honoured before it is re-read. A day is the interval every
# published crawler guidance names, and the trade is legible in both directions: shorter, and a
# host that publishes one robots.txt is asked for it again for every title the pipeline touches;
# longer, and a host that starts disallowing a path waits too long to be obeyed.
ROBOTS_TTL_SECONDS = 24 * 3600

# robots.txt is fetched before this host's RULES are known, and a short timeout is the bound on
# that: a host that will not answer this in fifteen seconds is a host whose crawl is about to fail
# anyway. This comment used to argue from the premise that robots.txt "is the one request that
# cannot be paced by the host's own bucket", which was false for this code: `get` builds `rt` -
# and its bucket - before it calls `_check_robots`, so the bucket is in hand and is now charged.
# [M5.1 review cycle 2, M51-C2-340-05]
ROBOTS_TIMEOUT_S = 15.0

# How much of a robots.txt this app reads. RFC 9309 §2.5 ("Limits") sets a crawler's parsing limit
# at "at least 500 kibibytes", which is the number every published crawler uses, and it is a bound
# this layer needs for its own reasons rather than for the standard's: this is the one request in
# the app that takes arbitrary bytes from a stranger BEFORE any of that host's policy applies, and
# what comes back is parsed inside §5.3's sequential tick and then written into a Postgres column
# that `_load_host_state` re-reads and re-parses on the first touch of that host in every drain for
# a day. Unbounded, a misconfigured or hostile host chooses how much of the household's database
# and worker tick it occupies. The same judgement `MAX_REDIRECTS` makes about httpx's default of
# twenty: a number for a browser is not a number for an unattended appliance.
#
# Cut at a LINE boundary, in `_truncate_robots` below, because a cut mid-line can turn a
# `Disallow: /private` into a `Disallow: /priv` or drop it entirely - a refusal silently relaxed,
# which is the direction review cycle 1 spent two findings closing.
# [M5.1 review cycle 2, M51-C2-340-06]
ROBOTS_MAX_BYTES = 500 * 1024


class FetchError(Exception):
    """Every failure this layer raises, so a caller can catch one type.

    `retryable` is the part that matters to the driver: §8 parks a job with a reason, and the
    difference between "ask again later" and "this answer will not change" is the difference
    between a park that resolves itself and one that burns a host's patience for ever.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = True, url: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.url = url


class HostPaused(FetchError):
    """The circuit breaker is open for this host.

    Retryable, and raised without sleeping: the cooldown is measured in minutes and the worker's
    tick in seconds, so the honest answer is to hand the task back rather than to hold the loop.
    """

    def __init__(self, host: str, until: float, remaining: float):
        super().__init__(f"host {host} paused for {remaining:.0f}s", retryable=True)
        self.host = host
        self.until = until
        self.remaining = remaining


class RobotsDisallowed(FetchError):
    """The one failure a retry loop must never treat as transient. Decision 340.

    Not retryable, because the answer will be the same tomorrow and the attempt itself is the
    impoliteness. A stage that catches this parks with the reason and does not schedule anything.
    """

    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows {url}", retryable=False, url=url)


class RobotsUnavailable(FetchError):
    """The host could not tell us its rules, so this layer does not guess that they permit us.

    RFC 9309 §2.3.1.4 is unambiguous about the unreachable case - a 5xx or a transport failure
    means the crawler "MUST assume complete disallow" - and §8's clause (`spec:404`) says this
    fetcher "honours each host's robots.txt". Reading a file the host never served as a blanket
    permission honours nothing; it is the most permissive possible reading of an answer that does
    not exist, taken on a household's own IP.

    RETRYABLE, and that is the whole reason this is not `RobotsDisallowed`. The counter-argument
    the old code made was sound about the exception it had: refusing a host over one outage
    "would let one outage park every task for that host as permanently refused - the one failure
    `RobotsDisallowed` promises is not transient". So this failure is the transient one, in
    `HostPaused`'s mould: it is not cached past the drain that met it, and the next drain asks
    the host again. A 4xx is NOT this - RFC 9309 §2.3.1.3 puts the whole 400-499
    range under "unavailable" and says a crawler may access any resource, which is what a host
    publishing no robots.txt means. [M5.1 review cycle 1, M51-340-03]

    NO STAGE PARKS ON IT, which this docstring used to say one did. At §8 stage 2 it is a
    best-effort source's note like the breaker's pause - decision 422 says why - and a title it
    leaves short at stage 4 is asked again when that window closes (decision 421).
    [M5.3 review cycle 2, M53-C2-NET-01]
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
    # A 304: the server says the bytes this app already stored are still current. There is no body
    # to hand back, and the caller re-reads the raw store rather than the network.
    from_cache: bool = False
    elapsed: float = 0.0
    # THE STRING THIS LAYER READ THE VALIDATORS UNDER, and the one a caller passes to
    # `rawstore.store(url=...)`. `url` above is the FINAL url - the last hop of a redirect chain,
    # with whatever query httpx built - and storing that is how a source silently loses conditional
    # re-fetching for ever: the next `get` keys `_validators` on the url it was ASKED for, finds no
    # row, and issues a full fetch with no error, no log and nothing in `total_bytes` to see it by.
    # The corpus could not make this mistake because one function held both sides on one variable
    # (`mdc/http.py:229` and `:289`); named change 1 moved the write to `acquire/rawstore` and left
    # two natural spellings, so the read key is CARRIED ON THE ANSWER rather than remembered.
    # [M5.1 review cycle 3, port-C3-01]
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


# The one correction to the corpus's bucket, and it is not cosmetic. `(now - updated) * rps` does
# not reproduce the wait that was computed to buy one token: a monotonic clock's origin is large,
# the subtraction cancels most of the significant digits, and the product lands a few parts in 1e14
# short - measured at rps 2.5, where a 0.4 second wait returns 0.9999999999999432 tokens. Compared
# at full precision that reads as "not yet", so the loop computes a 2e-14 second wait, a clock at
# that magnitude cannot represent an increment that small, and it asks again for ever. The corpus
# never sees it because `random.uniform(0, 0.08)` overshoots the shortfall on every call, so the
# defect stays invisible until something makes the pacing deterministic - which is exactly what a
# test of the pacing must do. It is worth fixing rather than testing around: a spin of zero-length
# sleeps inside §5.3's sequential tick is the failure `connectors/jellyfin.py`'s MAX_PAGES comment
# already records once, where one loop that never ended took every other job offline with the
# process still looking healthy. A nanosecond of slack is orders of magnitude below anything a host
# can measure, and it ends the argument.
_TOKEN_EPSILON = 1e-9


class TokenBucket:
    """Per-host pacing. The corpus's arithmetic, with the clock and the sleeper injected.

    Tokens accrue at `rps` and are capped at `burst`, which is the half that stops an idle hour
    from becoming a burst of three thousand requests the moment a stage wakes up. A caller that
    finds the bucket empty sleeps for exactly as long as one token takes to accrue, plus a little
    jitter so that two tasks queued behind the same host do not wake on the same millisecond and
    race for the one token between them.
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
                    # `max(0.0, ...)` is the epsilon's other half, and named change 9 says so:
                    # the comparison above grants a token at `1.0 - _TOKEN_EPSILON`, so the
                    # corpus's bare `self.tokens -= 1.0` would leave a negative remainder and
                    # carry that fraction of a token forward as a debt against the next grant.
                    # Forgiven rather than carried - a nanosecond of slack is the thing the
                    # epsilon already decided - and written down here because a reader comparing
                    # this loop with `mdc/http.py:92-94` finds two lines different and one of
                    # them declared. [M5.1 review cycle 2, port-BUCKET-01]
                    self.tokens = max(0.0, self.tokens - 1.0)
                    return
                need = (1.0 - self.tokens) / self.rps
                await self._sleep(need + self._jitter(0, 0.08))


@dataclass
class HostRuntime:
    """One host's in-process state. Everything here is rebuilt on a restart except what
    `fetch_host_state` holds, which is the robots answer and the breaker's refusal."""

    policy: HostPolicy
    bucket: TokenBucket
    sem: asyncio.Semaphore
    consecutive_failures: int = 0
    paused_until: float = 0.0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_checked: bool = False
    # The host was asked and did not answer (a 5xx, or nothing at all). Held for the drain so one
    # unreachable robots.txt costs one request rather than one per url, and so the refusal is the
    # same for every task in that drain. See `RobotsUnavailable`.
    robots_unavailable: int = 0
    requests: int = 0
    errors: int = 0
    # WHAT `persist_host_state` HAS ALREADY WRITTEN DOWN, so the pair above can stay a count of the
    # whole drain. The flush used to ZERO `requests`/`errors` as it wrote them, which made
    # `host_report()` - whose own docstring calls it "what this drain did, per host" - answer zeros
    # for every host the moment the `async with` block exited, while `fetch_host_state` held the
    # truth. A drain assembling its job detail after the block, which is the obvious shape and the
    # one M5.3's stage 2 will write, would publish "no requests to any host" on the same tick the
    # database recorded that there were. Kept as a high-water mark rather than by resetting, so
    # `persist_host_state`'s "a second call adds nothing" is the DELTA being zero and not the
    # counter being cleared. [M5.1 review cycle 3, port-C3-06]
    flushed_requests: int = 0
    flushed_errors: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Fetcher:
    """One instance per drain; shared by every stage that reaches the network in that drain.

    The corpus says "one instance per process" because its process is one crawl. Here the unit is
    the worker job: the per-host buckets and semaphores are what pace a drain, and they are
    rebuilt from `fetch_host_state` at the top of the next one.
    """

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
        # `_runtime` awaits Postgres between the miss and the insert, which the corpus's
        # synchronous lookup could not do. Two stages reaching the same host in the same drain
        # would otherwise each build a bucket, and a host would be paced at twice its declared
        # rate by exactly the machinery meant to pace it.
        self._hosts_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self.total_requests = 0
        self.total_errors = 0
        self.total_bytes = 0

    async def __aenter__(self) -> Fetcher:
        # `follow_redirects=False`, which is the one place this port deliberately contradicts
        # `mdc/http.py:128` rather than copying it. httpx following a redirect issues every hop
        # INSIDE `self._client.request`, with no callback: the target host's robots.txt is never
        # read, its declared `HostPolicy` is never applied, its bucket is never charged, its
        # breaker is never consulted, and `host_report()` attributes nothing to it at all. §8's
        # clause (`spec:404`) is about the requests this app puts on the wire, and a hop is one of
        # them - a same-host 302 into a `Disallow: /search/` path is enough to make the fetcher
        # refuse a url when asked for it and fetch the identical url when redirected to it. `get`
        # drives the hops itself so every one of them passes the same four gates.
        # [M5.1 review cycle 1, M51-340-02]
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

        Both reads are expressed as intervals against the database's `now()` rather than as
        timestamps, so nothing here has to reconcile Postgres's wall clock with the monotonic
        clock this layer paces itself by. A clock skew between the two would otherwise read as a
        breaker that never opens or one that never closes.
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
        # The run of failures this host is already on, so the breaker's threshold counts a host
        # that fails seven times a drain rather than resetting to zero on every worker restart.
        # The column existed and was only ever written as the literal zero, which made §6.6's
        # number a constant and the breaker a protection against a burst that fits inside one
        # drain. [M5.1 review cycle 1, M51-340-09]
        rt.consecutive_failures = int(row["consecutive_failures"] or 0)
        age = row["robots_age_s"]
        # `_is_robots_answer` and not merely the age: a cached row is only a rule this app may
        # honour if the host actually served one. `_fetch_robots` no longer writes anything else,
        # and the read refuses anything else too, so a row left by an older build - or by a hand
        # repair - cannot pin an allow-all the host never published.
        #
        # BOUNDED IN BOTH DIRECTIONS, which is what makes the sentence above true in the time
        # dimension as well. `age` is `now() - robots_fetched_at`, so a timestamp in the FUTURE is
        # a NEGATIVE age and satisfied `< ROBOTS_TTL_SECONDS` for as long as the clock took to
        # catch up - indefinitely, for a box whose RTC booted ahead before NTP pulled it back, or
        # for a dump restored from one that had. `rt.robots_checked` then suppresses every further
        # robots request, so the host is never asked again and any `Disallow` it publishes in the
        # meantime is not seen. The sibling read in this same function fails the safe way under
        # the same skew - `paused_remaining_s > 0` merely pauses the host longer - and this one
        # failed OPEN. A timestamp the database cannot have written is no cache.
        # [M5.1 review cycle 4, M51-C4-340-04]
        if age is not None and 0 <= float(age) < ROBOTS_TTL_SECONDS and _is_robots_answer(
            row["robots_status"]
        ):
            rt.robots = _parse_robots(row["robots_txt"] or "", row["robots_status"])
            rt.robots_checked = True
            # Here TOO and not only on a fresh fetch: the cached body is the same published rules,
            # and a rate honoured on the drain that read the file and forgotten on every drain that
            # read the cache would honour a host's `Crawl-delay` roughly once a day.
            # [M5.1 review cycle 2, M51-C2-340-03]
            self._pace_from_robots(host, rt)

    def _pace_from_robots(self, host: str, rt: HostRuntime) -> None:
        """Take the host's own published rate when it is stricter than the one this app declared.

        §8 (`spec:404`) says this fetcher "honours each host's robots.txt", and a host that wrote
        down how fast it wants to be crawled has honoured nothing if the app reads the file and
        then uses its own number. `hosts.py`'s row stays the CEILING - policies-as-data is intact,
        `declared_policies()` still shows what an operator configured - and the host can only ever
        move the rate DOWN. A robots.txt that asked to be crawled faster is not a permission this
        layer takes, because the declared row is a measurement of what that host survived.

        BURST GOES TO 1 WITH IT. A rate alone does not make the first two requests polite: an
        unmeasured host declares `burst = 2`, so a bucket that started full would put two requests
        back to back on a host that asked for thirty seconds between them, and only then begin
        pacing. `Crawl-delay` is a statement about the gap between requests, which is a burst of
        one by definition.

        NOT CLAMPED, AND THE INTERACTION IS WORTH WRITING DOWN. A host publishing an hour-long
        `Crawl-delay` makes the second request of a drain wait an hour, which §5.3's tick will not
        sit through: `worker.py`'s `_tick` cancels the drain at its budget and `pipeline.drain`'s
        cancellation arm hands the whole leased batch back with no attempt spent. That is the
        right failure - the task returns to the queue and the host is not crawled faster than it
        asked - and a cap here would instead be an undocumented override of a host's own file,
        which is the one thing §8 permits nobody. [M5.1 review cycle 2, M51-C2-340-03]

        `respect_robots=False` hosts never reach here, because `_check_robots` returns before the
        fetch and `_load_host_state` only parses a row the fetch wrote - so a documented override
        keeps its documented rate and `undocumented_overrides()` still sees every one of them.

        THE RATE THIS LEAVES IN USE IS PUBLISHED AS `effective_rps`, and a surface asking "how fast
        is this household crawling that host" must show that one. The clamp below is invisible
        everywhere else: `declared_policies()` is the configured table by design, `fetch_host_state`
        has no rate column, and `host_report()` published the ceiling beside three measured facts -
        fifteen times wrong for a host publishing `Crawl-delay: 30` against a declared 0.5, which is
        four of the rows this table calls "small sites run by individuals". A durable reader can
        derive the same number from the stored body (`_requested_rps(_parse_robots(...))`); what it
        may not do is read the declared figure and call it the rate.
        [M5.1 review cycle 3, port-C3-05]
        """
        if rt.robots is None:
            return
        asked = _requested_rps(rt.robots)
        if asked is None or asked >= rt.policy.rps:
            return
        # The credit this host has already given us, capped by what it now permits. A fresh bucket
        # starts FULL, and starting full here would hand back the burst the host's own file just
        # withdrew: on a `burst = 1` host the robots fetch has spent the only token, so the first
        # page must wait the published delay rather than going out back to back with it - which is
        # the effective burst of 2 the charge in `_fetch_robots` exists to remove. On a host whose
        # rules came out of the cache no request has been made yet, and the full bucket is carried
        # down to one token, which is what "one request, then wait" means.
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
            # RFC 9309 SECTION 2.3 SCOPES A ROBOTS.TXT TO THE URI AUTHORITY, WHICH INCLUDES THE
            # SCHEME, and this layer files one robots answer per HOST: `HostRuntime.robots` is one
            # field, `normalise_host` drops a port equal to the scheme's default so `http://x` and
            # `https://x` collapse to one key, and `fetch_host_state` is `host text PRIMARY KEY`
            # (`0024_acquisition.sql:179`). So a host's http and https sites are two published files
            # and this app read whichever url the drain happened to lease first and applied it to
            # both - nondeterministically, since it depends on lease order, and for
            # `ROBOTS_TTL_SECONDS` across worker restarts. It errs in both directions and the
            # impolite one is the one decision 340 exists to prevent: a `Disallow: /` published for
            # the plain-http site ignored for a day because an https url touched the host first.
            #
            # REFUSED RATHER THAN RE-KEYED, which is the smaller of the two repairs and the one that
            # needs no column in a migration that is already applied. Every url in this tree and in
            # the corpus M5.3 ports is https, so the rule costs nothing and states plainly what this
            # fetcher will do. Scoped to the hosts whose rules would be conflated and to no others:
            # a policy that turns robots OFF has no published file to confuse, which is what makes
            # the household's own Jellyfin on `http://192.168.1.10:8096` reachable through here.
            # [M5.1 review cycle 3, M51-C3-340-04]
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

        TWO OUTCOMES AND NOT ONE, which is the correction review cycle 1 made to this function.
        An ANSWER is cached and honoured: a 200 is a rule, and a 4xx - 404, 403, 401 alike - is
        RFC 9309 §2.3.1.3's "unavailable", which says a crawler may access any resource, so it
        parses as allow-all. A NON-ANSWER is a 5xx or a transport failure, RFC 9309 §2.3.1.4's
        "unreachable", where the crawler "MUST assume complete disallow" - and it is neither
        honoured as a permission nor written to the cache.

        BOTH HALVES OF THAT MATTER AND THE OLD CODE HAD NEITHER. It allowed the request, which is
        the most permissive reading of a file nobody served; and it wrote the non-answer into
        `fetch_host_state` with `robots_fetched_at = now()`, which `_load_host_state` then read
        back as a valid cache for `ROBOTS_TTL_SECONDS`. One DNS blip therefore suppressed a host's
        robots.txt for twenty-four hours across every worker restart, issuing no further robots
        request - and because the UPSERT is unconditional it also CLOBBERED a `Disallow: /` this
        app had already read and was obeying. That is decision 340's clause satisfied on paper and
        violated on the wire, invisible to `hosts.undocumented_overrides()` because it is not a
        declared override. [M5.1 review cycle 1, M51-340-01, M51-340-03, port-02]

        The cost of not caching a non-answer is one robots request per host per drain while a host
        is down, which is the right trade and is bounded: `rt.robots_checked` caps it at one per
        drain, and `_raise_if_paused` runs before `_check_robots` in `get`, so an open breaker
        suppresses even that.
        """
        assert self._client is not None
        robots_url = f"{scheme}://{host}/robots.txt"
        body, status = "", 0
        # PACED LIKE ANY OTHER REQUEST TO THIS HOST, which it did not used to be. `ROBOTS_TIMEOUT_S`
        # argued that this was "the one request that cannot be paced by the host's own bucket", and
        # the bucket is built by `_runtime` before `get` calls `_check_robots` at all - so the only
        # thing standing between a host and two back-to-back requests was that nobody charged it.
        # On the two hosts §8 stage 2 names by hand that turned a declared `burst=1` at 0.7 rps into
        # an effective burst of 2, on the first touch of every drain, for ever. The bound is one
        # request per host per drain (`rt.robots_checked`), which is a small number and not a reason
        # to exempt it: §8's clause (`spec:404`) is about the requests this app puts on the wire.
        # The breaker is consulted before this (`_raise_if_paused` runs first in `get`), so a paused
        # host is not even asked. [M5.1 review cycle 2, M51-C2-340-05]
        await rt.bucket.acquire()
        # Counted on BOTH counters. Change 8 in the module docstring argues that a robots fetch is
        # an outbound request and "a counter that quietly omits a class of request is a counter
        # that can lie" - and then incremented only the process-wide one, leaving `rt.requests`,
        # which is what `host_report()` publishes and `persist_host_state` flushes into the column
        # §6.6 reads, short by exactly that class. Not routed through `_note_success` /
        # `_note_failure`: a robots failure deliberately does not feed the breaker, because a host
        # that cannot serve one file is not a host refusing this app.
        # [M5.1 review cycle 1, port-07, M51-340-10]
        self.total_requests += 1
        rt.requests += 1
        chunks: list[bytes] = []
        try:
            # STREAMED, AND THE LIMIT IS ON THE WIRE RATHER THAN ONLY ON THE PARSE. `ROBOTS_MAX_BYTES`
            # argues that this is "the one request in the app that takes arbitrary bytes from a
            # stranger BEFORE any of that host's policy applies" and that unbounded "a misconfigured
            # or hostile host chooses how much of the household's database and worker tick it
            # occupies" - and then read the whole body with `self._client.get` and cut the string
            # afterwards, so the database was bounded and the tick was not. The client declares
            # `Accept-Encoding: gzip, deflate`, so the host chooses that allocation cheaply:
            # measured, a 240 KB gzip decompressed to 70 MB in the worker process and peaked a
            # quarter of a gigabyte of heap before `_truncate_robots` saw a character.
            # `ROBOTS_TIMEOUT_S` is an `httpx.Timeout`, which bounds each operation and never a
            # total, so nothing else was holding this.
            #
            # STOPPED AT THE LIMIT AND NOT SLICED TO IT: reading stops on the first chunk that
            # crosses `ROBOTS_MAX_BYTES`, so what is decoded is at most the limit plus one transport
            # chunk, and `_truncate_robots` below still has more than the limit to work with and so
            # still does its line-boundary job. Slicing the bytes to exactly the limit here would
            # hand it a string it reads as already short enough and leave a half-written `Disallow`
            # as the last rule - a refusal silently relaxed, which is the direction review cycle 1
            # spent two findings closing. [M5.1 review cycle 3, M51-C3-340-05]
            async with self._client.stream("GET", robots_url, timeout=ROBOTS_TIMEOUT_S) as resp:
                status = resp.status_code
                if 200 <= status < 300:
                    read = 0
                    async for part in resp.aiter_bytes():
                        chunks.append(part)
                        read += len(part)
                        if read >= ROBOTS_MAX_BYTES:
                            break
            # The whole 2xx class, for `_parse_robots`'s reason, and truncated at RFC 9309 §2.5's
            # limit before anything parses it or stores it.
            raw = b"".join(chunks).decode("utf-8", "replace") if 200 <= status < 300 else ""
            body = _truncate_robots(raw)
            if raw and not body:
                # A CUT THAT LEAVES NOTHING IS NOT A PUBLISHED EMPTY FILE. `_truncate_robots` stops
                # at the last complete line, so a host whose FIRST line is longer than
                # `ROBOTS_MAX_BYTES` leaves `kept` empty and this function held a 200 with a body
                # of `""`. `_is_robots_answer(200)` is True, so the refusal below was skipped,
                # `_parse_robots("", 200)` parsed an empty ruleset that `urllib.robotparser`
                # answers as allow-all, and the unconditional UPSERT pinned that non-answer in
                # `fetch_host_state` for `ROBOTS_TTL_SECONDS` - across every worker restart, with
                # no further request to the host. That is named change 11 broken in the one class
                # M51-340-01/03, M51-C2-340-01 and M51-C4-340-01 could not reach: the read
                # SUCCEEDS and no rule survives the cut. Status 0 is how this module already spells
                # RFC 9309 2.3.1.4's "unreachable", so the refusal below does the `rt.errors`
                # increment and the not-cached return with no second branch. The sentence is
                # logged here rather than there because that arm is quiet for status 0, which is
                # the one status it cannot name a number for.
                #
                # `raw` AND NOT `body` IS THE WHOLE TEST, and it is what keeps this a refusal of
                # damage rather than of size. A host that genuinely publishes an empty file, or
                # answers 204, has `raw == ""` too and still parses as the allow-all it really is;
                # a host whose rules precede the cut keeps them, which is RFC 9309 section 2.5's
                # parsing limit working as intended. [M5.1 review cycle 4 second pass,
                # M51-C4-340-06]
                log.info("robots.txt for %s is one line past the parsing limit, so this app read "
                         "no rule out of it; refusing this host for now", host)
                status = 0
        except Exception as exc:
            # AND THE STATUS GOES BACK TO "UNREACHABLE", WHICH IS WHAT THIS LINE ALREADY CLAIMS.
            # `status` is assigned INSIDE the `stream` block and `body` only after it closes, so
            # every failure while the BODY is being read - a reset, a truncated chunked response,
            # a corrupt gzip (this client declares `Accept-Encoding: gzip, deflate` at
            # `__aenter__`), a read timeout on a leg `ROBOTS_TIMEOUT_S` bounds per operation and
            # never as a total - landed here with `status` already 200 and `body` still `""`.
            # `_is_robots_answer(200)` is then True, so the refusal below was skipped, `rt.errors`
            # was not incremented, `_parse_robots("", 200)` parsed an EMPTY ruleset that
            # `urllib.robotparser` answers as allow-all, and the unconditional UPSERT wrote that
            # non-answer into `fetch_host_state` where `_load_host_state` reads it back as a valid
            # cache for a day - clobbering a `Disallow: /` this app had already read and was
            # obeying. That is the defect M51-340-01/03 closed for a 5xx and M51-C2-340-01 closed
            # for a 3xx, surviving in the one class those repairs could not reach: the headers
            # arrive and the body does not. RFC 9309 2.3.1.4's "unreachable" is the honest reading
            # and status 0 is how this module already spells it. A 4xx or 5xx never enters the
            # read loop (line 666), so no answer this function is meant to accept passes here.
            # [M5.1 review cycle 4, M51-C4-340-01]
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

    # `requests` IS WHAT WENT ON THE WIRE AND `errors` IS A SUBSET OF IT. Stated here because the
    # pair is a durable, operator-facing number - `persist_host_state` accumulates both into
    # `fetch_host_state` and `host_report()` publishes them - and until this cycle the column meant
    # two different things depending on which path wrote it. `_note_success` counted the request,
    # `_note_failure` counted only the error, and `_fetch_robots` counted the request before its
    # `try` and then the error as well: so a host answering 429 to three attempts durably recorded
    # that this app had made ZERO requests to the host that was rate limiting it, while one failed
    # robots fetch recorded one request and one error for the same single request. Neither
    # `requests` nor `requests + errors` was the wire count on every path, which is an ambiguous
    # column in a seam six milestones inherit. Change 8 in the module docstring already decided
    # which way this goes - "a counter that quietly omits a class of request is a counter that can
    # lie" - and `test_acquire_fetch.py` names the rule in the test it registers for the counter.
    # [M5.1 review cycle 2, M51-C2-340-02]
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

        Once per drain and not once per request: the counters are what §6.6 shows about a host and
        a row rewritten on every fetch would be write amplification of the very thing being rate
        limited. What is written is the DELTA since the last flush, so a second call adds nothing,
        and a failure here is logged rather than raised - a drain that did its work must not be
        reported as failed because the statistics it kept could not be written down.

        A DELTA AND NOT A RESET, which it used to be. Zeroing the counters as they were flushed
        made `host_report()` - "what this drain did, per host, for §6.6 and for the drain's own job
        detail" - answer zeros for every host the moment `__aexit__` ran, which is one line before
        the place a drain assembles that detail. [M5.1 review cycle 3, port-C3-06]

        `consecutive_failures` is written ABSOLUTELY where the other two accumulate, because it is
        not a count of what this drain did - it is the length of the run this host is currently on,
        and the next drain continues that run rather than starting a new one. The column was
        declared in `0024` and then only ever written as the literal zero by the breaker-open
        path, so it read 0 for every host that had not tripped and the breaker's threshold could
        only ever be reached inside ONE drain: a host failing seven times a drain, every drain,
        was never parked. [M5.1 review cycle 1, M51-340-09]
        """
        if self.conn is None:
            return
        for host, rt in sorted(self._hosts.items()):
            requests = rt.requests - rt.flushed_requests
            errors = rt.errors - rt.flushed_errors
            if not requests and not errors:
                continue
            # Marked before the write and not after it, which is the behaviour the reset had: a
            # statement that raises is logged and not retried, and counting it twice on the next
            # flush would inflate the one column §6.6 reads rather than leave it short.
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

        `allow_status` NAMES A STATUS THIS CALLER CAN READ AS AN ANSWER, and nothing more. It does
        not switch off the politeness: a status in `RETRYABLE_STATUS` still costs its `Retry-After`
        pause, still counts toward the host's breaker, and is handed back only once the attempts
        are spent. The condition here used to read `status in RETRYABLE_STATUS and status not in
        allow_status`, so a caller passing `allow_status=(429,)` - to read a provider's error body,
        which is an ordinary reason to pass it - fell through to `_note_success` and got the 429
        returned as a success: no pause, no breaker count, and the run of consecutive failures
        RESET on the one status that means the host is complaining about load. That is a per-call
        override of a stricter rule than the only override §8 permits, and one
        `hosts.undocumented_overrides()` is structurally unable to see. [M5.1 review cycle 1,
        M51-340-06]
        """
        assert self._client is not None, "use `async with Fetcher() as f`"
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        host = normalise_host(parsed.netloc, scheme=scheme)
        rt = await self._runtime(host)
        # THE URL THE REQUEST IS ACTUALLY MADE AGAINST, and the one string a caller must store the
        # answer under. `params` is handed to httpx separately, so `_validators(url)` read the
        # validator filed under a DIFFERENT document for every source whose endpoint is shared and
        # whose query is the only thing distinguishing one title from another - which is six of the
        # eight adapters M5.3 ports (`mdc/sources/omdb.py:37` is one url for every title, and
        # wikidata, wikipedia, tvmaze and tmdb are the same shape). One document's ETag then
        # conditioned another document's request, and a host whose ETag ignores the query would
        # answer 304 for bytes this app never stored under that name.
        # [M5.1 review cycle 3, port-C3-01]
        #
        # COMPUTED BEFORE THE ROBOTS DECISION, because `can_fetch` matches on path AND query and
        # that repair was applied to one of the two readers of this string. A caller passing its
        # query as `params=` - the convention the paragraph above says six of the eight adapters
        # use, and the shape `mdc/blogs.py` fetches `/wp-json/wp/v2/posts` with against four hosts
        # `hosts.py` declares with the default `respect_robots=True` - had its robots decision
        # taken against a url that never went on the wire, so a query-scoped `Disallow` was read
        # as permission. The hop path never had the gap: it joins the `Location` in first and
        # judges the joined url, so ONE function gave two different answers about one
        # byte-identical request. `normalise_host` and `policy_for` read only `parsed.netloc`,
        # which a query cannot change, so the host key, the bucket and the breaker are untouched
        # and only the string `can_fetch` judges moves. [M5.1 review cycle 4, M51-C4-340-02]
        request_url = str(httpx.URL(url, params=params)) if params else url

        self._raise_if_paused(host, rt)
        await self._check_robots(host, rt, request_url, scheme)

        req_headers = dict(headers or {})
        if any(key.lower() == "user-agent" for key in req_headers):
            # Decision 340: "It declares a User-Agent naming the app." httpx lets a per-request
            # header win over the client's, and `_check_robots` judges the request against the
            # module's own `USER_AGENT` - so a stage that copied a browser or bot agent in here to
            # get past a wall would misrepresent the household to a third party while being
            # allowed by a rule written for a name it is no longer sending. A refusal and not a
            # silent merge: the caller has to know it asked for something this layer will not do.
            # [M5.1 review cycle 1, M51-340-07]
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
        for attempt in range(1, max_attempts + 1):
            hop_url, hop_host, hop_rt, hop_scheme = url, host, rt, scheme
            hop_headers, hop_method, hop_json, hop_params = dict(req_headers), method, json_body, params
            hop_conditioned, hops, started = conditioned, 0, self._clock()
            resp: httpx.Response | None = None
            while True:
                # Re-checked inside the loop: a concurrent task on the same host can open the
                # breaker between two of this call's attempts, and the whole point of one host's
                # breaker is that it stops every task reaching that host and not merely the one
                # that tripped it.
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
                    # `httpx.RequestError` and not the three names this port inherited from
                    # `mdc/http.py:253`. `TooManyRedirects` and `DecodingError` are `RequestError`
                    # but NOT `TransportError`, so a host in a redirect loop or serving a
                    # truncated gzip escaped this layer entirely: not wrapped in `FetchError`,
                    # which this class's own docstring promises is "every failure this layer
                    # raises"; `_note_failure` never called, so the breaker could never open on
                    # it; and the task retried until its attempts were spent, burning the host
                    # again each time. `InvalidURL` and `StreamError` are deliberately still
                    # outside: those are caller bugs, not a remote host misbehaving.
                    # [M5.1 review cycle 1, M51-340-05]
                    last_exc = exc
                    await self._note_failure(hop_host, hop_rt)
                    resp = None
                    break

                location = resp.headers.get("location", "") if (
                    resp.status_code in REDIRECT_STATUS) else ""
                if not location:
                    break

                # A hop is an answer from THIS host and a request to the NEXT one, so it is
                # counted here and then re-gated below. Without the count, `host_report()` and
                # `fetch_host_state` would show one request for a chain that put several on the
                # wire, and §6.6's board could not show that the further hosts were reached at all.
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
                # THE BREAKER BEFORE THE ROBOTS FETCH, which is the order the entry path takes and
                # the order `_fetch_robots`'s own docstring asserts of both - "the breaker is
                # consulted before this (`_raise_if_paused` runs first in `get`), so a paused host
                # is not even asked". It was not true of a hop: `_check_robots` came first and the
                # cooldown was read only on the next turn of this loop, so a redirect into a host
                # in a 900-second cooldown charged that host's bucket and put a `/robots.txt` on
                # its wire first. And when that robots request failed too - the normal case, since
                # a host in cooldown is a host that has been failing - the walk parked with
                # `RobotsUnavailable`'s sentence, which decision 336 shows VERBATIM on §6.6's
                # board: a robots problem reported for a host that had none, hiding the one state
                # an operator could have waited out. [M5.1 review cycle 3, M51-C3-340-02]
                self._raise_if_paused(hop_host, hop_rt)
                await self._check_robots(hop_host, hop_rt, hop_url, hop_scheme)
                # The query is in the Location; re-appending the caller's params would send it
                # twice. The validators are dropped because they are facts about the ORIGINAL
                # url - a 304 from a different url would be the host agreeing about bytes this
                # app has never stored under that name. RFC 9110's method rewrite is kept:
                # 303 always becomes a GET, and a 301 or 302 on a POST does too, which is what
                # every client on the web does and what a redirecting host expects.
                hop_params = None
                if not _same_origin(from_scheme, from_host, hop_scheme, hop_host):
                    # AND THE CALLER'S HEADERS DO NOT CROSS AN ORIGIN, which is the protection named
                    # change 10 took off httpx and did not replace. `mdc/http.py:128` set
                    # `follow_redirects=True`, so httpx popped `Authorization` on a cross-origin hop
                    # (`_client.py:552-556`) and `Cookie` on every hop at all; driving the hops here
                    # re-sent every header the caller passed to whatever host a `Location` named.
                    # M5.3 ports "the corpus's request shapes verbatim" and those shapes are
                    # header-borne credentials - TMDb's bearer, Trakt's client id, and on any path
                    # that reaches the household's own server, Jellyfin's admin-equivalent key - so
                    # one redirect out of a CDN, a consent edge or a hijacked record would deliver
                    # them to that host in the clear, from the household's own IP, with no log line
                    # and no park.
                    #
                    # REBUILT FROM NOTHING rather than popping the names httpx knows: this layer
                    # sets only the two conditional validators, which are dropped on a hop anyway,
                    # so everything in here is the caller's and none of it is this layer's to
                    # forward. The client's own defaults - the declared User-Agent, `Accept-*` -
                    # live on the `AsyncClient` and survive untouched.
                    # [M5.1 review cycle 3, M51-C3-340-01]
                    hop_headers = {}
                hop_headers.pop("If-None-Match", None)
                hop_headers.pop("If-Modified-Since", None)
                hop_conditioned = False
                if resp.status_code == 303 or (
                    resp.status_code in (301, 302) and hop_method.upper() == "POST"
                ):
                    hop_method, hop_json = "GET", None

            if resp is None:
                if attempt >= max_attempts:
                    raise FetchError(
                        f"{type(last_exc).__name__}: {last_exc}", url=url
                    ) from last_exc
                await self._sleep(_backoff(attempt, self._jitter))
                continue

            elapsed = self._clock() - started
            status = resp.status_code

            if status == 304 and hop_conditioned:
                # The bytes this app already stored are still current, so there are none to hand
                # back: §8's "re-parsing is free forever" is this branch plus the raw store.
                self._note_success(hop_rt)
                return Response(str(resp.url), 304, b"", resp.headers, from_cache=True,
                                elapsed=elapsed, request_url=request_url)

            if 300 <= status < 400 and status not in allow_status:
                # EVERY 3xx LEAVES THIS LAYER FOLLOWED OR REFUSED, NEVER PARSED. The hop loop above
                # breaks out on `if not location`, and a 3xx is in neither `RETRYABLE_STATUS` nor
                # the `>= 400` arm, so a redirect this layer could not follow fell all the way
                # through to `_note_success` and was handed back as an ordinary answer - status
                # 301, body `<html>Moved Permanently</html>`, `from_cache False`. `MAX_REDIRECTS`
                # already argues why that must not happen ("a truncated chain returned as an answer
                # is a 3xx body handed to a parser") and refuses a chain that is too LONG; this is
                # the same outcome through the door the cap does not watch, and a stage written as
                # the obvious `try: ... except FetchError as e: park(e)` sees no error at all.
                #
                # THREE WAYS IN, and the class is refused rather than each of them: a 3xx carrying
                # no `Location` at all, which is what several CDNs and WAFs emit; a 300 Multiple
                # Choices, which is deliberately not in `REDIRECT_STATUS` because there is nothing
                # to follow; and a 304 answering a request this layer did not condition, which the
                # branch above cannot claim is the caller's stored bytes.
                #
                # RETRYABLE, which is the default and is the honest reading: a malformed redirect
                # from an edge is the transient case decision 336's `parked` exists for, while
                # `retryable=False` would tell a stage the answer will be the same tomorrow. And
                # `_note_success`, for the reason the `>= 400` arm gives: it is an answer rather
                # than a failure, and a host that emits one bad redirect has not refused this app.
                # [M5.1 review cycle 2, M51-C2-340-07]
                self._note_success(hop_rt)
                raise FetchError(
                    f"HTTP {status} with no usable redirect", status=status, url=url
                )

            if status in RETRYABLE_STATUS:
                await self._note_failure(hop_host, hop_rt)
                if attempt >= max_attempts:
                    if status not in allow_status:
                        raise FetchError(f"HTTP {status}", status=status, url=url)
                    # The caller named this status as an answer it can read, so it gets the body -
                    # after the pauses and the breaker count, never instead of them.
                    content = resp.content
                    self.total_bytes += len(content)
                    return Response(str(resp.url), status, content, resp.headers,
                                    elapsed=elapsed, request_url=request_url)
                delay = _retry_after(resp.headers) or _backoff(attempt, self._jitter)
                if status == 429:
                    # Never shortened. A 429 is the host saying how long to wait, and this takes
                    # the longer of what it asked for and three times what the backoff would have
                    # chosen - so an optimistic `Retry-After: 1` from a host that is rate limiting
                    # in earnest still costs a real pause. The corpus's rule, verbatim.
                    delay = max(delay, _backoff(attempt, self._jitter) * 3)
                await self._sleep(delay)
                continue

            if status >= 400 and status not in allow_status:
                # A 4xx that is not in the retryable set is a real answer rather than a glitch, and
                # is not the host's fault - so it does not count toward the breaker.
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

        The newest successful row and not merely the newest: a failed fetch stores no validator
        worth sending, and conditioning a request on one taken from an error page is how a store
        comes to hold a 503 under the name of the document. Reads the `(url, fetched_at DESC)`
        index `0024` adds for exactly this question.

        `, id DESC` because `fetched_at` alone does not order these rows. Its default is Postgres's
        `now()`, which is TRANSACTION start time, so two documents a stage stores for one url
        inside its own transaction carry a byte-identical timestamp and `LIMIT 1` returns whichever
        the plan happens to reach first. The corpus's query had no tiebreaker either and did not
        need one - `mdc/rawstore.py:120` passes `time.time()` per INSERT - so named change 1 took
        the clock away and left the query. `acquire/board.py:99` already reads this table with the
        tiebreaker; these three readers did not. [M5.1 review cycle 1, port-03]

        THE KEY IS THE URL THE REQUEST IS MADE AGAINST, INCLUDING THE QUERY, and `get` passes
        `request_url` rather than its `url` argument for that reason. The corpus kept the two sides
        honest by construction - `mdc/http.py` read at :229 and wrote at :289 inside one function,
        off one variable - and named change 1 deleted the write, so the only thing holding read key
        and write key together now is that the answer carries the key: `Response.request_url` is
        what a caller hands to `rawstore.store(url=...)`. [M5.1 review cycle 3, port-C3-01]
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

        VALID AFTER THE CONTEXT CLOSES, which is where a drain assembles that detail.
        `persist_host_state` writes a delta rather than zeroing the counters for exactly this
        reason. [M5.1 review cycle 3, port-C3-06]

        TWO RATES, because `rps` alone was a configured CEILING sitting among three measured facts
        and reading like a fourth. `_pace_from_robots` moves the bucket down to a host's published
        `Crawl-delay` without touching `rt.policy`, so a surface asking how fast this household is
        crawling a host has to show `effective_rps`; `rps` stays because what an operator
        configured and what a host asked for are different questions and §6.6 shows both.
        [M5.1 review cycle 3, port-C3-05]
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

    httpx's own rule, restated here because named change 10 took httpx off the hop path and with it
    the `Authorization` strip its `_client.py:552-556` performs: an origin is the scheme, the host
    and the port, and the single exception is a host upgrading its OWN http to https, which every
    site on the web does and which is not a change of party.

    THE PORT HALF NEEDS NO CODE HERE. Both arguments are already `normalise_host` keys, so a port
    equal to the scheme's default is gone from both and a non-default port is still in both - which
    is exactly what comparing ports would have asked, without a second spelling of the rule.
    [M5.1 review cycle 3, M51-C3-340-01]
    """
    if from_host != to_host:
        return False
    return from_scheme == to_scheme or (from_scheme == "http" and to_scheme == "https")


def _is_robots_answer(status: int | None) -> bool:
    """Did the host actually answer the robots.txt request? RFC 9309 §2.3.1.

    2xx is a served file; 4xx is §2.3.1.3's "unavailable", which means no rules were published and
    a crawler "MAY access any resources"; 5xx and a transport failure (status 0) are §2.3.1.4's
    "unreachable", where the crawler "MUST assume complete disallow" - not an answer, and not
    something to cache or to read as a permission. [M5.1 review cycle 1, M51-340-03]

    **A 3xx IS NOT A SERVED FILE HERE, AND THIS DOCSTRING USED TO SAY IT WAS.** RFC 9309 gives
    redirects their own subsection (§2.3.1.2) precisely because a redirect is not the file: it
    tells a crawler to follow at least five hops and only then to fall back to "unavailable". This
    module's client is built with `follow_redirects=False` (named change 10) and `_fetch_robots`
    calls `self._client.get` rather than `get`'s own hop loop, so NOBODY follows a robots redirect
    - and `body` is `""` for anything that is not a 200, so `_parse_robots` was handed an empty
    ruleset, which `urllib.robotparser` answers as allow-all. The unconditional UPSERT then wrote
    that non-answer into `fetch_host_state` with `robots_fetched_at = now()`, where
    `_load_host_state` read it back as a valid cache for a day and clobbered any `Disallow: /`
    this app had already read and was obeying. That is the defect M51-340-01/03 closed for a 5xx,
    surviving in the one status class the repair's boundary let through: one 301 from a CDN, a
    consent edge or a plain http->https hop, and this app crawls every path a host disallows, on
    the household's own IP, for twenty-four hours across every worker restart.

    A non-answer is the right side of the line for it rather than an omission: `RobotsUnavailable`
    is retryable and is not cached, so the next drain asks again and a host that redirects its
    robots.txt costs one refused task rather than a day of ignored rules.
    [M5.1 review cycle 2, M51-C2-340-01]
    """
    if status is None:
        return False
    status = int(status)
    return 200 <= status < 300 or 400 <= status < 500


def _parse_robots(body: str, status: int | None) -> urllib.robotparser.RobotFileParser:
    """A parser over this body, or an allow-all one for a host that published no rules.

    Only ever called with an ANSWER (`_is_robots_answer`). A 2xx is parsed; a 4xx parses as empty,
    which `urllib.robotparser` reads as "allow all" and which RFC 9309 §2.3.1.3 says is correct -
    the whole 400-499 range is "unavailable" and a crawler "MAY access any resources". A 3xx, a
    5xx or an unreachable host never reaches here: those are `RobotsUnavailable`, not an allow-all.

    The 2xx test is the whole class and not the literal 200, so that it and `_is_robots_answer`
    draw one line: a 204 on robots.txt is a served file with no rules in it, which parses to the
    same allow-all it would mean, while a status this function read as "no rules" and the
    predicate read as "an answer" would be a host's published rules dropped without a word.
    [M5.1 review cycle 2, M51-C2-340-01]
    """
    served = status is not None and 200 <= int(status) < 300
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines() if served else [])
    return parser


def _truncate_robots(body: str) -> str:
    """At most `ROBOTS_MAX_BYTES` of a host's robots.txt, cut between lines.

    Whole lines, because half of a `Disallow:` line is a rule this app would then not obey: a cut
    inside a path shortens the prefix a rule covers, and a cut inside the directive name drops the
    rule outright. Measured in bytes rather than characters because RFC 9309 §2.5's limit is in
    kibibytes, and the cheap character test first because a str can never encode to fewer bytes
    than it has characters - so the common case pays one `len`. [M5.1 review cycle 2, M51-C2-340-06]

    `and` AND NOT `or`, because that premise licenses the opposite short-circuit from the one it
    was used for. "A str can never encode to fewer bytes than it has characters" proves that MORE
    characters than the cap means more bytes than the cap - a reason to REJECT on the character
    count, never to ACCEPT on it. Under `or` the character test returned the body untouched on its
    own, and the byte test was dead code: it only ran when the character count already exceeded
    the cap, where the byte count necessarily does too. So for any body with substantial non-ASCII
    - a German-commented robots.txt, which is the shape one of the four blog hosts `hosts.py`
    declares would serve - the characters fit, the bytes did not, and the arbitrary byte the
    stream was cut at survived as the last rule. `_fetch_robots` stops reading on a BYTE count, so
    the body handed here is always at or past the cap in bytes and always cut mid-line; that is
    the whole reason this function exists. A half-written `Crawl-delay: 3` where the host wrote
    `Crawl-delay: 30` is `_requested_rps` pacing it ten times faster than it asked.
    [M5.1 review cycle 4, M51-C4-340-05]
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

    `Crawl-delay` and `Request-rate` are the two robots.txt directives that are about RATE rather
    than about paths - the only way a site can ask a crawler in writing to slow down - and
    `urllib.robotparser` has already parsed both onto the object `_parse_robots` returns. Neither
    was read. The app fetched the file, stored it in Postgres, held the parsed rules in memory,
    consulted them for `can_fetch` alone, and then crawled at a number of its own choosing:
    `DEFAULT_RPS = 1.0` for any host nobody has measured, which is thirty times a published
    `Crawl-delay: 30`. Four of the rows `hosts.py` declares are "small sites run by individuals".
    [M5.1 review cycle 2, M51-C2-340-03]

    THE STRICTER OF THE TWO when a host publishes both, for the same reason `get` takes
    `max(retry_after, backoff * 3)` on a 429: where a host has stated a wish twice, the polite
    reading is the one that asks less of it. Both are looked up against `USER_AGENT`, so a group
    addressed to `Spielplan` by name outranks the `*` group exactly as `can_fetch` makes it.
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
    """The corpus's curve: 1.6s, 3.2s, 6.4s ... capped at a minute, spread +/-30 percent.

    The cap is what stops the fourth retry of a dead host from being a quarter of an hour, and the
    spread is what stops a hundred tasks that failed together from retrying together.
    """
    return min(60.0, (2 ** attempt) * 0.8) * jitter(0.7, 1.3)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in either form RFC 9110 §10.2.3 permits, capped at five minutes.

    BOTH FORMS, AND THE REASON THIS READ ONLY ONE WAS FALSE. It used to say "a mis-parsed date
    read as a number would be a pause of geological length", and neither half of that can happen:
    `float()` RAISES on all three date productions rather than returning a number, and the cap on
    the line below already bounds any misreading to five minutes. What the omission actually did
    was discard an explicit instruction - a host answering 429 with a date an hour out got the
    backoff curve, so four requests inside twenty-four seconds where the coverage row for this
    layer says "a 429 honours Retry-After and backs off at least as far". The date form is what
    Cloudflare and several API gateways emit, and §8 stage 2's two HTML hosts sit behind exactly
    that. [M5.1 review cycle 4, M51-C4-340-03]

    THROUGH THE SAME CAP, so the two forms answer alike: a host asking for an hour is honoured for
    five minutes either way, which is the bound this app puts on how long one host may hold one
    task. THROUGH THE SAME FLOOR TOO, and that half was written as if it were true while only the
    date branch had it: a date already in the past is an instruction that has expired and a
    negative number of seconds is the same instruction spelled the other way, so neither is a
    negative pause. Unparseable still means "no instruction", which sends the caller to the
    backoff curve rather than to zero.
    """
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        # CLAMPED AT ZERO ON THIS BRANCH TOO, which the paragraph above promised and only the date
        # branch delivered. `min(float(raw), 300.0)` passed a NEGATIVE value through the cap, and a
        # negative delay is `await self._sleep(-1.0)` - a pause `asyncio.sleep` returns from at
        # once - so a host answering any retryable status with `Retry-After: -1` collapsed four
        # attempts of the backoff curve into one event-loop turn. Only the 429 arm survived, since
        # `max(delay, _backoff(attempt) * 3)` rescues it there, and 429 is the only status the
        # tests drive a `Retry-After` on. Zero is the right answer rather than None: it is falsy,
        # so `_retry_after(...) or _backoff(...)` at the call site sends an expired instruction to
        # the curve, which is what the date branch already means by the same value. `nan` and
        # `-inf` fall out of the same clamp, and `nan` is the one that mattered - it reached
        # `asyncio.sleep`, which raises `ValueError` out of `get`, past the one type `FetchError`'s
        # docstring says this layer raises. [M5.1 review cycle 4 second pass, M51-C4-340-07]
        return min(max(0.0, float(raw)), 300.0)
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # `parsedate_to_datetime` returns a naive datetime for a date with no zone, and RFC 9110 dates
    # are GMT by definition - so reading one as local time would move the pause by the household's
    # own offset.
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return min(max(0.0, (deadline - datetime.now(UTC)).total_seconds()), 300.0)
