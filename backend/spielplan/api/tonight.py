"""The Tonight surface's routes. Spec v2.1 §6.2 (rewritten, 54a-54g), §6.7, §11, §13; decision 117.

Thin, like `api/rank.py` and `api/rate.py`: every rule lives in `spielplan.tonight` and the only
things decided here are the HTTP shapes. Four of them are load-bearing.

**AN ANSWER NAMES A SEALED PAIR, NEVER TWO TITLE IDS.** The same property `api/rank.py` gives a
queue pair, and for a sharper reason: 54b makes `session_answer.selection` the discriminator
§13's evaluation depends on, so a route accepting `{"title_a": 4, "title_b": 9, "selection":
"adaptive"}` would let a client file its own answer into or out of the held-out stream. The pair
is drawn on the server, sealed with `itsdangerous` under `SESSION_SECRET`, and handed back
opaque. **And the seal is single-use**, because it carries the sequence number the participant
was on: answering moves the counter, so a replay is a stale card and gets a 409. §13's figures
count *rows* and §4.2's tables are append-only, so a replay that landed could not be taken back.

**NOTHING BEFORE THE REVEAL CARRIES THE POOL.** §6.2 step 3: the candidate pool is "internal —
never shown as a step". The v2.1 redesign deleted the visible shortlist because a pool rendered
before the votes anchors the votes it exists to collect — the same anchoring §6.1 forbids by
withholding the prediction until after the tap. So every payload below is assembled from named
fields rather than by serialising a domain object, and `_no_pool` is asserted in the tests
against every pre-reveal route.

**THE BALLOT'S BLINDNESS IS NOT ENFORCED HERE.** It is enforced in `ballot.tally`, which refuses
until every seat has submitted. This module simply calls it. That is deliberate: the TV route
and the WebSocket are second and third callers, and a guard living in one route is a guard three
callers can forget.

**A GUEST TURN IS A ROUTE THE HOST'S SESSION MAY CALL.** §6.2 step 2's hand-the-phone means one
signed-in cookie speaks for several participants, so a participant id is a parameter — and every
write checks that the caller is entitled to that seat: their own, or a guest seat in a session
they host. Without that check a `participant_id` in a URL is a way to cast somebody else's vote.
"""

from __future__ import annotations

import logging
import random
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser
from spielplan.core import auth
from spielplan.core.config import settings
from spielplan.db import pool as db_pool
from spielplan.home import rail
from spielplan.ledger import hyperparams
from spielplan.tonight import ballot as ballot_rules
from spielplan.tonight import channel as channel_rules
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import evaluation as evaluation_rules
from spielplan.tonight import play, rooms
from spielplan.tonight import round as round_rules
from spielplan.tonight import solo as solo_rules

log = logging.getLogger("spielplan.api.tonight")

router = APIRouter(prefix="/api/tonight", tags=["tonight"])

# A salt of its own, so a sealed Tonight pair can never be presented as a session cookie or as a
# Rank queue pair. Rotating SESSION_SECRET invalidates all three, which is §2's stated behaviour.
_PAIR_SALT = "spielplan/tonight/pair/v1"

# The round is drawn per request, so the draw needs entropy that is not a fixture seed.
_rng = random.SystemRandom()

HUB = channel_rules.Hub()

Kind = Literal["movie", "series"]


def _sealer() -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret or "insecure-dev-secret", _PAIR_SALT)


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


def _z(request: Request) -> float:
    """§6.3's `straddle_z`, reused: the round's "still straddles the boundary" is the same
    predicate the board's badge uses, so it reads the same bundle-shipped constant rather than
    growing a second one that could drift."""
    cached = getattr(request.app.state, "hyperparams", None)
    if cached is None:
        cached, _ = hyperparams.load(getattr(request.app.state, "artifacts", None))
    return float(cached.straddle_z)


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


class SoloBody(BaseModel):
    kind: Kind = "movie"
    runtime_budget_min: int = Field(default=130, ge=60, le=200)
    include_rewatches: bool = False
    offset: int = Field(default=0, ge=0, le=64)
    # 54f's sharpen round is stateless — §6.2 step 8 forbids the session row that would hold it —
    # so the client carries its own answers and hands them back.
    answers: list[dict[str, Any]] = Field(default_factory=list, max_length=64)


# --- the lobby and its channels ---------------------------------------------------------------


