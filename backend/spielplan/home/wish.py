"""The household's wish list, each person's Not for me, and the arrivals (decision 544, §4.2).

A want row whose title is not owned is on the list. One whose title is owned has arrived: it stands
as that person's banner until they dismiss it, which deletes the row, or see or rate the title, after
which the sweep deletes it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

import asyncpg
import httpx

from spielplan.connectors import registry
from spielplan.ledger.observations import KINDS, LIVE_LABEL_SQL, live_label_sql
from spielplan.push import send as push_send
from spielplan.scoring import serve

STATES: tuple[str, ...] = ("want", "not_for_me")

# Another member's score percentile among the unowned titles of the kind: "likely too", "maybe".
LIKELY_CDF = 0.70
MAYBE_CDF = 0.50


class NoSuchTitle(LookupError):
    pass


class Owned(ValueError):
    """`want` on a title the library already holds."""


async def state_for(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> dict[str, Any]:
    """The viewer's own row, as the title card reads it."""
    state = await conn.fetchval(
        "SELECT state FROM wish WHERE user_id = $1 AND title_id = $2", user_id, title_id
    )
    return {"state": state}


async def set_state(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, state: str
) -> dict[str, Any]:
    """Want it, Me too, or Not for me. A repeated answer keeps its `created_at`, the list's "since"."""
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    owned = await conn.fetchval("SELECT is_owned FROM title WHERE id = $1", title_id)
    if owned is None:
        raise NoSuchTitle(title_id)
    if owned and state == "want":
        raise Owned(title_id)
    await conn.execute(
        """
        INSERT INTO wish (user_id, title_id, state) VALUES ($1, $2, $3)
        ON CONFLICT (user_id, title_id) DO UPDATE SET state = excluded.state, created_at = now()
         WHERE wish.state <> excluded.state
        """,
        user_id, title_id, state,
    )
    return {"state": state}


async def clear(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> dict[str, Any]:
    """Remove from the list, or undo Not for me."""
    await conn.execute("DELETE FROM wish WHERE user_id = $1 AND title_id = $2", user_id, title_id)
    return {"state": None}


async def dismiss(conn: asyncpg.Connection, *, user_id: int, title_id: int) -> bool:
    """Closes an arrival's banner by deleting its want row; False when no arrival is there."""
    deleted = await conn.fetchval(
        """
        DELETE FROM wish w USING title t
         WHERE w.user_id = $1 AND w.title_id = $2 AND w.state = 'want'
           AND t.id = w.title_id AND t.is_owned
        RETURNING 1
        """,
        user_id, title_id,
    )
    return deleted is not None


async def retire_settled(conn: asyncpg.Connection) -> None:
    """Deletes each arrival its wanter has seen or rated, before the sweep can un-own its title and
    put it back on the list."""
    await conn.execute(
        f"""
        DELETE FROM wish w USING title t
         WHERE w.state = 'want' AND t.id = w.title_id AND t.is_owned
           AND (EXISTS (SELECT 1 FROM user_title ut WHERE ut.user_id = w.user_id
                         AND ut.title_id = t.id AND ut.state = 'seen')
                OR EXISTS (SELECT 1 FROM ({live_label_sql("w.user_id")}) lv
                            WHERE lv.title_id = t.id))
        """
    )


async def wanted_by(
    conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int]
) -> frozenset[int]:
    """Which of these titles this person wants: New in the library's mark."""
    if not title_ids:
        return frozenset()
    rows = await conn.fetch(
        "SELECT title_id FROM wish WHERE user_id = $1 AND state = 'want' AND title_id = ANY($2::int[])",
        user_id, [int(t) for t in title_ids],
    )
    return frozenset(int(r["title_id"]) for r in rows)


