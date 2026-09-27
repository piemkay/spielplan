"""Auth routes (§3.1, §3.2): password login, sessions, the forced first-login change, the PIN switch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, Field

from spielplan.api.deps import (
    DB,
    ActiveUser,
    CredentialedUser,
    CurrentUser,
    printable,
    set_session_cookie,
    write_txn,
)
from spielplan.core import auth
from spielplan.core.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # Length bounds are part of the defence: this anonymous route reaches argon2 and a query (sec-02).
    name: Annotated[str, AfterValidator(printable)] = Field(max_length=128)
    password: Annotated[str, AfterValidator(printable)] = Field(max_length=128)
    device_label: Annotated[str, AfterValidator(printable)] | None = Field(default=None, max_length=256)


# ASCII digits only: pydantic's `\d` accepts Arabic-Indic and fullwidth digits.
_PIN_DIGITS = r"^[0-9]+$"


class PinSwitchRequest(BaseModel):
    user_id: int
    pin: str = Field(min_length=4, max_length=12, pattern=_PIN_DIGITS)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class SetPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=12, pattern=_PIN_DIGITS)
    # Decision 170: setting a PIN costs the credential. Required, so an omission is a 422.
    current_password: str


class ReauthRequest(BaseModel):
    password: str = Field(max_length=128)


class PreferencesRequest(BaseModel):
    show_model: bool


# Decision 488: an unshipped surface is absent from navigation; `built` flips when it ships.
SURFACES: tuple[dict[str, str | bool], ...] = (
    {"key": "home", "href": "/", "label": "Home", "milestone": "M0", "built": True},
    {"key": "rate", "href": "/rate", "label": "Rate", "milestone": "M2", "built": True},
    {"key": "tonight", "href": "/tonight", "label": "Tonight", "milestone": "M4", "built": True},
    {"key": "rank", "href": "/rank", "label": "Rank", "milestone": "M3", "built": True},
    {"key": "map", "href": "/map", "label": "Map", "milestone": "M6", "built": False},
    {"key": "taste", "href": "/taste", "label": "Taste", "milestone": "M6", "built": False},
)


def shipped(key: str) -> bool:
    """What every entry point to surface `key` asks (decision 488)."""
    return any(s["key"] == key and s["built"] for s in SURFACES)


def _nav(user: auth.SessionUser) -> dict[str, list[dict[str, str | bool]]]:
    """Server-computed, so a hidden entry is absent from the response, not just the screen."""
    account: list[dict[str, str | bool]] = [
        {"key": "account", "href": "/account", "label": "Account & passkeys"},
    ]
    if user.is_admin:
        # Overview links the setup wizard (decision 527).
        account.append({"key": "admin", "href": "/admin", "label": "Admin"})
    return {"surfaces": [dict(s) for s in SURFACES if s["built"]], "account": account}


def _me(user: auth.SessionUser) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "auth_method": user.auth_method,
        "admin_reauth_required": user.is_admin and user.admin_reauth_required(),
        "show_model": user.show_model,
        "nav": _nav(user),
    }


@router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response, conn: DB
) -> dict[str, object]:
    # Trimmed on both sides: older rows may carry invisible whitespace under the `lower(name)` index.
    name = body.name.strip()
    row = await conn.fetchrow(
        "SELECT id, name, role, password_hash, must_change_password FROM app_user "
        "WHERE lower(btrim(name)) = lower($1) AND is_active",
        name,
    )
    # No short-circuit on an absent name: `check_password` costs the same either way and carries the
    # lockout (§3.2).
    ok, reason = await auth.check_password(
        conn, row["id"] if row is not None else None, body.password
    )
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, reason or auth.PASSWORD_REFUSAL)

    if auth.needs_rehash(row["password_hash"]):
        await conn.execute(
            "UPDATE app_user SET password_hash = $2 WHERE id = $1",
            row["id"],
            await auth.hash_password_async(body.password),
        )

    # Replaces this device's session (one cookie, one row), only after the credential checks.
    stale = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    # One duty, one `write_txn`: on autocommit a failed INSERT left the device signed out. The lockout
    # clear and argon2 stay outside it.
    async with write_txn(conn):
        if stale:
            await auth.destroy_session(conn, stale)
        sid = await auth.create_session(
            conn, row["id"], auth_method="password", device_label=body.device_label
        )
    set_session_cookie(response, sid)
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
    """§3.2's profile switch. Needs a session: an anonymous PIN route would be 10^4 guesses with no
    other gate. `CurrentUser`: reachable while §3.1's lock stands (decision 179)."""
    ok, reason = await auth.check_pin(conn, body.user_id, body.pin)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, reason or "wrong PIN")

    row = await conn.fetchrow(
        "SELECT id, name, role FROM app_user WHERE id = $1 AND is_active", body.user_id
    )
    # The handed-over device's session does not travel; one `write_txn`, as in `login`.
    async with write_txn(conn):
        await auth.destroy_session(conn, current.session_id)
        sid = await auth.create_session(conn, row["id"], auth_method="pin")
    set_session_cookie(response, sid)
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


