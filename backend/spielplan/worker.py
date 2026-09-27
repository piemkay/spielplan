"""The worker (§1, §5.3): `python -m spielplan.worker`, same codebase as the backend. The registry
below is §5.3's table; every job runs on CPU (§1).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from spielplan.core import logs, storage
from spielplan.core.config import Settings, settings
from spielplan.db import migrate, pool
from spielplan.models import artifacts
from spielplan.models.artifacts import ArtifactStore

logs.configure()
log = logging.getLogger("spielplan.worker")


@dataclass(frozen=True)
class Job:
    name: str
    milestone: str
    trigger: str
    budget: str
    # The job's report (`as_dict()`), stored in `job_run.detail`; None is legal.
    run: Callable[[], Awaitable[dict[str, object] | None]] | None = None
    # The module implementing a row this loop does not fire (a request triggers it, or another job
    # reaches it). No `run` and no `owner` means unwritten.
    owner: str | None = None
    # Seconds; carried even without an implementation, so the registry reads as §5.3.
    every: int = 3600
    # Order within one tick, low first: 0 produces coordinates, 1 consumes them.
    stage: int = 1
    # The household-local hour (§2's `TZ`) at or after which a daily job fires, once per local date;
    # None keeps `every` as a monotonic interval. Restarts must not spend §2's fourteen dump slots.
    anchor_hour: int | None = None
    # §5.3's budget in seconds, enforced by `_tick`: one job that never returns would stall the
    # sequential loop for good. The numbers sit beside the intervals at the foot of this file.
    timeout: float = 300.0


async def _prune_expired_sessions() -> None:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM auth_session WHERE expires_at < now()")
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s expired session(s)", gone)


async def _prune_webauthn_challenges() -> None:
    """§3.2: clears the challenges nobody finished; the verify path deletes consumed ones."""
    from spielplan.core import webauthn

    async with pool.acquire() as conn:
        await webauthn.prune_challenges(conn)


# The Played-write refusals last logged at ERROR: repeat only when the set of reasons changes.
_push_failure_reported: frozenset[str] | None = None


async def _jellyfin_seen_sync() -> dict[str, object] | None:
    """§7.3's 15-minute reconciliation. ERROR on failed Played writes (once per set of reasons), and
    INFO whenever anything moved or failed, since a silent sweep looks healthy."""
    global _push_failure_reported

    from spielplan.sync import seen

    async with pool.acquire() as conn:
        report = await seen.sync_all(conn)
        reasons = frozenset(report.push_errors) if report.push_failed else None
        if report.push_failed:
            log.log(
                logging.ERROR if reasons != _push_failure_reported else logging.DEBUG,
                "jellyfin seen sync: %d Played write(s) failed and the app->Jellyfin direction "
                "is incomplete: %s",
                report.push_failed,
                "; ".join(report.push_errors) or "no reason recorded",
            )
        _push_failure_reported = reasons
        if (
            report.pushed
            or report.adopted
            or report.needs_relink
            or report.push_failed
            or report.owed_no_token
            or report.unowned
            # Otherwise a member whose whole sweep raised moves no counter and nothing names them.
            or report.failed_users
        ):
            log.info("jellyfin seen sync: %s", report.as_dict())
        return report.as_dict()


# The unresolvable session items last logged at INFO: `/Sessions` re-reports them every minute.
_last_unresolved: frozenset[str] | None = None

# Likewise for episodes whose series `/Shows/{id}/Episodes` would not list (decision 210(c)).
_last_undecided: frozenset[str] | None = None


async def _jellyfin_sessions_poll() -> dict[str, object] | None:
    """§7.3's >= 90% playback poll. A minute: a prompt after the credits has missed its moment."""
    global _last_unresolved, _last_undecided

    from spielplan.sync import playback

    async with pool.acquire() as conn:
        report = await playback.poll(conn)
        if report.armed:
            log.info("armed %d finish prompt(s)", report.armed)
        unresolved = frozenset(report.unresolved)
        if report.unresolved:
            # A session no title matched (§7.1's identity rules); INFO only when the set changes.
            log.log(
                logging.INFO if unresolved != _last_unresolved else logging.DEBUG,
                "playback poll: %d session(s) matched no title: %s",
                len(report.unresolved), ", ".join(report.unresolved[:10]),
            )
        # Cleared when empty, so a returning stranger is news again.
        _last_unresolved = unresolved or None

        undecided = frozenset(report.undecided)
        if report.undecided:
            # Placed titles whose series could not be listed arm nothing (decision 210(c)); say so.
            log.log(
                logging.INFO if undecided != _last_undecided else logging.DEBUG,
                "playback poll: %d television session(s) whose series could not be listed: %s",
                len(report.undecided), ", ".join(report.undecided[:10]),
            )
        _last_undecided = undecided or None
        return report.as_dict()


# §2's fortnight: rows must cover every dump on disk. Four `every=60` jobs write 5,760 rows a day.
JOB_RUN_KEEP_DAYS = 14


