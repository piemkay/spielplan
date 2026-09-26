"""Metacritic: critic and user reviews with normalised scores, behind a slug that must be right.

Only two views: the filtered and paged ones are hydrated client-side and return nothing. A guessed
slug must be proved by fetching the title page first, since review pages carry no identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "metacritic"
BASE = "https://www.metacritic.com"


def slug_candidates(row: Any) -> list[str]:
    """The Metacritic paths worth trying, best first; a supplied slug is the only candidate."""
    prefix = "movie" if row["kind"] == "movie" else "tv"
    if row["metacritic_slug"]:
        slug = row["metacritic_slug"].strip("/")
        return [slug if slug.startswith(("movie/", "tv/")) else f"{prefix}/{slug}"]
    name = row["name"] or row["original_name"]
    if not name:
        return []
    base = f"{prefix}/{_ids.slugify(name)}"
    return [f"{base}-{row['year']}", base] if row["year"] else [base]


async def candidate_paths(conn: asyncpg.Connection, row: Any) -> list[str]:
    """`slug_candidates`, minus any guess another title already holds (same-name films collide)."""
    candidates = slug_candidates(row)
    if row["metacritic_slug"] or not candidates:
        return candidates
    taken = await conn.fetchval(
        "SELECT count(*) FROM title WHERE metacritic_slug = $1 AND id <> $2",
        candidates[-1], row["id"],
    )
    return [] if taken else candidates


@dataclass(frozen=True)
class _Resolved:
    """A path proved to be about this film, and what proving it cost."""

    path: str = ""
    doc_id: int | None = None
    fetched: bool = False
    note: str = ""


async def resolve_path(ctx: StageContext, row: Any) -> _Resolved:
    """The Metacritic path for this title, proved to be about this film.

    A supplied slug is taken as given and not fetched here; `fetched` says which happened.
    """
    if row["metacritic_slug"]:
        return _Resolved(path=slug_candidates(row)[0])

    candidates = await candidate_paths(ctx.conn, row)
    if not candidates:
        return _Resolved(note="no slug candidate")
    people = await _ids.known_people(ctx.conn, row["id"])

    wrong: list[str] = []
    errors: list[str] = []
    unproven: list[str] = []
    last_doc: int | None = None
    for path in candidates:
        captured = await _views.capture(
            ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{path}/",
            content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
            request_meta={"path": path}, name=path,
        )
        last_doc = captured.doc_id or last_doc
        if not captured.ok:
            errors.append(f"{path}: {captured.error}")
            continue
        if captured.unchanged:
            # A 304 on a guess means those bytes were fetched before and refused: no new evidence, no
            # promotion.
            unproven.append(path)
            continue
        if not _ids.belongs_to_title(captured.content, year=row["year"], people=people,
                                     mode="metacritic"):
            # The bytes stay (append-only); the slug and the review fetch do not.
            wrong.append(path)
            continue
        await _ids.set_ids(ctx.conn, row["id"], metacritic_slug=path)
        return _Resolved(path=path, doc_id=captured.doc_id, fetched=True)

    refusals = [f"{', '.join(wrong)}: a different film of the same name"] if wrong else []
    if unproven:
        # Kept apart from `wrong`: read-and-refused versus held-but-never-accepted.
        refusals.append(f"{', '.join(unproven)}: unchanged since a fetch that was never accepted")
    if refusals:
        return _Resolved(doc_id=last_doc, note="; ".join(refusals))
    return _Resolved(
        doc_id=last_doc,
        note=f"no metacritic page for any of {', '.join(candidates)}"
             + (f": {'; '.join(errors)}" if errors else ""),
    )


@handler("metacritic:page", source=SOURCE, priority=77, phase="enrich",
         description="Metacritic title page (metascore + user score)")
async def page(ctx: StageContext) -> SourceResult:
    """The title page: the metascore, the user score, and the proof that the slug is this film's."""
    kind = "metacritic:page"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")

    resolved = await resolve_path(ctx, row)
    if not resolved.path:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=resolved.doc_id,
                            note=resolved.note)
    if resolved.fetched:
        return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=resolved.doc_id,
                            note=f"path={resolved.path}"
                                 + (f" ({resolved.note})" if resolved.note else ""))

    # A supplied slug has not been fetched yet; a guessed one already was, by the identity check.
    captured = await _views.capture(
        ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{resolved.path}/",
        content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
        request_meta={"path": resolved.path}, name=resolved.path,
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=f"no metacritic page at {resolved.path}: {captured.error}")
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                        note=f"path={resolved.path}")


@handler("metacritic:reviews", source=SOURCE, priority=86, phase="enrich",
         description="Scored critic excerpts + user reviews")
async def reviews(ctx: StageContext) -> SourceResult:
    """Two views, both server-rendered. The rows §8 stage 4's gate counts come out of these.

    With no slug on the title, one is resolved and proved here, never guessed.
    """
    kind = "metacritic:reviews"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")

    resolved = await resolve_path(ctx, row)
    if not resolved.path:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=resolved.doc_id,
                            note=resolved.note)

    path = resolved.path.strip("/")
    captured = await _views.fetch_views(
        ctx, source=SOURCE, kind_prefix="reviews", views=[
            _views.View(name="critics", url=f"{BASE}/{path}/critic-reviews/"),
            _views.View(name="users", page=1, url=f"{BASE}/{path}/user-reviews/"),
        ],
    )
    got = [cap for cap in captured if cap.ok]
    if not got:
        return SourceResult(source=SOURCE, kind=kind, ok=False,
                            doc_id=next((c.doc_id for c in captured if c.doc_id), None),
                            note=f"no review view fetched: {_views.notes(captured)}")
    note = f"{len(got)}/{len(captured)} views"
    failed = _views.notes(captured)
    return SourceResult(source=SOURCE, kind=kind, ok=True,
                        doc_id=next((c.doc_id for c in got if c.doc_id), None),
                        note=f"{note}; {failed}" if failed else note)
