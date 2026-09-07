"""Admin routes for the Jellyfin connector and the household's accounts. Spec v2.1 §6.6
(Connectors, Users), §3.1, §3.3, §7.

§6.6's Connectors card: "Jellyfin (URL, API key, library pick, user-mapping table, test
button, sync now, webhook status)". M1 ships all of it but the library pick and the webhook —
§7.2's webhook belongs to the acquisition trigger, which is M5.

§6.6's Users card is "the household's whole user management, and the **only** place accounts
are made (decision 166)". The routes under `/api/admin/users` below are that card's row editor:
create, rename, change role, password reset, PIN reset, the passkey list and its per-credential
revoke, disable and delete. Jellyfin re-link/unlink are the two routes that already existed here.

Every route in this module is `AdminUser`, which means three things at once (§3.1, §3.2,
§6.6): a member gets 403, a signed-out caller gets 401, and an admin whose last password
authentication is older than 24 h is re-prompted before the route resolves.

The API key never comes back out. `has_api_key` is a boolean and the field posts empty to mean
"leave it alone" — §14.3 is blunt that this key is admin-equivalent on the whole media server,
and a GET that returns it turns every admin session into a copy of it.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, status
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

from spielplan.api.deps import DB, AdminUser, write_txn
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient, JellyfinError
from spielplan.connectors.registry import load_jellyfin, save_jellyfin
from spielplan.core import auth, webauthn
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


def _no_control_characters(value: str) -> str:
    """A NUL byte in a name reaches Postgres and answers 500 (sec-04, as-11).

    Unicode's `Cc` category is exactly the C0 and C1 controls, and none of them is typeable in
    a household name — one only ever arrives from a paste or a crafted body, and the place to
    answer for it is the edge, as a 422.
    """
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("must not contain control characters")
    return value


# Trimmed at the edge because the unique index is on `lower(name)` with no trim
# (0002_users.sql:26): ' Tom ' and 'Tom' were two accounts, two identical-looking chips in the
# switch list, and only one of them answered to what a person types (as-11).
AccountName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_control_characters),
]

# Decision 166: two roles and no others. A `Literal` rather than a pattern so `role='guest'`
# is a 422 from the schema, in the one place §6.6 leaves for making an account — the CHECK
# migration 0016 narrows is the same rule one layer down, not a substitute for this one.
AccountRole = Literal["member", "admin"]

# §6.6: "at least one active admin always exists — the last one can be neither demoted nor
# disabled nor deleted". Every route that could take an admin out of that count reads it and
# then writes, and two such routes running at once would each see the other's admin and both
# succeed. The name is the seam, not the row: demoting A while disabling B is the race.
_ROSTER_LOCK = "app_user_admin_floor"


class CreateUser(BaseModel):
    name: AccountName
    role: AccountRole = "member"


class EditUser(BaseModel):
    """§6.6's "rename" and "change role", in one row editor and so in one request."""

    name: AccountName | None = None
    role: AccountRole | None = None


class ActiveRequest(BaseModel):
    is_active: bool


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


# --- §6.6 Users: the row editor -----------------------------------------------------------