@router.get("/rooms")
async def open_rooms(user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 2's open-rooms list — "visible to every household device", not only the host's."""
    return {"rooms": await rooms.open_rooms(conn, viewer_id=user.id)}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def open_session(
    body: OpenBody, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    """Open a room, and tell the household it exists.

    The push invitation is best-effort in the strongest sense: it is dispatched after the room
    is committed and its outcome is not awaited into the response. §6's preamble makes push
    best-effort with an in-app equivalent for every prompt, and a lobby that blocked on a
    delivery receipt would break on exactly the iPhone the constraint was written about.
    """
    version = await _bundle_version(conn)
    room = await rooms.open_session(
        conn, host_user_id=user.id, kind=body.kind,
        budget_min=body.runtime_budget_min, include_rewatches=body.include_rewatches,
        bundle_version=version, guests=body.guests, rng=_rng,
    )
    await HUB.to_household(channel_rules.rooms_changed())
    await _invite(request, conn, session_id=room["session_id"], host_user_id=user.id,
                  room_code=room["room_code"])
    return {**room, "lobby": await rooms.lobby(conn, room["session_id"])}


async def _invite(
    request: Request, conn: asyncpg.Connection, *, session_id: int, host_user_id: int,
    room_code: str,
) -> None:
    """§6.2 step 2's push to members' phones. Never fatal, by construction.

    The sender is optional at import time on purpose: it is the half §7.3 dates to "the M4
    stack", and a household whose SECRETS_KEY is unset (§3.1's half-configured boot) must still
    be able to open a room and be joined by code.
    """
    try:
        from spielplan.push import send as push_send
    except Exception:  # pragma: no cover - the sender is absent only in a partial checkout
        return
    invited = await rooms.members_to_invite(
        conn, session_id=session_id, host_user_id=host_user_id
    )
    for user_id in invited:
        try:
            await push_send.send_to_user(
                conn, user_id,
                {"kind": "tonight.invite", "session_id": session_id, "room_code": room_code,
                 "title": "Tonight", "body": f"A room is open — {room_code}"},
            )
        except Exception:
            # Best-effort: §6's preamble guarantees an in-app equivalent for every push, and
            # the room code and the open-rooms list are both already live.
            log.info("push invitation to user %s was not delivered", user_id)


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
    await HUB.to_session(session_id, channel_rules.lobby_frame(lobby))
    return {"session_id": session_id, **seat, "lobby": lobby}


@router.get("/sessions/{session_id}")
async def lobby(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """The lobby, the progress and the ballot state — everything a device renders before the
    reveal, and nothing about the pool."""
    try:
        seen = await rooms.lobby(conn, session_id)
    except rooms.RoomError as exc:
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
    await HUB.to_session(session_id, channel_rules.lobby_frame(lobby))
    await HUB.to_household(channel_rules.rooms_changed())
    return {"session_id": session_id, "state": lobby["state"]}


# --- the round ---------------------------------------------------------------------------------


def _public_state(state: dict[str, Any], token: str | None) -> dict[str, Any]:
    """The round state a device may see.

    Assembled from named fields rather than by filtering the domain object: `state_for` carries
    the whole snapshot and the whole posterior, and a payload built by exclusion leaks the day
    somebody adds a field.
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


@router.get("/seats/{participant_id}/round")
async def round_state(
    participant_id: int, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    await _seat_for(conn, participant_id, user)
    try:
        state = await play.state_for(conn, participant_id, z=_z(request), rng=_rng)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    token = (
        None if state["_pair"] is None
        else _seal(participant_id, state["_pair"], state["answered"] + 1)
    )
    return _public_state(state, token)


@router.post("/seats/{participant_id}/answer")
async def answer(
    participant_id: int, body: AnswerBody, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    """One answer, then the next card — §6 preamble's "next card preloaded"."""
    seat = await _seat_for(conn, participant_id, user)
    pair, seq = _unseal(body.card_token, participant_id=participant_id)
    z = _z(request)
    try:
        written = await play.record_answer(
            conn, participant_id=participant_id, pair=pair, answer=body.answer,
            seq=seq, latency_ms=body.latency_ms, z=z,
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
    await _announce(conn, seat["session_id"], z=z)
    state = await play.state_for(conn, participant_id, z=z, rng=_rng)
    token = (
        None if state["_pair"] is None
        else _seal(participant_id, state["_pair"], state["answered"] + 1)
    )
    payload = {
        **_public_state(state, token),
        "wrote": {"seq": written["seq"], "stop_reason": written["stop_reason"]},
        "rail": rail.recent(user_id=user.id, limit=5),
    }
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.post("/seats/{participant_id}/undo")
async def undo(
    participant_id: int, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    """§6 preamble's "undo everywhere", reaching the one surface where a mis-tap is otherwise
    permanent: a hard cap, a blind reveal, no second pass."""
    seat = await _seat_for(conn, participant_id, user)
    try:
        out = await play.retract(conn, participant_id)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    z = _z(request)
    await _announce(conn, seat["session_id"], z=z)
    state = await play.state_for(conn, participant_id, z=z, rng=_rng)
    token = (
        None if state["_pair"] is None
        else _seal(participant_id, state["_pair"], state["answered"] + 1)
    )
    return {**_public_state(state, token), "retracted_seq": out["retracted_seq"]}


@router.post("/seats/{participant_id}/escape")
async def escape(
    participant_id: int, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    """54c's "just pick for us"."""
    seat = await _seat_for(conn, participant_id, user)
    try:
        out = await play.escape(conn, participant_id)
    except play.RoundError as exc:
        raise _room_error(exc) from exc
    z = _z(request)
    await _announce(conn, seat["session_id"], z=z)
    state = await play.state_for(conn, participant_id, z=z, rng=_rng)
    return {**_public_state(state, None), "ended_by": out["ended_by"]}


async def _announce(conn: asyncpg.Connection, session_id: int, *, z: float) -> None:
    """Push progress to the room, and combine once every seat has finished.

    The combine happens here rather than on a client's request because 54e's reveal is
    simultaneous: whichever device answers last must not be the only one that has a slate.
    """
    await HUB.to_session(
        session_id, channel_rules.progress_frame(session_id, await play.progress(conn, session_id))
    )
    if not await play.everyone_finished(conn, session_id):
        return
    state = await conn.fetchval("SELECT state FROM session WHERE id = $1", session_id)
    if state == rooms.STATE_VOTING:
        await play.finish(conn, session_id, z=z)
        await HUB.to_session(
            session_id, channel_rules.lobby_frame(await rooms.lobby(conn, session_id))
        )


# --- the ballot and the reveal -------------------------------------------------------------


@router.get("/sessions/{session_id}/ballot")
async def ballot_card(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """54e's ballot: the three finalists and the wildcard, and nothing about anybody's vote."""
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
        await HUB.to_session(session_id, channel_rules.reveal_frame(session_id))
        await HUB.to_household(channel_rules.rooms_changed())
    else:
        await HUB.to_session(
            session_id,
            channel_rules.progress_frame(session_id, await play.progress(conn, session_id)),
        )
    return {"submitted": submitted, "seated": seated, "revealed": revealed}


@router.get("/sessions/{session_id}/result")
async def result(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§6.2 step 7's winner card.

    Refused with the ballot's own reason until every seat has submitted: the guard lives in
    `ballot.tally`, which this calls, so the TV route and this route cannot disagree about when
    the evening is revealed.
    """
    try:
        counted = await ballot_rules.tally(conn, session_id)
        outcome = await ballot_rules.resolve(conn, session_id)
    except ballot_rules.BallotError as exc:
        raise _room_error(exc) from exc

    rows = await conn.fetch(
        """
        SELECT r.title_id, r.rank, r.slot, r.group_score, r.per_user_match, r.conflict,
               t.name, t.year, t.runtime_min, t.poster_path, t.jellyfin_id
          FROM session_result r JOIN title t ON t.id = r.title_id
         WHERE r.session_id = $1 ORDER BY r.rank
        """,
        session_id,
    )
    approvals = {r["title_id"]: r["approvals"] for r in counted}
    jf = await conn.fetchval("SELECT config FROM connector_config WHERE name = 'jellyfin'")
    base = (jf or {}).get("url", "") if isinstance(jf, dict) else ""
    budget = await conn.fetchval("SELECT runtime_budget_min FROM session WHERE id = $1", session_id)

    def card(row: asyncpg.Record) -> dict[str, Any]:
        from spielplan.tonight import pool as pool_rules

        return {
            "title_id": row["title_id"], "rank": row["rank"], "slot": row["slot"],
            "name": row["name"], "year": row["year"], "runtime_min": row["runtime_min"],
            "poster_path": row["poster_path"],
            "approvals": approvals.get(row["title_id"], 0),
            "match_lines": list((row["per_user_match"] or {}).values()),
            "conflict": row["conflict"],
            # §6.4's "honestly labelled", from the one place that holds the words. The client
            # spelled them itself, which left `combine.WILDCARD_LABEL` with no reader and the
            # two free to drift — and §6.4 is a claim about what the person is told, so the
            # copy is the rule rather than decoration around it.
            "label": (
                combine_rules.WILDCARD_LABEL
                if row["slot"] == combine_rules.SLOT_WILDCARD else None
            ),
            "fit_line": pool_rules.fit_line(
                runtime_min=row["runtime_min"], budget_min=budget or 130
            ),
            # §7.1's deep link, exactly as the spec writes it. Absent rather than guessed when
            # the title has no Jellyfin id or no connector is configured (§6.0).
            "play_url": (
                f"{base.rstrip('/')}/web/#/details?id={row['jellyfin_id']}"
                if base and row["jellyfin_id"] else None
            ),
        }

    slate = [card(r) for r in rows if r["slot"] in ("finalist", "wildcard")]
    winner = next((c for c in slate if c["title_id"] == outcome["chosen_title_id"]), None)
    # §6.2 step 7's "runners-up" are the titles that RAN — the rest of the ballot, ordered by
    # how close they came. The `runner_up` slot is the candidate pool's tail: never on any
    # ballot, therefore always "0 approved", and drawing it here put the two losing finalists —
    # the ones people actually approved — nowhere on the screen at all. The review found it.
    runners_up = sorted(
        (c for c in slate if winner is None or c["title_id"] != winner["title_id"]),
        key=lambda c: (-c["approvals"], -c["rank"]),
    )
    return {
        "session_id": session_id,
        # 54e/proposal 60: the reveal opens with an explicit beat before the winner appears —
        # "shipping the property without the moment ships half of it".
        "beat": "VOTES REVEALED TOGETHER",
        "winner": winner,
        "approval_share": outcome["approval_share"],
        "participants": outcome["participants"],
        "unanimous": winner is not None and winner["approvals"] == outcome["participants"],
        "runners_up": runners_up,
        "wildcard": next((c for c in slate if c["slot"] == "wildcard"), None),
        "finalists": [c for c in slate if c["slot"] == "finalist"],
    }


@router.get("/sessions/{session_id}/evaluation")
async def evaluation(session_id: int, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§13's instrument for the round, and §14 risk 6's rates. Reads the held-out stream and
    nothing else, and names no candidate."""
    return await evaluation_rules.report(conn, session_id)


# --- solo -------------------------------------------------------------------------------------


@router.post("/solo")
async def solo(
    body: SoloBody, user: ActiveUser, conn: DB, request: Request
) -> dict[str, object]:
    """54f: solo lands directly on three picks and a wildcard. No session row, so the sharpen
    round's answers travel with the request."""
    version = await _bundle_version(conn)
    answers = [
        round_rules.Answered(
            seq=int(a.get("seq", i + 1)), title_a=int(a["title_a"]), title_b=int(a["title_b"]),
            answer=str(a["answer"]),
        )
        for i, a in enumerate(body.answers)
        if str(a.get("answer")) in round_rules.ANSWERS
    ]
    return await solo_rules.picks(
        conn, user_id=user.id, kind=body.kind, budget_min=body.runtime_budget_min,
        include_rewatches=body.include_rewatches, bundle_version=version,
        answers=answers, offset=body.offset, z=_z(request), rng=_rng,
    )


# --- the session channel -----------------------------------------------------------------------


@router.websocket("/channel")
async def channel(socket: WebSocket, session_id: int | None = None) -> None:
    """§6.2 step 2's live lobby banner, and 54c's waiting view.

    Authenticated from the same session cookie the REST routes use — a WebSocket that skipped
    auth would be a second door into a household's rooms, and the frames it carries name who is
    in the room. Anonymous sockets are closed rather than served an empty stream, so a client
    bug is visible instead of quiet.
    """
    sid = auth.open_session_cookie(socket.cookies.get(auth.SESSION_COOKIE))
    user = None
    if sid:
        async with db_pool.acquire() as conn:
            user = await auth.load_session(conn, sid)
    if user is None:
        await socket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await socket.accept()
    sub = HUB.subscribe(socket, user_id=user.id, session_id=session_id)
    try:
        # Send the current picture immediately: a client that connects mid-evening must not
        # wait for the next change to know what it is looking at.
        async with db_pool.acquire() as conn:
            await socket.send_json(
                channel_rules.wire(
                    channel_rules.rooms_changed(await rooms.open_rooms(conn, viewer_id=user.id))
                )
            )
            if session_id is not None:
                await socket.send_json(
                    channel_rules.wire(
                        channel_rules.progress_frame(
                            session_id, await play.progress(conn, session_id)
                        )
                    )
                )
        while True:
            # The channel is one-way by design: a client that wants to write uses REST, where
            # the seat check lives. Reading keeps the socket alive and detects the close.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        HUB.unsubscribe(sub)


Router = Annotated[APIRouter, None]
