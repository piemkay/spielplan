"""Passkeys, with a real authenticator. Spec v2.1 §3.2, §3.3, §14.4, §4.2 webauthn_credential.

Every ceremony here is genuine: `tests/fixtures/soft_authenticator.py` signs real CTAP2
structures with a real P-256 key, so `core.webauthn` runs its actual verification. That is what
makes the negative cases meaningful — an assertion for the wrong origin, one for the wrong
rp_id, and a replay whose signature verifies perfectly and whose counter has not moved.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import pytest

from spielplan.core import auth, webauthn
from spielplan.core.config import settings
from tests.fixtures.soft_authenticator import SoftAuthenticator, b64


@pytest.fixture
def rp():
    cfg = settings()
    return cfg.rp_id, cfg.public_url


@pytest.fixture
def device(rp):
    rp_id, origin = rp
    return SoftAuthenticator(rp_id=rp_id, origin=origin)


async def _user(db, name="jenny", role="member") -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


async def _register(db, user_id: int, device: SoftAuthenticator, *, label="phone", **kwargs):
    ceremony = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    return await webauthn.register(
        db,
        user_id=user_id,
        handle=ceremony.id,
        credential=device.register(challenge, **kwargs),
        label=label,
    )


async def _authenticate(db, device: SoftAuthenticator, *, name=None, **kwargs) -> int:
    ceremony = await webauthn.authentication_options(db, name=name)
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    return await webauthn.authenticate(
        db, handle=ceremony.id, credential=device.authenticate(challenge, **kwargs)
    )


# --- §3.2: registration ------------------------------------------------------------------


async def test_a_passkey_registers_and_then_signs_in(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)
    assert await _authenticate(db, device) == user_id


async def test_the_credential_is_bound_to_the_public_url_origin(db, device, rp):
    """§14 risk 4: WebAuthn origin coupling is why PUBLIC_URL is required config."""
    rp_id, _origin = rp
    user_id = await _user(db)
    await _register(db, user_id, device)

    stored = await db.fetchrow(
        "SELECT rp_id, sign_count FROM webauthn_credential WHERE user_id = $1", user_id
    )
    assert stored["rp_id"] == rp_id


async def test_a_user_may_hold_several_passkeys(db, rp):
    """§3.2: "multiple passkeys per user (phone + desktop)"."""
    rp_id, origin = rp
    user_id = await _user(db)
    phone = SoftAuthenticator(rp_id=rp_id, origin=origin)
    desktop = SoftAuthenticator(rp_id=rp_id, origin=origin)
    await _register(db, user_id, phone, label="phone")
    await _register(db, user_id, desktop, label="desktop")

    listed = await webauthn.list_credentials(db, user_id)
    assert {c["label"] for c in listed} == {"phone", "desktop"}
    assert await _authenticate(db, phone) == user_id
    assert await _authenticate(db, desktop) == user_id


async def test_an_existing_passkey_is_excluded_from_a_new_registration(db, device):
    """`excludeCredentials` is what stops the same authenticator producing a second row that
    shadows the first."""
    user_id = await _user(db)
    await _register(db, user_id, device)
    ceremony = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    excluded = {c["id"] for c in ceremony.options["excludeCredentials"]}
    assert b64(device.credential_id) in excluded


# --- §3.2 / §14.4: origin and rp_id ------------------------------------------------------


async def test_registration_from_a_different_origin_is_refused(db, device):
    user_id = await _user(db)
    with pytest.raises(webauthn.PasskeyError):
        await _register(db, user_id, device, origin="https://evil.example")
    assert await db.fetchval("SELECT count(*) FROM webauthn_credential") == 0


async def test_an_assertion_for_a_different_rp_id_is_refused(db, device, rp):
    """The rp_id is hashed into authenticatorData, so a credential answering for another
    relying party fails verification rather than being trusted."""
    user_id = await _user(db)
    await _register(db, user_id, device)
    with pytest.raises(webauthn.PasskeyError):
        await _authenticate(db, device, rp_id="evil.example")
    assert await _authenticate(db, device) == user_id


async def test_an_assertion_from_a_different_origin_is_refused(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)
    with pytest.raises(webauthn.PasskeyError):
        await _authenticate(db, device, origin="https://evil.example")


async def test_a_credential_registered_for_another_address_stops_working(db, device):
    """§14.4: "changing PUBLIC_URL later invalidates registered passkeys." The row survives —
    and is listed as unusable — rather than being verified against an origin it never had."""
    user_id = await _user(db)
    await _register(db, user_id, device)
    await db.execute("UPDATE webauthn_credential SET rp_id = 'old.example' WHERE user_id = $1",
                     user_id)

    with pytest.raises(webauthn.PasskeyError, match="different address"):
        await _authenticate(db, device)

    listed = await webauthn.list_credentials(db, user_id)
    assert listed[0]["usable"] is False


# --- §4.2: sign_count is a replay guard --------------------------------------------------


async def test_a_replayed_assertion_is_refused(db, device):
    """The signature verifies perfectly; only the counter says it is a replay. This is the one
    reason §4.2 stores `sign_count`."""
    user_id = await _user(db)
    await _register(db, user_id, device)
    assert await _authenticate(db, device) == user_id

    with pytest.raises(webauthn.PasskeyError):
        await _authenticate(db, device, advance=False)


async def test_the_stored_counter_advances_with_each_use(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)
    await _authenticate(db, device)
    first = await db.fetchval(
        "SELECT sign_count FROM webauthn_credential WHERE user_id = $1", user_id
    )
    await _authenticate(db, device)
    second = await db.fetchrow(
        "SELECT sign_count, last_used_at FROM webauthn_credential WHERE user_id = $1", user_id
    )
    assert second["sign_count"] > first
    assert second["last_used_at"] is not None


# --- the challenge -----------------------------------------------------------------------


async def test_a_challenge_is_single_use(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)

    ceremony = await webauthn.authentication_options(db, name=None)
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    credential = device.authenticate(challenge)
    assert await webauthn.authenticate(db, handle=ceremony.id, credential=credential) == user_id

    # The very same, valid, unexpired assertion — refused, because the challenge is gone.
    with pytest.raises(webauthn.PasskeyError, match="expired"):
        await webauthn.authenticate(db, handle=ceremony.id, credential=credential)


async def test_an_expired_challenge_is_refused(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)
    ceremony = await webauthn.authentication_options(db, name=None)
    await db.execute(
        "UPDATE webauthn_challenge SET expires_at = now() - interval '1 minute' WHERE id = $1",
        ceremony.id,
    )
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    with pytest.raises(webauthn.PasskeyError, match="expired"):
        await webauthn.authenticate(
            db, handle=ceremony.id, credential=device.authenticate(challenge)
        )


async def test_a_registration_challenge_cannot_be_spent_on_a_sign_in(db, device):
    user_id = await _user(db)
    ceremony = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    with pytest.raises(webauthn.PasskeyError):
        await webauthn.authenticate(
            db, handle=ceremony.id, credential=device.authenticate(challenge)
        )


async def test_one_users_registration_challenge_cannot_register_for_another(db, rp):
    rp_id, origin = rp
    victim = await _user(db, "jenny")
    attacker = await _user(db, "mallory")
    ceremony = await webauthn.registration_options(db, user_id=victim, user_name="jenny")
    challenge = webauthn.base64url_to_bytes(ceremony.options["challenge"])
    device = SoftAuthenticator(rp_id=rp_id, origin=origin)
    with pytest.raises(webauthn.PasskeyError, match="different account"):
        await webauthn.register(
            db, user_id=attacker, handle=ceremony.id,
            credential=device.register(challenge), label="x",
        )


async def test_pruning_removes_only_expired_challenges(db):
    user_id = await _user(db)
    stale = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    fresh = await webauthn.registration_options(db, user_id=user_id, user_name="jenny")
    await db.execute(
        "UPDATE webauthn_challenge SET expires_at = now() - interval '1 hour' WHERE id = $1",
        stale.id,
    )
    assert await webauthn.prune_challenges(db) == 1
    remaining = await db.fetch("SELECT id FROM webauthn_challenge")
    assert [r["id"] for r in remaining] == [fresh.id]


# --- §3.2: the sign-in surface must not leak the roster ----------------------------------


async def test_an_unknown_name_yields_an_ordinary_ceremony(db):
    """The login screen must not become an oracle for which household members exist."""
    ceremony = await webauthn.authentication_options(db, name="nobody-here")
    assert ceremony.options["allowCredentials"] == []




# --- §3.2: logout, and the account it belongs to -----------------------------------------


async def test_logout_leaves_the_passkey_registered(db, device):
    """§3.2: "Logout clears the session cookie only — passkeys remain registered."""
    user_id = await _user(db)
    await _register(db, user_id, device)
    sid = await auth.create_session(db, user_id, auth_method="passkey")

    await auth.destroy_session(db, sid)
    assert await auth.load_session(db, sid) is None
    assert await _authenticate(db, device) == user_id


