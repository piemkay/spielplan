"""§6.6's System card, at the three facts decision 182 gave it and the three decision 454 adds.

Spec v2.1 §6.6 (System), §2 (Backups, Configuration), §3.1, §14.3; decisions 181, 182, 454;
docs/milestones/M4.7-plan.md §2 findings 1 and 10; docs/milestones/M5.7-plan.md Phase E.

M4.7 gives `job_run` its rows and secrets custody a readable state, and before this route
nothing read either: `last_run` was an in-process dict, every report was logged once and
dropped, and the only signal that a month of nightly dumps had failed was one ERROR a day in a
container log with no timestamp. `ops-11`'s finding is not that the dump can fail — it is that
the household cannot find out.

Two properties are worth more than the rest and are asserted hardest here:

  * **the fingerprint is a fingerprint.** §14.3 calls a Jellyfin API key "unscoped and
    admin-equivalent", and SECRETS_KEY is what opens the stored one. A card that answered "which
    key is this" with the key would be a worse disclosure than the one it exists to report.
  * **an unreadable key is reported, not raised.** The whole of dd03 is a custody problem that
    500s the routes that would describe it. This route is the description, so of all the routes
    in the app it is the one that must not.

M5.7 adds §6.6's remaining three - queue depth, last syncs and logs - and the property decision
454 is asserted hardest on is the log panel's: it is the one surface in the app that shows an
operator text a connector wrote, and a URL with a key in its query string is the text a connector
writes most readily (`push/send.py`'s filter exists for the same reason).

The e2e half is `e2e/specs/18-system.spec.js`: this file proves the payload against a key it
chose, that one proves an operator can find it. Skipped without TEST_DATABASE_URL; see
tests/conftest.py.
"""

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

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"
BACKUP = admin_api.BACKUP_JOB


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _member(app, admin):
    """A created account past §3.1's forced first-login change, so a 403 means the role."""
    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert made.status_code == 201, made.text
    otp = made.json()["one_time_password"]
    client = app()
    assert (
        await client.post("/api/auth/login", json={"name": "jenny", "password": otp})
    ).status_code == 200
    changed = await client.post(
        "/api/auth/password",
        json={"current_password": otp, "new_password": MEMBER_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    return client


async def _run(db, name: str, *, ok: bool | None, ago: timedelta, detail=None) -> None:
    """One `job_run` row, written the way `worker._record_start`/`_record_finish` leave it."""
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
    """One job as `worker._tick` fires it: the row opened, the registry's own body run, and the row
    closed ok with whatever it returned or failed with its reason -- so a `job_run` row here is one
    the worker really writes, not one a test supposed it would."""
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
    """A Jellyfin URL that accepts a connection and closes it: every read fails at once, through the
    real httpx and h11 path rather than a transport double. The two once-per-outage memos are reset
    so the WARNING an outage earns is written in this test whatever an earlier one left behind."""
    monkeypatch.setattr(seen_sync, "_unreachable_since", None)
    monkeypatch.setattr(playback, "_unreachable_since", None)

    async def close(reader, writer) -> None:
        writer.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


# --- the fingerprint, with no database in the way -------------------------------------


def test_the_fingerprint_is_short_stable_and_not_the_key():
    """§14.3: this route may prove *which* key is loaded and never what it is.

    Twelve lowercase hex characters, the same twelve every time — an operator comparing this
    install against the `.env` beside their dumps is doing a string comparison by eye, and a
    value that changed between two reads would answer no question at all.
    """
    key = "an-operators-secrets-key-not-a-real-one"
    fingerprint = sec.key_fingerprint(key)

    assert len(fingerprint) == 12
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert sec.key_fingerprint(key) == fingerprint
    assert key not in fingerprint
    # Nor any window of the key: a truncated digest that happened to leak four characters of a
    # short key would still be a leak, and this is the cheap way to say so.
    assert not any(key[i : i + 4] in fingerprint for i in range(len(key) - 3))


def test_two_keys_have_two_fingerprints():
    """The one thing the card claims: that this value distinguishes one `.env` from another."""
    assert sec.key_fingerprint("the-key-that-was-current-for-the-dump") != sec.key_fingerprint(
        "the-key-the-operator-regenerated-after"
    )


# --- the six facts, over HTTP ---------------------------------------------------------


async def test_the_card_reports_six_facts_and_no_more(secrets_key, db, app):
    """Decision 182's three - last successful backup, custody, newest `job_run` row per job - and
    decision 454's three: queue depth, last syncs and the web process's recent log lines.

    Still an equality on the key set rather than six presence checks. Decision 182 argued it for
    what stayed at M5; decision 454 closes §6.6's list, so the equality now says the card is
    exactly §6.6's five items and custody, and a seventh key - a control's state, a config echo -
    would arrive here before it arrived on any screen.
    """
    admin = await _admin(app)
    card = await _card(admin)

    assert sorted(card) == ["backup", "jobs", "last_syncs", "logs", "queue", "secrets"]
    assert sorted(card["secrets"]) == ["configured", "fingerprint", "key_id", "unreadable"]
    assert sorted(card["backup"]) == ["at", "bytes", "stale", "stale_after_hours"]
    assert sorted(card["queue"]) == ["by_kind", "by_state"]
    assert sorted(card["logs"]) == ["records", "scope", "since"]
    assert card["logs"]["scope"] == "web process"
    assert [sync["name"] for sync in card["last_syncs"]] == list(admin_api.SYNC_JOBS)
    assert all(sorted(sync) == ["at", "connector", "detail", "name"] for sync in card["last_syncs"])


# --- queue depth and last syncs (decision 454) ----------------------------------------


async def test_queue_depth_is_reported_by_state_and_by_kind(secrets_key, db, app):
    """§6.6's "queue depth", as M5.1's `queue.stats` counts it and with the totals beside it.

    Per kind AND state is `stats`' own grouping and the board's sentence ("nine identifies waiting,
    one extract failed"); the per-state totals are the line an operator reads first. Every state
    the table allows is present, zero included, so "nothing failed" is a 0 the card prints rather
    than a key it has to infer from an absence.
    """
    admin = await _admin(app)
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


async def test_a_last_sync_is_the_newest_successful_run_not_the_newest_run(secrets_key, db, app):
    """Decision 454: "newest successful" is what separates `last_syncs` from `jobs`.

    The install this exists for is the one whose seen-sync has been failing since Tuesday: `jobs`
    says the last attempt failed, and this says when the connector last actually answered. A newer
    failure and a newer run that never finished both leave the older success standing, and a job
    that has never succeeded is named with a null rather than dropped, because "never" is the
    answer an operator setting up Jellyfin needs to be able to read.
    """
    admin = await _admin(app)
    # Rows the worker writes: a sweep that read the library, and later one abandoned at its budget
    # -- a refused key is not one, because `seen.sync_all` returns a report for it rather than
    # raising (the tests below fire the real bodies for that). [M5.7 review cycle 1, M57-JFSYS-01]
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
    # `jobs` still reports the newest run whatever it says: the two keys are two different rows.
    jobs = {job["name"]: job for job in card["jobs"]}
    assert jobs["jellyfin-seen-sync"]["ok"] is None


def test_every_sync_job_is_a_job_the_card_already_reports():
    """`SYNC_JOBS` is a subset of `JOB_NAMES`, which `test_worker_registry.py` pins to the worker's
    registry. A name here that drifted from the registry would be a connector the card reports as
    "never succeeded" for ever, which is the one reading this list exists to make trustworthy."""
    assert admin_api.SYNC_JOBS, "no sync job is named, so last_syncs asks nothing"
    assert set(admin_api.SYNC_JOBS) <= set(admin_api.JOB_NAMES)
    assert admin_api.BACKUP_JOB not in admin_api.SYNC_JOBS


async def _poll_status(admin) -> dict:
    got = await admin.get("/api/admin/connectors/jellyfin")
    assert got.status_code == 200, got.text
    return got.json()["trigger"]["delta_poll"]


async def test_a_sync_job_that_asked_no_server_is_no_last_sync(secrets_key, db, app):
    """Decision 454's `last_syncs` is when each connector last ANSWERED, and the worker records
    `ok` for a run that asked nobody: on an install with no connector set up, `seen.sync_all` and
    `playback.poll` return a report and the intake jobs and the drain return nothing, and `_tick`
    closes every one of those rows ok. Read as successes, a fresh install's card said every
    connector had synced a minute ago, and the "never succeeded" it exists to be able to say could
    not be reached. The real bodies are fired here, as the loop fires them, and each connector is
    still "never" -- and the Jellyfin card's poll line says the poll has never run rather than "ok".
    [M5.7 review cycle 1, M57-JFSYS-01]"""
    admin = await _admin(app)
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
    """The install `last_syncs` was added for: Jellyfin down, the week-long outage `jobs` alone
    cannot date. The seen sync and the sessions poll catch the outage and return a report, so their
    rows close ok (§3.3: a degraded sync, never a broken app) -- and those are not syncs. Only the
    delta poll raises, and the card said the other three had synced a minute ago beside it.
    [M5.7 review cycle 1, M57-JFSYS-01]"""
    admin = await _admin(app)
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
    """The other half, so "never" is not simply what the card now always says: the same bodies
    against `ops/fake_jellyfin.py` read the library, the sessions and the delta, and each of those
    three is a last sync; the intake sweep with nothing ripe and the drain with nothing leased asked
    no server, and stay "never". [M5.7 review cycle 1, M57-JFSYS-01]"""
    module, transport = fake_jellyfin
    admin = await _admin(app)
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
    """`test_a_fortnight_of_failures_does_not_turn_the_last_good_dump_into_never`, for a sync. The seen
    sync swallows an outage and closes ok, so after a fortnight of a down server its newest ok row is
    one that reached nothing, and `job-run-prune`'s exemption of the newest ok row alone deleted the
    sweep that last read the library: "synced 20 days ago" became "never succeeded".
    [M5.7 review cycle 1, M57-JFSYS-01]"""
    admin = await _admin(app)
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


# --- the web process's recent log lines (decision 454) --------------------------------


def test_a_log_line_loses_every_credential_a_connector_could_have_written_into_it():
    """The redaction, over the spellings this codebase's own connectors produce.

    httpx writes a request's full URL, and TMDB and OMDb take their key in the query string
    (decision 453); Jellyfin's key rides in `X-Emby-Token` and the MediaBrowser header's `Token=`,
    the providers' in `Authorization: Bearer` and `x-api-key`, and §7.2's webhook token in
    `X-Spielplan-Token`. The names around each value survive, because "the TMDB request failed"
    is what the operator came to read; only the value goes.
    """
    lines = {
        "GET https://api.themoviedb.org/3/movie/7?api_key=TMDBKEY123&language=en": "TMDBKEY123",
        "GET https://www.omdbapi.com/?i=tt0111161&apikey=OMDBKEY456": "OMDBKEY456",
        "GET https://h.test/x?key=PLAINKEY1&token=PLAINTOKEN2&secret=PLAINSECRET3": "PLAIN",
        "GET https://h.test/cb?access_token=ACCESS789#frag": "ACCESS789",
        "provider said: Authorization: Bearer sk-ant-BEARER000 was refused": "BEARER000",
        "headers {'x-api-key': 'sk-ant-HEADER111', 'anthropic-version': '2023-06-01'}": "HEADER111",
        "delivery with X-Spielplan-Token: WEBHOOKTOKEN222 refused": "WEBHOOKTOKEN222",
        "X-Emby-Token=EMBY333 and MediaBrowser Token=\"MEDIABROWSER444\", Client=\"x\"": "333",
        # h11 refusing a header value quotes it as a bytes repr, in either quote, and names no
        # header -- the one spelling a padded Jellyfin key took into the ring. [M57-KEYS-C1-01]
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
    """Decision 454: the `spielplan` loggers, never the root, `httpx` or `uvicorn`.

    `httpx`'s INFO line is a full request URL for every call any connector makes, and `uvicorn`'s
    access log is every request path with its query string; both are written by code this app does
    not own, so a redaction list maintained here could never be proved complete over them. Only
    `app.py` installs it - the worker's lines stay in its container log, and the card says so.
    """
    import spielplan.app  # noqa: F401 - the install is app.py's, at import

    handler = logs.HANDLER
    assert handler in logging.getLogger("spielplan").handlers, "app.py did not install the ring"
    for other in ("", "httpx", "uvicorn", "uvicorn.access", "uvicorn.error"):
        assert handler not in logging.getLogger(other).handlers, other
    assert handler.level == logging.INFO
    logs.install()
    assert logging.getLogger("spielplan").handlers.count(handler) == 1, "install is not idempotent"


async def test_the_recent_log_lines_are_the_web_process_own_and_carry_no_key(secrets_key, db, app):
    """The card's log panel, end to end: a line a `spielplan` logger wrote is on it, redacted, and
    a line `httpx` wrote is not on it at all.

    The whole serialised body is searched rather than the record we meant, for the reason
    `test_the_card_never_hands_the_secrets_key_to_the_browser` gives: a field added later is
    caught by this and by nothing else.
    """
    admin = await _admin(app)
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
    """Bounded at 200 and newest last: a sync loop that fails every minute cannot grow the web
    process's memory, and the line the operator needs is the last one written."""
    admin = await _admin(app)
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
    """Plan §7 check 9 for the media-server key, which §14.3 makes admin-equivalent.

    Double-click a key in Jellyfin's API Keys table and the trailing space comes with it. Saved as
    typed, h11 refused it as a header value and quoted the whole of it -- `Illegal header value
    b'KEY '` -- into the error the library pick prints every time the Connectors page opens, the
    test button's answer, and, after one Sync now, the WARNING this ring holds for every admin
    session until the web process restarts. So the save trims it and refuses a key no header can
    carry without quoting it, and a key stored padded before that (read here straight into the row)
    is taken out of every message the client raises, whichever spelling the exception gave it.
    Searched as every six-character window, for `test_the_fingerprint_is_short_stable_and_not_the_key`'s
    reason. [M5.7 review cycle 1, M57-KEYS-C1-01]"""
    key = "JFYNkey0p9o8i7u6y5t4r3e2w1q"
    admin = await _admin(app)

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
    """A logging call that raised would fail the request or job that made it, so a record whose
    arguments do not fit its format is dropped from the ring rather than raised.

    Handed to the ring's handler directly: through the logger, pytest's own capture handler on
    the root re-raises a formatting error by design, which would test pytest rather than this."""
    logs.install()
    record = logging.LogRecord(
        "spielplan.test", logging.WARNING, __file__, 1, "%d is not a number", ("nope",), None
    )
    before = logs.snapshot()["records"]

    logs.HANDLER.handle(record)

    assert logs.snapshot()["records"] == before


def test_the_system_card_declares_a_read_and_nothing_else():
    """Plan E5 and decision 454: queue depth and logs are reads, and no control on this card starts
    a job, drains the queue or rotates a key. Asked of the app's own schema, over every path under
    the prefix, so a `POST /api/admin/system/drain` fails here the day it is written."""
    from spielplan.app import app

    operations = {
        (path, method)
        for path, methods in app.openapi()["paths"].items()
        if path == "/api/admin/system" or path.startswith("/api/admin/system/")
        for method in methods
    }
    assert operations == {("/api/admin/system", "get")}


async def test_the_card_never_hands_the_secrets_key_to_the_browser(secrets_key, db, app):
    """The assertion is over the whole serialised response, not over the field we meant.

    A field added later — a config echo, a debug dump of `Settings` — would be caught by this
    and by nothing else, and §14.3 is why that is worth a test rather than a code review.
    """
    admin = await _admin(app)
    got = await admin.get("/api/admin/system")

    assert got.status_code == 200
    assert secrets_key not in got.text
    assert got.json()["secrets"]["fingerprint"] == sec.key_fingerprint(secrets_key)
    assert got.json()["secrets"]["configured"] is True


async def test_the_card_reports_the_newest_run_of_each_job(secrets_key, db, app):
    """§6.6's "job health": one line per job, and it is the latest line.

    The older rows stay in the table — they are the history a later milestone's chart reads —
    so the card's contract is a projection, not a delete.
    """
    admin = await _admin(app)
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
    """`worker._record_start` opens the row before the call, so this row is a real state.

    A SIGKILL past the grace period, an OOM or a power cut leaves exactly this, and it is a
    more useful fact than no row at all — but only if the payload carries it rather than
    collapsing `ok IS NULL` into `ok = false`, which would report a crash as a job failure and
    send the operator looking for an exception that was never raised.
    """
    admin = await _admin(app)
    await _run(db, "nightly-backup", ok=None, ago=timedelta(minutes=5))

    job = (await _card(admin))["jobs"][0]

    assert job["name"] == "nightly-backup"
    assert job["finished_at"] is None
    assert job["ok"] is None


# --- the backup fact ------------------------------------------------------------------


async def test_the_backup_fact_is_the_newest_successful_dump_not_the_newest_attempt(
    secrets_key, db, app
):
    """The two keys are two different rows on precisely the install that needs this page.

    `jobs` says the last attempt failed; `backup` says when a dump last actually succeeded.
    Collapsing them would leave the household reading "nightly-backup: failed 5 minutes ago"
    with no way to learn that the last good dump is from Tuesday — or that there is none.
    """
    admin = await _admin(app)
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
    """36 hours: it cannot fire on a household whose dump ran at last night's anchor hour, and
    it does fire before a second night has been missed. The boundary is asserted from both
    sides, because a threshold tested only where it is obviously true is a constant."""
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=hours), detail={"bytes": 1})

    card = await _card(admin)

    assert card["backup"]["stale"] is stale
    assert card["backup"]["stale_after_hours"] == 36


async def test_an_install_that_has_never_completed_a_dump_is_stale(secrets_key, db, app):
    """"No backup yet" and "no backup since Tuesday" are one problem to whoever needs one."""
    admin = await _admin(app)
    await _run(db, BACKUP, ok=False, ago=timedelta(minutes=1), detail={"error": "no pg_dump"})

    card = await _card(admin)

    assert card["backup"]["at"] is None
    assert card["backup"]["bytes"] is None
    assert card["backup"]["stale"] is True


async def test_a_fortnight_of_failures_does_not_turn_the_last_good_dump_into_never(
    secrets_key, db, app
):
    """The card and the retention, on the one install that needs both.

    `job-run-prune` keeps a fortnight, which is §2's own rotation; the household whose dumps
    started failing is the household whose last successful row ages out of it. Deleted by age
    alone, that row's disappearance flips this card from "last successful backup: the 24th, 15
    days ago" plus the stale warning — the true and actionable answer — to the categorical
    "no dump has ever completed on this install", which the frontend renders as a sentence
    (`admin/system/+page.svelte`). The operator is then told backups were never set up, on a box
    whose `/data/backups` still holds fourteen restorable dumps, because `nightly.prune` rotates
    only after a dump that *worked*. Told the wrong problem, they go looking for a
    misconfiguration rather than for the disk that filled on the 24th.
    [M4.7 cycle 2 finding 7]
    """
    admin = await _admin(app)
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
    """The empty install, which is the one an operator meets first.

    The card asks for the newest row of each *registered* job rather than for the distinct names
    in the table, which is what keeps the read proportional to the twelve jobs instead of to
    every row the loop has ever written. That makes the join's kind load-bearing: an outer one
    would render twelve rows of nulls on a fresh boot and call it job health. And a row left by a
    job this build no longer has is history for a later milestone's chart, not health for now.
    """
    admin = await _admin(app)
    await _run(db, "a-job-this-build-does-not-have", ok=True, ago=timedelta(minutes=1))

    card = await _card(admin)

    assert card["jobs"] == []
    assert card["backup"]["at"] is None and card["backup"]["stale"] is True


async def test_the_data_tab_payload_does_not_carry_this_cards_facts(secrets_key, db, app):
    """Decision 182 chose this card; the Data tab's payload kept carrying `jobs` and `backup`.

    That spread was written while owner decision 2 was still open and its option (A) — "Data tab
    + Connectors card only" — was live. Decision 182 took option (B), and nothing on
    `frontend/src/routes/admin/data/+page.svelte` ever read either key: it renders `bundles`,
    `active`, `loaded`, `restart_required` and `rebuild_set`. A second copy of the 36-hour rule,
    on a route polled on a timer, is how two screens start telling one household different things
    about one dump — and the copy nobody reads is the one that drifts. [M4.7 ops-11; decision 182]
    """
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=40), detail={"bytes": 77})

    state = await admin.get("/api/admin/bundle/state")

    assert state.status_code == 200, state.text
    assert "jobs" not in state.json() and "backup" not in state.json()
    assert (await _card(admin))["backup"]["bytes"] == 77, "this card is where it is reported"


