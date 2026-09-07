"""Auth routes. Spec v2.1 §3.1, §3.2.

M0 ships the always-available fallbacks: password login, long-lived per-device session
cookies, the forced first-login password change, and the shared-device PIN switch.
Passkey registration/assertion lands at M1 (§12) and plugs into the same session table.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from spielplan.api.deps import DB, ActiveUser, CredentialedUser, CurrentUser, set_session_cookie, write_txn
from spielplan.core import auth
from spielplan.core.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    # §3.2's password login is the one anonymous route that reaches argon2 and the one that puts
    # caller-supplied text into a query, so the shapes it accepts are part of its defence.
    # 128 is longer than any password a person types and any name the `lower(name)` index has to
    # match; unbounded, an anonymous caller chose how much of §2's 4 vCPU box each attempt cost
    # (sec-02, as-11).
    name: str = Field(max_length=128)
    password: str = Field(max_length=128)
    device_label: str | None = Field(default=None, max_length=256)

    @field_validator("name", "password", "device_label")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        """A NUL byte in a login name reached Postgres and answered 500 (sec-04).

        asyncpg is right to refuse text Postgres cannot store; the place to answer for it is the
        edge, as a 422. Unicode's `Cc` category is exactly the C0 and C1 controls, and none of
        them is typeable in a name, a password or a device label — one only ever arrives from a
        paste or a crafted body.
        """
        if value is not None and any(unicodedata.category(ch) == "Cc" for ch in value):
            raise ValueError("must not contain control characters")
        return value


# §3.2's PIN is four digits, and the two ends of the same PIN disagreed about what that means:
# `SetPinRequest` carried `\d`, which pydantic's engine reads as Unicode-aware, so `١٢٣٤` and
# `１２３４` were accepted and then argon2-hashed — while `PinSwitchRequest` carried no pattern
# at all, so a PIN set as digits could be answered by anything of the right length (as-11).
_PIN_DIGITS = r"^[0-9]+$"


class PinSwitchRequest(BaseModel):
    user_id: int
    pin: str = Field(min_length=4, max_length=12, pattern=_PIN_DIGITS)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class SetPinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=12, pattern=_PIN_DIGITS)
    # Decision 170: the PIN is derived from the account credential, so setting one costs the
    # credential. Required rather than optional — an omitted field must be a 422 here, not a
    # PIN quietly set by whoever is holding an unlocked phone.
    current_password: str


class ReauthRequest(BaseModel):
    password: str = Field(max_length=128)


class PreferencesRequest(BaseModel):
    show_model: bool


# §6: the surface names are normative — Home / Rate / Tonight / Rank / Map / Taste — and each
# one is visible from day one with the milestone that owns it, so the shape of the finished app
# is legible rather than appearing later as a surprise.
SURFACES: tuple[dict[str, str], ...] = (
    {"key": "home", "href": "/", "label": "Home", "milestone": "M0"},
    {"key": "rate", "href": "/rate", "label": "Rate", "milestone": "M2"},
    {"key": "tonight", "href": "/tonight", "label": "Tonight", "milestone": "M4"},
    {"key": "rank", "href": "/rank", "label": "Rank", "milestone": "M3"},
    {"key": "map", "href": "/map", "label": "Map", "milestone": "M6"},
    {"key": "taste", "href": "/taste", "label": "Taste", "milestone": "M6"},
)


def _nav(user: auth.SessionUser) -> dict[str, list[dict[str, str]]]:
    """The navigation payload. §6.6 is admin-role only and §3.1 gives a member 'no admin'.

    Computed here rather than in the client, because "hidden" has to mean the entry does not
    exist in what the member's browser receives. A client-side `{#if role === 'admin'}` hides
    a link from someone reading the screen and shows it to anyone reading the response — and
    the prototype it replaces hardcoded the capability flag to true.
    """
    account = [
        {"key": "account", "href": "/account", "label": "Account & passkeys"},
        {"key": "taste", "href": "/taste", "label": "My Taste"},
    ]
    if user.is_admin:
        account += [
            {"key": "admin", "href": "/admin/data", "label": "Admin view"},
            {"key": "setup", "href": "/setup", "label": "Setup wizard"},
        ]
    return {"surfaces": [dict(s) for s in SURFACES], "account": account}


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
    # Both ends trimmed, because as-11 leaves untrimmed rows behind: creation accepted ' Tom '
    # and the unique index is on `lower(name)`, so the account exists under a name whose spaces
    # nobody can see and nobody retypes. Step 5 stops new ones being made; the rows already
    # there stay reachable only if the comparison ignores the whitespace on both sides. It costs
    # the index on a table that holds one row per person in the household.
    name = body.name.strip()
    row = await conn.fetchrow(
        "SELECT id, name, role, password_hash, must_change_password FROM app_user "
        "WHERE lower(btrim(name)) = lower($1) AND is_active",
        name,
    )
    # The absent name is NOT short-circuited: `check_password` verifies on every path, against
    # a throwaway hash when there is no row, so "no such name" cannot be read off the clock
    # (§3.2). It also carries the lockout — §2 puts this origin on Tailscale, where Cloudflare's
    # rate limit is not in front of anything, so repeated guesses against one name have to cost
    # something here — and every refusal it returns is the one sentence PASSWORD_REFUSAL holds.
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

    # Signing in replaces the session this device was already holding, for the reason the switch
    # below gives: the cookie names one long-lived row per sign-in, and a device that signs in
    # again — as a second person, or after a lost cookie — otherwise leaves the first row live
    # for its full sliding 90 days with nothing pointing at it (dd24). After the credential
    # check, never before: a wrong password must not be able to sign a device out.
    stale = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    # Replacing one session with another is a single duty, so it runs inside `write_txn` —
    # §4.2's house idiom, the same shape `/api/auth/password` below gives its three writes. On
    # autocommit the DELETE committed before the INSERT was attempted: a `create_session` that
    # failed on a statement timeout or a reset connection answered 500 and left the device
    # signed out of the session it arrived holding, which is a state it had before the request
    # and cannot get back. `check_password`'s lockout clear stays outside, because it belongs
    # to proving the credential rather than to this exchange, and because holding a transaction
    # open across argon2's tens of milliseconds is what `setup.py`'s first boot avoids.
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
    """§3.2: 'the account chip switches between member profiles, gated by the per-user PIN
    (the chip reads "member · passkey + PIN")'.

    *Switching*, not signing in: the caller must already hold a session. A 4-digit PIN is a
    convenience for a household device someone is already signed in on, and accepting one from
    an anonymous caller would make it the whole authentication story for every account that set
    one — 10,000 guesses against a route with no other gate.

    `CurrentUser`, not `ActiveUser`, by decision 179: this is one of the four routes reachable
    while §3.1's forced first-login change stands. Leaving a wrong — and possibly locked —
    account on a handed-over phone is exactly when switching away is wanted, so the lock guards
    the product surfaces and not the door out of the account holding it.
    """
    ok, reason = await auth.check_pin(conn, body.user_id, body.pin)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, reason or "wrong PIN")

    row = await conn.fetchrow(
        "SELECT id, name, role FROM app_user WHERE id = $1 AND is_active", body.user_id
    )
    # The device is handed over, so the session it was holding does not travel with it — and
    # the two writes that mean that are one duty, inside `write_txn` for the reason `login`
    # above gives. Worse here than there: a failed INSERT after a committed DELETE leaves the
    # handed-over phone with no session at all, which is precisely the case decision 179 keeps
    # this route reachable for.
    async with write_txn(conn):
        await auth.destroy_session(conn, current.session_id)
        sid = await auth.create_session(conn, row["id"], auth_method="pin")
    set_session_cookie(response, sid)
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
async def me(user: CurrentUser, conn: DB) -> dict[str, object]:
    """Everything the shell needs about the signed-in person, including what they may reach.

    The passkey count and the Jellyfin link travel with it because both drive prompts the
    shell owns: §3.1 prompts passkey registration after the first password change, and §7.3
    needs somewhere to say a link went stale.
    """
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
    body: ChangePasswordRequest, user: CurrentUser, conn: DB
) -> dict[str, object]:
    """The route that ends §3.1's forced first-login change, and one of the four reachable
    while it stands: `/me`, `/password`, `/logout` and `/switch` (decision 179). Every other
    authenticated route in the app is refused until this one has been used.

    A password change revokes every other session for the account. A change made because the
    old password leaked has to actually end the other sessions; otherwise every device that
    already has a cookie stays signed in for the full sliding 90 days.

    Those are three writes, so they run inside `write_txn` — §4.2's house idiom, and the same
    shape `api/admin.py`'s `reset_password` gives the identical three on the admin side. One
    duty must not be transactional on one half and autocommit on the other: uncommitted
    together, a failure after the hash UPDATE changes the password and leaves every other
    device signed in for its full sliding window, which is exactly what this route promises
    above will not happen. argon2 is tens of milliseconds (§3.2), so — as in `setup.py`'s first
    boot — the hash is computed before the transaction opens rather than held inside it.
    """
    # Through `check_password`, never a bare verify: §3.2's lockout counts wherever this
    # credential is checked (core/auth.py). This route is `CurrentUser`, so it is reachable
    # from a PIN-switch session and from an account still locked to §3.1's forced change, and
    # an uncounted verify made either of them an unthrottled oracle on the account password
    # that the front door's five attempts never saw. The wording stays this route's own: the
    # one sentence `login` answers with is an enumeration defence owed to a caller who has
    # proved nothing, and this caller already holds the account's session.
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
        # The credential being guessed at is gone, so the count against it goes too (§3.2). An
        # account locked out by guesses whose owner then changes it from an admin-issued
        # one-time password must not stay locked against the password that replaced it.
        await auth.clear_password_lockout(conn, user.id)
        revoked = await auth.destroy_other_sessions(conn, user.id, keep=user.session_id)
    return {"ok": True, "sessions_revoked": revoked}


@router.post("/reauth")
async def reauth(body: ReauthRequest, user: ActiveUser, conn: DB) -> dict[str, object]:
    """§3.2: "admin routes re-prompt after 24 h" — this is the prompt (sec-05).

    `ActiveUser`: decision 179 names four routes reachable while §3.1's forced first-login
    change stands, and this is not one of them. An admin whose password an operator has just
    reset has no business stamping `admin_verified_at` from the locked session — the way out of
    the lock is `/password`, and the stamp belongs to the session that follows it.

    `admin_verified_at` was written when the session was created and nowhere else, so the only
    way past a stale stamp was to sign in again: a second session row per cycle, the old one
    left live for its full sliding window, and — since §3.2's re-prompt is the point — no
    affordance in the shell that did anything but redirect Home. This clears the clock on the
    session in hand and mints nothing.

    The password, not the passkey, because §3.2 makes it the always-available fallback and it
    is what unblocks an admin whose authenticator is elsewhere. A passkey variant may follow and
    must honour the user-verification rule a passkey sign-in already honours.
    """
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    # Counted, for the reason `change_password` above gives: a route that verifies the account
    # password and writes no failure is a door the §3.2 lockout does not stand behind.
    ok, _ = await auth.check_password(conn, user.id, body.password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong current password")
    if not await auth.stamp_admin_verified(conn, user.session_id):
        # A PIN session. §3.2 makes it a switch convenience on a device someone is already
        # signed in on, so it cannot be the proof the re-prompt asks for however good the
        # password typed into it is: the way back to the admin surface is a password or passkey
        # sign-in, not an upgrade of this session.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "sign in with your password or a passkey to reach the admin surface",
        )
    # `user` was loaded before the stamp, so returning it as it stands would tell the shell the
    # re-prompt is still due on the very request that answered it, and the banner would survive
    # its own dismissal.
    return _me(replace(user, admin_verified_at=datetime.now(UTC)))


@router.post("/pin")
async def set_pin(body: SetPinRequest, user: CredentialedUser, conn: DB) -> dict[str, bool]:
    """§3.2 makes the password the account credential and the PIN a convenience derived from
    it, so setting the PIN costs the password (decision 170).

    Two gates, both of them the dependency's rather than this route's. `CredentialedUser`
    carries §3.1's lock, which is why the hand-written `must_change_password` check that stood
    here is gone: a lock enforced by one route's own `if` is a lock the next five routes forgot
    (spec-05). And a PIN session cannot re-key the switch it arrived through — otherwise
    whoever is holding a handed-over phone sets the PIN that unlocks the account tomorrow.

    The counters clear in the same statement: a PIN is reset precisely because the old one was
    forgotten and guessed at, and leaving `pin_locked_until` standing would lock the account
    out of the PIN that just replaced the one being guessed.
    """
    # Counted, for the reason `change_password` above gives. The password's lockout and the
    # PIN's are the same curve over two credentials (core/auth.py), and this route is where
    # they meet: guessing at the password here bought a PIN, and cost nothing.
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
async def switchable(conn: DB, _: ActiveUser) -> list[dict[str, object]]:
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
