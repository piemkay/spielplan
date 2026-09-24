"""The Jellyfin client against a real (fake) server. Spec v2.1 §7.1, §7.3, §14.3.

`ops/fake_jellyfin.py` answers over ASGI, so every assertion here is about an HTTP exchange:
the header the app sends, the route it picks, the parameters it builds. No database needed.

The load-bearing one is `test_the_admin_key_is_refused_on_a_played_write`. §14.3 says a
Jellyfin API key is unscoped and admin-equivalent with no read-only variant, so §7.3's
per-user-token rule is enforced by this app's code and nothing else. The fake refuses the
admin key on that route on purpose: it is the only way this restraint can fail a test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from spielplan.connectors.jellyfin import (
    FIELDS,
    MAX_PAGES,
    MIN_SERVER_VERSION,
    JellyfinClient,
    JellyfinError,
    NowPlaying,
)


@pytest.fixture
def client(fake_jellyfin):
    module, transport = fake_jellyfin
    return module, JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)


# --- §7.1: auth and the field list -----------------------------------------------------


async def test_the_api_key_travels_as_x_emby_token(client):
    module, jf = client
    assert len(await jf.users()) == 2

    wrong = JellyfinClient("http://jellyfin.test", "not-the-key", transport=jf.transport)
    with pytest.raises(JellyfinError) as exc:
        await wrong.users()
    assert exc.value.status == 401


async def test_the_key_travels_where_a_jellyfin_12_server_reads_it(client, monkeypatch):
    """§7.1 pins Jellyfin >= 10.9 with no ceiling, and `version_supported("12.1")` says yes -- but
    12.0 turned `EnableLegacyAuthorization` off by default and migrates upgraded installs to off,
    and with it off the server reads `X-Emby-Token` not at all
    (`AuthorizationContext.cs`: `if (EnableLegacyAuthorization && string.IsNullOrEmpty(token))
    { token = headers["X-Emby-Token"]; }`). The token it always reads is `Token=` inside the
    `MediaBrowser` Authorization header, on every release from 10.9 on. This client sent the key
    only in `X-Emby-Token`, and the double read it only there, so every read below was a 401 on
    the current release while the whole suite agreed with itself.

    The double now reports 12.1 and extracts the token as that server does. Every authenticated
    read an intake path, the full sweep or §7.3 makes is asked, and the admin key is still refused
    on the Played write -- that refusal is the double's, on purpose, and a 12.x server does not
    lose it. A key carrying characters the header's own grammar uses still arrives whole, because
    the server URL-decodes the value (`GetParts`). [M5.2 review cycle 4: M52-C4-AUTH-01,
    M52-C4-TTA-03]
    """
    module, jf = client
    monkeypatch.setattr(module, "SERVER_VERSION", "12.1.0")
    assert (await jf.check())["supported"] is True

    async with httpx.AsyncClient(transport=jf.transport, base_url="http://jellyfin.test") as raw:
        legacy_only = await raw.get("/Users", headers={"X-Emby-Token": module.API_KEY})
    assert legacy_only.status_code == 401, "a 12.x server reads no X-Emby-Token by default"

    assert len(await jf.users()) == 2
    assert len(await jf.libraries()) == len(module.LIBRARIES)
    assert await jf.library_folders()
    assert len(await jf.all_items(None)) == 7
    assert await jf.item_in_libraries("jf-1", ["jf-lib-films"]) is not None
    assert len(await jf.items_created_since(_watermark("2018-01-01T00:00:00"))) == 7
    assert await jf.episodes("jf-6")
    assert await jf.sessions() == []
    user_id, token = await jf.authenticate_by_name("patrick", module.PASSWORD)
    await jf.set_played("jf-1", user_id, True, token)
    assert "jf-1" in module.state.played[user_id]
    with pytest.raises(JellyfinError) as refused:
        await jf.set_played("jf-1", user_id, True, module.API_KEY)
    assert refused.value.status == 403, "the double still refuses the admin key on the write"

    monkeypatch.setattr(module, "API_KEY", 'an odd+key/with,"quotes"=')
    odd = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=jf.transport)
    assert len(await odd.users()) == 2


async def test_the_public_info_route_needs_no_key(client):
    """Which is what lets §6.6's test button tell a wrong URL from a wrong key."""
    _module, jf = client
    keyless = JellyfinClient("http://jellyfin.test", "", transport=jf.transport)
    info = await keyless.server_info()
    assert info["ServerName"] == "Fake Jellyfin"


async def test_the_check_reports_the_pinned_version(client):
    _module, jf = client
    probe = await jf.check()
    assert probe["supported"] is True
    assert probe["min_version"] == "10.9"
    assert probe["user_count"] == 2


async def test_an_older_server_fails_the_pin(client, monkeypatch):
    """§7.1 pins >= 10.9 because /UserPlayedItems and /Items?userId= are 10.9 routes. An 10.8
    server answers /System/Info and then 404s the routes that matter, so the version has to be
    checked rather than the socket."""
    module, jf = client
    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    assert (await jf.check())["supported"] is False
    assert MIN_SERVER_VERSION == (10, 9)


async def test_items_request_the_proven_field_set(client):
    """§7.1: "the proven FIELDS set incl. ProviderIds, MediaStreams, DateCreated, UserData"."""
    for field in ("ProviderIds", "MediaStreams", "DateCreated", "UserData"):
        assert field in FIELDS

    _module, jf = client
    items = await jf.all_items("jf-user-patrick")
    assert len(items) == 7
    assert items[0]["ProviderIds"]["Imdb"] == "tt0113277"
    assert items[0]["UserData"]["Played"] is False


async def test_user_data_is_per_user(client):
    """The whole seen sync depends on this: `UserData` is scoped to the id in the query."""
    module, jf = client
    module.state.played["jf-user-jenny"].add("jf-1")

    mine = {i["Id"]: i["UserData"]["Played"] for i in await jf.all_items("jf-user-patrick")}
    theirs = {i["Id"]: i["UserData"]["Played"] for i in await jf.all_items("jf-user-jenny")}
    assert mine["jf-1"] is False
    assert theirs["jf-1"] is True


async def test_paging_walks_the_whole_library(client):
    _module, jf = client
    assert len(await jf.all_items("jf-user-patrick", page=2)) == 7


def _paging_client(*, total: int, honour_start: bool, library: int | None = 0, abort_after: int):
    """A client against a server with stated paging manners, plus the list of what it asked for.

    `honour_start=False` is the shape that wedged the worker: a server -- or a proxy caching page
    one -- that ignores `StartIndex` and answers every request with the same page, so the
    short-page exit never arrives. `library=None` is the other endless shape, and the one the cap
    is now the only guard against: `StartIndex` honoured, every page full, no count ever given.
    `abort_after` is what turns "pages for ever" into a failure this suite can see instead of a
    hang; the plan's probe of the old loop got 41 pages and 20,500 dicts before the harness
    stopped it.
    """
    asked: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("StartIndex", 0))
        limit = int(request.url.params.get("Limit", 500))
        asked.append(start)
        assert len(asked) <= abort_after, f"the client never stopped: {len(asked)} pages"
        offset = start if honour_start else 0
        bounded = honour_start and library is not None
        stop = min(offset + limit, library) if bounded else offset + limit
        rows = [{"Id": f"i-{n}", "Type": "Movie"} for n in range(offset, max(stop, offset))]
        return httpx.Response(200, json={"Items": rows, "TotalRecordCount": total})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    return jf, asked


