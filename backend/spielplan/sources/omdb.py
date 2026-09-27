"""OMDb: critic percentages, IMDb rating, awards and a full plot in one request keyed on IMDb id.

Quota exhaustion is a note, not a `HostPaused` (that is the breaker's). Nothing is written to `title`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from spielplan.acquire import fetch
from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "omdb"
API = "https://www.omdbapi.com/"

# What OMDb says when the key is spent or wrong, as opposed to the film being absent.
_QUOTA = ("limit", "invalid api key")


def _refusal(response: fetch.Response) -> str:
    """OMDb's own `Error` string when it answered 200 and said no, else `""`. Tolerates non-JSON bodies.
    """
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "OMDb answered with a body that is not JSON"
    if not isinstance(data, dict) or data.get("Response") != "False":
        return ""
    error = str(data.get("Error") or "unknown")
    if any(term in error.lower() for term in _QUOTA):
        # So a board of these reads as one key repair.
        return f"OMDb refused the key rather than the title: {error}"
    return f"OMDb: {error}"


async def detail(ctx: StageContext) -> SourceResult:
    kind = "omdb:detail"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no imdb id")
    key = await credentials.omdb_key(ctx.conn)
    if not key:
        # Decision 377: a note, no request, and stage 2 advances.
        return SourceResult(source=SOURCE, kind=kind, ok=False,
                            note="no OMDb credential configured")

    captured = await _views.capture(
        ctx, source=SOURCE, kind="detail", url=API,
        params={"apikey": key, "i": imdb_id, "plot": "full", "tomatoes": "true", "r": "json"},
        # One url for every title, so the row needs the IMDb id; the params hold the key.
        request_meta={"imdb_id": imdb_id}, verdict=_refusal,
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=captured.error)
    if captured.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="unchanged since the last fetch")

    ratings = {r.get("Source"): r.get("Value") for r in (captured.response.json().get("Ratings")
                                                         or [])}
    return SourceResult(
        source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
        note="; ".join(f"{k}={v}" for k, v in ratings.items()) or "no scores",
    )
