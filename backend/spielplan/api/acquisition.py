"""§6.6's Acquisition card: the board and its three actions. Spec v2.1 §6.6, §8; decisions 322,
330, 336, 345, 444.

Its own module rather than two more routes in `api/admin.py`, which is the precedent M4.14 set
when it gave the bundle routes `api/artifacts.py` instead: `admin.py` is already the largest
entry in the residue ratchet at seventeen statements, and a surface whose controls were then
undecided (proposal 109's retry / retry-from-stage / abandon, adopted by number under decision
330 at M5.6) should grow in a file whose diff is about that surface alone.

M5.1 shipped the two GETs and no way to move a job, because a retry that shipped before decision
330 would have been an admin control nobody chose the semantics of. M5.6 adds the three POSTs
decision 444 decides - what each board state admits and what each action writes - and every rule
in them is `acquire/actions.py`'s: this module maps a refusal to 409 with its sentence as the
detail, a board with no row to 404, and answers a done action with the job as it now stands.

THIN BY CONSTRUCTION, AND MEASURED. Every statement this surface runs lives in
`spielplan/acquire/board.py` or, for the three actions, `spielplan/acquire/actions.py`, so this
module's raw-SQL residue is zero - which is why it is
absent from `test_layering_guards.py`'s `ALLOWED_RESIDUE` rather than present with a `0`. That
dict carries no zero rows by design: "Modules not named here hold zero, and that is asserted
rather than assumed", and the ratchet's grown half fails on the first statement to appear here
instead of tolerating it under a key somebody wrote in advance. The rule is not decoration on
this surface: a query written here would be a rule about what the board may show, and decision
345 is exactly such a rule.

ADMIN, THE WAY EVERY OTHER ADMIN ROUTE IS: `AdminUser` resolves §3.1's and §3.2's three refusals
at once - 401 for a stranger, 403 for a member, and a re-prompt for an admin whose last password
authentication is older than 24 h - and `test_api_gating.py`'s two sweeps walk the dependency
graph, so these routes join them by taking the dependency rather than by being listed anywhere.
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
    """The stage a retry resumes at. Its range is the job's own - 1 to the stage it reached - so it
    is `acquire/actions.retry_from` that refuses one outside it, with a sentence, not a 422."""

    stage: int


def _offered(job: dict[str, Any]) -> dict[str, Any]:
    """A board row with the actions its state admits (decision 444), which the server decides and
    the surface renders - `acquire/actions.admitted` is the one reading of decision 444's table."""
    return {**job, "actions": actions.admitted(job["status"])}


@router.get("")
async def pipeline_board(_: AdminUser, conn: DB) -> dict[str, Any]:
    """§6.6's pipeline board: per-title stage, status and reason.

    An envelope rather than a bare list, for the reason every other read on this app uses one:
    a top-level array is the one JSON shape that cannot grow. M5.6 adds the controls and M5.7
    adds queue depth to §6.6's System card, and both are additions a client should be able to
    ignore rather than a second route.

    THE BOARD IS NOT EMPTY ON A REAL INSTALL, and a reader of this route should know it before
    they interpret what they see: `placement/reconcile._park_thin` has been writing
    `(stage = 2, status = 'parked')` rows for thin-but-placed titles since M4.13, and §5.3 calls
    them "parked as acquisition jobs for M5 enrichment". Those rows are the pipeline's inbox and
    not a backlog of failures (decision 336), so a household opening this surface for the first
    time sees the work waiting rather than an accusation.

    `stages` is §8's ten names out of the driver's one tuple, which the surface draws its segments
    from, and each job carries the `actions` its state admits (decision 444), so the surface offers
    what the server will accept rather than deriving it.
    """
    return {
        "stages": actions.stage_legend(),
        "jobs": [_offered(job) for job in await board.board(conn)],
    }


@router.get("/{title_id}")
async def job_detail(title_id: int, _: AdminUser, conn: DB) -> dict[str, Any]:
    """One title: its board row, the queue rows behind it, and what has been fetched for it.

    404 on a title the pipeline has never touched, and that is a statement about the board and
    not about the title. `acquisition_job` gains its row at stage 1, so a title with no row is
    either a bundle title the pipeline has no opinion about or one whose task parked at stage 1
    and minted nothing at all (decision 323) - and an empty envelope would present both as a job
    that exists and is doing nothing.

    `documents` is the `raw_document` METADATA and never the document. Decision 345: the bytes
    live under `/data/raw`, which is mounted on the worker and deliberately absent from this
    container (M4.7 sec-08), so they are reachable only by an operator on the box. That is
    enforced in `acquire/board.py`, where the column list is, rather than by this handler
    remembering not to ask for it.
    """
    found = await board.job(conn, title_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no acquisition job for this title",
        )
    return {
        "job": found,
        "tasks": await board.tasks_for_title(conn, title_id),
        "documents": await board.documents_for_title(conn, title_id),
    }


async def _act(
    conn: Any, title_id: int, action: Callable[..., Awaitable[int]], *args: Any
) -> dict[str, Any]:
    """Run one board action and answer with the job as it now stands, or refuse.

    409 carries the refusal's own sentence as the detail - the meter's `over spend cap` reason for a
    paid-stage retry, decision 336's for a plain retry of a parked job - because the surface shows
    it verbatim and a translated status code would tell an operator less than the domain said. The
    job comes back with its admitted actions so the row can be redrawn from this answer alone.
    """
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