async def test_the_walk_stops_at_the_servers_own_total_record_count():
    """§5.3 gives every job a budget. The count was already being asked for by name
    (`EnableTotalRecordCount`) and thrown away, and the only exit left was a short page -- which a
    server that ignores `StartIndex` never sends.

    Two cases, because one page being the whole library never asks the count to arbitrate
    anything: the second walk crosses two pages and stops without spending a third request on a
    library it has already finished reading. The count is compared against DISTINCT ids -- see
    the sibling below for why that word carries the whole guard.
    """
    jf, asked = _paging_client(total=4, honour_start=False, abort_after=50)
    assert len(await jf.all_items("jf-user-patrick", page=4)) == 4
    assert asked == [0], "the server said how many there were; one page was the whole library"

    jf, asked = _paging_client(total=8, honour_start=True, library=8, abort_after=50)
    assert len(await jf.all_items("jf-user-patrick", page=4)) == 8
    assert asked == [0, 4], "the count ends a multi-page walk without a third round trip"


async def test_a_full_page_that_repeats_one_already_read_raises_instead_of_truncating():
    """The count is only a bound if it is counted in the same units the server is answering in.

    Measured against the exact server this module's own comment names -- one that ignores
    `StartIndex`, or a proxy caching page one -- while it reports an HONEST `TotalRecordCount`:
    the old walk compared `len(out)`, i.e. rows returned, so page one arriving three times
    satisfied `>= 10` after twelve rows and four distinct titles, `MAX_PAGES` was never reached
    and `all_items` returned a page-one library with no error. §7.2's ownership pass then un-owns
    everything past the cut -- the outcome step 1b exists to forbid ("do not silently truncate")
    and the one `_falsify_ownership` names as the most destructive statement it makes.
    [M4.11 review cycle 1: m411-rev-jf-01]
    """
    jf, asked = _paging_client(total=10, honour_start=False, abort_after=50)
    with pytest.raises(JellyfinError, match="ignoring StartIndex"):
        await jf.all_items("jf-user-patrick", page=4)
    assert asked == [0, 4], "one repeated page is the whole evidence; do not keep asking"


async def test_the_walk_raises_at_the_hard_page_cap():
    """And when the server will not even count, the cap stops it -- by raising, never by
    truncating: §7.2 re-derives ownership from this list, so a short read must abort the sweep
    rather than un-own everything past the cut.

    Driven against a server that HONOURS `StartIndex` and simply never runs out, because that is
    what the cap is now the last guard against: the page-one case above raises at the second
    request, so a fake that repeats itself would certify the wrong instrument.
    """
    jf, asked = _paging_client(
        total=0, honour_start=True, library=None, abort_after=MAX_PAGES + 5
    )
    with pytest.raises(JellyfinError, match="did not end after"):
        await jf.all_items("jf-user-patrick", page=2)
    assert len(asked) == MAX_PAGES


async def test_a_short_page_still_ends_the_walk_when_the_server_does_not_count():
    """The exit that was already there has to survive both new bounds: a server answering
    `TotalRecordCount: 0` is asking to be paged until a page comes back short."""
    jf, asked = _paging_client(total=0, honour_start=True, library=5, abort_after=20)
    assert len(await jf.all_items("jf-user-patrick", page=3)) == 5
    assert asked == [0, 3]


def _scripted_client(pages: list[dict | None]):
    """A server answering each request from a script, plus the offsets it was asked for.

    `_paging_client` above models a server with consistent manners; these are the page-SETS a real
    server only produces with something in front of it -- one page served as a gateway
    interstitial, or a library that changed between two requests. `None` is a 200 whose body is
    not JSON, which `_request` collapses to `{}` exactly as it does an empty body, a `null` body
    and a body with no `Items`.
    """
    served: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        served.append(int(request.url.params.get("StartIndex", 0)))
        page = pages[min(len(served) - 1, len(pages) - 1)]
        if page is None:
            return httpx.Response(200, text="<html>bad gateway</html>")
        return httpx.Response(200, json=page)

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    return jf, served


def _rows(first: int, last: int) -> list[dict]:
    return [{"Id": f"i-{n}", "Type": "Movie"} for n in range(first, last)]


async def test_a_short_page_the_server_says_is_short_raises_instead_of_truncating():
    """The sibling of the repeated page, and the shape §7.2's ownership pass is actually gated on.

    `TotalRecordCount` was only ever used to stop the walk EARLY; the short-page exit was
    unconditioned by the count on that same page, so a server answering fewer rows than `Limit`
    while still counting the rest handed back a partial library with no error. Measured against
    this module before the fix: `all_items` returned 5 of 11 and did not raise, and driven through
    `seen.sync_all` that page-set un-owned every title past the cut with a fresh
    `owned_checked_at`, pruned their copy rows and emptied Tonight's pool -- under an admin card
    reporting a successful sweep. The repeated-page raise cannot reach it: that one fires only on
    a FULL page, and the only other short-page test drives `total=0`, the one case where the count
    cannot arbitrate.

    Three page-sets, because the guard has to be right in three different units. A server that
    counts honestly and stops early raises. A page that came back as something other than JSON
    raises too, which is why the count is the last one the server GAVE rather than this page's --
    `_request` collapses that body to `{}` and this page counts nothing at all. And a library that
    genuinely shrank between two requests still exits cleanly, because the page that came back
    short carries the smaller count with it.
    [M4.11 review cycle 2: m411-rev2-resolve-01, m411-rev2-jf-01]
    """
    jf, asked = _paging_client(total=11, honour_start=True, library=5, abort_after=20)
    with pytest.raises(JellyfinError, match="stopped after 5 of the 11"):
        await jf.all_items("jf-user-patrick", page=8)
    assert asked == [0], "the count on the short page is the whole evidence; do not keep asking"

    jf, served = _scripted_client([{"Items": _rows(0, 4), "TotalRecordCount": 11}, None])
    with pytest.raises(JellyfinError, match="stopped after 4 of the 11"):
        await jf.all_items(None, page=4)
    assert served == [0, 4], "the interstitial is a page, and it ends the walk by raising"

    jf, served = _scripted_client([
        {"Items": _rows(0, 4), "TotalRecordCount": 11},
        {"Items": _rows(4, 7), "TotalRecordCount": 7},
    ])
    assert len(await jf.all_items(None, page=4)) == 7, (
        "a library that shrank under the walk is not a truncated read: its own last page says so"
    )


async def test_one_row_with_no_id_does_not_make_every_walk_a_truncated_one():
    """The negative control on the unit the new guard counts in.

    A row with no `Id` is kept and counted as progress on purpose -- "a corrupt row for `resolve`
    to refuse, not evidence about the server's paging" -- but it never enters the id SET. Compared
    against distinct ids, one such row anywhere in the library would leave every sweep for ever
    one short of `TotalRecordCount`, so the raise above would fire on a perfectly healthy server
    and `_note_unreachable` would report an outage that is not happening. The count is compared
    against rows KEPT for exactly this reason. [M4.11 review cycle 2: m411-rev2-resolve-01]
    """
    jf, served = _scripted_client([
        {"Items": [*_rows(0, 3), {"Type": "Movie"}], "TotalRecordCount": 4},
    ])
    assert len(await jf.all_items(None, page=8)) == 4
    assert served == [0]


async def test_the_library_can_be_read_once_with_no_user_id(client):
    """§7.2: ownership is "re-derived from Jellyfin, never trusted stale", and the library it is
    derived from is the one the admin key can see -- item visibility is per-user, and the union of
    two people's views is not the household's library. Omitting `userId` is one read for the whole
    sweep instead of one per member.

    The widening of the fake that this needs stays narrow, and both halves are asserted here: an
    unknown id that IS given still 404s, and a keyless read carries no `UserData` at all.
    """
    module, jf = client
    module.state.played["jf-user-patrick"].add("jf-1")

    household = await jf.all_items(None)
    assert [i["Id"] for i in household] == [i["Id"] for i in module.ITEMS]
    assert all("UserData" not in i for i in household), "a keyless read has no per-user state"

    mine = {i["Id"]: i["UserData"]["Played"] for i in await jf.all_items("jf-user-patrick")}
    assert mine["jf-1"] is True

    with pytest.raises(JellyfinError) as exc:
        await jf.all_items("jf-user-nobody")
    assert exc.value.status == 404, "an unknown id must not be read as no id"


