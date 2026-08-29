"""The Jellyfin client against a real (fake) server. Spec v2.1 §7.1, §7.3, §14.3.

`ops/fake_jellyfin.py` answers over ASGI, so every assertion here is about an HTTP exchange:
the header the app sends, the route it picks, the parameters it builds. No database needed.

The load-bearing one is `test_the_admin_key_is_refused_on_a_played_write`. §14.3 says a
Jellyfin API key is unscoped and admin-equivalent with no read-only variant, so §7.3's
per-user-token rule is enforced by this app's code and nothing else. The fake refuses the
admin key on that route on purpose: it is the only way this restraint can fail a test.
"""

from __future__ import annotations

import pytest

from spielplan.connectors.jellyfin import (
    FIELDS,
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


# --- §7.3: /Sessions -------------------------------------------------------------------


async def test_sessions_reduce_to_what_is_playing(client):
    module, jf = client
    await module.force_session(
        module.SessionControl(user_id="jf-user-patrick", item_id="jf-1", fraction=0.5)
    )
    sessions = await jf.sessions()
    assert len(sessions) == 1
    assert sessions[0].jf_user_id == "jf-user-patrick"
    assert sessions[0].item_id == "jf-1"
    assert 0.49 < sessions[0].fraction < 0.51


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
