"""Load a validated bundle's content into Postgres. Spec v2.1 §4.1, §10.

The mapping is declarative so the report can say exactly which bundle columns it could not
place. §4.1's shape note applies in one direction: a bundle column this app does not map is a
*report line*, never a silent drop and never a failure — the corpus export is the authority on
its own column names and this app must survive it gaining one. The inverse is a failure: a
column the mapping names and the bundle does not have is this app asserting a name upstream
never had, and loading it as NULLs is how `title.name` read nothing from `primary_title`.

Every table the bundle ships is accounted for before anything is written: mapped, loaded by a
bespoke path, or named as skipped with a reason. A table none of the three claims fails the
import (§10's "counts per table").

Rules enforced during the load rather than after:
  * rule 6 — NULLable PK components are coalesced to '' as rows stream past.
  * rule 3 — platform_rating goes to the `display` schema and nowhere else.
  * rule 8 — text is passed through untouched except for the 73 known-mojibake review rows.
  * rule 1 — dna_tag and dna_projected are loaded by two separate statements. There is no
             code path in this module that writes them from one query.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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
    # pg column -> converter, for the handful of places the corpus records a fact in a different
    # type than this app stores it: an award outcome as text where the app keeps a boolean, a
    # rating scale as two numeric bounds where the app keeps one label.
    transforms: dict[str, Callable[[object], object]] = field(default_factory=dict)
    required: bool = False

    @property
    def pg_columns(self) -> list[str]:
        return list(self.columns)


def _award_won(value: object) -> bool | None:
    """The corpus records an outcome as text; this app keeps a boolean.

    `won` is the only true value: §4.3's award block counts wins and nominations separately, and
    an unknown outcome is a nomination on the record rather than a win — the same reading
    `features.py`'s `won IS NOT TRUE` already takes.
    """
    if value is None:
        return None
    return str(value).strip().lower() == "won"


def _primary_role(value: object) -> str:
    """`is_primary` (0/1) becomes the role this app keys a language row by."""
    return "primary" if value else ""


def _scale_label(value: object) -> str | None:
    """`scale_hi` becomes the app's one-line scale label. The lower bound is almost always 0
    or 1 and the upper is what distinguishes a 0-10 source from a 0-100 one."""
    if value is None:
        return None
    number = float(value)
    return f"{number:g}"


# The load order is FK order. `credit` follows `person`; everything follows `title`.
#
# Every `source` column below is a column the corpus actually exports. Until M4.5 this table was
# identity pairs against an imagined schema — `"name": "name"` where the bundle ships
# `primary_title` — so essentially every table would have loaded empty or failed. The names are
# now taken from `tests/fixtures/real_bundle_shapes.json`, and `test_bundle_shapes.py` fails if
# the fixture and a real bundle drift apart.
MAPPINGS: tuple[TableMap, ...] = (
    TableMap(
        target="title",
        source="title",
        columns={
            "id": "id", "kind": "kind",
            # The corpus's names for the two title columns. `title.name` is this app's column;
            # `primary_title` is the bundle's, and the two were assumed identical.
            "name": "primary_title", "original_name": "original_title",
            "year": "year", "runtime_min": "runtime_min", "imdb_id": "imdb_id",
            "tmdb_id": "tmdb_id", "tvdb_id": "tvdb_id", "trakt_id": "trakt_id",
            "letterboxd_slug": "letterboxd_slug",
            "rt_slug": "rt_slug", "metacritic_slug": "metacritic_slug",
            "jellyfin_id": "jellyfin_id", "is_owned": "is_owned",
            # The tower's meta block carries exactly one `lang:` column per title and the corpus
            # builds it from this column alone (§4.2's "the same features the model was trained
            # on"); `title_language` is a different, multi-source fact. Without the column here
            # the seed leaves it NULL and the block never fires on a real bundle.
            "original_language": "original_language",
            # overview / tagline / poster_path / backdrop_path / trailer_key are NOT here: the
            # corpus does not export them on `title` at all. They are resolved per field from
            # `title_meta` and `title_video` after the load — §4.1's "one block = one droppable
            # source" — by `resolve_title_fields`.
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
        # The bundle has no `kind` on an alias; it has `source`, which is the droppable unit.
        columns={"title_id": "title_id", "alias": "alias", "region": "region",
                 "language": "language", "kind": "source"},
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
        # Two different facts, and 0015 keys the row by both (owner decision 2026-09-02).
        # `role` is `is_primary` — whether this is the title's main language; `source` is who
        # said so. Dropping `source` collapsed the corpus's four language sources onto one row
        # per (title_id, language, role), which is 17,342 duplicate groups in the shipped
        # bundle and a unique violation that rolls the whole seed back.
        columns={"title_id": "title_id", "source": "source", "language": "language",
                 "role": "is_primary"},
        coalesce_empty=("source", "role"),   # rule 6
        transforms={"role": _primary_role},
    ),
    TableMap(
        target="title_country", source="title_country",
        # Same key correction, same reason: 19,092 duplicate groups under (title_id, country).
        columns={"title_id": "title_id", "source": "source", "country": "country"},
        coalesce_empty=("source",),   # rule 6
    ),
    TableMap(
        target="title_video", source="title_video",
        # No `official` upstream; it stays NULL rather than being invented as true.
        columns={"title_id": "title_id", "site": "site", "key": "key", "type": "type"},
        coalesce_empty=("site", "type"),
    ),
    TableMap(
        target="person", source="person",
        columns={"id": "id", "name": "name", "imdb_id": "imdb_id", "tmdb_id": "tmdb_id",
                 "birth_year": "birth_year", "profile_path": "profile_path"},
    ),
    TableMap(
        target="credit", source="credit",
        # `billing_order` and `role_class` are the corpus's names. `role_class` is what the
        # feature contract's `p:<role_class>:<name>` grammar is built from (§4.3), so losing it
        # is losing the credit block.
        columns={"title_id": "title_id", "person_id": "person_id",
                 "department": "department", "job": "job", "character": "character",
                 "billing_order": "billing_order", "source": "source",
                 "role_class": "role_class"},
        coalesce_empty=("department", "job", "source"),
    ),
    TableMap(
        target="award", source="award",
        # The corpus records the outcome as text (`won` | `nominated`); this app stores a
        # boolean. `_award_won` casts it, rather than leaving a NULL `won` on every award.
        columns={"title_id": "title_id", "body": "award", "category": "category",
                 "year": "year", "won": "result"},
        coalesce_empty=("category",),
        transforms={"won": _award_won},
    ),
    TableMap(
        target="rating_source", source="rating_source",
        # §4.1 rule 4's frozen ids. The corpus records the scale as two bounds, not one string.
        columns={"id": "id", "name": "name", "scale": "scale_hi"},
        coalesce_empty=("scale",),
        transforms={"scale": _scale_label},
        required=True,
    ),
    TableMap(
        target="rating_title_map", source="rating_title_map",
        # The corpus's name for the key it maps from is `external_id`; `source_key` is this
        # app's column and was being read from the bundle as well.
        columns={"source_id": "source_id", "source_key": "external_id", "title_id": "title_id"},
    ),
    TableMap(
        target="ml_genome_tag", source="ml_genome_tag",
        columns={"tag_id": "tag_id", "tag": "tag"},
    ),
    TableMap(
        target="ml_link", source="ml_link",
        # `movie_id` is the MovieLens id under the corpus's name. There is no `title_id` here:
        # the corpus exports the link table as MovieLens publishes it, keyed by external ids,
        # and `_resolve_ml_links` below joins it to `title` after the load rather than mapping a
        # column the bundle does not have and loading NULLs.
        columns={"ml_movie_id": "movie_id", "imdb_id": "imdb_id", "tmdb_id": "tmdb_id"},
    ),
    TableMap(
        target="ml_genome_score", source="ml_genome_score",
        columns={"ml_movie_id": "movie_id", "tag_id": "tag_id", "relevance": "relevance"},
    ),
    # rule 3 — the display-only schema. Nothing else in this tuple targets it.
    TableMap(
        target="display.platform_rating", source="platform_rating",
        # The corpus keys this (title_id, source, metric) and records several metrics per
        # source — user_score beside critic_score, and the unscaled popularity and vote-count
        # metrics. Keyed on (title_id, platform) alone that is 32,463 duplicate groups, and
        # whichever row COPY happened to reach last would have been the one on the card.
        # `scale` travels with the number because §6.0 wants the caption with it: 8.3 means
        # nothing until the row also says out of 10.
        columns={"title_id": "title_id", "platform": "source", "metric": "metric",
                 "score": "value", "scale": "scale", "votes": "votes"},
        coalesce_empty=("platform", "metric"),   # rule 6
    ),
    # The corpus's `seed_list` is a 238-row list REGISTRY, not §4.3's 100-title onboarding list.
    # They collided on the name until M4.5, and mapping one onto the other COPYs 238 all-NULL
    # rows into a NOT NULL primary key. The onboarding list is loaded from `seed_list.json`.
    TableMap(
        target="title_list", source="seed_list",
        columns={"id": "id", "slug": "slug", "name": "name", "source": "source",
                 "kind": "kind", "category": "category", "weight": "weight",
                 "item_count": "item_count", "notes": "notes"},
        coalesce_empty=("name", "source"),
    ),
    TableMap(
        target="title_list_membership", source="title_list_membership",
        columns={"list_id": "list_id", "title_id": "title_id", "rank": "rank"},
    ),
    # The corpus builds this table live at export time as `watchlist(rank, title_id, record)`
    # — there is no `source` and no `added_at` to read. Both are left to their column defaults,
    # and `rank`/`record` are reported as unmapped bundle columns like any others.
    TableMap(
        target="watchlist", source="watchlist",
        columns={"title_id": "title_id"},
    ),
)

# Bundle tables this app deliberately does not load through MAPPINGS, with the reason. An
# unmapped table used to be invisible — `ImportReport` tracked unmapped *columns within mapped
# tables* — so `title_meta` (46,318 rows), `title_list_membership` and `imdb_ratings` vanished
# without a line anywhere. Every table the bundle ships is now either mapped above, loaded by a
# bespoke path, or named here, and `unaccounted_tables` reports anything that is none of the
# three rather than dropping it in silence.
BESPOKE_TABLES: dict[str, str] = {
    "title_meta": "loaded per source into title_meta.payload, then resolved per field onto title",
    "dna_tag": "loaded with its evidence by importer.dna.load_tags (§4.1 rule 1)",
    "dna_evidence": "loaded with dna_tag; keyed (title_id, term) upstream",
    "dna_projected": "loaded by importer.dna.load_projected — a separate statement, never a union",
}

SKIPPED_TABLES: dict[str, str] = {
    # Not "no surface reads one": `features.py` counts company rows into the thin-title signal.
    # The corpus keys this table (title_id, source, company) and 0003_content.sql keys it
    # (title_id, company, role), so 8,594 of the shipped rows collide the moment `source` is
    # dropped. Its two siblings took the other route on 2026-09-02 and adopted the corpus's key;
    # this one has no such migration yet, so it is named here rather than mapped.
    "title_company": "the corpus keys it per source and this app does not; loading it needs a "
                     "cross-source dedupe that does not exist yet",
    "imdb_ratings": "pre-selection signal for the corpus's own crawl; the app shows IMDb's "
                    "number from platform_rating, which §4.1 rule 3 keeps display-only",
    "dna_annotation": "curator working notes; no app surface reads one",
    "dna_term_signal": "vocabulary-building telemetry, superseded by the shipped vocabulary",
    "dna_exclusion": "the corpus's own extraction exclusions, applied before export",
    "dna_projection_run": "provenance of the corpus's wholesale projection runs",
    "sqlite_sequence": "SQLite bookkeeping, not data",
}


def unaccounted_tables(db: sqlite3.Connection) -> list[str]:
    """Tables the bundle ships that nothing above claims.

    §10 requires a migration report with "counts per table". A table nobody maps produced no
    line at all, which is how three shipped tables were dropped without anyone noticing for
    five milestones.
    """
    shipped = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    claimed = {m.source for m in MAPPINGS} | set(BESPOKE_TABLES) | set(SKIPPED_TABLES)
    return sorted(t for t in shipped - claimed if not t.startswith("sqlite_"))


def _sqlite_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def _rows(db: sqlite3.Connection, tmap: TableMap) -> Iterator[tuple]:
    """Stream one bundle table as tuples in `pg_columns` order.

    Every mapped column is selected by name. There is no `NULL` substitution for a column the
    bundle lacks any more: `load_content` refuses that bundle before it gets here, so a
    fallback would only be a second, silent implementation of a rule this app no longer has.

    Two coercions happen here and nowhere else:

    * rule 6 — NULL in a PK component becomes ``''``.
    * **SQLite has no boolean or date type.** `sqlite3` returns plain ints where Postgres wants
      `boolean` and plain strings where it wants `timestamptz`, and asyncpg's *binary* COPY
      encoder rejects both outright (``TypeError: a boolean is required``). Without these casts
      the very first import dies on `title`, which is the first and required mapping.
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
            # Transforms run first: they turn the corpus's representation into this app's, and
            # the coercions below are about this app's types.
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


