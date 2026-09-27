"""§8 stage 3's derive: one title, the documents already in the raw store, and the rows they become.

Deletes are scoped per table to this run's sources and, for `award`/`review`, to `origin = 'derived'`
(decisions 375, 420), so a partial crawl never erases rows it cannot rebuild. Never fetches.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from spielplan.acquire import rawstore
from spielplan.derive import ids, ledgers, parse, reviews
from spielplan.importer import meta
from spielplan.importer.load import MAPPINGS, TableMap, _timestamp
from spielplan.sources._ids import set_ids

# Shared with `importer/reviews.py`, so there is one answer to what an unparseable date becomes.

# `(store source, kind head)` -> the source this document's REVIEWS are filed under. Not derivable
# from `parse.parsed_sources()`: page and review kinds split differently.
REVIEW_DOCUMENTS: Mapping[tuple[str, str], str] = {
    ("tmdb", "movie_detail"): "tmdb",
    ("tmdb", "tv_detail"): "tmdb",
    ("tmdb", "reviews"): "tmdb",
    ("trakt", "comments"): "trakt",
    ("metacritic", "reviews"): "metacritic",
    ("wikipedia", "article"): "wikipedia",
}

# The sources reached by a guessed slug, and whether a disjoint cast alone refuses the page.
SCRAPED_PAGES: Mapping[str, tuple[str, bool]] = {
    "metacritic": ("metacritic", True),
    "rottentomatoes": ("rt", False),
}

# Only `page:main` carries the cast and year that settle identity.
SCRAPED_EVIDENCE = "page"

# Sources that emit `award` rows; `award` has no source column, so its delete is guarded by this.
# A source added to the parser but not here would lose its awards (tested).
AWARD_SOURCES = frozenset({"omdb", "wikidata"})

# Decision 334's one required source, as the two kinds it arrives under.
REQUIRED_DOCUMENTS = frozenset({"tmdb:movie_detail", "tmdb:tv_detail"})

# The only `origin` this module writes to `award` and `review_store.review`, and the only one it deletes.
DERIVED_ORIGIN = "derived"

# target table -> the column carrying a row's provenance, which the delete scopes on (from
# `importer/load.py`'s mapping). `award` has none.
SCOPE_COLUMN: Mapping[str, str | None] = {
    "title_alias": "kind",
    "title_genre": "source",
    "title_keyword": "source",
    "title_language": "source",
    "title_country": "source",
    "title_company": "source",
    "title_video": "source",
    "credit": "source",
    "award": None,
    "display.platform_rating": "platform",
}

# `derive/parse.TABLES` names the table it emits for; one of them lives in another schema.
TARGET_OF: Mapping[str, str] = {"platform_rating": "display.platform_rating"}

# Natural-key tables where two agreeing sources collide (TMDB repeats languages and countries).
IGNORE_DUPLICATES = frozenset({"title_alias", "title_genre", "title_keyword", "title_language",
                               "title_country", "title_company", "title_video"})

# `display.platform_rating` is last-write-wins; documents are read in a deterministic order.
_RATING_CONFLICT = (
    " ON CONFLICT (title_id, platform, metric) DO UPDATE SET"
    " score = EXCLUDED.score, scale = EXCLUDED.scale, votes = EXCLUDED.votes"
)

# `title_meta` stays one row per source; the conflict clause only covers a kind flip upstream.
_META_CONFLICT = (
    " ON CONFLICT (title_id, source) DO UPDATE SET"
    " payload = EXCLUDED.payload, fetched_at = EXCLUDED.fetched_at"
)

# The newest ok document per `(source, kind, page)` among this title's task keys. `page` is in the
# key so Trakt's comment sorts do not collapse. The task join is `acquire/board.py`'s. `d.url` ties
# a scraped review to its page, since page and reviews are resolved independently.
_DOCUMENTS = """
SELECT DISTINCT ON (d.source, d.kind, d.page)
       d.id, d.source, d.kind, d.page, d.fetched_at, d.url
  FROM raw_document d
 WHERE d.ok
   AND d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.source, d.kind, d.page, d.fetched_at DESC, d.id DESC
