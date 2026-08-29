"""Jellyfin HTTP client. Spec v2.1 §7.1.

Ported from the corpus connector's auth and field list: `X-Emby-Token`, `/Users`, and the
proven FIELDS set including ProviderIds, MediaStreams, DateCreated and UserData. Two
deliberate departures from that connector, both named in §7.1:

  * Jellyfin is pinned to >= 10.9 and reads use the modern `/Items?userId=` route. The corpus
    connector's `/Users/{id}/Items` is the legacy alias and still answers, but the modern
    route is where the server's own clients live and where new fields appear.
  * The corpus connector is read-only. This one writes exactly one thing — the per-user Played
    flag — and §14 risk 3 is why that restraint has to live in code: a Jellyfin API key is
    unscoped and admin-equivalent, with no read-only variant to fall back on. Every write here
    therefore takes an explicit `token` argument and there is no code path that reaches for the
    admin key when one is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("spielplan.jellyfin")

# The corpus connector's proven set. ProviderIds is the identity payload (§7.1), UserData
# carries Played/PlaybackPositionTicks for the seen sync (§7.3), DateCreated drives the §7.2
# delta poll at M5.
FIELDS = (
    "ProviderIds,MediaStreams,DateCreated,UserData,Genres,Overview,"
    "ProductionYear,RunTimeTicks,People,Studios,Path,OriginalTitle"
)
ITEM_TYPES = "Movie,Series"

# §7.1: "Pin Jellyfin >= 10.9" — /UserPlayedItems and /Items?userId= are the 10.9 routes.
MIN_SERVER_VERSION = (10, 9)

# Identifies this app to the server; it appears in the Jellyfin dashboard's device list and is
# what a per-user access token is issued against.
CLIENT_NAME = "Spielplan"
CLIENT_VERSION = "1.0"
DEVICE_NAME = "Spielplan"
DEVICE_ID = "spielplan-household"

TICKS_PER_SECOND = 10_000_000


class JellyfinError(RuntimeError):
    """A Jellyfin call that did not succeed. `status` is None for transport failures."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_auth_failure(self) -> bool:
        """§7.3: 'a 401 on write -> re-link prompt.' 403 lands here too: an expired per-user
        token and a token that lost its rights are the same problem for the person holding it.
        """
        return self.status in (401, 403)


@dataclass(frozen=True)
class JellyfinUser:
    id: str
    name: str
    is_admin: bool = False


@dataclass(frozen=True)
class NowPlaying:
    """One row of `/Sessions` that is actually playing something.

    `fraction` is position/runtime; §7.3 arms the finish prompt at >= 0.9 of it.
    """

    session_id: str
    jf_user_id: str
    item_id: str
    position_ticks: int
    runtime_ticks: int
    played: bool = False

    @property
    def fraction(self) -> float:
        if self.runtime_ticks <= 0:
            # A live stream or an item with no duration cannot be 90% finished. Reporting 0
            # rather than dividing by zero keeps it out of the prompt population honestly.
            return 0.0
        return self.position_ticks / self.runtime_ticks


@dataclass
class JellyfinClient:
    base_url: str
    api_key: str
    timeout: float = 15.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    @staticmethod
    def _authorization() -> str:
        return (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
            f'DeviceId="{DEVICE_ID}", Version="{CLIENT_VERSION}"'
        )

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Authorization": self._authorization()}
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
        except httpx.HTTPError as exc:
            # §3.3: the app must work when Jellyfin is down. Callers catch JellyfinError; an
            # httpx exception escaping into a route would make that promise depend on which
            # library this module happens to use.
            raise JellyfinError(f"{method} {path} failed: {exc}") from exc

        if response.status_code >= 400:
            raise JellyfinError(
                f"{method} {path} -> {response.status_code}", status=response.status_code
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # --- reads (admin key) -------------------------------------------------------------

    async def server_info(self) -> dict[str, Any]:
        """`/System/Info/Public` needs no key, which makes it the right 'test connection'
        probe: a wrong URL and a wrong key fail differently and the admin can tell which."""
        return await self._request("GET", "/System/Info/Public", token=None) or {}

    async def version_tuple(self) -> tuple[int, ...]:
        raw = str((await self.server_info()).get("Version") or "")
        parts: list[int] = []
        for chunk in raw.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            if not digits:
                break
            parts.append(int(digits))
        return tuple(parts)

    async def check(self) -> dict[str, Any]:
        """Probe the server and say whether it satisfies §7.1's >= 10.9 pin."""
        info = await self.server_info()
        version = await self.version_tuple()
        supported = version[:2] >= MIN_SERVER_VERSION if len(version) >= 2 else False
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

    async def items(self, jf_user_id: str, *, start: int = 0, limit: int = 500) -> list[dict]:
        """One page of the library as that user sees it — `UserData` is per-user, so the id
        in the query decides whose Played flags come back."""
        payload = await self._request(
            "GET",
            "/Items",
            token=self.api_key,
            params={
                "userId": jf_user_id,
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES,
                "Fields": FIELDS,
                "StartIndex": start,
                "Limit": limit,
                "EnableTotalRecordCount": "true",
            },
        )
        return list((payload or {}).get("Items") or [])

    async def all_items(self, jf_user_id: str, *, page: int = 500) -> list[dict]:
        out: list[dict] = []
        while True:
            batch = await self.items(jf_user_id, start=len(out), limit=page)
            out.extend(batch)
            if len(batch) < page:
                return out

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
                )
            )
        return out

    # --- per-user credentials ----------------------------------------------------------

    async def authenticate_by_name(self, username: str, password: str) -> tuple[str, str]:
        """§7.3's least-privilege write path: obtain that user's own access token.

        Returns (jellyfin_user_id, access_token). The password is used once, here, and is
        never stored — the token that comes back is what gets sealed into `connector_config`.
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

        `token` is required and is the linked user's own access token, never the admin key: a
        user token can only set its own state, which is the only enforcement available given
        that the admin key has no read-only variant (§14 risk 3).
        """
        if not token:
            raise JellyfinError("refusing to write Played state without a per-user token")
        await self._request(
            "POST" if played else "DELETE",
            f"/UserPlayedItems/{item_id}",
            token=token,
            params={"userId": jf_user_id},
        )

    def deep_link(self, jellyfin_id: str) -> str:
        """§7.1: 'Play on Jellyfin' — deep-link to the server's own web player."""
        return f"{self.base_url.rstrip('/')}/web/#/details?id={jellyfin_id}"


def played_of(item: dict) -> bool:
    return bool((item.get("UserData") or {}).get("Played"))


__all__ = [
    "FIELDS",
    "MIN_SERVER_VERSION",
    "JellyfinClient",
    "JellyfinError",
    "JellyfinUser",
    "NowPlaying",
    "played_of",
]
