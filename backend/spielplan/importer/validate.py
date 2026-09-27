"""Bundle validation — every §4.1 landmine rule, checked before anything is written.

Expected violations (duplicate tmdb_ids, shared pairs across the DNA tiers) are notes, not
failures: a bundle without them is the suspicious one. Every refusal is a report line, never raised.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from spielplan.importer.report import ImportReport

# §4.1 rule 4: "rating_source.id values are FROZEN — they key fitted_cuts, equating_map, and
# the dataset arrays. Never renumber."
FROZEN_RATING_SOURCE_IDS = {1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31}

# §4.1 rule 7, anchored as written: `%_bak%` is a substring and `%_good` ends the name.


def denied_tables(tables: Iterable[str]) -> list[str]:
    """The tables §4.1 rule 7 denies, sorted, so the report can name them and not only count."""
    return sorted(t for t in tables if t.endswith("_good") or "_bak" in t)

# Measured expectations, reported as notes beside the observed value so drift shows.
EXPECTED = {
    "dna_shared_pairs": 14_181,      # rule 1
    "dna_extracted_titles": 2_016,   # rule 1
    "dna_projected_titles": 11_324,  # rule 1
}

# Decision 162: the model bundle's identity column, row-aligned in `backbone.npz`.
IDENTITY_ARRAY = "title_identity"

# `title` reduced to what an identity token can be checked against: (kind, imdb_id, tmdb_id,
# name). One shape for the bundle's spine and the installed one.
Spine = dict[int, tuple[str | None, str | None, int | None, str | None]]

# The version becomes a directory name and an rmtree target under /data/artifacts, so it is
# untrusted input. Checked here, where the operator meets it.
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_version(raw: object) -> str:
    """Return a version string usable as a path segment, or 'unknown'."""
    text = str(raw or "").strip()
    return text if _SAFE_VERSION.match(text) else "unknown"


def _tables(db: sqlite3.Connection) -> set[str]:
    """The bundle's TABLES (`type = 'table'`), the same set `load.py` accounts for."""
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _views(db: sqlite3.Connection) -> set[str]:
    """Views the bundle ships — reported, never counted as tables (see `_tables`)."""
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def _count(db: sqlite3.Connection, sql: str, *args) -> int:
    row = db.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def _schema(db: sqlite3.Connection) -> dict[str, set[str]]:
    return {table: set(_columns(db, table)) for table in _tables(db)}


def _guard(
    schema: dict[str, set[str]], report: ImportReport, rule: str, table: str, *columns: str
) -> bool:
    """True when `table` exists with every one of `columns`; otherwise one report line naming it.

    An unexpected schema costs one line per surprise instead of the whole report.
    """
    if table not in schema:
        report.fail(
            rule,
            f"the bundle has no `{table}` table, so this rule cannot be checked",
            table=table,
        )
        return False
    missing = [c for c in columns if c not in schema[table]]
    if missing:
        report.fail(
            rule,
            f"`{table}` has no column(s) {', '.join(missing)} — this bundle's schema is not the "
            "one this rule is written against, and the rule is not checked",
            table=table, columns=missing, present=sorted(schema[table]),
        )
        return False
    return True


def _read_tsv(
    path: Path, report: ImportReport, rule: str, required_columns: Iterable[str] = ()
) -> list[dict[str, str]] | None:
    """One reader for every curated TSV in the bundle, or one report line saying why not.

    The only opener of a curated TSV in the importer. Returns `None` on a refusal, distinct from `[]`:
    an unreadable ledger must never read as an empty one (decision 247).
    """
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            rows = list(reader)
            header = list(reader.fieldnames or ())
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        report.fail(rule, f"{path.name} cannot be read as UTF-8 TSV: {exc}", file=path.name)
        return None
    missing = [c for c in required_columns if c not in header]
    if missing:
        report.fail(
            rule,
            f"{path.name} has no column(s) {', '.join(missing)}; its header is "
            f"{', '.join(header) or '(empty)'}, so this ledger would be read as nothing",
            file=path.name, columns=missing, header=header,
        )
        return None
    return rows


# BUNDLE.json cannot list its own sha256.
_UNLISTED_EXEMPT = "BUNDLE.json"


def _bundle_manifest(root: Path) -> dict[str, Any] | None:
    """`BUNDLE.json` read without a report line; `_read_bundle_identity` owns its failure."""
    path = root / _UNLISTED_EXEMPT
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256(path: Path) -> str:
    """The file's digest, read in chunks: a bundle must not have to fit in memory to be checked."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_bundle_files(root: Path, report: ImportReport) -> None:
    """Every file BUNDLE.json lists, stat'd and hashed, before a single row is written.

    Called first by `bundle.validate()`: every later rule assumes intact bytes, and a corrupted seed
    is unrepeatable. ~1-2 s for 1 GB. Editing a file after `make_bundle()` breaks the inventory.
    """
    payload = _bundle_manifest(root)
    if payload is None:
        return
    listed = payload.get("files")
    if not isinstance(listed, dict) or not listed:
        report.fail(
            "bundle-integrity",
            "BUNDLE.json records no `files` inventory, so nothing in this bundle can be verified "
            "before it is loaded (the corpus writes one entry per file with its size and sha256)",
        )
        return

    missing: list[str] = []
    mismatched: list[dict[str, Any]] = []
    undeclared: list[str] = []
    verified = 0
    checked_bytes = 0
    for name in sorted(listed):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            # A `files` key becomes a path, so it gets the version token's rule.
            report.fail(
                "bundle-integrity",
                f"BUNDLE.json lists {name!r}, which is not a path inside the bundle",
                file=name,
            )
            continue
        path = root / relative
        if not path.is_file():
            missing.append(name)
            continue
        entry = listed[name]
        declared_bytes = entry.get("bytes") if isinstance(entry, dict) else None
        declared_hash = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(declared_bytes, int) or not (
            isinstance(declared_hash, str) and declared_hash
        ):
            # A size that is not an int or a missing digest is a refusal: the inventory must declare what
            # to check.
            report.fail(
                "bundle-integrity",
                f"BUNDLE.json's inventory entry for {name} is {entry!r}, and an entry is an "
                "object carrying an integer `bytes` and a sha256 string. Without both, nothing "
                "about that file can be checked before it is read",
                file=name,
            )
            undeclared.append(name)
            continue
        try:
            size = path.stat().st_size
            digest = _sha256(path)
        except OSError as exc:
            # A listed file that exists but cannot be read (e.g. root-owned) is a report line, not a 500.
            report.fail(
                "bundle-integrity",
                f"{name} is in the bundle and cannot be read: {type(exc).__name__}: {exc}. Its "
                "size and sha256 could not be checked against BUNDLE.json, so nothing in this "
                "bundle was loaded",
                file=name,
            )
            continue
        if size != declared_bytes or digest != declared_hash:
            mismatched.append({
                "file": name, "bytes": size, "sha256": digest,
                "declared_bytes": declared_bytes, "declared_sha256": declared_hash,
            })
        else:
            verified += 1
            checked_bytes += size

    if missing:
        report.fail(
            "bundle-integrity",
            f"{len(missing)} file(s) BUNDLE.json lists are not in the bundle: "
            f"{', '.join(missing[:5])}",
            missing=missing,
        )

    unlisted = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and p.relative_to(root).as_posix() not in listed
        and p.relative_to(root).as_posix() != _UNLISTED_EXEMPT
    )
    if unlisted:
        # Same rule as listed-but-missing: the tree is not the one the corpus inventoried.
        report.fail(
            "bundle-integrity",
            f"{len(unlisted)} file(s) in the bundle are not in BUNDLE.json's inventory: "
            f"{', '.join(unlisted[:5])}. Only BUNDLE.json itself is exempt, because it cannot "
            "list its own hash",
            unlisted=unlisted,
        )

    if mismatched:
        first = mismatched[0]
        report.fail(
            "bundle-integrity",
            f"{len(mismatched)} file(s) do not match BUNDLE.json. {first['file']} is "
            f"{first['bytes']:,} bytes with sha256 {first['sha256'][:16]}, and the bundle "
            f"declares {first['declared_bytes']} bytes with sha256 "
            f"{str(first['declared_sha256'])[:16]}",
            files=[m["file"] for m in mismatched], first=first,
        )
    elif not missing and not unlisted and not undeclared:
        # Only claim verification when digests were actually checked.
        report.note(
            "bundle-integrity",
            f"{verified} file(s), {checked_bytes:,} bytes: size and sha256 verified against "
            "BUNDLE.json before anything was read",
            files=verified, bytes=checked_bytes,
        )

    _report_export_validations(payload, report)
    for name in ("content.sqlite", "reviews.sqlite"):
        path = root / name
        if path.is_file():
            _quick_check(path, report)


def _report_export_validations(payload: dict[str, Any], report: ImportReport) -> None:
    """The corpus's own export checks (BUNDLE.json `validations`); a failed row is a failure."""
    validations = payload.get("validations")
    if not isinstance(validations, list):
        return
    failed = [v for v in validations if isinstance(v, dict) and not v.get("ok", True)]
    for entry in failed[:10]:
        report.fail(
            "bundle-integrity",
            f"the corpus's own export check {str(entry.get('check'))!r} did not pass: "
            f"{entry.get('detail')}",
            check=str(entry.get("check")), detail=str(entry.get("detail")),
        )
    if len(failed) > 10:
        report.fail(
            "bundle-integrity",
            f"{len(failed) - 10} further export check(s) in BUNDLE.json did not pass",
            checks=[str(v.get("check")) for v in failed[10:]],
        )


