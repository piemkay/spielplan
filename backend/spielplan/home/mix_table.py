"""The recipe engine's table and pages (decisions 559 and 560): one term table per kind, cached on the
app's state and rebuilt when the vocabulary, the bundle or the DNA changes; an owned flip re-derives
the library's share without refetching the terms."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import numpy as np

from spielplan.db import dna_terms, library
from spielplan.derive import ids as derive_ids
from spielplan.home import mix
from spielplan.models import artifacts
from spielplan.placement import features
from spielplan.scoring import serve
from spielplan.tonight import pool as tonight_pool

# One round trip per request: DNA writers insert, delete or replace rows (new ids), a curator's
# repoint renames a tag in place, and Jellyfin flips `is_owned`, which only re-derives the library's share.
_SIGNATURE = """
    SELECT (SELECT count(*) FROM dna_tag WHERE version = $1) AS tags,
           (SELECT max(id) FROM dna_tag WHERE version = $1) AS tag_max,
           (SELECT sum(hashtext(term)) FROM dna_tag WHERE version = $1) AS tag_terms,
           (SELECT count(*) FROM dna_projected WHERE version = $1) AS projected,
           (SELECT max(id) FROM dna_projected WHERE version = $1) AS projected_max,
           (SELECT md5(string_agg(id::text, ',' ORDER BY id)) FROM title
             WHERE kind = $2 AND is_owned) AS owned
"""

# Folded per (title, term) in `_rows`: a GROUP BY over every tagged row spills at the default
# work_mem and takes seconds on a production-sized catalogue.
_ROWS = f"""
    SELECT d.title_id, d.term, d.tier = 'extracted' AS quoted, ({dna_terms.TERM_WEIGHT})::float8 AS r
      FROM dna_tagged d JOIN title t ON t.id = d.title_id
     WHERE d.version = $1 AND t.kind = $2
"""

_CARD = """
    SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.is_owned, t.placement,
           tp.item_n, tp.e_source, COALESCE(ut.state, 'unseen') AS seen_state
      FROM title t
      LEFT JOIN title_prior tp ON tp.title_id = t.id
      LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $2
     WHERE t.id = ANY($1::int[])
"""

_OTHER = {"movie": "series", "series": "movie"}


@dataclass
class _Cache:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    held: dict[str, tuple[tuple[Any, ...], str | None, mix.Table]] = field(default_factory=dict)


def _cache(app_state: Any) -> _Cache:
    cache = getattr(app_state, "mix", None)
    if cache is None:
        cache = _Cache()
        app_state.mix = cache
    return cache


async def table_for(app_state: Any, conn: asyncpg.Connection, kind: str) -> mix.Table | None:
    """The kind's table, or None without a vocabulary."""
    version = await dna_terms.active_version(conn)
    if version is None:
        return None
    sig = await conn.fetchrow(_SIGNATURE, version, kind)
    key = (version, getattr(app_state, "basis_key", None), sig["tags"], sig["tag_max"], sig["tag_terms"],
           sig["projected"], sig["projected_max"])
    cache = _cache(app_state)
    async with cache.lock:
        held = cache.held.get(kind)
        if held is not None and held[0] == key:
            if held[1] == sig["owned"]:
                return held[2]
            owned = await _owned(conn, kind)
            table = await asyncio.to_thread(held[2].with_owned, owned, len(owned))
        else:
            table = await _build(app_state, conn, kind, version)
        cache.held[kind] = (key, sig["owned"], table)
        return table


async def _owned(conn: asyncpg.Connection, kind: str) -> list[int]:
    return [r["id"] for r in await conn.fetch(
        "SELECT id FROM title WHERE kind = $1 AND is_owned", kind
    )]


