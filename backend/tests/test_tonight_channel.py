"""The session channel. Spec v2.1 §6.2 step 2, §1 ("REST + WebSocket"), 54c.

Pure, in its first half: the hub takes a `Socket` Protocol rather than `fastapi.WebSocket`, so its
routing rules — who receives which frame, and what a frame may contain — are testable without a
browser or an ASGI server. Same reason `connectors/jellyfin.py` takes an injected transport.

Two rules, and both are about what a frame can carry rather than what a screen draws:

  * **Two audiences.** "A room opened" is household news (§6.2 step 2: the open-rooms list is
    "visible to every household device"); a room's seats and progress are the room's. A hub that
    sent everything to everyone would put one evening's progress on a device in another room.
  * **Blind by construction.** Every frame is built by `rooms.lobby` or `play.progress`, and
    neither can return an answer. The tests below assert that on the frame builders directly,
    because the hub is exactly the place a later feature reaches for "just send them everything".

AND THE ROUTE THAT OWNS THE SOCKET, at the end of the file, against the running app. Two of this
channel's defects cannot be seen from the hub's side: the route held one of the pool's ten
connections across its opening sends, and every Tonight write awaited the fan-out inside the
request. Both are measurements of the app rather than of this module, so those tests take the `app`
fixture and drive a real handshake over ASGI — and they are here rather than in
`test_tonight_routes.py` because this is the file a reader opens to find out what the channel does.
Skipped without TEST_DATABASE_URL, unlike everything above them; see tests/conftest.py.
[decision 225]
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fastapi.routing import APIWebSocketRoute

from spielplan.api import deps
from spielplan.api import tonight as tonight_api
from spielplan.app import create_app
from spielplan.db import pool as db_pool
from spielplan.tonight import channel


class Recorder:
    """A socket that remembers, and optionally refuses.

    `closed` keeps the code rather than a flag: the hub closing a device it has given up on is a
    different event from a device closing itself, and only one of them carries a code the hub
    chose. [M4.12 finding 17]
    """

    def __init__(self, *, fails: bool = False) -> None:
        self.frames: list[dict] = []
        self.fails = fails
        self.closed: int | None = None

    async def send_json(self, data) -> None:
        if self.fails:
            raise ConnectionError("this phone locked")
        self.frames.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_a_household_frame_reaches_every_device():
    """§6.2 step 2: "active sessions are visible to **every household device**". The existence
    of a room is household news — that is what makes the open-rooms list live."""
    hub = channel.Hub()
    here, there = Recorder(), Recorder()
    hub.subscribe(here, user_id=1)
    hub.subscribe(there, user_id=2, session_id=7)

    sent = await hub.to_household(channel.rooms_changed())
    assert sent == 2
    assert here.frames[0]["kind"] == channel.ROOMS_CHANGED
    assert there.frames[0]["kind"] == channel.ROOMS_CHANGED


async def test_a_session_frame_reaches_only_the_room_it_is_about():
    """One household can hold two evenings — a member watching the lobby of room A must not
    receive room B's progress, which names who is in it and how far along they are."""
    hub = channel.Hub()
    in_room, elsewhere, idle = Recorder(), Recorder(), Recorder()
    hub.subscribe(in_room, user_id=1, session_id=7)
    hub.subscribe(elsewhere, user_id=2, session_id=8)
    hub.subscribe(idle, user_id=3)

    sent = await hub.to_session(7, channel.progress_frame(7, []))
    assert sent == 1
    assert in_room.frames and not elsewhere.frames and not idle.frames


async def test_a_device_that_stopped_answering_does_not_stop_the_others():
    """The lobby is the screen a household is looking at while somebody's phone locks."""
    hub = channel.Hub()
    dead, alive = Recorder(fails=True), Recorder()
    hub.subscribe(dead, user_id=1, session_id=7)
    hub.subscribe(alive, user_id=2, session_id=7)

    sent = await hub.to_session(7, channel.progress_frame(7, []))
    assert sent == 1
    assert alive.frames
    assert hub.size == 1, "the dead subscriber is dropped rather than retried forever"


