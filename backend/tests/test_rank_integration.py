from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from spielplan.db import library
from spielplan.home import rail
from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import drop, evaluation, queue, read, tiers
from tests.helpers import insert_user

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"


def _embedding(title_id: int) -> np.ndarray:
    rng = np.random.default_rng(1000 + title_id)
    vector = rng.normal(size=64)
    return vector / (np.linalg.norm(vector) * 8.0)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    return np.stack([_embedding(t) for t in ids]), np.ones(len(ids), dtype=bool)


@pytest.fixture
async def world(db):
    """Two people: decision 11's "never touches another's" cannot fail with one."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT x.id, x.kind, x.name, x.year, x.runtime, true
        FROM unnest($1::int[], $2::text[], $3::text[], $4::int[], $5::int[])
             AS x(id, kind, name, year, runtime)
        """,
        list(range(1, 13)),
        ["movie"] * 10 + ["series"] * 2,
        [f"Title {i}" for i in range(1, 13)],
        [1995 + (i % 3) * 10 for i in range(1, 13)],
        [90 + (i * 13) % 80 for i in range(1, 13)],
    )
    return {
        "patrick": await insert_user(db, "patrick", "admin"),
        "jenny": await insert_user(db, "jenny"),
    }


async def rate(db, user, *, verdicts=(), duels=(), tier_edits=()):
    for title_id, value in verdicts:
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)
    for a, b, outcome in duels:
        await observations.record_duel(
            db, user_id=user, title_a=a, title_b=b, outcome=outcome,
            context="profile_battle", decisive=False, hp=DEFAULTS,
        )
    for title_id, tier in tier_edits:
        await observations.record_tier_edit(db, user_id=user, title_id=title_id, tier=tier)


async def fitted(db, user, kind="movie"):
    return await refit.refit_user(
        db, user_id=user, kind=kind, hp=DEFAULTS, embeddings=fixture_embeddings
    )


@pytest.fixture
async def board_of(db, world):
    """A fitted movie board for Patrick: six verdicts across all three classes, four duels."""
    user = world["patrick"]
    await rate(
        db, user,
        verdicts=[(1, 2), (2, 2), (3, 1), (4, 1), (5, 0), (6, 0)],
        duels=[(1, 2, "A"), (3, 4, "TIE"), (5, 6, "B"), (1, 5, "A")],
    )
    report = await fitted(db, user)
    assert report.fitted, report.as_dict()
    return user


@pytest.fixture
async def sandwich(db, board_of):
    """The neighbours must be in the target tier: the client reads them off the rendered row, and
    `drop` checks it (finding 18)."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=5)
    await drop.drop(db, user_id=board_of, title_id=2, tier=5)
    return board_of


async def test_dropping_into_a_tier_writes_one_tier_edit_and_no_duel(db, board_of):
    result = await drop.drop(db, user_id=board_of, title_id=3, tier=5)

    edits = await db.fetch("SELECT title_id, tier, via FROM tier_edit WHERE user_id = $1", board_of)
    assert [(r["title_id"], r["tier"], r["via"]) for r in edits] == [(3, 5, "drag_drop")]
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'", board_of
    ) == 0
    assert result.neighbour_duels == 0


async def test_dropping_between_two_titles_writes_the_edit_and_two_margin_less_duels(db, sandwich):
    board_of = sandwich
    result = await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2)

    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id=$1 AND title_id=4 AND via='drag_drop'",
        board_of,
    ) == 1
    rows = await db.fetch(
        "SELECT title_a, title_b, outcome, margin, context, selection FROM duel "
        "WHERE user_id = $1 AND context = 'tier_insert' ORDER BY id",
        board_of,
    )
    assert len(rows) == 2 == result.neighbour_duels
    for row in rows:
        assert row["margin"] is None, "§6.3 says margin-less, and NULL is what that means"
        assert row["context"] == "tier_insert"
        # `selection` describes adaptive selection; a drop keeps the column default.
        assert row["selection"] == "random"


async def test_the_neighbour_duels_carry_the_placement_and_not_just_the_geometry(db, sandwich):
    """Written without outcomes the duels would record that a comparison happened, not what it said."""
    board_of = sandwich
    await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2)
    rows = await db.fetch(
        "SELECT title_a, title_b, outcome FROM duel WHERE user_id=$1 AND context='tier_insert' "
        "ORDER BY id",
        board_of,
    )
    assert (rows[0]["title_a"], rows[0]["title_b"], rows[0]["outcome"]) == (1, 4, "A")
    assert (rows[1]["title_a"], rows[1]["title_b"], rows[1]["outcome"]) == (4, 2, "A")


async def test_a_drop_at_the_end_of_a_tier_writes_the_one_duel_that_exists(db, sandwich):
    """One neighbour is one duel: refusing would make the end slots unreachable."""
    board_of = sandwich
    await drop.drop(db, user_id=board_of, title_id=4, tier=5, below=2)
    rows = await db.fetch(
        "SELECT title_a, title_b FROM duel WHERE user_id=$1 AND context='tier_insert'", board_of
    )
    assert [(r["title_a"], r["title_b"]) for r in rows] == [(4, 2)]


async def test_a_drop_is_one_transaction(db, sandwich):
    """A refused neighbour must leave no tier_edit behind."""
    board_of = sandwich
    before = await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id=$1", board_of)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=99, above=1, below=2)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=4)
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id=$1", board_of) == before


async def test_a_drop_naming_a_neighbour_that_is_not_in_the_target_tier_is_refused(db, board_of):
    """A stale board (second tab, nightly refit) names titles that have moved, and the duels are
    append-only, so the route checks where the neighbours actually are."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=5)
    await drop.drop(db, user_id=board_of, title_id=2, tier=5)
    duels = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'"
    before = await db.fetchval(duels, board_of)

    await drop.drop(db, user_id=board_of, title_id=2, tier=0)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2)
    assert await db.fetchval(duels, board_of) == before
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND title_id = 4", board_of
    ) == 0, "and the tier edit goes with the duels — the drop is one gesture"

    accepted = await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1)
    assert accepted.neighbour_duels == 1


