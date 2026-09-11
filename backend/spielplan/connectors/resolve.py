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

A third measurement decides the shape of the *output*: several Jellyfin items are routinely one
title. A household whose libraries ship "Movies" and "Movies 4K" has two items per film, and
§7.1's single `jellyfin_id` column cannot say so. So this module resolves the whole library in
one pass and produces three sweep-level facts (`ResolveReport.items`, `.matched_title_ids`,
`.kinds`) plus the `title_jellyfin_item` map, and it elects the one representative item id per
title itself, deterministically. Per-item election — which is what the sweep used to do, per
user — made the pointer flip every fifteen minutes and lost §7.3's finish prompt for whichever
member was not on the winning copy.
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
    # Resolution is user-independent, so §7.3's sweep reads the library once with the admin key
    # and carries these three facts into the per-user loop instead of re-resolving per user.
    # Each exists because a copy-related rule needs it and the alternative is a query per row:
    #
    #   `items`  -- every resolved copy, which is what "unseen clears every copy" reads, and
    #               what a `/Sessions` item id has to be resolved through before the right
    #               title's finish prompt can be armed (§7.3).
    #   `matched_title_ids` -- the complete set of titles the library holds. §7.2's "mark
    #               removed titles is_owned = false" is allowed to trust exactly this and only
    #               when the read completed; a partial set un-owns the household's library.
    #   `kinds`  -- the title's own `kind`, because decision 210 forbids a recursive
    #               MarkUnplayed against a Series folder and the push must know which it has
    #               without a second SELECT per row.
    items: dict[str, int] = field(default_factory=dict)
    matched_title_ids: set[int] = field(default_factory=set)
    kinds: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            # §6.6's sync card distinguishes copies from titles because this household has
            # both: `matched` counts library items, this counts the films behind them. The set
            # itself is not rendered -- a count is what an operator can read.
            "matched_titles": len(self.matched_title_ids),
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
    name/year fallback is last because it is the only one that can be wrong -- and because it
    can, it refuses rather than guesses when the name is ambiguous or the year is missing.
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

    # Last resort, and the one this function's own docstring distrusts. It now refuses two
    # things it used to guess at, because an arbitrary match is strictly worse than no match:
    # a refusal is *reported* (`report.unmatched`, which §7.2's acquisition half consumes at
    # M5), while a wrong match silently writes `is_owned`, `owned_checked_at` and the deep link
    # onto a film the household does not have, and nothing ever revisits it.
    #
    #   * No year, no match. `($3::int IS NULL OR t.year = $3)` meant an item whose scraper
    #     found no ProductionYear matched on name alone -- the widest possible reading of the
    #     weakest key. §7.1 names three provider keys and not this one; a name with no year is
    #     not an identity.
    #   * Two rows, no match. Measured on the corpus this resolves against: 2,438 titles share
    #     `(kind, lower(name))` and 573 groups still collide with the year applied, so
    #     `LIMIT 1` was never a tie-break -- it took whichever row the bundle imported first.
    #
    # Deliberately not a fuzzy scorer: a similarity threshold trades this silent wrong answer
    # for a tunable one, and §7.1 does not ask for the key at all.
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
    """Attach one Jellyfin item to its title, filling only what is NULL.

    Returns the title id, or None when the item does not resolve. An unresolved item is
    *reported*, never invented: `title.id` is carried over verbatim from the corpus (§4.2), so
    minting one here would create a row no bundle can ever reconcile with. Acquiring genuinely
    new titles is §8's pipeline, which arrives at M5.

    What this does NOT decide is `title.jellyfin_id`. §7.1 keeps one item id per title as the
    deep-link and single-write representative, and one item cannot choose it: a household whose
    libraries ship "Movies" and "Movies 4K" gives one film several items, and electing per item
    means the last one read wins. That election is `_elect_representatives`, over the whole
    page-set, once per sweep.
    """
    title_id = await resolve_title_id(conn, item)
    if title_id is None:
        report.unmatched.append(str(item.get("Name") or item.get("Id") or "?"))
        return None

    ids = identity(item)
    jellyfin_id = str(item.get("Id") or "") or None

    row = await conn.fetchrow(
        "SELECT kind, imdb_id, tmdb_id, tvdb_id FROM title WHERE id = $1", title_id
    )
    fills = {c: ids[c] for c in FILLABLE if ids[c] is not None and row[c] is None}

    sets = [f"{column} = ${i + 2}" for i, column in enumerate(fills)]
    values = list(fills.values())
    # §7.2: "is_owned … re-derived from Jellyfin, never trusted stale". Seeing the item in the
    # library IS the derivation, and owned_checked_at is what makes a later sweep able to tell
    # "still owned" from "not looked at since".
    sets += ["is_owned = true", "owned_checked_at = now()", "updated_at = now()"]
    # Guarded, because this runs once per item every fifteen minutes. An unconditional write
    # over an unchanged library is ~11,000 dead row versions per cycle — and every one of them
    # is work for autovacuum and noise in `updated_at`. The re-derivation still happens; only
    # the write is skipped when it would change nothing that matters, and `owned_checked_at` is
    # refreshed hourly so "still owned" stays distinguishable from "not looked at since".
    #
    # The guard used to test `jellyfin_id IS DISTINCT FROM`, which was true for every copy but
    # the current pointer — so the duplicated titles, the ones the churn arithmetic is worst
    # for, were the titles it did not protect. With the pointer elected once per sweep the test
    # belongs there and not here.
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
        # The map §7.1's single column cannot hold: which items are this one film. "Not seen"
        # has to clear Played on all of them or the next sweep's OR-collapse adopts the
        # untouched copy straight back over the person's explicit action (§7.3).
        #
        # Written with the same hourly guard as `owned_checked_at` above, and for the same
        # reason: an unchanged library would otherwise rewrite every row of this table every
        # fifteen minutes for a `seen_at` nothing reads faster than hourly.
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
    # The title's own `kind`, not the item's `Type`: an `imdb_id` match is not qualified by kind
    # (it is unique across kinds when present), and decision 210's "a Series folder never gets a
    # DELETE" has to be decided from what the corpus says this title is.
    report.kinds[title_id] = row["kind"]
    for column in fills:
        report.filled[column] = report.filled.get(column, 0) + 1
    return title_id


