"""The why-line machinery: DNA terms chosen first, membership derived from them.

Spec v2.1 §6.0 (M2 Home), §6.8 ("quiet reasons"), §4.1 rules 1 and 2, §6.4.

§6.0: "then shelves, each with a **mandatory one-line why in vocabulary terms** — a shelf that
cannot say why it exists doesn't ship". Proposal 24 sharpens it into the rule this module
exists to make structural: "The why-line must name terms **every** item on the shelf carries —
the prototype names the anchor's first two terms while admitting members on any two shared
terms, so a card can be shown under a reason it does not satisfy."

THE INVERSION. The prototype picks a list and then labels it. Everything here picks the TERMS
first and derives the list from them, so "shares obsession + morally-grey with it" is true of
every card by construction rather than by inspection. `common_terms` then re-derives, from the
cards that were actually returned, the terms all of them carry — the same function serves as
the shelf builders' verifier (`unsupported`), so a shelf whose why drifts from its membership
fails inside the request rather than in review.

§4.1 RULE 1. Both DNA tiers are read through the sanctioned `dna_tagged` view and nowhere else,
so the `tier` discriminator travels into the payload: a why-line that names a projected term
says so. A term present in both tiers is named once, as extracted (bool_or), because the
payload must never upgrade a projected term into a quote-verified one.

§4.1 RULE 2. salience, confidence and n_sources appear in ORDER BY and in the SELECT list and
in no predicate anywhere below. They decide WHICH TERM GETS NAMED. They never decide which
title is admitted — that is what makes a 0.5 cut (which would delete 44% of the extracted tier)
unrepresentable here rather than merely discouraged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

# §6.0 shelf 1's candidate pool. C(8,2) = 28 pairs is the whole search, which is why the pair
# choice can be exact rather than greedy.
ANCHOR_TERM_POOL = 8

# How many terms a why-line may name from the intersection all its cards carry. Copy, not a
# tuned number: a one-line why that names five terms is not a one-line why.
NAMED_TERM_CAP = 2

# §4.1 rule 2 made arithmetic. The extracted tier outranks the projected tier for *naming*
# because §4.1 calls the first quote-verified and the second inferred; both tiers stay fully
# admissible. Ranges: extracted 0.73..1.00, projected 0.00..0.30.
TERM_RANK = """
        CASE d.tier
            WHEN 'extracted' THEN 0.60 + 0.40 * (COALESCE(d.salience, 1.0) / 3.0)
            ELSE 0.30 * COALESCE(d.confidence, 0.5)
        END
"""

ROLES = ("member", "anchor_side")


@dataclass(frozen=True)
class WhyTerm:
    """One DNA term a why-line names, with the two things that make it checkable.

    `tier` is §4.1 rule 1's discriminator. `role` is the honesty flag: a **member** term is
    carried by every card on the shelf and is the shelf's admission predicate; an
    **anchor_side** term describes the user's own liked region (§6.4's "unvisited region of DNA
    space next to what you like") and is deliberately NOT on the cards, which are unvisited by
    definition. Collapsing the two is how a shelf ends up saying the wrong why.
    """

    term: str
    facet: str
    tier: str
    role: str = "member"

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "facet": self.facet, "tier": self.tier, "role": self.role}

    def with_role(self, role: str) -> WhyTerm:
        return WhyTerm(term=self.term, facet=self.facet, tier=self.tier, role=role)


async def vocabulary_version(conn: asyncpg.Connection) -> str | None:
    """The vocabulary the shelves name terms from, or None when M0 imported none.

    §4.3 ships `dna_vocab/v1/`; a household that has imported two bundles has two versions and
    the shelves must not mix them, because a term's facet and gloss are version-scoped.
    """
    return await conn.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC, version DESC LIMIT 1"
    )


async def terms_for(
    conn: asyncpg.Connection, title_id: int, *, version: str, limit: int = ANCHOR_TERM_POOL
) -> list[WhyTerm]:
    """One title's terms, best-named first. Rule 2: the ranking is an ORDER BY, never a filter.

    Both tiers are returned; a term carried in both is returned once and tiered `extracted`,
    so the pool cannot silently promote an inferred tag.
    """
    rows = await conn.fetch(
        f"""
        SELECT d.term,
               min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               max({TERM_RANK}) AS term_rank
          FROM dna_tagged d
         WHERE d.title_id = $1 AND d.version = $2
         GROUP BY d.term
         ORDER BY max({TERM_RANK}) DESC, d.term
         LIMIT $3
        """,
        title_id,
        version,
        limit,
    )
    return [WhyTerm(term=r["term"], facet=r["facet"], tier=r["tier"]) for r in rows]


async def rank_of(
    conn: asyncpg.Connection, terms: Sequence[str], *, version: str
) -> dict[str, float]:
    """The naming rank of each term over the whole catalog — the tie-break when two candidate
    pairs cover the same number of titles."""
    if not terms:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT d.term, max({TERM_RANK}) AS term_rank
          FROM dna_tagged d
         WHERE d.version = $1 AND d.term = ANY($2)
         GROUP BY d.term
        """,
        version,
        list(terms),
    )
    return {r["term"]: float(r["term_rank"]) for r in rows}


