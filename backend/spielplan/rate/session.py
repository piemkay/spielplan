"""The Rate surface's state machine: one placement card at a time (§6.1, §6.7, decision 35).

The server owns the block counter, the card on the table and its token, so Undo's boundary and a
stale tap are enforceable. It draws no cards itself: `queue` decides what to ask and `shelves` what
each shelf shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import numpy as np

from spielplan.art.poster import url_epoch
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import SECRETS_UNREADABLE_REASON, JellyfinConfig
from spielplan.db import dna_terms
from spielplan.db import pool as db_pool
from spielplan.db.library import normalise_kinds
from spielplan.derive.ids import APP_ID_MIN
from spielplan.home import rail
from spielplan.ledger import ladder, model, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import EmbeddingSource, PriorState
from spielplan.rate import queue
from spielplan.rate import shelves as rate_shelves
from spielplan.sync import seen

log = logging.getLogger("spielplan.rate.session")

# §6.1: "A block is 15 films", the unit decision 35 measures Undo's depth in.
BLOCK_SIZE = 15

DRAINED_LINE = "There's nothing more to rate right now."


class StaleCard(Exception):
    """The answer names a card that is not the one on the table (double tap, back, second device)."""

    def __init__(self, reason: Literal["no_card", "stale_card"] = "stale_card") -> None:
        super().__init__(reason)
        self.reason = reason


class BadTier(ValueError):
    """A placement on a step outside the person's set for the card's kind."""


class UndoUnavailable(Exception):
    """Decision 35: "back to the start of the current block of 15 and no further"."""

    def __init__(self, reason: Literal["empty", "block_boundary"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Jellyfin:
    """§7.3's write path, injected. `client is None` is legal (§3.3): the write then stays owed."""

    client: JellyfinClient | None
    cfg: JellyfinConfig


@dataclass
class RateSession:
    id: int
    user_id: int
    kinds: list[str]
    block_index: int
    slot: int
    seq: int
    current_card: dict[str, Any] | None
    card_token: uuid.UUID | None

    @property
    def kind(self) -> str:
        """Rate asks one kind at a time; a session from before that held two and asks the first."""
        return self.kinds[0]


class RailLine(str):
    """A log line that remembers which title it narrates.

    A `str` subclass because `Outcome.log` is serialised and read with `in`; the id travels with
    the line because `rail.record` runs after `s.current_card` has moved on.
    """

    def __new__(cls, text: str, *, title_id: int | None = None) -> RailLine:
        line = super().__new__(cls, text)
        line.title_id = title_id
        return line


@dataclass(frozen=True)
class Outcome:
    """One tap's result: the session as it now stands, and everything the response carries."""

    session: RateSession
    echo: dict[str, Any] | None = None
    # Plain strings except where §6.7's line is about one title, which `RailLine` carries.
    log: tuple[str, ...] = ()
    ledger: dict[str, Any] | None = None
    undone: str | None = None


# --- the block machine -------------------------------------------------------------------


def advance(block_index: int, slot: int) -> tuple[int, int]:
    """§6.1: the counter runs 1..15 and rolls into a new block.

    The roll moves the counter only: the fifteenth tap stays undoable until the next block's
    first observation lands (decisions 174, 199).
    """
    if slot >= BLOCK_SIZE:
        return block_index + 1, 1
    return block_index, slot + 1


_SESSION_COLUMNS = "id, user_id, kinds, block_index, slot, seq, current_card, card_token"


def _session(row: asyncpg.Record) -> RateSession:
    card = row["current_card"]
    return RateSession(
        id=row["id"],
        user_id=row["user_id"],
        kinds=list(row["kinds"]),
        block_index=row["block_index"],
        slot=row["slot"],
        seq=row["seq"],
        current_card=dict(card) if card else None,
        card_token=row["card_token"],
    )


def one_kind(kinds: Sequence[str]) -> list[str]:
    """Rate asks one kind at a time, switched from its title (§4.1 rule 5)."""
    wanted = normalise_kinds(kinds)
    if len(wanted) != 1:
        raise ValueError("Rate asks about films or series, one at a time")
    return wanted


async def open_or_resume(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str] | None = None,
    restart: bool = False,
) -> RateSession:
    """One live session per person (`rate_session_one_live`); `restart=True` ends it first. A new
    session asks about films unless `kinds` says otherwise; a resumed one keeps its kind."""
    wanted = one_kind(kinds) if kinds is not None else ["movie"]
    async with conn.transaction():
        if restart:
            await conn.execute(
                "UPDATE rate_session SET ended_at = now() "
                "WHERE user_id = $1 AND ended_at IS NULL",
                user_id,
            )
        # The conflict target names the partial index's own predicate.
        row = await conn.fetchrow(
            f"""
            INSERT INTO rate_session (user_id, kinds)
            VALUES ($1, $2::text[])
            ON CONFLICT (user_id) WHERE ended_at IS NULL DO NOTHING
            RETURNING {_SESSION_COLUMNS}
            """,
            user_id,
            wanted,
        )
        if row is None:
            row = await conn.fetchrow(
                f"UPDATE rate_session SET last_seen_at = now() "
                f"WHERE user_id = $1 AND ended_at IS NULL RETURNING {_SESSION_COLUMNS}",
                user_id,
            )
    if row is None:  # pragma: no cover — the insert and the update cannot both miss.
        raise RuntimeError(f"no live rate session for user {user_id} after open_or_resume")
    return _session(row)


