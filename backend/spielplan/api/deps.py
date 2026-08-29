"""Shared route dependencies. Spec v2.1 §3.1, §3.2."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, status

from spielplan.core import auth
from spielplan.db import pool


async def db() -> asyncpg.Connection:
    async with pool.acquire() as conn:
        yield conn


DB = Annotated[asyncpg.Connection, Depends(db)]


async def current_user(request: Request, conn: DB) -> auth.SessionUser:
    sid = auth.open_session_cookie(request.cookies.get(auth.SESSION_COOKIE))
    if not sid:
        # Missing, or signed under a SESSION_SECRET that has since been rotated (§2).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    user = await auth.load_session(conn, sid)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    return user


CurrentUser = Annotated[auth.SessionUser, Depends(current_user)]


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
