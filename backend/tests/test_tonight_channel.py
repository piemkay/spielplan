"""The session channel. Spec v2.1 §6.2 step 2, §1 ("REST + WebSocket"), 54c.

Pure: the hub takes a `Socket` Protocol rather than `fastapi.WebSocket`, so its routing rules —
who receives which frame, and what a frame may contain — are testable without a browser or an
ASGI server. Same reason `connectors/jellyfin.py` takes an injected transport.

Two rules, and both are about what a frame can carry rather than what a screen draws:

  * **Two audiences.** "A room opened" is household news (§6.2 step 2: the open-rooms list is
    "visible to every household device"); a room's seats and progress are the room's. A hub that
    sent everything to everyone would put one evening's progress on a device in another room.
  * **Blind by construction.** Every frame is built by `rooms.lobby` or `play.progress`, and
    neither can return an answer. The tests below assert that on the frame builders directly,
    because the hub is exactly the place a later feature reaches for "just send them everything".
"""

from __future__ import annotations

import asyncio

import pytest

from spielplan.tonight import channel


class Recorder:
    """A socket that remembers, and optionally refuses."""

    def __init__(self, *, fails: bool = False) -> None:
        self.frames: list[dict] = []
        self.fails = fails

    async def send_json(self, data) -> None:
        if self.fails:
            raise ConnectionError("this phone locked")
        self.frames.append(data)


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

    async def send_json(self, data) -> None:
        self.entered.set()
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
