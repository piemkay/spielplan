"""The application's HTTP seam: health, the schema, and what a database failure looks like.

Spec v2.1 §1, §2 (Configuration), §3.1, §4.1, §14; decision 181;
docs/milestones/M4.7-plan.md §2 findings 6, 7, 8 and 31.

Four defects, one block of code. `/api/health` answered 200 with `ok: false` while its three
consumers — the image HEALTHCHECK, CI's wait loop and `e2e/run.mjs` — all key on the status code
and none reads the body. The probe held a pooled connection with no timeout, so ten of them
against a hung database emptied a ten-connection pool and the next real request never answered.
The whole route surface was published anonymously as an OpenAPI document. And the one exception
handler was for `asyncpg.PostgresError`, so a refused connection at first boot — the operator who
changed `POSTGRES_PASSWORD` and not the password inside `DATABASE_URL` — escaped as a traceback.

The health and seam halves drive the *routes* over ASGI, not the helpers underneath them: what
these findings are about is the answer that reaches a phone or a wait loop, and only HTTP can say
what that is. Those halves are skipped without TEST_DATABASE_URL (see tests/conftest.py); the
schema half needs no database and must not skip, because it is a property of `create_app` alone.
"""

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

# The ping `/api/health` issues. Named here so the double below can block that one query and let
# every other query through — a blanket block would stop the second route this test needs.
PING = "SELECT 1"

# Port 1 is assignable to no service and nothing listens on it, so this is a real refused
# connection rather than a raised exception object pretending to be one. asyncpg does not wrap it:
# `ConnectionRefusedError` comes straight out of the event loop's `create_connection`.
DEAD_DSN = "postgresql://spielplan:spielplan@127.0.0.1:1/spielplan"

# What `deps.current_user` puts on the request when §3.2's window slides. Its shape does not
# matter here; that it survives an exception handler does.
SLID_COOKIE = "spielplan_session=a-slid-session-value; Path=/; HttpOnly; SameSite=lax"

DUPLICATE = "INSERT INTO app_setting (key, value) VALUES ('seam', '{}'::jsonb)"

# Two `OSError`s that have nothing to do with the database, keyed by the path segment that asks
# for them. Constructed rather than provoked because making a directory genuinely unwritable is
# platform-specific (POSIX chmod does not stop root, Windows needs an ACL) and the errno is the
# whole payload here — unlike the duplicate key below, where a real one is needed for its
# constraint name. Both are what `/data/artifacts` produces on a box where the host directory is
# not owned by uid 1000, or is full. [M4.7 ops-12, sec-08]
FILESYSTEM_FAILURES = {
    "denied": lambda: PermissionError(13, "Permission denied: '/data/artifacts/2026.09-a'"),
    "full": lambda: OSError(28, "No space left on device: '/data/artifacts/2026.09-a'"),
}

# The rest of the family the handler is *for*, and the half no class name catches. Python raises a
# named `ConnectionError` subclass only for ECONNREFUSED/ECONNRESET/ECONNABORTED/EPIPE; a host or
# a network that cannot be reached at all arrives as a *plain* `OSError` carrying the errno, and a
# certificate that stopped verifying arrives as `ssl.SSLError` — an `OSError` whose errno is not a
# network one. Constructed for the same reason as `FILESYSTEM_FAILURES`: there is no portable way
# to make a host unroutable from inside a test, and the errno is the whole payload.
#
# Every one of these is `backup/nightly.py:20-22`'s supported shape failing — "an install pointing
# at a Postgres outside this compose file" whose peer went offline, whose VPN dropped, or whose
# `sslmode=require` certificate expired. [M4.7 ops-12; cycle 2 finding 3]
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


# --- sec-12: the schema is not published to anonymous callers -------------------------------


async def test_the_schema_and_both_renderers_are_404_to_an_anonymous_caller():
    """`/api/docs`, `/redoc` and `/openapi.json` were all served, in front of no auth.

    Both renderers are asserted, not only the one the finding names: they render the same
    document, so closing `/api/docs` and leaving FastAPI's default `/redoc` would have moved the
    disclosure rather than ended it.
    """
    async with _client(create_app()) as client:
        for path in ("/api/docs", "/redoc", "/openapi.json"):
            answer = await client.get(path)
            assert answer.status_code == 404, f"{path} is still served: {answer.status_code}"


