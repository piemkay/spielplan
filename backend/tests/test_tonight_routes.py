"""Route tests for what only the route can get wrong: the seal, who may write to a seat, what a
payload carries, and the blind ballot."""

from __future__ import annotations

import itertools

import httpx
import pytest

from spielplan.push import keys, send
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import round as rnd

BUNDLE = "test-v1"


async def admin_client(app, name="patrick"):
    """Through the real wizard; each call returns its own cookie jar, so one test holds two identities."""
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": name, "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    return client, (await client.get("/api/auth/me")).json()["id"]


async def member_client(app, admin, name="jenny"):
    """A one-time password, a forced change, then a session of its own (§3.1)."""
    created = await admin.post("/api/admin/users", json={"name": name, "role": "member"})
    assert created.status_code == 201, created.text
    otp = created.json()["one_time_password"]
    client = app()
    assert (await client.post("/api/auth/login", json={"name": name, "password": otp})).is_success
    assert (await client.post(
        "/api/auth/password", json={"current_password": otp, "new_password": "member-password"}
    )).is_success
    return client, (await client.get("/api/auth/me")).json()["id"]


@pytest.fixture
async def library(db):
    """The pool is larger than the shortlist, which gives the round a boundary."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}', 'active')",
        BUNDLE,
    )
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT g, 'movie', 'Film ' || g, 2010, 95 + g, true FROM generate_series(1, 6) AS g
        """
    )
    return list(range(1, 7))


async def score(db, user_id, titles):
    """§5.1's per-user half, as the nightly job would have written it."""
    for i, title_id in enumerate(titles):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "VALUES ($1, $2, 'movie', $3, $4, 0.0) ON CONFLICT (user_id, title_id) DO NOTHING",
            user_id, title_id, BUNDLE, 0.9 - 0.1 * i,
        )


async def _register(db, user_id: int, endpoint: str) -> str:
    """One device, registered the way `POST /api/push/subscribe` would."""
    import os as _os

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = ec.generate_private_key(ec.SECP256R1())
    await db.execute(
        "INSERT INTO push_subscription (user_id, device_label, endpoint, p256dh, auth) "
        "VALUES ($1, 'phone', $2, $3, $4)",
        user_id, endpoint,
        keys.b64(private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
        keys.b64(_os.urandom(16)),
    )
    return endpoint


async def open_room(client, **kw):
    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True}
    body.update(kw)
    res = await client.post("/api/tonight/sessions", json=body)
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
async def solo_room(app, db, library):
    """One admin, scored, with a started room of their own."""
    client, user_id = await admin_client(app)
    await score(db, user_id, library)
    room = await open_room(client)
    started = await client.post(f"/api/tonight/sessions/{room['session_id']}/start")
    assert started.status_code == 200, started.text
    return {
        "client": client, "user_id": user_id, "session_id": room["session_id"],
        "room_code": room["room_code"],
        "seat": room["lobby"]["seats"][0]["participant_id"],
    }


@pytest.fixture
async def wide_solo_room(app, db, library):
    """For tests about a round's second answer: what keeps it asking is how many titles sit near the
    cut (decision 477)."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT g, 'movie', 'Film ' || g, 2010, 100, true FROM generate_series(7, 120) AS g
        """
    )
    client, user_id = await admin_client(app)
    await score(db, user_id, list(range(1, 121)))
    room = await open_room(client)
    started = await client.post(f"/api/tonight/sessions/{room['session_id']}/start")
    assert started.status_code == 200, started.text
    return {
        "client": client, "user_id": user_id, "session_id": room["session_id"],
        "seat": room["lobby"]["seats"][0]["participant_id"],
    }


def leaks_pool(payload) -> list[str]:
    """A pool rendered before the votes anchors the votes it collects (§6.2 step 3)."""
    banned = {"scores", "group_score", "pool", "candidates", "ranked", "beliefs", "finalists"}
    found: list[str] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in banned:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(payload)
    return found


async def test_an_answer_names_a_sealed_pair_and_never_two_title_ids(solo_room):
    """A client naming the ids and the arm could file its answer into or out of §13's stream."""
    client, seat = solo_room["client"], solo_room["seat"]
    state = (await client.get(f"/api/tonight/seats/{seat}/round")).json()

    assert state["card_token"], "the pair has to be sealed to be answerable"
    assert "title_a" not in state["pair"], "the wire shape names a card, not two ids"

    res = await client.post(
        f"/api/tonight/seats/{seat}/answer",
        json={"card_token": "not-a-real-seal", "answer": "A"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "stale_pair"


async def test_the_seal_is_single_use(db, wide_solo_room):
    """A replay would weight one judgement twice. Household-sized, so the first answer does not end
    the round and the refusal is not `round_over`."""
    client, seat = wide_solo_room["client"], wide_solo_room["seat"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]

    first = await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "B"}
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["reason"] == "stale_pair"
    assert await db.fetchval(
        "SELECT count(*) FROM session_answer WHERE participant_id = $1", seat
    ) == 1


async def test_the_stored_arm_is_the_arm_the_server_drew(db, solo_room):
    """An arm the client could name, or a constant the route substituted, leaks a held-out pair."""
    client, seat = solo_room["client"], solo_room["seat"]
    for _ in range(rnd.CAP_PAIRS):
        state = (await client.get(f"/api/tonight/seats/{seat}/round")).json()
        if state["card_token"] is None:
            break
        served = state["pair"]["selection"]
        answered = await client.post(
            f"/api/tonight/seats/{seat}/answer",
            json={"card_token": state["card_token"], "answer": "A"},
        )
        assert answered.status_code == 200, answered.text
        row = await db.fetchrow(
            "SELECT selection FROM session_answer WHERE participant_id = $1 "
            "ORDER BY seq DESC LIMIT 1",
            seat,
        )
        assert row["selection"] == served
    stored = {
        r["selection"] for r in await db.fetch(
            "SELECT DISTINCT selection FROM session_answer WHERE participant_id = $1", seat
        )
    }
    assert stored <= {rnd.SELECTION_ADAPTIVE, rnd.SELECTION_HOLDOUT}


async def test_an_answer_outside_the_four_is_refused_by_the_route(solo_room):
    """Decision 154: a fifth value never reaches the domain layer."""
    client, seat = solo_room["client"], solo_room["seat"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]

    res = await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "NO_PULL"}
    )
    assert res.status_code == 422


