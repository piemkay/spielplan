"""A fake Jellyfin server. Test infrastructure — never shipped in the app image.

§7.3 promises that seen state flows *both ways* and that the Played write uses the linked
user's own token rather than the admin key. Neither claim can be checked against a mock inside
the process that makes it: a mock asserts that we called what we think we call. This is a real
HTTP server that behaves the way Jellyfin >= 10.9 behaves on the handful of routes §7.1 names,
including the part that matters most — **it refuses the admin API key on `/UserPlayedItems`**.
That refusal is what turns "we use per-user tokens" from a comment into a test.

Two ways in:

  * Playwright drives it over HTTP as a compose service (`ops/compose.e2e.yml`), so the browser
    test can flip a flag in "Jellyfin" and watch it arrive in the app.
  * The backend integration tests mount it through `httpx.ASGITransport`, so the same server
    answers with no socket and no port.

The `/_test/*` routes are the control surface and have no Jellyfin counterpart. One of them fires
OUTWARD: §7.2's primary intake is a **push** from the Webhook plugin, so the double has to be able
to originate an `ItemAdded` and not only answer a read. It delivers over the same two ways in --
an ordinary POST on the compose network, and the app's own `ASGITransport` when a test has mounted
one into `WEBHOOK_TRANSPORT` -- and the payload's shape lives here for the reason the rest of this
file does. A test that authors its own `ItemAdded` has turned the double back into a mock, and can
then prove the handler tolerates a field the plugin never sends.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

TICKS_PER_MINUTE = 60 * 10_000_000

API_KEY = os.environ.get("FAKE_JELLYFIN_API_KEY", "fake-admin-key")
SERVER_VERSION = os.environ.get("FAKE_JELLYFIN_VERSION", "10.10.3")

# Jellyfin spells an instant with seven fractional digits and a trailing `Z`, which
# `datetime.fromisoformat` accepts in neither half, so every client of §7.2's delta read has to
# normalise before it can compare. The fake normalises the same way rather than comparing the
# strings, because a lexicographic compare agrees with a real one only while both sides spell the
# precision identically -- and the app's watermark, written by Postgres, will not.
_ISO = re.compile(
    r"^(?P<secs>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
)


def _instant(text: str) -> datetime:
    """Parse one of those, and 400 on anything else -- a real server does not guess at a filter."""
    match = _ISO.match(text.strip())
    if match is None:
        raise HTTPException(400, f"unparseable date: {text!r}")
    zone = match.group("zone") or "Z"
    return datetime.fromisoformat(
        f"{match.group('secs')}.{(match.group('frac') or '')[:6]:0<6}"
        f"{'+00:00' if zone == 'Z' else zone}"
    )


def _stamp(when: datetime) -> str:
    """And spell one back out the way the server does, so a client cannot depend on our format."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _minutes_after(stamp: str, minutes: int) -> str:
    return _stamp(_instant(stamp) + timedelta(minutes=minutes))


# Mirrors backend/tests/fixtures/make_bundle.py so every item resolves to a fixture title —
# except the last two, which are deliberately awkward:
#   * "Tampopo" carries NO ProviderIds, so it exercises the name/year fallback;
#   * "Christmas 2019" resolves to nothing at all and must not create a title (§4.2: title.id
#     is carried over verbatim from the corpus, so this connector never mints one).
#
# `DateCreated` is NOT when the item entered this library, and this file used to say it was.
# Jellyfin stamps it from the FILE: `UseFileCreationTimeForDateAdded` defaults to true
# (MediaBrowser.Model/Configuration/MetadataConfiguration.cs) and `ResolverHelper.SetDateCreated`
# then writes `info.CreationTimeUtc`, which .NET on Linux reads as the older of mtime and ctime --
# and an NFO's `<dateadded>` overrides it. What does mark the item's arrival is `DateLastSaved`,
# set to `DateTime.UtcNow` when the scan first saves it (`LibraryManager.RunMetadataSavers`), and
# that is the column `MinDateLastSaved` filters on. The double spelled the two as one instant, so a
# client re-filtering the server's answer on `DateCreated > since` agreed with it and with nothing
# a real server does: a film copied in with its mtime kept came back from the server and was thrown
# away. So each item carries both, the scan's save minutes after the file for most of them, and two
# whose file is years older than their arrival -- Tampopo, copied from an old drive with its times
# preserved, and the household's own Christmas footage, stamped by the camera that shot it.
# [M5.2 review cycle 4: M52-C4-REST-02, M52-C4-TTA-02; decision 409]
#
# `DateLastSaved` is never SERVED: `BaseItemDto` has no such property on any release from 10.9 to
# 12.1 (`ItemFields.DateLastSaved` exists and fills nothing), so `_NEVER_SERVED` strips it from
# every projection whatever `Fields` asks for. `DateCreated` is an `ItemFields` entry, served only
# when `Fields` asks -- see DEFAULT_FIELDS. The seven arrivals are still spread across five years,
# because a delta test over a corpus saved all at once passes against a fake that ignores the
# filter.
ITEMS: list[dict[str, Any]] = [
    {"Id": "jf-1", "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
     "RunTimeTicks": 170 * TICKS_PER_MINUTE, "DateCreated": "2019-03-14T21:05:00.0000000Z",
     "DateLastSaved": "2019-03-14T21:12:00.0000000Z",
     "ProviderIds": {"Imdb": "tt0113277", "Tmdb": "949"}},
    {"Id": "jf-2", "Name": "Prisoners", "Type": "Movie", "ProductionYear": 2013,
     "RunTimeTicks": 153 * TICKS_PER_MINUTE, "DateCreated": "2020-07-02T18:40:00.0000000Z",
     "DateLastSaved": "2020-07-02T18:52:00.0000000Z",
     "ProviderIds": {"Imdb": "tt1392214", "Tmdb": "146233"}},
    {"Id": "jf-3", "Name": "Paddington 2", "Type": "Movie", "ProductionYear": 2017,
     "RunTimeTicks": 103 * TICKS_PER_MINUTE, "DateCreated": "2021-11-20T09:15:00.0000000Z",
     "DateLastSaved": "2021-11-20T09:31:00.0000000Z",
     "ProviderIds": {"Tmdb": "346648"}},
    {"Id": "jf-6", "Name": "Severance", "Type": "Series", "ProductionYear": 2022,
     "RunTimeTicks": 48 * TICKS_PER_MINUTE, "DateCreated": "2022-04-01T07:30:00.0000000Z",
     "DateLastSaved": "2022-04-01T07:44:00.0000000Z",
     "ProviderIds": {"Imdb": "tt11280740", "Tmdb": "95396"}},
    {"Id": "jf-7", "Name": "The Bear", "Type": "Series", "ProductionYear": 2022,
     "RunTimeTicks": 30 * TICKS_PER_MINUTE, "DateCreated": "2023-06-23T22:10:00.0000000Z",
     "DateLastSaved": "2023-06-23T22:25:00.0000000Z",
     "ProviderIds": {"Imdb": "tt14452776", "Tmdb": "136315"}},
    {"Id": "jf-8", "Name": "Tampopo", "Type": "Movie", "ProductionYear": 1985,
     "RunTimeTicks": 114 * TICKS_PER_MINUTE, "DateCreated": "2016-05-02T19:20:00.0000000Z",
     "DateLastSaved": "2024-02-09T20:00:00.0000000Z",
     "ProviderIds": {}},
    {"Id": "jf-x", "Name": "Christmas 2019", "Type": "Movie", "ProductionYear": 2019,
     "RunTimeTicks": 41 * TICKS_PER_MINUTE, "DateCreated": "2019-12-25T16:03:00.0000000Z",
     "DateLastSaved": "2024-12-26T11:45:00.0000000Z",
     "ProviderIds": {}},
]

