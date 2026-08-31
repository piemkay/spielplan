"""The round's write path: start, serve, answer, retract, escape, finish.

Spec v2.1 §6.2 steps 3-6 (rewritten, 54b-54e), §4.2, §6 preamble, §13, §14 risk 6.

THE POOL IS SNAPSHOT AT START, AND THAT IS THE IMPLEMENTATION OF ONE OF §6.2's RULES. Step 6:
"Votes *choose*; nothing re-ranks within the evening by predicted enjoyment (measured: worth
0.000)." A pool rebuilt on every request would re-rank silently the moment the nightly fit ran
mid-evening, or the moment somebody marked a title seen in the other room. So `start` computes
the pool once, with its DNA and its axes, and writes it into `session.context`; every read
afterwards is of that snapshot. The rule stops being a thing to remember and becomes a thing
the data does.

EVERY ANSWER NAMES A SEALED PAIR, NEVER TWO TITLE IDS. Same reason `api/rank.py` seals a queue
pair: 54b/§13 make `selection` a discriminator the *evaluation* depends on, and a route that
accepted `{"title_a": 4, "title_b": 9, "selection": "adaptive"}` would let the client decide
which stream its answer belonged to. The seal carries the participant, the sequence number and
the arm, and it is single-use because it carries the seq — answering moves the counter, so a
replay is a stale card and gets a 409. §13's figures count *rows*, and §4.2's tables are
append-only, so a replay that landed would weight one judgement N-fold in the data admitted to
evaluate the round, and could not be taken back.

BLIND BY CONSTRUCTION, IN THE QUERY. 54c: "Someone who finishes early sees the others'
**progress and never their answers**." `progress()` selects counts and never titles, so the
blind property is a fact about what the statement can return rather than a decision the UI
makes — "the payload cannot carry the answers, not that the UI declines to draw them".
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.tonight import combine as combine_rules
from spielplan.tonight import copy as copy_rules
from spielplan.tonight import dna as dna_reads
from spielplan.tonight import pool as pool_rules
from spielplan.tonight import rooms
from spielplan.tonight import round as round_rules
from spielplan.tonight import tilt as tilt_rules


class RoundError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Snapshot:
    """The frozen evening: which titles, what each seat scores them, their DNA, the axes.

    Everything the round and the combine need, and nothing that changes while the evening runs.
    """

    candidates: dict[int, dict[str, Any]]
    scores: dict[int, dict[int, float]]      # title_id -> {participant_id: §5.1 score}
    dna: dict[int, dict[str, float]]
    axes: dict[str, dict[str, float]]
    version: str | None

    @property
    def title_ids(self) -> list[int]:
        return list(self.candidates)

    def pool_scores_for(self, participant_id: int) -> dict[int, float]:
        """One seat's §5.1 scores over the pool — the round's prior for a member.

        A guest has no entry anywhere in `scores`, so this is empty for them, which is exactly
        54c's "starts from the pool prior and is carried entirely by their answers".
        """
        return {
            t: seat_scores[participant_id]
            for t, seat_scores in self.scores.items()
            if participant_id in seat_scores
        }

    def member_average(self) -> dict[int, float]:
        """The pool's own order (§6.2 step 3), which is what a profile-less guest is ranked by.

        Never a member's Ledger wearing the guest's name — the prototype's `const u = guest ?
        'p' : who` is a privacy-shaped bug, not "contributes no taste term".
        """
        return {t: pool_rules.group_score(s) for t, s in self.scores.items()}

    def frame(self) -> tilt_rules.Frame:
        return tilt_rules.frame(self.dna)


def _as_snapshot(context: Any) -> Snapshot:
    ctx = context if isinstance(context, dict) else json.loads(context or "{}")
    raw = ctx.get("pool") or {}
    return Snapshot(
        candidates={int(k): v for k, v in (raw.get("candidates") or {}).items()},
        scores={
            int(t): {int(p): float(v) for p, v in seats.items()}
            for t, seats in (raw.get("scores") or {}).items()
        },
        dna={int(k): {t: float(w) for t, w in v.items()} for k, v in (raw.get("dna") or {}).items()},
        axes={f: {t: float(w) for t, w in v.items()} for f, v in (raw.get("axes") or {}).items()},
        version=raw.get("version"),
    )


async def snapshot_of(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    context = await conn.fetchval("SELECT context FROM session WHERE id = $1", session_id)
    if context is None:
        raise RoundError("no_room", "no such session")
    return _as_snapshot(context)


async def start(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    """Close the join window, build the pool once, and freeze it.

    §6.2 step 2's rule as the host's lobby states it — "Anyone who joins before you start is
    in" — is exactly this transition: after it the seats are fixed, which is what makes the
    participant count the averages are over a constant of the evening.
    """
    row = await conn.fetchrow(
        "SELECT kind, runtime_budget_min, include_rewatches, bundle_version, state "
        "FROM session WHERE id = $1",
        session_id,
    )
    if row is None:
        raise RoundError("no_room", "no such session")
    if row["state"] != rooms.STATE_OPEN:
        raise RoundError("already_started", "that room has already started")

    seats = await rooms.seats_of(conn, session_id)
    candidates = await pool_rules.build(
        conn,
        seats=seats,
        kind=row["kind"],
        budget_min=row["runtime_budget_min"],
        include_rewatches=row["include_rewatches"],
        bundle_version=row["bundle_version"],
    )
    if len(candidates) < 2:
        # §6.2 defines the happy path only. An empty or one-title pool is not a round, and
        # saying so is better than serving a pair that does not exist.
        raise RoundError(
            "empty_pool",
            "nothing in the library fits tonight — widen the budget or include rewatches",
        )

    version = await dna_reads.active_version(conn)
    ids = [c.title_id for c in candidates]
    payload = {
        "candidates": {
            str(c.title_id): {
                "title_id": c.title_id, "name": c.name, "year": c.year,
                "kind": c.kind, "runtime_min": c.runtime_min,
                "poster_path": c.poster_path,
                "over_budget_min": c.over_budget_min, "fit_line": c.fit_line,
            }
            for c in candidates
        },
        "scores": {
            str(c.title_id): {str(p): v for p, v in c.scores.items()} for c in candidates
        },
        "dna": {
            str(k): v for k, v in (await dna_reads.vectors_for(conn, ids, version=version or "")).items()
        },
        "axes": await dna_reads.axes_for(conn, version=version or ""),
        "version": version,
    }
    await conn.execute(
        # The dict, not a dumped string: `db/pool.py` registers a JSON codec on jsonb, so a
        # pre-dumped argument is encoded twice and lands as a JSON *string*.
        "UPDATE session SET context = jsonb_set(context, '{pool}', $2), state = $3 "
        "WHERE id = $1",
        session_id, payload, rooms.STATE_VOTING,
    )
    return _as_snapshot({"pool": payload})


# --- one participant's round -----------------------------------------------------------------


async def _participant(conn: asyncpg.Connection, participant_id: int) -> asyncpg.Record:
    row = await conn.fetchrow(
        """
        SELECT p.id, p.session_id, p.user_id, p.role, p.seat, p.tilt, p.answered_count,
               p.ended_by, s.state
          FROM session_participant p JOIN session s ON s.id = p.session_id
         WHERE p.id = $1
        """,
        participant_id,
    )
    if row is None:
        raise RoundError("no_seat", "no such participant")
    return row


async def _answers(conn: asyncpg.Connection, participant_id: int) -> list[round_rules.Answered]:
    """This seat's live answers. A retracted row is skipped — §6's undo means the answer no
    longer counts, while §14 risk 6's "log every vote" means the row itself stays."""
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
    """§6.2 step 2: "Guests use the initiator's phone **after the initiator finishes**
    (hand-the-phone, sequential turns)."

    A guest seat cannot answer until every earlier seat has ended, which is both halves of the
    rule: the initiator goes first, and guests take turns one at a time on the one device.
    Members on their own phones are unaffected — their seats are not guest seats.
    """
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
    snapshot: Snapshot, row: asyncpg.Record, answers: list[round_rules.Answered], *, z: float,
    rng: random.Random | None = None,
) -> round_rules.Round:
    is_member = row["role"] != rooms.ROLE_GUEST
    prior = snapshot.pool_scores_for(row["id"]) if is_member else snapshot.member_average()
    return round_rules.replay(
        prior, answers, z=z, has_profile=is_member,
        axes=combine_rules.axis_positions(snapshot.dna, snapshot.axes),
        rng=rng, escaped=row["ended_by"] == round_rules.ESCAPE,
    )


async def state_for(
    conn: asyncpg.Connection, participant_id: int, *, z: float, rng: random.Random | None = None
) -> dict[str, Any]:
    """What one participant's device renders: the next pair, or that they are done.

    Carries the escape's availability rather than leaving the client to compute it from a
    count — 54c makes the control a property of the round's state, and a client that decided
    for itself would be a second implementation of the rule.
    """
    row = await _participant(conn, participant_id)
    snapshot = await snapshot_of(conn, row["session_id"])
    answers = await _answers(conn, participant_id)
    played = _round_of(snapshot, row, answers, z=z, rng=rng)

    pair = None
    if played.stop_reason is None and played.next_pair is not None:
        pair = played.next_pair
    return {
        "participant_id": participant_id,
        "answered": row["answered_count"],
        "ended_by": row["ended_by"],
        "stop_reason": played.stop_reason,
        "escape_available": round_rules.escape_available(row["answered_count"]),
        "cap": round_rules.CAP_PAIRS,
        "pair": None if pair is None else {
            "selection": pair.selection,
            "reason": pair.reason,
            "a": snapshot.candidates.get(pair.title_a),
            "b": snapshot.candidates.get(pair.title_b),
        },
        "_pair": pair,
        "_round": played,
        "_snapshot": snapshot,
    }


async def record_answer(
    conn: asyncpg.Connection,
    *,
    participant_id: int,
    pair: round_rules.Pair,
    answer: str,
    seq: int,
    latency_ms: int | None,
    z: float,
) -> dict[str, Any]:
    """Write one answer, move the tilt, and decide whether this seat is finished.

    Inside one transaction: the row, the counter and the tilt are one fact, and a crash between
    them would leave `answered_count` disagreeing with the rows it counts — which is the number
    the lobby and the waiting screen both display.
    """
    row = await _participant(conn, participant_id)
    if row["ended_by"] is not None:
        raise RoundError("round_over", "this round has already ended")
    await _turn_is_open(conn, row)
    if answer not in round_rules.ANSWERS:
        raise RoundError("bad_answer", f"answer must be one of {round_rules.ANSWERS}")
    if seq != row["answered_count"] + 1:
        # The single-use guard. §13's figures count rows and §4.2's tables are append-only, so
        # a replay weights one judgement twice in the data admitted to evaluate the round.
        raise RoundError("stale_pair", "that pair is no longer on the table")

    snapshot = await snapshot_of(conn, row["session_id"])
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO session_answer
                (session_id, participant_id, seq, title_a, title_b, answer, selection, latency_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            row["session_id"], participant_id, seq,
            pair.title_a, pair.title_b, answer, pair.selection, latency_ms,
        )
        # §6.2 step 5's tilt. Held-out answers move it as little as they move the posterior:
        # 54b says they are "used for neither selection nor stopping", and the tilt feeds the
        # tonight score the shortlist is built from, so it is the same stream.
        tilt = dict(row["tilt"] or {})
        if pair.selection != round_rules.SELECTION_HOLDOUT:
            frame = snapshot.frame()
            a_dna = snapshot.dna.get(pair.title_a, {})
            b_dna = snapshot.dna.get(pair.title_b, {})
            if answer == round_rules.A:
                tilt = tilt_rules.observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
            elif answer == round_rules.B:
                tilt = tilt_rules.observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
            else:
                tilt = tilt_rules.observe_level(
                    tilt, first=a_dna, second=b_dna, frame=frame,
                    toward=answer == round_rules.EITHER,
                )
        await conn.execute(
            "UPDATE session_participant SET answered_count = answered_count + 1, tilt = $2 "
            "WHERE id = $1",
            participant_id, tilt,
        )
        refreshed = await _participant(conn, participant_id)
        played = _round_of(snapshot, refreshed, await _answers(conn, participant_id), z=z)
        if played.stop_reason is not None:
            await _end(conn, participant_id, played.stop_reason)
    return {"seq": seq, "stop_reason": played.stop_reason, "tilt": tilt}


async def _end(conn: asyncpg.Connection, participant_id: int, reason: str) -> None:
    """0013's CHECK ties `converged_at` to `ended_by = 'converged'`, so the two cannot drift —
    §14 risk 6 wants the rate at which each of the three fires."""
    await conn.execute(
        "UPDATE session_participant SET ended_by = $2, "
        "converged_at = CASE WHEN $2 = 'converged' THEN now() ELSE NULL END WHERE id = $1",
        participant_id, reason,
    )


async def retract(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """§6 preamble's "undo everywhere", reaching the round.

    Your own most recent live answer, and only while your own round is still running. Tombstone
    rather than DELETE (§14 risk 6: "log every vote"), and the tilt is recomputed from the
    surviving rows rather than subtracted — subtracting assumes the frame has not moved and the
    arithmetic is exact, and one of those is a floating-point hope.
    """
    row = await _participant(conn, participant_id)
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
    async with conn.transaction():
        await conn.execute(
            "UPDATE session_answer SET retracted_at = now() WHERE id = $1", last["id"]
        )
        answers = await _answers(conn, participant_id)
        frame = snapshot.frame()
        tilt: dict[str, float] = {}
        for a in answers:
            if a.selection == round_rules.SELECTION_HOLDOUT:
                continue
            a_dna, b_dna = snapshot.dna.get(a.title_a, {}), snapshot.dna.get(a.title_b, {})
            if a.answer == round_rules.A:
                tilt = tilt_rules.observe(tilt, chosen=a_dna, rejected=b_dna, frame=frame)
            elif a.answer == round_rules.B:
                tilt = tilt_rules.observe(tilt, chosen=b_dna, rejected=a_dna, frame=frame)
            else:
                tilt = tilt_rules.observe_level(
                    tilt, first=a_dna, second=b_dna, frame=frame,
                    toward=a.answer == round_rules.EITHER,
                )
        await conn.execute(
            "UPDATE session_participant SET answered_count = $2, tilt = $3 WHERE id = $1",
            participant_id, len(answers), tilt,
        )
    return {"answered": len(answers), "retracted_seq": last["seq"]}


async def escape(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """54c's "just pick for us": end this seat's round on what is known so far.

    Refused before pair 6 rather than ignored — a control that silently does nothing is worse
    than one that is not there — and recorded as `escape` so §14 risk 6 can count it.
    """
    row = await _participant(conn, participant_id)
    if row["ended_by"] is not None:
        raise RoundError("round_over", "this round has already ended")
    try:
        reason = round_rules.escape(answered=row["answered_count"])
    except round_rules.EscapeTooEarly as exc:
        raise RoundError("too_early", str(exc)) from exc
    await _end(conn, participant_id, reason)
    return {"ended_by": reason, "answered": row["answered_count"]}


async def progress(conn: asyncpg.Connection, session_id: int) -> list[dict[str, Any]]:
    """54c's waiting state: "**progress and never their answers**".

    The blind property is a fact about what this statement can return. There is no join to
    `session_answer` here and no title column anywhere in it, so a payload carrying somebody's
    answer is not something a caller could produce by mistake.
    """
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
            "expected": round_rules.CAP_PAIRS,
            "finished": r["ended_by"] is not None,
            "ended_by": r["ended_by"],
        }
        for r in rows
    ]


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
) -> dict[str, Any]:
    """§6.2 step 7's per-person match lines, "in DNA terms including the honest negative".

    THE INVARIANT, BORROWED FROM §6.0. `home/why.py` was inverted so a shelf's why-line names
    terms every card actually carries — "a card can be shown under a reason it does not
    satisfy" is the defect it exists to make unrepresentable. The winner card is the same claim
    on the screen the whole round exists to produce, so the terms come from the title's own
    `dna_tagged` rows and the participant's tilt only *orders* them.

    Three branches, and §6.2 fixes two of them verbatim: the pull line, the honest negative
    ("nothing here is their pull — *bleak* works against them"), and — for a guest with no grid
    profile — a line rather than silence, because every participant gets one.
    """
    carried = await dna_reads.terms_carried_by(
        conn, title_id, version=snapshot.version or "", limit=8
    )
    lines: dict[str, Any] = {}
    for seat in seats:
        name = seat["name"] or f"Guest {seat['seat'] - 1}"
        tilt = dict(seat["tilt"] or {})
        if seat["role"] == rooms.ROLE_GUEST and not tilt:
            lines[str(seat["id"])] = {
                "name": name, "line": copy_rules.no_profile(name), "terms": [], "sign": "none",
            }
            continue
        # The tilt weights the terms the title carries. It never admits one: a term the title
        # does not carry cannot appear here whatever the tilt says about it.
        scored = sorted(
            ((t["term"], tilt.get(t["term"], 0.0), t["tier"]) for t in carried),
            key=lambda x: -x[1],
        )
        pulls = [x for x in scored if x[1] > 0.0][:2]
        if pulls:
            lines[str(seat["id"])] = {
                "name": name,
                "line": f"pulls {name} with " + " + ".join(t for t, _, _ in pulls),
                "terms": [{"term": t, "tier": tier} for t, _, tier in pulls],
                "sign": "pull",
            }
            continue
        against = [x for x in scored if x[1] < 0.0]
        if against:
            worst = against[-1]
            lines[str(seat["id"])] = {
                "name": name,
                "line": copy_rules.no_pull(worst[0]),
                "terms": [{"term": worst[0], "tier": worst[2]}],
                "sign": "against",
            }
            continue
        # Nothing the title carries moves this person either way, which is a real state on a
        # short round: say so rather than inventing a term to fill the line.
        lines[str(seat["id"])] = {
            "name": name, "line": f"nothing here reads either way for {name} yet",
            "terms": [], "sign": "neutral",
        }
    return lines



async def finish(
    conn: asyncpg.Connection, session_id: int, *, z: float, phrasing: str | None = None
) -> combine_rules.Slate:
    """§6.2 step 5, against the stored rows, persisted to `session_result`.

    The slate is written rather than recomputed on read: §4.2 gives the round a durable
    per-title table, and a slate re-derived later cannot be compared against the votes that
    produced it — which is what §14 risk 6 exists to require.
    """
    snapshot = await snapshot_of(conn, session_id)
    seats = await conn.fetch(
        """
        SELECT p.id, p.role, p.tilt, p.seat, u.name
          FROM session_participant p LEFT JOIN app_user u ON u.id = p.user_id
         WHERE p.session_id = $1 ORDER BY p.seat
        """,
        session_id,
    )
    frame = snapshot.frame()
    per_participant: dict[int, dict[int, float]] = {}
    tilts: list[dict[str, float]] = []
    for seat in seats:
        is_member = seat["role"] != rooms.ROLE_GUEST
        prior = (
            snapshot.pool_scores_for(seat["id"]) if is_member else snapshot.member_average()
        )
        answers = await _answers(conn, seat["id"])
        played = round_rules.replay(prior, answers, z=z, has_profile=is_member)
        tilt = dict(seat["tilt"] or {})
        if is_member:
            tilts.append(tilt)
        per_participant[seat["id"]] = {
            t: b.mu + tilt_rules.adjustment(tilt, snapshot.dna.get(t, {}), frame)
            for t, b in played.beliefs.items()
        }

    member_ledger = {
        t: [v for p, v in seat_scores.items()] for t, seat_scores in snapshot.scores.items()
    }
    slate = combine_rules.combine(
        per_participant=per_participant,
        member_ledger=member_ledger,
        tilts=tilts,
        axes=snapshot.axes,
        dna=snapshot.dna,
        phrasing=phrasing,
    )
    # Match lines for the slate the ballot is over. Runners-up carry them too (§6.2 step 7:
    # "Match lines appear on the winner card and each runner-up"), but the pool's tail does not
    # — a line per candidate on a fifty-title pool is a query nobody reads.
    on_the_slate = set(slate.ballot_titles)
    matches = {
        title_id: await _match_lines(conn, snapshot=snapshot, seats=seats, title_id=title_id)
        for title_id in on_the_slate
    }
    async with conn.transaction():
        await conn.execute("DELETE FROM session_result WHERE session_id = $1", session_id)
        for row in slate.rows:
            await conn.execute(
                """
                INSERT INTO session_result
                    (session_id, title_id, rank, slot, group_score, per_user_match, conflict)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                session_id, row["title_id"], row["rank"], row["slot"], row["group_score"],
                matches.get(row["title_id"], {}),
                slate.conflict if slate.conflict and row["slot"] != "runner_up" else None,
            )
        await rooms.set_state(conn, session_id, rooms.STATE_BALLOT)
    return slate


__all__ = [
    "RoundError",
    "Snapshot",
    "escape",
    "everyone_finished",
    "finish",
    "progress",
    "record_answer",
    "retract",
    "snapshot_of",
    "start",
    "state_for",
]
