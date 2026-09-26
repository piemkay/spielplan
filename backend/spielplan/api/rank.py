"""Rank routes (§6.3); the rules live in `spielplan.rank`. A queue answer names a server-sealed pair
(the arm comes from the seal, §13), single-use under `_ANSWER_LOCK`. Every response leaves through
`rail.redact`, the arm included (decision 117). `kind` is required (§4.1 rule 5)."""

from __future__ import annotations

import contextlib
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
from spielplan.db import genres, library
from spielplan.home import rail
from spielplan.ledger import hyperparams, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.models import artifacts
from spielplan.rank import board as board_rules
from spielplan.rank import drop as drop_rules
from spielplan.rank import evaluation, queue, read, tiers

log = logging.getLogger("spielplan.api.rank")

router = APIRouter(prefix="/api/rank", tags=["rank"])

Kind = Literal["movie", "series"]

# Its own salt, so a sealed pair is never a session cookie; rotating SESSION_SECRET voids both (§2).
_PAIR_SALT = "spielplan/rank/pair/v1"

# The namespace half of a two-int advisory lock, so a user id cannot collide with other features' locks.
_ANSWER_LOCK = 6303

# Arm-independent (§6.8): the person must not learn which answers §13 ignores. The arm's own
# sentence travels under `model`.
_QUEUE_WHY = "Pick the one you enjoyed more — your answers are what put your board in order."

# The queue's two zero states (decision 495): too thin to pair, or every pair worth asking asked.
_QUEUE_THIN = "There is nothing to compare yet — rate a few more titles and the queue fills up."
_QUEUE_SETTLED = (
    "Nothing left to compare right now — you've answered every pair worth asking. "
    "Rate a few more titles and new ones turn up."
)

# Test seam only: production derives each draw from the queue position (`_queue_rng`).
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
        # The seal proves the server drew it; the id proves who for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that pair belongs to another account")
    return payload


def _queue_rng(user_id: int, kind: str, answered: int) -> random.Random:
    """Keyed on (user, kind, answered), so reloading cannot re-roll the arm and steer §13's held-out
    rate. HMAC under SESSION_SECRET, so arms cannot be predicted."""
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
    recent: set[int],
) -> queue.Pair | None:
    """Production and the test seam share this path, so the seam tests the real draw."""
    return queue.draw(
        pool, rng=_queue_rng(user_id, kind, answered), asked=asked, recent=recent
    )


def _hyperparams(request: Request) -> Hyperparams:
    """§4.3's constants, pinned at boot. The fallback serves lifespan-less tests and a bundle-less
    household; unreadable constants are a 503, never defaults (a new `hp_digest` refits everyone)."""
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
    """§10's invariant, checked before any durable write as `api/rate.py::_assert_active_basis` does;
    both arms, since a broken store matches the active version."""
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
    """Which bundle this process fits in; `refit.update_incrementally` re-checks after the board lock."""
    store = getattr(request.app.state, "artifacts", None)
    return refit.BASIS_UNSTATED if store is None else store.version


def _embeddings(request: Request, conn: asyncpg.Connection):
    """§5.1's coordinates with the version that describes them, so the `ledger_fit` stamp names it."""
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
    # Decision 473: an unknown genre lands in the board's no-match state rather than a 422.
    if genre:
        with contextlib.suppress(ValueError):
            genre = genres.canonical(genre)
    return library.RankFilters(
        q=q, genre=genre, decade=decade, runtime_max=runtime_max,
        runtime_min=runtime_min, seen=seen, dna=dna,
    )


class DropBody(BaseModel):
    """Pointer drag and tap-to-tier send the same body: the same `tier_edit` semantics (§6.3)."""

    title_id: int
    tier: int = Field(ge=0)
    above: int | None = None
    below: int | None = None


