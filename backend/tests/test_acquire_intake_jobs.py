"""§7.2's two intake paths inside the worker registry, asserted through `worker.JOBS` and `pool`,
never by calling the functions directly: the cadence, the shared budget and the connector-less install.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from spielplan import worker
from spielplan.acquire import intake, pipeline
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import save_jellyfin
from spielplan.core.config import settings
from spielplan.db import pool

POLL = "jellyfin-delta-poll"
SWEEP = "jellyfin-intake-sweep"
JELLYFIN_URL = "http://jellyfin.test"

# Every double item was created 2019-2024, so one poll from here reads the whole library.
BEFORE_THE_LIBRARY = datetime(2019, 1, 1, tzinfo=UTC)

# The series the double gives twelve episodes, which is §7.2's own burst.
BURST_SERIES = "jf-7"


def _job(name: str):
    return next(j for j in worker.JOBS if j.name == name)


@pytest.fixture
async def worker_env(db, pg_url, monkeypatch):
    """Neither job takes a connection, so the real pool is
    opened; `settings()` is cached, so cleared both ways."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    settings.cache_clear()
    await pool.open_pool(pg_url)
    try:
        yield
    finally:
        await pool.close_pool()
        settings.cache_clear()


@pytest.fixture
async def connected(db, worker_env, secrets_key, fake_jellyfin, monkeypatch):
    """Through `save_jellyfin`, because both drivers reload the config on every run."""
    module, transport = fake_jellyfin
    await save_jellyfin(db, url=JELLYFIN_URL, api_key=module.API_KEY)
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(JELLYFIN_URL, module.API_KEY, transport=transport),
    )
    return module


@pytest.fixture
def webhook(connected):
    """A sink, not the app: this file is about what the LOOP does once the handler has written a row."""
    delivered: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"ok": True})

    connected.WEBHOOK_TRANSPORT = httpx.MockTransport(receive)
    connected.WEBHOOK_URL = "http://spielplan.test/events/jellyfin"
    connected.WEBHOOK_TOKEN = "the-token-the-operator-pasted"
    return connected, delivered


async def _emit(module, delivered, **body) -> list[dict]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://fake-jellyfin"
    ) as control:
        response = await control.post("/_test/item-added", json=body)
    assert response.status_code == 200, response.text
    sent = int(response.json()["sent"])
    assert len(delivered) >= sent
    return delivered[-sent:]


async def _age_the_window(db, minutes: int) -> None:
    """`not_before` was written by Postgres's clock, so the
    rows are moved rather than a Python clock patched."""
    await db.execute(
        "UPDATE jellyfin_intake SET received_at = received_at - make_interval(mins => $1),"
        "       not_before = not_before - make_interval(mins => $1)", minutes,
    )


async def _task_keys(db) -> list[str]:
    return [
        row["key"] for row in await db.fetch(
            "SELECT key FROM acquisition_task WHERE kind = $1 ORDER BY key", pipeline.TASK_KIND
        )
    ]


def test_the_two_intake_rows_are_registered_at_section_7_2s_own_cadences():
    """Neither row is a model job: they must not hold while an import holds §10's lock."""
    poll, sweep = _job(POLL), _job(SWEEP)

    assert poll.run is worker._jellyfin_delta_poll
    assert sweep.run is worker._jellyfin_intake_sweep

    assert poll.every == 900, "section 7.2 says a 15-minute delta poll, and that is 900 seconds"
    assert sweep.every == 300, (
        "the debounce sweep runs every 300 s: short enough that a closed window is acted on "
        "inside section 7.2's own promise, long enough to stay out of the minute-interval group"
    )
    for job in (poll, sweep):
        assert 0 < job.timeout <= job.every, (
            f"{job.name}: a {job.timeout}s budget in a {job.every}s interval is a cadence this "
            "sequential loop can only keep by eating the slot of every job behind it"
        )
        assert job.name not in worker.MODEL_JOBS, (
            f"{job.name} writes no number expressed in a bundle, so holding it out of the loop "
            "across section 10's flip would stop section 7.2's intake for an import's duration"
        )


def test_what_an_add_waits_for_is_the_window_plus_one_sweep_and_not_an_hour():
    """The debounce is the window plus one sweep interval, capped by the fallback poll's own interval.
    The window's value is pinned here because the behavioural tests all derive their ageing from it."""
    sweep, poll = _job(SWEEP), _job(POLL)
    latency = intake.DEBOUNCE_SECONDS + sweep.every

    assert intake.DEBOUNCE_SECONDS == 600, (
        "section 7.2 says 'Debounce 10 min', and that is 600 seconds -- the number that decides "
        "whether a season of episodes arrives as one acquisition task or as twelve"
    )
    assert latency <= poll.every, (
        f"an add waits up to {latency}s to become a task, which is longer than the {poll.every}s "
        "fallback poll that exists for the households this path is the trigger for"
    )
    assert sweep.every != 60, (
        "the sweep is deliberately not a minute-interval job: that count sizes four arithmetic "
        "sentences in worker.py, and four minutes off a fifteen-minute promise does not buy them"
    )


