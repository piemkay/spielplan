"""The §5.1 serving stack. Spec v2.1 §5.1, §4.1 rule 5 (decision 18), §4.3, §5.3, §6.0, §10, §12.

Two halves, and the split is deliberate.

The first half is numpy only: §5.1's gate, the four `e_source` branches, the fold-in and the
blend weight. §5.3 puts a budget on that code and a budget measured through Postgres measures
Postgres.

The second half needs a real database, because §4.1 rule 5 is a claim about a *response shape*.
"Ranking partitions by kind" cannot be asserted against a function that returns numbers; it is
asserted against two sections that a client physically cannot merge. The landmine is measured:
the unpartitioned crowd top-10 is 8/10 TV series, and this fixture reproduces it in miniature —
the single highest-scoring title in the whole library is a series, so a concatenated
implementation surfaces a different set of films and every partition test below fails.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import inspect
import io
import struct
import time
import zipfile
import zlib
from collections.abc import Sequence

import asyncpg
import numpy as np
import pytest

from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from spielplan.scoring import backbone as bb
from spielplan.scoring import foldin, serve
from tests.fixtures import make_bundle as fx

BUNDLE = "test-v1"

# fx.ITEM_SUPPORT, restated so a fixture change that moves the gate is visible as a diff here
# rather than as three tests going quietly green on different numbers.
SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}
MOVIES = (1, 2, 3, 4, 5, 8)
SERIES = (6, 7)

# The two coordinates the app computes rather than imports (§5.3 placement reconciliation).
# Title 8's row carries no coordinate — `cold_mask` flags it, so the basis excludes it (cs-01) —
# and title 5 has one with n_t = 6, which is the only row thin enough for the gate to leave real
# weight on the Cold Tower.
COLD_PLACEMENTS = {8: 0.41, 5: 0.69}


def cold_vector(title_id: int) -> np.ndarray:
    """A deterministic stand-in for the Cold Tower's ê(t). PCG64 is platform-independent, so
    the same title yields the same vector on every machine."""
    v = np.random.default_rng(20260830 + 1000 + title_id).standard_normal(64)
    return v / np.linalg.norm(v)


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def store(tmp_path_factory) -> ArtifactStore:
    """The shipped fixture bundle, built once: §4.3's real backbone.npz, not a stand-in."""
    root = tmp_path_factory.mktemp("bundle")
    fx.make_bundle(root)
    return ArtifactStore.open(root / "artifacts", BUNDLE)


@pytest.fixture
def backbone(store) -> bb.Backbone:
    return bb.Backbone.open(store)


@pytest.fixture
async def world(db, backbone):
    """A household with a library, a basis, two cold placements and one rating sitting."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )
    # The fixture's spine is now the corpus's ten-column `title` row (§4.1); this surface only
    # needs the seven Postgres `title` carries, and `primary_title` is what Spielplan calls
    # `name`.
    for title_id, kind, name, _orig, year, runtime, imdb, tmdb, _lang, _country in fx.TITLES:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, runtime_min, imdb_id, tmdb_id, is_owned) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,true)",
            title_id, kind, name, year, runtime, imdb, tmdb,
        )
    for title_id, b_hat in COLD_PLACEMENTS.items():
        await place(db, title_id, b_hat)

    # §6.0: "credits, each person tappable → filters the library to their filmography". One
    # person credited across the partition is what decision 18's rule is actually about.
    await db.execute("INSERT INTO person (id, name) VALUES (100, 'Ada Cross-Kind')")
    for title_id in (2, 6):
        await db.execute(
            "INSERT INTO credit (title_id, person_id, department, job, source) "
            "VALUES ($1, 100, 'Directing', 'Director', 'tmdb')",
            title_id,
        )

    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'admin') RETURNING id"
    )
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    # Three verdicts: below §0's five-label floor, so β stays 0 and every ranked assertion below
    # is about the crowd half alone — which is the half the partition landmine lives in.
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
    """Write what §5.3's placement reconciliation writes: a title_placement row in this basis."""
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
    """A Backbone built in memory from (title_id, item_n, ||E||) triples.

    Constructed rather than loaded because the point of the blend measurement is that the two
    halves have KNOWN norms: `make_bundle`'s E is realistic and therefore unusable as a ruler.
    `row_of` is filled for every row on purpose — including a zero-norm one, which `Backbone.open`
    would have excluded (cs-01, the cold mask) — because `blend_ratios` takes whatever Backbone it
    is handed and must report a pair it cannot divide rather than raise inside a report.
    """
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
    """A stand-in ê of known norm, so the ratio the report prints can be written out by hand."""
    return cold_vector(title_id) * norm


def synth(n_titles: int, n_labels: int, *, seed: int, prior_signal: float = 0.0):
    """A synthetic (coords, reference, labels) triple with a controllable signal split.

    `prior_signal` = 1 makes the crowd prior perfectly predictive and the embedding pure noise;
    0 makes the embedding perfectly predictive and the prior anti-correlated. Those are the two
    ends β has to be able to reach.
    """
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
    cuts = np.quantile(taste, [1 / 3, 2 / 3])
    labels = [(int(i), int(np.searchsorted(cuts, taste[i], side="right"))) for i in picked]
    return coords, list(coords.values()), labels


# ==============================================================================================
# §5.1's arithmetic — numpy only
# ==============================================================================================


def test_the_gate_is_the_crowd_support_curve_and_a_missing_row_is_exactly_zero():
    """§5.1: `gate = n_t / (n_t + k)`, evidence gating, k ≈ 10.

    The n_t = 0 case is the load-bearing one: it is what makes "this title has no Backbone row"
    and "score it entirely from the Cold Tower" the same statement instead of two that have to
    be kept in agreement.
    """
    assert bb.EVIDENCE_K == 10.0
    assert bb.gate(0) == 0.0
    assert bb.gate(10) == pytest.approx(0.5)
    assert bb.gate(4218) == pytest.approx(4218 / 4228)
    assert bb.gate(6) == pytest.approx(0.375)
    # Monotone and never 1: no finite crowd ever fully retires the cold half.
    supports = [0, 1, 6, 30, 120, 900, 4218]
    gates = [bb.gate(n) for n in supports]
    assert gates == sorted(gates)
    assert max(gates) < 1.0


