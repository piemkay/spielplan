"""The bundle lifecycle: which ids this app owns, and which imports it will accept.

Spec v2.1 §4.1 (canonical key), §8 stage 1, §10 (the swap sequence), §12 (M2 exit criterion);
decisions 162 and 163 in `docs/spec-v2.2-proposals.md`.

Integration, against a real Postgres, because three of the four rules under test are enforced by
the schema as well as by the importer — `title_id_seq`'s floor, `artifact_bundle_one_seed`, the
`kind` CHECK. A rule that only the application enforces is one a restart, a concurrent import or
a developer with psql walks straight around, and decision 162's claim is that the collision is
impossible *by construction* rather than contingent on the corpus standing still.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
import lzma
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

import asyncpg
import numpy as np
import pytest

from spielplan.importer import bundle as bundle_import
from spielplan.importer.bundle import APP_ID_MIN
from spielplan.importer.report import ImportReport
from spielplan.placement import reconcile
from spielplan.scoring import serve
from tests.fixtures import make_bundle as fx

# The number the id partition exists because of. `content.sqlite`'s `sqlite_sequence` reads
# `title 21442`, so the corpus's next minted title id is this one — which is exactly where
# "mint above the imported maximum" would have started this app (decision 162).
CORPUS_NEXT_TITLE_ID = 21443


@pytest.fixture
def build(tmp_path):
    """Build a bundle at a version. `models_only` is decision 162's re-import shape.

    A models-only bundle is not a broken one: "the corpus supplies the trained artifacts", and
    content is exported once. The fixture builds the seed bundle, so the models-only bundle is
    that one minus the two content databases — which is what the exporter's models-only mode
    produces.
    """
    def make(version: str, *, models_only: bool = False) -> Path:
        root = tmp_path / version
        fx.make_bundle(root, version=version)
        if models_only:
            (root / "content.sqlite").unlink()
            (root / "reviews.sqlite").unlink()
            # BUNDLE.json is the corpus's inventory of the tree and M4.14 reads it before the
            # first row is written, so a bundle made models-only by deleting two files it still
            # lists is a bundle whose own manifest no longer describes it. Re-inventoried, so
            # this fixture goes on producing exactly the shape decision 162 describes and not a
            # second, accidental defect beside it. [M4.14 step B1]
            fx.reinventory(root)
        return root
    return make


@pytest.fixture
def artifacts_root(tmp_path) -> Path:
    """§10 stage 2's target: `/data/artifacts/<version>/`. Absent until something stages."""
    return tmp_path / "artifacts"


async def import_bundle_at(db, root: Path, artifacts_root: Path) -> ImportReport:
    return await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), artifacts_root
    )


async def seed(db, build, artifacts_root, version: str = "test-v1") -> ImportReport:
    """The one content import this install will ever accept (decision 162)."""
    report = await import_bundle_at(db, build(version), artifacts_root)
    assert report.ok, report.render()
    return report


def failures_of(report: ImportReport, rule: str) -> list[str]:
    return [f.message for f in report.failures if f.rule == rule]


# --- the id-bearing artifacts. No `break_` helper exists for these; they live here. -----------
#
# decision 162: `backbone.npz`, `review_text_emb.npz`, `seed_list.json`, `corrections_v1.tsv`
# and `dna_vocab/<v>/adjudications_v1.tsv` are all keyed by corpus `title.id` and all travel in
# `artifacts/`, so a *models-only* bundle can reach into this app's range long after content
# stopped arriving. Each helper below pokes one app-range id into one of them and touches
# nothing else.


def break_person_id_in_app_range(root: Path, app_min: int) -> None:
    """`person.id` is minted by the corpus too, and the contract's `p:<role>:<name>` columns are
    keyed by name — so a person collision is invisible in the feature vector as well as in the
    spine, and only the boundary can catch it."""
    import sqlite3

    db = sqlite3.connect(root / "content.sqlite")
    db.execute("UPDATE person SET id = ? WHERE id = 6", (app_min + 5,))
    db.commit()
    db.close()
    fx.reinventory(root)


def break_backbone_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "backbone.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    ids = arrays["title_ids"].astype(np.int64)
    ids[-1] = app_min + 3
    arrays["title_ids"] = ids.astype(np.int32)
    np.savez(path, **arrays)
    fx.reinventory(root)


def break_review_text_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "review_text_emb.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    ids = arrays["title_ids"].astype(np.int64)
    ids[-1] = app_min + 4
    arrays["title_ids"] = ids.astype(np.int32)
    np.savez(path, **arrays)
    fx.reinventory(root)


def break_seed_list_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["title_id"] = app_min + 1
    path.write_text(json.dumps(entries), encoding="utf-8")
    fx.reinventory(root)


def break_corrections_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "corrections_v1.tsv"
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    rows[0]["title_id"] = str(app_min + 2)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    fx.reinventory(root)


def break_adjudications_title_id_in_app_range(root: Path, app_min: int) -> None:
    """`dna_vocab/<v>/adjudications_v1.tsv` is the fifth id-bearing artifact and travels with the
    models: its real header is (scope, title_id, term, action, target, quote, source, note), so a
    `scope = title` row names a corpus `title.id` exactly as `corrections_v1.tsv` does."""
    path = root / "artifacts" / "dna_vocab" / "v1" / "adjudications_v1.tsv"
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    rows[0]["title_id"] = str(app_min + 6)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    fx.reinventory(root)


ID_SOURCES = (
    ("content.sqlite title.id", fx.break_title_id_in_app_range),
    ("content.sqlite person.id", break_person_id_in_app_range),
    ("backbone.npz title_ids", break_backbone_title_id_in_app_range),
    ("review_text_emb.npz title_ids", break_review_text_title_id_in_app_range),
    ("seed_list.json title_id", break_seed_list_title_id_in_app_range),
    ("corrections_v1.tsv title_id", break_corrections_title_id_in_app_range),
    ("dna_vocab/v1/adjudications_v1.tsv title_id", break_adjudications_title_id_in_app_range),
)


# --- platform-app-minted-ids-are-partitioned-from-the-corpus -----------------------------------


async def test_a_fresh_install_mints_from_a_range_the_corpus_cannot_reach(db):
    """decision 162, against the arithmetic M4.5 rejected.

    The proposal was "seed a sequence above the imported maximum". On a fresh install that has
    not seeded there IS no imported maximum, so the mint would start at 1 and the first acquired
    title would take corpus title 1's id; on a seeded one it would start at 21443, which is the
    id the corpus mints next. The floor is in the migration precisely so that neither is
    reachable before a seed has run.
    """
    title_id = await db.fetchval(
        "INSERT INTO title (kind, name) VALUES ('movie', 'acquired from Jellyfin') RETURNING id"
    )
    person_id = await db.fetchval(
        "INSERT INTO person (name) VALUES ('acquired from TMDB') RETURNING id"
    )
    assert title_id >= APP_ID_MIN
    assert person_id >= APP_ID_MIN
    assert title_id not in (1, CORPUS_NEXT_TITLE_ID)


async def test_the_seed_import_positions_the_mint_and_the_migration_does_not(
    db, build, artifacts_root
):
    """decision 162: "the sequence is positioned by the seed import rather than by the migration".

    A migration cannot do it: it runs against an empty `title`, so `setval(max(id))` would yield
    1. And a floor alone is not enough to be sure the seed positioned anything — so the seeded
    maximum is measured, shown to be far below the floor (which is what makes "above the imported
    maximum" the wrong rule), and the mint is then required to clear both.
    """
    assert await db.fetchval("SELECT nextval('title_id_seq')") == APP_ID_MIN, (
        "the migration must leave the mint at the floor, not at max(id) of an empty table"
    )

    report = await seed(db, build, artifacts_root)
    assert [f.detail["sequence"] for f in report.findings if f.rule == "id-partition"] == [
        "title_id_seq", "person_id_seq"
    ]

    seeded_max = await db.fetchval("SELECT max(id) FROM title")
    assert seeded_max + 1 < APP_ID_MIN, "the corpus's ids live below the floor, verbatim"

    minted = await db.fetchval(
        "INSERT INTO title (kind, name) VALUES ('movie', 'acquired later') RETURNING id"
    )
    assert minted > seeded_max
    assert minted >= APP_ID_MIN
    assert await db.fetchval("SELECT nextval('person_id_seq')") >= APP_ID_MIN


async def test_the_partition_is_a_constraint_not_a_convention(db):
    """The importer's refusal is only half of it: §8 stage 1 acquires titles through this same
    sequence, and a sequence whose MINVALUE is the floor cannot be walked back below it by
    anything — a mis-positioning, a concurrent import, or a developer with psql."""
    with pytest.raises(asyncpg.PostgresError):
        await db.execute("SELECT setval('title_id_seq', 21442)")
    assert await db.fetchval("SELECT nextval('title_id_seq')") >= APP_ID_MIN


@pytest.mark.parametrize(("source", "break_it"), ID_SOURCES, ids=[s for s, _ in ID_SOURCES])
async def test_an_id_reaching_into_the_app_range_is_refused_and_named(build, source, break_it):
    """decision 162: the importer refuses a bundle reaching into the app's range, naming the id.

    Both directions in one test, because either half alone is unfalsifiable: a clean bundle
    passing proves nothing if the check reads a file that is not there, and a broken bundle
    failing proves nothing if the check fails on everything. Six sources, each broken alone.
    """
    root = build("test-v1")
    clean = bundle_import.validate_id_partition(
        bundle_import.Bundle.open(root), ImportReport()
    )
    assert failures_of(clean, "id-partition") == []

    break_it(root, APP_ID_MIN)
    broken = bundle_import.validate_id_partition(
        bundle_import.Bundle.open(root), ImportReport()
    )
    messages = failures_of(broken, "id-partition")
    assert len(messages) == 1, messages
    assert source in messages[0]
    offending = [f.detail["ids"] for f in broken.failures if f.rule == "id-partition"][0]
    assert all(i >= APP_ID_MIN for i in offending)
    assert str(offending[0]) in messages[0], "the report must name the id, not just the file"


async def test_the_id_partition_is_checked_before_anything_else_in_the_bundle(build):
    """A bundle whose ids are not this install's produces one line and stops.

    Every other line of a §10 migration report — the per-table counts, the shared-pair count,
    the identity check — is a statement *about* title ids, so a report that goes on to make them
    is describing a namespace the operator does not have. `rule7-denylist` is `validate_content`'s
    first finding on every bundle, and its absence is how this test knows nothing else ran.
    """
    root = build("test-v1")
    fx.break_title_id_in_app_range(root, APP_ID_MIN)
    report = bundle_import.validate(bundle_import.Bundle.open(root))

    assert not report.ok
    assert [f.rule for f in report.failures] == ["id-partition"]
    assert [f.rule for f in report.findings if f.rule == "rule7-denylist"] == []


# --- platform-content-seeds-once-models-reimport -----------------------------------------------


