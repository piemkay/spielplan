"""The session's life: opening a room, seating people, and the open-rooms list.

Spec v2.1 §6.2 steps 1 and 2, §4.2, §11; §12's M4 row ("lobby + open-rooms discovery").

    "**2. Join channels, all equivalent:** push to members' phones (best-effort, §6 preamble);
     **room code / QR** in the lobby; a live in-app lobby banner over the WebSocket; the
     **open-rooms list** — active sessions are visible to every household device ('MX-2210 ·
     hosted by Mia · 3 min ago · Film · 60 min · skips seen') with tappable empty seats.
     **Guests use the initiator's phone after the initiator finishes** (hand-the-phone,
     sequential turns)."

The quotation is three channels and not four because decision 165 struck the fourth from the
sentence itself (spec:249): a quotation that kept it would be quoting a line the spec no
longer has, which is the one way a docstring can be wrong about the spec it cites.

"ALL EQUIVALENT" IS A CLAIM ABOUT WHAT A JOIN PRODUCES, not a list of affordances. Every
channel lands in `join`, and `join` is idempotent per member: a person who arrives by code and
then taps the same room in the open-rooms list re-attaches to the seat they already have. Two
seats for one member would change the participant count that every average, every tally and
§13's approval share are computed over — silently, and in the direction that makes a household
look bigger than it is. The schema holds the line too (0013's partial unique index), so the
rule survives a caller that forgets it.

THE GUEST SEAT IS NOT AN ACCOUNT. §4.2: "user_id NULL — NULL = guest slot on the host phone".
Guests are seats with no user, seated at open time by count, and they take their turns after
the initiator on the initiator's device. `seat` carries the order.

THE ROOM CODE IS THE CHANNEL THAT STILL WORKS. §6 preamble makes push best-effort and the
WebSocket needs the app open; the code needs neither, and §11 hands it to a Home Assistant
dashboard. It is unique among *live* rooms — two live rooms sharing a code walks a household
member into the wrong evening — and reusable afterwards, because a code is a handle on a room
and not a permanent name.
"""

from __future__ import annotations

import logging
import random
import string
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import asyncpg

from spielplan.tonight.pool import Seat

log = logging.getLogger("spielplan.tonight.rooms")

# §6.2's own example is `MX-2210`. Two letters, a dash, four digits — read aloud across a room
# and typed on a phone, so I and O and 0 and 1 are out: a code nobody can dictate is not a
# channel that works when push does not.
CODE_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IO")
CODE_DIGITS = "23456789"
CODE_LENGTH = 4

# §6.2 step 1: "members and/or N guests". Guests share one phone and take turns in sequence, so
# the cap is a fact about an evening rather than about the schema.
MAX_GUESTS = 6

