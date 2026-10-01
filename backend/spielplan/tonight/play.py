"""The round's write path: start, serve, answer, retract, escape, finish (§6.2 steps 3-6).

The pool, and each seat's films and reach, are snapshot at start, so nothing re-ranks within the
evening. Every answer names a sealed pair, so the client cannot choose its stream (§13). `progress()`
selects counts, never titles.
"""

from __future__ import annotations

import json
import random
import secrets
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import numpy as np

from spielplan.db import dna_terms
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import copy as copy_rules
from spielplan.tonight import dna as dna_reads
from spielplan.tonight import pool as pool_rules
from spielplan.tonight import rooms
from spielplan.tonight import round as round_rules
from spielplan.tonight import tilt as tilt_rules

# Advisory-lock namespace for `finish`; two ints so a session id cannot collide with other locks.
_FINISH_LOCK = 6202


class RoundError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SeatRound:
    """One seat's frozen round: the films it may be asked about, each film's card, the candidates
    its mood may re-rank, and why it has no round when it has none."""

    films: tuple[round_rules.Film, ...] = ()
    cards: dict[int, dict[str, Any]] = field(default_factory=dict)
    reach: tuple[int, ...] = ()
    no_round: str | None = None


@dataclass(frozen=True, eq=False)
class Snapshot:
    """The frozen evening: which titles, what each seat scores them, their DNA, and each seat's round."""

    candidates: dict[int, dict[str, Any]]
    scores: dict[int, dict[int, float]]      # title_id -> {participant_id: §5.1 score}
    dna: dict[int, dict[str, float]]
    version: str | None
    # §13's hold-out draw, sealed with the pool (decision 223).
    holdout_seed: str | None = None
    # The scale rule, frozen with the pool so a deploy cannot move an evening in flight (decision 477).
    scale: str | None = None
    # Derived from `scores` under `scale` in `_as_snapshot`, never stored.
    ledger: dict[int, dict[int, float]] = field(default_factory=dict)
    rounds: dict[int, SeatRound] = field(default_factory=dict)
    # Where each candidate within some seat's reach sits on the mood directions.
    z: dict[int, tuple[float, ...]] = field(default_factory=dict)
    # The directions as term weights, so a mood can be written back as terms (`tilt.back`).
    mood_terms: tuple[str, ...] = ()
    mood_axes: np.ndarray = field(default_factory=lambda: np.zeros((0, round_rules.MOOD_DIRECTIONS)))

    @property
    def title_ids(self) -> list[int]:
        return list(self.candidates)

    def round_of(self, participant_id: int) -> SeatRound:
        """A seat seated after the start has nothing frozen, so no round."""
        return self.rounds.get(participant_id, SeatRound())

    def stable_for(self, participant_id: int, *, guest: bool) -> dict[int, float]:
        """A seat's stable taste over the pool: its own Ledger, or for a guest the pool's own order."""
        return self.member_average() if guest else self.pool_scores_for(participant_id)

    def reach_of(self, participant_id: int) -> dict[int, tuple[float, ...]]:
        return {t: self.z[t] for t in self.round_of(participant_id).reach if t in self.z}

    def tilt(self, mean: np.ndarray) -> dict[str, float]:
        return tilt_rules.back(self.mood_terms, self.mood_axes, mean)

    def pool_scores_for(self, participant_id: int) -> dict[int, float]:
        """One seat's Ledger over the pool, on the room's scale; empty for a guest (54c).

        Rank-standardised so neither member's Ledger outvotes the other by its units (decision 477).
        """
        return {
            t: seat_scores[participant_id]
            for t, seat_scores in self.ledger.items()
            if participant_id in seat_scores
        }

    def member_average(self) -> dict[int, float]:
        """The pool's own order (§6.2 step 3), a profile-less guest's prior, on the room's scale.

        Never a member's Ledger wearing the guest's name.
        """
        return {t: pool_rules.group_score(s) for t, s in self.ledger.items()}

    def member_ledger(self) -> dict[int, list[float]]:
        """D's input: {title_id: [each seated member's score]}, on the room's scale — the scale
        `combine.D_THRESHOLD` was recalibrated on (decision 478)."""
        return {t: list(s.values()) for t, s in self.ledger.items()}


def _on_scale(scores: dict[int, dict[int, float]]) -> dict[int, dict[int, float]]:
    """`scores` as the room's scale reads them: each member's ranks as normal quantiles (decision 477)."""
    by_member: dict[int, dict[int, float]] = {}
    for t, seats in scores.items():
        for p, v in seats.items():
            by_member.setdefault(p, {})[t] = v
    out: dict[int, dict[int, float]] = {t: {} for t in scores}
    for p, own in by_member.items():
        for t, v in pool_rules.rank_normal(own).items():
            out[t][p] = v
    return out