async def test_a_models_only_bundle_is_a_bundle(build):
    """decision 162: a re-import carries models, not content. `bundle.py` refused any bundle
    without `content.sqlite`, which made the standing upstream unusable — the corpus supplies
    trained artifacts and the app has to be able to take them."""
    seed_bundle = bundle_import.Bundle.open(build("test-v1"))
    assert seed_bundle.kind == "seed"
    assert seed_bundle.content_db is not None

    model = bundle_import.Bundle.open(build("test-v2", models_only=True))
    assert model.kind == "model"
    assert model.content_db is None
    assert model.version == "test-v2"          # BUNDLE.json, not artifacts/manifest.json
    assert model.vocabulary_version == "v1"

    report = bundle_import.validate(model)
    assert report.ok, report.render()
    assert any("models-only" in f.message for f in report.findings)


async def test_the_content_seed_is_accepted_exactly_once(db, build, artifacts_root):
    """decision 162: "movie data is exported once and imported once".

    The refusal names the reason because the operator's next move follows from it: a second
    content import would upsert the corpus's rows over ids this install now owns, and what they
    actually want is either a models-only re-import or a restore.
    """
    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)

    second = await import_bundle_at(db, build("test-v2"), artifacts_root)

    assert not second.ok
    assert len(failures_of(second, "seed-once")) == 1
    assert "test-v1" in failures_of(second, "seed-once")[0]
    # "rather than upserting": nothing was written, and nothing was staged either.
    assert [r["version"] for r in await db.fetch("SELECT version FROM artifact_bundle")] == [
        "test-v1"
    ]
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    assert not (artifacts_root / "test-v2").exists()


async def test_exactly_one_content_seed_survives_a_developer_with_psql(db, build, artifacts_root):
    """The rule is about the whole table's history, so it is an index and not an `if`.

    `artifact_bundle_one_seed` is what makes "already seeded" true after a restart and during a
    concurrent import — the two cases in which application logic has nothing to read.
    """
    await seed(db, build, artifacts_root)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, report, state, kind) "
            "VALUES ('smuggled', '{}'::jsonb, '{}'::jsonb, 'validated', 'seed')"
        )
    with pytest.raises(asyncpg.PostgresError):
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, report, state, kind) "
            "VALUES ('nonsense', '{}'::jsonb, '{}'::jsonb, 'validated', 'partial')"
        )


async def test_a_models_only_bundle_imports_and_hot_swaps_without_content(
    db, build, artifacts_root
):
    """§10's swap sequence, run by a bundle that carries no content at all.

    The rebuild set is the reason `bundle_version` is stamped on placements, priors and scores —
    "everything expressed in the old Backbone's basis is garbage against a new one" — so a model
    update is exactly the import that needs all four of its steps, and it must run them without
    touching a single content row.
    """
    await seed(db, build, artifacts_root)
    titles_before = [
        dict(r) for r in await db.fetch("SELECT id, name, kind, is_owned FROM title ORDER BY id")
    ]

    swap = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)
    assert swap.ok, swap.render()

    rebuilt = [f.message.split(":")[0] for f in swap.findings if f.rule == "rebuild"]
    assert rebuilt == list(reconcile.REBUILD_SET)
    assert (artifacts_root / "test-v2" / "backbone.npz").is_file()

    rows = await db.fetch(
        "SELECT version, kind, state, vocabulary_version FROM artifact_bundle ORDER BY version"
    )
    assert [(r["version"], r["kind"], r["state"]) for r in rows] == [
        ("test-v1", "seed", "superseded"),
        ("test-v2", "model", "active"),
    ]
    # "the two are distinguishable in artifact_bundle so the admin board can say which kind of
    # import produced a version" — including the vocabulary each one carried.
    assert {r["vocabulary_version"] for r in rows} == {"v1"}

    assert [
        dict(r) for r in await db.fetch("SELECT id, name, kind, is_owned FROM title ORDER BY id")
    ] == titles_before


# --- platform-vocabulary-change-is-refused-not-degraded ----------------------------------------


async def dna_snapshot(db) -> dict[str, list[dict]]:
    """The active vocabulary, both DNA tiers, and every title's DNA card, as rows."""
    return {
        "vocabulary": [dict(r) for r in await db.fetch(
            "SELECT version, facet_count, term_count FROM dna_vocabulary ORDER BY version")],
        "extracted": [dict(r) for r in await db.fetch(
            "SELECT id, title_id, version, term, facet, salience FROM dna_tag ORDER BY id")],
        "projected": [dict(r) for r in await db.fetch(
            "SELECT id, title_id, version, term, facet, weight FROM dna_projected ORDER BY id")],
        "cards": [dict(r) for r in await db.fetch(
            "SELECT title_id, version, term, facet, tier FROM dna_tagged "
            "ORDER BY title_id, tier, term")],
    }


async def test_a_vocabulary_change_is_refused_and_nothing_moves(db, build, artifacts_root):
    """decision 163: a vocabulary change is a migration, not an import.

    The deferral is only honest if it cannot bite silently, and this one could: a models-only
    bundle shipping `dna_vocab/v2` would leave `dna_tag` and `dna_projected` on v1 while the
    feature builder filters on the active version, so both DNA blocks would be empty for every
    title — and empty is not an error anywhere in the read path. Hence the snapshot: the refusal
    has to leave the naming layer exactly as it found it, not merely fail to improve it.
    """
    await seed(db, build, artifacts_root)
    before = await dna_snapshot(db)
    assert before["cards"], "the seed has to have loaded a naming layer for this to mean anything"

    root = build("test-v2", models_only=True)
    fx.break_vocabulary_version(root, "v2")
    bundle = bundle_import.Bundle.open(root)
    assert bundle.vocabulary_version == "v2"

    report = await bundle_import.import_bundle(db, bundle, artifacts_root)

    assert not report.ok
    message = failures_of(report, "vocabulary-migration")
    assert len(message) == 1, [f.rule for f in report.failures]
    assert "v1 -> v2" in message[0], message[0]
    assert "does not exist yet" in message[0]

    # "nothing is staged, flipped or written."
    assert not (artifacts_root / "test-v2").exists()
    assert [(r["version"], r["state"]) for r in await db.fetch(
        "SELECT version, state FROM artifact_bundle")] == [("test-v1", "active")]
    assert await dna_snapshot(db) == before


async def test_the_refusal_is_specific_to_the_change_not_to_the_check(db, build, artifacts_root):
    """"A bundle carrying the same vocabulary version imports normally."

    Two models-only bundles into the same install, differing in one key. Without this half the
    refusal above is satisfied by a check that refuses every models-only bundle it is shown.
    """
    await seed(db, build, artifacts_root)

    same = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)
    assert same.ok, same.render()
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'test-v2'"
    ) == "active"

    changed_root = build("test-v3", models_only=True)
    fx.break_vocabulary_version(changed_root, "v2")
    changed = await import_bundle_at(db, changed_root, artifacts_root)
    assert len(failures_of(changed, "vocabulary-migration")) == 1


# --- platform-backup-restore-ordering-is-explicit ----------------------------------------------


async def test_a_model_bundle_into_an_install_with_no_content_is_refused_before_it_writes(
    db, build, artifacts_root
):
    """§10's swap sequence has one correct order, and the wrong one is refused rather than
    half-applied.

    "Before it writes anything" is a claim about ordering, not about the transaction: §10 stages
    by copying the artifacts tree, and that copy happens outside it. So the refusal has to come
    before the copy, and the assertion is that the staging directory does not exist.
    """
    assert await db.fetchval("SELECT count(*) FROM title") == 0

    report = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)

    assert not report.ok
    message = failures_of(report, "ordering")
    assert len(message) == 1, [f.rule for f in report.failures]
    assert "no content" in message[0]
    assert "restore or seed" in message[0]

    assert not artifacts_root.exists()
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0


async def test_the_right_order_leaves_every_owned_title_with_a_coordinate(
    db, build, artifacts_root
):
    """"After a correct restore-then-load … §12's M2 exit criterion reads the same number."

    The criterion is a count of owned titles with no coordinate, and it reads zero both when
    everything is placed and when there is nothing to place — so the owned count is asserted
    alongside it. Reading the same zero over a library that has quietly become empty is the
    failure the ordering refusal above exists to prevent.
    """
    await seed(db, build, artifacts_root)

    owned = await db.fetchval("SELECT count(*) FROM title WHERE is_owned")
    assert owned == len(fx.TITLES) > 0
    after_restore = await reconcile.placement_counts(db, bundle_version="test-v1")
    assert after_restore["owned_unplaced"] == 0
    assert await serve.uncoordinated_owned(db, kind="movie", bundle_version="test-v1") == []
    assert await serve.uncoordinated_owned(db, kind="series", bundle_version="test-v1") == []

    swap = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)
    assert swap.ok, swap.render()

    after_load = await reconcile.placement_counts(db, bundle_version="test-v2")
    assert after_load["owned"] == owned
    assert after_load["owned_unplaced"] == 0
    assert await serve.uncoordinated_owned(db, kind="movie", bundle_version="test-v2") == []
    assert await serve.uncoordinated_owned(db, kind="series", bundle_version="test-v2") == []
    # The same number, over the same library — not a zero that a lost catalog also produces.
    assert (after_load["owned_warm"], after_load["owned_cold"]) == (
        after_restore["owned_warm"], after_restore["owned_cold"]
    )


# --- platform-model-bundle-identity-is-checked-not-trusted ------------------------------------


def reidentify_backbone_row(root: Path, token: str = "imdb:tt9999999") -> None:
    """decision 162 — a models-only bundle whose identity column names a different film.

    The case the identity column exists for, and the only one nothing else can see: the corpus
    *merging* two titles changes what an id MEANS without changing the id, so `title_ids` still
    ascends and the range partition still holds. Row 0 is title 1, `Heat`.
    """
    path = root / "artifacts" / "backbone.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    tokens = arrays["title_identity"].astype("<U64")
    tokens[0] = token
    arrays["title_identity"] = tokens
    np.savez(path, **arrays)
    fx.reinventory(root)


async def test_a_models_only_bundle_s_identity_is_checked_against_the_installed_spine(
    db, build, artifacts_root
):
    """decision 162's identity column, checked on the only bundle kind that will ever arrive.

    A models-only re-import carries no `content.sqlite`, so the spine the check compares against
    is the installed one or there is none — and a check that silently does not run on the only
    bundles the corpus will ever export again is not a check. The note counts the rows it
    compared, because "no identity failure" is also what a skipped check produces.
    """
    await seed(db, build, artifacts_root)

    root = build("test-v2", models_only=True)
    assert not (root / "content.sqlite").exists()
    report = await import_bundle_at(db, root, artifacts_root)

    assert report.ok, report.render()
    # TWO notes, and they are two different facts about the same vector. The forward one is this
    # check's own: every row identifies the title the spine says it does. The second is decision
    # 248's reverse half, which `bundle.py` supplies from the ACTIVE backbone's coverage - and it
    # is the direction that is actually the refusal, because an id the install never seeded is
    # ordinary while a coordinate that stops existing is a merged or dropped corpus row.
    identity = [f for f in report.findings if f.rule == "identity" and f.severity == "note"]
    assert len(identity) == 2, [f.as_dict() for f in report.findings if f.rule == "identity"]
    checked = next(f for f in identity if "identify the title" in f.message)
    assert checked.detail["rows"] == len(fx.BACKBONE_TITLES)
    assert any("coverage does not go backwards" in f.message for f in identity)


