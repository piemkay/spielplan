from __future__ import annotations

import pytest

from spielplan.sync import playback


@pytest.fixture
async def signed_in(app, db):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    await db.execute(
        "INSERT INTO title (id, kind, name, year) VALUES (1, 'movie', 'Heat', 1995)"
    )
    user_id = (await client.get("/api/auth/me")).json()["id"]
    return client, user_id


async def test_marking_seen_and_back_again(signed_in):
    client, _user_id = signed_in
    assert (await client.get("/api/titles/1/state")).json()["state"] == "unseen"

    marked = await client.post("/api/titles/1/state", json={"state": "seen"})
    assert marked.status_code == 200
    assert marked.json()["state"] == "seen"
    assert marked.json()["synced"] is False
    assert (await client.get("/api/titles/1/state")).json()["state"] == "seen"

    await client.post("/api/titles/1/state", json={"state": "unseen"})
    assert (await client.get("/api/titles/1/state")).json()["state"] == "unseen"


async def test_an_untouched_title_reads_as_unseen_without_a_row(signed_in, db):
    """§4.2: the default is an absence, which the sync never pushes over Jellyfin's history."""
    client, _user_id = signed_in
    assert (await client.get("/api/titles/1/state")).json() == {
        "state": "unseen", "state_changed_at": None, "jf_synced_at": None
    }
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_a_third_state_is_refused(signed_in):
    """Owner decision 2026-08-29: there is no 'forgotten'."""
    client, _user_id = signed_in
    response = await client.post("/api/titles/1/state", json={"state": "forgotten"})
    assert response.status_code == 400
    assert "seen, unseen" in response.json()["detail"]


async def test_an_unknown_title_is_a_404_not_a_stray_row(signed_in, db):
    client, _user_id = signed_in
    assert (
        await client.post("/api/titles/999999/state", json={"state": "seen"})
    ).status_code == 404
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_seen_state_is_per_person(app, db):
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-pass"})
    await db.execute("INSERT INTO title (id, kind, name) VALUES (1, 'movie', 'Heat')")
    otp = (
        await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]
    await client.post("/api/titles/1/state", json={"state": "seen"})

    member = app()
    await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    await member.post(
        "/api/auth/password", json={"current_password": otp, "new_password": "a-real-password"}
    )
    assert (await member.get("/api/titles/1/state")).json()["state"] == "unseen"


async def test_the_prompt_queue_is_empty_by_default(signed_in):
    client, _user_id = signed_in
    assert (await client.get("/api/prompts/finish")).json() == []


async def test_an_armed_prompt_surfaces_and_its_first_tap_writes_seen(signed_in, db):
    client, user_id = signed_in
    await playback.arm(db, user_id=user_id, title_id=1, session_id="s", progress=0.96)

    queued = (await client.get("/api/prompts/finish")).json()
    assert len(queued) == 1
    assert queued[0]["name"] == "Heat"
    assert queued[0]["title_id"] == 1

    answered = await client.post(
        f"/api/prompts/finish/{queued[0]['id']}", json={"finished": True}
    )
    assert answered.status_code == 200
    assert (await client.get("/api/titles/1/state")).json()["state"] == "seen"
    assert (await client.get("/api/prompts/finish")).json() == []


async def test_declining_the_prompt_writes_unseen_and_closes_it(signed_in, db):
    """`state` reads `unseen` either way, so the row itself is checked (decision 211)."""
    client, user_id = signed_in
    await playback.arm(db, user_id=user_id, title_id=1, session_id="s", progress=0.96)
    queued = (await client.get("/api/prompts/finish")).json()

    declined = await client.post(
        f"/api/prompts/finish/{queued[0]['id']}", json={"finished": False}
    )
    assert declined.status_code == 200
    assert declined.json()["seen"] is False
    assert (await client.get("/api/titles/1/state")).json()["state"] == "unseen"
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 1", user_id
    ) == "unseen", "a declined prompt is an explicit action, not an absence (decision 211)"
    assert (await client.get("/api/prompts/finish")).json() == []


async def test_answering_a_prompt_twice_is_a_404(signed_in, db):
    client, user_id = signed_in
    await playback.arm(db, user_id=user_id, title_id=1, session_id="s", progress=0.96)
    event_id = (await client.get("/api/prompts/finish")).json()[0]["id"]
    await client.post(f"/api/prompts/finish/{event_id}", json={"finished": True})
    again = await client.post(f"/api/prompts/finish/{event_id}", json={"finished": False})
    assert again.status_code == 404


async def test_one_person_cannot_answer_anothers_prompt_over_the_wire(app, db):
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-pass"})
    await db.execute("INSERT INTO title (id, kind, name) VALUES (1, 'movie', 'Heat')")
    admin_id = (await client.get("/api/auth/me")).json()["id"]
    await playback.arm(db, user_id=admin_id, title_id=1, session_id="s", progress=0.96)
    event_id = (await client.get("/api/prompts/finish")).json()[0]["id"]

    otp = (
        await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]
    member = app()
    await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    await member.post(
        "/api/auth/password", json={"current_password": otp, "new_password": "a-real-password"}
    )

    assert (
        await member.post(f"/api/prompts/finish/{event_id}", json={"finished": True})
    ).status_code == 404
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0


async def test_the_state_routes_need_a_session(app):
    anonymous = app()
    assert (await anonymous.get("/api/prompts/finish")).status_code == 401
    assert (
        await anonymous.post("/api/titles/1/state", json={"state": "seen"})
    ).status_code == 401
