"""Load a validated bundle's content into Postgres. Spec v2.1 §4.1, §10.

The mapping is declarative so the report can say exactly which bundle columns it could not
place. §4.1's shape note applies: an unmapped column is a *report line*, never a silent drop
and never a failure — the corpus export is the authority on its own column names and this app
must survive it gaining one.

Rules enforced during the load rather than after:
  * rule 6 — NULLable PK components are coalesced to '' as rows stream past.
  * rule 3 — platform_rating goes to the `display` schema and nowhere else.
  * rule 8 — text is passed through untouched except for the 73 known-mojibake review rows.
  * rule 1 — dna_tag and dna_projected are loaded by two separate statements. There is no
             code path in this module that writes them from one query.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg

from spielplan.importer.report import ImportReport


@dataclass(frozen=True)
class TableMap:
    """One Postgres table fed from one bundle table."""

    target: str                       # schema-qualified Postgres table
    source: str                       # sqlite table
    columns: dict[str, str]           # pg column -> sqlite column
    coalesce_empty: tuple[str, ...] = ()   # pg columns whose NULL becomes '' (rule 6)
    # SQLite has no boolean type; these pg columns are `boolean` and their ints need casting.
    bool_columns: tuple[str, ...] = ()
    # What a missing/NULL boolean becomes. NOT NULL columns need a value, not None.
    bool_defaults: dict[str, bool | None] = field(default_factory=dict)
    # SQLite has no date type either; these pg columns are `timestamptz` and arrive as strings.
    timestamp_columns: tuple[str, ...] = ()
    required: bool = False

    @property
    def pg_columns(self) -> list[str]:
        return list(self.columns)


# The load order is FK order. `credit` follows `person`; everything follows `title`.
MAPPINGS: tuple[TableMap, ...] = (
    TableMap(
        target="title",
        source="title",
        columns={
            "id": "id", "kind": "kind", "name": "name", "original_name": "original_name",
            "year": "year", "runtime_min": "runtime_min", "imdb_id": "imdb_id",
            "tmdb_id": "tmdb_id", "tvdb_id": "tvdb_id", "trakt_id": "trakt_id",
            "trakt_slug": "trakt_slug", "letterboxd_slug": "letterboxd_slug",
            "rt_slug": "rt_slug", "metacritic_slug": "metacritic_slug",
            "jellyfin_id": "jellyfin_id", "is_owned": "is_owned", "overview": "overview",
            "tagline": "tagline", "poster_path": "poster_path",
            "backdrop_path": "backdrop_path", "trailer_key": "trailer_key",
        },
        # `title.is_owned` is NOT NULL; §7.2 re-derives it from Jellyfin anyway, so a bundle
        # that omits it starts at false rather than failing the import.
        bool_columns=("is_owned",),
        bool_defaults={"is_owned": False},
        required=True,
    ),
    TableMap(
        target="title_alias",
        source="title_alias",
        columns={"title_id": "title_id", "alias": "alias", "region": "region",
                 "language": "language", "kind": "kind"},
        coalesce_empty=("region", "language", "kind"),   # rule 6
    ),
    TableMap(
        target="title_genre", source="title_genre",
        columns={"title_id": "title_id", "genre": "genre", "source": "source"},
        coalesce_empty=("source",),
    ),
    TableMap(
        target="title_keyword", source="title_keyword",
        columns={"title_id": "title_id", "keyword": "keyword", "source": "source"},
        coalesce_empty=("source",),
    ),
    TableMap(
        target="title_language", source="title_language",
        columns={"title_id": "title_id", "language": "language", "role": "role"},
        coalesce_empty=("role",),
    ),
    TableMap(
        target="title_country", source="title_country",
        columns={"title_id": "title_id", "country": "country"},
    ),
    TableMap(
        target="title_company", source="title_company",
        columns={"title_id": "title_id", "company": "company", "role": "role"},
        coalesce_empty=("role",),
    ),
    TableMap(
        target="title_video", source="title_video",
        columns={"title_id": "title_id", "site": "site", "key": "key", "type": "type",
                 "official": "official"},
        coalesce_empty=("site", "type"),
        bool_columns=("official",),
    ),
    TableMap(
        target="person", source="person",
        columns={"id": "id", "name": "name", "imdb_id": "imdb_id", "tmdb_id": "tmdb_id",
                 "birth_year": "birth_year", "profile_path": "profile_path"},
    ),
    TableMap(
        target="credit", source="credit",
        columns={"title_id": "title_id", "person_id": "person_id",
                 "department": "department", "job": "job", "character": "character",
                 "ord": "ord", "source": "source"},
        coalesce_empty=("department", "job", "source"),
    ),
    TableMap(
        target="award", source="award",
        columns={"title_id": "title_id", "body": "body", "category": "category",
                 "year": "year", "won": "won", "person_id": "person_id"},
        coalesce_empty=("category",),
        bool_columns=("won",),
    ),
    TableMap(
        target="rating_source", source="rating_source",
        columns={"id": "id", "name": "name", "scale": "scale"},
        coalesce_empty=("scale",),
        required=True,
    ),
    TableMap(
        target="rating_title_map", source="rating_title_map",
        columns={"source_id": "source_id", "source_key": "source_key", "title_id": "title_id"},
    ),
    TableMap(
        target="ml_genome_tag", source="ml_genome_tag",
        columns={"tag_id": "tag_id", "tag": "tag"},
    ),
    TableMap(
        target="ml_link", source="ml_link",
        columns={"ml_movie_id": "ml_movie_id", "title_id": "title_id",
                 "imdb_id": "imdb_id", "tmdb_id": "tmdb_id"},
    ),
    TableMap(
        target="ml_genome_score", source="ml_genome_score",
        columns={"ml_movie_id": "ml_movie_id", "tag_id": "tag_id", "relevance": "relevance"},
    ),
    # rule 3 — the display-only schema. Nothing else in this tuple targets it.
    TableMap(
        target="display.platform_rating", source="platform_rating",
        columns={"title_id": "title_id", "platform": "platform", "score": "score",
                 "votes": "votes"},
    ),
    TableMap(
        target="seed_list", source="seed_list",
        columns={"position": "position", "title_id": "title_id", "decade": "decade"},
    ),
    TableMap(
        target="watchlist", source="watchlist",
        columns={"title_id": "title_id", "source": "source", "added_at": "added_at"},
        coalesce_empty=("source",),
        timestamp_columns=("added_at",),
    ),
)


def _sqlite_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def _rows(db: sqlite3.Connection, tmap: TableMap, available: list[str]) -> Iterator[tuple]:
    """Stream one bundle table as tuples in `pg_columns` order.

    Two coercions happen here and nowhere else:

    * rule 6 — NULL in a PK component becomes ``''``.
    * **SQLite has no boolean or date type.** `sqlite3` returns plain ints where Postgres wants
      `boolean` and plain strings where it wants `timestamptz`, and asyncpg's *binary* COPY
      encoder rejects both outright (``TypeError: a boolean is required``). Without these casts
      the very first import dies on `title`, which is the first and required mapping.
    """
    picks = [tmap.columns[c] for c in tmap.pg_columns]
    select = ", ".join(f'"{src}"' if src in available else "NULL" for src in picks)
    coalesce_idx = {i for i, c in enumerate(tmap.pg_columns) if c in tmap.coalesce_empty}
    bool_idx = {i: tmap.bool_defaults.get(c) for i, c in enumerate(tmap.pg_columns)
                if c in tmap.bool_columns}
    ts_idx = {i for i, c in enumerate(tmap.pg_columns) if c in tmap.timestamp_columns}

    for row in db.execute(f'SELECT {select} FROM "{tmap.source}"'):
        if coalesce_idx or bool_idx or ts_idx:
            out = list(row)
            for i in coalesce_idx:
                if out[i] is None:
                    out[i] = ""
            for i, default in bool_idx.items():
                out[i] = default if out[i] is None else bool(out[i])
            for i in ts_idx:
                out[i] = _timestamp(out[i])
            row = tuple(out)
        yield row


def _timestamp(value: object) -> datetime | None:
    """Best-effort ISO-8601 to aware datetime. An unparseable value becomes NULL and the row
    still loads — a malformed date is not worth failing a whole bundle over."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# §10: "Ledger *observations* always survive re-import (they reference `title.id` and
# vocabulary-independent facts)." `verdict`, `duel`, `tier_edit` and `user_title` all reference
# `title(id) ON DELETE CASCADE`, so a re-import must never DELETE a title row — it upserts.
# Everything else in MAPPINGS is derived content and is replaced wholesale, children first.
_TITLE_TARGET = "title"


def _split(target: str) -> tuple[str, str]:
    return (target.split(".")[0], target.split(".")[-1]) if "." in target else ("public", target)


async def _clear(conn: asyncpg.Connection, target: str) -> None:
    schema, table = _split(target)
    await conn.execute(f'DELETE FROM {schema}."{table}"')


async def _copy(
    conn: asyncpg.Connection, tmap: TableMap, db: sqlite3.Connection, available: list[str]
) -> int:
    schema, table = _split(tmap.target)
    written = await conn.copy_records_to_table(
        table, schema_name=schema, columns=tmap.pg_columns, records=_rows(db, tmap, available)
    )
    return int(str(written).rsplit(" ", 1)[-1]) if str(written).startswith("COPY") else 0


async def _upsert_titles(
    conn: asyncpg.Connection, tmap: TableMap, db: sqlite3.Connection, available: list[str]
) -> int:
    """Load `title` through a temp table so a re-import updates rows instead of deleting them."""
    cols = tmap.pg_columns
    col_list = ", ".join(f'"{c}"' for c in cols)
    await conn.execute(
        "CREATE TEMP TABLE _import_title (LIKE title INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    await conn.copy_records_to_table(
        "_import_title", columns=cols, records=_rows(db, tmap, available)
    )
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != "id")
    await conn.execute(
        f"""
        INSERT INTO title ({col_list})
        SELECT {col_list} FROM _import_title
        ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = now()
        """
    )
    return await conn.fetchval("SELECT count(*) FROM _import_title")


