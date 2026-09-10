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

A NEIGHBOUR IS CHECKED, NOT TAKEN ON TRUST. The client computes `above` and `below` from the
board it last rendered, and that board can be minutes old: two tabs, or one read before a
nightly refit. This module refused only self-reference and `above == below`, so a pair of
neighbours that had since moved was stored anyway — and because the duels carry the *placement*,
that is a comparison the person never made, in an append-only table with no undo. Each named
neighbour's current tier is resolved here and a mismatch is a `DropRefused`.

AND THE CHECK AND THE WRITES ARE ONE GESTURE. Resolving the neighbour outside the transaction
bought nothing a concurrent drop could not undo: under read committed a second drop moving that
very neighbour out of the tier commits into the gap between the read and the insert, and the
duel is stored against a title that has left — the same fabricated comparison, now with a
check standing in front of it. So the resolution and both writes sit inside one transaction
whose first statement is a per-user `pg_advisory_xact_lock`, which is the one guard that makes
the two drops take turns. Decision 202 is why this is a lock and not a 409: a drop carries no
seal and two deliberate drops are two legitimate gestures, so there is nothing here to refuse —
the property owed is that each gesture is written against the board it was checked against.
[M4.10 cycle 2, M410-C2-D202-01]

AND UNDER A FILTER THERE ARE NO NEIGHBOUR DUELS (decision 204). §6.3's two duels are for a drop
"between two titles"; on a filtered board "between" is a fact about the screen — the posters
either side of the gap are not the titles either side on the board. Re-pointing the duels at the
unfiltered neighbours silently would store a comparison against titles the person could not see,
so the filtered drop writes the `tier_edit` alone, which is the half of the gesture that is
unambiguous.
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

# The namespace half of the drop's advisory lock, keyed per user like `api/rank.py:77`'s
# `_ANSWER_LOCK = 6303` and for the same reason: a number of its own so a user id can never
# collide with another feature's lock on the same integer. 6202 is `tonight/play.py`'s finish,
# 6303 the queue answer; this is the next free one. Two ints rather than `deps.write_txn`'s
# single `hashtext(name)::bigint`, which is a different lock space again. [M4.10 cycle 2]
_DROP_LOCK = 6304


class DropRefused(ValueError):
    """A drop this board cannot accept — an unknown tier, a neighbour that is the title itself,
    or a neighbour that is not where the caller thinks it is. Refused rather than silently
    repaired: a drop that quietly did something else is the failure §6.3's whole "data, not
    override" clause is about."""


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
    """Where these titles render on this person's board right now.

    The same definition `board.build` applies, and it has to be: the caller's `above`/`below`
    come from a rendered board, so a refusal derived from any other number would refuse
    legitimate drags. That means the person's latest `tier_edit` where there is one (§6.3's
    "stays in the assigned tier"), and otherwise the tier the CURRENT cutpoints put `s` in —
    counting the boundaries at or below `s`, which is `model.tier_of`'s `searchsorted(...,
    side="right")` written in SQL and is indifferent to their order.

    Not `ledger_state.tier`: that is the tier as of the last *write*, and decision 11's
    re-initialisation moves the boundaries with no refit, so the stored number and the rendered
    board disagree for exactly as long as the refit is owed — the window this milestone closes.
    Clamped like `board.build` clamps, because decision 11 keeps `tier_edit` rows across a change
    in K and the board renders a stale level at the top of the set they now have.

    `ls.observed` for the same reason: §6.3's board is "every **rated** title", so an owned title
    nobody has rated has a coordinate and no place on the tier list — and therefore cannot be a
    neighbour. Absent from the answer is what a caller reads as "not in that tier".
    """
    rows = await conn.fetch(
        """
        SELECT ls.title_id,
               COALESCE(
                   te.tier,
                   (SELECT count(*) FROM unnest(c.boundaries) AS b WHERE b <= ls.s)
               ) AS tier
        FROM ledger_state ls
        LEFT JOIN ledger_cutpoints c ON c.user_id = ls.user_id AND c.kind = ls.kind
        LEFT JOIN (
            SELECT DISTINCT ON (title_id) title_id, tier
            FROM tier_edit WHERE user_id = $1
            ORDER BY title_id, created_at DESC, id DESC
        ) te ON te.title_id = ls.title_id
        WHERE ls.user_id = $1 AND ls.observed AND ls.title_id = ANY($2::int[])
        """,
        user_id,
        [int(t) for t in title_ids],
    )
    return {
        int(r["title_id"]): max(0, min(levels - 1, int(r["tier"])))
        for r in rows
        if r["tier"] is not None
    }


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
    filtered: bool = False,
) -> DropResult:
    """One drop: the tier edit, and a duel per neighbour it landed between.

    `above` is the title it was dropped *under* (better) and `below` the one it was dropped
    *over* (worse). Both optional — a drop into an empty tier has neither, a drop at the top of
    a tier has only `below`.

    `filtered` is whether the board the drop was made on had any filter switched on; the route
    passes `bool(filters.active())`. Decision 204: it suppresses the neighbour duels and nothing
    else. A bool rather than the `RankFilters` itself, because what changes the write is that
    "between" was computed on a subset, not which subset it was.

    One transaction, under a per-user advisory lock: §6.3's edit and its neighbour duels are one
    gesture, and a crash between them would leave a placement in the Ledger that the person never
    made. The neighbour resolution is inside it for the concurrent version of the same failure —
    see the module docstring.
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
        # First statement, before the neighbour is read: the lock is what makes the resolution
        # below and the writes after it one gesture. A second drop moving that neighbour out of
        # the tier commits into the gap otherwise — read committed lets it — and the duel then
        # records a placement against a title that had already left. [M4.10 cycle 2]
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _DROP_LOCK, user_id)
        if named and not filtered:
            placed = await _tiers_of(conn, user_id=user_id, title_ids=named, levels=len(tier_set))
            for neighbour in named:
                if placed.get(neighbour) != tier:
                    raise DropRefused(
                        f"title {neighbour} is not in {tier_set[tier]} any more - reload the board"
                    )
        edit = await observations.record_tier_edit(
            conn, user_id=user_id, title_id=title_id, tier=tier, via=via
        )
        if filtered and named:
            # Decision 204. Worth a line, because the person made a two-neighbour gesture and
            # one of its two halves was not stored: the §6.7 rail line below then narrates the
            # edit alone, which is what was written.
            log.info(
                "drop under an active filter for user %d: tier_edit only, %d neighbour(s) "
                "not recorded as duels",
                user_id,
                len(named),
            )
        # Above first, then below, so the rows read in board order — and so an inspection of
        # the two duels shows the sandwich rather than two unrelated comparisons.
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
