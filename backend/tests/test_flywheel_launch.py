"""Launch: exactly the selected rows, held against the cap inside its own transaction.

Spec v2.1 §8.4 ("picks a batch and providers, sees the cost estimate, launches"), §6.6 Data's
approve/spend controls; decisions 325, 343, 441, 442 and 443. Plan §7 checks 5 and 6: "cap set below
the total | Launch disabled; the reason names the cap" and "Launch inside the cap | exactly the
selected rows change state".

WHAT "EXACTLY" IS MEASURED ON. Three queued thin-facet rows and two of them launched: the two are
running under the batch the launch wrote, the third is untouched; the two titles are leasable by the
drain and carry the batch plan stage 6 will read, the third's task is still deferred and carries
none; and the board says, for each launched title, where it resumes and with what. Decision 443's
three steps are one transaction, so every refusal below is checked against all four tables it could
have touched - `flywheel_item`, `flywheel_batch`, `acquisition_task`, `acquisition_job` - byte for
byte, `updated_at` included.

THE SERVER IS THE GATE. Nothing the launch is handed carries a figure, and the money refusals are
the domain's own sentences, read inside the launch transaction: no cap (`spend.NO_CAP_REASON`), a
reservation over the room left, a model nobody priced, a provider with no key. Exactly the room
left launches.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg
import pytest

from spielplan.acquire import actions, pipeline, queue, stages
from spielplan.connectors import registry
from spielplan.flywheel import batch, store
from spielplan.llm import spend

# Three owned titles, each walked past stage 8 and found thin: the board parked at stage 9 for want
# of a bundle - where a real walk past the observation stops on a bundle-less database - and the
# task deferred behind that park.
A, B, C = 11, 12, 13
PARKED_AT_NINE = "stage 9 parked by this test: no bundle is active"
BIG_CAP = 25
PLAN = {"providers": ["gemini", "anthropic"], "passes": 2}


async def _cap(db, cap: float | None = None, **fields) -> None:
    """The `llm` row: Gemini assigned at one pass, and the cap when the test gives one. Partial, as
    `registry.save_connector` is, so a test that never sets a cap has none (decision 325)."""
    settings = {"extraction_provider": "gemini", "parallel": False, "passes": 1, **fields}
    if cap is not None:
        settings["cap_usd"] = cap
    await registry.save_connector(db, "llm", **settings)


async def _install(db) -> dict[int, int]:
    """Three thin titles with their queue rows, Gemini and Anthropic keyed, OpenAI not, and no cap.
    Returns each title's queue row id. A function as well as the fixture below, so
    `test_flywheel_api.py` installs the same queue under the app without importing a fixture."""
    rows = {}
    for title_id in (A, B, C):
        await db.execute(
            "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, 2021, true)",
            title_id, f"Thin Title {title_id}",
        )
        assert await pipeline.enqueue_title(db, title_id) is True
        await db.execute(
            "UPDATE acquisition_task SET next_attempt_at = now() + interval '1 day' WHERE key = $1",
            f"title:{title_id}",
        )
        await pipeline.write_board(db, title_id, stage=9, status=pipeline.PARKED, reason=PARKED_AT_NINE)
        rows[title_id] = await db.fetchval(
            "INSERT INTO flywheel_item (kind, detail, reason, title_id, est_titles)"
            " VALUES ('thin_facet', '{}'::jsonb, $2, $1, 1) RETURNING id",
            title_id, f"1 of the 3 facets vocabulary v1 declares carry no extracted-tier term ({title_id})",
        )
    for provider in ("gemini", "anthropic"):
        await registry.save_connector(db, provider, api_key=f"{provider}-test-key")
    await _cap(db)
    return rows


@pytest.fixture
async def queued(db, secrets_key) -> dict[int, int]:
    return await _install(db)


async def _snapshot(db) -> list:
    """Every row a launch could write, so "nothing was written" is a comparison and not a guess."""
    return [
        [dict(r) for r in await db.fetch(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("flywheel_item", "flywheel_batch", "acquisition_task", "acquisition_job")
    ]


async def _tasks(db) -> dict[int, asyncpg.Record]:
    return {row["payload"]["title_id"]: row for row in await db.fetch(
        "SELECT *, next_attempt_at <= now() AS due FROM acquisition_task ORDER BY id")}


async def _boards(db) -> dict[int, tuple]:
    return {row["title_id"]: (row["stage"], row["status"], row["reason"]) for row in await db.fetch(
        "SELECT title_id, stage, status, reason FROM acquisition_job ORDER BY title_id")}


def _launched_reason(batch_id: int, *, number: int = 5, name: str = "dna pack") -> str:
    return batch.LAUNCHED.format(batch=batch_id, number=number, name=name,
                                 providers="gemini + anthropic", passes=2)


# --- exactly the selected rows (plan check 6, decision 443) ------------------------------------


async def test_a_launch_marks_exactly_the_selected_rows_and_makes_exactly_their_titles_due(db, queued):
    """Two of three rows launched: those two run under the batch, the third stays queued with no
    batch; the two titles' tasks are due now carrying the plan and the batch under the one key stage
    6 reads (`stages.task_plan`), the third's is still deferred and carries no plan; the drain
    leases exactly the two; and each launched board says where it resumes and with what."""
    await _cap(db, BIG_CAP)

    launched = await batch.launch(db, item_ids=[queued[B], queued[A], queued[A]], **PLAN)

    batch_id = launched["batch"]["id"]
    marked = {r["title_id"]: (r["status"], r["batch_id"]) for r in await db.fetch(
        "SELECT title_id, status, batch_id FROM flywheel_item")}
    assert marked == {A: ("running", batch_id), B: ("running", batch_id), C: ("queued", None)}
    assert launched["items"] == [
        {"id": queued[A], "title_id": A, "stage": 5}, {"id": queued[B], "title_id": B, "stage": 5}
    ], "one item per selected row, a repeated id counted once"

    tasks = await _tasks(db)
    for title_id in (A, B):
        task = tasks[title_id]
        assert (task["state"], task["attempts"], task["due"]) == (queue.PENDING, 0, True), dict(task)
        assert stages.task_plan(queue.Task.from_row(task)) == PLAN
        assert task["payload"][stages.BATCH_KEY] == batch_id
    assert tasks[C]["due"] is False and stages.task_plan(queue.Task.from_row(tasks[C])) is None

    boards = await _boards(db)
    assert boards[A] == boards[B] == (5, "queued", _launched_reason(batch_id))
    assert boards[C] == (9, pipeline.PARKED, PARKED_AT_NINE)

    leased = await queue.lease(db, [pipeline.TASK_KIND], limit=10)
    assert sorted(task.payload["title_id"] for task in leased) == [A, B]


async def test_the_batch_row_records_the_figures_the_launch_was_held_against(db, queued):
    """Decision 443's step 1: providers, passes, the titles counted and the two figures decision 441
    computes - the total, and the reservation of both attempts - priced over the batch's own plan
    (decision 442), and never over the stored one-provider, one-pass settings."""
    await _cap(db, BIG_CAP)

    launched = await batch.launch(db, item_ids=[queued[A], queued[B]], **PLAN)

    plan = await spend.extraction_plan(db, batch=PLAN)
    per_title, total, reserved = batch.totals([p.price for p in plan.providers], passes=2, titles=2)
    row = await db.fetchrow("SELECT * FROM flywheel_batch")
    assert (row["id"], row["providers"], row["passes"], row["est_titles"]) == (
        launched["batch"]["id"], ["gemini", "anthropic"], 2, 2
    )
    assert (row["est_cost_usd"], row["reserved_usd"]) == (total, reserved)
    assert reserved == 2 * total and row["launched_at"] is not None
    stored = await spend.extraction_plan(db)
    assert batch.totals([p.price for p in stored.providers], passes=1, titles=2)[1] < total, (
        "the stored plan prices lower, so the figures above are the batch's and not the settings'"
    )
    assert (launched["batch"]["per_title_usd"], launched["batch"]["est_cost_usd"],
            launched["batch"]["reserved_usd"]) == (per_title, total, reserved)


async def test_a_title_whose_board_is_behind_the_pack_stage_resumes_where_it_is_and_says_so(db, queued):
    """`make_due` never moves a job forward, so a title an operator retried from stage 3 after it was
    found thin resumes at 3 - and the board's sentence names stage 3, since it is shown verbatim."""
    await _cap(db, BIG_CAP)
    await pipeline.write_board(db, A, stage=3, status=pipeline.FAILED, reason="a stage raised")

    launched = await batch.launch(db, item_ids=[queued[A]], **PLAN)

    assert launched["items"] == [{"id": queued[A], "title_id": A, "stage": 3}]
    assert (await _boards(db))[A] == (3, "queued", _launched_reason(launched["batch"]["id"], number=3,
                                                                     name="derive"))


