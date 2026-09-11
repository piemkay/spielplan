"""The Tonight surface's HTTP seam. Spec v2.1 §6.2 (rewritten), §6.7, §13; decision 117.

M3's own lesson, written down in docs/TESTING.md: **a row whose `what` names a surface has to be
tested through that surface.** Two of M3's worst findings were rows whose named tests exercised a
domain function directly and never reached the layer the row is about — `api/rank.py` had no
backend test of any kind until the review said so, and hard-coding one selection arm in it passed
the entire suite.

So these are the route tests, and they are about the four things only the route can get wrong:

  * **The seal.** A client that could name `title_a`, `title_b` and `selection` could file its own
    answer into or out of §13's held-out stream. The pair is drawn on the server and handed back
    opaque, and the seal is single-use.
  * **Who may write to a seat.** §6.2 step 2's hand-the-phone puts several participants behind one
    cookie by design — and only guests, and only on the host's device. Any other pairing is one
    member casting another's vote, and it would land in §13's approval share.
  * **What a payload carries.** §6.2 step 3's pool is "internal — never shown as a step", and 54c's
    waiting view is "progress and never their answers". Both are claims about bytes on the wire.
  * **The blind ballot, through the API.** 54e's simultaneity is the round's whole social property,
    and a guard that only exists in a template is not a guard.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import itertools

import httpx
import pytest

from spielplan.push import keys, send
from spielplan.tonight import combine as combine_rules
from spielplan.tonight import round as rnd

BUNDLE = "test-v1"


async def admin_client(app, name="patrick"):
    """The real first-boot admin, through the real wizard route — the same way
    `test_rank_routes.py` gets one. Each call returns a client with its own cookie jar, so one
    test can hold two identities at once."""
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": name, "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    return client, (await client.get("/api/auth/me")).json()["id"]


async def member_client(app, admin, name="jenny"):
    """A second household member, created the way §3.1 says they are: a one-time password, a
    forced change, then a session of their own."""
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
    """Six owned films and an active bundle — enough that the pool is larger than the shortlist,
    which is what gives the round a boundary to resolve."""
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


def leaks_pool(payload) -> list[str]:
    """The pool's own ranking, in any of the shapes it could arrive in.

    §6.2 step 3: the candidate pool is "internal — **never shown as a step**". The v2.1 redesign
    deleted the visible shortlist because a pool rendered before the votes anchors the votes it
    exists to collect — the same anchoring §6.1 forbids by withholding the prediction until
    after the tap.
    """
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


# --- the seal ---------------------------------------------------------------------------------


async def test_an_answer_names_a_sealed_pair_and_never_two_title_ids(solo_room):
    """54b makes `session_answer.selection` the discriminator §13's evaluation depends on. A
    route that accepted the two ids and the arm would let a client file its own answer into or
    out of the held-out stream — and §4.2's tables are append-only, so it could not be undone."""
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


async def test_the_seal_is_single_use(db, solo_room):
    """§13's figures count rows and §4.2's tables are append-only, so a replay would weight one
    judgement twice in the only data admitted to evaluate the round, permanently. The same
    property `api/rank.py` gives a queue pair, for the same reason."""
    client, seat = solo_room["client"], solo_room["seat"]
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
    """The mutation M3's review found on the Rank queue, restated here: an arm the client could
    name — or a constant the route substituted — puts a held-out pair into the adaptive stream,
    and §13's evaluation would then be reading data the model had already seen."""
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
    """Decision 154 fixes the set at four. A fifth value never reaches the domain layer."""
    client, seat = solo_room["client"], solo_room["seat"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]

    res = await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "NO_PULL"}
    )
    assert res.status_code == 422


# --- who may write to a seat ---------------------------------------------------------------


