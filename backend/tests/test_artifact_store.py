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
    """M4.5: `artifacts/manifest.json` is the ratings-model manifest and carries the fitted
    cut-points and nothing else — no `bundle_version`, no `vocabulary_version`, no title count.
    The bundle's identity lives in `BUNDLE.json` at the bundle ROOT, which never reaches
    `/data/artifacts/<version>/`, so the vocabulary version is read off the directory the
    bundle ships it in."""
    store = ArtifactStore.open(artifacts, "test-v1")
    assert not store.is_empty
    assert store.version == "test-v1"
    assert store.vocab_version == "v1"
    assert set(store.manifest["fitted_cuts"]) == {str(i) for i in fx.RATING_SOURCE_IDS}


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
    # §4.3 freezes the review-text scale INSIDE `text_block`; the top level has no such key,
    # and reading it there found nothing on every bundle the corpus has exported.
    assert contract["text_block"]["text_scale"] == pytest.approx(2.0)
    assert store.json("feature_contract.json") is contract    # second read is cached


def test_vocabulary_version_falls_back_to_the_directory_when_the_manifest_omits_it(artifacts):
    """Both branches, because M4.5 made the fallback the only one a real bundle takes: the
    shipped manifest names no vocabulary at all. A manifest that does name one still wins, or
    the fallback would be silently authoritative over the corpus's own statement."""
    manifest = json.loads((artifacts / "manifest.json").read_text(encoding="utf-8"))
    assert "vocabulary_version" not in manifest, "the shipped manifest carries no such key"
    assert ArtifactStore.open(artifacts, "test-v1").vocab_version == "v1"

    manifest["vocabulary_version"] = "v9"
    (artifacts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ArtifactStore.open(artifacts, "test-v1").vocab_version == "v9"


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


# --- the title count the Data tab renders ----------------------------------------------


def test_the_title_count_comes_from_the_bundles_own_identity_record(artifacts, tmp_path):
    """`summary()['titles']` read `title_count` out of `artifacts/manifest.json`, a key no
    bundle has ever written, so the Data tab's "N titles" has always rendered nothing.

    §10 stages only the bundle's `artifacts/` subtree, so BUNDLE.json — which does carry the
    counts, as `tables` — cannot be read from the store's root. It reaches the store from
    `artifact_bundle.manifest`, which is where `importer/bundle.py` records it.
    """
    identity = json.loads(
        (tmp_path / "bundle" / "BUNDLE.json").read_text(encoding="utf-8")
    )
    assert "title_count" not in identity, "the corpus records counts under `tables`"

    store = ArtifactStore.open(artifacts, "test-v1", identity=identity)
    assert store.summary()["titles"] == len(fx.TITLES)
    assert identity["tables"]["title"] == len(fx.TITLES)


def test_a_store_nobody_handed_an_identity_reports_no_count_rather_than_a_wrong_one(artifacts):
    """§3.1's empty-ish case: the count is a fact about the bundle, and a store opened without
    one has to say it does not know. `owned` is absent from the summary entirely — §7.2
    re-derives ownership from Jellyfin per install, so no bundle could carry it."""
    summary = ArtifactStore.open(artifacts, "test-v1").summary()
    assert summary["titles"] is None
    assert "owned" not in summary


def test_a_jsonb_column_handed_back_as_text_still_yields_the_count(artifacts, tmp_path):
    """The app's pool registers a json codec, so `artifact_bundle.manifest` normally arrives
    decoded. A connection without one hands back the raw string, and a store that then reported
    nothing would be indistinguishable from a bundle that shipped nothing."""
    raw = (tmp_path / "bundle" / "BUNDLE.json").read_text(encoding="utf-8")
    from spielplan.models.artifacts import _as_mapping

    assert _as_mapping(raw)["tables"]["title"] == len(fx.TITLES)
    assert _as_mapping("not json") == {}
    assert _as_mapping(None) == {}
