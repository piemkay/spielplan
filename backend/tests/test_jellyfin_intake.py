"""§7.2's two intake paths, from the event a server sent to the one task it becomes (decisions 363-369):
a fixed window per resolved title in a durable table, the library pick asked of the server, and an Episode
never keyed on itself. Payloads are the double's, never this file's. Needs TEST_DATABASE_URL."""

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
# Every fake item was created 2019-2024, so a watermark here is "before this library existed".
BEFORE_THE_LIBRARY = datetime(2019, 1, 1, tzinfo=UTC)

# Real GUIDs: the double's seven fixture ids are not, which let a string comparison pass the suite.
GUID_MOVIE = "6213b704a0d954293110f4d561b0f614"
GUID_SERIES = "0f0e0d0c0b0a49088706050403020100"


@pytest.fixture
def webhook(fake_jellyfin):
    """A sink rather than the app: this file is about what happens to a body once it has arrived."""
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
    """`_request` wraps an httpx error in `JellyfinError`
    (§3.3), which is what an outage looks like here."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the household's Jellyfin is not answering")

    return _client(httpx.MockTransport(refuse))


async def _age_the_window(db, minutes: int) -> None:
    """`not_before` was written by Postgres's clock, so the
    rows are moved rather than a Python clock patched."""
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
    """Added to THIS test's copy of the double; stored
    undashed, as `/Items` answers, dashed by the emitter."""
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
    """JSON spells a NUL only as the six characters `\\u0000`, as `System.Text.Json` writes it."""
    return httpx.Response(
        200, content=json.dumps({"Items": rows, "TotalRecordCount": len(rows)}).encode("ascii"),
        headers={"content-type": "application/json"},
    )


def _pending(key: str, opens_at: datetime, row_id: int) -> dict:
    """The id it decides, the key it groups by, the instant that decides."""
    return {"id": row_id, "resolved_key": key, "not_before": opens_at}


def test_twelve_events_for_one_series_inside_the_window_are_one_ripe_key():
    """Twelve episodes delivered seconds apart are ONE key, and all twelve rows are decided together."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [
        _pending("jf-7", start + timedelta(minutes=10, seconds=n), n) for n in range(12)
    ]

    ripe = intake.ripe_keys(rows, start + timedelta(minutes=11))

    assert len(ripe) == 1, "twelve events for one show are one job"
    assert ripe[0].key == "jf-7"
    assert ripe[0].ids == tuple(range(12))


def test_two_shows_added_in_one_window_are_two_keys():
    """Per TITLE, never per window: two shows in one window are two acquisitions."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("jf-7", start + timedelta(minutes=10), 1),
            _pending("jf-6", start + timedelta(minutes=10, seconds=30), 2),
            _pending("jf-7", start + timedelta(minutes=10, seconds=40), 3)]

    ripe = intake.ripe_keys(rows, start + timedelta(minutes=11))

    assert {entry.key: entry.ids for entry in ripe} == {"jf-7": (1, 3), "jf-6": (2,)}


def test_an_event_after_the_window_closed_opens_a_second_one_rather_than_extending_the_first():
    """Decision 363: FIXED windows; under a sliding one a long scan would postpone the show indefinitely."""
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
    """An event delivered a moment ago is not work yet: the rest of the season is still arriving."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("jf-7", start + timedelta(minutes=10), 1)]

    assert intake.ripe_keys(rows, start + timedelta(minutes=9, seconds=59)) == []


def test_the_oldest_window_is_swept_first():
    """A sweep that runs out of budget must have spent it on the oldest windows."""
    start = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    rows = [_pending("aaa", start + timedelta(minutes=20), 1),
            _pending("zzz", start + timedelta(minutes=10), 2)]

    assert [entry.key for entry in intake.ripe_keys(rows, start + timedelta(hours=1))] == [
        "zzz", "aaa"
    ]


