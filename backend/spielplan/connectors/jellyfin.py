"""Jellyfin HTTP client (§7.1): the corpus connector's auth and fields, pinned to >= 10.9.

It writes exactly one thing, the per-user Played flag, and only with an explicit per-user token:
the admin key is unscoped and admin-equivalent (§14 risk 3).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from spielplan.core.logs import scrub

log = logging.getLogger("spielplan.jellyfin")

# ProviderIds is identity (§7.1), UserData the seen sync (§7.3), Path the library (decision 408).
# DateCreated is the file's timestamp, not the item's arrival (decision 409).
FIELDS = (
    "ProviderIds,MediaStreams,DateCreated,UserData,Genres,Overview,"
    "ProductionYear,RunTimeTicks,People,Studios,Path,OriginalTitle"
)
ITEM_TYPES = "Movie,Series"

# §7.1: "Pin Jellyfin >= 10.9" — /UserPlayedItems and /Items?userId= are the 10.9 routes.
MIN_SERVER_VERSION = (10, 9)

# Below 10.9 `/UserPlayedItems` does not exist: the only repair is an upgrade, never a re-link.
UNSUPPORTED_PLAYED_WRITE = (
    "this server is {version}; the per-user Played route (POST /UserPlayedItems) arrived in {pin}"
)

# Backstop for `all_items` (100,000 titles). Raising, not truncating: §7.2 un-owns what is missing.
MAX_PAGES = 200

# Per-client (one `/Sessions` read) episode-list cache, keyed per user since visibility is per
# user (decision 210(c)); cleared wholesale at the cap.
EPISODE_CACHE_LIMIT = 16

# Identifies this app to the server; it appears in the Jellyfin dashboard's device list and is
# what a per-user access token is issued against.
CLIENT_NAME = "Spielplan"
CLIENT_VERSION = "1.0"
DEVICE_NAME = "Spielplan"
DEVICE_ID = "spielplan-household"

TICKS_PER_SECOND = 10_000_000

# One GUID in the spellings a server accepts: 32 hex digits or dashed, braced or not; ASCII-only.
_GUID = re.compile(
    r"[0-9a-f]{32}|(?P<brace>\{)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?(brace)\})",
    re.IGNORECASE | re.ASCII,
)

# A server silently drops an `ids` value that is not a GUID, which then filters nothing; the
# limit bounds that answer to one row.
MEMBERSHIP_LIMIT = 1


def canonical_id(value: Any) -> str:
    """A Jellyfin id in the server's own spelling: a GUID as 32 lowercase hex digits, and
    anything that is not a GUID exactly as it arrived, stripped.

    `/Items` writes the "N" form and the Webhook plugin the dashed "D" form (decision 415).
    """
    text = str(value).strip()
    return uuid.UUID(text).hex if _GUID.fullmatch(text) else text


def parse_version(raw: str) -> tuple[int, ...]:
    """`"10.10.3-rc1"` -> `(10, 10, 3)`. Jellyfin ships suffixes, and only a tuple of ints can be
    compared against the pin without guessing."""
    parts: list[int] = []
    for chunk in str(raw).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_supported(raw: str) -> bool:
    """§7.1's pin, as a verdict; an unparseable version is not supported."""
    version = parse_version(raw)
    return version[:2] >= MIN_SERVER_VERSION if len(version) >= 2 else False


def _utc(when: datetime) -> datetime:
    """An instant comparable with a server's; a naive one is read as UTC."""
    return when if when.tzinfo else when.replace(tzinfo=UTC)