# Jellyfin never plays a Series: a session on one plays an **Episode**, with its own `Id`, a
# `SeriesId` for the folder and the season/episode numbers its clients render. The fake used to
# hand back the Series id itself and emit no `Type` at all, which let the app resolve a session it
# could never resolve against a real server -- the exact shape `dd05-fake-and-e2e` records, and
# why §7.3's prompt never armed for the Series partition in the field. Two episodes per series, so
# that "the last known episode" (decision 210(c)) is a choice rather than a tautology, and the
# series' own runtime, because `/_test/session` sets a fraction of it and the e2e's 0.96 has to
# stay past §7.3's 0.9.
#
# Twelve of them for "The Bear", because §7.2's debounce clause is arithmetic over a burst --
# "library scans add seasons in bursts; series acquire per-show, not per-episode" -- and a season
# a test cannot fill has nothing to collapse. The twelve go on jf-7 and NOT on jf-6 because
# `test_jellyfin_client.py:322,345-346` pin Severance's index list at `[1, 2]` and its last episode
# at `jf-6-e2`: decision 210(c)'s "last known episode" is asserted against those literals, and a
# double that moved them would be editing the assertion it exists to feed. `_now_playing` still
# takes `episodes[-1]`, so The Bear's last known episode is simply e12 now.
EPISODE_COUNT: dict[str, int] = {"jf-6": 2, "jf-7": 12}

EPISODES: dict[str, list[dict[str, Any]]] = {
    str(series["Id"]): [
        {
            "Id": f"{series['Id']}-e{n}",
            "Name": f"Episode {n}",
            "Type": "Episode",
            "SeriesId": str(series["Id"]),
            "SeriesName": series["Name"],
            "ParentIndexNumber": 1,
            "IndexNumber": n,
            "RunTimeTicks": int(series["RunTimeTicks"]),
            # A scan stamps a season minutes apart, not years: the burst these twelve stand for is
            # one import, and a delta read that saw them scattered would be reading a library no
            # scan produces.
            "DateCreated": _minutes_after(str(series["DateCreated"]), n),
            "DateLastSaved": _minutes_after(str(series["DateLastSaved"]), n),
            # The episode's OWN ids. Jellyfin's TMDb provider sets them on every episode it
            # identifies -- `item.TrySetProviderId(MetadataProvider.Tvdb, externalIds?.TvdbId);`
            # and its Imdb twin (MediaBrowser.Providers/Plugins/Tmdb/TV/TmdbEpisodeProvider.cs,
            # v10.10.7) -- and the Webhook plugin sends them as `Provider_*` for an episode exactly
            # as for a film. This double gave episodes none, and three docstrings and an assertion
            # called that the plugin's shape. Well-formed and in no bundle, so a path that ever
            # keys an episode on its own identity finds one, as it would on a real server, rather
            # than parking at stage 1 for want of any (decision 323) and looking harmless.
            # [M5.2 review cycle 4: M52-C4-TTA-04]
            "ProviderIds": {
                "Tvdb": f"8{str(series['Id']).removeprefix('jf-')}{n:05d}",
                "Imdb": f"tt8{str(series['Id']).removeprefix('jf-')}{n:05d}",
            },
        }
        for n in range(1, EPISODE_COUNT.get(str(series["Id"]), 2) + 1)
    ]
    for series in ITEMS
    if series["Type"] == "Series"
}

# Flat views of the same rows. An episode is a library item like any other -- `/Items` may be asked
# for one (§7.2's webhook is the only direction an episode id enters the app) and a library holds
# it through its series, because that is where the tree puts it.
ALL_EPISODES: list[dict[str, Any]] = [row for rows in EPISODES.values() for row in rows]
EPISODE_SERIES: dict[str, str] = {
    str(row["Id"]): series_id for series_id, rows in EPISODES.items() for row in rows
}