async def test_unsubscribing_is_idempotent():
    """A socket closes once; a `finally` that ran twice must not raise into the disconnect."""
    hub = channel.Hub()
    sub = hub.subscribe(Recorder(), user_id=1)
    hub.unsubscribe(sub)
    hub.unsubscribe(sub)
    assert hub.size == 0


def test_the_progress_frame_carries_counts_and_never_an_answer():
    """54c: "Someone who finishes early sees the others' **progress and never their answers**"
    — "the payload cannot carry the answers, not that the UI declines to draw them"."""
    frame = channel.progress_frame(
        7,
        [
            {"participant_id": 1, "seat": 1, "name": "patrick", "answered": 6,
             "expected": 20, "finished": True, "ended_by": "converged"},
            {"participant_id": 2, "seat": 2, "name": "jenny", "answered": 9,
             "expected": 20, "finished": False, "ended_by": None},
        ],
    )
    assert frame["waiting_for"] == 1
    assert frame["participants"][0]["answered"] == 6

    keys = {k for p in frame["participants"] for k in p}
    assert keys == {
        "participant_id", "seat", "name", "answered", "expected", "finished", "ended_by"
    }
    for leaked in ("title", "EITHER", "NEITHER", "tilt", "card_token"):
        assert leaked not in repr(frame)


def test_the_reveal_frame_carries_no_result():
    """54e's simultaneity is a moment, not a payload. Every device fetches the reveal over REST
    when this lands, so `ballot.tally`'s guard stays the single place the blind rule is
    enforced — a frame that carried the winner would be a second path with no guard on it."""
    frame = channel.reveal_frame(7)
    assert set(frame) == {"kind", "session_id"}
    assert frame["kind"] == channel.REVEAL


def test_the_rooms_frame_may_be_empty():
    """A device that receives a bare `rooms.changed` re-reads over REST, which is what a
    reconnecting client does anyway — so the frame never has to carry a list the sender would
    have to scope per recipient."""
    assert channel.rooms_changed() == {"kind": channel.ROOMS_CHANGED, "rooms": []}
    assert channel.rooms_changed([{"session_id": 1}])["rooms"] == [{"session_id": 1}]


@pytest.mark.parametrize(
    "builder",
    [
        lambda: channel.rooms_changed(),
        lambda: channel.progress_frame(1, []),
        lambda: channel.reveal_frame(1),
        lambda: channel.lobby_frame({"session_id": 1, "seats": []}),
    ],
)
def test_every_frame_names_its_kind(builder):
    """A client switches on `kind`; a frame without one is a frame nobody can route."""
    frame = builder()
    assert frame["kind"] in (
        channel.ROOMS_CHANGED, channel.LOBBY, channel.PROGRESS, channel.REVEAL
    )


def test_a_frame_is_encoded_for_the_wire_before_it_is_sent():
    """`WebSocket.send_json` is plain `json.dumps` — unlike a FastAPI response, which runs
    `jsonable_encoder` first. §6.2 step 2's own open-rooms row carries an age ("3 min ago"), so
    the very first frame a device receives holds a `datetime`.

    The e2e found this: the socket raised `TypeError` on its first send, died before delivering
    anything, and the lobby silently fell back to the last REST read. Nothing logged on the
    client and nothing looked broken on the server — which is why it is asserted here, on the
    frame, rather than left to the transport.
    """
    import json
    from datetime import UTC, datetime

    frame = channel.rooms_changed(
        [{"session_id": 1, "room_code": "MX-2210", "started_at": datetime.now(UTC)}]
    )
    with pytest.raises(TypeError):
        json.dumps(frame)
    assert json.dumps(channel.wire(frame)), "the wired frame has to survive json.dumps"


async def test_every_frame_the_hub_sends_survives_json_dumps():
    """The hub is where it is fixed, so a later frame that grows a timestamp inherits it — and
    the `lobby` frame already has one."""
    import json
    from datetime import UTC, datetime

    hub = channel.Hub()
    seen = Recorder()
    hub.subscribe(seen, user_id=1, session_id=7)
    await hub.to_session(
        7, channel.lobby_frame({"session_id": 7, "started_at": datetime.now(UTC), "seats": []})
    )
    assert seen.frames
    assert json.dumps(seen.frames[0])


