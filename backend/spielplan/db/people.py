"""People by name, for the people picker (decision 557). One human is folded
across their person rows as the title card folds them (`library.fold_credits`)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import asyncpg

from spielplan.art.hosts import servable
from spielplan.db.library import ids_agree, lead_person
from spielplan.derive import ids as derive_ids

_WORDS = re.compile(r"[^\W_]+")

# A human can be several person rows, folded after the read, so the read fetches more names than it
# answers with.
_FETCH_FACTOR = 4

# Both sides as space-separated alnum words, so "vill" starts a word of "Denis Villeneuve" and
# punctuation never decides. Ranked by name, so every row of a name arrives together.
_SEARCH = """
    WITH hit AS (
        SELECT p.id AS person_id, p.name, p.imdb_id, p.tmdb_id, p.profile_path,
               array_agg(DISTINCT c.title_id) AS titles,
               array_agg(DISTINCT c.title_id) FILTER (WHERE t.is_owned) AS owned,
               array_agg(c.role_class) FILTER (WHERE c.role_class IS NOT NULL) AS roles
          FROM person p
          JOIN credit c ON c.person_id = p.id
          JOIN title t ON t.id = c.title_id AND t.kind = ANY($2::text[])
         WHERE ' ' || regexp_replace(lower(p.name), '[^[:alnum:]]+', ' ', 'g') LIKE $1
         GROUP BY p.id
    ),
    top AS (
        SELECT lower(name) AS key
          FROM hit
         GROUP BY lower(name)
         ORDER BY max(cardinality(COALESCE(owned, '{}'))) DESC, max(cardinality(titles)) DESC,
                  lower(name)
         LIMIT $3
    )
    SELECT h.* FROM hit h WHERE lower(h.name) IN (SELECT key FROM top) ORDER BY h.person_id
"""


async def search_people(
    conn: asyncpg.Connection, *, q: str, kinds: Sequence[str], limit: int = 8
) -> list[dict[str, Any]]:
    """People credited on a title of `kinds` with a word of their name starting with `q`, each human
    once with their most frequent role and how many titles of `kinds` they are on, owned first."""
    needle = " ".join(_WORDS.findall(q.lower()))
    if len(needle) < 2:
        return []
    rows = await conn.fetch(_SEARCH, f"% {needle}%", list(kinds), limit * _FETCH_FACTOR)
    names: dict[str, list[asyncpg.Record]] = {}
    for row in rows:
        names.setdefault(derive_ids.loose_name(row["name"]) or f"#{row['person_id']}", []).append(row)

    people = []
    for members in names.values():
        for part in [members] if ids_agree(members) else [[m] for m in members]:
            roles = Counter(role for m in part for role in m["roles"] or ())
            people.append({
                **_named(part),
                "role": min(roles, key=lambda r: (-roles[r], r)) if roles else None,
                "owned": len({t for m in part for t in m["owned"] or ()}),
                "titles": len({t for m in part for t in m["titles"]}),
            })
    people.sort(key=lambda p: (-p["owned"], -p["titles"], p["name"].lower(), p["person_id"]))
    return people[:limit]


def _named(members: Sequence[Any]) -> dict[str, Any]:
    lead = lead_person(members)
    return {
        "person_ids": sorted(m["person_id"] for m in members),
        "person_id": lead["person_id"],
        "name": lead["name"],
        # What `/api/art/person/{person_id}` can serve, as the card's credits carry it (decision 528).
        "photo": servable(lead["profile_path"]),
    }