# Jellyfin's libraries. §7.2's trigger is scoped to the ones the admin picked and
# `JellyfinConfig.library_ids` has never had a reader (decision 364); membership is a fact the
# SERVER holds, answerable only by asking it, never by a field in the operator's webhook template.
# So it lives here as a mapping and is served through `/Items`'s `ParentId`, which is the parameter
# a real client would scope with. Three rather than the two the plan asks for, so that "the library
# the admin did not pick" is a *choice* and not the only alternative: "Christmas 2019" is the
# household's own footage, resolves to no title at all (§4.2), and is exactly the library nobody
# wants an acquisition job billed for.
LIBRARIES: list[dict[str, Any]] = [
    {"Id": "jf-lib-films", "Name": "Films", "CollectionType": "movies",
     "Type": "CollectionFolder"},
    {"Id": "jf-lib-shows", "Name": "Shows", "CollectionType": "tvshows",
     "Type": "CollectionFolder"},
    {"Id": "jf-lib-home", "Name": "Home Videos", "CollectionType": "homevideos",
     "Type": "CollectionFolder"},
]

LIBRARY_MEMBERS: dict[str, tuple[str, ...]] = {
    "jf-lib-films": ("jf-1", "jf-2", "jf-3", "jf-8"),
    "jf-lib-shows": ("jf-6", "jf-7"),
    "jf-lib-home": ("jf-x",),
}


def _library_of(item_id: str) -> str | None:
    """Which library holds this item -- an episode through its series, the way the tree does."""
    key = EPISODE_SERIES.get(item_id, item_id)
    return next((lib for lib, members in LIBRARY_MEMBERS.items() if key in members), None)


# Where each library's files are on the server's disk -- `VirtualFolderInfo.Locations`, which
# `/Library/VirtualFolders` answers with -- and so where every item's `Path` begins. On a real server
# that is the whole of what membership IS: a library scans its locations, and `LibraryController.
# TranslateParentItem` maps a top-level folder to the library whose `PhysicalLocations` contain its
# path. The home library's location shares a prefix with the films' on purpose, because two
# libraries named `/media/films` and `/media/films-home` are an ordinary layout and a client testing
# `startswith` without a separator would file the household's footage under Films.
# [M5.2 review cycle 4: M52-C4-IDS-01, M52-C4-WH-01; decision 408]
LIBRARY_LOCATIONS: dict[str, tuple[str, ...]] = {
    "jf-lib-films": ("/media/films",),
    "jf-lib-shows": ("/media/shows",),
    "jf-lib-home": ("/media/films-home",),
}


def _path_of(item: dict[str, Any]) -> str | None:
    """The item's `Path` as the server's scan would have found it: under the location of the library
    that holds it, a film in its own folder, a series as its folder, an episode inside its season.
    An item no library holds has no path to derive, which on a real server cannot happen to a file
    it scanned; one that carries its own `Path` keeps it."""
    if item.get("Path"):
        return str(item["Path"])
    library = _library_of(str(item["Id"]))
    locations = LIBRARY_LOCATIONS.get(library or "", ())
    if not locations:
        return None
    if str(item["Type"]) == "Episode":
        series = next((s for s in ITEMS if s["Id"] == item["SeriesId"]), None)
        root = _path_of(series) if series else f"{locations[0]}/{item['SeriesName']}"
        season = int(item.get("ParentIndexNumber") or 1)
        return (f"{root}/Season {season:02d}/{item['SeriesName']} "
                f"S{season:02d}E{int(item.get('IndexNumber') or 0):02d}.mkv")
    if str(item["Type"]) == "Series":
        return f"{locations[0]}/{item['Name']}"
    name = f"{item['Name']} ({item['ProductionYear']})" if item.get("ProductionYear") else item["Name"]
    return f"{locations[0]}/{name}/{name}.mkv"


# Jellyfin's ids are GUIDs, and the server and its Webhook plugin spell one GUID two ways. Every
# REST answer writes `Id` as 32 lowercase hex digits (`JsonGuidConverter` formats with "N"), while
# the plugin assigns the raw `Guid` into its template's data and Handlebars renders it through
# `ToString()`, which is the dashed "D" form -- jellyfin-plugin-webhook#204 is an operator finding
# exactly that. The server's own binder parses either spelling in `ids` and `ParentId`. The seven
# fixture ids above are not GUIDs at all, so a double that compared strings could never refuse a
# client that compared strings too: every real `ItemAdded` then matched nothing and was filed as
# gone from the server, while this suite and the exit criterion stayed green on `jf-7`. So the
# double learns both spellings for any id that IS a GUID, and leaves a fixture id that is not one
# exactly as it was. [M5.2 review cycle 3: M52-C3-EVENTS-01]
_GUID = re.compile(
    r"[0-9a-f]{32}|(?P<brace>\{)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?(brace)\})",
    re.IGNORECASE | re.ASCII,
)


def _guid_key(value: str) -> str:
    """One GUID's identity whatever its spelling, which is what the server's binder compares."""
    text = str(value).strip()
    return uuid.UUID(text).hex if _GUID.fullmatch(text) else text


def _plugin_guid(value: str) -> str:
    """An id as the Webhook plugin renders it: a GUID dashed, anything else untouched."""
    text = str(value)
    return str(uuid.UUID(text)) if _GUID.fullmatch(text) else text


USERS: list[dict[str, Any]] = [
    {"Id": "jf-user-patrick", "Name": "patrick", "Policy": {"IsAdministrator": True}},
    {"Id": "jf-user-jenny", "Name": "jenny", "Policy": {"IsAdministrator": False}},
]

PASSWORD = os.environ.get("FAKE_JELLYFIN_PASSWORD", "jf-password")


