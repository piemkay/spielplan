"""Bundle import orchestration. Spec v2.1 §10.

Swap sequence, normative:

    validate -> stage to /data/artifacts/<version>/ -> recompute the rebuild set against the
    staged bundle -> transactionally flip artifact_bundle.active -> restart backend + worker

M0 implements validate / stage / flip and reports the rebuild set rather than computing it:
the rebuild set is user fold-in vectors, per-label-count blend weights, a full Ledger MAP
refit and Cold Tower re-placement, none of which exist before M2. The report names them so a
re-import at M2 does not quietly skip them.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from spielplan.importer import dna as dna_loader
from spielplan.importer import load as content_loader
from spielplan.importer import reviews as review_loader
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport

# §10: the rebuild set. "Everything expressed in the old Backbone's basis is garbage against a
# new one." Listed here so a re-import always reports what must be recomputed.
REBUILD_SET = (
    "user fold-in vectors (closed-form, ms)",
    "per-label-count blend weights",
    "full Personal Ledger MAP refit",
    "Cold Tower re-placement of every app-acquired title",
)


# The version string comes out of the bundle's own manifest — untrusted input that becomes a
# directory name under /data/artifacts AND an rmtree target. Anything outside this alphabet
# could escape the artifacts root or point the delete somewhere else entirely.
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_version(raw: object) -> str:
    """Return a version string usable as a path segment, or 'unknown'."""
    text = str(raw or "").strip()
    return text if _SAFE_VERSION.match(text) else "unknown"


@dataclass
class Bundle:
    root: Path
    version: str
    content_db: Path
    reviews_db: Path | None
    artifacts_dir: Path

    @classmethod
    def open(cls, path: Path) -> Bundle:
        """Open a bundle directory, or an unpacked-on-demand `.tar` / `.tar.zst` archive."""
        root = path if path.is_dir() else _unpack(path)
        manifest_path = root / "artifacts" / "manifest.json"
        version = "unknown"
        if manifest_path.is_file():
            version = safe_version(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("bundle_version")
            )
        reviews = root / "reviews.sqlite"
        return cls(
            root=root,
            version=version,
            content_db=root / "content.sqlite",
            reviews_db=reviews if reviews.is_file() else None,
            artifacts_dir=root / "artifacts",
        )


def _unpack(archive: Path) -> Path:
    target = archive.parent / f".unpacked-{archive.stem}"
    if target.is_dir():
        return _single_child(target)
    target.mkdir(parents=True)
    if archive.suffixes[-1:] == [".zst"]:
        import zstandard

        with (
            archive.open("rb") as fh,
            zstandard.ZstdDecompressor().stream_reader(fh) as stream,
            tarfile.open(fileobj=stream, mode="r|") as tar,
        ):
            tar.extractall(target, filter="data")
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
    return _single_child(target)


def _single_child(directory: Path) -> Path:
    children = [p for p in directory.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return directory


def validate(bundle: Bundle) -> ImportReport:
    """Validate without writing anything. This is the wizard's and the Data tab's first step."""
    report = ImportReport(bundle_version=bundle.version)
    if not bundle.content_db.is_file():
        report.fail("bundle", "content.sqlite is missing from the bundle")
        return report

    db = sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
    db.text_factory = str          # rule 8: UTF-8 in, UTF-8 out, no cleaning
    try:
        validator.validate_content(db, report)
    finally:
        db.close()

    validator.validate_artifacts(bundle.artifacts_dir, report)

    if bundle.reviews_db is None:
        report.warn(
            "bundle",
            "reviews.sqlite absent — future re-extraction and text embedding will have no bodies",
        )
    return report


async def import_bundle(
    conn: asyncpg.Connection, bundle: Bundle, artifacts_root: Path, *, activate: bool = True
) -> ImportReport:
    """Validate, load, stage the artifacts, then flip the active row — all in one transaction
    apart from the file copy, which is done before the flip so a failed copy cannot leave the
    DB pointing at files that are not there."""
    report = validate(bundle)
    if not report.ok:
        return report

    already = await conn.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = $1", bundle.version
    )
    if already == "active":
        report.fail("bundle", f"bundle {bundle.version} is already the active bundle")
        return report

    # Stage the artifacts BEFORE touching the DB (§10 swap sequence step 2).
    if bundle.version == "unknown":
        report.fail(
            "bundle",
            "manifest has no usable `bundle_version` — it names the artifact directory, so it "
            "must be a plain [A-Za-z0-9._-] token",
        )
        return report
    staged = (artifacts_root / bundle.version).resolve()
    if not staged.is_relative_to(artifacts_root.resolve()):
        report.fail("bundle", f"bundle version {bundle.version!r} escapes the artifacts root")
        return report
    if staged.exists():
        shutil.rmtree(staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle.artifacts_dir, staged)
    report.note("stage", f"artifacts staged to {staged}")

    db = sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
    db.text_factory = str
    try:
        async with conn.transaction():
            await content_loader.load_content(conn, db, report)

            vocab_version = report.vocabulary_version or "v1"
            vocab_dir = bundle.artifacts_dir / "dna_vocab" / vocab_version
            if vocab_dir.is_dir():
                await dna_loader.load_vocabulary(conn, vocab_dir, vocab_version, report)
            # rule 1: two tiers, two calls.
            await dna_loader.load_tags(conn, db, vocab_version, report)
            await dna_loader.load_projected(conn, db, vocab_version, report)

            await dna_loader.load_corrections(
                conn, bundle.artifacts_dir / "corrections_v1.tsv", report
            )
            await dna_loader.load_seed_list(
                conn, bundle.artifacts_dir / "seed_list.json", report
            )

            # §10 ships the bodies because re-extraction and the review-text block need them;
            # rule 8's mojibake repair happens here and nowhere else.
            if bundle.reviews_db is not None:
                rdb = sqlite3.connect(f"file:{bundle.reviews_db}?mode=ro", uri=True)
                rdb.text_factory = str
                try:
                    await review_loader.load_reviews(conn, rdb, report)
                finally:
                    rdb.close()

            if not report.ok:
                raise _Rollback(report)

            await conn.execute(
                """
                INSERT INTO artifact_bundle (version, manifest, report, state)
                VALUES ($1, $2, $3, 'validated')
                ON CONFLICT (version) DO UPDATE
                  SET manifest = EXCLUDED.manifest, report = EXCLUDED.report,
                      state = 'validated', imported_at = now()
                """,
                bundle.version,
                _manifest_of(bundle),
                report.as_dict(),
            )
            if activate:
                # §10: transactionally flip. The partial unique index guarantees one active row.
                await conn.execute(
                    "UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active'"
                )
                await conn.execute(
                    "UPDATE artifact_bundle SET state = 'active', activated_at = now() "
                    "WHERE version = $1",
                    bundle.version,
                )
                report.note(
                    "swap",
                    "artifact_bundle flipped to active — restart backend and worker; "
                    "no process may score or refit with a different loaded version",
                )
                report.note("rebuild-set", "recompute after this import: " + "; ".join(REBUILD_SET))
    except _Rollback:
        # The DB transaction rolled back, but the artifact copy happened before it (so a failed
        # copy could not leave the DB pointing at absent files). Say which is which rather than
        # claiming nothing happened.
        report.note(
            "rollback",
            f"database transaction rolled back — no rows written. Artifacts staged at {staged} "
            "remain on disk and are overwritten by the next import of this version.",
        )
    finally:
        db.close()

    return report


def _manifest_of(bundle: Bundle) -> dict:
    path = bundle.artifacts_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class _Rollback(Exception):
    def __init__(self, report: ImportReport) -> None:
        super().__init__("import validation failed")
        self.report = report