def test_the_backbone_loads_the_shipped_bundle_and_indexes_it_by_title_id(backbone):
    """§4.3 ships "E, E_full, b_i, μ, plus the per-title support counts item_n".

    The id mapping is NOT in §4.3 — it names no alignment between a row of E and a row of
    `title` — so the loader requires the `title_id` array the fixture ships and says so. Title 8
    has no coordinate on purpose: that is normal (§8 stage 10), not an error. Since M4.13 it is
    the corpus's own way of having none — a row in the file that `cold_mask` flags (cs-01) —
    rather than an absent row, so `n_rows` counts eight while the index answers for seven, and
    the three accessors below say the same thing about it either way.
    """
    assert not backbone.is_empty
    assert backbone.n_rows == 8
    assert backbone.mu == pytest.approx(0.12, abs=1e-6)
    for title_id in (1, 2, 3, 4, 5, 6, 7):
        assert backbone.row(title_id) is not None
        assert backbone.support(title_id) == SUPPORT[title_id]
        assert backbone.embedding(title_id).shape == (64,)
    assert backbone.row(8) is None
    # Not `SUPPORT[8]` read off the file: `item_n` ships 900 for that row (fx.COLD_BACKBONE_ROWS)
    # and the support the app can use is nothing, which is the pair cs-01 is about.
    assert backbone.support(8) == 0
    assert backbone.embedding(8) is None
    assert backbone.raw_prior(8) is None
    # And the OTHER accessor, which is the half cs-01 took with it by accident. §4.3 ships
    # `item_n` as "the per-title support counts" and glosses it "(the §5.1 gate input)"; those are
    # one number only while every row carries a coordinate, and excluding the flagged rows is
    # exactly what stopped them being one. `support()` is the gate's, `crowd_support()` is the
    # crowd's, and the crowd's is what §6.1's P(seen) and §8 stage 10's badge payload read out of
    # `title_prior.item_n`. [M4.13 cycle 2, M413-C2-DIM5-01]
    assert backbone.crowd_support(8) == fx.COLD_BACKBONE_ROWS[8] == 900
    for title_id in (1, 2, 3, 4, 5, 6, 7):
        assert backbone.crowd_support(title_id) == SUPPORT[title_id], (
            "the two accessors have to stay one number for every row that has a coordinate"
        )
    assert backbone.crowd_support(9999) == 0, "a title with no row in the file has no count"


def test_a_backbone_whose_arrays_disagree_is_a_fault_and_not_a_silent_index(tmp_path):
    """A shorter b_i than E is a row-for-row misalignment, which produces a plausible number
    for the wrong film. §4.3's arrays are aligned or the file cannot be joined to anything."""
    root = tmp_path / "artifacts"
    root.mkdir()

    def write(**arrays):
        np.savez(root / "backbone.npz", **arrays)
        return ArtifactStore.open(root, "broken")

    good = {
        # `title_ids`, plural — the name the corpus ships (M4.5).
        "title_ids": np.arange(1, 5, dtype=np.int32),
        # A real E, not zeros: a zero row is a row with no coordinate (see
        # `test_a_zeroed_backbone_row_is_not_a_warm_title`), so an all-zero fixture would load
        # a Backbone that indexes nothing while this test claims four rows.
        "E": np.random.default_rng(4).standard_normal((4, 64)).astype(np.float32),
        "b_i": np.zeros(4, dtype=np.float32),
        "item_n": np.zeros(4, dtype=np.int32),
        "mu": np.float32(0.1),
    }
    assert len(bb.Backbone.open(write(**good)).row_of) == 4
    assert bb.Backbone.open(write(**good)).n_rows == 4

    with pytest.raises(bb.BackboneError, match="aligned"):
        bb.Backbone.open(write(**{**good, "b_i": np.zeros(3, dtype=np.float32)}))
    with pytest.raises(bb.BackboneError, match="64-d|not \\(N"):
        bb.Backbone.open(write(**{**good, "E": np.zeros((4, 32), dtype=np.float32)}))
    with pytest.raises(bb.BackboneError, match="strictly increasing"):
        bb.Backbone.open(write(**{**good, "title_ids": np.array([3, 1, 2, 4], dtype=np.int32)}))
    with pytest.raises(bb.BackboneError, match="title_ids"):
        bb.Backbone.open(write(**{k: v for k, v in good.items() if k != "title_ids"}))


def test_a_truncated_backbone_npz_raises_backbone_error_rather_than_a_zip_error(tmp_path):
    """§3.1 keeps a half-configured boot legal, and `app.py:256-260` is how: it catches
    `BackboneError`, logs it, and serves with `Backbone.empty()` so the admin can reach the Data
    tab and re-import. That guard caught nothing for the most likely corruption there is.

    `np.load` raises `zipfile.BadZipFile` for a truncated or half-copied archive and `ValueError`
    for a file that is not an npz at all — neither is a `RuntimeError`, so an interrupted copy into
    `/data/artifacts` took the lifespan down and left no surface to fix the bundle from. Measured
    before the fix: a file cut at 50%, 97% and 2% all raised `BadZipFile` past the guard.

    The third case is the one a single `try` around the open would still have missed: `NpzFile`
    re-reads the member on every subscript, so a valid archive with one corrupt member fails at
    whichever read touches it — here `cold_mask`, which is read after every shape check has passed.

    ZERO BYTES IS ITS OWN DEPTH, and it is the depth a ladder of fractions cannot reach. `docker
    cp`, `scp` and a restore all create the destination and truncate it before writing, so an
    interrupted copy spends its first instant at exactly 0 — and `np.load` answers that one file
    length with `EOFError` from its own empty-magic check, not `BadZipFile`. It is not an
    `OSError`, a `ValueError` or a `RuntimeError`, so it went past `_reading` and past `app.py`'s
    lifespan guard alike; 2% of this fixture is ~77 bytes, which is already a `BadZipFile`.
    `ArtifactStore.open` does not save it either: `present` is `.exists()`, and an empty file
    exists. [M4.13 cycle 1, M413-R1-HP-02]

    AND THE ARCHIVE IS DEFLATED, which is what the corrupt-member case above could not see: it is
    built with `zipfile.ZipFile(path, "w")`, whose default is ZIP_STORED, and every member of the
    corpus's own `backbone.npz` is `compress_type 8`. Damage IN PLACE inside a deflate stream --
    a bad sector, a resumed copy that leaves the file its right length -- fails inside zlib before
    the CRC that would have made it a `BadZipFile` is computed, and `zlib.error` subclasses
    `Exception` directly rather than `OSError`, `ValueError` or `RuntimeError`. So the last arm
    below is the corrupt-member case again in the one compression mode the corpus actually ships,
    and the damage is deterministic rather than sampled: 0xFF opens a deflate block with BTYPE 3,
    which is reserved. [M4.13 cycle 2, M413-C2-DIM-BB-02]
    """
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

    # Truncated, at four depths: the guard must not depend on where the copy stopped, and 0.0 is
    # where every interrupted copy starts.
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

    # And it is the class `app.py`'s lifespan actually catches, which is the whole point.
    assert issubclass(bb.BackboneError, RuntimeError)
    assert not issubclass(zlib.error, RuntimeError | OSError | ValueError)


