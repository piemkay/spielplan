"""§6.6's Acquisition board, read side, over HTTP (§8). `reason` round-trips
verbatim, the board shows the `raw_document` ROW never the bytes, and
documents join through the fetching task. Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.acquire import board, queue, rawstore

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"

# §4.1's partition: the app's own ids start at 1e9. Written directly: this file is about the BOARD.
ACQUIRED = 1_000_000_701
OTHER = 1_000_000_702

# Everything a well-meaning renderer would tidy away; any of it missing breaks the verbatim promise.
REASON = 'stage 4: waiting for reviews to accrue  - "fewer than 20 ratings", retry in 30 days.'


async def _bootstrap(app):
    admin = app()
    created = await admin.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201

    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert made.status_code == 201
    otp = made.json()["one_time_password"]

    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.status_code == 200
    # §3.1 locks a new account to a password change; clear it so a later 403 is about role.
    changed = await member.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    assert changed.status_code == 200
    return admin, member


async def _title(conn, title_id: int, name: str) -> None:
    await conn.execute(
        "INSERT INTO title (id, kind, name, year, origin) VALUES ($1, 'movie', $2, 2016,"
        " 'acquired')",
        title_id, name,
    )


async def _job(conn, title_id: int, *, stage: int, status: str, reason: str | None) -> None:
    await conn.execute(
        "INSERT INTO acquisition_job (title_id, stage, status, reason, detail)"
        " VALUES ($1, $2, $3, $4, $5)",
        title_id, stage, status, reason, {"identify": {"source": "jellyfin"}},
    )


async def _task(conn, title_id: int, key: str, **columns) -> None:
    """`payload ->> 'title_id'` is what `acquire/board.py` joins on."""
    await conn.execute(
        "INSERT INTO acquisition_task (kind, key, payload, state, attempts, last_error,"
        " result_note) VALUES ('acquire', $1, $2, $3, $4, $5, $6)",
        key, {"title_id": title_id}, columns.get("state", "pending"),
        columns.get("attempts", 1), columns.get("last_error"), columns.get("result_note"),
    )


async def test_the_board_lists_a_parked_job_with_its_reason_verbatim(app, db):
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=4, status="parked", reason=REASON)
    admin, _member = await _bootstrap(app)

    answer = await admin.get("/api/admin/acquisition")
    assert answer.status_code == 200, answer.text
    jobs = {row["title_id"]: row for row in answer.json()["jobs"]}
    assert ACQUIRED in jobs, answer.json()

    row = jobs[ACQUIRED]
    assert row["reason"] == REASON, (
        "the board rewrote the park reason. `acquisition_job.reason` is 'shown verbatim on the "
        f"admin board' (0005_ledger.sql:138) and this is the only place that holds: {row['reason']}"
    )
    assert (row["stage"], row["status"]) == (4, "parked")
    assert row["name"] == "A Bigger Splash" and row["origin"] == "acquired"


async def test_the_board_is_an_envelope_and_not_a_bare_list(app, db):
    """A top-level JSON array is the one shape that cannot grow without a second route."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=10, status="ready", reason=None)
    admin, _member = await _bootstrap(app)

    payload = (await admin.get("/api/admin/acquisition")).json()
    assert isinstance(payload, dict) and isinstance(payload["jobs"], list)
    assert payload["jobs"][0]["reason"] is None, (
        "an advance clears the reason, and a board that substituted a placeholder sentence for "
        "NULL would be describing a state the row is not in"
    )


async def test_the_per_title_route_returns_its_stages_and_its_queue_rows(app, db):
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=5, status="parked", reason=REASON)
    await _task(
        db, ACQUIRED, "jellyfin:abc123",
        state="failed", attempts=2, last_error="host api.trakt.tv paused for 300s",
        result_note="deferred once for the review window",
    )
    admin, _member = await _bootstrap(app)

    answer = await admin.get(f"/api/admin/acquisition/{ACQUIRED}")
    assert answer.status_code == 200, answer.text
    payload = answer.json()

    assert payload["job"]["stage"] == 5 and payload["job"]["reason"] == REASON
    assert payload["job"]["detail"] == {"identify": {"source": "jellyfin"}}, (
        "`detail` is what the stages BEFORE the parked one did - `pipeline.write_board` "
        "concatenates one key per stage into it, and a single job is where that is readable"
    )

    (task,) = payload["tasks"]
    assert task["key"] == "jellyfin:abc123"
    assert (task["state"], task["attempts"], task["max_attempts"]) == ("failed", 2, 4), (
        "decision 336 needs attempts against max_attempts on this surface: a `failed` task with "
        "attempts left is a plain retry and one without is not"
    )
    assert task["last_error"] == "host api.trakt.tv paused for 300s"
    assert task["result_note"] == "deferred once for the review window"
    assert "lease_owner" not in task and "lease_expires" not in task, (
        "the lease is the drain's fencing and names a process, not a fact about the title"
    )


