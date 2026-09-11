"""Cold Tower placement: the contract, the vector, the forward pass, the sweep, and §10's
rebuild set. Spec v2.1 §4.3, §5.3, §8 stages 9–10, §10, §12.

Two halves, deliberately:

* The contract and the vector are tested with **no database and no torch**. §5.3 puts a budget
  on placement, and a vector builder that can only be exercised through Postgres cannot be
  measured. These tests also do the thing the `data-rules-feature-contract-sole-input` row asks
  for and nothing less: they *edit a contract* and watch the vector move. A test that asserted
  `input_dim == 131` would pass just as well against a hardcoded offset table, which is exactly
  the bug the requirement exists to forbid.
* The sweep and the rebuild set need the real schema (array columns, the `title_placement`
  primary key, the acquisition board's `ON CONFLICT`), so they take the `db` fixture and skip
  without TEST_DATABASE_URL — see tests/conftest.py.

The fixture bundle ships production-shaped artifacts with the widths shrunk to fit eight titles
(67 content columns + 64 review-text = 131, the tower's input_dim) and placeholder column names.
Placeholder names would make every DB-driven assertion vacuously true — every key unmapped,
every vector all zeros — so `_realistic_contract` rewrites the *names* in place, keeping every
declared size, which is what keeps the shipped `cold_tower.pt` loadable against it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from spielplan.importer import bundle as bundle_import
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import features, reconcile, tower
from spielplan.placement.contract import ContractError, FeatureContract, unproducible_meta_names
from spielplan.scoring import backbone, serve
from tests.fixtures import make_bundle as fx

# The real keys the fixture's content spine (plus `_seed_extra_titles`) actually contains, per
# block. Each is padded out to the contract's declared width, so the widths — and therefore the
# tower's input_dim — are untouched.
# The real keys the fixture's content spine (plus `_seed_extra_titles`) actually contains, per
# block, **in the grammar the shipped contract declares**. Until M4.5 this table encoded the
# bare keys the builders happened to emit — `"Crime"`, `"heist"`, `"1"` — which is precisely how
# a builder that missed all 244 credit columns passed: the fixture's contract was written to
# agree with it. Each list is padded out to the contract's declared width, so the widths — and
# therefore the tower's input_dim — are untouched.
REAL_KEYS: dict[str, list[str]] = {
    "dna_x": ["dna:themes.obsession", "dna:characters.morally_grey", "dna:mood.dread",
              "dna:sensibility.bleak", "dna:mood.cosy", "dna:visual.neon",
              "dna:themes.surveillance", "dna:pacing.relentless", "dna:register.deadpan"],
    "dna_p": ["dna:themes.obsession", "dna:mood.dread", "dna:mood.cosy", "dna:era.period",
              "dna:structure.procedural", "dna:visual.neon", "dna:pacing.patient",
              "dna:place.domestic"],
    "genome": ["g:heist", "g:dread", "g:cooking"],
    "genre": ["genre:crime", "genre:thriller", "genre:family", "genre:romance", "genre:sci-fi",
              "genre:drama", "genre:comedy"],
    "keyword": ["kw:heist", "kw:investigation", "kw:family", "kw:cooking"],
    "credit": ["p:director:Michael Mann", "p:director:Denis Villeneuve",
               "p:director:Wong Kar-wai", "p:cast:Al Pacino", "p:writer:Ada Cross-Kind",
               "p:composer:Kunihiko Murai"],
    "country": ["country:United States of America", "country:Hong Kong", "country:Japan"],
    "award": ["award:nominated", "award:won"],
    "meta": ["kind:movie", "kind:series", "decade:1990", "runtime:>160", "lang:en"],
}

THIN_TITLE = 9        # no keywords, no DNA row, no genre, no credit, no reviews — only `meta`
FULL_TITLE = 10       # every one of the nine blocks, plus a review-text row


# --- helpers ---------------------------------------------------------------------------------


# Meta is a closed grammar (`contract.META_PRODUCTIONS`), so its padding has to be grammatical
# too: a `__pad_0` in the meta block is a column no production can fill, which
# `unproducible_meta_names` correctly reports — and which would make every test here read that
# report as noise instead of as the signal it is.
_META_PADDING = [f"decade:{d}" for d in range(1900, 2030, 10)] + [
    f"lang:{c}" for c in ("ja", "yue", "fr", "de", "it", "ko", "es", "zh")
] + [f"runtime:{b}" for b in ("<80", "80-105", "105-130", "130-160", ">160")]


def _names(block: str, size: int) -> list[str]:
    real = REAL_KEYS.get(block, [])[:size]
    if block == "meta":
        pool = [n for n in _META_PADDING if n not in real]
        return real + pool[: size - len(real)]
    return real + [f"__pad_{i}" for i in range(size - len(real))]


def _contract_doc(*, sizes: dict[str, int] | None = None, text_scale: float = 2.0,
                  names: dict[str, list[str]] | None = None) -> dict:
    """A contract document in the shape the corpus ships: `content_blocks` as a list of
    {name, size}, ONE flat `feature_names` list sliced by those sizes, and `text_scale` inside
    `text_block`. The old shape — `blocks` as an object, `feature_names` as a dict keyed by
    block, `text_scale` at the top level — is a file the corpus has never produced."""
    blocks = dict(sizes or {"dna_x": 12, "dna_p": 10, "genome": 8, "genre": 9, "keyword": 11,
                            "credit": 6, "country": 4, "award": 2, "meta": 5})
    per_block = {b: _names(b, n) for b, n in blocks.items()}
    for block, override in (names or {}).items():
        per_block[block] = override
    return {
        "content_blocks": [{"name": b, "size": n} for b, n in blocks.items()],
        "content_dim": sum(blocks.values()),
        "feature_names": [n for b in blocks for n in per_block[b]],
        "input_dim": sum(blocks.values()) + 64,
        "model_file": "cold_tower.pt",
        "preprocessing": {
            "genome": "zero-imputed for titles without MovieLens genome",
            "absent_blocks": "dropped to zeros; the tower's dropout training anticipates them",
        },
        "text_block": {
            "source": "review_text_emb.npz:emb", "columns": "0..63", "dim": 64,
            "order": "singular-value (descending)", "text_scale": text_scale,
        },
    }


def _realistic_contract(artifacts: Path, **kwargs) -> None:
    """Rewrite the bundle's contract column *names* without touching a single declared size."""
    shipped = json.loads((artifacts / "feature_contract.json").read_text(encoding="utf-8"))
    sizes = {b["name"]: b["size"] for b in shipped["content_blocks"]}
    doc = _contract_doc(sizes=sizes, text_scale=shipped["text_block"]["text_scale"], **kwargs)
    (artifacts / "feature_contract.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")


def _extend_text_embeddings(artifacts: Path, extra: list[int]) -> None:
    """Give `extra` titles a review-text row, so the ninth block can be *present* somewhere."""
    npz = np.load(artifacts / "review_text_emb.npz", allow_pickle=False)
    ids = np.concatenate([npz["title_ids"], np.array(extra, dtype=np.int32)])
    rng = np.random.default_rng(7)
    emb = np.concatenate([
        npz["emb"], rng.normal(size=(len(extra), npz["emb"].shape[1])).astype(np.float32)
    ])
    order = np.argsort(ids)
    np.savez(artifacts / "review_text_emb.npz", title_ids=ids[order], emb=emb[order],
             covered=np.ones(ids.size, dtype=bool), singular=npz["singular"])


def _shrink_backbone(artifacts: Path, drop: int) -> None:
    """A different Backbone: one title's row is gone, so its coverage shrank (§10).

    Every row-aligned array is filtered by the same mask rather than a named few: decision 162
    added `title_identity` beside the ids, and a helper that rebuilt the file from a list of
    arrays it remembered would silently drop it — which the validator would then refuse, and
    for the right reason.
    """
    with np.load(artifacts / "backbone.npz", allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    keep = arrays["title_ids"] != drop
    rows = arrays["title_ids"].shape[0]
    np.savez(artifacts / "backbone.npz", **{
        k: (v[keep] if getattr(v, "shape", ()) and v.shape[0] == rows else v)
        for k, v in arrays.items()
    })


def _make_bundle(root: Path, *, version: str = "test-v1", **contract_kwargs) -> Path:
    fx.make_bundle(root, version=version)
    _realistic_contract(root / "artifacts", **contract_kwargs)
    _extend_text_embeddings(root / "artifacts", [FULL_TITLE])
    return root


async def _seed_extra_titles(db) -> None:
    """Two titles the Backbone has never seen: one with nothing, one with everything.

    §5.3's reconciliation is about exactly these. `THIN_TITLE` is the row the coverage requirement
    names — "a title with no keywords and no DNA row" — and it is stamped `origin = 'acquired'`
    so §10's "every app-acquired title" has a member. `FULL_TITLE` exists so "thin titles are
    parked" is falsifiable: without a title that is *not* thin, the parking rule could be
    "park everything" and every test would still pass.
    """
    await db.execute(
        "INSERT INTO title (id, kind, name, year, runtime_min, is_owned, origin)"
        " VALUES (9, 'movie', 'Sans Soleil', 1983, 100, true, 'acquired')"
    )
    await db.execute(
        "INSERT INTO title (id, kind, name, year, runtime_min, imdb_id, is_owned, overview)"
        " VALUES (10, 'movie', 'Tampopo Redux', 1985, 114, 'tt9999999', true, 'a noodle western')"
    )
    await db.execute("INSERT INTO title_genre (title_id, genre, source) VALUES (10, 'Comedy', 'x')")
    await db.execute(
        "INSERT INTO title_keyword (title_id, keyword, source) VALUES (10, 'cooking', 'x')"
    )
    await db.execute("INSERT INTO credit (title_id, person_id, job, role_class)"
        " VALUES (10, 3, 'Director', 'director')")
    await db.execute("INSERT INTO title_country (title_id, country) VALUES (10, 'Japan')")
    await db.execute(
        "INSERT INTO award (title_id, body, category, year, won)"
        " VALUES (10, 'Academy Awards', 'Best Picture', 1986, false),"
        "        (10, 'Academy Awards', 'Directing', 1986, true)"
    )
    await db.execute(
        "INSERT INTO ml_genome_tag (tag_id, tag) VALUES (1, 'heist'), (2, 'dread'), (3, 'cooking')"
    )
    await db.execute("INSERT INTO ml_link (ml_movie_id, title_id) VALUES (110, 10)")
    await db.execute(
        "INSERT INTO ml_genome_score (ml_movie_id, tag_id, relevance) VALUES (110, 3, 0.8)"
    )
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, provider)"
        " VALUES (10, 'v1', 'register.deadpan', 'register', 2, 0.6, 'gemini')"
    )
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES (10, 'v1', 'place.domestic', 'place', 0.3, 'keyword:cooking')"
    )


