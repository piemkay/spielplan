"""The Rate surface's state machine. Spec v2.1 §6.1, §6.7, §6.8, §7.3, §13, decision 35.

The session lives on the server, and three of §6.1's rules are enforceable only because it
does:

  * **The block counter is the server's.** §6.1: "Mix (default — alternates sweep and battle);
    blocks of 15." Decision 35 measures Undo's depth in that counter — "back to the start of
    the current block of 15 and no further" — so a counter the client owns is a counter the
    client can lie about, and Undo's boundary becomes unenforceable.
  * **The card in front of the person is the server's.** A write names a `card_token`, never a
    title id, so a client cannot answer a card it was never served, a battle pair does not
    reshuffle under the person's thumb between the draw and the tap, and §13's silent re-ask
    marker has a home no serialiser can reach.
  * **The card type is a function of the counter, never of the last card served.** §6.1's Mix
    "alternates sweep and battle", and the counter is what alternates — the monotone
    observation index across blocks (`observation_index`), not the slot inside one, because
    fifteen is odd and the slot made every roll serve two sweeps in a row (decision 200).
    Deriving the next type from what was last *answered* is the bug this module exists not to
    have: a run of duels then never returns a sweep card.

What this module is not: it draws no cards itself. `rate.queue`, `rate.battle` and
`rate.balance` own what to ask; this owns when, in what order, under which token, and how to
take it back.
"""

from __future__ import annotations

import contextlib
import logging
import math
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import numpy as np

from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import SECRETS_UNREADABLE_REASON, JellyfinConfig
from spielplan.db.library import normalise_kinds
from spielplan.home import rail
from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import VERDICT_LABELS, EmbeddingSource, PriorState
from spielplan.rate import LIVE_LABEL, balance, battle, queue
from spielplan.sync import seen

log = logging.getLogger("spielplan.rate.session")

# §6.1: "blocks of 15". Not a tuned constant — it is the spec's own number and the unit
# decision 35 measures Undo's depth in, so it is fixed here rather than in
# `ledger_hyperparams.json`, which §4.3 reserves for what the corpus project re-tunes offline.
BLOCK_SIZE = 15

MODES: tuple[str, ...] = ("mix", "sweep", "battle")
CardType = Literal["sweep", "battle"]
Side = Literal["left", "both", "right"]

# §4.2: outcome A | B | TIE. "About the same" is first-class data (22% of random pairs are
# genuine ties), so TIE is an outcome here and never a skip.
OUTCOMES: tuple[str, ...] = ("A", "B", "TIE")

# §6.1's battle context. A profile battle is drawn at random by design (§0 row 6's measured
# null), so it is neither boundary-targeted nor part of §13 stream (a)'s held-out sample.
BATTLE_CONTEXT = "profile_battle"
BATTLE_SELECTION = "random"


# §6.1's empty state, keyed by the CAUSE of the empty state rather than by nothing at all.
#
# There is one way to have no card and three reasons for it, and `payload` could not tell them
# apart: the sweep queue is spent (Sweep), no verdict class yet holds two titles so no pair can
# be drawn (Battle), or both (Mix). One sentence served all three, so a person in their first
# week who tapped Battle — the case where the pool is *empty* rather than exhausted, because the
# pool is the conjunction "seen AND verdicted within one class" — was told "You've rated
# everything we can queue right now", which is the opposite of true and sends them to Rank
# instead of back to the sweep that would fix it. §6.8 makes every line the app says about its
# own state a matter of honesty, and `cause` travels beside the copy so a client can pick its
# own heading instead of inferring one. No new session state: the cause is the mode, because the
# mode is what decided which draws were attempted (see `ensure_card`). [M4.10 finding 20]
DRAINED_CAUSES: dict[str, dict[str, str]] = {
    "queue": {
        "cause": "queue",
        "text": (
            "You've rated everything we can queue right now. Battles sharpen what you've "
            "already said."
        ),
    },
    "pool": {
        "cause": "pool",
        "text": (
            "A battle compares two titles you have already rated the same way, and there are "
            "not two of them yet. Rate a few in Sweep and the pairs start arriving."
        ),
    },
    "both": {
        "cause": "both",
        "text": (
            "You've rated everything we can queue right now, and no two of your ratings sit in "
            "the same band, so there is no pair left to compare either."
        ),
    },
}


def drained_for(mode: str) -> dict[str, str]:
    """Which empty state this is, from the mode that decided which pools were tried.

    Mix tries both and lands here only when both came back empty; Sweep and Battle each tried
    one. Defaulting to `both` rather than raising: a mode outside `MODES` cannot reach here
    (`set_controls` validates), and an empty state is not the place to turn a surprise into a
    500. [M4.10 finding 20]
    """
    if mode == "sweep":
        return DRAINED_CAUSES["queue"]
    if mode == "battle":
        return DRAINED_CAUSES["pool"]
    return DRAINED_CAUSES["both"]


class StaleCard(Exception):
    """The answer names a card that is not the one on the table.

    A double tap, a back button, a second device — all three arrive as a token that no longer
    matches `rate_session.card_token`, and all three must be refused rather than applied to
    whatever card happens to be current now.
    """

    def __init__(self, reason: str = "stale_card") -> None:
        super().__init__(reason)
        self.reason = reason


