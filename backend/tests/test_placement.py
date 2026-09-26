"""The fixture's placeholder column names would make every DB assertion vacuous, so
`_realistic_contract` renames them in place, keeping every declared size."""

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
from spielplan.scoring import backbone, foldin, serve
from tests.fixtures import make_bundle as fx

# Real keys per block, in the shipped contract's grammar, padded to each declared width.
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


# Meta is a closed grammar, so padding must be too: a `__pad_0` would be reported unproducible.
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
    """The corpus's shape: `content_blocks` as a list of {name, size} and one flat `feature_names` list."""
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
    shipped = json.loads((artifacts / "feature_contract.json").read_text(encoding="utf-8"))
    sizes = {b["name"]: b["size"] for b in shipped["content_blocks"]}
    doc = _contract_doc(sizes=sizes, text_scale=shipped["text_block"]["text_scale"], **kwargs)
    (artifacts / "feature_contract.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")


def _extend_text_embeddings(artifacts: Path, extra: list[int]) -> None:
    npz = np.load(artifacts / "review_text_emb.npz", allow_pickle=False)
    ids = np.concatenate([npz["title_ids"], np.array(extra, dtype=np.int32)])
    rng = np.random.default_rng(7)
    emb = np.concatenate([
        npz["emb"], rng.normal(size=(len(extra), npz["emb"].shape[1])).astype(np.float32)
    ])
    order = np.argsort(ids)
    np.savez(artifacts / "review_text_emb.npz", title_ids=ids[order], emb=emb[order],
             covered=np.ones(ids.size, dtype=bool), singular=npz["singular"])


def _uncover_backbone_row(artifacts: Path, title_id: int) -> None:
    """Flags the row in `cold_mask` rather than deleting it: a backbone that drops a bundle title's row
    is refused on import (decision 248). E is zeroed beside the flag so mask and coordinates agree."""
    with np.load(artifacts / "backbone.npz", allow_pickle=False) as npz:
        arrays = {k: npz[k] for k in npz.files}
    row = np.asarray(arrays["title_ids"]).reshape(-1) == title_id
    assert row.any(), f"title {title_id} has no Backbone row to uncover"
    e = np.array(arrays["E"], copy=True)
    e[row] = 0.0
    arrays["E"] = e
    mask = np.array(arrays.get("cold_mask", np.zeros(row.size, dtype=bool)), copy=True).reshape(-1)
    mask[row] = True
    arrays["cold_mask"] = mask
    np.savez(artifacts / "backbone.npz", **arrays)


def _make_bundle(root: Path, *, version: str = "test-v1", **contract_kwargs) -> Path:
    fx.make_bundle(root, version=version)
    _realistic_contract(root / "artifacts", **contract_kwargs)
    _extend_text_embeddings(root / "artifacts", [FULL_TITLE])
    # Both rewrites land after BUNDLE.json was written, so re-state its tree-derived keys.
    fx.reinventory(root)
    return root


async def _seed_extra_titles(db) -> None:
    """`FULL_TITLE` exists so "thin titles are parked" is falsifiable against "park everything"."""
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
    # Written by hand: this build's importer skips the MovieLens tables (decision 291), but an
    # older install still has them (decision 311), and `features._genome` reads them there.
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
    """Yields (store, report)."""
    artifacts_root = tmp_path / "artifacts"
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(bundle_root), artifacts_root
    )
    assert report.ok, report.render()
    await _seed_extra_titles(db)
    store = ArtifactStore.open(artifacts_root / "test-v1", "test-v1")
    return store, await reconcile.reconcile(db, store, scope="owned_missing")


def test_every_block_lands_at_the_offset_the_contract_declares():
    """The assertion a hardcoded offset table would pass; the next two are the ones it cannot."""
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
    """Frozen means read from the file; columns 64..255 appear in the vector nowhere."""
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
    """A default would silently move every coordinate while the vectors stay plausible."""
    doc = _contract_doc()
    doc["text_block"].pop("text_scale")
    with pytest.raises(ContractError, match="text_scale"):
        FeatureContract.load(doc)