async def end_session(conn: asyncpg.Connection, *, user_id: int) -> bool:
    ended = await conn.fetchval(
        "UPDATE rate_session SET ended_at = now() "
        "WHERE user_id = $1 AND ended_at IS NULL RETURNING id",
        user_id,
    )
    return ended is not None


async def set_kinds(conn: asyncpg.Connection, s: RateSession, kinds: Sequence[str]) -> RateSession:
    """The Films/Series switch. A change drops the card on the table; the block carries on."""
    wanted = one_kind(kinds)
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET kinds = $2::text[], last_seen_at = now(),
               current_card = CASE WHEN $3 THEN NULL ELSE current_card END,
               card_token   = CASE WHEN $3 THEN NULL ELSE card_token END
         WHERE id = $1
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        wanted,
        wanted != s.kinds,
    )
    return _session(row)


# --- the card cursor ---------------------------------------------------------------------


async def _observed_title_ids(conn: asyncpg.Connection, session_id: int) -> list[int]:
    """What this sitting has already put in front of the person; Undo lifts the suppression."""
    rows = await conn.fetch(
        "SELECT DISTINCT unnest(title_ids) AS title_id FROM rate_observation "
        "WHERE session_id = $1 AND undone_at IS NULL",
        session_id,
    )
    return [r["title_id"] for r in rows]


async def _draw(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    exclude: Sequence[int],
    head: Sequence[int],
    rng: Any = None,
) -> dict[str, Any] | None:
    cards = await queue.next_cards(
        conn, user_id=s.user_id, kind=s.kind, limit=1, exclude=tuple(exclude), head=tuple(head),
        rng=rng,
    )
    if not cards:
        return None
    card = cards[0]
    return {
        "kind": s.kind,
        "title_id": card.title_id,
        "reason": card.reason,
        "p_seen": card.p_seen,
        "source": card.source,
        # §13 stream (b): server-side only. `public_card` has no field for it by construction.
        "reask_of": card.reask_of,
    }


async def _draw_pinned(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    served: Sequence[int],
    head: Sequence[int],
    rng: Any = None,
) -> dict[str, Any] | None:
    """A card for the first drawable pinned title, or None.

    A pin lifts this sitting's suppression of the titles it names: the person asked for them again.
    """
    pinned = set(head)
    card = await _draw(conn, s, exclude=[t for t in served if t not in pinned], head=head, rng=rng)
    return card if card is not None and card["title_id"] in pinned else None


async def _follow_pins(
    conn: asyncpg.Connection, s: RateSession, head: Sequence[int]
) -> RateSession:
    """The first pin's kind is the session's (a finish prompt for a series, §6.0's banner naming a
    series first), rather than serving across §4.1 rule 5's partition."""
    rows = await conn.fetch("SELECT id, kind FROM title WHERE id = ANY($1::int[])", list(head))
    kinds = {int(r["id"]): r["kind"] for r in rows}
    pinned = [kinds[t] for t in head if t in kinds]
    if not pinned or pinned[0] == s.kind:
        return s
    return await set_kinds(conn, s, [pinned[0]])


async def ensure_card(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    rng: Any = None,
    head: Sequence[int] = (),
) -> RateSession:
    """Idempotent: draws only when the table is empty, and never before the person's set-up (Rate
    is closed until then). An explicit `head` the stashed card does not satisfy redraws once
    (§6.0's banner must serve the titles it names); a pin is also served on an empty table first.
    """
    if await ladder.set_up_at(conn, user_id=s.user_id) is None:
        return s
    head = tuple(int(t) for t in head)
    if head:
        s = await _follow_pins(conn, s, head)
    served = await _observed_title_ids(conn, s.id)
    if s.current_card is not None:
        if not head or s.current_card.get("title_id") in head:
            return s
        replacement = await _draw_pinned(conn, s, served=served, head=head, rng=rng)
        if replacement is None:
            return s
        return await stash(conn, s, replacement, expected=s.card_token)

    card = await _draw_pinned(conn, s, served=served, head=head, rng=rng) if head else None
    if card is None:
        card = await _draw(conn, s, exclude=served, head=head, rng=rng)
    return await stash(conn, s, card, expected=None)


