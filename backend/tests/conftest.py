"""Shared fixtures.

Tests that need a real Postgres read `TEST_DATABASE_URL` and skip when it is unset, so the
suite stays runnable on a machine with no Docker. To run them:

    docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db
    createdb spielplan_test      # or: docker compose exec db createdb -U spielplan spielplan_test
    TEST_DATABASE_URL=postgresql://spielplan:...@127.0.0.1:5432/spielplan_test pytest
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Load .env.test if present, so the URL does not have to be exported by hand every time.
_env_test = ROOT.parent / ".env.test"
if _env_test.is_file() and "TEST_DATABASE_URL" not in os.environ:
    for line in _env_test.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            os.environ["TEST_DATABASE_URL"] = value.strip()


def test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset (see tests/conftest.py)")
    return url


@pytest.fixture
async def db(pg_url):
    """A connection to a freshly migrated, empty database.

    Each test gets a clean schema: the fixtures are small and dropping the schema is faster
    and far less surprising than trying to unwind an importer that writes fourteen tables.
    """
    import asyncpg

    from spielplan.db import migrate, pool

    conn = await asyncpg.connect(pg_url)
    for typename in ("json", "jsonb"):
        import json as _json

        await conn.set_type_codec(
            typename, encoder=_json.dumps, decoder=_json.loads, schema="pg_catalog"
        )
    try:
        await conn.execute(
            "DROP SCHEMA IF EXISTS public CASCADE;"
            "DROP SCHEMA IF EXISTS display CASCADE;"
            "DROP SCHEMA IF EXISTS review_store CASCADE;"
            "CREATE SCHEMA public;"
        )
        await migrate.apply_all(conn)
        yield conn
    finally:
        await conn.close()
        await pool.close_pool()
