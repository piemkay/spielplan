"""§6.6's credit-fact editor: household rows in `credit_correction`, applied last and at once.

Spec v2.1 §6.6 Data ("credit facts (`corrections_v1.tsv`, applied last at every derive, §8 stage 3)"),
§8 stage 3, §14.5; decisions 171, 326, 423 and 445.

THE PROBE THIS MODULE EXISTS TO MAKE FALSE. Decision 171's Cost paragraph: "`DELETE FROM
credit_correction` is unscoped, so when §6.6's ledger editors land, an in-app-authored correction
absent from the next bundle's TSV is wiped (probed: P4 removed the app row)". Decision 326 scoped
that DELETE to `origin = 'bundle'` and decision 423 ordered `apply_corrections` so the household's
row is applied last and wins; this is the writer both were waiting for, and it writes the one
provenance neither import path may take.

APPLIED AT ONCE, AND THE APPLIER IS THE ONE THE DERIVE CALLS. `derive/ledgers.apply_corrections`
runs last in every derive of a title, but a bundle title is never derived, so a correction that
waited for the next derive would never reach the card for most of the library (decision 445). It is
called here in the same transaction as the INSERT, and again after a withdrawal - where its
`_reclaim` takes back the credit it minted for a name the ledger no longer asserts, which is the
only writer that can: that row carries `source = 'correction'`, the label decision 375's scoped
re-derive never reaches.

WHAT IS REFUSED, AND WHY HERE. The applier stores an evidence-less row and refuses it at apply time
("a correction without evidence is an opinion", `mdc/corrections.py:64-66`), because the importer
must keep the file's rows for the round trip. An editor has no file to keep: a row saved without
evidence would sit in the ledger, never reach the card, and read as done. So the editor refuses it,
and a kind outside `CORRECTION_KINDS`, and a title this install does not hold, where the admin can
see why.
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

# The bundle's own file name (`importer/bundle.py` reads `artifacts/corrections_v1.tsv`), so an
# export dropped into a bundle's artifacts directory is read by the import that already looks there.
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
# Household first (the row that takes effect, decision 423), then by title so a household row and
# the bundle row it overrules are read one after the other.
_ROWS = _SELECT + " ORDER BY c.origin DESC, c.title_id NULLS LAST, c.id"
_ROW = _SELECT + " WHERE c.id = $1"
_ROW_FOR_WITHDRAWAL = _SELECT + " WHERE c.id = $1 FOR UPDATE OF c"

# `origin` as a literal, for the reason `importer/dna.load_corrections` gives for its own: which
# rows a re-import may take is the question every reader of this statement has.
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
    """Write one household correction and apply its title's ledger, or refuse with the rule.

    Returns the stored row and `apply_corrections`' counts for the title, so the editor can say
    what reached the card (`replaced`, `added`, `already_correct`).
    """
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

    The re-apply is what makes a withdrawal true on the card: `apply_corrections`' `_reclaim`
    deletes the credit it minted for a name no remaining row asserts, and reports it as
    `withdrawn`. What it does not restore is the music credit a `composer` row replaced, which
    `derive/ledgers.apply_corrections` deleted: only a derive re-reads a source, and nothing here
    schedules one. A title with a raw store gets it back when it is next derived (§8 stage 3, a
    board retry from stage 3); A BUNDLE TITLE NEVER DOES, because it has no raw store to derive from
    (decision 162) and is never re-derived (decision 445), so its replaced credit is gone for good.
    The correction editor says so before a `composer` row is saved and again before one is
    withdrawn (`frontend/src/lib/ledgerEditors.svelte.js`'s `COMPOSER_WARNING`). A bundle row is
    refused; a missing id is a LookupError. [M5.6 review cycle 1, m56-curated-01]
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
    """The household's corrections as `corrections_v1.tsv`, in `CORRECTIONS_COLUMNS` order.

    Written with the csv module for the reason `curated/adjudications.export` gives: the importer
    reads with `csv.DictReader`, so an evidence link or a note holding a tab, a newline or a quote
    has to be quoted to come back as one field.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(CORRECTIONS_COLUMNS)
    for row in await conn.fetch(_EXPORT):
        writer.writerow(
            ["" if row[column] is None else str(row[column]) for column in CORRECTIONS_COLUMNS]
        )
    return EXPORT_NAME, buffer.getvalue()