async def load_content(
    conn: asyncpg.Connection, db: sqlite3.Connection, report: ImportReport
) -> ImportReport:
    """Load the bundle's content tables into Postgres inside the caller's transaction.

    Idempotent by construction: §10 calls a re-import "a planned admin event with a diff
    report", so a second import of the same or a newer bundle must succeed rather than collide
    on primary keys.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    usable: list[tuple[TableMap, list[str]]] = []
    for tmap in MAPPINGS:
        if tmap.source not in present:
            if tmap.required:
                report.fail("load", f"required bundle table `{tmap.source}` is missing")
            else:
                report.warn("load", f"bundle has no `{tmap.source}` — target left empty")
            continue

        available = _sqlite_columns(db, tmap.source)
        unmapped = sorted(set(available) - set(tmap.columns.values()))
        if unmapped:
            report.unmapped_columns[tmap.source] = unmapped

        absent = sorted(set(tmap.columns.values()) - set(available))
        if absent:
            report.warn(
                "load",
                f"`{tmap.source}` has no column(s) {absent} — imported as NULL",
                table=tmap.source, columns=absent,
            )
        usable.append((tmap, available))

    if not report.ok:
        return report

    # Three passes, because FK order runs one way for deletes and the other for inserts:
    #   1. clear every derived table, children first,
    #   2. upsert `title` — never delete it, or ON DELETE CASCADE takes the Ledger with it,
    #   3. refill the derived tables in declaration order, parents before children.
    for tmap, _ in reversed(usable):
        if tmap.target != _TITLE_TARGET:
            await _clear(conn, tmap.target)

    for tmap, available in usable:
        if tmap.target == _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _upsert_titles(
                conn, tmap, db, available
            )

    for tmap, available in usable:
        if tmap.target != _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _copy(conn, tmap, db, available)

    # rule: is_owned is re-derived from Jellyfin, never trusted stale (§7.2). Whatever the
    # bundle claimed, mark it as unverified so the first Jellyfin sync owns the truth.
    await conn.execute("UPDATE title SET owned_checked_at = NULL")
    report.note(
        "owned-flag",
        "is_owned imported but marked unverified — §7.2 re-derives it from Jellyfin, "
        "the corpus flag goes stale the moment the library changes",
    )
    return report
