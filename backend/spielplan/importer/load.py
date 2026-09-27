"""Load a validated bundle's content into Postgres (§4.1, §10).

An unmapped bundle column is a report line; a mapped column the bundle lacks is a failure. Every
shipped table is mapped, loaded bespoke, or skipped with a reason; anything else fails the import.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg

from spielplan.importer import meta
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
    # pg column -> converter, where the corpus stores a fact in a different type than this app.
    transforms: dict[str, Callable[[object], object]] = field(default_factory=dict)
    required: bool = False
    # This table's PRIMARY KEY as the migrations declare it; the validator counts duplicate groups by
    # it. Empty for surrogate-keyed tables (`credit`, `award`: dedupe at read time).
    key: tuple[str, ...] = ()

    @property
    def pg_columns(self) -> list[str]:
        return list(self.columns)


def _award_won(value: object) -> bool | None:
    """The corpus records an outcome as text; only `won` is a win (unknown means nominated)."""
    if value is None:
        return None
    return str(value).strip().lower() == "won"


def _primary_role(value: object) -> str:
    """`is_primary` (0/1) becomes the role this app keys a language row by."""
    return "primary" if value else ""


def _scale_label(value: object) -> str | None:
    """`scale_hi` becomes the app's one-line scale label; the upper bound tells 0-10 from 0-100."""
    if value is None:
        return None
    number = float(value)
    return f"{number:g}"


