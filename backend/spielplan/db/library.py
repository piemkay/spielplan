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

from spielplan.db import dna_terms

Kind = Literal["movie", "series"]
KINDS: tuple[Kind, ...] = ("movie", "series")
SeenFilter = Literal["any", "seen", "unseen"]

# Postgres's LIKE takes backslash as its escape character unless ESCAPE says otherwise, so the
# three characters a needle has to lose their meaning are the backslash itself and the two
# wildcards. Order matters: the backslash is doubled first, or the escapes added after it would
# be escaped in turn.
_LIKE_SPECIALS = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))


def _like_needle(q: str) -> str:
    """`q` as a substring LIKE pattern that matches it **literally**.

    §6.0 asks for "filter/search on title/alias" and both search surfaces built `%{q.lower()}%`
    straight from the input, so the wildcards kept their meaning: `%` returned the whole
    catalogue, `_` matched every one-character name, `h_at` returned *Heat*, and a title with a
    percent sign in it (`100% Wolf`) could not be searched for exactly. §6.0's count line was
    wrong in the same way, because it is computed from the same predicate.

    This is not injection and the fix is not sanitising: the value stays a bound parameter and
    nothing about it reaches the SQL text. §4.1 rule 8's distinction applies — escaping is how a
    metacharacter keeps being the character the person typed, and cleaning would be discarding
    it. `100%` still finds `100% Wolf`; it just no longer finds everything else too.

    Shared with `scoring/serve.py`, which imported it rather than keeping the second copy that
    made this one bug two. [M4.9 finding 12]
    """
    needle = q.lower()
    for character, escaped in _LIKE_SPECIALS:
        needle = needle.replace(character, escaped)
    return f"%{needle}%"


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
        needle = _like_needle(q)
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
        # §4.3: the vocabulary id IS `facet.term`. The corpus ships `dna:mood.cosy`, the loader
        # stores that string whole, and `facet` is the id's own prefix — so the qualified form
        # is not built here, it is what the column already holds. The predicate that shipped
        # constructed `facet || '.' || term` and therefore matched `mood_tone.mood.cosy` and
        # nothing a person would type: `?dna=cosy` (the bare term the §6.3 placeholder invites)
        # matched no row at all. `split_part(term, '.', 2)` is the other half of the same id,
        # so both spellings select the same titles and a wrong facet still selects none.
        # [M4.9 finding 2]
        #
        # §4.3 + §10 — one vocabulary. `dna_vocabulary` is versioned and a re-import leaves two
        # versions coexisting; `home/why.py` scoped the shelves for that reason and this
        # predicate did not, so the first vocabulary migration would have put a superseded
        # term's titles on the board. The version arrives as a subquery rather than a bound
        # argument because this builder is synchronous and shared with the Rank board, which
        # has no awaitable seam to thread one through; `db/dna_terms.py` owns both spellings.
        needle = arg(dna.strip().lower())
        where.append(
            "EXISTS (SELECT 1 FROM dna_tagged dt WHERE dt.title_id = t.id"
            f" AND dt.version = {dna_terms.ACTIVE_VERSION} AND ("
            f"lower(dt.term) = {needle} OR split_part(lower(dt.term), '.', 2) = {needle}))"
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

    The predicate is `_filters`'s, clause for clause, version scope included: this reports
    *which tier admitted* a row the filter already returned, so a spelling or a vocabulary the
    two disagree about would badge a title under a term that did not select it — or, as it
    shipped, report nothing for every row the board is showing. [M4.9 findings 2 and 10]
    """
    if not title_ids:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT dt.title_id, dt.tier
        FROM dna_tagged dt
        WHERE dt.title_id = ANY($1::int[])
          AND dt.version = {dna_terms.ACTIVE_VERSION}
          AND (lower(dt.term) = $2 OR split_part(lower(dt.term), '.', 2) = $2)
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
         ORDER BY t.year DESC NULLS LAST, lower(t.name), t.id
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
    be dropped later. Here we collapse to **one row per (person, job)**, keeping the smallest
    billing order and listing which sources agreed.

    NOT per (person, department, job). TMDB records the same job under two department spellings
    — `Acting`/`Actor`, `Directing`/`Director`, `Editing`/`Editor` — and the real export carries
    7,918 (title, person, job) triples spanning more than one of them across 1,216 of 19,071
    titles. Grouping on the department therefore returned two rows the §6.0 card cannot tell
    apart, and Svelte 5's keyed each throws on the duplicate key in the production branch too.
    §4.1's dedupe is a statement about the *person and the job*; the department is how a source
    files it.

    The departments are aggregated rather than chosen between (`array_agg(DISTINCT …)`, the same
    shape `sources` already has), because §4.1 rule 1 keeps what the sources said, and nothing
    here normalises a spelling: eighteen ship. `department` stays a single string for the callers
    that read it and `min()` is what picks it — an alphabetical accident, not a rule, and the
    corpus is the proof. Of its 7,918 colliding groups, min() lands on the TMDB canonical
    spelling for 6,565 (`Actor`/`Acting` → Acting, `Editing`/`Editor` → Editing,
    `Director`/`Directing` → Directing) and on the other spelling for 1,353
    (`Writer`/`Writing` → **Writer**, `Sound`/`Music` → Music, `Production Designer`/`Art` →
    Art). Which is why `departments` is what carries the truth and this key is compatibility.
    """
    rows = await conn.fetch(
        """
        SELECT c.person_id, p.name, min(c.department) AS department,
               array_agg(DISTINCT c.department) AS departments, c.job,
               -- 0015 renamed `ord` to `billing_order` (the corpus's own column name); the
               -- response key stays `ord` because §6.0's card reads it.
               min(c.billing_order)             AS ord,
               -- Ordered, because a bare `[1]` over an unordered array_agg is COPY order: 2,300
               -- (title, person) pairs carry more than one distinct character across sources, so
               -- which one this payload carried depended on the physical row order of the
               -- import. The PAYLOAD is what the aggregate makes deterministic, not a rendering:
               -- §6.0's card list does not name the character and `TitleDetail.svelte` prints
               -- name and job alone, so the field ships and no surface shows it (decision 197).
               (array_agg(c.character ORDER BY c.billing_order NULLS LAST, c.source)
                    FILTER (WHERE c.character IS NOT NULL))[1] AS character,
               array_agg(DISTINCT c.source)     AS sources
          FROM credit c JOIN person p ON p.id = c.person_id
         WHERE c.title_id = $1
         GROUP BY c.person_id, p.name, c.job
         -- `c.department` is no longer a grouping column, so the directing-first sort has to be
         -- an aggregate: Postgres rejects the bare column outright rather than mis-sorting.
         ORDER BY bool_or(c.department = 'Directing') DESC, min(c.billing_order) NULLS LAST, p.name
        """,
        title_id,
    )
    return [dict(r) for r in rows]


async def dna_for(
    conn: asyncpg.Connection, title_id: int, *, version: str | None
) -> dict[str, list[dict[str, Any]]]:
    """§4.1 rule 1: two tiers, two lists, never merged, never unioned.

    The extracted tier carries its evidence quotes; §4.1: 'a tag without its quote is
    unfalsifiable'. No confidence/salience predicate appears anywhere below (rule 2).

    `version` is the active vocabulary and is required rather than defaulted, because a card
    that silently showed every version's tags is exactly the failure §10 warns about ("a bundle
    re-import leaves two vocabularies coexisting") and a default would have hidden it a second
    time. `None` means no bundle has been imported, and then there is nothing to show: the
    caller resolves it once through `db/dna_terms.active_version` and hands the same string to
    §6.4's neighbour queries, so one card cannot mix two vocabularies within one response.
    [M4.9 finding 10]
    """
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
    """§4.1 rule 3: display-only. This is the ONLY function that reads `display`, and its
    result is labelled all the way to the UI. Aggregate platform scores are a popularity
    conduit and are banned as model features.

    The row is per (platform, metric) since 0015, because that is how the corpus keys it, and
    one platform legitimately has two: metacritic ships a critic_score and a user_score on
    different scales, and collapsing them left whichever one COPY reached last.

    **What the card shows: the metrics that are scores.** The corpus also keeps `popularity`,
    `critic_review_count`, `audience_rating_count` and trakt's ten `dist_N` histogram buckets
    in this table — 12 of one measured title's 22 shipped rows. §6.0 requires the caption to
    travel with the number, and those have no caption they could honestly print: `dist_7 =
    2197` is not a rating out of anything. The corpus marks the difference itself by recording
    `scale` for a score and NULL for the rest, so that is the predicate. Nothing is being
    hidden from a model here — rule 3 already forbids every row in this table from becoming a
    feature; this is a rendering choice about a display block.
    """
    rows = await conn.fetch(
        "SELECT platform, metric, score, scale, votes FROM display.platform_rating "
        "WHERE title_id = $1 AND scale IS NOT NULL ORDER BY platform, metric",
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
