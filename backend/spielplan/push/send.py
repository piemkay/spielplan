"""Sending one web-push message. Spec v2.1 §4.2, §6 preamble, §6.2 step 2, §7.3, §12 (M4).

Two RFCs, both implemented here against `cryptography` and `httpx` rather than pulled in as
libraries (see the package docstring):

  * **RFC 8291** — the payload is encrypted end to end for the subscription's own keys, so the
    push service forwarding it learns nothing. ECDH against the browser's `p256dh` with a
    fresh ephemeral key, HKDF salted by the subscription's `auth` secret, one aes128gcm record.
  * **RFC 8292** — the request is signed with the household's VAPID key so the push service can
    tell one application server from another: `Authorization: vapid t=<JWT>, k=<public key>`.

The three delivery rules are §4.2's, verbatim ("pruned on 404/410 from the push service"):

  * **404 or 410 deletes exactly that row.** Those two codes alone mean the endpoint is
    permanently gone. The row is deleted by `id`, so the member's other phone — a different
    row, a different endpoint, possibly a different push service — is untouched.
  * **Every other outcome leaves the row.** A 500, a 429 or a timeout is the push service
    having a bad minute; pruning on those deletes a live phone over a transient outage.
  * **A delivery stamps `last_seen_ok`**, which is what the worker's 90-day sweep reads.

And nothing here raises. §6's preamble: push is best-effort, with an in-app banner behind every
prompt, so a failed send is a returned result and never an exception in a lobby.
"""

from __future__ import annotations

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

from spielplan.api.push import device_handle
from spielplan.core.config import settings
from spielplan.push import keys

log = logging.getLogger("spielplan.push")

# RFC 8291 §3.1's fixed strings and widths. Spelled out rather than derived: they are wire
# constants, and a typo in one produces a body the phone silently drops.
_KEY_INFO = b"WebPush: info\x00"
_CEK_INFO = b"Content-Encoding: aes128gcm\x00"
_NONCE_INFO = b"Content-Encoding: nonce\x00"
_SALT_BYTES = 16

# One padding delimiter plus the AES-GCM tag: what a record costs beyond its plaintext.
_PAD_AND_TAG = 1 + 16
_RECORD_SIZE = 4096

# RFC 8292 caps the JWT at 24 h. Twelve keeps a clock skewed by an hour in either direction
# inside the window without pretending the token is a long-lived credential.
_JWT_LIFETIME = 12 * 3600

# RFC 8030's TTL, in seconds. A push-carried §6.2 invitation to a lobby that closed at nine is
# noise the next morning, and §6 preamble's in-app banner is the channel that keeps.
_TTL = 3600

# The send is inline in whatever opened the session. §6's preamble means the answer changes
# nothing the caller does, so a push service that has stopped answering must stop being waited
# on quickly rather than hold up the lobby it is announcing.
_TIMEOUT = 10.0


