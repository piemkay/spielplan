"""§7.2's intake: what the household's Jellyfin told us, and the one job each add becomes.

Spec v2.1 §7.2 ("**every Jellyfin add triggers acquisition**": `POST /events/jellyfin`
(ItemAdded), token-authed; a "15-minute delta poll on what the server saved since the last poll"
as fallback; "Debounce 10 min; series acquire per-show, not per-episode"), §5.3 (every job
durable), §6.6 (the library pick); decisions 363, 364, 365, 369, 409; `docs/milestones/M5.2-plan.md`
§1 and §4.

ONE ENQUEUE PATH WITH TWO FEEDERS, which is the plan's own argument for why the webhook and the
poll are one milestone rather than two: *"'Fallback' means the delta poll must produce the same
enqueue the webhook would have, for the same title, without duplicating it"* (§1). Both feeders
end at `pipeline.enqueue_item`, so the key is spelled once (`pipeline.key_for_item`), the payload
is shaped once, and `UNIQUE (kind, key)` makes the overlap between them a no-op rather than a
second crawl. A second enqueue spelling here would defeat exactly the property that makes the
fallback safe to run while the webhook is also working.

WHAT THIS MODULE MAY NOT DO, and each of these is a seam M5.1 built that M5.2 is not allowed to
work around:

  * It does not MINT. Stage 1 mints only on a provider id (decision 323) and an event is never
    where one is read: the plugin hands the template each of the item's as `Provider_<key>`, but
    the published template names none (decision 365), and an episode's name the episode rather
    than the show decision 369 files. So an event's whole journey to a title runs through the task
    this module files. `item_in_libraries` is read for its `ProviderIds` precisely so stage 1 has
    something to work with; if the server offers none, the walk parks with "no provider id" and
    this module is not the place to invent one. [M5.2 review cycle 4: M52-C4-WH-02]
  * It never enqueues PAID work. `queue.lease` filters on `paid` and the driver refuses an
    implemented paid stage with no spend cap (decision 347), so everything filed here takes
    `enqueue_item`'s default of `paid=False` and nothing may pass that flag.
  * It writes NO ownership column, ever (decision 362). A `DateCreated >` read is an add detector
    by construction and never observes an absence, and one `ItemAdded` says nothing about what the
    library no longer holds; §7.2's third bullet is discharged by the full sweep alone, where
    M4.11 built it and gated it twice. `test_seen_sync.py::test_no_second_path_learns_to_write_
    the_ownership_column` holds that from this side.
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

# §7.2's "Debounce 10 min", as the one number the window is made of. A module constant and not a
# column default: `0025_jellyfin_intake.sql` deliberately gives `not_before` no DEFAULT so that
# changing the window's length stays a code edit rather than a migration.
DEBOUNCE_SECONDS = 600

# `jellyfin_intake.state`, spelled here as well as in the CHECK because a writer reaching for a
# neighbouring table's vocabulary - `acquisition_task`'s `done`, `acquisition_job`'s `queued` - is
# the mistake that constraint exists to refuse. An intake row is not work: it is the record of an
# event, and the only questions asked of it are whether the sweep will act on it and why it did
# not.
PENDING = "pending"
ENQUEUED = "enqueued"
SKIPPED = "skipped"

# The one notification §7.2 names. The Webhook plugin sends a template per event type and an
# operator can point several of them at one URL, so the field is read rather than assumed - but
# its ABSENCE is read as this, because the template is operator-authored (decision 365) and a
# handler that refused every body without it would refuse the plainest working configuration.
ITEM_ADDED = "ItemAdded"

# Jellyfin's type for the thing a burst is made of. `ITEM_TYPES = "Movie,Series"` excludes it from
# every read this app makes of its own accord, so the webhook is the ONE direction an episode id
# enters this app at all (decision 369).
EPISODE = "Episode"

# Why a row was not enqueued, in the app's own words: §6.6's card renders these at M5.7, and until
# then they are what an operator reads in the table when a webhook they configured did nothing.
# Sentences rather than codes, for `stages.NO_PROVIDER_ID`'s reason - a reason shown verbatim to a
# person has to name the thing that person can change.
NOT_ITEM_ADDED = "not an ItemAdded"
NO_ITEM_ID = "no item id"
# A Jellyfin id is a GUID -- 32 characters as `/Items` spells it, 36 dashed as the Webhook plugin
# renders it, 38 braced -- so this is room for any spelling a real server or its plugin produces and
# a refusal for everything else. The bound exists because the value does not stay in this schema:
# `item_in_libraries` puts it in a query string, where an id past a request-line limit is a 414
# from Jellyfin or from any proxy in front of it -- and past httpx's own 64KB URL limit it is an
# `InvalidURL`, which escaped the sweep's handling entirely until `JellyfinClient._request` learned
# to wrap it (M52-C4-TOKEN-01). The payload is operator-authored and nothing else bounds it
# (decision 365).
#
# IT IS A BOUND ON THE RESOLVED KEY AND NOT ON ONE FIELD. For a Movie the two are the same string,
# which is why checking `ItemId` looked like checking the key; for an Episode decision 369 makes
# the key the payload's `SeriesId`, and that is the value `sweep_pending` hands to
# `item_in_libraries` and the value the partial btree `jellyfin_intake_ripe (resolved_key,
# not_before)` indexes. Bounded on `ItemId` alone the guard covered only the arm where it is
# redundant: an oversized `SeriesId` was recorded `pending` and then raised the `InvalidURL` above
# out of every later sweep -- and because the ripe set is swept oldest-first, out of every sweep
# after that too, which is M52-INTAKE-02's permanent wedge re-entered through the arm its fix did
# not cover. Incompressible past the index tuple's own limit it never even reached the sweep: the
# INSERT raised and `app.py`'s `PostgresError` handler answered 500 over a delivery nothing had
# recorded, which is both halves of decision 365 broken at once. [review cycle 2: M52-C2-EVENTS-01]
MAX_ITEM_ID = 128
ITEM_ID_TOO_LONG = "the item id is too long to be a jellyfin id"
NO_ITEM_TYPE = "no item type"
EPISODE_WITHOUT_SERIES = "episode without series id"
# The bound above, held against the field the Episode arm actually resolves the key from. Two
# sentences rather than one because a reason is read by a person: "the item id is too long" would
# send an operator to the field their template got right.
SERIES_ID_TOO_LONG = "the series id is too long to be a jellyfin id"
UNREADABLE = "unreadable payload"
LIBRARY_NOT_PICKED = "library not picked"
# The empty-pick twin of the line above, and a different fact wearing the same silence: with no
# pick to fail, an id the server will not answer for is an item that has left the library between
# the event and this sweep (or an operator's template pointing at another server). Saying "library
# not picked" there would tell an admin to change a setting that is not set.
GONE_FROM_SERVER = "the server no longer holds this item"
# Decision 369, asked of the server instead of of the template. `ItemType` is operator-authored
# like every other field, so `read_event`'s Episode arm is a boundary drawn with data an operator
# can forget -- decision 364's own words about the library pick, and the same answer: the row the
# membership read returns carries the server's own `Type`, and it is in hand at the instant the
# enqueue is decided. A hardcoded `Movie` over a season of episode ids, or a `Season` an operator
# ticked in the plugin, keys a task on something §8 can never acquire; `resolve.kind_of` is where
# this app's vocabulary for that already lives (it answers None for everything but Movie and
# Series), so nothing new is spelled here. [review cycle 1: M52-C1-EPISODE-01]
NOT_A_TITLE = "the server says this id is not a movie or a series"
# Two deliveries whose body never became a payload at all, recorded rather than answered with an
# error for decision 365's reason: the plugin does not retry, and a row is the only way an
# operator learns that something arrived. The published template renders under two kilobytes, so
# the first is a body no template produces; the second is a sender whose connection dropped, or
# whose body did not finish arriving inside `api/events.BODY_DEADLINE_S`.
# [review cycle 3: M52-C3-EVENTS-02, M52-C3-EVENTS-03]
PAYLOAD_TOO_LARGE = "payload too large for a webhook template"
DELIVERY_INTERRUPTED = "the delivery was interrupted before its body arrived"
# The server's own row for an item, which this schema refused even after `_storable_item` mended
# what it can: an `Id` past the btree's tuple limit, nesting past the encoder's own. Terminal,
# because nothing about it changes on a retry, and named, because the sweep that met it used to
# raise out of the job on it and head every sweep after. [review cycle 3: M52-C3-STATE-01]
UNSTORABLE_ROW = "the server's row for this item cannot be stored"

# `queue.lease` orders by `(priority, id)` and every task defaults to 100, so a re-offer of a title
# the bundle supplied and the app has already placed -- which stage 1 closes without walking
# (decision 411) -- is filed BELOW every genuine add. A re-scan re-stamps a whole library and the
# first poll of an upgraded install reads everything added since the install; at `DRAIN_LIMIT`
# tasks a drain, filed at the default they put a film the household had just added behind hours
# or days of titles it has had for years. [review cycle 3: m52-c3-own-02]
RE_OFFER_PRIORITY = 200

# How far behind the instant its read began a delta poll writes its watermark. The server stamps
# `DateLastSaved` BEFORE the row it saves is visible to a read: 10.10's `RunMetadataSavers` sets it
# to `DateTime.UtcNow` and then awaits the item's image conversion, dimensions and blurhash before
# `SaveItems` commits, and 10.11 re-stamps after the savers and still commits later. A row stamped
# just before a poll began and committed after its read was missed by that poll, and every later
# one asked `>=` the watermark and excluded it for good. Re-reading the last five minutes costs
# what `UNIQUE (kind, key)` absorbs for free -- the second mechanism decision 366 already names --
# and bounds decision 404's app-ahead skew to the same margin.
# [M5.2 review cycle 4: M52-C4-REST-04; decision 409]
WATERMARK_OVERLAP = timedelta(minutes=5)

# What a row the server answered with may carry and a jsonb column may not: a NUL, which JSON can
# only spell as the escape Postgres refuses, and a lone surrogate, which has no UTF-8 encoding.
_UNSTORABLE_CHARS = re.compile(r"[\x00\ud800-\udfff]")
# The errors that mean "this ONE row cannot be written", as against the database being unwell:
# a value the type refused, a key past the index's tuple limit, and a structure nested past the
# encoder's recursion limit on the way to `json.dumps`.
_UNSTORABLE = (asyncpg.DataError, asyncpg.ProgramLimitExceededError, RecursionError)


# --- what one delivered body is ------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One `ItemAdded` body, read. `state` is what the intake row is written as.

    `resolved_key` is the TITLE and not the event (decision 369): for an `Episode` it is the
    payload's `SeriesId`, and the episode's own id is kept in `item_id` so the twelve events of a
    season stay individually visible while the sweep groups them into one show.

    `id` is the intake row, once `record_event` has written it, and 0 before - which is what lets
    the reading be a pure function and the write a two-line wrapper around it.
    """

    state: str
    reason: str = ""
    resolved_key: str | None = None
    item_id: str = ""
    item_type: str = ""
    id: int = 0


