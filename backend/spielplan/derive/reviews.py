"""§8 stage 3's review extraction: the rows stage 4's gate counts and stage 5's pack is built of.

Spec v2.1 §8 stages 3 and 4, §4.1 rule 8 ("UTF-8 everywhere; never 'clean' non-ASCII"), §10's
promise that review bodies travel because they are "needed for future re-extraction and text
embedding"; decisions 334, 335.

PORT VERDICT: **ported with named changes** from `mdc/parse/reviews.py` (490 lines). Taken:
`ParsedReview` (`:39-63`) with its `fingerprint`, `_norm` (`:66-72`), `_int` (`:78-82`),
`iso_date` (`:92-117`) with `_MONTHS` / `_DATE_WORDS` / `_DATE_ISO` / `_SENTINEL_DATES`,
`parse_tmdb` (`:125-152`), `parse_trakt` (`:159-178`), `parse_metacritic` (`:185-190`) with
`_mc_from_json` (`:193-228`) and `_mc_from_html` (`:235-306`), `RECEPTION_SECTIONS` (`:318-319`)
and `parse_wikipedia_reception` (`:322-353`), and the `parse_document` dispatch (`:459-485`).

The corpus's header is the rule these are written to and still is: "the parsers here are
deliberately forgiving. Each source yields whatever it can rather than failing the document. A
site that changes its markup costs a parser fix and a `mdc rebuild`, never a re-crawl." Decision
334 is the same sentence from the app's side -- only `tmdb:detail` is required and every other
source's failure is a note on the job.

THE FOUR SOURCES ARE THE FOUR THAT ANSWER, and that is a measurement rather than a choice. The
corpus deleted its IMDb, Rotten Tomatoes and Letterboxd review parsers after live testing --
"IMDb answers 202/empty to this client, RT hydrates its review lists client-side, and
Letterboxd's review pages sit behind a bot challenge. None of them ever returned a review." That
is why §8 stage 4's gate asks for two sources and not five, and why `rt:page` is fetched for two
percentages rather than for prose.

NAMED CHANGE 1: **no BeautifulSoup.** `_mc_from_html` is the one parser in either project that
needed a DOM, and this app declares neither `beautifulsoup4` nor `lxml` (`backend/pyproject.toml`
`dependencies`) -- a parser is not a reason to add a dependency, and `test_static_contracts.py`
holds that file to what it declares. So `_collect` below is a `html.parser.HTMLParser` scan that
answers the four questions the bs4 selectors asked: find the elements whose attributes match,
give me their inner markup, their text and their attributes. It is a REWRITE of the mechanism
and a PORT of the selectors: `data-testid` is still the primary key into the card and the
Tailwind class names are still only a fallback, for the reason the corpus states -- "The Tailwind
class names on those cards change constantly; the `data-testid` attributes have been stable".

NAMED CHANGE 2: **the app's review row definitions are imported, not re-derived.**
`importer/reviews.REVIEW_SOURCE` (`:57-67`) is the map from `review_store.review`'s columns to
the corpus's, and it carries the argument for the one non-obvious pairing -- `rating` takes
`rating_norm` and not `rating_raw`, because the raw column is the review's own notation ("8/10",
"Rotten") and the target is `double precision`. `review_row` below spends that map rather than
listing columns again, so a derived review and an imported one are the same row by construction.
`repair_mojibake` (`:76-98`) is imported for the same reason: rule 8's repair is one definition,
and a second one here would be a second answer about which bytes may be touched. What that import
also does is put TWO of this tree's repairs over one string, which `sources/_htmlutil.py` forbade
in as many words until M5.3's first review cycle measured the composition and amended the rule
there rather than here -- see `review_row` and that module's paragraph.
[M5.3 review cycle 1, m53-c1-slug-05]

NAMED CHANGE 3: **the bulk parsers are not ported.** `parse_rt_bulk` (`:361-377`),
`parse_imdb_bulk` (`:380-395`), `split_review_blob` (`:409-431`), `parse_mpst_bulk` (`:434-456`)
and `bucket_of` (`:488-494`) read Kaggle, aclImdb and MPST dataset files that `mdc/bulk.py`
downloaded once; §8 stage 2 fetches none of them and this app's raw store will never hold one.
They are unlike `parse.parse_letterboxd_page`, which IS ported for a source nothing fetches:
there the install already holds `title_meta` rows to re-derive, and here the equivalent rows in
`review_store.review` arrive from `importer/reviews.load_reviews` ALREADY normalised out of
`reviews.sqlite`. A parser with neither a fetcher nor a stored document to read is not a hole.

NAMED CHANGE 4: **`parse_trakt_ratings` is not here.** The corpus files Trakt's rating histogram
with the reviews; it produces `platform_rating` rows and no review, so it lives in
`derive/parse.py` with the other eleven-table parsers.

WHAT THIS MODULE DOES NOT DECIDE. It does not deduplicate, it does not drop a short review and it
does not count anything: decision 335's fifty words is a TOTAL measured in `derive/gate.py` off
`review_store.review.word_count`, and the corpus's own `MIN_WORDS` is a per-review floor inside
`mdc/dna/packs.py` that M5.4 inherits. A parser that dropped a 30-word review here would silently
change the gate's arithmetic from the wrong end.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from spielplan.derive.parse import split_sections

# `_AUTHOR_KIND` is underscored and is imported anyway, deliberately. It is the two-value
# vocabulary that keeps `is_critic` honest -- `importer/reviews.py:69-73` records that
# `bool(author_kind)` maps every review to True "including 'user'", because a non-empty string is
# truthy and the failure is invisible. A second copy here would be a second answer to the same
# question in the same tree, and the day someone adds a third kind upstream only one of the two
# would learn about it. One definition, spelled where the import shows whose it is.
from spielplan.importer.reviews import _AUTHOR_KIND, REVIEW_SOURCE, repair_mojibake
from spielplan.sources._htmlutil import clean_text, next_data, walk


@dataclass
class ParsedReview:
    """One review, in the corpus's own field names.

    The names are the corpus's rather than `review_store.review`'s because `REVIEW_SOURCE` is the
    map between them and it lives on the import side; spelling the target's names here would make
    that map a thing two modules disagree about. `review_row` is the one place the crossing
    happens.
    """

    body: str
    source: str
    external_id: str | None = None
    author: str | None = None
    author_kind: str = "user"
    publication: str | None = None
    rating_raw: str | None = None
    rating_scale: float | None = None
    rating_norm: float | None = None
    headline: str | None = None
    created_date: str | None = None
    url: str | None = None
    is_spoiler: bool = False
    helpful_yes: int | None = None
    helpful_total: int | None = None
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """A stable identity for this review, for a derive that must not duplicate a row.

        The source's own id when it has one; otherwise a hash of the author and the first 400
        characters of the body. Decision 375 makes the derive idempotent by replacing a
        `(title_id, source)` scope rather than by matching fingerprints, so this is what a reader
        needs when two documents of ONE source carry the same review -- Metacritic's critic and
        user pages overlap, and the corpus's `_mc_from_json` already dedupes on `body[:120]`
        within a document but cannot see across two.
        """
        if self.external_id:
            return str(self.external_id)
        seed = f"{self.author or ''}|{self.body[:400]}"
        return "h" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:20]


def review_row(parsed: ParsedReview, title_id: int) -> dict[str, Any]:
    """`parsed` as a `review_store.review` row, through the import's own column map.

    Two transforms, and both are the import's: `is_critic` goes through `_AUTHOR_KIND` so an
    unrecognised kind is NULL rather than a critic, and `body` goes through `repair_mojibake` so
    rule 8's one conservative round trip applies to a scraped body exactly as it applies to a
    shipped one. `word_count` is not here and must not be: it is a GENERATED STORED column
    (`0003_content.sql:249-250`) and decision 335 reads the gate off it precisely so the
    measurement is taken from the stored body rather than from whatever a writer believed it had
    stored.

    THAT IS THE SECOND REPAIR THIS BODY HAS HAD, AND IT IS DELIBERATE RATHER THAN OVERLOOKED.
    Every `ParsedReview.body` in this module is built by `clean_text`, which runs
    `_htmlutil.fix_mojibake` -- the PER-RUN repair -- so this line composes the two repairs
    `sources/_htmlutil.py` said must never both run over one string. M5.3's first review cycle
    measured the composition rather than choosing between the sentence and the code: over 200,000
    randomly mangled strings compared against their own ground truth, the second pass recovered
    8,278 and damaged none. What it recovers is the DOUBLY-encoded body, which the per-run pass
    reduces to a singly-encoded one and cannot finish. `_htmlutil`'s rule is amended there to say
    so; this line is what it is about, and a reader deleting it on the strength of the old
    sentence would be re-mangling those 8,278. [M5.3 review cycle 1, m53-c1-slug-05]

    IT IS NOT DAMAGE-FREE, and "damaged none" above is one generator's result rather than a
    property of the pair. The per-run guard wants a marker followed by a CONTINUATION character
    (0x80-0xBF) and the importer's wants the bare marker, so a marker immediately before cp1252's
    C1 punctuation - the ellipsis, the dashes - passes the first pass untouched and can make this
    line re-encode the whole body around it: A-circumflex and an ellipsis are stored as U+0085.
    Cycle 2 re-measured it both ways: about a thousand synthetic recoveries per string damaged,
    and on 172,885 real Metacritic bodies out of the corpus's raw store no change at all. The
    composition stays; `_htmlutil`'s paragraph carries the measurement and why the guard there was
    not widened, and `test_sources_base.py` pins the damage beside the recovery.
    [M5.3 review cycle 2, m53-c2-moji-01]
    """
    values = {"title_id": title_id, **{k: getattr(parsed, v, None)
                                       for k, v in REVIEW_SOURCE.items() if k != "title_id"}}
    body, _repaired = repair_mojibake(parsed.body or "")
    values["body"] = body
    kind = parsed.author_kind
    values["is_critic"] = _AUTHOR_KIND.get(kind.strip().lower()) if isinstance(kind, str) else None
    return values


def _norm(value: float | None, scale: float) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value) / scale))
    except (TypeError, ValueError):
        return None


def _int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group(0)) if match else None


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_DATE_WORDS = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")
_DATE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# The source dataset's placeholder for "no date recorded". Passed through uncleaned it reads as a
# real publication date 200 years before cinema.
_SENTINEL_DATES = {"1800-01-01", "0000-00-00", "1900-01-01"}


def iso_date(value: str | None) -> str | None:
    """Normalise a review date to YYYY-MM-DD, or drop it.

    Metacritic renders its dates as "Feb 23, 2023" while every other source gives ISO, so a
    column that looks uniform sorts and range-filters wrongly for the 74k rows that are not.
    Anything unparseable becomes NULL rather than being stored as prose: a date column that
    sometimes holds prose is worse than one that is honestly empty.
    """
    text = (value or "").strip()
    if not text:
        return None
    match = _DATE_ISO.match(text)
    if match:
        return None if match.group(0) in _SENTINEL_DATES else match.group(0)
    match = _DATE_WORDS.search(text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        if month:
            out = f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"
            return None if out in _SENTINEL_DATES else out
    return None


# ---------------------------------------------------------------------------
# TMDB (JSON)
# ---------------------------------------------------------------------------


def parse_tmdb(payload: Any) -> list[ParsedReview]:
    """TMDB's long-form user reviews, which arrive either alone or appended to a detail call."""
    results: Any = []
    if isinstance(payload, Mapping):
        if "results" in payload:
            results = payload.get("results") or []
        elif "reviews" in payload:
            results = (payload.get("reviews") or {}).get("results") or []
    out = []
    for review in results:
        body = clean_text(review.get("content"))
        if not body:
            continue
        details = review.get("author_details") or {}
        rating = details.get("rating")
        out.append(ParsedReview(
            source="tmdb", body=body, external_id=review.get("id"),
            author=review.get("author") or details.get("username"),
            rating_raw=str(rating) if rating is not None else None,
            rating_scale=10.0, rating_norm=_norm(rating, 10.0),
            created_date=iso_date(review.get("created_at")),
            url=review.get("url"),
        ))
    return out


