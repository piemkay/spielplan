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
  * **The card type is a function of the slot, never of the last card served.** §6.1's Mix
    "alternates sweep and battle", and the counter is what alternates. Deriving the next type
    from what was last *answered* is the bug this module exists not to have: a run of duels
    then never returns a sweep card.

What this module is not: it draws no cards itself. `rate.queue`, `rate.battle` and
`rate.balance` own what to ask; this owns when, in what order, under which token, and how to
take it back.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError
from spielplan.connectors.registry import JellyfinConfig
from spielplan.db.library import normalise_kinds
from spielplan.home import rail
from spielplan.ledger import observations, refit
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


DRAINED = {
    "text": (
        "You've rated everything we can queue right now. Battles sharpen what you've "
        "already said."
    )
}


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


@dataclass(frozen=True)
class Outcome:
    """One tap's result: the session as it now stands, and everything the response carries."""

    session: RateSession
    reveal: dict[str, Any] | None = None
    log: tuple[str, ...] = ()
    ledger: dict[str, Any] | None = None
    undone: str | None = None


# --- the block machine -------------------------------------------------------------------


def card_type_for(mode: str, slot: int) -> CardType:
    """§6.1: "Mix (default — alternates sweep and battle); blocks of 15."

    A pure function of the SLOT. Slot 1 is a sweep, so Mix opens on a card that needs no prior
    ratings to answer. Sweep and Battle modes serve only their own type; the slot still
    advances, so switching modes mid-block does not restart the counter.
    """
    if mode == "sweep":
        return "sweep"
    if mode == "battle":
        return "battle"
    return "sweep" if slot % 2 == 1 else "battle"


def advance(block_index: int, slot: int) -> tuple[int, int]:
    """§6.1: "the counter runs 1..15 and rolls into a new block."

    Rolling *is* decision 35's commit: the instant the 15th observation lands, the block index
    moves and everything in the old block stops being undoable. Stated as arithmetic rather
    than as a separate rule, so the two cannot disagree.
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
    if s.current_card is not None:
        if not head or s.current_card.get("title_id") in tuple(head):
            return s
        replacement = await _draw_sweep(conn, s, exclude=served, head=head)
        if replacement is None or replacement["title_id"] not in tuple(head):
            return s
        return await stash_card(conn, s, replacement)

    skipped = await _skipped_title_ids(conn, s.id)
    wanted = card_type_for(s.mode, s.slot)
    card: dict[str, Any] | None = None
    if wanted == "battle":
        card = await _draw_battle(conn, s, exclude=skipped, rng=rng)
        if card is None and s.mode != "battle":
            card = await _draw_sweep(conn, s, exclude=served, head=head)
            if card is not None:
                card["substituted_for"] = "battle"
    else:
        card = await _draw_sweep(conn, s, exclude=served, head=head)
        if card is None and s.mode != "sweep":
            # §6.1's drained state: the queue is spent but the ratings already given can still
            # be sharpened against each other.
            card = await _draw_battle(conn, s, exclude=skipped, rng=rng)
            if card is not None:
                card["substituted_for"] = "sweep"
    return await stash_card(conn, s, card)


async def stash_card(
    conn: asyncpg.Connection, s: RateSession, card: dict[str, Any] | None
) -> RateSession:
    """Write the card and a fresh token in one statement.

    `rate_session_card_has_token` makes "a card with no token" unrepresentable; issuing the
    token here rather than at the route is what makes that CHECK an invariant instead of a
    reminder.
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


