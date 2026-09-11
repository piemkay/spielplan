"""The Rate session: the block machine, the six taps, and Undo. Spec v2.1 §4.2, §6.1, §7.3,
§13, decision 35.

Four questions, and the tests are grouped by which one they answer.

**What is served next?** §6.1's Mix "alternates sweep and battle" over "blocks of 15". The
prototype's bug — which the counter design exists to make unreachable — was that the index
advanced only on a verdict, so once Mix reached an odd slot no number of duels ever brought a
sweep card back. The test that matters is therefore not "sweep, battle, sweep" in the happy
case: it is the case where the card actually *served* is not the card the slot called for
(a battle with no pool substitutes a sweep), because that is the only sequence where "flip
from the slot" and "flip from the last card served" give different answers.

**What may the card carry?** Nothing the model believes about the title being rated (§6.1,
citing Cosley 2003). The reveal is computed before the write and served after it, and the
assertions run over the *serialised* payload so a nested field three levels down still counts.

**What does a tap write?** A verdict implies `seen`; `Not seen` writes `unseen`; there is no
third state (owner decision 2026-08-29). One battle answer is exactly one `duel` row —
including a Tie, which is data and never a dropped row. A correction writes no duel row at
all, and does not advance the counter.

**What can be taken back?** Decision 35: an observation journal with compensating writes,
bounded to the current block, restoring the exact card rather than the neighbouring queue
position.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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

HP = DEFAULTS

# §6.1's three refusals, as `api/rate.py._stale` names them. A double tap must leave through
# one of these and never through `app.py`'s generic conflict handler, which reports the name of
# a database constraint the surface has no rule for. [M4.10 finding 2]
STALE_REASONS = {"no_card", "stale_card", "wrong_card_type"}

# A race that passes once has not passed: the reproductions behind this milestone's findings 1
# and 2 failed in four and five of six attempts, not in six of six. Every gathered assertion
# below therefore runs at least eight times. [M4.10 plan, "assertions by layer"]
RACES = 8

# Every model quantity that would anchor a rating, and the marker values the fixtures write
# into `ledger_state` so a leak is visible as a literal string in the JSON.
FORBIDDEN_CARD_KEYS = {
    "predicted", "predicted_label", "s", "sigma", "cdf", "tier", "straddle",
    "score", "rank", "verdict_class", "band", "reask_of", "b", "gate", "beta",
}
MARKER_S = 0.777123
MARKER_CDF = 0.913357


# --- fixtures --------------------------------------------------------------------------------


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


async def make_user(db, name="patrick", role="admin") -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


async def label(db, user_id: int, title_id: int, value: int = 2) -> None:
    """Seen and verdicted — the battle pool's conjunction — written directly.

    Going through the surface would consume slots and cards; the pool is a fact about the
    tables, and these tests are about what the surface does with it.
    """
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
    """One person and twenty owned films. Twenty, because the block test spends fifteen."""
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 21)])
    return {"user": await make_user(db)}


@pytest.fixture
async def rated(db, world):
    """Four titles already in one verdict class, so a battle pair exists from the first slot."""
    for title_id in (1, 2, 3, 4):
        await label(db, world["user"], title_id, 2)
    return world


async def open_session(db, user_id, *, mode="mix", kinds=("movie",)) -> session.RateSession:
    s = await session.open_or_resume(db, user_id=user_id, kinds=list(kinds))
    if mode != "mix":
        s = await session.set_controls(db, s, mode=mode)
    return await session.ensure_card(db, s)


def token(s: session.RateSession) -> str:
    assert s.card_token is not None, "no card on the table"
    return str(s.card_token)


# --- §6.1: the block machine -----------------------------------------------------------------


def test_the_card_type_is_a_pure_function_of_the_monotone_counter_and_the_block_rolls_at_fifteen():
    """§6.1: "Mix (default — alternates sweep and battle); blocks of 15."

    Index 0 — slot 1 of the first block — is a sweep, so Mix opens on the one card a person with
    no ratings can answer.

    The arithmetic the type is derived from is the whole content of decision 200, so it is
    asserted as arithmetic: the index runs on past the roll, which is what the SLOT cannot do.
    Fifteen is odd, so a slot-derived type made slot 15 and the next block's slot 1 both sweeps —
    eight sweeps to seven battles and one consecutive-same pair per block.
    """
    assert [session.card_type_for("mix", n) for n in range(0, 6)] == [
        "sweep", "battle", "sweep", "battle", "sweep", "battle"
    ]
    assert session.card_type_for("sweep", 1) == "sweep"
    assert session.card_type_for("battle", 0) == "battle"

    # The counter the person reads, as one number: 15 per block, starting at 0.
    assert session.observation_index(0, 1) == 0
    assert session.observation_index(0, 15) == 14
    assert session.observation_index(1, 1) == 15
    assert session.observation_index(3, 8) == 52
    # And therefore alternation survives the roll, which is decision 200's whole claim.
    assert session.card_type_for("mix", session.observation_index(0, 15)) == "sweep"
    assert session.card_type_for("mix", session.observation_index(1, 1)) == "battle"

    assert session.advance(0, 1) == (0, 2)
    assert session.advance(0, 14) == (0, 15)
    assert session.advance(0, 15) == (1, 1)
    assert session.advance(3, 15) == (4, 1)


async def test_mix_alternates_on_the_counter_and_not_on_the_last_card_served(db, world):
    """§6.1's Mix, and the mutant this is built to kill.

    The sequence is chosen so that "flip from the slot" and "flip from the last card served"
    disagree. At slot 2 the counter calls for a battle and the person has exactly one rated
    title, so no pair exists and a sweep is substituted — the slot is untouched, which the
    payload says out loud (`substituted_for: 'battle'`). At slot 3 the counter calls for a
    sweep again. An implementation that flipped off the last card served would hand back a
    battle there; this asserts a sweep, and that the substitution marker is gone.
    """
    user = world["user"]
    s = await open_session(db, user)
    assert s.slot == 1
    assert s.current_card["type"] == "sweep"

    out = await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)
    s = out.session
    assert s.slot == 2
    # The counter says battle; the pool holds one title, so a pair is impossible.
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


async def test_a_run_of_duels_still_returns_sweep_cards(db, rated):
    """§6.1, and the prototype bug proposal 36 names: the queue index advanced only on a
    verdict, so a Mix session that reached an odd slot never came back from battles.

    Four duels in a row, and the sweep cards keep arriving between them.
    """
    user = rated["user"]
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


async def test_mix_keeps_alternating_across_the_block_roll(db, rated):
    """§6.1's "alternates sweep and battle" over "blocks of 15", asserted AT the roll.

    Fifteen is odd. A type derived from the slot therefore made slot 15 a sweep and the next
    block's slot 1 a sweep as well, so every block ended and the next began with the same card
    type: eight sweeps to seven battles, which over §6.1's 50-100-verdict target is three to
    seven fewer duels than the clause promises — in the arm §5.2 credits with within-liked
    resolution. The alternation test beside this one asserts the sequence WITHIN a block and so
    cannot see the one place it breaks; this one spends sixteen taps to cross it.

    Sixteen taps through the real taps rather than through `card_type_for`, because the claim is
    about what the surface serves: the pool grows as the sweeps are answered, so every battle
    slot has a pair and no substitution can stand in for the card under test. [decision 200]
    """
    s = await open_session(db, rated["user"])
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
    """§6.1: "each observation … advances the block counter by one". Verdict, not-seen, skip,
    duel and tie all do; only the corrections row does not, and the migration's
    `rate_observation_advances_rule` CHECK is what keeps that pinned to `kind_of`."""
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
    """§6.1: "the counter runs 1..15 and rolls into a new block."

    The counter claim alone, and deliberately: the roll used to be read as decision 35's commit
    as well, and decision 199 moved that point one observation later, to the first tap of the
    block the roll opens. Where it falls is asserted by
    `test_the_fifteenth_tap_stays_undoable_until_the_sixteenth_lands`; a second statement of it
    here would be a rule the code no longer has, 870 lines from the test that holds the rule it
    does have. [M4.10 cycle 2, M410-C2-D19-04; decisions 35, 199]
    """
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


# --- §6.8: what the surface says when the card is not the one the counter asked for ----------


async def test_the_empty_state_names_the_pool_that_is_empty_rather_than_claiming_it_is_all_rated(
    db, world
):
    """§6.8, on the one sentence a person sees when there is no card at all.

    There is one way to have no card and three reasons for it, and a single string served all
    three. The one that matters is Battle on a young profile: the pool is "seen AND verdicted
    within one class", so a member in their first week who taps Battle has an *empty* pool rather
    than an exhausted queue — and was told "You've rated everything we can queue right now", which
    is both false and points them at Rank instead of at the sweep that would fix it.

    Three sessions rather than one parametrisation, because the three causes are three different
    worlds: nothing rated (Battle), nothing left in the selected kinds (Sweep), and both (Mix).
    [M4.10 finding 20]
    """
    user = world["user"]

    battle = await session.payload(db, await open_session(db, user, mode="battle"))
    assert battle["card"] is None, "twenty unrated films is not a battle pool"
    assert battle["drained"]["cause"] == "pool"
    assert "rated everything" not in battle["drained"]["text"], (
        "the first-week member has rated nothing at all: "
        f"{battle['drained']['text']!r}"
    )
    assert "Sweep" in battle["drained"]["text"], "and the copy points at what fills the pool"

    # The same person with Series selected: this household has no series at all, so the sweep
    # queue really is spent for that partition and the old sentence is the true one.
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


async def test_every_card_that_stands_in_for_another_type_carries_its_marker(db, rated):
    """§6.1's substitution rule: "the slot is not changed", so the payload has to say why the
    card in front of the person is not the type the counter names.

    Three sites stash a card of the other type and only the first of them marked it. The banner's
    head redraw (§6.0's CTA, which "opens the §6.1 queue with those titles at the head") and both
    of `_redraw_pair`'s fallbacks left `substituted_for` unset, so `RateSweepCard`'s explanation
    disappeared and the payload contradicted itself: `serving` said battle over an unmarked sweep
    card. [M4.10 finding 21]
    """
    user = rated["user"]
    s = await open_session(db, user)
    assert s.current_card["type"] == "sweep" and s.current_card.get("substituted_for") is None

    # 1. The thin-pool substitution, which always worked — the guard that this stayed marked.
    lonely = await make_user(db, "lonely", "member")
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
    solitary = await make_user(db, "solitary", "member")
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


async def test_an_undo_across_a_mode_change_leaves_the_counter_naming_its_own_call(db, rated):
    """The one flip no draw-time marker can see, pinned so the payload's promise stays the truth.

    `_mark_substitution` marks a flip at the moment a card is DRAWN, which covers all three draws
    that stand one type in for another — and cannot cover this one: `set_controls` drops the card on
    a mode change and keeps the counter, and `undo` restores the journal's card verbatim without
    restoring the mode it was drawn under. So Mix -> Sweep -> Mix -> Undo puts a sweep card on the
    table while the counter, in the mode now in force, calls for a battle, with `substituted_for`
    null because nothing stood in for anything.

    That is `serving` doing what its own line says — "what the counter CALLS FOR, which is not always
    what is on the table" — and `payload`'s comment briefly claimed the opposite, that the two could
    disagree only on a marked substitution. This test is the claim's falsifier: it fails either if
    the marker is made to cover a flip it cannot see or if `serving` is quietly redefined as a second
    spelling of `card.type`, which would leave the counter's call nowhere and drift from
    `ops/devstub.py`'s mirror of the same definition. [M4.10 finding 21, cycle 1 M410-D8-03]
    """
    user = rated["user"]
    s = await open_session(db, user)
    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert s.slot == 2 and s.current_card["type"] == "battle", "slot 2 of block 0 is a battle"

    # Sweep for one tap, then back to Mix: the mode change drops the card and leaves the counter.
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


# --- §6.1: no prediction before the tap ------------------------------------------------------


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
    """A ranking for every title, all carrying the same two marker numbers, so a leak into any
    card shows up as a literal string.

    `fitted=True` runs a real `refit_user` first, and only then paints the markers over what it
    wrote. Since M4.10 finding 9 a Ledger cache MISS no longer fits inline — it stamps
    `refit_requested_at` and returns `rows=[]` for the 60 s job — so rows written straight into
    `ledger_state` with no `ledger_fit` behind them are a ranking the incremental path will not
    touch. A test whose subject is what the incremental update does to that number therefore
    needs the cache to exist; the ones that only need a belief to hide from a card do not, and
    pay nothing.
    """
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
    """§6.1: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003)", phrased
    "we'd have guessed the same" / "we'd have guessed {class}".

    Both halves are asserted, and the second half is what stops the first from being trivially
    satisfiable by a surface that simply never computes a prediction: the same numbers that are
    absent from the card are present, by name, in the response to the tap.
    """
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
    assert "cdf 0.91" in out.reveal["text"], "§6.8: the number appears beside its name"
    # And the card that comes back with it is still clean.
    assert_no_model_belief(await session.public_card(db, out.session))


async def test_the_battle_card_hides_the_verdict_band_it_was_drawn_from(db, rated):
    """The band is the person's own label rather than a model belief, and it still does not
    travel: it costs nothing to omit and it is an anchoring vector on a card whose whole job is
    to elicit a fresh comparison. Server-side it stays on `current_card`, because the write
    path needs it to repair the pair after a correction."""
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
    """Order of operations, made falsifiable: the reveal reports the ranking as it stood before
    this label existed. A handler that read `ledger_state` after the incremental update would
    report the number the label just moved — and "we'd have guessed the same" would be true by
    construction.

    `fitted=True` because the tripwire below asserts that the incremental update *did* move the
    number, and since M4.10 finding 9 an update with no cached fit behind it legitimately moves
    nothing: it queues the refit for the 60 s job and returns. Without a real `ledger_fit` this
    test would have gone on passing its first assertion while asserting the absence of a write
    it had stopped asking for.
    """
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
    """§3.1 makes a household with no fit a legal state, and a banded guess off a CDF that does
    not exist would be a number with no provenance."""
    s = await open_session(db, world["user"], mode="sweep")
    out = await session.record_verdict(db, s, card_token=token(s), value=1, hp=HP)
    assert out.reveal["available"] is False
    assert "yet" in out.reveal["reason"]


def _embeddings_from(vectors: dict[int, Any]):
    """§5.1's coordinate, from a fixture dict. The shape `observations.resolve_embeddings`
    enforces — (n, 64) and a mask — with one axis carrying the signal so the fitted `v` has a
    direction a test can reason about."""

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
    """§6.1: "Prediction reveal strictly *after* the tap", on the cards the queue actually deals.

    `predicted_class` read `ledger_state`, `refit_user` writes rows for observed titles plus
    *owned* ones, and §6.1's first queue is the imported seed list — 80 of whose 100 titles are
    unowned in v20260828. So measured on the real bundle the reveal was unavailable on 50 of 50
    taps for two members, and on 9 of 10 even after a full refit: the anchoring-safe feedback the
    whole surface is built around was dark for exactly the sitting §12's M2 exit criterion is
    defined over. §5.2 gives an unobserved title a coordinate at zero extra parameters, so the
    number was always computable from the cached fit; nothing was missing but the arithmetic.

    THE DISCRIMINATING ASSERTION IS THE PAIR. Two unowned titles, one pointing the way this
    person's liked titles point and one the opposite way, must come back with different numbers —
    an implementation that answered `mu` for every unobserved title would satisfy "available" and
    say the same thing about every film in the catalog. [M4.10 finding 26, §5.2]
    """
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
        s = await session.stash_card(
            db,
            s,
            {"type": "sweep", "kind": "movie", "title_id": title_id, "reason": "queued because",
             "p_seen": 0.4, "source": "p_seen", "reask_of": None},
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
    # Banded against the person's own labels, which is six for the first tap and seven for the
    # second — the first tap added one, and §5.2's band counts are live rather than fitted.
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
    """The first-sitting shape of finding 26, which is what §12's M2 criterion measures.

    Three taps on unowned titles — the queue a fresh household actually gets. The first two are
    dark and say so: §3.1 makes a fit-less household legal, §5.2's displayed weight is the CDF of
    the person's own fitted values, and one label is not a distribution. Then the fit runs, which
    is the 60 s `tier-set-refit` job doing what finding 9 moved out of the request, and the third
    tap is lit — on a title with no `ledger_state` row of its own, which before this milestone was
    the permanent state of the whole first sitting.
    """
    user = world["user"]
    for title_id, name in ((97, "First"), (98, "Second"), (99, "Third")):
        await _unowned(db, title_id, name)

    async def tap(title_id: int, value: int) -> dict[str, Any]:
        s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
        s = await session.stash_card(
            db,
            s,
            {"type": "sweep", "kind": "movie", "title_id": title_id, "reason": "queued because",
             "p_seen": 0.4, "source": "p_seen", "reask_of": None},
        )
        out = await session.record_verdict(db, s, card_token=token(s), value=value, hp=HP)
        return out.reveal

    first = await tap(97, 2)
    assert first["available"] is False and "yet" in first["reason"]
    second = await tap(98, 0)
    assert second["available"] is False, "two labels, and nothing fitted them yet"

    # What the sweep does sixty seconds later (finding 9: the first tap queues the fit rather than
    # running §5.3's "seconds" row inside a request that has already committed).
    assert (await refit.refit_user(db, user_id=user, kind="movie", hp=HP)).fitted
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id = $1 AND title_id = 99", user
    ) == 0, "title 99 is unowned, so the fit that just ran wrote it no row"

    third = await tap(99, 1)
    assert third["available"] is True, third.get("reason")
    assert third["label_count"] == 2, "banded against the two labels the fit was built on"
    assert 0.0 <= third["cdf"] <= 1.0


# --- §4.2 / §6.1: the two seen states ---------------------------------------------------------


async def test_a_verdict_writes_seen_and_not_seen_writes_unseen(db, rated):
    """§6.1: "Verdict implies `seen`", and the `Not seen` control is the one seen-state control
    (owner decision 2026-08-29: a title you cannot remember is plain `unseen`)."""
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
    """Owner decision 2026-08-29 removed `forgotten`. Both halves are asserted: the column
    refuses it, and the surface has no tap that could ask for it."""
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
    """§4.2: "verdict/duel history is append-only and survives the flip." The title leaves the
    battle pool because the pool is a conjunction — marked seen AND carrying a live verdict —
    not because anything was deleted."""
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


# --- §6.1 / §4.2: the duel write ---------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["A", "B", "TIE"])
async def test_one_battle_answer_writes_exactly_one_duel_row(db, rated, outcome):
    """§4.2: outcome A | B | TIE, context `profile_battle`. A Tie is a stored outcome feeding
    the Davidson tie term — 22% of random pairs are genuine ties — and never a skip, never a
    dropped row."""
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
    # §13 stream (a) evaluates the TIER model against adaptively-selected pairs; a profile
    # battle is random by design and is not part of that held-out sample.
    assert row["selection"] == "random"
    journal = await db.fetchrow(
        "SELECT kind_of, duel_id, title_ids FROM rate_observation WHERE session_id = $1", s.id
    )
    assert journal["kind_of"] == ("tie" if outcome == "TIE" else "duel")
    assert journal["duel_id"] == row["id"]
    assert out.session.slot == 2


async def test_the_decisive_toggle_is_persistent_and_sets_the_stored_margin(db, rated):
    """§6.1: "a persistent **decisive toggle** sets the margin weight (~1.6 vs 1.0)".

    Persistent means it belongs to the session, not to the request: one tap on the toggle
    changes every answer after it. The two numbers come from `hp.margin_for`, so they stay in
    `ledger_hyperparams.json` where §4.3 puts them.
    """
    user = rated["user"]
    s = await open_session(db, user, mode="battle")
    assert s.decisive is False
    s = (await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)).session

    s = await session.set_controls(db, s, decisive=True)
    assert s.current_card is not None, "the toggle changes the weight, not the question"
    s = (await session.record_duel(db, s, card_token=token(s), outcome="B", hp=HP)).session
    s = (await session.record_duel(db, s, card_token=token(s), outcome="A", hp=HP)).session

    margins = [
        r["margin"]
        for r in await db.fetch("SELECT margin FROM duel WHERE user_id = $1 ORDER BY id", user)
    ]
    assert margins == pytest.approx([1.0, 1.6, 1.6])
    assert HP.margin_for(True) == 1.6 and HP.margin_for(False) == 1.0


async def test_one_answer_may_override_the_toggle_without_moving_it(db, rated):
    """§6.1 keeps long-press "as an optional accelerator only": a decisive answer without a
    trip to the toggle, and the toggle is where it was afterwards."""
    s = await open_session(db, rated["user"], mode="battle")
    out = await session.record_duel(
        db, s, card_token=token(s), outcome="A", decisive=True, hp=HP
    )
    assert out.session.decisive is False
    assert await db.fetchval("SELECT margin FROM duel") == pytest.approx(1.6)


# --- §6.1: the corrections row -----------------------------------------------------------------


@pytest.mark.parametrize("side", ["left", "right"])
async def test_a_correction_unsees_exactly_the_named_side_and_writes_no_duel(db, rated, side):
    """§6.1: "`not seen: [left] [both] [right]` -> sets that side `unseen`, swaps it out of the
    pair … writes no duel row, syncs per §7.3, covered by the persistent Undo.\""""
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
    # The survivor keeps its place; the corrected half is replaced.
    assert s.current_card["type"] == "battle"
    assert survivor in (s.current_card["title_a"], s.current_card["title_b"])
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
    """§6.1's corrections zone repairs the question rather than answering it, and the
    migration's `rate_observation_advances_rule` CHECK pins that to `kind_of` so the two
    cannot drift apart."""
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
    """A household whose Jellyfin is reachable and whose person has their own token — §7.3's
    least-privilege write path, not the admin key."""
    module, transport = fake_jellyfin
    # Ids the fake server actually carries: the pair is drawn at random, so every member of the
    # pool has to be a real Jellyfin item or the assertion depends on the draw.
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
    """§6.1: the correction "syncs per §7.3", and §7.3's mapping is the plain boolean:
    `unseen` -> Played = false, written with that user's own token."""
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
    """Decision 35's compensating write reaches Jellyfin too, and symmetrically.

    The app-side row is restored byte for byte by `observations.undo` — §7.3's `jf_synced_at`
    loop guard included — so the only thing left owed is the Played flag we ourselves set. If
    the retraction did not push, the next sweep would read our own write back as Jellyfin's
    history and re-apply it.
    """
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
    """The other half of the symmetry: a title Jellyfin does not carry was never pushed, so its
    retraction writes nothing there either. Undo compensates what it did, not what it
    intended."""
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
    """§3.3: the app must work when Jellyfin is down. §7.3's table reads a present row with a
    NULL `jf_synced_at` as "the person acted and Jellyfin has not been told yet", which is
    exactly the queued sync the next sweep settles."""
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


