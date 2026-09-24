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
import inspect
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spielplan import worker
from spielplan.core.config import settings
from spielplan.models.artifacts import ArtifactStore

# The prose readers, borrowed rather than re-written: `_comment_prose` is this tree's notion of
# what a reader reads - tokenized comments and docstrings off the AST, so a count inside a string
# literal is not prose - and `_COUNT_WORDS` is its vocabulary for a hand-spelled one. One reader
# of this class rather than two that spell numbers differently. [M5.1 review cycle 2]
from tests.test_static_contracts import _COUNT_WORDS, _comment_prose

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
    # §7.2's two intake paths joined at M5.2, and neither is a fifth minute-interval row:
    # `jellyfin-intake-sweep` runs every 300 s, which is why it is absent from `at_90s` above and
    # present here, and `jellyfin-delta-poll` carries §7.2's own fifteen minutes. Decision 368
    # argues the 300 s rather than 60 against the four arithmetic sentences in `worker.py` that
    # are sized on the `every=60` count, and this pair of assertions is where that choice is
    # visible: one set says the sweep is not minutely, the other says it is not hourly either.
    assert at_1000s == minutely | {
        "jellyfin-seen-sync", "jellyfin-delta-poll", "jellyfin-intake-sweep"
    }


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

    One of them is code that ships in another process: the incremental Ledger update runs in the
    web process on every tap. One is code that ships and is reached through another job - the
    Cold Tower's forward pass, which the placement sweep calls until §8's acquisition pipeline
    exists. Only the last two rows here are genuinely unwritten. Pinned as sets rather than as
    counts, because the failure being guarded is a row drifting from one bucket to another
    silently, which a count cannot see. [M4.10 finding 35]

    **`bundle-import` was in the first set until M4.14 and is now live.** It was the sharpest
    example the paragraph above had - work that ships and is triggered by a request - and §5.3
    files it as a job with a "minutes" budget, so the 127 s it spent on the web process's event
    loop was the registry pointing at its own defect. `_bundle_import` claims the `job_run` row
    the route writes, which is what moves it across this line; the `owner` had to go with it,
    because a live job carrying one is counted twice by the census below and reads as
    documentation that this loop is not the caller. [M4.14 step E2, decision 253]
    """
    elsewhere = {j.name: j.owner for j in worker.JOBS if j.run is None and j.owner is not None}
    awaiting = {j.name: j.milestone for j in worker.JOBS if j.run is None and j.owner is None}

    assert set(elsewhere) == {"ledger-incremental", "cold-tower-placement"}
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


# The three names that acquire the ACTIVE bundle inside this process: `_active_store`, the
# worker's one door to it and the place §10's two assertions are made; `load_active`, the door
# `_active_store` itself opens; and `ArtifactStore.open`, the constructor `load_active` itself
# tail-calls and the idiom a job that wants the staged DIRECTORY rather than the mapping reaches
# for (`importer/bundle.py`, `importer/validate.py`). A job that reaches any of the three fits in
# that basis and writes numbers expressed in it.
#
# The third is spelled with its type because neither half of it is a door alone: `open` by itself
# is every `Path.open` in a body, and `ArtifactStore` by itself is `ArtifactStore.empty()` -
# §3.1's bundle-less sentinel, which three of today's six spell - and the annotations on
# `_active_store` and `_report_basis`. Matching the type holds a job out of the loop for naming
# the EMPTY store, which is `probe-comment`'s failure wearing a different hat.
# [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]
_BASIS_NAMES = frozenset({"_active_store", "load_active", "ArtifactStore.open"})


def _named(fn: ast.AST) -> set[str]:
    """Every name this function body mentions, attribute access included: `_active_store(c)` and
    `worker._active_store(c)` are the same acquisition seen from two modules.

    `X.y` is emitted qualified as well as bare, because the third door in `_BASIS_NAMES` can only
    be named by both of its halves at once - the comment there argues why.
    [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]

    The qualified form is what carries the walk across a file too: `bb.load_for` is the alias and
    the function in one string, which is all `_one_module_out` needs to find the file.
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
            if isinstance(node.value, ast.Name):
                out.add(f"{node.value.id}.{node.attr}")
    return out