async def stash(
    conn: asyncpg.Connection,
    s: RateSession,
    card: dict[str, Any] | None,
    *,
    expected: uuid.UUID | None,
) -> RateSession:
    """Write the card under a fresh token if the stored token is still `expected` (None: an empty
    table), decision and write in one statement, so two GETs or two devices end up under one token.
    A replacement that missed stashes into a table since emptied; otherwise the card another
    request stashed first is served."""
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET current_card = $2::jsonb, card_token = $3, last_seen_at = now()
         WHERE id = $1 AND card_token IS NOT DISTINCT FROM $4
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        card,
        uuid.uuid4() if card is not None else None,
        expected,
    )
    if row is None and expected is not None:
        return await stash(conn, s, card, expected=None)
    if row is None:
        row = await conn.fetchrow(f"SELECT {_SESSION_COLUMNS} FROM rate_session WHERE id = $1", s.id)
    return _session(row)


async def _titles(conn: asyncpg.Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The poster-forward card of §6.8, and nothing else: no `ledger_state`, `user_score` or
    `title.placement`, so the card cannot anchor on the model."""
    rows = await conn.fetch(
        "SELECT id, name, original_name, original_language, year, runtime_min, poster_path "
        "FROM title WHERE id = ANY($1::int[])",
        list(ids),
    )
    return {int(r["id"]): dict(r) for r in rows}


async def public_card(
    conn: asyncpg.Connection, s: RateSession, *, version: str | None
) -> dict[str, Any] | None:
    """§6.1: nothing the model guesses shows before the tap (anchoring; Cosley 2003).

    Built from an allow-list, so `reask_of` and the card's source never reach the wire.
    """
    card = s.current_card
    if card is None or s.card_token is None:
        return None
    title_id = card["title_id"]
    shelves = await rate_shelves.shelves_for(
        conn, user_id=s.user_id, title_id=title_id, kind=card["kind"], version=version
    )
    public = {
        "token": str(s.card_token),
        "kind": card["kind"],
        "title": (await _titles(conn, [title_id])).get(title_id),
        "reason": card["reason"],
        "shelves": [shelf.as_dict() for shelf in shelves],
    }
    # P(seen) travels under `model`, which `rail.redact` strips with Show the model off, and only
    # where the queue placed the card by it.
    if card.get("source") in ("seed", "p_seen") and card.get("p_seen") is not None:
        public["model"] = {"p_seen": round(float(card["p_seen"]), 2)}
    return public


async def _preload(
    conn: asyncpg.Connection, s: RateSession, *, version: str | None
) -> list[str]:
    """The art of the card most likely next (§6: the next card preloaded): its poster and its
    shelves', each URL as the client draws it. A peek with no re-ask draw, so it may miss; it
    writes nothing."""
    served = await _observed_title_ids(conn, s.id)
    current = [s.current_card["title_id"]] if s.current_card else []
    nxt = await queue.next_cards(
        conn, user_id=s.user_id, kind=s.kind, limit=1, exclude=[*served, *current], reask_rate=0.0
    )
    if not nxt:
        return []
    shelves = await rate_shelves.shelves_for(
        conn, user_id=s.user_id, title_id=nxt[0].title_id, kind=s.kind, version=version
    )
    ids = list(dict.fromkeys([nxt[0].title_id, *(film.id for shelf in shelves for film in shelf.films)]))
    epoch = await url_epoch(conn) if any(t >= APP_ID_MIN for t in ids) else None
    return [
        f"/api/art/{t}/poster" + (f"?v={epoch}" if epoch and t >= APP_ID_MIN else "") for t in ids
    ]


# --- the model's guess, read before the tap and shown only after it --------------------------


@dataclass(frozen=True)
class Guess:
    tier: int      # in the person's set today
    cdf: float


async def _predicted_coordinate(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
) -> tuple[float, int, int] | None:
    """§5.2's zero-parameter prediction for a title with no `ledger_state` row of its own.

    `ledger_state` is sized to the owned library and the seed-list queue is not, so this computes
    `s = mu + <v, e>` from the cached fit. Unlocked: a read for one card, before the write.
    """
    cache = await refit.load_cache(conn, user_id=user_id, kind=kind, hp=hp, lock=False)
    if cache is None or cache.cdf_reference.size < 2:
        return None
    matrix, _embedded = await observations.resolve_embeddings(
        embeddings or observations.zero_embeddings, [title_id]
    )
    s = float(cache.mu + matrix[0] @ cache.v)
    cdf = float(model.empirical_cdf(cache.cdf_reference, np.asarray([s]))[0])
    if not (math.isfinite(s) and math.isfinite(cdf)):
        # The same refusal `refit` makes of a non-finite update.
        return None
    return cdf, int(model.tier_of(np.asarray([s]), cache.cuts)[0]), cache.n_levels


async def guess(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
) -> Guess | None:
    """The step the model would have guessed, on the person's own cutpoints (decision 510), read
    BEFORE the write. The stored row first, the cached fit second; None before a fit."""
    row = await conn.fetchrow(
        "SELECT ls.cdf, ls.tier, "
        "       (SELECT cardinality(c.tier_set) FROM ledger_cutpoints c "
        "         WHERE c.user_id = ls.user_id AND c.kind = ls.kind) AS k "
        "  FROM ledger_state ls WHERE ls.user_id = $1 AND ls.title_id = $2",
        user_id,
        title_id,
    )
    if row is None or row["cdf"] is None or row["tier"] is None:
        coordinate = await _predicted_coordinate(
            conn, user_id=user_id, title_id=title_id, kind=kind, hp=hp, embeddings=embeddings
        )
        if coordinate is None:
            return None
        cdf, tier, levels = coordinate
    else:
        cdf, tier, levels = float(row["cdf"]), int(row["tier"]), row["k"]
    k = len(await observations.tier_set_of(conn, user_id=user_id, kind=kind))
    return Guess(tier=observations.rescale_level(tier, k_from=levels, k_to=k), cdf=cdf)


# --- §7.3's push, and its symmetric retraction ----------------------------------------------


async def _push_state(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    title_id: int,
) -> tuple[bool, str | None]:
    """Push the committed state to Jellyfin now; callers run this after their transaction closes.

    With no connector the §7.3 debt simply stands.
    """
    if jf is None or jf.client is None:
        # §6.7 prints this verbatim, so an unreadable DEK must not read as "not configured".
        if jf is not None and jf.cfg.secrets_unreadable:
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"
    return await seen.push_owed(conn, jf.client, jf.cfg, user_id=user_id, title_id=title_id)


def _state_entries(
    priors: Sequence[PriorState], pushed: dict[int, bool]
) -> list[dict[str, Any]]:
    """`rate_observation.prior_state`: what `user_title` held, plus whether we reached
    Jellyfin. Undo compensates what it did, not what it intended."""
    return [{**p.as_dict(), "pushed": bool(pushed.get(p.title_id, False))} for p in priors]


async def _mark_pushed(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    session_id: int,
    seq: int,
    entries: Sequence[dict[str, Any]],
) -> None:
    """Decision 207: correct the journal's `pushed` flags once the push has resolved.

    Best-effort and outside the transaction. If the row was undone mid-push the UPDATE matches
    nothing, and the tap makes the retraction the undo could not.
    """
    if not any(entry.get("pushed") for entry in entries):
        return
    try:
        marked = await conn.fetchval(
            "UPDATE rate_observation SET prior_state = $3::jsonb "
            "WHERE session_id = $1 AND seq = $2 AND undone_at IS NULL "
            "RETURNING id",
            session_id,
            seq,
            list(entries),
        )
    except asyncpg.PostgresError as exc:
        log.warning(
            "journal row %d/%d kept prior_state.pushed = false after a push that succeeded: %s",
            session_id,
            seq,
            exc,
        )
        return
    if marked is not None or jf is None:
        return
    try:
        for entry in entries:
            if not entry.get("pushed"):
                continue
            log.info(
                "observation %d/%d was undone while its Jellyfin push was in flight - "
                "retracting the Played flag for title %s",
                session_id,
                seq,
                entry["title_id"],
            )
            await seen.retract(
                conn,
                jf.client,
                jf.cfg,
                user_id=user_id,
                title_id=int(entry["title_id"]),
                prior_state=entry.get("state") if entry.get("existed") else None,
            )
    except asyncpg.PostgresError as exc:
        # Logged, not raised: the observation is durable and there is nothing to retry.
        log.warning(
            "observation %d/%d was undone mid-push and its Played flag could not be retracted: %s",
            session_id,
            seq,
            exc,
        )


# One tap's §7.3 settlement as a job over a connection, and the hook a caller passes to have it
# run after the response instead of inside it.
Settle = Callable[[asyncpg.Connection], Awaitable[Any]]
Later = Callable[[Settle], None]

_SETTLING: set[asyncio.Task[None]] = set()

# Unbounded, a slow Jellyfin let one phone's pushes take the whole web pool; two leave eight.
SETTLE_SLOTS = 2
# Bounded like a request's acquire; a settlement that gets none stays owed to §7.3's sweep.
SETTLE_ACQUIRE_TIMEOUT_S = 10

_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _settle_slots() -> asyncio.Semaphore:
    """The one semaphore for this event loop, made per loop because a semaphore binds to one."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(SETTLE_SLOTS))
    return _slots[1]


async def _push_and_mark(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    session_id: int,
    seq: int,
    priors: Sequence[PriorState],
    title_ids: Sequence[int],
) -> dict[int, tuple[bool, str | None]]:
    """Push each touched title's committed state, then correct the journal's `pushed` flags."""
    results: dict[int, tuple[bool, str | None]] = {}
    for title_id in title_ids:
        results[title_id] = await _push_state(conn, jf, user_id=user_id, title_id=title_id)
    pushed = {title_id: ok for title_id, (ok, _reason) in results.items()}
    await _mark_pushed(
        conn,
        jf,
        user_id=user_id,
        session_id=session_id,
        seq=seq,
        entries=_state_entries(priors, pushed),
    )
    return results


async def _push_and_narrate(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    state: str,
    event_kind: str,
    user_id: int,
    session_id: int,
    seq: int,
    priors: Sequence[PriorState],
    title_ids: Sequence[int],
) -> None:
    """A handed-off push, and §6.7's line for how it ended, recorded when it ends."""
    results = await _push_and_mark(
        conn, jf, user_id=user_id, session_id=session_id, seq=seq, priors=priors,
        title_ids=title_ids,
    )
    for title_id in title_ids:
        rail.record(kind=event_kind, line=_sync_line(state, *results[title_id]), user_id=user_id)


async def _settle_push(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    later: Later | None,
    *,
    state: str,
    event_kind: str,
    user_id: int,
    session_id: int,
    seq: int,
    priors: Sequence[PriorState],
    title_ids: Sequence[int],
) -> list[str]:
    """§7.3's push for one tap, now or after the response; returns §6.7's line per title.

    With `later` and a client, the push runs on its own connection after the response (a series
    Played write can take seconds); with no client, inline, since `_push_state` needs no query.
    """
    priors, title_ids = tuple(priors), tuple(title_ids)
    if later is not None and jf is not None and jf.client is not None:
        later(
            functools.partial(
                _push_and_narrate, jf=jf, state=state, event_kind=event_kind, user_id=user_id,
                session_id=session_id, seq=seq, priors=priors, title_ids=title_ids,
            )
        )
        return [_follows_line(state) for _ in title_ids]
    results = await _push_and_mark(
        conn, jf, user_id=user_id, session_id=session_id, seq=seq, priors=priors,
        title_ids=title_ids,
    )
    return [_sync_line(state, *results[title_id]) for title_id in title_ids]


def settle_in_background(job: Settle) -> None:
    """Run one tap's §7.3 settlement after the response, on a pooled connection of its own.

    No timeout of its own: a cancelled push may have reached Jellyfin while the journal says not.
    """
    task = asyncio.get_running_loop().create_task(_run_settlement(job))
    _SETTLING.add(task)
    task.add_done_callback(_SETTLING.discard)


async def _run_settlement(job: Settle) -> None:
    # Nothing may raise out of a bare task; the app-side write is durable either way.
    try:
        async with _settle_slots():
            connections = db_pool.pool()
            try:
                conn = await connections.acquire(timeout=SETTLE_ACQUIRE_TIMEOUT_S)
            except TimeoutError:
                log.warning(
                    "no pooled connection within %ss for a Rate answer's Jellyfin push (pool size"
                    " %d, idle %d); the seen-state sweep still owes it",
                    SETTLE_ACQUIRE_TIMEOUT_S,
                    connections.get_size(),
                    connections.get_idle_size(),
                )
                return
            try:
                await job(conn)
            finally:
                await connections.release(conn)
    except Exception:
        log.warning(
            "a Rate answer's Jellyfin push did not settle; the seen-state sweep still owes it",
            exc_info=True,
        )


async def settled(timeout: float | None = None) -> None:
    """Wait for every settlement handed off so far. For tests and shutdown, never a request.

    With `timeout`, stop waiting and cancel nothing (see `settle_in_background`).
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while _SETTLING:
        left = None if deadline is None else deadline - loop.time()
        if left is not None and left <= 0:
            return
        await asyncio.wait(list(_SETTLING), timeout=left)


def _sync_line(state: str, pushed: bool, reason: str | None) -> str:
    """§6.7's rail reports what actually happened, never a write that did not happen."""
    played = "true" if state == "seen" else "false"
    # Settled with a reason is a series stamped app-only (decision 533): nothing reached Jellyfin.
    if pushed and reason is None:
        return f"user_title.state = {state} -> Jellyfin Played {played}"
    return f"user_title.state = {state} -> not pushed ({reason or 'no connector'})"


def _follows_line(state: str) -> str:
    """The line for a push handed to `later`: it has not happened yet, so it says it follows."""
    return f"user_title.state = {state} -> Jellyfin push follows"


# --- the journal -----------------------------------------------------------------------------


async def _append(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    kind_of: Literal["placement", "not_seen"],
    card: dict[str, Any],
    title_ids: Sequence[int],
    tier_edit_id: int | None = None,
    verdict_id: int | None = None,
    superseded_verdict_id: int | None = None,
    prior_state: Sequence[dict[str, Any]] = (),
    latency_ms: int | None = None,
) -> RateSession:
    """One journal row, then the cursor moves (decision 35's observation journal).

    The INSERT and UPDATE are atomic here too: split, they leave the journal a row ahead and every
    later append refused.
    """
    block_index, slot = advance(s.block_index, s.slot)
    async with contextlib.AsyncExitStack() as stack:
        if not conn.is_in_transaction():
            await stack.enter_async_context(conn.transaction())
        try:
            await conn.execute(
                """
                INSERT INTO rate_observation
                    (session_id, user_id, seq, block_index, slot, kind_of, advances, card,
                     title_ids, tier_edit_id, verdict_id, superseded_verdict_id, prior_state,
                     latency_ms)
                VALUES ($1, $2, $3, $4, $5, $6, true, $7::jsonb, $8::int[], $9, $10, $11,
                        $12::jsonb, $13)
                """,
                s.id,
                s.user_id,
                s.seq + 1,
                s.block_index,
                s.slot,
                kind_of,
                card,
                list(title_ids),
                tier_edit_id,
                verdict_id,
                superseded_verdict_id,
                list(prior_state),
                latency_ms,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "rate_observation_seq":
                raise
            # Two taps on one card: §6.1's stale card, the 409 the client can act on.
            raise StaleCard("stale_card") from exc
        row = await conn.fetchrow(
            f"""
            UPDATE rate_session
               SET seq = $2, block_index = $3, slot = $4,
                   current_card = NULL, card_token = NULL, last_seen_at = now()
             WHERE id = $1
            RETURNING {_SESSION_COLUMNS}
            """,
            s.id,
            s.seq + 1,
            block_index,
            slot,
        )
    return _session(row)


async def _claim_card(conn: asyncpg.Connection, s: RateSession, card_token: str) -> None:
    """Take the card under a row lock, as the first statement of the write's transaction.

    Two taps on one token both pass `_take_card`'s snapshot check; the loser blocks here, re-reads
    a NULL token and leaves as §6.1's 409 before any write or push. Not taken on the GET.
    """
    row = await conn.fetchrow(
        "SELECT card_token FROM rate_session WHERE id = $1 FOR UPDATE", s.id
    )
    if row is None or str(row["card_token"]) != str(card_token):
        raise StaleCard("stale_card")


def _take_card(s: RateSession, token: str) -> dict[str, Any]:
    if s.current_card is None or s.card_token is None:
        raise StaleCard("no_card")
    if str(s.card_token) != str(token):
        raise StaleCard("stale_card")
    return s.current_card


async def _names(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> tuple[str, str]:
    row = await conn.fetchrow(
        "SELECT (SELECT name FROM app_user WHERE id = $1) AS rater,"
        " (SELECT name FROM title WHERE id = $2) AS title",
        user_id,
        title_id,
    )
    # Both exist by foreign key; the fallbacks cover a database that broke its constraints.
    return row["rater"] or "an unknown rater", row["title"] or "an unknown title"


def _placement_line(rater: str, title: str, label: str, *, refit_ms: float | None) -> str:
    """§6.7's `tier_edit(alex, Heat → A, via=explicit) → tier arm, incremental refit 31 ms`."""
    line = rail.tier_edit_line(title, label, via="explicit", rater=rater) + " → tier arm"
    if refit_ms is not None:
        line += f", incremental refit {refit_ms:.0f} ms"
    return line


# --- the two taps ----------------------------------------------------------------------------


async def record_placement(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    tier: int,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
    later: Later | None = None,
) -> Outcome:
    """§6.1's placement: a `tier_edit` (`via = 'explicit'`) that implies `seen` and records its
    tier's verdict (`ladder.place`); a re-ask card's edit names the edit it re-asks.

    Raises BadTier for a tier outside the person's set, before anything is written.
    `bundle_version` names the basis `embeddings` is in; `later` takes §7.3's push off the response.
    """
    card = _take_card(s, card_token)
    title_id, kind = int(card["title_id"]), str(card["kind"])
    tier_set = await observations.tier_set_of(conn, user_id=s.user_id, kind=kind)
    if not 0 <= tier < len(tier_set):
        raise BadTier(f"tier {tier} is outside the set of {len(tier_set)} steps")

    # Strictly first: the echo's guess is what the model believed *before* this placement existed.
    before = await guess(
        conn, user_id=s.user_id, title_id=title_id, kind=kind, hp=hp, embeddings=embeddings
    )

    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        placed = await ladder.place(
            conn, user_id=s.user_id, title_id=title_id, tier=tier, via="explicit",
            source="ladder", reask_of=card.get("reask_of"),
        )
        s = await _append(
            conn,
            s,
            kind_of="placement",
            card=card,
            title_ids=(title_id,),
            tier_edit_id=placed.tier_edit_id,
            verdict_id=placed.verdict_id,
            superseded_verdict_id=placed.superseded_verdict_id,
            # `pushed` is corrected by `_mark_pushed` once the push has happened.
            prior_state=_state_entries(placed.prior_state, {}),
            latency_ms=latency_ms,
        )

    # §7.3's push after the commit, where the placement made the title seen: the row then stands
    # as owed (`jf_synced_at` NULL).
    sync_lines: list[str] = []
    if placed.implied_seen:
        sync_lines = await _settle_push(
            conn, jf, later, state="seen", event_kind="tier_edit", user_id=s.user_id,
            session_id=s.id, seq=s.seq, priors=placed.prior_state, title_ids=(title_id,),
        )

    ledger = await refit.update_incrementally_reporting(
        conn,
        user_id=s.user_id,
        kind=placed.kind,
        title_ids=(title_id,),
        hp=hp,
        embeddings=embeddings,
        bundle_version=bundle_version,
    )
    rater, name = await _names(conn, user_id=s.user_id, title_id=title_id)
    line = RailLine(
        _placement_line(rater, name, placed.label, refit_ms=(ledger or {}).get("ms")),
        title_id=title_id,
    )
    echo: dict[str, Any] = {
        "title_id": title_id, "name": name, "tier": placed.tier, "word": placed.word,
    }
    if before is not None:
        echo["model"] = {
            "guess_word": observations.tier_words(tier_set)[before.tier],
            "cdf": round(before.cdf, 2),
        }
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, echo=echo, log=(line, *sync_lines), ledger=ledger)


async def record_not_seen(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
    later: Later | None = None,
) -> Outcome:
    """§6.1's `Not seen`: plain `unseen` (no third state); writes no observation row, deletes none,
    and a placed film keeps its step (decision 550)."""
    card = _take_card(s, card_token)
    title_id = int(card["title_id"])
    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        write = await observations.record_not_seen(conn, user_id=s.user_id, title_id=title_id)
        s = await _append(
            conn,
            s,
            kind_of="not_seen",
            card=card,
            title_ids=write.title_ids,
            prior_state=_state_entries(write.prior_state, {}),
            latency_ms=latency_ms,
        )
    sync_lines = await _settle_push(
        conn, jf, later, state="unseen", event_kind="not_seen", user_id=s.user_id,
        session_id=s.id, seq=s.seq, priors=write.prior_state, title_ids=(title_id,),
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, log=(write.log, *sync_lines))


# --- undo ------------------------------------------------------------------------------------


async def _block_started(conn: asyncpg.Connection, s: RateSession) -> bool:
    """Has the current block ever held a journal row, tombstones included (decision 199)?"""
    started = await conn.fetchval(
        "SELECT 1 FROM rate_observation WHERE session_id = $1 AND block_index = $2 LIMIT 1",
        s.id,
        s.block_index,
    )
    return started is not None


async def _undo_reaches(
    conn: asyncpg.Connection, s: RateSession, *, block_index: int, slot: int
) -> bool:
    """Decision 35's depth with decision 199's boundary: is this journal row still undoable?

    The fifteenth row of the previous block stays reachable at slot 1 until the new block has
    ever held a row; otherwise undo would walk back block by block.
    """
    if block_index == s.block_index:
        return True
    if not (s.slot == 1 and block_index == s.block_index - 1 and slot == BLOCK_SIZE):
        return False
    return not await _block_started(conn, s)


async def undo_availability(conn: asyncpg.Connection, s: RateSession) -> dict[str, Any]:
    """§6.1: Undo is always drawn, dimmed with nothing to undo, and names what it takes back."""
    row = await conn.fetchrow(
        "SELECT o.kind_of, o.block_index, o.slot, t.name FROM rate_observation o "
        "LEFT JOIN title t ON t.id = o.title_ids[1] "
        "WHERE o.session_id = $1 AND o.undone_at IS NULL ORDER BY o.seq DESC LIMIT 1",
        s.id,
    )
    if row is None or not await _undo_reaches(
        conn, s, block_index=row["block_index"], slot=row["slot"]
    ):
        return {"available": False, "kind": None, "name": None}
    return {"available": True, "kind": row["kind_of"], "name": row["name"]}


async def undo(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    jf: Jellyfin | None = None,
) -> Outcome:
    """Take back the last placement or Not seen in the block and put its card back (decision 35):
    the exact card (`rate_observation.card`), one block (`_undo_reaches`). Compared on the row's
    block and slot, not a timestamp: the journal row lands after the ledger's.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id, kind_of, block_index, slot, card, title_ids, tier_edit_id, verdict_id,
                   prior_state
              FROM rate_observation
             WHERE session_id = $1 AND undone_at IS NULL
             ORDER BY seq DESC LIMIT 1
             FOR UPDATE
            """,
            s.id,
        )
        if row is None:
            raise UndoUnavailable("empty")
        if not await _undo_reaches(conn, s, block_index=row["block_index"], slot=row["slot"]):
            raise UndoUnavailable("block_boundary")

        priors = [PriorState.from_dict(p) for p in (row["prior_state"] or [])]
        pushed = {int(p["title_id"]): bool(p.get("pushed")) for p in (row["prior_state"] or [])}
        kind_of = row["kind_of"]
        title_ids = list(row["title_ids"])
        if kind_of == "placement":
            # With `observations.undo` under it, the only code permitted to delete these rows (§4.2).
            undone = await ladder.undo_placement(
                conn, user_id=s.user_id, tier_edit_id=row["tier_edit_id"],
                verdict_id=row["verdict_id"], prior_state=priors,
            )
        else:
            undone = await observations.undo(
                conn, user_id=s.user_id, arm="not_seen", title_ids=title_ids, prior_state=priors
            )
        await conn.execute(
            "UPDATE rate_observation SET undone_at = now() WHERE id = $1", row["id"]
        )
        restored = await conn.fetchrow(
            f"""
            UPDATE rate_session
               SET block_index = $2, slot = $3, current_card = $4::jsonb, card_token = $5,
                   last_seen_at = now()
             WHERE id = $1
            RETURNING {_SESSION_COLUMNS}
            """,
            s.id,
            row["block_index"],
            row["slot"],
            dict(row["card"]),
            uuid.uuid4(),
        )
        s = _session(restored)

    for prior in priors:
        if pushed.get(prior.title_id) and jf is not None:
            await seen.retract(
                conn,
                jf.client,
                jf.cfg,
                user_id=s.user_id,
                title_id=prior.title_id,
                prior_state=prior.state if prior.existed else None,
            )

    ledger = None
    if kind_of == "placement":
        ledger = await refit.update_incrementally_reporting(
            conn,
            user_id=s.user_id,
            kind=undone.kind,
            title_ids=title_ids,
            hp=hp,
            embeddings=embeddings,
            bundle_version=bundle_version,
        )
    return Outcome(session=s, log=(undone.log,), ledger=ledger, undone=kind_of)


# --- the payload -------------------------------------------------------------------------------


async def _done(conn: asyncpg.Connection, s: RateSession) -> dict[str, Any] | None:
    """The block's end (§6.1): from its fifteenth answer until the next block's first."""
    if s.slot != 1 or s.block_index == 0 or await _block_started(conn, s):
        return None
    waiting = await ladder.rated_before(conn, user_id=s.user_id, kinds=s.kinds)
    return {"rated_before": len(waiting), "noun": "films" if s.kind == "movie" else "series"}


def _closed(state: ladder.LadderState) -> dict[str, Any]:
    """Rate before the person's set-up (decision 550): one card, and nothing else."""
    return {
        "setup": {"done": False, "earlier_ratings": state.earlier_ratings, "rated_before": 0},
        "session": None,
        "card": None,
        "preload": [],
        "echo": None,
        "done": None,
        "drained": None,
        "undo": {"available": False, "kind": None, "name": None},
    }


async def payload(
    conn: asyncpg.Connection,
    s: RateSession | None,
    *,
    echo: dict[str, Any] | None = None,
    log: Sequence[str] = (),
    ledger: dict[str, Any] | None = None,
    event_kind: str | None = None,
    user: Any = None,
) -> dict[str, Any]:
    """One envelope for every route; the next card travels in the response to the write (§6).

    `s` is None for a person who has no session because they have not set up their ladder.
    """
    user_id = s.user_id if s is not None else user.id
    # §6.7's rail narrates model writes only, so a read passes no `event_kind`.
    if event_kind is not None:
        for line in log:
            rail.record(
                kind=event_kind,
                line=line,
                user_id=user_id,
                title_id=getattr(line, "title_id", None),
            )

    state = await ladder.state(conn, user_id=user_id)
    if s is None or not state.done:
        return _closed(state)
    version = await dna_terms.active_version(conn)
    card = await public_card(conn, s, version=version)
    body = {
        "setup": {
            "done": True,
            "earlier_ratings": state.earlier_ratings,
            "rated_before": state.rated_before,
        },
        "session": {
            "kinds": s.kinds,
            "kind": s.kind,
            # §6.1's counter, and the unit decision 35's Undo depth is measured in.
            "block": {"slot": s.slot, "size": BLOCK_SIZE, "counter": f"{s.slot} of {BLOCK_SIZE}"},
        },
        "card": card,
        "preload": await _preload(conn, s, version=version) if card else [],
        "echo": echo,
        "done": await _done(conn, s),
        "drained": None if card else {"line": DRAINED_LINE},
        "undo": await undo_availability(conn, s),
        "ledger": ledger,
        # §6.7's rail, also carried here so the client shows this tap's line without a request.
        "log": list(log),
    }
    # Decision 117: with the toggle off the numbers are absent, not hidden; same gate as Home.
    return rail.redact(body, show_model=rail.visible_to(user)) if user is not None else body


__all__ = [
    "BLOCK_SIZE",
    "BadTier",
    "DRAINED_LINE",
    "Guess",
    "Jellyfin",
    "Outcome",
    "RateSession",
    "StaleCard",
    "UndoUnavailable",
    "advance",
    "end_session",
    "ensure_card",
    "guess",
    "one_kind",
    "open_or_resume",
    "payload",
    "public_card",
    "record_not_seen",
    "record_placement",
    "set_kinds",
    "settle_in_background",
    "settled",
    "stash",
    "undo",
    "undo_availability",
]
