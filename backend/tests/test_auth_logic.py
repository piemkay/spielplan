"""Auth logic that needs no database. Spec v2.1 §3.1, §3.2, §2.

Auth is named M0 scope and every part of it that can be tested without Postgres is here: the
cookie signing that makes SESSION_SECRET load-bearing, the admin re-prompt clock, and the
one-time-password alphabet.
"""

from __future__ import annotations

import inspect
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from spielplan.core import auth
from spielplan.core.config import Settings

# Long enough to satisfy M4.7's 32-character floor on SESSION_SECRET and SECRETS_KEY
# (sec-03): these bound tests are about SESSION_DAYS and ADMIN_REAUTH_HOURS, so the rest of
# the configuration has to be valid or the refusal under test is not the one that fires.
_SECRET = "a-perfectly-good-secret-not-a-real-one"


def _settings(secret: str = "session-secret-one-not-a-real-one") -> Settings:
    # M4.7 sec-03 gave SESSION_SECRET and SECRETS_KEY a 32-character floor, so every secret in
    # this file is long enough to construct; the rotation test below still only cares that two
    # different values do not verify each other's cookies.
    return Settings(session_secret=secret, secrets_key="secrets-key-one-not-a-real-one-either")


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
    with patch("spielplan.core.auth.settings", return_value=_settings("session-secret-old-not-a-real-one")):
        cookie = auth.seal_session_id("abc123")
    with patch("spielplan.core.auth.settings", return_value=_settings("session-secret-new-not-a-real-one")):
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
    """A static guard, because the property is not observable from the outputs.

    500 draws with no collision is what `random.choice` over a 31-glyph alphabet also produces,
    and what a generator seeded from the clock produces, and what one seeded from the user id
    produces. The distinctness this test used to assert was true of every implementation it was
    meant to rule out, so it could not fail — and an unfalsifiable test in the map reads as
    coverage of §3.1's one-time password.

    What actually matters is the source of the randomness: the OTP is the only credential the
    account has until its first login (§3.1), so it is drawn from the CSPRNG. That is a fact
    about the code, so it is read off the code.
    """
    source = inspect.getsource(auth.new_one_time_password)
    alias = next(name for name, value in vars(auth).items() if value is secrets)
    assert f"{alias}.choice(" in source, (
        "new_one_time_password must draw from the `secrets` CSPRNG; "
        f"its source does not call {alias}.choice"
    )
    assert "random." not in source, "the `random` module is seeded and predictable (§3.1)"


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


# --- §2 / §3.2: neither session number has an "off" -------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_days", 0),
        ("session_days", -1),
        ("admin_reauth_hours", 0),
        ("admin_reauth_hours", -5),
    ],
)
def test_a_session_window_of_zero_is_refused_at_configuration(field, value):
    """§3.2 fixes both numbers — 90-day sliding sessions, a 24 h admin re-prompt — so 0 is not
    "disabled", it is a household that cannot stay signed in. At `SESSION_DAYS=0` every login
    succeeds and the next request is 401 for everyone; at `ADMIN_REAUTH_HOURS=0` every admin
    route re-prompts milliseconds after a fresh sign-in. Neither is logged anywhere, so the
    operator would be reading it off the symptom. The bound refuses it at boot instead."""
    with pytest.raises(ValidationError) as raised:
        Settings(session_secret=_SECRET, secrets_key=_SECRET, **{field: value})
    assert field in str(raised.value), "the refusal must name the variable to change"


def test_the_session_defaults_are_the_numbers_the_spec_fixes():
    """The bound must not have moved the defaults §3.2 states."""
    cfg = Settings(session_secret=_SECRET, secrets_key=_SECRET)
    assert (cfg.session_days, cfg.admin_reauth_hours) == (90, 24)