async def test_a_re_identified_backbone_row_is_refused_against_the_installed_spine(
    db, build, artifacts_root
):
    """The negative half: without it, the note above is satisfied by a check that compares
    nothing. The refusal names the title because the operator's next move is to ask the corpus
    what happened to that film."""
    await seed(db, build, artifacts_root)

    root = build("test-v2", models_only=True)
    reidentify_backbone_row(root)
    report = await import_bundle_at(db, root, artifacts_root)

    assert not report.ok
    message = failures_of(report, "identity")
    assert len(message) == 1, [f.rule for f in report.failures]
    assert "tt9999999" in message[0] and "Heat" in message[0]

    # §10 stages by copying the artifacts tree, outside the transaction — so "refused" has to
    # mean the copy never happened, not that the DB rolled back.
    assert not (artifacts_root / "test-v2").exists()
    assert [(r["version"], r["state"]) for r in await db.fetch(
        "SELECT version, state FROM artifact_bundle")] == [("test-v1", "active")]


# --- platform-content-seeds-once-models-reimport: the seed's row is not overwritable ----------


def reuse_version(root: Path, version: str) -> None:
    """Point a bundle's BUNDLE.json at a version string an earlier import already used."""
    path = root / "BUNDLE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bundle_version"] = version
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


async def test_a_models_only_bundle_cannot_take_over_the_seed_s_row(db, build, artifacts_root):
    """decision 162: "already seeded" is a fact about the table's history, so nothing may erase it.

    `artifact_bundle` is keyed by version and the import upserts on it, so a models-only bundle
    reusing the seed's version string rewrote that row's `kind` to 'model' — and the seed-once
    refusal, which asks for a row of kind 'seed', then had nothing to find. The active-bundle
    check does not cover it: once a later model bundle has been imported the seed row is
    'superseded', not 'active'.
    """
    await seed(db, build, artifacts_root)
    swap = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)
    assert swap.ok, swap.render()
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "superseded"

    impostor = build("test-v3", models_only=True)
    reuse_version(impostor, "test-v1")
    report = await import_bundle_at(db, impostor, artifacts_root)

    assert not report.ok
    message = failures_of(report, "seed-once")
    assert len(message) == 1, [f.rule for f in report.failures]
    assert "test-v1" in message[0]

    # The seed's record survives, and so therefore does the refusal that reads it.
    assert await db.fetchval(
        "SELECT kind FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "seed"
    second_seed = await import_bundle_at(db, build("test-v4"), artifacts_root)
    assert failures_of(second_seed, "seed-once"), second_seed.render()


# --- §10 step 1: the operator's pre-flight validate is the decision point ----------------------


async def test_validate_refuses_a_second_content_seed_before_the_operator_commits(
    db, build, artifacts_root
):
    """§10 makes validate step 1, and §6.6 makes it the Data tab's decision point.

    A refusal that only fires at import tells the operator after they have committed. All three
    install-state refusals are facts about this install that no amount of reading the bundle can
    discover, so validate has to be given the connection and run them.
    """
    await seed(db, build, artifacts_root)

    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2"))
    )

    assert not report.ok
    assert len(failures_of(report, "seed-once")) == 1


async def test_validate_refuses_a_model_bundle_into_an_install_with_no_content(db, build):
    """The second of the three, on the install where it is the operator's whole question:
    nothing has been seeded, so §10's rebuild set has nothing to place."""
    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2", models_only=True))
    )

    assert not report.ok
    assert len(failures_of(report, "ordering")) == 1


async def test_the_data_tab_s_validate_route_runs_the_install_state_refusals(
    app, db, build, artifacts_root
):
    """The wiring, through the route the operator actually presses.

    `validate_bundle` called a synchronous validator that takes no connection, so the report the
    Data tab renders could not contain a refusal that depends on the install — and the operator
    read "ok" for an import that was going to be refused.
    """
    await seed(db, build, artifacts_root)
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201

    response = await admin.post(
        "/api/admin/bundle/validate", json={"path": str(build("test-v2"))}
    )

    assert response.status_code == 200, response.text
    payload = response.json()["report"]
    assert payload["ok"] is False
    assert [f["rule"] for f in payload["findings"] if f["severity"] == "fail"] == ["seed-once"]


async def test_validate_refuses_a_bundle_whose_ledger_constants_the_fit_cannot_use(
    db, build, artifacts_root
):
    """§10 step 1, for §4.3's constants file — the one bundle file nothing used to read here.

    `break_straddle_z` is §6.3's threshold at zero, which `hyperparams.from_mapping` refuses. The
    refusal had no catcher anywhere: the validator never opened `ledger_hyperparams.json`, so the
    bundle passed step 1, got staged, had the active row flipped onto it, and the `ValueError`
    then surfaced out of a *request handler* — every Rate write and every Rank board, for every
    member, after the operator had committed and gone home. §10 makes validate the decision point
    precisely so a bundle this install cannot serve never becomes the active one.

    Both halves are asserted because each covers for the other: a clean bundle's report has to
    carry the line proving the check ran at all, and the broken one's has to name the key and
    leave `artifacts/<version>/` absent.
    """
    seeded = await seed(db, build, artifacts_root)
    assert [
        f.message for f in seeded.findings
        if f.rule == "hyperparams" and f.severity == "note"
    ], "a clean bundle's report must show the constants were read: " + seeded.render()

    broken = build("test-v2", models_only=True)
    fx.break_straddle_z(broken)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(broken))

    assert not report.ok
    messages = failures_of(report, "hyperparams")
    assert len(messages) == 1, [f.rule for f in report.failures]
    assert "straddle_z" in messages[0], messages[0]

    # And the import the operator would have pressed next refuses in the same words, with §10
    # step 2 never reached: a staged directory left behind by a bundle that was then refused is
    # the state the swap sequence exists to make impossible.
    attempted = await import_bundle_at(db, broken, artifacts_root)
    assert failures_of(attempted, "hyperparams"), attempted.render()
    assert not (artifacts_root / "test-v2").exists(), "a refused bundle may not be staged"

    # A file that is not a JSON object at all — the class the check could not see. `from_mapping`
    # met it with an `AttributeError`, which is neither the `ValueError` this section catches nor
    # the `json.JSONDecodeError` above it, so there was no report line and no failure: the operator
    # pressing Validate on the Data tab got a 500 from a button whose only job is to report, because
    # `api/artifacts.py` wraps this call in no try and `app.py` registers no handler for it.
    # [M4.10 cycle 1, M410-R1-03]
    shapeless = build("test-v3", models_only=True)
    (shapeless / "artifacts" / "ledger_hyperparams.json").write_text("[]", encoding="utf-8")
    fx.reinventory(shapeless)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(shapeless))

    assert not report.ok
    messages = failures_of(report, "hyperparams")
    assert len(messages) == 1, [f.rule for f in report.failures]
    assert "JSON object" in messages[0], messages[0]

    # And a constants path that is present but unopenable, which `is_file()` read as an ABSENT
    # optional artifact: no failure, and not even the "constants read" note, while the install ran
    # on DEFAULTS under a different `hp_digest` — every cached fit discarded behind a number nobody
    # chose. §4.3's optional file is the one thing this check may pass silently, and a directory of
    # that name is not it. [M4.10 cycle 1, M410-R1-06]
    shadowed = build("test-v4", models_only=True)
    constants = shadowed / "artifacts" / "ledger_hyperparams.json"
    constants.unlink()
    constants.mkdir()
    fx.reinventory(shadowed)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(shadowed))

    assert not report.ok
    assert failures_of(report, "hyperparams"), report.render()

    # And the attribution: a truncated `artifacts/manifest.json` with byte-perfect constants. The
    # section reached `hyperparams.load` through `ArtifactStore.open`, which re-parses the manifest,
    # so the manifest's own `JSONDecodeError` came back a second time under the `hyperparams` rule —
    # the operator is told two files are broken, opens a valid one, and finds every constant in
    # range. That is the misattribution this section's own comment was written to prevent one file
    # type over ("send the operator looking for a key that is not the problem"), on the surface §10
    # makes the decision point. `validate_artifacts` owns the manifest line and already named it.
    # [M4.10 cycle 2, m410-c2-validate-blames-the-constants-for-a-broken-manifest]
    mangled = build("test-v5", models_only=True)
    (mangled / "artifacts" / "manifest.json").write_text("{not json", encoding="utf-8")
    # Each of these four fixtures breaks ONE file, and BUNDLE.json is the corpus's inventory of
    # the tree: re-inventoried, so the report's only failure is the one the block is about rather
    # than that failure beside a sha256 mismatch this test created. [M4.14 step B1]
    fx.reinventory(mangled)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(mangled))

    assert not report.ok
    assert [f.rule for f in report.failures] == ["artifacts"], (
        "one broken file, two failures: " + report.render()
    )
    assert "manifest.json" in failures_of(report, "artifacts")[0]
    assert failures_of(report, "hyperparams") == [], (
        "the constants file is valid and the report blames it for the manifest's parse error: "
        + report.render()
    )
    assert [
        f.message for f in report.findings
        if f.rule == "hyperparams" and f.severity == "note"
    ], "the constants were never checked at all: " + report.render()


# --- data-rules-an-archive-that-does-not-open-leaves-nothing-behind ---------------------------
#
# The `.tar` and `.tar.zst` doors had no test at any layer before M4.14. Every import test in
# this repository hands `Bundle.open` a directory, which is the one shape that never unpacks, so
# the branch an operator actually uses — copy the export in, press Validate — was reached by
# nothing. These six are the first, and each asserts the same two things: what the operator is
# told, and that nothing survives beside the archive that a retry could mistake for a bundle.
# [M4.14 step A3, finding 2.5]


def tarred(root: Path, target: Path) -> Path:
    """The shape an operator drops into `/data/import`: one archive holding the bundle."""
    with tarfile.open(target, "w") as tar:
        tar.add(root, arcname=root.name)
    return target


def trees_beside(archive: Path) -> list[str]:
    """Every extraction directory `_unpack` could have left, complete or half-written."""
    return sorted(
        p.name for p in archive.parent.iterdir()
        if p.is_dir() and (p.name.startswith(".unpacked-") or p.name.startswith(".unpacking-"))
    )


def truncate(archive: Path, fraction: float) -> Path:
    """A transfer that stopped: the same bytes, fewer of them."""
    payload = archive.read_bytes()
    archive.write_bytes(payload[: int(len(payload) * fraction)])
    return archive


