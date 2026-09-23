"""The craft supplement -- the material the base pack throws away. Spec v2.1 §8 stage 5, §14 risk 2.

§8 stage 5's one line names this half as well as the pack: "ported packs.py (interleaving, caps,
norm()) + craft supplement (wikipedia craft sections; NOTE: the RT critic-blurb pool is a frozen
2020 dataset -> genuinely new releases get thinner sound/visual facets -- known gap, mitigation:
wiki sections + metacritic critic excerpts, revisit if coverage measures poor)" (`spec:377-381`).
Both halves ship here, and the gap is MEASURED against this install below rather than repeated as
a warning (decision 391).

Everything between the two rules is `mdc/dna/craft.py`'s own module docstring, carried across
verbatim at the corpus's line width for the reason `packs.py` gives about its own: the paragraphs
are counts, and the counts are what say this module is worth having at all.

--- `mdc/dna/craft.py:3-36`, verbatim -------------------------------------------------------

A pack (:mod:`mdc.dna.packs`) is a plot summary plus a spread of reviews above
a 50-word floor.  That floor and the plot-only use of Wikipedia were both right
for the facets extraction was strongest on, and they are exactly why the craft
facets are thin.  Measured on the 824-title owned library, 2026-08-25:

* **1,861 title x facet cells are empty** in the extracted tier, 1,241 of them
  still empty after projected tags merge in.  The concentration is
  ``sound_score`` (325 / 260) and ``visual_style`` (162 / 71), then
  ``setting_place``, ``register_audience`` and ``character_dynamics``.
* **The packs do not contain the evidence.**  Parasite's 44k-char pack carries
  no discussion of Jung Jae-il's score at all; its Wikipedia article, already
  on disk, describes it as "minimalist piano pieces, punctuated with light
  percussion".  ``build_pack`` keeps the plot section and discards the rest of
  that article.
* **77,395 of the 77,420 Rotten Tomatoes critic reviews over the library are
  dropped by the word floor** (median 25 words).  They are the only large pool
  of *critic* prose in the corpus — what survives the floor is mostly user
  reviews (trakt, tmdb, metacritic users) — and critic blurbs are where
  cinematography and score get named.

So this module adds material rather than changing how any of it is read.

**The supplement is appended, never interleaved.**  That is a correctness
requirement, not a style choice: :mod:`mdc.dna.store` verifies every quote as a
substring of the title's pack and re-verifies *every prior pass* on each ingest
(a title's DNA is the union of all passes that ever covered it).  An augmented
pack that keeps the original text as an exact prefix leaves all previously
verified quotes verifiable; one that rewrote the body would drop them all as
``quote_unverified`` on the next ingest.

Augmentation is idempotent: :data:`SENTINEL` marks the supplement, and
re-augmenting replaces it rather than stacking a second copy.

Read-only on the database, like :mod:`mdc.dna.packs`.

--- end of the ported text; everything below is this port's ---------------------------------

WHAT THIS INSTALL PRODUCES TODAY IS NOTHING, AND THAT IS THE MEASUREMENT (decision 391). Both
inputs are absent, for two different reasons, and neither is a defect in this module:

  * THE WIKI HALF HAS NO DOCUMENT TO READ. `wiki_craft` wants `raw_document` rows with
    `source = 'wikipedia'` and `kind = 'article'`, and this install holds zero `raw_document`
    rows of ANY source: the table is `0024_acquisition.sql`'s, the only writers in the tree are
    `acquire/fetch.py` and `acquire/rawstore.py`, and no importer writes one -- so a corpus
    bundle cannot bring them. §8 stage 2's `wikipedia:article` fetcher is M5.3's and has not
    landed. Do not read the fixture's two `title_meta` rows whose SOURCE is `'wikipedia'`
    (`make_bundle.py:72,75`, titles 1 and 2, `plot_short` only) as evidence to the contrary:
    those are parsed fields in a jsonb payload and this module reads article bytes.
  * THE RT HALF HAS NO BLURBS TO READ. The fixture bundle ships exactly three
    `review_store.review` rows (`make_bundle.py:552-558`): metacritic at 10 words, trakt at 6,
    letterboxd at 1. None is from `rottentomatoes`, so `rt_critics` matches none of them; every
    one is under the pack's own 50-word floor, so none reaches the base pack either; and two of
    the three are under `RT_MIN_WORDS` as well.

So on the shipped fixture `supplement()` returns `""` for all eight titles and `augment()` hands
back the base pack byte for byte. `test_dna_packs.py` therefore inserts its own rows rather than
skipping the assertions: a fixture that cannot express the input is a fact about the fixture, and
the code path still has to be proved.

AND THE GAP §8 STAGE 5 NAMES IS REAL HERE AND UNMEASURABLE HERE AT THE SAME TIME. The frozen
2020 RT pool means genuinely new releases get thinner sound and visual facets; §14 risk 2 says
what to do about it -- "New-release DNA thinness (craft-blurb gap, review-accrual lag) --
mitigations in §8 stage 4/5; measure facet coverage of post-2025 titles" (`spec:498`). This
install cannot run that measurement yet and the honest statement of why is arithmetic: the
newest title the fixture bundle ships is from 2022 (`make_bundle.py`'s TITLES, Severance and The
Bear), so there is no post-2025 title here to measure the coverage of. The first titles that
qualify arrive through §8's own pipeline, which is what makes `dna.coverage.facet_coverage`
(decision 390) the instrument rather than this docstring.

NAMED PORT CHANGES, all of them about where a byte lives rather than about what is selected:

  1. THE WIKIPEDIA ARTICLE IS FOUND THROUGH THE ACQUISITION TASK, NOT UNDER THE TITLE ID. The
     corpus reads `raw_document WHERE entity_key = str(title_id)`; this app files a document
     under the acquisition task's key (`jellyfin:<id>`, `title:<id>`, `tmdb:<id>`), which may
     predate the title row entirely under decision 322. `acquire/board.py`'s
     `documents_for_title` already states the join that turns one into the other, and this
     module uses the same one rather than inventing a second custody rule -- a reader that
     guessed `entity_key = title_id` would find nothing on a real install and report an honest
     empty list, which is the failure that looks like data and not like a bug.
  2. THE BYTES COME BACK THROUGH `rawstore.read_json`, not through `gzip.open` on a path. The
     store owns its own compression and its own "is this file still the document this row
     names" check (`rawstore.read` hashes what it decompressed), and decision 345 keeps
     `/data/raw` off the backend container, so a path is exactly what this module must not hold.
     `EOFError` joins the corpus's five caught exceptions because this app's read raises it on a
     truncated member where the corpus's silently returned an empty string.
  3. THE RT BLURBS ARE BOUNDED ABOVE BY THE PACK'S OWN FLOOR. The corpus takes every critic
     review at or above `RT_MIN_WORDS` and lets the 25 of 77,420 that also clear the pack floor
     duplicate; decision 391 states the supplement as what sits BELOW the floor, and the bound
     is what makes that true. A review carried in both halves would be quotable from two places
     in one pack and would inflate the interleave's spread without adding any evidence.
     `is_critic` does the work the corpus's `author_kind = 'critic'` did, and NULL is not a
     critic in either -- the corpus coalesces to `''`, this reads the column as a bare
     predicate, and both exclude the unknown.
  4. THE THREE FALLBACKS ARE NOT PORTED, and this is the only place they are recorded.
     `_soundtrack_path` (a standalone Wikipedia soundtrack article) and `_score_prose` (a
     retrieved excerpt, with `_slug` to mark its site) read `raw_document` rows of kinds
     `soundtrack` and `score_prose` that corpus-side SCRIPTS produce -- `scripts/dna/
     fetch_soundtracks.py` and `ingest_score_prose.py` -- and no §8 stage fetches either. A
     reader for a document kind nothing in this app can write is not a port, it is a guess about
     a milestone that does not exist. The corpus's own measurement says what is being left on
     the table and it is small: 15 of the 183 titles missing a Music section had a standalone
     soundtrack article, and the hypothesis behind the other 168 was "mostly wrong -- those
     titles simply are not written about".
  5. `augment` takes and returns pack TEXT instead of reading and writing a file, which is the
     same seam change `packs.render_pack` makes and for the same reason: there are no pack
     files here, `dna_pack` and the raw store are where a pack lives, and a function of text is
     a function the no-DB half of the test plan can call. The two rules the corpus's `augment`
     exists to keep are kept exactly, in `apply_supplement`. The bare `list` and `tuple` return
     annotations are tightened to what they hold; nothing about what is returned changed.

THE SUPPLEMENT IS APPENDED, NEVER INTERLEAVED, AND RE-AUGMENTING REPLACES IT. Those are the two
rules, and the ported paragraphs above argue the first one: a pack that keeps the original text
as an exact PREFIX leaves every quote that ever verified against it still verifiable, and one
that rewrote the body would drop them all as `quote_unverified` on the next pass. `SENTINEL` is
what makes the second true, and it is the whole of the idempotence: `base_pack` cuts at the
sentinel, so augmenting twice replaces the supplement instead of stacking a second copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg

from spielplan.acquire import rawstore
from spielplan.dna import packs

# The line that separates the original pack from everything this module adds.
# Ingest never needs it — it exists so augmentation can be run twice.
SENTINEL = "[craft-supplement]"

# Wikipedia headings worth carrying, grouped by the marker they get in the
# pack.  Matched case-insensitively against the heading text at any level, so
# `=== Music ===` under `== Production ==` is found without walking the tree.
# Development, Writing, Casting, Marketing and the Reception subtree are
# deliberately absent: they describe how the film came to exist or how it
# landed, not what it is like, and they are the bulk of the article.
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

# Named port change 1: a document belongs to a title when it is filed under the key of a task
# that belongs to that title. `acquire/board.py`'s `documents_for_title` is the same join, and
# its docstring is where the seam is argued for every stage that writes into the store.
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

# Named port change 3. `word_count` is a property of the text and not one of §4.1 rule 2's
# weights, so bounding it is not a cut on evidence; the upper bound is the pack's own floor, so
# no review can be carried by both halves. `ORDER BY word_count DESC, id` is `packs._REVIEWS`'s
# tiebreak for `packs._REVIEWS`'s reason: a tie that reorders between runs changes which blurbs
# survive `RT_MAX`, which changes the pack text, which changes its sha.
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
    # `str(title_id)`, which is `acquire/board.py`'s own spelling at its call site: the
    # column is text and the cast is on the placeholder, so asyncpg types the parameter
    # as text and an int is refused rather than coerced.
    return await conn.fetchval(_WIKI_DOC, str(title_id))


async def _extract(conn: asyncpg.Connection, doc_id: int) -> str:
    """The plaintext extract out of a stored MediaWiki response, or "".

    The shape is `action=query&prop=extracts&explaintext=1`, which is what `mdc/sources/
    wikipedia.py:148` asks for and what `sections()` below is written to slice -- the extract
    carries `== Heading ==` markers rather than HTML. M5.3's fetcher owes the app that shape;
    anything else reads as a title with no craft prose, which is the outcome a large part of any
    library has anyway.

    Every failure is the empty string and none of them is raised. The supplement is ADDITIVE, so
    a damaged or unexpected document must cost this title its craft sections and must not cost
    the pack its build -- which is the corpus's own argument for an empty list being "the honest
    outcome for a large part of the library".
    """
    try:
        doc = await rawstore.read_json(conn, doc_id)
        return doc["query"]["pages"][0].get("extract") or ""
    except (OSError, ValueError, KeyError, IndexError, TypeError, EOFError):
        return ""


def sections(text: str) -> list[tuple[str, str]]:
    """Slice a plaintext Wikipedia extract into (heading, body) pairs.

    A section runs to the next heading at the same level or shallower, so a
    parent carries none of its children's text and nothing is counted twice.
    """
    # THAT LAST SENTENCE IS THE CORPUS'S AND IT OVERSTATES WHAT THE LOOP DOES, which is worth
    # one line here because a reader who trusts it reads a bug into the port. `level2 <= level`
    # stops a section at the next SIBLING or ancestor, so a parent's body does carry its
    # children's, and a nested heading is also returned on its own -- `== Music ==` with a
    # `=== Songs ===` under it yields both, and the child's text sits inside the parent's. It
    # does not reach the caller and that is why it came across exactly as it stood: `wiki_craft`
    # takes ONE section per marker, the first that matches a group's names, so a parent and a
    # child in the same group collapse to the parent, and a parent in no group (`Production`,
    # `Reception`) is never selected at all. [ported behaviour, deliberately not repaired]
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
    """Craft-bearing sections of a title's Wikipedia article, as (marker, text).

    Empty list when there is no article on disk or it carries none of them —
    which is the honest outcome for a large part of the library, and the reason
    this stage is measured before any web fetching is considered.
    """
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

    Ordered as the base pack orders reviews, so the cap keeps the same kind of material the rest
    of the pack keeps. No craft-keyword preference is applied: selecting for the words we hope to
    find is how a source stops being evidence.
    """
    # SERIES GET NONE.  The Kaggle source is a *movies* dataset
    # ("rotten-tomatoes-movies-and-critic-reviews-dataset"), matched to titles
    # by RT link, so a series sharing a slug with a film collects that film's
    # reviews: title 8746 is HBO's Euphoria (2019) and carries 14 notices for
    # the 2017 Vikander/Green film of the same name.  11 series corpus-wide are
    # affected, 1 of them owned.  The base pack never showed this because its
    # 50-word floor dropped every RT blurb; admitting them surfaced it.
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
    """The pack without any supplement — the exact prefix quotes verified against."""
    i = text.find("\n" + SENTINEL)
    return text if i < 0 else text[:i]


def apply_supplement(text: str, sup: str) -> str:
    """`text` with `sup` as its only supplement. Idempotent, and a prefix-preserving append.

    Both of this module's rules live in this one line, which is why it is one line and why it
    cuts at the sentinel before it appends rather than trusting the caller to hand over a base
    pack. An empty supplement is not a no-op: it STRIPS a supplement that is already there, which
    is what "re-augmenting replaces it" means for a title whose article has since been deleted.
    """
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

    Takes the pack text rather than rebuilding it, which is the corpus's own rule stated for a
    store instead of a directory: "so the original text is an exact prefix whatever else has
    changed in the database since". Rebuilding here would re-run the caps against today's reviews
    and could move the base, and every quote verified against the old base would go with it.
    """
    base = base_pack(text)
    sup, wiki_chars, n_sec, n_rt = await supplement(conn, title_id)
    out = apply_supplement(text, sup)
    info = CraftInfo(title_id=title_id, base_chars=len(base), wiki_chars=wiki_chars,
                     n_sections=n_sec, n_rt=n_rt, chars=len(out), sha=packs.sha(out))
    return out, info