async def _build(app_state: Any, conn: asyncpg.Connection, kind: str, version: str) -> mix.Table:
    vocab = await conn.fetch(
        "SELECT term, facet, label FROM dna_term WHERE version = $1 ORDER BY term", version
    )
    facets = await conn.fetch(
        "SELECT facet, colour FROM dna_facet WHERE version = $1 ORDER BY ord, facet", version
    )
    at = {r["term"]: i for i, r in enumerate(vocab)}
    rows = await asyncio.to_thread(_rows, await conn.fetch(_ROWS, version, kind), at)
    ids = [r[0] for r in rows]
    votes = await conn.fetch(
        f"WITH s AS ({library._PLATFORM_SCORE}) SELECT title_id, votes FROM s"
        " WHERE title_id = ANY($1::int[]) AND votes > 0",
        ids,
    )
    credits = await conn.fetch(
        """
        SELECT c.title_id, p.id, p.name
          FROM credit c JOIN person p ON p.id = c.person_id JOIN title t ON t.id = c.title_id
         WHERE t.kind = $1 AND c.role_class = 'director'
         ORDER BY p.id, c.title_id
        """,
        kind,
    )
    directors, people = _directors(credits)
    owned = await _owned(conn, kind)
    store = getattr(app_state, "artifacts", None)

    def build() -> mix.Table:
        return mix.Table.build(
            terms=[r["term"] for r in vocab],
            labels=[dna_terms.label_of(r["term"], r["label"]) for r in vocab],
            term_facets=[r["facet"] for r in vocab],
            facets=[r["facet"] for r in facets],
            colours={r["facet"]: r["colour"] for r in facets},
            rows=rows,
            votes={r["title_id"]: int(r["votes"]) for r in votes},
            directors=directors,
            people=people,
            review=features.text_embeddings(store, ids),
            owned=owned,
            n_owned=len(owned),
        )

    return await asyncio.to_thread(build)


def _rows(
    tagged: Sequence[asyncpg.Record], at: dict[str, int]
) -> list[tuple[int, list[int], list[bool], list[float]]]:
    """One entry per title and term of the vocabulary, ascending: quoted where either tier is, at its
    higher naming rank."""
    tagged = [r for r in tagged if r[1] in at]
    n = len(tagged)
    if not n:
        return []
    title = np.fromiter((r[0] for r in tagged), dtype=np.int64, count=n)
    col = np.fromiter((at[r[1]] for r in tagged), dtype=np.int64, count=n)
    order = np.lexsort((col, title))
    title, col = title[order], col[order]
    quoted = np.fromiter((r[2] for r in tagged), dtype=bool, count=n)[order]
    rank = np.fromiter((r[3] for r in tagged), dtype=np.float64, count=n)[order]
    new = np.ones(n, dtype=bool)
    new[1:] = (title[1:] != title[:-1]) | (col[1:] != col[:-1])
    starts = np.flatnonzero(new)
    title, col = title[starts], col[starts]
    quoted = np.logical_or.reduceat(quoted, starts)
    rank = np.maximum.reduceat(rank, starts)
    cut = np.flatnonzero(title[1:] != title[:-1]) + 1
    return [
        (int(t), c.tolist(), q.tolist(), w.tolist())
        for t, c, q, w in zip(title[np.r_[0, cut]], np.split(col, cut), np.split(quoted, cut),
                              np.split(rank, cut), strict=True)
    ]


def _directors(
    credits: Sequence[asyncpg.Record],
) -> tuple[dict[int, list[int]], list[tuple[tuple[int, ...], str]]]:
    """One human per loosely equal name, as the card folds credits; each title's directors by index."""
    index: dict[str, int] = {}
    ids: list[list[int]] = []
    names: list[str] = []
    per_title: dict[int, list[int]] = {}
    for r in credits:
        key = derive_ids.loose_name(r["name"]) or f"#{r['id']}"
        if key not in index:
            index[key] = len(names)
            ids.append([])
            names.append(r["name"])
        human = index[key]
        if r["id"] not in ids[human]:
            ids[human].append(r["id"])
        mine = per_title.setdefault(r["title_id"], [])
        if human not in mine:
            mine.append(human)
    return per_title, [(tuple(sorted(p)), n) for p, n in zip(ids, names, strict=True)]


async def _operands(
    app_state: Any, conn: asyncpg.Connection, table: mix.Table, kind: str, recipe: Sequence[mix.Ingredient]
) -> dict[int, mix.Operand]:
    """A recipe film of the other kind maps onto the same term axis (§6.0, decision 559)."""
    operands = {i.title_id: table.operand(i.title_id) for i in recipe if i.title_id in table.row_of}
    missing = [i.title_id for i in recipe if i.title_id not in operands]
    if missing:
        other = await table_for(app_state, conn, _OTHER[kind])
        if other is not None and other.terms == table.terms:
            operands.update({t: other.operand(t) for t in missing if t in other.row_of})
    return operands