"""

_REVIEW_COLUMNS = ("title_id", "source", "author", "url", "rating", "published_at",
                   "is_critic", "body")

# Written with COALESCE, not overwritten: `year`/`runtime_min` may be the library's own answer.
_TITLE_FIELDS = ("year", "runtime_min", "original_language")


@dataclass(frozen=True)
class _Document:
    """One raw document, read once and handed to both parsers."""

    source: str
    kind: str
    fetched_at: datetime | None
    content: bytes
    title_label: str | None
    review_label: str | None
    # For scraped sources, the path the identity check ruled on.
    url: str = ""

    @property
    def head(self) -> str:
        """The part of the kind before the colon, which is what chooses a parser."""
        return self.kind.split(":", 1)[0]


@dataclass(frozen=True)
class DeriveReport:
    """What one derive read, refused and wrote. Returned rather than logged; counts and names only."""

    title_id: int
    documents: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    rows: Mapping[str, int] = field(default_factory=dict)
    people: int = 0
    # One count per ledger: an operator must see which ran.
    adjudications: Mapping[str, int] = field(default_factory=dict)
    corrections: Mapping[str, int] = field(default_factory=dict)


def _values(tmap: TableMap, row: Mapping[str, Any], title_id: int) -> tuple:
    """One parsed row as `tmap.pg_columns`-ordered values, through the importer's own coercions.

    Transforms, then the empty-string coalesce, then casts. An omitted column is NULL.
    """
    out: list[Any] = []
    for column in tmap.pg_columns:
        value = title_id if column == "title_id" else row.get(tmap.columns[column])
        if column in tmap.transforms:
            value = tmap.transforms[column](value)
        if value is None and column in tmap.coalesce_empty:
            value = ""
        if column in tmap.bool_columns:
            value = tmap.bool_defaults.get(column) if value is None else bool(value)
        if column in tmap.timestamp_columns:
            value = _timestamp(value)
        out.append(value)
    return tuple(out)


_TARGETS: Mapping[str, TableMap] = {tmap.target: tmap for tmap in MAPPINGS}


async def _replace(
    conn: asyncpg.Connection,
    target: str,
    *,
    title_id: int,
    scope: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    conflict: str = "",
    origin: str = "",
) -> int:
    """Delete this title's rows for `scope`, then insert `rows`. Decision 375, in one place.

    The delete runs even for empty `rows`. The count is read back, since conflict clauses drop rows.
    `origin` is passed only for `award`, and rows the bundle already holds are not re-inserted.
    """
    tmap = _TARGETS[target]
    column = SCOPE_COLUMN[target]
    if column is not None:
        await conn.execute(
            f"DELETE FROM {target} WHERE title_id = $1 AND {column} = ANY($2::text[])",
            title_id, list(scope),
        )
    if not rows:
        return 0
    columns = [*tmap.pg_columns, "origin"] if origin else tmap.pg_columns
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    # Quoted: `credit.character` collides with a type name.
    names = ", ".join(f'"{column}"' for column in columns)
    values = [_values(tmap, row, title_id) for row in rows]
    if origin:
        mapped = ", ".join(f'"{column}"' for column in tmap.pg_columns)
        held = {tuple(row) for row in await conn.fetch(
            f"SELECT {mapped} FROM {target} WHERE title_id = $1 AND origin <> $2",
            title_id, origin,
        )}
        values = [row + (origin,) for row in values if row not in held]
        if not values:
            return 0
    await conn.executemany(
        f"INSERT INTO {target} ({names}) VALUES ({placeholders}){conflict}",
        values,
    )
    if column is None:
        # `award`: the author axis is the scope.
        return await conn.fetchval(
            f"SELECT count(*) FROM {target} WHERE title_id = $1 AND origin = $2",
            title_id, origin,
        )
    return await conn.fetchval(
        f"SELECT count(*) FROM {target} WHERE title_id = $1 AND {column} = ANY($2::text[])",
        title_id, list(scope),
    )


def _refuses(doc: _Document, *, year: int | None, people: set[str]) -> bool:
    """Is this scraped page about a different film? Judged before any of its rows are written.

    Stage 2 stores refused pages as `ok`, so this is not redundant. Compared against this run's cast
    plus the stored one. The slug is not cleared and nothing currently revisits it.
    """
    mode, people_decide = SCRAPED_PAGES[doc.source]
    return not parse.page_belongs_to_title(
        doc.content, year=year, people=people, mode=mode, people_decide=people_decide
    )


def _under(url: str, page_url: str | None) -> bool:
    """Was this document fetched from the accepted page url, or beneath it? False when none was accepted.
    """
    if not page_url or not url:
        return False
    base = page_url if page_url.endswith("/") else page_url + "/"
    return url == page_url or url.startswith(base)


def _cast(result: parse.ParsedTitle) -> set[str]:
    """This document's directors and billed cast as loose-match keys (by role class, as `known_people`).
    """
    return {
        key
        for row in result.table("credit") if row.get("role_class") in ("director", "cast")
        if (key := ids.loose_name((row.get("person") or {}).get("name")))
    }


async def derive_title(
    conn: asyncpg.Connection, title_id: int, *, priority: Sequence[str] | None = None,
    adjudicate: bool = True,
) -> DeriveReport:
    """Re-derive one title from the documents already in the raw store. §8 stage 3.

    One transaction. `adjudicate=False` is only for the metadata walk (decision 522). Documents are read
    in `(source, kind, page)` order and billing order is written into the rows.
    """
    title = await conn.fetchrow("SELECT id, year FROM title WHERE id = $1", title_id)
    if title is None:
        # A missing title is not a title with nothing derived.
        raise LookupError(f"title {title_id} does not exist: there is nothing to derive")

    order = list(priority) if priority is not None else meta.source_priority(None)
    parseable = parse.parsed_sources()

    read: list[_Document] = []
    labels: set[str] = set()
    # `payload ->> 'title_id'` is text, so pass a string.
    for row in await conn.fetch(_DOCUMENTS, str(title_id)):
        head = row["kind"].split(":", 1)[0]
        title_label = parseable.get((row["source"], head))
        review_label = REVIEW_DOCUMENTS.get((row["source"], head))
        if title_label is None and review_label is None:
            # Identifier-only kinds stage 2 already consumed; nothing to derive.
            continue
        read.append(_Document(
            source=row["source"], kind=row["kind"], fetched_at=row["fetched_at"],
            content=await rawstore.read(conn, row["id"]),
            title_label=title_label, review_label=review_label, url=row["url"] or "",
        ))
        labels.update(label for label in (title_label, review_label) if label)

    # Pass one: the non-scraped sources, which supply the cast the scraped pages are judged against.
    people = await ids.known_people(conn, title_id)
    parsed: list[tuple[_Document, parse.ParsedTitle]] = []
    for doc in read:
        if doc.source in SCRAPED_PAGES:
            continue
        result = parse.parse_document(doc.source, doc.kind, doc.content)
        parsed.append((doc, result))
        people |= _cast(result)

    # Pass two: judge each scraped source's `page:main`. A refused source keeps its label in scope, so
    # rows from a wrong page are deleted and not replaced.
    refused: dict[str, str] = {}
    proved: dict[str, str] = {}
    for doc in read:
        if doc.source in SCRAPED_PAGES and doc.head == SCRAPED_EVIDENCE:
            if _refuses(doc, year=title["year"], people=people):
                refused[doc.source] = f"{doc.source}:{doc.kind}: a different title of the same name"
            else:
                proved[doc.source] = doc.url

    review_rows: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    unproved: list[str] = []
    for doc in read:
        if doc.source in refused:
            continue
        # Every other document of a scraped source must hang off the accepted page url, or it is dropped.
        if doc.source in SCRAPED_PAGES and not _under(doc.url, proved.get(doc.source)):
            unproved.append(f"{doc.source}:{doc.kind}: not fetched from the page this title proved")
            continue
        if doc.source in SCRAPED_PAGES and doc.title_label is not None:
            parsed.append((doc, parse.parse_document(doc.source, doc.kind, doc.content)))
        if doc.review_label is None:
            continue
        for review in reviews.parse_document(doc.source, doc.kind, doc.content):
            review_rows.setdefault(review.source, []).append(
                (review.fingerprint(), reviews.review_row(review, title_id))
            )

    async with conn.transaction():
        # The DNA ledger at ingest, over the tags the title already carries (decision 376).
        adjudications = await ledgers.apply_adjudications(conn, title_id) if adjudicate else {}
        written = await _write(conn, title_id, parsed, review_rows, labels, order)
        # LAST: `_write` has just regenerated the credits a correction overrules.
        corrections = await ledgers.apply_corrections(conn, title_id)

    return DeriveReport(
        title_id=title_id,
        documents=tuple(f"{doc.source}:{doc.kind}" for doc in read),
        # Both kinds of refusal, sorted so the report is stable across derives.
        refused=tuple(refused[source] for source in sorted(refused)) + tuple(sorted(unproved)),
        sources=tuple(sorted(labels)),
        rows={table: count for table, count in sorted(written.items()) if count},
        people=written.get("person", 0),
        adjudications=adjudications,
        corrections=corrections,
    )


async def _write(
    conn: asyncpg.Connection,
    title_id: int,
    parsed: Sequence[tuple[_Document, parse.ParsedTitle]],
    review_rows: Mapping[str, list[tuple[str, dict[str, Any]]]],
    labels: set[str],
    order: Sequence[str],
) -> dict[str, int]:
    """Everything the derive writes, inside the caller's one transaction."""
    by_table: dict[str, list[Mapping[str, Any]]] = {target: [] for target in SCOPE_COLUMN}
    meta_rows: dict[str, tuple[Mapping[str, Any], Any]] = {}
    for doc, result in parsed:
        for table in parse.TABLES:
            if table == "title_meta":
                for row in result.table(table):
                    meta_rows[str(row.get("source"))] = (row, doc.fetched_at)
                continue
            by_table[TARGET_OF.get(table, table)].extend(result.table(table))

    written: dict[str, int] = {}
    written["person"] = await _people(conn, title_id, by_table)

    for target, rows in by_table.items():
        if target == "award":
            continue
        scope = sorted(labels | {str(row["source"]) for row in rows if row.get("source")})
        conflict = " ON CONFLICT DO NOTHING" if target in IGNORE_DUPLICATES else ""
        if target == "display.platform_rating":
            conflict = _RATING_CONFLICT
        written[target] = await _replace(
            conn, target, title_id=title_id, scope=scope, rows=rows, conflict=conflict
        )

    if labels & AWARD_SOURCES:
        # `origin`, not `title_id` alone: the bundle's awards cannot be re-crawled (decision 420).
        await conn.execute(
            "DELETE FROM award WHERE title_id = $1 AND origin = $2", title_id, DERIVED_ORIGIN,
        )
        written["award"] = await _replace(
            conn, "award", title_id=title_id, scope=(), rows=by_table["award"],
            origin=DERIVED_ORIGIN,
        )

    written["title_meta"] = await _meta(conn, title_id, meta_rows, labels)
    written["review"] = await _reviews(conn, title_id, review_rows, labels)
    await _resolve(conn, title_id, order)
    return written