def test_a_truncated_archive_leaves_no_tree_and_is_refused_the_same_way_twice(build, tmp_path):
    """The reproduction: `target.mkdir()` ran before `tarfile.open()` and nothing cleaned up.

    Measured on the real corpus tar cut at 90%: the FIRST `validate` raised and the SECOND
    returned `ok=True` over a 332-of-436 MB `reviews.sqlite`, because `_unpack` reused any
    existing `.unpacked-<stem>` without asking whether it was complete. With the fixture cut
    inside `reviews.sqlite` the import returned 200, flipped the seed active and loaded zero
    review bodies behind a single warn. Decision 162 and `artifact_bundle_one_seed` then make
    that corrupt seed the household's only content import, forever — the redo is a dropped
    database.

    So the assertion is on the SECOND attempt as much as the first: an archive that does not
    open has to be refused the same way every time, which is only true if the first attempt
    leaves nothing behind. [M4.14 step A3, finding 2.5]
    """
    archive = truncate(tarred(build("test-v1"), tmp_path / "v1.tar"), 0.6)

    refusals = []
    for _ in ("first", "second"):
        with pytest.raises((tarfile.TarError, EOFError, bundle_import.BundleOpenError)) as caught:
            bundle_import.Bundle.open(archive)
        refusals.append(type(caught.value).__name__)
        assert trees_beside(archive) == [], (
            "a half-extracted tree survived the failure, and the next attempt opens it as a bundle"
        )

    assert refusals[0] == refusals[1], f"the two attempts were refused differently: {refusals}"


async def test_a_seed_with_an_empty_naming_layer_imports_the_way_validate_said_it_would(
    db, build, artifacts_root
):
    """Decision 262: the two guards over one question asked it two different ways.

    `validate.py` refuses a missing `dna_vocab/` only when the bundle SHIPS DNA rows, and warns
    otherwise, in as many words: an empty naming layer is legal, and `test_bundle_validation.py`
    registers exactly that bundle as `report.ok`. The import-side guard asked a different
    question - it was conditioned on the bundle being a SEED - so the one bundle the validator
    calls legal was refused after `load_content` had run, with a sentence reading "this bundle
    carries DNA rows and names no vocabulary version" about a bundle whose `dna_tag` and
    `dna_projected` counts are both zero. That sentence could never be true where it printed:
    the validator fails any bundle that has rows AND no tree, so the only bundles reaching it
    had none.

    The corpus exporting a content seed before the DNA extraction pass has run is the case, and
    on a fresh install decision 256's refusal is inert (there is no active vocabulary to
    compare), so nothing upstream catches it either. The row records NULL, which is what this
    install's naming layer is. [M4.14 cycle 1, m414-c1-import-only-vocabulary-refusal]
    """
    # First, the bundle the validator DOES refuse: DNA rows and no tree to name them from. The
    # refusal reaches the import path, names the vocabulary rule, and stages nothing.
    with_rows = build("test-v2")
    shutil.rmtree(with_rows / "artifacts" / "dna_vocab")
    fx.reinventory(with_rows)
    refused = await import_bundle_at(db, with_rows, artifacts_root)
    assert not refused.ok
    assert failures_of(refused, "vocabulary"), refused.render()
    assert not (artifacts_root / "test-v2").exists()

    root = build("test-v1")
    shutil.rmtree(root / "artifacts" / "dna_vocab")
    content = sqlite3.connect(root / "content.sqlite")
    content.executescript(
        "DELETE FROM dna_tag; DELETE FROM dna_projected; DELETE FROM dna_evidence;"
    )
    content.commit()
    content.close()
    fx.reinventory(root)

    report = await import_bundle_at(db, root, artifacts_root)

    assert report.ok, report.render()
    assert "carries DNA rows" not in report.render()
    assert await db.fetchval("SELECT count(*) FROM title") > 0
    row = await db.fetchrow(
        "SELECT state, vocabulary_version FROM artifact_bundle WHERE version = 'test-v1'"
    )
    assert row["state"] == "active"
    assert row["vocabulary_version"] is None, (
        "the row records the naming layer this bundle arrived under, and it arrived under none"
    )
    assert await db.fetchval("SELECT count(*) FROM dna_vocabulary") == 0


async def test_curated_ledgers_naming_a_vocabulary_this_install_lacks_are_skipped_with_a_line(
    db, build, artifacts_root
):
    """decision 265: the FK the models-only branch reaches on section 3.1's empty naming layer.

    `dna_adjudication.version` is `NOT NULL REFERENCES dna_vocabulary(version)`, and
    `dna.load_vocabulary` - the only writer of that table in the whole backend - runs on the seed
    branch alone. Decision 247 newly calls `load_adjudications` from the models-only branch,
    where the parent row is not guaranteed; decision 262 makes an install with no
    `dna_vocabulary` row at all legal and reachable, and decision 256's refusal is guarded on
    `active_vocab` being set, so it asks such an install nothing. `validate_for_install` returned
    ok - the Data tab said the bundle was good - and the import then died mid-transaction on
    `dna_adjudication_version_fkey`, after the 205 MB copy, with the generic PostgresError
    backstop firing where a rule should have named the refusal first.

    Skipped rather than refused, because a refusal would be permanent: decision 162 makes the
    naming layer fillable only by a content import and seed-once forbids a second one, so every
    model bundle the corpus ever ships would meet it. Nothing is lost by skipping - the install
    has no DNA rows for these verdicts to be about. [M4.14 cycle 2, decision 265]
    """
    root = build("test-v1")
    shutil.rmtree(root / "artifacts" / "dna_vocab")
    content = sqlite3.connect(root / "content.sqlite")
    content.executescript(
        "DELETE FROM dna_tag; DELETE FROM dna_projected; DELETE FROM dna_evidence;"
    )
    content.commit()
    content.close()
    fx.reinventory(root)
    seeded = await import_bundle_at(db, root, artifacts_root)
    assert seeded.ok, seeded.render()
    assert await db.fetchval("SELECT count(*) FROM dna_vocabulary") == 0

    # The corpus runs the extraction pass and ships the vocabulary with the models, which under
    # decision 162 is the only way that layer could ever be filled.
    report = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)

    assert report.ok, report.render()
    skipped = [
        f.message for f in report.findings
        if f.rule == "vocabulary" and "has no row for" in f.message
    ]
    assert len(skipped) == 1, report.render()
    assert "dna_vocab/v1/" in skipped[0] and "decision 265" in skipped[0]
    assert await db.fetchval("SELECT count(*) FROM dna_adjudication") == 0
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'test-v2'"
    ) == "active", "the model bundle was refused on a layer it could never fill"


async def test_a_bundle_that_names_a_vocabulary_and_ships_no_tree_says_which_ledgers_it_skipped(
    db, build, artifacts_root
):
    """decision 266: decision 256's own remediation, taken literally, was silent about two of the
    four ledgers.

    "Export the bundle with `vocabulary_version` in BUNDLE.json, or ship its `dna_vocab/<version>/`
    tree" - take the first branch and the bundle imports green while `load_adjudications` and
    `load_axes` are never called at all: both are gated on that tree, so neither loader's own
    absent-file warning can fire and decision 247's "one line per absence" held with ZERO lines
    for half of them. The only line the report carried was the validator's, and it said "the
    naming layer will be empty" - false about the install, whose naming layer was intact, and a
    sentence that function has no connection with which to make. Both halves are asserted here.
    [M4.14 cycle 2, m414-c2-dim247-declared-vocabulary-no-tree, decision 266]
    """
    await seed(db, build, artifacts_root)
    before = (
        await db.fetchval("SELECT count(*) FROM dna_adjudication"),
        await db.fetchval("SELECT count(*) FROM dna_axis_weight"),
    )
    assert all(before), "the fixture ships both ledgers for this to have something to lose"

    silent = build("test-v2", models_only=True)
    shutil.rmtree(silent / "artifacts" / "dna_vocab")
    fx.reinventory(silent)
    fx.break_vocabulary_version(silent, "v1")
    assert bundle_import.Bundle.open(silent).vocabulary_version == "v1"

    report = await import_bundle_at(db, silent, artifacts_root)

    assert report.ok, report.render()
    skipped = [
        f.message for f in report.findings
        if f.rule == "vocabulary" and "ships no dna_vocab/v1/ tree" in f.message
    ]
    assert len(skipped) == 1, report.render()
    assert "left in place and not re-applied" in skipped[0]
    assert (
        await db.fetchval("SELECT count(*) FROM dna_adjudication"),
        await db.fetchval("SELECT count(*) FROM dna_axis_weight"),
    ) == before
    assert "the naming layer will be empty" not in report.render(), (
        "the validator said something about an install it has no connection to read"
    )


def header_offset(archive: Path, nth: int = 2) -> int:
    """The byte at which the `nth`-from-last member's 512-byte header block begins.

    The cut this fixture makes is not arbitrary. `tarfile` swallows `EOFHeaderError`,
    `TruncatedHeaderError` and `EmptyHeaderError` at any offset but 0 and returns from `next()`
    as if the archive had ended, so a truncation that lands ON a header block is the one class
    `extractall` returns NORMALLY from. Members are 512-aligned by construction, which is exactly
    what a block-aligned short write produces - `dd`, `head -c`, ENOSPC on whole blocks, an
    interrupted `rsync --partial`.
    """
    with tarfile.open(archive) as tar:
        return [m.offset for m in tar][-nth]


def test_a_truncation_at_a_member_boundary_is_refused_rather_than_extracted(build, tmp_path):
    """The class the 60% cut above cannot see: a cut that lands on a member header.

    `_unpack`'s stated rule is that a `.unpacked-<name>/` directory is a COMPLETE extraction,
    always - and `extractall` returning normally on a header-aligned truncation made that false.
    Measured on a fixture archive: 36 of 38 members extracted, no exception, `os.replace`
    promoted the partial tree, and the reuse check passed on the next attempt because
    `BUNDLE.json` is the FIRST member in the tar and therefore always survives. The bundle then
    read as `kind='model'`, so on a first boot the operator was told "this is a models-only
    bundle and the install has no content: restore or seed movie data first" - the
    wrong-path-diagnosed-as-a-data-modelling-problem decision 257 exists to eliminate - on every
    attempt, because the poisoned tree is reused and named in no report.

    The measurement is the end-of-archive marker, which is one predicate over the whole class and
    needs no manifest: a conforming tar ends in two zero blocks (1,024 bytes), and every
    truncation leaves fewer than one block past the last header the iteration read.
    [M4.14 cycle 1, m414-c1-dim-refusals-01]
    """
    archive = tarred(build("test-v1"), tmp_path / "v1.tar")
    whole = archive.read_bytes()
    archive.write_bytes(whole[: header_offset(archive)])

    for _ in ("first", "second"):
        with pytest.raises(bundle_import.BundleOpenError) as caught:
            bundle_import.Bundle.open(archive)
        assert "end-of-archive" in str(caught.value)
        assert str(caught.value).isascii()
        assert trees_beside(archive) == [], (
            "a header-aligned truncation extracted silently and left a tree the retry reuses"
        )

    archive.write_bytes(whole)             # the operator's next move: copy it again
    assert bundle_import.Bundle.open(archive).content_db is not None