def _imports(tree: ast.AST, source: Path) -> dict[str, tuple[str, str | None]]:
    """`alias -> (module, attribute)` for every import a file carries, top level or not.

    Not only the top level, because a job imports its helpers inside the body that uses them -
    `_ledger_map_refit` opens with three such lines and `worker.py`'s module scope names almost
    nothing a model job actually calls. A name that meant two different modules in two functions
    of one file would collapse here, and is not a shape this codebase writes.
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    out: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bare = alias.name.split(".")[0]
                out[alias.asname or bare] = (alias.name if alias.asname else bare, None)
        elif isinstance(node, ast.ImportFrom):
            # A relative import resolves to nothing here, so the job carrying it would be cleared
            # for a reason that is this function's and not the job's - the silent clearing this
            # whole derivation exists to refuse. `spielplan` spells every import absolutely today;
            # this assertion is what says so out loud if that ever stops being true.
            assert not node.level, (
                f"{source.name} carries a relative import, which this resolution cannot follow: "
                "spell it absolutely rather than leaving a job cleared by an unresolved call"
            )
            for alias in node.names:
                out[alias.asname or alias.name] = (node.module, alias.name)
    return out


# Parsed once per file, because the walk below re-enters `worker.py` for every job in the registry
# and crosses into a dozen more: measured at M4.16, twenty files for the fourteen live jobs that
# registry then held, in 0.04 s. M5.1's `acquisition-drain` makes fifteen and M5.2's two §7.2
# intake rows make seventeen; the figure is left as the measurement it was rather than grown by
# arithmetic nobody ran.
_PARSED: dict[Path, tuple[dict, dict]] = {}


def _parse(source: Path) -> tuple[dict, dict]:
    """One file's module-level functions by name, and `_imports`' alias map for the same file."""
    cached = _PARSED.get(source)
    if cached is None:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        defs = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        cached = _PARSED[source] = (defs, _imports(tree, source))
    return cached


def _one_module_out(mention: str, imports: dict[str, tuple[str, str | None]]) -> tuple[Path, str] | None:
    """The file and the function a mention names in another module of this package, or None.

    The walk stops at the PACKAGE rather than one module short of it: `spielplan` is the only code
    that can acquire this install's basis - a call into asyncpg or numpy cannot - and inside it a
    job's helper is as likely to sit in `scoring/` or `placement/` as beside the job, which is
    where CLAUDE.md puts rules ("rules live in the domain packages under `backend/spielplan/`").
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    alias, _, attr = mention.partition(".")
    if attr:
        # `bb.load_for`, after `from spielplan.scoring import backbone as bb`: the alias names the
        # module and the attribute names the function in it.
        target = imports.get(alias)
        if target is None:
            return None
        module = target[0] if target[1] is None else f"{target[0]}.{target[1]}"
        func = attr
    else:
        # `load_hp`, after `from spielplan.ledger.hyperparams import load as load_hp`: the
        # function came in by name and its alias carries the module it came from.
        target = imports.get(mention)
        if target is None or target[1] is None:
            return None
        module, func = target
    if module.split(".")[0] != "spielplan":
        return None
    try:
        spec = importlib.util.find_spec(module)
    except ImportError:
        # `settings.artifacts_dir` resolves `settings` to `spielplan.core.config.settings`, which
        # is a callable and not a module. Nothing to follow, and nothing wrong.
        return None
    origin = spec.origin if spec is not None else None
    return (Path(origin), func) if origin and origin.endswith(".py") else None


