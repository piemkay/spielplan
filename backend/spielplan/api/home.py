"""Home and the model-log rail (§6.0, §6.7). Every response leaves through `rail.redact`, so decision
117's toggle is one gate at one exit.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.db import library
from spielplan.home import notices, rail, shelves
from spielplan.models import artifacts

router = APIRouter(prefix="/api", tags=["home"])


def _kinds(kind: list[str]) -> list[str]:
    try:
        return library.normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


async def _bundle(request: Request, conn: asyncpg.Connection) -> str | None:
    """The ACTIVE bundle row: scores are bound to it, and a stale store is the one that is wrong (§10).
    Falls back to the loaded store so a pre-row Home can still name a version."""
    active = await artifacts.active_bundle_version(conn)
    if active is not None:
        return active
    store = getattr(request.app.state, "artifacts", None)
    return None if store is None or store.is_empty else store.version


@router.get("/home")
async def home(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: list[Literal["movie", "series"]] = Query(
        ..., description="§4.1 rule 5: one or both, never neither. Repeat the parameter for both."
    ),
) -> dict[str, Any]:
    """Kind-headed shelves (§6.0); the catalog grid is `/api/titles`."""
    payload = await shelves.build_home(
        conn, user=user, kinds=_kinds(kind), bundle_version=await _bundle(request, conn)
    )
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/home/rows")
async def home_rows(
    conn: DB,
    user: ActiveUser,
    request: Request,
    day: date,
    kind: list[Literal["movie", "series"]] = Query(...),
    shown: list[int] = Query([]),
) -> dict[str, Any]:
    """The rest of `day`'s rows after the first read, leaving out the titles it `shown` (decision 563)."""
    payload = await shelves.build_rows(
        conn, user=user, kinds=_kinds(kind), bundle_version=await _bundle(request, conn),
        day=day.isoformat(), shown=shown,
    )
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/home/worth-getting")
async def worth_getting(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: Literal["movie", "series"],
    for_: int | Literal["everyone"] | None = Query(None, alias="for"),
) -> dict[str, Any]:
    """Worth getting's See all: for one member, the viewer by default, or for everyone."""
    try:
        payload = await shelves.worth_getting_list(
            conn, user_id=user.id, kind=kind, audience=for_,
            bundle_version=await _bundle(request, conn),
        )
    except shelves.NotPickable as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "not_pickable", "message": str(exc)}
        ) from exc
    return rail.redact(payload, show_model=rail.visible_to(user))


Notice = Literal["pending", "setup", "wish_list"]


@router.put("/home/notices/{notice}")
async def hide_notice(notice: Notice, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """A sticky notice's x: hidden until tomorrow, or until something new joins it (decision 554)."""
    return await notices.hide(conn, user_id=user.id, notice=notice)


@router.delete("/home/notices/{notice}")
async def unhide_notice(notice: Notice, conn: DB, user: ActiveUser) -> dict[str, Any]:
    return await notices.unhide(conn, user_id=user.id, notice=notice)


@router.get("/model-log")
async def model_log(
    user: ActiveUser, limit: int = Query(rail.RAIL_LIMIT, ge=1, le=rail.RAIL_LIMIT)
):
    """Decision 117: with the toggle off there is no `events` key at all; a promise kept in CSS is not
    kept. `le=RAIL_LIMIT`: §6.7's ~15 is the buffer's depth."""
    if not rail.visible_to(user):
        return {
            "show_model": False,
            "hint": "turn on 'show the model' in the account menu to see the model log",
        }
    events = rail.recent(user_id=user.id, limit=limit)
    return {
        "show_model": True,
        "limit": limit,
        "kinds": rail.kinds_present(events),
        "events": events,
    }
