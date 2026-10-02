"""The polite per-host fetcher, asserted as arithmetic (§8, decision 340). No network: MockTransport doubles
and an injected clock, so a two-minute pause is asserted exactly. The robots cache, breaker persistence
and conditional request need TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from spielplan.acquire.fetch import (
    MAX_REDIRECTS,
    RETRYABLE_STATUS,
    ROBOTS_MAX_BYTES,
    USER_AGENT,
    Fetcher,
    FetchError,
    HostPaused,
    RobotsDisallowed,
    RobotsUnavailable,
    TokenBucket,
    _backoff,
    _retry_after,
)
from spielplan.acquire.hosts import (
    DEFAULT_RPS,
    HOST_POLICIES,
    WEB_TMDB_POLICY,
    normalise_host,
    policy_for,
)
from spielplan.core.config import settings

# Hosts whose declared policy turns robots off, so a pacing count carries no robots.txt request.
FAST = "api.themoviedb.org"       # rps 18, burst 20, threshold 8, cooldown 300
SECOND = "api.trakt.tv"           # rps 2.5, burst 3 - a different host, which is the whole point


class _Clock:
    """Replaces `time.monotonic` and `asyncio.sleep` together:
    the bucket must see the time a sleep claimed."""

    # A bucket that cannot satisfy its own wait spins; the bound turns a hung suite into a failure.
    _SPIN_LIMIT = 100

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if len(self.slept) > self._SPIN_LIMIT:
            raise AssertionError(
                f"paced {self._SPIN_LIMIT} times without progress; last wait was {seconds!r} - "
                "the bucket is asking for a token it can never be given"
            )
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _low(lo: float, _hi: float) -> float:
    """Jitter pinned to the bottom of its band, so the curve is a number and not a distribution."""
    return lo


def _fetcher(handler, clock: _Clock, **kwargs) -> Fetcher:
    return Fetcher(
        transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep,
        jitter=_low, **kwargs,
    )


def _always(status: int, **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **kwargs)
    return handler


def test_the_two_html_connectors_stage_two_names_are_kept_deliberately_slow():
    """The rate the corpus crawled both hosts at unblocked; raising it is a decision, not a tune."""
    for host in ("www.rottentomatoes.com", "www.metacritic.com"):
        policy = HOST_POLICIES[host]
        assert policy.rps == 0.7, f"{host} is one of the two hosts section 8 stage 2 scrapes"
        assert policy.burst == 1
        assert policy.max_concurrency == 1
        assert policy.breaker_cooldown_s == 900


def test_every_robots_override_records_the_reasoning_that_licenses_it():
    """An override needs its reasoning beside it; the second assertion stops the first passing vacuously."""
    overridden = [h for h, p in HOST_POLICIES.items() if not p.respect_robots]
    assert [h for h in overridden if not HOST_POLICIES[h].note.strip()] == []
    assert len(overridden) >= 5, "the table has overrides; a guard over none of them guards nothing"


def test_the_households_own_jellyfin_is_not_a_third_party():
    """§8's Jellyfin exemption: both halves, case-insensitive,
    whole-host, and from the URL the app stores."""
    home = "jellyfin.home.example"
    exempt = policy_for(home, jellyfin_host=home)
    assert exempt.respect_robots is False
    assert exempt.rps == 8.0
    assert exempt.note, "decision 340 requires the reasoning beside the override"

    assert policy_for(home.upper(), jellyfin_host=home).rps == 8.0
    stranger = policy_for(f"evil.{home}", jellyfin_host=home)
    assert stranger.rps == DEFAULT_RPS
    assert stranger.respect_robots is True

    for url, host in (
        ("http://jelly.home:8096", "jelly.home:8096"),
        ("https://jelly.home", "jelly.home"),
        ("https://jelly.home:443/", "jelly.home"),
        ("http://jelly.home:80", "jelly.home"),
        (f"http://{home}:8096/", f"{home}:8096"),
    ):
        assert policy_for(host, jellyfin_host=url).rps == 8.0, (
            f"a configured {url!r} left the household's own server on the stranger's policy"
        )
        assert policy_for("http", jellyfin_host=url).rps == DEFAULT_RPS, (
            f"a configured {url!r} handed the exemption to a host literally spelled 'http'"
        )
    # A bare netloc has no scheme, so an explicit default port is not a distinguishing port.
    assert policy_for("jelly.lan", jellyfin_host="jelly.lan:80").rps == 8.0
    assert policy_for("jelly.lan:8096", jellyfin_host="jelly.lan:80").rps == DEFAULT_RPS

    # A declared row outranks the configured Jellyfin host, or a typo there crawls a stage-2 host at 8 rps.
    for declared in ("www.rottentomatoes.com", "www.metacritic.com"):
        hijacked = policy_for(declared, jellyfin_host=f"https://{declared}")
        assert hijacked.rps == HOST_POLICIES[declared].rps, (
            f"a Jellyfin url typed as {declared!r} replaced a rate somebody measured"
        )
        assert hijacked.respect_robots is True, (
            f"a Jellyfin url typed as {declared!r} turned off that host's robots.txt"
        )


def test_an_unknown_host_is_crawled_at_the_slow_default():
    """The table's safety property: an unmeasured host costs slowness, not a block."""
    policy = policy_for("nobody-has-measured-this.example")
    assert policy.rps == DEFAULT_RPS
    assert policy.respect_robots is True


def test_the_configured_tmdb_api_host_is_crawled_under_tmdbs_own_row(monkeypatch):
    """`SPIELPLAN_TMDB_API_BASE` moves TMDB's API, not its rate: e2e's fake is asked under TMDB's row."""
    monkeypatch.setenv("SPIELPLAN_TMDB_API_BASE", "http://tmdb-fake:8097/3")
    settings.cache_clear()
    try:
        assert policy_for("tmdb-fake:8097") is HOST_POLICIES[FAST]
        assert policy_for("tmdb-fake").rps == DEFAULT_RPS
    finally:
        settings.cache_clear()


async def test_a_policy_handed_to_the_fetcher_outranks_the_declared_row():
    """Decision 558: the web process asks TMDB in a bucket of its own, smaller than the worker's."""
    assert WEB_TMDB_POLICY.rps < HOST_POLICIES[FAST].rps
    assert (WEB_TMDB_POLICY.burst, WEB_TMDB_POLICY.max_concurrency) == (4, 2)
    assert WEB_TMDB_POLICY.respect_robots is False and WEB_TMDB_POLICY.note
    clock = _Clock()
    async with _fetcher(_always(200), clock, policies={FAST: WEB_TMDB_POLICY}) as f:
        for n in range(WEB_TMDB_POLICY.burst + 1):
            await f.get(f"https://{FAST}/3/search/movie", params={"page": n})
    assert clock.slept == [pytest.approx(1 / WEB_TMDB_POLICY.rps)], "the fifth waited one token"
    assert [h["rps"] for h in f.host_report()] == [WEB_TMDB_POLICY.rps]

    worker = _Clock()
    async with _fetcher(_always(200), worker) as f:
        for n in range(WEB_TMDB_POLICY.burst + 1):
            await f.get(f"https://{FAST}/3/movie/{n}")
    assert worker.slept == [], "the worker's own row is untouched"