# ---------------------------------------------------------------------------
# Trakt (JSON)
# ---------------------------------------------------------------------------


def parse_trakt(payload: Any) -> list[ParsedReview]:
    """Trakt comments, which carry the commenter's own /10 rating.

    The corpus fetches them across the `likes`, `lowest` and `highest` sorts so the rating bands
    fill; the parser sees one page of one sort at a time and does not care which.
    """
    items = payload if isinstance(payload, list) else []
    out = []
    for comment in items:
        if not isinstance(comment, Mapping):
            continue
        body = clean_text(comment.get("comment"))
        if not body:
            continue
        rating = comment.get("user_rating")
        out.append(ParsedReview(
            source="trakt", body=body,
            external_id=str(comment.get("id")) if comment.get("id") else None,
            author=(comment.get("user") or {}).get("username"),
            rating_raw=str(rating) if rating is not None else None,
            rating_scale=10.0, rating_norm=_norm(rating, 10.0),
            created_date=iso_date(comment.get("created_at")),
            is_spoiler=bool(comment.get("spoiler")),
            helpful_yes=comment.get("likes"),
            meta={"is_review": bool(comment.get("review"))},
        ))
    return out


# ---------------------------------------------------------------------------
# the markup scan that replaces BeautifulSoup
# ---------------------------------------------------------------------------

