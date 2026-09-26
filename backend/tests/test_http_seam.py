"""The HTTP seam: health, the schema, and what a database failure looks like, over ASGI (§2, §14).
The schema half needs no database; the rest need TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import errno
import logging
import ssl
import time

import asyncpg
import httpx
import pytest
from fastapi import Request

from spielplan import app as app_module
from spielplan.api import deps
from spielplan.app import create_app
from spielplan.core.config import settings
from spielplan.db import pool as pool_module

# The ping `/api/health` issues; only it is blocked, so the other route still works.
PING = "SELECT 1"

# Port 1: a real refused connection. asyncpg does not wrap `ConnectionRefusedError`.
DEAD_DSN = "postgresql://spielplan:spielplan@127.0.0.1:1/spielplan"

# Its shape does not matter; that it survives an exception handler does.
SLID_COOKIE = "spielplan_session=a-slid-session-value; Path=/; HttpOnly; SameSite=lax"

DUPLICATE = "INSERT INTO app_setting (key, value) VALUES ('seam', '{}'::jsonb)"

# Constructed, not provoked: an unwritable directory is platform-specific and the errno is the payload.
FILESYSTEM_FAILURES = {
    "denied": lambda: PermissionError(13, "Permission denied: '/data/artifacts/2026.09-a'"),
    "full": lambda: OSError(28, "No space left on device: '/data/artifacts/2026.09-a'"),
}

# An unreachable host arrives as a *plain* `OSError` with an errno, a bad certificate as `ssl.SSLError`.
UNREACHABLE_FAILURES = {
    "no-route-to-host": lambda: OSError(errno.EHOSTUNREACH, "No route to host"),
    "network-unreachable": lambda: OSError(errno.ENETUNREACH, "Network is unreachable"),
    "network-down": lambda: OSError(errno.ENETDOWN, "Network is down"),
    "host-down": lambda: OSError(errno.EHOSTDOWN, "Host is down"),
    "network-reset": lambda: OSError(errno.ENETRESET, "Network dropped connection on reset"),
    "tls": lambda: ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    ),
}


def _client(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )


async def test_the_schema_and_both_renderers_are_404_to_an_anonymous_caller():
    """Both renderers: closing only `/api/docs` would move the disclosure, not end it."""
    async with _client(create_app()) as client:
        for path in ("/api/docs", "/redoc", "/openapi.json"):
            answer = await client.get(path)
            assert answer.status_code == 404, f"{path} is still served: {answer.status_code}"


def test_the_schema_still_builds_in_process_for_the_gating_sweeps():
    """`openapi()` does not need the route, so the gating sweeps still read it."""
    paths = create_app().openapi()["paths"]
    assert "/api/admin/users" in paths and "/api/health" in paths
    assert len(paths) > 50, "the schema is still the whole route surface, in process"


async def test_the_dev_flag_serves_the_docs_again(monkeypatch):
    """The same flag the config refusals honour, not a second knob."""
    monkeypatch.setenv("SPIELPLAN_INSECURE_DEV", "1")
    settings.cache_clear()
    try:
        async with _client(create_app()) as client:
            assert (await client.get("/api/docs")).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200
    finally:
        settings.cache_clear()


async def test_health_answers_200_and_the_body_its_three_consumers_read(app):
    """`e2e/run.mjs` reads `.bundle`, so the 503 change must not move the body."""
    answer = await app().get("/api/health")

    assert answer.status_code == 200
    assert answer.json() == {
        "ok": True,
        "role": "backend",
        "bundle": None,           # §3.1: a bundle-less app is a legal, reported state
        "public_url": settings().public_url,
        "storage": {"ok": True, "unwritable": []},
    }


async def test_health_reports_a_broken_install_as_no_loaded_bundle_rather_than_as_its_version(
    app, tmp_path
):
    """`load_active` carries a broken install's version; `is_empty` is the probe's question."""
    from spielplan.models.artifacts import ArtifactStore

    client = app()
    broken = ArtifactStore(version="gone-v1", root=tmp_path / "artifacts" / "gone-v1", broken=True)
    assert broken.is_empty, "a broken store still renders the no-bundle surfaces (section 3.1)"
    client._transport.app.state.artifacts = broken

    body = (await client.get("/api/health")).json()
    assert body["bundle"] is None, (
        "the unauthenticated probe named a bundle whose directory is gone"
    )
    assert set(body) == {"ok", "role", "bundle", "public_url", "storage"}, (
        "the broken state is reported through the field that already means it, not a new one"
    )


async def test_health_answers_503_when_the_database_is_unreachable(app, monkeypatch):
    """Docker, CI and e2e all key on the status code, so `ok: false` in a 200 was invisible."""

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError(111, "connection refused")

    client = app()
    monkeypatch.setattr(pool_module, "pool", refused)
    answer = await client.get("/api/health")

    assert answer.status_code == 503
    body = answer.json()
    assert body["ok"] is False
    assert set(body) == {"ok", "role", "bundle", "public_url", "storage"}, (
        "a failing health check answers the same shape as a passing one"
    )


def _block_the_ping(monkeypatch) -> None:
    """No SQL makes `SELECT 1` block, so a double that honours `timeout=` as asyncpg does."""
    real = asyncpg.Connection.fetchval

    async def fetchval(self, query, *args, timeout=None, **kwargs):
        if query.strip() != PING:
            return await real(self, query, *args, timeout=timeout, **kwargs)
        await asyncio.wait_for(asyncio.Event().wait(), timeout)

    monkeypatch.setattr(asyncpg.Connection, "fetchval", fetchval)


async def test_health_gives_up_on_a_hung_database_within_its_own_bound(app, monkeypatch):
    """The ping bound alone; the release half is the next test."""
    client = app()
    _block_the_ping(monkeypatch)

    started = time.monotonic()
    answer = await asyncio.wait_for(client.get("/api/health"), timeout=10)
    elapsed = time.monotonic() - started

    assert answer.status_code == 503 and answer.json()["ok"] is False
    assert elapsed < 5, f"the probe answered in {elapsed:.1f}s, outside its own bound"


# The route reads `_HEALTH_TIMEOUT_S` at call time, so this is the whole substitution.
_PATCHED_BOUND = 0.5


async def test_a_health_probe_holds_its_connection_for_two_of_its_own_bounds_not_one(
    app, monkeypatch
):
    """`Pool.release` defaults to the acquire's timeout, so acquire, ping and release cost three bounds."""
    client = app()
    monkeypatch.setattr(app_module, "_HEALTH_TIMEOUT_S", _PATCHED_BOUND)
    _block_the_ping(monkeypatch)
    real_release = asyncpg.pool.Pool.release

    async def cancelling_release(self, connection, *, timeout=None):
        await asyncio.sleep(_PATCHED_BOUND)
        return await real_release(self, connection, timeout=timeout)

    monkeypatch.setattr(asyncpg.pool.Pool, "release", cancelling_release)

    started = time.monotonic()
    answer = await asyncio.wait_for(client.get("/api/health"), timeout=10)
    elapsed = time.monotonic() - started

    assert answer.status_code == 503 and answer.json()["ok"] is False
    assert elapsed >= _PATCHED_BOUND * 2, (
        f"the probe held the pool for {elapsed:.2f}s, one bound and not two - the release double "
        "did not fire, so this proves nothing"
    )
    assert elapsed < _PATCHED_BOUND * 3, (
        f"the probe answered in {elapsed:.2f}s, outside the three bounds the constant's comment "
        "now states are the worst case"
    )