async def _elect_representatives(conn: asyncpg.Connection, report: ResolveReport) -> None:
    """Decide `title.jellyfin_id` once per sweep, deterministically, from the whole page-set.

    §7.1 keeps one item id per title and `api/library.py`, `api/tonight.py` and the compensating
    push in `sync/seen.py` all read it, so it has to be stable. It was not: the sweep wrote it
    to the copy *this user* had played, once per linked user, in user-id order — measured
    sequence across three sweeps with two members on different copies is
    `jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b`. `playback.observe` resolves a session against that
    one column, so the member who lost the coin toss never saw §7.3's finish prompt again, and
    permanently, because the order is fixed.

    The rule therefore never reads a Played flag: **keep the current id while it is still a copy
    of this title, else take the lowest live one.** Lowest is arbitrary but total and stable,
    which is the whole requirement — a pointer that does not move cannot lose a prompt.

    `relinked` is counted only when the old id is gone from the library, which is the case §7.1
    means by a re-link (a rebuilt library mints new item ids). Counting every second live copy,
    as the per-item path did, made the figure on §6.6's sync card read "your library was
    rebuilt" every fifteen minutes for a household that merely owns two rips of a film.
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
        # `current` can be in the library and still not be a copy of *this* title — a stale id
        # the corpus carried, or one a rebuild handed to another film. Membership is tested
        # against this title's own copies, not the page-set at large.
        if current in live:
            continue
        if current is not None:
            report.relinked += 1
        # `updated_at` is bumped on purpose: the deep link the surfaces render has changed, so
        # this is a real change to the row, unlike the no-op re-derivation guarded above.
        await conn.execute(
            "UPDATE title SET jellyfin_id = $2, updated_at = now() WHERE id = $1",
            row["id"], live[0],
        )


async def prune_missing_items(conn: asyncpg.Connection, report: ResolveReport) -> int:
    """Drop `title_jellyfin_item` rows for copies this sweep's library read did not contain.

    Separate from `upsert_items` and never called by it, because the caller is the only one who
    knows whether the read completed. §7.2 gates ownership falsification on exactly that fact
    for exactly this reason: a Jellyfin outage, or a page-set truncated at the client's page cap,
    is indistinguishable here from a library that genuinely shrank. Deleting the map on the
    strength of one is how "unseen clears every copy" would start missing copies.

    The empty-report refusal is belt and braces over that gate, not a substitute for it: an
    empty page-set is never a real library, so it can only be a read that did not happen.
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
    """The whole library, resolved in one pass. §7.3's sweep calls this once, not once per user.

    Resolution is user-independent — identity comes from ProviderIds and the corpus, never from
    `UserData` — so it is safe to call with items carrying no `UserData` at all, which is what
    the admin-key read with no `userId` returns and what §7.2 wants for ownership: the admin's
    view is the household's library, not one member's visibility of it.
    """
    report = ResolveReport()
    for item in items:
        await upsert_item(conn, item, report)
    await _elect_representatives(conn, report)
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
    "prune_missing_items",
    "resolve_title_id",
    "upsert_item",
    "upsert_items",
]