async def arrived(conn: asyncpg.Connection, *, user_id: int) -> list[dict[str, Any]]:
    """This person's wanted titles that are now owned and that they have neither seen nor rated."""
    rows = await conn.fetch(
        f"""
        WITH lv AS ({LIVE_LABEL_SQL})
        SELECT t.id, t.name, t.poster_path, t.jellyfin_id, w.created_at
          FROM wish w
          JOIN title t ON t.id = w.title_id AND t.is_owned
         WHERE w.user_id = $1 AND w.state = 'want'
           AND NOT EXISTS (SELECT 1 FROM user_title ut WHERE ut.user_id = $1
                            AND ut.title_id = t.id AND ut.state = 'seen')
           AND t.id NOT IN (SELECT title_id FROM lv)
         ORDER BY w.created_at, t.id
        """,
        user_id,
    )
    link = await registry.play_link(conn) if rows else None
    return [
        {
            "title_id": int(r["id"]),
            "name": r["name"],
            "poster_path": r["poster_path"],
            "since": r["created_at"],
            "play_url": link(r["jellyfin_id"]) if link and r["jellyfin_id"] else None,
        }
        for r in rows
    ]


async def summary(conn: asyncpg.Connection) -> dict[str, int]:
    """Home's wish list row: titles on the list, and how many more than one person wants."""
    row = await conn.fetchrow(
        """
        SELECT count(*) AS wanted, count(*) FILTER (WHERE n > 1) AS both
          FROM (SELECT w.title_id, count(*) AS n
                  FROM wish w
                  JOIN title t ON t.id = w.title_id AND NOT t.is_owned
                  JOIN app_user u ON u.id = w.user_id AND u.is_active
                                 AND u.role IN ('admin', 'member')
                 WHERE w.state = 'want'
                 GROUP BY w.title_id) listed
        """
    )
    return {"wanted": int(row["wanted"]), "both": int(row["both"])}


def link_for(kind: str, imdb_id: str | None, tmdb_id: int | None) -> str | None:
    """IMDb, else TMDB: what Copy the list hands to whoever gets the films."""
    if imdb_id:
        return f"https://www.imdb.com/title/{imdb_id}/"
    if tmdb_id:
        return f"https://www.themoviedb.org/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}"
    return None


def _likely(cdf: float | None) -> str | None:
    if cdf is None:
        return None
    if cdf >= LIKELY_CDF:
        return "likely"
    return "maybe" if cdf >= MAYBE_CDF else None


async def _cdfs(
    conn: asyncpg.Connection,
    *,
    member_ids: Sequence[int],
    title_ids: Sequence[int],
    bundle_version: str | None,
) -> dict[tuple[int, int], float]:
    """(member, title) -> the member's score percentile among unowned titles of its kind, read only
    where their own ratings rank that kind and they have not said Not for me."""
    if not member_ids or not title_ids or bundle_version is None:
        return {}
    personal = {
        m: set(await serve.personal_kinds(conn, user_id=m, kinds=KINDS, bundle_version=bundle_version))
        for m in member_ids
    }
    rows = await conn.fetch(
        """
        WITH ranked AS (
            SELECT us.user_id, us.title_id, us.kind,
                   percent_rank() OVER (PARTITION BY us.user_id, us.kind ORDER BY us.score) AS cdf
              FROM user_score us
              JOIN title t ON t.id = us.title_id AND NOT t.is_owned
             WHERE us.user_id = ANY($1::bigint[]) AND us.bundle_version = $2
        )
        SELECT user_id, title_id, kind, cdf FROM ranked r
         WHERE r.title_id = ANY($3::int[])
           AND NOT EXISTS (SELECT 1 FROM wish x WHERE x.user_id = r.user_id
                            AND x.title_id = r.title_id AND x.state = 'not_for_me')
        """,
        list(member_ids), bundle_version, list(title_ids),
    )
    return {
        (int(r["user_id"]), int(r["title_id"])): float(r["cdf"])
        for r in rows
        if r["kind"] in personal[int(r["user_id"])]
    }


