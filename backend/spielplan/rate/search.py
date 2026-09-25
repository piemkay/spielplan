"""Rate's "a title you know" search. Spec v2.1 §6.1, §6.0.

§6.1 is the only place a verdict is given, and until this the only way to rate a film the person
already knew was §6.0's Mark seen, then wait for the queue to come round to it behind the card
already on the table. The household test found exactly that: one member marked Dunkirk seen and
never reached it, another used Mark seen as a "queue this" gesture and wrote a seen state for
three titles she then answered "not seen". So Rate carries a search, and choosing a hit pins
that title as the head of the §6.1 queue -- §6.0's banner mechanism, which the title card's
"Rate it" link uses too -- so the verdict is still given on §6.1's card, under its card token,
its block counter, its Undo and its after-the-tap reveal. [owner instruction of 2026-09-25 after
the first household user test]

Ranked for a person looking for a film they remember rather than browsing: an exact name first,
then a name that starts with what was typed, then the rest; inside each, a title in the library
first and then the most widely rated. The catalogue grid orders by year, which would bury Heat
(1995) under every newer title with "heat" in its name.

A hit the person already rated says so and is not offered: the queue serves no rated title (a
re-rating on purpose is not something this surface asks), and a pin that silently did nothing
is the failure this search exists to end.
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

    Both kinds, deliberately: §4.1 rule 5 partitions what a surface RANKS, and this ranks nothing
    the model believes -- it finds a name. A hit outside the session's kinds is pinned all the
    same, and `session.ensure_card` widens the session to its kind so the counter keeps naming
    the partition it serves (proposal 46).
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
            # The person's own live label, by name: it is their answer, not the model's belief,
            # so it is not an anchor (§6.1) -- and it is why this hit is not offered.
            "rated": None if r["rated"] is None else VERDICT_LABELS[int(r["rated"])],
        }
        for r in rows
    ]


__all__ = ["DEFAULT_LIMIT", "find"]
