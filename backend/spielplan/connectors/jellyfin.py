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

# §7.1 pins the version for the *write*: below 10.9 `/UserPlayedItems` does not exist, so the
# reads only degrade in fields while the app->Jellyfin direction 404s for ever. The refusal names
# the route and both versions because the only repair is an upgrade of the media server, and a
# bare "push failed" reads as an outage the household should wait out. It is deliberately not a
# re-link: §7.3's re-link prompt answers a 401, and sending someone to re-type a password cannot
# add a route to their server.
UNSUPPORTED_PLAYED_WRITE = (
    "this server is {version}; the per-user Played route (POST /UserPlayedItems) arrived in {pin}"
)

# The hard stop for `all_items`. 200 pages of the 500-item default is 100,000 titles, an order of
# magnitude past the ~11,000 this connector was sized for, so no household reaches it — and §5.3's
# serial worker tick has nothing that would rescue a loop that never ends: one non-terminating page
# loop took every other job offline with the process still healthy. It is the backstop and not the
# detector, because the fault it was written for — a server that ignores `StartIndex`, or a proxy
# caching page one — is caught one page after it starts by the no-progress raise in `all_items`.
# Raising rather than returning what it has is the load-bearing half of both, because §7.2
# re-derives ownership from this list and a truncated library would un-own the remainder.
MAX_PAGES = 200

# Per client, and the client is rebuilt per poll (`registry.make_client`), so this cache spans one
# `/Sessions` read. What it collapses inside that read is ONE MEMBER's several rows on one series —
# a person signed in on two devices, or a client that reports a row per stream — and not two
# members watching the same show: decision 210(c) reads the list `?userId=`-scoped because episode
# visibility is per-user, so the key carries the user id and two members on one series are two
# reads by construction. Measured: two `/Sessions` rows in one poll, one client, two
# `GET /Shows/jf-6/Episodes`. That is the right answer and not a miss to fix — a shared cache
# would answer one member's question with the other's visibility.
# [M4.11 review cycle 2: m411-rev2-pb-01; decision 210(c)]
#
# Capped so a longer-lived client cannot end up holding a copy of every episode list it ever saw;
# cleared wholesale at the cap, because an LRU for sixteen entries is machinery nobody asked for.
EPISODE_CACHE_LIMIT = 16

# Identifies this app to the server; it appears in the Jellyfin dashboard's device list and is
# what a per-user access token is issued against.
CLIENT_NAME = "Spielplan"
CLIENT_VERSION = "1.0"
DEVICE_NAME = "Spielplan"
DEVICE_ID = "spielplan-household"

