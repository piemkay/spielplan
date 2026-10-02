"""Seen state and the finish prompt (§4.2, §7.3). Nothing here infers: only the person writes
`user_title`, and both prompt answers are explicit (decision 211).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from spielplan.api.deps import DB, ActiveUser
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.connectors.registry import JellyfinConfig
from spielplan.sync import playback, seen

router = APIRouter(prefix="/api", tags=["state"])


class StateRequest(BaseModel):
    state: str


class PromptAnswer(BaseModel):
    finished: bool


async def jellyfin_for(conn) -> tuple[JellyfinClient | None, JellyfinConfig]:
    cfg = await registry.load_jellyfin(conn)
    return registry.make_client(cfg), cfg


@router.post("/titles/{title_id}/state")
async def set_state(title_id: int, body: StateRequest, user: ActiveUser, conn: DB) -> dict:
    """§4.2: `unseen | seen` only; verdict and duel history survive the flip. The response says whether
    Jellyfin was told."""
    if body.state not in seen.STATES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"state must be one of {', '.join(seen.STATES)}"
        )
    if not await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such title")

    client, cfg = await jellyfin_for(conn)
    return await seen.set_state(
        conn, client, cfg, user_id=user.id, title_id=title_id, state=body.state
    )


@router.get("/titles/{title_id}/state")
async def get_state(title_id: int, user: ActiveUser, conn: DB) -> dict:
    row = await conn.fetchrow(
        "SELECT state, state_changed_at, jf_synced_at FROM user_title "
        "WHERE user_id = $1 AND title_id = $2",
        user.id, title_id,
    )
    # §4.2: an absent row is `unseen`, an absence the seen sync never pushes over Jellyfin.
    if row is None:
        return {"state": "unseen", "state_changed_at": None, "jf_synced_at": None}
    return dict(row)


@router.get("/prompts/finish")
async def finish_prompts(user: ActiveUser, conn: DB) -> list[dict]:
    """§7.3's queue of undeliverable prompts, surfaced as an in-app banner."""
    return await playback.pending(conn, user.id)


@router.post("/prompts/finish/{event_id}")
async def answer_finish_prompt(
    event_id: int, body: PromptAnswer, user: ActiveUser, conn: DB
) -> dict:
    """'Yes' writes `seen` and pushes it; 'no' writes `unseen`, or the 15-minute sweep would adopt
    Jellyfin's Played flag (decision 211)."""
    client, _cfg = await jellyfin_for(conn)
    result = await playback.answer(
        conn, user_id=user.id, event_id=event_id, finished=body.finished, client=client
    )
    if not result["ok"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(result["reason"]))
    return result


@router.post("/prompts/finish/{event_id}/close")
async def close_finish_prompt(event_id: int, user: ActiveUser, conn: DB) -> dict:
    """The prompt's x: no answer, and the seen sync stays away from the title (decision 554)."""
    result = await playback.close(conn, user_id=user.id, event_id=event_id)
    if not result["ok"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(result["reason"]))
    return result


@router.post("/prompts/finish/{event_id}/reopen")
async def reopen_finish_prompt(event_id: int, user: ActiveUser, conn: DB) -> dict:
    result = await playback.reopen(conn, user_id=user.id, event_id=event_id)
    if not result["ok"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(result["reason"]))
    return result
