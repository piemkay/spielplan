"""Rate is the ladder (§6.1): one placement card, a journal Undo reads, blocks of 15, and Rate closed
until the person's set-up. The tests that matter race two taps on one card and read the journal the
Jellyfin push corrected."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import asyncpg
import httpx
import numpy as np
import pytest

from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import JellyfinConfig
from spielplan.home import rail
from spielplan.ledger import ladder, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rate import queue, session
from tests.helpers import insert_user

HP = DEFAULTS

# §6.1's two refusals: a double tap must never reach `app.py`'s generic conflict handler.
STALE_REASONS = {"no_card", "stale_card"}

# A race that passes once has not passed: the reproductions failed four and five of six.
RACES = 8

# Marker values the fixtures write into `ledger_state`, so a leak shows as a literal in the JSON.
FORBIDDEN_CARD_KEYS = {
    "predicted", "s", "sigma", "cdf", "straddle", "score", "rank", "band", "reask_of", "source",
    "label", "b", "gate", "beta",
}
MARKER_S = 0.777123
MARKER_CDF = 0.913357

WORDS = observations.tier_words(observations.DEFAULT_TIER_SET)


async def make_titles(db, specs) -> None:
    """specs: [(id, kind, name)]."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned, overview)
        SELECT x.id, x.kind, x.name, true, 'A film about ' || x.name
        FROM unnest($1::int[], $2::text[], $3::text[]) AS x(id, kind, name)
        """,
        [s[0] for s in specs],
        [s[1] for s in specs],
        [s[2] for s in specs],
    )


async def set_up(db, user_id: int) -> None:
    """The cut-over row alone: these tests are about Rate, not the set-up's picks."""
    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", user_id)


async def rated_before(db, user_id: int, title_id: int, value: int = 2) -> None:
    """A seen film with a verdict from before the set-up: history, waiting for its step."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen') "
        "ON CONFLICT (user_id, title_id) DO UPDATE SET state = 'seen'",
        user_id,
        title_id,
    )
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value, created_at) "
        "VALUES ($1, $2, $3, now() - interval '30 days')",
        user_id,
        title_id,
        value,
    )


async def place(db, user_id: int, tiers: dict[int, int]) -> None:
    """Placements written straight through the ladder: going through Rate would spend cards."""
    for title_id, tier in tiers.items():
        await ladder.place(db, user_id=user_id, title_id=title_id, tier=tier)


@pytest.fixture
async def world(db):
    """Twenty films and a person who has set up their ladder; the block test spends fifteen."""
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    user = await insert_user(db, "patrick", "admin")
    await set_up(db, user)
    return {"user": user}


async def open_session(db, user_id, *, kinds=("movie",)) -> session.RateSession:
    s = await session.open_or_resume(db, user_id=user_id, kinds=list(kinds))
    return await session.ensure_card(db, s)


def token(s: session.RateSession) -> str:
    assert s.card_token is not None, "no card on the table"
    return str(s.card_token)


async def put(db, s: session.RateSession, title_id: int, **extra: Any) -> session.RateSession:
    """Stash one named card, as the queue would."""
    card = {"kind": "movie", "title_id": title_id, "reason": "queued because", "p_seen": 0.4,
            "source": "p_seen", "reask_of": None, **extra}
    return await session.stash(db, s, card, expected=s.card_token)


def _walk(node: Any, path: str = "card"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def assert_no_model_belief(card: dict[str, Any]) -> None:
    leaks = [f"{path}.{key}" for path, key, _ in _walk(card) if key in FORBIDDEN_CARD_KEYS]
    assert not leaks, f"the card carries the model's belief or a letter at {leaks}"
    body = json.dumps(card)
    assert str(MARKER_S) not in body and str(MARKER_CDF) not in body


async def seed_ledger(db, user_id: int, title_ids, *, fitted: bool = False) -> None:
    """Every title carries two marker numbers, so a leak shows as a literal. `fitted=True` runs a
    real refit first: without a cached fit the incremental path touches nothing."""
    if fitted:
        report = await refit.refit_user(db, user_id=user_id, kind="movie", hp=HP)
        assert report.fitted, report.error
    for title_id in title_ids:
        await db.execute(
            "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma, cdf, tier) "
            "VALUES ($1, $2, 'movie', $3, 0.25, $4, 5) "
            "ON CONFLICT (user_id, title_id) DO UPDATE "
            "SET s = EXCLUDED.s, sigma = EXCLUDED.sigma, cdf = EXCLUDED.cdf, tier = EXCLUDED.tier",
            user_id,
            title_id,
            MARKER_S,
            MARKER_CDF,
        )


# --- the placement -------------------------------------------------------------------------------


async def test_a_placement_is_one_tier_edit_its_verdict_and_one_journal_row(db, world):
    """§4.2: a ladder tap is a `tier_edit` (`explicit`) that implies seen and records its tier's
    verdict, journalled so Undo can take both back."""
    user = world["user"]
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]

    out = await session.record_placement(db, s, card_token=token(s), tier=5, hp=HP)

    edit = await db.fetchrow(
        "SELECT id, tier, via, reask_of FROM tier_edit WHERE user_id = $1 AND title_id = $2",
        user, title_id,
    )
    assert (edit["tier"], edit["via"], edit["reask_of"]) == (5, "explicit", None)
    verdict = await db.fetchrow(
        "SELECT id, value, source FROM verdict WHERE user_id = $1 AND title_id = $2", user, title_id
    )
    assert (verdict["value"], verdict["source"]) == (2, "ladder"), "A+ stands for liked"
    row = await db.fetchrow(
        "SELECT kind_of, tier_edit_id, verdict_id, title_ids, advances FROM rate_observation "
        "WHERE session_id = $1",
        s.id,
    )
    assert row["kind_of"] == "placement" and row["advances"] is True
    assert (row["tier_edit_id"], row["verdict_id"]) == (edit["id"], verdict["id"])
    assert list(row["title_ids"]) == [title_id]
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, title_id
    ) == "seen"

    assert out.session.slot == 2
    assert out.session.current_card["title_id"] != title_id, "the next card rode in with the answer"
    assert out.echo == {"title_id": title_id, "name": f"Title {title_id}", "tier": 5, "word": "Loved it"}


async def test_a_step_outside_the_set_is_refused_before_anything_is_written(db, world):
    user = world["user"]
    s = await open_session(db, user)
    for tier in (7, -1):
        with pytest.raises(session.BadTier):
            await session.record_placement(db, s, card_token=token(s), tier=tier, hp=HP)
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0
    assert await db.fetchval("SELECT count(*) FROM rate_observation") == 0
    assert str(await db.fetchval("SELECT card_token FROM rate_session WHERE id = $1", s.id)) == token(s)


async def test_a_custom_set_places_on_its_own_steps_and_names_them_by_label(db, world):
    """A custom set's labels are its words (plan reading 16); a step past its size is refused."""
    user = world["user"]
    await db.execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
        "VALUES ($1, 'movie', ARRAY[0.0, 1.0], ARRAY['bad', 'ok', 'good'])",
        user,
    )
    s = await open_session(db, user)
    card = await session.public_card(db, s, version=None)
    assert [(shelf["tier"], shelf["word"]) for shelf in card["shelves"]] == [
        (2, "good"), (1, "ok"), (0, "bad"),
    ]
    with pytest.raises(session.BadTier):
        await session.record_placement(db, s, card_token=token(s), tier=3, hp=HP)
    out = await session.record_placement(db, s, card_token=token(s), tier=2, hp=HP)
    assert out.echo["word"] == "good"


