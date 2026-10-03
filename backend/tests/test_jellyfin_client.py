"""The Jellyfin client against `ops/fake_jellyfin.py` over ASGI (§7.1, §7.3, §14.3). No database. The double
refuses the admin key on the Played write on purpose: the only way that restraint can fail a test."""

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
    last_played_of,
)


@pytest.fixture
def client(fake_jellyfin):
    module, transport = fake_jellyfin
    return module, JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)


async def test_the_api_key_travels_as_x_emby_token(client):
    module, jf = client
    assert len(await jf.users()) == 2

    wrong = JellyfinClient("http://jellyfin.test", "not-the-key", transport=jf.transport)
    with pytest.raises(JellyfinError) as exc:
        await wrong.users()
    assert exc.value.status == 401


async def test_the_key_travels_where_a_jellyfin_12_server_reads_it(client, monkeypatch):
    """Jellyfin 12 stops reading `X-Emby-Token` by default;
    `Token=` in the `MediaBrowser` header is always read."""
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
    """A 10.8 server answers /System/Info and 404s the routes that matter, so the version is checked."""
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
    """`honour_start=False` ignores `StartIndex`; `library=None`
    never runs out; `abort_after` turns a hang into a failure."""
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
    """Stops at the server's own count, compared against DISTINCT ids."""
    jf, asked = _paging_client(total=4, honour_start=False, abort_after=50)
    assert len(await jf.all_items("jf-user-patrick", page=4)) == 4
    assert asked == [0], "the server said how many there were; one page was the whole library"

    jf, asked = _paging_client(total=8, honour_start=True, library=8, abort_after=50)
    assert len(await jf.all_items("jf-user-patrick", page=4)) == 8
    assert asked == [0, 4], "the count ends a multi-page walk without a third round trip"


async def test_a_full_page_that_repeats_one_already_read_raises_instead_of_truncating():
    """A page served repeatedly must raise, not truncate: ownership is re-derived from this list."""
    jf, asked = _paging_client(total=10, honour_start=False, abort_after=50)
    with pytest.raises(JellyfinError, match="ignoring StartIndex"):
        await jf.all_items("jf-user-patrick", page=4)
    assert asked == [0, 4], "one repeated page is the whole evidence; do not keep asking"


async def test_the_walk_raises_at_the_hard_page_cap():
    """Driven against a server that honours `StartIndex`
    and never runs out: the only shape the cap guards."""
    jf, asked = _paging_client(
        total=0, honour_start=True, library=None, abort_after=MAX_PAGES + 5
    )
    with pytest.raises(JellyfinError, match="did not end after"):
        await jf.all_items("jf-user-patrick", page=2)
    assert len(asked) == MAX_PAGES


async def test_a_short_page_still_ends_the_walk_when_the_server_does_not_count():
    """A server answering `TotalRecordCount: 0` is paged until a page comes back short."""
    jf, asked = _paging_client(total=0, honour_start=True, library=5, abort_after=20)
    assert len(await jf.all_items("jf-user-patrick", page=3)) == 5
    assert asked == [0, 3]


def _scripted_client(pages: list[dict | None]):
    """`None` is a 200 whose body is not JSON, which `_request` collapses to `{}`."""
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
    """A short page the server counts as short raises; a library that genuinely shrank exits cleanly."""
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
    """A row with no `Id` counts as progress, or every sweep would read one short of the count."""
    jf, served = _scripted_client([
        {"Items": [*_rows(0, 3), {"Type": "Movie"}], "TotalRecordCount": 4},
    ])
    assert len(await jf.all_items(None, page=8)) == 4
    assert served == [0]


async def test_the_library_can_be_read_once_with_no_user_id(client):
    """Ownership is re-derived from the admin key's view in
    one keyless read; an unknown id given still 404s."""
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


async def test_sessions_reduce_to_what_is_playing(client):
    """A series session carries the episode's `Id` and the folder only as `SeriesId` (decision 210)."""
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
    """Keyed per series AND member: episode visibility is per-user, so two members are two reads."""
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
    """A refusal is not cached as empty: every episode would become "the last one"."""
    _module, jf = client
    for _attempt in (1, 2):
        with pytest.raises(JellyfinError) as exc:
            await jf.episodes("jf-nope")
        assert exc.value.status == 404


def test_last_played_reads_jellyfins_seven_digit_utc_date():
    item = {"UserData": {"LastPlayedDate": "2026-09-29T21:14:03.1234567Z"}}
    assert last_played_of(item) == datetime(2026, 9, 29, 21, 14, 3, 123456, tzinfo=UTC)
    assert last_played_of({"UserData": {"Played": True}}) is None


