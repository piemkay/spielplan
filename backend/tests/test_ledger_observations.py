"""The Ledger's database side (§4.2, §5.2, §5.3, §13): what the fit may see (both superseded and live
verdicts, no held-out or re-ask rows), what may write, and §5.3's budgets. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams
from spielplan.ledger.observations import UndoRefused
from spielplan.models.artifacts import ArtifactStore
from spielplan.scoring import backbone as bb
from spielplan.scoring import serve
from tests.fixtures import make_bundle as fx
from tests.helpers import insert_user


# No RNG: §5.3's budgets and "same observations, same fit" need the same input on every machine.
def _embedding(title_id: int) -> np.ndarray:
    rng = np.random.default_rng(1000 + title_id)
    vector = rng.normal(size=64)
    return vector / (np.linalg.norm(vector) * 8.0)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    return (
        np.stack([_embedding(t) for t in ids]),
        np.ones(len(ids), dtype=bool),
    )


async def make_titles(db, specs):
    """specs: [(id, kind, name)]."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned)
        SELECT x.id, x.kind, x.name, true
        FROM unnest($1::int[], $2::text[], $3::text[]) AS x(id, kind, name)
        """,
        [s[0] for s in specs],
        [s[1] for s in specs],
        [s[2] for s in specs],
    )


@pytest.fixture
async def world(db):
    await make_titles(
        db,
        [(i, "movie" if i <= 6 else "series", f"Title {i}") for i in range(1, 9)],
    )
    return {"user": await insert_user(db, "patrick", "admin")}


BUNDLE = "test-v1"

# Restated so a fixture change shows as a diff; title 8
# is flagged in `cold_mask`, so its usable support is 0.
SUPPORT = {1: 4218, 2: 900, 3: 120, 4: 30, 5: 6, 6: 240, 7: 55, 8: 0}

# `classify_warm` writes no placement for a warm title, so only thin titles and the rowless one appear.
COLD_PLACEMENTS = {4: 0.55, 5: 0.69, 7: 0.31, 8: 0.41}


def cold_vector(title_id: int) -> np.ndarray:
    """PCG64 is platform-independent, so the blend is the same on every box."""
    v = np.random.default_rng(20260830 + 1000 + title_id).standard_normal(64)
    return v / np.linalg.norm(v)


def placed_vector(title_id: int) -> np.ndarray:
    """Stored as float32, so the blend is computed from the rounded value and the equality stays exact."""
    return bb.unpack_vec(bb.pack_vec(cold_vector(title_id)))


@pytest.fixture(scope="session")
def store(tmp_path_factory) -> ArtifactStore:
    root = tmp_path_factory.mktemp("ledger-bundle")
    fx.make_bundle(root)
    return ArtifactStore.open(root / "artifacts", BUNDLE)


@pytest.fixture
async def served(db, store):
    """`world` assigns kinds by id; the bundle's own kinds differ (6 and 7 are series)."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )
    for title_id, kind, name, _orig, year, runtime, imdb, tmdb, _lang, _country in fx.TITLES:
        await db.execute(
            "INSERT INTO title (id, kind, name, year, runtime_min, imdb_id, tmdb_id, is_owned) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,true)",
            title_id, kind, name, year, runtime, imdb, tmdb,
        )
    for title_id, b_hat in COLD_PLACEMENTS.items():
        await db.execute(
            """
            INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                         tower_sha256, input_dim, blocks_present, blocks_dropped,
                                         blocks_imputed, nnz)
            VALUES ($1, $2, $3, $4, 'sha-contract', 'sha-tower', 131,
                    ARRAY['genre'], ARRAY[]::text[], ARRAY[]::text[], 7)
            """,
            title_id, BUNDLE, bb.pack_vec(cold_vector(title_id)), b_hat,
        )
    backbone = bb.Backbone.open(store)
    user = await insert_user(db, "patrick", "admin")
    return {"user": user, "backbone": backbone, "store": store}


async def fitted_row(db, served_world, title_id: int) -> np.ndarray:
    matrix, embedded = await observations.resolve_embeddings(
        observations.standard_embeddings(
            db, served_world["backbone"], bundle_version=BUNDLE
        ),
        [title_id],
    )
    assert embedded[0], f"title {title_id} entered the fit with no coordinate at all"
    return matrix[0]


async def live_verdicts(db, user_id, title_id):
    return await db.fetch(
        "SELECT id, value, created_at, superseded_by, is_reask FROM verdict "
        "WHERE user_id = $1 AND title_id = $2 ORDER BY id",
        user_id,
        title_id,
    )


async def test_a_re_rating_inserts_a_row_and_stamps_the_old_one_rather_than_mutating_it(db, world):
    """The FIRST row must be byte-identical afterwards; an audit-row design would pass a row count."""
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    before = (await live_verdicts(db, user, 1))[0]

    second = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    rows = await live_verdicts(db, user, 1)

    assert len(rows) == 2
    assert rows[0]["id"] == first.row_id and rows[1]["id"] == second.row_id
    assert rows[0]["value"] == before["value"] == 0, "the earlier verdict's value was mutated"
    assert rows[0]["created_at"] == before["created_at"]
    assert rows[0]["superseded_by"] == second.row_id
    assert second.superseded_id == first.row_id
    assert [r["superseded_by"] is None for r in rows] == [False, True], "exactly one live row"

    third = await observations.record_verdict(db, user_id=user, title_id=1, value=1)
    rows = await live_verdicts(db, user, 1)
    assert [r["superseded_by"] for r in rows] == [second.row_id, third.row_id, None], (
        "a third rating must supersede only the second and leave the first's pointer alone"
    )


async def test_the_fit_sees_both_the_superseded_verdict_and_the_live_one(db, world):
    """Fails the moment anyone adds `WHERE superseded_by IS NULL`, which looks like tidying."""
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    obs = loaded.obs
    row = int(np.flatnonzero(obs.title_ids == 1)[0])
    levels = sorted(obs.ord_level[(obs.ord_index == row) & (obs.ord_arm == observations.ARM_VERDICT)])

    assert levels == [0, 2], "the superseded verdict is missing from the fit"
    assert loaded.n_verdicts == 2


async def test_the_insert_and_the_supersede_stamp_are_one_transaction(db, world, monkeypatch):
    """Failing at the last step asks whether the insert and the stamp share a transaction."""
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    # Unseen again, so the second verdict reaches its last step: the seen write.
    await db.execute("UPDATE user_title SET state = 'unseen' WHERE user_id = $1 AND title_id = 1", user)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("Jellyfin fell over mid-write")

    monkeypatch.setattr(observations, "_set_state", explode)
    with pytest.raises(RuntimeError):
        await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    rows = await live_verdicts(db, user, 1)
    assert [r["id"] for r in rows] == [first.row_id], "the failed insert survived"
    assert rows[0]["superseded_by"] is None, "a row was stamped as superseded by a row that is gone"