class UndoUnavailable(Exception):
    """Decision 35: "back to the start of the current block of 15 and no further".

    Raised rather than silently no-opped, because the chip has to disable *visibly* at the
    boundary and a tap that quietly does nothing is the failure this replaces.
    """

    def __init__(self, reason: Literal["empty", "block_boundary"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Jellyfin:
    """§7.3's write path, injected. `client is None` is a legal, reported state (§3.3: the app
    must work when Jellyfin is down), and then the app-side write simply stays owed."""

    client: JellyfinClient | None
    cfg: JellyfinConfig


@dataclass
class RateSession:
    id: int
    user_id: int
    kinds: list[str]
    mode: str
    decisive: bool
    block_index: int
    slot: int
    seq: int
    current_card: dict[str, Any] | None
    card_token: uuid.UUID | None


class RailLine(str):
    """A log line that remembers which title it narrates.

    §6.7's event carries a `title_id` field and every producer left it null, so the line the
    client rendered named a film it could not link. The id has to travel WITH the line rather
    than be looked up beside it, because §6.7's sentence is composed at the write — see
    `rail.record`'s contract, "the rail shows what the model believed when it acted" — while
    `rail.record` itself is called one call later, in `payload`, by which time `s.current_card`
    is the NEXT card and the answered one is gone.

    A `str` subclass rather than a pair or a second tuple beside `log`: `Outcome.log` is also
    §6.1's per-response echo, so every line in it is serialised into the payload and read with
    `in` by tests and by `RateModelLog`. Subclassing leaves all of that working untouched and
    makes the id readable by exactly the one caller that wants it.
    """

    def __new__(cls, text: str, *, title_id: int | None = None) -> RailLine:
        line = super().__new__(cls, text)
        line.title_id = title_id
        return line


@dataclass(frozen=True)
class Outcome:
    """One tap's result: the session as it now stands, and everything the response carries."""

    session: RateSession
    reveal: dict[str, Any] | None = None
    # Plain strings except where §6.7's line is about one title, which `RailLine` carries.
    log: tuple[str, ...] = ()
    ledger: dict[str, Any] | None = None
    undone: str | None = None


# --- the block machine -------------------------------------------------------------------


def observation_index(block_index: int, slot: int) -> int:
    """How many observations this session has taken, from the two numbers it stores.

    The monotone counter decision 200 derives the card type from. `rate_session.seq` is the
    journal's row count and is deliberately NOT this number: a correction appends a row without
    advancing (§6.1 makes it a repair of the question, not an answer to it), and Undo moves the
    block and slot back while leaving `seq` where it was — so a type derived from `seq` would
    contradict the card Undo had just restored and would re-create the double sweep below one
    undo later. The index over (block, slot) is the same number in the ordinary case, moves only
    when the counter the person is reading moves, and is defined by `advance`'s own arithmetic.
    [decision 200]
    """
    return block_index * BLOCK_SIZE + slot - 1


def card_type_for(mode: str, index: int) -> CardType:
    """§6.1: "Mix (default — alternates sweep and battle); blocks of 15."

    A pure function of the MONOTONE observation index (`observation_index` above), not of the
    slot. Index 0 — slot 1 of the first block — is a sweep, so a new labeller opens on the one
    card they can answer with no ratings of their own. Sweep and Battle modes serve only their
    own type; the counter still advances, so switching modes mid-block does not restart it.

    It was the slot, and fifteen is odd: slot 15 was a sweep and `advance` rolls slot 15 to slot
    1, which is a sweep too. So every block ended and the next began with a sweep — eight sweeps
    to seven battles and one consecutive-same pair per block, which over §6.1's 50-100-verdict
    target is three to seven fewer duels than "alternates" promises, in the arm §5.2 credits with
    within-liked resolution. §6.1 is not amended: the spec says alternates, and it now does.
    [M4.10 finding 24, decision 200]
    """
    if mode == "sweep":
        return "sweep"
    if mode == "battle":
        return "battle"
    return "sweep" if index % 2 == 0 else "battle"


def advance(block_index: int, slot: int) -> tuple[int, int]:
    """§6.1: "the counter runs 1..15 and rolls into a new block."

    Rolling is decision 35's boundary, and decision 199 is precise about where that boundary
    falls: a block is committed when the FIRST observation of the NEXT block lands, not when the
    fifteenth of this one does. So the roll moves the counter and nothing else — the fifteenth
    tap stays undoable while the new block is still empty, which is what decision 174 ruled and
    what `_undo_reaches` implements. Everything in the old block stops being undoable one tap
    later. Stated as arithmetic rather than as a separate rule, so the two cannot disagree.
    [decisions 35, 174, 199]
    """
    if slot >= BLOCK_SIZE:
        return block_index + 1, 1
    return block_index, slot + 1


_SESSION_COLUMNS = (
    "id, user_id, kinds, mode, decisive, block_index, slot, seq, current_card, card_token"
)


def _session(row: asyncpg.Record) -> RateSession:
    card = row["current_card"]
    return RateSession(
        id=row["id"],
        user_id=row["user_id"],
        kinds=list(row["kinds"]),
        mode=row["mode"],
        decisive=row["decisive"],
        block_index=row["block_index"],
        slot=row["slot"],
        seq=row["seq"],
        current_card=dict(card) if card else None,
        card_token=row["card_token"],
    )


async def open_or_resume(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kinds: Sequence[str] | None = None,
    restart: bool = False,
) -> RateSession:
    """One live session per person, which is what `rate_session_one_live` says in DDL.

    Resuming rather than restarting is the point: a person who closes the app at slot 7 comes
    back to slot 7, with the same seven observations still undoable. `restart=True` ends the
    live session first, which is the only way to get a fresh block counter.
    """
    wanted = normalise_kinds(kinds) if kinds else list(observations.KINDS)
    async with conn.transaction():
        if restart:
            await conn.execute(
                "UPDATE rate_session SET ended_at = now() "
                "WHERE user_id = $1 AND ended_at IS NULL",
                user_id,
            )
        # The conflict target names the partial index's own predicate, so the DDL's uniqueness
        # rule is honoured by the insert rather than fought with an exception handler.
        row = await conn.fetchrow(
            f"""
            INSERT INTO rate_session (user_id, kinds, mode)
            VALUES ($1, $2::text[], 'mix')
            ON CONFLICT (user_id) WHERE ended_at IS NULL DO NOTHING
            RETURNING {_SESSION_COLUMNS}
            """,
            user_id,
            wanted,
        )
        if row is None:
            row = await conn.fetchrow(
                f"UPDATE rate_session SET last_seen_at = now() "
                f"WHERE user_id = $1 AND ended_at IS NULL RETURNING {_SESSION_COLUMNS}",
                user_id,
            )
    if row is None:  # pragma: no cover — the insert and the update cannot both miss.
        raise RuntimeError(f"no live rate session for user {user_id} after open_or_resume")
    return _session(row)


async def end_session(conn: asyncpg.Connection, *, user_id: int) -> bool:
    ended = await conn.fetchval(
        "UPDATE rate_session SET ended_at = now() "
        "WHERE user_id = $1 AND ended_at IS NULL RETURNING id",
        user_id,
    )
    return ended is not None


async def set_controls(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    mode: str | None = None,
    kinds: Sequence[str] | None = None,
    decisive: bool | None = None,
) -> RateSession:
    """§6.1's three controls: the mode, the kind toggles, and the persistent decisive toggle.

    Changing the mode or the kinds drops the card on the table — a battle pair is meaningless
    once Sweep is selected, and a film pair is meaningless once Films is switched off — so the
    next `ensure_card` draws fresh. The decisive toggle does not: it changes the *weight* of
    the next answer, not the question.
    """
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    wanted = normalise_kinds(kinds) if kinds is not None else s.kinds
    redraw = (mode is not None and mode != s.mode) or wanted != s.kinds
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET mode = $2, kinds = $3::text[], decisive = $4, last_seen_at = now(),
               current_card = CASE WHEN $5 THEN NULL ELSE current_card END,
               card_token   = CASE WHEN $5 THEN NULL ELSE card_token END
         WHERE id = $1
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        mode or s.mode,
        wanted,
        s.decisive if decisive is None else decisive,
        redraw,
    )
    return _session(row)


# --- the card cursor ---------------------------------------------------------------------


async def _observed_title_ids(
    conn: asyncpg.Connection, session_id: int, *, kinds_of: Sequence[str] | None = None
) -> list[int]:
    """What this sitting has already put in front of the person.

    `undone_at IS NULL` is what makes Undo lift the suppression along with the observation.

    The two card types want *different* answers out of this, which is why it takes a filter:

      * The **sweep queue** excludes everything. A rated title does not come back, a skipped
        one does not come back, and a title cannot be asked about twice in one sitting.
      * The **battle pool** excludes only what was skipped. Mix exists to ask two different
        questions about the same titles — §6.1 draws pairs "from the user's seen titles within
        verdict bands", and those bands are built out of exactly the verdicts the sweep half
        just collected. Excluding them would leave a person who arrived with no ratings unable
        to reach a battle at all, which is the alternation the surface is named for. A skip is
        the one signal that means "not this, not now", and it holds for the sitting.
    """
    rows = await conn.fetch(
        "SELECT DISTINCT unnest(title_ids) AS title_id FROM rate_observation "
        "WHERE session_id = $1 AND undone_at IS NULL "
        "  AND ($2::text[] IS NULL OR kind_of = ANY($2::text[]))",
        session_id,
        list(kinds_of) if kinds_of is not None else None,
    )
    return [r["title_id"] for r in rows]


async def _skipped_title_ids(conn: asyncpg.Connection, session_id: int) -> list[int]:
    return await _observed_title_ids(conn, session_id, kinds_of=("skip",))


async def _draw_sweep(
    conn: asyncpg.Connection, s: RateSession, *, exclude: Sequence[int], head: Sequence[int]
) -> dict[str, Any] | None:
    cards = await queue.next_sweep_cards(
        conn, user_id=s.user_id, kinds=s.kinds, limit=1, exclude=tuple(exclude), head=tuple(head)
    )
    if not cards:
        return None
    card = cards[0]
    kind = await observations.kind_of(conn, card.title_id)
    return {
        "type": "sweep",
        "kind": kind,
        "title_id": card.title_id,
        "reason": card.reason,
        "p_seen": card.p_seen,
        "source": card.source,
        # §13 stream (b): server-side only. `public_card` has no field for it by construction.
        "reask_of": card.reask_of,
    }


async def _draw_battle(
    conn: asyncpg.Connection, s: RateSession, *, exclude: Sequence[int], rng: Any = None
) -> dict[str, Any] | None:
    pair = await battle.next_battle_pair(
        conn, user_id=s.user_id, kinds=s.kinds, exclude=tuple(exclude), rng=rng
    )
    if pair is None:
        return None
    return _battle_card(await observations.kind_of(conn, pair.title_a), pair)


def _battle_card(kind: str, pair: battle.BattlePair) -> dict[str, Any]:
    return {
        "type": "battle",
        "kind": kind,
        "title_a": pair.title_a,
        "title_b": pair.title_b,
        # §6.1 draws pairs "within verdict bands"; the band is the pair's, and it stays
        # server-side — see `public_card`.
        "verdict_class": pair.verdict_class,
        "reason": pair.reason,
        "reask_of": pair.reask_of,
    }


async def ensure_card(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    rng: Any = None,
    head: Sequence[int] = (),
) -> RateSession:
    """Idempotent: draws only when the table is empty.

    A GET that redrew would make the card a moving target and every §6 preamble promise about
    preloading a lie. The substitution rule is the one wrinkle: when the slot calls for a
    battle and the person has not yet rated two titles in any one class, a sweep is served in
    its place and **the slot is not changed** — so alternation resumes by itself the moment a
    pool exists, rather than the surface silently becoming Sweep-only.

    `head` is the exception to the idempotency, and it has to be. §6.0's pending-verdicts banner
    names up to three titles and its CTA "opens the §6.1 queue with those titles at the head of
    the queue, **not** at whatever position the standing queue held" — and a person who taps it
    almost always has a standing session with a card already stashed. Treated as a plain
    refresh, the banner names three films and then serves a battle about two others, which is
    the exact failure the requirement exists to prevent ("naming titles and then presenting a
    different card is worse than no prompt").

    So an explicit `head` that the stashed card does not satisfy redraws once. It stays
    idempotent, because after the redraw the card *is* one of the named titles; and when none of
    them can be drawn — all rated already, or none of this session's kinds — the stashed card is
    kept rather than the surface flickering on every GET.
    """
    served = await _observed_title_ids(conn, s.id)
    wanted = card_type_for(s.mode, observation_index(s.block_index, s.slot))
    if s.current_card is not None:
        if not head or s.current_card.get("title_id") in tuple(head):
            return s
        replacement = await _draw_sweep(conn, s, exclude=served, head=head)
        if replacement is None or replacement["title_id"] not in tuple(head):
            return s
        # The banner's redraw is a sweep whatever the counter called for, so it is a
        # substitution exactly as much as the thin-pool one below is, and it said so nowhere.
        # [M4.10 finding 21]
        return await _stash_if_unchanged(
            conn, s, _mark_substitution(replacement, instead_of=wanted)
        )

    skipped = await _skipped_title_ids(conn, s.id)
    card: dict[str, Any] | None = None
    if wanted == "battle":
        card = await _draw_battle(conn, s, exclude=skipped, rng=rng)
        if card is None and s.mode != "battle":
            card = _mark_substitution(
                await _draw_sweep(conn, s, exclude=served, head=head), instead_of=wanted
            )
    else:
        card = await _draw_sweep(conn, s, exclude=served, head=head)
        if card is None and s.mode != "sweep":
            # §6.1's drained state: the queue is spent but the ratings already given can still
            # be sharpened against each other.
            card = _mark_substitution(
                await _draw_battle(conn, s, exclude=skipped, rng=rng), instead_of=wanted
            )
    return await _stash_if_empty(conn, s, card)


def _mark_substitution(
    card: dict[str, Any] | None, *, instead_of: CardType
) -> dict[str, Any] | None:
    """Mark a card the counter did not call for, wherever the type flips.

    Three sites stash a card of the other type and only one of them marked it: the thin-pool
    substitution did, while `ensure_card`'s banner redraw and both of `_redraw_pair`'s fallbacks
    did not. `public_card` ships `substituted_for` and `RateSweepCard.svelte` is the sentence
    that explains why a battle slot is showing a sweep ("a battle was due in this slot"), so an
    unmarked substitution is a surface that changed the question and said nothing — while the
    payload's own `serving` went on naming the type the counter wanted. One rule, applied at
    every flip, so the three sites cannot drift again.

    The field carries the TYPE and no cause, and the client's sentence states no cause either:
    these three sites have three different ones (a thin pool, §6.0's pinned head, an emptied
    verdict band) and the marker cannot tell them apart. The sentence named the thin pool while
    that was the only marked site, and marking the other two made it false where the pool can be
    demonstrably full — §6.8's honesty, so the claim went rather than the marker.
    [M4.10 finding 21, cycle 1 M410-D8-07]
    """
    if card is not None and card["type"] != instead_of:
        card["substituted_for"] = instead_of
    return card


async def stash_card(
    conn: asyncpg.Connection, s: RateSession, card: dict[str, Any] | None
) -> RateSession:
    """Write the card and a fresh token in one statement, replacing whatever is there.

    `rate_session_card_has_token` makes "a card with no token" unrepresentable; issuing the
    token here rather than at the route is what makes that CHECK an invariant instead of a
    reminder.

    Unconditional, and only one caller may be: `record_correction`'s repaired pair, which
    DELIBERATELY replaces a standing card — a corrected pair that kept the old card would go on
    asking about a title the person has just said they have not seen — and which is serialised
    anyway, because `_claim_card`'s `FOR UPDATE` is the first statement of its transaction. The
    banner's head redraw replaces a standing card just as deliberately but holds no lock at all,
    so it goes through `_stash_if_unchanged`; everything else goes through `_stash_if_empty`.
    [M4.10 finding 4, cycle 1 m410-rev-02]
    """
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET current_card = $2::jsonb, card_token = $3, last_seen_at = now()
         WHERE id = $1
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        card,
        uuid.uuid4() if card is not None else None,
    )
    return _session(row)