async def test_not_seen_writes_unseen_and_no_observation_and_a_placed_film_keeps_its_step(db, world):
    """Decision 550: Not seen on a placed film changes the seen state alone; Rank keeps the title."""
    user = world["user"]
    s = await open_session(db, user)
    unseen = s.current_card["title_id"]
    s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, unseen
    ) == "unseen"
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 0
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0
    assert s.slot == 2

    await place(db, user, {20: 4})
    s = await session.ensure_card(db, await put(db, s, 20))
    s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 20", user
    ) == "unseen"
    assert (await ladder.placements(db, user_id=user, kind="movie"))[20] == 4, "the step stays"
    assert [r["kind_of"] for r in await db.fetch(
        "SELECT kind_of FROM rate_observation WHERE session_id = $1 ORDER BY seq", s.id
    )] == ["not_seen", "not_seen"]


async def test_the_card_carries_no_model_belief_and_no_letter_and_the_guess_arrives_after(db, world):
    """The numbers absent from the card must be present in the tap's echo, or omission is trivial."""
    user = world["user"]
    await seed_ledger(db, user, range(1, 21))
    s = await open_session(db, user)

    whole = await session.payload(db, s)
    card = whole["card"]
    assert_no_model_belief(card)
    assert whole["echo"] is None
    assert set(card) == {"token", "kind", "title", "reason", "shelves", "model"}
    assert set(card["model"]) == {"p_seen"}, "P(seen) alone, and only behind Show the model"
    assert [shelf["word"] for shelf in card["shelves"]] == list(reversed(WORDS))
    assert all(set(shelf) == {"tier", "word", "count", "films"} for shelf in card["shelves"])

    out = await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP)
    assert out.echo["word"] == "Liked it"
    assert out.echo["model"] == {"guess_word": "Loved it", "cdf": round(MARKER_CDF, 2)}, (
        "the stored row said step 5 at that cdf"
    )
    assert_no_model_belief((await session.payload(db, out.session))["card"])


async def test_the_guess_is_read_before_the_write_and_not_after_it(db, world):
    """A handler reading after the update would echo the placement back as its own guess."""
    user = world["user"]
    await place(db, user, {1: 6, 2: 5, 3: 2, 4: 1})
    await seed_ledger(db, user, range(1, 21), fitted=True)
    s = await put(db, await session.open_or_resume(db, user_id=user, kinds=["movie"]), 9)

    out = await session.record_placement(db, s, card_token=token(s), tier=0, hp=HP)
    assert out.echo["model"]["cdf"] == round(MARKER_CDF, 2)
    assert out.ledger["applied"] is True
    after = await db.fetchval(
        "SELECT cdf FROM ledger_state WHERE user_id = $1 AND title_id = 9", user
    )
    assert after != pytest.approx(MARKER_CDF), "the incremental update did move the title"


async def test_no_guess_is_invented_before_the_first_fit(db, world):
    """No fit is legal (§3.1); a guess off a CDF that does not exist has no provenance."""
    s = await open_session(db, world["user"])
    out = await session.record_placement(db, s, card_token=token(s), tier=3, hp=HP)
    assert "model" not in out.echo


def _embeddings_from(vectors: dict[int, Any]):
    """(n, 64) and a mask, with one axis carrying the signal so `v` has a direction."""

    def rows(title_ids):
        matrix = np.zeros((len(title_ids), 64))
        mask = np.zeros(len(title_ids), dtype=bool)
        for i, title_id in enumerate(title_ids):
            vector = vectors.get(int(title_id))
            if vector is not None:
                matrix[i][0] = vector
                mask[i] = True
        return matrix, mask

    return rows


async def test_the_guess_reads_an_unowned_film_off_the_cached_fit(db, world):
    """The seed list is mostly unowned, with no `ledger_state` row; the cached fit still places
    them (§5.2). Two films pointing opposite ways must differ, or `mu` for all would pass."""
    user = world["user"]
    for title_id, name in ((98, "Pointing with them"), (99, "Pointing against them")):
        await db.execute(
            "INSERT INTO title (id, kind, name, is_owned) VALUES ($1, 'movie', $2, false)",
            title_id, name,
        )
    signal = {1: -1.0, 2: -1.0, 3: -1.0, 4: 0.0, 5: 1.0, 6: 1.0, 98: 1.0, 99: -1.0}
    await place(db, user, {1: 0, 2: 1, 3: 1, 4: 3, 5: 5, 6: 6})
    src = _embeddings_from(signal)
    report = await refit.refit_user(db, user_id=user, kind="movie", hp=HP, embeddings=src)
    assert report.fitted, report.error
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND title_id IN (98, 99)", user
    ) == 0, "unowned and unplaced, so the fit wrote them no row"

    echoed = {}
    for title_id in (98, 99):
        s = await put(db, await session.open_or_resume(db, user_id=user, kinds=["movie"]), title_id)
        out = await session.record_placement(
            db, s, card_token=token(s), tier=3, hp=HP, embeddings=src
        )
        echoed[title_id] = out.echo["model"]
    assert echoed[98]["cdf"] > echoed[99]["cdf"], echoed
    assert WORDS.index(echoed[98]["guess_word"]) >= WORDS.index(echoed[99]["guess_word"])


async def test_a_re_ask_is_written_with_the_edit_it_re_asks_and_counts_toward_the_block(db, world):
    """§13 stream (b): marked in the row, invisible on the wire, one of the 15; the fit skips a
    same-step answer and a different one moves the film (plan reading 10)."""
    user = world["user"]
    await place(db, user, {1: 5, 2: 5})
    first = {
        r["title_id"]: r["id"]
        for r in await db.fetch("SELECT id, title_id FROM tier_edit WHERE user_id = $1", user)
    }
    s = await put(db, await session.open_or_resume(db, user_id=user, kinds=["movie"]), 1,
                  source="reask", reason=queue.SEEN_REASON, p_seen=1.0, reask_of=first[1])
    card = await session.public_card(db, s, version=None)
    assert "reask" not in json.dumps(card) and "model" not in card

    s = (await session.record_placement(db, s, card_token=token(s), tier=5, hp=HP)).session
    assert s.slot == 2, "a re-ask counts toward the 15"
    s = await put(db, s, 2, source="reask", reason=queue.SEEN_REASON, p_seen=1.0, reask_of=first[2])
    s = (await session.record_placement(db, s, card_token=token(s), tier=2, hp=HP)).session
    assert s.slot == 3

    rows = await db.fetch(
        "SELECT title_id, tier, reask_of FROM tier_edit WHERE user_id = $1 AND reask_of IS NOT NULL",
        user,
    )
    assert {(r["title_id"], r["tier"], r["reask_of"]) for r in rows} == {
        (1, 5, first[1]), (2, 2, first[2]),
    }
    obs = await observations.load_observations(db, user_id=user, kind="movie", hp=HP)
    assert obs.n_tier_edits == 3 and obs.n_reask == 1, "the same-step answer is not a second label"
    assert (await ladder.placements(db, user_id=user, kind="movie"))[2] == 2, "a new step moves it"


# --- the block -----------------------------------------------------------------------------------


async def test_the_block_ends_at_fifteen_with_the_films_still_waiting(db, db_rated_before_world):
    """§6.1's block end, counted in the films rated before the set-up that still wait for a step."""
    user = db_rated_before_world
    s = await open_session(db, user)
    for n in range(15):
        assert s.current_card["reason"] == queue.RATED_BEFORE_REASON, n
        assert (await session.payload(db, s))["done"] is None, n
        s = (await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP)).session

    assert (s.block_index, s.slot) == (1, 1)
    body = await session.payload(db, s)
    assert body["done"] == {"rated_before": 2, "noun": "films"}
    assert body["setup"]["rated_before"] == 2
    assert body["card"] is not None, "Rate 15 more serves the card already on the table"

    s = (await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP)).session
    assert (await session.payload(db, s))["done"] is None