# --- decision 35: undo -------------------------------------------------------------------------


async def test_undo_pops_a_verdict_restores_the_exact_card_and_retracts_the_row(db, rated):
    """Decision 35: "an observation journal with compensating writes", and the card that comes
    back is the card that produced the observation — not the neighbouring queue position.

    `fitted=True` for the reason `test_the_reveal_is_read_before_the_write_and_not_after_it`
    gives, and for the assertion below: with no `ledger_fit` behind it the retraction's update is
    a cache miss, which queues the refit and applies nothing, so `applied is True` was reading a
    report that claimed a fit the request had not run. [M4.10 cycle 2,
    m410-c2-cache-miss-reports-a-refit-that-did-not-run]
    """
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21), fitted=True)
    s = await open_session(db, user, mode="sweep")
    title_id = s.current_card["title_id"]
    card_before = dict(s.current_card)

    s = (await session.record_verdict(db, s, card_token=token(s), value=2, hp=HP)).session
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE title_id = $1", title_id) == 1

    out = await session.undo(db, s, hp=HP)
    s = out.session
    # The compensating *Ledger* write, not merely a tombstone: §5.3's incremental update runs
    # again over the title the retraction touched.
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
    """Decision 35's other half: a battle pair is drawn at random, so an Undo that redrew would
    land the person on a question they never answered. Both journal kinds are covered — a tie
    is a `kind_of` of its own and must retract exactly like the duel it is."""
    s = await open_session(db, rated["user"], mode="battle")
    pair = (s.current_card["title_a"], s.current_card["title_b"])
    s = (await session.record_duel(db, s, card_token=token(s), outcome=outcome, hp=HP)).session

    s = (await session.undo(db, s, hp=HP)).session
    assert (s.current_card["title_a"], s.current_card["title_b"]) == pair
    assert await db.fetchval("SELECT count(*) FROM duel") == 0
    assert s.slot == 1


