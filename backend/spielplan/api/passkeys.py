"""Passkey routes. Spec v2.1 §3.2 — "Primary: WebAuthn passkeys".

Two ceremonies, four routes each half. Registration requires a session (§3.1 prompts it after
the forced first password change, so there is always one) — and specifically a `CredentialedUser`
one, because §3.2 makes the password the account credential and the PIN a convenience for a
handed-over device, so minting or revoking a permanent passkey from a 4-digit switch is not a
thing this surface allows. Sign-in cannot require a session, which is the whole point of it.

The verification itself lives in `core.webauthn`; this module is the HTTP shape and the
session it produces. A passkey sign-in creates exactly the same session row a password does,
with `auth_method = 'passkey'` — §3.2 makes passkeys primary and passwords the fallback, not
two different classes of session.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, ActiveUser, CredentialedUser, set_session_cookie, write_txn
from spielplan.core import auth, webauthn

router = APIRouter(prefix="/api/auth/passkey", tags=["auth"])


class PasskeyCredential(BaseModel):
    """The shape of a `PublicKeyCredential` as the browser serialises it.

    Declared so that a body missing `response` is a 422 from FastAPI *before* the route runs.
    It used to be a bare `dict`, and the ceremony consumed its single-use challenge and only
    then failed on the missing key — so the honest user's retry was refused as expired too
    (sec-04). The field names are the wire's, not this codebase's.
    """

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
    """Deleting the last passkey is allowed: §3.2 keeps password login always available, so
    there is no lock-out to protect against and refusing would just strand a lost device."""
    removed = await webauthn.delete_credential(conn, user.id, credential_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey on this account")
    return {"ok": True}


@router.post("/login/options")
async def login_options(body: LoginOptions, conn: DB) -> dict[str, object]:
    # The one anonymous write in the app (sec-14). Its bound is enforced by eviction inside
    # `_issue` rather than by a refusal here: a global cap on an anonymous route is a refusal
    # a stranger chooses for the household, and §3.2 makes this the primary way in.
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
    # The session this device was already holding does not survive a fresh sign-in on it, for
    # the reason `api/auth.py`'s login gives: one cookie names one row, and the row the cookie
    # stops naming otherwise stays live for its full sliding 90 days (dd24). After the assertion
    # verifies, never before — a failed ceremony must not be able to sign a device out.
    # The pair is one duty and runs inside `write_txn`, for the reason `api/auth.py`'s login
    # gives at the same seam: on autocommit the DELETE committed before the INSERT was
    # attempted, so a failure between them signed the device out of the session it arrived
    # holding — and §3.2 makes this the primary way back in.
    stale = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    async with write_txn(conn):
        if stale:
            await auth.destroy_session(conn, stale)
        sid = await auth.create_session(
            conn,
            user_id,
            auth_method="passkey",
            device_label=body.device_label,
            # §3.2's passkey is biometric; a presence-only tap signs in but does not answer the
            # admin re-prompt, so the authenticator's UV flag — not the ceremony's success — is
            # what stamps `admin_verified_at`.
            verified=user_verified,
        )
    set_session_cookie(response, sid)
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "must_change_password": row["must_change_password"],
    }
