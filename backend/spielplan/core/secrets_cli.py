"""`spielplan-secrets`: the operator's two custody commands (§2, §14.3). Commands, not routes: both
need the old key, which no running process holds. `rewrap` is lossless; `reset` is lossy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from spielplan.core import secrets
from spielplan.core.config import _MIN_SECRET_CHARS, settings

# `core/config`'s floor restated as one line of a refusal; the constant is imported so they cannot drift.
_KEY_FLOOR = (
    f"SECRETS_KEY must be at least {_MIN_SECRET_CHARS} characters; generate one with: "
    'python -c "import secrets;print(secrets.token_urlsafe(32))"'
)


async def _connect() -> asyncpg.Connection:
    """One connection, not the pool: a single session that owns its transaction."""
    return await asyncpg.connect(settings().database_url)


async def _rewrap(old_key: str, new_key: str) -> int:
    """`FOR UPDATE`, so a concurrent `ensure_dek` cannot mint a replacement between read and write."""
    # Only `--new-key` meets the floor: a short `--old-key` is exactly the install this must rescue.
    if len(new_key) < _MIN_SECRET_CHARS:
        print(f"refusing: {_KEY_FLOOR}")
        return 1
    conn = await _connect()
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT key_id, wrapped_dek FROM data_encryption_key "
                "WHERE retired_at IS NULL ORDER BY created_at FOR UPDATE"
            )
            if not rows:
                print("nothing to rewrap: no active data-encryption key row.")
                return 1
            if len(rows) > 1:
                # Unreachable once 0017_ops.sql is applied; older installs may still hold two rows.
                names = ", ".join(r["key_id"] for r in rows)
                print(
                    f"refusing: {len(rows)} un-retired data-encryption key rows ({names}). "
                    "Spec section 2 speaks of the one DEK row. Decide which is current and "
                    "stamp retired_at on the others, then re-run."
                )
                return 1
            row = rows[0]
            try:
                rewrapped = secrets.rewrap_dek(bytes(row["wrapped_dek"]), old_key, new_key)
            except secrets.SecretsUnreadable as exc:
                print(f"refusing: {exc}")
                return 1
            await conn.execute(
                "UPDATE data_encryption_key SET wrapped_dek = $2 WHERE key_id = $1",
                row["key_id"],
                rewrapped,
            )
        print(
            f"rewrapped data-encryption key {row['key_id']} under the new SECRETS_KEY. "
            "Every stored ciphertext is unchanged. Put the new value in .env and restart."
        )
        return 0
    finally:
        await conn.close()


async def _reset() -> int:
    """Retire every DEK row this SECRETS_KEY cannot open (active, or retired but still named) and clear
    what named it. Destructive, so it reports each row it empties."""
    conn = await _connect()
    try:
        secrets_key = settings().secrets_key
        if not secrets_key:
            print(
                "refusing: SECRETS_KEY is not set, so this cannot tell an unreadable row from a "
                "readable one. Set it to the value the install should use from now on."
            )
            return 1
        lost: list[str] = []
        async with conn.transaction():
            unreadable = await secrets.unreadable_key_ids(conn)
            if not unreadable:
                print(
                    "nothing to reset: this SECRETS_KEY opens the active data-encryption key row "
                    "and every row a stored secret names. Custody is intact."
                )
                return 0
            # Locked, so a concurrent Connectors-card repair cannot have its fresh ciphertext cleared.
            await conn.fetch(
                "SELECT key_id FROM data_encryption_key WHERE key_id = ANY($1::text[]) FOR UPDATE",
                unreadable,
            )
            for name in await conn.fetch(
                "UPDATE connector_config SET secrets_encrypted = NULL, secrets_key_id = NULL, "
                "updated_at = now() WHERE secrets_key_id = ANY($1::text[]) RETURNING name",
                unreadable,
            ):
                lost.append(f"connector_config/{name['name']}")
            # DELETE, not clear: the VAPID pair's halves are meaningless apart, and a missing row makes the
            # next boot mint a fresh pair.
            for key in await conn.fetch(
                "DELETE FROM app_setting WHERE secret_key_id = ANY($1::text[]) RETURNING key",
                unreadable,
            ):
                lost.append(f"app_setting/{key['key']}")
            # Last, so a failure above rolls back before any row retires. `retired_at IS NULL` keeps an
            # earlier retirement's date.
            retired = [
                row["key_id"]
                for row in await conn.fetch(
                    "UPDATE data_encryption_key SET retired_at = now() "
                    "WHERE key_id = ANY($1::text[]) AND retired_at IS NULL RETURNING key_id",
                    unreadable,
                )
            ]
        print(f"unreadable data-encryption key(s): {', '.join(unreadable)}")
        if retired:
            print(f"retired now: {', '.join(retired)}")
        if lost:
            print("cleared the secrets these rows held (they were already unrecoverable):")
            for item in lost:
                print(f"  - {item}")
            print(
                "Re-enter the connector credentials in Admin > Connectors. If app_setting/"
                "push.vapid is listed, restart the app: the next boot mints a fresh keypair, "
                "web-push stays off until it does, and every device must subscribe again."
            )
        else:
            print("no ciphertext named those keys, so nothing was cleared.")
        return 0
    finally:
        await conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spielplan-secrets",
        description="SECRETS_KEY custody: rotate the wrapping, or reset what cannot be opened.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rewrap = sub.add_parser(
        "rewrap",
        help="re-wrap the one DEK row under a new SECRETS_KEY, losing nothing",
    )
    rewrap.add_argument("--old-key", required=True, help="the SECRETS_KEY in use today")
    rewrap.add_argument("--new-key", required=True, help="the SECRETS_KEY to move to")

    sub.add_parser(
        "reset",
        help="retire DEK rows this SECRETS_KEY cannot open and clear the ciphertexts naming them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "rewrap":
        return asyncio.run(_rewrap(args.old_key, args.new_key))
    return asyncio.run(_reset())


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    sys.exit(main())
