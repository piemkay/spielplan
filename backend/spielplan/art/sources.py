"""Where a title's poster may come from, read once per request. Spec v2.1 §6.8, §7.1, §8 (the
household's Jellyfin is exempt from politeness); decisions 483 and 484.

The order is decision 483's: the household's own Jellyfin first, for a title it holds, because it
is the art the household's own server shows beside the same file; then `title.poster_path` when
`hosts.servable` passes it, at TMDB's w342 rather than the w500 the corpus stored; then the
poster the worker's TMDB lookup found for a title that had neither (decision 484). A title with no
candidate at all is the tinted 2:3 panel, which §6.8 keeps as a designed state.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from spielplan.art.hosts import SERVABLE_HOSTS, servable, tmdb_size

JELLYFIN = "jellyfin"
POSTER_PATH = "poster_path"
LOOKUP = "lookup"

# decision 484's lookup states, as `art_lookup.outcome` spells them (0034_art_lookup.sql).
FOUND = "found"
NONE = "none"
FAILED = "failed"


@dataclass(frozen=True)
class Candidate:
    source: str
    # What identifies this source's answer: the Jellyfin item for a Jellyfin candidate, the URL
    # for the other two. The cache compares these and nothing else (see `art/cache.py`).
    key: str
    url: str | None = None
    jellyfin_id: str | None = None


@dataclass(frozen=True)
class PosterRow:
    title_id: int
    jellyfin_id: str | None
    poster_path: str | None
    lookup_outcome: str | None
    lookup_url: str | None
    lookup_owed: bool

    def candidates(self) -> list[Candidate]:
        out: list[Candidate] = []
        if self.jellyfin_id:
            out.append(Candidate(JELLYFIN, f"jellyfin:{self.jellyfin_id}",
                                 jellyfin_id=self.jellyfin_id))
        if servable(self.poster_path):
            url = tmdb_size(self.poster_path)
            out.append(Candidate(POSTER_PATH, url, url=url))
        if self.lookup_outcome == FOUND and servable(self.lookup_url):
            url = tmdb_size(self.lookup_url)
            if all(c.key != url for c in out):
                out.append(Candidate(LOOKUP, url, url=url))
        return out

    @property
    def signature(self) -> str:
        return "|".join(c.key for c in self.candidates())


def servable_prefixes() -> list[str]:
    """`LIKE` patterns for the allow-list, for a query that has to pre-filter on the server. The
    Python test is still applied to every row it returns; this only keeps the scan short."""
    return [f"https://{host}/%" for host in SERVABLE_HOSTS]


async def read(conn: asyncpg.Connection, title_id: int) -> PosterRow | None:
    """The title's sources, and a lookup filed for it when it has no servable poster of its own.

    The one write a poster view makes, and it is a request rather than a fetch: §1 puts
    acquisition in the worker and decision 340 gives a host one bucket, so the web process never
    asks TMDB's API itself (decision 484) - it files the title, and `art-lookup` asks. `ON
    CONFLICT DO NOTHING`, so a title viewed a hundred times is filed once. A title with neither a
    TMDB nor an IMDb id is not filed: there is nothing to look it up by.
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
        title_id=int(row["id"]),
        jellyfin_id=row["jellyfin_id"] or None,
        poster_path=row["poster_path"],
        lookup_outcome=row["outcome"],
        lookup_url=row["poster_url"],
        lookup_owed=filed and row["outcome"] in (None, FAILED),
    )


__all__ = [
    "FAILED", "FOUND", "JELLYFIN", "LOOKUP", "NONE", "POSTER_PATH", "Candidate", "PosterRow",
    "read", "servable_prefixes",
]