@pytest.fixture
def bundle_root(tmp_path) -> Path:
    return _make_bundle(tmp_path / "bundle")


@pytest.fixture
async def placed(db, bundle_root, tmp_path):
    """Import the bundle, seed the two extra titles, run §5.3's sweep. Yields (store, report)."""
    artifacts_root = tmp_path / "artifacts"
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(bundle_root), artifacts_root
    )
    assert report.ok, report.render()
    await _seed_extra_titles(db)
    store = ArtifactStore.open(artifacts_root / "test-v1", "test-v1")
    return store, await reconcile.reconcile(db, store, scope="owned_missing")


# --- the contract is the sole input (data-rules-feature-contract-sole-input) ------------------


def test_every_block_lands_at_the_offset_the_contract_declares():
    """§4.3: "the nine content blocks **in order** with sizes … per-column `feature_names`".

    One key per block, each written into the column the contract names and into no other. This
    is the assertion a hardcoded offset table would pass; the next two tests are the ones it
    cannot.
    """
    contract = FeatureContract.load(_contract_doc())
    assert contract.block_names == (
        "dna_x", "dna_p", "genome", "genre", "keyword", "credit", "country", "award", "meta"
    )
    offset = 0
    for block in contract.blocks:
        assert block.offset == offset
        offset += block.size
    assert contract.content_width == offset == 67
    assert contract.text_offset == 67
    assert contract.input_dim == 67 + 64 == 131

    rows = {b.name: {b.names[0]: 1.0} for b in contract.blocks}
    built = features.build_vector(contract, 1, rows, None)
    expected = {b.offset for b in contract.blocks}
    assert set(np.nonzero(built.vec)[0].tolist()) == expected
    assert built.nnz == len(contract.blocks)


def test_editing_a_block_size_moves_every_later_column():
    """The requirement's own words: "editing a block size … changes the produced vector
    correspondingly". One column off the keyword block and every later block shifts by one."""
    wide = FeatureContract.load(_contract_doc())
    narrow = FeatureContract.load(_contract_doc(sizes={
        "dna_x": 12, "dna_p": 10, "genome": 8, "genre": 9, "keyword": 10,   # 11 -> 10
        "credit": 6, "country": 4, "award": 2, "meta": 5,
    }))

    assert narrow.input_dim == wide.input_dim - 1
    assert narrow.text_offset == wide.text_offset - 1
    for name in ("dna_x", "dna_p", "genome", "genre", "keyword"):
        assert narrow.block(name).offset == wide.block(name).offset      # before the edit
    for name in ("credit", "country", "award", "meta"):
        assert narrow.block(name).offset == wide.block(name).offset - 1   # after it

    rows = {"country": {"country:Japan": 1.0}, "award": {"award:won": 2.0}}
    assert np.nonzero(features.build_vector(wide, 1, rows, None).vec)[0].tolist() == [
        wide.block("country").column("country:Japan"), wide.block("award").column("award:won")
    ]
    assert np.nonzero(features.build_vector(narrow, 1, rows, None).vec)[0].tolist() == [
        wide.block("country").column("country:Japan") - 1,
        wide.block("award").column("award:won") - 1,
    ]


def test_text_scale_is_frozen_in_the_contract_and_scales_only_the_last_64_columns():
    """§4.3: "the review-text block = columns 0..63 of the 256-d SVD embedding (singular-value
    order) multiplied by a frozen scalar `text_scale` … computed over the corpus at export time
    and stored in this JSON."

    Frozen means it comes from the file: doubling it in the file doubles that slice and moves no
    content column. Columns 64..255 exist in the artifact and appear in the vector nowhere.
    """
    contract = FeatureContract.load(_contract_doc(text_scale=0.031_25))
    emb = np.arange(256, dtype=np.float32) + 1.0
    rows = {"genre": {"genre:crime": 1.0}}
    built = features.build_vector(contract, 1, rows, emb)

    tail = built.vec[contract.text_offset:]
    assert np.allclose(tail, emb[:64] * 0.031_25)
    assert tail.size == 64
    # Column 64 of the embedding (value 65.0) must not appear anywhere, at any scale.
    assert not np.any(np.isclose(built.vec, 65.0 * 0.031_25))
    assert "review_text" in built.blocks_present

    doubled = features.build_vector(
        FeatureContract.load(_contract_doc(text_scale=0.062_5)), 1, rows, emb
    )
    assert np.allclose(doubled.vec[contract.text_offset:], tail * 2.0)
    assert np.array_equal(doubled.vec[: contract.text_offset], built.vec[: contract.text_offset])


def test_a_contract_that_ships_no_text_scale_is_refused_rather_than_defaulted():
    """A default text_scale would silently move every coordinate ever computed, and the move
    would be invisible: the vectors stay finite and the ranking stays plausible."""
    doc = _contract_doc()
    doc["text_block"].pop("text_scale")
    with pytest.raises(ContractError, match="text_scale"):
        FeatureContract.load(doc)


def test_genome_is_zero_imputed_and_an_absent_block_is_dropped_not_defaulted():
    """§4.3: "genome zero-imputation"; §5.3: "absent blocks dropped — the tower's dropout
    training anticipates this".

    Both are all-zero slices; they are different facts about the title, and the difference is
    recorded rather than re-derived. What must be true of *both*: nothing is filled with a mean,
    a prior or a global average.
    """
    contract = FeatureContract.load(_contract_doc())
    built = features.build_vector(contract, 9, {"meta": {"kind:movie": 1.0}}, None)

    assert built.blocks_present == ("meta",)
    assert built.blocks_imputed == ("genome",)
    assert set(built.blocks_dropped) == {
        "dna_x", "dna_p", "genre", "keyword", "credit", "country", "award", "review_text"
    }
    genome = contract.block("genome")
    assert not built.vec[genome.offset:genome.stop].any()
    keyword = contract.block("keyword")
    assert not built.vec[keyword.offset:keyword.stop].any()
    # The whole vector is one column: the meta flag. Nothing was defaulted into the eight
    # blocks that had no data.
    assert built.nnz == 1
    assert built.is_thin


def test_a_key_the_contract_does_not_declare_is_counted_and_never_grows_the_vector():
    """The corpus export is the authority on its own column set (§4.1's shape note). A DNA term
    added after the bundle was built has no column, and inventing one would shift every later
    column out from under the tower."""
    contract = FeatureContract.load(_contract_doc())
    built = features.build_vector(
        contract, 1, {"genre": {"genre:crime": 1.0, "genre:cyberpunk": 1.0}}, None
    )
    assert built.unmapped == {"genre": 1}
    assert built.vec.size == contract.input_dim
    assert built.nnz == 1


def test_the_shipped_bundle_ships_a_contract_that_matches_its_own_tower(bundle_root):
    """The fixture is a scale model, and this is the check that it is a *consistent* one: the
    nine §4.3 names in §4.3's order, and a width the shipped checkpoint can actually take."""
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    assert contract.block_names == (
        "dna_x", "dna_p", "genome", "genre", "keyword", "credit", "country", "award", "meta"
    )
    assert contract.undeclared_blocks == ()
    assert features.unproducible_blocks(contract) == ()
    assert unproducible_meta_names(contract) == []
    assert contract.sha256 and len(contract.sha256) == 64
    assert tower.load_tower(store, contract).input_dim == contract.input_dim


# --- the forward pass (jellyfin-acquisition-eval-placement-vector-from-feature-contract) ------


def test_the_forward_pass_is_the_checkpoints_own_arithmetic(bundle_root):
    """A stub that returned zeros, or a mis-sliced output head, would pass every shape check.

    So the oracle is the checkpoint's weights, recomputed in numpy: the trunk's linear layers
    each followed by a ReLU, then the two heads §5.1 needs — ê(t), the coordinate, and b̂(t),
    "the shrunk item prior … from the Cold Tower for cold titles".
    """
    import torch

    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    cold = tower.load_tower(store, contract)

    # A bare state_dict, the way the corpus's exporter writes it — no wrapper to index into.
    state = torch.load(store.path("cold_tower.pt"), map_location="cpu", weights_only=True)
    w = {k: v.numpy() for k, v in state.items()}

    rng = np.random.default_rng(11)
    x = rng.normal(size=(3, contract.input_dim)).astype(np.float32)
    h = x
    for i in sorted(int(k.split(".")[1]) for k in w if k.startswith("trunk.") and "weight" in k):
        h = np.maximum(h @ w[f"trunk.{i}.weight"].T + w[f"trunk.{i}.bias"], 0.0)
    expect_e = h @ w["head_e.weight"].T + w["head_e.bias"]
    expect_b = (h @ w["head_b.weight"].T + w["head_b.bias"]).reshape(-1)

    e_hat, b_hat = cold.place(x)
    assert e_hat.shape == (3, 64) and b_hat.shape == (3,)
    assert np.allclose(e_hat, expect_e, atol=1e-5)
    assert np.allclose(b_hat, expect_b, atol=1e-5)
    # …and it is a function of its input, which a constant would not be.
    other, _ = cold.place(x + 1.0)
    assert not np.allclose(e_hat, other)


def test_the_tower_is_in_eval_mode_and_two_passes_agree_bit_for_bit(bundle_root):
    """§8 stage 9 is inference. A module left in training mode places the same title differently
    on every sweep, and the sweep is idempotent by contract — so eval mode is asserted, and a
    module that is not in it is refused rather than trusted."""
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    cold = tower.load_tower(store, contract)
    assert cold.module.training is False

    x = np.ones((1, contract.input_dim), dtype=np.float32)
    first, first_b = cold.place(x)
    second, second_b = cold.place(x)
    assert np.array_equal(first, second) and first_b == second_b

    cold.module.train()
    try:
        with pytest.raises(tower.TowerError, match="training mode"):
            cold.place(x)
    finally:
        cold.module.eval()


def test_a_tower_whose_width_disagrees_with_the_contract_is_refused_loudly(bundle_root, tmp_path):
    """§4.3 makes the contract "the exhaustive definition of the tower's input". If the two files
    disagree, the alternatives are a crash or thousands of plausible-looking wrong coordinates —
    so the error names both widths and the contract hash."""
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    doc = _contract_doc(sizes={"dna_x": 12, "dna_p": 10, "genome": 8, "genre": 9, "keyword": 10,
                               "credit": 6, "country": 4, "award": 2, "meta": 5})
    narrow = FeatureContract.load(doc, sha256="deadbeef" * 8)
    assert narrow.input_dim == 130
    with pytest.raises(tower.TowerError, match=r"131 input columns|130"):
        tower.load_tower(store, narrow)


