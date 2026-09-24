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
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("spielplan.jellyfin")

# The corpus connector's proven set. ProviderIds is the identity payload (§7.1), UserData
# carries Played/PlaybackPositionTicks for the seen sync (§7.3), and Path is what places an item
# in a library (decision 408). DateCreated was to drive the §7.2 delta poll and does not: it is
# the file's timestamp, not the item's arrival (decision 409).
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

# One GUID, in the spellings a Jellyfin server accepts for it: 32 hex digits, or the dashed form,
# braced or not, in either case. ASCII-only, so a case-insensitive match can never fold a
# non-ASCII letter into a hex digit.
_GUID = re.compile(
    r"[0-9a-f]{32}|(?P<brace>\{)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?(brace)\})",
    re.IGNORECASE | re.ASCII,
)

# The membership read's page. It asks about ONE id, so an honest answer is at most one row, and
# the bound exists for the answer that is not honest: a server DROPS a piece of `ids` it cannot
# parse as a GUID (`CommaDelimitedCollectionModelBinder` logs the FormatException at debug and
# leaves the value out), and an `ids` emptied that way filters nothing -- so without a `Limit`,
# `ids=<junk>&Recursive=true` with `FIELDS` and no `IncludeItemTypes` was every item on the server,
# People and MediaStreams included, for one key and on every sweep; on a large library that read
# outran the client's timeout, and a timeout has no status. Bounded, the same id costs one row
# that does not match it, and the sweep decides it rather than wedging on it.
# [M5.2 review cycle 3: M52-C3-EVENTS-04, M52-C3-SWEEP-05, M52-C3-LIB-05]
MEMBERSHIP_LIMIT = 1


def canonical_id(value: Any) -> str:
    """A Jellyfin id in the server's own spelling: a GUID as 32 lowercase hex digits, and
    anything that is not a GUID exactly as it arrived, stripped.

    ONE GUID HAS TWO SPELLINGS IN THIS APP'S INPUTS, and a comparison that cannot see that drops
    every real add. `/Items` writes every `Id` in the "N" form (`JsonGuidConverter` formats with
    `ToString("N")`), while the Webhook plugin assigns the raw `Guid` into its template's data and
    Handlebars renders it through `ToString()`, which is the dashed "D" form --
    jellyfin-plugin-webhook#204 is an operator finding exactly that. The server's binder parses
    either, so the membership read FOUND the item and this app threw it away. Canonicalised at the
    boundary (`intake.read_event`) and on both sides of the one comparison (`item_in_libraries`),
    the debounce group, the `ids=` the sweep sends and the key the task is filed under are all one
    spelling, and it is the spelling the delta poll files under too, so the two feeders still
    collide on `UNIQUE (kind, key)`. [M5.2 review cycle 3: M52-C3-EVENTS-01; decision 415]

    AN ID THAT IS NOT A GUID IS NOT REFUSED HERE, and that is decision 415's reading rather than a
    tolerance: the test double's ids are not GUIDs, and every check of M5.2's exit criterion runs
    against it. What makes such an id harmless on a real server is `MEMBERSHIP_LIMIT`.
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
    """§7.1's pin, as a verdict. A version that will not parse is *not* supported: the pin exists
    because the write route is absent below 10.9, and guessing in the server's favour would spend
    one 404 per tap of the household's seen state before anybody noticed."""
    version = parse_version(raw)
    return version[:2] >= MIN_SERVER_VERSION if len(version) >= 2 else False


def _utc(when: datetime) -> datetime:
    """An instant that can be compared with a server's. A naive one is read as UTC, because a
    stamp that omitted its zone can have meant nothing else here: §7.2's predicate compares two
    instants, and `datetime` refuses to compare an aware one with a naive one at all."""
    return when if when.tzinfo else when.replace(tzinfo=UTC)


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


