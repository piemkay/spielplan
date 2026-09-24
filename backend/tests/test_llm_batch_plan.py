"""A launched batch's plan, and the one reading of it the gate, stage 6 and the retry share.

Spec v2.1 §8.4 ("picks a batch and providers, sees the cost estimate, launches"; the pass count is
chosen per batch), §8 stage 6, §9; decisions 324, 325, 441 and 442.

Decision 442's whole reason is a quote that priced one plan over a stage that ran another: without a
carried plan a three-provider, two-pass batch would be gated on the stored one-by-one reservation
and bill six runs, and the admin's pass choice would silently not reach stage 6. So what this file
pins is that there is ONE plan, and it is asserted from both ends of the money:

  * THE RESERVATION. `spend.cap_check` with a batch reserves that batch's providers at that batch's
    passes at both attempts (decision 325), exactly the figure the stored plans of the same
    providers sum to -- read off the refusal the meter writes, whose digits are the ones it compared.
  * THE BILL. A task whose payload carries a plan walks stage 6 through the real driver against
    `ops/fake_llm.py`, and the provider calls the double received are providers x passes x the
    attempts each run used; the gate that let it through read the same plan, which a cap too small
    for the batch and large enough for the stored plan would not have let through.

And the two things a batch may not do: raise its own cap or reach a key or a model nobody configured
(the merge covers four settings and nothing else), and fail quietly - a batch that does not read is a
`PLAN` refusal naming the batch, never the stored settings run in its place.

The install is `test_llm_stage.py`'s, restated as fixtures here over that file's own helpers - the
suite's convention is to share helpers and restate fixtures: a title at stage 6 with a `title:<id>`
task, the active vocabulary, all three providers keyed against the double, and the pack in the raw
store. The double is a refuser and is used as shipped.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from decimal import Decimal

import pytest

from spielplan.acquire import actions, pipeline, stages
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.dna import packs
from spielplan.flywheel import batch as flywheel_batch
from spielplan.llm import pricing, spend
from tests.test_llm_stage import (
    DOUBLE,
    PLOT,
    REVIEWS,
    STAGE,
    TASK_KEY,
    TITLE,
    _board,
    _bundle_row,
    _calls,
    _drain,
    _settings,
    _vocabulary,
)

# Small enough that any reservation is over it and the meter says by how much: the refusal's
# `reserved_usd` is then the figure `cap_check` compared, read back rather than recomputed here.
TINY = 0.000001
BATCH = {"providers": ["gemini", "anthropic"], "passes": 2}


@pytest.fixture
def double():
    """`ops/fake_llm.py` with a fresh request log, loaded as `test_llm_stage.py` loads it."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR` under this test's tmp_path, so the pack and every paid answer land there."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def packed(db, data_dir, secrets_key, double) -> int:
    """A title at stage 6 with its `title:<id>` task, the active vocabulary, one bundle row, all
    three providers keyed against the double, and the title's pack kept in the raw store."""
    await _vocabulary(db)
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', 'Grey Harbour', 2021,"
        " true)", TITLE,
    )
    await _bundle_row(db)
    await pipeline.write_board(db, TITLE, stage=6, status=pipeline.RUNNING)
    assert await pipeline.enqueue_title(db, TITLE) is True
    for provider in ("anthropic", "openai", "gemini"):
        await registry.save_connector(db, provider, api_key=double.KEYS[provider])
    text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    return await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)


async def _reserved(db, **batch) -> Decimal:
    """The reservation `cap_check` held this title against, under the month as it stands."""
    refusal = await spend.cap_check(db, title_id=TITLE, **batch)
    assert refusal is not None and refusal.kind == spend.OVER_CAP, refusal
    return Decimal(refusal.detail["reserved_usd"])


async def _carry(db, plan) -> None:
    """The plan a launch writes onto the title's task (decision 443), under the one key stage 6's
    reader spells (`stages.PLAN_KEY`)."""
    await db.execute(
        "UPDATE acquisition_task SET payload = payload || $2::text::jsonb WHERE key = $1",
        TASK_KEY, json.dumps({stages.PLAN_KEY: plan}),
    )


# --- the reservation --------------------------------------------------------------------------


