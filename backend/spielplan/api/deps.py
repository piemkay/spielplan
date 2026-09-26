"""Shared route dependencies. Spec v2.1 §3.1, §3.2."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, Response, WebSocket, WebSocketException, status

from spielplan.core import auth
from spielplan.core.config import settings
from spielplan.db import pool

log = logging.getLogger("spielplan.api.deps")

# Seconds to wait for a connection, not a query (the importer and refit run for minutes, so the pool
# has no command_timeout). A saturated pool answers 503 rather than hangs.
_ACQUIRE_TIMEOUT_S = 10


async def db() -> asyncpg.Connection:
    """Bounded at the acquire only: a TimeoutError inside the route body is a query timeout and must
    reach `app.py`'s handlers. The pool census goes to the log."""
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
    """The house idiom for multi-write routes. Not a transactional `db` dependency, nor a yield dependency
    (its exit runs after the response, so an HTTPException would commit). `lock` takes a named advisory
    xact lock; `hashtext` keys cannot collide with the domain's (int, int) locks."""
    async with conn.transaction():
        if lock is not None:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", lock)
        yield conn


def set_session_cookie(response: Response, sid: str) -> None:
    """The one place that knows the cookie's shape; here since `current_user` re-issues it."""
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
        # Missing, or signed under a rotated SESSION_SECRET (§2).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    user = await auth.load_session(conn, sid)
    if user is None:
        # No clearing Set-Cookie: it addresses the cookie by name, so on a slow link it could end a session
        # the browser signed into after this request. The dead cookie just keeps drawing 401s.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    if user.session_slid:
        # Re-issued only when the row slid, at most once a day (§3.2).
        set_session_cookie(response, sid)
        # The slide is committed; if the route now raises, Starlette discards this response, so `app.py`'s
        # handlers put the header back from request state.
        request.state.slid_session_cookie = response.headers["set-cookie"]
    return user


CurrentUser = Annotated[auth.SessionUser, Depends(current_user)]


def carry_slid_session_cookie(request: Request, response: Response) -> Response:
    """The other half of `current_user`'s slide. Never overwrites a Set-Cookie already present."""
    slid = getattr(request.state, "slid_session_cookie", None)
    if slid and "set-cookie" not in response.headers:
        response.headers.append("set-cookie", slid)
    return response


async def active_user(user: CurrentUser) -> auth.SessionUser:
    """§3.1's first-login lock, enforced here so no route can be reached around it."""
    if user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "password change required before this account can be used",
        )
    return user


ActiveUser = Annotated[auth.SessionUser, Depends(active_user)]


# The same gates, holding the pool only for the session read: sixty poster requests on `DB` would
# drain it (decision 483). Dependencies, so the gating sweeps still see them.


@asynccontextmanager
async def brief_connection():
    """One pooled connection for one piece of work, released on exit, bounded like `db`."""
    connections = pool.pool()
    try:
        conn = await connections.acquire(timeout=_ACQUIRE_TIMEOUT_S)
    except TimeoutError:
        log.warning(
            "no pooled connection within %ss for a brief read: pool size %d, idle %d",
            _ACQUIRE_TIMEOUT_S,
            connections.get_size(),
            connections.get_idle_size(),
        )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from None
    try:
        yield conn
    finally:
        await connections.release(conn)


async def current_user_brief(request: Request, response: Response) -> auth.SessionUser:
    """`current_user`, on a connection held for the session read alone - the slide included."""
    async with brief_connection() as conn:
        return await current_user(request, response, conn)


CurrentUserBrief = Annotated[auth.SessionUser, Depends(current_user_brief)]


async def active_user_brief(user: CurrentUserBrief) -> auth.SessionUser:
    """`active_user`'s predicate, called rather than copied."""
    return await active_user(user)


ActiveUserBrief = Annotated[auth.SessionUser, Depends(active_user_brief)]


# The same gates for a socket, as dependencies so the gating sweeps can see them (decision 225).


async def current_user_ws(socket: WebSocket) -> auth.SessionUser:
    """Closes with 1008 instead of a 401. Takes no `DB`: a yield dependency would hold a pool connection
    for the socket's whole evening. The acquire is bounded and closes 1011 on timeout; the slide's
    cookie half cannot ride a handshake."""
    sid = auth.open_session_cookie(socket.cookies.get(auth.SESSION_COOKIE))
    if not sid:
        # Missing, or signed under a rotated SESSION_SECRET (§2).
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "not signed in")
    connections = pool.pool()
    try:
        conn = await connections.acquire(timeout=_ACQUIRE_TIMEOUT_S)
    except TimeoutError:
        log.warning(
            "no pooled connection within %ss for a tonight socket: pool size %d, idle %d",
            _ACQUIRE_TIMEOUT_S,
            connections.get_size(),
            connections.get_idle_size(),
        )
        raise WebSocketException(
            status.WS_1011_INTERNAL_ERROR, "database unavailable"
        ) from None
    try:
        user = await auth.load_session(conn, sid)
    finally:
        await connections.release(conn)
    if user is None:
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "session expired")
    return user


CurrentUserWS = Annotated[auth.SessionUser, Depends(current_user_ws)]


async def active_user_ws(user: CurrentUserWS) -> auth.SessionUser:
    """A sibling, not a reuse: an HTTPException under a WebSocket scope reaches an HTTP handler."""
    if user.must_change_password:
        raise WebSocketException(
            status.WS_1008_POLICY_VIOLATION,
            "password change required before this account can be used",
        )
    return user


ActiveUserWS = Annotated[auth.SessionUser, Depends(active_user_ws)]


async def credentialed_user(user: ActiveUser) -> auth.SessionUser:
    """Managing credentials needs the credential, not the PIN convenience (§3.2)."""
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
