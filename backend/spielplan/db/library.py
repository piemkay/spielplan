"""Catalog queries. Spec v2.1 §6.0 (M0), §4.1 rules 1, 2, 3, 5.

Three rules are enforced here rather than trusted to callers:

* **rule 5** — `kinds` is a *required, non-empty* argument on every listing function. There is
  no "all titles" query in this module, because "the unpartitioned crowd top-10 is 8/10 TV
  series". Owner decision 2026-08-29: kind is a *selection* of one or both, not a one-of-two
  switch — but selecting both is a selection, not a merge. This module only ever LISTS, in a
  kind-independent order, so it may interleave; a surface that RANKS (Rank, Tonight, the Home
  shelves) must render two headed sections and never one interleaved ranking, because the
  measured failure is a shared *ranking*, not a shared screen.
* **rule 1** — the two DNA tiers are returned as two labelled lists. Nothing here unions them.
* **rule 2** — `salience`, `confidence` and `n_sources` appear in ORDER BY and in the payload,
  never in a WHERE. `tests/test_no_weight_filters.py` greps this package to keep it that way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

Kind = Literal["movie", "series"]
KINDS: tuple[Kind, ...] = ("movie", "series")
SeenFilter = Literal["any", "seen", "unseen"]


def normalise_kinds(kinds: Sequence[str] | None) -> list[Kind]:
    """One or both, never neither and never something else.

    An empty selection would silently mean "everything", which is the unpartitioned query
    rule 5 exists to prevent — so it is an error, not a default.
    """
    chosen = [k for k in KINDS if kinds and k in kinds]
    if not chosen:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    return chosen


def _filters(
    *,
    kinds: Sequence[str],
    user_id: int | None = None,
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    seen: SeenFilter = "any",
    person_id: int | None = None,
    owned_only: bool = False,
    runtime_max: int | None = None,
    runtime_min: int | None = None,
    dna: str | None = None,
) -> tuple[str, list[Any]]:
    """The catalog's WHERE clause and its arguments, over alias `t`.

    Extracted so the listing and the hidden-by-kind count are the *same* predicate. They were
    not: the count read every title of the unselected kinds, ignoring the filters the listing
    had applied, so a person filter over a four-title filmography reported "26 series hidden".
    §6.0's count line exists to name what a toggle is hiding, and a number larger than anything
    the toggle could reveal is a worse answer than no number.
    """
    where = ["t.kind = ANY($1)"]
    args: list[Any] = [normalise_kinds(kinds)]

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if q:
        needle = f"%{q.lower()}%"
        where.append(
            f"(lower(t.name) LIKE {arg(needle)} OR EXISTS ("
            f"  SELECT 1 FROM title_alias a WHERE a.title_id = t.id AND lower(a.alias) LIKE {arg(needle)}"
            f"))"
        )
    if genre:
        where.append(
            f"EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = t.id AND g.genre = {arg(genre)})"
        )
    if decade is not None:
        where.append(f"t.year >= {arg(decade)} AND t.year < {arg(decade + 10)}")
    if person_id is not None:
        where.append(
            f"EXISTS (SELECT 1 FROM credit c WHERE c.title_id = t.id AND c.person_id = {arg(person_id)})"
        )
    # §6.3's two filters the §6.0 catalog does not have. Runtime is a bound on `runtime_min`
    # (the column is minutes, the parameter is the ceiling the person asked for) and a NULL
    # runtime is excluded rather than kept: "under 110 minutes" is a claim, and a title whose
    # length nobody knows cannot make it.
    if runtime_max is not None:
        where.append(f"t.runtime_min IS NOT NULL AND t.runtime_min <= {arg(runtime_max)}")
    if runtime_min is not None:
        where.append(f"t.runtime_min IS NOT NULL AND t.runtime_min >= {arg(runtime_min)}")
    if dna:
        # §6.3: "DNA facet/term predicates ("show only `mood.cosy`")". Two rules meet here.
        #
        # Rule 1 — the two tiers are never merged. `dna_tagged` (0004) is the ONE sanctioned
        # union and exists precisely so the `tier` discriminator cannot be dropped; a predicate
        # written against `dna_tag` alone would silently answer "no" for the 11,324 projected
        # titles, and one written against a fresh UNION would lose the tier.
        #
        # Rule 2 — no threshold on `confidence`, `salience` or `n_sources`. The obvious
        # implementation of "only good matches" is a confidence cut, and a 0.5 cut deletes 44%
        # of the extracted tier. Membership is membership; the weights rank, they never filter.
        #
        # The qualified form is built here rather than stored: a `dna_tag` row holds the bare
        # term and its facet in separate columns, so `mood.cosy` exists only at query time.
        needle = arg(dna.strip().lower())
        where.append(
            "EXISTS (SELECT 1 FROM dna_tagged dt WHERE dt.title_id = t.id AND ("
            f"lower(dt.term) = {needle} OR lower(dt.facet || '.' || dt.term) = {needle}))"
        )
    if owned_only:
        where.append("t.is_owned")
    if seen != "any" and user_id is not None:
        # A title with no user_title row is unseen — the absence of a row is the default state,
        # so `unseen` must include it rather than only matching explicit rows.
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
    """§6.3's six filter dimensions, minus `kind`, which the board already partitions by.

    A frozen record rather than eight keyword arguments threaded through three call sites: the
    board, its count and the queue's pool all have to agree about what the person asked for,
    and the M0 bug this module's `_filters` was extracted to fix was two of those disagreeing.
    """

    q: str | None = None
    genre: str | None = None
    decade: int | None = None
    runtime_max: int | None = None
    runtime_min: int | None = None
    seen: SeenFilter = "any"
    dna: str | None = None

    def active(self) -> dict[str, Any]:
        """What is switched on, for the "no match" state to list back (proposal 80)."""
        return {
            name: value
            for name, value in vars(self).items()
            if value not in (None, "", "any")
        }


def rank_filters(
    *, kind: str, user_id: int, filters: RankFilters | None = None
) -> tuple[str, list[Any]]:
    """§6.3's board predicate, over alias `t`, through the *same* builder the catalog uses.

    One builder, deliberately. §6.0's count line and its listing drifted apart at M0 because
    each had its own WHERE; a Rank-specific copy would re-open that class of bug against a
    surface where the number on screen is a tier list rather than a count.
    """
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
    """Which DNA tier(s) matched each survivor of a `dna` predicate.

    §4.1 rule 1: the two tiers "must stay distinguishable", and a filter that returns a title
    without saying whether the match was quote-verified or inferred has merged them in the only
    place it matters — the answer a person reads.
    """
    if not title_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT dt.title_id, dt.tier
        FROM dna_tagged dt
        WHERE dt.title_id = ANY($1::int[])
          AND (lower(dt.term) = $2 OR lower(dt.facet || '.' || dt.term) = $2)
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
    person_id: int | None = None,
    owned_only: bool = False,
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total). `kinds` is mandatory and non-empty — see rule 5 above."""
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

    lim, off = arg(limit), arg(offset)
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.is_owned,
               t.placement, tp.item_n, tp.e_source, {seen_select}
          FROM title t
          -- §8 stage 10's cold badge is about CROWD DATA, and `title.placement` stopped meaning
          -- that when warm was redefined from §5.1's gate: a title with a Backbone row and low
          -- support is placed by the Cold Tower so the blend can fire, while having plenty of
          -- crowd data behind it. `title_prior` carries the quantity the badge is named for.
          LEFT JOIN title_prior tp ON tp.title_id = t.id
          {seen_join}
         WHERE {clause}
         ORDER BY t.year DESC NULLS LAST, lower(t.name)
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
    person_id: int | None = None,
    owned_only: bool = False,
) -> dict[str, int]:
    """How many titles each unselected kind holds **under the same filters as the listing**.

    §6.0's count line has to be able to say "6 films · 2 series hidden": a toggle that hides
    things without saying how many is the silent truncation this control was introduced to fix.

    The filters are not optional decoration. Counting the whole catalog instead made a person
    filter over a four-title filmography report "26 series hidden" — a promise the toggle cannot
    keep, since turning Series on reveals two. Every caller that filters the listing must pass
    the same arguments here.
    """
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


