"""Daily jobs carry an `anchor_hour` and fire once per local date (§2's fourteen nights, not
uptime); sub-hour jobs keep the monotonic interval. `due` is pure: the caller supplies clocks."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from spielplan import worker
from spielplan.core.config import settings
from spielplan.models.artifacts import ArtifactStore

# Every daily job with an implementation, `job-run-prune` included.
NIGHTLY = (
    "job-run-prune", "push-subscription-prune", "placement-reconciliation", "ledger-map-refit",
    "fold-in-user-vectors", "nightly-backup",
)

# Fixed offsets, not `ZoneInfo`: `due` reads only `.hour` and `.date()`, and Windows has no tz
# database. The spring-forward case builds the missing hour from these two.
CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))


def test_every_registered_job_matches_its_spec_trigger():
    """`every` is what the loop obeys, and §5.3's cadence is what it must be."""
    by_name = {job.name: job for job in worker.JOBS}
    assert by_name["jellyfin-sessions-poll"].every == 60
    assert by_name["jellyfin-seen-sync"].every == 900        # "15 min + webhook"
    assert by_name["session-prune"].every == 3600            # "hourly"
    assert by_name["push-subscription-prune"].every == 86400  # "daily"


def test_the_tick_is_shorter_than_the_shortest_job():
    """A job can never run more often than the loop wakes."""
    assert min(job.every for job in worker.JOBS) > worker.TICK_SECONDS


def test_an_interval_job_that_has_never_run_is_due_immediately():
    """Only interval jobs: with no wall clock no anchored job is due, which is what stops every boot
    firing the daily jobs."""
    names = {job.name for job in worker.due(now=0.0, last_run={})}
    assert "jellyfin-seen-sync" in names
    assert names == {job.name for job in worker.JOBS if job.anchor_hour is None}
    assert not (names & set(NIGHTLY))


def test_only_the_elapsed_jobs_are_due():
    last = {job.name: 0.0 for job in worker.JOBS}
    # The minute-interval jobs. `tier-set-refit` (decision 11) and `ledger-refresh` are extra triggers
    # for §5.3's fit.
    minutely = {"jellyfin-sessions-poll", "fold-in-tick", "tier-set-refit", "ledger-refresh"}
    at_90s = {job.name for job in worker.due(now=90.0, last_run=last)}
    assert at_90s == minutely

    at_1000s = {job.name for job in worker.due(now=1000.0, last_run=last)}
    # Decision 368: the intake sweep runs every 300 s, neither minutely nor hourly.
    assert at_1000s == minutely | {
        "jellyfin-seen-sync", "jellyfin-delta-poll", "jellyfin-intake-sweep"
    }


def test_the_placement_sweep_runs_before_the_fits_that_read_its_coordinates():
    """Both fits read the coordinates the sweep writes, so it leads the registry `due` fires in."""
    # Late enough that all three anchors have passed: an evening first boot.
    late = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    order = [j.name for j in worker.due(1e9, {}, local=late, last_date={})]
    assert order.index("placement-reconciliation") < order.index("fold-in-user-vectors")
    assert order.index("placement-reconciliation") < order.index("ledger-map-refit")


def test_the_fold_in_runs_often_enough_to_answer_within_a_sitting():
    """The shelves order by what only the fold-in writes, so a minutes-scale tick runs alongside the
    nightly pass."""
    tick = next(j for j in worker.JOBS if j.name == "fold-in-tick")
    nightly = next(j for j in worker.JOBS if j.name == "fold-in-user-vectors")
    assert tick.run is not None, "M2 owes this one an implementation"
    assert tick.every <= 300, "a sitting is minutes long; an hourly tick is a nightly job"
    assert nightly.every == 86400, "§5.3's nightly pass is not replaced by the tick"


def _one_day(local: datetime, now: float, last_run, last_date) -> list[str]:
    """Stamped as `_tick` stamps a success: the schedule is `due` plus what the caller records."""
    fired = [job.name for job in worker.due(now, last_run, local=local, last_date=last_date)]
    for job in worker.JOBS:
        if job.name not in fired:
            continue
        last_run[job.name] = now
        if job.anchor_hour is not None:
            last_date[job.name] = local.date()
    return fired