async def household_list(
    conn: asyncpg.Connection, *, viewer_id: int, bundle_version: str | None
) -> dict[str, Any]:
    """Every member's wants, grouped by who wants each title, most wanters first."""
    members = await conn.fetch(
        "SELECT id, name, role, colour FROM app_user "
        "WHERE is_active AND role IN ('admin', 'member') ORDER BY id"
    )
    by_id = {int(m["id"]): m for m in members}
    rows = await conn.fetch(
        """
        SELECT t.id, t.kind, t.name, t.year, t.poster_path, t.imdb_id, t.tmdb_id,
               array_agg(w.user_id ORDER BY w.user_id) AS wanters, min(w.created_at) AS since
          FROM wish w
          JOIN title t ON t.id = w.title_id AND NOT t.is_owned
         WHERE w.state = 'want' AND w.user_id = ANY($1::bigint[])
         GROUP BY t.id
         ORDER BY min(w.created_at), t.id
        """,
        list(by_id),
    )
    cdfs = await _cdfs(
        conn, member_ids=list(by_id), title_ids=[int(r["id"]) for r in rows],
        bundle_version=bundle_version,
    )

    def person(member_id: int) -> dict[str, Any]:
        m = by_id[member_id]
        return {"id": member_id, "name": m["name"], "role": m["role"], "colour": m["colour"]}

    groups: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    for r in rows:
        title_id = int(r["id"])
        wanters = tuple(int(u) for u in r["wanters"])
        link = link_for(r["kind"], r["imdb_id"], r["tmdb_id"])
        groups.setdefault(wanters, []).append({
            "title_id": title_id,
            "kind": r["kind"],
            "name": r["name"],
            "year": r["year"],
            "poster_path": r["poster_path"],
            "since": r["since"],
            "mine": viewer_id in wanters,
            "others_likely": [
                {**person(m), "likely": _likely(cdfs.get((m, title_id)))}
                for m in sorted(by_id, key=lambda m: (m != viewer_id, m))
                if m not in wanters
            ],
            "link": link,
        })
    ordered = sorted(groups, key=lambda w: (-len(w), viewer_id not in w, w))
    copy_lines = [
        " ".join(
            part for part in (
                item["name"], f"({item['year']})" if item["year"] else "", item["link"] or ""
            ) if part
        )
        for wanters in ordered
        for item in groups[wanters]
    ]
    return {
        "groups": [
            {
                "wanters": [person(m) for m in sorted(w, key=lambda m: (m != viewer_id, m))],
                "items": groups[w],
            }
            for w in ordered
        ],
        "copy_text": "\n".join(copy_lines),
    }


async def announce_arrivals(
    conn: asyncpg.Connection,
    title_ids: Collection[int],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """A best-effort push to each member who wants a title the sweep just made owned; Home's banner
    stands regardless. Returns how many (member, title) pairs were sent to."""
    if not title_ids:
        return 0
    rows = await conn.fetch(
        """
        SELECT w.user_id, t.id, t.name
          FROM wish w JOIN title t ON t.id = w.title_id
          JOIN app_user u ON u.id = w.user_id AND u.is_active
         WHERE w.state = 'want' AND w.title_id = ANY($1::int[])
         ORDER BY t.id, w.user_id
        """,
        sorted(int(t) for t in title_ids),
    )
    for row in rows:
        await push_send.send_to_user(
            conn,
            int(row["user_id"]),
            {
                "kind": "wish.arrived",
                "title_id": int(row["id"]),
                "title": "Spielplan",
                "body": f"{row['name']} is here",
                "tag": f"arrived:{int(row['id'])}",
                "url": "/",
            },
            transport=transport,
        )
    return len(rows)


__all__ = [
    "LIKELY_CDF",
    "MAYBE_CDF",
    "STATES",
    "NoSuchTitle",
    "Owned",
    "announce_arrivals",
    "arrived",
    "clear",
    "dismiss",
    "household_list",
    "link_for",
    "retire_settled",
    "set_state",
    "state_for",
    "summary",
    "wanted_by",
]
