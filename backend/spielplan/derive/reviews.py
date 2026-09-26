"""§8 stage 3's review extraction: the rows stage 4's gate counts and stage 5's pack is built of.

Forgiving by design: each source yields what it can. No BeautifulSoup (a stdlib scan instead), and
no filtering or counting here: the gate's fifty words is measured off the stored rows.
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

# Imported although private: one definition keeps `is_critic` honest.
from spielplan.importer.reviews import _AUTHOR_KIND, REVIEW_SOURCE, repair_mojibake
from spielplan.sources._htmlutil import clean_text, next_data, walk


@dataclass
class ParsedReview:
    """One review, in the corpus's own field names; `review_row` is the one crossing to ours."""

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

        The source's own id, else a hash of the author and the first 400 body characters.
        """
        if self.external_id:
            return str(self.external_id)
        seed = f"{self.author or ''}|{self.body[:400]}"
        return "h" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:20]


def review_row(parsed: ParsedReview, title_id: int) -> dict[str, Any]:
    """`parsed` as a `review_store.review` row, through the import's own column map.

    `word_count` is a generated column and must not be written. `body` gets `repair_mojibake` after
    `clean_text`'s per-run repair, deliberately: the second pass finishes doubly-encoded bodies.
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

# The source dataset's "no date" placeholders.
_SENTINEL_DATES = {"1800-01-01", "0000-00-00", "1900-01-01"}


def iso_date(value: str | None) -> str | None:
    """Normalise a review date to YYYY-MM-DD, or drop it (Metacritic writes "Feb 23, 2023")."""
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


# TMDB (JSON)


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


# Trakt (JSON)


def parse_trakt(payload: Any) -> list[ParsedReview]:
    """Trakt comments, which carry the commenter's own /10 rating."""
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


# the markup scan that replaces BeautifulSoup

# Void elements have no end tag; depth tracking by tag name must not wait for one.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "param", "source", "track", "wbr"})


@dataclass(frozen=True)
class Element:
    """One matched element: its tag, its attributes, its inner markup and its text.

    `text` joins data nodes with a single space, as bs4's `get_text(" ")`.
    """

    tag: str
    attrs: Mapping[str, str]
    html: str
    text: str

    def get(self, name: str) -> str:
        """An attribute's value, empty when absent."""
        return self.attrs.get(name, "")


class _Collector(HTMLParser):
    """Collect the OUTERMOST elements matching `match`, so a nested card is not a second review."""

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
        # A truncated document still yields its open elements.
        while self._open:
            self._finish(len(page))
        return self.found

    def _finish(self, end: int) -> None:
        tag, attrs, start = self._open.pop()
        text = self._text.pop()
        if self._open:
            # A child's text is its parent's too; headers read "<score> <name>" only because of this.
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
        # Close the nearest open element with this tag; Metacritic's markup has stray end tags.
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


# Metacritic


def parse_metacritic(content: bytes, view: str) -> list[ParsedReview]:
    """`view` says which page this is, and therefore which scale its scores are on (100 vs 10)."""
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
    """The card's score and its scale, from the title/aria-label attribute first."""
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


# Wikipedia critical-reception prose

# Only sections that actually carry evaluative prose ("release" and "accolades" were not reviews).
RECEPTION_SECTIONS = ("critical reception", "reception", "critical response", "critical reaction",
                      "themes", "analysis", "style")

# A character floor on one section; not decision 335's word total.
MIN_SECTION_CHARS = 200


def parse_wikipedia_reception(payload: Any) -> list[ParsedReview]:
    """Treat the article's reception prose as one long critic 'review'.

    Often the second source stage 4's gate needs; `parse_wikipedia` excludes these sections.
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


# dispatch


def parse_document(source: str, kind: str, content: bytes) -> list[ParsedReview]:
    """One raw document to its reviews, and NEVER an exception: changed markup yields zero reviews."""
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
