"""First-boot wizard. Spec v2.1 §3.1.

The sequence is normative: create admin -> optional env-seeded connector config ->
bundle import (the *same* importer the §6.6 Data tab exposes) -> member-account creation.
A bundle-less app is a legal state, so the bundle step is skippable and the wizard reports
that explicitly rather than blocking.

Member first-run onboarding (PWA install + push permission, §6 preamble) is the fifth step;
it records completion per user because it is a per-phone act.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, AdminUser, CurrentUser
from spielplan.core import auth, secrets
from spielplan.core.config import settings

router = APIRouter(prefix="/api/setup", tags=["setup"])

STEPS = ("admin", "connectors", "bundle", "members", "onboarding")


class AdminInit(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10)


class ConnectorSeed(BaseModel):
    name: str
    config: dict = Field(default_factory=dict)
    secrets: dict | None = None


class MemberInit(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(default="member", pattern="^(member|admin|guest)$")


@router.get("/state")
async def state(conn: DB) -> dict[str, object]:
    done = {r["step"] for r in await conn.fetch("SELECT step FROM setup_step")}
    has_admin = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0
    members = await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'member'")
    bundle = await conn.fetchrow(
        "SELECT version, imported_at FROM artifact_bundle WHERE state = 'active'"
    )
    return {
        # §3.1: the wizard is needed until an admin exists; after that it is a revisitable page.
        "required": not has_admin,
        "steps": [{"step": s, "done": s in done} for s in STEPS],
        "has_admin": has_admin,
        "member_count": members,
        "bundle": dict(bundle) if bundle else None,
        # The ribbon the prototype prints on every wizard step, as data rather than copy
        # baked into the client.
        "note": "first boot · a bundle-less app is a legal state",
    }


@router.post("/admin", status_code=status.HTTP_201_CREATED)
async def create_admin(body: AdminInit, response: Response, conn: DB) -> dict[str, object]:
    """Only callable while no admin exists — otherwise this would be a privilege-escalation
    endpoint reachable by anyone who can see the setup page."""
    if await conn.fetchval("SELECT count(*) FROM app_user WHERE role = 'admin'") > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "an admin account already exists")

    user_id = await conn.fetchval(
        """
        INSERT INTO app_user (name, role, password_hash, must_change_password)
        VALUES ($1, 'admin', $2, false) RETURNING id
        """,
        body.name,
        auth.hash_password(body.password),
    )
    await conn.execute(
        "INSERT INTO setup_step (step) VALUES ('admin') ON CONFLICT (step) DO NOTHING"
    )
    sid = await auth.create_session(conn, user_id, auth_method="password")
    cfg = settings()
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.seal_session_id(sid),
        max_age=cfg.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=cfg.public_url.startswith("https://"),
        path="/",
    )
    return {"id": user_id, "name": body.name, "role": "admin"}


@router.post("/connectors")
async def seed_connector(body: ConnectorSeed, _: AdminUser, conn: DB) -> dict[str, object]:
    """§2: connectors are configured in the admin UI and stored in `connector_config`;
    env vars may only *seed* them on first boot. Writing a secret requires SECRETS_KEY —
    the app refuses rather than falling back."""
    if body.secrets:
        settings().require_secrets_key()
    await secrets.put_connector_secrets(conn, body.name, body.config, body.secrets)
    await conn.execute(
        "INSERT INTO setup_step (step) VALUES ('connectors') ON CONFLICT (step) DO NOTHING"
    )
    return {"ok": True, "name": body.name, "has_secrets": bool(body.secrets)}


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def create_member(body: MemberInit, _: AdminUser, conn: DB) -> dict[str, object]:
    """§3.1: 'a one-time password is issued, the account is locked to a password change at
    first login, and passkey registration is prompted afterwards.'

    The OTP is returned exactly once, in this response. It is never stored in plaintext and
    there is no route that will show it again — losing it means reissuing.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM app_user WHERE lower(name) = lower($1)", body.name
    )
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists")

    otp = auth.new_one_time_password()
    user_id = await conn.fetchval(
        """
        INSERT INTO app_user (name, role, password_hash, must_change_password)
        VALUES ($1, $2, $3, true) RETURNING id
        """,
        body.name,
        body.role,
        auth.hash_password(otp),
    )
    if body.role == "member":
        await conn.execute(
            "INSERT INTO setup_step (step) VALUES ('members') ON CONFLICT (step) DO NOTHING"
        )
    return {
        "id": user_id,
        "name": body.name,
        "role": body.role,
        "one_time_password": otp,
        "note": "shown once — the account is locked to a password change at first login",
    }


@router.post("/onboarding/complete")
async def complete_onboarding(user: CurrentUser, conn: DB) -> dict[str, bool]:
    """§6 preamble: iOS has no programmatic install prompt, so onboarding is a guided act the
    phone confirms. Recorded per user so the nag can stop for that phone and only that phone."""
    await conn.execute(
        """
        INSERT INTO setup_step (step, detail) VALUES ('onboarding', $1)
        ON CONFLICT (step) DO UPDATE SET detail = setup_step.detail || EXCLUDED.detail
        """,
        {str(user.id): True},
    )
    return {"ok": True}
