"""Your taste and Compare (§6.5). The numbers leave only through `rail.redact` (§6.7)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.home import rail
from spielplan.taste import chart

router = APIRouter(prefix="/api/taste", tags=["taste"])

Kind = Literal["movie", "series"]


@router.get("")
async def your_taste(conn: DB, user: ActiveUser, kind: Kind = Query(...)) -> dict[str, Any]:
    payload = await chart.chart(conn, user_id=user.id, kind=kind)
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/members")
async def members(conn: DB, user: ActiveUser, kind: Kind = Query(...)) -> dict[str, Any]:
    return await chart.members(conn, viewer_id=user.id, kind=kind)


@router.get("/compare")
async def compare(
    conn: DB, user: ActiveUser, a: int, b: int, kind: Kind = Query(...)
) -> dict[str, Any]:
    try:
        payload = await chart.compare(conn, viewer_id=user.id, kind=kind, a=a, b=b)
    except chart.NotPickable as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "not_pickable", "message": str(exc)}
        ) from exc
    return rail.redact(payload, show_model=rail.visible_to(user))
