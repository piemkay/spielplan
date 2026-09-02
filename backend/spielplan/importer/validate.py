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
from typing import Any

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

# decision 162: "the model bundle carries an identity column row-aligned to its title ids so a
# corpus-side re-identification is caught rather than trusted." It travels in `backbone.npz`,
# beside the ids it qualifies, because a separate file can go missing without the ids noticing.
IDENTITY_ARRAY = "title_identity"

# `title` reduced to what an identity token can be checked against: (kind, imdb_id, tmdb_id,
# name). One shape whether it was read from the bundle's own spine or from the installed one,
# because decision 162 makes the second the normal case and two shapes would drift.
Spine = dict[int, tuple[str | None, str | None, int | None, str | None]]


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


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

    §10 promises "a migration report (counts per table, validation failures, vocabulary
    version)" — a *list*, and a list is only enumerable if the enumeration survives the first
    surprise. Rule 1's evidence check asked for `dna_evidence.dna_tag_id` and `dna_tag.id`,
    neither of which any exported bundle has ever carried, so against the real artifact the
    operator got an `OperationalError` where §10 promises a page and every rule after it went
    unreached. Every query below therefore names its table and its columns here first: a schema
    this app does not expect costs one line per surprise instead of the whole report.
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


def validate_content(db: sqlite3.Connection, report: ImportReport) -> ImportReport:
    """Validate the bundle's `content.sqlite` against §4.1."""
    schema = _schema(db)
    tables = set(schema)

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
        # Both guards run before either result is used: a `and` here would report the first
        # broken tier and leave the second unexamined, which is the enumeration failure this
        # whole pass exists to end.
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

        # "dna_evidence ships with the extracted tier — a tag without its quote is unfalsifiable."
        if "dna_evidence" not in tables:
            report.fail("rule1-evidence", "`dna_evidence` is missing; extracted tags without "
                                          "quotes are unfalsifiable")
        elif tiers_ok and _guard(
            schema, report, "rule1-evidence", "dna_evidence", "title_id", "term"
        ):
            # The link is `(title_id, term)`. The shipped `dna_evidence` is
            # (id, title_id, term, pass_id, src, quote) and `dna_tag` has no surrogate key at
            # all — its primary key IS (title_id, term) — so `e.dna_tag_id = g.id` named two
            # columns that have never existed together in one bundle.
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
        # `NOT IN` is NULL-blind, and `salience` is NOT NULL in the target schema — a NULL
        # here would sail past validation and die mid-load instead.
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

    # ---- counts -----------------------------------------------------------
    for table in sorted(tables):
        if any(p in table for p in DENIED_TABLE_PATTERNS):
            continue
        try:
            report.table_counts[table] = _count(db, f'SELECT count(*) FROM "{table}"')
        except sqlite3.DatabaseError:  # a view that does not survive a plain count
            continue

    return report


def validate_artifacts(
    root: Path, report: ImportReport, *, spine: Spine | None = None
) -> ImportReport:
    """Validate the `artifacts/` side of the bundle against §4.3.

    `spine` is the *installed* title rows, supplied by the caller that has a connection. Under
    decision 162 a models-only bundle carries no `content.sqlite`, so the identity column below
    has nothing in the bundle to be checked against and the only spine that exists is this one.
    """
    from spielplan.models.artifacts import BUNDLE_FILES

    # Read before anything else: every later failure is reported under this bundle's name, and
    # §10's re-import diff is only a diff if the two reports name different versions.
    _read_bundle_identity(root.parent, report)

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        report.fail("artifacts", "artifacts/manifest.json is missing")
        return report
    if _read_json(manifest_path, report, "artifacts") is None:
        # Every check below reads the bundle through `ArtifactStore`, which parses this file on
        # open. Reported here rather than raised out of the third caller down (§10).
        return report

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
    from spielplan.placement.contract import ContractError, FeatureContract

    contract_path = root / "feature_contract.json"
    contract: FeatureContract | None = None
    raw = _read_json(contract_path, report, "feature-contract") if contract_path.is_file() else None
    if raw is not None:
        # `content_blocks` is a list of {name, size} and the widths live there; the app read a
        # `blocks` dict, which no shipped contract has, so this drift check summed nothing and
        # passed on every bundle.
        declared = raw.get("content_blocks")
        total = sum(int(b.get("size", 0)) for b in declared) if isinstance(declared, list) else 0
        if total and total != 6435:
            report.warn(
                "feature-contract",
                f"content blocks sum to {total}, not the documented 6,435 columns",
                observed=total, expected=6435,
            )
        # §4.3 freezes `text_scale` at export time, and the corpus writes it INSIDE `text_block`
        # beside the truncation it belongs to. Read at the top level it was absent from every
        # real bundle, so the one number §4.3 calls frozen was reported missing and then
        # defaulted downstream — which moves every coordinate a little, and nothing raises.
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

        # Parse it with the SAME parser §8 stage 9 uses, and report its refusal as a validation
        # failure. Two readers of one file drift; more to the point, §10 now recomputes the
        # rebuild set during import, so a contract this parser rejects takes the whole import
        # down — and without this the operator gets a stack trace out of a background step
        # instead of a line in the report they are standing in front of.
        try:
            contract = FeatureContract.load_path(contract_path)
        except ContractError as exc:
            report.fail("feature-contract", str(exc))
        except ValueError as exc:
            # A width or a scale of the wrong TYPE reaches the parser as an int()/float()
            # conversion rather than as its own refusal. Still a bundle the app cannot read, so
            # still a report line: §10 has no room for a traceback.
            report.fail("feature-contract", f"feature_contract.json cannot be parsed: {exc}")

    _validate_model_artifacts(root, report, contract, spine)

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
        # The corpus ships one combined `vocab_<version>_all.tsv` plus a per-facet TSV each
        # (`vocab_mood_v1.tsv`, …). `terms.tsv` was this app's own name for a file no bundle has
        # ever contained, so this check failed on every real bundle and on no broken one.
        version_dir = vocab_dir / resolved
        terms = sorted(version_dir.glob("vocab_*.tsv")) if version_dir.is_dir() else []
        if not terms:
            report.fail(
                "vocabulary",
                f"dna_vocab/{resolved}/ ships no vocab_*.tsv (the corpus writes "
                f"vocab_{resolved}_all.tsv and one file per facet) — the DNA tables reference "
                "this vocabulary version and cannot be loaded without its terms",
                version=resolved,
            )

    return report


