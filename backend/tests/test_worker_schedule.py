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

import ast
import importlib.util
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
    # `ledger-refresh` joined them at M4.13 and is a *third* trigger for the same fit: the
    # incremental path moves r and not v, so the rest of the library holds the first tap's estimate
    # until a full fit runs. [M4.13, dd22; plan step 27]
    minutely = {"jellyfin-sessions-poll", "fold-in-tick", "tier-set-refit", "ledger-refresh"}
    at_90s = {job.name for job in worker.due(now=90.0, last_run=last)}
    assert at_90s == minutely

    at_1000s = {job.name for job in worker.due(now=1000.0, last_run=last)}
    assert at_1000s == minutely | {"jellyfin-seen-sync"}


def test_every_sentence_that_counts_the_minutely_jobs_counts_the_registry():
    """Four comments size `job_run` retention, the per-job timeout budget and the INFO threshold
    off "the three 60-second jobs". Registering `ledger-refresh` at `every=60` made them four and
    moved the daily row total from 4,320 to 5,760, and the source kept the old number in all four
    places while THIS file's sibling comment was updated -- so the arithmetic behind
    `JOB_RUN_KEEP_DAYS`, the 55 s budgets and `DURATION_LOG_THRESHOLD` reads a third low.

    A count in a comment is a measurement (CLAUDE.md), so it is checked against the registry
    rather than against a constant: the next job registered at sixty seconds fails this test in
    the same four places, which is where the sentences are. 4,320 is deliberately not searched
    for -- `86400 / TICK_SECONDS` is also 4,320 and those two comments are still right.
    [M4.13 cycle 2, M413-D6-04]
    """
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    minutely = [job for job in worker.JOBS if job.every == 60]
    expected = words[len(minutely)]
    source = Path(worker.__file__).read_text(encoding="utf-8")

    counted = re.findall(
        r"(\w+) (?:`every=60` jobs|60-second rows|60-second jobs|that fire every sixty)", source
    )
    assert len(counted) == 4, (
        f"the sentences that count the minutely jobs moved; found {counted}"
    )
    assert set(counted) == {expected}, (
        f"{len(minutely)} jobs run every 60 s ({sorted(j.name for j in minutely)}), "
        f"and the source says {sorted(set(counted))}"
    )
    assert f"{len(minutely) * 1440:,}" in source, (
        "the rows-a-day and lines-a-day figures are the count times 1,440 and have to move with it"
    )


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

    A strictly nightly fold-in cannot meet it: the tier badges move within the sitting (the
    interactive path writes `ledger_state` — under the hand where the fit cache is warm, on the
    60 s sweep finding 9 hands a miss to) while the shelves stay in the order they had that
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


# --- M4.10: the registry against §5.3's own table, and a boot line that counts something ------
#
# `test_every_registered_job_matches_its_spec_trigger` weighs four entries against the cadences
# §5.3 gives them, which is the half that can be written from memory. What nothing here did was
# open the table. §5.3 has nine rows and this tuple carried eight of them: the missing one is row
# 9, bundle import validation and hot swap, which is also the only row implemented inside a
# request handler and the one whose 127 s POST no instrument in the box could see. A module that
# documents itself as "the registry that says so out loud rather than a process that silently
# does nothing" has to be held to the document it copies by something that reads it.
# [M4.10 finding 35]

SPEC_DOC = Path(__file__).resolve().parents[2] / "docs" / "spielplan-spec_v2.1.md"

# The module the boot-census call-site rule below reads. A source path and not `inspect`, because
# the question is where one statement sits inside `main()` rather than what a function closes over.
# [M4.10 cycle 1, m410-rev1-boot-census-call-site-is-untested]
WORKER_SOURCE = Path(worker.__file__)

