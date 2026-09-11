"""The Tonight surface's routes. Spec v2.1 §6.2 (rewritten, 54a-54g), §6.7, §11, §13; decision 117.

Thin, like `api/rank.py` and `api/rate.py`: every rule lives in `spielplan.tonight` and the only
things decided here are the HTTP shapes. Four of them are load-bearing.

**AN ANSWER NAMES A SEALED PAIR, NEVER TWO TITLE IDS.** The same property `api/rank.py` gives a
queue pair, and for a sharper reason: 54b makes `session_answer.selection` the discriminator
§13's evaluation depends on, so a route accepting `{"title_a": 4, "title_b": 9, "selection":
"adaptive"}` would let a client file its own answer into or out of the held-out stream. The pair
is drawn on the server, sealed with `itsdangerous` under `SESSION_SECRET`, and handed back
opaque. **And the seal is single-use per live answer count**, because that is what it carries:
answering moves the counter, so a replay is a stale card and gets a 409. §13's figures count *rows*
and §4.2's tables are append-only, so a replay that landed could not be taken back. An undo lowers
the count and therefore re-opens that seq — with the same card, not a new one, because the draw is
sealed against (seat, count) by a nonce frozen with the pool (`play._round_of`), so the re-issued
token is byte-identical to any the phone still has. A stashed token is the live card or nothing.
[decision 223; M4.12 findings 28 and 29]

**NOTHING BEFORE THE REVEAL CARRIES THE POOL.** §6.2 step 3: the candidate pool is "internal —
never shown as a step". The v2.1 redesign deleted the visible shortlist because a pool rendered
before the votes anchors the votes it exists to collect — the same anchoring §6.1 forbids by
withholding the prediction until after the tap. So every payload below is assembled from named
fields rather than by serialising a domain object, and `_no_pool` is asserted in the tests
against every pre-reveal route.

**THE BALLOT'S BLINDNESS IS NOT ENFORCED HERE.** It is enforced in `ballot.tally`, which refuses
until every seat has submitted. This module simply calls it. That is deliberate: the session
WebSocket is a second caller and any later reader is a third, and a guard living in one route
is a guard its other callers can forget. (It named the TV route as the second caller until
decision 165 retired that surface; the argument survives it, which is why the rule stayed.)

**A GUEST TURN IS A ROUTE THE HOST'S SESSION MAY CALL.** §6.2 step 2's hand-the-phone means one
signed-in cookie speaks for several participants, so a participant id is a parameter — and every
write checks that the caller is entitled to that seat: their own, or a guest seat in a session
they host. Without that check a `participant_id` in a URL is a way to cast somebody else's vote.
"""

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
from spielplan.tonight import ballot as ballot_rules
from spielplan.tonight import channel as channel_rules
from spielplan.tonight import evaluation as evaluation_rules
from spielplan.tonight import play, rooms
from spielplan.tonight import result as result_rules
from spielplan.tonight import round as round_rules
from spielplan.tonight import solo as solo_rules

log = logging.getLogger("spielplan.api.tonight")

router = APIRouter(prefix="/api/tonight", tags=["tonight"])

# A salt of its own, so a sealed Tonight pair can never be presented as a session cookie or as a
# Rank queue pair. Rotating SESSION_SECRET invalidates all three, which is §2's stated behaviour.
_PAIR_SALT = "spielplan/tonight/pair/v1"

# Entropy for the two draws that are nobody's to reproduce: §6.2 step 2's room code, and solo's
# own hold-out pair, whose answers live in the request and leave no row behind. A group round's
# draw is NOT one of them any more — it is seeded from the nonce frozen with the pool, because a
# pair redrawn per request is a pair the client can choose (decision 223; finding 28).
_rng = random.SystemRandom()

HUB = channel_rules.Hub()

