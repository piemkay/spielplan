"""A fake Jellyfin (>= 10.9) for tests, never shipped. It refuses the admin key on the Played write
(§7.3) so the per-user-token path can fail. `/_test/*` is the control surface, including pushing
the Webhook plugin's `ItemAdded`; payload shapes live here so no test can author its own.
"""

from __future__ import annotations

import json
import os
import re
import struct
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel

TICKS_PER_MINUTE = 60 * 10_000_000

API_KEY = os.environ.get("FAKE_JELLYFIN_API_KEY", "fake-admin-key")
SERVER_VERSION = os.environ.get("FAKE_JELLYFIN_VERSION", "10.10.3")

# Jellyfin writes seven fractional digits and a `Z`, which `fromisoformat` rejects; compared as
# instants, never as strings, because the app's Postgres-written watermark spells them differently.
_ISO = re.compile(
    r"^(?P<secs>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
)


def _instant(text: str) -> datetime:
    """400 on anything unparseable: a real server does not guess at a filter."""
    match = _ISO.match(text.strip())
    if match is None:
        raise HTTPException(400, f"unparseable date: {text!r}")
    zone = match.group("zone") or "Z"
    return datetime.fromisoformat(
        f"{match.group('secs')}.{(match.group('frac') or '')[:6]:0<6}"
        f"{'+00:00' if zone == 'Z' else zone}"
    )


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _minutes_after(stamp: str, minutes: int) -> str:
    return _stamp(_instant(stamp) + timedelta(minutes=minutes))


# Mirrors backend/tests/fixtures/make_bundle.py, except the last two: "Tampopo" has no ProviderIds
# (name/year fallback) and "Christmas 2019" resolves to no title and must not create one (§4.2).
#
# `DateCreated` is stamped from the FILE; arrival is `DateLastSaved`, which `MinDateLastSaved`
# filters on and the server never serves. Tampopo and Christmas have files years older than their
# arrival, and arrivals span years so a fake that ignored the filter would fail (decision 409).
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

# A session on a Series plays an Episode. At least two per series so "the last known episode"
# (decision 210(c)) is a choice; twelve on The Bear so §7.2's debounce has a burst to collapse.
# Severance stays at two: `test_jellyfin_client.py` pins its episodes.
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
            # A scan stamps a season minutes apart: the burst is one import.
            "DateCreated": _minutes_after(str(series["DateCreated"]), n),
            "DateLastSaved": _minutes_after(str(series["DateLastSaved"]), n),
            # The episode's own ids, as Jellyfin's TMDb provider sets them; in no bundle.
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

ALL_EPISODES: list[dict[str, Any]] = [row for rows in EPISODES.values() for row in rows]
EPISODE_SERIES: dict[str, str] = {
    str(row["Id"]): series_id for series_id, rows in EPISODES.items() for row in rows
}

# Library membership is a fact the server holds (decision 364). Three libraries, so "the one the
# admin did not pick" is a choice; the home videos are the one nobody wants acquired.
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
    """An episode belongs to its series' library."""
    key = EPISODE_SERIES.get(item_id, item_id)
    return next((lib for lib, members in LIBRARY_MEMBERS.items() if key in members), None)


# `VirtualFolderInfo.Locations`, where every item's `Path` begins (decision 408). films-home shares
# a prefix with films on purpose: a `startswith` without a separator files it under Films.
LIBRARY_LOCATIONS: dict[str, tuple[str, ...]] = {
    "jf-lib-films": ("/media/films",),
    "jf-lib-shows": ("/media/shows",),
    "jf-lib-home": ("/media/films-home",),
}


def _path_of(item: dict[str, Any]) -> str | None:
    """The item's `Path` as the scan would have found it under its library's location."""
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


# REST writes a GUID as 32 hex digits ("N"); the Webhook plugin renders it dashed ("D",
# jellyfin-plugin-webhook#204). The server's binder takes either. Non-GUID fixture ids stay as-is.
_GUID = re.compile(
    r"[0-9a-f]{32}|(?P<brace>\{)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?(brace)\})",
    re.IGNORECASE | re.ASCII,
)


def _guid_key(value: str) -> str:
    text = str(value).strip()
    return uuid.UUID(text).hex if _GUID.fullmatch(text) else text


