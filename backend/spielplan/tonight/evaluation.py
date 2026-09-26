"""The one read path §13 admits for judging the round (54b): held-out answers and nothing else.

`round.replay` keeps held-out answers out of the model; this keeps everything else out of the
evaluation. `n` travels with every rate: a rate over three pairs is not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.tonight import round as round_rules


@dataclass(frozen=True)
class Agreement:
    """How often the held-out answers agree with the shortlist the adaptive round produced."""

    pairs: int
    decisive: int
    agreed: int

    @property
    def rate(self) -> float | None:
        """None on an empty sample: 0.00 would read an absent measurement as a bad one."""
        return None if not self.decisive else self.agreed / self.decisive

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs, "decisive": self.decisive, "agreed": self.agreed,
            "rate": self.rate,
        }


async def held_out_answers(
    conn: asyncpg.Connection, session_id: int
) -> list[dict[str, Any]]:
    """Every hold-out answer of one session, and **nothing else**; retracted answers excluded."""
    rows = await conn.fetch(
        """
        SELECT a.participant_id, a.seq, a.title_a, a.title_b, a.answer
          FROM session_answer a
         WHERE a.session_id = $1
           AND a.selection = $2
           AND a.retracted_at IS NULL
         ORDER BY a.participant_id, a.seq
        """,
        session_id, round_rules.SELECTION_HOLDOUT,
    )
    return [dict(r) for r in rows]


async def shortlist_agreement(conn: asyncpg.Connection, session_id: int) -> Agreement:
    """54b's "shortlist stability": held-out pairs straddling the finalist boundary, agreed or not.

    Other pairs and level answers count in `pairs` but not in `decisive`.
    """
    finalists = {
        r["title_id"]
        for r in await conn.fetch(
            "SELECT title_id FROM session_result WHERE session_id = $1 AND slot = 'finalist'",
            session_id,
        )
    }
    answers = await held_out_answers(conn, session_id)
    decisive = agreed = 0
    for row in answers:
        a_in, b_in = row["title_a"] in finalists, row["title_b"] in finalists
        if a_in == b_in or row["answer"] not in (round_rules.A, round_rules.B):
            continue
        decisive += 1
        chose_a = row["answer"] == round_rules.A
        if chose_a == a_in:
            agreed += 1
    return Agreement(pairs=len(answers), decisive=decisive, agreed=agreed)


async def end_reasons(conn: asyncpg.Connection, session_id: int) -> dict[str, int]:
    """§14 risk 6: "the rate at which the cap and the escape control fire", per session."""
    rows = await conn.fetch(
        "SELECT ended_by, count(*) AS n FROM session_participant "
        "WHERE session_id = $1 AND ended_by IS NOT NULL GROUP BY ended_by",
        session_id,
    )
    counted = {r["ended_by"]: int(r["n"]) for r in rows}
    return {reason: counted.get(reason, 0) for reason in round_rules.END_REASONS}


async def report(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Everything §13 and §14 risk 6 ask of one evening; never per title, so it stays held out."""
    outcome = await conn.fetchrow(
        "SELECT chosen_title_id, approval_share, participants FROM session_outcome "
        "WHERE session_id = $1",
        session_id,
    )
    return {
        "session_id": session_id,
        "approval_share": None if outcome is None else float(outcome["approval_share"]),
        "participants": None if outcome is None else outcome["participants"],
        "shortlist_agreement": (await shortlist_agreement(conn, session_id)).as_dict(),
        "ended_by": await end_reasons(conn, session_id),
    }


__all__ = [
    "Agreement",
    "end_reasons",
    "held_out_answers",
    "report",
    "shortlist_agreement",
]