@router.post("/logout")
async def logout(request: Request, response: Response, conn: DB) -> dict[str, bool]:
    """§3.2: clears the session only; passkeys stay registered."""
    sid = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    if sid:
        await auth.destroy_session(conn, sid)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser, conn: DB) -> dict[str, object]:
    """The passkey count and Jellyfin link drive shell prompts (§3.1, §7.3)."""
    row = await conn.fetchrow(
        """
        SELECT u.jellyfin_user_id, u.jellyfin_link_state, u.pin_hash IS NOT NULL AS has_pin,
               (SELECT count(*) FROM webauthn_credential c
                 WHERE c.user_id = u.id AND c.rp_id = $2) AS passkeys
          FROM app_user u WHERE u.id = $1
        """,
        user.id,
        settings().rp_id,
    )
    payload = _me(user)
    payload["has_pin"] = bool(row["has_pin"])
    payload["passkeys"] = int(row["passkeys"])
    payload["jellyfin"] = {
        "linked": row["jellyfin_user_id"] is not None,
        "state": row["jellyfin_link_state"],
    }
    return payload


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest, response: Response, user: CurrentUser, conn: DB
) -> dict[str, object]:
    """Ends §3.1's forced change (decision 179). Revokes every other session and rotates the caller's
    own (decision 208); `sessions_revoked` counts only the others. Five writes, one `write_txn`;
    argon2 runs before it opens."""
    # Through `check_password`, so §3.2's lockout counts here too; the wording stays this route's own.
    ok, _ = await auth.check_password(conn, user.id, body.current_password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong current password")
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "new password must differ")
    password_hash = await auth.hash_password_async(body.new_password)
    async with write_txn(conn):
        await conn.execute(
            "UPDATE app_user SET password_hash = $2, must_change_password = false WHERE id = $1",
            user.id,
            password_hash,
        )
        # A new password clears the guesses against the old one (§3.2).
        await auth.clear_password_lockout(conn, user.id)
        revoked = await auth.destroy_other_sessions(conn, user.id, keep=user.session_id)
        # Two DELETEs: `revoked` counts other devices only. The new row keeps the old `auth_method`, so a
        # PIN-switched phone does not come out holding a 'password' session.
        label = await conn.fetchval(
            "SELECT device_label FROM auth_session WHERE id = $1", user.session_id
        )
        await auth.destroy_session(conn, user.session_id)
        sid = await auth.create_session(
            conn, user.id, auth_method=user.auth_method, device_label=label
        )
    # Drop `current_user`'s slide cookie: it names the row just deleted.
    del response.headers["set-cookie"]
    set_session_cookie(response, sid)
    return {"ok": True, "sessions_revoked": revoked}


@router.post("/reauth")
async def reauth(body: ReauthRequest, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§3.2's 24 h admin re-prompt, answered on the session in hand. `ActiveUser`: not one of decision
    179's four routes."""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    # Counted, as in `change_password`.
    ok, _ = await auth.check_password(conn, user.id, body.password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong current password")
    if not await auth.stamp_admin_verified(conn, user.session_id):
        # A PIN session cannot be the proof the re-prompt asks for (§3.2).
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "sign in with your password or a passkey to reach the admin surface",
        )
    # `user` predates the stamp; answer with the stamped view.
    return _me(replace(user, admin_verified_at=datetime.now(UTC)))


@router.post("/pin")
async def set_pin(body: SetPinRequest, user: CredentialedUser, conn: DB) -> dict[str, bool]:
    """Setting the PIN costs the password (decision 170); `CredentialedUser` carries §3.1's lock and
    refuses a PIN session. The counters clear with it."""
    # Counted: guessing the password here would otherwise buy a PIN for free.
    ok, _ = await auth.check_password(conn, user.id, body.current_password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong current password")
    await conn.execute(
        "UPDATE app_user SET pin_hash = $2, pin_failed_count = 0, pin_locked_until = NULL "
        "WHERE id = $1",
        user.id,
        await auth.hash_pin_async(body.pin),
    )
    return {"ok": True}


@router.post("/preferences")
async def set_preferences(
    body: PreferencesRequest, user: ActiveUser, conn: DB
) -> dict[str, object]:
    """§6.7's per-user toggle: gates the rail, inline numbers and the card's model line (decision 486)."""
    await conn.execute(
        "UPDATE app_user SET show_model = $2 WHERE id = $1", user.id, body.show_model
    )
    return {"ok": True, "show_model": body.show_model}


@router.get("/switchable")
async def switchable(conn: DB, _: ActiveUser) -> list[dict[str, object]]:
    """Accounts with a PIN only. Authenticated: the roster is not for strangers."""
    rows = await conn.fetch(
        "SELECT id, name, role, colour, avatar FROM app_user "
        "WHERE is_active AND pin_hash IS NOT NULL ORDER BY name"
    )
    return [dict(r) for r in rows]
