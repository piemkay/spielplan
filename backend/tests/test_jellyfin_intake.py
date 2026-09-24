"""§7.2's two intake paths, from the event a server sent to the one task it becomes.

"**every Jellyfin add triggers acquisition** - `POST /events/jellyfin` (ItemAdded), token-authed;
15-minute delta poll on what the server saved since the last poll ... Debounce 10 min; series
acquire per-show, not per-episode" (`docs/spielplan-spec_v2.1.md:342-346`, decision 409).

The clause welds two rules together and only one of them is falsifiable as written: "series
acquire per-show" has an observable - an `ItemAdded` for an Episode resolves to its `SeriesId` -
while "debounce 10 min" says nothing about the key, nothing about whether the window slides, and
nothing about where the pending set is when a worker restarts inside it. Decisions 363, 364, 365
and 369 settle those, and this file is where they are asserted: a fixed window per resolved title,
a durable table rather than a dict, the library pick applied by asking the server, and an Episode
that is never keyed on itself.

TWO LAYERS, for the reason `docs/milestones/M5.2-plan.md` §6 splits them. The window's arithmetic
is a pure function over events and one instant, so it is tested as one - twelve episodes of a
season are one show, two shows are two, and an event after the window closed is a second window
rather than an extension of the first. Everything else is a fact about Postgres and about what a
real server answers, so it is tested against both: a real database, and `ops/fake_jellyfin.py`
over ASGI.

THE PAYLOADS ARE THE DOUBLE'S AND NEVER THIS FILE'S. `ops/fake_jellyfin.py:_item_added` renders
the Webhook plugin's template - flattened `Provider_*` keys, for an episode its own ids and its
series identity beside them (M52-C4-TTA-04) - and a test that wrote that shape
out by hand would prove the handler tolerates a body nobody sends. The emitter is a refuser: it
404s an item this server does not hold and 409s a burst it cannot fill, so "twelve events for one
series" is twelve or it is an error.

The route that receives them is `api/events.py` and is M5.2's webhook stage, not this one's, so
the deliveries here are handed to `intake.record_event` - which is the whole of the handler's
synchronous work (plan A4, decision 365): it writes the row and decides nothing.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from spielplan.acquire import intake, pipeline, queue
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError
from spielplan.connectors.registry import JellyfinConfig, load_jellyfin, save_jellyfin
from spielplan.core.config import settings
from spielplan.db.pool import _init_connection as pool_init
from spielplan.importer import bundle as bundle_import
from spielplan.sync import seen
from tests.fixtures import make_bundle as fx

JELLYFIN_URL = "http://jellyfin.test"
# Every item this fake holds was created between 2019 and 2024, so a watermark here is "before
# this library existed" and a poll from it reads the whole of it. The instant a real install
# starts from is its own creation (decision 366), which is what
# `test_a_first_poll_on_a_fresh_install_enqueues_nothing_it_already_has` asserts from the other
# side.
BEFORE_THE_LIBRARY = datetime(2019, 1, 1, tzinfo=UTC)

# Two ids spelled the way every real Jellyfin id is: a GUID, which `/Items` writes as 32 lowercase
# hex digits and the Webhook plugin renders dashed. The first is jellyfin-plugin-webhook#204's own
# example, undashed. The double's seven fixture ids are not GUIDs, which is how a client comparing
# ids as strings passed this whole suite while every real delivery matched nothing.
# [review cycle 3: M52-C3-EVENTS-01]
GUID_MOVIE = "6213b704a0d954293110f4d561b0f614"
GUID_SERIES = "0f0e0d0c0b0a49088706050403020100"


@pytest.fixture
def webhook(fake_jellyfin):
    """The double's `ItemAdded` emitter, wired to a sink that records what crossed the wire.

    A sink rather than the app, because `POST /events/jellyfin` is the webhook stage's route and
    this file is about what happens to a body once it has arrived. The transport is the emitter's
    own injection point (`ops/fake_jellyfin.py`'s `WEBHOOK_TRANSPORT`), so the payload is built,
    serialised and posted by the double exactly as it would be across a compose network, and what
    these tests feed `record_event` is the body that came off that wire.
    """
    module, _ = fake_jellyfin
    delivered: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"ok": True})

    module.WEBHOOK_TRANSPORT = httpx.MockTransport(receive)
    module.WEBHOOK_URL = "http://spielplan.test/events/jellyfin"
    module.WEBHOOK_TOKEN = "the-token-the-operator-pasted"
    return module, delivered


async def _emit(module, delivered, **body) -> list[dict]:
    """Fire the emitter and hand back the bodies it actually delivered."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://fake-jellyfin"
    ) as control:
        response = await control.post("/_test/item-added", json=body)
    assert response.status_code == 200, response.text
    sent = int(response.json()["sent"])
    assert len(delivered) >= sent
    return delivered[-sent:]


def _config(**kwargs) -> JellyfinConfig:
    return JellyfinConfig(url=JELLYFIN_URL, api_key="fake-admin-key", **kwargs)


def _client(transport) -> JellyfinClient:
    return JellyfinClient(JELLYFIN_URL, "fake-admin-key", transport=transport)


def _unreachable() -> JellyfinClient:
    """A client whose every read fails, for the two rules that are about a read that did not
    happen. `_request` wraps an httpx error in `JellyfinError` (§3.3), so this is the shape a
    Jellyfin outage has from inside this app rather than an exception type invented here."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the household's Jellyfin is not answering")

    return _client(httpx.MockTransport(refuse))


async def _age_the_window(db, minutes: int) -> None:
    """Move every intake row back in time, which is the only honest way to watch a fixed window
    close: the clock is Postgres's (`now()` is what wrote `not_before`), so a test that patched a
    Python clock would be asserting against an instant the database never saw."""
    await db.execute(
        "UPDATE jellyfin_intake SET received_at = received_at - make_interval(mins => $1),"
        "       not_before = not_before - make_interval(mins => $1)", minutes,
    )


async def _record(db, payloads) -> list[intake.Event]:
    return [await intake.record_event(db, body) for body in payloads]


async def _task_keys(db) -> list[str]:
    return [
        row["key"] for row in await db.fetch(
            "SELECT key FROM acquisition_task WHERE kind = $1 ORDER BY key", pipeline.TASK_KIND
        )
    ]


async def _states(db) -> dict[str, int]:
    return {
        row["state"]: row["n"] for row in await db.fetch(
            "SELECT state, count(*) AS n FROM jellyfin_intake GROUP BY state"
        )
    }


def _hold_guid_titles(module) -> None:
    """Give the double a film and a three-episode series whose ids are real GUIDs.

    Added to THIS test's copy of the double (the fixture loads a fresh module per test), because
    the seven fixture ids are pinned by literal in files this milestone does not own. Stored in the
    server's own "N" spelling, which is what `/Items` answers with; the emitter dashes them on the
    way out, as the plugin does.
    """
    ticks = 45 * 60 * 10_000_000
    module.ITEMS.extend([
        {"Id": GUID_MOVIE, "Name": "A Film Jellyfin Spells In Hex", "Type": "Movie",
         "ProductionYear": 2024, "RunTimeTicks": ticks,
         "DateCreated": "2025-02-01T10:00:00.0000000Z", "ProviderIds": {"Tmdb": "5100001"}},
        {"Id": GUID_SERIES, "Name": "A Series Jellyfin Spells In Hex", "Type": "Series",
         "ProductionYear": 2025, "RunTimeTicks": ticks,
         "DateCreated": "2025-02-01T11:00:00.0000000Z", "ProviderIds": {"Tmdb": "5100002"}},
    ])
    episodes = [
        {"Id": uuid.uuid5(uuid.UUID(GUID_SERIES), f"e{n}").hex, "Name": f"Episode {n}",
         "Type": "Episode", "SeriesId": GUID_SERIES, "SeriesName": "A Series Jellyfin Spells In Hex",
         "ParentIndexNumber": 1, "IndexNumber": n, "RunTimeTicks": ticks,
         "DateCreated": f"2025-02-01T11:0{n}:00.0000000Z"}
        for n in (1, 2, 3)
    ]
    module.EPISODES[GUID_SERIES] = episodes
    module.ALL_EPISODES.extend(episodes)
    module.EPISODE_SERIES.update({row["Id"]: GUID_SERIES for row in episodes})
    module.LIBRARY_MEMBERS["jf-lib-films"] += (GUID_MOVIE,)
    module.LIBRARY_MEMBERS["jf-lib-shows"] += (GUID_SERIES,)


def _json(rows: list[dict]) -> httpx.Response:
    """An `/Items` envelope serialised the way the server serialises one: JSON can spell a
    control character only as an escape, so a NUL in a row arrives as the six characters
    `\\u0000`, exactly as `System.Text.Json` writes it."""
    return httpx.Response(
        200, content=json.dumps({"Items": rows, "TotalRecordCount": len(rows)}).encode("ascii"),
        headers={"content-type": "application/json"},
    )


# --- the window's arithmetic, without a database --------------------------------------


def _pending(key: str, opens_at: datetime, row_id: int) -> dict:
    """One pending row as `sweep_pending` reads it. Three columns, because three is what the
    arithmetic is over - the id it decides, the key it groups by, the instant that decides."""
    return {"id": row_id, "resolved_key": key, "not_before": opens_at}


def test_twelve_events_for_one_series_inside_the_window_are_one_ripe_key():
    """§7.2's observable, as arithmetic: "Debounce 10 min; series acquire per-show, not
    per-episode". Twelve episodes of a season, delivered seconds apart the way a scan delivers
    them, are ONE key - and all twelve rows are decided together, because the collapse happens
    over the rows and the queue then makes the enqueue itself idempotent (decision 363).
    """
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [
        _pending("jf-7", start + timedelta(minutes=10, seconds=n), n) for n in range(12)
    ]

    ripe = intake.ripe_keys(rows, start + timedelta(minutes=11))

    assert len(ripe) == 1, "twelve events for one show are one job"
    assert ripe[0].key == "jf-7"
    assert ripe[0].ids == tuple(range(12))


def test_two_shows_added_in_one_window_are_two_keys():
    """The collapse is per TITLE and never per window: an import that adds two series inside one
    ten minutes owes the household two acquisitions, and a debounce that grouped by time rather
    than by key would file one of them and lose the other."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("jf-7", start + timedelta(minutes=10), 1),
            _pending("jf-6", start + timedelta(minutes=10, seconds=30), 2),
            _pending("jf-7", start + timedelta(minutes=10, seconds=40), 3)]

    ripe = intake.ripe_keys(rows, start + timedelta(minutes=11))

    assert {entry.key: entry.ids for entry in ripe} == {"jf-7": (1, 3), "jf-6": (2,)}


