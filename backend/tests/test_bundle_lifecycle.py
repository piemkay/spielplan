"""The bundle lifecycle: which ids this app owns and which imports it accepts (§4.1,
§10, decisions 162, 163). Against Postgres, because the schema enforces these rules
too (`title_id_seq`'s floor, `artifact_bundle_one_seed`). Needs TEST_DATABASE_URL."""

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
from tests.fixtures import make_bundle as fx

# `content.sqlite`'s `sqlite_sequence` reads 21442, so this is the id the corpus mints next.
CORPUS_NEXT_TITLE_ID = 21443


@pytest.fixture
def build(tmp_path):
    """A models-only bundle is decision 162's re-import shape: the seed minus its two content databases."""
    def make(version: str, *, models_only: bool = False) -> Path:
        root = tmp_path / version
        fx.make_bundle(root, version=version)
        if models_only:
            (root / "content.sqlite").unlink()
            (root / "reviews.sqlite").unlink()
            # Re-inventoried, so the only defect is the one decision 162 describes.
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


# Five id-bearing artifacts travel in `artifacts/`; each helper pokes one app-range id into one of them.


def break_person_id_in_app_range(root: Path, app_min: int) -> None:
    """Person columns are keyed by name, so only the boundary can catch a person id collision."""
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
    """A `scope = title` row names a corpus `title.id` exactly as `corrections_v1.tsv` does."""
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


async def test_a_fresh_install_mints_from_a_range_the_corpus_cannot_reach(db):
    """"Above the imported maximum" would start at 1 on a fresh install and at 21443 on a seeded one."""
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
    """A migration runs against an empty `title`, so the
    seed positions the sequence; the mint clears both."""
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
    """MINVALUE makes the floor a constraint no psql session or concurrent import can walk back."""
    with pytest.raises(asyncpg.PostgresError):
        await db.execute("SELECT setval('title_id_seq', 21442)")
    assert await db.fetchval("SELECT nextval('title_id_seq')") >= APP_ID_MIN


@pytest.mark.parametrize(("source", "break_it"), ID_SOURCES, ids=[s for s, _ in ID_SOURCES])
async def test_an_id_reaching_into_the_app_range_is_refused_and_named(build, source, break_it):
    """Clean passes and broken fails, both, so neither half
    is unfalsifiable. Six sources, each broken alone."""
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
    """Every later report line is about title ids, so a foreign namespace produces one line and stops."""
    root = build("test-v1")
    fx.break_title_id_in_app_range(root, APP_ID_MIN)
    report = bundle_import.validate(bundle_import.Bundle.open(root))

    assert not report.ok
    assert [f.rule for f in report.failures] == ["id-partition"]
    assert [f.rule for f in report.findings if f.rule == "rule7-denylist"] == []


async def test_a_models_only_bundle_is_a_bundle(build):
    """Decision 162: the corpus supplies trained artifacts,
    so a bundle without `content.sqlite` is valid."""
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
    """A second content import would upsert corpus rows over ids this install now owns."""
    await seed(db, build, artifacts_root)
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)

    second = await import_bundle_at(db, build("test-v2"), artifacts_root)

    assert not second.ok
    assert len(failures_of(second, "seed-once")) == 1
    assert "test-v1" in failures_of(second, "seed-once")[0]
    # Nothing was written, and nothing was staged either.
    assert [r["version"] for r in await db.fetch("SELECT version FROM artifact_bundle")] == [
        "test-v1"
    ]
    assert await db.fetchval("SELECT count(*) FROM title") == len(fx.TITLES)
    assert not (artifacts_root / "test-v2").exists()


async def test_exactly_one_content_seed_survives_a_developer_with_psql(db, build, artifacts_root):
    """An index, not an `if`: it holds across restarts and concurrent imports."""
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
    """A model update needs all four rebuild steps and must touch no content row."""
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
    # Each row records the vocabulary it carried.
    assert {r["vocabulary_version"] for r in rows} == {"v1"}

    assert [
        dict(r) for r in await db.fetch("SELECT id, name, kind, is_owned FROM title ORDER BY id")
    ] == titles_before


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
    """Decision 163: a v2 vocabulary on v1 tags would silently empty both DNA blocks; nothing may move."""
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
    """Without this half, a check that refuses every models-only bundle would pass."""
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


