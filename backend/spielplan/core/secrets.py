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
    """The stored DEK will not open under the SECRETS_KEY this process holds.

    A real operator state, not a bug: it is what a restored dump looks like when the `.env`
    beside it was not restored too, or when SECRETS_KEY was regenerated. Until M4.7 the
    `InvalidTag` underneath escaped every caller — `app.py`'s only handler is for
    `asyncpg.PostgresError` — so the seen-state write, the verdict, the not-seen, the finish
    prompt and both admin connector routes answered 500, *including the PUT that re-enters the
    key*. §3.1 makes a half-configured boot a legal state and §3.3 makes the app-side write
    independent of Jellyfin; a 500 contradicts both.

    **RuntimeError, deliberately.** `push/keys.ensure_keypair` already catches `RuntimeError`
    around `ensure_dek` for §2's no-SECRETS_KEY refusal, so subclassing it also fixes the branch
    where a database holding a DEK row but no VAPID row made the container fail to start with a
    traceback that never named SECRETS_KEY. `Settings.require_secrets_key` keeps raising its own
    plain `RuntimeError`, and every existing `except RuntimeError` keeps its meaning.
    [M4.7 dd03, sec-10; decision 181]
    """

    def __init__(self, detail: str, key_id: str | None = None) -> None:
        super().__init__(detail)
        self.key_id = key_id


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


