"""The artifact store. Spec v2.1 §4.3, §3.1, §10.

The store is what makes "a bundle-less app is a legal state" true, so the empty case is the
first thing tested here, not an afterthought.
"""

from __future__ import annotations

import json

import pytest

from spielplan.models.artifacts import BUNDLE_FILES, ArtifactStore
from tests.fixtures import make_bundle as fx


@pytest.fixture
def artifacts(tmp_path):
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle" / "artifacts"


# --- §3.1: an empty store is a value, not an error ------------------------------------


def test_empty_store_is_legal_and_says_so():
    store = ArtifactStore.empty()
    assert store.is_empty
    assert store.version is None
    assert store.summary()["version"] is None


def test_empty_store_refuses_to_hand_out_paths():
    """Better a clear error at the call site than a path built from None."""
    with pytest.raises(RuntimeError, match="no artifact bundle loaded"):
        ArtifactStore.empty().path("backbone.npz")


# --- loading a real bundle -------------------------------------------------------------


def test_open_reads_the_manifest_and_the_vocabulary_version(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    assert not store.is_empty
    assert store.version == "test-v1"
    assert store.vocab_version == "v1"
    assert store.summary()["titles"] == len(fx.TITLES)


def test_presence_map_covers_every_declared_bundle_file(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    assert set(store.present) == set(BUNDLE_FILES)
    assert all(store.present.values()), "the fixture is meant to be a complete §4.3 bundle"

    # The absent branch, against a real absence rather than whatever the fixture happens not to
    # ship. It asserted `backbone.npz is False` until M2 gave the fixture a Backbone — at which
    # point it was testing the fixture's contents, not the presence map.
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
    assert contract["text_scale"] == pytest.approx(0.03125)
    assert store.json("feature_contract.json") is contract    # second read is cached


def test_vocabulary_version_falls_back_to_the_directory_when_the_manifest_omits_it(artifacts):
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    del manifest["vocabulary_version"]
    (artifacts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    store = ArtifactStore.open(artifacts, "test-v1")
    assert store.vocab_version == "v1"


# --- §10: no process may score or refit with a bundle other than the active one --------


def test_assert_matches_accepts_the_active_version(artifacts):
    ArtifactStore.open(artifacts, "test-v1").assert_matches("test-v1")


def test_assert_matches_refuses_a_stale_load(artifacts):
    store = ArtifactStore.open(artifacts, "test-v1")
    with pytest.raises(RuntimeError, match="restart backend and worker"):
        store.assert_matches("test-v2")


def test_assert_matches_refuses_scoring_with_no_bundle_at_all():
    with pytest.raises(RuntimeError):
        ArtifactStore.empty().assert_matches("test-v1")