def test_genome_is_zero_imputed_and_an_absent_block_is_dropped_not_defaulted():
    """Both are all-zero slices; nothing may be filled with a mean or a prior."""
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
    assert built.nnz == 1
    assert built.is_thin


def test_a_key_the_contract_does_not_declare_is_counted_and_never_grows_the_vector():
    """Inventing a column would shift every later column out from under the tower."""
    contract = FeatureContract.load(_contract_doc())
    built = features.build_vector(
        contract, 1, {"genre": {"genre:crime": 1.0, "genre:cyberpunk": 1.0}}, None
    )
    assert built.unmapped == {"genre": 1}
    assert built.vec.size == contract.input_dim
    assert built.nnz == 1


def test_the_shipped_bundle_ships_a_contract_that_matches_its_own_tower(bundle_root):
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


def test_the_forward_pass_is_the_checkpoints_own_arithmetic(bundle_root):
    """The oracle is the checkpoint's weights recomputed in numpy: a zero stub passes every shape check."""
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
    other, _ = cold.place(x + 1.0)
    assert not np.allclose(e_hat, other)


def test_the_tower_is_in_eval_mode_and_two_passes_agree_bit_for_bit(bundle_root):
    """The sweep is idempotent by contract, so a module in training mode is refused."""
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
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    doc = _contract_doc(sizes={"dna_x": 12, "dna_p": 10, "genome": 8, "genre": 9, "keyword": 10,
                               "credit": 6, "country": 4, "award": 2, "meta": 5})
    narrow = FeatureContract.load(doc, sha256="deadbeef" * 8)
    assert narrow.input_dim == 130
    with pytest.raises(tower.TowerError, match=r"131 input columns|130"):
        tower.load_tower(store, narrow)


def test_nothing_in_the_placer_reaches_for_a_gpu(bundle_root):
    """§2: no GPU. `map_location` only helps if nothing later moves a tensor, hence the static half."""
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    cold = tower.load_tower(store, FeatureContract.from_store(store))
    assert [p.device.type for p in cold.module.parameters()] == ["cpu"] * 8

    source = Path(tower.__file__).read_text(encoding="utf-8")
    assert 'map_location="cpu"' in source
    for forbidden in (".cuda(", "cuda:", "device=", "torch.device"):
        assert forbidden not in source, f"tower.py names a device: {forbidden!r}"


