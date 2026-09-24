"""§6.6's third editor: the per-facet axes §6.4 describes, as household rows in `dna_axis`.

Spec v2.1 §6.4 ("Axis definitions are a shipped, authored artifact: one TSV per vocabulary-v1 facet
(left pole, right pole, term -> weight in [-1, 1]) ... Editable in the §6.6 ledger editor"), §6.6
Data, §6.2 step 5; decisions 173, 264, 326, 342, 423 and 445.

AN EDITOR OVER AN EMPTY SET, AND IT SAYS SO (decision 342). §6.4: "No such file has been authored
yet and the corpus bundle ships none" (decision 173). The editor ships anyway because §6.6 names
three editors and three artifacts, and because the axis is not only the Map's: `tonight/dna.
axes_for` reads `dna_axis_weight` live on every evening, so an axis saved here turns on §6.2 step
5's split surfacing now, and §6.4's Map reads the same rows when it arrives at M6. With no axis,
`combine.contested_facet` has nothing to contest on - which is the state `importer/dna.load_axes`
warns about on every import of a bundle that carries none.

VALIDATED AS THE LOADER VALIDATES, so nothing saved here is a file the loader would refuse. The
rules are `load_axes`' own: the facet is one `dna_facet` declares at the version (the FK it checks
before writing), both poles are named (its two-pole header rule), every weight is a number (it skips
the row otherwise) inside [-1, 1] (it fails the report otherwise, and the column's CHECK agrees), and
there is at least one weight row - decision 264, because zero rows is indistinguishable from an
export that did not finish writing. A duplicated term is refused too: the loader would keep the last
of two rows silently, and a household that typed both meant one of them. Like the loader, this does
not ask whether a term belongs to the vocabulary: the fixture's own `sensibility` axis weighs a
`register` term, and §6.4 leaves what an axis weighs to its author.

HOUSEHOLD ROWS, AND A BUNDLE AXIS IS READ-ONLY (decision 445). `dna_axis` is keyed on (version,
facet), so unlike the two ledgers there is no room for a household row beside a bundle row: a
household axis over a shipped facet could only be written by overwriting the bundle's, which the
next import would then reload over the household's work. So a facet the bundle ships is refused,
and a facet the household authored is left in place by `load_axes` with a warning (decisions 342
and 423: the household's curated row takes effect).
"""

from __future__ import annotations

import csv
import io
import math
from typing import Any

import asyncpg

from spielplan.curated import Refused
from spielplan.db import dna_terms

NO_VOCABULARY = (
    "No DNA vocabulary is installed, so there is no facet for an axis to turn. Import the bundle first."
)
READ_ONLY = (
    "This facet's axis came with the bundle and is read-only here: the next import would restore it."
)

# The facets the vocabulary declares, in its own order - the set an axis may be written for, and
# what the editor lists so the facets with no axis are visible as such.
_DECLARED = "SELECT facet FROM dna_facet WHERE version = $1 ORDER BY ord, facet"

_AXES = """
    SELECT a.facet, a.left_pole, a.right_pole, a.origin
      FROM dna_axis a JOIN dna_facet f ON f.version = a.version AND f.facet = a.facet
     WHERE a.version = $1
     ORDER BY f.ord, a.facet
"""
_TERMS = "SELECT facet, term, weight FROM dna_axis_weight WHERE version = $1 ORDER BY facet, term"

_AXIS = "SELECT facet, left_pole, right_pole, origin FROM dna_axis WHERE version = $1 AND facet = $2"
_AXIS_FOR_WITHDRAWAL = _AXIS + " FOR UPDATE"
_AXIS_TERMS = "SELECT term, weight FROM dna_axis_weight WHERE version = $1 AND facet = $2 ORDER BY term"

# The conflict arm writes only over a household row. A bundle row makes the statement return
# nothing, which is the refusal - decided by the same statement that writes, so an import landing
# the facet between a read and this write cannot have its row overwritten under its own label.
_UPSERT = """
    INSERT INTO dna_axis (version, facet, left_pole, right_pole, origin)
    VALUES ($1, $2, $3, $4, 'household')
    ON CONFLICT (version, facet) DO UPDATE
       SET left_pole = EXCLUDED.left_pole, right_pole = EXCLUDED.right_pole
     WHERE dna_axis.origin = 'household'
    RETURNING facet
"""
_CLEAR = "DELETE FROM dna_axis_weight WHERE version = $1 AND facet = $2"
_ADD = "INSERT INTO dna_axis_weight (version, facet, term, weight) VALUES ($1, $2, $3, $4)"

# The stored float4 as Postgres prints it, which is its shortest exact form: 0.3 is written `0.3`,
# not the `0.30000001192092896` the driver's float8 would print, and reads back as the same float4.
_EXPORT_TERMS = (
    "SELECT term, weight::text AS weight FROM dna_axis_weight WHERE version = $1 AND facet = $2"
    " ORDER BY term"
)


def _text(value: object) -> str | None:
    """A pole or a term as `load_axes` reads it back: stripped, and absent when empty."""
    if value is None:
        return None
    return str(value).strip() or None


