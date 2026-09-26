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
from itertools import combinations
from typing import Any

import asyncpg

from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.ledger.observations import LIVE_LABEL_SQL

# `terms_for`'s default: a title's eight best-named terms. It was shelf 1's candidate pool (28
# pairs) until decision 513 had shelf 1 read every term of its anchor (`limit=None`); the default
# now serves the callers that want a title's best-named few (ops/m49_exit_criterion.py).
ANCHOR_TERM_POOL = 8

# How many terms a why-line may name from the intersection all its cards carry. Copy, not a
# tuned number: a one-line why that names five terms is not a one-line why.
NAMED_TERM_CAP = 2

# Decision 514: "which you like" needs this many liked titles of the kind behind it.
LIKED_TERM_MIN = 3

# Decision 513: shelf 1 names its pair from the anchor's nearest titles, this many per card slot.
NEIGHBOURHOOD_PER_CARD = 2

# §4.1 rule 2 made arithmetic. The extracted tier outranks the projected tier for *naming*
# because §4.1 calls the first quote-verified and the second inferred; both tiers stay fully
# admissible.
#
# The expression itself lives in `db/dna_terms.py` because `tonight/dna.py` held a verbatim copy
# of it and the two drifted together off the shipped data: the comment here used to promise
# "extracted 0.73..1.00, projected 0.00..0.30" while the projected branch ran to 2.40, because
# the column the `dna_tagged` view calls `confidence` holds `n_sources` for that tier. It is now
# 0.733..1.00 against 0.10..0.267 and the two bands cannot cross. Reading it from one module is
# what makes that a fact about the app rather than about this file. [M4.9 finding 20, decision
# 188]
TERM_RANK = dna_terms.TERM_WEIGHT

# The label rides beside the term in every read that builds a `WhyTerm`, joined on the term's own
# version so a superseded vocabulary cannot name it. `max` because those reads group by term.
LABEL_JOIN = "LEFT JOIN dna_term dl ON dl.version = d.version AND dl.term = d.term"

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
    # The vocabulary's own name for the term (`dna_term.label`), read beside it so a why-line is
    # written in words and never in ids: "World War II", not `era.wwii` (§6.8, decision 486).
    label: str | None = None

    @property
    def name(self) -> str:
        return dna_terms.label_of(self.term, self.label)

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "facet": self.facet, "tier": self.tier, "role": self.role,
                "label": self.name}

    def with_role(self, role: str) -> WhyTerm:
        return WhyTerm(term=self.term, facet=self.facet, tier=self.tier, role=role,
                       label=self.label)


async def vocabulary_version(conn: asyncpg.Connection) -> str | None:
    """The vocabulary the shelves name terms from, or None when M0 imported none.

    §4.3 ships `dna_vocab/v1/`; a household that has imported two bundles has two versions and
    the shelves must not mix them, because a term's facet and gloss are version-scoped.

    That sentence is the whole reason this function existed here first, and M4.9 found that the
    title card, the catalog/Rank DNA predicate and §6.4's wander neighbours had never applied
    it. Rather than teach three more modules to resolve the version, the resolution moved down
    to `db/dna_terms.py` — where the catalog's synchronous WHERE builder can also reach it as a
    scalar subquery — and this stays as the name the shelves call it by. One statement, one
    answer; two would be the second notion of "active vocabulary" this comment warns about.
    [M4.9 finding 10]
    """
    return await dna_terms.active_version(conn)


