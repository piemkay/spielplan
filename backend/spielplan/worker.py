"""The worker. Spec v2.1 §1, §5.3.

Same codebase as the backend, different entrypoint: `python -m spielplan.worker`. §5.3 lists
the jobs and their budgets; almost all of them belong to later milestones, and this module is
the registry that says so out loud rather than a process that silently does nothing.

Every job here runs on CPU. That is a hard constraint, not a preference (§1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from spielplan.core.config import Settings, settings
from spielplan.db import migrate, pool
from spielplan.models.artifacts import ArtifactStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
)
log = logging.getLogger("spielplan.worker")


@dataclass(frozen=True)
class Job:
    name: str
    milestone: str
    trigger: str
    budget: str
    # Returns whatever the job's own report already produces (`as_dict()`), which `_tick` stores
    # in `job_run.detail`. None is a legal answer: a prune has nothing to report beyond having
    # run, and §6.6's "job health" is satisfied by the row itself. [M4.7 ops-11]
    run: Callable[[], Awaitable[dict[str, object] | None]] | None = None
    # How often the trigger column actually means, in seconds. A job with no implementation
    # yet carries its interval anyway, so the registry stays a readable copy of §5.3.
    every: int = 3600
    # Execution order within one tick, low first. The registry below is in §5.3's table order,
    # which is documentation order and not an execution order: §5.3 lists the two nightly fits
    # above the placement sweep, but both fits read the coordinates the sweep writes. Left in
    # table order, the night a bundle arrives would fit every user against a library where the
    # newly-owned titles have no coordinate yet, and the sweep would correct it a day late —
    # once per import, silently, in the one direction nobody checks.
    #
    # `stage` keeps the table readable and the order right: 0 produces coordinates, 1 consumes
    # them. Equal stages keep their registry order.
    stage: int = 1
    # The hour of the household's own day (§2's `TZ`) at or after which this job may fire, once
    # per local calendar date. `None` keeps `every` as a monotonic interval.
    #
    # "Nightly" was a claim about nights that the code made about uptime: `last_run` was an
    # in-process dict and a never-run job was due immediately, so every worker start fired all
    # five `every=86400` jobs at once and spent one of §2's fourteen retention slots. Fourteen
    # restarts inside twenty-one minutes erased a fortnight of real history, and README's install
    # and upgrade gesture is `docker compose up`. `ZoneInfo` appeared nowhere in this module and
    # `settings().tz` was read by one log line.
    #
    # Only the daily jobs get an anchor. The sub-hour ones keep the monotonic interval on purpose:
    # §7.3's one-minute poll must not be moved by an NTP step or a DST transition, and "every 60
    # seconds of uptime" is exactly what it means. [M4.7 ops-03, cs-10; decision 181]
    anchor_hour: int | None = None


async def _prune_expired_sessions() -> None:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM auth_session WHERE expires_at < now()")
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s expired session(s)", gone)


async def _prune_webauthn_challenges() -> None:
    """§3.2: a WebAuthn challenge is single-use and lives five minutes. The verify path deletes
    the one it consumed; this clears the ones nobody finished."""
    from spielplan.core import webauthn

    async with pool.acquire() as conn:
        await webauthn.prune_challenges(conn)


async def _jellyfin_seen_sync() -> dict[str, object] | None:
    """§7.3's 15-minute reconciliation, and §5.3's `jellyfin-seen-sync` row."""
    from spielplan.sync import seen

    async with pool.acquire() as conn:
        report = await seen.sync_all(conn)
        if report.pushed or report.adopted or report.needs_relink:
            log.info("jellyfin seen sync: %s", report.as_dict())
        return report.as_dict()


async def _jellyfin_sessions_poll() -> dict[str, object] | None:
    """§7.3: ">= 90% playback (poll /Sessions + IsPlayed delta) arms a per-user prompt."

    A minute is the useful resolution: the window between crossing 90% and the credits ending
    is minutes long, and a prompt that arrives after the TV is off has missed its moment.
    """
    from spielplan.sync import playback

    async with pool.acquire() as conn:
        report = await playback.poll(conn)
        if report.armed:
            log.info("armed %d finish prompt(s)", report.armed)
        return report.as_dict()


# How long a `job_run` row is kept. §2 fixes the dump rotation at fourteen nights, and this is the
# same fortnight for the same reason: "last night's dump succeeded" is half of what an operator
# asks this table, and "and the thirteen before it" is the other half — a retention shorter than
# the dumps on disk would leave dumps in `/data/backups` that no row can account for. Longer buys
# nothing here: §6.6's card reads the newest row per job, and the history a chart would want is
# M5's surface with a schema of its own.
#
# It has to outlast the longest interval in `JOBS`, and it does by fourteen times. Two consequences
# make that the binding constraint rather than the fortnight: `_seed_schedule` reads the newest
# *successful* run per job, which is the memory that stops `docker compose up` spending a retention
# slot; and the newest row of a job that is still firing can never be a candidate, because the
# longest interval here is a day. Neither now rests on the arithmetic alone — `_prune_job_runs`
# exempts each job's newest successful row outright, because a daily job whose last success is
# older than a fortnight is exactly the install whose card and whose schedule need that row most.
#
# What it bounds: §5.3's three `every=60` jobs are 4,320 rows a day between them, and until this
# job existed nothing had ever deleted from the table. [M4.7 ops-11]
JOB_RUN_KEEP_DAYS = 14


async def _prune_job_runs() -> None:
    """The retention `0017_ops.sql` did not come with.

    `_tick` writes a row for every job it fires, including the three that fire every sixty
    seconds, and the three prunes beside this one clear sessions, challenges and dead push targets
    while the table this milestone added only ever grew. Both of its readers — §6.6's card and
    this module's own restart seeding — are bounded by name rather than by row count now, so this
    is about the disk and the vacuum rather than about their cost. [M4.7 ops-11]

    **A job's newest successful row is never taken, at any age.** By age alone this prune turned
    unbounded growth into a wrong answer, which is the worse of the two: §6.6's backup fact is
    the newest `ok` `nightly-backup` row, so an install whose dumps have been failing for a
    fortnight lost that row on the fifteenth night and the card stopped saying "last successful
    backup: the 24th, and it is stale" — the true, actionable sentence — and started saying that
    no dump has ever completed here. On a box still holding fourteen restorable dumps, because
    `nightly.prune` rotates only after a dump that worked. `_seed_schedule` reads the same row
    for the same reason, so the exemption is stated for every job rather than for the backup:
    one kept row per name is the whole cost, and a name this build no longer has keeps at most
    one row of history it will never be asked for.

    `IS DISTINCT FROM` rather than `<>`: a job that has never succeeded has no row to exempt, and
    `id <> NULL` is NULL, which would have exempted every row of every job that only ever failed.
    The correlated subquery is one `job_run_name_started` lookup per candidate row rather than
    the whole-table `DISTINCT ON` both readers were rewritten away from. [M4.7 cycle 2 finding 7]
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM job_run r "
            " WHERE r.started_at < now() - ($1::int * interval '1 day') "
            "   AND r.id IS DISTINCT FROM ("
            "       SELECT id FROM job_run "
            "        WHERE name = r.name AND ok ORDER BY started_at DESC LIMIT 1)",
            JOB_RUN_KEEP_DAYS,
        )
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s job run row(s) older than %d days", gone, JOB_RUN_KEEP_DAYS)


