"""§6.6's System card (decisions 182, 454). The fingerprint must never be the key, an unreadable key
is reported rather than raised, and the log panel carries no credential. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from spielplan import worker
from spielplan.acquire import queue
from spielplan.api import admin as admin_api
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.core import logs
from spielplan.core import secrets as sec
from spielplan.core.config import settings
from spielplan.sync import playback
from spielplan.sync import seen as seen_sync
from tests.helpers import admin_client, member_client

BACKUP = admin_api.BACKUP_JOB


async def _run(db, name: str, *, ok: bool | None, ago: timedelta, detail=None) -> None:
    started = datetime.now(UTC) - ago
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        name,
        started,
        None if ok is None else started + timedelta(seconds=2),
        ok,
        detail,
    )


async def _card(admin) -> dict:
    got = await admin.get("/api/admin/system")
    assert got.status_code == 200, got.text
    return got.json()


async def _fire(name: str) -> None:
    """One job as `worker._tick` fires it, so a `job_run` row here is one the worker really writes."""
    job = next(j for j in worker.JOBS if j.name == name)
    run_id = await worker._record_start(name)
    try:
        detail = await job.run()
    except Exception as exc:  # noqa: BLE001 - `_tick`'s own arm
        await worker._record_finish(run_id, ok=False, detail={"error": f"{type(exc).__name__}: {exc}"})
    else:
        await worker._record_finish(run_id, ok=True, detail=detail)


@pytest.fixture
async def hangs_up(monkeypatch):
    """A URL that accepts and closes: fails through real
    httpx and h11; the once-per-outage memos are reset."""
    monkeypatch.setattr(seen_sync._outage, "since", None)
    monkeypatch.setattr(playback._outage, "since", None)

    async def close(reader, writer) -> None:
        writer.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


def test_the_fingerprint_is_short_stable_and_not_the_key():
    """§14.3: prove *which* key is loaded, never what it is; stable, since operators compare by eye."""
    key = "an-operators-secrets-key-not-a-real-one"
    fingerprint = sec.key_fingerprint(key)

    assert len(fingerprint) == 12
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert sec.key_fingerprint(key) == fingerprint
    assert key not in fingerprint
    # Nor any window of the key: a truncated digest leaking four characters would still be a leak.
    assert not any(key[i : i + 4] in fingerprint for i in range(len(key) - 3))


def test_two_keys_have_two_fingerprints():
    assert sec.key_fingerprint("the-key-that-was-current-for-the-dump") != sec.key_fingerprint(
        "the-key-the-operator-regenerated-after"
    )


async def test_the_card_reports_seven_facts_and_no_more(secrets_key, db, app):
    """An equality on the key set, so an eighth key arrives here before it arrives on any screen."""
    admin = await admin_client(app)
    card = await _card(admin)

    assert sorted(card) == [
        "acquisition", "backup", "jobs", "last_syncs", "logs", "queue", "secrets"
    ]
    assert sorted(card["secrets"]) == ["configured", "fingerprint", "key_id", "unreadable"]
    assert sorted(card["backup"]) == ["at", "bytes", "stale", "stale_after_hours"]
    assert sorted(card["queue"]) == ["by_kind", "by_state"]
    assert sorted(card["logs"]) == ["records", "scope", "since"]
    assert card["logs"]["scope"] == "web process"
    assert [sync["name"] for sync in card["last_syncs"]] == list(admin_api.SYNC_JOBS)
    assert all(sorted(sync) == ["at", "connector", "detail", "name"] for sync in card["last_syncs"])


async def test_queue_depth_is_reported_by_state_and_by_kind(secrets_key, db, app):
    """Every allowed state is present, zero included, so "nothing failed" is a printed 0."""
    admin = await admin_client(app)
    for key in ("jellyfin:a1", "jellyfin:b2", "jellyfin:c3"):
        await queue.enqueue(db, "acquire", key)
    await queue.enqueue(db, "enrich", "t:1000000001")
    await db.execute("UPDATE acquisition_task SET state = 'failed' WHERE key = 'jellyfin:c3'")
    await db.execute("UPDATE acquisition_task SET state = 'done' WHERE key = 't:1000000001'")

    depth = (await _card(admin))["queue"]

    assert depth["by_kind"] == [
        {"kind": "acquire", "state": "failed", "count": 1},
        {"kind": "acquire", "state": "pending", "count": 2},
        {"kind": "enrich", "state": "done", "count": 1},
    ]
    assert depth["by_state"] == {"pending": 2, "leased": 0, "done": 1, "failed": 1, "skipped": 0}


async def test_the_acquisition_board_is_counted_by_status_zeros_included(secrets_key, db, app):
    """Overview's parked and failed titles (decision 527): every status printed, none dropped."""
    admin = await admin_client(app)
    for title_id, status in ((1000000101, "parked"), (1000000102, "parked"), (1000000103, "failed")):
        await db.execute(
            "INSERT INTO title (id, kind, name, year, origin) VALUES ($1, 'movie', $2, 2016, 'acquired')",
            title_id, f"Title {title_id}",
        )
        await db.execute(
            "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 2, $2)", title_id, status
        )

    counts = (await _card(admin))["acquisition"]

    assert counts == {
        "queued": 0, "running": 0, "parked": 2, "ready": 0, "failed": 1, "abandoned": 0
    }