async def best_pair(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    version: str,
    anchor_id: int,
    pool: Sequence[WhyTerm],
    floor: int,
) -> tuple[WhyTerm, WhyTerm, int] | None:
    """The two anchor terms that TOGETHER cover the most unseen owned titles of this kind.

    Proposal 24's rule, executed in the only order that makes it true: the pair is chosen for
    the size of its intersection, and the shelf is then that intersection. Returns None when no
    pair reaches `floor` — the shelf is then absent rather than shown under a why it cannot
    support.

    Ties break on the pair's naming rank, then lexicographically, so the same library always
    produces the same shelf.
    """
    if len(pool) < 2:
        return None
    by_term = {t.term: t for t in pool}
    ranks = await rank_of(conn, list(by_term), version=version)
    row = await conn.fetchrow(
        """
        WITH aterm(term, term_rank) AS (SELECT * FROM unnest($4::text[], $5::float8[])),
        cand AS (
            SELECT d.title_id, d.term
              FROM dna_tagged d
              JOIN title t ON t.id = d.title_id
              LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
             WHERE d.version = $6 AND d.term = ANY($4) AND t.kind = $2 AND t.is_owned
               AND t.id <> $3 AND COALESCE(ut.state, 'unseen') = 'unseen'
             GROUP BY d.title_id, d.term
        ),
        pairs AS (
            SELECT a.term AS t1, b.term AS t2, count(*) AS n, wa.term_rank + wb.term_rank AS rsum
              FROM cand a
              JOIN cand b ON b.title_id = a.title_id AND b.term > a.term
              JOIN aterm wa ON wa.term = a.term
              JOIN aterm wb ON wb.term = b.term
             GROUP BY a.term, b.term, wa.term_rank, wb.term_rank
        )
        SELECT t1, t2, n FROM pairs
         WHERE n >= $7
         ORDER BY n DESC, rsum DESC, t1, t2
         LIMIT 1
        """,
        user_id,
        kind,
        anchor_id,
        list(by_term),
        [ranks.get(term, 0.0) for term in by_term],
        version,
        floor,
    )
    if row is None:
        return None
    return by_term[row["t1"]], by_term[row["t2"]], int(row["n"])


async def carriers(
    conn: asyncpg.Connection,
    *,
    terms: Sequence[str],
    kind: str,
    version: str,
    user_id: int,
    exclude: Sequence[int] = (),
    unseen_only: bool = True,
) -> list[int]:
    """Every owned title of this kind carrying ALL of `terms`. The shelf's membership, exactly.

    `HAVING count(DISTINCT d.term) = cardinality($2)` is the whole of proposal 24: a title
    carrying one of the two named terms is not on the shelf, because the why-line says "shares
    {t1} + {t2}" and not "shares one of".
    """
    if not terms:
        return []
    rows = await conn.fetch(
        f"""
        SELECT d.title_id
          FROM dna_tagged d
          JOIN title t ON t.id = d.title_id
          LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
         WHERE d.version = $3 AND d.term = ANY($2) AND t.kind = $4 AND t.is_owned
           AND NOT (t.id = ANY($5))
           {"AND COALESCE(ut.state, 'unseen') = 'unseen'" if unseen_only else ""}
         GROUP BY d.title_id
        HAVING count(DISTINCT d.term) = cardinality($2)
        """,
        user_id,
        list(terms),
        version,
        kind,
        list(exclude),
    )
    return [int(r["title_id"]) for r in rows]


