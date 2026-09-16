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

from spielplan.importer import dna as dna_loader
from spielplan.importer import load as content_loader
from spielplan.importer import reviews as review_loader
from spielplan.importer import validate as validator
from spielplan.importer import vocab as vocab_reader
from spielplan.importer.report import ImportReport

# Re-exported, not re-defined. The token rule moved to `validate.py` because that is where the
# operator meets it: `Bundle.open` mapped an unsafe token to the literal "unknown" and the
# validator then reported a clean bundle under a version the identity record does not name, so
# `import_bundle` refused after the operator had committed. The name stays importable from here
# because `bundle.safe_version` is what four call sites outside this package already ask for, and
# a rename would be a diff about spelling in a milestone whose whole subject is refusals.
# [M4.14 step B7, finding 2.11]
from spielplan.importer.validate import safe_version
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile as placement

# The FIRST logger in this package, and it is used in exactly one place: the `except
# BaseException` arm of `import_bundle`. Every other line this package writes goes into
# `ImportReport`, which is the right channel because §10's report is what the operator reads -
# but that arm is the one exit whose report is never returned. It re-raises, `worker
# ._bundle_import`'s crash arm builds a FRESH `ImportReport`, and `_reap_abandoned_import` builds
# another, so what `_drop_orphan_staging` concluded about the disk on an abandonment at the job's
# budget was written into an object nobody reads. The could-not-remove case is the one that
# matters: `ignore_errors=True` swallows EACCES, EBUSY and ENOTEMPTY alike, and a staged tree
# that survives with no `artifact_bundle` row naming it is a directory the next import of that
# version meets and did not write. [M4.14 cycle 4, M414-C4-REF-06]
log = logging.getLogger("spielplan.importer")

# §10: the rebuild set. "Everything expressed in the old Backbone's basis is garbage against a
# new one." Defined once, in the module that runs it — M0 kept a copy here to report from, and
# two hand-maintained copies of a four-item list is how a fifth item lands in one of them.
REBUILD_SET = placement.REBUILD_SET

# decision 162: "Spielplan mints from a range disjoint from the corpus's, below 2^31 because
# every id column is `integer` and `ledger_fit.title_ids` is `int32`." One number, three
# statements of it: `0015_seed.sql` starts both sequences here, the refusal below is this
# boundary, and the seed import positions the mint above it. The corpus's own sqlite_sequence
# reads 21442, so the two ranges are a billion apart rather than one id apart.
APP_ID_MIN = 1_000_000_000

# The artifacts that carry corpus `title.id` and travel in `artifacts/` — so a models-only
# re-import can reach into the app's range long after content stopped arriving. None of them can
# detect it on their own: `backbone.py` checks only that the ids ascend, and the other three are
# read by id with no cross-check at all.
_ID_BEARING_NPZ = ("backbone.npz", "review_text_emb.npz")


# §10's first three steps belong to the scoring and Ledger lenses; `placement.rebuild_plan`
# takes them injected so the rebuild set is defined by the spec sentence rather than by which
# module happens to import which. Each is `async (conn, store, version) -> dict`, and the dict
# lands in that step's row of the import report.


async def _rebuild_fold_in(conn: Any, store: Any, version: str) -> dict[str, Any]:
    """§10 step 1 and 2, in one pass.

    §10 lists "user fold-in vectors" and "per-label-count blend weights" as two items, and they
    are two things — but one cross-validated fit produces both, and running it twice would
    double the work to make the report's shape match the sentence's. So this step does the fit
    and step 2 reports the weights it wrote.
    """
    from spielplan.scoring import backbone as bb
    from spielplan.scoring import foldin

    report = await foldin.run(
        conn, bb.load_for(store), bundle_version=version, only_stale=False, with_priors=True
    )
    return report.as_dict()


async def _rebuild_blend_weights(conn: Any, _store: Any, version: str) -> dict[str, Any]:
    """§10 step 2. Reports the β the step-1 fit produced, per user and kind.

    §5.1's β is not a separate computation — it is chosen by the same cross-validation that
    produces the vector. Recomputing it here would either repeat step 1 or invent a second
    estimator, and a second estimator for a number that already has one is how two parts of an
    app come to disagree about the same user.
    """
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
    """§10 step 3: "a **full** Personal Ledger MAP refit" — full history, every user and kind.

    `version` is the STAGED bundle, and this function is the reason it is threaded at all. It
    ignored the argument entirely: `standard_embeddings` passed no version, so
    `placement_embeddings` took its `$2 IS NULL` branch and joined `b.state = 'active'` - which
    during a pre-flip rebuild is the OUTGOING bundle - and `refit_all` stamped the fit from
    `active_bundle_version`, the outgoing version again. So §10's one step whose whole purpose is
    to re-express every fitted number in the NEW basis read the old placements and claimed the old
    version. Measured ||v_step3 - v_correct|| = 0.397 against ||v_correct|| = 0.782; and after the
    flip `load_cache` refused that fit on both its version and its digest, so the first tap per
    (user, kind) re-fitted on the request path against the still-old in-process Backbone and
    stamped THAT mixed-basis fit with the new version, which `load_cache` then trusted until the
    next nightly (||v_tap - v_correct|| = 0.643). One name, used twice. [M4.13, data-01]
    """
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    hp, _notes = load_hp(store)
    # Against the STAGED bundle's Backbone, not the active one: §10's whole point is that the
    # two bases are incompatible, and this refit exists to move every fitted number into the new
    # one. The placement source alone would fit every warm title at e = 0.
    #
    # All three statements of "which basis" now name one bundle - the Backbone `load_for(store)`
    # opens, the `title_placement` rows `bundle_version` selects, and the `ledger_fit` stamp - and
    # `run_rebuild` has already refused a store that is not the staged row (`assert_staged`), so
    # `store.version == version` here.
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
    # What `open` had to say before there was a report to say it in. Each entry is
    # `(severity, message)` and `validate` replays them under rule `bundle` as its first act.
    #
    # `Bundle.open` runs before `ImportReport` exists and its only other exit is an exception,
    # which `api/artifacts.py` turns into an HTTP 500 with no report attached at all - so a
    # refusal that has to be READ (decision 257's "several archives and no BUNDLE.json") cannot
    # be raised, and a fact that has to be SAID ("the directory you named holds one archive, and
    # that is what was opened") has nowhere else to go. An archive that will not open at all is
    # still `BundleOpenError`: there is no bundle for a report to be about.
    # [M4.14 step A5, decision 257, finding 2.6]
    open_findings: tuple[tuple[str, str], ...] = ()

    @property
    def kind(self) -> str:
        """§10's two kinds of import, under decision 162: content seeds once, models re-import.

        Which one a bundle is, is a property of what it carries rather than a flag an operator
        sets — an operator who can declare a content bundle to be "models only" can seed twice.
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
    """`BUNDLE.json` at the bundle root — the corpus's own record of what this bundle is.

    The version was read from `artifacts/manifest.json` until M4.5. §4.3 defines that file as
    the fitted 3-class cut-points and the shipped one carries nothing else, so every real bundle
    resolved to "unknown" — which `import_bundle` refuses outright, because the version names the
    staging directory. A missing or unparseable BUNDLE.json is not raised here: it lands as the
    same "no usable bundle_version" refusal, in the report, where the operator is standing.
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
    """The vocabulary this bundle carries. Decision 163's refusal has nothing to compare without
    it, and the shipped BUNDLE.json does not name one: the vocabulary's identity is the
    `dna_vocab/<version>/` directory the bundle ships (§4.3 names the directory, not a key).

    The listing that used to be inlined here was one of FOUR derivations of one fact, and they
    disagreed on the same tree: this one and `validate.py`'s copy both sorted lexicographically,
    so `v10` lost to `v2`, while `ArtifactStore.open` globbed FILES as well as directories and
    reported `vocab_version = 'zz_notes.txt'` off a tree this function called `v2`. The reading
    is now `vocab.version_of`, once, for all three readers; the BUNDLE.json key wins where a
    bundle declares one AND the tree does not contradict it, because a declaration is the
    corpus's statement and the directory is this app's inference from it.
    [M4.14 step B5, decision 256, finding 2.14]
    """
    declared = identity.get("vocabulary_version")
    declared = declared if isinstance(declared, str) and declared else None
    try:
        # Read whatever the bundle declares. Returning the declaration early skipped decision
        # 163's two-version refusal for exactly the bundles decision 256 asks the corpus to start
        # writing, and this is one of the two readers that can turn it into a line an operator
        # can act on. The declaration still wins as the ANSWER. [M4.14 cycle 1]
        derived = vocab_reader.version_of(artifacts_dir)
    except vocab_reader.VocabularyError as exc:
        # Decision 163 allows one vocabulary per bundle, so a tree holding two has no version to
        # name. Carried as a finding rather than raised: the operator can act on "this bundle
        # ships v1 and v2" and cannot act on a traceback, and `Bundle.open` has no report yet.
        findings.append(("fail", str(exc)))
        derived = None
    if declared and derived and declared != derived:
        # A tree with no answer to give was already a refusal; this is a bundle with TWO, and it
        # was the shape that passed. The declaration decided the answer and `version_of`'s reading
        # was computed and then dropped, so a models-only bundle retrained on v2 whose BUNDLE.json
        # said "v1" - the likeliest first value for that key, because decision 256's own refusal
        # coaches the operator to add it and the install's version is the number in front of them
        # - passed decision 163's comparison on the declaration, staged its v2 tree, and left
        # `ArtifactStore.open` deriving v2 from that staged directory while every `dna_tag` and
        # `dna_projected` row stayed on v1. That is decision 163's own stated catastrophe reached
        # through the door it guards: both DNA blocks empty for every title, and nothing in the
        # read path calling it an error. Neither value is preferred, because nothing here can say
        # which of the two is right - the bundle is what is wrong, and it is the corpus's to fix.
        # [M4.14 cycle 3, M414-C3-VOCAB-01, decisions 163 and 256]
        findings.append((
            "fail",
            f"BUNDLE.json declares DNA vocabulary {declared!r} and this bundle ships "
            f"dna_vocab/{derived}/ - section 4.3 names the vocabulary by the directory, so this "
            "bundle gives two answers and decision 163's comparison cannot be made against "
            "either; export it with the key and the tree naming one version",
        ))
    return declared or derived