@pytest.fixture
async def db_rated_before_world(db):
    """Seventeen films rated before the set-up and three more: a block leaves two waiting."""
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    user = await insert_user(db, "patrick", "admin")
    for title_id in range(1, 18):
        await rated_before(db, user, title_id)
    await set_up(db, user)
    return user


async def test_undo_takes_back_a_placement_its_verdict_and_its_seen_and_restores_the_card(db, world):
    """The card that comes back is the one that produced the observation, under a fresh token."""
    user = world["user"]
    await place(db, user, {1: 6, 2: 5, 3: 2, 4: 1})
    await seed_ledger(db, user, range(1, 21), fitted=True)
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]
    card_before = dict(s.current_card)

    s = (await session.record_placement(db, s, card_token=token(s), tier=6, hp=HP)).session
    assert await session.undo_availability(db, s) == {
        "available": True, "kind": "placement", "name": f"Title {title_id}",
    }
    out = await session.undo(db, s, hp=HP)
    s = out.session
    assert out.undone == "placement" and out.ledger["applied"] is True
    for table in ("tier_edit", "verdict"):
        assert await db.fetchval(
            f"SELECT count(*) FROM {table} WHERE user_id = $1 AND title_id = $2", user, title_id
        ) == 0, table
    assert await db.fetchval(
        "SELECT count(*) FROM user_title WHERE user_id = $1 AND title_id = $2", user, title_id
    ) == 0, "the implied `seen` went back to the absence it came from"
    assert s.current_card == card_before
    assert (s.block_index, s.slot) == (0, 1)
    assert await session.undo_availability(db, s) == {"available": False, "kind": None, "name": None}


async def test_undo_of_a_placement_makes_the_earlier_verdict_live_again(db, db_rated_before_world):
    """The placement's verdict supersedes the history's; its undo splices the chain back."""
    user = db_rated_before_world
    original = await db.fetchval("SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", user)
    s = await put(db, await session.open_or_resume(db, user_id=user, kinds=["movie"]), 1)
    s = (await session.record_placement(db, s, card_token=token(s), tier=0, hp=HP)).session
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original)

    await session.undo(db, s, hp=HP)
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original) is None
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", user
    ) == "seen", "a film seen before keeps its seen state"


async def test_undo_takes_back_a_not_seen(db, world):
    user = world["user"]
    s = await open_session(db, user)
    card_before = dict(s.current_card)
    s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert (await session.undo_availability(db, s))["kind"] == "not_seen"

    out = await session.undo(db, s, hp=HP)
    assert out.undone == "not_seen" and out.ledger is None
    assert out.session.current_card == card_before
    assert await db.fetchval(
        "SELECT count(*) FROM user_title WHERE user_id = $1 AND state = 'unseen'", user
    ) == 0


async def test_undo_stops_at_the_block_boundary_and_reports_it_rather_than_no_opping(db, world):
    """Decision 199: a block commits when the next block's first tap lands, so retracting the 16th
    does not hand block 0 back."""
    user = world["user"]
    s = await open_session(db, user)
    with pytest.raises(session.UndoUnavailable) as empty:
        await session.undo(db, s, hp=HP)
    assert empty.value.reason == "empty"

    for _ in range(16):
        s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert (s.block_index, s.slot) == (1, 2), "fifteen taps rolled the block and one landed in it"

    s = (await session.undo(db, s, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1), "the new block's only observation came back"
    assert (await session.undo_availability(db, s))["available"] is False
    with pytest.raises(session.UndoUnavailable) as refused:
        await session.undo(db, s, hp=HP)
    assert refused.value.reason == "block_boundary"
    assert await db.fetchval(
        "SELECT count(*) FROM rate_observation WHERE session_id = $1 AND undone_at IS NOT NULL",
        s.id,
    ) == 1, "a refused undo compensates nothing: only the sixteenth tap is tombstoned"


async def test_the_fifteenth_tap_stays_undoable_until_the_sixteenth_lands(db, world):
    user = world["user"]
    s = await open_session(db, user)
    for _ in range(14):
        s = (await session.record_placement(db, s, card_token=token(s), tier=3, hp=HP)).session
    fifteenth = s.current_card["title_id"]
    s = (await session.record_placement(db, s, card_token=token(s), tier=0, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1), "the fifteenth tap rolled the counter"
    assert (await session.undo_availability(db, s))["available"] is True, (
        "the tap the person can still see on screen has to be retractable"
    )

    s = (await session.undo(db, s, hp=HP)).session
    assert (s.block_index, s.slot) == (0, 15)
    assert s.current_card["title_id"] == fifteenth
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND title_id = $2", user, fifteenth
    ) == 0


async def test_undo_walks_back_to_the_first_observation_of_the_block_and_then_refuses(db, world):
    user = world["user"]
    await place(db, user, {20: 6})
    s = await open_session(db, user)
    for tier in (0, 3, 6):
        s = (await session.record_placement(db, s, card_token=token(s), tier=tier, hp=HP)).session
    for expected_slot in (3, 2, 1):
        s = (await session.undo(db, s, hp=HP)).session
        assert s.slot == expected_slot
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user) == 1, (
        "the placement from outside this block is untouched; the three from it are gone"
    )
    with pytest.raises(session.UndoUnavailable):
        await session.undo(db, s, hp=HP)


# --- the queue's card, the shelves, the session -------------------------------------------------


async def test_the_card_shows_the_persons_own_placed_films_on_their_shelves(db, world):
    """Seven shelves best first; an empty one keeps its word; never the film being placed."""
    user = world["user"]
    await place(db, user, {1: 6, 2: 0, 3: 6})
    s = await put(db, await session.open_or_resume(db, user_id=user, kinds=["movie"]), 4)
    shelves = (await session.public_card(db, s, version=None))["shelves"]
    assert [shelf["tier"] for shelf in shelves] == [6, 5, 4, 3, 2, 1, 0]
    by_tier = {shelf["tier"]: shelf for shelf in shelves}
    assert sorted(f["id"] for f in by_tier[6]["films"]) == [1, 3] and by_tier[6]["count"] == 2
    assert [f["id"] for f in by_tier[0]["films"]] == [2]
    assert by_tier[3] == {"tier": 3, "word": "It was fine", "count": 0, "films": []}
    assert set(by_tier[0]["films"][0]) == {
        "id", "name", "original_name", "original_language", "poster_path",
    }

    s = await put(db, s, 1)
    six = (await session.public_card(db, s, version=None))["shelves"][0]
    assert [f["id"] for f in six["films"]] == [3] and six["count"] == 1


async def test_a_placed_film_never_returns_unless_it_is_pinned_and_then_it_is_placed_anew(db, world):
    """A rewatch from the finish prompt is a new placement, not a re-ask (decision 550)."""
    user = world["user"]
    await place(db, user, {7: 2})
    s = await open_session(db, user)
    served = set()
    while s.current_card is not None:
        served.add(s.current_card["title_id"])
        s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert 7 not in served and len(served) == 19

    s = await session.ensure_card(db, s, head=[7])
    assert s.current_card["title_id"] == 7 and s.current_card["reask_of"] is None
    await session.record_placement(db, s, card_token=token(s), tier=6, hp=HP)
    assert [r["reask_of"] for r in await db.fetch(
        "SELECT reask_of FROM tier_edit WHERE user_id = $1 AND title_id = 7 ORDER BY id", user
    )] == [None, None]
    assert (await ladder.placements(db, user_id=user, kind="movie"))[7] == 6