async def test_a_last_sync_is_the_newest_successful_run_not_the_newest_run(secrets_key, db, app):
    """Newest SUCCESSFUL: a job that never succeeded is a null, not dropped."""
    admin = await admin_client(app)
    # A refused key is not a failed row: `seen.sync_all` returns a report for it rather than raising.
    await _run(db, "jellyfin-seen-sync", ok=True, ago=timedelta(hours=5),
               detail={"pushed": 2, "reached": True})
    await _run(db, "jellyfin-seen-sync", ok=False, ago=timedelta(hours=1),
               detail={"error": "abandoned: no result within this job's 600s budget"})
    await _run(db, "jellyfin-seen-sync", ok=None, ago=timedelta(minutes=3))
    await _run(db, "jellyfin-delta-poll", ok=False, ago=timedelta(minutes=9),
               detail={"error": "GET /Items -> 400"})

    card = await _card(admin)
    syncs = {sync["name"]: sync for sync in card["last_syncs"]}

    seen = syncs["jellyfin-seen-sync"]
    assert seen["connector"] == "Jellyfin"
    assert seen["detail"] == {"pushed": 2, "reached": True}
    assert timedelta(hours=4) < datetime.now(UTC) - datetime.fromisoformat(seen["at"]) < timedelta(
        hours=6
    )
    assert syncs["jellyfin-delta-poll"]["at"] is None
    assert syncs["jellyfin-delta-poll"]["detail"] is None
    assert syncs["acquisition-drain"]["at"] is None
    # `jobs` still reports the newest run: the two keys are two different rows.
    jobs = {job["name"]: job for job in card["jobs"]}
    assert jobs["jellyfin-seen-sync"]["ok"] is None


def test_every_sync_job_is_a_job_the_card_already_reports():
    """A name that drifted from the registry would read "never succeeded" for ever."""
    assert admin_api.SYNC_JOBS, "no sync job is named, so last_syncs asks nothing"
    assert set(admin_api.SYNC_JOBS) <= set(admin_api.JOB_NAMES)
    assert admin_api.BACKUP_JOB not in admin_api.SYNC_JOBS


async def _poll_status(admin) -> dict:
    got = await admin.get("/api/admin/connectors/jellyfin")
    assert got.status_code == 200, got.text
    return got.json()["trigger"]["delta_poll"]


