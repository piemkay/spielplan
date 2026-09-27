"""§8.4's Launch: the batch total, the cap it is held against, and the one transaction that launches.

The total reserves both attempts (decision 325). `launch` re-runs `quote` inside its transaction and
trusts nothing the client computed; the transaction is all-or-nothing (decision 443).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from spielplan.acquire import actions, pipeline, stages
from spielplan.flywheel import store
from spielplan.llm import pricing, spend
from spielplan.llm.pricing import ModelPrice

NOTHING_SELECTED = "select rows first: a batch launches the queue rows it names, and none is selected"

# An empty provider selection is refused here, never replaced by the stored assignment (decision 442).
NO_PROVIDER = (
    "choose at least one provider for this batch: a batch extracts with the providers it names, and"
    " this one names none"
)

# Decision 343 at the batch's scale.
UNKNOWN_PRICE = (
    "the per-title estimate is unknown: a provider in this batch has no known price, and a spend cap"
    " cannot be held against a price nobody knows. Set price_input and price_output"
    " for it in Admin, or choose a priced model"
)

# Shown verbatim on the board (`0005_ledger.sql`).
LAUNCHED = (
    "launched in flywheel batch {batch}: resumes at stage {number} ({name}) with {providers} x"
    " {passes} pass(es)"
)

FAILED_FOR_GOOD = (
    "nothing was launched, because {title} failed for good under task {key}, and the board's retry"
    " is the only way back: a launch would revive the task and pay for the same extraction again."
    " Retry the title on the Acquisition board, or leave its row out of the batch"
)

RUNNING_ROW = (
    ". To launch a running row again on another plan once its walk has stopped, abandon its title's"
    " job on the board: that takes the title out of its batch and queues the row again"
)

_CENT = Decimal("0.01")

# Locked so an observation cannot close a row between the check and the mark.
_SELECTED = """
SELECT f.id, f.status, f.title_id, t.name, t.year
  FROM flywheel_item f
  LEFT JOIN title t ON t.id = f.title_id
 WHERE f.id = ANY($1::bigint[])
 ORDER BY f.id
   FOR UPDATE OF f
"""

_BATCH = """
INSERT INTO flywheel_batch (providers, passes, est_titles, est_cost_usd, reserved_usd, launched_at)
VALUES ($1::text[], $2, $3, $4, $5, now())
RETURNING id, created_at, launched_at
"""

# A count short of the selection rolls the launch back (decision 443).
_MARK = """
UPDATE flywheel_item SET status = 'running', batch_id = $2
 WHERE id = ANY($1::bigint[]) AND status = 'queued' AND kind = $3