# §5.3's nine rows, keyed by the opening of each Job cell, against the registry name that stands
# for it. Written out rather than derived, because the mapping IS the claim: "Seen-state sync
# with Jellyfin" and `jellyfin-seen-sync` are one row only because a person says they are, and a
# renamed job that quietly stops answering for a spec row is the failure being guarded.
SPEC_5_3 = {
    "Ledger incremental update": "ledger-incremental",
    "Ledger full MAP refit": "ledger-map-refit",
    "Fold-in user vectors": "fold-in-user-vectors",
    "Cold Tower placement": "cold-tower-placement",
    "Placement reconciliation": "placement-reconciliation",
    "DNA projection": "dna-projection",
    "Seen-state sync with Jellyfin": "jellyfin-seen-sync",
    "Explore-frontier": "explore-frontier-cache",
    "Bundle import validation": "bundle-import",
}


def _spec_jobs_table() -> list[tuple[str, str, str]]:
    """§5.3's table as (job, trigger, budget), read out of the normative document.

    Cells go through `ascii()` wherever they reach an assertion message: the budget column holds
    an em dash and the job column holds § signs, and a Windows cp1252 console dies on either.
    This guard may fail on this machine; it may not crash the runner reporting it.
    """
    rows: list[tuple[str, str, str]] = []
    lines = SPEC_DOC.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### 5.3 Jobs"))
    for line in lines[start + 1:]:
        if line.startswith(("#", "---")):
            break
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] == "Job" or set(cells[0]) <= set("-: "):
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def test_every_row_of_the_spec_s_jobs_table_has_a_registry_entry():
    """The tuple calls itself a copy of §5.3's table; this is the diff against the original.

    Trigger is compared as a prefix because the registry drops the section pointers the table
    carries ("acquisition pipeline (§8)"); budget is compared exactly, because it is the number
    §5.3 holds the job to and a rounded copy is a different promise.
    """
    table = _spec_jobs_table()
    by_name = {job.name: job for job in worker.JOBS}
    problems: list[str] = []
    seen: set[str] = set()

    for job_cell, trigger, budget in table:
        matches = [key for key in SPEC_5_3 if job_cell.startswith(key)]
        if len(matches) != 1:
            problems.append(f"the table row {ascii(job_cell[:50])} maps to {matches}")
            continue
        seen.add(matches[0])
        name = SPEC_5_3[matches[0]]
        job = by_name.get(name)
        if job is None:
            problems.append(f"section 5.3 row {matches[0]!r} has no {name!r} entry in JOBS")
            continue
        if not trigger.startswith(job.trigger):
            problems.append(
                f"{name}: registry trigger {ascii(job.trigger)} is not the table's "
                f"{ascii(trigger)}"
            )
        if job.budget != budget:
            problems.append(
                f"{name}: registry budget {ascii(job.budget)} is not the table's {ascii(budget)}"
            )

    assert not problems, problems
    missing = sorted(set(SPEC_5_3) - seen)
    assert not missing, f"rows this guard expects and the table no longer has: {missing}"
    assert len(table) == 9, f"section 5.3 now has {len(table)} rows, not nine"


def test_a_job_this_loop_does_not_fire_says_which_of_the_three_things_that_means():
    """`run=None` meant three states at once, and the registry could not tell them apart.

    Two of them are code that ships: the incremental Ledger update runs in the web process on
    every tap, and the bundle import runs inside the admin's POST. One is code that ships and is
    reached through another job - the Cold Tower's forward pass, which the placement sweep calls
    until §8's acquisition pipeline exists. Only the last two rows here are genuinely unwritten.
    Pinned as sets rather than as counts, because the failure being guarded is a row drifting
    from one bucket to another silently, which a count cannot see. [M4.10 finding 35]
    """
    elsewhere = {j.name: j.owner for j in worker.JOBS if j.run is None and j.owner is not None}
    awaiting = {j.name: j.milestone for j in worker.JOBS if j.run is None and j.owner is None}

    assert set(elsewhere) == {"ledger-incremental", "cold-tower-placement", "bundle-import"}
    assert set(awaiting) == {"dna-projection", "explore-frontier-cache"}
    assert sorted(awaiting.values()) == ["M5", "M6"], (
        "a job with neither an implementation nor an owner has to name the milestone that owes "
        f"it one, and these name a milestone this build has already shipped: {awaiting}"
    )
    for name, module in elsewhere.items():
        assert importlib.util.find_spec(module) is not None, (
            f"{name} names {module!r} as its implementation and that module does not exist"
        )
    # `owner` means "this loop does not fire it". A live job carrying one would make the census
    # below double-count and, worse, would read as documentation that the loop is not the caller.
    assert not [j.name for j in worker.JOBS if j.run is not None and j.owner is not None]