def _quick_check(path: Path, report: ImportReport) -> None:
    """`PRAGMA quick_check` on a shipped SQLite file, as a report line rather than an exception.

    `quick_check`, not `integrity_check`: a transport check, ~0.5 s per file.
    """
    db = None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        answer = [str(r[0]) for r in db.execute("PRAGMA quick_check(1)")]
    except sqlite3.DatabaseError as exc:
        report.fail(
            "bundle-integrity",
            f"{path.name} is not a readable SQLite database: {exc}",
            file=path.name,
        )
        return
    finally:
        if db is not None:
            db.close()
    if answer != ["ok"]:
        report.fail(
            "bundle-integrity",
            f"{path.name} fails SQLite's own integrity check: {'; '.join(answer[:3])}",
            file=path.name, detail=answer[:3],
        )


def compare_table_counts(root: Path, report: ImportReport) -> None:
    """BUNDLE.json's own per-table counts against the ones this import measured.

    Called by `validate()` and again after the load, with the same answer; the report dedups. The
    `loaded:<target>` counts are a different fact and are not compared.
    """
    payload = _bundle_manifest(root)
    declared = payload.get("tables") if payload else None
    if not isinstance(declared, dict) or not report.table_counts:
        return
    # A count that is not an int is a report line; enumerated so the rest are still named.
    unreadable = sorted(table for table, n in declared.items() if not isinstance(n, int))
    if unreadable:
        shown = "; ".join(f"{table}: {declared[table]!r}" for table in unreadable[:5])
        report.fail(
            "bundle-integrity",
            f"{len(unreadable)} of BUNDLE.json's `tables` counts are not whole numbers - "
            f"{shown}. A count this app cannot read is a count it cannot compare against the "
            "rows the file actually holds",
            tables=unreadable,
        )
    drift = [
        (table, n, report.table_counts[table])
        for table, n in declared.items()
        if isinstance(n, int)
        and table in report.table_counts
        and n != report.table_counts[table]
    ]
    if drift:
        shown = "; ".join(
            f"{table}: BUNDLE.json says {n:,}, the file holds {found:,}"
            for table, n, found in drift[:5]
        )
        report.fail(
            "bundle-integrity",
            f"{len(drift)} table(s) do not hold the number of rows BUNDLE.json declares - {shown}",
            tables=[table for table, _, _ in drift],
        )
    elif not unreadable:
        agreed = sum(1 for table in declared if table in report.table_counts)
        report.note(
            "bundle-integrity", f"{agreed} table count(s) agree with BUNDLE.json", tables=agreed
        )


def validate_reviews(db: sqlite3.Connection, report: ImportReport) -> ImportReport:
    """`reviews.sqlite`, held to the same rules as `content.sqlite` (rule 7 applies to both)."""
    try:
        schema = _schema(db)
    except sqlite3.DatabaseError as exc:
        report.fail(
            "rule7-denylist", f"reviews.sqlite cannot be read: {exc}", database="reviews.sqlite"
        )
        return report

    denied = denied_tables(schema)
    if denied:
        report.fail(
            "rule7-denylist",
            f"reviews.sqlite contains {len(denied)} denied table(s) ({', '.join(denied)}) - "
            "export must read live tables only",
            tables=denied, database="reviews.sqlite",
        )
    else:
        report.note(
            "rule7-denylist",
            "reviews.sqlite ships no _bak table and no table whose name ends in _good",
            database="reviews.sqlite",
        )

    # The columns the loader SELECTs, checked before COPY.
    from spielplan.importer.reviews import REVIEW_SOURCE

    _guard(schema, report, "rule7-denylist", "review", *sorted(set(REVIEW_SOURCE.values())))
    return report


# The app's foreign keys, mirrored onto the corpus's tables: `(child, column, parent, parent column)`.
# Only tables this app loads (declined tables must not gate a seed; tested).
_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("dna_tag", "title_id", "title", "id"),
    ("dna_projected", "title_id", "title", "id"),
    ("dna_evidence", "title_id", "title", "id"),
    ("credit", "title_id", "title", "id"),
    ("credit", "person_id", "person", "id"),
    ("award", "title_id", "title", "id"),
    ("title_meta", "title_id", "title", "id"),
    ("title_video", "title_id", "title", "id"),
    ("title_alias", "title_id", "title", "id"),
    ("title_genre", "title_id", "title", "id"),
    ("title_keyword", "title_id", "title", "id"),
    ("title_country", "title_id", "title", "id"),
    ("title_company", "title_id", "title", "id"),
)