def test_the_three_paid_provider_hosts_are_declared_with_the_corpus_numbers_and_their_reasoning():
    """§8 stage 6's provider hosts with the corpus's numbers; each robots override carries its argument."""
    expected = {
        "api.anthropic.com": (2.0, 4, 4),
        "api.openai.com": (2.0, 4, 4),
        "generativelanguage.googleapis.com": (1.5, 3, 3),
    }
    for host, (rps, burst, concurrency) in expected.items():
        policy = HOST_POLICIES[host]
        assert (policy.rps, policy.burst, policy.max_concurrency) == (rps, burst, concurrency), host
        assert policy.breaker_cooldown_s == 120, host
        assert policy.breaker_threshold == 8, f"{host}: the corpus sets no threshold of its own"
        assert policy.respect_robots is False, host
        assert "politeness is not the constraint" in policy.note, host
        assert "rate limit" in policy.note, host
        assert policy_for(host) is policy
    for marker in ("anthropic", "openai", "googleapis", "generativelanguage"):
        assert len([h for h in HOST_POLICIES if marker in h]) == 1, (
            f"one {marker} host is declared; a second spelling of a provider is a second bucket "
            "and a second breaker for one rate limit"
        )


async def test_a_host_is_paced_at_its_declared_rate_once_its_burst_is_spent():
    """The burst is spent without waiting; each later request waits exactly one token's accrual."""
    clock = _Clock()
    bucket = TokenBucket(0.7, 1, clock=clock, sleep=clock.sleep, jitter=_low)
    await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == [pytest.approx(1 / 0.7), pytest.approx(1 / 0.7)]


async def test_the_bucket_never_refills_past_its_burst():
    """Tokens cap at `burst`, so an idle hour buys `burst` requests, not 3,600."""
    clock = _Clock()
    bucket = TokenBucket(1.0, 2, clock=clock, sleep=clock.sleep, jitter=_low)
    clock.advance(3600)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == [pytest.approx(1.0)]


async def test_the_bucket_cannot_spin_when_the_wait_it_computed_lands_a_float_short():
    """`(now - updated) * rps` lands at 0.9999999999999432 on a large clock; full precision would spin."""
    clock = _Clock(start=1000.0)
    bucket = TokenBucket(2.5, 3, clock=clock, sleep=clock.sleep, jitter=_low)
    for _ in range(3):
        await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == [pytest.approx(0.4)], "one wait, and it bought the token it asked for"


def test_the_backoff_curve_doubles_jitters_and_is_capped_at_a_minute():
    """The cap bounds a retry inside a worker tick; the band catches a jitter that is secretly constant."""
    assert _backoff(1, _low) == pytest.approx(1.6 * 0.7)
    assert _backoff(2, _low) == pytest.approx(3.2 * 0.7)
    assert _backoff(3, _low) == pytest.approx(6.4 * 0.7)
    assert _backoff(7, _low) == pytest.approx(60.0 * 0.7), "the cap, not 102 seconds"
    assert _backoff(20, _low) == pytest.approx(60.0 * 0.7)

    spread = {round(_backoff(3), 6) for _ in range(50)}
    assert len(spread) > 1, "a backoff with no jitter re-synchronises every retrying task"
    assert all(6.4 * 0.7 <= value <= 6.4 * 1.3 for value in spread)


def test_retry_after_is_read_in_seconds_capped_and_never_guessed():
    """Both RFC 9110 §10.2.3 forms through one cap; unparseable
    means the backoff curve, a past date expired."""
    assert _retry_after({"retry-after": "120"}) == 120.0
    assert _retry_after({"retry-after": "9999"}) == 300.0, "capped at five minutes"
    assert _retry_after({}) is None
    assert _retry_after({"retry-after": "not a pause"}) is None
    assert _retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is not None, (
        "the date form is as legal as the seconds form, and discarding it ignores the host"
    )

    soon = datetime.now(UTC) + timedelta(seconds=90)
    assert _retry_after({"retry-after": format_datetime(soon, usegmt=True)}) == pytest.approx(
        90.0, abs=5.0
    )
    hour = datetime.now(UTC) + timedelta(hours=1)
    assert _retry_after({"retry-after": format_datetime(hour, usegmt=True)}) == 300.0, (
        "the same cap as the seconds form, so the two spellings answer alike"
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    assert _retry_after({"retry-after": format_datetime(past, usegmt=True)}) == 0.0, (
        "an instruction that has expired is not a negative pause"
    )

    # The seconds form through the same floor: negative or non-finite values go to the curve.
    for header in ("-1", "-99999", "nan", "-inf"):
        assert _retry_after({"retry-after": header}) == 0.0, header
    assert _retry_after({"retry-after": "inf"}) == 300.0, "the cap holds for the other infinity"


def test_the_retryable_set_is_the_corpus_list_including_cloudflares_family():
    """520-524 are Cloudflare's origin-side family, in front
    of both §8 stage 2 HTML hosts; 404 is an answer."""
    assert sorted(RETRYABLE_STATUS) == [408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524]
    assert 404 not in RETRYABLE_STATUS


async def test_a_429_is_paused_for_as_long_as_the_host_asked():
    """`Retry-After` is honoured and never shortened; the backoff's 3.36 s is overruled."""
    clock = _Clock()
    async with _fetcher(_always(429, headers={"Retry-After": "120"}), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=2)
    assert caught.value.status == 429
    assert caught.value.retryable is True
    assert clock.slept == [pytest.approx(120.0)]


async def test_a_429_with_an_optimistic_retry_after_still_costs_three_backoffs():
    """The rule is a `max`: an optimistic `Retry-After: 1` still costs three backoffs."""
    clock = _Clock()
    async with _fetcher(_always(429, headers={"Retry-After": "1"}), clock) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=2)
    assert clock.slept == [pytest.approx(1.6 * 0.7 * 3)]


async def test_a_retryable_status_without_an_instruction_follows_the_backoff_curve():
    """The last attempt raises rather than sleeping for a fourth that will never happen."""
    clock = _Clock()
    async with _fetcher(_always(503), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=3)
    assert caught.value.status == 503
    assert clock.slept == [pytest.approx(1.6 * 0.7), pytest.approx(3.2 * 0.7)]