@pytest.mark.parametrize("tap", ["not_seen", "skip", "correction"])
async def test_undo_pops_an_observation_of_any_kind(db, rated, tap):
    """The coverage row's word is ANY: "verdict, not-seen, skip, duel, tie, correction". A
    single last-verdict slot could not cover the corrections row at all."""
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
    """The journal row *is* the suppression (`undone_at IS NULL` in the exclusion predicate),
    so undoing a skip has to put the title back in play, not merely back on screen."""
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
    """Decision 35: "back to the start of the current block of 15 and no further; … the chip
    disables visibly at the boundary."

    Two boundaries, and both are refusals with a reason: an empty journal, and the start of the
    block the person is in once the block before it has been committed.

    WHERE THAT COMMIT FALLS MOVED IN M4.10. Decision 199 applies decision 174's ruling — a block
    is finished when the FIRST observation of the next one lands, not when the fifteenth of this
    one does — so the fifteenth-tap claim this test used to make is gone, to
    `test_the_fifteenth_tap_stays_undoable_until_the_sixteenth_lands` below. What is asserted here
    is the boundary that remains: the sixteenth tap commits block 0, and retracting the sixteenth
    does not hand it back. Otherwise undo's depth would be a walk rather than a block — every
    earlier block would return one tap at a time, because undo restores the session to the block
    the row it popped was made in. [decisions 35, 174, 199]
    """
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    with pytest.raises(session.UndoUnavailable) as empty:
        await session.undo(db, s, hp=HP)
    assert empty.value.reason == "empty"

    for _ in range(16):
        s = (await session.record_skip(db, s, card_token=token(s))).session
    assert (s.block_index, s.slot) == (1, 2), "fifteen taps rolled the block and one landed in it"

    # The sixteenth tap is the commit, so its own retraction comes back and the block it closed
    # does not: decision 199's "until the sixteenth lands", which is where decision 35's "and no
    # further" now bites. The fifteenth tap's own reachability — before the sixteenth — is the
    # test below this one.
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
    """Decision 199, which is decision 174's ruling applied: a block is committed when the FIRST
    observation of the NEXT block lands.

    `advance` rolls the counter on the fifteenth observation, so comparing the journal row's
    block with the session's disabled the chip on the same round trip that answered card 15 —
    6.7% of every observation a household makes, at the end of a run where the fatigue mis-taps
    live, and in Battle mode a duel, which §4.2 gives no supersede path. Decision 35's reason for
    the depth is "the depth matches the counter the user is already reading", and the person
    reading "1 / 15" has just made a tap they can see and could not take back.

    Three things are asserted, because the rule has three halves: the fifteenth tap is reachable
    while the new block is empty, undoing it lands the session back at 15 with the card it
    answered (the journal's stored block and slot are what restores it, and decision 199 did not
    touch them), and one observation in the new block closes the old one for good.
    """
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

    # Re-answer it, then spend one tap in the new block: that is the commit. The fifteenth is out
    # of reach from there on — the chip disables, visibly, one tap later than it used to.
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
    """Repeated Undo, which is how a person actually uses it. Three taps come back one at a
    time; the fourth is refused."""
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
    """§4.2: a re-rating supersedes rather than overwrites, so its undo has to *un*-supersede —
    `observations.undo` splices the chain, and this is the surface end of that."""
    user = rated["user"]
    original = await db.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", user
    )
    s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
    s = await session.stash_card(
        db,
        s,
        {"type": "sweep", "kind": "movie", "title_id": 1, "reason": "re-rating", "p_seen": None,
         "source": "p_seen", "reask_of": None},
    )
    s = (await session.record_verdict(db, s, card_token=token(s), value=0, hp=HP)).session
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original)

    await session.undo(db, s, hp=HP)
    assert await db.fetchval("SELECT superseded_by FROM verdict WHERE id = $1", original) is None
    assert await db.fetchval(
        "SELECT count(*) FROM verdict WHERE user_id = $1 AND title_id = 1", user
    ) == 1


