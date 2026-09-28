"""Mix alternates on a monotone counter; the test that matters is a substituted card, where
"flip from the slot" and "flip from the last card served" disagree."""

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
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rate import session
from tests.helpers import insert_user

HP = DEFAULTS

# §6.1's three refusals: a double tap must never reach `app.py`'s generic conflict handler.
STALE_REASONS = {"no_card", "stale_card", "wrong_card_type"}

# A race that passes once has not passed: the reproductions failed four and five of six.
RACES = 8

# Marker values the fixtures write into `ledger_state`, so a leak shows as a literal in the JSON.
FORBIDDEN_CARD_KEYS = {
    "predicted", "predicted_label", "s", "sigma", "cdf", "tier", "straddle",
    "score", "rank", "verdict_class", "band", "reask_of", "b", "gate", "beta",
}
MARKER_S = 0.777123
MARKER_CDF = 0.913357


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


async def label(db, user_id: int, title_id: int, value: int = 2) -> None:
    """Written directly: going through the surface would consume slots and cards."""
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen') "
        "ON CONFLICT (user_id, title_id) DO UPDATE SET state = 'seen'",
        user_id,
        title_id,
    )
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
        user_id,
        title_id,
        value,
    )


@pytest.fixture
async def world(db):
    """Twenty films, because the block test spends fifteen."""
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    return {"user": await insert_user(db, "patrick", "admin")}


@pytest.fixture
async def rated(db, world):
    """Four titles in one verdict class, so a battle pair exists from the first slot."""
    for title_id in (1, 2, 3, 4):
        await label(db, world["user"], title_id, 2)
    return world


async def rated_elsewhere(db, user_id: int, ids, value: int) -> None:
    """Rated titles outside the queue's twenty: Mix battles only past fifteen labels (decision 492)."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned, overview)
        SELECT x, 'movie', 'Rated ' || x, true, 'A film about ' || x FROM unnest($1::int[]) x
        ON CONFLICT (id) DO NOTHING
        """,
        list(ids),
    )
    for title_id in ids:
        await label(db, user_id, title_id, value)


@pytest.fixture
async def warm(db, rated):
    """Past decision 492's warm-up, and every pair still comes from the liked four (decision 493)."""
    await rated_elsewhere(db, rated["user"], range(21, 32), 0)
    return rated


async def open_session(db, user_id, *, mode="mix", kinds=("movie",)) -> session.RateSession:
    s = await session.open_or_resume(db, user_id=user_id, kinds=list(kinds))
    if mode != "mix":
        s = await session.set_controls(db, s, mode=mode)
    return await session.ensure_card(db, s)


def token(s: session.RateSession) -> str:
    assert s.card_token is not None, "no card on the table"
    return str(s.card_token)


def test_the_card_type_is_a_pure_function_of_the_monotone_counter_and_the_block_rolls_at_fifteen():
    """Asserted as arithmetic: fifteen is odd, so a slot-derived type put two sweeps across each roll."""
    assert [session.card_type_for("mix", n) for n in range(0, 6)] == [
        "sweep", "battle", "sweep", "battle", "sweep", "battle"
    ]
    assert session.card_type_for("sweep", 1) == "sweep"
    assert session.card_type_for("battle", 0) == "battle"

    assert session.observation_index(0, 1) == 0
    assert session.observation_index(0, 15) == 14
    assert session.observation_index(1, 1) == 15
    assert session.observation_index(3, 8) == 52
    assert session.card_type_for("mix", session.observation_index(0, 15)) == "sweep"
    assert session.card_type_for("mix", session.observation_index(1, 1)) == "battle"

    assert session.advance(0, 1) == (0, 2)
    assert session.advance(0, 14) == (0, 15)
    assert session.advance(0, 15) == (1, 1)
    assert session.advance(3, 15) == (4, 1)


async def test_mix_alternates_on_the_counter_and_not_on_the_last_card_served(db, world):
    """At slot 2 no pair exists and a sweep stands in; at slot 3 the counter calls a sweep again,
    where flipping from the last card served would give a battle."""
    user = world["user"]
    await rated_elsewhere(db, user, range(21, 36), 0)
    s = await open_session(db, user)
    assert s.slot == 1
    assert s.current_card["type"] == "sweep"

    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
    s = out.session
    assert s.slot == 2
    assert s.current_card["type"] == "sweep"
    assert s.current_card["substituted_for"] == "battle"

    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
    s = out.session
    assert s.slot == 3
    assert s.current_card["type"] == "sweep", "slot 3 is a sweep slot whatever slot 2 served"
    assert s.current_card.get("substituted_for") is None

    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
    s = out.session
    assert s.slot == 4
    assert s.current_card["type"] == "battle", "three titles in one class is a pool"


async def test_a_run_of_duels_still_returns_sweep_cards(db, warm):
    """The prototype advanced the index only on a verdict, so Mix never came back from battles."""
    user = warm["user"]
    s = await open_session(db, user)
    served: list[str] = []
    for _ in range(6):
        served.append(s.current_card["type"])
        if s.current_card["type"] == "battle":
            out = await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)
        else:
            out = await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)
        s = out.session
    assert served == ["sweep", "battle", "sweep", "battle", "sweep", "battle"]
    assert s.slot == 7
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1", user
    ) == 3, "every battle slot wrote its duel"


async def test_mix_keeps_alternating_across_the_block_roll(db, warm):
    """Asserted AT the roll through real taps; the within-block test cannot see it (decision 200)."""
    s = await open_session(db, warm["user"])
    served: list[str] = []
    for _ in range(16):
        card = s.current_card
        served.append(card["type"])
        assert card.get("substituted_for") is None, (
            f"a substitution stood in at {(s.block_index, s.slot)}, so this says nothing about "
            "alternation"
        )
        if card["type"] == "battle":
            out = await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)
        else:
            out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
        s = out.session

    assert (served[14], served[15]) == ("sweep", "battle"), (
        "the block boundary served two of the same card type in a row: slot 15 of one block and "
        f"slot 1 of the next were {served[14]} then {served[15]}"
    )
    assert served == ["sweep", "battle"] * 8
    assert (s.block_index, s.slot) == (1, 2), "sixteen observations is one roll and one tap"


async def test_every_kind_of_observation_advances_the_counter_by_exactly_one(db, rated):
    """Only the corrections row does not advance; the `rate_observation_advances_rule` CHECK pins it."""
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")

    for step, tap in enumerate(("verdict", "not_seen", "skip"), start=1):
        assert s.slot == step
        if tap == "verdict":
            out = await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)
        elif tap == "not_seen":
            out = await session.record_not_seen(db, s, card_token=token(s))
        else:
            out = await session.record_skip(db, s, card_token=token(s))
        s = out.session
        assert s.slot == step + 1, f"{tap} did not advance the counter"

    s = await session.set_controls(db, s, mode="battle")
    s = await session.ensure_card(db, s)
    slot = s.slot
    out = await session.record_duel(db, s, card_token=token(s), outcome="TIE", hp=HP)
    assert out.session.slot == slot + 1, "a tie is an observation and advances like any other"

    advances = [
        r["advances"]
        for r in await db.fetch(
            "SELECT kind_of, advances FROM rate_observation WHERE user_id = $1 ORDER BY seq",
            user,
        )
    ]
    assert advances == [True, True, True, True]


async def test_the_counter_runs_to_fifteen_and_rolls_into_a_new_block(db, world):
    """The counter alone: undo's commit point moved one tap later (decision 199) and is tested below."""
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    slots = []
    for _ in range(15):
        slots.append((s.block_index, s.slot))
        s = (await session.record_skip(db, s, card_token=token(s))).session
    assert slots[0] == (0, 1)
    assert slots[-1] == (0, 15)
    assert (s.block_index, s.slot) == (1, 1)
    assert await db.fetchval(
        "SELECT count(*) FROM rate_observation WHERE session_id = $1 AND block_index = 0", s.id
    ) == 15


async def test_the_empty_state_names_the_pool_that_is_empty_rather_than_claiming_it_is_all_rated(
    db, world
):
    """A young profile's empty battle pool must not read "You've rated everything" (finding 20).
    Three sessions: nothing rated, a spent kind, and both."""
    user = world["user"]

    battle = await session.payload(db, await open_session(db, user, mode="battle"))
    assert battle["card"] is None, "twenty unrated films is not a battle pool"
    assert battle["drained"]["cause"] == "pool"
    assert "rated everything" not in battle["drained"]["text"], (
        "the first-week member has rated nothing at all: "
        f"{battle['drained']['text']!r}"
    )
    assert "one by one" in battle["drained"]["text"], "and the copy points at what fills the pool"

    # This household has no series, so the sweep queue really is spent there.
    await session.end_session(db, user_id=user)
    sweep = await session.payload(
        db, await open_session(db, user, mode="sweep", kinds=("series",))
    )
    assert sweep["card"] is None
    assert sweep["drained"]["cause"] == "queue"
    assert sweep["drained"]["text"].startswith("You've rated everything we can queue right now.")

    await session.end_session(db, user_id=user)
    mix = await session.payload(db, await open_session(db, user, kinds=("series",)))
    assert mix["card"] is None
    assert mix["drained"]["cause"] == "both", "Mix tried both pools and both were empty"
    assert mix["drained"]["text"] != sweep["drained"]["text"]
    for body in (battle, sweep, mix):
        assert body["drained"]["text"].isascii(), body["drained"]["text"]


async def test_every_card_that_stands_in_for_another_type_carries_its_marker(db, warm):
    """`substituted_for` must be set wherever a card of the other type is stashed (finding 21)."""
    user = warm["user"]
    s = await open_session(db, user)
    assert s.current_card["type"] == "sweep" and s.current_card.get("substituted_for") is None

    # 1. The thin-pool substitution: disliked ratings pass decision 492's warm-up, and decision
    #    493 keeps them out of a first sitting's pairs.
    lonely = await insert_user(db, "lonely", "member")
    await rated_elsewhere(db, lonely, range(21, 36), 0)
    thin = await open_session(db, lonely)
    thin = (await session.record_verdict(db, thin, card_token=token(thin), value=2, hp=HP)).session
    assert thin.slot == 2 and thin.current_card["type"] == "sweep"
    assert thin.current_card["substituted_for"] == "battle"

    # 2. The banner's head redraw, landing on a battle slot with a card already stashed.
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert s.slot == 2 and s.current_card["type"] == "battle", "slot 2 of block 0 is a battle"
    s = await session.ensure_card(db, s, head=[9])
    assert s.current_card["type"] == "sweep" and s.current_card["title_id"] == 9
    assert s.current_card["substituted_for"] == "battle", (
        "the banner replaced a battle with a sweep and the payload said nothing about it"
    )
    card = await session.public_card(db, s)
    assert card["substituted_for"] == "battle", "and the client is told, not only the server"
    body = await session.payload(db, s)
    assert body["session"]["block"]["serving"] == "battle", (
        "the counter still names what the slot called for; the marker is what explains the flip"
    )

    # 3. `_redraw_pair`'s fallback: a correction that leaves the survivor's band with nobody in
    #    it, which is the same substitution by a different door.
    solitary = await insert_user(db, "solitary", "member")
    await label(db, solitary, 1, 1)
    await label(db, solitary, 2, 1)
    lone = await session.open_or_resume(db, user_id=solitary, kinds=["movie"])
    pair = {
        "type": "battle", "kind": "movie", "title_a": 1, "title_b": 2,
        "verdict_class": 1, "reason": "same band", "reask_of": None,
    }
    repaired = await session._redraw_pair(db, lone, pair, corrected=[1])
    assert repaired["type"] == "sweep", "the band held nobody else, so the slot falls through"
    assert repaired["substituted_for"] == "battle"


