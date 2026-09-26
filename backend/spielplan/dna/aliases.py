"""The alias map: a raw keyword spelling -> the vocabulary term it names (§8 stage 8).

`alias_key` is not `norm()` and the two must never merge. Lexicon rows never project; rows naming a
term the vocabulary lacks are skipped (the table has no FK to `dna_term`).
"""

from __future__ import annotations

import re

import asyncpg

from spielplan.db.dna_terms import active_version

_PUNCT = re.compile(r"^[\s\"'`\-–—.,;:()\[\]]+|[\s\"'`\-–—.,;:()\[\]]+$")
_WS = re.compile(r"\s+")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+")

# The app's facet id (term prefix), not the corpus's `register_audience`.
_EXPLICIT_MAP_ONLY_FACET = "register"


def alias_key(phrase: str) -> str:
    """The alias map's lookup key: case, whitespace, punctuation, hyphen-vs-space, leading article.

    Deliberately light: plurals stay distinct. Not `norm()`.
    """
    p = (phrase or "").lower().replace("’", "'")
    p = p.replace("-", " ").replace("/", " ")
    p = _WS.sub(" ", p)
    p = _PUNCT.sub("", p)
    p = _ARTICLE.sub("", p)
    return p.strip()


async def load_alias_map(
    conn: asyncpg.Connection, version: str | None = None
) -> dict[str, tuple[str, str]]:
    """Normalised raw term -> (facet, vocabulary term id), for one vocabulary version.

    Callers must pass their keywords through `alias_key` too: `raw_term` is stored verbatim.
    """
    if version is None:
        version = await active_version(conn)
    if version is None:
        return {}

    # Joined to `dna_term` because `dna_alias` has no FK to it; the facet comes from `dna_term`.
    # Ordered `COLLATE "C"` because two spellings can fold to one key and the last write wins, so the
    # winner must not depend on the cluster's collation.
    rows = await conn.fetch(
        """
        SELECT a.alias, a.kind, t.term, t.facet
          FROM dna_alias a
          JOIN dna_term t ON t.version = a.version AND t.term = a.term
         WHERE a.version = $1
         ORDER BY a.alias COLLATE "C"
        """,
        version,
    )

    # A spelling that folds to nothing is dropped, never keyed on "".
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        # kind='lexicon' rows are extraction-lexicon / query-bridge only: they never project (measured:
        # Django and Hostel inheriting register.pulp). Case-folded; NULL is not lexicon.
        if (row["kind"] or "").strip().casefold() == "lexicon":
            continue
        key = alias_key(row["alias"])
        if key:
            out[key] = (row["facet"], row["term"])

    # A term's own id maps to itself, except register terms; `setdefault` so authored rows win.
    for row in await conn.fetch(
        "SELECT term, facet FROM dna_term WHERE version = $1 ORDER BY term", version
    ):
        if row["facet"] == _EXPLICIT_MAP_ONLY_FACET:
            continue
        key = alias_key(row["term"])
        if key:
            out.setdefault(key, (row["facet"], row["term"]))
    return out


__all__ = ["alias_key", "load_alias_map"]