@pytest.mark.parametrize("compress", ["gz", "xz"])
def test_a_compressed_tar_is_not_refused_as_a_truncated_one(build, tmp_path, compress):
    """The end-of-archive marker is a fact about the TAR, not about how it was carried.

    The seekable branch measured `archive.stat().st_size - tar.offset`, where the size is the
    file's and the offset counts bytes of the DECODED stream - so for any transparently
    decompressed archive the subtraction is negative and a whole, valid bundle came back as "has
    no end-of-archive marker - it is a truncated or still-copying transfer ... Copy the archive
    again and retry": a refusal that is both wrong and unsatisfiable, since every copy answers
    the same. `api/artifacts._resolve` admits any name ending `.tar` and `tarfile.is_tarfile` is
    true of a gzip, bzip2 and xz stream alike, so `tar czf v20260828.tar <bundle>` reaches it.
    The streaming branch had it right all along and this is the same predicate.

    The truncation half is asserted on the SAME compression, because a predicate that passes
    everything would pass this test too: the plain tar is cut on a member header first and then
    compressed, so `tarfile` decompresses cleanly and returns from `extractall` as though the
    archive had ended - the one class `extractall` cannot raise on.
    [M4.14 cycle 2, m414-c2-refusals-04]
    """
    plain = tarred(build("test-v1"), tmp_path / "plain.tar")
    whole = plain.read_bytes()
    cut = whole[: header_offset(plain)]

    def compressed(payload: bytes, name: str) -> Path:
        """The same tar bytes, carried inside a compression tarfile decodes transparently - and
        under a `.tar` name, because that is the name `_resolve` admits."""
        target = tmp_path / name
        target.write_bytes((gzip if compress == "gz" else lzma).compress(payload))
        return target

    good = compressed(whole, "good.tar")
    assert bundle_import.Bundle.open(good).content_db is not None
    assert trees_beside(good) == [f".unpacked-{good.name}"]

    short = compressed(cut, "short.tar")
    with pytest.raises(bundle_import.BundleOpenError) as caught:
        bundle_import.Bundle.open(short)
    assert "end-of-archive" in str(caught.value)
    assert trees_beside(short) == [f".unpacked-{good.name}"], "a truncated archive left a tree"


def test_an_archive_with_a_traversal_member_leaves_no_tree(build, tmp_path):
    """`filter="data"` refuses the traversal member — AFTER the members before it have landed.

    This is the failure mode that makes the cleanup load-bearing rather than tidy: the refusal
    is correct and the tree it leaves is a partial extraction of a hostile archive, which the
    retry then opens as a real bundle. The good member is added first on purpose, so the
    extraction is genuinely part-done when it raises.
    """
    root = build("test-v1")
    archive = tmp_path / "traversal.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(root / "BUNDLE.json", arcname="test-v1/BUNDLE.json")
        tar.add(root / "BUNDLE.json", arcname="../escaped.json")

    with pytest.raises(tarfile.TarError):
        bundle_import.Bundle.open(archive)

    assert trees_beside(archive) == []
    assert not (tmp_path / "escaped.json").exists(), "the traversal member itself escaped"


def test_a_regular_file_pointed_at_as_a_bundle_is_a_four_hundred(build, tmp_path):
    """A typo in the path is a typo, and it was an HTTP 500 with junk left on the host disk.

    `Bundle.open` treated every non-directory as an archive, so a README reached `tarfile.open`,
    the `ReadError` escaped as a 500 that `BundleImport.svelte` renders as a bare string, an
    empty `.unpacked-README/` was left behind, and the SECOND click was diagnosed as "this is a
    models-only bundle and the install has no content" — a decision-162 refusal for a typo.

    The status code is `api/artifacts.py`'s: it maps `BundleOpenError` to 400 and renders
    `err.message`, which is why this carries a sentence rather than a class name. What this
    layer owns is that the refusal exists, is a `ValueError` the route can catch, names the file,
    and is the same answer twice. [M4.14 step A3, finding 2.5]
    """
    readme = tmp_path / "README.txt"
    readme.write_text("this is not a bundle", encoding="utf-8")

    for _ in ("first", "second"):
        with pytest.raises(bundle_import.BundleOpenError) as caught:
            bundle_import.Bundle.open(readme)
        assert isinstance(caught.value, ValueError), "the route catches this as a 400"
        assert "README.txt" in str(caught.value)
        assert str(caught.value).isascii()
        assert trees_beside(readme) == []


def test_a_good_archive_of_the_same_name_validates_after_a_failed_one(build, tmp_path):
    """The operator's actual next move: transfer it again, over the same filename.

    Keying the extraction on the archive's name is what makes this a real risk — the retry
    resolves to the same `.unpacked-v1.tar/`, so a stale tree from the failed transfer is
    exactly the thing that would be read instead of the bytes they just copied.
    """
    root = build("test-v1")
    archive = truncate(tarred(root, tmp_path / "v1.tar"), 0.6)
    with pytest.raises((tarfile.TarError, EOFError, bundle_import.BundleOpenError)):
        bundle_import.Bundle.open(archive)

    tarred(root, archive)                  # the same name, the whole file this time

    bundle = bundle_import.Bundle.open(archive)
    assert bundle.version == "test-v1"
    assert bundle.content_db is not None
    report = bundle_import.validate(bundle)
    assert report.ok, report.render()
    assert trees_beside(archive) == [".unpacked-v1.tar"]


async def test_a_bundle_staged_under_the_artifacts_root_is_refused_before_anything_is_deleted(
    db, build, artifacts_root, tmp_path
):
    """A7: `/data/artifacts/<version>/` is the one directory the Data tab names by version.

    So it is the obvious place for an operator to put a bundle, `_resolve` accepts it (it is
    under DATA_DIR), and `import_bundle` then computed `staged = artifacts_root / version`, found
    it existing, and `rmtree`d it: `content.sqlite`, `reviews.sqlite`, `BUNDLE.json` and
    `artifacts/` deleted before the first byte was copied, `copytree` raising `FileNotFoundError`
    as a 500, and the gigabyte gone. Validation had passed.

    Both halves are asserted, because the refusal has to arrive at §10 step 1 as well: the Data
    tab enables Import on a positive report, so a refusal that only exists inside the import is a
    refusal the operator meets after they have committed. [M4.14 step A7, finding 2.8]
    """
    artifacts_root.mkdir(parents=True, exist_ok=True)
    root = build("test-v1")
    inside = artifacts_root / "test-v1"
    shutil.copytree(root, inside)
    bundle = bundle_import.Bundle.open(inside)

    pre_flight = await bundle_import.validate_for_install(db, bundle, artifacts_root)
    assert not pre_flight.ok, pre_flight.render()
    assert any("inside the artifacts tree" in m for m in failures_of(pre_flight, "bundle"))

    report = await bundle_import.import_bundle(db, bundle, artifacts_root)
    assert not report.ok
    assert (inside / "content.sqlite").is_file(), "the import deleted the bundle it was given"
    assert (inside / "reviews.sqlite").is_file()
    assert (inside / "BUNDLE.json").is_file()
    assert (inside / "artifacts").is_dir()
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 0


def test_the_extraction_a_report_is_about_is_named_in_that_report(build, tmp_path):
    """A reuse is trusted on `BUNDLE.json` alone, so the report has to say which tree it read.

    That trust is deliberate - `_verify_bundle_files` is what catches everything else - but every
    integrity finding names a path INSIDE the bundle and nothing named the `.unpacked-<name>/`
    root. So an extraction damaged while the archive stayed byte-identical (a household clearing
    disk space, an interrupted rsync of the directory, an antivirus quarantine) came back as "the
    bundle is missing files" on the first attempt and identically on every attempt after it, with
    no sentence anywhere telling the operator that a second copy existed or where it was. The
    only cure was deleting a dot-directory nothing had mentioned.

    The tree is not removed, and that is M4.7's dd10 rule rather than an omission: a failed
    import is exactly when the operator retries, and
    `test_a_failed_import_keeps_its_unpacked_tree_for_the_retry` asserts the tree survives one.
    What this milestone owes is the sentence. [M4.14 cycle 2, m414-c2-refusals-06]
    """
    archive = tarred(build("test-v1"), tmp_path / "v20260828.tar")
    whole = archive.read_bytes()

    first = bundle_import.validate(bundle_import.Bundle.open(archive))
    assert first.ok, first.render()
    tree = tmp_path / f".unpacked-{archive.name}"
    assert trees_beside(archive) == [tree.name]
    assert any(str(tree) in f.message for f in first.findings), first.render()

    (tree / "test-v1" / "artifacts" / "backbone.npz").unlink()
    assert archive.read_bytes() == whole, "this test damages the extraction, not the archive"

    second = bundle_import.validate(bundle_import.Bundle.open(archive))

    assert not second.ok, second.render()
    assert any("artifacts/backbone.npz" in m for m in failures_of(second, "bundle-integrity"))
    named = [f.message for f in second.findings if str(tree) in f.message]
    assert named, (
        "the report is about a tree beside the archive and no line says which: " + second.render()
    )
    assert "read from the extraction" in named[0], named[0]


async def test_validating_for_an_install_leaves_the_event_loop_answering(db, build, tmp_path):
    """Decision 287, and the one measure of this milestone's exit criterion it did not meet.

    `validate` is a plain synchronous function - the 1.04 GB sha256 pass and `load_tower`'s torch
    import are both inside it, ~2.97 s straight-line on the real bundle - and
    `validate_for_install` called it bare. Both routes that reach it are `async def`, so FastAPI's
    threadpool never applies and that window was spent on the API process's single event loop.
    `/api/health` acquires a pooled connection on that loop against `app._HEALTH_TIMEOUT_S = 2`,
    so `ops/m414_exit_criterion.py`'s check 9 saw exactly one 503 inside `POST /import`'s
    validation window in every recorded run, and 12/13 was structural rather than flake.

    THE GAP IS THE MEASUREMENT, not the latency, which is check 9's own argument: a blocked loop
    does not answer slowly, it does not answer at all, and a sampler on the same loop cannot
    measure its own starvation. So the sampler here counts what it managed to run, and the
    predicate is the worst interval between two samples.

    `validate` is replaced by a sleep rather than driven on the real bundle, for the reason
    `test_a_cleanup_that_could_not_remove_the_tree_says_so` gives about EACCES: the 1.04 GB
    corpus is not in this suite, and a fixture that validates in 0.9 s would make the assertion a
    statement about this box's disk. A synchronous `time.sleep` is what a CPU- and IO-bound
    stretch looks like to the loop, which is the only property under test - `validate`'s own
    correctness is asserted by every other test in this file, all of which now reach it through
    the offload. [M4.14 cycle 4, decision 287, M414-C4-REF-03/04 and m414-c4-e7-01]
    """
    blocked = 0.5
    interval = 0.02

    def slow(bundle, *, spine=None, active_coverage=None):
        time.sleep(blocked)
        return ImportReport(bundle_version=bundle.version)

    gaps: list[float] = []

    async def sampler() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(interval)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(bundle_import, "validate", slow)
    probe = asyncio.ensure_future(sampler())
    try:
        report = await bundle_import.validate_for_install(
            db, bundle_import.Bundle.open(build("test-v1")), tmp_path / "artifacts"
        )
    finally:
        probe.cancel()
        monkeypatch.undo()

    assert report.ok, report.render()
    assert gaps, "the sampler never ran, so this test measured nothing"
    # A sampler that ran through the window and one that was frozen for it are told apart by the
    # worst gap and by the count, exactly as check 9 tells them apart.
    assert max(gaps) < blocked / 2, (
        f"the loop was unavailable for {max(gaps):.2f}s while validate ran: "
        f"{len(gaps)} samples at {interval}s"
    )
    assert len(gaps) >= blocked / interval / 2, f"{len(gaps)} samples over {blocked}s"