async def test_the_queue_drained_says_so_and_preloads_nothing(db):
    await make_titles(db, [(1, "movie", "Heat"), (2, "movie", "Drive")])
    user = await insert_user(db, "patrick", "admin")
    await set_up(db, user)
    s = await open_session(db, user)
    first = await session.payload(db, s)
    assert first["drained"] is None and len(first["preload"]) >= 1
    for _ in range(2):
        s = (await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP)).session
    body = await session.payload(db, s)
    assert body["card"] is None and body["preload"] == []
    assert body["drained"] == {"line": session.DRAINED_LINE}


async def test_the_preload_names_the_next_cards_art(db, world):
    """§6: the next card preloaded: its poster first, then its shelves' posters, from this origin."""
    user = world["user"]
    await place(db, user, {19: 6, 20: 1})
    s = await open_session(db, user)
    body = await session.payload(db, s)
    assert body["preload"][0].startswith("/api/art/") and body["preload"][0].endswith("/poster")
    assert set(body["preload"][1:]) == {"/api/art/19/poster", "/api/art/20/poster"}

    s = (await session.record_placement(db, s, card_token=token(s), tier=3, hp=HP)).session
    assert body["preload"][0] == f"/api/art/{s.current_card['title_id']}/poster"


async def test_one_live_session_per_person_and_a_resume_returns_the_same_card(db, world):
    """Two live sessions would each hold a counter, and Undo would have to guess."""
    user = world["user"]
    first = await open_session(db, user)
    again = await session.ensure_card(db, await session.open_or_resume(db, user_id=user))
    assert (again.id, again.card_token, again.current_card) == (
        first.id, first.card_token, first.current_card,
    )
    restarted = await session.open_or_resume(db, user_id=user, restart=True)
    assert restarted.id != first.id
    assert await db.fetchval(
        "SELECT count(*) FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user
    ) == 1


async def test_rate_asks_about_one_kind_at_a_time(db, world):
    """Plan reading 13: films unless switched; the switch drops the card and keeps the block."""
    user = world["user"]
    await make_titles(db, [(90, "series", "A Series")])
    s = await open_session(db, user)
    assert s.kinds == ["movie"] and s.kind == "movie"
    s = (await session.record_not_seen(db, s, card_token=token(s))).session

    s = await session.set_kinds(db, s, ["series"])
    assert s.current_card is None and s.slot == 2
    s = await session.ensure_card(db, s)
    assert s.current_card == {**s.current_card, "kind": "series", "title_id": 90}
    for kinds in ([], ["movie", "series"]):
        with pytest.raises(ValueError):
            await session.set_kinds(db, s, kinds)


async def test_a_pin_lifts_an_earlier_not_seen(db, world):
    user = world["user"]
    s = await open_session(db, user)
    not_seen = s.current_card["title_id"]
    s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert s.current_card["title_id"] != not_seen

    s = await session.ensure_card(db, s, head=[not_seen])
    assert s.current_card["title_id"] == not_seen
    assert s.current_card["reason"] == queue.PINNED_REASON


async def test_a_banner_redraw_never_replaces_a_card_it_did_not_read(db, world):
    """Asserted as the state the race produces: over the route it split once in 40."""
    user = world["user"]
    s = await open_session(db, user)
    pinned = next(i for i in range(1, 21) if i != s.current_card["title_id"])
    stale = s  # the snapshot the second device is holding when the first one's redraw lands

    s = await session.ensure_card(db, s, head=[pinned])
    assert s.current_card["title_id"] == pinned
    winner = str(s.card_token)
    assert winner != str(stale.card_token)

    loser = await session.ensure_card(db, stale, head=[pinned])
    assert str(loser.card_token) == winner, (
        "the second device was handed a token the session does not carry"
    )
    out = await session.record_placement(db, loser, card_token=winner, tier=2, hp=HP)
    assert out.session.slot == 2


async def test_a_tap_that_dies_between_its_two_statements_leaves_no_phantom_journal_row(
    db, world, monkeypatch
):
    """`_append` is an INSERT then an UPDATE; a death between them must not wedge the session."""
    user = world["user"]
    s = await open_session(db, user)
    real = asyncpg.Connection.fetchrow

    async def die(self, query, *args, **kwargs):
        if "SET seq = $2" in query:
            raise RuntimeError("the task was cancelled between the journal row and the cursor")
        return await real(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", die)
    with pytest.raises(RuntimeError):
        await session.record_not_seen(db, s, card_token=token(s))
    monkeypatch.undo()

    assert await db.fetchval("SELECT count(*) FROM rate_observation") == 0
    s = await session.open_or_resume(db, user_id=user)
    assert (await session.record_not_seen(db, s, card_token=token(s))).session.slot == 2


# --- Jellyfin ------------------------------------------------------------------------------------


@pytest.fixture
async def linked(db, fake_jellyfin, secrets_key):
    """The person's own token: §7.3's least-privilege write path, not the admin key."""
    module, transport = fake_jellyfin
    owned = [item["Id"].removeprefix("jf-") for item in module.ITEMS if item["Id"] != "jf-x"]
    await make_titles(db, [(int(i), "movie", f"Title {i}") for i in owned])
    await db.execute("UPDATE title SET jellyfin_id = 'jf-' || id")
    user = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', 'jf-user-patrick', 'linked') RETURNING id"
    )
    await set_up(db, user)
    client = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)
    _jf_id, jf_token = await client.authenticate_by_name("patrick", module.PASSWORD)
    return {
        "module": module,
        "user": user,
        "jf": session.Jellyfin(
            client=client,
            cfg=JellyfinConfig(
                url="http://jellyfin.test",
                api_key=module.API_KEY,
                user_tokens={str(user): jf_token},
            ),
        ),
    }


async def _seen_first(db, user: int) -> int:
    """A recorded-seen film leads the queue; an explicit row, so a retraction has a prior."""
    title_id = await db.fetchval("SELECT min(id) FROM title")
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at) "
        "VALUES ($1, $2, 'seen', now(), now())",
        user, title_id,
    )
    return title_id


async def test_an_undo_of_a_not_seen_hands_back_the_played_flag_it_set(db, linked):
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    target = await _seen_first(db, user)
    s = await open_session(db, user)
    assert s.current_card["title_id"] == target

    s = (await session.record_not_seen(db, s, card_token=token(s), jf=jf)).session
    assert module.state.write_log[-1] == {
        "user": "jf-user-patrick", "item": f"jf-{target}", "played": False
    }
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert stored[0]["pushed"] is True, "Undo reads this flag to hand the Played flag back"

    await session.undo(db, s, hp=HP, jf=jf)
    assert module.state.write_log[-1] == {
        "user": "jf-user-patrick", "item": f"jf-{target}", "played": True
    }
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, target
    ) == "seen"


