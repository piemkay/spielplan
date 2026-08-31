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
