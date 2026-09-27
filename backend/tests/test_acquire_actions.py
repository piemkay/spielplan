"""§6.6's board actions: retry, retry from stage N and abandon, in the domain and over HTTP
(decision 444). Needs TEST_DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest

from spielplan.acquire import actions, pipeline, queue, rawstore, stages
from spielplan.connectors import registry
from spielplan.core import secrets
from spielplan.core.config import settings
from spielplan.importer import bundle as bundle_import
from spielplan.llm import spend
from tests.fixtures import make_bundle as fx
from tests.helpers import household
from tests.test_acquire_pipeline import (
    MOVIE,
    TRAKT_HOST,
    _CannedWeb,
    _Clock,
    _derived_counts,
    _enrichable,
    _factory,
    _json_route,
)

# §4.1's partition: the app's own ids start at 1e9. Written directly: the subject is the actions.
PARKED = 1_000_000_801
FAILED = 1_000_000_802
INBOX = 1_000_000_803
OTHER = 1_000_000_804

THE_TEN = ("identify", "enrich", "derive", "reviews gate", "dna pack", "dna extract", "verify",
           "project", "place", "ready")


# `test_acquire_pipeline.py`'s fixtures for a walk over a canned web, restated over its helpers.


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def bundled(db, data_dir, tmp_path):
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    return report


@pytest.fixture
async def keyed(db, secrets_key):
    await secrets.put_connector_secrets(db, "tmdb", {}, {"api_key": "tmdb-test-key"})
    await secrets.put_connector_secrets(db, "trakt", {"client_id": "trakt-test"}, None)
    return db


async def _title(db, title_id: int, name: str = "A Bigger Splash") -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES ($1, 'movie', $2, 2016, 'acquired')",
        title_id, name,
    )


async def _job(db, title_id: int, *, stage: int, status: str, reason: str | None = None,
               retry_after: datetime | None = None) -> None:
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status, reason, retry_after) VALUES"
        " ($1, $2, $3, $4, $5)",
        title_id, stage, status, reason, retry_after,
    )


async def _task(db, title_id: int, key: str, *, state: str = "pending", attempts: int = 0,
                last_error: str | None = None, next_attempt_at: datetime | None = None,
                **payload) -> int:
    return await db.fetchval(
        "INSERT INTO acquisition_task (kind, key, payload, state, attempts, last_error,"
        " next_attempt_at) VALUES ($1, $2, $3, $4, $5, $6, coalesce($7, now())) RETURNING id",
        pipeline.TASK_KIND, key, {"title_id": title_id, **payload}, state, attempts, last_error,
        next_attempt_at,
    )


async def _board(db, title_id: int):
    return await db.fetchrow("SELECT * FROM acquisition_job WHERE title_id = $1", title_id)


async def _tasks(db, title_id: int) -> list:
    return await db.fetch(
        "SELECT * FROM acquisition_task WHERE payload ->> 'title_id' = $1 ORDER BY id", str(title_id)
    )


async def _snapshot(db) -> tuple:
    """Both tables whole: a refused action rolls back, `updated_at` included."""
    return (
        [dict(r) for r in await db.fetch("SELECT * FROM acquisition_job ORDER BY title_id")],
        [dict(r) for r in await db.fetch("SELECT * FROM acquisition_task ORDER BY id")],
    )


async def _refused(call) -> str:
    with pytest.raises(actions.ActionRefused) as refusal:
        await call
    return refusal.value.reason


@pytest.mark.parametrize("window", ["open", "closed"])
async def test_a_job_parked_at_the_reviews_gate_retried_from_four_resumes_there_and_asks_nothing(
    db, bundled, keyed, window
):
    """With the window closed the driver alone would re-enter
    at stage 2; only the cleared `retry_after` keeps 4."""
    clock = _Clock()
    routes = _enrichable()
    for sort in ("likes", "lowest", "highest"):
        routes[(TRAKT_HOST, f"/movies/tt5000001/comments/{sort}")] = _json_route([])
    site = _CannedWeb(routes)
    assert await pipeline.enqueue_item(db, MOVIE) is True
    first = await pipeline.drain(db, limit=1, fetcher_factory=_factory(site, clock))
    assert first.parked == 1, first.as_dict()
    title_id = first.tasks[0].title_id
    parked = await _board(db, title_id)
    assert (parked["stage"], parked["status"]) == (4, pipeline.PARKED) and parked["retry_after"]
    assert site.fetched, "the first walk made no request, so the retry proves nothing"
    counted = await _derived_counts(db, title_id)
    if window == "closed":
        await db.execute("UPDATE acquisition_job SET retry_after = now() - interval '1 minute'")
        await db.execute("UPDATE acquisition_task SET next_attempt_at = now() - interval '1 minute'")
        assert await pipeline._resume_index(db, title_id) == 1, (
            "the window closed and the driver would re-ask from stage 2 on its own (decision 421)"
        )

    assert await actions.retry_from(db, title_id, 4) == 1
    board = await _board(db, title_id)
    assert (board["stage"], board["status"], board["retry_after"]) == (4, actions.QUEUED, None)
    assert board["reason"] == (
        "retried from the admin board: resumes at stage 4 (reviews gate); nothing before it runs again"
    )
    made: list[str] = []
    site.seen.clear()

    async def counting(conn):
        made.append("built")
        return await _factory(site, clock)(conn)

    second = await pipeline.drain(db, limit=1, fetcher_factory=counting)

    assert second.leased == 1, second.as_dict()
    walk = second.tasks[0]
    assert walk.stages_run == ["reviews gate"], walk.as_dict()
    assert site.fetched == [], [str(r.url) for r in site.fetched]
    assert made == [], "an HTTP client was built for a retry that resumes past every fetching stage"
    assert await _derived_counts(db, title_id) == counted, "the retry duplicated a derived row"


def test_each_board_state_admits_what_decision_444_gives_it():
    assert actions.admitted(pipeline.PARKED) == [actions.RETRY_FROM, actions.ABANDON]
    assert actions.admitted(pipeline.FAILED) == [actions.RETRY, actions.RETRY_FROM, actions.ABANDON]
    assert actions.admitted(actions.ABANDONED) == [actions.RETRY_FROM]
    assert actions.admitted(pipeline.READY) == [actions.RETRY_FROM]
    assert actions.admitted(actions.QUEUED) == actions.admitted(pipeline.RUNNING) == []
    assert actions.PACK_STAGE == 5
    assert [s["name"] for s in actions.stage_legend()] == list(THE_TEN)
    assert [s["number"] for s in actions.stage_legend()] == list(range(1, 11))


async def test_a_plain_retry_is_refused_on_a_parked_job_and_walks_a_failed_one_again(db):
    """Decision 336: `failed` is the only state offering a plain retry."""
    await _title(db, PARKED)
    await _job(db, PARKED, stage=3, status=pipeline.PARKED, reason="waiting on a bundle",
               retry_after=datetime.now(UTC) + timedelta(days=1))
    await _task(db, PARKED, f"title:{PARKED}", next_attempt_at=datetime.now(UTC) + timedelta(days=1))
    await _title(db, FAILED, "Call Me By Your Name")
    await _job(db, FAILED, stage=3, status=pipeline.FAILED, reason="OSError: the raw volume is full")
    await _task(db, FAILED, "jellyfin:jf-failed", state=queue.FAILED, attempts=4,
                last_error="OSError: the raw volume is full")
    before = await _snapshot(db)

    reason = await _refused(actions.retry(db, PARKED))
    assert "decision 336" in reason and "retry it from a stage instead" in reason, reason
    assert await _snapshot(db) == before

    assert await actions.retry(db, FAILED) == 1
    board = await _board(db, FAILED)
    assert (board["stage"], board["status"], board["retry_after"]) == (3, actions.QUEUED, None)
    assert board["reason"] == (
        "retried from the admin board: resumes at stage 3 (derive), where it failed; nothing before"
        " it runs again"
    )
    (task,) = await _tasks(db, FAILED)
    assert (task["state"], task["attempts"], task["last_error"]) == (queue.PENDING, 0, None)
    assert task["next_attempt_at"] <= datetime.now(UTC)
    assert task["result_note"] == board["reason"]


async def test_a_revived_task_loses_its_failed_for_good_mark_and_frees_the_titles_other_keys(
    db, secrets_key
):
    """A cap with room is configured, because a retry at the paid stage asks the meter first."""
    await registry.save_connector(db, "llm", cap_usd=100, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key="GEMINI-KEY-NOT-A-REAL-ONE-0003")
    await _title(db, FAILED)
    await _job(db, FAILED, stage=6, status=pipeline.FAILED, reason="the provider twice broke the contract")
    await _task(db, FAILED, "jellyfin:jf-first-copy", state=queue.FAILED, attempts=1,
                last_error="the provider twice broke the contract", **{stages.FAILED_FOR_GOOD_MARK: True})
    other = await _task(db, FAILED, f"title:{FAILED}",
                        next_attempt_at=datetime.now(UTC) + timedelta(days=1))

    async def stage_six_for_the_other_key() -> stages.Outcome:
        row = await db.fetchrow("SELECT * FROM acquisition_task WHERE id = $1", other)

        async def never():
            raise AssertionError("stage 6 asked for the fetcher, so it was about to send")

        ctx = stages.StageContext(conn=db, task=queue.Task.from_row(row), title_id=FAILED,
                                  open_fetcher=never)
        return await stages.dna_extract(ctx)

    held = await stage_six_for_the_other_key()
    assert held.reason == stages.FAILED_FOR_GOOD.format(key="jellyfin:jf-first-copy"), held.reason

    assert await actions.retry(db, FAILED) == 2
    first, second = await _tasks(db, FAILED)
    assert stages.FAILED_FOR_GOOD_MARK not in first["payload"], first["payload"]
    assert [(t["state"], t["attempts"]) for t in (first, second)] == [(queue.PENDING, 0)] * 2
    freed = await stage_six_for_the_other_key()
    assert freed.verb == stages.PARK and "failed for good" not in freed.reason, freed.reason


async def test_the_inbox_row_with_no_task_gets_exactly_one_title_task(db):
    """`_park_thin` writes board rows with no queue task at all (the pipeline's inbox)."""
    await _title(db, INBOX)
    await _job(db, INBOX, stage=2, status=pipeline.PARKED,
               reason="placed with 7 of 10 feature blocks; queued for stage 2 enrichment")

    assert await actions.retry_from(db, INBOX, 2) == 1

    (task,) = await _tasks(db, INBOX)
    assert (task["key"], task["state"], task["attempts"]) == (f"title:{INBOX}", queue.PENDING, 0)
    assert task["payload"] == {"title_id": INBOX}
    assert task["next_attempt_at"] <= datetime.now(UTC)
    assert (await _board(db, INBOX))["status"] == actions.QUEUED
    assert await pipeline.enqueue_title(db, INBOX) is False, "the same key, not a second claim"

    await db.execute("UPDATE acquisition_job SET status = 'parked' WHERE title_id = $1", INBOX)
    await db.execute("UPDATE acquisition_task SET state = 'done' WHERE key = $1", f"title:{INBOX}")
    assert await actions.retry_from(db, INBOX, 1) == 1
    assert [(t["key"], t["state"]) for t in await _tasks(db, INBOX)] == [(f"title:{INBOX}", "pending")]


async def test_an_abandoned_job_leases_nothing_until_it_is_retried_from_a_stage(db, data_dir):
    """Abandoned is neither parked nor failed: a thin-but-placed title is never shown as broken."""
    reason = "reviews gate: 1 source, 49 words - retry window 30 days"
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason=reason,
               retry_after=datetime.now(UTC) + timedelta(days=30))
    await _task(db, PARKED, "jellyfin:jf-abandon", last_error="an earlier 503",
                next_attempt_at=datetime.now(UTC) + timedelta(days=30))

    assert await actions.abandon(db, PARKED) == 1

    board = await _board(db, PARKED)
    assert (board["stage"], board["status"], board["retry_after"]) == (4, actions.ABANDONED, None)
    assert board["reason"] == (
        "abandoned from the admin board: nothing runs for this title until it is retried from a stage."
        f" It had parked at stage 4 (reviews gate) with: {reason}"
    )
    (task,) = await _tasks(db, PARKED)
    assert (task["state"], task["result_note"], task["last_error"]) == (
        queue.SKIPPED, actions.ABANDONED_NOTE, "an earlier 503"
    )
    await db.execute("UPDATE acquisition_task SET next_attempt_at = now() - interval '1 day'")
    drained = await pipeline.drain(db, limit=4)
    assert drained.leased == 0, drained.as_dict()
    assert (await _board(db, PARKED))["status"] == actions.ABANDONED
    assert "decision 336" in await _refused(actions.retry(db, PARKED))
    assert "not abandon" in await _refused(actions.abandon(db, PARKED))

    assert await actions.retry_from(db, PARKED, 2) == 1
    (task,) = await _tasks(db, PARKED)
    assert (task["state"], task["attempts"]) == (queue.PENDING, 0)
    assert ((await _board(db, PARKED))["stage"], (await _board(db, PARKED))["status"]) == (2, "queued")


