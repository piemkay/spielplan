"""Trakt: the rating distribution, and comments carrying the commenter's own rating.

Comments are fetched sorted by rating from both ends, the one sanctioned way to get a spread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "trakt"
API = "https://api.trakt.tv"

# (sort, pages of 25).
COMMENT_SORTS = (("likes", 2), ("lowest", 2), ("highest", 1))

# Spaces two sorts' page numbers apart; no sort gets more than ten pages.
_SORT_STRIDE = 10


def _base(kind: str) -> str:
    return "movies" if kind == "movie" else "shows"


def _no_credential(kind: str) -> SourceResult:
    return SourceResult(source=SOURCE, kind=kind, ok=False,
                        note="no Trakt client id configured")


async def summary(ctx: StageContext) -> SourceResult:
    """Two requests: the title and its rating distribution."""
    kind = "trakt:summary"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no imdb id")
    headers = await credentials.trakt_headers(ctx.conn)
    if headers is None:
        return _no_credential(kind)

    base = _base(row["kind"])
    captured = await _views.capture(
        ctx, source=SOURCE, kind="summary", url=f"{API}/{base}/{imdb_id}", headers=headers,
        params={"extended": "full"}, request_meta={"imdb_id": imdb_id},
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=captured.error)
    if captured.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note="unchanged since the last fetch")

    ids = captured.response.json().get("ids") or {}
    # Arrive together; `set_ids` is COALESCE (decision 372).
    filled = await _ids.set_ids(ctx.conn, row["id"], trakt_id=ids.get("trakt"),
                               trakt_slug=ids.get("slug"))

    ratings = await _views.capture(
        ctx, source=SOURCE, kind="ratings", url=f"{API}/{base}/{imdb_id}/ratings",
        headers=headers, request_meta={"imdb_id": imdb_id}, name="ratings",
    )
    notes = [] if ratings.ok else [f"ratings: {ratings.error}"]
    return SourceResult(
        source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
        note="; ".join([f"offered {', '.join(filled)}" if filled else "no new ids", *notes]),
    )


async def comments(ctx: StageContext) -> SourceResult:
    """The rating-labelled review text, taken from three ends of the distribution.

    Stops early on a short page, and entirely on a 404.
    """
    kind = "trakt:comments"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    imdb_id = _ids.valid_imdb(row["imdb_id"])
    if not imdb_id:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no imdb id")
    headers = await credentials.trakt_headers(ctx.conn)
    if headers is None:
        return _no_credential(kind)

    base = _base(row["kind"])
    total = 0
    last_doc: int | None = None
    refused = ""
    for index, (sort, max_pages) in enumerate(COMMENT_SORTS):
        for page in range(1, max_pages + 1):
            captured = await _views.capture(
                ctx, source=SOURCE, kind="comments",
                url=f"{API}/{base}/{imdb_id}/comments/{sort}", headers=headers,
                params={"page": page, "limit": 25, "extended": "full"},
                # Unique per (sort, page), or the store's newest-per-page view collapses two sorts.
                page=index * _SORT_STRIDE + page,
                request_meta={"sort": sort, "imdb_id": imdb_id}, name=f"{sort} p{page}",
                # A comment page is a JSON array, so not `json_object`.
                verdict=_views.json_body,
            )
            if not captured.ok:
                refused = f"{sort}: {captured.error}"
                break
            if captured.unchanged:
                continue
            items = captured.response.json()
            if not items:
                break
            last_doc = captured.doc_id or last_doc
            total += len(items)
            if len(items) < 25:
                break
        if refused:
            break

    if refused and total == 0:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=last_doc, note=refused)
    note = f"{total} comments across {len(COMMENT_SORTS)} sorts"
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=last_doc,
                        note=f"{note}; {refused}" if refused else note)