async def test_a_model_bundle_into_an_install_with_no_content_is_refused_before_it_writes(
    db, build, artifacts_root
):
    """The staging copy happens outside the transaction, so the refusal must come before the copy."""
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
    """The owned count is asserted too: zero unplaced titles also reads over an empty library."""
    await seed(db, build, artifacts_root)

    owned = await db.fetchval("SELECT count(*) FROM title WHERE is_owned")
    assert owned == len(fx.TITLES) > 0
    after_restore = await reconcile.placement_counts(db, bundle_version="test-v1")
    assert after_restore["owned_unplaced"] == 0

    swap = await import_bundle_at(db, build("test-v2", models_only=True), artifacts_root)
    assert swap.ok, swap.render()

    after_load = await reconcile.placement_counts(db, bundle_version="test-v2")
    assert after_load["owned"] == owned
    assert after_load["owned_unplaced"] == 0
    # The same number, over the same library.
    assert (after_load["owned_warm"], after_load["owned_cold"]) == (
        after_restore["owned_warm"], after_restore["owned_cold"]
    )


def reidentify_backbone_row(root: Path, token: str = "imdb:tt9999999") -> None:
    """A merge changes what an id MEANS without changing it; row 0 is title 1, `Heat`."""
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
    """A models-only bundle is checked against the installed spine; the note counts rows compared."""
    await seed(db, build, artifacts_root)

    root = build("test-v2", models_only=True)
    assert not (root / "content.sqlite").exists()
    report = await import_bundle_at(db, root, artifacts_root)

    assert report.ok, report.render()
    # Two notes: the forward identity check, and decision 248's coverage half from the ACTIVE backbone.
    identity = [f for f in report.findings if f.rule == "identity" and f.severity == "note"]
    assert len(identity) == 2, [f.as_dict() for f in report.findings if f.rule == "identity"]
    checked = next(f for f in identity if "identify the title" in f.message)
    assert checked.detail["rows"] == len(fx.BACKBONE_TITLES)
    assert any("coverage does not go backwards" in f.message for f in identity)


async def test_a_re_identified_backbone_row_is_refused_against_the_installed_spine(
    db, build, artifacts_root
):
    """The negative half; the refusal names the title."""
    await seed(db, build, artifacts_root)

    root = build("test-v2", models_only=True)
    reidentify_backbone_row(root)
    report = await import_bundle_at(db, root, artifacts_root)

    assert not report.ok
    message = failures_of(report, "identity")
    assert len(message) == 1, [f.rule for f in report.failures]
    assert "tt9999999" in message[0] and "Heat" in message[0]

    # Staging copies outside the transaction, so "refused" means the copy never happened.
    assert not (artifacts_root / "test-v2").exists()
    assert [(r["version"], r["state"]) for r in await db.fetch(
        "SELECT version, state FROM artifact_bundle")] == [("test-v1", "active")]


def reuse_version(root: Path, version: str) -> None:
    path = root / "BUNDLE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bundle_version"] = version
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


async def test_a_models_only_bundle_cannot_take_over_the_seed_s_row(db, build, artifacts_root):
    """Upserting a model bundle on the seed's version would
    erase the seed row the seed-once refusal reads."""
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

    # The seed's record survives, and so does the refusal that reads it.
    assert await db.fetchval(
        "SELECT kind FROM artifact_bundle WHERE version = 'test-v1'"
    ) == "seed"
    second_seed = await import_bundle_at(db, build("test-v4"), artifacts_root)
    assert failures_of(second_seed, "seed-once"), second_seed.render()


async def test_validate_refuses_a_second_content_seed_before_the_operator_commits(
    db, build, artifacts_root
):
    """Validate is the decision point, so it runs the install-state refusals too."""
    await seed(db, build, artifacts_root)

    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2"))
    )

    assert not report.ok
    assert len(failures_of(report, "seed-once")) == 1


async def test_validate_refuses_a_model_bundle_into_an_install_with_no_content(db, build):
    """On an unseeded install §10's rebuild set has nothing to place."""
    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2", models_only=True))
    )

    assert not report.ok
    assert len(failures_of(report, "ordering")) == 1