# The prefix `_unpack` writes and `_clean_unpacked` deletes. One name, two users: a tree nothing
# can recognise is a tree nothing can clean, which is how it survived five milestones.
#
# Keyed on the archive's FULL NAME and no longer on `Path.stem`: `v1.tar` and `v1.tgz` are two
# archives and one stem, so the second one an operator offered was read as the first one's
# extraction - a different bundle, silently. [M4.7 dd10; M4.14 step A3, finding 2.5]
_UNPACKED = ".unpacked-"

# Where an extraction lives WHILE it is being written. A second prefix rather than a flag,
# because the whole rule this pair enforces is that `.unpacked-<name>/` is a COMPLETE extraction:
# nothing reads `.unpacking-`, so a tree left behind by a `kill -9` or by a container restart
# mid-extract - the two failures no `except` can cover - can never be mistaken for a bundle, and
# the next attempt at the same archive reclaims its bytes. [M4.14 step A3, finding 2.5]
_UNPACKING = ".unpacking-"

# One tar block. A tar is a sequence of 512-byte blocks and closes with two zero ones, which is
# what `_unpack` measures for: the number is the format's, not a tuning knob.
_BLOCK = 512


class BundleOpenError(ValueError):
    """The path the operator named is not an archive this app can open.

    Raised rather than reported, because `Bundle.open` runs before there is an `ImportReport` to
    write into and there is no bundle for a report to be about. §6.6 makes the Data tab where an
    operator meets the importer and `BundleImport.svelte` renders `err.message`, so this carries
    the sentence; `api/artifacts.py` maps it to 400.

    What it replaces, measured: `Bundle.open` treated every non-directory as an archive, so a
    `README.txt` reached `tarfile.open`, its `ReadError` escaped as an HTTP 500 rendered as a
    bare string, an empty `.unpacked-README/` was left behind - and the SECOND click was then
    diagnosed as "this is a models-only bundle and the install has no content", a decision-162
    refusal for a typo. [M4.14 step A3, finding 2.5]
    """


