"""The ladder's set-up is each member's cut-over (decision 537): their earlier answers stay as history
and every reader of their answers starts from the set-up. Placements (§4.2, decision 536) and their
re-asks (§13 stream b). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import numpy as np
import pytest

from spielplan.ledger import ladder, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import evaluation, read
from spielplan.scoring import foldin
from tests.helpers import insert_user

FILMS = tuple(range(1, 9))
SERIES = (11, 12)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    rows = [np.random.default_rng(1000 + t).normal(size=64) for t in ids]
    return np.stack([r / (np.linalg.norm(r) * 8.0) for r in rows]), np.ones(len(ids), dtype=bool)


@pytest.fixture
async def world(db):
    ids = list(FILMS) + list(SERIES)
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned)
        SELECT x.id, x.kind, 'Title ' || x.id, true
        FROM unnest($1::int[], $2::text[]) AS x(id, kind)
        """,
        ids,
        ["movie"] * len(FILMS) + ["series"] * len(SERIES),
    )
    return {
        "patrick": await insert_user(db, "patrick", "admin"),
        "jenny": await insert_user(db, "jenny"),
    }


async def _history(db, user):
    """What a member answered before the ladder: verdicts, a drag, comparisons, a series verdict."""
    for title_id, value in ((1, 2), (2, 2), (3, 1), (4, 0), (5, 1), (11, 2)):
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)
    await observations.record_tier_edit(db, user_id=user, title_id=2, tier=6)
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=2, outcome="A", context="tier_queue",
        decisive=True, hp=DEFAULTS,
    )
    await observations.record_duel(
        db, user_id=user, title_a=3, title_b=4, outcome="A", context="tier_queue",
        selection="uniform_holdout",
    )


async def _set_up(db, user, picks=((1, 5), (6, 3), (7, 0))):
    return await ladder.finish_setup(db, user_id=user, picks=list(picks))


async def _fitted(db, user, kind="movie"):
    return await refit.refit_user(
        db, user_id=user, kind=kind, hp=DEFAULTS, embeddings=fixture_embeddings
    )


async def test_before_the_set_up_every_answer_is_read_as_today(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)

    loaded = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    assert (loaded.n_verdicts, loaded.n_tier_edits, loaded.n_duels) == (5, 1, 1)
    assert dict(await foldin.live_labels(db, user_id=patrick, kind="movie")) == {
        1: 4, 2: 6, 3: 3, 4: 2, 5: 3,
    }
    state = await ladder.state(db, user_id=patrick)
    assert (state.done, state.earlier_ratings, state.rated_before) == (False, 6, 0)
    assert await ladder.rated_before(db, user_id=patrick, kinds=observations.KINDS) == []
    with pytest.raises(ladder.NotSetUp):
        await ladder.require_set_up(db, user_id=patrick)