def test_a_zeroed_backbone_row_is_not_a_warm_title(tmp_path):
    """cs-01. §5.1's e(t) branch, §4.3's backbone.npz, §12's M2 exit criterion.

    §4.3 lists the arrays the file ships and never says that some rows of E are placeholders.
    v20260828 does exactly that: `cold_mask` is true on 2,879 of 14,397 rows, E is written as
    zeros for all of them, and the coordinate the corpus actually has for them lives in
    `E_hat`/`b_hat`. Read as coordinates those rows are worse than absent ones — the personal
    term is exactly zero for every user for ever, 1,915 of them carry `item_n >= WARM_SUPPORT`
    so the sweep is told to skip them, and §12's M2 criterion counts them as coordinated
    because `e_source` is not 'none'.

    Two shapes, because the mask is a courtesy and not a contract: with it and without it.
    """
    root = tmp_path / "artifacts"
    root.mkdir()
    e = np.random.default_rng(20260906).standard_normal((4, 64)).astype(np.float32)
    e[1] = 0.0
    e[2] = 0.0
    arrays = {
        "title_ids": np.arange(1, 5, dtype=np.int32),
        "E": e,
        "b_i": np.array([0.4, 0.5, 0.6, 0.7], dtype=np.float32),
        # Rows 2 and 3 are zeroed; row 2 also has the crowd support that made `warm_title_ids`
        # excuse it from the very sweep that would have given it a coordinate.
        "item_n": np.array([500, 900, 4, 200], dtype=np.int32),
        "mu": np.float32(0.1),
        "cold_mask": np.array([False, True, True, False]),
    }
    np.savez(root / "backbone.npz", **arrays)

    store = ArtifactStore.open(root, "cold-v1")
    back = bb.Backbone.open(store)
    assert back.row(2) is None and back.row(3) is None
    assert back.row(1) == 0 and back.row(4) == 3
    assert back.support(2) == 0 and back.embedding(2) is None and back.raw_prior(2) is None
    # The count is reported rather than swallowed: an operator reading the load notes can see
    # how much of the basis the corpus did not place.
    assert any("2 of 4 rows carry no coordinate" in note for note in back.notes)

    # No Cold Tower placement: the title has no coordinate, and says so. That is the state
    # §12's M2 criterion has to be able to count.
    assert bb.coordinate(2, back) is None

    # With one, the coordinate is the pure cold limit — gate exactly 0, nothing blended in.
    e_hat = cold_vector(2)
    c = bb.coordinate(2, back, (e_hat, 0.33))
    assert c.e_source == "cold_tower"
    assert c.gate == 0.0 and c.item_n == 0
    assert np.array_equal(c.e, e_hat) and c.b == pytest.approx(0.33)

    # And it is not excused from the sweep. Row 2 clears WARM_SUPPORT and would have been
    # stamped warm on support alone; row 3 is thin as well as cold.
    assert reconcile.warm_title_ids(store) == [1, 4]

    # No mask shipped: the norm decides, and it decides the same way. The largest flagged row
    # in the real bundle has norm 9.4e-14 and the smallest unflagged one 6.3e-5.
    del arrays["cold_mask"]
    np.savez(root / "backbone.npz", **arrays)
    bare = bb.Backbone.open(ArtifactStore.open(root, "cold-v2"))
    assert bare.row(2) is None and bare.row(3) is None
    assert bare.row(1) == 0 and bare.row(4) == 3
    assert reconcile.warm_title_ids(ArtifactStore.open(root, "cold-v2")) == [1, 4]


def test_a_title_with_no_backbone_row_scores_entirely_from_the_cold_tower(backbone):
    """§5.1: "b̂(t) from the Cold Tower for cold titles". n_t = 0 ⇒ gate 0 ⇒ both terms are the
    Cold Tower's, exactly — not approximately, because the blend weight is exactly zero."""
    e_hat = cold_vector(8)
    c = bb.coordinate(8, backbone, (e_hat, 0.41))
    assert c.e_source == "cold_tower"
    assert c.gate == 0.0
    assert c.item_n == 0
    assert c.b == pytest.approx(0.41)
    assert np.array_equal(c.e, e_hat)


def test_a_thin_crowd_row_blends_both_halves_rather_than_choosing_between_them(backbone):
    """§5.1's `e(t) = gate·E[t] + (1-gate)·ê(t)`, and "Blend, never route".

    Title 5 carries n_t = 6, so the gate is 0.375 and 62.5% of its coordinate is the Cold
    Tower's. This is the branch a router would replace with a decision, and the only branch in
    which both halves are visible in one number.
    """
    e_hat, b_hat = cold_vector(5), COLD_PLACEMENTS[5]
    c = bb.coordinate(5, backbone, (e_hat, b_hat))
    assert c.e_source == "blended"
    assert c.gate == pytest.approx(0.375)
    assert c.b == pytest.approx(0.375 * backbone.raw_prior(5) + 0.625 * b_hat)
    assert np.allclose(c.e, 0.375 * backbone.embedding(5) + 0.625 * e_hat)
    # And it is genuinely a blend: neither half on its own.
    assert not np.allclose(c.e, backbone.embedding(5))
    assert not np.allclose(c.e, e_hat)


def test_a_warm_title_with_no_placement_keeps_its_row_and_shrinks_only_its_prior(backbone):
    """§5.1: "e(t) = E[t] if rated (warm)" — the row itself, not a rounded copy of it.

    "b(t) = shrunk item prior" still applies: with no b̂ to shrink toward, the gate pulls b_i
    toward the crowd mean μ, which is the only shrinkage constant §5.1 defines.
    """
    c = bb.coordinate(1, backbone, None)
    assert c.e_source == "backbone"
    assert np.array_equal(c.e, backbone.embedding(1).astype(np.float64))
    g = bb.gate(SUPPORT[1])
    assert c.b == pytest.approx(g * backbone.raw_prior(1) + (1 - g) * backbone.mu)