async def _prune_dead_push_subscriptions() -> None:
    """§4.2: push targets are "pruned on 404/410 from the push service".

    The 404/410 half now happens at the moment of the send (`push/send.py`), which is the only
    place those codes are observable. This is the other half: a subscription whose device never
    came back at all — no delivery, no rejection, ninety days of silence."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM push_subscription "
            "WHERE last_seen_ok IS NOT NULL AND last_seen_ok < now() - interval '90 days'"
        )


# --- M2's nightly passes ---------------------------------------------------------------------
#
# All three read the ACTIVE bundle, and all three are no-ops without one: §3.1 makes a
# bundle-less household legal, so "no basis yet" is a normal state to log once and skip, not an
# error to retry every night.


async def _active_store(conn) -> ArtifactStore | None:
    store = await ArtifactStore.load_active(conn, settings().artifacts_dir)
    return None if store.is_empty else store


async def _ledger_map_refit() -> dict[str, object] | None:
    """§5.2: the four-arm MAP fit "refit nightly (full-history MAP; seconds at this scale)".

    Measured on the development box at M2's scale: 0.24-0.39 s per (user, kind) fit over a
    900-title owned library, 1.2 s for both members across both kinds. §5.3's budget is
    "seconds".
    """
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        hp, notes = load_hp(store or ArtifactStore.empty())
        for note in notes:
            log.info("ledger hyperparameters: %s", note)
        # §5.1's basis, composed the one way the whole app must agree on. Passing the placement
        # source alone here fitted every warm title at e = 0 — see `standard_embeddings`.
        embeddings = (
            observations.standard_embeddings(conn, bb.load_for(store)) if store else None
        )
        reports = await refit.refit_all(conn, hp, embeddings=embeddings)
        for r in reports:
            log.info("ledger refit: %s", r.as_dict())
        return {"refits": [r.as_dict() for r in reports]}


async def _fold_in_user_vectors() -> dict[str, object] | None:
    """§5.3: "User fold-in + blend weights — nightly, seconds"."""
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        if store is None:
            log.info("fold-in skipped: no active bundle (§3.1)")
            return None
        report = await foldin.run(
            conn, bb.load_for(store), bundle_version=store.version,
            only_stale=False, with_priors=True,
        )
        log.info("fold-in: %s", report.as_dict())
        return report.as_dict()


async def _fold_in_tick() -> dict[str, object] | None:
    """§12's M2 exit criterion, which a strictly nightly job cannot meet.

    "50-100 verdicts each produce **visibly personal rankings**" is a claim about what a person
    sees after a sitting, and §6.0's every shelf orders by `user_score`. Only the fold-in writes
    that table: the interactive path writes `ledger_state` (tier badges move immediately) and
    nothing else. Without this tick, a household rates all evening, watches every tier badge
    change, and every shelf stays in exactly the order it had that morning — for up to 24 hours.
    `foldin.run`'s docstring named this tick as the answer; nothing called it.

    Cheap enough to run often: the fold-in is a closed-form ridge solve, measured at 6-7 ms for
    100 labels against 839 titles, and `only_stale=True` skips every user whose label count has
    not moved. `with_priors=False` because `title_prior` is a property of the bundle, not of a
    person, and re-materialising it every minute would be work with no reader.
    """
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        if store is None:
            return None
        report = await foldin.run(
            conn, bb.load_for(store), bundle_version=store.version,
            only_stale=True, with_priors=False,
        )
        if report.refit:
            log.info("fold-in tick: %s", report.as_dict())
        return report.as_dict()


async def _tier_set_refits() -> dict[str, object] | None:
    """Decision 11's "a Ledger refit is queued for that user alone", serviced.

    Not in §5.3's table, and named here rather than smuggled in — §5.3 gives the full MAP refit
    a nightly cadence, and decision 11 adds a second trigger for it. Waiting for the night
    would leave the person who just changed their tier set looking at boundaries that are
    equal-mass quantiles rather than fitted cutpoints, for up to a day, with nothing on screen
    saying so. A minute is close enough to "immediately" for a preference nobody changes twice.

    The request is cleared per (user, kind) after that fit, so a failure re-runs rather than
    being swallowed: a queue that forgets what it dropped is worse than one that retries.
    """
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.rank import tiers
    from spielplan.scoring import backbone as bb

    async with pool.acquire() as conn:
        owed = await tiers.refits_owed(conn)
        if not owed:
            return None
        store = await _active_store(conn)
        hp, _notes = load_hp(store or ArtifactStore.empty())
        embeddings = (
            observations.standard_embeddings(conn, bb.load_for(store)) if store else None
        )
        done: list[dict[str, object]] = []
        for user_id, kind in owed:
            report = await refit.refit_user(
                conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings
            )
            await tiers.clear_refit_request(conn, user_id=user_id, kind=kind)
            log.info("tier-set refit: %s", report.as_dict())
            done.append(report.as_dict())
        return {"refits": done}


async def _nightly_backup() -> dict[str, object] | None:
    """§2: "nightly `pg_dump` to `/data/backups`, rotation 14".

    Not in §5.3's table — §2 is where it is written down, and it is a job in every other sense,
    so it is registered here rather than left to a host cron the compose file never mentions.
    Nothing else in this loop touches the filesystem outside `/data/artifacts`, and nothing else
    deletes; the rotation is the reason `backup/nightly.py` will only ever unlink a file its own
    naming produced.
    """
    from spielplan.backup import nightly

    # The household's date, not the process's: `Job.anchor_hour` decided this job was due for a
    # local calendar date, and `nightly.run`'s "already dumped tonight" guard has to be asked
    # about the same one. Asked about the UTC date instead, the two disagreed for every household
    # far enough from UTC that two local dates can share one UTC day. [M4.7 ops-03]
    report = await nightly.run(_now_local())
    log.info("backup: %s", report.as_dict())
    return report.as_dict()


async def _placement_reconciliation() -> dict[str, object] | None:
    """§5.3: "any owned title lacking a coordinate gets a feature vector built from DB data per
    the feature contract … and runs §8 stages 9-10 only". Trigger: "bundle import + nightly
    sweep" — the import half lives in the importer, this is the sweep."""
    from spielplan.placement import reconcile

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        if store is None:
            log.info("placement sweep skipped: no active bundle (§3.1)")
            return None
        report = await reconcile.reconcile(conn, store, scope="owned_missing")
        if report.placed or report.failed or report.demoted:
            log.info("placement: %s", report.as_dict())
        return report.as_dict()


