"""§6.6's DNA-verdict editor: household rows in `dna_adjudication`, applied in the same transaction.

Beside the bundle's rows, never over them (decisions 326, 445); `derive/ledgers` applies the
household's first (decision 423). Withdrawing restores nothing a DROP removed (§8 stage 7).
"""

from __future__ import annotations

import csv
import io
from typing import Any

import asyncpg

from spielplan.curated import Refused
from spielplan.db import dna_terms
from spielplan.derive import ledgers
from spielplan.importer.dna import ADJUDICATION_COLUMNS

ACTIONS = ("DROP", "REPOINT", "DROP_EVIDENCE")

# `title` rules on one title; `global` is the blanket half (`derive/ledgers._BLANKET_RULES`).
SCOPES = ("title", "global")

NO_VOCABULARY = (
    "No DNA vocabulary is installed, so there is no term for a verdict to rule on. "
    "Import the bundle first."
)
READ_ONLY = (
    "This verdict came with the bundle and is read-only here: the next import would restore it. "
    "Write a household verdict beside it instead; the household's takes effect."
)

_SELECT = """
    SELECT a.id, a.scope, a.title_id, t.name, a.term, a.verdict AS action, a.target, a.quote,
           a.source, a.note, a.origin, a.decided_at
      FROM dna_adjudication a LEFT JOIN title t ON t.id = a.title_id
"""
# Household first: the row that takes effect (decision 423).
_ROWS = _SELECT + " WHERE a.version = $1 ORDER BY a.origin DESC, a.term, a.title_id NULLS FIRST, a.id"
_ROW = _SELECT + " WHERE a.id = $1"
_ROW_FOR_WITHDRAWAL = _SELECT + " WHERE a.id = $1 FOR UPDATE OF a"

# `origin` as a literal, not the column's DEFAULT ('bundle').
_INSERT = """
    INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target, quote, source,
                                  note, origin)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'household')
    RETURNING id
"""

# The titles a blanket verdict can reach; the applier is per title (decision 375).
_CARRIERS = "SELECT DISTINCT title_id FROM dna_tag WHERE version = $1 AND term = $2 ORDER BY title_id"

# Under the TSV's own column names, in authoring order, so the importer reads them back in order.
_EXPORT = """
    SELECT scope, title_id, term, verdict AS action, target, quote, source, note
      FROM dna_adjudication
     WHERE version = $1 AND origin = 'household'
     ORDER BY id
"""


def _text(value: object) -> str | None:
    """A field as the importer will read it back: stripped, and absent when empty."""
    if value is None:
        return None
    return str(value).strip() or None


async def rows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every verdict at the active vocabulary, bundle and household, with the title it names."""
    version = await dna_terms.active_version(conn)
    if version is None:
        return []
    return [dict(row) for row in await conn.fetch(_ROWS, version)]


async def author(
    conn: asyncpg.Connection,
    *,
    scope: str,
    term: str,
    action: str,
    title_id: int | None = None,
    target: str | None = None,
    quote: str | None = None,
    source: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Write one household verdict and apply it, or refuse with the rule it broke.

    Refused here are the rows the applier would store and then ignore or miscount. Returns the row
    and the applier's counts (summed, with `titles`, for a global verdict).
    """
    scope = (scope or "").strip().lower()
    term = (term or "").strip()
    action = (action or "").strip().upper()
    target, quote, source, note = _text(target), _text(quote), _text(source), _text(note)

    async with conn.transaction():
        version = await dna_terms.active_version(conn)
        if version is None:
            raise Refused(NO_VOCABULARY)
        if scope not in SCOPES:
            raise Refused("A verdict is scoped to one title or is global to the term.")
        if scope == "title":
            if title_id is None:
                raise Refused("A title verdict names the title it rules on.")
            if await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id) is None:
                raise Refused(
                    f"Title {title_id} is not in this library, so a verdict on it rules on nothing."
                )
        elif title_id is not None:
            raise Refused("A global verdict rules on every title carrying the term, so it names none.")
        if not term:
            raise Refused("A verdict names the term it rules on.")
        if action not in ACTIONS:
            raise Refused("A verdict is DROP, REPOINT or DROP_EVIDENCE, as the corpus writes them.")
        if action == "REPOINT":
            if target is None:
                raise Refused("REPOINT names the vocabulary term the tag moves onto.")
            if await conn.fetchval(
                "SELECT 1 FROM dna_term WHERE version = $1 AND term = $2", version, target
            ) is None:
                raise Refused(
                    f"{target} is not a term of vocabulary {version}; a tag re-pointed onto it "
                    "would carry a term no facet, shelf or map knows."
                )
        if action == "DROP_EVIDENCE":
            if scope != "title":
                raise Refused("DROP_EVIDENCE rules on one title's quote, so it names the title.")
            if quote is None:
                raise Refused("DROP_EVIDENCE names the quote it drops.")

        row_id = await conn.fetchval(
            _INSERT, version, scope, title_id, term, action, target, quote, source, note
        )
        if scope == "title":
            applied = await ledgers.apply_adjudications(conn, title_id)
        else:
            carriers = [row["title_id"] for row in await conn.fetch(_CARRIERS, version, term)]
            applied = {"titles": len(carriers)}
            for carrier in carriers:
                for outcome, n in (await ledgers.apply_adjudications(conn, carrier)).items():
                    applied[outcome] = applied.get(outcome, 0) + n
        row = dict(await conn.fetchrow(_ROW, row_id))
    return {"row": row, "applied": applied}


async def withdraw(conn: asyncpg.Connection, row_id: int) -> dict[str, Any]:
    """Delete one household verdict; nothing it dropped comes back. A bundle row is refused."""
    async with conn.transaction():
        row = await conn.fetchrow(_ROW_FOR_WITHDRAWAL, row_id)
        if row is None:
            raise LookupError(f"there is no verdict {row_id}")
        if row["origin"] != "household":
            raise Refused(READ_ONLY)
        await conn.execute("DELETE FROM dna_adjudication WHERE id = $1", row_id)
    return {"withdrawn": dict(row)}


async def export(conn: asyncpg.Connection) -> tuple[str, str]:
    """The household's verdicts as `adjudications_<version>.tsv`, in the importer's own columns.

    Through the csv module: the importer reads with `csv.DictReader`, so tabs and quotes survive.
    """
    version = await dna_terms.active_version(conn)
    if version is None:
        raise Refused(NO_VOCABULARY)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(ADJUDICATION_COLUMNS)
    for row in await conn.fetch(_EXPORT, version):
        writer.writerow(
            ["" if row[column] is None else str(row[column]) for column in ADJUDICATION_COLUMNS]
        )
    return f"adjudications_{version}.tsv", buffer.getvalue()