def _read_json(path: Path, report: ImportReport, rule: str) -> dict[str, Any] | None:
    """Parse a bundle JSON file, or report why it cannot be parsed.

    §10's report is what the operator is standing in front of; a `JSONDecodeError` out of a
    validation pass is the same defect as the OperationalError above, one file type over.
    """
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
    """The bundle's own name for itself, read from where the bundle records it.

    §10's report opens with the bundle and vocabulary versions, and both live in `BUNDLE.json`
    at the bundle ROOT — beside `artifacts/`, not inside it. `artifacts/manifest.json` carries
    the fitted 3-class cut-points (§4.3) and no identity at all, so reading the version from
    there named every bundle the corpus has ever built "unknown": no artifact directory an
    operator can recognise, and no version to diff a re-import against (§10: "never a silent
    sync"). The vocabulary version falls back to the `dna_vocab/<version>/` directory the bundle
    ships, which is where a bundle that predates the field records it.
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
            "told apart from the next one (§10's diff report), and the artifact directory it "
            "names would be shared by every bundle",
        )
    else:
        report.bundle_version = str(version)
    vocabulary = payload.get("vocabulary_version")
    if vocabulary:
        report.vocabulary_version = str(vocabulary)


def _validate_model_artifacts(
    root: Path, report: ImportReport, contract: Any, spine: Spine | None = None
) -> None:
    """§4.3's model files, checked against each other rather than only for presence.

    §10's sequence puts validation before the flip precisely so a bad bundle never becomes the
    active one. Without this, the failures below all surface later and somewhere else: a Backbone
    with no id array raises on the first fold-in, and a tower whose input width disagrees
    with the contract does not raise at all — it broadcasts a short vector into a wide layer and
    places the whole library at plausible, wrong coordinates. That one is the reason this
    function exists; it is silent by construction everywhere except here.

    Every array is named the way the corpus names it. The app demanded `title_id` where both
    `backbone.npz` and `review_text_emb.npz` ship `title_ids`, so on a real bundle the id vector
    was reported absent while it was sitting in the file.
    """
    import numpy as np

    backbone = root / "backbone.npz"
    if backbone.is_file():
        try:
            with np.load(backbone, allow_pickle=False) as npz:
                keys = set(npz.files)
                # §4.3 names E, E_full, b_i, μ and item_n — and no id mapping, which is the gap.
                # E is a matrix of rows with no stated correspondence to `title.id`, so without
                # `title_ids` the basis is unusable: every row would be matched by position, and
                # a wrong index is a plausible number for the wrong film.
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
                    _validate_identity(root.parent, ids, npz, keys, report, spine)
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
                # `covered` is the third required array, not an optional extra: the feature
                # contract's own `preprocessing.missing_review_text` is "zeros when
                # covered=False", and the shipped bundle sets it False on 6,010 of 14,397 rows
                # whose `emb` is float noise around 1e-16. Without the flag those rows read as
                # review text, so the review-text block is *present* for 42% of titles that
                # have none — §5.3's thin badge stays off and §8 stage 2 never parks the
                # acquisition job that is the only thing which can fill it. Silent by
                # construction everywhere except here.
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
                        # §4.3 truncates; it never pads. A narrower embedding than the contract
                        # truncates to is a text block the app cannot build at the declared
                        # width, and the tower is fed that width or nothing.
                        report.fail(
                            "review-text",
                            f"emb has {emb.shape[1]} columns and the contract takes the first "
                            f"{contract.text_used}; §4.3 truncates the embedding and never pads it",
                        )
        except Exception as exc:                                   # noqa: BLE001
            report.fail("review-text", f"review_text_emb.npz is unreadable: {exc}")

    tower_path = root / "cold_tower.pt"
    if not tower_path.is_file():
        return
    if contract is None:
        report.note(
            "cold-tower",
            "not checked: the feature contract above did not parse, and §4.3 makes it the only "
            "statement of this tower's input width",
        )
        return

    # Loaded by the SAME loader §8 stage 9 uses, for the reason the contract is parsed by §8
    # stage 9's parser: two readers of one file drift. The corpus writes
    # `torch.save(model.state_dict())` — a bare mapping with no `version`, `arch` or `input_dim`
    # — so a validator hand-reading those keys reported every real bundle as version None while
    # the loader that actually has to build the module was never asked.
    from spielplan.models.artifacts import ArtifactStore
    from spielplan.placement.tower import TowerError, load_tower

    store = ArtifactStore.open(root, report.bundle_version or "unvalidated")
    try:
        tower = load_tower(store, contract)
    except TowerError as exc:
        report.fail("cold-tower", str(exc))
    except Exception as exc:                                       # noqa: BLE001
        report.fail("cold-tower", f"cold_tower.pt is unreadable: {exc}")
    else:
        report.note(
            "cold-tower",
            f"cold_tower.pt loads as {tower.arch} v{tower.version}: {tower.input_dim} input "
            f"columns -> {tower.embed_dim}-d, matching the contract",
            input_dim=tower.input_dim, embed_dim=tower.embed_dim, arch=tower.arch,
        )


def _validate_identity(bundle_root: Path, ids: Any, npz: Any, keys: set[str],
                       report: ImportReport, spine: Spine | None = None) -> None:
    """decision 162: the identity column, checked against the spine rather than trusted.

    Range partitioning stops two minters colliding; it cannot see the corpus *merging* two
    titles, which changes what an id MEANS without changing the id. `scoring/backbone.py`
    records that a wrong row "produces plausible numbers for the wrong films", and the
    strictly-increasing check above cannot see a merge — after one the ids still ascend.

    Measured on the shipped bundle: 2,139 of 14,397 backbone titles carry no `imdb_id`, and none
    carry neither `imdb_id` nor `tmdb_id`. So the identity is decided per ROW, not once for the
    vector: `imdb:<imdb_id>` where the exporter had one, `tmdb:<tmdb_id>:<kind>` where it did
    not — §4.1's "imdb_id … must never be the join key" is about joining, and this is not a
    join; it is the assertion that row r of E is the film the spine calls `title_ids[r]`.

    The check is made on the axis the token names and only that axis. A spine row that has since
    GAINED an imdb_id is not a re-identification — §8 stage 2's enrichment does exactly that —
    while a token naming an id the spine disagrees with is one, and fails naming the title.

    `spine` is the installed one when the caller had a connection. Under decision 162 a
    models-only bundle is the only kind that will ever arrive again and it carries no spine of
    its own, so reading one out of `content.sqlite` alone made this check skip exactly the case
    it exists for: a corpus-side merge reaches an install through a model bundle and nothing
    else.
    """
    if IDENTITY_ARRAY not in keys:
        # A SEED carries `content.sqlite`, which is a better identity source than the vector: it
        # names all 19,071 titles where the vector would name only the 14,397 with a model row,
        # and it is the same fact from the same export. So a seed without the vector is checked
        # against its own spine and warned; a MODEL bundle has no spine of its own, and there
        # the vector is the only thing standing between a corpus-side merge and a silent
        # re-identification — absent, it is a failure.
        #
        # No bundle the corpus has ever built carries the array (`mdc export-bundle` does not
        # write it), so failing a seed on it would make "the real bundle imports" false for the
        # only bundle that exists. The exporter is being taught to emit it for model bundles.
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

    if absent:
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
    """`title` reduced to what an identity token can be checked against.

    Read here rather than passed in because §10 validates the artifacts after the content
    connection is closed, and this is the one check that spans both halves of the bundle.
    """
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