async def test_an_undo_across_a_mode_change_leaves_the_counter_naming_its_own_call(db, warm):
    """A mode change drops the card but keeps the counter, and undo restores the card without the
    mode, so `serving` and `card.type` may disagree unmarked. Fails if either is redefined."""
    user = warm["user"]
    s = await open_session(db, user)
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert s.slot == 2 and s.current_card["type"] == "battle", "slot 2 of block 0 is a battle"

    s = await session.set_controls(db, s, mode="sweep")
    s = await session.ensure_card(db, s)
    assert s.current_card["type"] == "sweep"
    s = (await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)).session
    s = await session.set_controls(db, s, mode="mix")
    s = (await session.undo(db, s, hp=HP)).session

    assert (s.block_index, s.slot) == (0, 2), "undo puts the counter back where it answered"
    assert s.current_card["type"] == "sweep", "and restores the card it answered, which was a sweep"
    assert s.current_card.get("substituted_for") is None, (
        "nothing stood in for anything: the card was drawn for a sweep-mode session"
    )
    body = await session.payload(db, s)
    assert body["card"]["type"] == "sweep"
    assert body["card"]["substituted_for"] is None
    assert body["session"]["block"]["serving"] == "battle", (
        "`serving` is the counter's call in the mode now in force, and this is the one flip no "
        "draw-time marker can see -- so the two disagree here with nothing marked, and the comment "
        "that said otherwise was the defect"
    )


def _walk(node: Any, path: str = "card"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def assert_no_model_belief(card: dict[str, Any]) -> None:
    leaks = [
        f"{path}.{key}"
        for path, key, _ in _walk(card)
        if key in FORBIDDEN_CARD_KEYS
    ]
    assert not leaks, f"the card carries the model's belief at {leaks}"
    body = json.dumps(card)
    assert str(MARKER_S) not in body and str(MARKER_CDF) not in body


async def seed_ledger(db, user_id: int, title_ids, *, fitted: bool = False) -> None:
    """Every title carries two marker numbers, so a leak shows as a literal. `fitted=True` runs a
    real refit first: without a cached fit the incremental path touches nothing."""
    if fitted:
        await refit.refit_user(db, user_id=user_id, kind="movie", hp=HP)
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


async def test_the_sweep_card_carries_no_model_belief_and_the_reveal_arrives_with_the_verdict(
    db, rated
):
    """The numbers absent from the card must be present in the tap's response, or omission is trivial."""
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21))
    s = await open_session(db, user, mode="sweep")

    card = await session.public_card(db, s)
    assert card["type"] == "sweep"
    assert_no_model_belief(card)
    whole = await session.payload(db, s)
    assert_no_model_belief(whole["card"])
    assert whole["reveal"] is None
    assert str(MARKER_CDF) not in json.dumps(whole["card"])

    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
    assert out.reveal["available"] is True
    assert out.reveal["predicted"] in (0, 1, 2)
    assert out.reveal["cdf"] == pytest.approx(MARKER_CDF)
    assert out.reveal["text"].startswith("we'd have guessed")
    # Decision 491: the number rides beside its name only with Show the model on.
    modelled = session.viewer_reveal(out.reveal, show_model=True)
    assert "cdf 0.91" in modelled["text"], "§6.8: the number appears beside its name"
    member = session.viewer_reveal(out.reveal, show_model=False)
    assert member["text"] == out.reveal["text"] and "cdf" not in member["text"]
    assert {"cdf", "s", "label_count"}.isdisjoint(member)
    assert member["predicted_label"] == out.reveal["predicted_label"]
    assert_no_model_belief(await session.public_card(db, out.session))


async def test_the_battle_card_hides_the_verdict_band_it_was_drawn_from(db, rated):
    """The band is the person's own label but still an anchor; it stays server-side for corrections."""
    s = await open_session(db, rated["user"], mode="battle")
    assert s.current_card["verdict_class"] == 2
    card = await session.public_card(db, s)
    assert card["type"] == "battle"
    assert {card["left"]["id"], card["right"]["id"]} == {
        s.current_card["title_a"], s.current_card["title_b"]
    }
    assert (card["left"]["outcome"], card["right"]["outcome"]) == ("A", "B")
    assert_no_model_belief(card)


async def test_the_reveal_is_read_before_the_write_and_not_after_it(db, rated):
    """A handler reading after the update would make "we'd have guessed the same" true by
    construction. `fitted=True`: without a cached fit the update queues and moves nothing."""
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21), fitted=True)
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]

    out = await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)
    assert out.reveal["s"] == pytest.approx(MARKER_S)
    after = await db.fetchval(
        "SELECT s FROM ledger_state WHERE user_id = $1 AND title_id = $2", user, title_id
    )
    assert after != pytest.approx(MARKER_S), "the incremental update did move the ranking"


async def test_the_reveal_is_suppressed_rather_than_invented_before_the_first_fit(db, world):
    """No fit is legal (§3.1); a guess off a CDF that does not exist has no provenance."""
    s = await open_session(db, world["user"], mode="sweep")
    out = await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)
    assert out.reveal["available"] is False
    assert "yet" in out.reveal["reason"]


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


async def _unowned(db, title_id: int, name: str) -> None:
    """A catalog title the household does not own — 80 of v20260828's 100 seed titles."""
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned, overview) "
        "VALUES ($1, 'movie', $2, false, 'A film about ' || $2)",
        title_id,
        name,
    )


async def test_the_reveal_fires_on_an_unowned_queue_title_with_no_ledger_state_row(db, world):
    """The seed list is mostly unowned, with no `ledger_state` row; the cached fit still places them
    (§5.2). Two titles pointing opposite ways must differ, or `mu` for all would pass."""
    user = world["user"]
    await _unowned(db, 98, "Pointing with them")
    await _unowned(db, 99, "Pointing against them")
    # Three disliked, one fine, two liked, with the signal on one axis so `v` has a sign.
    signal = {1: -1.0, 2: -1.0, 3: -1.0, 4: 0.0, 5: 1.0, 6: 1.0, 98: 1.0, 99: -1.0}
    for title_id, value in ((1, 0), (2, 0), (3, 0), (4, 1), (5, 2), (6, 2)):
        await label(db, user, title_id, value)
    src = _embeddings_from(signal)
    report = await refit.refit_user(db, user_id=user, kind="movie", hp=HP, embeddings=src)
    assert report.fitted, report.error

    for title_id in (98, 99):
        assert await db.fetchval(
            "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND title_id = $2",
            user,
            title_id,
        ) == 0, f"title {title_id} is unowned and unobserved, so the fit wrote it no row"

    reveals = {}
    for title_id in (98, 99):
        s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
        s = await session.stash(
            db,
            s,
            {"type": "sweep", "kind": "movie", "title_id": title_id, "reason": "queued because",
             "p_seen": 0.4, "source": "p_seen", "reask_of": None},
            expected=s.card_token,
        )
        out = await session.record_verdict(
            db, s, card_token=token(s), value=2, hp=HP, embeddings=src
        )
        reveals[title_id] = out.reveal

    for title_id, reveal in reveals.items():
        assert reveal["available"] is True, (
            f"title {title_id} got {reveal.get('reason')!r} on a profile with a fitted ledger"
        )
        assert reveal["predicted"] in (0, 1, 2)
        assert 0.0 <= reveal["cdf"] <= 1.0, reveal
        assert reveal["text"].startswith("we'd have guessed")
    # §5.2's band counts are live: six labels for the first tap and seven for the second.
    assert (reveals[98]["label_count"], reveals[99]["label_count"]) == (6, 7)
    assert reveals[98]["s"] > reveals[99]["s"], (
        "the two unowned titles got the same ranking, so the coordinate was not read: "
        f"{reveals[98]['s']} vs {reveals[99]['s']}"
    )
    assert reveals[98]["cdf"] > reveals[99]["cdf"], "and therefore different displayed weights"
    assert reveals[98]["predicted"] >= reveals[99]["predicted"]


async def test_the_reveal_is_dark_only_until_the_first_fit_and_not_for_the_whole_sitting(
    db, world
):
    """One label is not a distribution, so the first two taps are dark; after the fit, lit."""
    user = world["user"]
    for title_id, name in ((97, "First"), (98, "Second"), (99, "Third")):
        await _unowned(db, title_id, name)

    async def tap(title_id: int, value: int) -> dict[str, Any]:
        s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
        s = await session.stash(
            db,
            s,
            {"type": "sweep", "kind": "movie", "title_id": title_id, "reason": "queued because",
             "p_seen": 0.4, "source": "p_seen", "reask_of": None},
            expected=s.card_token,
        )
        out = await session.record_verdict(db, s, card_token=token(s), value=value, hp=HP)
        return out.reveal

    first = await tap(97, 2)
    assert first["available"] is False and "yet" in first["reason"]
    second = await tap(98, 0)
    assert second["available"] is False, "two labels, and nothing fitted them yet"

    # What the 60 s sweep does: the first tap queues the fit rather than running it (finding 9).
    assert (await refit.refit_user(db, user_id=user, kind="movie", hp=HP)).fitted
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND title_id = 99", user
    ) == 0, "title 99 is unowned, so the fit that just ran wrote it no row"

    third = await tap(99, 1)
    assert third["available"] is True, third.get("reason")
    assert third["label_count"] == 2, "banded against the two labels the fit was built on"
    assert 0.0 <= third["cdf"] <= 1.0


async def test_a_verdict_writes_seen_and_not_seen_writes_unseen(db, rated):
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")
    verdicted = s.current_card["title_id"]
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    unseen = s.current_card["title_id"]
    s = (await session.record_not_seen(db, s, card_token=token(s))).session

    states = {
        r["title_id"]: r["state"]
        for r in await db.fetch(
            "SELECT title_id, state FROM user_title WHERE user_id = $1", user
        )
    }
    assert states[verdicted] == "seen"
    assert states[unseen] == "unseen"
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = $2", user, unseen
    ) == 0, "`Not seen` writes no observation row"


async def test_no_third_seen_state_is_reachable_from_the_write_path_or_the_column(db, world):
    """Both halves: the column refuses `forgotten`, and no tap can ask for it."""
    user = world["user"]
    with pytest.raises(asyncpg.PostgresError):
        await db.execute(
            "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'forgotten')", user
        )
    s = await open_session(db, user, mode="sweep")
    with pytest.raises(ValueError, match="0, 1 or 2"):
        await session.record_verdict(db, s, card_token=token(s), value=3, hp=HP)
    states = await db.fetchval(
        "SELECT array_agg(DISTINCT state) FROM user_title WHERE user_id = $1", user
    )
    assert states in (None, ["seen"], ["unseen"], ["seen", "unseen"])


