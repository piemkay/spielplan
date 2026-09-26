"""Migration runner: `.sql` files in filename order, one transaction each, sha256-checksummed.
An edited applied migration is a hard error. Serialised by a session-level advisory lock.
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
    # Hashed from `read_text` (universal newlines), so CRLF and LF checkouts agree. Not `read_bytes`.
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str]]:
    # No `is_dir` refusal: tests point this at tmp dirs; the entry points own the refusal.
    return [
        (p.stem, p.read_text(encoding="utf-8"))
        for p in sorted(directory.glob("*.sql"))
    ]


def _require_directory(directory: Path) -> None:
    """`Path.glob` on a missing directory yields nothing: a packaging error, not an empty release."""
    if not directory.is_dir():
        raise RuntimeError(
            f"no migrations directory at {directory}. This build ships no schema; an absent "
            "directory is a packaging error, never an empty release."
        )


async def apply_all(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    _require_directory(directory)
    # Session-level, not xact: each migration commits on its own. Taken before BOOTSTRAP, whose
    # CREATE TABLE IF NOT EXISTS itself races between two sessions.
    await conn.execute("SELECT pg_advisory_lock(hashtext('spielplan.migrate'))")
    try:
        await conn.execute(BOOTSTRAP)
        applied = {
            r["version"]: r["checksum"]
            for r in await conn.fetch("SELECT version, checksum FROM schema_migration")
        }
        discovered = discover(directory)

        # A renamed or deleted applied migration leaves an orphan row; refuse it by name.
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
                # 0001 creates schema_migration too, which BOOTSTRAP already did: strip that statement.
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
    """Writes nothing: only the backend applies; the worker only asks."""
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
    marker = "CREATE TABLE schema_migration ("
    start = sql.find(marker)
    if start == -1:
        return sql
    end = sql.find(");", start)
    return sql[:start] + sql[end + 2 :]


async def _main() -> int:
    """Through `db/pool`, which sets `search_path` and the json codecs exactly as the app's boot does."""
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
