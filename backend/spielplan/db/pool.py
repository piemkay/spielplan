"""Raw asyncpg and hand-written SQL, not an ORM: §4.1's rules are easier to keep in explicit SQL."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from spielplan.core.config import settings

# The belt, not §4.1 rule 3's boundary: an unqualified display table fails to resolve. A startup
# parameter, because asyncpg's `RESET ALL` on release drops a `SET` issued in `init=`.
APP_SEARCH_PATH = "public"

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # asyncpg returns json/jsonb as text unless a codec is registered; decode once, here.
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def open_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn or settings().database_url,
            min_size=min_size,
            max_size=max_size,
            server_settings={"search_path": APP_SEARCH_PATH},
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not open — call open_pool() during startup")
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn, conn.transaction():
        yield conn
