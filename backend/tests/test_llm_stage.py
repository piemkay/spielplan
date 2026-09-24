"""Stage 6 through the driver: the cap is asked before the call, and the verdict ends the walk.

Spec v2.1 §8 ("paid stages (6) never auto-retry past the spend cap"), §9 ("Two-attempt pattern:
retry once with the specific contract violation named"); decisions 325, 348, 431, 432.

`test_llm_extract.py` asks `extract.extract_title` directly, which is the right altitude for the
two-attempt loop and the wrong one for the three exit measures this file exists for. Measures 7, 8
and 9 are claims about the DRIVER - what `pipeline.drain` does with a task whose title has reached
stage 6 while the month is at its cap - and a claim about the driver asserted beside it is
decision 348's own warning restated: a stage that runs and then checks a cap has already spent the
money, so the only place "without issuing a paid call" can be measured is from outside the stage,
by counting what the provider received.

SO EVERY TEST HERE IS THE WHOLE PATH. A title with a board row at stage 6 and a `title:<id>` task
from `pipeline.enqueue_title`, drained by `pipeline.drain` with a `fetcher_factory` whose Fetcher
has `ops/fake_llm.py` as its transport - the real Fetcher, so the provider hosts' policies, the
pacing and the breaker are the ones production runs, and the double's own request log is the
count of outbound provider requests. Nothing is patched: the gate is `refuse_uncapped_spend` as
shipped, the cap and the plan are rows `registry.save_connector` wrote, and the meter is
`llm_call` as 0028 made it.

  * MEASURE 7: at the cap, the job parks `over spend cap` at stage 6 with ZERO requests and no
    `llm_call` row added, the board shows the wait with its end date, and the task is deferred
    with its attempt refunded (decision 336: waiting is not failing).
  * MEASURE 8: the next tick leases nothing for it and asks nothing.
  * MEASURE 9: an admin retry - the task made due now, `ops/m53_exit_criterion.py` check 5's
    manoeuvre - re-parks with the same reason and asks nothing, and `spend.retry_refusal` gives
    the sentence the board shows, because one state has one answer.
  * The same gate with no cap keeps decision 348's sentence; with no assignment it names the
    setting; with room it lets stage 6 run and the tier is written; a second violation fails the
    stage for good (decision 431) and `dna_tag` is unchanged (measure 3, through the driver).

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spielplan.acquire import fetch, pipeline, queue
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.dna import packs
from spielplan.llm import pricing, spend

REPO = Path(__file__).resolve().parents[2]
DOUBLE = REPO / "ops" / "fake_llm.py"

TITLE = 7
TASK_KEY = f"title:{TITLE}"
STAGE = next(s for s in pipeline.STAGES if s.number == 6)
# The month's cap, and what the meter already holds when a test says the month is at it.
CAP = 1
AT_CAP = Decimal("1.00")

# `test_llm_extract.py`'s title, restated rather than imported for this suite's convention on
# helpers shared across files. The double cuts its quotes from this pack's own sections, so the
# three tags it answers with really are quotable here.
PLOT = ("A fisherman returns to the harbour town that exiled him and slowly takes his revenge on the "
        "men who drowned his brother.")
REVIEWS = [
    ("imdb", "It is a **bleak** and unforgiving portrait of a town that has decided what it will not "
             "remember, shot in grey light by a director who refuses every consolation."),
    ("tmdb", "The tension builds patiently across two hours and never once releases, a slow burn "
             "that rewards anyone willing to stay with its long and silent scenes."),
]


# --- the install ---------------------------------------------------------------------------------


@pytest.fixture
def double():
    """`ops/fake_llm.py`, loaded the way `test_fake_llm.py` loads it, with a fresh request log."""
    spec = importlib.util.spec_from_file_location("fake_llm", DOUBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR` under this test's tmp_path, so the pack and every paid answer land there, and so
    stage 9 finds no active bundle and parks - which is where a walk past stage 6 stops here."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


async def _vocabulary(db) -> None:
    await db.execute("INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 4, 6)")
    await db.execute("INSERT INTO dna_facet (version, facet, ord) VALUES "
                     "('v1', 'mood', 0), ('v1', 'themes', 1), ('v1', 'pacing', 2), ('v1', 'place', 3)")
    await db.execute(
        "INSERT INTO dna_term (version, term, facet, gloss) VALUES "
        "('v1', 'mood.bleak', 'mood', 'hopeless, grey, unconsoled'), "
        "('v1', 'mood.tense', 'mood', NULL), "
        "('v1', 'pacing.slow_burn', 'pacing', 'patient build that pays off late'), "
        "('v1', 'place.harbour_town', 'place', NULL), "
        "('v1', 'themes.revenge', 'themes', 'a wrong answered in kind'), "
        "('v1', 'themes.robots', 'themes', NULL)"
    )


async def _bundle_row(db) -> None:
    """One extracted-tier row the seed import wrote, with its quote: the tier a failure leaves."""
    tag = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES ($1, 'v1', 'themes.robots', 'themes', 2) RETURNING id", TITLE,
    )
    await db.execute("INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'imdb:1')",
                     tag, "a director who refuses every consolation")


@pytest.fixture
async def titled(db, data_dir, secrets_key, double):
    """A title at stage 6 with a `title:<id>` task, the active vocabulary, one bundle row, and all
    three providers keyed - everything stage 6 needs except the pack and the settings.

    The board row reads `(6, running)`, which is exactly what `run_task` writes the moment stage 5
    advances, so the drain resumes at stage 6 (`_resume_index`) and the stages before it are not
    this file's subject.
    """
    await _vocabulary(db)
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned)"
        " VALUES ($1, 'movie', 'Grey Harbour', 2021, true)", TITLE,
    )
    await _bundle_row(db)
    await pipeline.write_board(db, TITLE, stage=6, status=pipeline.RUNNING)
    assert await pipeline.enqueue_title(db, TITLE) is True
    for provider in ("anthropic", "openai", "gemini"):
        await registry.save_connector(db, provider, api_key=double.KEYS[provider])


@pytest.fixture
async def packed(db, titled) -> int:
    """The title's pack, rendered by `dna/packs.py` and kept in the raw store under the task's key;
    its raw document is what every metered call cites (decision 430)."""
    text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    return await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)


async def _settings(db, **fields) -> None:
    """The `llm` row: Gemini assigned, one pass, and whatever the test adds - the cap above all."""
    base = {"extraction_provider": "gemini", "parallel": False, "passes": 1}
    await registry.save_connector(db, "llm", **{**base, **fields})


async def _spend_to_the_cap(db, pack_document_id: int) -> None:
    """One metered attempt this month costing the whole cap. `llm_call.at` defaults to now(), which
    is inside the household's calendar month whichever zone `TZ` resolves to (decision 325)."""
    await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=TITLE, pass_index=1, attempt=1,
        tokens_in=0, tokens_out_billed=0, usd=AT_CAP, ok=True, error=None,
        pack_document_id=pack_document_id, response_document_id=None,
    )


