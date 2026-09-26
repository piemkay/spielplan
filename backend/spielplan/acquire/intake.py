"""§7.2's intake: the Jellyfin webhook and the delta poll, both filed through `pipeline.enqueue_item`.

`UNIQUE (kind, key)` makes the two feeders' overlap a no-op. Never mints, never enqueues paid work,
never writes an ownership column (decision 362).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from spielplan.acquire import pipeline, stages
from spielplan.connectors import registry, resolve
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError, canonical_id

log = logging.getLogger("spielplan.acquire.intake")

# §7.2's "Debounce 10 min"; `not_before` has no DEFAULT so the window stays a code edit.
DEBOUNCE_SECONDS = 600

# `jellyfin_intake.state`, spelled as in the CHECK: an intake row records an event, it is not work.
PENDING = "pending"
ENQUEUED = "enqueued"
SKIPPED = "skipped"

# The template is operator-authored (decision 365), so a missing type reads as ItemAdded.
ITEM_ADDED = "ItemAdded"

# The webhook is the only way an episode id enters this app (decision 369).
EPISODE = "Episode"

# Reasons, shown verbatim: sentences naming what the operator can change.
NOT_ITEM_ADDED = "not an ItemAdded"
NO_ITEM_ID = "no item id"
# Room for any GUID spelling (32/36/38 chars). Bounds the resolved key, not just `ItemId`: an oversized
# `SeriesId` would break every later sweep's query string or the index insert.
MAX_ITEM_ID = 128
ITEM_ID_TOO_LONG = "the item id is too long to be a jellyfin id"
NO_ITEM_TYPE = "no item type"
EPISODE_WITHOUT_SERIES = "episode without series id"
# A separate sentence so the operator is sent to the right field.
SERIES_ID_TOO_LONG = "the series id is too long to be a jellyfin id"
UNREADABLE = "unreadable payload"
LIBRARY_NOT_PICKED = "library not picked"
# With no pick, an id the server will not answer for has left the library.
GONE_FROM_SERVER = "the server no longer holds this item"
# Decision 369, asked of the server's own row, since the template's `ItemType` can be wrong.
NOT_A_TITLE = "the server says this id is not a movie or a series"
# Bodies that never became a payload are still recorded (the plugin does not retry).
PAYLOAD_TOO_LARGE = "payload too large for a webhook template"
DELIVERY_INTERRUPTED = "the delivery was interrupted before its body arrived"
# The server's row could not be stored even after `_storable_item`; terminal.
UNSTORABLE_ROW = "the server's row for this item cannot be stored"

# Re-offers of placed bundle titles file below genuine adds (default priority is 100).
RE_OFFER_PRIORITY = 200

# `DateLastSaved` is stamped before the save commits, so re-read the last five minutes;
# `UNIQUE (kind, key)` absorbs the overlap (decision 409).
WATERMARK_OVERLAP = timedelta(minutes=5)

# A NUL and lone surrogates: storable in neither jsonb nor UTF-8.
_UNSTORABLE_CHARS = re.compile(r"[\x00\ud800-\udfff]")
# Errors meaning this ONE row cannot be written, not that the database is unwell.
_UNSTORABLE = (asyncpg.DataError, asyncpg.ProgramLimitExceededError, RecursionError)


# --- what one delivered body is ------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One `ItemAdded` body, read. `state` is what the intake row is written as.

    `resolved_key` is the title: an Episode's is its `SeriesId` (decision 369). `id` is 0 until written.
    """

    state: str
    reason: str = ""
    resolved_key: str | None = None
    item_id: str = ""
    item_type: str = ""
    id: int = 0


