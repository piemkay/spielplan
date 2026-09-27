from __future__ import annotations

import asyncio
import secrets as pysecrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer

from spielplan.core.config import settings

_hasher = PasswordHasher()

SESSION_COOKIE = "spielplan_session"
_COOKIE_SALT = "spielplan/session/v1"


def _serializer() -> URLSafeSerializer:
    # Signed, so rotating SESSION_SECRET invalidates every session (§2).
    return URLSafeSerializer(settings().session_secret, _COOKIE_SALT)


def seal_session_id(sid: str) -> str:
    return _serializer().dumps(sid)


def open_session_cookie(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        value = _serializer().loads(cookie)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None

# No ambiguous glyphs: it is read off an admin screen and typed by hand (§3.1).
_OTP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_one_time_password(length: int = 12) -> str:
    return "".join(pysecrets.choice(_OTP_ALPHABET) for _ in range(length))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    if not stored_hash:
        return False
    try:
        _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# argon2 costs ~30 ms CPU and 64 MiB per call: run it in threads, at most 4 at once to bound memory.
# Do not lower the argon2 parameters instead: the work factor is the credential's defence (§3.2).
_ARGON2_PARALLELISM = 4
_argon2_slots: asyncio.Semaphore | None = None
_argon2_slots_loop: asyncio.AbstractEventLoop | None = None


def _argon2_gate() -> asyncio.Semaphore:
    """Re-made per event loop: a Semaphore binds to the loop of its first contended acquire."""
    global _argon2_slots, _argon2_slots_loop
    loop = asyncio.get_running_loop()
    if _argon2_slots is None or _argon2_slots_loop is not loop:
        _argon2_slots = asyncio.Semaphore(_ARGON2_PARALLELISM)
        _argon2_slots_loop = loop
    return _argon2_slots


async def verify_password_async(stored_hash: str | None, password: str) -> bool:
    async with _argon2_gate():
        return await asyncio.to_thread(verify_password, stored_hash, password)


async def hash_password_async(password: str) -> str:
    async with _argon2_gate():
        return await asyncio.to_thread(hash_password, password)


# Lets the no-such-account branch pay the same argon2 time, so login is no account-name oracle.
ABSENT_ACCOUNT_HASH = _hasher.hash(pysecrets.token_urlsafe(32))


def hash_pin(pin: str) -> str:
    """The work factor is no defence for a 10^4 space; `check_pin`'s lockout is."""
    return _hasher.hash(pin)


async def hash_pin_async(pin: str) -> str:
    return await hash_password_async(pin)


PIN_ATTEMPT_LIMIT = 5
PIN_LOCKOUT_BASE = timedelta(minutes=1)
PIN_LOCKOUT_MAX = timedelta(hours=1)


async def check_pin(conn: asyncpg.Connection, user_id: int, pin: str) -> tuple[bool, str | None]:
    row = await conn.fetchrow(
        "SELECT pin_hash, pin_failed_count, pin_locked_until FROM app_user "
        "WHERE id = $1 AND is_active",
        user_id,
    )
    if row is None or not row["pin_hash"]:
        return False, "that profile has no switch PIN"

    locked_until = row["pin_locked_until"]
    if locked_until and locked_until > datetime.now(UTC):
        return False, "too many attempts — try again later"

    if await verify_password_async(row["pin_hash"], pin):
        await conn.execute(
            "UPDATE app_user SET pin_failed_count = 0, pin_locked_until = NULL WHERE id = $1",
            user_id,
        )
        return True, None

    # Incremented in SQL: across the argon2 await, a read-modify-write would lose concurrent failures.
    # The exponent is capped inside power(): Postgres overflows the interval from n=38 and the lockout dies.
    updated = await conn.fetchrow(
        """
        UPDATE app_user
           SET pin_failed_count = pin_failed_count + 1,
               pin_locked_until = CASE
                   WHEN pin_failed_count + 1 >= $2
                   THEN now() + least(
                            $3::interval * power(2, least(pin_failed_count + 1 - $2, 6)),
                            $4::interval)
                   ELSE pin_locked_until
               END
         WHERE id = $1
        RETURNING pin_failed_count, pin_locked_until
        """,
        user_id, PIN_ATTEMPT_LIMIT, PIN_LOCKOUT_BASE, PIN_LOCKOUT_MAX,
    )
    locked = updated is not None and updated["pin_failed_count"] >= PIN_ATTEMPT_LIMIT
    return False, "too many attempts — try again later" if locked else "wrong PIN"


# Same curve as the PIN. `check_password` is the only verify of the account password, so every guess counts.
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_LOCKOUT_BASE = timedelta(minutes=1)
PASSWORD_LOCKOUT_MAX = timedelta(hours=1)

# One sentence for every refusal: a distinct "locked" message would reveal that the account exists.
PASSWORD_REFUSAL = "wrong name or password"

_NO_SUCH_ACCOUNT = 0


async def clear_password_lockout(conn: asyncpg.Connection, user_id: int) -> None:
    """A password change clears it too, so a reset account is not locked against its new password."""
    await conn.execute(
        "UPDATE app_user SET password_failed_count = 0, password_locked_until = NULL "
        "WHERE id = $1",
        user_id,
    )


async def check_password(
    conn: asyncpg.Connection, user_id: int | None, password: str
) -> tuple[bool, str | None]:
    """Every path (absent, locked, wrong, right) pays one argon2 verify and the same two statements, so
    timing does not reveal a name. Residual: the UPDATE writes a row only for a present, unlocked name."""
    # bigserial starts at 1, so this id matches nothing yet costs the same round trips.
    probe = user_id if user_id is not None else _NO_SUCH_ACCOUNT
    row = await conn.fetchrow(
        "SELECT password_hash, password_failed_count, password_locked_until FROM app_user "
        "WHERE id = $1 AND is_active",
        probe,
    )
    locked_until = row["password_locked_until"] if row is not None else None
    locked = locked_until is not None and locked_until > datetime.now(UTC)

    # Verified even when locked, so a locked account does not refuse faster than the rest.
    stored = row["password_hash"] if row is not None else None
    matched = await verify_password_async(stored or ABSENT_ACCOUNT_HASH, password)

    if matched and row is not None and not locked:
        await clear_password_lockout(conn, probe)
        return True, None

    # In SQL and capped for `check_pin`'s reasons. The WHERE, not an early return, skips absent and
    # locked rows, so every refusing path issues the same statement.
    await conn.execute(
        """
        UPDATE app_user
           SET password_failed_count = password_failed_count + 1,
               password_locked_until = CASE
                   WHEN password_failed_count + 1 >= $2
                   THEN now() + least(
                            $3::interval * power(2, least(password_failed_count + 1 - $2, 6)),
                            $4::interval)
                   ELSE password_locked_until
               END
         WHERE id = $1 AND is_active
           AND (password_locked_until IS NULL OR password_locked_until <= now())
        """,
        probe, PASSWORD_ATTEMPT_LIMIT, PASSWORD_LOCKOUT_BASE, PASSWORD_LOCKOUT_MAX,
    )
    return False, PASSWORD_REFUSAL


@dataclass(frozen=True)
class SessionUser:
    id: int
    name: str
    role: str
    must_change_password: bool
    session_id: str
    auth_method: str
    admin_verified_at: datetime | None
    show_model: bool = False
    # The row slid on this request, so `api.deps.current_user` re-issues the cookie with it (§3.2).
    session_slid: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def admin_reauth_required(self, now: datetime | None = None) -> bool:
        if not self.is_admin:
            return True
        if self.admin_verified_at is None:
            return True
        now = now or datetime.now(UTC)
        return now - self.admin_verified_at > timedelta(hours=settings().admin_reauth_hours)


async def create_session(
    conn: asyncpg.Connection,
    user_id: int,
    *,
    auth_method: str,
    device_label: str | None = None,
    verified: bool = True,
) -> str:
    """`verified`: the credential proved who holds the device. Only a passkey assertion can say False,
    and a presence-only tap must not skip the 24 h admin re-prompt."""
    sid = pysecrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=settings().session_days)
    await conn.execute(
        """
        INSERT INTO auth_session (id, user_id, device_label, expires_at, auth_method,
                                  admin_verified_at)
        VALUES ($1, $2, $3, $4, $5, CASE WHEN $5 <> 'pin' AND $6 THEN now() END)
        """,
        sid,
        user_id,
        device_label,
        expires,
        auth_method,
        verified,
    )
    return sid


