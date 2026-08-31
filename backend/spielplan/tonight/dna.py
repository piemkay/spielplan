"""The DNA the round reasons over: one vector per candidate, and §6.4's authored axes.

Spec v2.1 §4.1 rules 1 and 2, §6.2 steps 4-5, §6.4, §10, §14 risk 7.

§4.1 RULE 1, STRUCTURALLY. Both tiers are read through the sanctioned `dna_tagged` view and
nowhere else, so the `tier` discriminator cannot be dropped on the way — the same discipline
`home/why.py` follows. A term present in both tiers is counted once, as extracted, because a
payload must never upgrade a projected term into a quote-verified one.

§4.1 RULE 2, ARITHMETICALLY. `salience` and `confidence` appear in the SELECT list and in the
weight expression and in **no predicate anywhere below**. They decide how loudly a term speaks;
they never decide which title or which term is admitted. That is what makes a 0.5 cut — which
would delete 44% of the extracted tier — unrepresentable here rather than merely discouraged.

§10 AND §14 RISK 7. Every read is scoped to one vocabulary version. `dna_tag` is
`UNIQUE (title_id, version, term, provider)` and the importer only upserts, so two vocabularies
coexist after a re-import; a tilt or a conflict explanation computed over a superseded
vocabulary is the app reasoning in a basis that is not the active row's.
"""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg

# The same ranking `home/why.py` uses, and for the same reason: §4.1 calls the extracted tier
# quote-verified and the projected tier inferred, so extracted speaks louder — while both stay
# fully admissible. Ranges: extracted 0.73..1.00, projected 0.00..0.30.
TERM_WEIGHT = """
        CASE d.tier
            WHEN 'extracted' THEN 0.60 + 0.40 * (COALESCE(d.salience, 1.0) / 3.0)
            ELSE 0.30 * COALESCE(d.confidence, 0.5)
        END
"""


async def vectors_for(
    conn: asyncpg.Connection, title_ids: Sequence[int], *, version: str
) -> dict[int, dict[str, float]]:
    """One sparse `term -> weight` vector per title.

    `max()` over the two tiers rather than a sum: a title that carries a term in both tiers
    holds it once, at the louder (extracted) weight. Summing would make the 14,181 pairs that
    exist in both tiers count double, which is the merge §4.1 rule 1 forbids arriving through
    arithmetic instead of through a UNION.
    """
    ids = sorted({int(t) for t in title_ids})
    if not ids or not version:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT d.title_id, d.term, max({TERM_WEIGHT}) AS weight
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = ANY($2)
         GROUP BY d.title_id, d.term
        """,
        version, ids,
    )
    out: dict[int, dict[str, float]] = {t: {} for t in ids}
    for row in rows:
        out[row["title_id"]][row["term"]] = float(row["weight"])
    return out


async def axes_for(conn: asyncpg.Connection, *, version: str) -> dict[str, dict[str, float]]:
    """§6.4's authored axis artifact: one bipolar axis per facet, `term -> weight ∈ [−1, 1]`.

    "Axis definitions are a shipped, authored artifact … Deterministic — no nightly rebuild, no
    Procrustes anchoring, no map shift on bundle re-import." So this is a lookup of a shipped
    file, and the contested-facet rule that reads it inherits that determinism.
    """
    if not version:
        return {}
    rows = await conn.fetch(
        "SELECT facet, term, weight FROM dna_axis_weight WHERE version = $1", version
    )
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(row["facet"], {})[row["term"]] = float(row["weight"])
    return out


async def poles_for(conn: asyncpg.Connection, *, version: str) -> dict[str, tuple[str, str]]:
    """Each axis's named poles, for the split copy ("You're split on {facet}") and for any
    surface that wants to say which side a title is on."""
    rows = await conn.fetch(
        "SELECT facet, left_pole, right_pole FROM dna_axis WHERE version = $1", version
    )
    return {r["facet"]: (r["left_pole"], r["right_pole"]) for r in rows}


async def active_version(conn: asyncpg.Connection) -> str | None:
    """The vocabulary every read here is scoped to. Same resolution `home/why.py` uses, so two
    surfaces cannot disagree about which vocabulary is live."""
    return await conn.fetchval(
        "SELECT version FROM dna_vocabulary ORDER BY imported_at DESC, version DESC LIMIT 1"
    )


async def terms_carried_by(
    conn: asyncpg.Connection, title_id: int, *, version: str, limit: int = 6
) -> list[dict[str, object]]:
    """The terms one title actually carries, loudest first, each with its tier.

    Feeds §6.2 step 7's per-person match lines, which may only name terms the winner carries —
    the invariant `home/why.py` was inverted to make structural, on the one screen the whole
    round exists to produce. `salience`/`confidence` order the list and admit nothing.
    """
    rows = await conn.fetch(
        f"""
        SELECT d.term, min(d.facet) AS facet,
               CASE WHEN bool_or(d.tier = 'extracted') THEN 'extracted' ELSE 'projected' END AS tier,
               max({TERM_WEIGHT}) AS weight
          FROM dna_tagged d
         WHERE d.version = $1 AND d.title_id = $2
         GROUP BY d.term
         ORDER BY max({TERM_WEIGHT}) DESC, d.term
         LIMIT $3
        """,
        version, title_id, limit,
    )
    return [
        {"term": r["term"], "facet": r["facet"], "tier": r["tier"], "weight": float(r["weight"])}
        for r in rows
    ]


__all__ = [
    "TERM_WEIGHT",
    "active_version",
    "axes_for",
    "poles_for",
    "terms_carried_by",
    "vectors_for",
]