async def test_one_member_cannot_answer_for_another(app, db, library):
    """§6.2 step 4 puts each participant on "their own device", and the blind reveal is
    meaningless if one device can cast another person's votes."""
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
    """§6.2 step 2: "Guests use the initiator's phone after the initiator finishes." One cookie
    speaks for several participants **by design, and only that**."""
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    room = await open_room(host, guests=1)
    guest = next(s["participant_id"] for s in room["lobby"]["seats"] if s["role"] == "guest")
    await host.post(f"/api/tonight/sessions/{room['session_id']}/start")

    assert (await host.get(f"/api/tonight/seats/{guest}/round")).status_code == 200


async def test_a_member_seat_is_never_writable_by_the_host(app, db, library):
    """The other half of the same rule: hand-the-phone covers guests, and nothing else. A host
    who could answer for a seated member could decide the evening alone."""
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
    """A second door into a household's rooms: the frames these routes carry name who is in the
    room and what the evening is."""
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


# --- what a payload carries ---------------------------------------------------------------


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
    """54c: "Someone who finishes early sees the others' **progress and never their answers**"
    — a claim about the payload, not about the UI."""
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
    """§6.7 gives `session_answer(p, pair 4) = A — pool-centred tilt` as one of its four worked
    examples, and decision 117 makes the rail one per-user preference, default off, governing
    "the rail and every inline annotation".

    READ FROM `/api/model-log`, not from the answer response. M4.9 dropped the `limit=5` rail
    this route used to embed: §6.7's drawer is one per user, and a second shorter copy of it on
    one surface, with its own depth and its own refresh, is not the same instrument. The write
    is unchanged and so is the gate — what moved is where a client reads the line.
    """
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
    """§6.7's rail is per user (decision 117), and during the blind round a line filed against
    another account would be an answer leaving its seat — which is the one thing 54c's
    blindness is about."""
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
    # From the one drawer, since M4.9 — the round no longer embeds a rail of its own. The
    # scoping claim is unchanged and is the buffer's, not the payload's: `rail.recent` merges
    # this user's deque with the household's and never another member's.
    host_rail = (await host.get("/api/model-log")).json()["events"]

    assert all(
        line["detail"].get("session_id") is None
        or f"session_answer({seat}," not in line["text"]
        for line in host_rail
    ), "the host's rail carries the member's answer"


# --- join channels, and the room code -------------------------------------------------------


async def test_the_room_code_is_readable_from_the_sessions_own_surface(solo_room):
    """§6 preamble makes the code the channel that still works when push cannot be delivered,
    and §11 hands it to a Home Assistant dashboard."""
    client, sid, code = solo_room["client"], solo_room["session_id"], solo_room["room_code"]

    assert code
    assert (await client.get(f"/api/tonight/sessions/{sid}")).json()["room_code"] == code
    assert (await client.get("/api/tonight/rooms")).json()["rooms"][0]["room_code"] == code


async def test_joining_by_code_and_by_id_reach_the_same_seat(app, db, library):
    """§6.2 step 2: "Join channels, all equivalent" — a claim about what a join produces, not a
    list of affordances. Two seats would change the participant count every average and §13's
    approval share are computed over."""
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
    """§6.2 step 2: "active sessions are visible to **every household device**", carrying the
    facets the spec's own example string names."""
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


# --- the blind ballot, through the API --------------------------------------------------------


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
    """54e: "Approvals stay hidden until every participant has submitted; then they are revealed
    together." Enforced in `ballot.tally`, which this route calls — so this route and the session
    WebSocket cannot disagree about when the evening is revealed. (It was the TV route here
    until decision 165 retired that surface; `tally`'s guard is what M4.12 kept of it.)"""
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
    assert body["unanimous"] is True
    assert body["winner"]["fit_line"]
    assert body["winner"]["match_lines"], "§6.2 step 7: one match line per participant"
    # The runtime does not mean the same thing without the kind: §6.0's label reads a series in
    # minutes per EPISODE, and this was the one card that sent a runtime and no kind, so the
    # reveal printed a series' 45 minutes the way it prints a film's while every other surface
    # said `45m/ep`. It is the SESSION's kind (0013: "an evening resolves to ONE title"), so it
    # is the same on every card. [M4.9 finding 37; review cycle 1: M49-CARD-2]
    assert body["winner"]["kind"] == "movie", body["winner"]
    assert {c["kind"] for c in body["runners_up"] + body["finalists"]} == {"movie"}


