"""The Backbone cache against a bundle directory rewritten under it (§10). The importer re-copies a
version's directory, so a cache keyed on version and root alone serves stale arrays."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from spielplan.models.artifacts import ArtifactStore
from spielplan.scoring import backbone as bb

VERSION = "v20260828"


def _write_backbone(root: Path, *, e00: float, mu: float) -> Path:
    """A zero row is an ABSENT row, so every row carries a real coordinate."""
    e = np.random.default_rng(20260912).standard_normal((4, 64)).astype(np.float32)
    e[0, 0] = e00
    np.savez(
        root / bb.BACKBONE_FILE,
        title_ids=np.arange(1, 5, dtype=np.int32),
        E=e,
        b_i=np.full(4, mu, dtype=np.float32),
        item_n=np.array([500, 900, 4, 200], dtype=np.int32),
        mu=np.float32(mu),
        cold_mask=np.zeros(4, dtype=bool),
    )
    return root / bb.BACKBONE_FILE


def test_a_restage_of_the_same_version_is_re_read_not_served_from_cache(tmp_path):
    """Re-opened, not reused: production builds a fresh `ArtifactStore` per job and per boot."""
    bb.forget_cached()
    root = tmp_path / "artifacts" / VERSION
    root.mkdir(parents=True)
    path = _write_backbone(root, e00=1.0, mu=0.25)

    first = bb.load_for(ArtifactStore.open(root, VERSION))
    assert first.embedding(1)[0] == np.float32(1.0)

    # mtime is set, not left to the clock: two writes in one filesystem tick would look unchanged.
    before = path.stat().st_mtime_ns
    _write_backbone(root, e00=2.0, mu=0.75)
    os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))

    second = bb.load_for(ArtifactStore.open(root, VERSION))
    assert second.embedding(1)[0] == np.float32(2.0), (
        "the rewritten directory was served from the cache: E[0, 0] is 2.0 on disk and 1.0 here"
    )
    assert second.mu == 0.75
    assert second is not first

    # The swap window still holds two, not one per restage.
    assert len(bb._CACHE) <= 2


def test_an_unchanged_bundle_directory_is_read_once(tmp_path):
    """§5.3's per-title budget is why this caches at all; dropping the cache would pass the test above."""
    bb.forget_cached()
    root = tmp_path / "artifacts" / VERSION
    root.mkdir(parents=True)
    _write_backbone(root, e00=1.0, mu=0.25)

    first = bb.load_for(ArtifactStore.open(root, VERSION))
    again = bb.load_for(ArtifactStore.open(root, VERSION))
    assert again is first, "an untouched directory must not be re-read on every load_for"

    # `forget_cached` must still forget: `test_model_basis.py` depends on it.
    bb.forget_cached()
    assert bb.load_for(ArtifactStore.open(root, VERSION)) is not first