async def test_a_title_the_pipeline_has_never_touched_is_404(app, db):
    """404, not an empty envelope, which would present two different states as one."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    admin, _member = await _bootstrap(app)

    missing = await admin.get(f"/api/admin/acquisition/{ACQUIRED}")
    assert missing.status_code == 404, missing.text
    assert "acquisition job" in missing.json()["detail"]


async def test_the_board_shows_the_raw_document_row_and_never_the_bytes(app, db):
    """`/data/raw` is absent from the backend container, so real bytes are written to catch a leak."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=3, status="running", reason=None)
    await _task(db, ACQUIRED, "jellyfin:abc123")
    secret = b'{"plot": "the-bytes-that-must-not-travel", "rating": 7.1}'
    document_id = await rawstore.store(
        db, source="tmdb", kind="movie", url="https://api.themoviedb.org/3/movie/949",
        content=secret, entity_key="jellyfin:abc123", http_status=200,
    )
    stored = await db.fetchrow(
        "SELECT content_path, content_sha256, byte_size FROM raw_document WHERE id = $1",
        document_id,
    )
    assert rawstore.resolve(stored["content_path"]).is_file(), (
        "the fixture wrote no file, so the leak this test is about could not have happened and "
        "the assertions below would pass against an empty store"
    )
    admin, _member = await _bootstrap(app)

    answer = await admin.get(f"/api/admin/acquisition/{ACQUIRED}")
    assert answer.status_code == 200, answer.text
    (document,) = answer.json()["documents"]

    # The five fields decision 345 enumerates, all off the row and all in Postgres.
    assert document["url"] == "https://api.themoviedb.org/3/movie/949"
    assert document["http_status"] == 200
    assert document["content_sha256"] == stored["content_sha256"]
    assert document["byte_size"] == stored["byte_size"] == len(secret)
    assert document["fetched_at"]

    body = answer.text
    assert "the-bytes-that-must-not-travel" not in body, "the board served the document"
    assert "content_path" not in document and stored["content_path"] not in body, (
        "the board handed back the path to a file this container cannot open - decision 345 "
        "enumerates five fields and a path is not one of them, so it reads as an offer"
    )


async def test_a_failed_fetch_is_on_the_board_because_that_is_why_a_title_is_thin(app, db):
    """Unlike `rawstore.latest`, failures count: they answer "why is this title thin"."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=2, status="parked", reason=REASON)
    await _task(db, ACQUIRED, "jellyfin:abc123")
    await rawstore.store(
        db, source="rt", kind="reviews:critics", url="https://www.rottentomatoes.com/m/x",
        content=b"", entity_key="jellyfin:abc123", http_status=503, ok=False,
        error="503 after 4 attempts", content_type="text/html",
    )
    admin, _member = await _bootstrap(app)

    (document,) = (await admin.get(f"/api/admin/acquisition/{ACQUIRED}")).json()["documents"]
    assert document["ok"] is False and document["error"] == "503 after 4 attempts"
    assert document["http_status"] == 503


async def test_a_document_filed_under_another_titles_task_is_not_this_titles(app, db):
    """Both directions, plus the two spellings a stage author reaches for by mistake: `entity_key` omitted,
    and the provider id. A document filed under either is one this board would never find."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _title(db, OTHER, "Call Me By Your Name")
    await _job(db, ACQUIRED, stage=3, status="running", reason=None)
    await _job(db, OTHER, stage=3, status="running", reason=None)
    await _task(db, ACQUIRED, "jellyfin:abc123")
    await _task(db, OTHER, "jellyfin:def456")
    await rawstore.store(
        db, source="tmdb", kind="movie", url="https://example.invalid/mine",
        content=b'{"id": 1}', entity_key="jellyfin:abc123",
    )
    await rawstore.store(
        db, source="tmdb", kind="movie", url="https://example.invalid/theirs",
        content=b'{"id": 2}', entity_key="jellyfin:def456",
    )
    await rawstore.store(
        db, source="omdb", kind="movie", url="https://example.invalid/nokey",
        content=b'{"id": 3}',
    )
    await rawstore.store(
        db, source="tmdb", kind="movie", url="https://example.invalid/invented",
        content=b'{"id": 4}', entity_key=f"tmdb:{ACQUIRED}",
    )
    admin, _member = await _bootstrap(app)

    urls = [
        row["url"]
        for row in (await admin.get(f"/api/admin/acquisition/{ACQUIRED}")).json()["documents"]
    ]
    assert urls == ["https://example.invalid/mine"], (
        "a document belongs to a title through the key of the task that fetched it "
        f"(acquire/board.py), and this read crossed titles: {urls}"
    )
    assert await db.fetchval("SELECT count(*) FROM raw_document") == 4, (
        "all four documents are in the store; three of them are files no operator can reach"
    )


@pytest.mark.parametrize("path", ["/api/admin/acquisition", f"/api/admin/acquisition/{ACQUIRED}"])
async def test_both_acquisition_routes_refuse_a_member_and_a_stranger(app, db, path):
    await _title(db, ACQUIRED, "A Bigger Splash")
    await _job(db, ACQUIRED, stage=1, status="queued", reason=None)
    _admin, member = await _bootstrap(app)

    assert (await member.get(path)).status_code == 403
    assert (await app().get(path)).status_code == 401


async def test_queue_depth_is_a_domain_function_the_system_card_reads(app, db):
    """The card's `queue.by_kind` is `stats` unchanged, which keeps the query in `acquire/queue.py`."""
    await db.execute(
        "INSERT INTO acquisition_task (kind, key) VALUES ('acquire', 'jellyfin:abc123')"
    )
    assert await queue.stats(db) == [{"kind": "acquire", "state": "pending", "count": 1}]

    admin, _member = await _bootstrap(app)
    card = (await admin.get("/api/admin/system")).json()
    assert card["queue"]["by_kind"] == await queue.stats(db)


async def test_the_board_reads_the_inbox_the_sweep_has_been_filling_since_m4_13(app, db):
    """`_park_thin`'s rows are the inbox, not a backlog of failures (decision 336)."""
    await _title(db, ACQUIRED, "A Bigger Splash")
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status, reason) VALUES ($1, 2, 'parked',"
        " $2) ON CONFLICT (title_id) DO NOTHING",
        ACQUIRED, "thin: placed by the model, no crowd data",
    )
    rows = await board.board(db)
    assert [(r["title_id"], r["stage"], r["status"]) for r in rows] == [(ACQUIRED, 2, "parked")]
    assert rows[0]["reason"] == "thin: placed by the model, no crowd data"
