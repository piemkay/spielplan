"""§6.2 step 6's blind approval ballot, and the one number §13 evaluates M4 on.

Spec v2.1 §6.2 step 6 (rewritten, 54e), §4.2 `session_ballot` / `session_outcome`, §13,
§14 risk 6.

    "**6. The ballot (blind).** Each participant taps **everything they would be happy with**
     among the three finalists and the wildcard — an approval ballot, not a ranking. Approvals
     stay hidden until every participant has submitted; then they are revealed together. The
     winner is the title with the most approvals, ties broken by group score.
     **Approval share** — the fraction of participants who approved the winner — is the number
     §13 evaluates the whole feature on, and this ballot is the only place it exists."

BLIND IS A PROPERTY OF THE READ, NOT OF THE CLIENT. `results()` returns nothing at all until
every seated participant has submitted. Not "returns them flagged hidden", not "returns them
and the UI declines to draw them" — the rows do not leave the database, because a payload that
carries an approval is one `curl` away from being read whatever the screen does. §6.2 calls the
simultaneity "the blind round's whole social property"; a property enforced in a template is
not a property.

WHY THE SHARE IS PERSISTED RATHER THAN DERIVED ON READ. §4.2 gives `session_outcome` its own
row "feeds §13", and §14 risk 6 forbids tuning the round before it is instrumented. A share
recomputed later would move with whatever the code does next, which is the opposite of a
measurement.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg

from spielplan.tonight import rooms


class BallotError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


async def slate_of(conn: asyncpg.Connection, session_id: int) -> list[dict[str, Any]]:
    """The three finalists and the wildcard, in slate order — what the ballot is over.

    Runners-up are not on the ballot: 54e says "among the three finalists and the wildcard",
    and a ballot over the whole pool is a ranking exercise rather than "everything you would be
    happy with".
    """
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

    Every title on the slate gets a row — approved true or false — rather than only the
    approvals: "has this person submitted?" is then a question about rows existing, which is
    what the reveal condition reads, and an empty ballot ("none of these") is a real answer a
    person can give rather than an absence indistinguishable from not having voted.
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
    """Approvals per title. **Only after everyone has submitted.**

    The guard is here rather than in the route because this is the function that can leak: a
    second caller — the TV route, the WebSocket, a later feature — would otherwise have to
    remember the rule, and 54e's simultaneity is the whole social point of the round.
    """
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


async def resolve(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Pick the winner, persist §13's number, and end the evening.

    Idempotent: a second call returns the stored outcome rather than re-deriving it. Two
    devices hitting "reveal" at the same moment is the normal case, and a share that changed
    between them would be the measurement moving under the thing it measures.
    """
    existing = await conn.fetchrow(
        "SELECT chosen_title_id, approval_share, participants FROM session_outcome "
        "WHERE session_id = $1",
        session_id,
    )
    if existing is not None:
        return {
            "chosen_title_id": existing["chosen_title_id"],
            "approval_share": float(existing["approval_share"]),
            "participants": existing["participants"],
        }

    counted = await tally(conn, session_id)
    if not counted:
        raise BallotError("no_slate", "there is nothing to resolve")
    _, seated = await submitted_count(conn, session_id)
    winner = counted[0]
    share = winner["approvals"] / seated if seated else 0.0

    async with conn.transaction():
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
