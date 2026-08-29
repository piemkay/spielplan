"""Seen state and the finish prompt. Spec v2.1 §4.2, §7.3.

Two things a person does, and the boundary between them is the spec's:

  * marking a title seen or unseen — an explicit action, authoritative over Jellyfin;
  * answering "Did you finish X?" — the *only* way a playback observation becomes state.

Nothing here infers. §7.3's "Jellyfin playback is a suggestion, never a silent write" is a
rule about who is allowed to write `user_title`, and the answer is: the person.
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
    """§4.2: state is `unseen | seen` and nothing else — there is no 'forgotten' (owner
    decision 2026-08-29). The verdict and duel history is append-only and survives the flip.

    The response reports whether Jellyfin was told, and why not when it was not, because
    "marked seen but your media server does not know" is a state the person can act on.
    """
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
    # §4.2: an absent row is `unseen`. That default is an absence, not an assertion — which is
    # exactly why the seen sync never pushes it over Jellyfin's history.
    if row is None:
        return {"state": "unseen", "state_changed_at": None, "jf_synced_at": None}
    return dict(row)


@router.get("/prompts/finish")
async def finish_prompts(user: ActiveUser, conn: DB) -> list[dict]:
    """§7.3: "when undeliverable, the prompt queues and surfaces as an in-app banner on next
    open. The banner path is the whole M1 behaviour." This is that queue."""
    return await playback.pending(conn, user.id)


@router.post("/prompts/finish/{event_id}")
async def answer_finish_prompt(
    event_id: int, body: PromptAnswer, user: ActiveUser, conn: DB
) -> dict:
    """The one tap. 'Yes' writes `seen` and pushes it to Jellyfin; 'no' closes the prompt and
    writes nothing at all."""
    client, _cfg = await jellyfin_for(conn)
    result = await playback.answer(
        conn, user_id=user.id, event_id=event_id, finished=body.finished, client=client
    )
    if not result["ok"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(result["reason"]))
    return result