async def test_a_sync_job_that_asked_no_server_is_no_last_sync(secrets_key, db, app):
    """The worker records `ok` for a run that asked nobody, which is no sync."""
    admin = await admin_client(app)
    for name in admin_api.SYNC_JOBS:
        await _fire(name)
    assert await db.fetchval("SELECT count(*) FROM job_run WHERE ok") == len(admin_api.SYNC_JOBS)

    syncs = {sync["name"]: sync["at"] for sync in (await _card(admin))["last_syncs"]}
    assert syncs == dict.fromkeys(admin_api.SYNC_JOBS), syncs
    poll = await _poll_status(admin)
    assert (poll["last_run_at"], poll["last_run_ok"], poll["last_ok_at"]) == (None, None, None), poll


async def test_a_sync_job_that_could_not_reach_its_server_is_no_last_sync(
    secrets_key, db, app, hangs_up
):
    """The seen sync and sessions poll swallow an outage and close ok; those are not syncs."""
    admin = await admin_client(app)
    saved = await admin.put("/api/admin/connectors/jellyfin",
                            json={"url": hangs_up, "api_key": "a-jellyfin-key-for-a-server-that-is-down"})
    assert saved.status_code == 200, saved.text
    for name in admin_api.SYNC_JOBS:
        await _fire(name)
    oks = {r["name"] for r in await db.fetch("SELECT name FROM job_run WHERE ok")}
    assert {"jellyfin-seen-sync", "jellyfin-sessions-poll"} <= oks, "the outage was not swallowed"

    syncs = {sync["name"]: sync["at"] for sync in (await _card(admin))["last_syncs"]}
    assert syncs == dict.fromkeys(admin_api.SYNC_JOBS), syncs
    poll = await _poll_status(admin)
    assert (poll["last_run_ok"], poll["last_ok_at"]) == (False, None), poll


async def test_a_sync_job_that_reached_its_server_is_a_last_sync(
    secrets_key, db, app, fake_jellyfin, monkeypatch
):
    module, transport = fake_jellyfin
    admin = await admin_client(app)
    await registry.save_jellyfin(db, url="http://jellyfin.test", api_key=module.API_KEY)
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(cfg.url, cfg.api_key, transport=transport) if cfg.configured else None,
    )
    for name in admin_api.SYNC_JOBS:
        await _fire(name)

    syncs = {sync["name"]: sync["at"] for sync in (await _card(admin))["last_syncs"]}
    reached = {"jellyfin-seen-sync", "jellyfin-sessions-poll", "jellyfin-delta-poll"}
    assert {name for name, at in syncs.items() if at is not None} == reached, syncs
    poll = await _poll_status(admin)
    assert poll["last_run_ok"] is True and poll["last_ok_at"] is not None, poll


async def test_a_fortnight_offline_does_not_turn_the_last_sync_into_never(secrets_key, db, app):
    """The prune must keep the newest row that reached the server, not merely the newest ok row."""
    admin = await admin_client(app)
    await _run(db, "jellyfin-seen-sync", ok=True, ago=timedelta(days=20),
               detail={"pushed": 1, "reached": True})
    for day in range(19, 0, -1):
        await _run(db, "jellyfin-seen-sync", ok=True, ago=timedelta(days=day),
                   detail={"pushed": 0, "reached": False})

    await worker._prune_job_runs()

    syncs = {sync["name"]: sync for sync in (await _card(admin))["last_syncs"]}
    assert syncs["jellyfin-seen-sync"]["at"] is not None, "the prune took the last sync that reached"
    assert syncs["jellyfin-seen-sync"]["detail"] == {"pushed": 1, "reached": True}
    # The nights inside the window, and the one sweep that reached: the exemption is one row.
    inside = worker.JOB_RUN_KEEP_DAYS - 1
    left = await db.fetchval("SELECT count(*) FROM job_run WHERE name = 'jellyfin-seen-sync'")
    assert left == inside + 1, "the prune kept more than the window and the row that reached"