class JellyfinError(RuntimeError):
    """A Jellyfin call that did not succeed. `status` is None for transport failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_auth_failure(self) -> bool:
        """§7.3: 'a 401 on write -> re-link prompt.' A 403 is the same problem for the person."""
        return self.status in (401, 403)


class Outage:
    """An unreachable Jellyfin logged once, not once a sweep, and its end once with how long it
    lasted. Monotonic: only the duration is asked."""

    def __init__(self, log: logging.Logger) -> None:
        self.log = log
        self.since: float | None = None

    def down(self, exc: JellyfinError) -> None:
        if self.since is None:
            self.since = time.monotonic()
            self.log.warning("jellyfin is unreachable: %s", exc)
        else:
            self.log.debug("jellyfin is still unreachable: %s", exc)

    def up(self) -> None:
        if self.since is not None:
            minutes = (time.monotonic() - self.since) / 60
            self.log.info("jellyfin reachable again after %d minute(s)", int(minutes))
            self.since = None


def _item_rows(payload: dict, what: str) -> list[dict]:
    """The envelope's `Items`, refused unless it is a list of objects.

    Raises rather than dropping rows or defaulting to empty: a short or empty answer would be read
    as authoritative. `all_items` keeps its own refusals and does not call this.
    """
    rows = payload.get("Items")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise JellyfinError(f"{what} answered rows that are not items")
    return rows


def _under(path: str, location: str) -> bool:
    """Whether `path` is `location` or inside it: a separator must follow (either kind), case-sensitive."""
    root = location.rstrip("/\\")
    return path == root or (path.startswith(root) and path[len(root):len(root) + 1] in ("/", "\\"))


def _in_the_pick(
    row: dict, library_ids: Sequence[str], folders: Sequence[dict[str, Any]], item_id: str
) -> dict | None:
    """The row when its `Path` is in a picked library, None when only in unpicked ones, and a raise
    when no listed library holds it (decision 408). A picked location decides first."""
    path = row.get("Path")
    if not isinstance(path, str) or not path:
        raise JellyfinError(
            f"GET /Items for {item_id!a} answered no Path, so no library can be said to hold it"
        )
    picked = {canonical_id(lib) for lib in library_ids}
    elsewhere = False
    for folder in folders:
        if not any(_under(path, location) for location in folder["Locations"] if location):
            continue
        if folder.get("ItemId") and canonical_id(folder["ItemId"]) in picked:
            return row
        elsewhere = True
    if elsewhere:
        return None
    raise JellyfinError(
        f"the server's path for {item_id!a} is inside none of the libraries it lists, so whether "
        "it is picked cannot be told"
    )


@dataclass(frozen=True)
class JellyfinUser:
    id: str
    name: str
    is_admin: bool = False


@dataclass(frozen=True)
class NowPlaying:
    """One row of `/Sessions` that is actually playing something; `fraction` is position/runtime.

    For an episode, `series_id` names the show and `raw` keeps the item for ProviderId resolution
    (decision 210).
    """

    session_id: str
    jf_user_id: str
    item_id: str
    position_ticks: int
    runtime_ticks: int
    played: bool = False
    item_type: str = ""
    series_id: str | None = None
    # Out of repr and `==`: it is the whole item payload, and a frozen dataclass must hash.
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def fraction(self) -> float:
        if self.runtime_ticks <= 0:
            # No duration cannot be 90% finished.
            return 0.0
        return self.position_ticks / self.runtime_ticks


@dataclass
class JellyfinClient:
    base_url: str
    # §14.3: the key is admin-equivalent, so it stays out of every traceback's repr.
    api_key: str = field(repr=False)
    timeout: float = 15.0
    # §7.1's pin as stored, re-probed each sweep; `None` (never probed) must not block the write.
    server_version: str = ""
    server_supported: bool | None = None
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    _episodes: dict[tuple[str, str], list[dict]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    @staticmethod
    def _authorization(token: str | None = None) -> str:
        """The `MediaBrowser` header, carrying the URL-encoded token when there is one.

        Jellyfin 12 ignores `X-Emby-Token` by default, so the token must ride here too.
        """
        credential = f'Token="{quote(token, safe="")}", ' if token else ""
        return (
            f'MediaBrowser {credential}Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
            f'DeviceId="{DEVICE_ID}", Version="{CLIENT_VERSION}"'
        )

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Authorization": self._authorization(token)}
        if token:
            headers["X-Emby-Token"] = token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with self._client() as client:
                response = await client.request(
                    method, self._url(path), params=params, json=json,
                    headers=self._headers(token),
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # §3.3: callers catch JellyfinError only (`InvalidURL` is not an `HTTPError`). Token
            # scrubbed, and `from None` so a chained message cannot leak it.
            raise JellyfinError(f"{method} {path} failed: {scrub(str(exc), token)}") from None

        if 300 <= response.status_code < 400:
            # A redirect (forward-auth portal, http->https) is a failed read; the Location names the fix.
            raise JellyfinError(
                f"{method} {path} -> {response.status_code} redirected to "
                f"{response.headers.get('location', '')!a}",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise JellyfinError(
                f"{method} {path} -> {response.status_code}", status=response.status_code
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (ValueError, RecursionError):
            # `RecursionError` too: a deeply nested body is unreadable, and it is not a `ValueError`.
            return None

    # --- reads (admin key) -------------------------------------------------------------

    async def server_info(self) -> dict[str, Any]:
        """`/System/Info/Public` needs no key, which makes it the right 'test connection'
        probe: a wrong URL and a wrong key fail differently and the admin can tell which."""
        return await self._request("GET", "/System/Info/Public", token=None) or {}

    async def probe_version(self) -> tuple[str, bool | None]:
        """The server's version and §7.1's verdict, in one unauthenticated call.

        An unreported version is `None`, never `False`: a blocked tokenless endpoint must not
        refuse every Played write locally.
        """
        raw = str((await self.server_info()).get("Version") or "")
        return raw, (version_supported(raw) if raw else None)

    async def check(self) -> dict[str, Any]:
        """Probe the server and say whether it satisfies §7.1's >= 10.9 pin (`None` if unreported)."""
        info = await self.server_info()
        version = str(info.get("Version") or "")
        supported = version_supported(version) if version else None
        # The key is only exercised by an authenticated call, so make one.
        users = await self.users()
        return {
            "server_name": info.get("ServerName"),
            "version": info.get("Version"),
            "supported": supported,
            "min_version": ".".join(str(p) for p in MIN_SERVER_VERSION),
            "user_count": len(users),
        }

    async def users(self) -> list[JellyfinUser]:
        """§3.3: 'Admin view maps each app user <-> one Jellyfin user (GET /Users)'."""
        rows = await self._request("GET", "/Users", token=self.api_key) or []
        return [
            JellyfinUser(
                id=str(r["Id"]),
                name=str(r.get("Name") or r["Id"]),
                is_admin=bool((r.get("Policy") or {}).get("IsAdministrator")),
            )
            for r in rows
            if r.get("Id")
        ]

    async def _walk(self, params: dict[str, Any], *, page: int, delta: bool) -> list[dict]:
        """Every page of one `/Items` read, bounded three times: by progress, by the server's own
        count, by `MAX_PAGES`.

        Bounds count distinct ids, so a server ignoring `StartIndex` raises; a short page ends the
        walk only when the server's count agrees, else raises. `start` advances by rows returned,
        which is what `StartIndex` indexes. The `delta` read also refuses a body with no envelope and
        a count that moved between pages: unsorted paging over a live table can skip a row when one
        ahead is deleted, and the watermark must not advance past it (decision 366).
        """
        out: list[dict] = []
        ids: set[str] = set()
        claimed = 0
        start = 0
        for _ in range(MAX_PAGES):
            payload = await self._request("GET", "/Items", token=self.api_key, params={
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES,
                "Fields": FIELDS,
                "StartIndex": start,
                "Limit": page,
                "EnableTotalRecordCount": "true",
                **params,
            })
            if not delta:
                payload = payload or {}
                batch = list(payload.get("Items") or [])
            elif isinstance(payload, dict) and "Items" in payload:
                batch = _item_rows(payload, "/Items")
            else:
                raise JellyfinError(
                    "GET /Items answered no envelope -- the watermark must not advance past a read "
                    "that did not happen"
                )
            total = int(payload.get("TotalRecordCount") or 0)
            if total:
                if delta and claimed and total != claimed:
                    raise JellyfinError(
                        f"/Items counted {claimed} rows and then {total} at StartIndex {start} -- "
                        "the library changed under the walk, and the watermark must not advance "
                        "past a read that may have skipped a row"
                    )
                claimed = total
            fresh = 0
            for item in batch:
                item_id = str(item.get("Id") or "")
                if item_id:
                    if item_id in ids:
                        continue
                    ids.add(item_id)
                # An item with no `Id` is kept and counted as progress: it is a corrupt row for
                # `resolve` to refuse, not evidence about the server's paging.
                out.append(item)
                fresh += 1
            start += len(batch)
            if len(batch) >= page and not fresh:
                raise JellyfinError(
                    f"/Items returned {len(batch)} rows at StartIndex {start - len(batch)} and "
                    "not one of them was new -- the server is ignoring StartIndex"
                )
            if len(batch) < page:
                if claimed and len(out) < claimed:
                    why = ("the watermark must not advance past a read that did not finish" if delta
                           else "a short read would un-own the rest")
                    raise JellyfinError(
                        f"/Items stopped after {len(out)} of the {claimed} rows it counted -- {why}"
                    )
                return out
            if claimed and len(ids) >= claimed:
                return out
        raise JellyfinError(
            f"/Items did not end after {MAX_PAGES} pages of {page} ({len(out)} rows)"
        )

    async def all_items(self, jf_user_id: str | None, *, page: int = 500) -> list[dict]:
        """The library as `jf_user_id` sees it; with `None`, the admin's view with no `UserData`
        (§7.2's ownership read)."""
        # `is not None`: an empty id must 404, not silently become the keyless read.
        params = {} if jf_user_id is None else {"userId": jf_user_id}
        return await self._walk(params, page=page, delta=False)

    # --- §7.2: the delta read, and the boundary §6.6's library pick finally draws --------

    async def items_created_since(
        self, since: datetime, *, library_ids: Sequence[str] = (), page: int = 500
    ) -> list[dict]:
        """Everything the server has saved since `since`, inside the picked libraries (§7.2 delta).

        An empty pick is one unscoped read (decision 364); otherwise one read per library,
        deduplicated by item id. No `userId`, and no `Episode` (decision 369). `MinDateLastSaved` is
        the whole delta: `DateCreated` is the file's timestamp (decision 409).
        """
        since = _utc(since)
        scopes: list[str | None] = [str(lib) for lib in library_ids] or [None]
        out: list[dict] = []
        seen: set[str] = set()
        for scope in scopes:
            params = {"MinDateLastSaved": since.isoformat()}
            if scope is not None:
                params["ParentId"] = scope
            for item in await self._walk(params, page=page, delta=True):
                item_id = str(item.get("Id") or "")
                if item_id:
                    if item_id in seen:
                        continue
                    seen.add(item_id)
                out.append(item)
        return out

    async def libraries(self) -> list[dict[str, Any]]:
        """`GET /Library/MediaFolders`, the folders §6.6's library pick picks from.

        An enveloped answer, unlike `/Users`; anything else is a failed read, never an empty pick.
        """
        payload = await self._request("GET", "/Library/MediaFolders", token=self.api_key)
        if not isinstance(payload, dict):
            raise JellyfinError("GET /Library/MediaFolders answered no envelope")
        return _item_rows(payload, "GET /Library/MediaFolders")

    async def library_folders(self) -> list[dict[str, Any]]:
        """`GET /Library/VirtualFolders`: every library with the paths it scans (decision 408).

        A bare list; anything that is not libraries with lists of paths is a failed read.
        """
        payload = await self._request("GET", "/Library/VirtualFolders", token=self.api_key)
        if not isinstance(payload, list) or not all(
            isinstance(folder, dict)
            and isinstance(folder.get("Locations"), list)
            and all(isinstance(path, str) for path in folder["Locations"])
            for folder in payload
        ):
            raise JellyfinError("GET /Library/VirtualFolders answered something that is not libraries")
        return payload

    async def item_in_libraries(
        self,
        item_id: str,
        library_ids: Sequence[str] = (),
        *,
        folders: Sequence[dict[str, Any]] | None = None,
    ) -> dict | None:
        """The item as this server holds it, if it is inside the picked libraries (decision 364).

        Membership is read off the row's `Path` against `/Library/VirtualFolders`, since Jellyfin
        ignores `ParentId` alongside `ids` (decision 408). A failed or unplaceable read raises,
        never answers None. No `IncludeItemTypes`; rows match by `canonical_id`.
        """
        params: dict[str, Any] = {
            "Recursive": "true",
            "Fields": FIELDS,
            "ids": str(item_id),
            "Limit": MEMBERSHIP_LIMIT,
            # The count is a second query on the server's side and nothing reads it here.
            "EnableTotalRecordCount": "false",
        }
        payload = await self._request("GET", "/Items", token=self.api_key, params=params)
        if not isinstance(payload, dict):
            raise JellyfinError(f"GET /Items answered no envelope for {item_id!a}")
        rows = _item_rows(payload, f"GET /Items for {item_id!a}")
        if not all(isinstance(row.get("Id"), str) for row in rows):
            raise JellyfinError(f"GET /Items for {item_id!a} answered a row with no id")
        wanted = canonical_id(item_id)
        row = next((row for row in rows if canonical_id(row["Id"]) == wanted), None)
        if row is None or not library_ids:
            return row
        listed = folders if folders is not None else await self.library_folders()
        return _in_the_pick(row, library_ids, listed, item_id)

    async def episodes(self, series_id: str, jf_user_id: str | None = None) -> list[dict]:
        """A series' episode list, cached per client and member (decision 210(c))."""
        key = (series_id, jf_user_id or "")
        if key in self._episodes:
            return self._episodes[key]
        params: dict[str, Any] = {}
        if jf_user_id is not None:
            params["userId"] = jf_user_id
        payload = await self._request(
            "GET", f"/Shows/{series_id}/Episodes", token=self.api_key, params=params
        ) or {}
        rows = list(payload.get("Items") or [])
        if len(self._episodes) >= EPISODE_CACHE_LIMIT:
            self._episodes.clear()
        self._episodes[key] = rows
        return rows

    async def sessions(self) -> list[NowPlaying]:
        """`/Sessions`, reduced to the rows that are playing something (§7.3)."""
        rows = await self._request("GET", "/Sessions", token=self.api_key) or []
        out: list[NowPlaying] = []
        for row in rows:
            item = row.get("NowPlayingItem") or {}
            if not item.get("Id") or not row.get("UserId"):
                continue
            play_state = row.get("PlayState") or {}
            out.append(
                NowPlaying(
                    session_id=str(row.get("Id") or ""),
                    jf_user_id=str(row["UserId"]),
                    item_id=str(item["Id"]),
                    position_ticks=int(play_state.get("PositionTicks") or 0),
                    runtime_ticks=int(item.get("RunTimeTicks") or 0),
                    played=bool((item.get("UserData") or {}).get("Played")),
                    item_type=str(item.get("Type") or ""),
                    series_id=str(item["SeriesId"]) if item.get("SeriesId") else None,
                    raw=item,
                )
            )
        return out

    # --- per-user credentials ----------------------------------------------------------

    async def authenticate_by_name(self, username: str, password: str) -> tuple[str, str]:
        """§7.3's least-privilege write path: obtain that user's own access token.

        Returns (jellyfin_user_id, access_token); the password is used once and never stored.
        """
        payload = await self._request(
            "POST",
            "/Users/AuthenticateByName",
            token=None,
            json={"Username": username, "Pw": password},
        ) or {}
        token = payload.get("AccessToken")
        user_id = (payload.get("User") or {}).get("Id")
        if not token or not user_id:
            raise JellyfinError("Jellyfin accepted the login but returned no access token")
        return str(user_id), str(token)

    # --- the one write (per-user token) ------------------------------------------------

    async def set_played(self, item_id: str, jf_user_id: str, played: bool, token: str) -> None:
        """§7.3: `seen` -> `POST /UserPlayedItems/{itemId}?userId=`, `unseen` -> `DELETE`.

        Only ever with the linked user's own token, never the admin key (§14 risk 3).
        """
        if not token:
            raise JellyfinError("refusing to write Played state without a per-user token")
        refusal = self.played_write_refusal()
        if refusal:
            # Local and status-less, so not §7.3's re-link: this server has no such route (§7.1).
            raise JellyfinError(refusal)
        await self._request(
            "POST" if played else "DELETE",
            f"/UserPlayedItems/{item_id}",
            token=token,
            params={"userId": jf_user_id},
        )

    def played_write_refusal(self) -> str | None:
        """Why a Played write cannot be attempted against this server, or None (never probed)."""
        if self.server_supported is False:
            return UNSUPPORTED_PLAYED_WRITE.format(
                version=self.server_version or "below the pin",
                pin=".".join(str(p) for p in MIN_SERVER_VERSION),
            )
        return None

    async def primary_image(
        self, item_id: str, *, max_width: int = 342
    ) -> tuple[bytes, str] | None:
        """The item's Primary image, resized by the server, or None when it has none (decision 483).

        Bytes, so not through `_request`; `maxWidth` matches a 2:3 card.
        """
        path = f"/Items/{quote(str(item_id), safe='')}/Images/Primary"
        headers = {**self._headers(self.api_key), "Accept": "image/webp,image/jpeg,image/png"}
        try:
            async with self._client() as client:
                response = await client.get(
                    self._url(path), params={"maxWidth": max_width, "quality": 85}, headers=headers
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise JellyfinError(f"GET {path} failed: {scrub(str(exc), self.api_key)}") from None
        if response.status_code == 404:
            return None
        # A redirect is refused like `_request` refuses one: httpx follows none, and a proxy's
        # login page is not a poster.
        if response.status_code >= 300:
            raise JellyfinError(f"GET {path} -> {response.status_code}", status=response.status_code)
        return response.content, response.headers.get("content-type", "")


def played_of(item: dict) -> bool:
    return bool((item.get("UserData") or {}).get("Played"))


__all__ = [
    "EPISODE_CACHE_LIMIT",
    "FIELDS",
    "MAX_PAGES",
    "MIN_SERVER_VERSION",
    "UNSUPPORTED_PLAYED_WRITE",
    "JellyfinClient",
    "JellyfinError",
    "JellyfinUser",
    "NowPlaying",
    "Outage",
    "canonical_id",
    "parse_version",
    "played_of",
    "version_supported",
]