# The push invitations in flight. A fire-and-forget task needs a strong reference or it is
# collectable mid-flight — asyncio keeps only a weak one — so a dropped handle would turn §6's
# "best-effort" into "sometimes". Entries are discarded on completion, so this is the set of
# dispatches running now rather than a log of every room ever opened. [M4.12 finding 42]
_INVITES: set[asyncio.Task[None]] = set()

# And how long one of them may hold a pooled connection. The plan's own alternative for finding 42,
# taken here because the connection is the thing that has to be bounded: `rooms.invite` loops over
# invited members awaiting one send each, and `push/send.py` gives every DEVICE a 10 s httpx
# timeout, so a household whose endpoints hang costs (members x phones) x 10 s of ONE of the pool's
# ten connections per room opened — and `_deliver` writes (it DELETEs a gone subscription), so the
# connection genuinely has to be in hand for the whole loop. Measured: ten openings against hanging
# endpoints took the pool to idle 0, and `GET /api/auth/me` then 503'd after its own ten seconds.
# Three seconds because §6's preamble makes push best-effort with an in-app equivalent for every
# prompt — the room code, the open-rooms list and the lobby banner are all already there — so a
# dispatch that has not landed in three is one the household is not waiting on.
# [M4.12 finding 42; M4.12 review cycle 2: M412-API2-02]
_INVITE_TIMEOUT_S = 3.0

# The hub frames in flight, held for the same reason and kept separate so a test that joins one is
# not waiting on the other. [M4.12 finding 17]
_FRAMES: set[asyncio.Task[int]] = set()


def _nudge(delivery: Coroutine[Any, Any, int]) -> None:
    """Hand a hub fan-out to the loop instead of putting it in a household's request.

    §6.2's own budget is the argument. `Hub._deliver` gives every device `SEND_TIMEOUT` (5 s) and a
    phone that locked mid-evening neither takes the frame nor raises, so a write that awaited the
    fan-out charged five seconds of somebody else's suspended laptop to the phone that answered —
    against "under 1.5 s". Nothing in any response here depends on delivery: this module's own
    docstring calls the frames at-most-once nudges to re-read, every client re-reads over REST on
    reconnect, and the re-read is what the frame is asking for anyway. [M4.12 finding 17]

    THE PAYLOAD IS BUILT BEFORE THE CALL, NEVER INSIDE IT. Every frame below is assembled from the
    request's pooled connection, which `deps.db` releases the moment the response is produced — a
    detached coroutine with a query still to run would be reading through a connection somebody else
    now holds, which is the rule `_invite` states at length for its own reason. So each caller passes
    a finished frame and this takes only the delivery.

    Nothing is awaited and nothing is logged here because there is nothing to report: `_deliver`
    catches per device, drops the socket and closes it, so the task cannot fail as a whole.
    """
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
        # One person's sealed pair answered as another would write into the wrong seat. The
        # seal proves the server drew it; the id proves who for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that pair belongs to another seat")
    return (
        round_rules.Pair(
            title_a=payload["a"], title_b=payload["b"], selection=payload["s"], reason=""
        ),
        int(payload["n"]),
    )


async def _bundle_version(conn: asyncpg.Connection) -> str:
    version = await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    if version is None:
        # §3.1: a bundle-less app is a legal state, and artifact-dependent surfaces render an
        # explicit "no bundle imported" state rather than erroring.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "no_bundle",
                    "message": "no artifact bundle is active — Tonight needs one to rank"},
        )
    return version


