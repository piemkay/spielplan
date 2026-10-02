"""TMDB: the metadata backbone, and the only stage-2 source allowed to park it (decision 334).

One `append_to_response` request per title. Writes identity only (decision 372) and never flips
`kind`; a kind disagreement becomes a note. `search` and `detail_for_wish` are the web process's two
reads (decision 558): never captured, and the query is never logged.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from spielplan.acquire.fetch import Fetcher, FetchError
from spielplan.core.config import settings
from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult, json_get

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "tmdb"

# A member is waiting on these, so one attempt.
WEB_TIMEOUT_S = 5.0

# A search row carries genre ids only; TMDB's two lists.
MOVIE_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller",
    10752: "War", 37: "Western",
}
TV_GENRES = {
    10759: "Action & Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 10762: "Kids", 9648: "Mystery", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics", 37: "Western",
}

_INT32_MAX = 2**31 - 1


def api() -> str:
    return settings().tmdb_api_base.rstrip("/")


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
        ctx, source=SOURCE, kind="find", url=f"{api()}/find/{imdb_id}", headers=headers,
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
        url=f"{api()}/{'movie' if is_movie else 'tv'}/{tmdb_id}", headers=headers,
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


def _segment(kind: str) -> str:
    return "movie" if kind == "movie" else "tv"


def _text(value: Any) -> str | None:
    return (str(value).strip() or None) if value else None


def _card(data: dict[str, Any], kind: str) -> dict[str, Any]:
    movie = kind == "movie"
    date = data.get("release_date" if movie else "first_air_date")
    poster = data.get("poster_path")
    return {
        "tmdb_id": int(data["id"]),
        "kind": kind,
        "name": _text(data.get("title" if movie else "name")),
        "original_name": _text(data.get("original_title" if movie else "original_name")),
        "year": int(date[:4]) if isinstance(date, str) and date[:4].isdigit() else None,
        "overview": _text(data.get("overview")),
        "poster_path": poster if isinstance(poster, str) else None,
    }


async def _web_get(fetcher: Fetcher, url: str, auth: tuple[dict[str, str], dict[str, str]],
                   **params: str) -> Any:
    headers, keyed = auth
    response = await asyncio.wait_for(
        fetcher.get(url, headers=headers, params={**keyed, **params}, max_attempts=1,
                    timeout=WEB_TIMEOUT_S),
        WEB_TIMEOUT_S,
    )
    return response.json()


async def search(
    fetcher: Fetcher, auth: tuple[dict[str, str], dict[str, str]], *, kind: str, q: str
) -> list[dict[str, Any]]:
    """`/search/movie` or `/search/tv`, page one in TMDB's order, each row's genres named.
    Raises `FetchError`, `TimeoutError` or `ValueError`."""
    data = await _web_get(fetcher, f"{api()}/search/{_segment(kind)}", auth,
                          query=q, include_adult="false")
    names = MOVIE_GENRES if kind == "movie" else TV_GENRES
    rows = []
    for hit in data.get("results") or []:
        if not isinstance(hit.get("id"), int) or not (row := _card(hit, kind))["name"]:
            continue
        row["genres"] = [names[g] for g in hit.get("genre_ids") or [] if g in names]
        row["popularity"] = float(hit.get("popularity") or 0)
        rows.append(row)
    return rows


async def detail_for_wish(
    fetcher: Fetcher, auth: tuple[dict[str, str], dict[str, str]], *, kind: str, tmdb_id: int
) -> dict[str, Any] | None:
    """`/movie/{id}` or `/tv/{id}` with its external ids, read once for Want it; None when TMDB holds
    no such title of the kind. Raises as `search` does."""
    try:
        data = await _web_get(fetcher, f"{api()}/{_segment(kind)}/{int(tmdb_id)}", auth,
                              append_to_response="external_ids")
    except FetchError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data.get("id"), int):
        raise ValueError(f"TMDB answered for {kind} {tmdb_id} with no id")
    external = data.get("external_ids") or {}
    if kind == "movie":
        runtime = data.get("runtime")
    else:
        episodes = [m for m in data.get("episode_run_time") or [] if isinstance(m, int)]
        runtime = round(sum(episodes) / len(episodes)) if episodes else None
    tvdb = external.get("tvdb_id")
    return {
        **_card(data, kind),
        "runtime_min": runtime if isinstance(runtime, int) and 0 < runtime <= _INT32_MAX else None,
        "imdb_id": _ids.valid_imdb(external.get("imdb_id") or data.get("imdb_id")),
        "tvdb_id": tvdb if kind == "series" and isinstance(tvdb, int) and 0 < tvdb <= _INT32_MAX
        else None,
        "genres": [g["name"] for g in data.get("genres") or [] if isinstance(g, dict) and g.get("name")],
    }
