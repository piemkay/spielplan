"""TVmaze - free, no key, series only. What TMDB is weakest on for television.

Spec v2.1 §8 stage 2 ("tvmaze:show (series)", `spec:367`), §4.1 rule 5; decisions 334, 340, 372.

Network and webChannel, precise episode counts and schedule, and a clean cast/crew embed
(`mdc/sources/tvmaze.py:1-5`). Two requests: the IMDb id is exchanged for TVmaze's own show id,
and the show is then fetched with its three embeds in one call.

PORT VERDICT: **ported with named changes** from `mdc/sources/tvmaze.py` (47 lines). Taken
verbatim: `API` (`:16`), the `/lookup/shows?imdb=` exchange (`:30`), the `/shows/{id}` request and
its `embed[]` list - `cast`, `crew`, `seasons` (`:40-43`) - and the series-only refusal (`:23-24`).

NAMED CHANGE 1: THE LOOKUP RESPONSE IS STORED. `:30-38` fetches the lookup and stores only the
show. §8's preamble is about all fetched bytes, and without the lookup document a derive cannot
tell "TVmaze does not hold this series" from "nobody has run stage 2 on it yet".

NAMED CHANGE 2: `raise Permanent("not on tvmaze")` (`:33`, `:38`) IS A NOTE. TVmaze is not the
required source (decision 334), and a series it does not carry is an ordinary answer.

THE SERIES-ONLY REFUSAL IS PORTED AS A `SourceResult` AND NOT AS A SKIP, because the driver has
to be able to tell "this kind did not apply" from "this kind failed" - a movie is not a TVmaze
failure. It reads `title.kind`, which §4.1 rule 5 makes the partition every ranking surface
already trusts, and it never writes it: decision 372 and `_ids.ID_COLUMNS` refuse `kind` outright
and `mdc/sources/tmdb.py:81-84`'s flip is the reason that refusal is spelled out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "tvmaze"
API = "https://api.tvmaze.com"

# `mdc/sources/tvmaze.py:42`, verbatim. Three embeds in one request rather than three requests:
# the whole reason this source costs two calls and not five.
EMBEDS = ["cast", "crew", "seasons"]


@handler("tvmaze:show", source=SOURCE, priority=55, phase="enrich",
         description="TV series details, cast and crew")
async def show(ctx: StageContext) -> SourceResult:
    kind = "tvmaze:show"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    if row["kind"] != "series":
        # `mdc/sources/tvmaze.py:24`: "movies only exist on tvmaze as specials". Reported ok,
        # because the kind did the right thing and spent nothing - a movie skipped here is not a
        # gap in this title's enrichment.
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
