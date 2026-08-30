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

import json
from typing import Any

import asyncpg
import pytest

from spielplan.api import rate as rate_api
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import JellyfinConfig
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rate import session

HP = DEFAULTS

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


def test_the_card_type_is_a_pure_function_of_the_slot_and_the_block_rolls_at_fifteen():
    """§6.1: "Mix (default — alternates sweep and battle); blocks of 15."

    Slot 1 is a sweep, so Mix opens on the one card a person with no ratings can answer.
    """
    assert [session.card_type_for("mix", n) for n in range(1, 7)] == [
        "sweep", "battle", "sweep", "battle", "sweep", "battle"
    ]
    assert session.card_type_for("sweep", 2) == "sweep"
    assert session.card_type_for("battle", 1) == "battle"
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
    """§6.1: "the counter runs 1..15 and rolls into a new block." The roll is also decision
    35's commit: everything in the old block stops being undoable at that instant."""
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


async def seed_ledger(db, user_id: int, title_ids) -> None:
    """A fitted ranking for every title, all carrying the same two marker numbers, so a leak
    into any card shows up as a literal string."""
    for title_id in title_ids:
        await db.execute(
            "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma, cdf, tier) "
            "VALUES ($1, $2, 'movie', $3, 0.25, $4, 5)",
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
    construction."""
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21))
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
    back is the card that produced the observation — not the neighbouring queue position."""
    user = rated["user"]
    await seed_ledger(db, user, range(1, 21))
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

    Two boundaries, and both are refusals with a reason: the start of the current block once
    the counter has rolled, and an empty journal.
    """
    user = world["user"]
    s = await open_session(db, user, mode="sweep")
    with pytest.raises(session.UndoUnavailable) as empty:
        await session.undo(db, s, hp=HP)
    assert empty.value.reason == "empty"

    for _ in range(15):
        s = (await session.record_skip(db, s, card_token=token(s))).session
    assert (s.block_index, s.slot) == (1, 1)

    assert await session.undo_availability(db, s) == {
        "available": False, "kind": None, "reason": "block_boundary"
    }
    with pytest.raises(session.UndoUnavailable) as refused:
        await session.undo(db, s, hp=HP)
    assert refused.value.reason == "block_boundary"
    assert await db.fetchval(
        "SELECT count(*) FROM rate_observation WHERE session_id = $1 AND undone_at IS NOT NULL",
        s.id,
    ) == 0, "a refused undo compensates nothing"


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
    """The real app plus this router.

    `app.py` is not this lens's file to edit; registering the router here means the route
    contract is tested now and the test keeps passing unchanged the moment `create_app` picks
    it up.
    """
    client = app()
    application = client._transport.app
    if not any(getattr(r, "path", "").startswith("/api/rate") for r in application.routes):
        application.include_router(rate_api.router)
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
    application = client._transport.app
    if not any(getattr(r, "path", "").startswith("/api/rate") for r in application.routes):
        application.include_router(rate_api.router)
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


async def test_a_correction_keeps_the_surviving_half_even_in_a_small_verdict_band(db, rate_client):
    """§6.1: "`not seen: [left] [both] [right]` sets exactly the named side(s) to unseen" — the
    other half keeps its place, against a fresh opponent from its own band.

    The failing shape is a skewed labeller, which is the common one and the one §5.2's
    class-balance warning is aimed at. The opponent used to be found by drawing whole pairs and
    rejecting any outside the survivor's class, eight times; `battle.draw` weights strata by
    pair count, so a minority band was missed on every attempt about half the time and the
    battle silently turned into a sweep card with nothing on screen saying why.

    12 liked, 4 fine, 4 disliked, correcting inside a minority band — repeated, because the old
    code failed this probabilistically rather than always.
    """
    client, user_id = rate_client
    titles = [(i, "movie", f"Title {i}") for i in range(1, 25)]
    await make_titles(db, titles)
    for title_id in range(1, 13):
        await label(db, user_id, title_id, 2)
    for title_id in range(13, 17):
        await label(db, user_id, title_id, 1)
    for title_id in range(17, 21):
        await label(db, user_id, title_id, 0)

    kept_as_battle = 0
    for _ in range(12):
        await client.post("/api/rate/session", json={"mode": "battle", "restart": True})
        card = (await client.get("/api/rate")).json()["card"]
        assert card["type"] == "battle", "a battle-mode session must serve a battle"
        left = card["left"]["id"]
        right = card["right"]["id"]

        body = (
            await client.post(
                "/api/rate/correction", json={"card_token": card["token"], "side": "left"}
            )
        ).json()
        after = body["card"]
        if after and after["type"] == "battle":
            kept_as_battle += 1
            titles_now = {after["left"]["id"], after["right"]["id"]}
            assert right in titles_now, "the half the person did NOT correct must keep its place"
            assert left not in titles_now, "the corrected half must be gone"

    assert kept_as_battle == 12, (
        f"the pair survived only {kept_as_battle}/12 corrections — the survivor is being "
        "abandoned when its verdict band is small"
    )