def test_an_extraction_is_not_destroyed_before_the_archive_it_would_be_re_extracted_from(
    build, tmp_path
):
    """The reuse arm's own comment states the premise it never checked.

    "The archive it came from is right here, so it is removed rather than reported" - but the
    `archive.is_file()` test sat EIGHT LINES BELOW that `rmtree`, outside the `target.is_dir()`
    block. So an operator who had opened a BUNDLE.json-less extraction once, deleted the 1.04 GB
    archive to reclaim disk and retried through an ops script lost the extraction too, and was
    answered "<archive> is neither a bundle directory nor a file" - a sentence about a path,
    naming neither the second copy that had existed nor its destruction. Measured on a 358 KB
    extraction: both copies gone, the directory empty, one of them mentioned.

    The shape that reaches the arm is ordinary rather than contrived: `_single_child` returns the
    extraction ROOT for any archive whose members land as more than one top-level entry, and that
    root carries no BUNDLE.json.

    The check belongs INSIDE the arm and not above `if target.is_dir():`, which is the other half
    of this test: hoisting it would take M4.7's dd10 rule with it, because an extraction that DOES
    carry BUNDLE.json is still reused when the archive is gone.
    [M4.14 cycle 4, M414-C4-REF-05]
    """
    root = build("test-v1")
    archive = tmp_path / "transfer.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(root, arcname=root.name)
        tar.add(root / "BUNDLE.json", arcname="NOTES.txt")   # a second top-level entry
    tree = tmp_path / f".unpacked-{archive.name}"

    first = bundle_import.Bundle.open(archive)
    assert first.root == tree, first.root
    assert not (tree / "BUNDLE.json").is_file(), sorted(p.name for p in tree.iterdir())
    sentinel = tree / "the-operators-own-copy"
    sentinel.write_bytes(b"x")

    # The archive is still here, so the arm's premise holds: the tree goes - and it is said out
    # loud, rather than a gigabyte leaving without a sentence on a shape where that happens on
    # every press.
    second = bundle_import.Bundle.open(archive)
    assert not sentinel.exists(), "the stale extraction is the thing the retry has to stop reusing"

    archive.unlink()

    with pytest.raises(bundle_import.BundleOpenError) as refused:
        bundle_import.Bundle.open(archive)

    assert tree.is_dir(), "the only remaining copy was deleted by the refusal that named the other"
    message = str(refused.value)
    assert str(archive) in message and str(tree) in message, message
    assert "left where it is" in message, message

    removed = [line for _, line in second.open_findings if str(tree) in line]
    assert removed, second.open_findings
    assert "was removed" in removed[0], removed[0]


def test_an_archive_dropped_in_the_import_directory_is_found_by_the_default_path(build, tmp_path):
    """decision 257: `/data/import` with one archive in it IS a path to that archive.

    `api/artifacts.py` resolves an empty `path` to `cfg.import_dir` and this module opened that
    directory as the bundle root, so the only layout the default understood was CI's — where
    `make_bundle` writes `BUNDLE.json` straight into `data/import`. README's first-boot line and
    the Data tab's placeholder both tell an operator to copy `v20260828.tar.zst` in there, and
    what came back was `ok=false` carrying decision 162's ordering refusal: a wrong-path
    diagnosis dressed as a data-modelling one, on the first screen of a first boot.

    Two archives is a refusal naming the count rather than a guess, and it is a REPORT line
    because there is a bundle-shaped question to answer and an operator to answer it.
    """
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    tarred(build("test-v1"), import_dir / "v20260828.tar")

    bundle = bundle_import.Bundle.open(import_dir)
    assert bundle.version == "test-v1"
    report = bundle_import.validate(bundle)
    assert report.ok, report.render()
    assert any("v20260828.tar was opened as the bundle" in f.message for f in report.findings)

    tarred(build("test-v2"), import_dir / "v20260901.tar")
    crowded = bundle_import.validate(bundle_import.Bundle.open(import_dir))
    assert not crowded.ok
    assert any("2 archives" in m for m in failures_of(crowded, "bundle"))


async def test_a_wrong_path_is_diagnosed_as_a_path_on_both_install_states(
    db, build, artifacts_root, tmp_path
):
    """Step A6's refusal, and decision 257's, reach the operator on an install that has state.

    Both live inside `validate()`, and `validate_for_install` returns the install-state report
    alone when that pass refuses - so on the two install states that actually exist they were
    discarded unread. A first boot has no titles, so every models-only-shaped path (and a
    directory that is not a bundle at all reads as one: no `content.sqlite`) came back with
    decision 162's "restore or seed movie data first", which is a data-modelling instruction for
    a typo, on the first screen of a first boot. A seeded install has a vocabulary, so the same
    paths came back with decision 256's "this bundle declares no DNA vocabulary version".

    Both install states are exercised in one test because they are the same defect twice and the
    fix is one ordering: what the path IS precedes what the install is.
    [M4.14 cycle 2, m414-c2-refusals-02, decision 257]

    The third path is the same class arriving from the other side: HALF of section 4.3's two
    things. A6's predicate refused a directory with neither, so a copy that brought BUNDLE.json
    and not the 205 MB `artifacts/` tree - an rsync cut short, a tar extracted through a filter, a
    restore that copied the small files first - walked past it into the same two data-modelling
    refusals, and step B1's line naming the forty `artifacts/*` entries the manifest still lists
    was discarded unread by the short-circuit above it. Nothing importable is refused by the wider
    test: decision 251 makes `cold_tower.pt`, `feature_contract.json` and `backbone.npz`
    validation failures when absent, and all three live in that directory.
    [M4.14 cycle 3, M414-C3-VOCAB-02]
    """
    shapeless = tmp_path / "backups"
    shapeless.mkdir()
    (shapeless / "nightly.sql").write_text("-- a pg_dump, not a bundle\n", encoding="utf-8")
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    tarred(build("test-v1"), import_dir / "v20260828.tar")
    tarred(build("test-v2"), import_dir / "v20260901.tar")
    half_copied = tmp_path / "half-copied"
    half_copied.mkdir()
    shutil.copy2(build("test-v3") / "BUNDLE.json", half_copied / "BUNDLE.json")

    async def refusals(path: Path) -> list[str]:
        report = await bundle_import.validate_for_install(
            db, bundle_import.Bundle.open(path), artifacts_root
        )
        assert not report.ok, report.render()
        assert not failures_of(report, "ordering"), report.render()
        assert not failures_of(report, "vocabulary-migration"), report.render()
        return failures_of(report, "bundle")

    assert not await db.fetchval("SELECT count(*) FROM title"), "this half is the first boot"
    assert any("is not a bundle" in m for m in await refusals(shapeless))
    assert any("2 archives" in m for m in await refusals(import_dir))
    assert any("no artifacts/" in m for m in await refusals(half_copied))

    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT version FROM dna_vocabulary") == "v1"

    assert any("is not a bundle" in m for m in await refusals(shapeless))
    assert any("2 archives" in m for m in await refusals(import_dir))
    assert any("no artifacts/" in m for m in await refusals(half_copied))


# --- platform-a-broken-install-says-so-and-can-be-restaged ------------------------------------
#
# `/data/artifacts` is a host bind mount outside the nightly `pg_dump`, so "database restored,
# files missing" is a realistic recovery state — and it had no cure: re-importing the seed is
# refused by seed-once, re-importing the active model bundle by "already the active bundle", and
# no route restages. The only repair was an undocumented manual copy. [M4.14 step D2, finding 2.17]


async def test_re_importing_the_active_bundle_with_no_files_on_disk_restages_it(
    db, build, artifacts_root
):
    """The repair path, end to end: same bundle, same version, files back, rebuild set re-run."""
    root = build("test-v1")
    await seed(db, build, artifacts_root)
    shutil.rmtree(artifacts_root / "test-v1")
    assert not (artifacts_root / "test-v1").exists()

    repair = await import_bundle_at(db, root, artifacts_root)

    assert repair.ok, repair.render()
    assert [f.message for f in repair.findings if f.rule == "restage"], "no restage was reported"
    assert (artifacts_root / "test-v1" / "backbone.npz").is_file()
    # §10's rebuild set runs against the restaged files: the fitted numbers were expressed in a
    # basis whose files were gone, and putting the files back is only half the repair.
    rebuilt = [f.message.split(":")[0] for f in repair.findings if f.rule == "rebuild"]
    assert rebuilt == list(reconcile.REBUILD_SET)
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert [(r["version"], r["state"]) for r in rows] == [("test-v1", "active")]


async def test_the_restage_branch_loads_no_content_and_never_clears_the_active_row(
    db, build, artifacts_root
):
    """The two things the repair must not do, asserted rather than assumed.

    It must not re-load content — the active row is the proof that this bundle's content was
    already imported, and running the loaders again would be a second content import wearing a
    repair's name, which is the one thing decision 162 forbids. And it must not clear the active
    row to get past "already the active bundle": that row is the provenance a restore needs, and
    §10 gives no process but the importer the right to move it.
    """
    root = build("test-v1")
    await seed(db, build, artifacts_root)
    titles_before = [
        dict(r) for r in await db.fetch("SELECT id, name, kind, is_owned FROM title ORDER BY id")
    ]
    activated_before = await db.fetchval(
        "SELECT activated_at FROM artifact_bundle WHERE version = 'test-v1'"
    )
    shutil.rmtree(artifacts_root / "test-v1")

    repair = await import_bundle_at(db, root, artifacts_root)

    assert repair.ok, repair.render()
    assert not [k for k in repair.table_counts if k.startswith("loaded:")], (
        f"the restage loaded content: {sorted(repair.table_counts)}"
    )
    assert [
        dict(r) for r in await db.fetch("SELECT id, name, kind, is_owned FROM title ORDER BY id")
    ] == titles_before
    row = await db.fetchrow(
        "SELECT state, kind, activated_at FROM artifact_bundle WHERE version = 'test-v1'"
    )
    assert (row["state"], row["kind"]) == ("active", "seed")
    assert row["activated_at"] >= activated_before
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 1


