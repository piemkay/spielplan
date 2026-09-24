"""§6.6 Data's three ledger editors and its reject review, over HTTP. Spec v2.1 §6.6 Data, §8
stages 3, 6 and 7, §6.4, §4.1 rule 2; decisions 330, 341, 342, 445 and 446.

THREE EDITORS, THREE ROUTE FAMILIES, AND NO HANDLER THAT REACHES TWO OF THEM. Proposal 105, adopted
as provenance for §6.6 Data under decision 445, is "three separate editors with separate semantics,
never one merged screen", and the domain keeps it by writing three tables from three modules that
share no function. A route layer that dispatched on a ledger name, or a handler that called two of
them, would be the merged write path one level up, so each handler below calls exactly one
`curated` module - `test_curated_api.py` reads this file's handlers for it - and the only code the
families share is the spelling of a refusal and of a TSV download.

EACH LEDGER'S LIST SAYS WHAT RE-APPLIES IT (proposal 105's own clause: "each showing its rows, its
provenance and the derive that will re-apply it"). The rows carry `origin`, which is the provenance;
`applies` is the one sentence the domain does not hold, because it is about where each ledger is
read rather than about any row: verdicts at ingest (§8 stage 3's derive and stage 6's extraction),
credit facts last at every derive, the axes read live by §6.2 step 5 and by the Map at M6. Written
here as ASCII with "section" for the sign, because it reaches a console as often as a screen.

A REFUSAL IS 409 WITH THE EDITOR'S SENTENCE, A MISSING ROW 404. `curated.Refused` carries the
sentence the editor composed and `api/acquisition.py` maps its domain's refusals to 409 the same
way. Only a bare `LookupError` is a 404, for `api/llm.connector_test`'s reason: the editors raise it
for a row or facet that is not there, and a `KeyError` from inside one is a fault in this build that
a 404 would report as the row not existing.

THE REVIEW IS TWO ORDERINGS AND NO FILTER (decision 446): the rejects newest first and a title's
extracted tags weakest first, both `dna/review`'s, handed on as read. No parameter here narrows
either list, and none may: a weight behind a query parameter is §4.1 rule 2's cut one layer up.

THIN BY CONSTRUCTION: no statement lives here, so the module is absent from
`test_layering_guards.py`'s `ALLOWED_RESIDUE`. Every route takes `AdminUser`, so
`test_api_gating.py`'s sweeps drive each one as a stranger and as a member.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from spielplan.api.deps import DB, AdminUser
from spielplan.curated import Refused, adjudications, axes, corrections
from spielplan.dna import review

router = APIRouter(prefix="/api/admin", tags=["admin", "curated"])

APPLIES_VERDICTS = (
    "DNA verdicts apply at ingest: at every derive (section 8 stage 3) and after every extraction"
    " (section 8 stage 6), the household's before the bundle's. A saved verdict is applied at once"
    " to the titles it rules on."
)
APPLIES_CORRECTIONS = (
    "Credit facts apply last at every derive (section 8 stage 3), the household's after the"
    " bundle's so it wins. A saved correction is applied to its title at once."
)
APPLIES_AXES = (
    "An axis is read live, by Tonight's split surfacing (section 6.2 step 5) now and by the Map"
    " (section 6.4) at M6; no derive re-applies it. No axis file has been authored and the bundle"
    " ships none."
)

_TSV = "text/tab-separated-values; charset=utf-8"


class Verdict(BaseModel):
    """One DNA verdict as the corpus's ledger spells it (`importer/dna.ADJUDICATION_COLUMNS`)."""

    scope: str
    term: str
    action: str
    title_id: int | None = None
    target: str | None = None
    quote: str | None = None
    source: str | None = None
    note: str | None = None


class Correction(BaseModel):
    """One credit fact as `importer/dna.CORRECTIONS_COLUMNS` spells it."""

    title_id: int
    kind: str
    value: str
    evidence: str
    note: str | None = None


class AxisWeight(BaseModel):
    term: str
    weight: float


class Axis(BaseModel):
    """One facet's axis, whole: its two poles and every term that turns it (§6.4)."""

    facet: str
    left_pole: str
    right_pole: str
    weights: list[AxisWeight]