async def test_a_verdict_implies_seen_and_undo_restores_the_exact_prior_state(db, world):
    """Undo restores `jf_synced_at` too, or the next sweep pushes a write nobody asked for."""
    user = world["user"]
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) VALUES ($1,$2,'unseen',$3)",
        user,
        1,
        stamp,
    )

    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    )
    assert row["state"] == "seen" and row["jf_synced_at"] is None
    assert write.implied_seen is True

    await observations.undo(db, user_id=user, write=write)
    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    )
    assert (row["state"], row["jf_synced_at"]) == ("unseen", stamp)
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 0


async def test_a_tier_implies_seen_and_a_seen_title_owes_jellyfin_nothing(db, world):
    """Decision 531. A title already seen keeps its row as it was, or every move queues a push."""
    user = world["user"]
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    state = "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2"
    await db.executemany(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) VALUES ($1,$2,$3,$4)",
        [(user, 1, "unseen", stamp), (user, 2, "seen", stamp)],
    )

    write = await observations.record_tier_edit(db, user_id=user, title_id=1, tier=4)
    assert write.implied_seen is True
    assert tuple(await db.fetchrow(state, user, 1)) == ("seen", None)
    await observations.undo(db, user_id=user, write=write)
    assert tuple(await db.fetchrow(state, user, 1)) == ("unseen", stamp)
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user) == 0

    moved = await observations.record_tier_edit(db, user_id=user, title_id=2, tier=4)
    assert moved.implied_seen is False
    assert tuple(await db.fetchrow(state, user, 2)) == ("seen", stamp)


async def test_re_rating_a_seen_title_owes_jellyfin_nothing_and_its_undo_leaves_the_row(db, world):
    """A series push re-marks every episode, so a verdict writes `seen` only where it was not."""
    user = world["user"]
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    state = "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2"
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) VALUES ($1,$2,'seen',$3)",
        user,
        1,
        stamp,
    )

    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    assert write.implied_seen is False and write.prior_state == ()
    assert tuple(await db.fetchrow(state, user, 1)) == ("seen", stamp)

    await observations.undo(db, user_id=user, write=write)
    assert tuple(await db.fetchrow(state, user, 1)) == ("seen", stamp)
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 0


async def test_not_seen_writes_no_observation_row_and_keeps_the_history(db, world):
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    write = await observations.record_not_seen(db, user_id=user, title_id=1)

    assert write.row_id is None
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    ) == "unseen"
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id=$1", user) == 1, (
        "the flip to unseen deleted the rating history"
    )

    await observations.undo(db, user_id=user, write=write)
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    ) == "seen"


async def test_undo_unstamps_the_row_it_superseded_and_stops_at_the_block_boundary(db, world):
    """The earlier verdict must be live again, and an observation from before the block is refused."""
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    second = await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    result = await observations.undo(db, user_id=user, write=second)
    rows = await live_verdicts(db, user, 1)
    assert [r["id"] for r in rows] == [first.row_id]
    assert rows[0]["superseded_by"] is None, "the earlier verdict stayed superseded by a ghost"
    assert result.unsuperseded == (first.row_id,)

    later = datetime.now(UTC) + timedelta(seconds=5)
    with pytest.raises(UndoRefused):
        await observations.undo(db, user_id=user, write=first, block_started_at=later)
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE id = $1", first.row_id) == 1


async def test_undo_refuses_another_persons_observation(db, world):
    """The journal is per person. A row id is not an authorisation."""
    user = world["user"]
    other = await insert_user(db, "jenny")
    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    with pytest.raises(UndoRefused):
        await observations.undo(db, user_id=other, write=write)


async def test_the_uniform_random_held_out_duels_never_reach_the_fit(db, world):
    """§13: the uniform-random stream is the only unbiased evaluation data, so it never reaches the fit."""
    user = world["user"]
    for selection in ("random", "boundary", "exploration"):
        await observations.record_duel(
            db, user_id=user, title_a=1, title_b=2, outcome="A",
            context="profile_battle", selection=selection, decisive=False, hp=DEFAULTS,
        )
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=2, outcome="B",
        context="profile_battle", selection=observations.HELD_OUT, decisive=False, hp=DEFAULTS,
    )

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id=$1", user) == 4
    assert loaded.n_duels == 3, "a held-out pair was trained on"
    assert loaded.n_held_out == 1
    assert set(loaded.obs.duel_outcome.tolist()) == {model.OUT_A}, (
        "the held-out row's outcome leaked into the fit"
    )


async def test_a_silent_re_ask_is_not_a_second_observation(db, world):
    """Keyed on `is_reask` (NOT NULL), not `reask_of` (SET NULL on delete), or it fails open."""
    user = world["user"]
    original = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    reask = await observations.record_verdict(
        db, user_id=user, title_id=1, value=1, is_reask=True, reask_of=original.row_id
    )

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert loaded.n_verdicts == 1 and loaded.n_reask == 1
    assert loaded.obs.ord_level.tolist() == [2], "the re-ask was counted as a second observation"

    rows = await live_verdicts(db, user, 1)
    assert rows[0]["superseded_by"] == reask.row_id, "the person's latest answer is not current"

    await db.execute("UPDATE verdict SET reask_of = NULL WHERE id = $1", reask.row_id)
    again = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert again.obs.ord_level.tolist() == [2], (
        "the exclusion depends on reask_of, so it fails open once a row is undone"
    )


async def test_the_fit_is_partitioned_by_kind_and_a_cross_kind_duel_is_refused(db, world):
    """Refused at the write: a row nothing reads is worse than an error."""
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)   # movie
    await observations.record_verdict(db, user_id=user, title_id=7, value=0)   # series

    movies = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    series = await observations.load_observations(
        db, user_id=user, kind="series", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert movies.obs.title_ids.tolist() == [1]
    assert series.obs.title_ids.tolist() == [7]

    with pytest.raises(ValueError, match="cross-kind"):
        await observations.record_duel(
            db, user_id=user, title_a=1, title_b=7, outcome="A", context="profile_battle"
        )
    assert await db.fetchval("SELECT count(*) FROM duel") == 0


async def test_a_margin_less_duel_carries_the_hesitant_weight_from_hyperparams(db, world):
    """§6.3's drag-drop neighbour duels are margin-less, which is hesitant, not weightless."""
    user = world["user"]
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=2, outcome="A", context="tier_insert"
    )
    await observations.record_duel(
        db, user_id=user, title_a=3, title_b=4, outcome="B", context="profile_battle",
        decisive=True, hp=DEFAULTS,
    )
    stored = await db.fetch("SELECT margin FROM duel WHERE user_id=$1 ORDER BY id", user)
    assert stored[0]["margin"] is None, "a neighbour duel invented a margin"
    assert stored[1]["margin"] == pytest.approx(DEFAULTS.margin_decisive)

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    # `duel.margin` is float4, and §4.3 normalises by the mean, so the comparison is approximate.
    assert loaded.obs.duel_margin.tolist() == pytest.approx(
        [DEFAULTS.margin_hesitant, DEFAULTS.margin_decisive], rel=1e-6
    )

    retuned = Hyperparams(margin_hesitant=0.5)
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=retuned, embeddings=fixture_embeddings
    )
    assert loaded.obs.duel_margin[0] == 0.5, "the hesitant weight is hard-coded somewhere"


