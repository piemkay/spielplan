"""Shared helpers for the scraped sources' markup. Imports no transport, and that is the point.

Spec v2.1 §8 stage 2 (`rt:page`, `metacritic:page`) and §8's preamble ("All fetched bytes land
in the app's own raw store, so re-parsing is free forever", `spec:398`); §4.1 rule 8 on never
"cleaning" non-ASCII; decision 373.

PORT VERDICT: **ported verbatim in substance** from `mdc/sources/_htmlutil.py` (225 lines) -
`next_data` (`:109-117`), `ld_json` (`:119-127`), `walk` (`:129-145`), `unescape` (`:158-177`),
`fix_mojibake` (`:185-213`) and `clean_text` (`:215-225`), with their regexes and their
arguments. Three cosmetic differences and no behavioural one: the imports are modernised, the
`html: bytes` parameter is renamed `page` where it shadowed the `html` module this file also
uses, and every non-ASCII literal in the CODE is spelled as an escape, for the reason the
corpus already spells `_MOJIBAKE` that way - one of them is U+FFFD, four are invisible C1
controls and the rest is punctuation a cp1252 console cannot print. The DOCSTRINGS keep the
corpus's examples as characters, because in this one module the character is the subject and
`importer/reviews.py:44` already carries the same three markers the same way.

The corpus's own header says what these are for and it is still true here: "The HTML sites are
the fragile part of the crawl: their markup changes, their URL patterns change, and any one of
them can start returning a challenge page. The strategy is the same everywhere - fetch a set of
views per title, treat each view as independently optional, store whatever bytes came back, and
let the (re-runnable) parser worry about structure later."

THIS MODULE IS A DEPENDENCY OF THE PARSERS AND NOT OF THE FETCHERS, and it imports nothing that
opens a socket. `mdc/parse/titles.py` imports `ids` and `_htmlutil` and no transport at all, and
decision 373 records that as the half of M5.1's guard worth keeping, because it is the whole of
"re-parsing is free forever": a parser that could reach the network is a parser whose next bug
costs another crawl of somebody else's host. `test_sources_base.py` reads this file with `ast`
and fails on an import of `httpx` or of `acquire.fetch`.

SO THE FETCH HALF OF THE CORPUS MODULE IS NOT HERE. `fetch_views` (`:38-97`), `View` (`:29-35`)
and `MIN_HTML_BYTES` (`:25`) are transport - they take a `Ctx`, call `ctx.fetcher.get` and write
into the raw store - and whatever ports them ports them beside the adapters, where the guard
above does not apply and should not. A later reader looking for them here finds this paragraph
rather than a missing name.

ONE REPAIR LIVES IN THIS TREE TWICE, ON PURPOSE. `importer/reviews.repair_mojibake` (`:76-98`)
is the app's WHOLE-STRING cp1252-over-UTF-8 round trip, written for the bundle's review bodies;
`fix_mojibake` below is the corpus's PER-RUN one, and its docstring is the measurement that
separates them - "Metacritic serves bodies where one byte of the pair was already lost ...
and a whole-string attempt fails on those and gives up on the recoverable parts of the same
review too". That is exactly this module's input. The two are not interchangeable: this one cleans
text nodes scraped out of markup, the importer's repairs a row shipped in `reviews.sqlite`. Both
honour §4.1 rule 8 the same way, by attempting nothing unless the string already carries the
tell-tale shape and keeping the attempt only when it round-trips cleanly - "São Paulo" and "Ça va"
are left exactly as they arrived.

AND ONE STRING DOES GET BOTH, WHICH THIS PARAGRAPH USED TO FORBID. It read "must not both run over
one string", and `derive/reviews.review_row` ran the importer's over a body `clean_text` had
already repaired - every scraped review in §8 stage 3, not an edge. M5.3's first review cycle
measured the composition instead of choosing between the sentence and the code: 200,000 randomly
mangled strings, each compared against its own ground truth, gave 8,278 recovered by the second
pass and NONE damaged by it. What is left for the second pass is the DOUBLY-encoded body, which
the per-run pass reduces to a singly-encoded one and has no second pass of its own to finish. So
the rule is now about REIMPLEMENTATION and not about composition: there may be no third repair,
and neither of these two may be rewritten into the other, but a scraped review body goes through
this one and then the importer's, on purpose. [M5.3 review cycle 1, m53-c1-slug-05]

"AND NONE DAMAGED BY IT" WAS A PROPERTY OF THAT GENERATOR AND NOT OF THE TWO FUNCTIONS, and "the
same way" above is true of the shape of the two guards and false of what they test. This one
fires on A-circumflex, A-tilde, A-ring or Eth followed by a CONTINUATION character, 0x80-0xBF; the
importer's fires on the bare marker. So a marker immediately before a character cp1252 encodes
into 0x80-0x9F - the ellipsis, the dashes, the bullet, the trade mark sign, and whatever else in
that block `clean_text` does not fold to ASCII - is declined here and attempted there, and when
every other character of the body is one cp1252 holds and the whole decodes as UTF-8, the string
is re-encoded around it: A-circumflex and an ellipsis become the invisible control U+0085, A-tilde
and the trade mark sign become U-grave. Re-measured in cycle 2 with two seeds of 300,000 synthetic
cases each - random words over Latin, accented Latin, that punctuation, Central European, CJK and
Cyrillic, each fed clean, singly and doubly mangled - the second pass recovered 50,905 and 50,819
and damaged 49 and 52, of which 29 and 31 were inputs carrying no mojibake at all. That ratio is
set by how often a generator puts a marker before that punctuation and says nothing about the
functions, which is how one cycle's generator measured none. What no generator sets is the
corpus's own raw store: over 172,885 real Metacritic review bodies parsed out of 11,811 of its
stored pages, this pass repaired 95 text nodes and the second pass changed not one body, in
either direction.

THE COMPOSITION STAYS, with its cost recorded rather than denied. It is the only thing in the tree
that finishes a doubly-encoded body; its damage needs a precondition the real bytes never met;
and the one way to close it from this side - widening the class here over that block - makes this
pass attempt strings it declines today in every scraped text node, titles and names as well as
review bodies, which is a change that owes a measurement of its own. The next author to widen
either guard re-measures rather than citing this paragraph, and `test_sources_base.py` pins the
damage beside the recovery so the paragraph cannot drift from the code unseen.
[M5.3 review cycle 2, m53-c2-moji-01]
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

# ---------------------------------------------------------------------------
# embedded-JSON extraction (Next.js / Nuxt / ld+json)
# ---------------------------------------------------------------------------

_NEXT_DATA = re.compile(
    rb'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON = re.compile(
    rb'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def next_data(page: bytes) -> Any | None:
    m = _NEXT_DATA.search(page)
    if not m:
        return None
    try:
        return json.loads(m.group(1).decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def ld_json(page: bytes) -> list[Any]:
    out = []
    for m in _LD_JSON.finditer(page):
        try:
            out.append(json.loads(m.group(1).decode("utf-8", "replace")))
        except json.JSONDecodeError:
            continue
    return out


def walk(obj: Any, predicate: Callable[[dict[str, Any]], bool]) -> Iterable[Any]:
    """Depth-first walk yielding every node matching ``predicate``.

    This is what makes the parsers survive markup churn: instead of a brittle path into a
    Next.js payload, we look for the *shape* of a review object anywhere in the tree.
    """
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if predicate(node):
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


_ENTITY = re.compile(r"&(#\d{2,6}|#[xX][0-9a-fA-F]{2,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")

# The unmistakable shapes of UTF-8 bytes that were decoded as Latin-1/cp1252: a C2/C3 lead byte
# followed by a continuation byte, or the trigram that any smart punctuation mark degrades into.
# Spelled with escapes because half of these code points are invisible C1 controls.
_MOJIBAKE = re.compile("[\u00c2\u00c3\u00c5\u00d0][\u0080-\u00bf]"
                       "|\u00e2\u20ac"        # the mangled apostrophe/quote family
                       "|\u00e3\u0192")       # doubly-mangled katakana


def unescape(s: str | None) -> str:
    """Decode HTML entities, including the doubly-escaped ones.

    Sources hand us `&apos;`, `&#x27;` and - where an escaping pass ran twice somewhere upstream
    - `&amp;#x27;`. `html.unescape` only peels one layer, so this repeats while the result still
    *looks* like an entity. Two passes is the cap: a third would start eating text that
    legitimately reads "&amp;" after one decode.
    """
    if not s:
        return ""
    for _ in range(2):
        if not _ENTITY.search(s):
            break
        s = html.unescape(s)
    return s


# Every character cp1252 can represent above U+007F - derived rather than typed out, since a
# third of them are invisible C1 controls. A mis-decoded byte is always one of these, so a run of
# them is exactly the span a repair attempt should cover, and the first character outside the set
# ends it.
_CP1252_HIGH = "".join(bytes([b]).decode("cp1252", "ignore")
                       for b in range(0x80, 0x100))
_CP1252_RUN = re.compile(f"[{re.escape(_CP1252_HIGH)}]+")


def fix_mojibake(s: str) -> str:
    """Repair text that was UTF-8 but got decoded as Latin-1/cp1252.

    ``Ã©`` for ``é``, ``Â°`` for ``°``, ``â€™`` for ``'``. The repair is the inverse round-trip -
    re-encode as cp1252, decode as UTF-8 - and it is applied **per run** rather than to the whole
    string, because some of this text is corrupt at the source rather than merely mis-decoded:
    Metacritic serves bodies where one byte of the pair was already lost ("dÃtail" for "détail"),
    and a whole-string attempt fails on those and gives up on the recoverable parts of the same
    review too.

    A run is kept only when it round-trips cleanly, so genuinely accented text - "São Paulo", "Ça
    va" - is left exactly as it is: those never form a valid UTF-8 pair. The whole-string guard
    means the attempt is only ever made on text that already carries the tell-tale shape.
    """
    if not s or not _MOJIBAKE.search(s):
        return s

    def repair(m: re.Match) -> str:
        run = m.group(0)
        try:
            fixed = run.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run
        return run if "\ufffd" in fixed else fixed

    return _CP1252_RUN.sub(repair, s)


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    s = fix_mojibake(s)
    s = s.replace("\xa0", " ").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


__all__ = [
    "clean_text",
    "fix_mojibake",
    "ld_json",
    "next_data",
    "unescape",
    "walk",
]