async def stamp_admin_verified(conn: asyncpg.Connection, sid: str) -> bool:
    """Re-stamp the session in hand. False for a PIN session, which cannot be the proof (§3.2)."""
    stamped = await conn.fetchval(
        "UPDATE auth_session SET admin_verified_at = now() "
        "WHERE id = $1 AND auth_method <> 'pin' RETURNING 1",
        sid,
    )
    return stamped is not None


async def load_session(conn: asyncpg.Connection, sid: str) -> SessionUser | None:
    row = await conn.fetchrow(
        """
        SELECT s.id AS session_id, s.auth_method, s.admin_verified_at,
               u.id, u.name, u.role, u.must_change_password, u.show_model
          FROM auth_session s JOIN app_user u ON u.id = s.user_id
         WHERE s.id = $1 AND s.expires_at > now() AND u.is_active
        """,
        sid,
    )
    if row is None:
        return None
    # Slides at most once a day; RETURNING tells the caller to re-issue the cookie on the same beat.
    slid = await conn.fetchval(
        "UPDATE auth_session SET last_seen_at = now(), expires_at = now() + ($2 || ' days')::interval"
        " WHERE id = $1 AND last_seen_at < now() - interval '1 day' RETURNING 1",
        sid,
        str(settings().session_days),
    )
    return SessionUser(
        id=row["id"],
        name=row["name"],
        role=row["role"],
        must_change_password=row["must_change_password"],
        session_id=row["session_id"],
        auth_method=row["auth_method"],
        admin_verified_at=row["admin_verified_at"],
        show_model=row["show_model"],
        session_slid=slid is not None,
    )


async def destroy_session(conn: asyncpg.Connection, sid: str) -> None:
    await conn.execute("DELETE FROM auth_session WHERE id = $1", sid)


async def destroy_other_sessions(conn: asyncpg.Connection, user_id: int, keep: str) -> int:
    result = await conn.execute(
        "DELETE FROM auth_session WHERE user_id = $1 AND id <> $2", user_id, keep
    )
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0


async def destroy_user_sessions(conn: asyncpg.Connection, user_id: int) -> int:
    result = await conn.execute("DELETE FROM auth_session WHERE user_id = $1", user_id)
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0
