"""Identity: which url is a title's, what an adapter may write back, and whether the page fits.

`set_ids` is the only write any adapter issues against `title` (decision 372).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import asyncpg

# The only `title` columns a stage-2 adapter may write (decision 372; `letterboxd_slug` per 374).
# `jellyfin_id` is absent on purpose: §7.1's resolver owns it.
ID_COLUMNS = frozenset({
    "imdb_id", "tmdb_id", "tvdb_id", "trakt_id", "trakt_slug", "letterboxd_slug",
    "rt_slug", "metacritic_slug", "wikidata_id", "wikipedia_title",
})

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"


def valid_imdb(imdb_id: Any) -> str | None:
    """Accepts short ids on purpose: unlike `stages._IMDB_ID`, `tt0001`-era ids are real here."""
    if not imdb_id:
        return None
    s = str(imdb_id).strip()
    return s if re.fullmatch(r"tt\d{5,10}", s) else None


async def title_row(conn: asyncpg.Connection, title_id: int) -> asyncpg.Record | None:
    """The row every adapter reads first. An explicit SELECT list keeps card fields away from adapters."""
    return await conn.fetchrow(
        "SELECT id, kind, name, original_name, year, imdb_id, tmdb_id, tvdb_id, trakt_id,"
        "       trakt_slug, letterboxd_slug, rt_slug, metacritic_slug, wikidata_id,"
        "       wikipedia_title"
        "  FROM title WHERE id = $1",
        title_id,
    )


async def set_ids(conn: asyncpg.Connection, title_id: int, **ids: Any) -> list[str]:
    """Fill identity columns that are still empty. Returns the column names it offered.

    Never overwrites: §4.1 rule 6 duplicates are legitimate, and content seeds once (decision 162).
    Never flips `kind`. Empty values are dropped rather than written.
    """
    clean = {k: v for k, v in ids.items() if v not in (None, "")}
    unknown = sorted(k for k in clean if k not in ID_COLUMNS)
    if unknown:
        # Raise, not filter: a caller passing a card field has misread decision 372.
        raise ValueError(
            f"a stage 2 source may not write {', '.join(unknown)} on title: decision 372 lets an "
            "adapter write the raw bytes and the identity columns and nothing else, and every "
            "card field, title_meta payload, credit, review and display row is stage 3's"
        )
    if not clean:
        return []
    columns = sorted(clean)
    sets = ", ".join(f"{col} = COALESCE({col}, ${i})" for i, col in enumerate(columns, start=2))
    await conn.execute(
        f"UPDATE title SET {sets}, updated_at = now() WHERE id = $1",
        title_id, *(clean[col] for col in columns),
    )
    return columns


async def known_people(conn: asyncpg.Connection, title_id: int) -> set[str]:
    """The directors and billed cast this app holds for a title, as `loose_name` keys.

    Delegates to `derive/ids.known_people` (one identity rule for stages 2 and 3), and unions in this
    run's TMDB cast, because on a first acquisition `credit` is still empty.
    """
    from spielplan.derive.ids import known_people as derived

    return await derived(conn, title_id) | await _fetched_cast(conn, title_id)


async def _fetched_cast(conn: asyncpg.Connection, title_id: int) -> set[str]:
    """The directors and billed cast of the TMDB detail document this title already holds.

    An `OSError` from `rawstore.read` is not caught (decision 361). Empty when there is no document.
    """
    from spielplan.acquire import rawstore
    from spielplan.derive import parse, rebuild

    row = await conn.fetchrow(_TMDB_DETAIL, str(title_id), sorted(rebuild.REQUIRED_DOCUMENTS))
    if row is None:
        return set()
    content = await rawstore.read(conn, row["id"])
    return rebuild._cast(parse.parse_document("tmdb", row["kind"], content))


# Newest good TMDB detail filed under one of this title's tasks.
_TMDB_DETAIL = """
SELECT d.id, d.kind
  FROM raw_document d
 WHERE d.ok
   AND d.source || ':' || d.kind = ANY($2::text[])
   AND d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.fetched_at DESC, d.id DESC
 LIMIT 1
"""


def belongs_to_title(content: bytes, *, year: int | None, people: set[str],
                     mode: str, people_decide: bool = True) -> bool:
    """Is this scraped page really about the title we asked for? The one predicate, borrowed.

    Import deferred so importing an adapter does not drag in stage 3's parsers.
    """
    from spielplan.derive.parse import page_belongs_to_title

    return page_belongs_to_title(
        content, year=year, people=people, mode=mode, people_decide=people_decide
    )


__all__ = [
    "ID_COLUMNS",
    "belongs_to_title",
    "known_people",
    "set_ids",
    "slugify",
    "title_row",
    "valid_imdb",
]
