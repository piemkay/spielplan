"""Wikipedia: the fullest plot available, and a digest of critical reception.

The two refusals (`_title_overlaps`, `_search`'s year check) are tuned on named wrong matches.
The search response is stored as evidence for a choice `set_ids` makes permanent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from spielplan.acquire import fetch
from spielplan.sources import _ids, _views
from spielplan.sources.base import SourceResult, handler

if TYPE_CHECKING:
    from spielplan.acquire.stages import StageContext

SOURCE = "wikipedia"
API = "https://en.wikipedia.org/w/api.php"

# Any parenthesised year, however the qualifier is worded.
_TITLE_YEAR = re.compile(r"\((\d{4})[^)]*\)")

# Articles that are never a film's own page.
_NOT_A_WORK = re.compile(
    r"^(list of\b|\d{4} in (film|television)\b|index of\b|outline of\b)"
    r"|\((disambiguation|book|novel|musical|band|album|song|magazine|"
    r"bar|company|newspaper|video game)\)$", re.I)

# Words with no identifying power must not build a match alone.
_STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
         "part", "chapter", "film", "movie", "series", "tv"}

# Raised against named failures; retune only with a film it got wrong.
_MIN_TITLE_SHARE = 0.75
_MIN_ARTICLE_SHARE = 0.7


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def _title_overlaps(article: str, title: str) -> bool:
    """Does the article name plausibly refer to this film at all?

    Containment must hold both ways on meaningful words, so *Ragnarok* never gets *Thor: Ragnarok*.
    """
    if _NOT_A_WORK.search(article.strip()):
        return False

    a = _norm(_TITLE_YEAR.sub("", article))
    t = _norm(title)
    if not a.strip() or not t.strip():
        return False

    theirs = {w for w in a.split() if w and w not in _STOP}
    ours = {w for w in t.split() if w and w not in _STOP}
    if not ours or not theirs:
        # An all-stopword title matches only exactly.
        return a.strip() == t.strip()

    shared = theirs & ours
    if len(shared) / len(ours) < _MIN_TITLE_SHARE:
        return False
    # Disambiguations are already stripped, so an extra word is part of a different name.
    return len(shared) / len(theirs) >= _MIN_ARTICLE_SHARE


def _pick(hits: list[str], title: str, year: int | None) -> str | None:
    """The first hit that survives both refusals; a hit with no year carries no contradiction."""
    for hit in hits:
        if not _title_overlaps(hit, title):
            continue
        found = _TITLE_YEAR.search(hit)
        if found and year:
            if abs(int(found.group(1)) - year) <= 1:
                return hit
            continue
        if not found:
            return hit
    return None


async def _search(ctx: StageContext, row, title: str) -> tuple[str | None, str]:
    """Find an article by search, refusing a hit that names a different year. Returns `(title, note)`."""
    year = row["year"]
    hint = "film" if row["kind"] == "movie" else "TV series"
    query = f"{title} {year} {hint}" if year else f"{title} {hint}"
    captured = await _views.capture(
        ctx, source=SOURCE, kind="search", url=API,
        params={"action": "query", "list": "search", "srsearch": query,
                "srlimit": 5, "format": "json", "formatversion": 2},
        request_meta={"srsearch": query},
    )
    if not captured.ok:
        return None, captured.error
    if captured.unchanged:
        return None, "the search answer is unchanged and no article is on the title"

    data = captured.response.json()
    hits = [h.get("title") for h in
            ((data.get("query") or {}).get("search") or []) if h.get("title")]
    if not hits:
        return None, f"no wikipedia search hit for {query!r}"
    picked = _pick(hits, title, year)
    if picked is None:
        return None, f"no wikipedia hit for {query!r} is this film: refused {', '.join(hits)}"
    return picked, f"searched: {picked}"


def _missing(response: fetch.Response) -> str:
    """The action API's own way of saying the article is not there: a 200 with `missing: true`."""
    try:
        data = response.json()
    except ValueError:
        return "wikipedia answered with a body that is not JSON"
    pages = (data.get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        return "wikipedia holds no such article"
    return ""


@handler("wikipedia:article", source=SOURCE, priority=50, phase="enrich",
         description="Full article plain text (plot, reception, production)")
async def article(ctx: StageContext) -> SourceResult:
    """One or two requests: the search, if the title does not already carry an article, and the
    extract.
    """
    kind = "wikipedia:article"
    row = await _ids.title_row(ctx.conn, ctx.title_id)
    if row is None:
        return SourceResult(source=SOURCE, kind=kind, ok=False, note="no title row")

    page = row["wikipedia_title"]
    searched = ""
    if not page:
        name = row["name"] or row["original_name"] or ""
        if not name:
            return SourceResult(source=SOURCE, kind=kind, ok=False,
                                note="no name to search wikipedia by")
        page, searched = await _search(ctx, row, name)
        if not page:
            return SourceResult(source=SOURCE, kind=kind, ok=False, note=searched)
        # Written before the extract, so a failed extract does not repeat the search. COALESCE.
        await _ids.set_ids(ctx.conn, row["id"], wikipedia_title=page)

    captured = await _views.capture(
        ctx, source=SOURCE, kind="article", url=API,
        params={"action": "query", "prop": "extracts|pageprops", "explaintext": 1,
                "exsectionformat": "wiki", "titles": page, "redirects": 1,
                "format": "json", "formatversion": 2},
        request_meta={"page": page}, verdict=_missing,
    )
    if not captured.ok:
        return SourceResult(source=SOURCE, kind=kind, ok=False, doc_id=captured.doc_id,
                            note=f"{page}: {captured.error}")
    if captured.unchanged:
        return SourceResult(source=SOURCE, kind=kind, ok=True,
                            note=f"{page}: unchanged since the last fetch")

    pages = (captured.response.json().get("query") or {}).get("pages") or []
    extract = pages[0].get("extract") or ""
    note = f"{page}: {len(extract)} chars"
    return SourceResult(source=SOURCE, kind=kind, ok=True, doc_id=captured.doc_id,
                        note=f"{note} ({searched})" if searched else note)