async def _prune_job_runs() -> None:
    """The retention `0017_ops.sql` lacked. Never takes a job's newest successful row, nor its newest row
    that reached a server: §6.6's backup and last-sync facts read those at any age. `IS DISTINCT
    FROM`, because `id <> NULL` would exempt every row of a job that never succeeded."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM job_run r "
            " WHERE r.started_at < now() - ($1::int * interval '1 day') "
            "   AND r.id IS DISTINCT FROM ("
            "       SELECT id FROM job_run "
            "        WHERE name = r.name AND ok ORDER BY started_at DESC LIMIT 1)"
            "   AND r.id NOT IN ("
            "       SELECT s.id FROM unnest($2::text[]) AS j(name) JOIN LATERAL ("
            "              SELECT id FROM job_run WHERE name = j.name AND ok"
            "                 AND detail IS NOT NULL"
            "                 AND coalesce((detail->>'reached')::boolean, true)"
            "               ORDER BY started_at DESC LIMIT 1) s ON true)",
            JOB_RUN_KEEP_DAYS,
            [job.name for job in JOBS],
        )
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s job run row(s) older than %d days", gone, JOB_RUN_KEEP_DAYS)


async def _prune_dead_push_subscriptions() -> None:
    """§4.2: the 404/410 half happens at send time; this takes devices silent for ninety days, judged on
    `COALESCE(last_seen_ok, created_at)` (a re-subscribe refreshes `created_at`)."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM push_subscription "
            "WHERE COALESCE(last_seen_ok, created_at) < now() - interval '90 days'"
        )
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s push subscription(s) silent for 90 days", gone)


# M2's nightly passes read the ACTIVE bundle and are no-ops without one (§3.1).


async def _active_store(conn) -> ArtifactStore | None:
    """The basis every model job fits against. `assert_not_broken` first: a broken store carries the
    active version, so the §10 comparison passes it. Both raise, so `_tick` records a refusal rather
    than a fit in a zero basis; bundle-less stays None (§3.1)."""
    store = await ArtifactStore.load_active(conn, settings().artifacts_dir)
    store.assert_not_broken()
    store.assert_matches(await artifacts.active_bundle_version(conn))
    return None if store.is_empty else store


async def _ledger_map_refit() -> dict[str, object] | None:
    """§5.2's full-history MAP refit, nightly; ~0.3 s per (user, kind) at M2 scale."""
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        hp, notes = load_hp(store or ArtifactStore.empty())
        for note in notes:
            log.info("ledger hyperparameters: %s", note)
        # §5.1's basis via `standard_embeddings`. `store.version` None on a bundle-less install is the
        # honest stamp: a zero basis belongs to no bundle.
        embeddings = (
            observations.standard_embeddings(
                conn, bb.load_for(store), bundle_version=store.version
            )
            if store
            else None
        )
        reports = await refit.refit_all(
            conn, hp, embeddings=embeddings,
            bundle_version=store.version if store else None,
        )
        for r in reports:
            log.info("ledger refit: %s", r.as_dict())
        return {"refits": [r.as_dict() for r in reports]}


async def _ledger_refresh_tick() -> dict[str, object] | None:
    """§5.2's full refit, asked for inside a sitting: the incremental path moves only touched titles, so
    unrated ones kept the first fit's order. Debounced by work (`refit.refreshes_owed`). A refusing
    board is retried every minute; bounding that needs a column."""
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    async with pool.acquire() as conn:
        owed = await refit.refreshes_owed(conn)
        if not owed:
            return None
        store = await _active_store(conn)
        hp, _notes = load_hp(store or ArtifactStore.empty())
        # The bundle that built the coordinates stamps the fit; None when bundle-less.
        embeddings = (
            observations.standard_embeddings(
                conn, bb.load_for(store), bundle_version=store.version
            )
            if store
            else None
        )
        bundle_version = store.version if store else None
        done: list[dict[str, object]] = []
        for user_id, kind, grown in owed:
            try:
                report = await refit.refit_user(
                    conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings,
                    bundle_version=bundle_version,
                )
            except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError):
                # A lost connection fails every item alike: raise so `_tick` records the failure.
                raise
            except Exception as exc:
                log.exception("ledger refresh failed for user %s/%s", user_id, kind)
                done.append(
                    refit.RefitReport(user_id=user_id, kind=kind, error=str(exc)).as_dict()
                )
            else:
                done.append({**report.as_dict(), "grown": grown})
        log.info("ledger refresh: %s", done)
        return {"refits": done}


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
    """§12's M2 exit criterion needs the shelves (`user_score`) to move within a sitting, and only the
    fold-in writes it. The partition rewrite, not the ridge solve, is the cost, so it is debounced
    (`foldin.PAUSE_SECONDS`)."""
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
    """Decision 11's tier-set refit, within a minute. Per item, and cleared per item even on failure:
    a fit failing on its own data would fail again, and the nightly pass covers it. The clear's
    stamp spares a newer request."""
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
        # The bundle that built the coordinates stamps the fit; None when bundle-less.
        embeddings = (
            observations.standard_embeddings(
                conn, bb.load_for(store), bundle_version=store.version
            )
            if store
            else None
        )
        bundle_version = store.version if store else None
        done: list[dict[str, object]] = []
        for user_id, kind, requested_at in owed:
            try:
                report = await refit.refit_user(
                    conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings,
                    bundle_version=bundle_version,
                )
            except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError):
                # A lost connection fails every remaining row alike: raise rather than clear them all and
                # report success.
                raise
            except Exception as exc:
                log.exception("tier-set refit failed for user %s/%s", user_id, kind)
                done.append(
                    refit.RefitReport(user_id=user_id, kind=kind, error=str(exc)).as_dict()
                )
            else:
                log.info("tier-set refit: %s", report.as_dict())
                done.append(report.as_dict())
            await tiers.clear_refit_request(
                conn, user_id=user_id, kind=kind, requested_at=requested_at
            )
        return {"refits": done}