async def terms_for(
    conn: asyncpg.Connection, title_id: int, *, version: str, limit: int | None = ANCHOR_TERM_POOL
) -> list[WhyTerm]:
    """One title's terms, best-named first. Rule 2: the ranking is an ORDER BY, never a filter.

    Both tiers are returned; a term carried in both is returned once and tiered `extracted`,
    so the pool cannot silently promote an inferred tag. `limit=None` returns every term, which
    is what shelf 1 names its pair from (decision 513).
    """
    rows = await conn.fetch(
        f"""
        SELECT d.term,
               min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               max({TERM_RANK}) AS term_rank,
               max(dl.label) AS label
          FROM dna_tagged d
          {LABEL_JOIN}
         WHERE d.title_id = $1 AND d.version = $2
         GROUP BY d.term
         ORDER BY max({TERM_RANK}) DESC, d.term
         LIMIT $3
        """,
        title_id,
        version,
        limit,
    )
    return [
        WhyTerm(term=r["term"], facet=r["facet"], tier=r["tier"], label=r["label"]) for r in rows
    ]


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
               max({TERM_RANK}) AS term_rank,
               max(dl.label) AS label
          FROM dna_tagged d
          {LABEL_JOIN}
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
    return [
        WhyTerm(term=r["term"], facet=r["facet"], tier=r["tier"], label=r["label"]) for r in rows
    ]


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
    liked_pool: int = 24,
    exclude: Sequence[int] = (),
) -> tuple[WhyTerm, WhyTerm, float, float] | None:
    """§6.4's explore frontier as a shelf: an unvisited term that sits next to a liked one.

    Returns (candidate, neighbour, cosine, affinity) or None.

    "the *adjacent possible* — regions of DNA space near the user's liked regions but
    unvisited". Four literal readings, and each is what makes the shelf's two lines true:

    * **unvisited** is zero coverage, not low coverage. The title says "You've never watched
      anything {term}", so one seen carrier disqualifies the term outright.
    * **near** is co-occurrence in this household's own owned catalog — cos(c, L) =
      |carriers of both| / sqrt(|c| · |L|) — so the edge is a nameable DNA term (§6.4: "Every
      connection is *nameable* — edges are DNA terms, never opaque similarity") rather than a
      distance in an embedding nobody can read.
    * **which you like** is what the person said (decision 514): at least `LIKED_TERM_MIN` of
      their liked titles of the kind carry the term, and more than half of the rated titles
      carrying it are liked. It read the Ledger's CDF averaged over whatever carried the term,
      with no support behind it, so one projected tag on one liked film made "wartime backdrop"
      a term Jenny liked - and the shelf then told her she had never watched World War II.
    * **close to, not the same as**: the neighbour comes from another facet. Inside one facet a
      near term is a narrower or broader name for the same thing, and "never watched World War
      I, close to turn of the 20th century, which you like" reads as the contradiction it is.

    `exclude` is decision 475's claim and decision 512's avoid set: titles a shelf built earlier
    already shows, and titles the member avoids, do not count toward a candidate's carriers, so a
    term they would empty below the floor is never chosen over one that still fills the shelf.
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
        f"""
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
                   count(DISTINCT d.title_id) AS n,
                   max(dl.label) AS label
              FROM dna_tagged d
              {LABEL_JOIN}
              JOIN title t ON t.id = d.title_id
              LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
             WHERE d.version = $3 AND t.kind = $2 AND t.is_owned
               AND COALESCE(ut.state, 'unseen') = 'unseen' AND NOT (t.id = ANY($5))
             GROUP BY d.term
        )
        SELECT p.term, p.facet, p.tier, p.n, p.label FROM pool p
         WHERE p.term NOT IN (SELECT term FROM seen_terms) AND p.n >= $4
         ORDER BY p.n DESC, p.term
        """,
        user_id,
        kind,
        version,
        carrier_floor,
        list(exclude),
    )
    if not candidates:
        return None

    # The affinity is the net share of liked over rated carriers, shrunk by two pseudo-ratings so
    # three for three does not outrank nine of ten; the HAVING is the sentence's own claim.
    liked = await conn.fetch(
        f"""
        WITH lv AS ({LIVE_LABEL_SQL}),
        rated AS (
            SELECT lv.title_id, lv.value
              FROM lv
              JOIN title t ON t.id = lv.title_id AND t.kind = $2
              JOIN user_title ut ON ut.title_id = lv.title_id AND ut.user_id = $1
                                AND ut.state = 'seen'
        )
        SELECT d.term,
               min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               (count(DISTINCT r.title_id) FILTER (WHERE r.value = 2)
                - count(DISTINCT r.title_id) FILTER (WHERE r.value = 0))::float8
                 / (count(DISTINCT r.title_id) + 2) AS aff,
               max(dl.label) AS label
          FROM rated r
          JOIN dna_tagged d ON d.title_id = r.title_id AND d.version = $3
          {LABEL_JOIN}
         GROUP BY d.term
        HAVING count(DISTINCT r.title_id) FILTER (WHERE r.value = 2) >= $5
           AND 2 * count(DISTINCT r.title_id) FILTER (WHERE r.value = 2) > count(DISTINCT r.title_id)
         ORDER BY aff DESC, d.term
         LIMIT $4
        """,
        user_id,
        kind,
        version,
        liked_pool,
        LIKED_TERM_MIN,
    )
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
        if cand_by_term[row["cand"]]["facet"] == liked_by_term[row["neighbour"]]["facet"]:
            continue    # the same thing under a narrower or broader name (decision 514)
        cos = float(row["shared"]) / ((float(row["cand_n"]) * float(row["neighbour_n"])) ** 0.5)
        aff = float(liked_by_term[row["neighbour"]]["aff"])
        # Ties: the larger candidate pool first (a bigger unvisited region is a better shelf),
        # then the terms ascending, so the same library always names the same pair.
        scored.append((-(cos * aff), -int(cand_by_term[row["cand"]]["n"]), row["cand"],
                       row["neighbour"], row, cos, aff))
    if not scored:
        return None
    scored.sort(key=lambda s: s[:4])
    _neg, _n, _term, _near, row, cos, aff = scored[0]
    c, ln = cand_by_term[row["cand"]], liked_by_term[row["neighbour"]]
    return (
        WhyTerm(term=c["term"], facet=c["facet"], tier=c["tier"], role="member",
                label=c["label"]),
        WhyTerm(term=ln["term"], facet=ln["facet"], tier=ln["tier"], role="anchor_side",
                label=ln["label"]),
        cos,
        aff,
    )


def phrase(terms: Sequence[WhyTerm]) -> str:
    """`{term} + {term}` — §6.0's own why-line shape for shelf 1, in the vocabulary's words."""
    return " + ".join(t.name for t in terms)


# --- shelf 1's membership: likeness to the anchor (decisions 475 and 513) ---------------------


def specificity_ctes(kind: str, version: str) -> str:
    """The CTEs that weigh a term by its rarity in the household's owned titles of one kind:
    `owned`, `tagged` (their distinct terms, either tier) and `spec` (term -> ln(N / carriers)).
    Shared by shelf 1 and the title card's why-line (decision 515), which must agree about what
    makes two titles alike. `kind` and `version` are the caller's placeholders."""
    return f"""
        owned AS (
            SELECT t.id FROM title t WHERE t.kind = {kind} AND t.is_owned
        ),
        tagged AS (
            SELECT DISTINCT d.title_id, d.term
              FROM dna_tagged d JOIN owned o ON o.id = d.title_id
             WHERE d.version = {version}
        ),
        spec AS (
            SELECT term, ln((SELECT greatest(count(*), 1) FROM owned)::float8 / count(*)) AS idf
              FROM tagged GROUP BY term
        )"""


