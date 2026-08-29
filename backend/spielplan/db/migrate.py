"""Migration runner.

Migrations are plain `.sql` files applied in filename order inside one transaction each,
recorded in `schema_migration` with a checksum. A changed checksum on an applied migration is
a hard error: at household scale the correct fix is a new migration, never an edited one.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

from spielplan.core.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
);
"""


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str]]:
    return [
        (p.stem, p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.sql"))
    ]


async def apply_all(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the versions applied in this run."""
    await conn.execute(BOOTSTRAP)
    applied = {
        r["version"]: r["checksum"]
        for r in await conn.fetch("SELECT version, checksum FROM schema_migration")
    }

    run: list[str] = []
    for version, sql in discover(directory):
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


async def pending(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Which migrations this code has that the database has not applied. Writes nothing.

    The worker uses this instead of applying: exactly one process may own the schema, or a
    rolling restart in which the two are briefly on different images has each of them
    applying and then rejecting the other's work.
    """
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
    conn = await asyncpg.connect(settings().database_url)
    try:
        run = await apply_all(conn)
        print(f"applied {len(run)} migration(s): {', '.join(run) if run else '(none pending)'}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