def test_a_log_line_loses_every_credential_a_connector_could_have_written_into_it():
    """The names around each value survive; only the value goes."""
    lines = {
        "GET https://api.themoviedb.org/3/movie/7?api_key=TMDBKEY123&language=en": "TMDBKEY123",
        "GET https://www.omdbapi.com/?i=tt0111161&apikey=OMDBKEY456": "OMDBKEY456",
        "GET https://h.test/x?key=PLAINKEY1&token=PLAINTOKEN2&secret=PLAINSECRET3": "PLAIN",
        "GET https://h.test/cb?access_token=ACCESS789#frag": "ACCESS789",
        "provider said: Authorization: Bearer sk-ant-BEARER000 was refused": "BEARER000",
        "headers {'x-api-key': 'sk-ant-HEADER111', 'anthropic-version': '2023-06-01'}": "HEADER111",
        "delivery with X-Spielplan-Token: WEBHOOKTOKEN222 refused": "WEBHOOKTOKEN222",
        "X-Emby-Token=EMBY333 and MediaBrowser Token=\"MEDIABROWSER444\", Client=\"x\"": "333",
        # h11 quotes a refused header value as a bytes repr and names no header.
        "jellyfin is unreachable: GET /Items failed: Illegal header value b'H11KEY555 '": "H11KEY555",
        "GET /Users failed: Illegal header value b\"H11KEY666 it's\"": "H11KEY666",
    }
    for line, secret in lines.items():
        redacted = logs.redact(line)
        assert secret not in redacted, redacted
        assert "https://" not in line or redacted.startswith("GET https://"), redacted
    assert "MEDIABROWSER444" not in logs.redact(
        "X-Emby-Token=EMBY333 and MediaBrowser Token=\"MEDIABROWSER444\", Client=\"x\""
    )
    assert logs.redact("keyboard shortcuts and the tokenizer are fine") == (
        "keyboard shortcuts and the tokenizer are fine"
    )
    assert len(logs.redact("x" * 5000)) <= logs.MESSAGE_LIMIT + 3


def test_the_ring_hangs_off_the_spielplan_logger_and_no_other():
    """`httpx` and `uvicorn` write URLs and query strings this app cannot prove redacted."""
    import spielplan.app  # noqa: F401 - the install is app.py's, at import

    handler = logs.HANDLER
    assert handler in logging.getLogger("spielplan").handlers, "app.py did not install the ring"
    for other in ("", "httpx", "uvicorn", "uvicorn.access", "uvicorn.error"):
        assert handler not in logging.getLogger(other).handlers, other
    assert handler.level == logging.INFO
    logs.install()
    assert logging.getLogger("spielplan").handlers.count(handler) == 1, "install is not idempotent"


async def test_the_recent_log_lines_are_the_web_process_own_and_carry_no_key(secrets_key, db, app):
    """The whole serialised body is searched, so a field added later is caught."""
    admin = await admin_client(app)
    logging.getLogger("spielplan.test").warning(
        "GET https://h.test/3/movie?api_key=SECRETXYZ&x=1 marker-4471 answered 401"
    )
    logging.getLogger("spielplan.test").warning("marker-4472 sent Bearer abc-bearer-value-9913")
    logging.getLogger("httpx").warning("marker-4473 HTTP Request: GET https://h.test/?api_key=Q")
    logging.getLogger("uvicorn.error").warning("marker-4474 from the server")

    got = await admin.get("/api/admin/system")

    assert got.status_code == 200, got.text
    records = got.json()["logs"]["records"]
    mine = [r for r in records if "marker-4471" in r["message"]]
    assert len(mine) == 1
    assert mine[0]["level"] == "WARNING"
    assert mine[0]["logger"] == "spielplan.test"
    assert "https://h.test/3/movie?api_key=" in mine[0]["message"]
    assert datetime.now(UTC) - datetime.fromisoformat(mine[0]["at"]) < timedelta(minutes=1)
    assert any("marker-4472" in r["message"] for r in records)
    assert "SECRETXYZ" not in got.text
    assert "abc-bearer-value-9913" not in got.text
    assert "marker-4473" not in got.text, "an httpx record reached the card"
    assert "marker-4474" not in got.text, "a uvicorn record reached the card"
    assert got.json()["logs"]["since"] is not None