# --- custody --------------------------------------------------------------------------


async def test_a_wrong_secrets_key_is_reported_rather_than_raised(
    secrets_key, db, app, monkeypatch
):
    """dd03, at the one route whose job is to describe it.

    The scenario is a restore whose `.env` did not come with it: the DEK row is the one the
    first boot minted, and the key in the environment is not the key that wrapped it. Every
    other admin route degrades; this one has to *name* the state, because it is the screen the
    operator is on when they find out.
    """
    admin = await _admin(app)
    before = await _card(admin)
    assert before["secrets"]["unreadable"] is False
    key_id = before["secrets"]["key_id"]
    assert key_id is not None

    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    after = await _card(admin)

    assert after["secrets"]["unreadable"] is True
    # The row did not move — that is the point. The ciphertexts still name this key_id, so an
    # operator who finds the right `.env` has lost nothing.
    assert after["secrets"]["key_id"] == key_id
    assert after["secrets"]["fingerprint"] != before["secrets"]["fingerprint"]


async def test_the_connectors_repair_does_not_heal_this_card_while_a_secret_is_still_sealed(
    secrets_key, db, app, monkeypatch
):
    """The card answers "is anything unopenable", not "does the active DEK row unwrap".

    Those were one question until M4.7 gave the Connectors card its repair. Pasting the API key
    over an unreadable DEK retires that row and seals under a fresh one, so the *active* row
    opens again — and a card that asked only about the active row flipped to `unreadable: false`
    at the exact moment the household's other secrets became unreachable: the VAPID pair here,
    and any second connector on a real install. The one surface built to report custody would
    have reported it healed. [M4.7 ops-11, dd03; decision 182]
    """
    admin = await _admin(app)
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
    # The shortcut worked, for Jellyfin: that is what makes the card's answer load-bearing
    # rather than pedantic, because this is the screen the admin is standing on.
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
    """§3.1 makes a half-configured boot a legal state, so the card describes it as one.

    Not an error and not an empty string: `configured: false` is what lets the surface say the
    key is unset rather than render a fingerprint of nothing and imply custody exists.
    """
    admin = await _admin(app)

    card = await _card(admin)

    assert card["secrets"]["configured"] is False
    assert card["secrets"]["fingerprint"] is None
    # Nothing minted a DEK, because nothing could: `ensure_dek` refuses without the key (§2).
    assert card["secrets"]["key_id"] is None
    assert card["secrets"]["unreadable"] is False


# --- who may read it ------------------------------------------------------------------


async def test_the_card_is_refused_to_a_signed_out_caller_and_to_a_member(secrets_key, db, app):
    """§6.6 is an admin surface, and this one names the custody state of the whole install.

    `test_api_gating.py` walks the dependency graph and would catch a missing `AdminUser` here
    as an arithmetic complaint about a route count; this says the same thing in the terms the
    route is written in, and it is cheap.
    """
    admin = await _admin(app)
    member = await _member(app, admin)
    anonymous = app()

    assert (await anonymous.get("/api/admin/system")).status_code == 401
    assert (await member.get("/api/admin/system")).status_code == 403
