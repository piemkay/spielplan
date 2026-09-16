"""Bundle validation — every §4.1 landmine rule, checked before anything is written.

Each check names the rule it enforces and the measured fact behind it, because the numbers are
the reason the rule exists. A check that finds the *expected* violation (duplicate tmdb_ids,
shared (title,term) pairs across the two DNA tiers) records a `note`, not a failure: those
duplicates are legitimate and a bundle without them is the suspicious one.
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

# §4.1 rule 7: "Deny-list %_bak% / %_good tables and every stale JSONL in data/export/ —
# export reads live tables only (the JSONLs predate the adjudication repairs)."
#
# The two wildcards are anchored the way the rule writes them: `%_bak%` is a substring and
# `%_good` ENDS the name. Both were matched as substrings, so a future `title_goodness` — not a
# stale copy of anything, just the next plausible table the corpus adds — would have refused the
# whole first boot under a rule about pre-adjudication leftovers. [M4.14 finding 2.22, cs-48]


def denied_tables(tables: Iterable[str]) -> list[str]:
    """The tables §4.1 rule 7 denies, sorted, so the report can name them and not only count."""
    return sorted(t for t in tables if t.endswith("_good") or "_bak" in t)

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

# The version string comes out of the bundle's own manifest — untrusted input that becomes a
# directory name under /data/artifacts AND an rmtree target. Anything outside this alphabet
# could escape the artifacts root or point the delete somewhere else entirely.
#
# It lives in the VALIDATOR because that is where the operator meets it. `Bundle.open` mapped an
# unsafe token to "unknown" while `_read_bundle_identity` wrote the raw string into
# `report.bundle_version` and failed only on absence, so `/validate` answered 200 with
# `report.bundle_version = 'v2026/08'` beside a top-level `bundle_version: 'unknown'` — two
# versions of one bundle on one screen — and `import_bundle` refused after the operator had
# committed. `validate`'s own docstring is the contract ("every refusal an import can raise has
# to be reachable here"), and `bundle.py` imports this module, so the dependency runs the legal
# way round. [M4.14 step B7, finding 2.11]
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_version(raw: object) -> str:
    """Return a version string usable as a path segment, or 'unknown'."""
    text = str(raw or "").strip()
    return text if _SAFE_VERSION.match(text) else "unknown"


def _tables(db: sqlite3.Connection) -> set[str]:
    """The bundle's TABLES — `type = 'table'`, the one word `load.py` reads.

    It read `type IN ('table','view')` while `load.unaccounted_tables` and
    `_account_for_shipped_tables` both read `type = 'table'`, so the two halves of §10's "counts
    per table" enumerated different sets: a shipped view was COUNTED here and then claimed by
    nobody on the load side, which is a hole in the exit criterion's "0 unaccounted" — the count
    says rows arrived and no target holds them. v20260828 ships no view, so this costs nothing
    against the artifact that exists and closes the case the next export can open.
    [M4.14 step B9, finding 2.23]
    """
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


def _read_tsv(
    path: Path, report: ImportReport, rule: str, required_columns: Iterable[str] = ()
) -> list[dict[str, str]] | None:
    """One reader for every curated TSV in the bundle, or one report line saying why not.

    THE CONTRACT: this is the only opener of a curated TSV in the importer.
    `bundle.validate_id_partition` reads `corrections_v1.tsv` and
    `dna_vocab/<version>/adjudications_v1.tsv` through it, and so do `dna.parse_corrections`,
    `dna._load_aliases` and `dna.load_adjudications` — five call sites, one handler, because
    the handler is the whole point and five copies of it is five chances to omit one. Both
    halves of that sentence were false until M4.14 cycle 1: the loader had been renamed public
    by this milestone's own step C2 and the name here still pointed at nothing, and
    `validate_id_partition` was opening both ledgers itself. [M4.14 cycle 1, M414-REV-247-05]

    §10 promises the operator a report and `/validate`'s own docstring promises it writes
    nothing — but `validate_id_partition` opened `corrections_v1.tsv` and `adjudications_v1.tsv`
    with `encoding="utf-8"` and no handler, so one latin-1 byte left that route as a
    `UnicodeDecodeError` with no finding at all. These are the hand-edited ledgers (6 corrections
    and 828 adjudications on v20260828), which makes them exactly the files a stray byte reaches.

    `None` rather than an empty list on a refusal, because the two mean different things to every
    caller: decision 247 makes a ledger that parses to nothing a refusal to REPLACE rather than
    an instruction to delete 828 curated verdicts, and that distinction is gone once an
    unreadable file has been flattened into "no rows". The header check is the same rule one
    column over — a name upstream never wrote reads nothing and now says so.
    [M4.14 step B2, finding 2.12]
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


# BUNDLE.json cannot list its own sha256, so it is the one legitimate member of "present but
# unlisted". Verified on both artifacts: 42 files listed in v20260828, 33 in the fixture, and in
# each case BUNDLE.json is the only file on disk outside the map.
_UNLISTED_EXEMPT = "BUNDLE.json"