def read_event(payload: Any) -> Event:
    """Decision 365's tolerance and decision 369's resolution, as one pure function.

    Requires only `ItemId` and `ItemType`; anything unreadable is still recorded (202). Refusals follow
    the operator's likely mistake. An Episode is never keyed on itself. Ids are canonicalised (415).
    """
    if not isinstance(payload, Mapping):
        return Event(SKIPPED, UNREADABLE)
    notification = str(payload.get("NotificationType") or ITEM_ADDED).strip()
    item_id = canonical_id(payload.get("ItemId") or "")
    item_type = str(payload.get("ItemType") or "").strip()
    if notification != ITEM_ADDED:
        return Event(SKIPPED, NOT_ITEM_ADDED, item_id=item_id, item_type=item_type)
    if not item_id:
        return Event(SKIPPED, NO_ITEM_ID, item_type=item_type)
    if len(item_id) > MAX_ITEM_ID:
        return Event(SKIPPED, ITEM_ID_TOO_LONG, item_type=item_type)
    if not item_type:
        return Event(SKIPPED, NO_ITEM_TYPE, item_id=item_id)
    # Casefolded: server versions disagree on capitalisation.
    if item_type.casefold() == EPISODE.casefold():
        series = canonical_id(payload.get("SeriesId") or "")
        if not series:
            return Event(SKIPPED, EPISODE_WITHOUT_SERIES, item_id=item_id, item_type=item_type)
        if len(series) > MAX_ITEM_ID:
            # The episode id is valid here and is what the card shows.
            return Event(SKIPPED, SERIES_ID_TOO_LONG, item_id=item_id, item_type=item_type)
        return Event(PENDING, resolved_key=series, item_id=item_id, item_type=item_type)
    return Event(PENDING, resolved_key=item_id, item_id=item_id, item_type=item_type)


# `not_before` from the same `now()` as `received_at`: a fixed, not sliding, window (decision 363).
# `$1::text::jsonb`: the pool's encoder would double-encode a dict.
_RECORD = """
INSERT INTO jellyfin_intake (raw, item_id, item_type, resolved_key, not_before, state, reason)
VALUES ($1::text::jsonb, $2, $3, $4, now() + ($5::int * interval '1 second'), $6, $7)
RETURNING id
"""


# An odd number of preceding backslashes is the NUL escape; an even number is literal text.
_NUL_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")


def _storable(payload: Any) -> str | None:
    """The delivered body as text `raw jsonb` will take, or None when there is no such text.

    Refuses NaN/Infinity, NUL and lone surrogates, which would otherwise 500 after a 202.
    `ensure_ascii=False` so a NUL escape is distinguishable from a title spelling those characters.
    """
    if not isinstance(payload, Mapping):
        return None
    try:
        body = json.dumps(dict(payload), default=str, allow_nan=False, ensure_ascii=False)
        # Cheap check for lone surrogates before asyncpg meets them.
        body.encode("utf-8")
    except (TypeError, ValueError):
        return None
    return None if _NUL_ESCAPE.search(body) else body


async def record_event(conn: asyncpg.Connection, payload: Any) -> Event:
    """Write one delivered event down. The handler's ONLY synchronous work.

    Decides nothing: the plugin does not retry, so decisions belong to the sweep. The body is kept
    whole. Refusals log at INFO, accepted events at DEBUG. Refused rows still get a window (NOT NULL).
    """
    event = read_event(payload)
    body = _storable(payload)
    if body is None:
        # Replace the event too, so no column describes a body the row does not hold.
        event, body = Event(SKIPPED, UNREADABLE), "{}"
    row_id = await conn.fetchval(
        _RECORD, body, event.item_id or None, event.item_type or None, event.resolved_key,
        DEBOUNCE_SECONDS, event.state, event.reason or None,
    )
    # `%a`, never `%s`, for payload values: a newline could forge a log line.
    if event.state == PENDING:
        log.debug("jellyfin intake: %a (%a) recorded for %a",
                  event.item_id, event.item_type, event.resolved_key)
    else:
        log.info("jellyfin intake: an event was recorded and not acted on -- %s (item %a, type %a)",
                 event.reason, event.item_id or "-", event.item_type or "-")
    return replace(event, id=int(row_id))