async def test_flipping_a_rated_title_back_to_unseen_leaves_its_verdicts_and_duels_in_place(
    db, rated
):
    """The title leaves the battle pool because the pool is a conjunction, not because of a delete."""
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    pair = (s.current_card["title_a"], s.current_card["title_b"])
    s = (await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)).session

    # The corrections row is the surface's own path back to `unseen` for a rated title.
    s = await session.ensure_card(db, s)
    corrected = s.current_card["title_a"]
    s = (await session.record_correction(db, s, card_token=token(s), side="left")).session

    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, corrected
    ) == "unseen"
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = $2", user, corrected
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND title_a = $2 AND title_b = $3",
        user, pair[0], pair[1],
    ) == 1


@pytest.mark.parametrize("outcome", ["A", "B", "TIE"])
async def test_one_battle_answer_writes_exactly_one_duel_row(db, rated, outcome):
    """A Tie feeds the Davidson tie term (22% of random pairs); never a skip or a dropped row."""
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    a, b = s.current_card["title_a"], s.current_card["title_b"]
    out = await session.record_duel(db, s, card_token=token(s), outcome=outcome, hp=HP)

    rows = await db.fetch("SELECT * FROM duel WHERE user_id = $1", user)
    assert len(rows) == 1
    row = rows[0]
    assert (row["title_a"], row["title_b"]) == (a, b)
    assert row["outcome"] == outcome
    assert row["context"] == "profile_battle"
    # A profile battle is random by design, outside §13's held-out sample.
    assert row["selection"] == "random"
    journal = await db.fetchrow(
        "SELECT kind_of, duel_id, title_ids FROM rate_observation WHERE session_id = $1", s.id
    )
    assert journal["kind_of"] == ("tie" if outcome == "TIE" else "duel")
    assert journal["duel_id"] == row["id"]
    assert out.session.slot == 2


async def test_much_more_weights_one_answer_and_a_tie_never(db, rated):
    """Decision 528: "Much more" is `decisive` for that answer alone; a TIE sent as decisive is still
    unweighted. The weights come from `hp.margin_for`, which keeps them in `ledger_hyperparams.json`."""
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    for outcome, decisive in (("A", True), ("B", False), ("TIE", True)):
        s = (
            await session.record_duel(
                db, s, card_token=token(s), outcome=outcome, decisive=decisive, hp=HP
            )
        ).session

    margins = [
        r["margin"]
        for r in await db.fetch("SELECT margin FROM duel WHERE user_id = $1 ORDER BY id", user)
    ]
    assert margins == pytest.approx([1.6, 1.0, 1.0])
    assert HP.margin_for(True) == 1.6 and HP.margin_for(False) == 1.0


@pytest.mark.parametrize("side", ["left", "right"])
async def test_a_correction_unsees_exactly_the_named_side_and_writes_no_duel(db, rated, side):
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    a, b = s.current_card["title_a"], s.current_card["title_b"]
    corrected, survivor = (a, b) if side == "left" else (b, a)

    out = await session.record_correction(db, s, card_token=token(s), side=side)
    s = out.session

    states = {
        r["title_id"]: r["state"]
        for r in await db.fetch(
            "SELECT title_id, state FROM user_title WHERE user_id = $1 AND title_id = ANY($2::int[])",
            user, [a, b],
        )
    }
    assert states[corrected] == "unseen"
    assert states[survivor] == "seen", "only the named side is corrected"
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", user) == 0
    # The survivor stays on its side; the corrected half is replaced.
    assert s.current_card["type"] == "battle"
    assert s.current_card["title_b" if side == "left" else "title_a"] == survivor
    assert corrected not in (s.current_card["title_a"], s.current_card["title_b"])


async def test_both_swaps_the_whole_pair(db, rated):
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    a, b = s.current_card["title_a"], s.current_card["title_b"]
    s = (await session.record_correction(db, s, card_token=token(s), side="both")).session

    unseen = await db.fetchval(
        "SELECT array_agg(title_id ORDER BY title_id) FROM user_title "
        "WHERE user_id = $1 AND state = 'unseen'",
        user,
    )
    assert sorted(unseen) == sorted([a, b])
    assert await db.fetchval("SELECT count(*) FROM duel") == 0
    assert {s.current_card["title_a"], s.current_card["title_b"]}.isdisjoint({a, b})


async def test_a_correction_does_not_advance_the_counter(db, rated):
    """Corrections repair the question; `rate_observation_advances_rule` pins that to `kind_of`."""
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    before = (s.block_index, s.slot)
    s = (await session.record_correction(db, s, card_token=token(s), side="left")).session
    assert (s.block_index, s.slot) == before

    row = await db.fetchrow(
        "SELECT kind_of, advances, slot FROM rate_observation WHERE user_id = $1", user
    )
    assert row["kind_of"] == "correction"
    assert row["advances"] is False
    with pytest.raises(asyncpg.PostgresError):
        # The CHECK is the enforcement, not the comment above it.
        await db.execute(
            "INSERT INTO rate_observation "
            "(session_id, user_id, seq, block_index, slot, kind_of, advances, card, title_ids) "
            "VALUES ($1, $2, 99, 0, 1, 'correction', true, '{}'::jsonb, '{}'::int[])",
            s.id, user,
        )


@pytest.fixture
async def linked(db, fake_jellyfin, secrets_key):
    """The person's own token: §7.3's least-privilege write path, not the admin key."""
    module, transport = fake_jellyfin
    # Every pool member must be a real fake-server item: the pair is drawn at random.
    owned = [item["Id"].removeprefix("jf-") for item in module.ITEMS if item["Id"] != "jf-x"]
    await make_titles(db, [(int(i), "movie", f"Title {i}") for i in owned])
    await db.execute("UPDATE title SET jellyfin_id = 'jf-' || id")
    user = await db.fetchval(
        "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
        "VALUES ('patrick', 'admin', 'jf-user-patrick', 'linked') RETURNING id"
    )
    for title_id in owned:
        await label(db, user, int(title_id), 2)
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


async def test_a_correction_queues_the_jellyfin_seen_state_sync(db, linked):
    """§7.3's mapping: `unseen` -> Played = false, with that user's own token."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    s = await open_session(db, user, mode="battle")
    corrected = s.current_card["title_a"]
    await session.record_correction(db, s, card_token=token(s), side="left", jf=jf)

    assert {"user": "jf-user-patrick", "item": f"jf-{corrected}", "played": False} in (
        module.state.write_log
    )
    assert await db.fetchval(
        "SELECT jf_synced_at IS NOT NULL FROM user_title WHERE user_id = $1 AND title_id = $2",
        user, corrected,
    ) is True


async def test_undo_pushes_back_exactly_what_the_forward_action_pushed(db, linked):
    """If the retraction did not push, the next sweep would read our own write back as history."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    s = await open_session(db, user, mode="battle")
    corrected = s.current_card["title_a"]
    s = (
        await session.record_correction(db, s, card_token=token(s), side="left", jf=jf)
    ).session
    assert module.state.write_log[-1]["played"] is False

    await session.undo(db, s, hp=HP, jf=jf)
    assert module.state.write_log[-1] == {
        "user": "jf-user-patrick", "item": f"jf-{corrected}", "played": True
    }
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, corrected
    ) == "seen"


