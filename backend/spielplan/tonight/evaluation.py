"""The one read path §13 admits for judging the round. Spec v2.1 §13, §14 risk 6; 54b.

    §13: "the 10% uniform-random comparison stream is the *only* data used to evaluate the tier
    model — adaptively-selected pairs inflate reliability (measured effect; the guard is
    non-negotiable)."
    54b, binding it to Tonight: hold-out pairs are "held out, exactly as §13's tier-queue stream
    is, and … the only data admissible for evaluating whether the round works". Extend §13's
    rows with "shortlist stability — how often the held-out pairs agree with the adaptive
    shortlist; and the rate at which the cap and the escape control fire."

THIS MODULE IS THE OTHER HALF OF THE GUARD. `round.replay` makes sure the hold-out stream never
reaches the model; this makes sure nothing else reaches the evaluation. The two are separate
functions in separate modules on purpose: M3 found `duel.selection` had four read paths where
it looked like one, and the lesson was that an exclusion enforced in the place that *writes* is
not an exclusion enforced in the place that *reads*.

WHY THE FIGURE IS THE ONE IT IS. An adaptive answer cannot say whether the shortlist is right:
the shortlist was built from it. A hold-out answer can, because the round never saw it — so
"did the participant prefer the finalist over the non-finalist, on a pair nobody chose for
them?" is a question with an honest answer. `n` travels with every rate for the reason M3's
`Agreement.rate` returns None on an empty sample: a rate over three pairs is not a measurement,
and §0 fixes a noise floor that calls anything under 0.003–0.008 a tie.
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
        """None rather than 0.0 on an empty sample — a rate over no pairs is not a number, and
        printing 0.00 next to it is how an absent measurement gets read as a bad one."""
        return None if not self.decisive else self.agreed / self.decisive

    def as_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs, "decisive": self.decisive, "agreed": self.agreed,
            "rate": self.rate,
        }


async def held_out_answers(
    conn: asyncpg.Connection, session_id: int
) -> list[dict[str, Any]]:
    """Every hold-out answer of one session, and **nothing else**.

    The `selection = 'uniform_holdout'` predicate is the whole module: a caller that wanted
    "all the answers" would be asking a different question, and the two must not share a
    function. Retracted rows are excluded — §6's undo means the answer no longer counts, and an
    evaluation that scored a retracted answer would be measuring a tap the person took back.
    """
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
    """54b's "shortlist stability — how often the held-out pairs agree with the adaptive
    shortlist".

    A hold-out pair with one finalist and one non-finalist is decisive: the shortlist says the
    finalist wins, and the participant said something. A pair with two finalists or two
    non-finalists says nothing about the *boundary*, and neither does `either` or `neither` —
    both are level answers, and folding them into a side would be inventing a threshold the
    person did not cross. They are counted in `pairs` and excluded from `decisive`, so the
    denominator is visible rather than implied.
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
    """54b's second added row, and §14 risk 6's own words: "the rate at which the cap and the
    escape control fire". Counted per session so a household's rate is a sum rather than a
    figure recomputed over a moving window."""
    rows = await conn.fetch(
        "SELECT ended_by, count(*) AS n FROM session_participant "
        "WHERE session_id = $1 AND ended_by IS NOT NULL GROUP BY ended_by",
        session_id,
    )
    counted = {r["ended_by"]: int(r["n"]) for r in rows}
    return {reason: counted.get(reason, 0) for reason in round_rules.END_REASONS}


async def report(conn: asyncpg.Connection, session_id: int) -> dict[str, Any]:
    """Everything §13 and §14 risk 6 ask of one evening, in one object.

    Deliberately not a per-title read: §13 evaluates the *round*, and a report that could name
    a candidate would invite the surfaces to render it, which is how an evaluation stream stops
    being held out.
    """
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