# --- §13 stream (b): the write half of the silent re-ask ------------------------------------


async def test_a_re_ask_is_written_distinguishably_and_shown_indistinguishably(db, rated):
    """§13: "~10% of comparisons/verdicts re-asked after >= 3 days" — invisible in the payload,
    distinguishable in the row, and counted once.

    The draw belongs to `rate.queue` and `rate.reask`; what is asserted here is the half this
    module owns: the card the server holds carries the reference, the card the client gets does
    not, the written row carries `is_reask`/`reask_of`, and the class-balance widget — which is
    what a person actually reads — does not count the answer twice.
    """
    user = rated["user"]
    original = await db.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1", user
    )
    s = await session.open_or_resume(db, user_id=user, kinds=["movie"])
    s = await session.stash_card(
        db,
        s,
        {"type": "sweep", "kind": "movie", "title_id": 1, "p_seen": 1.0, "source": "reask",
         "reason": "queued because: you have this marked seen", "reask_of": original},
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


# --- the card token ------------------------------------------------------------------------------


async def test_a_card_can_only_be_answered_once_and_only_by_the_control_it_carries(db, rated):
    """The token is why a client cannot answer a card it was never served. It is also the
    double-tap guard: the second tap names a card that is no longer on the table."""
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


# --- the session itself ---------------------------------------------------------------------------


async def test_one_live_session_per_person_and_a_resume_returns_the_same_card(db, world):
    """`rate_session_one_live` says one; two would each hold a block counter and Undo would
    have to guess which one a tap belonged to."""
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
    """§6.1: "Modes: **Mix** (default …)". Every entry point lands on the same card type."""
    s = await session.open_or_resume(db, user_id=world["user"])
    assert s.mode == "mix"
    assert sorted(s.kinds) == ["movie", "series"]


async def test_changing_the_kinds_drops_the_card_and_never_leaves_neither_selected(db, world):
    """Decision 18: Films and Series are two toggles, "either or both active, never neither" —
    enforced by `library.normalise_kinds`, which is already tested, rather than re-implemented
    here. A film pair is meaningless once Films is off, so the card is dropped."""
    user = world["user"]
    s = await open_session(db, user, kinds=("movie", "series"))
    assert s.current_card is not None
    s = await session.set_controls(db, s, kinds=["series"])
    assert s.current_card is None and s.card_token is None
    with pytest.raises(ValueError, match="at least one kind"):
        await session.set_controls(db, s, kinds=[])


# --- the routes ------------------------------------------------------------------------------------


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
    """One envelope for every route, and the next card rides in the response to the write —
    §6 preamble: "<2 s per sweep card … next card preloaded"."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    await seed_ledger(db, user_id, range(1, 9))
    for title_id in (1, 2, 3, 4):
        await label(db, user_id, title_id, 2)

    first = (await client.get("/api/rate")).json()
    assert first["session"]["block"] == {
        "index": 0, "slot": 1, "size": 15, "counter": "1 / 15", "serving": "sweep"
    }
    assert first["card"]["type"] == "sweep"
    assert first["undo"] == {"available": False, "kind": None, "reason": "empty"}
    assert first["class_balance"]["counts"] == [0, 0, 4]
    assert first["class_balance"]["warn"] is True, "4 of 4 liked is past the 60% line"
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
    """Decision 35, over HTTP: the refusal carries the reason the chip renders."""
    client, _user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 6)])
    await client.get("/api/rate")
    refused = await client.post("/api/rate/undo")
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "empty"


async def test_the_route_rejects_an_empty_kind_selection(rate_client):
    """Decision 18: never neither."""
    client, _user_id = rate_client
    assert (await client.post("/api/rate/session", json={"kinds": []})).status_code == 422


async def test_the_rate_routes_need_a_signed_in_account(app, db):
    """Every other router in `spielplan/api/` sits behind the same dependency; a surface that
    wrote to one person's ledger without a session would be a different kind of bug."""
    client = app()
    assert (await client.get("/api/rate")).status_code == 401
    assert (
        await client.post("/api/rate/verdict", json={"card_token": "x", "value": 1})
    ).status_code == 401


async def test_a_tap_reaches_the_transparency_rail(db, rate_client):
    """§6.7: the rail narrates "**every model write** in one human-readable line", and calls
    itself "the primary M2 debugging instrument".

    Every route already returns its lines in `log`, which is what the client renders for the tap
    just made. Nothing recorded them anywhere, so the rail — the thing the toggle exists to
    reveal — was empty in production while every unit test of the formatters passed. This is the
    test that the producer exists at all.

    A skip is deliberately absent: §6.7 says every model *write*, and a skip writes no
    observation row. Its own log line says so.
    """
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

    # Decision 117: with the toggle off the lines are ABSENT from the response, not hidden by
    # the client. Recording them still happens — the buffer is what the toggle reveals, so it
    # has to have been filling while the toggle was off, or turning it on would show nothing
    # until the next tap.
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
    """§6.7's commonest line: `verdict(jenny, Heat) = liked -> ordered-logit arm, incremental
    refit 31 ms` — the person, the film, and what the write actually cost.

    What shipped instead was `ledger/observations.py`'s audit sentence forwarded unchanged,
    `verdict(title 3) = liked -> ordered-logit arm`: a bare integer, which §6.8 rules out
    because nothing on the client can resolve it into the film it names. `rail.verdict_line`
    had rendered §6.7's format since M2 and was called from `test_home.py` and from
    `ops/devstub.py:1782`, which has narrated the person, the film and the ms since M2 — the
    harness modelled the line the app it stands in for never sent. The event's own `title_id`
    field was null at every producer, and the incremental refit's milliseconds were computed
    in the same handler and thrown away.

    Driven through the route rather than through `record_verdict`, because the four facts meet
    in three different places: the person is the request's session user, the film and the label
    are the handler's, and the milliseconds come from the ledger delta the same response
    reports under `ledger.ms` — which is what lets the number in the line be checked against
    something other than itself.

    The films are named rather than numbered on purpose. `Title 5` would satisfy the negative
    assertion below by accident; a cast with no digit in it makes any digit in the sentence one
    the renderer put there.
    """
    from spielplan.home import rail

    films = ["Heat", "Drive", "Ronin", "Collateral", "Thief", "Sicario", "Zodiac", "Michael Clayton"]
    client, user_id = rate_client
    rail.forget()
    await make_titles(db, [(i, "movie", films[i - 1]) for i in range(1, 9)])
    for title_id in (1, 2, 3, 4):
        await label(db, user_id, title_id, 2)
    # The labels first and `fitted=True` after them, because `refit_user` writes no `ledger_fit`
    # for a person with no observations — and without one the tap's update is a cache miss, which
    # since finding 9 queues the refit and times nothing. The assertion below said as much in its
    # own message ("the fixture owes a real incremental refit"); it was passing on a report that
    # claimed the fit anyway. [M4.10 cycle 2, m410-c2-cache-miss-reports-a-refit-that-did-not-run]
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

    # (a) §6.7's format, naming the person and the film's NAME. Compared against the renderer
    # rather than against a literal because `test_home.py` already pins what that renderer
    # emits; what is under test here is that the producer calls it, with these four facts.
    assert line["text"] == rail.verdict_line(rater, rated_name, "liked", refit_ms=ms)
    assert f"verdict({rater}, {rated_name}) = liked" in line["text"]
    assert "ordered-logit arm" in line["text"]

    # (b) the milliseconds are the delta's own, not a decoration: the same number the response
    # reports under `ledger.ms`, which `refit.update_incrementally` measured.
    printed = re.search(r"incremental refit (\d+) ms", line["text"])
    assert printed is not None, line["text"]
    assert printed.group(1) == f"{ms:.0f}"

    # (c) the event's own field, null at every producer before this one.
    assert line["title_id"] == rated_id

    # And the negative the finding is named after.
    assert re.search(r"title \d", line["text"]) is None, line["text"]

    # One sentence, not two renderings: §6.1's per-response echo is the same line the rail took.
    assert body["log"][0] == line["text"]
    rail.forget()


async def test_a_cache_miss_says_the_fit_is_owed_rather_than_timing_one_that_never_ran(
    db, rate_client
):
    """Finding 9's repair, on the two sentences it drives — §6.7's rail and the model log.

    A miss no longer fits inline: it stamps `refit_requested_at` and returns `rows=[]` for the
    60 s sweep. The report handed to the client kept `refit: true` and the millisecond count the
    inline fit used to earn, so the first tap per (user, kind) rendered "ledger movie - refit
    12.6 ms" under the toggle and recorded ", incremental refit 13 ms" on the rail, while
    `ledger_fit` held nothing, `ledger_state` had not moved and nothing anywhere said a fit was
    owed. Before this milestone the same branch really did run the fit, so both numbers described
    work that had been done; the repair removed the work and kept the sentences.

    Asserted on a household that has just turned decision 117's toggle on, because that is when
    it is read: every tap is a miss until the sweep lands, and so is every tap after a bundle
    import, after an `hp_digest` change and on the NaN fallback.
    [M4.10 cycle 2, m410-c2-cache-miss-reports-a-refit-that-did-not-run]
    """
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
    """§6.0's pending-verdicts banner: its CTA "opens the §6.1 queue with those titles at the
    head of the queue, **not** at whatever position the standing queue held" — because "naming
    titles and then presenting a different card is worse than no prompt".

    The case that matters is the only one that happens in practice: the person already has a
    session with a card stashed. `GET /api/rate` is deliberately idempotent so a refresh cannot
    redraw, and that idempotency swallowed the head — the banner named three films and the
    surface served whatever was already sitting there.
    """
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
    """The other half: if none of the named titles can be served — already rated, or not of this
    session's kinds — the surface must keep the card it has rather than redrawing on every GET.
    A head that never matches would otherwise make the card a moving target, which is what the
    idempotency exists to prevent."""
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 5)])
    await make_titles(db, [(90, "series", "A Series")])
    await label(db, user_id, 3, 2)
    # Films only, so the series below is genuinely out of this session's reach rather than a
    # title the queue would happily have served.
    await client.post("/api/rate/session", json={"kinds": ["movie"]})

    standing = (await client.get("/api/rate")).json()["card"]
    for absent in (3, 90, 12345):
        held = (await client.get("/api/rate", params=[("head", absent)])).json()["card"]
        assert held["token"] == standing["token"], f"head={absent} should not have redrawn"


