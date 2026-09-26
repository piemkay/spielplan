"""Home and the model-log rail (§6.0, §6.7). Every response leaves through `rail.redact`, so decision
117's toggle is one gate at one exit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter
from typing import Any, Literal
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, status

from spielplan.api.deps import DB, ActiveUser
from spielplan.core.config import settings
from spielplan.db import library
from spielplan.home import rail, shelves
from spielplan.models import artifacts

log = logging.getLogger("spielplan.api.home")

router = APIRouter(prefix="/api", tags=["home"])


def _report_build(route: str, payload: dict[str, Any], elapsed_ms: float) -> None:
    """A DEBUG log line, never a payload field: a per-request timing is an annotation about this viewer
    that decision 117's gate would not cover."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    sections = (payload.get("model") or {}).get("sections_ms") or []
    slowest = ", ".join(
        f"{row['shelf']}/{row['kind']} {row['ms']:.0f} ms" for row in sections[:3]
    )
    log.debug(
        "%s built in %.0f ms; %d section builders, %.0f ms in them%s",
        route, elapsed_ms, len(sections), sum(row["ms"] for row in sections),
        f"; slowest {slowest}" if slowest else "",
    )


def _now_local() -> tuple[datetime, str]:
    """The household clock (§2's `TZ`), not the device's."""
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
    """The ACTIVE bundle row: scores are bound to it, and a stale store is the one that is wrong (§10).
    Falls back to the loaded store so a pre-row Home can still name a version."""
    active = await artifacts.active_bundle_version(conn)
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
    """`q` or `person_id` switches to the catalog grid (may interleave kinds); otherwise kind-headed
    shelves (§6.0; decisions 18, 472). Only one of the two is ever in the payload."""
    now_local, tz = _now_local()
    started = perf_counter()
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
        # From the loaded store, not the active row: the numbers its scores were computed with.
        cold_eval=artifacts.cold_eval_of(getattr(request.app.state, "artifacts", None)),
    )
    _report_build("/api/home", payload, (perf_counter() - started) * 1000.0)
    return rail.redact(payload, show_model=rail.visible_to(user))


@router.get("/home/shelves")
async def home_shelves(
    conn: DB,
    user: ActiveUser,
    request: Request,
    kind: list[Literal["movie", "series"]] = Query(...),
) -> dict[str, Any]:
    """For a kind toggle, without re-running the banner's population query. Never the grid."""
    now_local, tz = _now_local()
    started = perf_counter()
    payload = await shelves.build_home(
        conn,
        user=user,
        kinds=_kinds(kind),
        bundle_version=await _bundle(request, conn),
        now_local=now_local,
        tz=tz,
    )
    _report_build("/api/home/shelves", payload, (perf_counter() - started) * 1000.0)
    slim = {
        key: payload[key]
        for key in ("kinds", "shelves", "sections", "shelves_total", "verdict_count",
                    "degraded", "partner", "bundle", "vocabulary", "suppressed", "library",
                    "avoiding")
        if key in payload
    }
    return rail.redact(slim, show_model=rail.visible_to(user))


@router.get("/home/pending-verdicts")
async def pending(conn: DB, user: ActiveUser) -> dict[str, Any]:
    """Reads only (it never writes `seen`); an empty population is `count: 0`, not 404."""
    banner = await shelves.pending_verdicts(conn, user_id=user.id)
    return banner or {"count": 0, "named": [], "head_title_ids": [], "copy": None, "cta": None}


@router.get("/model-log")
async def model_log(
    user: ActiveUser, limit: int = Query(rail.RAIL_LIMIT, ge=1, le=rail.RAIL_LIMIT)
):
    """Decision 117: with the toggle off there is no `events` key at all; a promise kept in CSS is not
    kept. `le=RAIL_LIMIT`: §6.7's ~15 is the buffer's depth."""
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
