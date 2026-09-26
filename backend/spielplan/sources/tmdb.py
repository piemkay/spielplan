"""TMDB: the metadata backbone, and the only stage-2 source allowed to park it (decision 334).

One `append_to_response` request per title. Writes identity only (decision 372) and never flips
`kind`; a kind disagreement becomes a note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult, handler, json_get

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "tmdb"
API = "https://api.themoviedb.org/3"

# One call carries what would otherwise be ten.
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

# The parser picks the payload shape by these kinds, not by `title.kind`.
MOVIE_DETAIL = "movie_detail"
TV_DETAIL = "tv_detail"


def _no_credential(kind: str) -> SourceResult:
    """Decision 377: an absent key is a note, not a park, and costs no request (`ran=False`)."""
    return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False,
                        note="no TMDB credential configured")


async def _find(ctx: StageContext, row: Any, auth: tuple[dict[str, str], dict[str, str]],
                imdb_id: str) -> tuple[int | None, str, _views.Captured]:
    """`/find/{imdb}`: the IMDb id exchanged for TMDB's own. Returns `(tmdb_id, note, captured)`.

    A None id is often an answer, not a failure; callers read `captured.ok` for that.
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
            # Record the kind disagreement and write nothing (§4.1 rule 5, decision 162).
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
        # Already resolved: an answer, not a skip.
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
    # `captured.ok`, so a failed request is visible to decision 334's park.
    return SourceResult(source=SOURCE, kind=kind, ok=captured.ok,
                        doc_id=captured.doc_id, note=note)


@handler("tmdb:detail", source=SOURCE, requires=credentials.TMDB, priority=20,
         phase="enrich", description="Full title record + credits + keywords")
async def detail(ctx: StageContext) -> SourceResult:
    """The one document §8 stage 3 cannot do without, which is why decision 334 requires it.

    The three early arms spend no request, so they report `ran=False` and advance rather than park.
    """
    kind = "tmdb:detail"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, ran=False, note="no title row")
    tmdb_id = row["tmdb_id"]
    if not tmdb_id:
        # The column is empty for several reasons this function cannot tell apart.
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
    # The only write; `set_ids` is COALESCE, so offering `imdb_id` never overwrites.
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
