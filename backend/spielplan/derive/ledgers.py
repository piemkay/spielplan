"""The two curated ledgers §8 stage 3 ends with: DNA verdicts at ingest, credit facts last.

Two calls at two points, never merged, or the derive silently reverts curated fixes (§14.5).
Both take one title and neither filters on `origin` (decision 326).
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms
from spielplan.derive import ids

# Both verdict vocabularies (app and corpus) folded onto one set of actions (decision 376).
KEEP = "keep"
REPOINT = "repoint"
DROP = "drop"
DROP_EVIDENCE = "drop_evidence"

_VERDICTS = {
    "keep": KEEP,
    "rename": REPOINT,
    "merge": REPOINT,
    "repoint": REPOINT,
    "drop": DROP,
    "drop_evidence": DROP_EVIDENCE,
}

# The two kinds the ledger ships; anything else is counted and skipped.
CORRECTION_KINDS = ("composer", "composer_add")

# A corrected credit's provenance; no re-derive scope deletes it, so `_reclaim` must.
CORRECTION_SOURCE = "correction"
_MUSIC_DEPARTMENT = "Sound"
_MUSIC_JOB = "Original Music Composer"

# Per-title rules, then blanket ones, in file order. `origin DESC` puts household rows first
# because this applier is first-wins, and keeps the order stable across re-imports (decision 423).
_TITLE_RULES = """
    SELECT term, verdict, target, quote FROM dna_adjudication
     WHERE version = $1 AND scope = 'title' AND title_id = $2
     ORDER BY origin DESC, id
"""
_BLANKET_RULES = """
    SELECT term, verdict, target, quote FROM dna_adjudication
     WHERE version = $1 AND NOT (scope = 'title' AND title_id IS NOT NULL)
     ORDER BY origin DESC, id
"""

_TAGS_FOR_TERM = "SELECT id, provider FROM dna_tag WHERE title_id = $1 AND version = $2" \
                 " AND term = $3 ORDER BY id"

_FACET_OF = "SELECT facet FROM dna_term WHERE version = $1 AND term = $2"

# `id <> $4`, or a tag already on the target would merge into itself.
_MERGE_TARGET = """
    SELECT id FROM dna_tag
     WHERE title_id = $1 AND version = $2 AND term = $3 AND id <> $4
       AND provider IS NOT DISTINCT FROM $5
     ORDER BY id LIMIT 1
"""

# GREATEST in SQL: a Python comparison of weights is what `test_landmine_guards.py` refuses.
# COALESCE because a NULL `n_sources` means uncounted, not zero.
_TAG_MERGE = """
    UPDATE dna_tag AS keep
       SET salience = GREATEST(keep.salience, src.salience),
           n_sources = GREATEST(COALESCE(keep.n_sources, 1), COALESCE(src.n_sources, 1))
      FROM dna_tag AS src
     WHERE keep.id = $1 AND src.id = $2
"""

# Only credits this applier minted. `<> ALL` over an empty array matches every row: the
# withdrawn-ledger case.
_STRANDED_CREDITS = """
    SELECT c.id FROM credit c JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1 AND c.source = $2 AND p.name <> ALL($3::text[])
     ORDER BY c.id
"""

# Deliberately wider than `role_class = 'composer'`, so a mis-filed music credit is replaced too.
_MUSIC_CREDITS = """
    SELECT c.id, p.name FROM credit c JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1
       AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')
     ORDER BY c.id
