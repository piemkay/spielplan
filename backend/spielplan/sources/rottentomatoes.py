"""Rotten Tomatoes: scores only (review text is hydrated client-side), and the canonical slug.

`people_decide=False`: RT's cast lists are the English dub for anime, so year or cast may vouch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "rottentomatoes"
BASE = "https://www.rottentomatoes.com"


def candidate_paths(row: Any) -> list[str]:
    """The RT paths worth trying, best first. A supplied slug is the only candidate."""
    prefix = "m" if row["kind"] == "movie" else "tv"
    if row["rt_slug"]:
        slug = row["rt_slug"].strip("/")
        return [slug if slug.startswith(("m/", "tv/")) else f"{prefix}/{slug}"]
    name = row["name"] or row["original_name"]
    if not name:
        return []
    base = _ids.slugify(name).replace("-", "_")
    out = [f"{prefix}/{base}_{row['year']}"] if row["year"] else []
    out.append(f"{prefix}/{base}")
    return out


async def page(ctx: StageContext) -> SourceResult:
    """One request per candidate, at seven-tenths of a request a second."""
    kind = "rt:page"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")
    paths = candidate_paths(row)
    if not paths:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no slug candidate")

    guessed = not row["rt_slug"]
    people = await _ids.known_people(ctx.conn, row["id"]) if guessed else set()

    wrong: list[str] = []
    errors: list[str] = []
    unproven: list[str] = []
    last_doc: int | None = None
    for path in paths:
        captured = await _views.capture(
            ctx, source=SOURCE, kind="page:main", url=f"{BASE}/{path}",
            content_type=_views.HTML, min_bytes=_views.MIN_HTML_BYTES,
            request_meta={"requested": path}, name=path,
        )
        last_doc = captured.doc_id or last_doc
        if not captured.ok:
            errors.append(f"{path}: {captured.error}")
            continue
        if captured.unchanged:
            if guessed:
                # A 304 on a guess is the sign of an earlier refusal (an accepted guess writes `rt_slug`);
                # try on.
                unproven.append(path)
                continue
            # A supplied slug is an identifier, not a guess: nothing to check.
            return SourceResult(source=SOURCE, kind=kind, ok=True,
                                note=f"path={path}: unchanged since the last fetch")

        # The canonical slug is the last redirect hop, the only form RT's sub-paths accept.
        canonical = urlparse(captured.response.url).path.strip("/")
        if guessed and not _ids.belongs_to_title(
            captured.content, year=row["year"], people=people, mode="rt", people_decide=False
        ):
            # The bytes stay (append-only); the slug does not.
            wrong.append(canonical)
            continue
        await _ids.set_ids(ctx.conn, row["id"], rt_slug=canonical)
        note = f"path={canonical}"
        return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                            note=note if canonical == path else f"{note} (from {path})")

    refusals = [f"{', '.join(wrong)}: a different title of the same name"] if wrong else []
    if unproven:
        # Kept apart from `wrong`: read-and-refused versus held-but-never-accepted.
        refusals.append(f"{', '.join(unproven)}: unchanged since a fetch that was never accepted")
    if refusals:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=last_doc,
                            note="; ".join(refusals))
    return SourceResult(
        source=SOURCE, kind=kind, ok=False, doc_id=last_doc,
        note=f"no RT page for any of {', '.join(paths)}"
             + (f": {'; '.join(errors)}" if errors else ""),
    )