async def test_a_bundle_less_household_still_produces_an_observation_set(db, world):
    """With no Backbone e = 0 and s = μ + r: the fit still ranks what was rated."""
    user = world["user"]
    for title_id, value in ((1, 2), (2, 0), (3, 1)):
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)

    loaded = await observations.load_observations(db, user_id=user, kind="movie", hp=DEFAULTS)
    assert not loaded.obs.embedded.any()
    assert loaded.obs.embeddings.shape == (3, 64)
    fit = model.fit(loaded.obs, DEFAULTS)
    assert np.all(np.isfinite(fit.s))
    assert fit.s[0] > fit.s[1], "with no bundle the fit stopped ranking what it was told"


async def test_a_warm_title_is_fitted_at_its_backbone_row_outright(db, served):
    """Bit equality: a repair that blended every title would still pass to six decimals."""
    assert SUPPORT[1] >= bb.WARM_SUPPORT and 1 not in COLD_PLACEMENTS
    fit_e = await fitted_row(db, served, 1)
    served_e = (await serve.coordinates(db, served["backbone"], bundle_version=BUNDLE))[1]
    row = served["backbone"].embedding(1).astype(np.float64)

    assert served_e.e_source == "backbone"
    assert np.array_equal(served_e.e, row)
    assert np.array_equal(fit_e, bb.direction(served_e))
    assert np.allclose(fit_e, row / np.linalg.norm(row) * bb.gate(SUPPORT[1]))


async def test_a_thin_title_is_fitted_at_the_blend_rather_than_at_its_raw_backbone_row(db, served):
    """`chain` handed thin titles their raw row; the fit must read the direction of the BLEND."""
    assert SUPPORT[4] == 30
    raw = served["backbone"].embedding(4).astype(np.float64)
    e_hat = placed_vector(4)
    fit_e = await fitted_row(db, served, 4)
    coords = await serve.coordinates(db, served["backbone"], bundle_version=BUNDLE)

    assert np.array_equal(coords[4].e, 0.75 * raw + 0.25 * e_hat), "not served at §5.1's blend"
    assert np.array_equal(fit_e, bb.direction(coords[4])), "the fit is not at the served blend"
    blend = 0.75 * raw + 0.25 * e_hat
    assert np.allclose(fit_e, blend / np.linalg.norm(blend))
    assert not np.allclose(fit_e, raw / np.linalg.norm(raw)), (
        "the fit is still at the raw Backbone row's direction (dd02)"
    )
    assert not np.allclose(fit_e, e_hat / np.linalg.norm(e_hat))

    deltas = {}
    for title_id in (4, 5, 7):
        row = served["backbone"].embedding(title_id).astype(np.float64)
        deltas[title_id] = float(np.linalg.norm(coords[title_id].e - row))
    print(f"\n||delta e|| fit-vs-raw-row: {deltas}")
    assert deltas[5] > deltas[4] > deltas[7] > 0.2, (
        "the gap between the blend and the raw row must shrink as the crowd support grows"
    )
    assert 0.5 < deltas[4] < 1.0 and 1.5 < deltas[5] < 2.5
    # These figures are published as this fixture's measurement, so a fixture change must show here.
    assert [round(deltas[t], 2) for t in (5, 4, 7)] == [1.94, 0.72, 0.40], deltas


async def test_a_title_with_no_backbone_row_is_fitted_at_its_placement_alone(db, served):
    """n_t = 0 gives a gate of exactly 0.0, so "no row" and "pure Cold Tower" are one statement."""
    assert SUPPORT[8] == 0 and served["backbone"].row(8) is None
    fit_e = await fitted_row(db, served, 8)
    e_hat = placed_vector(8)
    assert np.array_equal(fit_e, e_hat * (1.0 / np.linalg.norm(e_hat)))
    assert bb.gate(0) == 0.0


async def test_the_fitted_coordinate_equals_the_served_coordinate_for_every_title(db, served):
    """Every title, including uncovered ones: the fit and the served coordinate are the same function."""
    backbone = served["backbone"]
    coords = await serve.coordinates(db, backbone, bundle_version=BUNDLE)
    ids = [int(r["id"]) for r in await db.fetch("SELECT id FROM title ORDER BY id")]
    matrix, embedded = await observations.resolve_embeddings(
        observations.standard_embeddings(db, backbone, bundle_version=BUNDLE), ids
    )

    for i, title_id in enumerate(ids):
        if title_id in coords:
            assert embedded[i], f"title {title_id} is served a coordinate and fitted without one"
            assert np.array_equal(matrix[i], bb.direction(coords[title_id])), (
                f"title {title_id} is fitted at a different coordinate than it is served at"
            )
        else:
            assert not embedded[i], f"title {title_id} is fitted at a coordinate nobody serves"

    # Anti-vacuity: at least one title must be somewhere the precedence chain could not put it.
    blended = [t for t, c in coords.items() if c.e_source == "blended"]
    assert len(blended) >= 3, f"the fixture has no blended titles to disagree about: {blended}"
    for title_id in blended:
        raw = backbone.embedding(title_id).astype(np.float64)
        assert not np.allclose(matrix[ids.index(title_id)], raw / np.linalg.norm(raw))
    assert {c.e_source for c in coords.values()} == {"backbone", "blended", "cold_tower"}


async def _board(db, user_id: int, kind: str) -> list[SimpleNamespace]:
    """`ledger_state` in rank order."""
    rows = await db.fetch(
        "SELECT title_id, s, sigma, cdf, tier, observed FROM ledger_state"
        " WHERE user_id = $1 AND kind = $2 ORDER BY s DESC, title_id",
        user_id, kind,
    )
    return [SimpleNamespace(**dict(r)) for r in rows]


async def _set_tier_set(db, user_id: int, labels: Sequence[str], *, kind: str = "movie") -> None:
    """Written directly: this file is about what the FIT reads, not about `rank`'s settings path."""
    boundaries = [float(b) for b in model.initial_cutpoints(len(labels))]
    await db.execute(
        """
        INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set)
        VALUES ($1, $2, $3::float8[], $4::text[])
        ON CONFLICT (user_id, kind) DO UPDATE
            SET boundaries = EXCLUDED.boundaries, tier_set = EXCLUDED.tier_set
        """,
        user_id, kind, boundaries, list(labels),
    )


