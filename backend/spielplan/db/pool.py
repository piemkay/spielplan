"""Postgres access. Spec v2.1 §1 (Postgres 16), §4.1.

Raw asyncpg + hand-written SQL rather than an ORM: the schema is dictated by the corpus
export and by §4.1's landmine rules, several of which (the display-only schema boundary, the
never-union rule on the two DNA tiers) are easier to keep honest in explicit SQL than behind
a mapper that will happily join anything.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from spielplan.core.config import settings

# §4.1 rule 3: the feature builder must not be able to reach `display`. What actually keeps that
# boundary is the single schema-qualified read in `db/library.py` and the guard that pins it there
# (`test_landmine_guards.py::test_display_schema_is_read_from_exactly_one_place`); a future
# feature-builder role gets no USAGE at all. This line is only the belt: it makes an *unqualified*
# reference to a display table fail rather than resolve, which is worth having and is not the rule.
#
# Said plainly because the comment used to claim the enforcement outright while the setting was
# silently not in force: `SET search_path` inside `init=` is a session-local statement, and asyncpg
# issues `RESET ALL` when a connection is released back to the pool. `SHOW search_path` read
# `public` on a fresh connection and `"$user", public` after one release and re-acquire — so every
# connection after the first had the default. As a *startup* parameter it survives the reset,
# because `RESET ALL` restores the values the connection was opened with. [M4.7 schema-pool-acquire]
APP_SEARCH_PATH = "public"

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # asyncpg hands back json/jsonb as *text* unless a codec is registered. Every caller then
    # has to remember to parse it, and the one that forgets does not crash — it ships a string
    # where a list was expected, and the UI iterates it character by character. That is exactly
    # how the DNA evidence quotes rendered as eighty empty strings. Decode once, here.
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