def test_nothing_in_the_placer_reaches_for_a_gpu(bundle_root):
    """§2: "No GPU anywhere. Torch CPU wheels only. The image must build and run on a GPU-less
    VM." The runtime check is that every loaded tensor is on the CPU; the static one is that no
    code path names a device at all, because `map_location` only helps if nothing later moves."""
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    cold = tower.load_tower(store, FeatureContract.from_store(store))
    assert [p.device.type for p in cold.module.parameters()] == ["cpu"] * 8

    source = Path(tower.__file__).read_text(encoding="utf-8")
    assert 'map_location="cpu"' in source
    for forbidden in (".cuda(", "cuda:", "device=", "torch.device"):
        assert forbidden not in source, f"tower.py names a device: {forbidden!r}"


# --- §5.3's reconciliation, against the real schema -------------------------------------------


async def test_a_zeroed_backbone_row_is_demoted_and_swept_rather_than_left_warm(db, tmp_path):
    """cs-01, the half a loader test cannot show: what the sweep is handed.

    `classify_warm` stamps exactly `warm_title_ids`, and the shipped bundle carries 1,915 rows
    that clear `WARM_SUPPORT` while carrying no coordinate at all — E written as zeros, the real
    one kept in `E_hat`. On support alone they were stamped warm, which is the flag
    `titles_needing_placement` reads to decide who is already covered, so the sweep that exists
    to give them a coordinate skipped exactly them. A title already carrying the old stamp has to
    be demoted, not merely left out of the new list: §10's "everything expressed in the old
    Backbone's basis is garbage against a new one" is the same argument.

    The npz is written here rather than taken from `make_bundle.py` because this test needs the
    two exclusions side by side — a flagged row ABOVE `WARM_SUPPORT` and a thin one below it —
    and the shared fixture has one of each across different titles, so a demote count of 2 there
    would not distinguish them. The shared fixture does carry the row — the plan's §8 obliges this
    milestone to add it there, and
    `test_the_shared_fixtures_flagged_row_is_swept_and_served_from_the_tower` drives that one
    through the real importer — so this is the same rule stated at three rows, not a second bundle
    builder. [M4.13 cycle 1, M413-REV-02]
    """
    root = tmp_path / "artifacts"
    root.mkdir()
    e = np.random.default_rng(20260906).standard_normal((3, 64)).astype(np.float32)
    e[1] = 0.0
    np.savez(
        root / "backbone.npz",
        title_ids=np.array([1, 2, 3], dtype=np.int32),
        E=e,
        b_i=np.array([0.4, 0.5, 0.6], dtype=np.float32),
        # Title 2 is the defect: zeroed, and well above WARM_SUPPORT. Title 3 is thin and warm-less
        # for the ordinary reason, so the two exclusions stay distinguishable.
        item_n=np.array([500, 900, 4], dtype=np.int32),
        mu=np.float32(0.1),
        cold_mask=np.array([False, True, False]),
    )
    store = ArtifactStore.open(root, "cold-v1")

    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state)"
        " VALUES ('cold-v1', '{}'::jsonb, 'active')"
    )
    for title_id in (1, 2, 3):
        await db.execute(
            "INSERT INTO title (id, kind, name, is_owned, placement) "
            "VALUES ($1, 'movie', $2, true, 'warm')",
            title_id, f"title {title_id}",
        )

    warm, demoted = await reconcile.classify_warm(db, store, bundle_version="cold-v1")
    assert warm == 1, "only the covered, supported, non-zero row is warm"
    assert demoted == 2, "the zeroed row and the thin one lose a stamp they should never have had"

    stamped = {
        int(r["id"]): r["placement"]
        for r in await db.fetch("SELECT id, placement FROM title ORDER BY id")
    }
    assert stamped == {1: "warm", 2: "unplaced", 3: "unplaced"}

    # And the sweep now sees it. Before the fix title 2 was stamped warm, so `_MISSING_SQL`'s
    # `placement <> 'warm'` filtered it out and it never got a title_placement row.
    needing = await reconcile.titles_needing_placement(
        db, bundle_version="cold-v1", scope="owned_missing"
    )
    assert needing == [2, 3]


async def test_the_shared_fixtures_flagged_row_is_swept_and_served_from_the_tower(db, placed):
    """cs-01 composed, on the one bundle the importer, the devstub and the e2e stack all build.

    The two tests above hand `Backbone.open` and `classify_warm` an npz written in `tmp_path`, so
    they prove the loader reads `cold_mask` and the sweep acts on it -- and nothing about the two
    halves meeting. A flagged row has to survive `import_bundle` -> `classify_warm` ->
    `titles_needing_placement` -> `place_titles` -> `serve.coordinates` as ONE path, and until this
    test no layer in CI drove that: `make_bundle.py` shipped `cold_mask` as all-False and `E_hat` as
    E itself, so every array in the shared fixture agreed with a reader that ignored the mask.
    The M4.13 plan's section 8 is explicit that the row belongs here rather than in a third npz --
    "If M4.8 has not yet added a fixture title with `cold_mask` set, a zeroed `E` row and a non-zero
    `E_hat`, this milestone adds exactly that row there".

    `item_n` above `WARM_SUPPORT` is the whole trap, which is why it is asserted rather than
    assumed: 1,915 of the shipped bundle's 2,879 flagged rows clear it, and on support alone every
    one of them was stamped warm, excused from the sweep that exists to give it a coordinate, and
    served at e(t) = 0 for ever. [M4.13 cycle 1, M413-REV-02]
    """
    store, _report = placed

    # The fixture first: what follows is a claim about the pipeline only if the row is really in
    # the file the whole suite builds from.
    npz = store.npz("backbone.npz")
    flagged = [int(t) for t, cold in zip(npz["title_ids"], npz["cold_mask"], strict=True) if cold]
    assert flagged == [8], "the shared fixture ships no cold_mask row for the pipeline to carry"
    (cold_title,) = flagged
    row = npz["title_ids"].tolist().index(cold_title)
    assert not npz["E"][row].any(), "the export writes zeros for a flagged row, not a coordinate"
    assert np.linalg.norm(npz["E_hat"][row]) > 0, "and keeps the real one in E_hat (decision 236)"
    assert int(npz["item_n"][row]) >= backbone.WARM_SUPPORT, (
        "a flagged row under the threshold would be excused by the support test anyway, and the "
        "mask would be carrying nothing"
    )

    assert cold_title not in reconcile.warm_title_ids(store), "flagged, so not warm on support"
    assert await db.fetchval(
        "SELECT placement FROM title WHERE id = $1", cold_title
    ) == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1 AND bundle_version = 'test-v1'",
        cold_title,
    ) == 1, "the sweep it was excused from is the sweep that gives it its only coordinate"

    bb = backbone.load_for(store)
    assert bb.row(cold_title) is None and bb.support(cold_title) == 0
    served = (await serve.coordinates(db, bb, bundle_version="test-v1"))[cold_title]
    assert served.e_source == "cold_tower" and served.gate == 0.0
    assert np.linalg.norm(served.e) > 0, (
        "the pure cold limit, not the zeros the export shipped -- a title served at e(t) = 0 while "
        "reported coordinated is what section 12's M2 criterion counts as covered"
    )

    # AND THE CROWD STILL RATED IT. Excluding the row is a statement about the COORDINATE, and it
    # cost `support()` and the gate their input on purpose. It must not cost the crowd's count its
    # readers: `title_prior.item_n` is what section 6.1's P(seen) weights at 2.0 through
    # log1p(n)/log1p(1e5) and what section 8 stage 10's card payload carries, and writing 0 there
    # told the Rate sweep that one of the most-rated titles in the file was one nobody has seen.
    # The two quantities are separate objects now and this is where they meet: `gate` is 0 in the
    # same row, so nothing that wants the evidence gate has to read the popularity column.
    # [M4.13 cycle 2, M413-C2-DIM5-01]
    shipped = int(npz["item_n"][row])
    assert bb.crowd_support(cold_title) == shipped
    assert served.crowd_n == shipped and served.item_n == 0
    prior = await db.fetchrow(
        "SELECT item_n, gate, e_source FROM title_prior WHERE title_id = $1", cold_title
    )
    assert (prior["item_n"], prior["gate"], prior["e_source"]) == (shipped, 0.0, "cold_tower"), (
        "the popularity column carries the file's own count and the gate column carries the gate"
    )


async def test_a_title_with_no_keywords_and_no_dna_row_still_gets_a_coordinate(db, placed):
    """The coverage requirement's own sentence: "A title with no keywords and no DNA row still
    yields a vector and a coordinate."

    Eight of the ten blocks are absent for this title. Seven drop, genome imputes, `meta` alone
    is present — and §12's M2 exit criterion still holds for it.
    """
    row = await db.fetchrow(
        "SELECT * FROM title_placement WHERE title_id = $1 AND bundle_version = 'test-v1'",
        THIN_TITLE,
    )
    assert row is not None, "the thin title got no coordinate"
    e_hat = np.frombuffer(row["e_hat"], dtype=np.float32)
    assert e_hat.shape == (64,) and np.isfinite(e_hat).all()
    assert np.isfinite(row["b_hat"])
    assert row["blocks_present"] == ["meta"]
    assert row["blocks_imputed"] == ["genome"]
    assert set(row["blocks_dropped"]) == {
        "dna_x", "dna_p", "genre", "keyword", "credit", "country", "award", "review_text"
    }
    # 57 content columns + 64 review-text: the width the fixture's own contract declares. The
    # M4.5 rewrite moved it from 131, and a literal that no longer traces to the artifact is the
    # hardcoded offset table this file's docstring warns about.
    assert row["input_dim"] == 121
    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", THIN_TITLE) == "cold_tower"


