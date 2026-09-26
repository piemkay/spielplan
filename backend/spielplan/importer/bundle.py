"""Bundle import orchestration (§10): validate -> stage -> recompute the rebuild set -> flip.

The backend loads the flipped bundle itself, so no restart is owed unless that load fails (decision 497).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from spielplan.core import storage
from spielplan.importer import dna as dna_loader
from spielplan.importer import load as content_loader
from spielplan.importer import reviews as review_loader
from spielplan.importer import validate as validator
from spielplan.importer import vocab as vocab_reader
from spielplan.importer.report import ImportReport

# Re-exported for existing callers; the token rule lives in `validate.py`, where operators meet it.
from spielplan.importer.validate import safe_version
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile as placement

# Used only in `import_bundle`'s `except BaseException` arm, the one exit whose report is never
# returned, so the staging cleanup outcome is logged instead.
log = logging.getLogger("spielplan.importer")

# §10's rebuild set, defined once in `placement`.
REBUILD_SET = placement.REBUILD_SET

# Decision 162's mint floor, below 2^31 because id columns are `integer` and `ledger_fit.title_ids`
# is int32. Also in `0015_seed.sql`.
APP_ID_MIN = 1_000_000_000

# Artifacts keyed by corpus `title.id`; a models-only re-import could carry ids into the app's range.
_ID_BEARING_NPZ = ("backbone.npz", "review_text_emb.npz")


# `placement.rebuild_plan` takes the rebuild steps injected; each is `async (conn, store, version) ->
# dict`.


async def _rebuild_fold_in(conn: Any, store: Any, version: str) -> dict[str, Any]:
    """§10 steps 1 and 2 in one pass: one cross-validated fit produces both."""
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    report = await foldin.run(
        conn, bb.load_for(store), bundle_version=version, only_stale=False, with_priors=True
    )
    return report.as_dict()


async def _rebuild_blend_weights(conn: Any, _store: Any, version: str) -> dict[str, Any]:
    """§10 step 2. Reports the β the step-1 fit produced, per user and kind."""
    rows = await conn.fetch(
        "SELECT user_id, kind, blend_beta, label_count FROM user_vector "
        " WHERE bundle_version = $1 ORDER BY user_id, kind",
        version,
    )
    return {
        "weights": [
            {"user_id": r["user_id"], "kind": r["kind"],
             "beta": float(r["blend_beta"] or 0.0), "labels": r["label_count"]}
            for r in rows
        ]
    }


async def _rebuild_ledger_refit(conn: Any, store: Any, version: str) -> dict[str, Any]:
    """§10 step 3: "a **full** Personal Ledger MAP refit", against the STAGED `version`.

    Without the explicit version the refit read the outgoing bundle's placements and stamped its version.
    """
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    hp, _notes = load_hp(store)
    # Against the staged bundle's Backbone: the whole point is moving every fitted number to the new
    # basis. `run_rebuild` has already asserted `store.version == version`.
    reports = await refit.refit_all(
        conn,
        hp,
        embeddings=observations.standard_embeddings(
            conn, bb.load_for(store), bundle_version=version
        ),
        bundle_version=version,
    )
    return {"fits": [r.as_dict() for r in reports]}


@dataclass
class Bundle:
    root: Path
    version: str
    content_db: Path | None
    reviews_db: Path | None
    artifacts_dir: Path
    vocabulary_version: str | None = None
    # Findings `open` produced before a report existed, as `(severity, message)`; `validate` replays
    # them first. A refusal must be read, not raised (decision 257).
    open_findings: tuple[tuple[str, str], ...] = ()

    @property
    def kind(self) -> str:
        """§10's two kinds of import, under decision 162: content seeds once, models re-import.

        Derived from what the bundle carries, never an operator flag, or content could be seeded twice.
        """
        return "seed" if self.content_db is not None else "model"

    @classmethod
    def open(cls, path: Path) -> Bundle:
        """Open a bundle directory, an archive inside one, or a `.tar` / `.tar.zst` directly."""
        findings: list[tuple[str, str]] = []
        root = path if path.is_dir() else _unpack(path, findings)
        if root.is_dir() and not (root / "BUNDLE.json").is_file():
            root = _archive_within(root, findings) or root
        identity = _identity(root)
        content = root / "content.sqlite"
        reviews = root / "reviews.sqlite"
        return cls(
            root=root,
            version=safe_version(identity.get("bundle_version")),
            content_db=content if content.is_file() else None,
            reviews_db=reviews if reviews.is_file() else None,
            artifacts_dir=root / "artifacts",
            vocabulary_version=_vocabulary_version(identity, root / "artifacts", findings),
            open_findings=tuple(findings),
        )


def _identity(root: Path) -> dict[str, Any]:
    """`BUNDLE.json` at the bundle root: the corpus's own record of what this bundle is.

    Missing or unparseable is not raised here; it becomes the report's "no usable bundle_version".
    """
    path = root / "BUNDLE.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _vocabulary_version(
    identity: dict[str, Any], artifacts_dir: Path, findings: list[tuple[str, str]]
) -> str | None:
    """The vocabulary this bundle carries, read once via `vocab.version_of` (decision 256).

    The BUNDLE.json key wins when the tree does not contradict it.
    """
    declared = identity.get("vocabulary_version")
    declared = declared if isinstance(declared, str) and declared else None
    try:
        # Always read the tree too, so decision 163's two-version check still runs on declared bundles.
        derived = vocab_reader.version_of(artifacts_dir)
    except vocab_reader.VocabularyError as exc:
        # Two vocabularies in one tree have no version to name; a finding, not a raise.
        findings.append(("fail", str(exc)))
        derived = None
    if declared and derived and declared != derived:
        # The declaration and the tree disagree: refuse, since neither can be preferred (decision 163).
        findings.append((
            "fail",
            f"BUNDLE.json declares DNA vocabulary {declared!r} and this bundle ships "
            f"dna_vocab/{derived}/ - section 4.3 names the vocabulary by the directory, so this "
            "bundle gives two answers and decision 163's comparison cannot be made against "
            "either; export it with the key and the tree naming one version",
        ))
    return declared or derived


# The prefix `_unpack` writes and `_clean_unpacked` deletes, keyed on the archive's full name
# (`v1.tar` and `v1.tgz` share a stem).
_UNPACKED = ".unpacked-"

# In-progress extractions, never read, so a killed extraction cannot pass for a bundle.
_UNPACKING = ".unpacking-"

# One tar block (512 bytes), the format's own.
_BLOCK = 512


class BundleOpenError(ValueError):
    """The path the operator named is not an archive this app can open.

    Raised, not reported: there is no report yet. `api/artifacts.py` maps it to 400.
    """


def _unpack(archive: Path, findings: list[tuple[str, str]]) -> Path:
    """Extract `archive` beside itself and return the bundle root inside the extraction.

    A `.unpacked-<name>/` tree is always a COMPLETE extraction: extract into `.unpacking-<name>/`,
    `rmtree` on any failure, and rename only after the archive is shown to end with two zero blocks
    (tarfile silently accepts a truncation on a header boundary). A reused tree must hold `BUNDLE.json`.
    """
    target = archive.parent / f"{_UNPACKED}{archive.name}"
    if target.is_dir():
        root = _single_child(target)
        if (root / "BUNDLE.json").is_file():
            # Name the reused tree: findings about paths inside it are otherwise unexplained.
            findings.append((
                "note",
                f"{archive.name} was read from the extraction already at {target}, not from the "
                "archive: every path named below is inside that tree. Delete it to re-extract.",
            ))
            return root
        # A stale tree whose archive is still here is re-extracted. Checked here, not above, so a complete
        # tree is still reused when the archive was deleted.
        if not archive.is_file():
            raise BundleOpenError(
                f"{archive} is neither a bundle directory nor a file, and the extraction beside "
                f"it at {target} carries no BUNDLE.json, so it is not one this app can open "
                "either. It was left where it is - re-extracting needs the archive back"
            )
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            raise BundleOpenError(
                f"{target} is an incomplete extraction and could not be removed - delete it by "
                "hand and try the import again"
            )
        # Said out loud: a gigabyte removed without a sentence.
        findings.append((
            "note",
            f"an earlier extraction at {target} carried no BUNDLE.json, so it was removed and "
            f"{archive.name} was extracted again.",
        ))

    if not archive.is_file():
        raise BundleOpenError(f"{archive} is neither a bundle directory nor a file")
    zstd = archive.suffixes[-1:] == [".zst"]
    if not zstd and not tarfile.is_tarfile(archive):
        raise BundleOpenError(
            f"{archive.name} is not a tar archive - a bundle is a directory, a .tar or a .tar.zst"
        )

    staging = archive.parent / f"{_UNPACKING}{archive.name}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        if zstd:
            import zstandard

            with (
                archive.open("rb") as fh,
                zstandard.ZstdDecompressor().stream_reader(fh) as stream,
                tarfile.open(fileobj=stream, mode="r|") as tar,
            ):
                tar.extractall(staging, filter="data")
                # Streaming: `tar.offset` counts decompressed bytes, so read the tail off the stream.
                complete = len(tar.fileobj.read(_BLOCK)) == _BLOCK
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(staging, filter="data")
                # Seek back and read the two blocks off the tar's own stream; the file size is compressed
                # and
                # cannot be compared to `tar.offset`.
                tar.fileobj.seek(tar.offset)
                complete = len(tar.fileobj.read(2 * _BLOCK)) == 2 * _BLOCK
        if not complete:
            raise BundleOpenError(
                f"{archive.name} has no end-of-archive marker - it is a truncated or still-"
                "copying transfer rather than a whole bundle, and nothing was extracted from "
                "it. Copy the archive again and retry."
            )
    except Exception:
        # Any exception, same cleanup.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.replace(staging, target)
    findings.append((
        "note",
        f"{archive.name} was extracted to {target}: every path named below is inside that tree, "
        "and it is removed once this import has committed.",
    ))
    return _single_child(target)


def _archive_within(directory: Path, findings: list[tuple[str, str]]) -> Path | None:
    """Decision 257: a directory holding one archive and no BUNDLE.json IS a path to that archive.

    One archive is opened and said so; several is a refusal naming the count.
    """
    archives = sorted(
        p for p in directory.iterdir()
        if p.is_file() and (p.name.endswith(".tar") or p.name.endswith(".tar.zst"))
    )
    if not archives:
        return None
    if len(archives) > 1:
        names = ", ".join(p.name for p in archives[:4])
        findings.append((
            "fail",
            f"{directory} holds no BUNDLE.json and {len(archives)} archives ({names}) - name the "
            "archive to import in the path rather than the directory holding them",
        ))
        return None
    findings.append((
        "note",
        f"{directory} holds no BUNDLE.json and one archive, so {archives[0].name} was opened as "
        "the bundle",
    ))
    return _unpack(archives[0], findings)


def _single_child(directory: Path) -> Path:
    children = [p for p in directory.iterdir() if not p.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return directory


def _in_app_range(values: Iterable[Any]) -> list[int]:
    """The ids of `values` that are at or above the app's mint floor, smallest first."""
    found = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue                       # a malformed id is the owning loader's report line
        if number >= APP_ID_MIN:
            found.add(number)
    return sorted(found)


