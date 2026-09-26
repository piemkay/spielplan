"""The title card's one line on why a title is suggested to the person reading it.

Spec v2.1 §6.0 (the title card; the shelf table), §6.8 ("quiet reasons"); decisions 486, 512,
513 and 515.

Both members opened title after title from a shelf and found no sentence on the card saying why it
was there (second household test, H6). §6.8 asks every recommendation for a one-line why, and the
card is where a recommendation is read closely. So `why` is one sentence in the member register,
or None - never a sentence that is not true of this title for this person:

1. None for a title they have seen or rated: nothing is being suggested.
2. None for a title they avoid (decision 512): the shelves leave it out, so the card does not
   argue for it.
3. "Because you liked {X} — they share {a} + {b}" when one of their own liked titles of the kind,
   in the same form (animated or live-action), is alike enough to have put this title on X's
   shelf: the likeness shelf 1 ranks by (decision 513), and the two most specific terms both
   carry.
4. "One of the ones we think you'll enjoy most" when their own ratings rank it in the top
   `TOP_SHARE` of the household's owned titles of the kind - the Your top picks sentence, and
   only where their personal half carries weight, because the crowd's order is not "for you".
5. None otherwise.
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms
from spielplan.db import genres as genre_vocab
from spielplan.home import taste
from spielplan.home import why as why_mod
from spielplan.ledger.observations import LIVE_LABEL_SQL
from spielplan.scoring import serve

# The likeness a liked title must reach before the card names it: about the edge of the
# neighbourhood shelf 1 names its pair from. Measured on the second household test's library, the
# twenty-fourth nearest owned film to each member's liked films sat at a median of 0.22 and 0.25;
# the 127 owned series are sparser, and there 0.2 admits only the nearest few (the nearest series
# to a liked one sat at a median of 0.31 and 0.24).
LIKENESS_FLOOR = 0.2

# "One of the ones we think you'll enjoy most": the top tenth of the owned titles of the kind.
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
    with the two terms that best say why - named by the vocabulary's labels.

    Which two is a naming question, so §4.1 rule 2's naming rank may answer it: a shared term is
    named by its specificity times the lesser of its two naming ranks, so it is both rare in the
    library and prominent on both titles. By specificity alone Collateral was like Heat for
    "alienation + verbal sparring" - an inferred tag on Heat - rather than for its heist and its
    Los Angeles. The likeness itself reads no weight."""
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
    """Whether the member's own fitted ranking puts this title in the top `TOP_SHARE` of the
    household's owned titles of the kind. A profile whose personal half carries no weight is the
    crowd's order and has no "you" (`serve.personal_kinds`)."""
    if kind not in await serve.personal_kinds(
        conn, user_id=user_id, kinds=[kind], bundle_version=bundle_version
    ):
        return False
    # No row at all when this title has no score in the active basis, so "unknown" is None and
    # never a share of zero.
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
