"""§6.3's drag-and-drop, as observations: a placement plus one margin-less duel per neighbour.

Neighbours are re-checked under a per-user lock in the same transaction as the writes, so a duel is
never stored against a title that has moved. Under a filter no neighbour duels are written (decision 204).
The placement records its tier's verdict where the live one is none or another class (`ladder.place`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg
import numpy as np

from spielplan.home import rail
from spielplan.ledger import ladder, model, observations
from spielplan.rank import tiers

log = logging.getLogger("spielplan.rank.drop")

# §4.2: "context: profile_battle | tier_queue | tier_insert".
INSERT_CONTEXT = "tier_insert"


class DropRefused(ValueError):
    """A drop this board cannot accept. Refused, never silently repaired."""


@dataclass(frozen=True)
class DropResult:
    user_id: int
    title_id: int
    kind: str
    tier: int
    tier_edit_id: int
    duel_ids: tuple[int, ...]
    log: str

    @property
    def neighbour_duels(self) -> int:
        return len(self.duel_ids)


async def _tiers_of(
    conn: asyncpg.Connection, *, user_id: int, title_ids: list[int], levels: int
) -> dict[int, int]:
    """Where these rated titles render on this person's board right now, as `board.build` does.

    The latest `tier_edit` (via `rescale_level`), else the CURRENT cutpoints' tier with decision
    508's hold; not `ledger_state.tier`, which lags a boundary change. Absent means not on the board.
    """
    rows = await conn.fetch(
        f"""
        SELECT ls.title_id, te.tier AS assigned, te.n_levels AS assigned_k, lv.value AS verdict,
               (SELECT count(*) FROM unnest(c.boundaries) AS b WHERE b <= ls.s) AS fitted
        FROM ledger_state ls
        LEFT JOIN ledger_cutpoints c ON c.user_id = ls.user_id AND c.kind = ls.kind
        LEFT JOIN ({observations.latest_tier_edit_sql()}) te ON te.title_id = ls.title_id
        LEFT JOIN ({observations.LIVE_LABEL_SQL}) lv ON lv.title_id = ls.title_id
        WHERE ls.user_id = $1 AND ls.observed AND ls.title_id = ANY($2::int[])
        """,
        user_id,
        [int(t) for t in title_ids],
    )
    out: dict[int, int] = {}
    for r in rows:
        if r["assigned"] is not None:
            out[int(r["title_id"])] = observations.rescale_level(
                int(r["assigned"]), k_from=r["assigned_k"], k_to=levels
            )
        elif r["fitted"] is not None:
            held, _reach = model.hold_to_verdict(
                np.array([observations.rescale_level(int(r["fitted"]), k_from=None, k_to=levels)]),
                np.array([-1]),
                np.array([-1 if r["verdict"] is None else int(r["verdict"])]),
                levels,
            )
            out[int(r["title_id"])] = int(held[0])
    return out


async def drop(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    tier: int,
    above: int | None = None,
    below: int | None = None,
    via: str = "drag_drop",
    undoes: int | None = None,
    title_name: str | None = None,
    filtered: bool = False,
) -> DropResult:
    """One drop: the placement, and a duel per neighbour it landed between.

    `above` is the better neighbour and `below` the worse; either may be absent. `filtered`
    suppresses the neighbour duels only (decision 204). `undoes` names the edit a toast's Undo takes
    back (decision 534).
    """
    kind = await observations.kind_of(conn, title_id)
    tier_set = await tiers.tier_set_of(conn, user_id=user_id, kind=kind)
    if not 0 <= tier < len(tier_set):
        raise DropRefused(f"tier {tier} is outside this person's set {tier_set}")
    for neighbour in (above, below):
        if neighbour is not None and neighbour == title_id:
            raise DropRefused("a title cannot be dropped next to itself")
    if above is not None and above == below:
        raise DropRefused("a title cannot be dropped between one title and itself")

    named = [n for n in (above, below) if n is not None]

    duel_ids: list[int] = []
    async with conn.transaction():
        # First, before the neighbours are read, so a concurrent drop cannot move one in between.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", ladder.LOCK, user_id)
        if undoes is not None and not await conn.fetchval(
            "SELECT 1 FROM tier_edit WHERE id = $1 AND user_id = $2 AND title_id = $3",
            undoes,
            user_id,
            title_id,
        ):
            raise DropRefused(f"tier_edit {undoes} is not this person's edit of this title")
        if named and not filtered:
            where = await _tiers_of(conn, user_id=user_id, title_ids=named, levels=len(tier_set))
            for neighbour in named:
                if where.get(neighbour) != tier:
                    raise DropRefused(
                        f"title {neighbour} is not in {tier_set[tier]} any more - reload the board"
                    )
        placed = await ladder.place(
            conn, user_id=user_id, title_id=title_id, tier=tier, via=via, undoes=undoes,
            source="tier",
        )
        if filtered and named:
            log.info(
                "drop under an active filter for user %d: tier_edit only, %d neighbour(s) "
                "not recorded as duels",
                user_id,
                len(named),
            )
        # Above beats it, it beats below: written in board order.
        if above is not None and not filtered:
            won = await observations.record_duel(
                conn, user_id=user_id, title_a=above, title_b=title_id,
                outcome="A", context=INSERT_CONTEXT, margin=None,
            )
            duel_ids.append(int(won.row_id))
        if below is not None and not filtered:
            lost = await observations.record_duel(
                conn, user_id=user_id, title_a=title_id, title_b=below,
                outcome="A", context=INSERT_CONTEXT, margin=None,
            )
            duel_ids.append(int(lost.row_id))

    line = rail.tier_edit_line(
        title_name or f"title {title_id}",
        tier_set[tier],
        via=via,
        neighbour_duels=len(duel_ids),
    )
    if placed.verdict is not None:
        answered = model.verdict_class_of_tier(tier, len(tier_set))
        line += f" + verdict = {observations.VERDICT_LABELS[answered]}"
    return DropResult(
        user_id=user_id,
        title_id=title_id,
        kind=placed.kind,
        tier=tier,
        tier_edit_id=placed.tier_edit_id,
        duel_ids=tuple(duel_ids),
        log=line,
    )


__all__ = ["INSERT_CONTEXT", "DropRefused", "DropResult", "drop"]
