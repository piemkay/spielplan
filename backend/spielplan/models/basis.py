"""The backend's basis - store, Backbone, §4.3 constants - and how it follows the active bundle.
Spec v2.1 §4.3, §5.1, §10; decision 497.

§10's swap sequence ended "transactionally flip `artifact_bundle.active` -> restart backend +
worker", and no clause said who restarts. The build made it the operator's, so the first household
finished the wizard onto "no bundle imported", a 409 on every Rate and Rank fit and `loaded: none`
on the Data tab until somebody with a shell typed `docker compose restart backend worker` - an
install that was no longer `docker compose up` plus the wizard (§2), behind a wizard that never
named the command. The worker never needed it: `worker._active_store` reads the active bundle for
every model job. Only this process pinned its basis at boot and never looked again.

So the backend now does what §4.3, §5.3 and §6.6 already called the import - a hot swap. It reads
the active row (`active_bundle_key`) every `FOLLOW_SECONDS` and before it answers the Data tab, and
when the row names a flip it has not loaded - a first import, decision 253's restage of a broken
install, or the replacement of a loaded bundle, models-only or not - it loads that bundle with the
same function the boot uses and replaces all three attributes in one event-loop step. The restart
stays as the fallback for a load that raised, which this module retries on every tick and reports
through `unloaded()` until it lands. [decision 497; owner instruction of 2026-09-25 after the first
household user test]

WHY §10's INVARIANT STILL HOLDS WITHOUT THE RESTART. "No process may score or refit with a loaded
bundle version different from the active row." Between the flip and the re-pin - seconds now - the
Rate/Rank guard answers 409 exactly as it did between the flip and a restart. A request that
straddles the re-pin reads the store, the Backbone and the version it stamps with in one expression
at every fit call site (`_embeddings(...)`, `_basis(...)` are adjacent arguments with no await
between them), and the three are replaced with no await between them either, so no call site can
pair one bundle's Backbone with the other's version. What a straddling request CAN hold is an
outgoing `hp` read before the re-pin beside an incoming basis read after it, and `refit.load_cache`
already refuses that pairing on its own terms - it compares the caller's basis against the stamp
and the active row, and the constants' digest against the cached one - so the fit is queued for the
full refit rather than written in a mixed basis. That is the data-01 defence M4.13 built for the
tap that waits out an import, and it is why this re-pin needs no request-scoped copy of the basis.
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

# The `spielplan` logger and not a child of it: these are the lines the lifespan has always
# written, and `test_boot_logging.py` and README's runbook read them off that name.
log = logging.getLogger("spielplan")

# How often the backend asks whether the active row moved. One indexed single-row read, so the
# cost of asking is nothing; the number is how long a member's first tap after an import can meet
# the swap's 409. `None` turns the timer off and leaves the on-demand refresh - which is what the
# test suite's `app` fixture does, because its window tests pin `app.state` by hand and a timer
# re-pinning underneath them would make them race the clock.
FOLLOW_SECONDS: float | None = 5.0


@dataclass(frozen=True)
class Basis:
    """What one load produced, replaced as a unit. `hyperparams` is None when the bundle's constants
    are unusable, which the routers already read as "refuse with a 503" (`api/rate.py`)."""

    store: ArtifactStore
    backbone: backbone.Backbone
    hyperparams: hyperparams.Hyperparams | None
    # The active row this basis was loaded for, as `active_bundle_key` reads it.
    key: tuple[str, Any] | None


def _open(store: ArtifactStore) -> tuple[backbone.Backbone, hyperparams.Hyperparams | None]:
    """The file half of a load: the Backbone and §4.3's constants for a store already read.

    §5.1's basis, loaded once per pin - and a Backbone that fails to load degrades the scoring
    surfaces rather than stopping a boot the admin needs in order to fix the bundle.
    §4.3's constants ride with the basis, and for the same reason: one read per pin is all the
    spec ever asks for. Every Ledger router had a `_hyperparams` fallback that re-read the file per
    request - a read, a `from_mapping` validation and a note list on every tap of Rate, every board
    GET and every duel - and `load_cache` then re-digested the result. One attribute is the whole
    repair, and the notes are logged once instead of being thrown away at DEBUG on every request:
    §4.3's provenance ("a number from a default and the same number from a bundle mean different
    things when someone is reading a refit report") is a fact about the pin.

    Ordered after the basis on purpose. `from_mapping` raises `ValueError` on a constant outside its
    range, and the constants are then None so the routers refuse with a 503 rather than fit against
    silently substituted defaults - a different `hp_digest` invalidates every cached fit in the
    install, which is a worse failure than a refusal. Were the constants read first, that same
    ValueError would skip the basis and degrade the scoring surfaces for a reason that has nothing
    to do with them. The boot itself is not refused: §3.1 keeps a half-configured boot legal and
    the admin needs these routes in order to import a bundle that parses. [M4.10 finding 10; ml06,
    perf-07]

    Two `try` blocks and not one, which is the whole of the ordering argument above and none of its
    cost. `BackboneError` is a `RuntimeError`, so in one shared block an unusable basis - a state
    the paragraph above declares supported - took the first handler and the constants read never
    ran: the routers went back to reading, validating and re-digesting the file on every request,
    which is the per-request cost (perf-07) this read exists to delete, with §4.3's provenance
    notes never logged either. `OSError` joins `ValueError` on the second because
    `hyperparams.load` lets a present-but-unopenable constants file reach `read_text` rather than
    reading it as an absent one, and a boot that dies on it would be the one outcome §3.1 forbids
    here. [M4.10 cycle 1, M410-R1-02 / M410-R1-04 / M410-C1-HP-1]

    Moved here from `app.py`'s lifespan unchanged, so the boot and the re-pin load the same things
    in the same order (decision 497). Synchronous, and run in a thread by `load`: the Backbone is an
    `np.load` of the bundle's largest array, and the re-pin happens on a loop that is serving.
    """
    try:
        bb = backbone.load_for(store)
    except backbone.BackboneError:
        log.exception("backbone.npz is unusable — serving without collaborative scores")
        bb = backbone.Backbone.empty()
    try:
        hp, notes = hyperparams.load(store)
        for note in notes:
            log.info("hyperparameters: %s", note)
        # THE YARDSTICK, ONCE, BESIDE THE CONSTANTS IT IS READ WITH. Spec section 14's first risk
        # states its own mitigation as "expectations instrumented, not assumed", and
        # `user_vector.cv_rho` was neither: the fold-in computes a held-out Spearman per (user,
        # kind), stores it, and nothing in the app knew what a good one looks like. The corpus
        # ships the reference - `cold_eval.json`'s cold and ceiling figures - and this is the
        # process's one statement of it, at INFO like the constants above and for the same reason:
        # the pair is a fact about the pin, and an operator reading `cv_rho` in a later log line
        # has the scale in the same file. The floor comes from `hp`, not from DEFAULTS, so a bundle
        # that re-tunes the tie band is reported with its own. [M4.13 step 35, cs-31]
        yardstick = store.cold_eval()
        if yardstick is not None:
            log.info("fold-in rho reads against cold_eval.json: %s",
                     yardstick.line(floor=hp.rho_noise_floor))
        elif not store.is_empty:
            log.info(
                "bundle %s ships no cold_eval.json - a fitted cv_rho has no reference value "
                "in this install (spec section 0 row 1, section 14 risk 1)",
                store.version,
            )
    except (ValueError, OSError):
        # `log.exception` so the key is named twice: once in the message the operator greps for
        # and once in the traceback that says which check refused it.
        log.exception(
            "ledger_hyperparams.json is unusable - the Rate and Rank surfaces will answer "
            "503 until the bundle is fixed (spec section 4.3)"
        )
        hp = None
    return bb, hp


async def load(conn: asyncpg.Connection, artifacts_dir: Path) -> Basis:
    """Load whatever the active row names, the way the boot always has (§4.3: "when present").

    The key is read BEFORE the store. A flip landing between the two reads then leaves a key older
    than the store, which costs one redundant re-pin on the next tick; the other order would pin
    the incoming store under the outgoing key's twin and never look again.
    """
    key = await active_bundle_key(conn)
    store = await ArtifactStore.load_active(conn, artifacts_dir)
    bb, hp = await asyncio.to_thread(_open, store)
    return Basis(store=store, backbone=bb, hyperparams=hp, key=key)


def pin(state: Any, basis: Basis) -> None:
    """Replace the three attributes every artifact-dependent reader uses, in one event-loop step.

    No await anywhere in this function, and that is the atomicity: a coroutine on this loop sees
    either the whole outgoing basis or the whole incoming one. `app.state.hyperparams` is None for
    unusable constants, which `getattr(..., None)` in the routers reads exactly as it read the
    attribute being left unset.
    """
    state.artifacts = basis.store
    state.backbone = basis.backbone
    state.hyperparams = basis.hyperparams
    state.basis_key = basis.key


class _Follower:
    """This process's half of decision 497: the lock, the timer and the one failure it remembers."""

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir
        # Per app and created inside its lifespan, so it is bound to the loop that serves it; a
        # module-level `asyncio.Lock` is bound to the first loop that awaited it, and the suite
        # runs one loop per test (`api/artifacts.py::_EXTRACTING` makes the same argument).
        self.lock = asyncio.Lock()
        # The active row a load raised for, until one lands. Logged once per row rather than once
        # per tick, and read by `unloaded()` for the shell.
        self.failed_key: tuple[str, Any] | None = None
        self.task: asyncio.Task | None = None


def start(state: Any, artifacts_dir: Path) -> None:
    """Arm the follower for an app whose lifespan has just pinned its boot basis."""
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
    """Re-pin if the active row moved since this process last loaded it. True when it re-pinned.

    A no-op for an app with no follower - a test that builds `app.state` by hand, or a route
    called on a stub request - so the callers need not ask. Serialised on the follower's lock, so
    the Data tab's poll and the timer never load one bundle twice at once.
    """
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
    """Whether the active row names a bundle this process tried to load and could not.

    Read from memory and never from the database, because its reader is `/api/config`, the
    unauthenticated shell bootstrap whose failure semantics decision 271 defines: a database stall
    there must not turn a household's header neutral.
    """
    follower = getattr(state, "basis_follower", None)
    return follower is not None and follower.failed_key is not None