async def test_after_the_set_up_the_fit_reads_only_the_set_up_and_what_came_after(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    await _set_up(db, patrick)
    await observations.record_duel(
        db, user_id=patrick, title_a=1, title_b=6, outcome="B", context="tier_queue",
        decisive=False, hp=DEFAULTS,
    )

    loaded = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    assert (loaded.n_verdicts, loaded.n_tier_edits, loaded.n_duels) == (0, 3, 1)
    assert set(loaded.title_ids.tolist()) == {1, 6, 7}
    assert loaded.n_held_out == 0, "the held-out duel before the set-up is history too"

    series = await observations.load_observations(db, user_id=patrick, kind="series", hp=DEFAULTS)
    assert series.obs.is_empty(), "the series history stops at the film set-up as well"

    assert dict(await foldin.live_labels(db, user_id=patrick, kind="movie")) == {1: 5, 6: 3, 7: 0}
    assert await foldin.live_labels(db, user_id=patrick, kind="series") == []

    live = await db.fetch(
        f"SELECT title_id, value FROM ({observations.LIVE_LABEL_SQL}) l ORDER BY title_id", patrick
    )
    assert [(r["title_id"], r["value"]) for r in live] == [(1, 2), (6, 1), (7, 0)]
    edits = await db.fetch(
        f"SELECT title_id, tier FROM ({observations.latest_tier_edit_sql()}) e ORDER BY title_id",
        patrick,
    )
    assert [(r["title_id"], r["tier"]) for r in edits] == [(1, 5), (6, 3), (7, 0)]
    assert await ladder.placements(db, user_id=patrick, kind="movie") == {1: 5, 6: 3, 7: 0}

    # The history is kept, append-only.
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", patrick) == 6 + 3
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", patrick) == 3


async def test_the_set_up_rows_share_the_cut_over_instant(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    result = await _set_up(db, patrick)

    assert result.finished_at == await ladder.set_up_at(db, user_id=patrick)
    assert await ladder.require_set_up(db, user_id=patrick) == result.finished_at
    stamps = await db.fetch(
        "SELECT created_at FROM tier_edit WHERE user_id = $1 AND title_id = ANY($2::int[]) "
        "UNION ALL "
        "SELECT created_at FROM verdict WHERE user_id = $1 AND source = 'setup'",
        patrick,
        [1, 6, 7],
    )
    assert len(stamps) == 6 and {r["created_at"] for r in stamps} == {result.finished_at}
    edits = await db.fetch(
        "SELECT via FROM tier_edit WHERE user_id = $1 AND created_at = $2",
        patrick,
        result.finished_at,
    )
    assert [r["via"] for r in edits] == ["explicit"] * 3
    assert [(p.tier, p.label, p.word) for p in result.placed] == [
        (5, "A+", "Loved it"), (3, "B", "It was fine"), (0, "F", "Hated it"),
    ]


async def test_an_unseen_pick_becomes_seen_and_owes_jellyfin_the_push(db, world):
    patrick = world["patrick"]
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) "
        "VALUES ($1, 7, 'unseen', now())",
        patrick,
    )
    result = await _set_up(db, patrick)

    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = 7", patrick
    )
    assert (row["state"], row["jf_synced_at"]) == ("seen", None)
    assert [p.implied_seen for p in result.placed] == [True, True, True]


async def test_the_set_up_drops_the_fit_and_queues_both_boards(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    for kind in observations.KINDS:
        await _fitted(db, patrick, kind)
    assert await db.fetchval("SELECT count(*) FROM ledger_fit WHERE user_id = $1", patrick) == 2

    await _set_up(db, patrick)

    assert await db.fetchval("SELECT count(*) FROM ledger_fit WHERE user_id = $1", patrick) == 0
    assert await db.fetchval("SELECT count(*) FROM ledger_state WHERE user_id = $1", patrick) == 0
    queued = await db.fetch(
        "SELECT kind FROM ledger_cutpoints WHERE user_id = $1 AND refit_requested_at IS NOT NULL "
        "ORDER BY kind",
        patrick,
    )
    assert [r["kind"] for r in queued] == ["movie", "series"]

    # The refits then fit the set-up alone; a series board with nothing since fits as empty.
    movie, series = await _fitted(db, patrick), await _fitted(db, patrick, "series")
    assert movie.fitted and (movie.n_verdicts, movie.n_tier_edits) == (0, 3)
    assert series.error is None and not series.fitted
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND kind = 'series'", patrick
    ) == 0