def test_rescale_level_maps_by_cumulative_prior_mass_and_clamps_only_last():
    """MASS, not index; monotone; identity at equal K; and the clamp is LAST (6 -> 12 of level 5 is 11,
    not 5). `tier_edit.tier` has no CHECK against the set."""
    rescale = observations.rescale_level

    assert [rescale(level, k_from=6, k_to=12) for level in range(6)] == [0, 1, 4, 7, 9, 11]
    assert [rescale(level, k_from=6, k_to=4) for level in range(6)] == [0, 0, 1, 2, 3, 3]
    assert rescale(5, k_from=6, k_to=12) == 11, "a clamp-first reading would give 5"
    assert rescale(6, k_from=12, k_to=4) == 2, "a clamp-first reading would give 3"

    for k_from in range(2, 15):
        for k_to in range(2, 15):
            mapped = [rescale(level, k_from=k_from, k_to=k_to) for level in range(k_from)]
            assert mapped == sorted(mapped), (k_from, k_to, mapped)
            assert all(0 <= level < k_to for level in mapped), (k_from, k_to, mapped)
    for k in range(2, 21):
        assert [rescale(level, k_from=k, k_to=k) for level in range(k)] == list(range(k))

    # The clamp, reached only after the map: every level the column can hold, from both ends.
    for level in (-40, -1, 6, 12, 400):
        assert 0 <= rescale(level, k_from=6, k_to=4) <= 3
        assert 0 <= rescale(level, k_from=None, k_to=6) <= 5
    # An unknown board (`n_levels` NULL) is read as written.
    assert rescale(5, k_from=None, k_to=6) == 5
    assert rescale(5, k_from=None, k_to=4) == 3
    with pytest.raises(ValueError, match="at least one level"):
        rescale(0, k_from=6, k_to=0)


async def test_a_tier_edit_records_the_tier_set_size_it_was_written_under(db, world):
    """`ledger_cutpoints.tier_set` is overwritten in place, so the row is the only record of its K."""
    user = world["user"]
    first = await observations.record_tier_edit(db, user_id=user, title_id=1, tier=5)
    assert await db.fetchval("SELECT n_levels FROM tier_edit WHERE id = $1", first.row_id) == 6

    await _set_tier_set(db, user, [f"T{i}" for i in range(12)])
    second = await observations.record_tier_edit(db, user_id=user, title_id=2, tier=11)

    rows = await db.fetch(
        "SELECT id, tier, n_levels FROM tier_edit WHERE user_id = $1 ORDER BY id", user
    )
    assert [(r["tier"], r["n_levels"]) for r in rows] == [(5, 6), (11, 12)]
    assert rows[0]["id"] == first.row_id and rows[1]["id"] == second.row_id


async def test_an_edit_at_five_of_six_is_read_at_eleven_of_twelve(db, world):
    """The raw index left every S edit at tier 5 of 12; the fit must see the top tier."""
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    await observations.record_tier_edit(db, user_id=user, title_id=1, tier=5)
    await _set_tier_set(db, user, [f"T{i}" for i in range(12)])

    loaded = await observations.load_observations(db, user_id=user, kind="movie", hp=DEFAULTS)
    assert len(loaded.tier_set) == 12
    tier_levels = [
        int(level)
        for level, arm in zip(loaded.obs.ord_level, loaded.obs.ord_arm, strict=True)
        if int(arm) == observations.ARM_TIER
    ]
    assert tier_levels == [11], "the edit is still being read as level 5 of a 12-level set"
    assert loaded.obs.n_levels == 12

    # §4.2 is append-only, so the rescale is a READ.
    stored = await db.fetchrow("SELECT tier, n_levels FROM tier_edit WHERE user_id = $1", user)
    assert dict(stored) == {"tier": 5, "n_levels": 6}


async def test_shrinking_to_four_labels_keeps_an_s_edit_above_a_b_edit(db, world):
    """Mapped by mass the ORDER survives a shrink, where the clamp merged B..S."""
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=3, value=1)
    await observations.record_tier_edit(db, user_id=user, title_id=1, tier=5)   # S of E..S
    await observations.record_tier_edit(db, user_id=user, title_id=2, tier=3)   # B of E..S
    await _set_tier_set(db, user, ["bad", "ok", "good", "best"])

    loaded = await observations.load_observations(db, user_id=user, kind="movie", hp=DEFAULTS)
    by_title = {
        int(loaded.obs.title_ids[index]): int(level)
        for index, level, arm in zip(
            loaded.obs.ord_index, loaded.obs.ord_level, loaded.obs.ord_arm, strict=True
        )
        if int(arm) == observations.ARM_TIER
    }
    assert by_title == {1: 3, 2: 2}
    assert by_title[1] > by_title[2], "the clamp collapsed S and B into one tier"


async def _rate(db, user, *, verdicts=(), duels=(), tier_edits=()):
    for title_id, value in verdicts:
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)
    for a, b, outcome in duels:
        await observations.record_duel(
            db, user_id=user, title_a=a, title_b=b, outcome=outcome,
            context="profile_battle", decisive=False, hp=DEFAULTS,
        )
    for title_id, tier in tier_edits:
        await observations.record_tier_edit(db, user_id=user, title_id=title_id, tier=tier)


async def test_the_nightly_refit_writes_the_board_the_cutpoints_and_the_cache(db, world):
    """§5.2: the cutpoints ARE the displayed boundaries, not a percentile."""
    user = world["user"]
    await _rate(
        db, user,
        verdicts=[(1, 2), (2, 2), (3, 1), (4, 1), (5, 0), (6, 0)],
        duels=[(1, 2, "A"), (3, 4, "TIE"), (5, 6, "B"), (1, 5, "A")],
        tier_edits=[(1, 5), (5, 0)],
    )
    report = await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.fitted and report.converged, report.as_dict()
    assert (report.n_verdicts, report.n_duels, report.n_tier_edits) == (6, 4, 2)

    board = await _board(db, user, "movie")
    assert sorted(r.title_id for r in board) == [1, 2, 3, 4, 5, 6]
    assert {r.observed for r in board} == {True}
    assert all(np.isfinite(r.s) and r.sigma > 0 for r in board)
    ranked = [r.title_id for r in board]
    assert ranked.index(1) < ranked.index(3) < ranked.index(5), (
        "the board does not order the liked pile above the fine pile above the disliked one"
    )
    assert {r.tier for r in board} != {None} and all(0 <= r.tier <= 5 for r in board)
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='series'", user
    ) == 0, "the movie refit wrote into the series partition"

    cuts = await db.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", user
    )
    assert list(cuts["tier_set"]) == list(observations.DEFAULT_TIER_SET)
    assert len(cuts["boundaries"]) == len(observations.DEFAULT_TIER_SET) - 1
    assert list(cuts["boundaries"]) == sorted(cuts["boundaries"]), "§4.2: ordered ascending"
    assert list(cuts["boundaries"]) == pytest.approx(report.cutpoints)

    cache = await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    assert cache is not None and cache.n_observed == 6
    assert cache.title_ids.tolist() == sorted(cache.title_ids.tolist())
    # A cache built under other constants is wrong, not stale.
    assert await refit.load_cache(
        db, user_id=user, kind="movie", hp=Hyperparams(lambda_ridge=30.0), lock=False
    ) is None