async def _stash_if_unchanged(
    conn: asyncpg.Connection, s: RateSession, card: dict[str, Any] | None
) -> RateSession:
    """Replace the card this request read, or serve the card another request put there instead.

    §6.0's banner CTA "opens the §6.1 queue with those titles at the head", so this write has to
    replace a standing card — `_stash_if_empty`'s predicate would refuse it and serve the battle
    the banner had just promised not to. What it must not do is replace a card it never read.
    Finding 4's repair covered the empty-table branch and left this one writing with no predicate
    at all, so two devices following the same banner CTA both redrew and both stashed: two tokens
    for one session, last write wins, and the losing device's first tap was refused with "that card
    has already been answered" over a card nobody had answered. Measured at the route: one split in
    40 gathered redraws, and deterministic once the two draws were aligned.

    The token predicate is the whole fix, and a miss has three outcomes, not the two
    `_stash_if_empty` enumerates for its own callers. Another redraw won: serve it — both requests
    drew from the same pool under the same `head`, so it is a named title too. A tap answered the
    card and left the table empty: fill it with ours. Or — the commonest of the three, and the one
    `_stash_if_empty`'s "the same card either way" does not cover — a tap answered the card and
    `ensure_card` refilled it, because every `record_*` ends by drawing the next card, and a tap
    from a device that is not carrying the banner's `head` refills it with a title the banner did
    not name. Then the `card_token IS NULL` predicate misses as well and the re-read serves that
    card: §6.0 is not honoured on that request, and it is still the right answer, because the
    alternative is taking a card out from under the device that is holding its token — the refusal
    over a card nobody answered that this helper exists to stop. The banner's promise is kept on
    the next redraw; a clobbered token is not recoverable from the client at all.
    [M4.10 finding 4, cycle 1 m410-rev-02, cycle 2 M410-C2-D2-01]
    """
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET current_card = $2::jsonb, card_token = $3, last_seen_at = now()
         WHERE id = $1 AND card_token = $4
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        card,
        uuid.uuid4() if card is not None else None,
        s.card_token,
    )
    if row is not None:
        return _session(row)
    return await _stash_if_empty(conn, s, card)


async def _stash_if_empty(
    conn: asyncpg.Connection, s: RateSession, card: dict[str, Any] | None
) -> RateSession:
    """Stash a freshly drawn card, or serve the one another request stashed first.

    `ensure_card` decides to draw from `s.current_card is None` — a snapshot `_resume` read on
    an autocommit connection — and the old unconditional UPDATE then wrote whatever it drew.
    Two GETs on a session with an empty table therefore both drew and both stashed: same title,
    two tokens, last write wins, and `api/rate.py`'s own docstring promise ("a second GET
    returns the same card under the same token") was false. Reproduced: two tokens back, the
    first verdict 200 and the second 409, on nothing more exotic than a phone that reloaded
    while a laptop was open. The phone and the laptop are §6.2's household, not an edge case.

    `AND card_token IS NULL` makes the decision and the write one statement, so the loser's
    UPDATE matches no row; it then re-reads and serves the winner's card, which is the same
    card either way (both drew from the same pool) under the one token the surface will accept.
    A drawn card that is thrown away costs nothing durable: the draw writes nothing.
    [M4.10 finding 4]
    """
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET current_card = $2::jsonb, card_token = $3, last_seen_at = now()
         WHERE id = $1 AND card_token IS NULL
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        card,
        uuid.uuid4() if card is not None else None,
    )
    if row is not None:
        return _session(row)
    current = await conn.fetchrow(
        f"SELECT {_SESSION_COLUMNS} FROM rate_session WHERE id = $1", s.id
    )
    if current is None:  # pragma: no cover — the session was read one statement ago.
        raise RuntimeError(f"rate session {s.id} vanished under ensure_card")
    return _session(current)


_TITLE_COLUMNS = "id, kind, name, year, runtime_min, poster_path, overview"