def test_a_row_written_while_the_clock_ran_ahead_is_ripe_once_the_clock_is_corrected():
    """A row received in the future (a clock stepped back) cannot be collecting a burst, so it is ripe."""
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
    """The plugin renders GUIDs dashed and `/Items` undashed; the key is the server's spelling."""
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


async def test_a_movie_is_keyed_on_itself_and_an_episode_on_the_show(webhook):
    """An episode carries its own ids, but they are an EPISODE's,
    which no title here is, so the key is the series."""
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
    """Keying on the episode would be the twelve jobs §7.2 forbids."""
    module, delivered = webhook
    (episode,) = await _emit(module, delivered, item_id="jf-7", episodes=1, omit=["SeriesId"])

    event = intake.read_event(episode)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.EPISODE_WITHOUT_SERIES)
    assert event.resolved_key is None, "an episode id must never become an acquisition key"


async def test_an_episode_whose_type_is_spelled_in_another_case_is_still_keyed_on_the_show(
    webhook,
):
    """Jellyfin versions disagree about capitalisation, so the type is compared case-insensitively."""
    module, delivered = webhook
    (lowercase,) = await _emit(module, delivered, item_id="jf-7", episodes=1, item_type="episode")

    event = intake.read_event(lowercase)

    assert (event.state, event.resolved_key) == (intake.PENDING, "jf-7")
    assert event.item_id == "jf-7-e1", "the episode is still recorded as the event it was"


async def test_an_item_id_too_long_to_ask_the_server_about_is_recorded_and_never_asked(
    db, fake_jellyfin
):
    """The bound is on the RESOLVED key: on the Episode arm
    that is `SeriesId`, which reaches a query string."""
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
    """Decision 365: never 400 a delivery; the plugin does not retry, so a rejection is a silence."""
    module, delivered = webhook
    (body,) = await _emit(module, delivered, item_id="jf-1", omit=["ItemId"])

    event = intake.read_event(body)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.NO_ITEM_ID)
    assert event.item_type == "Movie", "what the body did carry is still recorded"


async def test_a_template_for_another_event_is_recorded_and_not_acted_on(webhook):
    """Other templates are recorded and enqueue nothing;
    an ABSENT `NotificationType` reads as `ItemAdded`."""
    module, delivered = webhook
    (other,) = await _emit(module, delivered, item_id="jf-1", notification_type="ItemUpdated")

    assert intake.read_event(other).reason == intake.NOT_ITEM_ADDED

    untyped = {key: value for key, value in other.items() if key != "NotificationType"}
    assert intake.read_event(untyped).state == intake.PENDING


async def test_a_value_a_delivery_carried_is_logged_escaped_and_never_as_a_line_of_its_own(
    db, caplog
):
    """Wire values are logged as `ascii()` reprs, so a newline cannot forge a log line."""
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
    """A non-object body is recorded as unreadable; raising would be a 500."""
    event = intake.read_event(body)

    assert (event.state, event.reason) == (intake.SKIPPED, intake.UNREADABLE)
    assert event.resolved_key is None


async def test_a_burst_of_twelve_episodes_yields_one_acquisition_task_for_the_show(
    db, fake_jellyfin, webhook
):
    """Twelve rows, one task, and nothing `paid` (decision 347)."""
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
    """The last sweep runs on a SECOND connection, so
    nothing the first held can have remembered the burst."""
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
    """The enqueue and its record are one transaction, enqueue
    first: re-enqueueing is free, a lost add is not."""
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
    """`state = 'pending'` in `_DECIDE` makes the second sweep
    decide nothing; the overlap is arranged, not raced."""
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
    """A template that calls an episode a Movie: the membership read's own `Type` must refuse all twelve."""
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
    """A 4xx provoked by one id is about that key; the sweep carries on to the keys behind it."""
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