async def test_a_drop_naming_a_neighbour_that_is_not_on_the_board_is_refused(db, board_of):
    """Title 7 is owned and placed but rated by nobody, so it is in no tier."""
    assert 7 not in {i.title_id for i in await read.items(db, user_id=board_of, kind="movie")}
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=7)


async def test_a_drop_under_an_active_filter_writes_the_edit_and_no_neighbour_duels(
    db, board_of
):
    """Decision 204: on a filtered board "between" is about the screen, so no neighbour duels."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=5)
    await drop.drop(db, user_id=board_of, title_id=2, tier=5)
    duels = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'"
    before = await db.fetchval(duels, board_of)

    result = await drop.drop(
        db, user_id=board_of, title_id=4, tier=5, above=1, below=2, filtered=True
    )
    assert result.neighbour_duels == 0
    assert await db.fetchval(duels, board_of) == before
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND title_id = 4", board_of
    ) == 1
    assert "margin-less duels" not in result.log, (
        "§6.7's line narrates what was written, so it cannot promise duels that were not"
    )


async def test_the_board_renders_the_drop_and_does_not_snap_it_back(db, board_of):
    await drop.drop(db, user_id=board_of, title_id=5, tier=6)
    await fitted(db, board_of)

    tiers, cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    placed = {e.title_id: e for t in tiers for e in t.entries}
    assert placed[5].tier == 6 == placed[5].assigned_tier
    assert cuts.tier_set[6] == "S"


async def test_a_drop_narrates_itself_with_the_number_of_duels_it_wrote(db, sandwich):
    board_of = sandwich
    both = await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2,
                           title_name="Drive")
    assert both.log == (
        "tier_edit(Drive → A+, via=drag_drop) + 2 margin-less duels vs new neighbours"
    )
    alone = await drop.drop(db, user_id=board_of, title_id=3, tier=0, title_name="Heat")
    assert alone.log == "tier_edit(Heat → F, via=drag_drop)"


async def test_tap_to_tier_writes_exactly_what_the_pointer_path_writes(db, board_of):
    """Same function, same `via`: no second write path to drift."""
    await drop.drop(db, user_id=board_of, title_id=3, tier=4, above=None, below=None)
    await drop.drop(db, user_id=board_of, title_id=4, tier=4, above=None, below=None)
    rows = await db.fetch(
        "SELECT title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )
    assert [r["via"] for r in rows] == ["drag_drop", "drag_drop"]
    # `{above: null, below: null}` is what a tap and a drop on empty row space post (finding 17).
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id=$1 AND context='tier_insert'", board_of
    ) == 0, "a tap into a tier invents no comparison"


async def test_ledger_cutpoints_is_keyed_by_user_and_kind(db, world):
    columns = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'ledger_cutpoints'"
        )
    }
    assert {"user_id", "kind", "boundaries", "tier_set", "refit_requested_at"} <= columns
    key = await db.fetch(
        """
        SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'ledger_cutpoints'::regclass AND i.indisprimary
        """
    )
    assert {r["attname"] for r in key} == {"user_id", "kind"}


async def test_a_boundary_list_that_does_not_match_the_tier_set_is_refused(db, world):
    """Enforced by the database, so no write path can index past its own labels."""
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
            "VALUES ($1, 'movie', ARRAY[0.0, 1.0], ARRAY['F','D','C','B'])",
            world["patrick"],
        )


async def test_saving_a_new_tier_set_reinitialises_to_equal_mass_quantiles(db, board_of):
    """The measured F3/D7/C15 shape is authored for K = 7 only."""
    report = await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])
    assert report.k_changed and report.initialised["movie"] == "quantile"

    row = await db.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'",
        board_of,
    )
    assert list(row["tier_set"]) == ["bad", "ok", "good"]
    assert len(row["boundaries"]) == 2, "§4.2: length = |tier set| - 1"

    s = np.asarray(
        [
            float(r["s"])
            for r in await db.fetch(
                "SELECT s FROM ledger_state WHERE user_id=$1 AND kind='movie' AND observed",
                board_of,
            )
        ]
    )
    assert np.allclose(list(row["boundaries"]), np.quantile(s, [1 / 3, 2 / 3]))


async def test_saving_a_new_tier_set_queues_a_refit_for_that_user_alone(db, board_of, world):
    """Queued, not run: a full MAP refit takes seconds, beyond a settings save's budget."""
    await rate(db, world["jenny"], verdicts=[(1, 2), (2, 0)])
    await fitted(db, world["jenny"])
    await tiers.save_tier_set(db, user_id=world["jenny"], tier_set=["F", "D", "C", "B", "A", "A+", "S"])

    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    owed = await tiers.refits_owed(db)
    assert {user for user, _kind, _at in owed} == {board_of}
    assert {kind for _user, kind, _at in owed} == {"movie", "series"}

    movie = next(at for _user, kind, at in owed if kind == "movie")
    await tiers.clear_refit_request(db, user_id=board_of, kind="movie", requested_at=movie)
    assert [k for u, k, _at in await tiers.refits_owed(db) if u == board_of] == ["series"]


