"""`spielplan-secrets` — the operator's two custody actions. Spec v2.1 §2, §14.3.

§2 has promised one of them since M0: "rotating `SECRETS_KEY` is an explicit admin action that
re-wraps the one DEK row", repeated by `.env.example` and certified by a green coverage row whose
only implementation was `core/secrets.rewrap_dek`, a pure function whose sole caller was a unit
test. An operator who followed `.env.example` and edited `SECRETS_KEY` landed in dd03 instead: a
green boot and a 500 on every member write.

**An operator command and not an HTTP route.** §2 calls rotation an explicit admin action, and
both halves need the *old* key, which a running process does not have — `rewrap` is exactly the
gesture "I am replacing the value in .env" and there is nowhere in the app to type the value being
replaced. `reset` is worse still as a route: it destroys secrets, and the account that would call
it may itself be unreachable on an install whose custody is broken.

Two subcommands, and they are not alternatives:

  * `rewrap --old-key --new-key` is the lossless one. The DEK never changes, so every ciphertext
    and every `key_id` stays exactly as it was; only the wrapping moves. Both keys are arguments
    and neither is read from the environment, so the order is: run this while both values are in
    hand, then put the new one in `.env` and restart.
  * `reset` is the lossy one, for the install that is already past that: a restored dump whose
    `.env` was not restored, or a regenerated `SECRETS_KEY`. It retires what it cannot unwrap and
    clears the ciphertexts that named it, so §6.6's Connectors card can be filled in again. It
    prints what was lost before it is gone.

[M4.7 spec-08, tq3-secrets-rotation-row, dd03; decision 181]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from spielplan.core import secrets
from spielplan.core.config import _MIN_SECRET_CHARS, settings

# The floor `core/config`'s validator holds, and its message, said again where a *new* key is
# written. Importing the constant rather than restating the number is what keeps the two from
# drifting; the sentence is restated because the validator's arrives inside a pydantic
# `ValidationError` and this one has to read as one line of a command's refusal. cs-44 put the
# floor on the app and left the one executor that writes a SECRETS_KEY without it, so an operator
# who followed this command's own success message ended up with an app that refuses to boot and a
# CLI that refuses to run - both with the same error, and a DEK now wrapped under the key neither
# will accept. [M4.7 cs-44, spec-08]
_KEY_FLOOR = (
    f"SECRETS_KEY must be at least {_MIN_SECRET_CHARS} characters; generate one with: "
    'python -c "import secrets;print(secrets.token_urlsafe(32))"'
)


async def _connect() -> asyncpg.Connection:
    """One connection, not `db/pool`.

    This runs as `docker compose exec backend spielplan-secrets ...` beside a live app, or on a
    stopped stack during a restore. It needs a single session that owns its transaction; opening
    the process-wide pool would be a second, longer-lived thing to close correctly for no gain.
    """
    return await asyncpg.connect(settings().database_url)


async def _rewrap(old_key: str, new_key: str) -> int:
    """Re-wrap the one un-retired DEK row. Ciphertexts and key_ids are untouched by construction.

    One transaction with `FOR UPDATE` on the row, because the loop is read-modify-write over the
    only row that matters and a concurrent `ensure_dek` minting a replacement between the read and
    the write would leave a row wrapped under a key nobody records.
    """
    # Before the connection, let alone the UPDATE: the refusal is about the argument, and an
    # operator who has to be told this should be told it before anything was written. Only
    # `--new-key` is held to the floor — the value being written is the one the app will have to
    # boot with, while `--old-key` is the value being *replaced*, and an install that ran with a
    # short key is precisely the install cs-44's floor strands. Rewrapping away from it is that
    # install's only door out, so a length refusal on `--old-key` would close it. A wrong
    # `--old-key` is already refused by the unwrap below, in the message that names SECRETS_KEY.
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
                # 0017_ops.sql makes this unreachable on a database that has applied it; an
                # install that lost sec-10's race before the index existed can still be here, and
                # guessing which row is "the one" is exactly the decision an operator must take.
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
    """Retire every DEK row this SECRETS_KEY cannot open, and clear what named it.

    Without this there is no way back on an install whose DEK is unreadable and whose SECRETS_KEY
    was regenerated: `ensure_dek` unwraps the active row before it can seal anything, so even
    re-entering the credentials fails. The clearing is the point and it is destructive, so it
    reports every row it empties or removes by name.

    **Every row a ciphertext names, retired or not** — `secrets.unreadable_key_ids`, the same
    question §6.6's System card asks. This asked "which *active* row will not open", and the
    Connectors card's repair retires the unreadable row and mints a fresh one, so an operator who
    took the repair the UI offers first and then followed its advice to run this got "custody is
    intact" over an install whose second connector and web-push keypair were still sealed under
    the retired row. That is exactly the install this command exists for.
    [M4.7 dd03, ops-11; decision 181]
    """
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
            # The `FOR UPDATE` this used to take on its own SELECT, kept where it belongs: the
            # Connectors card's repair retires a row and seals a fresh ciphertext in one request
            # (`connectors.save_jellyfin`), and a reset that read the old key_id and wrote after
            # that save would clear a ciphertext this SECRETS_KEY can open.
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
            # DELETE, where `connector_config` above only clears its sealed columns. The
            # asymmetry is the table's: `app_setting` exists for the VAPID pair (0001_system.sql
            # says so), whose halves are meaningless apart — clearing `secret` and keeping
            # `value` leaves a public key `/api/push/state` goes on handing browsers while
            # nothing holds the half that signs, which is the silent failure `push/keys`' own
            # docstring is written against. Worse, `ensure_keypair` short-circuited on that
            # surviving row, so the repair below promised a new keypair that no boot could ever
            # mint. Removing the row makes the next boot a first boot for the pair, exactly as
            # M4.7 section 2 words this repair. [M4.7 dd03; decision 181]
            for key in await conn.fetch(
                "DELETE FROM app_setting WHERE secret_key_id = ANY($1::text[]) RETURNING key",
                unreadable,
            ):
                lost.append(f"app_setting/{key['key']}")
            # Last, so a failure above rolls the whole thing back rather than retiring the only
            # row that could still identify the ciphertexts. `retired_at IS NULL` because the set
            # above now includes rows the Connectors card's repair already retired, and rows no
            # `data_encryption_key` row answers to at all (a dump restored without that table):
            # re-stamping the first would misdate the retirement, and the second matches nothing.
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
