"""One web-push message: RFC 8291 payload encryption, RFC 8292 VAPID signing. 404/410 deletes that row
by id, any other failure keeps it, a delivery stamps `last_seen_ok` (§4.2). Nothing raises (§6).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from spielplan.core.config import settings
from spielplan.push import keys

log = logging.getLogger("spielplan.push")

# RFC 8291 §3.1's wire constants; a typo produces a body the phone silently drops.
_KEY_INFO = b"WebPush: info\x00"
_CEK_INFO = b"Content-Encoding: aes128gcm\x00"
_NONCE_INFO = b"Content-Encoding: nonce\x00"
_SALT_BYTES = 16

# One padding delimiter plus the AES-GCM tag.
_PAD_AND_TAG = 1 + 16
_RECORD_SIZE = 4096

# Seconds. RFC 8292 caps the JWT at 24 h; 12 tolerates an hour of clock skew either way.
_JWT_LIFETIME = 12 * 3600

# RFC 8030 TTL, seconds: an invitation to a lobby is noise the next morning.
_TTL = 3600

# Seconds: the send is inline in whatever opened the session, which must not wait long.
_TIMEOUT = 10.0


def device_handle(endpoint: str) -> str:
    """A stable, non-reversible name for one push target: the endpoint is a bearer capability and must
    not reach logs or UI. Here, not in `api/`, so the worker need not import FastAPI."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


class _KeepEndpointsOutOfHttpxLogs(logging.Filter):
    """httpx logs every request URL, and a push endpoint is a credential: drop the in-flight ones."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_IN_FLIGHT and any(e in record.getMessage() for e in _IN_FLIGHT))


_IN_FLIGHT: set[str] = set()
logging.getLogger("httpx").addFilter(_KeepEndpointsOutOfHttpxLogs())


@dataclass(frozen=True)
class SendResult:
    """`device` is `device_handle`'s hash, never the endpoint."""

    device: str
    status: int | None      # None: the push service never answered (transport failure)
    pruned: bool = False

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """One aes128gcm record (RFC 8291 §3.4, RFC 8188 §2), with a per-message ephemeral key."""
    ua_public = keys.unb64(p256dh)
    # RFC 8291 mixes the raw point into the key: a compressed one derives a key the browser does not,
    # and the phone drops the body while every layer reports success.
    if len(ua_public) != 65 or ua_public[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    as_public = ephemeral.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    # The auth secret salts only this derivation: it is what the push service does not have.
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=keys.unb64(auth),
        info=_KEY_INFO + ua_public + as_public,
    ).derive(ephemeral.exchange(ec.ECDH(), ua_key))

    salt = os.urandom(_SALT_BYTES)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=_CEK_INFO).derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=_NONCE_INFO).derive(ikm)

    # 0x02 delimits the last record. It must fit the declared `rs`, or the phone drops the body while
    # the push service answers 201.
    if len(payload) + _PAD_AND_TAG > _RECORD_SIZE:
        raise ValueError(
            f"a push payload must fit one {_RECORD_SIZE}-byte record; this one needs "
            f"{len(payload) + _PAD_AND_TAG}"
        )
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + _RECORD_SIZE.to_bytes(4, "big") + len(as_public).to_bytes(1, "big") + as_public
    return header + record


def vapid_subject(public_url: str) -> str:
    """RFC 8292's `sub` must be mailto: or https:, and APNs enforces it; an http PUBLIC_URL becomes a
    mailto: on the same host."""
    if public_url.startswith("https://"):
        return public_url.rstrip("/")
    host = urlsplit(public_url).hostname or "localhost"
    return f"mailto:admin@{host}"


def _authorization(vapid: keys.VapidKeys, endpoint: str, subject: str, now: int) -> str:
    """RFC 8292: `aud` is the service's origin, never the endpoint path, which is a bearer capability."""
    origin = urlsplit(endpoint)
    claims = {
        "aud": f"{origin.scheme}://{origin.netloc}",
        "exp": now + _JWT_LIFETIME,
        "sub": subject,
    }
    signed = b".".join(
        keys.b64(json.dumps(part, separators=(",", ":")).encode("utf-8")).encode("ascii")
        for part in ({"typ": "JWT", "alg": "ES256"}, claims)
    )
    token = f"{signed.decode('ascii')}.{keys.b64(vapid.sign(signed))}"
    return f"vapid t={token}, k={vapid.public_key}"


async def _deliver(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    vapid: keys.VapidKeys,
    row: asyncpg.Record,
    payload: bytes,
    subject: str,
) -> SendResult:
    handle = device_handle(row["endpoint"])
    try:
        _IN_FLIGHT.add(row["endpoint"])          # see `_KeepEndpointsOutOfHttpxLogs`
        try:
            response = await client.post(
                row["endpoint"],
                content=_encrypt(payload, row["p256dh"], row["auth"]),
                headers={
                    "Authorization":
                        _authorization(vapid, row["endpoint"], subject, int(time.time())),
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(_TTL),
                },
            )
        finally:
            _IN_FLIGHT.discard(row["endpoint"])
    except Exception as exc:
        # The type only: httpx messages can carry the endpoint URL.
        log.warning("web-push to device %s failed (%s)", handle, type(exc).__name__)
        return SendResult(device=handle, status=None)

    if response.status_code in (404, 410):
        # §4.2: pruned on 404/410, by id, so only this device.
        await conn.execute("DELETE FROM push_subscription WHERE id = $1", row["id"])
        log.info("device %s is gone (%s) — subscription pruned", handle, response.status_code)
        return SendResult(device=handle, status=response.status_code, pruned=True)

    if 200 <= response.status_code < 300:
        await conn.execute(
            "UPDATE push_subscription SET last_seen_ok = now() WHERE id = $1", row["id"]
        )
        return SendResult(device=handle, status=response.status_code)

    log.warning("push service answered %s for device %s", response.status_code, handle)
    return SendResult(device=handle, status=response.status_code)


async def send_to_user(
    conn: asyncpg.Connection,
    user_id: int,
    payload: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[SendResult]:
    """Every device of one member (§4.2 keys on `user_id`). `transport` is injectable for tests."""
    try:
        vapid = await keys.load(conn)
        if vapid is None:
            # A half-configured install is legal (§3.1); the in-app banner still carries the prompt.
            log.info("web-push has no keypair — nothing sent to user %s", user_id)
            return []
        rows = await conn.fetch(
            "SELECT id, endpoint, p256dh, auth FROM push_subscription WHERE user_id = $1 "
            "ORDER BY id",
            user_id,
        )
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        subject = vapid_subject(settings().public_url)
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            return [await _deliver(conn, client, vapid, row, body, subject) for row in rows]
    except Exception:
        # No outcome may reach the caller, which is a lobby (§6.2).
        log.exception("web-push to user %s could not be attempted", user_id)
        return []


__all__ = ["SendResult", "device_handle", "send_to_user"]
