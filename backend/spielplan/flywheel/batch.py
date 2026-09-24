"""§8.4's Launch: the batch total, the cap it is held against, and the one transaction that launches.

Spec v2.1 §8.4 ("Admin reviews the queue, picks a batch and providers, sees the cost estimate,
launches", and v2.1.3's sentence after it: the estimate is the reservation of both attempts, shown
against what is left of the monthly cap, and Launch is refused with its reason), §6.6 Data's
approve/spend controls, §8 stage 6; decisions 325, 330, 343, 441, 442 and 443.

THE BATCH TOTAL IS THE CONTROL, AND THE PER-TITLE FIGURE IS REASSURANCE (plan C2 and its risk). One
click can select every queued row, so the figure that matters is the one the button is held
against. Per title it is `pricing.estimate_title` at §8 stage 6's `SPEC_INPUT_TOKENS` over the
batch's own providers and passes, priced by `spend.extraction_plan` with the batch merged in
(decision 442) - the reading the gate and stage 6 make of the same plan; then times the titles; then
times `spend.ATTEMPTS`, because decision 325 budgets attempt 2 inside the cap and a total over one
attempt would enable a batch the gate then parks half of (decision 441). `totals` is that
arithmetic and nothing else, pure, so "rows x passes x providers" is checked without a database.

FOUR REFUSALS, EACH ITS OWN SENTENCE, IN THE GATE'S ORDER. No cap (`spend.NO_CAP_REASON`); a plan
that cannot be made (its own sentence - no key, an unreadable key, a batch naming nothing this build
calls, a model nobody priced); a price the estimate cannot know (decision 343); and a reservation
larger than the room the meter leaves, the cap less the month's spend with unsettled ceilings
included. The first two come in `spend.cap_check`'s order, so a state the gate would park under one
sentence is not refused here under another; a selection of nothing is refused after them, because
selecting rows fixes none of the three. Exactly the room left is launchable, for `cap_check`'s
reason: a month that ends on its cap has not passed it.

THE SERVER IS THE GATE, AND IT IS ASKED AGAIN INSIDE THE LAUNCH. `quote` is what the surface asks on
every selection change; `launch` trusts nothing the client computed and runs `quote` again inside
its own transaction, over the rows it has just locked and the titles they count, and refuses with
the same sentence. What it does not do is HOLD the room: a batch writes nothing the meter sums, so
two launches in a row are each measured against the same month. What holds the month is stage 6's
gate on every title (decision 325), which parks the title whose reservation would breach the cap
rather than bill it. This check refuses the batch that cannot fit at all, before anything is due -
the warning after the fact plan C3 refuses to ship is the one this is not.

LAUNCH IS ONE TRANSACTION (decision 443): the `flywheel_batch` row with the figures it was held
against, exactly the selected rows marked running, and each row's title made due at `dna pack`
through `acquire/actions.make_due`, carrying the plan the gate and stage 6 read (`stages.PLAN_KEY`).
A title in flight, a row closed since it was selected, an id the queue does not hold or a row whose
producer is M6's refuses the whole launch, and the rollback leaves nothing behind. So does a title
that failed for good (decision 448): `make_due` would revive it, and the board's retry is the only
way back (decision 431).
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

# Decision 343 at the batch's scale. `extraction_plan` refuses an unpriced model by name before a
# plan exists, so this is the sentence for a plan built around that refusal - which a cap can no
# more be held against than the refusal itself.
UNKNOWN_PRICE = (
    "the per-title estimate is unknown: a provider in this batch has no known price, and a spend cap"
    " cannot be held against a price nobody knows (decision 343). Set price_input and price_output"
    " for it in Admin, or choose a priced model"
)

# The board's sentence for a launched title, shown verbatim there (`0005_ledger.sql`), so it says
# what made the job due, where it resumes and what it will be extracted with.
LAUNCHED = (
    "launched in flywheel batch {batch}: resumes at stage {number} ({name}) with {providers} x"
    " {passes} pass(es)"
)

FAILED_FOR_GOOD = (
    "nothing was launched, because {title} failed for good under task {key}, and decision 431 makes"
    " the board's retry the only way back: a launch would revive the task and pay for the same"
    " extraction again. Retry the title on the Acquisition board, or leave its row out of the batch"
    " (decision 448)"
)

# Where a relaunch of a running row finds it shut, the one way such a row comes back is named.
RUNNING_ROW = (
    ". To launch a running row again on another plan once its walk has stopped, abandon its title's"
    " job on the board: that takes the title out of its batch and queues the row again (decision 448)"
)

_CENT = Decimal("0.01")

# Every selected row, locked for the launch so an observation cannot close one between the check
# and the mark, and a second launch of the same rows waits and then finds them running.
_SELECTED = """
SELECT f.id, f.kind, f.status, f.title_id, f.est_titles, t.name, t.year
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

# Decision 443's step 2, predicate for predicate: a row that is not a queued thin-facet row is not
# marked, and a count short of the selection rolls the launch back.
_MARK = """
UPDATE flywheel_item SET status = 'running', batch_id = $2
 WHERE id = ANY($1::bigint[]) AND status = 'queued' AND kind = $3
RETURNING id
"""

# The stage `make_due` will clamp to, read under the same row lock it then takes, so the board's
# sentence names the stage the title really resumes at and not the one a launch asks for.
_REACHED = "SELECT stage FROM acquisition_job WHERE title_id = $1 FOR UPDATE"