async def test_undo_pushes_nothing_where_the_forward_action_pushed_nothing(db, linked):
    """Undo compensates what it did, not what it intended."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    await db.execute("UPDATE title SET jellyfin_id = NULL")
    s = await open_session(db, user, mode="battle")
    s = (
        await session.record_correction(db, s, card_token=token(s), side="both", jf=jf)
    ).session
    assert module.state.write_log == []
    await session.undo(db, s, hp=HP, jf=jf)
    assert module.state.write_log == []


async def test_with_no_connector_the_push_is_owed_rather_than_lost(db, rated):
    """A present row with NULL `jf_synced_at` means Jellyfin is owed the change (§7.3)."""
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    corrected = s.current_card["title_b"]
    out = await session.record_correction(db, s, card_token=token(s), side="right")

    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = $2",
        user, corrected,
    )
    assert row["state"] == "unseen"
    assert row["jf_synced_at"] is None
    assert any("not pushed" in line for line in out.log)


async def test_undo_pops_a_verdict_restores_the_exact_card_and_retracts_the_row(db, rated):
    """The card that comes back is the one that produced the observation. `fitted=True`: a cache
    miss queues the refit and applies nothing."""
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21), fitted=True)
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]
    card_before = dict(s.current_card)

    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE title_id = $1", title_id) == 1

    out = await session.undo(db, s, hp=HP)
    s = out.session
    # The compensating Ledger write, not merely a tombstone.
    assert out.ledger["applied"] is True
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE title_id = $1", title_id) == 0
    assert await db.fetchval(
        "SELECT count(*) FROM user_title WHERE user_id = $1 AND title_id = $2", user, title_id
    ) == 0, "the implied `seen` went back to the absence it came from"
    assert s.current_card == card_before
    assert (s.block_index, s.slot) == (0, 1)
    assert await db.fetchval(
        "SELECT undone_at IS NOT NULL FROM rate_observation WHERE session_id = $1", s.id
    ) is True


@pytest.mark.parametrize("outcome", ["A", "TIE"])
async def test_undo_restores_the_pair_it_was_asked_about_rather_than_reshuffling_it(
    db, rated, outcome
):
    """The pair was drawn at random, so an Undo that redrew would ask something never answered."""
    s = await open_session(db, rated["user"], mode="battle")
    pair = (s.current_card["title_a"], s.current_card["title_b"])
    s = (await session.record_duel(db, s, card_token=token(s), outcome=outcome, hp=HP)).session

    s = (await session.undo(db, s, hp=HP)).session
    assert (s.current_card["title_a"], s.current_card["title_b"]) == pair
    assert await db.fetchval("SELECT count(*) FROM duel") == 0
    assert s.slot == 1


@pytest.mark.parametrize("tap", ["not_seen", "skip", "correction"])
async def test_undo_pops_an_observation_of_any_kind(db, rated, tap):
    """ANY kind, including a correction, which a last-verdict slot could not cover."""
    user = rated["user"]
    mode = "battle" if tap == "correction" else "sweep"
    s = await open_session(db, user, mode=mode)
    card_before = dict(s.current_card)

    if tap == "not_seen":
        s = (await session.record_not_seen(db, s, card_token=token(s))).session
    elif tap == "skip":
        s = (await session.record_skip(db, s, card_token=token(s))).session
    else:
        s = (await session.record_correction(db, s, card_token=token(s), side="both")).session

    before_states = await db.fetchval(
        "SELECT count(*) FROM user_title WHERE user_id = $1 AND state = 'unseen'", user
    )
    assert before_states == (0 if tap == "skip" else (1 if tap == "not_seen" else 2))

    out = await session.undo(db, s, hp=HP)
    s = out.session
    assert out.undone == tap
    assert s.current_card == card_before, "the exact card came back"
    assert await db.fetchval(
        "SELECT count(*) FROM user_title WHERE user_id = $1 AND state = 'unseen'", user
    ) == 0, "the state the tap implied was compensated"
    assert await session.undo_availability(db, s) == {
        "available": False, "kind": None, "reason": "empty"
    }


async def test_undo_lifts_the_skip_suppression_so_the_card_can_be_answered(db, rated):
    """The journal row is the suppression, so the title must come back into play."""
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]
    s = (await session.record_skip(db, s, card_token=token(s))).session
    assert s.current_card["title_id"] != title_id

    s = (await session.undo(db, s, hp=HP)).session
    assert s.current_card["title_id"] == title_id
    s = (await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)).session
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = $2", user, title_id
    ) == 1


async def test_undo_stops_at_the_block_boundary_and_reports_it_rather_than_no_opping(db, world):
    """Decision 199: a block commits when the next block's first tap lands, so retracting the 16th
    does not hand block 0 back."""
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    with pytest.raises(session.UndoUnavailable) as empty:
        await session.undo(db, s, hp=HP)
    assert empty.value.reason == "empty"

    for _ in range(16):
        s = (await session.record_skip(db, s, card_token=token(s))).session
    assert (s.block_index, s.slot) == (1, 2), "fifteen taps rolled the block and one landed in it"

    # The sixteenth tap is the commit: its retraction comes back, the block it closed does not.
    assert await session.undo_availability(db, s) == {
        "available": True, "kind": "skip", "reason": None
    }
    s = (await session.undo(db, s, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1), "the new block's only observation came back"

    assert await session.undo_availability(db, s) == {
        "available": False, "kind": None, "reason": "block_boundary"
    }, "the sixteenth tap committed block 0, and retracting it does not un-commit it"
    with pytest.raises(session.UndoUnavailable) as refused:
        await session.undo(db, s, hp=HP)
    assert refused.value.reason == "block_boundary"
    assert await db.fetchval(
        "SELECT count(*) FROM rate_observation WHERE session_id = $1 AND undone_at IS NOT NULL",
        s.id,
    ) == 1, "a refused undo compensates nothing: only the sixteenth tap is tombstoned"


async def test_the_fifteenth_tap_stays_undoable_until_the_sixteenth_lands(db, world):
    """Decision 199: a block commits when the FIRST observation of the next block lands, so the
    fifteenth tap stays undoable until then."""
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    for _ in range(14):
        s = (await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)).session
    assert (s.block_index, s.slot) == (0, 15)

    fifteenth = s.current_card["title_id"]
    s = (await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1), "the fifteenth tap rolled the counter"

    assert await session.undo_availability(db, s) == {
        "available": True, "kind": "verdict", "reason": None
    }, "the tap the person can still see on screen has to be retractable"

    out = await session.undo(db, s, hp=HP)
    s = out.session
    assert out.undone == "verdict"
    assert (s.block_index, s.slot) == (0, 15), "undo restores the block and slot of the row"
    assert s.current_card["title_id"] == fifteenth, "and the exact card that produced it"
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = $2", user, fifteenth
    ) == 0, "the mis-tap is gone from §4.2's history, which only undo may do"

    # One tap in the new block is the commit; the fifteenth is out of reach from there.
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1)
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 2)
    s = (await session.undo(db, s, hp=HP)).session
    assert (s.block_index, s.slot) == (1, 1), "the sixteenth tap itself comes back"
    assert await session.undo_availability(db, s) == {
        "available": False, "kind": None, "reason": "block_boundary"
    }, "and having landed once, the sixteenth has committed the block it ended"


async def test_undo_walks_back_to_the_first_observation_of_the_block_and_then_refuses(db, rated):
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")
    for value in (0, 1, 2):
        s = (await session.record_verdict(db, s, card_token=token(s), value=value, hp=HP)).session
    assert s.slot == 4

    for expected_slot in (3, 2, 1):
        s = (await session.undo(db, s, hp=HP)).session
        assert s.slot == expected_slot
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 4, (
        "the four pre-existing labels are untouched; the three from this block are gone"
    )
    with pytest.raises(session.UndoUnavailable) as refused:
        await session.undo(db, s, hp=HP)
    assert refused.value.reason == "empty"


async def test_undo_of_a_re_rating_makes_the_previous_verdict_live_again(db, rated):
    """A re-rating supersedes, so its undo must un-supersede (`observations.undo` splices the chain)."""
    user = rated["user"]
    original = await db.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", user
    )
    s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
    s = await session.stash(
        db,
        s,
        {"type": "sweep", "kind": "movie", "title_id": 1, "reason": "re-rating", "p_seen": None,
         "source": "p_seen", "reask_of": None},
        expected=s.card_token,
    )
    s = (await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)).session
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original)

    await session.undo(db, s, hp=HP)
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original) is None
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = 1", user
    ) == 1


async def test_a_re_ask_is_written_distinguishably_and_shown_indistinguishably(db, rated):
    """The server-held card carries the reference and the client's does not; the row carries
    `is_reask`/`reask_of`, and the widget counts the answer once."""
    user = rated["user"]
    original = await db.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", user
    )
    s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
    s = await session.stash(
        db,
        s,
        {"type": "sweep", "kind": "movie", "title_id": 1, "p_seen": 1.0, "source": "reask",
         "reason": "queued because: you have this marked seen", "reask_of": original},
        expected=s.card_token,
    )
    before = (await session.payload(db, s))["class_balance"]["counts"]
    card = await session.public_card(db, s)
    assert_no_model_belief(card)
    assert "reask" not in json.dumps(card), "the served payload carries no marker of the stream"

    out = await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)
    row = await db.fetchrow(
        "SELECT is_reask, reask_of FROM verdict WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user
    )
    assert row["is_reask"] is True and row["reask_of"] == original
    after = (await session.payload(db, out.session))["class_balance"]["counts"]
    assert after == before, "a re-ask measures a judgement rather than adding one"


async def test_a_card_can_only_be_answered_once_and_only_by_the_control_it_carries(db, rated):
    """The token is also the double-tap guard: the second tap names a card no longer on the table."""
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")
    stale = token(s)
    s = (await session.record_verdict(db, s, card_token=stale, value=1, hp=HP)).session

    with pytest.raises(session.StaleCard) as again:
        await session.record_verdict(db, s, card_token=stale, value=1, hp=HP)
    assert again.value.reason == "stale_card"
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 5

    with pytest.raises(session.StaleCard) as wrong:
        await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)
    assert wrong.value.reason == "wrong_card_type"


async def test_one_live_session_per_person_and_a_resume_returns_the_same_card(db, world):
    """Two live sessions would each hold a counter, and Undo would have to guess."""
    user = world["user"]
    first = await open_session(db, user)
    again = await session.ensure_card(db, await session.open_or_resume(db, user_id=user))
    assert again.id == first.id
    assert again.card_token == first.card_token
    assert again.current_card == first.current_card
    assert await db.fetchval(
        "SELECT count(*) FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user
    ) == 1

    restarted = await session.open_or_resume(db, user_id=user, restart=True)
    assert restarted.id != first.id
    assert await db.fetchval(
        "SELECT count(*) FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user
    ) == 1


async def test_a_fresh_session_opens_in_mix(db, world):
    s = await session.open_or_resume(db, user_id=world["user"])
    assert s.mode == "mix"
    assert sorted(s.kinds) == ["movie", "series"]


async def test_changing_the_kinds_drops_the_card_and_never_leaves_neither_selected(db, world):
    """Decision 18: "never neither" is `library.normalise_kinds`'s; a film pair drops with Films."""
    user = world["user"]
    s = await open_session(db, user, kinds=("movie", "series"))
    assert s.current_card is not None
    s = await session.set_controls(db, s, kinds=["series"])
    assert s.current_card is None and s.card_token is None
    with pytest.raises(ValueError, match="at least one kind"):
        await session.set_controls(db, s, kinds=[])


@pytest.fixture
async def rate_client(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    user_id = (await client.get("/api/auth/me")).json()["id"]
    return client, user_id


async def test_the_route_serves_a_card_with_its_counter_its_balance_and_its_undo_state(
    db, rate_client
):
    """The next card rides in the write's response (§6: next card preloaded)."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await seed_ledger(db, user_id, range(1, 9))
    for title_id in (1, 2, 3, 4):
        await label(db, user_id, title_id, 2)

    first = (await client.get("/api/rate")).json()
    assert first["session"]["block"] == {
        "index": 0, "slot": 1, "size": 15, "counter": "1 of 15", "serving": "sweep"
    }
    assert first["card"]["type"] == "sweep"
    assert first["undo"] == {"available": False, "kind": None, "reason": "empty"}
    assert first["class_balance"]["counts"] == [0, 0, 4]
    # Four labels are not yet a habit: decision 491 arms the warning at fifteen.
    assert first["class_balance"]["warn"] is False
    assert first["class_balance"]["arms_at"] == 15
    assert_no_model_belief(first["card"])
    assert str(MARKER_CDF) not in json.dumps(first["card"])
    assert first["reveal"] is None

    # Idempotent: a refresh is not a redraw.
    assert (await client.get("/api/rate")).json()["card"]["token"] == first["card"]["token"]

    answered = await client.post(
        "/api/rate/verdict", json={"card_token": first["card"]["token"], "value": 2}
    )
    assert answered.status_code == 200
    body = answered.json()
    assert body["reveal"]["available"] is True
    assert body["session"]["block"]["slot"] == 2
    assert body["card"] is not None, "the next card came back with the answer"
    assert body["undo"]["available"] is True and body["undo"]["kind"] == "verdict"


async def test_the_route_refuses_a_stale_card_token(db, rate_client):
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    card = (await client.get("/api/rate")).json()["card"]
    assert (
        await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 1})
    ).status_code == 200

    stale = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 1}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_card"
    assert await db.fetchval("SELECT count(*) FROM verdict") == 1

    invented = await client.post(
        "/api/rate/verdict",
        json={"card_token": "3f0d3a1e-0000-4000-8000-000000000000", "value": 1},
    )
    assert invented.status_code == 409
    assert invented.json()["detail"]["reason"] == "stale_card"


async def test_the_route_reports_undo_as_unavailable_rather_than_no_opping(db, rate_client):
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await client.get("/api/rate")
    refused = await client.post("/api/rate/undo")
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"reason": "empty", "message": "Nothing to undo yet"}


async def test_the_route_rejects_an_empty_kind_selection(rate_client):
    client, _user_id = rate_client
    assert (await client.post("/api/rate/session", json={"kinds": []})).status_code == 422


async def test_the_rate_routes_need_a_signed_in_account(app, db):
    client = app()
    assert (await client.get("/api/rate")).status_code == 401
    assert (
        await client.post("/api/rate/verdict", json={"card_token": "x", "value": 1})
    ).status_code == 401


async def test_a_tap_reaches_the_transparency_rail(db, rate_client):
    """The rail must be recorded, not only returned: it was empty in production. A skip writes no
    observation, so it has no line."""
    from spielplan.home import rail

    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])

    card = (await client.get("/api/rate")).json()["card"]
    assert rail.recent(user_id=user_id) == [], "serving a card is not a model write"

    body = (
        await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    ).json()
    events = rail.recent(user_id=user_id)
    assert events, "the verdict never reached the rail"
    assert {e["kind"] for e in events} == {"verdict"}

    # Decision 117: absent from the response with the toggle off, but still recorded, so turning
    # it on shows the buffer.
    assert "log" not in body and "ledger" not in body

    await client.post("/api/auth/preferences", json={"show_model": True})
    with_model = (await client.get("/api/rate")).json()
    assert "log" in with_model, "the toggle must reveal the lines it gates"
    assert [e["text"] for e in events][::-1] == [
        e["text"] for e in rail.recent(user_id=user_id)
    ][::-1]

    before = len(rail.recent(user_id=user_id))
    await client.post("/api/rate/skip", json={"card_token": body["card"]["token"]})
    assert len(rail.recent(user_id=user_id)) == before, "a skip is not a model write"
    rail.forget()


async def test_the_verdict_rail_line_names_the_person_the_title_and_the_refit_ms(
    db, rate_client
):
    """Through the route: person, film and milliseconds meet in three places. The films are named
    without digits, so any digit in the line is the renderer's."""
    from spielplan.home import rail

    films = ["Heat", "Drive", "Ronin", "Collateral", "Thief", "Sicario", "Zodiac", "Michael Clayton"]
    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", films[i - 1]) for i in range(1, 9)])
    for title_id in (1, 2, 3, 4):
        await label(db, user_id, title_id, 2)
    # Labels first, then `fitted=True`: with no `ledger_fit` the tap is a cache miss and times nothing.
    await seed_ledger(db, user_id, range(1, 9), fitted=True)
    await client.post("/api/auth/preferences", json={"show_model": True})
    rater = (await client.get("/api/auth/me")).json()["name"]

    card = (await client.get("/api/rate")).json()["card"]
    rated_id, rated_name = card["title"]["id"], card["title"]["name"]
    assert not any(ch.isdigit() for ch in rated_name), "the cast must carry no digits"

    body = (
        await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    ).json()
    assert body["ledger"]["applied"] is True, (
        "the fixture owes a real incremental refit, or the ms in the line is untested"
    )
    ms = body["ledger"]["ms"]
    assert ms > 0, "a refit that cost no measurable time cannot pin the number in the line"

    events = rail.recent(user_id=user_id)
    assert [e["kind"] for e in events] == ["verdict", "verdict"], (
        "one verdict still narrates exactly two lines: the write and the seen-state push"
    )
    line = next(e for e in events if e["text"].startswith("verdict("))

    # (a) Against the renderer, which `test_home.py` pins; here the producer must call it.
    assert line["text"] == rail.verdict_line(rater, rated_name, "liked", refit_ms=ms)
    assert f"verdict({rater}, {rated_name}) = liked" in line["text"]
    assert "ordered-logit arm" in line["text"]

    # (b) The same number the response reports under `ledger.ms`.
    printed = re.search(r"incremental refit (\d+) ms", line["text"])
    assert printed is not None, line["text"]
    assert printed.group(1) == f"{ms:.0f}"

    # (c) The event's own field.
    assert line["title_id"] == rated_id

    assert re.search(r"title \d", line["text"]) is None, line["text"]

    # One sentence, not two renderings.
    assert body["log"][0] == line["text"]
    rail.forget()


async def test_a_cache_miss_says_the_fit_is_owed_rather_than_timing_one_that_never_ran(
    db, rate_client
):
    """A cache miss queues the fit, so neither the rail nor the model log may time one."""
    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    for title_id in (1, 2, 3):
        await label(db, user_id, title_id, 2)
    await client.post("/api/auth/preferences", json={"show_model": True})

    card = (await client.get("/api/rate")).json()["card"]
    body = (
        await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 2})
    ).json()

    assert await db.fetchval(
        "SELECT count(*) FROM ledger_fit WHERE user_id = $1", user_id
    ) == 0, "the fixture has a cached fit, so this says nothing about a miss"
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        user_id,
    ) is not None, "the sweep was never asked for the fit the request declined to run"

    ledger = body["ledger"]
    assert ledger["applied"] is False, (
        f"the model log will render this as a refit that took milliseconds: {ledger}"
    )
    assert "queued" in ledger["reason"], ledger
    assert "ms" not in ledger, f"a number for work nobody did: {ledger}"

    # And the line §6.7 persists, which quotes the same number one statement later.
    assert not any("refit" in line for line in body["log"]), body["log"]
    events = rail.recent(user_id=user_id)
    assert events, "the toggle is on, so the tap owes the rail its two lines"
    assert not any("refit" in e["text"] for e in events), [e["text"] for e in events]
    rail.forget()


