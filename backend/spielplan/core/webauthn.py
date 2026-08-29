"""WebAuthn passkeys. Spec v2.1 §3.2 (primary auth), §3.3, §14.4, §4.2 webauthn_credential.

"**Primary: WebAuthn passkeys** … viable because the Traefik+Cloudflare origin gives real
TLS. Registration from the profile page; multiple passkeys per user (phone + desktop)."

Three properties this module exists to hold, each one a way passkeys fail quietly:

  * **Origin binding.** `PUBLIC_URL` is required config precisely because a credential is
    bound to an origin (§14 risk 4). Both ceremonies check `expected_rp_id` and
    `expected_origin` against it, and a stored credential registered under a *different*
    rp_id is refused before verification even runs — after a `PUBLIC_URL` change those rows
    are archaeology, and §14.4 promises they stop working rather than half-working.
  * **Single-use challenges.** Issued here, stored server-side, deleted on first use. A
    replayed challenge is a signature over bytes the attacker chose.
  * **Monotonic sign_count.** §4.2 stores `sign_count` for exactly one reason: an assertion
    whose counter has not advanced is a replay of one this server already accepted.

Fallbacks stay: §3.2 keeps password login always available, so a household that cannot
register a passkey is never locked out.
"""

from __future__ import annotations

import secrets as pysecrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from spielplan.core.config import settings

CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Spielplan"


class PasskeyError(RuntimeError):
    """A ceremony that did not verify. The message reaches the user, so it says what to do."""


@dataclass(frozen=True)
class Ceremony:
    """What the client needs to run one ceremony: the handle and the options JSON."""

    id: str
    options: dict[str, Any]


def _rp_id() -> str:
    return settings().rp_id


def _origin() -> str:
    return settings().public_url


async def _issue(
    conn: asyncpg.Connection, *, purpose: str, user_id: int | None, challenge: bytes
) -> str:
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
    """Take a challenge and destroy it in the same statement — single use, no race."""
    row = await conn.fetchrow(
        "DELETE FROM webauthn_challenge WHERE id = $1 AND purpose = $2 RETURNING *",
        handle, purpose,
    )
    if row is None:
        raise PasskeyError("that sign-in attempt has expired — start again")
    if row["expires_at"] < datetime.now(UTC):
        raise PasskeyError("that sign-in attempt has expired — start again")
    if row["rp_id"] != _rp_id():
        # §14.4: PUBLIC_URL changed between the two halves of one ceremony.
        raise PasskeyError("this app's address changed mid-sign-in — start again")
    return row


async def prune_challenges(conn: asyncpg.Connection) -> int:
    result = await conn.execute("DELETE FROM webauthn_challenge WHERE expires_at < now()")
    return int(str(result).rsplit(" ", 1)[-1]) if str(result).startswith("DELETE") else 0


# --- registration -----------------------------------------------------------------------


async def registration_options(
    conn: asyncpg.Connection, *, user_id: int, user_name: str
) -> Ceremony:
    """§3.2: 'Registration from the profile page; multiple passkeys per user (phone +
    desktop).' The existing credentials go out as `excludeCredentials` so registering the same
    authenticator twice is refused by the authenticator itself rather than producing a second
    row that shadows the first."""
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
            # Discoverable so the phone can offer the account without it being typed first —
            # that is the whole "Face ID and you are in" experience §3.2 is buying.
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
    except InvalidRegistrationResponse as exc:
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
        # Deliberately NOT an upsert. The credential id comes out of a response the client
        # composes, and attestation format "none" means nothing vouches for it — so an upsert
        # would let one account overwrite another account's stored public key while the row
        # kept its original `user_id`, and the attacker's next assertion would verify *as that
        # user*. A collision is either a duplicate registration `excludeCredentials` should
        # have stopped, or that. Both are refusals.
        raise PasskeyError("that passkey is already registered") from exc
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "label": label,
        "rp_id": _rp_id(),
    }


def _transports(credential: dict) -> list[str]:
    raw = (credential.get("response") or {}).get("transports") or []
    return [str(t) for t in raw]


# --- authentication ---------------------------------------------------------------------


async def authentication_options(conn: asyncpg.Connection, *, name: str | None = None) -> Ceremony:
    """Issue a sign-in ceremony. Always with an empty allow-list.

    A name-narrowed `allowCredentials` would be an unauthenticated oracle: a non-empty list
    means "that account exists, is active, and has a passkey here", an empty one means it does
    not, and this route needs no session at all. §3.2 keeps the household roster behind a
    session for exactly that reason, so the sign-in screen must not hand it out by another door.

    Nothing is lost by it. Registration asks for a discoverable credential
    (`ResidentKeyRequirement.PREFERRED`), which is what makes the phone offer "sign in as
    Jenny" on its own — the allow-list was only ever a hint. An authenticator that stores no
    discoverable credential falls back to the password, which §3.2 keeps always available.

    `name` is accepted and ignored so the client may keep sending what the user typed without
    that choice changing what the server discloses.
    """
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


async def authenticate(conn: asyncpg.Connection, *, handle: str, credential: dict) -> int:
    """Verify an assertion and return the app user id it proves.

    Every refusal in here is deliberately the same sentence to the user; the distinctions
    matter to this code, not to the person holding the phone.
    """
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
        # §14.4: the credential belongs to a previous PUBLIC_URL. Refusing beats verifying
        # against an origin it was never bound to.
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
    except InvalidAuthenticationResponse as exc:
        # This is also the replay path: py_webauthn refuses an assertion whose sign_count has
        # not advanced past the stored one.
        raise PasskeyError(f"that passkey could not be verified: {exc}") from exc

    await conn.execute(
        "UPDATE webauthn_credential SET sign_count = $2, last_used_at = now() "
        "WHERE credential_id = $1",
        credential_id, verified.new_sign_count,
    )
    return int(stored["user_id"])


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
            # §14.4 made legible: a credential from an older PUBLIC_URL is listed and marked
            # dead rather than vanishing, so "my passkey stopped working" has an answer on
            # screen instead of in the logs.
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