class AnswerBody(BaseModel):
    pair: str
    outcome: Literal["A", "B", "TIE"]
    # No control sets it on this surface (decision 201). Kept on the wire; queue duels use the
    # hesitant weight.
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
    """§6.3's board, in the one shape every route here returns."""
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

    # Decision 209: before the sweep's first fit, "0 rated" would read as "not started"; the owed
    # stamp says fitting instead.
    fitting = cuts.refit_owed
    compared = await read.compared_count(conn, user_id=user.id, kind=kind)
    placed_by_you = sum(1 for r in unfiltered if r.assigned_tier is not None)
    payload: dict[str, Any] = {
        "kind": kind,
        "tier_set": list(cuts.tier_set),
        "tiers": read.public(tiers_out),
        "rated": len(rows),
        "rated_total": len(unfiltered),
        "fitting": fitting,
        "queue_eligible": len(eligible),
        "filters": filters.active(),
        # §4.1 rule 1: which tier matched each DNA survivor; absent without a predicate.
        "dna_tiers": {str(k): v for k, v in matched.items()} or None,
        # §6.8's why-line, in the member register (`board.why_line`, decision 486).
        "why": board_rules.why_line(
            rated=len(unfiltered),
            compared=compared,
            placed_by_you=placed_by_you,
            fitting=fitting,
            tier_set=cuts.tier_set,
        ),
    }
    if show_model:
        # Built only when visible: the held-out agreement is a query not worth running to discard.
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
        # The incremental update's report, not an exception (finding 8).
        payload["ledger"] = ledger
    if log_line:
        rail.record(user_id=user.id, kind="tier_edit", line=log_line)
        payload["log"] = [log_line]
    # `redact` still guarantees nothing gated escapes, including keys added later.
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
    dna: str | None = Query(
        None, description="§6.3: a term by its id, bare or facet-qualified, or by its label."
    ),
) -> dict[str, Any]:
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
    """Takes and answers with the board's filters, or a drop would silently clear them."""
    await _assert_active_basis(request, conn)
    hp = _hyperparams(request)
    # Built before the write: whether a filter was on is an input to the drop (decision 204).
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

    # §6.3's incremental refit, reported rather than raised: the edit is already committed, and a 500
    # invited a retry that wrote it twice.
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
    """§6.3's 70/20/10 queue. The arm travels sealed and gated, so no client can steer or learn §13's
    held-out stream."""
    hp = _hyperparams(request)
    show_model = rail.visible_to(user)
    pool = await read.candidates(conn, user_id=user.id, kind=kind, hp=hp)
    # One read for the draw and the seal, so a token's pair and its count cannot disagree.
    answered = await read.answered_comparisons(conn, user_id=user.id, kind=kind)
    pair = _draw(
        pool,
        user_id=user.id,
        kind=kind,
        answered=answered,
        # The adaptive arms skip pairs already asked and recent titles (decision 494); both reads leave
        # out the held-out stream, which the selector must never consult.
        asked=await read.asked_pairs(conn, user_id=user.id, kind=kind),
        recent=await read.recent_titles(conn, user_id=user.id, kind=kind),
    )
    if pair is None:
        # Which zero state it is (decision 495).
        return {
            "kind": kind,
            "pair": None,
            "reason": _QUEUE_SETTLED if len(pool) >= 2 else _QUEUE_THIN,
        }
    names = await read.names_for(conn, [pair.title_a, pair.title_b])
    served = pair.public()
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
    """The count check and the INSERT share one transaction under `_ANSWER_LOCK`, so a second answer
    under one seal gets the 409, not a duplicate duel. The refit runs after, outside the lock."""
    await _assert_active_basis(request, conn)
    hp = _hyperparams(request)
    sealed = _unseal(body.pair, user_id=user.id)
    kind = str(sealed["k"])
    async with write_txn(conn):
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _ANSWER_LOCK, user.id)
        if await read.answered_comparisons(conn, user_id=user.id, kind=kind) != sealed.get("n"):
            # The answer count is the token: double tap, replay and second tab are one question.
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
    # None on the held-out arm, where no fit ran: distinct from a fit that refused.
    ledger: dict[str, Any] | None = None
    if str(sealed["arm"]) != queue.ARM_HOLDOUT:
        # §13: the fit cannot see a held-out row, but refitting would restamp freshness and steer the
        # boundary arm. Reported, not raised, as in `drop`.
        ledger = await refit.update_incrementally_reporting(
            conn, user_id=user.id, kind=kind, title_ids=list(write.title_ids), hp=hp,
            embeddings=_embeddings(request, conn), bundle_version=_basis(request),
        )
    names = await read.names_for(conn, list(write.title_ids))
    # `duel_line` elides long names rather than refusing, so it is safe after the durable write.
    line = rail.duel_line(
        names.get(int(sealed["a"]), str(sealed["a"])),
        names.get(int(sealed["b"]), str(sealed["b"])),
        body.outcome,
        context="tier_queue",
        selection=str(sealed["arm"]),
    )
    rail.record(user_id=user.id, kind="duel", line=line)
    # Where the answered pair now sits, after the refit (see `read.placements`).
    placed = await read.placements(
        conn, user_id=user.id, kind=kind, hp=hp, title_ids=[int(sealed["a"]), int(sealed["b"])]
    )
    payload = await next_pair(conn, user, request, kind=kind)
    payload["placed"] = placed
    payload["log"] = [line]
    payload["ledger"] = ledger
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/tiers")
async def tier_set(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """Decision 11: one per-user setting, read for `KINDS[0]`; `save_tier_set` writes both kinds' rows."""
    tier_set = await tiers.tier_set_of(conn, user_id=user.id, kind=observations.KINDS[0])
    return {
        "tier_set": list(tier_set),
        "min": tiers.MIN_TIERS,
        "max": tiers.MAX_TIERS,
        # Decision 11's warning, in the member register (decision 486).
        "warning": (
            "Changing how many tiers you have throws away where your tier lines were learned to "
            "fall, and works them out again shortly. Your past moves are kept."
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
