"""Passkey routes. Spec v2.1 §3.2 — "Primary: WebAuthn passkeys".

Two ceremonies, four routes each half. Registration requires a session (§3.1 prompts it after
the forced first password change, so there is always one); sign-in cannot, which is the whole
point of it.

The verification itself lives in `core.webauthn`; this module is the HTTP shape and the
session it produces. A passkey sign-in creates exactly the same session row a password does,
with `auth_method = 'passkey'` — §3.2 makes passkeys primary and passwords the fallback, not
two different classes of session.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from spielplan.api.auth import set_session_cookie
from spielplan.api.deps import DB, ActiveUser, CurrentUser
from spielplan.core import auth, webauthn

router = APIRouter(prefix="/api/auth/passkey", tags=["auth"])


class RegisterVerify(BaseModel):
    ceremony_id: str
    credential: dict
    label: str | None = Field(default=None, max_length=64)


class LoginOptions(BaseModel):
    # Optional: with discoverable credentials the phone offers the account itself.
    name: str | None = None


class LoginVerify(BaseModel):
    ceremony_id: str
    credential: dict
    device_label: str | None = None


@router.post("/register/options")
async def register_options(user: ActiveUser, conn: DB) -> dict[str, object]:
    ceremony = await webauthn.registration_options(conn, user_id=user.id, user_name=user.name)
    return {"ceremony_id": ceremony.id, "options": ceremony.options}


@router.post("/register")
async def register(body: RegisterVerify, user: ActiveUser, conn: DB) -> dict[str, object]:
    try:
        credential = await webauthn.register(
            conn,
            user_id=user.id,
            handle=body.ceremony_id,
            credential=body.credential,
            label=body.label,
        )
    except webauthn.PasskeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "credential": credential}


@router.get("/credentials")
async def credentials(user: CurrentUser, conn: DB) -> list[dict[str, object]]:
    return await webauthn.list_credentials(conn, user.id)


@router.delete("/credentials/{credential_id:path}")
async def remove_credential(credential_id: str, user: ActiveUser, conn: DB) -> dict[str, bool]:
    """Deleting the last passkey is allowed: §3.2 keeps password login always available, so
    there is no lock-out to protect against and refusing would just strand a lost device."""
    removed = await webauthn.delete_credential(conn, user.id, credential_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey on this account")
    return {"ok": True}


@router.post("/login/options")
async def login_options(body: LoginOptions, conn: DB) -> dict[str, object]:
    ceremony = await webauthn.authentication_options(conn, name=body.name)
    return {"ceremony_id": ceremony.id, "options": ceremony.options}


@router.post("/login")
async def login(body: LoginVerify, response: Response, conn: DB) -> dict[str, object]:
    try:
        user_id = await webauthn.authenticate(
            conn, handle=body.ceremony_id, credential=body.credential
        )
    except webauthn.PasskeyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    row = await conn.fetchrow(
        "SELECT id, name, role, must_change_password FROM app_user WHERE id = $1", user_id
    )
    sid = await auth.create_session(
        conn, user_id, auth_method="passkey", device_label=body.device_label
    )
    set_session_cookie(response, sid)
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "must_change_password": row["must_change_password"],
    }