def _room_error(exc: rooms.RoomError | play.RoundError | ballot_rules.BallotError) -> HTTPException:
    """One mapping, so two callers cannot disagree about what a refusal means.

    409 for "the world moved" (a stale pair, a started room, a round already over), 404 for a
    room that is not there, 403 for a seat that is not yours, 422 for an answer that is not one
    of the four.
    """
    codes = {
        "no_room": status.HTTP_404_NOT_FOUND,
        "no_seat": status.HTTP_404_NOT_FOUND,
        "no_slate": status.HTTP_404_NOT_FOUND,
        "bad_answer": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "guest_count": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "not_on_slate": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "not_your_turn": status.HTTP_409_CONFLICT,
        "too_early": status.HTTP_409_CONFLICT,
        # The request is well formed and the world is not ready: a member the nightly fit has
        # not reached yet cannot be ranked against, so the host is told who rather than being
        # handed the budget advice that cannot help. Named here rather than left to the default
        # so the status is a decision the mapping records. [M4.12 finding 34; decision 216]
        "unscored_member": status.HTTP_409_CONFLICT,
    }
    return HTTPException(
        codes.get(exc.reason, status.HTTP_409_CONFLICT),
        detail={"reason": exc.reason, "message": str(exc)},
    )


async def _seat_for(
    conn: asyncpg.Connection, participant_id: int, user: auth.SessionUser
) -> asyncpg.Record:
    """The seat this caller is allowed to write to.

    Their own seat, or a **guest** seat in a session they host — §6.2 step 2's hand-the-phone
    puts several participants behind one cookie by design, and only that. Any other pairing is
    one member casting another's vote, which would land in §13's approval share.
    """
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


# --- bodies ---------------------------------------------------------------------------------


class OpenBody(BaseModel):
    """§6.2 step 1's three controls, plus the guest count. Bounds mirror 0013's CHECKs so a bad
    request is a 422 with a field name rather than a database error."""

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


class SoloAnswer(BaseModel):
    """One answer of 54f's sharpen round, as the client hands it back.

    A SHAPE RATHER THAN A DICT, because the route reconstructed each entry with `int(a["title_a"])`
    and a missing key or a non-numeric value then left the handler as a `KeyError` or a `ValueError`
    — a 500 on a malformed request, which §6.8's register owes a named field instead. Pydantic is
    already the thing that turns `OpenBody`'s bounds into a 422 with a field name, and this is the
    one Tonight body that had opted out of it.

    `selection` IS ABSENT AND IS NOT AN OVERSIGHT. 54b binds §13's arm to this round too, and the
    arm is a function of the seq and of the person — so the one field a client must never be able
    to choose is also the one it never has to send. The route re-derives it below. `seq` is
    optional because the client numbers its own round from one and an older client sent no seq at
    all; the route falls back to the position, exactly as it did before. [finding 38; 54b]
    """

    title_a: int
    title_b: int
    answer: Literal["A", "B", "EITHER", "NEITHER"]
    seq: int | None = None


class SoloBody(BaseModel):
    kind: Kind = "movie"
    runtime_budget_min: int = Field(default=130, ge=60, le=200)
    include_rewatches: bool = False
    offset: int = Field(default=0, ge=0, le=64)
    # 54f's THIRD control, and the only one that costs anything. The door and Reshuffle land on
    # picks and never draw a pair; "sharpen this" is the tap that asks for one. It defaults to
    # False because the expensive answer must be the one someone asked for: with the flag absent,
    # `round.replay` still replays the answers and skips only the pair search. [finding 35]
    sharpen: bool = False
    # 54f's sharpen round is stateless — §6.2 step 8 forbids the session row that would hold it —
    # so the client carries its own answers and hands them back.
    answers: list[SoloAnswer] = Field(default_factory=list, max_length=64)


# --- the lobby and its channels ---------------------------------------------------------------