async def test_a_correction_repairs_the_pair_from_the_survivors_own_band(db, rate_client):
    """§6.1: "`not seen: [left] [both] [right]` sets exactly the named side(s) to unseen" — the
    other half keeps its place, against a fresh opponent from its own verdict band.

    The opponent used to be found by asking for whole PAIRS and rejecting any outside the
    survivor's class, eight times. `battle.draw` weights strata by pair count n(n-1)/2, so on a
    60/20/20 labeller — the shape §5.2's class-balance warning pushes people toward — a minority
    band was missed on all eight attempts about half the time, and the battle silently became a
    sweep card with nothing on screen saying why.

    Driven directly rather than through whatever the surface happens to deal: the failure only
    shows when the SURVIVOR is in a small band, and a test that waits for random draws to put it
    there detects the bug about two times in three. Here the band is chosen, so the assertion is
    about the rule rather than about the draw.
    """
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
    """The same rule as the test above, through the route the person actually taps — the pair
    that comes back keeps the half they did not correct.

    This one takes whatever band the draw deals, so it says nothing about small bands; the
    deterministic test above is what pins that. Kept because the wiring between the route, the
    correction and the redraw is not exercised anywhere else.

    ITS TWO LOAD-BEARING ASSERTIONS USED TO BE UNDER `if after["type"] == "battle":`, and the
    regression its sibling's docstring names — "the survivor was abandoned and the slot fell
    through to a sweep card" — is exactly what makes that condition false. Verified by forcing the
    fallback: the test stayed green and asserted nothing. They are unconditional now, which the
    fixture supports by construction (the smallest band here is four titles, so the survivor
    always has an opponent), and the loop above is bounded rather than infinite: a surface that
    stopped dealing battles hung the suite instead of failing it. [M4.10 finding 31, plan step 8]
    """
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