async def common_terms(
    conn: asyncpg.Connection,
    *,
    title_ids: Sequence[int],
    version: str,
    limit: int = NAMED_TERM_CAP,
) -> list[WhyTerm]:
    """The terms carried by EVERY one of these titles, best-named first.

    Two jobs, one query, deliberately. It is the *verifier* — a why-line's member terms must be
    a subset of this set, which is what `unsupported()` below checks — and it is the *source*
    for the shelves whose predicate is not itself a DNA term (§6.0's "Top of your ledger",
    "Under 110 minutes", "New in the library"). Those shelves may still carry a vocabulary
    clause, and because it is computed by intersection over the cards that were actually
    returned, the clause cannot be false.
    """
    ids = sorted({int(t) for t in title_ids})
    if not ids:
        return []
    rows = await conn.fetch(
        f"""
        SELECT d.term,
               min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               max({TERM_RANK}) AS term_rank
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = ANY($2)
         GROUP BY d.term
        HAVING count(DISTINCT d.title_id) = cardinality($2)
         ORDER BY max({TERM_RANK}) DESC, d.term
         LIMIT $3
        """,
        version,
        ids,
        limit,
    )
    return [WhyTerm(term=r["term"], facet=r["facet"], tier=r["tier"]) for r in rows]


async def unsupported(
    conn: asyncpg.Connection,
    *,
    why_terms: Sequence[WhyTerm],
    title_ids: Sequence[int],
    version: str,
) -> list[str]:
    """Member terms the why-line names that some card does not carry. Must always be empty.

    §6.0: "a shelf that cannot say why it exists doesn't ship"; proposal 24: "nor does one that
    says the wrong why". The shelf builders derive membership from the terms, so this is a
    second, independent read of the same claim — cheap, and it turns a construction bug into a
    suppressed shelf instead of a lie on the user's screen.
    """
    named = [t.term for t in why_terms if t.role == "member"]
    if not named or not title_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT u.term
          FROM unnest($1::text[]) AS u(term)
         WHERE (
            SELECT count(DISTINCT d.title_id) FROM dna_tagged d
             WHERE d.version = $2 AND d.term = u.term AND d.title_id = ANY($3)
         ) <> cardinality($3)
        """,
        named,
        version,
        sorted({int(t) for t in title_ids}),
    )
    return [r["term"] for r in rows]


async def carried_by(
    conn: asyncpg.Connection,
    *,
    title_ids: Sequence[int],
    terms: Sequence[str],
    version: str,
) -> dict[int, list[str]]:
    """Which of the named terms each card actually carries — the receipt printed on the card.

    §6.8: model numbers and reasons appear "next to their name, never bare". A card that shows
    a term it does not carry would be exactly the bug proposal 24 names, so the card's chips
    come from the database rather than from the shelf's claim.
    """
    ids = [int(t) for t in title_ids]
    if not ids or not terms:
        return {i: [] for i in ids}
    rows = await conn.fetch(
        """
        SELECT d.title_id, array_agg(DISTINCT d.term ORDER BY d.term) AS terms
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = ANY($2) AND d.term = ANY($3)
         GROUP BY d.title_id
        """,
        version,
        ids,
        list(terms),
    )
    found = {int(r["title_id"]): list(r["terms"]) for r in rows}
    return {i: found.get(i, []) for i in ids}


async def frontier_term(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    version: str,
    min_seen: int,
    carrier_floor: int,
    liked_pool: int = 12,
) -> tuple[WhyTerm, WhyTerm, float, float] | None:
    """§6.4's explore frontier as a shelf: an unvisited term that sits next to a liked one.

    Returns (candidate, neighbour, cosine, affinity) or None.

    "the *adjacent possible* — regions of DNA space near the user's liked regions but
    unvisited". Three literal readings, and each is what makes the shelf title true:

    * **unvisited** is zero coverage, not low coverage. The title says "You've never watched
      anything {term}", so one seen carrier disqualifies the term outright.
    * **near** is co-occurrence in this household's own owned catalog — cos(c, L) =
      |carriers of both| / sqrt(|c| · |L|) — so the edge is a nameable DNA term (§6.4: "Every
      connection is *nameable* — edges are DNA terms, never opaque similarity") rather than a
      distance in an embedding nobody can read.
    * **liked** is the user's own posterior CDF, per §5.2's "empirical CDF of the user's own
      fitted `s` values, computed per kind", centred at 0.5 so an indifferent term scores 0.
    """
    seen_n = await conn.fetchval(
        """
        SELECT count(*) FROM user_title ut JOIN title t ON t.id = ut.title_id
         WHERE ut.user_id = $1 AND ut.state = 'seen' AND t.kind = $2
        """,
        user_id,
        kind,
    )
    if int(seen_n or 0) < min_seen:
        return None

    candidates = await conn.fetch(
        """
        WITH seen_terms AS (
            SELECT DISTINCT d.term
              FROM dna_tagged d
              JOIN title t ON t.id = d.title_id AND t.kind = $2
              JOIN user_title ut ON ut.title_id = d.title_id AND ut.user_id = $1
                                AND ut.state = 'seen'
             WHERE d.version = $3
        ),
        pool AS (
            SELECT d.term,
                   min(d.facet) AS facet,
                   CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted'
                        ELSE 'projected' END AS tier,
                   count(DISTINCT d.title_id) AS n
              FROM dna_tagged d
              JOIN title t ON t.id = d.title_id
              LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
             WHERE d.version = $3 AND t.kind = $2 AND t.is_owned
               AND COALESCE(ut.state, 'unseen') = 'unseen'
             GROUP BY d.term
        )
        SELECT p.term, p.facet, p.tier, p.n FROM pool p
         WHERE p.term NOT IN (SELECT term FROM seen_terms) AND p.n >= $4
         ORDER BY p.n DESC, p.term
        """,
        user_id,
        kind,
        version,
        carrier_floor,
    )
    if not candidates:
        return None

    liked = await conn.fetch(
        """
        SELECT d.term,
               min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               avg(ls.cdf) - 0.5 AS aff
          FROM dna_tagged d
          JOIN ledger_state ls ON ls.title_id = d.title_id AND ls.user_id = $1
          JOIN user_title ut ON ut.title_id = d.title_id AND ut.user_id = $1 AND ut.state = 'seen'
          JOIN title t ON t.id = d.title_id AND t.kind = $2
         WHERE d.version = $3 AND ls.cdf IS NOT NULL
         GROUP BY d.term
         ORDER BY avg(ls.cdf) - 0.5 DESC, d.term
         LIMIT $4
        """,
        user_id,
        kind,
        version,
        liked_pool,
    )
    liked = [r for r in liked if float(r["aff"]) > 0.0]
    if not liked:
        return None

    pairs = await conn.fetch(
        """
        WITH scoped AS (
            SELECT DISTINCT d.term, d.title_id
              FROM dna_tagged d JOIN title t ON t.id = d.title_id
             WHERE d.version = $1 AND t.kind = $2 AND t.is_owned
        ),
        sizes AS (SELECT term, count(*) AS n FROM scoped GROUP BY term)
        SELECT c.term AS cand, l.term AS neighbour, count(*) AS shared,
               sc.n AS cand_n, sl.n AS neighbour_n
          FROM scoped c
          JOIN scoped l ON l.title_id = c.title_id AND l.term <> c.term
          JOIN sizes sc ON sc.term = c.term
          JOIN sizes sl ON sl.term = l.term
         WHERE c.term = ANY($3) AND l.term = ANY($4)
         GROUP BY c.term, l.term, sc.n, sl.n
        """,
        version,
        kind,
        [r["term"] for r in candidates],
        [r["term"] for r in liked],
    )
    if not pairs:
        return None

    cand_by_term = {r["term"]: r for r in candidates}
    liked_by_term = {r["term"]: r for r in liked}
    scored = []
    for row in pairs:
        cos = float(row["shared"]) / ((float(row["cand_n"]) * float(row["neighbour_n"])) ** 0.5)
        aff = float(liked_by_term[row["neighbour"]]["aff"])
        # Ties: the larger candidate pool first (a bigger unvisited region is a better shelf),
        # then term ascending, so the same library always names the same term.
        scored.append((-(cos * aff), -int(cand_by_term[row["cand"]]["n"]), row["cand"], row, cos, aff))
    scored.sort(key=lambda s: s[:3])
    _neg, _n, _term, row, cos, aff = scored[0]
    c, ln = cand_by_term[row["cand"]], liked_by_term[row["neighbour"]]
    return (
        WhyTerm(term=c["term"], facet=c["facet"], tier=c["tier"], role="member"),
        WhyTerm(term=ln["term"], facet=ln["facet"], tier=ln["tier"], role="anchor_side"),
        cos,
        aff,
    )


def phrase(terms: Sequence[WhyTerm]) -> str:
    """`{term} + {term}` — §6.0's own why-line shape for shelf 1."""
    return " + ".join(t.term for t in terms)