# HTML elements that never have an end tag. `html.parser` reports them through `handle_startendtag`
# when they are written self-closing and through `handle_starttag` when they are not, so a scan
# that tracked depth by tag name alone would never close a `<br>` and would swallow the rest of
# the document into whichever element contained it.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "param", "source", "track", "wbr"})


@dataclass(frozen=True)
class Element:
    """One matched element: its tag, its attributes, its inner markup and its text.

    The four things `_mc_from_html`'s bs4 selectors asked of a node, and nothing else. `text` is
    `get_text(" ")` -- data nodes joined by a single space -- because that is what the corpus's
    header parsing is written against: "The header text is '<score> <name>'" only holds if the
    score element and the name are separated when they are concatenated.
    """

    tag: str
    attrs: Mapping[str, str]
    html: str
    text: str

    def get(self, name: str) -> str:
        """An attribute's value, empty when absent, so a caller can match without a None check."""
        return self.attrs.get(name, "")


class _Collector(HTMLParser):
    """Collect the OUTERMOST elements matching `match`, with their inner markup.

    Outermost rather than every match, because bs4's `select` on these selectors returns the
    review cards and not the cards nested inside a card -- and because a nested match would be
    returned twice, once on its own and once inside its parent, which is how a review card would
    become two reviews.
    """

    def __init__(self, match: Callable[[str, Mapping[str, str]], bool]) -> None:
        super().__init__(convert_charrefs=True)
        self._match = match
        self._open: list[tuple[str, Mapping[str, str], int]] = []
        self._depth = 0
        self._text: list[list[str]] = []
        self.found: list[Element] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def feed_source(self, page: str) -> list[Element]:
        self._page = page
        offset = 0
        self._lines = []
        for line in page.splitlines(keepends=True):
            self._lines.append(offset)
            offset += len(line)
        self._lines.append(offset)
        self.feed(page)
        self.close()
        # An element left open by a truncated document is still an answer: the markup from its
        # start tag to the end of what arrived is what the source sent, and the alternative is
        # dropping a review because the response was cut short.
        while self._open:
            self._finish(len(page))
        return self.found

    def _finish(self, end: int) -> None:
        tag, attrs, start = self._open.pop()
        text = self._text.pop()
        if self._open:
            # A nested element's text is its PARENT's text too, which is what `get_text(" ")`
            # means and what `_mc_from_html` depends on: a review card's header is
            # "<score> <name>" only because the score sits in a `<div>` inside the `<a>`, and a
            # scan that dropped a child's data would read every header as a bare score.
            self._text[-1].extend(text)
            return
        self.found.append(Element(tag=tag, attrs=attrs, html=self._page[start:end],
                                  text=re.sub(r"\s+", " ", " ".join(text)).strip()))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        self._depth += 1
        if self._open or self._match(tag, {k: (v or "") for k, v in attrs}):
            if not self._open:
                start_tag = self.get_starttag_text() or ""
                self._open.append((tag, {k: (v or "") for k, v in attrs},
                                   self._offset() + len(start_tag)))
                self._text.append([])
            else:
                self._open.append((tag, {}, -1))
                self._text.append([])

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        self._depth = max(0, self._depth - 1)
        if not self._open:
            return
        # Walk back to the nearest open element with this tag name. Metacritic's markup carries
        # unclosed `<!--[-->` template markers and the occasional stray `</div>`, and a scan that
        # popped unconditionally would close a card on somebody else's end tag.
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                while len(self._open) > index:
                    self._finish(self._offset())
                return

    def handle_data(self, data: str) -> None:
        if self._text:
            self._text[-1].append(data)