# §5.3's table, in order. `run=None` means the milestone that owns it has not arrived.
#
# ANCHOR_* are the household-local hours the six daily jobs fire at, spread one hour apart so a
# night is a sequence rather than one tick that serialises all six — `_tick` runs due jobs one
# after another, so six jobs in one tick is one long block during which §7.3's minute poll does
# not run. The order is the dependency order `stage` already declares (the sweep writes the
# coordinates both fits read) with the dump last, so it captures the night's work rather than the
# state before it; 01:00-06:00 is the window §2's "nightly" means on a household box. They are
# still only anchors: `stage` decides the order inside any tick that does hold more than one,
# which is what a first boot after 06:00 produces. [M4.7 ops-03, cs-10]
#
# The job-run prune takes the first hour rather than a seventh one past the dump: it is a
# millisecond DELETE with no reader waiting on it, and running it before the night's own rows are
# written keeps the fortnight it keeps to whole nights. [M4.7 ops-11]
ANCHOR_JOB_RUN_PRUNE = 1
ANCHOR_PUSH_PRUNE = 2
ANCHOR_PLACEMENT = 3
ANCHOR_LEDGER_REFIT = 4
ANCHOR_FOLD_IN = 5
ANCHOR_BACKUP = 6

JOBS: tuple[Job, ...] = (
    Job("session-prune", "M0", "hourly", "ms", _prune_expired_sessions, every=3600),
    Job("push-subscription-prune", "M0", "daily", "ms", _prune_dead_push_subscriptions,
        every=86400, anchor_hour=ANCHOR_PUSH_PRUNE),
    Job("webauthn-challenge-prune", "M1", "hourly", "ms", _prune_webauthn_challenges,
        every=3600),
    # Not in §5.3's table, and beside the three prunes rather than smuggled in at the end: §5.3
    # predates `job_run`, and a table this loop appends to on every tick is the same kind of thing
    # the three rows above keep bounded. See `JOB_RUN_KEEP_DAYS`. [M4.7 ops-11]
    Job("job-run-prune", "M0", "daily", "ms", _prune_job_runs,
        every=86400, anchor_hour=ANCHOR_JOB_RUN_PRUNE),
    Job("ledger-incremental", "M2", "every observation", "<50 ms"),
    Job("ledger-map-refit", "M2", "nightly", "seconds", _ledger_map_refit, every=86400,
        anchor_hour=ANCHOR_LEDGER_REFIT),
    Job("fold-in-user-vectors", "M2", "nightly", "seconds", _fold_in_user_vectors,
        every=86400, anchor_hour=ANCHOR_FOLD_IN),
    # Not in §5.3's table, and named here rather than smuggled in: §5.3 gives the fold-in a
    # nightly cadence, but §12's M2 exit criterion is about what a person sees *within a
    # sitting*, and every §6.0 shelf orders by a table only the fold-in writes. `foldin.run`
    # documents this tick as the answer to exactly that.
    Job("fold-in-tick", "M2", "after each sitting's writes", "ms", _fold_in_tick, every=60),
    # Decision 11's second trigger for §5.3's nightly fit. See `_tier_set_refits`.
    Job("tier-set-refit", "M3", "tier-set change", "seconds", _tier_set_refits, every=60),
    Job("cold-tower-placement", "M2", "acquisition pipeline", "<1 s/title"),
    Job("placement-reconciliation", "M2", "bundle import + nightly sweep", "seconds",
        _placement_reconciliation, every=86400, stage=0, anchor_hour=ANCHOR_PLACEMENT),
    Job("dna-projection", "M5", "acquisition", "<1 s"),
    Job("jellyfin-seen-sync", "M1", "15 min + webhook", "—", _jellyfin_seen_sync, every=900),
    Job("jellyfin-sessions-poll", "M1", "1 min", "ms", _jellyfin_sessions_poll, every=60),
    Job("explore-frontier-cache", "M6", "nightly", "minutes", every=86400),
    # §2's backup, not §5.3's table — see `_nightly_backup`. Budget from the corpus-scale
    # measurement §10 sizes: ~1.15 GB uncompressed, minutes of `pg_dump` on the reference box.
    Job("nightly-backup", "M0", "nightly", "minutes", _nightly_backup, every=86400,
        anchor_hour=ANCHOR_BACKUP),
)