def _unpack(archive: Path, findings: list[tuple[str, str]]) -> Path:
    """Extract `archive` beside itself and return the bundle root inside the extraction.

    THE RULE: a `.unpacked-<name>/` directory is a COMPLETE extraction, always. `target.mkdir()`
    ran before `tarfile.open()` and nothing cleaned up on any exception, so every failure mode -
    a truncated transfer, a full disk, a traversal member `filter="data"` refuses after earlier
    members have landed, a backend restart - left a partial tree that the retry opened as a real
    bundle. Reproduced with the real corpus tar cut at 90%: the first `validate` raised, the
    second returned `ok=True` over a 332-of-436 MB `reviews.sqlite`. With the fixture cut inside
    `reviews.sqlite` the import returned 200, flipped the seed active and loaded zero review
    bodies behind a single warn. Decision 162 and `artifact_bundle_one_seed` then make that seed
    the household's only content import, forever.

    So: extract into `.unpacking-<name>/`, `rmtree` it on any exception, and `os.replace` it onto
    `.unpacked-<name>/` only once `extractall` has returned AND the archive is shown to have
    ended. Both paths are children of `archive.parent` by construction, so the rename is a rename
    and not a copy. A reused final tree is checked for `BUNDLE.json` before it is trusted - the
    one thing that distinguishes an extraction this rule wrote from one an older build left. No
    force-re-extract flag: the reuse is verified, not overridden. [M4.14 step A3, finding 2.5]

    "AND the archive is shown to have ended" is the half `extractall` cannot supply. `tarfile`
    swallows `EOFHeaderError`, `TruncatedHeaderError` and `EmptyHeaderError` at every offset but
    0 and returns from `next()` as though the archive had ended, so a truncation landing ON a
    512-byte member header - which is what a block-aligned short write produces: `dd`, `head -c`,
    ENOSPC on whole blocks, an interrupted `rsync --partial` - extracts SILENTLY. Reproduced on a
    fixture archive: 36 of 38 members landed, no exception, the partial tree was promoted, and
    the retry reused it because `BUNDLE.json` is the first member in the tar and therefore always
    survives. The tree read as a models-only bundle, so a first boot was told "restore or seed
    movie data first" - a decision-162 refusal for a broken transfer - on every attempt.

    So the end-of-archive marker is measured instead: a conforming tar closes with two zero
    blocks, and every truncation of this class leaves under one block past the last header the
    iteration read. One predicate over the whole class, needing no manifest and no second pass -
    `_verify_bundle_files`' 42 hashes cannot stand in for it, because `validate_for_install`
    returns an install-state refusal alone and on a first boot never reaches them.
    [M4.14 cycle 1, m414-c1-dim-refusals-01]
    """
    target = archive.parent / f"{_UNPACKED}{archive.name}"
    if target.is_dir():
        root = _single_child(target)
        if (root / "BUNDLE.json").is_file():
            # THE TREE IS NAMED, because every finding the report goes on to make about this
            # import names a path INSIDE it and nothing named the tree itself. A reuse is trusted
            # on `BUNDLE.json` alone - deliberately, and `_verify_bundle_files` is what catches
            # the rest - so anything that damaged the extraction while the archive stayed perfect
            # (a household clearing disk space, an interrupted rsync of the directory, an
            # antivirus quarantine) read back as "the bundle is missing files", identically on
            # every attempt, with no sentence anywhere saying a second copy existed or where.
            # A note and not a removal: M4.7's dd10 rule is that a failed import keeps its tree
            # because that is exactly when the operator retries, and
            # `test_a_failed_import_keeps_its_unpacked_tree_for_the_retry` holds it.
            # [M4.14 cycle 2, m414-c2-refusals-06]
            findings.append((
                "note",
                f"{archive.name} was read from the extraction already at {target}, not from the "
                "archive: every path named below is inside that tree. Delete it to re-extract.",
            ))
            return root
        # A tree from before this rule, or one a crash left behind. The retry is exactly the
        # moment it has to stop being reused, and the archive it came from is right here, so it
        # is removed rather than reported: re-extracting is what the operator asked for.
        #
        # "THE ARCHIVE IS RIGHT HERE" IS A PREMISE, AND IT WAS ASSERTED RATHER THAN CHECKED. The
        # `archive.is_file()` test sat eight lines BELOW this `rmtree`, so an operator who
        # deleted the 1.04 GB archive to reclaim disk and retried lost the extraction as well and
        # was answered "<archive> is neither a bundle directory nor a file" - a sentence about a
        # path that named neither the second copy that had existed nor its destruction. Measured
        # on a 358 KB extraction: both copies gone, the directory empty, the message mentioning
        # only one of them. Checked HERE rather than hoisted above `if target.is_dir()`, which
        # would take M4.7's dd10 rule with it: an extraction that DOES carry BUNDLE.json is still
        # reused when the archive has been deleted, and
        # `test_a_failed_import_keeps_its_unpacked_tree_for_the_retry` holds exactly that.
        # [M4.14 cycle 4, M414-C4-REF-05]
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
        # Said out loud for the reason the reuse note one branch up is said out loud: a removal
        # nobody is told about is a gigabyte that leaves without a sentence, and on this shape -
        # an archive whose members land as more than one top-level entry - it happens on every
        # press. [M4.14 cycle 4, M414-C4-REF-05]
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
                # The DECOMPRESSED tail, read off the stream rather than measured against the
                # file: here `tar.offset` counts bytes of the expanded tar while
                # `archive.stat().st_size` is the compressed size, so the subtraction the
                # seekable branch makes is meaningless on this one. The iteration stopped having
                # read one block; a whole second one is the marker, and an exhausted stream
                # answers with nothing.
                complete = len(tar.fileobj.read(_BLOCK)) == _BLOCK
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(staging, filter="data")
                # `tar.offset` is where the block that ended the iteration began, and extraction
                # seeks the file object around afterwards - so the handle is seeked BACK to that
                # offset and the two blocks are read off the tar's own stream.
                #
                # Not `archive.stat().st_size - tar.offset`, which was the same subtraction the
                # streaming branch above already refuses to make: `tarfile.open(archive)` is
                # transparent-compression mode, so `tar.offset` counts bytes of the DECODED tar
                # while the file's size is the compressed one. For any gzip, bzip2 or xz stream
                # the difference is negative and a whole, valid archive was refused as "a
                # truncated or still-copying transfer ... Copy the archive again and retry" -
                # the one refusal in this importer an operator cannot satisfy, since every copy
                # answers the same. `api/artifacts._resolve` admits any name ending `.tar` and
                # `tarfile.is_tarfile` is true of all four, so `tar czf v20260828.tar <bundle>`
                # reaches here. Measured on one tree: plain +4608, gzip -577327, xz -578276.
                # The predicate is about the TAR rather than about how it was carried, which is
                # what makes it one rule for both branches. [M4.14 cycle 2, m414-c2-refusals-04]
                tar.fileobj.seek(tar.offset)
                complete = len(tar.fileobj.read(2 * _BLOCK)) == 2 * _BLOCK
        if not complete:
            raise BundleOpenError(
                f"{archive.name} has no end-of-archive marker - it is a truncated or still-"
                "copying transfer rather than a whole bundle, and nothing was extracted from "
                "it. Copy the archive again and retry."
            )
    except Exception:
        # Which exception ended the extraction changes nothing about what must not survive it,
        # so this catches the class rather than the four named members of it that were measured.
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
    """decision 257: a directory holding an archive and no BUNDLE.json IS a path to that archive.

    `api/artifacts.py` resolves an empty `path` to `cfg.import_dir`, and this module opened that
    directory as the bundle root - so the only layout the default understands is CI's, where
    `make_bundle` writes `BUNDLE.json` straight into `data/import`. README's first-boot line and
    the Data tab's own placeholder tell an operator to copy `v20260828.tar.zst` in there, and
    what they got back was `ok=false` carrying the decision-162 ordering refusal: a wrong-path
    diagnosis dressed as a data-modelling one, on the first screen of a first boot.

    One archive is opened AND SAID SO, because a path that silently means a different path is the
    next report nobody can read. Several is a refusal naming the count, not a guess: with two
    exports side by side there is no answer this module may invent on the operator's behalf, and
    the fix is one word of theirs. No new config and no allow-list - the rule is the directory the
    route already resolves. [M4.14 steps A5/A6, decision 257, finding 2.6]
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
    """decision 162: refuse a bundle that reaches into the range this app mints from.

    Partitioning the namespace makes the collision impossible by construction rather than
    contingent on the corpus standing still — but only if the boundary is asserted, and asserted
    on the *artifacts* too. `backbone.npz`, `review_text_emb.npz`, `seed_list.json`,
    `corrections_v1.tsv` and `dna_vocab/<version>/adjudications_v1.tsv` are all keyed by corpus
    `title.id` and all travel in `artifacts/`, so a
    models-only re-import can carry an id naming a title this household acquired long after
    content stopped arriving. Nothing downstream would notice: `Backbone.open` would resolve the
    row, the gate would stamp it warm, and §5.1 would score one film against another film's
    embedding — plausible numbers for the wrong film, which is the failure this whole partition
    exists to make impossible.

    The offending id is named because the operator's next move depends on it: an id in this
    range means the exporter minted into the app's half, and they need to know which one.
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
        # Through `validate._read_tsv`, which is what that function's own contract has said this
        # call site does since M4.14 step B2 and what it did not do: it opened the file itself
        # with `encoding="utf-8"` and no handler, and it runs FIRST inside `validate()` - ahead
        # of the corrections parse that does go through the handler - so one latin-1 byte in
        # either hand-edited ledger left `POST /validate` holding a `UnicodeDecodeError` with no
        # finding at all. No required columns: this pass needs `title_id` and reads it with
        # `.get`, and the header rule belongs to the ledger's own owner.
        # [M4.14 cycle 1, M414-REV-247-05]
        rows = validator._read_tsv(corrections, report, "corrections")
        _report_app_range(
            report,
            "corrections_v1.tsv title_id",
            _in_app_range(r.get("title_id") for r in rows or ()),
        )

    # The fifth id-bearing artifact, and it travels with the models like the other four:
    # `adjudications_v1.tsv` is keyed per TITLE (scope, title_id, term, action, …), not per term,
    # which is the whole reason the vocabulary layer was rewritten around it. Its `scope=global`
    # rows carry no title_id and `_in_app_range` drops them.
    #
    # The NAME follows the version, the way `dna.load_adjudications` builds it: decision 247's
    # guard 3 is "the version is the caller's and never a literal", and this boundary check held
    # the literal `adjudications_v1.tsv` inside a directory named by the version. At any
    # vocabulary but v1 the two read different files - this one finds nothing and passes in
    # silence while the loader loads the file that is there - so the fifth id-bearing artifact
    # decision 162's partition names would be the one artifact the partition is not asserted on.
    # [M4.14 cycle 3, decisions 162 and 247]
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

    Two statements, and they are the same kind: what `Bundle.open` had to say - decision 257's
    "several archives and no BUNDLE.json", the two-vocabulary refusal `_vocabulary_version`
    carries, and the note naming the archive that was opened inside the directory - and step A6's
    question, which is whether the thing at the end of the path is a bundle at all.

    A6: §4.3 makes `BUNDLE.json` and `artifacts/` the two things every bundle carries, and a
    directory with neither is an operator's typo - which arrived at `validate`'s models-only
    branch and came back as decision 162's ordering refusal, "restore or seed movie data first".
    That sentence is true of a real models-only bundle and meaningless about `/data/backups`, and
    it sent the reader looking at their install instead of at their path. A directory with ONE of
    the two is the same class arriving from the other side - a copy that did not finish, not a
    typo - and it reached the same two data-modelling refusals until cycle 3.
    [M4.14 step A6, decision 257, findings 2.5 and 2.6; cycle 3, M414-C3-VOCAB-02]

    A FUNCTION, because `validate_for_install` has to ask the same two questions BEFORE the
    install-state pass and `validate` is on the far side of that pass's short-circuit. Both
    callers ask them first and both stop on a failure, so the ordering is stated once: what the
    path IS precedes what the install is. [M4.14 cycle 2, m414-c2-refusals-02, M414-C2-VOCAB-02]
    """
    # Replayed first so that decision 257's "I opened the archive inside the directory you named"
    # is the line above every other line the report goes on to make about that archive.
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
        # HALF of section 4.3's two things is not a bundle either, and the sentence above refused
        # only a directory with neither. A path carrying BUNDLE.json and no `artifacts/` - an
        # rsync cut short, a tar extracted through a filter, a restore that copied the small files
        # first - went on to the install-state pass and was diagnosed there: a first boot answered
        # it with decision 162's "restore or seed movie data first" and a seeded install with
        # decision 256's "export the bundle with `vocabulary_version` in BUNDLE.json", an
        # instruction to the CORPUS about a bundle whose 205 MB of model files are simply not on
        # disk. Both are the data-modelling diagnosis of a copy that step A6 exists to delete, and
        # the one line naming the real cause - step B1's, over the 40 `artifacts/*` entries the
        # manifest still lists - is discarded unread by that pass's short-circuit.
        #
        # Nothing importable is refused by it: decision 251 makes `cold_tower.pt`,
        # `feature_contract.json` and `backbone.npz` validation failures when absent on both
        # bundle kinds, and all three live in `artifacts/`, so a bundle without that directory
        # could never have been imported under any reading. [M4.14 cycle 3, M414-C3-VOCAB-02]
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

    `spine` is the installed title rows, for the decision-162 identity check on a models-only
    bundle — which carries no spine of its own. `active_coverage` is decision 248's other half:
    the ids the ACTIVE backbone covers, so that coverage going BACKWARDS is refused where an id
    the install never seeded is only counted. Both come from `validate_for_install`; this
    signature stays synchronous because the pre-flight tools (`ops/devstub.py`, the fixture
    tests) validate a bundle with no install behind them.
    """
    report = ImportReport(bundle_version=bundle.version)
    refuse_on_path(bundle, report)
    if not report.ok:
        return report

    # B1: the 42 hashes the corpus ships and this importer read none of. Ahead of every rule
    # below, because all of them are statements about bytes nothing else has checked - a
    # `content.sqlite` truncated in transit is a bad bundle that every other rule reads as a
    # small one, and decision 162 plus `artifact_bundle_one_seed` make the seed it corrupts
    # unrepeatable. Measured at 1.45 s over v20260828's 42 files and 1.04 GB.
    # [M4.14 step B1, finding 2.4]
    validator._verify_bundle_files(bundle.root, report)

    # decision 162's boundary is checked first and stops the report when it is crossed. Every
    # other line of a migration report — the per-table counts, the shared-pair count, the
    # identity check — is a statement *about title ids*, and a bundle whose ids reach into the
    # app's range has none this install can believe. Same shape as the missing-content.sqlite
    # refusal this replaces: one line the operator can act on, rather than thirty about a
    # namespace that is not theirs.
    validate_id_partition(bundle, report)
    if not report.ok:
        return report

    if bundle.content_db is None:
        # decision 162: "movie data is exported once and imported once … the corpus supplies the
        # trained artifacts." A bundle with no content.sqlite is that re-import, not a broken
        # bundle. It was refused here until M4.5, which made the standing upstream unusable.
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
        # Rule 7 is a rule about the BUNDLE and it was being applied to half of one: `validate`
        # opened `content.sqlite` only, `reviews.sqlite` was first opened inside the load, and
        # the report's "no %_bak% / %_good tables present" note read as a statement about both.
        # §8 stage 5 re-extracts DNA from these bodies, so a stale `review_bak` here is precisely
        # the class rule 7 exists to catch. [M4.14 step B8, finding 2.22]
        rdb = sqlite3.connect(f"file:{bundle.reviews_db}?mode=ro", uri=True)
        rdb.text_factory = str
        try:
            validator.validate_reviews(rdb, report)
        finally:
            rdb.close()

    # B1's table-count comparison, asked HERE as well as inside the import transaction. It needs
    # nothing the import adds: `validate_content` above filled `report.table_counts` from the
    # same `content.sqlite`, and the `loaded:<target>` keys the loaders write afterwards are
    # deliberately not compared - so the answer is the same at either point, and asking it only
    # after the `rmtree` and `copytree` meant the Data tab said ok, the request answered 202, and
    # the operator learned minutes later from a polled job row that the bundle was refused by a
    # rule computable before the first of 205 MB moved. `validate_for_install`'s own contract is
    # that every refusal an import can raise is reachable here. The in-transaction call stays
    # where it is: it is the backstop for a tree that changed between the two.
    # [M4.14 cycle 1, m414-c1-dim-refusals-03]
    validator.compare_table_counts(bundle.root, report)

    validator.validate_artifacts(
        bundle.artifacts_dir, report, spine=spine, active_coverage=active_coverage
    )

    # decision 247's header check, on ANY bundle kind, before anything is staged. It was reached
    # only from `load_corrections`, which lives inside the seed branch of `import_bundle` - so a
    # models-only bundle carrying an invented header validated and imported with zero findings,
    # and the coverage row `data-rules-corrections-ledger-parsed-at-its-real-header` promised an
    # early warning that the only recurring kind of bundle could not receive. Parsed here rather
    # than re-implemented in `validate.py`: a second reading of one file is how the two come to
    # disagree about it. [M4.14 step C2, finding 2.15]
    corrections = bundle.artifacts_dir / "corrections_v1.tsv"
    if corrections.is_file():
        dna_loader.parse_corrections(corrections, report)

    # `validate_artifacts` makes the same two readings `Bundle.open` did - the BUNDLE.json key
    # through `_read_bundle_identity`, the tree through `vocab.version_of` - under the same
    # isinstance guard, so wherever it runs to the end the two answers agree by construction and
    # this line moves nothing. It is for that function's EARLY RETURNS: an `artifacts/manifest
    # .json` that is missing or unparseable stops it above its vocabulary branch, and the report
    # would then carry whatever BUNDLE.json declared and nothing the tree said. Restoring
    # `Bundle.open`'s answer here is what keeps the two readers from parting company on that exit.
    # The manifest KEY the line above this one used to argue against is gone: `ArtifactStore.open`
    # stopped preferring it this milestone, and §4.3 names the vocabulary by the directory, which
    # is `importer/vocab.py`'s whole subject. [§4.3, decision 256; M4.14 cycle 3, M414-C3-VOCAB-04]
    report.vocabulary_version = bundle.vocabulary_version or report.vocabulary_version
    return report


