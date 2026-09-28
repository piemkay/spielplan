"""Catalog queries (§6.0, §4.1). Rule 5: every listing takes a required, non-empty `kinds`, and a
surface that ranks renders kinds apart. Rule 1: the DNA tiers stay two lists. Rule 2: weights
never appear in a WHERE."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from spielplan.art.hosts import servable
from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.derive import ids as derive_ids
from spielplan.importer import meta

Kind = Literal["movie", "series"]
KINDS: tuple[Kind, ...] = ("movie", "series")
SeenFilter = Literal["any", "seen", "unseen"]
Sort = Literal["for_you", "newest"]

# LIKE's default escape is backslash. Order matters: double it first, or later escapes get escaped.
_LIKE_SPECIALS = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))


def _like_needle(q: str) -> str:
    """`q` as a LIKE pattern matching it literally: `%` and `_` are the characters typed."""
    needle = q.lower()
    for character, escaped in _LIKE_SPECIALS:
        needle = needle.replace(character, escaped)
    return f"%{needle}%"


# Search order (decision 472): both sides are normalised in SQL to space-padded alnum words, so
# punctuation never decides and nothing left in the query is a LIKE metacharacter.


def _norm_sql(expr: str) -> str:
    return f"(' ' || btrim(regexp_replace(lower({expr}), '[^[:alnum:]]+', ' ', 'g')) || ' ')"


# A leading article does not count: "godfather" must match "The Godfather" exactly.
_ARTICLE = "'^ (the|a|an) '"

# Tiers: 0 whole text; 1 starts it; 2 a starting word begins with it; 3 whole words anywhere;
# 4 a word begins with it; 5 inside a word; 6 absent (the predicate matched the other text).
SEARCH_TIERS = 7
# Decision 516: from here on a hit only contains the query inside a word.
WEAK_TIER = 5


def _tier_sql(n: str, nq: str) -> str:
    return (
        f"CASE WHEN {n} = {nq} THEN 0"
        f" WHEN {n} LIKE {nq} || '%' THEN 1"
        f" WHEN {n} LIKE rtrim({nq}) || '%' THEN 2"
        f" WHEN {n} LIKE '%' || {nq} || '%' THEN 3"
        f" WHEN {n} LIKE '%' || rtrim({nq}) || '%' THEN 4"
        f" WHEN {n} LIKE '%' || btrim({nq}) || '%' THEN 5"
        f" ELSE {SEARCH_TIERS - 1} END"
    )


def _text_tier_sql(n: str, nq: str) -> str:
    stripped = f"regexp_replace({n}, {_ARTICLE}, ' ')"
    return f"LEAST({_tier_sql(n, nq)}, {_tier_sql(stripped, nq)})"


def search_order_sql(q_param: str) -> tuple[str, str, str]:
    """(joins, ORDER BY, match select), best match first (decision 472): name tier x2 or alias tier x2+1,
    then owned, crowd percentile within the title's own kind, year, name, id (a total order for OFFSET).
    `match` is 'weak' from WEAK_TIER on, for the client to fold (decision 516)."""
    joins = f"""
          CROSS JOIN (SELECT {_norm_sql(f'{q_param}::text')} AS n) sq
          CROSS JOIN LATERAL (SELECT {_norm_sql('t.name')} AS n) sn
          LEFT JOIN LATERAL (
              SELECT min({_text_tier_sql('sa.n', 'sq.n')}) AS tier
                FROM (SELECT {_norm_sql('a.alias')} AS n
                        FROM title_alias a WHERE a.title_id = t.id) sa
          ) salias ON true
          LEFT JOIN (
              SELECT pp.title_id,
                     percent_rank() OVER (PARTITION BY pt.kind ORDER BY pp.item_n) AS pct
                FROM title_prior pp JOIN title pt ON pt.id = pp.title_id
               WHERE pt.kind = ANY($1)
          ) spop ON spop.title_id = t.id"""
    quality = (
        f"LEAST({_text_tier_sql('sn.n', 'sq.n')} * 2,"
        f" COALESCE(salias.tier, {SEARCH_TIERS - 1}) * 2 + 1)"
    )
    order = (
        f"{quality},"
        " t.is_owned DESC, COALESCE(spop.pct, 0) DESC, t.year DESC NULLS LAST, lower(t.name), t.id"
    )
    match = f"CASE WHEN {quality} < {WEAK_TIER} * 2 THEN 'strong' ELSE 'weak' END AS match"
    return joins, order, match


def normalise_kinds(kinds: Sequence[str] | None) -> list[Kind]:
    """An empty selection is an error, not "everything" (§4.1 rule 5)."""
    chosen = [k for k in KINDS if kinds and k in kinds]
    if not chosen:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    return chosen


async def household_ids(conn: asyncpg.Connection) -> list[int]:
    """Active admins and members: the one household predicate both nightly passes share (§5.3).
    The role clause stays although a CHECK makes it true today."""
    rows = await conn.fetch(
        "SELECT id FROM app_user WHERE is_active AND role IN ('admin', 'member') ORDER BY id"
    )
    return [int(r["id"]) for r in rows]


def _dna_term_matches(needle: str) -> str:
    """`dt` matches the typed `needle` (stripped, lower-cased) by id, leaf, shipped label or fallback
    label. Shared by `_filters` and `dna_tiers_for`, which must agree clause for clause."""
    leaf = "split_part(lower(dt.term), '.', 2)"
    return (
        f"(lower(dt.term) = {needle} OR {leaf} = {needle}"
        f" OR replace({leaf}, '_', ' ') = {needle}"
        f" OR dt.term IN (SELECT dm.term FROM dna_term dm WHERE dm.version = {dna_terms.ACTIVE_VERSION}"
        f" AND lower(btrim(dm.label)) = {needle}))"
    )


def _filters(
    *,
    kinds: Sequence[str],
    user_id: int | None = None,
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: SeenFilter = "any",
    person_id: int | Sequence[int] | None = None,
    owned_only: bool = False,
    runtime_max: int | None = None,
    runtime_min: int | None = None,
    dna: str | None = None,
) -> tuple[str, list[Any]]:
    """The catalog's WHERE over alias `t`; the listing and the hidden-by-kind count share it."""
    where = ["t.kind = ANY($1)"]
    args: list[Any] = [normalise_kinds(kinds)]

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if q:
        needle = _like_needle(q)
        where.append(
            f"(lower(t.name) LIKE {arg(needle)} OR EXISTS ("
            f"  SELECT 1 FROM title_alias a WHERE a.title_id = t.id AND lower(a.alias) LIKE {arg(needle)}"
            f"))"
        )
    if genre:
        # Decision 473: an unknown genre binds no label and so matches nothing.
        where.append(
            genre_vocab.predicate(
                arg(genre_vocab.raw_labels(genre)), arg(list(genre_vocab.EXCLUDED_SOURCES))
            )
        )
    if decade is not None:
        where.append(f"t.year >= {arg(decade)} AND t.year < {arg(decade + 10)}")
    if person_id is not None:
        # A set: `fold_credits` folds one human's person rows and names them all in `person_ids`.
        ids = [person_id] if isinstance(person_id, int) else [int(p) for p in person_id]
        where.append(
            "EXISTS (SELECT 1 FROM credit c WHERE c.title_id = t.id"
            f" AND c.person_id = ANY({arg(ids)}::int[]))"
        )
    # `runtime_min` is minutes; an unknown runtime cannot satisfy a bound, so NULL is excluded.
    if runtime_max is not None:
        where.append(f"t.runtime_min IS NOT NULL AND t.runtime_min <= {arg(runtime_max)}")
    if runtime_min is not None:
        where.append(f"t.runtime_min IS NOT NULL AND t.runtime_min >= {arg(runtime_min)}")
    if dna:
        # §4.1 rules 1 and 2: `dna_tagged` keeps the tier, and no weight filters. The term id is already
        # `facet.term`; the version is a subquery because this builder is synchronous.
        needle = arg(dna.strip().lower())
        where.append(
            "EXISTS (SELECT 1 FROM dna_tagged dt WHERE dt.title_id = t.id"
            f" AND dt.version = {dna_terms.ACTIVE_VERSION} AND {_dna_term_matches(needle)})"
        )
    if owned_only:
        where.append("t.is_owned")
    if seen != "any" and user_id is not None:
        # No user_title row means unseen.
        uid = arg(user_id)
        if seen == "seen":
            where.append(
                f"EXISTS (SELECT 1 FROM user_title ut WHERE ut.title_id = t.id "
                f"AND ut.user_id = {uid} AND ut.state = 'seen')"
            )
        else:
            where.append(
                f"NOT EXISTS (SELECT 1 FROM user_title ut WHERE ut.title_id = t.id "
                f"AND ut.user_id = {uid} AND ut.state = 'seen')"
            )
    return " AND ".join(where), args