async def test_one_member_cannot_answer_for_another(app, db, library):
    """The blind reveal means nothing if one device casts another's votes."""
    host, host_id = await admin_client(app)
    other, other_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, other_id, library)

    room = await open_room(host)
    host_seat = room["lobby"]["seats"][0]["participant_id"]
    await host.post(f"/api/tonight/sessions/{room['session_id']}/start")

    res = await other.get(f"/api/tonight/seats/{host_seat}/round")
    assert res.status_code == 403


async def test_a_host_may_take_a_guests_turn(app, db, library):
    """Hand-the-phone: one cookie speaks for several seats, by design and only for guests."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    room = await open_room(host, guests=1)
    guest = next(s["participant_id"] for s in room["lobby"]["seats"] if s["role"] == "guest")
    await host.post(f"/api/tonight/sessions/{room['session_id']}/start")

    assert (await host.get(f"/api/tonight/seats/{guest}/round")).status_code == 200


async def test_a_member_seat_is_never_writable_by_the_host(app, db, library):
    """A host who could answer for a member could decide the evening alone."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)

    room = await open_room(host)
    joined = (await member.post(
        "/api/tonight/sessions/join", json={"session_id": room["session_id"]}
    )).json()
    await host.post(f"/api/tonight/sessions/{room['session_id']}/start")

    res = await host.get(f"/api/tonight/seats/{joined['participant_id']}/round")
    assert res.status_code == 403


async def test_only_the_host_starts_the_round(app, db, library):
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    room = await open_room(host)
    await member.post("/api/tonight/sessions/join", json={"session_id": room["session_id"]})

    assert (await member.post(
        f"/api/tonight/sessions/{room['session_id']}/start"
    )).status_code == 403


async def test_every_tonight_route_refuses_an_unauthenticated_caller(app, library):
    """These frames name who is in the room."""
    anonymous = app()
    for method, path in (
        ("get", "/api/tonight/rooms"),
        ("post", "/api/tonight/sessions"),
        ("post", "/api/tonight/sessions/join"),
        ("get", "/api/tonight/sessions/1"),
        ("post", "/api/tonight/solo"),
        ("get", "/api/tonight/seats/1/round"),
        ("get", "/api/tonight/sessions/1/ballot"),
        ("get", "/api/tonight/sessions/1/result"),
    ):
        res = await (
            anonymous.post(path, json={}) if method == "post" else anonymous.get(path)
        )
        assert res.status_code == 401, f"{method} {path} answered {res.status_code}"


async def test_no_payload_before_the_reveal_carries_the_pool(solo_room):
    """Every route a participant can reach while the round runs."""
    client, sid, seat = solo_room["client"], solo_room["session_id"], solo_room["seat"]
    for path in (
        "/api/tonight/rooms",
        f"/api/tonight/sessions/{sid}",
        f"/api/tonight/seats/{seat}/round",
    ):
        payload = (await client.get(path)).json()
        assert not leaks_pool(payload), f"{path} leaks {leaks_pool(payload)}"

    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    answered = (await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )).json()
    assert not leaks_pool(answered)


async def test_the_waiting_payload_carries_counts_and_no_answer(solo_room):
    """A claim about the payload, not the UI."""
    client, sid, seat = solo_room["client"], solo_room["session_id"], solo_room["seat"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )

    progress = (await client.get(f"/api/tonight/sessions/{sid}")).json()["progress"]
    assert [p["answered"] for p in progress] == [1]
    assert {k for p in progress for k in p} == {
        "participant_id", "seat", "name", "answered", "expected", "finished", "ended_by",
    }


async def test_the_model_log_line_is_gated_by_the_per_user_toggle(db, solo_room):
    """Read from `/api/model-log`: the round no longer embeds a rail of its own (M4.9)."""
    client, seat, user_id = solo_room["client"], solo_room["seat"], solo_room["user_id"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    off = (await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )).json()
    assert "rail" not in off, "the round's payload carries no rail of its own"
    assert "events" not in (await client.get("/api/model-log")).json(), (
        "decision 117: the rail is off by default"
    )

    await db.execute("UPDATE app_user SET show_model = true WHERE id = $1", user_id)
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "B"}
    )
    events = (await client.get("/api/model-log")).json()["events"]

    assert events, "with the toggle on the round narrates its own write"
    line = events[0]
    assert line["kind"] == "session_answer"
    assert line["text"].startswith("session_answer(")
    assert "pool-centred tilt" in line["text"], "§6.2 step 5's measured centring lever, named"