async def test_a_relabel_at_the_same_size_keeps_the_learned_boundaries(db, board_of):
    """Renaming at the same K keeps a fitted board."""
    before = list(
        await db.fetchval(
            "SELECT boundaries FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", board_of
        )
    )
    report = await tiers.save_tier_set(
        db, user_id=board_of, tier_set=["E", "D", "C", "B", "A", "A+", "S"]
    )
    after = list(
        await db.fetchval(
            "SELECT boundaries FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", board_of
        )
    )
    assert not report.k_changed and not report.refit_queued
    assert report.initialised["movie"] == "kept"
    assert after == before
    assert await tiers.refits_owed(db) == []


async def test_saving_a_new_tier_set_leaves_the_tier_edit_rows_intact(db, board_of):
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    await drop.drop(db, user_id=board_of, title_id=5, tier=0)
    before = await db.fetch(
        "SELECT id, title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )

    report = await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])
    after = await db.fetch(
        "SELECT id, title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert report.tier_edits_kept == len(before)


async def test_a_shrunk_tier_set_still_fits_and_the_old_edits_still_count(db, board_of):
    """`load_observations` clamps an edit's level past the new K rather than raising."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    report = await fitted(db, board_of)
    assert report.fitted and report.n_tier_edits == 1
    assert len(report.cutpoints) == 2
    top = await db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id=$1 AND title_id=1", board_of
    )
    assert 0 <= top <= 2

    # The board must survive it too: `board._band` indexed a shrunk cutpoint array (a 500).
    rendered, cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert [t.label for t in rendered] == ["good", "ok", "bad"]
    placed = {e.title_id: e for t in rendered for e in t.entries}
    assert placed[1].tier == len(cuts.tier_set) - 1, "the top tier they had is the top they have"
    assert placed[1].assigned_tier == placed[1].tier


async def test_a_tier_set_change_invalidates_the_fit_rather_than_leaving_the_old_k(db, board_of):
    """The cutpoints in `theta` index a tier set of one LENGTH, so a fit at the old K means something
    else. Refused in `load_cache`, not by a DELETE on the settings path."""
    assert await refit.load_cache(db, user_id=board_of, kind="movie", hp=DEFAULTS, lock=False)

    # A relabel at the same K invalidates nothing: the boundaries still mean what they meant.
    await tiers.save_tier_set(db, user_id=board_of, tier_set=list("ABCDEFG"))
    kept = await refit.load_cache(db, user_id=board_of, kind="movie", hp=DEFAULTS, lock=False)
    assert kept is not None and kept.n_levels == 7, "a relabel is not a change of basis"

    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"T{i}" for i in range(12)])
    assert await refit.load_cache(
        db, user_id=board_of, kind="movie", hp=DEFAULTS, lock=False
    ) is None, "a fit at the old K is not stale, it means something else"

    # The refusal is read-side, so the row is still there to be re-fitted.
    await db.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL WHERE user_id = $1", board_of
    )
    await observations.record_verdict(db, user_id=board_of, title_id=7, value=2)
    delta = await refit.update_incrementally(
        db, user_id=board_of, kind="movie", title_ids=[7], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    assert (delta.fit_source, delta.refit, delta.rows) == (refit.QUEUED, True, ())
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'",
        board_of,
    ) is not None
    assert await db.fetchval("SELECT count(*) FROM ledger_fit WHERE user_id=$1", board_of) == 1

    report = await fitted(db, board_of)
    assert report.fitted and len(report.cutpoints) == 11
    refitted = await refit.load_cache(db, user_id=board_of, kind="movie", hp=DEFAULTS, lock=False)
    assert refitted is not None and refitted.n_levels == 12


async def test_the_loader_the_incremental_path_and_the_board_rescale_through_one_helper(
    db, board_of
):
    """One `observations.rescale_level` for loader, incremental path and board. The incremental half
    runs twice from one deterministic cache, `n_levels` 7 and a lying 12, as the control."""
    assert observations.rescale_level(6, k_from=7, k_to=12) == 11, (
        "the rescale a preserved tier_edit row takes on the incremental path is not the identity"
    )
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    assert await db.fetchval(
        "SELECT n_levels FROM tier_edit WHERE user_id=$1 AND title_id=1", board_of
    ) == 7

    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"T{i}" for i in range(12)])
    report = await fitted(db, board_of)
    assert report.fitted and report.n_tier_edits == 1

    loaded = await observations.load_observations(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    levels = [
        int(level)
        for level, arm in zip(loaded.obs.ord_level, loaded.obs.ord_arm, strict=True)
        if int(arm) == observations.ARM_TIER
    ]
    assert levels == [11], "the loader still reads the drop as level 6 of a 12-level set"

    rendered, cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert len(cuts.tier_set) == 12
    placed = {entry.title_id: entry for tier in rendered for entry in tier.entries}
    assert placed[1].assigned_tier == 11
    assert placed[1].tier == 11, "the bucket and the badge disagree about the drop"

    async def incremental_s() -> float:
        await refit.update_incrementally(
            db, user_id=board_of, kind="movie", title_ids=[1], hp=DEFAULTS,
            embeddings=fixture_embeddings,
        )
        return float(
            await db.fetchval(
                "SELECT s FROM ledger_state WHERE user_id=$1 AND kind='movie' AND title_id=1",
                board_of,
            )
        )

    honest = await incremental_s()
    await fitted(db, board_of)          # deterministic full fit: the cache is back where it was
    await db.execute(
        "UPDATE tier_edit SET n_levels = 12 WHERE user_id = $1 AND title_id = 1", board_of
    )
    lying = await incremental_s()
    print(f"\nincremental s for the dropped title: {honest:.4f} read at 11, {lying:.4f} read at 6")
    assert honest > lying, (
        "the incremental path is not reading the K the edit was written under: a top-tier drop and "
        "a mid-board one produced the same score"
    )


async def test_a_drop_beside_a_pre_k_change_neighbour_is_checked_at_the_rendered_tier(
    db, board_of
):
    """`drop._tiers_of` must read the level the board renders; the clamp and the map only diverge
    once K changes."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"T{i}" for i in range(12)])
    assert (await fitted(db, board_of)).fitted

    rendered, _cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    at = {entry.title_id: tier.index for tier in rendered for entry in tier.entries}
    assert at[1] == 11, "the board no longer renders the pre-K-change edit where this test assumes"

    result = await drop.drop(db, user_id=board_of, title_id=2, tier=at[1], above=1)
    assert result.neighbour_duels == 1, "the neighbour duel §6.3 asks for was not written"
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id=$1 AND context='tier_insert' "
        "AND title_a = 1 AND title_b = 2",
        board_of,
    ) == 1

    # Only an API call can send the stale level, but duels are append-only, so it is refused.
    with pytest.raises(drop.DropRefused, match="not in T6 any more"):
        await drop.drop(db, user_id=board_of, title_id=3, tier=6, above=1)