@dataclass(frozen=True)
class RankFilters:
    """§6.3's filters minus `kind`; one record so the board, its count and the queue agree."""

    q: str | None = None
    genre: str | None = None
    decade: int | None = None
    runtime_max: int | None = None
    runtime_min: int | None = None
    seen: SeenFilter = "any"
    dna: str | None = None

    def active(self) -> dict[str, Any]:
        """What is switched on, for the "no match" state to list back."""
        return {
            name: value
            for name, value in vars(self).items()
            if value not in (None, "", "any")
        }


def rank_filters(
    *, kind: str, user_id: int, filters: RankFilters | None = None
) -> tuple[str, list[Any]]:
    """The catalog's builder, so a board and its count cannot drift apart."""
    f = filters or RankFilters()
    return _filters(
        kinds=[kind],
        user_id=user_id,
        q=f.q,
        genre=f.genre,
        decade=f.decade,
        seen=f.seen,
        runtime_max=f.runtime_max,
        runtime_min=f.runtime_min,
        dna=f.dna,
    )


async def dna_tiers_for(
    conn: asyncpg.Connection, *, title_ids: Sequence[int], dna: str
) -> dict[int, list[str]]:
    """Which tier admitted each survivor of a `dna` filter (§4.1 rule 1). Same predicate as `_filters`."""
    if not title_ids:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT dt.title_id, dt.tier
        FROM dna_tagged dt
        WHERE dt.title_id = ANY($1::int[])
          AND dt.version = {dna_terms.ACTIVE_VERSION}
          AND {_dna_term_matches("$2")}
        ORDER BY dt.title_id, dt.tier
        """,
        [int(t) for t in title_ids],
        dna.strip().lower(),
    )
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(int(row["title_id"]), []).append(str(row["tier"]))
    return out


async def list_titles(
    conn: asyncpg.Connection,
    *,
    kinds: Sequence[str],
    user_id: int | None = None,
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: SeenFilter = "any",
    person_id: int | Sequence[int] | None = None,
    owned_only: bool = False,
    limit: int = 60,
    offset: int = 0,
    sort: Sort = "newest",
    bundle_version: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """`kinds` is mandatory (rule 5). `for_you` ranks by the member's score one kind at a time
    (decision 515); unscored titles close their kind in year order."""
    clause, args = _filters(
        kinds=kinds, user_id=user_id, q=q, genre=genre, decade=decade, seen=seen,
        person_id=person_id, owned_only=owned_only,
    )

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    total = await conn.fetchval(f"SELECT count(*) FROM title t WHERE {clause}", *args)

    seen_join, seen_select = "", "NULL::text AS seen_state"
    if user_id is not None:
        seen_select = "COALESCE(ut.state, 'unseen') AS seen_state"
        seen_join = f"LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = {arg(user_id)}"

    # Unsearched listings keep the year order. `$1` holds kinds in `KINDS` order, so films lead.
    search_joins, order, match = "", "t.year DESC NULLS LAST, lower(t.name), t.id", ""
    if q and q.strip():
        search_joins, order, match = search_order_sql(arg(q))
        match = f", {match}"
    elif sort == "for_you" and user_id is not None and bundle_version is not None:
        search_joins = (
            f"\n          LEFT JOIN user_score fy ON fy.title_id = t.id AND fy.user_id = {arg(user_id)}"
            f" AND fy.bundle_version = {arg(bundle_version)}"
        )
        order = (
            "array_position($1::text[], t.kind), fy.score DESC NULLS LAST, t.year DESC NULLS LAST,"
            " lower(t.name), t.id"
        )

    lim, off = arg(limit), arg(offset)
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.is_owned,
               t.placement, tp.item_n, tp.e_source, {seen_select}{match}
          FROM title t
          -- §8 stage 10's cold badge is about CROWD DATA, and `title.placement` stopped meaning
          -- that when warm was redefined from §5.1's gate: a title with a Backbone row and low
          -- support is placed by the Cold Tower so the blend can fire, while having plenty of
          -- crowd data behind it. `title_prior` carries the quantity the badge is named for.
          LEFT JOIN title_prior tp ON tp.title_id = t.id
          {seen_join}{search_joins}
         WHERE {clause}
         -- `t.id` is not decoration: §6.0 pages this list with LIMIT/OFFSET and the client
         -- appends, so a sort that is not a TOTAL order silently duplicates and drops rows.
         -- Postgres is free to return tied rows in any order and does change its mind —
         -- top-N heapsort at low offsets, quicksort at high ones — and any rewrite between two
         -- page fetches (the nightly reconcile, a Jellyfin sync) reshuffles them outright. The
         -- corpus has 584 tie groups covering 1,175 titles plus 340 NULL-year titles that all
         -- tie on the first key, and the review reproduced 3 duplicated / 3 missing over 600
         -- tied titles and 61/61 with an UPDATE between pages. The two other OFFSET readers
         -- (`scoring/serve.py`, `ledger/refit.py`) already tie-break on the id; this is the
         -- same fix, not keyset pagination, because §6.0 asks for offsets. [M4.9 finding 11]
         ORDER BY {order}
         LIMIT {lim} OFFSET {off}
        """,
        *args,
    )
    return [dict(r) for r in rows], total