# The loop wakes far more often than any job runs; `due` decides what actually fires. A single
# hourly tick would have made the 1-minute /Sessions poll a 1-hour one.
TICK_SECONDS = 20
MIGRATION_WAIT_SECONDS = 2
# 300 s, not 60. The backend is what applies the schema, and on the corpus database a single
# migration over populated tables can outlast a minute — at which point this process raised
# "is the backend running?" about a backend that was running perfectly well, and (with
# `restart: unless-stopped`) did it again every minute for ever. [M4.7 dd-deploy-worker-wait]
MIGRATION_WAIT_TRIES = 150

# How long after a failure a job may be tried again. The pre-stamp in `_tick` is what stops a
# job that fails in milliseconds from spinning at the tick rate; this is what stops a job that
# failed at 03:00 from waiting until 03:00 tomorrow. Five minutes is chosen against the failures
# that are actually transient here — a database mid-restart, a Jellyfin rebooting, a full disk
# somebody is clearing — so a household that fixes the cause during the evening gets its night's
# work without an operator having to restart the worker. [M4.7 ops-04]
RETRY_AFTER = 300

# Below this interval a job is too frequent to narrate: the three 60-second jobs would put 4320
# INFO lines a day into the log §6.6 asks an operator to read. At 900 s and above the line is the
# only evidence the job ran at all, and its duration is what 5d's measurement needs. [M4.7 ops-07]
DURATION_LOG_THRESHOLD = 900