def collect(page: str, match: Callable[[str, Mapping[str, str]], bool]) -> list[Element]:
    """Every outermost element of `page` matching `match`, in document order."""
    return _Collector(match).feed_source(page)


def first(page: str, match: Callable[[str, Mapping[str, str]], bool]) -> Element | None:
    """`select_one`: the first match, or None."""
    found = collect(page, match)
    return found[0] if found else None


def _testid(value: str) -> Callable[[str, Mapping[str, str]], bool]:
    return lambda _tag, attrs: attrs.get("data-testid") == value


def _class_contains(*fragments: str) -> Callable[[str, Mapping[str, str]], bool]:
    return lambda _tag, attrs: any(f in attrs.get("class", "") for f in fragments)


# ---------------------------------------------------------------------------
# Metacritic
# ---------------------------------------------------------------------------


def parse_metacritic(content: bytes, view: str) -> list[ParsedReview]:
    """`view` says which page this is, and therefore which scale its scores are on.

    A critic review is scored out of 100 and a user review out of 10, and the page does not
    always say so next to the number -- so a critic page parsed as a user page turns every
    Metascore into a rating ten times its own scale, which `rating_norm` then clamps to 1.0 and
    nothing downstream can tell from the stored row.
    """
    is_critic = "critic" in view
    out = _mc_from_json(content, is_critic)
    if out:
        return out
    return _mc_from_html(content, is_critic)