def test_a_title_with_neither_a_row_nor_a_placement_has_no_coordinate(backbone):
    """§12's M2 exit criterion, in the negative: "every owned title has a coordinate (warm
    Backbone row or Cold Tower placement)". Having neither is the one state that must not be
    ranked on an invented number, so there is no coordinate to rank it with."""
    assert bb.coordinate(8, backbone, None) is None
    assert bb.coordinate(999, backbone, None) is None


def test_a_title_at_item_n_thirty_weights_its_two_halves_three_to_one(backbone):
    """§5.1's middle line at the one support where the weights are round numbers.

    `gate = n_t/(n_t + k)` is 30/40 = 0.75 exactly, so e(t) is three parts Backbone row to one
    part Cold Tower placement — and "exactly" is the assertion, not "approximately": 0.75 and
    0.25 are both representable, so a blend that agrees to six decimals and not to the last bit
    has had something else done to it. Title 4 carries item_n = 30 in the shipped fixture.

    The same title on the FIT path is `test_ledger_observations.py`'s
    `test_the_fitted_coordinate_equals_the_served_coordinate_for_every_title`, which is the half
    that could not have been asserted before dd02: `standard_embeddings` composed its two sources
    as a precedence chain, so this title was fitted at its raw E while being served at this blend.
    """
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


def test_the_blend_report_names_the_ratio_of_the_two_weighted_halves_on_known_arrays():
    """cs-02 / dd15, decision 236. The measurement, on arrays whose two halves are known.

    Every row here has ||E|| = 0.2 and every placement ||ê|| = 50, so
    ((1-g)*||ê||)/(g*||E||) is a closed form per support and this test can write it out: at
    n_t = 6 the gate is 0.375 and the ratio 416.7, at 30 it is 0.75 and 83.3, at 55 it is 0.846
    and 45.5. The shipped bundle's own distribution over the 3,860 rows §5.1's middle line
    applies to is p10 82.5 / median 525.8 / p90 5,498.9, which these are a miniature of. 3,860 and
    not the 3,846 with `item_n < 90`: `WARM_SUPPORT` is computed and lands one ulp above 90, so
    the 14 non-cold rows at exactly 90 fall on the BLEND side -- the same ulp the `edge` assertion
    sixty lines below pins, and the population the helper actually walks.
    [M4.13 cycle 2, M413-C2-DIM5-03]

    There is no failing assertion about the SCALE, and that is decision 236 rather than timidity:
    whether the two halves should be brought within an order of magnitude of each other, and by
    which rescaling, is the corpus's contract question (§4.1 carries the artifact over verbatim).
    The app ships the number so the answer arrives with evidence behind it, and
    `ops/m413_exit_criterion.py` prints it against the real bundle with no verdict attached.
    """
    warm_norm, cold_norm = 0.2, 50.0
    supports = (6, 30, 55)
    back = basis([(t, n, warm_norm) for t, n in zip((1, 2, 3), supports, strict=True)])
    placements = {t: (cold_at(t, cold_norm), 0.0) for t in (1, 2, 3)}

    report = bb.blend_ratios(back, placements)
    assert (report.n_offered, report.n_measured) == (3, 3)
    assert (report.n_warm, report.n_no_row, report.n_degenerate) == (0, 0, 0)

    expected = sorted(
        ((1.0 - bb.gate(n)) * cold_norm) / (bb.gate(n) * warm_norm) for n in supports
    )
    assert sorted(report.ratios) == pytest.approx(expected)
    assert report.median == pytest.approx(((1 - 0.75) * cold_norm) / (0.75 * warm_norm))
    assert report.median == pytest.approx(83.3333, abs=1e-3)
    assert report.p10 <= report.median <= report.p90

    # The whole point of the number: the cold half outweighs the warm one by two orders of
    # magnitude at every gate the spec's own k produces, so "gate" is not weighting a blend.
    assert min(report.ratios) > 10.0
    assert report.as_dict()["measured"] == 3


def test_the_blend_report_counts_only_the_rows_the_middle_line_of_the_blend_applies_to():
    """§5.1 is three lines and only the middle one has two halves to compare.

    A row at or above WARM_SUPPORT is the FIRST line (E outright, gate >= 0.9); a title with no
    Backbone row is the THIRD (gate exactly 0, both terms the Cold Tower's); a pair with a zero on
    one side of the division has no ratio at all. Folding those into the measured set would let
    "the blend is balanced" and "there was nothing to blend" print the same number — which is how
    a measurement becomes a reassurance.
    """
    back = basis([
        (1, 89, 0.2),     # below the threshold: the middle line, and the only measured row
        (2, 91, 0.2),     # above it: warm, §5.1's first line
        (3, 4218, 0.2),   # far above it: warm
        (4, 0, 0.2),      # a coordinate with no crowd rating at all: g = 0, no ratio exists
        (5, 30, 0.0),     # a zeroed row (cs-01) left in the index: nothing to divide by
    ])
    placements = {t: (cold_at(t, 50.0), 0.0) for t in (1, 2, 3, 4, 5, 99)}

    report = bb.blend_ratios(back, placements)
    assert report.n_offered == 6
    assert report.n_measured == 1
    assert report.n_warm == 2
    assert report.n_no_row == 1, "title 99 has no row, so there is no warm half to compare"
    assert report.n_degenerate == 2
    assert report.ratios.size == 1
    assert report.ratios[0] == pytest.approx(
        ((1 - bb.gate(89)) * 50.0) / (bb.gate(89) * 0.2)
    )

    # Nothing measured is None and not 0.0: a percentile of an empty set is not a small number.
    empty = bb.blend_ratios(back, {2: (cold_at(2, 50.0), 0.0)})
    assert empty.n_measured == 0 and empty.median is None and empty.p90 is None

    # WARM_SUPPORT is computed (k*g/(1-g)) rather than written, so it is 90 + 1 ulp, and a title
    # with exactly 90 crowd ratings therefore falls on the BLEND side of the threshold — here and
    # in `placement.warm_title_ids`, which compares the same way. Recorded rather than rounded off:
    # the two readers agree, which is the property that matters.
    #
    # It is also what makes every published count of this helper's population a count at
    # WARM_SUPPORT and never at a literal 90. The two differ by the 17 rows v20260828 ships at
    # exactly 90 (14 of them non-cold), which is the whole of 3,860 against the 3,846 this file's
    # docstrings used to print. [M4.13 cycle 2, M413-C2-DIM5-03]
    assert bb.WARM_SUPPORT > 90 and bb.WARM_SUPPORT - 90.0 < 1e-9
    edge = bb.blend_ratios(basis([(7, 90, 0.2)]), {7: (cold_at(7, 50.0), 0.0)})
    assert (edge.n_measured, edge.n_warm) == (1, 0)