def test_a_session_with_no_runtime_is_not_ninety_percent_finished():
    """A live stream has no duration; 0 keeps it out of the prompt population."""
    assert NowPlaying("s", "u", "i", position_ticks=500, runtime_ticks=0).fraction == 0.0


async def test_a_played_write_uses_the_users_own_token(client):
    module, jf = client
    _jf_user, token = await jf.authenticate_by_name("patrick", module.PASSWORD)

    await jf.set_played("jf-1", "jf-user-patrick", True, token)
    assert "jf-1" in module.state.played["jf-user-patrick"]

    await jf.set_played("jf-1", "jf-user-patrick", False, token)
    assert "jf-1" not in module.state.played["jf-user-patrick"]
    assert [w["played"] for w in module.state.write_log] == [True, False]


async def test_a_server_below_the_pin_refuses_the_played_write_by_name(client):
    """Below 10.9 `/UserPlayedItems` does not exist; the reason
    names the route and versions, since only an upgrade helps."""
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

    # An unprobed server must not block the write.
    assert jf.played_write_refusal() is None
    await jf.set_played("jf-1", "jf-user-patrick", True, token)
    assert "jf-1" in module.state.played["jf-user-patrick"]


async def test_the_probe_reports_the_version_and_the_verdict(client, monkeypatch):
    """Operators upgrade the media server without revisiting this app's admin page."""
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
    """"The server did not say" must not read as "10.8": a
    forward-auth proxy answers 200 with a login page."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, content=body)
        # `check` also enumerates users; the fault under test is on the version route alone.
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
    """A real server would accept the admin key; the fake refuses it so a regression fails here."""
    module, jf = client
    with pytest.raises(JellyfinError) as exc:
        await jf.set_played("jf-1", "jf-user-patrick", True, module.API_KEY)
    assert exc.value.status == 403
    assert exc.value.is_auth_failure


async def test_a_write_with_no_token_never_reaches_the_network(client):
    """The refusal is local: a missing token must not fall back to the admin key."""
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


async def test_an_unreachable_server_raises_jellyfin_error_not_httpx(client):
    """§3.3: callers catch `JellyfinError`, so no httpx exception may escape."""
    _module, jf = client
    unreachable = JellyfinClient("http://127.0.0.1:1", "key", timeout=0.2)
    with pytest.raises(JellyfinError) as exc:
        await unreachable.users()
    assert exc.value.status is None
    assert not exc.value.is_auth_failure


async def test_items_are_requested_recursively(client):
    """Without `Recursive=true` a real Jellyfin returns library folders; the fake answers empty."""
    _module, jf = client
    assert await jf.all_items("jf-user-patrick"), "the client must ask recursively"


async def test_items_are_narrowed_to_movies_and_series(client):
    """§4.1 rule 5: episodes and music videos have nowhere to go."""
    module, jf = client
    kinds = {item["Type"] for item in await jf.all_items("jf-user-patrick")}
    assert kinds <= {"Movie", "Series"}
    assert "Movie" in kinds and "Series" in kinds
    assert module.ITEM_TYPES_ASKED, "the fake recorded no IncludeItemTypes filter"


async def test_the_field_set_is_actually_requested(client):
    """The fake projects items to the small default set unless `Fields` asks for more."""
    _module, jf = client
    items = await jf.all_items("jf-user-patrick")
    first = items[0]
    assert "ProviderIds" in first, "ProviderIds is the identity payload (§7.1)"
    assert "UserData" in first, "UserData carries the Played flag the seen sync reads (§7.3)"


def _watermark(text: str) -> datetime:
    """Spelled the way `connector_config` stores one (decision 366)."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def test_the_delta_read_returns_what_the_server_saved_after_the_watermark(client):
    """Items arrived across five years, so the watermark is a filter and not a tautology."""
    _module, jf = client
    recent = await jf.items_created_since(_watermark("2022-01-01T00:00:00"))
    assert {item["Name"] for item in recent} == {
        "Severance", "The Bear", "Tampopo", "Christmas 2019"
    }
    whole = await jf.items_created_since(_watermark("2018-01-01T00:00:00"))
    assert len(whole) == 7
    assert await jf.items_created_since(_watermark("2030-01-01T00:00:00")) == []


