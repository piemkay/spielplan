"""The Rank surface's routes. Spec v2.1 §6.3, §6.7, §13; decisions 11, 18, 117.

Thin, like `api/rate.py`: every rule lives in `spielplan.rank` and the only things decided here
are the HTTP shapes. Three of them are load-bearing.

**A queue answer names a sealed pair, never two title ids.** §13's guard says a held-out pair
must be identifiable end to end and never recorded as boundary-targeted — and a route that
accepted `{"title_a": 4, "title_b": 9, "arm": "boundary"}` would let the client decide which
stream a comparison belonged to. The pair is drawn on the server, sealed with `itsdangerous`
under `SESSION_SECRET`, and handed back opaque; the arm the duel is written with comes out of
that seal and out of nothing else.

**And the seal is single-use, under a lock.** It carries the count of comparisons the person
had answered when it was drawn; answering moves that count, so a second answer under the same
seal is a stale card and gets §6.1's 409. Without it the seal was user-bound but replayable,
and §13's agreement figure counts *rows* - so N replays of one uniform-random pair weight one
judgement N-fold in the only number §13 admits, and §4.2's tables are append-only so it cannot
be taken back. §6.1 reaches the same property through `rate_session.card_token`, which it can
because a rating session already has a row; a queue pair has none, and the observations already
imply the number, so no table is invented to hold one. The count is only a token while nothing
can read it twice before either write lands, which is what `_ANSWER_LOCK` is for.

**Every response leaves through `rail.redact` - including the queue's.** Decision 117 governs
"the rail and every inline annotation". `board.Entry.public()` already omits `s` and σ; the
`model` block that carries them is assembled here and removed by the gate when the toggle is
off. `GET /api/rank/queue` was the one route on this surface that called neither `visible_to`
nor `redact`, and `queue.Pair.public()` ships the arm together with the sentence "uniform-random,
held out - this pair never tunes the model": every member was told, in words, which of their
answers the model ignores. Those are §13's only admissible evaluation data, and a person told
an answer does not count has been given a reason to answer it carelessly. The arm and its
sentence travel under `model` now; the pair itself carries an arm-independent why-line in
§6.8's register. [M4.10 finding 16]

**`kind` is a single value, and required.** §4.1 rule 5 partitions every ranking surface, and
§6.3's board is one tier list at a time — decision 18's both-on case renders "two headed
sections", which on this surface means two boards, one request each. An absent `kind` would be
the unpartitioned query rule 5 exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import random
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from spielplan.api.artifacts import RESTART_REQUIRED, RESTORE_REQUIRED
from spielplan.api.deps import DB, ActiveUser, write_txn
from spielplan.core.config import settings
from spielplan.db import library
from spielplan.home import rail
from spielplan.ledger import hyperparams, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.models import artifacts
from spielplan.rank import drop as drop_rules
from spielplan.rank import evaluation, queue, read, tiers

log = logging.getLogger("spielplan.api.rank")

router = APIRouter(prefix="/api/rank", tags=["rank"])

Kind = Literal["movie", "series"]

# A salt of its own, so a sealed queue pair can never be presented as a session cookie or the
# other way round. Rotating SESSION_SECRET invalidates both, which is §2's stated behaviour.
_PAIR_SALT = "spielplan/rank/pair/v1"

# The namespace half of `answer`'s advisory lock. Two ints rather than one, so a user id can
# never collide with another feature's lock on the same number - `tonight/play.py:48` says the
# same thing about `finish`, and 6202 is its number. `deps.write_txn`'s single-argument
# `hashtext(name)::bigint` form is a third lock space again, so a name there cannot collide with
# a number here either. [M4.10 finding 1]
_ANSWER_LOCK = 6303

# §6.8's register, and deliberately the same line whichever arm drew the pair. What it replaces
# is `queue.Pair.reason`, which names the arm - and on the held-out tenth says so in as many
# words. A why-line is owed under §6.8 ("every shelf, recommendation, question and conflict
# carries a one-line why"); what is not owed is telling the person which of their answers §13
# will ignore. The arm's own sentence survives under `model`, for the reader decision 117 opens
# the rail to. [M4.10 finding 16]
_QUEUE_WHY = "one more comparison sharpens your board"

# The draw is a function of the queue position (`_queue_rng`), so production has no ambient
# generator and this stays None. It is the seam the tests reach for: `random.Random(7)` here
# makes the arm and the pair reproducible, and the two §13 route tests below need that because
# their old shape - draw sixty times and hope all three arms turn up - is red once per 285 CI
# runs on the only route-level proof of the guard. Production never seeds. [M4.10 finding 30]
_rng: random.Random | None = None


def _sealer() -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret, _PAIR_SALT)


def _seal(user_id: int, kind: str, pair: queue.Pair, answered: int) -> str:
    return _sealer().dumps(
        {
            "u": user_id,
            "k": kind,
            "a": pair.title_a,
            "b": pair.title_b,
            "arm": pair.arm,
            "n": answered,
        }
    )


def _unseal(token: str, *, user_id: int) -> dict[str, Any]:
    try:
        payload = _sealer().loads(token)
    except BadSignature as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "stale_pair", "message": "that pair is no longer on the table"},
        ) from exc
    if payload.get("u") != user_id:
        # One person's sealed pair answered by another would write a duel into the wrong
        # ledger. The seal proves the server drew it; the id proves who for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that pair belongs to another account")
    return payload


def _queue_rng(user_id: int, kind: str, answered: int) -> random.Random:
    """The generator behind one draw, keyed on the queue position rather than on the clock.

    `queue.draw` rolls the arm before it draws the pair, so a generator that was fresh per
    request made the arm a function of how many times the person pressed reload: reload until
    the pair is held out, or until it is not, then answer. That is worse than a pair-level door
    - §13 needs the held-out *rate* to be independent of the model, and a rate the client can
    steer by refreshing is independent of nothing. Keyed on `(user, kind, answered)` the draw is
    a function of what the person has answered instead: the same pair however many times they
    look at it, and a different one the moment they answer. HMAC rather than a bare hash because
    a client that could predict tomorrow's arm from today's pairs is back where it started, and
    `SESSION_SECRET` is the key this surface already trusts with the seal.

    No persisted nonce, checked against the tree: there is no per-user jsonb to hold one and a
    column for it would be a migration this milestone does not need. [M4.10 finding 15]
    """
    if _rng is not None:
        return _rng
    digest = hmac.new(
        settings().session_secret.encode(),
        f"{user_id}:{kind}:{answered}".encode(),
        hashlib.sha256,
    ).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _draw(
    pool: list[queue.Candidate],
    *,
    user_id: int,
    kind: str,
    answered: int,
    asked: set[frozenset[int]],
) -> queue.Pair | None:
    """One pair, through the one place the derivation and the test seam meet.

    Both paths go through here on purpose: a seam the production draw does not use is a seam
    that tests something else. [M4.10 findings 15, 30]
    """
    return queue.draw(pool, rng=_queue_rng(user_id, kind, answered), asked=asked)


def _hyperparams(request: Request) -> Hyperparams:
    """§4.3's constants, from the active bundle when there is one.

    `app.state.hyperparams` is where the lifespan's one read lands (`app.py`), and that is the
    normal path: §10 makes a bundle swap a restart, so nothing in a running process can change
    these numbers and re-reading the file per request bought nothing but a file read, a
    `from_mapping` validation and a discarded note list on every board.

    The fallback stays because tests construct the app without its lifespan, and because §3.1
    makes a bundle-less household legal — there the defaults are the honest answer. What it no
    longer does is let `from_mapping`'s `ValueError` out: a hand-edited or badly restored
    constant turned every Rank board and every Rate write into a 500 for everyone, with nothing
    on screen saying why. Refused with a 503 instead, and deliberately NOT defaulted: the
    defaults have a different `hp_digest`, so serving them would invalidate every cached fit in
    the install and re-fit the whole household behind a number nobody chose. [M4.10 finding 10]

    `OSError` beside `ValueError` for the reason `api/rate.py:_hyperparams` gives: `hyperparams.load`
    no longer reads a present-but-unopenable constants file as an absent one, and a file that cannot
    be opened is the same refusal as a file that cannot be parsed. [M4.10 cycle 1, M410-R1-06]
    """
    cached = getattr(request.app.state, "hyperparams", None)
    if cached is not None:
        return cached
    try:
        hp, notes = hyperparams.load(getattr(request.app.state, "artifacts", None))
    except (ValueError, OSError) as exc:
        log.error("ledger_hyperparams.json is unusable: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ledger constants unreadable - see backend log"
        ) from exc
    for note in notes:
        log.debug("hyperparameters: %s", note)
    return hp


async def _assert_active_basis(request: Request, conn: asyncpg.Connection) -> None:
    """§10's invariant on the Rank request path. One sentence of reasoning, in
    `api/rate.py::_assert_active_basis`, which states it at length; this is the same guard on the
    same window, and both refusals carry `api/artifacts.py::RESTART_REQUIRED` verbatim so the two
    surfaces and the import screen say one thing.

    At the top of the route, before `drop_rules.drop` and before the answer is written: M4.10
    finding 8 measured what a refusal raised after a durable write costs (a 500 over a row the
    person cannot take back, and a retry that wrote a second one). [M4.13, arch-03, data-01]

    Both arms, for the reason `api/rate.py` gives at length: a broken install's store carries the
    active version, so the comparison above passes and only the flag can refuse it. Drop and queue
    answer fit exactly as verdict and duel do. [M4.13 cycle 1]
    """
    store = getattr(request.app.state, "artifacts", None)
    if store is None:
        return
    try:
        store.assert_matches(await artifacts.active_bundle_version(conn))
    except RuntimeError as exc:
        log.error("refusing to score or refit: %s", exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "bundle_swapped", "message": RESTART_REQUIRED},
        ) from exc
    try:
        store.assert_not_broken()
    except RuntimeError as exc:
        log.error("refusing to score or refit: %s", exc)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "bundle_broken", "message": RESTORE_REQUIRED},
        ) from exc


def _basis(request: Request):
    """Which bundle this process is fitting in. See `api/rate.py::_basis`: the route's guard
    compares it to the active row before the write, and `refit.update_incrementally` re-asks after
    the board lock, because a tap can wait out the import that flips it. [M4.13 cycle 1]"""
    store = getattr(request.app.state, "artifacts", None)
    return refit.BASIS_UNSTATED if store is None else store.version


def _embeddings(request: Request, conn: asyncpg.Connection):
    """§5.1's coordinate source, with the version that describes it. See
    `api/rate.py::_embeddings`: the Backbone this process pinned at boot and the
    `title_placement` rows the cold half reads have to be the same bundle, and the `ledger_fit`
    stamp the fit earns has to name it. [M4.13, data-01]"""
    store = getattr(request.app.state, "artifacts", None)
    return observations.standard_embeddings(
        conn,
        getattr(request.app.state, "backbone", None),
        bundle_version=None if store is None else store.version,
    )


def _filters(
    q: str | None,
    genre: str | None,
    decade: int | None,
    runtime_max: int | None,
    runtime_min: int | None,
    seen: str,
    dna: str | None,
) -> library.RankFilters:
    return library.RankFilters(
        q=q, genre=genre, decade=decade, runtime_max=runtime_max,
        runtime_min=runtime_min, seen=seen, dna=dna,
    )


class DropBody(BaseModel):
    """§6.3's drop, from either input path — the pointer drag and the phone's tap-to-tier send
    the same body, because §6.3 gives them "the same `tier_edit` semantics"."""

    title_id: int
    tier: int = Field(ge=0)
    above: int | None = None
    below: int | None = None


class AnswerBody(BaseModel):
    pair: str
    outcome: Literal["A", "B", "TIE"]
    # Accepted on the wire for a control this surface does not have. §5.2 does weight a duel by
    # its margin, so the field means something to the model - but §6.1 gives the persistent
    # decisive toggle to Battle and §6.3 describes no such control on the tier queue, so nothing
    # the person can press sets it and every queue comparison is written at the hesitant weight.
    # The comment here used to say "§6.1's decisive toggle applies here too", which described a
    # control nobody can reach; the field stays because removing it would be a wire change for a
    # control the spec may yet give this surface, and the shape is already pinned by the client's
    # own test. Decision 201.
    decisive: bool = False


class TierSetBody(BaseModel):
    tier_set: list[str]


async def _payload(
    conn: asyncpg.Connection,
    *,
    user: Any,
    kind: str,
    hp: Hyperparams,
    filters: library.RankFilters,
    log_line: str | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§6.3's board, in the one shape every route on this surface returns."""
    show_model = rail.visible_to(user)
    tiers_out, cuts, rows = await read.load(
        conn, user_id=user.id, kind=kind, hp=hp, filters=filters
    )
    unfiltered = rows if not filters.active() else await read.items(
        conn, user_id=user.id, kind=kind
    )
    eligible = queue.eligible(unfiltered, cuts=cuts.boundaries, hp=hp)
    matched = (
        await library.dna_tiers_for(conn, title_ids=[r.title_id for r in rows], dna=filters.dna)
        if filters.dna
        else {}
    )

    # Decision 209. §6.3's board reads `ledger_state`, and finding 9 moved the first fit per
    # (user, kind) off the request and onto the 60 s sweep — so between a member's first verdict
    # and that sweep there is no row, and the two counts above are both 0. §5.3 is right that the
    # fit belongs there and nothing is lost, but "0 rated" is the same reading as "you have not
    # started", and an instrument whose healthy value equals its broken one is not an instrument.
    # The stamp the queued fit already wrote is what separates them; it says owed, not missing.
    fitting = cuts.refit_owed
    payload: dict[str, Any] = {
        "kind": kind,
        "tier_set": list(cuts.tier_set),
        "tiers": read.public(tiers_out),
        "rated": len(rows),
        "rated_total": len(unfiltered),
        "fitting": fitting,
        "queue_eligible": len(eligible),
        "filters": filters.active(),
        # §4.1 rule 1: a DNA predicate's survivors say which tier matched them. Absent when no
        # predicate is active, rather than an empty object claiming "nothing matched".
        "dna_tiers": {str(k): v for k, v in matched.items()} or None,
        # §6.8's data voice, and §6.3's provenance: seven letters must not read as given.
        # A count is provenance only while it is a count of something: with the fit owed and
        # nothing yet readable, "0 rated" is the reading decision 209 takes off this surface,
        # printed a second time in §6.8's own register. A board that has rows keeps its number —
        # a refit owed over a board that already reads is decision 11's window, with its own copy.
        "why": (
            "tiers are still being fitted · learned cutpoints, refit nightly"
            if fitting and not unfiltered
            else f"{len(unfiltered)} rated · learned cutpoints, refit nightly"
        ),
    }
    if show_model:
        # Assembled only when it can be seen. `redact` would remove it either way, but §13's
        # agreement figure is a query, and running one per board read to throw the answer away
        # is a cost the gate is supposed to save rather than merely hide.
        payload["model"] = {
            "cutpoints": [float(b) for b in cuts.boundaries],
            "hyperparams": hp.source,
            "straddle_z": hp.straddle_z,
            "tension_credible_mass": hp.tension_credible_mass,
            "held_out": (
                await evaluation.held_out_agreement(conn, user_id=user.id, kind=kind)
            ).as_dict(),
        }
    if ledger is not None:
        # What the incremental update did, reported rather than raised — the same gated key and
        # the same shape §6.1's payload already ships, so the clients need nothing. [finding 8]
        payload["ledger"] = ledger
    if log_line:
        rail.record(user_id=user.id, kind="tier_edit", line=log_line)
        payload["log"] = [log_line]
    # Still through the gate: `show_model` decides what is built, `redact` is what guarantees
    # nothing gated escapes — including a key a later edit adds without thinking about it.
    return rail.redact(payload, show_model=show_model)


