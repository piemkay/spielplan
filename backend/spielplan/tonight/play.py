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
the arm, and it is single-use per LIVE ANSWER COUNT because that is what it carries — answering
moves the counter, so a replay is a stale card and gets a 409, while an undo lowers the counter and
re-opens that seq with the card it already issued (`_round_of` seals the draw, decision 223).
§13's figures count *rows*, and §4.2's tables are append-only, so a replay that landed would weight
one judgement N-fold in the data admitted to evaluate the round, and could not be taken back.

BLIND BY CONSTRUCTION, IN THE QUERY. 54c: "Someone who finishes early sees the others'
**progress and never their answers**." `progress()` selects counts and never titles, so the
blind property is a fact about what the statement can return rather than a decision the UI
makes — "the payload cannot carry the answers, not that the UI declines to draw them".
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
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

# The namespace half of `finish`'s advisory lock. Two ints rather than one so a session
# id can never collide with another feature's lock on the same number.
_FINISH_LOCK = 6202


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
    # §13's hold-out draw, sealed against this evening. Frozen with the pool because that is the
    # right lifetime: the pair a seat is shown at a given answer count must be the same pair on
    # every read of it, and the pool is the thing the round is drawn from (decision 223). None
    # for a room started before this shipped — `_round_of` says what it falls back to and why.
    holdout_seed: str | None = None

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
        holdout_seed=raw.get("holdout_seed"),
    )


async def snapshot_of(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    context = await conn.fetchval("SELECT context FROM session WHERE id = $1", session_id)
    if context is None:
        raise RoundError("no_room", "no such session")
    return _as_snapshot(context)


async def _refuse_unscored_members(
    conn: asyncpg.Connection, session_id: int, *, bundle_version: str
) -> None:
    """§6.8's register, applied to the one refusal a household could not act on.

    `pool_rules.build` keeps only titles EVERY seated member has scored, so a member whose
    fold-in has not run for this bundle — a freshly created account before the 60 s tick, a
    worker that is down, the minutes after a re-import — removes every candidate, and the round
    then refused with `empty_pool`: "widen the budget or include rewatches". The host widens,
    retries, gets the same sentence, and nothing anywhere names the member. Reproduced over
    HTTP. [M4.12 finding 34; decision 216]

    ANY score for this bundle, and deliberately not a score of this session's kind. The kind-
    scoped question has a second answer — a household whose library holds no series at all —
    and a refusal that named a member for that would be this defect again with a different
    sentence. A member with movie scores and no series scores is told the library has nothing
    for tonight, which is true.

    The threshold stays zero rather than becoming §6.1's label count: `cs-16`'s other half would
    derive `has_profile` from a live observation count and let a thin member's seat carry the
    pool average, which rewrites §6.2 step 3's arithmetic and needs a number nobody has read off
    §6.1's curve (decision 216). `has_profile` therefore remains `role != guest`, and the role
    predicate below says so rather than leaning on a guest's NULL `user_id`: §6.2 step 3's
    "unless they have a grid profile" is a guest seat with an account attached, which M7 makes
    real and which the join alone would start naming in this refusal.
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
    raise RoundError(
        "unscored_member",
        f"no Ledger scores for {who} yet — tonight's pool is ranked from every member's "
        "scores, and the nightly fit writes them",
    )


async def start(conn: asyncpg.Connection, session_id: int) -> Snapshot:
    """Close the join window, build the pool once, and freeze it.

    §6.2 step 2's rule as the host's lobby states it — "Anyone who joins before you start is
    in" — is exactly this transition: after it the seats are fixed, which is what makes the
    participant count the averages are over a constant of the evening.

    THE CLAIM IS THE FIRST STATEMENT, AND ALL OF THIS IS ONE TRANSACTION. "Anyone who joins
    before you start is in" is a promise about two outcomes, and this function used to produce a
    third: it read the seats, built the pool and flipped the state LAST, so a join was admitted
    for the whole duration of the build — tens of seconds on a 696-title library — and the seat
    that arrived in that window had no entry in the frozen `scores` map. `pool_scores_for` is
    then empty, `round.boundary` is None, `stop_reason` is `converged` at zero answers, and
    `_end` is unreachable from every write path there is: nothing could ever close the room.
    Reproduced twice, including through the real HTTP routes. [M4.12 finding 5]

    ONE TRANSACTION RATHER THAN A CLAIM THAT COMMITS ON ITS OWN, for two reasons that point the
    same way. The first is the refusals below: `empty_pool` and `unscored_member` both raise
    AFTER the claim, and a room left in `voting` with no snapshot is a new instance of the defect
    this closes — worse than the original, because `state_for`'s belt-and-braces reads an empty
    snapshot as a seat that can never be asked anything and would end the evening before it
    began. Rolling back is the only release that cannot itself be skipped. The second is that a
    room must never be *visible* in `voting` without its pool: another device's GET of a seat's
    round lands during the build, and that is the same empty snapshot. This is `deps.write_txn`'s
    shape taken inside the rule rather than at the route, because the invariant belongs to the
    rule: every caller of `start` owes it, not only the handler. No advisory lock, because the
    claim's own row is the thing two callers contend for and the UPDATE already serialises them.

    That row lock also reaches `rooms.open_session`'s abandon clause, which is the behaviour its
    own test asserts: a host opening a second room mid-start waits for this transaction and then
    re-evaluates `state = 'open'`, so the room being started is skipped rather than abandoned
    underneath the build.

    `rooms.join` IS THE OTHER HALF, AND THE PREDICATE ALONE IS NOT ENOUGH. A join's
    `INSERT ... SELECT ... FROM session WHERE state = 'open'` reads under MVCC, so while this
    transaction is uncommitted it still sees `open` and seats the member anyway (measured: the
    insert succeeds mid-claim). `join`'s `FOR SHARE` is what makes the two statements serialise
    on the session row — it waits for this transaction and then re-evaluates its predicate, so
    it is refused if we committed and admitted if we rolled back, and a join that took the share
    lock first makes this UPDATE wait until its seat is visible to `seats_of` below.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "UPDATE session SET state = $2 WHERE id = $1 AND state = $3 "
            "RETURNING kind, runtime_budget_min, include_rewatches, bundle_version",
            session_id, rooms.STATE_VOTING, rooms.STATE_OPEN,
        )
        if row is None:
            # Three reasons share one empty result, and the caller's statuses differ (404 against
            # 409), so the second read is worth one round trip on a path that is already over.
            #
            # AND `ended_at` IS ONE OF THE THREE. The claim tests `state = 'open'` only, so a room
            # the household ended — or one that reached its reveal — came back as a room that "has
            # already started": false of an abandoned room, unactionable on either, and 409 where
            # the same module answers a join on the same row 404 "that session has ended"
            # (`rooms.join`), which is also what finding 8 put into `_participant`. A host whose
            # lobby outlived their own End taps Start and is sent looking for a round that does not
            # exist. §6.8's register is what a refusal owes, and this is one column on a statement
            # already being run. [M4.12 review cycle 1: M412-PLAY-3; decision 216]
            live = await conn.fetchrow("SELECT ended_at FROM session WHERE id = $1", session_id)
            if live is None:
                raise RoundError("no_room", "no such session")
            if live["ended_at"] is not None:
                raise RoundError("no_room", "that evening has ended")
            raise RoundError("already_started", "that room has already started")

        seats = await rooms.seats_of(conn, session_id)
        await _refuse_unscored_members(conn, session_id, bundle_version=row["bundle_version"])
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
            # saying so is better than serving a pair that does not exist. A pool of two or
            # three IS admitted: it has no shortlist boundary to resolve, so every seat ends
            # itself at zero answers and the evening goes straight to 54e's ballot rather than
            # being refused a round a household with three owned shows could never have
            # (decision 215).
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
                str(k): v
                for k, v in (await dna_reads.vectors_for(conn, ids, version=version or "")).items()
            },
            "axes": await dna_reads.axes_for(conn, version=version or ""),
            "version": version,
            # §13's draw, sealed for the evening (decision 223). It rides in the pool payload
            # rather than in a column because that is the lifetime it wants and jsonb costs no
            # migration; `secrets` rather than `random` because a client that could guess it
            # could pre-compute every hold-out pair of an evening it is about to be asked about.
            "holdout_seed": secrets.token_hex(16),
        }
        await conn.execute(
            # The dict, not a dumped string: `db/pool.py` registers a JSON codec on jsonb, so a
            # pre-dumped argument is encoded twice and lands as a JSON *string*. The state moved
            # in the claim above, so this statement carries the snapshot alone.
            "UPDATE session SET context = jsonb_set(context, '{pool}', $2) WHERE id = $1",
            session_id, payload,
        )
    return _as_snapshot({"pool": payload})


