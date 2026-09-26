"""The hub takes a `Socket` Protocol, so its routing and frame rules are tested without a browser.
The route tests at the end need the running app and TEST_DATABASE_URL."""

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
    """`closed` keeps the code: the hub closing a device differs from a device closing itself."""

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
    """The open-rooms list is visible to every household device (§6.2 step 2)."""
    hub = channel.Hub()
    here, there = Recorder(), Recorder()
    hub.subscribe(here, user_id=1)
    hub.subscribe(there, user_id=2, session_id=7)

    sent = await hub.to_household(channel.rooms_changed())
    assert sent == 2
    assert here.frames[0]["kind"] == channel.ROOMS_CHANGED
    assert there.frames[0]["kind"] == channel.ROOMS_CHANGED


async def test_a_session_frame_reaches_only_the_room_it_is_about():
    """One household can hold two evenings; room B's progress names who is in it."""
    hub = channel.Hub()
    in_room, elsewhere, idle = Recorder(), Recorder(), Recorder()
    hub.subscribe(in_room, user_id=1, session_id=7)
    hub.subscribe(elsewhere, user_id=2, session_id=8)
    hub.subscribe(idle, user_id=3)

    sent = await hub.to_session(7, channel.progress_frame(7, []))
    assert sent == 1
    assert in_room.frames and not elsewhere.frames and not idle.frames


async def test_a_device_that_stopped_answering_does_not_stop_the_others():
    hub = channel.Hub()
    dead, alive = Recorder(fails=True), Recorder()
    hub.subscribe(dead, user_id=1, session_id=7)
    hub.subscribe(alive, user_id=2, session_id=7)

    sent = await hub.to_session(7, channel.progress_frame(7, []))
    assert sent == 1
    assert alive.frames
    assert hub.size == 1, "the dead subscriber is dropped rather than retried forever"


async def test_unsubscribing_is_idempotent():
    hub = channel.Hub()
    sub = hub.subscribe(Recorder(), user_id=1)
    hub.unsubscribe(sub)
    hub.unsubscribe(sub)
    assert hub.size == 0


def test_the_progress_frame_carries_counts_and_never_an_answer():
    """54c: the payload cannot carry the answers, not merely that the UI declines to draw them."""
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
    """Devices fetch the reveal over REST, so `ballot.tally`'s guard stays the one blind-rule check."""
    frame = channel.reveal_frame(7)
    assert set(frame) == {"kind", "session_id"}
    assert frame["kind"] == channel.REVEAL


def test_the_ballot_frame_carries_the_submitted_count_and_nothing_else():
    """Two integers and the kind: approvals stay hidden until everyone is in."""
    frame = channel.ballot_frame(7, submitted=1, seated=2)
    assert frame == {"kind": channel.BALLOT, "session_id": 7, "submitted": 1, "seated": 2}
    assert channel.BALLOT not in (
        channel.ROOMS_CHANGED, channel.LOBBY, channel.PROGRESS, channel.REVEAL
    ), "its own kind, so the client never files a ballot count under the round"


def test_the_rooms_frame_may_be_empty():
    """A bare `rooms.changed` makes the device re-read over REST, so no per-recipient list is needed."""
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
    """`WebSocket.send_json` is plain `json.dumps` with no `jsonable_encoder`, and the open-rooms row
    carries a `datetime`: the socket died on its first send."""
    import json
    from datetime import UTC, datetime

    frame = channel.rooms_changed(
        [{"session_id": 1, "room_code": "MX-2210", "started_at": datetime.now(UTC)}]
    )
    with pytest.raises(TypeError):
        json.dumps(frame)
    assert json.dumps(channel.wire(frame)), "the wired frame has to survive json.dumps"


async def test_every_frame_the_hub_sends_survives_json_dumps():
    """Fixed in the hub, so a later frame with a timestamp inherits it."""
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
    """Stringifying more than needed would change what the client sees."""
    frame = {"kind": "progress", "session_id": 7, "n": 3, "ok": True, "name": "patrick",
             "seats": [{"answered": 6, "ended_by": None}]}
    assert channel.wire(frame) == frame


class Stalls:
    """Unlike `Recorder(fails=True)`: a suspended device neither raises nor finishes."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()

    async def send_json(self, data) -> None:
        self.entered.set()
        await asyncio.Event().wait()

    async def close(self, code: int = 1000) -> None:
        # A close is a send and stalls too, which is why the hub's close is bounded.
        self.closing.set()
        await asyncio.Event().wait()


async def test_one_socket_that_never_finishes_writing_does_not_hold_the_others(monkeypatch):
    """One device that stopped draining held every frame behind it; delivery is concurrent now."""
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
    """The client re-reads only on `onclose`, so a dropped device must also be closed. Both shapes,
    and the stalled close is bounded too."""
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
    """Concurrent: a frame costs the slowest single socket, not the sum."""
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


def _leaves(routes):
    """FastAPI 0.141 does not flatten `include_router`; only `original_route` knows the socket's path.
    Duplicated from `test_route_inventory.py` on purpose."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _leaves(candidates())
            continue
        original = getattr(route, "original_route", None)
        yield original if original is not None else route
        yield from _leaves(getattr(route, "routes", ()))


def _resolves(dependant, target) -> bool:
    """`test_api_gating.py::_behind` written out: its walk enumerates methods, and a socket has none."""
    return any(sub.call is target or _resolves(sub, target) for sub in dependant.dependencies)


def test_the_channel_is_behind_the_dependency_graph_and_never_behind_deps_db():
    """Decision 225: behind the dependency graph (§3.1's lock applies), but never `deps.db`, which would
    hold a pool connection for the socket's whole evening."""
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
    """A locked phone stays connected and writable but never drains; nothing raises."""

    def __init__(self) -> None:
        self.accepted = asyncio.Event()
        self.writing = asyncio.Event()
        self._connected = False

    async def receive(self) -> dict:
        if not self._connected:
            self._connected = True
            return {"type": "websocket.connect"}
        # Stays connected: `receive_text` is what holds the socket open all evening.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(self, message: dict) -> None:
        if message["type"] == "websocket.accept":
            self.accepted.set()
            return
        self.writing.set()
        await asyncio.Event().wait()


def _handshake_scope(client, path: str) -> dict:
    """httpx has no WebSocket transport, so the app is called directly with the jar's cookies."""
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
    """The first-boot admin is not password-locked, and a bundle row avoids the bundle-less 409."""
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
    """The opening sends ran inside `db_pool.acquire()`, pinning a connection per stalled phone.
    As many sockets as the pool holds, read off the pool; both the pool and a GET are asserted."""
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
    """The socket gate's acquire must be bounded like `deps.db`'s. 1011, not 1008: the server failed.
    `wait_for` turns a regression into a red run instead of a wedged one."""
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
    """A write must not await the fan-out: SEND_TIMEOUT would charge a suspended device's 5 s to the
    answering phone. The pending frame task proves delivery was attempted but not awaited."""
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
