"""Rate routes (§6.1); the rules live in `spielplan.rate.session`. A write names a `card_token`, never
a title, so the card stays the server's; a stale token is the 409 double-tap guard. Every response
carries the next card (§6 preamble). Rate is closed until the person's set-up (decision 550)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from spielplan.api import deps
from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.ledger import ladder
from spielplan.rate import direct, session
from spielplan.rate import shelves as rate_shelves

router = APIRouter(prefix="/api/rate", tags=["rate"])

Head = Annotated[list[int], Field(default_factory=list)]


class SessionBody(BaseModel):
    """Every field is optional: the same route starts a session and switches its kind."""

    kinds: list[Literal["movie", "series"]] | None = None
    restart: bool = False
    head: Head


class PlaceBody(BaseModel):
    card_token: str
    tier: int
    latency_ms: int | None = None
    head: Head


class CardBody(BaseModel):
    card_token: str
    latency_ms: int | None = None
    head: Head


async def _jellyfin(conn: asyncpg.Connection) -> session.Jellyfin:
    cfg = await registry.load_jellyfin(conn)
    return session.Jellyfin(client=registry.make_client(cfg), cfg=cfg)


def _refused(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"reason": reason, "message": message})


def _stale(exc: session.StaleCard) -> HTTPException:
    """The double-tap guard: an answer to a card no longer on the table is a 409."""
    return _refused(
        status.HTTP_409_CONFLICT,
        exc.reason,
        {
            "no_card": "there is no card on the table",
            "stale_card": "that card has already been answered",
        }.get(exc.reason, "the card token is not current"),
    )


async def _set_up(conn: asyncpg.Connection, user_id: int) -> None:
    try:
        await ladder.require_set_up(conn, user_id=user_id)
    except ladder.NotSetUp as exc:
        raise _refused(
            status.HTTP_409_CONFLICT, "not_set_up", "Set up your ladder first"
        ) from exc


async def _resume(conn: asyncpg.Connection, user_id: int) -> session.RateSession:
    return await session.open_or_resume(conn, user_id=user_id)


@router.get("")
async def current(
    conn: DB,
    user: ActiveUser,
    head: list[int] = Query(
        default=[], description="§7.3/§6.0: title ids pinned to the front (the finish prompt, Home)."
    ),
) -> dict[str, Any]:
    """Idempotent: a second GET returns the same card under the same token. Before the set-up it
    opens no session and returns the closed card."""
    if await ladder.set_up_at(conn, user_id=user.id) is None:
        return await session.payload(conn, None, user=user)
    s = await session.ensure_card(conn, await _resume(conn, user.id), head=head)
    return await session.payload(conn, s, user=user)


@router.post("/session")
async def controls(body: SessionBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§6.1's Films/Series switch; `restart` begins a fresh block."""
    await _set_up(conn, user.id)
    try:
        s = await session.open_or_resume(
            conn, user_id=user.id, kinds=body.kinds, restart=body.restart
        )
        if body.kinds is not None:
            s = await session.set_kinds(conn, s, body.kinds)
    except ValueError as exc:
        raise _refused(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_kinds", str(exc)) from exc
    s = await session.ensure_card(conn, s, head=body.head)
    return await session.payload(conn, s, user=user)


@router.delete("/session")
async def end(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """The journal stays (§4.2 is append-only)."""
    return {"ended": await session.end_session(conn, user_id=user.id)}


@router.post("/place")
async def place(body: PlaceBody, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """§6.1's placement (implies `seen`). The echo rides on this response only: nothing the model
    guessed shows before the tap."""
    await deps.assert_active_basis(request, conn)
    await _set_up(conn, user.id)
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_placement(
            conn,
            s,
            card_token=body.card_token,
            tier=body.tier,
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
    except session.BadTier as exc:
        raise _refused(status.HTTP_422_UNPROCESSABLE_CONTENT, "bad_tier", str(exc)) from exc
    return await session.payload(
        conn, outcome.session, echo=outcome.echo, log=outcome.log, ledger=outcome.ledger,
        event_kind="tier_edit", user=user,
    )


@router.post("/not-seen")
async def not_seen(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """A film you cannot remember is plain `unseen`; a placed film keeps its step (§4.2)."""
    await _set_up(conn, user.id)
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


@router.post("/undo")
async def undo(conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """Decision 35: refused at the block boundary with a reason, never a silent no-op."""
    await deps.assert_active_basis(request, conn)
    await _set_up(conn, user.id)
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
        raise _refused(
            status.HTTP_409_CONFLICT,
            "nothing_to_undo",
            {
                "empty": "Nothing to undo yet",
                "block_boundary": "Undo only goes back to the start of these 15",
            }[exc.reason],
        ) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, ledger=outcome.ledger, event_kind="undo", user=user
    )


class TitleAnswerBody(BaseModel):
    answer: Literal["not_seen"]


@router.post("/title/{title_id}")
async def answer_from_title_card(
    title_id: int, body: TitleAnswerBody, conn: DB, user: ActiveUser
) -> dict[str, Any]:
    """Decision 487: the title card's Not seen, put on the person's table as a card under a fresh
    token, so §6.1's journal, counter and Undo apply. Works before the set-up."""
    try:
        outcome = await direct.not_seen(
            conn,
            user_id=user.id,
            title_id=title_id,
            jf=await _jellyfin(conn),
            later=session.settle_in_background,
        )
    except LookupError as exc:
        raise _refused(status.HTTP_404_NOT_FOUND, "no_title", "no such title") from exc
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.get("/shelves")
async def shelves(
    conn: DB, user: ActiveUser, title_id: int = Query(description="The film being placed.")
) -> dict[str, Any]:
    """The title card's ladder sheet: the same shelves Rate draws for this title (decision 551)."""
    await _set_up(conn, user.id)
    try:
        return await rate_shelves.sheet(conn, user_id=user.id, title_id=title_id)
    except LookupError as exc:
        raise _refused(status.HTTP_404_NOT_FOUND, "no_title", "no such title") from exc
