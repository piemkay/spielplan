"""The Rate surface's routes. Spec v2.1 §6.1, §6.7, §7.3, §13, decision 35.

Thin by design: every rule this surface has lives in `spielplan.rate.session`, and the only
things decided here are the HTTP shapes.

Two of those shapes are load-bearing.

  * **A write names a `card_token`, never a title.** §6.1 puts the card in the server's hands
    (the prediction may not travel with it, the re-ask may not be visible in it, the block
    counter may not be the client's), and a route that accepted `{"title_id": 41, "value": 2}`
    would give all three back. A token that no longer matches is a 409 with a reason, which is
    also the double-tap guard.
  * **Every response is the same envelope, and it carries the next card.** §6 preamble:
    "<2 s per sweep card, <1.5 s per battle, undo everywhere, next card preloaded."

Exported as `router`; `app.py` registers it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import asyncpg
import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.ledger import hyperparams, observations
from spielplan.ledger.hyperparams import Hyperparams
from spielplan.ledger.model import EMBED_DIM
from spielplan.ledger.observations import EmbeddingSource
from spielplan.rate import session

log = logging.getLogger("spielplan.api.rate")

router = APIRouter(prefix="/api/rate", tags=["rate"])

Head = Annotated[list[int], Field(default_factory=list)]


class ControlsBody(BaseModel):
    """§6.1's three controls. Every field is optional: the same route starts a session and
    changes one knob on an existing one."""

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
    # One answer's override of the session's persistent toggle — §6.1's long-press accelerator.
    decisive: bool | None = None
    latency_ms: int | None = None
    head: Head


class CorrectionBody(BaseModel):
    card_token: str
    side: Literal["left", "both", "right"]


def _hyperparams(request: Request) -> Hyperparams:
    """§4.3's constants, from the active bundle when there is one.

    `app.state.hyperparams` is where the nightly job's copy lives once startup sets it; until
    then this reads the same file the job would. §3.1 makes a bundle-less household legal, so
    the fallback is the documented defaults and never an error.
    """
    cached = getattr(request.app.state, "hyperparams", None)
    if cached is not None:
        return cached
    hp, notes = hyperparams.load(getattr(request.app.state, "artifacts", None))
    for note in notes:
        log.debug("hyperparameters: %s", note)
    return hp


def _backbone_embeddings(backbone: Any) -> EmbeddingSource:
    """§5.1: a rated title's coordinate is its Backbone row. Read straight out of the loaded
    npz, because §8 stage 10 deliberately stores no Postgres row for a warm title."""

    def rows(title_ids):
        ids = list(title_ids)
        matrix = np.zeros((len(ids), EMBED_DIM))
        present = np.zeros(len(ids), dtype=bool)
        for i, title_id in enumerate(ids):
            vector = backbone.embedding(int(title_id))
            if vector is not None:
                matrix[i] = vector
                present[i] = True
        return matrix, present

    return rows


def _embeddings(request: Request, conn: asyncpg.Connection) -> EmbeddingSource:
    """The source the cached fit was built with — §5.1's "warm Backbone row first, Cold Tower
    placement second".

    `refit.update_incrementally` is explicit that the caller owns this precondition: `v` lives
    in that basis and nothing in the database records which one it was. So the nightly
    `ledger-map-refit` job must compose these two sources in this order too.
    """
    sources = []
    backbone = getattr(request.app.state, "backbone", None)
    if backbone is not None and not getattr(backbone, "is_empty", True):
        sources.append(_backbone_embeddings(backbone))
    sources.append(observations.placement_embeddings(conn))
    return observations.chain(*sources)


async def _jellyfin(conn: asyncpg.Connection) -> session.Jellyfin:
    cfg = await registry.load_jellyfin(conn)
    return session.Jellyfin(client=registry.make_client(cfg), cfg=cfg)


def _stale(exc: session.StaleCard) -> HTTPException:
    """§6.1's card is the server's. Refusing an answer to a card that is no longer on the table
    is what makes that true over HTTP — and it is the double-tap guard."""
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
        description="§7.3/§6.0: title ids the pending-verdicts banner pinned to the front.",
    ),
) -> dict[str, Any]:
    """Open or resume, then serve the card. Idempotent: a second GET returns the same card
    under the same token, because a card that redrew under a refresh would make every "next
    card preloaded" promise in §6 a lie."""
    s = await _resume(conn, user.id)
    s = await session.ensure_card(conn, s, head=head)
    return await session.payload(conn, s, user=user)


@router.post("/session")
async def controls(
    body: ControlsBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1's mode and kind controls, plus the persistent decisive toggle.

    A fresh session opens in Mix — §6.1 makes it the default, and every entry point into the
    surface lands on the same card type as a result.
    """
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
    """Close the live session. The journal stays: §4.2 is append-only and the rows are the
    record of what the person actually said."""
    return {"ended": await session.end_session(conn, user_id=user.id)}


@router.post("/verdict")
async def verdict(
    body: VerdictBody, conn: DB, user: ActiveUser, request: Request
) -> dict[str, Any]:
    """§6.1: `Liked / Fine / Disliked`. Verdict implies `seen`.

    The reveal rides on this response and on no other, which is §6.1's anchoring rule
    (Cosley 2003) expressed as a route: the card carried no belief, the answer to the card
    carries it.
    """
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_verdict(
            conn,
            s,
            card_token=body.card_token,
            value=body.value,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, reveal=outcome.reveal, log=outcome.log,
        ledger=outcome.ledger, event_kind="verdict", user=user,
    )


@router.post("/not-seen")
async def not_seen(body: CardBody, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§6.1's one seen-state control. Owner decision 2026-08-29: a title you cannot remember is
    plain `unseen`, and the verdict and duel rows survive the flip (§4.2)."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_not_seen(
            conn,
            s,
            card_token=body.card_token,
            jf=await _jellyfin(conn),
            latency_ms=body.latency_ms,
            head=body.head,
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
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_duel(
            conn,
            s,
            card_token=body.card_token,
            outcome=body.outcome,
            decisive=body.decisive,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
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
    """§6.1's corrections row. Writes no duel row, does not advance the counter, syncs §7.3."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.record_correction(
            conn, s, card_token=body.card_token, side=body.side, jf=await _jellyfin(conn)
        )
    except session.StaleCard as exc:
        raise _stale(exc) from exc
    return await session.payload(
        conn, outcome.session, log=outcome.log, event_kind="not_seen", user=user
    )


@router.post("/undo")
async def undo(conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    """Decision 35. Refused at the block boundary with a reason, never silently no-opped — the
    chip has to be able to disable visibly, and `GET /api/rate` carries the same
    `undo.available` flag so it can do that before the tap."""
    s = await _resume(conn, user.id)
    try:
        outcome = await session.undo(
            conn,
            s,
            hp=_hyperparams(request),
            embeddings=_embeddings(request, conn),
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


@router.get("/balance")
async def class_balance(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§5.2's running class balance on its own, for the widget's own poll.

    Not partitioned by kind: §4.1 rule 5 binds surfaces that *rank*, and this one ranks
    nothing — it describes a labelling habit, and the 5x lever is about the labeller.
    """
    s = await _resume(conn, user.id)
    return (await session.payload(conn, s, user=user))["class_balance"]