# --- one participant's round -----------------------------------------------------------------


# The seat every write path reads first, and the same statement with the seat's row locked. One
# column list rather than two copies of it: two statements here would drift, and the drift would be
# silent, because the locked read is the one every guard below is evaluated against.
_SEAT = """
    SELECT p.id, p.session_id, p.user_id, p.role, p.seat, p.tilt, p.answered_count,
           p.ended_by, s.state, s.ended_at
      FROM session_participant p JOIN session s ON s.id = p.session_id
     WHERE p.id = $1
"""
# `OF p`, and the `OF` is the load-bearing part: the statement joins `session`, so a bare
# FOR UPDATE would lock that row too and make every answer in the room queue behind `start`'s
# claim and behind `rooms.set_state` over a row this read only looks at.
_SEAT_LOCKED = _SEAT + " FOR UPDATE OF p"


async def _participant(
    conn: asyncpg.Connection, participant_id: int, *, lock: bool = False
) -> asyncpg.Record:
    """This seat as the write paths read it — optionally holding it until the caller commits.

    EVERY GUARD IN THIS MODULE WAS A CHECK-THEN-ACT ON AN AUTOCOMMIT CONNECTION, which is the one
    shape all of M4.12's write races have. Two taps on one card both read `answered_count = 0`,
    both satisfied `seq == answered_count + 1`, and the loser's INSERT collided with 0014's partial
    unique index. `app.py`'s `_conflict` (M4.7, decision 181) answers that 409
    `conflict: session_answer_seq` — a status the client re-reads on, but carrying no `reason` the
    round's contract defines, over a card that no longer exists, and naming a database object at a
    household. An undo interleaved with an answer was worse, because it left the counter behind
    the highest live seq, so every LATER tap collided too — for ever, on a seat nothing but an
    answer can end. [M4.12 findings 10 and 11]

    THE SEAM ANSWERS 409, AND THIS PARAGRAPH SAID 500 IN NINE PLACES. That was written from the
    plan's own finding 12, which cites an `app.py` that predates M4.7's handler; the tree has
    asserted the 409 since (`test_http_seam.py`, "the seam M4.10 and M4.12 plug into, asserted
    here so they can point at it"). The repairs below are unchanged and were never about the
    status code — the answer race left a reused seq and a wedged seat, and the ballot's loser had
    its whole vote rolled back — but the record of why has to be the seam's actual behaviour, or
    the next person to weigh an unguarded write against a lock reads this file and concludes this
    app answers a lost race with a 500. [M4.12 review cycle 1: M412-CONC-02]

    `lock=True` is only meaningful inside `async with conn.transaction()`, because that is what
    decides how long the lock is held; a locked read on an autocommit connection releases it with
    the implicit transaction of its own statement and buys nothing. It is M4.10's idiom
    (`api/deps.py`'s `write_txn`, and `finish`'s advisory lock below) applied at the three seams
    that still had none: the loser WAITS and then re-reads, so it refuses on what the winner
    actually committed rather than on what it read before the winner existed.
    """
    row = await conn.fetchrow(_SEAT_LOCKED if lock else _SEAT, participant_id)
    if row is None:
        raise RoundError("no_seat", "no such participant")
    if row["ended_at"] is not None:
        # A SEAT IN A ROOM THAT HAS ENDED IS NOT A SEAT. The statement selected the session's state
        # and never its `ended_at`, so an abandoned room kept serving every seat in it: the round
        # read answered 200 with a pair and a fresh card token, an answer landed in an evening that
        # was over, and the undo was accepted. On the client `tonight.svelte.js`'s `refresh` had
        # branches for revealed / `ballot` / `voting` only — this same diff adds the `abandoned`
        # one (finding 7's client half) and the `open` one (finding 15) — so the member's phone
        # showed a lobby for a room that no longer existed and nothing it asked for contradicted
        # that. One refusal here covers `state_for` and all three writes, which is why it is in
        # the read they share rather than in four handlers. [M4.12 finding 8]
        #
        # `ended_at` and not the state name: 0013's `session_ended_states` CHECK ties the timestamp
        # to `resolved` and `abandoned` together, so this asks the schema's own question and a later
        # ended state needs no edit here. `resolved` is in scope deliberately — an evening that
        # reached its winner has no round left to play either.
        #
        # `no_room` rather than a reason of its own: `_room_error` maps it to 404, which is what the
        # room now is, and §6.8's register for a thing that is not there.
        raise RoundError("no_room", "that evening has ended")
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