async def test_the_queue_shows_a_launched_row_running_with_its_titles_board(db, queued):
    """The admin queue's own read after a launch: the row is running under its batch, and the board
    row it carries says why the title is waiting now, verbatim (§6.6 Data)."""
    await _cap(db, BIG_CAP)
    launched = await batch.launch(db, item_ids=[queued[A]], **PLAN)

    rows = {row["title_id"]: row for row in await store.queue(db)}

    assert set(rows) == {A, B, C}
    assert (rows[A]["status"], rows[A]["batch_id"]) == ("running", launched["batch"]["id"])
    assert rows[A]["board"] == {"stage": 5, "status": "queued",
                                "reason": _launched_reason(launched["batch"]["id"])}
    assert rows[B]["status"] == "queued" and rows[B]["board"]["status"] == pipeline.PARKED


# --- the money refusals (plan check 5, decision 441) --------------------------------------------


async def _over_the_cap(db) -> None:
    await _cap(db, 0.01)


async def _no_cap(db) -> None:
    pass


async def _unpriced(db) -> None:
    await _cap(db, BIG_CAP)
    await registry.save_connector(db, "gemini", model="gemini-9-ultra")


async def _keyless(db) -> None:
    await _cap(db, BIG_CAP)


@pytest.mark.parametrize(
    ("arrange", "providers", "opens"),
    [
        (_over_the_cap, ["gemini", "anthropic"], f"{spend.OVER_CAP_PREFIX}: this batch reserves up to"),
        (_no_cap, ["gemini", "anthropic"], spend.NO_CAP_REASON),
        (_unpriced, ["gemini", "anthropic"], "no price is known for gemini model 'gemini-9-ultra'"),
        (_keyless, ["openai"], "no API key is configured for openai"),
    ],
    ids=["over-the-cap", "no-cap", "unknown-price", "keyless"],
)
async def test_each_money_refusal_is_its_own_sentence_and_writes_nothing(
    db, queued, arrange, providers, opens
):
    """Four ways Launch is disabled, each refused again inside the transaction with its own sentence
    whatever a client displayed - and nothing written anywhere: no batch, no row running, no task
    due, no board moved."""
    await arrange(db)
    before = await _snapshot(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A], queued[B]], providers=providers, passes=2)

    assert refused.value.reason.startswith(opens), refused.value.reason
    assert await _snapshot(db) == before


