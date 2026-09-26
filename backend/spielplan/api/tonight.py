"""Tonight routes (§6.2); the rules live in `spielplan.tonight`. An answer names a sealed pair, single-use
per answer count, so §13's arm is never client-chosen; nothing before the reveal carries the pool;
the ballot's blindness is `ballot.tally`'s; a guest seat is writable only by its session's host."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Coroutine
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser, ActiveUserWS
from spielplan.core import auth
from spielplan.core.config import settings
from spielplan.db import pool as db_pool
from spielplan.home import rail
from spielplan.models import artifacts
from spielplan.tonight import ballot as ballot_rules
from spielplan.tonight import channel as channel_rules
from spielplan.tonight import evaluation as evaluation_rules
from spielplan.tonight import play, rooms
from spielplan.tonight import result as result_rules
from spielplan.tonight import round as round_rules
from spielplan.tonight import solo as solo_rules

log = logging.getLogger("spielplan.api.tonight")

router = APIRouter(prefix="/api/tonight", tags=["tonight"])

# Its own salt: a Tonight pair is never a session cookie or a Rank pair.
_PAIR_SALT = "spielplan/tonight/pair/v1"

# For the room code and solo's hold-out pair only. A group round's draw is seeded from the pool's
# frozen nonce, so no client can re-roll it (decision 223).
_rng = random.SystemRandom()

HUB = channel_rules.Hub()

# Strong references: asyncio holds tasks weakly, so an unreferenced invite could be collected mid-flight.
_INVITES: set[asyncio.Task[None]] = set()

# Seconds one invitation may hold a pooled connection: per-device sends at 10 s each could drain
# the pool, and push is best-effort with an in-app equivalent (§6).
_INVITE_TIMEOUT_S = 3.0

# Hub frames in flight, kept apart so a test joining one does not wait on the other.
_FRAMES: set[asyncio.Task[int]] = set()


def _nudge(delivery: Coroutine[Any, Any, int]) -> None:
    """Fan-out off the request: a locked phone costs `SEND_TIMEOUT` per device, against §6.2's 1.5 s.
    Callers pass a finished frame, since the request's connection is released with the response."""
    task = asyncio.create_task(delivery)
    _FRAMES.add(task)
    task.add_done_callback(_FRAMES.discard)

Kind = Literal["movie", "series"]


def _sealer() -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret, _PAIR_SALT)


def _seal(participant_id: int, pair: round_rules.Pair, seq: int) -> str:
    return _sealer().dumps(
        {"p": participant_id, "a": pair.title_a, "b": pair.title_b,
         "s": pair.selection, "n": seq}
    )


def _unseal(token: str, *, participant_id: int) -> tuple[round_rules.Pair, int]:
    try:
        payload = _sealer().loads(token)
    except BadSignature as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "stale_pair", "message": "that pair is no longer on the table"},
        ) from exc
    if payload.get("p") != participant_id:
        # The seal proves the server drew it; the id proves which seat.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that pair belongs to another seat")
    return (
        round_rules.Pair(
            title_a=payload["a"], title_b=payload["b"], selection=payload["s"], reason=""
        ),
        int(payload["n"]),
    )


async def _bundle_version(conn: asyncpg.Connection) -> str:
    """The active bundle, or a 409: a Tonight round cannot rank without a basis (§3.1)."""
    version = await artifacts.active_bundle_version(conn)
    if version is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "no_bundle",
                    "message": "no artifact bundle is active — Tonight needs one to rank"},
        )
    return version


