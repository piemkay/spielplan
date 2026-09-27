"""Rate routes (§6.1); the rules live in `spielplan.rate.session`. A write names a `card_token`, never
a title, so the card stays the server's; a stale token is the 409 double-tap guard. Every response
carries the next card (§6 preamble)."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from spielplan.api import deps
from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.rate import direct, session
from spielplan.rate import search as search_rules

log = logging.getLogger("spielplan.api.rate")

router = APIRouter(prefix="/api/rate", tags=["rate"])

Head = Annotated[list[int], Field(default_factory=list)]


class ControlsBody(BaseModel):
    """Every field is optional: the same route starts a session and changes one knob."""

    mode: Literal["mix", "sweep", "battle"] | None = None
    kinds: list[Literal["movie", "series"]] | None = None
    decisive: bool | None = None
    restart: bool = False
    head: Head


class VerdictBody(BaseModel):
    card_token: str
    value: Literal[0, 1, 2]
    latency_ms: int | None = None
    head: Head


class CardBody(BaseModel):
    card_token: str
    latency_ms: int | None = None
    head: Head


class DuelBody(BaseModel):
    card_token: str
    outcome: Literal["A", "B", "TIE"]
    # One answer's override of the session toggle (§6.1's long-press).
    decisive: bool | None = None
    latency_ms: int | None = None
    head: Head


class CorrectionBody(BaseModel):
    card_token: str
    side: Literal["left", "both", "right"]


async def _jellyfin(conn: asyncpg.Connection) -> session.Jellyfin:
    cfg = await registry.load_jellyfin(conn)
    return session.Jellyfin(client=registry.make_client(cfg), cfg=cfg)


def _stale(exc: session.StaleCard) -> HTTPException:
    """The double-tap guard: an answer to a card no longer on the table is a 409."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason": exc.reason,
            "message": {
                "no_card": "there is no card on the table",
                "stale_card": "that card has already been answered",
                "wrong_card_type": "that answer does not fit the card on the table",
            }.get(exc.reason, "the card token is not current"),
        },
    )


async def _resume(conn: asyncpg.Connection, user_id: int) -> session.RateSession:
    return await session.open_or_resume(conn, user_id=user_id)


@router.get("")
async def current(
    conn: DB,
    user: ActiveUser,
    head: list[int] = Query(
        default=[],
        description="§7.3/§6.0: title ids pinned to the front (the banner, Rate it, search).",
    ),
) -> dict[str, Any]:
    """Idempotent: a second GET returns the same card under the same token."""
    s = await _resume(conn, user.id)
    s = await session.ensure_card(conn, s, head=head)
    return await session.payload(conn, s, user=user)


@router.post("/session")
async def controls(
    body: ControlsBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1's mode, kind and decisive controls (decision 520). A fresh session opens in Mix."""
    try:
        s = await session.open_or_resume(
            conn, user_id=user.id, kinds=body.kinds, restart=body.restart
        )
        if body.mode is not None or body.kinds is not None or body.decisive is not None:
            s = await session.set_controls(
                conn, s, mode=body.mode, kinds=body.kinds, decisive=body.decisive
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    s = await session.ensure_card(conn, s, head=body.head)
    return await session.payload(conn, s, user=user)


@router.delete("/session")
async def end(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """The journal stays (§4.2 is append-only)."""
    return {"ended": await session.end_session(conn, user_id=user.id)}


@router.get("/search")
async def search(
    conn: DB,
    user: ActiveUser,
    q: str = Query("", max_length=200, description="A title, or part of one, the person knows."),
    limit: int = Query(search_rules.DEFAULT_LIMIT, ge=1, le=20),
) -> dict[str, Any]:
    """Choosing a hit is not a write: the client pins it with `head=`."""
    return {"q": q, "items": await search_rules.find(conn, user_id=user.id, q=q, limit=limit)}


@router.post("/verdict")
async def verdict(
    body: VerdictBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1's verdict (implies `seen`). The reveal rides on this response only: the anchoring rule."""
    await deps.assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_verdict(
            conn,
            s,
            card_token=body.card_token,
            value=body.value,
            hp=deps.hyperparams(request),
            embeddings=deps.embeddings(request, conn),
            bundle_version=deps.basis(request),
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
            later=session.settle_in_background,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, reveal=outcome.reveal, log=outcome.log,
        ledger=outcome.ledger, event_kind="verdict", user=user,
    )


@router.post("/not-seen")
async def not_seen(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """A title you cannot remember is plain `unseen`; the verdict and duel rows survive (§4.2)."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_not_seen(
            conn,
            s,
            card_token=body.card_token,
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
            later=session.settle_in_background,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.post("/skip")
async def skip(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_skip(
            conn, s, card_token=body.card_token, latency_ms=body.latency_ms, head=body.head
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(conn, outcome.session, log=outcome.log, user=user)


@router.post("/duel")
async def duel(body: DuelBody, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """§6.1's battle answer, `Tie` included — one duel row, never a dropped one."""
    await deps.assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_duel(
            conn,
            s,
            card_token=body.card_token,
            outcome=body.outcome,
            decisive=body.decisive,
            hp=deps.hyperparams(request),
            embeddings=deps.embeddings(request, conn),
            bundle_version=deps.basis(request),
            latency_ms=body.latency_ms,
            head=body.head,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, ledger=outcome.ledger, event_kind="duel", user=user
    )


@router.post("/correction")
async def correction(body: CorrectionBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """No duel row and no counter advance; syncs §7.3."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_correction(
            conn, s, card_token=body.card_token, side=body.side, jf=await _jellyfin(conn),
            later=session.settle_in_background,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.post("/undo")
async def undo(conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """Decision 35: refused at the block boundary with a reason, never a silent no-op."""
    await deps.assert_active_basis(request, conn)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.undo(
            conn,
            s,
            hp=deps.hyperparams(request),
            embeddings=deps.embeddings(request, conn),
            bundle_version=deps.basis(request),
            jf=await _jellyfin(conn),
        )
    except session.UndoUnavailable as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": exc.reason,
                "message": {
                    "empty": "nothing to undo in this block",
                    "block_boundary": (
                        "undo reaches back to the start of this block of 15 and no further"
                    ),
                }[exc.reason],
            },
        ) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, ledger=outcome.ledger, event_kind="undo", user=user
    )


class TitleAnswerBody(BaseModel):
    answer: Literal["disliked", "fine", "liked", "not_seen"]


@router.post("/title/{title_id}")
async def answer_from_title_card(
    title_id: int, body: TitleAnswerBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """Decision 487: the title card's four answers, put on the person's table as a sweep card under a
    fresh token, so §6.1's card, counter, Undo and reveal all apply."""
    # First, for all four answers, so the route's first statement never depends on its body.
    await deps.assert_active_basis(request, conn)
    try:
        outcome = await direct.answer(
            conn,
            user_id=user.id,
            title_id=title_id,
            choice=body.answer,
            hp=deps.hyperparams(request),
            embeddings=deps.embeddings(request, conn),
            bundle_version=deps.basis(request),
            jf=await _jellyfin(conn),
            later=session.settle_in_background,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such title") from exc
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    event = "not_seen" if body.answer == "not_seen" else "verdict"
    return await session.payload(
        conn, outcome.session, reveal=outcome.reveal, log=outcome.log,
        ledger=outcome.ledger, event_kind=event, user=user,
    )