class _Clock:
    """The fetcher's clock and sleeper together, so no pacing is waited out in real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


async def _drain(db, double) -> pipeline.DrainReport:
    """One tick of the real drain, its one Fetcher speaking to the double - decision 373's seam."""
    clock = _Clock()

    async def factory(conn):
        return fetch.Fetcher(conn=conn, transport=httpx.ASGITransport(app=double.app), clock=clock,
                             sleep=clock.sleep, jitter=lambda low, high: 0.0)

    return await pipeline.drain(db, fetcher_factory=factory)


async def _scenario(double, **fields) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=double.app)) as http:
        resp = await http.post("http://fake-llm/_test/scenario", json=fields)
    assert resp.status_code == 200, resp.text


async def _board(db):
    return await db.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", TITLE)


async def _task(db):
    return await db.fetchrow(
        "SELECT * FROM acquisition_task WHERE kind = $1 AND key = $2", pipeline.TASK_KIND, TASK_KEY
    )


async def _calls(db) -> int:
    return await db.fetchval("SELECT count(*) FROM llm_call")


async def _tags(db) -> int:
    return await db.fetchval("SELECT count(*) FROM dna_tag WHERE title_id = $1", TITLE)


async def _assert_parked_at_six(db, reason: str) -> None:
    """The deferral decision 336 gives a park with a deadline, on both tables: the board shows the
    stage, the sentence and the wait's end; the task is pending with the lease's attempt handed
    back, due at that same instant, carrying the sentence as a note and no error."""
    board = await _board(db)
    assert (board["stage"], board["status"], board["reason"]) == (6, pipeline.PARKED, reason)
    assert board["retry_after"] is not None and board["retry_after"] > datetime.now(UTC)
    task = await _task(db)
    assert task["state"] == queue.PENDING
    assert task["attempts"] == 0, "a park hands back the attempt its lease took (decision 336)"
    assert task["next_attempt_at"] == board["retry_after"]
    assert (task["result_note"], task["last_error"]) == (reason, None)


# --- exit measures 7, 8 and 9: the month at its cap ------------------------------------------------