async def _nightly_backup() -> dict[str, object] | None:
    """§2's nightly dump, rotation 14. Registered here rather than left to a host cron."""
    from spielplan.backup import nightly

    # The household's date, the one `anchor_hour` judged due, so `nightly.run`'s guard agrees.
    report = await nightly.run(_now_local())
    log.info("backup: %s", report.as_dict())
    return report.as_dict()


async def _storage_check() -> dict[str, object] | None:
    """Whether this loop can write its five mounts, named once with the chown that fixes them. Raises,
    so `job_run.ok` is false and RETRY_AFTER re-asks until it is fixed."""
    result = await asyncio.to_thread(storage.probe, settings().data_dir, storage.WORKER_MOUNTS)
    problem = storage.refusal(result)
    if problem is not None:
        raise RuntimeError(problem)
    return {"writable": " ".join(storage.WORKER_MOUNTS)}


async def _placement_reconciliation() -> dict[str, object] | None:
    """§5.3's nightly sweep: owned titles lacking a coordinate go through §8 stages 9-10."""
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


async def _acquisition_drain() -> dict[str, object] | None:
    """§8's pipeline, driven. Reclaim before counting (a killed worker's tasks are leased, not pending);
    with work due, the basis is asked once per batch, so a broken install fails the job rather than
    spending every task's attempts. Bundle-less still walks stages 1-8 (§3.1)."""
    from spielplan.acquire import pipeline, queue

    async with pool.acquire() as conn:
        reclaimed = await queue.reclaim_expired(conn)
        await pipeline.close_abandoned_boards(conn)
        await pipeline.complete_landed_boards(conn)
        due = await queue.pending_count(conn, [pipeline.TASK_KIND])
        store = await _active_store(conn) if due else None
        report = await pipeline.drain(conn, limit=pipeline.DRAIN_LIMIT, run_id=_JOB_RUN.get())
        if not report.leased and not any(reclaimed.values()):
            return None
        detail = report.as_dict()
        detail["reclaimed"] = {
            state: count + report.reclaimed.get(state, 0) for state, count in reclaimed.items()
        }
        detail["basis"] = store.version if store is not None else None
        return detail


async def _metadata_backfill() -> dict[str, object] | None:
    """Decision 522: TMDB metadata for bundle titles the corpus never fetched, in this sequential loop
    so `api.themoviedb.org` has one bucket."""
    from spielplan.acquire import backfill

    async with pool.acquire() as conn:
        return await backfill.walk(conn, run_id=_JOB_RUN.get())


async def _art_lookup() -> dict[str, object] | None:
    """Decision 484: TMDB poster lookups, sharing the drain's one TMDB bucket. None when there is no
    key, nothing owed, or egress is off (decision 483)."""
    from spielplan.art import lookup

    async with pool.acquire() as conn:
        return await lookup.drain(conn)


async def _jellyfin_delta_poll() -> dict[str, object] | None:
    """§7.2's 15-minute fallback intake (decision 409). No configured, readable connector is not a
    failure; a failed read raises, since `poll_delta` never advances the watermark past it. The
    config is re-read every poll: the watermark and library pick change underneath it."""
    from spielplan.acquire import intake
    from spielplan.connectors import registry

    async with pool.acquire() as conn:
        cfg = await registry.load_jellyfin(conn)
        client = registry.make_client(cfg)
        if client is None:
            return None
        return (await intake.poll_delta(conn, client, cfg)).as_dict()


async def _jellyfin_intake_sweep() -> dict[str, object] | None:
    """§7.2's 10-minute debounce, driven: ripe intake rows become tasks. Outages do not raise;
    `sweep_pending` leaves undecided keys pending. None when nothing was ripe."""
    from spielplan.acquire import intake
    from spielplan.connectors import registry

    async with pool.acquire() as conn:
        cfg = await registry.load_jellyfin(conn)
        client = registry.make_client(cfg)
        if client is None:
            return None
        report = await intake.sweep_pending(conn, client, cfg)
        return report.as_dict() if report.ripe else None