async def _answer[T](call: Awaitable[T]) -> T:
    """Await one editor call, mapping its refusal to 409 and a missing row to 404."""
    try:
        return await call
    except Refused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    except LookupError as exc:
        if type(exc) is not LookupError:
            raise
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _download(exported: tuple[str, str]) -> Response:
    """An export as the file a household saves and a bundle carries back: the importer's own name
    and columns, UTF-8, served as an attachment."""
    name, text = exported
    return Response(
        content=text.encode("utf-8"), media_type=_TSV,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- DNA verdicts (`adjudications_v1.tsv`) ------------------------------------------------------


@router.get("/curated/adjudications")
async def list_verdicts(_: AdminUser, conn: DB) -> dict[str, Any]:
    """Every verdict at the active vocabulary, the bundle's and the household's, and what applies them."""
    return {"rows": await adjudications.rows(conn), "applies": APPLIES_VERDICTS}


@router.post("/curated/adjudications", status_code=status.HTTP_201_CREATED)
async def author_verdict(body: Verdict, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Write one household verdict and apply it at once (decision 445)."""
    return await _answer(adjudications.author(conn, **body.model_dump()))


@router.get("/curated/adjudications/export")
async def export_verdicts(_: AdminUser, conn: DB) -> Response:
    """The household's verdicts as `adjudications_<version>.tsv`, in the importer's columns."""
    return _download(await _answer(adjudications.export(conn)))


@router.delete("/curated/adjudications/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_verdict(row_id: int, _: AdminUser, conn: DB) -> Response:
    """Withdraw one household verdict; a bundle row is 409, and nothing it dropped comes back."""
    await _answer(adjudications.withdraw(conn, row_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- credit facts (`corrections_v1.tsv`) --------------------------------------------------------


@router.get("/curated/corrections")
async def list_corrections(_: AdminUser, conn: DB) -> dict[str, Any]:
    """Every credit correction, the bundle's and the household's, and what applies them."""
    return {"rows": await corrections.rows(conn), "applies": APPLIES_CORRECTIONS}


@router.post("/curated/corrections", status_code=status.HTTP_201_CREATED)
async def author_correction(body: Correction, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Write one household correction and apply its title's ledger at once (decision 445)."""
    return await _answer(corrections.author(conn, **body.model_dump()))


@router.get("/curated/corrections/export")
async def export_corrections(_: AdminUser, conn: DB) -> Response:
    """The household's corrections as `corrections_v1.tsv`, in `CORRECTIONS_COLUMNS` order."""
    return _download(await _answer(corrections.export(conn)))


@router.delete("/curated/corrections/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_correction(row_id: int, _: AdminUser, conn: DB) -> Response:
    """Withdraw one household correction and re-apply its title's ledger; a bundle row is 409."""
    await _answer(corrections.withdraw(conn, row_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the per-facet axes (§6.4, decision 342) ----------------------------------------------------


@router.get("/curated/axes")
async def list_axes(_: AdminUser, conn: DB) -> dict[str, Any]:
    """Every axis at the active vocabulary with its terms, the declared facets, and what reads them."""
    return {**await axes.rows(conn), "applies": APPLIES_AXES}


@router.post("/curated/axes", status_code=status.HTTP_201_CREATED)
async def author_axis(body: Axis, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Write one household axis, its poles and the whole of its terms (decision 342)."""
    return await _answer(axes.author(
        conn, facet=body.facet, left_pole=body.left_pole, right_pole=body.right_pole,
        weights=[(entry.term, entry.weight) for entry in body.weights],
    ))


@router.delete("/curated/axes/{facet}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_axis(facet: str, _: AdminUser, conn: DB) -> Response:
    """Withdraw the household's axis for a facet; a bundle axis is 409."""
    await _answer(axes.withdraw(conn, facet))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/curated/axes/{facet}/export")
async def export_axis(facet: str, _: AdminUser, conn: DB) -> Response:
    """The household's axis as `<facet>.tsv`, the file §6.4 names and `load_axes` reads."""
    return _download(await _answer(axes.export(conn, facet)))


# --- the review (decision 446) ------------------------------------------------------------------


@router.get("/dna/rejects")
async def dna_rejects(_: AdminUser, conn: DB) -> dict[str, Any]:
    """What stage 7 refused, newest first, bounded on recency and never on a weight."""
    return {"rejects": await review.rejects(conn), "limit": review.REJECT_LIMIT}


@router.get("/dna/evidence/{title_id}")
async def dna_evidence(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """One title's extracted tags at the active vocabulary, weakest first, none left out."""
    return await review.low_evidence(conn, title_id)
