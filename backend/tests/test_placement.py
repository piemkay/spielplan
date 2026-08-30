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
REAL_KEYS: dict[str, list[str]] = {
    "dna_x": ["themes.obsession", "character.morally-grey", "mood.dread", "sensibility.bleak",
              "mood.cosy", "visual.neon", "themes.surveillance", "pacing.relentless",
              "register.deadpan"],
    "dna_p": ["themes.obsession", "mood.dread", "mood.cosy", "era.period", "structure.procedural",
              "visual.neon", "pacing.patient", "place.domestic"],
    "genome": ["heist", "dread", "cooking"],
    "genre": ["Crime", "Thriller", "Family", "Romance", "Sci-Fi", "Drama", "Comedy"],
    "keyword": ["heist", "investigation", "family", "cooking"],
    "credit": ["1", "2", "3", "4", "5"],
    "country": ["US", "HK", "JP"],
    "award": ["award_nominations", "award_wins"],
    "meta": ["kind:movie", "kind:series", "year_norm", "runtime_norm", "n_credits_log"],
}

THIN_TITLE = 9        # no keywords, no DNA row, no genre, no credit, no reviews — only `meta`
FULL_TITLE = 10       # every one of the nine blocks, plus a review-text row


# --- helpers ---------------------------------------------------------------------------------


def _names(block: str, size: int) -> list[str]:
    real = REAL_KEYS.get(block, [])[:size]
    return real + [f"__pad_{i}" for i in range(size - len(real))]


def _contract_doc(*, sizes: dict[str, int] | None = None, text_scale: float = 0.031_25,
                  names: dict[str, list[str]] | None = None) -> dict:
    """A contract document in the shape the bundle ships, with real feature names."""
    blocks = dict(sizes or {"dna_x": 12, "dna_p": 10, "genome": 8, "genre": 9, "keyword": 11,
                            "credit": 6, "country": 4, "award": 2, "meta": 5})
    feature_names = {b: _names(b, n) for b, n in blocks.items()}
    for block, override in (names or {}).items():
        feature_names[block] = override
    return {
        "blocks": blocks,
        "block_order": list(blocks),
        "feature_names": feature_names,
        "review_text": {"svd_dims": 256, "used": 64, "order": "singular-value"},
        "text_scale": text_scale,
        "genome_imputation": "zero",
        "absent_blocks": "dropped",
    }


def _realistic_contract(artifacts: Path, **kwargs) -> None:
    """Rewrite the bundle's contract column *names* without touching a single declared size."""
    shipped = json.loads((artifacts / "feature_contract.json").read_text(encoding="utf-8"))
    doc = _contract_doc(sizes=shipped["blocks"], text_scale=shipped["text_scale"], **kwargs)
    (artifacts / "feature_contract.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")


def _extend_text_embeddings(artifacts: Path, extra: list[int]) -> None:
    """Give `extra` titles a review-text row, so the ninth block can be *present* somewhere."""
    npz = np.load(artifacts / "review_text_emb.npz", allow_pickle=False)
    ids = np.concatenate([npz["title_id"], np.array(extra, dtype=np.int32)])
    rng = np.random.default_rng(7)
    emb = np.concatenate([
        npz["emb"], rng.normal(size=(len(extra), npz["emb"].shape[1])).astype(np.float32)
    ])
    order = np.argsort(ids)
    np.savez(artifacts / "review_text_emb.npz", title_id=ids[order], emb=emb[order],
             components=npz["components"])


