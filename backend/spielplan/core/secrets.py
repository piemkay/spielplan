"""Connector-secret custody (§2, §14.3). SECRETS_KEY wraps one random DEK; secrets are AES-GCM
sealed under the DEK and every ciphertext names its `key_id`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

import asyncpg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from spielplan.core.config import settings

log = logging.getLogger("spielplan.secrets")

_NONCE = 12
_INFO = b"spielplan/dek-wrap/v1"


class SecretsUnreadable(RuntimeError):
    """The stored DEK will not open under this SECRETS_KEY: an operator state (restored dump, new key).
    A RuntimeError so `push/keys.ensure_keypair`'s existing handler covers it."""

    def __init__(self, detail: str, key_id: str | None = None) -> None:
        super().__init__(detail)
        self.key_id = key_id


def _kek(secrets_key: str) -> bytes:
    """HKDF, so SECRETS_KEY may be any printable string, never truncated or padded into an AES key."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO
    ).derive(secrets_key.encode("utf-8"))


def _wrap(dek: bytes, secrets_key: str) -> bytes:
    aes = AESGCM(_kek(secrets_key))
    nonce = os.urandom(_NONCE)
    return nonce + aes.encrypt(nonce, dek, _INFO)


def _unwrap(blob: bytes, secrets_key: str, *, key_id: str | None = None) -> bytes:
    aes = AESGCM(_kek(secrets_key))
    try:
        return aes.decrypt(blob[:_NONCE], blob[_NONCE:], _INFO)
    except InvalidTag as exc:
        named = f" ({key_id})" if key_id else ""
        raise SecretsUnreadable(
            f"the wrapped data-encryption key{named} does not open under this SECRETS_KEY. "
            "Spec section 2: back up the env file alongside the dumps - restore the .env that "
            "was current when the dump was taken, or run 'spielplan-secrets reset' to retire the "
            "unreadable row and re-enter the connector credentials.",
            key_id,
        ) from exc


async def active_key_id(conn: asyncpg.Connection) -> str | None:
    """Read-only: the boot probe must not mint a DEK, as `ensure_dek` would."""
    return await conn.fetchval(
        "SELECT key_id FROM data_encryption_key WHERE retired_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


async def retire_dek(conn: asyncpg.Connection, key_id: str) -> None:
    """Loses nothing: `load_dek` still opens a retired row by id. By id, because another process may
    have repaired the active row in between."""
    await conn.execute(
        "UPDATE data_encryption_key SET retired_at = now() "
        "WHERE key_id = $1 AND retired_at IS NULL",
        key_id,
    )


async def ensure_dek(
    conn: asyncpg.Connection, *, retire_unreadable: bool = False
) -> tuple[str, bytes]:
    """`retire_unreadable` is the caller's word: an admin re-entering a credential is the repair, while
    the boot-time env seed must skip rather than spend it unattended at every restart."""
    secrets_key = settings().require_secrets_key()
    row = await conn.fetchrow(
        "SELECT key_id, wrapped_dek FROM data_encryption_key "
        "WHERE retired_at IS NULL ORDER BY created_at DESC LIMIT 1"
    )
    if row is not None:
        try:
            return row["key_id"], _unwrap(row["wrapped_dek"], secrets_key, key_id=row["key_id"])
        except SecretsUnreadable:
            if not retire_unreadable:
                raise
            await retire_dek(conn, row["key_id"])
            log.error(
                "sealing under a fresh data-encryption key: retired %s, which this SECRETS_KEY "
                "does not open. Every other secret sealed under it stays unreadable until its "
                "own SECRETS_KEY returns (spec section 2).",
                row["key_id"],
            )

    # DO NOTHING then re-SELECT adopts the winner of concurrent first boots, as `push/keys` does.
    # No conflict target, so both the primary key and 0017's partial unique index are caught.
    dek = os.urandom(32)
    key_id = urlsafe_b64encode(os.urandom(9)).decode("ascii")
    await conn.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ($1, $2) "
        "ON CONFLICT DO NOTHING",
        key_id,
        _wrap(dek, secrets_key),
    )
    won = await conn.fetchrow(
        "SELECT key_id, wrapped_dek FROM data_encryption_key "
        "WHERE retired_at IS NULL ORDER BY created_at DESC LIMIT 1"
    )
    if won is None:  # pragma: no cover - the insert either landed or another row is active
        raise SecretsUnreadable(
            "the data-encryption key row vanished between insert and read; retry the request"
        )
    if won["key_id"] == key_id:
        return key_id, dek
    return won["key_id"], _unwrap(won["wrapped_dek"], secrets_key, key_id=won["key_id"])


async def load_dek(conn: asyncpg.Connection, key_id: str) -> bytes:
    """Both refusals are `SecretsUnreadable`: to a reader, no key and the wrong key are one fact."""
    blob = await conn.fetchval(
        "SELECT wrapped_dek FROM data_encryption_key WHERE key_id = $1", key_id
    )
    if blob is None:
        raise SecretsUnreadable(
            f"no data-encryption key row with id {key_id!r} - the ciphertext naming it was "
            "restored without its key row, or the row was deleted rather than retired",
            key_id,
        )
    secrets_key = settings().secrets_key
    if not secrets_key:
        raise SecretsUnreadable(
            f"SECRETS_KEY is not set, so the data-encryption key ({key_id}) cannot be unwrapped. "
            "Spec section 2: connector secrets are AEAD-encrypted under a DEK wrapped by "
            "SECRETS_KEY, and the app refuses to fall back to SESSION_SECRET.",
            key_id,
        )
    return _unwrap(blob, secrets_key, key_id=key_id)


async def unreadable_key_ids(conn: asyncpg.Connection) -> list[str]:
    """One question for both the System card and `spielplan-secrets reset`, so they cannot disagree.
    Un-retired rows count even if unnamed; retired ones count while a ciphertext names them."""
    named = {
        row["key_id"]
        for row in await conn.fetch(
            "SELECT secrets_key_id AS key_id FROM connector_config "
            "WHERE secrets_encrypted IS NOT NULL "
            "UNION SELECT secret_key_id FROM app_setting WHERE secret IS NOT NULL "
            "UNION SELECT key_id FROM data_encryption_key WHERE retired_at IS NULL"
        )
    }
    named.discard(None)
    unreadable: list[str] = []
    for key_id in sorted(named):
        try:
            # A ciphertext naming a deleted key row lands here too: unrecoverable, not merely locked.
            await load_dek(conn, key_id)
        except SecretsUnreadable:
            unreadable.append(key_id)
    return unreadable


def aad_for(table: str, row: str) -> bytes:
    """Binds each ciphertext to its row, so a blob copied into another row fails to open (§14.3)."""
    return f"{table}/{row}".encode()


def seal(dek: bytes, payload: dict[str, Any], aad: bytes | None = None) -> bytes:
    aes = AESGCM(dek)
    nonce = os.urandom(_NONCE)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return nonce + aes.encrypt(nonce, raw, aad)


def _open_with_fallback(
    dek: bytes, blob: bytes, aad: bytes | None
) -> tuple[dict[str, Any], bool]:
    """`bound` is False only for a ciphertext sealed before M4.7 bound them; callers re-seal it.
    Delete the fallback once no install predates M4.7."""
    aes = AESGCM(dek)
    try:
        return json.loads(aes.decrypt(blob[:_NONCE], blob[_NONCE:], aad)), True
    except InvalidTag:
        if aad is None:
            raise
        return json.loads(aes.decrypt(blob[:_NONCE], blob[_NONCE:], None)), False


def open_sealed(dek: bytes, blob: bytes, aad: bytes | None = None) -> dict[str, Any]:
    return _open_with_fallback(dek, blob, aad)[0]


async def _rebind_connector_row(
    conn: asyncpg.Connection, name: str, stored: bytes, dek: bytes, payload: dict[str, Any]
) -> None:
    """Re-sealed on read, not on the next save, which may never come. `updated_at` stays: no admin
    saved anything."""
    await conn.execute(
        # The `= $3` guard keeps a save that landed in between from being overwritten.
        "UPDATE connector_config SET secrets_encrypted = $2 "
        "WHERE name = $1 AND secrets_encrypted = $3",
        name,
        seal(dek, payload, aad_for("connector_config", name)),
        stored,
    )


async def put_connector_secrets(
    conn: asyncpg.Connection,
    name: str,
    config: dict[str, Any],
    secrets: dict[str, Any] | None,
    *,
    retire_unreadable: bool = False,
) -> None:
    key_id, dek = (None, None)
    blob = None
    if secrets:
        key_id, dek = await ensure_dek(conn, retire_unreadable=retire_unreadable)
        blob = seal(dek, secrets, aad_for("connector_config", name))
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


async def put_connector_config(
    conn: asyncpg.Connection, name: str, config: dict[str, Any]
) -> None:
    """Unlike `put_connector_secrets(secrets=None)`, never NULLs the sealed columns: a secret this
    process cannot read must survive until the right `.env` returns."""
    await conn.execute(
        """
        INSERT INTO connector_config (name, config, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (name) DO UPDATE SET config = EXCLUDED.config, updated_at = now()
        """,
        name,
        config,
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
    try:
        opened, bound = _open_with_fallback(
            dek, row["secrets_encrypted"], aad_for("connector_config", name)
        )
    except InvalidTag as exc:
        # The DEK opened, so the ciphertext itself is wrong (moved or tampered). One exception type out
        # of this module, which `registry.load_jellyfin` degrades on.
        raise SecretsUnreadable(
            f"the stored secret for connector {name!r} does not open under its own key_id "
            f"({row['secrets_key_id']}) - the ciphertext does not belong to this row",
            row["secrets_key_id"],
        ) from exc
    if not bound:
        await _rebind_connector_row(conn, name, row["secrets_encrypted"], dek, opened)
    return config, opened


def rewrap_dek(wrapped: bytes, old_key: str, new_key: str) -> bytes:
    return _wrap(_unwrap(wrapped, old_key), new_key)


# Domain-separated, so the fingerprint is not the digest other tools print for the same key.
_FINGERPRINT_INFO = b"spielplan/secrets-key-fingerprint/v1"


def key_fingerprint(secrets_key: str) -> str:
    """12 hex chars (48 bits): tells keys apart by eye, never proves what one is (decision 182)."""
    return hashlib.sha256(_FINGERPRINT_INFO + secrets_key.encode()).hexdigest()[:12]


__all__ = [
    "SecretsUnreadable",
    "aad_for",
    "active_key_id",
    "key_fingerprint",
    "ensure_dek",
    "load_dek",
    "retire_dek",
    "unreadable_key_ids",
    "seal",
    "open_sealed",
    "put_connector_config",
    "put_connector_secrets",
    "get_connector_secrets",
    "rewrap_dek",
]


def _b64(x: bytes) -> str:  # pragma: no cover - helper kept for admin tooling
    return urlsafe_b64encode(x).decode("ascii")


def _unb64(x: str) -> bytes:  # pragma: no cover - helper kept for admin tooling
    return urlsafe_b64decode(x)