# §5.3's ninth row. Literal names shared with `api/artifacts.py`, which imports FastAPI;
# `test_bundle_import_job.py` keeps them equal.
BUNDLE_IMPORT_JOB = "bundle-import"

# `job_run.detail->>'phase'`, the whole state machine: queued (route), running (claim), then
# active or failed. In `job_run`, not `artifact_bundle` (decision 253).
PHASE_QUEUED = "queued"
PHASE_RUNNING = "running"
PHASE_ACTIVE = "active"
PHASE_FAILED = "failed"

# Seconds: one attempt's ceiling, which is also the age at which a claim counts as abandoned.
BUNDLE_IMPORT_TIMEOUT = 600.0


async def _claim_bundle_import(conn) -> asyncpg.Record | None:
    """Claim the oldest queued import exactly once (`FOR UPDATE SKIP LOCKED`). `claimed_at` goes in
    `detail`: every budget is measured from it, while `started_at` stays the press of Import."""
    return await conn.fetchrow(
        "UPDATE job_run SET detail = jsonb_set("
        "     jsonb_set(detail, '{phase}', to_jsonb($3::text)), '{claimed_at}', to_jsonb(now())"
        " ) WHERE id = ("
        "     SELECT id FROM job_run "
        "      WHERE name = $1 AND finished_at IS NULL AND detail->>'phase' = $2 "
        "      ORDER BY started_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        " ) RETURNING id, started_at, detail",
        BUNDLE_IMPORT_JOB, PHASE_QUEUED, PHASE_RUNNING,
    )


# Both terminal writers check for a committed flip first, so a lost report never marks a
# committed import failed. No restart is owed: the backend follows the active row (decision 497).
_COMMITTED_UNREPORTED = (
    "this import committed and {version} is the active bundle - the process was stopped before "
    "it could report. Nothing needs importing again, and the backend loads it without a restart."
)


async def _committed_import(conn, version: str | None, since) -> object | None:
    """An ACTIVE row for this version activated after `since`, so an earlier import of it does not count."""
    if not version:
        return None
    return await conn.fetchval(
        "SELECT activated_at FROM artifact_bundle "
        " WHERE version = $1 AND state = 'active' AND activated_at >= $2",
        version, since,
    )


async def _finish_bundle_import(job_id: int, detail: dict, report, *, ok: bool) -> None:
    """One writer for every exit, so the Data tab reads one shape. For a refusal this is the only
    surviving copy of the report (§10)."""
    closed = {
        **detail,
        "phase": PHASE_ACTIVE if ok else PHASE_FAILED,
        "report": report.as_dict(),
        "text": report.render(),
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE job_run SET finished_at = now(), ok = $2, detail = $3 WHERE id = $1",
            job_id, ok, closed,
        )


async def _reap_abandoned_import(conn) -> None:
    """Close claims nothing will finish (cancelled at the budget, or killed): judged on `claimed_at`,
    and reporting a committed flip as such rather than as a failure."""
    from spielplan.importer.report import ImportReport

    stale = await conn.fetch(
        "SELECT id, started_at, detail FROM job_run "
        " WHERE name = $1 AND finished_at IS NULL AND detail->>'phase' = $2 "
        "   AND coalesce((detail->>'claimed_at')::timestamptz, started_at) "
        "       < now() - ($3::float8 * interval '1 second')",
        BUNDLE_IMPORT_JOB, PHASE_RUNNING, BUNDLE_IMPORT_TIMEOUT,
    )
    for row in stale:
        detail = dict(row["detail"] or {})
        version = detail.get("bundle_version")
        report = ImportReport(bundle_version=version)
        flipped = await _committed_import(conn, version, row["started_at"])
        if flipped:
            # Committed; only the report was lost.
            report.note("import", _COMMITTED_UNREPORTED.format(version=version))
            log.warning("bundle import: job %s committed and was never reported", row["id"])
            await _finish_bundle_import(row["id"], detail, report, ok=True)
            continue
        report.fail(
            "import",
            f"no process reported the outcome of this import within {BUNDLE_IMPORT_TIMEOUT:g}s "
            "of claiming it: the worker was stopped, killed or abandoned it at its budget, and "
            "this install carries no active bundle at that version. Read "
            "/api/admin/bundle/state and the bundle's own row before importing it again.",
        )
        log.warning("bundle import: job %s was abandoned and is recorded as failed", row["id"])
        await _finish_bundle_import(row["id"], detail, report, ok=False)