def _plugin_guid(value: str) -> str:
    text = str(value)
    return str(uuid.UUID(text)) if _GUID.fullmatch(text) else text


USERS: list[dict[str, Any]] = [
    {"Id": "jf-user-patrick", "Name": "patrick", "Policy": {"IsAdministrator": True}},
    {"Id": "jf-user-jenny", "Name": "jenny", "Policy": {"IsAdministrator": False}},
]

PASSWORD = os.environ.get("FAKE_JELLYFIN_PASSWORD", "jf-password")


class State:
    def __init__(self) -> None:
        self.played: dict[str, set[str]] = {u["Id"]: set() for u in USERS}
        self.tokens: dict[str, str] = {}          # token -> jellyfin user id
        self.sessions: list[dict[str, Any]] = []
        self.write_log: list[dict[str, Any]] = []
        self.webhook_log: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.__init__()


state = State()
router = APIRouter()


# Jellyfin reads `Token=` from the `MediaBrowser` Authorization header first; the legacy
# `X-Emby-Token` headers only while `EnableLegacyAuthorization` is on, which is off from 12.0.
def _legacy_authorization() -> bool:
    numbers = [int(part) for part in re.findall(r"\d+", SERVER_VERSION)[:2]]
    return tuple(numbers) < (12, 0)


# `AuthorizationContext.GetParts`: quoted values kept whole then URL-decoded; keys case-sensitive.
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
    """Unauthenticated, as Jellyfin serves it: tells a wrong URL apart from a wrong key."""
    return {"ServerName": "Fake Jellyfin", "Version": SERVER_VERSION, "Id": "fake-server"}


@router.get("/Users")
async def users(request: Request) -> list[dict[str, Any]]:
    _require_api_key(_token(request))
    return USERS


@router.get("/Library/MediaFolders")
async def media_folders(request: Request) -> dict[str, Any]:
    """An `{"Items", "TotalRecordCount"}` envelope, where `/Users` is a bare list, as on a real server."""
    _require_api_key(_token(request))
    return {"Items": LIBRARIES, "TotalRecordCount": len(LIBRARIES)}


@router.get("/Library/VirtualFolders")
async def virtual_folders(request: Request) -> list[dict[str, Any]]:
    """`VirtualFolderInfo` as a bare list; `Locations` is the only membership fact the key can read."""
    _require_api_key(_token(request))
    return [
        {"Name": lib["Name"], "Locations": list(LIBRARY_LOCATIONS.get(str(lib["Id"]), ())),
         "CollectionType": lib["CollectionType"], "LibraryOptions": {},
         "ItemId": _guid_key(str(lib["Id"])), "PrimaryImageItemId": None,
         "RefreshProgress": None, "RefreshStatus": "Idle"}
        for lib in LIBRARIES
    ]


# Jellyfin's default projection; the rest only when `Fields` asks, so a client must ask. Episode
# series fields are core `BaseItemDto` properties, sent unasked.
DEFAULT_FIELDS = {
    "Id", "Name", "Type", "ProductionYear", "RunTimeTicks",
    "SeriesId", "SeriesName", "ParentIndexNumber", "IndexNumber",
}
_NEVER_SERVED = {"DateLastSaved"}