def _reaches_the_basis(run) -> bool:
    """Does `run` acquire the active bundle - itself, or through a helper it calls?

    `"_active_store" in inspect.getsource(job.run)` was the first form of this, and it is a text
    property of ONE function rather than the property the test below claims. Three ways past it,
    all of them ordinary: a job that reaches the basis one call frame out - which is the refactor
    a seventh fit invites, once there are six to share a helper with - a job that calls
    `ArtifactStore.load_active` itself, and, in the other direction, a job whose COMMENT names
    `_active_store`, which the substring forced INTO the set. The first two leave the guard green
    while `_tick` never skips the job, which is exactly the silent failure a hand-kept list
    produces. `ast` is also what makes the third go away for free: a comment is not a node.
    [M4.14 cycle 1, m414-c1-dim-lock-06]

    A fourth shape went past the `ast` form too, and past the substring before it: a job that
    resolves the active version itself and builds the store with `ArtifactStore.open`, because it
    wants the DIRECTORY rather than the None-for-empty mapping `_active_store` returns.
    `dna-projection` - the seventh `worker.JOBS` already carries, `run=None` and M5 owing it an
    implementation - is that shape, and `importer/bundle.py` and `importer/validate.py` both write
    the idiom today. The paragraph above listed `load_active` among the doors it had closed and
    `open` is that constructor's sibling, so the guard claimed this one shut a milestone before it
    was. [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]

    One module deep was the boundary until cycle 3, and it cleared the fifth shape - which is the
    first one's, moved one file out. The assertion below guards `run` ITSELF, so a job whose `run`
    is a module-level function and whose acquisition sits one call away in `scoring/` or
    `placement/` - the shared helper this docstring has anticipated since cycle 1, put where
    CLAUDE.md puts rules - was not asserted on and was not derived: seen, cleared, and never
    skipped while an import held the lock. So the walk follows a call into any module of this
    package, resolved from the imports the file itself carries.

    What that boundary was protecting is answered by a name instead. The basis is loaded outside
    the worker too - `importer/bundle.active_backbone_coverage` opens it for decision 248's
    reverse coverage check - so a package-wide walk derives `bundle-import` into the set, and
    skipping the import job while an import holds the lock is the one thing `_tick` must never
    do. Measured over today's registry the walk derives `MODEL_JOBS` and that one job and nothing
    else, at one hop and at every depth past it, so the guard below drops it by
    `worker.BUNDLE_IMPORT_JOB` rather than by keeping the walk too short to see it. The package is
    where the walk does stop, because code this repository did not write cannot open this
    install's basis; and a `run` that is not a module-level function is still an assertion rather
    than a False, because a derivation that cannot see a job has to say so instead of clearing it.
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    source = Path(inspect.getsourcefile(run))
    defs, _ = _parse(source)
    assert run.__name__ in defs, (
        f"{run.__qualname__} is not a module-level function of {source.name}, so this derivation "
        "cannot see what it calls; widen it rather than leaving a job unclassified"
    )

    seen: set[tuple[Path, str]] = set()
    pending = [(source, run.__name__)]
    while pending:
        file, name = pending.pop()
        if (file, name) in seen:
            continue
        seen.add((file, name))
        here, imports = _parse(file)
        if name not in here:
            continue
        mentions = _named(here[name])
        if mentions & _BASIS_NAMES:
            return True
        pending.extend((file, callee) for callee in mentions & set(here))
        pending.extend(
            out for out in (_one_module_out(m, imports) for m in mentions) if out is not None
        )
    return False


async def _the_shared_basis_helper(conn):
    """The helper M5's seventh fit will share with the sixth: one acquisition, six callers."""
    return await worker._active_store(conn)


async def _probe_fits_through_a_helper() -> None:
    """A model job written with one frame of indirection, and nothing else unusual about it."""
    await _the_shared_basis_helper(None)


async def _probe_loads_the_store_itself() -> None:
    """A model job that opens the basis directly, naming neither the worker's door nor a helper."""
    await ArtifactStore.load_active(None, settings().artifacts_dir)


async def _probe_only_names_the_basis_in_a_comment() -> None:
    # `_active_store` is named here and called nowhere, which is what a comment is: this job
    # writes nothing expressed in a basis and must not be held out of the loop.
    return None


async def _probe_opens_the_active_directory_by_path() -> None:
    """A model job that resolves the active version itself and opens that DIRECTORY.

    The shape `dna-projection` will have: it needs the tree's files, not `_active_store`'s
    None-for-empty mapping, so it makes `load_active`'s own SELECT and calls the constructor
    `load_active` tail-calls. It writes in the active basis and names neither of the other two
    doors, so before this cycle it derived False, stayed out of `MODEL_JOBS`, and was never
    skipped while an import held the lock - a projection started against v1 and committed after
    §10's flip, in a basis the install no longer serves.
    [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]
    """
    conn = None  # never called: this body exists to be parsed, like the three above.
    version = await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    ArtifactStore.open(settings().artifacts_dir / version, version)


async def _probe_names_the_type_without_the_door() -> None:
    """A job that touches `ArtifactStore` and acquires no basis at all.

    The guard on the cure rather than on the disease. Closing `probe-open` by putting the TYPE
    in `_BASIS_NAMES` also derives this one - §3.1's bundle-less sentinel and an annotation,
    which is what `_ledger_map_refit`, `_fold_in_tick` and `_tier_set_refits` each spell in their
    own bodies - and a job held out of the loop for naming the empty store is `probe-comment`'s
    failure in a new hat. Measured: with `"ArtifactStore"` as the name, this probe derives True.
    [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]
    """
    store: ArtifactStore = ArtifactStore.empty()
    assert store.is_empty