async def test_a_models_only_restage_keeps_the_seed_row_s_kind_and_seed_once_still_refuses(
    db, build, artifacts_root
):
    """The restage repairs the row that exists; it does not re-describe the import that made it.

    Both tests above restage from `build("test-v1")` - a SEED bundle - so `EXCLUDED.kind` is
    'seed' and the assertion one function up passes for the wrong reason. The models-only copy is
    the natural repair rather than the exotic one: a restage keys on the version, so the bundle
    an operator points at MUST declare the active version, and while that active row is still the
    SEED row - every install from first boot until its first model swap - the copy that carries
    the `artifacts/` tree needing restaging is the 205 MB models-only half. `content.sqlite` plus
    `reviews.sqlite` are 790 MB of the 1.04 GB the restage does not use, and `/data/import` is
    routinely emptied.

    `import_bundle` wrote `kind = EXCLUDED.kind` from `bundle.kind`, which is derived from
    whether a `content.sqlite` is present, so that repair rewrote the seed row to 'model'.
    `refuse_on_install_state`'s seed-once query asks for the oldest row of kind 'seed' and then
    found none, `artifact_bundle_one_seed` - the partial unique index that makes "exactly one
    content seed, ever" survive a restart and a developer with psql - indexed nothing, and the
    next content bundle was accepted and ran `load_content` over the ids this install owns. The
    second half of this test is what that index is for, so it is asserted through the refusal and
    not only through the column.
    [M4.14 cycle 2, m414-c2-dimlock-restage-erases-seed-once, decisions 162 and 258]
    """
    await seed(db, build, artifacts_root)
    shutil.rmtree(artifacts_root / "test-v1")

    repair = await import_bundle_at(db, build("test-v1", models_only=True), artifacts_root)

    assert repair.ok, repair.render()
    assert [f.message for f in repair.findings if f.rule == "restage"], "no restage was reported"
    assert (artifacts_root / "test-v1" / "backbone.npz").is_file()
    row = await db.fetchrow("SELECT state, kind FROM artifact_bundle WHERE version = 'test-v1'")
    assert (row["state"], row["kind"]) == ("active", "seed"), dict(row)
    assert await db.fetchval(
        "SELECT version FROM artifact_bundle WHERE kind = 'seed' "
        "ORDER BY imported_at, version LIMIT 1"
    ) == "test-v1", "seed-once's own question no longer has an answer"

    second = await import_bundle_at(db, build("test-v9"), artifacts_root)

    assert not second.ok, second.render()
    assert failures_of(second, "seed-once"), second.render()
    assert not [k for k in second.table_counts if k.startswith("loaded:")], (
        f"a second content seed loaded content: {sorted(second.table_counts)}"
    )


async def test_a_half_restored_artifacts_directory_is_restaged_rather_than_refused(
    db, build, artifacts_root
):
    """The gate asked whether the directory EXISTS, and a directory is not a bundle.

    README's Backups section documents `cp -a /mnt/backup/artifacts/<version> data/artifacts/` as
    one of the two ways out of "database restored, files missing", and an interrupted copy - or a
    worker killed mid-`copytree` - leaves a directory holding part of the tree. From that moment
    the re-import D3's banner points the operator at answered "bundle <v> is already the active
    bundle" plus seed-once, so the repair D2 shipped was closed by a predicate a half-written
    directory satisfies. It is silent as well as closed: `ArtifactStore.load_active` keys
    `broken` on `root.is_dir()`, so the store reports an ordinary install, `assert_not_broken`
    passes, and every §5.2 fit runs against `Backbone.empty()` and is stamped with the active
    version - M4.13's data-03 loss, reached through the directory this milestone made repairable.
    [M4.14 cycle 3, m414-c3-dimlock-01; decision 258]
    """
    await seed(db, build, artifacts_root)
    staged = artifacts_root / "test-v1"
    for path in sorted(staged.iterdir()):
        if path.name == "manifest.json":
            continue                       # the one file BUNDLE_FILES marks required
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    assert staged.is_dir() and not (staged / "backbone.npz").exists()

    repair = await import_bundle_at(db, build("test-v1"), artifacts_root)

    assert repair.ok, repair.render()
    assert [f.message for f in repair.findings if f.rule == "restage"], "no restage was reported"
    assert (staged / "backbone.npz").is_file()
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert [(r["version"], r["state"]) for r in rows] == [("test-v1", "active")]


# --- data-rules-the-vocabulary-version-has-one-derivation: the refusal half --------------------


async def test_a_bundle_declaring_no_vocabulary_into_an_install_that_has_one_is_refused(
    db, build, artifacts_root
):
    """decision 256. The guard read "if active_vocab AND bundle declares one AND they differ",
    so a bundle declaring nothing skipped decision 163's comparison altogether — and the real
    BUNDLE.json declares nothing: its sixteen top-level keys carry no vocabulary key at all, so
    the whole refusal rested on a `dna_vocab/` directory listing. Decision 162 then narrows every
    future re-import to a models-only bundle, which is exactly the kind least likely to ship that
    tree. Omit it and a v2-trained backbone lands on a v1 install behind a warning: `dna_tag` and
    `dna_projected` stay on v1 while the feature builder filters on v2, both DNA blocks go empty
    for every title, and nothing in the read path calls that an error.

    Asserted against a model bundle this test constructs, because no real one exists: the M4.5
    close-out records that the model re-import path has never run against a real model bundle and
    that is still true. [M4.14 step B5, finding 2.14]
    """
    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT version FROM dna_vocabulary") == "v1"

    silent = build("test-v2", models_only=True)
    shutil.rmtree(silent / "artifacts" / "dna_vocab")
    fx.reinventory(silent)
    bundle = bundle_import.Bundle.open(silent)
    assert bundle.vocabulary_version is None

    pre_flight = await bundle_import.validate_for_install(db, bundle, artifacts_root)
    assert not pre_flight.ok, pre_flight.render()
    refusal = failures_of(pre_flight, "vocabulary-migration")
    assert len(refusal) == 1
    assert "declares no DNA vocabulary version" in refusal[0] and "'v1'" in refusal[0]

    report = await import_bundle_at(db, silent, artifacts_root)
    assert not report.ok
    assert not (artifacts_root / "test-v2").exists(), "a refused bundle staged its artifacts"


async def test_a_bundle_shipping_two_vocabularies_is_told_that_and_not_that_it_declared_none(
    db, build, artifacts_root
):
    """decision 163's refusal, on the one install state that could not read it.

    A bundle whose `dna_vocab/` holds two version directories and whose BUNDLE.json declares
    nothing has `vocabulary_version is None` - `vocab.version_of` raises and `_vocabulary_version`
    carries the refusal as an open finding instead - so decision 256's branch fired, the
    install-state pass short-circuited `validate()`, and the one line that could name the two
    directories was never replayed. What the operator read was "this bundle declares no DNA
    vocabulary version ... or ship its `dna_vocab/<version>/` tree": advice to do what the bundle
    had already done twice. Following the other half of that advice - adding the BUNDLE.json key
    - then produced the real refusal on a second attempt, which is the round trip this asserts
    away. [M4.14 cycle 2, M414-C2-VOCAB-02, decisions 163 and 256]
    """
    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT version FROM dna_vocabulary") == "v1"

    crowded = build("test-v2", models_only=True)
    vocab = crowded / "artifacts" / "dna_vocab"
    shutil.copytree(vocab / "v1", vocab / "v2")
    fx.reinventory(crowded)
    bundle = bundle_import.Bundle.open(crowded)
    assert bundle.vocabulary_version is None, "the fixture declares no vocabulary key"

    report = await bundle_import.validate_for_install(db, bundle, artifacts_root)

    assert not report.ok, report.render()
    named = failures_of(report, "bundle")
    assert any("2 vocabulary versions (v1, v2)" in m for m in named), report.render()
    assert not failures_of(report, "vocabulary-migration"), report.render()


async def test_two_vocabularies_in_the_staged_tree_are_a_broken_install_and_not_a_dead_boot(
    db, build, artifacts_root
):
    """decision 256 made `ArtifactStore.open` RAISE, and the install-side caller had no handler.

    The raise is right where it is: `open` writes no import report, so a sentinel would arrive as
    `None` and be indistinguishable from section 3.1's legal bundle-less install. What was missed
    is that `load_active` calls it bare and `app.py`'s lifespan calls `load_active` bare, while
    every other failure in that same block is caught and degraded - the file's own comment says
    "a boot that dies on it would be the one outcome section 3.1 forbids here", and its
    last-resort `OSError` handler cannot help because a `VocabularyError` is a `RuntimeError`. So
    a staged tree that BOOTED before this milestone stopped booting: no /api/health, no
    healthcheck, no Data tab, and a container restart-looping on the one surface that could
    repair it. The importer cannot produce that tree (`validate` refuses two vocabularies before
    staging and the stage rmtrees before it copies), which is why it is the operator's manual
    restore of /data/artifacts - the procedure decision 258 has this milestone document - that
    reaches it.

    The BROKEN store and not a new state, per decision 258: `is_empty` is True, the model jobs
    refuse rather than fitting in a zero basis, and the ERROR line names the directory and both
    versions. The second half asserts the other bare caller: `active_backbone_coverage` reaches
    `load_active` as an ARGUMENT to `validate(...)`, before any report object exists, so the same
    tree made POST /validate and POST /import raise out of the route - a 500 on the only surface
    that could repair it. [M4.14 cycle 2, M414-C2-VOCAB-01, decisions 256 and 258]
    """
    from spielplan.models.artifacts import ArtifactStore

    await seed(db, build, artifacts_root)
    staged = artifacts_root / "test-v1" / "dna_vocab"
    assert not (await ArtifactStore.load_active(db, artifacts_root)).broken
    shutil.copytree(staged / "v1", staged / "v2")

    store = await ArtifactStore.load_active(db, artifacts_root)

    assert store.broken and store.is_empty
    assert store.version == "test-v1", "the broken store carries the version so a fit is honest"
    # The validate seam, which is the one an operator can still reach on a running install.
    assert await bundle_import.active_backbone_coverage(db, artifacts_root) is None
    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2", models_only=True)), artifacts_root
    )
    assert report.ok, report.render()


async def test_the_recorded_vocabulary_version_is_never_null_after_a_successful_import(
    db, build, artifacts_root
):
    """`artifact_bundle.vocabulary_version` records which naming layer a bundle arrived under.

    It read NULL for every real bundle, because the row was written from the bundle's own
    DECLARATION and no shipped BUNDLE.json declares one. What makes the column trustworthy is
    decision 256 rather than a fallback: a bundle with no version to record, into an install that
    has one, is refused — so a row that exists is a row whose vocabulary was known, and the
    column and `dna_vocabulary` cannot drift apart.

    The models-only half is asserted against a model bundle this test constructs; no real model
    bundle exists to assert against. [M4.14 step B5, decision 256, finding 2.14]
    """
    await seed(db, build, artifacts_root)
    await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)

    active_vocab = await db.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC LIMIT 1"
    )
    rows = await db.fetch("SELECT version, vocabulary_version FROM artifact_bundle ORDER BY version")
    assert [r["version"] for r in rows] == ["test-v1", "test-v2"]
    assert {r["vocabulary_version"] for r in rows} == {active_vocab} == {"v1"}

    # And the bundle that would have written NULL is not stored at all.
    silent = build("test-v3", models_only=True)
    shutil.rmtree(silent / "artifacts" / "dna_vocab")
    fx.reinventory(silent)
    refused = await import_bundle_at(db, silent, artifacts_root)
    assert not refused.ok
    assert await db.fetchval(
        "SELECT count(*) FROM artifact_bundle WHERE vocabulary_version IS NULL"
    ) == 0