async def test_every_owned_title_has_a_coordinate_after_reconciliation(db, placed):
    """§12's M2 exit criterion, as the one query the partial index exists for.

    It also pins which of §5.1's three branches each title lands in, because that is where the
    exit criterion and the scoring formula meet. Warm is `item_n ≥ WARM_SUPPORT` (90 at k = 10),
    not "the Backbone has a row" — the fixture's supports are 4218 / 900 / 120 / 30 / 6 / 240 /
    55 for titles 1–7, so:

      1, 2, 3, 6   warm      gate ≥ 0.92, coordinate taken from `backbone.npz` outright and no
                             `title_placement` row, because copying E into Postgres would make
                             §10's rebuild set five items long
      4, 5, 7      blended   gate 0.75 / 0.375 / 0.846 — a Backbone row AND a tower coordinate,
                             which is the only way §5.1's `gate·E + (1-gate)·ê` can fire
      8, 9, 10     cold      no Backbone row at all; the tower's coordinate is the whole of it

    Under the earlier reading — warm iff covered — the middle column was empty and the blend was
    dead code that no test could reach.
    """
    _store, report = placed
    assert await db.fetchval(
        "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
    ) == 0

    warm = await db.fetch("SELECT id FROM title WHERE placement = 'warm' ORDER BY id")
    assert [r["id"] for r in warm] == [1, 2, 3, 6]
    cold = await db.fetch("SELECT id FROM title WHERE placement = 'cold_tower' ORDER BY id")
    assert [r["id"] for r in cold] == [4, 5, 7, 8, THIN_TITLE, FULL_TITLE]
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = ANY(ARRAY[1,2,3,6])"
    ) == 0, "a warm title's coordinate is the Backbone's, so it must have no row here"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = ANY(ARRAY[4,5,7])"
    ) == 3, "a low-support Backbone title needs a tower coordinate to be blended with"

    counts = await reconcile.placement_counts(db, bundle_version="test-v1")
    assert counts["owned_unplaced"] == 0
    assert counts["owned_warm"] == 4
    assert counts["owned_cold"] == 6
    assert counts["placement_rows"] == 6

    # §5.3 triggers placement on "bundle import **and** nightly sweep", and the import runs it
    # as step 4 of §10's rebuild set. So by the time this sweep runs, the four titles the bundle
    # itself brought are already placed and only the two seeded afterwards are left. The sweep
    # doing less work is the import having done it — which is why the assertions that matter are
    # the six rows and the zero unplaced above, not how many this particular call wrote.
    assert report.placed == 2 and report.failed == 0
    assert report.considered == 2


async def test_a_low_support_title_is_served_as_a_genuine_blend_of_both_coordinates(db, placed):
    """§5.1: `e(t) = gate·E[t] + (1-gate)·ê(t)`, `gate = n_t/(n_t + k)`.

    The one test that the middle branch is reachable *in production* rather than only from a
    unit test that hands `coordinate()` a placement by hand. It runs the real §5.3 sweep and
    then asks the serving path what it would use, because the failure this guards against is
    not arithmetic — the arithmetic was always right — but a placement policy under which no
    low-support title ever acquires the ê the arithmetic needs.

    Title 5 has `item_n = 6`, so gate = 6/16 = 0.375 and 62.5% of its coordinate is the tower's.
    Title 1 has 4218, so it is warm and takes E outright. Title 8's row is flagged in `cold_mask`,
    so it has no coordinate to blend and takes the tower's outright.
    """
    store, _report = placed
    bb = backbone.load_for(store)
    coords = await serve.coordinates(db, bb, bundle_version="test-v1")

    blended = coords[5]
    assert blended.e_source == "blended"
    assert blended.gate == pytest.approx(6 / 16)

    e_backbone = bb.embedding(5)
    e_hat = (await serve.placements(db, bundle_version="test-v1"))[5][0]
    assert not np.allclose(e_backbone, e_hat), "the fixture must not make the branches identical"
    np.testing.assert_allclose(
        blended.e, blended.gate * e_backbone + (1 - blended.gate) * e_hat, rtol=1e-6, atol=1e-7
    )
    # Strictly between the two, which is what "blend" means and what a branch that silently
    # fell through to one side would fail.
    assert not np.allclose(blended.e, e_backbone)
    assert not np.allclose(blended.e, e_hat)

    assert coords[1].e_source == "backbone"
    np.testing.assert_allclose(coords[1].e, bb.embedding(1), rtol=1e-6)
    assert coords[8].e_source == "cold_tower"


async def test_the_cold_tower_coordinate_is_written_unscaled(db, placed):
    """cs-02 / dd15, decision 236. What the sweep stores is what the tower produced, bit for bit.

    §5.1's middle line only means "a blend" while its two halves are comparable, and on the shipped
    bundle they are not: over the 3,860 rows the line applies to -- a real Backbone row below
    WARM_SUPPORT -- median ||E|| is 0.0152 against median ||E_hat|| 20.47, so
    ((1-g)*||ê||)/(g*||E||) runs p10 82.5 / median 525.8 / p90 5,498.9 and the cold half decides
    every thin title's personal term. Every population is named because the figures this replaces
    were taken over two different ones: 0.184 was a median over `item_n >= 90` INCLUDING the cold
    rows whose E is zeros (0.3835 over rows that carry a coordinate), and 3,846 was a literal cut
    at 90 where `WARM_SUPPORT` is one ulp above it. [M4.13 cycle 2, M413-C2-DIM5-03] Decision 236 sends the
    rescaling upstream — whether E is unit-scale item factors or support-weighted is the corpus's
    contract (§4.1 carries the artifact over verbatim) — so the app measures and scales nothing.

    This is the negative half of that ruling, asserted where the scaling would be cheapest to slip
    in: `reconcile.place_titles` writes `e_hat[i].astype(np.float32).tobytes()`, and one `/
    np.linalg.norm(...)` there would silently move three readers at once — §5.2's fit, §5.1's
    serving path, and §6.7's rail — with every stored row still looking plausible. So the tower is
    re-run over the same built vector and the bytes are compared, rather than the norms being
    sanity-checked into a range.
    """
    store, _report = placed
    contract = FeatureContract.from_store(store)
    cold = tower.load_tower(store, contract)
    stored = {
        int(r["title_id"]): (backbone.unpack_vec(r["e_hat"]), float(r["b_hat"]))
        for r in await db.fetch("SELECT title_id, e_hat, b_hat FROM title_placement")
    }
    assert stored, "the sweep placed nothing, so there is no write to check"

    ids = sorted(stored)
    built = await features.build_vectors(db, store, contract, ids, vocab_version="v1")
    assert [b.title_id for b in built] == ids
    e_hat, b_hat = cold.place(np.stack([b.vec for b in built]))

    for index, title_id in enumerate(ids):
        # The float32 round trip is the convention (0008: "64 x float32 LE"), and it is the ONLY
        # transformation between the tower's output and the row.
        assert np.array_equal(stored[title_id][0], backbone.unpack_vec(
            e_hat[index].astype(np.float32).tobytes()
        )), f"title {title_id}'s stored e_hat is not the tower's own output"
        assert stored[title_id][1] == pytest.approx(float(b_hat[index]), abs=1e-6)

    # And the norms are the tower's, not 1.0 and not the Backbone's: the imbalance the measurement
    # exists to report is IN the stored rows, which is what makes the report about production data.
    norms = {t: float(np.linalg.norm(stored[t][0])) for t in ids}
    assert not any(abs(n - 1.0) < 1e-6 for n in norms.values()), (
        f"a stored e_hat has unit norm: something normalised it on the way in ({norms})"
    )
    bb_backbone = backbone.load_for(store)
    report = backbone.blend_ratios(bb_backbone, {t: stored[t] for t in ids})
    print(f"\nblend ratio over the swept rows: {report.as_dict()}")
    assert report.n_measured >= 1, "no thin row was placed, so the blend has nothing to weigh"


async def test_a_thin_title_is_placed_badged_and_parked_and_a_complete_one_is_not(db, placed):
    """§5.3: "thin ones (2 lack keywords, 3 lack any DNA row) are **still placed, badged, and
    parked** as acquisition jobs for M5 enrichment."

    Placed and badged: `title.placement = 'cold_tower'`, which is §8 stage 10's badge input.
    Parked: the *job* waits at §8 stage 2 (enrich), the fetch every later block derives from —
    the title itself is ready. `FULL_TITLE` has all nine blocks and a review-text row, so it is
    not thin and gets no job; without it "park everything" would pass this test.
    """
    job = await db.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", THIN_TITLE)
    assert job is not None
    assert job["stage"] == 2 and job["status"] == "parked"
    assert "keyword" in job["reason"] and "dna_x" in job["reason"]
    assert job["detail"]["blocks_imputed"] == ["genome"]

    full = await db.fetchrow(
        "SELECT blocks_present, blocks_dropped, nnz FROM title_placement WHERE title_id = $1",
        FULL_TITLE,
    )
    assert full["blocks_dropped"] == []
    assert set(full["blocks_present"]) == {
        "dna_x", "dna_p", "genome", "genre", "keyword", "credit", "country", "award", "meta",
        "review_text",
    }
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", FULL_TITLE
    ) == 0


async def test_the_vector_is_built_from_the_database_through_the_contracts_own_columns(db, placed):
    """§5.3: "a feature vector built from **DB data** per the feature contract".

    Every value below is traceable to one row of the content spine and lands in the column the
    contract names for it — the extracted term in the extracted tier's column and the projected
    one in *its* column (§4.1 rule 1: two tiers, never merged), genome relevance, the award
    counts, and the meta block's one-hots.

    Every column name below used to be the bare database value the builder happened to emit —
    `"Comedy"`, `"cooking"`, `"3"` — against a fixture contract written to agree with it. The
    shipped contract names them `genre:comedy`, `kw:cooking`, `p:director:Wong Kar-wai`, so this
    test asserted only that the code agrees with itself (§4.3: the contract is the *exhaustive*
    definition of the tower's input, and it is the corpus that writes it).
    """
    store, _report = placed
    contract = FeatureContract.from_store(store)
    built = (await features.build_vectors(
        db, store, contract, [FULL_TITLE], vocab_version="v1"
    ))[0]
    vec = built.vec

    # 1.0 and not the row's salience 2 / weight 0.3: the corpus built both DNA blocks as
    # presence (`contract.DEFAULT_ENCODING`), so those are the only values the tower has seen.
    assert vec[contract.block("dna_x").column("dna:register.deadpan")] == pytest.approx(1.0)
    assert vec[contract.block("dna_p").column("dna:place.domestic")] == pytest.approx(1.0)
    assert vec[contract.block("genome").column("g:cooking")] == pytest.approx(0.8, abs=1e-6)
    assert vec[contract.block("genre").column("genre:comedy")] == 1.0
    assert vec[contract.block("keyword").column("kw:cooking")] == 1.0
    assert vec[contract.block("credit").column("p:director:Wong Kar-wai")] == 1.0
    assert vec[contract.block("country").column("country:Japan")] == 1.0
    assert vec[contract.block("award").column("award:nominated")] == pytest.approx(1.0)
    assert vec[contract.block("award").column("award:won")] == pytest.approx(1.0)
    assert vec[contract.block("meta").column("kind:movie")] == 1.0
    assert vec[contract.block("meta").column("kind:series")] == 0.0
    assert vec[contract.block("meta").column("decade:1980")] == 1.0

    # Every DB key the contract declares landed. The one block with leftovers is `meta`: the
    # shipped contract's 57 meta columns are one-hots only (`kind:`, `decade:`, `runtime:`,
    # `lang:` — `real_bundle_shapes.json` lists no other pattern), so `META_PRODUCTIONS`'
    # continuous productions and `has:` flags have no column to go to. They are *counted*
    # rather than silently discarded, and no key anywhere widened the vector.
    assert set(built.unmapped) == {"meta"}
    assert built.unmapped["meta"] > 0
    assert built.vec.size == contract.input_dim