"""


def _bump(stats: dict[str, int], key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


async def apply_adjudications(conn: asyncpg.Connection, title_id: int) -> dict[str, int]:
    """Rule the curated DNA ledger over this title's tags, at the derive's INGEST point.

    Rules over whatever rows the title carries for the active version. Returns counts by outcome.
    """
    version = await dna_terms.active_version(conn)
    if version is None:
        return {}

    rules = [dict(row) for row in await conn.fetch(_TITLE_RULES, version, title_id)]
    blanket = [dict(row) for row in await conn.fetch(_BLANKET_RULES, version)]
    if not rules and not blanket:
        return {}
    stats: dict[str, int] = {"rules": len(rules) + len(blanket)}

    # 1. Evidence-level drops, per-title rules only, before anything moves a term.
    for rule in rules:
        if _VERDICTS.get((rule["verdict"] or "").strip().lower()) == DROP_EVIDENCE:
            await _drop_evidence(conn, title_id, version, rule, stats)

    # 2. Per-title verdicts. `handled` stops phase 3 touching a term already ruled on for this title.
    handled: set[str] = set()
    for rule in rules:
        action = _VERDICTS.get((rule["verdict"] or "").strip().lower())
        if action == DROP_EVIDENCE:
            continue
        handled.add(rule["term"])
        await _rule(conn, title_id, version, rule, action, stats)

    # 3. The blanket sweep, over what the per-title rows did not name.
    for rule in blanket:
        if rule["term"] in handled:
            continue
        action = _VERDICTS.get((rule["verdict"] or "").strip().lower())
        if action == DROP_EVIDENCE:
            # The corpus's blanket sweep has no arm for this action either.
            continue
        await _rule(conn, title_id, version, rule, action, stats)

    return stats


async def _rule(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    rule: dict,
    action: str | None,
    stats: dict[str, int],
) -> None:
    """One DROP, REPOINT or KEEP verdict against one term. Unknown verdicts and `keep` are counted."""
    if action == DROP:
        gone = await conn.fetch(
            "DELETE FROM dna_tag WHERE title_id = $1 AND version = $2 AND term = $3 RETURNING id",
            title_id, version, rule["term"],
        )
        for _ in gone:
            _bump(stats, "dropped")
    elif action == REPOINT and (rule["target"] or "").strip():
        await _repoint(conn, title_id, version, rule["term"], rule["target"].strip(), stats)
    elif action == KEEP:
        _bump(stats, "kept")
    else:
        _bump(stats, "unreadable_verdict")


async def _repoint(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    term: str,
    target: str,
    stats: dict[str, int],
) -> None:
    """Re-point every tag carrying `term` onto `target`, merging where the target is already there.

    Refused and counted when the vocabulary lacks `target`: the ledger must never invent a term.
    """
    facet = await conn.fetchval(_FACET_OF, version, target)
    if facet is None:
        _bump(stats, "repoint_target_unknown")
        return
    for row in await conn.fetch(_TAGS_FOR_TERM, title_id, version, term):
        survivor = await conn.fetchval(
            _MERGE_TARGET, title_id, version, target, row["id"], row["provider"]
        )
        if survivor is None:
            await conn.execute(
                "UPDATE dna_tag SET term = $2, facet = $3 WHERE id = $1", row["id"], target, facet
            )
        else:
            await conn.execute(_TAG_MERGE, survivor, row["id"])
            # Evidence moves before the delete: `dna_evidence` is ON DELETE CASCADE.
            await conn.execute(
                "UPDATE dna_evidence SET dna_tag_id = $1 WHERE dna_tag_id = $2", survivor, row["id"]
            )
            await conn.execute("DELETE FROM dna_tag WHERE id = $1", row["id"])
        _bump(stats, "repointed")


async def _drop_evidence(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    rule: dict,
    stats: dict[str, int],
) -> None:
    """Drop the quote this verdict names, and the tag if nothing is left to hold it up.

    Case-insensitive substring match; no quote drops all evidence. A tag with no quote is dropped
    (§4.1 rule 1).
    """
    quote = (rule["quote"] or "").strip()
    for row in await conn.fetch(_TAGS_FOR_TERM, title_id, version, rule["term"]):
        if quote:
            dropped = await conn.fetch(
                "DELETE FROM dna_evidence WHERE dna_tag_id = $1"
                " AND strpos(lower(quote), lower($2)) > 0 RETURNING id",
                row["id"], quote,
            )
        else:
            dropped = await conn.fetch(
                "DELETE FROM dna_evidence WHERE dna_tag_id = $1 RETURNING id", row["id"]
            )
        if dropped:
            _bump(stats, "evidence_dropped")
        if not await conn.fetchval(
            "SELECT count(*) FROM dna_evidence WHERE dna_tag_id = $1", row["id"]
        ):
            await conn.execute("DELETE FROM dna_tag WHERE id = $1", row["id"])
            _bump(stats, "dropped")


async def apply_corrections(conn: asyncpg.Connection, title_id: int) -> dict[str, int]:
    """Apply this title's credit corrections. LAST, after everything else the derive writes.

    Not parser bugs: sources are wrong, so the fix is recorded and re-applied after every derive.
    Idempotent. Household rows apply last because this applier is last-wins (decision 423). Also
    reclaims credits the ledger no longer asserts, before the early return.
    """
    rows = await conn.fetch(
        "SELECT field, new_value, evidence FROM credit_correction WHERE title_id = $1"
        " ORDER BY origin, id",
        title_id,
    )
    stats: dict[str, int] = {"rows": len(rows)}
    # The names the ledger still asserts, by the loop's own tests.
    asserted = {
        name
        for row in rows
        if (row["field"] or "").strip() in CORRECTION_KINDS
        and (row["evidence"] or "").strip()
        if (name := (row["new_value"] or "").strip())
    }
    withdrawn = await _reclaim(conn, title_id, asserted)
    if withdrawn:
        stats["withdrawn"] = withdrawn
    if not rows:
        return stats
    if await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id) is None:
        # `credit_correction.title_id` has no FK on purpose; unknown titles are counted and skipped.
        stats["unknown_title"] = len(rows)
        return stats

    for row in rows:
        kind = (row["field"] or "").strip()
        if kind not in CORRECTION_KINDS:
            _bump(stats, "unknown_kind")
            continue
        if not (row["evidence"] or "").strip():
            _bump(stats, "no_evidence")      # a correction without evidence is an opinion
            continue
        name = (row["new_value"] or "").strip()
        if not name:
            _bump(stats, "no_value")
            continue

        existing = await conn.fetch(_MUSIC_CREDITS, title_id)
        have = {record["name"] for record in existing}
        if kind == "composer":
            if have == {name}:
                _bump(stats, "already_correct")
                continue
            await conn.execute(
                "DELETE FROM credit WHERE id = ANY($1::bigint[])",
                [record["id"] for record in existing],
            )
            await _credit(conn, title_id, name)
            _bump(stats, "replaced")
        else:
            if name in have:
                _bump(stats, "already_correct")
                continue
            await _credit(conn, title_id, name)
            _bump(stats, "added")

    return stats


async def _reclaim(conn: asyncpg.Connection, title_id: int, asserted: set[str]) -> int:
    """Delete the credits this applier minted for names the ledger no longer asserts."""
    stranded = await conn.fetch(_STRANDED_CREDITS, title_id, CORRECTION_SOURCE, sorted(asserted))
    if stranded:
        await conn.execute(
            "DELETE FROM credit WHERE id = ANY($1::bigint[])", [row["id"] for row in stranded],
        )
    return len(stranded)


async def _credit(conn: asyncpg.Connection, title_id: int, name: str) -> None:
    """The one corrected credit, minted through `derive/ids.upsert_person`'s id rule."""
    person_id = await ids.upsert_person(conn, name=name)
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        title_id, person_id, _MUSIC_DEPARTMENT, _MUSIC_JOB,
        ids.classify_role(_MUSIC_DEPARTMENT, _MUSIC_JOB), CORRECTION_SOURCE,
    )
