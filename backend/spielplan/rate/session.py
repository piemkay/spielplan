"""The Rate surface's state machine (§6.1, §6.7, §13, decision 35).

The server owns the block counter, the card on the table and its token, so Undo's boundary and a
stale tap are enforceable. It draws no cards itself: `queue` and `battle` decide what to ask.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import numpy as np

from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import SECRETS_UNREADABLE_REASON, JellyfinConfig
from spielplan.db import pool as db_pool
from spielplan.db.library import normalise_kinds
from spielplan.home import rail
from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.observations import VERDICT_LABELS, EmbeddingSource, PriorState
from spielplan.rate import balance, battle, queue
from spielplan.sync import seen

log = logging.getLogger("spielplan.rate.session")

# §6.1: "blocks of 15", the unit decision 35 measures Undo's depth in.
BLOCK_SIZE = 15

# Decision 492: Mix starts battling once the person holds one block of live ratings. See `warm_up`.
MIX_WARMUP_LABELS = BLOCK_SIZE

MODES: tuple[str, ...] = ("mix", "sweep", "battle")
CardType = Literal["sweep", "battle"]
Side = Literal["left", "both", "right"]

# §4.2: outcome A | B | TIE. "About the same" is first-class data (22% of random pairs are
# genuine ties), so TIE is an outcome here and never a skip.
OUTCOMES: tuple[str, ...] = ("A", "B", "TIE")

# Random by design (§0 row 6), so neither boundary-targeted nor in §13 stream (a)'s holdout.
BATTLE_CONTEXT = "profile_battle"
BATTLE_SELECTION = "random"


# §6.1's empty state, keyed by its cause: the mode decided which draws were attempted.
DRAINED_CAUSES: dict[str, dict[str, str]] = {
    "queue": {
        "cause": "queue",
        "text": (
            "You've rated everything we can queue right now. Pairs sharpen what you've "
            "already said."
        ),
    },
    "pool": {
        "cause": "pool",
        "text": (
            "A pair is two titles you rated the same way, and there is no new pair to "
            "compare yet. Rate a few more one by one and the pairs start arriving."
        ),
    },
    "both": {
        "cause": "both",
        "text": (
            "You've rated everything we can queue right now, and there is no new pair of your "
            "ratings left to compare either."
        ),
    },
}


def drained_for(mode: str) -> dict[str, str]:
    """Which empty state this is, from the mode that decided which pools were tried."""
    if mode == "sweep":
        return DRAINED_CAUSES["queue"]
    if mode == "battle":
        return DRAINED_CAUSES["pool"]
    return DRAINED_CAUSES["both"]


class StaleCard(Exception):
    """The answer names a card that is not the one on the table (double tap, back, second device)."""

    def __init__(self, reason: str = "stale_card") -> None:
        super().__init__(reason)
        self.reason = reason


class UndoUnavailable(Exception):
    """Decision 35: "back to the start of the current block of 15 and no further"."""

    def __init__(self, reason: Literal["empty", "block_boundary"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Jellyfin:
    """§7.3's write path, injected. `client is None` is legal (§3.3): the write then stays owed."""

    client: JellyfinClient | None
    cfg: JellyfinConfig


@dataclass
class RateSession:
    id: int
    user_id: int
    kinds: list[str]
    mode: str
    block_index: int
    slot: int
    seq: int
    current_card: dict[str, Any] | None
    card_token: uuid.UUID | None