async def test_the_over_cap_sentence_names_the_reservation_the_cap_and_what_is_left(db, queued):
    await _over_the_cap(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A], queued[B]], **PLAN)

    reason = refused.value.reason
    assert "(2 title(s) x $" in reason and "a title x 2 attempts, at 2 pass(es) x 2 provider(s))" in reason
    assert "only $0.01 of the $0.01 monthly cap is left for " in reason, reason
    reason.encode("ascii")


async def test_a_reservation_exactly_equal_to_the_room_left_launches(db, queued):
    """`spend.cap_check`'s reading, which the launch keeps: a month that ends on its cap has not
    passed it. One micro-dollar less and the same launch is refused."""
    plan = await spend.extraction_plan(db)
    reserved = batch.totals([p.price for p in plan.providers], passes=1, titles=1)[2]
    await _cap(db, float(reserved - Decimal("0.000001")))

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A]], providers=["gemini"], passes=1)
    assert refused.value.reason.startswith(spend.OVER_CAP_PREFIX)

    await _cap(db, float(reserved))
    launched = await batch.launch(db, item_ids=[queued[A]], providers=["gemini"], passes=1)

    assert launched["batch"]["reserved_usd"] == reserved
    assert await db.fetchval("SELECT count(*) FROM flywheel_batch") == 1


# --- a selection the launch cannot take whole (decision 443) -----------------------------------


