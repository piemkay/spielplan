"""Where a poster comes from, and how often anyone is asked for it. Spec v2.1 §6.8, §7.1, §8's
politeness clause; decisions 483 and 485.

No database: `sources.read` is replaced by the row the test states, the third-party image host is
an `httpx.MockTransport`, and the household's Jellyfin is `ops/fake_jellyfin.py` mounted
in-process - the double answers with the bytes a real server sends and refuses what one refuses,
so the first source is asked over HTTP rather than assumed. The clock is injected so a 180-day
re-fetch is asserted rather than waited for.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from spielplan.art import cache, poster, sources
from spielplan.art.poster import ArtService
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
W500 = "https://image.tmdb.org/t/p/w500/abc123.jpg"
W342 = "https://image.tmdb.org/t/p/w342/abc123.jpg"
AMAZON = "https://m.media-amazon.com/images/M/abc._V1_SX300.jpg"
JELLYFIN_URL = "http://jellyfin.test"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Host:
    """The image host: what it was asked, and what it answers."""

    def __init__(self, answer=None) -> None:
        self.asked: list[str] = []
        self.answer = answer or (lambda request: httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        result = self.answer(request)
        return await result if asyncio.iscoroutine(result) else result


class Pool:
    """`connect()` as the route hands it in, counting what is held."""

    def __init__(self) -> None:
        self.held = 0
        self.opened = 0

    @asynccontextmanager
    async def connect(self):
        self.held += 1
        self.opened += 1
        try:
            yield object()
        finally:
            self.held -= 1


def _row(**overrides) -> sources.PosterRow:
    base = dict(title_id=7, jellyfin_id=None, poster_path=W500, lookup_outcome=None,
                lookup_url=None, lookup_owed=False)
    return sources.PosterRow(**{**base, **overrides})


@pytest.fixture
def row(monkeypatch):
    """The title the route read; a test replaces `row["row"]` to change what the database says."""
    state = {"row": _row()}

    async def read(conn, title_id):
        return state["row"]

    monkeypatch.setattr(sources, "read", read)
    return state


@pytest.fixture
async def service(tmp_path):
    made: list[ArtService] = []

    async def make(host: Host, *, clock=None, egress=True) -> ArtService:
        svc = await ArtService(
            tmp_path / "art", egress=egress, transport=httpx.MockTransport(host),
            clock=clock or Clock(),
        ).open()
        made.append(svc)
        return svc

    yield make
    for svc in made:
        await svc.close()


@pytest.fixture
def jellyfin(fake_jellyfin, monkeypatch):
    """The household's server, configured, with the double behind `registry.make_client`."""
    module, transport = fake_jellyfin

    async def load(conn):
        return registry.JellyfinConfig(url=JELLYFIN_URL, api_key=module.API_KEY)

    monkeypatch.setattr(registry, "load_jellyfin", load)
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(JELLYFIN_URL, module.API_KEY, transport=transport),
    )
    return module


async def test_a_tmdb_poster_is_fetched_at_w342_once_and_then_served_from_disk(row, service):
    """The corpus stored w500; decision 483 serves w342 by rewriting the segment, never by
    prefixing it, and the second view of the same card costs a disk read and no request."""
    host, pool = Host(), Pool()
    svc = await service(host)
    first = await svc.poster(7, connect=pool.connect)
    second = await svc.poster(7, connect=pool.connect)
    assert host.asked == [W342], "w500 must be rewritten to w342, and asked for once"
    assert (first.status, first.content_type, first.body) == (200, "image/jpeg", JPEG)
    assert first.max_age == 15552000
    assert second.body == JPEG and second.etag == first.etag


async def test_imdb_hosted_art_never_reaches_the_transport(row, service):
    """`m.media-amazon.com` is never fetched, proxied or stored - so a title whose only art is
    there is the tinted panel, and the request that would have fetched it is never made."""
    row["row"] = _row(poster_path=AMAZON, lookup_owed=True)
    host = Host()
    svc = await service(host)
    answer = await svc.poster(7, connect=Pool().connect)
    assert answer.status == 404 and host.asked == []
    assert answer.max_age == poster.BROWSER_PENDING, "a lookup is filed, so the panel is re-asked"


async def test_an_upstream_404_is_remembered_and_not_asked_again_inside_its_ttl(row, service):
    clock = Clock()
    host = Host(lambda request: httpx.Response(404))
    svc = await service(host, clock=clock)
    first = await svc.poster(7, connect=Pool().connect)
    again = await svc.poster(7, connect=Pool().connect)
    assert (first.status, again.status) == (404, 404)
    assert first.max_age == poster.BROWSER_NONE, "a cacheable 404, or every render re-asks"
    assert len(host.asked) == 1
    clock.now += poster.MISSING_TTL
    await svc.poster(7, connect=Pool().connect)
    assert len(host.asked) == 2


