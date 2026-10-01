"""Rate's shelves (§6.1, decision 551): one per tier, each holding the person's own placed films most
like the film being placed. The same shelves open from the title card. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import asyncpg

from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.home import why
from spielplan.ledger import ladder, observations

SHELF_FILMS = 4


@dataclass(frozen=True)
class ShelfFilm:
    id: int
    name: str
    original_name: str | None
    original_language: str | None
    poster_path: str | None


@dataclass(frozen=True)
class Shelf:
    tier: int
    word: str
    count: int
    films: tuple[ShelfFilm, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "word": self.word,
            "count": self.count,
            "films": [asdict(film) for film in self.films],
        }


async def _genres(conn: asyncpg.Connection, title_ids: Sequence[int]) -> dict[int, set[str]]:
    rows = await conn.fetch(
        "SELECT title_id, lower(genre) AS genre FROM title_genre "
        "WHERE title_id = ANY($1::int[]) AND source <> ALL($2::text[])",
        list(title_ids),
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    raw: dict[int, set[str]] = {}
    for r in rows:
        raw.setdefault(int(r["title_id"]), set()).add(r["genre"])
    return {title_id: set(genre_vocab.facet(labels)) for title_id, labels in raw.items()}


async def _standing(
    conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int]
) -> dict[int, tuple[float | None, datetime]]:
    """Each placed film's `ledger_state.s` (None before a fit) and when it was placed."""
    rows = await conn.fetch(
        f"""
        SELECT e.title_id, e.created_at, ls.s
          FROM ({observations.latest_tier_edit_sql()}) e
          LEFT JOIN ledger_state ls ON ls.user_id = $1 AND ls.title_id = e.title_id
         WHERE e.title_id = ANY($2::int[])
        """,
        user_id,
        list(title_ids),
    )
    return {
        int(r["title_id"]): (None if r["s"] is None else float(r["s"]), r["created_at"])
        for r in rows
    }


async def _films(conn: asyncpg.Connection, title_ids: Sequence[int]) -> dict[int, ShelfFilm]:
    rows = await conn.fetch(
        "SELECT id, name, original_name, original_language, poster_path FROM title "
        "WHERE id = ANY($1::int[])",
        list(title_ids),
    )
    return {int(r["id"]): ShelfFilm(**dict(r)) for r in rows}


async def shelves_for(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, kind: str, version: str | None
) -> list[Shelf]:
    """One shelf per tier of the person's set for `kind`, best first, each with up to four of their
    films placed there: most alike first (`why.likeness`), ties to a shared genre, then the
    higher-placed. Never `title_id` itself; an empty tier keeps its shelf. `version` None (no
    vocabulary yet) leaves every film equally alike.
    """
    placed = await ladder.placements(conn, user_id=user_id, kind=kind)
    placed.pop(title_id, None)
    words = observations.tier_words(await observations.tier_set_of(conn, user_id=user_id, kind=kind))
    likes = (
        await why.likeness(
            conn, title_id=title_id, candidate_ids=list(placed), kind=kind, version=version
        )
        if version is not None and placed
        else {}
    )
    genres = await _genres(conn, [title_id, *placed])
    target = genres.get(title_id, set())
    standing = await _standing(conn, user_id=user_id, title_ids=list(placed))

    def order(film: int) -> tuple[Any, ...]:
        like = likes.get(film)
        s, placed_at = standing[film]
        return (
            -(like.likeness if like else 0.0),
            not (genres.get(film, set()) & target),
            s is None,
            -(s or 0.0),
            -placed_at.timestamp(),
            film,
        )

    by_tier: dict[int, list[int]] = {}
    for film in sorted(placed, key=order):
        by_tier.setdefault(placed[film], []).append(film)
    films = await _films(conn, [t for ids in by_tier.values() for t in ids[:SHELF_FILMS]])
    return [
        Shelf(
            tier=tier,
            word=words[tier],
            count=len(by_tier.get(tier, ())),
            films=tuple(films[t] for t in by_tier.get(tier, [])[:SHELF_FILMS]),
        )
        for tier in reversed(range(len(words)))
    ]


async def sheet(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> dict[str, Any]:
    """The title card's ladder sheet: the title's current step by its word, and its shelves.

    Raises LookupError for an unknown title.
    """
    kind = await observations.kind_of(conn, title_id)
    words = observations.tier_words(await observations.tier_set_of(conn, user_id=user_id, kind=kind))
    tier = (await ladder.placements(conn, user_id=user_id, kind=kind)).get(title_id)
    shelves = await shelves_for(
        conn, user_id=user_id, title_id=title_id, kind=kind,
        version=await dna_terms.active_version(conn),
    )
    return {
        "title_id": title_id,
        "kind": kind,
        "current": None if tier is None else {"tier": tier, "word": words[tier]},
        "shelves": [shelf.as_dict() for shelf in shelves],
    }


__all__ = ["SHELF_FILMS", "Shelf", "ShelfFilm", "shelves_for", "sheet"]