def _bundle_manifest(root: Path) -> dict[str, Any] | None:
    """`BUNDLE.json` read without a report line. ONE FILE, ONE OWNER.

    `_read_bundle_identity` owns the absent/unparseable BUNDLE.json failure and states it in the
    operator's terms. A second reader emitting a second line for the same broken file is the
    misattribution `validate_hyperparams` refuses one file type over.
    """
    path = root / _UNLISTED_EXEMPT
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256(path: Path) -> str:
    """The file's digest, read in chunks: `content.sqlite` is 374 MB and `reviews.sqlite` 416 MB,
    and a bundle must not have to fit in memory to be checked."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_bundle_files(root: Path, report: ImportReport) -> None:
    """Every file BUNDLE.json lists, stat'd and hashed, before a single row is written.

    THE CONTRACT: `bundle.validate()` calls this FIRST — ahead of `validate_id_partition` and of
    every content rule — because everything after it is a statement about bytes that nothing else
    has checked. §10 puts validation before the flip so a bad bundle never becomes the active
    one, and a `content.sqlite` truncated in transit is a bad bundle that every other rule reads
    as a small one.

    The corpus ships the evidence and nothing read it: `files` is a 42-entry map of
    `{bytes, sha256}`, `validations` is 68 rows with an `ok` flag, `total_bytes` is
    1,042,461,726 — and only `bundle_version`, `vocabulary_version` and `tables.title` were ever
    looked at. Measured against v20260828: a zeroed `equating_map.json` validated clean, a
    truncated `reviews.sqlite` validated and then died as an uncaught `sqlite3.DatabaseError`
    inside the import transaction, and a truncated `content.sqlite` made `validate` itself a 500
    — after which `artifact_bundle_one_seed` makes the redo impossible.
    [M4.14 step B1, findings 2.4 and 2.5]

    HASHING IS NOT OPTIONAL FOR A SEED, and it costs what it was measured to cost: 42 files,
    1.04 GB, 0.64 s re-measured in this worktree with a warm page cache, plus about 1.0 s for the
    two `PRAGMA quick_check`s. Seconds on the reference box's disk, not minutes — and it is a
    once-per-import read of files the import is about to read anyway.

    A note for the caller that wires this in: a bundle whose files are edited after the corpus
    wrote BUNDLE.json IS a bundle whose inventory no longer matches, and a test fixture that
    mutates `content.sqlite` after `make_bundle()` is in exactly that state.
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
            # A `files` key is untrusted input that this function turns into a path — the same
            # rule the version token is held to, one file down. [M4.14 step B1]
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
            # THE LEAF, which every level above it already guards, and the two coercions this
            # loop made on it are two halves of one defect. `int(declared_bytes)` on a value read
            # straight out of BUNDLE.json raised ValueError out of `validate()`,
            # `validate_for_install` and both routes - none of which has an `except`, and none of
            # `app.py`'s nine registered handlers names ValueError or TypeError - so a manifest
            # this app cannot parse reached the operator as "Internal Server Error", verbatim the
            # class the row `data-rules-validation-reports-rather-than-raises` calls "never
            # exceptions". And `bool(declared_hash)` made the digest OPTIONAL: an entry declaring
            # none took the success branch below, counted itself verified, and the note at the
            # foot of this function then asserted that every listed file had been sha256-verified
            # when none of them had. Measured against the fixture under a size-only
            # `{name: bytes}` inventory: a zeroed `artifacts/equating_map.json` - finding 2.4's
            # own example - validated `ok=True` behind that affirming sentence.
            #
            # Both are refusals rather than tolerances, because the exit criterion is that every
            # file listed in BUNDLE.json is sha256-verified before a single row is written, and
            # an inventory that declares nothing to check means the tree on disk is not the tree
            # the corpus inventoried - the same rule as listed-but-missing, one field further in.
            # A digest the corpus could not compute is a bundle defect the corpus must fix, and
            # decision 162 plus `artifact_bundle_one_seed` are what make the seed it would
            # corrupt unrepeatable. [M4.14 cycle 4, M414-C4-REF-01 and M414-C4-REF-02]
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
            # The readable/unreadable boundary, which is the one this loop had no branch for: a
            # file that is ABSENT is reported two lines up, and a file that is PRESENT and cannot
            # be opened escaped `validate()`, `validate_for_install` and both routes as an
            # uncaught exception - `api/artifacts._open` guards `Bundle.open` alone, and
            # `app.py`'s last-resort `OSError` handler deliberately re-raises anything that is
            # not one of five network errnos, so EACCES on the operator's first press of Validate
            # arrived as "Internal Server Error". That is the state the reference box produces on
            # its own: the image runs as uid 1000, `/data/import` is a host bind mount, and a
            # bundle copied in by root (scp, `docker cp`, an older root container's
            # `.unpacked-*` tree) carries files this process can stat and cannot read. A listed
            # file removed or replaced between the stat and the hash is the same frame.
            #
            # `_read_tsv` one screen up catches the same class for step B2's stated reason, and
            # this is the same sentence one reader over: §10 promises a report, and the milestone
            # thesis is that every way this importer can refuse becomes a report line.
            # [M4.14 cycle 2, m414-c2-refusals-01, step B1]
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
        # The SAME rule as listed-but-missing, and deliberately so: both mean the tree on disk is
        # not the tree the corpus inventoried, and an extra file is the shape a half-extracted
        # archive and a hand-edited bundle both take.
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
        # `undeclared` joins the guard for the reason the sentence exists: it is the one line in
        # this report that says the hashing happened, and on an inventory that declared no
        # digests it printed "0 file(s) ... verified" beside 33 failures - two contradictory
        # sentences about one bundle on one screen. [M4.14 cycle 4, M414-C4-REF-02]
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
    """The corpus's own export checks, which this app has never read (finding 2.4).

    68 rows on v20260828, every one `ok: true` — and the corpus runs there precisely the
    referential checks `_validate_integrity` below now runs here, then writes the verdict into a
    list nothing opened. Both projects believed the other was checking. A failed row is a failure
    and not a note because the exporter is saying, inside the bundle, that the bundle is wrong.
    """
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

    A truncated `content.sqlite` made `validate` itself a 500 and a truncated `reviews.sqlite`
    died inside the import transaction, because in both cases the first thing to touch the file
    was a query that assumed it. `quick_check` and not `integrity_check`: it skips the
    index-versus-table cross-checks, which is the expensive half, and this is a transport check
    rather than a forensic one. Measured on v20260828: 0.57 s for the 374 MB `content.sqlite`
    and 0.42 s for the 416 MB `reviews.sqlite`. [M4.14 step B1, finding 2.5]
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

    THE CONTRACT: `bundle.validate()` calls this once `validate_content` has filled
    `report.table_counts`, and `bundle.import_bundle` calls it again after the load. Twice
    because the two are different moments and not different facts - the validator's call is the
    one the operator reads before they commit, the import's is the backstop for a tree that
    changed between them - and the answer is identical at both, since the `loaded:<target>` keys
    the loaders add below are never compared and `declared` never carries that prefix. The
    report records one statement once, so the second call adds no second line.
    [M4.14 cycle 1, m414-c1-dim-refusals-03]

    The comparison is cheap and independent of the hashes above:
    `tables` is what the corpus says it exported and `report.table_counts` is what the shipped
    `content.sqlite` actually holds, so a disagreement means the file is not the one the manifest
    describes even in the case where a re-hash would agree — an export that wrote its manifest
    from the wrong side of a filter.

    The `loaded:<target>` counts the loaders add are deliberately NOT compared: they are a
    different fact (rows per POSTGRES target, after mapping, after the display split), and a
    bundle table that legitimately feeds no target would read as a shortfall. [M4.14 step B1]
    """
    payload = _bundle_manifest(root)
    declared = payload.get("tables") if payload else None
    if not isinstance(declared, dict) or not report.table_counts:
        return
    # `_verify_bundle_files`' leaf rule, asked of this manifest's other map. `int(n)` on a value
    # the corpus wrote raised ValueError out of both routes as a 500, and it is the WORSE of the
    # two sites for it: this function is called a second time inside the import transaction,
    # where a ValueError is none of `import_bundle`'s named refusals and falls to the
    # `except BaseException` arm, so the job closes "the import did not run to a report" with an
    # exception type as its only evidence. A count this app cannot read is a report line, because
    # §10 promises a report and the row `data-rules-validation-reports-rather-than-raises` says
    # "never exceptions". Enumerated rather than returned on, for the reason that row's `why`
    # gives: a validator that dies on the first surprise cannot name the rest.
    # [M4.14 cycle 4, M414-C4-REF-01]
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
    """`reviews.sqlite`, held to the same rules as `content.sqlite`.

    THE CONTRACT: `bundle.validate()` calls this beside `validate_content`, with a read-only
    connection to `reviews.sqlite`.

    Rule 7 is a rule about the BUNDLE and it was applied to half of one. `validate` opened
    `content.sqlite` only; `reviews.sqlite` was first opened inside the load; and the report's
    note "no %_bak% / %_good tables present" read as a statement about the bundle. §8 stage 5
    re-extracts DNA from these bodies and §4.3's review-text block is an SVD over them, so a
    stale `review_bak` here is precisely the class rule 7 exists to catch: the extracted tier
    rebuilt from a pre-adjudication copy, with nothing in the read path calling it an error.
    [M4.14 step B8, finding 2.22]
    """
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

    # The columns the loader SELECTs, checked here rather than discovered as an
    # `OperationalError` mid-COPY. Three of these eight were named after the Postgres side and
    # selected verbatim from SQLite, where they do not exist, and 485,602 rows loaded with no
    # rating, no date and no critic flag under a single warn. [M4.9; M4.14 step B8]
    from spielplan.importer.reviews import REVIEW_SOURCE

    _guard(schema, report, "rule7-denylist", "review", *sorted(set(REVIEW_SOURCE.values())))
    return report


# The app's foreign keys, mirrored onto the tables the CORPUS names, because this runs against
# `content.sqlite` and not against Postgres. `(child, column, parent, parent column)`.
#
# Nine synthetic orphan variants passed `validate()` and `validate_for_install()` and then raised
# `ForeignKeyViolationError` inside the transaction, where `app.py`'s handler turns any
# `PostgresError` into 500 {"detail": "database error"} — the staged tree left behind, no row
# naming it, and §10's promised report replaced by two words. The real v20260828 is clean on
# every shape below, so this is next-export exposure; but the corpus already runs these checks at
# export and writes them into `BUNDLE.json`'s `validations`, a list nothing read (finding 2.4).
# Both projects believed the other was checking. Measured: 0.49 s over the 374 MB content.sqlite.
#
# `title_company.title_id` and `title_list_membership.list_id` are here and not in the plan's own
# enumeration, on the enumeration's stated rule — 0003:124 gives `title_company` the same
# `REFERENCES title(id)` as its four siblings and M4.9 moved the table into `MAPPINGS`, and
# 0015:80 keys a membership row to `title_list(id)`, which is `seed_list` under the corpus's
# name. A list of "the app's FKs" that omits two of them checks what it happens to remember.
# [M4.14 step B3, finding 2.9]
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
    ("title_language", "title_id", "title", "id"),
    ("title_country", "title_id", "title", "id"),
    ("title_company", "title_id", "title", "id"),
    ("title_list_membership", "title_id", "title", "id"),
    ("title_list_membership", "list_id", "seed_list", "id"),
    ("rating_title_map", "title_id", "title", "id"),
    ("rating_title_map", "source_id", "rating_source", "id"),
    ("watchlist", "title_id", "title", "id"),
    ("ml_genome_score", "tag_id", "ml_genome_tag", "tag_id"),
)

# Bundle columns COPY will hit that are NOT NULL on the Postgres side and have no rule 6
# `coalesce_empty` answer in `load.MAPPINGS` — so a NULL here is a `NotNullViolationError` inside
# the one transaction that carries the whole seed, which `app.py:113-117` turns into the same
# empty-report 500 as the orphans above. Named as the corpus names them, against the migration
# that declares the target NOT NULL. [M4.14 step B3, finding 2.9]
_NOT_NULL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ml_genome_score", "movie_id"),        # 0003: PRIMARY KEY (ml_movie_id, tag_id)
    ("ml_genome_score", "tag_id"),          # 0003: and a FK to ml_genome_tag(tag_id)
    ("ml_genome_score", "relevance"),       # 0003: relevance real NOT NULL
    ("ml_genome_tag", "tag_id"),            # 0003: tag_id integer PRIMARY KEY
    ("ml_genome_tag", "tag"),               # 0003: tag text NOT NULL
    ("award", "award"),                     # 0003: award.body text NOT NULL
    ("title_video", "key"),                 # 0018: PRIMARY KEY (title_id, source, key)
    ("dna_tag", "facet"),                   # 0004: facet text NOT NULL (bespoke loader)
    ("dna_projected", "facet"),             # 0004: facet text NOT NULL (bespoke loader)
    ("rating_title_map", "external_id"),    # 0003: PRIMARY KEY (source_id, source_key)
    ("title_list_membership", "list_id"),   # 0015: PRIMARY KEY (list_id, title_id)
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


def _duplicate_groups_through_the_loader(
    db: sqlite3.Connection, tmap: Any
) -> tuple[int, list[tuple]]:
    """The same count for a key column the mapping TRANSFORMS, read through the loader's reader.

    `title_language.role` is `_primary_role(is_primary)`, which collapses 0 and NULL onto '' — so
    two rows that differ in SQLite are one row under the app's key, a `GROUP BY` over the source
    columns would report no duplicate, and COPY would still roll the whole seed back. What COPY
    sees is what has to be counted, and it is counted through `load._rows` rather than through a
    second SQL spelling of the transform, which is the "two readers of one file drift" failure
    this module keeps citing. One table takes this path (47,302 rows on v20260828).
    """
    from spielplan.importer import load

    index = [tmap.pg_columns.index(column) for column in tmap.key]
    seen: set[tuple] = set()
    duplicated: set[tuple] = set()
    for row in load._rows(db, tmap):
        key = tuple(row[i] for i in index)
        if key in seen:
            duplicated.add(key)
        else:
            seen.add(key)
    return len(duplicated), sorted(duplicated, key=str)[:3]


def _validate_integrity(
    db: sqlite3.Connection, schema: dict[str, set[str]], report: ImportReport
) -> None:
    """Referential integrity, NOT NULLs and this app's own keys — before anything is staged.

    §4.1's rules are about what the corpus MEANS; these three are about what Postgres will
    accept, and the importer checked neither until now: `validate_content` enforced the eight
    landmine rules and nothing about foreign keys, NULLs or the keys this app narrows the
    corpus's rows onto. Every one of those violations reached the database as an exception
    inside the single transaction that carries the whole seed, and §10's report — the one thing
    the operator is standing in front of — could not name a single offending row.

    Each finding names the table, the column, the count and the first offending ids, because the
    operator's next move is an export-side fix and "a foreign key failed" is not a bug report.

    The duplicate-group check is derived from `load.MAPPINGS`' own `key` rather than from a second
    list kept here: a key that drifts from the migration is exactly how 17,342 duplicate
    `title_language` groups reached a COPY that rolled the seed back (0015), and a hand-maintained
    copy in the validator would drift the same way one file further from the DDL.
    [M4.14 step B3, finding 2.9]
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
        if any(column in tmap.transforms for column in tmap.key):
            groups, first = _duplicate_groups_through_the_loader(db, tmap)
        else:
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
        # The same DatabaseError -> report.fail shape `_guard` applies one query later. This is
        # the first query this module makes, above every guard, so a truncated `content.sqlite`
        # raised out of `validate` itself and the operator got a 500 where §10 promises a page.
        # [M4.14 step B1, finding 2.5]
        report.fail("bundle", f"content.sqlite cannot be read: {exc}")
        return report
    tables = set(schema)

    # ---- rule 7: deny-list ------------------------------------------------
    # Named rather than counted, and the database it examined named with them: this pass reads
    # `content.sqlite` and `validate_reviews` reads the other half, and the old note ("no %_bak%
    # / %_good tables present") read as a statement about the whole bundle when it had never
    # opened `reviews.sqlite`. [M4.14 step B8, finding 2.22]
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

        # The corpus's two namings, counted rather than rewritten in silence. §4.3 keys a
        # vocabulary id as `facet.term` and the corpus files the extraction pass that found the
        # tag under its own label (`character_dynamics` for `characters.*`), so the shipped
        # `facet` column and the term's prefix legitimately disagree on 29,188 of 31,540
        # `dna_tag` rows and 206,151 of 223,136 `dna_projected` rows. `importer/dna.app_facet`
        # stores the prefix, because `dna_facet`, `dna_term`, §6.4's axes and §6.8's palette all
        # key on it — and §10 promises a report, so the size of that rewrite is a line in it.
        #
        # A NOTE, not a warn: neither naming is wrong upstream, and nothing about the bundle
        # needs an operator's attention. What would deserve one is this number changing shape
        # between bundles, which is why it is counted per tier. [M4.9 finding 1, step 2.2]
        # `&=` and not `and`, for the reason the block above states: a short circuit would
        # report the first broken tier and leave the second unexamined.
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

    # §4.1's eight rules are about what the corpus MEANS. These three are about what Postgres
    # will accept, and they run before the counts because the report reads top-down and a count
    # is the one line a bundle that cannot be imported at all can still produce. [M4.14 step B3]
    _validate_integrity(db, schema, report)

    # ---- counts -----------------------------------------------------------
    views = _views(db)
    if views:
        # Counted as a table until M4.14, and then claimed by nobody on the load side, which is
        # a count saying rows arrived with no target holding them. [M4.14 step B9, finding 2.23]
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
            # A table whose pages are unreadable. `_verify_bundle_files`'s `quick_check` names
            # that case as what it is; here it costs one count rather than the whole report.
            continue

    return report