def _note_series_runtime(db: sqlite3.Connection, report: ImportReport) -> None:
    """§10's report names the one place `runtime_min` means two different things.

    126 corpus series carry a **total** runtime in the column `home/shelves.py:73-75` reads as
    minutes per episode (67 between 110 and 199, 59 at 200 or more: *Press Gang* 1290, *The Life
    & Times of Tim* 900). 110 is the threshold rather than a fitted cut because it is the point
    past which no plausible episode length survives — a two-hour episode is a special, and a
    21-hour one is a season total.

    Decision 192 makes this a report line and nothing else. `episode_count`/`season_count` stay
    unmapped, no migration adds them, and nothing in the app divides a total by an episode count
    to recover per-episode minutes: §4.1's "one block = one droppable source" puts that
    resolution corpus-side, and §6.0's card list does not name either column. What the app owes
    is that an operator can read the ambiguity in the migration report instead of discovering it
    on a card that says a series runs 21 hours an episode. [M4.9 finding 36, decision 192]
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
    """A7: refuse a bundle that lives inside the tree its own import is about to delete.

    `_resolve` accepts any path under `DATA_DIR`, and `DATA_DIR/artifacts` is under `DATA_DIR`.
    For a bundle at `/data/artifacts/v20260828/` - the one directory the Data tab names by
    version, so the obvious place to put one - `import_bundle` computed
    `staged = artifacts_root / bundle.version`, found it existing, `rmtree`d it, and deleted
    `content.sqlite`, `reviews.sqlite`, `BUNDLE.json` and `artifacts/` before copying the first
    byte; `copytree` then raised `FileNotFoundError` as an HTTP 500 and the gigabyte on the box
    was gone. Validation had passed. [M4.14 step A7, finding 2.8]

    Both directions of containment, because both are the same accident: the bundle inside the
    staging target is the reproduction above, and the staging target inside the bundle (a bundle
    root of `/data`, say) makes `copytree` a copy of a tree into itself. Written as a predicate
    so the destructive line in `import_bundle` can ask it directly: `validate_for_install` asks
    it so the Data tab refuses before Import is enabled, and the `rmtree` asks it again because a
    guard that protects an `rmtree` belongs beside the `rmtree`.
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
    """Is the ACTIVE version's staged tree absent, or there and holding no basis to serve from?

    D2's gate asked `.exists()`, and a directory is not a bundle. Any partly populated
    `/data/artifacts/<version>/` therefore closed the one repair this milestone shipped: the
    re-import came back "bundle <v> is already the active bundle" plus seed-once, which is the
    unrepairable state finding 2.17 named, re-created by the fix for it. The state arrives
    without this importer's help - README's Backups section documents `cp -a` of that directory
    as a restore, and an interrupted one leaves exactly this - and it is SILENT, because
    `ArtifactStore.load_active` keys `broken` on `root.is_dir()`: a half tree loads as an
    ordinary install, `assert_not_broken` passes, and every §5.2 fit runs against
    `Backbone.empty()` and is stamped with the active version. That is M4.13's data-03 loss, and
    the repair for it must not be gated on a predicate that a half-written directory satisfies.
    [M4.14 cycle 3, m414-c3-dimlock-01; decision 258, M4.13 data-03]

    `backbone.npz` is the question because it is the file §5.1 reads the basis out of and the one
    `Backbone.open` answers `empty()` for. `missing_required` cannot stand in: `BUNDLE_FILES`
    marks only `manifest.json` required, which decision 251 deliberately did not touch, and a
    tree holding one stray manifest would pass. The store's own `broken` predicate is M4.13's and
    this milestone does not touch it (plan section 8) - what is repaired here is the repair.
    """
    from spielplan.scoring.backbone import BACKBONE_FILE

    root = artifacts_root / version
    return not root.is_dir() or not (root / BACKBONE_FILE).is_file()


async def refuse_on_install_state(
    conn: asyncpg.Connection, bundle: Bundle, report: ImportReport,
    artifacts_root: Path | None = None,
) -> ImportReport:
    """The refusals that are facts about the *install* rather than about the bundle.

    §10's sequence is "validate -> stage -> recompute -> flip", and these run ahead of all of it
    on purpose. Each one means no bundle of this kind can be imported at this install *whatever*
    validation would go on to say, and each has to refuse before the first byte is written —
    staging is a copy of the artifacts tree, so "refused before it writes anything" is a claim
    about ordering, not about the transaction. Reporting "you have already seeded" underneath a
    thirty-table content validation buries the one line the operator can act on.

    `artifacts_root` is `settings().artifacts_dir`, supplied by the two callers that stage into
    it. Optional, because `ops/devstub.py` and the fixture pre-flight validate bundles with no
    install behind them at all and a refusal about a directory there is no directory for would be
    an invention. With it, two more install facts become reachable from `/validate` rather than
    only from the import: whether this version is already the active one, and whether the bundle
    is sitting inside the tree the import is about to delete and re-create.
    [M4.14 steps A7 and D2, decision 253, findings 2.8 and 2.17]
    """
    # THE RESTAGE, decided here because the refusals below have to know about it. An active row
    # whose directory is gone is a broken install (`ArtifactStore.load_active` carries it as
    # `broken`), and it had no supported repair at all: re-importing the seed is refused by
    # seed-once, re-importing the active model bundle by "already the active bundle", and
    # `/data/artifacts` is a host bind mount outside the nightly `pg_dump`, so "database
    # restored, files missing" is a realistic recovery state whose only cure was an undocumented
    # manual copy. The active row is deliberately NOT cleared to reach this branch - it is the
    # provenance a restore needs, and §10 gives no process but the importer the right to move it.
    # [M4.14 step D2, finding 2.17]
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
        # Moved out of `import_bundle`, where the Data tab could not see it: §10 step 1 is the
        # operator's decision point, and validating a bundle that is already active reported
        # "ok" and then refused at the flip - after they had committed. `import_bundle` keeps its
        # own copy as belt and braces. [M4.14 step B5, finding 2.11]
        report.fail("bundle", f"bundle {bundle.version} is already the active bundle")

    if artifacts_root is not None and bundle.version != "unknown":
        _refuse_self_staging(bundle, artifacts_root, report)

    # `0015_seed.sql` decides the same question the same way — "the oldest bundle is the seed by
    # construction: it is the one that brought content into an empty install" — and its backfill
    # breaks the tie on `version`. Two orderings for one row is how a migration and the code that
    # reads it come to disagree about which bundle seeded.
    seeded = await conn.fetchval(
        "SELECT version FROM artifact_bundle WHERE kind = 'seed' "
        "ORDER BY imported_at, version LIMIT 1"
    )
    if bundle.content_db is None:
        # decision 162 fixes one order: movie data is restored or seeded first, then a model
        # bundle is loaded on top of it. The other order has nothing for §10's rebuild set to
        # place — and §12's M2 exit criterion ("every owned title has a coordinate") reads zero
        # both for a fully placed library and for an empty one, so the wrong order has to fail
        # loudly rather than arrive at a number that looks right.
        titles = await conn.fetchval("SELECT count(*) FROM title")
        if not titles:
            report.fail(
                "ordering",
                "this is a models-only bundle and the install has no content: restore or seed "
                "movie data first, then load the model bundle (decision 162). §10's rebuild set "
                "re-places every title against the staged basis, and there are no titles",
            )
        if seeded is not None and bundle.version == seeded and not restaging:
            # `artifact_bundle` is keyed by version and the import upserts on it, so a model
            # bundle exported under the seed's version string rewrote that row's kind to
            # 'model' — and the refusal below, which asks for a row of kind 'seed', then had
            # nothing left to find. The active-bundle check does not cover it: after any later
            # model import the seed row is 'superseded', not 'active'. "Content is imported
            # once" is a claim about the whole table's history, so the row recording it is not
            # overwritable by anything.
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
        # Not on a restage: the row this import is repairing IS the seed row, so seed-once would
        # refuse the repair on the strength of the very record the repair exists to keep. The
        # restage loads no content at all (see `import_bundle`), so "content is imported once"
        # is not the claim it would be answering. [M4.14 step D2, decision 162]
        report.fail(
            "seed-once",
            f"movie data was already seeded by bundle {seeded!r} (decision 162: content is "
            "exported once and imported once; every later title is acquired by this app). A "
            "second content import would upsert the corpus's rows over ids this install now "
            "owns — re-import the models only, or restore movie data from a backup",
            seeded=seeded,
        )

    # decision 163. `dna_tag` and `dna_projected` both FK to `dna_vocabulary(version)`, and the
    # newest row is what the rest of the app reads as active (`placement/reconcile.py`), so that
    # is what a bundle is compared against rather than a second notion kept alongside it.
    active_vocab = await conn.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC LIMIT 1"
    )
    if active_vocab and bundle.vocabulary_version is None:
        # decision 256: the guard used to read "if active_vocab AND bundle.vocabulary_version AND
        # they differ", so a bundle that declares nothing skipped decision 163's comparison
        # entirely - and the real BUNDLE.json declares nothing: its sixteen top-level keys carry
        # no vocabulary key at all, which leaves the whole refusal resting on a `dna_vocab/`
        # directory listing. Decision 162 then narrows every future re-import to a models-only
        # bundle, which is precisely the kind least likely to ship that tree. Omit it and a
        # v2-trained backbone lands on a v1 install behind a warning: dna_tag and dna_projected
        # stay on v1 while the feature builder filters on v2, both DNA blocks go empty for every
        # title, and nothing in the read path calls that an error. Not relaxed for models-only
        # bundles, because that is the case it matters in. [M4.14 step B5, finding 2.14]
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
    """The install's own `title` rows, in the shape an identity token is checked against.

    decision 162: content seeds once, so from the seed onwards THIS is the spine — a models-only
    bundle carries none, and the corpus merging two titles reaches an install through exactly
    such a bundle. Same four columns as the bundle-side reading, so `_validate_identity` cannot
    tell which one it was handed.
    """
    rows = await conn.fetch("SELECT id, kind, imdb_id, tmdb_id, name FROM title")
    return {r["id"]: (r["kind"], r["imdb_id"], r["tmdb_id"], r["name"]) for r in rows}