def test_wiring_leaves_everything_else_alone():
    """A converter that stringified more than it had to would change what the client sees."""
    frame = {"kind": "progress", "session_id": 7, "n": 3, "ok": True, "name": "patrick",
             "seats": [{"answered": 6, "ended_by": None}]}
    assert channel.wire(frame) == frame


class Stalls:
    """A socket that accepts the frame and never finishes writing it.

    Not the same failure as `Recorder(fails=True)`, and the difference is the point: a phone
    that has gone away raises, and a phone on a bad connection or a laptop that suspended
    mid-write does neither. It is the ordinary failure of a household WebSocket.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()

    async def send_json(self, data) -> None:
        self.entered.set()
        await asyncio.Event().wait()

    async def close(self, code: int = 1000) -> None:
        # A close is a send: it writes a close frame through the transport that has just stopped
        # draining, so on this socket it stalls too. Not a contrived double -- it is the reason the
        # hub's close has to be bounded at all: the tidy-up for one device must not cost the
        # household the frame the others are waiting for. [M4.12 finding 17]
        self.closing.set()
        await asyncio.Event().wait()


async def test_one_socket_that_never_finishes_writing_does_not_hold_the_others(monkeypatch):
    """§6.2 step 2's lobby is live for the household, not for whoever the hub reaches first.

    `_deliver` awaited each socket in turn, so one device that stopped draining held every
    frame behind it: the other phones' lobbies stopped updating and the reveal beat 54e times
    across the household never arrived. The module said the opposite in a comment above a queue
    field nothing ever read.
    """
    monkeypatch.setattr(channel, "SEND_TIMEOUT", 0.05)
    hub = channel.Hub()
    stalled, alive = Stalls(), Recorder()
    hub.subscribe(stalled, user_id=1, session_id=7)
    hub.subscribe(alive, user_id=2, session_id=7)

    sent = await asyncio.wait_for(hub.to_session(7, channel.progress_frame(7, [])), timeout=2.0)

    assert sent == 1
    assert alive.frames, "the device that was answering got the frame"
    assert stalled.entered.is_set(), "and the stalled one was not skipped, it was given up on"
    assert hub.size == 1, "a socket that cannot take a frame is dropped, like one that raises"


async def test_a_device_the_hub_gives_up_on_is_closed_and_not_only_forgotten(monkeypatch):
    """Unsubscribing is half of giving up on a device; the other half is telling it.

    The client's only re-read path is `socket.onclose` (`tonight.svelte.js`), so a hub that drops a
    subscriber and nothing else leaves that phone connected, deaf, and certain its lobby is live
    for the rest of the evening -- which is the exact failure §6's preamble makes this channel the
    answer to, arriving through the guard written to contain it.

    Both shapes, because they are two different failures and the hub was silent about both: the
    device that raised, and the device that accepted the frame and never finished taking it. The
    stalled one's `close` stalls as well, so the bound on the tidy-up is asserted here too -- the
    whole fan-out still returns, and the device that was answering still has its frame.
    [M4.12 finding 17; decision 225]
    """
    monkeypatch.setattr(channel, "SEND_TIMEOUT", 0.05)
    hub = channel.Hub()
    gone, stalled, alive = Recorder(fails=True), Stalls(), Recorder()
    for i, socket in enumerate((gone, stalled, alive)):
        hub.subscribe(socket, user_id=i, session_id=7)

    sent = await asyncio.wait_for(hub.to_session(7, channel.progress_frame(7, [])), timeout=2.0)

    assert sent == 1 and alive.frames, "the device that was answering got the frame"
    assert gone.closed is not None, (
        "the device that raised was forgotten without being closed, so its onclose never fires "
        "and it never reconnects"
    )
    assert stalled.closing.is_set(), (
        "the device that stopped draining was forgotten without being closed - the same silence, "
        "and the failure that does not announce itself"
    )
    assert alive.closed is None, "only the devices the hub gave up on are closed"
    assert hub.size == 1


async def test_a_slow_socket_costs_the_others_nothing():
    """The stall above is the extreme; the everyday case is one device simply slower than the
    rest. Delivery is concurrent, so the frame costs the household the slowest single socket
    rather than the sum of all of them."""
    delay = 0.05

    class Slow:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_json(self, data) -> None:
            await asyncio.sleep(delay)
            self.frames.append(data)

    hub = channel.Hub()
    sockets = [Slow() for _ in range(6)]
    for i, socket in enumerate(sockets):
        hub.subscribe(socket, user_id=i, session_id=7)

    started = asyncio.get_running_loop().time()
    sent = await hub.to_session(7, channel.progress_frame(7, []))
    elapsed = asyncio.get_running_loop().time() - started

    assert sent == 6 and all(s.frames for s in sockets)
    assert elapsed < delay * len(sockets) / 2, (
        f"{elapsed:.3f}s for six devices at {delay}s each - delivery is serialised"
    )


# --- the route that owns the socket -------------------------------------------------------------


def _leaves(routes):
    """Every route object the app will actually match, unwrapped.

    FastAPI 0.141 does not flatten `include_router`: an included router is one opaque route whose
    children come out of `effective_candidates()` with the prefixes applied, and each child is a
    context wrapper rather than the route itself — for the WebSocket the wrapper's own `path` is the
    empty string and only `original_route` knows where the socket lives.
    `test_route_inventory.py::_leaf_routes` records the same discovery; this is that walk, narrowed
    to one path, and the duplication is deliberate rather than an import across test modules.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _leaves(candidates())
            continue
        original = getattr(route, "original_route", None)
        yield original if original is not None else route
        yield from _leaves(getattr(route, "routes", ()))