@pytest.fixture
async def another_request(db, pg_url):
    """A second connection: a fit's transaction opens before it reads, so a writer sharing its
    connection would roll back with a refused fit."""
    import json as _json

    import asyncpg

    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=_json.dumps, decoder=_json.loads, schema="pg_catalog"
        )
    try:
        yield conn
    finally:
        await conn.close()


async def test_a_refit_cannot_overwrite_a_tier_set_it_did_not_fit_against(
    db, board_of, monkeypatch, another_request
):
    """The fit's tier-set write is a compare-and-set: a PUT landing mid-fit must not be reverted."""
    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"K{i}" for i in range(5)])
    real = observations.load_observations

    async def racing(conn, **kwargs):
        loaded = await real(conn, **kwargs)
        # On its own connection: a save on the fit's connection joins its transaction.
        if kwargs["kind"] == "movie" and len(loaded.tier_set) == 5:
            await tiers.save_tier_set(
                another_request, user_id=board_of, tier_set=[f"L{i}" for i in range(9)]
            )
        return loaded

    monkeypatch.setattr(observations, "load_observations", racing)
    with pytest.raises(refit.RefitRefused):
        await fitted(db, board_of)
    monkeypatch.undo()

    rows = await db.fetch(
        "SELECT kind, tier_set, boundaries, refit_requested_at FROM ledger_cutpoints "
        "WHERE user_id = $1 ORDER BY kind",
        board_of,
    )
    assert [len(r["tier_set"]) for r in rows] == [9, 9], "both kinds keep the set last chosen"
    assert all(len(r["boundaries"]) == 8 for r in rows), "§4.2: length = |tier set| - 1"
    assert all(r["refit_requested_at"] is not None for r in rows), (
        "and the fit that refused leaves the request owed, so the next sweep fits the new set"
    )


async def test_a_refit_request_made_during_a_fit_survives_the_sweep_that_did_not_fit_it(
    db, board_of
):
    """The request stamp bounds the clear, which is why `refits_owed` returns three values."""
    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])
    owed = {(user, kind): at for user, kind, at in await tiers.refits_owed(db)}
    assert set(owed) == {(board_of, "movie"), (board_of, "series")}
    fitted_against = owed[(board_of, "movie")]

    # `now()` is the transaction timestamp and two transactions can share a microsecond, so the
    # later stamp is set explicitly.
    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"L{i}" for i in range(9)])
    await db.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = $2 WHERE user_id = $1",
        board_of,
        fitted_against + timedelta(seconds=1),
    )

    await tiers.clear_refit_request(
        db, user_id=board_of, kind="movie", requested_at=fitted_against
    )
    still = {kind for user, kind, _at in await tiers.refits_owed(db) if user == board_of}
    assert still == {"movie", "series"}, "a request the sweep did not fit is still owed"

    current = {
        (user, kind): at for user, kind, at in await tiers.refits_owed(db)
    }[(board_of, "movie")]
    await tiers.clear_refit_request(db, user_id=board_of, kind="movie", requested_at=current)
    assert [kind for user, kind, _at in await tiers.refits_owed(db) if user == board_of] == [
        "series"
    ]