@pytest.mark.parametrize("holder", ["walk", "lease"])
async def test_every_action_is_refused_while_the_title_is_in_flight(db, pg_url, holder):
    """Two writers of one board row is what decision 322's lock prevents, and a click is a writer."""
    await _title(db, FAILED)
    await _job(db, FAILED, stage=3, status=pipeline.FAILED, reason="a stage raised")
    await _task(db, FAILED, f"title:{FAILED}", attempts=1)
    walker = await asyncpg.connect(pg_url)
    try:
        if holder == "walk":
            assert await walker.fetchval("SELECT pg_try_advisory_lock($1, $2)", pipeline._TITLE_LOCK,
                                         FAILED)
        else:
            assert len(await queue.lease(db, [pipeline.TASK_KIND], limit=1)) == 1
        before = await _snapshot(db)

        for act in (lambda: actions.retry(db, FAILED), lambda: actions.retry_from(db, FAILED, 2),
                    lambda: actions.abandon(db, FAILED)):
            assert await _refused(act()) == actions.IN_FLIGHT
        assert await _snapshot(db) == before
    finally:
        await walker.close()


async def test_a_lease_that_lands_after_the_actions_check_takes_nothing_from_it(db, pg_url, monkeypatch):
    """The drain's lease lands between the action's read and its write, on another connection."""
    await _title(db, PARKED)
    await _job(db, PARKED, stage=9, status=pipeline.PARKED, reason="stage 9: no bundle is active")
    await _task(db, PARKED, f"title:{PARKED}", next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    drainer = await asyncpg.connect(pg_url)
    checked = actions._hold
    leased: list[queue.Task] = []

    async def check_then_the_drain_leases(conn, title_id):
        await checked(conn, title_id)
        leased.extend(await queue.lease(drainer, [pipeline.TASK_KIND], limit=10))

    monkeypatch.setattr(actions, "_hold", check_then_the_drain_leases)
    try:
        closed = await actions.abandon(db, PARKED)

        assert leased == [], "the drain leased the title's task between the action's check and its write"
        assert closed == 1
        (task,) = await _tasks(db, PARKED)
        assert task["state"] == queue.SKIPPED
        assert await queue.lease(drainer, [pipeline.TASK_KIND], limit=10) == []
    finally:
        await drainer.close()
    assert (await _board(db, PARKED))["status"] == actions.ABANDONED


async def _die_in_the_stage(db) -> None:
    """What a SIGKILL, an OOM or the tick's budget leaves: a task leased past its expiry."""
    (task,) = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    await db.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 second' WHERE id = $1", task.id
    )
    await queue.reclaim_expired(db)
    await pipeline.close_abandoned_boards(db)
    await pipeline.complete_landed_boards(db)