async def test_reconciliation_is_idempotent(db, placed):
    """§5.3 runs this on every bundle import *and* nightly. A second sweep must be a no-op —
    otherwise every night rewrites 13k rows and re-parks every thin title."""
    store, _first = placed
    before = await db.fetch(
        "SELECT title_id, e_hat, created_at FROM title_placement ORDER BY title_id"
    )
    again = await reconcile.reconcile(db, store, scope="owned_missing")
    assert again.considered == 0 and again.placed == 0
    after = await db.fetch(
        "SELECT title_id, e_hat, created_at FROM title_placement ORDER BY title_id"
    )
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


async def test_a_bundle_with_no_backbone_row_for_a_title_never_calls_it_warm(db, placed):
    """"Warm" is a Backbone row with crowd support behind it. Title 8 has no row at all, so it
    takes the cold branch — §5.1's `e(t) = ê(t)` — and §12's exit criterion is about exactly
    those titles."""
    store, _ = placed
    assert 8 not in reconcile.warm_title_ids(store)
    assert await db.fetchval("SELECT placement FROM title WHERE id = 8") == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = 8"
    ) == 1


# --- the nine blocks' key grammar, and the block that hits none of its columns -----------------
#     (data-rules-feature-blocks-keyed-as-the-contract-names-them,
#      data-rules-a-feature-block-that-never-hits-is-not-present)


# One column per block, in the grammar the shipped contract declares, with the value the
# fixture's own content spine puts there. Every entry is traceable to one INSERT in
# `_seed_extra_titles`, which is what makes this a test of the builder rather than of a table
# written to agree with it.
CONTRACT_COLUMNS: dict[str, tuple[str, float]] = {
    # Both DNA tiers are presence: the row's salience (2) and weight (0.3) are values the
    # corpus's exporter never put in a cell, so neither is what the checkpoint reads.
    "dna_x": ("dna:register.deadpan", 1.0),          # the extracted tier
    "dna_p": ("dna:place.domestic", 1.0),            # the projected tier
    "genome": ("g:cooking", 0.8),                    # MovieLens relevance, above the 0.5 cut
    "genre": ("genre:comedy", 1.0),                  # 'Comedy', lowercased by the contract
    "keyword": ("kw:cooking", 1.0),
    "credit": ("p:director:Wong Kar-wai", 1.0),      # person 3, by name and not by id
    "country": ("country:Japan", 1.0),
    "award": ("award:won", 1.0),
    "meta": ("kind:movie", 1.0),
}


@pytest.fixture
def grammar_bundle(tmp_path) -> Path:
    """The same bundle with its credit columns keyed `credit:<n>` (`break_contract_block_grammar`).

    That is the shape this repo's fixture used to declare, and it is the one a `person_id::text`
    builder agrees with perfectly — which is why the miss was invisible for as long as it was.
    """
    root = _make_bundle(tmp_path / "grammar")
    fx.break_contract_block_grammar(root)
    return root


@pytest.fixture
async def spine(db):
    """The rows §8 stage 9 reads, inserted directly rather than through `import_bundle`.

    `placed` above reaches the same state through the importer, and these tests deliberately do
    not: what they assert is a property of the feature builder and the contract, and a defect
    anywhere else in the import path must not get to decide whether it is asserted at all.
    """
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state)"
        " VALUES ('test-v1', '{}'::jsonb, 'active')"
    )
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 14)"
    )
    await db.execute("INSERT INTO person (id, name) VALUES (3, 'Wong Kar-wai')")
    await _seed_extra_titles(db)


async def _sweep(db, root: Path):
    """§5.3's sweep against the bundle at `root`. Returns (store, report)."""
    store = ArtifactStore.open(root / "artifacts", "test-v1")
    return store, await reconcile.reconcile(db, store, scope="owned_missing")


def test_present_absent_and_all_keys_missing_are_three_distinguishable_states():
    """The row's own sentence: "A block that is present and populated, one that is legitimately
    absent, and one whose keys all miss are three distinguishable states."

    All three are the same bytes in the vector, so the difference can only be bookkeeping. §4.3
    makes `feature_contract.json` "the **exhaustive** definition of the tower's input", which
    fixes what `present` is allowed to mean: a block that hit a column that definition declares.
    A block marked present because its query returned rows claims the tower was fed something it
    was not.
    """
    contract = FeatureContract.load(_contract_doc())
    built = features.build_vector(
        contract,
        1,
        {
            "genre": {"genre:crime": 1.0},         # populated: a column the contract declares
            "keyword": {"3": 1.0, "8829": 1.0},    # every key misses: row ids, not `kw:` names
            # `credit` is not in `rows` at all: legitimately absent.
        },
        None,
    )

    assert built.blocks_present == ("genre",)
    assert built.blocks_empty == ("keyword",)
    assert "keyword" not in built.blocks_dropped, "an all-miss block is not an absent one"
    assert "credit" in built.blocks_dropped
    assert built.unmapped == {"keyword": 2}

    # The four names partition the contract's blocks: no block is in two of them, and none is
    # in nothing. That is what "distinguishable" has to mean if the badge is to read them.
    named = [set(built.blocks_present), set(built.blocks_dropped),
             set(built.blocks_imputed), set(built.blocks_empty)]
    assert sum(len(s) for s in named) == len(set().union(*named))
    assert set().union(*named) == {*contract.block_names, "review_text"}

    # …and the vector cannot tell them apart, which is the whole reason they are recorded.
    kw = contract.block("keyword")
    assert not built.vec[kw.offset:kw.stop].any()
    assert built.nnz == 1


def test_is_thin_sees_a_block_whose_keys_all_miss():
    """§5.3: "thin ones … are still placed, badged, and parked as acquisition jobs".

    The falsifiable half: a title with every block populated is not thin, and the same title
    with one block's keys rewritten into a grammar the contract does not declare is — even
    though nothing about it is absent and `blocks_dropped` stays empty.
    """
    contract = FeatureContract.load(_contract_doc())
    text = np.zeros(contract.text_dims, dtype=np.float32)
    complete = {b.name: {b.names[0]: 1.0} for b in contract.blocks}
    assert not features.build_vector(contract, 1, complete, text).is_thin

    built = features.build_vector(contract, 1, {**complete, "credit": {"3": 1.0}}, text)
    assert built.blocks_dropped == (), "nothing about this title is absent"
    assert built.blocks_empty == ("credit",)
    assert built.is_thin


async def test_every_content_block_is_keyed_as_the_contract_names_it(db, spine, bundle_root):
    """§4.3: "§8 stage 9 builds vectors from this file and nothing else."

    The row names the grammar: `kw:`, `dna:`, `g:`, `p:<role>:<name>`, `genre:`, `country:`,
    `lang:`, `decade:`, `runtime:`, `award:`, `kind:` — "and not by bare database values or by
    row ids". So the builders' raw keys are asserted first, before the contract is consulted.
    A test that only read the *vector* would be scoring the builder against a contract written
    from the same reading that produced the key, which is how `credit:3` survived.

    `FULL_TITLE` is the title whose keywords, genres, DNA terms, genome tags, countries, crew
    and awards the contract names, so each of the nine blocks owes a non-zero column.
    """
    store, report = await _sweep(db, bundle_root)
    contract = FeatureContract.from_store(store)

    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]
    assert keys["credit"] == {"p:director:Wong Kar-wai": 1.0}, "the crew is keyed by name"
    assert set(keys["genre"]) == {"genre:comedy"}
    assert set(keys["keyword"]) == {"kw:cooking"}
    assert set(keys["country"]) == {"country:Japan"}
    assert set(keys["genome"]) == {"g:cooking"}
    assert set(keys["dna_x"]) == {"dna:register.deadpan"}
    assert set(keys["dna_p"]) == {"dna:place.domestic"}
    assert set(keys["award"]) == {"award:nominated", "award:won"}
    assert {"kind:movie", "decade:1980", "runtime:105-130"} <= set(keys["meta"])

    built = (await features.build_vectors(
        db, store, contract, [FULL_TITLE], vocab_version="v1"
    ))[0]
    for block, (column, value) in CONTRACT_COLUMNS.items():
        offset = contract.block(block).column(column)
        assert offset is not None, f"the contract declares no {column!r} in block {block}"
        assert built.vec[offset] == pytest.approx(value, abs=1e-6), f"block {block} is zero"
    meta = contract.block("meta")
    assert built.vec[meta.column("decade:1980")] == 1.0
    assert built.vec[meta.column("kind:series")] == 0.0
    assert built.vec[contract.block("award").column("award:nominated")] == pytest.approx(1.0)

    # `meta` is the only block with leftovers, and only because the shipped contract's 57 meta
    # columns are one-hots (`real_bundle_shapes.json` lists `kind:`, `decade:`, `runtime:`,
    # `lang:` and no other pattern) while `META_PRODUCTIONS` also produces `has:` flags and
    # continuous columns. Every other block's keys are the contract's own column names.
    assert set(built.unmapped) == {"meta"}

    # Nine blocks present, none of them degraded — and the import-time check does not fire on a
    # contract the builder agrees with, which is what makes its firing in the next test a signal.
    assert built.blocks_empty == ()
    assert set(built.blocks_present) == {*contract.block_names, "review_text"}
    assert report.blocks_never_hit == []


