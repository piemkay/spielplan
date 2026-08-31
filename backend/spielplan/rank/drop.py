"""§6.3's drag-and-drop, as observations. Spec v2.1 §6.3, §5.2, §4.2, §6.7; proposals 15, 71.

§6.3: "**Drag-and-drop rearrange** — the owner's requirement, implemented as Ledger
observations: dropping a title into a tier emits a `tier_edit`; dropping it *between* two
titles emits that edit **plus two margin-less duels** against its new neighbours."

So there are two write shapes and the difference between them is a fact about the board, not
about the arm — which is why `observations.record_tier_edit` refuses to guess and this module
composes the calls instead.

THE TWO DUELS CARRY THE PLACEMENT, AND THEY HAVE OUTCOMES. A drop between `above` and `below`
is a claim with content: the person put this title under one title and over another. That is
`above` beats it and it beats `below` — two Davidson observations at the ordinal resolution the
tier arm cannot express, which is the whole reason §6.3 asks for them. Writing them
outcome-less, or writing the pair without saying who won, would store the geometry and throw
away the judgement.

**Margin-less** is §6.3's word and `margin = NULL` is what it means: §6.1's decisive toggle is
about how hard a person tapped a battle, and a drop has no such gesture to read. `model`
normalises `margin/mean(margin)` over the rows that have one, so a NULL is not a zero-weight
duel — it is a duel with the ordinary weight.

ONE NEIGHBOUR IS A LEGAL DROP. §6.3 says "between two titles" and does not say what a drop at
the top of a tier is. It is one neighbour and one duel: refusing it would make the first and
last slot of every tier undroppable, and inventing a second duel against nothing would put a
comparison in the Ledger that nobody made.

`selection` stays at 0005's default `'random'`. The column exists so §13's held-out rows are
identifiable at query time, and its values are statements about *adaptive* selection; a drop is
not adaptively selected by anything. `context = 'tier_insert'` is what says where it came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from spielplan.home import rail
from spielplan.ledger import observations
from spielplan.rank import tiers

log = logging.getLogger("spielplan.rank.drop")

# §4.2: "context: profile_battle | tier_queue | tier_insert".
INSERT_CONTEXT = "tier_insert"


class DropRefused(ValueError):
    """A drop this board cannot accept — an unknown tier, or a neighbour that is the title
    itself. Refused rather than silently repaired: a drop that quietly did something else is
    the failure §6.3's whole "data, not override" clause is about."""


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


async def drop(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    tier: int,
    above: int | None = None,
    below: int | None = None,
    via: str = "drag_drop",
    title_name: str | None = None,
) -> DropResult:
    """One drop: the tier edit, and a duel per neighbour it landed between.

    `above` is the title it was dropped *under* (better) and `below` the one it was dropped
    *over* (worse). Both optional — a drop into an empty tier has neither, a drop at the top of
    a tier has only `below`.

    One transaction: §6.3's edit and its neighbour duels are one gesture, and a crash between
    them would leave a placement in the Ledger that the person never made.
    """
    tier_set = await tiers.tier_set_of(conn, user_id=user_id)
    if not 0 <= tier < len(tier_set):
        raise DropRefused(f"tier {tier} is outside this person's set {tier_set}")
    for neighbour in (above, below):
        if neighbour is not None and neighbour == title_id:
            raise DropRefused("a title cannot be dropped next to itself")
    if above is not None and above == below:
        raise DropRefused("a title cannot be dropped between one title and itself")

    duel_ids: list[int] = []
    async with conn.transaction():
        edit = await observations.record_tier_edit(
            conn, user_id=user_id, title_id=title_id, tier=tier, via=via
        )
        # Above first, then below, so the rows read in board order — and so an inspection of
        # the two duels shows the sandwich rather than two unrelated comparisons.
        if above is not None:
            won = await observations.record_duel(
                conn, user_id=user_id, title_a=above, title_b=title_id,
                outcome="A", context=INSERT_CONTEXT, margin=None,
            )
            duel_ids.append(int(won.row_id))
        if below is not None:
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
    return DropResult(
        user_id=user_id,
        title_id=title_id,
        kind=edit.kind,
        tier=tier,
        tier_edit_id=int(edit.row_id),
        duel_ids=tuple(duel_ids),
        log=line,
    )


__all__ = ["INSERT_CONTEXT", "DropRefused", "DropResult", "drop"]