# `docker compose ps` said "Up" for a worker whose loop was stuck, because the inherited HTTP
# healthcheck was disabled and replaced with nothing. This file is the replacement: its mtime is
# the last moment the loop went round, and the compose healthcheck reads its age. It lives under
# the cache mount because that mount already exists on the worker and holds nothing durable.
# [M4.7 ops-07]
HEARTBEAT_NAME = "worker.heartbeat"

_heartbeat_failed = False


def _local_zone() -> ZoneInfo | None:
    """§2's `TZ` as a zone, or None when this process cannot resolve the name it was given.

    Split out of `_now_local` so the fallback can be *reported* as well as taken: the two
    callers are the clock and the boot line, and only one of them can say anything. Returning
    None rather than raising keeps §3.1's rule — a typo in `TZ` must not stop the loop, because
    a job fired against the wrong clock is a better failure than a household whose sync, prompts
    and dumps all stop over a spelling. [M4.7 ops-03; cycle 2 finding 11]
    """
    try:
        return ZoneInfo(settings().tz)
    except Exception:  # noqa: BLE001 - a bad TZ must not stop the worker (§3.1)
        return None


def _now_local() -> datetime:
    """The household's wall clock, which is what "nightly" is a claim about (§2's `TZ`).

    The same shape as `api/home.py:_now_local`, including the fallback: a job anchored to an hour
    that cannot be resolved is better fired against the process's own clock than not fired at
    all. Written here rather than imported because the worker must not import an `api` module —
    `api/` decides HTTP shapes (CLAUDE.md) and importing it would pull a router into a process
    that serves nothing. `datetime.now(None)` is that fallback: the process's own naive clock.
    """
    return datetime.now(_local_zone())


def _report_starting(cfg: Settings) -> None:
    """The one line this process writes before it does anything — naming the clock it will use.

    M4.7 is what made `TZ` load-bearing: six jobs, §2's nightly dump among them, now fire at an
    hour of the household's own day rather than every 24 h of uptime. The single configuration
    typo that reschedules the whole night was invisible in exactly the log this milestone exists
    to make readable — `_now_local` swallowed an unresolvable zone at no level at all, and this
    line printed `settings().tz`, the string that was *asked* for, as though it had been
    honoured. `TZ=Europe/Berln` therefore produced a container that boots green, a log that
    agrees with the operator's spelling, and a 06:00 dump running at 08:00 local, with §6.6's
    card reporting job outcomes and nothing anywhere reporting the clock that produced them.

    A WARNING beside the boot line rather than inside `_now_local`, which the loop calls on every
    one of its 4,320 daily ticks: the substitution is a property of the configuration, so it is
    stated once, where the rest of the configuration is stated, and where an operator reading
    `docker compose logs worker` after a restart is already looking. `core/config.py` deliberately
    does not validate `TZ` — an install that cannot resolve a zone must still run (§3.1), which is
    a different rule from `PUBLIC_URL`'s. [M4.7 cycle 2 finding 11; decision 181]
    """
    zone = _local_zone()
    if zone is None:
        log.warning(
            "TZ=%s does not resolve to a time zone here, so every anchored job falls back to "
            "this process's own clock (UTC in a container): the nightly dump, both fits, the "
            "placement sweep and the two nightly prunes. Check the IANA spelling, or add a tz "
            "database to the image.",
            cfg.tz,
        )
    log.info(
        "worker starting · tz=%s · db=%s",
        zone if zone is not None else "system clock (TZ unresolved)",
        cfg.database_url.rsplit("@", 1)[-1],
    )