async def _people(
    conn: asyncpg.Connection, title_id: int, by_table: dict[str, list[Mapping[str, Any]]]
) -> int:
    """Resolve every credit's human to a `person` row, then put the id on a copy of the row.

    Once per distinct human. Id'd credits resolve first, so name-only credits find them on this title.
    """
    credited = await ids.credited_on(conn, title_id)
    minted: dict[tuple[Any, ...], int] = {}
    person_ids: dict[int, int] = {}
    rows = by_table["credit"]

    def has_id(i: int) -> bool:
        person = rows[i].get("person") or {}
        return bool(person.get("imdb_id") or person.get("tmdb_id"))

    for i in sorted(range(len(rows)), key=lambda i: not has_id(i)):
        row = rows[i]
        person = dict(row.get("person") or {})
        person.setdefault("name", "?")
        role_class = row.get("role_class")
        key = (person.get("name"), person.get("imdb_id"), person.get("tmdb_id"),
               None if has_id(i) else role_class)
        if key not in minted:
            minted[key] = await ids.upsert_person(
                conn, **person, role_class=role_class, credited=credited
            )
            ids.note_credited(credited, person["name"], role_class, minted[key], has_id(i))
        person_ids[i] = minted[key]
    by_table["credit"] = [{**row, "person_id": person_ids[i]} for i, row in enumerate(rows)]
    # Distinct people, not distinct lookups.
    return len(set(person_ids.values()))