def _census_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if "job(s) live" in r.getMessage()]
    assert len(lines) == 1, f"the boot census is not one line: {lines}"
    return lines[0]


def test_the_boot_census_counts_the_registry_rather_than_a_number_somebody_typed(
    monkeypatch, caplog
):
    """Four fabricated rows, one of each state, and the line has to follow them.

    The old line derived "awaiting their milestone" from `run is None` alone, so its arithmetic
    was right and its category was wrong. Substituting the registry is the only way to assert
    that the counts are computed here rather than restated: against a tuple whose names the real
    one does not share, a number somebody typed cannot survive.
    """
    async def _noop() -> None:
        return None

    monkeypatch.setattr(worker, "JOBS", (
        worker.Job("fired-here", "M0", "hourly", "ms", _noop, every=3600),
        worker.Job("runs-on-a-tap", "M0", "every observation", "ms", owner="spielplan.api.rate"),
        worker.Job("runs-on-a-post", "M0", "admin action", "minutes",
                   owner="spielplan.importer.bundle"),
        worker.Job("nobody-has-written-it", "M9", "nightly", "minutes"),
    ))

    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_registry()

    line = _census_line(caplog)
    assert line.startswith("1 job(s) live in this loop; 2 run outside it: ")
    assert "runs-on-a-tap(spielplan.api.rate)" in line
    assert "runs-on-a-post(spielplan.importer.bundle)" in line
    assert "1 awaiting their milestone: nobody-has-written-it(M9)" in line
    assert "fired-here" not in line, "the live jobs are counted, not listed - there are twelve"
    assert line.isascii(), f"the boot line a cp1252 console has to print is not ASCII: {line!r}"


def test_the_boot_census_no_longer_reports_two_shipped_jobs_as_pending(caplog):
    """The sentence an operator actually read, against the registry they actually have.

    "4 awaiting their milestone: ledger-incremental(M2), cold-tower-placement(M2), ..." was false
    about half of what it named, at every boot, in the one line this process writes about its own
    contents. The remainder is asserted exactly rather than counted, because the count was never
    the part that was wrong.
    """
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_registry()

    line = _census_line(caplog)
    assert line.endswith(
        "2 awaiting their milestone: dna-projection(M5), explore-frontier-cache(M6)"
    ), line
    outside = line.split("run outside it: ", 1)[1].split(";", 1)[0]
    assert "ledger-incremental(spielplan.ledger.refit)" in outside
    assert "cold-tower-placement(spielplan.placement.tower)" in outside
    assert "bundle-import(spielplan.importer.bundle)" in outside
    assert line.isascii(), f"the boot line a cp1252 console has to print is not ASCII: {line!r}"


