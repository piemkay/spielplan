"""`title_meta`, and the title card resolved out of it. Spec v2.1 §4.1, §6.0, §10.

§4.1 lists the table in the content spine with its rule attached:

    title_meta (multi-source, per-source rows kept — "one block = one droppable source")

That rule is about **storage**. It says a source can be dropped later without a re-import, so
the import must not collapse the rows; it does not say a reader must take a whole block from
one source. The corpus itself resolves per *field* — `mdc/export.py:34-45` walks a documented
`SOURCE_PRIORITY` in `_best(rows, field)`, treating None, '' and 0 as absent — and per field is
what the data requires: tmdb carries tagline and poster and plot, omdb carries a poster and no
tagline, and wikipedia is the only source carrying `plot_short`. A whole-block rule blanks
fields another source has.

The order is the corpus's, not this app's. Decision 162 makes the corpus a one-time seed for
content, so the seed carries the rule it was assembled under: `BUNDLE.json.source_priority`
when the bundle ships one, `SOURCE_PRIORITY` below with a report line when it does not.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import asyncpg

from spielplan.importer.report import ImportReport

# `mdc/export.py:34-45`, verbatim. mpst sits last on purpose: its synopses are the longest plot
# text in the corpus and they are full retellings, ending included — a poor default and a good
# last resort.
SOURCE_PRIORITY: tuple[str, ...] = (
    "tmdb", "omdb", "trakt", "tvmaze", "wikipedia", "jellyfin", "wikidata",
    "letterboxd", "rottentomatoes", "metacritic", "mpst",
)

# The corpus's own name for each card field. `overview` reads two of them in order, because
# `plot_short` exists on exactly one source and is better than nothing.
_PLOT_FIELDS = ("plot_full", "plot_short")
_CARD_FIELDS = {"tagline": "tagline", "poster_path": "poster_url", "backdrop_path": "backdrop_url"}


def source_priority(bundle_root: Path | None, report: ImportReport) -> list[str]:
    """The per-field precedence this bundle was assembled under.

    `BUNDLE.json` is where the corpus already records the facts the importer has to obey —
    `display_only_tables`, `frozen_rating_source_ids`, `nullable_pk_columns` — so the order
    belongs beside them rather than as a constant in this app that a corpus-side change would
    silently invalidate.
    """
    shipped: object = None
    manifest = (bundle_root / "BUNDLE.json") if bundle_root else None
    if manifest is not None and manifest.is_file():
        shipped = json.loads(manifest.read_text(encoding="utf-8")).get("source_priority")

    if isinstance(shipped, list) and shipped and all(isinstance(s, str) for s in shipped):
        report.note(
            "source-priority",
            f"per-field source order read from the bundle: {', '.join(shipped)}",
            priority=list(shipped), origin="bundle",
        )
        return list(shipped)

    report.note(
        "source-priority",
        "bundle ships no `source_priority` — resolving the title card by the corpus's own "
        f"order ({', '.join(SOURCE_PRIORITY)})",
        priority=list(SOURCE_PRIORITY), origin="default",
    )
    return list(SOURCE_PRIORITY)


def best(by_source: Mapping[str, Mapping[str, Any]], field: str, priority: Sequence[str]) -> Any:
    """`mdc/export.py`'s `_best`, ported.

    The three absent values are the corpus's: NULL, '' and 0. Zero matters for `budget` and
    `revenue`, where it means unknown rather than free, and an empty string matters because a
    source that answered with nothing must not stop the walk.
    """
    for source in priority:
        value = (by_source.get(source) or {}).get(field)
        if value not in (None, "", 0):
            return value
    return None


def _payload_columns(db: sqlite3.Connection) -> list[str]:
    """Every shipped column but the two that become the Postgres row's primary key.

    Keeping `title_id` and `source` inside the payload as well would store the key twice and
    invite a reader to trust the copy over the column it is joined by.
    """
    columns = [r[1] for r in db.execute('PRAGMA table_info("title_meta")')]
    return [c for c in columns if c not in ("title_id", "source")]


def _meta_rows(db: sqlite3.Connection, columns: Sequence[str]) -> Iterator[tuple]:
    select = ", ".join(f'"{c}"' for c in ("title_id", "source", *columns))
    for row in db.execute(f"SELECT {select} FROM title_meta"):
        yield row[0], row[1], dict(zip(columns, row[2:], strict=True))


async def load_title_meta(
    conn: asyncpg.Connection, db: sqlite3.Connection, report: ImportReport
) -> int:
    """Load the bundle's per-source meta rows, one Postgres row per (title, source).

    Bespoke rather than a `TableMap` because the shapes do not correspond: the corpus ships 21
    typed columns and this app keeps `payload jsonb`, which is what lets a source the corpus
    adds arrive without a migration.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "title_meta" not in present:
        report.warn("title-meta", "bundle has no `title_meta` — the title card has no source")
        return 0

    columns = _payload_columns(db)
    rows = list(_meta_rows(db, columns))
    await conn.execute("DELETE FROM title_meta")
    # INSERT rather than COPY, which the rest of this package uses: asyncpg's COPY encoder is
    # binary and the json/jsonb codec `db/pool.py` registers is a text one, so a dict reaches
    # a binary COPY as "no binary format encoder for type jsonb". 46,318 rows is not the place
    # to trade a correct codec for a faster path.
    await conn.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)", rows
    )
    count = len(rows)
    report.table_counts["loaded:title_meta"] = count
    report.note(
        "title-meta",
        f"{count} per-source meta rows kept — §4.1's 'one block = one droppable source'",
        columns=columns,
    )
    return count


async def resolve_title_fields(
    conn: asyncpg.Connection, priority: Sequence[str], report: ImportReport
) -> None:
    """Resolve §6.0's card fields onto `title`, per field, keeping the per-source rows.

    Only titles that have a meta row are touched. A title with none must render the card
    without those fields (§6.0), and blanking one would also overwrite whatever §8's
    acquisition path wrote for a title the corpus never shipped.
    """
    grouped: dict[int, dict[str, Any]] = {}
    for row in await conn.fetch("SELECT title_id, source, payload FROM title_meta"):
        grouped.setdefault(row["title_id"], {})[row["source"]] = row["payload"]

    updates = []
    for title_id, by_source in grouped.items():
        overview = next(
            (v for f in _PLOT_FIELDS if (v := best(by_source, f, priority)) is not None), None
        )
        resolved = {pg: best(by_source, src, priority) for pg, src in _CARD_FIELDS.items()}
        updates.append(
            (title_id, overview, resolved["tagline"],
             resolved["poster_path"], resolved["backdrop_path"])
        )

    await conn.executemany(
        "UPDATE title SET overview = $2, tagline = $3, poster_path = $4, backdrop_path = $5 "
        " WHERE id = $1",
        updates,
    )

    # §4.3's `title_video` carries no source column into this app, so the trailer is chosen by
    # what it is rather than by who said so: a trailer before a teaser, YouTube before the
    # sites the player cannot embed.
    await conn.execute(
        """
        UPDATE title t SET trailer_key = v.key
          FROM (SELECT DISTINCT ON (title_id) title_id, key
                  FROM title_video WHERE key <> ''
                 ORDER BY title_id, (lower(type) = 'trailer') DESC, (site = 'YouTube') DESC, key
               ) v
         WHERE v.title_id = t.id
        """
    )
    trailers = await conn.fetchval("SELECT count(*) FROM title WHERE trailer_key IS NOT NULL")
    report.note(
        "title-card",
        f"{len(updates)} title cards resolved per field from title_meta; "
        f"{trailers} carry a trailer key",
        titles=len(updates),
    )