@pytest.mark.parametrize("via", ["retry", "retry_from"])
async def test_a_retried_walk_that_dies_in_the_stage_it_resumed_at_is_closed_failed_and_retryable(db, via):
    """A board left `queued` beside a reaped task admitted
    no action, so the reaper must close it `failed`."""
    await _title(db, FAILED)
    await _job(db, FAILED, stage=3, status=pipeline.FAILED, reason="OSError: the raw volume is full")
    await _task(db, FAILED, f"title:{FAILED}", state=queue.FAILED, attempts=4,
                last_error="OSError: the raw volume is full")
    if via == "retry":
        assert await actions.retry(db, FAILED) == 1
    else:
        assert await actions.retry_from(db, FAILED, 2) == 1
    resumed = await _board(db, FAILED)
    assert resumed["status"] == actions.QUEUED

    for _attempt in range(4):
        await _die_in_the_stage(db)

    (task,) = await _tasks(db, FAILED)
    assert (task["state"], task["last_error"]) == (queue.FAILED, queue.ABANDONED)
    board = await _board(db, FAILED)
    assert (board["stage"], board["status"]) == (resumed["stage"], pipeline.FAILED), dict(board)
    assert board["reason"] == f"{queue.ABANDONED} It had been queued with: {resumed['reason']}"
    assert actions.admitted(board["status"]) == [actions.RETRY, actions.RETRY_FROM, actions.ABANDON]
    assert await actions.retry(db, FAILED) == 1