# Bundle columns that are NOT NULL in Postgres with no rule 6 coalesce. Only tables this app loads.
_NOT_NULL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("award", "award"),                     # 0003: award.body text NOT NULL
    ("title_video", "key"),                 # 0018: PRIMARY KEY (title_id, source, key)
    ("dna_tag", "facet"),                   # 0004: facet text NOT NULL (bespoke loader)
    ("dna_projected", "facet"),             # 0004: facet text NOT NULL (bespoke loader)
)


def _key_expression(tmap: Any, pg_column: str) -> str:
    """One key column as SQL over the bundle's own table, with rule 6's coalesce applied."""
    source = tmap.columns[pg_column]
    return f"""coalesce("{source}", '')""" if pg_column in tmap.coalesce_empty else f'"{source}"'


def _duplicate_groups(db: sqlite3.Connection, tmap: Any) -> tuple[int, list[tuple]]:
    """Rows of one bundle table that share one app key, counted as GROUPS (decision 195)."""
    exprs = ", ".join(_key_expression(tmap, column) for column in tmap.key)
    grouped = f'SELECT {exprs} FROM "{tmap.source}" GROUP BY {exprs} HAVING count(*) > 1'
    groups = _count(db, f"SELECT count(*) FROM ({grouped})")
    if not groups:
        return 0, []
    return groups, [tuple(r) for r in db.execute(f"{grouped} LIMIT 3")]


