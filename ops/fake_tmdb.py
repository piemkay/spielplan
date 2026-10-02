"""A fake TMDB API for e2e and tests, never shipped (decision 558). A fixed fixture of titles Spielplan
does not hold, behind `/3/search/{movie,tv}` and `/3/{movie,tv}/{id}`. It refuses any key but its own,
and answers 404 for every other id and path, as TMDB does for one it has no record of.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

API_KEY = os.environ.get("FAKE_TMDB_API_KEY", "e2e-tmdb-key")

MOVIE_GENRES = {18: "Drama", 35: "Comedy", 53: "Thriller", 878: "Science Fiction", 10749: "Romance"}
TV_GENRES = {18: "Drama", 9648: "Mystery"}

# Two films a spec can each want once (desktop and phone), a series that shares "lights" with the first,
# and a film that resolves by its IMDb id to the fixture bundle's Chungking Express under a TMDB id the
# bundle does not carry.
MOVIES: list[dict[str, Any]] = [
    {"id": 910001, "title": "Harbour Lights", "original_title": "Harbour Lights",
     "release_date": "2024-11-20", "runtime": 118, "imdb_id": "tt9100001",
     "overview": "A lighthouse keeper's daughter returns to the town that forgot her.",
     "poster_path": "/harbourlights.jpg", "genre_ids": [18, 10749], "popularity": 41.5},
    {"id": 910002, "title": "Glass Orchard", "original_title": "Glass Orchard",
     "release_date": "2025-03-14", "runtime": 104, "imdb_id": "tt9100002",
     "overview": "Two botanists find a greenhouse that grows what you fear.",
     "poster_path": "/glassorchard.jpg", "genre_ids": [878, 53], "popularity": 23.0},
    {"id": 910003, "title": "Chungking Express", "original_title": "Chungking Express",
     "release_date": "1994-07-14", "runtime": 102, "imdb_id": "tt0109424",
     "overview": "Two lovesick policemen in Hong Kong.",
     "poster_path": "/chungkingexpress.jpg", "genre_ids": [18, 35, 10749], "popularity": 12.0},
]
SERIES: list[dict[str, Any]] = [
    {"id": 920001, "name": "Northern Lights", "original_name": "Northern Lights",
     "first_air_date": "2025-09-05", "episode_run_time": [52], "imdb_id": "tt9200001",
     "tvdb_id": 9200001, "overview": "A coastal town keeps its lights on and its secrets in.",
     "poster_path": "/northernlights.jpg", "genre_ids": [9648, 18], "popularity": 30.0},
]

router = APIRouter(prefix="/3")


def _require_key(api_key: str) -> None:
    if api_key != API_KEY:
        raise HTTPException(401, {"status_code": 7, "success": False,
                                  "status_message": "Invalid API key: You must be granted a valid key."})


def _missing() -> HTTPException:
    return HTTPException(404, {"status_code": 34, "success": False,
                               "status_message": "The resource you requested could not be found."})


def _hit(entry: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    keep = (*names, "id", "overview", "poster_path", "genre_ids", "popularity")
    return {key: entry[key] for key in keep} | {"adult": False}


def _search(entries: list[dict[str, Any]], names: tuple[str, ...], query: str) -> dict[str, Any]:
    needle = query.strip().casefold()
    hits = [_hit(e, names) for e in entries
            if needle and any(needle in e[name].casefold() for name in names[:2])]
    return {"page": 1, "results": hits, "total_pages": 1, "total_results": len(hits)}


def _detail(entry: dict[str, Any], genres: dict[int, str], append: str) -> dict[str, Any]:
    body = {key: value for key, value in entry.items() if key not in ("genre_ids", "tvdb_id")}
    body["genres"] = [{"id": g, "name": genres[g]} for g in entry["genre_ids"]]
    if "external_ids" in append.split(","):
        body["external_ids"] = {"imdb_id": entry["imdb_id"], "tvdb_id": entry.get("tvdb_id")}
    return body


@router.get("/search/movie")
async def search_movie(api_key: str = "", query: str = "") -> dict[str, Any]:
    _require_key(api_key)
    return _search(MOVIES, ("title", "original_title", "release_date"), query)


@router.get("/search/tv")
async def search_tv(api_key: str = "", query: str = "") -> dict[str, Any]:
    _require_key(api_key)
    return _search(SERIES, ("name", "original_name", "first_air_date"), query)


@router.get("/movie/{tmdb_id}")
async def movie(tmdb_id: int, api_key: str = "",
                append_to_response: str = Query(default="")) -> dict[str, Any]:
    _require_key(api_key)
    entry = next((e for e in MOVIES if e["id"] == tmdb_id), None)
    if entry is None:
        raise _missing()
    return _detail(entry, MOVIE_GENRES, append_to_response)


@router.get("/tv/{tmdb_id}")
async def tv(tmdb_id: int, api_key: str = "",
             append_to_response: str = Query(default="")) -> dict[str, Any]:
    _require_key(api_key)
    entry = next((e for e in SERIES if e["id"] == tmdb_id), None)
    if entry is None:
        raise _missing()
    return _detail(entry, TV_GENRES, append_to_response)


def create_app() -> FastAPI:
    app = FastAPI(title="Fake TMDB", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return "User-agent: *\nAllow: /\n"

    return app


app = create_app()