async def _round_of(
    snapshot: Snapshot, row: asyncpg.Record, answers: list[round_rules.Answered], *, z: float,
) -> round_rules.Round:
    """One seat's whole round, replayed off the event loop.

    THE SEARCH IS CPU AND THE LOOP IS SHARED. `round.select` is the only expensive thing Tonight
    does — vectorised in this same milestone, but still a `searchsorted` over every straddling
    pair of a 696-title pool, and `axis_positions` below is a second pass over every candidate's
    DNA vector. Run inline it stops everything else this process is doing for its whole duration:
    the other phone's GET of its own round, the WebSocket hub's next frame, `/api/health`. §6's
    preamble budgets 1.5 s per battle and §2 names a 4 vCPU box, so "fast enough on my laptop" is
    not the measurement. Nothing in here touches the database or the clock, which is what makes a
    thread legal at all; the numpy half releases the GIL, which is what makes it worth having.
    [M4.12 finding 2]

    One hop for both pure halves rather than one per call: `axis_positions` feeds `replay`, and
    two `to_thread` calls would pay the switch twice to compute one round. The generator crosses
    into the thread with them and is not a second thing to synchronise: it is made here, per call,
    and belongs to the single caller awaiting this.

    AND THE DRAW IS SEALED HERE, WHICH IS THE WHOLE OF §13's GUARD ON THIS SURFACE. The route used
    to hand its own `SystemRandom` down, so the hold-out pair was redrawn on every GET and nothing
    persisted it: twelve reads at `answered = 9` returned twelve DISTINCT "uniform-random" pairs,
    every one of them sealed into a valid card token, and answering with the first-minted one was
    accepted — §13's only admissible stream, chosen by the client. Honest clients hit it too, since
    `refresh()` re-reads the round on every `rooms.changed` / `lobby` / `reveal` frame and on
    reconnect, so the card changed under the person's thumb after a screen lock. The generator is
    now a function of (the nonce frozen with the pool, this seat, this answer count), so the pair
    is a fact about the round rather than about when it was asked, and the token minted from it is
    byte-identical on every read. [M4.12 findings 28 and 29; decision 223]

    `seq` IS THE LIVE ANSWER COUNT PLUS ONE — the same number `replay` selects at — so an undo
    re-opens the seq it retracted with the same card rather than a new one. That is what closes
    finding 29 by construction instead of by a second check: a token stashed before the undo is
    the token the server re-issues after it, so replaying it names the pair the seat is looking at.

    The fallback keys a room started before this shipped by its own ids. It is weaker on purpose —
    session and participant ids travel in URLs, so a client could compute the seed — and that
    costs nothing the seal is about: predicting a uniform draw is not choosing it, and the
    alternative is changing the pair under the thumb of an evening already in progress.
    """
    is_member = row["role"] != rooms.ROLE_GUEST
    prior = snapshot.pool_scores_for(row["id"]) if is_member else snapshot.member_average()
    escaped = row["ended_by"] == round_rules.ESCAPE
    seq = len(answers) + 1
    seed = snapshot.holdout_seed or str(row["session_id"])
    rng = random.Random(f"{seed}:{row['id']}:{seq}")

    def played() -> round_rules.Round:
        return round_rules.replay(
            prior, answers, z=z, has_profile=is_member,
            axes=combine_rules.axis_positions(snapshot.dna, snapshot.axes),
            # The seat, and never the seed: 54b's arm has a second caller with no participant and
            # no pool, so the key is the one thing both can name (decision 223). A group answer's
            # arm is stored on its row anyway, drawn from the sealed pair at serve time — this is
            # what decides the arm of the pair that has not been answered yet.
            holdout_key=str(row["id"]),
            rng=rng, escaped=escaped,
        )

    return await asyncio.to_thread(played)


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
    """What one seat's device renders: the next pair, or that they are done.

    Carries the escape's availability rather than leaving the client to compute it from a
    count — 54c makes the control a property of the round's state, and a client that decided
    for itself would be a second implementation of the rule.

    ONE SHAPE FOR THE READ AND FOR THE THREE WRITES. `state_for` built this dict, and the answer,
    undo and escape routes got theirs by calling `state_for` again after their own write — a
    second decode of the frozen pool out of jsonb and a second run of the whole round, per tap
    (finding 3). They build it from the round they have already replayed now, so the shape has to
    live where all four can reach it: a second copy would drift, and the copy that drifted would
    be the one the phone sees after a tap rather than the one it sees on a reload.

    `played is None` is a seat with no round to report, which is two real states rather than a
    defensive branch: the escape, whose seat is over before anything would be selected for it,
    and the stale ending in `_next_card` below. The pair and the stop reason are mutually
    exclusive in `replay`'s own return, and that invariant is kept here rather than in each
    caller.
    """
    pair = None if played is None or stop_reason is not None else played.next_pair
    candidates = {} if snapshot is None else snapshot.candidates
    return {
        "participant_id": participant_id,
        "answered": answered,
        "ended_by": ended_by,
        "stop_reason": stop_reason,
        # THE ESCAPE CLOSES WITH THE ROUND. The flag was the count alone, so a seat that had
        # converged, hit the cap or taken the escape itself kept advertising a control `escape`
        # answers with `round_over` — on the one surface whose docstring above says the availability
        # travels precisely so the client does not decide for itself. `round.escape_available` stays
        # the pure predicate about the count; whether this seat may still be asked anything is a
        # fact about the seat, and it is added here rather than folded into the predicate so the two
        # questions keep their own homes. [M4.12 finding 9]
        "escape_available": ended_by is None and round_rules.escape_available(answered),
        "cap": round_rules.CAP_PAIRS,
        "pair": None if pair is None else {
            "selection": pair.selection,
            "reason": pair.reason,
            "a": candidates.get(pair.title_a),
            "b": candidates.get(pair.title_b),
        },
        "_pair": pair,
        "_round": played,
        "_snapshot": snapshot,
        # DID THIS CALL END THE SEAT? Private, like the three above it, and it exists for one
        # caller: `api/tonight.py`'s round read, the only handler that neither settles nor pushes
        # a frame. `state_for`'s belt-and-braces below is the ONLY thing that can end a seat on a
        # pool of two or three candidates (decision 215) -- there is no pair to answer and the
        # escape is refused below pair six -- so on such an evening the device that read the last
        # un-ended seat left the room in `voting` with every seat `converged`, woke nobody, and
        # issued no further read: the client's one GET of the session runs BEFORE its round read,
        # in the same `refresh()`, so the order the rule needs is the one order it never produces.
        # The flag rather than settling on every round read: the ordinary poll stays one statement,
        # and the read that actually moved the seat pays for the progress frame the other phones
        # are waiting on. [M4.12 review cycle 2: M412-PLAY-4; decision 215]
        "_ended_now": ended_now,
    }