# The load order is FK order. `credit` follows `person`; everything follows `title`.
# Source column names are the corpus's, per `tests/fixtures/real_bundle_shapes.json`.
MAPPINGS: tuple[TableMap, ...] = (
    TableMap(
        target="title",
        source="title",
        key=("id",),
        columns={
            "id": "id", "kind": "kind",
            # The corpus's names for the two title columns.
            "name": "primary_title", "original_name": "original_title",
            "year": "year", "runtime_min": "runtime_min", "imdb_id": "imdb_id",
            "tmdb_id": "tmdb_id", "tvdb_id": "tvdb_id", "trakt_id": "trakt_id",
            "letterboxd_slug": "letterboxd_slug",
            "rt_slug": "rt_slug", "metacritic_slug": "metacritic_slug",
            "jellyfin_id": "jellyfin_id", "is_owned": "is_owned",
            # The tower's `lang:` meta column is built from this alone.
            "original_language": "original_language",
            # overview / tagline / poster_path / backdrop_path / trailer_key are resolved per field from
            # `title_meta` and `title_video` after the load, by `resolve_title_fields`.
        },
        # NOT NULL, and §7.2 re-derives it from Jellyfin anyway.
        bool_columns=("is_owned",),
        bool_defaults={"is_owned": False},
        required=True,
    ),
    TableMap(
        target="title_alias",
        source="title_alias",
        key=("title_id", "alias", "region", "language", "kind"),
        # The bundle has no `kind` on an alias; it has `source`, which is the droppable unit.
        columns={"title_id": "title_id", "alias": "alias", "region": "region",
                 "language": "language", "kind": "source"},
        coalesce_empty=("region", "language", "kind"),   # rule 6
    ),
    TableMap(
        target="title_genre", source="title_genre",
        key=("title_id", "genre", "source"),
        columns={"title_id": "title_id", "genre": "genre", "source": "source"},
        coalesce_empty=("source",),
    ),
    TableMap(
        target="title_keyword", source="title_keyword",
        key=("title_id", "keyword", "source"),
        columns={"title_id": "title_id", "keyword": "keyword", "source": "source"},
        coalesce_empty=("source",),
    ),
    TableMap(
        target="title_language", source="title_language",
        key=("title_id", "source", "language", "role"),
        # `role` (is it the main language) and `source` (who said so) are both key columns (0015).
        columns={"title_id": "title_id", "source": "source", "language": "language",
                 "role": "is_primary"},
        coalesce_empty=("source", "role"),   # rule 6
        transforms={"role": _primary_role},
    ),
    TableMap(
        target="title_country", source="title_country",
        key=("title_id", "source", "country"),
        # `source` is a key column here too (0015).
        columns={"title_id": "title_id", "source": "source", "country": "country"},
        coalesce_empty=("source",),   # rule 6
    ),
    TableMap(
        target="title_company", source="title_company",
        key=("title_id", "source", "company", "role"),
        # Keyed (title_id, source, company, role) since 0018. The company's own `country` is unmapped.
        columns={"title_id": "title_id", "source": "source", "company": "company",
                 "role": "role"},
        coalesce_empty=("source", "role"),   # rule 6
    ),
    TableMap(
        target="title_video", source="title_video",
        key=("title_id", "source", "key"),
        # No `official` upstream; it stays NULL rather than invented. `source` is a key column (0018).
        columns={"title_id": "title_id", "source": "source", "site": "site", "key": "key",
                 "type": "type"},
        coalesce_empty=("site", "type", "source"),
    ),
    TableMap(
        target="person", source="person",
        key=("id",),
        columns={"id": "id", "name": "name", "imdb_id": "imdb_id", "tmdb_id": "tmdb_id",
                 "birth_year": "birth_year", "profile_path": "profile_path"},
    ),
    TableMap(
        target="credit", source="credit",
        # `role_class` feeds the feature contract's `p:<role_class>:<name>` grammar.
        columns={"title_id": "title_id", "person_id": "person_id",
                 "department": "department", "job": "job", "character": "character",
                 "billing_order": "billing_order", "source": "source",
                 "role_class": "role_class"},
        coalesce_empty=("department", "job", "source"),
    ),
    TableMap(
        target="award", source="award",
        # The corpus records the outcome as text; `_award_won` casts it.
        columns={"title_id": "title_id", "body": "award", "category": "category",
                 "year": "year", "won": "result"},
        coalesce_empty=("category",),
        transforms={"won": _award_won},
    ),
    TableMap(
        target="rating_source", source="rating_source",
        key=("id",),
        # §4.1 rule 4's frozen ids. url/license/version/notes carry the per-dataset terms (0018) and are
        # not coalesced: no terms must read as "not stated".
        columns={"id": "id", "name": "name", "scale": "scale_hi", "url": "url",
                 "license": "license", "version": "version", "notes": "notes"},
        coalesce_empty=("scale",),
        transforms={"scale": _scale_label},
        required=True,
    ),
    TableMap(
        target="rating_title_map", source="rating_title_map",
        key=("source_id", "source_key"),
        # The corpus's name for the key it maps from is `external_id`.
        columns={"source_id": "source_id", "source_key": "external_id", "title_id": "title_id"},
    ),
    # rule 3 — the display-only schema. Nothing else in this tuple targets it.
    TableMap(
        target="display.platform_rating", source="platform_rating",
        key=("title_id", "platform", "metric"),
        # Keyed (title_id, platform, metric): several metrics per source. `scale` travels with the number.
        columns={"title_id": "title_id", "platform": "source", "metric": "metric",
                 "score": "value", "scale": "scale", "votes": "votes"},
        coalesce_empty=("platform", "metric"),   # rule 6
    ),
    # The corpus's `seed_list` is a 238-row list registry, not §4.3's onboarding list, which loads
    # from `seed_list.json`.
    TableMap(
        target="title_list", source="seed_list",
        key=("id",),
        columns={"id": "id", "slug": "slug", "name": "name", "source": "source",
                 "kind": "kind", "category": "category", "weight": "weight",
                 "item_count": "item_count", "notes": "notes"},
        coalesce_empty=("name", "source"),
    ),
    TableMap(
        target="title_list_membership", source="title_list_membership",
        key=("list_id", "title_id"),
        columns={"list_id": "list_id", "title_id": "title_id", "rank": "rank"},
    ),
    # No `source` or `added_at` upstream: column defaults.
    TableMap(
        target="watchlist", source="watchlist",
        key=("title_id",),
        columns={"title_id": "title_id"},
    ),
)