def test_an_event_after_the_window_closed_opens_a_second_one_rather_than_extending_the_first():
    """THE WINDOW IS FIXED (decision 363), which is the half of the debounce §7.2 does not
    settle and the half with a failure mode: under a sliding window a library scan that keeps
    adding episodes postpones the acquisition of the show it started with for as long as it
    runs, and a big import can run for hours.

    So `not_before` is a pure function of each row's own arrival, a key is ripe when the OLDEST
    of its pending rows says so, and a thirteenth episode arriving after the first twelve were
    swept is a window of its own - not an extension of theirs, and not something the first
    sweep silently consumes on its way past.
    """
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    burst = [_pending("jf-7", start + timedelta(minutes=10), n) for n in range(12)]
    latecomer = _pending("jf-7", start + timedelta(minutes=21), 99)

    first = intake.ripe_keys([*burst, latecomer], start + timedelta(minutes=10, seconds=1))
    assert [entry.ids for entry in first] == [tuple(range(12))], (
        "the late event was swept inside a window whose ten minutes had not run"
    )

    second = intake.ripe_keys([latecomer], start + timedelta(minutes=22))
    assert [entry.ids for entry in second] == [(99,)]


def test_a_window_that_has_not_opened_is_swept_by_nobody():
    """The negative the whole debounce exists for: an event delivered a moment ago is not work
    yet, because the rest of the season is still arriving."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("jf-7", start + timedelta(minutes=10), 1)]

    assert intake.ripe_keys(rows, start + timedelta(minutes=9, seconds=59)) == []


def test_the_oldest_window_is_swept_first():
    """A sweep that runs out of its budget must have spent it on the events that have waited
    longest, rather than on whichever key happened to sort first."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("aaa", start + timedelta(minutes=20), 1),
            _pending("zzz", start + timedelta(minutes=10), 2)]

    assert [entry.key for entry in intake.ripe_keys(rows, start + timedelta(hours=1))] == [
        "zzz", "aaa"
    ]


def test_a_row_written_while_the_clock_ran_ahead_is_ripe_once_the_clock_is_corrected():
    """Decision 363's window is a pure function of the row's own arrival, and that arrival is
    Postgres's `now()` -- so a database clock that ran a day ahead (a VM resumed from suspend, a
    drifting RTC) and was then corrected by NTP leaves rows whose `received_at` is in the future.
    Measured by the clock that wrote it, such a row's ten minutes end a day and ten minutes from
    now, and it waited out the whole step before anything looked at it.

    A row received AFTER the instant it is swept at cannot be a window still collecting its burst
    -- nothing honest arrives in the future -- so it is ripe. Ripening early costs nothing the
    debounce protects: the queue's `UNIQUE (kind, key)` still makes a late sibling a no-op.
    [review cycle 3: M52-C3-STATE-07]
    """
    now = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    arrived = now + timedelta(days=1)
    stepped = {**_pending("jf-7", arrived + timedelta(seconds=intake.DEBOUNCE_SECONDS), 1),
               "received_at": arrived}
    collecting = {**_pending("jf-6", now + timedelta(minutes=4), 2),
                  "received_at": now - timedelta(minutes=6)}

    ripe = intake.ripe_keys([stepped, collecting], now)

    assert [entry.key for entry in ripe] == ["jf-7"], (
        "a row stamped by a clock that ran ahead waits out the whole step"
    )


@pytest.mark.parametrize("spelling", [
    "6213b704-a0d9-5429-3110-f4d561b0f614",
    "6213B704-A0D9-5429-3110-F4D561B0F614",
    "{6213b704-a0d9-5429-3110-f4d561b0f614}",
    "6213B704A0D954293110F4D561B0F614",
])
def test_a_guid_is_keyed_in_the_servers_own_spelling_however_the_plugin_spelled_it(spelling):
    """Decision 363's key is the title, and one title has one key only if one GUID has one
    spelling. The Webhook plugin assigns the raw `Guid` into its template's data and Handlebars
    renders it dashed; `/Items` writes every `Id` as 32 lowercase hex digits. Kept as delivered,
    the key was the dashed form, `item_in_libraries` compared it as a string against the server's
    row, and every real `ItemAdded` was filed `skipped` as "the server no longer holds this item"
    -- terminally, with a false reason, and with no task. Canonicalised here, the debounce group,
    the `ids=` the sweep sends and the key the task is filed under are all the server's spelling.
    [review cycle 3: M52-C3-EVENTS-01, M52-C3-SWEEP-01, M52-C3-369-GUID-01]
    """
    movie = intake.read_event({"ItemId": spelling, "ItemType": "Movie"})
    episode = intake.read_event(
        {"ItemId": "0f0e0d0c-0b0a-4908-8706-050403020101", "ItemType": "Episode",
         "SeriesId": spelling}
    )

    assert (movie.state, movie.resolved_key, movie.item_id) == (
        intake.PENDING, GUID_MOVIE, GUID_MOVIE
    )
    assert (episode.state, episode.resolved_key) == (intake.PENDING, GUID_MOVIE)
    assert episode.item_id == "0f0e0d0c0b0a49088706050403020101"


# --- what one delivered body is (decisions 365, 369) ----------------------------------


async def test_a_movie_is_keyed_on_itself_and_an_episode_on_the_show(webhook):
    """Decision 369, read off two bodies the double built rather than off two this file did.

    An `Episode` payload carries `SeriesId` beside the episode's own `ItemId`, and `Provider_*`
    keys of its own beside both: the plugin flattens `item.ProviderIds` after its type switch, so
    for every item it sends, and Jellyfin's TMDb provider gives each episode its own Tvdb and Imdb
    ids. This test used to assert the opposite -- that an episode event carries no identity at all
    -- and passed only because the double sent none. The key is the series because the identity an
    episode carries is an EPISODE's, which no title in this app is, and not because there is none.
    [M5.2 review cycle 4: M52-C4-TTA-04]
    """
    module, delivered = webhook
    (movie,) = await _emit(module, delivered, item_id="jf-1")
    (episode, *_) = await _emit(module, delivered, item_id="jf-7", episodes=12)
    show = next(item for item in module.ITEMS if item["Id"] == "jf-7")

    assert intake.read_event(movie).resolved_key == "jf-1"
    own = {key: value for key, value in episode.items() if key.startswith("Provider_")}
    assert own, "the plugin sends an episode's own provider ids, as it does every item's"
    assert own.get("Provider_imdb") != show["ProviderIds"]["Imdb"], "and they are not the show's"
    resolved = intake.read_event(episode)
    assert (resolved.state, resolved.resolved_key) == (intake.PENDING, "jf-7")
    assert resolved.item_id == "jf-7-e1", "the episode is still recorded as the event it was"


async def test_an_episode_with_no_series_id_is_refused_rather_than_keyed_on_the_episode(webhook):
    """The fallback that looks harmless - key it on the episode - is exactly the twelve jobs
    §7.2 forbids, one per episode of the season a scan just added. The template is
    operator-authored, so a missing `SeriesId` is a real configuration and not a hypothetical.
    """
    module, delivered = webhook
    (episode,) = await _emit(module, delivered, item_id="jf-7", episodes=1, omit=["SeriesId"])

    event = intake.read_event(episode)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.EPISODE_WITHOUT_SERIES)
    assert event.resolved_key is None, "an episode id must never become an acquisition key"


async def test_an_episode_whose_type_is_spelled_in_another_case_is_still_keyed_on_the_show(
    webhook,
):
    """Decision 369 held against the one field it is drawn with, spelled the way a server or a
    hand-written template can spell it.

    `resolve.provider_ids` already argues this for the same server's other strings -- "different
    Jellyfin versions and plugins disagree about capitalisation ... a case-sensitive lookup here
    would silently drop identity on some libraries" -- and this was the one place in the tree that
    compared a Jellyfin-supplied type exactly. Off by one letter, the Episode arm was skipped and
    the event was keyed on the EPISODE, which is decision 369's own failure and the twelve jobs
    §7.2 forbids. [review cycle 1: M52-C1-EPISODE-01]
    """
    module, delivered = webhook
    (lowercase,) = await _emit(module, delivered, item_id="jf-7", episodes=1, item_type="episode")

    event = intake.read_event(lowercase)

    assert (event.state, event.resolved_key) == (intake.PENDING, "jf-7")
    assert event.item_id == "jf-7-e1", "the episode is still recorded as the event it was"


async def test_an_item_id_too_long_to_ask_the_server_about_is_recorded_and_never_asked(
    db, fake_jellyfin
):
    """The payload is operator-authored and nothing bounds `ItemId` (decision 365), but the value
    does not stay in this schema: `item_in_libraries` puts it in a query string. Past a
    request-line limit that is a 414 from Jellyfin or from any proxy in front of it, and past
    httpx's own 64KB URL limit it is an `InvalidURL` -- not a `JellyfinError`, so it escapes the
    sweep's own handling and takes the job down rather than one key.

    BOTH ARMS, BECAUSE THE BOUND IS ON THE RESOLVED KEY AND THE TWO ARMS RESOLVE IT FROM DIFFERENT
    FIELDS (decision 369). On the Movie arm the key IS `ItemId`, so this test could not fail there
    whatever the guard checked; on the Episode arm it is `SeriesId`, which went unbounded and
    reached both the query string above and the partial btree `jellyfin_intake_ripe` indexes it
    with. One `ItemAdded` was then answered 202 and recorded pending, and every later sweep raised
    `InvalidURL` on it -- oldest window first, so on every sweep after that too, with every add the
    household made piling up behind it. The sweep below is what holds that half: unbounded, it is
    the one call in this file that does not come back. [review cycle 1: M52-INTAKE-02;
    review cycle 2: M52-C2-EVENTS-01]

    Authored rather than emitted, because the double will not render it: `_item` 404s an id this
    server does not hold, which is the refusal that keeps every other body in this file honest.
    """
    event = await intake.record_event(
        db, {"ItemId": "y" * 100_000, "ItemType": "Movie", "Name": "a title nobody holds"}
    )

    assert (event.state, event.reason) == (intake.SKIPPED, intake.ITEM_ID_TOO_LONG)
    assert event.resolved_key is None, "a key nothing can be asked about is not a key"

    episode = await intake.record_event(
        db, {"ItemId": "jf-7-e1", "ItemType": "Episode", "SeriesId": "s" * 100_000}
    )

    assert (episode.state, episode.reason) == (intake.SKIPPED, intake.SERIES_ID_TOO_LONG)
    assert episode.resolved_key is None, "an episode's key is its SeriesId and this is not one"
    assert episode.item_id == "jf-7-e1", "the episode is still recorded as the event it was"
    assert await _states(db) == {intake.SKIPPED: 2}, "both deliveries are still recorded"

    await _age_the_window(db, minutes=11)
    report = await intake.sweep_pending(db, _client(fake_jellyfin[1]), _config())

    assert (report.ripe, report.enqueued, report.deferred) == (0, 0, 0), (
        "a key no URL can be built from must never reach the sweep at all"
    )


async def test_a_payload_missing_a_field_the_handler_acts_on_is_recorded_and_not_refused(webhook):
    """Decision 365: require two fields, tolerate the rest, and never answer 400 to a delivery.

    `ItemId` is one of the two, and an operator whose template omits it has configured something
    this app cannot act on - so it is recorded with that reason, which is the only way the
    operator ever learns. The plugin does not retry; a rejection is a silence.
    """
    module, delivered = webhook
    (body,) = await _emit(module, delivered, item_id="jf-1", omit=["ItemId"])

    event = intake.read_event(body)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.NO_ITEM_ID)
    assert event.item_type == "Movie", "what the body did carry is still recorded"


