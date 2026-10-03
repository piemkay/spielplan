"""The numpy half has a §5.3 budget; the database half asserts rule 5 on the response shape.
The fixture's top-scoring title is a series, as in the corpus's unpartitioned top-10."""

from __future__ import annotations

import io
import struct
import time
import zipfile
import zlib
from collections.abc import Sequence

import asyncpg
import numpy as np
import pytest

from spielplan.ledger import model, observations
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from spielplan.scoring import backbone as bb
from spielplan.scoring import foldin, serve
from tests.fixtures import make_bundle as fx

BUNDLE = "test-v1"

# fx.ITEM_SUPPORT restated, so a fixture change that moves the gate shows as a diff here.
SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}
MOVIES = (1, 2, 3, 4, 5, 8)
SERIES = (6, 7)

# Title 8 is cold-masked (no coordinate); title 5 has n_t = 6, the only row thin enough for
# the gate to weight the Cold Tower.
COLD_PLACEMENTS = {8: 0.41, 5: 0.69}


def cold_vector(title_id: int) -> np.ndarray:
    """PCG64 is platform-independent, so the same title yields the same vector everywhere."""
    v = np.random.default_rng(20260830 + 1000 + title_id).standard_normal(64)
    return v / np.linalg.norm(v)


@pytest.fixture(scope="session")
def store(tmp_path_factory) -> ArtifactStore:
    root = tmp_path_factory.mktemp("bundle")
    fx.make_bundle(root)
    return ArtifactStore.open(root / "artifacts", BUNDLE)


@pytest.fixture
def backbone(store) -> bb.Backbone:
    return bb.Backbone.open(store)


@pytest.fixture
async def world(db, backbone):
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )
    # The corpus's ten-column `title` row; Postgres carries seven, and `primary_title` is `name`.
    for title_id, kind, name, _orig, year, runtime, imdb, tmdb, _lang, _country in fx.TITLES:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, runtime_min, imdb_id, tmdb_id, is_owned) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,true)",
            title_id, kind, name, year, runtime, imdb, tmdb,
        )
    for title_id, b_hat in COLD_PLACEMENTS.items():
        await place(db, title_id, b_hat)

    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'admin') RETURNING id"
    )
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    # Below §0's five-label floor, so β stays 0 and the assertions are about the crowd half.
    for title_id, value in ((1, 0), (2, 2), (6, 2)):
        await db.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
            patrick, title_id, value,
        )
    report = await foldin.run(
        db, backbone, bundle_version=BUNDLE, only_stale=False, with_priors=True
    )
    return {"patrick": patrick, "jenny": jenny, "report": report, "backbone": backbone}


async def place(conn, title_id: int, b_hat: float, *, bundle: str = BUNDLE) -> None:
    await conn.execute(
        """
        INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                     tower_sha256, input_dim, blocks_present, blocks_dropped,
                                     blocks_imputed, nnz)
        VALUES ($1, $2, $3, $4, 'sha-contract', 'sha-tower', 131,
                ARRAY['genre'], ARRAY[]::text[], ARRAY[]::text[], 7)
        """,
        title_id, bundle, bb.pack_vec(cold_vector(title_id)), b_hat,
    )


def basis(rows: Sequence[tuple[int, int, float]], *, mu: float = 0.1) -> bb.Backbone:
    """Built in memory so both halves have KNOWN norms."""
    e = np.zeros((len(rows), 64))
    for i, (title_id, _n, norm) in enumerate(rows):
        direction = np.random.default_rng(777 + title_id).standard_normal(64)
        e[i] = direction / np.linalg.norm(direction) * norm
    return bb.Backbone(
        version="synthetic",
        title_ids=np.asarray([r[0] for r in rows], dtype=np.int64),
        E=e,
        b_i=np.zeros(len(rows)),
        item_n=np.asarray([r[1] for r in rows], dtype=np.int64),
        mu=mu,
        row_of={int(r[0]): i for i, r in enumerate(rows)},
    )


def cold_at(title_id: int, norm: float) -> np.ndarray:
    """Known norm, so the blend can be written by hand."""
    return cold_vector(title_id) * norm


def synth(n_titles: int, n_labels: int, *, seed: int, prior_signal: float = 0.0):
    """`prior_signal` = 1 makes the prior perfectly predictive and the embedding noise; 0 the reverse."""
    rng = np.random.default_rng(seed)
    e = rng.normal(size=(n_titles, 64)) / 8.0
    w = rng.normal(size=64) / 8.0
    taste = e @ w * 64.0
    b = prior_signal * taste + (1.0 - prior_signal) * (-taste)
    coords = {
        i: bb.Coordinate(title_id=i, e=e[i], b=float(b[i]), gate=0.9, item_n=90, e_source="backbone")
        for i in range(n_titles)
    }
    picked = rng.choice(n_titles, size=n_labels, replace=False)
    return coords, list(coords.values()), _steps(taste, picked)


def _steps(taste: np.ndarray, picked: np.ndarray) -> list[tuple[int, int]]:
    """§5.1's targets: each picked title's step on a seven-tier ladder, F = 0 to S = 6."""
    cuts = np.quantile(taste, np.arange(1, 7) / 7)
    return [(int(i), int(np.searchsorted(cuts, taste[i], side="right"))) for i in picked]


def test_the_gate_is_the_crowd_support_curve_and_a_missing_row_is_exactly_zero():
    """n_t = 0 makes "no Backbone row" and "score from the Cold Tower" one statement."""
    assert bb.EVIDENCE_K == 10.0
    assert bb.gate(0) == 0.0
    assert bb.gate(10) == pytest.approx(0.5)
    assert bb.gate(4218) == pytest.approx(4218 / 4228)
    assert bb.gate(6) == pytest.approx(0.375)
    # Monotone and never 1: no finite crowd fully retires the cold half.
    supports = [0, 1, 6, 30, 120, 900, 4218]
    gates = [bb.gate(n) for n in supports]
    assert gates == sorted(gates)
    assert max(gates) < 1.0