async def test_the_tier_set_is_read_for_the_kind_that_is_asked(db, board_of):
    await db.execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
        "VALUES ($1, 'series', $2::float8[], $3::text[])",
        board_of,
        [-0.5, 0.5],
        ["low", "mid", "high"],
    )
    assert await tiers.tier_set_of(db, user_id=board_of, kind="series") == (
        "low",
        "mid",
        "high",
    )
    assert len(await tiers.tier_set_of(db, user_id=board_of, kind="movie")) == 7


async def test_a_drop_resolves_the_tier_set_of_the_titles_own_kind(db, board_of):
    await rate(db, board_of, verdicts=[(11, 2), (12, 0)])
    await fitted(db, board_of, "series")
    await tiers.save_tier_set(db, user_id=board_of, tier_set=[f"L{i}" for i in range(9)])
    # A series drop into the eighth of nine tiers is legal, and was refused when films reverted.
    assert (await drop.drop(db, user_id=board_of, title_id=11, tier=7)).kind == "series"

    await db.execute(
        "UPDATE ledger_cutpoints SET tier_set = $2::text[], boundaries = $3::float8[] "
        "WHERE user_id = $1 AND kind = 'series'",
        board_of,
        [f"K{i}" for i in range(5)],
        [-1.0, -0.3, 0.3, 1.0],
    )

    film = await drop.drop(db, user_id=board_of, title_id=1, tier=7)
    assert (film.kind, film.tier) == ("movie", 7)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=11, tier=7)
    assert (await drop.drop(db, user_id=board_of, title_id=11, tier=4)).kind == "series"


async def test_one_persons_tier_set_never_touches_anothers(db, board_of, world):
    jenny = world["jenny"]
    await rate(db, jenny, verdicts=[(1, 2), (2, 1), (3, 0)])
    await fitted(db, jenny)
    before = await db.fetch(
        "SELECT kind, boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 ORDER BY kind",
        jenny,
    )

    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    after = await db.fetch(
        "SELECT kind, boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 ORDER BY kind",
        jenny,
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert [r for r in await tiers.refits_owed(db) if r[0] == jenny] == []


async def test_a_tier_set_the_board_could_not_render_is_refused(db, board_of):
    for bad in (
        [],
        ["only"],
        ["A", "A"],
        ["A", ""],
        [f"T{i}" for i in range(20)],
        # Past §6.7's 400-character rail limit, which `rail.record` enforces by raising after the commit.
        ["A" * 400, "B", "C"],
    ):
        with pytest.raises(tiers.TierSetRefused):
            await tiers.save_tier_set(db, user_id=board_of, tier_set=bad)


async def test_a_held_out_pair_is_stored_with_its_own_discriminator(db, board_of):
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    rows = await db.fetch(
        "SELECT context, selection FROM duel WHERE user_id=$1 AND selection='uniform_holdout'",
        board_of,
    )
    assert [(r["context"], r["selection"]) for r in rows] == [("tier_queue", "uniform_holdout")]


async def test_the_selector_never_counts_a_held_out_comparison(db, board_of):
    """`queue._exploration` picks the least-compared title, so this count is a selector input."""
    for _ in range(5):
        await observations.record_duel(
            db, user_id=board_of, title_a=1, title_b=2, outcome="A",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=3, outcome="A",
        context="tier_queue", selection=queue.ARM_BOUNDARY,
    )

    counts = await read.comparison_counts(db, user_id=board_of, kind="movie")
    # Four profile battles from the fixture (1v2, 3v4, 5v6, 1v5) plus the one boundary pair.
    assert counts[1] == 3, "the five held-out pairs on title 1 must be invisible here"
    assert counts[2] == 1
    assert counts[3] == 2


async def test_the_held_out_stream_never_reaches_the_fit(db, board_of):
    before = await fitted(db, board_of)
    for a, b in ((1, 2), (2, 3), (3, 4)):
        await observations.record_duel(
            db, user_id=board_of, title_a=a, title_b=b, outcome="A",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )
    after = await fitted(db, board_of)
    assert after.n_duels == before.n_duels
    assert after.n_held_out == 3
    assert np.isclose(after.objective, before.objective)


async def test_the_evaluation_read_path_admits_only_held_out_rows(db, board_of):
    """The adaptive pairs agree with the model, so a leak shows as a higher rate, not an error."""
    order = [r["title_id"] for r in await db.fetch(
        "SELECT title_id FROM ledger_state WHERE user_id = $1 AND kind = 'movie'"
        " ORDER BY s DESC, title_id",
        board_of,
    )]
    best, worst = order[0], order[-1]

    # Ten adaptive pairs the model gets right, and two held-out pairs it gets wrong.
    for _ in range(10):
        await observations.record_duel(
            db, user_id=board_of, title_a=best, title_b=worst, outcome="A",
            context="tier_queue", selection=queue.ARM_BOUNDARY,
        )
    for _ in range(2):
        await observations.record_duel(
            db, user_id=board_of, title_a=best, title_b=worst, outcome="B",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )

    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 2, "the ten adaptive pairs are not evaluation data"
    assert agreement.decisive == 2 and agreement.agreed == 0
    assert agreement.rate == 0.0


async def test_the_evaluation_reports_nothing_rather_than_zero_when_nothing_is_held_out(db, board_of):
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 0
    assert agreement.rate is None, "a held-out sample of zero is 'not measured', not 'terrible'"


async def test_a_tie_is_counted_and_not_scored(db, board_of):
    """Scoring a tie needs a |Δs| threshold nothing has measured."""
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="TIE",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert (agreement.pairs, agreement.ties, agreement.decisive) == (1, 1, 0)
    assert agreement.rate is None


@pytest.mark.parametrize(
    ("arm", "phrase"),
    [
        (queue.ARM_BOUNDARY, "boundary-targeted"),
        (queue.ARM_EXPLORATION, "exploration"),
        (queue.ARM_HOLDOUT, "uniform-random, held out"),
    ],
)
async def test_a_queue_answer_stores_its_arm_and_the_log_line_names_the_same_one(
    db, board_of, arm, phrase
):
    """The prototype named the boundary arm unconditionally, so every tenth line lied (proposal 120)."""
    write = await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=arm,
    )
    stored = await db.fetchval("SELECT selection FROM duel WHERE id = $1", write.row_id)
    assert stored == arm

    names = await read.names_for(db, [1, 2])
    line = rail.duel_line(names[1], names[2], "A", context="tier_queue", selection=arm)
    assert phrase in line
    assert line.endswith(phrase)