def test_the_schema_still_builds_in_process_for_the_gating_sweeps():
    """The reason this fix is free: `openapi()` does not need the route.

    `test_api_gating.py` walks the running app for every admin path and `test_devstub_contract.py`
    compares the harness against `app.openapi()["paths"]`. Both read the method; only the HTTP
    exposure is gone.
    """
    paths = create_app().openapi()["paths"]
    assert "/api/admin/users" in paths and "/api/health" in paths
    assert len(paths) > 50, "the schema is still the whole route surface, in process"


async def test_the_dev_flag_serves_the_docs_again(monkeypatch):
    """§2's `SPIELPLAN_INSECURE_DEV` is the developer's way back to the browsable schema.

    One flag rather than a second one of its own: the developer reading the schema on their own
    machine is the same person the config refusals already exempt, and a knob per exemption is
    the unrequested configurability CLAUDE.md forbids.
    """
    monkeypatch.setenv("SPIELPLAN_INSECURE_DEV", "1")
    settings.cache_clear()
    try:
        async with _client(create_app()) as client:
            assert (await client.get("/api/docs")).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200
    finally:
        settings.cache_clear()


# --- ops-02 / dd-health-probes: health is honest, and bounded --------------------------------


async def test_health_answers_200_and_the_body_its_three_consumers_read(app):
    """The 503 change must not move the body: `e2e/run.mjs` reads `.bundle` out of it."""
    answer = await app().get("/api/health")

    assert answer.status_code == 200
    assert answer.json() == {
        "ok": True,
        "role": "backend",
        "bundle": None,           # §3.1: a bundle-less app is a legal, reported state
        "public_url": settings().public_url,
    }


async def test_health_reports_a_broken_install_as_no_loaded_bundle_rather_than_as_its_version(
    app, tmp_path
):
    """data-03's third state, on the one probe nobody has to authenticate to read.

    `load_active` carries a broken install's version on purpose -- that is what makes the fit it
    refuses to make stamp honestly, and it is why `assert_matches` cannot see the state at all.
    Reading `.version` here therefore turned "no bundle" into the name of a bundle this process
    cannot open one file of, for an operator whose model jobs are all refusing. `is_empty` is the
    question the probe is actually asking, and it is the same vocabulary §6.6's Data tab uses:
    `loaded: null` beside `active` and `broken`. [M4.13 cycle 2, M413-C2-D1-03]
    """
    from spielplan.models.artifacts import ArtifactStore

    client = app()
    broken = ArtifactStore(version="gone-v1", root=tmp_path / "artifacts" / "gone-v1", broken=True)
    assert broken.is_empty, "a broken store still renders the no-bundle surfaces (section 3.1)"
    client._transport.app.state.artifacts = broken

    body = (await client.get("/api/health")).json()
    assert body["bundle"] is None, (
        "the unauthenticated probe named a bundle whose directory is gone"
    )
    assert set(body) == {"ok", "role", "bundle", "public_url"}, (
        "the broken state is reported through the field that already means it, not a new one"
    )


async def test_health_answers_503_when_the_database_is_unreachable(app, monkeypatch):
    """The finding as the operator meets it: Docker, CI and the e2e harness all said healthy.

    Every one of them is a wait-until-success loop over `curl -fsS` or `res.ok`, so the status
    code is the whole signal and `ok: false` inside a 200 was invisible to all three.
    """

    def refused(*_args, **_kwargs):
        raise ConnectionRefusedError(111, "connection refused")

    client = app()
    monkeypatch.setattr(pool_module, "pool", refused)
    answer = await client.get("/api/health")

    assert answer.status_code == 503
    body = answer.json()
    assert body["ok"] is False
    assert set(body) == {"ok", "role", "bundle", "public_url"}, (
        "a failing health check answers the same shape as a passing one"
    )


