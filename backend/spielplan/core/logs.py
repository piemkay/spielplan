"""§6.6's System-card logs: this web process's recent `spielplan` records, in memory and redacted.
Other loggers (httpx, uvicorn access) never reach the ring: they carry URLs this module cannot vouch for.
"""

from __future__ import annotations

import collections
import logging
import re
from datetime import UTC, datetime
from typing import Any

CAPACITY = 200

MESSAGE_LIMIT = 500

SCOPE = "web process"

_MASK = "[redacted]"

# Each pattern is anchored on the name in front of the value, so the name survives the mask.
_REDACTIONS = (
    # `api_key`/`apikey` (TMDB, OMDb) and any query parameter ending in key, token or secret.
    re.compile(r"([?&][\w.-]*(?:key|token|secret)=)[^&#\s\"'<>]*", re.IGNORECASE),
    # `Authorization: Bearer <value>`.
    re.compile(r"(\bbearer\s+)[^\s\"',;]+", re.IGNORECASE),
    # `X-Spielplan-Token` (§7.2), `X-Emby-Token`, `x-api-key`, `x-goog-api-key`, as a line or a dict repr.
    re.compile(r"(\bx-[\w-]*(?:token|key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+", re.IGNORECASE),
    # The MediaBrowser `Authorization` header's `Token="..."` (`connectors/jellyfin.py`).
    re.compile(r"(\btoken=\")[^\"]*", re.IGNORECASE),
    # h11's "illegal header value b'...'", which quotes the value without the header's name.
    re.compile(r"(\billegal header value b)(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", re.IGNORECASE),
)


def redact(text: str) -> str:
    """Masked before the cut, so the cut can never split a value the patterns would have caught."""
    for pattern in _REDACTIONS:
        text = pattern.sub(rf"\1{_MASK}", text)
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT] + "..."


class RecentLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._ring: collections.deque[dict[str, str]] = collections.deque(maxlen=CAPACITY)
        self.since: datetime | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # Never raises into the caller. Records that do not format are dropped here; the root handler
        # reports them to the container log.
        try:
            entry = {
                "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": redact(record.getMessage()),
            }
        except Exception:  # noqa: BLE001 - see above
            return
        self._ring.append(entry)

    def records(self) -> list[dict[str, str]]:
        # Under `Handler.handle`'s lock, so a read never iterates a deque mid-append.
        with self.lock:
            return list(self._ring)


HANDLER = RecentLines()


def configure() -> None:
    """Root logging for both processes. httpx logs every request URL at INFO, query keys included."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def install() -> None:
    logger = logging.getLogger("spielplan")
    if HANDLER not in logger.handlers:
        HANDLER.since = datetime.now(UTC)
        logger.addHandler(HANDLER)


def snapshot() -> dict[str, Any]:
    return {"scope": SCOPE, "since": HANDLER.since, "records": HANDLER.records()}
