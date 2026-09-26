"""Stage 6 through the driver: the cap is asked before the call, and the verdict ends the walk (§8, §9).
Every test is the whole path over `ops/fake_llm.py`; the double's request log counts what was paid.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import importlib.util
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

# `test_llm_extract.py`'s title; the double cuts its quotes from this pack, so they are quotable.
PLOT = ("A fisherman returns to the harbour town that exiled him and slowly takes his revenge on the "
        "men who drowned his brother.")
REVIEWS = [
    ("imdb", "It is a **bleak** and unforgiving portrait of a town that has decided what it will not "
             "remember, shot in grey light by a director who refuses every consolation."),
    ("tmdb", "The tension builds patiently across two hours and never once releases, a slow burn "
             "that rewards anyone willing to stay with its long and silent scenes."),
]


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
    """So stage 9 finds no active bundle and parks: where a walk past stage 6 stops here."""
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
    tag = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES ($1, 'v1', 'themes.robots', 'themes', 2) RETURNING id", TITLE,
    )
    await db.execute("INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'imdb:1')",
                     tag, "a director who refuses every consolation")


@pytest.fixture
async def titled(db, data_dir, secrets_key, double):
    """The board reads `(6, running)`, so the drain resumes at stage 6 (`_resume_index`)."""
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
    text, info = packs.render_pack(TITLE, "Grey Harbour", 2021, "film", None, None, PLOT, REVIEWS)
    return await packs.store_pack(db, TITLE, "v1", text, info, entity_key=TASK_KEY)


async def _settings(db, **fields) -> None:
    base = {"extraction_provider": "gemini", "parallel": False, "passes": 1}
    await registry.save_connector(db, "llm", **{**base, **fields})


async def _spend_to_the_cap(db, pack_document_id: int) -> None:
    """`llm_call.at` defaults to now(), inside the household's month whichever zone `TZ` resolves to."""
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
    board = await _board(db)
    assert (board["stage"], board["status"], board["reason"]) == (6, pipeline.PARKED, reason)
    assert board["retry_after"] is not None and board["retry_after"] > datetime.now(UTC)
    task = await _task(db)
    assert task["state"] == queue.PENDING
    assert task["attempts"] == 0, "a park hands back the attempt its lease took (decision 336)"
    assert task["next_attempt_at"] == board["retry_after"]
    assert (task["result_note"], task["last_error"]) == (reason, None)


async def _park_over_the_cap(db, double, pack_document_id: int) -> str:
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
    """Decision 348 puts the check before stage 6 runs: a
    stage that ran then checked would already have billed."""
    reason = await _park_over_the_cap(db, double, packed)

    assert "of the $1.00 monthly cap is spent" in reason, reason
    assert "so no provider is called for this title" in reason, reason
    board = await _board(db)
    assert board["detail"][STAGE.name]["paid_stage"] == STAGE.name
    assert await _tags(db) == 1, "the tier the bundle wrote is untouched"


async def test_the_next_tick_leases_nothing_for_a_title_parked_over_the_cap(db, packed, double):
    """The park deferred the task, so `queue.lease` does not hand it to the next tick at all."""
    reason = await _park_over_the_cap(db, double, packed)
    before = dict(await _board(db))

    report = await _drain(db, double)

    assert report.leased == 0 and report.tasks == [], report.as_dict()
    assert double.state.requests == []
    assert await _calls(db) == 1
    assert dict(await _board(db)) == before
    await _assert_parked_at_six(db, reason)


async def test_an_admin_retry_over_the_cap_is_refused_with_the_boards_reason(db, packed, double):
    """However a task is made due, stage 6 cannot call anyone past the cap."""
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


async def test_an_uncapped_install_parks_stage_six_with_decision_348s_sentence(db, packed, double):
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
    """A park and not a failure: what is missing is a person's choice."""
    await registry.save_connector(db, "llm", cap_usd=CAP)
    report = await _drain(db, double)

    reason = report.tasks[0].reason
    assert "no extraction provider is assigned" in reason, reason
    assert double.state.requests == []
    await _assert_parked_at_six(db, reason)


async def test_under_the_cap_stage_six_writes_the_tier_and_the_board_moves_past_it(db, packed, double):
    """The double invents one term, the retry names it, and the second answer complies."""
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
    """Closed on the first attempt: every re-run would bill two more calls against a provider in breach."""
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
    """`blockReason` is final and closes the task; a cut-off is retryable and keeps the curve."""
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


@pytest.mark.parametrize("refusal", ["no cap", "over cap", "no assignment"])
async def test_a_title_the_gate_parks_never_builds_the_fetcher(db, packed, double, refusal):
    """The gate is asked before the factory, so a parked title never builds an HTTP client."""
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
    await _settings(db)

    async def broken(conn):
        raise ValueError("Unknown scheme for proxy URL")

    report = await pipeline.drain(db, fetcher_factory=broken)

    assert report.tasks[0].reason == pipeline.NO_SPEND_CAP, report.as_dict()
    await _assert_parked_at_six(db, pipeline.NO_SPEND_CAP)


async def test_a_second_key_does_not_rerun_a_title_whose_extraction_failed_for_good(db, packed, double):
    """Decision 431 is per TITLE: a second library copy
    must not rerun an extraction that failed for good."""
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
    """An exhausted balance is the account's state, not
    this title's, so it parks with the attempt refunded."""
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
    """`worker.py` bounds the drain at 420 s and a call may
    take 300 s; the write-ahead row survives the cancel."""
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
    """Decision 325's bound is per TASK: 4 walks x 1 run x 2 attempts = 8 calls, each metered."""
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
    """The fixture resumes past stage 5, so this title reaches stage 6 with no pack."""
    await _settings(db, cap_usd=100)
    report = await _drain(db, double)

    reason = report.tasks[0].reason
    assert "no DNA pack is stored" in reason and "stage 5" in reason, reason
    assert double.state.requests == []
    assert await _calls(db) == 0
    await _assert_parked_at_six(db, reason)


async def test_a_title_failed_for_good_on_its_last_attempt_still_parks_its_other_keys(
    db, packed, double
):
    """`queue.fail(permanent=True)` on the last attempt looks
    like exhaustion, so permanence is recorded on the task."""
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
    """A breaker pause met before anything was billed parks until the pause ends, attempt refunded."""
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
    """Stage 6 opens the Fetcher only when it is about to send."""
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
    """Two keys x 4 walks x 2 attempts = 16 metered calls: the bound holds per task, not per title."""
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