async def test_a_deactivated_account_cannot_sign_in_with_its_passkey(db, device):
    user_id = await _user(db)
    await _register(db, user_id, device)
    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", user_id)
    with pytest.raises(webauthn.PasskeyError, match="not registered here"):
        await _authenticate(db, device)


async def test_removing_a_passkey_removes_only_that_one(db, rp):
    rp_id, origin = rp
    user_id = await _user(db)
    phone = SoftAuthenticator(rp_id=rp_id, origin=origin)
    desktop = SoftAuthenticator(rp_id=rp_id, origin=origin)
    await _register(db, user_id, phone, label="phone")
    await _register(db, user_id, desktop, label="desktop")

    assert await webauthn.delete_credential(db, user_id, b64(phone.credential_id)) is True
    assert [c["label"] for c in await webauthn.list_credentials(db, user_id)] == ["desktop"]
    assert await _authenticate(db, desktop) == user_id


async def test_one_user_cannot_delete_anothers_passkey(db, device):
    victim = await _user(db, "jenny")
    other = await _user(db, "patrick")
    await _register(db, victim, device)
    assert await webauthn.delete_credential(db, other, b64(device.credential_id)) is False
    assert len(await webauthn.list_credentials(db, victim)) == 1


async def test_a_credential_id_cannot_be_claimed_by_another_account(db, rp):
    """The registration response is composed by the client and attestation format "none" means
    nothing vouches for it, so the credential id in it is attacker-chosen.

    An upsert here would overwrite the victim's stored public key while the row kept its
    original `user_id` — and the attacker's next assertion would then verify *as the victim*.
    The registration is refused instead, and the victim's passkey keeps working.
    """
    rp_id, origin = rp
    victim = await _user(db, "jenny")
    attacker = await _user(db, "mallory")

    victims_device = SoftAuthenticator(rp_id=rp_id, origin=origin)
    await _register(db, victim, victims_device, label="jenny-phone")

    # Same credential id, the attacker's own key.
    forged = SoftAuthenticator(
        rp_id=rp_id, origin=origin, credential_id=victims_device.credential_id
    )
    with pytest.raises(webauthn.PasskeyError, match="already registered"):
        await _register(db, attacker, forged, label="stolen")

    row = await db.fetchrow(
        "SELECT user_id, label FROM webauthn_credential WHERE credential_id = $1",
        victims_device.credential_id,
    )
    assert (row["user_id"], row["label"]) == (victim, "jenny-phone")
    assert await _authenticate(db, victims_device) == victim