async def test_ten_health_probes_do_not_lock_another_route_out_of_the_pool(app, monkeypatch):
    """The image checks every 30 s and CI every 2 s: ten in flight is one hung database."""
    probes = [app() for _ in range(10)]
    other = app()
    _block_the_ping(monkeypatch)

    in_flight = [asyncio.create_task(c.get("/api/health")) for c in probes]
    try:
        live = pool_module.pool()
        for _ in range(60):
            if live.get_size() == 10 and live.get_idle_size() == 0:
                break
            await asyncio.sleep(0.05)
        assert live.get_idle_size() == 0, "the probes did not take the pool, so this proves nothing"

        started = time.monotonic()
        answered = await asyncio.wait_for(other.get("/api/setup/state"), timeout=10)
        elapsed = time.monotonic() - started

        assert answered.status_code == 200, "a real request was locked out by the health probes"
        assert elapsed < 5, f"the request waited {elapsed:.1f}s behind the probes"
        assert [a.status_code for a in await asyncio.gather(*in_flight)] == [503] * 10
    finally:
        # Without this a failing run wedges in teardown: `close_pool()` waits for ever.
        for probe in in_flight:
            probe.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)


async def test_an_exhausted_pool_answers_at_the_acquire_bound_rather_than_holding_the_phone(
    app, monkeypatch
):
    """The detail carries the assertion: the connection
    handler also answers 503, with the wrong diagnosis."""
    client = app()
    monkeypatch.setattr(deps, "_ACQUIRE_TIMEOUT_S", 0.25)
    live = pool_module.pool()
    held = [await live.acquire() for _ in range(10)]
    try:
        assert live.get_idle_size() == 0, "the pool is not exhausted, so this proves nothing"

        answer = await asyncio.wait_for(client.get("/api/setup/state"), timeout=10)

        assert answer.status_code == 503
        assert answer.json() == {"detail": "database unavailable"}
    finally:
        for conn in held:
            await live.release(conn)