async def test_undo_pushes_nothing_where_the_forward_action_pushed_nothing(db, linked):
    """Undo compensates what it did, not what it intended."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    await db.execute("UPDATE title SET jellyfin_id = NULL")
    s = await open_session(db, user)
    s = (await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP, jf=jf)).session
    assert module.state.write_log == []
    await session.undo(db, s, hp=HP, jf=jf)
    assert module.state.write_log == []


async def test_with_no_connector_the_push_is_owed_rather_than_lost(db, world):
    """A present row with NULL `jf_synced_at` means Jellyfin is owed the change (§7.3)."""
    user = world["user"]
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]
    out = await session.record_placement(db, s, card_token=token(s), tier=4, hp=HP)
    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = $2",
        user, title_id,
    )
    assert row["state"] == "seen" and row["jf_synced_at"] is None
    assert any("not pushed" in line for line in out.log)


async def test_the_journal_records_the_push_that_happened_and_not_the_one_intended(db, linked):
    """`prior_state.pushed` is corrected after the push resolves; `undo` reads exactly it."""
    user, jf = linked["user"], linked["jf"]
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]
    out = await session.record_placement(db, s, card_token=token(s), tier=5, hp=HP, jf=jf)
    assert any("Jellyfin Played true" in line for line in out.log)
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert [e["title_id"] for e in stored] == [title_id] and stored[0]["pushed"] is True

    await db.execute("UPDATE title SET jellyfin_id = NULL")
    s = out.session
    out = await session.record_placement(db, s, card_token=token(s), tier=0, hp=HP, jf=jf)
    assert any("not pushed (not on Jellyfin)" in line for line in out.log)
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert stored[0]["pushed"] is False, "an owed push must not be recorded as a push"


async def test_an_undo_whose_token_has_expired_asks_for_a_re_link(db, linked):
    """§7.3: a 401 on the retraction's write also asks for a re-link."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    target = await _seen_first(db, user)
    s = await open_session(db, user)
    s = (await session.record_not_seen(db, s, card_token=token(s), jf=jf)).session
    assert module.state.write_log[-1]["played"] is False

    expired = session.Jellyfin(
        client=jf.client,
        cfg=JellyfinConfig(
            url=jf.cfg.url, api_key=jf.cfg.api_key, user_tokens={str(user): "expired-token"}
        ),
    )
    await session.undo(db, s, hp=HP, jf=expired)
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", user
    ) == "needs_relink"
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, target
    ) == "seen", "the app-side row is restored either way"


class _SlowPlayed(httpx.AsyncBaseTransport):
    """The fake Jellyfin with §7.3's one write made slow — §3.3's bad-weather case."""

    def __init__(self, inner: httpx.AsyncBaseTransport, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "UserPlayedItems" in request.url.path:
            await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


async def _second_connection(pg_url: str) -> asyncpg.Connection:
    """Two coroutines on one connection are not concurrent. Installs `db`'s jsonb codecs."""
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


def _slow(linked, fake_jellyfin, delay: float = 0.5) -> session.Jellyfin:
    module, transport = fake_jellyfin
    return session.Jellyfin(
        client=JellyfinClient(
            "http://jellyfin.test", module.API_KEY, transport=_SlowPlayed(transport, delay)
        ),
        cfg=linked["jf"].cfg,
    )


async def test_the_placement_is_durable_before_jellyfin_answers(db, pg_url, fake_jellyfin, linked):
    """The app-side write commits before the network call; no transaction waits on Jellyfin."""
    user, module = linked["user"], linked["module"]
    jf = _slow(linked, fake_jellyfin)
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]

    watcher = await _second_connection(pg_url)
    try:
        started = time.monotonic()
        tap = asyncio.create_task(
            session.record_placement(db, s, card_token=token(s), tier=5, hp=HP, jf=jf)
        )
        visible: float | None = None
        held = 0
        while not tap.done():
            await asyncio.sleep(0.01)
            if visible is None and await watcher.fetchval(
                "SELECT count(*) FROM tier_edit WHERE user_id = $1 AND title_id = $2",
                user, title_id,
            ):
                visible = time.monotonic() - started
            held = max(
                held,
                await watcher.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND state = 'idle in transaction' "
                    "AND clock_timestamp() - xact_start > interval '0.3 seconds' "
                    "AND pid <> pg_backend_pid()"
                ),
            )
        out = await tap
        elapsed = time.monotonic() - started
    finally:
        await watcher.close()

    assert elapsed > 0.4, f"the tap took {elapsed:.2f} s, so the slow transport was never reached"
    assert visible is not None and visible < 0.25, (
        f"the placement was invisible to a second connection for {visible!r} s of {elapsed:.2f} s"
    )
    assert held == 0, "a backend held an open transaction across a foreign server's latency"
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True}
    ]
    assert any("Jellyfin Played true" in line for line in out.log)


async def test_a_handed_off_push_answers_the_tap_first_and_settles_the_same_bookkeeping(
    db, pg_url, fake_jellyfin, linked
):
    """With `later` the tap answers first and the push settles on its own pooled connection, with
    the same stamp, `prior_state.pushed` correction (decision 207) and rail line."""
    from spielplan.db import pool as db_pool

    await db_pool.open_pool(pg_url, min_size=1, max_size=2)
    user, module = linked["user"], linked["module"]
    jf = _slow(linked, fake_jellyfin)
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]
    rail.forget(user_id=user)

    started = time.monotonic()
    out = await session.record_placement(
        db, s, card_token=token(s), tier=5, hp=HP, jf=jf, later=session.settle_in_background
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.4, f"the tap waited {elapsed:.2f} s for a push that sleeps 0.5 s"
    assert "user_title.state = seen -> Jellyfin push follows" in out.log
    assert out.session.current_card is not None
    assert module.state.write_log == []

    await session.settled()
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True}
    ]
    assert await db.fetchval(
        "SELECT jf_synced_at IS NOT NULL FROM user_title WHERE user_id = $1 AND title_id = $2",
        user, title_id,
    ) is True
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert stored[0]["pushed"] is True
    events = rail.recent(user_id=user)
    assert "user_title.state = seen -> Jellyfin Played true" in [e["text"] for e in events]
    assert {e["kind"] for e in events} == {"tier_edit"}


async def test_an_undo_taken_while_the_push_is_in_flight_still_hands_the_played_flag_back(
    db, pg_url, fake_jellyfin, linked
):
    """`undo`'s `FOR UPDATE` on the journal row makes the repair hold in both interleavings."""
    user, module = linked["user"], linked["module"]
    jf = _slow(linked, fake_jellyfin)
    s = await open_session(db, user)
    title_id = s.current_card["title_id"]
    # An explicit prior `unseen`: decision 210 makes `seen.retract` refuse a prior of no row.
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, state_changed_at, jf_synced_at) "
        "VALUES ($1, $2, 'unseen', now(), now())",
        user, title_id,
    )

    other = await _second_connection(pg_url)
    try:
        tap = asyncio.create_task(
            session.record_placement(db, s, card_token=token(s), tier=5, hp=HP, jf=jf)
        )
        while not await other.fetchval(
            "SELECT count(*) FROM rate_observation WHERE user_id = $1", user
        ):
            await asyncio.sleep(0.01)
        assert not tap.done(), "the push had already resolved, so this raced nothing"
        popped = await session.undo(
            other, await session.open_or_resume(other, user_id=user), hp=HP, jf=jf
        )
        assert popped.undone == "placement"
        await tap
    finally:
        await other.close()

    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user) == 0
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True},
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": False},
    ], f"the tap's Played write was never handed back: {module.state.write_log}"