async def _park_over_the_cap(db, double, pack_document_id: int) -> str:
    """Measure 7's walk: the cap set, the month already at it, one drain. Returns the reason."""
    await _settings(db, cap_usd=CAP)
    await _spend_to_the_cap(db, pack_document_id)
    report = await _drain(db, double)

    assert report.leased == 1 and report.parked == 1, report.as_dict()
    walk = report.tasks[0]
    assert (walk.stage, walk.stages_run) == (6, [STAGE.name]), walk.as_dict()
    assert walk.reason.startswith(spend.OVER_CAP_PREFIX), walk.reason
    assert double.state.requests == [], "a provider was asked while the month was at its cap"
    assert await _calls(db) == 1, "an llm_call row was added for a title that was never called"
    await _assert_parked_at_six(db, walk.reason)
    return walk.reason


async def test_at_the_cap_stage_six_parks_over_spend_cap_and_asks_no_provider(db, packed, double):
    """Exit measure 7. §8: "paid stages (6) never auto-retry past the spend cap", and decision 348
    puts the check in the driver's gate so that it is asked BEFORE stage 6 runs - a stage that ran
    and then checked would already have billed the call it was checking.

    The count that makes this a measurement is the double's own request log: empty. The meter is
    unchanged, so nothing was paid and nothing was recorded as paid. The reason is the one M5.7's
    card and this exit criterion match on - it starts `over spend cap` - and names the month and
    the cap, and the park carries `waiting_on_the_world()`'s deadline, so the title is re-asked
    daily and resumes by itself when the month rolls over or the cap is raised (decision 325)."""
    reason = await _park_over_the_cap(db, double, packed)

    assert "of the $1.00 monthly cap is spent" in reason, reason
    assert "so no provider is called for this title" in reason, reason
    board = await _board(db)
    assert board["detail"][STAGE.name]["paid_stage"] == STAGE.name
    assert await _tags(db) == 1, "the tier the bundle wrote is untouched"


async def test_the_next_tick_leases_nothing_for_a_title_parked_over_the_cap(db, packed, double):
    """Exit measure 8: still parked, no auto-retry. The park deferred the task to its deadline, so
    `queue.lease` - which claims `next_attempt_at <= now()` only - does not hand it to the next
    tick at all: no walk, no gate asked, no request, and the board still says what it said."""
    reason = await _park_over_the_cap(db, double, packed)
    before = dict(await _board(db))

    report = await _drain(db, double)

    assert report.leased == 0 and report.tasks == [], report.as_dict()
    assert double.state.requests == []
    assert await _calls(db) == 1
    assert dict(await _board(db)) == before
    await _assert_parked_at_six(db, reason)


async def test_an_admin_retry_over_the_cap_is_refused_with_the_boards_reason(db, packed, double):
    """Exit measure 9. §8's parked job is "retryable from admin", and the coverage row says a retry
    that would breach the cap "is refused with that reason rather than queued".

    Decision 330's revive button is M5.6's, so the retry is made the way `ops/m53_exit_criterion.py`
    check 5 makes one - the task due now - and the drain that leases it walks straight back into
    the gate, which is the guarantee: however a task is made due, stage 6 cannot call anyone past
    the cap. It re-parks with the same sentence and asks nothing, and `spend.retry_refusal`, the
    advice M5.6's route will show before it makes the task due, is that sentence word for word."""
    reason = await _park_over_the_cap(db, double, packed)
    await db.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, TASK_KEY,
    )

    report = await _drain(db, double)

    assert report.leased == 1 and report.parked == 1, report.as_dict()
    assert report.tasks[0].reason == reason
    assert double.state.requests == [], "the retry reached a provider past the cap"
    assert await _calls(db) == 1
    await _assert_parked_at_six(db, reason)
    assert await spend.retry_refusal(db, title_id=TITLE) == reason


# --- the gate's other two refusals ---------------------------------------------------------------


async def test_an_uncapped_install_parks_stage_six_with_decision_348s_sentence(db, packed, double):
    """No default cap ships (decision 325), so a household that has keyed and assigned a provider
    and set no cap reaches stage 6 and parks under the sentence M5.1 shipped, unchanged - the gate
    kept its name and became the cap check rather than being routed around (decision 348). Nothing
    else was missing, so the sentence is about the cap alone, and nothing was asked."""
    await _settings(db)
    report = await _drain(db, double)

    assert report.tasks[0].reason == pipeline.NO_SPEND_CAP, report.as_dict()
    assert double.state.requests == []
    assert await _calls(db) == 0
    await _assert_parked_at_six(db, pipeline.NO_SPEND_CAP)
    assert (await _board(db))["detail"][STAGE.name] == {"paid_stage": STAGE.name}