def _touch_heartbeat() -> None:
    """Say the loop went round. Never raise: a heartbeat is evidence, not a dependency."""
    global _heartbeat_failed

    path = settings().data_dir / "cache" / HEARTBEAT_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        _heartbeat_failed = False
    except OSError as exc:
        # Once per outage rather than once per tick: at TICK_SECONDS this would be 4320 identical
        # WARNING lines a day, which is how a real message gets lost.
        if not _heartbeat_failed:
            log.warning("cannot write the worker heartbeat at %s: %s", path, exc)
            _heartbeat_failed = True


def due(
    now: float,
    last_run: dict[str, float],
    *,
    local: datetime | None = None,
    last_date: dict[str, date] | None = None,
) -> list[Job]:
    """The jobs that may fire now. Pure, so the schedule is testable without a clock.

    Two rules, because §5.3 has two kinds of job. A job with no `anchor_hour` keeps the monotonic
    interval: "every 60 seconds of uptime" is exactly what §7.3's playback poll means, and a
    clock that jumps must not move it. A job with an `anchor_hour` fires once per **local
    calendar date**, at or after that hour — because §2's "rotation 14" is a promise about
    fourteen nights, and counting 24-hour periods of uptime instead is what let fourteen restarts
    inside twenty-one minutes erase a fortnight of dumps.

    `local` and `last_date` are the caller's, not this function's, so it stays pure: `main` reads
    the wall clock once per tick and seeds `last_date` from `job_run`. With no wall clock supplied
    no anchored job is due, which is the honest answer to "which jobs are due?" asked without one
    — and it keeps a test that is only asking about the interval half from depending on the hour
    it happens to run at.

    The `RETRY_AFTER` floor on an anchored job is not a second schedule. After a success the date
    stamp holds the job until tomorrow and the floor never binds; after a failure `_tick` clears
    the date stamp so the job can be retried *today*, and this is what keeps that retry to
    RETRY_AFTER rather than to the 20-second tick. [M4.7 ops-03, cs-10, ops-04]
    """
    dates = last_date or {}
    ready: list[Job] = []
    for job in JOBS:
        if job.run is None:
            continue
        since = now - last_run.get(job.name, float("-inf"))
        if job.anchor_hour is None:
            if since >= job.every:
                ready.append(job)
        elif (
            local is not None
            and local.hour >= job.anchor_hour
            and dates.get(job.name) != local.date()
            and since >= RETRY_AFTER
        ):
            ready.append(job)
    # Stable, so equal stages keep §5.3's table order and only the declared dependency moves.
    return sorted(ready, key=lambda j: j.stage)


async def _record_start(name: str) -> int | None:
    """Open the job's `job_run` row before it runs. Returns the row id, or None if that failed.

    §6.6 names "job health … backup status" as the System card's data and §2 promises a nightly
    dump with rotation 14, and neither could be answered by anything: `last_run` is an in-process
    dict, and `BackupReport`, `SyncReport`, `WatchReport` and the refit reports were all logged
    once and dropped. So a restart could not know whether last night's dump happened, and the
    nightly backup could fail for a month with the only signal one ERROR a day in a container log.

    Written *before* the call rather than after it, so a job killed mid-flight — a SIGKILL past
    the grace period, an OOM, a power cut — leaves a row saying it started and never finished,
    which is a different and more useful fact than no row at all.

    Bookkeeping never takes the loop down. A database that cannot accept this row is a database
    the job itself is about to fail against, and that failure is the one worth reporting.
    [M4.7 ops-11; decision 181]
    """
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO job_run (name) VALUES ($1) RETURNING id", name
            )
    except Exception:
        log.exception("could not record the start of job %s", name)
        return None


