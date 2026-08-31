"""The VAPID keypair, generated at first boot and kept like every other secret. Spec v2.1 §2.

§2: "A web-push VAPID keypair is generated at first boot and stored the same way" — the same
way being §2's DEK: AEAD-sealed under it, with the `key_id` stored beside the ciphertext so
rotation stays possible. `app_setting` was created in migration 0001 for exactly this, and its
comment says so.

Generation is idempotent because the failure of a non-idempotent one is invisible. A browser
subscription is bound to the application server key it was created against: regenerate the
pair on a restart and every registered device keeps its subscription, the push service keeps
answering 201, and not one notification is ever delivered again. Nothing fails anywhere.
"""

from __future__ import annotations

import logging
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field

import asyncpg
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from spielplan.core import secrets

log = logging.getLogger("spielplan.push")

# One row, not one row per half: the pair is meaningless split, and `app_setting` carries the
# public half in `value` and the sealed private half in `secret` on the same key.
SETTING_KEY = "push.vapid"

# P-256 private scalars are fixed-width; the stored form is that width, not the DER envelope,
# so what is sealed is the secret and nothing else.
_SCALAR_BYTES = 32


def b64(raw: bytes) -> str:
    """base64url without padding — how RFC 8291/8292 and `PushSubscription.toJSON()` spell
    every key on the wire."""
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64(text: str) -> bytes:
    """The inverse, tolerant of the padding browsers omit and Firefox sometimes keeps."""
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class VapidKeys:
    """A loaded keypair, offered to callers as a signature and nothing else.

    `public_key` is the uncompressed point in base64url — the form `applicationServerKey` takes
    in the browser and the `k=` parameter takes in RFC 8292's Authorization header. The private
    half is held as a live `cryptography` object with no accessor and no repr: an `api/` route
    can return a string or a dict by accident, and cannot return this by accident at all.
    """

    public_key: str
    _signing_key: ec.EllipticCurvePrivateKey = field(repr=False)

    def sign(self, message: bytes) -> bytes:
        """ES256 over `message`, in JWS's raw r||s form.

        `cryptography` signs into DER, which JWS does not accept (RFC 7515 A.3): a DER
        signature is variable-length and self-describing, and every push service rejects one.
        The conversion is the whole reason this is a method rather than a caller's business.
        """
        r, s = decode_dss_signature(self._signing_key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(_SCALAR_BYTES, "big") + s.to_bytes(_SCALAR_BYTES, "big")


async def ensure_keypair(conn: asyncpg.Connection) -> str | None:
    """Return the household's VAPID public key, generating the pair on first boot.

    Answers None rather than raising when there is no SECRETS_KEY. §3.1 makes a half-configured
    boot a legal state — the app must still serve the wizard the admin needs in order to finish
    configuring it — and §2 forbids the fallback that would make this succeed anyway.
    """
    stored = await public_key(conn)
    if stored is not None:
        return stored

    try:
        key_id, dek = await secrets.ensure_dek(conn)
    except RuntimeError:
        # §2's refusal, not an error: without custody there is nowhere to put the private half.
        log.info("no SECRETS_KEY — web-push is unconfigured; notifications fall back to §6's banner")
        return None

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    scalar = private.private_numbers().private_value.to_bytes(_SCALAR_BYTES, "big")
    # DO NOTHING rather than DO UPDATE: two processes booting against one database must end up
    # with one pair, and the loser of the race must adopt the winner's rather than overwrite it
    # — see the module docstring for what overwriting costs.
    await conn.execute(
        """
        INSERT INTO app_setting (key, value, secret, secret_key_id)
        VALUES ($1, $2, $3, $4) ON CONFLICT (key) DO NOTHING
        """,
        SETTING_KEY,
        {"public_key": b64(public)},
        secrets.seal(dek, {"private_key": b64(scalar)}),
        key_id,
    )
    return await public_key(conn)


async def public_key(conn: asyncpg.Connection) -> str | None:
    """The application server key the browser needs at subscribe time, or None if unconfigured.

    Reads; never generates. The subscribe screen asks this on every load, and a route that
    could mint a keypair is a route that mints one whenever the boot-time call did not run.
    """
    value = await conn.fetchval("SELECT value FROM app_setting WHERE key = $1", SETTING_KEY)
    return (value or {}).get("public_key")


async def load(conn: asyncpg.Connection) -> VapidKeys | None:
    """The signing half, for `send`. None when the pair is absent or its DEK is unreadable.

    Unreadable is a real state — a restored database against a rotated SECRETS_KEY — and §6's
    preamble makes it a lost notification rather than a failed session.
    """
    row = await conn.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = $1", SETTING_KEY
    )
    if row is None or row["secret"] is None:
        return None
    try:
        dek = await secrets.load_dek(conn, row["secret_key_id"])
        sealed = secrets.open_sealed(dek, row["secret"])
    except Exception:
        log.warning("the stored VAPID private key cannot be decrypted — web-push is disabled")
        return None
    scalar = int.from_bytes(unb64(sealed["private_key"]), "big")
    return VapidKeys(row["value"]["public_key"], ec.derive_private_key(scalar, ec.SECP256R1()))


__all__ = ["SETTING_KEY", "VapidKeys", "b64", "ensure_keypair", "load", "public_key", "unb64"]