# Bundle tables deliberately not loaded through MAPPINGS, with the reason reported.
BESPOKE_TABLES: dict[str, str] = {
    "title_meta": "loaded per source into title_meta.payload, then resolved per field onto title",
    "dna_tag": "loaded with its evidence by importer.dna.load_tags (§4.1 rule 1)",
    "dna_evidence": "loaded with dna_tag; keyed (title_id, term) upstream",
    "dna_projected": "loaded by importer.dna.load_projected — a separate statement, never a union",
}

SKIPPED_TABLES: dict[str, str] = {
    "imdb_ratings": "pre-selection signal for the corpus's own crawl; the app shows IMDb's "
                    "number from platform_rating, which §4.1 rule 3 keeps display-only",
    # Decision 291: the MovieLens genome slice is a validation artifact, never imported. The tower
    # treats the block as zero-imputed; cold-masked titles lost real input (decision 304). Installs
    # seeded earlier keep their rows (decision 311).
    "ml_genome_tag": "the genome's tag vocabulary; media-graph-spec_v1.1.md:175 makes the whole "
                     "slice a corpus-side validation artifact (decision 291)",
    "ml_link": "MovieLens ids for a genome this app no longer imports; "
               "media-graph-spec_v1.1.md:175, decision 291",
    "ml_genome_score": "the genome relevance scores for a block no import this build populates; "
                       "media-graph-spec_v1.1.md:175, decisions 291 and 311",
    "dna_annotation": "curator working notes; no app surface reads one",
    "dna_term_signal": "vocabulary-building telemetry, superseded by the shipped vocabulary",
    "dna_exclusion": "the corpus's own extraction exclusions, applied before export",
    "dna_projection_run": "provenance of the corpus's wholesale projection runs",
    "sqlite_sequence": "SQLite bookkeeping, not data",
}


def unaccounted_tables(db: sqlite3.Connection) -> list[str]:
    """Tables the bundle ships that nothing above claims."""
    shipped = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    claimed = {m.source for m in MAPPINGS} | set(BESPOKE_TABLES) | set(SKIPPED_TABLES)
    return sorted(t for t in shipped - claimed if not t.startswith("sqlite_"))


def _sqlite_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def _rows(db: sqlite3.Connection, tmap: TableMap) -> Iterator[tuple]:
    """Stream one bundle table as tuples in `pg_columns` order.

    Coalesces NULL key components to '' (rule 6) and casts SQLite ints/strings to boolean and
    timestamptz, which asyncpg's binary COPY requires.
    """
    select = ", ".join(f'"{tmap.columns[c]}"' for c in tmap.pg_columns)
    coalesce_idx = {i for i, c in enumerate(tmap.pg_columns) if c in tmap.coalesce_empty}
    bool_idx = {i: tmap.bool_defaults.get(c) for i, c in enumerate(tmap.pg_columns)
                if c in tmap.bool_columns}
    ts_idx = {i for i, c in enumerate(tmap.pg_columns) if c in tmap.timestamp_columns}
    fn_idx = {i: tmap.transforms[c] for i, c in enumerate(tmap.pg_columns)
              if c in tmap.transforms}

    for row in db.execute(f'SELECT {select} FROM "{tmap.source}"'):
        if coalesce_idx or bool_idx or ts_idx or fn_idx:
            out = list(row)
            # Transforms first: they produce this app's representation, which the casts below assume.
            for i, fn in fn_idx.items():
                out[i] = fn(out[i])
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
    still loads.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# §10: Ledger observations survive re-import. Four tables reference `title(id) ON DELETE RESTRICT`
# (0022), so `title` is upserted, never deleted; all other mapped content is replaced.
_TITLE_TARGET = "title"


def _split(target: str) -> tuple[str, str]:
    return (target.split(".")[0], target.split(".")[-1]) if "." in target else ("public", target)


async def _clear(conn: asyncpg.Connection, target: str) -> None:
    schema, table = _split(target)
    await conn.execute(f'DELETE FROM {schema}."{table}"')