async def test_a_batch_reserves_its_own_providers_at_its_own_passes_and_both_attempts(db, packed):
    """Two providers at two passes is four runs, each reserved at two attempts: the figure is twice
    what the two providers' stored one-pass plans reserve between them, to the micro-dollar, because
    `pricing.estimate_title` is priced per provider and multiplied by passes. And the stored setting
    is untouched by having been overridden for one question."""
    await _settings(db, cap_usd=TINY, extraction_provider="anthropic")
    anthropic = await _reserved(db)
    await _settings(db, cap_usd=TINY)
    gemini = await _reserved(db)

    refusal = await spend.cap_check(db, title_id=TITLE, batch=BATCH)
    assert refusal.kind == spend.OVER_CAP, refusal
    assert Decimal(refusal.detail["reserved_usd"]) == 2 * (gemini + anthropic)
    assert gemini != anthropic, "two prices, or the sum above could not tell the providers apart"
    assert f"{spend.ATTEMPTS} attempts x 2 pass(es) x 2 provider(s)" in refusal.reason, refusal.reason
    refusal.reason.encode("ascii")

    plan = await spend.extraction_plan(db, batch=BATCH)
    assert ([p.provider for p in plan.providers], plan.passes) == (["gemini", "anthropic"], 2)
    stored = await spend.extraction_plan(db)
    assert ([p.provider for p in stored.providers], stored.passes) == (["gemini"], 1), (
        "the batch reached the stored settings; it is merged over them for one question and no more"
    )


async def test_a_batch_cannot_raise_the_cap_or_name_a_key_or_a_model(db, packed, double):
    """Decision 442: the merge covers `extraction_provider`, `parallel`, `parallel_providers` and
    `passes`, never `cap_usd`, a key or a model - so a batch chooses among what the household has
    configured and can neither lift its own ceiling nor reach a provider nobody keyed. Everything
    else a batch carries is not read at all, including the stored settings' own names."""
    await _settings(db, cap_usd=TINY)
    smuggled = {
        "providers": ["gemini"], "passes": 1,
        "cap_usd": 1_000_000, "api_key": "sk-smuggled-by-a-batch", "model": "gemini-9-ultra",
        "models": {"gemini": "gemini-9-ultra"}, "extraction_provider": "openai",
        "parallel": True, "parallel_providers": ["openai"],
    }

    assert await spend.cap_check(db, title_id=TITLE, batch=smuggled) == await spend.cap_check(
        db, title_id=TITLE
    ), "a batch carrying a cap, a key or a model changed what the meter compared"
    plan = await spend.extraction_plan(db, batch=smuggled)
    assert [(p.provider, p.model, p.key) for p in plan.providers] == [
        ("gemini", pricing.DEFAULT_MODELS["gemini"], double.KEYS["gemini"])
    ]
    assert await spend.cap(db) == Decimal(str(TINY))


@pytest.mark.parametrize("batch", [
    {"providers": [], "passes": 1},
    {"providers": ["mistral"], "passes": 1},
    {"providers": "gemini", "passes": 1},
    {"providers": ["gemini", 7], "passes": 1},
    {"providers": ["gemini"], "passes": 0},
    {"providers": ["gemini"], "passes": True},
    {"providers": ["gemini"], "passes": "2"},
    {"passes": 2},
    ["gemini"],
    "gemini x 2",
])
async def test_a_batch_that_does_not_read_is_a_plan_refusal_naming_the_batch(db, packed, batch):
    """A malformed batch is refused, and refused as the batch's fault: the sentence an operator
    reads on the board names the flywheel batch, not "the llm connector's" settings, which are
    fine and which an operator sent there would find nothing wrong with. Never the stored plan run
    in its place - that is the quote-over-another-plan decision 442 exists to prevent. With no cap
    at all, decision 348's sentence still comes first (decision 325's order)."""
    await _settings(db)
    assert (await spend.cap_check(db, title_id=TITLE, batch=batch)).kind == spend.NO_CAP

    await _settings(db, cap_usd=100)
    refusal = await spend.cap_check(db, title_id=TITLE, batch=batch)
    assert refusal is not None and refusal.kind == spend.PLAN, refusal
    assert "flywheel batch" in refusal.reason and "llm connector" not in refusal.reason, refusal.reason
    refusal.reason.encode("ascii")
    assert await spend.extraction_plan(db, batch=batch) == refusal
    assert await spend.retry_refusal(db, title_id=TITLE, batch=batch) == refusal.reason


async def test_the_retry_pre_check_prices_the_batch_the_task_carries(db, packed):
    """The admin retry's advice is `cap_check`'s answer for the plan the revived task will walk
    with (decision 444), so a month with room for the stored plan and not for the batch refuses the
    batch's retry and lets the stored one through - one state, one answer, whichever plan it is."""
    await _settings(db, cap_usd=TINY)
    stored = await _reserved(db)
    await _settings(db, cap_usd=float(2 * stored))

    assert await spend.retry_refusal(db, title_id=TITLE) is None
    one_provider_twice = {"providers": ["gemini"], "passes": 2}
    assert await spend.retry_refusal(db, title_id=TITLE, batch=one_provider_twice) is None
    refused = await spend.retry_refusal(db, title_id=TITLE, batch=BATCH)
    assert refused is not None and refused.startswith(spend.OVER_CAP_PREFIX), refused
    assert "2 pass(es) x 2 provider(s)" in refused, refused


# --- the plan through the driver ----------------------------------------------------------------