async def test_one_participants_rail_never_carries_anothers_answer(app, db, library):
    """A line filed against another account would be an answer leaving its seat."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    await db.execute("UPDATE app_user SET show_model = true")

    room = await open_room(host)
    joined = (await member.post(
        "/api/tonight/sessions/join", json={"session_id": room["session_id"]}
    )).json()
    await host.post(f"/api/tonight/sessions/{room['session_id']}/start")

    seat = joined["participant_id"]
    token = (await member.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    await member.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )

    host_seat = room["lobby"]["seats"][0]["participant_id"]
    host_token = (await host.get(f"/api/tonight/seats/{host_seat}/round")).json()["card_token"]
    await host.post(
        f"/api/tonight/seats/{host_seat}/answer",
        json={"card_token": host_token, "answer": "A"},
    )
    # `rail.recent` merges this user's deque with the household's and never another member's.
    host_rail = (await host.get("/api/model-log")).json()["events"]

    assert all(
        line["detail"].get("session_id") is None
        or f"session_answer({seat}," not in line["text"]
        for line in host_rail
    ), "the host's rail carries the member's answer"


async def test_the_room_code_is_readable_from_the_sessions_own_surface(solo_room):
    """The code works when push does not, and §11 shows it on a Home Assistant dashboard."""
    client, sid, code = solo_room["client"], solo_room["session_id"], solo_room["room_code"]

    assert code
    assert (await client.get(f"/api/tonight/sessions/{sid}")).json()["room_code"] == code
    assert (await client.get("/api/tonight/rooms")).json()["rooms"][0]["room_code"] == code


async def test_joining_by_code_and_by_id_reach_the_same_seat(app, db, library):
    """Two seats would skew every per-participant average."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    room = await open_room(host)

    by_code = (await member.post(
        "/api/tonight/sessions/join", json={"room_code": room["room_code"]}
    )).json()
    by_id = (await member.post(
        "/api/tonight/sessions/join", json={"session_id": room["session_id"]}
    )).json()

    assert by_code["participant_id"] == by_id["participant_id"]
    assert await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1 AND user_id = $2",
        room["session_id"], member_id,
    ) == 1


async def test_a_code_that_names_no_live_room_is_refused(app, db, library):
    client, user_id = await admin_client(app)
    res = await client.post("/api/tonight/sessions/join", json={"room_code": "ZZ-9999"})
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "no_room"


async def test_the_open_rooms_list_is_the_households_and_not_the_hosts(app, db, library):
    """Visible to every household device, with the spec's example facets."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    room = await open_room(host, runtime_budget_min=60, include_rewatches=False)

    listed = (await member.get("/api/tonight/rooms")).json()["rooms"]
    assert len(listed) == 1
    row = listed[0]
    assert row["room_code"] == room["room_code"]
    assert row["host"] == "patrick"
    assert row["kind"] == "movie" and row["runtime_budget_min"] == 60
    assert row["skips_seen"] is True
    assert row["started_at"] and row["joinable"] is True and row["viewer_seated"] is False


async def _play_out(client, seat):
    for _ in range(rnd.CAP_PAIRS + 2):
        state = (await client.get(f"/api/tonight/seats/{seat}/round")).json()
        if state["ended_by"] or state["card_token"] is None:
            return state
        await client.post(
            f"/api/tonight/seats/{seat}/answer",
            json={"card_token": state["card_token"], "answer": "A"},
        )
    return (await client.get(f"/api/tonight/seats/{seat}/round")).json()


async def test_the_result_is_refused_until_every_seat_has_submitted(app, db, library):
    """Enforced in `ballot.tally`, so this route and the WebSocket agree on when to reveal."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)

    room = await open_room(host)
    sid = room["session_id"]
    joined = (await member.post("/api/tonight/sessions/join", json={"session_id": sid})).json()
    await host.post(f"/api/tonight/sessions/{sid}/start")

    host_seat = room["lobby"]["seats"][0]["participant_id"]
    await _play_out(host, host_seat)
    await _play_out(member, joined["participant_id"])

    card = (await host.get(f"/api/tonight/sessions/{sid}/ballot")).json()
    assert card["revealed"] is False
    assert 1 <= len(card["slate"]) <= 4
    assert not leaks_pool(card), "the ballot card names titles, never the pool's ranking"

    early = await host.get(f"/api/tonight/sessions/{sid}/result")
    assert early.status_code == 409
    assert early.json()["detail"]["reason"] == "still_voting"

    chosen = [card["slate"][0]["title_id"]]
    first = await host.post(
        f"/api/tonight/seats/{host_seat}/ballot", json={"approved": chosen}
    )
    assert first.status_code == 200, first.text
    assert first.json()["revealed"] is False

    still = await member.get(f"/api/tonight/sessions/{sid}/result")
    assert still.status_code == 409, "one submission is not every submission"

    last = await member.post(
        f"/api/tonight/seats/{joined['participant_id']}/ballot", json={"approved": chosen}
    )
    assert last.json()["revealed"] is True

    revealed = await host.get(f"/api/tonight/sessions/{sid}/result")
    assert revealed.status_code == 200, revealed.text
    body = revealed.json()
    assert body["beat"] == "VOTES REVEALED TOGETHER"
    assert body["winner"]["title_id"] == chosen[0]
    assert body["approval_share"] == pytest.approx(1.0)
    assert body["participants"] == 2
    # "Unanimous" was true over one member's only yes; each seat's breadth is released instead (54e).
    assert "unanimous" not in body
    assert [(b["approved"], b["of"], b["only_yes"]) for b in body["breadth"]] == [
        (1, len(card["slate"]), True), (1, len(card["slate"]), True),
    ]
    assert body["winner"]["fit_line"]
    assert body["winner"]["match_lines"], "§6.2 step 7: one match line per participant"
    # Series runtimes read per episode, so the winner card carries the SESSION's kind.
    assert body["winner"]["kind"] == "movie", body["winner"]
    assert {c["kind"] for c in body["runners_up"] + body["finalists"]} == {"movie"}