async def test_a_ballot_naming_a_title_off_the_slate_is_refused(app, db, library):
    """54e: "among the three finalists and the wildcard". A ballot over the whole pool is a
    ranking exercise, and one naming a title nobody was offered would land in §13's numbers."""
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
    """§13 evaluates the round; a report that can name a candidate invites a surface to draw
    it, which is how a held-out stream stops being held out."""
    client, sid = solo_room["client"], solo_room["session_id"]
    payload = (await client.get(f"/api/tonight/sessions/{sid}/evaluation")).json()

    assert set(payload) == {
        "session_id", "approval_share", "participants", "shortlist_agreement", "ended_by"
    }
    assert "title_id" not in repr(payload)
    assert set(payload["ended_by"]) == set(rnd.END_REASONS)


# --- solo ------------------------------------------------------------------------------------


async def test_solo_returns_picks_without_a_room(app, db, library):
    """54f: solo lands directly on the picks, and §6.2 step 8 mints no session row — so a solo
    evening publishes no room for another household device to join."""
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
    """§3.1: a bundle-less app is a legal state, and artifact-dependent surfaces render an
    explicit "no bundle imported" state instead of erroring."""
    client, _ = await admin_client(app)
    res = await client.post("/api/tonight/solo", json={})
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "no_bundle"

    room = await client.post("/api/tonight/sessions", json={})
    assert room.status_code == 409
    assert room.json()["detail"]["reason"] == "no_bundle"


# --- §6.2 step 2 + §12 M4: push join, through the route --------------------------------------
#
# The sender's own best-effort contract is tested above; these are about the SEAM. The row's
# claim is that *opening a session* invites the right members and that no send outcome can stop
# the evening — both statements about `api/tonight.py`, and neither observable from `send.py`.


class _Recorder(httpx.AsyncBaseTransport):
    """A push service that records who was asked, and can be told to fail.

    `payloads` is what the *route* handed the sender, captured by the fixture below rather than
    read off the wire: on the wire it is one aes128gcm record for a key only the device holds, so
    decrypting it here would assert that this test can read its own output. `test_push_sender.py`
    owns the claim that what the sender is handed is what the browser receives.
    """

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
    """§6.2 step 2: "push to members' phones". The host opened the room and does not need
    telling; a member already seated does not either."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    host_device = await _register(db, host_id, "https://push.test/host")
    member_device = await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task now (finding 42), so the assertions below are about
    # a send that has finished rather than one that may not have started.
    await invites_settled()

    assert member_device in service.urls, "the other member's phone is the point of the push"
    assert host_device not in service.urls, "the host is holding the phone that opened the room"
    assert room["room_code"], "and the room exists either way"


async def test_the_invitation_names_its_own_replacement_key_and_the_surface_that_answers_it(
    secrets_key, app, db, library, recorder
):
    """§6.2 step 2's invitation, as the service worker reads it (syncpush-10).

    A notification tag is a *replacement* key: with none set the worker falls back to its own
    `'spielplan'` for every sender, so the household's newest notification overwrote the previous
    one whatever it was — an invitation silently replacing an unread §7.3 finish prompt, on the
    phone, which is the one place §6's in-app banner is not what the member is looking at. Keying
    it on the session is what makes two rooms two notifications. And §6.2 step 2's answer to an
    invitation is the lobby: a tap that lands on Home makes the member go looking for the room
    they were just told about.

    Asserted against the payload the route handed the sender, and `session_id` against the room
    the route returned — a literal restated here would pass just as well with the field absent.
    """
    host, host_id = await admin_client(app)
    _member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task now (finding 42), so the assertions below are about
    # a send that has finished rather than one that may not have started.
    await invites_settled()

    assert len(service.payloads) == 1, "one invitation, to the member who was not holding the host's phone"
    payload = service.payloads[0]
    assert payload["tag"] == f"tonight:{room['session_id']}"
    assert payload["url"] == "/tonight"
    # The kind is the one the frontend switches on; the tag is for the browser, not instead of it.
    assert payload["kind"] == "tonight.invite"
    assert room["room_code"] in payload["body"]


async def test_a_session_opens_even_when_every_push_fails(
    secrets_key, app, db, library, recorder
):
    """§6's preamble: push is best-effort and every push-carried prompt has an in-app
    equivalent. A lobby that blocked on a delivery receipt would break on exactly the iPhone the
    constraint was written about."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    await _register(db, member_id, "https://push.test/member")

    service = recorder(_Recorder(raises=httpx.ConnectError("no route to the push service")))
    room = await open_room(host)

    # The dispatch is a background task now (finding 42), so the assertions below are about
    # a send that has finished rather than one that may not have started.
    await invites_settled()

    assert service.urls, "the attempt was made"
    assert room["room_code"] and room["session_id"]
    # And the two channels §6's preamble guarantees still reach it.
    joined = await member.post(
        "/api/tonight/sessions/join", json={"room_code": room["room_code"]}
    )
    assert joined.status_code == 200, joined.text
    assert (await member.get("/api/tonight/rooms")).json()["rooms"][0]["room_code"] == room["room_code"]
    assert (await host.post(
        f"/api/tonight/sessions/{room['session_id']}/start"
    )).status_code == 200


