"""The session's life: opening a room, seating people, and the open-rooms list (§6.2 steps 1-2).

`join` is idempotent per member, so every channel is "equivalent". Guests are seats with no user
(§4.2). A room code is unique among live rooms only.
"""

from __future__ import annotations

import json
import logging
import random
import string
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import asyncpg

from spielplan.tonight.pool import MAX_VETOES, VETOES, Seat

log = logging.getLogger("spielplan.tonight.rooms")

# §6.2's example is `MX-2210`; no I, O, 0 or 1, so a code can be dictated across a room.
CODE_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IO")
CODE_DIGITS = "23456789"
CODE_LENGTH = 4

# §6.2 step 1: "members and/or N guests"; guests share one phone in turn.
MAX_GUESTS = 6

STATE_OPEN = "open"
STATE_VOTING = "voting"
STATE_BALLOT = "ballot"
STATE_RESOLVED = "resolved"
STATE_ABANDONED = "abandoned"

ROLE_HOST = "host"
ROLE_MEMBER = "member"
ROLE_GUEST = "guest"


class RoomError(Exception):
    """A join that cannot be honoured. Carries a `reason` the route turns into a status."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def make_code(rng: random.Random) -> str:
    letters = "".join(rng.choice(CODE_LETTERS) for _ in range(2))
    digits = "".join(rng.choice(CODE_DIGITS) for _ in range(CODE_LENGTH))
    return f"{letters}-{digits}"


async def _open_with_code(
    conn: asyncpg.Connection, rng: random.Random, insert: str, *args: Any
) -> tuple[int, str]:
    """Insert the session under a code no live room holds, and return both.

    Retries on a lost uniqueness race (0013's partial index) instead of answering with its name;
    bounded, so an exhausted space fails rather than hangs.
    """
    for _ in range(20):
        code = make_code(rng)
        taken = await conn.fetchval(
            "SELECT 1 FROM session WHERE upper(room_code) = upper($1) AND ended_at IS NULL", code
        )
        if taken:
            continue
        try:
            # A savepoint: a UniqueViolationError aborts the transaction it lands in.
            async with conn.transaction():
                session_id = await conn.fetchval(insert, code, *args)
        except asyncpg.UniqueViolationError:
            continue
        return int(session_id), code
    raise RoomError("no_code", "could not allocate a room code")


async def open_session(
    conn: asyncpg.Connection,
    *,
    host_user_id: int,
    kind: str,
    budget_min: int,
    include_rewatches: bool,
    bundle_version: str,
    guests: int = 0,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Open a room. The host takes seat 1; guest seats, counted by the host, follow in turn order."""
    if guests < 0 or guests > MAX_GUESTS:
        raise RoomError("guest_count", f"between 0 and {MAX_GUESTS} guests")
    rng = rng or random.SystemRandom()

    # One transaction: the room, its seats and the abandonment below are one fact.
    async with conn.transaction():
        session_id, code = await _open_with_code(
            conn,
            rng,
            """
            INSERT INTO session (room_code, host_user_id, kind, runtime_budget_min,
                                 include_rewatches, bundle_version)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            host_user_id, kind, budget_min, include_rewatches, bundle_version,
        )
        await conn.execute(
            "INSERT INTO session_participant (session_id, user_id, role, seat) "
            "VALUES ($1, $2, $3, 1)",
            session_id, host_user_id, ROLE_HOST,
        )
        for i in range(guests):
            await conn.execute(
                "INSERT INTO session_participant (session_id, user_id, role, seat) "
                "VALUES ($1, NULL, $2, $3)",
                session_id, ROLE_GUEST, 2 + i,
            )

        # Abandon the host's other unstarted rooms, which otherwise stay live forever. Last, so a
        # failed open never costs the room the host was in; `id <> $4` spares the new one.
        await conn.execute(
            "UPDATE session SET state = $1, ended_at = now() "
            "WHERE host_user_id = $2 AND state = $3 AND ended_at IS NULL AND id <> $4",
            STATE_ABANDONED, host_user_id, STATE_OPEN, session_id,
        )
    return {"session_id": session_id, "room_code": code}


async def resolve_code(conn: asyncpg.Connection, room_code: str) -> int:
    """A live room's id, by its code. A code matching nothing live is refused rather than
    opening a room or attaching the caller to an evening that has ended."""
    session_id = await conn.fetchval(
        "SELECT id FROM session WHERE upper(room_code) = upper($1) AND ended_at IS NULL",
        room_code.strip(),
    )
    if session_id is None:
        raise RoomError("no_room", "no live room has that code")
    return int(session_id)


async def join(conn: asyncpg.Connection, *, session_id: int, user_id: int) -> dict[str, Any]:
    """Seat a member. Idempotent — this is what "all equivalent" means.

    The state is a predicate of the INSERT, with `FOR SHARE` so a join serialises against
    `play.start`'s claim: refused once it commits, admitted if it rolls back.
    """
    # Two channels one tap apart race each other: on a unique violation, re-read and retry.
    for _ in range(5):
        row = await conn.fetchrow(
            "SELECT state, ended_at FROM session WHERE id = $1", session_id
        )
        if row is None or row["ended_at"] is not None:
            raise RoomError("no_room", "that session has ended")

        existing = await conn.fetchrow(
            "SELECT id, seat, role FROM session_participant "
            "WHERE session_id = $1 AND user_id = $2",
            session_id, user_id,
        )
        if existing is not None:
            return {"participant_id": existing["id"], "seat": existing["seat"],
                    "role": existing["role"], "created": False}

        if row["state"] != STATE_OPEN:
            # Once pairs are served the participant set is fixed; the INSERT re-checks this.
            raise RoomError("started", "that room has already started")

        seat = await conn.fetchval(
            "SELECT coalesce(max(seat), 0) + 1 FROM session_participant WHERE session_id = $1",
            session_id,
        )
        try:
            participant_id = await conn.fetchval(
                "INSERT INTO session_participant (session_id, user_id, role, seat) "
                "SELECT $1, $2, $3, $4 FROM session "
                " WHERE id = $1 AND state = $5 AND ended_at IS NULL FOR SHARE "
                "RETURNING id",
                session_id, user_id, ROLE_MEMBER, seat, STATE_OPEN,
            )
        except asyncpg.UniqueViolationError:
            continue
        if participant_id is None:
            # The room moved while the statement waited on it.
            raise RoomError("started", "that room has already started")
        return {"participant_id": participant_id, "seat": seat, "role": ROLE_MEMBER,
                "created": True}
    raise RoomError("seat_race", "too many devices sat down at once — try again")


async def seats_of(conn: asyncpg.Connection, session_id: int) -> list[Seat]:
    """The session's seats; `is_member` is `role <> 'guest'` until M7's grid (§6.2 step 3)."""
    rows = await conn.fetch(
        "SELECT id, user_id, role FROM session_participant WHERE session_id = $1 ORDER BY seat",
        session_id,
    )
    return [
        Seat(participant_id=r["id"], user_id=r["user_id"], is_member=r["role"] != ROLE_GUEST)
        for r in rows
    ]


async def lobby(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Everything the lobby screen renders; no candidate, pool or ranking (§6.2 step 3)."""
    row = await conn.fetchrow(
        """
        SELECT s.id, s.room_code, s.state, s.kind, s.runtime_budget_min, s.include_rewatches,
               s.started_at, s.ended_at, s.host_user_id, u.name AS host_name,
               s.context -> 'vetoes' AS vetoes, s.context -> 'vetoes_by' AS vetoes_by
          FROM session s JOIN app_user u ON u.id = s.host_user_id
         WHERE s.id = $1
        """,
        session_id,
    )
    if row is None:
        raise RoomError("no_room", "no such session")
    people = await conn.fetch(
        """
        SELECT p.id, p.seat, p.role, p.user_id, p.answered_count, p.ended_by,
               u.name, u.avatar
          FROM session_participant p
          LEFT JOIN app_user u ON u.id = p.user_id
         WHERE p.session_id = $1
         ORDER BY p.seat
        """,
        session_id,
    )
    by_seat = vetoes_by_seat({"vetoes_by": row["vetoes_by"]})
    return {
        "session_id": row["id"],
        "room_code": row["room_code"],
        "state": row["state"],
        "kind": row["kind"],
        "runtime_budget_min": row["runtime_budget_min"],
        "include_rewatches": row["include_rewatches"],
        "started_at": row["started_at"],
        "host": {"user_id": row["host_user_id"], "name": row["host_name"]},
        # §6.2 step 1's "not tonight" control: the union the pool excludes (decisions 480, 505).
        "vetoes": _vetoes_payload(
            vetoes_of({"vetoes": row["vetoes"], "vetoes_by": row["vetoes_by"]})
        ),
        "veto_options": [{"key": k, "label": label} for k, (label, _) in VETOES.items()],
        "seats": [
            {
                "participant_id": p["id"],
                "seat": p["seat"],
                "role": p["role"],
                "user_id": p["user_id"],
                # A guest has no account (§4.2), so no invented name.
                "name": p["name"] or f"Guest {p['seat'] - 1}",
                "avatar": p["avatar"],
                "answered_count": p["answered_count"],
                "ended_by": p["ended_by"],
                # This member's own vetoes (decision 505).
                "vetoes": _vetoes_payload(by_seat.get(p["id"], [])),
            }
            for p in people
        ],
    }


async def open_rooms(conn: asyncpg.Connection, *, viewer_id: int) -> list[dict[str, Any]]:
    """§6.2 step 2's open-rooms list: every live room, visible to every household device."""
    rows = await conn.fetch(
        """
        SELECT s.id, s.room_code, s.state, s.kind, s.runtime_budget_min, s.include_rewatches,
               s.started_at, u.name AS host_name, s.context -> 'vetoes' AS vetoes,
               s.context -> 'vetoes_by' AS vetoes_by,
               count(p.id) AS seated,
               bool_or(p.user_id = $1) AS viewer_seated
          FROM session s
          JOIN app_user u ON u.id = s.host_user_id
          LEFT JOIN session_participant p ON p.session_id = s.id
         WHERE s.ended_at IS NULL
         GROUP BY s.id, u.name
         ORDER BY s.started_at DESC
        """,
        viewer_id,
    )
    return [
        {
            "session_id": r["id"],
            "room_code": r["room_code"],
            "state": r["state"],
            "host": r["host_name"],
            "started_at": r["started_at"],
            "kind": r["kind"],
            "runtime_budget_min": r["runtime_budget_min"],
            # The spec's own words for the toggle, so the row reads the way §6.2 writes it.
            "skips_seen": not r["include_rewatches"],
            "seated": int(r["seated"]),
            "viewer_seated": bool(r["viewer_seated"]),
            # A started room is listed but not joinable.
            "joinable": r["state"] == STATE_OPEN and not r["viewer_seated"],
            # What tonight has already ruled out, whoever ruled it out (decisions 480, 505).
            "vetoes": _vetoes_payload(
                vetoes_of({"vetoes": r["vetoes"], "vetoes_by": r["vetoes_by"]})
            ),
        }
        for r in rows
    ]


def _decoded(value: Any, empty: Any) -> Any:
    """A jsonb value decoded by `db/pool.py`'s codec, or a raw string where there is none."""
    if isinstance(value, str):
        value = json.loads(value)
    return value if value else empty


def vetoes_by_seat(context: Any) -> dict[int, list[str]]:
    """Each seated member's own known veto keys, by participant id; a seat with none is absent."""
    ctx = _decoded(context, {})
    out: dict[int, list[str]] = {}
    for seat, keys in _decoded(ctx.get("vetoes_by"), {}).items():
        known = [k for k in VETOES if k in set(keys or [])]
        if known:
            out[int(seat)] = known
    return out


def vetoes_of(context: Any) -> list[str]:
    """The veto keys the room's pool excludes: the union of every seat's own (decision 505).

    A pre-505 room-wide `vetoes` is still read; a retired key stops vetoing rather than failing.
    """
    ctx = _decoded(context, {})
    union = set(_decoded(ctx.get("vetoes"), []))
    for keys in vetoes_by_seat(ctx).values():
        union.update(keys)
    return [k for k in VETOES if k in union]


def _vetoes_payload(keys: Sequence[str]) -> list[dict[str, str]]:
    return [{"key": k, "label": VETOES[k][0]} for k in keys]


async def set_vetoes(
    conn: asyncpg.Connection, *, session_id: int, user_id: int, keys: Sequence[str]
) -> list[str]:
    """Replace THIS member's "not tonight" vetoes (decisions 480, 505): any seat, before Start.

    One statement merges this seat's list into `vetoes_by`, so two phones cannot overwrite each
    other, and its state predicate refuses a veto racing Start.
    """
    unknown = sorted(set(keys) - set(VETOES))
    if unknown:
        raise RoomError("bad_veto", f"{unknown} are not on the list")
    wanted = [k for k in VETOES if k in set(keys)]
    if len(wanted) > MAX_VETOES:
        raise RoomError("bad_veto", f"at most {MAX_VETOES} vetoes each")
    seat = await conn.fetchval(
        "SELECT id FROM session_participant WHERE session_id = $1 AND user_id = $2",
        session_id, user_id,
    )
    if seat is None:
        raise RoomError("not_seated", "only somebody in the room can change what it rules out")
    moved = await conn.fetchval(
        "UPDATE session SET context = jsonb_set(context, '{vetoes_by}', "
        "  coalesce(context -> 'vetoes_by', '{}'::jsonb) "
        "  || jsonb_build_object($2::text, to_jsonb($3::text[]))) "
        "WHERE id = $1 AND state = $4 AND ended_at IS NULL RETURNING id",
        session_id, str(seat), wanted, STATE_OPEN,
    )
    if moved is None:
        raise RoomError("started", "the room has started, so tonight's list is already built")
    return wanted


async def set_state(conn: asyncpg.Connection, session_id: int, state: str) -> None:
    """Move the room along. 0013's CHECK ties `ended_at` to the two ended states, so the two
    cannot drift apart."""
    ended = state in (STATE_RESOLVED, STATE_ABANDONED)
    await conn.execute(
        "UPDATE session SET state = $2, ended_at = CASE WHEN $3 THEN now() ELSE NULL END "
        "WHERE id = $1",
        session_id, state, ended,
    )


async def end_session(conn: asyncpg.Connection, session_id: int) -> None:
    """The host closes the evening, started or not (decision 169). Abandoned, never deleted.

    `FOR UPDATE` and the `ended_at` refusal keep a resolved evening resolved.
    """
    async with conn.transaction():
        live = await conn.fetchval(
            "SELECT id FROM session WHERE id = $1 AND ended_at IS NULL FOR UPDATE", session_id
        )
        if live is None:
            raise RoomError("no_room", "that evening has already ended")
        await set_state(conn, session_id, STATE_ABANDONED)


async def members_to_invite(
    conn: asyncpg.Connection, *, session_id: int, host_user_id: int
) -> Sequence[int]:
    """Who §6.2 step 2's push goes to: every active member not the host and not seated."""
    rows = await conn.fetch(
        """
        SELECT u.id FROM app_user u
         WHERE u.is_active AND u.role IN ('admin', 'member') AND u.id <> $2
           AND NOT EXISTS (SELECT 1 FROM session_participant p
                            WHERE p.session_id = $1 AND p.user_id = u.id)
        """,
        session_id, host_user_id,
    )
    return [r["id"] for r in rows]


async def invite(
    conn: asyncpg.Connection,
    send: Callable[[asyncpg.Connection, int, dict[str, Any]], Awaitable[Any]],
    *,
    session_id: int,
    host_user_id: int,
    room_code: str,
) -> None:
    """§6.2 step 2's push to members' phones. Never fatal, by construction.

    `tag` keys the notification on the session so it replaces nothing else; `url` is the room's
    own join link (decision 481).
    """
    invited = await members_to_invite(
        conn, session_id=session_id, host_user_id=host_user_id
    )
    for user_id in invited:
        try:
            await send(
                conn, user_id,
                {"kind": "tonight.invite", "session_id": session_id, "room_code": room_code,
                 "title": "Tonight", "body": f"A room is open — {room_code}",
                 "tag": f"tonight:{session_id}", "url": f"/tonight?room={room_code}"},
            )
        except Exception:
            # Best-effort per member (§6 preamble); WARNING with traceback, so a sender bug shows.
            log.warning(
                "push invitation to user %s was not delivered", user_id, exc_info=True
            )


__all__ = [
    "CODE_DIGITS",
    "CODE_LENGTH",
    "CODE_LETTERS",
    "MAX_GUESTS",
    "ROLE_GUEST",
    "ROLE_HOST",
    "ROLE_MEMBER",
    "RoomError",
    "STATE_ABANDONED",
    "STATE_BALLOT",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_VOTING",
    "end_session",
    "invite",
    "join",
    "lobby",
    "make_code",
    "members_to_invite",
    "open_rooms",
    "open_session",
    "resolve_code",
    "seats_of",
    "set_state",
    "set_vetoes",
    "vetoes_by_seat",
    "vetoes_of",
]