async def test_a_404_is_an_answer_and_is_raised_without_a_retry():
    """A 4xx outside the retryable set is an answer: raised at once and not counted toward the breaker."""
    clock = _Clock()
    async with _fetcher(_always(404), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/0", max_attempts=4)
        assert f.total_errors == 0, "a 404 is not the host's fault"
    assert caught.value.retryable is False
    assert caught.value.status == 404
    assert clock.slept == []


async def test_the_breaker_opens_for_one_host_and_the_other_host_keeps_fetching():
    """The second host is warmed up BEFORE the failures, or
    a breaker leaking to every known host would pass."""
    clock = _Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if request.url.host == FAST else 200, content=b"ok")

    async with _fetcher(handler, clock) as f:
        assert (await f.get(f"https://{SECOND}/movies/fight-club", max_attempts=1)).status == 200
        for _ in range(8):
            with pytest.raises(FetchError):
                await f.get(f"https://{FAST}/3/movie/603", max_attempts=1)
        with pytest.raises(HostPaused) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=1)
        assert caught.value.remaining == pytest.approx(300.0)
        assert caught.value.retryable is True

        other = await f.get(f"https://{SECOND}/movies/fight-club/comments", max_attempts=1)
        assert other.status == 200

        report = {row["host"]: row for row in f.host_report()}
        assert report[FAST]["paused_for"] == pytest.approx(300.0)
        assert report[SECOND]["paused_for"] == 0.0


async def test_robots_disallowed_is_not_retryable_and_costs_one_request():
    """The answer will be the same tomorrow, so `retryable=False`, and only the robots.txt went out."""
    clock = _Clock()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, content=b"page")

    async with _fetcher(handler, clock) as f:
        with pytest.raises(RobotsDisallowed) as caught:
            await f.get("https://unmeasured.example/title/1")
        assert f.total_requests == 1
    assert caught.value.retryable is False
    assert seen == ["/robots.txt"]