async def test_a_member_with_no_phone_is_still_reachable(secrets_key, app, db, library, recorder):
    """The commonest case on day one, and the one §6's preamble is written for: iOS grants push
    only to an installed PWA, so a household member may simply not have a subscription."""
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)

    service = recorder(_Recorder())
    room = await open_room(host)

    # The dispatch is a background task now (finding 42), so the assertions below are about
    # a send that has finished rather than one that may not have started.
    await invites_settled()

    assert service.urls == [], "nothing to send to"
    assert (await member.post(
        "/api/tonight/sessions/join", json={"room_code": room["room_code"]}
    )).status_code == 200


async def test_an_armed_finish_prompt_is_pushed_to_that_member_alone(
    db, secrets_key, recorder
):
    """§7.3: "Push notification if the user isn't in the app — best-effort; when undeliverable,
    the prompt queues and surfaces as an in-app banner on next open. The banner path is the
    whole M1 behaviour; **push arrives with the M4 stack**."
    """
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
    """The fallback §7.3 names is the whole point: the prompt is armed first and the push is
    discarded, so a household with no network still has a banner waiting on next open."""
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
    """§6.4: the exploratory pick is "honestly labelled", and §6.8 makes those words part of the
    rule rather than decoration around it.

    They were spelled in the client. `combine.WILDCARD_LABEL` held the same sentence and had no
    reader anywhere, so the copy the household actually read and the copy the tests asserted
    were two different strings that nothing kept together. The route serves it now, and only on
    the card the honesty is about.
    """
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
    """54b: a hold-out pair is "used for neither selection nor stopping". Solo's sharpen round is
    the same round, so the same rule binds it.

    Solo mints no session row (§6.2 step 8), so its answers travel with the request -- and the
    route rebuilt each one as `round_rules.Answered(seq, title_a, title_b, answer)` with no
    `selection`, whose dataclass default is `adaptive`. The sharpen pairs the hold-out arm drew
    were stamped as such on the way out and arrived back stamped adaptive: both `replay`'s filter
    and solo's own live-count filter were dead by construction, and the answer moved the posterior
    that selection and stopping read.

    The arm is the one thing the server does not have to trust the client for -- it is a function
    of the seq and of `user.id` (decision 223) -- so it is re-derived here rather than accepted.

    NO SKIP, IN EITHER HALF. This test used to give up when the pool converged before pair 10,
    which at the round's own boundary (decision 214) is what this pool does, so the claim went
    unmeasured on every run -- a `pytest.skip` in a registered test is a green suite asserting
    nothing (finding 44). Neither half below needs the round to reach a given length: the first
    walks as far as this pool serves, and the second sends the answers itself, which is what a
    client does anyway -- §6.2 step 8 leaves the server no row to read them off.
    """
    host, host_id = await admin_client(app)
    await score(db, host_id, library)
    key = str(host_id)

    # `sharpen` on every request here, because half one walks the pairs this pool serves and the
    # door draws none now (M4.12 finding 35): 54f's third control is the one that asks, and this
    # test is about the arm of the pairs it asks with.
    body = {
        "kind": "movie", "runtime_budget_min": 200, "include_rewatches": True,
        "offset": 0, "sharpen": True,
    }
    first = (await host.post("/api/tonight/solo", json={**body, "answers": []})).json()
    assert first["pair"] is not None, "54f's sharpen round serves nothing on this pool at all"

    # Half one: every pair this pool serves carries the arm the rule names for this person.
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

    # Half two: the round trip, over a history long enough to hold this person's first hold-out.
    # The provenance line counts the answers that actually tilted the picks, so a hold-out that
    # came back adaptive is one too many -- the whole claim, visible on the screen.
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
    """The property a RATE has to have that a slot schedule had for free (decision 223).

    54b's arm is drawn rather than scheduled now, and solo's arm is re-derived on every request
    because §6.2 step 8 mints no row to store it on. So the draw has to land on the same answer
    every time the same history is posted -- twice in a row, and after a restart, since the client
    holds these answers across one. A key that moved, or a draw keyed through the process's hash
    salt, would file the same answer into §13's stream on one request and out of it on the next:
    the evaluation's denominator would then depend on how often somebody reloaded the screen, which
    is the defect finding 28 describes arriving by a second door. `user.id` is the key because it
    is the only thing here that is stable, server-side and never client-supplied.
    """
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

    # And it is the arm the rule names, not whatever the route felt like: one of these answers is
    # held out and the rest tilted the picks, so the line reports one fewer than were sent.
    counted = int(again[0]["provenance"].split("tilted by your ")[1].split()[0])
    assert counted == len(sent) - 1, (
        f"{len(sent)} answers sent, the one at pair {held} drawn by the arm, {counted} counted"
    )