async def test_the_data_tab_s_validate_route_runs_the_install_state_refusals(
    app, db, build, artifacts_root
):
    """Through the route the operator presses: the Data
    tab's report must carry install-dependent refusals."""
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
    """A constant `from_mapping` refuses must fail validate, not surface later from request handlers."""
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

    # The import refuses in the same words, with nothing staged.
    attempted = await import_bundle_at(db, broken, artifacts_root)
    assert failures_of(attempted, "hyperparams"), attempted.render()
    assert not (artifacts_root / "test-v2").exists(), "a refused bundle may not be staged"

    # A file that is not a JSON object raised `AttributeError`, a 500 from Validate.
    shapeless = build("test-v3", models_only=True)
    (shapeless / "artifacts" / "ledger_hyperparams.json").write_text("[]", encoding="utf-8")
    fx.reinventory(shapeless)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(shapeless))

    assert not report.ok
    messages = failures_of(report, "hyperparams")
    assert len(messages) == 1, [f.rule for f in report.failures]
    assert "JSON object" in messages[0], messages[0]

    # A directory at the constants path is present-but-unopenable, not an absent optional file.
    shadowed = build("test-v4", models_only=True)
    constants = shadowed / "artifacts" / "ledger_hyperparams.json"
    constants.unlink()
    constants.mkdir()
    fx.reinventory(shadowed)

    report = await bundle_import.validate_for_install(db, bundle_import.Bundle.open(shadowed))

    assert not report.ok
    assert failures_of(report, "hyperparams"), report.render()

    # A truncated `manifest.json` must be named once, under its own rule, not blamed on the constants.
    mangled = build("test-v5", models_only=True)
    (mangled / "artifacts" / "manifest.json").write_text("{not json", encoding="utf-8")
    # Re-inventoried, so the report's only failure is the one this block is about.
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


# The archive doors: what the operator is told, and that nothing survives beside the archive.


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
    """A partial extraction reused on the second attempt
    imported a corrupt seed; refused the same way twice."""
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
    """Decision 262: a seed with an empty naming layer is legal, and imports as validate said."""
    # First the bundle validate DOES refuse: DNA rows and no tree.
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
    """Decision 265: curated ledgers naming an absent
    vocabulary are skipped with a line, not an FK violation."""
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

    # The corpus ships the vocabulary with the models, the only way that layer can be filled.
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
    """Decision 266: a declared version with no tree skips two ledgers, and the report must say which."""
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
    """A cut on a member header is the one truncation `extractall` returns normally from."""
    with tarfile.open(archive) as tar:
        return [m.offset for m in tar][-nth]


def test_a_truncation_at_a_member_boundary_is_refused_rather_than_extracted(build, tmp_path):
    """A conforming tar ends in two zero blocks; a header-aligned cut leaves fewer."""
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
    """The end marker is measured on the DECODED stream; a compressed `.tar` must not read as truncated."""
    plain = tarred(build("test-v1"), tmp_path / "plain.tar")
    whole = plain.read_bytes()
    cut = whole[: header_offset(plain)]

    def compressed(payload: bytes, name: str) -> Path:
        """Under a `.tar` name, because that is the name `_resolve` admits."""
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
    """`filter="data"` refuses after earlier members landed; the partial tree must not survive."""
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
    """A non-archive file is a `BundleOpenError` sentence, the same answer twice."""
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
    """A retry reuses the same `.unpacked-<name>/`, so a stale tree must not be read instead."""
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
    """`import_bundle` would rmtree a bundle stored at `artifacts/<version>/` before copying it."""
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
    """The reused extraction's path is named, so a damaged second copy can be found."""
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
    """Decision 287: `validate` runs in a thread; the sampler's worst gap is the measurement."""
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
    # A frozen sampler and a running one differ by the worst gap and the count.
    assert max(gaps) < blocked / 2, (
        f"the loop was unavailable for {max(gaps):.2f}s while validate ran: "
        f"{len(gaps)} samples at {interval}s"
    )
    assert len(gaps) >= blocked / interval / 2, f"{len(gaps)} samples over {blocked}s"


def test_an_extraction_is_not_destroyed_before_the_archive_it_would_be_re_extracted_from(
    build, tmp_path
):
    """The extraction is removed only when the archive it would be re-extracted from exists."""
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

    # The archive is still here, so the tree goes, and it is said out loud.
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
    """Decision 257: `/data/import` with one archive in it IS a path to that archive; two is a refusal."""
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
    """What the path IS precedes what the install is, including a copy missing `artifacts/`."""
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