def _room_error(exc: rooms.RoomError | play.RoundError | ballot_rules.BallotError) -> HTTPException:
    """One mapping of refusals to status codes, shared by every caller."""
    codes = {
        "no_room": status.HTTP_404_NOT_FOUND,
        "no_seat": status.HTTP_404_NOT_FOUND,
        "no_slate": status.HTTP_404_NOT_FOUND,
        "bad_answer": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "guest_count": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "not_on_slate": status.HTTP_422_UNPROCESSABLE_ENTITY,
        # Decision 480: a bad veto is malformed; an unseated caller is not entitled.
        "bad_veto": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "not_seated": status.HTTP_403_FORBIDDEN,
        "not_your_turn": status.HTTP_409_CONFLICT,
        "too_early": status.HTTP_409_CONFLICT,
        # A member the nightly fit has not reached cannot be ranked yet (decision 216).
        "unscored_member": status.HTTP_409_CONFLICT,
    }
    return HTTPException(
        codes.get(exc.reason, status.HTTP_409_CONFLICT),
        detail={"reason": exc.reason, "message": str(exc)},
    )


async def _seat_for(
    conn: asyncpg.Connection, participant_id: int, user: auth.SessionUser
) -> asyncpg.Record:
    """Their own seat, or a guest seat in a session they host (§6.2's hand-the-phone); anything else
    casts another member's vote."""
    row = await conn.fetchrow(
        """
        SELECT p.id, p.user_id, p.role, p.session_id, s.host_user_id
          FROM session_participant p JOIN session s ON s.id = p.session_id
         WHERE p.id = $1
        """,
        participant_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such seat")
    if row["user_id"] == user.id:
        return row
    if row["role"] == rooms.ROLE_GUEST and row["host_user_id"] == user.id:
        return row
    raise HTTPException(status.HTTP_403_FORBIDDEN, "that seat belongs to someone else")


class OpenBody(BaseModel):
    """§6.2 step 1's controls plus guests; bounds mirror 0013's CHECKs, so a bad request is a 422."""

    kind: Kind = "movie"
    runtime_budget_min: int = Field(default=130, ge=60, le=200)
    include_rewatches: bool = False
    guests: int = Field(default=0, ge=0, le=rooms.MAX_GUESTS)


class JoinBody(BaseModel):
    session_id: int | None = None
    room_code: str | None = Field(default=None, max_length=16)


class AnswerBody(BaseModel):
    card_token: str
    answer: Literal["A", "B", "EITHER", "NEITHER"]
    latency_ms: int | None = None


class BallotBody(BaseModel):
    approved: list[int] = Field(default_factory=list, max_length=8)


class VetoBody(BaseModel):
    """Decision 480's vetoes as this member's whole set (decision 505): a replace, never a toggle."""

    vetoes: list[str] = Field(default_factory=list, max_length=8)


class SoloAnswer(BaseModel):
    """A shape, so a malformed entry is a 422. No `selection`: the arm is re-derived server-side (54b).
    `seq` falls back to the position."""

    title_a: int
    title_b: int
    answer: Literal["A", "B", "EITHER", "NEITHER"]
    seq: int | None = None


class SoloBody(BaseModel):
    kind: Kind = "movie"
    runtime_budget_min: int = Field(default=130, ge=60, le=200)
    include_rewatches: bool = False
    offset: int = Field(default=0, ge=0, le=64)
    # 54f: only the "sharpen this" tap draws a pair; the expensive answer must be asked for.
    sharpen: bool = False
    # Stateless (§6.2 step 8 forbids a row), so the client carries its answers.
    answers: list[SoloAnswer] = Field(default_factory=list, max_length=64)


@router.get("/rooms")
async def open_rooms(user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 2: visible to every household device, not only the host's."""
    return {"rooms": await rooms.open_rooms(conn, viewer_id=user.id)}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def open_session(body: OpenBody, user: ActiveUser, conn: DB) -> dict[str, object]:
    """The push invitation runs after the commit, off the request, bounded by `_INVITE_TIMEOUT_S` (§6)."""
    version = await _bundle_version(conn)
    room = await rooms.open_session(
        conn, host_user_id=user.id, kind=body.kind,
        budget_min=body.runtime_budget_min, include_rewatches=body.include_rewatches,
        bundle_version=version, guests=body.guests, rng=_rng,
    )
    _nudge(HUB.to_household(channel_rules.rooms_changed()))
    task = asyncio.create_task(
        _invite(session_id=room["session_id"], host_user_id=user.id,
                room_code=room["room_code"])
    )
    _INVITES.add(task)
    task.add_done_callback(_INVITES.discard)
    return {**room, "lobby": await rooms.lobby(conn, room["session_id"])}


async def _invite(*, session_id: int, host_user_id: int, room_code: str) -> None:
    """Its own connection, never the request's, which is released with the response. The sender is
    optional (§3.1's half-configured boot). Never raises: failures are logged."""
    try:
        from spielplan.push import send as push_send
    except Exception:  # pragma: no cover - the sender is absent only in a partial checkout
        return
    try:
        async with db_pool.acquire() as conn:
            await asyncio.wait_for(
                rooms.invite(
                    conn, push_send.send_to_user, session_id=session_id,
                    host_user_id=host_user_id, room_code=room_code,
                ),
                timeout=_INVITE_TIMEOUT_S,
            )
    except TimeoutError:
        # Its own branch: a timeout means hanging endpoints, not a failure to start.
        log.warning(
            "the tonight invitation for session %s did not finish within %ss and was dropped so "
            "the connection could be released", session_id, _INVITE_TIMEOUT_S,
        )
    except Exception:
        log.warning(
            "the tonight invitation for session %s was not dispatched", session_id, exc_info=True
        )


@router.post("/sessions/join")
async def join(body: JoinBody, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 2's equivalent join channels, as one route; a second arrival re-attaches its seat."""
    try:
        session_id = (
            body.session_id if body.session_id is not None
            else await rooms.resolve_code(conn, body.room_code or "")
        )
        seat = await rooms.join(conn, session_id=session_id, user_id=user.id)
    except rooms.RoomError as exc:
        raise _room_error(exc) from exc
    lobby = await rooms.lobby(conn, session_id)
    _nudge(HUB.to_session(session_id, channel_rules.lobby_frame(lobby)))
    return {"session_id": session_id, **seat, "lobby": lobby}


@router.get("/sessions/{session_id}")
async def lobby(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """Everything a device renders before the reveal, never the pool. Settled first: every device polls
    this, so a stalled room is picked up here (see `play.settle`)."""
    try:
        await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z)
        seen = await rooms.lobby(conn, session_id)
    except (rooms.RoomError, play.RoundError) as exc:
        # A domain refusal from `settle` maps here; a failed combine reaches `app.py` as a 500.
        raise _room_error(exc) from exc
    submitted, seated = await ballot_rules.submitted_count(conn, session_id)
    mine = next(
        (s for s in seen["seats"] if s["user_id"] == user.id), None
    )
    return {
        **seen,
        "progress": await play.progress(conn, session_id),
        "ballot": {"submitted": submitted, "seated": seated,
                   "revealed": await ballot_rules.everyone_submitted(conn, session_id)},
        "me": mine,
    }


@router.post("/sessions/{session_id}/vetoes")
async def set_vetoes(
    session_id: int, body: VetoBody, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """Decision 480's lobby control (up to three each, decision 505). Pushes the lobby to the room and
    the row to the household, whose open-rooms list shows vetoes."""
    try:
        await rooms.set_vetoes(conn, session_id=session_id, user_id=user.id, keys=body.vetoes)
        lobby = await rooms.lobby(conn, session_id)
    except rooms.RoomError as exc:
        raise _room_error(exc) from exc
    _nudge(HUB.to_session(session_id, channel_rules.lobby_frame(lobby)))
    _nudge(HUB.to_household(channel_rules.rooms_changed()))
    return {"session_id": session_id, "vetoes": lobby["vetoes"], "seats": lobby["seats"]}


@router.post("/sessions/{session_id}/start")
async def start(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """The host closes the join window (§6.2 step 2)."""
    host = await conn.fetchval("SELECT host_user_id FROM session WHERE id = $1", session_id)
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such session")
    if host != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the host starts the round")
    try:
        await play.start(conn, session_id)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    lobby = await rooms.lobby(conn, session_id)
    _nudge(HUB.to_session(session_id, channel_rules.lobby_frame(lobby)))
    _nudge(HUB.to_household(channel_rules.rooms_changed()))
    return {"session_id": session_id, "state": lobby["state"]}


@router.post("/sessions/{session_id}/end")
async def end_room(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """Decision 169: the host ends the evening. Both frames go out, since the room's devices and the
    household's open-rooms list are both stale otherwise."""
    host = await conn.fetchval("SELECT host_user_id FROM session WHERE id = $1", session_id)
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such session")
    if host != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the host ends the room")
    try:
        await rooms.end_session(conn, session_id)
    except rooms.RoomError as exc:
        raise _room_error(exc) from exc
    lobby = await rooms.lobby(conn, session_id)
    _nudge(HUB.to_session(session_id, channel_rules.lobby_frame(lobby)))
    _nudge(HUB.to_household(channel_rules.rooms_changed()))
    return {"session_id": session_id, "state": lobby["state"]}


def _public_state(state: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Built from named fields, never by filtering the domain object, so a new field cannot leak."""
    pair = state["pair"]
    return {
        "participant_id": state["participant_id"],
        "answered": state["answered"],
        "cap": state["cap"],
        "typical": state["typical"],
        "ended_by": state["ended_by"],
        "stop_reason": state["stop_reason"],
        "escape_available": state["escape_available"],
        "card_token": token,
        "pair": None if pair is None else {
            "a": pair["a"], "b": pair["b"],
            # The held-out arm is identifiable end to end (54b): sent, never accepted.
            "selection": pair["selection"],
            "reason": pair["reason"],
        },
    }


# `z` is always `round_rules.BOUNDARY_Z`, never §6.3's `straddle_z`: different score scales
# (decisions 175, 205, 214).


@router.get("/seats/{participant_id}/round")
async def round_state(
    participant_id: int, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """When serving the card ended the seat (a tiny pool, decision 215), this read settles and
    announces: the client reads the round after the session, so nothing else would wake the room."""
    seat = await _seat_for(conn, participant_id, user)
    try:
        state = await play.state_for(conn, participant_id, z=round_rules.BOUNDARY_Z)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    if state["_ended_now"]:
        await _announce(conn, seat["session_id"])
    token = (
        None if state["_pair"] is None
        else _seal(participant_id, state["_pair"], state["answered"] + 1)
    )
    return _public_state(state, token)


@router.post("/seats/{participant_id}/answer")
async def answer(
    participant_id: int, body: AnswerBody, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """One answer, then the next card (§6 preamble)."""
    seat = await _seat_for(conn, participant_id, user)
    pair, seq = _unseal(body.card_token, participant_id=participant_id)
    try:
        written = await play.record_answer(
            conn, participant_id=participant_id, pair=pair, answer=body.answer,
            seq=seq, latency_ms=body.latency_ms, z=round_rules.BOUNDARY_Z,
        )
    except play.RoundError as exc:
        raise _room_error(exc) from exc

    # §6.7's rail, under the ANSWERING user's id: a line filed to another account would leak a blind answer.
    rail.record(
        user_id=user.id,
        kind="session_answer",
        line=rail.session_answer_line(str(seat["id"]), seq, body.answer),
        title_id=pair.title_a,
        detail={"selection": pair.selection, "session_id": seat["session_id"]},
    )
    await _announce(conn, seat["session_id"])
    # The card `record_answer` already drew, under the same seal a reload would use: no second replay.
    token = (
        None if written["_pair"] is None
        else _seal(participant_id, written["_pair"], written["answered"] + 1)
    )
    payload = {
        **_public_state(written, token),
        "wrote": {"seq": written["seq"], "stop_reason": written["stop_reason"]},
    }
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.post("/seats/{participant_id}/undo")
async def undo(
    participant_id: int, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """§6 preamble's undo, where a mis-tap is otherwise permanent."""
    seat = await _seat_for(conn, participant_id, user)
    try:
        out = await play.retract(conn, participant_id, z=round_rules.BOUNDARY_Z)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    await _announce(conn, seat["session_id"])
    token = (
        None if out["_pair"] is None
        else _seal(participant_id, out["_pair"], out["answered"] + 1)
    )
    return {**_public_state(out, token), "retracted_seq": out["retracted_seq"]}


@router.post("/seats/{participant_id}/escape")
async def escape(
    participant_id: int, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """54c's "just pick for us"."""
    seat = await _seat_for(conn, participant_id, user)
    try:
        out = await play.escape(conn, participant_id)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    await _announce(conn, seat["session_id"])
    # No token: a seat that has just ended has no next pair.
    return _public_state(out, None)


async def _announce(conn: asyncpg.Connection, session_id: int) -> None:
    """Progress frames, plus a lobby frame only when `play.settle` moved the room. The settle is awaited
    (a write owed to the room); the frames are nudged."""
    progress = channel_rules.progress_frame(session_id, await play.progress(conn, session_id))
    _nudge(HUB.to_session(session_id, progress))
    if await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z):
        lobby = channel_rules.lobby_frame(await rooms.lobby(conn, session_id))
        _nudge(HUB.to_session(session_id, lobby))


@router.get("/sessions/{session_id}/ballot")
async def ballot_card(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """54e's ballot, nothing about anybody's vote. Settled first, so a room whose votes are in never
    shows an empty slate."""
    try:
        await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    submitted, seated = await ballot_rules.submitted_count(conn, session_id)
    slate = await ballot_rules.slate_of(conn, session_id)
    return {
        "session_id": session_id,
        "slate": [
            {"title_id": r["title_id"], "slot": r["slot"], "name": r["name"],
             "year": r["year"], "runtime_min": r["runtime_min"],
             "poster_path": r["poster_path"]}
            for r in slate
        ],
        "submitted": submitted,
        "seated": seated,
        "revealed": await ballot_rules.everyone_submitted(conn, session_id),
    }


@router.post("/seats/{participant_id}/ballot")
async def submit_ballot(
    participant_id: int, body: BallotBody, user: ActiveUser, conn: DB
) -> dict[str, object]:
    seat = await _seat_for(conn, participant_id, user)
    try:
        await ballot_rules.submit(conn, participant_id=participant_id, approved=body.approved)
    except ballot_rules.BallotError as exc:
        raise _room_error(exc) from exc
    session_id = seat["session_id"]
    submitted, seated = await ballot_rules.submitted_count(conn, session_id)
    revealed = await ballot_rules.everyone_submitted(conn, session_id)
    if revealed:
        await ballot_rules.resolve(conn, session_id)
        # Nudged, not awaited: every device fetches the result over REST anyway.
        _nudge(HUB.to_session(session_id, channel_rules.reveal_frame(session_id)))
        _nudge(HUB.to_household(channel_rules.rooms_changed()))
    else:
        # The ballot's own count: two integers and no title (54e).
        _nudge(HUB.to_session(
            session_id, channel_rules.ballot_frame(session_id, submitted=submitted, seated=seated)
        ))
    return {"submitted": submitted, "seated": seated, "revealed": revealed}


@router.get("/sessions/{session_id}/result")
async def result(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 7's winner card: settled first, then refused by `ballot.tally` until every seat has
    submitted. Only §7.1's deep link is HTTP's; the card is `tonight/result.slate`."""
    try:
        await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z)
        counted = await ballot_rules.tally(conn, session_id)
        outcome = await ballot_rules.resolve(conn, session_id)
    except (ballot_rules.BallotError, play.RoundError) as exc:
        raise _room_error(exc) from exc

    jf = await conn.fetchval("SELECT config FROM connector_config WHERE name = 'jellyfin'")
    base = (jf or {}).get("url", "") if isinstance(jf, dict) else ""

    def play_url(jellyfin_id: str) -> str:
        """§7.1's deep link."""
        return f"{base.rstrip('/')}/web/#/details?id={jellyfin_id}"

    return await result_rules.slate(
        conn, session_id, counted, outcome,
        # None without a connector (§6.0): absent rather than guessed.
        play_url=play_url if base else None,
        # Decision 117's gate, asked where the payload is built (decision 486).
        show_model=rail.visible_to(user),
    )


@router.get("/sessions/{session_id}/evaluation")
async def evaluation(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§13's instrument and §14 risk 6's rates: the held-out stream only, naming no candidate."""
    return await evaluation_rules.report(conn, session_id)


@router.post("/solo")
async def solo(
    body: SoloBody, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """54f: solo lands on three picks and a wildcard. No session row, so the sharpen answers travel
    with the request."""
    version = await _bundle_version(conn)
    # One key for both the re-derivation and `picks`, stable across requests and server-side (54b).
    holdout_key = str(user.id)
    # A loop, so the seq fallback is spelled once. Candidacy is the domain's to enforce, not this route's.
    answers = []
    for i, a in enumerate(body.answers):
        seq = a.seq if a.seq is not None else i + 1
        answers.append(round_rules.Answered(
            seq=seq, title_a=a.title_a, title_b=a.title_b, answer=a.answer,
            # Re-derived, never accepted (54b).
            selection=(
                round_rules.SELECTION_HOLDOUT
                if round_rules.is_holdout(seq, key=holdout_key)
                else round_rules.SELECTION_ADAPTIVE
            ),
        ))
    return await solo_rules.picks(
        conn, user_id=user.id, kind=body.kind, budget_min=body.runtime_budget_min,
        include_rewatches=body.include_rewatches, bundle_version=version,
        answers=answers, offset=body.offset, sharpen=body.sharpen,
        z=round_rules.BOUNDARY_Z, rng=_rng, holdout_key=holdout_key,
    )


@router.websocket("/channel")
async def channel(socket: WebSocket, user: ActiveUserWS, session_id: int | None = None) -> None:
    """Behind `ActiveUserWS`, so the gating sweeps see it (decision 225). Acquires for itself and
    releases before sending: a `deps.db` connection would be held all evening. The acquire and the
    sends share the hub's SEND_TIMEOUT; a socket that misses it is closed to reconnect."""
    await socket.accept()
    sub = HUB.subscribe(socket, user_id=user.id, session_id=session_id)
    try:
        # The current picture first, built inside the acquire and sent after it.
        async with db_pool.pool().acquire(timeout=channel_rules.SEND_TIMEOUT) as conn:
            opening = [
                channel_rules.rooms_changed(await rooms.open_rooms(conn, viewer_id=user.id))
            ]
            if session_id is not None:
                opening.append(
                    channel_rules.progress_frame(session_id, await play.progress(conn, session_id))
                )
        for frame in opening:
            await asyncio.wait_for(
                socket.send_json(channel_rules.wire(frame)),
                timeout=channel_rules.SEND_TIMEOUT,
            )
        while True:
            # One-way: writes go through REST, where the seat check lives. Reading detects the close.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        # A saturated pool or a client not reading: either way, close so it reconnects.
        log.info(
            "a tonight socket did not get its opening frames within %ss (a saturated pool, or a "
            "client that is not reading); closing it to let it reconnect",
            channel_rules.SEND_TIMEOUT,
        )
        await channel_rules.close_quietly(socket)
    finally:
        HUB.unsubscribe(sub)


Router = Annotated[APIRouter, None]