async def _next_card(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    *,
    answered: int,
    answers: list[round_rules.Answered],
    snapshot: Snapshot,
    z: float,
) -> dict[str, Any]:
    """The card a write hands straight back — §6 preamble's "next card preloaded", paid for once.

    One snapshot read and one replay for the whole tap. The write paths used to return a small
    `{seq, stop_reason}` dict and let the route call `state_for`, which re-read the frozen pool
    (on the order of a megabyte of jsonb on a 300-title pool, decoded into four dicts on every
    read) and replayed the round a second time to produce a card the write had already computed.
    [M4.12 finding 3]

    AND THE ENDING IS DECIDED HERE, OUTSIDE THE SEAT'S ROW LOCK, WHICH IS WHY IT IS CONDITIONAL.
    Finding 2 moves the replay off the event loop, and a thread that is awaited inside
    `record_answer`'s transaction would hold the `FOR UPDATE OF p` lock for the whole search —
    the one thing finding 2 exists to stop. So the replay runs after the commit, and in that
    window another request for this seat can commit its own write: an undo from the same phone,
    which is the interleaving finding 11 is about. Ending a seat on a replay whose answer count
    is no longer the seat's would report somebody who is still playing as finished, and nothing
    but an answer can reopen a round — finding 6's stranding arriving through this repair's own
    door. `_end`'s `when_answered` guard is in the statement, and when it refuses, this replay
    describes a seat that no longer exists: the card then reports the row and carries no pair, so
    the client re-reads (`tonight.svelte.js`'s `answer`, which refreshes when a write hands back no
    pair) rather than acting on a round that has moved.

    A crash between the commit and the ending leaves the seat un-ended with its answers intact,
    which is exactly the state `state_for`'s belt-and-braces was written for: the next read of
    the round ends it and `settle` moves the room on. One fewer thing inside the transaction is
    not one more thing that can be lost.

    `row` is the one the caller's write locked, not a re-read of it: `_round_of` takes the seat's
    role, its id and whether it was escaped, and a write that adds an answer moves none of the
    three. `answered` is passed separately precisely because it IS the one thing that moved.
    """
    played = await _round_of(snapshot, row, answers, z=z)
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


