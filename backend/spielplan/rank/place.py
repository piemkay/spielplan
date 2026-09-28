"""§6.3's Place with questions (decision 528): one title's spot inside its tier, found by a binary
search over the tier in the board's order. Nothing is stored between questions: the search travels in
the caller's sealed token, and each read of it is taken against the tier as the board orders it now.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import asyncpg

from spielplan.ledger import observations
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.rank import board, read

# §4.2's duel context for these answers (0041).
CONTEXT = "tier_place"

# The result's neighbourhood: two rows of a phone's four columns.
AROUND = 8


class PlaceRefused(ValueError):
    """A title this board cannot place: not rated, of the other kind, or its tier is gone."""


@dataclass(frozen=True)
class Search:
    """`low`..`high` are the insertion points still possible among the tier's other titles, 0 being
    above the first. `skipped` holds the neighbours the person has not seen."""

    title_id: int
    tier: int
    low: int
    high: int
    asked: int = 0
    skipped: tuple[int, ...] = ()

    def probe(self, others: Sequence[int]) -> int | None:
        """The window's middle, or the title nearest it the person has seen; None ends the search."""
        middle = (self.low + self.high) // 2
        seen = [i for i in range(self.low, self.high) if others[i] not in self.skipped]
        return min(seen, key=lambda i: (abs(i - middle), i), default=None)

    def answered(self, index: int, outcome: str) -> Search:
        """A puts the title above `others[index]`, B below it, and a tie ends it just below it."""
        low, high = {
            "A": (self.low, index),
            "B": (index + 1, self.high),
            "TIE": (index + 1, index + 1),
        }[outcome]
        return replace(self, low=low, high=high, asked=self.asked + 1)


@dataclass(frozen=True)
class View:
    """A search read against its tier: `others` is the tier best first, the placed title left out."""

    search: Search
    label: str
    others: tuple[int, ...]

    @property
    def probe(self) -> int | None:
        return self.search.probe(self.others)

    def progress(self) -> dict[str, int]:
        """Places in the tier counted from 1, the placed title among them."""
        s = self.search
        return {
            "low": s.low + 1,
            "high": s.high + 1,
            "size": len(self.others) + 1,
            "asked": s.asked,
            "estimate": s.asked + math.ceil(math.log2(s.high - s.low + 1)),
        }

    def spot(self) -> tuple[int | None, int | None, list[int]]:
        """The neighbours above and below where the search put it, and the neighbourhood's ids."""
        at = self.search.low
        placed = [*self.others[:at], self.search.title_id, *self.others[at:]]
        start = max(0, min(at - 2, len(placed) - AROUND))
        above = self.others[at - 1] if at > 0 else None
        below = self.others[at] if at < len(self.others) else None
        return above, below, placed[start : start + AROUND]


def _view(tier: board.Tier, search: Search) -> View:
    others = tuple(e.title_id for e in tier.entries if e.title_id != search.title_id)
    high = min(search.high, len(others))
    return View(replace(search, low=min(search.low, high), high=high), tier.label, others)


async def begin(
    conn: asyncpg.Connection, *, user_id: int, kind: str, hp: Hyperparams, title_id: int
) -> View:
    """The search over the tier the title renders in on the whole, unfiltered board."""
    tiers, _cuts, _rows = await read.load(conn, user_id=user_id, kind=kind, hp=hp)
    tier = next((t for t in tiers if any(e.title_id == title_id for e in t.entries)), None)
    if tier is None:
        raise PlaceRefused(f"title {title_id} is not on your {kind} board")
    return _view(tier, Search(title_id=title_id, tier=tier.index, low=0, high=len(tier.entries)))


async def resume(
    conn: asyncpg.Connection, *, user_id: int, kind: str, hp: Hyperparams, search: Search
) -> View:
    """A sealed search against its tier as it stands now; a tier that shrank clamps the window."""
    tiers, _cuts, _rows = await read.load(conn, user_id=user_id, kind=kind, hp=hp)
    tier = next((t for t in tiers if t.index == search.tier), None)
    if tier is None:
        raise PlaceRefused("that tier is no longer on your board")
    return _view(tier, search)


async def answer(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    search: Search,
    neighbour: int,
    index: int,
    outcome: str,
    decisive: bool,
    hp: Hyperparams,
) -> tuple[Search, observations.Write]:
    """One duel through the path every duel takes, placed title as A; a tie is never weighted."""
    write = await observations.record_duel(
        conn,
        user_id=user_id,
        title_a=search.title_id,
        title_b=neighbour,
        outcome=outcome,
        context=CONTEXT,
        decisive=decisive and outcome != "TIE",
        hp=hp,
    )
    return search.answered(index, outcome), write


async def skip(
    conn: asyncpg.Connection, *, user_id: int, search: Search, neighbour: int
) -> Search:
    """§6.1's Not seen for the neighbour; the search asks about the one beside it next."""
    await observations.record_not_seen(conn, user_id=user_id, title_id=neighbour)
    return replace(search, skipped=(*search.skipped, neighbour))


__all__ = ["AROUND", "CONTEXT", "PlaceRefused", "Search", "View", "answer", "begin", "resume", "skip"]
