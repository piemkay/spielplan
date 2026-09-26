"""A launched batch's plan, and the one reading of it the gate, stage 6 and the retry share (decision 442).
Asserted from both ends of the money: the reservation and the bill. Needs TEST_DATABASE_URL."""

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

# Small enough that any reservation is over it: the refusal's `reserved_usd` is then read back.
TINY = 0.000001
BATCH = {"providers": ["gemini", "anthropic"], "passes": 2}


@pytest.fixture
def double():
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def packed(db, data_dir, secrets_key, double) -> int:
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
    refusal = await spend.cap_check(db, title_id=TITLE, **batch)
    assert refusal is not None and refusal.kind == spend.OVER_CAP, refusal
    return Decimal(refusal.detail["reserved_usd"])


async def _carry(db, plan) -> None:
    """Under the one key stage 6's reader spells (`stages.PLAN_KEY`)."""
    await db.execute(
        "UPDATE acquisition_task SET payload = payload || $2::text::jsonb WHERE key = $1",
        TASK_KEY, json.dumps({stages.PLAN_KEY: plan}),
    )


async def test_a_batch_reserves_its_own_providers_at_its_own_passes_and_both_attempts(db, packed):
    """`pricing.estimate_title` is priced per provider and multiplied by passes, so the figure is exact."""
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
    """Decision 442: the merge covers four settings, never `cap_usd`, a key or a model."""
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
    """The sentence names the flywheel batch, not the llm connector's settings, which are fine."""
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
    await _settings(db, cap_usd=TINY)
    stored = await _reserved(db)
    await _settings(db, cap_usd=float(2 * stored))

    assert await spend.retry_refusal(db, title_id=TITLE) is None
    one_provider_twice = {"providers": ["gemini"], "passes": 2}
    assert await spend.retry_refusal(db, title_id=TITLE, batch=one_provider_twice) is None
    refused = await spend.retry_refusal(db, title_id=TITLE, batch=BATCH)
    assert refused is not None and refused.startswith(spend.OVER_CAP_PREFIX), refused
    assert "2 pass(es) x 2 provider(s)" in refused, refused


async def test_the_gate_reserves_for_the_plan_the_task_carries(db, packed, double):
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
    """The double bills one invented term then compliance on the retry: two calls per run."""
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
    """Decision 448: abandoning the job takes the title out of its batch, so a relaunch can replan it."""
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