async def test_a_stage_past_the_one_the_job_reached_is_refused(db):
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting")
    before = await _snapshot(db)

    for stage in (5, 0, 11):
        assert "never moves a job forward" in await _refused(actions.retry_from(db, PARKED, stage))
    assert await _snapshot(db) == before
    with pytest.raises(actions.NoJob):
        await actions.retry_from(db, OTHER, 1)


async def _at_the_cap(db, title_id: int) -> None:
    """One metered attempt costing the whole cap, citing a raw document as 0028 requires."""
    await registry.save_connector(db, "llm", cap_usd=1, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key="GEMINI-KEY-NOT-A-REAL-ONE-0004")
    document = await rawstore.store(
        db, source="dna", kind="pack", url=f"pack:{title_id}", content=b"a pack",
        entity_key=f"title:{title_id}", content_type="text/plain",
    )
    await spend.record_call(
        db, provider="gemini", model="gemini-3.7-flash", title_id=title_id, pass_index=1, attempt=1,
        tokens_in=0, tokens_out_billed=0, usd=Decimal("1.00"), ok=True, error=None,
        pack_document_id=document, response_document_id=None,
    )


async def test_a_paid_stage_retry_over_the_cap_is_refused_with_the_meters_reason(
    db, data_dir, secrets_key
):
    """Stage 5 fetches nothing, so a retry from 5 is refused like 6; a retry from 2 is queued."""
    await _title(db, PARKED)
    await _job(db, PARKED, stage=6, status=pipeline.PARKED, reason="over spend cap: parked earlier",
               retry_after=datetime.now(UTC) + timedelta(days=1))
    await _task(db, PARKED, f"title:{PARKED}", next_attempt_at=datetime.now(UTC) + timedelta(days=1))
    await _at_the_cap(db, PARKED)
    before = await _snapshot(db)

    meter = await spend.retry_refusal(db, title_id=PARKED)
    assert meter is not None and meter.startswith(spend.OVER_CAP_PREFIX), meter
    for stage in (6, 5):
        assert await _refused(actions.retry_from(db, PARKED, stage)) == meter
    assert await _snapshot(db) == before

    assert await actions.retry_from(db, PARKED, 2) == 1
    assert (await _board(db, PARKED))["stage"] == 2


