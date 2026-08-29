"""Authentication primitives. Spec v2.1 §3.

Passkeys are primary (§3.2) and land at M1; M0 ships the fallbacks the spec always keeps
available: argon2 password login, per-device long-lived session cookies, and the per-user PIN
used for fast switching on a shared/TV device.

§3.1: user creation issues a ONE-TIME PASSWORD and locks the account to a password change at
first login. That lock is `app_user.must_change_password`, and it is the auth layer's job —
not the UI's — to refuse every route but the change-password one while it is set.
"""

from __future__ import annotations

import hmac
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
    # §2: "Rotating SESSION_SECRET invalidates sessions only and never touches stored secrets."
    # That is only true if the secret is actually load-bearing, so the session id travels
    # signed: a cookie signed under the old secret stops verifying the moment it rotates.
    return URLSafeSerializer(settings().session_secret or "insecure-dev-secret", _COOKIE_SALT)


def seal_session_id(sid: str) -> str:
    return _serializer().dumps(sid)


def open_session_cookie(cookie: str | None) -> str | None:
    """Return the session id inside a cookie, or None if it is missing or not ours."""
    if not cookie:
        return None
    try:
        value = _serializer().loads(cookie)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None

# §3.1: a one-time password the operator reads aloud once. Ambiguous glyphs removed —
# it is transcribed by hand from an admin screen to a phone.
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


# A real argon2 hash of a value nobody has, so the "no such account" branch of a login can pay
# the same tens of milliseconds the "wrong password" branch does. Without it the two answer in
# very different times while saying the same thing, and the route becomes an account-name
# oracle for anyone who can measure a round trip.
ABSENT_ACCOUNT_HASH = _hasher.hash(pysecrets.token_urlsafe(32))


def hash_pin(pin: str) -> str:
    """§3.2: a 4-digit PIN. argon2 anyway — but the search space is 10^4, so the work factor is
    not the defence. `check_pin` below is: a PIN is only ever accepted from an already
    authenticated session, and failures lock the account out with a growing delay."""
    return _hasher.hash(pin)


def verify_pin(stored_hash: str | None, pin: str) -> bool:
    return verify_password(stored_hash, pin)


# A 4-digit PIN has 10,000 possibilities; without a lockout an attacker with a session on the
# shared device walks the space in minutes. Five tries, then a lockout that doubles.
PIN_ATTEMPT_LIMIT = 5
PIN_LOCKOUT_BASE = timedelta(minutes=1)
PIN_LOCKOUT_MAX = timedelta(hours=1)


async def check_pin(conn: asyncpg.Connection, user_id: int, pin: str) -> tuple[bool, str | None]:
    """Verify a switch PIN under a lockout. Returns (ok, refusal reason)."""
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

    if verify_pin(row["pin_hash"], pin):
        await conn.execute(
            "UPDATE app_user SET pin_failed_count = 0, pin_locked_until = NULL WHERE id = $1",
            user_id,
        )
        return True, None

    # Incremented in the database, not in Python. The read above happens before an argon2
    # verify that takes tens of milliseconds, so a read-modify-write here would let every
    # request that got its SELECT in first write the same number: ten concurrent guesses would
    # cost one failure. The lockout is the actual defence for a 10^4 keyspace (the hash is
    # not), so it has to count every attempt.
    updated = await conn.fetchrow(
        """
        UPDATE app_user
           SET pin_failed_count = pin_failed_count + 1,
               pin_locked_until = CASE
                   WHEN pin_failed_count + 1 >= $2
                   THEN now() + least(
                            $3::interval * power(2, pin_failed_count + 1 - $2),
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


@dataclass(frozen=True)
class SessionUser:
    id: int
    name: str
    role: str
    must_change_password: bool
    session_id: str
    auth_method: str
    admin_verified_at: datetime | None
    # §6.7, owner decision 2026-08-29: one global per-user "show the model" preference,
    # default off, toggled from the account dropdown. Debugging, not a product surface.
    show_model: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def admin_reauth_required(self, now: datetime | None = None) -> bool:
        """§3.2: admin routes re-prompt after 24 h."""
        if not self.is_admin:
            return True
        if self.admin_verified_at is None:
            return True
        now = now or datetime.now(UTC)
        return now - self.admin_verified_at > timedelta(hours=settings().admin_reauth_hours)


async def create_session(
    conn: asyncpg.Connection, user_id: int, *, auth_method: str, device_label: str | None = None
) -> str:
    sid = pysecrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=settings().session_days)
    await conn.execute(
        """
        INSERT INTO auth_session (id, user_id, device_label, expires_at, auth_method,
                                  admin_verified_at)
        VALUES ($1, $2, $3, $4, $5, CASE WHEN $5 <> 'pin' THEN now() END)
        """,
        sid,
        user_id,
        device_label,
        expires,
        auth_method,
    )
    return sid


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
    # 90-day *sliding* window (§3.2): touch on use.
    await conn.execute(
        "UPDATE auth_session SET last_seen_at = now(), expires_at = now() + ($2 || ' days')::interval"
        " WHERE id = $1",
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
    )


async def destroy_session(conn: asyncpg.Connection, sid: str) -> None:
    """§3.2: 'Logout clears the session cookie only — passkeys remain registered.'"""
    await conn.execute("DELETE FROM auth_session WHERE id = $1", sid)


async def destroy_other_sessions(conn: asyncpg.Connection, user_id: int, keep: str) -> int:
    """Revoke every other session for a user — what a password change has to mean.

    Without this, changing a password because it leaked leaves every device that already has a
    cookie signed in indefinitely (the window is 90 days, sliding, so in practice forever).
    """
    result = await conn.execute(
        "DELETE FROM auth_session WHERE user_id = $1 AND id <> $2", user_id, keep
    )
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
