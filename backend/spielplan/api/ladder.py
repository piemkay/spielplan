"""The ladder's set-up (§6.1): its steps, a page of films per step, and the finish that is the cut-over."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from spielplan.api import deps
from spielplan.api.deps import DB, ActiveUser
from spielplan.ledger import ladder
from spielplan.rate import setup

router = APIRouter(prefix="/api/ladder", tags=["ladder"])

ALREADY_SET_UP = {"reason": "already_set_up", "message": "Your ladder is already set up."}
REFUSED = {
    "empty": "Put at least one film on your ladder to finish.",
    "duplicate": "A film can be on one step only.",
    "not_a_film": "The set-up takes films only.",
    "bad_tier": "That step is not on your ladder.",
}


class Pick(BaseModel):
    title_id: int
    tier: int


class FinishBody(BaseModel):
    picks: list[Pick]


@router.get("/setup")
async def setup_state(conn: DB, user: ActiveUser) -> dict[str, Any]:
    state = await ladder.state(conn, user_id=user.id)
    return {
        "done": state.done,
        "earlier_ratings": state.earlier_ratings,
        "steps": [asdict(s) for s in await setup.steps(conn, user_id=user.id)],
    }


@router.get("/setup/films")
async def setup_films(
    conn: DB,
    user: ActiveUser,
    step: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=48),
    exclude: str = Query("", pattern=r"^[0-9,]*$"),
) -> dict[str, Any]:
    """`exclude` is comma-joined title ids: the picks of the other steps."""
    if await ladder.set_up_at(conn, user_id=user.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_SET_UP)
    excluded = [int(part) for part in exclude.split(",") if part]
    try:
        films, more = await setup.films_page(
            conn, user_id=user.id, step=step, offset=offset, limit=limit, exclude=excluded
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "bad_step", "message": str(exc)}
        ) from exc
    return {"films": films, "more": more}


@router.post("/setup/finish")
async def finish(body: FinishBody, conn: DB, user: ActiveUser, request: Request) -> dict[str, Any]:
    await deps.assert_active_basis(request, conn)
    try:
        return await setup.finish(
            conn, user_id=user.id, picks=[(p.title_id, p.tier) for p in body.picks]
        )
    except ladder.AlreadySetUp as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_SET_UP) from exc
    except ladder.SetupRefused as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"reason": exc.reason, "message": REFUSED[exc.reason]},
        ) from exc