async def test_a_row_already_launched_refuses_the_whole_launch(db, queued):
    await _cap(db, BIG_CAP)
    await batch.launch(db, item_ids=[queued[A]], **PLAN)
    before = await _snapshot(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[B], queued[A]], **PLAN)

    assert refused.value.reason.startswith(f"row {queued[A]} is running and not queued"), refused.value
    # The one way a running row comes back to the queue is named where a relaunch finds it shut
    # (decision 448). [M5.6 review cycle 1, M56-MONEY-01]
    assert "abandon its title's job on the board" in refused.value.reason, refused.value.reason
    assert await _snapshot(db) == before


async def test_a_title_that_failed_stage_six_for_good_is_not_launched_until_the_board_retries_it(
    db, queued
):
    """Decision 431 makes an admin retry of the task that failed for good "the only way back", and a
    launch revives every failed task of its titles, the mark included - so a queued row whose title
    had since failed stage 6 for good bought attempt 1 and the named retry again from a provider that
    had already refused the contract twice. The launch refuses the whole batch naming the title, the
    task and the board's retry, writes nothing, and launches once the board has retried it (decision
    448). [M5.6 review cycle 1, M56-MONEY-02]"""
    await _cap(db, BIG_CAP)
    await db.execute(
        "UPDATE acquisition_task SET state = 'failed', attempts = 1, last_error = 'broke the contract'"
        " , payload = payload || $2::text::jsonb WHERE key = $1",
        f"title:{B}", json.dumps({stages.FAILED_FOR_GOOD_MARK: True}),
    )
    await pipeline.write_board(db, B, stage=6, status=pipeline.FAILED, reason="broke the contract")
    before = await _snapshot(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A], queued[B]], **PLAN)

    reason = refused.value.reason
    assert reason.startswith("nothing was launched, because Thin Title 12 (2021) failed for good under"
                             f" task title:{B}"), reason
    assert "decision 431" in reason and "retry" in reason, reason
    assert await _snapshot(db) == before

    assert await actions.retry(db, B) == 1
    launched = await batch.launch(db, item_ids=[queued[A], queued[B]], **PLAN)
    assert [item["title_id"] for item in launched["items"]] == [A, B]


async def test_an_id_the_queue_does_not_hold_refuses_the_whole_launch(db, queued):
    await _cap(db, BIG_CAP)
    before = await _snapshot(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A], 999_999], **PLAN)

    assert refused.value.reason.startswith("row(s) 999999 are not in the extraction queue")
    assert await _snapshot(db) == before


async def test_a_row_whose_producer_is_m6s_refuses_the_whole_launch_naming_m6(db, queued):
    """Decision 443: rows of the two kinds M6 produces are shown and selectable, and a launch naming
    one is refused with a sentence naming M6, because no M5 stage can act on a query."""
    await _cap(db, BIG_CAP)
    m6 = await store.enqueue(db, kind=store.EMPTY_PREDICATE, detail={"terms": ["themes.robots"]},
                             reason="no owned title carries robots")
    before = await _snapshot(db)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[queued[A], m6], **PLAN)

    assert refused.value.reason.startswith(f"row {m6} comes from the empty_predicate feed, whose"
                                           " producer is M6's"), refused.value.reason
    assert await _snapshot(db) == before


async def test_a_title_in_flight_refuses_the_whole_launch_and_undoes_the_titles_already_made_due(
    db, queued, pg_url
):
    """A walk holds B (the driver's session lock, `pipeline._TITLE_LOCK`, taken on another
    connection as a worker takes it). A is made due first - the launch walks titles in order - so the
    refusal at B has to roll A's board, task and row back with it, which is what one transaction
    means."""
    await _cap(db, BIG_CAP)
    before = await _snapshot(db)
    walker = await asyncpg.connect(pg_url)
    try:
        await walker.execute("SELECT pg_advisory_lock($1, $2)", pipeline._TITLE_LOCK, B)

        with pytest.raises(batch.LaunchRefused) as refused:
            await batch.launch(db, item_ids=[queued[A], queued[B]], **PLAN)
    finally:
        await walker.close()

    assert refused.value.reason.startswith(
        "nothing was launched, because Thin Title 12 (2021) cannot be made due now: this title is being"
        " walked right now"
    ), refused.value.reason
    assert await _snapshot(db) == before


async def test_a_launch_that_names_nothing_is_refused(db, queued):
    await _cap(db, BIG_CAP)

    with pytest.raises(batch.LaunchRefused) as refused:
        await batch.launch(db, item_ids=[], **PLAN)

    assert refused.value.reason == batch.NOTHING_SELECTED