STATE_OPEN = "open"
STATE_VOTING = "voting"
STATE_BALLOT = "ballot"
STATE_RESOLVED = "resolved"
STATE_ABANDONED = "abandoned"
LIVE_STATES = (STATE_OPEN, STATE_VOTING, STATE_BALLOT)

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

    The check and the insert are one loop rather than two steps. 0013's partial unique index is
    what actually keeps two live rooms from sharing a code — the check only keeps the common
    case off it — and between a check that passed and an insert that follows, another device can
    take the code. Two people tapping "Together" at the same moment is the ordinary event in a
    household, not the rare one, and the cost of not catching it here is the main control of the
    surface answering with the name of a database index — `app.py` maps a lost uniqueness race to
    409 `conflict: session_room_code_live` — where §6.8's register wants a room. The retry is the
    same answer `sync/playback.py` gives a lost race.

    Bounded, then a plain failure: an unbounded loop on a 32^2 * 8^4 space that is somehow
    exhausted is a hang rather than an error.
    """
    for _ in range(20):
        code = make_code(rng)
        taken = await conn.fetchval(
            "SELECT 1 FROM session WHERE upper(room_code) = upper($1) AND ended_at IS NULL", code
        )
        if taken:
            continue
        try:
            # A savepoint, not a bare statement: a UniqueViolationError aborts the transaction
            # it lands in, so a caller that wrapped this loop in one would find the retry itself
            # refused. `conn.transaction()` is a savepoint when nested and a real transaction
            # when not, which is the same line for both callers.
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
    """Open a room. The host takes seat 1; guest seats follow, in turn order.

    Guests are seated at open time rather than as they arrive, because they do not arrive — the
    host says how many are on the sofa. Their seats exist from the start so the lobby can show
    them and the round can refuse to open a guest's turn early (§6.2 step 2's sequential
    hand-off).
    """
    if guests < 0 or guests > MAX_GUESTS:
        raise RoomError("guest_count", f"between 0 and {MAX_GUESTS} guests")
    rng = rng or random.SystemRandom()

    # One transaction, because the room, its seats and the abandonment below are one fact. Every
    # statement here used to stand alone, which was survivable while none of them took anything
    # away — and stopped being survivable when the abandon arrived.
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

        # 0013 admits `abandoned` and only a result ever ended a room, so one opened and drifted
        # away from stayed live forever: on §6.2 step 2's list for every household device with
        # an age that only grows, and still holding the host's seat, so every later visit to the
        # surface restored them into a room nobody was in. A host cannot be hosting two rooms
        # nobody has started, and the second tap is the one moment that intent is unambiguous —
        # so it is read here rather than by adding a control the spec does not name. Only `open`
        # rooms: a round in progress is people answering on their own devices, and 54e's reveal
        # waits for every seat.
        #
        # LAST, and not first. Allocating a code can fail after twenty collisions, and every
        # insert above can fail; done first, any of those left the host's previous evening
        # abandoned and no new room in its place — a tap on "Together" that answers 409 and
        # takes the room they were in. `id <> $1` because the room just made is the host's and
        # is `open`, so it would otherwise abandon itself.
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

    A member who is already seated gets their existing seat back, whichever channel they used.
    The alternative is a second seat, which changes the participant count every average and
    §13's approval share are computed over, in the direction that makes a household look bigger
    than it is.

    THE SESSION'S STATE IS READ INSIDE THE LOOP, AND THE RULE IS A PROPERTY OF THE INSERT. The
    read used to sit above the loop and the INSERT carried no predicate, so "anyone who joins
    before you start is in" was decided from a row that was already minutes old by the time the
    statement ran — and `play.start` took tens of seconds to build its pool, which is exactly how
    long the window was. The seat that landed in it had no entry in the frozen `scores` map, and
    from there nothing could end the round or close the room. [M4.12 finding 5]

    `FOR SHARE` IS WHAT MAKES THE TWO STATEMENTS SERIALISE, and the predicate alone does not.
    `play.start` claims the room in its first statement and holds that claim in a transaction for
    the whole pool build, so an ordinary MVCC read here still sees `open` and seats the member
    anyway (measured: the insert succeeds mid-claim). The share lock waits for that transaction
    instead and then re-evaluates: refused once the claim commits, admitted if it rolled back —
    and a join that takes the lock first makes `start`'s own UPDATE wait, so its `seats_of` sees
    this seat. One statement's worth of lock on one row, which is the cost of not needing either
    side to be lucky.
    """
    # Read, decide, insert — and everything between those is another device doing the same
    # thing. The code, the banner and the open-rooms row are "all equivalent", and two of them
    # are one tap apart on the same screen, so arriving twice AT ONCE is as ordinary as arriving
    # twice in a row. 0013's two indexes are what actually keep one member to one seat and one
    # seat to one member; losing the race made them do it by raising into the route, and the
    # person was told the join had failed on a room they were by then in. Re-read and try again.
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
            # §6.2 says nothing about when joining closes, so the smallest rule that keeps a
            # round coherent: once the pairs are being served, the participant set the pool was
            # built for is fixed. A late arrival joins the next session. Checked here as well as
            # in the statement below because this is where the reason gets its sentence — the
            # INSERT can only return nothing.
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
            # The room moved while this statement waited on it, which is the one outcome the
            # stale read above could not produce.
            raise RoomError("started", "that room has already started")
        return {"participant_id": participant_id, "seat": seat, "role": ROLE_MEMBER,
                "created": True}
    raise RoomError("seat_race", "too many devices sat down at once — try again")