def test_the_blend_expression_rescales_neither_half_while_the_scale_question_is_open():
    """Decision 236: the app measures and rescales nothing, and that has to be checkable.

    Two halves of one claim. Numerically, e(t) is exactly `g*E + (1-g)*ê` over arrays whose norms
    are two orders of magnitude apart — a coordinate whose own norm is dominated by the cold half,
    which is the state the measurement exists to report. Statically, `coordinate`'s single blend
    expression contains no normalisation, because the plausible repair (divide ê by its norm, or by
    E's median row norm) is one line and would land exactly there. Asserted on the source rather
    than inferred from an output, since a rescaling that cancelled on this fixture would pass the
    numbers and still have moved every household's ranking.
    """
    back = basis([(1, 30, 0.2)])
    e_hat = cold_at(1, 50.0)
    c = bb.coordinate(1, back, (e_hat, 0.0))

    assert np.array_equal(c.e, 0.75 * back.E[0] + 0.25 * e_hat)
    assert np.linalg.norm(back.E[0]) == pytest.approx(0.2)
    assert np.linalg.norm(e_hat) == pytest.approx(50.0)
    # Unscaled, so the cold quarter is ~83x the warm three-quarters and the blend's own norm says
    # so. A normalised ê would have put this near 1.0 and hidden the finding inside a plausible
    # number.
    assert np.linalg.norm(c.e) == pytest.approx(np.linalg.norm(0.25 * e_hat), rel=0.02)

    source = inspect.getsource(bb.coordinate)
    assert "g * e_warm + (1.0 - g) * e_cold" in source, (
        "§5.1's blend is one expression; a second spelling of it is a second definition"
    )
    for spelling in ("linalg.norm", "normalize", "/ scale", "* scale"):
        assert spelling not in source, (
            f"{spelling!r} in coordinate(): decision 236 leaves the rescaling upstream, and the "
            "measurement in blend_ratios() is what the answer is owed"
        )


def test_zero_labels_give_beta_zero_and_the_bare_crowd_prior():
    """Stated plainly: a member who has rated nothing gets β = 0, v = 0, μ = 0, so score_u(t)
    is the z-scored crowd prior and identical for every unfitted member of that kind. The
    ranked list still answers — honestly — rather than refusing."""
    coords, reference, _ = synth(40, 0, seed=1)
    fit = foldin.fit_user([], coords, reference)

    assert fit.beta == 0.0
    assert fit.mu == 0.0
    assert fit.label_count == 0
    assert np.array_equal(fit.v, np.zeros(64))
    for c in reference:
        s, cf = foldin.score(fit, c)
        assert cf == 0.0
        assert s == pytest.approx((c.b - fit.prior_mean) / fit.prior_sd)
    # Two members with no labels rank the library identically — there is nothing yet to differ on.
    other = foldin.fit_user([], coords, reference)
    assert [foldin.score(fit, c)[0] for c in reference] == [
        foldin.score(other, c)[0] for c in reference
    ]


def test_fewer_than_five_labels_do_not_buy_a_blend_weight():
    """§0/§6.1: "personal signal roughly triples from 5 to 100 labels". A β cross-validated on
    four points is noise wearing a number, so below the floor the fold-in is computed and the
    ordering stays the crowd's."""
    coords, reference, labels = synth(40, 4, seed=7)
    fit = foldin.fit_user(labels, coords, reference)
    assert fit.label_count == 4
    assert fit.beta == 0.0
    assert fit.lam == max(foldin.LAMBDA_GRID)   # the stiffest grid point, not a fitted one


def test_the_personal_half_wins_when_it_actually_predicts():
    """The counterpart the previous test needs to be meaningful: given a signal the fold-in can
    see and a crowd prior that points the wrong way, β must move off zero. A blend weight that
    is always 0 would pass every honesty test and personalise nothing."""
    coords, reference, labels = synth(400, 60, seed=11, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=5)
    assert fit.beta > 0.0
    assert fit.cv_rho > 0.0
    assert fit.cf_sd > 0.0

    ranked = sorted(reference, key=lambda c: -foldin.score(fit, c)[0])
    crowd = sorted(reference, key=lambda c: -c.b)
    # "exactly where per-user top-10s stop being the global chart" — the personal ordering is
    # not the crowd's.
    assert [c.title_id for c in ranked[:10]] != [c.title_id for c in crowd[:10]]


def test_a_within_noise_floor_improvement_does_not_move_beta():
    """§0: "pipeline variance 0.003–0.008 Spearman; anything smaller is a tie." A tie must not
    buy personalisation, so when the crowd prior already ranks the labels perfectly nothing the
    fold-in adds can move β off zero."""
    coords, reference, labels = synth(200, 40, seed=3, prior_signal=1.0)
    fit = foldin.fit_user(labels, coords, reference, seed=2)
    assert fit.cv_rho > 0.9        # the prior alone is near-perfect here
    assert fit.beta == 0.0
    assert foldin.NOISE_FLOOR == 0.008


def test_beta_is_capped_at_the_measured_optimum_and_the_clamp_is_visible():
    """§5.1: "Blend with the crowd prior at β = 0.8 (measured optimum)".

    The grid searches to 1.0 on purpose. A fit that wants more than the measured optimum is
    unsupported by anything in the corpus, so it is clamped — and `beta_clamped` records that it
    happened, because a silent clamp is a measurement nobody ever sees.
    """
    coords, reference, labels = synth(400, 120, seed=17, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=4)
    assert fit.beta == pytest.approx(foldin.BETA_MAX)
    assert fit.beta_clamped is True
    assert max(foldin.BETA_GRID) == 1.0, "a grid stopping at 0.8 makes the clamp untestable"


def test_mu_shifts_every_score_and_reorders_nothing():
    """§5.1's μ_u. It exists so the number sits on the person's own scale; it is added to every
    title of the kind, so it cannot change an ordering — and a test that only looked at
    orderings would never notice if it silently did."""
    coords, reference, labels = synth(120, 30, seed=23)
    fit = foldin.fit_user(labels, coords, reference, seed=1)
    shifted = foldin.Fit(**{**{f: getattr(fit, f) for f in fit.__dataclass_fields__},
                            "mu": fit.mu + 1.75})

    base = np.array([foldin.score(fit, c)[0] for c in reference])
    moved = np.array([foldin.score(shifted, c)[0] for c in reference])
    assert np.allclose(moved - base, 1.75)
    assert list(np.argsort(-base)) == list(np.argsort(-moved))


