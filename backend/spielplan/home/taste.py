"""What a member's own verdicts say they do not enjoy (decision 512), which ranking shelves leave out.

Mood/themes/sensibility terms and canonical genres disliked often and never liked (presence, either
tier, both kinds), plus films far longer than the longest they liked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import asyncpg

from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.ledger.observations import LIVE_LABEL_SQL

# Four, not three: three-for-three happens by chance on about 1 term in 70.
AVOID_MIN_DISLIKED = 4

# What a title is about and how it feels; the other facets over-generalise.
AVOID_FACETS: tuple[str, ...] = ("mood", "themes", "sensibility")

# The runtime ceiling: liked films needed, minutes of slack, and the floor it never cuts below.
RUNTIME_MIN_LIKED = 10
RUNTIME_MARGIN_MIN = 30
RUNTIME_FLOOR_MIN = 180

# Verdict values, as `verdict_value_check` spells them.
DISLIKED, FINE, LIKED = 0, 1, 2


@dataclass(frozen=True)
class Avoided:
    """One member's avoid set. Empty for a member with no disliked pattern yet."""

    terms: frozenset[str] = frozenset()
    genres: frozenset[str] = frozenset()      # canonical names (decision 473)
    runtime_max: int | None = None            # films only; minutes
    # The vocabulary's names for the terms, then the genres - what a member can read back.
    labels: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:
        return bool(self.terms or self.genres or self.runtime_max is not None)

    def as_dict(self) -> dict[str, object]:
        return {"labels": list(self.labels), "runtime_max": self.runtime_max}


def _avoids(disliked: int, fine: int, liked: int) -> bool:
    return disliked >= AVOID_MIN_DISLIKED and liked == 0 and disliked >= fine


async def avoided_for(conn: asyncpg.Connection, *, user_id: int, version: str | None) -> Avoided:
    """The member's avoid set from their live verdicts (re-asks excluded)."""
    terms: dict[str, str] = {}
    if version is not None:
        rows = await conn.fetch(
            f"""
            WITH lv AS ({LIVE_LABEL_SQL}),
            tagged AS (
                SELECT DISTINCT d.term, lv.title_id, lv.value
                  FROM lv JOIN dna_tagged d ON d.title_id = lv.title_id AND d.version = $2
                 WHERE d.facet = ANY($3::text[])
            )
            SELECT g.term, max(dl.label) AS label,
                   count(*) FILTER (WHERE g.value = {DISLIKED}) AS disliked,
                   count(*) FILTER (WHERE g.value = {FINE}) AS fine,
                   count(*) FILTER (WHERE g.value = {LIKED}) AS liked
              FROM tagged g
              LEFT JOIN dna_term dl ON dl.version = $2 AND dl.term = g.term
             GROUP BY g.term
            """,
            user_id,
            version,
            list(AVOID_FACETS),
        )
        terms = {
            r["term"]: dna_terms.label_of(r["term"], r["label"])
            for r in rows
            if _avoids(int(r["disliked"]), int(r["fine"]), int(r["liked"]))
        }

    # Per title first, so a title two sources call "music" and "musical" counts once.
    genre_rows = await conn.fetch(
        f"""
        WITH lv AS ({LIVE_LABEL_SQL})
        SELECT lv.title_id, lv.value, lower(g.genre) AS genre
          FROM lv JOIN title_genre g ON g.title_id = lv.title_id
         WHERE g.source <> ALL($2::text[])
        """,
        user_id,
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    per_title: dict[int, tuple[int, set[str]]] = {}
    for r in genre_rows:
        value, names = per_title.setdefault(int(r["title_id"]), (int(r["value"]), set()))
        names.update(genre_vocab.GENRE_CANON.get(r["genre"], ()))
    counts: dict[str, list[int]] = {}
    for value, names in per_title.values():
        for name in names:
            counts.setdefault(name, [0, 0, 0])[value] += 1
    genres = sorted(g for g, (d, f, lk) in counts.items() if _avoids(d, f, lk))

    liked_films = await conn.fetchrow(
        f"""
        WITH lv AS ({LIVE_LABEL_SQL})
        SELECT count(*) AS n, max(t.runtime_min) AS longest
          FROM lv JOIN title t ON t.id = lv.title_id AND t.kind = 'movie'
         WHERE lv.value = {LIKED}
        """,
        user_id,
    )
    runtime_max = None
    if int(liked_films["n"]) >= RUNTIME_MIN_LIKED and liked_films["longest"] is not None:
        runtime_max = max(int(liked_films["longest"]) + RUNTIME_MARGIN_MIN, RUNTIME_FLOOR_MIN)

    return Avoided(
        terms=frozenset(terms),
        genres=frozenset(genres),
        runtime_max=runtime_max,
        labels=tuple(sorted(terms.values(), key=str.lower)) + tuple(genres),
    )


async def avoided_titles(
    conn: asyncpg.Connection,
    avoids: Sequence[Avoided],
    *,
    kind: str,
    version: str | None,
    title_ids: Sequence[int] | None = None,
) -> frozenset[int]:
    """The owned titles of `kind` any of these members avoids (or, with `title_ids`, those titles)."""
    terms = sorted(set().union(*(a.terms for a in avoids))) if avoids else []
    genres = sorted(set().union(*(a.genres for a in avoids))) if avoids else []
    ceilings = [a.runtime_max for a in avoids if a.runtime_max is not None]
    runtime_max = min(ceilings) if ceilings and kind == "movie" else None
    if not terms and not genres and runtime_max is None:
        return frozenset()
    raw = sorted({label for g in genres for label in genre_vocab.raw_labels(g)})
    scope = "t.is_owned" if title_ids is None else "t.id = ANY($7::int[])"
    rows = await conn.fetch(
        f"""
        SELECT t.id FROM title t
         WHERE t.kind = $1 AND {scope} AND (
               EXISTS (SELECT 1 FROM dna_tagged d
                        WHERE d.title_id = t.id AND d.version = $2 AND d.term = ANY($3::text[]))
            OR EXISTS (SELECT 1 FROM title_genre g
                        WHERE g.title_id = t.id AND g.source <> ALL($5::text[])
                          AND lower(g.genre) = ANY($4::text[]))
            OR (t.runtime_min IS NOT NULL AND t.runtime_min > $6::int))
        """,
        kind,
        version,
        terms,
        raw,
        list(genre_vocab.EXCLUDED_SOURCES),
        runtime_max,
        *([] if title_ids is None else [[int(t) for t in title_ids]]),
    )
    return frozenset(int(r["id"]) for r in rows)


__all__ = [
    "AVOID_FACETS",
    "AVOID_MIN_DISLIKED",
    "RUNTIME_FLOOR_MIN",
    "RUNTIME_MARGIN_MIN",
    "RUNTIME_MIN_LIKED",
    "Avoided",
    "avoided_for",
    "avoided_titles",
]
