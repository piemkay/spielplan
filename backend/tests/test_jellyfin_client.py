"""The Jellyfin client against a real (fake) server. Spec v2.1 §7.1, §7.3, §14.3.

`ops/fake_jellyfin.py` answers over ASGI, so every assertion here is about an HTTP exchange:
the header the app sends, the route it picks, the parameters it builds. No database needed.

The load-bearing one is `test_the_admin_key_is_refused_on_a_played_write`. §14.3 says a
Jellyfin API key is unscoped and admin-equivalent with no read-only variant, so §7.3's
per-user-token rule is enforced by this app's code and nothing else. The fake refuses the
admin key on that route on purpose: it is the only way this restraint can fail a test.
"""

from __future__ import annotations

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