async def active_backbone_coverage(
    conn: asyncpg.Connection, artifacts_root: Path | None
) -> set[int] | None:
    """decision 248: which of this install's bundle titles the ACTIVE backbone actually covers.

    The caller half of the reverse identity check. `validate._validate_identity` refuses a
    retrained backbone that stops covering a title the current one covers — a merged or dropped
    corpus row, after which §5.1 would go on scoring that title from a basis that no longer knows
    it — and that comparison needs a set only a connection and the staged files can produce.

    `origin = 'bundle'` and not every title: a title this household ACQUIRED was never in any
    backbone, and its absence from the next one is not a regression. `None` when there is no
    active bundle, when its files are gone, or when it ships no backbone — those are §3.1's legal
    states, and a broken install has no coverage it could have lost. [M4.14, decision 248]
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

    §10 makes validate the first step and §6.6 makes it the Data tab's decision point, so every
    refusal an import can raise has to be reachable here — otherwise the operator reads "ok" and
    is refused after they have committed. The install-state refusals (seed-once,
    models-need-content, vocabulary-change, already-active, staging-into-itself) are facts about
    this install that no amount of reading the bundle can discover, and both halves of the
    identity check need the install for the same reason: the installed spine, and the coverage
    the active backbone already has.

    `artifacts_root` is `settings().artifacts_dir`, threaded from the route and from
    `import_bundle`. Optional, because the pre-flight tools validate with no install behind them.
    """
    # THE PATH IS ASKED ABOUT FIRST, and that ordering is the whole of two defects. Everything
    # `refuse_on_install_state` says is a sentence about a bundle, and the install-state pass
    # short-circuits this function - so on any install with state to refuse on, step A6's refusal
    # and every finding `Bundle.open` produced were discarded unread. A first boot (`titles` = 0)
    # answered a mistyped path with decision 162's "restore or seed movie data first"; a seeded
    # install answered the same path with decision 256's "this bundle declares no DNA vocabulary
    # version" - two data-modelling diagnoses of a path mistake, which is exactly the sentence
    # step A6 was written to delete. The sharpest case is decision 163's: a bundle shipping TWO
    # `dna_vocab/<v>/` trees has not declared NONE, and the one line that says so is the open
    # finding replayed here, so decision 256's remedy ("ship its `dna_vocab/<version>/` tree")
    # was advice to do what the bundle had already done twice.
    #
    # Into its OWN report, discarded when it passes rather than carried: `validate` replays
    # `open_findings` itself, and the splice below puts `install_state.findings` in front of
    # `validate`'s without going through `_record`, which is the one path around its dedup - so a
    # carried note would print decision 257's "X was opened as the bundle" twice.
    # [M4.14 cycle 2, m414-c2-refusals-02 and M414-C2-VOCAB-02, decisions 256 and 257]
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
        # Same reason `validate_id_partition` stops the report: a bundle that cannot be imported
        # at this install *at all* gets the one line the operator can act on, not that line
        # buried under thirty table counts.
        return install_state
    spine = None if bundle.content_db is not None else await installed_spine(conn)
    coverage = await active_backbone_coverage(conn, artifacts_root)
    # OFF THE EVENT LOOP, and this one line is decision 252's unbuilt half (decision 287).
    # `validate` is wholly synchronous and expensive in two places that are this milestone's own:
    # `_verify_bundle_files` hashes 42 files and 1.04 GB (1.77 s measured) and `validate_artifacts`
    # imports torch and builds the Cold Tower (1.20-1.23 s), ~2.97 s straight-line. Both routes
    # that reach this function are `async def`, so FastAPI's threadpool does not apply and that
    # window was spent on the API process's single loop - `app._HEALTH_TIMEOUT_S` is 2, so
    # `/api/health` answered 503 once inside `POST /import`'s validation window in every recorded
    # run of `ops/m414_exit_criterion.py`, and its check 9 was the one red check of thirteen.
    #
    # A THREAD and not decision 252's one-shot child process: the cost that ruling was written
    # against is a blocked loop, and hashlib, sqlite3 and torch all release the GIL for the work
    # that blocks it, so a thread buys the same responsiveness without spawn latency or Windows'
    # importable-callable constraint. It does not buy the resident-set half of 252's argument -
    # torch stays in this process - which is why 287 supersedes only the subprocess half. The
    # other half is kept exactly: the route still blocks on this await, so validation is
    # synchronous from the operator's side and `validate_for_install`'s contract that "every
    # refusal an import can raise has to be reachable here" is untouched. (The one-shot pool's
    # class name is deliberately not written here: `test_decision_252_does_not_assert_a_subprocess
    # _this_tree_does_not_have` greps this package for it, and that guard is what holds decision
    # 252's record against the absence of the construction it rules on.)
    #
    # Nothing loop-bound crosses the boundary. `validate` takes no connection: both of its
    # install-dependent inputs are awaited above and cross as plain values, and every
    # `sqlite3.connect` it makes is opened and closed inside the call, on the calling thread.
    # [M4.14 cycle 4, decision 287, plan step E7; supersedes half of decision 252]
    report = await asyncio.to_thread(validate, bundle, spine=spine, active_coverage=coverage)
    # The install-state pass writes NOTES as well as refusals now — decision 253's restage is
    # one, and it changes what the whole rest of this report means — and `validate` builds its
    # own report rather than being handed one. Carried across rather than dropped, and first,
    # because they are the facts about the install the findings below are then read under.
    report.findings[:0] = install_state.findings
    # §10's swap sequence ends in a restart, so step 1 is the last moment at which a constant the
    # §5.2 fit cannot use can be reported to the operator rather than discovered as a refusal on
    # the Rate and Rank surfaces afterwards. Here and not inside `validate` because `validate` is
    # also the pre-flight tools' entry point with no install behind it, while this is the function
    # `api/artifacts.py`'s validate route and `import_bundle` below both go through — the two
    # paths that can actually stage and flip. [M4.10 finding 10; ml06]
    validator.validate_hyperparams(bundle.artifacts_dir, report)
    return report