async def _record_finish(run_id: int | None, *, ok: bool, detail: dict[str, object] | None) -> None:
    """Close the row `_record_start` opened. `detail` is whatever the job's report produced."""
    if run_id is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE job_run SET finished_at = now(), ok = $2, detail = $3 WHERE id = $1",
                run_id, ok, detail,
            )
    except Exception:
        log.exception("could not record the outcome of job run %s", run_id)


async def _tick(
    now: float, local: datetime, last_run: dict[str, float], last_date: dict[str, date]
) -> None:
    _touch_heartbeat()
    loop = asyncio.get_running_loop()
    tick_started = loop.time()
    for job in due(now, last_run, local=local, last_date=last_date):
        # Stamped before the call, not after: a job that fails in milliseconds would otherwise be
        # due again on every tick, and a job that takes minutes would have its interval measured
        # from the wrong end. The `except` below is what turns this into a retry rather than a
        # full interval's silence.
        last_run[job.name] = now
        if job.anchor_hour is not None:
            last_date[job.name] = local.date()
        loud = job.every >= DURATION_LOG_THRESHOLD
        if loud:
            log.info("job %s started", job.name)
        run_id = await _record_start(job.name)
        started = loop.time()
        try:
            detail = await job.run()
        except Exception as exc:
            # One failing job must not take the loop down, and it must not be exiled for a whole
            # interval either: the stamp above said "it just ran", and for a nightly job that
            # meant the next attempt was tomorrow. The comment here used to claim "the next tick
            # retries it", which was true only for the jobs whose interval is shorter than a tick
            # — that is, none of them. Both branches bring the next attempt to RETRY_AFTER after
            # the failure: an interval job by rewinding its stamp, an anchored one by forgetting
            # that today has been spent while the stamp holds the floor.
            #
            # **After the failure, not when the tick opened.** `now` is read once per tick, before
            # any job runs, so measuring from it bounded the gap from an instant the failure may
            # be minutes past, and any failure that itself outlasts RETRY_AFTER was re-armed on
            # the next 20-second tick with no back-off at all. This milestone made that concrete
            # rather than theoretical: `nightly.DUMP_TIMEOUT_SECONDS` is 1800 s against a
            # RETRY_AFTER of 300, so a `pg_dump` blocked behind an import's lock times out at
            # 06:30 and is immediately due again — 36 back-to-back attempts before midnight,
            # during which this sequential loop never reaches §7.3's poll, the fold-in tick or
            # `_touch_heartbeat`.
            #
            # Measured from the top of the *tick* rather than from this job's own `started`,
            # because the loop is sequential and the failing job is rarely alone in it: a first
            # boot after 06:00 — a fresh box, or README's Recovery path — finds all six anchored
            # jobs due in one tick with the dump last by design (see ANCHOR_BACKUP), so a dump
            # that fails after the sweep and the two fits would otherwise be stamped as if it had
            # failed when the tick opened, and re-armed exactly as above. Adding the tick's
            # elapsed time to `now` keeps the arithmetic in the caller's clock frame, which is the
            # frame `due` compares against; reading `loop.time()` outright would mix two.
            #
            # `min` because RETRY_AFTER is a ceiling on the wait and never a floor: §7.3's poll
            # runs every 60 s because a finish prompt that arrives five minutes late has missed
            # the credits, and a Jellyfin that returned one error must not cost that window.
            # [M4.7 ops-04; cycle 2 finding 4, cycle 3 finding 9]
            log.exception("job %s failed", job.name)
            failed_at = now + (loop.time() - tick_started)
            if job.anchor_hour is None:
                last_run[job.name] = failed_at - job.every + min(job.every, RETRY_AFTER)
            else:
                last_run[job.name] = failed_at
                last_date.pop(job.name, None)
            await _record_finish(
                run_id, ok=False, detail={"error": f"{type(exc).__name__}: {exc}"}
            )
        else:
            await _record_finish(run_id, ok=True, detail=detail)
            if loud:
                log.info("job %s done in %.1fs", job.name, loop.time() - started)


