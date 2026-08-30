"""Home and the model-log rail, over HTTP. Spec v2.1 §6.0, §6.7; decisions 18 and 117.

`kind` is required and repeated exactly as `/api/titles` does it, so the empty selection stays
unrepresentable in the URL (decision 18: either or both, never neither).

Every response leaves through `rail.redact`. Decision 117 says the toggle "governs the rail and
every inline annotation", and a gate applied at one exit is a gate that cannot be forgotten at
another — which is why no route below assembles its own payload shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.core.config import settings
from spielplan.db import library
from spielplan.home import rail, shelves
from spielplan.ledger import refit

router = APIRouter(prefix="/api", tags=["home"])


def _now_local() -> tuple[datetime, str]:
    """§2's `TZ`. Proposal 22 puts the greeting on the household clock, not the device clock —
    which also makes the band assertable without a browser."""
    tz = settings().tz
    try:
        return datetime.now(ZoneInfo(tz)), tz
    except Exception:  # noqa: BLE001 - a bad TZ must not take Home down (§3.1)
        return datetime.now(), tz


def _kinds(kind: list[str]) -> list[str]:
    try:
        return library.normalise_kinds(kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


async def _bundle(request: Request, conn: asyncpg.Connection) -> str | None:
    """The ACTIVE bundle, from the database rather than from the process's loaded store.

    §10: "everything expressed in the old Backbone's basis is garbage against a new one." The
    scores and priors Home reads are bound to a bundle_version column, so the version that
    matters is the one those rows were written against — which is the row in `artifact_bundle`,
    not whatever the store happened to open at boot. They agree in normal operation; when they
    disagree, the store is the stale one.
    """
    active = await refit.active_bundle_version(conn)
    if active is not None:
        return active
    store = getattr(request.app.state, "artifacts", None)
    return None if store is None or store.is_empty else store.version


@router.get("/home")
async def home(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: list[Literal["movie", "series"]] = Query(
        ..., description="§4.1 rule 5: one or both, never neither. Repeat the parameter for both."
    ),
    q: str | None = None,
    person_id: int | None = None,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """§6.0's Home payload.

    `mode` is the server's answer to §6.0's own sentence — "Search or an active person-filter
    switches Home into the catalog grid; clearing it returns the shelves". With `q` or
    `person_id` set the response carries `catalog` (one flat, year-ordered list that MAY
    interleave the kinds, per decision 18) and no shelves; with neither, it carries the shelves
    (one kind-headed section each, never one interleaved ranking) and no catalog. The two modes
    cannot both be rendered, because only one of them is ever in the payload.
    """
    now_local, tz = _now_local()
    payload = await shelves.build_home(
        conn,
        user=user,
        kinds=_kinds(kind),
        bundle_version=await _bundle(request, conn),
        now_local=now_local,
        tz=tz,
        q=q,
        person_id=person_id,
        limit=limit,
        offset=offset,
    )
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/home/shelves")
async def home_shelves(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: list[Literal["movie", "series"]] = Query(...),
) -> dict[str, Any]:
    """The shelves alone, for a client that renders the greeting and banner separately.

    Same builder, same gate, same partition. It exists so the shelf row can be refetched on a
    kind toggle without re-running the banner's population query, and it deliberately cannot be
    asked for the grid: a caller who wants the grid wants `/api/titles`.
    """
    now_local, tz = _now_local()
    payload = await shelves.build_home(
        conn,
        user=user,
        kinds=_kinds(kind),
        bundle_version=await _bundle(request, conn),
        now_local=now_local,
        tz=tz,
    )
    slim = {
        key: payload[key]
        for key in ("kinds", "shelves", "sections", "shelves_total", "verdict_count",
                    "degraded", "partner", "bundle", "vocabulary", "suppressed")
        if key in payload
    }
    return rail.redact(slim, show_model=rail.visible_to(user))


@router.get("/home/pending-verdicts")
async def pending(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """§6.0's banner on its own. Reads only — proposal 150: "it never writes `seen`".

    Returns `{count: 0, ...}` rather than 404 for an empty population: "nothing to rate" is an
    answer, and a client that has to distinguish an error from an empty banner will get it
    wrong on the first flaky request.
    """
    banner = await shelves.pending_verdicts(conn, user_id=user.id)
    return banner or {"count": 0, "named": [], "head_title_ids": [], "copy": None, "cta": None}


@router.get("/model-log")
async def model_log(conn: DB, user: ActiveUser, limit: int = Query(rail.RAIL_LIMIT, ge=1, le=50)):
    """§6.7's rail. Decision 117: one per-user toggle, default off.

    With the toggle OFF the response has no `events` key at all — not an empty list, not a list
    the client is trusted to hide. §6.7's promise is that the numbers are not there, and a
    promise kept in CSS is not kept: the payload would still be in the network tab and in the
    service-worker cache.
    """
    if not rail.visible_to(user):
        return {
            "show_model": False,
            "hint": "turn on 'show the model' in the account menu to see the model log",
        }
    events = rail.recent(user_id=user.id, limit=limit)
    return {
        "show_model": True,
        "limit": limit,
        "kinds": rail.kinds_present(events),
        "events": events,
    }