def _block_the_ping(monkeypatch) -> None:
    """Make `SELECT 1` never return, the way a wedged server does.

    A double rather than a real hang because there is no SQL that makes `SELECT 1` block, and
    the finding's measurement is precisely "with the ping blocked". It honours `timeout=` exactly
    as asyncpg does — wrapping the wait and raising `TimeoutError` — so what is under test is
    whether the route passes a bound at all and releases its connection when the bound expires.
    With today's `fetchval("SELECT 1")` and no timeout the wait is unbounded, which is the defect.
    """
    real = asyncpg.Connection.fetchval

    async def fetchval(self, query, *args, timeout=None, **kwargs):
        if query.strip() != PING:
            return await real(self, query, *args, timeout=timeout, **kwargs)
        await asyncio.wait_for(asyncio.Event().wait(), timeout)

    monkeypatch.setattr(asyncpg.Connection, "fetchval", fetchval)


async def test_health_gives_up_on_a_hung_database_within_its_own_bound(app, monkeypatch):
    """curl gave up at 5 s; the server-side coroutine did not, and kept its connection.

    What this measures is the *ping* bound alone. `_block_the_ping` raises out of `fetchval` with
    the real server underneath still answering, so the connection never enters asyncpg's
    cancelling state and the release that follows is a no-op — which is not what a wedged peer
    does. The test below is the other half. [cycle 3 finding 8]
    """
    client = app()
    _block_the_ping(monkeypatch)

    started = time.monotonic()
    answer = await asyncio.wait_for(client.get("/api/health"), timeout=10)
    elapsed = time.monotonic() - started

    assert answer.status_code == 503 and answer.json()["ok"] is False
    assert elapsed < 5, f"the probe answered in {elapsed:.1f}s, outside its own bound"


# Long enough that the two bounds below are separable from scheduling noise, short enough that
# the test costs a second. The route reads `_HEALTH_TIMEOUT_S` at call time, so this is the whole
# substitution.
_PATCHED_BOUND = 0.5


async def test_a_health_probe_holds_its_connection_for_two_of_its_own_bounds_not_one(
    app, monkeypatch
):
    """The arithmetic the comment beside `_HEALTH_TIMEOUT_S` used to get wrong by 2x.

    It claimed the constant was short enough "that ten concurrent probes cannot hold the
    ten-connection pool for longer than that". They can hold it for twice that: asyncpg defaults
    `Pool.release`'s timeout to the one the acquire was given, and against a peer that has stopped
    answering the release spends a whole one waiting for the out-of-band cancel of the query the
    ping just abandoned, before terminating the connection. Acquire plus ping plus release is
    therefore three of these bounds and not one — 6 s on the shipped value of 2, past the 5 s the
    image's HEALTHCHECK gives curl, which is the window the comment said it was well inside.

    The release is a double for the same reason the ping is: there is no way to freeze a real peer
    from inside this suite, and the wait is the whole payload. It honours the timeout asyncpg
    resolves rather than inventing one, so what is asserted is the sum this route pays.
    [cycle 3 finding 8]
    """
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
    """The measured failure: pool at size 10, idle 0, and `/api/setup/state` never answering.

    The image runs its HEALTHCHECK every 30 s and CI's loop every 2 s, so "ten in flight" is not
    a stress test — it is one hung database and a few minutes. The property is that the probes
    let go: each releases at its own timeout, and the request behind them is served.
    """
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
        # Without this a *failing* run wedges rather than reports: probes that never gave their
        # connections back are probes `close_pool()` waits on for ever, and the wait happens in
        # fixture teardown where there is no assertion left to fail. Which is the defect itself,
        # seen from the test side.
        for probe in in_flight:
            probe.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)