async def test_a_ballot_naming_a_title_off_the_slate_is_refused(app, db, library):
    """A title nobody was offered would land in §13's numbers."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    room = await open_room(host)
    sid = room["session_id"]
    await host.post(f"/api/tonight/sessions/{sid}/start")
    seat = room["lobby"]["seats"][0]["participant_id"]
    await _play_out(host, seat)

    card = (await host.get(f"/api/tonight/sessions/{sid}/ballot")).json()
    off_slate = next(t for t in library if t not in {s["title_id"] for s in card["slate"]})
    res = await host.post(f"/api/tonight/seats/{seat}/ballot", json={"approved": [off_slate]})
    assert res.status_code == 422
    assert res.json()["detail"]["reason"] == "not_on_slate"


async def test_the_evaluation_route_names_no_candidate(solo_room):
    """A report that names a candidate invites a surface to draw it."""
    client, sid = solo_room["client"], solo_room["session_id"]
    payload = (await client.get(f"/api/tonight/sessions/{sid}/evaluation")).json()

    assert set(payload) == {
        "session_id", "approval_share", "participants", "shortlist_agreement", "ended_by"
    }
    assert "title_id" not in repr(payload)
    assert set(payload["ended_by"]) == set(rnd.END_REASONS)


async def test_solo_returns_picks_without_a_room(app, db, library):
    """Solo mints no session row, so it publishes no room."""
    client, user_id = await admin_client(app)
    await score(db, user_id, library)
    out = (await client.post(
        "/api/tonight/solo",
        json={"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True},
    )).json()

    assert len(out["picks"]) == 3
    assert out["wildcard"] is not None
    assert out["provenance"].startswith("200 min budget")
    assert (await client.get("/api/tonight/rooms")).json()["rooms"] == []
    assert await db.fetchval("SELECT count(*) FROM session") == 0


async def test_a_bundle_less_app_says_so_rather_than_erroring(app, db):
    """§3.1: a bundle-less app is legal and renders "no bundle imported"."""
    client, _ = await admin_client(app)
    res = await client.post("/api/tonight/solo", json={})
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "no_bundle"

    room = await client.post("/api/tonight/sessions", json={})
    assert room.status_code == 409
    assert room.json()["detail"]["reason"] == "no_bundle"


# The seam: opening a session invites the right members, and no send outcome stops the evening.


class _Recorder(httpx.AsyncBaseTransport):
    """`payloads` is what the route handed the sender; on the wire it is encrypted for the device."""

    def __init__(self, *, raises: Exception | None = None, status: int = 201) -> None:
        self.urls: list[str] = []
        self.payloads: list[dict] = []
        self.raises = raises
        self.status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.raises:
            raise self.raises
        return httpx.Response(self.status)


@pytest.fixture
def recorder(monkeypatch):
    """Route the sender through a transport the test owns, without touching the route."""
    made: list[_Recorder] = []
    real = send.send_to_user

    async def routed(conn, user_id, payload, *, transport=None):
        made[0].payloads.append(payload)
        return await real(conn, user_id, payload, transport=made[0])

    def install(rec: _Recorder) -> _Recorder:
        made.clear()
        made.append(rec)
        monkeypatch.setattr("spielplan.push.send.send_to_user", routed)
        return rec

    return install


async def test_opening_a_session_invites_the_other_member_and_nobody_else(
    secrets_key, app, db, library, recorder
):
    """Not the host, and not a member already seated."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    host_device = await _register(db, host_id, "https://push.test/host")
    member_device = await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task (finding 42), so wait for it to finish.
    await invites_settled()

    assert member_device in service.urls, "the other member's phone is the point of the push"
    assert host_device not in service.urls, "the host is holding the phone that opened the room"
    assert room["room_code"], "and the room exists either way"


async def test_the_invitation_names_its_own_replacement_key_and_the_surface_that_answers_it(
    secrets_key, app, db, library, recorder
):
    """The tag is a replacement key: none set, and an invitation overwrote an unread finish prompt.
    Asserted against the returned room, since a restated literal passes with the field absent."""
    host, host_id = await admin_client(app)
    _member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task (finding 42), so wait for it to finish.
    await invites_settled()

    assert len(service.payloads) == 1, "one invitation, to the member who was not holding the host's phone"
    payload = service.payloads[0]
    assert payload["tag"] == f"tonight:{room['session_id']}"
    # The room's own join link (decision 481).
    assert payload["url"] == f"/tonight?room={room['room_code']}"
    # The frontend switches on the kind; the tag is for the browser.
    assert payload["kind"] == "tonight.invite"
    assert room["room_code"] in payload["body"]


async def test_a_session_opens_even_when_every_push_fails(
    secrets_key, app, db, library, recorder
):
    """Push is best-effort; a lobby blocked on delivery breaks on the iPhone."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder(raises=httpx.ConnectError("no route to the push service")))
    room = await open_room(host)

    # The dispatch is a background task (finding 42), so wait for it to finish.
    await invites_settled()

    assert service.urls, "the attempt was made"
    assert room["room_code"] and room["session_id"]
    # The two channels §6's preamble guarantees still reach it.
    joined = await member.post(
        "/api/tonight/sessions/join", json={"room_code": room["room_code"]}
    )
    assert joined.status_code == 200, joined.text
    assert (await member.get("/api/tonight/rooms")).json()["rooms"][0]["room_code"] == room["room_code"]
    assert (await host.post(
        f"/api/tonight/sessions/{room['session_id']}/start"
    )).status_code == 200


async def test_a_member_with_no_phone_is_still_reachable(secrets_key, app, db, library, recorder):
    """iOS grants push only to an installed PWA, so no subscription is common."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task (finding 42), so wait for it to finish.
    await invites_settled()

    assert service.urls == [], "nothing to send to"
    assert (await member.post(
        "/api/tonight/sessions/join", json={"room_code": room["room_code"]}
    )).status_code == 200


async def test_an_armed_finish_prompt_is_pushed_to_that_member_alone(
    db, secrets_key, recorder
):
    """§7.3: push arrives with M4; the banner is the fallback."""
    from spielplan.sync import playback

    await keys.ensure_keypair(db)
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    patrick = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('patrick', 'member') RETURNING id"
    )
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (1, 'movie', 'Heat', true)"
    )
    hers = await _register(db, jenny, "https://push.test/jenny")
    his = await _register(db, patrick, "https://push.test/patrick")

    service = recorder(_Recorder())
    await playback.notify(db, user_id=jenny, title_id=1)

    assert service.urls == [hers], "§7.3's prompt is per-user"
    assert his not in service.urls


