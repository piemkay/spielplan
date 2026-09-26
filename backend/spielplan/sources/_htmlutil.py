"""Shared helpers for the scraped sources' markup. Imports no transport: parsers never fetch (373).

`fix_mojibake` (per run) and `importer/reviews.repair_mojibake` (whole string) both run on scraped
review bodies, in that order, on purpose; do not merge them or add a third.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

# embedded-JSON extraction (Next.js / Nuxt / ld+json)

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
    """Depth-first walk yielding every node matching ``predicate``; finds shapes, not brittle paths."""
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

# UTF-8 bytes decoded as Latin-1/cp1252. Escapes because several are invisible C1 controls.
_MOJIBAKE = re.compile("[\u00c2\u00c3\u00c5\u00d0][\u0080-\u00bf]"
                       "|\u00e2\u20ac"        # the mangled apostrophe/quote family
                       "|\u00e3\u0192")       # doubly-mangled katakana


def unescape(s: str | None) -> str:
    """Decode HTML entities, including doubly-escaped ones. Two passes max: a third eats literal "&amp;".
    """
    if not s:
        return ""
    for _ in range(2):
        if not _ENTITY.search(s):
            break
        s = html.unescape(s)
    return s


# Every cp1252 character above U+007F; a repair run spans exactly these.
_CP1252_HIGH = "".join(bytes([b]).decode("cp1252", "ignore")
                       for b in range(0x80, 0x100))
_CP1252_RUN = re.compile(f"[{re.escape(_CP1252_HIGH)}]+")


def fix_mojibake(s: str) -> str:
    """Repair text that was UTF-8 but got decoded as Latin-1/cp1252.

    ``Ã©`` for ``é``, ``â€™`` for ``'``. Applied per run, because some source text lost a byte of a pair
    and a whole-string attempt would give up on the recoverable parts. A run is kept only when it
    round-trips cleanly, so "São Paulo" is left alone (§4.1 rule 8).
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
