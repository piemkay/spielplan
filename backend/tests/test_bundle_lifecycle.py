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

import csv
import json
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


def break_backbone_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "backbone.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    ids = arrays["title_ids"].astype(np.int64)
    ids[-1] = app_min + 3
    arrays["title_ids"] = ids.astype(np.int32)
    np.savez(path, **arrays)


def break_review_text_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "review_text_emb.npz"
    arrays = dict(np.load(path, allow_pickle=False))
    ids = arrays["title_ids"].astype(np.int64)
    ids[-1] = app_min + 4
    arrays["title_ids"] = ids.astype(np.int32)
    np.savez(path, **arrays)


def break_seed_list_title_id_in_app_range(root: Path, app_min: int) -> None:
    path = root / "artifacts" / "seed_list.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["title_id"] = app_min + 1
    path.write_text(json.dumps(entries), encoding="utf-8")


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
    checked = [f for f in report.findings if f.rule == "identity" and f.severity == "note"]
    assert len(checked) == 1, [f.as_dict() for f in report.findings if f.rule == "identity"]
    assert checked[0].detail["rows"] == len(fx.BACKBONE_TITLES)


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