@router.get("/rooms")
async def open_rooms(user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 2's open-rooms list — "visible to every household device", not only the host's."""
    return {"rooms": await rooms.open_rooms(conn, viewer_id=user.id)}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def open_session(body: OpenBody, user: ActiveUser, conn: DB) -> dict[str, object]:
    """Open a room, and tell the household it exists.

    The push invitation is best-effort in the strongest sense: it is dispatched after the room
    is committed and its outcome is not awaited into the response. §6's preamble makes push
    best-effort with an in-app equivalent for every prompt, and a lobby that blocked on a
    delivery receipt would break on exactly the iPhone the constraint was written about.

    This paragraph used to be a claim rather than a description: `_invite` was awaited here, and
    it loops over invited members awaiting one send each, with the sender opening an httpx client
    per device at a 10 s timeout. Measured with the sender stubbed at 1 s it cost 1.10 s for a
    single member; two members with two phones each and the home connection down is up to 40 s of
    apparently-hung lobby, the whole of it holding this request's pooled connection. [finding 42]

    Moving it off the request kept the second half of that cost: the task holds a connection of its
    own for the same 40 s, and a host who opens a few rooms while the endpoints hang takes the
    ten-connection pool down with invitations while Rate, Home and auth 503 with nothing in the log
    pointing at Tonight. `_INVITE_TIMEOUT_S` is what bounds it. [M4.12 review cycle 2: M412-API2-02]
    """
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
    """Dispatch §6.2 step 2's invitation, off the request that opened the room.

    ITS OWN CONNECTION, NEVER THE REQUEST'S. `deps.db` releases the request's connection when the
    response is produced, and this coroutine outlives the response by design — writing through a
    released connection is a use-after-free with a ten-connection pool behind it. The acquire is
    the same one every other non-request caller makes (`db_pool.acquire`), and it is taken inside
    the task rather than handed in, so the pool hands it out only once the send is actually about
    to run. [finding 42; the rule it applies is finding 16's]

    The sender is optional at import time on purpose: it is the half §7.3 dates to "the M4
    stack", and a household whose SECRETS_KEY is unset (§3.1's half-configured boot) must still
    be able to open a room and be joined by code.

    NOTHING HERE MAY RAISE. An exception out of a bare task surfaces whenever the event loop
    collects it, with no request to attach it to and no status code to carry it — so the failure
    is logged here, at warning with the traceback, which is also what `rooms.invite` does per
    member. The two layers catch different things: a send that failed is one member's phone, and
    a failure out here is the pool, the database or the invite list.
    """
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
        # Its own branch rather than the catch-all below, because the two say different things to
        # whoever reads the log: this one has already invited some members and is giving the
        # connection back, and the other never got started. Neither is an outage — §6's preamble
        # makes push best-effort — but only one of them means the endpoints are hanging.
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
    """§6.2 step 2: "Join channels, all equivalent."

    One route behind every channel, so "equivalent" is a fact about the code rather than a
    claim about four of them. A second arrival re-attaches to the seat the member already has.
    """
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
    """The lobby, the progress and the ballot state — everything a device renders before the
    reveal, and nothing about the pool.

    Settled before it reads, like the ballot and the result below it. This is the screen every
    device in the room is already polling, so it is where a room that stopped progressing has to
    be picked up: `play.settle` says why the rule lives there rather than in the answer handler,
    and the cost here is two counts on indexed columns.
    """
    try:
        await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z)
        seen = await rooms.lobby(conn, session_id)
    except (rooms.RoomError, play.RoundError) as exc:
        # `_room_error` already maps both exception types; a refusal out of `settle` is a domain
        # reason with a status (a room that is not there is a 404), which is not the same as a
        # combine that failed — that raises an asyncpg error and keeps travelling to `app.py`,
        # where it is the 500 and the log line it should be.
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


@router.post("/sessions/{session_id}/start")
async def start(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """The host closes the join window. §6.2 step 2, as the host's lobby states it: "Anyone who
    joins before you start is in.""" ""
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
    """The host ends the evening — decision 169, and the one control a stuck room needs.

    HOST-ONLY, and the check is `start`'s above, written out at the route rather than pushed into
    `rooms.end_session`: the host is a column on `session` and this is the same question `start`
    asks of the same column, while the rule underneath is about the room's lifecycle and would
    otherwise have to be told who was asking. §6.2 step 1 gives the host the session's controls, and
    a member ending the evening on the household's behalf is the failure with the sign flipped.

    Both frames, because both screens are wrong otherwise: the room's own devices are sitting on a
    lobby or a round (`HUB.to_session`), and every other household device has the room on §6.2 step
    2's list (`HUB.to_household`). The lobby is read after the write so the frame carries
    `abandoned` rather than the state the caller arrived with.
    """
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


# --- the round ---------------------------------------------------------------------------------


def _public_state(state: dict[str, Any], token: str | None) -> dict[str, Any]:
    """The round state a device may see.

    Assembled from named fields rather than by filtering the domain object: the card carries
    the whole snapshot and the whole posterior, and a payload built by exclusion leaks the day
    somebody adds a field. Four callers now — the round read and the three writes, which return
    `play._card`'s shape rather than making this module ask for it again (M4.12 finding 3).
    """
    pair = state["pair"]
    return {
        "participant_id": state["participant_id"],
        "answered": state["answered"],
        "cap": state["cap"],
        "ended_by": state["ended_by"],
        "stop_reason": state["stop_reason"],
        "escape_available": state["escape_available"],
        "card_token": token,
        "pair": None if pair is None else {
            "a": pair["a"], "b": pair["b"],
            # 54b/proposal 146: the held-out arm is identifiable end to end. It travels so the
            # UI *may* label it; it is never accepted from the client.
            "selection": pair["selection"],
            "reason": pair["reason"],
        },
    }


# The straddle multiple the round is asked with. Named here and nowhere decided: §6.2's boundary
# is `round_rules.BOUNDARY_Z`, calibrated against the owned-pool score scale, and the route's only
# job is to hand `play` the round's own constant. It used to read §6.3's `straddle_z` out of the
# bundle on the argument that "still straddles" is one predicate — two scales, one multiple, and
# the round stopped being able to converge (decisions 175, 205, 214).


@router.get("/seats/{participant_id}/round")
async def round_state(
    participant_id: int, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """The next card, and — when serving it is what ended the seat — the room's transition too.

    THE FOURTH SETTLING READ, and the one the rule was written without. `play.state_for`'s
    belt-and-braces is the only thing that can end a seat on a pool of two or three candidates
    (decision 215): there is no pair to answer and the escape is refused below pair six. This was
    the one read in this module that called neither `play.settle` nor `_announce` and pushed no
    frame, so the device that read the LAST un-ended seat left the room in `voting` with every
    seat `converged` and woke nobody — and `tonight.svelte.js`'s `refresh()` holds the client's
    only GET of the session and runs it BEFORE its round read, so the order `settle` needs is the
    one order the client never produces. A household with three owned shows tapped Start and
    waited until somebody reloaded. Reproduced through the real app on a three-film library:
    both seats `converged`, session `voting`, and one further GET of the session moved it.

    GATED ON THE WRITE, not called on every read: the ordinary poll of a live round stays one
    statement, and only the read that actually moved a seat pays for the progress frame and the
    settle the other phones in the room are waiting on. [M4.12 review cycle 2: M412-PLAY-4]
    """
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
    """One answer, then the next card — §6 preamble's "next card preloaded"."""
    seat = await _seat_for(conn, participant_id, user)
    pair, seq = _unseal(body.card_token, participant_id=participant_id)
    try:
        written = await play.record_answer(
            conn, participant_id=participant_id, pair=pair, answer=body.answer,
            seq=seq, latency_ms=body.latency_ms, z=round_rules.BOUNDARY_Z,
        )
    except play.RoundError as exc:
        raise _room_error(exc) from exc

    # §6.7's rail, decision 117. `session_answer_line` is one of §6.7's four worked examples
    # and has existed since M2 with nothing producing it; this is the write. Recorded under the
    # ANSWERING USER's id, never the seat's owner — during the blind round a line filed against
    # another account would be an answer leaving its seat, which is the one thing 54c's
    # blindness is about.
    rail.record(
        user_id=user.id,
        kind="session_answer",
        line=rail.session_answer_line(str(seat["id"]), seq, body.answer),
        title_id=pair.title_a,
        detail={"selection": pair.selection, "session_id": seat["session_id"]},
    )
    await _announce(conn, seat["session_id"])
    # THE CARD IS THE ONE THE WRITE ALREADY DREW. This route used to call `play.state_for` here,
    # which read the frozen pool out of jsonb a second time and replayed the whole round a second
    # time — two searches and two decodes for one tap, on the surface §6's preamble budgets at
    # 1.5 s. `record_answer` computes the next card under the seal the reload draws under too
    # (`play._round_of`, decision 223), so the payload is assembled from its return and a reload
    # mints the identical token. [M4.12 findings 3 and 28]
    token = (
        None if written["_pair"] is None
        else _seal(participant_id, written["_pair"], written["answered"] + 1)
    )
    # NO EMBEDDED RAIL. This response used to carry `rail.recent(limit=5)` because §6.7's drawer
    # was mounted on Home alone, so the surface that produces §6.7's own fourth worked example
    # could not otherwise show it. That made the round's log a second, shorter rail with its own
    # depth and its own refresh — one drawer per route, differing from each other. The write
    # above still happens; `GET /api/model-log` is where it is read, and the frontend shell stage
    # of this same milestone moves the `ModelRail` mount into the layout so the drawer opens here
    # too. One drawer, not one per route. [M4.9 findings 25 and 26]
    payload = {
        **_public_state(written, token),
        "wrote": {"seq": written["seq"], "stop_reason": written["stop_reason"]},
    }
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.post("/seats/{participant_id}/undo")
async def undo(
    participant_id: int, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """§6 preamble's "undo everywhere", reaching the one surface where a mis-tap is otherwise
    permanent: a hard cap, a blind reveal, no second pass."""
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
    # No token and no second read: a seat that has just ended has no next pair by construction, so
    # `escape` carries the whole card (finding 3).
    return _public_state(out, None)


async def _announce(conn: asyncpg.Connection, session_id: int) -> None:
    """Push progress to the room, and tell it when the combine has moved it on.

    The frames, and nothing else. The `voting -> ballot` transition itself is `play.settle`,
    which the three reads below also call — a rule whose only caller was this function was a rule
    one dropped connection could retire for the evening, which is finding 4 and the whole of why
    this function is now two statements. The transition still runs on an answer rather than
    waiting for a client to ask, because 54e's reveal is simultaneous: whichever device answers
    last must not be the only one that has a slate.

    `settle`'s return value is what decides the second frame. A lobby frame pushed on every
    answer would tell every device in the room to re-render its whole lobby ten times a round.

    Each frame is read from this connection and then handed to `_nudge`: the read is the request's
    and the delivery is not, which is the whole of finding 17. The settle is still awaited here —
    it is a write this answer owes the room, not a nudge.
    """
    progress = channel_rules.progress_frame(session_id, await play.progress(conn, session_id))
    _nudge(HUB.to_session(session_id, progress))
    if await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z):
        lobby = channel_rules.lobby_frame(await rooms.lobby(conn, session_id))
        _nudge(HUB.to_session(session_id, lobby))


# --- the ballot and the reveal -------------------------------------------------------------


@router.get("/sessions/{session_id}/ballot")
async def ballot_card(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """54e's ballot: the three finalists and the wildcard, and nothing about anybody's vote.

    Settled before it reads. This route used to answer 200 with `slate: []` on a room whose every
    vote was in and whose combine had not run — the worst of the three stuck shapes, because a
    client cannot tell it from a room that has not finished answering yet. [finding 4]
    """
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
        # 54e's reveal is the one frame a household is waiting on, and it is still not awaited
        # here: every device fetches the result over REST when it lands, so a delivery the last
        # voter's request waited for would cost that phone five seconds per suspended laptop and
        # buy the room nothing. [finding 17]
        _nudge(HUB.to_session(session_id, channel_rules.reveal_frame(session_id)))
        _nudge(HUB.to_household(channel_rules.rooms_changed()))
    else:
        progress = channel_rules.progress_frame(session_id, await play.progress(conn, session_id))
        _nudge(HUB.to_session(session_id, progress))
    return {"submitted": submitted, "seated": seated, "revealed": revealed}


@router.get("/sessions/{session_id}/result")
async def result(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 7's winner card.

    Settled before it reads, like the two reads above it, and then refused with the ballot's own
    reason until every seat has submitted: the guard lives in `ballot.tally`, which this calls, so
    this route and the session WebSocket cannot disagree about when the evening is revealed (it
    was the TV route here until decision 165). The refusal
    and the settle are not in tension — the 409 is the ballot's and is correct, while the room it
    was asked about has moved on, so the household can now vote instead of needing SQL.

    The card itself is `tonight/result.slate`. What is left here is the one thing that is genuinely
    HTTP's: §7.1's deep link needs the connector's configured URL, which is configuration rather
    than arithmetic. [finding 4; arch-06]
    """
    try:
        await play.settle(conn, session_id, z=round_rules.BOUNDARY_Z)
        counted = await ballot_rules.tally(conn, session_id)
        outcome = await ballot_rules.resolve(conn, session_id)
    except (ballot_rules.BallotError, play.RoundError) as exc:
        raise _room_error(exc) from exc

    jf = await conn.fetchval("SELECT config FROM connector_config WHERE name = 'jellyfin'")
    base = (jf or {}).get("url", "") if isinstance(jf, dict) else ""

    def play_url(jellyfin_id: str) -> str:
        """§7.1's deep link, exactly as the spec writes it."""
        return f"{base.rstrip('/')}/web/#/details?id={jellyfin_id}"

    return await result_rules.slate(
        conn, session_id, counted, outcome,
        # Absent rather than guessed when no connector is configured (§6.0) — which is why the
        # link-maker itself is None here rather than a function that returns a bare path.
        play_url=play_url if base else None,
    )


@router.get("/sessions/{session_id}/evaluation")
async def evaluation(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§13's instrument for the round, and §14 risk 6's rates. Reads the held-out stream and
    nothing else, and names no candidate."""
    return await evaluation_rules.report(conn, session_id)


# --- solo -------------------------------------------------------------------------------------


@router.post("/solo")
async def solo(
    body: SoloBody, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """54f: solo lands directly on three picks and a wildcard. No session row, so the sharpen
    round's answers travel with the request."""
    version = await _bundle_version(conn)
    # ONE EXPRESSION, TWO READERS, AND THAT IS THE POINT. 54b's arm is a rate drawn from a stable
    # key now (decision 223), and solo has two places that must agree about it: the re-derivation
    # below, which classifies an answer the client is holding, and `picks`, which draws the pair
    # that answer will be. `user.id` because it is the only thing about a solo sharpen round that
    # survives a request — §6.2 step 8 mints no session row, so there is no nonce to freeze and no
    # row to read the arm back off. Server-side and never client-supplied (54b), and stable, so a
    # stored answer cannot classify one way on one request and the other way on the next.
    holdout_key = str(user.id)
    # A LOOP RATHER THAN A COMPREHENSION, for one reason: the seq is read twice — once as the
    # row's own and once as the arm's key — and a fallback spelled in two places is a fallback
    # that can be spelled two ways. Nothing here validates the ids: whether a title is still a
    # candidate is a domain fact `tonight/pool.py` decides and `round.replay` / `tilt.applies`
    # enforce, and a route that re-derived it would be a second pool with a second opinion.
    # The entry's SHAPE is `SoloAnswer`'s and is refused as a 422 before this runs — the old
    # `if str(a.get("answer")) in ANSWERS` filter dropped a malformed answer silently, which is a
    # round that quietly counted one fewer than the person gave. [findings 37, 38]
    answers = []
    for i, a in enumerate(body.answers):
        seq = a.seq if a.seq is not None else i + 1
        answers.append(round_rules.Answered(
            seq=seq, title_a=a.title_a, title_b=a.title_b, answer=a.answer,
            # RE-DERIVED, never accepted. 54b binds §13's guard to this round too, and the arm
            # is a function of the seq and this person — so the one field a client must not be
            # able to choose is also the one field it never has to send. Reconstructing the
            # answer without it took `Answered`'s default of `adaptive`, which made both the
            # replay's hold-out filter and solo's own live count dead by construction: the
            # sharpen answers the arm drew moved the posterior that selection and stopping read.
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


# --- the session channel -----------------------------------------------------------------------


@router.websocket("/channel")
async def channel(socket: WebSocket, user: ActiveUserWS, session_id: int | None = None) -> None:
    """§6.2 step 2's live lobby banner, and 54c's waiting view.

    Behind the same two gates as every REST route, and declared rather than written out:
    `deps.active_user_ws` is `active_user` for a socket, so §3.1's first-login lock applies here and
    a sweep over the dependency graph can *see* that it does. The frames name who is in which room,
    so a socket that authenticated in its own body was a door with no gate anything could enumerate
    — and for two milestones that was literally true of this one. [decision 225; finding 20]

    THE ONE ROUTE THAT ACQUIRES FOR ITSELF, deliberately, and the reason is the lifetime and not the
    layering: `deps.db` holds its connection until the endpoint returns, and this endpoint returns
    when the household closes the app. Ten phones watching a lobby would hold all ten of the pool's
    connections (`db/pool.py`'s `max_size`) for the evening, and Rate, Home and auth would stop
    answering with nothing failing in Tonight. So both payloads are built inside the acquire, the
    block is exited, and only then does anything go to the socket — a client that is connected but
    not reading then stalls its own send and nothing else. Raising `max_size` moves that wall rather
    than removing it. [finding 16; decision 225]

    The sends carry the hub's own timeout, because the route has the same problem the hub had: a
    phone that cannot take its opening frame in five seconds is one the evening should not wait for,
    and closing it is what gets its `onclose` reconnect rather than leaving it connected and deaf.
    THE ACQUIRE CARRIES IT TOO, and for the same sentence read the other way round: a socket that
    cannot be GIVEN its opening frames in five seconds is one the evening should not wait for
    either. `pool().acquire()` is asyncpg's queue wait with no bound, so a saturated pool left this
    block waiting for ever inside a `try` whose `except TimeoutError` exists precisely to stop that
    — the socket accepted, subscribed, and permanently silent, with nothing in the log. The bound
    is the hub's rather than `deps.db`'s ten seconds because the frames are the point: a phone that
    waits longer than its own send budget for a picture of the room is better off reconnecting.
    [M4.12 review cycle 2: M412-API2-01]
    """
    await socket.accept()
    sub = HUB.subscribe(socket, user_id=user.id, session_id=session_id)
    try:
        # Send the current picture immediately: a client that connects mid-evening must not
        # wait for the next change to know what it is looking at. Built here and sent below.
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
            # The channel is one-way by design: a client that wants to write uses REST, where
            # the seat check lives. Reading keeps the socket alive and detects the close.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        # Either end of the same budget: the pool had nothing to hand out, or the phone would not
        # take what it was handed. Both are "this socket did not get its opening frames", and both
        # are answered the same way, so they are logged as the one thing with both causes named.
        log.info(
            "a tonight socket did not get its opening frames within %ss (a saturated pool, or a "
            "client that is not reading); closing it to let it reconnect",
            channel_rules.SEND_TIMEOUT,
        )
        await channel_rules.close_quietly(socket)
    finally:
        HUB.unsubscribe(sub)


Router = Annotated[APIRouter, None]