def test_the_backbone_loads_the_shipped_bundle_and_indexes_it_by_title_id(backbone):
    """§4.3 names no row alignment, so the loader requires the shipped `title_id` array. Title 8
    is a cold-masked row: the file has eight rows while the index answers for seven."""
    assert not backbone.is_empty
    assert backbone.title_ids.size == 8
    assert backbone.mu == pytest.approx(0.12, abs=1e-6)
    for title_id in (1, 2, 3, 4, 5, 6, 7):
        assert backbone.row(title_id) is not None
        assert backbone.support(title_id) == SUPPORT[title_id]
        assert backbone.embedding(title_id).shape == (64,)
    assert backbone.row(8) is None
    # `item_n` ships 900 for that row, but the gate's usable support is nothing (cs-01).
    assert backbone.support(8) == 0
    assert backbone.embedding(8) is None
    # The mask takes the coordinate, not the crowd prior (C1.2); `make_bundle` zeroes this b_i.
    assert backbone.raw_prior(8) == 0.0
    # `support()` is the gate's; `crowd_support()` is the crowd's, read by §6.1's P(seen) and
    # the badge payload via `title_prior.item_n`.
    assert backbone.crowd_support(8) == fx.COLD_BACKBONE_ROWS[8] == 900
    for title_id in (1, 2, 3, 4, 5, 6, 7):
        assert backbone.crowd_support(title_id) == SUPPORT[title_id], (
            "the two accessors have to stay one number for every row that has a coordinate"
        )
    assert backbone.crowd_support(9999) == 0, "a title with no row in the file has no count"


def test_a_backbone_whose_arrays_disagree_is_a_fault_and_not_a_silent_index(tmp_path):
    """A shorter b_i than E misaligns rows: a plausible number for the wrong film."""
    root = tmp_path / "artifacts"
    root.mkdir()

    def write(**arrays):
        np.savez(root / "backbone.npz", **arrays)
        return ArtifactStore.open(root, "broken")

    good = {
        # `title_ids`, plural — the name the corpus ships (M4.5).
        "title_ids": np.arange(1, 5, dtype=np.int32),
        # A real E: a zero row has no coordinate, so an all-zero fixture would index nothing.
        "E": np.random.default_rng(4).standard_normal((4, 64)).astype(np.float32),
        "b_i": np.zeros(4, dtype=np.float32),
        "item_n": np.zeros(4, dtype=np.int32),
        "mu": np.float32(0.1),
    }
    assert len(bb.Backbone.open(write(**good)).row_of) == 4
    assert bb.Backbone.open(write(**good)).title_ids.size == 4

    with pytest.raises(bb.BackboneError, match="aligned"):
        bb.Backbone.open(write(**{**good, "b_i": np.zeros(3, dtype=np.float32)}))
    with pytest.raises(bb.BackboneError, match="64-d|not \\(N"):
        bb.Backbone.open(write(**{**good, "E": np.zeros((4, 32), dtype=np.float32)}))
    with pytest.raises(bb.BackboneError, match="strictly increasing"):
        bb.Backbone.open(write(**{**good, "title_ids": np.array([3, 1, 2, 4], dtype=np.int32)}))
    with pytest.raises(bb.BackboneError, match="title_ids"):
        bb.Backbone.open(write(**{k: v for k, v in good.items() if k != "title_ids"}))