# --- §7.3: /Sessions -------------------------------------------------------------------


async def test_sessions_reduce_to_what_is_playing(client):
    """A film plays itself; a series plays an **episode**. The session row for a series carries the
    episode's own `Id` and the folder only as a `SeriesId` (§7.3, decision 210), so a prompt that
    resolved `item_id` alone could never find the title -- which is why the whole Series partition
    sat outside §13's capture loop while the capture rate read healthy."""
    module, jf = client
    await module.force_session(
        module.SessionControl(user_id="jf-user-patrick", item_id="jf-1", fraction=0.5)
    )
    sessions = await jf.sessions()
    assert len(sessions) == 1
    assert sessions[0].jf_user_id == "jf-user-patrick"
    assert sessions[0].item_id == "jf-1"
    assert (sessions[0].item_type, sessions[0].series_id) == ("Movie", None)
    assert 0.49 < sessions[0].fraction < 0.51

    await module.force_session(
        module.SessionControl(
            user_id="jf-user-jenny", item_id="jf-6", fraction=0.96, session_id="sess-2"
        )
    )
    playing = next(s for s in await jf.sessions() if s.jf_user_id == "jf-user-jenny")
    assert playing.item_id == "jf-6-e2", "the session plays the episode, not the folder"
    assert (playing.item_type, playing.series_id) == ("Episode", "jf-6")
    assert playing.raw["SeriesName"] == "Severance", "the raw item travels for the resolver"
    assert playing.fraction > 0.9


async def test_the_episode_list_is_read_once_per_series(client):
    """Decision 210(c): a series finishes on its last known episode, so the prompt needs the list
    -- once per series and member, not once per session row, because the poll runs every 60 s and
    one member's several session rows on one show would otherwise be one read each.

    The comment on `EPISODE_CACHE_LIMIT` claimed the case it collapses is "two people on two
    phones, one series, one episode list", which is the one case this key cannot serve: decision
    210(c) reads the list `?userId=`-scoped because episode visibility is per-user, so the key
    carries the member and two members on one series are two reads by construction. Measured
    through `playback.poll`: two `/Sessions` rows, one client, two `GET /Shows/jf-6/Episodes`.
    That is the correct answer, so the assertion below states it as a fact rather than as a miss
    to fix -- a shared cache would answer one member's question with the other's visibility. This
    body drove the anonymous key, which production never uses.
    [M4.11 review cycle 2: m411-rev2-pb-01]
    """
    module, jf = client
    episodes = await jf.episodes("jf-6")
    assert [e["IndexNumber"] for e in episodes] == [1, 2]
    assert episodes[-1]["Id"] == "jf-6-e2"
    assert await jf.episodes("jf-6") == episodes
    assert module.EPISODES_ASKED == ["jf-6"], "the second call must not reach the server"

    module.EPISODES_ASKED.clear()
    for _row in range(2):
        await jf.episodes("jf-6", "jf-user-patrick")
    assert module.EPISODES_ASKED == ["jf-6"], (
        "one member's second session row on the same series must not re-read the list"
    )
    await jf.episodes("jf-6", "jf-user-jenny")
    assert module.EPISODES_ASKED == ["jf-6", "jf-6"], (
        "and the other member's read is a separate one, because the list is `?userId=`-scoped"
    )


async def test_an_unknown_series_has_no_episode_list(client):
    """And a refusal is not cached as an empty series -- an empty list would make every episode
    "the last one" (decision 210(c)) for as long as the client lives."""
    _module, jf = client
    for _attempt in (1, 2):
        with pytest.raises(JellyfinError) as exc:
            await jf.episodes("jf-nope")
        assert exc.value.status == 404


def test_a_session_with_no_runtime_is_not_ninety_percent_finished():
    """A live stream has no duration. Reporting 0 rather than dividing by zero keeps it out of
    the prompt population honestly."""
    assert NowPlaying("s", "u", "i", position_ticks=500, runtime_ticks=0).fraction == 0.0


# --- §7.3 / §14.3: the one write --------------------------------------------------------


async def test_a_played_write_uses_the_users_own_token(client):
    module, jf = client
    _jf_user, token = await jf.authenticate_by_name("patrick", module.PASSWORD)

    await jf.set_played("jf-1", "jf-user-patrick", True, token)
    assert "jf-1" in module.state.played["jf-user-patrick"]

    await jf.set_played("jf-1", "jf-user-patrick", False, token)
    assert "jf-1" not in module.state.played["jf-user-patrick"]
    assert [w["played"] for w in module.state.write_log] == [True, False]


async def test_a_server_below_the_pin_refuses_the_played_write_by_name(client):
    """§7.1 pins >= 10.9 for the **write**: below it `/UserPlayedItems` does not exist, so every
    push 404s for ever while the reads keep answering and the admin card prints a healthy sweep.

    The reason names the route and both versions because the only repair is an upgrade of the
    media server -- and it is not a re-link: §7.3's re-link prompt answers a 401, and no password
    the person can type adds a route to their server.
    """
    module, jf = client
    _id, token = await jf.authenticate_by_name("patrick", module.PASSWORD)
    old = JellyfinClient(
        "http://jellyfin.test", module.API_KEY, server_version="10.8.13",
        server_supported=False, transport=jf.transport,
    )

    refusal = old.played_write_refusal()
    assert "10.8.13" in refusal and "10.9" in refusal and "/UserPlayedItems" in refusal
    with pytest.raises(JellyfinError) as exc:
        await old.set_played("jf-1", "jf-user-patrick", True, token)
    assert str(exc.value) == refusal
    assert not exc.value.is_auth_failure, "a version problem is not a credential problem"
    assert module.state.write_log == [], "the refusal is local; the server never sees the write"

    # Nobody has probed this one, and that must not block the write: an install whose admin never
    # pressed the test button still has to be able to push.
    assert jf.played_write_refusal() is None
    await jf.set_played("jf-1", "jf-user-patrick", True, token)
    assert "jf-1" in module.state.played["jf-user-patrick"]


async def test_the_probe_reports_the_version_and_the_verdict(client, monkeypatch):
    """What the sweep re-reads at its head, because an operator upgrades the media server without
    ever coming back to this app's admin page (§7.1)."""
    module, jf = client
    assert await jf.probe_version() == (module.SERVER_VERSION, True)
    monkeypatch.setattr(module, "SERVER_VERSION", "10.8.13")
    assert await jf.probe_version() == ("10.8.13", False)