def validate_artifacts(
    root: Path, report: ImportReport, *, spine: Spine | None = None,
    active_coverage: set[int] | None = None,
) -> ImportReport:
    """Validate the `artifacts/` side of the bundle against §4.3.

    `spine` is the *installed* title rows, supplied by the caller that has a connection. Under
    decision 162 a models-only bundle carries no `content.sqlite`, so the identity column below
    has nothing in the bundle to be checked against and the only spine that exists is this one.

    `active_coverage` is the set of title ids the ACTIVE backbone covers among this install's
    `origin='bundle'` titles, supplied by the same caller from the same connection. Decision 248
    makes coverage that goes BACKWARDS the refusal a models-only re-import needs: the corpus
    keeps its own catalogue, so a retrained backbone legitimately covers titles this install
    never seeded, while a title that had a coordinate and stops having one is a merge or a
    dropped row and `§5.1` would go on scoring it from a basis that no longer knows it.
    `None` means the caller had no install to ask — the pre-flight tools and the fixture tests.
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

    # `_REPORTED_AS_FAILURES` is excluded here and not from `BUNDLE_FILES`: decision 251 makes
    # those three absences failures, and a warn beside a failure for one file says the absence is
    # tolerable and not tolerable at once — the two-lines-for-one-fact misattribution
    # `validate_hyperparams` refuses one file type over.
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
        # The third unguarded coercion of the same shape, and the only one of the three that
        # predates M4.14 - a block that is not an object raised AttributeError and a `size` that
        # is not a number raised ValueError or TypeError, both out of a route with no `except`.
        # A warn rather than a failure because the branch it guards is one: the contract is
        # parsed by §8 stage 9's own parser twenty lines down, and that is the reader whose
        # refusal is the failure. This one only has to stop being an exception.
        # [M4.14 cycle 4, M414-C4-REF-01]
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
        except (ValueError, AttributeError, TypeError) as exc:
            # A width or a scale of the wrong TYPE reaches the parser as an int()/float()
            # conversion rather than as its own refusal. Still a bundle the app cannot read, so
            # still a report line: §10 has no room for a traceback.
            #
            # AttributeError and TypeError join it for the same reason one cycle later: the
            # parser's own type-checks reach `spec.get("size")` only once `spec.get("name")` has
            # worked, so a `content_blocks` ENTRY that is not an object - the shape the sum above
            # now warns about - raises out of `contract.py:407` instead, past the one arm that
            # was written to keep this file's defects out of the operator's face. Closing it here
            # rather than in `placement/contract.py` keeps the rule where §10's report is owed:
            # `FeatureContract` is §8 stage 9's parser and its refusals are ContractError by
            # design; what this arm owes is that NONE of its readings escapes as a traceback.
            # [M4.14 cycle 4, M414-C4-REF-01]
            report.fail("feature-contract", f"feature_contract.json cannot be parsed: {exc}")

    _validate_seed_list(root, report, spine)
    _validate_model_artifacts(root, report, contract, spine, active_coverage)

    # ONE derivation, in `importer/vocab.py`, shared with `bundle.py` and `ArtifactStore.open`.
    # This function used to repeat the listing inline — lexicographic, so `v2` beat `v10` — a
    # hundred lines from the copy in `bundle._vocabulary_version`. [M4.14 step B5, finding 2.14]
    from spielplan.importer import vocab

    vocab_dir = root / "dna_vocab"
    # The tree is read whatever BUNDLE.json declares, and the declaration then decides the
    # ANSWER rather than whether the question is asked. Guarded on `is None`, the one bundle
    # decision 163 exists to refuse - a tree carrying two vocabularies - skipped this branch
    # entirely the moment it declared one, and the refusal reached the operator as a traceback
    # from `_validate_model_artifacts` instead. A declaration is the corpus's statement and the
    # directory is this app's inference from it, which is why the declaration still wins; what
    # it may not do is stop the inference being made. [M4.14 cycle 1]
    declared = report.vocabulary_version
    try:
        derived = vocab.version_of(root)
    except vocab.VocabularyError as exc:
        report.fail("vocabulary", str(exc), versions=list(exc.versions))
        derived = None
    if declared and derived and declared != derived:
        # The second reader's half of the same rule, argued at `bundle._vocabulary_version`: a
        # declaration that contradicts the tree leaves one bundle with two vocabularies, and the
        # one this report carries is not the one the STAGED tree will answer with. Both values
        # are in hand here, so both are named rather than one silently winning.
        # [M4.14 cycle 3, M414-C3-VOCAB-01, decisions 163 and 256]
        report.fail(
            "vocabulary",
            f"BUNDLE.json declares DNA vocabulary {declared!r} and this bundle ships "
            f"dna_vocab/{derived}/ - section 4.3 names the vocabulary by the directory, so this "
            "bundle gives two answers and decision 163's comparison cannot be made against "
            "either; export it with the key and the tree naming one version",
            declared=declared, derived=derived,
        )
    report.vocabulary_version = declared or derived

    # `dna_tag`/`dna_projected` carry a FK to `dna_vocabulary(version)`, and `validate_content`
    # ran first and counted both tiers. If the bundle ships DNA rows and no vocabulary to create
    # that row from, the load dies mid-transaction on a raw foreign-key violation
    # (`0004_dna.sql:76`) instead of here, where the operator can read why: reproduced as
    # `validate.ok=True`, `/validate` 200, `/import` 500. The comment two branches down has
    # described this exact failure since M4.5 while guarding only the other branch.
    #
    # The warning stays for a bundle with no DNA rows: §3.1 makes an empty naming layer legal,
    # and a models-only bundle is the kind least likely to ship a `dna_vocab/` tree at all.
    # [M4.14 step B4, finding 2.10]
    # Counted and reported SEPARATELY, never summed. §4.1 rule 1 is a claim about rows, and a
    # sum drops the tier discriminator exactly as a UNION does: an operator reading one "17 DNA
    # rows" cannot tell whether the vocabulary this bundle is missing names extracted tags,
    # projected ones or both, which is the first thing the answer turns on. `_strip_scalar_tier
    # _counts` in `test_landmine_guards.py` already sanctions a per-tier count as "a labelled
    # number, not a row" and sanctions no addition of two; its Python arm rejected `tagged +
    # projected` by the spelling, and the spelling was the defect. [M4.14 step B4, finding 2.10]
    tagged_rows = report.table_counts.get("dna_tag") or 0
    projected_rows = report.table_counts.get("dna_projected") or 0
    resolved = report.vocabulary_version
    version_dir = vocab_dir / resolved if resolved else None
    if version_dir is not None and version_dir.is_dir():
        # The corpus ships one combined `vocab_<version>_all.tsv` plus a per-facet TSV each
        # (`vocab_mood_v1.tsv`, …). `terms.tsv` was this app's own name for a file no bundle has
        # ever contained, so this check failed on every real bundle and on no broken one.
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
        # The same statement about a bundle that DID name a vocabulary and shipped no tree for
        # it - which is precisely what decision 256's refusal asks the operator to produce. The
        # sentence below said "it names nothing" over a report whose header carries the name the
        # bundle declared, two elements from decision 266's line quoting that same name: two
        # answers about one field on one screen, the shape step B7 removed one field over.
        # [M4.14 cycle 3, M414-C3-VOCAB-03, decisions 256 and 266]
        report.warn(
            "vocabulary",
            f"this bundle declares DNA vocabulary {resolved!r} and carries no "
            f"dna_vocab/{resolved}/ directory, so it ships no vocabulary files at all: a seed "
            "from it leaves the naming layer empty (section 3.1), and a re-import from it leaves "
            "the installed one exactly as it is",
            version=resolved,
        )
    else:
        # A statement about the BUNDLE, because that is the only thing this function has read.
        # "The naming layer will be empty" is true of a seed and false of the models-only
        # re-import decision 162 makes the recurring shape: the install's naming layer is
        # whatever its last content import left, and this validator has no connection with which
        # to know. An operator reading it on a model bundle was sent to look at an install that
        # was intact. [M4.14 cycle 2, m414-c2-dim247-declared-vocabulary-no-tree, decision 266]
        report.warn(
            "vocabulary",
            "no dna_vocab/ in this bundle and no DNA rows, so it names nothing: a seed from it "
            "leaves the naming layer empty (section 3.1), and a re-import from it leaves the "
            "installed one exactly as it is",
        )

    return report


def validate_hyperparams(store_dir: Path, report: ImportReport) -> ImportReport:
    """§4.3's constants file, read by the app's own reader before §10 stages it.

    §10's sequence is validate -> stage -> recompute the rebuild set -> flip -> restart, and the
    restart is why this check belongs at step 1 and nowhere later: `hyperparams.from_mapping`
    raises `ValueError` on a constant outside its range, and that refusal has no catcher between
    here and the three Ledger routers — so a bundle that reaches the flip with a hand-edited λ or
    a non-positive `straddle_z` turns the Rate and Rank surfaces into refusals on a box whose
    operator has already walked away, with `artifacts/<version>/` staged and the active row
    flipped. Checked here it is one report line naming the key, in the report the operator is
    standing in front of, with nothing written and nothing staged.

    A `fail` and not a `warn`, for the same reason the routers refuse rather than default: §4.3
    makes this file the single source of the §5.2 constants, and importing on substituted
    defaults changes `hp_digest`, which invalidates every cached fit in the install.

    Through `hyperparams.load` rather than a second parser, which is the rule `feature_contract`
    above already follows: one reader of a bundle file, so the validator cannot pass a file the
    app will then refuse. It is the staged directory's own layout — `ledger_hyperparams.json`
    beside `manifest.json` — that decides whether the file is there at all.
    [M4.10 finding 10; ml06]
    """
    from spielplan.ledger import hyperparams
    from spielplan.models.artifacts import ArtifactStore

    constants = store_dir / "ledger_hyperparams.json"
    if not (constants.exists() or constants.is_symlink()):
        # §4.3 lists the file as optional and `validate_artifacts` has already warned about every
        # absent optional artifact by name. A second line for this one would report one absence
        # twice, and §3.1 makes the defaults legal.
        #
        # ABSENT, not merely unopenable: the guard was `is_file()`, which is false for a path that
        # exists as a directory, so an artifacts tree assembled by an extraction that made one
        # returned this report untouched — no failure and not even the "constants read" note, while
        # the app booted on DEFAULTS under a different `hp_digest`. `hyperparams.load` draws the
        # same distinction for the same reason. [M4.10 cycle 1, M410-R1-06]
        return report
    # Constructed rather than `ArtifactStore.open`ed, which is the one place this section departs
    # from how every other reader addresses a store. `open` re-parses `manifest.json`, and a
    # manifest that is not readable JSON therefore raised out of it into the handler below and was
    # reported as the CONSTANTS file's parse error: one truncated manifest, two failures, and an
    # operator sent to open a `ledger_hyperparams.json` in which every key is in range. That is
    # exactly the misattribution the clause below refuses one file type over, and §10 makes this
    # report the decision point. `validate_artifacts` owns the manifest line and has already
    # emitted it by name; `hyperparams.load` reads only `is_empty` and `path()`, so the constants
    # are still checked on a bundle whose manifest is broken and the operator gets both facts in
    # one pass. [M4.10 cycle 2, m410-c2-validate-blames-the-constants-for-a-broken-manifest]
    store = ArtifactStore(version=report.bundle_version or "staged", root=store_dir)
    try:
        hp, _notes = hyperparams.load(store)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Before the `ValueError` clause, and not merely for tidiness: `json.JSONDecodeError` IS
        # a `ValueError`, so the broader clause would report an unparseable file as a constant
        # out of range and send the operator looking for a key that is not the problem.
        report.fail("hyperparams", f"ledger_hyperparams.json is not readable JSON: {exc}")
    except ValueError as exc:
        report.fail(
            "hyperparams",
            f"ledger_hyperparams.json carries a constant the §5.2 fit cannot use: {exc}",
        )
    else:
        # The digest, because §10's re-import report is a diff: two bundles whose constants agree
        # produce the same fits, and the operator cannot tell that from a line that only says ok.
        report.note(
            "hyperparams",
            f"§5.2 constants read from the bundle; fit digest {hp.digest()}",
            hp_digest=hp.digest(), hp_source=hp.source,
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
        # The SAME token rule the import refuses on, applied where the operator reads the answer.
        # This wrote the raw string into `report.bundle_version` and failed only on absence, so a
        # bundle named `v2026/08` validated 200 with `report.bundle_version = 'v2026/08'` beside a
        # top-level `bundle_version: 'unknown'` from `Bundle.open` - two versions of one bundle on
        # one screen - and `import_bundle` refused after the operator had committed.
        # [M4.14 step B7, finding 2.11]
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
        # The test `bundle._vocabulary_version` applies to the same key, applied where the second
        # reader is. Anything truthy was accepted here and coerced with `str()`, so an export
        # writing a JSON number put "v1" in this report's header - the other reader had rejected
        # the number and fallen to the directory - and "no dna_vocab/1/" in the failure beneath
        # it: two vocabularies for one bundle on one screen, and a refusal naming a directory the
        # corpus never wrote. That is the shape step B7 removed one field over.
        # [M4.14 cycle 1, m414-c1-declaration-read-twice]
        report.vocabulary_version = vocabulary
    _validate_nullable_pk_columns(payload, report)


def _validate_nullable_pk_columns(payload: dict[str, Any], report: ImportReport) -> None:
    """rule 6's landmine, read from where the corpus declares it.

    `nullable_pk_columns` names the primary-key components SQLite let through as NULL, and the
    corpus's own `nullable_pk_note` states the contract this app is held to in as many words:
    "the Postgres importer coalesces TEXT-affinity components to '' (spec §4.1 rule 6)". So the
    refusal is exactly about that promise - a column this app COALESCES whose declared affinity
    is not TEXT would write '' into a column that cannot hold one, and COPY rejects the seed.

    Measured on v20260828, which is why the rule is not the shorter "not TEXT is a failure": the
    real bundle declares four entries and two of them are INTEGER (`ml_genome_score.movie_id`
    and `.tag_id`). This app coalesces neither - they are NOT NULL on both sides and carry no
    rule 6 answer at all - and `_validate_integrity`'s NOT NULL scan is what stands behind them,
    as the corpus's own note does when it asserts every listed column NULL-free in that bundle.
    Failing on affinity alone would refuse the only bundle that exists. [M4.14 step B3]
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
    """`seed_list.json`'s shape and its title ids, read here rather than inside the transaction.

    `bundle.validate_id_partition` swallows a parse error into `payload = []` and
    `dna.load_seed_list` then re-raises it inside the transaction through a bare `json.loads` and
    `int(item["title_id"])`. And `seed_list.title_id` is a NOT NULL foreign key to `title(id)`
    (`0003_content.sql`), so one entry naming a title the install does not carry aborts the whole
    import on a constraint violation that names a constraint rather than a file.

    Checked to the shape the loader may then rely on: a list, or `{"titles": [...]}`, every entry
    an object with an integer `title_id`. v20260828 writes the first (100 entries of `title_id`,
    `title`, `year`, `kind` and the three rating shares); the second is what a hand-edited list
    arrives as and costs one branch.

    An id the spine does not carry is a FAILURE against the bundle's own `content.sqlite` - the
    bundle disagreeing with itself - and a counted NOTE against an INSTALLED spine, which is
    decision 247's rule for the loader: `load_seed_list` skips and counts those entries because
    the corpus's catalogue is not frozen at this install's seed (decision 248).
    [M4.14 step B2, findings 2.12 and 2.15]
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

    # `year`'s SHAPE, which this function's contract covers ("checked to the shape the loader may
    # then rely on") and which it read nothing of. `dna._decade` turns this field into
    # `seed_list.decade`, and a `year` of `NaN` - the spelling an unresolved year arrives in from
    # a frame, since `json.dumps` writes the bare literal and `json.loads` accepts it - raised
    # ValueError inside the import transaction, past every arm that could have named it. The
    # loader now keeps a NULL decade for it, as its own docstring always promised; this is the
    # half that says so before the operator commits, which is `validate_for_install`'s contract.
    # A note and not a failure, on the loader's trade: a hole in §4.3's stratification is a
    # smaller loss than a refused seed. The DERIVATION stays in `dna._decade` alone and is not
    # repeated here - this asks only whether the field is a number, which is a question about the
    # file. [M4.14 cycle 4, m414-c4-dim247-03]
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


# decision 251: an absent `feature_contract.json`, `cold_tower.pt` or `backbone.npz` is a
# validation FAILURE on both bundle kinds, and the argument is recorded with the files rather
# than left implicit. §10 step 4 recomputes the rebuild set on EVERY import, and
# `placement/reconcile.py` reaches all three without a guard that a real import can miss.
#
# `BUNDLE_FILES`' `required` flags stay exactly as they are: they answer a different question
# (what this store can be LOADED with at all, which §3.1 makes legal to answer with "nothing")
# and they feed the Data tab's `missing_required` summary. [M4.14 step B6, finding 2.13]
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

    for name, (rule, why) in _REPORTED_AS_FAILURES.items():
        if not (root / name).is_file():
            report.fail(rule, f"{name} is missing: {why} (decision 251)", file=name)

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
        return                          # named as a failure by the decision 251 loop above
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
    from spielplan.importer import vocab
    from spielplan.models.artifacts import ArtifactStore
    from spielplan.placement.tower import TowerError, load_tower

    try:
        store = ArtifactStore.open(root, report.bundle_version or "unvalidated")
    except vocab.VocabularyError as exc:
        # `ArtifactStore.open` derives the vocabulary through `vocab.version_of`, which RAISES on
        # a tree holding two - correctly, since its own docstring argues that the one caller with
        # no report to write is the one where the exception has to carry the sentence. This
        # caller HAS a report, and it is three hundred lines above the branch that catches the
        # same exception, so decision 163's refusal left `validate()`, left
        # `validate_for_install`, and left both routes as a 500 with no findings at all: neither
        # `_open` (BundleOpenError/TarError/ZstdError/OSError) nor `app.py` catches a
        # `RuntimeError`. Reachable exactly when the bundle DECLARES a vocabulary, because the
        # declaration is what disarms the two report-producing derivations - and decision 256's
        # own refusal message asks the corpus to start writing that declaration.
        # [M4.14 cycle 1, m414-c1-vocab-two-version-refusal-is-a-500]
        report.fail("vocabulary", str(exc), versions=list(exc.versions))
        return
    try:
        tower = load_tower(store, contract)
    except TowerError as exc:
        report.fail("cold-tower", str(exc))
    except Exception as exc:                                       # noqa: BLE001
        report.fail("cold-tower", f"cold_tower.pt is unreadable: {exc}")
    else:
        # `tower.notes` rides in the same line rather than a second finding: it qualifies this
        # claim (the version in it was assumed from the tensor names, not read off the file), and
        # a qualification in a separate note is one an operator can read without the claim.
        # [M4.13 step 36, cs-54]
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

    WHICH DIRECTION IS THE REFUSAL is decision 248, and it is not the one this check started
    with. The corpus keeps its own catalogue - `sqlite_sequence` stood at title 21442 against the
    19,071 the bundle exported - so a retrained `backbone.npz` covers whatever the corpus had at
    export time, and rows for titles this install lacks are INERT: a `Backbone` lookup is by id
    and never reaches them, while the merge this check exists for is visible on the rows that do
    match. Failing on them refused the first retrained backbone, which is the first model bundle
    this app will ever meet. The refusal is the other direction: coverage that goes BACKWARDS.
    `active_coverage` is the id set the ACTIVE backbone covers among the install's
    `origin='bundle'` titles, from the caller that has a connection; a title that had a
    coordinate and would stop having one is a dropped or merged row, and §5.1 would go on scoring
    it against a basis that no longer knows it. [M4.14, decision 248, finding 2.16]
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
        # A SEED carries `content.sqlite`, which is a better identity source than the vector: it
        # names all 19,071 titles where the vector would name only the 14,397 with a model row,
        # and it is the same fact from the same export. So a seed without the vector is checked
        # against its own spine and warned; a MODEL bundle has no spine of its own, and there
        # the vector is the only thing standing between a corpus-side merge and a silent
        # re-identification — absent, it is a failure.
        #
        # No bundle this app has SEEN carries the array: v20260828's `backbone.npz` holds E,
        # E_full, E_hat, b_hat, b_i, cold_mask, item_n, mu and title_ids, and nothing else. What
        # is no longer true is the reason this comment gave - `mdc export-bundle` DOES write
        # `title_identity` as of movie_data_curator@3666eaa; the export has simply not been
        # re-run since. So failing a seed on the array would still refuse the only bundle that
        # exists today, and stops being a concession the first time the corpus exports again.
        # [M4.14 step C5]
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
        # decision 248, argued in the docstring: on a MODELS-ONLY bundle the spine is the
        # install's, the corpus's catalogue has moved on since it was seeded, and these rows are
        # inert. Counted, because the count is the merge signal an operator reads - a retrain
        # that suddenly covers thousands more titles is a fact about the corpus, not a fault.
        report.note(
            "identity",
            f"{len(absent):,} backbone row(s) name a corpus title this install never seeded "
            f"(first: {absent[:5]}); those rows are never looked up and the identity check is "
            "made on the rows that match",
            rows=len(absent), title_ids=absent[:20],
        )
    elif absent:
        # A SEED is checked against its OWN content.sqlite, and there this is the bundle
        # disagreeing with itself: a row of E that no title in the same export claims.
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
