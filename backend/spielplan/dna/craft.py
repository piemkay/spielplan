"""The craft supplement: Wikipedia craft sections and short RT critic blurbs the base pack drops.

Appended after `SENTINEL`, never interleaved, so the base pack stays an exact prefix and every
previously verified quote still verifies. Re-augmenting replaces the supplement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg

from spielplan.acquire import rawstore
from spielplan.dna import packs

# Separates the original pack from the supplement, so augmentation can be run twice.
SENTINEL = "[craft-supplement]"

# Headings worth carrying, matched case-insensitively at any level. Development, Casting and
# Reception are left out: they describe how the film came to be, not what it is like.
CRAFT_SECTIONS: tuple[tuple[str, frozenset], ...] = (
    ("music", frozenset({
        "music", "soundtrack", "score", "songs", "sound", "sound design",
        "music and soundtrack", "musical numbers", "sound and music"})),
    ("filming", frozenset({
        "filming", "cinematography", "photography", "principal photography",
        "filming locations", "locations", "shooting"})),
    ("design", frozenset({
        "design", "production design", "visual effects", "art direction",
        "costume design", "costumes", "animation", "special effects",
        "effects", "makeup", "visual style"})),
    ("editing", frozenset({"editing", "post-production", "postproduction"})),
    ("setting", frozenset({"premise", "setting", "synopsis"})),
)

MAX_SECTION_CHARS = 2500      # per section; Music sections run long on musicals
MAX_WIKI_CHARS = 7000         # total across sections, per title
RT_MAX = 36                   # critic blurbs admitted per title
RT_MIN_WORDS = 12             # below this a blurb is a verdict, not a reading
RT_MAX_CHARS = 400

_HEADING = re.compile(r"^(=+) ?(.+?) ?=+$", re.M)

# A document belongs to a title via its acquisition task's key (`acquire/board.py`).
_WIKI_DOC = """
SELECT d.id
  FROM raw_document d
 WHERE d.source = 'wikipedia' AND d.kind = 'article' AND d.ok
   AND d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.fetched_at DESC, d.id DESC
 LIMIT 1
"""

# Bounded above by the pack's own floor, so no review is carried twice; `id` breaks ties so
# the pack text (and its sha) is stable.
_RT = """
SELECT body
  FROM review_store.review
 WHERE title_id = $1 AND source = 'rottentomatoes' AND is_critic
   AND word_count >= $2 AND word_count < $3
 ORDER BY word_count DESC, id
 LIMIT $4
"""


@dataclass
class CraftInfo:
    title_id: int
    base_chars: int
    wiki_chars: int
    n_sections: int
    n_rt: int
    chars: int
    sha: str

    @property
    def added(self) -> int:
        return self.chars - self.base_chars


async def _wiki_doc(conn: asyncpg.Connection, title_id: int) -> int | None:
    """The newest good Wikipedia article filed under one of this title's acquisition tasks."""
    # The column is text; an int is refused rather than coerced.
    return await conn.fetchval(_WIKI_DOC, str(title_id))


async def _extract(conn: asyncpg.Connection, doc_id: int) -> str:
    """The plaintext extract out of a stored MediaWiki response, or "".

    Every failure is "": the supplement is additive and must never cost the pack its build.
    """
    try:
        doc = await rawstore.read_json(conn, doc_id)
        return doc["query"]["pages"][0].get("extract") or ""
    except (OSError, ValueError, KeyError, IndexError, TypeError, EOFError):
        return ""


def sections(text: str) -> list[tuple[str, str]]:
    """Slice a plaintext Wikipedia extract into (heading, body) pairs."""
    # A parent's body does include its children's (the corpus docstring overstated this); harmless,
    # since `wiki_craft` takes one section per marker.
    marks = [(m.start(), m.end(), len(m.group(1)), m.group(2))
             for m in _HEADING.finditer(text)]
    out = []
    for i, (_start, end_of_head, level, head) in enumerate(marks):
        end = len(text)
        for start2, _e2, level2, _h2 in marks[i + 1:]:
            if level2 <= level:
                end = start2
                break
        out.append((head, text[end_of_head:end]))
    return out


