"""TMDB - the metadata backbone, and the one source of §8 stage 2 that is allowed to park it.

Spec v2.1 §8 stage 2 (`spec:365-368`), §4.1 rules 5 and 6; decisions 334, 340, 372, 373, 377.

One `append_to_response` call per title fetches details, credits, keywords, external ids,
certifications, videos, alternative titles and the first page of TMDB's own reviews together,
which is what keeps a title to roughly one request (`mdc/sources/tmdb.py:1-7`). That list is
ported character for character below: every entry taken out of it is another round trip on a
household's own IP address, and every entry added is bytes nobody parses.

PORT VERDICT: **ported with named changes** from `mdc/sources/tmdb.py` (217 lines). Taken: `API`
(`:19`), `MOVIE_APPEND` and `TV_APPEND` (`:21-31`) verbatim, the `/find/{imdb}?external_source=
imdb_id` resolve shape (`:70-72`), the detail path and its two parameters (`:129-130`), and the
external-ids read that yields `imdb_id`, `tvdb_id` and `wikidata_id` (`:145-152`).

FOUR THINGS ARE NOT PORTED, and three of them are decisions rather than tidying.

  * `UPDATE title SET primary_title, original_title, year, runtime_min, episode_count,
    season_count, original_language, tmdb_popularity` (`:154-170`). Decision 372: an adapter
    writes the raw bytes and the identity columns and NOTHING else. Every one of those is a field
    a card renders, and §8 stage 3 writes them from this same document out of the raw store -
    which is what makes a bad parse cost a re-parse instead of a re-crawl (`spec:398`).
  * THE KIND FLIP (`:79-84`). The corpus, finding the title under TMDB's other bucket, believes
    TMDB and flips `title.kind`. Here `kind` is §4.1 rule 5's ranking partition - "every ranking
    surface partitions by it (measured: the unpartitioned crowd top-10 is 8/10 TV series)" - and
    decision 162 makes the write permanent, so a provider's disagreement about a boxed set is not
    licence to move a placed title between two ranking universes. The disagreement is recorded as
    a note under this source's name instead, which is the form decision 334 gives every stage-2
    finding, and an operator who agrees with TMDB can act on it.
  * THE `tmdb:reviews` SELF-ENQUEUE (`:170-174`) and the `tmdb:reviews` handler (`:177-199`).
    §8's unit of work is one title walking ten stages (`acquire/pipeline.py:46-60`), not a task
    per source, and a handler that enqueues a second task for its own title puts the sequence
    into the queue's ORDER BY - where two tasks of one title can be leased by two workers at
    once. The first page of reviews arrives inside `append_to_response` either way.
  * INLINE RESOLUTION INSIDE `tmdb:detail` (`:113-121`). The corpus resolves inline because "the
    workers have no ordering guarantee, and a Skip here would be permanent". This app has that
    guarantee: `base.available_kinds` returns kinds sorted by `default_priority` and
    `tmdb:resolve` is 10 against `tmdb:detail`'s 20. So a missing `tmdb_id` here is a fact about
    this title rather than a race, and it is reported as one instead of spending a request to
    re-discover what the kind ten points below it already found out.

DECISION 334 MAKES THIS THE ONLY REQUIRED SOURCE, so the notes below are what an operator reads
off a parked job. `tmdb:detail` failing parks stage 2 with a reason naming it; the driver decides
that, not this module - an adapter returns a `SourceResult` and never raises for an ordinary
outcome (`sources/base.py:31-45`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult, handler, json_get

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "tmdb"
API = "https://api.themoviedb.org/3"

# `mdc/sources/tmdb.py:21-31`, verbatim. The whole argument for this module's request count is in
# these two lists: one call carries what would otherwise be ten.
MOVIE_APPEND = ",".join([
    "credits", "keywords", "external_ids", "release_dates", "reviews",
    "videos", "alternative_titles", "recommendations", "similar",
    "watch/providers",
])

TV_APPEND = ",".join([
    "aggregate_credits", "content_ratings", "external_ids", "keywords",
    "reviews", "videos", "alternative_titles", "recommendations", "similar",
    "watch/providers",
])

# The two `raw_document.kind` values §8 stage 3 reads a title's TMDB payload back under, kept
# distinct because a movie payload and a tv payload have different shapes and the parser picks by
# this string rather than by re-reading `title.kind`.
MOVIE_DETAIL = "movie_detail"
TV_DETAIL = "tv_detail"


def _no_credential(kind: str) -> SourceResult:
    """Decision 377: an absent key is a note and never a park, and it costs no request.

    `available_kinds` already filters a kind whose capability is off, so reaching this means the
    capability map and `connector_config` disagree - an install that configured TMDB and then
    cleared it mid-drain, or a DEK that stopped opening between the two reads. §3.1 calls a
    half-configured boot legal; this is the same state one layer in.

    `ran=False` says the second half of decision 377's own sentence: an absent key costs no
    request, so the required source did not run and the driver's park does not fire for it.
    """
    return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False,
                        note="no TMDB credential configured")


async def _find(ctx: StageContext, row: Any, auth: tuple[dict[str, str], dict[str, str]],
                imdb_id: str) -> tuple[int | None, str, _views.Captured]:
    """`/find/{imdb}` - the IMDb id this app already holds, exchanged for TMDB's own.

    Returns `(tmdb_id, note, captured)`. `tmdb_id` is None when TMDB has never heard of the
    title, which is an answer and not a failure: 21% of this schema's titles carry no IMDb id at
    all (`0003_content.sql:20-22`) and the ones that do are not all in TMDB.

    WHICH IS WHY THE CALLER READS `captured.ok` AND NOT THIS ID. Four of the five arms below
    return None and only one of them is a failure: TMDB holding no record, TMDB filing the id
    under the other `kind`, TMDB answering with a hit that carries no id, and a 304 saying the
    document this app already holds is still current are all things TMDB SAID. Collapsing them
    onto `ok = tmdb_id is not None` put a refused key and a 500 into the same value as all four,
    and decision 334's park - the required source ran and did not answer - could then never fire
    for this kind. [M5.3 review cycle 2, m53-c2-334-01]
    """
    headers, params = auth
    captured = await _views.capture(
        ctx, source=SOURCE, kind="find", url=f"{API}/find/{imdb_id}", headers=headers,
        params={**params, "external_source": "imdb_id"},
        request_meta={"imdb_id": imdb_id},
    )
    if not captured.ok:
        return None, captured.error, captured
    if captured.unchanged:
        return None, "unchanged since the last fetch", captured

    data = captured.response.json()
    ours = "movie_results" if row["kind"] == "movie" else "tv_results"
    theirs = "tv_results" if row["kind"] == "movie" else "movie_results"
    hits = data.get(ours) or []
    if not hits:
        if data.get(theirs):
            # The corpus flips `title.kind` here. See the port verdict: this records the
            # disagreement and writes nothing, because §4.1 rule 5 makes `kind` the partition
            # every ranking surface reads and decision 162 makes the write permanent.
            return None, (
                f"TMDB files {imdb_id} as a {'series' if row['kind'] == 'movie' else 'movie'} "
                f"and this title is a {row['kind']}; kind is not a stage 2 write"
            ), captured
        return None, f"TMDB has no record for {imdb_id}", captured

    tmdb_id = hits[0].get("id")
    if not tmdb_id:
        return None, f"TMDB answered for {imdb_id} with no id", captured
    await _ids.set_ids(ctx.conn, row["id"], tmdb_id=tmdb_id)
    return int(tmdb_id), f"tmdb_id={tmdb_id}", captured


@handler("tmdb:resolve", source=SOURCE, requires=credentials.TMDB, priority=10,
         phase="enrich", description="Resolve IMDb id -> TMDB id")
async def resolve(ctx: StageContext) -> SourceResult:
    """§8 stage 2's first kind, and the cheapest: one request that makes the next one possible."""
    kind = "tmdb:resolve"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False, note="no title row")
    if row["tmdb_id"]:
        # Already resolved, by stage 1 off Jellyfin's ProviderIds or by an earlier drain. An
        # answer rather than a skip: the kind did what it exists to do and spent nothing.
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note=f"already resolved: tmdb_id={row['tmdb_id']}")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False,
                            note="no imdb id to resolve from")
    auth = await credentials.tmdb_auth(ctx.conn)
    if auth is None:
        return _no_credential(kind)

    tmdb_id, note, captured = await _find(ctx, row, auth, imdb_id)
    # `captured.ok` AND NOT `tmdb_id is not None`, which is `_find`'s paragraph applied: this kind
    # ran, TMDB answered, and whether the answer carried an id is a fact about the film rather
    # than about the source. The note still says which it was, and `tmdb:detail` still reports
    # `ran=False` on the empty column, so a film TMDB has no record of walks on exactly as before
    # - what changes is that a request that FAILED is now visible to decision 334's park.
    # [M5.3 review cycle 2, m53-c2-334-01]
    return SourceResult(source=SOURCE, kind=kind, ok=captured.ok,
                        doc_id=captured.doc_id, note=note)


