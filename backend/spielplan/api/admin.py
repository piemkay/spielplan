"""Admin routes for the Jellyfin connector. Spec v2.1 §6.6 (Connectors, Users), §3.3, §7.

§6.6's Connectors card: "Jellyfin (URL, API key, library pick, user-mapping table, test
button, sync now, webhook status)". M1 ships all of it but the library pick and the webhook —
§7.2's webhook belongs to the acquisition trigger, which is M5.

Every route in this module is `AdminUser`, which means three things at once (§3.1, §3.2,
§6.6): a member gets 403, a signed-out caller gets 401, and an admin whose last password
authentication is older than 24 h is re-prompted before the route resolves.

The API key never comes back out. `has_api_key` is a boolean and the field posts empty to mean
"leave it alone" — §14.3 is blunt that this key is admin-equivalent on the whole media server,
and a GET that returns it turns every admin session into a copy of it.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, AdminUser
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError
from spielplan.connectors.registry import load_jellyfin, save_jellyfin
from spielplan.sync import playback, seen

router = APIRouter(prefix="/api/admin", tags=["admin"])


class JellyfinSettings(BaseModel):
    # Empty means "keep the stored one" for both fields: the form shows the key as a mask and
    # a partial save must never blank the half it did not send.
    url: str = Field(default="", max_length=512)
    api_key: str = ""


class LinkRequest(BaseModel):
    jellyfin_user_id: str = Field(min_length=1, max_length=64)
    # §7.3's least-privilege write path costs "one-time password entry per linked user".
    # Optional: a link without a token still drives the P(seen) prior and attribution, it just
    # cannot write Played state until someone completes it.
    jellyfin_username: str | None = None
    jellyfin_password: str | None = None


async def _client(conn) -> JellyfinClient:
    client = registry.make_client(await load_jellyfin(conn))
    if client is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Jellyfin is not configured — set its URL and API key first"
        )
    return client


@router.get("/connectors/jellyfin")
async def get_jellyfin(_: AdminUser, conn: DB) -> dict[str, object]:
    cfg = await load_jellyfin(conn)
    return {
        "url": cfg.url,
        "has_api_key": bool(cfg.api_key),
        "configured": cfg.configured,
        "library_ids": cfg.library_ids,
        "linked_users": len(cfg.user_tokens),
    }


@router.put("/connectors/jellyfin")
async def put_jellyfin(body: JellyfinSettings, _: AdminUser, conn: DB) -> dict[str, object]:
    cfg = await save_jellyfin(conn, url=body.url or None, api_key=body.api_key or None)
    return {"url": cfg.url, "has_api_key": bool(cfg.api_key), "configured": cfg.configured}


@router.post("/connectors/jellyfin/test")
async def test_jellyfin(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's test button. §7.1 pins Jellyfin >= 10.9, so the probe reports the version and
    whether it clears that bar rather than only whether the socket opened."""
    client = await _client(conn)
    try:
        return {"ok": True, **await client.check()}
    except JellyfinError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status}


@router.get("/connectors/jellyfin/users")
async def jellyfin_users(_: AdminUser, conn: DB) -> list[dict[str, object]]:
    """§3.3: "Admin view maps each app user <-> one Jellyfin user (GET /Users)"."""
    client = await _client(conn)
    try:
        users = await client.users()
    except JellyfinError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Jellyfin: {exc}") from exc
    return [{"id": u.id, "name": u.name, "is_admin": u.is_admin} for u in users]


@router.get("/users")
async def app_users(_: AdminUser, conn: DB) -> list[dict[str, object]]:
    """The user-mapping table's left-hand column (§6.6 Users)."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.name, u.role, u.is_active, u.jellyfin_user_id, u.jellyfin_link_state,
               u.pin_hash IS NOT NULL AS has_pin,
               (SELECT count(*) FROM webauthn_credential c WHERE c.user_id = u.id) AS passkeys
          FROM app_user u ORDER BY u.id
        """
    )
    cfg = await load_jellyfin(conn)
    return [
        {**dict(r), "has_jellyfin_token": cfg.token_for(r["id"]) is not None} for r in rows
    ]


@router.post("/users/{user_id}/jellyfin")
async def link_jellyfin(
    user_id: int, body: LinkRequest, _: AdminUser, conn: DB
) -> dict[str, object]:
    """§3.3: the map is optional and one-to-one.

    One-to-one is held by the partial unique index on `jellyfin_user_id`, not by a lookup
    before the write: two admins linking at once would both pass the lookup. The 409 below is
    the index reporting the collision it actually prevented.
    """
    if not await conn.fetchval("SELECT 1 FROM app_user WHERE id = $1", user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")

    token: str | None = None
    if body.jellyfin_username and body.jellyfin_password:
        client = await _client(conn)
        try:
            jf_user_id, token = await client.authenticate_by_name(
                body.jellyfin_username, body.jellyfin_password
            )
        except JellyfinError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, f"Jellyfin refused that sign-in: {exc}"
            ) from exc
        if jf_user_id != body.jellyfin_user_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "those Jellyfin credentials belong to a different Jellyfin user",
            )

    try:
        await conn.execute(
            "UPDATE app_user SET jellyfin_user_id = $2, jellyfin_link_state = $3 WHERE id = $1",
            user_id,
            body.jellyfin_user_id,
            # A link with no token is real but incomplete: it attributes playback and feeds the
            # P(seen) prior, and it cannot write Played state until someone signs in (§7.3).
            "linked" if token else "needs_relink",
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that Jellyfin user is already linked to another account (§3.3: one-to-one)",
        ) from exc

    if token:
        cfg = await load_jellyfin(conn)
        tokens = dict(cfg.user_tokens)
        tokens[str(user_id)] = token
        await save_jellyfin(conn, user_tokens=tokens)
    else:
        # A stored token belongs to one Jellyfin identity. Re-pointing this account at a
        # different Jellyfin user without a new sign-in must drop the old one, or the next
        # Played write sends the previous user's credential with the new user's id.
        await seen.forget_token(conn, user_id)

    return {
        "ok": True,
        "user_id": user_id,
        "jellyfin_user_id": body.jellyfin_user_id,
        "has_token": bool(token),
        "state": "linked" if token else "needs_relink",
    }


@router.delete("/users/{user_id}/jellyfin")
async def unlink_jellyfin(user_id: int, _: AdminUser, conn: DB) -> dict[str, bool]:
    await seen.unlink(conn, user_id)
    return {"ok": True}


@router.post("/connectors/jellyfin/sync")
async def sync_now(_: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's "sync now", and the same code path as the 15-minute job (§5.3)."""
    cfg = await load_jellyfin(conn)
    return (await seen.sync_all(conn, registry.make_client(cfg))).as_dict()


@router.post("/connectors/jellyfin/poll")
async def poll_now(_: AdminUser, conn: DB) -> dict[str, object]:
    """The /Sessions watcher, on demand. §7.3's arming path runs on a 1-minute trigger; this
    is the same call, so an admin can prove the prompt arrives without waiting for one."""
    cfg = await load_jellyfin(conn)
    return (await playback.poll(conn, registry.make_client(cfg))).as_dict()