def test_the_fit_is_reproducible_from_its_inputs():
    """Two people reading the same refit report must see the same number, so the fold-in is a
    function of (labels, coordinates, seed) and of nothing else — no clock, no row order."""
    coords, reference, labels = synth(150, 40, seed=31)
    a = foldin.fit_user(labels, coords, reference, seed=9)
    b = foldin.fit_user(list(reversed(labels)), coords, reference, seed=9)
    assert np.allclose(a.v, b.v)
    assert (a.beta, a.lam, a.mu) == (b.beta, b.lam, b.mu)
    assert a.cv_rho == pytest.approx(b.cv_rho)


def test_a_fitted_vector_survives_the_round_trip_through_the_bytea_convention():
    """`user_vector.vec` and `title_placement.e_hat` are both "64 × float32 LE". One pair of
    functions, so the two tables cannot drift into two conventions."""
    coords, reference, labels = synth(120, 30, seed=41)
    fit = foldin.fit_user(labels, coords, reference, seed=3)
    raw = bb.pack_vec(fit.v)
    assert len(raw) == 256
    assert np.allclose(bb.unpack_vec(raw), fit.v, atol=1e-6)
    with pytest.raises(ValueError):
        bb.unpack_vec(raw[:128])


def test_a_full_fold_in_refit_stays_inside_its_budget():
    """§5.3: "Fold-in user vectors, blend weights per label count — nightly — seconds".

    100 labels against an 839-title reference population: five λ × 5 folds of 64×64 solves, 55
    Spearmans, and one 839×64 matvec. Measured at 6.1–6.8 ms over five runs on this machine;
    asserted at 1 s, which is the budget the spec actually writes, so the test fails on an
    algorithmic regression (a per-title solve, an O(n²) Spearman) rather than on a slow
    afternoon.
    """
    coords, reference, labels = synth(839, 100, seed=53)
    started = time.perf_counter()
    fit = foldin.fit_user(labels, coords, reference, seed=1)
    rows = foldin.score_many(fit, reference)
    elapsed = time.perf_counter() - started

    assert len(rows) == 839
    assert elapsed < 1.0, f"fold-in + scoring took {elapsed * 1000:.0f} ms"


def test_score_many_agrees_with_score_one_at_a_time():
    """The ranked list sorts on the vectorised path and the title card prints the scalar one.
    Two implementations of one number is how a card starts disagreeing with the sort above it."""
    coords, reference, labels = synth(200, 30, seed=61)
    fit = foldin.fit_user(labels, coords, reference, seed=7)
    many = foldin.score_many(fit, reference)
    for c, (title_id, s, cf) in zip(reference, many, strict=True):
        one_s, one_cf = foldin.score(fit, c)
        assert title_id == c.title_id
        assert s == pytest.approx(one_s)
        assert cf == pytest.approx(one_cf)


# ==============================================================================================
# The materialised stack and the ranked read — real Postgres
# ==============================================================================================


async def test_priors_name_every_e_source_the_spec_defines(db, world):
    """§5.1 has four states and `title_prior` records which one each title landed in, so the
    §6.0 card and §8 stage 10's "no crowd data yet" badge read one column instead of
    re-deriving the branch."""
    rows = {r["title_id"]: dict(r) for r in await db.fetch("SELECT * FROM title_prior")}
    assert len(rows) == len(fx.TITLES)

    assert rows[1]["e_source"] == "backbone"
    assert rows[8]["e_source"] == "cold_tower"
    assert rows[5]["e_source"] == "blended"      # §5.1's gate branch, reachable and reached
    assert rows[8]["gate"] == 0.0
    assert rows[8]["b_i"] is None                # no Backbone row: there is no raw crowd bias
    assert rows[8]["b"] == pytest.approx(0.41, abs=1e-5)
    assert rows[1]["item_n"] == SUPPORT[1]
    assert world["report"].priors.by_source == {"backbone": 6, "blended": 1, "cold_tower": 1}


async def test_every_owned_title_has_a_coordinate_or_the_report_names_it(db, world, backbone):
    """§12's M2 exit criterion, as a number a test can read — and its negative, so the assertion
    can fail. A title with neither a Backbone row nor a placement keeps a `title_prior` row with
    b NULL, is absent from every ranked list, and is NAMED rather than counted."""
    for kind in ("movie", "series"):
        assert await serve.uncoordinated_owned(db, kind=kind, bundle_version=BUNDLE) == []

    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    report = await serve.materialise_priors(db, backbone, bundle_version=BUNDLE)
    assert report.uncoordinated_owned == [99]
    assert await serve.uncoordinated_owned(db, kind="movie", bundle_version=BUNDLE) == [99]
    assert await db.fetchval("SELECT b FROM title_prior WHERE title_id = 99") is None

    await place(db, 99, 0.5)
    report = await serve.materialise_priors(db, backbone, bundle_version=BUNDLE)
    assert report.uncoordinated_owned == []
    assert await db.fetchval("SELECT e_source FROM title_prior WHERE title_id = 99") == "cold_tower"