async def state_for(
    conn: asyncpg.Connection, participant_id: int, *, z: float
) -> dict[str, Any]:
    """What one participant's device renders, read fresh: the next pair, or that they are done.

    The shape is `_card`'s, which the three write paths also return — this is the reload, not the
    thing that runs after every tap (finding 3).
    """
    row = await _participant(conn, participant_id)
    snapshot = await snapshot_of(conn, row["session_id"])
    answers = await _answers(conn, participant_id)
    played = await _round_of(snapshot, row, answers, z=z)

    answered, ended_by = row["answered_count"], row["ended_by"]
    stop_reason = played.stop_reason
    ended_now = False
    if stop_reason is not None and ended_by is None:
        # THE BELT AND BRACES THAT MAKES A STRANDED SEAT UNREPRESENTABLE. `replay` returns a
        # reason and a pair that are mutually exclusive, so a reason here means there is nothing
        # this seat can be asked — and until this line the only ways to record that were an
        # answer (which needs a pair) and the escape (refused below five answers). A seat the
        # round cannot ask anything therefore kept `ended_by` NULL for ever,
        # `everyone_finished` stayed false, and no route could close the room: the lobby said
        # `voting`, the ballot answered with an empty slate, every write was refused. Two
        # ordinary evenings land here — a pool of two or three candidates, which has no
        # shortlist boundary to resolve (decision 215), and a seat the frozen snapshot holds no
        # scores for (finding 5). [M4.12 finding 6]
        #
        # A WRITE ON A READ, deliberately: this is the same shape as `settle` one screen down,
        # and for the same reason. The household is in the room holding a phone, so the rule has
        # to fire where the state is computed rather than in a sweep that measures its latency
        # in minutes. Idempotent by the `ended_by IS NULL` guard, so re-reading the round is not
        # a second ending; the transition to the ballot is picked up by the next `settle`, which
        # every read of the session, the ballot and the result calls — AND, since this write is
        # the only one a two- or three-candidate evening has, the round read itself, which
        # announces when `_ended_now` comes back true. That sentence used to name three reads the
        # client makes in the wrong order for it: `refresh()` reads the session and THEN the
        # round, so the read that ended the last seat was followed by nothing at all.
        # [M4.12 review cycle 2: M412-PLAY-4]
        #
        # Stamped with the replay's OWN reason rather than a fourth value: 0013's
        # `session_ended_states` CHECK admits `converged | cap | escape` and is
        # sha256-checksummed, and `converged` is honest about the mechanism — the boundary is
        # empty — if generous about the word (decision 215).
        #
        # The payload reports THIS read's reason, while `_end`'s own guard means the ROW keeps
        # whichever writer arrived first. The two differ only when an escape and this read land
        # together, and then the row is the one that matters: it is what §14 risk 6 counts, and
        # to the device both readings say the same thing — this seat is done.
        #
        # AND `when_answered`, like the write paths one screen down. This read ships with a replay
        # off the loop (finding 2), so the gap between `_answers` above and this statement is a
        # real await point in which another request for the seat commits — an undo, which lowers
        # the count and gives the round a pair again. It first shipped without the guard, argued
        # as "a read that refused to end a stranded seat because the count moved under it would be
        # the seat nothing ends again". That is false in both halves: the writer that moved the
        # count evaluates the ending for its own count in `_next_card`, and if it dies before
        # doing so the NEXT read of this seat replays the answers that are actually there and ends
        # it correctly. Refusing costs one read; ending on a stale reason costs the round and
        # nothing but an answer can reopen it. `_card` suppresses the pair on the stop reason and
        # never on `ended_by`, so the stamped seat was then served a live pair it could not
        # answer — every tap 409 `round_over`, re-read, re-rendered, until a frame dragged the
        # room to the ballot. The belt-and-braces case is untouched: an unrankable seat has
        # `answered_count = 0` and no writer that can move it.
        # [M4.12 review cycle 1: M412-CONC-01]
        #
        # TWO REFUSALS, and they are not the same refusal. A count that moved means this replay
        # describes a round the seat no longer has, so the card reports the ROW and carries no
        # pair — `_next_card`'s shape, and the client re-reads rather than acting on it. An
        # `ended_by` that is already set means another writer got there first, which is the
        # deliberate case above: the row keeps theirs, the payload reports this read's reason, and
        # to the device both readings say the same thing.
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
    z: float,
) -> dict[str, Any]:
    """Write one answer, move the tilt, and hand back the next card.

    Inside one transaction: the row, the counter and the tilt are one fact, and a crash between
    them would leave `answered_count` disagreeing with the rows it counts — which is the number
    the lobby and the waiting screen both display.

    AND THE GUARDS ARE INSIDE THAT TRANSACTION NOW, BEHIND THE SEAT'S ROW LOCK. They used to sit
    above it on an autocommit connection, so two taps on one card both passed the seq check and
    the loser's INSERT collided with 0014's partial unique index — which `app.py`'s `_conflict`
    answers 409 `conflict: session_answer_seq`. The client re-reads the round on a 409 and only on
    a 409 (`tonight.svelte.js`'s `answer`, whose sole re-read is its 409 branch), so the second tap
    did re-read; what it got was a constraint name where §6.8's register wants a sentence, over a
    card that no longer existed, and a `session_answer` row whose seq had been reused. Reproduced 5
    races of 6. The lock makes the loser wait, re-read and refuse on `stale_pair`, which the
    existing mapping already answers 409: the same status by the statement order rather than by
    how fast the two phones were, and with a reason the round itself defines.
    [M4.12 finding 10; M4.12 review cycle 1: M412-CONC-02]

    0014's index stays as the backstop, and nothing here catches a `UniqueViolationError`: a
    repair that caught one would be this same check-then-act with the race moved into an except
    branch, and `app.py`'s own handler for it is M4.7's.

    THE SEQ IS MINTED FROM THE ROWS, NOT FROM THE COUNTER. `seq` arrives in the sealed card as
    `answered_count + 1` and is checked against exactly that, because that is §13's single-use
    seal — answering moves the counter, so a replay is a stale card. But the row's own seq is the
    next one that has never been issued, tombstones included, which makes the two numbers
    independent: `answered_count` is displayed progress and `seq` is identity. They were one
    number before, and an undo interleaved with an answer is what that cost (finding 11, and
    `retract` below).

    THE ROUND IS REPLAYED AFTER THE COMMIT, WHICH IS THE WHOLE POINT OF `_next_card`. The tilt and
    the counter do not depend on it, and the search is tens of seconds of CPU on a real pool: held
    inside this transaction it held the seat's row lock for its whole duration, so the undo button
    and the next tap queued behind a computation neither of them needs. `rng` travels from the
    route for the same reason the card does — the hold-out arm draws its pair from it (§13), and a
    card selected under a different generator than the reload's would be the route quietly using
    two selectors.
    """
    async with conn.transaction():
        row = await _participant(conn, participant_id, lock=True)
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
        # Tombstones included, which is the whole point: §14 risk 6 keeps a retracted answer as a
        # row, so reusing its seq is how the replacement answer collided with it. The count is
        # read under the lock, so there is no second writer between this and the INSERT.
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
        # §6.2 step 5's tilt. Held-out answers move it as little as they move the posterior:
        # 54b says they are "used for neither selection nor stopping", and the tilt feeds the
        # tonight score the shortlist is built from, so it is the same stream.
        tilt = dict(row["tilt"] or {})
        if pair.selection != round_rules.SELECTION_HOLDOUT:
            # One helper, three callers (here, `retract` below and `solo.picks`), because the
            # four-branch dispatch had already drifted between them — see `tilt.applied`. The
            # §10 membership check travels inside it, which costs nothing here (the pair is
            # minted from this same frozen snapshot) and is the whole point on the other two.
            tilt = tilt_rules.applied(
                tilt, answer=answer, title_a=pair.title_a, title_b=pair.title_b,
                vectors=snapshot.dna, frame=snapshot.frame(),
            )
        # The literal rather than `answered_count + 1` in SQL, because the locked read above is
        # what makes it exact — and because `_set_answered` can only hold the invariant if it is
        # handed the number the caller means to write.
        await _set_answered(conn, participant_id, count=row["answered_count"] + 1, tilt=tilt)
        answers = await _answers(conn, participant_id)
    card = await _next_card(
        conn, row, answered=row["answered_count"] + 1, answers=answers, snapshot=snapshot, z=z,
    )
    # `seq` is the one that was WRITTEN, which after an undo is no longer the one the card carried.
    # The route prints it back as `wrote.seq`, and a number that named a different row than the one
    # in `session_answer` would make §14 risk 6's log unreadable from the outside.
    return {**card, "seq": written_seq, "tilt": tilt}