async def _resolve_ml_links(conn: asyncpg.Connection, report: ImportReport) -> None:
    """Join the MovieLens link table to `title` after the load.

    `ml_link.title_id` is this app's column, not the corpus's: the bundle exports the table as
    MovieLens publishes it. §4.3's genome block reads through this join, so leaving the column
    NULL would empty a block without emptying anything the read path can notice — the failure
    M4.5 exists for. `imdb_id` and nothing else: `tmdb_id` is legitimately duplicated across
    titles (§4.1 rule 6, the movie/series pair), so a tmdb join would attach a genome vector to
    an arbitrary one of them.
    """
    await conn.execute(
        """
        UPDATE ml_link l SET title_id = t.id
          FROM title t
         WHERE l.title_id IS NULL AND l.imdb_id IS NOT NULL AND t.imdb_id = l.imdb_id
        """
    )
    linked = await conn.fetchval("SELECT count(*) FROM ml_link WHERE title_id IS NOT NULL")
    total = await conn.fetchval("SELECT count(*) FROM ml_link")
    if total:
        report.note(
            "ml-link",
            f"{linked} of {total} MovieLens links resolved to a title by imdb_id; the rest "
            "contribute no genome block",
            linked=linked, total=total,
        )


def _account_for_shipped_tables(db: sqlite3.Connection, report: ImportReport) -> bool:
    """§10's "counts per table", for the tables the *bundle* ships rather than the ones mapped.

    Mapped tables get their count as they load and bespoke ones from their own loaders. What is
    left is the two cases the report could not previously express: a table this app declines,
    which is named here with its reason, and a table nobody claims, which fails the import.
    Silence is how `title_meta`'s 46,318 rows went missing for five milestones — a report that
    cannot say "I did not load this" cannot be audited.
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
    conn: asyncpg.Connection,
    db: sqlite3.Connection,
    report: ImportReport,
    *,
    bundle_root: Path | None = None,
) -> ImportReport:
    """Load the bundle's content tables into Postgres inside the caller's transaction.

    Idempotent by construction: §10 calls a re-import "a planned admin event with a diff
    report", so a second import of the same or a newer bundle must succeed rather than collide
    on primary keys.

    `bundle_root` is where the per-field source order travels (`BUNDLE.json`); without it the
    title card resolves by the corpus's own order and the report says so.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not _account_for_shipped_tables(db, report):
        return report
    priority = meta.source_priority(bundle_root, report)

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

        # The shape note runs one way only. An *unmapped bundle* column is a report line,
        # because the corpus is the authority on its own column names. A column this app's
        # mapping names and the bundle does not have is the opposite claim — this app asserting
        # a name upstream never had — and selecting NULL for it loads a whole column of nothing
        # under a heading that says it worked. `title.name` did exactly that against
        # `primary_title` until M4.5.
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

    # Three passes, because FK order runs one way for deletes and the other for inserts:
    #   1. clear every derived table, children first,
    #   2. upsert `title` — never delete it, or ON DELETE CASCADE takes the Ledger with it,
    #   3. refill the derived tables in declaration order, parents before children.
    for tmap in reversed(usable):
        if tmap.target != _TITLE_TARGET:
            await _clear(conn, tmap.target)

    for tmap in usable:
        if tmap.target == _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _upsert_titles(conn, tmap, db)

    for tmap in usable:
        if tmap.target != _TITLE_TARGET:
            report.table_counts[f"loaded:{tmap.target}"] = await _copy(conn, tmap, db)

    await _resolve_ml_links(conn, report)

    # §4.1's per-source meta rows, then §6.0's card resolved out of them per field. After the
    # derived tables, because the trailer key is read from `title_video`.
    await meta.load_title_meta(conn, db, report)
    await meta.resolve_title_fields(conn, priority, report)

    # rule: is_owned is re-derived from Jellyfin, never trusted stale (§7.2). Whatever the
    # bundle claimed, mark it as unverified so the first Jellyfin sync owns the truth.
    await conn.execute("UPDATE title SET owned_checked_at = NULL")
    report.note(
        "owned-flag",
        "is_owned imported but marked unverified — §7.2 re-derives it from Jellyfin, "
        "the corpus flag goes stale the moment the library changes",
    )
    return report