def _mc_from_json(content: bytes, is_critic: bool) -> list[ParsedReview]:
    data = next_data(content)
    if data is None:
        return []

    def is_review(node: dict) -> bool:
        return isinstance(node, dict) and "quote" in node and "score" in node

    out: list[ParsedReview] = []
    seen: set[str] = set()
    for node in walk(data, is_review):
        body = clean_text(node.get("quote"))
        if not body:
            continue
        if body[:120] in seen:
            continue
        seen.add(body[:120])
        score = node.get("score")
        scale = 100.0 if is_critic else 10.0
        publication = node.get("publicationName")
        if publication is None and isinstance(node.get("publication"), Mapping):
            publication = node["publication"].get("name")
        out.append(ParsedReview(
            source="metacritic", body=body,
            external_id=str(node.get("id")) if node.get("id") else None,
            author=node.get("author") or node.get("reviewerName"),
            author_kind="critic" if is_critic else "user",
            publication=publication,
            rating_raw=str(score) if score is not None else None,
            rating_scale=scale, rating_norm=_norm(score, scale),
            created_date=iso_date(str(node.get("date") or "")),
            url=node.get("url"),
        ))
    return out


_MC_SCORE_TITLE = re.compile(r"(Metascore|User score)\s+([\d.]+)\s+out of\s+(\d+)", re.I)


def _mc_score(card: str, *, is_critic: bool) -> tuple[float | None, float | None]:
    """The card's score and the scale it is on.

    The score lives in a title/aria-label like "Metascore 100 out of 100" or "User score 10 out
    of 10", which also tells us the scale -- so it is read from the attribute first and from the
    element text only when the attribute is not there, where the scale has to be assumed from
    which page we are on.
    """
    holder = first(card, lambda _tag, attrs: "score" in attrs.get("title", "").lower()
                   or "score" in attrs.get("aria-label", "").lower())
    if holder is not None:
        raw = holder.get("title") or holder.get("aria-label")
        match = _MC_SCORE_TITLE.search(raw)
        if match:
            try:
                return float(match.group(2)), float(match.group(3))
            except ValueError:
                pass
    inner = first(card, _class_contains("c-siteReviewScore", "ReviewScore"))
    value = _int(inner.text) if inner is not None else None
    return (float(value) if value is not None else None), (100.0 if is_critic else 10.0)