async def test_the_delta_read_keeps_what_the_server_saved_whatever_the_files_own_date(client):
    """`DateCreated` comes from the FILE by default; `DateLastSaved`
    marks the arrival, and the server's filter is the evidence."""
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
    """An EMPTY pick is the whole server; a non-empty one is a `ParentId` filter the server applies."""
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
    """The fake serves episodes, so the exclusion is exercised rather than assumed."""
    module, jf = client
    got = await jf.items_created_since(_watermark("2018-01-01T00:00:00"))
    assert {item["Type"] for item in got} == {"Movie", "Series"}
    assert module.ITEM_TYPES_ASKED == ["movie", "series"], "narrowed in the query, not after it"
    assert len(module.ALL_EPISODES) >= 12, "the double has a season to leak"


async def test_a_short_delta_page_raises_rather_than_advancing_the_watermark_past_it():
    """A short delta page is SILENT damage: the watermark would move past the dropped rows."""
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [
            {"Id": "a", "Type": "Movie", "DateCreated": "2024-01-01T00:00:00.0000000Z"},
            {"Id": "b", "Type": "Movie", "DateCreated": "2024-01-02T00:00:00.0000000Z"},
        ], "TotalRecordCount": 9})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(JellyfinError, match="must not advance"):
        await jf.items_created_since(_watermark("2022-01-01T00:00:00"), page=4)


async def test_a_delta_page_that_is_not_an_envelope_raises_rather_than_reading_nothing():
    """An unparseable first page must not read as a completed empty read."""
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
    """`MinDateLastSaved` returns re-saved old items too;
    a full page of them is progress, and they are kept."""
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
    """Deleting this raise would still pass the sibling
    above; a caching proxy would cost 200 pages a poll."""
    jf, asked = _paging_client(total=10, honour_start=False, abort_after=50)

    with pytest.raises(JellyfinError, match="ignoring StartIndex"):
        await jf.items_created_since(_watermark("2018-01-01T00:00:00"), page=4)

    assert asked == [0, 4], "one repeated page is the whole evidence; do not keep asking"


async def test_the_delta_walk_raises_at_the_hard_page_cap():
    """Against a server that honours `StartIndex` and never runs out."""
    jf, asked = _paging_client(
        total=0, honour_start=True, library=None, abort_after=MAX_PAGES + 5
    )

    with pytest.raises(JellyfinError, match="did not end after"):
        await jf.items_created_since(_watermark("2018-01-01T00:00:00"), page=2)

    assert len(asked) == MAX_PAGES


async def test_a_delta_walk_whose_count_moved_between_pages_raises_rather_than_skipping_a_row():
    """No `SortBy`, so a row deleted mid-walk shifts the boundary;
    a count that moved means the walk did not finish."""
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
    """`/Library/MediaFolders` is an envelope while `/Users` is a bare list."""
    module, jf = client
    libraries = await jf.libraries()
    assert len(libraries) == len(module.LIBRARIES)
    assert {lib["Id"] for lib in libraries} >= {"jf-lib-films", "jf-lib-shows"}

    wrong = JellyfinClient("http://jellyfin.test", "not-the-key", transport=jf.transport)
    with pytest.raises(JellyfinError) as exc:
        await wrong.libraries()
    assert exc.value.status == 401


async def test_an_item_answers_only_under_a_library_that_holds_it(client):
    """The server ignores `ParentId` beside `ids` (decision
    408), so membership is decided from the row's `Path`."""
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
    """The server binds `ids` by GUID value, so `Id` comparison must too."""
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
    """No `ParentId` at all, so a stale library in the pick cannot make the read fail."""
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
    """No `IncludeItemTypes`, so the sweep can see an episode id and refuse it by `Type`."""
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
    """"In no picked library" and "nobody could ask" are opposite facts; a failure must raise."""
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "library scan in progress"})

    jf = JellyfinClient("http://jellyfin.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(JellyfinError) as exc:
        await jf.item_in_libraries("jf-1", ["jf-lib-films"])
    assert exc.value.status == 503

    # An unparseable body is a failed read, never the
    # authoritative negative the sweep would file terminally.
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
    """A row under no library location, or with no `Path`, is an undecided read, not "outside the pick"."""
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
    """httpx follows no redirect by default; a redirect is
    a failed read carrying its status and `Location`."""
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
    """`httpx.InvalidURL` subclasses `Exception` directly, not `httpx.HTTPError`."""
    jf = JellyfinClient("http://jellyfin:8O96", "k")
    with pytest.raises(JellyfinError, match="Invalid port"):
        await jf.server_info()


async def test_an_envelope_whose_items_are_not_objects_is_a_failed_read_at_every_read_that_pages():
    """Rows that are not objects are refused, not dropped: dropping them would make a SHORT page."""
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
    """`{}`, `{"Items": null}` or a non-iterable `Items` is a failed read, never an empty answer."""
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
    """`json.loads` raises `RecursionError` past its depth limit, which is not a `ValueError`."""
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
