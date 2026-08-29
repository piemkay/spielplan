"""Auth logic that needs no database. Spec v2.1 §3.1, §3.2, §2.

Auth is named M0 scope and every part of it that can be tested without Postgres is here: the
cookie signing that makes SESSION_SECRET load-bearing, the admin re-prompt clock, and the
one-time-password alphabet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from spielplan.core import auth
from spielplan.core.config import Settings


def _settings(secret: str = "session-secret-one") -> Settings:
    return Settings(session_secret=secret, secrets_key="k")


def _user(**kw) -> auth.SessionUser:
    base = {
        "id": 1, "name": "patrick", "role": "admin", "must_change_password": False,
        "session_id": "sid", "auth_method": "password", "admin_verified_at": None,
    }
    return auth.SessionUser(**{**base, **kw})


# --- cookie signing: §2 "Rotating SESSION_SECRET invalidates sessions only" -------------


def test_session_cookie_round_trips_under_the_same_secret():
    with patch("spielplan.core.auth.settings", return_value=_settings()):
        cookie = auth.seal_session_id("abc123")
        assert cookie != "abc123", "the raw id must not travel unsigned"
        assert auth.open_session_cookie(cookie) == "abc123"


def test_rotating_session_secret_invalidates_every_cookie():
    """This is the whole reason the id is signed. Before signing, SESSION_SECRET was unused and
    rotating it did nothing at all — §2 says it must invalidate sessions."""
    with patch("spielplan.core.auth.settings", return_value=_settings("old")):
        cookie = auth.seal_session_id("abc123")
    with patch("spielplan.core.auth.settings", return_value=_settings("new")):
        assert auth.open_session_cookie(cookie) is None


def test_tampered_or_absent_cookie_is_rejected_without_a_database_lookup():
    with patch("spielplan.core.auth.settings", return_value=_settings()):
        assert auth.open_session_cookie(None) is None
        assert auth.open_session_cookie("") is None
        assert auth.open_session_cookie("not-a-signed-value") is None
        # Tamper with the payload, not the last signature character: base64 ignores some
        # trailing bits, so flipping the final char can leave the signature bytes identical.
        cookie = auth.seal_session_id("abc123")
        payload, _, signature = cookie.partition(".")
        flipped = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        assert auth.open_session_cookie(f"{flipped}.{signature}") is None


# --- §3.2: admin routes re-prompt after 24 h -------------------------------------------


def test_admin_reauth_required_when_never_verified():
    assert _user(admin_verified_at=None).admin_reauth_required()


def test_admin_reauth_not_required_inside_the_window():
    recent = datetime.now(UTC) - timedelta(hours=23)
    with patch("spielplan.core.auth.settings", return_value=_settings()):
        assert not _user(admin_verified_at=recent).admin_reauth_required()


def test_admin_reauth_required_after_the_window():
    stale = datetime.now(UTC) - timedelta(hours=25)
    with patch("spielplan.core.auth.settings", return_value=_settings()):
        assert _user(admin_verified_at=stale).admin_reauth_required()


def test_a_non_admin_is_always_refused_admin_routes():
    fresh = datetime.now(UTC)
    with patch("spielplan.core.auth.settings", return_value=_settings()):
        assert _user(role="member", admin_verified_at=fresh).admin_reauth_required()
        assert not _user(role="member").is_admin


# --- §3.1: the one-time password ------------------------------------------------------


def test_one_time_password_avoids_ambiguous_glyphs():
    """It is read off an admin screen and typed into a phone, so 0/O and 1/l must not appear."""
    for _ in range(200):
        otp = auth.new_one_time_password()
        assert len(otp) == 12
        assert not set(otp) & set("01loi")


def test_one_time_passwords_do_not_repeat():
    assert len({auth.new_one_time_password() for _ in range(500)}) == 500


# --- password hashing -----------------------------------------------------------------


def test_password_verify_round_trip_and_rejection():
    stored = auth.hash_password("a-long-enough-password")
    assert auth.verify_password(stored, "a-long-enough-password")
    assert not auth.verify_password(stored, "a-long-enough-passworD")
    assert not auth.verify_password(None, "anything")
    assert not auth.verify_password("not-a-hash", "anything")


def test_hashes_are_salted():
    assert auth.hash_password("same") != auth.hash_password("same")


def test_pin_lockout_constants_are_sane():
    """A 4-digit PIN is 10,000 possibilities; the lockout is the defence, not the hash."""
    assert auth.PIN_ATTEMPT_LIMIT <= 6
    assert timedelta(seconds=30) <= auth.PIN_LOCKOUT_BASE
    assert auth.PIN_LOCKOUT_MAX >= auth.PIN_LOCKOUT_BASE