async def _title_cards(conn: asyncpg.Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The poster-forward card of §6.8, and nothing else.

    The column list is the first half of the anchoring guard: `ledger_state` is not joined,
    `user_score` is not joined, and `title.placement` — the §8 stage-10 cold badge — is not
    read, because a badge that says "no crowd data yet" is still a statement about the model.
    """
    rows = await conn.fetch(
        f"SELECT {_TITLE_COLUMNS} FROM title WHERE id = ANY($1::int[])", list(ids)
    )
    return {
        r["id"]: {
            "id": r["id"],
            "kind": r["kind"],
            "name": r["name"],
            "year": r["year"],
            "runtime_min": r["runtime_min"],
            "poster_path": r["poster_path"],
            # §6.1's task on a sweep card is "did you see this?", so the aid is a plot
            # logline. Never "cleaned" — §4.1 rule 8.
            "recall_aid": _recall_aid(r["overview"]),
        }
        for r in rows
    }


def _recall_aid(overview: str | None, limit: int = 180) -> str | None:
    if not overview:
        return None
    text = overview.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


async def public_card(
    conn: asyncpg.Connection, s: RateSession
) -> dict[str, Any] | None:
    """§6.1: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003)."

    Built field by field from an allow-list. It never copies `current_card` and deletes keys:
    a deny-list leaks the first time a field is added, and the two fields it would leak are
    §13's re-ask reference and the pair's verdict band — the model's belief and the person's
    own prior label, the two things a card must not carry.

    Nothing that reaches this payload comes from `ledger_state`, `user_score` or
    `ledger_cutpoints`. The prediction exists and is computable; it is served in the response
    to the verdict, which is `predicted_class` below.
    """
    card = s.current_card
    if card is None or s.card_token is None:
        return None
    token = str(s.card_token)
    if card["type"] == "sweep":
        titles = await _title_cards(conn, [card["title_id"]])
        return {
            "type": "sweep",
            "token": token,
            "kind": card["kind"],
            "title": titles.get(card["title_id"]),
            "reason": card["reason"],
            "p_seen": card.get("p_seen"),
            # `source` stays server-side with `reask_of`: "reask" would mark the re-ask exactly
            # as loudly as the reference itself, and §13 wants the slot indistinguishable.
            "substituted_for": card.get("substituted_for"),
            # §6.8 / proposal 52: lowercase, worst -> best, matching the stored ordinal.
            "verdict_labels": [[i, label] for i, label in enumerate(VERDICT_LABELS)],
            "controls": ["verdict", "not_seen", "skip"],
        }
    titles = await _title_cards(conn, [card["title_a"], card["title_b"]])
    # Named `left`/`right` rather than `a`/`b` because §6.1's corrections row names the sides
    # exactly that way ("not seen: [left] [both] [right]"), and one vocabulary for the two
    # controls that sit on the same card is one fewer mapping for the client to get wrong. The
    # outcome letter each poster writes travels with it.
    return {
        "type": "battle",
        "token": token,
        "kind": card["kind"],
        "left": {**(titles.get(card["title_a"]) or {}), "outcome": "A"},
        "right": {**(titles.get(card["title_b"]) or {}), "outcome": "B"},
        "reason": card["reason"],
        "substituted_for": card.get("substituted_for"),
        "outcomes": list(OUTCOMES),
        # §6.1: "Corrections zone at the bottom (nothing tappable inside the poster cards),
        # one row: `not seen: [left] [both] [right]`".
        "corrections": {"label": "not seen", "sides": ["left", "both", "right"]},
        "controls": ["duel", "correction", "skip"],
    }


# --- the reveal, which happens only after the tap ------------------------------------------


async def _predicted_coordinate(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
) -> tuple[float, float] | None:
    """§5.2's zero-parameter prediction for a title with no `ledger_state` row of its own.

    §5.2 gives an unobserved title a coordinate at no extra parameters — it has no residual, so
    `s = mu + <v, e>` — and §5.2's displayed weight is the empirical CDF of the person's own
    fitted values. Both are in the cached fit, so this is arithmetic over numbers that already
    exist rather than a fit: exactly what `refit._update_incrementally` computes with r = 0, and
    what `refit_user` writes for every *owned* unrated title.

    IT HAS TO BE COMPUTED HERE BECAUSE `ledger_state` IS SIZED TO THE OWNED LIBRARY ON PURPOSE,
    and the sweep queue is not. `refit_user` writes rows for observed titles plus owned ones, and
    §6.1's first-run queue is the imported seed list — in v20260828, 100 titles of which 80 are
    unowned. Measured on that bundle, two members each rating 50 titles got `available: False` on
    50 of 50 taps and on 9 of 10 even after a full refit, so §6.1's "prediction reveal strictly
    after the tap" — the anchoring-safe feedback the whole surface is built around — was dark for
    precisely the sitting §12's M2 exit criterion is defined over. Widening the refit's row
    universe to the whole catalog was the other way and is the wrong one: it would write 19,000
    rows per person per kind for a number any one of them can be recomputed from in microseconds.

    Not locked (`lock=False`): this is a read for one sentence on one card, taken deliberately
    before the write transaction opens, and `load_cache`'s `FOR UPDATE` exists to serialise
    observations against each other — holding it here would make the reveal contend with the
    refit for no gain. Returns None rather than a reason: the caller owns the copy.
    [M4.10 finding 26, §5.2]
    """
    cache = await refit.load_cache(conn, user_id=user_id, kind=kind, hp=hp, lock=False)
    if cache is None or cache.cdf_reference.size < 2:
        return None
    matrix, _embedded = await observations.resolve_embeddings(
        embeddings or observations.zero_embeddings, [title_id]
    )
    s = float(cache.mu + matrix[0] @ cache.v)
    cdf = float(model.empirical_cdf(cache.cdf_reference, np.asarray([s]))[0])
    if not (math.isfinite(s) and math.isfinite(cdf)):
        # The same refusal `refit` makes of a non-finite update: say nothing rather than band a
        # number that is not one.
        return None
    return s, cdf


async def predicted_class(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
) -> dict[str, Any]:
    """What the model would have guessed — read BEFORE the write, served after it.

    Reading it after the row lands would make "we'd have guessed the same" trivially true: the
    incremental update touches exactly this title.

    The band is the person's own. §5.2: the displayed 0..1 weight "is the **empirical CDF of
    the user's own fitted `s` values, computed per kind**", and their own three-class habit
    says where the cuts on that axis fall — a labeller who calls 20% of what they watch
    disliked has their disliked band at the bottom 20% of their own ranking. So the prediction
    uses two quantities that already exist (`ledger_state.cdf` and the live verdict counts) and
    invents no threshold of its own. Before the first fit there is no CDF, and the reveal says
    so rather than banding a number it does not have.

    The stored row first, the cached fit second: `ledger_state` is where the nightly job and the
    incremental update leave the numbers every other surface reads, and an unowned queue title
    simply has no row there — see `_predicted_coordinate`. `hp` is required for the same reason
    `load_cache` compares it: a cache built under other constants is wrong, not stale.
    """
    row = await conn.fetchrow(
        "SELECT s, sigma, cdf, tier FROM ledger_state WHERE user_id = $1 AND title_id = $2",
        user_id,
        title_id,
    )
    if row is None or row["cdf"] is None:
        coordinate = await _predicted_coordinate(
            conn, user_id=user_id, title_id=title_id, kind=kind, hp=hp, embeddings=embeddings
        )
        if coordinate is None:
            return {
                "available": False,
                "reason": "no fitted ranking for this title yet — rate a few more first",
            }
        predicted_s, predicted_cdf = coordinate
    else:
        predicted_s, predicted_cdf = float(row["s"]), float(row["cdf"])
    counts = [0, 0, 0]
    for label in await conn.fetch(
        """
        WITH label AS ({LIVE_LABEL})
        SELECT l.value, count(*) AS n
          FROM label l JOIN title t ON t.id = l.title_id
         WHERE t.kind = $2
         GROUP BY l.value
        """.replace("{LIVE_LABEL}", LIVE_LABEL),
        user_id,
        kind,
    ):
        counts[label["value"]] = label["n"]
    total = sum(counts)
    if total == 0:
        return {"available": False, "reason": "no labels of your own to band against yet"}

    cdf = predicted_cdf
    low = counts[0] / total
    high = (counts[0] + counts[1]) / total
    guess = 0 if cdf < low else (1 if cdf < high else 2)
    return {
        "available": True,
        "predicted": guess,
        "predicted_label": VERDICT_LABELS[guess],
        "cdf": cdf,
        "s": predicted_s,
        "label_count": total,
    }


def reveal_for(prediction: dict[str, Any], value: int) -> dict[str, Any]:
    """§6.1's phrasing: "we'd have guessed the same" / "we'd have guessed {class}", with the
    number in the data voice beside its name (§6.8)."""
    if not prediction.get("available"):
        return dict(prediction)
    agreed = prediction["predicted"] == value
    head = "we'd have guessed the same" if agreed else (
        f"we'd have guessed {prediction['predicted_label']}"
    )
    return {**prediction, "agreed": agreed, "text": f"{head} · cdf {prediction['cdf']:.2f}"}


# --- §7.3's push, and its symmetric retraction ----------------------------------------------


async def _push_state(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    title_id: int,
) -> tuple[bool, str | None]:
    """Settle §7.3's debt now rather than in fifteen minutes — and outside the transaction.

    `observations.record_*` has already made the app-side write and left `jf_synced_at` NULL,
    which is precisely "the person acted and Jellyfin has not been told yet" — the sweep would
    push it eventually. `seen.push_owed` reads that owed row and pushes it, so the person sees
    their media server agree while the card is still on screen. With no connector the debt
    simply stands.

    It no longer takes a `state` and no longer calls `seen.set_state`: that call rewrote the
    same `user_title` row the observation had just written, one statement earlier, and then
    awaited a socket with a 15 s client budget — all of it inside `conn.transaction()`. Every
    caller below now calls this AFTER its transaction closes, and the state it pushes is the
    committed one rather than one passed alongside it. [M4.10 finding 11]
    """
    if jf is None or jf.client is None:
        # Answered without a query, because `api/rate.py._jellyfin` hands every tap a `Jellyfin`
        # whether the household has a connector or not, and a house with none is the common
        # case. §6.7's rail prints this reason verbatim, so an unreadable DEK must not read
        # there as "not configured" — the connector is configured and its credentials will not
        # open (M4.7 dd03). `seen.push_owed` draws the same distinction for the same string.
        if jf is not None and jf.cfg.secrets_unreadable:
            return False, SECRETS_UNREADABLE_REASON
        return False, "Jellyfin not configured"
    return await seen.push_owed(conn, jf.client, jf.cfg, user_id=user_id, title_id=title_id)


def _state_entries(
    write: observations.Write, pushed: dict[int, bool]
) -> list[dict[str, Any]]:
    """`rate_observation.prior_state`: what `user_title` held, plus whether we reached
    Jellyfin. Undo compensates what it did, not what it intended."""
    return [
        {**p.as_dict(), "pushed": bool(pushed.get(p.title_id, False))}
        for p in write.prior_state
    ]


async def _mark_pushed(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    session_id: int,
    seq: int,
    entries: Sequence[dict[str, Any]],
) -> None:
    """Decision 207: correct the journal's `pushed` flags once the push has actually resolved.

    The journal row is written inside the observation's transaction and the push happens after
    it, so at the moment `_append` runs the honest value of `pushed` is not yet known and false
    is what gets stored. Leaving it there would be a lie with teeth: `_state_entries`' own
    docstring says why — "Undo compensates what it did, not what it intended" — and `undo` reads
    exactly this field to decide whether to hand Jellyfin its Played flag back. A permanently
    false `pushed` silently drops the compensating write for every title Jellyfin *was* told
    about, and the next §7.3 sweep then reads our own write back as the household's history.

    One statement, outside the transaction, and best-effort: the observation is durable and the
    person's tap is answered either way, so a failure here is logged and the reconciliation is
    left to §7.3's sweep rather than turned into a 500 over a row that was written. Skipped
    entirely when nothing was pushed, which is every household with no connector.

    `AND undone_at IS NULL` is not tidiness, and neither is the compensation behind it. The row
    is visible and undoable for the whole of the push — §3.3's slow server makes that the 15 s
    client budget — and in that window it says `pushed = false`, so an Undo taken there skips
    `seen.retract` and leaves Jellyfin holding a Played flag the app has no record of. There is
    nothing left to reconcile it from either: `observations.undo` deletes the `user_title` row a
    first verdict created, so §7.3 reads an absent row plus Played and adopts it — the app taking
    its own write back as the household's history, which is the harm the paragraph above says
    this flag exists to prevent. When the UPDATE matches nothing the tap therefore owes the
    retraction the undo could not make, and it is the tap that makes it. `undo` takes the journal
    row `FOR UPDATE`, so both interleavings are covered: an undo that commits first is seen here
    as a tombstone, and one that commits later blocks on the row lock and then reads the corrected
    `pushed = true`. [§3.3, §7.3, decision 207; M4.10 cycle 2, M410-C2-F11-01]
    """
    if not any(entry.get("pushed") for entry in entries):
        return
    try:
        marked = await conn.fetchval(
            "UPDATE rate_observation SET prior_state = $3::jsonb "
            "WHERE session_id = $1 AND seq = $2 AND undone_at IS NULL "
            "RETURNING id",
            session_id,
            seq,
            list(entries),
        )
    except asyncpg.PostgresError as exc:
        log.warning(
            "journal row %d/%d kept prior_state.pushed = false after a push that succeeded: %s",
            session_id,
            seq,
            exc,
        )
        return
    if marked is not None or jf is None:
        return
    try:
        for entry in entries:
            if not entry.get("pushed"):
                continue
            log.info(
                "observation %d/%d was undone while its Jellyfin push was in flight - "
                "retracting the Played flag for title %s",
                session_id,
                seq,
                entry["title_id"],
            )
            await seen.retract(
                conn,
                jf.client,
                jf.cfg,
                user_id=user_id,
                title_id=int(entry["title_id"]),
                prior_state=entry.get("state") if entry.get("existed") else None,
            )
    except asyncpg.PostgresError as exc:
        # Logged and not raised, for the rule the rest of this milestone is about: the tap's
        # observation is durable and, here, already retracted, so a 500 would be a refusal over a
        # row the person cannot retry. A refused Played write inside `seen.retract` is already a
        # `(False, reason)` rather than an exception, so this catches the database half only.
        log.warning(
            "observation %d/%d was undone mid-push and its Played flag could not be retracted: %s",
            session_id,
            seq,
            exc,
        )


# --- the journal -----------------------------------------------------------------------------


async def _append(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    kind_of: str,
    card: dict[str, Any],
    title_ids: Sequence[int],
    verdict_id: int | None = None,
    duel_id: int | None = None,
    superseded_verdict_id: int | None = None,
    prior_state: Sequence[dict[str, Any]] = (),
    latency_ms: int | None = None,
) -> RateSession:
    """One journal row, then the cursor moves. Decision 35's "observation journal with
    compensating writes rather than a lastAction variable".

    `advances` is derived from `kind_of` here and pinned by the migration's
    `rate_observation_advances_rule` CHECK — a correction is a repair, not an observation, so
    it redraws the pair in place and the counter the person is reading does not move.

    The two statements are atomic HERE and not only at the call sites, because for a year they
    were atomic at four of the five: `record_skip` called this bare. The INSERT is at `seq + 1`
    and the UPDATE is what moves the session to it, so a process death, a dropped pool
    connection or a cancelled task between them leaves the journal at N+1 with the session at
    N — and `rate_observation_seq` then refuses every later append in that session for ever.
    Reproduced from that state: verdict, skip and not-seen all 500, `GET /api/rate` kept
    serving a card that could not be answered, undo tombstoned the phantom row and the next
    verdict still 500'd (the unique index is not partial on `undone_at`), and only
    `DELETE /api/rate/session` recovered it. Skip is the most frequent tap in a sweep.

    A guard rather than an unconditional `conn.transaction()` so the four callers that already
    hold one pay nothing: asyncpg would nest as a savepoint, which is correct but is two more
    round trips on a path §6 budgets at 2 s. `test_static_contracts.py` asserts the call sites
    independently, because this fallback is the second line of defence and not the contract.
    [M4.10 finding 3, decision 35]
    """
    advances = kind_of != "correction"
    block_index, slot = (
        advance(s.block_index, s.slot) if advances else (s.block_index, s.slot)
    )
    async with contextlib.AsyncExitStack() as stack:
        if not conn.is_in_transaction():
            await stack.enter_async_context(conn.transaction())
        try:
            await conn.execute(
                """
                INSERT INTO rate_observation
                    (session_id, user_id, seq, block_index, slot, kind_of, advances, card,
                     title_ids, verdict_id, duel_id, superseded_verdict_id, prior_state,
                     latency_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::int[], $10, $11, $12,
                        $13::jsonb, $14)
                """,
                s.id,
                s.user_id,
                s.seq + 1,
                s.block_index,
                s.slot,
                kind_of,
                advances,
                card,
                list(title_ids),
                verdict_id,
                duel_id,
                superseded_verdict_id,
                list(prior_state),
                latency_ms,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "rate_observation_seq":
                raise
            # Belt and braces behind `_claim_card`'s row lock, and the reason it stays: two taps
            # that both reached this INSERT answered the same card, so the second one is
            # §6.1's stale card and must leave through the 409 the surface can act on rather
            # than through `app.py`'s generic conflict handler, which names a constraint the
            # client has no rule for. `tonight/rooms.py:102-108` maps its own natural key the
            # same way. [M4.10 finding 2]
            raise StaleCard("stale_card") from exc
        row = await conn.fetchrow(
            f"""
            UPDATE rate_session
               SET seq = $2, block_index = $3, slot = $4,
                   current_card = NULL, card_token = NULL, last_seen_at = now()
             WHERE id = $1
            RETURNING {_SESSION_COLUMNS}
            """,
            s.id,
            s.seq + 1,
            block_index,
            slot,
        )
    return _session(row)


async def _claim_card(conn: asyncpg.Connection, s: RateSession, card_token: str) -> None:
    """Take the card under a row lock, as the first statement of the write's transaction.

    `_take_card` validates against the snapshot `_resume` read on an autocommit connection, so
    two taps on one token both pass it: a double tap, a second tab, a phone and a laptop on one
    account. The loser used to get as far as `INSERT INTO rate_observation`, collide with
    `rate_observation_seq` and roll back — no duplicate observation, but by then its Jellyfin
    Played write had already gone out for a card someone else had answered. M4.7's handler turned
    what the person then saw from a blanket 500 into a 409 naming that constraint, which is
    better and is still not §6.1's refusal: the client has a rule for `stale_card` and none for
    the name of an index. §6.1 makes the card the server's; this is what makes that true when
    two answers arrive together, and it does so before the socket rather than after it.

    `FOR UPDATE` on `rate_session` and not on anything else, because that row is what every tap
    ends by writing (`_append`'s UPDATE) — so the loser blocks here, wakes after the winner
    commits, re-reads `card_token` as NULL and leaves through `api/rate.py`'s existing 409
    BEFORE any observation is written and before the socket is touched. Deliberately not taken
    in `_resume` / `open_or_resume`: those are shared with the GET and the controls route,
    where a lock held across a draw would serialise reading the surface as well as writing it.
    [M4.10 finding 2]
    """
    row = await conn.fetchrow(
        "SELECT card_token FROM rate_session WHERE id = $1 FOR UPDATE", s.id
    )
    if row is None or str(row["card_token"]) != str(card_token):
        raise StaleCard("stale_card")


def _take_card(s: RateSession, token: str, *, want: CardType) -> dict[str, Any]:
    if s.current_card is None or s.card_token is None:
        raise StaleCard("no_card")
    if str(s.card_token) != str(token):
        raise StaleCard("stale_card")
    if s.current_card["type"] != want:
        raise StaleCard("wrong_card_type")
    return s.current_card


async def _verdict_rail_line(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    value: int,
    refit_ms: float | None,
) -> RailLine:
    """§6.7's commonest line, rendered here because here is where its four facts are true.

    `ledger/observations.py` keeps its own sentence — `verdict(title 5) = liked -> ordered-logit
    arm · implies seen` — and the two are DELIBERATELY different. That one is the domain layer's
    audit record of the row it wrote: it belongs to callers that never saw a person or a rail,
    and its qualifiers (`implies seen`, §13's re-ask marker) are facts about the row rather than
    about the model write. This one is §6.7's narration of the same write, and it names the
    person and the film because §6.8 forbids a bare model number — a bare title id is exactly
    one, and nothing on the client can resolve it back into a film.

    The two names cost one round trip on a path §6 budgets at 2 s, taken here rather than
    threaded down from the route because this is the only moment that holds the title, the
    label and the incremental refit's own milliseconds together; `rail.record`'s docstring is
    explicit that a line recomposed later "from numbers that have since moved" is the thing to
    avoid. [M4.9 finding 23, plan step 7.1]
    """
    row = await conn.fetchrow(
        "SELECT (SELECT name FROM app_user WHERE id = $1) AS rater,"
        " (SELECT name FROM title WHERE id = $2) AS title",
        user_id,
        title_id,
    )
    # Both rows exist by foreign key — the verdict this line narrates references them — so these
    # fall back only for a database that has already broken its own constraints, and they say so
    # rather than printing `None` or reaching for the id §6.8 rules out.
    rater = row["rater"] or "an unknown rater"
    title_name = row["title"] or "an unknown title"
    return RailLine(
        rail.verdict_line(rater, title_name, VERDICT_LABELS[value], refit_ms=refit_ms),
        title_id=title_id,
    )


# --- the five taps ---------------------------------------------------------------------------


async def record_verdict(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    value: int,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """§6.1's `Liked / Fine / Disliked`, and "Verdict implies `seen`".

    `bundle_version` travels beside `embeddings` and says which basis it is expressed in, because
    the two are one fact and `refit.load_cache` has to be able to check it. See
    `refit.update_incrementally`: the route pins both at boot and the tap can outlive the flip.
    [M4.13 cycle 1, finding 15]
    """
    card = _take_card(s, card_token, want="sweep")
    if value not in (0, 1, 2):
        raise ValueError(f"verdict value must be 0, 1 or 2, not {value!r}")
    title_id = card["title_id"]

    # Strictly first: the reveal is what the model believed *before* this label existed.
    prediction = await predicted_class(
        conn,
        user_id=s.user_id,
        title_id=title_id,
        kind=card["kind"],
        hp=hp,
        embeddings=embeddings,
    )

    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        write = await observations.record_verdict(
            conn,
            user_id=s.user_id,
            title_id=title_id,
            value=value,
            source="sweep",
            # §13 stream (b): distinguishable server-side, invisible in the payload.
            is_reask=card.get("reask_of") is not None,
            reask_of=card.get("reask_of"),
        )
        s = await _append(
            conn,
            s,
            kind_of="verdict",
            card=card,
            title_ids=write.title_ids,
            verdict_id=write.row_id,
            superseded_verdict_id=write.superseded_id,
            # `pushed` is false here and corrected by `_mark_pushed` below: the push has not
            # happened yet, and this row must say what was done, not what is about to be.
            prior_state=_state_entries(write, {}),
            latency_ms=latency_ms,
        )

    # §7.3's socket, deliberately after the commit. `observations.record_verdict` has already
    # written `user_title` with `jf_synced_at = NULL` inside the transaction above, which is
    # precisely §7.3's "owed" — so what the transaction keeps is the person's action and what it
    # loses is a foreign server's 15 s budget. [M4.10 finding 11]
    pushed, reason = await _push_state(conn, jf, user_id=s.user_id, title_id=title_id)
    await _mark_pushed(
        conn,
        jf,
        user_id=s.user_id,
        session_id=s.id,
        seq=s.seq,
        entries=_state_entries(write, {title_id: pushed}),
    )

    ledger = await refit.update_incrementally_reporting(
        conn,
        user_id=s.user_id,
        kind=write.kind,
        title_ids=write.title_ids,
        hp=hp,
        embeddings=embeddings,
        bundle_version=bundle_version,
    )
    # §6.7's line is composed here, one statement after the millisecond count it quotes and
    # while the answered card and the label are still in hand. It REPLACES `write.log` on this
    # arm rather than joining it: `payload` turns every line in `log` into one rail event, so
    # carrying both would narrate one write twice, in two formats. The audit sentence itself
    # is untouched in `ledger/observations.py` — `_verdict_rail_line` says why the two are
    # allowed to read differently. [M4.9 finding 23, plan step 7.1]
    line = await _verdict_rail_line(
        conn,
        user_id=s.user_id,
        title_id=title_id,
        value=value,
        refit_ms=(ledger or {}).get("ms"),
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(
        session=s,
        reveal=reveal_for(prediction, value),
        log=(line, _sync_line("seen", pushed, reason)),
        ledger=ledger,
    )


def _sync_line(state: str, pushed: bool, reason: str | None) -> str:
    """§6.7's rail reports what actually happened, never a write that did not happen."""
    played = "true" if state == "seen" else "false"
    if pushed:
        return f"user_title.state = {state} -> Jellyfin Played {played}"
    return f"user_title.state = {state} -> not pushed ({reason or 'no connector'})"


async def record_not_seen(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """§6.1's `Not seen`, and the owner decision of 2026-08-29: there is no third state. A
    title you cannot remember is plain `unseen`, and §4.2's append-only history survives the
    flip — this writes no observation row and deletes none."""
    card = _take_card(s, card_token, want="sweep")
    title_id = card["title_id"]
    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        write = await observations.record_not_seen(
            conn, user_id=s.user_id, title_id=title_id
        )
        s = await _append(
            conn,
            s,
            kind_of="not_seen",
            card=card,
            title_ids=write.title_ids,
            prior_state=_state_entries(write, {}),
            latency_ms=latency_ms,
        )
    pushed, reason = await _push_state(conn, jf, user_id=s.user_id, title_id=title_id)
    await _mark_pushed(
        conn,
        jf,
        user_id=s.user_id,
        session_id=s.id,
        seq=s.seq,
        entries=_state_entries(write, {title_id: pushed}),
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, log=(write.log, _sync_line("unseen", pushed, reason)))


async def record_skip(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """`Skip` writes nothing to any arm. The journal row *is* the suppression: the card's
    titles are in `title_ids`, and `_observed_title_ids` keeps them out of the rest of the
    sitting. It is not a `not_seen`, so §13's not-seen-rate instrument does not count it."""
    if s.current_card is None or s.card_token is None:
        raise StaleCard("no_card")
    if str(s.card_token) != str(card_token):
        raise StaleCard("stale_card")
    card = s.current_card
    titles = (
        [card["title_id"]] if card["type"] == "sweep" else [card["title_a"], card["title_b"]]
    )
    # Wrapped like its four siblings, and for decision 35's reason rather than for tidiness:
    # `_append` is an INSERT at `seq + 1` and an UPDATE that moves the session to it, and this
    # was the one tap that issued them on the autocommit connection. See `_append`. [finding 3]
    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        s = await _append(conn, s, kind_of="skip", card=card, title_ids=titles,
                          latency_ms=latency_ms)
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, log=("skipped — no observation row written",))


async def record_duel(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    outcome: str,
    hp: Hyperparams,
    decisive: bool | None = None,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """§6.1's battle answer: exactly one duel row, context `profile_battle`.

    A `TIE` is one of those rows and never a skip — §4.2: "'about the same' is first-class
    data: 22% of random pairs are genuine ties" — and dropping it would starve the Davidson
    tie term the arm is built around.

    The margin comes from the session's persistent decisive toggle (§6.1: "~1.6 vs 1.0"), read
    through `hp.margin_for` so the two numbers stay in `ledger_hyperparams.json` where §4.3
    puts them. A per-request `decisive` overrides for one answer without moving the toggle,
    which is where the long-press accelerator lands.
    """
    card = _take_card(s, card_token, want="battle")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    hard = s.decisive if decisive is None else decisive

    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        write = await observations.record_duel(
            conn,
            user_id=s.user_id,
            title_a=card["title_a"],
            title_b=card["title_b"],
            outcome=outcome,
            context=BATTLE_CONTEXT,
            selection=BATTLE_SELECTION,
            decisive=hard,
            hp=hp,
            is_reask=card.get("reask_of") is not None,
            reask_of=card.get("reask_of"),
        )
        s = await _append(
            conn,
            s,
            kind_of="tie" if outcome == "TIE" else "duel",
            card=card,
            title_ids=write.title_ids,
            duel_id=write.row_id,
            latency_ms=latency_ms,
        )

    ledger = await refit.update_incrementally_reporting(
        conn,
        user_id=s.user_id,
        kind=write.kind,
        title_ids=write.title_ids,
        hp=hp,
        embeddings=embeddings,
        bundle_version=bundle_version,
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, log=(write.log,), ledger=ledger)


async def record_correction(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    side: Side,
    jf: Jellyfin | None = None,
    rng: Any = None,
) -> Outcome:
    """§6.1's corrections zone: "`not seen: [left] [both] [right]` -> sets that side `unseen`,
    swaps it out of the pair (`both` swaps the whole pair), **writes no duel row**, syncs per
    §7.3, covered by the persistent Undo."

    It does not advance. A correction is a repair of the question, not an answer to it, and
    the migration's `rate_observation_advances_rule` CHECK is what stops that from drifting.

    The corrected title's own verdicts and duels are untouched: §4.2's history is append-only
    and survives the flip. The title leaves the battle pool because the pool is a conjunction —
    marked seen AND carrying a live verdict — not because anything was deleted.
    """
    card = _take_card(s, card_token, want="battle")
    if side not in ("left", "both", "right"):
        raise ValueError(f"side must be left, both or right, not {side!r}")
    corrected = {
        "left": [card["title_a"]],
        "right": [card["title_b"]],
        "both": [card["title_a"], card["title_b"]],
    }[side]

    writes: list[observations.Write] = []
    lines: list[str] = []
    async with conn.transaction():
        await _claim_card(conn, s, card_token)
        for title_id in corrected:
            writes.append(
                await observations.record_not_seen(
                    conn, user_id=s.user_id, title_id=title_id
                )
            )
        s = await _append(
            conn,
            s,
            kind_of="correction",
            card=card,
            title_ids=corrected,
            prior_state=[e for w in writes for e in _state_entries(w, {})],
        )
        replacement = await _redraw_pair(conn, s, card, corrected=corrected, rng=rng)
        s = await stash_card(conn, s, replacement)

    # Both pushes after the commit, for finding 11's reason — and `both` makes the cost of the
    # old shape plainest: two 15 s-budget sockets awaited in series inside one transaction.
    pushes: dict[int, bool] = {}
    for title_id in corrected:
        pushed, reason = await _push_state(conn, jf, user_id=s.user_id, title_id=title_id)
        pushes[title_id] = pushed
        lines.append(_sync_line("unseen", pushed, reason))
    await _mark_pushed(
        conn,
        jf,
        user_id=s.user_id,
        session_id=s.id,
        seq=s.seq,
        entries=[e for w in writes for e in _state_entries(w, pushes)],
    )

    lines.append(
        "pair half swapped, no duel row written"
        if side != "both"
        else "pair swapped, no duel row written"
    )
    return Outcome(session=s, log=tuple(lines))


async def _redraw_pair(
    conn: asyncpg.Connection,
    s: RateSession,
    card: dict[str, Any],
    *,
    corrected: Sequence[int],
    rng: Any = None,
) -> dict[str, Any] | None:
    """Keep the half the person did not correct; replace the half they did.

    §6.1's correction exists to remove a title from the duel pool without inventing a comparison
    the person never made — so the half they kept must keep its place, against a fresh opponent
    from its own verdict band.

    The opponent is drawn from the band DIRECTLY. It used to be drawn by asking
    `battle.next_battle_pair` for a whole pair and rejecting any that fell outside the
    survivor's class, eight times. `battle.draw` weights strata by pair count n(n-1)/2, so the
    chance of hitting a small band is small by construction: on a 60/20/20 labeller — exactly
    the shape §5.2's class-balance warning pushes people toward — correcting a title in one of
    the minority bands missed on every attempt about half the time, and the battle silently
    became a sweep card with nothing on screen saying why.

    Uniform within the band, for the same reason `battle.draw` is: §0 row 6 measured that no
    selection rule beats random for profiles (best +0.0013, CI spans 0).
    """
    survivor = next((t for t in (card["title_a"], card["title_b"]) if t not in corrected), None)
    exclude = set(await _skipped_title_ids(conn, s.id)) | set(corrected)
    if survivor is None:
        pair = await _draw_battle(conn, s, exclude=sorted(exclude), rng=rng)
        if pair is not None:
            return pair
        # `both` left nothing to keep and no pair to draw, so the slot falls through to a sweep —
        # marked, because the counter still wants the battle this card was. [M4.10 finding 21]
        return _mark_substitution(
            await _draw_sweep(
                conn, s, exclude=sorted(await _observed_title_ids(conn, s.id)), head=()
            ),
            instead_of=card["type"],
        )

    pool = await battle.battle_pool(
        conn,
        user_id=s.user_id,
        kinds=[card["kind"]],
        exclude=tuple(sorted(exclude | {survivor})),
    )
    band = sorted(m.title_id for m in pool if m.verdict_class == card["verdict_class"])
    if band:
        opponent = (rng or random).choice(band)
        keep_left = survivor == card["title_a"]
        return {
            "type": "battle",
            "kind": card["kind"],
            "title_a": survivor if keep_left else opponent,
            "title_b": opponent if keep_left else survivor,
            "verdict_class": card["verdict_class"],
            "reason": card["reason"],
            "reask_of": None,
        }
    # No second title left in the survivor's class: the pair cannot be repaired, so the slot
    # falls back the same way `ensure_card` does when the pool is thin — and is marked the same
    # way, which it was not. [M4.10 finding 21]
    return _mark_substitution(
        await _draw_sweep(
            conn,
            s,
            exclude=sorted(set(await _observed_title_ids(conn, s.id)) | set(corrected)),
            head=(),
        ),
        instead_of=card["type"],
    )


# --- undo ------------------------------------------------------------------------------------


async def _undo_reaches(
    conn: asyncpg.Connection, s: RateSession, *, block_index: int, slot: int
) -> bool:
    """Decision 35's depth, with decision 199's boundary: is this journal row still undoable?

    Decision 35 says "back to the start of the current block of 15 and no further", and decision
    174 says when a block is finished: when the FIRST observation of the next block lands, "which
    is literally what decision 35 says". `advance` rolls the counter on the fifteenth observation,
    so comparing block indexes alone disabled the chip on the same round trip that answered card
    15 — 6.7% of every observation a household makes, at the end of a run where fatigue mis-taps
    live, and in Battle mode a duel, which §4.2 gives no supersede path.

    So the previous block is reachable too, under decision 199's arithmetic: the person is at slot
    1 and the row they are reaching for is the fifteenth of the block before.

    THE SECOND STATEMENT IS WHAT MAKES "UNTIL THE SIXTEENTH LANDS" MEAN WHAT IT SAYS. The
    arithmetic alone is `s.slot == 1`, and undo restores the session to the block and slot of the
    row it pops — so after the sixteenth tap had been made AND retracted the session would be back
    at slot 1 and the fifteenth reachable again, which is not "the sixteenth commits the block it
    ended" (decision 199's own title, and `library-rate-undo-block-depth`'s requirement text) and
    leaves decision 35's "and no further" bounding one tap rather than a walk: every earlier block
    would come back, one undo at a time. So the journal is asked whether the new block has EVER
    held a row, tombstones included. It runs only at the boundary — the common case returns on the
    first comparison — and the rest of decision 199 is untouched: `advance`, the block and slot
    each row stores, and the reach inside one block all stand.
    [decisions 35, 174, 199; M4.10 finding 25]
    """
    if block_index == s.block_index:
        return True
    if not (s.slot == 1 and block_index == s.block_index - 1 and slot == BLOCK_SIZE):
        return False
    started = await conn.fetchval(
        "SELECT 1 FROM rate_observation WHERE session_id = $1 AND block_index = $2 LIMIT 1",
        s.id,
        s.block_index,
    )
    return started is None


async def undo_availability(conn: asyncpg.Connection, s: RateSession) -> dict[str, Any]:
    """Decision 35: "the chip disables visibly at the boundary"."""
    row = await conn.fetchrow(
        "SELECT kind_of, block_index, slot FROM rate_observation "
        "WHERE session_id = $1 AND undone_at IS NULL ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    if row is None:
        return {"available": False, "kind": None, "reason": "empty"}
    if not await _undo_reaches(conn, s, block_index=row["block_index"], slot=row["slot"]):
        return {"available": False, "kind": None, "reason": "block_boundary"}
    return {"available": True, "kind": row["kind_of"], "reason": None}


async def undo(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    jf: Jellyfin | None = None,
) -> Outcome:
    """Pop the most recent observation of any kind and put the card that produced it back.

    Decision 35 in three parts, all here:

      * **Any kind.** Verdict, not-seen, skip, duel, tie and correction all leave a journal
        row, so all six are undoable by the same tap. A `lastAction` variable could not cover
        the corrections row at all.
      * **The exact card.** `rate_observation.card` holds the card verbatim, so a battle pair
        comes back as itself rather than reshuffling, and the person lands on what they
        answered rather than on the neighbouring queue position.
      * **One block.** The journal row's own `block_index` and `slot` are the test, in
        `_undo_reaches` — shared with `undo_availability` so the chip and the refusal cannot
        disagree. They are compared here rather than passed to `observations.undo` as a
        `block_started_at` timestamp, because the journal row is written *after* the ledger row
        it describes — a timestamp comparison would refuse the first observation of every block
        by a few microseconds.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id, kind_of, block_index, slot, card, title_ids, verdict_id, duel_id,
                   superseded_verdict_id, prior_state
              FROM rate_observation
             WHERE session_id = $1 AND undone_at IS NULL
             ORDER BY seq DESC LIMIT 1
             FOR UPDATE
            """,
            s.id,
        )
        if row is None:
            raise UndoUnavailable("empty")
        if not await _undo_reaches(conn, s, block_index=row["block_index"], slot=row["slot"]):
            raise UndoUnavailable("block_boundary")

        priors = [PriorState.from_dict(p) for p in (row["prior_state"] or [])]
        pushed = {int(p["title_id"]): bool(p.get("pushed")) for p in (row["prior_state"] or [])}
        kind_of = row["kind_of"]
        title_ids = list(row["title_ids"])
        arm = {
            "verdict": "verdict",
            "duel": "duel",
            "tie": "duel",
            "not_seen": "not_seen",
            "correction": "not_seen",
        }.get(kind_of)
        row_id = row["verdict_id"] if kind_of == "verdict" else row["duel_id"]

        undone: observations.Undo | None = None
        if arm is not None:
            # The only code permitted to delete a verdict or a duel row (§4.2 is append-only
            # for everything else). `not_seen` covers both the Not-seen tap and the
            # corrections row: neither wrote an observation, and both need `user_title` and
            # §7.3's `jf_synced_at` put back exactly as they were.
            undone = await observations.undo(
                conn,
                user_id=s.user_id,
                arm=arm,
                row_id=row_id,
                title_ids=title_ids,
                prior_state=priors,
            )
        await conn.execute(
            "UPDATE rate_observation SET undone_at = now() WHERE id = $1", row["id"]
        )
        restored = await conn.fetchrow(
            f"""
            UPDATE rate_session
               SET block_index = $2, slot = $3, current_card = $4::jsonb, card_token = $5,
                   last_seen_at = now()
             WHERE id = $1
            RETURNING {_SESSION_COLUMNS}
            """,
            s.id,
            row["block_index"],
            row["slot"],
            dict(row["card"]),
            uuid.uuid4(),
        )
        s = _session(restored)

    for prior in priors:
        if pushed.get(prior.title_id) and jf is not None:
            # `seen.retract` and no longer a private copy of it: the copy had drifted, and what
            # it had lost was §7.3's "a 401 on write -> re-link prompt" — an expired token
            # discovered on an Undo left the link reading `linked` with nothing asking the
            # person to fix it. [M4.10 finding 11]
            await seen.retract(
                conn,
                jf.client,
                jf.cfg,
                user_id=s.user_id,
                title_id=prior.title_id,
                prior_state=prior.state if prior.existed else None,
            )

    ledger = None
    if undone is not None and arm in ("verdict", "duel"):
        ledger = await refit.update_incrementally_reporting(
            conn,
            user_id=s.user_id,
            kind=undone.kind,
            title_ids=title_ids,
            hp=hp,
            embeddings=embeddings,
            bundle_version=bundle_version,
        )
    lines = [undone.log] if undone is not None else [f"undo: {kind_of} — nothing to retract"]
    return Outcome(session=s, log=tuple(lines), ledger=ledger, undone=kind_of)


# --- the payload -------------------------------------------------------------------------------


async def payload(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    reveal: dict[str, Any] | None = None,
    log: Sequence[str] = (),
    ledger: dict[str, Any] | None = None,
    event_kind: str | None = None,
    user: Any = None,
) -> dict[str, Any]:
    """One envelope for every route, so the client has one shape to render.

    The next card travels in the response to the write. §6 preamble: "<2 s per sweep card,
    <1.5 s per battle, undo everywhere, next card preloaded" — a client that has to ask for
    the next card after every tap cannot make that budget.
    """
    # §6.7's rail, fed from the lines this response already carries. `event_kind` is None for a
    # read and for a skip: §6.7 narrates "every model write", and a skip writes no observation —
    # its own log line says so. Recording it would put a non-write in the log of writes.
    #
    # `title_id` is read off the line rather than passed beside it because only some lines are
    # about one title — the verdict's is, the Jellyfin push line that follows it is not — and
    # the handler that knows which is which has already returned. See `RailLine`.
    if event_kind is not None:
        for line in log:
            rail.record(
                kind=event_kind,
                line=line,
                user_id=s.user_id,
                title_id=getattr(line, "title_id", None),
            )

    card = await public_card(conn, s)
    shares = await balance.class_balance(conn, user_id=s.user_id, kinds=s.kinds)
    body = {
        "session": {
            "id": s.id,
            "mode": s.mode,
            "kinds": s.kinds,
            "decisive": s.decisive,
            "block": {
                "index": s.block_index,
                "slot": s.slot,
                "size": BLOCK_SIZE,
                # §6.1's counter, and the unit decision 35's Undo depth is measured in.
                "counter": f"{s.slot} / {BLOCK_SIZE}",
                # What the counter CALLS FOR, which is not always what is on the table: a
                # substitution is served in place and the slot is deliberately not moved, so the
                # flip is reported on the card as `substituted_for` rather than by quietly
                # rewriting this.
                #
                # The two disagree on a substitution, which the card explains — and on one flip no
                # draw-time marker can see, which it does not: `set_controls` drops the card on a
                # mode change and keeps the counter, and `undo` restores the journal's card verbatim
                # without restoring the mode it was drawn under, so Mix -> Sweep -> Mix -> Undo puts
                # a sweep card under `serving: "battle"` with no marker. That is the field doing
                # exactly what it says (the counter, in the mode now in force, calls for a battle)
                # and it is why `rate/+page.svelte` has named the card's own type in the counter line
                # since M2. Deriving `serving` from the card instead — the plan's other option — was
                # weighed and refused: it would make the field a second spelling of `card.type` and
                # leave the counter's call nowhere, and `ops/devstub.py` mirrors this definition on
                # purpose. [M4.10 finding 21, decision 200, cycle 1 M410-D8-03]
                "serving": card_type_for(s.mode, observation_index(s.block_index, s.slot)),
            },
        },
        "card": card,
        # Keyed by cause, because "no card" has three of them and one of the three is not a
        # drained queue at all. [M4.10 finding 20]
        "drained": None if card else drained_for(s.mode),
        # §5.2: "the rating UI shows a running class balance" — the measured 5x lever. Rendered
        # by `balance`'s own projection, so the widget's copy and its threshold have one home.
        "class_balance": shares.as_dict(),
        "undo": await undo_availability(conn, s),
        "reveal": reveal,
        "ledger": ledger,
        # §6.7's rail. Also recorded above, into the ephemeral buffer §6.7 asks for; the
        # response carries them too so the client can show the line for the tap just made
        # without a second request.
        "log": list(log),
    }
    # Decision 117: with the toggle off the rail and every inline number are ABSENT, not hidden.
    # Same gate as §6.0's Home, so one preference cannot mean two things on two surfaces — and a
    # surface that shipped the numbers and let the client hide them would make the promise
    # cosmetic. `reveal` survives: §6.1 requires the prediction after the tap.
    return rail.redact(body, show_model=rail.visible_to(user)) if user is not None else body


__all__ = [
    "BLOCK_SIZE",
    "DRAINED_CAUSES",
    "MODES",
    "OUTCOMES",
    "Jellyfin",
    "Outcome",
    "RateSession",
    "StaleCard",
    "UndoUnavailable",
    "advance",
    "card_type_for",
    "drained_for",
    "end_session",
    "ensure_card",
    "observation_index",
    "open_or_resume",
    "payload",
    "predicted_class",
    "public_card",
    "record_correction",
    "record_duel",
    "record_not_seen",
    "record_skip",
    "record_verdict",
    "set_controls",
    "undo",
    "undo_availability",
]