def test_a_truncated_backbone_npz_raises_backbone_error_rather_than_a_zip_error(tmp_path):
    """`app.py` catches `BackboneError` to keep a half-configured boot legal, so every corruption
    must surface as one: `BadZipFile`, `ValueError`, `EOFError` at zero bytes, and `zlib.error`
    inside a deflated member (the corpus's npz is compressed)."""
    root = tmp_path / "artifacts"
    root.mkdir()
    arrays = {
        "title_ids": np.arange(1, 5, dtype=np.int32),
        "E": np.random.default_rng(9).standard_normal((4, 64)).astype(np.float32),
        "b_i": np.zeros(4, dtype=np.float32),
        "item_n": np.array([500, 900, 4, 200], dtype=np.int32),
        "mu": np.float32(0.1),
        "cold_mask": np.zeros(4, dtype=bool),
    }
    path = root / "backbone.npz"
    np.savez(path, **arrays)
    whole = path.read_bytes()
    assert not bb.Backbone.open(ArtifactStore.open(root, "whole")).is_empty

    # Four depths; 0.0 is where every interrupted copy starts.
    for fraction in (0.0, 0.02, 0.5, 0.97):
        path.write_bytes(whole[: int(len(whole) * fraction)])
        assert ArtifactStore.open(root, f"cut-{fraction}").present["backbone.npz"], (
            "a zero-byte member is PRESENT, which is why `Backbone.open`'s early return misses it"
        )
        with pytest.raises(bb.BackboneError, match="could not be read"):
            bb.Backbone.open(ArtifactStore.open(root, f"cut-{fraction}"))

    # Not an archive at all — what a failed download leaves behind.
    path.write_bytes(b"<html>504 Gateway Time-out</html>")
    with pytest.raises(bb.BackboneError, match="could not be read"):
        bb.Backbone.open(ArtifactStore.open(root, "not-a-zip"))

    # A valid archive with one corrupt member, read after the shape checks have all passed.
    def member(array: np.ndarray) -> bytes:
        buffer = io.BytesIO()
        np.lib.format.write_array(buffer, np.asarray(array), allow_pickle=False)
        return buffer.getvalue()

    with zipfile.ZipFile(path, "w") as archive:
        for name, array in arrays.items():
            raw = member(array)
            archive.writestr(f"{name}.npy", raw[: len(raw) // 2] if name == "cold_mask" else raw)
    with pytest.raises(bb.BackboneError, match="cold_mask"):
        bb.Backbone.open(ArtifactStore.open(root, "half-a-mask"))

    # The same archive in the corpus's own compression mode, damaged in place rather than cut.
    np.savez_compressed(path, **arrays)
    with zipfile.ZipFile(path) as archive:
        assert {i.compress_type for i in archive.infolist()} == {zipfile.ZIP_DEFLATED}
        info = archive.getinfo("cold_mask.npy")
    raw = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack("<HH", raw[info.header_offset + 26:info.header_offset + 30])
    start = info.header_offset + 30 + name_len + extra_len
    raw[start:start + info.compress_size] = b"\xff" * info.compress_size
    path.write_bytes(bytes(raw))
    with pytest.raises(bb.BackboneError, match="cold_mask"):
        bb.Backbone.open(ArtifactStore.open(root, "deflate-damaged"))

    # The class `app.py`'s lifespan actually catches.
    assert issubclass(bb.BackboneError, RuntimeError)
    assert not issubclass(zlib.error, RuntimeError | OSError | ValueError)


def test_a_zeroed_backbone_row_is_not_a_warm_title(tmp_path):
    """The corpus writes E as zeros for `cold_mask` rows (the real coordinate is in `E_hat`). Two
    shapes, because the mask is a courtesy and not a contract."""
    root = tmp_path / "artifacts"
    root.mkdir()
    e = np.random.default_rng(20260906).standard_normal((4, 64)).astype(np.float32)
    e[1] = 0.0
    e[2] = 0.0
    arrays = {
        "title_ids": np.arange(1, 5, dtype=np.int32),
        "E": e,
        "b_i": np.array([0.4, 0.5, 0.6, 0.7], dtype=np.float32),
        # Rows 2 and 3 are zeroed; row 2 also has the support that excused it from the sweep.
        "item_n": np.array([500, 900, 4, 200], dtype=np.int32),
        "mu": np.float32(0.1),
        "cold_mask": np.array([False, True, True, False]),
    }
    np.savez(root / "backbone.npz", **arrays)

    store = ArtifactStore.open(root, "cold-v1")
    back = bb.Backbone.open(store)
    assert back.row(2) is None and back.row(3) is None
    assert back.row(1) == 0 and back.row(4) == 3
    assert back.support(2) == 0 and back.embedding(2) is None
    # The mask takes the coordinate and NOT the crowd prior: a flagged b_i is real (C1.2).
    assert back.raw_prior(2) == pytest.approx(0.5) and back.crowd_support(2) == 900
    # Reported, so an operator can see how much of the basis the corpus did not place.
    assert any("2 of 4 rows carry no coordinate" in note for note in back.notes)

    # A prior alone is not a coordinate: §12's M2 criterion must be able to count this.
    assert bb.coordinate(2, back) is None

    # With a placement, e(t) is the pure cold limit and b(t) blends the row's b_i with b̂.
    e_hat = cold_vector(2)
    c = bb.coordinate(2, back, (e_hat, 0.33))
    assert c.e_source == "cold_tower"
    assert c.gate == 0.0 and c.item_n == 0 and c.crowd_n == 900
    g = bb.gate(900)
    assert np.array_equal(c.e, e_hat) and c.b == pytest.approx(g * 0.5 + (1 - g) * 0.33)

    # Row 2 clears WARM_SUPPORT and would have been stamped warm on support alone.
    assert reconcile.warm_title_ids(store) == [1, 4]

    # No mask shipped: the norm decides. Flagged rows are ~1e-13, unflagged ones above 6e-5.
    del arrays["cold_mask"]
    np.savez(root / "backbone.npz", **arrays)
    bare = bb.Backbone.open(ArtifactStore.open(root, "cold-v2"))
    assert bare.row(2) is None and bare.row(3) is None
    assert bare.row(1) == 0 and bare.row(4) == 3
    assert reconcile.warm_title_ids(ArtifactStore.open(root, "cold-v2")) == [1, 4]


def test_a_title_with_no_backbone_row_scores_entirely_from_the_cold_tower(backbone):
    """Exactly, not approximately: the blend weight is exactly zero. Title 999 is in no row at all."""
    e_hat = cold_vector(999)
    c = bb.coordinate(999, backbone, (e_hat, 0.41))
    assert c.e_source == "cold_tower"
    assert c.gate == 0.0
    assert c.item_n == 0 and c.crowd_n == 0
    assert c.b == pytest.approx(0.41)
    assert np.array_equal(c.e, e_hat)

    masked = bb.coordinate(8, backbone, (cold_vector(8), 0.41))
    assert masked.e_source == "cold_tower" and masked.gate == 0.0
    assert np.array_equal(masked.e, cold_vector(8))
    g = bb.gate(fx.COLD_BACKBONE_ROWS[8])
    assert masked.b == pytest.approx(g * backbone.raw_prior(8) + (1 - g) * 0.41)


def test_a_thin_crowd_row_blends_both_halves_rather_than_choosing_between_them(backbone):
    """Title 5: n_t = 6, gate 0.375, so 62.5% of the coordinate is the Cold Tower's."""
    e_hat, b_hat = cold_vector(5), COLD_PLACEMENTS[5]
    c = bb.coordinate(5, backbone, (e_hat, b_hat))
    assert c.e_source == "blended"
    assert c.gate == pytest.approx(0.375)
    assert c.b == pytest.approx(0.375 * backbone.raw_prior(5) + 0.625 * b_hat)
    assert np.allclose(c.e, 0.375 * backbone.embedding(5) + 0.625 * e_hat)
    assert not np.allclose(c.e, backbone.embedding(5))
    assert not np.allclose(c.e, e_hat)


def test_a_warm_title_with_no_placement_keeps_its_row_and_its_shipped_prior(backbone):
    """b_i is already shrunk by the corpus; with no b̂ the gate is a no-op for b (C1.1)."""
    c = bb.coordinate(1, backbone, None)
    assert c.e_source == "backbone"
    assert np.array_equal(c.e, backbone.embedding(1).astype(np.float64))
    assert c.b == backbone.raw_prior(1)
    assert backbone.mu != 0.0, "a zero intercept could not tell the two readings apart"


def test_a_thin_title_cannot_outrank_a_warm_one_on_the_intercept():
    """b_i is centred on zero and μ is the rating intercept; shrinking toward μ lifted thin titles."""
    back = basis([(1, 5, 0.1), (2, 100_000, 0.5)], mu=0.68)
    back.b_i[:] = [0.0, 0.1]
    thin, warm = bb.coordinate(1, back), bb.coordinate(2, back)
    assert thin.b == 0.0
    assert warm.b == pytest.approx(0.1)
    assert thin.b < warm.b


def test_a_title_with_neither_a_row_nor_a_placement_has_no_coordinate(backbone):
    """No coordinate to rank on, so it must not be ranked on an invented number."""
    assert bb.coordinate(8, backbone, None) is None
    assert bb.coordinate(999, backbone, None) is None


def test_a_title_at_item_n_thirty_weights_its_two_halves_three_to_one(backbone):
    """30/40 = 0.75 exactly; both weights are representable, so the assertion is exact."""
    assert SUPPORT[4] == 30, "the fixture moved; this test is about the 3:1 gate"
    e_hat, b_hat = cold_vector(4), 0.55
    c = bb.coordinate(4, backbone, (e_hat, b_hat))

    assert c.e_source == "blended"
    assert c.gate == 0.75 and c.item_n == 30
    assert c.gate / (1.0 - c.gate) == 3.0, "three parts crowd to one part tower"

    e_row = backbone.embedding(4).astype(np.float64)
    assert np.array_equal(c.e, 0.75 * e_row + 0.25 * e_hat), (
        "e(t) is not exactly the 3:1 blend of the two halves"
    )
    assert c.b == pytest.approx(0.75 * backbone.raw_prior(4) + 0.25 * b_hat)


def test_exactly_ninety_crowd_ratings_still_falls_on_the_blend_side():
    """WARM_SUPPORT is computed (k*g/(1-g)), so it is 90 + 1 ulp, here and in
    `placement.warm_title_ids` alike."""
    assert bb.WARM_SUPPORT > 90 and bb.WARM_SUPPORT - 90.0 < 1e-9


def test_the_blend_expression_rescales_neither_half_while_the_scale_question_is_open():
    """Decision 236: no rescaling."""
    back = basis([(1, 30, 0.2)])
    e_hat = cold_at(1, 50.0)
    c = bb.coordinate(1, back, (e_hat, 0.0))

    assert np.array_equal(c.e, 0.75 * back.E[0] + 0.25 * e_hat)
    assert np.linalg.norm(back.E[0]) == pytest.approx(0.2)
    assert np.linalg.norm(e_hat) == pytest.approx(50.0)
    # Unscaled, so the cold quarter is ~83x the warm three-quarters; a normalised ê would hide it.
    assert np.linalg.norm(c.e) == pytest.approx(np.linalg.norm(0.25 * e_hat), rel=0.02)


def test_zero_labels_give_beta_zero_and_the_bare_crowd_prior():
    """No labels: β = 0, v = 0, μ = 0, so the score is the z-scored crowd prior for every member."""
    coords, reference, _ = synth(40, 0, seed=1)
    fit = foldin.fit_user([], coords, reference)

    assert fit.beta == 0.0
    assert fit.mu == 0.0
    assert fit.label_count == 0
    assert np.array_equal(fit.v, np.zeros(64))
    for c, (_t, s, cf) in zip(reference, foldin.score_many(fit, reference), strict=True):
        assert cf == 0.0
        assert s == pytest.approx((c.b - fit.prior_mean) / fit.prior_sd)
    other = foldin.fit_user([], coords, reference)
    assert foldin.score_many(fit, reference) == foldin.score_many(other, reference)


def test_fewer_than_five_labels_do_not_buy_a_blend_weight():
    """A β cross-validated on four points is noise, so below the floor the ordering stays the crowd's."""
    coords, reference, labels = synth(40, 4, seed=7)
    fit = foldin.fit_user(labels, coords, reference)
    assert fit.label_count == 4
    assert fit.beta == 0.0
    assert fit.lam == max(foldin.LAMBDA_GRID)   # the stiffest grid point, not a fitted one


def test_the_personal_half_wins_when_it_actually_predicts():
    """The counterpart: a β that is always 0 would pass every honesty test and personalise nothing."""
    coords, reference, labels = synth(400, 60, seed=11, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=5)
    assert fit.beta > 0.0
    assert fit.cv_rho > 0.0
    assert fit.cf_sd > 0.0

    ranked = sorted(foldin.score_many(fit, reference), key=lambda row: -row[1])
    crowd = sorted(reference, key=lambda c: -c.b)
    assert [t for t, _s, _cf in ranked[:10]] != [c.title_id for c in crowd[:10]]


def test_a_within_noise_floor_improvement_does_not_move_beta():
    """§0: a Spearman gain under 0.003-0.008 is a tie and must not buy personalisation."""
    coords, reference, labels = synth(200, 40, seed=3, prior_signal=1.0)
    fit = foldin.fit_user(labels, coords, reference, seed=2)
    assert fit.cv_rho > 0.9        # the prior alone is near-perfect here
    assert fit.beta == 0.0
    assert foldin.NOISE_FLOOR == 0.008


def test_beta_is_capped_at_the_measured_optimum_and_the_clamp_is_visible():
    """The grid searches to 1.0 on purpose; `beta_clamped` records the clamp."""
    coords, reference, labels = synth(400, 120, seed=17, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=4)
    assert fit.beta == pytest.approx(foldin.BETA_MAX)
    assert fit.beta_clamped is True
    assert max(foldin.BETA_GRID) == 1.0, "a grid stopping at 0.8 makes the clamp untestable"


def test_a_verdict_only_member_fits_as_the_three_class_target_did():
    """§5.1: a verdict's step is its class's middle tier (E, D, C), three evenly spaced levels, so
    the centred ridge fits the same v, λ and β as the retired -1/0/+1 target; only μ moves."""
    coords, reference, _ = synth(300, 60, seed=11)
    rng = np.random.default_rng(5)
    classes = [(int(t), int(c)) for t, c in zip(rng.choice(300, 60, replace=False),
                                                 rng.integers(0, 3, 60), strict=True)]
    steps = foldin.fit_user(
        [(t, model.class_step(c, 6)) for t, c in classes], coords, reference, seed=5
    )
    three = foldin.fit_user([(t, c - 1) for t, c in classes], coords, reference, seed=5)
    assert [model.class_step(c, 6) for c in (0, 1, 2)] == [0, 1, 2]
    assert np.allclose(steps.v, three.v)
    assert (steps.beta, steps.lam) == (three.beta, three.lam)
    assert steps.cv_rho == pytest.approx(three.cv_rho)
    assert steps.mu == pytest.approx(three.mu + 1.0)


def test_mu_shifts_every_score_and_reorders_nothing():
    """μ is added to every title of the kind, so it must not change an ordering."""
    coords, reference, labels = synth(120, 30, seed=23)
    fit = foldin.fit_user(labels, coords, reference, seed=1)
    shifted = foldin.Fit(**{**{f: getattr(fit, f) for f in fit.__dataclass_fields__},
                            "mu": fit.mu + 1.75})

    base = np.array([s for _t, s, _cf in foldin.score_many(fit, reference)])
    moved = np.array([s for _t, s, _cf in foldin.score_many(shifted, reference)])
    assert np.allclose(moved - base, 1.75)
    assert list(np.argsort(-base)) == list(np.argsort(-moved))


def test_the_fit_is_reproducible_from_its_inputs():
    """A function of (labels, coordinates, seed) only: no clock, no row order."""
    coords, reference, labels = synth(150, 40, seed=31)
    a = foldin.fit_user(labels, coords, reference, seed=9)
    b = foldin.fit_user(list(reversed(labels)), coords, reference, seed=9)
    assert np.allclose(a.v, b.v)
    assert (a.beta, a.lam, a.mu) == (b.beta, b.lam, b.mu)
    assert a.cv_rho == pytest.approx(b.cv_rho)


def test_a_fitted_vector_survives_the_round_trip_through_the_bytea_convention():
    """Both columns are "64 x float32 LE": one pair of functions, one convention."""
    coords, reference, labels = synth(120, 30, seed=41)
    fit = foldin.fit_user(labels, coords, reference, seed=3)
    raw = bb.pack_vec(fit.v)
    assert len(raw) == 256
    assert np.allclose(bb.unpack_vec(raw), fit.v, atol=1e-6)
    with pytest.raises(ValueError):
        bb.unpack_vec(raw[:128])


def test_a_full_fold_in_refit_stays_inside_its_budget():
    """Measured at ~7 ms; asserted at the spec's 1 s, so only an algorithmic regression fails."""
    coords, reference, labels = synth(839, 100, seed=53)
    started = time.perf_counter()
    fit = foldin.fit_user(labels, coords, reference, seed=1)
    rows = foldin.score_many(fit, reference)
    elapsed = time.perf_counter() - started

    assert len(rows) == 839
    assert elapsed < 1.0, f"fold-in + scoring took {elapsed * 1000:.0f} ms"


def _rescaled(coords: dict[int, bb.Coordinate], factors: np.ndarray) -> dict[int, bb.Coordinate]:
    return {
        t: bb.Coordinate(title_id=c.title_id, e=c.e * float(f), b=c.b, gate=c.gate,
                         item_n=c.item_n, e_source=c.e_source, crowd_n=c.crowd_n)
        for (t, c), f in zip(coords.items(), factors, strict=True)
    }


def test_the_fit_and_its_scores_do_not_move_when_a_rows_norm_does():
    """Decision 469: a row's norm is crowd support or tower scale, so any positive per-title rescale
    must leave λ, β, ρ, v and every score unchanged."""
    coords, reference, labels = synth(300, 60, seed=19)
    factors = 10.0 ** np.random.default_rng(3).uniform(-3, 3, size=len(coords))
    scaled = _rescaled(coords, factors)

    a = foldin.fit_user(labels, coords, reference, seed=4)
    b = foldin.fit_user(labels, scaled, list(scaled.values()), seed=4)
    assert (a.lam, a.beta) == (b.lam, b.beta) and a.beta > 0.0
    assert a.cv_rho == pytest.approx(b.cv_rho, abs=1e-9)
    assert np.allclose(a.v, b.v, atol=1e-9)
    ranked_a = foldin.score_many(a, reference)
    ranked_b = foldin.score_many(b, list(scaled.values()))
    assert np.allclose([s for _, s, _ in ranked_a], [s for _, s, _ in ranked_b], atol=1e-9)
    assert np.allclose([cf for _, _, cf in ranked_a], [cf for _, _, cf in ranked_b], atol=1e-9)


def test_on_a_support_weighted_basis_the_personal_top_is_taste_and_not_popularity():
    """Norms grow with support (0.002·n^0.6); read raw, both tops are the popular fifth. Read as
    directions, the two tops share nothing."""
    rng = np.random.default_rng(469)
    n = 500
    support = np.round(10.0 ** rng.uniform(np.log10(5), 5, size=n)).astype(int)
    direction = rng.standard_normal((n, 64))
    direction /= np.linalg.norm(direction, axis=1, keepdims=True)
    w = rng.standard_normal(64)
    w /= np.linalg.norm(w)
    taste = direction @ w
    coords = {
        i: bb.Coordinate(title_id=i, e=direction[i] * 0.002 * support[i] ** 0.6, b=0.0,
                         gate=bb.gate(int(support[i])), item_n=int(support[i]),
                         e_source="backbone", crowd_n=int(support[i]))
        for i in range(n)
    }
    reference = list(coords.values())
    picked = rng.choice(n, size=60, replace=False)
    rated = {int(i) for i in picked}
    popular = support >= np.quantile(support, 0.8)
    raw = np.asarray([c.e for c in reference])

    tops = {}
    for sign in (1.0, -1.0):
        liking = sign * taste
        labels = _steps(liking, picked)
        fit = foldin.fit_user(labels, coords, reference, seed=11)
        order = sorted(foldin.score_many(fit, reference), key=lambda row: -row[2])
        chosen = [t for t, _, _ in order if t not in rated][:20]
        liked = sum(bool(liking[t] > 0) for t in chosen)
        from_popular = sum(bool(popular[t]) for t in chosen)
        assert liked >= 17, f"only {liked} of the personal top 20 match the taste"
        assert from_popular <= 10, (
            f"{from_popular} of the personal top 20 are the most popular fifth: the half is "
            "ranking by norm"
        )
        tops[sign] = set(chosen)

        # Anti-vacuity: the raw-row ridge's top twenty is the popular fifth, so the assertions can fail.
        y = np.asarray([step for _, step in labels], dtype=float)
        v_raw = foldin.fold_in(raw[[t for t, _ in labels]], y - y.mean(), 1.0)
        raw_top = [int(t) for t in np.argsort(-(raw @ v_raw)) if int(t) not in rated][:20]
        assert sum(bool(popular[t]) for t in raw_top) >= 15
    assert not tops[1.0] & tops[-1.0]


def test_a_coordinates_direction_speaks_at_the_evidence_behind_it():
    """Normalising drops a thin row's shrinkage; the gate puts it back. Cold Tower rows speak at full
    voice; a zero row says nothing."""
    e = np.random.default_rng(5).standard_normal(64) * 37.0
    unit = e / np.linalg.norm(e)

    def coordinate(source: str, gate: float, vector=e) -> bb.Coordinate:
        return bb.Coordinate(title_id=1, e=vector, b=0.0, gate=gate, item_n=5, e_source=source)

    thin = bb.direction(coordinate("backbone", bb.gate(5)))
    assert np.allclose(thin, unit * bb.gate(5)) and np.linalg.norm(thin) == pytest.approx(1 / 3)
    for source in ("blended", "cold_tower"):
        assert np.allclose(bb.direction(coordinate(source, 0.375)), unit)
    assert np.array_equal(bb.direction(coordinate("backbone", 0.9, np.zeros(64))), np.zeros(64))
    matrix = bb.directions([coordinate("backbone", 0.5), coordinate("cold_tower", 0.0)])
    assert matrix.shape == (2, 64)
    assert np.allclose(np.linalg.norm(matrix, axis=1), [0.5, 1.0])
    assert bb.directions([]).shape == (0, 64)


async def test_priors_name_every_e_source_the_spec_defines(db, world):
    """`title_prior.e_source` records §5.1's branch so readers need not re-derive it."""
    rows = {r["title_id"]: dict(r) for r in await db.fetch("SELECT * FROM title_prior")}
    assert len(rows) == len(fx.TITLES)

    assert rows[1]["e_source"] == "backbone"
    assert rows[8]["e_source"] == "cold_tower"
    assert rows[5]["e_source"] == "blended"      # §5.1's gate branch, reachable and reached
    assert rows[8]["gate"] == 0.0
    # A flagged row keeps its crowd prior (C1.2), blended with b̂ = 0.41 at the 900-rating gate.
    assert rows[8]["b_i"] == 0.0
    g = bb.gate(900)
    assert rows[8]["b"] == pytest.approx(g * 0.0 + (1 - g) * 0.41, abs=1e-5)
    assert rows[1]["item_n"] == SUPPORT[1]
    assert world["report"].priors.by_source == {"backbone": 6, "blended": 1, "cold_tower": 1}


async def test_every_owned_title_has_a_coordinate_or_the_report_names_it(db, world, backbone):
    """A title with neither row nor placement keeps b NULL, is unranked, and is NAMED."""
    assert world["report"].priors.uncoordinated_owned == []

    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    report = await serve.materialise_priors(db, backbone, bundle_version=BUNDLE)
    assert report.uncoordinated_owned == [99]
    assert await db.fetchval("SELECT b FROM title_prior WHERE title_id = 99") is None

    await place(db, 99, 0.5)
    report = await serve.materialise_priors(db, backbone, bundle_version=BUNDLE)
    assert report.uncoordinated_owned == []
    assert await db.fetchval("SELECT e_source FROM title_prior WHERE title_id = 99") == "cold_tower"


async def test_an_uncoordinated_title_is_absent_from_the_ranked_list_rather_than_ranked_at_zero(
    db, world, backbone
):
    """Ranking it on a default puts an unscoreable title mid-list, looking like a judgement."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    await foldin.run(db, backbone, bundle_version=BUNDLE, only_stale=False, with_priors=True)

    top = await serve.top_scored(
        db, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE, limit=50
    )
    assert 99 not in [item["id"] for item in top["items"]]


async def test_each_kind_is_ranked_on_its_own_and_never_in_one_merged_ordering(db, world):
    """The library's top title is a series, so the films list must lead with the top FILM, and a
    limit of 3 is the top three FILMS, not the films among the top three."""
    top = {
        kind: await serve.top_scored(
            db, user_id=world["patrick"], kind=kind, bundle_version=BUNDLE, limit=50
        )
        for kind in ("movie", "series")
    }
    assert {i["id"] for i in top["movie"]["items"]} == set(MOVIES)
    assert {i["id"] for i in top["series"]["items"]} == set(SERIES)
    for kind, section in top.items():
        assert all(item["kind"] == kind for item in section["items"])
        scores = [item["score"] for item in section["items"]]
        assert scores == sorted(scores, reverse=True), "each kind is ordered within itself"

    rows = await db.fetch(
        "SELECT title_id, kind FROM user_score WHERE user_id = $1 ORDER BY score DESC, title_id",
        world["patrick"],
    )
    # Asserted, not assumed: the library's top-scoring title IS a series.
    assert rows[0]["kind"] == "series"
    films = await serve.top_scored(
        db, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE, limit=3
    )
    films = [i["id"] for i in films["items"]]
    assert films == [r["title_id"] for r in rows if r["kind"] == "movie"][:3]
    assert films != [r["title_id"] for r in rows[:3] if r["title_id"] in MOVIES]


async def test_only_owned_titles_reach_the_top_shelf(db, world):
    await db.execute("UPDATE title SET is_owned = false WHERE id = 4")
    top = await serve.top_scored(
        db, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE, limit=50
    )
    assert {i["id"] for i in top["items"]} == set(MOVIES) - {4}


async def test_a_read_bound_to_another_basis_returns_nothing_rather_than_old_numbers(db, world):
    """A dropped guard fails silently, so the assertion is ABSENCE."""
    for kind in ("movie", "series"):
        top = await serve.top_scored(
            db, user_id=world["patrick"], kind=kind, bundle_version="test-v2", limit=50
        )
        assert top["items"] == []


async def test_a_zero_label_member_is_still_fitted_and_still_ranked(db, world):
    """"Fitted to zero labels" and "never fitted" differ, so the row is written."""
    row = await serve.fit_row(db, user_id=world["jenny"], kind="movie")
    assert row is not None
    assert row["label_count"] == 0
    assert row["blend_beta"] == 0.0
    assert row["bundle_version"] == BUNDLE

    top = await serve.top_scored(
        db, user_id=world["jenny"], kind="movie", bundle_version=BUNDLE, limit=50
    )
    assert top["fitted"] is True
    assert top["personalised"] is False
    assert top["label_count"] == 0
    assert len(top["items"]) == len(MOVIES)


async def test_a_clamped_blend_weight_is_storable_and_anything_above_it_is_not(db, world):
    """`blend_beta` is `real`, and float4(0.8) > 0.8 as float8, so the CHECK casts its literal."""
    assert await db.fetchval("SELECT 0.8::real <= 0.8::real") is True, "the ceiling must admit β"
    assert await db.fetchval("SELECT 0.8::real <= 0.8") is False, (
        "float4(0.8) still widens above the numeric literal — the cast in 0009 is load-bearing"
    )

    coords, reference, labels = synth(400, 120, seed=17, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=4)
    assert fit.beta == pytest.approx(foldin.BETA_MAX) and fit.beta_clamped

    # `updated_at` is the caller's (the moment labels were read), so it is read as `refit_user` does.
    await foldin.write_fit(
        db, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE, fit=fit,
        updated_at=await db.fetchval("SELECT clock_timestamp()"),
    )
    stored = await db.fetchval(
        "SELECT blend_beta FROM user_vector WHERE user_id = $1 AND kind = 'movie'",
        world["patrick"],
    )
    assert stored == pytest.approx(foldin.BETA_MAX, abs=1e-6)
    assert f"β {stored:.2f}" == "β 0.80"
    assert stored == pytest.approx(0.8, abs=1e-7), "stored as the measured optimum, not below it"

    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE user_vector SET blend_beta = 0.9 WHERE user_id = $1 AND kind = 'movie'",
            world["patrick"],
        )


async def test_a_refit_rewrites_the_scores_and_leaves_the_observations_alone(db, world, backbone):
    """A refit replaces scores: a title that lost its coordinate loses its score."""
    before = await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", world["patrick"])
    stamps = await db.fetch(
        "SELECT title_id, computed_at FROM user_score WHERE user_id = $1", world["patrick"]
    )

    for title_id, value in ((3, 2), (4, 0)):
        await db.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
            world["patrick"], title_id, value,
        )
    # The tick is debounced; shifting both stamps back by the pause keeps their ORDER.
    shift = foldin.PAUSE_SECONDS + 10
    await db.execute(
        "UPDATE verdict SET created_at = created_at - ($2::int * interval '1 second') "
        " WHERE user_id = $1",
        world["patrick"], shift,
    )
    await db.execute(
        "UPDATE user_vector SET updated_at = updated_at - ($2::int * interval '1 second') "
        " WHERE user_id = $1",
        world["patrick"], shift,
    )
    report = await foldin.run(db, backbone, bundle_version=BUNDLE, only_stale=True)

    assert (world["patrick"], "movie") in report.refit
    assert (world["jenny"], "movie") not in report.refit      # nothing moved for her
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1", world["patrick"]
    ) == before + 2
    assert await db.fetchval(
        "SELECT label_count FROM user_vector WHERE user_id = $1 AND kind = 'movie'",
        world["patrick"],
    ) == 4
    after = await db.fetch(
        "SELECT title_id, computed_at FROM user_score WHERE user_id = $1", world["patrick"]
    )
    assert {r["title_id"] for r in after} == {r["title_id"] for r in stamps}


async def test_a_verdict_on_a_title_with_no_coordinate_is_dropped_and_counted(db, world, backbone):
    """Dropped from the fit but counted, or `label_count` and the widget disagree."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 99, 2)", world["patrick"]
    )
    fit = await foldin.refit_user(
        db, backbone, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE
    )
    assert fit.label_count == 3      # the person rated three films of this kind …
    assert fit.used == 2             # … and two of them could reach the model
    assert fit.dropped == 1


async def test_the_model_line_prints_the_real_b_beta_and_gate(db, world):
    """The card prints the number the ranking uses. σ is an em dash before a fit, never 0.00."""
    prior = await db.fetchrow("SELECT b, gate, item_n FROM title_prior WHERE title_id = 3")
    line = await serve.model_line(db, user_id=world["patrick"], title_id=3, bundle_version=BUNDLE)

    assert line["available"] is True
    assert line["b"] == pytest.approx(prior["b"], abs=1e-6)
    assert line["gate"] == pytest.approx(prior["gate"], abs=1e-6)
    assert line["item_n"] == SUPPORT[3]
    assert line["e_source"] == "backbone"
    assert line["text"] == (
        f"b(t) {prior['b']:.2f} · β 0.00 · gate {prior['gate']:.2f}"
    )
    assert line["second_line"] == f"σ — · support n={SUPPORT[3]}"

    await db.execute(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma) "
        "VALUES ($1, 3, 'movie', 0.4, 0.09)",
        world["patrick"],
    )
    line = await serve.model_line(db, user_id=world["patrick"], title_id=3, bundle_version=BUNDLE)
    assert line["second_line"] == f"σ ±0.09 · support n={SUPPORT[3]}"