async def test_a_template_for_another_event_is_recorded_and_not_acted_on(webhook):
    """An operator can point every one of the plugin's templates at one URL. §7.2 names
    `ItemAdded` and nothing else, so the others are recorded and enqueue nothing - and an ABSENT
    `NotificationType` is read as `ItemAdded`, because the template is operator-authored and
    refusing the plainest working configuration would be refusing §7.2's own subject.
    """
    module, delivered = webhook
    (other,) = await _emit(module, delivered, item_id="jf-1", notification_type="ItemUpdated")

    assert intake.read_event(other).reason == intake.NOT_ITEM_ADDED

    untyped = {key: value for key, value in other.items() if key != "NotificationType"}
    assert intake.read_event(untyped).state == intake.PENDING


async def test_a_value_a_delivery_carried_is_logged_escaped_and_never_as_a_line_of_its_own(
    db, caplog
):
    """`record_event`'s refusal line and the sweep's per-key lines interpolated `ItemId`,
    `ItemType` and the resolved key verbatim, and the payload is the only source of all three --
    so a token holder's `ItemType` carrying a newline wrote a second, fully formed log line of its
    choosing into `docker compose logs backend`, which §6.6 and the M4.7 runbook treat as the
    operator's evidence: a forged `ERROR ... database error`, or a forged `applied migrations`.
    Non-ASCII ids reached the same lines, against the house rule for console output. Every value
    that came off the wire is written as its `ascii()` repr. [review cycle 3: M52-C3-EVENTS-06]
    """
    forged = "Movie\n2026-09-23 12:00:00,000 ERROR   spielplan database error (forged)"
    with caplog.at_level(logging.DEBUG, logger="spielplan.acquire.intake"):
        await intake.record_event(
            db, {"NotificationType": "PlaybackStart", "ItemId": "jf-1\r\nX-Evil: 1",
                 "ItemType": forged}
        )
        await intake.record_event(db, {"ItemId": "jf-é☃\n1", "ItemType": "Movie"})
        await _age_the_window(db, minutes=11)
        await intake.sweep_pending(
            db, _client(httpx.MockTransport(lambda request: _json([]))), _config()
        )

    lines = [record.getMessage() for record in caplog.records]
    assert any("not an ItemAdded" in line for line in lines), lines
    assert any("enqueues nothing" in line for line in lines), lines
    for line in lines:
        assert "\n" not in line and "\r" not in line, f"a delivery wrote its own log line: {line!r}"
        assert line.isascii(), f"a delivery put a non-ASCII byte on the console: {line!r}"


@pytest.mark.parametrize("body", [None, "", "not json at all", [1, 2, 3], 7])
def test_a_body_that_is_not_an_object_is_read_as_a_refusal_rather_than_raising(body):
    """The handler answers 202 to a body it cannot act on (decision 365), so the reading of one
    must not raise: an exception here is a 500 on the route, which is the one answer that clause
    forbids. A body that is not an object is not a template that is wrong; it is a delivery this
    app cannot read at all, and it is recorded as that."""
    event = intake.read_event(body)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.UNREADABLE)
    assert event.resolved_key is None


# --- the burst, against a real database -----------------------------------------------


async def test_a_burst_of_twelve_episodes_yields_one_acquisition_task_for_the_show(
    db, fake_jellyfin, webhook
):
    """§7.2's headline observable and the inherited coverage row's first clause: a burst of
    `ItemAdded` for many episodes of one series inside the debounce window yields ONE job for the
    show, not one per episode.

    Twelve rows land, because the record of how a burst arrived is what an operator reads at
    §6.6; one task leaves, because the collapse is over `resolved_key` and the queue's
    `UNIQUE (kind, key)` is the second mechanism under it. And nothing that leaves is `paid`
    (decision 347): `queue.lease` filters on that flag and the driver refuses an implemented paid
    stage with no spend cap, so an intake path that set it would have routed the household's
    library straight past the gate §8 puts in front of billing.
    """
    module, _ = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=12))
    assert await _states(db) == {intake.PENDING: 12}

    unripe = await intake.sweep_pending(db, _client(fake_jellyfin[1]), _config())
    assert (unripe.ripe, unripe.enqueued) == (0, 0), "the window had not closed"

    await _age_the_window(db, minutes=11)
    report = await intake.sweep_pending(db, _client(fake_jellyfin[1]), _config())

    assert (report.ripe, report.enqueued, report.rows) == (1, 1, 12)
    assert await _task_keys(db) == ["jellyfin:jf-7"]
    assert await _states(db) == {intake.ENQUEUED: 12}
    assert await db.fetchval("SELECT bool_or(paid) FROM acquisition_task") is False
    items = await db.fetch("SELECT item_id FROM jellyfin_intake ORDER BY item_id")
    assert len(items) == 12, "every event is still recorded as the event it was"


async def test_the_burst_survives_a_sweep_that_stops_and_starts_inside_the_window(
    db, pg_url, fake_jellyfin, webhook
):
    """The durability clause of `...the-webhook-survives-what-a-real-server-sends`, and the whole
    reason the pending set is a table: §5.3 files every job as durable, and a library scan
    delivers a season in a burst. A set living in the worker's memory would drop the scan to a
    restart inside the ten minutes and the household would never learn that it had.

    The restart is of the SWEEP and not of the process, which is what the plan asks for and what
    a test can actually do - and it is made real here by running the last sweep on a SECOND
    CONNECTION, so that nothing the first one held can be what remembered the burst. The second
    carries the pool's codecs, because `db/pool._init_connection` registers them and a bare
    `asyncpg.connect` does not.
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=12))

    await _age_the_window(db, minutes=5)
    halfway = await intake.sweep_pending(db, _client(transport), _config())
    assert (halfway.ripe, halfway.enqueued) == (0, 0)

    await _age_the_window(db, minutes=6)
    other = await asyncpg.connect(pg_url)
    await pool_init(other)
    try:
        report = await intake.sweep_pending(other, _client(transport), _config())
    finally:
        await other.close()

    assert (report.ripe, report.enqueued) == (1, 1)
    assert await _task_keys(db) == ["jellyfin:jf-7"]


async def test_a_sweep_cut_between_the_enqueue_and_the_record_of_it_loses_no_add(
    db, fake_jellyfin, webhook, monkeypatch
):
    """`sweep_pending`'s "THE ENQUEUE AND THE RECORD OF IT ARE ONE TRANSACTION, in that order",
    which was argued in prose and asserted nowhere.

    The asymmetry the order rests on is real and only one way round: re-enqueueing after a crash
    is free, because `queue.enqueue` is `ON CONFLICT (kind, key) DO NOTHING`, while a row marked
    `enqueued` beside a task that was never filed is an add nobody will ever look for again. The
    only restart this suite exercised is one where the window has NOT closed, so the sweep that
    restarts has nothing to do -- decide-first survived it, and so did dropping the transaction.

    The interruption is the one `_jellyfin_intake_sweep` actually has: a worker cut by its own
    120-second budget, or stopped, between the two writes. Twelve episodes of one season, so what
    a reordered sweep loses is a whole show. [review cycle 2: M52-C2-INTAKE-02]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=12))
    await _age_the_window(db, minutes=11)

    real = pipeline.enqueue_item

    async def enqueue_then_die(conn, item):
        await real(conn, item)
        raise RuntimeError("the worker was cut between the two writes")

    monkeypatch.setattr(pipeline, "enqueue_item", enqueue_then_die)
    with pytest.raises(RuntimeError):
        await intake.sweep_pending(db, _client(transport), _config())

    assert await _states(db) == {intake.PENDING: 12}, (
        "decided first, the twelve rows read enqueued beside a task that does not exist"
    )
    assert await _task_keys(db) == [], "the transaction is what takes the enqueue back with them"

    monkeypatch.undo()
    report = await intake.sweep_pending(db, _client(transport), _config())

    assert (report.ripe, report.enqueued) == (1, 1)
    assert await _task_keys(db) == ["jellyfin:jf-7"], "the burst is still one job for the show"
    assert await _states(db) == {intake.ENQUEUED: 12}


async def test_two_sweeps_at_once_file_one_task_and_only_one_of_them_decides_the_rows(
    db, pg_url, fake_jellyfin, webhook
):
    """The other half of the same docstring: "TWO SWEEPS AT ONCE COST NOTHING AND NO LOCK IS
    TAKEN", which `queue.lease` calls "the ordinary state during a rolling restart".

    Both sweeps read the same ripe rows; `UNIQUE (kind, key)` makes one enqueue create the task
    and the other return False, and the `state = 'pending'` in `_DECIDE` is what makes the second
    UPDATE match nothing. Without that one clause the counts are the only thing that lies -- the
    table and the queue still agree -- so nothing in a single-threaded suite can fail on it, and
    a report claiming to have decided rows another sweep decided is how §6.6's card would learn
    to double-count a burst.

    The overlap is arranged rather than raced: the first sweep is held inside its membership read
    until the second has finished, which is the interleaving the argument is about and the only
    one a test can assert. The second connection is the pool's, for the restart test's reason --
    `db/pool._init_connection` registers the codecs and a bare `asyncpg.connect` does not.
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    healthy = await _client(transport).item_in_libraries("jf-7", [])
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=12))
    await _age_the_window(db, minutes=11)

    reading, release = asyncio.Event(), asyncio.Event()

    async def held(_request: httpx.Request) -> httpx.Response:
        reading.set()
        await release.wait()
        return httpx.Response(200, json={"Items": [healthy], "TotalRecordCount": 1})

    first = asyncio.create_task(
        intake.sweep_pending(db, _client(httpx.MockTransport(held)), _config())
    )
    await reading.wait()

    other = await asyncpg.connect(pg_url)
    await pool_init(other)
    try:
        second = await intake.sweep_pending(other, _client(transport), _config())
    finally:
        await other.close()
    release.set()
    overtaken = await first

    assert (second.ripe, second.enqueued, second.rows) == (1, 1, 12)
    assert (overtaken.ripe, overtaken.enqueued, overtaken.already_queued) == (1, 0, 1)
    assert overtaken.rows == 0, "the second UPDATE may not re-decide rows another sweep decided"
    assert await _task_keys(db) == ["jellyfin:jf-7"], "two sweeps, one task"
    assert await _states(db) == {intake.ENQUEUED: 12}


async def test_a_template_that_calls_an_episode_a_movie_files_no_task_for_the_episode(
    db, fake_jellyfin, webhook
):
    """Decision 369's clause is absolute -- "no acquisition task is ever keyed on an episode" --
    and `read_event` can only hold it for a payload that DECLARES itself an Episode. `ItemType`
    is operator-authored like every other field, so that is a boundary drawn with data an operator
    can forget, which is decision 364's own argument for asking the server about the library.

    Here the whole payload is the plugin's and one field is the mistake: a template that hardcodes
    `Movie` where the published contract renders `{{ItemType}}`. Twelve episodes of one season
    then key on themselves, and the membership read -- which sends no `IncludeItemTypes`, on
    purpose -- hands back twelve rows whose own `Type` says `Episode`. Read, that is twelve
    refusals with a reason; unread, it was twelve acquisition tasks keyed on episode ids, in the
    case §7.2 calls hardest to notice. [review cycle 1: M52-C1-EPISODE-01]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(
        module, delivered, item_id="jf-7", episodes=12, item_type="Movie"
    ))
    await _age_the_window(db, minutes=11)

    report = await intake.sweep_pending(db, _client(transport), _config())

    assert (report.ripe, report.enqueued, report.skipped) == (12, 0, 12)
    assert await _task_keys(db) == [], "not one of them may become work"
    assert await _states(db) == {intake.SKIPPED: 12}
    assert await db.fetchval("SELECT DISTINCT reason FROM jellyfin_intake") == intake.NOT_A_TITLE