# What a pair card shows of a film: never its step, and no score.
_CARD = ("title_id", "name", "year", "kind", "runtime_min", "poster_path", "genres")


def _seat_round(raw: Mapping[str, Any]) -> SeatRound:
    films = raw.get("films") or []
    return SeatRound(
        films=tuple(
            round_rules.Film(
                title_id=int(f["title_id"]), step=f["step"], runtime_min=f["runtime_min"],
                z=tuple(float(v) for v in f["z"]),
            )
            for f in films
        ),
        cards={int(f["title_id"]): {k: f[k] for k in _CARD} for f in films},
        reach=tuple(int(t) for t in raw.get("reach") or ()),
        no_round=raw.get("no_round"),
    )


def _as_snapshot(context: Any) -> Snapshot:
    """The frozen evening; a room started under another round or scale is refused, never guessed at."""
    ctx = context if isinstance(context, dict) else json.loads(context or "{}")
    raw = ctx.get("pool") or {}
    if raw.get("round") != round_rules.ROUND_MARKER or raw.get("scale") != pool_rules.SCALE_MARKER:
        raise RoundError(
            "no_room", "this room was started by a different version of Spielplan - start a new one"
        )
    scores = {
        int(t): {int(p): float(v) for p, v in seats.items()}
        for t, seats in (raw.get("scores") or {}).items()
    }
    mood = raw.get("mood") or {}
    axes = np.asarray(mood.get("axes") or [], dtype=float)
    return Snapshot(
        candidates={int(k): v for k, v in (raw.get("candidates") or {}).items()},
        scores=scores,
        dna={int(k): {t: float(w) for t, w in v.items()} for k, v in (raw.get("dna") or {}).items()},
        version=raw.get("version"),
        holdout_seed=raw.get("holdout_seed"),
        scale=raw["scale"],
        ledger=_on_scale(scores),
        rounds={int(p): _seat_round(v) for p, v in (raw.get("seats") or {}).items()},
        z={int(t): tuple(float(x) for x in v) for t, v in (raw.get("z") or {}).items()},
        mood_terms=tuple(mood.get("terms") or ()),
        mood_axes=axes.reshape(-1, round_rules.MOOD_DIRECTIONS),
    )


