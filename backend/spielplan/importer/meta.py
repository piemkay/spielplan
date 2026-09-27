"""`title_meta`, and the title card resolved out of it (§4.1, §6.0, §10).

Rows stay per source; the card resolves per field over the corpus's order. Never eligible: an
`mpst` overview, a matched-source plot shared with another title (499), an unservable image (501).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Any

import asyncpg

from spielplan.art.hosts import servable
from spielplan.importer.report import ImportReport

# The corpus's order, verbatim. mpst last: full retellings, ending included.
SOURCE_PRIORITY: tuple[str, ...] = (
    "tmdb", "omdb", "trakt", "tvmaze", "wikipedia", "jellyfin", "wikidata",
    "letterboxd", "rottentomatoes", "metacritic", "mpst",
)

# The card's overview reads these two in order.
_PLOT_FIELDS = ("plot_full", "plot_short")
_CARD_FIELDS = {"tagline": "tagline", "poster_path": "poster_url", "backdrop_path": "backdrop_url"}
_IMAGE_FIELDS = ("poster_path", "backdrop_path")

# Decision 499. Kept in `SOURCE_PRIORITY` so the tuple stays the corpus's verbatim.
NEVER_OVERVIEW = frozenset({"mpst"})

# Decision 499: sources whose plot is matched onto a title (by page or dataset row), where a
# shared text means a wrong film.
MATCHED_TEXT_SOURCES = ("wikipedia", "mpst")

# Same text, same field, another title. Two EXISTS branches so each uses 0037's hash index.
_SHARED_PLOT = """
SELECT m.title_id, m.source, 'plot_full' AS field
  FROM title_meta m
 WHERE m.source = ANY($2::text[]) AND ($1::int[] IS NULL OR m.title_id = ANY($1::int[]))
   AND btrim(m.payload ->> 'plot_full') <> ''
   AND EXISTS (SELECT 1 FROM title_meta o
                WHERE btrim(o.payload ->> 'plot_full') = btrim(m.payload ->> 'plot_full')
                  AND o.title_id <> m.title_id)
UNION ALL
SELECT m.title_id, m.source, 'plot_short' AS field
  FROM title_meta m
 WHERE m.source = ANY($2::text[]) AND ($1::int[] IS NULL OR m.title_id = ANY($1::int[]))
   AND btrim(m.payload ->> 'plot_short') <> ''
   AND EXISTS (SELECT 1 FROM title_meta o
                WHERE btrim(o.payload ->> 'plot_short') = btrim(m.payload ->> 'plot_short')
                  AND o.title_id <> m.title_id)
"""


async def shared_plot_texts(
    conn: asyncpg.Connection, title_ids: Sequence[int] | None = None
) -> set[tuple[int, str, str]]:
    """`(title_id, source, field)` for every matched-source plot another title also carries.

    Decision 499's one definition, also read by `dna/packs.py`. Both members of a pair are flagged.
    """
    ids = None if title_ids is None else list(title_ids)
    rows = await conn.fetch(_SHARED_PLOT, ids, list(MATCHED_TEXT_SOURCES))
    return {(r["title_id"], r["source"], r["field"]) for r in rows}


def card_fields(
    by_source: Mapping[str, Mapping[str, Any]],
    priority: Sequence[str],
    shared: Collection[tuple[str, str]] = (),
) -> dict[str, Any]:
    """One title's card, per field, from its per-source rows: `best()` over what is eligible. Pure."""
    def eligible(field: str, keep) -> dict[str, Mapping[str, Any]]:
        return {s: row for s, row in by_source.items() if keep(s, (row or {}).get(field))}

    def plot(field: str) -> Any:
        return best(
            eligible(field, lambda s, _v: s not in NEVER_OVERVIEW and (s, field) not in shared),
            field, priority,
        )

    card: dict[str, Any] = {
        "overview": next((v for f in _PLOT_FIELDS if (v := plot(f)) is not None), None),
        "tagline": best(by_source, _CARD_FIELDS["tagline"], priority),
    }
    for pg in _IMAGE_FIELDS:
        src = _CARD_FIELDS[pg]
        card[pg] = best(eligible(src, lambda _s, v: servable(v)), src, priority)
    return card