class State:
    """Everything mutable, in one object so `/\\_test/reset` is a single assignment."""

    def __init__(self) -> None:
        self.played: dict[str, set[str]] = {u["Id"]: set() for u in USERS}
        self.tokens: dict[str, str] = {}          # token -> jellyfin user id
        self.sessions: list[dict[str, Any]] = []
        self.write_log: list[dict[str, Any]] = []
        # What the Webhook plugin has pushed, for `write_log`'s reason in the other direction: a
        # delivery is a fact about requests, and "the app enqueued one job" is only half an
        # assertion until the other half says how many events it was told about.
        self.webhook_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.__init__()


state = State()
router = APIRouter()


# A request's token, found where the server looks for it and nowhere else. Jellyfin reads `Token=`
# out of the `MediaBrowser` Authorization header first, on every release from 10.9 to 12.1, and the
# `X-Emby-Token` header only after that and only while `EnableLegacyAuthorization` is on
# (Jellyfin.Server.Implementations/Security/AuthorizationContext.cs). 10.11 added that switch
# defaulting on; 12.0 made it default off and ships a migration that turns it off on upgraded
# installs (`20260531160000_DisableLegacyAuthorization`). The double read `X-Emby-Token` alone, so a
# client sending the key nowhere else passed every test here and was anonymous -- a 401 on every
# authenticated read -- against the current release. So the legacy header is honoured exactly while
# the version this double reports is below 12, and a test that reports 12.1 sees what a household
# on `jellyfin/jellyfin:latest` sees. [M5.2 review cycle 4: M52-C4-AUTH-01, M52-C4-TTA-03]
def _legacy_authorization() -> bool:
    numbers = [int(part) for part in re.findall(r"\d+", SERVER_VERSION)[:2]]
    return tuple(numbers) < (12, 0)


# `AuthorizationContext.GetParts`: comma-separated `key=value` pairs after the scheme, a quoted value
# kept whole, the quotes trimmed and the value URL-decoded. Keys are case-sensitive there too.
_PAIR = re.compile(r'\s*([^=,\s]+)=("[^"]*"|[^,]*)')


def _token(request: Request) -> str | None:
    legacy = _legacy_authorization()
    header = request.headers.get("authorization") or ""
    if not header and legacy:
        header = request.headers.get("x-emby-authorization") or ""
    scheme, _, rest = header.partition(" ")
    token = ""
    if scheme.lower() == "mediabrowser" or (legacy and scheme.lower() == "emby"):
        pairs = {key: unquote_plus(value.strip('"')) for key, value in _PAIR.findall(rest)}
        token = pairs.get("Token", "")
    if not token and legacy:
        token = request.headers.get("x-emby-token") or request.headers.get("x-mediabrowser-token")
    return token or None


def _require_api_key(token: str | None) -> None:
    if token != API_KEY:
        raise HTTPException(401, "invalid api key")


def _user_for_token(token: str | None) -> str | None:
    return state.tokens.get(token or "")


def _item(item_id: str) -> dict[str, Any]:
    for item in ITEMS:
        if item["Id"] == item_id:
            return item
    raise HTTPException(404, "no such item")


@router.get("/System/Info/Public")
async def info() -> dict[str, Any]:
    """Unauthenticated, exactly as Jellyfin serves it — which is what lets the admin's test
    button tell a wrong URL apart from a wrong key."""
    return {"ServerName": "Fake Jellyfin", "Version": SERVER_VERSION, "Id": "fake-server"}


@router.get("/Users")
async def users(request: Request) -> list[dict[str, Any]]:
    _require_api_key(_token(request))
    return USERS


@router.get("/Library/MediaFolders")
async def media_folders(request: Request) -> dict[str, Any]:
    """The libraries §6.6's pick picks from, behind the admin key exactly as `/Users` and `/Items`.

    Jellyfin's envelope and not a bare list: `{"Items": [...], "TotalRecordCount": n}`. The shape is
    half the point -- `/Users` above IS a bare list, on this server and on the real one, so a client
    that reads the two the same way is wrong about one of them and finds out here rather than in
    front of the admin. The other half is the key, for the reason `/Items` gives below: a real server
    authenticates this read, and a fake more permissive than the server it stands in for is precisely
    how `dd05-fake-and-e2e` happened.
    """
    _require_api_key(_token(request))
    return {"Items": LIBRARIES, "TotalRecordCount": len(LIBRARIES)}


@router.get("/Library/VirtualFolders")
async def virtual_folders(request: Request) -> list[dict[str, Any]]:
    """Each library with the paths it scans -- `VirtualFolderInfo`, as a BARE list.

    `ItemId` is the library's id in the server's "N" spelling (`libraryFolder.Id.ToString("N")` in
    `LibraryManager.GetVirtualFolderInfo`), the same id `/Library/MediaFolders` answers as `Id`, and
    `Locations` is what makes it the membership read: the server exposes no other fact that names
    the library an item is in to a caller holding only the API key. Behind the key as the server
    has it (`FirstTimeSetupOrElevated`). [M5.2 review cycle 4: M52-C4-IDS-01; decision 408]
    """
    _require_api_key(_token(request))
    return [
        {"Name": lib["Name"], "Locations": list(LIBRARY_LOCATIONS.get(str(lib["Id"]), ())),
         "CollectionType": lib["CollectionType"], "LibraryOptions": {},
         "ItemId": _guid_key(str(lib["Id"])), "PrimaryImageItemId": None,
         "RefreshProgress": None, "RefreshStatus": "Idle"}
        for lib in LIBRARIES
    ]