async def test_every_owned_title_gets_a_coordinate_even_unrated(db, world):
    """An unobserved title has no r: s = μ + ⟨v, e⟩, with the σ it would have if never rated."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )

    rows = {r.title_id: r for r in await _board(db, user, "movie")}
    assert set(rows) == {1, 2, 3, 4, 5, 6}, "an owned film has no coordinate"
    assert [rows[t].observed for t in (1, 2, 3)] == [True, True, True]
    assert [rows[t].observed for t in (4, 5, 6)] == [False, False, False]
    assert all(np.isfinite(rows[t].s) for t in rows)
    unrated = await db.fetchrow(
        "SELECT sigma, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=4", user
    )
    assert unrated["sigma"] == pytest.approx(unrated["sigma_prior"])
    assert rows[4].sigma > rows[1].sigma, "an unrated title is not less certain than a rated one"


async def test_the_displayed_weight_is_the_cdf_of_the_persons_own_s_per_kind(db, world):
    """Per kind: §4.1 rule 5 makes films and series separate rankings."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 2), (3, 2), (4, 1), (5, 0), (6, 0)])
    await _rate(db, user, verdicts=[(7, 0), (8, 0)], duels=[(7, 8, "A")])
    for kind in ("movie", "series"):
        await refit.refit_user(
            db, user_id=user, kind=kind, hp=DEFAULTS, embeddings=fixture_embeddings
        )

    films = [r for r in await _board(db, user, "movie") if r.observed]
    series = [r for r in await _board(db, user, "series") if r.observed]
    assert films[0].cdf == pytest.approx(1.0 - 1.0 / (2 * len(films))), "best film is not ~1.0"
    assert films[-1].cdf == pytest.approx(1.0 / (2 * len(films))), "worst film is not ~0.0"
    assert series[0].cdf == pytest.approx(0.75), (
        "the better of two disliked series was placed on the films' scale"
    )
    assert series[0].s < films[0].s, "the fixture is not actually testing a per-kind reference"


async def test_freshness_inflates_sigma_eff_and_never_the_fitted_sigma(db, world):
    """Inflation is for display and queueing; `ledger_state.sigma` moves with the calendar only
    through recency (decision 564), switched off here."""
    user = world["user"]
    hp = Hyperparams(recency_half_life_days=1e12)
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1), (4, 1)])
    now = datetime.now(UTC)

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=hp, embeddings=fixture_embeddings, now=now
    )
    fresh = await db.fetchrow(
        "SELECT sigma, sigma_eff, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=1",
        user,
    )
    assert fresh["sigma_eff"] == pytest.approx(fresh["sigma"]), "inflated inside the grace period"

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=hp, embeddings=fixture_embeddings,
        now=now + timedelta(days=int(26 * refit.DAYS_PER_MONTH)),
    )
    stale = await db.fetchrow(
        "SELECT sigma, sigma_eff, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=1",
        user,
    )
    assert stale["sigma"] == pytest.approx(fresh["sigma"]), "the calendar reached the likelihood"
    assert stale["sigma_eff"] > fresh["sigma_eff"]
    assert stale["sigma_eff"] <= stale["sigma_prior"] + 1e-12, "§5.2 caps inflation at the prior σ"