async def record_refusal(conn: asyncpg.Connection, reason: str) -> Event:
    """Record a delivery whose body never became a payload (too large, or interrupted); `raw` is `{}`."""
    row_id = await conn.fetchval(
        _RECORD, "{}", None, None, None, DEBOUNCE_SECONDS, SKIPPED, reason,
    )
    log.info("jellyfin intake: an event was recorded and not acted on -- %s", reason)
    return Event(SKIPPED, reason, id=int(row_id))


# --- the debounce's arithmetic --------------------------------------------------------------------


@dataclass(frozen=True)
class RipeKey:
    """One title whose window has opened, and the rows the sweep decided on when it did."""

    key: str
    opens_at: datetime
    ids: tuple[int, ...]


def ripe_keys(rows: Sequence[Mapping[str, Any]], now: datetime) -> list[RipeKey]:
    """Decision 363's window, as a pure function over pending rows and one instant.

    Ripeness is over the key's whole pending set, but only rows whose own window opened are returned.
    Oldest window first. A row received after `now` (clock stepped back) counts as open.
    """

    def opened(row: Mapping[str, Any]) -> bool:
        received = row.get("received_at")
        return row["not_before"] <= now or (received is not None and received > now)

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["resolved_key"]), []).append(row)
    ripe: list[RipeKey] = []
    for key, members in groups.items():
        ids = tuple(int(row["id"]) for row in members if opened(row))
        if not ids:
            continue
        ripe.append(RipeKey(
            key=key,
            opens_at=min(row["not_before"] for row in members),
            ids=ids,
        ))
    return sorted(ripe, key=lambda entry: (entry.opens_at, entry.key))


# --- the sweep ------------------------------------------------------------------------------------


@dataclass
class SweepReport:
    """What one sweep did, for the worker's log and for a test to assert against."""

    ripe: int = 0
    # Created versus already-held; a closed key is not reopened by a re-add until a board revive.
    enqueued: int = 0
    already_queued: int = 0
    skipped: int = 0
    rows: int = 0
    # Keys left pending because the server could not be asked (decision 364): never a decision.
    deferred: int = 0
    blocked: str = ""
    # Whether the server answered anything; §6.6's "last syncs" reads this (decision 454).
    reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ripe": self.ripe, "enqueued": self.enqueued, "already_queued": self.already_queued,
            "skipped": self.skipped, "rows": self.rows, "deferred": self.deferred,
            "blocked": self.blocked, "reached": self.reached,
        }


# The pending set in the index's order, with the database's `now()`, the clock that wrote `not_before`.
_PENDING = """
SELECT id, resolved_key, received_at, not_before, now() AS swept_at
  FROM jellyfin_intake
 WHERE state = 'pending'
 ORDER BY resolved_key, not_before, id
"""

_DECIDE = """
UPDATE jellyfin_intake SET state = $2, reason = $3
 WHERE id = ANY($1::bigint[]) AND state = 'pending'
"""