async def test_every_request_declares_the_user_agent_that_names_the_app():
    """`urllib.robotparser` matches the token before the slash, so the app's name comes first. Asserted over
    all four kinds of request, named by hand; the robots.txt fetch does not go through `get`'s headers."""
    clock = _Clock()
    sent: list[tuple[str, str]] = []
    flaky = {"left": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((f"{request.url.host}{request.url.path}",
                     request.headers.get("user-agent", "")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "unmeasured.example":
            if flaky["left"]:
                flaky["left"] -= 1
                return httpx.Response(503)
            return httpx.Response(302, headers={"location": "https://elsewhere.example/landed"})
        return httpx.Response(200, content=b"ok")

    async with _fetcher(handler, clock) as f:
        assert (await f.get("https://unmeasured.example/detail")).status == 200

    assert [path for path, _ in sent] == [
        "unmeasured.example/robots.txt",
        "unmeasured.example/detail",
        "unmeasured.example/detail",
        "unmeasured.example/detail",
        "elsewhere.example/robots.txt",
        "elsewhere.example/landed",
    ], f"the four kinds of request this asserts over are no longer all here: {sent}"
    anonymous = [path for path, agent in sent if agent != USER_AGENT]
    assert not anonymous, f"these went out without the declared User-Agent: {anonymous}"
    assert USER_AGENT.split("/")[0] == "Spielplan"


async def test_the_request_counter_starts_at_zero_and_counts_what_went_out():
    """A re-parse must cost zero requests; bytes are counted too, so a 304 carrying a body would show."""
    clock = _Clock()
    async with _fetcher(_always(200, content=b"seven!!"), clock) as f:
        assert f.total_requests == 0
        assert f.total_bytes == 0
        await f.get(f"https://{FAST}/3/movie/603")
        assert f.total_requests == 1
        assert f.total_bytes == 7
        assert f.total_errors == 0


async def test_robots_txt_is_cached_in_postgres_and_read_back_on_the_next_drain(db):
    """The worker restarts, so a fresh fetcher over the same database must refuse from the cache unasked."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, content=b"page")

    clock = _Clock()
    async with _fetcher(handler, clock, conn=db) as first:
        assert (await first.get("https://policy.example/public")).status == 200

    row = await db.fetchrow("SELECT * FROM fetch_host_state WHERE host = 'policy.example'")
    assert row["robots_status"] == 200
    assert "Disallow: /private" in row["robots_txt"]
    assert row["robots_fetched_at"] is not None
    # Two: the robots.txt fetch is an outbound request and is counted like the page.
    assert row["requests"] == 2, "the drain's counters are flushed once, on the way out"

    seen.clear()
    async with _fetcher(handler, _Clock(), conn=db) as second:
        with pytest.raises(RobotsDisallowed):
            await second.get("https://policy.example/private")
        assert second.total_requests == 0
    assert seen == [], "a cached robots.txt is a refusal that costs the host nothing"


async def test_the_breakers_refusal_survives_the_worker_it_opened_in(db):
    """`paused_until` is written when the breaker opens, since that process may never reach its shutdown
    path. Stored as an interval against Postgres's `now()`, so no wall clock meets the monotonic one."""
    async with _fetcher(_always(500), _Clock(), conn=db) as first:
        for _ in range(8):
            with pytest.raises(FetchError):
                await first.get(f"https://{SECOND}/movies/x", max_attempts=1)

    remaining = await db.fetchval(
        "SELECT EXTRACT(EPOCH FROM (paused_until - now())) FROM fetch_host_state WHERE host = $1",
        SECOND,
    )
    assert remaining is not None and 240 < float(remaining) <= 300

    async with _fetcher(_always(200), _Clock(), conn=db) as second:
        with pytest.raises(HostPaused) as caught:
            await second.get(f"https://{SECOND}/movies/x", max_attempts=1)
        assert second.total_requests == 0
    assert caught.value.remaining > 240


async def test_a_stored_validator_conditions_the_next_request_and_a_304_costs_no_bytes(db):
    """Validators come from the newest SUCCESSFUL row, hence a newer failed row with another ETag."""
    url = f"https://{FAST}/3/movie/603"
    for fetched_at, ok, etag in (("now() - interval '2 days'", True, '"good-etag"'),
                                 ("now() - interval '1 hour'", False, '"error-page-etag"')):
        await db.execute(
            "INSERT INTO raw_document (source, kind, url, content_sha256, content_path, "
            "                          etag, last_modified, ok, fetched_at) "
            f"VALUES ('tmdb', 'detail', $1, 'sha', 'raw/ab/sha.gz', $2, $3, $4, {fetched_at})",
            url, etag, "Wed, 21 Oct 2026 07:28:00 GMT", ok,
        )

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("if-none-match", ""))
        return httpx.Response(304)

    async with _fetcher(handler, _Clock(), conn=db) as f:
        response = await f.get(url, conditional=True)
        assert f.total_bytes == 0, "a 304 transfers nothing; that is what makes it worth asking"
    assert sent == ['"good-etag"'], "the newest SUCCESSFUL row supplies the validator"
    assert response.status == 304
    assert response.from_cache is True
    assert response.content == b""


async def test_a_robots_txt_the_host_could_not_serve_refuses_rather_than_allowing():
    """RFC 9309 §2.3.1.4: an unreachable robots.txt means
    "complete disallow"; retryable, so a drain asks again."""
    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    for name, handler in (
        ("a transport failure", unreachable),
        ("a 500", lambda r: httpx.Response(500, text="oops")),
        ("a 503", lambda r: httpx.Response(503, text="maintenance")),
    ):
        async with _fetcher(handler, _Clock()) as f:
            with pytest.raises(RobotsUnavailable) as caught:
                await f.get("https://unmeasured.example/private/thing")
            assert caught.value.retryable is True, name
            assert f.total_requests == 1, f"{name}: only the robots.txt went out"


async def test_a_host_that_published_no_rules_is_still_crawlable():
    """RFC 9309 §2.3.1.3: any 4xx on robots.txt is "unavailable", so the host stays crawlable."""
    for status in (404, 403, 401, 410):
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(status, text="nope")
            return httpx.Response(200, content=b"page")

        async with _fetcher(handler, _Clock()) as f:
            assert (await f.get("https://unmeasured.example/title/1")).status == 200


async def test_a_robots_failure_is_not_cached_as_permission(db):
    """A failed robots fetch leaves no cached row, or it
    pins allow-all for 24 h and clobbers a `Disallow`."""
    host = "pinned.example"

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("the host blipped")
        return httpx.Response(200, content=b"page")

    async with _fetcher(broken, _Clock(), conn=db) as first:
        with pytest.raises(RobotsUnavailable):
            await first.get(f"https://{host}/private/thing")

    row = await db.fetchrow("SELECT * FROM fetch_host_state WHERE host = $1", host)
    assert row is None or row["robots_fetched_at"] is None, (
        "a request that never landed was written into the cache as this host's robots.txt"
    )

    asked: list[str] = []

    def strict(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, content=b"page")

    async with _fetcher(strict, _Clock(), conn=db) as second:
        with pytest.raises(RobotsDisallowed):
            await second.get(f"https://{host}/private/thing")
    assert asked == ["/robots.txt"], "the next drain never re-asked; the blip was the cache"


async def test_a_redirect_is_gated_by_the_host_it_goes_to():
    """A hop is a request: the target's robots, policy and bucket apply, same-host hops included."""
    def cross_host(request: httpx.Request) -> httpx.Response:
        if request.url.host == "start.example":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(302, headers={"location": "https://elsewhere.example/secret"})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, content=b"SECRET BODY")

    async with _fetcher(cross_host, _Clock()) as f:
        with pytest.raises(RobotsDisallowed) as caught:
            await f.get("https://start.example/go")
        assert "elsewhere.example" in caught.value.url
        report = {row["host"]: row for row in f.host_report()}
        assert set(report) == {"start.example", "elsewhere.example"}, (
            "the redirect target never got a runtime, so its policy could not apply"
        )
        assert report["start.example"]["requests"] == 2, "robots.txt and the 302 both went out"

    def same_host(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
        if request.url.path == "/m/wrong-slug":
            return httpx.Response(302, headers={"location": "/search/?q=wrong-slug"})
        return httpx.Response(200, content=b"THE SEARCH PAGE")

    async with _fetcher(same_host, _Clock()) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get("https://unmeasured.example/search/?q=x")
        with pytest.raises(RobotsDisallowed):
            await f.get("https://unmeasured.example/m/wrong-slug")


async def test_a_redirect_chain_is_capped_rather_than_followed_to_exhaustion():
    """httpx's default twenty hops is a browser's number; past the cap is a refusal, not a 3xx body."""
    def loops(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        n = int(request.url.params.get("n", "0"))
        return httpx.Response(302, headers={"location": f"/go?n={n + 1}"})

    async with _fetcher(loops, _Clock()) as f:
        with pytest.raises(FetchError) as caught:
            await f.get("https://unmeasured.example/go?n=0")
        assert f"more than {MAX_REDIRECTS} redirects" in str(caught.value)


async def test_a_port_the_scheme_already_implies_is_the_same_host():
    """`netloc` keeps port and userinfo; `:443` must neither
    miss the policy row nor key a second runtime."""
    assert normalise_host("www.rottentomatoes.com:443") == "www.rottentomatoes.com"
    assert normalise_host("WWW.Rottentomatoes.COM") == "www.rottentomatoes.com"
    assert normalise_host("anyuser@www.rottentomatoes.com") == "www.rottentomatoes.com"
    assert normalise_host("host:80", scheme="http") == "host"
    # A non-default port is part of the host: two services on one box must not share an exemption.
    assert normalise_host("jelly.lan:8096") == "jelly.lan:8096"
    # Both sides of the Jellyfin comparison go through the same normalisation.
    assert policy_for(normalise_host("JELLY.lan:8096"), jellyfin_host="jelly.lan:8096").rps == 8.0
    assert policy_for(normalise_host("evil.jelly.lan"), jellyfin_host="jelly.lan").rps == 1.0

    # The trailing root dot is a third spelling of one host;
    # unnormalised it escapes the table and its guard.
    assert normalise_host("www.rottentomatoes.com.") == "www.rottentomatoes.com"
    assert normalise_host("WWW.Rottentomatoes.COM.:443") == "www.rottentomatoes.com"
    assert normalise_host("[::1]") == "[::1]", "an IPv6 literal carries no root label to strip"
    assert normalise_host(".") == ".", "a netloc that is nothing but the root label is not a host"
    dotted = policy_for(
        normalise_host("www.rottentomatoes.com."), jellyfin_host="www.rottentomatoes.com."
    )
    assert (dotted.rps, dotted.respect_robots) == (0.7, True), (
        "a declared host spelled with the root label took the Jellyfin exemption: section 8 lets "
        "a VALUE widen nothing"
    )

    clock = _Clock()
    host = "www.rottentomatoes.com"
    async with _fetcher(_always(200, content=b"ok"), clock) as f:
        await f.get(f"https://{host}/m/a")
        await f.get(f"https://{host}:443/m/b")
        assert list(f._hosts) == [host], "one physical host, two buckets and two breakers"
        # Two waits for three requests: the robots.txt is charged to the same bucket as the pages.
        assert clock.slept == [pytest.approx(1 / 0.7), pytest.approx(1 / 0.7)], (
            "robots.txt and both pages are three requests to one host at its declared pace"
        )
        assert f._hosts[host].policy.rps == 0.7


async def test_a_decoding_failure_is_a_fetch_error_and_counts_against_the_host():
    """httpx's `DecodingError` and `TooManyRedirects` are not
    `TransportError`, yet must surface as `FetchError`."""
    def truncated(_request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("gzip stream ends early")

    clock = _Clock()
    async with _fetcher(truncated, clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=2)
        assert "DecodingError" in str(caught.value)
        assert f.total_errors == 2, "both attempts counted against the host"
        assert f._hosts[FAST].consecutive_failures == 2


async def test_allow_status_does_not_buy_an_unpaced_429():
    """`allow_status` makes a status readable, not unpaced: the pause and breaker count come first."""
    clock = _Clock()
    async with _fetcher(_always(429, headers={"Retry-After": "600"}), clock) as f:
        response = await f.get(f"https://{FAST}/3/movie/603", max_attempts=2,
                               allow_status=(429,))
        assert response.status == 429, "the caller named this status and still gets the answer"
        assert clock.slept == [pytest.approx(300.0)], "Retry-After, capped at five minutes"
        assert f.total_errors == 2
        assert f._hosts[FAST].consecutive_failures == 2, (
            "the breaker was told the host succeeded on the status that says it is overloaded"
        )


async def test_a_caller_may_not_replace_the_user_agent():
    """httpx lets a per-request header win; a replaced agent
    would be judged under a name it no longer sends."""
    clock = _Clock()
    async with _fetcher(_always(200, content=b"ok"), clock) as f:
        for spelling in ("User-Agent", "user-agent"):
            with pytest.raises(ValueError, match="User-Agent"):
                await f.get(f"https://{FAST}/3/movie/603", headers={spelling: "Googlebot/2.1"})
        assert f.total_requests == 0, "nothing went out under the wrong name"


async def test_the_run_of_failures_a_host_is_on_survives_the_drain_that_counted_it(db):
    """The failure run is read back across drains, or a host
    failing seven times a drain against eight never trips."""
    async with _fetcher(_always(500), _Clock(), conn=db) as first:
        for _ in range(7):
            with pytest.raises(FetchError):
                await first.get(f"https://{SECOND}/movies/x", max_attempts=1)
        assert first._hosts[SECOND].consecutive_failures == 7

    stored = await db.fetchval(
        "SELECT consecutive_failures FROM fetch_host_state WHERE host = $1", SECOND
    )
    assert stored == 7, "the run this drain was on is not a fact the next drain can rebuild"

    async with _fetcher(_always(500), _Clock(), conn=db) as second:
        with pytest.raises(FetchError):
            await second.get(f"https://{SECOND}/movies/x", max_attempts=1)
        assert second._hosts[SECOND].paused_until > 0, (
            "the eighth consecutive failure did not open the breaker; the count restarted"
        )


async def test_a_robots_txt_the_host_only_redirected_is_not_an_answer(db):
    """RFC 9309 §2.3.1.2: a 3xx on robots.txt is not the file; refused, and not cached as allow-all."""
    for status in (301, 302, 307, 308):
        host = f"redirected-{status}.example"

        def hop(request: httpx.Request, status=status) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(status, headers={"location": "/robots.txt.html"},
                                      text="<html>Moved</html>")
            return httpx.Response(200, content=b"THE PAGE BODY")

        async with _fetcher(hop, _Clock(), conn=db) as f:
            with pytest.raises(RobotsUnavailable) as caught:
                await f.get(f"https://{host}/anything")
            assert caught.value.retryable is True, status
            assert f.total_requests == 1, f"{status}: only the robots.txt went out"

        row = await db.fetchrow("SELECT * FROM fetch_host_state WHERE host = $1", host)
        assert row is None or row["robots_fetched_at"] is None, (
            f"a {status} on robots.txt was cached as this host's published rules"
        )


async def test_the_request_counter_counts_a_request_that_went_out_and_failed():
    """A request that went out and failed is still one request, counted once, on every path."""
    clock = _Clock()
    async with _fetcher(_always(503), clock) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=4)
        assert f.total_requests == 4, "four attempts were put on the wire and all four failed"
        assert f.total_errors == 4
        report = {row["host"]: row for row in f.host_report()}
        assert report[FAST]["requests"] == 4, "the durable per-host column says the same thing"
        assert report[FAST]["errors"] == 4, "errors are a subset of requests, not a second class"


async def test_a_published_crawl_delay_is_honoured_rather_than_the_apps_own_number(db):
    """`Crawl-delay` and `Request-rate` are honoured from the
    fetch and the cache; the declared row is the ceiling."""
    def publisher(rules: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=rules)
            return httpx.Response(200, content=b"page")
        return handler

    slow = publisher("User-agent: *\nCrawl-delay: 30\n")
    clock = _Clock()
    async with _fetcher(slow, clock, conn=db) as f:
        for n in range(6):
            assert (await f.get(f"https://slowblog.example/review/{n}")).status == 200
    assert clock.slept == [pytest.approx(30.0)] * 5, (
        "six pages behind a published Crawl-delay of 30 cost 150 seconds, not 4"
    )

    # The same rules from the Postgres cache, with no robots request.
    cached = _Clock()
    async with _fetcher(slow, cached, conn=db) as f:
        for n in range(3):
            await f.get(f"https://slowblog.example/review/{n}")
        assert f.total_requests == 3, "the robots.txt came out of the cache"
    assert cached.slept == [pytest.approx(30.0)] * 2

    # `Request-rate: 1/60` is the other spelling; the stricter of the two wins.
    rate = _Clock()
    async with _fetcher(publisher("User-agent: *\nRequest-rate: 1/60\nCrawl-delay: 5\n"), rate) as f:
        await f.get("https://rated.example/a")
        await f.get("https://rated.example/b")
    assert rate.slept == [pytest.approx(60.0)], "the stricter of the two directives"

    # A host asking to be crawled faster than its declared row does not get it.
    eager = _Clock()
    async with _fetcher(publisher("User-agent: *\nCrawl-delay: 0.05\n"), eager) as f:
        for n in range(3):
            await f.get(f"https://unmeasured.example/{n}")
    assert eager.slept == [pytest.approx(1.0)] * 2, "DEFAULT_RPS is a ceiling a host cannot raise"

    # The robots.txt spends the burst of one, so the page after it waits the delay.
    tight = _Clock()
    async with _fetcher(publisher("User-agent: *\nCrawl-delay: 10\n"), tight) as f:
        for path in ("/m/a", "/m/b"):
            await f.get(f"https://www.rottentomatoes.com{path}")
    assert tight.slept == [pytest.approx(10.0)] * 2, (
        "robots.txt is a request to this host, and a burst of 1 has no token left after it"
    )


async def test_a_robots_txt_larger_than_the_parsing_limit_is_cut_between_lines(db):
    """RFC 9309 §2.5: the parse is capped and cut between lines, since a cut mid-line relaxes a rule.
    Measured in bytes, not characters: non-ASCII text can fit one and not the other."""
    ascii_line = "# padding that no crawler is obliged to read"
    # Each umlaut is one character and two bytes, so the characters fit and the bytes do not.
    latin_line = "# keine Beruecksichtigung: \u00e4\u00f6\u00fc\u00df" * 3
    plain = f"{ascii_line}\n" * 30_000
    latin = f"{latin_line}\n" * 5_200
    assert len(plain) > ROBOTS_MAX_BYTES, "this test needs a body past the limit"
    assert len(latin) < ROBOTS_MAX_BYTES < len(latin.encode("utf-8")), (
        "the non-ASCII arm must be short in characters and long in bytes, or it proves nothing"
    )

    for host, padding, last in (
        ("enormous.example", plain, ascii_line), ("umlaut.example", latin, latin_line)
    ):
        def enormous(request: httpx.Request, padding=padding) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=f"User-agent: *\nDisallow: /private\n{padding}")
            return httpx.Response(200, content=b"page")

        async with _fetcher(enormous, _Clock(), conn=db) as f:
            assert (await f.get(f"https://{host}/public")).status == 200
            with pytest.raises(RobotsDisallowed):
                await f.get(f"https://{host}/private/thing")

        stored = await db.fetchval("SELECT robots_txt FROM fetch_host_state WHERE host = $1", host)
        assert len(stored.encode("utf-8")) <= ROBOTS_MAX_BYTES, (
            f"{host}: a third party decided how large a column this household's database carries"
        )
        assert stored.splitlines()[-1] == last, (
            f"{host}: the cut landed inside a line rather than between two"
        )
        assert "Disallow: /private" in stored


async def test_a_redirect_this_layer_cannot_follow_is_refused_rather_than_parsed():
    """A 3xx with nothing to follow (no `Location`, a 300,
    an unconditioned 304) is refused, not answered."""
    def stuck(status: int):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(status, content=b"<html>Moved Permanently</html>")
        return handler

    for status in (301, 302, 303, 307, 308, 300, 304):
        async with _fetcher(stuck(status), _Clock()) as f:
            with pytest.raises(FetchError) as caught:
                await f.get("https://waf.example/m/whiplash", max_attempts=2)
            assert caught.value.status == status
            assert "no usable redirect" in str(caught.value)
            assert caught.value.retryable is True, "an edge that emits one bad hop may not tomorrow"

    async with _fetcher(stuck(301), _Clock()) as f:
        named = await f.get("https://waf.example/m/whiplash", allow_status=(301,))
        assert named.status == 301, "`allow_status` still names a status a caller can read"


# Event-loop turns the gate stays shut, so every task reaches it before any is let go.
_SETTLE_TURNS = 64


async def test_a_host_is_held_to_the_concurrency_it_declares_and_not_only_to_its_rate():
    """The bucket paces over time, not concurrency; its burst
    covers every request, so only the semaphore holds."""
    policy = HOST_POLICIES[FAST]
    assert policy.burst > policy.max_concurrency, (
        "this test needs a host whose bucket permits more at once than its semaphore does; "
        f"{FAST} declares burst {policy.burst} and concurrency {policy.max_concurrency}"
    )
    in_flight = peak = 0
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await gate.wait()
        in_flight -= 1
        return httpx.Response(200, content=b"ok")

    async def open_the_gate() -> None:
        for _ in range(_SETTLE_TURNS):
            await asyncio.sleep(0)
        gate.set()

    clock = _Clock()
    async with _fetcher(handler, clock) as f:
        await asyncio.gather(open_the_gate(), *(
            f.get(f"https://{FAST}/3/movie/{n}") for n in range(policy.burst)
        ))
        assert clock.slept == [], "the burst covers every request, so nothing here was paced"
        assert f.total_requests == policy.burst
    assert peak == policy.max_concurrency, (
        f"{policy.burst} tasks reached {FAST} and {peak} of them were on the wire at once, "
        f"against the {policy.max_concurrency} that host declares"
    )



async def test_a_hop_into_another_origin_does_not_carry_the_callers_credentials():
    """A cross-origin hop drops the caller's headers;
    same-origin hops and http->https upgrades keep them."""
    creds = {
        "Authorization": "Bearer SECRET-TMDB-TOKEN",
        "trakt-api-key": "SECRET-TRAKT-CLIENT-ID",
        "X-Emby-Token": "SECRET-JELLYFIN-KEY",
        "Cookie": "session=SECRET-SESSION",
    }
    leaked = [name.lower() for name in creds]

    def carrying(request: httpx.Request) -> list[str]:
        return [name for name in leaked if name in request.headers]

    seen: list[tuple[str, list[str]]] = []

    def across(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), carrying(request)))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://elsewhere.example/detail"})
        return httpx.Response(200, content=b"{}")

    async with _fetcher(across, _Clock()) as f:
        assert (await f.get("https://start.example/go", headers=creds)).status == 200
    sent = dict(seen)
    assert sent["https://start.example/go"] == leaked, (
        "the control failed: the credentials never reached the first host either"
    )
    assert sent["https://elsewhere.example/detail"] == [], (
        "a redirect delivered the household's provider keys to a host it never chose to trust"
    )
    assert sent["https://elsewhere.example/robots.txt"] == []

    kept: list[list[str]] = []

    def within(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        kept.append(carrying(request))
        if request.url.path == "/m/wrong-slug":
            return httpx.Response(302, headers={"location": "/m/right-slug"})
        return httpx.Response(200, content=b"THE PAGE")

    async with _fetcher(within, _Clock()) as f:
        await f.get("https://onehost.example/m/wrong-slug", headers=creds)
    assert kept == [leaked, leaked], (
        "a same-host redirect is the same party, and a stage that lost its key there would "
        "read every slug miss as a 401"
    )

    upgraded: list[list[str]] = []

    def to_https(request: httpx.Request) -> httpx.Response:
        upgraded.append(carrying(request))
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": f"https://{FAST}/3/movie/603"})
        return httpx.Response(200, content=b"{}")

    async with _fetcher(to_https, _Clock()) as f:
        await f.get(f"http://{FAST}/3/movie/603", headers=creds)
    assert upgraded == [leaked, leaked], (
        "a host upgrading its own http to https is httpx's own carve-out and not a third party"
    )


async def test_a_hop_into_a_paused_host_is_refused_before_its_robots_txt_is_asked_for():
    """A hop into a paused host is refused before its robots.txt is asked, and reported as a pause."""
    wire: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(str(request.url))
        if request.url.host == "start.example":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(302, headers={"location": "https://smallblog.example/review/x"})
        return httpx.Response(503, text="maintenance")

    clock = _Clock()
    async with _fetcher(handler, clock) as f:
        paused = await f._runtime("smallblog.example")
        paused.paused_until = clock() + 900.0

        with pytest.raises(HostPaused) as caught:
            await f.get("https://start.example/go")
        assert caught.value.host == "smallblog.example"
        assert [url for url in wire if "smallblog.example" in url] == [], (
            "a host in a 900-second cooldown was asked for a file anyway"
        )
        report = {row["host"]: row for row in f.host_report()}
        assert report["smallblog.example"]["requests"] == 0


async def test_a_robots_honouring_host_is_not_reached_over_plain_http():
    """RFC 9309 §2.3 scopes robots.txt by scheme but the cache is per host, so plain http is refused."""
    wire: list[str] = []

    def either(request: httpx.Request) -> httpx.Response:
        wire.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=b"page")

    async with _fetcher(either, _Clock()) as f:
        with pytest.raises(FetchError) as caught:
            await f.get("http://twoschemes.example/review/x")
        assert caught.value.retryable is False
        assert "https" in str(caught.value)
        assert wire == [], "the refusal has to come before the request it is refusing"

        assert (await f.get(f"http://{FAST}/3/movie/603")).status == 200, (
            "a host whose policy turns robots off has no rules to conflate"
        )


