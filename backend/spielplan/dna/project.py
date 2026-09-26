"""Stage 8, the projected tier: one title's own keywords, mapped through the alias map.

Per title (a pure function restricted to one title's inputs). Refuses bundle titles. The weight is
a raw inventory count (decision 385); `via` is the producing keyword (decision 393).
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms
from spielplan.dna.aliases import alias_key, load_alias_map

# The inventories worth projecting, and why each is here:
#   llm:*            pass-0 memory tags -- three models, independent of each other
#   movielens        the tag genome, already relevance-thresholded upstream
#   movielens_tags   free user tags
#   mpst             a 71-tag controlled set over 4,440 titles
#   tmdb, wikidata   crawled keyword inventories
# `title_aspect` is absent: ~30% complete, and projecting it back would be close to circular.
DEFAULT_SOURCES = (
    "llm:gemini37", "llm:sonnet5", "llm:terra",
    "movielens", "movielens_tags", "mpst", "tmdb", "wikidata",
)


async def project_title(
    conn: asyncpg.Connection, title_id: int, *, version: str | None = None
) -> int:
    """Project one title's keywords onto the vocabulary. Returns the number of terms written.

    Idempotent upsert; `created_at` stays out of the SET list. The count is written, never compared
    (§4.1 rule 2).
    """
    origin = await conn.fetchval("SELECT origin FROM title WHERE id = $1", title_id)
    if origin != "acquired":
        raise ValueError(
            f"title {title_id} has origin {origin!r} and stage 8 projects only an acquired title: "
            "a bundle title's projected rows are content decision 162 seeds once, and a re-derive "
            "here would overwrite them with no import able to restore them"
        )
    scoped = await dna_terms.active_version(conn) if version is None else version
    if scoped is None:
        return 0

    amap = await load_alias_map(conn, scoped)

    keywords = await conn.fetch(
        """
        SELECT k.keyword, k.source
          FROM title_keyword k
         WHERE k.title_id = $1
           AND k.source = ANY($2::text[])
        """,
        title_id, list(DEFAULT_SOURCES),
    )

    # Both sides go through `alias_key`. `named` counts inventories; `produced` collects spellings.
    named: dict[tuple[str, str], set[str]] = {}
    produced: dict[tuple[str, str], set[str]] = {}
    for row in keywords:
        keyword = row["keyword"] or ""
        hit = amap.get(alias_key(keyword))
        if hit is None:
            continue
        named.setdefault(hit, set()).add(row["source"])
        produced.setdefault(hit, set()).add(f"keyword:{keyword}")

    # Sorted so a failure reproduces; `float` because the column is `real`.
    rows = [
        (title_id, scoped, term, facet, float(len(inventories)),
         ", ".join(sorted(produced[(facet, term)])))
        for (facet, term), inventories in sorted(named.items())
    ]
    if not rows:
        return 0

    await conn.executemany(
        """
        INSERT INTO dna_projected (title_id, version, term, facet, weight, via)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (title_id, version, term)
        DO UPDATE SET facet = EXCLUDED.facet, via = EXCLUDED.via, weight = EXCLUDED.weight
        """,
        rows,
    )
    return len(rows)


__all__ = ["DEFAULT_SOURCES", "project_title"]