async def _probe_reaches_the_basis_one_module_out() -> None:
    """A job whose `run` is module-level and whose basis acquisition is one module away.

    The shape the first paragraph above anticipates - "the refactor a seventh fit invites, once
    there are six to share a helper with" - with the shared helper where CLAUDE.md puts rules
    ("rules live in the domain packages under `backend/spielplan/`") rather than in `worker.py`.
    `run` IS a module-level function here, so the assertion that guards the boundary never fires
    and there is nothing to widen: the walk ran out of MODULE and returned False, and the job was
    seen, cleared, and never skipped while an import held the lock - `probe-helper`'s failure with
    the helper one import away.

    `active_backbone_coverage` rather than an invented helper, because it is the acquisition one
    module out that this codebase already writes: decision 248's reverse coverage check, opening
    on `ArtifactStore.load_active`. The import sits inside the body for the same reason every
    model job's does, and this body is never executed, so naming it costs this file no import.
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    from spielplan.importer import bundle as importer

    conn = None  # never called: this body exists to be parsed, like the four above.
    await importer.active_backbone_coverage(conn, settings().artifacts_dir)


def test_the_model_job_derivation_sees_a_basis_reached_through_a_helper():
    """The guard above is a derivation, and this is what keeps the derivation honest.

    `MODEL_JOBS` and the set derived from today's six agree under any rule that reads the six
    bodies, so the test above cannot fail for the reason it exists: every one of them spells
    `_active_store` in its own first lines. What it is FOR is the seventh, and the seventh is
    written by somebody who has six to copy from - so it is asserted here against jobs that do
    not exist yet, which is the only place the difference between the rules is visible.

    Three probes, because there were three ways past the substring this replaced. Two of them
    leave `MODEL_JOBS` short while the guard stays green - a fold-in started against v1 and
    committed after §10's flip then stamps `user_vector` and `ledger_fit` with a version the
    install no longer serves, which is the defect D1 exists to prevent and the one §10's
    invariant cannot see from inside the job. The third goes the other way and is the cheaper
    failure: a job forced into the skip because a comment mentioned the door.
    [M4.14 cycle 1, m414-c1-dim-lock-06]

    Five now. `probe-open` is the fourth way past, and the first one this docstring got wrong:
    it is the same silent shape as the first two, and it survived the rule that replaced the
    substring. `probe-sentinel` is the fifth and points the other way - it is what stops the
    obvious fix for the fourth, naming `ArtifactStore` itself, from re-opening `probe-comment`.
    [M4.14 cycle 2, m414-c2-dimlock-derivation-misses-artifactstore-open]

    Six. `probe-one-out` is the way past a walk bounded by one FILE, and the one the boundary's
    own paragraph promised could not happen: it is `run` itself that the assertion there guards,
    so a job whose `run` is module-level and whose acquisition is not was cleared in silence.
    [M4.14 cycle 3, m414-c3-dimlock-04]
    """
    probes = (
        worker.Job("probe-helper", "M5", "nightly", "seconds", _probe_fits_through_a_helper),
        worker.Job("probe-direct", "M5", "nightly", "seconds", _probe_loads_the_store_itself),
        worker.Job("probe-comment", "M5", "nightly", "seconds",
                   _probe_only_names_the_basis_in_a_comment),
        worker.Job("probe-open", "M5", "nightly", "seconds",
                   _probe_opens_the_active_directory_by_path),
        worker.Job("probe-sentinel", "M5", "nightly", "seconds",
                   _probe_names_the_type_without_the_door),
        worker.Job("probe-one-out", "M5", "nightly", "seconds",
                   _probe_reaches_the_basis_one_module_out),
    )
    fitting = {job.name for job in probes if _reaches_the_basis(job.run)}

    assert fitting == {"probe-helper", "probe-direct", "probe-open", "probe-one-out"}, (
        "the derivation has to answer for what a job DOES with the basis, not for what its text "
        f"says: {sorted(fitting)}"
    )


def test_every_job_that_fits_against_the_active_bundle_is_named_in_model_jobs():
    """`MODEL_JOBS` is a list kept by hand, and this is what keeps it honest.

    `_tick` skips those rows while a bundle import holds the lock, because §10's flip replaces
    the basis they fit in and a fold-in that starts against v1 and commits after the swap stamps
    `user_vector` and `ledger_fit` with a version this install no longer serves. The set is
    therefore a claim about which jobs WRITE in a basis, and the property that makes a job one of
    them is visible in its source: it acquires the basis through one of `_BASIS_NAMES`' three
    doors. Derived from that rather than restated, so the seventh model job cannot join this loop
    and silently not join the skip - which is the failure a hand-kept list produces, and it
    produces it silently.

    The other direction too: a name in the set whose job does not read the store is a job being
    held out of the loop for no reason anybody can see. [M4.14 step D1, finding 2.2]

    "Visible in its source" is `_reaches_the_basis` and not a substring of one function's text,
    for the reasons that function argues; the difference between the two rules is asserted in
    `test_the_model_job_derivation_sees_a_basis_reached_through_a_helper`, because today's six
    agree under both. [M4.14 cycle 1, m414-c1-dim-lock-06]
    """
    # `bundle-import` is excluded by name, which is what lets the derivation above cross a module
    # boundary rather than stopping one short of `importer/bundle.active_backbone_coverage`. It is
    # the one job that opens the store on purpose and the one job this skip must never take: it IS
    # §10's flip, and holding it out of the loop while the import lock is held is what that
    # docstring calls the one thing `_tick` must never do. Asserted rather than assumed, because a
    # name excluded from a set it was never in is a comment that has stopped being true.
    # [M4.14 cycle 3, m414-c3-dimlock-04]
    importer = next(j for j in worker.JOBS if j.name == worker.BUNDLE_IMPORT_JOB)
    assert _reaches_the_basis(importer.run), (
        "the exclusion below is by name because the walk derives the import job, and it no longer "
        "does: either the walk stopped crossing modules or the import stopped opening the store"
    )
    fitting = {
        job.name for job in worker.JOBS
        if job.run is not None and job.name != worker.BUNDLE_IMPORT_JOB
        and _reaches_the_basis(job.run)
    }

    assert fitting == set(worker.MODEL_JOBS), (
        "MODEL_JOBS and the jobs that acquire a basis through one of _BASIS_NAMES' doors "
        "have diverged; "
        f"only in the source: {sorted(fitting - set(worker.MODEL_JOBS))}, "
        f"only in the set: {sorted(set(worker.MODEL_JOBS) - fitting)}"
    )


def test_the_boot_line_does_not_call_a_broken_install_legal(caplog, tmp_path):
    """dd01, in the one line this process prints about its own basis.

    `is_empty` is True for a household that has never imported a bundle and for one whose active
    bundle's directory is gone, and this line said "(section 3.1: that is legal)" for both - so
    an operator whose model jobs were all about to refuse read, at every `docker compose up`,
    that the state was normal. Three cases because there are three, and the third is the one that
    must stay silent: a loaded bundle is not news.
    """
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
    assert "fired-here" not in line, "the live jobs are counted, not listed"
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
    # M4.14 moved the bundle import into this loop, so it is counted among the live and named
    # nowhere - the census lists the two categories that are NOT running here. Asserted as an
    # absence rather than deleted, because the line an operator reads would look the same if the
    # row had simply been dropped from the registry, and that is the other way this sentence
    # goes wrong. [M4.14 step E2]
    assert "bundle-import" not in line, (
        "bundle-import is live in this loop now, so the census must not list it as work that "
        f"runs elsewhere or as work awaiting a milestone: {line}"
    )
    assert line.isascii(), f"the boot line a cp1252 console has to print is not ASCII: {line!r}"


# --- M5.1 cycle 1: the size of the registry is `len(JOBS)` and no assertion message -----------

# An assertion message renders only when its assertion fails, so nothing in a green suite has ever
# read one: a count written there is prose sitting in the one place a test file cannot check it.
# The census test above substitutes `worker.JOBS` for four fabricated rows - which is the whole of
# what makes it honest, and what its docstring means by "a number somebody typed cannot survive" -
# and then spelled the real registry's size in the message four lines below, where no edit to
# `JOBS` could ever turn it red. M5.2's two §7.2 intake rows are that edit, and they arrive with
# this guard in place rather than with the word. `test_backup.py:2118-2128` records this exact
# class costing three readings of one number in one module, and decision 309's standard is the
# one that applies: a line that is true and describes the wrong thing is still the defect. Asked
# of assertion messages alone, so the dated measurement at the head of this file stays - it names
# the milestone that took the count and the day it was measured, which is the form that keeps
# being true. [M5.1 review cycle 1, M51-REV-REG-04]
_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _spellings(n: int) -> set[str]:
    """The two ways this file could write the number `n`: the digits, and the English word."""
    if n < 20:
        return {str(n), _WORDS[n]}
    tens, ones = divmod(n, 10)
    return {str(n), _TENS[tens] if not ones else f"{_TENS[tens]}-{_WORDS[ones]}"}


def _counts_in_assertion_messages(source: str, spellings: set[str]) -> list[str]:
    """Every assertion message in `source` that states one of `spellings` as a count.

    `(?!-)` is what keeps a unit out of it: "a fifteen-minute interval" is the length of a wait
    and not a tally of anything, and this file measures waits everywhere. A hyphen inside a
    spelling ("twenty-one") is still matched, because that one IS the number.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assert) or node.msg is None:
            continue
        for part in ast.walk(node.msg):
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                continue
            hit = sorted(
                word for word in spellings
                if re.search(rf"\b{re.escape(word)}\b(?!-)", part.value.lower())
            )
            if hit:
                found.append(f"line {node.lineno}: {hit} in {part.value.strip()!r}")
    return sorted(found)