async def test_a_non_finite_fit_never_reaches_a_shelf(db, world, monkeypatch):
    """Postgres sorts NaN ABOVE every real, so one poisoned title would top every shelf."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1), (4, 1)])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    good = {r.title_id: r.s for r in await _board(db, user, "movie")}

    real_fit = model.fit

    def one_bad_title(obs, hp, **kwargs):
        fit = real_fit(obs, hp, **kwargs)
        s = fit.s.copy()
        s[0] = np.nan
        return type(fit)(**{**fit.__dict__, "s": s})

    monkeypatch.setattr(refit.model, "fit", one_bad_title)
    report = await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.rejected_nonfinite == 1
    board = await _board(db, user, "movie")
    assert all(np.isfinite(r.s) for r in board), "a NaN reached ledger_state"
    assert 1 not in {r.title_id for r in board if r.observed}

    def broken_block(obs, hp, **kwargs):
        fit = real_fit(obs, hp, **kwargs)
        return type(fit)(**{**fit.__dict__, "mu": float("nan")})

    monkeypatch.setattr(refit.model, "fit", broken_block)
    with pytest.raises(refit.RefitRefused):
        await refit.refit_user(
            db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
        )
    kept = {r.title_id: r.s for r in await _board(db, user, "movie")}
    assert kept[2] == pytest.approx(good[2]), "a refused fit still overwrote the board"


async def test_refit_all_covers_every_active_person_and_both_kinds(db, world):
    """§4.1 rule 5 makes the nightly row two fits per person."""
    user = world["user"]
    other = await insert_user(db, "jenny")
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    await _rate(db, other, verdicts=[(7, 2), (8, 0)])

    reports = await refit.refit_all(db, DEFAULTS, embeddings=fixture_embeddings)
    by_key = {(r.user_id, r.kind): r for r in reports}
    assert set(by_key) == {(user, "movie"), (user, "series"), (other, "movie"), (other, "series")}
    assert by_key[(user, "movie")].fitted and by_key[(other, "series")].fitted
    assert not by_key[(user, "series")].fitted, "a fit was invented out of no observations"
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='series'", user
    ) == 0


async def test_the_incremental_block_solve_is_a_stationary_point_of_the_same_objective(db, world):
    """The residual gradient of the FULL objective must be zero at the moved titles."""
    user = world["user"]
    await _rate(
        db, user,
        verdicts=[(1, 2), (2, 1), (3, 0), (4, 1), (5, 2)],
        duels=[(1, 2, "A"), (2, 3, "A"), (4, 5, "B"), (1, 4, "TIE")],
    )
    await db.execute("UPDATE duel SET margin = 1.6 WHERE title_a = 1 AND title_b = 2")
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )

    # A duel's margin weight depends on the mean over the whole fit set.
    await observations.record_duel(
        db, user_id=user, title_a=2, title_b=5, outcome="B",
        context="tier_queue", selection="boundary", decisive=True, hp=DEFAULTS,
    )
    delta = await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[2, 5], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    assert delta.fit_source == "incremental" and not delta.refit
    assert [r.title_id for r in delta.rows] == [2, 5]

    cache = await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    residuals = np.array(
        [cache.r[int(np.flatnonzero(cache.title_ids == t)[0])] for t in loaded.obs.title_ids]
    )
    _gz, g_r, *_ = model._grad_hess(
        loaded.obs, DEFAULTS, cache.mu, cache.v, cache.gamma, cache.cuts, cache.log_nu,
        residuals, with_duels=True,
    )
    moved = [int(np.flatnonzero(loaded.obs.title_ids == t)[0]) for t in (2, 5)]
    assert np.max(np.abs(g_r[moved])) < 1e-7, (
        f"the block solve is not a stationary point of F: |g| = {np.abs(g_r[moved])}"
    )
    # And the answer it wrote is the answer it solved for.
    for row, i in zip(delta.rows, moved, strict=True):
        assert row.s == pytest.approx(cache.mu + loaded.obs.embeddings[i] @ cache.v + residuals[i])


async def test_an_old_answer_weighs_less_and_never_under_the_floor_on_both_paths(db, world):
    """Decision 564: 0.5 ** (age / 730 days), floored at 0.25, on every ordinal and duel row; the
    incremental path weighs them as the full fit does."""
    user = world["user"]
    await _rate(
        db, user,
        verdicts=[(1, 2), (2, 1), (3, 0), (4, 1), (5, 2)],
        duels=[(1, 2, "A"), (2, 3, "A"), (4, 5, "B")],
    )
    await db.execute("UPDATE verdict SET created_at = now() - interval '730 days' WHERE title_id = 1")
    await db.execute("UPDATE verdict SET created_at = now() - interval '3650 days' WHERE title_id = 2")
    await db.execute("UPDATE duel SET created_at = now() - interval '365 days' WHERE title_a = 1")
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    by_title = dict(zip(loaded.obs.title_ids[loaded.obs.ord_index].tolist(),
                        loaded.obs.ord_weight.tolist(), strict=True))
    assert by_title[1] == pytest.approx(0.5, abs=1e-4)
    assert by_title[2] == observations.RECENCY_FLOOR
    assert by_title[3] == pytest.approx(1.0, abs=1e-4)
    assert loaded.obs.duel_weight.tolist() == pytest.approx([0.5**0.5, 1.0, 1.0], abs=1e-4)

    await refit.refit_user(db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings)
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=5, outcome="B",
        context="tier_queue", selection="boundary", decisive=False, hp=DEFAULTS,
    )
    await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1, 5], hp=DEFAULTS, embeddings=fixture_embeddings,
    )
    cache = await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    residuals = np.array(
        [cache.r[int(np.flatnonzero(cache.title_ids == t)[0])] for t in loaded.obs.title_ids]
    )
    _gz, g_r, *_ = model._grad_hess(
        loaded.obs, DEFAULTS, cache.mu, cache.v, cache.gamma, cache.cuts, cache.log_nu,
        residuals, with_duels=True,
    )
    moved = [int(np.flatnonzero(loaded.obs.title_ids == t)[0]) for t in (1, 5)]
    assert np.max(np.abs(g_r[moved])) < 1e-7


async def test_a_sharpen_move_is_read_by_the_board_and_never_by_the_fit(db, world):
    """Decision 564: the move renders; the person's own placement stays the fit's evidence."""
    user = world["user"]
    await observations.record_tier_edit(db, user_id=user, title_id=1, tier=2)
    await observations.record_tier_edit(db, user_id=user, title_id=1, tier=4, via="sharpen")
    latest = await db.fetchrow(
        f"SELECT tier, via FROM ({observations.latest_tier_edit_sql()}) e WHERE e.title_id = 1", user
    )
    assert (latest["tier"], latest["via"]) == (4, "sharpen")

    loaded = await observations.load_observations(db, user_id=user, kind="movie", hp=DEFAULTS)
    assert loaded.n_tier_edits == 1 and loaded.obs.ord_level.tolist() == [2]
    _verdicts, edits, _duels, _mean = await refit._load_local(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS
    )
    assert [int(e["tier"]) for e in edits] == [2]


async def test_the_incremental_path_serves_an_undo_with_the_same_call(db, world):
    """It re-reads the observations, so a retraction and a write are the same call."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 1), (2, 1), (3, 1), (4, 1)], duels=[(1, 2, "A")])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    before = await db.fetchrow(
        "SELECT s, sigma FROM ledger_state WHERE user_id = $1 AND title_id = 1", user
    )

    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    after = (await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS, embeddings=fixture_embeddings
    )).rows[0]
    assert after.s > before["s"], "a 'liked' did not raise the title's score"
    assert after.sigma <= before["sigma"] + 1e-12, "an observation made the model less certain"

    await observations.undo(db, user_id=user, write=write)
    restored = (await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS, embeddings=fixture_embeddings
    )).rows[0]
    assert restored.s == pytest.approx(before["s"], abs=1e-6), "undo did not put the score back"
    assert restored.sigma == pytest.approx(before["sigma"], rel=1e-6)


async def test_an_undo_leaves_the_freshness_clock_where_the_observation_put_it(db, world):
    """The rate is 0.0 under the fixture's provisional constants, so `DEFAULTS` is used here."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1), (4, 1)])
    # Past §5.2's twelve-month grace period, for the whole history.
    long_ago = datetime.now(UTC) - timedelta(days=400)
    await db.execute("UPDATE verdict SET created_at = $2 WHERE user_id = $1", user, long_ago)
    now = datetime.now(UTC)

    async def clock(title_id: int = 1):
        return await db.fetchrow(
            "SELECT last_observed_at, sigma, sigma_eff, sigma_prior, observed FROM ledger_state "
            " WHERE user_id = $1 AND title_id = $2",
            user, title_id,
        )

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings, now=now
    )
    stale = await clock()
    assert stale["last_observed_at"] == long_ago
    assert stale["sigma_eff"] > stale["sigma"], (
        "the board is not inflated at thirteen months, so this fixture cannot see the defect"
    )

    # §4.2: changing your mind is an INSERT that supersedes, so the title IS touched today.
    write = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    fresh_at = await db.fetchval("SELECT created_at FROM verdict WHERE id = $1", write.row_id)
    await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS,
        embeddings=fixture_embeddings, now=now,
    )
    rerated = await clock()
    assert rerated["last_observed_at"] == fresh_at, (
        "a re-rating made now is the newest observation of the title, so it is the clock"
    )
    assert rerated["sigma_eff"] == pytest.approx(rerated["sigma"]), "inflated inside the grace"

    # ...and retracting it leaves the thirteen-month-old verdict, which is what the clock must say.
    await observations.undo(db, user_id=user, write=write)
    await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS,
        embeddings=fixture_embeddings, now=now,
    )
    restored = await clock()
    assert restored["observed"] is True, "the surviving verdict is still an observation"
    assert restored["last_observed_at"] == long_ago, (
        "the undo stamped the wall clock over a verdict it did not touch"
    )
    assert restored["sigma_eff"] > restored["sigma"], (
        "retracting a re-rating switched §5.2's inflation off for a title nobody has touched"
    )
    assert restored["sigma_eff"] == pytest.approx(stale["sigma_eff"], rel=1e-6), (
        "the board is not back where the retraction found it"
    )
    assert restored["sigma_eff"] <= restored["sigma_prior"] + 1e-12, "§5.2 caps at the prior sigma"


async def test_a_cache_from_other_hyperparameters_is_refitted_rather_than_trusted(db, world):
    """A digest mismatch is a miss: it stamps a refit request and returns no rows, never fitting inline."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    retuned = Hyperparams(lambda_ridge=30.0)

    delta = await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=retuned, embeddings=fixture_embeddings
    )
    assert delta.refit is True and delta.fit_source == refit.QUEUED
    assert delta.rows == ()
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_fit WHERE user_id=$1 AND kind='movie'", user
    ) == 0
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", user
    ) is not None