@dataclass(frozen=True)
class Neighbour:
    """One unseen owned title near the anchor, and which of the anchor's terms it carries."""

    title_id: int
    likeness: float
    shared: frozenset[str]


async def anchor_neighbours(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    kind: str,
    version: str,
    bundle_version: str | None,
    anchor_id: int,
    exclude: Sequence[int] = (),
    limit: int,
) -> tuple[list[Neighbour], dict[str, float]]:
    """The `limit` unseen owned titles of this kind most like the anchor, most alike first, and
    the specificity of every anchor term they share.

    LIKENESS IS SPECIFICITY-WEIGHTED (decision 513). Each term counts by how rare it is in the
    household's owned titles of the kind - ln(N / carriers), the library's own inverse document
    frequency - and two titles are as alike as the cosine of those weighted term sets. Counting
    shared terms, as decision 475 did, let the generic ones decide: "mentor & protege", on 295 of
    753 owned films, joined The Grand Budapest Hotel to GoodFellas and Dune: Part Two, and
    "melancholic + romantic" joined Pride & Prejudice to The Last Samurai and Captain America.
    Weighted, Budapest's nearest owned films are The Phoenician Scheme and Amsterdam. The cosine's
    norm is the candidate's whole term set, so a title with a long DNA row is not alike merely
    for carrying more terms. No weight column is read anywhere here (§4.1 rule 2), and both tiers
    count alike, as presence.

    THE ANCHOR'S FORM (decision 513). An animated anchor draws animated titles and a live-action
    anchor live-action ones - decision 473's canonical Animation, read across every structured
    source - because "Because you liked Chernobyl" drew Attack on Titan and Berserk on shared
    moods, and a cartoon is a different evening from a drama whatever mood they share.

    Ties keep the order the person's own scores put them in, then the id.
    """
    animation = genre_vocab.raw_labels("Animation")
    rows = await conn.fetch(
        f"""
        WITH {specificity_ctes("$2", "$3")},
        norm AS (
            SELECT g.title_id, sqrt(sum(s.idf * s.idf)) AS n
              FROM tagged g JOIN spec s USING (term) GROUP BY g.title_id
        ),
        anchor AS (
            SELECT DISTINCT d.term FROM dna_tagged d WHERE d.title_id = $4 AND d.version = $3
        ),
        anchor_norm AS (
            SELECT sqrt(sum(s.idf * s.idf)) AS n FROM anchor a JOIN spec s USING (term)
        ),
        animated AS (
            SELECT DISTINCT g.title_id FROM title_genre g
             WHERE (g.title_id IN (SELECT id FROM owned) OR g.title_id = $4)
               AND g.source <> ALL($7::text[]) AND lower(g.genre) = ANY($6::text[])
        )
        SELECT g.title_id,
               sum(s.idf * s.idf) / nullif(nm.n * (SELECT n FROM anchor_norm), 0) AS likeness,
               array_agg(g.term ORDER BY g.term) AS shared,
               array_agg(s.idf ORDER BY g.term) AS shared_idf
          FROM tagged g
          JOIN anchor a USING (term)
          JOIN spec s USING (term)
          JOIN norm nm ON nm.title_id = g.title_id
          LEFT JOIN user_title ut ON ut.title_id = g.title_id AND ut.user_id = $1
          LEFT JOIN user_score us ON us.title_id = g.title_id AND us.user_id = $1
                                 AND us.bundle_version = $9
         WHERE g.title_id <> $4 AND NOT (g.title_id = ANY($5::int[]))
           AND COALESCE(ut.state, 'unseen') = 'unseen'
           AND (g.title_id IN (SELECT title_id FROM animated))
             = ($4 IN (SELECT title_id FROM animated))
         GROUP BY g.title_id, nm.n
        HAVING count(*) >= 2
         ORDER BY likeness DESC NULLS LAST, max(us.score) DESC NULLS LAST, g.title_id
         LIMIT $8
        """,
        user_id,
        kind,
        version,
        anchor_id,
        list(exclude),
        animation,
        list(genre_vocab.EXCLUDED_SOURCES),
        limit,
        bundle_version,
    )
    specificity: dict[str, float] = {}
    neighbours = []
    for r in rows:
        specificity.update(zip(r["shared"], (float(w) for w in r["shared_idf"]), strict=True))
        neighbours.append(
            Neighbour(int(r["title_id"]), float(r["likeness"] or 0.0), frozenset(r["shared"]))
        )
    return neighbours, specificity