async def _bundle_import() -> dict[str, object] | None:
    """§5.3's bundle import, off the web process, where minutes of work would stall its loop past every
    health and proxy timeout. The import gets its own connection so phase writes stay visible;
    `IMPORT_LOCK` is the only serialisation."""
    from spielplan.importer import bundle as bundle_import
    from spielplan.importer.report import ImportReport

    async with pool.acquire() as conn:
        claimed = await _claim_bundle_import(conn)
        if claimed is None:
            await _reap_abandoned_import(conn)
            return None

    job_id = claimed["id"]
    claimed_from = claimed["started_at"]
    detail = dict(claimed["detail"] or {})
    path = Path(str(detail.get("path") or ""))
    version = detail.get("bundle_version")
    log.info("bundle import: claimed job %s - %s at %s", job_id, version, path)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        bundle = bundle_import.Bundle.open(path)
        # The tree may have changed since validation: refuse a different bundle.
        if version and bundle.version != version:
            raise RuntimeError(
                f"{path} is bundle {bundle.version!r} now and was {version!r} when it was "
                "validated, so nothing was imported"
            )
        async with pool.acquire() as work:
            report = await bundle_import.import_bundle(
                work, bundle, settings().artifacts_dir
            )
    except Exception as exc:
        # Record a report for every failure and re-raise for `_tick`. Check for a committed flip first;
        # if that check fails too, the row stays `running` for the reaper.
        crash = ImportReport(bundle_version=version)
        flipped = None
        with contextlib.suppress(Exception):
            async with pool.acquire() as conn:
                flipped = await _committed_import(conn, version, claimed_from)
        if flipped:
            crash.note("import", _COMMITTED_UNREPORTED.format(version=version))
            crash.warn(
                "import",
                f"the process stopped after the flip on {type(exc).__name__}: {exc}",
            )
            log.warning("bundle import: job %s committed and then raised: %s", job_id, exc)
            await _finish_bundle_import(job_id, detail, crash, ok=True)
            raise
        crash.fail(
            "import",
            f"the import did not run to a report: {type(exc).__name__}: {exc}",
        )
        await _finish_bundle_import(job_id, detail, crash, ok=False)
        raise

    await _finish_bundle_import(job_id, detail, report, ok=report.ok)
    log.info(
        "bundle import: job %s %s in %.1fs",
        job_id, "imported" if report.ok else "refused", loop.time() - started,
    )
    return {"job_id": job_id, "bundle_version": bundle.version, "ok": report.ok}


# §5.3's table. `run=None`: not fired by this loop. The daily jobs are anchored one hour apart
# (01:00-06:00), in dependency order with the dump last.
ANCHOR_JOB_RUN_PRUNE = 1
ANCHOR_PUSH_PRUNE = 2
ANCHOR_PLACEMENT = 3
ANCHOR_LEDGER_REFIT = 4
ANCHOR_FOLD_IN = 5
ANCHOR_BACKUP = 6

# Every `timeout` is at or under its job's interval, since the loop is sequential; the 60-second
# jobs get 55 s. `nightly-backup` is the exception: it must exceed `nightly.DUMP_TIMEOUT_SECONDS`
# (1800 s) so pg_dump's own timeout fires first.