async def test_one_key_the_server_will_not_answer_for_does_not_wedge_the_keys_behind_it(
    db, fake_jellyfin, webhook
):
    """Decision 364 asks that a failed membership read leave THAT key's rows pending. Stopping
    the whole sweep was this module's own addition, and it rested on "what it says is about the
    SERVER and not about the key" -- which is false of a 4xx the one id in `ids=` provoked.

    The ripe set is swept oldest-first, so the offending key was first on every later sweep too:
    one unanswerable id stopped the household's entire webhook intake, permanently, while every
    later add piled up pending behind it and the job kept recording `ok`. The four statuses that
    are about the server are asserted one test down, and the outage with a transport where
    nothing answers at all by `...an_outage_stops_the_sweep_at_the_first_key...`.

    The 400 is scripted at the transport for `...no_longer_holds...`'s reason -- the double
    refuses to invent a fault it does not have -- while the row that comes back for the healthy
    key is the double's own. It stands for a PROXY's refusal of one request line: Jellyfin itself
    never 400s an id it cannot parse, it drops it, which
    `test_an_id_the_server_cannot_parse_costs_one_bounded_read_and_is_decided` holds. This file
    used to script that 400 as "not a GUID", a response the server never sends.
    [review cycle 1: M52-INTAKE-02; review cycle 3: M52-C3-LIB-05]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    healthy = await _client(transport).item_in_libraries("jf-2", [])
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _record(db, await _emit(module, delivered, item_id="jf-2"))
    await _age_the_window(db, minutes=11)

    def one_bad_id(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("ids") == "jf-1":
            return httpx.Response(400, text="the gateway refused this request line")
        return httpx.Response(200, json={"Items": [healthy], "TotalRecordCount": 1})

    report = await intake.sweep_pending(
        db, _client(httpx.MockTransport(one_bad_id)), _config()
    )

    assert (report.ripe, report.enqueued, report.deferred) == (2, 1, 1)
    assert "400" in report.blocked
    assert await _task_keys(db) == ["jellyfin:jf-2"], "the healthy key was decided"
    assert await _states(db) == {intake.PENDING: 1, intake.ENQUEUED: 1}


# Spelled out rather than read off `intake._SERVER_WIDE`: a parameter list derived from the set
# under test loses a case whenever the set loses a member, and the test goes on passing.
@pytest.mark.parametrize("status", [401, 408, 429])
async def test_a_refusal_about_the_server_stops_the_sweep_though_the_server_answers(
    db, fake_jellyfin, webhook, status
):
    """The other half of the test above, and until review cycle 3 nothing asserted it at all.
    `_SERVER_WIDE` names three statuses that arrive on one key's read and are about the SERVER --
    an admin key it no longer accepts (401), a request it timed out on (408), a rate limit the
    next key hits just as hard (429) -- so the sweep stops at the key that met one and leaves every
    key behind it pending and unasked. Dropped from the set, each becomes "a fact about the key": a
    revoked key is then spent once per ripe key on every sweep, and a rate limit is answered with
    more requests. 403 used to be listed here as a revoked key, and Jellyfin never sends one to
    this app's key: the test below. [M5.2 review cycle 4: M52-C4-SWEEP-01]

    `/System/Info/Public` answers throughout, from the double, because the status alone has to
    decide: a server that is up and refuses this app's key is exactly the server a probe would
    call healthy. Two ripe keys, because with one the stop and the carry-on file the same report.
    [review cycle 3: M52-C3-SWEEP-06]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _record(db, await _emit(module, delivered, item_id="jf-2"))
    await _age_the_window(db, minutes=11)
    asked: list[str | None] = []

    class RefusesEveryKey(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items":
                asked.append(request.url.params.get("ids"))
                return httpx.Response(status, text="refused")
            return await transport.handle_async_request(request)

    report = await intake.sweep_pending(db, _client(RefusesEveryKey()), _config())

    assert (report.ripe, report.enqueued, report.skipped, report.deferred) == (2, 0, 0, 2), report
    assert asked == ["jf-1"], f"a {status} is about the server and the sweep asked again: {asked}"
    assert str(status) in report.blocked
    assert await _states(db) == {intake.PENDING: 2}
    assert await _task_keys(db) == []


async def test_a_403_on_one_key_is_that_keys_and_the_sweep_goes_on(db, fake_jellyfin, webhook):
    """Jellyfin never answers this app's key 403 on `/Items`. A revoked or unknown key is 401
    (`AuthService`: `throw new SecurityException("Invalid token.")`, failed by
    `CustomAuthenticationHandler` as a challenge), and the key authenticates as `Administrator`,
    which `ItemsController` exempts from its visibility check ("api keys can always access all
    folders"). So a 403 on one key's read is something in front of the server refusing that one
    request -- a WAF over a query string it dislikes -- and `_SERVER_WIDE` filing it as a revoked
    key stopped every sweep at that key, oldest first, with every add behind it unasked for good.

    The crafted id is a delivery any token holder can send (decision 406), bounded by
    `MAX_ITEM_ID` and let through to `ids=` by decision 415; the WAF is scripted at the transport,
    and every other answer is the double's. [M5.2 review cycle 4: M52-C4-SWEEP-01]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await intake.record_event(db, {"ItemId": "jf-1' or '1'='1", "ItemType": "Movie"})
    await _age_the_window(db, minutes=12)
    await _record(db, await _emit(module, delivered, item_id="jf-2"))
    await _record(db, await _emit(module, delivered, item_id="jf-3"))
    await _age_the_window(db, minutes=11)

    class Waf(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "'" in str(request.url.params.get("ids", "")):
                return httpx.Response(403, text="request blocked")
            return await transport.handle_async_request(request)

    report = await intake.sweep_pending(db, _client(Waf()), _config())

    assert (report.ripe, report.enqueued, report.deferred) == (3, 2, 1), report
    assert "403" in report.blocked
    assert await _task_keys(db) == ["jellyfin:jf-2", "jellyfin:jf-3"]
    assert await db.fetchval(
        "SELECT resolved_key FROM jellyfin_intake WHERE state = 'pending'"
    ) == "jf-1' or '1'='1"


async def test_one_id_the_server_fails_on_does_not_stop_the_sweep_while_the_server_answers(
    db, fake_jellyfin, webhook
):
    """The arm of M52-INTAKE-02's wedge its fix did not touch. A failure with no status, or a 5xx,
    was read as "the SERVER is down" and stopped the sweep with every key behind it deferred --
    and because the ripe set is swept oldest-first, an id the server fails on EVERY time (a DTO
    that will not build for one corrupt item, one read that outruns the client's timeout) headed
    every later sweep, and nothing the household added after it was ever filed by this path.

    The server is ASKED rather than assumed down: one unauthenticated `/System/Info/Public`, and an
    answer means the failure was this key's. A server that answers nothing still stops the sweep,
    which `...an_outage_stops_the_sweep_at_the_first_key...` below holds with a transport where
    nothing answers. Both faults are scripted at the transport for `...no_longer_holds...`'s
    reason -- the double refuses to invent a fault it does not have -- and every healthy answer is
    the double's own. [review cycle 3: M52-C3-SWEEP-02]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=12)
    await _record(db, await _emit(module, delivered, item_id="jf-2"))
    await _record(db, await _emit(module, delivered, item_id="jf-3"))
    await _age_the_window(db, minutes=11)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the server took too long building this one item", request=request)

    def failed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="one item's DTO would not build")

    class OneIdFails(httpx.AsyncBaseTransport):
        def __init__(self, fault) -> None:
            self.fault = fault

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Items" and request.url.params.get("ids") == "jf-1":
                return self.fault(request)
            return await transport.handle_async_request(request)

    for label, fault in (("a read that timed out", timed_out), ("a 500 for one item", failed)):
        await db.execute("DELETE FROM acquisition_task")
        await db.execute("UPDATE jellyfin_intake SET state = 'pending', reason = NULL")

        report = await intake.sweep_pending(db, _client(OneIdFails(fault)), _config())

        assert (report.ripe, report.enqueued, report.deferred) == (3, 2, 1), (label, report)
        assert await _task_keys(db) == ["jellyfin:jf-2", "jellyfin:jf-3"], label
        assert await db.fetchval(
            "SELECT resolved_key FROM jellyfin_intake WHERE state = 'pending'"
        ) == "jf-1", f"{label}: the key the server failed on stays pending"


async def test_an_outage_stops_the_sweep_at_the_first_key_and_asks_about_no_other(
    db, fake_jellyfin, webhook
):
    """`sweep_pending`'s "AN OUTAGE ALONE STOPS THE SWEEP", which the test above used to hand to
    `...a_membership_read_that_failed_leaves_the_events_pending...` -- a test whose non-empty pick
    fails at `/Library/MediaFolders` (decision 410) before any key is asked about, and whose single
    ripe key files the same report whether the loop stops or carries on. So the stop was asserted
    nowhere: made a `continue`, or `_about_the_key` made to answer True for everything, the whole
    suite stayed green while an outage spent up to the client's timeout on every ripe key in
    turn, until the job's budget ran out.

    An empty pick, so the per-key read is the first request; three ripe keys, so that "stopped"
    and "carried on" are different request logs. Every request fails with no status, the probe
    included, which is what a Jellyfin that is down looks like from here.
    [review cycle 3: M52-C3-SWEEP-06]
    """
    module, _ = fake_jellyfin
    _, delivered = webhook
    for item_id in ("jf-1", "jf-2", "jf-3"):
        await _record(db, await _emit(module, delivered, item_id=item_id))
    await _age_the_window(db, minutes=11)
    asked: list[str | None] = []

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items":
            asked.append(request.url.params.get("ids"))
        raise httpx.ConnectError("the household's Jellyfin is not answering", request=request)

    report = await intake.sweep_pending(db, _client(httpx.MockTransport(down)), _config())

    assert (report.ripe, report.enqueued, report.skipped, report.deferred) == (3, 0, 0, 3), report
    assert asked == ["jf-1"], f"the sweep went on asking a server that answers nothing: {asked}"
    assert "not answering" in report.blocked
    assert await _states(db) == {intake.PENDING: 3}
    assert await _task_keys(db) == []


async def test_an_add_spelled_the_way_the_webhook_plugin_spells_it_becomes_one_task(
    db, fake_jellyfin, webhook
):
    """§7.2's trigger on a server whose ids are GUIDs, which is every real one.

    The plugin renders `{{ItemId}}` and `{{SeriesId}}` dashed and `/Items` answers undashed; the
    server's binder parses either, so the membership read FOUND the item -- and the client then
    compared the two spellings as strings, answered None, and the sweep filed a real add
    `skipped` with "the server no longer holds this item", terminally, while the double's `jf-7`
    ids were spelled alike on both sides and kept every test green. Here the double holds GUID ids
    and spells them the plugin's way on the way out, so this is the first test in the suite that a
    string comparison can fail. Twice: once over an empty pick, and once with both libraries picked,
    where the same defect wore the other false reason, "library not picked".
    [review cycle 3: M52-C3-EVENTS-01, M52-C3-SWEEP-01, M52-C3-LIB-01, M52-C3-369-GUID-01,
    M52-C3-STATE-04]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    _hold_guid_titles(module)
    (movie,) = await _emit(module, delivered, item_id=GUID_MOVIE)
    burst = await _emit(module, delivered, item_id=GUID_SERIES, episodes=3)

    assert movie["ItemId"] == str(uuid.UUID(GUID_MOVIE)), "the plugin renders a GUID dashed"
    assert {body["SeriesId"] for body in burst} == {str(uuid.UUID(GUID_SERIES))}

    await _record(db, [movie, *burst])
    await _age_the_window(db, minutes=11)
    report = await intake.sweep_pending(db, _client(transport), _config())

    assert (report.ripe, report.enqueued, report.skipped) == (2, 2, 0), report
    assert await _task_keys(db) == sorted([f"jellyfin:{GUID_MOVIE}", f"jellyfin:{GUID_SERIES}"])
    assert await _states(db) == {intake.ENQUEUED: 4}

    await _record(db, [movie, *burst])
    await _age_the_window(db, minutes=11)
    picked = await intake.sweep_pending(
        db, _client(transport), _config(library_ids=["jf-lib-films", "jf-lib-shows"])
    )

    assert (picked.ripe, picked.already_queued, picked.skipped) == (2, 2, 0), picked
    assert await db.fetchval(
        "SELECT count(*) FROM jellyfin_intake WHERE state = 'skipped'"
    ) == 0, "a picked library's own add was recorded as a library nobody picked"


async def test_an_id_the_server_cannot_parse_costs_one_bounded_read_and_is_decided(
    db, fake_jellyfin, webhook
):
    """What a real server does with an id that is not a GUID: it DROPS it. Jellyfin binds `ids`
    through `CommaDelimitedCollectionModelBinder`, which logs the FormatException at debug and
    leaves the value out, and an `ids` emptied that way filters nothing -- so `ids=<junk>` with
    `Recursive=true`, no `IncludeItemTypes` (decision 369) and People and MediaStreams in `Fields`
    was a dump of the whole server, with no `Limit`, per junk key, per sweep. On a large library
    that read outruns the client's timeout, the timeout has no status, and the key heads every
    later sweep. The double matches strings and answered `[]`, so none of it was visible.

    The transport below binds `ids` the way the server does, and the id is the double's own
    `jf-1` -- which is not a GUID, so it is exactly what a real server would drop. The read has to
    be bounded, and what comes back still decides nothing about an item it does not name.
    [review cycle 3: M52-C3-EVENTS-04, M52-C3-SWEEP-05, M52-C3-LIB-05]
    """
    module, _ = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=11)

    library = [
        {"Id": uuid.uuid5(uuid.NAMESPACE_OID, str(n)).hex, "Name": f"title {n}", "Type": "Movie"}
        for n in range(2000)
    ]
    limits: list[str | None] = []

    def binder(request: httpx.Request) -> httpx.Response:
        parsed = set()
        for piece in request.url.params.get("ids", "").split(","):
            try:
                parsed.add(uuid.UUID(piece).hex)
            except ValueError:
                continue
        rows = [row for row in library if row["Id"] in parsed] if parsed else list(library)
        limits.append(request.url.params.get("Limit"))
        if request.url.params.get("Limit") is not None:
            rows = rows[: int(request.url.params["Limit"])]
        return _json(rows)

    report = await intake.sweep_pending(db, _client(httpx.MockTransport(binder)), _config())

    assert limits and all(limit is not None and int(limit) <= 2 for limit in limits), (
        f"the membership read asked for the whole server: Limit={limits}"
    )
    assert (report.ripe, report.skipped, report.enqueued) == (1, 1, 0)
    assert await db.fetchval("SELECT reason FROM jellyfin_intake") == intake.GONE_FROM_SERVER


async def test_a_server_row_carrying_a_nul_neither_wedges_the_sweep_nor_loses_the_add(
    db, fake_jellyfin, webhook
):
    """The class `intake._storable` closed for the operator's body, reached through the SERVER's.

    The sweep hands the row `/Items` answered to `queue.enqueue`, which dumps it into a `jsonb`
    column, and Postgres holds a NUL nowhere -- so a title whose overview came out of a tag or an
    NFO with one in it raised `UntranslatableCharacterError` out of the INSERT, past a handler that
    catches only `JellyfinError`. That key is the oldest window, so it headed every later sweep and
    the webhook intake was dead for the life of the install, with a traceback every `RETRY_AFTER`
    and nothing naming the title. The add is kept rather than refused: the NUL becomes U+FFFD,
    which no provider id can contain, so nothing the scrub touches can resolve to another title.
    [review cycle 3: M52-C3-STATE-01, M52-C3-SWEEP-03]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=12)
    await _record(db, await _emit(module, delivered, item_id="jf-2"))
    await _age_the_window(db, minutes=11)

    rows = {
        "jf-1": {**await _client(transport).item_in_libraries("jf-1", []),
                 "Overview": "a tagline\x00from an embedded tag"},
        "jf-2": await _client(transport).item_in_libraries("jf-2", []),
    }

    def answer(request: httpx.Request) -> httpx.Response:
        return _json([rows[request.url.params["ids"]]])

    report = await intake.sweep_pending(db, _client(httpx.MockTransport(answer)), _config())

    assert (report.ripe, report.enqueued) == (2, 2), report
    assert await _task_keys(db) == ["jellyfin:jf-1", "jellyfin:jf-2"]
    overview = await db.fetchval(
        "SELECT payload->'item'->>'Overview' FROM acquisition_task WHERE key = 'jellyfin:jf-1'"
    )
    assert overview == "a tagline�from an embedded tag"