async def test_the_ring_holds_two_hundred_lines_however_many_are_written(secrets_key, db, app):
    """Bounded at 200 and newest last."""
    admin = await admin_client(app)
    chatty = logging.getLogger("spielplan.test.chatty")
    for n in range(10_000):
        chatty.warning("line %d of a loop that logs too much", n)

    held = logs.snapshot()["records"]
    assert len(held) == logs.CAPACITY == 200
    assert held[-1]["message"] == "line 9999 of a loop that logs too much"
    assert held[0]["message"] == "line 9800 of a loop that logs too much"
    assert len((await _card(admin))["logs"]["records"]) == 200


async def test_a_jellyfin_key_pasted_with_a_space_is_stored_trimmed_and_printed_nowhere(
    secrets_key, db, app, hangs_up
):
    """A double-clicked key carries a trailing space, which h11 quotes whole into every error."""
    key = "JFYNkey0p9o8i7u6y5t4r3e2w1q"
    admin = await admin_client(app)

    def carries(text: str) -> bool:
        return any(key[i : i + 6] in text for i in range(len(key) - 5))

    saved = await admin.put("/api/admin/connectors/jellyfin",
                            json={"url": hangs_up, "api_key": f" {key}\t "})
    assert saved.status_code == 200, saved.text
    assert (await registry.load_jellyfin(db)).api_key == key, "the key was stored with its padding"
    kept = await admin.put("/api/admin/connectors/jellyfin", json={"api_key": "   "})
    assert kept.status_code == 200, kept.text
    assert (await registry.load_jellyfin(db)).api_key == key, "a blank field replaced the key"
    for inner in (f"{key[:10]} {key[10:]}", f"{key}\u200b"):
        refused = await admin.put("/api/admin/connectors/jellyfin", json={"api_key": inner})
        assert refused.status_code == 422, refused.text
        assert not carries(refused.text), refused.text
    assert (await registry.load_jellyfin(db)).api_key == key

    await registry.save_jellyfin(db, api_key=f"{key} ")
    bodies = []
    libraries = await admin.get("/api/admin/connectors/jellyfin/libraries")
    assert libraries.status_code == 200 and libraries.json()["ok"] is False, libraries.text
    bodies.append(libraries.text)
    for method, path in (("POST", "/api/admin/connectors/jellyfin/test"),
                         ("GET", "/api/admin/connectors/jellyfin/users"),
                         ("POST", "/api/admin/connectors/jellyfin/sync"),
                         ("POST", "/api/admin/connectors/jellyfin/poll"),
                         ("GET", "/api/admin/connectors/jellyfin")):
        bodies.append((await admin.request(method, path)).text)
    card = await admin.get("/api/admin/system")
    bodies.append(card.text)
    unreachable = [r for r in card.json()["logs"]["records"] if r["logger"] == "spielplan.sync.seen"]
    assert unreachable, "the sync's WARNING never reached the ring, so the ring was not searched"
    for text in bodies:
        assert not carries(text), text


def test_a_record_that_cannot_be_formatted_never_raises_into_the_caller():
    """Through the handler directly: pytest's root capture handler re-raises formatting errors by design."""
    logs.install()
    record = logging.LogRecord(
        "spielplan.test", logging.WARNING, __file__, 1, "%d is not a number", ("nope",), None
    )
    before = logs.snapshot()["records"]

    logs.HANDLER.handle(record)

    assert logs.snapshot()["records"] == before


def test_the_system_card_declares_a_read_and_nothing_else():
    """Asked of the app's own schema over every path under the prefix."""
    from spielplan.app import app

    operations = {
        (path, method)
        for path, methods in app.openapi()["paths"].items()
        if path == "/api/admin/system" or path.startswith("/api/admin/system/")
        for method in methods
    }
    assert operations == {("/api/admin/system", "get")}