async def test_a_robots_txt_is_bounded_on_the_wire_and_not_only_after_it_is_parsed():
    """The robots body is bounded while streaming: a small
    gzip must not decompress to 70 MB before the cut."""
    chunk = b"# padding that no crawler is obliged to read\n" * 1500
    served: list[int] = []

    async def enormous_body():
        yield b"User-agent: *\nDisallow: /private\n"
        for _ in range(64):
            served.append(len(chunk))
            yield chunk

    def enormous(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=enormous_body())
        return httpx.Response(200, content=b"page")

    async with _fetcher(enormous, _Clock()) as f:
        assert (await f.get("https://enormous.example/public")).status == 200
        with pytest.raises(RobotsDisallowed):
            await f.get("https://enormous.example/private/thing")

    consumed = sum(served)
    assert consumed < 2 * ROBOTS_MAX_BYTES, (
        f"a stranger put {consumed} bytes through this worker's heap for a file the app reads "
        f"at most {ROBOTS_MAX_BYTES} of"
    )
    assert consumed >= ROBOTS_MAX_BYTES - len(chunk), (
        "the reader stopped short of the limit, so the rules before it may have been cut"
    )


async def test_the_validator_is_keyed_on_the_url_the_request_was_made_against(db):
    """The validator is keyed on the url with its query, carried back as `Response.request_url`."""
    url = f"https://{FAST}/3/movie/603"
    await db.execute(
        "INSERT INTO raw_document (source, kind, url, content_sha256, content_path, "
        "                          etag, ok, fetched_at) "
        "VALUES ('tmdb', 'detail', $1, 'sha', 'raw/ab/sha.gz', $2, true, now())",
        f"{url}?append_to_response=credits", '"credits-etag"',
    )

    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.headers.get("if-none-match", "")))
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, content=b"{}")

    async with _fetcher(handler, _Clock(), conn=db) as f:
        same = await f.get(url, params={"append_to_response": "credits"}, conditional=True)
        other = await f.get(url, params={"append_to_response": "images"}, conditional=True)

    assert sent[0] == (f"{url}?append_to_response=credits", '"credits-etag"'), (
        "the validator was read under a url this app never requested"
    )
    assert same.status == 304 and same.from_cache is True
    assert same.request_url == f"{url}?append_to_response=credits", (
        "the answer does not carry the string a caller must store it under"
    )

    assert sent[1] == (f"{url}?append_to_response=images", ""), (
        "one document's ETag conditioned a request for a different document"
    )
    assert other.status == 200
    assert other.request_url == f"{url}?append_to_response=images"


