"""Rank routes (§6.3); the rules live in `spielplan.rank`. A queue answer names a server-sealed pair
(the arm comes from the seal, §13), single-use under `_ANSWER_LOCK`. Every response leaves through
`rail.redact`, the arm included (decision 117). `kind` is required (§4.1 rule 5)."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import random
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from spielplan.api import deps
from spielplan.api.deps import DB, ActiveUser, write_txn
from spielplan.core.config import settings
from spielplan.db import genres, library
from spielplan.home import rail
from spielplan.ledger import ladder, observations, refit
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.rank import drop as drop_rules
from spielplan.rank import evaluation, queue, read, tiers
from spielplan.rank import place as place_rules

log = logging.getLogger("spielplan.api.rank")

router = APIRouter(prefix="/api/rank", tags=["rank"])

Kind = Literal["movie", "series"]

# Its own salt, so a sealed pair is never a session cookie; rotating SESSION_SECRET voids both (§2).
_PAIR_SALT = "spielplan/rank/pair/v1"
# Place with questions has its own too, so neither token passes for the other.
_PLACE_SALT = "spielplan/rank/place/v1"

# The namespace half of a two-int advisory lock, so a user id cannot collide with other features' locks.
_ANSWER_LOCK = 6303

# Every write here waits for the member's set-up (decision 550); the board stays readable.
_NOT_SET_UP = "Set up your ladder first."

# The queue's two zero states (decision 495): too thin to pair, or every pair worth asking asked.
_QUEUE_THIN = "There is nothing to compare yet — rate a few more titles and the queue fills up."
_QUEUE_SETTLED = (
    "Nothing left to compare right now — you've answered every pair worth asking. "
    "Rate a few more titles and new ones turn up."
)


def _sealer(salt: str = _PAIR_SALT) -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret, salt)


def _seal(user_id: int, kind: str, pair: queue.Pair, answered: int) -> str:
    return _sealer().dumps(
        {
            "u": user_id,
            "k": kind,
            "a": pair.title_a,
            "b": pair.title_b,
            "arm": pair.arm,
            "n": answered,
            "r": pair.reask_of,
        }
    )


async def _set_up(conn: asyncpg.Connection, user_id: int) -> None:
    try:
        await ladder.require_set_up(conn, user_id=user_id)
    except ladder.NotSetUp as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"reason": "not_set_up", "message": _NOT_SET_UP}
        ) from exc


def _unseal(token: str, *, user_id: int, salt: str = _PAIR_SALT) -> dict[str, Any]:
    try:
        payload = _sealer(salt).loads(token)
    except BadSignature as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "stale_pair", "message": "that pair is no longer on the table"},
        ) from exc
    if payload.get("u") != user_id:
        # The seal proves the server drew it; the id proves who for.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that pair belongs to another account")
    return payload


async def _claim(
    conn: asyncpg.Connection, *, user_id: int, kind: str, sealed: dict[str, Any], context: str
) -> None:
    """Inside the answer's transaction: the answer count is the token, so a double tap, a replay and
    a second tab are one question."""
    await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _ANSWER_LOCK, user_id)
    if await read.answered_comparisons(
        conn, user_id=user_id, kind=kind, context=context
    ) != sealed.get("n"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"reason": "stale_pair", "message": "that pair has already been answered"},
        )


def _queue_rng(user_id: int, kind: str, answered: int, stream: str = "") -> random.Random:
    """Keyed on (user, kind, answered), so reloading cannot re-roll the arm and steer §13's held-out
    rate. HMAC under SESSION_SECRET, so arms cannot be predicted. `stream` keys the re-ask coin apart
    from the arm's roll."""
    key = f"{user_id}:{kind}:{answered}" + (f":{stream}" if stream else "")
    digest = hmac.new(settings().session_secret.encode(), key.encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _filters(
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    runtime_max: int | None = Query(None, ge=1),
    runtime_min: int | None = Query(None, ge=1),
    seen: Literal["any", "seen", "unseen"] = "any",
    dna: str | None = Query(
        None, description="§6.3: a term by its id, bare or facet-qualified, or by its label."
    ),
) -> library.RankFilters:
    # Decision 473: an unknown genre lands in the board's no-match state rather than a 422.
    if genre:
        with contextlib.suppress(ValueError):
            genre = genres.canonical(genre)
    return library.RankFilters(
        q=q, genre=genre, decade=decade, runtime_max=runtime_max,
        runtime_min=runtime_min, seen=seen, dna=dna,
    )


Filters = Annotated[library.RankFilters, Depends(_filters)]
# Decision 528: each tier's first N entries; the counts stay whole.
PerTier = Annotated[int | None, Query(ge=1)]


class DropBody(BaseModel):
    """Pointer drag and tap-to-tier send the same body: the same `tier_edit` semantics (§6.3). `via`
    and `undoes` are recorded, not read by the fit (decision 534)."""

    title_id: int
    tier: int = Field(ge=0)
    above: int | None = None
    below: int | None = None
    via: Literal["drag_drop", "explicit"] = "drag_drop"
    undoes: int | None = None


class AnswerBody(BaseModel):
    pair: str
    outcome: Literal["A", "B", "TIE"]
    # No control sets it on this surface (decision 201). Kept on the wire; queue duels use the
    # hesitant weight.
    decisive: bool = False


class TierSetBody(BaseModel):
    tier_set: list[str]


class PlaceBody(BaseModel):
    title_id: int
    kind: Kind


class PlaceAnswerBody(BaseModel):
    token: str
    outcome: Literal["A", "B", "TIE"]
    decisive: bool = False


class PlaceSkipBody(BaseModel):
    token: str


async def _payload(
    conn: asyncpg.Connection,
    *,
    user: Any,
    kind: str,
    hp: Hyperparams,
    filters: library.RankFilters,
    log_line: str | None = None,
    ledger: dict[str, Any] | None = None,
    per_tier: int | None = None,
) -> dict[str, Any]:
    """§6.3's board, in the one shape every route here returns."""
    show_model = rail.visible_to(user)
    tiers_out, cuts, rows = await read.load(
        conn, user_id=user.id, kind=kind, hp=hp, filters=filters
    )
    unfiltered = rows if not filters.active() else await read.items(
        conn, user_id=user.id, kind=kind
    )
    matched = (
        await library.dna_tiers_for(conn, title_ids=[r.title_id for r in rows], dna=filters.dna)
        if filters.dna
        else {}
    )

    # Decision 209: before the sweep's first fit, "0 rated" would read as "not started"; the owed
    # stamp says fitting instead.
    fitting = cuts.refit_owed
    compared = await read.comparisons_since_setup(conn, user_id=user.id, kind=kind)
    payload: dict[str, Any] = {
        "kind": kind,
        "set_up": await ladder.set_up_at(conn, user_id=user.id) is not None,
        "guessing": compared < read.GUESS_UNTIL,
        "tier_set": list(cuts.tier_set),
        "tiers": read.public(tiers_out, per_tier),
        "rated": len(rows),
        # What Sharpen will ask about: the queue's eligible set, which is the straddle badge (§6.3).
        "straddling": len(queue.eligible(unfiltered, cuts=cuts.boundaries, hp=hp)),
        "rated_total": len(unfiltered),
        "fitting": fitting,
        "filters": filters.active(),
        # §4.1 rule 1: which tier matched each DNA survivor; absent without a predicate.
        "dna_tiers": {str(k): v for k, v in matched.items()} or None,
    }
    if show_model:
        # Built only when visible: the held-out agreement is a query not worth running to discard.
        payload["model"] = {
            "cutpoints": [float(b) for b in cuts.boundaries],
            "hyperparams": hp.source,
            "straddle_z": hp.straddle_z,
            "tension_credible_mass": hp.tension_credible_mass,
            "comparisons": compared,
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
    filters: Filters,
    kind: Kind = Query(..., description="§4.1 rule 5: one board at a time, never a merge."),
    per_tier: PerTier = None,
) -> dict[str, Any]:
    return await _payload(
        conn, user=user, kind=kind, hp=deps.hyperparams(request), filters=filters, per_tier=per_tier
    )


@router.get("/tier")
async def tier_page(
    conn: DB,
    user: ActiveUser,
    request: Request,
    filters: Filters,
    kind: Kind = Query(...),
    index: int = Query(..., ge=0),
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    """One tier's entries a page at a time, cut from the same board `board` builds (decision 528)."""
    tiers_out, _cuts, _rows = await read.load(
        conn, user_id=user.id, kind=kind, hp=deps.hyperparams(request), filters=filters
    )
    tier = next((t for t in tiers_out if t.index == index), None)
    if tier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"there is no tier {index} on this board")
    payload = {
        "kind": kind,
        "index": index,
        "count": len(tier.entries),
        "offset": offset,
        "entries": [entry.public() for entry in tier.entries[offset : offset + limit]],
    }
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.post("/drop")
async def drop(
    body: DropBody, conn: DB, user: ActiveUser, request: Request, filters: Filters,
    kind: Kind = Query(...),
    per_tier: PerTier = None,
) -> dict[str, Any]:
    """Takes and answers with the board's filters and paging, or a drop would silently clear them.
    Whether a filter was on is an input to the drop (decision 204)."""
    await deps.assert_active_basis(request, conn)
    await _set_up(conn, user.id)
    hp = deps.hyperparams(request)
    names = await read.names_for(conn, [body.title_id])
    try:
        result = await drop_rules.drop(
            conn,
            user_id=user.id,
            title_id=body.title_id,
            tier=body.tier,
            above=body.above,
            below=body.below,
            via=body.via,
            undoes=body.undoes,
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
        embeddings=deps.embeddings(request, conn), bundle_version=deps.basis(request),
    )
    payload = await _payload(
        conn, user=user, kind=result.kind, hp=hp, filters=filters,
        log_line=result.log, ledger=ledger, per_tier=per_tier,
    )
    # What a toast's Undo names as the edit it takes back.
    return {**payload, "tier_edit_id": result.tier_edit_id}


@router.get("/queue")
async def next_pair(
    conn: DB, user: ActiveUser, request: Request, kind: Kind = Query(...)
) -> dict[str, Any]:
    """§6.3's 70/20/10 queue, with §13(b)'s re-asks. The arm travels sealed and gated, so no client
    can steer or learn §13's held-out stream."""
    await _set_up(conn, user.id)
    hp = deps.hyperparams(request)
    show_model = rail.visible_to(user)
    pool = await read.candidates(conn, user_id=user.id, kind=kind, hp=hp)
    # One read for the draw and the seal, so a token's pair and its count cannot disagree.
    answered = await read.answered_comparisons(conn, user_id=user.id, kind=kind)
    pair = queue.reask(
        pool,
        await read.reask_pairs(conn, user_id=user.id, kind=kind),
        _queue_rng(user.id, kind, answered, "reask"),
    ) or queue.draw(
        pool,
        rng=_queue_rng(user.id, kind, answered),
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
    by_id = {c.title_id: c for c in pool}
    tier_set = await tiers.tier_set_of(conn, user_id=user.id, kind=kind)
    served = pair.public()
    arm = {"arm": served.pop("arm"), "reason": served.pop("reason")}
    payload = {
        "kind": kind,
        "pair": {
            **served,
            "name_a": names.get(pair.title_a),
            "name_b": names.get(pair.title_b),
            "token": _seal(user.id, kind, pair, answered),
            # One form on every arm, so no reason tells §13's pairs apart.
            "reason": queue.why(by_id[pair.title_a], by_id[pair.title_b], tier_set, kind),
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
    await deps.assert_active_basis(request, conn)
    await _set_up(conn, user.id)
    hp = deps.hyperparams(request)
    sealed = _unseal(body.pair, user_id=user.id)
    kind = str(sealed["k"])
    reask_of = sealed.get("r")
    async with write_txn(conn):
        await _claim(conn, user_id=user.id, kind=kind, sealed=sealed, context="tier_queue")
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
            is_reask=reask_of is not None,
            reask_of=reask_of,
        )
    # None on the held-out arm and on a re-ask, where no fit ran: distinct from a fit that refused.
    ledger: dict[str, Any] | None = None
    if str(sealed["arm"]) != queue.ARM_HOLDOUT and reask_of is None:
        # §13: the fit sees neither a held-out row nor a re-ask, but refitting would restamp
        # freshness and steer the boundary arm. Reported, not raised, as in `drop`.
        ledger = await refit.update_incrementally_reporting(
            conn, user_id=user.id, kind=kind, title_ids=list(write.title_ids), hp=hp,
            embeddings=deps.embeddings(request, conn), bundle_version=deps.basis(request),
        )
    names = await read.names_for(conn, list(write.title_ids))
    # `duel_line` elides long names rather than refusing, so it is safe after the durable write.
    line = rail.duel_line(
        names.get(int(sealed["a"]), str(sealed["a"])),
        names.get(int(sealed["b"]), str(sealed["b"])),
        body.outcome,
        context="tier_queue",
        selection=str(sealed["arm"]),
    ) + (" · asked again, held out of the fit" if reask_of is not None else "")
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


def _search(sealed: dict[str, Any]) -> place_rules.Search:
    return place_rules.Search(
        title_id=int(sealed["t"]),
        tier=int(sealed["i"]),
        low=int(sealed["lo"]),
        high=int(sealed["hi"]),
        asked=int(sealed["q"]),
        skipped=tuple(int(t) for t in sealed["x"]),
    )


async def _placing(
    conn: asyncpg.Connection, *, user_id: int, kind: str, view: place_rules.View
) -> dict[str, Any]:
    """The next pair under a fresh seal, or where the search put the title."""
    s, probe = view.search, view.probe
    if probe is None:
        above, below, around = view.spot()
        cards = await read.cards_for(conn, around)
        return {
            "done": True,
            "kind": kind,
            "title_id": s.title_id,
            "tier": view.label,
            "above": cards.get(above),
            "below": cards.get(below),
            "asked": s.asked,
            "around": [cards[t] for t in around if t in cards],
        }
    neighbour = view.others[probe]
    cards = await read.cards_for(conn, [s.title_id, neighbour])
    token = _sealer(_PLACE_SALT).dumps(
        {
            "u": user_id,
            "k": kind,
            "t": s.title_id,
            "i": s.tier,
            "lo": s.low,
            "hi": s.high,
            "q": s.asked,
            "x": list(s.skipped),
            "b": neighbour,
            "m": probe,
            "n": await read.answered_comparisons(
                conn, user_id=user_id, kind=kind, context=place_rules.CONTEXT
            ),
        }
    )
    return {
        "kind": kind,
        "token": token,
        "tier": view.label,
        "left": {**cards[s.title_id], "outcome": "A"},
        "right": {**cards[neighbour], "outcome": "B"},
        "progress": view.progress(),
    }


async def _resumed(
    conn: asyncpg.Connection, request: Request, *, user_id: int, kind: str,
    search: place_rules.Search,
) -> dict[str, Any]:
    try:
        view = await place_rules.resume(
            conn, user_id=user_id, kind=kind, hp=deps.hyperparams(request), search=search
        )
    except place_rules.PlaceRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _placing(conn, user_id=user_id, kind=kind, view=view)


@router.post("/place")
async def place(body: PlaceBody, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """§6.3's Place with questions: the first pair, or the end when the tier holds no other title."""
    await _set_up(conn, user.id)
    try:
        view = await place_rules.begin(
            conn, user_id=user.id, kind=body.kind, hp=deps.hyperparams(request),
            title_id=body.title_id,
        )
    except place_rules.PlaceRefused as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _placing(conn, user_id=user.id, kind=body.kind, view=view)


@router.post("/place/answer")
async def place_answer(
    body: PlaceAnswerBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """Single-use as the queue's seal is; the refit runs after the lock, reported as in `drop`."""
    await deps.assert_active_basis(request, conn)
    await _set_up(conn, user.id)
    hp = deps.hyperparams(request)
    sealed = _unseal(body.token, user_id=user.id, salt=_PLACE_SALT)
    kind = str(sealed["k"])
    async with write_txn(conn):
        await _claim(conn, user_id=user.id, kind=kind, sealed=sealed, context=place_rules.CONTEXT)
        search, write = await place_rules.answer(
            conn, user_id=user.id, search=_search(sealed), neighbour=int(sealed["b"]),
            index=int(sealed["m"]), outcome=body.outcome, decisive=body.decisive, hp=hp,
        )
    await refit.update_incrementally_reporting(
        conn, user_id=user.id, kind=kind, title_ids=list(write.title_ids), hp=hp,
        embeddings=deps.embeddings(request, conn), bundle_version=deps.basis(request),
    )
    names = await read.names_for(conn, list(write.title_ids))
    a, b = write.title_ids
    rail.record(
        user_id=user.id,
        kind="duel",
        line=rail.duel_line(
            names.get(a, str(a)), names.get(b, str(b)), body.outcome,
            context=place_rules.CONTEXT, selection="random",
        ),
    )
    return await _resumed(conn, request, user_id=user.id, kind=kind, search=search)


@router.post("/place/skip")
async def place_skip(
    body: PlaceSkipBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """The neighbour on the table is not seen: no duel, so no claim, and a replay marks it again."""
    await _set_up(conn, user.id)
    sealed = _unseal(body.token, user_id=user.id, salt=_PLACE_SALT)
    kind = str(sealed["k"])
    search = await place_rules.skip(
        conn, user_id=user.id, search=_search(sealed), neighbour=int(sealed["b"])
    )
    return await _resumed(conn, request, user_id=user.id, kind=kind, search=search)


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