def test_every_live_daily_job_is_anchored_to_an_hour_of_the_household_s_day():
    """A daily job with no anchor means every 24 h of uptime."""
    unanchored = [
        j.name for j in worker.JOBS
        if j.run is not None and j.every == 86400 and j.anchor_hour is None
    ]
    assert not unanchored, f"daily jobs still measuring uptime: {unanchored}"
    assert {j.name for j in worker.JOBS if j.anchor_hour is not None} == set(NIGHTLY)
    for job in worker.JOBS:
        if job.anchor_hour is not None:
            assert 0 <= job.anchor_hour <= 23


def test_the_nightly_anchors_are_staggered_so_one_night_is_not_one_tick():
    """`_tick` runs due jobs in sequence, so shared anchors block the minute poll. The sweep precedes
    the fits, the dump goes last, and `job-run-prune` first."""
    anchors = {j.name: j.anchor_hour for j in worker.JOBS if j.anchor_hour is not None}
    assert len(set(anchors.values())) == len(anchors), f"two jobs share an hour: {anchors}"
    assert anchors["placement-reconciliation"] < anchors["ledger-map-refit"]
    assert anchors["placement-reconciliation"] < anchors["fold-in-user-vectors"]
    assert anchors["nightly-backup"] == max(anchors.values())
    assert anchors["job-run-prune"] == min(anchors.values())


def test_a_nightly_job_fires_once_per_local_calendar_date_at_its_anchor():
    """`now - last_run >= 86400` fired at whatever hour the worker started, and drifted."""
    start = datetime(2026, 9, 7, 0, 0, tzinfo=CET)
    last_run: dict[str, float] = {}
    last_date: dict[str, date] = {}
    fired: list[datetime] = []
    for step in range(48):
        local = start + timedelta(hours=step)
        if "nightly-backup" in _one_day(local, step * 3600.0, last_run, last_date):
            fired.append(local)

    anchor = next(j for j in worker.JOBS if j.name == "nightly-backup").anchor_hour
    assert [d.hour for d in fired] == [anchor, anchor], f"fired at {fired}"
    assert [d.date() for d in fired] == [date(2026, 9, 7), date(2026, 9, 8)]


def test_a_nightly_job_is_not_due_before_its_anchor_hour():
    """Midnight is not the night §2 means."""
    backup = next(j for j in worker.JOBS if j.name == "nightly-backup")
    for hour in range(backup.anchor_hour):
        local = datetime(2026, 9, 7, hour, 30, tzinfo=CET)
        names = {j.name for j in worker.due(1e9, {}, local=local, last_date={})}
        assert "nightly-backup" not in names, f"fired at {hour:02d}:30"
    at_anchor = datetime(2026, 9, 7, backup.anchor_hour, 0, tzinfo=CET)
    assert "nightly-backup" in {
        j.name for j in worker.due(1e9, {}, local=at_anchor, last_date={})
    }


def test_fourteen_restarts_inside_an_hour_do_not_erase_a_fortnight():
    """A restart re-derives `last_run` and `last_date` from `job_run`, as `main` does."""
    local = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    seeded_run = {name: -3600.0 for name in NIGHTLY}   # ran an hour ago, per job_run
    seeded_date = {name: local.date() for name in NIGHTLY}

    for restart in range(14):
        # Each restart's loop clock starts near zero; the seed carries the previous run's age.
        names = {
            j.name
            for j in worker.due(90.0 * restart, dict(seeded_run), local=local,
                                last_date=dict(seeded_date))
        }
        assert not (names & set(NIGHTLY)), f"restart {restart} re-fired {names & set(NIGHTLY)}"


def test_the_night_after_a_seeded_run_still_fires():
    """The seed must not stop the next night firing."""
    yesterday = date(2026, 9, 6)
    tonight = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    seeded_run = {name: -86400.0 for name in NIGHTLY}
    seeded_date = {name: yesterday for name in NIGHTLY}
    names = {
        j.name for j in worker.due(0.0, seeded_run, local=tonight, last_date=seeded_date)
    }
    assert set(NIGHTLY) <= names