def test_no_assertion_message_in_this_file_spells_the_size_of_the_registry():
    """The count is `len(JOBS)`; every other copy of it is a claim nothing reads.

    Read against today's live count rather than against every number, because this file's messages
    count nights, rows, hours and seconds and each of those is derived from the thing it describes.
    The registry's size is the one number here with no reader at all - so the guard refuses it at
    the moment somebody types it, which is the only moment it is still correct.
    """
    live = len([job for job in worker.JOBS if job.run is not None])
    counted = _counts_in_assertion_messages(
        Path(__file__).read_text(encoding="utf-8"), _spellings(live)
    )
    assert not counted, (
        f"these state the size of the registry, which is len(JOBS) = {live} today and is a "
        f"different number the day any M5 lane gives `dna-projection` a `run`: {counted}. The "
        "sentence reads the same without it."
    )


def test_the_registry_count_guard_sees_the_sentence_this_file_shipped():
    """The synthetic violation is the real one, and it is fed to the guard that runs.

    A whole-file scan is what this guard does in production, so the fixture is a whole module -
    two assertions, one of them carrying the count and one of them carrying the same word as a
    unit, because the second is what the guard has to leave alone to be usable here at all.
    """
    source = (
        'def t(line):\n'
        '    assert "fired-here" not in line, (\n'
        '        "the live jobs are counted, not listed - there are fifteen"\n'
        '    )\n'
        '    assert line, "a restart should reconcile rather than wait out a fifteen-minute gap"\n'
    )
    found = _counts_in_assertion_messages(source, _spellings(15))
    assert len(found) == 1, f"the guard reads a unit as a count, or misses the count: {found}"
    assert found[0].startswith("line 2: "), found