# --- §6.2 steps 5-6 (54e): the lifecycle settles from a read -----------------------------------
#
# M4.12 finding 4. Only the route can get this wrong, which is why these are here rather than in
# `test_tonight_integration.py`: the rule itself is `play.settle` and is asserted there, and what
# is asserted here is that a GET of the session, the ballot and the result each call it before
# they read. Before this milestone the `voting -> ballot` transition ran from the answer, undo and
# escape handlers and from nowhere else — so one combine that raised left the evening in `voting`
# for ever, the lobby reporting `voting`, the ballot answering 200 with an empty slate and the
# result answering 409 `still_voting`, with every vote in the database and every write refused.

import asyncio  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import asyncpg  # noqa: E402

from spielplan.tonight import play  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


class _FailsOnce:
    """A `play.finish` that loses its connection the first time it is called.

    The fault finding 4 is written about. The answer is already committed when the combine runs,
    so a dropped connection or a container restart between the two leaves the answer standing,
    the request 500ing and the room in `voting` with every seat's `ended_by` set. An asyncpg
    connection error rather than a bare exception, because that is the shape the real fault
    arrives in and the one `app.py`'s handler turns into a 500 a client can read.
    """

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
    """Play this seat out, returning the status of the last answer the server took.

    `_play_out` above discards the statuses; the last one is the whole point here, because the
    answer that finishes the room is the one the combine runs on.
    """
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
    """A room in the state finding 4 leaves: every vote in, no slate, nothing able to move it.

    Reached the way a household reaches it — both members answer through the real routes, and
    the combine fails once on the answer that finishes the room. The round route settles only when
    the read is itself what ended the seat (M412-PLAY-4), and here the answer did, so the room is
    genuinely still stuck when this returns.
    """
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
    """The lobby is the screen every device is already polling, so it is where the recovery has
    to arrive: before this, it reported `voting` for ever on a room whose votes were all in."""
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
    """`GET /ballot` used to answer 200 with `slate: []` — the worst of the three shapes,
    because a client cannot tell it from a room that has not combined yet."""
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
    """The whole of what this row is for: the evening resolves rather than hanging.

    `GET /result` still answers the ballot's own 409 — nobody has voted yet, and that refusal
    lives in `ballot.tally` — but the room it was refused on has moved, so the two members can
    submit and reach the reveal. Before this, the ballot submit answered 422 `not_on_slate`
    against the empty slate and the evening ended in SQL.
    """
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
    """Decision 215's evening, driven the way the client drives it, through the routes.

    On a pool of two or three candidates nothing but the round read can end a seat: there is no
    pair to answer, the escape is refused below pair six, and `boundary` returns None at or below
    the shortlist size, so `play.state_for`'s belt-and-braces is the only writer. That read was
    the one handler here that called neither `play.settle` nor `_announce` and pushed no frame,
    so the device that read the LAST un-ended seat left the room in `voting` with every seat
    `converged` and woke nobody -- and `tonight.svelte.js`'s `refresh()` holds the client's only
    GET of the session and runs it BEFORE its round read, so the one order the rule needed was
    the one order the client never produced. Reproduced exactly as written below: both seats
    `converged`, `SELECT state` still `voting`, and one further GET of the session moved it.

    The domain tests for this clause call `play.state_for` for each seat and then `play.settle`
    themselves, which is why the suite was green: what was missing was a caller, not a rule.
    The session is read before each round read and never after, so nothing but the round route
    can be what moves this room. [M4.12 review cycle 2: M412-PLAY-4; decision 215]
    """
    host, host_id = await admin_client(app)
    member, member_id = await member_client(app, host)
    await score(db, host_id, library)
    await score(db, member_id, library)
    # Three candidates rather than the fixture's six. The budget floor is 60 and the grace is 40,
    # so the pool is narrowed by making the other three films too long to admit rather than by a
    # slider position that cannot be set (§6.2 step 1; `pool.admits`).
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
        # `refresh()`: the session, and then the round. Never the other way round, and never
        # twice -- there is no poll, and no frame is pushed after a start.
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
    """arch-06, and the milestone's own exit criterion: `grep -n 'play.finish'` finds nothing.

    A source read rather than a behavioural assertion, because what is being held is the shape
    of the module: a second route that combined inline would pass every test above while putting
    the transition back where one caller owns it. The rule's own tests are the ones that prove it
    works; this is the one that keeps it from growing a second home.
    """
    source = (BACKEND / "spielplan" / "api" / "tonight.py").read_text(encoding="utf-8")

    assert "play.finish" not in source, "the combine is `play.settle`'s to call"
    assert "play.settle(" in source, "and the router's reads are what call it"
    assert source.count("play.settle(") >= 4, (
        "the answer path and the three reads — the session, the ballot and the result"
    )