async def _set_answered(
    conn: asyncpg.Connection, participant_id: int, *, count: int, tilt: dict[str, float]
) -> None:
    """Move the displayed progress and the tilt together, and refuse to move the counter past the
    rows it claims to count.

    A COUNTER THAT DISAGREED WITH THE ROWS WAS THE WEDGE, not a symptom of it. While the row's seq
    WAS `answered_count + 1`, a counter the retract had left behind the highest live seq minted the
    next card at a seq that already existed, and every tap from then on was a unique violation —
    three taps after the interleaving that caused it, on a seat whose round nothing but an answer
    can end. Minting from the rows is what removes that particular collision; this is what keeps
    the counter itself honest, because it is still the number the lobby and the waiting screen
    display and §14 risk 6 still counts the rows. `max(seq) >= count` is the cheap form of it: one
    indexed aggregate in the same statement, and true of every legal history — an answer moves both
    by one, and a retract only ever lowers the count while the tombstone holds the seq.
    [M4.12 finding 11]

    THE GUARD IS IN THE STATEMENT, for `_end`'s reason one screen down: a check in a caller is a
    check another caller can be written without, and this one has two callers already. A raised
    `AssertionError` rather than the `assert` statement, because `python -O` strips the statement
    and this is what stands between a bug here and a seat whose progress nobody can read; and
    raised rather than logged, because both callers are inside a transaction, so refusing the write
    rolls back a seat that is still playable instead of committing one that is not.
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
    """0013's CHECK ties `converged_at` to `ended_by = 'converged'`, so the two cannot drift —
    §14 risk 6 wants the rate at which each of the three fires. True if this call ended the seat.

    `AND ended_by IS NULL`: THE FIRST ENDING RECORDED IS THE ONE THAT HAPPENED. Both callers
    check `ended_by` before they get here and neither ever means to overwrite one, but since
    `state_for` became a caller there are two writers that can race — a device reading the round
    of a seat that has just converged while the person taps "just pick for us" on another. Last
    write wins would report that seat as `converged` when the household escaped it, and §14 risk
    6's whole use for this column is the rate at which each of the three fires. The guard is in
    the statement rather than in the three callers because a check in a caller is a check another
    caller can be written without. [M4.12 finding 6]

    `when_answered`: AND THE REPLAY THIS REASON CAME FROM IS STILL THIS SEAT'S ROUND. The write
    paths replay after their transaction commits, because a search held inside it holds the seat's
    row lock (finding 2), so between the commit and this statement another request for the seat can
    land — an undo, which lowers the count and gives the round a pair again. Ending on the stale
    reason would report somebody who is still playing as finished, on a seat nothing but an answer
    can reopen. Every caller that can be raced passes it and reads the return: the two write paths
    because they moved the count themselves, and `state_for` because its own replay is off the loop
    too, so the same undo commits between the answers it read and this statement. `escape` is the
    exception and needs none — it decides inside the transaction that holds `FOR UPDATE OF p`, so
    nothing can move under it. [M4.12 findings 2, 3; M4.12 review cycle 1: M412-CONC-01]
    """
    ended = await conn.fetchval(
        "UPDATE session_participant SET ended_by = $2, "
        "converged_at = CASE WHEN $2 = 'converged' THEN now() ELSE NULL END "
        "WHERE id = $1 AND ended_by IS NULL AND ($3::int IS NULL OR answered_count = $3) "
        "RETURNING id",
        participant_id, reason, when_answered,
    )
    return ended is not None


async def retract(conn: asyncpg.Connection, participant_id: int, *, z: float) -> dict[str, Any]:
    """§6 preamble's "undo everywhere", reaching the round.

    Your own most recent live answer, and only while your own round is still running. Tombstone
    rather than DELETE (§14 risk 6: "log every vote"), and the tilt is recomputed from the
    surviving rows rather than subtracted — subtracting assumes the frame has not moved and the
    arithmetic is exact, and one of those is a floating-point hope.

    THE SAME ROW LOCK AS THE ANSWER, BECAUSE THE TWO INTERLEAVED INTO A DEAD END. This read, the
    choice of which row to tombstone and the recount all used to sit outside the transaction that
    writes, so an answer committing in the window was counted by the recount that was meant to
    exclude it: live seqs {1, 2, 4} with `answered_count = 3`, the next card minted at a seq that
    already existed, and every subsequent tap collided with it for ever — 409 `conflict:
    session_answer_seq` out of `app.py`'s handler, which is a status the client re-reads on and a
    round it can never advance — the exact dead end 0014's
    own comment says it removed, arriving through the undo the index was added for. Reproduced here
    as live {1, 3} with the counter at 2, and `UniqueViolationError` on (participant, seq) = (1, 3)
    two taps later. [M4.12 finding 11]

    Both writers take the seat's row, so they serialise: the undo either lands before the answer
    (and the answer's card is then stale, which the client re-reads on) or after it (and takes it
    back). Either is a correct evening. What neither is any longer is a counter that disagrees with
    the rows, which is why `_set_answered` writes it.

    AND IT RETURNS THE CARD, which is finding 3's other half and not only a saving. The route used
    to call `state_for` after this, so the undo's own payload cost a second pool decode and a
    second round; but `state_for` is also what ends a seat the round can no longer ask anything
    (finding 6), and an undo CAN reach that state — a round with fewer answers has a different
    boundary, and a pool can run out of distinct pairs. Returning the card from here without
    `_next_card`'s ending would leave that seat un-ended with nothing on the undo path to read it
    again: the client goes to `waiting` (`tonight.svelte.js`'s `loadRound`, which is where a card
    with no pair lands whichever tap fetched it) and the room never settles. So
    the belt and braces travel with the card.
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
        frame = snapshot.frame()
        tilt: dict[str, float] = {}
        for a in answers:
            if a.selection == round_rules.SELECTION_HOLDOUT:
                continue
            # The rebuild reads the same rows `round.replay` reads and must skip the same ones:
            # §10 lets a re-import take a title out from under a stored answer, and a tilt that
            # counted a row the posterior ignored would leave the two describing two different
            # histories of one evening. `tilt.applied` holds that predicate for all three callers.
            tilt = tilt_rules.applied(
                tilt, answer=a.answer, title_a=a.title_a, title_b=a.title_b,
                vectors=snapshot.dna, frame=frame,
            )
        await _set_answered(conn, participant_id, count=len(answers), tilt=tilt)
    card = await _next_card(
        conn, row, answered=len(answers), answers=answers, snapshot=snapshot, z=z,
    )
    return {**card, "retracted_seq": last["seq"]}