async def test_a_failed_finish_prompt_leaves_the_banner_path_intact(db, secrets_key, recorder):
    """The prompt is armed before the push, so a failed push leaves the banner."""
    from spielplan.sync import playback

    await keys.ensure_keypair(db)
    jenny = await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
    )
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES (1, 'movie', 'Heat', true)"
    )
    await _register(db, jenny, "https://push.test/jenny")
    armed = await playback.arm(db, user_id=jenny, title_id=1, session_id="s1", progress=0.97)
    assert armed

    recorder(_Recorder(raises=httpx.ConnectError("down")))
    await playback.notify(db, user_id=jenny, title_id=1)

    pending = await playback.pending(db, jenny)
    assert [p["title_id"] for p in pending] == [1], (
        "the queued prompt is what §7.3 promises when the push does not arrive"
    )


async def test_the_wildcard_card_carries_the_label_it_is_honest_about(app, db, library):
    """The route serves the label, on the wildcard card only; the client no longer spells it."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    room = await open_room(host)
    sid = room["session_id"]
    await host.post(f"/api/tonight/sessions/{sid}/start")
    seat = room["lobby"]["seats"][0]["participant_id"]
    await _play_out(host, seat)

    card = (await host.get(f"/api/tonight/sessions/{sid}/ballot")).json()
    await host.post(
        f"/api/tonight/seats/{seat}/ballot", json={"approved": [card["slate"][0]["title_id"]]}
    )
    body = (await host.get(f"/api/tonight/sessions/{sid}/result")).json()

    assert body["wildcard"] is not None, "or every assertion below is about nothing"
    assert body["wildcard"]["label"] == combine_rules.WILDCARD_LABEL
    for finalist in body["finalists"]:
        assert finalist["label"] is None, "a finalist is not a step outside anybody's usual"


async def test_solos_held_out_pair_is_still_held_out_when_it_comes_back(app, db, library):
    """Solo's answers return without `selection` (defaulting to adaptive), so the arm is re-derived
    from seq and `user.id` (decision 223). No skip in either half (finding 44)."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    key = str(host_id)

    # `sharpen` on every request: the door draws no pair (finding 35).
    body = {
        "kind": "movie", "runtime_budget_min": 200, "include_rewatches": True,
        "offset": 0, "sharpen": True,
    }
    first = (await host.post("/api/tonight/solo", json={**body, "answers": []})).json()
    assert first["pair"] is not None, "54f's sharpen round serves nothing on this pool at all"

    # Half one: every served pair carries the arm the rule names for this person.
    answers = []
    for seq in range(1, rnd.CAP_PAIRS + 1):
        out = (await host.post("/api/tonight/solo", json={**body, "answers": answers})).json()
        pair = out["pair"]
        if pair is None:
            break
        assert pair["selection"] == (
            rnd.SELECTION_HOLDOUT if rnd.is_holdout(seq, key=key) else rnd.SELECTION_ADAPTIVE
        ), f"the arm served at pair {seq} is not the one the rule draws for this person"
        answers.append(
            {"seq": seq, "title_a": pair["a"]["title_id"], "title_b": pair["b"]["title_id"],
             "answer": rnd.A}
        )

    # Half two: the round trip, over a history holding this person's first hold-out.
    held = next(seq for seq in itertools.count(1) if rnd.is_holdout(seq, key=key))
    sent = [
        {"seq": seq, "title_a": library[0], "title_b": library[1], "answer": rnd.A}
        for seq in range(1, held + 1)
    ]
    after = (await host.post("/api/tonight/solo", json={**body, "answers": sent})).json()
    assert "tilted by your" in after["provenance"]
    counted = int(after["provenance"].split("tilted by your ")[1].split()[0])
    assert counted == len(sent) - 1, (
        f"{len(sent)} answers sent, the one at pair {held} held out, provenance claims {counted}"
    )


async def test_a_solo_answer_is_classified_the_same_way_on_every_request(app, db, library):
    """Solo re-derives the arm per request, so the same history must classify the same way every time;
    `user.id` is stable, server-side and never client-supplied (decision 223)."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    key = str(host_id)

    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True, "offset": 0}
    held = next(seq for seq in itertools.count(1) if rnd.is_holdout(seq, key=key))
    sent = [
        {"seq": seq, "title_a": library[0], "title_b": library[1], "answer": rnd.A}
        for seq in range(1, held + 2)
    ]

    again = [
        (await host.post("/api/tonight/solo", json={**body, "answers": sent})).json()
        for _ in range(4)
    ]
    lines = {out["provenance"] for out in again}
    assert len(lines) == 1, f"the same answers were counted differently across requests: {lines}"
    tilts = {tuple(sorted(out["tilt"].items())) for out in again}
    assert len(tilts) == 1, "the same answers moved the tilt differently across requests"
    assert {tuple(p["title_id"] for p in out["picks"]) for out in again}.__len__() == 1

    # One answer is held out, so the line reports one fewer than were sent.
    counted = int(again[0]["provenance"].split("tilted by your ")[1].split()[0])
    assert counted == len(sent) - 1, (
        f"{len(sent)} answers sent, the one at pair {held} drawn by the arm, {counted} counted"
    )


# Finding 4: a GET of the session, ballot and result must call `play.settle` before reading;
# the rule itself is tested in `test_tonight_integration.py`.

import asyncio  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import asyncpg  # noqa: E402

from spielplan.tonight import play  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


class _FailsOnce:
    """The answer commits before the combine, so a dropped connection leaves the room in `voting`.
    An asyncpg connection error, as the real fault arrives."""

    def __init__(self, real) -> None:
        self.real = real
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError(
                "connection was closed in the middle of operation"
            )
        return await self.real(*args, **kwargs)


async def duo_room(app, db, library):
    """Two real accounts, both seated, the round started — §12's M4 household."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    room = await open_room(host)
    session_id = room["session_id"]
    joined = (await member.post(
        "/api/tonight/sessions/join", json={"session_id": session_id}
    )).json()
    assert (await host.post(
        f"/api/tonight/sessions/{session_id}/start"
    )).status_code == 200
    return {
        "host": host, "member": member, "session_id": session_id,
        "host_seat": room["lobby"]["seats"][0]["participant_id"],
        "member_seat": joined["participant_id"],
    }