# Spelled out, not read off `_SERVER_WIDE`, or losing a member would lose a case silently.
@pytest.mark.parametrize("status", [401, 408, 429])
async def test_a_refusal_about_the_server_stops_the_sweep_though_the_server_answers(
    db, fake_jellyfin, webhook, status
):
    """401, 408 and 429 are about the SERVER, so the sweep stops there and leaves the rest pending."""
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
    """Jellyfin never 403s this app's admin key, so a 403 is a proxy refusing one request: that key's."""
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
    """The server is ASKED via `/System/Info/Public`; an answer means the failure was this key's."""
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
    """An outage stops the sweep at the first key; three ripe keys make stop and carry-on different logs."""
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
    """The first test here a string comparison of GUIDs can fail, with and without a pick."""
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
    """Jellyfin drops an unparseable id, which turns `ids=<junk>`
    into a whole-server dump; the read is bounded."""
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
    """A NUL in a server row becomes U+FFFD rather than raising out of the INSERT and wedging the sweep."""
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


async def test_an_add_in_a_library_the_admin_did_not_pick_is_recorded_and_enqueues_nothing(
    db, fake_jellyfin, webhook
):
    """Membership is the server's fact (`ParentId`), never the
    event's; the enqueue is a task, not an acquisition."""
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
    """"In no picked library" and "nobody could ask" are opposite facts; the rows stay pending."""
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
    """An id the server will not answer for left the library;
    stubbed, since the double refuses to invent it."""
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
    """Decision 410: a stale picked library holds back only adds
    it might hold; the pick is never narrowed or widened."""
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


@pytest.fixture
async def configured(db, secrets_key, fake_jellyfin):
    """Through `save_jellyfin`/`load_jellyfin`: the watermark is sealed connector state."""
    module, transport = fake_jellyfin
    await save_jellyfin(db, url=JELLYFIN_URL, api_key=module.API_KEY)
    return module, _client(transport)


async def test_the_delta_poll_enqueues_the_set_once_and_the_next_poll_enqueues_nothing(
    db, configured
):
    """The second poll reads NOTHING: the watermark, not `UNIQUE (kind, key)`, keeps it quiet."""
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
    """The same pick boundary on the other path."""
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
    """`DateCreated` is the file's; `DateLastSaved` marks the arrival, which the poll must read."""
    _, client = configured
    await save_jellyfin(db, delta_watermark=datetime(2024, 1, 1, tzinfo=UTC))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (report.read, report.enqueued) == (2, 2), report.as_dict()
    assert await _task_keys(db) == ["jellyfin:jf-8", "jellyfin:jf-x"]


async def test_a_row_saved_just_before_a_poll_and_visible_only_after_its_read_is_not_lost(
    db, configured, fake_jellyfin
):
    """`DateLastSaved` is stamped before the row commits, so
    the watermark trails the read by `WATERMARK_OVERLAP`."""
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
    """Decision 410 on the poll: live scopes are filed, the
    watermark holds, and the poll raises naming the id."""
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
    """A NUL row must not freeze the watermark in a livelock of re-paged supersets."""
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
    """A key past btree's tuple limit is counted as `unkeyable`, and the poll goes on."""
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
    """The poll's final save must not overwrite an origin move made while it was paging."""
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
    """A watermark in the future is read as never polled."""
    module, client = configured
    now = await db.fetchval("SELECT now()")
    # Two seconds on: the double spells whole seconds, and the poll reads `DateLastSaved`.
    module.ITEMS[0]["DateLastSaved"] = module._stamp(now + timedelta(seconds=2))
    await save_jellyfin(db, delta_watermark=now + timedelta(days=1))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.enqueued == 1, report.as_dict()
    assert await _task_keys(db) == ["jellyfin:jf-1"]
    assert report.since < now, "the poll read from a floor it could trust"