async def test_the_gate_reserves_for_the_plan_the_task_carries(db, packed, double):
    """The driver's gate is `spend.cap_check` asked with the task's plan: a month with room for no
    reservation parks the title naming the batch's own arithmetic, two passes of two providers, and
    not the stored settings' one by one. Nothing is asked of any provider."""
    await _settings(db, cap_usd=TINY)
    await _carry(db, BATCH)

    report = await _drain(db, double)

    walk = report.tasks[0]
    assert (walk.stage, walk.status) == (6, pipeline.PARKED), walk.as_dict()
    assert walk.reason.startswith(spend.OVER_CAP_PREFIX), walk.reason
    assert "2 attempts x 2 pass(es) x 2 provider(s)" in walk.reason, (
        f"the gate reserved for a plan other than the one the task carries: {walk.reason}"
    )
    assert double.state.requests == []
    assert await _calls(db) == 0


async def test_stage_six_runs_the_plan_the_task_carries_and_the_gate_let_it_through(
    db, packed, double
):
    """Decision 442's other end: the stage that bills runs the batch's plan. The stored settings are
    Gemini at one pass, which the double's default answer - one invented term, then compliance on
    the named retry - bills in two calls; the batch is Gemini and Anthropic at two passes each, four
    runs, and the double received eight, four per provider, every run's retry among them. The cap
    admits both attempts of all four runs, so the gate that let the title through asked about the
    same plan the stage then ran - the test above is that gate refusing when it does not."""
    await _settings(db, cap_usd=100)
    await _carry(db, BATCH)

    report = await _drain(db, double)

    walk = report.tasks[0]
    assert STAGE.name in walk.stages_run and walk.stage > STAGE.number, walk.as_dict()
    sent = double.state.requests
    assert Counter(r["provider"] for r in sent) == {"gemini": 4, "anthropic": 4}, sent
    assert sum(1 for r in sent if r.get("retry")) == 4, "one named retry per run, four runs"
    assert await _calls(db) == len(sent) == 2 * 2 * spend.ATTEMPTS
    wrote = (await _board(db))["detail"][STAGE.name]
    assert (wrote["providers"], wrote["passes"], wrote["runs"]) == (["gemini", "anthropic"], 2, 4), wrote


async def test_abandoning_a_launched_title_takes_it_out_of_its_batch_so_a_relaunch_can_replan_it(
    db, packed, double
):
    """Decision 448, amending 443. A launched title the gate parks over the cap - two launches in one
    month each fit the room alone, and the second one's titles are the ones parked - kept its row
    `running` and its batch plan through every retry, so the household could neither relaunch it on
    a cheaper plan nor retry it below the paid stage without pricing the dear one again, and the month
    rolling over billed the plan it could not revise. Abandoning its job is the lever: the row is
    queued again with no batch, no task of the title carries the plan or the batch any more, and a
    relaunch at one provider and one pass walks the paid stage on that plan and bills only it.
    [M5.6 review cycle 1, M56-MONEY-01]"""
    await _settings(db, cap_usd=TINY)
    stored, dear = await _reserved(db), await _reserved(db, batch=BATCH)
    cheaper = await flywheel_batch.quote(db, titles=1, providers=["gemini"], passes=1)
    room = max(stored, cheaper["reserved_usd"])
    assert room < dear, "the month has to fit the cheaper plan and not the batch, or this proves nothing"
    row = await db.fetchval(
        "INSERT INTO flywheel_item (kind, detail, reason, title_id, est_titles)"
        " VALUES ('thin_facet', '{}'::jsonb, 'thin by this test', $1, 1) RETURNING id", TITLE,
    )
    await _settings(db, cap_usd=100)
    await flywheel_batch.launch(db, item_ids=[row], **BATCH)
    await _settings(db, cap_usd=float(room))

    parked = (await _drain(db, double)).tasks[0]
    assert (parked.stage, parked.status) == (STAGE.number, pipeline.PARKED), parked.as_dict()
    assert "2 pass(es) x 2 provider(s)" in parked.reason and await _calls(db) == 0

    await actions.abandon(db, TITLE)

    assert actions.RELEASED in (await _board(db))["reason"]
    assert tuple(await db.fetchrow("SELECT status, batch_id FROM flywheel_item WHERE id = $1", row)) == (
        "queued", None
    )
    for payload in await db.fetch("SELECT payload FROM acquisition_task"):
        assert not {stages.PLAN_KEY, stages.BATCH_KEY} & set(payload["payload"]), payload["payload"]

    await flywheel_batch.launch(db, item_ids=[row], providers=["gemini"], passes=1)
    walk = (await _drain(db, double)).tasks[0]

    assert STAGE.name in walk.stages_run and walk.stage > STAGE.number, walk.as_dict()
    assert Counter(r["provider"] for r in double.state.requests) == {"gemini": 2}, (
        "one run and its named retry, on the plan the relaunch chose and not the batch it left"
    )
    assert await db.fetchval("SELECT status FROM flywheel_item WHERE id = $1", row) == "done"