JOBS: tuple[Job, ...] = (
    Job("session-prune", "M0", "hourly", "ms", _prune_expired_sessions, every=3600,
        timeout=60),
    Job("push-subscription-prune", "M0", "daily", "ms", _prune_dead_push_subscriptions,
        every=86400, anchor_hour=ANCHOR_PUSH_PRUNE, timeout=60),
    Job("webauthn-challenge-prune", "M1", "hourly", "ms", _prune_webauthn_challenges,
        every=3600, timeout=60),
    # Not in §5.3's table: keeps `job_run` bounded (see `JOB_RUN_KEEP_DAYS`).
    Job("job-run-prune", "M0", "daily", "ms", _prune_job_runs,
        every=86400, anchor_hour=ANCHOR_JOB_RUN_PRUNE, timeout=60),
    Job("ledger-incremental", "M2", "every observation", "<50 ms",
        owner="spielplan.ledger.refit"),
    Job("ledger-map-refit", "M2", "nightly", "seconds", _ledger_map_refit, every=86400,
        anchor_hour=ANCHOR_LEDGER_REFIT, timeout=900),
    # Not in §5.3's table: re-runs the full fit as observations accrue (`_ledger_refresh_tick`).
    Job("ledger-refresh", "M2", "5+ observations since the last full fit", "seconds",
        _ledger_refresh_tick, every=60, timeout=55),
    Job("fold-in-user-vectors", "M2", "nightly", "seconds", _fold_in_user_vectors,
        every=86400, anchor_hour=ANCHOR_FOLD_IN, timeout=600),
    # Not in §5.3's table: the shelves must move within a sitting (§12 M2). The budget names both costs.
    Job("fold-in-tick", "M2", "after each sitting's writes", "ms of numpy + s of partition writes",
        _fold_in_tick, every=60, timeout=55),
    # Decision 11's second trigger for §5.3's nightly fit. See `_tier_set_refits`.
    Job("tier-set-refit", "M3", "tier-set change", "seconds", _tier_set_refits, every=60,
        timeout=55),
    Job("cold-tower-placement", "M2", "acquisition pipeline", "<1 s/title",
        owner="spielplan.placement.tower"),
    Job("placement-reconciliation", "M2", "bundle import + nightly sweep", "seconds",
        _placement_reconciliation, every=86400, stage=0, anchor_hour=ANCHOR_PLACEMENT,
        timeout=1800),
    # Reached through the drain's stage 8 (decision 463).
    Job("dna-projection", "M5", "acquisition", "<1 s", owner="spielplan.dna.project"),
    # Not in §5.3's table: it fires the rows above. 420 s must stay under `queue.LEASE_SECONDS` (900),
    # or a reaped task gets a second walker (`test_acquire_drain.py` pins it), and above the 300 s
    # Retry-After sleep, or one 429 cancels the batch. A measured batch is ~69 s of pacing.
    Job("acquisition-drain", "M5.1", "queue",
        "~9 s/title of paced crawl x 8 a tick + <1 s/title placed",
        _acquisition_drain, every=1800, timeout=420),
    # Decision 522, before the lookup so a title it gives a poster is not filed again.
    Job("metadata-backfill", "M5", "queue", "~11 s of paced TMDB + a derive/title x 100 a tick",
        _metadata_backfill, every=1800, timeout=300),
    # Decision 484; 300 lookups at 18 rps is under 20 s of pacing.
    Job("art-lookup", "M5", "queue", "~17 s of paced TMDB lookups x 300 a tick",
        _art_lookup, every=1800, timeout=300),
    Job("jellyfin-seen-sync", "M1", "15 min + webhook", "—", _jellyfin_seen_sync, every=900,
        timeout=600),
    Job("jellyfin-sessions-poll", "M1", "1 min", "ms", _jellyfin_sessions_poll, every=60,
        timeout=55),
    # §7.2's two intake paths into the drain's queue (decision 368): 10-15 minutes from an add to a
    # filed task. The poll may take a third of its interval: an abandoned poll repeats for free.
    Job("jellyfin-delta-poll", "M5.2", "15 min", "seconds of one filtered read",
        _jellyfin_delta_poll, every=900, timeout=300),
    # The sweep: 120 s of its 300 s. An abandonment is free, since undecided keys stay pending.
    Job("jellyfin-intake-sweep", "M5.2", "webhook + 10 min debounce", "ms + one read per title",
        _jellyfin_intake_sweep, every=300, timeout=120),
    Job("explore-frontier-cache", "M6", "nightly", "minutes", every=86400),
    # No cadence: `every` is a fallback poll, and `_tick` fires the job when a row is queued. 600 s
    # equals the worker's `stop_grace_period` in docker-compose.yml (decision 300): keep them equal.
    # It is ~2.8x the 213 s measured on a warm NVMe box.
    Job(BUNDLE_IMPORT_JOB, "M0", "admin action", "minutes", _bundle_import,
        every=3600, stage=0, timeout=BUNDLE_IMPORT_TIMEOUT),
    # Before the dump in a tick, so an unwritable `/data/backups` is explained on the row above it.
    Job("storage-check", "M5", "hourly", "ms", _storage_check, every=3600, timeout=60),
    # §2's backup (see `_nightly_backup`); minutes of pg_dump at corpus scale.
    Job("nightly-backup", "M0", "nightly", "minutes", _nightly_backup, every=86400,
        anchor_hour=ANCHOR_BACKUP, timeout=2100),
)

# Jobs that fit against the active bundle and so must not run across §10's flip.
# `test_worker_schedule.py` derives the same set from each `run` and fails on drift.
MODEL_JOBS = frozenset({
    "ledger-map-refit",
    "ledger-refresh",
    "fold-in-user-vectors",
    "fold-in-tick",
    "tier-set-refit",
    "placement-reconciliation",
    # Stage 9 writes `title_placement`. The derivation cannot see that path (`pipeline.STAGES` is
    # data), so the batch-level `_active_store` in `_acquisition_drain` is what keeps this honest.
    "acquisition-drain",
})

# Seconds between wake-ups; `due` decides what fires.
TICK_SECONDS = 20
MIGRATION_WAIT_SECONDS = 2
# 150 x 2 s: a migration over populated tables can outlast a minute.
MIGRATION_WAIT_TRIES = 150

# Seconds after a failure before a retry; the pre-stamp in `_tick` stops a spin.
RETRY_AFTER = 300

# Seconds: faster jobs are not narrated per run (the log is §6.6's operator data).
DURATION_LOG_THRESHOLD = 900

# Its mtime is the loop's last round; the compose healthcheck reads its age.
HEARTBEAT_NAME = "worker.heartbeat"

_heartbeat_failed = False


def _local_zone() -> ZoneInfo | None:
    """§2's `TZ`, or None when unresolvable: a typo must not stop the loop (§3.1)."""
    try:
        return ZoneInfo(settings().tz)
    except Exception:  # noqa: BLE001 - a bad TZ must not stop the worker (§3.1)
        return None


def _now_local() -> datetime:
    """The household's wall clock, falling back to the process's own."""
    return datetime.now(_local_zone())


def _report_starting(cfg: Settings) -> None:
    """Names the clock the anchored jobs will use; an unresolvable TZ is a WARNING here, once."""
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


