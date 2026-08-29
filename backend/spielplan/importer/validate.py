"""Bundle validation — every §4.1 landmine rule, checked before anything is written.

Each check names the rule it enforces and the measured fact behind it, because the numbers are
the reason the rule exists. A check that finds the *expected* violation (duplicate tmdb_ids,
shared (title,term) pairs across the two DNA tiers) records a `note`, not a failure: those
duplicates are legitimate and a bundle without them is the suspicious one.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spielplan.importer.report import ImportReport

# §4.1 rule 4: "rating_source.id values are FROZEN — they key fitted_cuts, equating_map, and
# the dataset arrays. Never renumber."
FROZEN_RATING_SOURCE_IDS = {1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31}

# §4.1 rule 7: "Deny-list %_bak% / %_good tables and every stale JSONL in data/export/ —
# export reads live tables only (the JSONLs predate the adjudication repairs)."
DENIED_TABLE_PATTERNS = ("_bak", "_good")

# Measured expectations from the spec. Present as *notes* with the observed value next to the
# expected one, so a re-import diff shows drift instead of hiding it.
EXPECTED = {
    "dna_shared_pairs": 14_181,      # rule 1
    "dna_extracted_titles": 2_016,   # rule 1
    "dna_projected_titles": 11_324,  # rule 1
    "mojibake_review_rows": 73,      # rule 8
}


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def _count(db: sqlite3.Connection, sql: str, *args) -> int:
    row = db.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def validate_content(db: sqlite3.Connection, report: ImportReport) -> ImportReport:
    """Validate the bundle's `content.sqlite` against §4.1."""
    tables = _tables(db)

    # ---- rule 7: deny-list ------------------------------------------------
    denied = sorted(t for t in tables if any(p in t for p in DENIED_TABLE_PATTERNS))
    if denied:
        report.fail(
            "rule7-denylist",
            f"bundle contains {len(denied)} denied table(s) — export must read live tables only",
            tables=denied,
        )
    else:
        report.note("rule7-denylist", "no %_bak% / %_good tables present")

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
    else:
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

        # "dna_evidence ships with the extracted tier — a tag without its quote is unfalsifiable."
        if "dna_evidence" not in tables:
            report.fail("rule1-evidence", "`dna_evidence` is missing; extracted tags without "
                                          "quotes are unfalsifiable")
        else:
            orphans = _count(
                db,
                "SELECT count(*) FROM dna_tag g WHERE NOT EXISTS "
                "(SELECT 1 FROM dna_evidence e WHERE e.dna_tag_id = g.id)",
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
        # `NOT IN` is NULL-blind, and `salience` is NOT NULL in the target schema — a NULL
        # here would sail past validation and die mid-load instead.
        salience_bad = _count(
            db, "SELECT count(*) FROM dna_tag WHERE salience IS NULL OR salience NOT IN (1,2,3)"
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
                "salience/confidence/n_sources imported as weights — no confidence cut is applied "
                "(a 0.5 cut would delete 44% of the extracted tier)",
            )

    # ---- rule 8: UTF-8, mojibake ------------------------------------------
    report.note(
        "rule8-utf8",
        "text imported as UTF-8 with no ASCII cleaning — the corpus legitimately contains CJK, "
        "RTL scripts, ZWSP and emoji",
    )

    # ---- counts -----------------------------------------------------------
    for table in sorted(tables):
        if any(p in table for p in DENIED_TABLE_PATTERNS):
            continue
        try:
            report.table_counts[table] = _count(db, f'SELECT count(*) FROM "{table}"')
        except sqlite3.DatabaseError:  # a view that does not survive a plain count
            continue

    return report


def validate_artifacts(root: Path, report: ImportReport) -> ImportReport:
    """Validate the `artifacts/` side of the bundle against §4.3."""
    from spielplan.models.artifacts import BUNDLE_FILES

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        report.fail("artifacts", "artifacts/manifest.json is missing")
        return report

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report.bundle_version = manifest.get("bundle_version") or report.bundle_version
    report.vocabulary_version = manifest.get("vocabulary_version")

    missing = [name for name, required in BUNDLE_FILES.items() if required and not (root / name).exists()]
    if missing:
        report.fail("artifacts", f"required artifact(s) missing: {', '.join(missing)}", missing=missing)

    optional_missing = [
        name for name, required in BUNDLE_FILES.items() if not required and not (root / name).exists()
    ]
    if optional_missing:
        report.warn(
            "artifacts",
            f"{len(optional_missing)} optional artifact(s) absent — the surfaces that need them "
            "will render their no-artifact state",
            missing=optional_missing,
        )

    # §4.3: the feature contract is the *exhaustive* definition of the tower's input, and §8
    # stage 9 builds vectors "from this file and nothing else". If it is here, it must be sane.
    contract_path = root / "feature_contract.json"
    if contract_path.is_file():
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        blocks = contract.get("blocks", {})
        total = sum(int(v) for v in blocks.values()) if isinstance(blocks, dict) else 0
        if total and total != 6435:
            report.warn(
                "feature-contract",
                f"content blocks sum to {total}, not the documented 6,435 columns",
                observed=total, expected=6435,
            )
        if "text_scale" not in contract:
            report.fail(
                "feature-contract",
                "feature_contract.json has no frozen `text_scale` — the review-text block cannot "
                "be reproduced without it",
            )

    vocab_dir = root / "dna_vocab"
    if not vocab_dir.is_dir():
        report.warn("vocabulary", "no dna_vocab/ in the bundle — the naming layer will be empty")
    elif report.vocabulary_version is None:
        versions = sorted(p.name for p in vocab_dir.iterdir() if p.is_dir())
        report.vocabulary_version = versions[-1] if versions else None

    # `dna_tag`/`dna_projected` carry a FK to `dna_vocabulary(version)`. If the bundle ships DNA
    # rows but no vocabulary directory to create that row from, the load dies mid-transaction on
    # a raw foreign-key violation instead of here, where the operator can read why.
    if vocab_dir.is_dir():
        resolved = report.vocabulary_version or "v1"
        if not (vocab_dir / resolved / "terms.tsv").is_file():
            report.fail(
                "vocabulary",
                f"dna_vocab/{resolved}/terms.tsv is missing — the DNA tables reference this "
                "vocabulary version and cannot be loaded without it",
                version=resolved,
            )

    return report