async def _target(conn: asyncpg.Connection, user_id: int) -> asyncpg.Record:
    """The account a row-editor route acts on, read inside its own transaction.

    Read there and not before: every floor below is a check followed by a write, and a check
    made outside the transaction that writes is a check against a roster that may have moved.
    """
    row = await conn.fetchrow(
        "SELECT id, name, role, is_active FROM app_user WHERE id = $1", user_id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    return row


async def _refuse_if_last_active_admin(conn: asyncpg.Connection, row, verb: str) -> None:
    """§6.6: "at least one active admin always exists — the last one can be neither demoted nor
    disabled nor deleted" (decision 166).

    A floor rather than a warning because it is a security rule: `POST /api/setup/admin` takes
    no auth dependency at all and is gated only on there being no admin (§3.1), so an install
    that reaches zero admins lets anyone who can see the origin mint one.
    """
    if row["role"] != "admin" or not row["is_active"]:
        return
    others = await conn.fetchval(
        "SELECT count(*) FROM app_user WHERE role = 'admin' AND is_active AND id <> $1",
        row["id"],
    )
    if not others:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the last active admin cannot be {verb} (§6.6: at least one active admin always "
            "exists) — promote another account first",
        )


def _refuse_self(admin: auth.SessionUser, user_id: int, what: str) -> None:
    """§6.6: "an admin cannot reset their own credentials from this tab (§3.2's 24-hour
    re-prompt governs the rest)".

    The tab issues one-time passwords for other people, and §3.1's one-time password is a value
    an admin hands over rather than one they read for themselves; disabling yourself here is the
    same shape, an admin ending their own session from the screen that governs everyone else's.
    """
    if admin.id == user_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"an admin cannot {what} from the Users tab (§6.6)"
        )


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUser, _: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's "create (name + role ∈ {member, admin})", and §3.1's one-time password.

    The one-time password is returned exactly once, here. It is stored only as an argon2 hash
    and no route reads it back, so a lost one is reissued below rather than looked up — which is
    what §3.1's "an admin never sees, sets or types a member's password" costs, and why this
    body carries a name and a role and no password field at all.
    """
    otp = auth.new_one_time_password()
    # Hashed before the transaction opens: argon2 is tens of milliseconds by design (§3.2), and
    # a transaction held open across it is a row lock held across it.
    password_hash = await auth.hash_password_async(otp)
    try:
        async with write_txn(conn):
            user_id = await conn.fetchval(
                """
                INSERT INTO app_user (name, role, password_hash, must_change_password)
                VALUES ($1, $2, $3, true) RETURNING id
                """,
                body.name,
                body.role,
                password_hash,
            )
    except asyncpg.UniqueViolationError as exc:
        # `app_user_name_key` is on lower(name): the index reports the collision a
        # check-then-insert would miss between two admins creating at once (as04).
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    return {
        "id": user_id,
        "name": body.name,
        "role": body.role,
        "one_time_password": otp,
        "note": "shown once — the account is locked to a password change at first login",
    }


@router.patch("/users/{user_id}")
async def edit_user(user_id: int, body: EditUser, _: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's "rename" and "change role", which the roster opens as one row editor."""
    if body.name is None and body.role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "send a name, a role, or both")
    try:
        async with write_txn(conn, lock=_ROSTER_LOCK):
            row = await _target(conn, user_id)
            if body.role is not None and body.role != row["role"]:
                await _refuse_if_last_active_admin(conn, row, "demoted")
            updated = await conn.fetchrow(
                """
                UPDATE app_user SET name = coalesce($2, name), role = coalesce($3, role)
                 WHERE id = $1 RETURNING id, name, role, is_active
                """,
                user_id,
                body.name,
                body.role,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a user named {body.name!r} already exists"
        ) from exc
    return dict(updated)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, admin: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's "password reset (reissues the one-time password and re-arms the first-login
    change — an admin never sees, sets or types a member's password)".

    Every session for the account goes with it. The reset happens because the old credential is
    lost or no longer trusted, and a device already holding a cookie would otherwise keep the
    account for the rest of its sliding 90 days (§3.2) without ever meeting the new one.
    """
    _refuse_self(admin, user_id, "reset their own password")
    otp = auth.new_one_time_password()
    password_hash = await auth.hash_password_async(otp)
    async with write_txn(conn):
        await _target(conn, user_id)
        await conn.execute(
            "UPDATE app_user SET password_hash = $2, must_change_password = true WHERE id = $1",
            user_id,
            password_hash,
        )
        # The account being reset may be locked out by the guesses that made the reset
        # necessary, and that count must not survive onto the credential replacing them (§3.2).
        await auth.clear_password_lockout(conn, user_id)
        revoked = await auth.destroy_user_sessions(conn, user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "one_time_password": otp,
        "sessions_revoked": revoked,
        "note": "shown once — the account is locked to a password change at first login",
    }


@router.post("/users/{user_id}/reset-pin")
async def reset_pin(user_id: int, admin: AdminUser, conn: DB) -> dict[str, object]:
    """§6.6's "PIN reset".

    The counters go with the hash. A member locked out of their own PIN is exactly who needs
    this, and clearing the hash while `pin_locked_until` still stands would hand them an account
    that refuses the PIN they are about to set (§3.2).
    """
    _refuse_self(admin, user_id, "reset their own PIN")
    async with write_txn(conn):
        await _target(conn, user_id)
        await conn.execute(
            "UPDATE app_user SET pin_hash = NULL, pin_failed_count = 0, pin_locked_until = NULL "
            "WHERE id = $1",
            user_id,
        )
    return {"ok": True, "user_id": user_id, "has_pin": False}


@router.get("/users/{user_id}/passkeys")
async def list_passkeys(user_id: int, _: AdminUser, conn: DB) -> list[dict[str, object]]:
    """The *list* half of §6.6's "passkey list with per-credential revoke".

    Without it the revoke below is unreachable from any client: the roster carries a count, and
    a count cannot name the credential a lost phone holds. `webauthn.list_credentials` is the
    same projection the account page reads for itself — label, rp_id, timestamps, sign count,
    and no public key, because a client has nothing to do with one and §14.3's rule about
    admin-equivalent secrets is the same rule about credential material.

    `_target` first, so a roster one delete out of date answers 404 like every route beside it.
    """
    await _target(conn, user_id)
    return await webauthn.list_credentials(conn, user_id)


@router.delete("/users/{user_id}/passkeys/{credential_id:path}")
async def revoke_passkey(
    user_id: int, credential_id: str, _: AdminUser, conn: DB
) -> dict[str, bool]:
    """§6.6's "passkey list with per-credential revoke", on someone else's account.

    Scoped to the named account: `delete_credential` matches on (user_id, credential_id), so an
    id read off one roster row cannot revoke another account's key. §3.2 keeps password login
    always available, so revoking the last passkey strands nobody. `{credential_id:path}`
    mirrors the member-side route — the id is base64url straight off the wire.
    """
    async with write_txn(conn):
        await _target(conn, user_id)
        removed = await webauthn.delete_credential(conn, user_id, credential_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such passkey on that account")
    return {"ok": True}


@router.post("/users/{user_id}/active")
async def set_active(
    user_id: int, body: ActiveRequest, admin: AdminUser, conn: DB
) -> dict[str, object]:
    """§6.6's "disable (`is_active = false`: every session and passkey assertion refused, the
    account and its Ledger kept, the user gone from switch lists, rating partners and Tonight
    lobbies)", and the undo the same control has to have.

    The refusals are already the auth layer's — `load_session`, the login lookup and the passkey
    assertion lookup all require `is_active`. Deleting the sessions is what makes a disable take
    effect on a device holding one right now rather than at its next lapse.
    """
    async with write_txn(conn, lock=_ROSTER_LOCK):
        row = await _target(conn, user_id)
        revoked = 0
        if not body.is_active:
            # The floor first, because it is the wider rule: the only caller who can reach it
            # is an admin disabling themselves (anyone else disabling them is a second active
            # admin, so the floor does not bind), and that person needs to hear that the
            # household would have no admin — not that this tab is for other people.
            await _refuse_if_last_active_admin(conn, row, "disabled")
            _refuse_self(admin, user_id, "disable their own account")
        await conn.execute(
            "UPDATE app_user SET is_active = $2 WHERE id = $1", user_id, body.is_active
        )
        if not body.is_active:
            revoked = await auth.destroy_user_sessions(conn, user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "is_active": body.is_active,
        "sessions_revoked": revoked,
    }


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, _: AdminUser, conn: DB) -> dict[str, bool]:
    """§6.6's "delete" — the destructive half of the pair whose other half, disable, keeps "the
    account and its Ledger".

    Everything referencing the row goes with it, and one of those references is not obvious:
    `session.host_user_id` is ON DELETE CASCADE (0013_tonight.sql:30), so deleting a member
    deletes every Tonight session they hosted, including the other member's answers and ballots.
    §6.6 asks for a delete, so the route deletes; saying what is lost before asking is the
    roster's job.
    """
    async with write_txn(conn, lock=_ROSTER_LOCK):
        row = await _target(conn, user_id)
        await _refuse_if_last_active_admin(conn, row, "deleted")
        await conn.execute("DELETE FROM app_user WHERE id = $1", user_id)
    return {"ok": True}


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