def test_the_two_jellyfin_reads_that_share_an_interval_fit_inside_it():
    """Both reads run every 900 s in the same tick, one
    after another, so their budgets must fit together."""
    poll, sweep, seen = _job(POLL), _job(SWEEP), _job("jellyfin-seen-sync")
    drain = _job("acquisition-drain")

    assert seen.every == poll.every, "the pair below is asserted because they share a tick"
    assert poll.timeout + seen.timeout <= poll.every, (
        f"{poll.timeout}s and {seen.timeout}s of Jellyfin reads in a {poll.every}s interval is "
        "more than the interval, and this loop runs them one after another"
    )
    assert poll.timeout <= seen.timeout, (
        "the poll takes the smaller share because an abandoned poll costs nothing the next one "
        "does not re-derive, and an abandoned ownership sweep costs the read its gate is"
    )
    assert sweep.timeout * 2 <= sweep.every, (
        f"a {sweep.timeout}s sweep in a {sweep.every}s interval leaves a sweep that ran long "
        "overlapping the next one rather than merely late"
    )
    assert sweep.timeout <= drain.timeout, (
        "the sweep enqueues and the drain leases; neither half of one add's journey may hold this "
        "loop longer than the other"
    )


async def test_neither_intake_job_crashes_on_a_household_that_has_no_connector(worker_env, db):
    """A raised job re-arms at `RETRY_AFTER`, writing a traceback into the operator's log for ever."""
    assert await _job(POLL).run() is None
    assert await _job(SWEEP).run() is None

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 0
    assert await db.fetchval("SELECT count(*) FROM jellyfin_intake") == 0


async def test_a_connector_whose_secrets_will_not_open_is_skipped_rather_than_raised_on(
    worker_env, db, secrets_key, monkeypatch
):
    """The difference between unconfigured and unopenable is reported at boot and on the card, not here."""
    await save_jellyfin(db, url=JELLYFIN_URL, api_key="the-key-this-install-can-no-longer-read")
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    settings.cache_clear()

    assert (await registry.load_jellyfin(db)).secrets_unreadable is True, (
        "this test is about the degraded config, so the fixture has to actually produce one"
    )
    assert await _job(POLL).run() is None
    assert await _job(SWEEP).run() is None
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 0


async def test_the_delta_poll_job_files_the_library_it_has_not_seen_and_then_stops(
    worker_env, connected, db
):
    """The second run reads NOTHING: that is the watermark written back through `save_jellyfin`."""
    await save_jellyfin(db, delta_watermark=BEFORE_THE_LIBRARY)

    first = await _job(POLL).run()

    assert first is not None
    assert (first["read"], first["enqueued"], first["already_queued"]) == (7, 7, 0), first
    assert len(await _task_keys(db)) == 7

    second = await _job(POLL).run()

    assert second is not None
    assert (second["read"], second["enqueued"]) == (0, 0), second
    assert second["since"] == first["until"], "the second poll starts where the first finished"
    assert len(await _task_keys(db)) == 7


async def test_the_intake_sweep_job_leaves_an_open_window_alone_and_collapses_a_closed_one(
    worker_env, webhook, db
):
    """The first run, with the window open, is what makes the second mean anything."""
    module, delivered = webhook
    payloads = await _emit(module, delivered, item_id=BURST_SERIES, episodes=12)
    assert len(payloads) == 12, "the double refuses to fake a burst it cannot fill"
    for body in payloads:
        await intake.record_event(db, body)

    assert await _job(SWEEP).run() is None, "the ten minutes are not up, so nothing is ripe"
    assert await _task_keys(db) == []
    assert await db.fetchval(
        "SELECT count(*) FROM jellyfin_intake WHERE state = 'pending'"
    ) == 12

    await _age_the_window(db, intake.DEBOUNCE_SECONDS // 60 + 1)
    detail = await _job(SWEEP).run()

    assert detail is not None
    assert (detail["ripe"], detail["enqueued"], detail["rows"]) == (1, 1, 12), detail
    assert await _task_keys(db) == [f"jellyfin:{BURST_SERIES}"], (
        "twelve episodes of one series are one acquisition task for the show, not twelve"
    )
    assert await _job(SWEEP).run() is None, "the burst was decided, so the next sweep has nothing"