async def test_a_fit_in_another_coordinate_geometry_is_refused_and_owed_to_the_tick(db, world):
    """Another coordinate geometry is another basis, so `load_cache` refuses it and the tick refits."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    # Decision 508 widens the stamp to the scale its tiers are read on.
    assert await db.fetchval(
        "SELECT geometry FROM ledger_fit WHERE user_id = $1 AND kind = 'movie'", user
    ) == refit.LEDGER_GEOMETRY
    assert await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    assert (user, "movie") not in [(u, k) for u, k, _ in await refit.refreshes_owed(db)]

    await db.execute(
        "UPDATE ledger_fit SET geometry = 'raw' WHERE user_id = $1 AND kind = 'movie'", user
    )
    assert await refit.load_cache(
        db, user_id=user, kind="movie", hp=DEFAULTS, lock=False
    ) is None
    assert (user, "movie") in [(u, k) for u, k, _ in await refit.refreshes_owed(db)]

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    assert (user, "movie") not in [(u, k) for u, k, _ in await refit.refreshes_owed(db)]


async def test_one_verdict_on_an_off_scale_coordinate_does_not_decide_the_board(db, served):
    """A Cold Tower placement sits at ||ê|| ~30-80, so a raw
    read inflated s and σ; directions share one scale."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (9, 'movie', 'Off Scale', true)"
    )
    await db.execute(
        """
        INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                     tower_sha256, input_dim, blocks_present, blocks_dropped,
                                     blocks_imputed, nnz)
        VALUES (9, $1, $2, 0.3, 'sha-contract', 'sha-tower', 131,
                ARRAY['genre'], ARRAY[]::text[], ARRAY[]::text[], 7)
        """,
        BUNDLE, bb.pack_vec(cold_vector(9) * 78.0),
    )
    user = served["user"]
    await _rate(
        db, user,
        verdicts=[(9, 2), (1, 2), (2, 2), (3, 1), (4, 1), (5, 0)],
        duels=[(1, 2, "A"), (1, 3, "A"), (1, 4, "A"), (1, 5, "A"), (2, 3, "A")],
    )
    backbone = served["backbone"]

    async def raw_coordinates(title_ids):
        """What `standard_embeddings` returned before decision 471: e(t) itself."""
        coords = await serve.coordinates(db, backbone, bundle_version=BUNDLE, kind="movie")
        ids = [int(t) for t in title_ids]
        matrix = np.zeros((len(ids), 64))
        for i, title_id in enumerate(ids):
            if title_id in coords:
                matrix[i] = coords[title_id].e
        return matrix, np.asarray([t in coords for t in ids])

    async def board():
        rows = await db.fetch(
            "SELECT title_id, s, sigma_prior FROM ledger_state WHERE user_id = $1 AND observed",
            user,
        )
        return {r["title_id"]: (float(r["s"]), float(r["sigma_prior"])) for r in rows}

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=raw_coordinates,
        bundle_version=BUNDLE,
    )
    raw = await board()
    others = [sp for t, (_s, sp) in raw.items() if t != 9]
    assert raw[9][1] > 10.0 * max(others), f"the fixture does not reproduce the off-scale σ: {raw}"
    assert max(raw, key=lambda t: raw[t][0]) == 9, "and the one-verdict title is not on top"

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS,
        embeddings=observations.standard_embeddings(db, backbone, bundle_version=BUNDLE),
        bundle_version=BUNDLE,
    )
    read = await board()
    # On one scale: tau (2.0) plus a bounded (mu, v) part, against 44 read raw.
    assert max(sp for _s, sp in read.values()) < 1.5 * DEFAULTS.b_i_tau, read
    assert read[9][1] < 1.5 * min(sp for _s, sp in read.values()), read
    assert max(read, key=lambda t: read[t][0]) == 1, (
        f"the title that won every duel is not first: {sorted(read, key=lambda t: -read[t][0])}"
    )


BUDGET_TITLES = 900          # §1: "Ledger refit for 2 users over 839+ titles — seconds"
BUDGET_VERDICTS = 100        # §5.2: "Aim for 50-100 in the first sitting or two"
BUDGET_DUELS = 300
BUDGET_TIER_EDITS = 20


async def _rate_at_scale(db, user, pool, rng, taste, *, n_verdicts, n_duels, n_edits):
    """Straight through SQL, so the fixture's cost stays outside the measurement."""
    truth = {int(t): float(_embedding(int(t)) @ taste) for t in pool}
    rated = rng.choice(pool, size=min(n_verdicts, len(pool)), replace=False)
    await db.execute(
        """
        INSERT INTO verdict (user_id, title_id, value)
        SELECT $1, x.title_id, x.value FROM unnest($2::int[], $3::smallint[]) AS x(title_id, value)
        """,
        user,
        [int(t) for t in rated],
        # Spread across three classes: a 60% "liked" fixture measures a labeller the spec warns about.
        [int(np.searchsorted([-0.002, 0.002], truth[int(t)])) for t in rated],
    )
    pairs = rng.choice(rated, size=(n_duels, 2))
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    await db.execute(
        """
        INSERT INTO duel (user_id, title_a, title_b, outcome, margin, context, selection)
        SELECT $1, x.a, x.b, x.outcome, x.margin, 'profile_battle', 'random'
        FROM unnest($2::int[], $3::int[], $4::text[], $5::real[]) AS x(a, b, outcome, margin)
        """,
        user,
        [int(a) for a, _ in pairs],
        [int(b) for _, b in pairs],
        [
            "A" if truth[int(a)] > truth[int(b)] + 1e-3
            else "B" if truth[int(b)] > truth[int(a)] + 1e-3 else "TIE"
            for a, b in pairs
        ],
        [1.6 if i % 3 == 0 else 1.0 for i in range(len(pairs))],
    )
    edited = rng.choice(rated, size=min(n_edits, len(rated)), replace=False)
    await db.execute(
        """
        INSERT INTO tier_edit (user_id, title_id, tier, via)
        SELECT $1, x.title_id, x.tier, 'drag_drop'
        FROM unnest($2::int[], $3::smallint[]) AS x(title_id, tier)
        """,
        user,
        [int(t) for t in edited],
        [int(np.clip(3 + round(truth[int(t)] * 400), 0, 5)) for t in edited],
    )


