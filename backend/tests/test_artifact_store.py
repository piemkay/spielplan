"""The artifact store (§4.3, §3.1, §10). An empty store is a legal state, tested first."""

from __future__ import annotations

import json

import pytest

from spielplan.importer import vocab
from spielplan.models.artifacts import BUNDLE_FILES, ArtifactStore
from tests.fixtures import make_bundle as fx


@pytest.fixture
def artifacts(tmp_path):
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle" / "artifacts"


def test_empty_store_is_legal_and_says_so():
    store = ArtifactStore.empty()
    assert store.is_empty
    assert store.version is None
    assert store.summary()["version"] is None


def test_empty_store_refuses_to_hand_out_paths():
    with pytest.raises(RuntimeError, match="no artifact bundle loaded"):
        ArtifactStore.empty().path("backbone.npz")


def test_open_reads_the_manifest_and_the_vocabulary_version(artifacts):
    """`artifacts/manifest.json` holds only the fitted
    cut-points, so the version comes from the directory."""
    store = ArtifactStore.open(artifacts, "test-v1")
    assert not store.is_empty
    assert store.version == "test-v1"
    assert store.vocab_version == "v1"
    assert set(store.manifest["fitted_cuts"]) == {str(i) for i in fx.RATING_SOURCE_IDS}


def test_the_store_derives_the_vocabulary_the_same_way_the_bundle_does(artifacts):
    """One derivation (`importer/vocab.version_of`), asserted
    in both shapes: a stray file and two versions."""
    (artifacts / "dna_vocab" / "zz_notes.txt").write_text("keeping v1\n", encoding="utf-8")
    assert vocab.version_of(artifacts) == "v1"
    assert ArtifactStore.open(artifacts, "test-v1").vocab_version == "v1"

    (artifacts / "dna_vocab" / "v2").mkdir()
    with pytest.raises(vocab.VocabularyError):
        vocab.version_of(artifacts)
    with pytest.raises(vocab.VocabularyError):
        ArtifactStore.open(artifacts, "test-v1")


# Written by neither `make_bundle.py` nor this fixture: both files are optional. Named rather
# than counted, so the next file added to either side fails this test.
NOT_IN_THE_FIXTURE = frozenset({"cold_eval.json", "content_summary.json"})


def test_presence_map_covers_every_declared_bundle_file(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    assert set(store.present) == set(BUNDLE_FILES)
    absent = {name for name, there in store.present.items() if not there}
    assert absent == NOT_IN_THE_FIXTURE, (
        f"the fixture is meant to be a complete §4.3 bundle apart from {sorted(NOT_IN_THE_FIXTURE)}"
    )
    assert not (NOT_IN_THE_FIXTURE & {n for n, req in BUNDLE_FILES.items() if req}), (
        "a file the fixture does not ship may not be a REQUIRED one"
    )

    # The absent branch, against a real absence rather than whatever the fixture happens not to ship.
    (artifacts / "cold_tower.pt").unlink()
    stripped = ArtifactStore.open(artifacts, "test-v1")
    assert stripped.present["cold_tower.pt"] is False
    assert stripped.present["manifest.json"] is True


def test_missing_required_lists_only_required_absences(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    assert store.missing_required() == []

    (artifacts / "manifest.json").unlink()
    stripped = ArtifactStore.open(artifacts, "test-v1")
    assert stripped.missing_required() == ["manifest.json"]


def test_json_is_cached_and_returns_the_file(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    contract = store.json("feature_contract.json")
    # §4.3 freezes the review-text scale INSIDE `text_block`; the top level has no such key.
    assert contract["text_block"]["text_scale"] == pytest.approx(2.0)
    assert store.json("feature_contract.json") is contract    # second read is cached


def test_the_vocabulary_version_comes_from_the_directory_and_not_from_the_manifest(artifacts):
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert "vocabulary_version" not in manifest, "the shipped manifest carries no such key"
    assert ArtifactStore.open(artifacts, "test-v1").vocab_version == "v1"

    manifest["vocabulary_version"] = "v9"
    (artifacts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ArtifactStore.open(artifacts, "test-v1").vocab_version == "v1"
    assert vocab.version_of(artifacts) == "v1"


def test_assert_matches_accepts_the_active_version(artifacts):
    ArtifactStore.open(artifacts, "test-v1").assert_matches("test-v1")


def test_assert_matches_refuses_a_stale_load(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    with pytest.raises(RuntimeError, match="restart backend and worker"):
        store.assert_matches("test-v2")


def test_assert_matches_refuses_scoring_with_no_bundle_at_all():
    with pytest.raises(RuntimeError):
        ArtifactStore.empty().assert_matches("test-v1")


def test_the_title_count_comes_from_the_bundles_own_identity_record(artifacts, tmp_path):
    """§10 stages only `artifacts/`, so BUNDLE.json's counts
    reach the store via `artifact_bundle.manifest`."""
    identity = json.loads(
        (tmp_path / "bundle" / "BUNDLE.json").read_text(encoding="utf-8")
    )
    assert "title_count" not in identity, "the corpus records counts under `tables`"

    store = ArtifactStore.open(artifacts, "test-v1", identity=identity)
    assert store.summary()["titles"] == len(fx.TITLES)
    assert identity["tables"]["title"] == len(fx.TITLES)


def test_a_store_nobody_handed_an_identity_reports_no_count_rather_than_a_wrong_one(artifacts):
    """§7.2 re-derives ownership per install, so no bundle carries `owned`."""
    summary = ArtifactStore.open(artifacts, "test-v1").summary()
    assert summary["titles"] is None
    assert "owned" not in summary


def test_a_jsonb_column_handed_back_as_text_still_yields_the_count(artifacts, tmp_path):
    """A connection without the pool's json codec hands back the raw string."""
    raw = (tmp_path / "bundle" / "BUNDLE.json").read_text(encoding="utf-8")
    from spielplan.models.artifacts import _as_mapping

    assert _as_mapping(raw)["tables"]["title"] == len(fx.TITLES)
    assert _as_mapping("not json") == {}
    assert _as_mapping(None) == {}


def test_a_yardstick_that_is_not_utf8_degrades_to_none_rather_than_500ing_three_surfaces(
    artifacts,
):
    """`json()` reads strict UTF-8, and `UnicodeDecodeError` is not a `JSONDecodeError`."""
    good = {"cold": {"spearman": 0.35}, "ceiling": {"spearman": 0.39}}
    (artifacts / "cold_eval.json").write_text(json.dumps(good), encoding="utf-8")
    assert ArtifactStore.open(artifacts, "yard-ok").summary()["cold_eval"] is not None

    # A note typed in a cp1252 editor, and a Windows editor's UTF-16 BOM: neither is UTF-8.
    latin1 = b'{"cold": {"spearman": 0.35}, "ceiling": {"spearman": 0.39}, "note": "caf\xe9"}'
    for payload in (latin1, json.dumps(good).encode("utf-16")):
        (artifacts / "cold_eval.json").write_bytes(payload)
        store = ArtifactStore.open(artifacts, "yard-bad")
        assert store.summary()["cold_eval"] is None
        assert store.cold_eval() is None