async def test_a_block_that_hit_nothing_is_persisted_and_parks_the_title(db, spine, grammar_bundle):
    """The row: "the miss count is persisted with the placement, is_thin sees it, and the title
    is badged and parked like any other thin title."

    `FULL_TITLE` carries a row for every one of the nine blocks, so nothing about it is absent.
    Without the persisted counts its placement is byte-identical to one where all 244 credit
    columns landed — which is exactly the state every placement this app computed was in.
    """
    _store, report = await _sweep(db, grammar_bundle)
    assert report.failed == 0

    full = await db.fetchrow(
        "SELECT blocks_present, blocks_dropped, blocks_empty, blocks_unmapped"
        "  FROM title_placement WHERE title_id = $1 AND bundle_version = 'test-v1'",
        FULL_TITLE,
    )
    assert full is not None, "a degraded block must not stop the title being placed"
    assert full["blocks_dropped"] == []
    assert full["blocks_empty"] == ["credit"]
    assert "credit" not in full["blocks_present"]
    assert full["blocks_unmapped"]["credit"] == 1

    # The legitimately-absent case, in the same table, from the same sweep: `blocks_dropped`
    # carries it and `blocks_empty` stays empty, so the two are readable apart in SQL.
    thin = await db.fetchrow(
        "SELECT blocks_dropped, blocks_empty FROM title_placement WHERE title_id = $1",
        THIN_TITLE,
    )
    assert "keyword" in thin["blocks_dropped"] and thin["blocks_empty"] == []

    # Badged and parked like any other thin title (§5.3), and the job says which block and why.
    assert await db.fetchval(
        "SELECT placement FROM title WHERE id = $1", FULL_TITLE
    ) == "cold_tower"
    job = await db.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", FULL_TITLE)
    assert job is not None and job["stage"] == 2 and job["status"] == "parked"
    assert "credit" in job["reason"]
    assert job["detail"]["blocks_empty"] == ["credit"]
    assert job["detail"]["blocks_unmapped"]["credit"] == 1


async def test_the_sweep_names_a_block_whose_declared_columns_its_keys_never_hit(
    db, spine, grammar_bundle
):
    """The row's last clause: "an import-time check reports any block whose declared columns the
    builder's keys never hit."

    §5.3 makes bundle import a placement trigger, so the sweep the import runs *is* the
    import-time check, and it draws the distinction a per-title verdict cannot. One title whose
    crew the contract happens not to name is a fact about the title, and §8 stage 2 enrichment
    can change it. The same block hitting nothing on every title is a fact about the key
    grammar, which no enrichment can reach — so it is reported as its own line rather than left
    to be inferred from a parking backlog.
    """
    _store, report = await _sweep(db, grammar_bundle)
    assert report.blocks_never_hit == ["credit"]
    assert any("credit" in note for note in report.notes), report.notes


# --- the values, not just the columns ---------------------------------------------------------
#     (data-rules-feature-blocks-keyed-as-the-contract-names-them)
#
# Hitting the right column is half of §8 stage 9. The other half is writing the number the
# checkpoint was trained on, and the authority for that is the same file the columns came from:
# the corpus's exporter, `scripts/build_content.py`. Each test below names the builder line it
# is holding this app to, and each fails against a builder that hits every column and fills it
# from a distribution of its own.


def test_both_dna_tiers_enter_the_vector_as_presence_not_as_strength():
    """`build("dna_x", ...)` and `build("dna_p", ...)` are called without `weighted=True`, so
    every cell the corpus wrote in these 989 columns is 1.0 — measured over all 245,961 of the
    shipped `content_X.npz`'s nonzeros in the two blocks.

    Salience runs 1..3 and the projected weight is a fraction, so a builder that wrote either
    put the tower's two largest content blocks on a scale it had never seen. §4.1 rule 2 keeps
    those numbers out of the *predicate*; it does not license putting them in the cell.
    """
    contract = FeatureContract.load(_contract_doc())
    built = features.build_vector(
        contract,
        1,
        {"dna_x": {"dna:mood.dread": 3.0}, "dna_p": {"dna:mood.cosy": 0.25}},
        None,
    )
    assert built.vec[contract.block("dna_x").column("dna:mood.dread")] == 1.0
    assert built.vec[contract.block("dna_p").column("dna:mood.cosy")] == 1.0
    assert set(built.blocks_present) == {"dna_x", "dna_p"}


def test_an_uncovered_review_text_row_is_not_a_present_block(bundle_root):
    """§4.3's contract records the rule in its own preprocessing map:
    `missing_review_text: "zeros when covered=False"`.

    The shipped bundle sets `covered` False on 6,010 of its 14,397 rows and leaves their `emb`
    as float noise near 1e-16. Reading those rows anyway fed the tower a block of zeros while
    reporting it present — so §5.3's badge stayed off and §8 stage 2 parked nothing for 42% of
    the corpus. An uncovered row is not a row; the block drops, and the drop is recorded.

    The title below is thin for its eight OTHER absent blocks and not for this one. M4.13's
    step 31 took `review_text` out of the thinness verdict, because the block's source is the
    bundle's own npz and no §8 stage 2 re-fetch can write into it; not present and not thin are
    compatible, and `test_an_uncovered_review_text_row_is_recorded_but_parks_no_job` is the
    other half. [M4.13, cs-21]
    """
    artifacts = bundle_root / "artifacts"
    with np.load(artifacts / "review_text_emb.npz", allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    covered = arrays["covered"].copy()
    covered[arrays["title_ids"] == FULL_TITLE] = False
    np.savez(artifacts / "review_text_emb.npz", **{**arrays, "covered": covered})

    store = ArtifactStore.open(artifacts, "test-v1")
    contract = FeatureContract.from_store(store)
    emb = features.text_embeddings(store, [FULL_TITLE, 1])
    assert FULL_TITLE not in emb, "an uncovered row is not review text"
    assert 1 in emb, "…and a covered one still is"

    built = features.build_vector(
        contract, FULL_TITLE, {"meta": {"kind:movie": 1.0}}, emb.get(FULL_TITLE)
    )
    assert "review_text" in built.blocks_dropped
    assert "review_text" not in built.blocks_present
    assert not built.vec[contract.text_offset:].any()
    assert built.is_thin


async def test_the_genome_block_takes_the_corpus_relevance_cut(db, spine, bundle_root):
    """`build("genome", "… WHERE g.relevance >= 0.5", weighted=True)`.

    The cut is not a filter on a weight in §4.1 rule 2's sense — it is which rows the 983
    columns were counted from, and `content_X.npz` proves it: the block's smallest nonzero is
    exactly 0.5. Below it the tower was trained to see zero, and the shipped bundle has 587,502
    of its 888,023 genome scores down there.
    """
    await db.execute(
        "INSERT INTO ml_genome_score (ml_movie_id, tag_id, relevance)"
        " VALUES (110, 1, 0.49), (110, 2, 0.5)"
    )
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]

    # `g:heist` at 0.49 is below the cut; `g:dread` at exactly 0.5 is not — the corpus's
    # predicate is `>=`, and an off-by-one here moves a whole tag's column out of the block.
    assert set(keys["genome"]) == {"g:cooking", "g:dread"}
    assert keys["genome"]["g:dread"] == pytest.approx(0.5)


async def test_a_mixed_case_keyword_lands_in_the_contracts_lowercase_column(
    db, spine, bundle_root
):
    """`build("keyword", "SELECT title_id, 'kw:'||lower(trim(keyword)) …")`.

    15,096 of the bundle's 764,732 keyword rows are not already lower-cased, and every one of
    them missed its column outright — silently, because a miss is only ever counted.
    """
    await db.execute(
        "INSERT INTO title_keyword (title_id, keyword, source) VALUES ($1, ' Cooking ', 'imdb')",
        FULL_TITLE,
    )
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]
    assert set(keys["keyword"]) == {"kw:cooking"}


async def test_the_credit_block_takes_only_the_roles_the_corpus_built_its_columns_from(
    db, spine, bundle_root
):
    """The corpus's predicate, verbatim: the four above-the-line crafts, plus cast down to
    third billing and no further.

    Every one of the shipped bundle's 281,655 credit rows carries a `role_class`, so "any
    non-NULL role_class" is not a near-miss — it lights 52,421 rows the corpus left dark. The
    contract declares a `p:cast:Al Pacino` column and no `p:editor:` column at all, which is
    the same fact stated from the other side.
    """
    await db.execute(
        "INSERT INTO person (id, name) VALUES"
        " (11, 'Al Pacino'), (12, 'Thelma Schoonmaker'), (13, 'Bit Player'),"
        " (14, 'Unbilled Extra')"
    )
    await db.execute(
        "INSERT INTO credit (title_id, person_id, job, role_class, billing_order) VALUES"
        " ($1, 11, 'Actor',  'cast',   1),"      # top-billed: the corpus keeps it
        " ($1, 13, 'Actor',  'cast',   9),"      # ninth-billed: it does not
        " ($1, 14, 'Actor',  'cast',   NULL),"   # unbilled: `<= 3` is NULL, so neither does this
        " ($1, 12, 'Editor', 'editor', NULL)",   # a craft the 244 columns have no name for
        FULL_TITLE,
    )
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]
    assert set(keys["credit"]) == {"p:director:Wong Kar-wai", "p:cast:Al Pacino"}


async def test_the_meta_blocks_language_column_is_the_titles_original_language(
    db, spine, bundle_root
):
    """The corpus reads one column: `SELECT id, kind, year, runtime_min, original_language FROM
    title`, and writes `lang:{lang}` once.

    `title_language` is a different fact — the multi-source list of languages spoken in a title,
    averaging 2.98 distinct entries per title across the shipped bundle. Reading it here set
    three `lang:` columns on a typical film in a block whose training distribution has one.
    """
    await db.execute(
        "UPDATE title SET original_language = 'ja' WHERE id = $1", FULL_TITLE
    )
    await db.execute(
        "INSERT INTO title_language (title_id, language, source) VALUES"
        " ($1, 'en', 'tmdb'), ($1, 'fr', 'tmdb'), ($1, 'de', 'imdb')",
        FULL_TITLE,
    )
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]
    assert {k for k in keys["meta"] if k.startswith("lang:")} == {"lang:ja"}


# --- §5.3's budget (jellyfin-acquisition-eval-cpu-job-budgets) --------------------------------