async def test_a_zeroed_backbone_row_is_demoted_and_swept_rather_than_left_warm(db, tmp_path):
    """The shipped bundle has cold-masked rows above `WARM_SUPPORT` with E zeroed; support alone
    stamped them warm and the sweep skipped them. An existing warm stamp must be demoted."""
    root = tmp_path / "artifacts"
    root.mkdir()
    e = np.random.default_rng(20260906).standard_normal((3, 64)).astype(np.float32)
    e[1] = 0.0
    np.savez(
        root / "backbone.npz",
        title_ids=np.array([1, 2, 3], dtype=np.int32),
        E=e,
        b_i=np.array([0.4, 0.5, 0.6], dtype=np.float32),
        # Title 2 is the defect (zeroed, well above WARM_SUPPORT); title 3 is thin for the ordinary reason.
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
        # `title_placement_has_basis` (0023) refuses a placed title with a NULL `placement_bundle`.
        await db.execute(
            "INSERT INTO title (id, kind, name, is_owned, placement, placement_bundle) "
            "VALUES ($1, 'movie', $2, true, 'warm', 'cold-v1')",
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

    needing = await reconcile.titles_needing_placement(
        db, bundle_version="cold-v1", scope="owned_missing"
    )
    assert needing == [2, 3]


async def test_the_shared_fixtures_flagged_row_is_swept_and_served_from_the_tower(db, placed):
    """`item_n` above `WARM_SUPPORT` is the trap: on support alone the flagged row was stamped warm."""
    store, _report = placed

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

    # Excluding the coordinate must not zero the crowd count: `title_prior.item_n` feeds §6.1's
    # P(seen) and the card payload. `gate` is 0 in the same row.
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
    # 57 content columns + 64 review-text, as the fixture's contract declares.
    assert row["input_dim"] == 121
    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", THIN_TITLE) == "cold_tower"


async def test_every_owned_title_has_a_coordinate_after_reconciliation(db, placed):
    """Warm is `item_n >= WARM_SUPPORT` (90 at k = 10), not "has a Backbone row". Supports for
    titles 1-7 are 4218/900/120/30/6/240/55: 1,2,3,6 warm; 4,5,7 blended; 8,9,10 cold."""
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

    # The import already placed the bundle's own titles (§10 step 4); only the two seeded later remain.
    assert report.placed == 2 and report.failed == 0
    assert report.considered == 2


async def test_a_low_support_title_is_served_as_a_genuine_blend_of_both_coordinates(db, placed):
    """Title 5: item_n = 6, so gate = 6/16 = 0.375. Title 1 is warm; title 8 is cold-masked."""
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
    assert not np.allclose(blended.e, e_backbone)
    assert not np.allclose(blended.e, e_hat)

    assert coords[1].e_source == "backbone"
    np.testing.assert_allclose(coords[1].e, bb.embedding(1), rtol=1e-6)
    assert coords[8].e_source == "cold_tower"


async def test_the_cold_tower_coordinate_is_written_unscaled(db, placed):
    """Decision 236: the app rescales nothing, so the stored bytes must equal a re-run of the tower."""
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
        # The float32 round trip is the only transformation (0008: "64 x float32 LE").
        assert np.array_equal(stored[title_id][0], backbone.unpack_vec(
            e_hat[index].astype(np.float32).tobytes()
        )), f"title {title_id}'s stored e_hat is not the tower's own output"
        assert stored[title_id][1] == pytest.approx(float(b_hat[index]), abs=1e-6)

    norms = {t: float(np.linalg.norm(stored[t][0])) for t in ids}
    assert not any(abs(n - 1.0) < 1e-6 for n in norms.values()), (
        f"a stored e_hat has unit norm: something normalised it on the way in ({norms})"
    )
    bb_backbone = backbone.load_for(store)
    report = backbone.blend_ratios(bb_backbone, {t: stored[t] for t in ids})
    print(f"\nblend ratio over the swept rows: {report.as_dict()}")
    assert report.n_measured >= 1, "no thin row was placed, so the blend has nothing to weigh"


async def test_a_thin_title_is_placed_badged_and_parked_and_a_complete_one_is_not(db, placed):
    """Parked means the job waits at §8 stage 2; `FULL_TITLE` gets no job, so "park everything" fails."""
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
    """Column names are the shipped grammar (`genre:comedy`, `kw:cooking`, `p:director:...`), not
    the bare database values the builder emits."""
    store, _report = placed
    contract = FeatureContract.from_store(store)
    built = (await features.build_vectors(
        db, store, contract, [FULL_TITLE], vocab_version="v1"
    ))[0]
    vec = built.vec

    # Presence, not salience or weight: the corpus built both DNA blocks as presence.
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

    # Only `meta` has leftovers: its shipped columns are one-hots only, so continuous productions
    # and `has:` flags have no column. They are counted, not discarded.
    assert set(built.unmapped) == {"meta"}
    assert built.unmapped["meta"] > 0
    assert built.vec.size == contract.input_dim


async def test_reconciliation_is_idempotent(db, placed):
    """Runs on every import and nightly; a second sweep must be a no-op."""
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
    store, _ = placed
    assert 8 not in reconcile.warm_title_ids(store)
    assert await db.fetchval("SELECT placement FROM title WHERE id = 8") == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = 8"
    ) == 1


async def test_the_sweep_places_what_the_household_rated_or_is_asked_to_rate(db, placed):
    """A title with none of decision 470's reasons is still left alone; `all_missing` is the admin's."""
    store, _ = placed
    for title_id, name in ((11, "Rated, Unowned"), (12, "On The Seed List"), (13, "Nobody's")):
        await db.execute(
            "INSERT INTO title (id, kind, name, year, runtime_min, is_owned) "
            "VALUES ($1, 'movie', $2, 2017, 94, false)",
            title_id, name,
        )
    user = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    await db.execute("INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 11, 2)", user)
    position = await db.fetchval("SELECT COALESCE(max(position), 0) + 1 FROM seed_list")
    await db.execute(
        "INSERT INTO seed_list (position, title_id, decade) VALUES ($1, 12, 2010)", position
    )

    assert await reconcile.titles_needing_placement(
        db, bundle_version="test-v1", scope="owned_missing"
    ) == [11, 12]
    basis = backbone.load_for(store)
    labels = await foldin.live_labels(db, user_id=user, kind="movie")
    before = await serve.coordinates(db, basis, bundle_version="test-v1", kind="movie")
    assert foldin.fit_user(labels, before, list(before.values())).dropped == 1

    report = await reconcile.reconcile(db, store, scope="owned_missing")
    assert report.placed == 2 and report.failed == 0
    after = await serve.coordinates(db, basis, bundle_version="test-v1", kind="movie")
    assert foldin.fit_user(labels, after, list(after.values())).dropped == 0, (
        "a verdict still sits on a title with no coordinate after the sweep"
    )
    placed_now = await db.fetch(
        "SELECT title_id FROM title_placement WHERE title_id = ANY(ARRAY[11, 12, 13]) "
        " ORDER BY title_id"
    )
    assert [r["title_id"] for r in placed_now] == [11, 12]
    assert await db.fetchval("SELECT placement FROM title WHERE id = 13") == "unplaced"

    # Placed, still not owned: owned counters and "New in the library" read `is_owned`.
    counts = await reconcile.placement_counts(db, bundle_version="test-v1")
    assert counts["owned_cold"] == 6 and counts["placement_rows"] == 8
    again = await reconcile.reconcile(db, store, scope="owned_missing")
    assert again.considered == 0


# Every entry traces to one INSERT in `_seed_extra_titles`.
CONTRACT_COLUMNS: dict[str, tuple[str, float]] = {
    # Both DNA tiers are presence: salience and weight never reached a corpus cell.
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
    """Credit columns keyed `credit:<n>`: the shape a `person_id::text` builder agrees with."""
    root = _make_bundle(tmp_path / "grammar")
    fx.break_contract_block_grammar(root)
    return root


@pytest.fixture
async def spine(db):
    """Inserted directly, not through `import_bundle`, so no other import defect decides the outcome."""
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
    """Returns (store, report)."""
    store = ArtifactStore.open(root / "artifacts", "test-v1")
    return store, await reconcile.reconcile(db, store, scope="owned_missing")


def test_present_absent_and_all_keys_missing_are_three_distinguishable_states():
    """The same bytes in the vector, so only bookkeeping tells them apart; `present` means a
    declared column was hit."""
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

    # The four names partition the contract's blocks.
    named = [set(built.blocks_present), set(built.blocks_dropped),
             set(built.blocks_imputed), set(built.blocks_empty)]
    assert sum(len(s) for s in named) == len(set().union(*named))
    assert set().union(*named) == {*contract.block_names, "review_text"}

    kw = contract.block("keyword")
    assert not built.vec[kw.offset:kw.stop].any()
    assert built.nnz == 1


def test_is_thin_sees_a_block_whose_keys_all_miss():
    """The same title with one block's keys in an undeclared grammar is thin, with nothing absent."""
    contract = FeatureContract.load(_contract_doc())
    text = np.zeros(contract.text_dims, dtype=np.float32)
    complete = {b.name: {b.names[0]: 1.0} for b in contract.blocks}
    assert not features.build_vector(contract, 1, complete, text).is_thin

    built = features.build_vector(contract, 1, {**complete, "credit": {"3": 1.0}}, text)
    assert built.blocks_dropped == (), "nothing about this title is absent"
    assert built.blocks_empty == ("credit",)
    assert built.is_thin


async def test_every_content_block_is_keyed_as_the_contract_names_it(db, spine, bundle_root):
    """Raw builder keys are asserted before the contract is consulted: a vector-only check scores
    the builder against a contract written from the same reading."""
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

    # Only `meta` has leftovers: its shipped columns are one-hots, while `META_PRODUCTIONS` also
    # produces `has:` flags and continuous columns.
    assert set(built.unmapped) == {"meta"}

    assert built.blocks_empty == ()
    assert set(built.blocks_present) == {*contract.block_names, "review_text"}
    assert report.blocks_never_hit == []


async def test_a_block_that_hit_nothing_is_persisted_and_parks_the_title(db, spine, grammar_bundle):
    """Without the persisted counts this placement is identical to one where every column landed."""
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

    # Absent and all-miss are readable apart in SQL: `blocks_dropped` vs `blocks_empty`.
    thin = await db.fetchrow(
        "SELECT blocks_dropped, blocks_empty FROM title_placement WHERE title_id = $1",
        THIN_TITLE,
    )
    assert "keyword" in thin["blocks_dropped"] and thin["blocks_empty"] == []

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
    """One title missing a column is enrichable; a block hitting nothing on every title is a
    key-grammar fault, so it is reported as its own line."""
    _store, report = await _sweep(db, grammar_bundle)
    assert report.blocks_never_hit == ["credit"]
    assert any("credit" in note for note in report.notes), report.notes


# Values, not just columns: the authority is the corpus exporter, `scripts/build_content.py`.


def test_both_dna_tiers_enter_the_vector_as_presence_not_as_strength():
    """The corpus built both DNA blocks without `weighted=True`: every cell is 1.0."""
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
    """An uncovered row (`covered=False`, emb near 1e-16) is not a row: the block drops. It is not
    thin for that, since no re-fetch can write the bundle's npz."""
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
    """The cut is `>= 0.5`: the smallest nonzero in the corpus's genome block is exactly 0.5."""
    await db.execute(
        "INSERT INTO ml_genome_score (ml_movie_id, tag_id, relevance)"
        " VALUES (110, 1, 0.49), (110, 2, 0.5)"
    )
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    contract = FeatureContract.from_store(store)
    keys = (await features.fetch_blocks(
        db, [FULL_TITLE], contract, vocab_version="v1"
    ))[FULL_TITLE]

    # The corpus predicate is `>=`: `g:dread` at exactly 0.5 stays.
    assert set(keys["genome"]) == {"g:cooking", "g:dread"}
    assert keys["genome"]["g:dread"] == pytest.approx(0.5)


async def test_a_mixed_case_keyword_lands_in_the_contracts_lowercase_column(
    db, spine, bundle_root
):
    """The corpus lower-cases and trims keywords; mixed-case ones used to miss their column."""
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
    """The corpus's predicate: the four above-the-line crafts, plus cast to third billing."""
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
    """The corpus writes `lang:` once from `title.original_language`; `title_language` lists several."""
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


async def test_cold_tower_placement_of_one_title_stays_under_one_second(db, placed):
    """Module load is excluded: per-process, not per-title. The bound is the spec's budget."""
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

    # A per-call module reload (~1 s) must fail this bound; a slow CI runner must not.
    contract = FeatureContract.from_store(store)
    cold = tower.load_tower(store, contract)
    x = np.zeros((1, contract.input_dim), dtype=np.float32)
    cold.place(x)
    began = time.perf_counter()
    for _ in range(20):
        cold.place(x)
    per_call = (time.perf_counter() - began) / 20
    assert per_call < 0.05, f"forward pass {per_call * 1000:.2f} ms/title"


@pytest.fixture
async def reimported(db, placed, tmp_path):
    """The new Backbone drops title 7's coordinate and the new contract renames `genre:comedy`."""
    await _seed_observations(db)
    second = _make_bundle(
        tmp_path / "bundle2", version="test-v2",
        # The shipped grammar, with `genre:comedy` renamed.
        names={"genre": ["genre:crime", "genre:thriller", "genre:family", "genre:romance",
                         "genre:sci-fi", "genre:drama", "genre:comedy-drama"]},
    )
    _uncover_backbone_row(second / "artifacts", title_id=7)
    # A models-only bundle: content seeds once (decision 162).
    (second / "content.sqlite").unlink()
    (second / "reviews.sqlite").unlink()
    # Re-state the inventory: the Backbone shrink and carve-outs happened after BUNDLE.json.
    fx.reinventory(second)
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(second), tmp_path / "artifacts"
    )
    assert report.ok, report.render()
    return ArtifactStore.open(tmp_path / "artifacts" / "test-v2", "test-v2")


async def _seed_observations(db) -> None:
    """Something has to exist for "observations survive re-import" to be falsifiable."""
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
    """Four steps in §10's order; the axis scatter is authored TSVs and must not be rebuilt."""
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

    assert await db.fetchval("SELECT placement FROM title WHERE id = 7") == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1 AND bundle_version = 'test-v2'",
        THIN_TITLE,
    ) == 1


