"""Where a title's poster may come from, in decision 483's order, and a person's headshot.

Household Jellyfin first, then a servable `poster_path` at w342, then decision 484's lookup.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from spielplan.art.hosts import SERVABLE_HOSTS, servable, tmdb_size

JELLYFIN = "jellyfin"
POSTER_PATH = "poster_path"
LOOKUP = "lookup"

# `art_lookup.outcome` values.
FOUND = "found"
NONE = "none"
FAILED = "failed"


@dataclass(frozen=True)
class Candidate:
    source: str
    # The cache compares these keys and nothing else.
    key: str
    url: str | None = None
    jellyfin_id: str | None = None


@dataclass(frozen=True)
class PosterRow:
    jellyfin_id: str | None
    poster_path: str | None
    lookup_outcome: str | None
    lookup_url: str | None
    lookup_owed: bool
    size: str = "w342"

    def candidates(self) -> list[Candidate]:
        out: list[Candidate] = []
        if self.jellyfin_id:
            out.append(Candidate(JELLYFIN, f"jellyfin:{self.jellyfin_id}",
                                 jellyfin_id=self.jellyfin_id))
        if servable(self.poster_path):
            url = tmdb_size(self.poster_path, self.size)
            out.append(Candidate(POSTER_PATH, url, url=url))
        if self.lookup_outcome == FOUND and servable(self.lookup_url):
            url = tmdb_size(self.lookup_url, self.size)
            if all(c.key != url for c in out):
                out.append(Candidate(LOOKUP, url, url=url))
        return out

    @property
    def signature(self) -> str:
        return "|".join(c.key for c in self.candidates())


def servable_prefixes() -> list[str]:
    """`LIKE` pre-filter for the allow-list; `servable` is still applied to every row."""
    return [f"https://{host}/%" for host in SERVABLE_HOSTS]


async def read(conn: asyncpg.Connection, title_id: int) -> PosterRow | None:
    """The title's sources; files a lookup when it has no servable poster of its own.

    The web process never asks TMDB itself (decision 484). `ON CONFLICT DO NOTHING` files a title once.
    """
    row = await conn.fetchrow(
        """
        SELECT t.id, t.jellyfin_id, t.poster_path,
               (t.tmdb_id IS NOT NULL OR t.imdb_id IS NOT NULL) AS has_ids,
               l.title_id IS NOT NULL AS filed, l.outcome, l.poster_url
          FROM title t LEFT JOIN art_lookup l ON l.title_id = t.id
         WHERE t.id = $1
        """,
        title_id,
    )
    if row is None:
        return None
    filed = bool(row["filed"])
    if not filed and row["has_ids"] and not servable(row["poster_path"]):
        await conn.execute(
            "INSERT INTO art_lookup (title_id) VALUES ($1) ON CONFLICT (title_id) DO NOTHING",
            title_id,
        )
        filed = True
    return PosterRow(
        jellyfin_id=row["jellyfin_id"] or None,
        poster_path=row["poster_path"],
        lookup_outcome=row["outcome"],
        lookup_url=row["poster_url"],
        lookup_owed=filed and row["outcome"] in (None, FAILED),
    )


async def read_person(conn: asyncpg.Connection, person_id: int) -> PosterRow | None:
    """A person's headshot: their stored `profile_path` at w185, with no Jellyfin and no lookup."""
    row = await conn.fetchrow("SELECT profile_path FROM person WHERE id = $1", person_id)
    if row is None:
        return None
    return PosterRow(jellyfin_id=None, poster_path=row["profile_path"], lookup_outcome=None,
                     lookup_url=None, lookup_owed=False, size="w185")


__all__ = [
    "FAILED", "FOUND", "JELLYFIN", "LOOKUP", "NONE", "POSTER_PATH", "Candidate", "PosterRow",
    "read", "read_person", "servable_prefixes",
]
