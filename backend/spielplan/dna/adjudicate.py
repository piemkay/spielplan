"""The curation ledger the trust boundary consults (§8 stage 3, §14.5).

Read per call, never cached: imports replace `dna_adjudication` wholesale. Verdicts are read across
both projects' spellings, case-folded; an unknown verdict changes no tag (decision 389).
"""

from __future__ import annotations

import asyncpg

# `rename` is the app's spelling, `repoint` the corpus's; `merge` re-points too.
REPOINT_VERDICTS = frozenset({"rename", "repoint", "merge"})

RETIRE_VERDICTS = frozenset({"drop"})

# Only meaningful per title, to except a title from a blanket rule.
KEEP_VERDICTS = frozenset({"keep"})

# The first of these on a title ends the search, so a per-title row beats the blanket rule.
TERM_VERDICTS = REPOINT_VERDICTS | RETIRE_VERDICTS | KEEP_VERDICTS

# Per-title rows first (`title_id IS NULL` sorts false first), then file order by `id`.
_LEDGER_FOR_TERM = """
    SELECT title_id, verdict, target
      FROM dna_adjudication
     WHERE version = $1 AND term = $2 AND (title_id = $3 OR title_id IS NULL)
     ORDER BY title_id IS NULL, id
"""


def _verdict_key(verdict: object) -> str:
    """Stripped and case-folded. `str()` so a NULL keys to "" rather than "none"."""
    return str(verdict or "").strip().lower()


async def _verdicts_for(
    conn: asyncpg.Connection, term: str, title_id: int | None, version: str
) -> list[asyncpg.Record]:
    """This title's verdicts on one term, then the blanket ones, in the order they were written."""
    return await conn.fetch(_LEDGER_FOR_TERM, version, term, title_id)


async def rename(
    conn: asyncpg.Connection, term: str, title_id: int | None = None, *, version: str | None
) -> str | None:
    """The term a retired id was re-pointed to, or None.

    The first per-title verdict about the term ends the search; in the blanket sweep a `drop` does not.
    """
    if not version:
        return None
    rows = await _verdicts_for(conn, term, title_id, version)

    for row in rows:
        if row["title_id"] is None:
            continue
        verdict = _verdict_key(row["verdict"])
        if verdict in TERM_VERDICTS:
            target = (row["target"] or "").strip()
            return target if verdict in REPOINT_VERDICTS and target else None

    for row in rows:
        if row["title_id"] is not None:
            continue
        verdict = _verdict_key(row["verdict"])
        target = (row["target"] or "").strip()
        if verdict in REPOINT_VERDICTS and target:
            return target
    return None


async def is_retired(
    conn: asyncpg.Connection, term: str, title_id: int | None = None, *, version: str | None
) -> bool:
    """True when the ledger drops this term outright rather than re-pointing it.

    Separates a curated retirement from `unknown_term`; evidence rows and unknown verdicts fall through.
    """
    if not version:
        return False
    rows = await _verdicts_for(conn, term, title_id, version)

    for row in rows:
        if row["title_id"] is None:
            continue
        verdict = _verdict_key(row["verdict"])
        if verdict in TERM_VERDICTS:
            return verdict in RETIRE_VERDICTS

    return any(
        _verdict_key(row["verdict"]) in RETIRE_VERDICTS
        for row in rows if row["title_id"] is None
    )


__all__ = ["is_retired", "rename"]