async def test_the_banner_cta_serves_a_named_title_even_over_a_standing_session(db, rate_client):
    """`GET /api/rate` is idempotent, which swallowed the banner's head over a stashed card."""
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 8)])

    standing = (await client.get("/api/rate")).json()["card"]
    assert standing is not None

    named = next(i for i in range(1, 8) if i != standing["title"]["id"])
    pinned = (await client.get("/api/rate", params=[("head", named)])).json()["card"]
    assert pinned["title"]["id"] == named, "the CTA must land on a title the banner named"

    # Still idempotent: the same request returns the same card under the same token.
    again = (await client.get("/api/rate", params=[("head", named)])).json()["card"]
    assert again["token"] == pinned["token"] and again["title"]["id"] == named

    # And a plain refresh afterwards does not bounce back to the old card.
    plain = (await client.get("/api/rate")).json()["card"]
    assert plain["token"] == pinned["token"]


async def test_a_head_that_cannot_be_drawn_leaves_the_standing_card_alone(db, rate_client):
    """A head that never matches must not redraw on every GET."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 5)])
    await make_titles(db, [(90, "series", "A Series")])
    await label(db, user_id, 3, 2)
    await label(db, user_id, 90, 1)
    await client.post("/api/rate/session", json={"kinds": ["movie"]})

    standing = (await client.get("/api/rate")).json()["card"]
    for absent in (3, 90, 12345):
        held = (await client.get("/api/rate", params=[("head", absent)])).json()
        assert held["card"]["token"] == standing["token"], f"head={absent} should not have redrawn"
        assert held["session"]["kinds"] == ["movie"], "a pin nobody can serve widens nothing"


async def test_a_correction_repairs_the_pair_from_the_survivors_own_band(db, rate_client):
    """The survivor's opponent comes from its own band; drawing whole pairs missed small bands.
    Driven directly: random draws put the survivor in a small band only sometimes."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 25)])
    for title_id in range(1, 19):          # a large majority band: 18 liked, 153 pairs
        await label(db, user_id, title_id, 2)
    for title_id in range(19, 23):         # and a small one: 4 disliked, 6 pairs
        await label(db, user_id, title_id, 0)

    await client.post("/api/rate/session", json={"mode": "battle", "restart": True})
    s = await session.open_or_resume(db, user_id=user_id)

    # A pair from the SMALL band, every time — the case the old redraw lost.
    for corrected, survivor in ((19, 20), (20, 19)):
        card = {
            "type": "battle", "kind": "movie",
            "title_a": 19, "title_b": 20,
            "verdict_class": 0, "reason": "same band", "reask_of": None,
        }
        repaired = await session._redraw_pair(db, s, card, corrected=[corrected])
        assert repaired is not None, "the correction produced no card at all"
        assert repaired["type"] == "battle", (
            "the survivor was abandoned and the slot fell through to a sweep card — with "
            "nothing on screen saying why the battle vanished"
        )
        pair = {repaired["title_a"], repaired["title_b"]}
        assert survivor in pair, "the half the person did NOT correct must keep its place"
        assert corrected not in pair, "the corrected half must be gone"
        assert repaired["verdict_class"] == 0
        opponent = (pair - {survivor}).pop()
        assert opponent in (21, 22), (
            f"the opponent must come from the survivor's own band, not {opponent}"
        )


async def test_a_correction_through_the_surface_swaps_only_the_named_side(db, rate_client):
    """Takes whatever band the draw deals; kept for the route wiring. Its assertions are
    unconditional and its loop bounded: the fixture's smallest band has four titles."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 25)])
    for title_id in range(1, 13):
        await label(db, user_id, title_id, 2)
    for title_id in range(13, 17):
        await label(db, user_id, title_id, 1)
    for title_id in range(17, 21):
        await label(db, user_id, title_id, 0)

    card = (await client.get("/api/rate")).json()["card"]
    for _ in range(5):
        if card["type"] == "battle":
            break
        await client.post("/api/rate/session", json={"mode": "battle", "restart": True})
        card = (await client.get("/api/rate")).json()["card"]
    else:
        pytest.fail(f"five restarts in Battle mode and the surface never dealt a pair: {card}")

    left, right = card["left"]["id"], card["right"]["id"]
    body = (
        await client.post(
            "/api/rate/correction", json={"card_token": card["token"], "side": "left"}
        )
    ).json()

    after = body["card"]
    assert after is not None
    assert after["type"] == "battle", "the correction fell through to a sweep card"
    assert after.get("substituted_for") is None, "and it did so without saying so"
    titles_now = {after["left"]["id"], after["right"]["id"]}
    assert right in titles_now, "the half the person did NOT correct must keep its place"
    assert left not in titles_now, "the corrected half must be gone"

    # §6.1: the correction "writes no duel row for the pair" and does not advance the counter.
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", user_id) == 0
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, left
    ) == "unseen"


# Four taps as separate tests: the fixtures differ and a failure must name the tap.


async def _second_connection(pg_url: str) -> asyncpg.Connection:
    """Two coroutines on one connection are not concurrent. Installs `db`'s jsonb codecs, or the
    cards come back as text."""
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


@pytest.fixture
async def two_devices(app):
    """Both pool connections open concurrently before any write: warming them in turn serialises."""
    phone = app()
    created = await phone.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    user_id = (await phone.get("/api/auth/me")).json()["id"]
    laptop = app()
    laptop.cookies.update(phone.cookies)
    await asyncio.gather(phone.get("/api/health"), laptop.get("/api/health"))
    return phone, laptop, user_id


async def _both(first, second) -> tuple[list[int], list[Any]]:
    """`return_exceptions=True`: an escaped exception is a 500 to the person, worth a loud message."""
    answers = await asyncio.gather(first, second, return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            pytest.fail(
                f"a tap escaped every handler in app.py, which the person sees as a 500: "
                f"{answer!r}"
            )
    return sorted(a.status_code for a in answers), list(answers)


def _refusal(answers: list[Any], attempt: int) -> dict[str, Any]:
    """The 409's body, asserted to be §6.1's refusal and not a database constraint's name."""
    refused = next(a for a in answers if a.status_code == 409)
    detail = refused.json()["detail"]
    assert isinstance(detail, dict), (
        f"attempt {attempt + 1}: the loser left through app.py's generic conflict handler "
        f"({detail!r}) — the client has no rule for a constraint name, and by the time that "
        f"handler ran the tap had already told Jellyfin"
    )
    assert detail["reason"] in STALE_REASONS, f"attempt {attempt + 1}: {detail}"
    return detail