@router.get("")
async def board(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: Kind = Query(..., description="§4.1 rule 5: one board at a time, never a merge."),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    runtime_max: int | None = Query(None, ge=1),
    runtime_min: int | None = Query(None, ge=1),
    seen: Literal["any", "seen", "unseen"] = "any",
    dna: str | None = Query(None, description='§6.3: a term, bare or facet-qualified.'),
) -> dict[str, Any]:
    """§6.3's board: every rated title of this kind, in tiers, best-first."""
    return await _payload(
        conn,
        user=user,
        kind=kind,
        hp=_hyperparams(request),
        filters=_filters(q, genre, decade, runtime_max, runtime_min, seen, dna),
    )


@router.post("/drop")
async def drop(
    body: DropBody, conn: DB, user: ActiveUser, request: Request,
    kind: Kind = Query(...),
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    runtime_max: int | None = Query(None, ge=1),
    runtime_min: int | None = Query(None, ge=1),
    seen: Literal["any", "seen", "unseen"] = "any",
    dna: str | None = None,
) -> dict[str, Any]:
    """§6.3's drag-and-drop, and the phone's tap-to-tier: one route, because they have the same
    semantics and a second one would be a second thing to keep in step.

    It takes the board's filters as well as the drop, and answers with the board *under those
    filters*. Without them the response would be the unfiltered board and the person's filters
    would silently clear on every drop — the client would have to choose between showing a
    board it did not ask for and making a second request for one it already had.
    """
    await _assert_active_basis(request, conn)
    hp = _hyperparams(request)
    # Built before the write, not only for the answer: decision 204 makes "was a filter on?" an
    # input to what the drop records, because the client computes `above`/`below` from the board
    # it can see and a filtered board's neighbours are neighbours on that board and nowhere else.
    filters = _filters(q, genre, decade, runtime_max, runtime_min, seen, dna)
    names = await read.names_for(conn, [body.title_id])
    try:
        result = await drop_rules.drop(
            conn,
            user_id=user.id,
            title_id=body.title_id,
            tier=body.tier,
            above=body.above,
            below=body.below,
            title_name=names.get(body.title_id),
            filtered=bool(filters.active()),
        )
    except drop_rules.DropRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # §6.3: "The model refits (incremental immediately, exact nightly)". Immediately is what
    # makes the board move under the person's hand rather than overnight.
    #
    # Reporting, not raising: `drop_rules.drop` has committed the edit and its neighbour duels
    # by the time this runs, and a bare call turned a refused fit into a 500 over a durable row
    # — with the client's retry writing a second one, 1 -> 2, measured. The Rate seam caught the
    # same exceptions on purpose and this one did not, so one surface lost the tap and the other
    # kept it. [M4.10 finding 8]
    touched = {body.title_id} | {t for t in (body.above, body.below) if t is not None}
    ledger = await refit.update_incrementally_reporting(
        conn, user_id=user.id, kind=result.kind, title_ids=sorted(touched), hp=hp,
        embeddings=_embeddings(request, conn), bundle_version=_basis(request),
    )
    return await _payload(
        conn, user=user, kind=result.kind, hp=hp, filters=filters,
        log_line=result.log, ledger=ledger,
    )


