"""§6.6 Data's two ledger editors and reject review. Each handler calls exactly one `curated`
module, never two (decision 445). No parameter filters the review lists: a weight behind a query
parameter would be §4.1 rule 2's cut."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from spielplan.api.deps import DB, AdminUser
from spielplan.curated import Refused, adjudications, corrections
from spielplan.dna import review

router = APIRouter(prefix="/api/admin", tags=["admin", "curated"])

APPLIES_VERDICTS = (
    "Verdicts apply every time a title's features are worked out and after every extraction, the"
    " household's before the bundle's. A saved verdict is applied at once to the titles it rules on."
)
APPLIES_CORRECTIONS = (
    "Credit facts apply last every time a title's features are worked out, the household's after"
    " the bundle's so it wins. A saved correction is applied to its title at once."
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


async def _answer[T](call: Awaitable[T]) -> T:
    """Only a bare LookupError is a 404: a KeyError from inside an editor is a fault, not a missing row."""
    try:
        return await call
    except Refused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    except LookupError as exc:
        if type(exc) is not LookupError:
            raise
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _download(exported: tuple[str, str]) -> Response:
    name, text = exported
    return Response(
        content=text.encode("utf-8"), media_type=_TSV,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/curated/adjudications")
async def list_verdicts(_: AdminUser, conn: DB) -> dict[str, Any]:
    return {"rows": await adjudications.rows(conn), "applies": APPLIES_VERDICTS}


@router.post("/curated/adjudications", status_code=status.HTTP_201_CREATED)
async def author_verdict(body: Verdict, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Write one household verdict and apply it at once (decision 445)."""
    return await _answer(adjudications.author(conn, **body.model_dump()))


@router.get("/curated/adjudications/export")
async def export_verdicts(_: AdminUser, conn: DB) -> Response:
    return _download(await _answer(adjudications.export(conn)))


@router.delete("/curated/adjudications/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_verdict(row_id: int, _: AdminUser, conn: DB) -> Response:
    """Withdraw one household verdict; a bundle row is 409, and nothing it dropped comes back."""
    await _answer(adjudications.withdraw(conn, row_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/curated/corrections")
async def list_corrections(_: AdminUser, conn: DB) -> dict[str, Any]:
    return {"rows": await corrections.rows(conn), "applies": APPLIES_CORRECTIONS}


@router.post("/curated/corrections", status_code=status.HTTP_201_CREATED)
async def author_correction(body: Correction, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Write one household correction and apply its title's ledger at once (decision 445)."""
    return await _answer(corrections.author(conn, **body.model_dump()))


@router.get("/curated/corrections/export")
async def export_corrections(_: AdminUser, conn: DB) -> Response:
    return _download(await _answer(corrections.export(conn)))


@router.delete("/curated/corrections/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_correction(row_id: int, _: AdminUser, conn: DB) -> Response:
    """Withdraw one household correction and re-apply its title's ledger; a bundle row is 409."""
    await _answer(corrections.withdraw(conn, row_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/dna/rejects")
async def dna_rejects(_: AdminUser, conn: DB) -> dict[str, Any]:
    """What stage 7 refused, newest first, bounded on recency and never on a weight."""
    return {"rejects": await review.rejects(conn), "limit": review.REJECT_LIMIT}


@router.get("/dna/evidence/{title_id}")
async def dna_evidence(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """One title's extracted tags at the active vocabulary, weakest first, none left out."""
    return await review.low_evidence(conn, title_id)
