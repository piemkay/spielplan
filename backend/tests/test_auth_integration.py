"""Auth against a real database. Spec v2.1 §3.1, §3.2, §2.

`test_auth_logic.py` covers the pure predicates. These are the parts that only exist once rows
do: the forced first-login change, the one-time password's lifecycle, the sliding session, the
admin re-prompt clock, and the PIN lockout.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from spielplan.core import auth


async def _member(db, name="jenny", *, otp=None) -> tuple[int, str]:
    """Create an account the way §3.1 does: a one-time password, locked to a change."""
    otp = otp or auth.new_one_time_password()
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role, password_hash, must_change_password) "
        "VALUES ($1, 'member', $2, true) RETURNING id",
        name,
        auth.hash_password(otp),
    )
    return user_id, otp


# --- §3.1: the one-time password ------------------------------------------------------


async def test_the_otp_is_stored_only_as_a_hash(db):
    """It is shown once and never again. A plaintext column would make "shown once" a lie."""
    user_id, otp = await _member(db)
    stored = await db.fetchval("SELECT password_hash FROM app_user WHERE id = $1", user_id)
    assert otp not in stored
    assert stored.startswith("$argon2")
    assert auth.verify_password(stored, otp)


async def test_a_new_account_is_locked_to_a_password_change(db):
    user_id, _ = await _member(db)
    assert await db.fetchval("SELECT must_change_password FROM app_user WHERE id = $1", user_id)


async def test_changing_the_password_clears_the_lock_and_invalidates_the_otp(db):
    user_id, otp = await _member(db)
    await db.execute(
        "UPDATE app_user SET password_hash = $2, must_change_password = false WHERE id = $1",
        user_id,
        auth.hash_password("a-real-password-now"),
    )
    row = await db.fetchrow(
        "SELECT password_hash, must_change_password FROM app_user WHERE id = $1", user_id
    )
    assert row["must_change_password"] is False
    assert not auth.verify_password(row["password_hash"], otp), "the OTP must stop working"
    assert auth.verify_password(row["password_hash"], "a-real-password-now")


async def test_names_are_unique_case_insensitively(db):
    """The index is on lower(name): "Jenny" and "jenny" are the same person on a shared TV."""
    await _member(db, "Jenny")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _member(db, "jenny")


# --- §3.2: sessions -------------------------------------------------------------------


async def test_a_session_round_trips_and_carries_the_preference(db):
    user_id, _ = await _member(db)
    sid = await auth.create_session(db, user_id, auth_method="password", device_label="phone")

    user = await auth.load_session(db, sid)
    assert user is not None
    assert user.id == user_id
    assert user.must_change_password is True
    assert user.show_model is False        # decision 117: default off


async def test_loading_a_session_slides_its_expiry(db):
    """§3.2: "90-day sliding". A fixed window would sign the household out mid-year."""
    user_id, _ = await _member(db)
    sid = await auth.create_session(db, user_id, auth_method="password")
    await db.execute(
        "UPDATE auth_session SET expires_at = now() + interval '10 days' WHERE id = $1", sid
    )

    assert await auth.load_session(db, sid) is not None
    expires = await db.fetchval("SELECT expires_at FROM auth_session WHERE id = $1", sid)
    assert expires > datetime.now(UTC) + timedelta(days=80)


async def test_an_expired_session_does_not_load(db):
    user_id, _ = await _member(db)
    sid = await auth.create_session(db, user_id, auth_method="password")
    await db.execute(
        "UPDATE auth_session SET expires_at = now() - interval '1 minute' WHERE id = $1", sid
    )
    assert await auth.load_session(db, sid) is None


async def test_a_deactivated_account_cannot_use_its_session(db):
    user_id, _ = await _member(db)
    sid = await auth.create_session(db, user_id, auth_method="password")
    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", user_id)
    assert await auth.load_session(db, sid) is None


async def test_logout_removes_one_session_and_leaves_the_others(db):
    """§3.2: "Logout clears the session cookie only" — the phone stays signed in."""
    user_id, _ = await _member(db)
    phone = await auth.create_session(db, user_id, auth_method="password", device_label="phone")
    desk = await auth.create_session(db, user_id, auth_method="password", device_label="desk")

    await auth.destroy_session(db, desk)
    assert await auth.load_session(db, desk) is None
    assert await auth.load_session(db, phone) is not None


async def test_a_password_change_revokes_every_other_session(db):
    """A change made because the old password leaked has to actually end the other sessions."""
    user_id, _ = await _member(db)
    keep = await auth.create_session(db, user_id, auth_method="password")
    for _ in range(3):
        await auth.create_session(db, user_id, auth_method="password")

    revoked = await auth.destroy_other_sessions(db, user_id, keep=keep)
    assert revoked == 3
    assert await auth.load_session(db, keep) is not None
    assert await db.fetchval("SELECT count(*) FROM auth_session WHERE user_id = $1", user_id) == 1


# --- §3.2: the admin re-prompt --------------------------------------------------------


async def test_a_password_login_stamps_the_admin_clock_and_a_pin_switch_does_not(db):
    """§3.2: admin routes re-prompt after 24 h. A PIN is a convenience on a shared device and
    must not silently satisfy that re-prompt."""
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('admin', 'admin') RETURNING id"
    )
    by_password = await auth.create_session(db, user_id, auth_method="password")
    by_pin = await auth.create_session(db, user_id, auth_method="pin")

    assert (await auth.load_session(db, by_password)).admin_verified_at is not None
    assert (await auth.load_session(db, by_pin)).admin_verified_at is None
    assert (await auth.load_session(db, by_pin)).admin_reauth_required()


async def test_the_admin_reprompt_fires_after_the_window(db):
    user_id = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('admin', 'admin') RETURNING id"
    )
    sid = await auth.create_session(db, user_id, auth_method="password")
    await db.execute(
        "UPDATE auth_session SET admin_verified_at = now() - interval '25 hours' WHERE id = $1", sid
    )
    assert (await auth.load_session(db, sid)).admin_reauth_required()


# --- §3.2: the switch PIN -------------------------------------------------------------


async def test_a_correct_pin_passes_and_clears_the_failure_count(db):
    user_id, _ = await _member(db)
    await db.execute(
        "UPDATE app_user SET pin_hash = $2, pin_failed_count = 3 WHERE id = $1",
        user_id,
        auth.hash_pin("1234"),
    )
    ok, reason = await auth.check_pin(db, user_id, "1234")
    assert ok and reason is None
    assert await db.fetchval("SELECT pin_failed_count FROM app_user WHERE id = $1", user_id) == 0


async def test_repeated_wrong_pins_lock_the_account_out(db):
    """10,000 possibilities: the lockout is the defence, not the hash."""
    user_id, _ = await _member(db)
    await db.execute(
        "UPDATE app_user SET pin_hash = $2 WHERE id = $1", user_id, auth.hash_pin("1234")
    )

    for _ in range(auth.PIN_ATTEMPT_LIMIT):
        ok, _reason = await auth.check_pin(db, user_id, "9999")
        assert not ok

    locked_until = await db.fetchval(
        "SELECT pin_locked_until FROM app_user WHERE id = $1", user_id
    )
    assert locked_until is not None and locked_until > datetime.now(UTC)

    # …and the correct PIN is refused while the lockout stands, or the lockout is theatre.
    ok, reason = await auth.check_pin(db, user_id, "1234")
    assert not ok
    assert "too many attempts" in reason


async def test_an_account_without_a_pin_cannot_be_switched_to(db):
    user_id, _ = await _member(db)
    ok, reason = await auth.check_pin(db, user_id, "1234")
    assert not ok
    assert "no switch PIN" in reason