async def test_an_unassigned_extraction_parks_naming_the_setting_and_asks_no_provider(
    db, packed, double
):
    """A cap and three keyed providers, and no `extraction_provider`: decision 324 says stage 6
    never guesses one, so the gate parks naming the setting to fix, with a deadline, and nothing
    is asked. A park and not a failure, because what is missing is a person's choice."""
    await registry.save_connector(db, "llm", cap_usd=CAP)
    report = await _drain(db, double)

    reason = report.tasks[0].reason
    assert "no extraction provider is assigned" in reason, reason
    assert double.state.requests == []
    await _assert_parked_at_six(db, reason)


# --- with room under the cap, stage 6 runs and the verdict decides -------------------------------


async def test_under_the_cap_stage_six_writes_the_tier_and_the_board_moves_past_it(db, packed, double):
    """The gate lets stage 6 run, and stage 6 reaches its verdict inside itself (decision 432): the
    double's default first answer invents one term, the retry names it, the second answer complies,
    and the tier is written. The board moves on - stage 7 records the verdict stage 6 reached
    (decision 462), stage 8 takes the bundle branch because this title's origin is `bundle`, the
    column's default (decision 463), and stage 9 parks for the bundle this install does not have -
    so "past 6" is a board row at 9. Both attempts are metered, and the stage's own detail says
    what it wrote and what it cost."""
    await _settings(db, cap_usd=100)
    report = await _drain(db, double)

    walk = report.tasks[0]
    assert walk.stages_run == ["dna extract", "verify", "project", "place"], walk.as_dict()
    assert walk.stage == 9 and walk.status == pipeline.PARKED, walk.as_dict()
    assert len(double.state.requests) == 2
    assert await _calls(db) == 2
    assert await db.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND provider = 'gemini'", TITLE
    ) == 3
    board = await _board(db)
    assert board["stage"] == 9
    wrote = board["detail"][STAGE.name]
    assert (wrote["tags"], wrote["calls"]) == (3, 2), wrote


async def test_a_second_violation_fails_stage_six_for_good_and_writes_nothing(db, packed, double):
    """Exit measure 3 through the driver, and decision 431's verb. A provider that repeats the
    violation its retry named is asked exactly twice and never a third time: stage 6 fails
    PERMANENTLY, so the task is closed on its first attempt rather than put back on the queue's
    curve - where every re-run would bill two more calls against a title whose provider has twice
    answered in breach - and the board says no retry is coming, because an operator needs to know
    that nothing will happen until they act. `dna_tag` for the title is the count it was."""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", posture="stubborn")
    before = await _tags(db)

    report = await _drain(db, double)

    walk = report.tasks[0]
    assert (walk.stage, walk.status) == (6, pipeline.FAILED), walk.as_dict()
    assert len(double.state.requests) == 2
    assert await _calls(db) == 2, "both paid attempts are in the meter"
    assert await _tags(db) == before == 1
    board = await _board(db)
    assert (board["stage"], board["status"], board["reason"]) == (6, pipeline.FAILED, walk.reason)
    assert board["detail"][STAGE.name]["retrying"] is False, board["detail"]
    task = await _task(db)
    assert task["state"] == queue.FAILED, "a permanent failure closes the task (decision 431)"
    assert task["attempts"] == 1 and task["attempts"] < task["max_attempts"]
    assert task["last_error"] == walk.reason

    again = await _drain(db, double)
    assert again.leased == 0 and len(double.state.requests) == 2, again.as_dict()


@pytest.mark.parametrize(("envelope", "state", "retrying"), [
    ("blocked", queue.FAILED, False),
    ("max_tokens", queue.PENDING, True),
])
async def test_a_provider_refusal_fails_for_good_and_a_cut_off_keeps_the_queues_curve(
    db, packed, double, envelope, state, retrying
):
    """Decision 431's two provider failures, as the driver records them. Gemini's
    `promptFeedback.blockReason` is final - asking again cannot change the answer - so the task is
    closed on its first attempt; a cut-off at the output ceiling is retryable, so the task goes
    back on the queue's backoff and the board says a retry is coming, every re-run behind the same
    gate. One call either way, metered as a failed call."""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", envelope=envelope)

    report = await _drain(db, double)

    walk = report.tasks[0]
    assert (walk.stage, walk.status) == (6, pipeline.FAILED), walk.as_dict()
    assert len(double.state.requests) == 1
    assert await db.fetchval("SELECT count(*) FROM llm_call WHERE NOT ok") == 1
    board = await _board(db)
    assert board["detail"][STAGE.name]["retrying"] is retrying, board["detail"]
    task = await _task(db)
    assert task["state"] == state
    if state == queue.PENDING:
        assert task["next_attempt_at"] > datetime.now(UTC), "the queue's curve, not now"


