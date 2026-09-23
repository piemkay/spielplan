"""Trakt - the rating distribution, and the only review text that arrives with a rating on it.

Spec v2.1 §8 stage 2 ("trakt:summary->comments", `spec:367`), §8 stage 6; decisions 334, 340,
372, 377.

Two things Trakt gives that nothing else gives cheaply: a rating DISTRIBUTION - how the 1-10
votes are spread, which is a diversity signal in its own right - and user comments that carry the
commenter's own rating, which is exactly the rating-labelled review text §8 stage 6's aspect
extraction needs (`mdc/sources/trakt.py:1-7`).

PORT VERDICT: **ported with named changes** from `mdc/sources/trakt.py` (133 lines). Taken
verbatim: `API` (`:19`), the three headers (`:25-29`, and they live in `credentials.trakt_headers`
under decision 377), the `/{movies|shows}/{imdb}?extended=full` summary shape (`:47`), the
`/ratings` and `/stats` companions (`:60-61`), `COMMENT_SORTS` (`:79`) with its argument, and the
comments request (`:97-98`) with `page`, `limit=25` and `extended=full`.

`COMMENT_SORTS` IS THE PORT THAT MATTERS AND ITS COMMENT IS PORTED WITH IT. Trakt sorts comments
by the COMMENTER'S OWN RATING, which is "the one API-sanctioned way to guarantee a rating spread
per title rather than hoping the default ordering happens to contain a dissenter" (`:75-78`).
`likes` gives the well-written middle and `highest`/`lowest` bracket it. Five requests at most,
and §8 stage 5's pack is built out of the spread they produce - a pack assembled from one end of
the distribution is a pack that agrees with itself.

NAMED CHANGE 1: NO SELF-ENQUEUE. `:70-71` enqueues `trakt:comments` from inside `trakt:summary`.
Here both are registered kinds of one stage and the driver runs them in priority order - 40 then
45 - for the reason `acquire/pipeline.py:46-60` gives: §8's unit of work is one title walking ten
stages, and a task per source puts the sequence into the queue's ORDER BY where two workers can
lease two tasks of one title at once.

NAMED CHANGE 2: THE PAGE KEY IS `enumerate`d RATHER THAN `.index()`ed. `:112` computes the raw
store's page number as `COMMENT_SORTS.index((sort, max_pages)) * 10 + page`, which is a lookup by
VALUE - so two sorts given the same page budget would collapse onto one key and the store's
newest-per-page view would hide one of them. The number itself is unchanged, and the spacing of
ten is what keeps two sorts' pages from colliding.

NAMED CHANGE 3: A 404 IS A NOTE. `:50` and `:120` raise `Permanent("not on trakt")`. Trakt is not
the required source (decision 334), so not being on Trakt is a sentence in
`acquisition_job.detail` under this source's name and stage 2 advances.

`trakt.chart` (`:127-133`) IS NOT PORTED. It is a seed helper for discovering titles to crawl,
and §8 stage 1 takes its titles from the household's own Jellyfin library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from spielplan.sources import _ids, _views, credentials
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "trakt"
API = "https://api.trakt.tv"

# `mdc/sources/trakt.py:79`, verbatim: (sort, pages of 25). See the port verdict above for why
# these three and not the default ordering.
COMMENT_SORTS = (("likes", 2), ("lowest", 2), ("highest", 1))

# What the store's `page` column spaces two sorts apart by. Ten, because no sort is given more
# than ten pages and a collision would make one sort's comments invisible to the derive.
_SORT_STRIDE = 10


def _base(kind: str) -> str:
    return "movies" if kind == "movie" else "shows"


def _no_credential(kind: str) -> SourceResult:
    return SourceResult(source=SOURCE, kind=kind, ok=False,
                        note="no Trakt client id configured")


@handler("trakt:summary", source=SOURCE, requires=credentials.TRAKT, priority=40,
         phase="enrich", description="Trakt summary, stats and rating distribution")
async def summary(ctx: StageContext) -> SourceResult:
    """Three requests: the title, its rating distribution, and its watch statistics.

    The two companions are fetched even when one of them fails, which is the corpus's `continue`
    (`:62-65`) as a return value: a distribution without the watch counts is still a
    distribution, and the alternative is to lose both to whichever host hiccup came first.
    """
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
    # Both halves of Trakt's own identity, offered together because they arrive together and the
    # request that fetches one has already fetched the other. `set_ids` is COALESCE, so neither
    # can overwrite what the bundle imported (decision 372).
    filled = await _ids.set_ids(ctx.conn, row["id"], trakt_id=ids.get("trakt"),
                               trakt_slug=ids.get("slug"))

    notes = []
    for companion in ("ratings", "stats"):
        extra = await _views.capture(
            ctx, source=SOURCE, kind=companion, url=f"{API}/{base}/{imdb_id}/{companion}",
            headers=headers, request_meta={"imdb_id": imdb_id}, name=companion,
        )
        if not extra.ok:
            notes.append(f"{companion}: {extra.error}")
    return SourceResult(
        source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
        note="; ".join([f"offered {', '.join(filled)}" if filled else "no new ids", *notes]),
    )


@handler("trakt:comments", source=SOURCE, requires=credentials.TRAKT, priority=45,
         phase="enrich",
         description="User comments, rating-stratified (likes/lowest/highest)")
async def comments(ctx: StageContext) -> SourceResult:
    """The rating-labelled review text, taken from three ends of the distribution.

    Stops early on a short page, because a page under the limit is the last one and asking for
    the next is a request that returns `[]` (`:115-116`). Stops entirely on a 404, because that
    is Trakt saying it does not hold this title at all and the remaining sorts would each say it
    again (`:100-102`, `:117-118`).
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
                # Unique per (sort, page) or the raw store's newest-per-page view collapses two
                # sorts into one document (`mdc/sources/trakt.py:110-112`).
                page=index * _SORT_STRIDE + page,
                request_meta={"sort": sort, "imdb_id": imdb_id}, name=f"{sort} p{page}",
                # THE ONE JSON SHAPE IN §8 STAGE 2 THAT IS NOT AN OBJECT. A comment page is an
                # array, so `capture`'s default `json_object` would file every good page of every
                # sort as not-a-document. The body still has to parse - which is the half that
                # keeps a proxy's interception page out of the store - and the loop below already
                # reads `items` as a sequence. [M5.3 review cycle 1, M53-C1-NET-02]
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
