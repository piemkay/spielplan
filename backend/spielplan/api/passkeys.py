"""Passkey routes (§3.2). Registration needs a `CredentialedUser` (never a PIN session); sign-in needs
no session and creates the same session row a password does.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser, CredentialedUser, set_session_cookie, write_txn
from spielplan.core import auth, webauthn

router = APIRouter(prefix="/api/auth/passkey", tags=["auth"])


class PasskeyCredential(BaseModel):
    """The browser's `PublicKeyCredential`, declared so a malformed body is a 422 before the single-use
    challenge is consumed. Field names are the wire's."""

    id: str
    rawId: str
    type: str
    response: dict
    clientExtensionResults: dict = Field(default_factory=dict)


class RegisterVerify(BaseModel):
    ceremony_id: str
    credential: PasskeyCredential
    label: str | None = Field(default=None, max_length=64)


class LoginOptions(BaseModel):
    # Optional: with discoverable credentials the phone offers the account itself.
    name: str | None = None


class LoginVerify(BaseModel):
    ceremony_id: str
    credential: PasskeyCredential
    device_label: str | None = None


@router.post("/register/options")
async def register_options(user: CredentialedUser, conn: DB) -> dict[str, object]:
    ceremony = await webauthn.registration_options(conn, user_id=user.id, user_name=user.name)
    return {"ceremony_id": ceremony.id, "options": ceremony.options}


@router.post("/register")
async def register(body: RegisterVerify, user: CredentialedUser, conn: DB) -> dict[str, object]:
    try:
        credential = await webauthn.register(
            conn,
            user_id=user.id,
            handle=body.ceremony_id,
            credential=body.credential.model_dump(),
            label=body.label,
        )
    except webauthn.PasskeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "credential": credential}


@router.get("/credentials")
async def credentials(user: ActiveUser, conn: DB) -> list[dict[str, object]]:
    return await webauthn.list_credentials(conn, user.id)


@router.delete("/credentials/{credential_id:path}")
async def remove_credential(
    credential_id: str, user: CredentialedUser, conn: DB
) -> dict[str, bool]:
    """Deleting the last passkey is allowed: password login always remains (§3.2)."""
    removed = await webauthn.delete_credential(conn, user.id, credential_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey on this account")
    return {"ok": True}


@router.post("/login/options")
async def login_options(body: LoginOptions, conn: DB) -> dict[str, object]:
    # The one anonymous write; bounded by eviction in `_issue`, never by a refusal (§3.2).
    ceremony = await webauthn.authentication_options(conn, name=body.name)
    return {"ceremony_id": ceremony.id, "options": ceremony.options}


@router.post("/login")
async def login(
    body: LoginVerify, request: Request, response: Response, conn: DB
) -> dict[str, object]:
    try:
        user_id, user_verified = await webauthn.authenticate(
            conn, handle=body.ceremony_id, credential=body.credential.model_dump()
        )
    except webauthn.PasskeyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    row = await conn.fetchrow(
        "SELECT id, name, role, must_change_password FROM app_user WHERE id = $1", user_id
    )
    # Replaces this device's existing session only after the assertion verifies, in one transaction,
    # as `api/auth.py`'s login does.
    stale = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    async with write_txn(conn):
        if stale:
            await auth.destroy_session(conn, stale)
        sid = await auth.create_session(
            conn,
            user_id,
            auth_method="passkey",
            device_label=body.device_label,
            # The UV flag, not the ceremony's success, answers the admin re-prompt (§3.2).
            verified=user_verified,
        )
    set_session_cookie(response, sid)
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "must_change_password": row["must_change_password"],
    }