# --- M5.1 cycle 2: nor does the prose of either file about the registry -----------------------

# Cycle 1 asked this of assertion messages, on the argument that a message renders only when its
# assertion fails and so is the one place a count has no reader at all. The same number was
# already stale in the place with the MOST readers: `test_worker_registry.py:321` justifies its
# `DISTINCT ON` repair by calling the read proportional to a count of jobs rather than to
# history, and the count it spelled was `main`'s. Decision 347 registered `acquisition-drain` and
# made it wrong with that file green and untouched all milestone - cycle 1's guard reads
# `Path(__file__)` and walks `ast.Assert.msg`, so it could see neither that file nor a docstring.
# The file MORE about the registry than the one that was guarded was the file that was not.
# [M5.1 review cycle 2, M51-REG-JOBS-02]
#
# An EQUALITY where cycle 1 wrote a ban, because a docstring is read: a count a reader can check
# against the code under it earns its place, and what keeps it earning it is that it has to be
# today's. Two discriminations make that usable. A tally is PLURAL - "one job later in the same
# tick" (`test_worker_registry.py:526`) is English's article and this registry has never held one
# job - and a count DATED by the milestone that measured it stays, which is the form cycle 1
# protected by scoping itself and the form `:637` above is written in: the milestone tag sits in
# the same sentence as the number, so a measurement says when it was true and a claim about the
# registry a reader is looking at now does not.
#
# One limit, stated rather than papered over: a tally wrapped between two comment lines is not
# read, because `_comment_prose` yields a comment per line and a docstring whole, and gluing the
# two kinds into blocks would join a comment to the docstring under it and exempt a count from a
# citation that is not in its sentence. The shape that carries most of the prose is already read
# whole, and both shapes this was written for sit on one line.
_REGISTRY_PROSE = ("test_worker_schedule.py", "test_worker_registry.py")