class LaunchRefused(Exception):
    """The launch was refused and nothing was written; `reason` is the sentence to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def totals(
    prices: Sequence[ModelPrice | None], *, passes: int, titles: int
) -> tuple[Decimal, Decimal, Decimal] | None:
    """(per_title, total, reserved) for `titles` titles at `passes` passes over one price per
    provider - or None when any price is unknown (decision 343), which the caller renders as that
    and never as a figure. Decision 441's three lines, and only them."""
    per_title = pricing.estimate_title(
        tokens_in=pricing.SPEC_INPUT_TOKENS, prices=prices, passes=passes
    )
    if per_title is None:
        return None
    total = per_title * titles
    return per_title, total, total * spend.ATTEMPTS


def _dollars(amount: Decimal) -> str:
    """ASCII dollars in `llm/spend`'s spelling, spelled again rather than borrowed from a private
    helper: cents when the figure is whole cents, every digit when it is not, so a sentence the
    admin decides with never rounds a real fraction of a cent to "$0.00"."""
    if amount == amount.quantize(_CENT):
        return f"${amount.quantize(_CENT)}"
    return f"${amount.normalize():f}"


def _over(
    *, titles: int, passes: int, providers: int, per_title: Decimal, reserved: Decimal,
    meter: Mapping[str, Any],
) -> str:
    """The over-cap sentence: the reservation, its arithmetic, the room left, the cap and the month,
    opening on `spend.OVER_CAP_PREFIX` as every over-cap reason in this app does."""
    month = meter["period_start"].astimezone(spend.local_zone()).strftime("%Y-%m")
    left = (f"{_dollars(meter['remaining_usd'])} of the {_dollars(meter['cap_usd'])} monthly cap is"
            f" left for {month} ({meter['tz']})")
    if meter["unsettled_usd"] > 0:
        left += (f" -- {_dollars(meter['unsettled_usd'])} of the month's spend is calls whose answer"
                 " never arrived, counted at the most they could have billed (decision 436)")
    return (
        f"{spend.OVER_CAP_PREFIX}: this batch reserves up to {_dollars(reserved)} ({titles} title(s)"
        f" x {_dollars(per_title)} a title x {spend.ATTEMPTS} attempts, at {passes} pass(es) x"
        f" {providers} provider(s)), and only {left}. Select fewer rows, run fewer passes or"
        " providers, or raise the cap in Admin (decision 441)"
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
    """The quote, from what has already been read. Pure, so `ops/devstub.py` quotes its fixture with
    this module's arithmetic and sentences rather than a copy of either.

    `prices` is one per provider, or None when the plan was refused and `refused` is its sentence;
    `meter` is `spend.meter`'s reading. Decimals are kept: `api/` decides how money is spelled.
    """
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
    """What a batch of `titles` titles at `providers` x `passes` would reserve, against the room the
    month leaves, and whether Launch may press (decision 441).

    One instant for the plan and the meter, because both turn over on a local midnight (a dated
    price, decision 343; the month, decision 325). The batch is handed to `extraction_plan` as a
    launch would write it into the task (decision 442), so the plan priced here is the one the gate
    reserves against and stage 6 runs, a malformed batch included.
    """
    instant = now if now is not None else datetime.now(UTC)
    chosen = list(dict.fromkeys(providers))
    meter = await spend.meter(conn, now=instant)
    plan = await spend.extraction_plan(
        conn, now=instant, batch={"providers": chosen, "passes": passes}
    )
    if isinstance(plan, spend.Refusal):
        return assess(titles=titles, providers=chosen, passes=passes, prices=None,
                      refused=plan.reason, meter=meter)
    return assess(titles=titles, providers=chosen, passes=plan.passes,
                  prices=[planned.price for planned in plan.providers], refused=None, meter=meter)


def _count(row: asyncpg.Record) -> int:
    # A thin-facet row is one title - `thin._REFRESH` writes `est_titles` 1 - and one written without
    # the figure is still the one title 0029's CHECK makes it name, never a free zero in the total.
    return 1 if row["est_titles"] is None else int(row["est_titles"])


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
            " Reload the queue and select again (decision 443)"
        )
    for row in rows:
        if row["kind"] != store.THIN_FACET:
            raise LaunchRefused(
                f"row {row['id']} comes from the {row['kind']} feed, whose producer is M6's search and"
                " explore frontier (spec section 6.4): no M5 stage can act on a query or a frontier,"
                " so a launch naming one is refused and nothing was launched (decision 443)"
            )
    for row in rows:
        if row["status"] != "queued":
            raise LaunchRefused(
                f"row {row['id']} is {row['status']} and not queued, so nothing was launched: it has"
                " been launched or closed since it was selected. Reload the queue (decision 443)"
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
    """Launch exactly the selected rows as one batch, or refuse and write nothing (decision 443).

    Returns the batch as written - its figures are the quote it was held against inside this
    transaction, never the client's - and each row with its title and the stage it resumes at.
    Rows stay `running` until the title's next stage-8 observation closes them (decision 440), or
    until the board's abandon queues them again out of the batch (decision 448).
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        raise LaunchRefused(NOTHING_SELECTED)
    chosen = list(dict.fromkeys(providers))
    async with conn.transaction():
        rows = await conn.fetch(_SELECTED, ids)
        _admit(ids, rows)
        titles = sum(_count(row) for row in rows)
        quoted = await quote(conn, titles=titles, providers=chosen, passes=passes, now=now)
        if not quoted["launchable"]:
            raise LaunchRefused(quoted["reason"])
        batch = await conn.fetchrow(
            _BATCH, chosen, passes, titles, quoted["total_usd"], quoted["reserved_usd"]
        )
        marked = await conn.fetch(_MARK, ids, batch["id"], store.THIN_FACET)
        if len(marked) != len(ids):
            # Unreachable while `_SELECTED` holds the rows and `_admit` has passed them; kept because
            # "exactly the selected rows" is the clause, and a count is what states it.
            raise LaunchRefused(
                "the selection changed while it was being launched, so nothing was launched. Reload"
                " the queue and select again (decision 443)"
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