async def escape(conn: asyncpg.Connection, participant_id: int) -> dict[str, Any]:
    """54c's "just pick for us": end this seat's round on what is known so far.

    Refused before pair 6 rather than ignored — a control that silently does nothing is worse
    than one that is not there — and recorded as `escape` so §14 risk 6 can count it.

    The same locked read as the answer and the undo, because this decides on `answered_count` and
    the other two move it: without the lock "just pick for us" tapped on the fifth answer's reply
    can be refused `too_early` on a count that has already reached six, or admitted on one that
    has just been undone back to five. One writer at a time on a seat, so the refusal is evaluated
    against the count that is actually there. [M4.12 finding 10]

    The card it returns needs neither the frozen pool nor a round: the seat is over, so there is
    nothing to select, and `replay`'s own answer for an escaped seat is the reason just written.
    The route called `state_for` here, which decoded the whole pool out of jsonb and replayed every
    answer in order to be told that (finding 3). Zero of each is the honest cost of ending a round.
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
) -> combine_rules.Slate | None:
    """§6.2 step 5, against the stored rows, persisted to `session_result`.

    The slate is written rather than recomputed on read: §4.2 gives the round a durable
    per-title table, and a slate re-derived later cannot be compared against the votes that
    produced it — which is what §14 risk 6 exists to require.

    None, and nothing written, when the household ended the evening while this combine was
    running. The transition at the foot of the transaction below says why that is a predicate on
    the session row rather than a bare write.
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
        # Off the loop, one seat at a time, for `_round_of`'s reason: this is the same search, and
        # it runs once PER SEAT on the answer that finishes the room — the request a household is
        # most obviously waiting on. Sequentially rather than gathered: the combine is one slate
        # over all of them, and four threads competing for a 4 vCPU box (§2) finish no sooner.
        played = await asyncio.to_thread(
            round_rules.replay, prior, answers, z=z, has_profile=is_member,
            # The seat, as `_round_of` keys it. The combine reads only the beliefs, so the arm
            # decides nothing here — but a key it could reach a different answer with would be a
            # second definition of the same thing (decision 223).
            holdout_key=str(seat["id"]),
            # AND NO PAIR, because nobody is shown one: the line below reads `played.beliefs` and
            # nothing else. Left at the default this searched O(n^2) over the straddling set for a
            # pair it then discarded — 95-101 ms per seat at 696 candidates, against 0.7-1.0 ms
            # with the flag off — and it did so precisely for the seats that end at 54c's escape,
            # since a converged seat has nothing left to straddle and a capped one short-circuits.
            # Two or three "just pick for us" taps is 200-300 ms of §6's 1.5 s budget spent on
            # pairs that do not exist. `select` never touches the beliefs (they are accumulated
            # before the flag is read), which is why `solo.picks` can already pass it for the same
            # reason (finding 35). [M4.12 review cycle 1: M412-PLAY-2]
            select=False,
        )
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
        # 54e's reveal is simultaneous, so the combine runs on whichever answer finishes the
        # room — and when two people finish at the same moment, that is both of them.
        # `settle` below reads the session state and calls this in a separate statement with
        # nothing in between, and `everyone_finished` turns true the instant the last of the two
        # final answers commits. Both callers then arrive here, and the second one's DELETE
        # cannot see the first's uncommitted rows: it deletes nothing, inserts, and is refused
        # by `session_result_pkey` — 409 `conflict: session_result_pkey` out of `app.py`'s
        # handler on the last answer of somebody's round, or on whichever read called `settle`,
        # over a room that is by then perfectly fine.
        #
        # The lock rather than a caught exception, because it makes the loser WAIT and then do
        # the work correctly: by the time it proceeds the winner has committed, so its DELETE
        # sees those rows and replaces them, and the room ends with one slate either way. It is
        # taken inside this transaction, after the slate is computed, so the LLM call above is
        # not holding it. `ballot.resolve` was made idempotent for this same beat at the other
        # end; this is the same promise on this end.
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _FINISH_LOCK, session_id)
        # AND THE ROOM IS STILL THE ONE THIS COMBINE WAS RUN FOR. The transition at the foot of
        # this transaction used to be a bare `set_state(..., 'ballot')` — a statement with no
        # predicate, and `set_state` writes `ended_at = NULL` for every state that is not an ended
        # one, which makes it the only write in this codebase that can move a session from an
        # ended state back to a live one. `settle` reads the state in its own unlocked statement
        # and the whole of this function runs between that read and that write, so a host's End
        # committing inside the window was silently undone: their phone had its 200
        # {"state": "abandoned"} and left the room, and the room came back in `ballot` with
        # `ended_at` NULL — on §6.2 step 2's open-rooms list for every household device and
        # holding its code against `session_room_code_live`. That is precisely the room decision
        # 169's control exists for, and every other phone in it is polling the lobby and therefore
        # calling `settle`. The advisory lock above serialises two combines and nothing else;
        # `rooms.end_session` contends on the session ROW, so the two met on nothing at all.
        #
        # `ended_at IS NULL` rather than `state = 'voting'`, for `_participant`'s reason: 0013's
        # `session_ended_states` CHECK ties the timestamp to `resolved` and `abandoned` together,
        # so this asks the schema's own question, and re-running the combine over a room already
        # in `ballot` stays the idempotent thing the lock above argues for. `FOR UPDATE` takes the
        # same row `end_session` takes, so the two serialise the way `start`'s claim and
        # `end_session` already do: an End arriving after this point waits and then ends the
        # balloted room, which is a correct evening. Returning before the DELETE is what makes the
        # refusal free — there is nothing to roll back.
        # [M4.12 review cycle 1: M412-PLAY-1; decision 169]
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
                     reserved)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                session_id, row["title_id"], row["rank"], row["slot"], row["group_score"],
                matches.get(row["title_id"], {}),
                slate.conflict if slate.conflict and row["slot"] != "runner_up" else None,
                # 54d's "**labelled as such**", persisted with the slate rather than recomputed:
                # the same reason the slate itself is written down, since which card was the
                # counterweight cannot be re-derived once the axes move under it. False on every
                # row of every night that surfaced no split, which is all of them on the shipped
                # bundle. [decision 220; migration 0021]
                row["reserved"],
            )
        await rooms.set_state(conn, session_id, rooms.STATE_BALLOT)
    return slate


