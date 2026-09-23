"""OMDb - the above-board route to Rotten Tomatoes' and Metacritic's scores.

Spec v2.1 §8 stage 2 (`spec:367`); decisions 334, 340, 372, 377.

The critic percentages, the IMDb rating and vote count, the awards line, box office and a full
plot, in one request keyed on the IMDb id, without touching either site's HTML
(`mdc/sources/omdb.py:1-6`). It is the cheapest source in §8 stage 2 and the one that makes the
two scraped ones optional rather than load-bearing.

PORT VERDICT: **ported with named changes** from `mdc/sources/omdb.py` (59 lines). Taken verbatim:
`API` (`:19`) and the five query parameters (`:30-36`) - `apikey`, `i`, `plot=full`,
`tomatoes=true`, `r=json`. `plot=full` is the one worth naming: §8 stage 4's gate requires a plot
(decision 335), and the short plot is a sentence where the full one is a paragraph.

NAMED CHANGE 1: THE QUOTA PAUSE IS A NOTE HERE, NOT A `HostPaused`. The corpus raises
`HostPaused("www.omdbapi.com", time.time() + 3600)` when the answer is a limit or an invalid-key
error (`:43-48`), and its argument is exactly right about the cost: the free tier is 1,000 calls
a day, "which a 10k corpus will hit every day for over a week". But `acquire/fetch.HostPaused` is
the CIRCUIT BREAKER's state, owned by the `Fetcher` and rebuilt per drain from `fetch_host_state`
- an adapter fabricating one would raise something that looks like a breaker trip and pauses
nothing. Under decision 334 the consequence the corpus feared does not follow either: no title is
marked failed, stage 2 advances, and the drain's cost is one wasted request per title rather than
a run of retries. So the quota answer is a note under this source's name, distinguished from
"this film is not in OMDb" because they call for different actions - one is a key to top up, the
other is a title OMDb has never held. THE MILESTONE THAT WANTS THE PAUSE adds a way for a stage
to hand the fetcher a host-level deadline; it is not something this module can reach from here,
and inventing a second pausing mechanism beside decision 340's would be worse than the note.

NAMED CHANGE 2: THE REFUSAL INSIDE A 200 IS A `verdict` AND THE BYTES ARE STORED EITHER WAY. OMDb
answers `{"Response": "False", "Error": "..."}` with HTTP 200, and the corpus parses before it
stores so it can choose the flag (`:38-52`). Here the store happens first - that is §8's
preamble, and `_views.capture`'s `verdict` hook is how the flag is still chosen correctly. The
row lands `ok = false` with the provider's own `Error` string, so an operator sees what OMDb
said and `rawstore.latest` never hands it to a parser.

NOTHING IS WRITTEN TO `title`. The corpus writes nothing here either, and there is nothing this
source knows that is identity: the IMDb id is what the request was keyed on. Every rating, the
plot and the awards text are §8 stage 3's, parsed from this document out of the raw store.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from spielplan.acquire import fetch
from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "omdb"
API = "https://www.omdbapi.com/"

# The two substrings `mdc/sources/omdb.py:47` tests for. They are what OMDb says when the key is
# spent or wrong, as opposed to when the film is simply not there, and the difference is the
# difference between an admin gesture and a fact about this title.
_QUOTA = ("limit", "invalid api key")


def _refusal(response: fetch.Response) -> str:
    """OMDb's own `Error` string when it answered 200 and said no, else `""`.

    Defensive about the decode because this runs before anything has established that the body
    is JSON at all: a proxy's interception page arrives as 200 with HTML, and a `JSONDecodeError`
    escaping here would turn a source decision 334 makes optional into a raised stage.
    """
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "OMDb answered with a body that is not JSON"
    if not isinstance(data, dict) or data.get("Response") != "False":
        return ""
    error = str(data.get("Error") or "unknown")
    if any(term in error.lower() for term in _QUOTA):
        # Named separately so a board full of notes reads as one repair rather than as a
        # thousand titles OMDb has never heard of.
        return f"OMDb refused the key rather than the title: {error}"
    return f"OMDb: {error}"


@handler("omdb:detail", source=SOURCE, requires=credentials.OMDB, priority=30,
         phase="enrich", description="RT + Metacritic + IMDb scores, awards, plot")
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
        # Decision 377: a note, no request, and stage 2 advances (decision 334).
        return SourceResult(source=SOURCE, kind=kind, ok=False,
                            note="no OMDb credential configured")

    captured = await _views.capture(
        ctx, source=SOURCE, kind="detail", url=API,
        params={"apikey": key, "i": imdb_id, "plot": "full", "tomatoes": "true", "r": "json"},
        # `imdb_id` and not the params: this is one url for every title in the library
        # (`rawstore.store`'s co-keying paragraph names this module as the example), so the row
        # needs something that says which title it is - and the params are where the key is.
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