async def test_a_credential_parameter_is_sent_but_never_part_of_the_url_a_caller_stores():
    """TMDB's `api_key` and OMDb's `apikey` reach the provider; the url filed in raw_document masks them."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, content=b"{}")

    async with _fetcher(handler, _Clock()) as f:
        tmdb = await f.get(f"https://{FAST}/3/movie/603", params={"api_key": "SEKRIT-tmdb"})
        omdb = await f.get(f"https://{FAST}/", params={"apikey": "SEKRIT-omdb", "i": "tt0133093"})

    assert sent == [
        f"https://{FAST}/3/movie/603?api_key=SEKRIT-tmdb",
        f"https://{FAST}/?apikey=SEKRIT-omdb&i=tt0133093",
    ], "the provider must still receive its key"
    for answer in (tmdb, omdb):
        assert "SEKRIT" not in answer.request_url, answer.request_url
    assert omdb.request_url == f"https://{FAST}/?apikey=REDACTED&i=tt0133093"


async def test_the_report_says_what_the_drain_did_after_the_context_has_closed(db):
    """`host_report()` is read after the context closes, so the flush writes a delta and zeroes nothing."""
    async with _fetcher(_always(200, content=b"{}"), _Clock(), conn=db) as f:
        await f.get(f"https://{FAST}/3/movie/603")
        await f.get(f"https://{FAST}/3/movie/604")
        inside = {row["host"]: row for row in f.host_report()}
        assert inside[FAST]["requests"] == 2
        await f.persist_host_state()
        await f.get(f"https://{FAST}/3/movie/605")

    outside = {row["host"]: row for row in f.host_report()}
    assert outside[FAST]["requests"] == 3, (
        "the report answered a different question after the block than inside it"
    )
    stored = await db.fetchval("SELECT requests FROM fetch_host_state WHERE host = $1", FAST)
    assert stored == 3, "a mid-drain flush and the one on the way out double-counted the requests"


async def test_the_report_carries_the_rate_actually_in_use_and_not_only_the_declared_one():
    """The report shows the rate in use after `Crawl-delay`, not only the declared ceiling."""
    host = "www.metacritic.com"

    def publisher(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
        return httpx.Response(200, content=b"page")

    async with _fetcher(publisher, _Clock()) as f:
        await f.get(f"https://{host}/kritik/x")
        row = {r["host"]: r for r in f.host_report()}[host]

    assert row["rps"] == HOST_POLICIES[host].rps == 0.7, "the declared ceiling is still published"
    assert row["effective_rps"] == pytest.approx(1 / 30), (
        "the board would tell an operator this host is crawled twenty times faster than it is"
    )


class _BreaksMidBody(httpx.AsyncByteStream):
    """httpx's `stream()` returns after the headers, so the status is already 200 when the body fails."""

    def __init__(self, first: bytes, exc: Exception) -> None:
        self._first, self._exc = first, exc

    async def __aiter__(self):
        yield self._first
        raise self._exc


