"""Wikipedia - the fullest plot this pipeline can get, and a digest of what critics said.

Spec v2.1 §8 stage 2 ("wikipedia:article", `spec:367`), §8 stage 4 (the gate needs a plot), §8
stage 5 (the craft supplement); decisions 320, 334, 340, 372.

Cheap, legal, and unusually well suited to §8 stage 6's extraction: the article's *Plot* section
is a far fuller synopsis than any API's one-paragraph overview, and *Critical reception* is a
curated digest of what critics actually said - dense evaluative prose with none of the noise of a
user review feed. Both arrive as plain text in one request (`mdc/sources/wikipedia.py:1-7`).

PORT VERDICT: **ported with named changes** from `mdc/sources/wikipedia.py` (183 lines). Taken
verbatim, and they are the reason this module is the longest of the eight: `API` (`:20`),
`_TITLE_YEAR` (`:36`), `_NOT_A_WORK` (`:40-43`), `_STOP` (`:48-49`), `_title_overlaps`
(`:52-95`) with its thresholds and every measurement in its docstring, `_search`'s query shape
and its year tolerance (`:98-131`), and the extract request's seven parameters (`:147-151`).

THE TWO REFUSALS ARE THE WHOLE VALUE OF THE PORT AND THEY ARE MEASUREMENTS, NOT TASTE.
`_title_overlaps` is at 0.75 and 0.7 because the numbers below them were tried and failed on
named films: bare containment gave *Ragnarok* (2013) the article for *Thor: Ragnarok* and
*Return* (2008) the one for *The Return of the King*, and at 0.5 a one-word title still matched
any two-word article containing it. `_search`'s year check exists because taking the top hit
blindly gave *Obsession* (2015, Dutch) the article for *Obsession (1976 film)*, "and with it a
reception section describing Columbia's 1976 release". A search hit is the same class of risk as
a guessed slug: it writes another film's prose into this title's pack, and §8 stage 7's quote
verification cannot see it because the quote really is in the pack.

NAMED CHANGE 1: THE SEARCH RESPONSE IS STORED. `:109-112` fetches and stores nothing. §8's
preamble is about ALL fetched bytes (`spec:398`), and this one in particular is the EVIDENCE for
a choice that becomes permanent: `set_ids` is fill-never-clobber, so the article title picked
here is the one every later fetch uses, and an operator asking why a title carries the wrong
article has nothing to read unless the search answer is in the store.

NAMED CHANGE 2: A MISSING PAGE IS A `verdict` RATHER THAN A RAISE. `:154-155` raises `Permanent`
after parsing; the store happens first here, so the flag is chosen by the hook `_views.capture`
provides and the bytes stay as the honest record of what the action API returned.

NAMED CHANGE 3: `split_sections` (`:164-183`) IS NOT PORTED HERE. It is a pure function over the
extract text with no transport in it at all, which makes it §8 stage 3's by the same rule that
keeps `_htmlutil` free of the fetcher; the plan puts it in `derive/parse.py` with the other
fourteen entry points. A reader looking for it here finds this paragraph.

DECISION 320 AND WHAT THIS MODULE MAKES TRUE. `title.wikipedia_title` is an article identifier,
and until this milestone the build held none - which is the premise decision 320's deferral of
the CC BY-SA material link rests on. What it is written FOR is the next request's url: no route
exposes it and no surface can read it, so nothing here makes a link practicable. The guard that
watches that premise is narrowed to the two places where it could stop being true, and this
paragraph is one half of its argument (`test_static_contracts.py`, `_reads_the_article_carrier`).
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

# Any parenthesised year, however the qualifier is worded: "(1976 film)", "(2011 Indian film)",
# "(1984 TV series)". Requiring the literal word "film" let "180 (2011 Indian film)" through as
# undisambiguated. (`mdc/sources/wikipedia.py:33-36`.)
_TITLE_YEAR = re.compile(r"\((\d{4})[^)]*\)")

# Articles that are never a film's own page, however well the words overlap (`:39-43`).
_NOT_A_WORK = re.compile(
    r"^(list of\b|\d{4} in (film|television)\b|index of\b|outline of\b)"
    r"|\((disambiguation|book|novel|musical|band|album|song|magazine|"
    r"bar|company|newspaper|video game)\)$", re.I)

# Words that carry no identifying power, so they must not be able to build a match on their own:
# "The Journey" vs "The Hobbit: An Unexpected Journey" scored 2/2 = 1.0 because both tokens were
# shared (`:45-49`).
_STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
         "part", "chapter", "film", "movie", "series", "tv"}

# How many of the film's meaningful words must appear in the article's, and how much of the
# article the film must account for. Both were raised to these values against named failures -
# see the port verdict - so they are measurements and a later tuning pass needs a film it got
# wrong, not an intuition.
_MIN_TITLE_SHARE = 0.75
_MIN_ARTICLE_SHARE = 0.7


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def _title_overlaps(article: str, title: str) -> bool:
    """Does the article name plausibly refer to this film at all? `:52-95`, verbatim.

    Guards against the search returning something unrelated when a film has no article -
    observed: "Los politicos" (1904) matching "2028 Summer Olympics", "The Right of Youth" (1911)
    matching "2026 in film".

    Bare containment used to be enough, which is how *Ragnarok* (2013) got *Thor: Ragnarok* and
    *Return* (2008) got *The Return of the King*: a short title is a substring of every longer
    article that happens to contain it. Containment now has to run both ways on the MEANINGFUL
    words - the article may not introduce a whole extra subject the film never names.
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
        # An all-stopword title ("The Return") can only be taken on an exact match, never on
        # overlap.
        return a.strip() == t.strip()

    shared = theirs & ours
    # Nearly all of the film's words must appear in the article's...
    if len(shared) / len(ours) < _MIN_TITLE_SHARE:
        return False
    # ...and the article must not introduce a subject the film never names. Real disambiguations
    # - "(1976 film)", "(2016 American film)" - are stripped before this point, so a surviving
    # extra word is genuinely part of a different name.
    return len(shared) / len(theirs) >= _MIN_ARTICLE_SHARE


def _pick(hits: list[str], title: str, year: int | None) -> str | None:
    """The first hit that survives both refusals. `mdc/sources/wikipedia.py:119-131`, verbatim.

    Wikipedia disambiguates same-named films by year in the article title, so when a hit carries
    a year it is checkable and a mismatch means the search found a different film. A hit with no
    year carries no contradicting evidence and is taken.
    """
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
    """Find an article by search, and refuse a hit that names a different year.

    Returns `(article title or None, note)`. The search answer is stored either way - see named
    change 1 - because it is the evidence for a choice `set_ids` then makes permanent.
    """
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

    `requires=None`: the action API is free and unauthenticated. `acquire/hosts.py` overrides
    robots.txt for this host with the reason written beside the number - Wikimedia's robots.txt
    is aimed at search engines indexing article HTML, and this is the action API, which Wikimedia
    governs by its own User-Agent policy instead.
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
        # Written before the extract is fetched, exactly as `:144-145` does, so a title whose
        # extract request fails does not have to run the search again on the next drain. It is
        # COALESCE, so this cannot overwrite an article a household or the bundle supplied.
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
