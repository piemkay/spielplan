"""WebAuthn passkeys (§3.2, §14.4). A credential from another rp_id is refused before verifying,
challenges are single-use (the replay guard), and the sign-count UPDATE refuses a counter that
went backwards while admitting synced passkeys' constant 0.
"""

from __future__ import annotations

import secrets as pysecrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from spielplan.core.config import settings

CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Spielplan"

# Anonymous callers can grow this table, so the oldest rows are evicted, never refused: a 429 would
# lock the household out. Sized to be unreachable by accident, not as a security boundary.
MAX_OPEN_SIGN_INS = 500


class PasskeyError(RuntimeError):
    """A ceremony that did not verify. The message reaches the user, so it says what to do."""


@dataclass(frozen=True)
class Ceremony:
    id: str
    options: dict[str, Any]


def _rp_id() -> str:
    return settings().rp_id


def _origin() -> str:
    return settings().public_url


async def _issue(
    conn: asyncpg.Connection, *, purpose: str, user_id: int | None, challenge: bytes
) -> str:
    # Swept inline too, so table size follows the five-minute TTL, not the hourly prune.
    await conn.execute(
        "DELETE FROM webauthn_challenge WHERE purpose = $1 AND expires_at < now()", purpose
    )
    if purpose == "authenticate":
        # Evict oldest first: `expires_at` orders by age (constant TTL), `id` breaks ties.
        await conn.execute(
            """
            DELETE FROM webauthn_challenge
             WHERE purpose = 'authenticate'
               AND id IN (SELECT id FROM webauthn_challenge WHERE purpose = 'authenticate'
                           ORDER BY expires_at DESC, id DESC OFFSET $1)
            """,
            MAX_OPEN_SIGN_INS - 1,
        )
    handle = pysecrets.token_urlsafe(24)
    await conn.execute(
        """
        INSERT INTO webauthn_challenge (id, user_id, purpose, challenge, rp_id, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        handle, user_id, purpose, challenge, _rp_id(), datetime.now(UTC) + CHALLENGE_TTL,
    )
    return handle


async def _consume(conn: asyncpg.Connection, handle: str, purpose: str) -> asyncpg.Record:
    """Take and destroy in one statement: single use, no race."""
    row = await conn.fetchrow(
        "DELETE FROM webauthn_challenge WHERE id = $1 AND purpose = $2 RETURNING *",
        handle, purpose,
    )
    if row is None:
        raise PasskeyError("that sign-in attempt has expired — start again")
    if row["expires_at"] < datetime.now(UTC):
        raise PasskeyError("that sign-in attempt has expired — start again")
    if row["rp_id"] != _rp_id():
        raise PasskeyError("this app's address changed mid-sign-in — start again")
    return row


async def prune_challenges(conn: asyncpg.Connection) -> int:
    result = await conn.execute("DELETE FROM webauthn_challenge WHERE expires_at < now()")
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0


async def registration_options(
    conn: asyncpg.Connection, *, user_id: int, user_name: str
) -> Ceremony:
    """Existing credentials go out as `excludeCredentials`, so the authenticator itself refuses a twin."""
    existing = await conn.fetch(
        "SELECT credential_id FROM webauthn_credential WHERE user_id = $1 AND rp_id = $2",
        user_id, _rp_id(),
    )
    options = webauthn.generate_registration_options(
        rp_id=_rp_id(),
        rp_name=RP_NAME,
        user_id=str(user_id).encode("utf-8"),
        user_name=user_name,
        user_display_name=user_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(r["credential_id"])) for r in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable, so the phone offers the account without it being typed (§3.2).
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    handle = await _issue(
        conn, purpose="register", user_id=user_id, challenge=options.challenge
    )
    return Ceremony(id=handle, options=_json(options))


async def register(
    conn: asyncpg.Connection, *, user_id: int, handle: str, credential: dict, label: str | None
) -> dict[str, Any]:
    row = await _consume(conn, handle, "register")
    if row["user_id"] != user_id:
        raise PasskeyError("that registration belongs to a different account")

    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=bytes(row["challenge"]),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
        )
    except WebAuthnException as exc:
        # The base class: a malformed payload raises siblings of `InvalidRegistrationResponse`.
        raise PasskeyError(f"that passkey could not be registered: {exc}") from exc

    try:
        await conn.execute(
            """
            INSERT INTO webauthn_credential
                (credential_id, user_id, public_key, sign_count, transports, label, rp_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            verified.credential_id,
            user_id,
            verified.credential_public_key,
            verified.sign_count,
            _transports(credential),
            label,
            _rp_id(),
        )
    except asyncpg.UniqueViolationError as exc:
        # Never an upsert: with attestation "none" the credential id is client-chosen, so an upsert would
        # let one account overwrite another's public key.
        raise PasskeyError("that passkey is already registered") from exc
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "label": label,
        "rp_id": _rp_id(),
    }