async def test_the_card_never_hands_the_secrets_key_to_the_browser(secrets_key, db, app):
    """The whole serialised response, so a later config echo or debug dump is caught."""
    admin = await admin_client(app)
    got = await admin.get("/api/admin/system")

    assert got.status_code == 200
    assert secrets_key not in got.text
    assert got.json()["secrets"]["fingerprint"] == sec.key_fingerprint(secrets_key)
    assert got.json()["secrets"]["configured"] is True


async def test_the_card_reports_the_newest_run_of_each_job(secrets_key, db, app):
    """The older rows stay: the card is a projection, not a delete."""
    admin = await admin_client(app)
    await _run(db, "fold-in-tick", ok=True, ago=timedelta(hours=3), detail={"users": 1})
    await _run(db, "fold-in-tick", ok=False, ago=timedelta(minutes=2), detail={"error": "boom"})
    await _run(db, "session-prune", ok=True, ago=timedelta(minutes=30), detail=None)

    jobs = {job["name"]: job for job in (await _card(admin))["jobs"]}

    assert sorted(jobs) == ["fold-in-tick", "session-prune"]
    assert jobs["fold-in-tick"]["ok"] is False
    assert jobs["fold-in-tick"]["detail"] == {"error": "boom"}
    # A prune reports nothing beyond having run, and a null `detail` is still job health.
    assert jobs["session-prune"]["ok"] is True
    assert jobs["session-prune"]["detail"] is None


async def test_a_job_that_started_and_never_finished_is_reported_as_unfinished(
    secrets_key, db, app
):
    """`ok IS NULL` is a crash, not a job failure; collapsing it sends the operator after an exception."""
    admin = await admin_client(app)
    await _run(db, "nightly-backup", ok=None, ago=timedelta(minutes=5))

    job = (await _card(admin))["jobs"][0]

    assert job["name"] == "nightly-backup"
    assert job["finished_at"] is None
    assert job["ok"] is None


async def test_the_backup_fact_is_the_newest_successful_dump_not_the_newest_attempt(
    secrets_key, db, app
):
    """`jobs` says the last attempt failed; `backup` says when a dump last succeeded."""
    admin = await admin_client(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=10), detail={"bytes": 4096, "kept": 14})
    await _run(db, BACKUP, ok=False, ago=timedelta(minutes=5), detail={"error": "no pg_dump"})

    card = await _card(admin)

    assert card["jobs"][0]["ok"] is False
    assert card["backup"]["bytes"] == 4096
    assert card["backup"]["stale"] is False
    assert (datetime.now(UTC) - datetime.fromisoformat(card["backup"]["at"])) < timedelta(
        hours=11
    )


@pytest.mark.parametrize(
    ("hours", "stale"),
    [(9, False), (35, False), (37, True)],
)
async def test_a_dump_is_stale_once_it_is_older_than_a_night_and_a_half(
    secrets_key, db, app, hours, stale
):
    """Asserted from both sides: a threshold tested only where obviously true is a constant."""
    admin = await admin_client(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=hours), detail={"bytes": 1})

    card = await _card(admin)

    assert card["backup"]["stale"] is stale
    assert card["backup"]["stale_after_hours"] == 36


async def test_an_install_that_has_never_completed_a_dump_is_stale(secrets_key, db, app):
    """"No backup yet" and "no backup since Tuesday" are one problem to whoever needs one."""
    admin = await admin_client(app)
    await _run(db, BACKUP, ok=False, ago=timedelta(minutes=1), detail={"error": "no pg_dump"})

    card = await _card(admin)

    assert card["backup"]["at"] is None
    assert card["backup"]["bytes"] is None
    assert card["backup"]["stale"] is True


async def test_a_fortnight_of_failures_does_not_turn_the_last_good_dump_into_never(
    secrets_key, db, app
):
    """Pruned by age alone, the last good row vanishes and the card claims no dump ever completed."""
    admin = await admin_client(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(days=15), detail={"bytes": 41235968})
    for night in range(13, 0, -1):
        await _run(db, BACKUP, ok=False, ago=timedelta(days=night), detail={"error": "no space"})

    await worker._prune_job_runs()
    card = await _card(admin)

    assert card["backup"]["at"] is not None, (
        "the prune deleted the last successful dump, so the card says none has ever completed"
    )
    assert card["backup"]["bytes"] == 41235968
    assert card["backup"]["stale"] is True, "fifteen days is stale by any reading"
    assert card["jobs"][0]["ok"] is False, "the newest attempt is still the failure it was"


