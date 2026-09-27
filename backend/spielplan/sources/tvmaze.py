"""TVmaze: free, keyless, series only; network, episode counts and cast/crew for television.

The lookup response is stored too, so a derive can tell "not on TVmaze" from "not run yet".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "tvmaze"
API = "https://api.tvmaze.com"

# Three embeds in one request.
EMBEDS = ["cast", "crew", "seasons"]


async def show(ctx: StageContext) -> SourceResult:
    kind = "tvmaze:show"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    if row["kind"] != "series":
        # Movies only exist on TVmaze as specials; reported ok, nothing spent.
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="not applicable: tvmaze carries series")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no imdb id")

    lookup = await _views.capture(
        ctx, source=SOURCE, kind="lookup", url=f"{API}/lookup/shows",
        params={"imdb": imdb_id}, request_meta={"imdb_id": imdb_id},
    )
    if not lookup.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=lookup.doc_id,
                            note=f"not on tvmaze: {lookup.error}")
    if lookup.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="unchanged since the last fetch")
    show_id = (lookup.response.json() or {}).get("id")
    if not show_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=lookup.doc_id,
                            note="not on tvmaze")

    captured = await _views.capture(
        ctx, source=SOURCE, kind="show", url=f"{API}/shows/{show_id}",
        params={"embed[]": EMBEDS}, request_meta={"tvmaze_id": show_id},
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=captured.error)
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                        note=f"tvmaze {show_id}")
