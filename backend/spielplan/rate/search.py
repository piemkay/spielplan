"""Rate's "a title you know" search: a chosen hit is pinned as the head of the §6.1 queue.

Ranked for recall: exact name, then prefix, then the rest; owned, then most rated, within each.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from spielplan.db.library import _like_needle
from spielplan.rate import LIVE_LABEL, VERDICT_LABELS

# Enough to find a remembered film on a phone without the list outgrowing the screen.
DEFAULT_LIMIT = 8

_FIND = f"""
WITH label AS ({LIVE_LABEL})
SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.is_owned,
       l.value AS rated
  FROM title t
  LEFT JOIN title_prior tp ON tp.title_id = t.id
  LEFT JOIN label l ON l.title_id = t.id
 WHERE lower(t.name) LIKE $2
    OR EXISTS (SELECT 1 FROM title_alias a WHERE a.title_id = t.id AND lower(a.alias) LIKE $2)
 ORDER BY lower(t.name) = $3 DESC,
          starts_with(lower(t.name), $3) DESC,
          t.is_owned DESC,
          COALESCE(tp.item_n, 0) DESC,
          t.year DESC NULLS LAST,
          t.id
 LIMIT $4
"""


async def find(
    conn: asyncpg.Connection, *, user_id: int, q: str, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """The titles a remembered name most likely means, each saying whether it is rated.

    Both kinds: this ranks nothing the model believes, so §4.1 rule 5 does not apply.
    """
    needle = (q or "").strip().lower()
    if not needle:
        return []
    rows = await conn.fetch(_FIND, user_id, _like_needle(needle), needle, limit)
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "name": r["name"],
            "year": r["year"],
            "runtime_min": r["runtime_min"],
            "poster_path": r["poster_path"],
            "is_owned": r["is_owned"],
            # Their own answer, not an anchor (§6.1); a rated hit is not offered.
            "rated": None if r["rated"] is None else VERDICT_LABELS[int(r["rated"])],
        }
        for r in rows
    ]


__all__ = ["DEFAULT_LIMIT", "find"]