def read_event(payload: Any) -> Event:
    """Decision 365's tolerance and decision 369's resolution, as one pure function.

    The Webhook plugin sends an OPERATOR-CONFIGURED TEMPLATE, so no clause fixes the fields and
    anything this app requires is a field an operator can forget. Two are required - `ItemId` and
    `ItemType` - because they are the whole of what the sweep can act on, and everything else is
    advisory. A body that carries neither, or that cannot be read as an object at all, is still
    RECORDED, with its reason, and the route still answers 202: a webhook the app rejects is one
    the operator never learns about, and the plugin does not retry.

    THE ORDER OF THE REFUSALS IS THE ORDER OF THE OPERATOR'S MISTAKES. `NotificationType` is asked
    first because an operator who pointed the Playback templates at this URL has configured the
    wrong template, and telling them a field is missing would send them to fix the wrong thing;
    its absence is `ItemAdded` for the reason above. The Episode arm is last because it is the one
    refusal that is about the library rather than about the template.

    AN EPISODE IS NEVER KEYED ON ITSELF, not even as a fallback. An `Episode` with no `SeriesId`
    is recorded with that reason and enqueues nothing, because the fallback that looks harmless
    here - key it on the episode - produces exactly the twelve jobs §7.2 forbids, one per episode
    of the season a scan just added, and the household would be billed twelve times for one show.

    EVERY ID IS READ IN THE SERVER'S SPELLING (`jellyfin.canonical_id`, decision 415). The plugin
    renders a GUID dashed and `/Items` answers it undashed, so a key kept as delivered was a key
    the membership read could never match: every real add was filed as gone from the server, and
    two spellings of one show were two debounce groups. [review cycle 3: M52-C3-EVENTS-01]
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
    # Casefolded, for `resolve.provider_ids`' argument about the same server's other strings:
    # different Jellyfin versions and plugins disagree about capitalisation, and this is the one
    # place in the tree that compared a server-supplied type string exactly. A body spelling it
    # `episode` took the arm below and was keyed on the episode - decision 369's own failure, off
    # by one letter. [review cycle 1: M52-C1-EPISODE-01]
    if item_type.casefold() == EPISODE.casefold():
        series = canonical_id(payload.get("SeriesId") or "")
        if not series:
            return Event(SKIPPED, EPISODE_WITHOUT_SERIES, item_id=item_id, item_type=item_type)
        if len(series) > MAX_ITEM_ID:
            # `item_id` is KEPT here and dropped at the twin above, because there the over-long
            # value is the item id itself and here it is a legitimate episode id that has already
            # passed that bound -- and it is what §6.6's card shows the operator when it asks
            # which delivery this was.
            return Event(SKIPPED, SERIES_ID_TOO_LONG, item_id=item_id, item_type=item_type)
        return Event(PENDING, resolved_key=series, item_id=item_id, item_type=item_type)
    return Event(PENDING, resolved_key=item_id, item_id=item_id, item_type=item_type)


# `received_at` defaults to `now()` and `not_before` is computed from the same `now()` in the same
# statement, so the window is a pure function of THIS row's own arrival and the two instants can
# never drift apart by a round trip. That is what makes it FIXED rather than sliding (decision
# 363): a later event for the same key writes its own row with its own window and extends nothing,
# so a library scan that runs for an hour cannot postpone acquisition for the season it started
# with.
#
# `$1::text::jsonb` and not `$1::jsonb`, for `queue.enqueue`'s reason: `db/pool.py` registers
# `json.dumps` as the jsonb encoder, so a parameter typed jsonb is encoded twice and stored as a
# JSON *string* that every later reader iterates character by character.
_RECORD = """
INSERT INTO jellyfin_intake (raw, item_id, item_type, resolved_key, not_before, state, reason)
VALUES ($1::text::jsonb, $2, $3, $4, now() + ($5::int * interval '1 second'), $6, $7)
RETURNING id
"""


# A NUL, as `json.dumps` writes one: the escape is always emitted, even under
# `ensure_ascii=False`, because JSON has no other spelling for a control character. Preceded by an
# ODD number of backslashes it IS that escape; preceded by an even number it is a title that spells
# those six characters itself, and refusing the row for that would be refusing a real add.
_NUL_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")


def _storable(payload: Any) -> str | None:
    """The delivered body as text `raw jsonb` will take, or None when there is no such text.

    A DUMP IS NOT THE SAME AS A STORABLE DUMP, and every way the two differ is a body
    `json.loads` accepted a moment earlier -- so the route answered 202, and then the INSERT
    raised, and `app.py`'s `asyncpg.PostgresError` handler answered 500 with a traceback over a
    delivery nothing had recorded. That is both halves of decision 365 broken at once.

      * `NaN`, `Infinity` and `-Infinity`, which the stdlib decoder reads as bare tokens and out
        of an overflowing literal such as `1e999`, re-emit as tokens no JSON parser reads.
        `allow_nan=False` makes that a refusal here rather than a 22P02 out of the `::jsonb` cast
        -- the same decoder tolerance `stages._runtime_min` already catches one module over.
      * A NUL is a character Postgres holds nowhere: not in a jsonb string, not in the `item_id`
        text parameter beside it.
      * A lone surrogate has no UTF-8 encoding at all, so it raises inside asyncpg rather than in
        Postgres, which is a 500 the PostgresError handler would not even have named.

    `ensure_ascii=False` is what makes the middle one answerable at all. Every OTHER codepoint is
    then written out as itself, so the only escapes left in the dump are the ones JSON requires -
    and `_NUL_ESCAPE` is the one of those this schema refuses. With the default, every non-ASCII
    character is an escape too and the six that spell a NUL cannot be told from a title that
    spells those same six characters, which jsonb holds perfectly well.

    None rather than a repaired body, because a body this app cannot store is a body §6.6's card
    cannot show: `record_event` records it the way it records a body that would not parse, which
    is the answer decision 365 already gives for "arrived, unreadable, not acted on".
    [review cycle 1: m52-rev-events-02, M52-INTAKE-01]
    """
    if not isinstance(payload, Mapping):
        return None
    try:
        body = json.dumps(dict(payload), default=str, allow_nan=False, ensure_ascii=False)
        # Not the value that is bound - asyncpg encodes it again - but the one question this
        # process can ask cheaply that Postgres would otherwise answer with a 500.
        body.encode("utf-8")
    except (TypeError, ValueError):
        return None
    return None if _NUL_ESCAPE.search(body) else body


async def record_event(conn: asyncpg.Connection, payload: Any) -> Event:
    """Write one delivered event down. The handler's ONLY synchronous work (plan A4).

    It decides nothing. Not whether the library is picked, not whether the item resolves, not
    whether the household already owns the title - all of that is the sweep's, because the handler
    answers a server that will NOT retry: the Webhook plugin fires and forgets, so a decision
    taken on the request's thread is a decision that is lost when it is slow or when it raises,
    and the add it was about is lost with it. What this writes is the record; what the sweep does
    with it is a question that can be asked again on the next tick.

    The body is kept whole so that an operator whose template is wrong can be shown what actually
    arrived rather than told 400. `default=str` on the dump, for `pipeline.write_board`'s reason:
    the value is whatever a server sent, and losing the type of one field is a far smaller harm
    than losing the row - and losing the row here means answering 500 to a delivery, which is the
    one answer decision 365 forbids. `_storable` is the rest of that sentence, finished: three
    shapes the decoder accepts and this schema cannot hold, each of which answered exactly the
    500 this paragraph was written to refuse.

    A refusal is logged at INFO and an accepted event at DEBUG, which is the ratio of how often
    each is worth reading: a library scan delivers a season in a burst, and twelve INFO lines
    saying a webhook worked would bury the one line saying that a template is misconfigured.

    A REFUSED ROW IS GIVEN A WINDOW TOO, because `not_before` is NOT NULL and a refusal still
    arrived at an instant worth recording. Nothing reads it: the sweep's one read is over pending
    rows, and a row the handler has already decided is a record rather than work.
    """
    event = read_event(payload)
    body = _storable(payload)
    if body is None:
        # `_storable` argues which bodies these are and why they are not repaired. The event is
        # replaced rather than kept, so that the columns beside `raw` cannot describe a body the
        # row does not hold -- and so that a NUL in `ItemId` itself never reaches a text
        # parameter, which fails one error code away from the cast and for the same reason.
        event, body = Event(SKIPPED, UNREADABLE), "{}"
    row_id = await conn.fetchval(
        _RECORD, body, event.item_id or None, event.item_type or None, event.resolved_key,
        DEBOUNCE_SECONDS, event.state, event.reason or None,
    )
    # `%a` and never `%s` for anything a delivery carried: the payload is the only source of these
    # values, the log format escapes nothing, and a token holder's `ItemType` carrying a newline
    # wrote a second, fully formed log line of its own choosing -- a forged `ERROR` in the stream
    # §6.6 and the M4.7 runbook read as the operator's evidence. `ascii()` also keeps a non-ASCII
    # id off a cp1252 console. [review cycle 3: M52-C3-EVENTS-06]
    if event.state == PENDING:
        log.debug("jellyfin intake: %a (%a) recorded for %a",
                  event.item_id, event.item_type, event.resolved_key)
    else:
        log.info("jellyfin intake: an event was recorded and not acted on -- %s (item %a, type %a)",
                 event.reason, event.item_id or "-", event.item_type or "-")
    return replace(event, id=int(row_id))


async def record_refusal(conn: asyncpg.Connection, reason: str) -> Event:
    """Write down a delivery whose body never became a payload: one larger than any template, or
    one whose sender left before it finished (`PAYLOAD_TOO_LARGE`, `DELIVERY_INTERRUPTED`).

    `record_event`'s row with nothing in it but the reason, and for its reason: decision 365
    records what it cannot act on, because the plugin does not retry and §6.6's card is the only
    place an operator will ever learn that a delivery arrived and broke. `raw` is `{}` rather than
    the bytes, for `api/events.py`'s argument about an unparseable body -- and here the bytes are
    exactly what was refused. [review cycle 3: M52-C3-EVENTS-02, M52-C3-EVENTS-03]
    """
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

    IN PYTHON AND NOT IN SQL, deliberately. The same collapse is expressible as
    `GROUP BY resolved_key HAVING min(not_before) <= now()`, and `jellyfin_intake_ripe
    (resolved_key, not_before) WHERE state = 'pending'` is the index for exactly that read - which
    it still serves, because the rows below arrive in that index's own order. What SQL cannot be
    is RUN WITHOUT A DATABASE, and §7.2's debounce is arithmetic: twelve episodes of one series
    inside the window are one show, two series inside one window are two, and an event after the
    window closed is a second window rather than an extension of the first. Writing it here means
    those three sentences are tested at the layer they are stated in; writing it in SQL and
    keeping a Python twin for the tests would be two implementations of one clause, which is the
    failure `pipeline.key_for_item` argues against one module over.

    RIPENESS IS MEASURED OVER THE KEY'S WHOLE PENDING SET, and only the rows whose OWN window has
    opened are handed back. The two are different questions and the difference is the second
    window: a thirteenth episode arriving a minute after the first twelve were swept belongs to a
    window of its own, and a sweep that consumed it here would mark as enqueued a row whose ten
    minutes had not run. It costs one extra membership read for that key and one no-op enqueue,
    and it buys a table in which `state` means what it says.

    Oldest window first, so a sweep that runs out of its budget has spent it on the events that
    have waited longest rather than on whichever key sorted first.

    `resolved_key` is read without a guard: `jellyfin_intake_pending_rows_have_a_key` makes a
    keyless pending row unwritable, so a defence here would be a branch no database can enter.

    A ROW RECEIVED AFTER `now` IS OPEN. The window is measured on the clock that wrote it, and a
    database clock that ran ahead and was stepped back by NTP leaves rows whose `received_at` is
    in the future, which waited out the whole step before this function would look at them.
    Nothing honest arrives in the future -- the sweep reads `now()` in the same statement as the
    rows -- so such a row is not a window still collecting its burst, and ripening it early costs
    nothing the debounce protects: `UNIQUE (kind, key)` still makes a late sibling a no-op. Read
    off `received_at` itself rather than inferred from `not_before`, so a row that carries no
    arrival is judged by its window alone. [review cycle 3: M52-C3-STATE-07]
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
    # Tasks the queue actually created, and the keys whose task it already held. Counted apart
    # because a healthy install running both feeders produces the second constantly - that is the
    # fallback overlapping the webhook, which is `UNIQUE (kind, key)` working - while the first
    # going to zero on an install that receives events is the failure worth a line.
    #
    # It also counts the state `pipeline.TITLE_GONE` names in its operator advice: a key the queue
    # has already CLOSED is a key `ON CONFLICT DO NOTHING` will not reopen, so an item added,
    # removed and added again inside one install's life is recorded here and acquires nothing
    # until decision 330's revive action arrives at M5.6. Disclosed rather than worked around -
    # the lever is the board's, and a second enqueue spelling invented here to get past the
    # conflict is exactly what `key_for_item` exists to prevent.
    enqueued: int = 0
    already_queued: int = 0
    skipped: int = 0
    rows: int = 0
    # Ripe keys this sweep left pending because the server could not be asked (decision 364), and
    # the reason it could not. Never a decision: "this id is in no library the admin picked" and
    # "nobody could ask" are the same silence and opposite facts, and recording the second as the
    # first would file the household's own add as deliberately unwanted.
    deferred: int = 0
    blocked: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ripe": self.ripe, "enqueued": self.enqueued, "already_queued": self.already_queued,
            "skipped": self.skipped, "rows": self.rows, "deferred": self.deferred,
            "blocked": self.blocked,
        }


# One read, in the index's own order: the pending SET, which is what `jellyfin_intake_ripe` is
# partial over. `now()` travels with the rows so that the window is measured on the clock that
# WROTE `not_before` - the worker and the backend are separate containers (`queue.py`'s named
# change 1 argues the same point about the lease), and a worker whose clock runs fast would
# otherwise sweep a window that had not closed.
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
    arrived. §7.2's "Debounce 10 min; series acquire per-show, not per-episode"; decisions 363,
    364, 369.

    THE PENDING SET IS A TABLE AND THIS FUNCTION HOLDS NOTHING. §5.3 files every job as durable,
    and a library scan delivers a season in a burst: a pending set living in the worker's memory
    would drop the whole scan to a restart inside the ten minutes, and the household would never
    learn that it had. So a sweep is a read, some decisions and some writes, with no state between
    calls - which is also what makes "restarted at minute five" a test that can be written at all.

    THE LIBRARY PICK IS APPLIED HERE AND NOT IN THE HANDLER (plan B3, decision 364), and it is
    applied by ASKING THE SERVER. An empty pick - the state of every install in existence, because
    nothing has ever written `library_ids` - means the whole server and the read is an ordinary id
    lookup. A non-empty pick is the acquisition boundary, and membership is a fact only Jellyfin
    holds: the webhook payload is operator-authored (decision 365), so a boundary drawn with a
    field an operator can forget is not a boundary. The server states it as the row's `Path` and
    the picked libraries' locations, and never as a `ParentId` beside `ids`, which it drops
    (decision 408). The same read carries the item's `ProviderIds`, which is what §8 stage 1 needs
    and what no delivery of the published template carries: it names none of the plugin's
    `Provider_<key>` fields (decision 365). [M5.2 review cycle 4: M52-C4-WH-02]

    A READ THAT FAILED DECIDES NOTHING, AND AN OUTAGE ALONE STOPS THE SWEEP. `item_in_libraries`
    raises on a failed read instead of answering None for exactly this reason. When what it says
    is about the SERVER, the next key would fail the same way, and spending the job's budget on a
    hundred timeouts is worse than leaving the rows for a tick with a server that answers.
    `_about_the_key` is where the other kind is told apart - a 4xx the one id in `ids=` provoked
    says nothing about the next key, and stopping there wedged the whole intake behind it for
    ever, because the ripe set is swept oldest-first. Either way nothing is lost and nothing is
    decided: the rows stay pending, and their window has already closed.

    THE ENQUEUE AND THE RECORD OF IT ARE ONE TRANSACTION, in that order. `queue.enqueue` is
    `ON CONFLICT (kind, key) DO NOTHING`, so re-enqueueing after a crash is free, while a row
    marked `enqueued` beside a task that was never filed is an add nobody will ever look for
    again - the asymmetry that decides which way round a crash may land.

    TWO SWEEPS AT ONCE COST NOTHING AND NO LOCK IS TAKEN. `queue.lease`'s own docstring calls two
    loops "the ordinary state during a rolling restart", and that is what this sweep is handed:
    both read the same ripe rows, both ask the server, one `enqueue_item` creates the task and the
    other returns False, and the `state = 'pending'` in `_DECIDE` means the second UPDATE matches
    nothing. The counts in two reports then disagree about who did it; the table and the queue do
    not, which is the part §7.2 is a claim about.

    AN OUTAGE IS ASKED ABOUT, NOT INFERRED FROM ONE KEY. A failure with no status or a 5xx was read
    as the server being down, and stopped the sweep with every key behind it deferred -- so an id
    the server fails on every time (one item whose DTO will not build, one read that outruns the
    timeout) headed every later sweep, oldest first, and nothing added after it was ever filed by
    this path: M52-INTAKE-02's wedge through the arm its fix did not touch. `_server_answers`
    costs one unauthenticated read and settles it; a server that answers it was failing on this
    key alone. [review cycle 3: M52-C3-SWEEP-02]

    A PICKED LIBRARY THE SERVER NO LONGER LISTS IS A FAULT IN THE PICK (decision 410), read once
    per sweep off `/Library/MediaFolders` rather than learned as a 400 per key. What a live picked
    library holds is filed; what none of them holds stays pending, because it may be in the
    library the stale id used to name; and a pick none of whose libraries survive reads nothing at
    all, because an empty scope is the WHOLE SERVER and the boundary may not widen by itself.
    [review cycle 3: M52-C3-LIB-02, M52-C3-SWEEP-04]

    NO ONE SERVER ROW CAN STOP THE REST. The enqueue stores the row `/Items` answered, and a NUL
    in an overview is a character jsonb holds nowhere: the INSERT raised out of the job, the key
    was the oldest window, and the webhook intake was dead until someone edited that item in
    Jellyfin. `_storable_item` mends what it can without changing what the row identifies, and
    what it cannot mend is decided `skipped` with `UNSTORABLE_ROW`. [review cycle 3:
    M52-C3-STATE-01, M52-C3-SWEEP-03]
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
        # Where the libraries are on the server's disk, read once for every key below: membership
        # is the row's `Path` placed against these (decision 408). A pick with no live library
        # asks nothing, for the reason the `if not live` below gives.
        folders = await client.library_folders() if live else []
    except JellyfinError as exc:
        report.blocked = str(exc)
        report.deferred = len(due)
        log.warning(
            "jellyfin intake sweep: the library pick could not be checked against the server, so "
            "%d key(s) stay pending for the next sweep: %s", report.deferred, exc,
        )
        return report
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
        if item is None and stale:
            # In no live picked library, while the pick names one this server no longer lists --
            # which is where it may be. `report.blocked` already names that id, once.
            report.deferred += 1
            continue
        if item is None or resolve.kind_of(item) is None:
            # Two facts, one shape: nobody answered for this id inside the pick, or the row that
            # came back is not a title this app acquires. The second is decision 369 asked of the
            # server -- `NOT_A_TITLE` argues why the payload's own `ItemType` is not enough, and
            # this is the instant to ask it, because the authoritative row is already in hand and
            # the alternative is `pipeline.key_for_item` keying a task on an episode.
            reason = NOT_A_TITLE
            if item is None:
                reason = LIBRARY_NOT_PICKED if cfg.library_ids else GONE_FROM_SERVER
            report.skipped += 1
            report.rows += _rows_decided(await conn.execute(_DECIDE, list(ripe.ids), SKIPPED,
                                                            reason))
            log.info("jellyfin intake sweep: %a enqueues nothing -- %s", ripe.key, reason)
            continue
        try:
            # Outside the transaction, because `re_offered_title` may meet a value the resolver's
            # columns refuse, and a Postgres error inside a transaction aborts the enqueue too.
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
    """The admin's pick split into the libraries this server still lists and the ones it does
    not (decision 410). An empty pick is the whole server and costs no read.

    Compared as GUIDs, for `canonical_id`'s reason: the pick is stored the way the admin route
    received it and `/Library/MediaFolders` answers in the server's spelling. A listing that
    cannot be read raises, and each caller treats that as the outage it is -- never as a pick
    that turned out to be empty, which would widen the boundary to every library.
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
    """Whether the server is up at all, asked through the one read that needs no key. An empty
    or unparseable answer is not an answer: `_request` reduces a portal's page to None, and that is
    the shape of a server this sweep cannot reach."""
    try:
        return bool(await client.server_info())
    except JellyfinError:
        return False