def _report_registry() -> None:
    """The boot census of §5.3's table: live, run elsewhere (`owner`), awaiting a milestone. ASCII only."""
    live = [j for j in JOBS if j.run is not None]
    elsewhere = [j for j in JOBS if j.run is None and j.owner is not None]
    awaiting = [j for j in JOBS if j.run is None and j.owner is None]
    log.info(
        "%d job(s) live in this loop; %d run outside it: %s; %d awaiting their milestone: %s",
        len(live),
        len(elsewhere),
        ", ".join(f"{j.name}({j.owner})" for j in elsewhere) or "none",
        len(awaiting),
        ", ".join(f"{j.name}({j.milestone})" for j in awaiting) or "none",
    )


async def _import_is_queued() -> bool:
    """Whether the import job has work a clock cannot see: a queued row, or a claim the reaper should
    close. Never raises."""
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT true FROM job_run "
                " WHERE name = $1 AND finished_at IS NULL "
                "   AND (detail->>'phase' = $2 OR (detail->>'phase' = $3 "
                "        AND coalesce((detail->>'claimed_at')::timestamptz, started_at) "
                "            < now() - ($4::float8 * interval '1 second'))) LIMIT 1",
                BUNDLE_IMPORT_JOB, PHASE_QUEUED, PHASE_RUNNING, BUNDLE_IMPORT_TIMEOUT,
            ))
    except Exception:
        log.exception("could not ask whether a bundle import is queued")
        return False


async def _import_in_flight() -> bool:
    """A read of `pg_locks`, never a try-lock, which would make a real writer's try-lock fail.
    `hashtext` is int4 sign-extended to int8, so the key is compared as classid/objid halves."""
    from spielplan.importer.bundle import IMPORT_LOCK

    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT true FROM pg_locks "
                " WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
                "   AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                "   AND classid::bigint = (hashtext($1)::bigint >> 32) & 4294967295 "
                "   AND objid::bigint = hashtext($1)::bigint & 4294967295 LIMIT 1",
                IMPORT_LOCK,
            ))
    except Exception:
        log.exception("could not ask whether a bundle import is running")
        return False


def _report_basis(store: ArtifactStore) -> None:
    """Tells a broken install (a WARNING with the repair) from a legal bundle-less one."""
    if store.broken:
        log.warning(
            "the active bundle's files are missing, so every model job in this loop will refuse "
            "rather than fit in a zero basis: restore /data/artifacts from backup, or import "
            "that bundle again from the Data tab, which restages the artifacts and re-runs the "
            "rebuild set. Restarting this process does not help - it reloads the same store"
        )
    elif store.is_empty:
        log.info("no artifact bundle active - model jobs stay idle (section 3.1: that is legal)")


def _report_storage() -> None:
    """At boot, before any job fails on it. A WARNING, never a refusal (§3.1)."""
    problem = storage.refusal(storage.probe(settings().data_dir, storage.WORKER_MOUNTS))
    if problem is not None:
        log.warning("storage: %s", problem)


async def _fill_term_labels(conn: asyncpg.Connection, store: ArtifactStore) -> None:
    """Backfill vocabulary labels for installs seeded before 0030. Never stops boot (decision 486)."""
    if store.is_empty or store.root is None or store.vocab_version is None:
        return
    from spielplan.importer import dna as dna_loader

    vocab_dir = store.root / "dna_vocab" / store.vocab_version
    try:
        filled = await dna_loader.backfill_labels(conn, vocab_dir, store.vocab_version)
    except Exception:  # noqa: BLE001 - labels are copy and must not stop the worker (§3.1)
        log.warning("vocabulary labels were not backfilled from %s", vocab_dir, exc_info=True)
        return
    if filled:
        log.info("backfilled %d vocabulary label(s) from %s", filled, vocab_dir)


def _touch_heartbeat() -> None:
    """Say the loop went round. Never raise: a heartbeat is evidence, not a dependency."""
    global _heartbeat_failed

    path = settings().data_dir / "cache" / HEARTBEAT_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        _heartbeat_failed = False
    except OSError as exc:
        # Once per outage, not once per tick.
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
    """Pure, so the schedule is testable without a clock. Unanchored jobs keep a monotonic interval;
    anchored ones fire once per local date at or after their hour, and RETRY_AFTER bounds a retry
    after a failure. Without `local`, no anchored job is due."""
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
    # Stable: equal stages keep §5.3's table order.
    return sorted(ready, key=lambda j: j.stage)


# The `job_run` id of the job running now, handed to `pipeline.drain` (decision 468). Exactly
# what `_record_start` returned, None included, so a walk never files under another run.
_JOB_RUN: contextvars.ContextVar[int | None] = contextvars.ContextVar("job_run", default=None)


async def _record_start(name: str) -> int | None:
    """Open the job's `job_run` row before it runs, so a killed job leaves "started, never
    finished". Never raises."""
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