# --- §14.4: the binding is to PUBLIC_URL, not to whatever the default happens to be --------


async def test_the_credential_records_the_configured_origin_not_the_default(db, monkeypatch):
    """The other origin test compares `settings().rp_id` to `settings().rp_id` — true of any
    value, including a wrong one. This pins a literal, under a PUBLIC_URL nothing else uses."""
    from spielplan.core.config import settings

    monkeypatch.setenv("PUBLIC_URL", "https://spielplan.example.tld")
    settings.cache_clear()
    try:
        assert settings().rp_id == "spielplan.example.tld"
        device = SoftAuthenticator(rp_id="spielplan.example.tld",
                                   origin="https://spielplan.example.tld")
        user_id = await _user(db)
        await _register(db, user_id, device)

        row = await db.fetchrow(
            "SELECT rp_id FROM webauthn_credential WHERE user_id = $1", user_id
        )
        assert row["rp_id"] == "spielplan.example.tld"
        assert await db.fetchval("SELECT count(*) FROM webauthn_challenge") == 0
        assert await _authenticate(db, device) == user_id
    finally:
        settings.cache_clear()


async def test_a_credential_from_the_old_origin_is_refused_after_the_move(db, device, monkeypatch):
    """§14.4: "changing PUBLIC_URL later invalidates registered passkeys." Registered under the
    default origin, then the app moves — the same authenticator must stop working."""
    from spielplan.core.config import settings

    user_id = await _user(db)
    await _register(db, user_id, device)

    monkeypatch.setenv("PUBLIC_URL", "https://spielplan.example.tld")
    settings.cache_clear()
    try:
        with pytest.raises(webauthn.PasskeyError, match="different address"):
            await _authenticate(db, device)
    finally:
        settings.cache_clear()


# --- §3.2: the sign-in ceremony is not an account oracle -----------------------------------


async def test_a_sign_in_ceremony_never_narrows_to_an_account(db, device):
    """The route needs no session. A name-narrowed `allowCredentials` would tell an anonymous
    caller which household members exist and which of them hold a passkey — the same roster
    `/auth/switchable` keeps behind a session."""
    user_id = await _user(db, "jenny")
    await _register(db, user_id, device)

    for name in (None, "jenny", "JENNY", "nobody-here"):
        ceremony = await webauthn.authentication_options(db, name=name)
        assert ceremony.options["allowCredentials"] == [], (
            f"the ceremony for {name!r} disclosed credentials"
        )

    # …and sign-in still works, because the credential is discoverable.
    assert await _authenticate(db, device) == user_id