@pytest.mark.parametrize(
    "name,body",
    [
        ("an empty 200", b""),
        ("an auth portal answering 200 with HTML", b"<html><body>Sign in</body></html>"),
        ("a payload with no Version key", b'{"ServerName": "jellyfin"}'),
    ],
)
async def test_a_server_that_reports_no_version_is_unknown_and_not_below_the_pin(name, body):
    """§7.1's pin gates the Played write, so "the server did not say" must not read as "10.8".

    `_request` answers `{}` for all three of these, and `version_supported("")` is False -- so
    the probe stored `server_supported = false` on the connector, `_push` refused every Played
    write locally with no network attempt, and the refusal named no version because it falls back
    to the literal "below the pin". Permanently: the sweep re-probes every fifteen minutes and
    re-confirms it. The trigger is not exotic -- `/System/Info/Public` is tokenless by design here
    and is exactly the endpoint hardening guides tell people to block, because it leaks the server
    name and version, and a forward-auth proxy in front of it answers 200 with a login page. A
    4xx/5xx is a different thing and is already handled correctly (`_note_unreachable`, stored
    verdict untouched). [review cycle 1: m411-rev-jf-04]
    """
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, content=body)
        # `check` also enumerates users, which is the call that exercises the key. Answered
        # normally: the fault under test is on the version route alone.
        return httpx.Response(200, json=[])

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))

    assert await jf.probe_version() == ("", None), name
    assert (await jf.check())["supported"] is None, name

    jf.server_version, jf.server_supported = await jf.probe_version()
    assert jf.played_write_refusal() is None, (
        f"{name}: a refusal that cannot name a version is not §7.1's refusal -- the write is "
        "attempted and its real failure counted, which is what every install did before the "
        "verdict was stored at all"
    )


async def test_the_admin_key_is_refused_on_a_played_write(client):
    """§14.3: the admin key *would* work against a real server — it is admin-equivalent. This
    fake refuses it so that a regression which reaches for the admin key fails here instead of
    silently working in production with far too much authority."""
    module, jf = client
    with pytest.raises(JellyfinError) as exc:
        await jf.set_played("jf-1", "jf-user-patrick", True, module.API_KEY)
    assert exc.value.status == 403
    assert exc.value.is_auth_failure


async def test_a_write_with_no_token_never_reaches_the_network(client):
    """The refusal is local. A missing token must not fall back to the admin key, and must not
    become a request the server gets to answer however it likes."""
    module, jf = client
    with pytest.raises(JellyfinError, match="without a per-user token"):
        await jf.set_played("jf-1", "jf-user-patrick", True, "")
    assert module.state.write_log == []


async def test_one_users_token_cannot_write_another_users_state(client):
    module, jf = client
    _id, patrick_token = await jf.authenticate_by_name("patrick", module.PASSWORD)
    with pytest.raises(JellyfinError) as exc:
        await jf.set_played("jf-1", "jf-user-jenny", True, patrick_token)
    assert exc.value.status == 403


async def test_a_wrong_password_yields_no_token(client):
    _module, jf = client
    with pytest.raises(JellyfinError) as exc:
        await jf.authenticate_by_name("patrick", "wrong")
    assert exc.value.status == 401


# --- §3.3: the app works when Jellyfin does not -----------------------------------------


async def test_an_unreachable_server_raises_jellyfin_error_not_httpx(client):
    """§3.3: "the app must work when Jellyfin is down". Callers catch `JellyfinError`; letting
    an httpx exception escape would make that promise depend on which HTTP library is used."""
    _module, jf = client
    unreachable = JellyfinClient("http://127.0.0.1:1", "key", timeout=0.2)
    with pytest.raises(JellyfinError) as exc:
        await unreachable.users()
    assert exc.value.status is None
    assert not exc.value.is_auth_failure


def test_the_deep_link_is_the_web_player_route(client):
    """§7.1: "deep-link to the server's web player ({jf_url}/web/#/details?id={jellyfin_id})"."""
    _module, jf = client
    assert jf.deep_link("abc") == "http://jellyfin.test/web/#/details?id=abc"


# --- §7.1: the request contract, not just the constant ------------------------------------


async def test_items_are_requested_recursively(client):
    """Without `Recursive=true` a real Jellyfin returns the library folders, not the films in
    them. The fake answers empty for a non-recursive query, so forgetting it fails here rather
    than producing an empty sync that looks like an empty library."""
    _module, jf = client
    assert await jf.all_items("jf-user-patrick"), "the client must ask recursively"


async def test_items_are_narrowed_to_movies_and_series(client):
    """§4.1 rule 5: `kind` is movie|series. Asking for everything would drag episodes and
    music videos into a resolver that has nowhere to put them."""
    module, jf = client
    kinds = {item["Type"] for item in await jf.all_items("jf-user-patrick")}
    assert kinds <= {"Movie", "Series"}
    assert "Movie" in kinds and "Series" in kinds
    assert module.ITEM_TYPES_ASKED, "the fake recorded no IncludeItemTypes filter"


async def test_the_field_set_is_actually_requested(client):
    """The fake projects items down to Jellyfin's small default set unless `Fields` asks for
    more — so this fails if the client stops sending FIELDS, which an assertion over the
    FIELDS constant alone could never do."""
    _module, jf = client
    items = await jf.all_items("jf-user-patrick")
    first = items[0]
    assert "ProviderIds" in first, "ProviderIds is the identity payload (§7.1)"
    assert "UserData" in first, "UserData carries the Played flag the seen sync reads (§7.3)"


# --- §7.2: the delta read, the libraries, and one item's membership -----------------------