# "Database restored, files missing" had no cure until the restage.


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
    # The rebuild set runs against the restaged files.
    rebuilt = [f.message.split(":")[0] for f in repair.findings if f.rule == "rebuild"]
    assert rebuilt == list(reconcile.REBUILD_SET)
    rows = await db.fetch("SELECT version, state FROM artifact_bundle")
    assert [(r["version"], r["state"]) for r in rows] == [("test-v1", "active")]


async def test_the_restage_branch_loads_no_content_and_never_clears_the_active_row(
    db, build, artifacts_root
):
    """The restage loads no content and never clears the active row."""
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
    """A models-only restage must keep the seed row's `kind`, or seed-once stops refusing."""
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
    """A half-copied directory is not a bundle; it is restaged rather than refused."""
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


async def test_a_bundle_declaring_no_vocabulary_into_an_install_that_has_one_is_refused(
    db, build, artifacts_root
):
    """Decision 256: a bundle declaring no vocabulary into an install with one is refused."""
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
    """Two vocabulary directories must be reported as that, not as "declares none"."""
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
    """Two staged vocabularies make a BROKEN store (decision 258), never a dead boot or a 500."""
    from spielplan.models.artifacts import ArtifactStore

    await seed(db, build, artifacts_root)
    staged = artifacts_root / "test-v1" / "dna_vocab"
    assert not (await ArtifactStore.load_active(db, artifacts_root)).broken
    shutil.copytree(staged / "v1", staged / "v2")

    store = await ArtifactStore.load_active(db, artifacts_root)

    assert store.broken and store.is_empty
    assert store.version == "test-v1", "the broken store carries the version so a fit is honest"
    # The validate seam, which an operator can still reach on a running install.
    assert await bundle_import.active_backbone_coverage(db, artifacts_root) is None
    report = await bundle_import.validate_for_install(
        db, bundle_import.Bundle.open(build("test-v2", models_only=True)), artifacts_root
    )
    assert report.ok, report.render()


async def test_the_recorded_vocabulary_version_is_never_null_after_a_successful_import(
    db, build, artifacts_root
):
    """A bundle with no known vocabulary is refused, so a stored row always records one."""
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


# Decision 247: the models-only path used to skip all four curated ledgers.


def rewrite_curated_ledgers(root: Path, *, keep: int = 3) -> None:
    """Three files, three tables, each observable afterwards."""
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
    """The ledgers load; the content tiers do not."""
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
    """An upsert with no clear left a merged onboarding list."""
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
    """Decision 248: an unknown seed-list id is ordinary, so it is counted and skipped."""
    await seed(db, build, artifacts_root)
    models = build("test-v2", models_only=True)
    seeds = json.loads((models / "artifacts" / "seed_list.json").read_text(encoding="utf-8"))
    seeds.append({**seeds[0], "title_id": 999_001, "title": "a corpus title this install lacks"})
    (models / "artifacts" / "seed_list.json").write_text(json.dumps(seeds), encoding="utf-8")
    fx.reinventory(models)

    report = await import_bundle_at(db, models, artifacts_root)

    assert report.ok, report.render()
    # Two lines name the id: the validator's before the transaction and the loader's from inside.
    named = [f for f in report.findings if f.rule == "seed-list" and "999001" in f.message]
    assert len(named) == 2, [f.message for f in report.findings if f.rule == "seed-list"]
    loaded = next(f for f in named if "skipped" in f.detail)
    assert loaded.detail["skipped"] == 1 and loaded.detail["title_ids"] == [999_001]
    assert await db.fetchval("SELECT count(*) FROM seed_list") == len(fx.TITLES)


async def test_an_absent_curated_ledger_leaves_the_installed_rows_standing(
    db, build, artifacts_root
):
    """Omission is a warning that changes nothing: all four absent, and every installed row stands."""
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
    # One line per absence, and no more than one.
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


async def test_a_backbone_that_stops_covering_an_installed_bundle_title_is_refused(
    db, build, artifacts_root
):
    """Decision 248: coverage going BACKWARDS is the refusal, not an id the install lacks."""
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