@router.get("/queue")
async def next_pair(
    conn: DB, user: ActiveUser, request: Request, kind: Kind = Query(...)
) -> dict[str, Any]:
    """§6.3's "sharpen my ranking": one comparison, drawn by the 70/20/10 selector.

    The arm travels sealed rather than plain. §13's guard needs the held-out tenth to be
    identifiable end to end, and a client that could name the arm could put an adaptively
    chosen pair into the evaluation stream — which is the inflation the guard exists to stop.

    It travels sealed *and gated*, and the draw is derived rather than rolled: the two halves of
    the same guard, argued in the module docstring and in `_queue_rng`. What reaches an ungated
    client is the pair, its names, its token and a why-line that says the same thing on all
    three arms.
    """
    hp = _hyperparams(request)
    show_model = rail.visible_to(user)
    pool = await read.candidates(conn, user_id=user.id, kind=kind, hp=hp)
    # Read before the draw, because the draw is derived from it and the seal carries it. One
    # read for both, so the pair a token names and the count that retires it cannot disagree.
    answered = await read.answered_comparisons(conn, user_id=user.id, kind=kind)
    pair = _draw(
        pool,
        user_id=user.id,
        kind=kind,
        answered=answered,
        # Finding 12's other half: the exploration arm re-served the pairs it had already asked,
        # and each repeat is an independent Davidson row. `read.asked_pairs` leaves the held-out
        # stream out of the set, because a selector that consulted §13's rows would be reading
        # the evaluation data it exists to be audited by.
        asked=await read.asked_pairs(conn, user_id=user.id, kind=kind),
    )
    if pair is None:
        # Proposal 80's zero state, in §6.8's register: an honest "nothing to sharpen" rather
        # than an empty card.
        return {
            "kind": kind,
            "pair": None,
            "reason": (
                "There is nothing to compare yet — rate a few more titles and the queue fills up."
            ),
        }
    names = await read.names_for(conn, [pair.title_a, pair.title_b])
    served = pair.public()
    # `Pair.public()` stays the one place these fields are named; which of them a person may see
    # is the route's question and not the selector's.
    arm = {"arm": served.pop("arm"), "reason": served.pop("reason")}
    payload = {
        "kind": kind,
        "pair": {
            **served,
            "name_a": names.get(pair.title_a),
            "name_b": names.get(pair.title_b),
            "token": _seal(user.id, kind, pair, answered),
            "reason": _QUEUE_WHY,
            "model": arm,
        },
        "pool": len(pool),
    }
    return rail.redact(payload, show_model=show_model)