def _watermark(text: str) -> datetime:
    """An instant to read from, spelled the way `connector_config` stores one (decision 366)."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def test_the_delta_read_returns_what_the_server_saved_after_the_watermark(client):
    """§7.2's second intake path, the fifteen-minute delta poll, over what the server SAVED since
    the watermark (decision 409).

    The fake's seven items arrived across five years on purpose, so this is a filter rather than a
    tautology — a corpus saved at one instant passes identically against a server that drops the
    parameter on the floor. Tampopo and the Christmas footage are in the recent set though their
    FILES are older than the watermark, because it is the server's save that marks an arrival.
    """
    _module, jf = client
    recent = await jf.items_created_since(_watermark("2022-01-01T00:00:00"))
    assert {item["Name"] for item in recent} == {
        "Severance", "The Bear", "Tampopo", "Christmas 2019"
    }
    whole = await jf.items_created_since(_watermark("2018-01-01T00:00:00"))
    assert len(whole) == 7
    assert await jf.items_created_since(_watermark("2030-01-01T00:00:00")) == []


async def test_the_delta_read_keeps_what_the_server_saved_whatever_the_files_own_date(client):
    """`DateCreated` is not the instant an item entered the library, and this read threw real adds
    away on the belief that it was.

    Jellyfin stamps it from the FILE: `UseFileCreationTimeForDateAdded = true;` is the default
    (MediaBrowser.Model/Configuration/MetadataConfiguration.cs, v10.10.7), and
    `ResolverHelper.SetDateCreated` then writes `var dateCreated = info.CreationTimeUtc;` -- on Linux
    under .NET 8 the older of mtime and ctime -- while an NFO's `<dateadded>` overrides it. What
    marks the arrival is `DateLastSaved`, stamped `DateTime.UtcNow` when the scan saves the row, and
    that is what `MinDateLastSaved` selects on. So the server answered with a film copied in with
    its mtime kept, and this walk re-applied `DateCreated > since`, dropped it uncounted and let the
    watermark advance past it for good. The double spelled the two stamps as one instant and could
    not show it; it now carries both.

    Against the double first: Tampopo's file is from 2016 and the household's Christmas footage
    from 2019, and both arrived in 2024. Then against a scripted server, because a row the server
    answered with and that carries no `DateCreated` at all is an add too -- the server's own filter
    is the whole of the evidence. [M5.2 review cycle 4: M52-C4-REST-02, M52-C4-TTA-02]
    """
    _module, jf = client
    arrived = await jf.items_created_since(_watermark("2024-01-01T00:00:00"))
    assert {item["Id"] for item in arrived} == {"jf-8", "jf-x"}
    assert all(item["DateCreated"] < "2024" for item in arrived), "their files predate the mark"

    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params.get("MinDateLastSaved", ""))
        return httpx.Response(200, json={"Items": [
            {"Id": "copied", "Type": "Movie", "DateCreated": "2019-01-01T00:00:00.0000000Z"},
            {"Id": "added", "Type": "Movie", "DateCreated": "2024-01-01T00:00:00.0000000Z"},
            {"Id": "undated", "Type": "Movie"},
        ], "TotalRecordCount": 3})

    scripted = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    kept = await scripted.items_created_since(_watermark("2022-01-01T00:00:00"))
    assert [item["Id"] for item in kept] == ["copied", "added", "undated"]
    assert asked and asked[0].startswith("2022-01-01"), "the server narrows it, and only it"


async def test_the_delta_read_is_scoped_to_the_libraries_the_admin_picked(client):
    """decision 364: `library_ids` becomes the acquisition boundary and gets its first reader.
    An EMPTY pick is the whole server — the state of every install in existence, since nothing
    has ever written the field — and a non-empty one is a filter the server applies by
    `ParentId`, never a claim the event makes about itself.

    "Christmas 2019" is the household's own footage in a third library, so "the library nobody
    picked" is a choice here rather than the only alternative.
    """
    _module, jf = client
    since = _watermark("2018-01-01T00:00:00")
    picked = await jf.items_created_since(since, library_ids=["jf-lib-films"])
    films = {item["Id"] for item in picked}
    assert films == {"jf-1", "jf-2", "jf-3", "jf-8"}
    two = await jf.items_created_since(since, library_ids=["jf-lib-films", "jf-lib-shows"])
    both = {item["Id"] for item in two}
    assert both == films | {"jf-6", "jf-7"}
    assert "jf-x" not in both
    unscoped = await jf.items_created_since(since, library_ids=[])
    assert len(unscoped) == 7


async def test_the_delta_read_never_returns_an_episode(client):
    """`ITEM_TYPES` is unchanged and still excludes `Episode` (decision 369), and the fake now
    serves episodes to any read that asks for them — fourteen rows, twelve of them one season of
    one series. So the exclusion is exercised rather than assumed: a delta read that returned
    them would file the twelve jobs §7.2 forbids before the debounce ever saw the burst."""
    module, jf = client
    got = await jf.items_created_since(_watermark("2018-01-01T00:00:00"))
    assert {item["Type"] for item in got} == {"Movie", "Series"}
    assert module.ITEM_TYPES_ASKED == ["movie", "series"], "narrowed in the query, not after it"
    assert len(module.ALL_EPISODES) >= 12, "the double has a season to leak"


async def test_a_short_delta_page_raises_rather_than_advancing_the_watermark_past_it():
    """`all_items` raises on this shape because a short read un-owns the remainder (§7.2's third
    bullet). This walk raises on it because a short read here is SILENT: the watermark advances
    only on a completed read (decision 366), so the next poll asks about the instants after the
    page that was dropped and those adds are never looked for again.

    The bounds are beside `all_items`'s rather than shared with it — that walk is the ownership
    pass's input and `_falsify_ownership` depends on its exact refusals.
    """
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [
            {"Id": "a", "Type": "Movie", "DateCreated": "2024-01-01T00:00:00.0000000Z"},
            {"Id": "b", "Type": "Movie", "DateCreated": "2024-01-02T00:00:00.0000000Z"},
        ], "TotalRecordCount": 9})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(JellyfinError, match="must not advance"):
        await jf.items_created_since(_watermark("2022-01-01T00:00:00"), page=4)


async def test_a_delta_page_that_is_not_an_envelope_raises_rather_than_reading_nothing():
    """Decision 366's "advances ONLY on a completed read", asked of the read that did not happen
    at all rather than of the one that raised.

    `_request` answers None for an empty body and for a body it cannot parse -- a forward-auth
    portal, a proxy's maintenance page, a base URL pointing at the wrong vhost -- and the trailing
    `or {}` this page used to end with turned each of those into an empty envelope. On the FIRST
    page there is no `TotalRecordCount` yet, so `claimed` is 0, the short-page bound is inert, and
    the walk returned `[]` as a COMPLETED read. `poll_delta` then wrote the watermark forward over
    instants nothing had looked at, and every title created before them is invisible to this path
    for the life of the install -- the "add nobody ever looks for again" the walk's own docstring
    refuses. A quiet household answers `{"Items": [], "TotalRecordCount": 0}`, which is what the
    `Items` key tells apart. [review cycle 1: m52-rev-lib-03]
    """
    bodies = {
        "a portal's sign-in page": httpx.Response(200, text="<html>Sign in</html>"),
        "no body at all": httpx.Response(200, content=b""),
        "an object that is not an envelope": httpx.Response(200, json={}),
        "a bare list": httpx.Response(200, json=[{"Id": "jf-1", "Type": "Movie"}]),
    }
    for label, response in bodies.items():
        jf = JellyfinClient(
            "http://jellyfin.test", "k",
            transport=httpx.MockTransport(lambda _request, r=response: r),
        )
        with pytest.raises(JellyfinError, match="no envelope") as exc:
            await jf.items_created_since(_watermark("2022-01-01T00:00:00"))
        assert "no envelope" in str(exc.value), label

    honest = JellyfinClient(
        "http://jellyfin.test", "k",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})
        ),
    )
    assert await honest.items_created_since(_watermark("2022-01-01T00:00:00")) == [], (
        "a household that added nothing is not a failed read"
    )


async def test_a_full_delta_page_of_re_saved_old_items_is_progress_and_not_a_wedged_walk():
    """`MinDateLastSaved` selects a SUPERSET -- an item whose artwork was refreshed or whose
    metadata was corrected is re-saved without being re-created -- and a full page of such rows
    is a page the walk moved through, not the fault the no-progress bound was written for. Read
    as that fault, the walk raises "the server is ignoring StartIndex" at a household whose server
    is behaving perfectly; the watermark then never advances again (decision 366) and §7.2's
    fallback is dead until somebody greps the worker log.

    The rows are KEPT now, every one of them: the server's save is the delta (decision 409), and a
    re-saved title the bundle supplied and the app placed exits at stage 1 below every genuine add
    (decision 411). What still has to hold is the request count -- two full pages, then the short
    one -- because every other delta test hands this walk a page SHORTER than `page`, so the bound
    is not reached at all. [review cycle 1: m52-rev-delta-03; M5.2 review cycle 4: M52-C4-REST-02]
    """
    pages: list[list[dict]] = [
        [{"Id": "old-1", "Type": "Movie", "DateCreated": "2019-01-01T00:00:00.0000000Z"},
         {"Id": "old-2", "Type": "Movie", "DateCreated": "2019-01-02T00:00:00.0000000Z"}],
        [{"Id": "old-3", "Type": "Movie", "DateCreated": "2019-01-03T00:00:00.0000000Z"},
         {"Id": "old-4", "Type": "Movie", "DateCreated": "2019-01-04T00:00:00.0000000Z"}],
        [{"Id": "added", "Type": "Movie", "DateCreated": "2024-01-01T00:00:00.0000000Z"}],
    ]
    asked: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("StartIndex", 0))
        asked.append(start)
        rows = [row for page in pages for row in page][start : start + 2]
        return httpx.Response(200, json={"Items": rows, "TotalRecordCount": 5})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    kept = await jf.items_created_since(_watermark("2022-01-01T00:00:00"), page=2)

    assert [item["Id"] for item in kept] == ["old-1", "old-2", "old-3", "old-4", "added"]
    assert asked == [0, 2, 4], "the walk paged past both full pages instead of refusing them"


async def test_a_full_delta_page_that_repeats_one_already_read_raises_instead_of_truncating():
    """The delta walk carries all three of `all_items`'s bounds (plan C1) and only one of them was
    held by a test that can fail on it.

    This is the twin of `test_a_full_page_that_repeats_one_already_read_raises_instead_of_
    truncating`, against the same server: one that ignores `StartIndex`, or a proxy caching page
    one, while reporting an honest `TotalRecordCount`. The sibling above asserts that a full page
    of legitimately re-saved old items stays SILENT, so deleting this raise satisfies it perfectly
    -- which is how a bound the plan mandates shipped with no test that can fail on it.

    What it costs is not a wrong answer but a poll that reads 200 pages where 2 would do, every
    fifteen minutes, inside the five-minute budget decision 368 sized on an abandoned poll costing
    nothing -- and then a delta path that raises at the cap and never advances its watermark
    (decision 366). The request count is asserted because "stop at the evidence" is half the
    claim. [review cycle 2: m52-rev2-delta-01]
    """
    jf, asked = _paging_client(total=10, honour_start=False, abort_after=50)

    with pytest.raises(JellyfinError, match="ignoring StartIndex"):
        await jf.items_created_since(_watermark("2018-01-01T00:00:00"), page=4)

    assert asked == [0, 4], "one repeated page is the whole evidence; do not keep asking"


async def test_the_delta_walk_raises_at_the_hard_page_cap():
    """And the third of the three bounds, which nothing in the suite reached either.

    Driven against a server that HONOURS `StartIndex`, counts nothing and simply never runs out,
    because that is the only shape the cap is the last guard against: the repeated-page server
    above raises at the second request, so a fake that repeats itself would certify the wrong
    instrument. It raises rather than truncating for the reason this walk's own docstring gives --
    a short read here is SILENT, since the watermark advances only on a completed read and the
    next poll asks about the instants after the page that was dropped.
    [review cycle 2: m52-rev2-delta-01]
    """
    jf, asked = _paging_client(
        total=0, honour_start=True, library=None, abort_after=MAX_PAGES + 5
    )

    with pytest.raises(JellyfinError, match="did not end after"):
        await jf.items_created_since(_watermark("2018-01-01T00:00:00"), page=2)

    assert len(asked) == MAX_PAGES


async def test_a_delta_walk_whose_count_moved_between_pages_raises_rather_than_skipping_a_row():
    """The walk sends no `SortBy`, so 10.10 pages with no ORDER BY at all (`GetOrderByText`
    answers `string.Empty` for an empty order) and 10.11 by the non-unique `SortName`, as
    LIMIT/OFFSET over a live table. A row deleted AHEAD of the offset between two pages -- a
    Radarr upgrade replacing a file, a scan removing one -- moves every later row one place left,
    so the row at the page boundary is never served; and the server's count drops by the same one,
    so the short-page bound was satisfied and the walk returned 999 rows of the 1000 present, with
    the watermark then written past the one it skipped.

    A count that moved is a walk that did not finish, and it raises: the next poll reads again
    from the same watermark. [M5.2 review cycle 4: M52-C4-REST-03]
    """
    library = [{"Id": f"{n:032x}", "Type": "Movie"} for n in range(1000)]
    served: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("StartIndex", 0))
        limit = int(request.url.params.get("Limit", 500))
        served.append(start)
        if len(served) == 2:
            del library[10]
        return httpx.Response(200, json={
            "Items": library[start : start + limit], "TotalRecordCount": len(library)
        })

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(JellyfinError, match="counted 1000 rows and then 999"):
        await jf.items_created_since(_watermark("2018-01-01T00:00:00"))
    assert served == [0, 500]


async def test_the_libraries_are_listed_behind_the_admin_key(client):
    """§6.6's library pick needs something to pick from, and decision 364 makes that pick the
    acquisition boundary. Jellyfin answers `/Library/MediaFolders` inside an envelope while
    `/Users` is a bare list, so a client that reads the two the same way is wrong about one of
    them — and the fake authenticates this read exactly as the real server does."""
    module, jf = client
    libraries = await jf.libraries()
    assert len(libraries) == len(module.LIBRARIES)
    assert {lib["Id"] for lib in libraries} >= {"jf-lib-films", "jf-lib-shows"}

    wrong = JellyfinClient("http://jellyfin.test", "not-the-key", transport=jf.transport)
    with pytest.raises(JellyfinError) as exc:
        await wrong.libraries()
    assert exc.value.status == 401


async def test_an_item_answers_only_under_a_library_that_holds_it(client):
    """decision 364's membership test, asked of the server rather than of the payload: the
    Webhook plugin's template is operator-authored (decision 365), so an event carries whatever
    was left in it and never a library it can be trusted about.

    The same row is what §8 stage 1 mints from — it needs `ProviderIds` (decision 323) and the
    published template names none of the plugin's `Provider_<key>` fields — which is why the
    identity read is the one membership is decided over. [M5.2 review cycle 4: M52-C4-WH-02]

    AND THE DOUBLE NOW ANSWERS `ids` THE WAY THE SERVER DOES, which is what lets this test fail.
    `ItemsController.GetItems` resolves a `ParentId` and then sets `query.Parent = null;` before
    `folder.GetItems(query)`, and `Folder.GetItems` sends a query with `ItemIds` straight to
    `LibraryManager.GetItemsResult`, whose only library scoping is `if (query.Recursive &&
    !query.ParentId.IsEmpty())` -- so `ids=jf-1&ParentId=<Shows>` answers Heat on every release
    from 10.9.11 to 12.1. The read asked that and trusted it; against the double, which scoped by
    `ParentId` anyway, the `jf-lib-shows` assertion below held for three cycles and was false on every
    real server. The household's footage sits at `/media/films-home`, beside the films at
    `/media/films`, so a bare prefix test would file it under Films as well.
    [M5.2 review cycle 4: M52-C4-IDS-01, M52-C4-WH-01; decision 408]
    """
    _module, jf = client
    held = await jf.item_in_libraries("jf-1", ["jf-lib-films"])
    assert held is not None and held["Id"] == "jf-1"
    assert held.get("ProviderIds"), "the membership read is also the identity read"
    assert await jf.item_in_libraries("jf-1", ["jf-lib-shows"]) is None
    assert await jf.item_in_libraries("jf-1", ["jf-lib-shows", "jf-lib-films"]) is not None
    assert await jf.item_in_libraries("jf-1", []) is not None, "an empty pick is the server"
    assert await jf.item_in_libraries("jf-never-existed", []) is None
    assert await jf.item_in_libraries("jf-x", ["jf-lib-films"]) is None, (
        "/media/films-home is not inside /media/films"
    )
    assert await jf.item_in_libraries("jf-x", ["jf-lib-home"]) is not None


async def test_the_membership_read_matches_one_guid_in_every_spelling_the_server_accepts(client):
    """Jellyfin writes every `Id` as 32 lowercase hex digits and parses a GUID in `ids` in any of
    its spellings; the Webhook plugin renders `{{ItemId}}` dashed. So the server FOUND the item
    and this read threw it away, because it compared the row's `Id` with the id it was asked
    about as two strings -- and the sweep filed every real add `skipped`, "the server no longer
    holds this item", with no task. The double now binds `ids` by GUID value, as the server does,
    which is what lets this test fail at all: its fixture ids are not GUIDs, and the two sides of
    a string comparison of `jf-1` always agree. [review cycle 3: M52-C3-EVENTS-01, M52-C3-LIB-01]
    """
    module, jf = client
    guid = "6213b704a0d954293110f4d561b0f614"
    module.ITEMS.append({"Id": guid, "Name": "A Film Jellyfin Spells In Hex", "Type": "Movie",
                         "ProductionYear": 2024, "RunTimeTicks": 1,
                         "ProviderIds": {"Tmdb": "5100001"}})
    module.LIBRARY_MEMBERS["jf-lib-films"] += (guid,)

    for spelling in ("6213b704-a0d9-5429-3110-f4d561b0f614",
                     "6213B704-A0D9-5429-3110-F4D561B0F614",
                     "{6213b704-a0d9-5429-3110-f4d561b0f614}", guid.upper(), guid):
        held = await jf.item_in_libraries(spelling, ["jf-lib-films"])
        assert held is not None and held["Id"] == guid, spelling
        assert await jf.item_in_libraries(spelling, []) is not None, spelling
        assert await jf.item_in_libraries(spelling, ["jf-lib-shows"]) is None, spelling


async def test_the_membership_read_sends_no_parent_id_a_stale_pick_could_break(client):
    """A picked library the server no longer has answers 400 (`GetParentItem` throws "Invalid
    parent id"), and when this read sent `ParentId` once per picked library it raised on the first
    stale one before asking the next -- review cycle 3's fix was to ask every scope first. The read
    sends no `ParentId` at all now, because the server never applied it beside `ids` (decision 408),
    so a stale id in the pick cannot make it fail: it asks one question about the id, reads where
    the libraries are, and places the row's `Path`. A stale id is decision 410's to name, once per
    sweep, off `/Library/MediaFolders`. [review cycle 3: M52-C3-LIB-02; M5.2 review cycle 4:
    M52-C4-IDS-01]
    """
    module, jf = client
    asked: list[httpx.URL] = []

    class Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            asked.append(request.url)
            return await jf.transport.handle_async_request(request)

    recording = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=Recording())
    held = await recording.item_in_libraries("jf-1", ["jf-lib-gone", "jf-lib-films"])
    assert held is not None and held["Id"] == "jf-1"
    assert await recording.item_in_libraries("jf-x", ["jf-lib-gone", "jf-lib-films"]) is None, (
        "the footage is in a library the server lists and nobody picked"
    )
    assert asked and not [url for url in asked if "ParentId" in url.params]
    assert {url.path for url in asked} == {"/Items", "/Library/VirtualFolders"}


async def test_the_membership_read_answers_for_an_episode_id(client):
    """`item_in_libraries` sends no `IncludeItemTypes`, on purpose, and until the sweep learned to
    read the row's own `Type` that clause had no observable behaviour at all: on the honest path
    `sweep_pending` only ever asks about a `resolved_key`, which is a series or a movie.

    It has one now, and it runs the other way from the argument in that docstring. The sweep
    refuses a row this app does not acquire (decision 369), and it can only refuse a row the
    server was willing to describe -- narrowed to Movie,Series this read would answer None for an
    episode id and the sweep would file the household's own add as a library the admin did not
    pick. `ITEM_TYPES` is unchanged in every read this app makes of its own accord.
    [review cycle 1: M52-C1-MEMBER-03]
    """
    _module, jf = client
    episode = await jf.item_in_libraries("jf-7-e3", [])

    assert episode is not None and episode["Id"] == "jf-7-e3"
    assert episode["Type"] == "Episode", "the server's own answer is what decision 369 is read off"
    assert episode.get("SeriesId") == "jf-7"
    assert await jf.item_in_libraries("jf-7-e3", ["jf-lib-shows"]) is not None, (
        "an episode is inside the library that holds its series"
    )
    assert await jf.item_in_libraries("jf-7-e3", ["jf-lib-films"]) is None


async def test_a_membership_read_that_fails_raises_instead_of_answering_no():
    """"This id is in no library the admin picked" and "nobody could ask" are the same silence
    and opposite facts. Decision 364 leaves the intake row pending for the next sweep on the
    second, so answering None here would record the household's own add as one they deselected
    — the same polarity decision 362 refuses in the ownership direction."""
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "library scan in progress"})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(JellyfinError) as exc:
        await jf.item_in_libraries("jf-1", ["jf-lib-films"])
    assert exc.value.status == 503

    # And "fails" is not only what `_request` raises on. It answers None for an empty body and for
    # a body it cannot parse -- a forward-auth portal, a proxy's maintenance page -- and the
    # trailing `or {}` this read used to end with turned those into the authoritative negative
    # this docstring refuses, TERMINALLY: the sweep writes `skipped` and never looks at the row
    # again, while the report says `deferred: 0` and names no outage at all.
    # [review cycle 1: m52-rev-lib-02]
    for label, response in {
        "a portal's sign-in page": httpx.Response(200, text="<html>Sign in</html>"),
        "no body at all": httpx.Response(200, content=b""),
        "a bare list": httpx.Response(200, json=[]),
    }.items():
        portal = JellyfinClient(
            "http://jellyfin.test", "k",
            transport=httpx.MockTransport(lambda _request, r=response: r),
        )
        with pytest.raises(JellyfinError, match="no envelope") as raised:
            await portal.item_in_libraries("jf-1", ["jf-lib-films"])
        assert "no envelope" in str(raised.value), label


async def test_a_row_no_listed_library_can_place_is_a_failed_read_and_never_an_absence(client):
    """Decision 364's polarity, held against the one new way decision 408's read can come up
    empty-handed. The row's `Path` is placed against `/Library/VirtualFolders`, and on an honest
    server every file a library scanned sits under that library's locations -- so a row under none
    of them, or carrying no `Path`, is not "outside the pick" but a read that could not decide.
    It happens for real when the server's `PathSubstitutions` rewrite the `Path` a row is served
    with (`DtoService.GetMappedPath`) while `Locations` stay as configured. Answered None, every
    such add would be filed terminally as a library nobody picked; raised, it waits, named.

    And an answer to `/Library/VirtualFolders` that is not a list of libraries each carrying a list
    of paths is a failed read too, for `_item_rows`' reason: a map with half its libraries missing
    would decide membership against the half that is there. [decision 408]
    """
    module, jf = client
    module.ITEMS.append({"Id": "jf-moved", "Name": "Somewhere Else", "Type": "Movie",
                         "ProductionYear": 2024, "RunTimeTicks": 1,
                         "Path": "/mnt/substituted/Somewhere Else (2024)/Somewhere Else.mkv"})
    module.ITEMS.append({"Id": "jf-pathless", "Name": "No Path", "Type": "Movie",
                         "ProductionYear": 2024, "RunTimeTicks": 1})

    with pytest.raises(JellyfinError, match="inside none of the libraries"):
        await jf.item_in_libraries("jf-moved", ["jf-lib-films"])
    with pytest.raises(JellyfinError, match="no Path"):
        await jf.item_in_libraries("jf-pathless", ["jf-lib-films"])
    assert await jf.item_in_libraries("jf-moved", []) is not None, "an empty pick asks no path"

    for answer in ({"Items": []}, [{"ItemId": "jf-lib-films"}],
                   [{"ItemId": "jf-lib-films", "Locations": "/media/films"}], ["jf-lib-films"]):
        broken = JellyfinClient(
            "http://jellyfin.test", module.API_KEY,
            transport=httpx.MockTransport(
                lambda request, a=answer: httpx.Response(200, json=a)
                if request.url.path == "/Library/VirtualFolders"
                else httpx.Response(200, json={"Items": [
                    {"Id": "jf-1", "Type": "Movie", "Path": "/media/films/Heat/Heat.mkv"}
                ]})
            ),
        )
        with pytest.raises(JellyfinError, match="not libraries"):
            await broken.item_in_libraries("jf-1", ["jf-lib-films"])


async def test_a_redirect_is_a_failed_read_that_names_where_it_pointed():
    """httpx follows no redirect by default, so a forward-auth portal's 302 or a proxy's
    http->https 301 reached the JSON parse and became None: the intake paths recorded "answered no
    envelope" with no status, and `check()` read `users()` as an empty list and answered success --
    the admin's test button said the connection worked while neither intake path filed anything.
    Jellyfin itself answers 302 to a path that leaves out its configured BaseUrl
    (`BaseUrlRedirectionMiddleware`: `httpContext.Response.Redirect(target)`). A redirect is a
    failed read carrying its status and its `Location`, which is the lever an operator needs.
    [M5.2 review cycle 4: M52-C4-REDIRECT-01]
    """
    def portal(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://auth.example/login?rd=x"}, text="<html>sign in</html>"
        )

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(portal))
    for label, read in (
        ("the test button", jf.check),
        ("the membership read", lambda: jf.item_in_libraries("jf-1", [])),
        ("the delta read", lambda: jf.items_created_since(_watermark("2022-01-01T00:00:00"))),
    ):
        with pytest.raises(JellyfinError) as exc:
            await read()
        assert exc.value.status == 302, label
        assert "https://auth.example/login" in str(exc.value), label


async def test_a_url_no_request_can_be_built_from_is_a_jellyfin_error_and_not_an_escape():
    """`httpx.InvalidURL` is not an `httpx.HTTPError` -- it subclasses `Exception` directly -- so a
    saved URL with a letter where its port should be (`http://jellyfin:8O96`) raised straight out
    of `_request` past every caller's `except JellyfinError`: the admin PUT's version probe answered
    500 after the save it followed had committed. [M5.2 review cycle 4: M52-C4-TOKEN-01]
    """
    jf = JellyfinClient("http://jellyfin:8O96", "k")
    with pytest.raises(JellyfinError, match="Invalid port"):
        await jf.server_info()


async def test_an_envelope_whose_items_are_not_objects_is_a_failed_read_at_every_read_that_pages():
    """Review cycle 1 made a body that is not an ENVELOPE a failed read at these three reads. The
    guard stopped one level short of the fault it was written for: `{"Items": "maintenance"}` and
    `{"Items": ["jf-1"]}` pass an `isinstance(payload, dict)` test, and the very next line calls
    `.get` on a `str`.

    What that cost is each read's own worst answer. `libraries` did not raise at all -- it handed
    the admin card the CHARACTERS of the string, which `api/admin.jellyfin_libraries` then read
    `.get` on, outside its `except JellyfinError`, for the 500 that route's docstring exists to
    refuse. The other two raised `AttributeError`, which is not a `JellyfinError`: it escapes
    `sweep_pending`'s handler entirely, so decision 364's "a membership read that FAILS leaves the
    row pending" became a sweep that raised out of the job with the ripe set swept oldest-first --
    the permanent wedge, one exception type away from the one review cycle 1 closed.

    A row that is not an object is refused rather than dropped, because dropping it makes a page
    of strings a SHORT page, and a short page is how this walk knows the library ended.
    [review cycle 2: m52-c2-lib-02]
    """
    bodies = {
        "Items is a string": {"Items": "maintenance", "TotalRecordCount": 11},
        "Items is a list of strings": {"Items": ["jf-1", "jf-2"], "TotalRecordCount": 2},
        "one row among the items is not an object": {
            "Items": ["jf-2", {"Id": "jf-1", "Type": "Movie"}], "TotalRecordCount": 2
        },
    }
    for label, answer in bodies.items():
        jf = JellyfinClient(
            "http://jellyfin.test", "k",
            transport=httpx.MockTransport(
                lambda _request, a=answer: httpx.Response(200, json=a)
            ),
        )
        with pytest.raises(JellyfinError, match="not items") as folders:
            await jf.libraries()
        assert "MediaFolders" in str(folders.value), label
        with pytest.raises(JellyfinError, match="not items") as member:
            await jf.item_in_libraries("jf-1", ["jf-lib-films"])
        assert "jf-1" in str(member.value), label
        with pytest.raises(JellyfinError, match="not items"):
            await jf.items_created_since(_watermark("2022-01-01T00:00:00"))


async def test_an_envelope_with_no_list_of_items_is_a_failed_read_and_never_an_empty_answer():
    """The same guard, one level further up. `list(payload.get("Items") or [])` turned a 200 of
    `{}`, `{"error": ...}` or `{"Items": null}` into an authoritative EMPTY answer: the membership
    read then said the item was nowhere, and the sweep filed it `skipped` -- terminally, while a
    gateway in maintenance mode was the only thing that had spoken. `libraries` reported the same
    bodies as `ok: true` with no libraries, the "deliberate choice nobody made" its own docstring
    refuses. `{"Items": null}` also passed the delta page's `"Items" in payload` check and ended the
    walk as a completed empty read, so the watermark moved past it. And a truthy `Items` that
    cannot be iterated -- `5`, `true` -- raised `TypeError`, which is not a `JellyfinError` and
    escaped the sweep, the poll and the admin route's handler alike: the class review cycle 2 said
    it had closed. A row whose `Id` is not a string is not an answer about any id either.
    [review cycle 3: M52-C3-LIB-06]
    """
    bodies = {
        "an empty object": {},
        "an error object": {"error": "maintenance"},
        "Items null": {"Items": None, "TotalRecordCount": 0},
        "Items zero": {"Items": 0},
        "Items a number": {"Items": 5},
        "Items true": {"Items": True},
    }
    for answer in bodies.values():
        jf = JellyfinClient(
            "http://jellyfin.test", "k",
            transport=httpx.MockTransport(
                lambda _request, a=answer: httpx.Response(200, json=a)
            ),
        )
        with pytest.raises(JellyfinError):
            await jf.libraries()
        with pytest.raises(JellyfinError):
            await jf.item_in_libraries("jf-1", ["jf-lib-films"])
        with pytest.raises(JellyfinError):
            await jf.items_created_since(_watermark("2022-01-01T00:00:00"))

    for row in ({"Type": "Movie"}, {"Id": {"jf": 1}, "Type": "Movie"}):
        jf = JellyfinClient(
            "http://jellyfin.test", "k",
            transport=httpx.MockTransport(
                lambda _request, r=row: httpx.Response(200, json={"Items": [r]})
            ),
        )
        with pytest.raises(JellyfinError):
            await jf.item_in_libraries("jf-1", [])


async def test_an_answer_nested_past_the_decoders_depth_is_a_failed_read_and_never_an_escape():
    """The same guard one layer further down, under the envelope rather than inside it. `_request`
    reads a body it cannot parse -- a portal's sign-in page, a proxy's maintenance page -- as no
    body, and it caught only `ValueError` to do so. The decoder's own depth limit is not one:
    `json.loads` over an answer nested a hundred thousand deep raises `RecursionError`, a
    `RuntimeError`, which is not a `JellyfinError` either. It escaped `sweep_pending`'s handler at
    the oldest ripe key, which is the first key every later sweep reads again, and the delta poll's
    and the admin route's alike -- the permanent wedge review cycle 2 closed for `AttributeError`
    and cycle 3 for `TypeError`, reached through the one exception type the parse itself can raise.
    An answer no parser can read is an answer about nothing, so each read raises the failed read
    its callers already handle rather than an error none of them catches.
    [M5.2 review cycle 3, green pass]
    """
    deep = b'{"Items": ' + b"[" * 100_000 + b"]" * 100_000 + b', "TotalRecordCount": 1}'
    jf = JellyfinClient(
        "http://jellyfin.test", "k",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=deep)),
    )
    with pytest.raises(JellyfinError):
        await jf.libraries()
    with pytest.raises(JellyfinError):
        await jf.item_in_libraries("jf-1", ["jf-lib-films"])
    with pytest.raises(JellyfinError):
        await jf.item_in_libraries("jf-1", [])
    with pytest.raises(JellyfinError):
        await jf.items_created_since(_watermark("2022-01-01T00:00:00"))