async def wiki_craft(conn: asyncpg.Connection, title_id: int) -> list[tuple[str, str]]:
    """Craft-bearing sections of a title's Wikipedia article, as (marker, text); often empty."""
    doc_id = await _wiki_doc(conn, title_id)
    if doc_id is None:
        return []
    text = await _extract(conn, doc_id)
    if not text:
        return []

    secs = sections(text)
    out = []
    budget = MAX_WIKI_CHARS
    for marker, names in CRAFT_SECTIONS:
        for head, body in secs:
            if head.strip().lower() not in names:
                continue
            body = packs.clean(body)[:MAX_SECTION_CHARS][:budget]
            if len(body) < 80:      # a heading with a hatnote and nothing else
                continue
            out.append((marker, body))
            budget -= len(body)
            break               # one section per marker, the first that matches
        if budget <= 0:
            break
    return out


async def rt_critics(conn: asyncpg.Connection, title_id: int) -> list[str]:
    """Short Rotten Tomatoes critic blurbs, which the pack's word floor drops.

    No craft-keyword preference: selecting for hoped-for words stops a source being evidence.
    """
    # Series get none: the RT dataset is movies-only and matched by slug, so a series can inherit a
    # same-named film's reviews (Euphoria 2019).
    kind = await conn.fetchval("SELECT kind FROM title WHERE id = $1", title_id)
    if kind == "series":
        return []
    rows = await conn.fetch(_RT, title_id, RT_MIN_WORDS, packs.MIN_WORDS, RT_MAX)
    out = []
    for row in rows:
        t = packs.clean(row["body"])[:RT_MAX_CHARS]
        if t:
            out.append(t)
    return out


def base_pack(text: str) -> str:
    """The pack without any supplement: the exact prefix quotes verified against."""
    i = text.find("\n" + SENTINEL)
    return text if i < 0 else text[:i]


def apply_supplement(text: str, sup: str) -> str:
    """`text` with `sup` as its only supplement. Idempotent; an empty `sup` strips an existing one."""
    base = base_pack(text)
    return base if not sup else base.rstrip("\n") + "\n\n" + sup


async def supplement(conn: asyncpg.Connection, title_id: int) -> tuple[str, int, int, int]:
    """Build the block to append.  Returns (text, wiki_chars, n_sections, n_rt)."""
    wiki = await wiki_craft(conn, title_id)
    rt = await rt_critics(conn, title_id)
    if not wiki and not rt:
        return "", 0, 0, 0

    body = [
        SENTINEL,
        "[note] Further source material for this same title: encyclopaedia "
        "sections on how it was made, and short critic notices. Quote from "
        "these exactly as from any block above.",
        "",
    ]
    for marker, text in wiki:
        body += [f"[wiki:{marker}]", text, ""]
    for n, text in enumerate(rt, 1):
        body += [f"[rtcritic:{n}]", text, ""]
    return "\n".join(body), sum(len(t) for _m, t in wiki), len(wiki), len(rt)


async def augment(
    conn: asyncpg.Connection, title_id: int, text: str
) -> tuple[str, CraftInfo]:
    """One title's pack with its craft supplement on the end, and what that cost.

    Takes the pack text rather than rebuilding it, so the base stays byte-identical.
    """
    base = base_pack(text)
    sup, wiki_chars, n_sec, n_rt = await supplement(conn, title_id)
    out = apply_supplement(text, sup)
    info = CraftInfo(title_id=title_id, base_chars=len(base), wiki_chars=wiki_chars,
                     n_sections=n_sec, n_rt=n_rt, chars=len(out), sha=packs.sha(out))
    return out, info
