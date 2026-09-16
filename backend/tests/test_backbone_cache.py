"""The Backbone cache against a bundle directory that gets rewritten under it. §10, §4.3.

ml10. `scoring/backbone.py`'s `_CACHE` was keyed on `(store.version, str(store.root))` under a
docstring asserting that "a bundle directory is immutable for the life of its version" — and the
importer rmtree's and re-copytree's exactly that directory on every import of that version:
`importer/bundle.py` stages with `shutil.rmtree(staged)` then `shutil.copytree(...)`, which is
reached by a retry after a failed import and by M4.14's restage of the ACTIVE version, where
same-version-by-definition is the whole point. So the second import of a version served the first
import's arrays for the life of the process, with nothing in any log saying so.

The two tests below are the two halves of the fix, and the second exists because the first has a
wrong answer that passes it: deleting the cache. §5.3 budgets "<1 s/title" for steady-state work
and reading a 14,397-row basis is not per-title work, so the cache has to keep caching.

No database and no bundle fixture: a four-row synthetic `backbone.npz` states the property, and
`data/import`'s real one would only make the numbers harder to see. [M4.14, ml10]
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from spielplan.models.artifacts import ArtifactStore
from spielplan.scoring import backbone as bb

VERSION = "v20260828"


def _write_backbone(root: Path, *, e00: float, mu: float) -> Path:
    """A minimal §4.3 basis: the arrays `Backbone.open` requires, and nothing else.

    Every row carries a real coordinate — a zero row is an ABSENT row (cs-01), so a fixture of
    zeros would index nothing and both tests would read the same empty basis whatever the cache
    did. `e00` is the number the assertions follow from disk to `embedding()`.
    """
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
    """ml10, the reproduction. §10 stages to `/data/artifacts/<version>/` and flips; it does not
    promise that a version's directory is written once, and the importer's own rmtree/copytree is
    the proof it is not. A second store over a rewritten directory must see the rewritten numbers.

    The store is re-opened rather than reused, because that is what production does: the worker
    builds a fresh `ArtifactStore` per job through `_active_store`, and `app.py`'s lifespan builds
    one per boot. A fresh store with the same version and root is exactly the object that used to
    hit the stale entry.
    """
    bb.forget_cached()
    root = tmp_path / "artifacts" / VERSION
    root.mkdir(parents=True)
    path = _write_backbone(root, e00=1.0, mu=0.25)

    first = bb.load_for(ArtifactStore.open(root, VERSION))
    assert first.embedding(1)[0] == np.float32(1.0)

    # The restage: same version, same root, different arrays. mtime is set rather than left to
    # the clock because two writes inside one filesystem tick is a property of the test machine,
    # not of the thing under test; a real restage takes the corpus's own mtime through copytree.
    before = path.stat().st_mtime_ns
    _write_backbone(root, e00=2.0, mu=0.75)
    os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))

    second = bb.load_for(ArtifactStore.open(root, VERSION))
    assert second.embedding(1)[0] == np.float32(2.0), (
        "the rewritten directory was served from the cache: E[0, 0] is 2.0 on disk and 1.0 here"
    )
    assert second.mu == 0.75
    assert second is not first

    # The swap window still holds two, not one per restage: an unbounded cache is a second bug
    # wearing the fix's clothes.
    assert len(bb._CACHE) <= 2


def test_an_unchanged_bundle_directory_is_read_once(tmp_path):
    """The other half. §5.3's per-title budget is why this module caches at all, and the cheap
    wrong fix for ml10 — drop the cache, or re-open on every call — re-reads the whole basis on
    every request that asks for it. Identity is the assertion because a second `Backbone.open`
    cannot return the object the first one built.
    """
    bb.forget_cached()
    root = tmp_path / "artifacts" / VERSION
    root.mkdir(parents=True)
    _write_backbone(root, e00=1.0, mu=0.25)

    first = bb.load_for(ArtifactStore.open(root, VERSION))
    again = bb.load_for(ArtifactStore.open(root, VERSION))
    assert again is first, "an untouched directory must not be re-read on every load_for"

    # And `forget_cached` still forgets: five call sites in `test_model_basis.py` depend on it.
    bb.forget_cached()
    assert bb.load_for(ArtifactStore.open(root, VERSION)) is not first
