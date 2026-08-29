"""Spec §2 secrets custody, exercised without a database.

The load-bearing properties: SECRETS_KEY never falls back to SESSION_SECRET, a wrapped DEK
round-trips, ciphertext is unreadable under the wrong key, and rotation re-wraps rather than
re-encrypts every secret.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from spielplan.core import secrets as sec
from spielplan.core.config import Settings


def test_secrets_key_never_falls_back_to_session_secret():
    s = Settings(session_secret="a-perfectly-good-session-secret", secrets_key=None)
    with pytest.raises(RuntimeError) as exc:
        s.require_secrets_key()
    assert "SECRETS_KEY" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)


def test_dek_wrap_round_trip():
    dek = os.urandom(32)
    blob = sec._wrap(dek, "operator-chosen-key")
    assert blob != dek
    assert sec._unwrap(blob, "operator-chosen-key") == dek


def test_dek_unwrap_fails_under_wrong_key():
    blob = sec._wrap(os.urandom(32), "right")
    with pytest.raises(InvalidTag):
        sec._unwrap(blob, "wrong")


def test_seal_round_trip_and_tamper_detection():
    dek = os.urandom(32)
    payload = {"api_key": "jf-unscoped-admin-equivalent", "url": "https://jellyfin.home.lan"}
    blob = sec.seal(dek, payload)
    assert b"jf-unscoped" not in blob          # plaintext must not survive in the ciphertext
    assert sec.open_sealed(dek, blob) == payload

    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        sec.open_sealed(dek, bytes(tampered))


def test_rotation_rewraps_the_dek_without_touching_secrets():
    dek = os.urandom(32)
    wrapped_old = sec._wrap(dek, "old-secrets-key")
    sealed = sec.seal(dek, {"token": "abc"})

    wrapped_new = sec.rewrap_dek(wrapped_old, "old-secrets-key", "new-secrets-key")

    # The DEK is unchanged, so every existing ciphertext still opens — that is the point
    # of wrapping one key rather than encrypting under SECRETS_KEY directly.
    assert sec._unwrap(wrapped_new, "new-secrets-key") == dek
    assert sec.open_sealed(sec._unwrap(wrapped_new, "new-secrets-key"), sealed) == {"token": "abc"}


def test_public_url_trailing_slash_is_stripped_and_rp_id_derived():
    # §14.4: PUBLIC_URL identity is load-bearing for WebAuthn; "…/" and "…" must be one origin.
    s = Settings(public_url="https://spielplan.example.tld/")
    assert s.public_url == "https://spielplan.example.tld"
    assert s.rp_id == "spielplan.example.tld"