# --- M4.10: one card, one transaction, and the socket outside it -------------------------------
#
# Every guard on this surface used to be a check-then-act on an autocommit connection, so a
# double tap, a second tab, or a phone and a laptop on one account all reached the write
# together. The four taps are separate tests rather than one loop because the fixtures differ
# and a failure has to name the tap.


async def _second_connection(pg_url: str) -> asyncpg.Connection:
    """A second connection, because two coroutines on one connection are not concurrent.

    The jsonb codecs are the ones `conftest.py`'s `db` fixture installs: `rate_observation.card`
    and `rate_session.current_card` are jsonb, and without them asyncpg hands back a string and
    the card comes out of the journal as text.
    """
    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return conn


@pytest.fixture
async def two_devices(app):
    """One account reached from two devices over one cookie jar — §6.2's household.

    Both pool connections are opened before any write starts, and concurrently. Warming them one
    after the other is itself a serialisation: `test_account_security.py`'s first-boot race used
    to pass with its advisory lock removed because the loser's connection cost was what ordered
    the two requests, and that lesson is this milestone's too.
    """
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
    """Two gathered requests, as sorted statuses and the responses themselves.

    `return_exceptions=True` with a loud failure rather than `raise_app_exceptions=False` on the
    transport: an exception that escapes every handler in `app.py` is a 500 to the person, and a
    message naming it is worth more here than a bare `500` in a list of status codes.
    """
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
    """§6.1: the card is the server's, and a token that no longer matches is a 409 with a reason.

    `_take_card` validated the token against the snapshot `_resume` had read on an autocommit
    connection, so both taps passed it and the loser got as far as `INSERT INTO rate_observation`
    before `rate_observation_seq` refused it. What the person then saw was `app.py`'s generic
    conflict — a string naming a database constraint — over a card that had in fact been
    recorded, which `rate.svelte.js` treats as a hard error and does not re-read from.

    Eight attempts per tap, and the counts are checked after each one: a race that writes two
    rows in one attempt of eight is exactly as broken as one that writes two every time.
    """
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

        # One journal row and one advanced slot: decision 35's counter is defined over a
        # sequence, and two rows at one slot would make Undo's depth unanswerable.
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
    """The same race on the duel arm, with the two taps answering *differently*.

    A and B for one pair is what the unserialised shape produced on the Rank queue, and it is
    the worst case here too: one of the two is a judgement the person never made, in a table
    §4.2 makes append-only.
    """
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
    """The same race with §7.3's push made slow, which is how it is reached in a house.

    With the push inside the transaction — where it was — a Jellyfin taking 300 ms turned a
    millisecond-wide window into a third of a second, which is comfortably longer than the gap
    between two impatient taps. The push is outside the transaction now, so the winner commits
    and the loser wakes before the socket is even opened; the assertion is that the outcome is
    the same shape whatever the network does.
    """
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
    """`api/rate.py`'s own promise: "a second GET returns the same card under the same token".

    It was false whenever the table was empty. `ensure_card` decided to draw from a snapshot and
    `stash_card` then wrote unconditionally, so two GETs both drew and both stashed — the same
    title under two tokens, last write wins. Reproduced as two tokens back, the first verdict
    200 and the second 409, on nothing more exotic than a phone that reloaded while a laptop was
    open. [M4.10 finding 4]
    """
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
    """§6.0's banner CTA, and the branch finding 4's repair did not reach.

    `_stash_if_empty` closed the empty-table draw; the head redraw went on writing with no
    predicate at all. Two devices following the same pending-verdicts CTA therefore both redrew and
    both stashed — two tokens for one session, last write wins — and the losing device's first tap
    was refused with "that card has already been answered" over a card nobody had answered.

    Asserted as the state the race produces rather than as the race: over the route two gathered
    redraws split once in 40 attempts, and a race that passes once has not passed. The second
    device's `ensure_card` is driven from the snapshot it read before the first device's redraw
    landed, which is exactly what the losing request holds.

    Three arms, because the compare-and-set has three outcomes and only two of them were covered:
    redraw-against-redraw here, and at the end the one the helper's own enumeration denied — a tap
    wins, refills the table from its own queue position, and the losing banner redraw is served a
    title the banner did not name. [M4.10 finding 4, cycle 1 m410-rev-02, cycle 2 M410-C2-D2-01]
    """
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

    # The third outcome of a missed compare-and-set, which the helper's enumeration denied: a tap
    # does not leave the table empty, because every `record_*` ends in `ensure_card`. So a tap from
    # a device that is not carrying the banner's `head` refills the table with a title the banner
    # did not name, the losing redraw misses `card_token IS NULL` as well, and the person who
    # followed a CTA naming three films is shown a fourth. Asserted as the property that decides
    # the behaviour: the card served is the card stored, so it is answerable — which is why serving
    # it beats clobbering a card another device is holding. [M4.10 cycle 2, M410-C2-D2-01]
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
    """Decision 35's journal, and the one tap that wrote it outside a transaction.

    `_append` is an INSERT at `seq + 1` followed by the UPDATE that moves the session to it.
    `record_skip` issued both on the autocommit connection, so a process death, a dropped pool
    connection or a cancelled task between them left the journal at N+1 with the session at N —
    and `rate_observation_seq` then refused every later append in that session for ever.
    Reproduced from that state: verdict, skip and not-seen all 500, `GET /api/rate` went on
    serving a card that could not be answered, undo tombstoned the phantom row and the next
    verdict still 500'd, and only `DELETE /api/rate/session` recovered. Skip is the most
    frequent tap in a sweep. [M4.10 finding 3]

    The failure is injected at the cursor move rather than by killing a process, because what
    has to hold is that the two statements are one unit — and that is the same property either
    way.
    """
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
    """`linked`, with the labels dropped so the sweep queue is full.

    `linked` rates every owned title so that a battle pair always exists. The §7.3 push this
    milestone moved out of the transaction rides on the sweep taps — verdict, not-seen and the
    correction — so those need a queue with something in it.
    """
    await db.execute("DELETE FROM verdict")
    await db.execute("DELETE FROM user_title")
    return linked