async def sweep_pending(
    conn: asyncpg.Connection, client: JellyfinClient, cfg: registry.JellyfinConfig
) -> SweepReport:
    """Turn ripe intake rows into acquisition tasks: one task per title, however many events
    arrived (decisions 363, 364, 369).

    Stateless between calls. The library pick is checked by asking the server (`Path` against the picked
    folders, decision 408). Failed reads decide nothing; only a confirmed outage stops the sweep.
    Enqueue and record are one transaction; two concurrent sweeps are harmless.
    """
    report = SweepReport()
    rows = await conn.fetch(_PENDING)
    if not rows:
        return report
    due = ripe_keys(rows, rows[0]["swept_at"])
    report.ripe = len(due)
    if not due:
        return report
    try:
        live, stale = await _pick_on_the_server(client, cfg.library_ids)
        # Library locations, read once per sweep (decision 408).
        folders = await client.library_folders() if live else []
    except JellyfinError as exc:
        report.blocked = str(exc)
        report.deferred = len(due)
        log.warning(
            "jellyfin intake sweep: the library pick could not be checked against the server, so "
            "%d key(s) stay pending for the next sweep: %s", report.deferred, exc,
        )
        return report
    # A pick is checked by a read the server answered; the whole server's is checked by none.
    report.reached = bool(cfg.library_ids)
    if stale:
        report.blocked = _stale_pick(stale)
        log.warning("jellyfin intake sweep: %s", report.blocked)
        if not live:
            report.deferred = len(due)
            return report
    for index, ripe in enumerate(due):
        try:
            item = await client.item_in_libraries(ripe.key, live, folders=folders)
        except JellyfinError as exc:
            report.blocked = str(exc)
            if _about_the_key(exc) or (
                exc.status not in _SERVER_WIDE and await _server_answers(client)
            ):
                report.reached = True
                report.deferred += 1
                log.warning(
                    "jellyfin intake sweep: the server would not answer for %a, which stays "
                    "pending while the sweep goes on to the other keys: %s", ripe.key, exc,
                )
                continue
            report.deferred += len(due) - index
            log.warning(
                "jellyfin intake sweep: the server could not be read, so %d key(s) stay pending "
                "for the next sweep: %s", report.deferred, exc,
            )
            return report
        report.reached = True
        if item is None and stale:
            # Possibly in the stale library; `report.blocked` already names it.
            report.deferred += 1
            continue
        if item is None or resolve.kind_of(item) is None:
            # Nothing answered inside the pick, or the server's row is not a Movie or Series.
            reason = NOT_A_TITLE
            if item is None:
                reason = LIBRARY_NOT_PICKED if cfg.library_ids else GONE_FROM_SERVER
            report.skipped += 1
            report.rows += _rows_decided(await conn.execute(_DECIDE, list(ripe.ids), SKIPPED,
                                                            reason))
            log.info("jellyfin intake sweep: %a enqueues nothing -- %s", ripe.key, reason)
            continue
        try:
            # Outside the transaction: a Postgres error inside would abort the enqueue.
            stored = _storable_item(item)
            re_offer = await stages.re_offered_title(conn, stored)
            async with conn.transaction():
                created = await _file(conn, stored, re_offer)
                report.rows += _rows_decided(
                    await conn.execute(_DECIDE, list(ripe.ids), ENQUEUED, None)
                )
        except _UNSTORABLE as exc:
            report.skipped += 1
            report.rows += _rows_decided(await conn.execute(_DECIDE, list(ripe.ids), SKIPPED,
                                                            UNSTORABLE_ROW))
            log.warning("jellyfin intake sweep: %a enqueues nothing -- %s: %s",
                        ripe.key, UNSTORABLE_ROW, type(exc).__name__)
            continue
        if created:
            report.enqueued += 1
            log.info("jellyfin intake sweep: %d event(s) for %a are one acquisition task",
                     len(ripe.ids), ripe.key)
        else:
            report.already_queued += 1
    return report


async def _pick_on_the_server(
    client: JellyfinClient, library_ids: Sequence[str]
) -> tuple[list[str], list[str]]:
    """The admin's pick split into libraries the server still lists and ones it does not (decision 410).

    GUIDs compared canonically. An unreadable listing raises: never read as an empty (whole-server) pick.
    """
    if not library_ids:
        return [], []
    listed = {
        canonical_id(folder["Id"]) for folder in await client.libraries() if folder.get("Id")
    }
    live = [lib for lib in library_ids if canonical_id(lib) in listed]
    stale = [lib for lib in library_ids if canonical_id(lib) not in listed]
    return live, stale


def _stale_pick(stale: Sequence[str]) -> str:
    """The one sentence both feeders say about decision 410's state, naming the lever."""
    return (
        f"the library pick names {len(stale)} librar{'y' if len(stale) == 1 else 'ies'} this "
        f"server no longer lists ({', '.join(ascii(lib) for lib in stale)}); what the other "
        "picked libraries hold is still filed, and everything else waits until the pick is saved "
        "again from the libraries the server lists"
    )


async def _server_answers(client: JellyfinClient) -> bool:
    """Whether the server is up at all, via the one read that needs no key; an empty answer is no answer.
    """
    try:
        return bool(await client.server_info())
    except JellyfinError:
        return False


