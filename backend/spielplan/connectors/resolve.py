"""Jellyfin item -> `title` row (§7.1, §4.1 rules 5-6): the ported fill-never-clobber resolver.

tmdb/tvdb matches are qualified by kind (ids repeat across kinds). One pass over the whole library
also writes the copy map and elects one representative `jellyfin_id` per title, deterministically.
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


@dataclass(frozen=True)
class UnmatchedItem:
    """One item this resolver refused: the three fields the acquisition half keys on (decision 370).

    `provider_ids` is normalised (`provider_ids()`), which is idempotent under `identity()`.
    """

    jellyfin_id: str | None
    name: str
    provider_ids: dict[str, str]


@dataclass
class ResolveReport:
    matched: int = 0
    unmatched: list[UnmatchedItem] = field(default_factory=list)
    filled: dict[str, int] = field(default_factory=dict)
    relinked: int = 0
    # Sweep-level facts, so nothing is re-resolved per user: every resolved copy (`items`), the
    # complete owned set (`matched_title_ids`, trusted only on a completed read), and each
    # title's `kind` (decision 210's series rule).
    items: dict[str, int] = field(default_factory=dict)
    matched_title_ids: set[int] = field(default_factory=set)
    kinds: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            # Copies vs titles: `matched` counts items, this the titles behind them.
            "matched_titles": len(self.matched_title_ids),
            "unmatched": len(self.unmatched),
            # Names only, bounded at twenty (decision 370).
            "unmatched_names": [entry.name for entry in self.unmatched[:20]],
            "filled": self.filled,
            "relinked": self.relinked,
        }


def provider_ids(item: dict) -> dict[str, str]:
    """Jellyfin's ProviderIds with the keys lowercased: versions and plugins disagree on case."""
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

    By how much each key identifies: jellyfin_id, imdb_id, then tmdb/tvdb within the kind, and
    last name + year, which refuses rather than guesses.
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

    # Last resort: refuse with no year or on two candidates. A refusal is reported; a wrong match
    # silently writes ownership onto a film the household does not have.
    name = str(item.get("Name") or "").strip()
    year = _as_int(item.get("ProductionYear"))
    if not name or year is None:
        return None
    candidates = await conn.fetch(
        """
        SELECT t.id FROM title t
         WHERE t.kind = $1
           AND t.year = $3
           AND (lower(t.name) = lower($2)
                OR lower(coalesce(t.original_name, '')) = lower($2)
                OR EXISTS (SELECT 1 FROM title_alias a
                            WHERE a.title_id = t.id AND lower(a.alias) = lower($2)))
         ORDER BY t.id
         LIMIT 2
        """,
        kind, name, year,
    )
    return candidates[0]["id"] if len(candidates) == 1 else None


