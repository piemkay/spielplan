"""Round 2's contradictions (decisions 508-510): R1 a disliked film in A, R2 a pick left below
what it beat, R3 ties a tier apart, R4 Home's letter against the card's guess."""

from __future__ import annotations

import numpy as np
import pytest

from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import drop, read
from spielplan.rate import session as rate_session
from spielplan.scoring.backbone import COORDINATE_GEOMETRY

# A taste along one axis. Titles 1-24 sit evenly on it and are rated by where they sit; title 25
# is La La Land, near the top of the axis and disliked; 26 is owned and unrated, 27 unowned.
TASTE = {t: -1.0 + 2.0 * (t - 1) / 23.0 for t in range(1, 25)} | {25: 0.9, 26: 0.8, 27: -0.9}
LA_LA_LAND = 25


def _verdict(title_id: int) -> int:
    x = TASTE[title_id]
    return 0 if x < -0.35 else (1 if x <= 0.3 else 2)


def axis_embeddings(title_ids):
    ids = list(title_ids)
    matrix = np.zeros((len(ids), 64))
    for i, t in enumerate(ids):
        matrix[i, 0] = TASTE.get(int(t), 0.0)
    return matrix, np.asarray([int(t) in TASTE for t in ids], dtype=bool)


@pytest.fixture
async def household(db):
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT x.id, 'movie', x.name, 2001, 100, x.id <> 27
        FROM unnest($1::int[], $2::text[]) AS x(id, name)
        """,
        list(range(1, 28)),
        [f"Title {t}" if t != LA_LA_LAND else "La La Land" for t in range(1, 28)],
    )
    user = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'member') RETURNING id"
    )
    for title_id in range(1, 25):
        await observations.record_verdict(
            db, user_id=user, title_id=title_id, value=_verdict(title_id)
        )
    await observations.record_verdict(db, user_id=user, title_id=LA_LA_LAND, value=0)
    return user


async def _fit(db, user):
    report = await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=axis_embeddings
    )
    assert report.fitted, report.as_dict()


async def _rows(db, user):
    return {
        int(r["title_id"]): (float(r["s"]), int(r["tier"]))
        for r in await db.fetch(
            "SELECT title_id, s, tier FROM ledger_state WHERE user_id = $1 AND observed", user
        )
    }


async def test_a_film_the_taste_vector_loves_and_the_person_disliked_sits_in_a_disliked_tier(
    db, household
):
    """R1: stored and rendered tiers both put the disliked film in F/D/C; fine films share B."""
    await _fit(db, household)
    stored = await _rows(db, household)
    assert stored[LA_LA_LAND][1] <= 2, stored[LA_LA_LAND]
    liked = [t for t in range(1, 25) if _verdict(t) == 2]
    fine = [t for t in range(1, 25) if _verdict(t) == 1]
    assert stored[LA_LA_LAND][0] < min(stored[t][0] for t in liked)
    assert {stored[t][1] for t in fine} == {3}
    assert {stored[t][1] for t in liked} <= {4, 5, 6}

    tiers, _cuts, _items = await read.load(db, user_id=household, kind="movie", hp=DEFAULTS)
    shown = {e.title_id: t.label for t in tiers for e in t.entries}
    assert shown[LA_LA_LAND] in {"F", "D", "C"}
    assert any(label == "C" for label in shown.values()), "C stood empty on a board of dislikes"


async def test_a_pick_in_the_queue_lifts_the_winner_over_the_title_it_beat(db, household):
    """R2 (decision 509): the incremental update after one pick lifts the winner over the loser."""
    await _fit(db, household)
    loser = LA_LA_LAND
    winner = 1
    before = await _rows(db, household)
    assert before[winner][0] < before[loser][0], "the fixture does not start the pair inverted"
    await observations.record_duel(
        db, user_id=household, title_a=winner, title_b=loser, outcome="A",
        context="tier_queue", decisive=False, hp=DEFAULTS, selection="boundary",
    )
    delta = await refit.update_incrementally(
        db, user_id=household, kind="movie", title_ids=[winner, loser], hp=DEFAULTS,
        embeddings=axis_embeddings,
    )
    assert delta.fit_source == "incremental"
    after = await _rows(db, household)
    assert after[winner][0] > after[loser][0], (before[winner], before[loser], after)


async def test_a_title_its_verdict_holds_is_a_neighbour_where_the_board_shows_it(db, household):
    """The hold renders La La Land in C, so a drag beside it there must not be refused."""
    liked = [t for t in range(1, 25) if _verdict(t) == 2][:3]
    for other in liked:
        await observations.record_duel(
            db, user_id=household, title_a=LA_LA_LAND, title_b=other, outcome="A",
            context="tier_queue", decisive=True, hp=DEFAULTS, selection="boundary",
        )
    await _fit(db, household)
    tiers, cuts, _items = await read.load(db, user_id=household, kind="movie", hp=DEFAULTS)
    entry = next(e for t in tiers for e in t.entries if e.title_id == LA_LA_LAND)
    assert entry.tier == 2, entry
    assert int(model.tier_of(np.array([entry.s]), cuts.boundaries)[0]) > 2, (
        "the hold did not bind, so this does not test it"
    )
    result = await drop.drop(
        db, user_id=household, title_id=2, tier=2, above=LA_LA_LAND, below=None
    )
    assert result.tier == 2 and result.neighbour_duels == 1


async def test_rates_guess_is_the_tier_home_shows_owned_or_not(db, household):
    """R4 (decision 510): Rate's guess is the title's tier on the person's cuts, owned or not."""
    await _fit(db, household)
    for title_id in (26, 27, 12):
        guess = await rate_session.guess(
            db, user_id=household, title_id=title_id, kind="movie", hp=DEFAULTS,
            embeddings=axis_embeddings,
        )
        assert guess is not None
        cache = await refit.load_cache(
            db, user_id=household, kind="movie", hp=DEFAULTS, lock=False
        )
        stored = await db.fetchval(
            "SELECT tier FROM ledger_state WHERE user_id = $1 AND title_id = $2",
            household, title_id,
        )
        tier = (
            int(stored)
            if stored is not None
            else int(model.tier_of(
                np.array([cache.mu + axis_embeddings([title_id])[0][0] @ cache.v]), cache.cuts
            )[0])
        )
        assert guess.tier == tier, (title_id, tier, guess)
    unrated = await db.fetch(
        "SELECT tier FROM ledger_state WHERE user_id = $1 AND NOT observed", household
    )
    assert unrated and {int(r["tier"]) for r in unrated} <= {2, 3, 4}, (
        "an unrated title wears a class's middle tier, never a grade (decision 510)"
    )


async def test_a_board_fitted_on_the_old_tier_scale_is_refitted_by_the_tick(db, household):
    """Decision 508 changes no digest-visible constant, so the stamp carries the tier scale too."""
    await _fit(db, household)
    assert refit.LEDGER_GEOMETRY != COORDINATE_GEOMETRY
    await db.execute(
        "UPDATE ledger_fit SET geometry = $2 WHERE user_id = $1", household, COORDINATE_GEOMETRY
    )
    assert await refit.load_cache(
        db, user_id=household, kind="movie", hp=DEFAULTS, lock=False
    ) is None
    assert (household, "movie") in [(u, k) for u, k, _ in await refit.refreshes_owed(db)]