def _item_rows(payload: dict, what: str) -> list[dict]:
    """The envelope's `Items`, refused when it is not a list of objects.

    Review cycle 1 made a body that is not an ENVELOPE a failed read at three of this client's
    reads, because a bare list reached `.get` on a `list` and left the admin route as an
    `AttributeError` -- a 500, worse than the 502 that route refuses -- and because a forward-auth
    portal's 200 read as an authoritative empty answer. The guard stopped one level short.
    `{"Items": "maintenance"}` and `{"Items": ["a", "b"]}` are envelopes by that test, and the very
    next line in each caller calls `.get` on a `str`: `libraries` answered with the CHARACTERS of
    the string, and the other two raised `AttributeError` -- which is not a `JellyfinError`, so it
    escapes `sweep_pending`'s handler and the admin route's `except` alike, which is the same
    escape `intake.MAX_ITEM_ID` bounds an id against.

    RAISING RATHER THAN DROPPING THE ROW. A body carrying something that is not an item is not
    this server's answer to this question, and every caller here already prefers a read that
    raised to one that quietly returned less: decision 364 leaves an intake row pending on a
    failed membership read rather than deciding it, and decision 366 leaves the watermark where it
    was. Dropped instead, a page of strings would be a SHORT page, which the delta walk reads as
    the end of the library.

    `all_items` is deliberately not a caller: it is the ownership pass's input,
    `_falsify_ownership` depends on its exact refusals, and this milestone is an addition that
    neither relaxes them nor adds to them (plan C1). [review cycle 2: m52-c2-lib-02]

    AND AN ENVELOPE WITH NO LIST OF ITEMS IS NOT AN EMPTY ONE. `payload.get("Items") or []` made
    `{}`, `{"error": ...}` and `{"Items": null}` a successful empty answer -- a gateway in
    maintenance mode was then an authoritative "this id is nowhere", which the sweep records
    terminally, and `{"Items": null}` passed the delta page's `"Items" in payload` test and ended
    the walk as a completed empty read that advanced the watermark. A truthy `Items` that cannot
    be iterated (`5`, `true`) raised `TypeError`, which is not a `JellyfinError` and escaped every
    caller's handler -- the class the paragraph above says it closed. Only a list is an answer.
    [M5.2 review cycle 3: M52-C3-LIB-06]
    """
    rows = payload.get("Items")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise JellyfinError(f"{what} answered rows that are not items")
    return rows


def _under(path: str, location: str) -> bool:
    """Whether `path` is `location` or inside it, as a library holds what it scans.

    A separator has to follow the location, because `/media/films` and `/media/films-home` are two
    libraries on an ordinary server and a bare prefix test puts the second one's files in the
    first. Either separator, because the server spells paths the way the OS it runs on does.
    Case-sensitive, as the server's own `PhysicalLocations.Contains(item.Path)` is
    (`LibraryController.TranslateParentItem`). [decision 408]
    """
    root = location.rstrip("/\\")
    return path == root or (path.startswith(root) and path[len(root):len(root) + 1] in ("/", "\\"))