async def upsert_item(conn: asyncpg.Connection, item: dict, report: ResolveReport) -> int | None:
    """Attach one Jellyfin item to its title, filling only what is NULL; None when unresolved.

    An unresolved item is reported, never minted (§4.2). `title.jellyfin_id` is elected by
    `_elect_representatives`, not here.
    """
    title_id = await resolve_title_id(conn, item)
    if title_id is None:
        # `name` keeps the spelling it had exactly, because it is still what `as_dict` sends.
        report.unmatched.append(UnmatchedItem(
            jellyfin_id=str(item.get("Id") or "") or None,
            name=str(item.get("Name") or item.get("Id") or "?"),
            provider_ids=provider_ids(item),
        ))
        return None

    ids = identity(item)
    jellyfin_id = str(item.get("Id") or "") or None

    row = await conn.fetchrow(
        "SELECT kind, imdb_id, tmdb_id, tvdb_id FROM title WHERE id = $1", title_id
    )
    fills = {c: ids[c] for c in FILLABLE if ids[c] is not None and row[c] is None}

    sets = [f"{column} = ${i + 2}" for i, column in enumerate(fills)]
    values = list(fills.values())
    # §7.2: seeing the item IS the ownership derivation.
    sets += ["is_owned = true", "owned_checked_at = now()", "updated_at = now()"]
    # Guarded so an unchanged library writes nothing, with `owned_checked_at` refreshed hourly.
    guard = (
        f"({' OR '.join(f'{c} IS NULL' for c in fills)} OR " if fills else "("
    ) + (
        "NOT is_owned OR owned_checked_at IS NULL "
        "OR owned_checked_at < now() - interval '1 hour')"
    )
    await conn.execute(
        f"UPDATE title SET {', '.join(sets)} WHERE id = $1 AND {guard}", title_id, *values
    )

    if jellyfin_id:
        # Every copy of this title, so "not seen" clears them all (§7.3); same hourly guard.
        await conn.execute(
            """
            INSERT INTO title_jellyfin_item (jellyfin_id, title_id) VALUES ($1, $2)
            ON CONFLICT (jellyfin_id) DO UPDATE
               SET title_id = excluded.title_id, seen_at = now()
             WHERE title_jellyfin_item.title_id IS DISTINCT FROM excluded.title_id
                OR title_jellyfin_item.seen_at < now() - interval '1 hour'
            """,
            jellyfin_id, title_id,
        )
        report.items[jellyfin_id] = title_id

    report.matched += 1
    report.matched_title_ids.add(title_id)
    # The title's own kind, for decision 210's series rule; an imdb match is not kind-qualified.
    report.kinds[title_id] = row["kind"]
    for column in fills:
        report.filled[column] = report.filled.get(column, 0) + 1
    return title_id


async def _elect_representatives(conn: asyncpg.Connection, report: ResolveReport) -> None:
    """Decide `title.jellyfin_id` once per sweep, deterministically, from the whole page-set.

    Keep the current id while it is still a copy of this title, else take the lowest live one;
    never read a Played flag. `relinked` counts only an old id gone from the library.
    """
    copies: dict[int, list[str]] = {}
    for jellyfin_id, title_id in report.items.items():
        copies.setdefault(title_id, []).append(jellyfin_id)

    rows = await conn.fetch(
        "SELECT id, jellyfin_id FROM title WHERE id = ANY($1::int[])", list(copies)
    )
    for row in rows:
        live = sorted(copies[row["id"]])
        current = row["jellyfin_id"]
        # Membership is tested against this title's own copies, not the page-set at large.
        if current in live:
            continue
        if current is not None:
            report.relinked += 1
        # A real change: the deep link moved.
        await conn.execute(
            "UPDATE title SET jellyfin_id = $2, updated_at = now() WHERE id = $1",
            row["id"], live[0],
        )


async def prune_missing_items(conn: asyncpg.Connection, report: ResolveReport) -> int:
    """Drop `title_jellyfin_item` rows for copies this sweep's library read did not contain.

    Only the caller knows the read completed; an empty report is refused as a read that did not
    happen.
    """
    if not report.items:
        log.warning("not pruning the Jellyfin item map: this sweep resolved no items at all")
        return 0
    return await conn.fetchval(
        """
        WITH gone AS (
            DELETE FROM title_jellyfin_item
             WHERE NOT (jellyfin_id = ANY($1::text[]))
            RETURNING 1
        )
        SELECT count(*) FROM gone
        """,
        list(report.items),
    )


async def upsert_items(conn: asyncpg.Connection, items: list[dict]) -> ResolveReport:
    """The whole library, resolved in one pass; user-independent, so the keyless read suffices (§7.2)."""
    report = ResolveReport()
    for item in items:
        await upsert_item(conn, item, report)
    await _elect_representatives(conn, report)
    if report.unmatched:
        log.info(
            "%d Jellyfin item(s) did not resolve to a title: %s",
            len(report.unmatched), ", ".join(entry.name for entry in report.unmatched[:5]),
        )
    return report


__all__ = [
    "ResolveReport",
    "UnmatchedItem",
    "identity",
    "kind_of",
    "provider_ids",
    "prune_missing_items",
    "resolve_title_id",
    "upsert_item",
    "upsert_items",
]