async def test_the_loser_of_a_double_tap_never_tells_jellyfin(
    db, pg_url, linked_sweep, monkeypatch
):
    """The half of finding 2 that M4.7's conflict handler did not close.

    `_push_state` ran between the observation and the journal row, inside the transaction. The
    loser's observation rolled back — so no duplicate label — but its Played write had already
    gone out over the wire, where nothing rolls back: Jellyfin was told the household had seen a
    title this app has no record of anyone rating. With the card claimed under a row lock the
    loser never reaches the observation, let alone the socket.

    Driven at the domain layer on two real connections, because the thing being counted is the
    write log of a media server and the thing being raced is one function's transaction. The
    app-side state is reset between attempts and the fake's write log is not: the fake carries
    six items and refuses a Played write for any other id, so eight attempts have to reuse them
    while the thing being counted goes on accumulating.

    `observations.record_verdict` is counted as well as the write log, and that count is the one
    assertion in the suite that can tell the lock apart from its backstop. Both refusals leave as
    `StaleCard("stale_card")` — `_append` maps `rate_observation_seq` to the same exception on
    purpose — so with `_claim_card` removed every other assertion here still holds: the loser's
    INSERT collides, rolls back, and since finding 11 moved the socket out it never reaches
    Jellyfin either. What changes is that the loser gets as far as writing the ledger row and
    `user_title`, which is the work the lock exists to stop and which shows up nowhere else.
    [M4.10 cycle 1, m410-rev-01]
    """
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
    """§3.3, and the sentence `sync/seen.py` and `ledger/observations._set_state` both already
    carried: "the app-side write is committed before the network call and never depends on it".

    It was false for the most frequent caller of both. Measured with a transport sleeping 1.5 s:
    the tap took 1.66 s, a second connection could not see the verdict row until 1.67 s, one
    backend sat `idle in transaction` for the whole wait against a pool of ten, and a second
    connection's UPDATE of the same row blocked 0.97 s behind a 1.0 s call. The budget §6's
    preamble gives a sweep card is 2 s *in total*.

    Half a second here rather than one and a half: enough that the 10 ms poll below can tell the
    two shapes apart by an order of magnitude, short enough not to spend it on every suite run.
    [M4.10 finding 11]
    """
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
            # Whose latency is being held, in the only terms Postgres reports it in. The watcher
            # excludes itself; the only other backend in this database is the one running the tap.
            #
            # A DURATION and not a state, sampled on every poll rather than only while the row is
            # invisible. `idle in transaction` on its own is what the correct shape ALSO reports:
            # the tap's own observation transaction is three round trips with the backend idle
            # between them, and `refit.update_incrementally` opens another one after the push — so
            # the plain predicate was a statement about how coarsely it was sampled, and with the
            # row usually visible on the first poll it was not sampled at all. A 0.3 s floor is
            # above both of those and far below the 0.5 s the transport sleeps, so it reads 1 on
            # the pre-M4.10 shape and 0 on this one at any poll resolution. `clock_timestamp()`
            # rather than `now()` because `now()` is the transaction's own start.
            # [M4.10 cycle 1, f11-04]
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