class RailLine(str):
    """A log line that remembers which title it narrates.

    A `str` subclass because `Outcome.log` is serialised and read with `in`; the id travels with
    the line because `rail.record` runs after `s.current_card` has moved on.
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
    """The monotone observation count decision 200 derives the card type from.

    Not `rate_session.seq`: a correction appends without advancing, and Undo moves this back.
    """
    return block_index * BLOCK_SIZE + slot - 1


def card_type_for(mode: str, index: int) -> CardType:
    """§6.1: "Mix (default — alternates sweep and battle); blocks of 15."

    On the monotone index, not the slot: fifteen is odd, so the slot served two sweeps at every
    roll (decision 200). Index 0 is a sweep.
    """
    if mode == "sweep":
        return "sweep"
    if mode == "battle":
        return "battle"
    return "sweep" if index % 2 == 0 else "battle"


def warm_up(wanted: CardType, *, mode: str, labels: int) -> CardType:
    """Decision 492: in Mix, no battle until `MIX_WARMUP_LABELS` live ratings stand.

    Changes the counter's call, not the card under it, so nothing is marked as a substitution.
    """
    if mode == "mix" and labels < MIX_WARMUP_LABELS:
        return "sweep"
    return wanted


def advance(block_index: int, slot: int) -> tuple[int, int]:
    """§6.1: "the counter runs 1..15 and rolls into a new block."

    The roll moves the counter only: the fifteenth tap stays undoable until the next block's
    first observation lands (decisions 174, 199).
    """
    if slot >= BLOCK_SIZE:
        return block_index + 1, 1
    return block_index, slot + 1


_SESSION_COLUMNS = (
    "id, user_id, kinds, mode, block_index, slot, seq, current_card, card_token"
)


def _session(row: asyncpg.Record) -> RateSession:
    card = row["current_card"]
    return RateSession(
        id=row["id"],
        user_id=row["user_id"],
        kinds=list(row["kinds"]),
        mode=row["mode"],
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
    """One live session per person (`rate_session_one_live`); `restart=True` ends it first."""
    wanted = normalise_kinds(kinds) if kinds else list(observations.KINDS)
    async with conn.transaction():
        if restart:
            await conn.execute(
                "UPDATE rate_session SET ended_at = now() "
                "WHERE user_id = $1 AND ended_at IS NULL",
                user_id,
            )
        # The conflict target names the partial index's own predicate.
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
) -> RateSession:
    """§6.1's two controls: the mode and the kind toggles. A change drops the card on the table."""
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    wanted = normalise_kinds(kinds) if kinds is not None else s.kinds
    redraw = (mode is not None and mode != s.mode) or wanted != s.kinds
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET mode = $2, kinds = $3::text[], last_seen_at = now(),
               current_card = CASE WHEN $4 THEN NULL ELSE current_card END,
               card_token   = CASE WHEN $4 THEN NULL ELSE card_token END
         WHERE id = $1
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        mode or s.mode,
        wanted,
        redraw,
    )
    return _session(row)


# --- the card cursor ---------------------------------------------------------------------


async def _observed_title_ids(
    conn: asyncpg.Connection, session_id: int, *, kinds_of: Sequence[str] | None = None
) -> list[int]:
    """What this sitting has already put in front of the person; Undo lifts the suppression.

    The sweep excludes everything; the battle pool excludes only skips, because Mix battles over
    the very verdicts the sweep half just collected.
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


async def _draw_pinned(
    conn: asyncpg.Connection, s: RateSession, *, served: Sequence[int], head: Sequence[int]
) -> dict[str, Any] | None:
    """A sweep card for the first drawable pinned title, or None.

    A pin lifts this sitting's suppression of the titles it names: the person asked for them again.
    """
    pinned = set(head)
    card = await _draw_sweep(
        conn, s, exclude=[t for t in served if t not in pinned], head=head
    )
    return card if card is not None and card["title_id"] in pinned else None


async def _admit_pinned_kinds(
    conn: asyncpg.Connection, s: RateSession, head: Sequence[int]
) -> RateSession:
    """A pin of a kind the session does not hold widens the session to hold it.

    Widening rather than serving across the partition keeps §4.1 rule 5; a rated title widens
    nothing.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT t.kind
          FROM title t
         WHERE t.id = ANY($1::int[])
           AND NOT EXISTS (
               SELECT 1 FROM verdict v
                WHERE v.user_id = $2 AND v.title_id = t.id AND NOT v.is_reask
           )
        """,
        [int(t) for t in head],
        s.user_id,
    )
    missing = [r["kind"] for r in rows if r["kind"] not in s.kinds]
    if not missing:
        return s
    return await set_controls(conn, s, kinds=[*s.kinds, *missing])


async def _live_labels(conn: asyncpg.Connection, s: RateSession) -> int:
    """The person's live ratings over the session's kinds: the class-balance widget's own total."""
    return (await balance.class_balance(conn, user_id=s.user_id, kinds=s.kinds)).total


