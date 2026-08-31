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
    created = await admin.post("/api/setup/members", json={"name": name, "role": "member"})
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
    "the rail and every inline annotation"."""
    client, seat, user_id = solo_room["client"], solo_room["seat"], solo_room["user_id"]
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    off = (await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "A"}
    )).json()
    assert "rail" not in off, "decision 117: the rail is off by default"

    await db.execute("UPDATE app_user SET show_model = true WHERE id = $1", user_id)
    token = (await client.get(f"/api/tonight/seats/{seat}/round")).json()["card_token"]
    on = (await client.post(
        f"/api/tonight/seats/{seat}/answer", json={"card_token": token, "answer": "B"}
    )).json()

    assert on["rail"], "with the toggle on the round narrates its own write"
    line = on["rail"][0]
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
    host_rail = (await host.post(
        f"/api/tonight/seats/{host_seat}/answer",
        json={"card_token": host_token, "answer": "A"},
    )).json()["rail"]

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
    together." Enforced in `ballot.tally`, which this route calls — so the TV route and this one
    cannot disagree about when the evening is revealed."""
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
    """A push service that records who was asked, and can be told to fail."""

    def __init__(self, *, raises: Exception | None = None, status: int = 201) -> None:
        self.urls: list[str] = []
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

    assert member_device in service.urls, "the other member's phone is the point of the push"
    assert host_device not in service.urls, "the host is holding the phone that opened the room"
    assert room["room_code"], "and the room exists either way"


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
    `selection`, whose dataclass default is `adaptive`. Every tenth sharpen pair is drawn by the
    hold-out arm and stamped as such on the way out, and arrived back stamped adaptive: both
    `replay`'s filter and solo's own live-count filter were dead by construction, and the answer
    moved the posterior that selection and stopping read.

    The seq is the one thing the server does not have to trust the client for -- `is_holdout` is
    a function of it -- so the arm is re-derived here rather than accepted.
    """
    host, host_id = await admin_client(app)
    await score(db, host_id, library)

    body = {"kind": "movie", "runtime_budget_min": 200, "include_rewatches": True, "offset": 0}
    first = (await host.post("/api/tonight/solo", json={**body, "answers": []})).json()
    if first.get("pair") is None:
        pytest.skip("this pool serves no sharpen pair")

    # Walk to the hold-out slot, answering whatever is served.
    answers = []
    seen_holdout = None
    for seq in range(1, rnd.HOLDOUT_EVERY + 1):
        out = (await host.post("/api/tonight/solo", json={**body, "answers": answers})).json()
        pair = out.get("pair")
        if pair is None:
            pytest.skip("this pool converged before the hold-out slot")
        if pair["selection"] == rnd.SELECTION_HOLDOUT:
            seen_holdout = seq
        answers.append(
            {"seq": seq, "title_a": pair["a"]["title_id"], "title_b": pair["b"]["title_id"],
             "answer": rnd.A}
        )
    assert seen_holdout == rnd.HOLDOUT_EVERY, "the arm fires on the seq this test is about"

    # The provenance line counts the answers that actually tilted the picks. The hold-out is not
    # one of them, so it reports one fewer than were sent -- which is the whole claim, visible.
    after = (await host.post("/api/tonight/solo", json={**body, "answers": answers})).json()
    assert "tilted by your" in after["provenance"]
    counted = int(after["provenance"].split("tilted by your ")[1].split()[0])
    assert counted == len(answers) - 1, (
        f"{len(answers)} answers sent, one of them held out, provenance claims {counted}"
    )
