"""§7.2's two intake paths, inside M4.7's worker registry. Spec v2.1 §7.2, §5.3, §3.1, §2.

`test_jellyfin_intake.py` proves what `intake.poll_delta` and `intake.sweep_pending` do when they
are called. What it cannot see is whether anything ever calls them: a debounce table nothing sweeps
is a pending set that never ripens, and a delta poll nobody fires is §7.2's fallback in name only.
This file is that seam - the two rows M5.2 adds to the loop (plan C4, decision 368) - asserted
through `worker.JOBS` and `pool` rather than by calling the two functions directly, because the
production call path is what is under test. Same argument `test_acquire_drain.py` makes one row
over, and the same one `test_worker_jobs.py`'s `worker_env` makes for the whole registry.

THREE CLAIMS THE REGISTRY CANNOT MAKE ABOUT ITSELF, and they are why this file exists rather than
two more cases in `test_worker_jobs.py`:

  * **The cadence is what §7.2 promises a household.** "Debounce 10 min" is a promise about when an
    add becomes work, and the window alone cannot keep it: a fixed ten-minute window swept once an
    hour is a debounce of up to seventy minutes. The promise is `intake.DEBOUNCE_SECONDS` plus one
    sweep interval, and only the registry holds the second half of that sum.
  * **The budget is bounded by a row this one does not own.** Two Jellyfin reads share the 900 s
    interval and this loop runs them one after another, so the fraction each may hold is a claim
    about the pair. Nothing but an assertion keeps two `timeout` values in one tuple in order.
  * **A household with no Jellyfin at all is a legal install.** §3.1 makes a half-configured boot
    legal, and a background job that raises on one puts a traceback in the log §6.6 calls the
    operator's data every `RETRY_AFTER` - for ever, on an install whose owner simply has not
    connected a media server.

`worker.JOBS` is read and never substituted here: the tuple IS the subject, and a test that built
its own `Job` would assert that two functions work and say nothing about the interval, the budget
or the name the loop fires them under.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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

# Every item the double holds was created between 2019 and 2024, so a watermark here is "before
# this library existed" and one poll reads the whole of it. A real install starts from its own
# creation instant instead (decision 366), which is asserted where that rule lives.
BEFORE_THE_LIBRARY = datetime(2019, 1, 1, tzinfo=UTC)

# The series the double gives twelve episodes, which is §7.2's own burst.
BURST_SERIES = "jf-7"


def _job(name: str):
    """The registry row, read out of `JOBS` rather than built here."""
    return next(j for j in worker.JOBS if j.name == name)


@pytest.fixture
async def worker_env(db, pg_url, monkeypatch):
    """The worker's own view of the world: the real pool, against the test database.

    Neither job takes a connection - each acquires from the pool exactly as `_tick` calls it - so
    this opens the real pool rather than handing either one `db`. `settings()` is `lru_cache`d, so
    the cache is cleared on the way in and on the way out.
    """
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
    """The connector as an admin left it, with the double behind `registry.make_client`.

    The config goes in through `save_jellyfin` because that is what the job reads back: both
    drivers call `registry.load_jellyfin` on every run rather than holding a `cfg`, and a fixture
    that handed them a constructed config would exercise neither the sealed read-modify-write that
    carries the watermark (decision 366) nor the reload the poll depends on.

    `make_client` is substituted rather than the client threaded through, for the reason that
    function's own docstring gives: it is the single construction site precisely so a test can
    point the whole app at `ops/fake_jellyfin.py` without a transport argument on every caller.
    """
    module, transport = fake_jellyfin
    await save_jellyfin(db, url=JELLYFIN_URL, api_key=module.API_KEY)
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(JELLYFIN_URL, module.API_KEY, transport=transport),
    )
    return module


@pytest.fixture
def webhook(connected):
    """The double's `ItemAdded` emitter, wired to a sink that records what crossed the wire.

    A sink rather than the app, because `POST /events/jellyfin` is the webhook stage's route and
    this file is about what the LOOP does with a row once the handler has written it. The payload
    is built, serialised and posted by `ops/fake_jellyfin.py` exactly as it would be across a
    compose network, so what reaches `record_event` here is a body a real plugin sends - the
    argument `test_jellyfin_intake.py` makes at length, borrowed rather than re-made.
    """
    delivered: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"ok": True})

    connected.WEBHOOK_TRANSPORT = httpx.MockTransport(receive)
    connected.WEBHOOK_URL = "http://spielplan.test/events/jellyfin"
    connected.WEBHOOK_TOKEN = "the-token-the-operator-pasted"
    return connected, delivered


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


async def _age_the_window(db, minutes: int) -> None:
    """Move every intake row back in time, which is the only honest way to watch a fixed window
    close: `not_before` was written by Postgres's clock, so a test that patched a Python one would
    be asserting against an instant the database never saw."""
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


# --- the registry entries (backend, no DB) -------------------------------------------------------


def test_the_two_intake_rows_are_registered_at_section_7_2s_own_cadences():
    """§7.2 names both numbers out loud, and this is where they stop being prose.

    The milestone column is the milestone of the WORK and not of the diff that registered it -
    `ledger-refresh` is M2 and landed at M4.13 - and here the two are the same milestone, which is
    exactly what plan C4's exception says it is taking.

    NEITHER ROW IS A MODEL JOB, which is the assertion `acquisition-drain`'s sibling makes in the
    other direction. `_tick` skips `MODEL_JOBS` while an import holds §10's lock, because those
    jobs write numbers expressed in a basis; these two write `acquisition_task` and
    `jellyfin_intake` rows, which mean the same thing whichever bundle is active. A name added
    here would hold §7.2's intake offline for the length of an import for no reason anybody could
    see, and the derivation in `test_worker_schedule.py` is what keeps the two in step.
    """
    poll, sweep = _job(POLL), _job(SWEEP)

    assert poll.run is worker._jellyfin_delta_poll
    assert sweep.run is worker._jellyfin_intake_sweep
    assert (poll.milestone, sweep.milestone) == ("M5.2", "M5.2")

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
    """§7.2's "Debounce 10 min" is a promise about arrival, and the window is only half of it.

    `intake.DEBOUNCE_SECONDS` decides when a key is RIPE; the registry decides when anyone looks.
    The same table swept hourly is a fixed ten-minute window and a seventy-minute debounce, and
    every test in `test_jellyfin_intake.py` would still pass - which is precisely why the sum is
    asserted here, against the two constants that make it, and not there.

    The ceiling is the fallback's own interval rather than a number chosen for this assertion: a
    webhook delivery that took longer to become work than the poll that exists for households
    with no webhook at all would make §7.2's trigger slower than its fallback.

    AND THE WINDOW'S OWN VALUE IS PINNED HERE, because nothing else in the tree pins it. The line
    that used to stand in this one's place read `latency >= DEBOUNCE_SECONDS` against a `latency`
    defined one line above as `DEBOUNCE_SECONDS + sweep.every`, so it reduced to
    `sweep.every >= 0` -- a claim `test_worker_jobs.py`'s `0 < job.timeout <= job.every` already
    makes unfalsifiable. Nor do the behavioural tests hold it: `test_jellyfin_intake.py` ages its
    rows five minutes to watch a window stay shut and eleven to watch it open, and this file's own
    ageing is derived as `DEBOUNCE_SECONDS // 60 + 1`, so every one of them moves with the
    constant. A WIDENING is caught by the ceiling below, at 601 seconds; a NARROWING to anything
    between 301 and 599 was invisible to the whole suite, which is exactly the direction that
    turns §7.2's one job for a season back into one per episode.
    [review cycle 1: M52-INTAKE-03]

    AND NOT `every=60`, which is decision 368's other half. A minutely row moves
    `len([j for j in JOBS if j.every == 60])`, and four arithmetic sentences in `worker.py` are
    sized on that count - `JOB_RUN_KEEP_DAYS`, the two 55 s budgets and `DURATION_LOG_THRESHOLD` -
    with a rows-a-day figure that is the count times 1,440. Held here as the claim it is, and held
    against the registry itself by `test_worker_schedule.py`.
    """
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
    """§5.3's budget rule (`worker.py:987-1006`) reaches a row this one does not own.

    `job.timeout <= job.every` is asserted for every live row by `test_worker_jobs.py`. What only
    this pair needs is the sum: `jellyfin-seen-sync` and `jellyfin-delta-poll` both run every 900
    seconds and are therefore due in the same tick, and `_tick` awaits due jobs one after another.
    Two budgets that each fit inside the interval and do not fit inside it TOGETHER is a pair that
    can keep its cadence only by starving §7.3's minute poll, which is the whole reason the rule
    exists.

    WHY THE POLL TAKES THE SMALLER SHARE: an abandonment costs the two of them different things.
    `poll_delta` advances the watermark only on a read that completed, so an attempt cancelled at
    its budget re-asks the identical question on the next tick and `UNIQUE (kind, key)` absorbs
    whatever it enqueued before it was cut. The sweep's gate is a library read that COMPLETED -
    `seen._falsify_ownership` calls itself the most destructive statement in its module - so it
    cannot be cut and repeated for free, and it keeps the two thirds.

    The debounce sweep gets its own floor from the row it feeds. It enqueues and
    `acquisition-drain` leases, so the two halves of one add's journey are bounded alike; and half
    its own interval is what leaves the next sweep on time after one that ran long.
    """
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


def test_no_budget_paragraph_promises_the_minute_poll_a_bound_its_own_timeout_breaks():
    """Decision 368 asks each intake row for a budget paragraph in `acquisition-drain`'s
    convention, and the sweep's said it and the drain "are bounded alike because neither may hold
    this sequential loop past the minute §7.3's playback poll is promised" -- over two rows that
    both carry `timeout=120`, in a loop `_tick` runs one job after another. Either may hold it for
    two minutes. The drain's own paragraph makes the honest claim (its share of an interval is time
    the playback poll does not get); the line an operator reads as a budget may not promise the
    minute poll a bound the row beneath it does not keep. [review cycle 3: M52-C3-PAPER-08]
    """
    source = Path(worker.__file__).read_text(encoding="utf-8")
    for job in worker.JOBS:
        above = source[: source.find(f'Job("{job.name}"')].rsplit("Job(", 1)[-1]
        paragraph = " ".join(
            line.strip().lstrip("#").strip()
            for line in above.splitlines() if line.strip().startswith("#")
        )
        if job.timeout > 60:
            assert "past the minute" not in paragraph, (
                f"{job.name}'s budget paragraph promises the minute poll a bound, and its own "
                f"timeout is {job.timeout}s"
            )


# --- a household with no Jellyfin (Postgres) -----------------------------------------------------


async def test_neither_intake_job_crashes_on_a_household_that_has_no_connector(worker_env, db):
    """§3.1's legal install, met by both jobs at once.

    Nothing here is configured and nothing has been imported: no bundle, no connector row, no
    intake rows. Both jobs have to answer "nothing to do" and return, because `_tick` records a
    raised job and re-arms it at `RETRY_AFTER` - so a job that treated an unconfigured connector
    as an error would write a traceback into the log §6.6 asks the operator to read every five
    minutes for the life of the install.

    `None` and not an empty report, for the reason `_acquisition_drain` returns none for a tick
    that leased nothing: `job_run` already records that the job ran, and `detail` is for the run
    that did something.
    """
    assert await _job(POLL).run() is None
    assert await _job(SWEEP).run() is None

    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 0
    assert await db.fetchval("SELECT count(*) FROM jellyfin_intake") == 0


async def test_a_connector_whose_secrets_will_not_open_is_skipped_rather_than_raised_on(
    worker_env, db, secrets_key, monkeypatch
):
    """The second state one predicate covers, and it is not the same install as the first.

    `load_jellyfin` degrades a SECRETS_KEY failure to a config that keeps its URL and drops its
    credentials, so `configured` is False and `make_client` returns None - the same answer it
    gives a household that never connected anything. That is right HERE, because the answer to
    "may this process read Jellyfin" is the same; what must not be the same is where the
    DIFFERENCE is reported, and it is not reported by these jobs at all. §2's custody line at boot
    and §6.6's connector card say which of the two an install is in; a poll that raised would say
    it in a traceback every fifteen minutes and fix nothing.
    """
    await save_jellyfin(db, url=JELLYFIN_URL, api_key="the-key-this-install-can-no-longer-read")
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    settings.cache_clear()

    assert (await registry.load_jellyfin(db)).secrets_unreadable is True, (
        "this test is about the degraded config, so the fixture has to actually produce one"
    )
    assert await _job(POLL).run() is None
    assert await _job(SWEEP).run() is None
    assert await db.fetchval("SELECT count(*) FROM acquisition_task") == 0


# --- the two paths, driven from the registry (Postgres + the double) ------------------------------


async def test_the_delta_poll_job_files_the_library_it_has_not_seen_and_then_stops(
    worker_env, connected, db
):
    """§7.2's fallback, fired the way the loop fires it.

    The detail this returns is what `job_run` carries and §6.6's card reads, so it is asserted as
    the job's answer rather than as `poll_delta`'s: a driver that called the right function and
    dropped its report would leave an operator with a row saying the poll ran and nothing saying
    what it found.

    The second run is the assertion with teeth. It reads NOTHING, which is the watermark the
    driver wrote back through `save_jellyfin` doing its work - a driver that held its `cfg` across
    calls, or reloaded it from anywhere but the database, would poll from the same floor for ever
    and lean on `UNIQUE (kind, key)` to look quiet while re-walking the corpus every fifteen
    minutes.
    """
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
    """§7.2's "Debounce 10 min; series acquire per-show, not per-episode", from the loop.

    Twelve deliveries for one series, and the job run twice: once while the window is still open,
    once after it has closed. The first run is what makes the second mean anything - a sweep that
    ignored `not_before` would produce the same single task and pass every assertion below it,
    while acquiring a season the moment its first episode landed rather than when the scan that
    added it had finished.

    The rows are written by `intake.record_event`, which is the whole of the handler's synchronous
    work (plan A4, decision 365); the payloads are the double's, for the reason the `webhook`
    fixture gives. What this file adds to `test_jellyfin_intake.py` is the frame around them: the
    registry row, its interval and its budget, and a driver that loads a config and hands the
    right three arguments to the right function.
    """
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