async def test_an_exhausted_pool_answers_at_the_acquire_bound_rather_than_holding_the_phone(
    app, monkeypatch
):
    """The other branch of the same finding, and the one nothing provoked.

    The test above proves the probes let go; this one proves what a request meets while they have
    not. `api/deps.db` keeps its connection for the life of a request, so ten slow requests are
    ten held connections and the eleventh waits — unbounded, for ever, which is the measured
    defect (pool size 10, idle 0, `/api/setup/state` never answering). The bound is
    `_ACQUIRE_TIMEOUT_S`, cut to a fraction of a second here so the assertion is about the branch
    rather than about ten seconds of wall clock.

    The connections are held directly rather than through blocked health probes, because the
    property under test is what the *dependency* does with a pool that has nothing to give, and
    holding them outright says that without a second timeout racing this one.

    The detail carries the assertion rather than the status, because the status alone cannot fail:
    an acquire timeout that stops being caught here is a `TimeoutError` reaching `app.py`'s
    connection handler, which answers 503 as well — the right code with the wrong diagnosis, and a
    request that is queued behind nine peers reported as a database nobody can reach. `wait_for`
    covers the other regression: if the bound goes away the request is never answered at all, and
    a hang inside a test is a wedged run rather than a red one. [M4.7 schema-pool-acquire]
    """
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
    """The same branch from the operator's side, and it was silent.

    The acquire covers two failures at once. One is the pool above: ten peers holding the ten
    connections, which is a busy database. The other is a pool with nothing to hand out, spending
    the whole bound inside asyncpg's connect on a peer that has stopped answering — §2's supported
    "a Postgres outside this compose file" behind a VPN that dropped. Measured against a frozen
    proxy, that second one answered every tap `503 database busy` after exactly `_ACQUIRE_TIMEOUT_S`
    with `pool size 0 idle 0`, and the root logger recorded nothing for the whole episode; the same
    peer *refusing* one second earlier is a `ConnectionRefusedError`, which reaches `app.py`'s
    handler and logs "database unreachable" with its traceback.

    So the log line is the whole repair: the detail cannot name a cause the acquire cannot
    distinguish, and the census can — an empty pool is a peer that is not there. Provoked with the
    holders taken outright, as above, because what is under test is that the branch says anything
    at all. [cycle 3 finding 7]
    """
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
    """§4.1 rule 3's belt, which was not fastened.

    `SET search_path` in `init=` is session-local and asyncpg issues `RESET ALL` on release, so
    the first connection out of the pool had `public` and every one after it had the server
    default. As a startup parameter it survives the reset, because `RESET ALL` restores what the
    connection was opened with.
    """
    live = pool_module.pool()
    async with live.acquire() as conn:
        first = await conn.fetchval("SHOW search_path")
    async with live.acquire() as conn:
        after_release = await conn.fetchval("SHOW search_path")

    assert first == "public"
    assert after_release == "public", (
        f"a re-acquired connection reads {after_release!r} - the SET did not survive the reset"
    )


# --- the error seam: 409, 503, and still 500 for everything else -----------------------------


@pytest.fixture
async def seam(db):
    """The real application, with routes that fail the ways the handlers exist for — and one way
    they must not answer for.

    They are added here rather than found among the shipped routes because the shipped routes
    catch their own conflicts — which is correct, and leaves the handler that catches the ones
    nobody predicted with nothing to be provoked by. The errors themselves are real: a genuine
    duplicate key from Postgres carrying its own constraint name, a genuine undefined table, and
    a genuine refused TCP connection. `/artifacts/{failure}` is the exception, and says why in
    `FILESYSTEM_FAILURES`.
    """
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
    """The seam M4.10 and M4.12 plug into, asserted here so they can point at it.

    Naming the constraint is the part that makes it useful: "database error" with a 500 tells a
    caller nothing about which of a route's several uniqueness rules it just lost a race to.
    """
    answer = await seam.get("/api/test-seam/conflict")

    assert answer.status_code == 409
    assert "app_setting_pkey" in answer.json()["detail"]


async def test_the_unique_handler_wins_over_the_general_one(seam):
    """`UniqueViolationError` is a `PostgresError`, so both handlers match it.

    Starlette resolves a handler along `type(exc).__mro__` and the subclass names itself first,
    which means the 409 is chosen wherever the two are written relative to each other. Asserted
    rather than assumed, because the day that stops being true the only symptom is a 500 on a
    conflict — the exact answer this seam exists to replace.
    """
    conflict = await seam.get("/api/test-seam/conflict")
    other = await seam.get("/api/test-seam/undefined-table")

    assert conflict.status_code == 409
    assert other.status_code == 500 and other.json() == {"detail": "database error"}


