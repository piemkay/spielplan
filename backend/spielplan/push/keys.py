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
from cryptography.exceptions import InvalidTag
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


async def _rebind_to_its_row(conn: asyncpg.Connection, row: asyncpg.Record) -> None:
    """Re-seal a private half written before sec-10's binding, on the first boot that sees it.

    `core/secrets.open_sealed` opens a pre-M4.7 ciphertext by retrying with no associated data,
    and its comment calls that a migration-era branch to be deleted "once no install predates
    M4.7" because the row is "re-sealed with it by the next save through `put_connector_secrets`
    / `ensure_keypair`". For the VAPID pair that promise was empty: `ensure_keypair` had no save
    path for an existing row at all, so an install carrying an unbound pair would carry it for
    ever and the branch could never be removed. This is that save. [M4.7 sec-10; decision 181]

    Silent on custody, deliberately: an unreadable DEK is a real operator state (§2's restored
    dump without its `.env`) and the pair comes back whole the moment the right key does, so a
    boot must not touch a ciphertext it cannot read.
    """
    try:
        dek = await secrets.load_dek(conn, row["secret_key_id"])
    except secrets.SecretsUnreadable:
        return
    aad = secrets.aad_for("app_setting", SETTING_KEY)
    try:
        # The one question `open_sealed` cannot be asked directly: it falls back to `None` on
        # the caller's behalf, so opening *without* the AAD is what identifies a row that has
        # not been bound yet. A row already carrying it fails here and is left alone.
        sealed = secrets.open_sealed(dek, row["secret"], None)
    except InvalidTag:
        return
    await conn.execute(
        # `secret = $3` rather than the key alone: a concurrent `spielplan-secrets reset` plus a
        # second boot could have replaced the pair between the read above and this write, and
        # storing the re-sealed *old* private half beside the *new* public one would produce a
        # row whose two halves are not a pair — the one failure this module exists to prevent.
        "UPDATE app_setting SET secret = $2, updated_at = now() WHERE key = $1 AND secret = $3",
        SETTING_KEY,
        secrets.seal(dek, sealed, aad),
        row["secret"],
    )


async def ensure_keypair(conn: asyncpg.Connection) -> str | None:
    """Return the household's VAPID public key, generating the pair on first boot.

    Answers None rather than raising when there is no SECRETS_KEY. §3.1 makes a half-configured
    boot a legal state — the app must still serve the wizard the admin needs in order to finish
    configuring it — and §2 forbids the fallback that would make this succeed anyway.

    **The sealed half decides whether a pair exists, not the public one.** This used to
    short-circuit on the stored `value`, which is the half that is worth nothing alone, and M4.7
    turned that from a theoretical gap into a permanent one: `spielplan-secrets reset` cleared
    the private half of every row it could not open, so no later boot ever minted a replacement,
    `load()` answered None for ever, and `/api/push/state` went on handing browsers a key nothing
    could sign for — precisely the silent failure the module docstring describes. `reset` now
    deletes the row (`core/secrets_cli`), and this reads the column that says whether a private
    half is there, so a pair broken by any other route is repaired at the next boot too.
    [M4.7 dd03; decision 181]
    """
    row = await conn.fetchrow(
        "SELECT value, secret, secret_key_id FROM app_setting WHERE key = $1", SETTING_KEY
    )
    if row is not None and row["secret"] is not None:
        await _rebind_to_its_row(conn, row)
        return (row["value"] or {}).get("public_key")

    try:
        key_id, dek = await secrets.ensure_dek(conn)
    except RuntimeError:
        # §2's refusal, not an error: without custody there is nowhere to put the private half.
        log.info("no SECRETS_KEY — web-push is unconfigured; notifications fall back to §6's banner")
        return None

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    scalar = private.private_numbers().private_value.to_bytes(_SCALAR_BYTES, "big")
    # DO UPDATE under a WHERE rather than DO NOTHING, and the WHERE is the whole safety property:
    # two processes booting against one database must end up with one pair, so a row that still
    # holds a sealed private half is never overwritten (see the module docstring for what
    # overwriting costs) and the loser of that race adopts the winner's exactly as before, while
    # a row whose private half is gone is not a pair at all and must be replaced rather than
    # preserved.
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
        # sec-10: bound to the row that holds it, like every connector secret.
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
    """The application server key the browser needs at subscribe time, or None if unconfigured.

    Reads; never generates. The subscribe screen asks this on every load, and a route that
    could mint a keypair is a route that mints one whenever the boot-time call did not run.

    A row whose sealed half is gone is not a pair, and the `secret IS NOT NULL` is what makes
    this say so: `load` has always answered None for such a row, while this answered with the
    public half — so the browser subscribed against a key `push/send.py` could not sign for, the
    push service kept returning 201 and no notification was ever delivered again. The two
    readers of this row now give the same answer. [M4.7 dd03]
    """
    value = await conn.fetchval(
        "SELECT value FROM app_setting WHERE key = $1 AND secret IS NOT NULL", SETTING_KEY
    )
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
        sealed = secrets.open_sealed(dek, row["secret"], secrets.aad_for("app_setting", SETTING_KEY))
    except Exception:
        log.warning("the stored VAPID private key cannot be decrypted — web-push is disabled")
        return None
    scalar = int.from_bytes(unb64(sealed["private_key"]), "big")
    return VapidKeys(row["value"]["public_key"], ec.derive_private_key(scalar, ec.SECP256R1()))


__all__ = ["SETTING_KEY", "VapidKeys", "b64", "ensure_keypair", "load", "public_key", "unb64"]