async def test_the_model_line_says_so_rather_than_inventing_a_number(db, world, backbone):
    """§3.1: no title, no prior in the active basis, no coordinate: each names itself."""
    assert (await serve.model_line(
        db, user_id=world["patrick"], title_id=4242, bundle_version=BUNDLE
    ))["available"] is False

    stale = await serve.model_line(
        db, user_id=world["patrick"], title_id=1, bundle_version="test-v2"
    )
    assert stale["available"] is False
    assert "active bundle" in stale["reason"]

    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    await serve.materialise_priors(db, backbone, bundle_version=BUNDLE)
    unplaced = await serve.model_line(
        db, user_id=world["patrick"], title_id=99, bundle_version=BUNDLE
    )
    assert unplaced["available"] is False
    assert unplaced["reason"] == "no Backbone row and no Cold Tower placement"


async def test_the_gate_on_the_card_is_a_crowd_number_and_not_a_per_viewer_one(db, world):
    """n_t is a crowd count, so the gate is the same for every viewer."""
    patrick = await serve.model_line(
        db, user_id=world["patrick"], title_id=1, bundle_version=BUNDLE
    )
    jenny = await serve.model_line(db, user_id=world["jenny"], title_id=1, bundle_version=BUNDLE)
    assert patrick["gate"] == jenny["gate"] == pytest.approx(bb.gate(SUPPORT[1]))
    assert patrick["b"] == jenny["b"]