async def position_id_sequences(conn: asyncpg.Connection, report: ImportReport) -> None:
    """decision 162: the seed import positions the mint — `0015_seed.sql` deliberately does not.

    A migration cannot. It runs against an empty `title`, so `setval(max(id))` would yield 1 and
    the first acquired title would take corpus title 1's id. The seed is the only moment at which
    the corpus's maximum is known, so this runs at the end of a content import and nowhere else.

    The floor is a floor and not a start: `GREATEST` means a seed of a *smaller* corpus can never
    walk the mint back down into the corpus's range, and the sequences' own MINVALUE refuses it
    besides. `setval(..., false)` positions the next mint *at* the value rather than after it.
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


# The advisory-lock name every bundle import serialises on, and the name any other writer that
# must not run across §10's flip asks about. A NAME hashed by Postgres rather than a hand-picked
# integer, because `tonight/play.py`'s `_FINISH_LOCK = 6202` is the one other lock in this
# codebase and the thing it argues is that a lock's identity must not be able to collide with
# another feature's by arithmetic accident; `hashtext` makes the identity the sentence.
# Module-level and exported so the worker can take the same key. [M4.14 step D1, finding 2.2]
IMPORT_LOCK = "spielplan.bundle-import"


async def _drop_orphan_staging(
    conn: asyncpg.Connection, bundle: Bundle, staged: Path, report: ImportReport,
    *, wrote: bool = False,
) -> None:
    """After a rolled-back import: remove the staged tree if no row in the database names it.

    The copy happens before the transaction (so a failed copy cannot leave the DB pointing at
    absent files), so a rollback leaves `/data/artifacts/<version>/` behind with nothing that
    refers to it and nothing that would ever clean it up — `artifact_bundle` is the only index of
    that directory, and a version with no row is a directory no surface can name. Measured with
    two concurrent imports of different versions: `artifact_bundle` held one version while
    `/data/artifacts` held two.

    A row that DOES name this version keeps its files, whatever state it is in: a `superseded`
    row is the bundle a restore rolls back to, and decision 249 makes those rows provenance
    rather than garbage. The delete is therefore conditional on the table, not on the outcome.
    [M4.14 step D1, finding 2.2]

    `wrote` is the boundary of that clause, and without it the clause defended the wrong tree.
    The caller passes it when this import emptied `staged` and wrote whatever is there now - the
    copy that failed part way after the `rmtree` succeeded - and what is on disk then is nobody's
    provenance: the row's files were destroyed a statement earlier, and half a copy of a bundle
    is not a bundle a restore can roll back to. Keeping it closed the restage D2 shipped, because
    a directory that merely EXISTS made the retry "bundle <v> is already the active bundle", and
    the note below promised the operator those files "are overwritten by the next import of this
    version" - false on exactly that path, because the next import of that version was refused.
    [M4.14 cycle 3, m414-c3-dim23-restage-half-copy, m414-c3-dimlock-01 and -02]

    Conditional on the table is what lets it run on EVERY exit that leaves no row, and it was
    reached from the `_Rollback` arm alone - so the two exits D1's own sentence covers and that
    arm does not, a Postgres or sqlite error raised inside the transaction and the cancellation
    `asyncio.wait_for` delivers at the job's budget, each left `/data/artifacts/<version>/` (205
    MB of the real bundle) behind with nothing indexing it and nothing that ever would.
    [M4.14 cycle 1, m414-c1-dim-lock-05]

    The note no longer names a transaction, because the fourth exit does not have one: the
    `rmtree` and the `copytree` run BEFORE `conn.transaction()` opens and are now guarded too
    (cycle 2), so an ENOSPC or a read-only mount reaches this function with nothing ever having
    begun. "No rows were written" is what is true on all four. [M4.14 cycle 2, m414-c2-refusals-03]
    """
    named = await conn.fetchval(
        "SELECT version FROM artifact_bundle WHERE version = $1", bundle.version
    )
    if not staged.exists():
        # The two states this branch used to merge, said apart. A `copytree` that raised at its
        # own `makedirs` - an artifacts root that is present and not writable by uid 1000, the
        # case the stage failure's own sentence names - reaches here with nothing ever having
        # been created, and was told that artifacts "remain on disk" at a path that does not
        # exist. [M4.14 cycle 3, m414-c3-refusals-01]
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
        # `_clean_unpacked`'s rule, which this function did not have: `ignore_errors=True`
        # swallows EACCES, EBUSY and ENOTEMPTY alike, and the note beneath it stated a removal
        # that had not happened - two contradictory sentences about one directory on one screen,
        # in the milestone whose subject is that a refusal says what it did.
        # [M4.14 cycle 3, m414-c3-refusals-01; cycle 2 finding 12]
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
    DB pointing at files that are not there."""
    # D1: ONE import at a time, on this install, across every process that can start one.
    # `grep -rn advisory backend/spielplan` was empty before this line: every install-state
    # refusal, the already-active check and the rmtree+copytree all run outside the transaction,
    # so two imports were serialised by nothing at all. Reproduced twice. With a 0.15 s stagger
    # on the same version the second request passed every check — the first's row was still
    # uncommitted and therefore invisible to it — then deleted and re-copied the very tree the
    # first was about to hand to `run_rebuild`, and died on `UniqueViolationError
    # title_alias_pkey`. With two different seed versions, `artifact_bundle` held one and
    # `/data/artifacts` held two. The operator-facing trigger is ordinary: a retry after a proxy
    # timeout, which §5.3's "minutes" makes the expected shape of this request.
    #
    # SESSION-LEVEL, not `pg_advisory_xact_lock` as `tonight/play.py` takes it, because the work
    # this serialises starts BEFORE the transaction does: the rmtree and the copytree are the
    # two statements the reproduction above collided on, and an xact lock would not be held yet.
    # Released in a `finally` on the same connection that took it — a session lock outlives the
    # transaction by design, which is the point, and would outlive the request too.
    #
    # TRY and not wait: a second import is a mistake to be told about, not a queue to join. §10
    # makes this a planned admin event and the operator is standing in front of the Data tab, so
    # the answer is a report line the route turns into a 422. [M4.14 step D1, finding 2.2]
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
            # A7, asked a second time. `validate_for_install` above asks it so the Data tab refuses
            # before Import is enabled; this one is here because the `rmtree` two lines down is the
            # statement that deletes the operator's gigabyte, and a guard that protects a destructive
            # line belongs beside that line rather than four hundred lines away in a caller.
            # [M4.14 step A7, finding 2.8]
            return report

        # `kind` beside `state`, because the restage below has to keep it. The upsert at the foot
        # of the transaction writes `kind = EXCLUDED.kind` from `bundle.kind`, which is derived
        # from whether the tree carries a content database - so restaging the ACTIVE seed from a
        # models-only copy of that same export rewrote the seed row's kind to 'model'. That copy
        # is the natural repair rather than an exotic one: a restage keys on the version, and
        # while the active row IS the seed row the artifacts half is the 205 MB an operator
        # keeps, the two content databases being 790 MB of the 1.04 GB a restage does not use.
        # `refuse_on_install_state`'s seed-once query asks for the oldest row of kind 'seed' and
        # then had nothing to find, `artifact_bundle_one_seed` indexes nothing with no seed row,
        # and the next content bundle was accepted and ran `load_content` over the ids this
        # install owns - the exact loss decision 162 exists to prevent, silently, on a path this
        # milestone opened. `refuse_on_install_state`'s first seed-once guard already argues that
        # this row's kind is the only record that content was ever seeded.
        # [M4.14 cycle 2, m414-c2-dimlock-restage-erases-seed-once, decisions 162 and 258]
        existing = await conn.fetchrow(
            "SELECT state, kind FROM artifact_bundle WHERE version = $1", bundle.version
        )
        already = existing["state"] if existing is not None else None
        # decision 253's restage. Reaching here with an active row means `refuse_on_install_state`
        # found no directory for it — a broken install, which this import repairs rather than being
        # refused by. The second reading of the same fact, beside the `rmtree`, for the same reason
        # A7 is asked twice.
        restaging = already == "active"
        if restaging and not _needs_restage(artifacts_root, bundle.version):
            report.fail("bundle", f"bundle {bundle.version} is already the active bundle")
            return report

        # THE IMPORT'S TWO DESTRUCTIVE FILESYSTEM STATEMENTS, GUARDED. They ran before the inner
        # `try:` below, so none of the four arms that exist to turn a failure into a report line
        # and to remove an orphan tree could see either of them - and the `except (PostgresError,
        # DatabaseError, OSError)` arm names "a disk that fills" as a case it covers while the
        # largest disk write of the import (205 MB of the real bundle) sat outside it. Measured:
        # an ENOSPC mid-`copytree` left `/data/artifacts/<version>/` half written with no
        # `artifact_bundle` row naming it, no finding, and no surface that could name it; a
        # `rmtree` that could not remove a held file left that version's tree half deleted.
        #
        # Its own `try` and not the inner one's, because the sentence differs: nothing has begun
        # here, so the report may not talk about a transaction. `_drop_orphan_staging` is the same
        # rule as on every other exit - the tree goes only when no row names that version, which
        # is what keeps a superseded bundle's files (decision 249's provenance, and what a restore
        # rolls back to) from being removed by a failure to replace them.
        # [M4.14 cycle 2, m414-c2-refusals-03, step D1]
        #
        # WHICH TREE IS WHOSE, which that rule could not tell and therefore got backwards here.
        # The `rmtree` runs first, so once it returns the row's files are already gone and
        # whatever the failed `copytree` left is this attempt's own half-copy: keeping it kept
        # wreckage under decision 249's name, and on the restage path - where the active row
        # ALWAYS names the version - the directory it left then made the retry "bundle <v> is
        # already the active bundle", closing the repair D2 exists to provide with its own
        # output. `emptied` is the fact the rule was missing; `existed` is the other one, because
        # a copy that destroyed the previous one and could not replace it is the single thing the
        # operator has to be told and the only thing the report withheld.
        # [M4.14 cycle 3, m414-c3-dim23-restage-half-copy, -a-failed-replacement, dimlock-01/-02]
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

        # decision 247 guard 3: the version the curated ledgers are written under, read once and
        # used for the ledgers, for the two content tiers below and for the `artifact_bundle` row,
        # so that the three cannot disagree. `dna_adjudication.version` is a foreign key to
        # `dna_vocabulary`, so the `or "v1"` literal this replaces could file 828 curated verdicts
        # under a vocabulary the install is not on.
        #
        # Guard 3 asks for the install's ACTIVE version where the bundle names none, and that is
        # what this is: decision 256's refusal above makes "the bundle declares nothing and the
        # install has a vocabulary" a report failure, so by the time execution reaches here the
        # bundle's version either equals the install's or the install has none to equal. A second
        # reading of `dna_vocabulary` here would be a branch that cannot run, which is how two
        # derivations of one fact start. [M4.14 steps B5 and C2, decisions 247 and 256]
        #
        # It can still be None, and legally: an install with no vocabulary at all, taking a
        # bundle that names none, is section 3.1's empty naming layer. Decision 262 is what
        # that case does - see the seed branch below - and nothing downstream of here
        # invents a version for it. [M4.14 cycle 1]
        vocabulary = report.vocabulary_version

        db = (
            # `restaging` closes the content side whatever the bundle kind: the active row is the
            # proof that this bundle's content was already loaded, and re-running the loaders would
            # be a second content import wearing a repair's name. [M4.14 step D2]
            None if bundle.content_db is None or restaging
            else sqlite3.connect(f"file:{bundle.content_db}?mode=ro", uri=True)
        )
        try:
            async with conn.transaction():
                # decision 162: a models-only re-import loads no content at all. Every loader below
                # reads `content.sqlite` or writes rows keyed by the corpus's title ids, and running
                # any of them against a model bundle would be a second content import wearing
                # another name — which is the one thing "content seeds once" forbids.
                if db is not None:
                    db.text_factory = str
                    # §4.1's per-field resolution runs over an order the corpus owns, and the
                    # corpus records it in `BUNDLE.json` at the bundle root — so the loader needs
                    # the root, not just the sqlite handle, or the order stops travelling.
                    await content_loader.load_content(conn, db, report, bundle_root=bundle.root)
                    if not report.ok:
                        # `load_content` RETURNS on its own shape refusals - a mapping naming a
                        # column the bundle does not ship, a required table absent, a shipped
                        # table this importer accounts for nowhere - and each of them returns
                        # before a single `title` row is written. Nothing read `report.ok`
                        # between there and `load_tags` below, which then INSERTed `dna_tag` rows
                        # against an empty `title` and died on `dna_tag_title_id_fkey`: not a
                        # `_Rollback`, so the handler that removes the staged tree never ran and
                        # the operator's report was a constraint name instead of `load.py`'s own
                        # sentence naming the column. The refusal is already written; this is the
                        # line that makes it the report.
                        # [M4.14 cycle 1, m414-c1-dim-refusals-02]
                        raise _Rollback(report)

                    # Counted apart and never summed, for §4.1 rule 1's reason and in the
                    # shape `validate.py` counts them: a sum drops the tier discriminator exactly
                    # as a UNION does.
                    tagged_rows = report.table_counts.get("dna_tag") or 0
                    projected_rows = report.table_counts.get("dna_projected") or 0
                    if vocabulary is None and (tagged_rows or projected_rows):
                        # decision 256: no code path invents a vocabulary version. `or "v1"` did,
                        # and it did so for the rows that define what a DNA term MEANS — `dna_tag`
                        # and `dna_projected` both foreign-key `dna_vocabulary(version)`, so a
                        # bundle that named none had its whole naming layer filed under a literal
                        # this module made up, with nothing anywhere recording that it had.
                        #
                        # Conditioned on the ROWS, which is the question `validate.py` asks one
                        # file over, and not on the bundle being a seed, which is what it asked.
                        # The two guards over one question disagreed, and this side could never
                        # print a true sentence: the validator refuses any bundle that has rows
                        # and no tree, so every bundle that reached this line had none - and it
                        # was told it "carries DNA rows". §3.1 makes an empty naming layer legal
                        # and `test_bundle_validation.py` registers exactly that bundle as ok, so
                        # the refusal below now fires only where the validator would have fired
                        # first and this is the backstop, not a second rule.
                        # [M4.14 cycle 1, m414-c1-import-only-vocabulary-refusal, decision 262]
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
                        # Decision 262: the row records NULL, and NULL is what this install's
                        # naming layer is - no `dna_vocabulary` row is created either, so the
                        # column and that table still cannot drift apart. Said out loud because
                        # the whole DNA surface renders its empty state from here on.
                        report.note(
                            "vocabulary",
                            "this bundle ships no DNA rows and names no vocabulary version, so "
                            "the naming layer is empty (§3.1) and the artifact_bundle row "
                            "records no vocabulary",
                        )
                    # rule 1: two tiers, two calls. Called with `vocabulary` even when it is None:
                    # both tiers are empty in that branch, so the DELETE matches nothing and the
                    # insert has nothing to file - and skipping them would skip the two "bundle
                    # has no dna_tag table" refusals, which are statements about the bundle's
                    # shape rather than about its vocabulary.
                    await dna_loader.load_tags(conn, db, vocabulary, report)
                    await dna_loader.load_projected(conn, db, vocabulary, report)

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
                else:
                    # decision 247: the four CURATED ledgers travel with the models and are loaded on
                    # every import, this one included. They sat inside the branch above, which under
                    # decision 162 no recurring bundle ever enters — so a models-only re-import of a
                    # bundle whose `corrections_v1.tsv`, `seed_list.json` and adjudications differed
                    # left `credit_correction`, `seed_list`, `dna_adjudication` and `dna_axis_weight`
                    # byte-for-byte unchanged, with `report.table_counts` empty and no line in the
                    # report to notice it by, while `validate_id_partition` had just read two of
                    # those same files off the same bundle. The importer inspected artifacts it then
                    # discarded.
                    #
                    # NEVER `load_vocabulary`'s term tables, `load_tags` or `load_projected`: those
                    # are the content tiers decision 162 forbids re-importing, and they are why this
                    # is an `else` rather than the whole block moving out of the `if`. Each loader
                    # below reports its own absent file as a warning that changes nothing — omission
                    # must never be destructive. [M4.14 step C2, decision 247, finding 2.15]
                    await dna_loader.load_corrections(
                        conn, bundle.artifacts_dir / "corrections_v1.tsv", report
                    )
                    await dna_loader.load_seed_list(
                        conn, bundle.artifacts_dir / "seed_list.json", report
                    )
                    if vocabulary is not None:
                        vocab_dir = bundle.artifacts_dir / "dna_vocab" / vocabulary
                        # decision 265. `dna_adjudication.version` is `NOT NULL REFERENCES
                        # dna_vocabulary(version)` and `load_vocabulary` - the only writer of
                        # that table in this app - runs on the seed branch alone, so an install
                        # whose naming layer is empty has no parent row for these ledgers to
                        # hang off. Decision 262 makes that install legal and ordinary (a content
                        # seed exported before the DNA extraction pass had run), and decision
                        # 256's refusal is guarded on `active_vocab` being set, so it asks such
                        # an install nothing: `validate_for_install` returned ok and the import
                        # then died mid-transaction on `dna_adjudication_version_fkey`, after the
                        # 205 MB copy, with no rule in this importer having named it.
                        #
                        # SKIPPED with a line rather than refused, and the difference matters:
                        # under decision 162 the naming layer can only be filled by a content
                        # import and seed-once forbids a second one, so a refusal here would be a
                        # refusal that household could never satisfy and every later model bundle
                        # would meet it too. Decision 247's own rule is that an unapplied ledger
                        # changes nothing, and there is nothing to change - the install has no
                        # DNA rows for these verdicts to be about.
                        #
                        # decision 266: the same for a bundle that names a vocabulary and ships
                        # no tree for it, which is what decision 256's remediation invites an
                        # operator to produce ("export the bundle with `vocabulary_version` in
                        # BUNDLE.json"). Both ledgers live inside that tree, so neither loader's
                        # own absent-file warning can fire, and the only line the report carried
                        # was `validate`'s - which is a sentence about the bundle and was read as
                        # one about the install. One line per skip, the shape decision 247's
                        # coverage row asks for. [M4.14 cycle 2, decisions 265 and 266]
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
                            # §6.4's axis TSVs live beside the vocabulary files. `load_axes` reads
                            # `dna_facet` for its facet set here, because on a models-only bundle
                            # `load_vocabulary` has not run to hand it one. [decision 173]
                            await dna_loader.load_axes(conn, vocab_dir, vocabulary, report)

                # §10's "counts per table", checked against what the corpus says it exported rather
                # than only printed. Inside the transaction so a disagreement rolls the load back:
                # a bundle whose `content.sqlite` holds a different number of rows than its own
                # BUNDLE.json declares is not the bundle that manifest describes, and a re-hash can
                # still agree with it — an export that wrote its manifest from the wrong side of a
                # filter is exactly that shape. [M4.14 step B1, finding 2.4]
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
                    # decision 162: which kind of import produced a version is what the §6.6 board
                    # has to be able to say, and `artifact_bundle_one_seed` is what makes "exactly
                    # one content seed, ever" survive a restart and a developer with psql.
                    #
                    # The STORED kind on a restage, never the bundle's: a restage repairs the row
                    # that exists, it does not re-describe the import that produced it. `kind` is
                    # the one record that content was ever seeded, and decision 249 makes these
                    # rows provenance - so a repair that loads no content (see `db` above) may
                    # not be the thing that erases it. [M4.14 cycle 2, decisions 162 and 258]
                    existing["kind"] if restaging and existing is not None else bundle.kind,
                    # The resolved vocabulary, and never NULL after a successful import: the row
                    # records which naming layer a bundle was imported under, and it read NULL for
                    # every real bundle because the shipped BUNDLE.json declares no vocabulary key
                    # and this module only ever wrote the declaration. What makes the column
                    # trustworthy now is decision 256 rather than a fallback - a bundle with no
                    # version to record into an install that has one is refused, so a row that
                    # exists is a row whose vocabulary was known. [M4.14 step B5, finding 2.14]
                    #
                    # One exception, and decision 262 names it: section 3.1's empty naming
                    # layer. A bundle with no DNA rows into an install with no vocabulary
                    # writes NULL here and creates no `dna_vocabulary` row either, so the
                    # column still records what the install has rather than a guess.
                    # [M4.14 cycle 1]
                    vocabulary,
                )
                if activate:
                    # §10's sequence: "recompute the rebuild set against the **staged** bundle ->
                    # transactionally flip". Before the flip, so a rebuild that fails takes the whole
                    # import down with it rather than leaving a new basis active with every fitted
                    # number still expressed in the old one.
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
                    # The flip, and NOT a second phrasing of §10 step 5. This note is rendered as a
                    # finding on the same import screen that also shows the restart sentence, so the
                    # two stood two elements apart saying the same thing in different words - the
                    # drift `api/artifacts.py::RESTART_REQUIRED` was extracted to prevent, inside the
                    # screen it was extracted for. The instruction is stated once, there; the importer
                    # cannot quote it without inverting `importer -> api`, so it points instead.
                    # [M4.13 cycle 1, m413-c1-dim1-restart-sentence-written-in-three-places]
                    #
                    # `superseded` rides in the detail because §6.7's rail can no longer carry it.
                    # `bundle_swap` and `reconcile` were recorded here while the import ran inside
                    # the request, where the per-process ring buffer is the one
                    # `GET /api/home/model-log` reads; step E2 moved the import into the worker
                    # and the two calls went with it, into a buffer with no reader in a process
                    # with no HTTP surface. Decision 263 moves both kinds to
                    # `rail.AWAITING_PRODUCER` and removes the calls, so the report - which IS
                    # persisted, and is what the Data tab renders - is where the one fact those
                    # lines carried that nothing else did now lives. `already_active` is still
                    # read ABOVE the two UPDATEs: after them the same SELECT answers with this
                    # bundle's own version, and the note would say a swap superseded itself.
                    # [decision 189; M4.14 cycle 1, m414-c1-dim-lock-02, decision 263]
                    report.note(
                        "swap",
                        "artifact_bundle flipped to active — every fitted number is expressed in "
                        "this basis from now on; this import's note carries the restart it needs"
                        + (f"; it supersedes {already_active}" if already_active else ""),
                        superseded=already_active,
                    )
                    report.note(
                        "rebuild-set", "recomputed against the staged bundle: " + "; ".join(REBUILD_SET)
                    )
                    # C4: the stored report is the WHOLE report. The row above was INSERTed with
                    # `report.as_dict()` before any of §10's rebuild ran and nothing updated it, so
                    # the database's copy of "a planned admin event with a diff report" was missing
                    # exactly {rebuild, swap, rebuild-set} — measured as that set difference after a
                    # clean fixture import — and therefore said the rebuild had not been performed.
                    # Last statement in the transaction, because every finding above it is part of
                    # what it stores; inside it, because a report claiming a flip that rolled back is
                    # the same defect from the other side. Once the Data tab polls instead of reading
                    # the response (§5.3, the worker job), this row is the only thing it can render.
                    # [M4.14 step C4, finding 2.19]
                    await conn.execute(
                        "UPDATE artifact_bundle SET report = $2 WHERE version = $1",
                        bundle.version, report.as_dict(),
                    )
        except _Rollback:
            # The DB transaction rolled back, but the artifact copy happened before it (so a failed
            # copy could not leave the DB pointing at absent files). Say which is which rather than
            # claiming nothing happened.
            await _drop_orphan_staging(conn, bundle, staged, report)
        except (asyncpg.PostgresError, sqlite3.DatabaseError, OSError) as exc:
            # The database's OWN refusals, turned into the report line section 10 promises. A
            # constraint this importer has no rule for, a `content.sqlite` that stops being
            # readable mid-load, a disk that fills: none of the three is a `_Rollback`, so the
            # clause above could not see any of them and the staged tree outlived the transaction
            # with no `artifact_bundle` row naming it and nothing that would ever clean it up.
            # Deliberately NOT `except Exception`: a `KeyError` out of this package is a bug and
            # goes on arriving as one, which is the boundary `api/artifacts.py::_open` draws one
            # layer up. [M4.14 cycle 1, m414-c1-dim-refusals-02]
            report.fail(
                "import",
                f"the import stopped on {type(exc).__name__}: {exc}. Nothing was written - the "
                "transaction rolled back - and no rule in this importer named this refusal first",
            )
            await _drop_orphan_staging(conn, bundle, staged, report)
        except BaseException:
            # Cancellation, and the bugs above it. `_tick` bounds this job with `asyncio.wait_for`,
            # which CANCELS the attempt - and a `CancelledError` is not an `Exception` - so
            # without this arm an abandonment at the budget left a full copy of the artifacts tree
            # under a version the database has never heard of. The tree goes and the exception is
            # re-raised unchanged: what happened is the caller's to report, and this arm owes only
            # the disk. [M4.14 cycle 1, m414-c1-dim-lock-05]
            #
            # AND THE DISK'S OWN OUTCOME IS LOGGED, because this is the one exit where `report` is
            # never returned: the raise carries no report, `worker._bundle_import`'s crash arm and
            # `_reap_abandoned_import` each build a fresh one, and so every line
            # `_drop_orphan_staging` just wrote - including the could-not-remove case cycle 3
            # added precisely because `ignore_errors=True` hides EACCES, EBUSY and ENOTEMPTY -
            # went into an object with no reader. Widening the arm to RETURN a report would be
            # the wrong repair (the sentence above is right: this arm owes only the disk), so the
            # findings are emitted where an operator correlating an abandoned claim can still
            # find them. `len(report.findings)` is captured first so only what this call appended
            # is logged, not the whole import's report.
            # [M4.14 cycle 4, M414-C4-REF-06]
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
        # SUPPRESSED, for `_clean_unpacked`'s reason one function down: housekeeping must never
        # fail an import that has already committed. This statement is the last thing the import
        # does and it sat unguarded in a `finally`, so a connection that died anywhere in the tail
        # - the seconds `_clean_unpacked` spends rmtree-ing 790 MB of a 1.04 GB extraction with
        # this connection idle, a `pg_terminate_backend`, a pooler cutting an idle session - threw
        # away the `return report` above it. The bundle was active, and `worker._bundle_import`'s
        # crash arm then closed the job `ok=false` with "the import did not run to a report":
        # verbatim the outcome decision 253 exists to remove, reached through the code that
        # removed it. Nothing leaks by staying quiet: `create_pool`'s reset query runs
        # `pg_advisory_unlock_all()` when the connection goes back, and a session that has died
        # holds nothing at all. [M4.14 cycle 3, m414-c3-dimlock-03; decision 253]
        with contextlib.suppress(Exception):
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", IMPORT_LOCK)