_JOB_TALLY = re.compile(r"\b(\w+(?:-\w+)?)\s+(?:live\s+|registered\s+)?jobs\b", re.IGNORECASE)


def _job_tallies(path: Path) -> list[tuple[int, str, str]]:
    """Every sentence of `path`'s prose that says how many jobs there are, and how it spells it.

    Sentences rather than whole blocks, so the dated form is read the way it is written: a rule
    that asked a whole docstring for a milestone tag would exempt a bare count sitting three
    paragraphs from an unrelated citation. The line is the prose block's, which for a docstring is
    the `def` it hangs under - enough to find it, and not a second number to keep true.
    """
    found: list[tuple[int, str, str]] = []
    for line, text in _comment_prose(path):
        for sentence in re.split(r"(?<=[.;]) ", text):
            if re.search(r"\bM\d", sentence):
                continue
            for match in _JOB_TALLY.finditer(sentence):
                word = match.group(1).lower()
                if word.isdigit() or word in _COUNT_WORDS:
                    found.append((line, sentence.strip(), word))
    return found


def test_no_prose_in_either_registry_file_states_a_size_the_registry_does_not_have():
    """`len(JOBS)` is the size; a copy of it in prose is a claim with a shelf life.

    Both files rather than this one, because the class belongs to the pair: one owns `due()` and
    the other owns the loop's bookkeeping, and the sentence that went stale is in the one cycle 1
    could not reach. Read against every spelling rather than against today's, which is the half
    cycle 1 cannot do - a guard that looks only for the live count cannot see a count that was
    already wrong when it was written, and this one had been wrong since decision 347.

    Repeated back with `!a` rather than `!r`, because the sentence is somebody else's:
    these files quote spec prose and carry its em dashes, and a failure has to print on
    the cp1252 console this repository keeps its output ASCII for.
    """
    live = len([job for job in worker.JOBS if job.run is not None])
    assert live < len(_COUNT_WORDS), (
        f"this registry now holds {live} live jobs and the borrowed vocabulary spells as far as "
        f"{len(_COUNT_WORDS) - 1}: extend `_COUNT_WORDS` rather than leaving the word unread"
    )
    here = Path(__file__).resolve().parent
    stale = [
        f"{name}:{line} says {word!a} where len(JOBS) is {live}: {sentence!a}"
        for name in _REGISTRY_PROSE
        for line, sentence, word in _job_tallies(here / name)
        if word not in {str(live), _COUNT_WORDS[live]}
    ]
    assert not stale, (
        "these state a size for the registry that it does not have; the sentence reads the same "
        f"without the number, and `len(JOBS)` is the count: {stale}"
    )


def test_the_prose_tally_reader_tells_a_count_from_an_article_and_a_date(tmp_path):
    """The shapes this pair writes, fed as one docstring to the reader that runs.

    The violation is the sentence `test_worker_registry.py` shipped, kept verbatim: it is a string
    literal here and a string literal is not prose, so the guard above does not read its own
    fixture. The three it must leave alone are the singular article, a unit whose noun is not the
    registry, and the dated measurement `:637` is written in - the form cycle 1 kept deliberately.
    """
    module = tmp_path / "prose.py"
    module.write_text(
        'def t():\n'
        '    """Proportional to history rather than to the fourteen jobs it answers for.\n'
        '\n'
        '    The same arithmetic one job later, over fourteen retention nights.\n'
        '    Measured at M4.16: twenty files for the fourteen live jobs that registry then held.\n'
        '    """\n',
        encoding="utf-8",
    )

    found = _job_tallies(module)

    assert [word for _line, _sentence, word in found] == ["fourteen"], (
        f"the reader misses the count, or reads an article, a unit or a date as one: {found}"
    )


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
