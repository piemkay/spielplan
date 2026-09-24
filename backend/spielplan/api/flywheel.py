"""§6.6 Data's extraction queue: the flywheel's rows, the batch quote and Launch. Spec v2.1 §8.4,
§6.6 Data; decisions 330, 441, 442 and 443.

Three routes, and every rule behind them is a domain answer: the queue is `flywheel/store.queue`,
the quote and the launch are `flywheel/batch`, the meter and the plan `llm/spend`. This module
decides how a Decimal and a missing figure are spelled on the wire and maps a refused launch to
409 with its sentence as the detail, as `api/acquisition.py` maps a refused board action - the
surface shows the sentence verbatim, and a status code alone would tell the operator less than the
domain said.

THE QUOTE IS ADVISORY AND THE LAUNCH IS THE GATE (decision 441). The surface asks `GET /quote` on
every selection change to draw its running total and to disable Launch with the reason; `POST
/launch` takes only the row ids, the providers and the passes, and the domain counts the titles,
prices the plan and reads the meter again inside its own transaction. So there is no field in the
launch body a stale or edited page could use to carry its own figure past the cap, and a batch plan
reaches `llm/spend` only as providers and passes, which is all decision 442's merge reads.

MONEY IS A STRING. For `api/llm.py`'s reason, spelled again here rather than imported from its
private helper: a JSON number is a binary float to the client that parses it, and the meter and the
reservation are exact on purpose (decision 325).

THIN BY CONSTRUCTION: no statement lives here, so the module is absent from
`test_layering_guards.py`'s `ALLOWED_RESIDUE` and its first query would fail that ratchet as
`(1, 0)`. ADMIN, THE WAY EVERY OTHER ADMIN ROUTE IS: each takes `AdminUser`, so
`test_api_gating.py`'s two sweeps drive them as a stranger and as a member.
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
    """What a launch names and nothing it could price: the rows, the providers and the passes.

    The passes are a plain integer rather than a bounded one so that a batch the plan cannot run is
    refused by `llm/spend`'s own sentence for it (decision 442), not by a validation error that
    names a field instead of the fault."""

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
    """The open queue rows, newest first, with what a batch over them can be launched with.

    `providers` is one entry per provider this build calls. `configured` is `api/llm.provider_card`'s
    bit, word for word, so this picker and M5.7's provider card never disagree about a provider;
    `reason` is the plan's own refusal for a batch of that provider alone - None when the plan can
    be made - which is the sentence the quote will give if it is picked. `defaults` is the stored
    plan's providers and passes, or nulls beside the stored plan's refusal (decision 324).

    One instant for the whole read, because a dated price and the month both turn over on a local
    midnight, and the plans and the meter here are read at the same one.
    """
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
    """The running total for a selection of `titles` titles at `providers` (comma-separated) x
    `passes`, against the room the month leaves, and whether Launch may press (decision 441).

    `titles` is the sum of the selected rows' `est_titles`, which the surface holds; the launch does
    not trust it and counts again. A figure the plan could not price is null, and `reason` says why.
    """
    chosen = [name.strip() for name in providers.split(",") if name.strip()]
    quoted = await batch.quote(conn, titles=titles, providers=chosen, passes=passes)
    return _spelled(quoted, _QUOTED_MONEY)


@router.post("/launch")
async def flywheel_launch(body: Launch, _: AdminUser, conn: DB) -> dict[str, Any]:
    """Launch exactly the selected rows as one batch (decision 443), or 409 with the reason and
    nothing written: over the cap, no cap, a plan that cannot be made, a price nobody knows, a row
    closed since it was selected, a row whose producer is M6's, or a title in flight."""
    try:
        launched = await batch.launch(
            conn, item_ids=body.item_ids, providers=body.providers, passes=body.passes
        )
    except batch.LaunchRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    return {"batch": _spelled(launched["batch"], _BATCH_MONEY), "items": launched["items"]}