RETURNING id
"""

# Read under `make_due`'s own row lock, so the sentence names the stage the title really resumes at.
_REACHED = "SELECT stage FROM acquisition_job WHERE title_id = $1 FOR UPDATE"


class LaunchRefused(Exception):
    """The launch was refused and nothing was written; `reason` is the sentence to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def totals(
    prices: Sequence[ModelPrice | None], *, passes: int, titles: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(per_title, total, reserved), or None when any price is unknown (decision 343)."""
    per_title = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, prices=prices, passes=passes
    )
    if per_title is None:
        return None
    total = per_title * titles
    return per_title, total, total * spend.ATTEMPTS


def _dollars(amount: Decimal) -> str:
    """Whole cents when exact, every digit otherwise, so a real fraction never shows as "$0.00"."""
    if amount == amount.quantize(_CENT):
        return f"${amount.quantize(_CENT)}"
    return f"${amount.normalize():f}"


def _over(
    *, titles: int, passes: int, providers: int, per_title: Decimal, reserved: Decimal,
    meter: Mapping[str, Any],
) -> str:
    """The over-cap sentence, opening on `spend.OVER_CAP_PREFIX`."""
    month = meter["period_start"].astimezone(spend.local_zone()).strftime("%Y-%m")
    left = (f"{_dollars(meter['remaining_usd'])} of the {_dollars(meter['cap_usd'])} monthly cap is"
            f" left for {month} ({meter['tz']})")
    if meter["unsettled_usd"] > 0:
        left += (f" -- {_dollars(meter['unsettled_usd'])} of the month's spend is calls whose answer"
                 " never arrived, counted at the most they could have billed")
    return (
        f"{spend.OVER_CAP_PREFIX}: this batch reserves up to {_dollars(reserved)} ({titles} title(s)"
        f" x {_dollars(per_title)} a title x {spend.ATTEMPTS} attempts, at {passes} pass(es) x"
        f" {providers} provider(s)), and only {left}. Select fewer rows, run fewer passes or"
        " providers, or raise the cap in Admin"
    )


def assess(
    *,
    titles: int,
    providers: Sequence[str],
    passes: int,
    prices: Sequence[ModelPrice | None] | None,
    refused: str | None,
    meter: Mapping[str, Any],
) -> dict[str, Any]:
    """The quote from what has already been read; `prices` is None when the plan was refused."""
    if isinstance(titles, bool) or not isinstance(titles, int) or titles < 0:
        raise ValueError(f"a batch counts a whole number of titles, not {titles!r}")
    figures = None if prices is None else totals(prices, passes=passes, titles=titles)
    per_title, total, reserved = figures if figures is not None else (None, None, None)
    if meter["cap_usd"] is None:
        reason = spend.NO_CAP_REASON
    elif refused is not None:
        reason = refused
    elif figures is None:
        reason = UNKNOWN_PRICE
    elif titles == 0:
        reason = NOTHING_SELECTED
    elif reserved > meter["remaining_usd"]:
        reason = _over(titles=titles, passes=passes, providers=len(providers), per_title=per_title,
                       reserved=reserved, meter=meter)
    else:
        reason = None
    return {
        "titles": titles,
        "providers": list(providers),
        "passes": passes,
        "per_title_usd": per_title,
        "total_usd": total,
        "reserved_usd": reserved,
        "cap_usd": meter["cap_usd"],
        "remaining_usd": meter["remaining_usd"],
        "launchable": reason is None,
        "reason": reason,
    }


async def quote(
    conn: asyncpg.Connection,
    *,
    titles: int,
    providers: Sequence[str],
    passes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a batch would reserve against the month's room, and whether Launch may press (decision 441).

    One instant for plan and meter: both turn over on a local midnight.
    """
    instant = now if now is not None else datetime.now(UTC)
    chosen = list(dict.fromkeys(providers))
    meter = await spend.meter(conn, now=instant)
    if not chosen:
        return assess(titles=titles, providers=chosen, passes=passes, prices=None,
                      refused=NO_PROVIDER, meter=meter)
    plan = await spend.extraction_plan(
        conn, now=instant, batch={"providers": chosen, "passes": passes}
    )
    if isinstance(plan, spend.Refusal):
        return assess(titles=titles, providers=chosen, passes=passes, prices=None,
                      refused=plan.reason, meter=meter)
    return assess(titles=titles, providers=chosen, passes=plan.passes,
                  prices=[planned.price for planned in plan.providers], refused=None, meter=meter)


def _label(row: asyncpg.Record) -> str:
    if row["name"] is None:
        return f"title {row['title_id']}"
    return f"{row['name']} ({row['year']})" if row["year"] else str(row["name"])


def _admit(ids: list[int], rows: list[asyncpg.Record]) -> None:
    """Refuse a selection a launch cannot take whole (decision 443), naming the first bad row."""
    found = {row["id"] for row in rows}
    missing = [str(item) for item in ids if item not in found]
    if missing:
        raise LaunchRefused(
            f"row(s) {', '.join(missing)} are not in the extraction queue, so nothing was launched."
            " Reload the queue and select again"
        )
    for row in rows:
        if row["status"] != "queued":
            raise LaunchRefused(
                f"row {row['id']} is {row['status']} and not queued, so nothing was launched: it has"
                " been launched or closed since it was selected. Reload the queue"
                + (RUNNING_ROW if row["status"] == "running" else "")
            )


async def launch(
    conn: asyncpg.Connection,
    *,
    item_ids: Sequence[int],
    providers: Sequence[str],
    passes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Launch exactly the selected rows as one batch, or refuse and write nothing (decision 443)."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        raise LaunchRefused(NOTHING_SELECTED)
    chosen = list(dict.fromkeys(providers))
    async with conn.transaction():
        rows = await conn.fetch(_SELECTED, ids)
        _admit(ids, rows)
        titles = len(rows)
        quoted = await quote(conn, titles=titles, providers=chosen, passes=passes, now=now)
        if not quoted["launchable"]:
            raise LaunchRefused(quoted["reason"])
        batch = await conn.fetchrow(
            _BATCH, chosen, passes, titles, quoted["total_usd"], quoted["reserved_usd"]
        )
        marked = await conn.fetch(_MARK, ids, batch["id"], store.THIN_FACET)
        if len(marked) != len(ids):
            # Rows are locked, so only a kind other than thin_facet (M6's feeds) lands here.
            raise LaunchRefused(
                "the selection changed while it was being launched, so nothing was launched. Reload"
                " the queue and select again"
            )
        plan = {"providers": chosen, "passes": passes}
        items = []
        for row in sorted(rows, key=lambda r: (r["title_id"], r["id"])):
            reached = await conn.fetchval(_REACHED, row["title_id"])
            stage = actions.PACK_STAGE if reached is None else min(actions.PACK_STAGE, int(reached))
            named = next(s for s in pipeline.STAGES if s.number == stage)
            reason = LAUNCHED.format(batch=batch["id"], number=stage, name=named.name,
                                     providers=" + ".join(chosen), passes=passes)
            held = await actions.failed_for_good(conn, row["title_id"])
            if held is not None:
                raise LaunchRefused(FAILED_FOR_GOOD.format(title=_label(row), key=held))
            try:
                await actions.make_due(
                    conn, row["title_id"], stage=actions.PACK_STAGE, reason=reason,
                    payload={stages.PLAN_KEY: plan, stages.BATCH_KEY: batch["id"]},
                )
            except (actions.ActionRefused, actions.NoJob) as exc:
                refusal = exc.reason if isinstance(exc, actions.ActionRefused) else str(exc)
                raise LaunchRefused(
                    f"nothing was launched, because {_label(row)} cannot be made due now: {refusal}"
                ) from exc
            items.append({"id": row["id"], "title_id": row["title_id"], "stage": stage})
    return {
        "batch": {
            "id": batch["id"],
            "created_at": batch["created_at"],
            "launched_at": batch["launched_at"],
            "providers": chosen,
            "passes": passes,
            "est_titles": titles,
            "per_title_usd": quoted["per_title_usd"],
            "est_cost_usd": quoted["total_usd"],
            "reserved_usd": quoted["reserved_usd"],
        },
        "items": items,
    }