async def _seed_schedule(
    conn, *, loop_now: float, local: datetime
) -> tuple[dict[str, float], dict[str, date]]:
    """Give the loop the memory it never had, from `job_run`.

    Without this the whole of the anchoring above is defeated by a restart: `last_date` would be
    empty, every anchored job would find today unspent, and `docker compose up` — README's own
    install and upgrade gesture — would spend a retention slot every time. Seeding from the
    newest **successful** run per job is what makes a restart neither spend a night nor skip one:
    a job that has genuinely never run is still due immediately (that is a first boot, and a first
    boot should back up), and a job that ran an hour ago is not run again.

    Both dictionaries come from the same query because they are two readings of the same fact.
    `last_run` is monotonic loop time, so the row's age is subtracted from the loop's clock rather
    than mixed with it; `max(0.0, …)` because a clock that has been stepped backwards must not
    park a job in the future.

    Asked one job at a time rather than as `DISTINCT ON (name)` over the table. This runs at every
    worker start — `docker compose up` is README's install *and* upgrade gesture — and Postgres 16
    answers a bare DISTINCT ON with a full scan of `job_run_name_started`, so the cost of starting
    the worker grew with every row the loop had ever written: measured at 2.0 ms over one day of
    rows and 7.1 s over a year of them. Driven by the registry's own names it is one index lookup
    each, 0.7 ms at every size. A job whose name is no longer in `JOBS` is not asked about, which
    is also the right answer — the loop has no schedule to seed for it. [M4.7 ops-03, cs-10, ops-11]
    """
    rows = await conn.fetch(
        "SELECT j.name, r.started_at "
        "  FROM unnest($1::text[]) AS j(name) "
        "  JOIN LATERAL ("
        "       SELECT started_at FROM job_run "
        "        WHERE name = j.name AND ok ORDER BY started_at DESC LIMIT 1"
        "  ) r ON true",
        [job.name for job in JOBS if job.run is not None],
    )
    now_utc = datetime.now(UTC)
    last_run: dict[str, float] = {}
    last_date: dict[str, date] = {}
    for row in rows:
        age = max(0.0, (now_utc - row["started_at"]).total_seconds())
        last_run[row["name"]] = loop_now - age
        # `astimezone(None)` is the system zone, which is exactly what `_now_local`'s bad-TZ
        # fallback returns — so the two sides of the comparison agree in both branches.
        last_date[row["name"]] = row["started_at"].astimezone(local.tzinfo).date()
    if last_run:
        log.info("schedule seeded from %d previous job run(s)", len(last_run))
    return last_run, last_date


async def main() -> None:
    cfg = settings()
    _report_starting(cfg)

    # Inside the `try`, so the `finally` closes it. Opened outside, a `raise` from the migration
    # wait below left the pool open and the process dead — and `restart: unless-stopped` then
    # opened another one every few minutes. [M4.7 dd-deploy-worker-wait]
    try:
        await pool.open_pool(cfg.database_url, max_size=4)

        # The backend owns the schema. The worker waits for it and never applies: during a rolling
        # restart the two are briefly on different images, and two appliers means whichever loses
        # the race has its own migration rejected by the other's checksum.
        async with pool.acquire() as conn:
            missing: list[str] = []
            for _ in range(MIGRATION_WAIT_TRIES):
                missing = await migrate.pending(conn)
                if not missing:
                    break
                log.info(
                    "waiting for the backend to apply %d migration(s): %s",
                    len(missing), ", ".join(missing),
                )
                await asyncio.sleep(MIGRATION_WAIT_SECONDS)
            else:
                # "is the backend running?" was the wrong question in both of the cases that
                # actually produce this: a migration the backend is crash-looping on, and a
                # migration over populated tables that legitimately takes longer than the wait.
                # Name what is missing and where the answer is instead.
                raise RuntimeError(
                    "the database is still behind this build after "
                    f"{MIGRATION_WAIT_TRIES * MIGRATION_WAIT_SECONDS}s. Still pending: "
                    f"{', '.join(missing)}. The backend applies migrations and this process "
                    "never does, so read its log first: if it shows a migration error, fix that. "
                    "Nothing here can make progress until it lands."
                )
            store = await ArtifactStore.load_active(conn, cfg.artifacts_dir)
            local = _now_local()
            last_run, last_date = await _seed_schedule(
                conn, loop_now=asyncio.get_running_loop().time(), local=local
            )

        if store.is_empty:
            log.info("no artifact bundle active — model jobs stay idle (§3.1: that is legal)")

        pending = [j for j in JOBS if j.run is None]
        log.info(
            "%d job(s) live, %d awaiting their milestone: %s",
            len(JOBS) - len(pending),
            len(pending),
            ", ".join(f"{j.name}({j.milestone})" for j in pending),
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: stop.set())

        while not stop.is_set():
            # The wall clock is read once per tick and passed down, so every anchored job in one
            # tick is judged against the same instant and `due` stays pure.
            await _tick(loop.time(), _now_local(), last_run, last_date)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    finally:
        await pool.close_pool()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