async def test_the_incremental_path_agrees_with_the_full_fit_after_the_cut_over(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    await _set_up(db, patrick)
    await _fitted(db, patrick)

    delta = await refit.update_incrementally(
        db, user_id=patrick, kind="movie", title_ids=[2, 6], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    observed = {row.title_id: row.observed for row in delta.rows}
    assert observed == {2: False, 6: True}, "title 2's verdict and drag are from before the set-up"


async def test_the_board_and_its_duel_readers_start_at_the_cut_over(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    await _set_up(db, patrick)
    await observations.record_duel(
        db, user_id=patrick, title_a=6, title_b=7, outcome="A", context="tier_queue",
        decisive=True, hp=DEFAULTS,
    )
    await observations.record_duel(
        db, user_id=patrick, title_a=1, title_b=6, outcome="A", context="tier_queue",
        selection="uniform_holdout",
    )
    await _fitted(db, patrick)

    items = {i.title_id: i for i in await read.items(db, user_id=patrick, kind="movie")}
    assert set(items) == {1, 6, 7}
    assert [(items[t].assigned_tier, items[t].verdict) for t in (1, 6, 7)] == [(5, 2), (3, 1), (0, 0)]

    assert await read.comparison_counts(db, user_id=patrick, kind="movie") == {6: 1, 7: 1}
    assert await read.asked_pairs(db, user_id=patrick, kind="movie") == {frozenset((6, 7))}
    assert await read.recent_titles(db, user_id=patrick, kind="movie") == {6, 7}
    # The seal's nonce counts every answer ever given in the context.
    assert await read.answered_comparisons(db, user_id=patrick, kind="movie") == 4

    agreement = await evaluation.held_out_agreement(db, user_id=patrick, kind="movie")
    assert agreement.pairs + agreement.unplaced == 1


async def test_a_second_finish_is_refused_and_writes_nothing(db, world):
    patrick = world["patrick"]
    await _set_up(db, patrick)
    edits = await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", patrick)
    with pytest.raises(ladder.AlreadySetUp):
        await _set_up(db, patrick, picks=((2, 6),))
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", patrick) == edits


@pytest.mark.parametrize(
    ("picks", "reason"),
    [
        ((), "empty"),
        (((1, 6), (1, 5)), "duplicate"),
        (((1, 6), (11, 5)), "not_a_film"),
        (((1, 6), (999, 5)), "not_a_film"),
        (((1, 7),), "bad_tier"),
        (((1, -1),), "bad_tier"),
    ],
)
async def test_a_set_up_that_places_nothing_valid_is_refused(db, world, picks, reason):
    patrick = world["patrick"]
    with pytest.raises(ladder.SetupRefused) as refused:
        await _set_up(db, patrick, picks=picks)
    assert refused.value.reason == reason
    assert await ladder.set_up_at(db, user_id=patrick) is None
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", patrick) == 0


async def test_a_placement_records_its_class_where_the_live_verdict_is_none_or_another(db, world):
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=1, value=2)
    await observations.record_verdict(db, user_id=patrick, title_id=2, value=0)

    none = await ladder.place(db, user_id=patrick, title_id=3, tier=4)
    same = await ladder.place(db, user_id=patrick, title_id=1, tier=6)
    other = await ladder.place(db, user_id=patrick, title_id=2, tier=5)

    assert none.verdict is not None and none.superseded_verdict_id is None
    assert same.verdict is None and same.verdict_id is None
    assert other.verdict is not None and other.superseded_verdict_id is not None
    values = await db.fetch(
        f"SELECT title_id, value FROM ({observations.LIVE_LABEL_SQL}) l ORDER BY title_id", patrick
    )
    assert [(r["title_id"], r["value"]) for r in values] == [(1, 2), (2, 2), (3, 2)]
    sources = await db.fetch(
        "SELECT source FROM verdict WHERE id = ANY($1::bigint[])",
        [none.verdict_id, other.verdict_id],
    )
    assert {r["source"] for r in sources} == {"ladder"}
    assert (none.kind, none.label, none.word, none.implied_seen) == ("movie", "A", "Liked it", True)
    assert same.implied_seen is False and same.prior_state == ()

    with pytest.raises(ValueError):
        await ladder.place(db, user_id=patrick, title_id=3, tier=7)


async def test_a_placement_after_the_cut_over_records_a_verdict_even_where_history_agrees(db, world):
    patrick = world["patrick"]
    await observations.record_verdict(db, user_id=patrick, title_id=2, value=2)
    await _set_up(db, patrick)

    placed = await ladder.place(db, user_id=patrick, title_id=2, tier=6)
    assert placed.verdict is not None, "the old liked verdict is history; the placement needs its own"


async def test_undoing_a_placement_removes_both_rows_and_splices_the_chain(db, world):
    patrick = world["patrick"]
    original = await observations.record_verdict(db, user_id=patrick, title_id=2, value=0)
    await observations.record_not_seen(db, user_id=patrick, title_id=2)
    prior = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = 2", patrick
    )

    placed = await ladder.place(db, user_id=patrick, title_id=2, tier=5)
    assert placed.implied_seen and placed.superseded_verdict_id == original.row_id

    undone = await ladder.undo_placement(
        db, user_id=patrick, tier_edit_id=placed.tier_edit_id, verdict_id=placed.verdict_id,
        prior_state=placed.prior_state,
    )
    assert undone.unsuperseded == (original.row_id,)
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", patrick) == 0
    rows = await db.fetch("SELECT id, superseded_by FROM verdict WHERE user_id = $1", patrick)
    assert [(r["id"], r["superseded_by"]) for r in rows] == [(original.row_id, None)]
    after = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = 2", patrick
    )
    assert dict(after) == dict(prior)

    with pytest.raises(observations.UndoRefused):
        await ladder.undo_placement(
            db, user_id=patrick, tier_edit_id=placed.tier_edit_id, verdict_id=None, prior_state=(),
        )


