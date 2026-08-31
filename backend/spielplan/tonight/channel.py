"""The session channel. Spec v2.1 §6.2 step 2, §1 ("REST + WebSocket"), §6 preamble.

    "a live in-app lobby banner over the WebSocket … the **open-rooms list** — active sessions
     are visible to every household device"
    54c: "Someone who finishes early sees the others' **progress and never their answers**."

WHY A HUB AND NOT A BROADCAST. §6 makes push best-effort, so the in-app channel is the one that
has to work on the iPhone that never delivers a notification — and it carries two different
things to two different audiences. **Household** frames announce that a room opened, and go to
every signed-in device so the open-rooms list and the lobby banner are live. **Session** frames
carry a room's seats and progress, and go only to devices watching that room.

BLIND BY CONSTRUCTION, HERE TOO. Every frame this hub sends is built by `rooms.lobby` or
`play.progress`, and neither can return an answer: one has no join to `session_answer` at all,
the other selects counts and never titles. So the blind property is a fact about what the
payload builders can produce, not a rule this module has to remember — which matters because a
hub is exactly the place a later feature reaches for "just send them everything".

AT-MOST-ONCE, IN MEMORY, AND THAT IS THE RIGHT SCALE. A household is a handful of devices and
one backend process (§1's compose file has one `backend`). A dropped frame costs a stale lobby
until the next REST read, which every client does on reconnect; a broker would cost a service.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("spielplan.tonight.channel")

# Frame kinds. Named rather than free-form so a client can switch on them, and so the two
# audiences below stay legible at every call site.
ROOMS_CHANGED = "rooms.changed"
LOBBY = "lobby"
PROGRESS = "progress"
REVEAL = "reveal"

# One household, one process, a handful of devices: a per-socket queue this deep is a slow
# client, and a slow client is dropped rather than allowed to stall the hub.
QUEUE_DEPTH = 32


class Socket(Protocol):
    """What the hub needs of a connection. A Protocol rather than `fastapi.WebSocket` so the
    hub is testable without a browser or an ASGI server — the same reason
    `connectors/jellyfin.py` takes an injected transport."""

    async def send_json(self, data: Any) -> None: ...


# `eq=False` so a Subscriber hashes by identity. Two devices can hold indistinguishable field
# values — same user, same room, a socket object that compares equal — and they are still two
# subscribers; value equality would silently collapse them into one and drop a phone's frames.
@dataclass(eq=False)
class Subscriber:
    socket: Socket
    user_id: int
    session_id: int | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_DEPTH))


class Hub:
    """The live subscriptions of one backend process."""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()

    def subscribe(self, socket: Socket, *, user_id: int, session_id: int | None = None) -> Subscriber:
        sub = Subscriber(socket=socket, user_id=user_id, session_id=session_id)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def watching(self, session_id: int) -> list[Subscriber]:
        return [s for s in self._subscribers if s.session_id == session_id]

    @property
    def size(self) -> int:
        return len(self._subscribers)

    async def _deliver(self, targets: Iterable[Subscriber], frame: dict[str, Any]) -> int:
        sent = 0
        for sub in list(targets):
            try:
                await sub.socket.send_json(frame)
                sent += 1
            except Exception:
                # A device that has gone away must not stop the frame reaching the others: the
                # lobby is the screen a household is looking at while somebody's phone locks.
                log.debug("dropping a session subscriber that stopped answering")
                self.unsubscribe(sub)
        return sent

    async def to_household(self, frame: dict[str, Any]) -> int:
        """Every signed-in device. Used for "a room opened" — §6.2 step 2's open-rooms list is
        "visible to every household device", so the *existence* of a room is household news."""
        return await self._deliver(self._subscribers, frame)

    async def to_session(self, session_id: int, frame: dict[str, Any]) -> int:
        """Only the devices watching this room."""
        return await self._deliver(self.watching(session_id), frame)


def rooms_changed(rooms: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The banner frame. Carries the room list the receiving device is entitled to see, or
    nothing at all — a device that gets `{"kind": "rooms.changed"}` re-reads over REST, which is
    what a reconnecting client does anyway."""
    return {"kind": ROOMS_CHANGED, "rooms": rooms or []}


def lobby_frame(lobby: dict[str, Any]) -> dict[str, Any]:
    return {"kind": LOBBY, "lobby": lobby}


def progress_frame(session_id: int, progress: list[dict[str, Any]]) -> dict[str, Any]:
    """54c's waiting view: "Patrick 6/6 ✓ · Jenny 9/~12 · Mia 4/~10 · waiting for 2".

    Built by `play.progress`, which has no join to `session_answer` — so this frame cannot
    carry an answer even if a later caller wanted it to.
    """
    return {
        "kind": PROGRESS,
        "session_id": session_id,
        "participants": progress,
        "waiting_for": sum(1 for p in progress if not p["finished"]),
    }


def reveal_frame(session_id: int) -> dict[str, Any]:
    """54e's simultaneity, as a moment rather than a state: "Approvals … are revealed together."

    Deliberately carries no result. Every device fetches the reveal over REST when this lands,
    so the ballot guard in `ballot.tally` is the single place the blind rule is enforced — a
    frame that carried the winner would be a second path to the same data with no guard on it.
    """
    return {"kind": REVEAL, "session_id": session_id}


__all__ = [
    "Hub",
    "LOBBY",
    "PROGRESS",
    "QUEUE_DEPTH",
    "REVEAL",
    "ROOMS_CHANGED",
    "Socket",
    "Subscriber",
    "lobby_frame",
    "progress_frame",
    "reveal_frame",
    "rooms_changed",
]
