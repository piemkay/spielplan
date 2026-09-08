"""Shared route dependencies. Spec v2.1 §3.1, §3.2."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, Response, status

from spielplan.core import auth
from spielplan.core.config import settings
from spielplan.db import pool

log = logging.getLogger("spielplan.api.deps")

# Longer than any request should ever wait for a *connection* (the query itself is unbounded —
# the importer's COPY and the nightly refit legitimately run for minutes, which is why there is
# no `command_timeout` on the pool), and short enough that a saturated pool answers rather than
# hangs. §7.3's playback poll runs every 60 s, so a phone that waits ten seconds and is told the
# database is unavailable retries on its own; a phone that waits forever holds a socket open until
# the browser gives up, with nothing in the log to say why. [M4.7 schema-pool-acquire]
_ACQUIRE_TIMEOUT_S = 10


async def db() -> asyncpg.Connection:
    """One pooled connection for the whole request, bounded at the acquire.

    Unbounded, this dependency turned "the database is slow" into "every phone hangs": the pool
    holds ten connections, `api/deps` keeps one for the life of a request, and nothing anywhere
    passed a timeout — measured, ten in-flight health probes against a blocked database took the
    pool to idle 0 and an eleventh request to `/api/setup/state` never answered.

    The guard is around the acquire alone rather than around the `yield`, because a `TimeoutError`
    raised *inside* a route body means something else entirely (it is what `asyncio.TimeoutError`
    aliases on 3.11+, and asyncpg raises it for a query timeout too) and must keep travelling to
    `app.py`'s handlers rather than be answered from here. That is what costs the `async with`:
    the release has to be explicit, and it is the same call asyncpg's own acquire context manager
    makes on exit. [M4.7 dd-health-probes, schema-pool-acquire]

    "Unavailable" and not "busy", because this bound covers two failures the acquire cannot tell
    apart: ten peers holding the ten connections, and a pool with nothing to hand out spending the
    whole ten seconds inside asyncpg's `_get_new_connection` on a TCP connect that never
    completes. §2 supports "a Postgres outside this compose file", so the second is a VPN dropping
    mid-session — and it is the case where the class-based handlers in `app.py` would have said
    "database unreachable" with a traceback had the peer refused one second earlier instead of
    going silent. The census is in the log rather than in the detail because that is where a
    reader can act on it: size 0 is a peer that is not there, size 10 idle 0 is a pool in use.
    [cycle 3 finding 7]
    """
    connections = pool.pool()
    try:
        conn = await connections.acquire(timeout=_ACQUIRE_TIMEOUT_S)
    except TimeoutError:
        log.warning(
            "no pooled connection within %ss: pool size %d, idle %d",
            _ACQUIRE_TIMEOUT_S,
            connections.get_size(),
            connections.get_idle_size(),
        )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from None
    try:
        yield conn
    finally:
        await connections.release(conn)


DB = Annotated[asyncpg.Connection, Depends(db)]


@asynccontextmanager
async def write_txn(conn: asyncpg.Connection, *, lock: str | None = None):
    """The house idiom for a route that issues more than one write (§3.1, §4.2).

    Not a transactional `db` dependency: the minutes-long bundle import and every read-only
    route must not run inside one request-scoped transaction, and asyncpg's pool reset already
    rolls back on release. Not a FastAPI yield-dependency either — its exit runs after the
    response is produced, so an HTTPException raised in the route would commit the partial
    write. An explicit `async with` in the body is plain control flow and rolls back.

    `lock` names a seam whose writers must not interleave — §3.1's first boot is the case that
    forced it, where a check-then-insert let two callers each become the only admin. It is taken
    as the first statement inside the transaction, so the loser waits and then reads what the
    winner committed, and it is released with the transaction whichever way that ends. The
    single-argument `hashtext(...)::bigint` form is a different lock space from the (int, int)
    pairs the domain packages take (`tonight/play.py:611`), so a name here cannot collide with a
    number there.
    """
    async with conn.transaction():
        if lock is not None:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", lock)
        yield conn


def set_session_cookie(response: Response, sid: str) -> None:
    """One place that knows the cookie's shape, so the passkey and password paths cannot
    drift apart on HttpOnly, SameSite or the sliding window (§3.2).

    It lives here rather than in `api/auth.py` because `current_user` below re-issues it on the
    slide, and a dependency importing a router would invert the layering.
    """
    cfg = settings()
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.seal_session_id(sid),
        max_age=cfg.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=cfg.public_url.startswith("https://"),
        path="/",
    )


async def current_user(request: Request, response: Response, conn: DB) -> auth.SessionUser:
    sid = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    if not sid:
        # Missing, or signed under a SESSION_SECRET that has since been rotated (§2).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    user = await auth.load_session(conn, sid)
    if user is None:
        # The row is gone or lapsed, so the cookie naming it is dead weight the browser would
        # keep sending for the rest of its 90 days. Starlette builds the error response itself
        # and discards the one this dependency was handed, so the clearing Set-Cookie has to
        # travel on the exception to survive.
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "session expired",
            headers={"set-cookie": response.headers["set-cookie"]},
        )
    if user.session_slid:
        # §3.2's window slides in the browser only if the cookie is re-issued with it, and the
        # row moves at most once a day, so this costs one Set-Cookie a day rather than one per
        # request.
        set_session_cookie(response, sid)
        # `load_session` has already committed the slide on this autocommit connection, so if
        # the route now refuses — a 403 from the three dependencies below, the admin re-prompt's
        # 401, a 404, a 422 — Starlette builds its own response and discards this one, and the
        # cookie goes nowhere. There is no second chance: `last_seen_at` is fresh, so no later
        # request that day re-issues it either, and the browser's cookie ends the day staler
        # than the row it names. core/auth.py states the contract as "slides in the row and in
        # the cookie together or not at all", so the header rides on the request state and
        # `app.py`'s error handlers put it back on whatever response actually goes out.
        request.state.slid_session_cookie = response.headers["set-cookie"]
    return user


CurrentUser = Annotated[auth.SessionUser, Depends(current_user)]


def carry_slid_session_cookie(request: Request, response: Response) -> Response:
    """Re-attach the slide's Set-Cookie to a response Starlette built for itself.

    The other half of `current_user`'s note above, kept here because `set_session_cookie` is
    already the one place that knows the cookie's shape. It is a no-op on every request that did
    not slide, and it never overwrites a Set-Cookie the response already carries — the lapsed
    session's clearing cookie is threaded through `headers=` on its own exception and wins.
    """
    slid = getattr(request.state, "slid_session_cookie", None)
    if slid and "set-cookie" not in response.headers:
        response.headers.append("set-cookie", slid)
    return response


async def active_user(user: CurrentUser) -> auth.SessionUser:
    """§3.1: an account created with a one-time password is *locked to a password change at
    first login*. Enforcing that here rather than in the UI means no route can be reached
    around it."""
    if user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "password change required before this account can be used",
        )
    return user


ActiveUser = Annotated[auth.SessionUser, Depends(active_user)]


async def credentialed_user(user: ActiveUser) -> auth.SessionUser:
    """§3.2: the password is the account credential and the PIN a convenience derived from it,
    so managing credentials requires the credential rather than the convenience."""
    if user.auth_method == "pin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "sign in with your password or a passkey to manage credentials",
        )
    return user


CredentialedUser = Annotated[auth.SessionUser, Depends(credentialed_user)]


async def admin_user(user: ActiveUser) -> auth.SessionUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    if user.admin_reauth_required():
        # §3.2: "admin routes re-prompt after 24 h".
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "admin re-authentication required",
            headers={"X-Spielplan-Reauth": "admin"},
        )
    return user


AdminUser = Annotated[auth.SessionUser, Depends(admin_user)]


def artifacts(request: Request):
    return request.app.state.artifacts