# --- §6.2 step 2 + finding 42: the invitation is not awaited into the lobby --------------------


async def invites_settled() -> None:
    """Join the background dispatch `POST /api/tonight/sessions` hands the invitation to.

    The route answers without awaiting the push (finding 42), which is the property the test
    below exists for — so every test that asserts anything about a send has to join the task
    rather than sleep at it. Reaching into the router's own set is the honest way to do that: a
    fixed sleep would be the flake this milestone is removing from the e2e suite.
    """
    from spielplan.api import tonight as tonight_api

    await asyncio.gather(*list(tonight_api._INVITES), return_exceptions=True)

async def test_opening_a_room_returns_before_the_invitation_is_delivered(
    secrets_key, app, db, library, monkeypatch
):
    """The claim this route's own docstring made for a milestone while doing the opposite.

    `_invite` was awaited inline, and it loops over invited members awaiting `send_to_user` for
    each; the sender opens one httpx client with a 10 s timeout per device and posts in sequence.
    Measured with the sender stubbed at 1 s it cost 1.10 s for one member, and two members with
    two phones each on a home connection that is down is up to 40 s of apparently-hung lobby —
    on exactly the iPhone §6's best-effort preamble was written about. The dispatch also holds
    the request's pooled connection for the whole of it, which is ten phones from a stall.
    """
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
    """Finding 42 moved the dispatch off the request and kept the other half of its cost.

    `_invite` acquires once and hands that connection to `rooms.invite`, which awaits one send per
    invited member, and `push/send.py` gives every DEVICE its own 10 s httpx timeout — so a
    household whose endpoints hang (the home connection down, which is the case finding 42 is
    written about) pins one of the pool's ten connections for (members x phones) x 10 s per room
    opened. `rooms.open_session` does not refuse a repeat opening, so opening a few rooms while
    that is true takes the pool down and Rate, Home and auth 503 with nothing in the log pointing
    at Tonight. Measured: ten openings against a hanging sender left pool size 10 idle 0, and
    `GET /api/auth/me` then answered 503 after its own ten seconds.

    The bound is the plan's own alternative for step 12. It is cut to a fraction of a second here
    so the assertion is about the branch rather than about three seconds of wall clock, and the
    shipped number is asserted separately against the thing it has to be tighter than.
    `wait_for` covers the regression: without the bound the task never ends, and a hang inside a
    test is a wedged run rather than a red one. [M4.12 review cycle 2: M412-API2-02]
    """
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

    # `wait` and not `wait_for`: a timed-out `wait_for` cancels what it was waiting on, which
    # releases the connection the assertion is about and leaves the failure reporting a healthy
    # pool. The census is taken while the dispatch is still in whatever state it reached.
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


