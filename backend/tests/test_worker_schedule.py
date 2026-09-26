"""Daily jobs carry an `anchor_hour` and fire once per local date (§2's fourteen nights, not
uptime); sub-hour jobs keep the monotonic interval. `due` is pure: the caller supplies clocks."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
    """`trigger` is §5.3's prose and `every` what the loop obeys; they must agree."""
    by_name = {job.name: job for job in worker.JOBS}
    assert by_name["jellyfin-sessions-poll"].every == 60
    assert by_name["jellyfin-seen-sync"].every == 900        # "15 min + webhook"
    assert by_name["session-prune"].every == 3600            # "hourly"
    assert by_name["push-subscription-prune"].every == 86400  # "daily"


def test_the_tick_is_shorter_than_the_shortest_job():
    """A job can never run more often than the loop wakes."""
    live = [job.every for job in worker.JOBS if job.run is not None]
    assert min(live) > worker.TICK_SECONDS


def test_an_interval_job_that_has_never_run_is_due_immediately():
    """Only interval jobs: with no wall clock no anchored job is due, which is what stops every boot
    firing the daily jobs."""
    names = {job.name for job in worker.due(now=0.0, last_run={})}
    assert "jellyfin-seen-sync" in names
    assert names == {
        job.name for job in worker.JOBS if job.run is not None and job.anchor_hour is None
    }
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


def test_a_job_awaiting_its_milestone_is_never_due():
    """`run=None` says "not yet" out loud."""
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
    """Both fits read the coordinates the sweep writes, so `due` sorts by `stage`; the registry stays
    in §5.3's order for reading."""
    # Late enough that all three anchors have passed: an evening first boot.
    late = datetime(2026, 9, 7, 23, 0, tzinfo=CET)
    order = [j.name for j in worker.due(1e9, {}, local=late, last_date={})]
    assert order.index("placement-reconciliation") < order.index("fold-in-user-vectors")
    assert order.index("placement-reconciliation") < order.index("ledger-map-refit")

    # The table itself is still §5.3's order.
    table = [j.name for j in worker.JOBS]
    assert table.index("ledger-map-refit") < table.index("placement-reconciliation")


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


# A source path, not `inspect`: the question is where one statement sits inside `main()`.
WORKER_SOURCE = Path(worker.__file__)


def test_a_job_this_loop_does_not_fire_says_which_of_the_three_things_that_means():
    """`run=None` meant three states; pinned as sets, since a count cannot see a row changing bucket.
    `dna-projection` is reached through the drain (decision 463); `bundle-import` is live."""
    elsewhere = {j.name: j.owner for j in worker.JOBS if j.run is None and j.owner is not None}
    awaiting = {j.name: j.milestone for j in worker.JOBS if j.run is None and j.owner is None}

    assert set(elsewhere) == {"ledger-incremental", "cold-tower-placement", "dna-projection"}
    assert set(awaiting) == {"explore-frontier-cache"}
    assert sorted(awaiting.values()) == ["M6"], (
        "a job with neither an implementation nor an owner has to name the milestone that owes "
        f"it one, and these name a milestone this build has already shipped: {awaiting}"
    )
    for name, module in elsewhere.items():
        assert importlib.util.find_spec(module) is not None, (
            f"{name} names {module!r} as its implementation and that module does not exist"
        )
    # `owner` means this loop does not fire it; a live job carrying one is counted twice.
    assert not [j.name for j in worker.JOBS if j.run is not None and j.owner is not None]


# The three doors to the ACTIVE bundle. `ArtifactStore.open` is spelled with its type: `open`
# alone is every `Path.open`, and the type alone is the bundle-less `ArtifactStore.empty()`.
_BASIS_NAMES = frozenset({"_active_store", "load_active", "ArtifactStore.open"})


def _named(fn: ast.AST) -> set[str]:
    """Attribute access included; `X.y` is emitted qualified too, which carries the walk across files."""
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
    """Not only top-level imports: jobs import helpers inside their bodies."""
    out: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bare = alias.name.split(".")[0]
                out[alias.asname or bare] = (alias.name if alias.asname else bare, None)
        elif isinstance(node, ast.ImportFrom):
            # A relative import resolves to nothing, which would clear a job silently.
            assert not node.level, (
                f"{source.name} carries a relative import, which this resolution cannot follow: "
                "spell it absolutely rather than leaving a job cleared by an unresolved call"
            )
            for alias in node.names:
                out[alias.asname or alias.name] = (node.module, alias.name)
    return out