# --- review cycle 1: the factory, per-title permanence, the account, the budget -------------------


@pytest.mark.parametrize("refusal", ["no cap", "over cap", "no assignment"])
async def test_a_title_the_gate_parks_never_builds_the_fetcher(db, packed, double, refusal):
    """`run_task` opened the drain's Fetcher for any stage declaring `fetches` BEFORE `_run_stage`
    asked the gate, so every daily re-ask of a title parked at stage 6 built an HTTP client for a walk
    that would make no request -- the property decision 373 states as "THE POINT IS THE CALL THAT
    NEVER HAPPENS". The gate is asked first now, and the factory is not called at all.
    [M5.5 review cycle 1, NBR-02, M55-SPEND-05]"""
    if refusal == "no cap":
        await _settings(db)
    elif refusal == "over cap":
        await _settings(db, cap_usd=CAP)
        await _spend_to_the_cap(db, packed)
    else:
        await registry.save_connector(db, "llm", cap_usd=CAP)
    built = []

    async def factory(conn):
        built.append(conn)
        return fetch.Fetcher(conn=conn, transport=httpx.ASGITransport(app=double.app))

    report = await pipeline.drain(db, fetcher_factory=factory)

    assert report.parked == 1 and report.tasks[0].stage == 6, report.as_dict()
    assert built == [], "a Fetcher was built for a title the gate parked"
    assert double.state.requests == []


async def test_a_factory_that_raises_cannot_turn_a_park_into_a_failure(db, packed, double):
    """The raising factory was the driver's failure, spending the attempt a park would have refunded:
    a proxy variable httpx cannot read failed every title parked at stage 6 on each daily re-ask, and
    four of those closed them for good. With the gate asked first, the park is the answer and the
    factory is never reached. [M5.5 review cycle 1, NBR-02]"""
    await _settings(db)

    async def broken(conn):
        raise ValueError("Unknown scheme for proxy URL")

    report = await pipeline.drain(db, fetcher_factory=broken)

    assert report.tasks[0].reason == pipeline.NO_SPEND_CAP, report.as_dict()
    await _assert_parked_at_six(db, pipeline.NO_SPEND_CAP)


async def test_a_second_key_does_not_rerun_a_title_whose_extraction_failed_for_good(db, packed, double):
    """Decision 431: "The admin retry is the only way back after a permanent failure", stated per
    TITLE -- "retried exactly once" would otherwise be "true per walk and false per title". The queue
    closes one TASK, and decision 322 gives a title several keys by design, so a second library copy
    of a film whose extraction had failed for good walked back into stage 6 and paid for attempt 1 and
    the named retry again with no admin action. The gate now reads the title's permanent failure and
    parks the other key -- with a deadline, so it comes back by itself once an admin revives the task
    that failed. [M5.5 review cycle 1, M55-BUDGET-06]"""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", posture="stubborn")
    first = await _drain(db, double)
    assert first.tasks[0].status == pipeline.FAILED and len(double.state.requests) == 2

    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ($1, 'jellyfin:jf-second-copy-4k',"
        " jsonb_build_object('title_id', $2::int))", pipeline.TASK_KIND, TITLE)
    second = await _drain(db, double)

    walk = second.tasks[0]
    assert (walk.key, walk.status, walk.stage) == ("jellyfin:jf-second-copy-4k", pipeline.PARKED, 6)
    assert "failed for good" in walk.reason and TASK_KEY in walk.reason, walk.reason
    assert len(double.state.requests) == 2, "the second key bought the extraction again"
    assert await _calls(db) == 2


async def test_a_refusal_of_the_households_account_parks_the_title_with_its_attempt_refunded(
    db, packed, double
):
    """An exhausted balance is the account's state and not this title's: stage 6 parks with a deadline
    naming what the provider said, the task is deferred with its attempt handed back, and the title is
    asked again daily -- where decision 431 had failed it for good. [M5.5 review cycle 1,
    M55-BUDGET-07]"""
    await _settings(db, cap_usd=100)
    body = {"error": {"code": 402, "message": "Your Prepay credit balance is depleted."}}

    async def factory(conn):
        return fetch.Fetcher(conn=conn, transport=httpx.MockTransport(
            lambda request: httpx.Response(402, json=body)))

    report = await pipeline.drain(db, fetcher_factory=factory)

    walk = report.tasks[0]
    assert walk.status == pipeline.PARKED, walk.as_dict()
    assert "Your Prepay credit balance is depleted." in walk.reason
    await _assert_parked_at_six(db, walk.reason)
    assert await db.fetchval("SELECT sum(usd) FROM llm_call") == 0


