"""§6.6's DNA-verdict editor: household rows in `dna_adjudication`, true the moment they are saved.

Spec v2.1 §6.6 Data, §8 stage 3 ("ends by applying BOTH curated ledgers"), §8 stage 7 ("Failures
drop, never repaired"), §14.5; decisions 326, 376, 389, 423 and 445.

A HOUSEHOLD VERDICT IS A ROW IN THE BUNDLE'S OWN TABLE, NOT A SECOND LEDGER. `importer/dna.
load_adjudications` fills `dna_adjudication` from `adjudications_<version>.tsv` with `origin =
'bundle'` and DELETEs only those rows on a models-only re-import (decision 326), so a row written
here with `origin = 'household'` survives every re-import by construction. `derive/ledgers.
apply_adjudications` reads both origins and sorts the household's first, which in a first-effective
applier is what makes the household's verdict the one that takes effect (decision 423). A title the
bundle already rules on gets a second, household row beside the bundle's, never an edit of it:
decision 445 makes a bundle row read-only in the app, because an edited bundle row is restored by
the next import and the household's fix would last until it forgot it had made one.

APPLIED AT ONCE, IN THE SAME TRANSACTION (decision 445). Stage 3 applies the ledger at every derive,
but a bundle title is never derived - it arrived whole - so a verdict that waited for the next derive
would be saved and never true for most of the library. A title verdict is applied to its title; a
global one to every title carrying the term at the active version, which is `derive/ledgers`'
blanket sweep run over the titles it can reach. The applier is called, never re-implemented: it
owns the three-phase order and the per-title-beats-blanket rule (`mdc/dna/adjudication.py:22-28`),
and a second copy of either is the drift §14.5's scar is made of.

WITHDRAWN, IT RESTORES NOTHING. §8 stage 7 drops and never repairs, and a DROP deleted the tag and
its evidence rows: there is no copy to put back, and inventing one would be a tag with no quote in
the title's pack (§4.1 rule 1). What withdrawal does is stop the verdict applying, so the next
extraction or derive of the term is no longer ruled on. Re-deriving the title is what restores a
dropped reading, and that is a board retry from stage 3 (decision 444), not an editor's write.

THE CORPUS'S SPELLING. `ACTIONS` is the three strings the shipped `adjudications_v1.tsv` carries and
what `derive/ledgers._VERDICTS` folds onto its actions under decision 389, so the export lands in the
corpus's ledger unchanged and needs no translation back.
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

# `title` rules on one title and is applied first; `global` is the blanket half the applier sweeps
# over what the title rows did not name (`derive/ledgers._BLANKET_RULES`). The corpus also writes
# `scope = 'term'`, which the applier reads as blanket too; the editor writes this table's own two.
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
# Household first because it is the row that takes effect (decision 423), then by term so a title
# verdict and the blanket rule it shadows sit together.
_ROWS = _SELECT + " WHERE a.version = $1 ORDER BY a.origin DESC, a.term, a.title_id NULLS FIRST, a.id"
_ROW = _SELECT + " WHERE a.id = $1"
_ROW_FOR_WITHDRAWAL = _SELECT + " WHERE a.id = $1 FOR UPDATE OF a"

# `origin` as a literal rather than the column's DEFAULT (which is 'bundle'): which rows an import
# may take is the one question a reader of this statement has.
_INSERT = """
    INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target, quote, source,
                                  note, origin)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'household')
    RETURNING id
"""

# The titles a blanket verdict can reach today. The applier is per title (decision 375), so the
# global case is the same call once per title rather than a wider statement.
_CARRIERS = "SELECT DISTINCT title_id FROM dna_tag WHERE version = $1 AND term = $2 ORDER BY title_id"

# Selected under the TSV's own column names, in authoring order, so that each row is written as
# the importer reads it and `load_adjudications` inserts them back in the order they were typed.
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
    """Every verdict at the active vocabulary, bundle and household, with the title it names.

    The bundle's rows are listed because §6.6 has each editor show "its rows, its provenance and
    the derive that will re-apply it", and a household verdict beside a bundle one only reads as
    taking effect when both are on the screen.
    """
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

    Each refusal is a row the applier would store and then ignore or miscount, refused here where
    the admin can read why rather than saved as a fix that is never true:

      * a REPOINT onto a term `dna_term` does not carry is `_repoint`'s `repoint_target_unknown`,
        which moves nothing - the one place the ledger could invent a term;
      * a DROP_EVIDENCE with no quote drops every quote the term has on the title, which is the
        corpus's behaviour for such a row and never what a household typing one meant;
      * a DROP_EVIDENCE with no title is passed over by the blanket sweep ("a quote is a fact about
        one title's pack, and a blanket rule naming one cannot mean anything");
      * a title this install does not hold would rule over nothing, and a global verdict naming a
        title would be read as blanket anyway (`_BLANKET_RULES`).

    Returns the stored row and the applier's counts - summed over the titles, with `titles`, for a
    global verdict - so the editor can say what the save did.
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
    """Delete one household verdict. Nothing it dropped comes back (see the module docstring).

    A bundle row is refused rather than deleted: the next models-only import would re-insert it, so
    the withdrawal would be a fix that lasts until the household forgets it made one. A missing id
    is a LookupError, which is a different answer from a refusal.
    """
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

    Decision 445: "Export is household rows only, in the importer's column set". The header is
    `importer/dna.ADJUDICATION_COLUMNS` itself, imported and never retyped, and the rows go through
    the csv module with a TAB delimiter, because `validate._read_tsv` reads with `csv.DictReader`: a
    note holding a tab, a newline or a quote character is quoted here and read back whole there,
    where a TAB-join would split it into columns the loader then mis-assigns.
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