async def test_a_held_out_pair_is_never_narrated_as_boundary_targeted(db, board_of):
    line = rail.duel_line(
        "Heat", "Drive", "A", context="tier_queue", selection=queue.ARM_HOLDOUT
    )
    assert "boundary" not in line
    assert "held out" in line


async def test_an_arm_with_no_phrase_fails_loudly_rather_than_borrowing_one():
    """`duel.selection`'s CHECK and `ARM_PHRASES` must stay in step."""
    with pytest.raises(rail.RailError):
        rail.duel_line("a", "b", "A", context="tier_queue", selection="clairvoyance")

    check = await_free_check_values()
    assert check == set(rail.ARM_PHRASES), (
        "every value duel.selection admits needs a phrase, and no phrase may name a value the "
        "column refuses"
    )


def await_free_check_values() -> set[str]:
    """Read out of 0005, so this runs without Postgres."""
    import re

    sql = (PACKAGE.parent / "migrations" / "0005_ledger.sql").read_text(encoding="utf-8")
    match = re.search(r"CHECK \(selection IN \(([^)]*)\)\)", sql)
    assert match, "0005 no longer constrains duel.selection"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


@pytest.fixture
async def tagged(db, board_of):
    """Both tiers, overlapping (§4.1 rule 1). Terms are the shipped `facet.term` ids, whole."""
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 2) "
        "ON CONFLICT DO NOTHING"
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        [("v1", "mood", 0)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        [("v1", "mood.cosy", "mood"), ("v1", "mood.bleak", "mood")],
    )
    await db.executemany(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources) "
        "VALUES ($1, 'v1', $2, 'mood', 3, 0.2, 1)",
        [(1, "mood.cosy"), (2, "mood.bleak")],
    )
    await db.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) "
        "VALUES ($1, 'v1', $2, 'mood', 0.1)",
        [(1, "mood.cosy"), (3, "mood.cosy")],
    )
    return board_of


async def test_each_rank_filter_narrows_the_board_on_its_own(db, board_of):
    everything = await read.items(db, user_id=board_of, kind="movie")
    assert len(everything) == 6

    short = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(runtime_max=110)
    )
    assert 0 < len(short) < len(everything)
    ids = {i.title_id for i in short}
    runtimes = {
        int(r["id"]): int(r["runtime_min"])
        for r in await db.fetch("SELECT id, runtime_min FROM title WHERE id = ANY($1::int[])",
                                list(ids))
    }
    assert all(v <= 110 for v in runtimes.values())

    seen = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(seen="seen")
    )
    # Every verdict implies seen, so `unseen` must come back empty.
    assert len(seen) == len(everything)
    unseen = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(seen="unseen")
    )
    assert unseen == []


async def test_combining_rank_filters_intersects(db, board_of):
    both = await read.items(
        db, user_id=board_of, kind="movie",
        filters=library.RankFilters(runtime_max=140, decade=1995),
    )
    by_runtime = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(runtime_max=140)
    )
    by_decade = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(decade=1995)
    )
    assert {i.title_id for i in both} == (
        {i.title_id for i in by_runtime} & {i.title_id for i in by_decade}
    )


async def test_a_dna_predicate_matches_bare_and_facet_qualified_alike(db, tagged):
    """`dna_tag.term` holds `facet.term`; a bare term is what the placeholder invites. A different
    facet before the same bare term is a different predicate."""
    bare = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    qualified = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="mood.cosy")
    )
    assert {i.title_id for i in bare} == {i.title_id for i in qualified} == {1, 3}

    other = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="bleak")
    )
    assert {i.title_id for i in other} == {2}

    nothing = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="pacing.cosy")
    )
    assert nothing == [], "a wrong facet is a different predicate, not a looser one"


