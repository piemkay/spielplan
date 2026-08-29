"""Auth routes. Spec v2.1 §3.1, §3.2.

M0 ships the always-available fallbacks: password login, long-lived per-device session
cookies, the forced first-login password change, and the shared-device PIN switch.
Passkey registration/assertion lands at M1 (§12) and plugs into the same session table.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from spielplan.api.deps import DB, CurrentUser
from spielplan.core import auth
from spielplan.core.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    name: str
    password: str
    device_label: str | None = None


class PinSwitchRequest(BaseModel):
    user_id: int
    pin: str = Field(min_length=4, max_length=12)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class SetPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=12, pattern=r"^\d+$")


class PreferencesRequest(BaseModel):
    show_model: bool


def _set_cookie(response: Response, sid: str) -> None:
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


def _me(user: auth.SessionUser) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "auth_method": user.auth_method,
        "admin_reauth_required": user.is_admin and user.admin_reauth_required(),
        "show_model": user.show_model,
    }


@router.post("/login")
async def login(body: LoginRequest, response: Response, conn: DB) -> dict[str, object]:
    row = await conn.fetchrow(
        "SELECT id, name, role, password_hash, must_change_password FROM app_user "
        "WHERE lower(name) = lower($1) AND is_active",
        body.name,
    )
    if row is None or not auth.verify_password(row["password_hash"], body.password):
        # One message for both cases: an attacker learns nothing about which names exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong name or password")

    if auth.needs_rehash(row["password_hash"]):
        await conn.execute(
            "UPDATE app_user SET password_hash = $2 WHERE id = $1",
            row["id"],
            auth.hash_password(body.password),
        )

    sid = await auth.create_session(
        conn, row["id"], auth_method="password", device_label=body.device_label
    )
    _set_cookie(response, sid)
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "must_change_password": row["must_change_password"],
    }


@router.post("/switch")
async def pin_switch(
    body: PinSwitchRequest, response: Response, conn: DB, current: CurrentUser
) -> dict[str, object]:
    """§3.2: 'the account chip switches between member profiles, gated by the per-user PIN
    (the chip reads "member · passkey + PIN")'.

    *Switching*, not signing in: the caller must already hold a session. A 4-digit PIN is a
    convenience for a household device someone is already signed in on, and accepting one from
    an anonymous caller would make it the whole authentication story for every account that set
    one — 10,000 guesses against a route with no other gate.
    """
    ok, reason = await auth.check_pin(conn, body.user_id, body.pin)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, reason or "wrong PIN")

    row = await conn.fetchrow(
        "SELECT id, name, role FROM app_user WHERE id = $1 AND is_active", body.user_id
    )
    # The device is handed over, so the session it was holding does not travel with it.
    await auth.destroy_session(conn, current.session_id)
    sid = await auth.create_session(conn, row["id"], auth_method="pin")
    _set_cookie(response, sid)
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


@router.post("/logout")
async def logout(request: Request, response: Response, conn: DB) -> dict[str, bool]:
    """§3.2: 'Logout clears the session cookie only — passkeys remain registered.'"""
    sid = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    if sid:
        await auth.destroy_session(conn, sid)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser) -> dict[str, object]:
    return _me(user)


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest, user: CurrentUser, conn: DB
) -> dict[str, object]:
    """The one route reachable while `must_change_password` is set (§3.1).

    A password change revokes every other session for the account. A change made because the
    old password leaked has to actually end the other sessions; otherwise every device that
    already has a cookie stays signed in for the full sliding 90 days.
    """
    stored = await conn.fetchval("SELECT password_hash FROM app_user WHERE id = $1", user.id)
    if not auth.verify_password(stored, body.current_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong current password")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "new password must differ")
    await conn.execute(
        "UPDATE app_user SET password_hash = $2, must_change_password = false WHERE id = $1",
        user.id,
        auth.hash_password(body.new_password),
    )
    revoked = await auth.destroy_other_sessions(conn, user.id, keep=user.session_id)
    return {"ok": True, "sessions_revoked": revoked}


@router.post("/pin")
async def set_pin(body: SetPinRequest, user: CurrentUser, conn: DB) -> dict[str, bool]:
    if user.must_change_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "change your password first")
    await conn.execute(
        "UPDATE app_user SET pin_hash = $2 WHERE id = $1", user.id, auth.hash_pin(body.pin)
    )
    return {"ok": True}


@router.post("/preferences")
async def set_preferences(
    body: PreferencesRequest, user: CurrentUser, conn: DB
) -> dict[str, object]:
    """§6.7: the per-user "show the model" toggle, reached from the account dropdown.

    A preference, not a role: it reveals what the model is doing to the person whose model it
    is. It gates the transparency rail and the inline numeric annotations; the title card's
    model line is deliberately outside it (§6.0).
    """
    await conn.execute(
        "UPDATE app_user SET show_model = $2 WHERE id = $1", user.id, body.show_model
    )
    return {"ok": True, "show_model": body.show_model}


@router.get("/switchable")
async def switchable(conn: DB, _: CurrentUser) -> list[dict[str, object]]:
    """The account chip's switch list. Only accounts that have set a PIN can be switched to —
    otherwise the chip would offer a door with no lock on it.

    Authenticated: the list is the household roster, and an anonymous caller has no business
    enumerating who lives here or which of them have a PIN worth guessing.
    """
    rows = await conn.fetch(
        "SELECT id, name, role, colour, avatar FROM app_user "
        "WHERE is_active AND pin_hash IS NOT NULL ORDER BY name"
    )
    return [dict(r) for r in rows]