@pytest.mark.parametrize("tap", ("verdict", "not-seen", "skip"))
async def test_two_taps_on_one_card_token_leave_one_observation_and_one_409(db, two_devices, tap):
    """Eight attempts per tap, counts checked after each: two rows in one of eight is as broken."""
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 41)])
    await phone.post("/api/rate/session", json={"mode": "sweep"})
    body = {"verdict": {"value": 2}, "not-seen": {}, "skip": {}}[tap]

    for attempt in range(RACES):
        card = (await phone.get("/api/rate")).json()["card"]
        statuses, answers = await _both(
            phone.post(f"/api/rate/{tap}", json={"card_token": card["token"], **body}),
            laptop.post(f"/api/rate/{tap}", json={"card_token": card["token"], **body}),
        )
        assert statuses == [200, 409], f"attempt {attempt + 1}: {statuses}"
        _refusal(answers, attempt)

        # Decision 35's counter is over a sequence; two rows at one slot make Undo's depth unanswerable.
        assert await db.fetchval(
            "SELECT count(*) FROM rate_observation WHERE user_id = $1", user_id
        ) == attempt + 1, f"attempt {attempt + 1}: the journal took two rows for one card"
        assert await db.fetchval(
            "SELECT slot FROM rate_session WHERE user_id = $1 AND ended_at IS NULL", user_id
        ) == attempt + 2, f"attempt {attempt + 1}: the counter moved twice for one card"

        if tap == "verdict":
            assert await db.fetchval(
                "SELECT count(*) FROM verdict WHERE user_id = $1", user_id
            ) == attempt + 1, "§4.2 is append-only: a doubled tap is a doubled label"
        elif tap == "not-seen":
            assert await db.fetchval(
                "SELECT count(*) FROM user_title WHERE user_id = $1 AND state = 'unseen'",
                user_id,
            ) == attempt + 1
        else:
            assert await db.fetchval("SELECT count(*) FROM verdict") == 0
            assert await db.fetchval("SELECT count(*) FROM duel") == 0


async def test_two_answers_to_one_battle_card_write_exactly_one_duel(db, two_devices):
    """The taps answer differently: one would be a judgement the person never made."""
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 41)])
    for title_id in range(1, 41):
        await label(db, user_id, title_id, 2)
    await phone.post("/api/rate/session", json={"mode": "battle", "restart": True})

    for attempt in range(RACES):
        card = (await phone.get("/api/rate")).json()["card"]
        assert card["type"] == "battle", card
        statuses, answers = await _both(
            phone.post("/api/rate/duel", json={"card_token": card["token"], "outcome": "A"}),
            laptop.post("/api/rate/duel", json={"card_token": card["token"], "outcome": "B"}),
        )
        assert statuses == [200, 409], f"attempt {attempt + 1}: {statuses}"
        _refusal(answers, attempt)
        assert await db.fetchval(
            "SELECT count(*) FROM duel WHERE user_id = $1", user_id
        ) == attempt + 1, f"attempt {attempt + 1}: one pair, two contradictory duel rows"
        assert await db.fetchval(
            "SELECT count(*) FROM rate_observation WHERE user_id = $1", user_id
        ) == attempt + 1


async def test_a_slow_jellyfin_does_not_widen_the_double_tap_window(db, two_devices, monkeypatch):
    """The push is outside the transaction, so a slow Jellyfin cannot widen the window."""
    real = session._push_state

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real(*args, **kwargs)

    monkeypatch.setattr(session, "_push_state", slow)

    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 41)])
    await phone.post("/api/rate/session", json={"mode": "sweep"})

    for attempt in range(RACES):
        card = (await phone.get("/api/rate")).json()["card"]
        statuses, answers = await _both(
            phone.post("/api/rate/verdict", json={"card_token": card["token"], "value": 1}),
            laptop.post("/api/rate/verdict", json={"card_token": card["token"], "value": 1}),
        )
        assert statuses == [200, 409], f"attempt {attempt + 1}: {statuses}"
        _refusal(answers, attempt)
        assert await db.fetchval(
            "SELECT count(*) FROM verdict WHERE user_id = $1", user_id
        ) == attempt + 1


async def test_two_concurrent_gets_on_an_empty_table_serve_one_card_under_one_token(
    db, two_devices
):
    """Two GETs on an empty table both drew and stashed: one title under two tokens (finding 4)."""
    phone, laptop, user_id = two_devices
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    await phone.post("/api/rate/session", json={"mode": "sweep"})

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
        assert len(tokens) == 1, (
            f"attempt {attempt + 1}: two GETs stashed two tokens for one card ({tokens}), so "
            f"one of the two devices is holding a token the next tap will refuse"
        )
        stored = await db.fetchval(
            "SELECT card_token FROM rate_session WHERE user_id = $1 AND ended_at IS NULL",
            user_id,
        )
        assert tokens == {str(stored)}, "the token served is not the token stored"
        assert len({c["title"]["id"] for c in cards}) == 1

        # And the card that came back is answerable, which is the point of the token matching.
        answered = await phone.post(
            "/api/rate/verdict", json={"card_token": cards[0]["token"], "value": 1}
        )
        assert answered.status_code == 200, answered.text


async def test_a_banner_redraw_never_replaces_a_card_it_did_not_read(db, world):
    """Asserted as the state the race produces: over the route it split once in 40. Three arms of
    the compare-and-set, including a tap refilling the table first."""
    user = world["user"]
    s = await open_session(db, user)
    pinned = next(i for i in range(1, 21) if i != s.current_card["title_id"])
    stale = s  # the snapshot the second device is holding when the first one's redraw lands

    s = await session.ensure_card(db, s, head=[pinned])
    assert s.current_card["title_id"] == pinned, "the CTA redraws onto one of its named titles"
    winner = str(s.card_token)
    assert winner != str(stale.card_token), "the redraw is a new card and so a new token"

    loser = await session.ensure_card(db, stale, head=[pinned])
    assert str(loser.card_token) == winner, (
        "the second device was handed a token the session does not carry, so its first tap is a "
        "409 telling the person a card nobody answered was already answered"
    )
    assert loser.current_card["title_id"] == pinned, "and it is still the title the banner named"
    assert str(await db.fetchval("SELECT card_token FROM rate_session WHERE id = $1", s.id)) == (
        winner
    ), "the token served is not the token stored"

    # The point of the token matching: the card both devices hold is answerable.
    out = await session.record_verdict(db, loser, card_token=winner, value=2, hp=HP)
    assert out.session.slot == 2

    # A tap refills the table via `ensure_card`, so the losing redraw serves the card stored:
    # answerable, rather than clobbering another device's card.
    s = out.session
    stale = s
    s = (await session.record_skip(db, s, card_token=token(s))).session
    refilled = s.current_card["title_id"]
    named = next(
        i for i in range(1, 21) if i not in (pinned, refilled, stale.current_card["title_id"])
    )

    served = await session.ensure_card(db, stale, head=[named])
    assert served.current_card["title_id"] == refilled, (
        "the banner device replaced a card the tap had just drawn, so the phone's token is dead"
    )
    assert str(served.card_token) == str(s.card_token), "the card served is not the card stored"
    assert (
        await session.record_verdict(db, served, card_token=str(served.card_token), value=1, hp=HP)
    ).session.slot == 4, "the card both devices now hold must still be answerable"


async def test_a_skip_that_dies_between_its_two_statements_leaves_no_phantom_journal_row(
    db, rated, monkeypatch
):
    """`_append` is an INSERT then an UPDATE; on autocommit, a death between them wedged the
    session. Injected at the cursor move: the two statements must be one unit."""
    user = rated["user"]
    s = await open_session(db, user, mode="sweep")
    real = asyncpg.Connection.fetchrow

    async def die(self, query, *args, **kwargs):
        if "SET seq = $2" in query:
            raise RuntimeError("the task was cancelled between the journal row and the cursor")
        return await real(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", die)
    with pytest.raises(RuntimeError):
        await session.record_skip(db, s, card_token=token(s))
    monkeypatch.undo()

    assert await db.fetchval("SELECT count(*) FROM rate_observation") == 0, (
        "the journal kept a row at seq 1 with the session still at seq 0: every later append in "
        "this session now collides with rate_observation_seq, and nothing but deleting the "
        "session recovers it"
    )
    # The session is still answerable, which is the half the wedged state destroyed.
    s = await session.open_or_resume(db, user_id=user)
    out = await session.record_skip(db, s, card_token=token(s))
    assert out.session.slot == 2


@pytest.fixture
async def linked_sweep(db, linked):
    """`linked` rates every owned title; the sweep taps that push need a non-empty queue."""
    await db.execute("DELETE FROM verdict")
    await db.execute("DELETE FROM user_title")
    return linked


async def test_the_loser_of_a_double_tap_never_tells_jellyfin(
    db, pg_url, linked_sweep, monkeypatch
):
    """The loser's Played write used to go out before its rollback. Counting `record_verdict` tells
    the lock from its backstop. The fake refuses unknown ids, so attempts reuse its six items."""
    user, jf, module = linked_sweep["user"], linked_sweep["jf"], linked_sweep["module"]
    observed = 0
    real_record = observations.record_verdict

    async def counting(*args, **kwargs):
        nonlocal observed
        observed += 1
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(observations, "record_verdict", counting)
    other = await _second_connection(pg_url)
    try:
        for attempt in range(RACES):
            for table in ("rate_observation", "rate_session", "verdict", "user_title"):
                await db.execute(f"DELETE FROM {table}")
            s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
            s = await session.set_controls(db, s, mode="sweep")
            s = await session.ensure_card(db, s)
            assert s.current_card is not None and s.current_card["type"] == "sweep"

            results = await asyncio.gather(
                session.record_verdict(db, s, card_token=token(s), value=2, hp=HP, jf=jf),
                session.record_verdict(other, s, card_token=token(s), value=0, hp=HP, jf=jf),
                return_exceptions=True,
            )
            assert len(module.state.write_log) == attempt + 1, (
                f"attempt {attempt + 1}: the losing tap told Jellyfin the person had seen a "
                f"title this app has no record of anyone rating — {module.state.write_log}"
            )
            assert observed == attempt + 1, (
                f"attempt {attempt + 1}: the losing tap reached `observations.record_verdict` "
                f"and wrote a ledger row the database then rolled back ({observed} calls for "
                f"{attempt + 1} answerable cards) -- the card was not claimed under a lock first"
            )
            refused = [r for r in results if isinstance(r, BaseException)]
            assert len(refused) == 1, f"attempt {attempt + 1}: {results}"
            assert isinstance(refused[0], session.StaleCard), (
                f"attempt {attempt + 1}: the loser was refused by the database rather than by "
                f"§6.1's card rule ({refused[0]!r}), and `api/rate.py` has no 409 for that"
            )
            assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 1
    finally:
        await other.close()


class _SlowPlayed(httpx.AsyncBaseTransport):
    """The fake Jellyfin with §7.3's one write made slow — §3.3's bad-weather case."""

    def __init__(self, inner: httpx.AsyncBaseTransport, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "UserPlayedItems" in request.url.path:
            await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


async def test_the_verdict_is_durable_before_jellyfin_answers(
    db, pg_url, fake_jellyfin, linked_sweep
):
    """The app-side write commits before the network call. 0.5 s so the 10 ms poll separates the
    two shapes by an order of magnitude."""
    module, transport = fake_jellyfin
    user = linked_sweep["user"]
    jf = session.Jellyfin(
        client=JellyfinClient(
            "http://jellyfin.test", module.API_KEY, transport=_SlowPlayed(transport, 0.5)
        ),
        cfg=linked_sweep["jf"].cfg,
    )
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]

    watcher = await _second_connection(pg_url)
    try:
        started = time.monotonic()
        tap = asyncio.create_task(
            session.record_verdict(db, s, card_token=token(s), value=2, hp=HP, jf=jf)
        )
        visible: float | None = None
        held = 0
        samples = 0
        while not tap.done():
            await asyncio.sleep(0.01)
            if visible is None and await watcher.fetchval(
                "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = $2",
                user,
                title_id,
            ):
                visible = time.monotonic() - started
            # A duration, not a state: `idle in transaction` is normal between round trips. 0.3 s is above
            # that and below the 0.5 s sleep. `clock_timestamp()`: `now()` is the transaction's start.
            samples += 1
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

    assert elapsed > 0.4, (
        f"the tap took {elapsed:.2f} s, so the slow transport was never reached and this test "
        f"measured nothing"
    )
    assert visible is not None and visible < 0.25, (
        f"the verdict row was invisible to a second connection for {visible!r} s of a "
        f"{elapsed:.2f} s tap: the Jellyfin call is inside the transaction"
    )
    assert samples >= 1, (
        "the transaction-duration clause was never evaluated, so it asserted its own initialiser"
    )
    assert held == 0, (
        "a backend held an open transaction for more than 0.3 s during the tap -- row locks held "
        "for a foreign server's latency, against a pool of ten"
    )
    # And the push still happened, with §7.3's stamp and §6.7's line.
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True}
    ]
    assert any("Jellyfin Played true" in line for line in out.log)
    assert await db.fetchval(
        "SELECT jf_synced_at IS NOT NULL FROM user_title WHERE user_id = $1 AND title_id = $2",
        user,
        title_id,
    ) is True


