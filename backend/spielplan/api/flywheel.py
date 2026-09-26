"""§6.6 Data's extraction queue, quote and Launch (§8.4). The quote is advisory; the launch recounts,
reprices and rereads the meter itself (decision 441). Money is a string: JSON numbers are floats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from spielplan.api import llm as llm_api
from spielplan.api.deps import DB, AdminUser
from spielplan.connectors import registry
from spielplan.flywheel import batch, store
from spielplan.llm import client, pricing, spend

router = APIRouter(prefix="/api/admin/flywheel", tags=["admin", "flywheel"])

_QUOTED_MONEY = ("per_title_usd", "total_usd", "reserved_usd", "cap_usd", "remaining_usd")
_BATCH_MONEY = ("per_title_usd", "est_cost_usd", "reserved_usd")


class Launch(BaseModel):
    """Rows, providers and passes only: nothing a stale page could use to carry a figure past the cap.
    `passes` is unbounded so `llm/spend` refuses a bad one with its own sentence (decision 442)."""

    item_ids: list[int]
    providers: list[str]
    passes: int


def _money(amount: Decimal | None) -> str | None:
    return None if amount is None else str(amount)


def _spelled(figures: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {**figures, **{key: _money(figures[key]) for key in keys}}


def _meter(reading: dict[str, Any]) -> dict[str, Any]:
    return {
        **_spelled(reading, ("spent_usd", "unsettled_usd", "cap_usd", "remaining_usd")),
        "period_start": reading["period_start"].isoformat(),
        "period_end": reading["period_end"].isoformat(),
    }


@router.get("")
async def flywheel_queue(_: AdminUser, conn: DB) -> dict[str, Any]:
    """`configured` is `api/llm.provider_card`'s bit, so the two surfaces agree; `reason` is the plan's
    refusal for that provider alone. One instant for the whole read (prices turn at midnight)."""
    now = datetime.now(UTC)
    today = now.astimezone(spend.local_zone()).date()
    providers = []
    for name in client.PROVIDERS:
        card = llm_api.provider_card(name, await registry.load_connector(conn, name), on=today)
        alone = await spend.extraction_plan(conn, now=now, batch={"providers": [name], "passes": 1})
        providers.append({
            "name": name,
            "configured": card["configured"],
            "reason": alone.reason if isinstance(alone, spend.Refusal) else None,
        })
    stored = await spend.extraction_plan(conn, now=now)
    if isinstance(stored, spend.Refusal):
        defaults = {"providers": None, "passes": None, "reason": stored.reason}
    else:
        defaults = {"providers": [chosen.provider for chosen in stored.providers],
                    "passes": stored.passes, "reason": None}
    return {
        "items": await store.queue(conn),
        "providers": providers,
        "defaults": defaults,
        "meter": _meter(await spend.meter(conn, now=now)),
        "input_tokens_assumed": pricing.SPEC_INPUT_TOKENS,
    }


@router.get("/quote")
async def flywheel_quote(
    _: AdminUser,
    conn: DB,
    titles: int = Query(..., ge=0),
    providers: str = Query(...),
    passes: int = Query(..., ge=1),
) -> dict[str, Any]:
    """`titles` is the surface's sum of `est_titles`; the launch recounts. An unpriceable figure is null
    and `reason` says why."""
    chosen = [name.strip() for name in providers.split(",") if name.strip()]
    quoted = await batch.quote(conn, titles=titles, providers=chosen, passes=passes)
    return _spelled(quoted, _QUOTED_MONEY)


@router.post("/launch")
async def flywheel_launch(body: Launch, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Exactly the selected rows as one batch (decision 443), or 409 with the reason and nothing written."""
    try:
        launched = await batch.launch(
            conn, item_ids=body.item_ids, providers=body.providers, passes=body.passes
        )
    except batch.LaunchRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    return {"batch": _spelled(launched["batch"], _BATCH_MONEY), "items": launched["items"]}