def _census_calls_in_main(source: str) -> list[str]:
    """Where `main()` calls `_report_registry`, and what stands between it and the boot path.

    A list of descriptions rather than a bool, because there are three ways to lose the line and
    the operator cannot tell them apart: no call at all, a call behind a branch this container
    never takes, and a call in some other function that nothing boots. A `try` body and an
    `async with` body are unconditional once `main` is running, so they are walked through; an
    `if`, a loop, an `except` and a `try`'s `else` are not.
    """
    tree = ast.parse(source)
    main = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "main"
        ),
        None,
    )
    if main is None:
        return ["worker.py has no main()"]

    found: list[str] = []

    def walk(body: list[ast.stmt], guard: str | None) -> None:
        # The compound statements are descended into and then skipped, so a call inside one is
        # reported once, under its own guard, rather than twice -- `ast.walk` over a whole
        # statement would find it again and call it unconditional.
        for stmt in body:
            if isinstance(stmt, ast.Try):
                walk(stmt.body, guard)
                for handler in stmt.handlers:
                    walk(handler.body, f"behind `except` at line {handler.lineno}")
                walk(stmt.orelse, f"behind the `else` of a `try` at line {stmt.lineno}")
                walk(stmt.finalbody, guard)
                continue
            if isinstance(stmt, ast.With | ast.AsyncWith):
                walk(stmt.body, guard)
                continue
            if isinstance(stmt, ast.If):
                walk(stmt.body, f"behind an `if` at line {stmt.lineno}")
                walk(stmt.orelse, f"behind an `else` at line {stmt.lineno}")
                continue
            if isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
                walk(stmt.body, f"inside a loop at line {stmt.lineno}")
                walk(stmt.orelse, f"inside a loop at line {stmt.lineno}")
                continue
            if isinstance(stmt, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
                walk(stmt.body, f"inside a nested definition at line {stmt.lineno}")
                continue
            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_report_registry"
                ):
                    found.append(guard or "unconditional")
                    break

    walk(main.body, None)
    return found


def test_the_boot_census_is_actually_called_at_boot():
    """The two tests above call `_report_registry()` themselves, so neither can see the one call
    site that makes the line a boot line.

    `grep -rn "_report_registry" backend/` finds three places: the definition, one call inside
    `main()`, and those tests. Delete the call and all four registry tests stay green while the
    coverage row goes on claiming that an operator reads these counts at every boot — which is
    the shape of the very defect this row repairs, a line nobody read telling nobody something
    false for two milestones.

    Read off the source rather than by booting `main()`: booting it needs a pool, the migration
    wait and a signal handler, and what is in doubt is one statement's position, which is a fact
    about the code. `test_auth_logic.py:124` reads the CSPRNG the same way and for the same
    reason. Unconditional matters as much as present: behind the migration wait's `else`, or
    behind `if store.is_empty`, the line would be missing on exactly the boots an operator is
    reading the log for. [M4.10 finding 35; cycle 1, m410-rev1-boot-census-call-site-is-untested]
    """
    calls = _census_calls_in_main(WORKER_SOURCE.read_text(encoding="utf-8"))
    assert calls == ["unconditional"], (
        "main() must call _report_registry() exactly once and on every boot; found: "
        f"{calls or 'no call at all'}"
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # `main()`'s own shape: inside the try whose `finally` closes the pool.
        ("the shape that ships",
         "async def main():\n    try:\n        _report_registry()\n    finally:\n        pass\n",
         ["unconditional"]),
        ("at the top of main", "async def main():\n    _report_registry()\n", ["unconditional"]),
        ("inside an async with", "async def main():\n    async with pool.acquire() as conn:\n"
                                 "        _report_registry()\n", ["unconditional"]),
        ("the call deleted",
         "async def main():\n    try:\n        pass\n    finally:\n        pass\n", []),
        ("behind a branch the container may not take",
         "async def main():\n    if store.is_empty:\n        _report_registry()\n",
         ["behind an `if` at line 2"]),
        ("behind the migration wait's else",
         "async def main():\n    try:\n        pass\n    except OSError:\n"
         "        _report_registry()\n",
         ["behind `except` at line 4"]),
        ("called twice", "async def main():\n    _report_registry()\n    _report_registry()\n",
         ["unconditional", "unconditional"]),
        ("defined but booted by nothing",
         "def _boot():\n    _report_registry()\nasync def main():\n    pass\n", []),
    ],
)
def test_the_boot_census_call_site_guard_catches_a_real_violation(name, source, expected):
    """A guard that cannot see its own violation is the M4.7 lesson, so each shape is named."""
    assert _census_calls_in_main(source) == expected, name
