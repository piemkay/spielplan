"""Authentication primitives. Spec v2.1 §3.

Passkeys are primary (§3.2) and land at M1; M0 ships the fallbacks the spec always keeps
available: argon2 password login, per-device long-lived session cookies, and the per-user PIN
used for fast switching on a shared/TV device.

§3.1: user creation issues a ONE-TIME PASSWORD and locks the account to a password change at
first login. That lock is `app_user.must_change_password`, and it is the auth layer's job —
not the UI's — to refuse every route but the four that are the way out of it: `/api/auth/me`,
`/api/auth/password`, `/api/auth/logout` and `/api/auth/switch`. The switch is in that set by
decision 179 — a member locked to a forced change on a handed-over phone must still be able to
switch away, so the lock guards the product surfaces and not the door out of the wrong account.
"""

from __future__ import annotations

import asyncio
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


# argon2 at the library defaults (t=3, m=65536 KiB, p=4) costs 30-34 ms of CPU and 64 MiB per
# call, and every one of them ran synchronously inside `async def`: eight concurrent
# wrong-password logins took an unrelated GET /api/health from 38 ms to 223 ms. argon2-cffi
# releases the GIL while hashing, so a worker thread actually buys the parallelism, and §2 puts
# this origin behind Tailscale — which bypasses Cloudflare's rate limiting — on a 4 vCPU box.
# Four at a time is the ceiling because 4 x 64 MiB is the memory a stranger may make the box
# spend, and the bound is process-wide rather than per-request for the same reason.
# Do not answer this by lowering the argon2 parameters: the work factor is the credential's
# defence (§3.2), the CPU is not.
_ARGON2_PARALLELISM = 4
_argon2_slots: asyncio.Semaphore | None = None
_argon2_slots_loop: asyncio.AbstractEventLoop | None = None


def _argon2_gate() -> asyncio.Semaphore:
    """The one bound, re-made when the event loop under it changes.

    An `asyncio.Semaphore` binds itself to the loop of its first *contended* acquire and raises
    on every other one (CPython `Lib/asyncio/mixins.py`). The app has exactly one loop, so this
    re-creates nothing in production; the suite gives every test its own, and the tests that
    matter here — five simultaneous guesses against one account — are precisely the contended
    ones, so a single module-level semaphore would poison every later test that contends.
    """
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


async def hash_pin_async(pin: str) -> str:
    return await hash_password_async(pin)


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

    if await verify_password_async(row["pin_hash"], pin):
        await conn.execute(
            "UPDATE app_user SET pin_failed_count = 0, pin_locked_until = NULL WHERE id = $1",
            user_id,
        )
        return True, None

    # Incremented in the database, not in Python. The read above happens before an argon2
    # verify that takes tens of milliseconds — and now suspends the task while a worker thread
    # does it — so a read-modify-write here would let every request that got its SELECT in
    # first write the same number: ten concurrent guesses would cost one failure. The lockout
    # is the actual defence for a 10^4 keyspace (the hash is not), so it has to count every
    # attempt.
    #
    # The exponent is capped *inside* power() (as-01). Postgres multiplies before the outer
    # least() ever runs, so `interval '1 minute' * power(2, n)` raises
    # DatetimeFieldOverflowError from n=38 — pin_failed_count 42 — the route answers 500, and
    # neither the counter nor pin_locked_until is written: from the 42nd guess on there is no
    # lockout at all, which is exactly where one is needed. 2^6 minutes already saturates the
    # 1 h cap, so capping the exponent changes no reachable lockout, only the overflow.
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


# §3.2 keeps the argon2 password available as the fallback behind the passkey, and it was the
# one credential nothing counted: no per-account limit, no per-IP limit, no concurrency cap.
# The same curve as the PIN's above rather than a second scheme — one escalation, one bound,
# one place to get the overflow right. It buys the same trade the PIN already accepts: someone
# who knows a name can hold that account locked, which is why §3.2 makes the passkey primary
# and why the ceiling stays an hour.
#
# The count belongs to the credential and not to the front door, so `check_password` below is
# the only place the account password is verified. Three routes used to verify the same
# `app_user.password_hash` with a bare `verify_password_async` and count nothing —
# `/api/auth/password`, `/api/auth/reauth` and `/api/auth/pin` — so any session on an account
# was an unthrottled oracle on its password: 60 guesses through the first of them left
# `password_failed_count` at 0 while the front door still answered the correct password. Each
# route keeps its own status code and wording; what none of them keeps is its own verify.
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_LOCKOUT_BASE = timedelta(minutes=1)
PASSWORD_LOCKOUT_MAX = timedelta(hours=1)

