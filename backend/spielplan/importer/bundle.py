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

import csv
import json
import re
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
from spielplan.importer.report import ImportReport
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile as placement

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


async def _rebuild_ledger_refit(conn: Any, store: Any, _version: str) -> dict[str, Any]:
    """§10 step 3: "a **full** Personal Ledger MAP refit" — full history, every user and kind."""
    from spielplan.ledger import observations, refit
    from spielplan.ledger.hyperparams import load as load_hp
    from spielplan.scoring import backbone as bb

    hp, _notes = load_hp(store)
    # Against the STAGED bundle's Backbone, not the active one: §10's whole point is that the
    # two bases are incompatible, and this refit exists to move every fitted number into the new
    # one. The placement source alone would fit every warm title at e = 0.
    reports = await refit.refit_all(
        conn, hp, embeddings=observations.standard_embeddings(conn, bb.load_for(store))
    )
    return {"fits": [r.as_dict() for r in reports]}


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
    content_db: Path | None
    reviews_db: Path | None
    artifacts_dir: Path
    vocabulary_version: str | None = None

    @property
    def kind(self) -> str:
        """§10's two kinds of import, under decision 162: content seeds once, models re-import.

        Which one a bundle is, is a property of what it carries rather than a flag an operator
        sets — an operator who can declare a content bundle to be "models only" can seed twice.
        """
        return "seed" if self.content_db is not None else "model"

    @classmethod
    def open(cls, path: Path) -> Bundle:
        """Open a bundle directory, or an unpacked-on-demand `.tar` / `.tar.zst` archive."""
        root = path if path.is_dir() else _unpack(path)
        identity = _identity(root)
        content = root / "content.sqlite"
        reviews = root / "reviews.sqlite"
        return cls(
            root=root,
            version=safe_version(identity.get("bundle_version")),
            content_db=content if content.is_file() else None,
            reviews_db=reviews if reviews.is_file() else None,
            artifacts_dir=root / "artifacts",
            vocabulary_version=_vocabulary_version(identity, root / "artifacts"),
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


def _vocabulary_version(identity: dict[str, Any], artifacts_dir: Path) -> str | None:
    """The vocabulary this bundle carries. Decision 163's refusal has nothing to compare without
    it, and the shipped BUNDLE.json does not name one: the vocabulary's identity is the
    `dna_vocab/<version>/` directory the bundle ships (§4.3 names the directory, not a key)."""
    declared = identity.get("vocabulary_version")
    if isinstance(declared, str) and declared:
        return declared
    vocab = artifacts_dir / "dna_vocab"
    versions = sorted(p.name for p in vocab.iterdir() if p.is_dir()) if vocab.is_dir() else []
    return versions[-1] if versions else None


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
        with corrections.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        _report_app_range(
            report,
            "corrections_v1.tsv title_id",
            _in_app_range(r.get("title_id") for r in rows),
        )

    # The fifth id-bearing artifact, and it travels with the models like the other four:
    # `adjudications_v1.tsv` is keyed per TITLE (scope, title_id, term, action, …), not per term,
    # which is the whole reason the vocabulary layer was rewritten around it. Its `scope=global`
    # rows carry no title_id and `_in_app_range` drops them.
    vocab_version = bundle.vocabulary_version
    if vocab_version:
        relative = f"dna_vocab/{vocab_version}/adjudications_v1.tsv"
        adjudications = bundle.artifacts_dir / "dna_vocab" / vocab_version / "adjudications_v1.tsv"
        if adjudications.is_file():
            with adjudications.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter="\t"))
            _report_app_range(
                report,
                f"{relative} title_id",
                _in_app_range(r.get("title_id") for r in rows),
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


def validate(bundle: Bundle, *, spine: validator.Spine | None = None) -> ImportReport:
    """Validate without writing anything. This is the wizard's and the Data tab's first step.

    `spine` is the installed title rows, for the decision-162 identity check on a models-only
    bundle — which carries no spine of its own. Callers with a connection use
    `validate_for_install`; this signature stays synchronous because the pre-flight tools
    (`ops/devstub.py`, the fixture tests) validate a bundle with no install behind them.
    """
    report = ImportReport(bundle_version=bundle.version)

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
        finally:
            db.close()

        if bundle.reviews_db is None:
            report.warn(
                "bundle",
                "reviews.sqlite absent — future re-extraction and text embedding will have no "
                "bodies",
            )

    validator.validate_artifacts(bundle.artifacts_dir, report, spine=spine)

    # `validate_artifacts` reads the vocabulary out of `artifacts/manifest.json`, which does not
    # carry one. BUNDLE.json does, and decision 163's refusal compares against this value — so
    # the authoritative reading is applied last rather than left to lose a race with a key that
    # no shipped manifest has.
    report.vocabulary_version = bundle.vocabulary_version or report.vocabulary_version
    return report


async def refuse_on_install_state(
    conn: asyncpg.Connection, bundle: Bundle, report: ImportReport
) -> ImportReport:
    """The three refusals that are facts about the *install* rather than about the bundle.

    §10's sequence is "validate -> stage -> recompute -> flip", and these run ahead of all of it
    on purpose. Each one means no bundle of this kind can be imported at this install *whatever*
    validation would go on to say, and each has to refuse before the first byte is written —
    staging is a copy of the artifacts tree, so "refused before it writes anything" is a claim
    about ordering, not about the transaction. Reporting "you have already seeded" underneath a
    thirty-table content validation buries the one line the operator can act on.
    """
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
        if seeded is not None and bundle.version == seeded:
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
    elif seeded is not None:
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
    if active_vocab and bundle.vocabulary_version and bundle.vocabulary_version != active_vocab:
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


async def validate_for_install(conn: asyncpg.Connection, bundle: Bundle) -> ImportReport:
    """§10 step 1, with the install in the picture. Writes nothing.

    §10 makes validate the first step and §6.6 makes it the Data tab's decision point, so every
    refusal an import can raise has to be reachable here — otherwise the operator reads "ok" and
    is refused after they have committed. The three install-state refusals (seed-once,
    models-need-content, vocabulary-change) are facts about this install that no amount of
    reading the bundle can discover, and the identity check needs the installed spine for the
    same reason.
    """
    report = ImportReport(
        bundle_version=bundle.version, vocabulary_version=bundle.vocabulary_version
    )
    await refuse_on_install_state(conn, bundle, report)
    if not report.ok:
        # Same reason `validate_id_partition` stops the report: a bundle that cannot be imported
        # at this install *at all* gets the one line the operator can act on, not that line
        # buried under thirty table counts.
        return report
    spine = None if bundle.content_db is not None else await installed_spine(conn)
    return validate(bundle, spine=spine)


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


async def import_bundle(
    conn: asyncpg.Connection, bundle: Bundle, artifacts_root: Path, *, activate: bool = True
) -> ImportReport:
    """Validate, load, stage the artifacts, then flip the active row — all in one transaction
    apart from the file copy, which is done before the flip so a failed copy cannot leave the
    DB pointing at files that are not there."""
    report = await validate_for_install(conn, bundle)
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
            "BUNDLE.json at the bundle root has no usable `bundle_version` — it names the "
            "artifact directory, so it must be a plain [A-Za-z0-9._-] token",
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

    db = (
        None if bundle.content_db is None
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
                bundle.kind,
                report.vocabulary_version,
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
                report.note(
                    "rebuild-set", "recomputed against the staged bundle: " + "; ".join(REBUILD_SET)
                )
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
        if db is not None:
            db.close()

    return report


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
