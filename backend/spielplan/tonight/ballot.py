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

# The namespace half of `submit`'s advisory lock, keyed per participant rather than per session:
# two seats submitting at the same moment is the normal case on 54e's simultaneous reveal and must
# not queue, while one seat submitting twice is the case that collided. Two ints, the same space
# `play.finish`'s `_FINISH_LOCK` takes and a different number, so a ballot can never wait on a
# combine; deliberately not `api/deps.write_txn`'s single-argument `hashtext(...)` space, which is
# a different space again (`deps.py`'s docstring says why).
_SUBMIT_LOCK = 6206


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

    AND "REPLACES" IS DELETE-THEN-INSERT, WHICH NEEDED A LOCK TO MEAN ANYTHING. Without one the
    second of two overlapping submits could not see the first's uncommitted rows: its DELETE
    removed nothing, its INSERTs collided with `session_ballot_one_per_title`, and `app.py`'s
    `_conflict` answered 409 `conflict: session_ballot_one_per_title` — so on the one moment §6.2
    step 6 makes social the phone was shown the name of a database index, and the whole of that
    submission was rolled back with it: the DELETE and every INSERT are one transaction, so the
    ballot standing afterwards is the OTHER tab's, not the merge of the two the constraint name
    suggests. A double tap and a second tab are both ordinary. Probe:
    `['UniqueViolationError', 'dict']`, 4 rows, state correct.
    [M4.12 finding 12; M4.12 review cycle 1: M412-CONC-02 — this paragraph named the wrong status,
    and the seam has answered 409 since M4.7's `_conflict`; the repair is unchanged]

    The lock rather than `ON CONFLICT ... DO UPDATE` plus a DELETE of rows no longer on the slate,
    for `finish`'s reason: it makes the loser WAIT and then do the whole thing correctly, so one
    of the two ballots stands entire rather than the pair of them merging into a third nobody
    cast. 0013's unique index stays the backstop, and nothing here catches its violation — a
    repair that caught one would be the same check-then-act with the race moved into an except
    branch.

    THE OTHER WRITER ON THE SAME BEAT IS `resolve`, AND THE LOCK ABOVE DOES NOT MEET IT. That lock
    is keyed per participant, so it serialises one person's two submits and nothing else — while
    the state question at the top of this function is a check-then-act on an autocommit connection
    that `resolve` can walk through: a changed-mind re-submit that passed the guard commits AFTER
    `resolve` has counted the approvals and stored §13's share, and `session_outcome` then names a
    winner the surviving `session_ballot` rows say nobody approved. Reproduced with the window
    widened: seat A [t1], A re-submits [t2] gathered with B's submit-and-reveal, and the outcome
    said `{chosen: 1, approval_share: 0.5}` over rows in which title 1 had zero approvals. §13
    evaluates M4 on that number and §14 risk 6 keeps those rows as the log it is derived from, so
    the two disagreeing is the one thing this table cannot do.

    So the question is asked again HERE, under the session row `resolve` takes `FOR UPDATE` around
    its tally — the idiom `play.finish` already uses on the same row — and a submit that arrives
    after the reveal is the `not_ballot` refusal the client re-reads on rather than a silent write
    into a resolved room. The read outside the transaction stays: it is the cheap refusal for the
    ordinary case and it is what makes `not_on_slate` answerable without taking a lock at all.
    [M4.12 review cycle 2: M412-CONC-04]
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
        # The first statement, so the loser's DELETE below runs after the winner has committed and
        # therefore sees the rows it is meant to replace.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _SUBMIT_LOCK, participant_id)
        state = await conn.fetchval(
            "SELECT state FROM session WHERE id = $1 FOR SHARE", row["session_id"]
        )
        if state != rooms.STATE_BALLOT:
            raise BallotError("not_ballot", "this session is not taking approvals")
        # `FOR SHARE` and not `FOR UPDATE`: two seats submitting at the same moment is the normal
        # case on 54e's simultaneous reveal and must not queue behind each other, and a shared
        # lock is what `resolve`'s exclusive one has to wait for. Either order is then correct —
        # a submit that gets there first is counted, one that arrives after the reveal reads
        # `resolved` and is refused.
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
    second caller — the session WebSocket, a later feature — would otherwise have to remember
    the rule, and 54e's simultaneity is the whole social point of the round. This clause named
    the TV route first; decision 165 retires that surface and `tally`'s guard is exactly what
    M4.12 KEPT of it, because the callers it was written for are still two.
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


async def _stored_outcome(conn: asyncpg.Connection, session_id: int) -> dict[str, Any] | None:
    """§13's row for this evening, if it has one. Asked twice by `resolve` — once cheaply and
    once under the lock — so it is spelled once."""
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
    """Pick the winner, persist §13's number, and end the evening.

    Idempotent: a second call returns the stored outcome rather than re-deriving it. Two
    devices hitting "reveal" at the same moment is the normal case, and a share that changed
    between them would be the measurement moving under the thing it measures.

    AND THE TALLY AND THE ROW IT IS STORED AS ARE ONE TRANSACTION, HOLDING THE SESSION ROW. The
    count, the winner and the share were computed on an autocommit connection and written after,
    so a re-submit that had already passed `submit`'s state check could commit in between and
    leave `session_outcome` naming a winner the surviving `session_ballot` rows do not approve —
    §13's headline number against §14 risk 6's log of the votes it came from. `FOR UPDATE` on the
    session row is the same lock `play.finish` takes at the other end of the evening and the one
    `submit` now waits for, so the two writers meet: whichever gets there first, the stored share
    is the share of the ballot that is actually in the table.

    The idempotent read stays OUTSIDE the lock and is repeated inside it. Every device polls
    `GET /result` after the reveal and the answer is a single indexed row; taking a row lock on
    every one of those polls would serialise the whole household's reveal on the one write that
    has already happened. [M4.12 review cycle 2: M412-CONC-04]
    """
    existing = await _stored_outcome(conn, session_id)
    if existing is not None:
        return existing

    async with conn.transaction():
        await conn.execute("SELECT id FROM session WHERE id = $1 FOR UPDATE", session_id)
        # Re-asked under the lock, because the cheap read above is a check-then-act like any
        # other: two devices can both have found no outcome and both be here. The INSERT's
        # `ON CONFLICT DO NOTHING` stopped that being a crash; this stops it being a second
        # tally, which is what the idempotence promise is actually about.
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