TICKS_PER_SECOND = 10_000_000


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
    """§7.1's pin, as a verdict. A version that will not parse is *not* supported: the pin exists
    because the write route is absent below 10.9, and guessing in the server's favour would spend
    one 404 per tap of the household's seen state before anybody noticed."""
    version = parse_version(raw)
    return version[:2] >= MIN_SERVER_VERSION if len(version) >= 2 else False


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

    A session on a series plays an **episode**, and the app holds no title for an episode: the
    row's `NowPlayingItem` carries the episode's own `Id` plus a `SeriesId` for the folder. So the
    shape travels with the row — the prompt resolves `series_id or item_id` (§7.3, decision 210)
    — and `raw` keeps the item itself, because a copy this household's `title.jellyfin_id` does
    not point at still resolves through `resolve.resolve_title_id`'s ProviderIds. Without these
    three the whole Series partition sat outside §13's capture loop while capture read healthy.
    """

    session_id: str
    jf_user_id: str
    item_id: str
    position_ticks: int
    runtime_ticks: int
    played: bool = False
    item_type: str = ""
    series_id: str | None = None
    # Out of the repr and out of `==`: it is the entire item payload, it would bury a failure
    # header the way the admin key used to (§14.3), and a frozen dataclass that compares a dict
    # cannot be hashed.
    raw: dict = field(default_factory=dict, repr=False, compare=False)

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
    # §14.3: "Jellyfin API keys are unscoped and admin-equivalent — no read-only variant exists".
    # A default repr copies that key into every traceback that happens to hold a client; this
    # review watched a pytest failure header print
    # `JellyfinClient(base_url='http://jellyfin.test', api_key='fake-admin-key', timeout=15.0)`.
    # Same shape as `push/keys.py`'s signing key — no accessor, no repr — and `base_url` stays
    # visible on purpose, because it is what makes such a line worth printing at all.
    api_key: str = field(repr=False)
    timeout: float = 15.0
    # §7.1's pin as `registry.save_jellyfin` stored it, re-probed at the head of the sweep. `None`
    # means nobody has probed this server yet and must not block the write: an install whose admin
    # never pressed the test button still has to be able to push.
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
        return parse_version(str((await self.server_info()).get("Version") or ""))

    async def probe_version(self) -> tuple[str, bool | None]:
        """The server's version and §7.1's verdict, in one unauthenticated call.

        The sweep re-probes at its head rather than trusting what the admin's test button stored
        months ago: an operator upgrades the media server without ever returning to this app's
        admin page, and the stored verdict is what gates the Played write.

        A version the server did not report is `None` and never `False`. `_request` answers `{}`
        for an empty body, a body that will not parse as JSON, and a payload with no `Version`
        key — which is what a forward-auth portal or a hardening rule on `/System/Info/Public`
        produces, the endpoint being tokenless here by design and the one such guides tell people
        to block. `version_supported("")` is False, so every one of those read as "this server is
        below the pin", stored that verdict on the connector and refused every Played write
        locally, for ever, with no network attempt and a message naming no version. `None` is the
        state the design already has for it — `JellyfinConfig` calls it "nobody has probed yet"
        and `played_write_refusal` deliberately does not refuse on it, so the write is attempted
        and its real failure counted. A refusal is only ever raised on a version the app can
        quote. [review cycle 1: m411-rev-jf-04; §7.1]
        """
        raw = str((await self.server_info()).get("Version") or "")
        return raw, (version_supported(raw) if raw else None)

    async def check(self) -> dict[str, Any]:
        """Probe the server and say whether it satisfies §7.1's >= 10.9 pin.

        `supported` is `None` for an unreported version, for `probe_version`'s reason and so that
        the two routes that store the pair agree: this one is the admin's test button and stores
        what it returns.
        """
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

    async def _items_page(
        self, jf_user_id: str | None, *, start: int, limit: int
    ) -> dict[str, Any]:
        """One page with its envelope intact, `TotalRecordCount` included.

        `items` throws that envelope away and every caller was happy with a list, which is
        exactly how `all_items` came to page for ever against a count it had already asked the
        server for (§5.3 gives every job a budget).
        """
        params: dict[str, Any] = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Fields": FIELDS,
            "StartIndex": start,
            "Limit": limit,
            "EnableTotalRecordCount": "true",
        }
        # `is not None`, not truthiness: an empty id is a corrupt `app_user` row, and sending it
        # keeps the server's 404 rather than silently turning one member's read into the keyless
        # one below, where every `UserData` is absent and so every title reads as not played.
        if jf_user_id is not None:
            params["userId"] = jf_user_id
        return await self._request("GET", "/Items", token=self.api_key, params=params) or {}

    async def items(
        self, jf_user_id: str | None, *, start: int = 0, limit: int = 500
    ) -> list[dict]:
        """One page of the library. With a user id, as that user sees it — `UserData` is per-user,
        so the id in the query decides whose Played flags come back.

        With `None` the parameter is omitted and the admin key answers for the library itself,
        with no `UserData` at all. That is the shape §7.2 wants for ownership: item visibility is
        per-user, and "the household's library" is the admin's view of it rather than the union of
        two people's. The documented alternative — resolve off the first linked user's page-set
        and take the owned set as the union across users — is N reads for a worse answer, and
        would lose a title only one person can see.
        """
        payload = await self._items_page(jf_user_id, start=start, limit=limit)
        return list(payload.get("Items") or [])

    async def all_items(self, jf_user_id: str | None, *, page: int = 500) -> list[dict]:
        """Every page, bounded three times: by progress, by the server's own count, by `MAX_PAGES`.

        Every bound counts DISTINCT item ids, never rows returned, and that distinction is the
        whole of this walk. Against the very server it was hardened for — one that ignores
        `StartIndex`, or a proxy caching page one — a row count is satisfied by the same page
        arriving over and over: with an honest `TotalRecordCount` of 11,000 and a page of 500,
        `len(out) >= total` came true after 22 requests holding 500 distinct titles, so the walk
        returned a page-one library, `MAX_PAGES` was never reached, and §7.2's ownership pass
        un-owned the other ~10,500 with a fresh `owned_checked_at` and no error on any surface.
        So a full page that contributes not one new id IS the fault, and raises here — which is
        what leaves the cap below as a backstop rather than the only guard, and keeps step 1b's
        rule ("do not silently truncate") true of the input it was written about.

        The SHORT page is the other half of that rule and was the half left unconditioned. A page
        shorter than `Limit` ended the walk whatever the count on that same page said, so a server
        answering 500 rows under a `TotalRecordCount` of 11,000 — a proxy serving one page as an
        interstitial, a filter applied after the query, or a 200 whose body `_request` cannot parse
        and collapses to `{}` — handed back a page-one library with no error, and §7.2's ownership
        pass un-owned the other ~10,500 exactly as above. A short page is now an exit only when the
        server's own count agrees, and a raise otherwise. Two details make that safe. The count is
        compared against ROWS KEPT rather than distinct ids, because `TotalRecordCount` counts rows:
        one corrupt row with no `Id` — deliberately kept below — would otherwise leave every sweep
        for ever one short of the count and abort a healthy server. And it is the last count the
        server actually gave rather than this page's, so a page that collapses to `{}` cannot excuse
        itself by counting nothing, while a library that legitimately shrank under the walk still
        exits on the smaller count its own last page reports.
        [M4.11 review cycle 2: m411-rev2-resolve-01, m411-rev2-jf-01]

        `start` still advances by rows returned, because that is what `StartIndex` indexes: an
        offset into the server's own ordering. Advancing it by the deduplicated count would
        re-ask for rows already consumed the first time two pages legitimately overlap — a
        library that changed under a walk — and turn a shrinking library into a loop.
        [M4.11 review cycle 1: m411-rev-jf-01]
        """
        out: list[dict] = []
        ids: set[str] = set()
        claimed = 0
        start = 0
        for _ in range(MAX_PAGES):
            payload = await self._items_page(jf_user_id, start=start, limit=page)
            batch = list(payload.get("Items") or [])
            total = int(payload.get("TotalRecordCount") or 0)
            if total:
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
                    raise JellyfinError(
                        f"/Items stopped after {len(out)} of the {claimed} rows it counted -- "
                        "a short read would un-own the rest"
                    )
                return out
            if claimed and len(ids) >= claimed:
                return out
        raise JellyfinError(
            f"/Items did not end after {MAX_PAGES} pages of {page} ({len(out)} items)"
        )

    async def episodes(self, series_id: str, jf_user_id: str | None = None) -> list[dict]:
        """A series' episode list, cached per client (decision 210(c)).

        §7.3 arms the finish prompt at >= 90% of an *episode*, and decision 210(c) makes that a
        series finish only when the episode is the last one this server knows about — so the
        prompt needs the list, once per series and member rather than once per session row. The
        member is in the key because the URL carries `userId`: see `EPISODE_CACHE_LIMIT`.
        """
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
        refusal = self.played_write_refusal()
        if refusal:
            # Local, and with no status, so `is_auth_failure` stays False: §7.1 says this server
            # has no such route, which is not §7.3's "401 on write -> re-link prompt".
            raise JellyfinError(refusal)
        await self._request(
            "POST" if played else "DELETE",
            f"/UserPlayedItems/{item_id}",
            token=token,
            params={"userId": jf_user_id},
        )

    def played_write_refusal(self) -> str | None:
        """Why a Played write cannot be attempted against this server, or None.

        A predicate as well as an exception, so the sweep can count a refused row once without a
        round trip it knows will 404 (§7.1). `server_supported is None` — nobody probed — is not
        a refusal: the write is attempted and its real failure counted, which is the behaviour
        every install had before the verdict was stored at all.
        """
        if self.server_supported is False:
            return UNSUPPORTED_PLAYED_WRITE.format(
                version=self.server_version or "below the pin",
                pin=".".join(str(p) for p in MIN_SERVER_VERSION),
            )
        return None

    def deep_link(self, jellyfin_id: str) -> str:
        """§7.1: 'Play on Jellyfin' — deep-link to the server's own web player."""
        return f"{self.base_url.rstrip('/')}/web/#/details?id={jellyfin_id}"


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
    "parse_version",
    "played_of",
    "version_supported",
]
