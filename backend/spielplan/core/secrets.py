"""Connector-secret custody. Spec v2.1 §2, §14.3.

SECRETS_KEY (env) wraps a random 256-bit data-encryption key (DEK) created at first boot and
stored in `data_encryption_key`. Connector secrets are AEAD-encrypted under that DEK and every
ciphertext carries its `key_id`, so rotation is possible without re-reading plaintext from
anywhere else.

Rotating SESSION_SECRET invalidates sessions only and never touches stored secrets.
Rotating SECRETS_KEY is an explicit admin action that re-wraps the one DEK row.

§14.3 is why this matters more than it looks: a Jellyfin API key is unscoped and
admin-equivalent, so the stored connector secret can administer the whole media server.
"""

from __future__ import annotations

import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

import asyncpg
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from spielplan.core.config import settings

_NONCE = 12
_INFO = b"spielplan/dek-wrap/v1"


def _kek(secrets_key: str) -> bytes:
    """Derive a 256-bit key-encryption key from SECRETS_KEY.

    HKDF rather than raw bytes so SECRETS_KEY may be any printable string the operator
    generated, without silently truncating or padding it into an AES key.
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO
    ).derive(secrets_key.encode("utf-8"))


def _wrap(dek: bytes, secrets_key: str) -> bytes:
    aes = AESGCM(_kek(secrets_key))
    nonce = os.urandom(_NONCE)
    return nonce + aes.encrypt(nonce, dek, _INFO)


def _unwrap(blob: bytes, secrets_key: str) -> bytes:
    aes = AESGCM(_kek(secrets_key))
    return aes.decrypt(blob[:_NONCE], blob[_NONCE:], _INFO)


async def ensure_dek(conn: asyncpg.Connection) -> tuple[str, bytes]:
    """Return the active (key_id, dek), creating it on first boot."""
    secrets_key = settings().require_secrets_key()
    row = await conn.fetchrow(
        "SELECT key_id, wrapped_dek FROM data_encryption_key "
        "WHERE retired_at IS NULL ORDER BY created_at DESC LIMIT 1"
    )
    if row is not None:
        return row["key_id"], _unwrap(row["wrapped_dek"], secrets_key)

    dek = os.urandom(32)
    key_id = urlsafe_b64encode(os.urandom(9)).decode("ascii")
    await conn.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ($1, $2)",
        key_id,
        _wrap(dek, secrets_key),
    )
    return key_id, dek


async def load_dek(conn: asyncpg.Connection, key_id: str) -> bytes:
    """Load a specific DEK by id — used when decrypting older ciphertexts after rotation."""
    blob = await conn.fetchval(
        "SELECT wrapped_dek FROM data_encryption_key WHERE key_id = $1", key_id
    )
    if blob is None:
        raise KeyError(f"no data-encryption key with id {key_id!r}")
    return _unwrap(blob, settings().require_secrets_key())


def seal(dek: bytes, payload: dict[str, Any]) -> bytes:
    aes = AESGCM(dek)
    nonce = os.urandom(_NONCE)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return nonce + aes.encrypt(nonce, raw, None)


def open_sealed(dek: bytes, blob: bytes) -> dict[str, Any]:
    aes = AESGCM(dek)
    return json.loads(aes.decrypt(blob[:_NONCE], blob[_NONCE:], None))


async def put_connector_secrets(
    conn: asyncpg.Connection, name: str, config: dict[str, Any], secrets: dict[str, Any] | None
) -> None:
    key_id, dek = (None, None)
    blob = None
    if secrets:
        key_id, dek = await ensure_dek(conn)
        blob = seal(dek, secrets)
    await conn.execute(
        """
        INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (name) DO UPDATE
          SET config = EXCLUDED.config,
              secrets_encrypted = EXCLUDED.secrets_encrypted,
              secrets_key_id = EXCLUDED.secrets_key_id,
              updated_at = now()
        """,
        name,
        config,
        blob,
        key_id,
    )


async def get_connector_secrets(
    conn: asyncpg.Connection, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await conn.fetchrow(
        "SELECT config, secrets_encrypted, secrets_key_id FROM connector_config WHERE name = $1",
        name,
    )
    if row is None:
        return {}, {}
    config = row["config"]      # decoded by the pool's json codec
    if not row["secrets_encrypted"]:
        return config, {}
    dek = await load_dek(conn, row["secrets_key_id"])
    return config, open_sealed(dek, row["secrets_encrypted"])


def rewrap_dek(wrapped: bytes, old_key: str, new_key: str) -> bytes:
    """Admin action: re-wrap the one DEK row under a new SECRETS_KEY."""
    return _wrap(_unwrap(wrapped, old_key), new_key)


__all__ = [
    "ensure_dek",
    "load_dek",
    "seal",
    "open_sealed",
    "put_connector_secrets",
    "get_connector_secrets",
    "rewrap_dek",
]


def _b64(x: bytes) -> str:  # pragma: no cover - helper kept for admin tooling
    return urlsafe_b64encode(x).decode("ascii")


def _unb64(x: str) -> bytes:  # pragma: no cover - helper kept for admin tooling
    return urlsafe_b64decode(x)
