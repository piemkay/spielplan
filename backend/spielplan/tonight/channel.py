"""The session channel (§6.2 step 2): household frames to every device, session frames per room.

Blind by construction: every frame is built from `rooms.lobby`, `play.progress` or two integers.
At-most-once and in memory; a dropped frame costs a stale lobby until the next REST read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

log = logging.getLogger("spielplan.tonight.channel")

ROOMS_CHANGED = "rooms.changed"
LOBBY = "lobby"
PROGRESS = "progress"
BALLOT = "ballot"
REVEAL = "reveal"

# Per-device send bound: a stalled phone neither raises nor ends by itself.
SEND_TIMEOUT = 5.0


def wire(value: Any) -> Any:
    """A frame, in the types a WebSocket can actually put on the wire.

    `send_json` is plain `json.dumps` (no `jsonable_encoder`): a `datetime` would kill the socket.
    """
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return [wire(v) for v in value]
    return value


class Socket(Protocol):
    """What the hub needs of a connection; a Protocol so the hub is testable without ASGI."""

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


# Not 1000: a normal closure could read as "do not come back", and the hub wants the device back.
GAVE_UP = 1011


async def close_quietly(socket: Socket) -> None:
    """Tell a device the hub has given up on it, bounded, and never raise.

    The client re-reads only on `onclose`, so an unclosed dropped socket is deaf all evening. The
    close is itself a send, hence the timeout.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(socket.close(code=GAVE_UP), timeout=SEND_TIMEOUT)


# `eq=False`: two devices with equal fields are still two subscribers.
@dataclass(eq=False)
class Subscriber:
    socket: Socket
    user_id: int
    session_id: int | None = None


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
        """One frame to many devices, concurrently, each on its own clock (`SEND_TIMEOUT`)."""
        subscribers = list(targets)
        payload = wire(frame)

        async def deliver(sub: Subscriber) -> bool:
            try:
                await asyncio.wait_for(sub.socket.send_json(payload), timeout=SEND_TIMEOUT)
            except Exception:
                # A gone device must not stop the frame reaching the others.
                log.debug("dropping a session subscriber that stopped answering")
                # Unsubscribed first: the close is the half that can take time.
                self.unsubscribe(sub)
                await close_quietly(sub.socket)
                return False
            return True

        results = await asyncio.gather(*(deliver(s) for s in subscribers))
        return sum(results)

    async def to_household(self, frame: dict[str, Any]) -> int:
        """Every signed-in device: a room's existence is household news (§6.2 step 2)."""
        return await self._deliver(self._subscribers, frame)

    async def to_session(self, session_id: int, frame: dict[str, Any]) -> int:
        """Only the devices watching this room."""
        return await self._deliver(self.watching(session_id), frame)


def rooms_changed(rooms: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The banner frame; an empty one makes the device re-read over REST."""
    return {"kind": ROOMS_CHANGED, "rooms": rooms or []}


def lobby_frame(lobby: dict[str, Any]) -> dict[str, Any]:
    return {"kind": LOBBY, "lobby": lobby}


def progress_frame(session_id: int, progress: list[dict[str, Any]]) -> dict[str, Any]:
    """54c's waiting view, built by `play.progress`, which cannot carry an answer."""
    return {
        "kind": PROGRESS,
        "session_id": session_id,
        "participants": progress,
        "waiting_for": sum(1 for p in progress if not p["finished"]),
    }


def ballot_frame(session_id: int, *, submitted: int, seated: int) -> dict[str, Any]:
    """54e's waiting count, live: two integers and nothing else until the reveal."""
    return {"kind": BALLOT, "session_id": session_id, "submitted": submitted, "seated": seated}


def reveal_frame(session_id: int) -> dict[str, Any]:
    """54e's reveal moment. Carries no result: `ballot.tally` stays the one guarded path."""
    return {"kind": REVEAL, "session_id": session_id}


__all__ = [
    "BALLOT",
    "GAVE_UP",
    "Hub",
    "LOBBY",
    "PROGRESS",
    "SEND_TIMEOUT",
    "REVEAL",
    "ROOMS_CHANGED",
    "Socket",
    "Subscriber",
    "close_quietly",
    "wire",
    "ballot_frame",
    "lobby_frame",
    "progress_frame",
    "reveal_frame",
    "rooms_changed",
]