def _validate_integrity(
    db: sqlite3.Connection, schema: dict[str, set[str]], report: ImportReport
) -> None:
    """Referential integrity, NOT NULLs and this app's own keys — before anything is staged.

    Each finding names the table, column, count and first offending ids. Keys come from
    `load.MAPPINGS`, never a second list.
    """
    from spielplan.importer import load

    for child, column, parent, parent_column in _FOREIGN_KEYS:
        if child not in schema:
            continue        # a table the bundle does not ship is `load.py`'s line, not an orphan
        if not _guard(schema, report, "integrity-orphan", child, column):
            continue
        if not _guard(schema, report, "integrity-orphan", parent, parent_column):
            continue
        where = (
            f'FROM "{child}" c WHERE c."{column}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM "{parent}" p WHERE p."{parent_column}" = c."{column}")'
        )
        orphans = _count(db, f"SELECT count(*) {where}")
        if not orphans:
            continue
        first = [
            r[0] for r in db.execute(f'SELECT DISTINCT c."{column}" {where} ORDER BY 1 LIMIT 5')
        ]
        report.fail(
            "integrity-orphan",
            f"{orphans:,} row(s) in {child}.{column} name a {parent}.{parent_column} this bundle "
            f"does not carry (first: {', '.join(str(i) for i in first)})",
            table=child, column=column, parent=parent, rows=orphans, ids=first,
        )

    for table, column in _NOT_NULL_COLUMNS:
        if table not in schema:
            continue
        if not _guard(schema, report, "integrity-null", table, column):
            continue
        nulls = _count(db, f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL')
        if nulls:
            report.fail(
                "integrity-null",
                f"{nulls:,} row(s) in {table}.{column} are NULL, and the column this app loads "
                "them into is NOT NULL with no rule 6 coalesce standing behind it",
                table=table, column=column, rows=nulls,
            )

    for tmap in load.MAPPINGS:
        if not tmap.key or tmap.source not in schema:
            continue
        sources = [tmap.columns[column] for column in tmap.key]
        if not _guard(schema, report, "integrity-duplicate", tmap.source, *sources):
            continue
        groups, first = _duplicate_groups(db, tmap)
        if groups:
            report.fail(
                "integrity-duplicate",
                f"{groups:,} group(s) of rows in {tmap.source} share one {tmap.target} key "
                f"({', '.join(tmap.key)}); the first is {first[0]}. A COPY into that primary key "
                "rolls the whole import back",
                table=tmap.source, target=tmap.target, key=list(tmap.key), groups=groups,
                first=[str(key) for key in first],
            )


def validate_content(db: sqlite3.Connection, report: ImportReport) -> ImportReport:
    """Validate the bundle's `content.sqlite` against §4.1."""
    try:
        schema = _schema(db)
    except sqlite3.DatabaseError as exc:
        # The first query: a truncated file must be a report line.
        report.fail("bundle", f"content.sqlite cannot be read: {exc}")
        return report
    tables = set(schema)

    # ---- rule 7: deny-list ------------------------------------------------
    # Named per database: this pass reads `content.sqlite` only.
    denied = denied_tables(tables)
    if denied:
        report.fail(
            "rule7-denylist",
            f"content.sqlite contains {len(denied)} denied table(s) ({', '.join(denied)}) - "
            "export must read live tables only",
            tables=denied, database="content.sqlite",
        )
    else:
        report.note(
            "rule7-denylist",
            "content.sqlite ships no _bak table and no table whose name ends in _good",
            database="content.sqlite",
        )

    # ---- title spine ------------------------------------------------------
    if "title" not in tables:
        report.fail("spine", "bundle has no `title` table")
        return report

    cols = set(_columns(db, "title"))
    if "id" not in cols:
        report.fail("spine", "`title` has no `id` column — §4.1: the canonical key is title.id")

    # rule 5: kind non-null, movie/series only.
    if "kind" not in cols:
        report.fail("rule5-kind", "`title.kind` is missing; every ranking surface partitions by it")
    else:
        bad_kind = _count(
            db, "SELECT count(*) FROM title WHERE kind IS NULL OR kind NOT IN ('movie','series')"
        )
        if bad_kind:
            report.fail(
                "rule5-kind",
                f"{bad_kind} title rows have a null or unknown `kind` — "
                "the unpartitioned crowd top-10 is 8/10 TV series, so this is not cosmetic",
                rows=bad_kind,
            )
        else:
            movies = _count(db, "SELECT count(*) FROM title WHERE kind = 'movie'")
            series = _count(db, "SELECT count(*) FROM title WHERE kind = 'series'")
            report.note("rule5-kind", f"kind is clean: {movies:,} movies, {series:,} series",
                        movies=movies, series=series)

    # rule: imdb_id is NULL on ~21% of titles and must never be the join key.
    if "imdb_id" in cols:
        total = _count(db, "SELECT count(*) FROM title")
        null_imdb = _count(db, "SELECT count(*) FROM title WHERE imdb_id IS NULL OR imdb_id = ''")
        pct = (100.0 * null_imdb / total) if total else 0.0
        report.note(
            "imdb-not-a-key",
            f"imdb_id is absent on {null_imdb:,}/{total:,} titles ({pct:.0f}%) — joins use title.id",
            null=null_imdb, total=total, pct=round(pct, 1),
        )

    # rule 6: duplicates on tmdb_id / trakt_id / slugs are LEGITIMATE (mostly movie/series
    # pairs). Their presence is expected; their absence would suggest the exporter deduped.
    for column, expected in (("tmdb_id", 315), ("trakt_id", 171)):
        if column in cols:
            dupes = _count(
                db,
                f"SELECT count(*) FROM (SELECT {column} FROM title "
                f"WHERE {column} IS NOT NULL GROUP BY {column} HAVING count(*) > 1)",
            )
            report.note(
                "rule6-no-unique",
                f"{dupes} duplicate {column} value(s) — expected around {expected}; "
                "no UNIQUE constraint is created on this column",
                observed=dupes, expected=expected,
            )

    # rule 6: NULLable PK components must be coalesced to ''.
    if "title_alias" in tables:
        alias_cols = _columns(db, "title_alias")
        for col in ("region", "language", "kind"):
            if col in alias_cols:
                nulls = _count(db, f"SELECT count(*) FROM title_alias WHERE {col} IS NULL")
                if nulls:
                    report.note(
                        "rule6-coalesce",
                        f"title_alias.{col} is NULL on {nulls:,} rows — coalesced to '' on import",
                        column=col, rows=nulls,
                    )

    # ---- rule 4: frozen rating_source ids ---------------------------------
    if "rating_source" not in tables:
        # §10 lists rating_source as "mandatory always".
        report.fail("rule4-frozen-ids", "`rating_source` is missing — it is mandatory in every bundle")
    elif _guard(schema, report, "rule4-frozen-ids", "rating_source", "id"):
        ids = {int(r[0]) for r in db.execute("SELECT id FROM rating_source")}
        stray = sorted(ids - FROZEN_RATING_SOURCE_IDS)
        missing = sorted(FROZEN_RATING_SOURCE_IDS - ids)
        if stray:
            report.fail(
                "rule4-frozen-ids",
                f"rating_source contains non-frozen id(s) {stray} — these ids key fitted_cuts, "
                "equating_map and the dataset arrays and must never be renumbered",
                stray=stray,
            )
        if missing:
            report.warn(
                "rule4-frozen-ids",
                f"frozen rating_source id(s) {missing} are absent from this bundle",
                missing=missing,
            )
        if not stray and not missing:
            report.note("rule4-frozen-ids", "all 11 frozen rating_source ids present, none added")

    # ---- rule 1: the two DNA tiers stay separate --------------------------
    have_tag = "dna_tag" in tables
    have_proj = "dna_projected" in tables
    if not have_tag or not have_proj:
        report.fail(
            "rule1-two-tiers",
            "bundle must ship dna_tag AND dna_projected as separate tables "
            f"(dna_tag={'yes' if have_tag else 'no'}, dna_projected={'yes' if have_proj else 'no'})",
        )
    else:
        # Both guards run first, so a broken second tier is still reported.
        tiers_ok = _guard(schema, report, "rule1-two-tiers", "dna_tag", "title_id", "term")
        tiers_ok &= _guard(schema, report, "rule1-two-tiers", "dna_projected", "title_id", "term")
        if tiers_ok:
            extracted_titles = _count(db, "SELECT count(DISTINCT title_id) FROM dna_tag")
            projected_titles = _count(db, "SELECT count(DISTINCT title_id) FROM dna_projected")
            shared = _count(
                db,
                "SELECT count(*) FROM (SELECT DISTINCT title_id, term FROM dna_tag "
                "INTERSECT SELECT DISTINCT title_id, term FROM dna_projected)",
            )
            report.note(
                "rule1-two-tiers",
                f"{shared:,} (title,term) pairs exist in both tiers and stay distinguishable "
                f"(expected ~{EXPECTED['dna_shared_pairs']:,})",
                shared=shared, expected=EXPECTED["dna_shared_pairs"],
                extracted_titles=extracted_titles, projected_titles=projected_titles,
            )

        # The corpus's extraction label and the term prefix disagree on most rows; a note counting the
        # rewrite `app_facet` makes. `&=`, not `and`, so both tiers are examined.
        facets_ok = _guard(schema, report, "rule1-two-tiers", "dna_tag", "facet", "term")
        facets_ok &= _guard(schema, report, "rule1-two-tiers", "dna_projected", "facet", "term")
        if facets_ok:
            relabelled = {
                table: _count(
                    db,
                    f"SELECT count(*) FROM {table} WHERE instr(term, '.') > 0 "
                    "AND facet <> substr(term, 1, instr(term, '.') - 1)",
                )
                for table in ("dna_tag", "dna_projected")
            }
            report.note(
                "rule1-two-tiers",
                f"{relabelled['dna_tag']:,} dna_tag and {relabelled['dna_projected']:,} "
                "dna_projected row(s) ship the extraction label rather than the term's own "
                "facet id; the vocabulary facet is imported and the label is not stored",
                dna_tag=relabelled["dna_tag"], dna_projected=relabelled["dna_projected"],
            )

        # "dna_evidence ships with the extracted tier — a tag without its quote is unfalsifiable."
        if "dna_evidence" not in tables:
            report.fail("rule1-evidence", "`dna_evidence` is missing; extracted tags without "
                                          "quotes are unfalsifiable")
        elif tiers_ok and _guard(
            schema, report, "rule1-evidence", "dna_evidence", "title_id", "term"
        ):
            # The link is `(title_id, term)`: `dna_tag` has no surrogate key.
            orphans = _count(
                db,
                "SELECT count(*) FROM (SELECT title_id, term FROM dna_tag "
                "EXCEPT SELECT title_id, term FROM dna_evidence)",
            )
            if orphans:
                report.fail(
                    "rule1-evidence",
                    f"{orphans:,} extracted tag(s) carry no evidence quote",
                    rows=orphans,
                )
            else:
                report.note("rule1-evidence", "every extracted tag carries at least one quote")

        # rule 2 sanity: weights must be present and in range — but never used as a filter.
        # `NOT IN` is NULL-blind and `salience` is NOT NULL in the target.
        if _guard(schema, report, "rule2-weights", "dna_tag", "salience"):
            salience_bad = _count(
                db,
                "SELECT count(*) FROM dna_tag WHERE salience IS NULL OR salience NOT IN (1,2,3)",
            )
            if salience_bad:
                report.fail(
                    "rule2-weights",
                    f"{salience_bad} dna_tag row(s) have salience outside {{1,2,3}} "
                    "(§8 stage 7 trust boundary)",
                    rows=salience_bad,
                )
            else:
                report.note(
                    "rule2-weights",
                    "salience/confidence/n_sources imported as weights — no confidence cut is "
                    "applied (a 0.5 cut would delete 44% of the extracted tier)",
                )

    # ---- rule 8: UTF-8, mojibake ------------------------------------------
    report.note(
        "rule8-utf8",
        "text imported as UTF-8 with no ASCII cleaning — the corpus legitimately contains CJK, "
        "RTL scripts, ZWSP and emoji",
    )

    # What Postgres will accept, before the counts.
    _validate_integrity(db, schema, report)

    # ---- counts -----------------------------------------------------------
    views = _views(db)
    if views:
        # Views are noted, never counted as tables.
        report.note(
            "table-view",
            f"{len(views)} view(s) shipped and not counted as tables: {', '.join(sorted(views))}"
            " - the counts are per table and the loader accounts for tables only",
            views=sorted(views),
        )
    for table in sorted(tables):
        if table in denied:
            continue
        try:
            report.table_counts[table] = _count(db, f'SELECT count(*) FROM "{table}"')
        except sqlite3.DatabaseError:
            # Unreadable pages: `quick_check` names it; here it costs one count.
            continue

    return report


def validate_artifacts(
    root: Path, report: ImportReport, *, spine: Spine | None = None,
    active_coverage: set[int] | None = None,
) -> ImportReport:
    """Validate the `artifacts/` side of the bundle against §4.3.

    `spine` is the installed title rows (a models-only bundle has none); `active_coverage` is the ids
    the active backbone covers (decision 248). `None` means no install to ask.
    """
    from spielplan.models.artifacts import BUNDLE_FILES

    # First, so every later failure names this bundle.
    _read_bundle_identity(root.parent, report)

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        report.fail("artifacts", "artifacts/manifest.json is missing")
        return report
    if _read_json(manifest_path, report, "artifacts") is None:
        # Every check below reads through `ArtifactStore`, which parses this file.
        return report

    missing = [name for name, required in BUNDLE_FILES.items() if required and not (root / name).exists()]
    if missing:
        report.fail("artifacts", f"required artifact(s) missing: {', '.join(missing)}", missing=missing)

    # Decision 251's three are failures, so not also warned about.
    optional_missing = [
        name for name, required in BUNDLE_FILES.items()
        if not required and name not in _REPORTED_AS_FAILURES and not (root / name).exists()
    ]
    if optional_missing:
        report.warn(
            "artifacts",
            f"{len(optional_missing)} optional artifact(s) absent — the surfaces that need them "
            "will render their no-artifact state",
            missing=optional_missing,
        )

    # §4.3: the feature contract is the tower's exhaustive input definition; it must be sane.
    from spielplan.placement.contract import ContractError, FeatureContract

    contract_path = root / "feature_contract.json"
    contract: FeatureContract | None = None
    raw = _read_json(contract_path, report, "feature-contract") if contract_path.is_file() else None
    if raw is not None:
        # Widths live in `content_blocks`, a list of {name, size}.
        declared = raw.get("content_blocks")
        # Malformed entries warn here; the real parser below owns the failure.
        blocks = declared if isinstance(declared, list) else []
        sized = [b for b in blocks if isinstance(b, dict) and isinstance(b.get("size"), int)]
        if len(sized) != len(blocks):
            report.warn(
                "feature-contract",
                f"{len(blocks) - len(sized)} of feature_contract.json's {len(blocks)} "
                "`content_blocks` entries carry no whole-number `size`, so the column widths "
                "§4.3 freezes cannot be summed",
                blocks=len(blocks), sized=len(sized),
            )
        total = sum(b["size"] for b in sized)
        if total and total != 6435:
            report.warn(
                "feature-contract",
                f"content blocks sum to {total}, not the documented 6,435 columns",
                observed=total, expected=6435,
            )
        # §4.3 freezes `text_scale` inside `text_block`, not at the top level.
        text_block = raw.get("text_block")
        scale = text_block.get("text_scale") if isinstance(text_block, dict) else None
        if scale is None:
            report.fail(
                "feature-contract",
                "feature_contract.json has no frozen `text_block.text_scale` — the review-text "
                "block cannot be reproduced without it",
            )
        elif not isinstance(scale, (int, float)):
            report.fail(
                "feature-contract",
                f"`text_block.text_scale` is {scale!r}, not a number — §4.3 freezes it as the "
                "scalar every review-text column is multiplied by",
            )
        else:
            report.note("feature-contract", f"review-text block frozen at text_scale {scale}",
                        text_scale=float(scale))

        # Parsed with §8 stage 9's own parser, so its refusal is a report line, not an import crash.
        try:
            contract = FeatureContract.load_path(contract_path)
        except ContractError as exc:
            report.fail("feature-contract", str(exc))
        except (ValueError, AttributeError, TypeError) as exc:
            # Type errors inside the contract are report lines too; none may escape as a traceback.
            report.fail("feature-contract", f"feature_contract.json cannot be parsed: {exc}")

    _validate_seed_list(root, report, spine)
    _validate_model_artifacts(root, report, contract, spine, active_coverage)

    # One derivation of the version, in `importer/vocab.py`.
    from spielplan.importer import vocab

    vocab_dir = root / "dna_vocab"
    # The tree is always read; a declaration decides the answer, never whether to ask.
    declared = report.vocabulary_version
    try:
        derived = vocab.version_of(root)
    except vocab.VocabularyError as exc:
        report.fail("vocabulary", str(exc), versions=list(exc.versions))
        derived = None
    if declared and derived and declared != derived:
        # Declaration and tree disagree: name both (decisions 163, 256).
        report.fail(
            "vocabulary",
            f"BUNDLE.json declares DNA vocabulary {declared!r} and this bundle ships "
            f"dna_vocab/{derived}/ - section 4.3 names the vocabulary by the directory, so this "
            "bundle gives two answers and decision 163's comparison cannot be made against "
            "either; export it with the key and the tree naming one version",
            declared=declared, derived=derived,
        )
    report.vocabulary_version = declared or derived

    # DNA rows with no vocabulary to reference would fail the FK mid-load, so fail here. Tiers are
    # counted apart, never summed (§4.1 rule 1).
    tagged_rows = report.table_counts.get("dna_tag") or 0
    projected_rows = report.table_counts.get("dna_projected") or 0
    resolved = report.vocabulary_version
    version_dir = vocab_dir / resolved if resolved else None
    if version_dir is not None and version_dir.is_dir():
        # The corpus ships `vocab_<version>_all.tsv` plus one TSV per facet.
        if not sorted(version_dir.glob("vocab_*.tsv")):
            report.fail(
                "vocabulary",
                f"dna_vocab/{resolved}/ ships no vocab_*.tsv (the corpus writes "
                f"vocab_{resolved}_all.tsv and one file per facet) - the DNA tables reference "
                "this vocabulary version and cannot be loaded without its terms",
                version=resolved,
            )
    elif tagged_rows or projected_rows:
        report.fail(
            "vocabulary",
            f"this bundle ships {tagged_rows:,} dna_tag row(s) and {projected_rows:,} "
            f"dna_projected row(s) and no dna_vocab/{resolved or '<version>'}/ to name them "
            "from; dna_tag and dna_projected reference a vocabulary version that would have to "
            "be invented, and the load fails on that foreign key mid-transaction rather than here",
            dna_tag=tagged_rows, dna_projected=projected_rows, version=resolved,
        )
    elif resolved:
        # The bundle named a vocabulary and shipped no tree for it (decision 266).
        report.warn(
            "vocabulary",
            f"this bundle declares DNA vocabulary {resolved!r} and carries no "
            f"dna_vocab/{resolved}/ directory, so it ships no vocabulary files at all: a seed "
            "from it leaves the naming layer empty (section 3.1), and a re-import from it leaves "
            "the installed one exactly as it is",
            version=resolved,
        )
    else:
        # A statement about the bundle only: this function has no connection to see the install.
        report.warn(
            "vocabulary",
            "no dna_vocab/ in this bundle and no DNA rows, so it names nothing: a seed from it "
            "leaves the naming layer empty (section 3.1), and a re-import from it leaves the "
            "installed one exactly as it is",
        )

    return report


def validate_hyperparams(store_dir: Path, report: ImportReport) -> ImportReport:
    """§4.3's constants file, read by the app's own reader before §10 stages it.

    A `fail`: an out-of-range constant would otherwise surface as refusals on Rate and Rank after the
    flip, and defaults would change `hp_digest`. Read through `hyperparams.load`, never a second parser.
    """
    from spielplan.ledger import hyperparams
    from spielplan.models.artifacts import ArtifactStore

    constants = store_dir / "ledger_hyperparams.json"
    if not (constants.exists() or constants.is_symlink()):
        # Optional and already warned about by name; absent means not there at all, not unopenable.
        return report
    # Constructed, not `open`ed: a broken manifest is `validate_artifacts`' line, not this file's.
    store = ArtifactStore(version=report.bundle_version or "staged", root=store_dir)
    try:
        hp, _notes = hyperparams.load(store)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Before the `ValueError` clause: `JSONDecodeError` is a `ValueError`.
        report.fail("hyperparams", f"ledger_hyperparams.json is not readable JSON: {exc}")
    except ValueError as exc:
        report.fail(
            "hyperparams",
            f"ledger_hyperparams.json carries a constant the §5.2 fit cannot use: {exc}",
        )
    else:
        # The digest, so a re-import report shows whether the constants changed.
        report.note(
            "hyperparams",
            f"§5.2 constants read from the bundle; fit digest {hp.digest()}",
            hp_digest=hp.digest(), hp_source=hp.source,
        )
    return report


def _read_json(path: Path, report: ImportReport, rule: str) -> dict[str, Any] | None:
    """Parse a bundle JSON file, or report why it cannot be parsed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.fail(rule, f"{path.name} is not readable JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        report.fail(rule, f"{path.name} is a {type(payload).__name__}, not an object")
        return None
    return payload


def _read_bundle_identity(bundle_root: Path, report: ImportReport) -> None:
    """The bundle's own name for itself, from `BUNDLE.json` at the bundle root.

    The vocabulary falls back to the `dna_vocab/<version>/` directory.
    """
    path = bundle_root / "BUNDLE.json"
    if not path.is_file():
        report.fail(
            "bundle-identity",
            "BUNDLE.json is missing from the bundle root — it records `bundle_version`, which "
            "names the artifact directory and stamps every placement, prior and score (§10)",
        )
        return
    payload = _read_json(path, report, "bundle-identity")
    if payload is None:
        return

    version = payload.get("bundle_version")
    if not version:
        report.fail(
            "bundle-identity",
            "BUNDLE.json records no `bundle_version`; an import stamped 'unknown' cannot be "
            "told apart from the next one (§10's migration report), and the artifact directory it "
            "names would be shared by every bundle",
        )
    else:
        # The same token rule the import refuses on.
        safe = safe_version(version)
        if safe == "unknown":
            report.fail(
                "bundle-identity",
                f"BUNDLE.json's `bundle_version` {str(version)!r} is not a usable name for the "
                "artifact directory it becomes: it must be a plain [A-Za-z0-9._-] token, because "
                "it is both a path segment under /data/artifacts and an rmtree target",
                bundle_version=str(version),
            )
        report.bundle_version = safe
    vocabulary = payload.get("vocabulary_version")
    if isinstance(vocabulary, str) and vocabulary:
        # Only a string counts as a declared vocabulary, as in `bundle._vocabulary_version`.
        report.vocabulary_version = vocabulary
    _validate_nullable_pk_columns(payload, report)


def _validate_nullable_pk_columns(payload: dict[str, Any], report: ImportReport) -> None:
    """rule 6's landmine, read from where the corpus declares it (`nullable_pk_columns`).

    Fails only a column this app coalesces whose affinity is not TEXT.
    """
    declared = payload.get("nullable_pk_columns")
    if not isinstance(declared, dict):
        return
    from spielplan.importer import load

    coalesced = {
        (tmap.source, tmap.columns[column])
        for tmap in load.MAPPINGS
        for column in tmap.coalesce_empty
        if column in tmap.columns
    }
    offenders = [
        f"{table}.{entry.get('column')} ({entry.get('affinity') or 'no affinity'})"
        for table, columns in declared.items()
        if isinstance(columns, list)
        for entry in columns
        if isinstance(entry, dict)
        and (table, str(entry.get("column"))) in coalesced
        and str(entry.get("affinity", "")).upper() != "TEXT"
    ]
    if offenders:
        report.fail(
            "rule6-coalesce",
            f"BUNDLE.json declares {len(offenders)} nullable primary-key component(s) this "
            f"importer coalesces to '' whose affinity is not TEXT: {', '.join(offenders)}. An "
            "empty string is not a value those columns can hold, and the COPY rolls the seed back",
            columns=offenders,
        )


def _spine_ids(content_db: Path) -> set[int] | None:
    """Just the ids of the bundle's own spine, for the checks that need membership and no more."""
    if not content_db.is_file():
        return None
    db = sqlite3.connect(f"file:{content_db}?mode=ro", uri=True)
    try:
        return {int(r[0]) for r in db.execute("SELECT id FROM title")}
    except sqlite3.DatabaseError:
        return None                     # an unreadable spine is `validate_content`'s line
    finally:
        db.close()


def _validate_seed_list(root: Path, report: ImportReport, spine: Spine | None) -> None:
    """`seed_list.json`'s shape and its title ids, checked before the transaction.

    A list, or `{"titles": [...]}`, of objects with an integer `title_id`. An unknown id fails against
    the bundle's own spine and is a counted note against an installed one (decision 248).
    """
    path = root / "seed_list.json"
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.fail("seed-list", f"seed_list.json is not readable JSON: {exc}")
        return
    entries = payload.get("titles") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        report.fail(
            "seed-list",
            f"seed_list.json is a {type(payload).__name__}, not a list of titles or an object "
            "with a `titles` list; the onboarding list (section 4.3) is what the first Rate "
            "session reads",
        )
        return

    ids: list[int] = []
    malformed = 0
    for entry in entries:
        try:
            ids.append(int(entry["title_id"]))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    if malformed:
        report.fail(
            "seed-list",
            f"{malformed} of {len(entries)} seed_list.json entry(ies) carry no integer "
            "`title_id`, and the loader writes that value into a NOT NULL integer column",
            rows=malformed, entries=len(entries),
        )

    # A non-numeric `year` gives a NULL decade in the loader; noted here before commit.
    unreadable_years = [
        entry.get("title_id") for entry in entries
        if isinstance(entry, dict) and entry.get("year") is not None
        and not (isinstance(entry["year"], int | float) and math.isfinite(entry["year"]))
    ]
    if unreadable_years:
        report.note(
            "seed-list",
            f"{len(unreadable_years)} of {len(entries)} onboarding entry(ies) carry a `year` "
            f"this app cannot read as a number (first: {unreadable_years[:5]}); those titles are "
            "still offered and their decade is left NULL",
            rows=len(unreadable_years), entries=len(entries),
        )

    known = set(spine) if spine is not None else _spine_ids(root.parent / "content.sqlite")
    if known is None or not ids:
        return
    unknown = sorted(set(ids) - known)
    if not unknown:
        report.note(
            "seed-list",
            f"{len(ids)} onboarding title(s), every one of them in the spine",
            rows=len(ids),
        )
    elif spine is not None:
        report.note(
            "seed-list",
            f"{len(unknown)} of {len(ids)} onboarding title(s) name a title this install never "
            f"seeded (first: {unknown[:5]}); the loader skips and counts them (decision 247)",
            rows=len(unknown), title_ids=unknown[:20],
        )
    else:
        report.fail(
            "seed-list",
            f"{len(unknown)} onboarding title(s) name a title this bundle's own spine does not "
            f"carry (first: {unknown[:5]}); seed_list.title_id is a NOT NULL foreign key to "
            "title(id) and the import aborts on it",
            rows=len(unknown), title_ids=unknown[:20],
        )


# Decision 251: these three absent are failures on both bundle kinds; every import's rebuild
# reaches them. `BUNDLE_FILES`' `required` flags answer a different question.
_REPORTED_AS_FAILURES: dict[str, tuple[str, str]] = {
    "feature_contract.json": (
        "feature-contract",
        "`FeatureContract.from_store` runs unconditionally in the rebuild, so an absent contract "
        "is a ContractError 500 on every import rather than a degraded one",
    ),
    "cold_tower.pt": (
        "cold-tower",
        "the rebuild's `load_tower` sits inside `if ids:`, which on a real import is never empty "
        "- scope 'reimport' adds every acquired title - so an absent tower is a 500 there",
    ),
    "backbone.npz": (
        "backbone",
        "the section 5.2 refit composes its basis through `backbone.load_for(store)`, so an "
        "absent array fits every board from zero embeddings: e(t)=0, silently, on every title",
    ),
}


def _validate_model_artifacts(
    root: Path, report: ImportReport, contract: Any, spine: Spine | None = None,
    active_coverage: set[int] | None = None,
) -> None:
    """§4.3's model files, checked against each other rather than only for presence.

    A tower whose input width disagrees with the contract never raises: it places the library wrongly.
    """
    import numpy as np

    for name, (rule, why) in _REPORTED_AS_FAILURES.items():
        if not (root / name).is_file():
            report.fail(rule, f"{name} is missing: {why} (decision 251)", file=name)

    backbone = root / "backbone.npz"
    if backbone.is_file():
        try:
            with np.load(backbone, allow_pickle=False) as npz:
                keys = set(npz.files)
                # Without `title_ids`, rows of E would be matched by position.
                if "title_ids" not in keys:
                    report.fail(
                        "backbone",
                        "backbone.npz ships no `title_ids` array, so its rows cannot be matched "
                        "to titles (§4.3 names none; the exporter must add it)",
                        keys=sorted(keys),
                    )
                for name in ("E", "b_i", "item_n"):
                    if name not in keys:
                        report.fail("backbone", f"backbone.npz is missing `{name}` (§4.3)")
                if {"title_ids", "E"} <= keys:
                    ids, e = npz["title_ids"], npz["E"]
                    if e.ndim != 2 or e.shape[0] != ids.shape[0]:
                        report.fail(
                            "backbone",
                            f"E is {e.shape} but there are {ids.shape[0]} title ids — the rows "
                            "and the mapping disagree",
                        )
                    elif e.shape[1] != 64:
                        report.fail(
                            "backbone",
                            f"E is {e.shape[1]}-dimensional; §1 fixes the item space at 64",
                        )
                    if ids.size and not np.all(np.diff(ids.astype("int64")) > 0):
                        report.fail("backbone", "`title_ids` is not strictly increasing")
                    _validate_identity(
                        root.parent, ids, npz, keys, report, spine, active_coverage
                    )
        except Exception as exc:                                   # noqa: BLE001
            report.fail("backbone", f"backbone.npz is unreadable: {exc}")

    text_emb = root / "review_text_emb.npz"
    if text_emb.is_file():
        try:
            with np.load(text_emb, allow_pickle=False) as npz:
                keys = set(npz.files)
                for name in ("title_ids", "emb"):
                    if name not in keys:
                        report.fail(
                            "review-text",
                            f"review_text_emb.npz ships no `{name}` array; §4.3's review-text "
                            "block is columns 0..63 of this embedding, matched to titles by id",
                            keys=sorted(keys),
                        )
                # Required: `covered=False` rows carry noise embeddings that would read as review text.
                if "covered" not in keys:
                    report.fail(
                        "review-text",
                        "review_text_emb.npz ships no `covered` array; the contract's "
                        "`preprocessing.missing_review_text` rule (\"zeros when covered=False\") "
                        "cannot be applied and uncovered rows are read as text",
                        keys=sorted(keys),
                    )
                elif "title_ids" in keys and (
                    npz["covered"].shape[0] != npz["title_ids"].shape[0]
                ):
                    report.fail(
                        "review-text",
                        f"covered has {npz['covered'].shape[0]} entries but there are "
                        f"{npz['title_ids'].shape[0]} title ids — the flags and the mapping "
                        "disagree",
                    )
                if {"title_ids", "emb"} <= keys:
                    ids, emb = npz["title_ids"], npz["emb"]
                    if emb.ndim != 2 or emb.shape[0] != ids.shape[0]:
                        report.fail(
                            "review-text",
                            f"emb is {emb.shape} but there are {ids.shape[0]} title ids — the "
                            "rows and the mapping disagree",
                        )
                    elif contract is not None and emb.shape[1] < contract.text_used:
                        # §4.3 truncates; it never pads.
                        report.fail(
                            "review-text",
                            f"emb has {emb.shape[1]} columns and the contract takes the first "
                            f"{contract.text_used}; §4.3 truncates the embedding and never pads it",
                        )
        except Exception as exc:                                   # noqa: BLE001
            report.fail("review-text", f"review_text_emb.npz is unreadable: {exc}")

    tower_path = root / "cold_tower.pt"
    if not tower_path.is_file():
        return                          # named as a failure by the decision 251 loop above
    if contract is None:
        report.note(
            "cold-tower",
            "not checked: the feature contract above did not parse, and §4.3 makes it the only "
            "statement of this tower's input width",
        )
        return

    # Loaded by §8 stage 9's own loader.
    from spielplan.importer import vocab
    from spielplan.models.artifacts import ArtifactStore
    from spielplan.placement.tower import TowerError, load_tower

    try:
        store = ArtifactStore.open(root, report.bundle_version or "unvalidated")
    except vocab.VocabularyError as exc:
        # This caller has a report, so the two-vocabulary refusal becomes a line, not a 500.
        report.fail("vocabulary", str(exc), versions=list(exc.versions))
        return
    try:
        tower = load_tower(store, contract)
    except TowerError as exc:
        report.fail("cold-tower", str(exc))
    except Exception as exc:                                       # noqa: BLE001
        report.fail("cold-tower", f"cold_tower.pt is unreadable: {exc}")
    else:
        # `tower.notes` qualifies the claim, so it rides in the same line.
        report.note(
            "cold-tower",
            f"cold_tower.pt loads as {tower.arch} v{tower.version}: {tower.input_dim} input "
            f"columns -> {tower.embed_dim}-d, matching the contract"
            + "".join(f"; {note}" for note in tower.notes),
            input_dim=tower.input_dim, embed_dim=tower.embed_dim, arch=tower.arch,
            assumed=list(tower.notes),
        )


def _validate_identity(bundle_root: Path, ids: Any, npz: Any, keys: set[str],
                       report: ImportReport, spine: Spine | None = None,
                       active_coverage: set[int] | None = None) -> None:
    """Decision 162: the identity column, checked against the spine rather than trusted.

    Per row, `imdb:<id>` or `tmdb:<id>:<kind>`, on the axis the token names. Extra rows for titles the
    install lacks are inert; coverage going backwards is the refusal (decision 248).
    """
    if active_coverage:
        dropped = sorted(set(active_coverage) - {int(i) for i in ids.tolist()})
        if dropped:
            report.fail(
                "identity",
                f"{len(dropped):,} installed title(s) the active backbone covers are not covered "
                f"by this one (first: {dropped[:5]}); a coordinate that exists today would stop "
                "existing, which is a merged or dropped corpus row rather than a retrain",
                rows=len(dropped), title_ids=dropped[:20],
            )
        else:
            report.note(
                "identity",
                f"coverage does not go backwards: all {len(active_coverage):,} title(s) the "
                "active backbone covers are covered by this one too (decision 248)",
                rows=len(active_coverage),
            )

    if IDENTITY_ARRAY not in keys:
        # A seed without the vector is checked against its own spine and warned; a model bundle without
        # it fails. The corpus exporter writes it now but has not re-run.
        if not (bundle_root / "content.sqlite").is_file():
            report.fail(
                "identity",
                f"backbone.npz ships no `{IDENTITY_ARRAY}` array — decision 162 requires an "
                "identity column row-aligned to `title_ids` on a models-only bundle, which "
                "carries no spine of its own, so a corpus-side re-identification would be "
                "trusted rather than caught",
                keys=sorted(keys),
            )
            return
        report.warn(
            "identity",
            f"backbone.npz ships no `{IDENTITY_ARRAY}`; checking `title_ids` against this "
            "bundle's own content.sqlite instead, which covers more titles than the vector "
            "would. A models-only re-import must carry the array (decision 162)",
        )
        spine = spine or _spine_identities(bundle_root / "content.sqlite", report)
        if spine is not None:
            missing = [int(i) for i in ids.tolist() if int(i) not in spine][:8]
            if missing:
                report.fail(
                    "identity",
                    "backbone.npz names title ids the bundle's own spine does not have: "
                    f"{missing} — a row of E that no title claims is attributed to nothing",
                )
        return

    identity = npz[IDENTITY_ARRAY]
    if identity.shape[0] != ids.shape[0]:
        report.fail(
            "identity",
            f"`{IDENTITY_ARRAY}` has {identity.shape[0]} entries and `title_ids` has "
            f"{ids.shape[0]} — an identity that is not row-aligned identifies the wrong rows",
        )
        return

    if spine is None:
        spine = _spine_identities(bundle_root / "content.sqlite", report)
    if spine is None:
        return

    absent: list[int] = []
    unparsed: list[str] = []
    mismatched: list[dict[str, Any]] = []
    for title_id, token in zip(ids.tolist(), identity.tolist(), strict=True):
        row = spine.get(int(title_id))
        if row is None:
            absent.append(int(title_id))
            continue
        kind, imdb_id, tmdb_id, name = row
        token = str(token)
        if token.startswith("imdb:"):
            claimed, found = token[5:], imdb_id or ""
        elif token.startswith("tmdb:"):
            _, _, rest = token.partition(":")
            claimed = rest
            found = f"{'' if tmdb_id is None else tmdb_id}:{kind or ''}"
        else:
            unparsed.append(token)
            continue
        if claimed != found:
            mismatched.append(
                {"title_id": int(title_id), "title": name, "bundle": token, "spine": found}
            )

    if absent and not (bundle_root / "content.sqlite").is_file():
        # Decision 248: inert rows on a models-only bundle, counted.
        report.note(
            "identity",
            f"{len(absent):,} backbone row(s) name a corpus title this install never seeded "
            f"(first: {absent[:5]}); those rows are never looked up and the identity check is "
            "made on the rows that match",
            rows=len(absent), title_ids=absent[:20],
        )
    elif absent:
        # On a seed, a row no title in the same export claims.
        report.fail(
            "identity",
            f"{len(absent):,} backbone row(s) name a title the spine does not carry "
            f"(first: {absent[:5]}) — the basis asserts things about films this bundle has no "
            "row for",
            rows=len(absent), title_ids=absent[:20],
        )
    if unparsed:
        report.fail(
            "identity",
            f"{len(unparsed):,} identity token(s) are neither `imdb:<id>` nor "
            f"`tmdb:<id>:<kind>` (first: {unparsed[:3]})",
            rows=len(unparsed),
        )
    if mismatched:
        shown = "; ".join(
            f"{m['title_id']} {m['title']!r}: bundle says {m['bundle']}, spine says {m['spine']}"
            for m in mismatched[:5]
        )
        report.fail(
            "identity",
            f"{len(mismatched):,} backbone row(s) identify a different title than the spine "
            f"does — {shown}",
            rows=len(mismatched), titles=mismatched[:20],
        )
    if not (absent or unparsed or mismatched):
        report.note(
            "identity",
            f"{ids.shape[0]:,} backbone row(s) identify the title the spine says they do",
            rows=int(ids.shape[0]),
        )


def _spine_identities(content_db: Path, report: ImportReport) -> Spine | None:
    """`title` reduced to what an identity token can be checked against."""
    if not content_db.is_file():
        report.note(
            "identity",
            "no content.sqlite beside artifacts/ and no installed spine was supplied — a "
            "models-only bundle is checked against the spine this install already carries "
            "(decision 162), and that needs a database connection this caller did not have",
        )
        return None
    db = sqlite3.connect(f"file:{content_db}?mode=ro", uri=True)
    db.text_factory = str
    try:
        schema = _schema(db)
        if not _guard(schema, report, "identity", "title",
                      "id", "kind", "imdb_id", "tmdb_id", "primary_title"):
            return None
        return {
            int(r[0]): (r[1], r[2], r[3], r[4])
            for r in db.execute(
                "SELECT id, kind, imdb_id, tmdb_id, primary_title FROM title"
            )
        }
    finally:
        db.close()