# --- data-rules-a-models-only-import-loads-the-curated-ledgers-it-carries ----------------------
#
# `bundle.py`'s `if db is not None:` held `load_corrections`, `load_seed_list` and
# `load_vocabulary`, and under decision 162 the only recurring bundle is models-only — which
# never enters that branch. So the four curated ledgers were inspected by
# `validate_id_partition` off the same bundle and then discarded: measured three times
# independently, `credit_correction` / `seed_list` / `dna_adjudication` / `dna_axis_weight` were
# byte-for-byte unchanged after a models-only import of a bundle whose ledgers all differed,
# `report.table_counts` was `{}` and the report carried no line at all.
# [M4.14 step C2, decision 247, finding 2.15]


def rewrite_curated_ledgers(root: Path, *, keep: int = 3) -> None:
    """Give a model bundle ledgers that differ from the seed's, the way a re-export would.

    A shorter onboarding list, a second credit correction, and one more per-title adjudication:
    three files, three tables, and each of them observable in the database afterwards.
    """
    seeds = json.loads((root / "artifacts" / "seed_list.json").read_text(encoding="utf-8"))
    (root / "artifacts" / "seed_list.json").write_text(
        json.dumps(seeds[:keep]), encoding="utf-8"
    )
    (root / "artifacts" / "corrections_v1.tsv").write_text(
        "kind\ttitle_id\tvalue\tevidence\tnote\n"
        "composer\t8\tKunihiko Murai\thttps://example.invalid/tampopo\tcredited twice upstream\n"
        "director\t1\tMichael Mann\thttps://example.invalid/heat\tre-exported\n",
        encoding="utf-8",
    )
    adjudications = root / "artifacts" / "dna_vocab" / "v1" / "adjudications_v1.tsv"
    adjudications.write_text(
        adjudications.read_text(encoding="utf-8")
        + "title\t2\tmood.dread\tkeep\t\t\ttrakt:comment\tre-exported\n",
        encoding="utf-8",
    )
    fx.reinventory(root)


async def test_a_models_only_import_loads_the_curated_ledgers_and_no_content_tier(
    db, build, artifacts_root
):
    """decision 247 option (A): the four curated ledgers travel with the models and are loaded.

    Asserted against a model bundle this test constructs, because no real model bundle exists —
    the M4.5 close-out recorded that the model re-import path had never run against one, and
    this milestone deliberately does not build that fixture. The bundle is therefore the seed
    minus its two databases, with the three ledgers rewritten the way a re-export would write
    them, which is precisely the shape decision 162 says will arrive again.

    The second half is the boundary the first half must not cross: `load_vocabulary`'s term
    tables, `load_tags` and `load_projected` are the CONTENT tiers, and re-importing those is
    the one thing "content seeds once" forbids.
    """
    await seed(db, build, artifacts_root)
    models = build("test-v2", models_only=True)
    rewrite_curated_ledgers(models)

    before = {
        "terms": await db.fetchval("SELECT count(*) FROM dna_term"),
        "extracted": await db.fetchval("SELECT count(*) FROM dna_tag"),
        "projected": await db.fetchval("SELECT count(*) FROM dna_projected"),
    }
    after_report = await import_bundle_at(db, models, artifacts_root)
    assert after_report.ok, after_report.render()

    assert await db.fetchval("SELECT count(*) FROM credit_correction") == 2
    assert await db.fetchval("SELECT count(*) FROM seed_list") == 3
    assert await db.fetchval(
        "SELECT count(*) FROM dna_adjudication WHERE version = 'v1' AND title_id = 2"
    ) == 1
    assert await db.fetchval("SELECT count(*) FROM dna_axis_weight") > 0

    assert before == {
        "terms": await db.fetchval("SELECT count(*) FROM dna_term"),
        "extracted": await db.fetchval("SELECT count(*) FROM dna_tag"),
        "projected": await db.fetchval("SELECT count(*) FROM dna_projected"),
    }, "a models-only import re-imported a content tier"
    assert not [k for k in after_report.table_counts if k.startswith("loaded:title")]


async def test_a_shorter_onboarding_list_replaces_the_old_one_rather_than_merging(
    db, build, artifacts_root
):
    """`load_seed_list` upserted `ON CONFLICT (position)` with no preceding clear.

    Executed against the fixture's eight-entry list, a three-entry refresh left EIGHT rows with
    positions 3-7 still naming the old ids — and `rate/queue.py` joins and counts the whole
    table, so §6.1's first run would have offered a merge of two onboarding lists, at a row count
    a correct load never produces. Wired onto the model path by decision 247, that merge is what
    every re-import would have shipped.
    """
    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)

    models = build("test-v2", models_only=True)
    rewrite_curated_ledgers(models, keep=3)
    report = await import_bundle_at(db, models, artifacts_root)
    assert report.ok, report.render()

    rows = await db.fetch("SELECT position, title_id FROM seed_list ORDER BY position")
    assert [r["position"] for r in rows] == [0, 1, 2], "the old list left its tail behind"
    assert [r["title_id"] for r in rows] == [t[0] for t in fx.TITLES[:3]]


async def test_a_seed_list_entry_naming_an_unknown_title_is_counted_and_skipped(
    db, build, artifacts_root
):
    """decision 247 guard 1, through the import rather than through the loader.

    `seed_list.title_id` is a NOT NULL foreign key to `title(id)`, and decision 248 makes an
    unknown id ORDINARY: the corpus's catalogue is not frozen at this install's seed, so a later
    onboarding list can name a title this household never acquired. Before the guard, one such
    entry aborted the whole import on a constraint name rather than a file name — and with
    decision 247 wiring this loader onto the model path, that is every re-import.
    """
    await seed(db, build, artifacts_root)
    models = build("test-v2", models_only=True)
    seeds = json.loads((models / "artifacts" / "seed_list.json").read_text(encoding="utf-8"))
    seeds.append({**seeds[0], "title_id": 999_001, "title": "a corpus title this install lacks"})
    (models / "artifacts" / "seed_list.json").write_text(json.dumps(seeds), encoding="utf-8")
    fx.reinventory(models)

    report = await import_bundle_at(db, models, artifacts_root)

    assert report.ok, report.render()
    # TWO lines name the id, and they are the two halves of one rule: `validate._validate_seed_list`
    # says the number before the transaction opens, and the loader says it from inside — which is
    # the one that has to be true, because the count is of what was actually written.
    named = [f for f in report.findings if f.rule == "seed-list" and "999001" in f.message]
    assert len(named) == 2, [f.message for f in report.findings if f.rule == "seed-list"]
    loaded = next(f for f in named if "skipped" in f.detail)
    assert loaded.detail["skipped"] == 1 and loaded.detail["title_ids"] == [999_001]
    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)


async def test_an_absent_curated_ledger_leaves_the_installed_rows_standing(
    db, build, artifacts_root
):
    """decision 247's other half: omission is a warning, and a warning changes nothing.

    Three of the four loaders clear before they write — `load_corrections`'s DELETE,
    `load_seed_list`'s (step C1) and decision 261's per-facet DELETE in `load_axes` — and each of
    those clears stands behind an early return that fires when the file is not there. Three
    guards, three functions, and every other test on this row ships the file it asserts about: a
    refactor that moved any one clear in front of its guard would drop the six shipped
    corrections, the hundred-title onboarding list or an authored axis's weights on the next
    re-import, with the whole row still green. `load_adjudications` upserts rather than clearing,
    so its absence is asserted here for §14.5's reason instead — 828 curated verdicts are what a
    silent revert costs, and `bundle.py`'s models-only branch states the rule for all four in one
    sentence: omission must never be destructive.

    All four omitted at once, because that is one export that stopped short rather than four
    independent accidents, and because the row's clause is about the class and not about any one
    file. Against a model bundle this test constructs, because no real one exists.
    [M4.14 cycle 1, M414-REV-247-03, decision 247]
    """
    await seed(db, build, artifacts_root)
    installed = {
        table: await db.fetchval(f"SELECT count(*) FROM {table}")
        for table in (
            "credit_correction", "seed_list", "dna_adjudication", "dna_axis", "dna_axis_weight"
        )
    }
    assert all(installed.values()), f"the fixture stopped shipping a ledger: {installed}"

    models = build("test-v2", models_only=True)
    vocab_dir = models / "artifacts" / "dna_vocab" / "v1"
    (models / "artifacts" / "corrections_v1.tsv").unlink()
    (models / "artifacts" / "seed_list.json").unlink()
    (vocab_dir / "adjudications_v1.tsv").unlink()
    for facet in fx.AXES:
        (vocab_dir / f"{facet}.tsv").unlink()
    fx.reinventory(models)

    report = await import_bundle_at(db, models, artifacts_root)

    assert report.ok, report.render()
    assert installed == {
        table: await db.fetchval(f"SELECT count(*) FROM {table}") for table in installed
    }, "an absent ledger file moved rows"
    # One line per absence, and no more than one: §10's report is what an operator counts the
    # bundle's omissions from, and two lines for one file is the ambiguity M414-REV-247-04 named
    # one requirement down.
    warned: dict[str, list[str]] = {}
    for finding in report.findings:
        if finding.severity == "warn":
            warned.setdefault(finding.rule, []).append(finding.message)
    for rule, named in (
        ("corrections", "corrections_v1.tsv"),
        ("seed-list", "seed_list.json"),
        ("adjudications", "adjudications_v1.tsv"),
        ("axes", "no authored axis definition"),
    ):
        assert len(warned.get(rule, [])) == 1, f"{rule}: {warned.get(rule)}"
        assert named in warned[rule][0], warned[rule][0]


# --- platform-a-model-bundle-may-cover-titles-the-install-never-seeded: the lifecycle half -----


async def test_a_backbone_that_stops_covering_an_installed_bundle_title_is_refused(
    db, build, artifacts_root
):
    """decision 248: the refusal is coverage going BACKWARDS, not an id the install lacks.

    Against a model bundle this test constructs, because no real one exists. A retrained
    `backbone.npz` legitimately covers whatever the corpus had at export time — `sqlite_sequence`
    stood at title 21442 against the 19,071 the bundle exported — and rows for titles this
    install never seeded are inert, because a `Backbone` lookup is by id and never reaches them.
    A title that HAD a coordinate and would stop having one is the other thing entirely: a merged
    or dropped corpus row, after which §5.1 goes on scoring a title from a basis that no longer
    knows it. That is the observable the check is worth having for, and it needs the ACTIVE
    backbone's coverage — a fact only the install can supply, which is why the caller half of
    this check lives in `bundle.py`. [M4.14, decision 248, finding 2.16]
    """
    await seed(db, build, artifacts_root)
    models = build("test-v2", models_only=True)
    path = models / "artifacts" / "backbone.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    dropped = int(arrays["title_ids"][-1])
    keep = arrays["title_ids"] != arrays["title_ids"][-1]
    for name, value in list(arrays.items()):
        if getattr(value, "shape", ()) and value.shape[0] == keep.size:
            arrays[name] = value[keep]
    np.savez(path, **arrays)
    fx.reinventory(models)

    report = await import_bundle_at(db, models, artifacts_root)

    assert not report.ok, report.render()
    refusal = failures_of(report, "identity")
    assert len(refusal) == 1
    assert "not covered by this one" in refusal[0] and str(dropped) in refusal[0]
    assert not (artifacts_root / "test-v2").exists()