def _storable_item(value: Any) -> Any:
    """A server row as text a jsonb column will take, with its identity untouched.

    NUL and lone surrogates become U+FFFD (so a scrubbed provider id fails validation), non-finite
    numbers become null.
    """
    if isinstance(value, str):
        return _UNSTORABLE_CHARS.sub("�", value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {_storable_item(key): _storable_item(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_storable_item(inner) for inner in value]
    return value


async def _file(conn: asyncpg.Connection, item: dict[str, Any], re_offer: int | None) -> bool:
    """`pipeline.enqueue_item`, at `RE_OFFER_PRIORITY` for a title stage 1 will close unwalked."""
    if re_offer is None:
        return await pipeline.enqueue_item(conn, item)
    return await pipeline.enqueue_item(conn, item, priority=RE_OFFER_PRIORITY)


# Statuses about the server rather than the key: 401 (key refused), 408, 429. A 403 or 400 on one
# key's read comes from something in front of Jellyfin, about that request.
_SERVER_WIDE = frozenset({401, 408, 429})


def _about_the_key(exc: JellyfinError) -> bool:
    """Whether this refusal is about the one id that was asked about (decision 364).

    A key-specific 4xx must not stop the sweep, or one bad id wedges intake forever (oldest first).
    """
    return exc.status is not None and 400 <= exc.status < 500 and exc.status not in _SERVER_WIDE


def _rows_decided(tag: str) -> int:
    """asyncpg hands back `UPDATE <n>`; the count is what the report publishes as `rows`."""
    return int(tag.rpartition(" ")[2] or 0)


# --- the fallback ---------------------------------------------------------------------------------


@dataclass
class DeltaReport:
    """What one delta poll read and filed. `since` is the watermark it read from and `until` the
    one it wrote, which is `WATERMARK_OVERLAP` before the instant its read began.
    """

    since: datetime | None = None
    until: datetime | None = None
    read: int = 0
    enqueued: int = 0
    already_queued: int = 0
    # Counted, not raised: raising would freeze the watermark forever.
    unkeyable: int = 0
    # Non-titles the server returned (a proxy dropped `IncludeItemTypes`); counted so it is visible.
    not_a_title: int = 0
    # Counted beside `unkeyable`, for the same reason.
    unstorable: int = 0
    # The stored watermark was ahead of this poll's start, so it read from the floor.
    watermark_in_future: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "read": self.read, "enqueued": self.enqueued,
            "already_queued": self.already_queued, "unkeyable": self.unkeyable,
            "not_a_title": self.not_a_title,
            "unstorable": self.unstorable, "watermark_in_future": self.watermark_in_future,
        }


async def poll_delta(
    conn: asyncpg.Connection, client: JellyfinClient, cfg: registry.JellyfinConfig
) -> DeltaReport:
    """§7.2's fallback, the fifteen-minute delta poll, filed into the same queue as the webhook's sweep
    (decisions 364, 366, 409).

    The watermark advances only after a complete read, only for the server it was read from, and a
    future watermark reads from the floor. Failed reads raise. Scoped to live picked libraries; a stale
    pick raises after filing the rest. Episodes are refused here too (decision 369).
    """
    since = await registry.delta_since(conn, cfg)
    # From Postgres, before the read.
    started = await conn.fetchval("SELECT now()")
    ahead = since > started
    if ahead:
        log.warning("jellyfin delta poll: the stored watermark %s is later than now (%s), so this "
                    "poll reads from the install's floor instead", since.isoformat(),
                    started.isoformat())
        since = await registry.delta_since(conn, replace(cfg, delta_watermark=None))
    live, stale = await _pick_on_the_server(client, cfg.library_ids)
    if stale and not live:
        raise JellyfinError(_stale_pick(stale))
    items = await client.items_created_since(since, library_ids=live)
    watermark = started - WATERMARK_OVERLAP
    report = DeltaReport(since=since, until=watermark, read=len(items), watermark_in_future=ahead)
    for item in items:
        if resolve.kind_of(item) is None:
            report.not_a_title += 1
            continue
        try:
            stored = _storable_item(item)
            created = await _file(conn, stored, await stages.re_offered_title(conn, stored))
        except ValueError:
            report.unkeyable += 1
            log.info("jellyfin delta poll: an item with no id of any kind was not enqueued")
            continue
        except _UNSTORABLE as exc:
            report.unstorable += 1
            log.warning("jellyfin delta poll: %s (item %a): %s", UNSTORABLE_ROW,
                        str(item.get("Id") or "")[:64], type(exc).__name__)
            continue
        if created:
            report.enqueued += 1
        else:
            report.already_queued += 1
    if stale:
        raise JellyfinError(_stale_pick(stale))
    # Last: a watermark written before the enqueues is an add lost to a crash.
    await registry.save_jellyfin(conn, delta_watermark=watermark, watermark_origin=cfg.url)
    if report.enqueued:
        log.info("jellyfin delta poll: %d title(s) saved since %s are now queued",
                 report.enqueued, since.isoformat())
    return report


# --- what §6.6's card reads -----------------------------------------------------------------------

# Reasons meaning the delivery was not an add at all; every other row was an add.
_NOT_AN_ADD = (NOT_ITEM_ADDED, UNREADABLE, PAYLOAD_TOO_LARGE, DELIVERY_INTERRUPTED)

# Read on demand when an admin opens the card.
_WEBHOOK_FACTS = """
SELECT max(received_at) FILTER (WHERE coalesce(reason, '') <> ALL($1::text[])) AS last_item_added_at,
       max(received_at) AS last_delivery_at,
       count(*) FILTER (WHERE received_at > now() - interval '7 days') AS deliveries_7d
  FROM jellyfin_intake
"""
_NEWEST_REFUSAL = """
SELECT received_at, reason FROM jellyfin_intake
 WHERE state = $1 ORDER BY received_at DESC, id DESC LIMIT 1
"""
# A run that closed ok with no report asked no server, so it counts as neither.
_NEWEST_RUN = (
    "SELECT started_at, ok, detail FROM job_run WHERE name = $1 AND (ok IS NOT TRUE OR detail IS NOT"
    " NULL) ORDER BY started_at DESC LIMIT 1"
)
_NEWEST_OK = (
    "SELECT finished_at FROM job_run WHERE name = $1 AND ok AND detail IS NOT NULL"
    " ORDER BY started_at DESC LIMIT 1"
)


async def trigger_status(
    conn: asyncpg.Connection, *, poll_job: str, watermark: datetime | None
) -> dict[str, Any]:
    """§6.6's "webhook status", as facts rather than a mode (decision 455).

    `webhook`: last ItemAdded, last delivery, seven-day count, newest refusal. `delta_poll`: the
    watermark, the newest run and the newest success. Nulls, never a 500, when unconfigured.
    """
    facts = await conn.fetchrow(_WEBHOOK_FACTS, list(_NOT_AN_ADD))
    refusal = await conn.fetchrow(_NEWEST_REFUSAL, SKIPPED)
    newest = await conn.fetchrow(_NEWEST_RUN, poll_job)
    succeeded = await conn.fetchval(_NEWEST_OK, poll_job)
    # A failed run's detail is `{"error": ...}`; an unconfigured poll has none.
    detail = newest["detail"] if newest else None
    filed = detail.get("enqueued") if isinstance(detail, dict) else None
    return {
        "webhook": {
            "last_item_added_at": facts["last_item_added_at"],
            "last_delivery_at": facts["last_delivery_at"],
            "deliveries_7d": int(facts["deliveries_7d"]),
            "last_refusal": (
                {"at": refusal["received_at"], "reason": refusal["reason"]} if refusal else None
            ),
        },
        "delta_poll": {
            "watermark": watermark,
            "last_run_at": newest["started_at"] if newest else None,
            "last_run_ok": newest["ok"] if newest else None,
            "last_ok_at": succeeded,
            "last_filed": filed if isinstance(filed, int) else None,
        },
    }