async def seats_of(conn: asyncpg.Connection, session_id: int) -> list[Seat]:
    """The session's seats, as the pool needs them.

    `is_member` is the taste question (§6.2 step 3: "Guests contribute no taste term unless they
    have a grid profile"), so it is `role <> 'guest'` today and will be `role <> 'guest' OR has
    a grid profile` when M7 ships the grid.
    """
    rows = await conn.fetch(
        "SELECT id, user_id, role FROM session_participant WHERE session_id = $1 ORDER BY seat",
        session_id,
    )
    return [
        Seat(participant_id=r["id"], user_id=r["user_id"], is_member=r["role"] != ROLE_GUEST)
        for r in rows
    ]


async def lobby(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Everything the lobby screen renders — which, since decision 165, is every surface that
    renders a room at all.

    Deliberately carries no candidate, no pool and no ranking: §6.2 step 3's pool is "internal —
    never shown as a step", and the lobby is the screen most likely to leak it.
    """
    row = await conn.fetchrow(
        """
        SELECT s.id, s.room_code, s.state, s.kind, s.runtime_budget_min, s.include_rewatches,
               s.started_at, s.ended_at, s.host_user_id, u.name AS host_name
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
    return {
        "session_id": row["id"],
        "room_code": row["room_code"],
        "state": row["state"],
        "kind": row["kind"],
        "runtime_budget_min": row["runtime_budget_min"],
        "include_rewatches": row["include_rewatches"],
        "started_at": row["started_at"],
        "host": {"user_id": row["host_user_id"], "name": row["host_name"]},
        "seats": [
            {
                "participant_id": p["id"],
                "seat": p["seat"],
                "role": p["role"],
                "user_id": p["user_id"],
                # A guest is "Guest 1" and not a person's name: §4.2 gives them no account, and
                # inventing one on screen is how a guest ends up looking like a member.
                "name": p["name"] or f"Guest {p['seat'] - 1}",
                "avatar": p["avatar"],
                "answered_count": p["answered_count"],
                "ended_by": p["ended_by"],
            }
            for p in people
        ],
    }


async def open_rooms(conn: asyncpg.Connection, *, viewer_id: int) -> list[dict[str, Any]]:
    """§6.2 step 2's open-rooms list: "active sessions are visible to **every household
    device**".

    Every live room, not only the viewer's own — that is the whole feature. The row carries the
    six facets the spec's example string names, plus whether this viewer already has a seat, so
    the client can render "tappable empty seats" without a second round trip.
    """
    rows = await conn.fetch(
        """
        SELECT s.id, s.room_code, s.state, s.kind, s.runtime_budget_min, s.include_rewatches,
               s.started_at, u.name AS host_name,
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
            # A room that has started is still listed — the household can see the evening is
            # happening — but its seat is not tappable. Hiding it would be a worse lie.
            "joinable": r["state"] == STATE_OPEN and not r["viewer_seated"],
        }
        for r in rows
    ]


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
    """The host closes the evening. §6.2 step 2's open-rooms list; §14 risk 6; decision 169.

    NOTHING COULD END A ROOM THAT HAD STARTED. `open_session`'s abandon clause above takes rooms in
    state `open` only, and it says why: a round in progress is people answering on their own phones,
    and 54e's reveal waits for every seat. There is no Tonight job in `worker.py`'s registry either.
    So a room that reached `voting` and lost a seat — a phone that never came back, a seat the
    frozen snapshot had no scores for — was live for ever: on §6.2 step 2's list for every
    household device with an age that only grows, still holding each member's seat so every later
    visit to the surface restored them into it, and unfinishable. Recovery was SQL against
    `session`. Decision 169 ships the control in the same diff as `play.settle`, and the two are one
    lifecycle: `settle` moves a room whose seats have all ended, and this is what the host taps when
    they never will. Not a 6 h idle sweep — that is M4.7's worker registry if it is built at all,
    and a household that wants its evening back wants it now.

    ABANDONED, NEVER DELETED. §14 risk 6 reads `session_answer`, `session_participant.ended_by` and
    `session_outcome` for the rates it asks for, and the evenings a household cut short are exactly
    the evenings those rates are about. 0013's `session_ended_states` CHECK already ties `ended_at`
    to this state, so `set_state` is the whole write and no migration is owed.

    THE ROW IS TAKEN `FOR UPDATE` AND THE REFUSAL IS WHAT KEEPS A RESOLVED EVENING RESOLVED. Two
    taps on End, or an End from a lobby whose room has since reached its reveal, would otherwise
    rewrite `state` over `resolved` and leave `session_outcome` hanging off a room recorded as
    abandoned. The lock is M4.10's idiom (`api/deps.py`'s `write_txn`) at a second seam, and it buys
    one more thing: `play.start` claims the session row in its first statement and holds it for the
    whole pool build, so an End arriving mid-claim waits and then ends the started room rather than
    racing the claim over the same row. Any live room, whatever it is doing — a host who taps End
    on a room nobody started is saying the same thing as one who taps it on a round.
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
    """Who §6.2 step 2's push invitation goes to: every household member who is not the host
    and not already seated. Admin and member alike — §3.1 makes admin "full product, plus
    admin", not a different kind of household."""
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

    THE SENDER IS AN ARGUMENT. It used to be imported inside a closure in `api/tonight.py`, which
    made "who is invited" and "what the invitation says" observable only through the ASGI app and
    a push transport — two rules about an evening, testable only with a web-push stack standing
    up. The callable is also what lets the route hand the whole dispatch to a background task
    with a connection of its own (arch-06, finding 42). `api/tonight.py` still resolves the
    import, because a household whose SECRETS_KEY is unset (§3.1's half-configured boot) must be
    able to open a room and be joined by code, and that is the route's question rather than this
    module's.

    `tag` and `url` are the two fields the service worker cannot invent (syncpush-10). A tag is a
    replacement key, and with none set every notification the household received took the
    worker's default and overwrote the previous one whatever it was: a §6.2 invitation silently
    replaced an unread §7.3 finish prompt, on the phone, where the banner fallback is not the
    thing the member is looking at. Keying it on the session makes two rooms two notifications
    and a re-announced room one. `url` is where a tap lands, and §6.2 step 2's answer to an
    invitation is the lobby, not Home. The service worker stays a dumb renderer and keeps its
    default for a sender that sets neither.
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
                 "tag": f"tonight:{session_id}", "url": "/tonight"},
            )
        except Exception:
            # Best-effort, per member: §6's preamble guarantees an in-app equivalent for every
            # push, and the room code and the open-rooms list are both already live. One
            # member's unreachable phone is not the next member's problem, which is why the
            # try sits inside the loop.
            #
            # WARNING with the traceback, not INFO with a sentence. This was logged at INFO
            # saying only that a delivery had not happened, so a real sender bug — a malformed
            # payload, an expired VAPID key, a TypeError in the encryption path — looked exactly
            # like a phone that was off, at a level nothing in §7.3's operator story reads.
            # [M4.12 finding 42]
            log.warning(
                "push invitation to user %s was not delivered", user_id, exc_info=True
            )


__all__ = [
    "CODE_DIGITS",
    "CODE_LENGTH",
    "CODE_LETTERS",
    "LIVE_STATES",
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
]
