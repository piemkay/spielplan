"""Decision 564's moves: when Sharpen's answers put a placed title clearly in another step, it moves.

The move is a `tier_edit` with `via = 'sharpen'` (plus the class verdict, as any placement) that the
fit never reads; Undo is an ordinary drop back, which the fit does read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import asyncpg
import numpy as np

from spielplan.ledger import ladder, model
from spielplan.rank import board, read

# A move waits for this many Sharpen answers about the title since its latest placement.
MOVE_MIN_ANSWERS = 2
# One-sided 80%: this much of the posterior lies beyond one edge of the shown step.
MOVE_Z = 0.84


@dataclass(frozen=True)
class Move:
    title_id: int
    name: str
    source: int
    target: int


def due(
    items: Sequence[board.Item], *, cuts: np.ndarray, answers_since: Mapping[int, int]
) -> list[Move]:
    """The placed titles whose posterior (displayed σ) lies `MOVE_Z` beyond one edge of the step they
    are shown in, after `MOVE_MIN_ANSWERS` answers; the target is the step that bound reaches."""
    cuts = np.asarray(cuts, dtype=float)
    out = []
    for item in items:
        if item.assigned_tier is None or answers_since.get(item.title_id, 0) < MOVE_MIN_ANSWERS:
            continue
        low, high = board._band(int(item.assigned_tier), cuts)
        floor, ceiling = item.s - MOVE_Z * item.sigma, item.s + MOVE_Z * item.sigma
        # The step the 80% bound reaches, not the one `s` falls in: no overshoot past the evidence.
        if floor >= high or ceiling < low:
            target = int(model.tier_of(np.array([floor if floor >= high else ceiling]), cuts)[0])
            out.append(Move(item.title_id, item.name, int(item.assigned_tier), target))
    return out


async def settle(
    conn: asyncpg.Connection, *, user_id: int, kind: str
) -> list[tuple[Move, ladder.Placement]]:
    """Writes every due move under the member's lock, so two settles move a title once."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", ladder.LOCK, user_id)
        cuts = await read.cutpoints_of(conn, user_id=user_id, kind=kind)
        rows = await read.items(conn, user_id=user_id, kind=kind)
        since = await read.since_placement(conn, user_id=user_id, kind=kind)
        moved = []
        for move in due(
            rows, cuts=cuts.boundaries, answers_since={t: n for t, (_days, n) in since.items()}
        ):
            placed = await ladder.place(
                conn, user_id=user_id, title_id=move.title_id, tier=move.target, via="sharpen",
                source="tier",
            )
            moved.append((move, placed))
    return moved


__all__ = ["MOVE_MIN_ANSWERS", "MOVE_Z", "Move", "due", "settle"]
