"""§8 stage 2's adapter contract: what a source adapter answers with.

Adapters return a `SourceResult` instead of raising: a source not answering is ordinary (decision
334), and the stage-2 driver maps the set of them onto one `stages.Outcome`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: a runtime import would close a cycle with `acquire/stages.py`.
    from spielplan.acquire.stages import StageContext


@dataclass(frozen=True)
class SourceResult:
    """What ONE source did. `note` is shown verbatim on the admin board.

    `ran=False` claims no request left the box, so a required source that never ran is not parked
    (decision 334). Default True, the conservative answer.
    """

    source: str
    kind: str
    ok: bool
    doc_id: int | None = None
    note: str = ""
    ran: bool = True


Handler = Callable[["StageContext"], Awaitable[SourceResult]]


def json_get(obj: Any, *path: Any, default: Any = None) -> Any:
    """Walk `path` into a decoded JSON document, answering `default` wherever it does not go."""
    cur = obj
    for key in path:
        if cur is None:
            return default
        try:
            cur = cur[key] if isinstance(key, int) else cur.get(key)
        except (KeyError, IndexError, TypeError, AttributeError):
            return default
    return cur if cur is not None else default


__all__ = ["Handler", "SourceResult", "json_get"]