async def test_cold_tower_placement_of_one_title_stays_under_one_second(db, placed):
    """§5.3: "Cold Tower placement of new/changed titles — acquisition pipeline (§8) — **<1
    s/title**", on a box with no GPU (§2).

    The measurement is the whole §8 stage 9–10 path for one title: the nine block queries, the
    vector, the forward pass, and the writes. Module load is excluded on purpose — it is
    per-process, not per-title, and the sweep in production has already paid it.

    Measured on the development box against this fixture: 17 ms for the whole path, of which
    0.03 ms is the forward pass. The headroom is ~60×, and the corpus contract is 50× wider than
    this one — which is why the assertion is the spec's budget and not the measurement.
    """
    store, _ = placed
    await db.execute(
        "INSERT INTO title (id, kind, name, year, runtime_min, is_owned, origin)"
        " VALUES (11, 'movie', 'La Jetée', 1962, 28, true, 'acquired')"
    )
    began = time.perf_counter()
    report = await reconcile.reconcile(db, store, scope="owned_missing")
    elapsed = time.perf_counter() - began

    assert report.placed == 1
    assert elapsed < 1.0, f"one-title placement took {elapsed * 1000:.0f} ms (§5.3 budget: 1 s)"

    # §1's own claim about the model, separately: "Cold Tower inference — sub-ms/title."
    # Measured: 0.027 ms/title on this contract. The bound below is loose enough to survive a
    # slower CI runner and tight enough that a per-call module reload (~1 s) fails it.
    contract = FeatureContract.from_store(store)
    cold = tower.load_tower(store, contract)
    x = np.zeros((1, contract.input_dim), dtype=np.float32)
    cold.place(x)
    began = time.perf_counter()
    for _ in range(20):
        cold.place(x)
    per_call = (time.perf_counter() - began) / 20
    assert per_call < 0.05, f"forward pass {per_call * 1000:.2f} ms/title"


# --- §10's rebuild set (jellyfin-acquisition-eval-reimport-rebuild-set) -----------------------


@pytest.fixture
async def reimported(db, placed, tmp_path):
    """A second bundle with a different Backbone and a changed column set, imported and staged.

    §10: "Bundle re-import (new vocabulary, retrained backbone) is a planned admin event." The
    new Backbone has lost title 7 (coverage shrank) and the new contract renames the
    `genre:comedy` column, so a title whose content did not change still gets a different
    vector — which is what "feature vectors rebuilt from the staged bundle's feature contract,
    whose column set may change" means in practice.
    """
    await _seed_observations(db)
    second = _make_bundle(
        tmp_path / "bundle2", version="test-v2",
        # The shipped grammar, with one column renamed: seven names for a seven-wide block,
        # `genre:comedy` becoming `genre:comedy-drama`. Bare `Crime`/`Comedy Drama` names were
        # the fixture agreeing with the builder about a grammar neither shares with the corpus.
        names={"genre": ["genre:crime", "genre:thriller", "genre:family", "genre:romance",
                         "genre:sci-fi", "genre:drama", "genre:comedy-drama"]},
    )
    _shrink_backbone(second / "artifacts", drop=7)
    # decision 162: content seeds once and the corpus re-ships models. §10's "bundle re-import
    # (new vocabulary, retrained backbone)" is therefore a MODELS-ONLY bundle — which is exactly
    # what this test needs, because everything it asserts is about the new basis and the new
    # contract, not about content arriving a second time.
    (second / "content.sqlite").unlink()
    (second / "reviews.sqlite").unlink()
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(second), tmp_path / "artifacts"
    )
    assert report.ok, report.render()
    return ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2")


async def _seed_observations(db) -> None:
    """§10: "Ledger *observations* always survive re-import (they reference `title.id` and
    vocabulary-independent facts)." Something has to exist for that to be falsifiable."""
    user = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('owner', 'admin') RETURNING id"
    )
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value, source) VALUES ($1, 1, 2, 'sweep'),"
        " ($1, 2, 0, 'sweep'), ($1, 8, 1, 'prompt')", user,
    )
    await db.execute(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, margin, context, selection)"
        " VALUES ($1, 1, 2, 'A', 1.6, 'profile_battle', 'random'),"
        "        ($1, 2, 8, 'TIE', 1.0, 'tier_queue', 'uniform_holdout')", user,
    )
    await db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 1, 6, 'drag_drop')",
        user,
    )


async def test_a_reimport_runs_exactly_the_four_rebuild_steps_and_no_map_rebuild(db, reimported):
    """§10: "Re-import therefore recomputes: user fold-in vectors (closed-form, ms),
    per-label-count blend weights, a full Ledger MAP refit, Cold Tower re-placement of every
    app-acquired title … (The v1 Map is a deterministic axis scatter and needs no rebuild — a
    future UMAP lens would recompute here.)"

    Four, named, in that order, and the axis scatter is not one of them: `dna_axis` and
    `dna_axis_weight` are authored TSVs (§6.4), so a step that rebuilt them would be recomputing
    an artifact the operator wrote by hand.
    """
    calls: list[str] = []

    def recorder(name):
        async def run(_conn, _store, version):
            calls.append(name)
            return {"version": version}
        return run

    axes_before = await db.fetch("SELECT * FROM dna_axis_weight ORDER BY facet, term")
    results = await reconcile.run_rebuild(
        db, reimported, "test-v2",
        fold_in=recorder("user-foldin"),
        blend_weights=recorder("blend-weights"),
        ledger_refit=recorder("ledger-map-refit"),
    )

    assert [r["id"] for r in results] == [
        "user-foldin", "blend-weights", "ledger-map-refit", "cold-tower-replacement"
    ]
    assert calls == ["user-foldin", "blend-weights", "ledger-map-refit"]
    assert len(results) == 4, "§10 names four things; a fifth is a bug, not an improvement"
    for result in results:
        text = f"{result['id']} {result['title']}".lower()
        for forbidden in reconcile.FORBIDDEN_STEP_WORDS:
            assert forbidden not in text, f"the rebuild set names a map rebuild: {text!r}"

    axes_after = await db.fetch("SELECT * FROM dna_axis_weight ORDER BY facet, term")
    assert [tuple(r) for r in axes_before] == [tuple(r) for r in axes_after]
    assert axes_after, "the fixture ships authored axes; an empty table proves nothing"

    # Step 4 did the work: coverage shrank by one title, so title 7 is no longer warm, and every
    # app-acquired title is re-placed in the new basis.
    assert await db.fetchval("SELECT placement FROM title WHERE id = 7") == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1 AND bundle_version = 'test-v2'",
        THIN_TITLE,
    ) == 1


async def test_a_reimport_rebuilds_vectors_from_the_staged_contract(db, reimported):
    """"…using feature vectors rebuilt from the staged bundle's contract (whose column set may
    differ)." Title 8's rows did not change; its `Comedy` column did — so its coordinate must."""
    await reconcile.run_rebuild(db, reimported, "test-v2")
    rows = await db.fetch(
        "SELECT bundle_version, e_hat, contract_sha256 FROM title_placement"
        " WHERE title_id = 8 ORDER BY bundle_version"
    )
    assert [r["bundle_version"] for r in rows] == ["test-v1", "test-v2"]
    assert rows[0]["contract_sha256"] != rows[1]["contract_sha256"]
    assert rows[0]["e_hat"] != rows[1]["e_hat"]
    # §10's rollback story: the previous bundle's rows survive rather than being overwritten.
    assert np.isfinite(np.frombuffer(rows[0]["e_hat"], dtype=np.float32)).all()


