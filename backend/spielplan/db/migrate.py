"""Migration runner.

Migrations are plain `.sql` files applied in filename order inside one transaction each,
recorded in `schema_migration` with a checksum. A changed checksum on an applied migration is
a hard error: at household scale the correct fix is a new migration, never an edited one.

Applying is serialised by a session-level advisory lock. `pending`'s docstring has always said
"exactly one process may own the schema"; until M4.7 that was prose and the two containers
compose starts together could and did race inside the DDL. [M4.7 data-08]
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from spielplan.core.config import settings
from spielplan.db.pool import close_pool, open_pool

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
);
"""


def _checksum(sql: str) -> str:
    # Hashed from `discover`'s `read_text`, and that is load-bearing: `read_text` translates
    # universal newlines, so a CRLF checkout and an LF one produce the same digest for the same
    # file. Replacing it with `read_bytes` would give every Windows clone a different checksum
    # from the Linux container that migrated the database, which lands as this module's hardest
    # error at boot with no edit having happened. [M4.7 data-08]
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str]]:
    # No `is_dir` refusal here on purpose: `discover` is the seam the tests point at tmp
    # directories, and the refusal belongs to the two entry points that own a database.
    return [
        (p.stem, p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.sql"))
    ]


def _require_directory(directory: Path) -> None:
    """A missing migrations directory is a packaging failure, not an empty release.

    `Path.glob` on a directory that does not exist yields nothing rather than raising, so
    before this both entry points read "no migrations to apply" out of it: `apply_all` created
    `schema_migration` and returned `[]`, and the app booted with exactly one table and 404s
    everywhere. That is the shape a bad `COPY backend/migrations` in the image produces, and it
    has to be an error at the first line rather than a silent empty schema. [M4.7 data-08]
    """
    if not directory.is_dir():
        raise RuntimeError(
            f"no migrations directory at {directory}. This build ships no schema; an absent "
            "directory is a packaging error, never an empty release."
        )


async def apply_all(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the versions applied in this run."""
    _require_directory(directory)
    # One writer, held for the whole run. Compose starts backend and worker together and CI
    # starts a stack while a drill is mid-apply; with no lock at all, two concurrent applies on
    # a fresh database were reproduced killing one of them with a unique violation on
    # `pg_type_typname_nsp_index` — two sessions running the same CREATE TABLE, which is what
    # DDL races look like from the outside.
    #
    # Session-level rather than `pg_advisory_xact_lock`, because each migration below runs in
    # its own transaction: a transaction-scoped lock would be dropped by the first COMMIT and
    # leave the rest of the release unprotected, which is the window the loser needs. And taken
    # before BOOTSTRAP rather than after, because `CREATE TABLE IF NOT EXISTS` is itself one of
    # the statements that raises that violation when two sessions reach it together — the
    # advisory lock needs no table of its own, so there is nothing to order it after.
    # [M4.7 data-08]
    await conn.execute("SELECT pg_advisory_lock(hashtext('spielplan.migrate'))")
    try:
        await conn.execute(BOOTSTRAP)
        applied = {
            r["version"]: r["checksum"]
            for r in await conn.fetch("SELECT version, checksum FROM schema_migration")
        }
        discovered = discover(directory)

        # The other direction of the checksum guard, and the one nothing looked at: a migration
        # renamed after it was applied leaves its old version row behind, and the rename itself
        # then re-runs the file's DDL under a new name and dies on a DuplicateTableError that
        # names a table, not a migration. The orphan row is the evidence, and until now no
        # tooling reported it. Refusing here is what makes CLAUDE.md's "never rename an applied
        # migration" visible at the moment it is broken. [M4.7 data-08]
        orphans = sorted(set(applied) - {v for v, _ in discovered})
        if orphans:
            raise RuntimeError(
                f"schema_migration records {len(orphans)} migration(s) with no file in "
                f"{directory}: {', '.join(orphans)}. An applied migration is never renamed or "
                "deleted; restore the file, or this database holds DDL this build cannot name."
            )

        run: list[str] = []
        for version, sql in discovered:
            digest = _checksum(sql)
            if version in applied:
                if applied[version] != digest:
                    raise RuntimeError(
                        f"migration {version} changed after being applied "
                        f"(recorded {applied[version][:12]}, on disk {digest[:12]}). "
                        "Add a new migration instead of editing an applied one."
                    )
                continue
            async with conn.transaction():
                # 0001 creates schema_migration itself; the bootstrap above already did, and
                # CREATE TABLE without IF NOT EXISTS would fail, so skip that one statement.
                await conn.execute(_strip_bootstrap(sql) if version.startswith("0001") else sql)
                await conn.execute(
                    "INSERT INTO schema_migration (version, checksum) VALUES ($1, $2)",
                    version,
                    digest,
                )
            run.append(version)
        return run
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtext('spielplan.migrate'))")


async def pending(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Which migrations this code has that the database has not applied. Writes nothing.

    The worker uses this instead of applying: exactly one process may own the schema, or a
    rolling restart in which the two are briefly on different images has each of them
    applying and then rejecting the other's work.
    """
    _require_directory(directory)
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migration')")
    if exists is None:
        return [version for version, _ in discover(directory)]
    applied = {
        r["version"]: r["checksum"]
        for r in await conn.fetch("SELECT version, checksum FROM schema_migration")
    }
    out = []
    for version, sql in discover(directory):
        digest = _checksum(sql)
        if version not in applied:
            out.append(version)
        elif applied[version] != digest:
            raise RuntimeError(
                f"migration {version} in the database does not match this build "
                f"(recorded {applied[version][:12]}, on disk {digest[:12]}). The two processes "
                "are running different code; finish the deploy before continuing."
            )
    return out


def _strip_bootstrap(sql: str) -> str:
    """Drop the CREATE TABLE schema_migration statement from 0001 — the runner owns it."""
    marker = "CREATE TABLE schema_migration ("
    start = sql.find(marker)
    if start == -1:
        return sql
    end = sql.find(");", start)
    return sql[:start] + sql[end + 2 :]


async def _main() -> int:
    """`spielplan-migrate`, as an operator runs it inside the image.

    Through `db/pool` rather than a bare `asyncpg.connect`: `open_pool` is where `search_path`
    is set as a startup parameter and the json codecs are registered, and this command applies
    the same DDL the app's own boot does. A second, un-initialised path to it is a difference
    that shows up only in whichever migration happens to depend on the setting. One connection
    is all this needs, so the pool is opened at size one and closed in a `finally`.
    [M4.7 plan step 9, spec-08]
    """
    db = await open_pool(settings().database_url, min_size=1, max_size=1)
    try:
        async with db.acquire() as conn:
            run = await apply_all(conn)
        print(f"applied {len(run)} migration(s): {', '.join(run) if run else '(none pending)'}")
    finally:
        await close_pool()
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