async def test_an_uncoordinated_title_is_absent_from_the_ranked_list_rather_than_ranked_at_zero(
    db, world, backbone
):
    """The alternative — ranking it on a default — puts a title nobody can score in the middle
    of the list, where it looks like a judgement."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (99, 'movie', 'Unplaced', true)"
    )
    await foldin.run(db, backbone, bundle_version=BUNDLE, only_stale=False, with_priors=True)

    section = await serve.ranked_section(
        db, user_id=world["patrick"], kind="movie", bundle_version=BUNDLE, limit=50
    )
    assert 99 not in [item["id"] for item in section["items"]]
    assert section["uncoordinated"] == [99]


# --- data-rules-ranking-partitions-by-kind ------------------------------------------------------


async def test_the_ranked_list_returns_two_kind_headed_sections_and_never_one_merged_ordering(
    db, world
):
    """§4.1 rule 5 + owner decision 18: kind is two independent toggles, and with both on the
    result is TWO kind-headed sections each ordered within itself.

    Measured: "the unpartitioned crowd top-10 is 8/10 TV series". This fixture reproduces the
    landmine — the single highest-scoring title in the library is a series — so the test asserts
    the thing a concatenated implementation gets wrong: the films section leads with the top
    FILM, and holds every film, not the films that survived a cross-kind sort.
    """
    sections = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["series", "movie"], bundle_version=BUNDLE, limit=50
    )

    # A list of sections, in canonical order. There is no top-level ordering to render.
    assert [s["kind"] for s in sections] == ["movie", "series"]
    assert [s["heading"] for s in sections] == ["Films", "Series"]

    by_kind = {s["kind"]: s for s in sections}
    assert {i["id"] for i in by_kind["movie"]["items"]} == set(MOVIES)
    assert {i["id"] for i in by_kind["series"]["items"]} == set(SERIES)
    for section in sections:
        assert all(item["kind"] == section["kind"] for item in section["items"])
        scores = [item["score"] for item in section["items"]]
        assert scores == sorted(scores, reverse=True), "each section is ordered within itself"

    # The landmine, asserted rather than assumed: the library's top-scoring title IS a series,
    # so a merged ordering would put it above every film.
    everything = await db.fetch(
        "SELECT title_id, kind, score FROM user_score WHERE user_id = $1 ORDER BY score DESC",
        world["patrick"],
    )
    assert everything[0]["kind"] == "series"
    assert by_kind["movie"]["items"][0]["kind"] == "movie"


async def test_a_limit_applies_per_section_and_not_across_a_merge(db, world):
    """Decision 18's "never one merged ordering", detectable by SHAPE rather than by reading
    the code: with both toggles on and limit=3 this returns 3 films AND 2 series — 5 rows — and
    the three films are the top three FILMS, which a merged-then-split implementation cannot
    return because a series has already taken one of its three slots."""
    sections = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie", "series"], bundle_version=BUNDLE, limit=3
    )
    films = [i["id"] for i in sections[0]["items"]]
    series = [i["id"] for i in sections[1]["items"]]

    assert len(films) == 3 and len(series) == 2
    assert sections[0]["total"] == len(MOVIES) and sections[1]["total"] == len(SERIES)

    rows = await db.fetch(
        "SELECT title_id, kind FROM user_score WHERE user_id = $1 ORDER BY score DESC, title_id",
        world["patrick"],
    )
    merged_top3 = [r["title_id"] for r in rows[:3]]
    assert films == [r["title_id"] for r in rows if r["kind"] == "movie"][:3]
    # The two implementations genuinely disagree on this fixture — otherwise the assertion above
    # would pass against a merge and this whole test would be decoration.
    assert films != [t for t in merged_top3 if t in MOVIES]


async def test_a_section_is_identical_with_and_without_the_other_kind_selected(db, world):
    """"The measured failure is a shared RANKING, not a shared screen" — so selecting Series
    must not touch a single row or position in Films. This is the contract every §6.0 shelf
    consumes."""
    both = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie", "series"], bundle_version=BUNDLE, limit=4
    )
    alone = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie"], bundle_version=BUNDLE, limit=4
    )
    assert len(alone) == 1
    assert both[0] == alone[0]


async def test_neither_kind_is_refused_rather_than_defaulted(db, world):
    """An empty selection would silently mean "everything", which is exactly the unpartitioned
    query rule 5 exists to prevent. `normalise_kinds` is reused rather than re-implemented, so
    the ranked list and the catalog refuse the same thing for the same reason."""
    for empty in ([], None, ["nonsense"]):
        with pytest.raises(ValueError, match="at least one kind"):
            await serve.ranked_sections(
                db, user_id=world["patrick"], kinds=empty, bundle_version=BUNDLE
            )
    with pytest.raises(ValueError, match="unknown kind"):
        await serve.ranked_section(
            db, user_id=world["patrick"], kind="anime", bundle_version=BUNDLE
        )


async def test_a_person_filter_does_not_suspend_the_partition(db, world):
    """Decision 18: a filmography spanning both kinds is complete ACROSS two sections, never
    merged into one because a person filter is active. With Films only, the hidden series is
    counted — a toggle that hides things without saying how many is the silent truncation the
    control exists to fix."""
    sections = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie", "series"], bundle_version=BUNDLE,
        person_id=100, limit=50,
    )
    assert [s["kind"] for s in sections] == ["movie", "series"]
    assert [[i["id"] for i in s["items"]] for s in sections] == [[2], [6]]

    films_only = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie"], bundle_version=BUNDLE,
        person_id=100, limit=50,
    )
    assert [[i["id"] for i in s["items"]] for s in films_only] == [[2]]
    hidden = await serve.hidden_by_kind(
        db, user_id=world["patrick"], kinds=["movie"], bundle_version=BUNDLE, person_id=100
    )
    assert hidden == {"series": 1}


async def test_the_section_filters_narrow_the_ranking_without_reordering_it(db, world):
    """The ranked list carries §6.0's catalog filters, and they are predicates on the section —
    never on the population the score was standardised over. So filtering narrows the list and
    leaves every surviving title's number, and their relative order, exactly as it was."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')",
        world["patrick"],
    )
    await db.execute("UPDATE title SET is_owned = false WHERE id = 4")
    args = {"user_id": world["patrick"], "kind": "movie", "bundle_version": BUNDLE, "limit": 50}

    everything = await serve.ranked_section(db, owned_only=False, **args)
    order = [i["id"] for i in everything["items"]]
    assert set(order) == set(MOVIES)

    owned = await serve.ranked_section(db, **args)
    assert 4 not in [i["id"] for i in owned["items"]]

    seen = await serve.ranked_section(db, seen="seen", owned_only=False, **args)
    assert [i["id"] for i in seen["items"]] == [1]
    unseen = await serve.ranked_section(db, seen="unseen", owned_only=False, **args)
    # An absent user_title row is the default, not an assertion (§7.3) — so it counts as unseen,
    # and the surviving order is the full order with one title removed rather than a re-sort.
    assert [i["id"] for i in unseen["items"]] == [t for t in order if t != 1]

    by_name = await serve.ranked_section(db, q="heat", owned_only=False, **args)
    assert [i["id"] for i in by_name["items"]] == [1]
    # §4.1 rule 8: never "clean" non-ASCII. The CJK title is searchable as itself.
    cjk = await serve.ranked_section(db, q="重慶", owned_only=False, **args)
    assert [i["id"] for i in cjk["items"]] == [5]

    nineties = await serve.ranked_section(db, decade=1990, owned_only=False, **args)
    assert {i["id"] for i in nineties["items"]} == {1, 4, 5}