async def credits_for(conn: asyncpg.Connection, title_id: int) -> list[dict[str, Any]]:
    """§4.1: 'credit (dedupe at read time, never at import)'.

    The same person/job can arrive from several sources; import keeps every row so a source can
    be dropped later. Here we collapse to one row per (person, department, job), keeping the
    smallest billing order and listing which sources agreed.
    """
    rows = await conn.fetch(
        """
        SELECT c.person_id, p.name, c.department, c.job,
               min(c.ord)                       AS ord,
               (array_agg(c.character) FILTER (WHERE c.character IS NOT NULL))[1] AS character,
               array_agg(DISTINCT c.source)     AS sources
          FROM credit c JOIN person p ON p.id = c.person_id
         WHERE c.title_id = $1
         GROUP BY c.person_id, p.name, c.department, c.job
         ORDER BY (c.department = 'Directing') DESC, min(c.ord) NULLS LAST, p.name
        """,
        title_id,
    )
    return [dict(r) for r in rows]


async def dna_for(conn: asyncpg.Connection, title_id: int) -> dict[str, list[dict[str, Any]]]:
    """§4.1 rule 1: two tiers, two lists, never merged, never unioned.

    The extracted tier carries its evidence quotes; §4.1: 'a tag without its quote is
    unfalsifiable'. No confidence/salience predicate appears anywhere below (rule 2).
    """
    extracted = await conn.fetch(
        """
        SELECT g.term, g.facet, g.salience, g.confidence, g.n_sources, g.provider,
               COALESCE(
                 json_agg(json_build_object('quote', e.quote, 'source', e.source)
                          ORDER BY e.id) FILTER (WHERE e.id IS NOT NULL),
                 '[]'::json) AS evidence
          FROM dna_tag g
          LEFT JOIN dna_evidence e ON e.dna_tag_id = g.id
         WHERE g.title_id = $1
         GROUP BY g.id, g.term, g.facet, g.salience, g.confidence, g.n_sources, g.provider
         ORDER BY g.salience DESC, g.facet, g.term
        """,
        title_id,
    )
    projected = await conn.fetch(
        """
        SELECT term, facet, weight, via
          FROM dna_projected
         WHERE title_id = $1
         ORDER BY weight DESC NULLS LAST, facet, term
        """,
        title_id,
    )
    return {"extracted": [dict(r) for r in extracted], "projected": [dict(r) for r in projected]}