async def test_a_reimport_rebuilds_vectors_from_the_staged_contract(db, reimported):
    """Title 8's rows did not change, but its `Comedy` column did, so its coordinate must."""
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
    """Whole rows, not counts: renumbered ids or a cleared `superseded_by` would keep the counts."""
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
    """The rebuild runs against the *staged* bundle before the flip; that exception is checked."""
    store, _ = placed
    fake = ArtifactStore.open(store.root, "never-imported")
    with pytest.raises(RuntimeError, match="staged"):
        await reconcile.run_rebuild(db, fake, "never-imported")


class _KilledBeforeTheBadge:
    """The badge UPDATE fails from the `after`-th chunk on, opening the window between upsert and
    badge; the chunk before it is the control."""

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
    """`award` is used for both halves on purpose: a rule keyed on the block NAME would pass the
    first assertion and fail the second."""
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

    # The genome is in the tuple even when imputed: the verdict must not depend on which list it hits.
    no_genome = features.build_vector(
        contract, 1, {k: v for k, v in complete.items() if k != "genome"}, text
    )
    assert no_genome.blocks_imputed == ("genome",) and no_genome.blocks_dropped == ()
    assert not no_genome.is_thin
    assert "genome" in features.UNENRICHABLE_BLOCKS


async def test_a_title_whose_only_gap_is_an_award_it_never_won_is_not_parked(
    db, spine, bundle_root
):
    """`THIN_TITLE` still parks in the same sweep, so this cannot pass as "park nothing"."""
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
    """`covered = False` is a property of the export; no stage-2 fetch can reach the bundle's npz."""
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
    """The run report names the narrowing, or a shrinking backlog reads as a library that improved."""
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

    # The job's reason names every gap, enrichable or not.
    job = await db.fetchrow("SELECT reason, detail FROM acquisition_job WHERE title_id = $1",
                            THIN_TITLE)
    assert "award" in job["reason"] and "review_text" in job["reason"], job["reason"]
    assert "award" in job["detail"]["blocks_dropped"]