def _transports(credential: dict) -> list[str]:
    raw = (credential.get("response") or {}).get("transports") or []
    return [str(t) for t in raw]


async def authentication_options(conn: asyncpg.Connection, *, name: str | None = None) -> Ceremony:
    """Always an empty allow-list: a name-narrowed one would tell an anonymous caller which accounts
    exist. `name` is accepted and ignored."""
    _ = name
    options = webauthn.generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=[],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    handle = await _issue(
        conn, purpose="authenticate", user_id=None, challenge=options.challenge
    )
    return Ceremony(id=handle, options=_json(options))


async def authenticate(
    conn: asyncpg.Connection, *, handle: str, credential: dict
) -> tuple[int, bool]:
    """Returns (user id, whether the authenticator verified the user): a presence-only tap signs in
    but must not satisfy the 24 h admin re-prompt. Every refusal is the same sentence."""
    row = await _consume(conn, handle, "authenticate")

    raw_id = credential.get("rawId") or credential.get("id")
    if not raw_id:
        raise PasskeyError("that passkey could not be verified")
    try:
        credential_id = base64url_to_bytes(str(raw_id))
    except Exception as exc:  # noqa: BLE001 - malformed client input, not a server fault
        raise PasskeyError("that passkey could not be verified") from exc

    stored = await conn.fetchrow(
        """
        SELECT c.credential_id, c.public_key, c.sign_count, c.rp_id, c.user_id
          FROM webauthn_credential c JOIN app_user u ON u.id = c.user_id
         WHERE c.credential_id = $1 AND u.is_active
        """,
        credential_id,
    )
    if stored is None:
        raise PasskeyError("that passkey is not registered here")
    if stored["rp_id"] != _rp_id():
        # Registered under a previous PUBLIC_URL (§14.4): refuse rather than verify against it.
        raise PasskeyError("that passkey was registered for a different address")

    try:
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=bytes(row["challenge"]),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            credential_public_key=bytes(stored["public_key"]),
            credential_current_sign_count=int(stored["sign_count"]),
        )
    except WebAuthnException as exc:
        # The base class: a malformed body would otherwise 500 after the challenge was consumed.
        raise PasskeyError(f"that passkey could not be verified: {exc}") from exc

    # The UPDATE is the counter check, so two racing assertions cannot both land.
    # `$2 = 0 AND sign_count = 0` admits synced passkeys, which always report 0.
    advanced = await conn.fetchval(
        "UPDATE webauthn_credential SET sign_count = $2, last_used_at = now() "
        "WHERE credential_id = $1 AND (sign_count < $2 OR ($2 = 0 AND sign_count = 0)) "
        "RETURNING 1",
        credential_id, verified.new_sign_count,
    )
    if advanced is None:
        raise PasskeyError("that passkey could not be verified")
    return int(stored["user_id"]), bool(verified.user_verified)


async def list_credentials(conn: asyncpg.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT credential_id, label, rp_id, created_at, last_used_at, sign_count "
        "FROM webauthn_credential WHERE user_id = $1 ORDER BY created_at",
        user_id,
    )
    return [
        {
            "id": bytes_to_base64url(bytes(r["credential_id"])),
            "label": r["label"],
            "rp_id": r["rp_id"],
            # Listed as dead rather than hidden, so "my passkey stopped working" has an answer (§14.4).
            "usable": r["rp_id"] == _rp_id(),
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "sign_count": r["sign_count"],
        }
        for r in rows
    ]


async def delete_credential(conn: asyncpg.Connection, user_id: int, credential_id: str) -> bool:
    try:
        raw = base64url_to_bytes(credential_id)
    except Exception:  # noqa: BLE001
        return False
    result = await conn.execute(
        "DELETE FROM webauthn_credential WHERE user_id = $1 AND credential_id = $2",
        user_id, raw,
    )
    return str(result).endswith("1")


def _json(options: Any) -> dict[str, Any]:
    import json

    return json.loads(options_to_json(options))


__all__ = [
    "MAX_OPEN_SIGN_INS",
    "Ceremony",
    "PasskeyError",
    "authenticate",
    "authentication_options",
    "delete_credential",
    "list_credentials",
    "prune_challenges",
    "register",
    "registration_options",
]