async def _answer_until_ended(client, seat):
    """Returns the last status: the answer that finishes the room is the one the combine runs on."""
    last = None
    for _ in range(rnd.CAP_PAIRS + 2):
        state = (await client.get(f"/api/tonight/seats/{seat}/round")).json()
        if state["ended_by"] or state["card_token"] is None:
            break
        last = (await client.post(
            f"/api/tonight/seats/{seat}/answer",
            json={"card_token": state["card_token"], "answer": "A"},
        )).status_code
    return last


async def _stuck_in_voting(app, db, library, monkeypatch):
    """Every vote in, no slate: the combine fails once on the finishing answer."""
    room = await duo_room(app, db, library)
    broken = _FailsOnce(play.finish)
    monkeypatch.setattr(play, "finish", broken)

    await _answer_until_ended(room["host"], room["host_seat"])
    assert await _answer_until_ended(room["member"], room["member_seat"]) == 500, (
        "the combine raised after the answer committed, which is the fault under test"
    )
    assert broken.calls == 1
    session_id = room["session_id"]
    assert await db.fetchval(
        "SELECT count(*) FROM session_participant WHERE session_id = $1 AND ended_by IS NULL",
        session_id,
    ) == 0, "every seat has finished"
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == "voting"
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) == 0
    return {**room, "broken": broken}


async def test_a_combine_that_failed_once_is_finished_by_the_next_lobby_read(
    app, db, library, monkeypatch
):
    """The lobby is what every device polls, so the recovery arrives there."""
    room = await _stuck_in_voting(app, db, library, monkeypatch)
    session_id = room["session_id"]

    lobby = await room["host"].get(f"/api/tonight/sessions/{session_id}")

    assert lobby.status_code == 200, lobby.text
    assert lobby.json()["state"] == "ballot", "the read moved the room on"
    assert room["broken"].calls == 2, "the read called the combine rather than awaiting a POST"
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1", session_id
    ) > 0
    assert not leaks_pool(lobby.json()), "and it is still the lobby (§6.2 step 3)"


async def test_a_stuck_rooms_ballot_read_returns_the_slate_rather_than_an_empty_one(
    app, db, library, monkeypatch
):
    """`slate: []` cannot be told from a room that has not combined yet."""
    room = await _stuck_in_voting(app, db, library, monkeypatch)
    session_id = room["session_id"]

    card = await room["member"].get(f"/api/tonight/sessions/{session_id}/ballot")

    assert card.status_code == 200, card.text
    body = card.json()
    assert 1 <= len(body["slate"]) <= 4, "54e's three finalists and the wildcard"
    assert body["revealed"] is False
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == "ballot"


async def test_a_stuck_rooms_result_read_lets_the_evening_finish(app, db, library, monkeypatch):
    """The result still answers the ballot's 409, but the room has moved, so the evening can finish."""
    room = await _stuck_in_voting(app, db, library, monkeypatch)
    host, member, session_id = room["host"], room["member"], room["session_id"]

    stuck = await host.get(f"/api/tonight/sessions/{session_id}/result")
    assert stuck.status_code == 409
    assert stuck.json()["detail"]["reason"] == "still_voting"
    assert await db.fetchval(
        "SELECT state FROM session WHERE id = $1", session_id
    ) == "ballot", "the refusal is the ballot's; the room moved anyway"

    card = (await host.get(f"/api/tonight/sessions/{session_id}/ballot")).json()
    chosen = [card["slate"][0]["title_id"]]
    assert (await host.post(
        f"/api/tonight/seats/{room['host_seat']}/ballot", json={"approved": chosen}
    )).status_code == 200
    last = await member.post(
        f"/api/tonight/seats/{room['member_seat']}/ballot", json={"approved": chosen}
    )
    assert last.status_code == 200, last.text
    assert last.json()["revealed"] is True

    revealed = await host.get(f"/api/tonight/sessions/{session_id}/result")
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["winner"]["title_id"] == chosen[0]
    assert await db.fetchval(
        "SELECT count(*) FROM session_outcome WHERE session_id = $1", session_id
    ) == 1, "§13's one row per evening"


async def test_a_three_candidate_evening_reaches_the_ballot_from_the_round_read(app, db, library):
    """Decision 215: with two or three candidates only the round read can end a seat, and the client
    reads the session BEFORE the round, so the round route itself must settle."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    # Three candidates by making the others too long: the budget floor is 60 and the grace 40.
    await db.execute("UPDATE title SET runtime_min = 240 WHERE id > 3")

    room = await open_room(host, runtime_budget_min=60)
    sid = room["session_id"]
    joined = (await member.post("/api/tonight/sessions/join", json={"session_id": sid})).json()
    assert (await host.post(f"/api/tonight/sessions/{sid}/start")).status_code == 200, (
        "decision 215 admits a pool of three rather than refusing the household its evening"
    )
    seats = [room["lobby"]["seats"][0]["participant_id"], joined["participant_id"]]

    ended = []
    for client, seat in ((host, seats[0]), (member, seats[1])):
        # `refresh()` reads the session and then the round, never the reverse, never twice.
        assert (await client.get(f"/api/tonight/sessions/{sid}")).status_code == 200
        card = (await client.get(f"/api/tonight/seats/{seat}/round")).json()
        assert card["pair"] is None and card["ended_by"] == rnd.CONVERGED, card
        ended.append(await db.fetchval("SELECT state FROM session WHERE id = $1", sid))

    assert ended[0] == "voting", "one seat is not every seat; the room waits for the other"
    assert ended[1] == "ballot", (
        "both seats are 'converged' and the room is still 'voting': the read that ended the last "
        "seat settled nothing, so the household's waiting screen says everyone has finished and "
        "nothing on it will ever ask again"
    )

    card = (await member.get(f"/api/tonight/sessions/{sid}/ballot")).json()
    assert len(card["slate"]) == 3, "every candidate is on the ballot when there are this few"
    assert card["revealed"] is False


def test_no_route_calls_the_combine_directly():
    """Static: a second route combining inline would pass every behavioural test."""
    source = (BACKEND / "spielplan" / "api" / "tonight.py").read_text(encoding="utf-8")

    assert "play.finish" not in source, "the combine is `play.settle`'s to call"
    assert "play.settle(" in source, "and the router's reads are what call it"
    assert source.count("play.settle(") >= 4, (
        "the answer path and the three reads — the session, the ballot and the result"
    )


async def invites_settled() -> None:
    """Joins the router's own task set: a fixed sleep would be a flake."""
    from spielplan.api import tonight as tonight_api

    await asyncio.gather(*list(tonight_api._INVITES), return_exceptions=True)

