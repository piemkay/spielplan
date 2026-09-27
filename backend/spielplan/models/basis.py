"""The backend's basis (store, Backbone, §4.3 constants) and how it follows the active bundle without
a restart (decision 497). Each fit reads store, Backbone and version with no await between them, and
`pin` swaps all three in one loop step, so no call site mixes two bundles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from spielplan.db import pool
from spielplan.ledger import hyperparams
from spielplan.models.artifacts import ArtifactStore, active_bundle_key
from spielplan.scoring import backbone

# The `spielplan` logger itself: tests and README's runbook read these lines off that name.
log = logging.getLogger("spielplan")

# Seconds between active-row checks: how long a first tap after an import can meet the 409. None
# disables the timer (the test `app` fixture pins `app.state` by hand).
FOLLOW_SECONDS: float | None = 5.0


@dataclass(frozen=True)
class Basis:
    """Replaced as a unit. `hyperparams` is None for unusable constants, which the routers answer 503."""

    store: ArtifactStore
    backbone: backbone.Backbone
    hyperparams: hyperparams.Hyperparams | None
    key: tuple[str, Any] | None


def _open(store: ArtifactStore) -> tuple[backbone.Backbone, hyperparams.Hyperparams | None]:
    """The Backbone, then §4.3's constants, in two `try` blocks: an unusable basis (`BackboneError` is a
    RuntimeError) must not skip the constants, and bad constants must not cost the basis.
    Synchronous: `load` runs it in a thread."""
    try:
        bb = backbone.load_for(store)
    except backbone.BackboneError:
        log.exception("backbone.npz is unusable — serving without collaborative scores")
        bb = backbone.Backbone.empty()
    try:
        hp, notes = hyperparams.load(store)
        for note in notes:
            log.info("hyperparameters: %s", note)
    except (ValueError, OSError):
        log.exception(
            "ledger_hyperparams.json is unusable - the Rate and Rank surfaces will answer "
            "503 until the bundle is fixed (spec section 4.3)"
        )
        hp = None
    return bb, hp


async def load(conn: asyncpg.Connection, artifacts_dir: Path) -> Basis:
    """Key before store: a flip in between costs one redundant re-pin, never a missed one."""
    key = await active_bundle_key(conn)
    store = await ArtifactStore.load_active(conn, artifacts_dir)
    bb, hp = await asyncio.to_thread(_open, store)
    return Basis(store=store, backbone=bb, hyperparams=hp, key=key)


def pin(state: Any, basis: Basis) -> None:
    """No await in here: that is the atomicity. A coroutine sees the whole old or the whole new basis."""
    state.artifacts = basis.store
    state.backbone = basis.backbone
    state.hyperparams = basis.hyperparams
    state.basis_key = basis.key


class _Follower:
    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir
        # Created inside the lifespan so it binds to the serving loop; tests run one loop each.
        self.lock = asyncio.Lock()
        # The active row a load raised for, until one lands; logged once per row.
        self.failed_key: tuple[str, Any] | None = None
        self.task: asyncio.Task | None = None


def start(state: Any, artifacts_dir: Path) -> None:
    follower = _Follower(artifacts_dir)
    state.basis_follower = follower
    if FOLLOW_SECONDS:
        follower.task = asyncio.get_running_loop().create_task(_follow(state, FOLLOW_SECONDS))


async def stop(state: Any) -> None:
    follower = getattr(state, "basis_follower", None)
    if follower is None or follower.task is None:
        return
    follower.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await follower.task


async def _follow(state: Any, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            await refresh(state)
        except Exception:  # noqa: BLE001 - the timer must outlive a database that blinked
            log.warning("could not ask which bundle is active; asking again", exc_info=True)


async def refresh(state: Any, conn: asyncpg.Connection | None = None) -> bool:
    """True when it re-pinned. A no-op without a follower; serialised on its lock."""
    follower = getattr(state, "basis_follower", None)
    if follower is None:
        return False
    async with follower.lock:
        if conn is not None:
            return await _refresh(state, follower, conn)
        async with pool.acquire() as own:
            return await _refresh(state, follower, own)


async def _refresh(state: Any, follower: _Follower, conn: asyncpg.Connection) -> bool:
    key = await active_bundle_key(conn)
    if key == getattr(state, "basis_key", None):
        return False
    try:
        basis = await load(conn, follower.artifacts_dir)
    except Exception:  # noqa: BLE001 - the outgoing basis keeps serving; `unloaded()` reports it
        if key != follower.failed_key:
            log.exception(
                "bundle %s is active and this process could not load it; it keeps serving %s "
                "and tries again every few seconds - if this persists, restart backend and worker",
                key[0] if key else None, getattr(getattr(state, "artifacts", None), "version", None),
            )
        follower.failed_key = key
        return False
    outgoing = getattr(getattr(state, "artifacts", None), "version", None)
    pin(state, basis)
    follower.failed_key = None
    log.info(
        "bundle %s is active and loaded in this process without a restart (was %s; decision 497)",
        basis.store.version, outgoing,
    )
    return True


def unloaded(state: Any) -> bool:
    """From memory, never the database: its reader is the unauthenticated `/api/config` (decision 271)."""
    follower = getattr(state, "basis_follower", None)
    return follower is not None and follower.failed_key is not None