def refused_images(by_source: Mapping[str, Mapping[str, Any]]) -> int:
    """How many image URLs these rows carry on a host this app may not serve (decision 501)."""
    return sum(
        1
        for row in by_source.values()
        for pg in _IMAGE_FIELDS
        if (v := (row or {}).get(_CARD_FIELDS[pg])) not in (None, "", 0) and not servable(v)
    )


def best(by_source: Mapping[str, Mapping[str, Any]], field: str, priority: Sequence[str]) -> Any:
    """`mdc/export.py`'s `_best`, ported.

    Absent values are NULL, '' and 0 (zero `budget` means unknown, not free).
    """
    for source in priority:
        value = (by_source.get(source) or {}).get(field)
        if value not in (None, "", 0):
            return value
    return None


def _payload_columns(db: sqlite3.Connection) -> list[str]:
    """Every shipped column but the two that become the Postgres row's primary key."""
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

    A `payload jsonb` rather than typed columns, so a new corpus source needs no migration.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "title_meta" not in present:
        report.warn("title-meta", "bundle has no `title_meta` — the title card has no source")
        return 0

    columns = _payload_columns(db)
    rows = list(_meta_rows(db, columns))
    await conn.execute("DELETE FROM title_meta")
    # Not COPY: the registered jsonb codec is text-only and binary COPY cannot encode it.
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
    conn: asyncpg.Connection,
    priority: Sequence[str],
    report: ImportReport | None = None,
    title_ids: Sequence[int] | None = None,
) -> None:
    """Resolve §6.0's card fields onto `title`, per field, keeping the per-source rows.

    Only titles with a meta row are touched. `title_ids=None` is the import's whole catalog; a derive
    passes its one title. One function, so both paths share one resolution rule.
    """
    # A predicate, so an empty scope matches nothing rather than every title.
    ids = None if title_ids is None else list(title_ids)

    grouped: dict[int, dict[str, Any]] = {}
    rows = await conn.fetch(
        "SELECT title_id, source, payload FROM title_meta"
        " WHERE ($1::int[] IS NULL OR title_id = ANY($1::int[]))",
        ids,
    )
    for row in rows:
        grouped.setdefault(row["title_id"], {})[row["source"]] = row["payload"]

    # Scoped like the read above; only the named titles are written (decision 375).
    shared: dict[int, set[tuple[str, str]]] = {}
    for title_id, source, field in await shared_plot_texts(conn, ids):
        shared.setdefault(title_id, set()).add((source, field))

    updates = []
    without_overview = refused = 0
    for title_id, by_source in grouped.items():
        card = card_fields(by_source, priority, shared.get(title_id, ()))
        without_overview += card["overview"] is None and any(
            (row or {}).get(f) not in (None, "", 0)
            for row in by_source.values() for f in _PLOT_FIELDS
        )
        refused += refused_images(by_source)
        updates.append(
            (title_id, card["overview"], card["tagline"],
             card["poster_path"], card["backdrop_path"])
        )

    await conn.executemany(
        "UPDATE title SET overview = $2, tagline = $3, poster_path = $4, backdrop_path = $5 "
        " WHERE id = $1",
        updates,
    )

    # No source column on `title_video`: prefer a trailer over a teaser, YouTube over the rest.
    await conn.execute(
        """
        UPDATE title t SET trailer_key = v.key
          FROM (SELECT DISTINCT ON (title_id) title_id, key
                  FROM title_video
                 WHERE key <> '' AND ($1::int[] IS NULL OR title_id = ANY($1::int[]))
                 ORDER BY title_id, (lower(type) = 'trailer') DESC, (site = 'YouTube') DESC, key
               ) v
         WHERE v.title_id = t.id
        """,
        ids,
    )

    if report is None:
        return

    # The count follows the scope.
    trailers = await conn.fetchval(
        "SELECT count(*) FROM title WHERE trailer_key IS NOT NULL"
        " AND ($1::int[] IS NULL OR id = ANY($1::int[]))",
        ids,
    )
    report.note(
        "title-card",
        f"{len(updates)} title cards resolved per field from title_meta; "
        f"{trailers} carry a trailer key",
        titles=len(updates),
    )
    # State the narrowings with counts, or a stripped card looks like a title with no data.
    report.note(
        "title-card",
        f"{without_overview} titles carry plot text only from MPST or text another title shares, "
        f"and show no overview (decision 499); {refused} poster/backdrop URLs on hosts this app "
        "may not serve were skipped (decision 501)",
        without_overview=without_overview, refused_images=refused,
    )
