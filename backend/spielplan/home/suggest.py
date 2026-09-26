"""The title card's one line on why a title is suggested (decision 515), or None.

None if seen, rated or avoided; else "Because you liked {X} — they share {a} + {b}"; else "One of
the ones we think you'll enjoy most" when their own fit ranks it in the top `TOP_SHARE`.
"""

from __future__ import annotations

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
        liked = await _likest_liked(
            conn, user_id=user_id, title_id=title_id, kind=kind, version=version
        )
        if liked is not None:
            name, terms = liked
            return f"Because you liked {name} — they share {' + '.join(terms)}"

    if bundle_version is not None and await _in_top_share(
        conn, user_id=user_id, title_id=title_id, kind=kind, bundle_version=bundle_version
    ):
        return "One of the ones we think you'll enjoy most"
    return None


async def _likest_liked(
    conn: asyncpg.Connection, *, user_id: int, title_id: int, kind: str, version: str
) -> tuple[str, list[str]] | None:
    """The member's liked title of this kind and form most like this one, if it clears the floor,
    with the two terms that best say why (specificity x the lesser naming rank; naming only)."""
    rows = await conn.fetch(
        f"""
        WITH {why_mod.specificity_ctes("$3", "$4")},
        target AS (
            SELECT d.term, max({why_mod.TERM_RANK}) AS r
              FROM dna_tagged d JOIN spec s USING (term)
             WHERE d.title_id = $2 AND d.version = $4
             GROUP BY d.term
        ),
        target_norm AS (
            SELECT sqrt(sum(s.idf * s.idf)) AS n FROM target g JOIN spec s USING (term)
        ),
        liked AS (
            SELECT lv.title_id
              FROM ({LIVE_LABEL_SQL}) lv
              JOIN title t ON t.id = lv.title_id AND t.kind = $3
             WHERE lv.value = 2
               AND EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = lv.title_id
                            AND g.source <> ALL($6::text[]) AND lower(g.genre) = ANY($5::text[]))
                 = EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = $2
                            AND g.source <> ALL($6::text[]) AND lower(g.genre) = ANY($5::text[]))
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
        )
        SELECT lt.title_id, t.name,
               sum(s.idf * s.idf) / nullif(ln.n * (SELECT n FROM target_norm), 0) AS likeness,
               array_agg(lt.term ORDER BY s.idf * least(lt.r, g.r) DESC, lt.term) AS shared,
               array_agg(dl.label ORDER BY s.idf * least(lt.r, g.r) DESC, lt.term) AS labels
          FROM liked_terms lt
          JOIN target g USING (term)
          JOIN spec s USING (term)
          JOIN liked_norm ln ON ln.title_id = lt.title_id
          JOIN title t ON t.id = lt.title_id
          LEFT JOIN dna_term dl ON dl.version = $4 AND dl.term = lt.term
         GROUP BY lt.title_id, t.name, ln.n
        HAVING count(*) >= 2
         ORDER BY likeness DESC NULLS LAST, lt.title_id
         LIMIT 1
        """,
        user_id,
        title_id,
        kind,
        version,
        genre_vocab.raw_labels("Animation"),
        list(genre_vocab.EXCLUDED_SOURCES),
    )
    if not rows or rows[0]["likeness"] is None or float(rows[0]["likeness"]) < LIKENESS_FLOOR:
        return None
    best = rows[0]
    names = [
        dna_terms.label_of(term, label)
        for term, label in list(zip(best["shared"], best["labels"], strict=True))[
            : why_mod.NAMED_TERM_CAP
        ]
    ]
    return best["name"], names


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


__all__ = ["LIKENESS_FLOOR", "TOP_SHARE", "why_suggested"]