async def test_opening_a_room_returns_before_the_invitation_is_delivered(
    secrets_key, app, db, library, monkeypatch
):
    """`_invite` was awaited inline, one 10 s client per device, holding the request's connection."""
    host, host_id = await admin_client(app)
    _member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await _register(db, member_id, "https://push.test/member")

    delay = 1.0
    sent: list[int] = []

    async def slow(conn, user_id, payload, **kwargs):
        await asyncio.sleep(delay)
        sent.append(user_id)

    monkeypatch.setattr("spielplan.push.send.send_to_user", slow)

    began = time.perf_counter()
    room = await open_room(host)
    elapsed = time.perf_counter() - began

    assert elapsed < delay / 2, f"the lobby waited {elapsed:.2f}s on a {delay:.1f}s send"
    assert sent == [], "the response arrived while the sender was still in flight"
    assert room["room_code"] and room["lobby"]["seats"], "and the lobby is complete"

    await invites_settled()
    assert sent == [member_id], "and the invitation was still delivered, on its own connection"


async def test_a_hanging_invitation_gives_its_pooled_connection_back(
    secrets_key, app, db, library, monkeypatch
):
    """A hanging sender pinned a pool connection per room opened. The bound is cut to a fraction of a
    second here; `wait_for` turns a regression into a red run, not a wedged one."""
    from spielplan.api import tonight as tonight_api
    from spielplan.db import pool as db_pool

    host, host_id = await admin_client(app)
    _member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await _register(db, member_id, "https://push.test/member")

    assert 0 < tonight_api._INVITE_TIMEOUT_S < send._TIMEOUT, (
        "a bound no tighter than one device's own timeout bounds nothing"
    )
    monkeypatch.setattr(tonight_api, "_INVITE_TIMEOUT_S", 0.3)

    async def never_answers(conn, user_id, payload, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("spielplan.push.send.send_to_user", never_answers)

    live = db_pool.pool()
    await open_room(host)
    in_flight = list(tonight_api._INVITES)
    assert in_flight, "the dispatch never started, so this proves nothing"

    # `wait`, not `wait_for`: a timed-out `wait_for` cancels the task and frees the connection.
    _settled, pending = await asyncio.wait(in_flight, timeout=3.0)
    size, idle = live.get_size(), live.get_idle_size()
    for task in pending:
        task.cancel()
    await asyncio.gather(*in_flight, return_exceptions=True)

    assert not pending, (
        f"the invitation is still in flight against an endpoint that never answers, holding "
        f"{size - idle} of the pool's {live.get_max_size()} connections (size {size}, idle {idle})"
    )
    assert live.get_size() - live.get_idle_size() == 0, (
        f"the dispatch gave up and kept its connection: size {live.get_size()}, idle "
        f"{live.get_idle_size()}"
    )
    answered = await asyncio.wait_for(host.get("/api/auth/me"), timeout=5.0)
    assert answered.status_code == 200, "and every other surface is still being served"


# Findings 35 and 38 are about `SoloBody`, invisible from `solo.picks`.


async def test_the_solo_door_draws_no_pair_and_an_explicit_sharpen_does(app, db, library):
    """`sharpen` defaults to False: the expensive answer must be the one asked for."""
    client, user_id = await admin_client(app)
    await score(db, user_id, library)
    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True}

    door = (await client.post("/api/tonight/solo", json=body)).json()
    sharpened = (await client.post(
        "/api/tonight/solo", json={**body, "sharpen": True}
    )).json()

    assert door["pair"] is None and door["stop_reason"] is None
    assert len(door["picks"]) == 3 and door["wildcard"] is not None
    assert sharpened["pair"] is not None, "the control that exists to ask has to ask"
    assert [p["title_id"] for p in sharpened["picks"]] == [p["title_id"] for p in door["picks"]], (
        "'re-ranks in place' -- with no answers yet, the two orders are the same ranking"
    )


async def test_a_malformed_solo_answer_is_a_refusal_that_names_the_field(app, db, library):
    """Three shapes, three failures: missing key, unparsable value, and an answer outside the four
    (which used to be dropped silently)."""
    client, user_id = await admin_client(app)
    await score(db, user_id, library)
    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True}

    missing = await client.post(
        "/api/tonight/solo", json={**body, "answers": [{"answer": "A", "title_b": 2}]}
    )
    unparsable = await client.post(
        "/api/tonight/solo",
        json={**body, "answers": [{"title_a": "x", "title_b": 2, "answer": "A"}]},
    )
    not_an_answer = await client.post(
        "/api/tonight/solo",
        json={**body, "answers": [{"title_a": 1, "title_b": 2, "answer": "MAYBE"}]},
    )

    for res in (missing, unparsable, not_an_answer):
        assert res.status_code == 422, res.text
    assert ["body", "answers", 0, "title_a"] in [
        d["loc"] for d in missing.json()["detail"]
    ], missing.text
    assert ["body", "answers", 0, "title_a"] in [
        d["loc"] for d in unparsable.json()["detail"]
    ], unparsable.text
    assert ["body", "answers", 0, "answer"] in [
        d["loc"] for d in not_an_answer.json()["detail"]
    ], not_an_answer.text
    # Nothing written, refusal or not: solo mints no row.
    assert await db.fetchval("SELECT count(*) FROM session") == 0