async def test_a_handed_off_push_answers_the_tap_first_and_settles_the_same_bookkeeping(
    db, pg_url, fake_jellyfin, linked_sweep
):
    """With `later` the tap answers first and the push settles on its own pooled connection, with
    the same stamp, `prior_state.pushed` correction (decision 207) and rail line."""
    from spielplan.db import pool as db_pool

    await db_pool.open_pool(pg_url, min_size=1, max_size=2)
    module, transport = fake_jellyfin
    user = linked_sweep["user"]
    jf = session.Jellyfin(
        client=JellyfinClient(
            "http://jellyfin.test", module.API_KEY, transport=_SlowPlayed(transport, 0.5)
        ),
        cfg=linked_sweep["jf"].cfg,
    )
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]
    rail.forget(user_id=user)

    started = time.monotonic()
    out = await session.record_verdict(
        db, s, card_token=token(s), value=2, hp=HP, jf=jf, later=session.settle_in_background
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.4, f"the tap waited {elapsed:.2f} s for a push that sleeps 0.5 s"
    assert "user_title.state = seen -> Jellyfin push follows" in out.log
    assert out.session.current_card is not None, "and the next card rode in with the answer"
    assert module.state.write_log == [], "the push had not happened when the tap was answered"

    await session.settled()
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True}
    ]
    assert await db.fetchval(
        "SELECT jf_synced_at IS NOT NULL FROM user_title WHERE user_id = $1 AND title_id = $2",
        user,
        title_id,
    ) is True
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert stored[0]["pushed"] is True, "Undo reads this flag to hand the Played flag back"
    assert "user_title.state = seen -> Jellyfin Played true" in [
        e["text"] for e in rail.recent(user_id=user)
    ], "the rail says how the push ended once it has"


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
    # Show the model opens §6.7's rail, where the push's line is read.
    await client.post("/api/auth/preferences", json={"show_model": True})
    await client.post("/api/rate/session", json={"mode": "sweep"})
    card = (await client.get("/api/rate")).json()["card"]

    answered, asked = [], set()
    for path, body in (
        ("/api/rate/verdict", {"value": 2}),
        ("/api/rate/not-seen", {}),
    ):
        asked.add(card["title"]["id"])
        started = time.monotonic()
        reply = await client.post(path, json={"card_token": card["token"], **body})
        answered.append(time.monotonic() - started)
        assert reply.status_code == 200, reply.text
        assert any("Jellyfin push follows" in line for line in reply.json()["log"]), reply.json()
        card = reply.json()["card"]
    untouched = next(int(i) for i in owned if int(i) not in asked)
    started = time.monotonic()
    reply = await client.post(f"/api/rate/title/{untouched}", json={"answer": "fine"})
    answered.append(time.monotonic() - started)
    assert reply.status_code == 200, reply.text
    assert max(answered) < 0.45, f"a Rate route waited for Jellyfin: {answered}"

    await session.settled()
    assert len(module.state.write_log) == 3, module.state.write_log
    assert {w["played"] for w in module.state.write_log} == {True, False}


async def test_handed_off_pushes_behind_a_hung_jellyfin_leave_the_pool_to_the_requests(db, pg_url):
    """At most `SETTLE_SLOTS` pushes hold a connection; the rest wait in memory, so a hung Jellyfin
    cannot drain the web pool."""
    from spielplan.db import pool as db_pool

    await db_pool.open_pool(pg_url)                  # the web pool's own ten
    live = db_pool.pool()
    running, most, done = 0, 0, []

    async def hung_push(conn: asyncpg.Connection) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await conn.fetchval("SELECT 1")
        await asyncio.sleep(0.2)                     # Jellyfin, not answering
        running -= 1
        done.append(1)

    for _ in range(10):
        session.settle_in_background(hung_push)
    await asyncio.sleep(0.05)
    assert live.get_size() - live.get_idle_size() <= session.SETTLE_SLOTS
    request = await live.acquire(timeout=0.5)        # a phone's next request is served at once
    await live.release(request)
    await session.settled()
    assert most == session.SETTLE_SLOTS and len(done) == 10


async def test_a_handed_off_push_that_gets_no_connection_is_left_owed_and_raises_nothing(
    db, pg_url, monkeypatch, caplog
):
    """The slot's acquire is bounded like a request's; the settlement is left to §7.3's sweep."""
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
    """A stop waits, bounded, for handed-off pushes before the pool closes."""
    from spielplan.core.config import settings
    from spielplan.models import basis

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    neutral = tmp_path / "no-dot-env"                # no developer .env seeds a connector here
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
            for _ in range(session.SETTLE_SLOTS + 2):   # two of them still waiting for a slot
                session.settle_in_background(push)
    finally:
        settings.cache_clear()
    assert len(ran) == session.SETTLE_SLOTS + 2


async def test_the_journal_records_the_push_that_happened_and_not_the_one_intended(
    db, linked_sweep
):
    """`prior_state.pushed` is corrected after the push resolves; `undo` reads exactly it."""
    user, jf = linked_sweep["user"], linked_sweep["jf"]
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]
    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP, jf=jf)
    assert any("Jellyfin Played true" in line for line in out.log)

    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert [e["title_id"] for e in stored] == [title_id]
    assert stored[0]["pushed"] is True, (
        "the push succeeded and the journal says it did not, so Undo will not retract it"
    )

    # The other two cases, on a title Jellyfin does not carry: owed, and still honest.
    await db.execute("UPDATE title SET jellyfin_id = NULL")
    s = out.session
    owed_id = s.current_card["title_id"]
    out = await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP, jf=jf)
    assert any("not pushed (not on Jellyfin)" in line for line in out.log)
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert [e["title_id"] for e in stored] == [owed_id]
    assert stored[0]["pushed"] is False, "an owed push must not be recorded as a push"


async def test_an_undo_of_a_not_seen_hands_back_the_played_flag_it_set(db, linked):
    """Not-seen's `_mark_pushed` was pinned by nothing. A `seen`, unverdicted title is served first,
    so dropping one verdict from `linked` puts it on the table."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    target = await db.fetchval(
        "SELECT title_id FROM verdict WHERE user_id = $1 ORDER BY title_id LIMIT 1", user
    )
    await db.execute("DELETE FROM verdict WHERE user_id = $1 AND title_id = $2", user, target)
    s = await open_session(db, user, mode="sweep")
    assert s.current_card["title_id"] == target, (
        "every other title is verdicted, so the queue has exactly one candidate"
    )

    s = (await session.record_not_seen(db, s, card_token=token(s), jf=jf)).session
    assert module.state.write_log[-1] == {
        "user": "jf-user-patrick", "item": f"jf-{target}", "played": False
    }
    stored = await db.fetchval(
        "SELECT prior_state FROM rate_observation WHERE session_id = $1 ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    assert [e["title_id"] for e in stored] == [target]
    assert stored[0]["pushed"] is True, (
        "the push happened and the journal says it did not, so Undo will skip the retraction"
    )

    await session.undo(db, s, hp=HP, jf=jf)
    assert module.state.write_log[-1] == {
        "user": "jf-user-patrick", "item": f"jf-{target}", "played": True
    }, "the Played flag this tap set was never handed back"
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, target
    ) == "seen"


async def test_an_undo_taken_while_the_push_is_in_flight_still_hands_the_played_flag_back(
    db, pg_url, fake_jellyfin, linked_sweep
):
    """An Undo while the push is in flight read `pushed = false`. `undo`'s `FOR UPDATE` on the
    journal row makes the repair hold in both interleavings."""
    module, transport = fake_jellyfin
    user = linked_sweep["user"]
    jf = session.Jellyfin(
        client=JellyfinClient(
            "http://jellyfin.test", module.API_KEY, transport=_SlowPlayed(transport, 0.5)
        ),
        cfg=linked_sweep["jf"].cfg,
    )
    s = await open_session(db, user, mode="sweep")
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
            session.record_verdict(db, s, card_token=token(s), value=2, hp=HP, jf=jf)
        )
        # Timed off the journal row becoming visible, not a sleep.
        while not await other.fetchval(
            "SELECT count(*) FROM rate_observation WHERE user_id = $1", user
        ):
            await asyncio.sleep(0.01)
        assert not tap.done(), "the push had already resolved, so this raced nothing"
        popped = await session.undo(
            other,
            await session.open_or_resume(other, user_id=user, kinds=["movie"]),
            hp=HP,
            jf=jf,
        )
        assert popped.undone == "verdict"
        await tap
    finally:
        await other.close()

    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 0
    assert module.state.write_log == [
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": True},
        {"user": "jf-user-patrick", "item": f"jf-{title_id}", "played": False},
    ], (
        "the tap's Played write was never handed back, so Jellyfin holds a flag this app has no "
        f"record of and the next seen-state sweep will adopt it: {module.state.write_log}"
    )
    assert module.state.played["jf-user-patrick"] == set()

    row = await db.fetchrow(
        "SELECT undone_at, prior_state FROM rate_observation WHERE user_id = $1 "
        "ORDER BY seq DESC LIMIT 1",
        user,
    )
    assert row["undone_at"] is not None
    assert row["prior_state"][0]["pushed"] is False, (
        "the follow-up UPDATE wrote `pushed = true` onto a row that had already been tombstoned"
    )


async def test_an_undo_whose_token_has_expired_asks_for_a_re_link(db, linked):
    """§7.3: a 401 on the retraction's write also asks for a re-link."""
    user, jf, module = linked["user"], linked["jf"], linked["module"]
    s = await open_session(db, user, mode="battle")
    corrected = s.current_card["title_a"]
    s = (
        await session.record_correction(db, s, card_token=token(s), side="left", jf=jf)
    ).session
    assert module.state.write_log[-1]["played"] is False
    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", user
    ) == "linked"

    expired = session.Jellyfin(
        client=jf.client,
        cfg=JellyfinConfig(
            url=jf.cfg.url, api_key=jf.cfg.api_key, user_tokens={str(user): "expired-token"}
        ),
    )
    await session.undo(db, s, hp=HP, jf=expired)

    assert await db.fetchval(
        "SELECT jellyfin_link_state FROM app_user WHERE id = $1", user
    ) == "needs_relink", (
        "the retraction was refused by Jellyfin and nothing asked the person to re-link"
    )
    # The app-side row is restored either way — §7.3's debt stands, it is not lost.
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user, corrected
    ) == "seen"


