"""The why-line machinery: DNA terms chosen FIRST, membership derived from them (proposal 24).

Both tiers are read only through `dna_tagged`; a term in both is named once, as extracted. Weights
order which term is NAMED and never filter which title is admitted (§4.1 rule 2).
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

# `terms_for`'s default: a title's eight best-named terms.
ANCHOR_TERM_POOL = 8

# Copy, not a tuned number: a one-line why names at most two terms.
NAMED_TERM_CAP = 2

# Decision 514: "which you like" needs this many liked titles of the kind behind it.
LIKED_TERM_MIN = 3

# Decision 513: shelf 1 names its pair from the anchor's nearest titles, this many per card slot.
NEIGHBOURHOOD_PER_CARD = 2

# Naming rank only: the extracted tier's band sits wholly above the projected tier's (§4.1 rule 2).
TERM_RANK = dna_terms.TERM_WEIGHT

# The label rides beside the term in every read that builds a `WhyTerm`, joined on the term's own
# version so a superseded vocabulary cannot name it. `max` because those reads group by term.
LABEL_JOIN = "LEFT JOIN dna_term dl ON dl.version = d.version AND dl.term = d.term"

ROLES = ("member", "anchor_side")


@dataclass(frozen=True)
class WhyTerm:
    """One DNA term a why-line names.

    A `member` term is on every card; an `anchor_side` term describes the user's liked region
    and is deliberately NOT on the cards.
    """

    term: str
    facet: str
    tier: str
    role: str = "member"
    # `dna_term.label`, so a why-line says "World War II", not `era.wwii` (decision 486).
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
    """The vocabulary the shelves name terms from, or None. Versions must never mix."""
    return await dna_terms.active_version(conn)


async def terms_for(
    conn: asyncpg.Connection, title_id: int, *, version: str, limit: int | None = ANCHOR_TERM_POOL
) -> list[WhyTerm]:
    """One title's terms, best-named first (an ORDER BY, never a filter). `limit=None`: all."""
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
    """Every owned title of this kind carrying ALL of `terms`: the shelf's membership (proposal 24)."""
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
    """§6.4's explore frontier: (candidate, neighbour, cosine, affinity), or None.

    Unvisited is zero seen carriers; near is co-occurrence cosine in the owned catalog; liked is
    the person's own verdicts (decision 514); the neighbour is from another facet. `exclude`d titles
    do not count as carriers.
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
        # Ties: the larger candidate pool first, then the terms ascending.
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
    """`{term} · {term}` — §6.0's own why-line shape for shelf 1, in the vocabulary's words."""
    line = " · ".join(t.name for t in terms)
    return line[:1].upper() + line[1:]


# --- shelf 1's membership: likeness to the anchor (decisions 475 and 513) ---------------------


def specificity_ctes(kind: str, version: str) -> str:
    """CTEs `owned`, `tagged` and `spec` (term -> ln(N / carriers) over the owned titles of a kind).

    Shared by shelf 1 and the title card (decision 515). `kind`/`version` are SQL placeholders.
    """
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
    """The `limit` unseen owned titles most like the anchor, and the specificity of shared terms.

    Likeness is the cosine of idf-weighted term sets (decision 513); animated anchors draw animated
    titles only, and vice versa. Ties go to the person's own scores, then the id.
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
    """The pair of anchor terms shelf 1 names, and its cards, or None below `floor`.

    The winner maximises summed likeness times the pair's mean specificity (decision 513).
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


# --- likeness between any two titles: Shares a lot with and Rate's shelves (decisions 541, 551) ---


@dataclass(frozen=True)
class Like:
    """How much one candidate shares with a target, and the strongest term the two share."""

    title_id: int
    likeness: float
    shared: int
    term: str | None
    facet: str | None
    label: str | None


async def likeness(
    conn: asyncpg.Connection,
    *,
    title_id: int,
    candidate_ids: Sequence[int],
    kind: str,
    version: str,
) -> dict[int, Like]:
    """Every candidate sharing at least one term with `title_id`, keyed by candidate id.

    Shelf 1's cosine of idf-weighted term sets over both tiers (decision 513), idf over the owned
    titles of `kind`; a term no owned title carries weighs as one carried by a single title. Neither
    the target nor a candidate need be owned, unseen or of the target's form. The strongest term is
    named as `suggest._likest_liked` names them: idf times the lesser naming rank.
    """
    rows = await conn.fetch(
        f"""
        WITH {specificity_ctes("$3", "$4")},
        terms AS (
            SELECT d.title_id, d.term, min(d.facet) AS facet, max({TERM_RANK}) AS r
              FROM dna_tagged d
             WHERE d.version = $4 AND (d.title_id = $1 OR d.title_id = ANY($2::int[]))
             GROUP BY d.title_id, d.term
        ),
        weighted AS (
            SELECT g.*, COALESCE(s.idf, ln((SELECT greatest(count(*), 1) FROM owned)::float8)) AS idf
              FROM terms g LEFT JOIN spec s USING (term)
        ),
        norm AS (
            SELECT title_id, sqrt(sum(idf * idf)) AS n FROM weighted GROUP BY title_id
        )
        SELECT c.title_id,
               sum(c.idf * c.idf) / nullif(nc.n * nt.n, 0) AS likeness,
               count(*) AS shared,
               (array_agg(c.term ORDER BY c.idf * least(c.r, t.r) DESC, c.term))[1] AS term,
               (array_agg(c.facet ORDER BY c.idf * least(c.r, t.r) DESC, c.term))[1] AS facet,
               (array_agg(dl.label ORDER BY c.idf * least(c.r, t.r) DESC, c.term))[1] AS label
          FROM weighted c
          JOIN weighted t ON t.term = c.term AND t.title_id = $1
          JOIN norm nc ON nc.title_id = c.title_id
          JOIN norm nt ON nt.title_id = $1
          LEFT JOIN dna_term dl ON dl.version = $4 AND dl.term = c.term
         WHERE c.title_id <> $1
         GROUP BY c.title_id, nc.n, nt.n
        """,
        title_id,
        list(candidate_ids),
        kind,
        version,
    )
    return {
        int(r["title_id"]): Like(
            title_id=int(r["title_id"]),
            likeness=float(r["likeness"] or 0.0),
            shared=int(r["shared"]),
            term=r["term"],
            facet=r["facet"],
            label=dna_terms.label_of(r["term"], r["label"]),
        )
        for r in rows
    }


async def term_vectors(
    conn: asyncpg.Connection, title_ids: Sequence[int], *, kind: str, version: str
) -> dict[int, dict[str, tuple[float, float, str]]]:
    """Each title's terms as `term -> (idf, naming rank, label)`, weighed as `likeness` weighs them,
    so a caller can take the same cosine for many pairs from one read."""
    rows = await conn.fetch(
        f"""
        WITH {specificity_ctes("$2", "$3")}
        SELECT d.title_id, d.term, max({TERM_RANK}) AS r, max(dl.label) AS label,
               COALESCE(max(s.idf), ln((SELECT greatest(count(*), 1) FROM owned)::float8)) AS idf
          FROM dna_tagged d
          {LABEL_JOIN}
          LEFT JOIN spec s ON s.term = d.term
         WHERE d.version = $3 AND d.title_id = ANY($1::int[])
         GROUP BY d.title_id, d.term
        """,
        [int(t) for t in title_ids],
        kind,
        version,
    )
    out: dict[int, dict[str, tuple[float, float, str]]] = {}
    for r in rows:
        out.setdefault(int(r["title_id"]), {})[r["term"]] = (
            float(r["idf"]), float(r["r"]), dna_terms.label_of(r["term"], r["label"])
        )
    return out


_SHARE_FIELDS = (
    "kind", "name", "original_name", "original_language", "year", "runtime_min", "poster_path",
)


async def shares_with(
    conn: asyncpg.Connection,
    *,
    user_id: int,
    title_id: int,
    kind: str,
    version: str,
    cap: int = 8,
    floor: int = 3,
) -> list[dict[str, Any]]:
    """§6.4's Shares a lot with: the owned titles of `kind` most like `title_id`, read as shelf 1
    reads them (the anchor's form, at least two shared terms), seen ones included; [] below `floor`."""
    owned = await conn.fetch(
        """
        WITH animated AS (
            SELECT DISTINCT g.title_id FROM title_genre g
             WHERE (g.title_id IN (SELECT id FROM title WHERE kind = $2 AND is_owned) OR g.title_id = $1)
               AND g.source <> ALL($4::text[]) AND lower(g.genre) = ANY($3::text[])
        )
        SELECT t.id FROM title t
         WHERE t.kind = $2 AND t.is_owned AND t.id <> $1
           AND (t.id IN (SELECT title_id FROM animated)) = ($1 IN (SELECT title_id FROM animated))
        """,
        title_id,
        kind,
        genre_vocab.raw_labels("Animation"),
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    likes = await likeness(
        conn, title_id=title_id, candidate_ids=[int(r["id"]) for r in owned], kind=kind,
        version=version,
    )
    closest = sorted(
        (like for like in likes.values() if like.shared >= 2),
        key=lambda like: (-like.likeness, like.title_id),
    )[:cap]
    if len(closest) < floor:
        return []
    rows = await conn.fetch(
        """
        SELECT t.id, t.kind, t.name, t.original_name, t.original_language, t.year, t.runtime_min,
               t.poster_path, COALESCE(ut.state, 'unseen') = 'seen' AS seen
          FROM title t
          LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
         WHERE t.id = ANY($2::int[])
        """,
        user_id,
        [like.title_id for like in closest],
    )
    by_id = {int(r["id"]): r for r in rows}
    return [
        {
            "title_id": like.title_id,
            **{k: by_id[like.title_id][k] for k in _SHARE_FIELDS},
            "seen": bool(by_id[like.title_id]["seen"]),
            "term": {"term": like.term, "facet": like.facet, "label": like.label},
        }
        for like in closest
    ]