async def test_the_loser_of_a_double_tap_never_tells_jellyfin(db, pg_url, linked, monkeypatch):
    """Counting `record_tier_edit` tells the lock from its backstop. The fake refuses unknown ids,
    so attempts reuse its items."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    observed = 0
    real_record = observations.record_tier_edit

    async def counting(*args, **kwargs):
        nonlocal observed
        observed += 1
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(observations, "record_tier_edit", counting)
    other = await _second_connection(pg_url)
    try:
        for attempt in range(RACES):
            for table in ("rate_observation", "rate_session", "verdict", "tier_edit", "user_title"):
                await db.execute(f"DELETE FROM {table}")
            s = await open_session(db, user)
            results = await asyncio.gather(
                session.record_placement(db, s, card_token=token(s), tier=6, hp=HP, jf=jf),
                session.record_placement(other, s, card_token=token(s), tier=0, hp=HP, jf=jf),
                return_exceptions=True,
            )
            assert len(module.state.write_log) == attempt + 1, module.state.write_log
            assert observed == attempt + 1, (
                f"attempt {attempt + 1}: the losing tap reached the ledger before its refusal"
            )
            refused = [r for r in results if isinstance(r, BaseException)]
            assert len(refused) == 1 and isinstance(refused[0], session.StaleCard), results
            assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user) == 1
    finally:
        await other.close()


async def test_handed_off_pushes_behind_a_hung_jellyfin_leave_the_pool_to_the_requests(db, pg_url):
    """At most `SETTLE_SLOTS` pushes hold a connection; the rest wait in memory."""
    from spielplan.db import pool as db_pool

    await db_pool.open_pool(pg_url)
    live = db_pool.pool()
    running, most, done = 0, 0, []

    async def hung_push(conn: asyncpg.Connection) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await conn.fetchval("SELECT 1")
        await asyncio.sleep(0.2)
        running -= 1
        done.append(1)

    for _ in range(10):
        session.settle_in_background(hung_push)
    await asyncio.sleep(0.05)
    assert live.get_size() - live.get_idle_size() <= session.SETTLE_SLOTS
    request = await live.acquire(timeout=0.5)
    await live.release(request)
    await session.settled()
    assert most == session.SETTLE_SLOTS and len(done) == 10


async def test_a_handed_off_push_that_gets_no_connection_is_left_owed_and_raises_nothing(
    db, pg_url, monkeypatch, caplog
):
    from spielplan.db import pool as db_pool

    monkeypatch.setattr(session, "SETTLE_ACQUIRE_TIMEOUT_S", 0.2)
    await db_pool.open_pool(pg_url, min_size=1, max_size=1)
    live = db_pool.pool()
    ran = []

    async def push(conn: asyncpg.Connection) -> None:
        ran.append(1)

    held = await live.acquire()
    try:
        session.settle_in_background(push)
        await session.settled()
    finally:
        await live.release(held)
    assert ran == []
    assert "the seen-state sweep still owes it" in caplog.text


async def test_a_stop_lets_the_handed_off_pushes_land_before_the_pool_closes(
    db, pg_url, tmp_path, monkeypatch
):
    from spielplan.core.config import settings
    from spielplan.models import basis

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    neutral = tmp_path / "no-dot-env"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    monkeypatch.setattr(basis, "FOLLOW_SECONDS", None)
    settings.cache_clear()
    ran = []

    async def push(conn: asyncpg.Connection) -> None:
        await asyncio.sleep(0.2)
        await conn.fetchval("SELECT 1")
        ran.append(1)

    from spielplan.app import create_app

    application = create_app()
    try:
        async with application.router.lifespan_context(application):
            for _ in range(session.SETTLE_SLOTS + 2):
                session.settle_in_background(push)
    finally:
        settings.cache_clear()
    assert len(ran) == session.SETTLE_SLOTS + 2


def test_the_rail_never_reports_a_jellyfin_write_for_a_series_settled_app_only():
    """Decision 533: the push reports success with a reason, and nothing reached Jellyfin."""
    line = session._sync_line("seen", True, "series seen is app-only")
    assert "Jellyfin Played" not in line and "app-only" in line
    assert session._sync_line("seen", True, None).endswith("Jellyfin Played true")


# --- the routes (contract C7) --------------------------------------------------------------------

ENVELOPE = {"setup", "session", "card", "preload", "echo", "done", "drained", "undo"}


async def _admin(app) -> tuple[Any, int]:
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    return client, (await client.get("/api/auth/me")).json()["id"]


@pytest.fixture
async def rate_client(app, db):
    client, user_id = await _admin(app)
    await set_up(db, user_id)
    return client, user_id


async def test_before_the_set_up_rate_is_closed_and_opens_no_session(db, app):
    """Decision 550: one closed card and nothing else; every Rate write waits for the set-up."""
    client, user_id = await _admin(app)
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await rated_before(db, user_id, 1)
    await observations.record_tier_edit(db, user_id=user_id, title_id=2, tier=3)

    closed = await client.get("/api/rate")
    assert closed.status_code == 200
    assert closed.json() == {
        "setup": {"done": False, "earlier_ratings": 2, "rated_before": 0},
        "session": None, "card": None, "preload": [], "echo": None, "done": None,
        "drained": None, "undo": {"available": False, "kind": None, "name": None},
    }
    assert await db.fetchval("SELECT count(*) FROM rate_session") == 0

    for method, path, body in (
        ("POST", "/api/rate/session", {"kinds": ["movie"]}),
        ("POST", "/api/rate/place", {"card_token": "x", "tier": 3}),
        ("POST", "/api/rate/not-seen", {"card_token": "x"}),
        ("POST", "/api/rate/undo", None),
        ("GET", "/api/rate/shelves?title_id=1", None),
    ):
        refused = await client.request(method, path, json=body)
        assert refused.status_code == 409, f"{method} {path}: {refused.text}"
        assert refused.json()["detail"]["reason"] == "not_set_up", path
    assert await db.fetchval("SELECT count(*) FROM rate_session") == 0

    # The title card's Not seen still works, journalled through a session that draws nothing.
    answered = await client.post("/api/rate/title/3", json={"answer": "not_seen"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["card"] is None and answered.json()["setup"]["done"] is False
    assert await db.fetchval(
        "SELECT current_card IS NULL FROM rate_session WHERE user_id = $1", user_id
    ) is True


async def test_the_route_serves_the_envelope_and_the_placement_answers_with_the_next_card(
    db, rate_client
):
    """The next card rides in the write's response (§6: next card preloaded); C7's shape."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await seed_ledger(db, user_id, range(1, 9))

    first = (await client.get("/api/rate")).json()
    assert set(first) == ENVELOPE, "with Show the model off the gated keys are absent"
    assert first["setup"] == {"done": True, "earlier_ratings": 0, "rated_before": 0}
    assert first["session"] == {
        "kinds": ["movie"], "kind": "movie",
        "block": {"slot": 1, "size": 15, "counter": "1 of 15"},
    }
    card = first["card"]
    assert set(card) == {"token", "kind", "title", "reason", "shelves"}
    assert set(card["title"]) == {
        "id", "name", "original_name", "original_language", "year", "runtime_min", "poster_path",
    }
    assert first["undo"] == {"available": False, "kind": None, "name": None}
    assert (first["echo"], first["done"], first["drained"]) == (None, None, None)
    assert_no_model_belief(card)
    assert (await client.get("/api/rate")).json()["card"]["token"] == card["token"], "idempotent"

    answered = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})
    assert answered.status_code == 200, answered.text
    body = answered.json()
    name = card["title"]["name"]
    assert body["echo"] == {"title_id": card["title"]["id"], "name": name, "tier": 5,
                            "word": "Loved it"}, "no guess with Show the model off"
    assert body["session"]["block"]["slot"] == 2
    assert body["card"] is not None and body["card"]["token"] != card["token"]
    assert body["undo"] == {"available": True, "kind": "placement", "name": name}
    assert str(MARKER_CDF) not in json.dumps(body)

    await client.post("/api/auth/preferences", json={"show_model": True})
    nxt = body["card"]
    body = (await client.post("/api/rate/place", json={"card_token": nxt["token"], "tier": 1})).json()
    assert body["echo"]["model"] == {"guess_word": "Loved it", "cdf": round(MARKER_CDF, 2)}
    assert set(body) == ENVELOPE | {"ledger", "log"}


async def test_the_route_refuses_a_stale_card_a_bad_step_and_an_empty_undo(db, rate_client):
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    card = (await client.get("/api/rate")).json()["card"]

    undo = await client.post("/api/rate/undo")
    assert undo.status_code == 409
    assert undo.json()["detail"] == {"reason": "nothing_to_undo", "message": "Nothing to undo yet"}

    bad = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 7})
    assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "bad_tier"
    assert (await client.post(
        "/api/rate/place", json={"card_token": card["token"], "tier": 2}
    )).status_code == 200
    for stale_token in (card["token"], "3f0d3a1e-0000-4000-8000-000000000000"):
        stale = await client.post("/api/rate/place", json={"card_token": stale_token, "tier": 2})
        assert stale.status_code == 409
        assert stale.json()["detail"]["reason"] == "stale_card"
    assert await db.fetchval("SELECT count(*) FROM tier_edit") == 1