# Parsed once per file: the walk re-enters `worker.py` for every job.
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
    """The walk stops at the package: only `spielplan` can acquire this install's basis."""
    alias, _, attr = mention.partition(".")
    if attr:
        # `bb.load_for`: the alias names the module, the attribute the function.
        target = imports.get(alias)
        if target is None:
            return None
        module = target[0] if target[1] is None else f"{target[0]}.{target[1]}"
        func = attr
    else:
        # `load_hp`: the function came in by name, and its alias carries the module.
        target = imports.get(mention)
        if target is None or target[1] is None:
            return None
        module, func = target
    if module.split(".")[0] != "spielplan":
        return None
    try:
        spec = importlib.util.find_spec(module)
    except ImportError:
        # `settings` is a callable, not a module: nothing to follow.
        return None
    origin = spec.origin if spec is not None else None
    return (Path(origin), func) if origin and origin.endswith(".py") else None


def _reaches_the_basis(run) -> bool:
    """An AST walk, not a substring of one function: a helper one frame or one module out, or
    `ArtifactStore.open` directly, must count; a comment must not. `bundle-import` is dropped by
    name because the package walk derives it."""
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
    """A job that resolves the active version itself and opens that DIRECTORY with the constructor."""
    conn = None  # never called: this body exists to be parsed, like the three above.
    version = await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    ArtifactStore.open(settings().artifacts_dir / version, version)


async def _probe_names_the_type_without_the_door() -> None:
    """The type alone must not count: this probe names `ArtifactStore` and acquires no basis."""
    store: ArtifactStore = ArtifactStore.empty()
    assert store.is_empty


async def _probe_reaches_the_basis_one_module_out() -> None:
    """`run` is module-level and the acquisition one module away, in `importer/bundle`."""
    from spielplan.importer import bundle as importer

    conn = None  # never called: this body exists to be parsed, like the four above.
    await importer.active_backbone_coverage(conn, settings().artifacts_dir)


def test_the_model_job_derivation_sees_a_basis_reached_through_a_helper():
    """Today's six all spell `_active_store` themselves, so the rules differ only on jobs not yet
    written: these probes are those jobs."""
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
    """`_tick` skips these during an import. Derived from the source, so a new model job cannot join
    the loop and silently miss the skip; the reverse holds too."""
    # `bundle-import` is excluded by name: it IS §10's flip and must never be skipped.
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


def _census_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if "job(s) live" in r.getMessage()]
    assert len(lines) == 1, f"the boot census is not one line: {lines}"
    return lines[0]


def test_the_boot_census_counts_the_registry_rather_than_a_number_somebody_typed(
    monkeypatch, caplog
):
    """Four fabricated rows, one per state, so a typed number cannot survive."""
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
    """Asserted exactly: the count was never what was wrong."""
    with caplog.at_level(logging.INFO, logger="spielplan.worker"):
        worker._report_registry()

    line = _census_line(caplog)
    assert line.endswith("1 awaiting their milestone: explore-frontier-cache(M6)"), line
    outside = line.split("run outside it: ", 1)[1].split(";", 1)[0]
    assert "ledger-incremental(spielplan.ledger.refit)" in outside
    assert "cold-tower-placement(spielplan.placement.tower)" in outside
    # Reached through the drain (decision 463).
    assert "dna-projection(spielplan.dna.project)" in outside
    # Live now, so it is named nowhere; asserted as an absence, since a dropped row would look the same.
    assert "bundle-import" not in line, (
        "bundle-import is live in this loop now, so the census must not list it as work that "
        f"runs elsewhere or as work awaiting a milestone: {line}"
    )
    assert line.isascii(), f"the boot line a cp1252 console has to print is not ASCII: {line!r}"


def _census_calls_in_main(source: str) -> list[str]:
    """Three ways to lose the line: no call, a call behind a branch, a call nothing boots. `try` and
    `async with` bodies are unconditional; `if`, loops, `except` and `else` are not."""
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
        # Compound statements are descended into and then skipped, so a call is reported once.
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
    """The tests above call `_report_registry()` themselves, so only this sees the boot call site.
    Read off the source: booting `main()` needs a pool, the migration wait and signals."""
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
    """Each shape named, so the guard can see its own violation."""
    assert _census_calls_in_main(source) == expected, name
