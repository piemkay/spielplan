"""The household's wish list and each person's Not for me (§6.0). Every member sees the whole list;
each writes only their own rows."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from spielplan.api.deps import DB, ActiveUser
from spielplan.home import wish
from spielplan.models import artifacts

router = APIRouter(prefix="/api/wish", tags=["wish"])


class WishIn(BaseModel):
    state: Literal["want", "not_for_me"]


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


@router.delete("/{title_id}")
async def clear_wish(title_id: int, conn: DB, user: ActiveUser) -> dict[str, Any]:
    return await wish.clear(conn, user_id=user.id, title_id=title_id)


@router.post("/{title_id}/dismiss")
async def dismiss_arrival(title_id: int, conn: DB, user: ActiveUser) -> dict[str, Any]:
    if not await wish.dismiss(conn, user_id=user.id, title_id=title_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing arrived to dismiss")
    return {"dismissed": True}