async def _draw_battle(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    exclude: Sequence[int],
    rng: Any = None,
    labels: int | None = None,
) -> dict[str, Any] | None:
    pair = await battle.next_battle_pair(
        conn, user_id=s.user_id, kinds=s.kinds, exclude=tuple(exclude), rng=rng, labels=labels
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
        # Server-side only; see `public_card`.
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

    A battle slot with no drawable pair serves a sweep without changing the slot, so alternation
    resumes by itself. An explicit `head` the stashed card does not satisfy redraws once (§6.0's
    banner must serve the titles it names); a pin is also served on an empty table first.
    """
    head = tuple(int(t) for t in head)
    if head:
        s = await _admit_pinned_kinds(conn, s, head)
    served = await _observed_title_ids(conn, s.id)
    labels = await _live_labels(conn, s)
    wanted = warm_up(
        card_type_for(s.mode, observation_index(s.block_index, s.slot)), mode=s.mode, labels=labels
    )
    if s.current_card is not None:
        if not head or s.current_card.get("title_id") in head:
            return s
        replacement = await _draw_pinned(conn, s, served=served, head=head)
        if replacement is None:
            return s
        # The banner's redraw is a sweep whatever the counter called for: mark it.
        return await stash(
            conn, s, _mark_substitution(replacement, instead_of=wanted), expected=s.card_token
        )

    if head:
        pinned = await _draw_pinned(conn, s, served=served, head=head)
        if pinned is not None:
            marked = _mark_substitution(pinned, instead_of=wanted)
            return await stash(conn, s, marked, expected=None)

    skipped = await _skipped_title_ids(conn, s.id)
    card: dict[str, Any] | None = None
    if wanted == "battle":
        card = await _draw_battle(conn, s, exclude=skipped, rng=rng, labels=labels)
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
                await _draw_battle(conn, s, exclude=skipped, rng=rng, labels=labels),
                instead_of=wanted,
            )
    return await stash(conn, s, card, expected=None)


def _mark_substitution(
    card: dict[str, Any] | None, *, instead_of: CardType
) -> dict[str, Any] | None:
    """Mark a card the counter did not call for, wherever the type flips.

    The type and no cause: three sites flip for three different reasons. `serving` relies on it.
    """
    if card is not None and card["type"] != instead_of:
        card["substituted_for"] = instead_of
    return card


async def stash(
    conn: asyncpg.Connection,
    s: RateSession,
    card: dict[str, Any] | None,
    *,
    expected: uuid.UUID | None,
) -> RateSession:
    """Write the card under a fresh token if the stored token is still `expected` (None: an empty
    table), decision and write in one statement, so two GETs or two devices end up under one token.
    A replacement that missed stashes into a table since emptied; otherwise the card another
    request stashed first is served."""
    row = await conn.fetchrow(
        f"""
        UPDATE rate_session
           SET current_card = $2::jsonb, card_token = $3, last_seen_at = now()
         WHERE id = $1 AND card_token IS NOT DISTINCT FROM $4
        RETURNING {_SESSION_COLUMNS}
        """,
        s.id,
        card,
        uuid.uuid4() if card is not None else None,
        expected,
    )
    if row is None and expected is not None:
        return await stash(conn, s, card, expected=None)
    if row is None:
        row = await conn.fetchrow(f"SELECT {_SESSION_COLUMNS} FROM rate_session WHERE id = $1", s.id)
    return _session(row)


# `overview_is_mpst`: the overview IS the title's MPST synopsis, a full retelling.
_TITLE_CARDS = """
SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.overview,
       EXISTS (
           SELECT 1 FROM title_meta m
            WHERE m.title_id = t.id AND m.source = 'mpst'
              AND btrim(m.payload->>'plot_full') = btrim(t.overview)
       ) AS overview_is_mpst
  FROM title t
 WHERE t.id = ANY($1::int[])
"""


async def _title_cards(conn: asyncpg.Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """The poster-forward card of §6.8, and nothing else.

    No `ledger_state`, `user_score` or `title.placement`: the card must not anchor on the model.
    """
    rows = await conn.fetch(_TITLE_CARDS, list(ids))
    return {
        r["id"]: {
            "id": r["id"],
            "kind": r["kind"],
            "name": r["name"],
            "year": r["year"],
            "runtime_min": r["runtime_min"],
            "poster_path": r["poster_path"],
            # A recall aid, never an MPST synopsis: those retell the ending.
            "recall_aid": None if r["overview_is_mpst"] else _recall_aid(r["overview"]),
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

    Built from an allow-list: `reask_of` and the pair's verdict band must never reach the card.
    """
    card = s.current_card
    if card is None or s.card_token is None:
        return None
    token = str(s.card_token)
    if card["type"] == "sweep":
        titles = await _title_cards(conn, [card["title_id"]])
        public = {
            "type": "sweep",
            "token": token,
            "kind": card["kind"],
            "title": titles.get(card["title_id"]),
            "reason": card["reason"],
            # `source` stays server-side with `reask_of` (§13).
            "substituted_for": card.get("substituted_for"),
            # §6.8 / proposal 52: lowercase, worst -> best, matching the stored ordinal.
            "verdict_labels": [[i, label] for i, label in enumerate(VERDICT_LABELS)],
            "controls": ["verdict", "not_seen", "skip"],
        }
        # P(seen) travels under `model`, which `rail.redact` strips with Show the model off, and
        # only where the queue placed the card by it.
        if card.get("source") in ("seed", "p_seen") and card.get("p_seen") is not None:
            public["model"] = {"p_seen": round(float(card["p_seen"]), 2)}
        return public
    titles = await _title_cards(conn, [card["title_a"], card["title_b"]])
    # `left`/`right` match §6.1's corrections row; each poster carries its outcome letter.
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

# Proposal 153's suppressed reveal: no fit yet or no labels, one remedy.
NO_GUESS_YET = "no guess yet - rate a few more first"


async def _predicted_coordinate(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
) -> tuple[float, float, int, int] | None:
    """§5.2's zero-parameter prediction for a title with no `ledger_state` row of its own.

    `ledger_state` is sized to the owned library and the seed-list queue is not, so this computes
    `s = mu + <v, e>` from the cached fit. Unlocked: a read for one card, before the write.
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
        # The same refusal `refit` makes of a non-finite update.
        return None
    # The tier Home would show, against the stored cuts (decision 510).
    return s, cdf, int(model.tier_of(np.asarray([s]), cache.cuts)[0]), cache.n_levels


async def predicted_class(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
) -> dict[str, Any]:
    """What the model would have guessed, read BEFORE the write and served after it.

    The class the title's tier stands for, on the person's own cutpoints (decision 510). The
    stored row first, the cached fit second (see `_predicted_coordinate`).
    """
    row = await conn.fetchrow(
        "SELECT ls.s, ls.sigma, ls.cdf, ls.tier, "
        "       (SELECT cardinality(c.tier_set) FROM ledger_cutpoints c "
        "         WHERE c.user_id = ls.user_id AND c.kind = ls.kind) AS k "
        "  FROM ledger_state ls WHERE ls.user_id = $1 AND ls.title_id = $2",
        user_id,
        title_id,
    )
    if row is None or row["cdf"] is None or row["tier"] is None:
        coordinate = await _predicted_coordinate(
            conn, user_id=user_id, title_id=title_id, kind=kind, hp=hp, embeddings=embeddings
        )
        if coordinate is None:
            return {"available": False, "reason": NO_GUESS_YET}
        predicted_s, predicted_cdf, predicted_tier, levels = coordinate
    else:
        predicted_s, predicted_cdf = float(row["s"]), float(row["cdf"])
        predicted_tier = int(row["tier"])
        levels = int(row["k"] or len(observations.DEFAULT_TIER_SET))
    total = (await balance.class_balance(conn, user_id=user_id, kinds=[kind])).total
    if total == 0:
        return {"available": False, "reason": NO_GUESS_YET}

    cdf = predicted_cdf
    # Decision 510: the class the tier letter stands for, so the reveal matches the badge on Home.
    guess = model.verdict_class_of_tier(predicted_tier, levels)
    return {
        "available": True,
        "predicted": guess,
        "predicted_label": VERDICT_LABELS[guess],
        "cdf": cdf,
        "s": predicted_s,
        "label_count": total,
    }


def reveal_for(prediction: dict[str, Any], value: int) -> dict[str, Any]:
    """§6.1's phrasing: "we'd have guessed the same" / "we'd have guessed {class}"; no number."""
    if not prediction.get("available"):
        return dict(prediction)
    agreed = prediction["predicted"] == value
    head = "we'd have guessed the same" if agreed else (
        f"we'd have guessed {prediction['predicted_label']}"
    )
    return {**prediction, "agreed": agreed, "text": head}


# The reveal's model quantities: the displayed weight, the coordinate and the band's support.
_REVEAL_NUMBERS = ("cdf", "s", "label_count")


def viewer_reveal(reveal: dict[str, Any] | None, *, show_model: bool) -> dict[str, Any] | None:
    """The reveal as one viewer may see it: with Show the model off, no model number is sent."""
    if reveal is None or not reveal.get("available"):
        return reveal
    if not show_model:
        return {key: value for key, value in reveal.items() if key not in _REVEAL_NUMBERS}
    return {**reveal, "text": f"{reveal['text']} · cdf {reveal['cdf']:.2f}"}


# --- §7.3's push, and its symmetric retraction ----------------------------------------------


async def _push_state(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    title_id: int,
) -> tuple[bool, str | None]:
    """Push the committed state to Jellyfin now; callers run this after their transaction closes.

    With no connector the §7.3 debt simply stands.
    """
    if jf is None or jf.client is None:
        # §6.7 prints this verbatim, so an unreadable DEK must not read as "not configured".
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
    """Decision 207: correct the journal's `pushed` flags once the push has resolved.

    Best-effort and outside the transaction. If the row was undone mid-push the UPDATE matches
    nothing, and the tap makes the retraction the undo could not.
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
        # Logged, not raised: the observation is durable and there is nothing to retry.
        log.warning(
            "observation %d/%d was undone mid-push and its Played flag could not be retracted: %s",
            session_id,
            seq,
            exc,
        )


# One tap's §7.3 settlement as a job over a connection, and the hook a caller passes to have it
# run after the response instead of inside it.
Settle = Callable[[asyncpg.Connection], Awaitable[Any]]
Later = Callable[[Settle], None]

_SETTLING: set[asyncio.Task[None]] = set()

# Unbounded, a slow Jellyfin let one phone's pushes take the whole web pool; two leave eight.
SETTLE_SLOTS = 2
# Bounded like a request's acquire; a settlement that gets none stays owed to §7.3's sweep.
SETTLE_ACQUIRE_TIMEOUT_S = 10

_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _settle_slots() -> asyncio.Semaphore:
    """The one semaphore for this event loop, made per loop because a semaphore binds to one."""
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(SETTLE_SLOTS))
    return _slots[1]


async def _push_and_mark(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    user_id: int,
    session_id: int,
    seq: int,
    writes: Sequence[observations.Write],
    title_ids: Sequence[int],
) -> dict[int, tuple[bool, str | None]]:
    """Push each touched title's committed state, then correct the journal's `pushed` flags."""
    results: dict[int, tuple[bool, str | None]] = {}
    for title_id in title_ids:
        results[title_id] = await _push_state(conn, jf, user_id=user_id, title_id=title_id)
    pushed = {title_id: ok for title_id, (ok, _reason) in results.items()}
    await _mark_pushed(
        conn,
        jf,
        user_id=user_id,
        session_id=session_id,
        seq=seq,
        entries=[e for w in writes for e in _state_entries(w, pushed)],
    )
    return results


async def _push_and_narrate(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    *,
    state: str,
    event_kind: str,
    user_id: int,
    session_id: int,
    seq: int,
    writes: Sequence[observations.Write],
    title_ids: Sequence[int],
) -> None:
    """A handed-off push, and §6.7's line for how it ended, recorded when it ends."""
    results = await _push_and_mark(
        conn, jf, user_id=user_id, session_id=session_id, seq=seq, writes=writes,
        title_ids=title_ids,
    )
    for title_id in title_ids:
        rail.record(kind=event_kind, line=_sync_line(state, *results[title_id]), user_id=user_id)


async def _settle_push(
    conn: asyncpg.Connection,
    jf: Jellyfin | None,
    later: Later | None,
    *,
    state: str,
    event_kind: str,
    user_id: int,
    session_id: int,
    seq: int,
    writes: Sequence[observations.Write],
    title_ids: Sequence[int],
) -> list[str]:
    """§7.3's push for one tap, now or after the response; returns §6.7's line per title.

    With `later` and a client, the push runs on its own connection after the response (a series
    Played write can take seconds); with no client, inline, since `_push_state` needs no query.
    """
    writes, title_ids = tuple(writes), tuple(title_ids)
    if later is not None and jf is not None and jf.client is not None:
        later(
            functools.partial(
                _push_and_narrate, jf=jf, state=state, event_kind=event_kind, user_id=user_id,
                session_id=session_id, seq=seq, writes=writes, title_ids=title_ids,
            )
        )
        return [_follows_line(state) for _ in title_ids]
    results = await _push_and_mark(
        conn, jf, user_id=user_id, session_id=session_id, seq=seq, writes=writes,
        title_ids=title_ids,
    )
    return [_sync_line(state, *results[title_id]) for title_id in title_ids]


def settle_in_background(job: Settle) -> None:
    """Run one tap's §7.3 settlement after the response, on a pooled connection of its own.

    No timeout of its own: a cancelled push may have reached Jellyfin while the journal says not.
    """
    task = asyncio.get_running_loop().create_task(_run_settlement(job))
    _SETTLING.add(task)
    task.add_done_callback(_SETTLING.discard)


async def _run_settlement(job: Settle) -> None:
    # Nothing may raise out of a bare task; the app-side write is durable either way.
    try:
        async with _settle_slots():
            connections = db_pool.pool()
            try:
                conn = await connections.acquire(timeout=SETTLE_ACQUIRE_TIMEOUT_S)
            except TimeoutError:
                log.warning(
                    "no pooled connection within %ss for a Rate answer's Jellyfin push (pool size"
                    " %d, idle %d); the seen-state sweep still owes it",
                    SETTLE_ACQUIRE_TIMEOUT_S,
                    connections.get_size(),
                    connections.get_idle_size(),
                )
                return
            try:
                await job(conn)
            finally:
                await connections.release(conn)
    except Exception:
        log.warning(
            "a Rate answer's Jellyfin push did not settle; the seen-state sweep still owes it",
            exc_info=True,
        )


async def settled(timeout: float | None = None) -> None:
    """Wait for every settlement handed off so far. For tests and shutdown, never a request.

    With `timeout`, stop waiting and cancel nothing (see `settle_in_background`).
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while _SETTLING:
        left = None if deadline is None else deadline - loop.time()
        if left is not None and left <= 0:
            return
        await asyncio.wait(list(_SETTLING), timeout=left)


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
    """One journal row, then the cursor moves (decision 35's observation journal).

    A correction does not advance (`rate_observation_advances_rule`). The INSERT and UPDATE are
    atomic here too: split, they leave the journal a row ahead and every later append refused.
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
            # Two taps on one card: §6.1's stale card, the 409 the client can act on.
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

    Two taps on one token both pass `_take_card`'s snapshot check; the loser blocks here, re-reads
    a NULL token and leaves as §6.1's 409 before any write or push. Not taken on the GET.
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
    """§6.7's commonest line, composed here where the title, label and refit time are all in hand.

    Deliberately not `ledger/observations.py`'s audit sentence: §6.8 wants names, not ids.
    """
    row = await conn.fetchrow(
        "SELECT (SELECT name FROM app_user WHERE id = $1) AS rater,"
        " (SELECT name FROM title WHERE id = $2) AS title",
        user_id,
        title_id,
    )
    # Both exist by foreign key; the fallbacks cover a database that broke its constraints.
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
    later: Later | None = None,
) -> Outcome:
    """§6.1's `Liked / Fine / Disliked`, and "Verdict implies `seen`".

    `bundle_version` names the basis `embeddings` is in, so `refit.load_cache` can check it.
    `later` takes §7.3's push off the response (`_settle_push`).
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
            # `pushed` is corrected by `_mark_pushed` once the push has happened.
            prior_state=_state_entries(write, {}),
            latency_ms=latency_ms,
        )

    # §7.3's push after the commit, where the verdict made the title seen: the row then stands as
    # owed (`jf_synced_at` NULL).
    sync_lines: list[str] = []
    if write.implied_seen:
        sync_lines = await _settle_push(
            conn, jf, later, state="seen", event_kind="verdict", user_id=s.user_id,
            session_id=s.id, seq=s.seq, writes=(write,), title_ids=(title_id,),
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
    # Replaces `write.log` on this arm, so one write is narrated once.
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
        log=(line, *sync_lines),
        ledger=ledger,
    )


def _sync_line(state: str, pushed: bool, reason: str | None) -> str:
    """§6.7's rail reports what actually happened, never a write that did not happen."""
    played = "true" if state == "seen" else "false"
    # Settled with a reason is a series stamped app-only (decision 533): nothing reached Jellyfin.
    if pushed and reason is None:
        return f"user_title.state = {state} -> Jellyfin Played {played}"
    return f"user_title.state = {state} -> not pushed ({reason or 'no connector'})"


def _follows_line(state: str) -> str:
    """The line for a push handed to `later`: it has not happened yet, so it says it follows."""
    return f"user_title.state = {state} -> Jellyfin push follows"


async def record_not_seen(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
    later: Later | None = None,
) -> Outcome:
    """§6.1's `Not seen`: plain `unseen` (no third state); writes no observation row, deletes none."""
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
    sync_lines = await _settle_push(
        conn, jf, later, state="unseen", event_kind="not_seen", user_id=s.user_id,
        session_id=s.id, seq=s.seq, writes=(write,), title_ids=(title_id,),
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(session=s, log=(write.log, *sync_lines))


async def record_skip(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """`Skip` writes to no arm; its journal row suppresses the card's titles for the sitting."""
    if s.current_card is None or s.card_token is None:
        raise StaleCard("no_card")
    if str(s.card_token) != str(card_token):
        raise StaleCard("stale_card")
    card = s.current_card
    titles = (
        [card["title_id"]] if card["type"] == "sweep" else [card["title_a"], card["title_b"]]
    )
    # In a transaction like its siblings; see `_append`.
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
    decisive: bool = False,
    embeddings: EmbeddingSource | None = None,
    bundle_version: Any = refit.BASIS_UNSTATED,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """§6.1's battle answer: exactly one duel row, context `profile_battle`; a TIE is data.

    "Much more" is `decisive`, weighted by `hp.margin_for`; a TIE never is (decision 528).
    """
    card = _take_card(s, card_token, want="battle")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")

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
            decisive=decisive and outcome != "TIE",
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
    later: Later | None = None,
) -> Outcome:
    """§6.1's corrections zone: `not seen: [left] [both] [right]`, writing no duel row.

    It does not advance (`rate_observation_advances_rule`). History is untouched; the title
    leaves the pool because the pool needs "marked seen".
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
        s = await stash(conn, s, replacement, expected=s.card_token)

    lines.extend(
        await _settle_push(
            conn, jf, later, state="unseen", event_kind="not_seen", user_id=s.user_id,
            session_id=s.id, seq=s.seq, writes=writes, title_ids=corrected,
        )
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
    """Keep the half the person did not correct, against a fresh opponent from its own band.

    Drawn from the band directly and uniformly (§0 row 6): a whole-pair draw rarely hits a small
    band.
    """
    survivor = next((t for t in (card["title_a"], card["title_b"]) if t not in corrected), None)
    exclude = set(await _skipped_title_ids(conn, s.id)) | set(corrected)
    if survivor is None:
        pair = await _draw_battle(conn, s, exclude=sorted(exclude), rng=rng)
        if pair is not None:
            return pair
        # Nothing to keep and no pair to draw: a marked sweep, as the counter wanted a battle.
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
    # `battle.draw`'s no-repeat rule holds for the repaired pair too.
    answered = await battle.answered_pairs(conn, user_id=s.user_id, kinds=[card["kind"]])
    band = sorted(
        m.title_id
        for m in pool
        if m.verdict_class == card["verdict_class"]
        and frozenset((survivor, m.title_id)) not in answered
    )
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
    # The survivor's band is empty: fall back to a marked sweep, as `ensure_card` does.
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
    """Decision 35's depth with decision 199's boundary: is this journal row still undoable?

    The fifteenth row of the previous block stays reachable at slot 1 until the new block has
    ever held a row, tombstones included; otherwise undo would walk back block by block.
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

    Decision 35: any kind, the exact card (`rate_observation.card`), one block (`_undo_reaches`).
    Compared on the row's block and slot, not a timestamp: the journal row lands after the ledger's.
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
            # The only code permitted to delete a verdict or duel row (§4.2 is append-only).
            # `not_seen` covers the corrections row too: both restore `user_title` exactly.
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
    """One envelope for every route; the next card travels in the response to the write (§6)."""
    # §6.7's rail narrates model writes only, so a read or a skip passes no `event_kind`.
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
            "block": {
                "index": s.block_index,
                "slot": s.slot,
                "size": BLOCK_SIZE,
                # §6.1's counter, and the unit decision 35's Undo depth is measured in.
                "counter": f"{s.slot} of {BLOCK_SIZE}",
                # What the counter calls for, which a substitution does not move (the card says
                # `substituted_for`); includes decision 492's warm-up.
                "serving": warm_up(
                    card_type_for(s.mode, observation_index(s.block_index, s.slot)),
                    mode=s.mode,
                    labels=shares.total,
                ),
            },
        },
        "card": card,
        # Keyed by cause: one of the three is not a drained queue at all.
        "drained": None if card else drained_for(s.mode),
        # §5.2's measured 5x lever, rendered by `balance`'s own projection.
        "class_balance": shares.as_dict(),
        "undo": await undo_availability(conn, s),
        "reveal": reveal if user is None else viewer_reveal(
            reveal, show_model=rail.visible_to(user)
        ),
        "ledger": ledger,
        # §6.7's rail, also carried here so the client shows this tap's line without a request.
        "log": list(log),
    }
    # Decision 117: with the toggle off the numbers are absent, not hidden; same gate as Home.
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
    "viewer_reveal",
    "warm_up",
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
    "settle_in_background",
    "settled",
    "undo",
    "undo_availability",
]