async def test_a_robots_txt_whose_body_never_arrives_is_not_a_200(db):
    """RFC 9309 §2.3.1.4: a 200 whose body never arrived is unreachable, refused, and not cached."""
    strict = b"User-agent: *\nDisallow: /\n"

    for name, exc in (
        ("a reset", httpx.RemoteProtocolError("peer closed connection mid-body")),
        ("a corrupt gzip", httpx.DecodingError("invalid deflate stream")),
        ("a read timeout", httpx.ReadTimeout("timed out reading the body")),
    ):
        host = f"halfbody-{len(name)}.example"
        asked: list[str] = []

        def broken(request: httpx.Request, exc=exc, asked=asked) -> httpx.Response:
            asked.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, stream=_BreaksMidBody(strict[:8], exc))
            return httpx.Response(200, content=b"THE DISALLOWED PAGE")

        async with _fetcher(broken, _Clock(), conn=db) as f:
            with pytest.raises(RobotsUnavailable) as caught:
                await f.get(f"https://{host}/private/thing")
            assert caught.value.retryable is True, name
            assert f.total_errors == 1, f"{name}: the board shows a host with no errors"
        assert asked == ["/robots.txt"], f"{name}: the disallowed page went on the wire"

        row = await db.fetchrow("SELECT * FROM fetch_host_state WHERE host = $1", host)
        assert row is None or row["robots_fetched_at"] is None, (
            f"{name}: a body that never arrived was cached as this host's published rules"
        )

    # The control: the same file, whole, is obeyed and cached.
    def intact(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=strict)
        return httpx.Response(200, content=b"THE DISALLOWED PAGE")

    async with _fetcher(intact, _Clock(), conn=db) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get("https://wholebody.example/private/thing")
    stored = await db.fetchval(
        "SELECT robots_txt FROM fetch_host_state WHERE host = 'wholebody.example'"
    )
    assert stored == strict.decode(), stored