def _unwrap(blob: bytes, secrets_key: str, *, key_id: str | None = None) -> bytes:
    """Open a wrapped DEK, or say why not in the one word the operator can act on.

    The `InvalidTag` this replaces carried no text at all, which is how a wrong SECRETS_KEY
    reached a member's phone as "database error" (dd03). `key_id` is optional because
    `rewrap_dek` unwraps a blob the caller already holds and has no row in hand.
    """
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
    """The `key_id` of the one un-retired row, or None on an install that has never sealed.

    Read-only and here rather than in `app.py`, because the boot probe must be able to ask "is
    the stored key still openable" without minting one as a side effect — `ensure_dek` would.
    0017_ops.sql's partial unique index is what makes "the one" a fact rather than an ordering
    convention; the ORDER BY is what keeps this answerable on a database that predates it.
    """
    return await conn.fetchval(
        "SELECT key_id FROM data_encryption_key WHERE retired_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )


async def retire_dek(conn: asyncpg.Connection, key_id: str) -> None:
    """Stamp `retired_at` on one row, named by id, so a fresh one can be minted.

    Retiring loses nothing that was readable: `load_dek` finds a row by id whether it is retired
    or not, so if the correct `.env` ever comes back, every ciphertext naming this key still
    opens. What it does is free 0017's partial unique index, which is the only way `ensure_dek`
    can mint a replacement.

    By id rather than "whichever row is active", because the caller is repairing a row it has
    just failed to open and another process holding the same env may have repaired it in between:
    retiring *that* row would push a key this SECRETS_KEY can read aside and mint a third.
    """
    await conn.execute(
        "UPDATE data_encryption_key SET retired_at = now() "
        "WHERE key_id = $1 AND retired_at IS NULL",
        key_id,
    )


async def ensure_dek(
    conn: asyncpg.Connection, *, retire_unreadable: bool = False
) -> tuple[str, bytes]:
    """Return the active (key_id, dek), creating it on first boot.

    `retire_unreadable` is a caller saying "I am an explicit admin write of a credential someone
    just typed, so if the stored row will not open, retire it and mint one I can seal under". It
    is the caller's word and not this module's judgement because the same unreadable row means
    two opposite things: to the boot-time env seed it means *skip* — spending an admin's one
    repair unattended, at every restart, is how a restored dump loses the ciphertexts its `.env`
    would still have opened — while to the Connectors card's PUT and the wizard's connector route
    it means the admin is re-entering the credential, which *is* the repair.

    M4.7 derived that instead from "did the read report `secrets_unreadable`", which only a row
    that already holds a ciphertext can report. On an install whose DEK was minted for the VAPID
    pair alone the flag was False, this function raised, and the 500 came out of the very PUT
    that repairs custody — with no `{"detail": ...}` on it, because `app.py` has no handler for
    this type. [M4.7 dd03; decision 181]
    """
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

    # DO NOTHING then re-SELECT, adopting the winner — exactly what `push/keys.ensure_keypair`
    # does for the VAPID pair (push/keys.py:98-107) and for the same reason. This was
    # SELECT-then-INSERT with no lock, and three concurrent first boots on separate connections
    # left two active rows: harmless for reads, because every ciphertext names its key_id, but §2
    # says "the one DEK row" and both executors this milestone adds assume exactly one. The
    # conflict target is left off so this catches the primary key and 0017's partial unique index
    # alike; the loser blocks on the winner's insert, then reads the row the winner committed.
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
    """Load a specific DEK by id — used when decrypting older ciphertexts after rotation.

    Both refusals are `SecretsUnreadable` because this is the *read* path, and from the caller's
    side "there is no key in the environment" and "the key in the environment is the wrong one"
    are one fact: the stored secret will not open. `ensure_dek` keeps §2's bare refusal, because
    a write with no custody has nowhere to put the plaintext and must stop rather than degrade.
    """
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
    """Every data-encryption key this SECRETS_KEY cannot open that something still points at.

    One function because there is one question, and its two askers are the surface that
    *reports* custody (§6.6's System card) and the command that *repairs* it
    (`spielplan-secrets reset`) — so a disagreement between them is a card that says the install
    is broken beside a command that says there is nothing to do. They disagreed: the card asked
    this, `reset` asked "which *active* row will not open", and M4.7's own Connectors-card repair
    drives the two apart permanently. Pasting the API key retires the unreadable row and seals
    under a fresh one (`connectors.save_jellyfin`), so the active row opens again while the tmdb
    key and `app_setting/push.vapid` are still sealed under the retired one — and `reset`, the
    repair the card's own advice sends the operator to next, became a no-op on exactly the
    install it exists for. `/api/push/state` then hands browsers an application server key
    nothing can sign for, for ever, which is the failure `push/keys`' docstring is written
    against.

    Un-retired rows stay in the set even when nothing names them yet: an install whose DEK will
    not open has broken custody whether or not it has sealed anything, and the next write is what
    would discover it. All of them, not `active_key_id`'s newest one — an install that lost
    sec-10's race before 0017 existed carries two, and this is the question `spielplan-secrets
    reset` uses to clear that state. Retired rows are in the set when a ciphertext names one,
    because a retired row is not a dead row: it is the row those ciphertexts still open under, if
    the right `.env` comes back. [M4.7 dd03, ops-11, sec-10; decision 182]
    """
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
            # A ciphertext naming a key row that is *gone* answers here too (`load_dek` raises
            # the same type for it): a dump restored without `data_encryption_key` is exactly
            # that state, and it is unrecoverable rather than merely locked.
            await load_dek(conn, key_id)
        except SecretsUnreadable:
            unreadable.append(key_id)
    return unreadable


def aad_for(table: str, row: str) -> bytes:
    """The associated data every stored secret is bound to: the table and row that hold it.

    §14 risk 3 makes a leaked Jellyfin key admin-equivalent on the whole media server, and until
    M4.7 `seal`/`open_sealed` passed `None` while the DEK wrap above used `_INFO` — so a
    ciphertext was bound to nothing. Copying `jellyfin`'s `secrets_encrypted` and `secrets_key_id`
    into the `tmdb` row made `get_connector_secrets('tmdb')` hand back the Jellyfin admin key,
    with every CHECK constraint satisfied. Naming the row in the AEAD's associated data makes that
    move fail to open instead. [M4.7 sec-10]
    """
    return f"{table}/{row}".encode()


def seal(dek: bytes, payload: dict[str, Any], aad: bytes | None = None) -> bytes:
    aes = AESGCM(dek)
    nonce = os.urandom(_NONCE)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return nonce + aes.encrypt(nonce, raw, aad)


def _open_with_fallback(
    dek: bytes, blob: bytes, aad: bytes | None
) -> tuple[dict[str, Any], bool]:
    """Open a sealed payload, and say whether it was bound to the row it came out of.

    `bound` is False only for a ciphertext written before M4.7 gave every stored secret its
    associated data, and it is *reported* rather than swallowed because only the caller — the one
    holding the connection and the DEK — can close the gap. The fallback's first comment claimed
    it "cannot weaken the binding above", which was true of blobs sealed after the change and of
    nothing else: an unbound blob opens in whatever row it is copied into, which is sec-10's
    reproduction verbatim, and it stays that way until something re-seals it. So the two callers
    that can, do — `_rebind_connector_row` below and `push/keys._rebind_to_its_row` — and the
    branch is migration-era in fact rather than only in its comment. Delete it once no install
    predates M4.7. [M4.7 sec-10]
    """
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
    """Re-seal a connector secret written before the binding, on the first read that opens it.

    On the read and not on the next save, because a connector row has three writers — the env
    seed (first boot only), the wizard's route and an admin save — so a household that never
    reopens the Connectors card would carry an unbound ciphertext for as long as the install
    lives, and `_open_with_fallback`'s branch could never be deleted. The DEK is already in hand
    here, which is what makes it cost nothing. `push/keys._rebind_to_its_row` is the same repair
    for the VAPID pair; this is the other half of the sentence its comment promises.

    `updated_at` is deliberately not bumped: nothing about the household's configuration changed,
    and that column is what §6.6's Connectors card would show as the moment an admin last saved.
    [M4.7 sec-10]
    """
    await conn.execute(
        # `secrets_encrypted = $3` is the same guard `push/keys` uses: a save that landed between
        # the read above and this write must not be overwritten with the old plaintext re-sealed,
        # which would silently restore a credential the admin had just replaced.
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
    """Write a connector's non-secret half, leaving its sealed columns exactly as they are.

    `put_connector_secrets(..., secrets=None)` means "this connector has no secret" and NULLs
    both sealed columns, which is correct when the caller read the old secret and carried it
    forward. It is destructive when the caller *could not* read it: an admin correcting the URL
    on an install whose DEK will not open would erase a ciphertext that is only unreadable until
    the right `.env` comes back. One statement, so the two meanings stop being one call.
    [M4.7 dd03]
    """
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
        # The DEK opened, so custody is fine and the *ciphertext* is the problem: a blob moved
        # between rows (`aad_for`) or a tampered one. One type out of this module, because
        # `registry.load_jellyfin` degrades on one type and a bare `InvalidTag` here would 500
        # the same routes dd03 is about.
        raise SecretsUnreadable(
            f"the stored secret for connector {name!r} does not open under its own key_id "
            f"({row['secrets_key_id']}) - the ciphertext does not belong to this row",
            row["secrets_key_id"],
        ) from exc
    if not bound:
        await _rebind_connector_row(conn, name, row["secrets_encrypted"], dek, opened)
    return config, opened


def rewrap_dek(wrapped: bytes, old_key: str, new_key: str) -> bytes:
    """Admin action: re-wrap the one DEK row under a new SECRETS_KEY."""
    return _wrap(_unwrap(wrapped, old_key), new_key)


# Domain-separated, so this value is *this app's* fingerprint of the key rather than the digest
# any other tool would print for the same string — a fingerprint an operator might paste into a
# ticket must not double as a lookup key somewhere else.
_FINGERPRINT_INFO = b"spielplan/secrets-key-fingerprint/v1"


def key_fingerprint(secrets_key: str) -> str:
    """A short, stable identifier for the SECRETS_KEY this process holds. Never the key.

    §6.6's System card has one question to settle about custody, and decision 182 gives it two
    facts to settle it with: this and the active `key_id`. The question is "is the .env I just
    restored the one that was current when the dump was taken" — which a twelve-character
    comparison answers and which nothing else in the app can answer at all, because the key is
    never stored and every ciphertext names only the DEK's id.

    Twelve hex characters is 48 bits: enough to tell two keys apart by eye, and far too few to
    reconstruct one that `Settings`' validator already refuses below 32 characters (§14.4). The
    stronger reason for a digest at all is §14.3 — SECRETS_KEY wraps the DEK that seals a
    Jellyfin API key the spec calls "unscoped and admin-equivalent", so an admin route may prove
    *which* key is loaded and must never prove what it is. [M4.7 ops-11; decision 182]
    """
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