# Without a vocabulary every recipe film is refused as carrying no terms, and nothing ranks.
_EMPTY = mix.Table.build(terms=(), labels=(), term_facets=(), facets=(), colours={}, rows=(), votes={},
                         directors={}, people=(), review={}, owned=(), n_owned=0)


async def _recipe(
    app_state: Any, conn: asyncpg.Connection, kind: str, recipe: Sequence[mix.Ingredient]
) -> tuple[mix.Table, dict[int, mix.Operand]]:
    table = await table_for(app_state, conn, kind) or _EMPTY
    operands = {} if table is _EMPTY else await _operands(app_state, conn, table, kind, recipe)
    mix.check(table, recipe, operands)
    return table, operands


def _term(table: mix.Table, col: int, quoted: bool | None = None) -> dict[str, Any]:
    out = {"term": table.terms[col], "label": table.labels[col],
           "facet": table.facets[table.term_facet[col]]}
    if quoted is not None:
        out["quoted"] = quoted
    return out


async def _cards(conn: asyncpg.Connection, ids: Sequence[int], user_id: int) -> dict[int, dict[str, Any]]:
    cards = [dict(r) for r in await conn.fetch(_CARD, list(ids), user_id)]
    await library.carry_original_names(conn, cards)
    return {c["id"]: c for c in cards}


def _ingredient(
    table: mix.Table, ing: mix.Ingredient, op: mix.Operand, card: dict[str, Any]
) -> dict[str, Any]:
    return {
        "title_id": ing.title_id,
        **{k: card.get(k) for k in ("kind", "name", "year", "poster_path")},
        "like": ing.like,
        "groups": list(ing.groups),
        "terms": [_term(table, c, q) for c, q in mix.film_terms(table, ing, op)],
        "sheet": [
            {
                "group": group,
                "name": mix.GROUP_NAMES[group],
                "colour": table.colours.get(mix.GROUPS[group][0]),
                "offered": offered,
                "quoted": [_term(table, c) for c in quoted],
                "inferred": [_term(table, c) for c in inferred],
            }
            for group, offered, quoted, inferred in mix.group_terms(table, op)
        ],
    }


async def recipe_page(
    app_state: Any,
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    recipe: Sequence[mix.Ingredient],
    eligible: library.Eligible | None,
    pool: str = "library",
    sort: str = "match",
    limit: int = 60,
    offset: int = 0,
) -> dict[str, Any]:
    """`GET /api/mix/titles`: the library or the well-known titles beyond it, in the recipe's order."""
    table, operands = await _recipe(app_state, conn, kind, recipe)
    bundle = await artifacts.active_bundle_version(conn)
    personal = await serve.personal_kinds(conn, user_id=user_id, kinds=[kind], bundle_version=bundle)
    asks = not any(i.like for i in recipe)
    order = np.zeros(0, dtype=np.int64) if asks else mix.rank(table, recipe, operands)
    if eligible is not None:
        order = order[np.isin(table.ids[order], np.fromiter(eligible.ids, dtype=np.int64))]
    owned = table.owned[order]
    chosen = order[owned] if pool == "library" else order[~owned]

    effective = "for_you" if sort == "for_you" and personal else "match"
    if effective == "for_you" and len(chosen):
        scores = {r["title_id"]: r["score"] for r in await conn.fetch(
            "SELECT title_id, score FROM user_score WHERE user_id = $1 AND bundle_version = $2"
            " AND title_id = ANY($3::int[])",
            user_id, bundle, [int(t) for t in table.ids[chosen]],
        )}
        # Unscored last, each run in match order (decision 559 item 4).
        chosen = np.array(sorted(
            chosen, key=lambda r: (int(table.ids[r]) not in scores, -scores.get(int(table.ids[r]), 0.0))
        ), dtype=np.int64)
    strong = eligible.strong if eligible is not None and pool == "library" else None
    if strong is not None:
        is_strong = np.isin(table.ids[chosen], np.fromiter(strong, dtype=np.int64))
        chosen = np.concatenate([chosen[is_strong], chosen[~is_strong]])

    page = [int(r) for r in chosen[offset:offset + limit]]
    # A thin list ("Only N films in your library fit") shows every fit: a fold would hide one of a few.
    cells = mix.director_cap(table, page) if offset == 0 and len(chosen) >= mix.THIN_UNDER else page
    cards = await _cards(conn, [int(table.ids[r]) for r in page] + [i.title_id for i in recipe], user_id)

    def card(row: int) -> dict[str, Any]:
        title_id = int(table.ids[row])
        item = dict(cards[title_id])
        if strong is not None:
            item["match"] = "strong" if title_id in strong else "weak"
        item["why"] = [
            {"title_id": ing.title_id, "name": cards[ing.title_id]["name"], "groups": list(ing.groups),
             "like": ing.like, "terms": [_term(table, c, q) for c, q in named]}
            for ing, named in mix.why(table, recipe, operands, row)
        ]
        return item

    def cell(c: int | mix.Fold) -> dict[str, Any]:
        if isinstance(c, int):
            return card(c)
        people = [table.people[d] for d in c.directors]
        return {"fold": {"person_ids": sorted({p for ids, _ in people for p in ids}),
                         "name": " & ".join(name for _, name in people),
                         "items": [card(r) for r in c.rows]}}

    more, less = mix.derived(table, recipe, operands)
    library_total = int(owned.sum())
    return {
        "kind": kind,
        "pool": pool,
        "sort": effective,
        "for_you_available": bool(personal),
        "total": len(chosen),
        "library_total": library_total,
        "beyond_total": len(order) - library_total,
        "strong_total": None if strong is None else int(is_strong.sum()),
        "limit": limit,
        "offset": offset,
        "recipe": {
            "asks_for_like": asks,
            "ingredients": [_ingredient(table, i, operands[i.title_id], cards[i.title_id]) for i in recipe],
            "more": [_term(table, c, q) for c, q in more],
            "less": [_term(table, c, q) for c, q in less],
        },
        "items": [cell(c) for c in cells],
    }