def test_a_spring_forward_night_is_not_skipped():
    """`push-subscription-prune` is anchored at 02:00, the hour Berlin skips on 2026-03-29; "at or
    after the anchor, once per date" survives it."""
    prune = next(j for j in worker.JOBS if j.name == "push-subscription-prune")
    assert prune.anchor_hour == 2, "this test is about the job anchored inside the lost hour"

    readings = [
        datetime(2026, 3, 28, 22, 0, tzinfo=CET), datetime(2026, 3, 28, 23, 0, tzinfo=CET),
        datetime(2026, 3, 29, 0, 0, tzinfo=CET), datetime(2026, 3, 29, 1, 0, tzinfo=CET),
        datetime(2026, 3, 29, 3, 0, tzinfo=CEST), datetime(2026, 3, 29, 4, 0, tzinfo=CEST),
    ]
    assert 2 not in {r.hour for r in readings}, "the fixture must actually lose the hour"

    last_run: dict[str, float] = {}
    last_date: dict[str, date] = {}
    fired = [
        local
        for step, local in enumerate(readings)
        if prune.name in _one_day(local, step * 3600.0, last_run, last_date)
    ]
    assert [d.date() for d in fired] == [date(2026, 3, 28), date(2026, 3, 29)]
    assert fired[1].hour == 3, f"the lost hour pushed it to {fired[1]}"


# An unresolvable `TZ` still boots (§3.1), but must be reported.


def _resolvable_zone() -> str | None:
    """Windows has no tz database, so the resolved branch skips there; CI asserts both."""
    for name in ("Europe/Berlin", "UTC"):
        try:
            ZoneInfo(name)
        except Exception:  # noqa: BLE001 - no tz database is the case this is detecting
            continue
        return name
    return None


@pytest.fixture
def tz(monkeypatch):
    """Set §2's `TZ` for one test, and put `settings()`'s cache back afterwards."""
    def set_to(value: str) -> None:
        monkeypatch.setenv("TZ", value)
        settings.cache_clear()

    yield set_to
    settings.cache_clear()


def test_a_tz_the_container_cannot_resolve_is_reported_at_boot(tz, caplog):
    """`TZ=Europe/Berln` booted green and logged the requested zone while jobs ran on UTC."""
    tz("Europe/Berln")

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_starting(settings())

    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Europe/Berln" in m for m in warned), (
        "an unresolvable TZ is taken silently, at no log level"
    )
    starting = [m for m in (r.getMessage() for r in caplog.records) if m.startswith("worker start")]
    assert starting, "the worker no longer says it is starting"
    assert "Europe/Berln" not in starting[0], (
        "the boot line still prints the configured zone as though it had been honoured"
    )


@pytest.mark.skipif(
    _resolvable_zone() is None, reason="no tz database in this checkout; CI asserts this branch"
)
def test_a_tz_that_does_resolve_is_named_as_itself_and_warns_about_nothing(tz, caplog):
    """So the report is not a warning every boot."""
    zone = _resolvable_zone()
    tz(zone)

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_starting(settings())

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    starting = [m for m in (r.getMessage() for r in caplog.records) if m.startswith("worker start")]
    assert starting and zone in starting[0]


def test_the_loop_still_takes_the_fallback_clock_rather_than_stopping(tz):
    """A worker refusing to start over a spelling would take the household down."""
    tz("Europe/Berln")

    assert worker._local_zone() is None
    assert worker._now_local().tzinfo is None, "the fallback is the process's own naive clock"


def test_the_boot_line_does_not_call_a_broken_install_legal(caplog, tmp_path):
    """`is_empty` is True both for no bundle and for a missing directory; only the first is legal."""
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_basis(ArtifactStore.empty())
    assert "that is legal" in caplog.text, "section 3.1's bundle-less household is still legal"
    assert caplog.text.isascii(), caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_basis(
            ArtifactStore(version="v1", root=tmp_path / "artifacts" / "v1", broken=True)
        )
    line = caplog.text
    assert "that is legal" not in line, (
        "a broken install is not section 3.1's legal state: the files of the ACTIVE bundle are "
        f"gone and every model job will refuse - {line}"
    )
    assert "restore /data/artifacts" in line and "import that bundle again" in line, (
        f"the broken-install line has to name the repair, and both halves of it: {line}"
    )
    assert "Restarting this process does not help" in line, (
        "restart is the instruction for a SWAP; it reloads the same empty store here"
    )
    assert line.isascii(), line

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_basis(ArtifactStore(version="v1", root=tmp_path))
    assert caplog.text == "", f"a healthy basis is not news at boot: {caplog.text}"
