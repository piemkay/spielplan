"""The worker's schedule. Spec v2.1 §5.3, §2 (Backups, TZ); decision 181.

§5.3's table gives each job a trigger, and the registry is meant to be a readable copy of it.
Before M1 the loop ran everything once an hour, which was fine while the only jobs were
hourly and daily prunes — and would have quietly turned §7.3's 1-minute `/Sessions` poll into
a 1-hour one, so a finish prompt would arrive long after the television was off.

M4.7 added the other half. §2's "rotation 14" is a promise about fourteen *nights*, and the
loop measured uptime: `last_run` was an in-process dict with no memory across restarts, a
job that had never run was due immediately, and `ZoneInfo` appeared nowhere in the module —
so every `docker compose up` fired all five daily jobs at once and spent a retention slot,
and fourteen restarts inside twenty-one minutes erased a fortnight of real dumps. The daily
jobs now carry an `anchor_hour` and fire once per local calendar date; the sub-hour ones keep
the monotonic interval, which is what they mean.

`due` is pure, so the schedule is testable without waiting or mocking a clock — including the
wall clock, which the caller supplies rather than this function reading.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from spielplan import worker
from spielplan.core.config import settings

# Every daily job with an implementation: §5.3's four, §2's dump, and `job-run-prune` — which is
# in neither table and is a daily job in every other sense, so it is anchored like the rest.
NIGHTLY = (
    "job-run-prune", "push-subscription-prune", "placement-reconciliation", "ledger-map-refit",
    "fold-in-user-vectors", "nightly-backup",
)

# Europe/Berlin's two offsets, written as fixed offsets rather than as `ZoneInfo("Europe/Berlin")`.
# `due` reads `.hour` and `.date()` and nothing else, so the zone object is incidental here — and a
# Windows checkout has no system tz database (which is exactly what `_now_local`'s fallback is
# for), so a named zone would make these tests error on the platform CLAUDE.md says to keep
# working. The spring-forward case below builds the transition out of these two by hand, which is
# also the more honest fixture: it asserts what the loop sees, an hour that never appears.
CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))


def test_every_registered_job_matches_its_spec_trigger():
    """The `trigger` column is prose from §5.3; `every` is what the loop obeys. They have to
    agree, or the registry is documentation that lies."""
    by_name = {job.name: job for job in worker.JOBS}
    assert by_name["jellyfin-sessions-poll"].every == 60
    assert by_name["jellyfin-seen-sync"].every == 900        # "15 min + webhook"
    assert by_name["session-prune"].every == 3600            # "hourly"
    assert by_name["push-subscription-prune"].every == 86400  # "daily"


def test_the_tick_is_shorter_than_the_shortest_job():
    """Otherwise the shortest interval is a fiction: a job can never run more often than the
    loop wakes."""
    live = [job.every for job in worker.JOBS if job.run is not None]
    assert min(live) > worker.TICK_SECONDS


def test_an_interval_job_that_has_never_run_is_due_immediately():
    """A worker restart should reconcile, not wait out a fifteen-minute interval it has no
    memory of.

    Still true, and now only of the interval jobs: with no wall clock supplied no anchored job
    is due, which is `due`'s honest answer to a question asked without one. That the daily jobs
    are *not* in this set is the fix — "has never run" used to fire them all at every boot.
    """
    names = {job.name for job in worker.due(now=0.0, last_run={})}
    assert "jellyfin-seen-sync" in names
    assert names == {
        job.name for job in worker.JOBS if job.run is not None and job.anchor_hour is None
    }
    assert not (names & set(NIGHTLY))


def test_only_the_elapsed_jobs_are_due():
    last = {job.name: 0.0 for job in worker.JOBS}
    # The minute-interval jobs, and only those. `tier-set-refit` joined them at M3: decision 11
    # adds a second trigger for §5.3's nightly fit, and a person who just changed their tier set
    # should not spend a day looking at equal-mass quantiles instead of fitted cutpoints.
    minutely = {"jellyfin-sessions-poll", "fold-in-tick", "tier-set-refit"}
    at_90s = {job.name for job in worker.due(now=90.0, last_run=last)}
    assert at_90s == minutely

    at_1000s = {job.name for job in worker.due(now=1000.0, last_run=last)}
    assert at_1000s == minutely | {"jellyfin-seen-sync"}


def test_a_job_awaiting_its_milestone_is_never_due():
    """§5.3's later rows are declared so the registry reads like the spec, and `run=None` is
    what says "not yet" out loud instead of a silent omission."""
    pending = [job for job in worker.JOBS if job.run is None]
    assert pending, "the registry should still name the jobs later milestones own"
    due_names = {job.name for job in worker.due(now=1e9, last_run={})}
    assert not due_names & {job.name for job in pending}


def test_the_registry_covers_the_milestones_it_claims():
    milestones = {job.milestone for job in worker.JOBS}
    assert {"M0", "M1", "M2", "M5", "M6"} <= milestones
    live_m1 = {job.name for job in worker.JOBS if job.milestone == "M1" and job.run is not None}
    assert live_m1 == {
        "jellyfin-seen-sync", "jellyfin-sessions-poll", "webauthn-challenge-prune"
    }


def test_the_placement_sweep_runs_before_the_fits_that_read_its_coordinates():
    """§5.3 lists the two nightly fits above the placement sweep, but both fits read the
    coordinates the sweep writes: §5.1's `e(t)` needs ê for a cold or low-support title, and
    §5.2's fit takes the same coordinates as its embeddings.

    Left in table order, the night a bundle arrives fits every user against a library whose
    newly-owned titles have no coordinate yet, and the sweep corrects it a day later — once per
    import, silently, and only ever in the direction that makes the first night's tiers worse.
    The registry stays in §5.3's order for reading; `due` sorts by `stage`.
    """
    # Late enough in the household's day that all three anchors have passed, which is the tick a
    # first boot in the evening produces — the one case where they do share a tick.
    late = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    order = [j.name for j in worker.due(1e9, {}, local=late, last_date={})]
    assert order.index("placement-reconciliation") < order.index("fold-in-user-vectors")
    assert order.index("placement-reconciliation") < order.index("ledger-map-refit")

    # …and the table itself is still §5.3's, so the registry has not been reordered to fake it.
    table = [j.name for j in worker.JOBS]
    assert table.index("ledger-map-refit") < table.index("placement-reconciliation")


def test_the_fold_in_runs_often_enough_to_answer_within_a_sitting():
    """§12's M2 exit criterion — "50-100 verdicts each produce visibly personal rankings" — is a
    claim about what a person sees during a sitting, and every §6.0 shelf orders by a table only
    the fold-in writes.

    A strictly nightly fold-in cannot meet it: the tier badges move on every tap (the
    interactive path writes `ledger_state`) while the shelves stay in the order they had that
    morning, for up to a day. §5.3's nightly pass stays exactly as §5.3 writes it; this asserts
    the tick exists alongside it and is measured in minutes, not hours.
    """
    tick = next(j for j in worker.JOBS if j.name == "fold-in-tick")
    nightly = next(j for j in worker.JOBS if j.name == "fold-in-user-vectors")
    assert tick.run is not None, "M2 owes this one an implementation"
    assert tick.every <= 300, "a sitting is minutes long; an hourly tick is a nightly job"
    assert nightly.every == 86400, "§5.3's nightly pass is not replaced by the tick"


# --- M4.7: a night is a night, not 24 hours of uptime -----------------------------------------


def _one_day(local: datetime, now: float, last_run, last_date) -> list[str]:
    """One tick, stamped exactly the way `_tick` stamps a *successful* run.

    The stamping is the half a pure `due` test cannot skip: the schedule is `due` plus what the
    caller records, and the defect this file now guards was in the second half — nothing recorded
    anything across a restart at all.
    """
    fired = [job.name for job in worker.due(now, last_run, local=local, last_date=last_date)]
    for job in worker.JOBS:
        if job.name not in fired:
            continue
        last_run[job.name] = now
        if job.anchor_hour is not None:
            last_date[job.name] = local.date()
    return fired


def test_every_live_daily_job_is_anchored_to_an_hour_of_the_household_s_day():
    """§2's "rotation 14" and §5.3's "nightly" are claims about nights. A daily job with no
    anchor is one that means "every 24 h of uptime", which is the defect, so the registry is
    what has to carry the anchor rather than a comment."""
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
    """`_tick` awaits due jobs one after another, so every daily job sharing a tick is one long
    block during which §7.3's minute poll does not run — and one of them is a `pg_dump` whose
    budget §5.3 writes as "minutes".

    The order is asserted too, because it is not arbitrary: the sweep writes the coordinates both
    fits read (`stage`), and the dump goes last so it captures the night's work. `job-run-prune`
    goes first so the fortnight it keeps is fourteen whole nights rather than thirteen and part of
    tonight.
    """
    anchors = {j.name: j.anchor_hour for j in worker.JOBS if j.anchor_hour is not None}
    assert len(set(anchors.values())) == len(anchors), f"two jobs share an hour: {anchors}"
    assert anchors["placement-reconciliation"] < anchors["ledger-map-refit"]
    assert anchors["placement-reconciliation"] < anchors["fold-in-user-vectors"]
    assert anchors["nightly-backup"] == max(anchors.values())
    assert anchors["job-run-prune"] == min(anchors.values())


def test_a_nightly_job_fires_once_per_local_calendar_date_at_its_anchor():
    """Two whole days, read hour by hour on the household's clock (§2's `TZ`).

    The old rule was `now - last_run >= 86400`, which is a claim about elapsed uptime: it fires
    at whatever hour the worker happened to start and drifts by however long each tick took.
    """
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
    """Midnight is not the night §2 means, and a job that fires the moment the date rolls over
    would run against a database still serving whoever is up at 00:05."""
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
    """The measured failure, in the units §2 promises: fourteen simulated restarts inside
    twenty-one minutes over a directory holding fourteen nights left zero nights of history.

    A restart is modelled the way `main` produces one — `last_run` and `last_date` re-derived
    from `job_run`'s newest successful run per job — so what is under test is that the seed is
    load-bearing, not that a long-lived dict remembers.
    """
    local = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    seeded_run = {name: -3600.0 for name in NIGHTLY}   # ran an hour ago, per job_run
    seeded_date = {name: local.date() for name in NIGHTLY}

    for restart in range(14):
        # Each restart's loop clock starts near zero and the seed carries the previous run's age.
        names = {
            j.name
            for j in worker.due(90.0 * restart, dict(seeded_run), local=local,
                                last_date=dict(seeded_date))
        }
        assert not (names & set(NIGHTLY)), f"restart {restart} re-fired {names & set(NIGHTLY)}"


def test_the_night_after_a_seeded_run_still_fires():
    """The other half of the same seed, and the reason the plan says not to stamp `now()` at
    boot: a schedule that cannot re-fire trades spending a night for silently skipping one."""
    yesterday = date(2026, 9, 6)
    tonight = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    seeded_run = {name: -86400.0 for name in NIGHTLY}
    seeded_date = {name: yesterday for name in NIGHTLY}
    names = {
        j.name for j in worker.due(0.0, seeded_run, local=tonight, last_date=seeded_date)
    }
    assert set(NIGHTLY) <= names


def test_a_spring_forward_night_is_not_skipped():
    """Europe/Berlin loses 02:00-03:00 on 2026-03-29, and `push-subscription-prune` is anchored
    at 02:00 — the hour that does not exist that night.

    The readings are what the loop would see: 00 and 01 on CET, then 03 onwards on CEST, with no
    02 anywhere. "At or after the anchor, once per date" survives that. "Exactly at the anchor
    hour" would have skipped a night once a year, silently, in the direction nobody checks.
    """
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


# --- M4.7 cycle 2: which clock the anchors were actually resolved against ----------------------
#
# The anchoring above is only as good as `TZ`, and M4.7 is what made `TZ` load-bearing: six jobs,
# §2's nightly dump among them, now fire at an hour of the household's own day. A zone that does
# not resolve is still not a refusal — §3.1 keeps a boot legal and a job fired against the wrong
# clock beats a loop that will not start — but it stopped being allowed to be silent.


def _resolvable_zone() -> str | None:
    """A zone name this checkout can actually resolve, or None if it has no tz database at all.

    A Windows checkout has neither `/usr/share/zoneinfo` nor, unless someone installed it, the
    `tzdata` wheel — which is exactly the condition `_now_local`'s fallback exists for, and why
    the *fallback* branch is the one that can be asserted everywhere while the resolved branch
    is skipped where there is nothing to resolve. CI runs on Linux and asserts both.
    """
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
    """The typo that moves the whole night, said out loud once.

    `_now_local`'s fallback swallowed an unresolvable zone at no log level at all, and the one
    line the worker writes at boot printed `settings().tz` — the string that was *asked* for — as
    though it had been honoured. So `TZ=Europe/Berln` gave a container that boots green, a log
    that agrees with the operator's spelling, and a household whose 06:00 dump, placement sweep
    and both fits silently moved to the process's own zone, which in a container is UTC. Every
    instrument in the box confirmed the configuration that was not in force.
    [M4.7 cycle 2 finding 11]
    """
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
    """The counterpart, so the report cannot become a warning every household reads every boot."""
    zone = _resolvable_zone()
    tz(zone)

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_starting(settings())

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    starting = [m for m in (r.getMessage() for r in caplog.records) if m.startswith("worker start")]
    assert starting and zone in starting[0]


def test_the_loop_still_takes_the_fallback_clock_rather_than_stopping(tz):
    """§3.1's half of the rule, unchanged: reporting the substitution is not refusing it.

    A worker that would not start on a mistyped `TZ` would take the whole household's sync,
    prompts and dumps down over a spelling — a worse failure than the one being reported.
    """
    tz("Europe/Berln")

    assert worker._local_zone() is None
    assert worker._now_local().tzinfo is None, "the fallback is the process's own naive clock"
