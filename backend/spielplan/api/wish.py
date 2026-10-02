"""The household's wish list and each person's Not for me (§6.0). Every member sees the whole list;
each writes only their own rows."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import BaseModel

from spielplan.api.deps import DB, ActiveUser, ActiveUserBrief, brief_connection
from spielplan.home import beyond, wish
from spielplan.models import artifacts

router = APIRouter(prefix="/api/wish", tags=["wish"])


class WishIn(BaseModel):
    state: Literal["want", "not_for_me"]


class RestoreIn(BaseModel):
    since: datetime


@router.get("")
async def wish_list(conn: DB, user: ActiveUser) -> dict[str, Any]:
    return await wish.household_list(
        conn, viewer_id=user.id, bundle_version=await artifacts.active_bundle_version(conn)
    )


@router.put("/{title_id}")
async def set_wish(title_id: int, body: WishIn, conn: DB, user: ActiveUser) -> dict[str, Any]:
    try:
        return await wish.set_state(conn, user_id=user.id, title_id=title_id, state=body.state)
    except wish.NoSuchTitle:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such title") from None
    except wish.Owned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"reason": "owned", "message": "It's in the library already."},
        ) from None


# Decision 558's two TMDB reads: behind the brief session, so no pooled connection waits on TMDB.
@router.get("/tmdb")
async def tmdb_search(
    request: Request,
    user: ActiveUserBrief,
    kind: list[Literal["movie", "series"]] = Query(...),
    q: str = "",
) -> dict[str, Any]:
    return await beyond.search_tmdb(request.app.state.art.fetcher, brief_connection, kinds=kind, q=q)


@router.put("/tmdb/{kind}/{tmdb_id}")
async def want_tmdb(
    kind: Literal["movie", "series"],
    tmdb_id: Annotated[int, Path(ge=1, le=2_147_483_647)],
    request: Request,
    user: ActiveUserBrief,
) -> dict[str, Any]:
    try:
        return await beyond.want_tmdb(
            request.app.state.art.fetcher, brief_connection, user_id=user.id, kind=kind, tmdb_id=tmdb_id
        )
    except beyond.NoSuchTmdbTitle:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "TMDB has no such title") from None
    except beyond.TmdbUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "tmdb_unavailable"}
        ) from None


@router.delete("/{title_id}")
async def clear_wish(title_id: int, conn: DB, user: ActiveUser) -> dict[str, Any]:
    return await wish.clear(conn, user_id=user.id, title_id=title_id)


@router.post("/{title_id}/dismiss")
async def dismiss_arrival(title_id: int, conn: DB, user: ActiveUser) -> dict[str, Any]:
    if not await wish.dismiss(conn, user_id=user.id, title_id=title_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing arrived to dismiss")
    return {"dismissed": True}


@router.post("/{title_id}/restore")
async def restore_arrival(title_id: int, body: RestoreIn, conn: DB, user: ActiveUser) -> dict[str, Any]:
    """An arrival's Undo after its x (decision 554)."""
    if not await wish.restore_arrival(conn, user_id=user.id, title_id=title_id, since=body.since):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing to restore")
    return {"restored": True}


@router.get("/summary")
async def wish_summary(conn: DB, user: ActiveUser) -> dict[str, int]:
    """You's Wish list row: the household's wanted count."""
    return await wish.summary(conn)
