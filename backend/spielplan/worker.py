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


async def _prune_expired_sessions() -> None:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM auth_session WHERE expires_at < now()")
        gone = str(result).rsplit(" ", 1)[-1]
        if gone != "0":
            log.info("pruned %s expired session(s)", gone)


async def _prune_dead_push_subscriptions() -> None:
    """§4.2: push targets are 'pruned on 404/410 from the push service'. Until push ships (M4)
    the only thing to clean is a subscription whose device never came back."""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM push_subscription "
            "WHERE last_seen_ok IS NOT NULL AND last_seen_ok < now() - interval '90 days'"
        )


# §5.3's table, in order. `run=None` means the milestone that owns it has not arrived.
JOBS: tuple[Job, ...] = (
    Job("session-prune", "M0", "hourly", "ms", _prune_expired_sessions),
    Job("push-subscription-prune", "M0", "daily", "ms", _prune_dead_push_subscriptions),
    Job("ledger-incremental", "M2", "every observation", "<50 ms"),
    Job("ledger-map-refit", "M2", "nightly", "seconds"),
    Job("fold-in-user-vectors", "M2", "nightly", "seconds"),
    Job("cold-tower-placement", "M2", "acquisition pipeline", "<1 s/title"),
    Job("placement-reconciliation", "M2", "bundle import + nightly sweep", "seconds"),
    Job("dna-projection", "M5", "acquisition", "<1 s"),
    Job("jellyfin-seen-sync", "M1", "15 min + webhook", "—"),
    Job("explore-frontier-cache", "M6", "nightly", "minutes"),
)

TICK_SECONDS = 3600
MIGRATION_WAIT_SECONDS = 2
MIGRATION_WAIT_TRIES = 30


async def _tick() -> None:
    for job in JOBS:
        if job.run is None:
            continue
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

    try:
        while not stop.is_set():
            await _tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
    finally:
        await pool.close_pool()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