@handler("tmdb:detail", source=SOURCE, requires=credentials.TMDB, priority=20,
         phase="enrich", description="Full title record + credits + keywords")
async def detail(ctx: StageContext) -> SourceResult:
    """The one document §8 stage 3 cannot do without, which is why decision 334 requires it.

    THE THREE ARMS BELOW ANSWER WITHOUT ASKING, AND SAY SO. Decision 334 parks stage 2 for a
    required source that RAN and did not answer; none of these three spends a request, so none of
    them is that. The one that is reachable in the ordinary course is the middle one: stage 1
    mints on any of imdb/tmdb/tvdb (decision 323), `_find` returns no id both for a film TMDB has
    never heard of and for the kind disagreement this module refuses to act on, and no other
    adapter writes `tmdb_id` - so an IMDb-only file TMDB does not hold arrives here with an empty
    column on every drain, for ever. Parked, that title re-walked all eight sources once a day
    including two Rotten Tomatoes and four Metacritic fetches - the two hosts `acquire/hosts.py`
    paces at 0.7 rps with a quarter-hour breaker - under a reason naming a connector that was
    working and a retry that re-parked identically. Reported as not-asked it advances instead and
    parks at §8 stage 4 with "no plot yet, 0 sources, 0 words" and a thirty-day window, which is
    the sentence `stages.enrich` already promises for the structurally identical case of an
    install that has typed in no TMDB key at all, and whose window asks the hosts again once in
    thirty days (decision 421) where the stage-2 park asked them daily. [M5.3 review cycle 1,
    M53-334-01; review cycle 2, m53-c2-gate-01]
    """
    kind = "tmdb:detail"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False, note="no title row")
    tmdb_id = row["tmdb_id"]
    if not tmdb_id:
        # THE NOTE SAYS WHAT IS TRUE HERE AND NOT WHY, which it used to guess at: it read
        # "tmdb:resolve found none", and the column is equally empty when that kind was refused a
        # key, got a 500, or was never asked because the title carries no IMDb id. This function
        # cannot see which, `tmdb:resolve` writes its own note beside this one, and `stages.enrich`
        # is where the two are read together. [M5.3 review cycle 2, m53-c2-334-01]
        return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False,
                            note="no tmdb id on the title, so TMDB could not be asked")
    auth = await credentials.tmdb_auth(ctx.conn)
    if auth is None:
        return _no_credential(kind)

    headers, params = auth
    is_movie = row["kind"] == "movie"
    captured = await _views.capture(
        ctx, source=SOURCE, kind=MOVIE_DETAIL if is_movie else TV_DETAIL,
        url=f"{API}/{'movie' if is_movie else 'tv'}/{tmdb_id}", headers=headers,
        params={**params, "append_to_response": MOVIE_APPEND if is_movie else TV_APPEND,
                "language": "en-US"},
        request_meta={"tmdb_id": tmdb_id},
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=captured.error)
    if captured.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="unchanged since the last fetch")

    data = captured.response.json()
    external = data.get("external_ids") or {}
    # THE ONLY WRITE, and every name in it is in `_ids.ID_COLUMNS`. `imdb_id` is offered even
    # when the row already carries one because `set_ids` is COALESCE and will not overwrite it;
    # the point of offering it is the title that arrived through stage 1 on a TMDB id alone.
    filled = await _ids.set_ids(
        ctx.conn, row["id"],
        imdb_id=external.get("imdb_id"),
        tvdb_id=external.get("tvdb_id"),
        wikidata_id=external.get("wikidata_id"),
    )
    reviews = json_get(data, "reviews", "total_results", default=0)
    return SourceResult(
        source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
        note=f"tmdb {tmdb_id}: {reviews} reviews"
             + (f", filled {', '.join(filled)}" if filled else ""),
    )