class _KeepEndpointsOutOfHttpxLogs(logging.Filter):
    """§4.2's endpoint is a bearer capability, and httpx writes it into the log by itself.

    `httpx` logs one INFO line per request with the full URL in it. For Jellyfin that is a
    useful trace; for a push endpoint it copies a credential — anyone holding it can push to
    that phone — into the household's log on every notification. There is no per-client switch
    for that line, so it is dropped here, and only ever for an endpoint this module is
    delivering to at that instant: every other httpx caller's trace survives untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_IN_FLIGHT and any(e in record.getMessage() for e in _IN_FLIGHT))


_IN_FLIGHT: set[str] = set()
logging.getLogger("httpx").addFilter(_KeepEndpointsOutOfHttpxLogs())


@dataclass(frozen=True)
class SendResult:
    """What became of one device's copy.

    `device` is `api/push.py`'s hash handle, never the endpoint: the endpoint is a bearer
    capability, and a result object ends up in logs and admin views.
    """

    device: str
    status: int | None      # None: the push service never answered (transport failure)
    pruned: bool = False

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """One aes128gcm record for this subscription's keys (RFC 8291 §3.4, RFC 8188 §2).

    The ephemeral keypair is per message on purpose: it is what makes the shared secret — and
    therefore the content-encryption key — different for every push, so two messages to the
    same phone reveal nothing about each other.
    """
    ua_public = keys.unb64(p256dh)
    # RFC 8291 §3.4 assumes the uncompressed form on both sides: `key_info` mixes in the raw
    # point, so a 33-byte compressed one derives a different IKM from the one the browser
    # derives, and the phone gets a body it cannot read while every layer reports success. No
    # shipping browser sends one — but `POST /api/push/subscribe` accepts any 256-char string,
    # so a hand-rolled subscription reaches here.
    if len(ua_public) != 65 or ua_public[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    as_public = ephemeral.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    # The auth secret salts this first derivation and nothing else: it is what a push service
    # holding the endpoint does not have, and therefore what it cannot forge a message with.
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=keys.unb64(auth),
        info=_KEY_INFO + ua_public + as_public,
    ).derive(ephemeral.exchange(ec.ECDH(), ua_key))

    salt = os.urandom(_SALT_BYTES)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=_CEK_INFO).derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=_NONCE_INFO).derive(ikm)

    # RFC 8188 §2: the record's padding delimiter, 0x02 because this is the last (and only) one.
    # And the record has to fit the `rs` the header declares: past that a decoder splits
    # the body at `rs`, decrypts a fragment and fails the tag — while a push service that
    # does not enforce the limit answers 201, so every layer here reports a delivery the
    # phone silently dropped. `_deliver` turns this into a logged, un-pruned failure.
    if len(payload) + _PAD_AND_TAG > _RECORD_SIZE:
        raise ValueError(
            f"a push payload must fit one {_RECORD_SIZE}-byte record; this one needs "
            f"{len(payload) + _PAD_AND_TAG}"
        )
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + _RECORD_SIZE.to_bytes(4, "big") + len(as_public).to_bytes(1, "big") + as_public
    return header + record


def vapid_subject(public_url: str) -> str:
    """RFC 8292's `sub`: how a push service operator reaches whoever runs this application
    server. The RFC sanctions a `mailto:` or an **https:** URI, and §2 puts this app behind
    "one plain-HTTP port" with the operator's Traefik in front — so `PUBLIC_URL` is `http://…`
    on a LAN or Tailscale install, which is neither.

    Apple's APNs is the service that documents and enforces this, and §6's preamble makes the
    iPhone the form factor push exists for; a rejected `sub` there is a 403 on every delivery,
    an un-pruned row, one log line, and a household that simply never gets a notification —
    exactly the "nothing visibly failing" mode `api/push.py`'s docstring was written against.
    So an https URL is used as-is and anything else becomes a mailto: derived from the same
    host, which every service accepts.
    """
    if public_url.startswith("https://"):
        return public_url.rstrip("/")
    host = urlsplit(public_url).hostname or "localhost"
    return f"mailto:admin@{host}"


def _authorization(vapid: keys.VapidKeys, endpoint: str, subject: str, now: int) -> str:
    """RFC 8292 §2/§4: the signed JWT and the public key that verifies it, in one header.

    `aud` is the push service's origin and not the endpoint path — the endpoint is a bearer
    capability and the JWT travels to exactly one service, so scoping the audience any tighter
    would leak the capability into a token the service logs.
    """
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
        # The type, never the exception: httpx puts the request URL in several of its messages,
        # and that URL is the bearer capability §4.2's keys are guarded as.
        log.warning("web-push to device %s failed (%s)", handle, type(exc).__name__)
        return SendResult(device=handle, status=None)

    if response.status_code in (404, 410):
        # §4.2: "pruned on 404/410 from the push service". By id, so this is the one device.
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
    """Deliver one notification to every device of one member, and report what happened.

    Scoped to `user_id` because §4.2 keys the table on it and §7.3's prompt is per-user: the
    household has no shared notification, and a query without that clause sends Jenny's "did
    you finish X?" to Patrick's phone.

    `transport` is injected the way `connectors/jellyfin.py` injects it — so tests exercise the
    real request building without a socket, rather than asserting that we call ourselves.
    """
    try:
        vapid = await keys.load(conn)
        if vapid is None:
            # §3.1: a half-configured install is legal, and §6's preamble already promises the
            # member a banner. Nothing to send with is a logged fact, not a failure.
            log.info("web-push has no keypair — nothing sent to user %s", user_id)
            return []
        rows = await conn.fetch(
            "SELECT id, endpoint, p256dh, auth FROM push_subscription WHERE user_id = $1 "
            "ORDER BY id",   # a stable order, so a partial failure reads the same twice
            user_id,
        )
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        subject = vapid_subject(settings().public_url)
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            return [await _deliver(conn, client, vapid, row, body, subject) for row in rows]
    except Exception:
        # The outer net, for everything that is not one device's delivery: no send outcome and
        # no accident around one may reach the caller, because the caller is a lobby (§6.2).
        log.exception("web-push to user %s could not be attempted", user_id)
        return []


__all__ = ["SendResult", "send_to_user"]