async def test_the_target_is_the_placed_step_else_the_middle_of_the_verdicts_class(db, world):
    """§5.1 (decision 536): a placement's tier, E = 0 to S = 5, wins over its verdict; a title with a
    verdict alone is fitted at E, D or C. Each answered title is one target."""
    patrick = world["patrick"]
    for title_id, value in ((1, 2), (2, 1), (3, 0)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)
    await observations.record_tier_edit(db, user_id=patrick, title_id=3, tier=5)
    await observations.record_tier_edit(db, user_id=patrick, title_id=4, tier=0)

    targets = dict(await foldin.live_labels(db, user_id=patrick, kind="movie"))
    assert targets == {1: 2, 2: 1, 3: 5, 4: 0}


async def test_a_silent_reask_does_not_erase_the_label_it_re_asked(db, world):
    """`record_verdict` supersedes the previous row for a re-ask too, so `superseded_by IS NULL AND
    NOT is_reask` matched neither row. The label count must not move."""
    patrick = world["patrick"]
    for title_id, value in ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1)):
        await observations.record_verdict(db, user_id=patrick, title_id=title_id, value=value)

    before = await foldin.live_labels(db, user_id=patrick, kind="movie")
    assert len(before) == 5
    rows_before = await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = 1", patrick
    )

    original = await db.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", patrick
    )
    await observations.record_verdict(
        db, user_id=patrick, title_id=1, value=2, is_reask=True, reask_of=original
    )

    after = await foldin.live_labels(db, user_id=patrick, kind="movie")
    assert len(after) == 5, f"the re-ask erased a label: {sorted(before)} -> {sorted(after)}"
    assert dict(after)[1] == model.class_step(2, 6), (
        "and the erased title keeps the answer the person actually gave"
    )
    assert sorted(after) == sorted(before), "a same-answer re-ask must change nothing at all"

    # The append-only history is intact; the fit reads one of the rows.
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = 1", patrick
    ) == rows_before + 1