async def predicted_class(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, kind: str
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
    """
    row = await conn.fetchrow(
        "SELECT s, sigma, cdf, tier FROM ledger_state WHERE user_id = $1 AND title_id = $2",
        user_id,
        title_id,
    )
    if row is None or row["cdf"] is None:
        return {
            "available": False,
            "reason": "no fitted ranking for this title yet — rate a few more first",
        }
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

    cdf = float(row["cdf"])
    low = counts[0] / total
    high = (counts[0] + counts[1]) / total
    guess = 0 if cdf < low else (1 if cdf < high else 2)
    return {
        "available": True,
        "predicted": guess,
        "predicted_label": VERDICT_LABELS[guess],
        "cdf": cdf,
        "s": float(row["s"]),
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
    state: str,
) -> tuple[bool, str | None]:
    """Settle §7.3's debt now rather than in fifteen minutes.

    `observations.record_*` has already made the app-side write and left `jf_synced_at` NULL,
    which is precisely "the person acted and Jellyfin has not been told yet" — the sweep would
    push it eventually. Calling M1's own `seen.set_state` here rewrites the same value (it is
    idempotent) and pushes, so the person sees their media server agree while the card is
    still on screen. With no connector the debt simply stands.
    """
    if jf is None or jf.client is None:
        return False, "Jellyfin not configured"
    result = await seen.set_state(
        conn, jf.client, jf.cfg, user_id=user_id, title_id=title_id, state=state
    )
    return bool(result["synced"]), result["reason"]


async def _compensate_push(
    conn: asyncpg.Connection, jf: Jellyfin | None, *, user_id: int, prior: PriorState
) -> None:
    """Undo pushes back exactly what the forward action pushed, and only that.

    `observations.undo` has already restored `user_title` byte for byte, including §7.3's
    `jf_synced_at` loop guard, so this must not go through `seen.set_state`: that would clear
    the restored stamp and, where the prior state was *no row at all*, invent an explicit
    `unseen` assertion out of an absence (§7.3: "an absent row is the *default*, not an
    assertion"). What is left is the Played flag itself, which we did write and which Jellyfin
    would otherwise hand back on the next sweep as history.
    """
    if jf is None or jf.client is None:
        return
    jellyfin_id = await conn.fetchval(
        "SELECT jellyfin_id FROM title WHERE id = $1", prior.title_id
    )
    if not jellyfin_id:
        return
    users = {u.app_user_id: u for u in await seen.linked_users(conn, jf.cfg)}
    user = users.get(user_id)
    if user is None or not user.token:
        return
    played = prior.existed and prior.state == "seen"
    try:
        await jf.client.set_played(jellyfin_id, user.jf_user_id, played, user.token)
    except JellyfinError as exc:
        # The retraction is owed, not lost: the app-side row is already correct, and §7.3's
        # next sweep reconciles from it wherever a row exists to reconcile from.
        log.warning("compensating Played write for title %d failed: %s", prior.title_id, exc)
        return
    if prior.existed:
        await conn.execute(
            "UPDATE user_title SET jf_synced_at = now() WHERE user_id = $1 AND title_id = $2",
            user_id,
            prior.title_id,
        )


def _state_entries(
    write: observations.Write, pushed: dict[int, bool]
) -> list[dict[str, Any]]:
    """`rate_observation.prior_state`: what `user_title` held, plus whether we reached
    Jellyfin. Undo compensates what it did, not what it intended."""
    return [
        {**p.as_dict(), "pushed": bool(pushed.get(p.title_id, False))}
        for p in write.prior_state
    ]


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
    """
    advances = kind_of != "correction"
    block_index, slot = (
        advance(s.block_index, s.slot) if advances else (s.block_index, s.slot)
    )
    await conn.execute(
        """
        INSERT INTO rate_observation
            (session_id, user_id, seq, block_index, slot, kind_of, advances, card, title_ids,
             verdict_id, duel_id, superseded_verdict_id, prior_state, latency_ms)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::int[], $10, $11, $12, $13::jsonb, $14)
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


def _take_card(s: RateSession, token: str, *, want: CardType) -> dict[str, Any]:
    if s.current_card is None or s.card_token is None:
        raise StaleCard("no_card")
    if str(s.card_token) != str(token):
        raise StaleCard("stale_card")
    if s.current_card["type"] != want:
        raise StaleCard("wrong_card_type")
    return s.current_card


async def _ledger_update(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    title_ids: Sequence[int],
    hp: Hyperparams,
    embeddings: EmbeddingSource | None,
) -> dict[str, Any] | None:
    """§5.3's "<50 ms" row, run after the person's write has committed.

    A fit that refuses is a model problem, not a reason to lose the tap: the observation is
    already durable and the nightly refit will pick it up, so this reports rather than raises.
    """
    try:
        delta = await refit.update_incrementally(
            conn,
            user_id=user_id,
            kind=kind,
            title_ids=list(title_ids),
            hp=hp,
            embeddings=embeddings,
        )
    except (refit.RefitRefused, ValueError) as exc:
        log.warning("incremental update refused for user %d/%s: %s", user_id, kind, exc)
        return {"applied": False, "reason": str(exc)}
    return {
        "applied": True,
        "kind": delta.kind,
        "refit": delta.refit,
        "ms": round(delta.micros / 1000.0, 1),
        "rows": [{"title_id": r.title_id, "cdf": r.cdf, "tier": r.tier} for r in delta.rows],
    }


# --- the five taps ---------------------------------------------------------------------------


async def record_verdict(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    card_token: str,
    value: int,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
    jf: Jellyfin | None = None,
    latency_ms: int | None = None,
    rng: Any = None,
    head: Sequence[int] = (),
) -> Outcome:
    """§6.1's `Liked / Fine / Disliked`, and "Verdict implies `seen`"."""
    card = _take_card(s, card_token, want="sweep")
    if value not in (0, 1, 2):
        raise ValueError(f"verdict value must be 0, 1 or 2, not {value!r}")
    title_id = card["title_id"]

    # Strictly first: the reveal is what the model believed *before* this label existed.
    prediction = await predicted_class(
        conn, user_id=s.user_id, title_id=title_id, kind=card["kind"]
    )

    async with conn.transaction():
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
        pushed, reason = await _push_state(
            conn, jf, user_id=s.user_id, title_id=title_id, state="seen"
        )
        s = await _append(
            conn,
            s,
            kind_of="verdict",
            card=card,
            title_ids=write.title_ids,
            verdict_id=write.row_id,
            superseded_verdict_id=write.superseded_id,
            prior_state=_state_entries(write, {title_id: pushed}),
            latency_ms=latency_ms,
        )

    ledger = await _ledger_update(
        conn,
        user_id=s.user_id,
        kind=write.kind,
        title_ids=write.title_ids,
        hp=hp,
        embeddings=embeddings,
    )
    s = await ensure_card(conn, s, rng=rng, head=head)
    return Outcome(
        session=s,
        reveal=reveal_for(prediction, value),
        log=(write.log, _sync_line("seen", pushed, reason)),
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
        write = await observations.record_not_seen(
            conn, user_id=s.user_id, title_id=title_id
        )
        pushed, reason = await _push_state(
            conn, jf, user_id=s.user_id, title_id=title_id, state="unseen"
        )
        s = await _append(
            conn,
            s,
            kind_of="not_seen",
            card=card,
            title_ids=write.title_ids,
            prior_state=_state_entries(write, {title_id: pushed}),
            latency_ms=latency_ms,
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

    ledger = await _ledger_update(
        conn,
        user_id=s.user_id,
        kind=write.kind,
        title_ids=write.title_ids,
        hp=hp,
        embeddings=embeddings,
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

    entries: list[dict[str, Any]] = []
    lines: list[str] = []
    async with conn.transaction():
        for title_id in corrected:
            write = await observations.record_not_seen(
                conn, user_id=s.user_id, title_id=title_id
            )
            pushed, reason = await _push_state(
                conn, jf, user_id=s.user_id, title_id=title_id, state="unseen"
            )
            entries.extend(_state_entries(write, {title_id: pushed}))
            lines.append(_sync_line("unseen", pushed, reason))
        s = await _append(
            conn,
            s,
            kind_of="correction",
            card=card,
            title_ids=corrected,
            prior_state=entries,
        )
        replacement = await _redraw_pair(conn, s, card, corrected=corrected, rng=rng)
        s = await stash_card(conn, s, replacement)

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
        return await _draw_battle(conn, s, exclude=sorted(exclude), rng=rng) or await _draw_sweep(
            conn, s, exclude=sorted(await _observed_title_ids(conn, s.id)), head=()
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
    # falls back the same way `ensure_card` does when the pool is thin.
    return await _draw_sweep(
        conn, s, exclude=sorted(set(await _observed_title_ids(conn, s.id)) | set(corrected)), head=()
    )


# --- undo ------------------------------------------------------------------------------------


async def undo_availability(conn: asyncpg.Connection, s: RateSession) -> dict[str, Any]:
    """Decision 35: "the chip disables visibly at the boundary"."""
    row = await conn.fetchrow(
        "SELECT kind_of, block_index FROM rate_observation "
        "WHERE session_id = $1 AND undone_at IS NULL ORDER BY seq DESC LIMIT 1",
        s.id,
    )
    if row is None:
        return {"available": False, "kind": None, "reason": "empty"}
    if row["block_index"] != s.block_index:
        return {"available": False, "kind": None, "reason": "block_boundary"}
    return {"available": True, "kind": row["kind_of"], "reason": None}


async def undo(
    conn: asyncpg.Connection,
    s: RateSession,
    *,
    hp: Hyperparams,
    embeddings: EmbeddingSource | None = None,
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
      * **One block.** The journal row's own `block_index` is the test. It is compared here
        rather than passed to `observations.undo` as a `block_started_at` timestamp, because
        the journal row is written *after* the ledger row it describes — a timestamp
        comparison would refuse the first observation of every block by a few microseconds.
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
        if row["block_index"] != s.block_index:
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
        if pushed.get(prior.title_id):
            await _compensate_push(conn, jf, user_id=s.user_id, prior=prior)

    ledger = None
    if undone is not None and arm in ("verdict", "duel"):
        ledger = await _ledger_update(
            conn,
            user_id=s.user_id,
            kind=undone.kind,
            title_ids=title_ids,
            hp=hp,
            embeddings=embeddings,
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
    if event_kind is not None:
        for line in log:
            rail.record(kind=event_kind, line=line, user_id=s.user_id)

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
                "serving": card_type_for(s.mode, s.slot),
            },
        },
        "card": card,
        "drained": None if card else DRAINED,
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
    "MODES",
    "OUTCOMES",
    "Jellyfin",
    "Outcome",
    "RateSession",
    "StaleCard",
    "UndoUnavailable",
    "advance",
    "card_type_for",
    "end_session",
    "ensure_card",
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