# --- §6.2 step 8 (54f): the solo body, at the seam -------------------------------------------
#
# M4.12 findings 35 and 38. Both are about `SoloBody` and neither is visible from `solo.picks`:
# the flag is a field the client sends, and the refusal is what FastAPI does with a body the
# route never got to see.


async def test_the_solo_door_draws_no_pair_and_an_explicit_sharpen_does(app, db, library):
    """54f: the door "lands **directly** on three picks and a wildcard", and "a **sharpen this**
    control runs the same adaptive round". Two taps, two costs, and one field between them.

    `sharpen` defaults to False on purpose: the expensive answer has to be the one somebody asked
    for. An older client that sends no such field gets the door, which is the screen it was asking
    for anyway.
    """
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
    """Finding 38. §6.8's register is what a refusal owes, and `SoloBody.answers` was
    `list[dict[str, Any]]`: the route then reconstructed each entry with `int(a["title_a"])`, so a
    missing key left the handler as a `KeyError` and a non-numeric value as a `ValueError` -- a 500
    on a malformed request, on the one Tonight body that had opted out of the validation every
    other body gets for free.

    Three shapes, because they failed three different ways: the missing key, the unparsable value,
    and an answer outside decision 154's four. The last one used to be dropped silently, which is
    a round that quietly counted one fewer answer than the person gave -- worse than a refusal,
    because nothing anywhere says it happened.
    """
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
    # And nothing was written by any of them: §6.2 step 8 mints no row, refusal or not.
    assert await db.fetchval("SELECT count(*) FROM session") == 0


async def test_the_arm_is_still_the_servers_and_never_the_clients(app, db, library):
    """54b, guarded through the new shape rather than around it. `SoloAnswer` deliberately has no
    `selection` field: the arm is a function of the seq and of this person (decision 223), so the
    one field a client must not choose is also the one it never has to send.

    A body that tries anyway is answered exactly as if it had not -- pydantic ignores the extra
    key by default and the route re-derives. The assertion is that the two requests agree, which
    is the property that matters: a client cannot move an answer into or out of §13's stream.
    """
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