async def test_the_journal_records_the_push_that_happened_and_not_the_one_intended(
    db, linked_sweep
):
    """Decision 207. The journal row is written inside the transaction and the push happens
    after it, so `prior_state.pushed` is false at the moment it is stored and is corrected by
    one follow-up UPDATE once the push has resolved.

    The field is not bookkeeping: `undo` reads exactly it to decide whether to hand Jellyfin
    back the Played flag, which is what `_state_entries`' docstring means by "Undo compensates
    what it did, not what it intended". Left permanently false, every retraction on a linked
    household would be silently skipped and §7.3's next sweep would read our own write back as
    the household's history.
    """
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
    """Decision 207's third arm, which nothing reached.

    `_mark_pushed` is called from `record_verdict`, `record_not_seen` and `record_correction`. The
    verdict's copy is asserted above; the correction's is pinned indirectly, because
    `test_undo_pushes_back_exactly_what_the_forward_action_pushed` requires a retraction that `undo`
    gates on `prior_state.pushed`. Not-seen's was pinned by nothing — deleting its call left the
    suite green — and it is the arm where the lost flag costs most: `observations.undo` restores
    `user_title` byte for byte, §7.3's `jf_synced_at` stamp included, so a skipped retraction leaves
    Jellyfin's Played flag false with nothing owed, and §7.3's next sweep reads our own write back
    as the household's history. [M4.10 decision 207, cycle 1 f11-02]

    A title already `seen` and not yet verdicted is what the sweep queue serves first (`NOT s.seen`
    is the second key of its ORDER BY), so dropping one verdict from `linked` puts one on the table
    — and `seen` is the prior state the retraction has to put back.
    """
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
    """Decision 207's window, from the side the decision's own cost paragraph waved through.

    Step 4 made the journal row visible and undoable while the push is still on the wire, and the
    follow-up UPDATE corrects `pushed` only afterwards. In between the row reads `pushed = false`
    and `undo` gates the compensating write on exactly that field, so an Undo in that window
    skipped `seen.retract` and the Played flag the tap had just set stayed true on the media
    server with nothing owed anywhere — `observations.undo` deletes the `user_title` row a first
    verdict created, so there is no `jf_synced_at` left for §7.3 to reconcile from and the next
    sweep adopts our own write back as the household's history. That is the harm `_mark_pushed`'s
    docstring says the flag exists to prevent, and it did not exist before this milestone: at HEAD
    the push and the journal row were inside one transaction, so the row was never visible
    carrying a provisional `pushed`.

    Raced rather than argued, on a second connection because two coroutines on one connection are
    not concurrent, with the §3.3 slow server the 15 s client budget is kept for. `undo`'s
    `FOR UPDATE` on the journal row is what makes the repair total in both interleavings: an undo
    that commits first is seen as a tombstone, and one that commits later blocks on the row lock
    and then reads `pushed = true`. [M4.10 cycle 2, M410-C2-F11-01]
    """
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
    # The card is dealt, so this is a prior the retraction is allowed to put back rather than a
    # change to what the queue serves. `linked_sweep` empties `user_title`, and M4.11's decision 210
    # makes `seen.retract` refuse a prior of "no row at all": sending Played = false there would
    # push the app's absence over Jellyfin's history, which is the one row `sync/seen.py`'s own
    # header says never happens. The window this test is about — the journal row visible while the
    # push is still on the wire — is unchanged by that, and an explicit prior `unseen` is what a
    # re-rating of a title the household had marked not-seen actually looks like.
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
        # The window opens at the commit and closes at `_mark_pushed`, so the undo is timed off
        # the journal row becoming visible rather than off a sleep.
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
    """§7.3: "a 401 on write -> re-link prompt" — on the retraction too.

    `rate/session.py` carried its own copy of the push path for Undo, and the copy had drifted:
    it returned silently where the person's link had no usable token and where Jellyfin rejected
    it, so a token that expired between the tap and the Undo left `jellyfin_link_state` reading
    `linked` with nothing anywhere asking the person to fix it — and the retraction quietly lost.
    Going through `sync/seen.retract`, which goes through `_push`, is the repair. [finding 11]
    """
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


# --- the two routes this milestone owned and nothing named ------------------------------------


async def test_the_not_seen_route_writes_unseen_advances_and_preloads_the_next_card(
    db, rate_client
):
    """§6.1's one seen-state control over HTTP, and the owner decision of 2026-08-29: a title
    you cannot remember is plain `unseen`, with no third state.

    The route was reachable only through other milestones' fixtures — `test_restore_drill.py`
    and `test_secrets_custody.py` both tap it on the way to something else — so nothing asserted
    what it writes. M4.10's route inventory counts it as named from here. [M4.10 finding 34]
    """
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
    """Exit criterion clause 4's Rate half: after the observation commits, no route raises.

    `payload` records §6.7's verdict line after `record_verdict`'s transaction has closed, and
    `rail.verdict_line` interpolated the member's name and the title's name without eliding either
    — so the line could exceed `MAX_LINE`, `rail.record` refused it, and `app.py` rendered a 500
    over a durable `verdict` and `rate_observation`. The retry is worse than the 500: `_claim_card`
    has already nulled the card token, so the person is told the card was already answered and has
    no way to try again. `rank/tiers.py`'s `MAX_LABEL` comment records the same failure for tier
    labels, and the Rank half of this rule is `test_rank_routes.py`'s.

    Both names are data rather than choices: `title.name` is bundle free text with no length rule
    anywhere in `importer/validate.py`, and `AccountName` (`api/setup.py`) allows 64 characters, so
    a household whose member is "Grandma's iPad in the living room" needs no unusual title to reach
    it. [M4.10 cycle 1, M410-R1-01]
    """
    client, user_id = rate_client
    member = "Grandma's iPad in the living room and the one in the kitchen :-)"
    assert len(member) <= 64, "longer than AccountName allows, so the test cheats"
    await db.execute("UPDATE app_user SET name = $1 WHERE id = $2", member, user_id)
    long_name = ("The Assassination of Jesse James by the Coward Robert Ford " * 6)[:310]
    await make_titles(db, [(1, "movie", long_name)])
    # The arithmetic that makes this bite rather than pass by luck: the chrome around the two names
    # is 42 characters with no refit clause and 71 with a six-figure millisecond count, so these
    # two names are over `MAX_LINE` whichever the incremental refit reports.
    assert len(member) + len(long_name) + 42 > rail.MAX_LINE

    card = (await client.get("/api/rate")).json()["card"]
    # §6.7's buffer is per process and keyed by user id, so it carries whatever an earlier test in
    # this file recorded for the same id. Emptied here rather than read with a filter, because the
    # assertion below is that this tap recorded exactly one line.
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


async def test_the_balance_route_serves_the_widgets_own_poll(db, rate_client):
    """§5.2's running class balance on its own, for the widget that polls it.

    Named by nothing at any layer before this: `test_account_security.py` probes the path for a
    401 and no test had ever read its body. It must be exactly the `class_balance` block of the
    envelope — two copies of one number that can disagree is the finding `_verdict_rail_line`
    was written against. [M4.10 finding 34]
    """
    client, user_id = rate_client
    await make_titles(db, [(i, "movie", f"Title {i}") for i in range(1, 9)])
    for title_id in (1, 2, 3, 4, 5):
        await label(db, user_id, title_id, 2)
    await label(db, user_id, 6, 0)

    answered = await client.get("/api/rate/balance")
    assert answered.status_code == 200
    balance = answered.json()
    assert balance["counts"] == [1, 0, 5]
    assert balance["warn"] is True, "5 of 6 liked is past §5.2's 60% line"
    assert balance == (await client.get("/api/rate")).json()["class_balance"], (
        "the widget's poll and the envelope must not be able to disagree"
    )
