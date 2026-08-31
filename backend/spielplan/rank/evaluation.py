"""§13 stream (a) — the one read path allowed to judge the tier model.

Spec v2.1 §13, §6.3; decision 54b, proposal 146.

§13: "the 10% uniform-random comparison stream is the *only* data used to evaluate the tier
model — adaptively-selected pairs inflate reliability (measured effect; the guard is
non-negotiable)."

The guard has two halves and they live in different files. The **exclusion** half is in
`ledger.observations.load_observations`, which never lets a `uniform_holdout` row into the fit.
This is the **admission** half: the only function in the app that reads those rows, and the
only one that reads *nothing else*. Keeping it alone in a module is deliberate — the query
below is the thing a reviewer has to check, and it is easier to check that one file says
`selection = 'uniform_holdout'` and nothing else says it than to audit every query that
mentions `duel`.

WHY AGREEMENT AND NOT SPEARMAN. §13's added rows name "per-user held-out Spearman" for the
*ranking*, which `scoring.foldin` already computes over verdicts. A comparison stream measures
something narrower and more direct: given a pair the model has never been fitted on, does the
model's ordering agree with the person's answer? That is the tier model's own accuracy, on the
only sample that can honestly report it.

TIES ARE COUNTED, NOT SCORED. §4.2: "about the same" is first-class data, 22% of random pairs.
A tie is not a wrong answer and not a right one — scoring it either way needs a threshold on
|Δs| that nothing has measured, and inventing one here would be a tuning constant with no
provenance sitting inside the instrument that exists to keep tuning honest. So ties are
reported and excluded from the rate.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from spielplan.ledger.observations import HELD_OUT


@dataclass(frozen=True)
class Agreement:
    """§13's reading for one (user, kind)."""

    user_id: int
    kind: str
    pairs: int = 0            # held-out duels with both titles placed
    decisive: int = 0         # of those, the ones that were not ties
    ties: int = 0
    agreed: int = 0
    unplaced: int = 0         # held-out duels a coordinate is missing for

    @property
    def rate(self) -> float | None:
        """None rather than 0.0 when there is nothing to report. A held-out sample of zero is
        "not measured yet", and a 0.0 next to it would read as "measured, and terrible"."""
        return self.agreed / self.decisive if self.decisive else None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "pairs": self.pairs,
            "decisive": self.decisive,
            "ties": self.ties,
            "agreed": self.agreed,
            "unplaced": self.unplaced,
            "rate": self.rate,
            "stream": HELD_OUT,
        }


async def held_out_agreement(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> Agreement:
    """How often the model's ordering agrees with a comparison it was never fitted on.

    The `WHERE d.selection = $3` is the whole point of the function. Widening it to "all
    comparisons" would raise the number and destroy its meaning, which is exactly the
    inflation §13 measured and forbade.
    """
    rows = await conn.fetch(
        """
        SELECT d.outcome, a.s AS s_a, b.s AS s_b
        FROM duel d
        JOIN title t ON t.id = d.title_a AND t.kind = $2
        LEFT JOIN ledger_state a ON a.user_id = d.user_id AND a.title_id = d.title_a
        LEFT JOIN ledger_state b ON b.user_id = d.user_id AND b.title_id = d.title_b
        WHERE d.user_id = $1
          AND d.selection = $3
          -- §13 stream (b) is a different instrument: a re-ask measures whether the PERSON
          -- gives the same answer twice, not whether the model agrees with them, and letting
          -- one row in twice would weight it double here as well as in the fit.
          AND NOT d.is_reask
        """,
        user_id,
        kind,
        HELD_OUT,
    )

    pairs = decisive = ties = agreed = unplaced = 0
    for row in rows:
        if row["s_a"] is None or row["s_b"] is None:
            unplaced += 1
            continue
        pairs += 1
        if row["outcome"] == "TIE":
            ties += 1
            continue
        decisive += 1
        predicted = "A" if float(row["s_a"]) > float(row["s_b"]) else "B"
        if predicted == row["outcome"]:
            agreed += 1

    return Agreement(
        user_id=user_id, kind=kind, pairs=pairs, decisive=decisive, ties=ties,
        agreed=agreed, unplaced=unplaced,
    )


__all__ = ["Agreement", "held_out_agreement"]