def _resolves(dependant, target) -> bool:
    """Whether `target` is anywhere in this route's dependency tree.

    `test_api_gating.py::_behind`, written out rather than imported: that module reaches its walk
    through `paths_behind`, which enumerates `(METHOD, path)` pairs and so cannot see a route that
    has no methods at all.
    """
    return any(sub.call is target or _resolves(sub, target) for sub in dependant.dependencies)


def test_the_channel_is_behind_the_dependency_graph_and_never_behind_deps_db():
    """Both halves of decision 225, asserted where they are decidable: on the route's dependant.

    §3.2 puts every route behind a session and §3.1 locks a new account to a password change until
    it is exchanged. This socket opened the cookie itself and called `auth.load_session`, so its
    dependant tree was empty: the lock was a paragraph in the route body instead of the gate every
    other route is measured at, and `test_api_gating.py`'s sweep — which subtracts what is behind
    `active_user` from what is behind `current_user` — could see neither side of it. A socket *can*
    take a dependency; what it cannot take is an HTTP one.

    The second assertion is the half a later reading loses, because taking `conn: DB` is what every
    other route does and reads as the tidy fix. `deps.db` holds its connection until the endpoint
    returns, and this endpoint returns when the household closes the app — so ten phones watching a
    lobby would hold all ten of the pool's connections for the evening, which is finding 16 with a
    longer lease. Raising `max_size` moves that wall rather than removing it.
    [findings 16, 20; decision 225]
    """
    found = [
        route
        for route in _leaves(create_app().routes)
        if isinstance(route, APIWebSocketRoute) and route.path == "/api/tonight/channel"
    ]
    assert len(found) == 1, f"the app has {len(found)} Tonight channel routes, not one"
    dependant = found[0].dependant

    assert _resolves(dependant, deps.active_user_ws), (
        "the Tonight channel does not resolve `active_user_ws`: its auth is written out in the "
        "route body, where no dependency sweep can see it and nothing holds it to section 3.1"
    )
    assert not _resolves(dependant, deps.db), (
        "the Tonight channel takes `deps.db`, and a yield dependency on a socket lives as long as "
        "the socket - one of the pool's ten connections per open phone, for the whole evening"
    )


class _WontRead:
    """An ASGI peer that completes the handshake and then never takes a frame.

    The ordinary failure of a household WebSocket, and `Stalls` above one layer down: a phone whose
    screen locked mid-evening is still connected, its socket is still writable as far as the server
    is concerned, and its receive buffer never drains. Nothing raises, so nothing ends by itself.
    """

    def __init__(self) -> None:
        self.accepted = asyncio.Event()
        self.writing = asyncio.Event()
        self._connected = False

    async def receive(self) -> dict:
        if not self._connected:
            self._connected = True
            return {"type": "websocket.connect"}
        # The client stays connected. A disconnect here would end the route before anything is
        # measured, and it is the handler's `receive_text` that holds the socket open all evening.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(self, message: dict) -> None:
        if message["type"] == "websocket.accept":
            self.accepted.set()
            return
        self.writing.set()
        await asyncio.Event().wait()