async def _with_queued_import(ready: list[Job]) -> list[Job]:
    """`due`'s answer with the bundle import in front when queued: its flip replaces the basis every
    other job here fits against. Moved to the front even when `due` produced it."""
    job = next((j for j in JOBS if j.name == BUNDLE_IMPORT_JOB and j.run is not None), None)
    if job is None:
        return ready
    # Asked only when `due` did not already produce the row.
    if not any(j.name == BUNDLE_IMPORT_JOB for j in ready) and not await _import_is_queued():
        return ready
    return [job, *(j for j in ready if j.name != BUNDLE_IMPORT_JOB)]


async def _tick(
    now: float, local: datetime, last_run: dict[str, float], last_date: dict[str, date]
) -> None:
    _touch_heartbeat()
    loop = asyncio.get_running_loop()
    tick_started = loop.time()
    # Asked once per tick, and only when a model job is due.
    importing: bool | None = None
    for job in await _with_queued_import(due(now, last_run, local=local, last_date=last_date)):
        if job.name in MODEL_JOBS:
            if importing is None:
                importing = await _import_in_flight()
            if importing:
                # Neither stamped nor recorded: the next tick owes it.
                log.info(
                    "job %s skipped: a bundle import is in flight, and no fit may be written "
                    "across the swap that ends it", job.name,
                )
                continue
        # Stamped before the call, so a fast failure does not re-run every tick.
        last_run[job.name] = now
        if job.anchor_hour is not None:
            last_date[job.name] = local.date()
        loud = job.every >= DURATION_LOG_THRESHOLD
        if loud:
            log.info("job %s started", job.name)
        run_id = await _record_start(job.name)
        # Scoped to this job by the reset in `finally` (decision 468).
        job_run = _JOB_RUN.set(run_id)
        started = loop.time()
        try:
            # §5.3's budget, enforced: `wait_for` cancels, releasing the job's connection and locks.
            # A wedged job stops the heartbeat, and the compose check goes unhealthy (a signal only).
            detail = await asyncio.wait_for(job.run(), job.timeout)
        except Exception as exc:
            # Re-arm RETRY_AFTER after the failure (not after the tick opened, or a long failure retries at
            # once): interval jobs by rewinding the stamp, anchored ones by forgetting today. `min`, because
            # RETRY_AFTER is a ceiling, not a floor.
            if isinstance(exc, TimeoutError):
                # No traceback: a cancelled wait's points here; the budget and elapsed time are what matter.
                log.error(
                    "job %s did not finish within its %gs budget and was abandoned after %.1fs",
                    job.name, job.timeout, loop.time() - started,
                )
                reason = f"abandoned: no result within this job's {job.timeout:g}s budget"
            else:
                log.exception("job %s failed", job.name)
                reason = f"{type(exc).__name__}: {exc}"
            failed_at = now + (loop.time() - tick_started)
            if job.anchor_hour is None:
                last_run[job.name] = failed_at - job.every + min(job.every, RETRY_AFTER)
            else:
                last_run[job.name] = failed_at
                last_date.pop(job.name, None)
            await _record_finish(run_id, ok=False, detail={"error": reason})
        else:
            await _record_finish(run_id, ok=True, detail=detail)
            if loud:
                log.info("job %s done in %.1fs", job.name, loop.time() - started)
        finally:
            _JOB_RUN.reset(job_run)


async def _seed_schedule(
    conn, *, loop_now: float, local: datetime
) -> tuple[dict[str, float], dict[str, date]]:
    """Seed the schedule from each job's newest successful `job_run` row, so a restart neither spends
    a night nor skips one. One index lookup per name, never a DISTINCT ON scan."""
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
        # `astimezone(None)` is the system zone, matching `_now_local`'s fallback.
        last_date[row["name"]] = row["started_at"].astimezone(local.tzinfo).date()
    if last_run:
        log.info("schedule seeded from %d previous job run(s)", len(last_run))
    return last_run, last_date


async def main() -> None:
    cfg = settings()
    _report_starting(cfg)

    # Inside the `try`, so the `finally` closes the pool on a failed migration wait.
    try:
        await pool.open_pool(cfg.database_url, max_size=4)

        # The backend owns the schema and the worker only waits: two appliers would reject each other.
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
                raise RuntimeError(
                    "the database is still behind this build after "
                    f"{MIGRATION_WAIT_TRIES * MIGRATION_WAIT_SECONDS}s. Still pending: "
                    f"{', '.join(missing)}. The backend applies migrations and this process "
                    "never does, so read its log first: if it shows a migration error, fix that. "
                    "Nothing here can make progress until it lands."
                )
            store = await ArtifactStore.load_active(conn, cfg.artifacts_dir)
            await _fill_term_labels(conn, store)
            local = _now_local()
            last_run, last_date = await _seed_schedule(
                conn, loop_now=asyncio.get_running_loop().time(), local=local
            )

        _report_basis(store)
        _report_storage()
        _report_registry()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: stop.set())

        while not stop.is_set():
            # One wall-clock read per tick, so every anchored job is judged against one instant.
            await _tick(loop.time(), _now_local(), last_run, last_date)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    finally:
        await pool.close_pool()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