async def test_a_host_that_refuses_is_asked_again_after_ten_minutes(row, service):
    """A refusal that is not "no such file" - a CDN's 403 here, which the fetcher does not retry -
    is no answer about the poster, so it is kept ten minutes and not a week."""
    clock = Clock()
    host = Host(lambda request: httpx.Response(403))
    svc = await service(host, clock=clock)
    answer = await svc.poster(7, connect=Pool().connect)
    asked = len(host.asked)
    assert answer.status == 404 and answer.max_age == poster.BROWSER_TRANSIENT
    await svc.poster(7, connect=Pool().connect)
    assert len(host.asked) == asked, "the negative sidecar answers inside its ten minutes"
    clock.now += poster.ERROR_TTL
    await svc.poster(7, connect=Pool().connect)
    assert len(host.asked) > asked


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "image/jpeg"}),
        httpx.Response(200, content=JPEG + b"\x00" * poster.MAX_BYTES,
                       headers={"content-type": "image/jpeg"}),
    ],
    ids=["declared-html", "html-called-jpeg", "over-the-cap"],
)
async def test_bytes_that_are_not_a_poster_are_never_stored_or_served(row, service, answer):
    """The bytes leave this origin under this app's name, so what they ARE is checked here."""
    svc = await service(Host(lambda request: answer))
    served = await svc.poster(7, connect=Pool().connect)
    assert served.status == 404 and served.body == b""
    assert svc.cache.read(7, _row().signature).status == cache.MISSING


async def test_two_phones_asking_at_once_cost_one_upstream_request(row, service):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    host = Host(slow)
    svc = await service(host)
    pool = Pool()
    both = asyncio.gather(svc.poster(7, connect=pool.connect), svc.poster(7, connect=pool.connect))
    await asyncio.sleep(0.05)
    assert pool.held == 0, "a connection was held while the host was being asked"
    gate.set()
    first, second = await both
    assert len(host.asked) == 1
    assert first.body == second.body == JPEG


async def test_a_tmdb_file_is_fetched_again_after_180_days(row, service):
    clock = Clock()
    host = Host()
    svc = await service(host, clock=clock)
    await svc.poster(7, connect=Pool().connect)
    clock.now += poster.THIRD_PARTY_TTL - 1
    await svc.poster(7, connect=Pool().connect)
    assert len(host.asked) == 1
    clock.now += 1
    await svc.poster(7, connect=Pool().connect)
    assert len(host.asked) == 2


async def test_a_new_source_is_a_new_question_under_the_same_url(row, service):
    """A title re-derived with a different poster, or become owned, is not answered from the
    entry the old sources wrote."""
    host = Host()
    svc = await service(host)
    await svc.poster(7, connect=Pool().connect)
    row["row"] = _row(poster_path="https://image.tmdb.org/t/p/w500/other.jpg")
    await svc.poster(7, connect=Pool().connect)
    assert host.asked == [W342, "https://image.tmdb.org/t/p/w342/other.jpg"]


async def test_the_households_jellyfin_is_asked_first(row, service, jellyfin):
    clock = Clock()
    row["row"] = _row(jellyfin_id="jf-1")
    host = Host()
    svc = await service(host, clock=clock)
    answer = await svc.poster(7, connect=Pool().connect)
    assert answer.status == 200 and answer.content_type == "image/png"
    assert jellyfin.IMAGES_ASKED == ["jf-1"] and host.asked == []
    assert svc.cache.read(7, row["row"].signature).expires_at == clock.now + poster.JELLYFIN_TTL


async def test_a_title_jellyfin_holds_no_image_for_falls_through_to_tmdb(row, service, jellyfin):
    """The double's Tampopo has no Primary image, as a real server's unidentified film has none."""
    clock = Clock()
    row["row"] = _row(jellyfin_id="jf-8")
    host = Host()
    svc = await service(host, clock=clock)
    answer = await svc.poster(7, connect=Pool().connect)
    assert answer.status == 200 and answer.body == JPEG
    assert jellyfin.IMAGES_ASKED == ["jf-8"] and host.asked == [W342]
    assert svc.cache.read(7, row["row"].signature).expires_at == clock.now + poster.THIRD_PARTY_TTL


async def test_jellyfin_down_falls_through_to_tmdb_and_is_asked_again_tomorrow(
    row, service, jellyfin, monkeypatch
):
    """§3.3: the app works when Jellyfin is down, and an owned title must not 404 for it - the
    TMDB file stands in for a day rather than for 180, so the household's own art comes back."""
    def unreachable(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(JELLYFIN_URL, "key", transport=httpx.MockTransport(unreachable)),
    )
    clock = Clock()
    row["row"] = _row(jellyfin_id="jf-1")
    svc = await service(Host(), clock=clock)
    answer = await svc.poster(7, connect=Pool().connect)
    assert answer.status == 200 and answer.body == JPEG
    assert svc.cache.read(7, row["row"].signature).expires_at == clock.now + poster.STAND_IN_TTL