def _clean_unpacked(bundle: Bundle, report: ImportReport) -> None:
    """Delete the tree `_unpack` extracted, once the import that needed it has committed.

    `_unpack` was the only writer of `.unpacked-<name>/` and there was no reader and no cleaner
    anywhere: every validated archive left a full second copy of itself under `/data/import`,
    `content.sqlite` and `reviews.sqlite` included — 790 MB of a 1042 MB bundle, and the two
    files the staged `/data/artifacts/<version>/` copy deliberately does not carry. The same
    bundle offered as `.tar` and as `.tar.zst` is two archives and leaves two trees; the
    measured total was 3.6 GB for one bundle. `docker-compose.yml` binds `./data/import` from the
    host, so that is the household's disk, and `POST /validate` — documented as writing nothing —
    is what spends it.

    Only after a commit, and only for a tree this module extracted. A failed import is exactly
    when the operator retries, and making them re-extract a gigabyte to do it would be a
    punishment for a failure that is usually the bundle's. A directory bundle is left alone: it
    is the operator's own directory, not this module's scratch space. [M4.7 dd10]

    The note follows the outcome, not the call. `ignore_errors=True` stays — housekeeping must
    never fail an import that has already committed — but it swallows EACCES, EBUSY, ENOTEMPTY
    and "cannot call rmtree on a symbolic link" alike, so a note written unconditionally after it
    reported every one of those to the household as a success. The install that meets this is one
    this milestone created: a `.unpacked-*` tree written by the previous root container, which the
    uid-1000 image reuses happily (`_unpack` reuses an existing tree) and then cannot unlink out
    of a root-owned directory. Reporting 790 MB freed while they are still on the host's disk is
    worse than reporting nothing, because dd10's whole subject is that nobody could see what
    `/data/import` was holding. [M4.7 dd10; cycle 2 finding 12]
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
    """What `artifact_bundle.manifest` records: BUNDLE.json, the bundle's own identity record.

    `artifacts/manifest.json` was stored here until M4.5. §4.3 defines that file as the fitted
    3-class cut-points per source, so the admin board's "which bundle is this" panel was reading
    a calibration table — and the version, the table list and the export's own validations, all
    of which BUNDLE.json carries, were nowhere in the database.
    """
    return _identity(bundle.root)


class _Rollback(Exception):
    def __init__(self, report: ImportReport) -> None:
        super().__init__("import validation failed")
        self.report = report