@router.post("/queue/answer")
async def answer(
    body: AnswerBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """One tier-queue comparison, written with the arm the server drew it under, under a lock.

    The lock is finding 1 in one sentence: the count check and the INSERT were two round trips
    apart on a connection that autocommits every statement, with no unique index on `duel` to
    backstop them, so two gathered answers under one seal both passed the check and both wrote.
    Measured with no injected latency at all: two rows in most races, sometimes carrying
    contradictory outcomes for the same pair, and once on the held-out arm — §13's only
    admissible evaluation data, in a §4.2 append-only table on a surface with no undo. It is
    taken as the first statement inside the transaction, which is `deps.write_txn`'s own
    argument: the loser waits, re-counts what the winner committed, and leaves through the 409
    below rather than writing a second judgement nobody made.

    The refit stays OUTSIDE that transaction, and that is deliberate. `rate/session.py` sets the
    shape — the observation transaction closes and the ledger update runs after it — and a fit
    measured at 1-7 s held under an advisory lock would trade a correctness fix for a latency
    defect. The count has moved by the time the loser wakes, so the 409 does not depend on the
    fit being inside. [M4.10 finding 1]
    """
    await _assert_active_basis(request, conn)
    hp = _hyperparams(request)
    sealed = _unseal(body.pair, user_id=user.id)
    kind = str(sealed["k"])
    async with write_txn(conn):
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _ANSWER_LOCK, user.id)
        if await read.answered_comparisons(conn, user_id=user.id, kind=kind) != sealed.get("n"):
            # The double-tap guard, the replay guard and the second-tab guard are one question:
            # is this still the pair on the table? §6.1 answers it with a token it nulls on
            # write; here the answer count is the token.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"reason": "stale_pair", "message": "that pair has already been answered"},
            )
        write = await observations.record_duel(
            conn,
            user_id=user.id,
            title_a=int(sealed["a"]),
            title_b=int(sealed["b"]),
            outcome=body.outcome,
            context="tier_queue",
            selection=str(sealed["arm"]),
            decisive=body.decisive,
            hp=hp,
        )
    # None on the held-out arm, where no fit was attempted — which is a different fact from a
    # fit that ran and refused, and the two must not read alike.
    ledger: dict[str, Any] | None = None
    if str(sealed["arm"]) != queue.ARM_HOLDOUT:
        # §13: the fit cannot see a held-out row, so re-fitting after one can only perturb
        # state — and it did. The incremental write stamps `last_observed_at = now` for every
        # title carrying an observation, which resets §5.2's freshness inflation, which moves
        # `sigma_eff`, which moves `straddle`, which is the predicate the *boundary* arm draws
        # from. The evaluation stream would have been steering the selector it exists to audit,
        # and the nightly refit — which derives that stamp from the excluded set — would have
        # undone it again every night. Skipping the call is both correct and cheaper: nothing
        # the model knows has changed.
        #
        # Reporting, not raising, for the reason the drop route says at length: the duel is
        # durable by now and a refusal that reached the client as a 500 lost the comparison and
        # invited a retry that wrote another. [M4.10 finding 8]
        ledger = await refit.update_incrementally_reporting(
            conn, user_id=user.id, kind=kind, title_ids=list(write.title_ids), hp=hp,
            embeddings=_embeddings(request, conn), bundle_version=_basis(request),
        )
    names = await read.names_for(conn, list(write.title_ids))
    # `duel_line` elides a name too long for §6.7's 400 rather than refusing it (`home/rail.py`'s
    # MAX_NAME_IN_LINE), which is what makes this call safe to make after the write: `title.name`
    # is bundle data, so there is no person to refuse, and this line is composed once the row is
    # already durable. [M4.10 finding 8]
    line = rail.duel_line(
        names.get(int(sealed["a"]), str(sealed["a"])),
        names.get(int(sealed["b"]), str(sealed["b"])),
        body.outcome,
        context="tier_queue",
        selection=str(sealed["arm"]),
    )
    rail.record(user_id=user.id, kind="duel", line=line)
    payload = await next_pair(conn, user, request, kind=kind)
    payload["log"] = [line]
    payload["ledger"] = ledger
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/tiers")
async def tier_set(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """Decision 11: a per-user preference, so it lives beside the person's other settings.

    Read for `KINDS[0]` explicitly, and the route deliberately gains no `kind` parameter for it.
    `tier_set_of` now requires the kind because a fit was reverting one kind's set and leaving
    the two rows disagreeing (finding 5), and the read side no longer rests on an invariant the
    write side was not enforcing. But the *control* is one control: decision 11's setting is
    per-user, `save_tier_set` writes both rows in one transaction, and a query parameter here
    would change the API shape and the client for a difference the settings surface does not
    offer. Naming the kind in the call rather than in the URL is the honest version of that.
    """
    tier_set = await tiers.tier_set_of(conn, user_id=user.id, kind=observations.KINDS[0])
    return {
        "tier_set": list(tier_set),
        "min": tiers.MIN_TIERS,
        "max": tiers.MAX_TIERS,
        "warning": (
            "Changing the number of tiers discards your learned cutpoints and queues a refit. "
            "Your past moves are kept."
        ),
    }


@router.put("/tiers")
async def save_tier_set(body: TierSetBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    try:
        report = await tiers.save_tier_set(conn, user_id=user.id, tier_set=body.tier_set)
    except tiers.TierSetRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {
        "tier_set": list(report.tier_set),
        "previous": list(report.previous),
        "k_changed": report.k_changed,
        "refit_queued": report.refit_queued,
        "tier_edits_kept": report.tier_edits_kept,
    }


__all__ = ["router"]