async def test_a_drain_cancelled_mid_call_leaves_the_call_in_the_meter_where_the_gate_sees_it(
    db, packed, double
):
    """`worker.py` bounds the drain with `asyncio.wait_for(timeout=420)` and one provider call may take
    `TIMEOUT_S` = 300 s, so a cancellation mid-call is how an ordinary tick ends on a backlog. The row
    used to be written after the answer, so a cancelled call the provider billed left nothing, and the
    re-walk after `reclaim_expired` bought attempt 1 again behind a gate that read the month without
    it. The write-ahead row survives the cancellation at its ceiling, and the next gate reads it.
    `worker.py` is not edited (plan §8); this is the drain under the same `wait_for` it runs under.
    [M5.5 review cycle 1, M55-BUDGET-03, NBR-01]"""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", content="clean")

    class _Stalled(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await httpx.ASGITransport(app=double.app).handle_async_request(request)
            await response.aread()
            await asyncio.Event().wait()

    async def factory(conn):
        return fetch.Fetcher(conn=conn, transport=_Stalled())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(pipeline.drain(db, fetcher_factory=factory), timeout=1.0)

    [call] = await db.fetch("SELECT * FROM llm_call")
    assert call["error"].startswith(spend.UNSETTLED_PREFIX), dict(call)
    [sent] = double.state.requests
    usage = sent["usage"]
    billed = pricing.usd(usage["promptTokenCount"],
                         usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
                         pricing.price_for("gemini", "gemini-3.7-flash"))
    assert call["usd"] >= billed > 0, "the ceiling left standing is at least what the provider billed"
    assert await spend.spent(db) == call["usd"], "the gate's SUM reads the cancelled call"
    assert (await spend.meter(db))["unsettled_usd"] == call["usd"]


async def test_a_violation_then_a_provider_failure_is_bounded_by_the_queue_and_every_call_metered(
    db, packed, double
):
    """Decision 431 keeps a retryable provider failure on the queue's curve even after attempt 1 broke
    the contract, so each walk buys attempt 1 and the named retry again. That is decision 325's bound
    per TASK: at most `max_attempts` walks x runs x two attempts, here 4 x 1 x 2 = 8 calls -- each ONE
    request on the wire (decision 436 (1)), where the fetcher used to put up to four sends behind each,
    and each one metered. This docstring called it "the per-title bound", which it is only for a title
    holding one key; a title with two walks it twice (the two-key test below).
    [M5.5 review cycle 1, M55-BUDGET-05; review cycle 2, C2-PAID-03]"""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", fault=503, fault_when="retry")
    for _ in range(5):
        await db.execute("UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1",
                         pipeline.TASK_KIND)
        await _drain(db, double)

    task = await _task(db)
    assert task["state"] == queue.FAILED and task["attempts"] == task["max_attempts"] == 4
    assert len(double.state.requests) == 8, [r["envelope"] for r in double.state.requests]
    assert await _calls(db) == 8
    assert await _tags(db) == 1


async def test_a_title_with_no_pack_parks_at_stage_six_naming_stage_five(db, titled, double):
    """Decision 432: stage 6 reads the STORED pack, and stage 5 builds it (decision 461) - but the
    fixture puts the board at stage 6, so the resume point skips stage 5 and this title reaches
    stage 6 with no pack, as a walk resumed past stage 5 does. A keyed, assigned, capped install:
    the gate lets it through (no pack is no reservation, and stage 6 says why itself), and the
    stage parks naming stage 5, with a deadline, having asked nobody."""
    await _settings(db, cap_usd=100)
    report = await _drain(db, double)

    reason = report.tasks[0].reason
    assert "no DNA pack is stored" in reason and "stage 5" in reason, reason
    assert double.state.requests == []
    assert await _calls(db) == 0
    await _assert_parked_at_six(db, reason)


# --- review cycle 2: permanence on the last attempt, the breaker, the fetcher a park never needs ----


async def test_a_title_failed_for_good_on_its_last_attempt_still_parks_its_other_keys(
    db, packed, double
):
    """The per-title read above told a permanent failure apart by `attempts < max_attempts`, and
    `queue.fail(permanent=True)` on a task's final attempt writes exactly the row exhaustion writes. So
    a title whose three earlier walks each met a transient failure -- an Anthropic 529 hour, a flaky
    TMDb, none of which refunds its attempt -- and whose provider then broke the contract twice on the
    fourth, was not seen, and its second key walked back into stage 6 and bought attempt 1 and the named
    retry again. Permanence is recorded on the task that failed, not inferred from its count.
    [M5.5 review cycle 2, C2-PAID-01]"""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", posture="stubborn")
    await db.execute("UPDATE acquisition_task SET attempts = max_attempts - 1 WHERE kind = $1 AND key = $2",
                     pipeline.TASK_KIND, TASK_KEY)
    first = await _drain(db, double)
    assert first.tasks[0].status == pipeline.FAILED and len(double.state.requests) == 2
    task = await _task(db)
    assert task["state"] == queue.FAILED and task["attempts"] == task["max_attempts"]

    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ($1, 'jellyfin:jf-second-copy-4k',"
        " jsonb_build_object('title_id', $2::int))", pipeline.TASK_KIND, TITLE)
    second = await _drain(db, double)

    walk = second.tasks[0]
    assert (walk.key, walk.status, walk.stage) == ("jellyfin:jf-second-copy-4k", pipeline.PARKED, 6)
    assert "failed for good" in walk.reason and TASK_KEY in walk.reason, walk.reason
    assert len(double.state.requests) == 2, "the second key bought the extraction again"
    assert await _calls(db) == 2


async def _another_title(db, title_id: int) -> None:
    """A second title at stage 6 with its own pack, board row and `title:<id>` task."""
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned)"
        " VALUES ($1, 'movie', 'Grey Harbour', 2021, true)", title_id,
    )
    text, info = packs.render_pack(title_id, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    await packs.store_pack(db, title_id, "v1", text, info, entity_key=f"title:{title_id}")
    await pipeline.write_board(db, title_id, stage=6, status=pipeline.RUNNING)
    assert await pipeline.enqueue_title(db, title_id) is True


async def test_titles_behind_one_account_refusal_all_park_rather_than_failing_on_the_breaker(
    db, packed, double
):
    """OpenAI refuses an exhausted prepaid balance with a 429 (`credit_balance_exhausted`), which the
    fetcher re-sends four times and counts toward the host's breaker. Eight such failures open it, so
    the first two titles of a drain parked as the account's and every later one met `HostPaused` --
    a retryable refusal that never sent anything, which stage 6 read as a transient failure and spent
    an attempt on. Titles closed one by one on the breaker, which M55-BUDGET-07's park exists to
    prevent. A breaker pause met before anything in the extraction was billed now parks until the
    pause ends, with the attempt refunded. [M5.5 review cycle 2, C2-PAID-02]"""
    await _settings(db, cap_usd=100, extraction_provider="openai")
    for title_id in (8, 9, 10):
        await _another_title(db, title_id)
    body = {"error": {"message": "Your organization has no prepaid credits remaining.",
                      "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted"}}
    sent = []
    clock = _Clock()

    def refused(request):
        sent.append(request)
        return httpx.Response(429, json=body)

    async def factory(conn):
        return fetch.Fetcher(conn=conn, transport=httpx.MockTransport(refused), clock=clock,
                             sleep=clock.sleep, jitter=lambda low, high: 0.0)

    report = await pipeline.drain(db, fetcher_factory=factory)

    assert report.leased == 4 and report.parked == 4, report.as_dict()
    assert len(sent) == 8, "two titles met the account refusal before the breaker opened"
    tasks = await db.fetch("SELECT key, state, attempts FROM acquisition_task WHERE kind = $1 ORDER BY key",
                           pipeline.TASK_KIND)
    states = [(t["state"], t["attempts"]) for t in tasks]
    assert states == [(queue.PENDING, 0)] * 4, [dict(t) for t in tasks]
    paused = [w for w in report.tasks if "paused" in w.reason]
    assert len(paused) == 2, [w.reason for w in report.tasks]
    assert await db.fetchval("SELECT coalesce(sum(usd), 0) FROM llm_call") == 0


async def test_a_title_waiting_for_its_pack_under_a_cap_never_builds_the_fetcher(db, titled, double):
    """Moving the factory after the gate kept it off a title the GATE parks, and still opened it before
    stage 6's own parks: a capped install's title with no stored pack -- decision 432 said every title
    reached stage 6 in that state until stage 5 was wired -- built the Fetcher on every daily re-ask, and a
    factory that raised turned the park into the drain's failure, spending the attempt, four of which
    closed the title for good. Stage 6 now opens the drain's Fetcher only when it is about to send.
    [M5.5 review cycle 2, NBR-C2-01]"""
    await _settings(db, cap_usd=100)
    built = []

    async def broken(conn):
        built.append(conn)
        raise ValueError("Unknown scheme for proxy URL")

    report = await pipeline.drain(db, fetcher_factory=broken)

    reason = report.tasks[0].reason
    assert "no DNA pack is stored" in reason, report.as_dict()
    assert built == [], "a Fetcher was asked for by a title that parked before any request"
    await _assert_parked_at_six(db, reason)


async def test_a_title_with_two_keys_buys_the_named_retry_once_per_walk_of_each_key(
    db, packed, double, monkeypatch
):
    """Decision 325 stated the curve's bound per TITLE -- "at most `max_attempts` walks of 2 x passes x
    providers metered calls" -- and so did the test above. It holds per TASK. A title holds one task
    per key by design (decision 322), each key walks its own `max_attempts` curve, and
    `stages._FAILED_FOR_GOOD` reads a permanent failure and never an exhausted one. So a violation
    followed by a transient failure, on a film with two library copies, is 2 keys x 4 walks x 2
    attempts = 16 metered calls with the named retry sent 8 times, each walk behind a cap check of its
    own: twice the bound the record gave the owner. Every call is metered and each was inside the cap
    when it was asked, so the record is restated rather than the code changed (decision 438 keeps the
    same bound recorded and not enforced). [M5.5 review cycle 2, C2-PAID-03]"""
    await _settings(db, cap_usd=100)
    await _scenario(double, provider="gemini", fault=503, fault_when="retry")
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ($1, 'jellyfin:jf-second-copy-4k',"
        " jsonb_build_object('title_id', $2::int))", pipeline.TASK_KIND, TITLE)
    asked = []
    real = spend.cap_check

    async def counted(conn, **kwargs):
        asked.append(kwargs["title_id"])
        return await real(conn, **kwargs)

    monkeypatch.setattr(spend, "cap_check", counted)
    for _ in range(5):
        await db.execute("UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1",
                         pipeline.TASK_KIND)
        await _drain(db, double)

    tasks = await db.fetch("SELECT key, state, attempts, max_attempts FROM acquisition_task"
                           " WHERE kind = $1 ORDER BY key", pipeline.TASK_KIND)
    assert [(t["state"], t["attempts"], t["max_attempts"]) for t in tasks] == [(queue.FAILED, 4, 4)] * 2
    retries = [r for r in double.state.requests if r.get("retry")]
    assert len(double.state.requests) == 16, [r["envelope"] for r in double.state.requests]
    assert len(retries) == 8, "the named retry is sent once per walk of each key"
    assert asked == [TITLE] * 8, "each walk passes the cap check afresh"
    assert await _calls(db) == 16
    assert await _tags(db) == 1


def _exit_script(monkeypatch):
    """`ops/m55_exit_criterion.py` as a module of its own, the way `test_m51_exit_criterion.py` loads
    its script: the variables it sets outright at import, its `sys.path` entries and its
    `sys.modules` entry are all put back by `monkeypatch`."""
    for name, placeholder in {
        "SESSION_SECRET": "a-session-secret-this-test-puts-back-afterwards",
        "SECRETS_KEY": "a-secrets-key-this-test-puts-back-afterwards",
        "PUBLIC_URL": "http://localhost:8080",
    }.items():
        monkeypatch.setenv(name, os.environ.get(name, placeholder))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("m55_exit_criterion_under_test",
                                                  REPO / "ops" / "m55_exit_criterion.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fetcher_first", [False, True])
async def test_exit_check_seven_fails_when_a_fetcher_is_built_for_the_title_it_parks(
    db, packed, double, monkeypatch, tmp_path, fetcher_first
):
    """`ops/m55_exit_criterion.py` check 7 printed "run_task opens the drain's Fetcher before the gate
    asks" -- lead 3's defect, which review cycle 1 fixed (NBR-02) -- beside a count of built Fetchers
    its verdict never read. So the console the RELEASE block will commit as the MEASURED record stated
    an ordering the code no longer has, and a regression that built a Fetcher for a parked title again
    would have passed the instrument. Check 7 is run here on this file's install, once over the
    shipped drain and once over a drain that builds its Fetcher before walking anything, which check 7
    must now fail. [M5.5 review cycle 2, M55-C2-DOC-03]"""
    module = _exit_script(monkeypatch)
    await _spend_to_the_cap(db, packed)
    ctx = module.Install(conn=db, work=tmp_path, double=double, clock=module.Clock(),
                         httpx_log=module.Captured(), app_log=module.Captured(), version="v1")
    if fetcher_first:
        real = pipeline.drain

        async def eager(conn, *, fetcher_factory, **kwargs):
            await fetcher_factory(conn)
            return await real(conn, fetcher_factory=fetcher_factory, **kwargs)

        monkeypatch.setattr(pipeline, "drain", eager)

    ok, detail = await module.check_seven(ctx)

    assert ok is not fetcher_first, detail
    assert f"fetcher(s) built for the drain: {int(fetcher_first)}" in detail, detail
    assert "before the gate asks" not in detail, detail
    assert double.state.requests == []