async def twist_page(
    app_state: Any,
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    recipe: Sequence[mix.Ingredient],
    eligible: library.Eligible | None,
    seed: int = 0,
) -> dict[str, Any]:
    """`GET /api/mix/twists`: a group of one of the member's films placed A or above (decision 539),
    kept where the twisted recipe leaves enough of the library under the request's filters."""
    table, operands = await _recipe(app_state, conn, kind, recipe)
    films, _word, _n = await tonight_pool.liked_films(conn, user_id=user_id, kind=kind)
    by_id = {f["title_id"]: f for f in films if f["title_id"] in table.row_of}
    rows = table.owned.copy()
    if eligible is not None:
        rows &= np.isin(table.ids, np.fromiter(eligible.ids, dtype=np.int64))
    picks = mix.twists(table, recipe, operands, [table.operand(t) for t in by_id], seed=seed, rows=rows)
    out = []
    for title_id, group, n in picks:
        film = by_id[title_id]
        terms = mix.film_terms(table, mix.Ingredient(title_id, (group,)), table.operand(title_id),
                               mix.TWIST_TERMS)
        out.append({
            "title_id": title_id, "name": film["name"], "year": film["year"],
            "poster_path": film["poster_path"], "group": group, "group_name": mix.GROUP_NAMES[group],
            "colour": table.colours.get(mix.GROUPS[group][0]),
            "terms": [_term(table, c) for c, _q in terms], "library_n": n,
        })
    return {"seed": seed, "twists": out}


async def picker(conn: asyncpg.Connection, q: str, limit: int) -> list[dict[str, Any]]:
    """`GET /api/mix/films`: best match first over both kinds, owned or not, among titles carrying
    MIN_TERMS terms, never a wished row (decision 559 item 1)."""
    version = await dna_terms.active_version(conn)
    if version is None or not q.strip():
        return []
    clause, args = library._filters(kinds=list(library.KINDS), q=q)
    args += [q, version, mix.MIN_TERMS, limit]
    n = len(args)
    joins, order, _match = library.search_order_sql(f"${n - 3}")
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.kind, t.name, t.year, t.poster_path, t.is_owned
          FROM title t
          {joins}
         WHERE {clause} AND t.origin <> 'wished'
           AND (SELECT count(DISTINCT d.term) FROM dna_tagged d
                 WHERE d.title_id = t.id AND d.version = ${n - 2}) >= ${n - 1}
         ORDER BY {order}
         LIMIT ${n}
        """,
        *args,
    )
    items = [dict(r) for r in rows]
    await library.carry_original_names(conn, items)
    return items