async def test_each_sweep_chunk_is_one_transaction(db, spine, bundle_root, monkeypatch):
    """One title per chunk, so the first chunk is the control. Per-chunk atomicity, not
    all-or-nothing: one bad title must not cost the night."""
    monkeypatch.setattr(reconcile, "CHUNK", 1)
    store = ArtifactStore.open(bundle_root / "artifacts", "test-v1")
    conn = _KilledBeforeTheBadge(db, after=2)

    with pytest.raises(RuntimeError, match="killed"):
        await reconcile.reconcile(conn, store, scope="owned_missing")
    assert conn.badges == 2, "the second chunk has to have been reached, not the first"

    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", THIN_TITLE) == "cold_tower"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", THIN_TITLE
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM acquisition_job WHERE title_id = $1", THIN_TITLE
    ) == 1

    assert await db.fetchval("SELECT placement FROM title WHERE id = $1", FULL_TITLE) == "unplaced"
    assert await db.fetchval(
        "SELECT count(*) FROM title_placement WHERE title_id = $1", FULL_TITLE
    ) == 0, "the upsert outlived the badge, so this title has a coordinate and no placement"


async def test_a_title_with_a_placement_row_and_no_stamp_is_re_swept(db, spine, bundle_root):
    """The work list and §12's count must mean the same thing, or the title is stranded for ever."""
    _store, first = await _sweep(db, bundle_root)
    assert (first.considered, first.placed) == (2, 2), first.as_dict()

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