# --- §10's basis guard, and the per-user half ---------------------------------------------------


async def test_a_read_bound_to_another_basis_returns_nothing_rather_than_old_numbers(db, world):
    """§10: "everything expressed in the old Backbone's basis is garbage against a new one."

    The failure mode of a dropped guard is silent, plausible-looking numbers, so the assertion
    is ABSENCE: a section against a superseded version is empty and its total says zero, rather
    than quietly serving the previous basis's ordering.
    """
    sections = await serve.ranked_sections(
        db, user_id=world["patrick"], kinds=["movie", "series"], bundle_version="test-v2", limit=50
    )
    assert [s["total"] for s in sections] == [0, 0]
    assert [s["items"] for s in sections] == [[], []]


async def test_a_zero_label_member_is_still_fitted_and_still_ranked(db, world):
    """"Fitted to zero labels" and "never fitted" are different states, and §6.0's zero-verdict
    fallback has to tell them apart without a second query — so the row is written."""
    row = await serve.fit_row(db, user_id=world["jenny"], kind="movie")
    assert row is not None
    assert row["label_count"] == 0
    assert row["blend_beta"] == 0.0
    assert row["bundle_version"] == BUNDLE

    section = await serve.ranked_section(
        db, user_id=world["jenny"], kind="movie", bundle_version=BUNDLE, limit=50
    )
    assert section["fitted"] is True
    assert section["personalised"] is False
    assert section["label_count"] == 0
    assert len(section["items"]) == len(MOVIES)


async def test_a_clamped_blend_weight_is_storable_and_anything_above_it_is_not(db, world):
    """§5.1's β = 0.8 is a constraint, not a code convention: `0009_scoring.sql` CHECKs it — and
    the CHECK has to admit the very value §5.1 measured.

    It did not. `blend_beta` is `real`, and the CHECK compared it against the numeric literal
    0.8, which Postgres resolves through float8 where float4(0.8) is 0.800000011920929. So
    `SELECT 0.8::real <= 0.8` was FALSE: a fit clamped to the ceiling failed its INSERT, in a
    nightly job, on the most common case there is. The literal is now cast to `real`, and the
    first assertion below is what would catch a revert.
    """
    assert await db.fetchval("SELECT 0.8::real <= 0.8::real") is True, "the ceiling must admit β"
    assert await db.fetchval("SELECT 0.8::real <= 0.8") is False, (
        "float4(0.8) still widens above the numeric literal — the cast in 0009 is load-bearing"
    )

    coords, reference, labels = synth(400, 120, seed=17, prior_signal=0.0)
    fit = foldin.fit_user(labels, coords, reference, seed=4)
    assert fit.beta == pytest.approx(foldin.BETA_MAX) and fit.beta_clamped

    # `updated_at` is the caller's since M4.13's step 21 — the moment the labels were read, which
    # only the caller knows — so it is read here the way `refit_user` reads it rather than
    # defaulted, and this test is about the CHECK on the column beside it.
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
    """§10: a re-import "recomputes user fold-in vectors, per-label-count blend weights …" while
    "Ledger observations always survive re-import". A refit replaces scores rather than updating
    them, so a title that lost its coordinate loses its score instead of keeping a stale one."""
    before = await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", world["patrick"])
    stamps = await db.fetch(
        "SELECT title_id, computed_at FROM user_score WHERE user_id = $1", world["patrick"]
    )

    for title_id, value in ((3, 2), (4, 0)):
        await db.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
            world["patrick"], title_id, value,
        )
    # The tick is debounced since M4.13's step 22: §12 M2 asks for a personal ranking "after a
    # sitting", so a label written this instant is deliberately not refit yet. Moving the whole
    # timeline back by the pause is what waiting it out looks like without spending thirty seconds
    # here — both stamps shift by the same interval, so the ORDER of the two clocks, which is what
    # staleness is actually about, is untouched. [M4.13, perf-04]
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
    """It cannot inform a fold-in — there is no vector to regress against — but silently
    ignoring it would make `label_count` and the class-balance widget disagree about how much
    the person has actually done."""
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


# --- §6.0's model line ---------------------------------------------------------------------------


async def test_the_model_line_prints_the_real_b_beta_and_gate(db, world):
    """§6.0: "the model line in the data voice (`b(t) 0.52 · β 0.8 · gate 0.93`)".

    The card prints the number the ranking uses — no display rescaling — which is the whole of
    the transparency promise. σ is an em dash before the Ledger has fitted this title, never
    0.00, which would read as certainty.
    """
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
    """§3.1: render the state, do not error. Three states have no honest line — no title, no
    prior in the active basis, and a title with no coordinate at all — and each names itself."""
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
    """"Rated (warm)" is read as CROWD support: n_t is a crowd count, so the gate is the same
    number for everyone looking at the same card. A per-user reading would make §6.0's one
    printed number two."""
    patrick = await serve.model_line(
        db, user_id=world["patrick"], title_id=1, bundle_version=BUNDLE
    )
    jenny = await serve.model_line(db, user_id=world["jenny"], title_id=1, bundle_version=BUNDLE)
    assert patrick["gate"] == jenny["gate"] == pytest.approx(bb.gate(SUPPORT[1]))
    assert patrick["b"] == jenny["b"]


async def test_a_silent_reask_does_not_erase_the_label_it_re_asked(db, world):
    """§13's re-ask stream "measures test-retest consistency" — it is an instrument, and an
    instrument that moves what it measures is not one.

    The rule everyone agreed on was "a re-ask must not count as a second observation". What
    shipped did something else: `record_verdict` stamps `superseded_by` on the previous row for
    a re-ask too (deliberately — it is the person's latest answer), so a predicate spelling
    `superseded_by IS NULL AND NOT is_reask` matched NEITHER row and the title left the label
    set entirely. §13 aims for ~200 re-asks; each one silently deleted a real label from §5.1's
    fold-in, from `blend_beta`, and from the count Home's why-line prints.

    Same answer both times, so nothing about this person's taste has changed and the label count
    must not move either.
    """
    from spielplan.ledger import observations

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
    assert dict(after)[1] == 2, "and the erased title keeps the answer the person actually gave"
    assert sorted(after) == sorted(before), "a same-answer re-ask must change nothing at all"

    # The append-only history is intact: §4.2 keeps every row, the fit just reads one of them.
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = 1", patrick
    ) == rows_before + 1
