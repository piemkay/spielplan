"""Home's sticky notices put away (decision 554, §6.0): the pending row, the set-up notice and the
wish-list row hide until the next midnight in §2's `TZ`, or sooner when something new joins them."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from spielplan.core.config import settings

NOTICES: tuple[str, ...] = ("pending", "setup", "wish_list")

# Still hidden: put away since today's local midnight, and nothing new has joined it since. The
# pending row is joined by a title written seen; the wish list by a want on an unowned title.
_HIDDEN_SQL = """
SELECT n.notice
  FROM notice_hidden n
 WHERE n.user_id = $1 AND n.hidden_at >= $2
   AND CASE n.notice
         WHEN 'pending' THEN NOT EXISTS (
             SELECT 1 FROM user_title ut
              WHERE ut.user_id = n.user_id AND ut.state = 'seen'
                AND ut.state_changed_at > n.hidden_at)
         WHEN 'wish_list' THEN NOT EXISTS (
             SELECT 1 FROM wish w
               JOIN title t ON t.id = w.title_id AND NOT t.is_owned
               JOIN app_user u ON u.id = w.user_id AND u.is_active
                              AND u.role IN ('admin', 'member')
              WHERE w.state = 'want' AND w.created_at > n.hidden_at)
         ELSE true
       END
"""


def _zone() -> tzinfo:
    try:
        return ZoneInfo(settings().tz)
    except (ValueError, ZoneInfoNotFoundError):
        return UTC


def _midnight(now: datetime) -> datetime:
    return now.astimezone(_zone()).replace(hour=0, minute=0, second=0, microsecond=0)


async def hide(conn: asyncpg.Connection, *, user_id: int, notice: str) -> dict[str, Any]:
    hidden_at = await conn.fetchval(
        """
        INSERT INTO notice_hidden (user_id, notice) VALUES ($1, $2)
        ON CONFLICT (user_id, notice) DO UPDATE SET hidden_at = now()
        RETURNING hidden_at
        """,
        user_id, notice,
    )
    return {"notice": notice, "hidden_at": hidden_at}


async def unhide(conn: asyncpg.Connection, *, user_id: int, notice: str) -> dict[str, Any]:
    await conn.execute(
        "DELETE FROM notice_hidden WHERE user_id = $1 AND notice = $2", user_id, notice
    )
    return {"notice": notice, "hidden_at": None}


async def hidden(
    conn: asyncpg.Connection, *, user_id: int, now: datetime | None = None
) -> frozenset[str]:
    rows = await conn.fetch(_HIDDEN_SQL, user_id, _midnight(now or datetime.now(UTC)))
    return frozenset(r["notice"] for r in rows)


async def apply_hidden(
    conn: asyncpg.Connection, *, user_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Home's payload with the member's hidden notices taken out; the wish list stays open from You.
    `setup_hidden` says a set-up is still owed while its notice is away."""
    gone = await hidden(conn, user_id=user_id)
    if "pending" in gone:
        payload["banner"] = None
    payload["setup_hidden"] = "setup" in gone and payload["setup_notice"] is not None
    if payload["setup_hidden"]:
        payload["setup_notice"] = None
    payload["wish"] = {**payload["wish"], "hidden": "wish_list" in gone}
    return payload


__all__ = ["NOTICES", "apply_hidden", "hidden", "hide", "unhide"]