async def _reap_display_orphans(conn: asyncpg.Connection) -> None:
    """Clear `display.platform_rating` rows whose title is no longer in the catalogue.

    That schema has no FK on purpose (rule 3), so the reload cleans up. `NOT IN` is safe: `title.id`
    is NOT NULL.
    """
    await conn.execute(
        "DELETE FROM display.platform_rating WHERE title_id NOT IN (SELECT id FROM title)"
    )


async def _copy(conn: asyncpg.Connection, tmap: TableMap, db: sqlite3.Connection) -> int:
    schema, table = _split(tmap.target)
    written = await conn.copy_records_to_table(
        table, schema_name=schema, columns=tmap.pg_columns, records=_rows(db, tmap)
    )
    return int(str(written).rsplit(" ", 1)[-1]) if str(written).startswith("COPY") else 0


async def _upsert_titles(
    conn: asyncpg.Connection, tmap: TableMap, db: sqlite3.Connection
) -> int:
    """Load `title` through a temp table so a re-import updates rows instead of deleting them."""
    cols = tmap.pg_columns
    col_list = ", ".join(f'"{c}"' for c in cols)
    await conn.execute(
        "CREATE TEMP TABLE _import_title (LIKE title INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    await conn.copy_records_to_table("_import_title", columns=cols, records=_rows(db, tmap))
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != "id")
    await conn.execute(
        f"""
        INSERT INTO title ({col_list})
        SELECT {col_list} FROM _import_title
        ON CONFLICT (id) DO UPDATE SET {updates}, updated_at = now()
        """
    )
    return await conn.fetchval("SELECT count(*) FROM _import_title")


def _account_for_shipped_tables(db: sqlite3.Connection, report: ImportReport) -> bool:
    """§10's "counts per table" for the tables the bundle ships: declined ones are named, unclaimed
    ones fail the import.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in sorted(present & set(SKIPPED_TABLES)):
        report.skip_table(table, SKIPPED_TABLES[table])
    for table in sorted(present & set(BESPOKE_TABLES)):
        report.note("table-bespoke", f"`{table}`: {BESPOKE_TABLES[table]}", table=table)

    orphans = unaccounted_tables(db)
    if orphans:
        report.fail(
            "load",
            "bundle ships table(s) this importer accounts for nowhere: " + ", ".join(orphans),
            tables=orphans,
        )
    return not orphans


async def load_content(
    conn: asyncpg.Connection, db: sqlite3.Connection, report: ImportReport
) -> ImportReport:
    """Load the bundle's content tables into Postgres inside the caller's transaction.

    Idempotent: a re-import must succeed.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not _account_for_shipped_tables(db, report):
        return report

    usable: list[TableMap] = []
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

        # A mapped column the bundle lacks is a failure, never a column of NULLs.
        absent = sorted(set(tmap.columns.values()) - set(available))
        if absent:
            report.fail(
                "load",
                f"`{tmap.source}` has no column(s) {absent} — the mapping names a column the "
                "bundle does not ship",
                table=tmap.source, columns=absent,
            )
            continue
        usable.append(tmap)

    if not report.ok:
        return report

    # Three passes: clear derived tables children first, upsert `title` (never delete: RESTRICT),
    # refill parents first.
    for tmap in reversed(usable):
        if tmap.target != _TITLE_TARGET:
            await _clear(conn, tmap.target)

    for tmap in usable:
        if tmap.target == _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _upsert_titles(conn, tmap, db)

    for tmap in usable:
        if tmap.target != _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _copy(conn, tmap, db)

    # After the refill, and unconditionally.
    await _reap_display_orphans(conn)

    # After the derived tables, because the trailer key is read from `title_video`.
    await meta.load_title_meta(conn, db, report)
    await meta.resolve_title_fields(conn, meta.SOURCE_PRIORITY, report)

    # rule: is_owned is re-derived from Jellyfin, never trusted stale (§7.2).
    await conn.execute("UPDATE title SET owned_checked_at = NULL")
    report.note(
        "owned-flag",
        "is_owned imported but marked unverified — §7.2 re-derives it from Jellyfin, "
        "the corpus flag goes stale the moment the library changes",
    )
    return report