async def count_by_kind(
    conn: asyncpg.Connection,
    *,
    exclude: Sequence[str] = (),
    user_id: int | None = None,
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: SeenFilter = "any",
    person_id: int | Sequence[int] | None = None,
    owned_only: bool = False,
) -> dict[str, int]:
    """Under the listing's own filters: a count wider than the toggle can reveal is wrong (§6.0)."""
    hidden = [k for k in KINDS if k not in set(exclude)]
    if not hidden:
        return {}
    clause, args = _filters(
        kinds=hidden, user_id=user_id, q=q, genre=genre, decade=decade, seen=seen,
        person_id=person_id, owned_only=owned_only,
    )
    rows = await conn.fetch(
        f"SELECT t.kind, count(*) AS n FROM title t WHERE {clause} GROUP BY t.kind", *args
    )
    return {r["kind"]: r["n"] for r in rows}


async def get_title(
    conn: asyncpg.Connection, title_id: int, *, user_id: int | None = None
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT t.*, COALESCE(ut.state, 'unseen') AS seen_state
          FROM title t
          LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $2
         WHERE t.id = $1
        """,
        title_id,
        user_id,
    )
    return dict(row) if row else None


async def carry_original_names(
    conn: asyncpg.Connection, cards: Sequence[dict[str, Any]], *, key: str = "id"
) -> None:
    """Original title and language onto each card, in place, in one read (decision 516)."""
    ids = sorted({int(card[key]) for card in cards})
    if not ids:
        return
    rows = await conn.fetch(
        "SELECT id, original_name, original_language FROM title WHERE id = ANY($1::int[])", ids
    )
    names = {r["id"]: (r["original_name"], r["original_language"]) for r in rows}
    for card in cards:
        card["original_name"], card["original_language"] = names.get(int(card[key]), (None, None))


_CREDIT_ROWS = """
    SELECT c.person_id, p.name, p.imdb_id, p.tmdb_id, p.profile_path, c.source, c.department, c.job,
           c.character, c.billing_order, c.role_class
      FROM credit c JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1
     -- `p.name` first so the fold can take a person's first appearance as its place in the
     -- name order: the database's collation sorts the card's names, as it did when this read
     -- ended in `ORDER BY ..., p.name`, and a Python sort of the same strings would not agree.
     ORDER BY p.name, c.person_id, c.billing_order NULLS LAST, c.source, c.id
"""


def fold_credits(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per (person, §3.1 class): two sources spell one job two ways. People whose `loose_name`
    agrees and whose ids cannot disagree are one row; `person_ids` lists them all."""
    rank = {source: i for i, source in enumerate(meta.SOURCE_PRIORITY)}
    order: dict[int, int] = {}
    people: dict[int, Mapping[str, Any]] = {}
    groups: dict[tuple[int, Any], list[Mapping[str, Any]]] = {}
    for row in rows:
        order.setdefault(row["person_id"], len(order))
        people.setdefault(row["person_id"], row)
        cls = derive_ids.class_of(row["role_class"], row["department"], row["job"])
        groups.setdefault((row["person_id"], cls or ("job", row["job"])), []).append(row)

    buckets: dict[tuple[str, Any], list[tuple[int, Any]]] = {}
    for key in groups:
        loose = derive_ids.loose_name(people[key[0]]["name"])
        buckets.setdefault((loose, key[1]) if loose else ("", key), []).append(key)

    folded = []
    for keys in buckets.values():
        members = [people[pid] for pid, _ in keys]
        agree = all(
            len({m[field] for m in members if m[field]}) <= 1 for field in ("imdb_id", "tmdb_id")
        )
        for part in ([keys] if agree else [[k] for k in keys]):
            credit = _credit_row([row for key in part for row in groups[key]], part, people, rank)
            folded.append(((
                "Directing" not in credit["departments"], credit["ord"] is None,
                credit["ord"] or 0, min(order[pid] for pid, _ in part),
            ), credit))
    return [credit for _key, credit in sorted(folded, key=lambda f: f[0])]


def _credit_row(rows, keys, people, rank) -> dict[str, Any]:
    lead = min(
        (people[pid] for pid, _ in keys),
        key=lambda p: (-(bool(p["imdb_id"]) + bool(p["tmdb_id"])), p["person_id"]),
    )
    cls = keys[0][1] if isinstance(keys[0][1], str) else None
    place = lambda row: rank.get(row["source"], len(rank))  # noqa: E731
    jobs: dict[str, tuple[int, str]] = {}
    for row in rows:
        seen = jobs.get(row["job"])
        if seen is None or place(row) < seen[0]:
            jobs[row["job"]] = (place(row), row["job"])
    billed = [row["billing_order"] for row in rows if row["billing_order"] is not None]
    characters = sorted(
        (row for row in rows if row["character"] is not None),
        key=lambda row: (row["billing_order"] is None, row["billing_order"] or 0, row["source"]),
    )
    departments = sorted({row["department"] for row in rows if row["department"] is not None})
    return {
        "person_id": lead["person_id"],
        "person_ids": sorted({pid for pid, _ in keys}),
        "name": lead["name"],
        # What `/api/art/person/{person_id}` can serve (decision 528).
        "photo": servable(lead["profile_path"]),
        "role_class": cls,
        "job": min(
            rows, key=lambda row: (place(row), row["billing_order"] is None,
                                   row["billing_order"] or 0, row["job"] or "")
        )["job"],
        "jobs": [job for _, job in sorted(jobs.values(), key=lambda j: (j[0], j[1] or ""))],
        "department": departments[0] if departments else None,
        "departments": departments,
        "ord": min(billed) if billed else None,
        "character": characters[0]["character"] if characters else None,
        "sources": sorted({row["source"] for row in rows}),
    }


async def credits_for(conn: asyncpg.Connection, title_id: int) -> list[dict[str, Any]]:
    """§4.1: credits dedupe at read time, never at import. `departments` carries every spelling; the
    min `department` and `ord` stay for callers. In Python so `derive/ids` stays the one classifier."""
    return fold_credits([dict(r) for r in await conn.fetch(_CREDIT_ROWS, title_id)])


async def dna_for(
    conn: asyncpg.Connection, title_id: int, *, version: str | None
) -> dict[str, list[dict[str, Any]]]:
    """§4.1 rule 1: two tiers, two lists, never merged. `version` is required so one card cannot mix
    vocabularies; None means nothing is imported."""
    if version is None:
        return {"extracted": [], "projected": []}
    extracted = await conn.fetch(
        """
        SELECT g.term, g.facet, g.salience, g.confidence, g.n_sources, g.provider,
               COALESCE(
                 json_agg(json_build_object('quote', e.quote, 'source', e.source)
                          ORDER BY e.id) FILTER (WHERE e.id IS NOT NULL),
                 '[]'::json) AS evidence
          FROM dna_tag g
          LEFT JOIN dna_evidence e ON e.dna_tag_id = g.id
         WHERE g.title_id = $1 AND g.version = $2
         GROUP BY g.id, g.term, g.facet, g.salience, g.confidence, g.n_sources, g.provider
         ORDER BY g.salience DESC, g.facet, g.term
        """,
        title_id,
        version,
    )
    projected = await conn.fetch(
        """
        SELECT term, facet, weight, via
          FROM dna_projected
         WHERE title_id = $1 AND version = $2
         ORDER BY weight DESC NULLS LAST, facet, term
        """,
        title_id,
        version,
    )
    return {"extracted": [dict(r) for r in extracted], "projected": [dict(r) for r in projected]}


async def platform_ratings(conn: asyncpg.Connection, title_id: int) -> list[dict[str, Any]]:
    """§4.1 rule 3: display-only, the one reader of `display`. Only rows with a `scale` are scores with
    an honest caption; popularity and histogram buckets are not."""
    rows = await conn.fetch(
        "SELECT platform, metric, score, scale, votes FROM display.platform_rating "
        "WHERE title_id = $1 AND scale IS NOT NULL ORDER BY platform, metric",
        title_id,
    )
    return [dict(r) for r in rows]


async def genres(conn: asyncpg.Connection, kinds: Sequence[str]) -> list[str]:
    """Raw labels mapped to decision 473's vocabulary rather than listed raw."""
    rows = await conn.fetch(
        "SELECT DISTINCT lower(g.genre) AS genre FROM title_genre g JOIN title t ON t.id = g.title_id "
        "WHERE t.kind = ANY($1) AND g.source <> ALL($2::text[])",
        normalise_kinds(kinds),
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    return genre_vocab.facet({r["genre"] for r in rows})


async def title_genres(conn: asyncpg.Connection, title_id: int) -> list[str]:
    """One title's genres in the facet's vocabulary, so the card and the filter name them alike."""
    rows = await conn.fetch(
        "SELECT DISTINCT lower(genre) AS genre FROM title_genre "
        "WHERE title_id = $1 AND source <> ALL($2::text[])",
        title_id,
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    return genre_vocab.facet({r["genre"] for r in rows})


async def decades(conn: asyncpg.Connection, kinds: Sequence[str]) -> list[int]:
    rows = await conn.fetch(
        "SELECT DISTINCT (t.year / 10) * 10 AS decade FROM title t "
        "WHERE t.kind = ANY($1) AND t.year IS NOT NULL ORDER BY 1 DESC",
        normalise_kinds(kinds),
    )
    return [r["decade"] for r in rows]
