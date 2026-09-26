"""§6.2 step 6's blind approval ballot, and the approval share §13 evaluates M4 on.

Blind is a property of the read: `tally` returns nothing until every seat has submitted. The
share is persisted, not derived on read, so later code cannot move it (§14 risk 6).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg

from spielplan.tonight import rooms

# Advisory-lock namespace for `submit`, keyed per participant: simultaneous seats must not queue.
# Differs from `play._FINISH_LOCK`, so a ballot never waits on a combine.
_SUBMIT_LOCK = 6206


class BallotError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


async def slate_of(conn: asyncpg.Connection, session_id: int) -> list[dict[str, Any]]:
    """The three finalists and the wildcard, in slate order: what the ballot is over (54e)."""
    rows = await conn.fetch(
        """
        SELECT r.title_id, r.rank, r.slot, r.group_score, r.per_user_match, r.conflict,
               t.name, t.year, t.runtime_min, t.kind, t.poster_path, t.jellyfin_id
          FROM session_result r JOIN title t ON t.id = r.title_id
         WHERE r.session_id = $1 AND r.slot IN ('finalist', 'wildcard')
         ORDER BY r.rank
        """,
        session_id,
    )
    return [dict(r) for r in rows]


async def submit(
    conn: asyncpg.Connection, *, participant_id: int, approved: Sequence[int]
) -> dict[str, Any]:
    """One participant's approvals. Multi-select, and re-submitting replaces rather than adds.

    Every slate title gets a row, so an empty ballot is a real answer. The advisory lock makes a
    double submit wait and replace whole; the state is re-checked under `FOR SHARE` so a submit
    racing `resolve` is refused rather than written into a resolved room.
    """
    row = await conn.fetchrow(
        "SELECT p.id, p.session_id, s.state FROM session_participant p "
        "JOIN session s ON s.id = p.session_id WHERE p.id = $1",
        participant_id,
    )
    if row is None:
        raise BallotError("no_seat", "no such participant")
    if row["state"] != rooms.STATE_BALLOT:
        raise BallotError("not_ballot", "this session is not taking approvals")

    slate = [r["title_id"] for r in await slate_of(conn, row["session_id"])]
    unknown = set(approved) - set(slate)
    if unknown:
        raise BallotError("not_on_slate", f"{sorted(unknown)} are not on tonight's slate")

    async with conn.transaction():
        # First, so the loser's DELETE sees the rows it replaces.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _SUBMIT_LOCK, participant_id)
        state = await conn.fetchval(
            "SELECT state FROM session WHERE id = $1 FOR SHARE", row["session_id"]
        )
        if state != rooms.STATE_BALLOT:
            raise BallotError("not_ballot", "this session is not taking approvals")
        # `FOR SHARE`: simultaneous seats must not queue, and `resolve`'s `FOR UPDATE` waits on it.
        await conn.execute(
            "DELETE FROM session_ballot WHERE participant_id = $1", participant_id
        )
        for title_id in slate:
            await conn.execute(
                "INSERT INTO session_ballot (session_id, participant_id, title_id, approved) "
                "VALUES ($1, $2, $3, $4)",
                row["session_id"], participant_id, title_id, title_id in set(approved),
            )
    return {"submitted": True, "approved": sorted(set(approved))}


async def submitted_count(conn: asyncpg.Connection, session_id: int) -> tuple[int, int]:
    """(submitted, seated). The waiting screen's only number, and the reveal's condition."""
    seated = await conn.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1", session_id
    )
    submitted = await conn.fetchval(
        "SELECT count(DISTINCT participant_id) FROM session_ballot WHERE session_id = $1",
        session_id,
    )
    return int(submitted or 0), int(seated or 0)


async def everyone_submitted(conn: asyncpg.Connection, session_id: int) -> bool:
    submitted, seated = await submitted_count(conn, session_id)
    return seated > 0 and submitted >= seated


async def tally(conn: asyncpg.Connection, session_id: int) -> list[dict[str, Any]]:
    """Approvals per title, **only after everyone has submitted**; guarded here, where it can leak."""
    if not await everyone_submitted(conn, session_id):
        raise BallotError("still_voting", "approvals stay hidden until everyone has submitted")
    rows = await conn.fetch(
        """
        SELECT b.title_id, count(*) FILTER (WHERE b.approved) AS approvals,
               r.group_score, r.slot
          FROM session_ballot b
          JOIN session_result r ON r.session_id = b.session_id AND r.title_id = b.title_id
         WHERE b.session_id = $1
         GROUP BY b.title_id, r.group_score, r.slot
        """,
        session_id,
    )
    # 54e: "The winner is the title with the most approvals, **ties broken by group score**."
    return sorted(
        ({"title_id": r["title_id"], "approvals": int(r["approvals"]),
          "group_score": float(r["group_score"]), "slot": r["slot"]} for r in rows),
        key=lambda x: (-x["approvals"], -x["group_score"], x["title_id"]),
    )


async def _stored_outcome(conn: asyncpg.Connection, session_id: int) -> dict[str, Any] | None:
    """§13's row for this evening, if it has one."""
    row = await conn.fetchrow(
        "SELECT chosen_title_id, approval_share, participants FROM session_outcome "
        "WHERE session_id = $1",
        session_id,
    )
    if row is None:
        return None
    return {
        "chosen_title_id": row["chosen_title_id"],
        "approval_share": float(row["approval_share"]),
        "participants": row["participants"],
    }


async def resolve(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Pick the winner, persist §13's number, and end the evening. Idempotent.

    The tally and its row are one transaction under the session's `FOR UPDATE`, which `submit`
    waits for; the cheap idempotent read outside the lock serves the reveal's polls.
    """
    existing = await _stored_outcome(conn, session_id)
    if existing is not None:
        return existing

    async with conn.transaction():
        await conn.execute("SELECT id FROM session WHERE id = $1 FOR UPDATE", session_id)
        # Re-asked under the lock: two devices can both have found no outcome.
        existing = await _stored_outcome(conn, session_id)
        if existing is not None:
            return existing

        counted = await tally(conn, session_id)
        if not counted:
            raise BallotError("no_slate", "there is nothing to resolve")
        _, seated = await submitted_count(conn, session_id)
        winner = counted[0]
        share = winner["approvals"] / seated if seated else 0.0

        await conn.execute(
            "INSERT INTO session_outcome "
            "(session_id, chosen_title_id, approval_share, participants) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (session_id) DO NOTHING",
            session_id, winner["title_id"], share, seated,
        )
        await rooms.set_state(conn, session_id, rooms.STATE_RESOLVED)
    return {
        "chosen_title_id": winner["title_id"],
        "approval_share": share,
        "participants": seated,
        "unanimous": winner["approvals"] == seated,
    }


__all__ = [
    "BallotError",
    "everyone_submitted",
    "resolve",
    "slate_of",
    "submit",
    "submitted_count",
    "tally",
]