def _shrink_backbone(artifacts: Path, drop: int) -> None:
    """A different Backbone: one title's row is gone, so its coverage shrank (§10)."""
    npz = np.load(artifacts / "backbone.npz", allow_pickle=False)
    keep = npz["title_id"] != drop
    np.savez(artifacts / "backbone.npz", title_id=npz["title_id"][keep], E=npz["E"][keep],
             E_full=npz["E_full"][keep], b_i=npz["b_i"][keep], mu=npz["mu"],
             item_n=npz["item_n"][keep])


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
    await db.execute("INSERT INTO credit (title_id, person_id, job) VALUES (10, 3, 'Director')")
    await db.execute("INSERT INTO title_country (title_id, country) VALUES (10, 'JP')")
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
        " VALUES (10, 'v1', 'deadpan', 'register', 2, 0.6, 'gemini')"
    )
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via)"
        " VALUES (10, 'v1', 'domestic', 'place', 0.3, 'keyword:cooking')"
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

    rows = {"country": {"JP": 1.0}, "award": {"award_wins": 2.0}}
    assert np.nonzero(features.build_vector(wide, 1, rows, None).vec)[0].tolist() == [
        wide.block("country").column("JP"), wide.block("award").column("award_wins")
    ]
    assert np.nonzero(features.build_vector(narrow, 1, rows, None).vec)[0].tolist() == [
        wide.block("country").column("JP") - 1, wide.block("award").column("award_wins") - 1
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
    rows = {"genre": {"Crime": 1.0}}
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
    doc.pop("text_scale")
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
        contract, 1, {"genre": {"Crime": 1.0, "Cyberpunk": 1.0}}, None
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

    state = torch.load(store.path("cold_tower.pt"), map_location="cpu",
                       weights_only=True)["state_dict"]
    w = {k: v.numpy() for k, v in state.items()}

    rng = np.random.default_rng(11)
    x = rng.normal(size=(3, contract.input_dim)).astype(np.float32)
    h = x
    for i in sorted(int(k.split(".")[1]) for k in w if k.startswith("trunk.") and "weight" in k):
        h = np.maximum(h @ w[f"trunk.{i}.weight"].T + w[f"trunk.{i}.bias"], 0.0)
    expect_e = h @ w["embed.weight"].T + w["embed.bias"]
    expect_b = (h @ w["prior.weight"].T + w["prior.bias"]).reshape(-1)

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
    assert row["input_dim"] == 131
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
    Title 1 has 4218, so it is warm and takes E outright. Title 8 has no Backbone row at all.
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
    contract names for it — salience 2 in the extracted tier's column, the projected tier's
    strength in *its* column (§4.1 rule 1: two tiers, never merged), genome relevance, the
    award counts, and the meta grammar's two continuous productions.
    """
    store, _report = placed
    contract = FeatureContract.from_store(store)
    built = (await features.build_vectors(
        db, store, contract, [FULL_TITLE], vocab_version="v1"
    ))[0]
    vec = built.vec

    assert vec[contract.block("dna_x").column("register.deadpan")] == pytest.approx(2.0)
    assert vec[contract.block("dna_p").column("place.domestic")] == pytest.approx(0.3)
    assert vec[contract.block("genome").column("cooking")] == pytest.approx(0.8, abs=1e-6)
    assert vec[contract.block("genre").column("Comedy")] == 1.0
    assert vec[contract.block("keyword").column("cooking")] == 1.0
    assert vec[contract.block("credit").column("3")] == 1.0
    assert vec[contract.block("country").column("JP")] == 1.0
    assert vec[contract.block("award").column("award_nominations")] == pytest.approx(1.0)
    assert vec[contract.block("award").column("award_wins")] == pytest.approx(1.0)
    # meta: year_norm = (1985 - 1900)/125, one credit -> log1p(1)/5.
    assert vec[contract.block("meta").column("year_norm")] == pytest.approx(85 / 125, abs=1e-6)
    assert vec[contract.block("meta").column("n_credits_log")] == pytest.approx(
        float(np.log1p(1) / 5), abs=1e-6
    )
    assert vec[contract.block("meta").column("kind:movie")] == 1.0
    assert vec[contract.block("meta").column("kind:series")] == 0.0

    # Every DB key the contract declares landed. The one block with leftovers is `meta`: this
    # fixture is a scale model that declares 5 of the corpus contract's 57 meta columns, so the
    # grammar's other productions have no column to go to. They are *counted* rather than
    # silently discarded, and no key anywhere widened the vector.
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
    new Backbone has lost title 7 (coverage shrank) and the new contract renames the `Comedy`
    genre column, so a title whose content did not change still gets a different vector — which
    is what "feature vectors rebuilt from the staged bundle's feature contract, whose column set
    may change" means in practice.
    """
    await _seed_observations(db)
    second = _make_bundle(
        tmp_path / "bundle2", version="test-v2",
        names={"genre": ["Crime", "Thriller", "Family", "Romance", "Sci-Fi", "Drama",
                         "Comedy Drama"] + ["__pad_0", "__pad_1"]},
    )
    _shrink_backbone(second / "artifacts", drop=7)
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