async def test_the_robots_decision_is_taken_against_the_url_the_request_is_made_against():
    """`can_fetch` matches on the query, so robots is judged against the url with `params=` merged in."""
    def rules(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /wp-json/wp/v2/posts?per_page=100\n")
        return httpx.Response(200, content=b"page")

    posts = "https://blog.invalid/wp-json/wp/v2/posts"
    async with _fetcher(rules, _Clock()) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get(f"{posts}?per_page=100")
        with pytest.raises(RobotsDisallowed) as caught:
            await f.get(posts, params={"per_page": 100})
        assert "per_page=100" in str(caught.value), str(caught.value)

        # The same endpoint under an allowed query is still fetched.
        assert (await f.get(f"{posts}?per_page=10")).status == 200
        answer = await f.get(posts, params={"per_page": 10})
        assert answer.status == 200
        assert answer.request_url == f"{posts}?per_page=10"


async def test_a_robots_cache_timestamped_in_the_future_is_not_a_cache(db):
    """A row timestamped in the future has a negative age, which would pass the TTL test indefinitely."""
    host = "skewed.example"
    asked: list[str] = []

    def strict(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, content=b"page")

    for label, offset in (("a fresh row", "-1 hour"), ("a future row", "100 years")):
        await db.execute(
            "INSERT INTO fetch_host_state (host, robots_txt, robots_status, robots_fetched_at) "
            "VALUES ($1, '', 200, now() + $2::text::interval) "
            "ON CONFLICT (host) DO UPDATE SET robots_txt = excluded.robots_txt, "
            "  robots_status = excluded.robots_status, "
            "  robots_fetched_at = excluded.robots_fetched_at",
            host, offset,
        )
        asked.clear()
        async with _fetcher(strict, _Clock(), conn=db) as f:
            if label == "a fresh row":
                # The control: a row in the past IS a cache, so no robots request goes out.
                assert (await f.get(f"https://{host}/private/thing")).status == 200
                assert asked == ["/private/thing"], asked
            else:
                with pytest.raises(RobotsDisallowed):
                    await f.get(f"https://{host}/private/thing")
                assert asked == ["/robots.txt"], (
                    "a timestamp the database cannot have written was read as a valid cache"
                )


async def test_a_robots_txt_whose_first_line_outruns_the_limit_is_not_an_answer(db):
    """A first line past the limit leaves nothing after the cut: a non-answer, refused and not cached."""
    host = "onelongline.example"
    asked: list[str] = []

    def one_long_line(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="# " + "x" * (ROBOTS_MAX_BYTES + 1000))
        return httpx.Response(200, content=b"THE DISALLOWED PAGE")

    async with _fetcher(one_long_line, _Clock(), conn=db) as f:
        with pytest.raises(RobotsUnavailable) as caught:
            await f.get(f"https://{host}/private/thing")
        assert caught.value.retryable is True
        assert f.total_errors == 1, "the board shows a host with no errors"
    assert asked == ["/robots.txt"], "the page went out under rules this app never read"

    row = await db.fetchrow("SELECT * FROM fetch_host_state WHERE host = $1", host)
    assert row is None or row["robots_fetched_at"] is None, (
        "a file this app read no rule out of was pinned as this host's published rules for a day"
    )

    # Control: an empty file was read in full, so allow-all is the host's own answer.
    def silent(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, content=b"page")

    async with _fetcher(silent, _Clock(), conn=db) as f:
        assert (await f.get("https://silent.example/anything")).status == 200
    assert await db.fetchval(
        "SELECT robots_status FROM fetch_host_state WHERE host = 'silent.example'"
    ) == 200

    # Control: a rule before the cut is still obeyed.
    def rules_then_padding(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            padding = "# padding no crawler must read\n" * 30_000
            return httpx.Response(200, text=f"User-agent: *\nDisallow: /private\n{padding}")
        return httpx.Response(200, content=b"page")

    async with _fetcher(rules_then_padding, _Clock(), conn=db) as f:
        with pytest.raises(RobotsDisallowed):
            await f.get("https://cutlater.example/private/thing")
        assert (await f.get("https://cutlater.example/public")).status == 200


async def test_a_negative_retry_after_does_not_delete_the_backoff_curve():
    """A negative `Retry-After` must not become a zero sleep on any retryable status."""
    clock = _Clock()
    async with _fetcher(_always(503, headers={"Retry-After": "-1"}), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=3)
    assert caught.value.status == 503
    assert clock.slept == [pytest.approx(1.6 * 0.7), pytest.approx(3.2 * 0.7)], (
        "a host chose this fetcher's backoff curve away with one header"
    )

    # `nan` reached `asyncio.sleep`, which raises `ValueError` rather than a `FetchError`.
    nan_clock = _Clock()
    async with _fetcher(_always(503, headers={"Retry-After": "nan"}), nan_clock) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/604", max_attempts=2)
    assert nan_clock.slept == [pytest.approx(1.6 * 0.7)]


@pytest.mark.parametrize(("failure", "posts"), [
    (httpx.ReadTimeout, 1), (httpx.RemoteProtocolError, 1), (503, 1), (504, 1), (520, 1),
    (httpx.ConnectError, 3), (httpx.PoolTimeout, 3), (429, 3), (408, 3),
])
async def test_a_post_is_sent_again_only_when_it_provably_never_reached_the_host(failure, posts):
    """RFC 9110 §9.2.2: a POST (a paid generation) is re-sent only on a connect failure or 408/425/429."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("no answer", request=request)

    async with _fetcher(handler, _Clock()) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/list", method="POST", json_body={"a": 1}, max_attempts=3)
    assert sent == ["POST"] * posts

    sent.clear()
    async with _fetcher(handler, _Clock()) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=3)
    assert sent == ["GET"] * 3, "a GET is re-sent exactly as before"


async def test_a_server_error_outside_the_retry_set_counts_toward_the_breaker():
    """A 5xx outside the retry set (Anthropic's 529) is not retried but counts toward the breaker."""
    async with _fetcher(_always(529), _Clock()) as f:
        for _ in range(2):
            with pytest.raises(FetchError) as caught:
                await f.get(f"https://{FAST}/3/movie/603")
            assert caught.value.retryable is False
        [overloaded] = f.host_report()
    assert (overloaded["requests"], overloaded["errors"]) == (2, 2)

    async with _fetcher(_always(410), _Clock()) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/603")
        [gone] = f.host_report()
    assert (gone["requests"], gone["errors"]) == (1, 0)