async def _meta(
    conn: asyncpg.Connection,
    title_id: int,
    rows: Mapping[str, tuple[Mapping[str, Any], Any]],
    labels: set[str],
) -> int:
    """`title_meta`, one row per source, never collapsed.

    The payload drops the two key columns. `fetched_at` is the document's, keeping the row deterministic.
    """
    await conn.execute(
        "DELETE FROM title_meta WHERE title_id = $1 AND source = ANY($2::text[])",
        title_id, sorted(labels),
    )
    values = [
        (title_id, source, {k: v for k, v in row.items() if k not in ("title_id", "source")},
         fetched_at)
        for source, (row, fetched_at) in sorted(rows.items())
    ]
    if values:
        await conn.executemany(
            "INSERT INTO title_meta (title_id, source, payload, fetched_at)"
            f" VALUES ($1, $2, $3, $4){_META_CONFLICT}",
            values,
        )
    return len(values)


async def _reviews(
    conn: asyncpg.Connection,
    title_id: int,
    by_source: Mapping[str, list[tuple[str, dict[str, Any]]]],
    labels: set[str],
) -> int:
    """`review_store.review`, replaced by source and by author (decision 420).

    Deduplicated within a source by `fingerprint`, and reviews the bundle already holds are skipped,
    so stage 4's `sum(word_count)` never counts one review twice.
    """
    await conn.execute(
        "DELETE FROM review_store.review"
        " WHERE title_id = $1 AND source = ANY($2::text[]) AND origin = $3",
        title_id, sorted(labels), DERIVED_ORIGIN,
    )
    held = {tuple(row) for row in await conn.fetch(
        "SELECT source, coalesce(author, ''), left(body, 400) FROM review_store.review"
        " WHERE title_id = $1 AND origin <> $2",
        title_id, DERIVED_ORIGIN,
    )}
    placeholders = ", ".join(f"${i}" for i in range(1, len(_REVIEW_COLUMNS) + 1))
    total = 0
    for source in sorted(by_source):
        seen: set[str] = set()
        values = []
        for mark, row in by_source[source]:
            if mark in seen:
                continue
            seen.add(mark)
            if (row.get("source"), row.get("author") or "", (row.get("body") or "")[:400]) in held:
                continue
            row["published_at"] = _timestamp(row.get("published_at"))
            values.append(tuple(row.get(column) for column in _REVIEW_COLUMNS))
        if not values:
            continue
        # `origin` as a literal so this writer's rows are findable again.
        await conn.executemany(
            f"INSERT INTO review_store.review ({', '.join(_REVIEW_COLUMNS)}, origin)"
            f" VALUES ({placeholders}, '{DERIVED_ORIGIN}')",
            values,
        )
        total += len(values)
    return total