def likest_pair(
    neighbours: Sequence[Neighbour],
    specificity: dict[str, float],
    terms: Sequence[WhyTerm],
    *,
    cap: int,
    floor: int,
) -> tuple[list[int], WhyTerm, WhyTerm] | None:
    """The pair of anchor terms shelf 1 names, and its cards in likeness order - or None when
    no pair is carried by `floor` of the neighbours.

    Each pair's shelf is the first `cap` neighbours carrying both its terms. The pair that wins
    is the one whose shelf holds the most likeness, weighed by how specific the two terms are
    (decision 513): the anchor's nearest titles all carry some generic pair ("tense + gripping"
    joined Heat to all twelve of its neighbours), and naming it says nothing about why they are
    here, while "gritty + cops & detectives" does and is carried by nearly as many. Ties go to
    the terms in id order, so one library always gives one shelf. Proposal 24 survives: every
    card carries both named terms by construction.
    """
    by_term = {t.term: t for t in terms}
    carried = sorted({t for n in neighbours for t in n.shared if t in by_term})
    best: tuple[tuple[float, str, str], list[int]] | None = None
    for first, second in combinations(carried, 2):
        cards = [n for n in neighbours if first in n.shared and second in n.shared][:cap]
        if len(cards) < floor:
            continue
        rarity = (specificity.get(first, 0.0) + specificity.get(second, 0.0)) / 2.0
        key = (-sum(n.likeness for n in cards) * rarity, first, second)
        if best is None or key < best[0]:
            best = (key, [n.title_id for n in cards])
    if best is None:
        return None
    (_, first, second), cards = best
    return cards, by_term[first], by_term[second]