async def test_a_household_whose_worker_has_not_run_yet_is_named_no_jobs(secrets_key, db, app):
    """Newest row per *registered* job, inner join: a fresh boot is no rows, not twelve rows of nulls."""
    admin = await admin_client(app)
    await _run(db, "a-job-this-build-does-not-have", ok=True, ago=timedelta(minutes=1))

    card = await _card(admin)

    assert card["jobs"] == []
    assert card["backup"]["at"] is None and card["backup"]["stale"] is True


async def test_the_data_tab_payload_does_not_carry_this_cards_facts(secrets_key, db, app):
    """A second copy of the 36-hour rule is how two screens start disagreeing about one dump."""
    admin = await admin_client(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=40), detail={"bytes": 77})

    state = await admin.get("/api/admin/bundle/state")

    assert state.status_code == 200, state.text
    assert "jobs" not in state.json() and "backup" not in state.json()
    assert (await _card(admin))["backup"]["bytes"] == 77, "this card is where it is reported"


async def test_a_wrong_secrets_key_is_reported_rather_than_raised(
    secrets_key, db, app, monkeypatch
):
    """A restore whose `.env` did not come with it: this route must *name* the state."""
    admin = await admin_client(app)
    before = await _card(admin)
    assert before["secrets"]["unreadable"] is False
    key_id = before["secrets"]["key_id"]
    assert key_id is not None

    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    after = await _card(admin)

    assert after["secrets"]["unreadable"] is True
    # The row did not move: the ciphertexts still name this key_id.
    assert after["secrets"]["key_id"] == key_id
    assert after["secrets"]["fingerprint"] != before["secrets"]["fingerprint"]


async def test_the_connectors_repair_does_not_heal_this_card_while_a_secret_is_still_sealed(
    secrets_key, db, app, monkeypatch
):
    """The question is "is anything unopenable", not "does the active DEK row unwrap"."""
    admin = await admin_client(app)
    saved = await admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "JF-ADMIN-KEY-UNSCOPED"},
    )
    assert saved.status_code == 200, saved.text
    retired = (await _card(admin))["secrets"]["key_id"]

    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    assert (await _card(admin))["secrets"]["unreadable"] is True

    repaired = await admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "a-freshly-issued-jellyfin-key"},
    )
    assert repaired.status_code == 200, repaired.text
    assert (await admin.get("/api/admin/connectors/jellyfin")).json()["secrets_unreadable"] is False

    card = await _card(admin)
    assert card["secrets"]["key_id"] != retired, "the repair minted a fresh DEK"
    assert card["secrets"]["unreadable"] is True, (
        "app_setting/push.vapid is still sealed under the retired key"
    )
    assert await db.fetchval(
        "SELECT secret_key_id FROM app_setting WHERE key = 'push.vapid'"
    ) == retired


async def test_an_install_with_no_secrets_key_reports_that_rather_than_a_fingerprint(
    no_secrets_key, db, app
):
    """`configured: false`, not a fingerprint of nothing."""
    admin = await admin_client(app)

    card = await _card(admin)

    assert card["secrets"]["configured"] is False
    assert card["secrets"]["fingerprint"] is None
    # Nothing minted a DEK, because nothing could: `ensure_dek` refuses without the key (§2).
    assert card["secrets"]["key_id"] is None
    assert card["secrets"]["unreadable"] is False


async def test_the_card_is_refused_to_a_signed_out_caller_and_to_a_member(secrets_key, db, app):
    admin = await admin_client(app)
    member = await member_client(app, admin)
    anonymous = app()

    assert (await anonymous.get("/api/admin/system")).status_code == 401
    assert (await member.get("/api/admin/system")).status_code == 403
