"""§6.6's "logs": the web process's own recent lines, held in memory and redacted (decision 454).

Spec v2.1 §6.6 (System: "job health, queue depth, last syncs, backup status, logs"), §14.3;
decision 454; docs/milestones/M5.7-plan.md Phase E2 ("the last N lines of the app's own log with a
level filter; anything more is a log viewer nobody asked for").

IN MEMORY, BECAUSE THERE IS NOWHERE ELSE. Plan §5 writes no migration, so there is no table the web
process and the worker could both write, and a log file under `/data` would be a bind mount §2 does
not declare. What is left is what one process holds itself: the last `CAPACITY` records since it
started. So a web restart empties the panel and the worker's lines are never on it -- those stay in
its container log, and its failures stay in the System card's `jobs`, which `job_run` carries
across both processes. The card says so rather than implying a log it does not have (decision
454's cost).

THE `spielplan` LOGGERS AND NO OTHERS. `httpx` writes one INFO line per request with the full URL in
it, for every connector this app talks to, and `uvicorn`'s access log is every request path with
its query string. Both are written by code this app does not own, so a redaction list maintained
here could never be shown complete over them; `push/send.py` and `connectors/probes.py` already
filter the `httpx` line for the two credentials they know it carries, and that is a guard for a
container log, not a licence to put the line on a web page. Hanging the handler off the
`spielplan` logger rather than the root makes the exclusion structural: a record from any other
hierarchy never reaches it at all.

REDACTED HERE AS WELL AS AT THE SOURCE. §14.3 makes a stored credential admin-equivalent, and this
is the one surface that shows an operator text a connector's code wrote -- which is exactly where a
TMDB or OMDb key rides in a query string (decision 453), a provider key in a header, and §7.2's
webhook token in `X-Spielplan-Token`. Every value named that way is masked before the record is
kept, so the ring never holds one to leak: not to the card, not to a later reader of this module.
"""

from __future__ import annotations

import collections
import logging
import re
from datetime import UTC, datetime
from typing import Any

# Decision 454's bound. Enough for the minutes around a failure an operator is reading about, and
# small enough that a sync failing every minute cannot grow the web process's memory.
CAPACITY = 200

# A line is a line: a logged response body is the one record long enough to matter, and the card
# is not where anyone reads one.
MESSAGE_LIMIT = 500

# What the card calls the panel's reach, so it can say which process's lines these are.
SCOPE = "web process"

_MASK = "[redacted]"

# Each value this codebase's connectors are known to write down, found by the name in front of it
# so that the name survives: "the TMDB request answered 401" is what the operator came to read.
_REDACTIONS = (
    # A query parameter whose name ends in key, token or secret -- `api_key` and `apikey` (TMDB and
    # OMDb, decision 453), and the bare `key`, `token` and `secret` any other host might take.
    re.compile(r"([?&][\w.-]*(?:key|token|secret)=)[^&#\s\"'<>]*", re.IGNORECASE),
    # `Authorization: Bearer <value>`, the OpenAI provider's header and any OAuth host's.
    re.compile(r"(\bbearer\s+)[^\s\"',;]+", re.IGNORECASE),
    # A header whose name ends in token or key, as a line or a dict repr spells it:
    # `X-Spielplan-Token` (§7.2), `X-Emby-Token` (§7.1), `x-api-key` and `x-goog-api-key`.
    re.compile(r"(\bx-[\w-]*(?:token|key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+", re.IGNORECASE),
    # The MediaBrowser `Authorization` header's own `Token="..."` (`connectors/jellyfin.py`).
    re.compile(r"(\btoken=\")[^\"]*", re.IGNORECASE),
    # h11 refusing a header value, which it quotes as a bytes repr in either quote and without the
    # header's name: a Jellyfin key saved with a trailing space reached this ring whole that way.
    # `connectors/jellyfin._scrubbed` takes it out at the source; this is the net under it, because
    # any header this app sends can be refused the same way. [M5.7 review cycle 1, M57-KEYS-C1-01]
    re.compile(r"(\billegal header value b)(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", re.IGNORECASE),
)


def redact(text: str) -> str:
    """`text` with every credential value above masked, then cut to `MESSAGE_LIMIT`.

    Masked before the cut, and safe to cut after it: each pattern is anchored on the name, so a
    value the cut would have split has already gone whole."""
    for pattern in _REDACTIONS:
        text = pattern.sub(rf"\1{_MASK}", text)
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT] + "..."


class RecentLines(logging.Handler):
    """A bounded ring of redacted records at INFO and above, newest last."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._ring: collections.deque[dict[str, str]] = collections.deque(maxlen=CAPACITY)
        self.since: datetime | None = None

    def emit(self, record: logging.LogRecord) -> None:
        # Never raised into the caller: a logging call that raised would fail the request that made
        # it. A record whose arguments do not fit its format is dropped from the ring rather than
        # reported through `handleError` -- the root handler formats the same record and reports it
        # there, into the container log where a traceback belongs.
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
        # A copy taken under the handler's own lock, which `Handler.handle` holds around `emit`, so
        # a read never iterates a deque another thread is appending to.
        with self.lock:
            return list(self._ring)


# One ring per process, because the question the card asks is "what has THIS process said".
HANDLER = RecentLines()


def install() -> None:
    """Attach the ring to the `spielplan` logger, once. `app.py` calls this; the worker does not."""
    logger = logging.getLogger("spielplan")
    if HANDLER not in logger.handlers:
        HANDLER.since = datetime.now(UTC)
        logger.addHandler(HANDLER)


def snapshot() -> dict[str, Any]:
    """The card's `logs` key: whose lines these are, since when, and the lines themselves."""
    return {"scope": SCOPE, "since": HANDLER.since, "records": HANDLER.records()}