def _validated(
    facet: str, left: str | None, right: str | None, weights: object, declared: set[str]
) -> list[tuple[str, float]]:
    """The axis's rows, or the first of `load_axes`' rules it breaks, as a refusal."""
    if facet not in declared:
        raise Refused(
            f"'{facet}' is not a facet of this vocabulary; an axis is named for the facet it turns."
        )
    if left is None or right is None:
        raise Refused("An axis names both of its poles.")
    entries = list(weights or ())
    if not entries:
        raise Refused(
            "An axis carries at least one term and its weight; an axis with none is what an "
            "export that did not finish writing looks like, and the loader refuses it."
        )
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for entry in entries:
        try:
            raw_term, raw = entry
        except (TypeError, ValueError):
            raise Refused("Each row of an axis is a term and a number.") from None
        term = _text(raw_term)
        if term is None:
            raise Refused("Every row of an axis names a term.")
        if term in seen:
            raise Refused(f"{term} appears twice on this axis; give it one number.")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise Refused(f"The number for {term} is not a number.") from None
        if not math.isfinite(value) or not -1.0 <= value <= 1.0:
            raise Refused(f"The number for {term} is {raw}; an axis runs from -1 to 1.")
        seen.add(term)
        out.append((term, value))
    return out


async def _axis(conn: asyncpg.Connection, version: str, facet: str) -> dict[str, Any] | None:
    head = await conn.fetchrow(_AXIS, version, facet)
    if head is None:
        return None
    terms = await conn.fetch(_AXIS_TERMS, version, facet)
    return {**dict(head), "weights": [dict(row) for row in terms]}


async def rows(conn: asyncpg.Connection) -> dict[str, Any]:
    """Every axis at the active vocabulary with its origin and its terms, and the declared facets.

    The declared facets travel with the axes so the editor can show which facets have no axis at
    all, which on every shipped bundle is all of them (decision 173).
    """
    version = await dna_terms.active_version(conn)
    if version is None:
        return {"version": None, "facets": [], "axes": []}
    facets = [row["facet"] for row in await conn.fetch(_DECLARED, version)]
    terms: dict[str, list[dict[str, Any]]] = {}
    for row in await conn.fetch(_TERMS, version):
        terms.setdefault(row["facet"], []).append({"term": row["term"], "weight": row["weight"]})
    axes = [
        {**dict(row), "weights": terms.get(row["facet"], [])}
        for row in await conn.fetch(_AXES, version)
    ]
    return {"version": version, "facets": facets, "axes": axes}


async def author(
    conn: asyncpg.Connection,
    *,
    facet: str,
    left_pole: str,
    right_pole: str,
    weights: list[tuple[str, float]],
) -> dict[str, Any]:
    """Write one household axis - its poles and the whole of its terms - or refuse with the rule.

    The facet's terms are replaced rather than merged, which is decision 261's rule for the loader
    applied to the editor: the axis the household saves is the whole axis, and a term it removed
    must stop turning it. One transaction, so a refusal or a failure leaves the previous axis
    standing whole.
    """
    facet = (facet or "").strip()
    left, right = _text(left_pole), _text(right_pole)

    async with conn.transaction():
        version = await dna_terms.active_version(conn)
        if version is None:
            raise Refused(NO_VOCABULARY)
        declared = {row["facet"] for row in await conn.fetch(_DECLARED, version)}
        terms = _validated(facet, left, right, weights, declared)
        if await conn.fetchval(_UPSERT, version, facet, left, right) is None:
            raise Refused(READ_ONLY)
        await conn.execute(_CLEAR, version, facet)
        await conn.executemany(_ADD, [(version, facet, term, value) for term, value in terms])
        axis = await _axis(conn, version, facet)
    return {"axis": axis}


async def withdraw(conn: asyncpg.Connection, facet: str) -> dict[str, Any]:
    """Delete the household's axis for a facet; its terms go with it (`dna_axis_weight`'s FK
    cascades). The facet then has no axis until the household writes one or a bundle ships one."""
    facet = (facet or "").strip()
    async with conn.transaction():
        version = await dna_terms.active_version(conn)
        if version is None:
            raise Refused(NO_VOCABULARY)
        head = await conn.fetchrow(_AXIS_FOR_WITHDRAWAL, version, facet)
        if head is None:
            raise LookupError(f"there is no axis for facet {facet!r}")
        if head["origin"] != "household":
            raise Refused(READ_ONLY)
        axis = await _axis(conn, version, facet)
        await conn.execute("DELETE FROM dna_axis WHERE version = $1 AND facet = $2", version, facet)
    return {"withdrawn": axis}


async def export(conn: asyncpg.Connection, facet: str) -> tuple[str, str]:
    """The household's axis as `<facet>.tsv`, exactly the file §6.4 names and `load_axes` reads.

    §6.4: "named for the facet it turns: `<facet>.tsv` ... with the two pole names alone on the
    header line and one `term -> weight` row per line after it". Written through the csv module,
    because `load_axes` reads with `csv.reader`, so a pole or a term holding a tab or a quote is
    quoted here and read back as one cell. Household only (decision 445): a bundle axis is the
    bundle's file already.
    """
    facet = (facet or "").strip()
    version = await dna_terms.active_version(conn)
    if version is None:
        raise Refused(NO_VOCABULARY)
    head = await conn.fetchrow(_AXIS, version, facet)
    if head is None:
        raise LookupError(f"there is no axis for facet {facet!r}")
    if head["origin"] != "household":
        raise Refused(READ_ONLY)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow([head["left_pole"], head["right_pole"]])
    for row in await conn.fetch(_EXPORT_TERMS, version, facet):
        writer.writerow([row["term"], row["weight"]])
    return f"{facet}.tsv", buffer.getvalue()
