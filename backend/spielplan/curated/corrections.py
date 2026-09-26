"""§6.6's credit-fact editor: household rows in `credit_correction`, applied last and at once.

The household's row survives re-import and is applied last (decisions 326, 423). An evidence-less
row is refused here, since the applier would store it and never apply it.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import asyncpg

from spielplan.curated import Refused
from spielplan.derive import ledgers
from spielplan.importer.dna import CORRECTIONS_COLUMNS

KINDS = ledgers.CORRECTION_KINDS

# The bundle's own file name, so an exported file drops straight into a bundle.
EXPORT_NAME = "corrections_v1.tsv"

READ_ONLY = (
    "This correction came with the bundle and is read-only here: the next import would restore it. "
    "Write a household correction beside it instead; the household's takes effect."
)

_SELECT = """
    SELECT c.id, c.title_id, t.name, c.field AS kind, c.new_value AS value, c.evidence, c.note,
           c.origin, c.created_at
      FROM credit_correction c LEFT JOIN title t ON t.id = c.title_id
"""
# Household first (the row that takes effect, decision 423), then by title.
_ROWS = _SELECT + " ORDER BY c.origin DESC, c.title_id NULLS LAST, c.id"
_ROW = _SELECT + " WHERE c.id = $1"
_ROW_FOR_WITHDRAWAL = _SELECT + " WHERE c.id = $1 FOR UPDATE OF c"

# `origin` as a literal: which rows a re-import may take.
_INSERT = """
    INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin)
    VALUES ($1, $2, $3, $4, $5, 'household')
    RETURNING id
"""

# Under the TSV's column names, in authoring order.
_EXPORT = """
    SELECT field AS kind, title_id, new_value AS value, evidence, note
      FROM credit_correction
     WHERE origin = 'household'
     ORDER BY id
"""


def _text(value: object) -> str | None:
    """A field as `importer/dna.parse_corrections` will read it back: stripped, absent when empty."""
    if value is None:
        return None
    return str(value).strip() or None


async def rows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every correction, bundle and household, with the title it names (or none, for a title this
    install never acquired - `credit_correction.title_id` carries no foreign key on purpose)."""
    return [dict(row) for row in await conn.fetch(_ROWS)]


async def author(
    conn: asyncpg.Connection,
    *,
    title_id: int,
    kind: str,
    value: str,
    evidence: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Write one household correction and apply its title's ledger; returns the row and counts."""
    kind = (kind or "").strip()
    value, evidence, note = _text(value), _text(evidence), _text(note)

    async with conn.transaction():
        if title_id is None or await conn.fetchval(
            "SELECT 1 FROM title WHERE id = $1", title_id
        ) is None:
            raise Refused(
                f"Title {title_id} is not in this library, so a correction to it reaches no card."
            )
        if kind not in KINDS:
            raise Refused(f"A correction's kind is one of {', '.join(KINDS)}; the derive applies no other.")
        if value is None:
            raise Refused("A correction names the credit it asserts.")
        if evidence is None:
            raise Refused(
                "A correction carries the evidence that settles it; without it the derive refuses "
                "the row as an opinion and the card never changes."
            )

        row_id = await conn.fetchval(_INSERT, title_id, kind, value, evidence, note)
        applied = await ledgers.apply_corrections(conn, title_id)
        row = dict(await conn.fetchrow(_ROW, row_id))
    return {"row": row, "applied": applied}


async def withdraw(conn: asyncpg.Connection, row_id: int) -> dict[str, Any]:
    """Delete one household correction and re-apply its title's ledger.

    A replaced `composer` credit on a bundle title is gone for good (decisions 162, 445); the
    editor warns before (`COMPOSER_WARNING`). A bundle row is refused.
    """
    async with conn.transaction():
        row = await conn.fetchrow(_ROW_FOR_WITHDRAWAL, row_id)
        if row is None:
            raise LookupError(f"there is no correction {row_id}")
        if row["origin"] != "household":
            raise Refused(READ_ONLY)
        await conn.execute("DELETE FROM credit_correction WHERE id = $1", row_id)
        applied = await ledgers.apply_corrections(conn, row["title_id"])
    return {"withdrawn": dict(row), "applied": applied}


async def export(conn: asyncpg.Connection) -> tuple[str, str]:
    """The household's corrections as `corrections_v1.tsv`, in `CORRECTIONS_COLUMNS` order, via csv."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(CORRECTIONS_COLUMNS)
    for row in await conn.fetch(_EXPORT):
        writer.writerow(
            ["" if row[column] is None else str(row[column]) for column in CORRECTIONS_COLUMNS]
        )
    return EXPORT_NAME, buffer.getvalue()
