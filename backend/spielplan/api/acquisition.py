"""§6.6's Acquisition board and its three actions (decision 444). Every rule and statement is
`acquire/board.py`'s or `acquire/actions.py`'s; this maps refusals to 409, a missing job to 404.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from spielplan.acquire import actions, board
from spielplan.api.deps import DB, AdminUser

router = APIRouter(prefix="/api/admin/acquisition", tags=["admin", "acquisition"])


class RetryFrom(BaseModel):
    """Range-checked by `acquire/actions.retry_from`, with a sentence rather than a 422."""

    stage: int


def _offered(job: dict[str, Any]) -> dict[str, Any]:
    """A board row with the actions its state admits, decided server-side (decision 444)."""
    return {**job, "actions": actions.admitted(job["status"])}


@router.get("")
async def pipeline_board(_: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's pipeline board. Parked stage-2 rows from `placement/reconcile` are the pipeline's inbox,
    not failures (decision 336)."""
    return {
        "stages": actions.stage_legend(),
        "jobs": [_offered(job) for job in await board.board(conn)],
    }


@router.get("/{title_id}")
async def job_detail(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """404 for a title the pipeline never touched. `documents` is metadata only: the bytes stay on the
    worker's `/data/raw` (decision 345)."""
    found = await board.job(conn, title_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no acquisition job for this title",
        )
    return {
        "job": found,
        "documents": await board.documents_for_title(conn, title_id),
    }


async def _act(
    conn: Any, title_id: int, action: Callable[..., Awaitable[int]], *args: Any
) -> dict[str, Any]:
    """409 carries the domain's refusal sentence verbatim; the job comes back with its admitted actions."""
    try:
        await action(conn, title_id, *args)
    except actions.NoJob as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except actions.ActionRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    found = await board.job(conn, title_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=actions.NO_JOB)
    return {"job": _offered(found)}


@router.post("/{title_id}/retry")
async def retry(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Decision 444's plain retry: a failed job walked again from the stage that failed."""
    return await _act(conn, title_id, actions.retry)


@router.post("/{title_id}/retry-from")
async def retry_from(title_id: int, body: RetryFrom, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Walk a job again from a stage it reached; nothing before that stage runs (decision 424)."""
    return await _act(conn, title_id, actions.retry_from, body.stage)


@router.post("/{title_id}/abandon")
async def abandon(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Stop a parked or failed job until it is retried from a stage (decision 444)."""
    return await _act(conn, title_id, actions.abandon)