# Jellyfin returns a small default projection and only adds the rest when `Fields` asks. A fake
# that ignores `Fields` lets a client "prove" it requests ProviderIds while never requiring it —
# the assertion becomes the implementation compared to itself. So the projection is real here.
# An episode's series identity joins them rather than waiting for `Fields`, because `SeriesId`,
# `SeriesName`, `ParentIndexNumber` and `IndexNumber` are core `BaseItemDto` properties and not
# `ItemFields` entries: a real server sends them on every episode row unasked, and
# `/Shows/{id}/Episodes` below has always done so. `DateCreated` is the opposite case -- a genuine
# `ItemFields` entry -- so it stays out and appears only when a client asks, which is the whole
# reason `connectors/jellyfin.FIELDS` is falsifiable rather than decorative.
DEFAULT_FIELDS = {
    "Id", "Name", "Type", "ProductionYear", "RunTimeTicks",
    "SeriesId", "SeriesName", "ParentIndexNumber", "IndexNumber",
}
# What the double keeps about an item and a real server never puts on the wire (see ITEMS).
_NEVER_SERVED = {"DateLastSaved"}


def _projected(item: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """One row as the server serialises it for this `Fields`: the default projection, what was
    asked for, and `Path` -- an `ItemFields` entry -- derived from where the item lives."""
    row = {
        k: v for k, v in item.items()
        if (k in DEFAULT_FIELDS or k in fields) and k not in _NEVER_SERVED
    }
    if "Path" in fields and (path := _path_of(item)) is not None:
        row["Path"] = path
    return row
ITEM_TYPES_ASKED: list[str] = []
# Recorded for the same reason as ITEM_TYPES_ASKED: a cache is only cached if the second call
# does not arrive, and that is a fact about requests, not about a dict.
EPISODES_ASKED: list[str] = []


@router.get("/Items")
async def items(
    userId: str | None = Query(default=None),  # noqa: N803 - Jellyfin's own parameter name
    StartIndex: int = Query(default=0),  # noqa: N803
    Limit: int = Query(default=500),  # noqa: N803
    Recursive: str = Query(default="false"),  # noqa: N803
    IncludeItemTypes: str = Query(default=""),  # noqa: N803
    Fields: str = Query(default=""),  # noqa: N803
    ids: str = Query(default=""),
    ParentId: str | None = Query(default=None),  # noqa: N803
    MinDateLastSaved: str | None = Query(default=None),  # noqa: N803
    *,
    request: Request,
) -> dict[str, Any]:
    _require_api_key(_token(request))
    # The parameter is optional and its absence is not a wildcard: §7.2 re-derives ownership from
    # the library the *admin key* can see, because item visibility is per-user and the union of two
    # people's views is not "the household's library". A real server answers a keyless read with no
    # `UserData` at all, so this one must too -- a fake more permissive than the server it stands in
    # for is precisely how `dd05-fake-and-e2e` happened. An unknown id that IS given still 404s.
    if userId is not None and userId not in state.played:
        raise HTTPException(404, "no such user")

    # Without Recursive the real server returns the library folders, not the films inside them.
    # A client that forgets it gets an empty result rather than a silently-correct one.
    if Recursive.lower() != "true":
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": StartIndex}

    wanted_types = {t.strip().lower() for t in IncludeItemTypes.split(",") if t.strip()}
    # Recorded so a test can assert the client narrowed the query rather than asking for
    # everything and filtering afterwards.
    globals()["ITEM_TYPES_ASKED"] = sorted(wanted_types)
    fields = {f.strip() for f in Fields.split(",") if f.strip()}
    # Episodes are in the pool for one reason: `ITEM_TYPES = "Movie,Series"` excludes them, and an
    # exclusion the double cannot violate is an assumption rather than a test
    # (`test_items_are_narrowed_to_movies_and_series`). A read that NAMES its types and does not
    # name Episode still gets none, which is every read the app makes -- `connectors/jellyfin.py`
    # sends `IncludeItemTypes` on all of them -- so the seven rows the sweep walks are the seven it
    # always walked. An unfiltered read now returns the episodes as well, because a real server
    # recursing a library returns what is in it, and no shipped caller makes one.
    matching = [
        item for item in (*ITEMS, *ALL_EPISODES)
        if not wanted_types or str(item["Type"]).lower() in wanted_types
    ]
    # `ids` looks an item up by id and `ParentId` scopes a walk to a library, both compared as GUIDs
    # whatever their spelling, because that is what the server's binder does -- and the two do NOT
    # combine, which the paragraph on `ParentId` below says.
    #
    # A `ParentId` naming no folder of this server is a 400, and it used to be an empty page this
    # file called "the honest answer and not an error". It is not the server's answer: on 10.9 and
    # 10.10 `LibraryManager.GetParentItem` throws `ArgumentException("Invalid parent id")` and the
    # exception middleware maps that to 400. The empty page was the double being MORE permissive
    # than Jellyfin, which is `dd05-fake-and-e2e`'s shape again -- against it a deleted library in
    # the pick recorded adds as unwanted, while against a real server the same pick 400ed every
    # delta walk, and nothing in this suite could see either.
    # [M5.2 review cycle 3: M52-C3-LIB-02, M52-C3-SWEEP-04]
    #
    # THE ONE PERMISSIVENESS LEFT IS FORCED BY THE FIXTURE. A real server DROPS a piece of `ids`
    # that does not parse as a GUID (`CommaDelimitedCollectionModelBinder` logs the FormatException
    # at debug and leaves the value out), and an `ids` emptied that way filters nothing at all. The
    # seven ids above are not GUIDs, so this double has to keep matching them by string; what
    # makes that harmless is the `Limit` the client's membership read now sends, which bounds the
    # read a real server turns such an id into. [M5.2 review cycle 3: M52-C3-EVENTS-04]
    wanted_ids = {_guid_key(i) for i in ids.split(",") if i.strip()}
    if wanted_ids:
        matching = [item for item in matching if _guid_key(str(item["Id"])) in wanted_ids]
    # A `ParentId` is RESOLVED on every read and APPLIED only on a read that names no `ids`, which
    # is the server's behaviour and not a shortcut. `ItemsController.GetItems` resolves the folder
    # (`GetParentItem`, the 400 above) and then sets `query.Parent = null` -- which empties
    # `ParentId` -- before `folder.GetItems(query)`; `Folder.GetItems` sends any query with
    # `ItemIds` straight to `LibraryManager.GetItemsResult`, whose only library scoping needs a
    # non-empty `ParentId`. Read at v10.9.11, v10.10.7, v10.11.0 and v12.1, identically. The double
    # applied it to both, so a membership read spelled `ids=<key>&ParentId=<lib>` agreed with it
    # and a deselected library's add came back under the first picked library from every real
    # server. [M5.2 review cycle 4: M52-C4-IDS-01, M52-C4-WH-01; decision 408]
    if ParentId is not None:
        folder = _guid_key(ParentId)
        if folder not in {_guid_key(str(lib["Id"])) for lib in LIBRARIES}:
            raise HTTPException(400, f"Invalid parent id: {ParentId}")
        if not wanted_ids:
            matching = [
                item for item in matching
                if _guid_key(str(_library_of(str(item["Id"])) or "")) == folder
            ]
    # §7.2's delta path. Jellyfin has no `MinDateCreated`; `MinDateLastSaved` filters on
    # `DateLastSaved`, the instant the server last saved the row, and it is INCLUSIVE and excludes a
    # row that was never saved: `DateLastSaved not null and DateLastSaved>=@...` in 10.10's SQLite
    # repository, `e.DateLastSaved != null && e.DateLastSaved >= ...` in 10.11's. This answered it
    # from `DateCreated`, strictly -- the double's own premise, shared with the client, that the
    # two were one instant. [M5.2 review cycle 4: M52-C4-REST-02, M52-C4-TTA-02; decision 409]
    if MinDateLastSaved is not None:
        floor = _instant(MinDateLastSaved)
        matching = [
            item for item in matching
            if item.get("DateLastSaved") and _instant(str(item["DateLastSaved"])) >= floor
        ]

    played = state.played[userId] if userId is not None else None
    page = []
    for item in matching[StartIndex : StartIndex + Limit]:
        projected = _projected(item, fields)
        if "UserData" in fields and played is not None:
            projected["UserData"] = {
                "Played": item["Id"] in played, "PlaybackPositionTicks": 0
            }
        page.append(projected)
    return {"Items": page, "TotalRecordCount": len(matching), "StartIndex": StartIndex}


@router.get("/Shows/{series_id}/Episodes")
async def show_episodes(
    series_id: str,
    userId: str | None = Query(default=None),  # noqa: N803
    *,
    request: Request,
) -> dict[str, Any]:
    """Decision 210(c)'s read: which episode is the series' last known one.

    Narrow on purpose -- the same item store, the same 404s, and `UserData` only when a user id is
    given, exactly as `/Items` above.
    """
    _require_api_key(_token(request))
    if series_id not in EPISODES:
        raise HTTPException(404, "no such series")
    if userId is not None and userId not in state.played:
        raise HTTPException(404, "no such user")
    EPISODES_ASKED.append(series_id)
    rows = []
    for episode in EPISODES[series_id]:
        row = {k: v for k, v in episode.items() if k not in _NEVER_SERVED}
        if userId is not None:
            row["UserData"] = {
                "Played": episode["Id"] in state.played[userId], "PlaybackPositionTicks": 0
            }
        rows.append(row)
    return {"Items": rows, "TotalRecordCount": len(rows)}


@router.get("/Sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    _require_api_key(_token(request))
    return state.sessions


@router.post("/Users/AuthenticateByName")
async def authenticate(request: Request) -> dict[str, Any]:
    body = await request.json()
    name = str(body.get("Username") or "")
    match = next((u for u in USERS if u["Name"].lower() == name.lower()), None)
    if match is None or body.get("Pw") != PASSWORD:
        raise HTTPException(401, "invalid username or password")
    token = f"user-token-{match['Id']}"
    state.tokens[token] = match["Id"]
    return {"AccessToken": token, "User": match}


@router.api_route("/UserPlayedItems/{item_id}", methods=["POST", "DELETE"])
async def set_played(
    item_id: str,
    request: Request,
    userId: str = Query(...),  # noqa: N803
) -> dict[str, Any]:
    """The one write, and the one place this fake is stricter than a mock would be.

    A real Jellyfin admin key *would* be accepted here — it is admin-equivalent and has no
    read-only variant (§14.3). This fake refuses it on purpose, because the app's own restraint
    is the only thing enforcing §7.3's least-privilege path and a test that cannot fail when
    that restraint is removed is not testing it.
    """
    token = _token(request)
    if token == API_KEY:
        raise HTTPException(403, "this fake refuses the admin key on Played writes (§7.3)")
    token_user = _user_for_token(token)
    if token_user is None:
        raise HTTPException(401, "invalid token")
    if token_user != userId:
        raise HTTPException(403, "a user token may only set that user's own state")

    _item(item_id)
    played = request.method == "POST"
    if played:
        state.played[userId].add(item_id)
    else:
        state.played[userId].discard(item_id)
    state.write_log.append({"user": userId, "item": item_id, "played": played})
    return {"Played": played, "ItemId": item_id, "UserId": userId}


# --- control surface (no Jellyfin counterpart) ------------------------------------------


class PlayedControl(BaseModel):
    user_id: str
    item_id: str
    played: bool = True


class SessionControl(BaseModel):
    user_id: str
    item_id: str
    fraction: float = 0.95
    session_id: str = "sess-1"


control = APIRouter(prefix="/_test")


@control.post("/reset")
async def reset() -> dict[str, bool]:
    state.reset()
    return {"ok": True}


@control.post("/played")
async def force_played(body: PlayedControl) -> dict[str, Any]:
    """Simulate someone marking a title watched *in Jellyfin* — the other direction."""
    if body.played:
        state.played.setdefault(body.user_id, set()).add(body.item_id)
    else:
        state.played.setdefault(body.user_id, set()).discard(body.item_id)
    return {"ok": True, "played": sorted(state.played[body.user_id])}


def _now_playing(item: dict[str, Any]) -> dict[str, Any]:
    """The `NowPlayingItem` a real server would send for this library item.

    Every row carries `Type`, because the app decides what a session *is* from it (§7.3), and a
    Series row becomes its **last** known episode: decision 210(c) makes a series finish only when
    the finished episode is the last one the server knows about, and this control surface exists so
    a test can arm that prompt. `/_test/session`'s request body stays a bare (user, item, fraction)
    so `e2e/helpers.js` needs no edit for any of it.
    """
    if str(item["Type"]) != "Series":
        return {
            "Id": str(item["Id"]),
            "Name": item["Name"],
            "Type": str(item["Type"]),
            "RunTimeTicks": int(item["RunTimeTicks"]),
        }
    episodes = EPISODES.get(str(item["Id"])) or []
    if not episodes:
        raise HTTPException(409, "this fake has no episodes for that series")
    episode = episodes[-1]
    return {
        "Id": episode["Id"],
        "Name": episode["Name"],
        "Type": "Episode",
        "SeriesId": episode["SeriesId"],
        "SeriesName": episode["SeriesName"],
        "ParentIndexNumber": episode["ParentIndexNumber"],
        "IndexNumber": episode["IndexNumber"],
        "RunTimeTicks": int(episode["RunTimeTicks"]),
    }


@control.post("/session")
async def force_session(body: SessionControl) -> dict[str, Any]:
    playing = _now_playing(_item(body.item_id))
    runtime = int(playing["RunTimeTicks"])
    state.sessions = [
        s for s in state.sessions if s["Id"] != body.session_id
    ] + [
        {
            "Id": body.session_id,
            "UserId": body.user_id,
            "NowPlayingItem": {
                **playing,
                "UserData": {
                    "Played": playing["Id"] in state.played.get(body.user_id, set())
                },
            },
            "PlayState": {"PositionTicks": int(runtime * body.fraction)},
        }
    ]
    return {"ok": True, "sessions": len(state.sessions)}


@control.post("/sessions/clear")
async def clear_sessions() -> dict[str, bool]:
    state.sessions = []
    return {"ok": True}


@control.get("/state")
async def dump() -> dict[str, Any]:
    return {
        "played": {k: sorted(v) for k, v in state.played.items()},
        "tokens": list(state.tokens),
        "sessions": len(state.sessions),
        "writes": state.write_log,
        "webhooks": state.webhook_log,
    }


# --- the Webhook plugin, which pushes rather than answers (§7.2) -------------------------

# The plugin is a separate thing from the server's API: it POSTs an operator-authored template to a
# URL the operator typed, carrying whatever headers the operator added. None of it is server state,
# which is why `/_test/reset` leaves it alone -- a reset restores the LIBRARY, not the operator's
# wiring -- and why the emitter refuses rather than guesses when it has none.
WEBHOOK_URL = os.environ.get("FAKE_JELLYFIN_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("FAKE_JELLYFIN_WEBHOOK_TOKEN", "")
# §7.2 says "token-authed" and fixes nothing else. The plugin's Generic destination carries a secret
# exactly one way -- "Add Request Header" -- so that is the way this sends it, under the prefix
# `api/deps.py:288` already spells. Env-overridable because the handler owns that contract and
# publishes it (decision 365's `ops/jellyfin-webhook-template.json`); this file only has to be able
# to get it wrong on purpose.
WEBHOOK_TOKEN_HEADER = os.environ.get("FAKE_JELLYFIN_WEBHOOK_TOKEN_HEADER", "X-Spielplan-Token")
# Set by `backend/tests/conftest.py`'s fixture to an `httpx.ASGITransport` over the real app, so the
# SAME emitter that POSTs across the compose network delivers in-process with no socket. The
# alternative -- letting the test build the payload and post it itself -- is the one thing a double
# must not allow, because a test cannot be wrong about a shape it authored.
WEBHOOK_TRANSPORT: Any = None


class WebhookControl(BaseModel):
    url: str | None = None
    token: str | None = None


class ItemAddedControl(BaseModel):
    """What to emit. Every field past the first exists so the emitter can be wrong on purpose."""

    item_id: str
    episodes: int = 0
    token: str | None = None
    omit: tuple[str, ...] = ()
    notification_type: str = "ItemAdded"
    # The operator who hardcoded a literal where `ops/jellyfin-webhook-template.json` renders
    # `{{ItemType}}`, or who ticked an item type this app does not acquire. It is a lever rather
    # than a body a test writes, because the rest of the payload still has to be the plugin's:
    # what makes this delivery interesting is that every other field is right.
    item_type: str | None = None
    url: str | None = None


def _item_added(item: dict[str, Any], notification_type: str) -> dict[str, Any]:
    """One event as the Webhook plugin's template renders it.

    Decision 365 ships the operator's copy of this as `ops/jellyfin-webhook-template.json` and calls
    that file the contract; the two have to say the same thing, and this is the half a test can run
    against. It lives here rather than in a test for the reason the rest of this file exists: the
    plugin flattens provider ids into `Provider_<lowercase>` keys and sends an episode's series
    identity beside the episode's own, and a test that wrote that out by hand would prove the
    handler tolerates a shape nobody sends. An Episode carries `Provider_*` of its own as well:
    `DataObjectHelpers.AddBaseItemData` flattens `item.ProviderIds` after its type switch, so for
    every item (plugin v17, unchanged at v18 and v22). Those ids name the EPISODE, which is why
    decision 369 still keys the event on `SeriesId` -- this docstring once said an episode carried
    none, and a test asserted it against this double. [M5.2 review cycle 4: M52-C4-TTA-04]
    `Timestamp` and `UtcTimestamp` are the same instant here because this server has no other zone.

    `ItemId` and `SeriesId` go out the way the plugin renders them -- a GUID dashed, where `/Items`
    answers the same GUID undashed -- because that difference is the plugin's and a double that
    spelled them alike hid it from every test (`_plugin_guid`).
    """
    now = _stamp(datetime.now(UTC))
    payload: dict[str, Any] = {
        "ServerId": "fake-server",
        "ServerName": "Fake Jellyfin",
        "ServerVersion": SERVER_VERSION,
        "NotificationType": notification_type,
        "Timestamp": now,
        "UtcTimestamp": now,
        "Name": item["Name"],
        "ItemId": _plugin_guid(item["Id"]),
        "ItemType": item["Type"],
        "RunTimeTicks": item["RunTimeTicks"],
    }
    if item.get("ProductionYear"):
        payload["Year"] = item["ProductionYear"]
    for source, value in dict(item.get("ProviderIds") or {}).items():
        payload[f"Provider_{source.lower()}"] = value
    if str(item["Type"]) == "Episode":
        payload["SeriesId"] = _plugin_guid(item["SeriesId"])
        payload["SeriesName"] = item["SeriesName"]
        payload["SeasonNumber"] = item["ParentIndexNumber"]
        payload["EpisodeNumber"] = item["IndexNumber"]
    return payload


@control.post("/webhook")
async def configure_webhook(body: WebhookControl) -> dict[str, Any]:
    """Where to push, for a stack whose compose file does not say.

    `ops/compose.e2e.yml` hands this service exactly two environment variables, neither of them a
    webhook target, so over a socket there is otherwise nowhere to put one. A run wires it here and
    the compose file keeps saying what it says. The token is never echoed back: this answers whether
    one is SET, which is the only part of it a caller has any business reading.
    """
    global WEBHOOK_URL, WEBHOOK_TOKEN
    if body.url is not None:
        WEBHOOK_URL = body.url
    if body.token is not None:
        WEBHOOK_TOKEN = body.token
    return {
        "ok": True,
        "url": WEBHOOK_URL,
        "token_header": WEBHOOK_TOKEN_HEADER,
        "token_set": bool(WEBHOOK_TOKEN),
    }


@control.post("/item-added")
async def emit_item_added(body: ItemAddedControl) -> dict[str, Any]:
    """Fire §7.2's webhook at the app the way the plugin does -- and refuse to fake what it cannot.

    A refuser, in the four directions this milestone needs one:

      * an unknown item id 404s rather than inventing a library entry, so a burst can only be
        emitted for a series this server actually holds;
      * `episodes = n` against a series with fewer than n of them 409s, because a debounce test that
        quietly emitted eight events for "a burst of twelve" would pass for the wrong reason;
      * `omit` naming a field this template never carries 409s -- tolerating a field the plugin does
        not send is not tolerance of anything, and decision 365's two required fields are only worth
        asserting against the shape an operator can really produce;
      * with no target configured it 409s instead of reporting ok on a delivery that never left.

    `token` is the deliberate wrongness the 401 case needs: absent, it sends the configured one;
    `""` sends no header at all, which is the operator who never added one; anything else sends
    that, which is the operator who typed it wrong. `item_type` is the same kind of lever for the
    one field an operator can get wrong without getting anything else wrong. The statuses come
    back unjudged, because what the app answered is the assertion's business and not the
    double's.
    """
    item = _item(body.item_id)
    if body.episodes:
        if str(item["Type"]) != "Series":
            raise HTTPException(409, "only a series has episodes to burst")
        rows = EPISODES.get(str(item["Id"])) or []
        if len(rows) < body.episodes:
            raise HTTPException(409, f"this fake holds {len(rows)} episodes for that series")
        sources = list(rows[: body.episodes])
    else:
        sources = [item]

    payloads = []
    for source in sources:
        payload = _item_added(source, body.notification_type)
        if body.item_type is not None:
            payload["ItemType"] = body.item_type
        for field in body.omit:
            if field not in payload:
                raise HTTPException(409, f"this template never sends {field!r}")
            payload.pop(field)
        payloads.append(payload)

    url = body.url or WEBHOOK_URL
    if not url:
        raise HTTPException(409, "no webhook target configured; POST /_test/webhook first")
    token = WEBHOOK_TOKEN if body.token is None else body.token
    headers = {WEBHOOK_TOKEN_HEADER: token} if token else {}
    # The Generic destination's `new StringContent(body, Encoding.UTF8, contentType)`, whose type
    # is `text/plain` unless the operator adds a Content-Type header (`GenericClient.SendAsync`,
    # plugin v17 and v18). httpx's `json=` labelled every delivery with the JSON type instead, so
    # a handler that came to insist on it would have passed here and refused every real plugin.
    # [M5.2 review cycle 4: M52-C4-WH-02]
    headers["Content-Type"] = "text/plain; charset=utf-8"
    statuses = []
    async with httpx.AsyncClient(transport=WEBHOOK_TRANSPORT, timeout=10.0) as sender:
        for payload in payloads:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            statuses.append((await sender.post(url, content=content, headers=headers)).status_code)
    state.webhook_log.extend(payloads)
    return {"ok": True, "sent": len(payloads), "statuses": statuses, "payloads": payloads}


def create_app() -> FastAPI:
    app = FastAPI(title="Fake Jellyfin", docs_url=None, redoc_url=None)
    app.include_router(router)
    app.include_router(control)
    return app


app = create_app()

if __name__ == "__main__":  # pragma: no cover - the compose entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8096")))
