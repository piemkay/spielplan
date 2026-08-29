"""Jellyfin item -> `title` row. Spec v2.1 §7.1, under §4.1 rules 5 and 6.

"Identity: ProviderIds -> `imdb/tmdb/tvdb` + `jellyfin_id`; upsert into `title` by the ported
fill-never-clobber resolver."

Two corpus measurements decide the whole shape of this module:

  * `imdb_id` is NULL on 21% of titles, so it can never be *the* join key — only one of
    several, tried in order of how much it identifies.
  * 315 `tmdb_id` values are legitimately duplicated, almost all of them a movie and a series
    that share an id. So every tmdb/tvdb match is qualified by `kind`, and §4.1 rule 6 bans
    the UNIQUE constraint that would otherwise "fix" the duplicates by losing one of them.

Fill-never-clobber: a matched row has its NULL identity columns filled and its non-NULL ones
left exactly as they were. The bundle is derived from a curated corpus and Jellyfin's
ProviderIds are whatever a scraper guessed; when they disagree the corpus wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

log = logging.getLogger("spielplan.jellyfin.resolve")

KIND_OF_TYPE = {"movie": "movie", "series": "series"}
# The identity columns Jellyfin may contribute. `jellyfin_id` is deliberately not here: it is
# this server's own item id, not a provider's opinion, so it is set rather than filled.
FILLABLE = ("imdb_id", "tmdb_id", "tvdb_id")


@dataclass
class ResolveReport:
    matched: int = 0
    unmatched: list[str] = field(default_factory=list)
    filled: dict[str, int] = field(default_factory=dict)
    relinked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "unmatched": len(self.unmatched),
            "unmatched_names": self.unmatched[:20],
            "filled": self.filled,
            "relinked": self.relinked,
        }


def provider_ids(item: dict) -> dict[str, str]:
    """Jellyfin's ProviderIds with the keys lowercased.

    Different Jellyfin versions and plugins disagree about capitalisation ("Tmdb", "TMDB"),
    and a case-sensitive lookup here would silently drop identity on some libraries.
    """
    raw = item.get("ProviderIds") or {}
    return {str(k).lower(): str(v) for k, v in raw.items() if v}


def kind_of(item: dict) -> str | None:
    return KIND_OF_TYPE.get(str(item.get("Type") or "").lower())


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    digits = "".join(c for c in str(value) if c.isdigit())
    return int(digits) if digits else None


def identity(item: dict) -> dict[str, Any]:
    """The identity columns this item can contribute, in `title`'s own column names."""
    ids = provider_ids(item)
    return {
        "imdb_id": ids.get("imdb") or None,
        "tmdb_id": _as_int(ids.get("tmdb")),
        "tvdb_id": _as_int(ids.get("tvdb")),
    }


async def resolve_title_id(conn: asyncpg.Connection, item: dict) -> int | None:
    """Find the `title.id` this Jellyfin item is, or None.

    The order is by how much each key identifies. `imdb_id` is globally unique across kinds
    when present; tmdb and tvdb ids are only unique *within* a kind (§4.1 rule 6); the
    name/year fallback is last because it is the only one that can be wrong.
    """
    kind = kind_of(item)
    if kind is None:
        return None

    jellyfin_id = str(item.get("Id") or "")
    if jellyfin_id:
        found = await conn.fetchval(
            "SELECT id FROM title WHERE jellyfin_id = $1 AND kind = $2", jellyfin_id, kind
        )
        if found:
            return found

    ids = identity(item)
    if ids["imdb_id"]:
        found = await conn.fetchval("SELECT id FROM title WHERE imdb_id = $1", ids["imdb_id"])
        if found:
            return found
    for column in ("tmdb_id", "tvdb_id"):
        if ids[column] is not None:
            found = await conn.fetchval(
                f"SELECT id FROM title WHERE {column} = $1 AND kind = $2", ids[column], kind
            )
            if found:
                return found

    # Last resort. Year included because "The Office" without one matches two shows, and an
    # alias lookup because the bundle carries release-name variants Jellyfin often uses.
    name = str(item.get("Name") or "").strip()
    if not name:
        return None
    year = _as_int(item.get("ProductionYear"))
    return await conn.fetchval(
        """
        SELECT t.id FROM title t
         WHERE t.kind = $1
           AND ($3::int IS NULL OR t.year = $3)
           AND (lower(t.name) = lower($2)
                OR lower(coalesce(t.original_name, '')) = lower($2)
                OR EXISTS (SELECT 1 FROM title_alias a
                            WHERE a.title_id = t.id AND lower(a.alias) = lower($2)))
         ORDER BY t.id
         LIMIT 1
        """,
        kind, name, year,
    )