def _mc_from_html(content: bytes, is_critic: bool) -> list[ParsedReview]:
    """Metacritic renders reviews server-side inside `data-testid` cards.

    The Tailwind class names on those cards change constantly; the `data-testid` attributes have
    been stable, so they are the primary selector and the class names are only a fallback.
    """
    page = content.decode("utf-8", "replace")
    out: list[ParsedReview] = []

    cards = collect(page, _testid("review-card"))
    if not cards:
        cards = collect(page, _class_contains("c-siteReview", "review"))
    for card in cards:
        quote = (first(card.html, _testid("review-quote-text"))
                 or first(card.html, _class_contains("line-clamp"))
                 or first(card.html, _class_contains("review_body"))
                 or first(card.html, lambda tag, _attrs: tag == "blockquote"))
        body = clean_text(quote.text if quote else "")
        if not body:
            continue

        score, scale = _mc_score(card.html, is_critic=is_critic)

        header = first(card.html, _testid("review-card-header"))
        href = header.get("href") if header else ""
        publication = author = None
        label = clean_text(header.text if header else "")
        # The header text is "<score> <name>"; strip the leading score.
        if label and score is not None:
            label = re.sub(r"^\s*[\d.]+\s*", "", label).strip()
        if "/publication/" in href:
            publication = label or None
        elif "/user/" in href:
            author = label or None
        elif is_critic:
            publication = label or None
        else:
            author = label or None

        byline = first(card.html, _class_contains("review-footer__author",
                                                  "movie-review-footer__author"))
        if byline:
            author = re.sub(r"^\s*By\s+", "", clean_text(byline.text)).strip() or author

        date = first(card.html, _testid("review-card-date"))
        link = (first(card.html, lambda tag, attrs: tag == "a"
                      and "review-link" in attrs.get("class", ""))
                or first(card.html, lambda tag, attrs: tag == "a"
                         and attrs.get("href", "").startswith("http")))

        out.append(ParsedReview(
            source="metacritic", body=body,
            author=author, author_kind="critic" if is_critic else "user",
            publication=publication,
            rating_raw=(f"{score:g}" if score is not None else None),
            rating_scale=scale,
            rating_norm=_norm(score, scale) if scale else None,
            created_date=iso_date(clean_text(date.text) if date else None),
            url=link.get("href") if link else None,
        ))
    return out


# ---------------------------------------------------------------------------
# Wikipedia critical-reception prose
# ---------------------------------------------------------------------------

# Only sections that actually carry evaluative prose.
#
# "release" and "accolades" were here and are not reviews at all: 2,354 rows of premiere dates,
# certificates and distribution deals, and 1,207 rows enumerating nominations - 29.9% of
# everything this source produced, none of it a judgement about the film. "legacy" went with
# them; it is mostly later-work and parody trivia.
RECEPTION_SECTIONS = ("critical reception", "reception", "critical response", "critical reaction",
                      "themes", "analysis", "style")

# Below this a section is a stub sentence rather than reception prose, and the corpus's own floor.
# It is NOT decision 335's fifty: that one is a total across a title's reviews, measured in
# `derive/gate.py` off the stored `word_count`, and this one is a character floor on one section.
MIN_SECTION_CHARS = 200


def parse_wikipedia_reception(payload: Any) -> list[ParsedReview]:
    """Treat the article's reception prose as one long critic 'review'.

    It is not a review in the platform sense, but it is exactly what the aspect extractor wants:
    dense, evaluative, third-party language about the film with none of the plot recap that
    dominates user reviews. It is also, for a title nobody else has reviewed yet, frequently the
    second source §8 stage 4's gate needs -- which is why `parse.parse_wikipedia` deliberately
    excludes these sections from its own craft budget rather than storing them twice.
    """
    pages = ((payload or {}).get("query") or {}).get("pages") or [] if isinstance(
        payload, Mapping) else []
    if not pages:
        return []
    page = pages[0] if isinstance(pages, list) else next(iter(pages.values()))
    sections = split_sections(page.get("extract") or "")
    out = []
    for name in RECEPTION_SECTIONS:
        body = sections.get(name)
        if not body or len(body) < MIN_SECTION_CHARS:
            continue
        out.append(ParsedReview(
            source="wikipedia", body=clean_text(body),
            external_id=f"{page.get('pageid')}:{name}",
            author_kind="critic", publication="Wikipedia",
            headline=name.title(), language="en",
            meta={"section": name},
        ))
    return out


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def parse_document(source: str, kind: str, content: bytes) -> list[ParsedReview]:
    """One raw document to its reviews, and NEVER an exception.

    `derive/parse.parse_document`'s docstring argues the blanket `except` and the argument is the
    same one: a document already in the raw store cannot be improved by a retry, so a parser that
    raised on changed markup would turn decision 334's note into a stage that fails for ever. A
    source whose markup moved yields zero reviews, stage 4's gate then finds the title thin, and
    decision 336 parks it with a deadline -- which is the correct state for a title waiting on a
    parser fix, and it is a state the board can show.
    """
    try:
        if source == "tmdb":
            return parse_tmdb(json.loads(content.decode("utf-8", "replace")))
        if source == "trakt":
            return parse_trakt(json.loads(content.decode("utf-8", "replace")))
        if source == "metacritic":
            return parse_metacritic(content, kind)
        if source == "wikipedia":
            return parse_wikipedia_reception(json.loads(content.decode("utf-8", "replace")))
    except Exception:                                      # noqa: BLE001
        return []
    return []