def _deaf_to(parameter: str, inner: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
    """The double with one query parameter dropped, as a caching proxy or rewriting gateway would."""

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
    """Handed episodes anyway, the poll must file no task for one."""
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
    """A failed read raises and leaves the watermark: a swallowed outage looks like a quiet household."""
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    cfg = await load_jellyfin(db)

    with pytest.raises(JellyfinError):
        await intake.poll_delta(db, _unreachable(), cfg)

    assert (await load_jellyfin(db)).delta_watermark == BEFORE_THE_LIBRARY
    assert await _task_keys(db) == []


async def test_a_poll_cut_part_way_through_its_enqueues_leaves_the_watermark_where_it_was(
    db, configured, monkeypatch
):
    """The watermark is written last: tasks already filed are committed, and only it can lose the rest."""
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
    """Decision 416: a background save must not mint the one-time webhook token."""
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
    """`started` is taken BEFORE the read; a slow read makes the two orders distinguishable."""
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
    """Decision 366: never epoch, or the first poll files a task for every owned title."""
    _, client = configured
    assert (await load_jellyfin(db)).delta_watermark is None

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert (report.read, report.enqueued) == (0, 0)
    assert await _task_keys(db) == []
    # Decision 412: the instant this install gained §7.2's fallback.
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
    """Decision 412: on an upgrade the floor is 0025's `applied_at`, not when 0001 ran."""
    module, client = configured
    await db.execute(
        "UPDATE schema_migration SET applied_at = applied_at - interval '180 days'"
        " WHERE version < '0025'"
    )
    gained = await db.fetchval(
        "SELECT applied_at FROM schema_migration WHERE version = '0025_jellyfin_intake'"
    )
    installed = await db.fetchval("SELECT min(applied_at) FROM schema_migration")
    # `DateLastSaved`, which the poll reads (decision 409).
    module.ITEMS[0]["DateLastSaved"] = module._stamp(installed + timedelta(days=30))

    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.since == gained, "the first poll read from when this install was created"
    assert (report.read, report.enqueued) == (0, 0), (
        "a title added between the install and the upgrade was filed as a new add"
    )


async def test_the_two_paths_file_one_task_for_one_title(db, configured, webhook):
    """Both feeders over one title in one test: differently spelled keys would each look correct alone."""
    module, client = configured
    _, delivered = webhook
    await _record(db, await _emit(module, delivered, item_id="jf-1"))
    await _age_the_window(db, minutes=11)
    assert (await intake.sweep_pending(db, client, await load_jellyfin(db))).enqueued == 1

    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)
    report = await intake.poll_delta(db, client, await load_jellyfin(db))

    assert report.already_queued >= 1, "the poll re-offered the title the webhook had filed"
    assert await _task_keys(db) == [f"jellyfin:jf-{n}" for n in (1, 2, 3, 6, 7, 8, "x")]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`stages.active_store` reads `settings().artifacts_dir`,
    so the path arrives through the environment."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def owned(db, data_dir, tmp_path):
    """An active bundle, because stage 9 needs a basis to place against."""
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
    """Placed is what decision 411 reads: a placed title has nothing left for §8 to add."""
    await db.execute(
        "UPDATE title SET is_owned = true,"
        "       placement = CASE WHEN placement = 'unplaced' THEN 'warm' ELSE placement END,"
        "       placement_bundle = coalesce(placement_bundle, 'test-v1')"
        " WHERE id = ANY($1::int[])", [1, 2, 3, 8],
    )


async def test_a_re_scan_of_a_title_the_household_owns_mints_nothing_and_bills_nothing(
    db, owned, configured
):
    """Every re-offered task is drained and ends at stage
    1 (decision 411), with the board byte-identical."""
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
    """A re-offer of a placed title is filed below every genuine add."""
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


async def _ownership(db) -> dict[int, tuple[bool, datetime | None]]:
    """`owned_checked_at` counts too: re-stamping it is a write decision 362 keeps off the feeders."""
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
    """Neither feeder writes ownership; the full sweep over a thinned mirror is the diff's control."""
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