async def test_the_acquire_bound_says_in_the_log_which_of_its_two_failures_this_was(
    app, monkeypatch, caplog
):
    """An acquire that times out on an empty pool is an unreachable peer, and must say so in the log."""
    client = app()
    monkeypatch.setattr(deps, "_ACQUIRE_TIMEOUT_S", 0.25)
    live = pool_module.pool()
    held = [await live.acquire() for _ in range(10)]
    try:
        with caplog.at_level(logging.WARNING, logger="spielplan.api.deps"):
            answer = await asyncio.wait_for(client.get("/api/setup/state"), timeout=10)

        assert answer.status_code == 503
        said = [r.getMessage() for r in caplog.records if r.name == "spielplan.api.deps"]
        assert said, "the acquire gave up and the log says nothing at all"
        assert "no pooled connection within 0.25s" in said[0], said
        assert "pool size 10, idle 0" in said[0], (
            f"the line does not say which of the two failures this was: {said[0]}"
        )
    finally:
        for conn in held:
            await live.release(conn)


async def test_the_search_path_survives_a_release_back_to_the_pool(app):
    """`init=`'s `SET search_path` is lost to `RESET ALL` on release; a startup parameter survives it."""
    live = pool_module.pool()
    async with live.acquire() as conn:
        first = await conn.fetchval("SHOW search_path")
    async with live.acquire() as conn:
        after_release = await conn.fetchval("SHOW search_path")

    assert first == "public"
    assert after_release == "public", (
        f"a re-acquired connection reads {after_release!r} - the SET did not survive the reset"
    )


@pytest.fixture
async def seam(db):
    """Routes added to fail the ways the handlers exist
    for: the shipped routes catch their own conflicts."""
    application = create_app()

    @application.get("/api/test-seam/conflict")
    async def _conflict():
        await db.execute(DUPLICATE)
        await db.execute(DUPLICATE)

    @application.get("/api/test-seam/undefined-table")
    async def _undefined():
        await db.fetchval("SELECT 1 FROM a_table_that_was_never_created")

    @application.get("/api/test-seam/refused")
    async def _refused():
        await asyncpg.connect(DEAD_DSN, timeout=5)

    @application.get("/api/test-seam/artifacts/{failure}")
    async def _artifacts(failure: str):
        raise FILESYSTEM_FAILURES[failure]()

    @application.get("/api/test-seam/unreachable/{failure}")
    async def _unreachable(failure: str):
        raise UNREACHABLE_FAILURES[failure]()

    @application.get("/api/test-seam/slid/{failure}")
    async def _slid(failure: str, request: Request):
        request.state.slid_session_cookie = SLID_COOKIE
        if failure == "conflict":
            await db.execute(DUPLICATE)
            await db.execute(DUPLICATE)
        raise ConnectionRefusedError(111, "connection refused")

    async with _client(application) as client:
        yield client


async def test_a_unique_violation_becomes_a_409_naming_the_constraint(seam):
    """Naming the constraint tells the caller which uniqueness rule it lost a race to."""
    answer = await seam.get("/api/test-seam/conflict")

    assert answer.status_code == 409
    assert "app_setting_pkey" in answer.json()["detail"]


async def test_the_unique_handler_wins_over_the_general_one(seam):
    """Both handlers match; Starlette resolves along the MRO, and the 409 must win."""
    conflict = await seam.get("/api/test-seam/conflict")
    other = await seam.get("/api/test-seam/undefined-table")

    assert conflict.status_code == 409
    assert other.status_code == 500 and other.json() == {"detail": "database error"}


async def test_a_refused_connection_becomes_a_503_rather_than_a_traceback(seam):
    """`ConnectionRefusedError` is `OSError`, not `PostgresError`."""
    answer = await seam.get("/api/test-seam/refused")

    assert answer.status_code == 503
    assert answer.json() == {"detail": "database unreachable"}


@pytest.mark.parametrize("failure", sorted(UNREACHABLE_FAILURES))
async def test_every_shape_of_unreachable_becomes_a_503_not_only_the_three_with_class_names(
    seam, failure
):
    """The whole family: a handler chosen by class can be right for some members and wrong for others."""
    answer = await seam.get(f"/api/test-seam/unreachable/{failure}")

    assert answer.status_code == 503
    assert answer.json() == {"detail": "database unreachable"}


@pytest.mark.parametrize(("failure", "raised"), [("denied", PermissionError), ("full", OSError)])
async def test_a_filesystem_failure_is_not_reported_as_a_database_failure(
    seam, caplog, failure, raised
):
    """EACCES (13) and ENOSPC (28) on `/data/artifacts` are not the database, so they stay 500."""
    with pytest.raises(raised):
        await seam.get(f"/api/test-seam/artifacts/{failure}")

    assert "database unreachable" not in caplog.text


@pytest.mark.parametrize(("failure", "status"), [("conflict", 409), ("unreachable", 503)])
async def test_both_new_handlers_carry_the_slid_session_cookie(seam, failure, status):
    """§3.2's slide happens before the route body, so a handler-built response must carry the Set-Cookie."""
    answer = await seam.get(f"/api/test-seam/slid/{failure}")

    assert answer.status_code == status
    assert answer.headers.get("set-cookie") == SLID_COOKIE