async def test_a_same_step_re_ask_is_recorded_and_not_fitted_and_a_different_one_moves(db, world):
    patrick = world["patrick"]
    setup = {p.edit.title_ids[0]: p.tier_edit_id for p in (await _set_up(db, patrick)).placed}

    same = await ladder.place(db, user_id=patrick, title_id=6, tier=3, reask_of=setup[6])
    assert same.verdict is None
    stored = await db.fetchval("SELECT reask_of FROM tier_edit WHERE id = $1", same.tier_edit_id)
    assert stored == setup[6]
    loaded = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    assert (loaded.n_tier_edits, loaded.n_reask) == (3, 1)

    moved = await ladder.place(db, user_id=patrick, title_id=7, tier=2, reask_of=setup[7])
    assert moved.verdict is None, "C is still disliked"
    loaded = await observations.load_observations(db, user_id=patrick, kind="movie", hp=DEFAULTS)
    assert (loaded.n_tier_edits, loaded.n_reask) == (4, 1)
    assert await ladder.placements(db, user_id=patrick, kind="movie") == {1: 5, 6: 3, 7: 2}

    # The incremental path skips the same-step re-ask too.
    await _fitted(db, patrick)
    full = await db.fetchval("SELECT s FROM ledger_state WHERE user_id = $1 AND title_id = 6", patrick)
    delta = await refit.update_incrementally(
        db, user_id=patrick, kind="movie", title_ids=[6], hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert delta.rows[0].s == pytest.approx(full, abs=1e-4)


async def test_films_rated_before_wait_until_they_are_placed_again(db, world):
    patrick = world["patrick"]
    await _history(db, patrick)
    await observations.record_not_seen(db, user_id=patrick, title_id=4)
    result = await _set_up(db, patrick)

    # 1 is placed in the set-up; 4 is not seen; 2's drag is the newest old answer; 11 is a series.
    assert await ladder.rated_before(db, user_id=patrick, kinds=["movie"]) == [2, 5, 3]
    assert await ladder.rated_before(db, user_id=patrick, kinds=["series"]) == [11]
    assert (result.earlier_ratings, result.rated_before) == (6, 4)

    await ladder.place(db, user_id=patrick, title_id=5, tier=3)
    state = await ladder.state(db, user_id=patrick)
    assert (state.done, state.earlier_ratings, state.rated_before) == (True, 6, 3)


async def test_a_placed_film_marked_not_seen_keeps_its_step(db, world):
    patrick = world["patrick"]
    await _set_up(db, patrick)
    await observations.record_not_seen(db, user_id=patrick, title_id=6)
    assert (await ladder.placements(db, user_id=patrick, kind="movie"))[6] == 3
    assert dict(await foldin.live_labels(db, user_id=patrick, kind="movie"))[6] == 3


async def test_one_members_set_up_cuts_nobody_elses_history(db, world):
    patrick, jenny = world["patrick"], world["jenny"]
    await _history(db, patrick)
    await _history(db, jenny)
    await _set_up(db, patrick)

    loaded = await observations.load_observations(db, user_id=jenny, kind="movie", hp=DEFAULTS)
    assert (loaded.n_verdicts, loaded.n_tier_edits, loaded.n_duels) == (5, 1, 1)
    assert (await ladder.state(db, user_id=jenny)).done is False


async def test_the_set_up_goes_with_its_member(db, world):
    jenny = world["jenny"]
    await _set_up(db, jenny)
    await db.execute("DELETE FROM app_user WHERE id = $1", jenny)
    assert await db.fetchval("SELECT count(*) FROM ladder_setup") == 0


def test_the_words_follow_the_default_set_and_a_custom_set_names_itself():
    assert observations.tier_words(observations.DEFAULT_TIER_SET) == (
        "Hated it", "Didn't like it", "Not really for me", "It was fine", "Liked it", "Loved it",
        "All-time favourite",
    )
    assert observations.tier_words(("Meh", "Good", "Great")) == ("Meh", "Good", "Great")