def _handshake_scope(client, path: str) -> dict:
    """One WebSocket connect scope carrying this client's cookies.

    httpx has no WebSocket transport, so the app is called directly rather than through the client,
    and the cookie jar is what makes the socket this account's — the same private-transport
    arrangement `test_api_gating.py::_websocket` and `test_route_inventory.py::_handshake` both use.
    """
    cookies = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    return {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 51000),
        "server": ("test", 80), "subprotocols": [],
        "headers": [(b"host", b"test"), (b"cookie", cookies.encode())],
    }


@pytest.fixture
async def signed_in(app, db):
    """One active session, and the one row a Tonight write needs.

    §3.1's first-boot admin is not locked to a password change, so the setup call is the whole
    sign-in; and `_bundle_version` refuses a bundle-less app with a 409 before `open_session` is
    reached, so the bundle row is the other half of the smallest fixture a Tonight write takes. No
    titles and no scores: nothing below starts a round.
    """
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('chan-v1', '{}', 'active')"
    )
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    return client


async def test_phones_watching_the_lobby_hold_none_of_the_pools_connections(signed_in):
    """The socket lives for the evening, so whatever it holds, it holds for the evening.

    `channel()` awaited its two opening sends *inside* `async with db_pool.acquire()`. A client that
    is connected but not reading blocks that send with nothing to end it, so the connection was
    pinned — and `db/pool.py` opens the pool with `max_size=10`, which is why this is not a Tonight
    failure: with ten phones in that state Rate, Home and auth stop answering and nothing anywhere
    reports an error. The repair is to build both payloads inside the acquire, exit the block, and
    only then write to the socket.

    As many sockets as the pool has connections, read off the pool rather than written as ten, so
    the test measures the wall rather than a number someone typed. Both readings are asserted: the
    pool, which is the mechanism, and an ordinary authenticated GET, which is what the household
    notices. [finding 16; decision 225]
    """
    live = db_pool.pool()
    phones = [_WontRead() for _ in range(live.get_max_size())]
    application = signed_in._transport.app
    scope = _handshake_scope(signed_in, "/api/tonight/channel")
    sockets = [
        asyncio.create_task(application(dict(scope), phone.receive, phone.send))
        for phone in phones
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(phone.writing.wait() for phone in phones)), timeout=10.0
        )
        checked_out = live.get_size() - live.get_idle_size()
        assert checked_out == 0, (
            f"{len(phones)} phones sitting on an opening frame are holding {checked_out} of the "
            f"pool's {live.get_max_size()} connections (size {live.get_size()}, idle "
            f"{live.get_idle_size()}) - the sends are inside the acquire"
        )

        answered = None
        with contextlib.suppress(TimeoutError):
            answered = await asyncio.wait_for(signed_in.get("/api/auth/me"), timeout=5.0)
        assert answered is not None and answered.status_code == 200, (
            "an ordinary authenticated GET could not be served while those phones sat on their "
            "opening frame - the pool is what they were holding, and every surface shares it"
        )
    finally:
        for socket in sockets:
            socket.cancel()
        await asyncio.gather(*sockets, return_exceptions=True)