async def test_the_paid_stage_pre_check_prices_the_plan_the_revived_task_carries(db, secrets_key):
    """Pricing the stored plan instead would queue a task the gate then parks under another sentence."""
    await registry.save_connector(db, "llm", cap_usd=100, extraction_provider="gemini")
    await registry.save_connector(db, "gemini", api_key="GEMINI-KEY-NOT-A-REAL-ONE-0005")
    await _title(db, FAILED)
    await _job(db, FAILED, stage=6, status=pipeline.FAILED, reason="a transient 529")
    await _task(db, FAILED, f"title:{FAILED}", state=queue.FAILED, attempts=4,
                **{stages.PLAN_KEY: {"providers": [], "passes": 1}})
    before = await _snapshot(db)

    reason = await _refused(actions.retry(db, FAILED))
    assert "flywheel batch" in reason, reason
    assert await _snapshot(db) == before


async def test_the_board_names_the_ten_stages_and_the_actions_each_job_admits(app, db):
    """The surface renders what the server accepts rather than deriving it."""
    states = {PARKED: pipeline.PARKED, FAILED: pipeline.FAILED, INBOX: actions.ABANDONED,
              OTHER: pipeline.READY, 1_000_000_805: actions.QUEUED, 1_000_000_806: pipeline.RUNNING}
    for title_id, state in states.items():
        await _title(db, title_id, f"title {title_id}")
        await _job(db, title_id, stage=4, status=state, reason=f"{state} for this test")
    admin, _member = await household(app)

    payload = (await admin.get("/api/admin/acquisition")).json()

    assert payload["stages"] == [{"number": n, "name": name} for n, name in enumerate(THE_TEN, 1)]
    offered = {job["title_id"]: job["actions"] for job in payload["jobs"]}
    assert offered == {title_id: actions.admitted(state) for title_id, state in states.items()}
    assert offered[FAILED] == ["retry", "retry_from", "abandon"]
    assert offered[1_000_000_805] == offered[1_000_000_806] == []