# One sentence for every refusal — no such name, wrong password, locked out. A distinct
# "locked" message would say "this account exists and someone is guessing at it" to the person
# doing the guessing, and undo the enumeration defence ABSENT_ACCOUNT_HASH pays for in time.
PASSWORD_REFUSAL = "wrong name or password"

# The id `check_password` addresses when the name matched nothing, so that the absent path
# issues the same statements as the present one instead of skipping them (see there).
_NO_SUCH_ACCOUNT = 0


async def clear_password_lockout(conn: asyncpg.Connection, user_id: int) -> None:
    """Forget the failures. §3.2: proving the password is what clears the count.

    A password *change* clears it too, and through this same function: an account locked out by
    guesses, whose owner then resets it from an admin-issued one-time password, must not stay
    locked against the credential that replaced the one being guessed at.
    """
    await conn.execute(
        "UPDATE app_user SET password_failed_count = 0, password_locked_until = NULL "
        "WHERE id = $1",
        user_id,
    )


async def check_password(
    conn: asyncpg.Connection, user_id: int | None, password: str
) -> tuple[bool, str | None]:
    """Verify a login password under a lockout. Returns (ok, refusal reason). §3.2.

    `user_id` is None for a name that matched no active account, and that branch exists here
    rather than in the route because it is the branch that has to cost the same: argon2 takes
    tens of milliseconds and an index miss takes none, so a caller that short-circuits on "no
    such name" answers it in the timing while the body says nothing (see ABSENT_ACCOUNT_HASH
    above). One verification happens on every path through this function — absent, locked,
    wrong and right alike — and every refusal is the same sentence.

    The same is true of the database, and argon2 was not on its own enough: the present-name
    path used to pay a SELECT and a failure-increment UPDATE the absent path skipped entirely,
    which measured as a systematic ~3.6 ms head start for a name nobody holds and a second one
    for a name that is locked out — the enumeration oracle again, one layer down, and readable
    off a few hundred samples on §2's Tailscale origin where no rate limit stands in front. So
    the statements are unconditional and it is the *row* they address that varies: `probe` is
    the real id, or one no sequence hands out. Both paths issue the same two round trips.

    Equal statements are not equal work, and this docstring used to claim the second where it
    could only show the first. The increment below matches one row for a name that exists and
    is not currently locked out, and no row for an absent name or a locked one — a heap tuple,
    its WAL record and the commit that flushes it, against nothing. Measured over 200
    interleaved pairs on a 4 vCPU box: a name that exists refuses ~1.5 ms slower than one that
    does not, on a ~37 ms refusal. It is small beside argon2 and it is still a signal a few
    hundred samples separate.

    Closing it needs a row the absent path can write to, and `app_user` has nowhere to keep
    one: `role` admits 'admin' and 'member' and nothing else (decision 166), §6.6's roster
    lists disabled accounts as well as active ones, and the wizard's `member_count` counts by
    role — a sentinel row there is a ghost account on a household screen. A sink in another
    table would be a different tuple in a different index, and a shared one would serialise
    every absent-name attempt behind one row lock, which is a fresh oracle pointing the other
    way. So the residual is named — here, in
    `test_a_wrong_name_and_a_wrong_password_cost_the_same`, which measures the rows each path
    writes rather than only counting its calls, and in the coverage row.
    """
    # app_user.id is a bigserial starting at 1, so nothing is ever stored under this id: the
    # SELECT below returns no row and the UPDATE matches none, at the cost of the same primary
    # key probe and the same round trip the present-name path pays.
    probe = user_id if user_id is not None else _NO_SUCH_ACCOUNT
    row = await conn.fetchrow(
        "SELECT password_hash, password_failed_count, password_locked_until FROM app_user "
        "WHERE id = $1 AND is_active",
        probe,
    )
    locked_until = row["password_locked_until"] if row is not None else None
    locked = locked_until is not None and locked_until > datetime.now(UTC)

    # Verified before the lockout is honoured, and the answer thrown away when it stands. A
    # locked account that returned early would answer in a millisecond where every other
    # refusal takes thirty, and that timing is a probe for "which of these names is worth
    # attacking" — the same oracle the unconditional verify closes for absent names.
    stored = row["password_hash"] if row is not None else None
    matched = await verify_password_async(stored or ABSENT_ACCOUNT_HASH, password)

    if matched and row is not None and not locked:
        await clear_password_lockout(conn, probe)
        return True, None

    # In SQL for the reason check_pin's comment gives: the verify above suspends the task, so a
    # read-modify-write would let a burst of guesses cost one failure between them. The
    # exponent is capped inside power() for the reason as-01 gives there — an overflow here
    # raises, the route answers 500, and the failure that triggered it is never written down.
    #
    # It is issued on every refusing path, and which failures it actually counts is decided in
    # the WHERE rather than by returning early above: an absent name addresses a row that does
    # not exist, and an account already locked out is excluded here exactly as the early return
    # excluded it — the lockout is a delay, not a counter that grows while you wait it out.
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
    # §6.7, owner decision 2026-08-29: one global per-user "show the model" preference,
    # default off, toggled from the account dropdown. Debugging, not a product surface.
    show_model: bool = False
    # §3.2's window slides in the row and in the cookie together or not at all. This says the
    # row moved on *this* request, which is how `api.deps.current_user` knows to re-issue the
    # cookie — at most once a day, because that is how often `load_session` moves the row.
    session_slid: bool = False

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
    conn: asyncpg.Connection,
    user_id: int,
    *,
    auth_method: str,
    device_label: str | None = None,
    verified: bool = True,
) -> str:
    """`verified` says the credential proved *who* is holding the device, not only that the
    device is the right one — which is the difference §3.2 draws when it makes a passkey "Face
    ID / Touch ID / Android biometrics". It defaults to True because the two callers that do not
    pass it have nothing to report: a typed password is itself the verification, and a PIN is
    excluded by the CASE below either way. Only a passkey assertion carries a separate flag, and
    a presence-only tap must not open the admin surface without the 24 h re-prompt.
    """
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
    """Answer §3.2's "admin routes re-prompt after 24 h" in place. Returns False for a PIN
    session, which cannot be the proof.

    `admin_verified_at` was written at INSERT above and nowhere else, so the only way past a
    stale stamp was signing in again — which mints a second session and leaves the stale one
    live for its full sliding window. Re-authenticating on the session in hand ends that.

    The `auth_method <> 'pin'` guard is `create_session`'s CASE, restated: a 4-digit PIN is a
    switch convenience on a device someone is already signed in on (§3.2), so it cannot clear a
    re-prompt that a password or a verified passkey is there to answer. The boolean says the
    row was not stamped, so the route refuses rather than reporting a silent no-op as success.
    """
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
    # 90-day *sliding* window (§3.2): touch on use, but at most once a day. Unconditional, this
    # was one serialised row update plus one rewrite of `auth_session_expiry` for every request
    # in a page's fan-out, and the browser saw none of it — the cookie's Max-Age was written
    # once at login, so a daily user's cookie still died 90 days after the last sign-in while
    # the row it named stayed valid. The RETURNING is what tells the caller the row moved, so
    # the Set-Cookie goes out on the same beat.
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


async def destroy_user_sessions(conn: asyncpg.Connection, user_id: int) -> int:
    """Every session an account holds, which is what §6.6's disable and password reset mean.

    `destroy_other_sessions` above keeps the caller's own row because there the caller *is* the
    account holder, changing their own password. Here an admin is acting on someone else, and
    both duties say the same thing: the credential that opened those sessions is gone — lost,
    guessed at, or taken away — so no device may keep what it opened.
    """
    result = await conn.execute("DELETE FROM auth_session WHERE user_id = $1", user_id)
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