async def test_the_arm_is_still_the_servers_and_never_the_clients(app, db, library):
    """`SoloAnswer` has no `selection`; an extra key is ignored and the arm re-derived."""
    client, user_id = await admin_client(app)
    await score(db, user_id, library)
    key = str(user_id)
    held = next(seq for seq in itertools.count(1) if rnd.is_holdout(seq, key=key))
    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True}
    honest = [
        {"seq": seq, "title_a": library[0], "title_b": library[1], "answer": rnd.A}
        for seq in range(1, held + 1)
    ]
    lying = [{**a, "selection": rnd.SELECTION_ADAPTIVE} for a in honest]

    a = (await client.post("/api/tonight/solo", json={**body, "answers": honest})).json()
    b = (await client.post("/api/tonight/solo", json={**body, "answers": lying})).json()

    assert a["provenance"] == b["provenance"], "a client moved an answer into the adaptive stream"
    counted = int(a["provenance"].split("tilted by your ")[1].split()[0])
    assert counted == len(honest) - 1, (
        f"{len(honest)} answers sent, the one at pair {held} held out, provenance claims {counted}"
    )


class _Records:
    """Takes every frame, to assert what the hub was asked to send."""

    def __init__(self):
        self.frames = []

    async def send_json(self, data):
        self.frames.append(data)

    async def close(self, code=1000):
        return None


async def _frames_settled():
    from spielplan.api import tonight as tonight_api

    await asyncio.gather(*list(tonight_api._FRAMES), return_exceptions=True)


async def test_a_ballot_submit_pushes_the_submitted_count_and_no_approvals(app, db, library):
    """The ballot's own count, two integers and no approval (54e), then the reveal frame."""
    from spielplan.api import tonight as tonight_api

    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    room = await open_room(host)
    sid = room["session_id"]
    joined = (await member.post("/api/tonight/sessions/join", json={"session_id": sid})).json()
    await host.post(f"/api/tonight/sessions/{sid}/start")
    host_seat = room["lobby"]["seats"][0]["participant_id"]
    await _play_out(host, host_seat)
    await _play_out(member, joined["participant_id"])
    slate = (await host.get(f"/api/tonight/sessions/{sid}/ballot")).json()["slate"]

    watcher = _Records()
    sub = tonight_api.HUB.subscribe(watcher, user_id=member_id, session_id=sid)
    try:
        first = await host.post(
            f"/api/tonight/seats/{host_seat}/ballot", json={"approved": [slate[0]["title_id"]]}
        )
        assert first.status_code == 200, first.text
        await _frames_settled()
    finally:
        tonight_api.HUB.unsubscribe(sub)

    # Equality over the whole frame: there is no key an approval could hide under.
    ballots = [f for f in watcher.frames if f["kind"] == "ballot"]
    assert ballots == [{"kind": "ballot", "session_id": sid, "submitted": 1, "seated": 2}]
    assert not [f for f in watcher.frames if f["kind"] == "progress"], (
        "the round's progress frame is not the ballot's count"
    )


async def test_each_seated_member_sets_their_own_vetoes_over_http(app, db, library):
    """Decisions 480 and 505: any seated member sets up to three of their own; the lobby carries the
    union and each seat's own."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    room = await open_room(host)
    sid = room["session_id"]
    assert [o["key"] for o in room["lobby"]["veto_options"]] == [
        "violence", "sexual_violence", "horror", "harrowing"
    ]
    assert room["lobby"]["vetoes"] == []

    outsider = await member.post(f"/api/tonight/sessions/{sid}/vetoes", json={"vetoes": ["horror"]})
    assert outsider.status_code == 403, outsider.text

    await member.post("/api/tonight/sessions/join", json={"session_id": sid})
    full = await host.post(
        f"/api/tonight/sessions/{sid}/vetoes", json={"vetoes": ["violence", "horror", "harrowing"]}
    )
    assert full.status_code == 200, full.text
    set_ = await member.post(
        f"/api/tonight/sessions/{sid}/vetoes", json={"vetoes": ["sexual_violence"]}
    )
    assert set_.status_code == 200, set_.text
    union = ["violence", "sexual_violence", "horror", "harrowing"]
    assert [v["key"] for v in set_.json()["vetoes"]] == union
    mine = next(s for s in set_.json()["seats"] if s["user_id"] == member_id)
    assert mine["vetoes"] == [{"key": "sexual_violence", "label": "sexual violence"}]
    lobby = (await host.get(f"/api/tonight/sessions/{sid}")).json()
    assert [v["key"] for v in lobby["vetoes"]] == union
    assert [v["key"] for v in lobby["me"]["vetoes"]] == ["violence", "horror", "harrowing"]
    rooms_row = next(
        r for r in (await host.get("/api/tonight/rooms")).json()["rooms"] if r["session_id"] == sid
    )
    assert [v["key"] for v in rooms_row["vetoes"]] == union

    bad = await member.post(f"/api/tonight/sessions/{sid}/vetoes", json={"vetoes": ["gore"]})
    assert bad.status_code == 422, bad.text
    four = await member.post(
        f"/api/tonight/sessions/{sid}/vetoes", json={"vetoes": union}
    )
    assert four.status_code == 422, four.text


async def test_the_round_card_says_what_to_expect_rather_than_the_cap(solo_room):
    """The card carries the sweep's median beside the cap."""
    client, seat = solo_room["client"], solo_room["seat"]
    card = (await client.get(f"/api/tonight/seats/{seat}/round")).json()
    assert card["typical"] == rnd.TYPICAL_PAIRS
    assert card["typical"] < card["cap"] == rnd.CAP_PAIRS


def test_the_reveal_route_asks_the_callers_own_toggle_before_it_shows_d():
    """The result route passes the caller's Show-the-model answer to the reveal builder (decision 486)."""
    import inspect

    from spielplan.api import tonight as tonight_api

    source = inspect.getsource(tonight_api.result)
    assert "show_model=rail.visible_to(user)" in source