async def test_the_not_seen_route_writes_unseen_advances_and_preloads_the_next_card(
    db, rate_client
):
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await client.post("/api/rate/session", json={"mode": "sweep"})

    card = (await client.get("/api/rate")).json()["card"]
    title_id = card["title"]["id"]
    answered = await client.post("/api/rate/not-seen", json={"card_token": card["token"]})
    assert answered.status_code == 200
    body = answered.json()

    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = $2", user_id, title_id
    ) == "unseen"
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user_id) == 0
    assert body["session"]["block"]["slot"] == 2
    assert body["undo"] == {"available": True, "kind": "not_seen", "reason": None}
    assert body["card"] is not None and body["card"]["title"]["id"] != title_id
    assert body["reveal"] is None, "§6.1's reveal rides on the verdict and on no other answer"

    # The same token twice is the same 409 every other tap gives.
    again = await client.post("/api/rate/not-seen", json={"card_token": card["token"]})
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] in STALE_REASONS


async def test_two_long_names_on_the_rail_do_not_lose_the_verdict(db, rate_client):
    """After the commit no route may raise: `_claim_card` has nulled the token, so a retry cannot
    succeed. Both names are unconstrained data."""
    client, user_id = rate_client
    member = "Grandma's iPad in the living room and the one in the kitchen :-)"
    assert len(member) <= 64, "longer than AccountName allows, so the test cheats"
    await db.execute("UPDATE app_user SET name = $1 WHERE id = $2", member, user_id)
    long_name = ("The Assassination of Jesse James by the Coward Robert Ford " * 6)[:310]
    await make_titles(db, [(1, "movie", long_name)])
    # 42 characters of chrome with no refit clause, 71 with one: over `MAX_LINE` either way.
    assert len(member) + len(long_name) + 42 > rail.MAX_LINE

    card = (await client.get("/api/rate")).json()["card"]
    # §6.7's buffer is per process and keyed by user id, so earlier tests leave lines in it.
    rail.forget(user_id=user_id)
    answered = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 0}
    )
    assert answered.status_code == 200, answered.text
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user_id) == 1

    # The line is in the rail, shortened rather than missing: §6.7 still names who rated what.
    narrated = next(e for e in rail.recent(user_id=user_id) if e["text"].startswith("verdict("))
    assert len(narrated["text"]) <= rail.MAX_LINE, len(narrated["text"])
    assert member in narrated["text"], narrated["text"]
    assert long_name[:60] in narrated["text"] and "…" in narrated["text"], (
        "the title name has to be shortened rather than dropped"
    )
    rail.forget(user_id=user_id)

    # And the retry that a 500 would have forced is refused, which is why the 500 was unrecoverable.
    again = await client.post("/api/rate/verdict", json={"card_token": card["token"], "value": 0})
    assert again.status_code == 409
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user_id) == 1


async def test_the_envelope_carries_the_widgets_class_balance(db, rate_client):
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    for title_id in (1, 2, 3, 4, 5):
        await label(db, user_id, title_id, 2)
    await label(db, user_id, 6, 0)

    answered = await client.get("/api/rate")
    assert answered.status_code == 200
    balance = answered.json()["class_balance"]
    assert balance["counts"] == [1, 0, 5]
    assert balance["warn"] is False, "5 of 6 liked is past the 60% line, under decision 491's 15"
    assert balance["arms_at"] == 15


async def test_mix_serves_single_titles_until_a_block_of_ratings_stands(db, world):
    """Decision 492: the warm-up changes the counter's CALL rather than marking a substitution."""
    user = world["user"]
    s = await open_session(db, user)
    for n in range(session.MIX_WARMUP_LABELS):
        assert s.current_card["type"] == "sweep", f"card {n + 1} of a first block"
        assert s.current_card.get("substituted_for") is None, "a warm-up is not a substitution"
        body = await session.payload(db, s)
        assert body["session"]["block"]["serving"] == "sweep", n
        s = (
            await session.record_verdict(
                db, s, card_token=token(s), value=(2, 1)[n % 2], hp=HP
            )
        ).session

    assert (s.block_index, s.slot) == (1, 1)
    body = await session.payload(db, s)
    assert body["class_balance"]["total"] == session.MIX_WARMUP_LABELS
    assert body["session"]["block"]["serving"] == "battle", "decision 200's alternation resumes"
    assert s.current_card["type"] == "battle"
    assert s.current_card.get("substituted_for") is None


async def test_the_warm_up_is_mixes_alone(db, rated):
    """Decision 492 holds back Mix's choice only; Battle mode is the person's."""
    s = await open_session(db, rated["user"], mode="battle")
    assert s.current_card["type"] == "battle"
    assert session.warm_up("battle", mode="battle", labels=0) == "battle"
    assert session.warm_up("battle", mode="mix", labels=14) == "sweep"
    assert session.warm_up("battle", mode="mix", labels=15) == "battle"
    assert session.warm_up("sweep", mode="mix", labels=0) == "sweep"


async def test_a_pin_is_served_on_an_empty_table_even_where_a_battle_was_due(db, warm):
    """C5.2: a pin leads whatever the slot called for, marked as the substitution it is."""
    user = warm["user"]
    s = await open_session(db, user)
    assert s.current_card["type"] == "sweep" and s.current_card["title_id"] != 9
    s = (
        await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP, head=[9])
    ).session
    assert s.slot == 2, "slot 2 is a battle slot for a person holding a block of ratings"
    assert s.current_card["type"] == "sweep" and s.current_card["title_id"] == 9
    assert s.current_card["source"] == "pinned"
    assert s.current_card["substituted_for"] == "battle"


async def test_a_pin_lifts_this_sittings_skip_and_an_earlier_not_seen(db, world):
    """C5.2: the pin re-opens a skip and a "not seen", nothing else."""
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    skipped = s.current_card["title_id"]
    s = (await session.record_skip(db, s, card_token=token(s))).session
    not_seen = s.current_card["title_id"]
    s = (await session.record_not_seen(db, s, card_token=token(s))).session
    assert s.current_card["title_id"] not in (skipped, not_seen)

    s = await session.ensure_card(db, s, head=[skipped])
    assert s.current_card["title_id"] == skipped
    s = (await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)).session

    s = await session.ensure_card(db, s, head=[not_seen])
    assert s.current_card["title_id"] == not_seen
    assert s.current_card["reason"] == "You picked this one."


async def test_a_pin_of_the_other_kind_widens_the_session_to_serve_it(db, rate_client):
    """A pin of the other kind widens the session, keeping proposal 46's counter honest."""
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 5)])
    await make_titles(db, [(90, "series", "A Series")])
    await client.post("/api/rate/session", json={"kinds": ["movie"]})
    assert (await client.get("/api/rate")).json()["card"]["title"]["id"] != 90

    body = (await client.get("/api/rate", params=[("head", 90)])).json()
    assert body["card"]["title"]["id"] == 90
    assert sorted(body["session"]["kinds"]) == ["movie", "series"]


async def test_the_search_finds_a_title_and_its_pick_is_rated_on_the_card(db, rate_client):
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await make_titles(db, [(40, "movie", "Dunkirk")])
    await observations.record_not_seen(db, user_id=user_id, title_id=40)

    empty = await client.get("/api/rate/search", params={"q": ""})
    assert empty.status_code == 200 and empty.json()["items"] == []

    found = (await client.get("/api/rate/search", params={"q": "dunk"})).json()
    assert [h["id"] for h in found["items"]] == [40]
    assert found["items"][0]["rated"] is None

    card = (await client.get("/api/rate", params=[("head", 40)])).json()["card"]
    assert card["type"] == "sweep" and card["title"]["id"] == 40
    assert card["reason"] == "You picked this one."
    answered = await client.post(
        "/api/rate/verdict", json={"card_token": card["token"], "value": 2, "head": [40]}
    )
    assert answered.status_code == 200
    assert answered.json()["card"]["title"]["id"] != 40, "a rated pin is not served again"

    after = (await client.get("/api/rate/search", params={"q": "dunk"})).json()["items"]
    assert after[0]["rated"] == "liked"


async def test_the_reveal_carries_its_number_only_behind_show_the_model(db, rate_client):
    """Gated where the payload is built: a member with Show the model off is not SENT the number."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await seed_ledger(db, user_id, range(1, 9))
    for title_id in (1, 2, 3, 4):
        await label(db, user_id, title_id, 2)

    first = (await client.get("/api/rate")).json()["card"]
    body = (
        await client.post("/api/rate/verdict", json={"card_token": first["token"], "value": 2})
    ).json()
    reveal = body["reveal"]
    assert reveal["available"] is True
    assert reveal["text"].startswith("we'd have guessed")
    assert not re.search(r"\d", reveal["text"]), reveal["text"]
    assert {"cdf", "s", "label_count"}.isdisjoint(reveal), reveal
    assert str(MARKER_CDF) not in json.dumps(body)

    await client.post("/api/auth/preferences", json={"show_model": True})
    second = body["card"]
    assert second["type"] == "sweep", "five ratings: Mix is still in decision 492's warm-up"
    body = (
        await client.post("/api/rate/verdict", json={"card_token": second["token"], "value": 1})
    ).json()
    assert re.search(r" · cdf \d\.\d\d$", body["reveal"]["text"]), body["reveal"]["text"]
    assert body["reveal"]["cdf"] == pytest.approx(MARKER_CDF)


async def test_the_sweep_cards_p_seen_travels_only_behind_show_the_model(db, rate_client):
    """Gated in the payload (decision 486). A card not placed by P(seen) has no `model` at all, so
    the key cannot tell a re-ask."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])

    card = (await client.get("/api/rate")).json()["card"]
    assert card["type"] == "sweep"
    assert "model" not in card and "p_seen" not in card, card
    assert "%" not in card["reason"] and "likely" not in card["reason"], card["reason"]

    await client.post("/api/auth/preferences", json={"show_model": True})
    card = (await client.get("/api/rate")).json()["card"]
    assert 0.0 < card["model"]["p_seen"] < 1.0, card
    assert "%" not in card["reason"], "the number is beside the line, not in it"

    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 7, 'seen')", user_id
    )
    fresh = (await client.post("/api/rate/session", json={"restart": True})).json()["card"]
    assert fresh["title"]["id"] == 7 and fresh["reason"] == "You have this marked as seen."
    assert "model" not in fresh, "a recorded-seen card looks exactly like a re-ask"


async def test_the_recall_aid_never_shows_an_mpst_synopsis(db, world):
    """C9.2: an MPST synopsis retells the ending; an overview that IS the MPST text gets no aid."""
    spoiler = "The film begins with the suicide of Peter, and ends with Karen in the hospital."
    await db.execute("UPDATE title SET overview = $1 WHERE id = 7", spoiler)
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES "
        "(7, 'mpst', jsonb_build_object('plot_full', $1::text)), "
        "(6, 'mpst', jsonb_build_object('plot_full', 'a longer retelling nobody resolved'))",
        "  " + spoiler + "  ",
    )
    cards = await session._title_cards(db, [6, 7])
    assert cards[7]["recall_aid"] is None
    assert cards[6]["recall_aid"] == "A film about Title 6", "a non-MPST overview keeps its aid"