# --- the library pick, which had never had a reader (decision 364) --------------------


async def test_an_add_in_a_library_the_admin_did_not_pick_is_recorded_and_enqueues_nothing(
    db, fake_jellyfin, webhook
):
    """The row `...an-add-outside-the-picked-libraries-is-recorded-not-acquired`, both ways round.

    §6.6 gives the admin a library pick and nothing has ever read it, so §7.2's heading has meant
    "every add on the server" and a deselected library would be billed for anyway. "Christmas
    2019" is the household's own footage; it resolves to no title at all, and it is exactly the
    library nobody wants an acquisition job for.

    MEMBERSHIP IS THE SERVER'S FACT and is asked for over HTTP (`ParentId`), never read off the
    event: the template is operator-authored, and a boundary drawn with a field an operator can
    forget is not a boundary. What is recorded is the reason, in the app's own words, because
    until M5.7 renders it that row is the only thing that tells an operator why a webhook they
    configured did nothing.

    The enqueue in the second half is a TASK and not an acquisition: jf-x carries no provider id,
    so stage 1 will park it with "no provider id" (decision 323). The pick decides whether work is
    filed; stage 1 decides whether it can be done, and this row is about the first.
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-x"))
    await _age_the_window(db, minutes=11)

    films_only = await intake.sweep_pending(
        db, _client(transport), _config(library_ids=["jf-lib-films"])
    )

    assert (films_only.ripe, films_only.skipped, films_only.enqueued) == (1, 1, 0)
    assert await _task_keys(db) == []
    recorded = await db.fetchrow("SELECT state, reason FROM jellyfin_intake")
    assert (recorded["state"], recorded["reason"]) == (intake.SKIPPED, intake.LIBRARY_NOT_PICKED)

    await _record(db, await _emit(module, delivered, item_id="jf-x"))
    await _age_the_window(db, minutes=11)
    picked = await intake.sweep_pending(
        db, _client(transport), _config(library_ids=["jf-lib-films", "jf-lib-home"])
    )

    assert (picked.ripe, picked.enqueued) == (1, 1)
    assert await _task_keys(db) == ["jellyfin:jf-x"]


async def test_a_membership_read_that_failed_leaves_the_events_pending_for_the_next_sweep(
    db, fake_jellyfin, webhook
):
    """Decision 364's last sentence, and the failure it exists to prevent: "this id is in no
    library the admin picked" and "nobody could ask" are the same silence and opposite facts.

    Read as the first, a Jellyfin outage would record every add the household made during it as
    deliberately unwanted - silently, permanently, and with the reason naming a setting that is
    not even set. So the rows stay pending, the sweep says what stopped it, and the next tick
    asks again.
    """
    module, _ = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=12))
    await _age_the_window(db, minutes=11)

    report = await intake.sweep_pending(db, _unreachable(), _config(library_ids=["jf-lib-shows"]))

    assert (report.ripe, report.deferred, report.enqueued, report.skipped) == (1, 1, 0, 0)
    assert "not answering" in report.blocked
    assert await _states(db) == {intake.PENDING: 12}
    assert await _task_keys(db) == []


async def test_an_item_the_server_no_longer_holds_is_not_recorded_as_a_library_nobody_picked(
    db, fake_jellyfin, webhook
):
    """The other way a membership read comes back empty, and the reason it has a sentence of its
    own: with no pick to fail, an id the server will not answer for is an item that left the
    library between the event and the sweep - a household that added a film and deleted it again
    inside ten minutes, or a template pointing at another server. Telling that operator that a
    library was not picked would send them to a setting that is not set.

    The empty answer is stubbed at the transport rather than emitted by the double, because the
    double REFUSES to invent one: `_item` 404s an unknown id on purpose, so that a burst can only
    be emitted for a series this server actually holds. The body is still the double's; what is
    doubled here is the server's own `/Items`, answering the way it answers for an id that is
    gone.
    """
    module, _ = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=11)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    report = await intake.sweep_pending(db, _client(httpx.MockTransport(empty)), _config())

    assert (report.ripe, report.skipped, report.enqueued) == (1, 1, 0)
    assert await db.fetchval("SELECT reason FROM jellyfin_intake") == intake.GONE_FROM_SERVER
    assert await _task_keys(db) == []


async def test_a_picked_library_the_server_no_longer_lists_holds_back_only_what_it_might_hold(
    db, fake_jellyfin, webhook
):
    """Decision 410. An admin deletes and re-creates a library in Jellyfin, or points the
    connector at another install -- and the pick survives both on purpose. A real server answers
    a `ParentId` it cannot find with 400 (`GetParentItem` throws "Invalid parent id"), so the
    membership read raised on the FIRST picked scope before asking the next one, the 400 was filed
    as a fact about the key, and every add stayed pending for ever: one read per key per sweep,
    oldest first, including adds sitting in a picked library that still exists.

    A stale id is a fault in the PICK, and the sweep learns so once, from `/Library/MediaFolders`,
    rather than from a 400 per key. An add found in a live picked library is filed; one found in
    none of them stays pending, because it may be in the library the stale id used to name, and
    the reason names that id. The pick is never narrowed to the libraries that survived, and never
    widened to the whole server when none did -- the second of those would bill the household for
    every library it deselected. [review cycle 3: M52-C3-LIB-02, M52-C3-SWEEP-04]
    """
    module, transport = fake_jellyfin
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _record(db, await _emit(module, delivered, item_id="jf-x"))
    await _age_the_window(db, minutes=11)

    report = await intake.sweep_pending(
        db, _client(transport), _config(library_ids=["jf-lib-gone", "jf-lib-films"])
    )

    assert (report.ripe, report.enqueued, report.deferred, report.skipped) == (2, 1, 1, 0), report
    assert "jf-lib-gone" in report.blocked
    assert await _task_keys(db) == ["jellyfin:jf-1"], "a live picked library's add is still filed"
    assert await db.fetchval(
        "SELECT resolved_key FROM jellyfin_intake WHERE state = 'pending'"
    ) == "jf-x"

    asked: list[str] = []

    class Counting(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            return await transport.handle_async_request(request)

    only_stale = await intake.sweep_pending(
        db, _client(Counting()), _config(library_ids=["jf-lib-gone"])
    )

    assert (only_stale.ripe, only_stale.deferred, only_stale.skipped) == (1, 1, 0), only_stale
    assert "/Items" not in asked, "a pick with no live library may not be read as the whole server"
    assert await _task_keys(db) == ["jellyfin:jf-1"]


# --- the fallback: the delta poll -----------------------------------------------------


@pytest.fixture
async def configured(db, secrets_key, fake_jellyfin):
    """The connector as an admin left it, with a watermark predating the whole library.

    Through `save_jellyfin` and `load_jellyfin` rather than a constructed config, because the
    watermark is `connector_config` state (decision 366) and the poll's contract is that it reads
    what the last poll wrote - a test holding a config object in memory would never exercise the
    sealed read-modify-write that carries it.
    """
    module, transport = fake_jellyfin
    await save_jellyfin(db, url=JELLYFIN_URL, api_key=module.API_KEY)
    return module, _client(transport)


async def test_the_delta_poll_enqueues_the_set_once_and_the_next_poll_enqueues_nothing(
    db, configured
):
    """The inherited row's second clause: "with the webhook plugin absent the 15-minute
    `DateCreated > last_sync` delta poll enqueues the same set once and does not re-enqueue titles
    from a previous poll".

    Both mechanisms are asserted rather than either (plan C5). The second poll reads NOTHING,
    which is the watermark doing its work - a poll that re-read the library every fifteen minutes
    and leaned on `UNIQUE (kind, key)` to stay quiet would pass a task count and still walk the
    household's whole corpus forever.
    """
    _, client = configured
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)

    first = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert first.since == BEFORE_THE_LIBRARY
    assert (first.read, first.enqueued, first.already_queued) == (7, 7, 0)
    assert len(await _task_keys(db)) == 7

    second = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (second.read, second.enqueued) == (0, 0), "the watermark, not the queue, stopped it"
    assert second.since == first.until, "the second poll starts where the first one finished"
    assert len(await _task_keys(db)) == 7


async def test_the_delta_poll_is_scoped_to_the_libraries_the_admin_picked(db, configured):
    """The same boundary as the webhook's, on the other path - which is the last sentence of
    `...an-add-outside-the-picked-libraries-is-recorded-not-acquired`. A pick that held for one
    intake path and not the other would bill the household for a deselected library every fifteen
    minutes while the webhook politely declined it."""
    _, client = configured
    await save_jellyfin(
        db, library_ids=["jf-lib-films"], delta_watermark=BEFORE_THE_LIBRARY
    )

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.enqueued == 4
    assert await _task_keys(db) == [
        "jellyfin:jf-1", "jellyfin:jf-2", "jellyfin:jf-3", "jellyfin:jf-8"
    ], "the shows and the household's own footage are outside the pick"


async def test_the_poll_files_an_add_whose_file_is_older_than_the_last_poll(db, configured):
    """§7.2's fallback, for the adds it most often exists to catch. By default Jellyfin stamps
    `DateCreated` from the FILE (`UseFileCreationTimeForDateAdded = true;`,
    `ResolverHelper.SetDateCreated`: `var dateCreated = info.CreationTimeUtc;`), and the instant an
    item arrives is its `DateLastSaved`, which `MinDateLastSaved` selects on. So a film copied in
    with its mtime kept, an archive unpacked with its own dates, the household's camera footage, or
    any add first saved by the default twelve-hourly scheduled scan, came back from the server --
    and the poll re-filtered it on `DateCreated > since`, dropped it uncounted, reported a quiet
    household and advanced the watermark past it for good. `DateCreated` never changes, so no later
    poll recovered it either.

    The last poll here ran at the start of 2024; Tampopo (a 2016 file) and the Christmas footage
    (a 2019 file) both arrived after it. [M5.2 review cycle 4: M52-C4-REST-02, M52-C4-TTA-02]
    """
    _, client = configured
    await save_jellyfin(db, delta_watermark=datetime(2024, 1, 1, tzinfo=UTC))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (report.read, report.enqueued) == (2, 2), report.as_dict()
    assert await _task_keys(db) == ["jellyfin:jf-8", "jellyfin:jf-x"]


async def test_a_row_saved_just_before_a_poll_and_visible_only_after_its_read_is_not_lost(
    db, configured, fake_jellyfin
):
    """The server stamps `DateLastSaved` BEFORE the row is visible: 10.10's `RunMetadataSavers`
    sets `item.DateLastSaved = DateTime.UtcNow;` and then awaits the item's image work before
    `SaveItems` commits, and 10.11 still commits after the stamp. A row stamped just before a poll
    began and committed after its read was missed by that poll -- and the watermark it then wrote
    was the instant the read began, so every later poll asked `>=` that instant and excluded the
    row for good.

    Written `WATERMARK_OVERLAP` behind that instant, the next poll reads the row, and the re-read
    of the margin costs what `UNIQUE (kind, key)` absorbs. The late commit is scripted at the
    transport -- the double has no transactions to be late with -- and every answer is the
    double's. [M5.2 review cycle 4: M52-C4-REST-04]
    """
    module, transport = fake_jellyfin
    _, client = configured
    now = await db.fetchval("SELECT now()")
    module.ITEMS[0]["DateLastSaved"] = module._stamp(now - timedelta(seconds=2))
    await save_jellyfin(db, delta_watermark=now - timedelta(minutes=1))

    class NotYetCommitted(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await transport.handle_async_request(request)
            await response.aread()
            body = json.loads(response.content)
            body["Items"] = [row for row in body["Items"] if row["Id"] != "jf-1"]
            body["TotalRecordCount"] = len(body["Items"])
            return httpx.Response(200, json=body)

    first = await intake.poll_delta(db, _client(NotYetCommitted()), await load_jellyfin(db))
    assert (first.read, first.enqueued) == (0, 0), "the row was not visible to this read"

    second = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (second.read, second.enqueued) == (1, 1), second.as_dict()
    assert await _task_keys(db) == ["jellyfin:jf-1"]


async def test_a_poll_whose_pick_names_a_library_the_server_lost_files_the_rest_and_holds_on(
    db, configured
):
    """Decision 410 on the fallback path. The delta walk raised on the first picked scope a real
    server 400s, so a stale id anywhere in the pick filed nothing at all -- not even the adds in
    the libraries that still exist -- and raised `GET /Items -> 400` every fifteen minutes, which
    names neither the pick nor the id.

    The walk now reads only the scopes the server still lists and files what they returned, which
    `UNIQUE (kind, key)` makes safe to repeat. The watermark holds, because the stale scope was not
    read and a watermark advanced past it would step over whatever that library gained; and the
    poll still RAISES, naming the id, because a raised job is the line an operator greps for.
    [review cycle 3: M52-C3-LIB-02]
    """
    _, client = configured
    await save_jellyfin(
        db, library_ids=["jf-lib-films", "jf-lib-gone"], delta_watermark=BEFORE_THE_LIBRARY
    )

    with pytest.raises(JellyfinError, match="jf-lib-gone"):
        await intake.poll_delta(db, client, await load_jellyfin(db))

    assert await _task_keys(db) == [
        "jellyfin:jf-1", "jellyfin:jf-2", "jellyfin:jf-3", "jellyfin:jf-8"
    ], "the live picked library's adds are filed whatever the stale one does"
    assert (await load_jellyfin(db)).delta_watermark == BEFORE_THE_LIBRARY


async def test_a_server_row_carrying_a_nul_neither_freezes_the_watermark_nor_loses_the_add(
    db, configured, fake_jellyfin
):
    """The delta half of the same class. `poll_delta` raised out of the INSERT before its
    last-statement watermark write, so a poll that met the row re-paged an ever-growing superset
    every fifteen minutes, filed the rows the server happened to list ahead of it, and never filed
    an add made after it -- decision 366's "advances only on a completed read" become a livelock
    rather than a pause. [review cycle 3: M52-C3-STATE-01, M52-C3-SWEEP-03]
    """
    module, client = configured
    module.ITEMS[2]["Overview"] = "a tagline\x00from an embedded tag"
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.enqueued == 7, report.as_dict()
    assert (await load_jellyfin(db)).delta_watermark == report.until
    assert await db.fetchval(
        "SELECT payload->'item'->>'Overview' FROM acquisition_task WHERE key = 'jellyfin:jf-3'"
    ) == "a tagline�from an embedded tag"


async def test_a_server_row_no_index_can_hold_is_counted_and_the_poll_goes_on(
    db, configured, fake_jellyfin
):
    """What scrubbing cannot mend. The key is `jellyfin:<Id>`, and `acquisition_task`'s
    `UNIQUE (kind, key)` is a btree whose tuples Postgres caps at a few kilobytes -- so a row whose
    `Id` is past that (a proxy's garbage, a server bug) raised `ProgramLimitExceededError` out of
    the INSERT, and the poll froze exactly as it did on a NUL. One row the schema cannot hold is
    counted beside `unkeyable` and the poll goes on, so no single row can stop the rest.
    [review cycle 3: M52-C3-STATE-01, M52-C3-SWEEP-03]
    """
    module, client = configured
    module.ITEMS[0]["Id"] = secrets.token_urlsafe(9000)
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (report.enqueued, report.as_dict().get("unstorable")) == (6, 1), report.as_dict()
    assert (await load_jellyfin(db)).delta_watermark == report.until, (
        "one row the schema cannot hold may not freeze the watermark"
    )


async def test_a_poll_racing_an_origin_move_does_not_write_the_old_servers_watermark(
    db, configured
):
    """Decision 366: the watermark is "dropped on a Jellyfin origin move". The poll's last
    statement is a save that named no URL, so the merge computed `moved` against whatever was
    stored at that instant -- and an admin who moved the connector while the poll was paging had
    their move's drop overwritten by an instant belonging to the OLD server's read. The new
    server's first poll then started there, and every title it held from before that instant was
    never offered by this path. The race is replayed deterministically: the poll holds the config
    it loaded, and the move lands before its save. [review cycle 3: M52-C3-STATE-05]
    """
    _, client = configured
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    before_the_move = await load_jellyfin(db)
    await save_jellyfin(db, url="http://another-jellyfin.test:8096", api_key="another-key")
    assert (await load_jellyfin(db)).delta_watermark is None, "the move dropped it"

    report = await intake.poll_delta(db, client, before_the_move)

    assert report.enqueued == 7, "the old server's read still filed what it read"
    assert (await load_jellyfin(db)).delta_watermark is None, (
        "the new server inherited a watermark from the old server's read"
    )


async def test_a_watermark_in_the_future_is_read_as_never_polled(db, configured, fake_jellyfin):
    """A database clock that ran ahead and was stepped back leaves a watermark later than now.
    The poll asked for `DateCreated > <tomorrow>`, read nothing, and wrote `started` -- moving the
    watermark BACK to the present without having read the instants between the last honest poll
    and now, so every title added in them was never offered by this path. The poll can see the
    state (`since > started`), which is what separates it from decision 404's skew, and it reads
    it the way `registry._instant` reads a stored instant it cannot trust: as never polled.
    [review cycle 3: M52-C3-STATE-07]
    """
    module, client = configured
    now = await db.fetchval("SELECT now()")
    # Two seconds on, because the double spells whole seconds and this database was migrated a
    # moment ago: truncated to the second, "now" can fall before the floor the poll falls back to.
    # `DateLastSaved` and not `DateCreated`, because the server's save is what the poll reads
    # (decision 409).
    module.ITEMS[0]["DateLastSaved"] = module._stamp(now + timedelta(seconds=2))
    await save_jellyfin(db, delta_watermark=now + timedelta(days=1))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.enqueued == 1, report.as_dict()
    assert await _task_keys(db) == ["jellyfin:jf-1"]
    assert report.since < now, "the poll read from a floor it could trust"


def _deaf_to(parameter: str, inner: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
    """The double's own server, with one query parameter dropped on the way in.

    A caching proxy keyed on the path, a gateway that rewrites query strings, a portal in front of
    the household's Jellyfin: the fault `_created_walk`'s own bounds are written against is "a
    server ignoring `StartIndex`, a proxy serving page one", and this is that fault aimed at the
    one parameter decision 369's absolute clause rested on. The rows are still the double's --
    twelve of them one season of one series, which is what §7.2 forbids twelve jobs for.
    """

    class Deaf(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url = request.url.copy_with(params=request.url.params.remove(parameter))
            return await inner.handle_async_request(
                httpx.Request(
                    request.method, url, headers=request.headers, content=request.content
                )
            )

    return Deaf()


async def test_a_server_that_ignores_the_type_filter_files_no_task_for_an_episode(
    db, configured, fake_jellyfin
):
    """Decision 369's clause is absolute -- "no acquisition task is ever keyed on an episode" --
    and on this path it rested entirely on a remote server honouring `IncludeItemTypes`.

    That is the one thing about a read this module does not otherwise take on trust: the walk
    behind it has paging bounds precisely because a server may ignore what it was asked. Handed the
    season anyway, `poll_delta` filed one task per EPISODE -- `key_for_item` takes `Id` first and never
    asks what the item is -- and every one of them burns a `(kind, key)` the queue will not reopen
    while the real adds behind it wait out a sequential drain.

    `ITEM_TYPES` is unchanged (decision 369) and the query still narrows: what is doubled here is
    only the loss of the narrowing. [review cycle 2: m52-c2-delta-episode-filter]
    """
    _, transport = fake_jellyfin
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    deaf = _client(_deaf_to("IncludeItemTypes", transport))

    report = await intake.poll_delta(db, deaf, await load_jellyfin(db))

    assert report.read > 7, "the deaf server really did answer with more than the seven titles"
    assert report.not_a_title >= 12, "a whole season came back and was refused, not swallowed"
    assert not [key for key in await _task_keys(db) if "-e" in key], (
        "no acquisition task may ever be keyed on an episode"
    )
    assert await _task_keys(db) == [
        "jellyfin:jf-1", "jellyfin:jf-2", "jellyfin:jf-3", "jellyfin:jf-6",
        "jellyfin:jf-7", "jellyfin:jf-8", "jellyfin:jf-x",
    ], "the titles the household really did add are still filed, one each"


async def test_a_poll_whose_read_failed_leaves_the_watermark_where_it_was(db, configured):
    """Exit-criterion check 5, and the reason the advance is the last statement in the poll.

    A watermark that moved on a read that did not finish is an add nobody ever looks for again:
    the next poll asks about the instants AFTER the page that was dropped, and §7.2's trigger is
    silently gone for that title for ever. `items_created_since` refuses to return short for the
    same reason, so "the read completed" is simply "it returned".

    It RAISES rather than reporting a quiet poll, because `worker.py`'s job runner records a
    raised job with its reason and a swallowed outage looks exactly like a healthy household with
    nothing new - which is the shape M4.11 finding 3 records one connector over.
    """
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    cfg = await load_jellyfin(db)

    with pytest.raises(JellyfinError):
        await intake.poll_delta(db, _unreachable(), cfg)

    assert (await load_jellyfin(db)).delta_watermark == BEFORE_THE_LIBRARY
    assert await _task_keys(db) == []


async def test_a_poll_cut_part_way_through_its_enqueues_leaves_the_watermark_where_it_was(
    db, configured, monkeypatch
):
    """`poll_delta`'s last statement, held against the order its own comment argues for: "Last, and
    never before the enqueues: a watermark written first is an add lost to a crash, and this is the
    one write in this module whose loss nothing later re-derives."

    The neighbouring ordering claim in the same function -- `started` taken BEFORE the read -- has
    a test. This one had none, and the poll passes every assertion in the suite with the two
    statements swapped: exit-criterion check 5 fails the READ, which returns before the loop and
    before the save, so it cannot tell the orders apart either.

    The interruption is the one the job has. `_jellyfin_delta_poll` holds a pool connection with no
    enclosing transaction under a 300-second budget, so a poll that read four hundred adds is
    cancellable mid-loop; the tasks already filed are committed, which is why only the watermark
    can lose them. Written first it is already past every instant the loop had not reached, and
    `DateCreated > since` is false for those titles for ever. [review cycle 2: m52-rev2-delta-03]
    """
    _, client = configured
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    real = pipeline.enqueue_item
    filed = 0

    async def enqueue_until_cut(conn, item):
        nonlocal filed
        if filed == 2:
            raise RuntimeError("the poll was cut at its budget")
        filed += 1
        return await real(conn, item)

    monkeypatch.setattr(pipeline, "enqueue_item", enqueue_until_cut)
    with pytest.raises(RuntimeError):
        await intake.poll_delta(db, client, await load_jellyfin(db))

    assert len(await _task_keys(db)) == 2, "what the loop did file before the cut is committed"
    assert (await load_jellyfin(db)).delta_watermark == BEFORE_THE_LIBRARY, (
        "a watermark written first steps over every add the loop never reached"
    )

    monkeypatch.undo()
    again = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (again.read, again.enqueued, again.already_queued) == (7, 5, 2), (
        "the next poll asks about the same instants and files the five it lost"
    )
    assert len(await _task_keys(db)) == 7


async def test_a_poll_does_not_mint_the_webhook_token_no_admin_would_ever_see(db, configured):
    """Decision 416, at the caller that reached the merge first on every upgraded install.

    `webhook_token` is new in this milestone, so a connector configured before it loads
    configured and EMPTY -- which is exactly the state the `configured` fixture leaves, because
    it saves the way every caller but `put_jellyfin` saves. `jellyfin-delta-poll` is a new job
    name with no `job_run` history, so `due()` fires it on the worker's first tick after boot,
    and this poll's last statement is a `save_jellyfin`. Minting there sealed the value into the
    database and handed it to a worker that dropped it: decision 332 gives the token ONE
    appearance, there is no rotation route anywhere in this app, and `POST /events/jellyfin` then
    answers 401 to every real delivery for ever while §6.6's card says a token exists.
    [review cycle 1: m52-rev-delta-01, m52-rev1-token-01]
    """
    _, client = configured
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    assert (await load_jellyfin(db)).webhook_token == "", "the state an upgrade arrives in"

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.enqueued == 7, "the poll did its own work"
    assert (await load_jellyfin(db)).webhook_token == "", (
        "a value shown once may not be spent by a job nobody is watching"
    )


async def test_the_watermark_names_the_instant_the_read_began_and_not_the_one_it_ended(
    db, configured
):
    """`poll_delta` takes `started` BEFORE the read, and its own comment is the whole claim:
    after the read it would name an instant the read could not have seen the whole of.

    An add made while a walk of eleven thousand titles is still paging is returned by neither
    this read nor -- with the later instant stored -- any read after it, because
    `DateCreated > since` is then false for ever. That is the "add nobody ever looks for again"
    this module refuses two paragraphs higher, and nothing in the suite could tell the two orders
    apart: every existing assertion is relational (`second.since == first.until`) and holds under
    both. So the read is made to take a measurable time and the watermark is held against the
    instant it began. [review cycle 1: m52-rev-delta-04]
    """
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)

    async def slowly(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    began = await db.fetchval("SELECT now()")
    report = await intake.poll_delta(db, _client(httpx.MockTransport(slowly)), await load_jellyfin(db))
    ended = await db.fetchval("SELECT now()")

    took = (ended - began).total_seconds()
    assert took >= 0.5, "the read really did take time to answer"
    assert (report.until - began).total_seconds() < took / 2, (
        "the watermark names an instant after the read the poll cannot have seen the whole of"
    )
    assert (await load_jellyfin(db)).delta_watermark == report.until


async def test_a_first_poll_on_a_fresh_install_enqueues_nothing_it_already_has(db, configured):
    """Decision 366's floor, at the layer where it is observable rather than at the one where it
    is computed: an install that has never polled starts from `min(applied_at)` - this database's
    own creation instant - and not from epoch.

    From epoch, the first poll of a household with eleven thousand titles files an acquisition
    task for every one of them: both mechanisms absorb the duplicates and it is still a whole
    library's worth of work nobody asked for, on the one boot where the operator is watching.
    """
    _, client = configured
    assert (await load_jellyfin(db)).delta_watermark is None

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (report.read, report.enqueued) == (0, 0)
    assert await _task_keys(db) == []
    # Decision 412: the instant this install gained §7.2's fallback, which on a fresh install is
    # the same migration run as `min(applied_at)` and differs from it by the seconds that run took.
    gained = await db.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )
    assert report.since == gained
    assert (await load_jellyfin(db)).delta_watermark == report.until, (
        "a completed read writes down where it got to, so the next poll is a delta and not a walk"
    )


async def test_a_first_poll_after_an_upgrade_reads_from_the_upgrade_and_not_the_install(
    db, configured, fake_jellyfin
):
    """Decision 412, amending decision 366's floor for the install it did not describe. Every
    install that exists today meets this milestone as an UPGRADE, and on an upgrade
    `min(applied_at)` is when 0001 ran -- months before M5.2 arrived -- so the first poll filed a
    task for every title the household had added to Jellyfin since installing the app, each of
    them already resolved by M4.11's sweep, and every genuine add waited behind them. The floor is
    the instant this install gained the fallback: 0025's own `applied_at`. What was added before
    it is the full sweep's unmatched report's to hand on (decision 370), not this poll's.
    [review cycle 3: M52-C3-STATE-02]
    """
    module, client = configured
    await db.execute(
        "UPDATE schema_migration SET applied_at = applied_at - interval '180 days'"
        " WHERE version < '0025'"
    )
    gained = await db.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )
    installed = await db.fetchval("SELECT min(applied_at) FROM schema_migration")
    # The server's save of the item, which is what the poll reads (decision 409): a stamp on
    # `DateCreated` alone would leave this assertion true whatever the floor was.
    module.ITEMS[0]["DateLastSaved"] = module._stamp(installed + timedelta(days=30))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.since == gained, "the first poll read from when this install was created"
    assert (report.read, report.enqueued) == (0, 0), (
        "a title added between the install and the upgrade was filed as a new add"
    )


async def test_the_two_paths_file_one_task_for_one_title(db, configured, webhook):
    """§1's argument for why the webhook and the poll are one milestone: "'Fallback' means the
    delta poll must produce the same enqueue the webhook would have, for the same title, without
    duplicating it".

    The two feeders are exercised over one title in one test because that is the only place the
    claim can fail - two enqueue paths that spelled the key differently would each look correct
    alone and would file two tasks for one film the moment both ran, which is the ordinary state
    on an install whose plugin works and whose poll is also on.
    """
    module, client = configured
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=11)
    assert (await intake.sweep_pending(db, client, await load_jellyfin(db))).enqueued == 1

    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.already_queued >= 1, "the poll re-offered the title the webhook had filed"
    assert await _task_keys(db) == [f"jellyfin:jf-{n}" for n in (1, 2, 3, 6, 7, 8, "x")]


# --- §9's re-scan gotcha --------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR`, and therefore `settings().artifacts_dir`, under this test's own tmp_path.

    The same fixture `test_acquire_pipeline.py` defines and for the same reason: `stages.
    active_store` reaches for `settings().artifacts_dir` the way `worker.py` does, so the path
    has to arrive through the environment rather than through an argument that does not exist on
    the real path.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def owned(db, data_dir, tmp_path):
    """A household that already owns Heat, placed, the way the nightly sweep and the Cold Tower
    would have left it - and an active bundle, because stage 9 has to have a basis to place
    against."""
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    await db.execute(
        "UPDATE title SET is_owned = true, placement = 'cold_tower', placement_bundle = 'test-v1'"
        " WHERE id = 1"
    )
    return report


async def _own_and_place(db) -> None:
    """The four films the double's `Films` library holds, as a household that has had them for
    years would have them: owned, and placed by the sweep and the Cold Tower. Placed is the half
    decision 411 reads -- a title with a placement has nothing left for §8 to add."""
    await db.execute(
        "UPDATE title SET is_owned = true,"
        "       placement = CASE WHEN placement = 'unplaced' THEN 'warm' ELSE placement END,"
        "       placement_bundle = coalesce(placement_bundle, 'test-v1')"
        " WHERE id = ANY($1::int[])", [1, 2, 3, 8],
    )


async def test_a_re_scan_of_a_title_the_household_owns_mints_nothing_and_bills_nothing(
    db, owned, configured
):
    """The inherited row's fourth clause, and the plan's own risks section: "Jellyfin's
    `DateCreated` is the server's, not the file's. A library re-scan can re-stamp items the
    household has had for years ... the JOB must then find the title already owned and exit at
    stage 1 rather than re-acquiring. Assert that, or a re-scan bills the household for its whole
    library."

    EVERY RE-STAMPED TASK IS DRAINED, AND EACH ONE ENDS AT STAGE 1 (decision 411). This test used
    to walk one of the four and accept `ready` at stage 10 as the pass, with "no paid work" resting
    on stage 6 still being a stub -- so it passed against a pipeline that walked every owned title
    through every stage, stamped §8's "new - model placement, no crowd data" badge over warm corpus
    titles, and wrote over the `(2, parked)` row `placement/reconcile._park_thin` leaves for a thin
    title, which `_PARK`'s `ON CONFLICT DO NOTHING` can never put back. So the board is asserted
    byte-identical across the whole drain, thin row included, and nothing about the walk depends
    on which stages have bodies yet. [review cycle 3: m52-c3-own-01]

    The watermark is wound back by hand, which is what a re-scan looks like from this side: the
    server re-saves the rows and the same rows come back over a boundary that has not moved. (The
    plan's premise quoted above is wrong about the stamp -- `DateCreated` is the FILE's, and the
    poll reads `DateLastSaved` (decision 409) -- and right about the risk, which is what this
    asserts.)
    """
    _, client = configured
    await _own_and_place(db)
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status, reason) VALUES (2, 2, 'parked', $1)"
        " ON CONFLICT (title_id) DO NOTHING",
        "placed with 4 of 10 feature blocks - missing keyword; queued for stage 2 enrichment",
    )
    titles_before = await db.fetchval("SELECT count(*) FROM title")
    board_before = [
        dict(row) for row in await db.fetch("SELECT * FROM acquisition_job ORDER BY title_id")
    ]
    await save_jellyfin(
        db, library_ids=["jf-lib-films"], delta_watermark=BEFORE_THE_LIBRARY
    )
    assert (await intake.poll_delta(db, client, await load_jellyfin(db))).enqueued == 4

    leased = await queue.lease(db, [pipeline.TASK_KIND], limit=8)
    assert len(leased) == 4
    for task in leased:
        walk = await pipeline.run_task(db, task)
        assert (walk.status, walk.stage, walk.stages_run) == ("parked", 1, ["identify"]), (
            walk.as_dict()
        )
        assert walk.reason.startswith("this item resolves to title "), walk.reason

    assert await db.fetchval("SELECT count(*) FROM title") == titles_before
    assert await db.fetchval("SELECT count(*) FROM title WHERE origin = 'acquired'") == 0
    assert [
        dict(row) for row in await db.fetch("SELECT * FROM acquisition_job ORDER BY title_id")
    ] == board_before, "a re-stamp rewrote a board row it has no business touching"

    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    again = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (again.read, again.enqueued, again.already_queued) == (4, 0, 4)
    assert await db.fetchval("SELECT count(*) FROM title") == titles_before


async def test_a_re_offered_title_already_placed_waits_behind_a_genuine_add(
    db, owned, configured, webhook
):
    """Decision 411's second half. A task that exits at stage 1 still takes one of the drain's
    `DRAIN_LIMIT` slots, and `queue.lease` orders by `(priority, id)` with both feeders filing at
    the default -- so a re-scan's re-stamps, or the first poll of an upgraded install, queued every
    owned title in FIFO ahead of a film the household had really just added, and at eight tasks
    per thirty-minute drain that add waited hours, or days on a large library, with nothing saying
    why. A re-offer of a title already placed is filed below every genuine add instead.
    [review cycle 3: m52-c3-own-02]
    """
    module, client = configured
    _, delivered = webhook
    await _own_and_place(db)
    await save_jellyfin(db, library_ids=["jf-lib-films"], delta_watermark=BEFORE_THE_LIBRARY)
    assert (await intake.poll_delta(db, client, await load_jellyfin(db))).enqueued == 4

    await save_jellyfin(db, library_ids=[])
    await _record(db, await _emit(module, delivered, item_id="jf-x"))
    await _age_the_window(db, minutes=11)
    assert (await intake.sweep_pending(db, client, await load_jellyfin(db))).enqueued == 1

    (first,) = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    assert first.key == "jellyfin:jf-x", "a genuine add waited behind four titles already placed"


# --- decision 362, from the feeders' side ---------------------------------------------


async def _ownership(db) -> dict[int, tuple[bool, datetime | None]]:
    """Every title's ownership as the two columns a write to it moves, which is
    `ops/m52_exit_criterion.py`'s `_ownership` and for its reason: a feeder that re-stamped
    `owned_checked_at` on every tick without changing anybody's flag is still writing the column
    decision 362 keeps off it."""
    return {
        row["id"]: (row["is_owned"], row["owned_checked_at"])
        for row in await db.fetch("SELECT id, is_owned, owned_checked_at FROM title")
    }


def _moved(before: dict, after: dict) -> list[int]:
    return sorted(
        title for title in before.keys() | after.keys() if before.get(title) != after.get(title)
    )


async def test_neither_feeder_writes_the_ownership_column_and_the_full_sweep_visibly_does(
    db, owned, configured, webhook
):
    """Exit-criterion check 11 -- "no ownership write from the delta path at all" -- run in CI.

    THE ROW'S OWN COMMENT SAID AN ABSENCE HAS NO RUN THAT OBSERVES IT, and check 11 is one: it
    diffs `(is_owned, owned_checked_at)` across the polls and deliveries and shows, in the same
    run, that the differ can see the full sweep's write. Only the owner runs that script, so the
    clause was held in CI by `test_seen_sync.py`'s literal walk alone -- which cannot see a new
    CALLER of the writers that already ship. A poll that "kept ownership fresh on add" with
    `resolve.upsert_items`, then `prune_missing_items` and `seen._falsify_ownership` over what it
    read, wrote no literal at all, un-owned every title outside its fifteen-minute window, pruned
    the copy map, and left every test green. [review cycle 3: m52-c3-own-03]

    So the household here is one a completed sweep has already read, the delta window covers only
    part of its library, and the webhook delivers a film and a season. `owned_checked_at` is wound
    back two hours first -- a sweep that last refreshed it before an outage or a worker restart --
    because `resolve.upsert_item`'s hourly guard would otherwise make a delta-side upsert a no-op
    on a title stamped a moment ago, and this test would pass by timing. The last half is the
    instrument's own control: the full sweep, over a mirror that lost Prisoners, moves every title
    it read and un-owns the one it did not, so an empty diff above is a fact and not a blind spot.
    """
    module, client = configured
    _, delivered = webhook
    assert (await seen.sync_all(db, client)).resolve["matched_titles"] == 6
    await db.execute(
        "UPDATE title SET owned_checked_at = owned_checked_at - interval '2 hours'"
        " WHERE owned_checked_at IS NOT NULL"
    )
    before = await _ownership(db)

    await save_jellyfin(db, delta_watermark=datetime(2022, 1, 1, tzinfo=UTC))
    delta = await intake.poll_delta(db, client, await load_jellyfin(db))
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _record(db, await _emit(module, delivered, item_id="jf-7", episodes=3))
    await _age_the_window(db, minutes=11)
    swept = await intake.sweep_pending(db, client, await load_jellyfin(db))

    assert (delta.read, delta.enqueued) == (4, 4), "Heat, Prisoners and Paddington 2 are outside it"
    assert (swept.ripe, swept.enqueued, swept.already_queued) == (2, 1, 1), swept
    assert _moved(before, await _ownership(db)) == [], "a feeder wrote the ownership column"

    module.ITEMS[:] = [row for row in module.ITEMS if row["Id"] != "jf-2"]
    await seen.sync_all(db, client)
    after = await _ownership(db)

    assert _moved(before, after) == [1, 2, 3, 6, 7, 8], "the differ could not see the full sweep"
    assert after[2][0] is False