async def _big_world(
    db,
    *,
    n_titles=BUDGET_TITLES,
    users=("patrick", "jenny"),
    n_verdicts=BUDGET_VERDICTS,
    n_duels=BUDGET_DUELS,
    n_edits=BUDGET_TIER_EDITS,
):
    """Deterministic, and both kinds, since the nightly job is two fits per person."""
    specs = [(i, "movie" if i % 3 else "series", f"Title {i}") for i in range(1, n_titles + 1)]
    await make_titles(db, specs)
    by_kind = {
        kind: [i for i, k, _ in specs if k == kind] for kind in ("movie", "series")
    }
    ids = []
    for index, name in enumerate(users):
        user = await insert_user(db, name, "admin" if index == 0 else "member")
        ids.append(user)
        rng = np.random.default_rng(20 + index)
        taste = _embedding(7 + index)
        for kind, share in (("movie", 1.0), ("series", 0.4)):
            await _rate_at_scale(
                db, user, by_kind[kind], rng, taste,
                n_verdicts=max(4, int(n_verdicts * share)),
                n_duels=max(4, int(n_duels * share)),
                n_edits=max(2, int(n_edits * share)),
            )
    return ids


async def test_a_full_map_refit_of_both_users_over_the_owned_library_lands_inside_the_budget(db):
    """60 s for the household is a generous "seconds"; the measured number is printed."""
    users = await _big_world(db)
    started = time.perf_counter()
    reports = await refit.refit_all(db, DEFAULTS, embeddings=fixture_embeddings)
    elapsed = time.perf_counter() - started
    print(f"\nfull refit, {len(users)} users x 2 kinds x {BUDGET_TITLES} titles: {elapsed:.2f} s")
    for report in reports:
        print(f"  {report.as_dict()}")

    fitted = [r for r in reports if r.fitted]
    assert len(fitted) == 4, [r.as_dict() for r in reports]
    assert all(r.converged for r in fitted), "a nightly fit did not converge"
    assert all(len(r.cutpoints) == 5 and r.cutpoints == sorted(r.cutpoints) for r in fitted)
    assert elapsed < 60.0, f"§5.3's nightly refit took {elapsed:.1f} s"

    for user in users:
        for kind in ("movie", "series"):
            rows = await _board(db, user, kind)
            owned = await db.fetchval(
                "SELECT count(*) FROM title WHERE is_owned AND kind = $1", kind
            )
            assert len(rows) == owned, "§12: every owned title must have a coordinate"
            assert all(np.isfinite(r.s) and r.sigma > 0 for r in rows)


async def test_an_incremental_update_lands_inside_the_fifty_millisecond_budget(db):
    """Measured end to end on Postgres; an incremental update must touch one or two titles."""
    users = await _big_world(db, users=("patrick",))
    user = users[0]
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    unrated = [
        int(r["id"])
        for r in await db.fetch(
            """
            SELECT t.id FROM title t
            WHERE t.kind = 'movie' AND NOT EXISTS (
                SELECT 1 FROM verdict v WHERE v.user_id = $1 AND v.title_id = t.id)
            ORDER BY t.id LIMIT 60
            """,
            user,
        )
    ]

    timings: list[float] = []
    for index, title_id in enumerate(unrated):
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=index % 3)
        started = time.perf_counter()
        delta = await refit.update_incrementally(
            db, user_id=user, kind="movie", title_ids=[title_id], hp=DEFAULTS,
            embeddings=fixture_embeddings,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert not delta.refit, "the cache went cold mid-run; this is not the incremental path"
        assert len(delta.rows) == 1, "an incremental update touched more than the title it was given"
        if index >= 10:                       # the first ten warm the connection and the caches
            timings.append(elapsed_ms)

    median = statistics.median(timings)
    print(
        f"\nincremental update over {BUDGET_TITLES} titles / "
        f"{BUDGET_VERDICTS} verdicts: median {median:.1f} ms, "
        f"p95 {sorted(timings)[int(0.95 * len(timings))]:.1f} ms, max {max(timings):.1f} ms"
    )
    assert median < 50.0, f"§5.3's <50 ms budget: median was {median:.1f} ms"


async def test_the_incremental_cost_does_not_grow_with_the_library(db):
    """The library grows 15x and the observation count stays fixed, so a re-solve would show."""
    ratios = []
    for n_titles in (60, BUDGET_TITLES):
        users = await _big_world(
            db, n_titles=n_titles, users=(f"p{n_titles}",),
            n_verdicts=35, n_duels=100, n_edits=6,
        )
        user = users[0]
        await refit.refit_user(
            db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
        )
        # Films only: this loop calls the movie board, and 0022's composite FK refuses a series row.
        rated = [
            int(r["title_id"])
            for r in await db.fetch(
                "SELECT DISTINCT v.title_id FROM verdict v JOIN title t ON t.id = v.title_id "
                "WHERE v.user_id = $1 AND t.kind = 'movie' ORDER BY v.title_id LIMIT 25",
                user,
            )
        ]
        samples = []
        for index, title_id in enumerate(rated):
            await observations.record_verdict(
                db, user_id=user, title_id=title_id, value=(index + 1) % 3
            )
            started = time.perf_counter()
            await refit.update_incrementally(
                db, user_id=user, kind="movie", title_ids=[title_id], hp=DEFAULTS,
                embeddings=fixture_embeddings,
            )
            if index >= 5:
                samples.append((time.perf_counter() - started) * 1000.0)
        ratios.append(statistics.median(samples))
        await db.execute("DELETE FROM app_user WHERE id = $1", user)
        await db.execute("DELETE FROM title")

    small, large = ratios
    print(f"\nincremental median: {small:.1f} ms at 60 titles, {large:.1f} ms at {BUDGET_TITLES}")
    # Measured at 1.2x for a 15x library.
    assert large / small < 2.5, (
        f"the incremental cost grew {large / small:.1f}x for a {BUDGET_TITLES / 60:.0f}x "
        "library — something is re-solving the whole fit"
    )