async def _resolve(conn: asyncpg.Connection, title_id: int, order: Sequence[str]) -> None:
    """The card, the three `title` fields below it, and the two identity columns a parse yielded.

    Scoped to this title: unscoped it would rewrite every card in the install.
    """
    await meta.resolve_title_fields(conn, order, title_ids=[title_id])

    rows = await conn.fetch("SELECT source, payload FROM title_meta WHERE title_id = $1", title_id)
    by_source = {row["source"]: row["payload"] for row in rows}
    if not by_source:
        return

    resolved = {name: meta.best(by_source, name, order) for name in _TITLE_FIELDS}
    await conn.execute(
        "UPDATE title SET year = COALESCE(year, $2), runtime_min = COALESCE(runtime_min, $3),"
        " original_language = COALESCE(original_language, $4) WHERE id = $1",
        title_id, _int(resolved["year"]), _int(resolved["runtime_min"]),
        resolved["original_language"],
    )

    # Fill-never-clobber via `sources/_ids.set_ids` for identifiers no adapter wrote.
    await set_ids(
        conn, title_id,
        wikipedia_title=_extra(by_source.get("wikipedia"), "title"),
        wikidata_id=_extra(by_source.get("wikidata"), "qid"),
    )


def _extra(payload: Any, key: str) -> Any:
    """One value out of a `title_meta` payload's `extra`, which the parsers store as a JSON STRING."""
    blob = (payload or {}).get("extra") if isinstance(payload, Mapping) else None
    if isinstance(blob, Mapping):
        return blob.get(key)
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return parsed.get(key) if isinstance(parsed, Mapping) else None


def _int(value: Any) -> int | None:
    """An int for `title.year` / `runtime_min`, or NULL; sources may answer with floats or nonsense."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