async def test_a_handshake_on_an_exhausted_pool_is_closed_rather_than_left_hanging(
    signed_in, monkeypatch, caplog
):
    """The gate decision 225 added was the one acquire in `api/deps.py` that was not bounded.

    `db` forty lines above it spends a whole docstring on why it is (`_ACQUIRE_TIMEOUT_S`, a 503
    and a census line naming size and idle), and `current_user_ws` took its connection with a bare
    `async with pool.acquire()` — asyncpg's queue wait, no bound. FastAPI resolves dependencies
    before the endpoint body, so that wait happens before `websocket.accept()`: measured against a
    saturated pool, `GET /api/auth/me` answered 503 in 10.0 s with its log line while the handshake
    was still pending at 13.0 s having sent nothing at all and logged nothing. The client has no
    handshake timer — `connect()` schedules its reconnect from `onclose` alone — so the lobby
    banner is silently dead rather than backing off.

    1011 and not 1008: §3.2's door refuses a caller, and this is the server failing. The bound is
    cut to a fraction of a second so the assertion is about the branch rather than about ten
    seconds of wall clock, and `wait_for` is what turns a regression into a red run instead of a
    wedged one. [M4.12 review cycle 2: M412-API2-01; decision 225]
    """
    monkeypatch.setattr(deps, "_ACQUIRE_TIMEOUT_S", 0.25)
    live = db_pool.pool()
    held = [await live.acquire() for _ in range(live.get_max_size())]
    sent: list[dict] = []
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}]

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    application = signed_in._transport.app
    scope = _handshake_scope(signed_in, "/api/tonight/channel")
    try:
        assert live.get_idle_size() == 0, "the pool is not exhausted, so this proves nothing"
        with caplog.at_level(logging.WARNING, logger="spielplan.api.deps"):
            try:
                await asyncio.wait_for(application(scope, receive, send), timeout=10.0)
            except TimeoutError:
                pytest.fail(
                    "the handshake is still pending against an exhausted pool, with no frame, no "
                    "close and no upper bound - every HTTP surface answers 503 in this state"
                )
    finally:
        for conn in held:
            await live.release(conn)

    assert [frame["type"] for frame in sent] == ["websocket.close"], (
        f"the socket was neither served nor closed: {[f['type'] for f in sent]}"
    )
    assert sent[0].get("code") == 1011, (
        f"closed with {sent[0].get('code')} rather than 1011 - a pool with nothing to hand out is "
        "the server failing, not the session door refusing this caller"
    )
    said = [r.getMessage() for r in caplog.records if r.name == "spielplan.api.deps"]
    assert said and "pool size" in said[0], (
        f"the acquire gave up on a socket and the log says nothing an operator can act on: {said}"
    )


async def test_a_phone_that_never_takes_a_frame_does_not_delay_the_write(signed_in):
    """§6's budget is the answering phone's, and the hub's timeout was being charged to it.

    `Hub._deliver` gives every device SEND_TIMEOUT (5 s), and a suspended laptop neither takes the
    frame nor raises — so a write that awaited the fan-out paid five seconds for somebody else's lid
    against §6.2's "under 1.5 s". Nothing in any of these responses depends on delivery: this
    module's docstring calls the frames at-most-once nudges to re-read, and every client re-reads
    over REST on reconnect.

    The mechanism is asserted beside the timing, because a fast response on its own cannot tell a
    detached fan-out from a stalled subscriber the hub happened to reach quickly: the frame task is
    still pending when the response is in hand, and the stalled socket is entered afterwards — so
    the delivery really was attempted, and really was not waited for. Opening a room rather than
    answering a card, because it is the cheapest write that fans out and the defect is the await
    rather than the route. [finding 17; decision 225]
    """
    stalled = Stalls()
    sub = tonight_api.HUB.subscribe(stalled, user_id=0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        opened = None
        with contextlib.suppress(TimeoutError):
            opened = await asyncio.wait_for(
                signed_in.post("/api/tonight/sessions", json={"kind": "movie"}), timeout=2.0
            )
        waited = loop.time() - started
        assert opened is not None and opened.status_code == 201, (
            f"opening a room took more than {waited:.2f}s with one suspended phone subscribed: "
            f"the request is awaiting the fan-out, and SEND_TIMEOUT is {channel.SEND_TIMEOUT}s"
        )
        assert waited < 1.0, f"the lobby waited {waited:.2f}s on a device that cannot take a frame"
        assert [task for task in tonight_api._FRAMES if not task.done()], (
            "no fan-out was in flight when the response came back - the frame was either awaited "
            "in the request or never sent at all"
        )
        await asyncio.wait_for(stalled.entered.wait(), timeout=5.0)
    finally:
        tonight_api.HUB.unsubscribe(sub)
        for task in list(tonight_api._FRAMES):
            task.cancel()
        await asyncio.gather(*list(tonight_api._FRAMES), return_exceptions=True)