async def test_a_dna_predicate_reaches_both_tiers_and_keeps_them_apart(db, tagged):
    """`dna_tag` alone would miss the projected tier; a fresh UNION would lose the tier column."""
    survivors = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    ids = [i.title_id for i in survivors]
    assert set(ids) == {1, 3}

    matched = await library.dna_tiers_for(db, title_ids=ids, dna="cosy")
    assert matched[1] == ["extracted", "projected"], "a pair in both tiers reports both"
    assert matched[3] == ["projected"]


async def test_a_dna_predicate_matches_the_name_a_member_reads(db, tagged):
    """Decision 486: members see a term's label, so the filter must match it as it matches the id."""
    await db.execute("UPDATE dna_term SET label = 'Warm and Snug' WHERE term = 'mood.cosy'")
    await db.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ('v1', 'mood.slow_burn', 'mood')"
    )
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources) "
        "VALUES (2, 'v1', 'mood.slow_burn', 'mood', 2, 0.9, 1)"
    )

    by_label = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="  Warm and snug ")
    )
    assert {i.title_id for i in by_label} == {1, 3}
    matched = await library.dna_tiers_for(db, title_ids=[1, 3], dna="warm and snug")
    assert matched == {1: ["extracted", "projected"], 3: ["projected"]}

    unlabelled = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="slow burn")
    )
    assert {i.title_id for i in unlabelled} == {2}, "a term with no label is read by its leaf"

    stranger = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="warm")
    )
    assert stranger == [], "a label is a name, matched whole, not a substring search"


async def test_no_rank_filter_puts_a_threshold_on_a_weight(db, tagged):
    """§4.1 rule 2: a 0.5 confidence cut deletes 44% of the extracted tier."""
    survivors = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    assert 1 in {i.title_id for i in survivors}


async def test_no_filter_suspends_the_kind_partition(db, world):
    """A DNA term shared across kinds is where a merge would show."""
    user = world["patrick"]
    await rate(db, user, verdicts=[(1, 2), (2, 0)])
    await rate(db, user, verdicts=[(11, 2), (12, 0)])
    await fitted(db, user, "movie")
    await fitted(db, user, "series")

    films = await read.items(db, user_id=user, kind="movie")
    series = await read.items(db, user_id=user, kind="series")
    assert {i.title_id for i in films} == {1, 2}
    assert {i.title_id for i in series} == {11, 12}

    filtered = await read.items(
        db, user_id=user, kind="series", filters=library.RankFilters(runtime_max=999)
    )
    assert {i.title_id for i in filtered} == {11, 12}


async def test_the_queue_pool_is_the_whole_board_and_not_the_filtered_view(db, board_of):
    """A queue limited to the filter would hold proposal 157's identity only on that corner."""
    filters = library.RankFilters(runtime_max=95)
    narrow = await read.items(db, user_id=board_of, kind="movie", filters=filters)
    assert len(narrow) < 6

    pool = await read.candidates(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert len(pool) == 6


async def test_the_board_and_the_queue_agree_about_who_is_eligible(db, board_of):
    tiers, cuts, rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    badged = {e.title_id for t in tiers for e in t.entries if e.straddle is not None}
    eligible = {
        i.title_id for i in queue.eligible(rows, cuts=cuts.boundaries, hp=DEFAULTS)
    }
    assert badged == eligible


async def test_a_drawn_pair_is_two_titles_from_this_persons_board(db, board_of):
    pool = await read.candidates(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    ids = {c.title_id for c in pool}
    rng = random.Random(2)
    for _ in range(200):
        pair = queue.draw(pool, rng=rng)
        assert {pair.title_a, pair.title_b} <= ids
        assert pair.title_a != pair.title_b


async def test_a_ledger_cache_miss_queues_the_refit_instead_of_fitting_in_the_request(
    db, board_of
):
    """A cache miss ran the full MAP fit on the event loop (6.96 s at n = 2000); the committed tap
    now stamps `refit_requested_at` and the 60 s sweep fits."""
    await db.execute("DELETE FROM ledger_fit WHERE user_id = $1", board_of)
    await db.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL WHERE user_id = $1", board_of
    )
    await observations.record_verdict(db, user_id=board_of, title_id=7, value=2)

    delta = await refit.update_incrementally(
        db, user_id=board_of, kind="movie", title_ids=[7], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    assert delta.refit is True and delta.rows == ()
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_fit WHERE user_id = $1", board_of
    ) == 0, "a cache miss must not run a full MAP fit on the request path"
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        board_of,
    ) is not None