def _storable_item(value: Any) -> Any:
    """A server row as text a jsonb column will take, with its identity untouched.

    `intake._storable` refuses the operator's body outright, because that body is only a record;
    this is the row an acquisition task is filed from, so refusing it drops a real add. A NUL or a
    lone surrogate becomes U+FFFD, which no imdb, tmdb or tvdb id can contain, so a scrubbed
    provider id is refused by stage 1's own validators rather than resolving to another title; a
    non-finite number becomes null, which is what JSON would have had to say. Keys are scrubbed as
    values are. Recursive, and a structure deep enough to exhaust that is `_UNSTORABLE`'s last
    member. [review cycle 3: M52-C3-STATE-01, M52-C3-SWEEP-03]
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


# The statuses that are about the SERVER even though they arrive on one key's read: an admin key
# it no longer accepts (401), a request the server timed out on (408), and a rate limit that the
# next key would hit just as hard (429). Everything else in the 4xx range is taken as a fact about
# the one request -- a 414 over a request line some proxy will not take, a WAF's refusal of one
# query string.
#
# 403 IS NOT ONE OF THEM, and this set used to call it "an admin key it has revoked". Jellyfin
# answers a revoked or unknown key 401 (`AuthService` throws "Invalid token.", which
# `CustomAuthenticationHandler` fails as a challenge), and it never answers the API key 403 on
# `/Items`: the key authenticates as `Administrator` and `ItemsController` skips its visibility
# check for it ("api keys can always access all folders"). So a 403 on one key's read is something
# in front of the server refusing that request -- the WAF the paragraph above already files as
# key-specific -- and filed as server-wide, one crafted id at the oldest window stopped every sweep
# with every key behind it unasked. [M5.2 review cycle 4: M52-C4-SWEEP-01]
#
# THIS USED TO NAME "A 400 OVER A MALFORMED GUID", AND JELLYFIN NEVER SENDS ONE. Its binder drops
# an unparseable piece of `ids` silently, which `jellyfin.MEMBERSHIP_LIMIT` now bounds, and this
# read sends no `ParentId` whose 400 it could meet (decision 408); a stale pick is read off
# `/Library/MediaFolders` before any key is asked about (decision 410). So a 400 that still arrives
# here is a proxy's, about one request. [review cycle 3: M52-C3-SWEEP-04]
_SERVER_WIDE = frozenset({401, 408, 429})


def _about_the_key(exc: JellyfinError) -> bool:
    """Whether this refusal is about the one id that was asked about (decision 364).

    `sweep_pending`'s `return` rests on "what it says is about the SERVER and not about the key -
    so the next key would fail the same way", and that argument is only true of the refusals it
    is true of. `_request` raises the same `JellyfinError` for every status >= 400, so a 4xx the
    one id provoked was indistinguishable from an outage -- and because `ripe_keys` sorts by
    `(opens_at, key)`, the offending key is first on every later sweep too. One unanswerable id
    therefore stopped the household's whole webhook intake, permanently, with no remedy but SQL.
    Decision 364 asks for the failed key's rows to stay pending, which `continue` gives it; the
    whole-sweep stop was this module's own addition and belongs to the outage case alone.
    A transport failure carries no status, and it used to be "the outage case by definition" --
    which a read that times out on one heavy item is not. Whether a status-less failure or a 5xx
    is an outage is now asked of the server (`_server_answers`), and only this function's `True`
    skips the asking. [review cycle 1: M52-INTAKE-02; review cycle 3: M52-C3-SWEEP-02]
    """
    return exc.status is not None and 400 <= exc.status < 500 and exc.status not in _SERVER_WIDE


def _rows_decided(tag: str) -> int:
    """asyncpg hands back `UPDATE <n>`; the count is what the report publishes as `rows`."""
    return int(tag.rpartition(" ")[2] or 0)


# --- the fallback ---------------------------------------------------------------------------------


@dataclass
class DeltaReport:
    """What one delta poll read and filed. `since` is the watermark it read from and `until` the
    one it wrote, which is `WATERMARK_OVERLAP` before the instant its read began."""

    since: datetime | None = None
    until: datetime | None = None
    read: int = 0
    enqueued: int = 0
    already_queued: int = 0
    # Rows the server answered with that carry no `Id` and no provider id at all. Counted rather
    # than raised: `_created_walk` keeps such a row deliberately - it is a corrupt row for the
    # resolver to refuse, not evidence about this server's paging - and a poll that died on one
    # would leave the watermark where it was and re-read it every fifteen minutes for ever.
    unkeyable: int = 0
    # Rows the server answered with that this app does not acquire -- an `Episode`, a `Season`,
    # a folder. On an honest server this is always 0, because `ITEM_TYPES = "Movie,Series"`
    # narrows the query and decision 369 keeps it that way; it is not 0 on a server, proxy or
    # gateway that drops a query parameter, which is the fault `_created_walk`'s own three bounds
    # are written against. Counted rather than swallowed, because a poll silently discarding most
    # of what it read looks exactly like a quiet household.
    # [decision 407; review cycle 2: m52-c2-delta-episode-filter]
    not_a_title: int = 0
    # Rows this schema refused even after `_storable_item`, counted beside `unkeyable` for its
    # reason: a poll that died on one froze the watermark and re-paged a growing superset for ever.
    # [review cycle 3: M52-C3-STATE-01]
    unstorable: int = 0
    # The stored watermark was LATER than the instant this poll began -- a clock that ran ahead and
    # was stepped back -- so the poll read from the floor instead (`poll_delta`).
    # [review cycle 3: M52-C3-STATE-07]
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
    """§7.2's fallback, the fifteen-minute delta poll, filed into the same queue the webhook's
    sweep files into. Decisions 364, 366 and 409 -- the last because the poll reads what the
    server SAVED since the watermark, not §7.2's former `DateCreated > last_sync`: `DateCreated`
    is the file's timestamp, and re-filtering on it threw real adds away
    (`JellyfinClient._created_page` argues it at the read).

    THE WATERMARK ADVANCES ONLY ON A COMPLETED READ, and that is the first of the two mechanisms
    §7.2's "does not re-enqueue titles from a previous poll" rests on; the second is
    `UNIQUE (kind, key)`, which absorbs the overlap whatever this function does. Both, because the
    first is a promise about the poll and the second is a property of the queue, and a milestone
    that shipped only the second would have a poll that re-read the library for ever while the
    coverage row still passed.

    A FAILED READ RAISES OUT OF HERE rather than being reported as a poll that found nothing.
    `items_created_since` refuses to return short - a page the server truncated raises, because an
    add nobody ever looks for again is worse than a poll that failed - and `worker.py`'s job runner
    records a raised job with its reason, which is the line an operator greps for. Swallowing it
    would make a Jellyfin outage look exactly like a healthy quiet household, which is the shape
    M4.11 finding 3 records one connector over.

    THE WATERMARK IS THIS APP'S CLOCK AND `DateLastSaved` IS THE SERVER'S, which is a skew this
    function cannot close: nothing in §7.1's read surface reports the server's own time. The
    exposure is one-sided - an app clock BEHIND the server's re-reads instants the queue then
    de-duplicates - but on the other side it is the SKEW ITSELF that is stepped over, and not, as
    this paragraph used to claim, only a skew larger than the read takes. The filter is evaluated
    in the server's clock domain, so taking `started` before the read buys a margin measured in
    the app's: an app clock ahead by D lost the adds saved in the D that followed each poll.
    Accepted rather than closed, with its cost written down (decision 404): keeping the watermark
    in the server's domain means it never advances on a household that added nothing, which pins
    `MinDateLastSaved` at an ever-older instant and grows the superset every poll re-pages. It is
    the reason §7.2 calls this path the fallback and the webhook the trigger, and both ends are the
    household's own machines, usually the same one under compose. `WATERMARK_OVERLAP` now bounds
    it: a skew inside that margin is re-read rather than stepped over (decision 409).
    [review cycle 1: m52-rev-delta-02]

    THE READ IS SCOPED TO THE PICKED LIBRARIES (decision 364), by the same rule the sweep applies
    to an event: an empty pick is the whole server, and a non-empty one is the acquisition
    boundary. A deselected library is not billed for through this path either.

    AND DECISION 369'S CLAUSE IS HELD HERE TOO, RATHER THAN RESTING ON THE QUERY ALONE.
    `ITEM_TYPES = "Movie,Series"` is unchanged and still excludes `Episode`, but a parameter is a
    request and not a guarantee: this walk's paging bounds exist because this app does not trust
    a server to honour `StartIndex` either. So a proxy, gateway or portal that drops one query
    parameter handed this loop the season §7.2 forbids twelve jobs for and it filed one task per
    EPISODE -- keyed on ids nothing can ever acquire, each burning a slot in a sequential drain
    and burning its `(kind, key)` permanently, since `ON CONFLICT DO NOTHING` never reopens a
    closed row. `sweep_pending` asks `resolve.kind_of` of the server's own row at the sibling
    enqueue and refuses it there (`NOT_A_TITLE`); this is the same question at the other feeder,
    asked before `key_for_item` can mint a key from an episode's id.
    [decision 407; review cycle 2: m52-c2-delta-episode-filter]

    `ParentId` IS TRUSTED ON THIS READ (decision 414, which decision 408 narrows to it). The
    server applies it to a walk that names no `ids`, which this one never does -- unlike the
    sweep's membership read, where it drops it and decision 408 reads the row's `Path` instead. A
    proxy that drops `ParentId` would still widen this read to the whole server, and that stays
    accepted rather than assumed away.

    A PICKED LIBRARY THE SERVER NO LONGER LISTS STOPS NOTHING ELSE (decision 410). The walk raised
    on the first scope a real server 400s, so one stale id filed nothing even from the libraries
    that still exist, every fifteen minutes, as `GET /Items -> 400`. The live scopes are read and
    filed -- `UNIQUE (kind, key)` makes that free to repeat -- and the poll then RAISES naming the
    stale id, before the watermark: the scope it could not read may have gained adds, and a
    watermark advanced past them would step over them for ever. [review cycle 3: M52-C3-LIB-02]

    A WATERMARK IN THE FUTURE IS READ AS NEVER POLLED. A database clock that ran ahead and was
    stepped back left `since > started`; the poll asked for everything created after tomorrow,
    read nothing, and wrote `started` -- moving the watermark back without ever reading the
    instants in between. Unlike decision 404's skew this poll can SEE the state, and it answers it
    the way `registry._instant` answers a stored instant it cannot trust: from the floor, which
    re-reads rather than skips. [review cycle 3: M52-C3-STATE-07]

    AND THE WATERMARK IS WRITTEN ONLY FOR THE SERVER IT WAS READ FROM. The save names the origin
    this read covered (`watermark_origin`), so a poll that raced an admin's origin move cannot put
    the old server's instant back after the move dropped it (decision 366).
    [review cycle 3: M52-C3-STATE-05]
    """
    since = await registry.delta_since(conn, cfg)
    # Taken BEFORE the read and from Postgres, which is the clock `received_at` and every other
    # instant in this schema is written by. After the read it would name an instant the read could
    # not have seen the whole of; from Python it would be the worker container's.
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
    # Last, and never before the enqueues: a watermark written first is an add lost to a crash,
    # and this is the one write in this module whose loss nothing later re-derives.
    await registry.save_jellyfin(conn, delta_watermark=watermark, watermark_origin=cfg.url)
    if report.enqueued:
        log.info("jellyfin delta poll: %d title(s) saved since %s are now queued",
                 report.enqueued, since.isoformat())
    return report
