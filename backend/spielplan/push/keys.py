"""The VAPID keypair (§2): minted once at first boot, sealed like every secret. Never regenerated:
subscriptions are bound to the public key, and a new pair silently stops every delivery.
"""

from __future__ import annotations

import logging
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field

import asyncpg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from spielplan.core import secrets

log = logging.getLogger("spielplan.push")

# One row: `value` holds the public half, `secret` the sealed private half; meaningless apart.
SETTING_KEY = "push.vapid"

# The raw P-256 scalar, not the DER envelope.
_SCALAR_BYTES = 32


def b64(raw: bytes) -> str:
    """base64url without padding, as RFC 8291/8292 and `PushSubscription.toJSON()` spell keys."""
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    """Tolerant of the padding browsers omit and Firefox sometimes keeps."""
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class VapidKeys:
    """A keypair that can only sign: the private half has no accessor or repr, so no route can return it."""

    public_key: str
    _signing_key: ec.EllipticCurvePrivateKey = field(repr=False)

    def sign(self, message: bytes) -> bytes:
        """ES256 in JWS's raw r||s form; `cryptography` signs DER, which every push service rejects."""
        r, s = decode_dss_signature(self._signing_key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(_SCALAR_BYTES, "big") + s.to_bytes(_SCALAR_BYTES, "big")


async def _rebind_to_its_row(conn: asyncpg.Connection, row: asyncpg.Record) -> None:
    """Re-seal a private half sealed before sec-10's row binding. Silent when the DEK is unreadable."""
    try:
        dek = await secrets.load_dek(conn, row["secret_key_id"])
    except secrets.SecretsUnreadable:
        return
    aad = secrets.aad_for("app_setting", SETTING_KEY)
    try:
        # Opening without the AAD is what identifies an unbound row; a bound one fails and is left alone.
        sealed = secrets.open_sealed(dek, row["secret"], None)
    except InvalidTag:
        return
    await conn.execute(
        # `secret = $3`, so a pair replaced in between is never joined to the old private half.
        "UPDATE app_setting SET secret = $2, updated_at = now() WHERE key = $1 AND secret = $3",
        SETTING_KEY,
        secrets.seal(dek, sealed, aad),
        row["secret"],
    )


async def ensure_keypair(conn: asyncpg.Connection) -> str | None:
    """The public key, minting the pair on first boot; None without SECRETS_KEY (§3.1). The sealed half
    decides whether a pair exists: a row that lost it is replaced."""
    row = await conn.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = $1", SETTING_KEY
    )
    if row is not None and row["secret"] is not None:
        await _rebind_to_its_row(conn, row)
        return (row["value"] or {}).get("public_key")

    try:
        key_id, dek = await secrets.ensure_dek(conn)
    except RuntimeError:
        log.info("no SECRETS_KEY — web-push is unconfigured; notifications fall back to §6's banner")
        return None

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    scalar = private.private_numbers().private_value.to_bytes(_SCALAR_BYTES, "big")
    # The WHERE is the safety property: a row holding a sealed private half is never overwritten (the
    # race loser adopts the winner's), while one without it is replaced.
    await conn.execute(
        """
        INSERT INTO app_setting (key, value, secret, secret_key_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (key) DO UPDATE
           SET value = EXCLUDED.value, secret = EXCLUDED.secret,
               secret_key_id = EXCLUDED.secret_key_id, updated_at = now()
         WHERE app_setting.secret IS NULL
        """,
        SETTING_KEY,
        {"public_key": b64(public)},
        secrets.seal(dek, {"private_key": b64(scalar)}, secrets.aad_for("app_setting", SETTING_KEY)),
        key_id,
    )
    if row is not None:
        log.warning(
            "the stored web-push keypair had lost its private half - minted a replacement. "
            "Every device must subscribe again; the old public key can no longer be signed for."
        )
    return await public_key(conn)


async def public_key(conn: asyncpg.Connection) -> str | None:
    """Reads, never mints. A row without its sealed half is not a pair: None, as `load` answers."""
    value = await conn.fetchval(
        "SELECT value FROM app_setting WHERE key = $1 AND secret IS NOT NULL", SETTING_KEY
    )
    return (value or {}).get("public_key")


async def load(conn: asyncpg.Connection) -> VapidKeys | None:
    """None when absent or its DEK is unreadable (a restored database against a rotated key)."""
    row = await conn.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = $1", SETTING_KEY
    )
    if row is None or row["secret"] is None:
        return None
    try:
        dek = await secrets.load_dek(conn, row["secret_key_id"])
        sealed = secrets.open_sealed(dek, row["secret"], secrets.aad_for("app_setting", SETTING_KEY))
    except Exception:
        log.warning("the stored VAPID private key cannot be decrypted — web-push is disabled")
        return None
    scalar = int.from_bytes(unb64(sealed["private_key"]), "big")
    return VapidKeys(row["value"]["public_key"], ec.derive_private_key(scalar, ec.SECP256R1()))


__all__ = ["SETTING_KEY", "VapidKeys", "b64", "ensure_keypair", "load", "public_key", "unb64"]