async def test_a_first_ever_tap_queues_its_refit_even_with_no_cutpoints_row(db, world):
    """No `ledger_cutpoints` row exists before a first fit, so an UPDATE would have queued nothing."""
    jenny = world["jenny"]
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_cutpoints WHERE user_id = $1", jenny
    ) == 0
    await observations.record_verdict(db, user_id=jenny, title_id=1, value=2)

    delta = await refit.update_incrementally(
        db, user_id=jenny, kind="movie", title_ids=[1], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    assert delta.refit is True and not delta.rows
    row = await db.fetchrow(
        "SELECT tier_set, boundaries, refit_requested_at FROM ledger_cutpoints "
        "WHERE user_id = $1 AND kind = 'movie'",
        jenny,
    )
    assert row is not None and row["refit_requested_at"] is not None
    assert tuple(row["tier_set"]) == observations.DEFAULT_TIER_SET, (
        "the row the stamp needs carries §6.3's prior shape, which is what the board already "
        "falls back to when there is no row at all"
    )


async def test_the_board_reads_the_displayed_sigma_and_the_badge_follows_it(db, board_of):
    """Moving the displayed sigma must move the board's sigma and badge; which neighbour the badge
    names is decision 205's to move."""
    await db.execute(
        "UPDATE ledger_state SET sigma_eff = 1e-6 WHERE user_id = $1 AND kind = 'movie'",
        board_of,
    )
    settled, cuts, rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert rows and all(i.sigma == pytest.approx(1e-6) for i in rows)
    # Decision 508's hold ignores sigma: a held title names the tier its `s` falls in.
    held = {
        e.title_id
        for t in settled
        for e in t.entries
        if e.assigned_tier is None
        and int(model.tier_of(np.array([e.s]), cuts.boundaries)[0]) != e.model_tier
    }
    assert not [
        e for t in settled for e in t.entries if e.straddle is not None and e.title_id not in held
    ]

    target = rows[0].title_id
    await db.execute(
        "UPDATE ledger_state SET sigma_eff = 5.0 WHERE user_id = $1 AND title_id = $2",
        board_of,
        target,
    )
    widened, _cuts, after = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert {i.title_id: i.sigma for i in after}[target] == pytest.approx(5.0)
    badged = {e.title_id for t in widened for e in t.entries if e.straddle is not None} - held
    assert badged == {target}, "the badge is computed from the displayed sigma, not the fitted one"


async def test_an_unrated_owned_title_is_not_on_the_board(db, board_of):
    """Titles 7 to 10 are owned, placed and unrated."""
    everything = await read.items(db, user_id=board_of, kind="movie")
    assert {i.title_id for i in everything} == {1, 2, 3, 4, 5, 6}
    placed = await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='movie'", board_of
    )
    assert placed > 6, "the unrated titles are placed; they are just not on the tier list"


async def test_a_board_with_no_cutpoints_row_falls_back_to_the_prior_not_to_percentiles(db, world):
    """With nothing fitted, the model's prior, not a cut of the population on screen."""
    from spielplan.ledger import model

    cuts = await read.cutpoints_of(db, user_id=world["jenny"], kind="movie")
    assert cuts.tier_set == observations.DEFAULT_TIER_SET
    assert np.allclose(cuts.boundaries, model.initial_cutpoints(7))


async def test_the_public_projection_carries_no_ungated_model_number(db, board_of):
    """Top-level `s` or σ would route around `rail.redact` (decision 117)."""
    tiers, _cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    payload = read.public(tiers)
    flat = repr(payload)
    for entry in (e for t in payload for e in t["entries"]):
        assert "s" not in entry and "sigma" not in entry
    assert "sigma" not in flat


async def test_the_asked_set_leaves_the_held_out_stream_out_of_the_selector(db, board_of):
    """`queue._exploration` refuses to re-serve pairs in this set, so a held-out row would steer it."""
    await observations.record_duel(
        db, user_id=board_of, title_a=9, title_b=10, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    await observations.record_duel(
        db, user_id=board_of, title_a=7, title_b=8, outcome="B",
        context="tier_queue", selection=queue.ARM_BOUNDARY,
    )

    asked = await read.asked_pairs(db, user_id=board_of, kind="movie")
    assert frozenset((7, 8)) in asked
    assert frozenset((9, 10)) not in asked, "§13: the evaluation stream is not a selector input"
    assert frozenset((1, 2)) in asked, (
        "and every context counts - a pair settled in a §6.1 battle is one they have answered"
    )
    assert await read.asked_pairs(db, user_id=board_of, kind="series") == set(), (
        "§4.1 rule 5: a films pair is not a fact about the series board"
    )


async def test_the_evaluation_partitions_by_kind_on_both_sides_of_the_pair(db, board_of):
    """§10's re-import can change `title.kind`, making an existing duel cross-kind; join both sides."""
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    before = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert before.pairs == 1

    await db.execute("UPDATE title SET kind = 'series' WHERE id = 2")
    after = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert after.pairs == 0, (
        "a pair with one foot in each partition is not evidence about either (§4.1 rule 5)"
    )


async def test_the_evaluation_abstains_when_the_model_has_no_ordering(db, board_of):
    """`s_a == s_b` is the model's tie; scoring it as "B" is an unmeasured threshold at zero."""
    await db.execute(
        "UPDATE ledger_state SET s = 0.5 WHERE user_id = $1 AND title_id IN (1, 2)", board_of
    )
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 1
    assert agreement.undecided == 1
    assert agreement.decisive == 0 and agreement.agreed == 0
    assert agreement.rate is None, "no ordering is an abstention, not a coin flip scored as B"