def _projected(item: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    row = {
        k: v for k, v in item.items()
        if (k in DEFAULT_FIELDS or k in fields) and k not in _NEVER_SERVED
    }
    if "Path" in fields and (path := _path_of(item)) is not None:
        row["Path"] = path
    return row
ITEM_TYPES_ASKED: list[str] = []
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
    # A keyless read gets no `UserData`, as from a real server.
    if userId is not None and userId not in state.played:
        raise HTTPException(404, "no such user")

    # Without Recursive a real server returns the library folders, not the films.
    if Recursive.lower() != "true":
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": StartIndex}

    wanted_types = {t.strip().lower() for t in IncludeItemTypes.split(",") if t.strip()}
    globals()["ITEM_TYPES_ASKED"] = sorted(wanted_types)
    fields = {f.strip() for f in Fields.split(",") if f.strip()}
    # Episodes are in the pool so the app's `IncludeItemTypes` exclusion is testable.
    matching = [
        item for item in (*ITEMS, *ALL_EPISODES)
        if not wanted_types or str(item["Type"]).lower() in wanted_types
    ]
    # One permissiveness, forced by the fixture: a real server drops a non-GUID piece of `ids`
    # (and an emptied `ids` filters nothing); this matches the fixture ids by string.
    wanted_ids = {_guid_key(i) for i in ids.split(",") if i.strip()}
    if wanted_ids:
        matching = [item for item in matching if _guid_key(str(item["Id"])) in wanted_ids]
    # As the server does: an unknown `ParentId` is a 400, and it scopes only a read with no `ids`
    # (`ItemsController.GetItems` drops it when `ids` is given; decision 408).
    if ParentId is not None:
        folder = _guid_key(ParentId)
        if folder not in {_guid_key(str(lib["Id"])) for lib in LIBRARIES}:
            raise HTTPException(400, f"Invalid parent id: {ParentId}")
        if not wanted_ids:
            matching = [
                item for item in matching
                if _guid_key(str(_library_of(str(item["Id"])) or "")) == folder
            ]
    # §7.2's delta: inclusive, and a never-saved row is excluded, as in the server's repository.
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
    """Decision 210(c)'s read: which episode is the series' last known one."""
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


# Decision 483's first poster source. These two 404, so the art route's fall-through is testable.
NO_PRIMARY_IMAGE = {"jf-8", "jf-x"}
IMAGES_ASKED: list[str] = []


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A real flat-colour PNG, which a browser decodes to a nonzero `naturalWidth`."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


@router.get("/Items/{item_id}/Images/Primary")
async def primary_image(item_id: str) -> Response:
    """Anonymous, as Jellyfin serves item images. `maxWidth` and `quality` are ignored."""
    IMAGES_ASKED.append(item_id)
    known = {str(item["Id"]) for item in ITEMS} | set(EPISODE_SERIES)
    if item_id not in known or item_id in NO_PRIMARY_IMAGE:
        raise HTTPException(404, "no such image")
    shade = sum(item_id.encode()) % 200
    return Response(_png(2, 3, (shade, 64, 200 - shade)), media_type="image/png")


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
    """A real server accepts the admin key here. This one refuses it on purpose, so a test fails
    when the app's own §7.3 restraint is removed."""
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
    if body.played:
        state.played.setdefault(body.user_id, set()).add(body.item_id)
    else:
        state.played.setdefault(body.user_id, set()).discard(body.item_id)
    return {"ok": True, "played": sorted(state.played[body.user_id])}


def _now_playing(item: dict[str, Any]) -> dict[str, Any]:
    """A Series plays its last known episode, so a test can arm decision 210(c)'s prompt."""
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

# The operator's wiring, not server state: `/_test/reset` leaves it alone.
WEBHOOK_URL = os.environ.get("FAKE_JELLYFIN_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("FAKE_JELLYFIN_WEBHOOK_TOKEN", "")
# The Generic destination carries a secret only as an added request header.
WEBHOOK_TOKEN_HEADER = os.environ.get("FAKE_JELLYFIN_WEBHOOK_TOKEN_HEADER", "X-Spielplan-Token")
# Set by `backend/tests/conftest.py` to an ASGITransport over the real app.
WEBHOOK_TRANSPORT: Any = None


class WebhookControl(BaseModel):
    url: str | None = None
    token: str | None = None


class ItemAddedControl(BaseModel):
    """Every field past the first exists so the emitter can be wrong on purpose."""

    item_id: str
    episodes: int = 0
    token: str | None = None
    omit: tuple[str, ...] = ()
    notification_type: str = "ItemAdded"
    item_type: str | None = None
    url: str | None = None


def _item_added(item: dict[str, Any], notification_type: str) -> dict[str, Any]:
    """One event as the Webhook plugin renders it; must agree with
    `ops/jellyfin-webhook-template.json` (decision 365). An episode's `Provider_*` name the episode."""
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
    """Where to push. The token is never echoed back, only whether one is set."""
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
    """Fire §7.2's webhook the way the plugin does, refusing what it cannot fake: unknown items,
    bursts larger than the series, omitting a field the template never sends, and no target.
    `token`: absent sends the configured one, `""` sends no header, anything else is sent as typed.
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
    # The plugin's Generic destination sends text/plain unless the operator adds a Content-Type.
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
