"""Read-layer DNA constants shared by Home, Tonight and the catalogue: term weight, active vocabulary,
labels. §4.1 rule 2: salience, confidence and `n_sources` appear in arithmetic only, never in a comparison.
"""

from __future__ import annotations

from collections.abc import Iterable

import asyncpg

# Callers bind the `dna_tagged` view to `d`. A projected `confidence` is `n_sources` (1..8), saturated
# and floored at 0 (decision 188), so projected (0.15..0.27) never outweighs extracted (0.73..1.00).
TERM_WEIGHT = """
        CASE d.tier
            WHEN 'extracted' THEN 0.60 + 0.40 * (COALESCE(d.salience, 1.0) / 3.0)
            ELSE 0.30 * (GREATEST(COALESCE(d.confidence, 0.5), 0.0)
                         / (1.0 + GREATEST(COALESCE(d.confidence, 0.5), 0.0)))
        END
"""

# Both columns: version strings do not sort in import order, and `imported_at` (now()) ties
# within one transaction.
ACTIVE_VERSION = """
        (SELECT v.version FROM dna_vocabulary v ORDER BY v.imported_at DESC, v.version DESC
          LIMIT 1)
"""


# Both tiers, each by name (§4.1 rules 1-2): a veto and a leave-out want recall over precision
# (decision 504).
BOTH_TIERS: tuple[str, ...] = ("extracted", "projected")


def unvetoed(terms: str, version: str, tiers: str) -> str:
    """No vetoed term on `t` in either tier (decision 504); with no vocabulary version, nothing is."""
    return f"""(
        cardinality({terms}::text[]) = 0 OR {version}::text IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM dna_tagged d
             WHERE d.title_id = t.id AND d.version = {version} AND d.tier = ANY({tiers}::text[])
               AND d.term = ANY({terms}::text[])
        )
    )"""


async def active_version(conn: asyncpg.Connection) -> str | None:
    """None before any bundle import; not an error."""
    return await conn.fetchval(f"SELECT {ACTIVE_VERSION}")


def label_of(term: str, label: str | None) -> str:
    if label and label.strip():
        return label.strip()
    leaf = term.split(".", 1)[1] if "." in term else term
    return leaf.replace("_", " ")


async def labels_for(
    conn: asyncpg.Connection, terms: Iterable[str]
) -> dict[str, dict[str, str | None]]:
    """Every term asked about gets an entry (unknown ones via `label_of`), so no id is ever printed."""
    wanted = list(dict.fromkeys(t for t in terms if t))
    if not wanted:
        return {}
    rows = await conn.fetch(
        f"SELECT term, label, gloss FROM dna_term WHERE version = {ACTIVE_VERSION} "
        "AND term = ANY($1::text[])",
        wanted,
    )
    found = {r["term"]: r for r in rows}
    return {
        t: {
            "label": label_of(t, found[t]["label"] if t in found else None),
            "gloss": found[t]["gloss"] if t in found else None,
        }
        for t in wanted
    }


__all__ = [
    "ACTIVE_VERSION", "BOTH_TIERS", "TERM_WEIGHT", "active_version", "label_of", "labels_for",
    "unvetoed",
]