async def test_the_session_route_switches_the_kind_and_refuses_two(db, rate_client):
    client, _user_id = rate_client
    await make_titles(db, [(1, "movie", "Heat"), (90, "series", "A Series")])
    assert (await client.get("/api/rate")).json()["card"]["kind"] == "movie"

    series = (await client.post("/api/rate/session", json={"kinds": ["series"]})).json()
    assert series["session"]["kind"] == "series" and series["card"]["title"]["id"] == 90
    both = await client.post("/api/rate/session", json={"kinds": ["movie", "series"]})
    assert both.status_code == 422 and both.json()["detail"]["reason"] == "bad_kinds"
    assert (await client.post("/api/rate/session", json={"kinds": []})).status_code == 422
    assert (await client.delete("/api/rate/session")).json() == {"ended": True}


async def test_the_shelves_route_is_the_title_cards_ladder_sheet(db, rate_client):
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await place(db, user_id, {1: 6, 2: 4})

    sheet = (await client.get("/api/rate/shelves", params={"title_id": 2})).json()
    assert sheet["title_id"] == 2 and sheet["kind"] == "movie"
    assert sheet["current"] == {"tier": 4, "word": "Liked it"}
    assert [s["tier"] for s in sheet["shelves"]] == [6, 5, 4, 3, 2, 1, 0]
    assert [f["id"] for f in sheet["shelves"][0]["films"]] == [1]
    assert sheet["shelves"][2]["films"] == [], "never the film itself"

    unplaced = (await client.get("/api/rate/shelves", params={"title_id": 3})).json()
    assert unplaced["current"] is None
    assert (await client.get("/api/rate/shelves", params={"title_id": 999})).status_code == 404


async def test_the_rate_routes_need_a_signed_in_account(app, db):
    client = app()
    assert (await client.get("/api/rate")).status_code == 401
    assert (
        await client.post("/api/rate/place", json={"card_token": "x", "tier": 1})
    ).status_code == 401


async def test_the_retired_routes_are_gone(rate_client):
    client, _user_id = rate_client
    for path in ("/api/rate/verdict", "/api/rate/skip", "/api/rate/duel", "/api/rate/correction"):
        assert (await client.post(path, json={})).status_code in (404, 405), path
    assert (await client.get("/api/rate/search", params={"q": "x"})).status_code in (404, 405)


async def test_a_placement_reaches_the_rail_in_section_6_7s_form(db, rate_client):
    """Person, film and milliseconds meet in one line; the films carry no digits, so any digit in
    the line is the renderer's."""
    films = ["Heat", "Drive", "Ronin", "Collateral", "Thief", "Sicario", "Zodiac", "Michael Clayton"]
    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", films[i - 1]) for i in range(1, 9)])
    await place(db, user_id, {1: 6, 2: 5, 3: 2, 4: 1})
    await seed_ledger(db, user_id, range(1, 9), fitted=True)

    card = (await client.get("/api/rate")).json()["card"]
    assert rail.recent(user_id=user_id) == [], "serving a card is not a model write"
    body = (await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 5})).json()
    assert "log" not in body and "ledger" not in body, "decision 117 gates them off the wire"

    await client.post("/api/auth/preferences", json={"show_model": True})
    nxt = body["card"]
    body = (await client.post("/api/rate/place", json={"card_token": nxt["token"], "tier": 5})).json()
    assert body["ledger"]["applied"] is True
    ms = body["ledger"]["ms"]
    line = body["log"][0]
    assert line == f"tier_edit(patrick, {nxt['title']['name']} → A+, via=explicit) → tier arm" + (
        f", incremental refit {ms:.0f} ms"
    )
    events = [e for e in rail.recent(user_id=user_id) if e["text"].startswith("tier_edit(")]
    assert events[0]["text"] == line and events[0]["title_id"] == nxt["title"]["id"]
    assert {e["kind"] for e in rail.recent(user_id=user_id)} == {"tier_edit"}
    rail.forget()


async def test_a_cache_miss_says_the_fit_is_owed_rather_than_timing_one_that_never_ran(
    db, rate_client
):
    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await client.post("/api/auth/preferences", json={"show_model": True})

    card = (await client.get("/api/rate")).json()["card"]
    body = (await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 2})).json()
    assert body["ledger"]["applied"] is False and "queued" in body["ledger"]["reason"]
    assert "ms" not in body["ledger"]
    assert not any("refit" in line for line in body["log"]), body["log"]
    rail.forget()


async def test_two_long_names_on_the_rail_do_not_lose_the_placement(db, rate_client):
    """After the commit no route may raise: the card's token is spent, so a retry cannot succeed."""
    client, user_id = rate_client
    member = "Grandma's iPad in the living room and the one in the kitchen :-)"
    await db.execute("UPDATE app_user SET name = $1 WHERE id = $2", member, user_id)
    long_name = ("The Assassination of Jesse James by the Coward Robert Ford " * 6)[:310]
    await make_titles(db, [(1, "movie", long_name)])
    assert len(member) + len(long_name) + 60 > rail.MAX_LINE

    card = (await client.get("/api/rate")).json()["card"]
    rail.forget(user_id=user_id)
    answered = await client.post("/api/rate/place", json={"card_token": card["token"], "tier": 0})
    assert answered.status_code == 200, answered.text
    narrated = next(e for e in rail.recent(user_id=user_id) if e["text"].startswith("tier_edit("))
    assert len(narrated["text"]) <= rail.MAX_LINE
    assert member in narrated["text"] and long_name[:60] in narrated["text"]
    rail.forget(user_id=user_id)


