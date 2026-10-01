"""§13 stream (a): the one read path allowed to judge the tier model (the fit excludes these rows).

Agreement on held-out pairs, not Spearman. Ties, the person's or the model's, are counted, not scored.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from spielplan.ledger.observations import HELD_OUT, cutover_sql


@dataclass(frozen=True)
class Agreement:
    """§13's reading for one (user, kind)."""

    user_id: int
    kind: str
    pairs: int = 0            # held-out duels with both titles placed
    decisive: int = 0         # of those, the ones both the person and the model took a side on
    ties: int = 0             # the person said "about the same"
    undecided: int = 0        # the model has no ordering: s_a == s_b
    agreed: int = 0
    unplaced: int = 0         # held-out duels a coordinate is missing for

    @property
    def rate(self) -> float | None:
        """None rather than 0.0 when nothing was measured."""
        return self.agreed / self.decisive if self.decisive else None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "pairs": self.pairs,
            "decisive": self.decisive,
            "ties": self.ties,
            "undecided": self.undecided,
            "agreed": self.agreed,
            "unplaced": self.unplaced,
            "rate": self.rate,
            "stream": HELD_OUT,
        }


async def held_out_agreement(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> Agreement:
    """How often the model's ordering agrees with a comparison it was never fitted on.

    Only `uniform_holdout` rows since the member's cut-over: adaptive pairs inflate the number (§13).
    """
    rows = await conn.fetch(
        f"""
        SELECT d.outcome, a.s AS s_a, b.s AS s_b
        FROM duel d
        -- BOTH sides, as `observations.load_observations` joins them and for its reason: a
        -- filter that only holds because of a check somewhere else is a filter that stops
        -- holding quietly. `record_duel` refuses a cross-kind write, but §10's re-import
        -- upserts `title.kind`, so a corpus reclassification retroactively makes an existing
        -- held-out duel cross-kind — and §4.1 rule 5 says such a pair is evidence about
        -- neither partition. Joining one side would leave the fit and §13's own figure
        -- disagreeing about the population, in the one number §13 requires to be honest.
        JOIN title ta ON ta.id = d.title_a AND ta.kind = $2
        JOIN title tb ON tb.id = d.title_b AND tb.kind = $2
        LEFT JOIN ledger_state a ON a.user_id = d.user_id AND a.title_id = d.title_a
        LEFT JOIN ledger_state b ON b.user_id = d.user_id AND b.title_id = d.title_b
        WHERE d.user_id = $1
          AND d.selection = $3
          AND d.created_at >= {cutover_sql()}
          -- §13 stream (b) is a different instrument: a re-ask measures whether the PERSON
          -- gives the same answer twice, not whether the model agrees with them, and letting
          -- one row in twice would weight it double here as well as in the fit.
          AND NOT d.is_reask
        """,
        user_id,
        kind,
        HELD_OUT,
    )

    pairs = decisive = ties = undecided = agreed = unplaced = 0
    for row in rows:
        if row["s_a"] is None or row["s_b"] is None:
            unplaced += 1
            continue
        pairs += 1
        if row["outcome"] == "TIE":
            ties += 1
            continue
        s_a, s_b = float(row["s_a"]), float(row["s_b"])
        if s_a == s_b:
            undecided += 1
            continue
        decisive += 1
        if ("A" if s_a > s_b else "B") == row["outcome"]:
            agreed += 1

    return Agreement(
        user_id=user_id, kind=kind, pairs=pairs, decisive=decisive, ties=ties,
        undecided=undecided, agreed=agreed, unplaced=unplaced,
    )


__all__ = ["Agreement", "held_out_agreement"]