async def test_the_three_actions_answer_with_the_job_or_refuse_with_the_sentence(app, db):
    await _title(db, FAILED)
    await _job(db, FAILED, stage=3, status=pipeline.FAILED, reason="a stage raised")
    await _task(db, FAILED, f"title:{FAILED}", state=queue.FAILED, attempts=4)
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting on reviews")
    await _title(db, OTHER)
    admin, _member = await household(app)

    retried = await admin.post(f"/api/admin/acquisition/{FAILED}/retry")
    assert retried.status_code == 200, retried.text
    job = retried.json()["job"]
    assert (job["stage"], job["status"], job["actions"]) == (3, "queued", [])
    assert job["reason"].startswith("retried from the admin board: resumes at stage 3 (derive)")

    plain = await admin.post(f"/api/admin/acquisition/{PARKED}/retry")
    assert plain.status_code == 409 and "decision 336" in plain.json()["detail"], plain.text
    forward = await admin.post(f"/api/admin/acquisition/{PARKED}/retry-from", json={"stage": 9})
    assert forward.status_code == 409 and "never moves a job forward" in forward.json()["detail"]

    abandoned = await admin.post(f"/api/admin/acquisition/{PARKED}/abandon")
    assert abandoned.status_code == 200, abandoned.text
    assert (abandoned.json()["job"]["status"], abandoned.json()["job"]["actions"]) == (
        "abandoned", ["retry_from"]
    )
    back = await admin.post(f"/api/admin/acquisition/{PARKED}/retry-from", json={"stage": 2})
    assert back.status_code == 200, back.text
    assert (back.json()["job"]["stage"], back.json()["job"]["status"]) == (2, "queued")

    missing = await admin.post(f"/api/admin/acquisition/{OTHER}/abandon")
    assert missing.status_code == 404 and missing.json()["detail"] == actions.NO_JOB


@pytest.mark.parametrize(("path", "body"), [
    (f"/api/admin/acquisition/{PARKED}/retry", None),
    (f"/api/admin/acquisition/{PARKED}/retry-from", {"stage": 2}),
    (f"/api/admin/acquisition/{PARKED}/abandon", None),
])
async def test_the_three_actions_refuse_a_member_and_a_stranger(app, db, path, body):
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting")
    _admin, member = await household(app)
    before = await _snapshot(db)

    assert (await member.post(path, json=body)).status_code == 403
    assert (await app().post(path, json=body)).status_code == 401
    assert await _snapshot(db) == before