async def test_the_cards_p_seen_travels_only_behind_show_the_model(db, rate_client):
    """Gated in the payload (decision 486). A card not placed by P(seen) has no `model` at all, so
    the key cannot tell a re-ask."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])

    card = (await client.get("/api/rate")).json()["card"]
    assert "model" not in card
    await client.post("/api/auth/preferences", json={"show_model": True})
    card = (await client.get("/api/rate")).json()["card"]
    assert 0.0 < card["model"]["p_seen"] < 1.0, card

    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 7, 'seen')", user_id
    )
    fresh = (await client.post("/api/rate/session", json={"restart": True})).json()["card"]
    assert fresh["title"]["id"] == 7 and fresh["reason"] == queue.SEEN_REASON
    assert "model" not in fresh, "a recorded-seen card looks exactly like a re-ask"


async def test_films_rated_before_the_set_up_come_first_without_their_old_answer(db, app):
    """Decision 550: "You rated this one before.", and the old answer is never shown."""
    client, user_id = await _admin(app)
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await rated_before(db, user_id, 4, value=0)
    await set_up(db, user_id)

    body = (await client.get("/api/rate")).json()
    assert body["setup"] == {"done": True, "earlier_ratings": 1, "rated_before": 1}
    assert body["card"]["title"]["id"] == 4
    assert body["card"]["reason"] == queue.RATED_BEFORE_REASON
    wire = json.dumps(body)
    assert "disliked" not in wire and "Not really" not in json.dumps(body["card"]["title"])


async def test_the_banner_cta_serves_a_named_title_even_over_a_standing_session(db, rate_client):
    """`GET /api/rate` is idempotent, which must not swallow a head over a stashed card."""
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 8)])
    standing = (await client.get("/api/rate")).json()["card"]
    named = next(i for i in range(1, 8) if i != standing["title"]["id"])
    pinned = (await client.get("/api/rate", params=[("head", named)])).json()["card"]
    assert pinned["title"]["id"] == named
    again = (await client.get("/api/rate", params=[("head", named)])).json()["card"]
    assert again["token"] == pinned["token"]
    assert (await client.get("/api/rate")).json()["card"]["token"] == pinned["token"]

    held = (await client.get("/api/rate", params=[("head", 12345)])).json()
    assert held["card"]["token"] == pinned["token"], "a head nobody can serve redraws nothing"


async def test_a_pin_of_the_other_kind_switches_the_session_to_serve_it(db, rate_client):
    """A finish prompt for a series lands on Rate's series, keeping §4.1 rule 5's partition."""
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 5)])
    await make_titles(db, [(90, "series", "A Series")])
    assert (await client.get("/api/rate")).json()["card"]["title"]["id"] != 90

    body = (await client.get("/api/rate", params=[("head", 90)])).json()
    assert body["card"]["title"]["id"] == 90
    assert body["session"]["kinds"] == ["series"]


# --- two devices -------------------------------------------------------------------------------


@pytest.fixture
async def two_devices(app, db):
    """Both pool connections open concurrently before any write: warming them in turn serialises."""
    phone, user_id = await _admin(app)
    await set_up(db, user_id)
    laptop = app()
    laptop.cookies.update(phone.cookies)
    await asyncio.gather(phone.get("/api/health"), laptop.get("/api/health"))
    return phone, laptop, user_id


async def _both(first, second) -> tuple[list[int], list[Any]]:
    """`return_exceptions=True`: an escaped exception is a 500 to the person, worth a loud message."""
    answers = await asyncio.gather(first, second, return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            pytest.fail(f"a tap escaped every handler in app.py: {answer!r}")
    return sorted(a.status_code for a in answers), list(answers)


def _refusal(answers: list[Any], attempt: int) -> None:
    refused = next(a for a in answers if a.status_code == 409)
    detail = refused.json()["detail"]
    assert isinstance(detail, dict), f"attempt {attempt + 1}: generic conflict handler ({detail!r})"
    assert detail["reason"] in STALE_REASONS, f"attempt {attempt + 1}: {detail}"


@pytest.mark.parametrize("tap", ("place", "not-seen"))
async def test_two_taps_on_one_card_token_leave_one_observation_and_one_409(db, two_devices, tap):
    """Eight attempts per tap, counts checked after each: two rows in one of eight is as broken."""
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 41)])
    body = {"place": {"tier": 4}, "not-seen": {}}[tap]

    for attempt in range(RACES):
        card = (await phone.get("/api/rate")).json()["card"]
        statuses, answers = await _both(
            phone.post(f"/api/rate/{tap}", json={"card_token": card["token"], **body}),
            laptop.post(f"/api/rate/{tap}", json={"card_token": card["token"], **body}),
        )
        assert statuses == [200, 409], f"attempt {attempt + 1}: {statuses}"
        _refusal(answers, attempt)
        assert await db.fetchval(
            "SELECT count(*) FROM rate_observation WHERE user_id = $1", user_id
        ) == attempt + 1, f"attempt {attempt + 1}: the journal took two rows for one card"
        assert await db.fetchval(
            "SELECT slot FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user_id
        ) == attempt + 2
        written = "tier_edit" if tap == "place" else "user_title"
        assert await db.fetchval(
            f"SELECT count(*) FROM {written} WHERE user_id = $1", user_id
        ) == attempt + 1


async def test_a_slow_jellyfin_does_not_widen_the_double_tap_window(db, two_devices, monkeypatch):
    real = session._push_state

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real(*args, **kwargs)

    monkeypatch.setattr(session, "_push_state", slow)
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 41)])

    for attempt in range(RACES):
        card = (await phone.get("/api/rate")).json()["card"]
        statuses, answers = await _both(
            phone.post("/api/rate/place", json={"card_token": card["token"], "tier": 2}),
            laptop.post("/api/rate/place", json={"card_token": card["token"], "tier": 2}),
        )
        assert statuses == [200, 409], f"attempt {attempt + 1}: {statuses}"
        _refusal(answers, attempt)
        assert await db.fetchval(
            "SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id
        ) == attempt + 1


async def test_two_concurrent_gets_on_an_empty_table_serve_one_card_under_one_token(
    db, two_devices
):
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    await phone.get("/api/rate")

    for attempt in range(RACES):
        await db.execute(
            "UPDATE rate_session SET current_card = NULL, card_token = NULL "
            "WHERE user_id = $1 AND ended_at IS NULL",
            user_id,
        )
        statuses, answers = await _both(phone.get("/api/rate"), laptop.get("/api/rate"))
        assert statuses == [200, 200], f"attempt {attempt + 1}: {statuses}"
        cards = [a.json()["card"] for a in answers]
        tokens = {c["token"] for c in cards}
        assert len(tokens) == 1, f"attempt {attempt + 1}: two tokens for one card ({tokens})"
        stored = await db.fetchval(
            "SELECT card_token FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user_id
        )
        assert tokens == {str(stored)}
        answered = await phone.post(
            "/api/rate/place", json={"card_token": cards[0]["token"], "tier": 3}
        )
        assert answered.status_code == 200, answered.text


async def test_the_rate_routes_hand_the_push_off_and_answer_without_waiting_for_it(
    db, rate_client, fake_jellyfin, monkeypatch
):
    from spielplan.api import rate as rate_routes

    client, user_id = rate_client
    module, transport = fake_jellyfin
    owned = [item["Id"].removeprefix("jf-") for item in module.ITEMS if item["Id"] != "jf-x"]
    await make_titles(db, [(int(i), "movie", f"Title {i}") for i in owned])
    await db.execute("UPDATE title SET jellyfin_id = 'jf-' || id")
    await db.execute(
        "UPDATE app_user SET jellyfin_user_id = 'jf-user-patrick', jellyfin_link_state = 'linked' "
        "WHERE id = $1",
        user_id,
    )
    plain = JellyfinClient("http://jellyfin.test", module.API_KEY, transport=transport)
    _jf_id, jf_token = await plain.authenticate_by_name("patrick", module.PASSWORD)
    jf = session.Jellyfin(
        client=JellyfinClient(
            "http://jellyfin.test", module.API_KEY, transport=_SlowPlayed(transport, 0.5)
        ),
        cfg=JellyfinConfig(
            url="http://jellyfin.test", api_key=module.API_KEY, user_tokens={str(user_id): jf_token}
        ),
    )

    async def slow_jellyfin(_conn):
        return jf

    monkeypatch.setattr(rate_routes, "_jellyfin", slow_jellyfin)
    await client.post("/api/auth/preferences", json={"show_model": True})
    card = (await client.get("/api/rate")).json()["card"]

    answered, asked = [], set()
    for path, body in (("/api/rate/place", {"tier": 5}), ("/api/rate/not-seen", {})):
        asked.add(card["title"]["id"])
        started = time.monotonic()
        reply = await client.post(path, json={"card_token": card["token"], **body})
        answered.append(time.monotonic() - started)
        assert reply.status_code == 200, reply.text
        assert any("Jellyfin push follows" in line for line in reply.json()["log"]), reply.json()
        card = reply.json()["card"]
    untouched = next(int(i) for i in owned if int(i) not in asked)
    started = time.monotonic()
    reply = await client.post(f"/api/rate/title/{untouched}", json={"answer": "not_seen"})
    answered.append(time.monotonic() - started)
    assert reply.status_code == 200, reply.text
    assert max(answered) < 0.45, f"a Rate route waited for Jellyfin: {answered}"

    await session.settled()
    assert len(module.state.write_log) == 3, module.state.write_log
    assert {w["played"] for w in module.state.write_log} == {True, False}
    assert re.search(r"Played (true|false)", " ".join(e["text"] for e in rail.recent(user_id=user_id)))