async def upsert_item(conn: asyncpg.Connection, item: dict, report: ResolveReport) -> int | None:
    """Attach one Jellyfin item to its title, filling only what is NULL.

    Returns the title id, or None when the item does not resolve. An unresolved item is
    *reported*, never invented: `title.id` is carried over verbatim from the corpus (§4.2), so
    minting one here would create a row no bundle can ever reconcile with. Acquiring genuinely
    new titles is §8's pipeline, which arrives at M5.
    """
    title_id = await resolve_title_id(conn, item)
    if title_id is None:
        report.unmatched.append(str(item.get("Name") or item.get("Id") or "?"))
        return None

    ids = identity(item)
    jellyfin_id = str(item.get("Id") or "") or None

    row = await conn.fetchrow(
        "SELECT imdb_id, tmdb_id, tvdb_id, jellyfin_id FROM title WHERE id = $1", title_id
    )
    fills = {c: ids[c] for c in FILLABLE if ids[c] is not None and row[c] is None}
    if row["jellyfin_id"] and jellyfin_id and row["jellyfin_id"] != jellyfin_id:
        # The library was rebuilt, or two Jellyfin items resolve to one title. The current
        # server's id is the useful one — it is what the deep link and the Played write need —
        # so it is replaced, and the fact is counted rather than swallowed.
        report.relinked += 1

    sets = [f"{column} = ${i + 2}" for i, column in enumerate(fills)]
    values = list(fills.values())
    # §7.2: "is_owned … re-derived from Jellyfin, never trusted stale". Seeing the item in the
    # library IS the derivation, and owned_checked_at is what makes a later sweep able to tell
    # "still owned" from "not looked at since".
    sets += [
        f"jellyfin_id = ${len(values) + 2}",
        "is_owned = true",
        "owned_checked_at = now()",
        "updated_at = now()",
    ]
    values.append(jellyfin_id)
    # Guarded, because this runs once per item per linked user every fifteen minutes. An
    # unconditional write over an unchanged library is ~11,000 dead row versions per user per
    # cycle — two million a day on a box the spec sizes at 4 vCPU — and every one of them is
    # work for autovacuum and noise in `updated_at`. The re-derivation still happens; only the
    # write is skipped when it would change nothing that matters, and `owned_checked_at` is
    # refreshed hourly so "still owned" stays distinguishable from "not looked at since".
    guard = (
        f"({' OR '.join(f'{c} IS NULL' for c in fills)} OR " if fills else "("
    ) + (
        f"jellyfin_id IS DISTINCT FROM ${len(values) + 1} OR NOT is_owned "
        "OR owned_checked_at IS NULL OR owned_checked_at < now() - interval '1 hour')"
    )
    await conn.execute(
        f"UPDATE title SET {', '.join(sets)} WHERE id = $1 AND {guard}", title_id, *values
    )

    report.matched += 1
    for column in fills:
        report.filled[column] = report.filled.get(column, 0) + 1
    return title_id


async def upsert_items(conn: asyncpg.Connection, items: list[dict]) -> ResolveReport:
    report = ResolveReport()
    for item in items:
        await upsert_item(conn, item, report)
    if report.unmatched:
        log.info(
            "%d Jellyfin item(s) did not resolve to a title: %s",
            len(report.unmatched), ", ".join(report.unmatched[:5]),
        )
    return report


__all__ = [
    "ResolveReport",
    "identity",
    "kind_of",
    "provider_ids",
    "resolve_title_id",
    "upsert_item",
    "upsert_items",
]