def validate_id_partition(bundle: Bundle, report: ImportReport) -> ImportReport:
    """Decision 162: refuse a bundle whose ids reach into the range this app mints from.

    Checked on the id-bearing artifacts too, which travel with every models-only re-import. The
    offending id is named.
    """
    if bundle.content_db is not None:
        db = sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
        try:
            for table in ("title", "person"):
                rows = db.execute(
                    f"SELECT id FROM {table} WHERE id >= ? ORDER BY id LIMIT 8", (APP_ID_MIN,)
                ).fetchall()
                _report_app_range(report, f"content.sqlite {table}.id", [r[0] for r in rows])
        except sqlite3.Error:
            pass                           # an unreadable spine is `validate_content`'s line
        finally:
            db.close()

    import numpy as np

    for name in _ID_BEARING_NPZ:
        path = bundle.artifacts_dir / name
        if not path.is_file():
            continue
        try:
            with np.load(path, allow_pickle=False) as npz:
                if "title_ids" not in npz.files:
                    continue               # absence is `_validate_model_artifacts`'s line
                ids = np.asarray(npz["title_ids"]).reshape(-1)
                _report_app_range(report, f"{name} title_ids", ids[ids >= APP_ID_MIN][:8])
        except Exception:                                          # noqa: BLE001, S112
            continue                       # an unreadable artifact is validate.py's line

    seeds = bundle.artifacts_dir / "seed_list.json"
    if seeds.is_file():
        try:
            payload = json.loads(seeds.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = []
        entries = payload if isinstance(payload, list) else []
        _report_app_range(
            report,
            "seed_list.json title_id",
            _in_app_range(e.get("title_id") for e in entries if isinstance(e, dict)),
        )

    corrections = bundle.artifacts_dir / "corrections_v1.tsv"
    if corrections.is_file():
        # Through `validate._read_tsv`, so a bad byte becomes a finding rather than an exception.
        rows = validator._read_tsv(corrections, report, "corrections")
        _report_app_range(
            report,
            "corrections_v1.tsv title_id",
            _in_app_range(r.get("title_id") for r in rows or ()),
        )

    # The adjudications ledger, named by the version like the loader names it (decision 247).
    vocab_version = bundle.vocabulary_version
    if vocab_version:
        name = f"adjudications_{vocab_version}.tsv"
        relative = f"dna_vocab/{vocab_version}/{name}"
        adjudications = bundle.artifacts_dir / "dna_vocab" / vocab_version / name
        if adjudications.is_file():
            rows = validator._read_tsv(adjudications, report, "adjudications")
            _report_app_range(
                report,
                f"{relative} title_id",
                _in_app_range(r.get("title_id") for r in rows or ()),
            )

    return report


def _report_app_range(report: ImportReport, source: str, offending: Iterable[Any]) -> None:
    ids = _in_app_range(offending)
    if not ids:
        return
    report.fail(
        "id-partition",
        f"{source} carries id {ids[0]}, which is inside the range this app mints from "
        f"(>= {APP_ID_MIN:,}). decision 162 partitions the two namespaces so a title the "
        "household acquired and a title the corpus exported can never be the same id; a bundle "
        "reaching across the boundary claims ids this install owns",
        source=source, floor=APP_ID_MIN, ids=ids,
    )


def refuse_on_path(bundle: Bundle, report: ImportReport) -> None:
    """What is true of the PATH the operator named, before anything is said about the bundle.

    Replays `Bundle.open`'s findings and refuses a path missing `BUNDLE.json` or `artifacts/`.
    Both validate callers ask this first, so the path is judged before the install.
    """
    # Replayed first so "the archive inside was opened" heads the report.
    for severity, message in bundle.open_findings:
        (report.fail if severity == "fail" else report.note)("bundle", message)
    if not report.ok:
        return
    if not (bundle.root / "BUNDLE.json").is_file() and not bundle.artifacts_dir.is_dir():
        report.fail(
            "bundle",
            f"{bundle.root} holds no BUNDLE.json and no artifacts/ - it is not a bundle. A bundle "
            "is the directory the corpus exported, a .tar or a .tar.zst of it, or the directory "
            "holding exactly one of those",
        )
    elif not bundle.artifacts_dir.is_dir():
        # Half of §4.3's two things is an incomplete copy, not a bundle; say so rather than letting the
        # install-state pass misdiagnose it.
        report.fail(
            "bundle",
            f"{bundle.root} holds a BUNDLE.json and no artifacts/ - section 4.3 makes both of "
            "them things every bundle carries, so this is a partial copy rather than a bundle. "
            "Re-copy or re-extract it and import it again",
        )


def validate(
    bundle: Bundle, *, spine: validator.Spine | None = None,
    active_coverage: set[int] | None = None,
) -> ImportReport:
    """Validate without writing anything. This is the wizard's and the Data tab's first step.

    `spine` and `active_coverage` come from `validate_for_install`; synchronous so fixture tests can
    validate a bundle with no install behind them.
    """
    report = ImportReport(bundle_version=bundle.version)
    refuse_on_path(bundle, report)
    if not report.ok:
        return report

    # The shipped hashes, first: every later rule assumes the bytes are intact.
    validator._verify_bundle_files(bundle.root, report)

    # Decision 162's boundary stops the report: nothing else about ids can be believed past it.
    validate_id_partition(bundle, report)
    if not report.ok:
        return report

    if bundle.content_db is None:
        # Decision 162: a bundle with no content.sqlite is a models-only re-import, not broken.
        report.note(
            "bundle",
            "models-only bundle (decision 162): no content.sqlite, so no content is loaded and "
            "§10's rebuild set runs against the existing spine",
        )
    else:
        db = sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
        db.text_factory = str          # rule 8: UTF-8 in, UTF-8 out, no cleaning
        try:
            validator.validate_content(db, report)
            _note_series_runtime(db, report)
        finally:
            db.close()

        if bundle.reviews_db is None:
            report.warn(
                "bundle",
                "reviews.sqlite absent — future re-extraction and text embedding will have no "
                "bodies",
            )

    if bundle.reviews_db is not None:
        # Rule 7 applies to both databases.
        rdb = sqlite3.connect(f"file:{bundle.reviews_db}?mode=ro", uri=True)
        rdb.text_factory = str
        try:
            validator.validate_reviews(rdb, report)
        finally:
            rdb.close()

    # Asked here too so the refusal is reachable before anything is staged; the in-transaction call
    # is the backstop.
    validator.compare_table_counts(bundle.root, report)

    validator.validate_artifacts(
        bundle.artifacts_dir, report, spine=spine, active_coverage=active_coverage
    )

    # Decision 247's header check on every bundle kind, before staging.
    corrections = bundle.artifacts_dir / "corrections_v1.tsv"
    if corrections.is_file():
        dna_loader.parse_corrections(corrections, report)

    # Restores `Bundle.open`'s answer when `validate_artifacts` returned early.
    report.vocabulary_version = bundle.vocabulary_version or report.vocabulary_version
    return report


def _note_series_runtime(db: sqlite3.Connection, report: ImportReport) -> None:
    """Report the corpus series whose `runtime_min` is a total, not per episode (decision 192).

    Over 110 minutes per episode is a season total. Reported only; nothing divides it.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "title" not in tables:
        return
    columns = {r[1] for r in db.execute('PRAGMA table_info("title")')}
    if not {"kind", "runtime_min"} <= columns:
        return
    total_shaped = db.execute(
        "SELECT count(*) FROM title WHERE kind = 'series' AND runtime_min >= 110"
    ).fetchone()[0]
    if not total_shaped:
        return
    report.note(
        "runtime-semantics",
        f"{total_shaped:,} series carry runtime_min >= 110, which is a season or series total "
        "rather than minutes per episode; the column is imported as shipped and nothing "
        "derives per-episode minutes from it (decision 192)",
        series=total_shaped, threshold=110,
    )


def _refuse_self_staging(bundle: Bundle, artifacts_root: Path, report: ImportReport) -> bool:
    """Refuse a bundle that lives inside the tree its own import is about to delete, or vice versa.

    A predicate so the destructive `rmtree` can ask it directly, as well as `validate_for_install`.
    """
    root = bundle.root.resolve()
    staged = (artifacts_root / bundle.version).resolve()
    if root == staged or root.is_relative_to(staged) or staged.is_relative_to(root):
        report.fail(
            "bundle",
            f"the bundle at {root} is inside the artifacts tree this import stages into "
            f"({staged}), so staging it would delete it - copy or unpack it under the import "
            "directory and import it from there",
            bundle=str(root), staged=str(staged),
        )
        return True
    return False


def _needs_restage(artifacts_root: Path, version: str) -> bool:
    """Is the ACTIVE version's staged tree absent, or present without `backbone.npz`?

    A half-copied directory must not block the restage that repairs it (decision 258).
    """
    from spielplan.scoring.backbone import BACKBONE_FILE

    root = artifacts_root / version
    return not root.is_dir() or not (root / BACKBONE_FILE).is_file()


async def refuse_on_install_state(
    conn: asyncpg.Connection, bundle: Bundle, report: ImportReport,
    artifacts_root: Path | None = None,
) -> ImportReport:
    """The refusals that are facts about the install rather than the bundle, asked before anything.

    `artifacts_root` is optional because fixture pre-flights have no install.
    """
    # A restage (decision 253): the active row's directory is gone, and this import repairs it.
    already = await conn.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = $1", bundle.version
    )
    restaging = (
        already == "active"
        and artifacts_root is not None
        and bundle.version != "unknown"
        and _needs_restage(artifacts_root, bundle.version)
    )
    if restaging:
        report.note(
            "restage",
            f"the artifacts for the active bundle {bundle.version} are missing or incomplete "
            f"under {artifacts_root}, so this import restages them instead of being refused as a "
            "re-import",
        )
    elif already == "active":
        # Checked at validation so the operator sees it before committing; `import_bundle` checks again.
        report.fail("bundle", f"bundle {bundle.version} is already the active bundle")

    if artifacts_root is not None and bundle.version != "unknown":
        _refuse_self_staging(bundle, artifacts_root, report)

    # The staging directory must be writable, asked at the decision point (C10.2).
    if artifacts_root is not None and (problem := storage.writable(artifacts_root)) is not None:
        report.fail(
            "stage",
            "the artifacts cannot be staged, so this import cannot run: "
            + str(storage.refusal({"artifacts": problem})),
            path=str(artifacts_root),
        )

    # Same tie-break as `0015_seed.sql`: the oldest bundle is the seed.
    seeded = await conn.fetchval(
        "SELECT version FROM artifact_bundle WHERE kind = 'seed' "
        "ORDER BY imported_at, version LIMIT 1"
    )
    if bundle.content_db is None:
        # Decision 162: content first, then models; a model bundle on an empty install must fail loudly.
        titles = await conn.fetchval("SELECT count(*) FROM title")
        if not titles:
            report.fail(
                "ordering",
                "this is a models-only bundle and the install has no content: restore or seed "
                "movie data first, then load the model bundle (decision 162). §10's rebuild set "
                "re-places every title against the staged basis, and there are no titles",
            )
        if seeded is not None and bundle.version == seeded and not restaging:
            # A model bundle exported under the seed's version would rewrite the seed row's kind.
            report.fail(
                "seed-once",
                f"bundle version {bundle.version!r} is the version this install's content seed "
                "was imported under, and this bundle carries no content. Importing it would "
                "rewrite the seed's `artifact_bundle` row and erase the only record that movie "
                "data was ever seeded (decision 162) — export the model bundle under its own "
                "version string",
                seeded=seeded,
            )
    elif seeded is not None and not restaging:
        # Not on a restage: the row being repaired IS the seed row.
        report.fail(
            "seed-once",
            f"movie data was already seeded by bundle {seeded!r} (decision 162: content is "
            "exported once and imported once; every later title is acquired by this app). A "
            "second content import would upsert the corpus's rows over ids this install now "
            "owns — re-import the models only, or restore movie data from a backup",
            seeded=seeded,
        )

    # Decision 163: the newest `dna_vocabulary` row is the active one.
    active_vocab = await conn.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC LIMIT 1"
    )
    if active_vocab and bundle.vocabulary_version is None:
        # Decision 256: a bundle declaring no vocabulary is refused rather than skipping decision 163.
        report.fail(
            "vocabulary-migration",
            "this bundle declares no DNA vocabulary version and the install is on "
            f"{active_vocab!r}, so decision 163's comparison cannot be made — export the bundle "
            "with `vocabulary_version` in BUNDLE.json, or ship its `dna_vocab/<version>/` tree",
            active=active_vocab, bundle=None,
        )
    elif active_vocab and bundle.vocabulary_version != active_vocab:
        report.fail(
            "vocabulary-migration",
            f"bundle carries DNA vocabulary {bundle.vocabulary_version!r} and this install is on "
            f"{active_vocab!r}; the {active_vocab} -> {bundle.vocabulary_version} vocabulary "
            "migration does not exist yet (decision 163: a vocabulary change is a fundamental "
            "data migration, not a bundle swap). Importing it would leave dna_tag and "
            "dna_projected on the old version while the feature builder filters on the new one, "
            "so both DNA blocks would be empty for every title and nothing in the read path "
            "would call that an error",
            active=active_vocab, bundle=bundle.vocabulary_version,
        )
    return report


async def installed_spine(conn: asyncpg.Connection) -> validator.Spine:
    """The install's own `title` rows, in the same shape as the bundle-side spine."""
    rows = await conn.fetch("SELECT id, kind, imdb_id, tmdb_id, name FROM title")
    return {r["id"]: (r["kind"], r["imdb_id"], r["tmdb_id"], r["name"]) for r in rows}


async def active_backbone_coverage(
    conn: asyncpg.Connection, artifacts_root: Path | None
) -> set[int] | None:
    """Decision 248: which of this install's bundle titles the ACTIVE backbone covers.

    Only `origin = 'bundle'`. None when there is no active bundle, files, or backbone.
    """
    if artifacts_root is None:
        return None
    from spielplan.scoring import backbone as bb

    store = await ArtifactStore.load_active(conn, artifacts_root)
    if store.is_empty:
        return None
    covered = bb.load_for(store).title_ids
    if covered is None:
        return None
    installed = {
        r["id"] for r in await conn.fetch("SELECT id FROM title WHERE origin = 'bundle'")
    }
    return {int(t) for t in covered.tolist()} & installed


async def validate_for_install(
    conn: asyncpg.Connection, bundle: Bundle, artifacts_root: Path | None = None
) -> ImportReport:
    """§10 step 1, with the install in the picture. Writes nothing.

    Every refusal an import can raise must be reachable here, or the operator reads "ok" and is
    refused after committing.
    """
    # The path is judged first, in its own report: the install-state pass short-circuits and would
    # otherwise misdiagnose a path mistake.
    path_state = ImportReport(
        bundle_version=bundle.version, vocabulary_version=bundle.vocabulary_version
    )
    refuse_on_path(bundle, path_state)
    if not path_state.ok:
        return path_state
    install_state = ImportReport(
        bundle_version=bundle.version, vocabulary_version=bundle.vocabulary_version
    )
    await refuse_on_install_state(conn, bundle, install_state, artifacts_root)
    if not install_state.ok:
        # A bundle that cannot be imported here at all gets the one actionable line.
        return install_state
    spine = None if bundle.content_db is not None else await installed_spine(conn)
    coverage = await active_backbone_coverage(conn, artifacts_root)
    # Off the event loop (decision 287): validation hashes 1 GB and builds the Cold Tower, and a
    # blocked loop fails `/api/health`. A thread, since hashlib, sqlite3 and torch release the GIL.
    report = await asyncio.to_thread(validate, bundle, spine=spine, active_coverage=coverage)
    # Install-state notes lead the report: the findings below are read under them.
    report.findings[:0] = install_state.findings
    # Only on the paths that can stage and flip.
    validator.validate_hyperparams(bundle.artifacts_dir, report)
    return report


async def position_id_sequences(conn: asyncpg.Connection, report: ImportReport) -> None:
    """Decision 162: the seed import positions the mint; a migration cannot know the corpus maximum.

    `GREATEST` so a smaller corpus never walks the mint back; `setval(..., false)` mints AT the value.
    """
    for sequence, table in (("title_id_seq", "title"), ("person_id_seq", "person")):
        mint_from = await conn.fetchval(
            f"SELECT setval('{sequence}', GREATEST(coalesce(max(id), 0) + 1, $1), false) "
            f"FROM {table}",
            APP_ID_MIN,
        )
        report.note(
            "id-partition",
            f"{sequence} positioned: the next {table} this app mints is {int(mint_from)}",
            sequence=sequence, mint_from=int(mint_from),
        )


# The advisory lock every bundle import takes. A hashed name, so no feature collides by arithmetic.
IMPORT_LOCK = "spielplan.bundle-import"


async def _drop_orphan_staging(
    conn: asyncpg.Connection, bundle: Bundle, staged: Path, report: ImportReport,
    *, wrote: bool = False,
) -> None:
    """After a failed import: remove the staged tree if no `artifact_bundle` row names it.

    Rows keep their files (decision 249), unless `wrote` says this import emptied and half-refilled it.
    """
    named = await conn.fetchval(
        "SELECT version FROM artifact_bundle WHERE version = $1", bundle.version
    )
    if not staged.exists():
        # Nothing was ever created: say so rather than claiming files remain.
        report.note(
            "rollback",
            "the import did not complete and no rows were written, and nothing was staged at "
            f"{staged}.",
        )
        return
    if named is not None and not wrote:
        report.note(
            "rollback",
            "the import did not complete and no rows were written. Artifacts staged at "
            f"{staged} remain on disk and are overwritten by the next import of this version.",
        )
        return
    shutil.rmtree(staged, ignore_errors=True)
    if staged.exists():
        # `ignore_errors` hides failures, so report what is actually still there.
        report.note(
            "rollback",
            "the import did not complete and no rows were written, and the artifacts it had "
            f"begun staging at {staged} could not be removed - delete that directory by hand "
            "before importing this version again.",
        )
        return
    report.note(
        "rollback",
        "the import did not complete and no rows were written, and the artifacts staged at "
        f"{staged} were removed: " + (
            "they are half of a copy this import wrote, and half a bundle is neither one this "
            "install can serve nor one a restore can roll back to."
            if named is not None else
            "no artifact_bundle row names that version, so nothing else would ever have found "
            "them."
        ),
    )


async def import_bundle(
    conn: asyncpg.Connection, bundle: Bundle, artifacts_root: Path, *, activate: bool = True
) -> ImportReport:
    """Validate, load, stage the artifacts, then flip the active row — all in one transaction
    apart from the file copy, which is done before the flip so a failed copy cannot leave the
    DB pointing at files that are not there.
    """
    # One import at a time. Session-level (the rmtree/copytree run before the transaction), try rather
    # than wait (a second import is a mistake), released in `finally`.
    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", IMPORT_LOCK):
        refusal = ImportReport(
            bundle_version=bundle.version, vocabulary_version=bundle.vocabulary_version
        )
        refusal.fail(
            "import",
            "another import is already running on this install - wait for it to finish and read "
            "/api/admin/bundle/state",
        )
        return refusal
    try:
        report = await validate_for_install(conn, bundle, artifacts_root)
        if not report.ok:
            return report

        # Stage the artifacts BEFORE touching the DB (§10 swap sequence step 2).
        if bundle.version == "unknown":
            report.fail(
                "bundle",
                "BUNDLE.json at the bundle root has no usable `bundle_version` — it names the "
                "artifact directory, so it must be a plain [A-Za-z0-9._-] token",
            )
            return report
        staged = (artifacts_root / bundle.version).resolve()
        if not staged.is_relative_to(artifacts_root.resolve()):
            report.fail("bundle", f"bundle version {bundle.version!r} escapes the artifacts root")
            return report
        if _refuse_self_staging(bundle, artifacts_root, report):
            # A7 again, beside the `rmtree` it protects.
            return report

        # `kind` is read so a restage keeps it: the seed row's kind is the only record content was seeded.
        existing = await conn.fetchrow(
            "SELECT state, kind FROM artifact_bundle WHERE version = $1", bundle.version
        )
        already = existing["state"] if existing is not None else None
        # Decision 253's restage: an active row with no directory.
        restaging = already == "active"
        if restaging and not _needs_restage(artifacts_root, bundle.version):
            report.fail("bundle", f"bundle {bundle.version} is already the active bundle")
            return report

        # The destructive filesystem steps get their own guard. `emptied` and `existed` tell
        # `_drop_orphan_staging` whose tree is on disk after a failed copy.
        existed = emptied = False
        try:
            existed = staged.exists()
            if existed:
                shutil.rmtree(staged)
            emptied = True
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundle.artifacts_dir, staged)
        except OSError as exc:
            report.fail(
                "stage",
                f"the artifacts could not be staged to {staged}: {type(exc).__name__}: {exc}. "
                "Nothing was written to the database and no bundle was flipped; check the free "
                "space and the ownership of the artifacts directory",
            )
            if existed and emptied:
                report.fail(
                    "stage",
                    f"the copy of {bundle.version} that was staged at {staged} was removed to "
                    "make room for this one and the replacement did not complete, so that "
                    "version's artifacts are no longer on disk: import this bundle again once "
                    "the cause above is cleared, or restore that directory from a backup",
                )
            await _drop_orphan_staging(conn, bundle, staged, report, wrote=emptied)
            return report
        if restaging:
            report.note(
                "restage",
                f"restaged: artifacts were missing for the active bundle {bundle.version} and have "
                f"been copied back to {staged}; §10's rebuild set runs again against them",
            )
        else:
            report.note("stage", f"artifacts staged to {staged}")

        # Decision 247 guard 3: one vocabulary for the ledgers, the tiers and the row. None is legal
        # (§3.1,
        # decision 262).
        vocabulary = report.vocabulary_version

        db = (
            # A restage never reloads content.
            None if bundle.content_db is None or restaging
            else sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
        )
        try:
            async with conn.transaction():
                # Decision 162: a models-only re-import loads no content.
                if db is not None:
                    db.text_factory = str
                    # The loader needs the root: `BUNDLE.json` carries the source priority.
                    await content_loader.load_content(conn, db, report, bundle_root=bundle.root)
                    if not report.ok:
                        # `load_content` returns (not raises) on shape refusals; roll back before later
                        # loaders hit FKs.
                        raise _Rollback(report)

                    # Counted apart, never summed (§4.1 rule 1).
                    tagged_rows = report.table_counts.get("dna_tag") or 0
                    projected_rows = report.table_counts.get("dna_projected") or 0
                    if vocabulary is None and (tagged_rows or projected_rows):
                        # Decision 256: no code path invents a vocabulary version. Backstop for the
                        # validator's refusal.
                        report.fail(
                            "vocabulary",
                            f"this bundle ships {tagged_rows:,} dna_tag and {projected_rows:,} "
                            "dna_projected row(s) and names no vocabulary version: §4.3 names it "
                            "by the `dna_vocab/<version>/` directory and BUNDLE.json may declare "
                            "it, and this import will not invent one",
                            dna_tag=tagged_rows, dna_projected=projected_rows,
                        )
                        raise _Rollback(report)
                    if vocabulary is not None:
                        vocab_dir = bundle.artifacts_dir / "dna_vocab" / vocabulary
                        if vocab_dir.is_dir():
                            await dna_loader.load_vocabulary(conn, vocab_dir, vocabulary, report)
                    else:
                        # Decision 262: NULL, and no `dna_vocabulary` row either.
                        report.note(
                            "vocabulary",
                            "this bundle ships no DNA rows and names no vocabulary version, so "
                            "the naming layer is empty (§3.1) and the artifact_bundle row "
                            "records no vocabulary",
                        )
                    # Rule 1: two tiers, two calls, even with no vocabulary, so the missing-table refusals
                    # still fire.
                    await dna_loader.load_tags(conn, db, vocabulary, report)
                    await dna_loader.load_projected(conn, db, vocabulary, report)

                    await dna_loader.load_corrections(
                        conn, bundle.artifacts_dir / "corrections_v1.tsv", report
                    )
                    await dna_loader.load_seed_list(
                        conn, bundle.artifacts_dir / "seed_list.json", report
                    )

                    # Rule 8's mojibake repair happens here and nowhere else.
                    if bundle.reviews_db is not None:
                        rdb = sqlite3.connect(f"file:{bundle.reviews_db}?mode=ro", uri=True)
                        rdb.text_factory = str
                        try:
                            await review_loader.load_reviews(conn, rdb, report)
                        finally:
                            rdb.close()
                else:
                    # Decision 247: the curated ledgers load on every import; never the content tiers
                    # (decision 162).
                    await dna_loader.load_corrections(
                        conn, bundle.artifacts_dir / "corrections_v1.tsv", report
                    )
                    await dna_loader.load_seed_list(
                        conn, bundle.artifacts_dir / "seed_list.json", report
                    )
                    if vocabulary is not None:
                        vocab_dir = bundle.artifacts_dir / "dna_vocab" / vocabulary
                        # Decisions 265 and 266: no vocabulary row (or no tree) for the ledgers to
                        # reference, so skip them
                        # with a line; a refusal here could never be satisfied.
                        installed_vocab = await conn.fetchval(
                            "SELECT version FROM dna_vocabulary WHERE version = $1", vocabulary
                        )
                        if not vocab_dir.is_dir():
                            report.warn(
                                "vocabulary",
                                f"this bundle names DNA vocabulary {vocabulary!r} and ships no "
                                f"dna_vocab/{vocabulary}/ tree, so it carries neither the "
                                "adjudications ledger nor any axis definition: the ones already "
                                "installed are left in place and not re-applied (decision 266)",
                                version=vocabulary,
                            )
                        elif installed_vocab is None:
                            report.warn(
                                "vocabulary",
                                f"the curated DNA ledgers in dna_vocab/{vocabulary}/ name a "
                                "vocabulary this install has no row for, so they are not applied "
                                "and nothing installed changes: section 3.1's empty naming layer "
                                "is filled by a content import, and decision 162 allows one of "
                                "those (decision 265)",
                                version=vocabulary,
                            )
                        else:
                            await dna_loader.load_adjudications(
                                conn, vocab_dir, vocabulary, report
                            )
                            # `load_axes` reads `dna_facet` itself on a models-only bundle (decision 173).
                            await dna_loader.load_axes(conn, vocab_dir, vocabulary, report)

                # Inside the transaction, so a count mismatch rolls the load back.
                validator.compare_table_counts(bundle.root, report)

                if not report.ok:
                    raise _Rollback(report)

                if db is not None:
                    await position_id_sequences(conn, report)

                await conn.execute(
                    """
                    INSERT INTO artifact_bundle
                           (version, manifest, report, state, kind, vocabulary_version)
                    VALUES ($1, $2, $3, 'validated', $4, $5)
                    ON CONFLICT (version) DO UPDATE
                      SET manifest = EXCLUDED.manifest, report = EXCLUDED.report,
                          state = 'validated', imported_at = now(),
                          kind = EXCLUDED.kind, vocabulary_version = EXCLUDED.vocabulary_version
                    """,
                    bundle.version,
                    _manifest_of(bundle),
                    report.as_dict(),
                    # The stored kind on a restage, never the bundle's: `kind` is the record of seeding
                    # (decision 162).
                    existing["kind"] if restaging and existing is not None else bundle.kind,
                    # Never NULL after a successful import, except decision 262's empty naming layer.
                    vocabulary,
                )
                if activate:
                    # Before the flip, so a failed rebuild takes the import down with it.
                    steps = await placement.run_rebuild(
                        conn, ArtifactStore.open(staged, bundle.version), bundle.version,
                        fold_in=_rebuild_fold_in,
                        blend_weights=_rebuild_blend_weights,
                        ledger_refit=_rebuild_ledger_refit,
                    )
                    for step in steps:
                        report.note("rebuild", f"{step['title']}: {step.get('result', step)}")

                    # §10: transactionally flip. The partial unique index guarantees one active row.
                    already_active = await conn.fetchval(
                        "SELECT version FROM artifact_bundle WHERE state = 'active'"
                    )
                    await conn.execute(
                        "UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active'"
                    )
                    await conn.execute(
                        "UPDATE artifact_bundle SET state = 'active', activated_at = now() "
                        "WHERE version = $1",
                        bundle.version,
                    )
                    # Records what was superseded; read above the UPDATEs, which would otherwise show this
                    # bundle.
                    report.note(
                        "swap",
                        "artifact_bundle flipped to active — every fitted number is expressed in "
                        "this basis from now on, and the backend and the worker load it without a "
                        "restart"
                        + (f"; it supersedes {already_active}" if already_active else ""),
                        superseded=already_active,
                    )
                    report.note(
                        "rebuild-set", "recomputed against the staged bundle: " + "; ".join(REBUILD_SET)
                    )
                    # Store the whole report, last in the transaction; the Data tab renders only this row.
                    await conn.execute(
                        "UPDATE artifact_bundle SET report = $2 WHERE version = $1",
                        bundle.version, report.as_dict(),
                    )
        except _Rollback:
            # The copy predates the transaction, so say which is which.
            await _drop_orphan_staging(conn, bundle, staged, report)
        except (asyncpg.PostgresError, sqlite3.DatabaseError, OSError) as exc:
            # The database's own refusals as a report line; not `except Exception`, so bugs still surface.
            report.fail(
                "import",
                f"the import stopped on {type(exc).__name__}: {exc}. Nothing was written - the "
                "transaction rolled back - and no rule in this importer named this refusal first",
            )
            await _drop_orphan_staging(conn, bundle, staged, report)
        except BaseException:
            # Cancellation at the job budget (`CancelledError` is not an `Exception`): drop the tree, log
            # what
            # `_drop_orphan_staging` found (no report is returned on this exit), and re-raise.
            with contextlib.suppress(Exception):
                before = len(report.findings)
                await _drop_orphan_staging(conn, bundle, staged, report)
                for finding in report.findings[before:]:
                    log.warning(
                        "bundle import %s: %s: %s", bundle.version, finding.rule, finding.message
                    )
            raise
        finally:
            if db is not None:
                db.close()

        if report.ok:
            _clean_unpacked(bundle, report)
        return report
    finally:
        # Suppressed: housekeeping must not fail a committed import. The pool's reset unlocks anyway.
        with contextlib.suppress(Exception):
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", IMPORT_LOCK)


def _clean_unpacked(bundle: Bundle, report: ImportReport) -> None:
    """Delete the tree `_unpack` extracted, once the import that needed it has committed.

    Only after a commit (a failed import is when the operator retries) and only for trees this module
    extracted. The note reports what was actually removed.
    """
    unpacked = bundle.root if bundle.root.name.startswith(_UNPACKED) else bundle.root.parent
    if not unpacked.name.startswith(_UNPACKED):
        return
    shutil.rmtree(unpacked, ignore_errors=True)
    if unpacked.exists():
        report.note(
            "cleanup",
            f"could not remove the unpacked bundle tree at {unpacked} - delete it by hand",
        )
    else:
        report.note("cleanup", f"removed the unpacked bundle tree at {unpacked}")


def _manifest_of(bundle: Bundle) -> dict:
    """What `artifact_bundle.manifest` records: BUNDLE.json, the bundle's own identity record."""
    return _identity(bundle.root)


class _Rollback(Exception):
    def __init__(self, report: ImportReport) -> None:
        super().__init__("import validation failed")
        self.report = report