async def snapshot_of(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    context = await conn.fetchval("SELECT context FROM session WHERE id = $1", session_id)
    if context is None:
        raise RoundError("no_room", "no such session")
    return _as_snapshot(context)


async def _refuse_unscored_members(
    conn: asyncpg.Connection, session_id: int, *, bundle_version: str
) -> None:
    """Name the members with no scores for this bundle, rather than refusing with `empty_pool`.

    Any score for the bundle, not one of this kind: a library with no series is not a member's
    fault. Guests are excluded by role (decision 216).
    """
    missing = await conn.fetch(
        """
        SELECT u.name
          FROM session_participant p JOIN app_user u ON u.id = p.user_id
         WHERE p.session_id = $1 AND p.role <> $2
           AND NOT EXISTS (
                SELECT 1 FROM user_score us
                 WHERE us.user_id = p.user_id AND us.bundle_version = $3
           )
         ORDER BY p.seat
        """,
        session_id, rooms.ROLE_GUEST, bundle_version,
    )
    if not missing:
        return
    who = ", ".join(r["name"] for r in missing)
    # No model nouns (decision 486); the fold-in tick writes scores every minute.
    raise RoundError(
        "unscored_member",
        f"{who} has no scores yet — Tonight ranks from every member's scores, and new ones "
        "arrive within a couple of minutes, so try Start again shortly",
    )


async def _frozen_rounds(
    conn: asyncpg.Connection,
    *,
    seats: Sequence[pool_rules.Seat],
    kind: str,
    bundle_version: str,
    vetoed_terms: Sequence[str],
    version: str | None,
    space: tilt_rules.Space,
    ledger: Mapping[int, Mapping[int, float]],
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    """Each seat's films and reach (decision 539): a member's own liked films and their top 30 unseen,
    a guest's well-known films and the pool's own top 30. Also every candidate some seat can reach."""
    average = {t: pool_rules.group_score(s) for t, s in ledger.items()}
    well_known: list[dict[str, Any]] | None = None
    chosen: dict[int, tuple[list[dict[str, Any]], str | None, list[int]]] = {}
    for seat in seats:
        if seat.is_member and seat.user_id is not None:
            films, word = await pool_rules.liked_films(
                conn, user_id=seat.user_id, kind=kind, vetoed_terms=vetoed_terms, dna_version=version,
            )
            stable = {t: s[seat.participant_id] for t, s in ledger.items() if seat.participant_id in s}
            seen = await pool_rules.seen_among(conn, user_id=seat.user_id, title_ids=list(stable))
        else:
            if well_known is None:
                well_known = await pool_rules.well_known_films(
                    conn, kind=kind, bundle_version=bundle_version, vetoed_terms=vetoed_terms,
                    dna_version=version,
                )
            films, word, stable, seen = well_known, None, average, set()
        chosen[seat.participant_id] = (films, word, pool_rules.reach(stable, seen=seen))

    asked = sorted(
        {f["title_id"] for films, _, _ in chosen.values() if round_rules.has_round(films) for f in films}
    )
    dna = await dna_reads.vectors_for(conn, asked, version=version or "")
    genres = await pool_rules.genres_of(conn, asked)
    frozen = {}
    for participant_id, (films, word, reach) in chosen.items():
        has_round = round_rules.has_round(films)
        frozen[str(participant_id)] = {
            "films": [
                {
                    **f,
                    "z": list(space.project(dna.get(f["title_id"], {}))),
                    "genres": genres.get(f["title_id"], []),
                }
                for f in films
            ] if has_round else [],
            "reach": reach,
            "no_round": None if has_round else copy_rules.no_round(
                need=round_rules.MIN_ROUND_FILMS, have=len(films), word=word
            ),
        }
    return frozen, {t for _, _, reach in chosen.values() for t in reach}


async def start(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    """Close the join window, build the pool once, and freeze it with each seat's round.

    One transaction whose first statement is the claim, so a join cannot land mid-build and a
    refusal rolls the claim back; `rooms.join`'s `FOR SHARE` serialises against it.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "UPDATE session SET state = $2 WHERE id = $1 AND state = $3 "
            "RETURNING kind, runtime_budget_min, include_rewatches, bundle_version, context",
            session_id, rooms.STATE_VOTING, rooms.STATE_OPEN,
        )
        if row is None:
            # Three reasons share one empty result, with different statuses: ask which.
            live = await conn.fetchrow("SELECT ended_at FROM session WHERE id = $1", session_id)
            if live is None:
                raise RoundError("no_room", "no such session")
            if live["ended_at"] is not None:
                raise RoundError("no_room", "that evening has ended")
            raise RoundError("already_started", "that room has already started")

        seats = await rooms.seats_of(conn, session_id)
        await _refuse_unscored_members(conn, session_id, bundle_version=row["bundle_version"])
        version = await dna_reads.active_version(conn)
        vetoes = rooms.vetoes_of(row["context"])
        vetoed = pool_rules.veto_terms(vetoes)
        candidates = await pool_rules.build(
            conn,
            seats=seats,
            kind=row["kind"],
            budget_min=row["runtime_budget_min"],
            include_rewatches=row["include_rewatches"],
            bundle_version=row["bundle_version"],
            vetoed_terms=vetoed,
            dna_version=version,
        )
        if len(candidates) < 2:
            # A pool of two or three is admitted (decision 215). A pool the vetoes emptied says so
            # (decision 480).
            named = pool_rules.veto_labels(vetoes)
            raise RoundError(
                "empty_pool",
                "nothing in the library fits tonight — widen the budget or include rewatches"
                + (f", or lift the veto on {' and '.join(named)}" if named else ""),
            )

        ids = [c.title_id for c in candidates]
        # Genres rather than DNA: the card must not preview the pool's order.
        genres = await pool_rules.genres_of(conn, ids)
        dna = await dna_reads.vectors_for(conn, ids, version=version or "")
        space = tilt_rules.space({t: dna.get(t, {}) for t in ids})
        seat_rounds, reached = await _frozen_rounds(
            conn, seats=seats, kind=row["kind"], bundle_version=row["bundle_version"],
            vetoed_terms=vetoed, version=version, space=space,
            ledger=_on_scale({c.title_id: dict(c.scores) for c in candidates}),
        )
        payload = {
            "candidates": {
                str(c.title_id): {
                    "title_id": c.title_id, "name": c.name, "year": c.year,
                    "kind": c.kind, "runtime_min": c.runtime_min,
                    "poster_path": c.poster_path,
                    "over_budget_min": c.over_budget_min, "fit_line": c.fit_line,
                    "genres": genres.get(c.title_id, []),
                }
                for c in candidates
            },
            "scores": {
                str(c.title_id): {str(p): v for p, v in c.scores.items()} for c in candidates
            },
            "dna": {str(k): v for k, v in dna.items()},
            "version": version,
            # §13's draw, sealed for the evening (decision 223); `secrets` so a client cannot predict it.
            "holdout_seed": secrets.token_hex(16),
            # Decision 477's rule; `scores` stays the raw §5.1 read and `_as_snapshot` derives the rest.
            "scale": pool_rules.SCALE_MARKER,
            "round": round_rules.ROUND_MARKER,
            "seats": seat_rounds,
            "z": {str(t): list(space.project(dna.get(t, {}))) for t in reached},
            "mood": {"terms": list(space.terms), "axes": space.axes.tolist()},
            # Frozen for §14 risk 6's reader, with whose each one was (decisions 480, 505).
            "vetoes": vetoes,
            "vetoes_by": {str(s): k for s, k in rooms.vetoes_by_seat(row["context"]).items()},
        }
        await conn.execute(
            # The dict, not a dumped string: the jsonb codec would encode it twice.
            "UPDATE session SET context = jsonb_set(context, '{pool}', $2) WHERE id = $1",
            session_id, payload,
        )
    return _as_snapshot({"pool": payload})


# --- one participant's round -----------------------------------------------------------------


# One column list for the plain and the locked read, so the two cannot drift.
_SEAT = """
    SELECT p.id, p.session_id, p.user_id, p.role, p.seat, p.tilt, p.answered_count,
           p.ended_by, s.state, s.ended_at
      FROM session_participant p JOIN session s ON s.id = p.session_id
     WHERE p.id = $1
"""
# `OF p`: a bare FOR UPDATE would lock the joined session row too.
_SEAT_LOCKED = _SEAT + " FOR UPDATE OF p"


async def _participant(
    conn: asyncpg.Connection, participant_id: int, *, lock: bool = False
) -> asyncpg.Record:
    """This seat as the write paths read it, optionally locked until the caller commits.

    `lock=True` only means something inside a transaction: the loser of a race waits and refuses
    on what the winner committed, instead of colliding with a unique index.
    """
    row = await conn.fetchrow(_SEAT_LOCKED if lock else _SEAT, participant_id)
    if row is None:
        raise RoundError("no_seat", "no such participant")
    if row["ended_at"] is not None:
        # An ended room (`ended_at`, per 0013's CHECK) has no seats; 404 via `no_room`.
        raise RoundError("no_room", "that evening has ended")
    return row


async def _answers(conn: asyncpg.Connection, participant_id: int) -> list[round_rules.Answered]:
    """This seat's live answers; retracted rows stay in the table (§14 risk 6) but are skipped."""
    rows = await conn.fetch(
        "SELECT seq, title_a, title_b, answer, selection FROM session_answer "
        "WHERE participant_id = $1 AND retracted_at IS NULL ORDER BY seq",
        participant_id,
    )
    return [
        round_rules.Answered(
            seq=r["seq"], title_a=r["title_a"], title_b=r["title_b"],
            answer=r["answer"], selection=r["selection"],
        )
        for r in rows
    ]


async def _turn_is_open(conn: asyncpg.Connection, row: asyncpg.Record) -> None:
    """§6.2 step 2: guests answer on the initiator's phone, in turn, after every earlier seat ends."""
    if row["role"] != rooms.ROLE_GUEST:
        return
    blocking = await conn.fetchval(
        """
        SELECT count(*) FROM session_participant
         WHERE session_id = $1 AND seat < $2 AND ended_by IS NULL
           AND (role = 'host' OR role = 'guest')
        """,
        row["session_id"], row["seat"],
    )
    if blocking:
        raise RoundError(
            "not_your_turn",
            "the phone has not reached this guest yet — earlier turns are still open",
        )


def _round_of(
    snapshot: Snapshot, row: asyncpg.Record, answers: list[round_rules.Answered], *, select: bool = True
) -> round_rules.Round:
    """One seat's whole round, replayed from its rows.

    The draw is seeded by (the pool's sealed nonce, this seat, live answer count + 1), so every read
    shows the same pair and an undo re-opens its seq with the same card (decision 223).
    """
    seq = len(answers) + 1
    return round_rules.replay(
        snapshot.round_of(row["id"]).films,
        snapshot.stable_for(row["id"], guest=row["role"] == rooms.ROLE_GUEST),
        snapshot.reach_of(row["id"]),
        answers,
        # The seat, never the seed: the arm is a rate keyed on who answers (decision 223).
        holdout_key=str(row["id"]),
        rng=random.Random(f"{snapshot.holdout_seed}:{row['id']}:{seq}"),
        escaped=row["ended_by"] == round_rules.ESCAPE,
        select=select,
    )


def _card(
    participant_id: int,
    *,
    answered: int,
    ended_by: str | None,
    stop_reason: str | None,
    played: round_rules.Round | None,
    snapshot: Snapshot | None = None,
    ended_now: bool = False,
) -> dict[str, Any]:
    """What one seat's device renders: the next pair, or that they are done, and why a seat with too
    little to ask about has no round.

    One shape for the read and the three writes. `played is None` is a seat with no round to
    report (an escape, or a stale ending in `_next_card`).
    """
    pair = None if played is None or stop_reason is not None else played.next_pair
    seat = None if snapshot is None else snapshot.round_of(participant_id)
    cards = {} if seat is None else seat.cards
    return {
        "participant_id": participant_id,
        "answered": answered,
        "ended_by": ended_by,
        "stop_reason": stop_reason,
        # The escape closes with the round: an ended seat is never offered it.
        "escape_available": ended_by is None and round_rules.escape_available(answered),
        "cap": round_rules.CAP_PAIRS,
        # The header's expectation: the typical round, not the cap.
        "typical": round_rules.TYPICAL_PAIRS,
        "no_round": None if seat is None else seat.no_round,
        "pair": None if pair is None else {
            "selection": pair.selection,
            "reason": pair.reason,
            "a": cards.get(pair.title_a),
            "b": cards.get(pair.title_b),
        },
        "_pair": pair,
        # Private: whether this call ended the seat, so the round read (the only handler that
        # neither settles nor pushes a frame) can announce it (decision 215).
        "_ended_now": ended_now,
    }


async def _next_card(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    *,
    answered: int,
    played: round_rules.Round,
    snapshot: Snapshot,
) -> dict[str, Any]:
    """The card a write hands straight back, from the write's own replay: one snapshot read and one
    replay for the whole tap.

    Runs after the commit, so the ending is conditional on the answer count (`when_answered`): on
    a refusal the card reports the row with no pair and the client re-reads.
    """
    ended_by, stop_reason = row["ended_by"], played.stop_reason
    ended_now = False
    if stop_reason is not None and ended_by is None:
        if await _end(conn, row["id"], stop_reason, when_answered=answered):
            ended_by, ended_now = stop_reason, True
        else:
            fresh = await _participant(conn, row["id"])
            answered, ended_by = fresh["answered_count"], fresh["ended_by"]
            stop_reason, played = None, None
    return _card(
        row["id"], answered=answered, ended_by=ended_by, stop_reason=stop_reason,
        played=played, snapshot=snapshot, ended_now=ended_now,
    )


async def state_for(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """What one participant's device renders, read fresh (the reload); `_card`'s shape."""
    row = await _participant(conn, participant_id)
    snapshot = await snapshot_of(conn, row["session_id"])
    answers = await _answers(conn, participant_id)
    played = _round_of(snapshot, row, answers)

    answered, ended_by = row["answered_count"], row["ended_by"]
    stop_reason = played.stop_reason
    ended_now = False
    if stop_reason is not None and ended_by is None:
        # A reason with no pair means nothing can be asked, so end the seat here or it strands
        # (too little to ask about, decision 539). A write on a read, idempotent, and guarded by
        # `when_answered`: if the count moved, report the row with no pair; if another writer
        # ended it first, the row keeps theirs.
        if await _end(conn, participant_id, stop_reason, when_answered=answered):
            ended_by, ended_now = stop_reason, True
        else:
            fresh = await _participant(conn, participant_id)
            if fresh["answered_count"] != answered:
                answered, ended_by = fresh["answered_count"], fresh["ended_by"]
                stop_reason, played = None, None
            else:
                ended_by = stop_reason
    return _card(
        participant_id, answered=answered, ended_by=ended_by,
        stop_reason=stop_reason, played=played, snapshot=snapshot, ended_now=ended_now,
    )


async def record_answer(
    conn: asyncpg.Connection,
    *,
    participant_id: int,
    pair: round_rules.Pair,
    answer: str,
    seq: int,
    latency_ms: int | None,
) -> dict[str, Any]:
    """Write one answer, move the tilt, and hand back the next card.

    Guards run under the seat's row lock, so a double tap refuses as `stale_pair`. The row's seq
    is minted from the rows (tombstones included), independent of `answered_count`.
    """
    async with conn.transaction():
        row = await _participant(conn, participant_id, lock=True)
        if row["ended_by"] is not None:
            raise RoundError("round_over", "this round has already ended")
        await _turn_is_open(conn, row)
        if answer not in round_rules.ANSWERS:
            raise RoundError("bad_answer", f"answer must be one of {round_rules.ANSWERS}")
        if seq != row["answered_count"] + 1:
            # Single-use: a replay would weight one judgement twice in §13's data.
            raise RoundError("stale_pair", "that pair is no longer on the table")

        snapshot = await snapshot_of(conn, row["session_id"])
        # Tombstones included: reusing a retracted seq collides with its row.
        written_seq = await conn.fetchval(
            "SELECT coalesce(max(seq), 0) + 1 FROM session_answer WHERE participant_id = $1",
            participant_id,
        )
        await conn.execute(
            """
            INSERT INTO session_answer
                (session_id, participant_id, seq, title_a, title_b, answer, selection, latency_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            row["session_id"], participant_id, written_seq,
            pair.title_a, pair.title_b, answer, pair.selection, latency_ms,
        )
        answers = await _answers(conn, participant_id)
        played = _round_of(snapshot, row, answers)
        # The mood as term weights; a held-out answer moves none of it.
        tilt = snapshot.tilt(played.mood.mean)
        await _set_answered(conn, participant_id, count=row["answered_count"] + 1, tilt=tilt)
    card = await _next_card(
        conn, row, answered=row["answered_count"] + 1, played=played, snapshot=snapshot,
    )
    # The seq WRITTEN, which after an undo differs from the one the card carried.
    return {**card, "seq": written_seq, "tilt": tilt}


async def _set_answered(
    conn: asyncpg.Connection, participant_id: int, *, count: int, tilt: dict[str, float]
) -> None:
    """Move the displayed progress and the tilt together, never the counter past the rows.

    Raises rather than asserts (`-O` strips asserts); both callers are in a transaction, so the
    refusal rolls back a playable seat.
    """
    moved = await conn.fetchval(
        """
        UPDATE session_participant p SET answered_count = $2, tilt = $3
         WHERE p.id = $1
           AND $2 <= (SELECT coalesce(max(seq), 0) FROM session_answer WHERE participant_id = p.id)
        RETURNING p.id
        """,
        participant_id, count, tilt,
    )
    if moved is None:
        raise AssertionError(
            f"refusing answered_count = {count} on seat {participant_id}: it would pass the "
            "highest seq this seat has ever been issued, so the counter would claim answers that "
            "have no rows"
        )


async def _end(
    conn: asyncpg.Connection, participant_id: int, reason: str, *, when_answered: int | None = None
) -> bool:
    """End a seat; True if this call ended it. The first ending recorded is the one that happened.

    `when_answered` refuses to end on a replay whose answer count is no longer the seat's.
    """
    ended = await conn.fetchval(
        "UPDATE session_participant SET ended_by = $2, "
        "converged_at = CASE WHEN $2 = 'converged' THEN now() ELSE NULL END "
        "WHERE id = $1 AND ended_by IS NULL AND ($3::int IS NULL OR answered_count = $3) "
        "RETURNING id",
        participant_id, reason, when_answered,
    )
    return ended is not None


async def retract(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """§6 preamble's "undo everywhere", reaching the round: your latest live answer, while playing.

    A tombstone, not a DELETE (§14 risk 6); the tilt is rebuilt from the surviving rows. Under the
    seat's row lock, like the answer; returns `_next_card`, which may end the seat.
    """
    async with conn.transaction():
        row = await _participant(conn, participant_id, lock=True)
        if row["ended_by"] is not None:
            raise RoundError("round_over", "a finished round cannot be edited")
        last = await conn.fetchrow(
            "SELECT id, seq FROM session_answer WHERE participant_id = $1 AND retracted_at IS NULL "
            "ORDER BY seq DESC LIMIT 1",
            participant_id,
        )
        if last is None:
            raise RoundError("nothing_to_undo", "no answer to take back")

        snapshot = await snapshot_of(conn, row["session_id"])
        await conn.execute(
            "UPDATE session_answer SET retracted_at = now() WHERE id = $1", last["id"]
        )
        answers = await _answers(conn, participant_id)
        played = _round_of(snapshot, row, answers)
        await _set_answered(
            conn, participant_id, count=len(answers), tilt=snapshot.tilt(played.mood.mean)
        )
    card = await _next_card(conn, row, answered=len(answers), played=played, snapshot=snapshot)
    return {**card, "retracted_seq": last["seq"]}


async def escape(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """"Just pick for us": end this seat's round on what is known so far.

    Refused before pair 4 and recorded as `escape` (§14 risk 6); decided under the row lock.
    """
    async with conn.transaction():
        row = await _participant(conn, participant_id, lock=True)
        if row["ended_by"] is not None:
            raise RoundError("round_over", "this round has already ended")
        try:
            reason = round_rules.escape(answered=row["answered_count"])
        except round_rules.EscapeTooEarly as exc:
            raise RoundError("too_early", str(exc)) from exc
        ended_now = await _end(conn, participant_id, reason)
    return _card(
        participant_id, answered=row["answered_count"], ended_by=reason, stop_reason=reason,
        played=None, ended_now=ended_now,
    )


async def progress(conn: asyncpg.Connection, session_id: int) -> list[dict[str, Any]]:
    """54c's waiting state: "**progress and never their answers**"; no join to `session_answer`."""
    rows = await conn.fetch(
        """
        SELECT p.id, p.seat, p.role, p.answered_count, p.ended_by, u.name
          FROM session_participant p LEFT JOIN app_user u ON u.id = p.user_id
         WHERE p.session_id = $1 ORDER BY p.seat
        """,
        session_id,
    )
    return [
        {
            "participant_id": r["id"],
            "seat": r["seat"],
            "name": r["name"] or f"Guest {r['seat'] - 1}",
            "answered": r["answered_count"],
            "expected": expected_pairs(r["answered_count"]),
            "finished": r["ended_by"] is not None,
            "ended_by": r["ended_by"],
        }
        for r in rows
    ]


def expected_pairs(answered: int) -> int | None:
    """§6.2 step 4's "Mia 4/~10": the typical round until reached, then None (decision 507).

    A function of the count alone, which keeps this payload blind.
    """
    if answered >= round_rules.TYPICAL_PAIRS:
        return None
    return round_rules.TYPICAL_PAIRS


async def everyone_finished(conn: asyncpg.Connection, session_id: int) -> bool:
    return not await conn.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1 AND ended_by IS NULL",
        session_id,
    )


# --- the combine ------------------------------------------------------------------------------


async def _match_lines(
    conn: asyncpg.Connection,
    *,
    snapshot: Snapshot,
    seats: Sequence[asyncpg.Record],
    title_id: int,
    tonight: Mapping[int, Mapping[int, float]],
) -> dict[str, Any]:
    """§6.2 step 7's per-person match lines, "in DNA terms including the honest negative".

    Terms come from the title's own rows and the tilt only orders them. The negative is printed
    only for a title below the seat's median tonight score; every line names its person in labels.
    """
    carried = await dna_reads.terms_carried_by(
        conn, title_id, version=snapshot.version or "", limit=8
    )
    named = await dna_terms.labels_for(conn, [t["term"] for t in carried])

    def word(term: str) -> str:
        return str((named.get(term) or {}).get("label") or dna_terms.label_of(term, None))

    def listed(terms: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"term": t, "tier": tier, "label": word(t)} for t, tier in terms]

    lines: dict[str, Any] = {}
    for seat in seats:
        name = seat["name"] or f"Guest {seat['seat'] - 1}"
        tilt = dict(seat["tilt"] or {})
        if seat["role"] == rooms.ROLE_GUEST and not tilt:
            lines[str(seat["id"])] = {
                "name": name, "line": copy_rules.no_profile(name), "terms": [], "sign": "none",
            }
            continue
        own = tonight.get(seat["id"]) or {}
        below_usual = title_id in own and own[title_id] < statistics.median(own.values())
        # The tilt weights carried terms; it never admits one.
        scored = sorted(
            ((t["term"], tilt.get(t["term"], 0.0), t["tier"]) for t in carried),
            key=lambda x: -x[1],
        )
        pulls = [x for x in scored if x[1] > 0.0][:2]
        leaned = bool(pulls)
        if not pulls and not below_usual:
            # Theirs by stable taste: the title's own loudest terms, as solo says (`solo.why_line`).
            pulls = [(t["term"], 0.0, t["tier"]) for t in carried[:2]]
        if pulls:
            words = [word(t) for t, _, _ in pulls]
            lines[str(seat["id"])] = {
                "name": name,
                "line": copy_rules.leaned(name, words) if leaned else copy_rules.usual(name, words),
                "terms": listed([(t, tier) for t, _, tier in pulls]),
                "sign": "pull",
            }
            continue
        against = [x for x in scored if x[1] < 0.0]
        if against and below_usual:
            worst = against[-1]
            lines[str(seat["id"])] = {
                "name": name,
                "line": copy_rules.no_pull(word(worst[0]), name=name),
                "terms": listed([(worst[0], worst[2])]),
                "sign": "against",
            }
            continue
        # Nothing carried moves this person either way: say so rather than invent a term.
        lines[str(seat["id"])] = {
            "name": name, "line": f"nothing here reads either way for {name} yet",
            "terms": [], "sign": "neutral",
        }
    return lines


async def finish(conn: asyncpg.Connection, session_id: int) -> combine_rules.Slate | None:
    """§6.2 step 5, against the stored rows, persisted to `session_result` (§14 risk 6).

    None, and nothing written, when the household ended the evening while this ran.
    """
    snapshot = await snapshot_of(conn, session_id)
    seats = await conn.fetch(
        """
        SELECT p.id, p.role, p.tilt, p.seat, p.ended_by, u.name
          FROM session_participant p LEFT JOIN app_user u ON u.id = p.user_id
         WHERE p.session_id = $1 ORDER BY p.seat
        """,
        session_id,
    )
    per_participant: dict[int, dict[int, float]] = {}
    for seat in seats:
        answers = await _answers(conn, seat["id"])
        # No pair: only the mood is read.
        played = _round_of(snapshot, seat, answers, select=False)
        per_participant[seat["id"]] = round_rules.tonight(
            snapshot.stable_for(seat["id"], guest=seat["role"] == rooms.ROLE_GUEST),
            snapshot.reach_of(seat["id"]),
            played.mood.mean,
        )

    slate = combine_rules.combine(
        per_participant=per_participant,
        # On the room's scale, the one decision 478 recalibrated the threshold on.
        member_ledger=snapshot.member_ledger(),
        dna=snapshot.dna,
    )
    # Match lines for the ballot's titles only (§6.2 step 7), not the pool's tail.
    on_the_slate = set(slate.ballot_titles)
    matches = {
        title_id: await _match_lines(
            conn, snapshot=snapshot, seats=seats, title_id=title_id, tonight=per_participant,
        )
        for title_id in on_the_slate
    }
    async with conn.transaction():
        # Two final answers can both reach the combine: the lock makes the loser wait and replace.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _FINISH_LOCK, session_id)
        # Still a live room: a bare `set_state` would resurrect a room the host ended meanwhile.
        # `FOR UPDATE` serialises with `rooms.end_session` (decision 169).
        live = await conn.fetchval(
            "SELECT id FROM session WHERE id = $1 AND ended_at IS NULL FOR UPDATE", session_id
        )
        if live is None:
            return None
        await conn.execute("DELETE FROM session_result WHERE session_id = $1", session_id)
        for row in slate.rows:
            await conn.execute(
                """
                INSERT INTO session_result
                    (session_id, title_id, rank, slot, group_score, per_user_match, conflict,
                     reserved_for)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                session_id, row["title_id"], row["rank"], row["slot"], row["group_score"],
                matches.get(row["title_id"], {}),
                slate.conflict if slate.conflict and row["slot"] != "runner_up" else None,
                row["reserved_for"],
            )
        await rooms.set_state(conn, session_id, rooms.STATE_BALLOT)
    return slate


async def settle(conn: asyncpg.Connection, session_id: int) -> bool:
    """Move a room whose every seat has ended on to the ballot. True if THIS call moved it.

    Every read calls it (session, ballot, result), so a failed combine does not strand the room;
    repeatable because `finish` locks and `ballot.resolve` is idempotent. Nothing is swallowed.
    """
    if not await everyone_finished(conn, session_id):
        return False
    state = await conn.fetchval("SELECT state FROM session WHERE id = $1", session_id)
    # The state is the claim: only a `voting` room has a combine to run.
    if state != rooms.STATE_VOTING:
        return False
    # `finish` re-checks under the row lock, so the return stays true of THIS call.
    return await finish(conn, session_id) is not None


__all__ = [
    "RoundError",
    "Snapshot",
    "escape",
    "everyone_finished",
    "expected_pairs",
    "finish",
    "progress",
    "record_answer",
    "retract",
    "settle",
    "snapshot_of",
    "start",
    "state_for",
]