def _in_the_pick(
    row: dict, library_ids: Sequence[str], folders: Sequence[dict[str, Any]], item_id: str
) -> dict | None:
    """The row when its `Path` is inside a picked library, None when it is inside only libraries
    the admin did not pick, and a raise when the server's own answers cannot place it at all
    (decision 408; `JellyfinClient.item_in_libraries` argues each of the three).

    A picked location decides before an unpicked one, so an item under two nested libraries is
    filed when either is picked -- the server lists it in both. A location that is the empty
    string names no directory and would otherwise contain every path, so it holds nothing.
    """
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
    def _authorization(token: str | None = None) -> str:
        """The `MediaBrowser` header, carrying the token itself when there is one.

        `X-Emby-Token` alone -- §7.1's ported auth -- is anonymous on Jellyfin 12. The server reads
        `Token=` out of this header first on every release from 10.9 to 12.1, and `X-Emby-Token`
        only after that and only while `EnableLegacyAuthorization` is on
        (`Jellyfin.Server.Implementations/Security/AuthorizationContext.cs`); 12.0 made that
        default off and migrates upgraded installs to off. So every read this app makes, and §7.3's
        Played write, answered 401 on the current release while `version_supported("12.1")` said
        yes. `X-Emby-Token` is still sent beside it, which is §7.1's text and costs nothing.
        The value is URL-encoded because the server URL-decodes it (`AuthorizationContext.GetParts`).
        [M5.2 review cycle 4: M52-C4-AUTH-01, M52-C4-TTA-03]
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
            # §3.3: the app must work when Jellyfin is down. Callers catch JellyfinError; an
            # httpx exception escaping into a route would make that promise depend on which
            # library this module happens to use. `InvalidURL` is named because it is NOT an
            # `HTTPError`: a saved URL with a letter in its port raised it out of the admin PUT's
            # probe as a 500, after the save had committed. [M5.2 review cycle 4: M52-C4-TOKEN-01]
            raise JellyfinError(f"{method} {path} failed: {exc}") from exc

        if 300 <= response.status_code < 400:
            # httpx follows no redirect by default, so a forward-auth portal's 302 or a proxy's
            # http->https 301 reached the JSON parse below and became None: the intake paths
            # recorded "answered no envelope" with no status, and `check()` read `users()` as an
            # empty list and told the admin the connection worked. Jellyfin sends one itself when
            # the saved URL omits its BaseUrl. The Location is what names the lever.
            # [M5.2 review cycle 4: M52-C4-REDIRECT-01; decisions 364, 366]
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
            # `RecursionError` because the decoder's depth limit is one more way a body is
            # unreadable, and it is a `RuntimeError` rather than a `ValueError`: an answer nested a
            # hundred thousand deep escaped every caller's `except JellyfinError` and wedged the
            # intake sweep at its oldest key, where an HTML page in the same place is the failed
            # read decision 364 leaves pending. `api/events.py` catches the same pair for the same
            # reason on the way in. [M5.2 review cycle 3, green pass]
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

    # --- §7.2: the delta read, and the boundary §6.6's library pick finally draws --------

    async def _created_page(
        self, since: datetime, *, library_id: str | None, start: int, limit: int
    ) -> dict[str, Any]:
        """One page of the delta read, envelope intact, for `_created_walk`'s bounds.

        No `userId`, for the reason `items` gives: an add belongs to the household's library and
        item visibility is per-user, so the admin key's view is the one §7.2 is about.
        `ITEM_TYPES` is unchanged and still excludes `Episode` (decision 369) -- the webhook is
        the only direction an episode id enters this app, resolved through its `SeriesId` before
        anything else touches it, and a delta read that returned episodes would produce exactly
        the twelve jobs §7.2 forbids.

        `MinDateLastSaved` IS THE DELTA, and the whole of it (decision 409). It selects every row
        the server has saved since the watermark: every add, because the scan saves an item the
        moment it first resolves it, and also an item whose artwork was refreshed or whose metadata
        was corrected. This walk used to narrow that superset again on `DateCreated > since`, and
        `DateCreated` is not the instant an item entered the library: by default it is the FILE's
        timestamp (`UseFileCreationTimeForDateAdded`, `ResolverHelper.SetDateCreated`), or an NFO's
        `<dateadded>`. A film copied in with its mtime kept, an archive unpacked with its dates, or
        any add first saved by the twelve-hourly scheduled scan came back from the server and was
        thrown away here, uncounted, with the watermark then advanced past it for good. The
        superset is filed as it stands: `UNIQUE (kind, key)` absorbs what was filed before, and a
        re-saved title the bundle supplied and the app placed exits at stage 1 below every genuine
        add (decision 411). [M5.2 review cycle 4: M52-C4-REST-02, M52-C4-TTA-02]

        AN ANSWER THAT IS NOT AN ENVELOPE IS A FAILED READ, and the trailing `or {}` this line
        used to end with made it a successful empty one. `_request` answers None for an empty body
        and for a body it cannot parse -- a forward-auth portal, a proxy's maintenance page, a
        base URL pointing at the wrong vhost -- and on the FIRST page there is no
        `TotalRecordCount` yet, so `_created_walk`'s short-page bound is inert and the walk
        returns nothing as a completed read. `poll_delta` then advances the watermark (decision
        366) over instants nothing looked at, and those adds are what that walk's own docstring
        calls "an add nobody ever looks for again". The `Items` key is what tells a quiet
        household's honest `{"Items": [], "TotalRecordCount": 0}` from a 200 that answered nothing
        at all. [review cycle 1: m52-rev-lib-03]
        """
        params: dict[str, Any] = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Fields": FIELDS,
            "StartIndex": start,
            "Limit": limit,
            "EnableTotalRecordCount": "true",
            "MinDateLastSaved": _utc(since).isoformat(),
        }
        if library_id is not None:
            params["ParentId"] = library_id
        payload = await self._request("GET", "/Items", token=self.api_key, params=params)
        if not isinstance(payload, dict) or "Items" not in payload:
            raise JellyfinError(
                "GET /Items answered no envelope -- the watermark must not advance past a read "
                "that did not happen"
            )
        return payload

    async def _created_walk(
        self, since: datetime, *, library_id: str | None, page: int
    ) -> list[dict]:
        """One scope's pages, bounded the way `all_items` is bounded and for a second reason.

        Beside that walk rather than through it: `all_items` is the ownership pass's input and
        `_falsify_ownership` depends on its exact refusals, so this read is an addition that
        cannot relax them by sharing them. The bounds are the same three because the fault they
        were hardened against -- a server ignoring `StartIndex`, a proxy serving page one, a page
        truncated under an honest `TotalRecordCount` -- is a property of the server rather than of
        the question asked. What differs is the damage: there, a short read un-owns the remainder;
        here, it is silent, because the watermark advances only on a completed read (decision 366)
        and the next poll asks about the instants AFTER the page that was dropped. An add nobody
        ever looks for again is worse than a poll that raised, so this walk raises too.

        AND A FOURTH, WHICH `all_items` DOES NOT HAVE: A COUNT THAT MOVED BETWEEN PAGES. The walk
        sends no `SortBy`, so 10.10 pages with no ORDER BY at all and 10.11 by the non-unique
        `SortName`, as LIMIT/OFFSET over a live table. A row deleted ahead of the current offset
        between two pages moves every later row one place left, so one present row is never
        served -- and the server's count drops by the same one, so the short-page bound above is
        satisfied and the walk returned short, with the watermark then advanced past the skipped
        row for good. A count that differs from the first page's is a walk that did not finish:
        it raises, and the next poll re-reads from the same watermark, the polarity decision 366
        already gives a failed read. It cannot see a delete and an add that cancel inside one walk;
        nothing short of a second, unpaged read could. `all_items` keeps its M4.11 exit on a
        library that shrank under it, because plan C1 leaves its refusals as they are.
        [M5.2 review cycle 4: M52-C4-REST-03]
        """
        out: list[dict] = []
        ids: set[str] = set()
        claimed = 0
        start = 0
        for _ in range(MAX_PAGES):
            payload = await self._created_page(
                since, library_id=library_id, start=start, limit=page
            )
            batch = _item_rows(payload, "/Items")
            total = int(payload.get("TotalRecordCount") or 0)
            if total:
                if claimed and total != claimed:
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
                # An item with no `Id` is kept and counted as progress, for `all_items`'s reason:
                # it is a corrupt row for `resolve` to refuse, not evidence about the paging.
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
                        "the watermark must not advance past a read that did not finish"
                    )
                return out
            if claimed and len(ids) >= claimed:
                return out
        raise JellyfinError(
            f"/Items did not end after {MAX_PAGES} pages of {page} ({len(out)} rows)"
        )

    async def items_created_since(
        self, since: datetime, *, library_ids: Sequence[str] = (), page: int = 500
    ) -> list[dict]:
        """Everything the server has saved since `since`, inside the picked libraries -- §7.2's
        second intake path, the fifteen-minute delta poll that answers when the Webhook plugin is
        absent, misconfigured or was never installed. The name is §7.2's `DateCreated`, which is
        not what the read filters on (decision 409; `_created_page` argues why).

        An EMPTY pick is the whole server and issues ONE unscoped read (decision 364). That is the
        state of every install in existence -- nothing has ever written `library_ids` -- so a
        filter whose allow-list is empty would make this poll return nothing, everywhere, for
        ever. A non-empty pick is one scoped read per library, because `ParentId` takes one id
        and scopes a read that names no `ids`, which this one never does (decision 408).

        Deduplicated across those reads by item id: a folder can sit under two collections, and
        the caller's contract is one row per item rather than one per library that holds it. A row
        with no `Id` at all is kept and not deduplicated, for `all_items`'s reason -- it is a
        corrupt row for `resolve` to refuse, not evidence about this server's paging.
        """
        since = _utc(since)
        scopes: list[str | None] = [str(lib) for lib in library_ids] or [None]
        out: list[dict] = []
        seen: set[str] = set()
        for scope in scopes:
            for item in await self._created_walk(since, library_id=scope, page=page):
                item_id = str(item.get("Id") or "")
                if item_id:
                    if item_id in seen:
                        continue
                    seen.add(item_id)
                out.append(item)
        return out

    async def libraries(self) -> list[dict[str, Any]]:
        """`GET /Library/MediaFolders` -- the folders §6.6's library pick picks from.

        Jellyfin answers this one inside an envelope (`{"Items": [...], "TotalRecordCount": n}`)
        while `/Users` above is a bare list, so a client that reads the two the same way is wrong
        about one of them. M5.7 renders these; decision 364 is what gives the stored pick its
        first reader, and a pick over libraries nobody can see listed is a pick nobody can make.

        AND AN ANSWER THAT IS NOT AN ENVELOPE IS A FAILED READ. `api/admin.jellyfin_libraries`
        argues that "the empty list is a meaningful answer in its own right" and reports `ok:
        false` with the server's own words for the other case -- but it only ever saw that case
        through `JellyfinError`, so a 200 carrying a portal's HTML reached the card as `ok: true`
        with no libraries, which is the shape of a deliberate choice nobody made. A body that
        parses as a bare list reached `.get` on a `list` and left the route as an `AttributeError`
        -- a 500, which is worse than the 502 that route refuses. [review cycle 1: m52-rev-lib-04]
        """
        payload = await self._request("GET", "/Library/MediaFolders", token=self.api_key)
        if not isinstance(payload, dict):
            raise JellyfinError("GET /Library/MediaFolders answered no envelope")
        return _item_rows(payload, "GET /Library/MediaFolders")

    async def library_folders(self) -> list[dict[str, Any]]:
        """`GET /Library/VirtualFolders` -- every library with the paths it scans, which is the one
        fact this server gives the API key that places an item in a library (decision 408).

        A BARE list, unlike `/Library/MediaFolders` above (`LibraryStructureController.
        GetVirtualFolders` returns `IEnumerable<VirtualFolderInfo>`): `ItemId` is the library's id
        in the "N" spelling -- the id `/Library/MediaFolders` answers as `Id` -- and `Locations` the
        paths it scans. An answer that is not a list of libraries each carrying a list of paths is
        a failed read, for `_item_rows`' reason: read as a server with fewer libraries, it would
        decide membership against half a map.
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
        """The item as this server holds it, if it is inside the picked libraries -- decision
        364's membership test, decided the way decision 408 says, over the read that carries the
        item's `ProviderIds`.

        Two answers on purpose. The Webhook plugin's template is operator-authored (decision 365),
        so an event carries whatever the operator left in it and never a library it can be trusted
        about: a boundary held with data an operator can forget is not a boundary. And the same row
        is what §8 stage 1 needs, since it mints only on a provider id (decision 323) and the
        published template names none of the plugin's `Provider_<key>` fields.
        [M5.2 review cycle 4: M52-C4-WH-02]

        MEMBERSHIP IS READ OFF THE ROW'S `Path`, NOT ASKED OF `ParentId` (decision 408). This read
        used to send `ids=<key>&ParentId=<lib>` once per picked library and trust the server to
        narrow the answer, and no Jellyfin does: `ItemsController.GetItems` resolves the folder and
        then sets `query.Parent = null`, which empties `ParentId`, before `Folder.GetItems` hands
        any query with ids to `LibraryManager.GetItemsResult` -- whose only library scoping needs a
        non-empty `ParentId`. Read at v10.9.11, v10.10.7, v10.11.0 and v12.1, identically. So every
        live picked library answered for every id, and an add in a library the admin deselected was
        filed under the first one; the double scoped by `ParentId` anyway and every test agreed.
        The server's own rule for which library a folder belongs to is a path rule --
        `LibraryController.TranslateParentItem` asks which library's `PhysicalLocations` contain it
        -- and `/Library/VirtualFolders` is where those locations are exposed, so the row's `Path`
        (in `FIELDS`) is placed against them. `folders` is that listing when the caller has already
        read it; a sweep reads it once rather than once per key.
        [M5.2 review cycle 4: M52-C4-IDS-01, M52-C4-WH-01]

        An EMPTY pick is the whole server and asks nothing but the id (decision 364).

        A read that FAILS raises rather than answering None. "This id is in no library the admin
        picked" and "nobody could ask" are the same silence and opposite facts, and decision 364
        leaves an intake row pending for the next sweep on the second -- an answer of None here
        would instead record the household's own add as deliberately unwanted. "Fails" is not
        only what `_request` raises on: it answers None for an empty body and for a body it
        cannot parse, and the trailing `or {}` this read used to end with turned a forward-auth
        portal's 200 into the authoritative negative this paragraph exists to refuse -- terminal,
        because the sweep writes `skipped` and never reads that row again. A body that parses as
        a bare list is the same fault wearing an `AttributeError`. [review cycle 1: m52-rev-lib-02]
        A row whose path sits inside NO library the server lists is that silence too, and raises:
        it cannot happen to a file a library scanned, and it does happen when the server's
        `PathSubstitutions` rewrite the `Path` a row is served with (`DtoService.GetMappedPath`)
        while `Locations` stay as configured -- so such an install's adds wait, named, rather than
        being filed as a library nobody picked.

        No `IncludeItemTypes`, because the question is about THIS id: narrowing to Movie,Series
        would answer "not picked" for an episode id that reached here, which is decision 369's
        failure wearing decision 364's reason. The sweep is what makes that load-bearing rather
        than decorative -- it refuses a row whose `Type` this app does not acquire, and it can
        only refuse a row the server was willing to answer with, so an id the read declined to
        describe would be filed as a library the admin did not pick. `ITEM_TYPES` is unchanged in
        every read this app makes of its own accord. [review cycle 1: M52-C1-MEMBER-03]

        THE ROW IS MATCHED BY GUID AND NOT BY STRING (`canonical_id`). The Webhook plugin renders an
        id dashed and this server answers it undashed, so an exact comparison threw away the row
        the server had found, and every real `ItemAdded` was filed terminally as gone from the
        server -- or, under a pick, as a library nobody picked. A row whose `Id` is not a string
        at all is not an answer about this id or any other, and is a failed read like the portal
        page above. [M5.2 review cycle 3: M52-C3-EVENTS-01, M52-C3-LIB-01, M52-C3-LIB-06]
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
    "canonical_id",
    "parse_version",
    "played_of",
    "version_supported",
]
