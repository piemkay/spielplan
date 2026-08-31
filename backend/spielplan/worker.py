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

from spielplan.core.config import settings
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
    run: Callable[[], Awaitable[None]] | None = None
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


async def _jellyfin_seen_sync() -> None:
    """§7.3's 15-minute reconciliation, and §5.3's `jellyfin-seen-sync` row."""
    from spielplan.sync import seen

    async with pool.acquire() as conn:
        report = await seen.sync_all(conn)
        if report.pushed or report.adopted or report.needs_relink:
            log.info("jellyfin seen sync: %s", report.as_dict())


async def _jellyfin_sessions_poll() -> None:
    """§7.3: ">= 90% playback (poll /Sessions + IsPlayed delta) arms a per-user prompt."

    A minute is the useful resolution: the window between crossing 90% and the credits ending
    is minutes long, and a prompt that arrives after the TV is off has missed its moment.
    """
    from spielplan.sync import playback

    async with pool.acquire() as conn:
        report = await playback.poll(conn)
        if report.armed:
            log.info("armed %d finish prompt(s)", report.armed)


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


async def _ledger_map_refit() -> None:
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


async def _fold_in_user_vectors() -> None:
    """§5.3: "User fold-in + blend weights — nightly, seconds"."""
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        if store is None:
            log.info("fold-in skipped: no active bundle (§3.1)")
            return
        report = await foldin.run(
            conn, bb.load_for(store), bundle_version=store.version,
            only_stale=False, with_priors=True,
        )
        log.info("fold-in: %s", report.as_dict())


async def _fold_in_tick() -> None:
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
            return
        report = await foldin.run(
            conn, bb.load_for(store), bundle_version=store.version,
            only_stale=True, with_priors=False,
        )
        if report.refit:
            log.info("fold-in tick: %s", report.as_dict())


async def _tier_set_refits() -> None:
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
            return
        store = await _active_store(conn)
        hp, _notes = load_hp(store or ArtifactStore.empty())
        embeddings = (
            observations.standard_embeddings(conn, bb.load_for(store)) if store else None
        )
        for user_id, kind in owed:
            report = await refit.refit_user(
                conn, user_id=user_id, kind=kind, hp=hp, embeddings=embeddings
            )
            await tiers.clear_refit_request(conn, user_id=user_id, kind=kind)
            log.info("tier-set refit: %s", report.as_dict())


async def _placement_reconciliation() -> None:
    """§5.3: "any owned title lacking a coordinate gets a feature vector built from DB data per
    the feature contract … and runs §8 stages 9-10 only". Trigger: "bundle import + nightly
    sweep" — the import half lives in the importer, this is the sweep."""
    from spielplan.placement import reconcile

    async with pool.acquire() as conn:
        store = await _active_store(conn)
        if store is None:
            log.info("placement sweep skipped: no active bundle (§3.1)")
            return
        report = await reconcile.reconcile(conn, store, scope="owned_missing")
        if report.placed or report.failed or report.demoted:
            log.info("placement: %s", report.as_dict())


# §5.3's table, in order. `run=None` means the milestone that owns it has not arrived.
JOBS: tuple[Job, ...] = (
    Job("session-prune", "M0", "hourly", "ms", _prune_expired_sessions, every=3600),
    Job("push-subscription-prune", "M0", "daily", "ms", _prune_dead_push_subscriptions,
        every=86400),
    Job("webauthn-challenge-prune", "M1", "hourly", "ms", _prune_webauthn_challenges,
        every=3600),
    Job("ledger-incremental", "M2", "every observation", "<50 ms"),
    Job("ledger-map-refit", "M2", "nightly", "seconds", _ledger_map_refit, every=86400),
    Job("fold-in-user-vectors", "M2", "nightly", "seconds", _fold_in_user_vectors,
        every=86400),
    # Not in §5.3's table, and named here rather than smuggled in: §5.3 gives the fold-in a
    # nightly cadence, but §12's M2 exit criterion is about what a person sees *within a
    # sitting*, and every §6.0 shelf orders by a table only the fold-in writes. `foldin.run`
    # documents this tick as the answer to exactly that.
    Job("fold-in-tick", "M2", "after each sitting's writes", "ms", _fold_in_tick, every=60),
    # Decision 11's second trigger for §5.3's nightly fit. See `_tier_set_refits`.
    Job("tier-set-refit", "M3", "tier-set change", "seconds", _tier_set_refits, every=60),
    Job("cold-tower-placement", "M2", "acquisition pipeline", "<1 s/title"),
    Job("placement-reconciliation", "M2", "bundle import + nightly sweep", "seconds",
        _placement_reconciliation, every=86400, stage=0),
    Job("dna-projection", "M5", "acquisition", "<1 s"),
    Job("jellyfin-seen-sync", "M1", "15 min + webhook", "—", _jellyfin_seen_sync, every=900),
    Job("jellyfin-sessions-poll", "M1", "1 min", "ms", _jellyfin_sessions_poll, every=60),
    Job("explore-frontier-cache", "M6", "nightly", "minutes", every=86400),
)

# The loop wakes far more often than any job runs; `due` decides what actually fires. A single
# hourly tick would have made the 1-minute /Sessions poll a 1-hour one.
TICK_SECONDS = 20
MIGRATION_WAIT_SECONDS = 2
MIGRATION_WAIT_TRIES = 30


def due(now: float, last_run: dict[str, float]) -> list[Job]:
    """The jobs whose interval has elapsed. Pure, so the schedule is testable without a clock.

    A job that has never run is due immediately: a worker restart should reconcile, not wait
    out a fifteen-minute interval it has no memory of.
    """
    ready = [
        job
        for job in JOBS
        if job.run is not None and now - last_run.get(job.name, float("-inf")) >= job.every
    ]
    # Stable, so equal stages keep §5.3's table order and only the declared dependency moves.
    return sorted(ready, key=lambda j: j.stage)


async def _tick(now: float, last_run: dict[str, float]) -> None:
    for job in due(now, last_run):
        last_run[job.name] = now
        try:
            await job.run()
        except Exception:
            # One failing job must not take the loop down; the next tick retries it.
            log.exception("job %s failed", job.name)


async def main() -> None:
    cfg = settings()
    log.info("worker starting · tz=%s · db=%s", cfg.tz, cfg.database_url.rsplit("@", 1)[-1])
    await pool.open_pool(cfg.database_url, max_size=4)

    # The backend owns the schema. The worker waits for it and never applies: during a rolling
    # restart the two are briefly on different images, and two appliers means whichever loses
    # the race has its own migration rejected by the other's checksum.
    async with pool.acquire() as conn:
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
                f"{MIGRATION_WAIT_TRIES * MIGRATION_WAIT_SECONDS}s — is the backend running?"
            )
        store = await ArtifactStore.load_active(conn, cfg.artifacts_dir)

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

    last_run: dict[str, float] = {}
    try:
        while not stop.is_set():
            await _tick(asyncio.get_running_loop().time(), last_run)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    finally:
        await pool.close_pool()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
