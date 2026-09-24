"""§6.6's board actions: retry, retry from stage N and abandon, in the domain and over HTTP.

Spec v2.1 §8 ("Failure at any stage parks the job with a reason, retryable from admin; paid stages
(6) never auto-retry past the spend cap"), §6.6 Data's board clause as v2.1.3 amends it; decisions
336, 421, 424, 431, 442 and 444.

WHAT IS ASSERTED, IN THE ORDER THE PLAN'S EXIT TABLE NAMES IT (check 9, "retry from stage 4 |
resumes at 4; no re-fetch; no duplicated derived row"):

  * A job parked at the reviews gate and retried from stage 4 re-enters at 4 through the real drain
    with stages 2-4 live over a canned web: no request reaches the transport, no HTTP client is
    built, no derived row is duplicated - and that holds with the thirty-day window still open and
    with it already closed, where the driver on its own would re-enter at stage 2 (decision 421)
    and only the retry's cleared `retry_after` keeps it at 4 (decision 424).
  * Decision 444's table: a plain retry is failed's alone (decision 336); abandon is parked's and
    failed's; a job in flight admits nothing; a stage past the one reached is refused.
  * What a revive writes: the failed-for-good mark removed so stage 6 stops parking the title's
    other keys (decision 431), and `_park_thin`'s inbox row - a board row with no task at all -
    given exactly one `title:<id>` task.
  * A walk holding the title, or a leased task, refuses every action with nothing written.
  * A retry whose first implemented stage is the paid one, over the month's cap, is refused with
    the meter's own sentence and changes nothing, while a retry from stage 2 of the same title is
    not - the coverage row's "refused with that reason rather than queued".
  * Over HTTP, each of the three POSTs by name, the refusals as 409 carrying the sentence, the
    board envelope's ten stage names and each job's admitted actions.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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
from tests.test_acquisition_board import _bootstrap

# §4.1's partition: the app's own ids start at 1e9. Written directly, as `test_acquisition_board.py`
# writes them, because the subject is the actions and not the mint.
PARKED = 1_000_000_801
FAILED = 1_000_000_802
INBOX = 1_000_000_803
OTHER = 1_000_000_804

THE_TEN = ("identify", "enrich", "derive", "reviews gate", "dna pack", "dna extract", "verify",
           "project", "place", "ready")


# `test_acquire_pipeline.py`'s three fixtures for a walk over a canned web, restated here over its
# helpers, which is how this suite shares a fixture between files.


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`DATA_DIR`, and so the raw store and the artifacts, under this test's own tmp_path."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().data_dir
    settings.cache_clear()


@pytest.fixture
async def bundled(db, data_dir, tmp_path):
    """A real bundle, imported and active, so the walk has a basis past stage 8."""
    root = fx.make_bundle(tmp_path / "bundle")
    report = await bundle_import.import_bundle(
        db, bundle_import.Bundle.open(root), settings().artifacts_dir
    )
    assert report.ok, report.render()
    return report


@pytest.fixture
async def keyed(db, secrets_key):
    """TMDB and Trakt configured where §2 puts them, which stage 2 needs to ask anything."""
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
    """Both tables whole, for "nothing changed": a refused action rolls back, updated_at included."""
    return (
        [dict(r) for r in await db.fetch("SELECT * FROM acquisition_job ORDER BY title_id")],
        [dict(r) for r in await db.fetch("SELECT * FROM acquisition_task ORDER BY id")],
    )


async def _refused(call) -> str:
    with pytest.raises(actions.ActionRefused) as refusal:
        await call
    return refusal.value.reason


# --- exit check 9: a retry from stage 4 resumes at 4 and asks nothing ----------------------------


@pytest.mark.parametrize("window", ["open", "closed"])
async def test_a_job_parked_at_the_reviews_gate_retried_from_four_resumes_there_and_asks_nothing(
    db, bundled, keyed, window
):
    """Exit check 9, through the board's action rather than the hand-made due date the M5.3 check
    used. The first walk crawls a canned web and parks at the reviews gate with one source; the
    retry from stage 4 writes the board `queued` at 4 with `retry_after` cleared; the drain that
    follows re-enters at `reviews gate`, the request log stays empty, the fetcher factory is never
    called and the derived rows are the count they were.

    WITH THE WINDOW CLOSED TOO, which is the case the cleared `retry_after` exists for: the driver
    alone re-enters a park whose own deadline has passed at `reask_from`, stage 2, and asks the
    sources again (decision 421). An operator's retry from stage 4 is not that event, and decision
    424 says a retry resumes at the stage it names - so the retry, and nothing else, keeps it at 4.
    """
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


# --- decision 444's table --------------------------------------------------------------------------


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
    """Decision 336: `failed` "is the only state offering a plain retry". A parked job is waiting on
    something that may change, so its plain retry is refused with that sentence and nothing moves;
    the failed job is revived at the stage that failed - pending, attempts 0, due now, its error
    cleared - and its board says so."""
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
    """Decision 431 per title: stage 6 parks every other key of a title whose extraction failed for
    good, and `stages.FAILED_FOR_GOOD` tells the operator that key resumes once the failed task is
    retried. The retry revives both tasks and removes the mark, and stage 6 asked about the other
    key no longer parks it under that sentence. A cap with room is configured, because a retry at
    the paid stage asks the meter first; with no pack stored the meter lets it through (decision
    432) and stage 6 then parks on its own reasons, which are not this one."""
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
    """`placement/reconcile._park_thin` writes board rows at stage 2 with no queue task at all - the
    pipeline's inbox (decision 336). A retry from stage 2 gives it its `title:<id>` task, spelled as
    `pipeline.enqueue_title` spells it, pending and due now; a second retry once the job is walkable
    again revives that task rather than adding one."""
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
    """Decision 444: abandon closes the title's open tasks as skipped and writes the board's fourth
    outcome, which is neither parked nor failed - a thin-but-placed title abandoned from the board is
    never shown as broken. The reason keeps what the job had stopped with. A drain then leases
    nothing for it, and the drain's own board repairs leave it alone; a retry from a stage brings it
    back."""
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
    """Two writers of one board row is the state decision 322's lock exists to prevent, and an
    operator's click is a writer. A walk holding the title - the driver's own session lock, taken
    here on a second connection - or a task of the title leased refuses every action, with nothing
    written."""
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
    """Decision 444: an action refuses while any of the title's tasks is leased, and abandon closes
    the title's open tasks. A bare read of the task rows held neither: the drain's lease, landing in
    the round trips between that read and the abandon's write, took the task, the abandon closed
    nothing and committed `abandoned`, and the leased walk then overwrote the board and could go on to
    the paid stage the operator had meant to stop. The drain's lease is placed exactly there, on
    another connection, and finds the rows the action holds skipped. [M5.6 review cycle 1,
    M56-BOARD-LEASE-RACE]"""
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
    """What a SIGKILL, an OOM or the tick's budget leaves of one walk - its task leased past its
    expiry - and then the drain's own opening repairs, in `pipeline.drain`'s order."""
    (task,) = await queue.lease(db, [pipeline.TASK_KIND], limit=1)
    await db.execute(
        "UPDATE acquisition_task SET lease_expires = now() - interval '1 second' WHERE id = $1", task.id
    )
    await queue.reclaim_expired(db)
    await pipeline.close_abandoned_boards(db)
    await pipeline.complete_landed_boards(db)


@pytest.mark.parametrize("via", ["retry", "retry_from"])
async def test_a_retried_walk_that_dies_in_the_stage_it_resumed_at_is_closed_failed_and_retryable(db, via):
    """A retry writes the board `queued` at stage N (decision 444), and the board keeps it until the
    resumed walk advances past N. A walk that dies inside N on every attempt has its task closed by
    the reaper - and a board still reading `queued` beside it admitted no action, so it said "retried"
    for ever over a title nothing would walk again and no board control could reach. The reaper's
    board arm closes it `failed` like the two crash markers before it, keeping the retry's sentence,
    and the failed job admits a retry. [M5.6 review cycle 1, M56-BOARD-QUEUED-WEDGE]"""
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
    """A retry resumes at a stage the job reached and never moves it forward (decision 444)."""
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting")
    before = await _snapshot(db)

    for stage in (5, 0, 11):
        assert "never moves a job forward" in await _refused(actions.retry_from(db, PARKED, stage))
    assert await _snapshot(db) == before
    with pytest.raises(actions.NoJob):
        await actions.retry_from(db, OTHER, 1)


# --- the paid stage's retry against the cap -------------------------------------------------------


async def _at_the_cap(db, title_id: int) -> None:
    """A capped, planned install whose month is already at its cap: one metered attempt costing the
    whole of it, citing a raw document as 0028 requires (`test_llm_stage.py`'s arithmetic)."""
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
    """The coverage row's clause: a manual retry of a parked stage-6 job that would breach the cap
    "is refused with that reason rather than queued". A retry from 6, and from 5 - a declared no-op,
    so the first stage that RUNS is 6, read off the flags - is refused with `spend.retry_refusal`'s
    sentence, which is the board's own `over spend cap` reason, and neither table moves. A retry
    from 2 of the same title runs no paid stage first, is not the meter's to refuse, and is
    queued."""
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
    """Decision 442: the retry's pre-check reads the batch plan off the task a revive will walk, by
    the one reader stage 6 uses. A plan that does not read is refused as the batch's fault even on an
    install whose stored plan and cap are fine - where pricing the stored plan instead would queue a
    task the gate then parks under a different sentence."""
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


# --- over HTTP ------------------------------------------------------------------------------------


async def test_the_board_names_the_ten_stages_and_the_actions_each_job_admits(app, db):
    """The envelope carries §8's ten names out of the driver's tuple, and every job its admitted
    actions, so the surface renders what the server accepts rather than deriving it (decision
    444)."""
    states = {PARKED: pipeline.PARKED, FAILED: pipeline.FAILED, INBOX: actions.ABANDONED,
              OTHER: pipeline.READY, 1_000_000_805: actions.QUEUED, 1_000_000_806: pipeline.RUNNING}
    for title_id, state in states.items():
        await _title(db, title_id, f"title {title_id}")
        await _job(db, title_id, stage=4, status=state, reason=f"{state} for this test")
    admin, _member = await _bootstrap(app)

    payload = (await admin.get("/api/admin/acquisition")).json()

    assert payload["stages"] == [{"number": n, "name": name} for n, name in enumerate(THE_TEN, 1)]
    offered = {job["title_id"]: job["actions"] for job in payload["jobs"]}
    assert offered == {title_id: actions.admitted(state) for title_id, state in states.items()}
    assert offered[FAILED] == ["retry", "retry_from", "abandon"]
    assert offered[1_000_000_805] == offered[1_000_000_806] == []


async def test_the_three_actions_answer_with_the_job_or_refuse_with_the_sentence(app, db):
    """Each POST by name: a done action answers 200 with the job as it now stands and the actions
    its new state admits; a refusal is 409 with the domain's sentence as the detail, verbatim; a
    title the pipeline never touched is 404."""
    await _title(db, FAILED)
    await _job(db, FAILED, stage=3, status=pipeline.FAILED, reason="a stage raised")
    await _task(db, FAILED, f"title:{FAILED}", state=queue.FAILED, attempts=4)
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting on reviews")
    await _title(db, OTHER)
    admin, _member = await _bootstrap(app)

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
    """Named in full, for `test_route_inventory.py`'s rule that every route a test names; the
    refusals are `AdminUser`'s, and nothing is written either way."""
    await _title(db, PARKED)
    await _job(db, PARKED, stage=4, status=pipeline.PARKED, reason="waiting")
    _admin, member = await _bootstrap(app)
    before = await _snapshot(db)

    assert (await member.post(path, json=body)).status_code == 403
    assert (await app().post(path, json=body)).status_code == 401
    assert await _snapshot(db) == before
