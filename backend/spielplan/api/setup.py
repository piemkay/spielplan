"""First-boot wizard (§3.1): admin, optional connector seed, then the same bundle importer §6.6 uses.
Accounts are made at §6.6's Users card (decision 164).
"""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints

from spielplan.api.deps import DB, ActiveUser, AdminUser, current_user, set_session_cookie, write_txn
from spielplan.core import auth, secrets
from spielplan.core.config import settings
from spielplan.llm import client, spend

router = APIRouter(prefix="/api/setup", tags=["setup"])

STEPS = ("admin", "connectors", "bundle", "onboarding")

# The wizard ribbon, as data: the one thing besides `required` the anonymous login page needs.
NOTE = "first boot · a bundle-less app is a legal state"

# Trimmed before storing: login resolves on `lower(name)` (§3.1).
AccountName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class AdminInit(BaseModel):
    name: AccountName
    password: str = Field(min_length=10)


class ConnectorSeed(BaseModel):
    name: str
    config: dict = Field(default_factory=dict)
    secrets: dict | None = None


# The rows §6.6's spend guard owns. This route stays mounted after first boot and would store a
# billable config with no figure shown (decision 450).
_SPEND_GUARDED = frozenset((spend.SETTINGS, *client.PROVIDERS))
_SPEND_GUARDED_REFUSAL = (
    " is not seeded here: the extraction plan, the models and the price overrides are written by"
    " PUT /api/admin/llm with the estimate the preview showed (decision 450), the cap by"
    " PUT /api/admin/llm/cap (decision 452), and a provider's key by"
    " PUT /api/admin/connectors/<provider>"
)


async def _optional_user(
    request: Request, response: Response, conn: DB
) -> auth.SessionUser | None:
    """`current_user`, but a missing, dead or password-locked session is a stranger, not a 401 (sec-14)."""
    try:
        user = await current_user(request, response, conn)
    except HTTPException:
        return None
    return None if user.must_change_password else user


OptionalUser = Annotated[auth.SessionUser | None, Depends(_optional_user)]


@router.get("/state")
async def state(user: OptionalUser, conn: DB) -> dict[str, object]:
    """Anonymous callers get `required` and `note` only: the full payload fingerprints the install."""
    has_admin = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0
    # §3.1: the wizard is needed until an admin exists; after that it is a revisitable page.
    if user is None:
        return {"required": not has_admin, "note": NOTE}

    done = {r["step"] for r in await conn.fetch("SELECT step FROM setup_step")}
    members = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'member'")
    bundle = await conn.fetchrow(
        "SELECT version, imported_at FROM artifact_bundle WHERE state = 'active'"
    )
    return {
        "required": not has_admin,
        "steps": [{"step": s, "done": s in done} for s in STEPS],
        "has_admin": has_admin,
        "member_count": members,
        "bundle": dict(bundle) if bundle else None,
        "note": NOTE,
    }


@router.post("/admin", status_code=status.HTTP_201_CREATED)
async def create_admin(body: AdminInit, response: Response, conn: DB) -> dict[str, object]:
    """Only while no admin exists. The check and the three writes are one transaction under one lock,
    so two submits cannot both become the only admin."""
    # Hashed before the lock and off the event loop: argon2 takes tens of ms by design.
    password_hash = await auth.hash_password_async(body.password)
    try:
        async with write_txn(conn, lock="setup_admin"):
            if await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "an admin account already exists")

            user_id = await conn.fetchval(
                """
                INSERT INTO app_user (name, role, password_hash, must_change_password)
                VALUES ($1, 'admin', $2, false) RETURNING id
                """,
                body.name,
                password_hash,
            )
            await conn.execute(
                "INSERT INTO setup_step (step) VALUES ('admin') ON CONFLICT (step) DO NOTHING"
            )
            sid = await auth.create_session(conn, user_id, auth_method="password")
    except asyncpg.UniqueViolationError as exc:
        # A pre-existing non-admin row holds the name: say so rather than 500.
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    set_session_cookie(response, sid)
    return {"id": user_id, "name": body.name, "role": "admin"}


@router.post("/connectors")
async def seed_connector(body: ConnectorSeed, _: AdminUser, conn: DB) -> dict[str, object]:
    """§2: an env or wizard seed. Refuses the spend-guarded rows with 409, naming the three routes that
    do write them."""
    if body.name in _SPEND_GUARDED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.name}{_SPEND_GUARDED_REFUSAL}")
    if body.secrets:
        settings().require_secrets_key()
    # `retire_unreadable`: an admin typing a credential is the repair, as on the Connectors card.
    await secrets.put_connector_secrets(
        conn, body.name, body.config, body.secrets, retire_unreadable=True
    )
    await conn.execute(
        "INSERT INTO setup_step (step) VALUES ('connectors') ON CONFLICT (step) DO NOTHING"
    )
    return {"ok": True, "name": body.name, "has_secrets": bool(body.secrets)}


@router.post("/onboarding/complete")
async def complete_onboarding(user: ActiveUser, conn: DB) -> dict[str, bool]:
    """Recorded per user: decision 180 asks each phone once. `ActiveUser`: onboarding comes after
    §3.1's forced password change."""
    await conn.execute(
        """
        INSERT INTO setup_step (step, detail) VALUES ('onboarding', $1)
        ON CONFLICT (step) DO UPDATE SET detail = setup_step.detail || EXCLUDED.detail
        """,
        {str(user.id): True},
    )
    return {"ok": True}