async def platform_ratings(conn: asyncpg.Connection, title_id: int) -> list[dict[str, Any]]:
    """§4.1 rule 3: display-only. This is the ONLY function that reads `display`, and its
    result is labelled all the way to the UI. Aggregate platform scores are a popularity
    conduit and are banned as model features."""
    rows = await conn.fetch(
        "SELECT platform, score, votes FROM display.platform_rating "
        "WHERE title_id = $1 ORDER BY platform",
        title_id,
    )
    return [dict(r) for r in rows]


async def genres(conn: asyncpg.Connection, kinds: Sequence[str]) -> list[str]:
    rows = await conn.fetch(
        "SELECT DISTINCT g.genre FROM title_genre g JOIN title t ON t.id = g.title_id "
        "WHERE t.kind = ANY($1) ORDER BY 1",
        normalise_kinds(kinds),
    )
    return [r["genre"] for r in rows]


async def decades(conn: asyncpg.Connection, kinds: Sequence[str]) -> list[int]:
    rows = await conn.fetch(
        "SELECT DISTINCT (t.year / 10) * 10 AS decade FROM title t "
        "WHERE t.kind = ANY($1) AND t.year IS NOT NULL ORDER BY 1 DESC",
        normalise_kinds(kinds),
    )
    return [r["decade"] for r in rows]