async def test_a_refused_connection_becomes_a_503_rather_than_a_traceback(seam):
    """ops-12, as first boot produces it.

    `.env.example` used to ask for the database password twice — `POSTGRES_PASSWORD` and again
    inside `DATABASE_URL` — with compose defaulting each independently, so the two disagreed the
    first time an operator set a real one, on a stack whose database is the last service to be
    ready. The same milestone states it once; this handler is what makes the *next* such failure
    (a wrong host, a database that is not up yet) a status rather than a traceback.
    `ConnectionRefusedError` is `OSError`, not `PostgresError`, so it escaped the only handler
    there was and the caller got a traceback rather than a status.
    """
    answer = await seam.get("/api/test-seam/refused")

    assert answer.status_code == 503
    assert answer.json() == {"detail": "database unreachable"}


@pytest.mark.parametrize("failure", sorted(UNREACHABLE_FAILURES))
async def test_every_shape_of_unreachable_becomes_a_503_not_only_the_three_with_class_names(
    seam, failure
):
    """The rest of ops-12's family, which the fix for its other half narrowed the handler out of.

    Registering `ConnectionError`, `socket.gaierror` and `TimeoutError` catches a refusal, a name
    that does not resolve and an acquire that gave up — and nothing else. Python maps
    EHOSTUNREACH, ENETUNREACH, ENETDOWN, EHOSTDOWN and ENETRESET to a *plain* `OSError`, which is
    a subclass of none of the three, and a certificate that stopped verifying to `ssl.SSLError`,
    which is an `OSError` with an errno that means nothing about the network. Every one of them
    escaped the seam as a bare traceback: the phone got `ServerErrorMiddleware`'s unstyled 500
    and the log never wrote the line naming the service that is actually down.

    Parametrised over the whole set rather than over the members that happen to be handled,
    because a handler chosen by exception class had no way to be wrong about one of them and
    right about the others — that is the shape of the defect, not an accident of which one was
    tried. [M4.7 cycle 2 finding 3]
    """
    answer = await seam.get(f"/api/test-seam/unreachable/{failure}")

    assert answer.status_code == 503
    assert answer.json() == {"detail": "database unreachable"}


@pytest.mark.parametrize(("failure", "raised"), [("denied", PermissionError), ("full", OSError)])
async def test_a_filesystem_failure_is_not_reported_as_a_database_failure(
    seam, caplog, failure, raised
):
    """The other half of the handler above: what it must *not* claim.

    Registered on `OSError`, it answered the whole family — so a `/data/artifacts` the container's
    uid cannot write, which is the failure the image's `USER` directive makes likely (sec-08) and
    which reaches a route through `POST /api/admin/bundle/import`, came back as
    "database unreachable" with the log agreeing. The operator then has a message pointing at the
    one service that is working. The handler that reads the errno re-raises these two instead, so
    they are the 500 they were: httpx's ASGI transport re-raises rather than inventing the
    response `ServerErrorMiddleware` sends over a real socket, which is why this asserts the
    escape rather than a status code. Both errnos matter — 13 is the uid-1000 image meeting a
    host directory nobody chowned, 28 is the disk the same directory fills.

    The log is asserted too, because it is half the defect — 503 and the ERROR record are written
    by the same three lines, and a phone that never sees the body still leaves that record behind.
    """
    with pytest.raises(raised):
        await seam.get(f"/api/test-seam/artifacts/{failure}")

    assert "database unreachable" not in caplog.text


@pytest.mark.parametrize(("failure", "status"), [("conflict", 409), ("unreachable", 503)])
async def test_both_new_handlers_carry_the_slid_session_cookie(seam, failure, status):
    """§3.2's slide happens in the row before the route body runs, so a response Starlette builds
    for itself has to be given the Set-Cookie back or the cookie and the row disagree for the
    rest of the day. The three handlers that existed already do this; a new handler that forgets
    re-opens the bug quietly, on exactly the responses nobody looks at twice."""
    answer = await seam.get(f"/api/test-seam/slid/{failure}")

    assert answer.status_code == status
    assert answer.headers.get("set-cookie") == SLID_COOKIE