async def test_verdicts_duels_and_tier_edits_survive_a_reimport_unchanged(db, reimported):
    """§10: "Ledger *observations* always survive re-import (they reference `title.id` and
    vocabulary-independent facts)."

    Whole rows, not counts: an importer that renumbered ids, reset `created_at`, dropped
    `selection` or cleared `superseded_by` would keep the counts and destroy §13's holdout
    stream and §4.2's append-only guarantee.
    """
    before = {
        table: [tuple(r) for r in await db.fetch(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("verdict", "duel", "tier_edit")
    }
    await reconcile.run_rebuild(db, reimported, "test-v2")
    after = {
        table: [tuple(r) for r in await db.fetch(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("verdict", "duel", "tier_edit")
    }
    assert before == after
    assert len(before["verdict"]) == 3 and len(before["duel"]) == 2 and len(before["tier_edit"]) == 1


async def test_the_rebuild_refuses_a_bundle_that_was_never_staged(db, placed, tmp_path):
    """§10's invariant — "no process may score or refit with a loaded bundle version different
    from the active row" — has exactly one exception: the rebuild set is recomputed against the
    *staged* bundle, before the flip. That exception is a positive check, not a skipped one."""
    store, _ = placed
    fake = ArtifactStore.open(store.root, "never-imported")
    with pytest.raises(RuntimeError, match="staged"):
        await reconcile.run_rebuild(db, fake, "never-imported")


# --- M4.13: thinness is a claim about what enrichment can fix, and the chunk is one transaction -
#     (data-rules-the-sweep-parks-only-jobs-that-can-be-finished)
#
# Two findings that look like one and are not. cs-21 NARROWS what thin means: `is_thin` counted
# any dropped block, so 46 of the 130 titles the sweep places were thin against §5.3's stated 5 -
# 40 of them for having won no awards, which is an acquisition job that can never close.
# `data-rules-a-feature-block-that-never-hits-is-not-present` (M4.5) WIDENS it: a block whose keys
# all miss the contract is thin even though nothing about the title is absent. Both are right, and
# the pair is what these tests hold apart. ml05 is the other half: the sweep's three statements per
# chunk were not transactional and its work list keyed on `title_placement` while §12 keys on
# `title.placement`, so an interrupted chunk left a title permanently outside the count.
# [M4.13, cs-21 and ml05; plan steps 30 and 31]


class _KilledBeforeTheBadge:
    """`conn` with one chunk's badge UPDATE replaced by a raise.

    The window ml05 reproduced is a worker killed between `executemany(_UPSERT)` and the
    `UPDATE title SET placement = 'cold_tower'` that follows it, which no test can produce with a
    signal. What it can produce is that statement failing, which opens the same window from the
    other side: every module in this package takes `conn: Any` and routes every statement through
    it, so the sweep can be handed a connection one of whose statements does not work.

    Only the badge, and only from the `after`-th chunk on, so the chunk before it is the control:
    a sweep that wrote nothing at all would pass an atomicity test for the wrong reason.
    """

    def __init__(self, conn, *, after: int):
        self._conn = conn
        self._after = after
        self.badges = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args, **kwargs):
        if "placement = 'cold_tower'" in sql:
            self.badges += 1
            if self.badges >= self._after:
                raise RuntimeError("the worker was killed before it could badge the chunk")
        return await self._conn.execute(sql, *args, **kwargs)


def test_a_block_whose_keys_all_miss_the_contract_is_still_thin():
    """The two rules, on one title, at the same block.

    §5.3's backlog is "thin ones (2 lack keywords, 3 lack any DNA row)" - five titles out of
    nineteen - and `is_thin` returned True for 46 of 130 because it counted every dropped block.
    The award block's source is one COUNT over `award`, so a title nobody nominated drops it; §8
    stage 2 is a *fetch*, and no fetch makes an awards body give a 1983 film a prize. That is the
    argument `features.py` already makes two lines above for the genome, and `UNENRICHABLE_BLOCKS`
    is it written down once.

    What the same narrowing must not reach: a block that produced rows and landed none of the
    columns the contract declares. §4.3 makes `feature_contract.json` "the **exhaustive**
    definition of the tower's input", so those keys are a grammar disagreement between the builder
    and the export - and that IS fixable, by a re-fetch whose keys the contract names. `award` is
    deliberately the block used for both halves here, because a rule keyed on the block NAME would
    pass the first assertion and fail this one.
    """
    contract = FeatureContract.load(_contract_doc())
    text = np.zeros(contract.text_dims, dtype=np.float32)
    complete = {b.name: {b.names[0]: 1.0} for b in contract.blocks}
    assert not features.build_vector(contract, 1, complete, text).is_thin, (
        "a title with every block populated is thin, so this fixture cannot tell the rules apart"
    )

    absent = features.build_vector(
        contract, 1, {k: v for k, v in complete.items() if k != "award"}, text
    )
    assert absent.blocks_dropped == ("award",) and absent.blocks_empty == ()
    assert not absent.is_thin, "an award nobody gave is a job §8 stage 2 can never finish"

    all_miss = features.build_vector(contract, 1, {**complete, "award": {"won": 1.0}}, text)
    assert all_miss.blocks_dropped == (), "nothing about this title is absent"
    assert all_miss.blocks_empty == ("award",)
    assert all_miss.unmapped == {"award": 1}
    assert all_miss.is_thin, (
        "a block keyed `won` against `award:won` columns is a grammar the contract does not "
        "declare, and §8 stage 2's re-fetch is the remedy"
    )

    # The genome reaches `blocks_imputed` rather than `blocks_dropped` when the contract declares
    # its zero-imputation, so it was already excused - and it is in the tuple anyway, because the
    # verdict must not depend on which of the two lists a block lands in. A bundle that stopped
    # recording the imputation would otherwise silently re-park 983 columns' worth of absence.
    no_genome = features.build_vector(
        contract, 1, {k: v for k, v in complete.items() if k != "genome"}, text
    )
    assert no_genome.blocks_imputed == ("genome",) and no_genome.blocks_dropped == ()
    assert not no_genome.is_thin
    assert "genome" in features.UNENRICHABLE_BLOCKS


async def test_a_title_whose_only_gap_is_an_award_it_never_won_is_not_parked(
    db, spine, bundle_root
):
    """§5.3: "thin ones ... are still placed, badged, and parked as acquisition jobs for M5
    enrichment." Placed and badged is unchanged; the parking is what narrows.

    Measured over the 130 titles the sweep places on the real bundle: award absent 40, review_text
    uncovered 27, dna_x 11, dna_p 4, keyword 2 - 46 thin, and §5.3 states the whole backlog as 5 of
    19. Forty of those jobs waited at stage 2 for an enrichment that cannot arrive, on the board
    §8.4's flywheel reads.

    `THIN_TITLE` is in the same sweep and still parks, which is what keeps this from passing as
    "park nothing".
    """
    await db.execute("DELETE FROM award WHERE title_id = $1", FULL_TITLE)
    _store, report = await _sweep(db, bundle_root)

    placed = await db.fetchrow(
        """
        SELECT t.placement, p.blocks_dropped, p.blocks_empty, p.blocks_imputed
          FROM title t JOIN title_placement p ON p.title_id = t.id
         WHERE t.id = $1 AND p.bundle_version = 'test-v1'
        """,
        FULL_TITLE,
    )
    assert placed is not None, "the title was not placed at all, so nothing below means anything"
    # §8 stage 10's badge input, unchanged: the title is ready, visible and rankable.
    assert placed["placement"] == "cold_tower"
    assert placed["blocks_dropped"] == ["award"], placed["blocks_dropped"]
    assert placed["blocks_empty"] == []
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", FULL_TITLE
    ) == 0, "an award nobody gave parked a stage-2 job that can never close"

    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", THIN_TITLE
    ) == 1, "the title that lacks keywords and DNA still parks, or this asserts nothing"
    assert (report.placed, report.parked_thin) == (2, 1), report.as_dict()


async def test_an_uncovered_review_text_row_is_recorded_but_parks_no_job(db, spine, tmp_path):
    """`covered = False` is a property of the EXPORT, not of this install.

    The review-text block's source is not the database at all: it is `review_text_emb.npz` inside
    the bundle (§4.3, `features.text_embeddings`). §8 stage 2 writes database rows, so it cannot
    reach the block either way - and for a row the corpus itself marks uncovered, with `emb` at
    float noise near 1e-16, the only thing that changes the answer is the next export. The shipped
    bundle sets the flag False on 6,010 of 14,397 rows.

    `test_an_uncovered_review_text_row_is_not_a_present_block` is the other half and still holds:
    the block is not *present*, its 64 columns stay zero, and the drop is recorded. Not present and
    not thin are compatible.
    """
    root = _make_bundle(tmp_path / "uncovered")
    artifacts = root / "artifacts"
    with np.load(artifacts / "review_text_emb.npz", allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    covered = arrays["covered"].copy()
    covered[arrays["title_ids"] == FULL_TITLE] = False
    np.savez(artifacts / "review_text_emb.npz", **{**arrays, "covered": covered})

    _store, report = await _sweep(db, root)

    placed = await db.fetchrow(
        "SELECT blocks_dropped, blocks_present FROM title_placement"
        " WHERE title_id = $1 AND bundle_version = 'test-v1'",
        FULL_TITLE,
    )
    assert placed is not None
    assert placed["blocks_dropped"] == ["review_text"], placed["blocks_dropped"]
    assert "review_text" not in placed["blocks_present"]
    assert await db.fetchval(
        "SELECT placement FROM title WHERE id = $1", FULL_TITLE
    ) == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", FULL_TITLE
    ) == 0, "a row the corpus marks uncovered parked a job only a new bundle could close"
    assert (report.placed, report.parked_thin) == (2, 1), report.as_dict()


async def test_the_report_still_names_the_blocks_a_placed_title_dropped(db, spine, bundle_root):
    """Only the verdict changes. The facts stay where §5.3's badge and §8.4's flywheel read them.

    `title_placement.blocks_dropped` is the persisted record and is untouched - 0015_seed wrote
    those columns precisely so a block that filled every column it had and a block that hit none
    of them stop producing identical rows. And the run-level report names the narrowing out loud,
    because `parked_thin` dropping is otherwise indistinguishable from a library that improved:
    the operator reading §6.6's job detail is the only person who can tell a backlog that shrank
    because titles were enriched from one that shrank because the rule moved.
    """
    await db.execute("DELETE FROM award WHERE title_id = $1", FULL_TITLE)
    _store, report = await _sweep(db, bundle_root)

    persisted = {
        r["title_id"]: r["blocks_dropped"]
        for r in await db.fetch("SELECT title_id, blocks_dropped FROM title_placement")
    }
    assert "award" in persisted[FULL_TITLE], persisted
    assert "award" in persisted[THIN_TITLE] and "review_text" in persisted[THIN_TITLE]

    note = next((n for n in report.notes if "not parked" in n), None)
    assert note is not None, report.notes
    assert "1 title(s)" in note and "award" in note, note
    assert note.isprintable(), note

    # The one report that does still exist for a thin title names every gap, enrichable or not:
    # the reason line is what the §8.4 board shows, and a stage-2 fetch that knows the title also
    # lacks an award it will not find is better informed than one that does not.
    job = await db.fetchrow("SELECT reason, detail FROM acquisition_job WHERE title_id = $1",
                            THIN_TITLE)
    assert "award" in job["reason"] and "review_text" in job["reason"], job["reason"]
    assert "award" in job["detail"]["blocks_dropped"]


async def test_each_sweep_chunk_is_one_transaction(db, spine, bundle_root, monkeypatch):
    """ml05, reproduced: a chunk that dies after its upsert used to leave a coordinate behind.

    `title_placement` row written, `title.placement` still `'unplaced'`, and `_MISSING_SQL` keyed
    on `NOT EXISTS (title_placement ...)` - so the work list never revisited the title while
    `placement_counts` kept counting it as unplaced. §12's M2 query never reaches 0, and
    `serve.coordinates` ranks the title perfectly happily without its badge.

    One title per chunk here, so the first chunk is the control: per-chunk atomicity means the
    completed chunk survives. All-or-nothing over the whole sweep would be a different (and
    worse) promise - a corpus-scale sweep is 28 chunks of 512 and one bad title must not cost
    the night.
    """
    monkeypatch.setattr(reconcile, "CHUNK", 1)
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    conn = _KilledBeforeTheBadge(db, after=2)

    with pytest.raises(RuntimeError, match="killed"):
        await reconcile.reconcile(conn, store, scope="owned_missing")
    assert conn.badges == 2, "the second chunk has to have been reached, not the first"

    # The chunk that finished: placed, badged, parked.
    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", THIN_TITLE) == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", THIN_TITLE
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", THIN_TITLE
    ) == 1

    # The chunk that died: nothing at all, which is the only state §12's count can read.
    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", FULL_TITLE) == "unplaced"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", FULL_TITLE
    ) == 0, "the upsert outlived the badge, so this title has a coordinate and no placement"


async def test_a_title_with_a_placement_row_and_no_stamp_is_re_swept(db, spine, bundle_root):
    """The work list and §12's count have to mean the same thing.

    `_MISSING_SQL` asked "has this title a `title_placement` row for the active bundle?" while
    `placement_counts` - and §12's M2 exit criterion - ask "is `title.placement` still
    'unplaced'?". A title in the gap between those two questions is stranded for ever: every
    nightly sweep skips it and every count keeps it. The upsert is `ON CONFLICT DO UPDATE`, so
    re-placing one is idempotent and costs a forward pass.
    """
    _store, first = await _sweep(db, bundle_root)
    assert (first.considered, first.placed) == (2, 2), first.as_dict()

    # Exactly the state the previous test's interrupted chunk used to leave behind.
    await db.execute(
        "UPDATE title SET placement = 'unplaced', placement_bundle = NULL WHERE id = $1",
        FULL_TITLE,
    )
    assert await reconcile.titles_needing_placement(
        db, bundle_version="test-v1", scope="owned_missing"
    ) == [FULL_TITLE], "the sweep's work list cannot see a title §12's count is still holding"

    _store, again = await _sweep(db, bundle_root)
    assert (again.considered, again.placed) == (1, 1), again.as_dict()
    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", FULL_TITLE) == "cold_tower"
    counts = await reconcile.placement_counts(db, bundle_version="test-v1")
    assert counts["owned_unplaced"] == 0, counts
    assert counts["placement_rows"] == 2, counts