async def test_with_egress_off_no_image_host_is_asked_and_jellyfin_still_is(
    row, service, jellyfin
):
    """Decision 483's switch, which e2e and CI run under: no internet host is asked for a poster,
    and the fake Jellyfin - which is not the internet - still serves one."""
    host = Host()
    svc = await service(host, egress=False)
    none = await svc.poster(7, connect=Pool().connect)
    assert none.status == 404 and host.asked == []
    assert svc.cache.read(7, _row().signature) is None, "an answer nobody was asked is not kept"
    row["row"] = _row(jellyfin_id="jf-2")
    owned = await svc.poster(7, connect=Pool().connect)
    assert owned.status == 200 and jellyfin.IMAGES_ASKED == ["jf-2"]


async def test_a_redirect_off_the_allow_list_is_neither_stored_nor_served(row, service):
    def answer(request):
        if request.url.host == "image.tmdb.org":
            return httpx.Response(302, headers={"location": AMAZON})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    svc = await service(Host(answer))
    served = await svc.poster(7, connect=Pool().connect)
    assert served.status == 404
    assert not (svc.cache.root / "7.img").exists()


async def test_a_title_the_database_does_not_hold_is_a_cacheable_404(monkeypatch, service):
    async def read(conn, title_id):
        return None

    monkeypatch.setattr(sources, "read", read)
    answer = await (await service(Host())).poster(999, connect=Pool().connect)
    assert (answer.status, answer.max_age) == (404, poster.BROWSER_NONE)


def test_the_candidates_are_jellyfin_then_the_stored_path_then_the_lookup():
    both = _row(jellyfin_id="jf-1", lookup_outcome=sources.FOUND,
                lookup_url="https://image.tmdb.org/t/p/w342/found.jpg")
    assert [c.source for c in both.candidates()] == ["jellyfin", "poster_path", "lookup"]
    assert [c.source for c in _row(poster_path=AMAZON).candidates()] == []
    looked = _row(poster_path=None, lookup_outcome=sources.FOUND,
                  lookup_url="https://image.tmdb.org/t/p/w342/found.jpg")
    assert [c.url for c in looked.candidates()] == ["https://image.tmdb.org/t/p/w342/found.jpg"]


def test_both_image_hosts_are_declared_with_the_reasoning_for_their_robots_override():
    """Decision 340 as the art route meets it: each servable host has a measured row §6.6 can
    show, and neither is left on the slow default with robots honoured - which, in a fetcher that
    lives as long as the web process, would let one unanswered robots.txt refuse every poster from
    that host until a restart (decision 485)."""
    from spielplan.acquire.hosts import HOST_POLICIES, undocumented_overrides
    from spielplan.art.hosts import SERVABLE_HOSTS

    for host in SERVABLE_HOSTS:
        policy = HOST_POLICIES[host]
        assert policy.respect_robots is False and policy.note, host
    assert HOST_POLICIES["static.tvmaze.com"].rps == 2.0, "api.tvmaze.com's own rate"
    assert undocumented_overrides() == []


def test_the_page_remembers_a_404_no_longer_than_the_shortest_one_it_is_sent():
    """`lib/art.js` stops a card asking for a poster the route has just answered 404 for, for as
    long as the browser's own cache would give the same answer. That holds only while the page's
    memory is no longer than the shortest max-age a 404 carries; a longer one would hide art the
    browser would already fetch again (decision 483's cacheable 404s)."""
    import re
    from pathlib import Path

    art_js = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "art.js"
    minutes = re.search(r"MISSING_FOR_MS = (\d+) \* 60 \* 1000;", art_js.read_text(encoding="utf-8"))
    assert minutes, "art.js no longer spells MISSING_FOR_MS in minutes"
    shortest = min(poster.BROWSER_NONE, poster.BROWSER_PENDING, poster.BROWSER_TRANSIENT)
    assert int(minutes.group(1)) * 60 == shortest


async def test_the_jellyfin_client_reads_the_primary_image_as_bytes(fake_jellyfin):
    """§7.1's client, against the double: the resized Primary image, None for an item with none."""
    module, transport = fake_jellyfin
    client = JellyfinClient(JELLYFIN_URL, module.API_KEY, transport=transport)
    data, content_type = await client.primary_image("jf-1")
    assert poster.sniff(data) == "image/png" and content_type.startswith("image/png")
    assert await client.primary_image("jf-8") is None
