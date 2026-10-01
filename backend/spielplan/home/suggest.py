"""The title card's one line on why a title is suggested (decision 515), or None.

None if seen, rated or avoided; else "Because you liked {X} — {a}, {b}"; else "One of
the ones we think you'll enjoy most" when their own fit ranks it in the top `TOP_SHARE`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg

from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.home import taste
from spielplan.home import why as why_mod
from spielplan.ledger.observations import LIVE_LABEL_SQL
from spielplan.scoring import serve

# About the edge of the neighbourhood shelf 1 names its pair from.
LIKENESS_FLOOR = 0.2

TOP_SHARE = 0.1


async def why_suggested(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, bundle_version: str | None
) -> str | None:
    """One member-register sentence, or None. See the module docstring for the order."""
    row = await conn.fetchrow(
        f"""
        SELECT t.kind,
               COALESCE(ut.state, 'unseen') = 'seen' AS seen,
               EXISTS (SELECT 1 FROM ({LIVE_LABEL_SQL}) lv WHERE lv.title_id = t.id) AS rated
          FROM title t
          LEFT JOIN user_title ut ON ut.title_id = t.id AND ut.user_id = $1
         WHERE t.id = $2
        """,
        user_id,
        title_id,
    )
    if row is None or row["seen"] or row["rated"]:
        return None
    kind = row["kind"]
    version = await dna_terms.active_version(conn)

    avoided = await taste.avoided_for(conn, user_id=user_id, version=version)
    if avoided and await taste.avoided_titles(
        conn, [avoided], kind=kind, version=version, title_ids=[title_id]
    ):
        return None

    if version is not None:
        liked = (
            await likest_liked(conn, user_id=user_id, title_ids=[title_id], kind=kind, version=version)
        ).get(title_id)
        if liked is not None:
            return f"Because you liked {liked.name} — {', '.join(liked.terms)}"

    if bundle_version is not None and await _in_top_share(
        conn, user_id=user_id, title_id=title_id, kind=kind, bundle_version=bundle_version
    ):
        return "One of the ones we think you'll enjoy most"
    return None


@dataclass(frozen=True)
class Liked:
    """The person's liked title a target is most like, and the terms that best say why."""

    title_id: int
    name: str
    terms: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"title_id": self.title_id, "name": self.name, "terms": list(self.terms)}


async def likest_liked(
    conn: asyncpg.Connection, *, user_id: int, title_ids: Sequence[int], kind: str, version: str
) -> dict[int, Liked]:
    """Per target, the member's liked title of this kind and form most like it, where it clears the
    floor, with the two terms that best say why (specificity x the lesser naming rank; naming only).
    One read for every target; a target with no such title is absent."""
    if not title_ids:
        return {}
    rows = await conn.fetch(
        f"""
        WITH {why_mod.specificity_ctes("$3", "$4")},
        liked AS (
            SELECT lv.title_id
              FROM ({LIVE_LABEL_SQL}) lv
              JOIN title t ON t.id = lv.title_id AND t.kind = $3
             WHERE lv.value = 2
        ),
        animated AS (
            SELECT DISTINCT g.title_id FROM title_genre g
             WHERE (g.title_id = ANY($2::int[]) OR g.title_id IN (SELECT title_id FROM liked))
               AND g.source <> ALL($6::text[]) AND lower(g.genre) = ANY($5::text[])
        ),
        target AS (
            SELECT d.title_id, d.term, max({why_mod.TERM_RANK}) AS r
              FROM dna_tagged d JOIN spec s USING (term)
             WHERE d.title_id = ANY($2::int[]) AND d.version = $4
             GROUP BY d.title_id, d.term
        ),
        target_norm AS (
            SELECT g.title_id, sqrt(sum(s.idf * s.idf)) AS n
              FROM target g JOIN spec s USING (term) GROUP BY g.title_id
        ),
        liked_terms AS (
            SELECT d.title_id, d.term, max({why_mod.TERM_RANK}) AS r
              FROM dna_tagged d JOIN liked l ON l.title_id = d.title_id JOIN spec s USING (term)
             WHERE d.version = $4
             GROUP BY d.title_id, d.term
        ),
        liked_norm AS (
            SELECT lt.title_id, sqrt(sum(s.idf * s.idf)) AS n
              FROM liked_terms lt JOIN spec s USING (term) GROUP BY lt.title_id
        ),
        pairs AS (
            SELECT g.title_id AS target_id, lt.title_id AS liked_id,
                   sum(s.idf * s.idf) / nullif(ln.n * tn.n, 0) AS likeness,
                   array_agg(lt.term ORDER BY s.idf * least(lt.r, g.r) DESC, lt.term) AS shared,
                   array_agg(dl.label ORDER BY s.idf * least(lt.r, g.r) DESC, lt.term) AS labels
              FROM target g
              JOIN liked_terms lt USING (term)
              JOIN spec s USING (term)
              JOIN liked_norm ln ON ln.title_id = lt.title_id
              JOIN target_norm tn ON tn.title_id = g.title_id
              LEFT JOIN dna_term dl ON dl.version = $4 AND dl.term = lt.term
             WHERE (g.title_id IN (SELECT title_id FROM animated))
                 = (lt.title_id IN (SELECT title_id FROM animated))
             GROUP BY g.title_id, lt.title_id, ln.n, tn.n
            HAVING count(*) >= 2
        )
        SELECT DISTINCT ON (p.target_id) p.target_id, p.liked_id, t.name, p.likeness,
               p.shared, p.labels
          FROM pairs p JOIN title t ON t.id = p.liked_id
         ORDER BY p.target_id, p.likeness DESC NULLS LAST, p.liked_id
        """,
        user_id,
        [int(t) for t in title_ids],
        kind,
        version,
        genre_vocab.raw_labels("Animation"),
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    found: dict[int, Liked] = {}
    for row in rows:
        if row["likeness"] is None or float(row["likeness"]) < LIKENESS_FLOOR:
            continue
        pairs = list(zip(row["shared"], row["labels"], strict=True))[: why_mod.NAMED_TERM_CAP]
        found[int(row["target_id"])] = Liked(
            title_id=int(row["liked_id"]),
            name=row["name"],
            terms=tuple(dna_terms.label_of(term, label) for term, label in pairs),
        )
    return found


async def _in_top_share(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, kind: str, bundle_version: str
) -> bool:
    """Whether the member's own fitted ranking (β > 0) puts this title in the top `TOP_SHARE`."""
    if kind not in await serve.personal_kinds(
        conn, user_id=user_id, kinds=[kind], bundle_version=bundle_version
    ):
        return False
    # None, never 0, when the title has no score in the active basis.
    share = await conn.fetchval(
        """
        WITH owned AS (
            SELECT us.score FROM user_score us JOIN title t ON t.id = us.title_id AND t.is_owned
             WHERE us.user_id = $1 AND us.kind = $4 AND us.bundle_version = $3
        )
        SELECT (SELECT count(*) FROM owned o WHERE o.score > m.score)::float8
               / nullif((SELECT count(*) FROM owned), 0)
          FROM user_score m
         WHERE m.user_id = $1 AND m.title_id = $2 AND m.bundle_version = $3
        """,
        user_id,
        title_id,
        bundle_version,
        kind,
    )
    return share is not None and float(share) < TOP_SHARE


__all__ = ["LIKENESS_FLOOR", "TOP_SHARE", "Liked", "likest_liked", "why_suggested"]