async def settle(conn: asyncpg.Connection, session_id: int, *, z: float) -> bool:
    """Move a room whose every seat has ended on to the ballot. True if THIS call moved it.

    §6.2 steps 5-6 (54e). THE LIFECYCLE NEEDS AN OWNER A READ CAN CALL. This transition used to
    exist only inside `api/tonight.py`'s `_announce`, which made a phone's POST its only caller —
    so a combine that raised once left the answer standing, the request 500ing, and the room in
    `voting` for the rest of the evening with every seat's `ended_by` set: the lobby reported
    `voting`, the ballot answered 200 with an empty slate, the result answered 409 `still_voting`,
    and every write was refused because each seat had finished. The votes were all in the
    database and nothing could reach them; recovery was SQL against `session`. A dropped
    connection and a container restart mid-request are both ordinary, and one of them costs the
    evening. [M4.12 finding 4]

    So the rule lives here and every read calls it: the session, the ballot and the result. That
    is cheap — two counts on indexed columns in the common case, and the common case is a room
    that has not finished — and it is safe to repeat by construction rather than by arrangement:
    `finish` above takes `pg_advisory_xact_lock` and then DELETEs and re-INSERTs the slate in one
    transaction, and `ballot.resolve` is idempotent at the other end.

    NOT A WORKER SWEEP, and nothing swallowed. A sweep measures its own latency in minutes and
    the household is waiting now, in the room, holding a phone. And a settle that caught its own
    exception would answer 200 over a room it had failed to move — which is how this defect
    survived a milestone with a green suite: the fault has to travel, to the log and to the
    status code, so the combine's own failure is visible where it happens.
    """
    if not await everyone_finished(conn, session_id):
        return False
    state = await conn.fetchval("SELECT state FROM session WHERE id = $1", session_id)
    # The state is the claim, not the seat counts: a room already in `ballot` (or resolved, or
    # abandoned) has had its combine, and a room still `open` has no snapshot to combine — and
    # `everyone_finished` is vacuously true of a session with no seats at all.
    if state != rooms.STATE_VOTING:
        return False
    # The state read above is a check-then-act over the whole combine, which is seconds on a real
    # pool. `finish` re-asks the question under the session row's own lock and answers None when
    # the household ended the evening in that window, so "THIS call moved it" stays true of the
    # return value rather than of the read that preceded it. [M4.12 review cycle 1: M412-PLAY-1]
    return await finish(conn, session_id, z=z) is not None


__all__ = [
    "RoundError",
    "Snapshot",
    "escape",
    "everyone_finished",
    "finish",
    "progress",
    "record_answer",
    "retract",
    "settle",
    "snapshot_of",
    "start",
    "state_for",
]
