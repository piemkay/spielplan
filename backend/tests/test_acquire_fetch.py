"""The polite per-host fetcher, asserted as arithmetic. Spec v2.1 §8, decision 340.

§8 says the fetcher "is polite, and says so in a form that can be failed" (`spec:404`), which is
the whole reason decision 340 exists: politeness that is only claimed in a docstring cannot be
reviewed. So this file asserts the numbers rather than the intent - how long a 429 costs, how far
the backoff curve climbs, how many requests a paced host is allowed, and which host a breaker
takes down.

**Nothing here reaches the network.** Every request is served by an `httpx.MockTransport` double
in the test that needs it, and the clock is injected, so a test that asserts a two-minute pause
runs in microseconds and asserts the pause exactly rather than approximately. That is the point:
`asyncio.sleep`ing for real would make the difference between a 120-second pause and a 1.12-second
one invisible, and that difference is the defect this layer exists to prevent
(`docs/milestones/M5.1-plan.md` §8: M5.1 fetches nothing real).

Three tests take the integration layer, because three of the fetcher's promises are promises about
Postgres and cannot be asserted anywhere else: the robots cache that decision 340 puts in the
database, the breaker's refusal surviving the worker that opened it, and the conditional request
built from the previous `raw_document` row. Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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
    declared_policies,
    normalise_host,
    policy_for,
    undocumented_overrides,
)

# Two hosts whose declared policy turns robots off, so a test about pacing is about pacing: with
# `respect_robots=True` the fetcher's first act on a new host is to read its robots.txt, which is a
# request the test would have to serve and then subtract from every count it makes.
FAST = "api.themoviedb.org"       # rps 18, burst 20, threshold 8, cooldown 300
SECOND = "api.trakt.tv"           # rps 2.5, burst 3 - a different host, which is the whole point


class _Clock:
    """A clock that moves only when something sleeps on it.

    Injected in place of `time.monotonic` and `asyncio.sleep` together, because the two are one
    fiction: the assertion every pacing test makes is "it slept for exactly this long", and the
    bucket's refill arithmetic then has to see that time actually passed.
    """

    # A bucket that cannot satisfy its own wait does not fail, it spins, and a spinning test is a
    # hung suite with no message - which is how this file first ran. The bound turns that into an
    # assertion so a regression in `_TOKEN_EPSILON` reads as a failure rather than as a machine
    # that has to be killed by hand.
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


# --- hosts.py: the policies as data (decision 340) ------------------------------------


def test_the_two_html_connectors_stage_two_names_are_kept_deliberately_slow():
    """§8 stage 2 names `rt:page` and `metacritic:page->reviews`, and this is what that costs.

    Seven-tenths of a request a second, one at a time, with a quarter-hour cooldown rather than the
    usual five minutes - the rate the corpus crawled both hosts at without being blocked. Asserted
    as four numbers because "deliberately slow" is the kind of intent a later performance pass
    tunes away in good faith: these are measurements, and raising them is a decision somebody has
    to take rather than a value somebody can nudge.
    """
    for host in ("www.rottentomatoes.com", "www.metacritic.com"):
        policy = HOST_POLICIES[host]
        assert policy.rps == 0.7, f"{host} is one of the two hosts section 8 stage 2 scrapes"
        assert policy.burst == 1
        assert policy.max_concurrency == 1
        assert policy.breaker_cooldown_s == 900


def test_every_robots_override_records_the_reasoning_that_licenses_it():
    """Decision 340, and §8: an override is permitted "only where it is documented with its
    reasoning beside the policy it changes" (`spec:404`).

    A permission with no enforcement is a comment, so the clause is read as a query over the
    table. The second assertion is what stops the first from passing vacuously - if a later edit
    turned robots back on everywhere, `undocumented_overrides()` would be empty for the wrong
    reason and this guard would go quiet exactly when it stopped guarding anything.

    The third names the corpus's one worked override by its evidence rather than by its host, so
    that copying the row without its argument fails here: Wikimedia disallows `/w/` to keep
    crawlers off the script endpoints, and the batched action API is the sanctioned route to the
    awards, box-office and country claims that honouring the blanket rule lost entirely.
    """
    assert undocumented_overrides() == []
    overridden = [h for h, p in HOST_POLICIES.items() if not p.respect_robots]
    assert len(overridden) >= 5, "the table has overrides; a guard over none of them guards nothing"
    assert "/w/" in HOST_POLICIES["www.wikidata.org"].note


def test_the_households_own_jellyfin_is_not_a_third_party():
    """§8: "The household's own Jellyfin server is exempt: it is reached with the household's own
    key and is not a third party to be polite to."

    Both halves of the exemption, because either one alone is useless: throttling the owned-library
    sync to a stranger's rate would make it take hours, and honouring Jellyfin's stock robots.txt
    would block it outright.

    The last two are the exemption's edges. Case-insensitive, because a hostname is, and an admin
    who typed the server in mixed case would otherwise silently get the slow default. Whole-host
    and not a suffix match, because `evil.jellyfin.example` is a third party that merely ends in
    the household's name - the shape that turns a convenience into a hole.

    AND THE SPELLING THE APP ACTUALLY HOLDS, which every assertion above missed because every one
    of them passes a bare hostname. The only Jellyfin location this app stores is a URL
    (`connectors/registry.py:85`, `:225`, `.env.example`), and `normalise_host` takes a netloc:
    handed `http://jelly.home:8096` it returns the literal string `'http'`, so the exemption fails
    silently in BOTH directions - the household's own server throttled to DEFAULT_RPS with its
    stock robots.txt honoured, and any host whose key really is `http` handed the 8 rps exemption.
    The http-on-80 row is the same miss by a second mechanism: `get` keys a request under the
    scheme it was made with, so a configured value normalised under a hardcoded https default kept
    a port the request side had already dropped. [M5.1 review cycle 2, port-JF-01, M51-C2-340-04]
    """
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
    # A bare netloc carries no scheme, so an explicitly written default port is not a
    # distinguishing port - and `get` keys an http request to port 80 as the bare host.
    assert policy_for("jelly.lan", jellyfin_host="jelly.lan:80").rps == 8.0
    assert policy_for("jelly.lan:8096", jellyfin_host="jelly.lan:80").rps == DEFAULT_RPS

    # AND A DECLARED ROW OUTRANKS THE CONNECTOR URL, which is the edge the exemption had left
    # open in the one direction that matters. `jellyfin_host` is whatever an admin typed into
    # section 6.6's Jellyfin card (`api/admin.py` validates its LENGTH and nothing else), and the
    # test came first in `policy_for`, so that text field could replace any row in the table: point
    # it at one of the two hosts section 8 stage 2 scrapes - by a typo, by a reverse proxy that
    # fronts both, or by being talked into it - and that host is crawled at 8 rps with its
    # robots.txt ignored, while `declared_policies()` and `undocumented_overrides()` both keep
    # reporting the measured numbers because neither takes a `jellyfin_host` argument at all. No
    # household runs Jellyfin on one of these names, so the guard costs nothing and the exemption
    # still wins everywhere else. [M5.1 review cycle 3, M51-C3-340-03]
    for declared in ("www.rottentomatoes.com", "www.metacritic.com", "www.film-rezensionen.de"):
        hijacked = policy_for(declared, jellyfin_host=f"https://{declared}")
        assert hijacked.rps == HOST_POLICIES[declared].rps, (
            f"a Jellyfin url typed as {declared!r} replaced a rate somebody measured"
        )
        assert hijacked.respect_robots is True, (
            f"a Jellyfin url typed as {declared!r} turned off that host's robots.txt"
        )


def test_an_unknown_host_is_crawled_at_the_slow_default():
    """One request a second and robots honoured, for a host nobody has measured.

    The default is the safety property of the whole table: a stage that reaches a host before
    anyone tuned it cannot hurt that host, and the cost of being wrong is slowness rather than a
    block. It is also what makes adding a row to `HOST_POLICIES` a deliberate act.
    """
    policy = policy_for("nobody-has-measured-this.example")
    assert policy.rps == DEFAULT_RPS
    assert policy.respect_robots is True


def test_no_paid_provider_host_is_declared_by_this_milestone():
    """The corpus's three LLM hosts are dropped, and that is a decision rather than an oversight.

    M5.1 adds no provider dependency: §8 stage 6 is M5.5's, and so is the spend cap that gates it.
    A rate declared here for `api.anthropic.com` would be configuration for a feature with no
    caller, and - worse - would read to a later agent as permission to reach it. Asserted by
    substring so that a differently-spelled provider host fails the same way.
    """
    for marker in ("anthropic", "openai", "googleapis", "generativelanguage"):
        assert not [h for h in HOST_POLICIES if marker in h], (
            f"{marker} belongs to M5.5's connector layer with its spend cap, not to the spine"
        )


def test_the_board_can_read_every_policy_including_its_reasoning():
    """§8: policies are carried "as data rather than as constants, so §6.6 can show them".

    The read exists and is complete, which is the whole of M5.1's obligation here - the surface
    that renders it is M5.7's. Sorted, so an operator reading the list twice reads the same list.
    """
    rows = declared_policies()
    assert len(rows) == len(HOST_POLICIES)
    assert [r["host"] for r in rows] == sorted(HOST_POLICIES)
    assert set(rows[0]) == {
        "host", "rps", "burst", "max_concurrency", "breaker_threshold",
        "breaker_cooldown_s", "respect_robots", "note",
    }


# --- the pacing and backoff arithmetic ------------------------------------------------


async def test_a_host_is_paced_at_its_declared_rate_once_its_burst_is_spent():
    """Rotten Tomatoes' numbers: burst 1, then one request every 1/0.7 seconds.

    The burst is spent without waiting - that is what a burst is - and every request after it
    waits exactly as long as one token takes to accrue. Asserted to the fraction because the
    failure this catches is a bucket that is a little too generous, which no crawl notices until
    the host does.
    """
    clock = _Clock()
    bucket = TokenBucket(0.7, 1, clock=clock, sleep=clock.sleep, jitter=_low)
    await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == [pytest.approx(1 / 0.7), pytest.approx(1 / 0.7)]


async def test_the_bucket_never_refills_past_its_burst():
    """An idle hour does not buy a thundering herd.

    Tokens accrue at `rps` and are capped at `burst`, so a stage that reaches a host for the first
    time in an hour gets `burst` requests and then the declared rate - not 3,600 of them. This is
    the half of the algorithm that is invisible in a busy crawl and decisive in an idle one, which
    is exactly this app: the pipeline runs per title, minutes apart.
    """
    clock = _Clock()
    bucket = TokenBucket(1.0, 2, clock=clock, sleep=clock.sleep, jitter=_low)
    clock.advance(3600)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == [pytest.approx(1.0)]


async def test_the_bucket_cannot_spin_when_the_wait_it_computed_lands_a_float_short():
    """The one correction this port makes to the corpus's bucket, pinned by its own numbers.

    Trakt's declared policy, from a clock at 1000 seconds: three requests spend the burst, and the
    fourth computes a 0.4-second wait to buy one token back. `(now - updated) * rps` does not
    return that token - the subtraction cancels most of a large clock's significant digits and the
    product lands at 0.9999999999999432 - so a full-precision "have I got a token" reads "not yet",
    computes a 2e-14 second wait that the clock cannot even represent at that magnitude, and asks
    again for ever.

    The corpus never meets this because its jitter overshoots the shortfall on every call, which is
    what makes it worth a test rather than a comment: the defect appears exactly when the pacing is
    made deterministic, and a spin of zero-length sleeps inside §5.3's sequential tick takes every
    other job offline with the process still looking healthy.
    """
    clock = _Clock(start=1000.0)
    bucket = TokenBucket(2.5, 3, clock=clock, sleep=clock.sleep, jitter=_low)
    for _ in range(3):
        await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == [pytest.approx(0.4)], "one wait, and it bought the token it asked for"


def test_the_backoff_curve_doubles_jitters_and_is_capped_at_a_minute():
    """1.6s, 3.2s, 6.4s ... capped at 60, spread +/-30 percent. The corpus's curve, verbatim.

    The cap is what stops a fourth retry against a dead host from being a quarter of an hour inside
    a worker tick measured in seconds. The spread is what stops a hundred tasks that failed
    together from retrying together and failing together again - asserted as a band rather than a
    value, because a jitter that is secretly a constant would pass any single-point assertion.
    """
    assert _backoff(1, _low) == pytest.approx(1.6 * 0.7)
    assert _backoff(2, _low) == pytest.approx(3.2 * 0.7)
    assert _backoff(3, _low) == pytest.approx(6.4 * 0.7)
    assert _backoff(7, _low) == pytest.approx(60.0 * 0.7), "the cap, not 102 seconds"
    assert _backoff(20, _low) == pytest.approx(60.0 * 0.7)

    spread = {round(_backoff(3), 6) for _ in range(50)}
    assert len(spread) > 1, "a backoff with no jitter re-synchronises every retrying task"
    assert all(6.4 * 0.7 <= value <= 6.4 * 1.3 for value in spread)


def test_retry_after_is_read_in_seconds_capped_and_never_guessed():
    """The header in both forms RFC 9110 §10.2.3 permits, through one cap.

    Unparseable means "no instruction", which sends the caller to the backoff curve rather than to
    zero - the difference between backing off politely and hammering a host that just asked for
    quiet.

    THE DATE FORM USED TO RETURN None, and the reason given was that "a date mis-read as a number
    would be a pause of geological length". Neither half is possible: `float()` RAISES on all
    three date productions rather than returning a number, and the five-minute cap bounds any
    misreading anyway. What the omission did was discard an explicit instruction - measured, a
    host answering 429 with a date an hour out was hit four times inside twenty-four seconds where
    the coverage row for this layer says a 429 "backs off at least as far" - and the form is the
    one Cloudflare and several API gateways emit, which is what the two HTML hosts §8 stage 2
    names sit behind. A date already past is an expired instruction and not a negative pause.
    [M5.1 review cycle 4, M51-C4-340-03]
    """
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

    # AND THE SECONDS FORM THROUGH THE SAME FLOOR, which is the half the sentence above promised
    # and only the date branch delivered: `min(float(raw), 300.0)` had no lower bound, so a host
    # answering `-1` bought `await self._sleep(-1.0)` - a pause `asyncio.sleep` returns from
    # immediately - and the four attempts of the backoff curve landed inside one event-loop turn.
    # Zero is not "no instruction": it is falsy, so the caller's `_retry_after(...) or _backoff(...)`
    # sends it to the curve, which is what an expired instruction already means one branch down.
    # The non-finite pair come free with the clamp and are worth pinning, because `nan` reached
    # `asyncio.sleep` and raised `ValueError` out of `get`, which `FetchError`'s own docstring says
    # is the one type this layer raises. [M5.1 review cycle 4 second pass, M51-C4-340-07]
    for header in ("-1", "-99999", "nan", "-inf"):
        assert _retry_after({"retry-after": header}) == 0.0, header
    assert _retry_after({"retry-after": "inf"}) == 300.0, "the cap holds for the other infinity"


def test_the_retryable_set_is_the_corpus_list_including_cloudflares_family():
    """520-524 are in the set on purpose, and are the ones a rewrite drops.

    They are Cloudflare's origin-side family, and they matter here because the two HTML hosts §8
    stage 2 names sit behind it: an unhappy origin answers 520 where a plain server answers 502,
    and a fetcher that treats 520 as a real answer parks a title on a blip. 404 is outside the set
    for the mirror-image reason - retrying a real answer is how one wrong url becomes four.
    """
    assert sorted(RETRYABLE_STATUS) == [408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524]
    assert 404 not in RETRYABLE_STATUS


# --- the retry loop, against a local double -------------------------------------------


async def test_a_429_is_paused_for_as_long_as_the_host_asked():
    """§8's politeness where it costs something: the host said two minutes, so it is two minutes.

    `Retry-After` is honoured and never shortened. The backoff would have chosen 3.36 seconds and
    is overruled, which is the direction that matters - a fetcher that quietly preferred its own
    curve would look polite in code review and be a rate-limit violation on the wire.
    """
    clock = _Clock()
    async with _fetcher(_always(429, headers={"Retry-After": "120"}), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=2)
    assert caught.value.status == 429
    assert caught.value.retryable is True
    assert clock.slept == [pytest.approx(120.0)]


async def test_a_429_with_an_optimistic_retry_after_still_costs_three_backoffs():
    """The other direction, and the reason the rule is a `max` rather than a preference.

    A host that is rate limiting in earnest sometimes answers `Retry-After: 1`, and taking it at
    its word is how a crawl gets itself blocked. The floor is three times the backoff the attempt
    would otherwise have chosen: 3.36 seconds where the header asked for one.
    """
    clock = _Clock()
    async with _fetcher(_always(429, headers={"Retry-After": "1"}), clock) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=2)
    assert clock.slept == [pytest.approx(1.6 * 0.7 * 3)]


async def test_a_retryable_status_without_an_instruction_follows_the_backoff_curve():
    """A 503 with no header: the curve, and one sleep fewer than the attempts.

    Two sleeps for three attempts, because the last attempt raises rather than waiting for a fourth
    that will never happen - a fetcher that slept after its final attempt would add a minute of
    dead time to every permanently failing url.
    """
    clock = _Clock()
    async with _fetcher(_always(503), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=3)
    assert caught.value.status == 503
    assert clock.slept == [pytest.approx(1.6 * 0.7), pytest.approx(3.2 * 0.7)]


async def test_a_404_is_an_answer_and_is_raised_without_a_retry():
    """A 4xx outside the retryable set is the host answering, not the host failing.

    So it is raised at once, non-retryable, and - the half that is easy to get wrong - it does not
    count toward the breaker. A pipeline that asks for fifty titles a stranger's API has never
    heard of would otherwise trip the breaker on its own bad guesses and park a host that was
    behaving perfectly.
    """
    clock = _Clock()
    async with _fetcher(_always(404), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/0", max_attempts=4)
        assert f.total_errors == 0, "a 404 is not the host's fault"
    assert caught.value.retryable is False
    assert caught.value.status == 404
    assert clock.slept == []


async def test_the_breaker_opens_for_one_host_and_the_other_host_keeps_fetching():
    """§8's "one hostile host cannot burn the whole run", asserted as an isolation property.

    Eight consecutive failures is the declared threshold, so the ninth call is refused before a
    request is made - `HostPaused`, retryable, raised without sleeping, because a five-minute
    cooldown inside a twenty-second worker tick is a task to hand back rather than a wait to sit
    through. The second host is the assertion that matters: the breaker is per host, and a table of
    per-host policies whose breaker was global would be a table with one entry.

    That second host is fetched once BEFORE the failures and again after, which is not ceremony. A
    drain builds a host's runtime lazily, on first use, so a breaker that leaked across hosts would
    still leave an untouched host working and the isolation would be asserted against a host the
    leak had not reached yet - a mutation that paused every known host passed this test until the
    warm-up was added.
    """
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
    """Decision 340, and the one failure a retry loop must never treat as transient.

    The answer will be the same tomorrow and the attempt itself is the impoliteness, so
    `RobotsDisallowed` is raised before the request is built and carries `retryable=False`. The
    counter is the second assertion: exactly one request went out, the robots.txt, and the url the
    caller asked for was never touched.
    """
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
    """Decision 340: "It declares a User-Agent naming the app."

    Named, so a host that wants to address this crawler in its robots.txt or block it by name can.
    `urllib.robotparser` matches on the token before the slash, which is why the app's name comes
    first and the parenthetical follows - a `User-agent: Spielplan` rule has to apply to this.

    EVERY REQUEST MEANS FOUR KINDS OF REQUEST, and the first form of this test could reach only
    one of them. It fetched `FAST`, whose declared policy sets `respect_robots=False` - the two
    hosts at the top of this file are chosen precisely so that no robots request is made - and
    then asserted `sent == [USER_AGENT]`, a one-element equality that structurally forbids a
    second request. So the robots.txt fetch, the retries and the post-redirect request were all
    outside its reach, and the robots fetch is the one that matters most: it is the file a host
    reads to decide how to treat this crawler, and it is the one request that does not go through
    `get`'s header path at all. Moving `_fetch_robots` onto a client of its own - a plausible
    repair for any of this cycle's robots findings - would have shipped it anonymously with the
    old assertion green. Run here over an unmeasured host, which honours robots at the slow
    default, and asserted on the recorded headers rather than on a count.
    [M5.1 review cycle 1, M51-340-08]

    The path list is what stops the header assertion passing vacuously: it names the four kinds
    of request by hand, so a change that stops exercising one of them reddens here rather than
    quietly reducing what "every request" is asserted over.
    """
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
    """The counter the exit criterion reads when it asserts a re-parse costs nothing.

    Check 7 of `ops/m51_exit_criterion.py` measures "zero outbound requests" against this number,
    so it has to start at zero on a fresh fetcher and move only when something is actually sent.
    Bytes are counted alongside for the same reason: a 304 that returned a body would be invisible
    in a request count and obvious here.
    """
    clock = _Clock()
    async with _fetcher(_always(200, content=b"seven!!"), clock) as f:
        assert f.total_requests == 0
        assert f.total_bytes == 0
        await f.get(f"https://{FAST}/3/movie/603")
        assert f.total_requests == 1
        assert f.total_bytes == 7
        assert f.total_errors == 0


# --- the three promises that are promises about Postgres ------------------------------


async def test_robots_txt_is_cached_in_postgres_and_read_back_on_the_next_drain(db):
    """Decision 340: robots.txt is "fetched per host and cached in Postgres".

    The corpus caches it in the process, which is enough for a CLI whose process is the crawl.
    This runs inside §5.3's worker, which restarts, and a fetcher that re-read every host's
    robots.txt on every restart would be impolite by exactly the mechanism meant to make it polite.

    So the second half is the test: a fresh fetcher against the same database issues no request at
    all and still refuses the disallowed path. The refusal came out of Postgres.
    """
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
    # TWO, and not the one this test used to assert. Port change 8 says a robots fetch is an
    # outbound request and that "a counter that quietly omits a class of request is a counter that
    # can lie", and then counted it only on the process-wide `total_requests` - leaving
    # `rt.requests`, which is what `host_report()` publishes and this column stores, short by
    # exactly that class. The drain made two requests: robots.txt and the page.
    # [M5.1 review cycle 1, port-07, M51-340-10]
    assert row["requests"] == 2, "the drain's counters are flushed once, on the way out"

    seen.clear()
    async with _fetcher(handler, _Clock(), conn=db) as second:
        with pytest.raises(RobotsDisallowed):
            await second.get("https://policy.example/private")
        assert second.total_requests == 0
    assert seen == [], "a cached robots.txt is a refusal that costs the host nothing"


async def test_the_breakers_refusal_survives_the_worker_it_opened_in(db):
    """A worker killed after opening a breaker must not come back and resume hammering.

    §5.3's worker restarts, and its tick is twenty seconds against a five-minute cooldown - so a
    breaker held only in memory would protect a host for one tick and then forget. `paused_until`
    is written the moment the breaker opens rather than on the way out, because the process that
    opened it is precisely the process that may not get to run its shutdown path.

    Stored and read back as an interval against the database's own `now()`, so nothing has to
    reconcile Postgres's wall clock with the monotonic clock the fetcher paces itself by.
    """
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
    """§8: "All fetched bytes land in the app's own raw store, so re-parsing is free forever."

    This is the other half of that promise - the half that keeps the *fetch* cheap when the bytes
    have not changed. The validators come off the newest **successful** `raw_document` row for the
    url, which is why the fixture plants a newer failed row carrying a different ETag: a validator
    taken from an error page is how a store comes to hold a 503 under the name of the document.

    A 304 hands back no body, and `total_bytes` is the assertion that nothing was transferred.
    """
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


# --- review cycle 1: politeness, on the wire rather than in a docstring ------------------------
#
# Each of these reddens against the layer as this milestone first shipped it, and each is a
# property §8's clause (`spec:404`) claims and M5.3 through M5.7 will inherit unchanged. Decision
# 340 exists so a reviewer "can fail the behaviour instead of debating it"; this is where the
# failing happens.


async def test_a_robots_txt_the_host_could_not_serve_refuses_rather_than_allowing():
    """RFC 9309 §2.3.1.4: when robots.txt is unreachable a crawler "MUST assume complete disallow".

    Every failure mode used to allow the request instead. `_parse_robots` read anything that was
    not a 200 as an empty ruleset, which `urllib.robotparser` answers as allow-all, and the
    `except` in `_fetch_robots` took the same branch for a timeout or a connection reset - so a
    host that 500s during a deploy, or one a DNS blip hid for a second, was crawled on the
    household's IP under a file this app had never read. That is the most permissive possible
    reading of an answer that does not exist.

    RETRYABLE, which is what makes the refusal affordable. The old code's counter-argument was
    sound about the exception it had - `RobotsDisallowed` is `retryable=False`, so refusing a host
    over one outage would park every task for it as permanently refused - so this is a different
    exception, in `HostPaused`'s mould: a stage parks with a deadline and the next drain asks
    again. [M5.1 review cycle 1, M51-340-03]
    """
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
    """The other side of the line, and it is RFC 9309 §2.3.1.3's: the whole 4xx range is
    "unavailable", where "the crawler MAY access any resources".

    Without this the fix above would be a refusal to crawl anything that does not publish a
    robots.txt, which is most of the web and all four of the blogs `hosts.py` declares. 404, 403
    and 401 alike: a 403 on robots.txt is an anti-bot reflex rather than a rule, and inventing a
    rule out of it is as much a guess as ignoring one.
    """
    for status in (404, 403, 401, 410):
        def handler(request: httpx.Request, status=status) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(status, text="nope")
            return httpx.Response(200, content=b"page")

        async with _fetcher(handler, _Clock()) as f:
            assert (await f.get("https://unmeasured.example/title/1")).status == 200


async def test_a_robots_failure_is_not_cached_as_permission(db):
    """Decision 340 puts the robots cache in Postgres, and the cache used to hold non-answers.

    `_fetch_robots` wrote `fetch_host_state` outside its own `try`, so a transport failure landed
    with `robots_txt = ''`, `robots_status = 0` and `robots_fetched_at = now()` - and
    `_load_host_state` gated only on age, so for `ROBOTS_TTL_SECONDS` (24 h) across every worker
    restart that host was read as allow-all and never asked again. Because the write is
    `ON CONFLICT DO UPDATE` it also CLOBBERED a `Disallow: /` this app had already read and was
    obeying.

    The owner's review question for decision 340 was whether "the cache cannot pin a stale allow".
    This is that question, asked of the database: the failed drain must leave no robots row, and a
    second fetcher over the same connection must ask again - and then obey what it is told.
    [M5.1 review cycle 1, M51-340-01, M51-340-03, port-02]
    """
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
    """§8 (`spec:404`): the fetcher "honours each host's robots.txt" and "carries per-host rate and
    concurrency policies as data". A hop is a request, and every hop used to skip all of it.

    `follow_redirects=True` was taken verbatim from `mdc/http.py:128`, where it was harmless: the
    corpus is a developer's CLI. Here httpx expanded one `request` call into a chain of requests
    inside the layer that was supposed to be gating them, so a target host's robots.txt was never
    read, its declared policy never applied, its bucket never charged, its breaker never consulted,
    and `host_report()` attributed the whole chain to the host that redirected.

    TWO ASSERTIONS AND THE SECOND IS THE SHARPER ONE. A cross-host hop into a host that disallows
    everything must be refused; and a SAME-host hop into a disallowed PATH must be refused too,
    which is the ordinary slug-miss shape M5.3 will meet on Rotten Tomatoes and Metacritic - the
    fetcher refusing a url when asked for it and fetching the identical url when redirected to it.
    [M5.1 review cycle 1, M51-340-02]
    """
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
    """httpx's own default is twenty hops, which is a number for a browser.

    Every hop is a request on the household's IP, and a chain that needs more than a handful is a
    loop or a redirector. The cap is a refusal rather than a silent stop, because a 3xx body
    returned as an answer is a redirect page handed to a parser. [M5.1 review cycle 1, M51-340-02]
    """
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
    """`urlparse(url).netloc` keeps the port and any userinfo. The policy table is keyed by host.

    So `www.rottentomatoes.com:443` missed every row and took `DEFAULT_RPS` - 1.0 rps, burst 2,
    a five-minute cooldown - instead of the 0.7/1/900 the corpus measured for that host. Worse
    than the rate: it keyed a SECOND runtime for one physical host, so the two spellings had two
    buckets and two breakers, and §6.6's board would have shown one host twice with neither number
    right. A url built from third-party provider data is exactly where an explicit `:443` comes
    from. [M5.1 review cycle 1, M51-340-04]
    """
    assert normalise_host("www.rottentomatoes.com:443") == "www.rottentomatoes.com"
    assert normalise_host("WWW.Rottentomatoes.COM") == "www.rottentomatoes.com"
    assert normalise_host("anyuser@www.rottentomatoes.com") == "www.rottentomatoes.com"
    assert normalise_host("host:80", scheme="http") == "host"
    # A non-default port stays, because it is part of the host: a household Jellyfin on
    # `192.168.1.10:8096` needs it, or two services on one box would share one exemption.
    assert normalise_host("jelly.lan:8096") == "jelly.lan:8096"
    # Both sides of the Jellyfin comparison go through the same normalisation, so the exemption
    # does not stop matching when M5.2 stores the configured host with an explicit default port.
    assert policy_for(normalise_host("JELLY.lan:8096"), jellyfin_host="jelly.lan:8096").rps == 8.0
    assert policy_for(normalise_host("evil.jelly.lan"), jellyfin_host="jelly.lan").rps == 1.0

    # THE DNS ROOT LABEL IS THE THIRD SPELLING OF ONE AUTHORITY and it was the one left keying a
    # second runtime. `www.rottentomatoes.com.` resolves to the same server, misses HOST_POLICIES,
    # and so takes DEFAULT_RPS with its own bucket and its own breaker - the exact harm the port
    # and userinfo rules above were added to close. It also widens the Jellyfin exemption, which
    # `policy_for` guards with `host not in HOST_POLICIES`: a spelling that is not in the table is
    # a spelling an admin's configured Jellyfin url can capture, so a declared host came back at
    # 8 rps with `respect_robots=False`. The last assertion is that guard holding for all three
    # spellings. [M5.1 review cycle 4 second pass, M51-C4-340-08]
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
        # TWO WAITS FOR THREE REQUESTS, and the first of the three is the robots.txt. This host
        # declares `respect_robots=True`, so the fetcher reads its rules before either page - and
        # that read is now charged to the same bucket, which it was not when this assertion was
        # first written. One wait was the arithmetic of a burst of 1 spent on the page rather than
        # on the robots fetch, which is the effective burst of 2 M51-C2-340-05 measured on exactly
        # the two hosts §8 stage 2 names. [M5.1 review cycle 2, M51-C2-340-05]
        assert clock.slept == [pytest.approx(1 / 0.7), pytest.approx(1 / 0.7)], (
            "robots.txt and both pages are three requests to one host at its declared pace"
        )
        assert f._hosts[host].policy.rps == 0.7


async def test_a_decoding_failure_is_a_fetch_error_and_counts_against_the_host():
    """`FetchError`'s own docstring: "Every failure this layer raises, so a caller can catch one
    type." Two httpx failures were not that type.

    The `except` named `TimeoutException`, `TransportError` and `ProtocolError`, which is
    `mdc/http.py:253` verbatim. In httpx 0.28 `DecodingError` and `TooManyRedirects` are
    `RequestError` but NOT `TransportError`, so a host serving a truncated gzip escaped this layer
    entirely: not wrapped, `_note_failure` never called, the breaker unable to open on it, and the
    task retried until its attempts were spent - burning the host again on every one. The obvious
    stage body is `try: ... except FetchError as e: park(e)`, and it would not have caught this.
    [M5.1 review cycle 1, M51-340-05]
    """
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
    """`allow_status` names a status a caller can read. It is not an override of the politeness.

    The condition read `status in RETRYABLE_STATUS and status not in allow_status`, so a caller
    passing `allow_status=(429,)` - to read a provider's error body, which is an ordinary reason
    to pass it - fell through to `_note_success` and got the 429 back as a success: `Retry-After`
    never read, no pause, no breaker count, and the run of consecutive failures RESET on the one
    status that means the host is complaining about load. §8 permits exactly one kind of
    politeness override, documented in `HOST_POLICIES` with its reasoning and enforced by
    `undocumented_overrides()`; this was a second one, per call, that the guard cannot see.

    The body is still reachable, which is the half the caller actually wanted: the pause and the
    breaker count come first, and the allowed status is handed back once the attempts are spent.
    [M5.1 review cycle 1, M51-340-06]
    """
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
    """Decision 340: "It declares a User-Agent naming the app."

    httpx lets a per-request header win over the client's, and `_check_robots` judges the request
    against this module's own `USER_AGENT` - so a stage that copied a browser or bot agent into
    `headers=` to get past a wall would have misrepresented the household to a third party while
    being allowed by a rule written for a name it was no longer sending. The registered test for
    the declared agent passes no headers, so it stayed green throughout.

    A refusal rather than a silent merge: the caller has to learn that it asked for something this
    layer will not do. [M5.1 review cycle 1, M51-340-07]
    """
    clock = _Clock()
    async with _fetcher(_always(200, content=b"ok"), clock) as f:
        for spelling in ("User-Agent", "user-agent"):
            with pytest.raises(ValueError, match="User-Agent"):
                await f.get(f"https://{FAST}/3/movie/603", headers={spelling: "Googlebot/2.1"})
        assert f.total_requests == 0, "nothing went out under the wrong name"


async def test_the_run_of_failures_a_host_is_on_survives_the_drain_that_counted_it(db):
    """`fetch_host_state.consecutive_failures` was write-only, and always the literal zero.

    `_note_failure` wrote the row only when the breaker OPENED, and that write hardcodes the
    counter back to 0 (correctly - the breaker resets it); `_load_host_state` did not select the
    column at all. So the number §6.6 would show was 0 for every host that had not tripped, and
    the breaker could only ever be reached inside ONE drain: a host failing seven times a drain,
    every drain, against a threshold of eight, was never parked at all.
    [M5.1 review cycle 1, M51-340-09]
    """
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


# --- review cycle 2: the classes the first cycle's boundaries let through ----------------------


async def test_a_robots_txt_the_host_only_redirected_is_not_an_answer(db):
    """RFC 9309 §2.3.1.2 gives a redirect its own subsection: a 3xx is not the served file.

    Review cycle 1 closed a non-answer being cached as permission and drew the line at 500, so a
    301, 302, 307 or 308 on `/robots.txt` still read as "the host answered". Nobody follows it -
    the client is built `follow_redirects=False` and `_fetch_robots` calls `self._client.get`
    rather than `get`'s own hop loop - so `body` was `""`, which `urllib.robotparser` answers as
    allow-all, and the unconditional UPSERT wrote that into `fetch_host_state` with
    `robots_fetched_at = now()`. `_load_host_state` then honoured it for a day, across every
    worker restart, issuing no further robots request: one CDN hop and this app crawls every path
    a host disallows, on the household's own IP, and CLOBBERS a `Disallow: /` it had already read.

    Both halves are asserted, because the first cycle's finding was that either alone is useless:
    the request is REFUSED, and the redirect is not written to the cache - so the next drain asks
    again and then obeys what it is told. [M5.1 review cycle 2, M51-C2-340-01]
    """
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
    """Port change 8: "a counter that quietly omits a class of request is a counter that can lie".

    `_note_success` counted the request and `_note_failure` counted only the error, so a host
    answering 429 to four attempts durably recorded that this app had made ZERO requests to the
    host that was rate limiting it - `persist_host_state` accumulates `rt.requests` into
    `fetch_host_state` and `host_report()` publishes it. `_fetch_robots` meanwhile counted the
    request before its `try` AND the error after it, so one failed robots fetch recorded one
    request and one error for the same single request: within one module and one table, `requests`
    meant two different things depending on which path wrote the row.

    The registered counter test above asserts the rule in its own name - it "counts what went
    out" - and passed only because it exercises the 200 path. [M5.1 review cycle 2, M51-C2-340-02]
    """
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
    """§8 (`spec:404`): the fetcher "honours each host's robots.txt".

    `Crawl-delay` and `Request-rate` are the only two directives in robots.txt that are about RATE
    rather than about paths - the one way a site can ask a crawler in writing to slow down - and
    `urllib.robotparser` had already parsed both onto the object this layer holds. `_check_robots`
    read that object for `can_fetch` and nothing else, so a small review site publishing
    `Crawl-delay: 30` was crawled at `DEFAULT_RPS = 1.0` by an unattended household appliance:
    thirty times faster than it asked, in the file this app fetched and stored. Four of the rows
    `hosts.py` declares are "small sites run by individuals".

    The declared row stays the CEILING, which is the assertion the third host makes: a robots.txt
    asking to be crawled FASTER buys nothing, because `hosts.py`'s number is a measurement of what
    that host survived. The cached path is asserted too, because a rate honoured on the drain that
    read the file and forgotten on every drain that read the Postgres cache would honour a
    `Crawl-delay` about once a day. [M5.1 review cycle 2, M51-C2-340-03]
    """
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

    # The same rules out of the Postgres cache, on a fetcher that issues no robots request at all.
    cached = _Clock()
    async with _fetcher(slow, cached, conn=db) as f:
        for n in range(3):
            await f.get(f"https://slowblog.example/review/{n}")
        assert f.total_requests == 3, "the robots.txt came out of the cache"
    assert cached.slept == [pytest.approx(30.0)] * 2

    # `Request-rate: 1/60` says the same thing in the other spelling, and the stricter of the two
    # wins when a host publishes both.
    rate = _Clock()
    async with _fetcher(publisher("User-agent: *\nRequest-rate: 1/60\nCrawl-delay: 5\n"), rate) as f:
        await f.get("https://rated.example/a")
        await f.get("https://rated.example/b")
    assert rate.slept == [pytest.approx(60.0)], "the stricter of the two directives"

    # And a host asking to be crawled faster than its declared row does not get it.
    eager = _Clock()
    async with _fetcher(publisher("User-agent: *\nCrawl-delay: 0.05\n"), eager) as f:
        for n in range(3):
            await f.get(f"https://unmeasured.example/{n}")
    assert eager.slept == [pytest.approx(1.0)] * 2, "DEFAULT_RPS is a ceiling a host cannot raise"

    # The declared burst is not handed back by the clamp: this host allows one request at a time,
    # the robots.txt is that request, and the page after it waits the delay rather than going out
    # beside it. `www.rottentomatoes.com` is one of the two §8 stage 2 names by hand.
    tight = _Clock()
    async with _fetcher(publisher("User-agent: *\nCrawl-delay: 10\n"), tight) as f:
        for path in ("/m/a", "/m/b"):
            await f.get(f"https://www.rottentomatoes.com{path}")
    assert tight.slept == [pytest.approx(10.0)] * 2, (
        "robots.txt is a request to this host, and a burst of 1 has no token left after it"
    )


async def test_a_robots_txt_larger_than_the_parsing_limit_is_cut_between_lines(db):
    """RFC 9309 §2.5 ("Limits"): a crawler's parsing limit is "at least 500 kibibytes".

    This is the one request in the app that takes arbitrary bytes from a stranger before any of
    that host's policy applies, and there was no bound on it: a multi-megabyte robots.txt was
    parsed inside §5.3's sequential tick, written whole into a Postgres column, and re-read and
    re-parsed on the first touch of that host in every drain for twenty-four hours. A third party
    chose how much of the household's database and worker tick it occupied.

    CUT BETWEEN LINES, which is the half that decides the direction of the error: a cut inside a
    path shortens the prefix a rule covers and a cut inside the directive name drops the rule, so
    a byte-exact truncation relaxes a refusal - the direction review cycle 1 spent two findings
    closing. The rules published before the limit are still obeyed, which is the assertion that
    stops this from passing by refusing the host outright.
    [M5.1 review cycle 2, M51-C2-340-06]

    IN BOTH ALPHABETS, because the cheap character test was joined to the byte test with `or` and
    so decided the ACCEPT on its own. The premise it rested on - a str never encodes to fewer
    bytes than it has characters - licenses a REJECT on the character count and nothing else, so
    for a body with substantial non-ASCII the characters fit, the bytes did not, and the arbitrary
    byte `_fetch_robots` stopped the stream at survived as the last rule: an ASCII padding is the
    one shape where the two tests cannot disagree, which is why this passed for two cycles. The
    German arm below is the shape one of the four blogs `hosts.py` declares would serve, and the
    rule it leaves half-written is a rate directive. [M5.1 review cycle 4, M51-C4-340-05]
    """
    ascii_line = "# padding that no crawler is obliged to read"
    # Every umlaut is one character and two bytes, so this body's character count stays well
    # under the cap while its byte count passes it - which is the whole disagreement.
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
    """`MAX_REDIRECTS`'s own comment: "a truncated chain returned as an answer is a 3xx body handed
    to a parser." The cap refuses a chain that is too LONG and watched only that door.

    A 3xx is in neither `RETRYABLE_STATUS` nor the `>= 400` arm, and the hop loop breaks out when
    there is no `Location` to follow - so a 301 from a WAF with no `Location` header fell through
    to `_note_success` and came back as an ordinary `Response`: status 301, body
    `<html>Moved Permanently</html>`. A stage written as the obvious
    `try: ... except FetchError as e: park(e)` sees no error, stores those bytes in the raw store
    under the document's url, and parses a redirect page as the title's page. The two HTML hosts
    §8 stage 2 names sit behind exactly the intermediary class that emits these -
    `RETRYABLE_STATUS`'s own comment says so about 520-524.

    Three ways in and the whole class is refused: no `Location` at all; a 300, which is
    deliberately not in `REDIRECT_STATUS` because there is nothing to follow; and a 304 answering
    a request this layer never conditioned, which is an empty body presented as a fresh answer.
    A caller that names the status in `allow_status` still gets it, because that is what
    `allow_status` is for. [M5.1 review cycle 2, M51-C2-340-07]
    """
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


# How many turns of the event loop the gate below is held shut for. The measurement is how
# many requests can be on the wire AT ONCE, so every task has to reach the place it will be
# stopped before any of them is let go: a gate opened on the first turn would measure the
# scheduler's order rather than the declared cap. Each `get` needs a handful of turns to reach
# the transport and they advance in parallel, so sixty-four is two orders of magnitude of
# headroom for a yield that costs nothing. [M5.1 review cycle 1, M51-340-11]
_SETTLE_TURNS = 64


async def test_a_host_is_held_to_the_concurrency_it_declares_and_not_only_to_its_rate():
    """§8 (`spec:404`): the fetcher "carries per-host rate and concurrency policies as data".

    Eighteen tests were registered on that clause and `max_concurrency` reached two of them as
    DATA alone - `policy.max_concurrency == 1` in the stage-2 policy test, and the key name in
    `declared_policies()`'s row shape - so the `async with hop_rt.sem` in `get` could have been
    deleted with every one of them green. Measured rather than argued: with the semaphore
    neutered, the sequential and token-bucket traces that every pacing test in this file asserts
    come out byte-identical, while the peak in flight for a host declaring twelve goes to twenty.

    The two halves are not the same promise. A bucket paces requests over TIME and says nothing
    about how many are open at once, which is the half a host feels as load rather than as
    frequency - and it is the half M5.3 inherits, because a fan-out over one provider is exactly
    where a cap that nothing asserts stops being a cap. So the burst is spent deliberately here:
    the bucket is asked to permit every one of these requests, and anything that holds one back
    is the semaphore. [M5.1 review cycle 1, M51-340-11]
    """
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



# --- review cycle 3: the seams six milestones inherit ------------------------------------------
#
# M5.1 ships no caller that passes a credential, fetches nothing real and reads no robots.txt on a
# household's IP. Every test below is about the shape M5.2 through M5.7 are written against, which
# is what this milestone exists to publish: a fetcher that is wrong here is wrong six times over,
# and each of those milestones will have built on it before anyone notices.


async def test_a_hop_into_another_origin_does_not_carry_the_callers_credentials():
    """The protection named change 10 took off httpx and did not replace.

    `mdc/http.py:128` set `follow_redirects=True`, so httpx popped `Authorization` when a redirect
    crossed an origin (`_client.py:552-556`) and `Cookie` on every redirect at all. Driving the
    hops in this module - which is what buys the per-hop robots, policy, bucket and breaker - took
    both of those out of the path and put nothing back: `hop_headers` was copied to the next host
    unchanged apart from the two conditional validators.

    M5.3's eight adapters are "a thin `HandlerSpec` over M5.1's fetcher" porting "the corpus's
    request shapes verbatim", and those shapes are header-borne credentials handed straight to
    `ctx.fetcher.get(headers=...)` - `{"Authorization": f"Bearer {tmdb_bearer}"}`,
    `{"trakt-api-key": ...}`, `{"X-Emby-Token": ...}`. One `Location` out of a CDN, a consent edge
    or a hijacked record and the household's provider keys are delivered to that host in the
    clear, from the household's own IP, with no log line and no park.

    THREE ARMS, because a rule that dropped everything always would be a different defect. A
    cross-origin hop drops the caller's headers; a same-origin hop keeps them, which is the
    ordinary slug-miss shape M5.3 meets on the two HTML hosts; and a host upgrading its own http
    to https keeps them, which is httpx's own carve-out and what every redirecting site expects.
    The first arm's control is that the credentials really were on the wire before the hop.
    [M5.1 review cycle 3, M51-C3-340-01]
    """
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
    """`_fetch_robots`'s own docstring: "the breaker is consulted before this (`_raise_if_paused`
    runs first in `get`), so a paused host is not even asked". That was true of the host `get` was
    called for and false of every host a redirect reached.

    The entry path is `_runtime`, `_raise_if_paused`, `_check_robots`. The hop path was `_runtime`,
    `_check_robots`, and the cooldown check only on the next turn of the loop - so a redirect into
    a host whose breaker is open charged that host's bucket and put a `/robots.txt` on its wire
    before anything read `paused_until`.

    THE SECOND ASSERTION IS THE OPERATOR-FACING HALF and is the sharper one. A host in cooldown is
    a host that has been failing, so its robots.txt is failing too - and the walk therefore parked
    with `RobotsUnavailable`'s sentence, which decision 336 shows verbatim on section 6.6's board.
    The board reported a robots problem for a host whose actual state was a breaker cooldown, which
    is the one thing the operator could have waited out. [M5.1 review cycle 3, M51-C3-340-02]
    """
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
    """RFC 9309 section 2.3 scopes a robots.txt to the URI authority, which includes the scheme.

    This layer files one robots answer per HOST: `HostRuntime.robots` is one field, `normalise_host`
    drops a port equal to the scheme's default so `http://x` and `https://x` collapse to one key,
    and `fetch_host_state` is `host text PRIMARY KEY`. So a host's http and https sites are two
    published files, and this app read whichever url the drain happened to lease first and applied
    it to both - nondeterministically, and for `ROBOTS_TTL_SECONDS` across worker restarts. It errs
    in both directions and the impolite one is the one decision 340 exists to prevent: a
    `Disallow: /` published for the plain-http site ignored for a day because an https url touched
    the host first.

    REFUSED RATHER THAN KEYED, which needs no column and is falsifiable here: every url in this
    tree and in the corpus M5.3 ports is https. The control is the second half - a host whose
    declared policy turns robots OFF has no rules to conflate, and the household's own Jellyfin on
    `http://192.168.1.10:8096` is exactly that row, so the refusal must not reach it.
    [M5.1 review cycle 3, M51-C3-340-04]
    """
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
    """`ROBOTS_MAX_BYTES`'s own comment says what the bound is for: this is "the one request in the
    app that takes arbitrary bytes from a stranger BEFORE any of that host's policy applies", and
    unbounded "a misconfigured or hostile host chooses how much of the household's database and
    worker tick it occupies". The database was bounded and the tick was not.

    `_truncate_robots` cut the string AFTER `self._client.get` had read and decoded the whole body,
    so the host chose the allocation. The client declares `Accept-Encoding: gzip, deflate`, so it
    chose it cheaply: measured, a 240 KB gzip decompressed to 70 MB in the worker process and
    peaked a quarter of a gigabyte of heap before `_truncate_robots` saw a character.
    `ROBOTS_TIMEOUT_S` is an `httpx.Timeout`, which bounds each operation and never a total, so
    nothing else was holding this.

    MEASURED AS BYTES CONSUMED and not as bytes kept, which is the distinction the registered size
    test cannot make: it asserts what landed in Postgres, and that assertion is green either way.
    The published rules are still obeyed, which is what stops a bound from being a refusal.
    [M5.1 review cycle 3, M51-C3-340-05]
    """
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
    """A conditional re-fetch has two halves in two modules, and they have to agree on one string.

    `mdc/http.py` held both: `_cache_lookup(url)` at :229 and `_cache_store(url, ...)` at :289, one
    function and one variable, so read key and write key could not disagree. Named change 1 deleted
    the write - `acquire/rawstore` owns `raw_document` now - and left two natural spellings behind:
    `_validators` read the BARE url while `params` went to httpx separately, and `Response.url` is
    the url that was actually on the wire.

    BOTH DIRECTIONS FAIL, which is why the key and not the documentation is the fix. Six of the
    eight adapters M5.3 ports fetch a shared endpoint where the query is the only thing that
    distinguishes one title's document from another's (`mdc/sources/omdb.py:37` is one url for
    every title), so one document's ETag conditioned another document's request; and an adapter
    storing the value the fetcher handed it got no conditioning at all, for ever, with no error, no
    log and nothing in `total_bytes` to see it by.

    `Response.request_url` is what closes it rather than a rule in a docstring: the string the
    validators were read under is carried on the answer, so `rawstore.store(url=...)` has one
    value to be handed and an adapter cannot key the two sides differently.
    [M5.1 review cycle 3, port-C3-01]
    """
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


async def test_the_report_says_what_the_drain_did_after_the_context_has_closed(db):
    """`host_report()`'s own docstring: "What this drain did, per host, for section 6.6 and for the
    drain's own job detail." The drain assembles its job detail after the `async with` block.

    `__aexit__` calls `persist_host_state`, which zeroed `rt.requests` and `rt.errors` as it
    flushed them - so the obvious shape, and the one M5.3's stage 2 will write, published "this
    drain made no requests to any host" on the same tick `fetch_host_state` recorded that it had.
    Two numbers about one drain, disagreeing, with the wrong one on the surface section 6.6 reads.

    THE FLUSH STILL ADDS EACH REQUEST EXACTLY ONCE, which is the second assertion: the counters
    are a high-water mark and the flush writes the delta, so `persist_host_state`'s "a second call
    adds nothing" is the delta being zero rather than the counter being cleared.
    [M5.1 review cycle 3, port-C3-06]
    """
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
    """Decision 340 puts the per-host rate in section 6.6 "as data rather than as constants, so
    section 6.6 can show them" - and `_pace_from_robots` can move that rate down with nothing
    recording it.

    A host publishing `Crawl-delay: 30` is crawled at a thirtieth of a request a second while
    `host_report()` published `rt.policy.rps`, the declared ceiling: fifteen times wrong for
    `www.film-rezensionen.de`, and four of the rows `hosts.py` declares are "small sites run by
    individuals", which is the population most likely to publish one. Every other key in that row
    is a measured fact about the drain, so the one that is a configured ceiling has to say which
    it is. [M5.1 review cycle 3, port-C3-05]
    """
    host = "www.film-rezensionen.de"

    def publisher(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
        return httpx.Response(200, content=b"page")

    async with _fetcher(publisher, _Clock()) as f:
        await f.get(f"https://{host}/kritik/x")
        row = {r["host"]: r for r in f.host_report()}[host]

    assert row["rps"] == HOST_POLICIES[host].rps == 0.5, "the declared ceiling is still published"
    assert row["effective_rps"] == pytest.approx(1 / 30), (
        "the board would tell an operator this host is crawled fifteen times faster than it is"
    )


# --- review cycle 4: the answers a robots fetch only half received -------------------------------


class _BreaksMidBody(httpx.AsyncByteStream):
    """A response whose headers arrive and whose body does not.

    httpx's `stream()` returns once the response headers are read, so `resp.status_code` is
    already 200 by the time the body fails - which is the whole shape of the defect below. The
    four exceptions this stands in for are the ordinary ones: a reset, a truncated chunked body,
    a corrupt gzip (this client declares `Accept-Encoding: gzip, deflate`) and a read timeout.
    """

    def __init__(self, first: bytes, exc: Exception) -> None:
        self._first, self._exc = first, exc

    async def __aiter__(self):
        yield self._first
        raise self._exc


async def test_a_robots_txt_whose_body_never_arrives_is_not_a_200(db):
    """RFC 9309 §2.3.1.4 again, in the one status class the earlier repairs could not reach.

    `_fetch_robots` assigns `status` INSIDE the `stream` block and `body` only after it closes, so
    a failure while the body was being read was swallowed with `status` already 200 and `body`
    still `""`. `_is_robots_answer(200)` is True, so the refusal was skipped, `rt.errors` was not
    incremented, `_parse_robots("", 200)` parsed an EMPTY ruleset - which `urllib.robotparser`
    answers as allow-all - and the unconditional UPSERT wrote that non-answer into
    `fetch_host_state` with `robots_fetched_at = now()`, where `_load_host_state` reads it back as
    a valid cache for twenty-four hours across every worker restart. Measured: a host publishing
    `Disallow: /` was crawled, the board showed zero errors, and the log line said "refusing this
    host for now" while the request went out.

    The host chooses the transfer encoding and can cut the body, so it can trigger this itself.
    [M5.1 review cycle 4, M51-C4-340-01]
    """
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

    # The control: the same file, whole. The refusal is the host's own and the cache is the
    # host's own, which is what makes the three arms above a defect rather than a policy.
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
    """§8 (`spec:404`): the fetcher "honours each host's robots.txt". `can_fetch` matches on QUERY.

    `get` took the robots decision against the caller's `url` and computed `request_url` - the
    string httpx actually puts on the wire - twenty-four lines later. So a caller passing its
    query as `params=`, which this module's own port-C3-01 comment records as the convention six
    of the eight adapters M5.3 ports use and which is how `mdc/blogs.py` fetches
    `/wp-json/wp/v2/posts` against four hosts `hosts.py` declares with the default
    `respect_robots=True`, had its robots decision taken against a url that never went out.

    ONE FUNCTION, TWO ANSWERS, ONE BYTE-IDENTICAL REQUEST: the hop path joins the `Location` in
    first and judges the joined url, so the same url reached by a redirect was refused while the
    url asked for directly was fetched. This is the read-key/write-key split port-C3-01 closed for
    `_validators`, left open on the other consumer of the same string.
    [M5.1 review cycle 4, M51-C4-340-02]
    """
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

        # The other side of the line, so this is not a refusal of the endpoint: the same endpoint
        # under a query the host did not disallow is still fetched, and both spellings agree.
        assert (await f.get(f"{posts}?per_page=10")).status == 200
        answer = await f.get(posts, params={"per_page": 10})
        assert answer.status == 200
        assert answer.request_url == f"{posts}?per_page=10"


async def test_a_robots_cache_timestamped_in_the_future_is_not_a_cache(db):
    """Decision 340's own review question: "the cache cannot pin a stale allow."

    `_load_host_state` gated the cached row on `age < ROBOTS_TTL_SECONDS` with no lower bound,
    where `age` is `now() - robots_fetched_at`. A row timestamped in the FUTURE is a negative age,
    which satisfies that test for as long as the clock takes to catch up - indefinitely for a box
    whose RTC booted ahead before NTP pulled it back, or for a dump restored from one that had.
    `rt.robots_checked` then suppresses every further robots request, so the host is never asked
    again and any `Disallow` it publishes in the meantime is not seen.

    The sibling read in the same function fails the safe way under the same skew -
    `paused_remaining_s > 0` merely pauses the host longer - and this one failed OPEN, which is
    the direction the paragraph above it says it was written to close. [M5.1 review cycle 4,
    M51-C4-340-04]
    """
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
                # The control: a row the database could have written IS a cache, so the allow-all
                # it records is honoured and no robots request goes out. Without this the
                # assertion below would also pass on a build that never cached anything.
                assert (await f.get(f"https://{host}/private/thing")).status == 200
                assert asked == ["/private/thing"], asked
            else:
                with pytest.raises(RobotsDisallowed):
                    await f.get(f"https://{host}/private/thing")
                assert asked == ["/robots.txt"], (
                    "a timestamp the database cannot have written was read as a valid cache"
                )


# --- review cycle 4, second pass: the two fail-opens a third party spells ------------------------


async def test_a_robots_txt_whose_first_line_outruns_the_limit_is_not_an_answer(db):
    """Named change 11: "a robots.txt this app could not read refuses the request rather than
    allowing it, and is not written to the cache".

    `_truncate_robots` cuts at a line boundary, and when the host's FIRST line is longer than
    `ROBOTS_MAX_BYTES` nothing survives the cut at all. `status` was still 200, so
    `_is_robots_answer` skipped the refusal, `_parse_robots("", 200)` parsed an empty ruleset that
    `urllib.robotparser` answers as allow-all, and the unconditional UPSERT wrote that non-answer
    into `fetch_host_state` where `_load_host_state` honours it for `ROBOTS_TTL_SECONDS` across
    every worker restart - asking the host nothing for a day. That is the same fail-open
    M51-340-01/03 closed for a 5xx, M51-C2-340-01 for a 3xx and M51-C4-340-01 for a body that
    never arrived, in the one class those repairs could not reach: the read SUCCEEDS and no rule
    survives the cut.

    THE CUT IS WHAT MAKES IT A NON-ANSWER, and the two controls below are where the line is drawn.
    A host that genuinely publishes an empty file was read in full, so allow-all is its own
    answer and is cached; a host whose rules precede the cut is obeyed, which is
    `test_a_robots_txt_larger_than_the_parsing_limit_is_cut_between_lines`' whole assertion and the
    reason RFC 9309 section 2.5's parsing limit is legitimate. Only "bytes arrived and this app
    read no rule out of them" is RFC 9309 2.3.1.4's unreachable.
    [M5.1 review cycle 4 second pass, M51-C4-340-06]
    """
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

    # Control one: a host that really does publish nothing. The whole file arrived, so allow-all
    # is the host's own answer and the cache is the host's own cache.
    def silent(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, content=b"page")

    async with _fetcher(silent, _Clock(), conn=db) as f:
        assert (await f.get("https://silent.example/anything")).status == 200
    assert await db.fetchval(
        "SELECT robots_status FROM fetch_host_state WHERE host = 'silent.example'"
    ) == 200

    # Control two: a rule before the cut is still obeyed, so this refuses damage rather than
    # refusing every large file.
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
    """`Retry-After` is a header a third party chooses, and `-1` switched this layer's pacing off.

    The seconds branch was `min(float(raw), 300.0)` with no floor, so a negative value survived
    the cap and reached `await self._sleep(-1.0)`, which returns immediately. Only the 429 arm
    escaped it, because `max(delay, _backoff(attempt) * 3)` rescues a negative delay there - and
    429 is the only status the registered drives carry a `Retry-After` on, so nine of the eleven
    members of `RETRYABLE_STATUS` had no assertion standing over this at all. A broken gateway
    with a skewed clock emits exactly this value. [M5.1 review cycle 4 second pass, M51-C4-340-07]
    """
    clock = _Clock()
    async with _fetcher(_always(503, headers={"Retry-After": "-1"}), clock) as f:
        with pytest.raises(FetchError) as caught:
            await f.get(f"https://{FAST}/3/movie/603", max_attempts=3)
    assert caught.value.status == 503
    assert clock.slept == [pytest.approx(1.6 * 0.7), pytest.approx(3.2 * 0.7)], (
        "a host chose this fetcher's backoff curve away with one header"
    )

    # And `nan` reached `asyncio.sleep`, which raises `ValueError` - out of `get`, past the type
    # `FetchError`'s docstring says is "every failure this layer raises".
    nan_clock = _Clock()
    async with _fetcher(_always(503, headers={"Retry-After": "nan"}), nan_clock) as f:
        with pytest.raises(FetchError):
            await f.get(f"https://{FAST}/3/movie/604", max_attempts=2)
    assert nan_clock.slept == [pytest.approx(1.6 * 0.7)]
