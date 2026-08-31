"""The Rank surface's routes. Spec v2.1 §6.3, §6.7, §13; decisions 11, 18, 117.

Thin, like `api/rate.py`: every rule lives in `spielplan.rank` and the only things decided here
are the HTTP shapes. Three of them are load-bearing.

**A queue answer names a sealed pair, never two title ids.** §13's guard says a held-out pair
must be identifiable end to end and never recorded as boundary-targeted — and a route that
accepted `{"title_a": 4, "title_b": 9, "arm": "boundary"}` would let the client decide which
stream a comparison belonged to. The pair is drawn on the server, sealed with `itsdangerous`
under `SESSION_SECRET`, and handed back opaque; the arm the duel is written with comes out of
that seal and out of nothing else.

**And the seal is single-use.** It carries the count of comparisons the person had answered
when it was drawn; answering moves that count, so a second answer under the same seal is a
stale card and gets §6.1's 409. Without it the seal was user-bound but replayable, and §13's
agreement figure counts *rows* - so N replays of one uniform-random pair weight one judgement
N-fold in the only number §13 admits, and §4.2's tables are append-only so it cannot be taken
back. §6.1 reaches the same property through `rate_session.card_token`, which it can because a
rating session already has a row; a queue pair has none, and the observations already imply
the number, so no table is invented to hold one.

**Every response leaves through `rail.redact`.** Decision 117 governs "the rail and every
inline annotation". `board.Entry.public()` already omits `s` and σ; the `model` block that
carries them is assembled here and removed by the gate when the toggle is off.

**`kind` is a single value, and required.** §4.1 rule 5 partitions every ranking surface, and
§6.3's board is one tier list at a time — decision 18's both-on case renders "two headed
sections", which on this surface means two boards, one request each. An absent `kind` would be
the unpartitioned query rule 5 exists to prevent.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser
from spielplan.core.config import settings
from spielplan.db import library
from spielplan.home import rail
from spielplan.ledger import hyperparams, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.rank import drop as drop_rules
from spielplan.rank import evaluation, queue, read, tiers

log = logging.getLogger("spielplan.api.rank")

router = APIRouter(prefix="/api/rank", tags=["rank"])

Kind = Literal["movie", "series"]

# A salt of its own, so a sealed queue pair can never be presented as a session cookie or the
# other way round. Rotating SESSION_SECRET invalidates both, which is §2's stated behaviour.
_PAIR_SALT = "spielplan/rank/pair/v1"

# §6.3's queue is drawn per request, so the draw needs entropy that is not a fixture seed.
_rng = random.SystemRandom()


def _sealer() -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret or "insecure-dev-secret", _PAIR_SALT)


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


def _hyperparams(request: Request) -> Hyperparams:
    cached = getattr(request.app.state, "hyperparams", None)
    if cached is not None:
        return cached
    hp, notes = hyperparams.load(getattr(request.app.state, "artifacts", None))
    for note in notes:
        log.debug("hyperparameters: %s", note)
    return hp


def _embeddings(request: Request, conn: asyncpg.Connection):
    return observations.standard_embeddings(conn, getattr(request.app.state, "backbone", None))


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
    # §5.2: tier-queue duels are margin-weighted, so §6.1's decisive toggle applies here too.
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

    payload: dict[str, Any] = {
        "kind": kind,
        "tier_set": list(cuts.tier_set),
        "tiers": read.public(tiers_out),
        "rated": len(rows),
        "rated_total": len(unfiltered),
        "queue_eligible": len(eligible),
        "filters": filters.active(),
        # §4.1 rule 1: a DNA predicate's survivors say which tier matched them. Absent when no
        # predicate is active, rather than an empty object claiming "nothing matched".
        "dna_tiers": {str(k): v for k, v in matched.items()} or None,
        # §6.8's data voice, and §6.3's provenance: seven letters must not read as given.
        "why": (
            f"{len(unfiltered)} rated · learned cutpoints, refit nightly"
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
    hp = _hyperparams(request)
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
        )
    except drop_rules.DropRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # §6.3: "The model refits (incremental immediately, exact nightly)". Immediately is what
    # makes the board move under the person's hand rather than overnight.
    touched = {body.title_id} | {t for t in (body.above, body.below) if t is not None}
    await refit.update_incrementally(
        conn, user_id=user.id, kind=result.kind, title_ids=sorted(touched), hp=hp,
        embeddings=_embeddings(request, conn),
    )
    return await _payload(
        conn, user=user, kind=result.kind, hp=hp,
        filters=_filters(q, genre, decade, runtime_max, runtime_min, seen, dna),
        log_line=result.log,
    )


@router.get("/queue")
async def next_pair(
    conn: DB, user: ActiveUser, request: Request, kind: Kind = Query(...)
) -> dict[str, Any]:
    """§6.3's "sharpen my ranking": one comparison, drawn by the 70/20/10 selector.

    The arm travels sealed rather than plain. §13's guard needs the held-out tenth to be
    identifiable end to end, and a client that could name the arm could put an adaptively
    chosen pair into the evaluation stream — which is the inflation the guard exists to stop.
    """
    hp = _hyperparams(request)
    pool = await read.candidates(conn, user_id=user.id, kind=kind, hp=hp)
    pair = queue.draw(pool, rng=_rng)
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
    answered = await read.answered_comparisons(conn, user_id=user.id, kind=kind)
    return {
        "kind": kind,
        "pair": {
            **pair.public(),
            "name_a": names.get(pair.title_a),
            "name_b": names.get(pair.title_b),
            "token": _seal(user.id, kind, pair, answered),
        },
        "pool": len(pool),
    }


@router.post("/queue/answer")
async def answer(
    body: AnswerBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """One tier-queue comparison, written with the arm the server drew it under."""
    hp = _hyperparams(request)
    sealed = _unseal(body.pair, user_id=user.id)
    kind = str(sealed["k"])
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
    if str(sealed["arm"]) != queue.ARM_HOLDOUT:
        # §13: the fit cannot see a held-out row, so re-fitting after one can only perturb
        # state — and it did. The incremental write stamps `last_observed_at = now` for every
        # title carrying an observation, which resets §5.2's freshness inflation, which moves
        # `sigma_eff`, which moves `straddle`, which is the predicate the *boundary* arm draws
        # from. The evaluation stream would have been steering the selector it exists to audit,
        # and the nightly refit — which derives that stamp from the excluded set — would have
        # undone it again every night. Skipping the call is both correct and cheaper: nothing
        # the model knows has changed.
        await refit.update_incrementally(
            conn, user_id=user.id, kind=kind, title_ids=list(write.title_ids), hp=hp,
            embeddings=_embeddings(request, conn),
        )
    names = await read.names_for(conn, list(write.title_ids))
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
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/tiers")
async def tier_set(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """Decision 11: a per-user preference, so it lives beside the person's other settings."""
    return {
        "tier_set": list(await tiers.tier_set_of(conn, user_id=user.id)),
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
