"""Source packs -- the text an extraction pass is allowed to read. Spec v2.1 §8 stage 5, §4.1.

§8 stage 5 is one line -- "ported packs.py (interleaving, caps, norm()) + craft supplement"
(`spec:381`) -- and this module is the first half of it. `norm()` is deliberately NOT here: it
lives alone in `dna/norm.py` for the reason that module argues at length, and `clean()` and
`sha()` are the pack builder's, so they are. The craft supplement is `dna/craft.py`.

Everything between the two rules below is `mdc/dna/packs.py`'s own module docstring, carried
across verbatim at the corpus's own line width so the quotation is visible as one. Its
measurements are the argument FOR the four caps rather than decoration on them, and a port that
kept the numbers and dropped the comparison behind them would leave the next reader nothing to
argue with the first time 60 reviews per title looks like a number somebody guessed.

--- `mdc/dna/packs.py:3-31`, verbatim ------------------------------------------------------

A pack is a plot summary plus a *spread* of reviews across sources (spec §4.1).
The spread matters: single-source input biases tone tags, and one site's
reviewer culture is not the film.

The caps here are measured, not guessed.  An A/B over ten titles
(reports/model-bakeoff.md and the ab_packsize run behind it) compared ≤20
reviews / ≤4 per source against ≤60 / ≤12:

    +17% tags overall
    setting_place coverage   4/10 -> 10/10 titles
    visual_style coverage    8/10 -> 10/10
    pacing_energy coverage   9/10 -> 10/10

Place evidence lives in review prose that the thin cap truncated away, and it
costs ~5.5k extra input tokens per title.  Do not lower these without re-running
that comparison.

**Series get one pack for the whole show** (decision 2026-08-20, 121 of the 839
owned titles).  Reviews may discuss different seasons, so the header says so and
tells the extractor to describe the show as a whole.  This works for most shows
and has one named failure mode - anthology formats like The White Lotus, where
any single `place` tag misrepresents two thirds of the show.  The prompt's
guidance is to tag only the invariants and let `structure.anthology` carry the
format; ``structure.anthology`` on a title is also the cheap detection signal
for which shows would need per-season DNA if that is ever built.

Read-only on the database.  Packs are content-hashed so a tag verified against
one pack can never be silently credited to a different one.

--- end of the ported text; everything below is this port's ---------------------------------

FOUR NAMED PORT CHANGES, because the app's schema is not the corpus's. Each is a change to where
a value is read from and none is a change to what the pack contains:

  1. THE PLOT COMES OUT OF `title_meta.payload`, NOT OFF TWO COLUMNS. `0003_content.sql:71-78`
     keeps `title_meta` as `(title_id, source, fetched_at, payload jsonb)` -- "one block = one
     droppable source", §4.1 -- and the payload carries the corpus's own column names, because
     `importer/meta._payload_columns` copies every shipped column but the two that become the
     key. So `plot_full` and `plot_short` are read as `payload ->> ...`, and the corpus's
     ORDER BY is kept exactly: LONGEST, not source-priority. `title.overview` is already the
     per-field resolved choice (`importer/meta.resolve_title_fields` walks `SOURCE_PRIORITY`),
     and it is the wrong value here -- a card wants the most TRUSTED source and a pack wants the
     most EVIDENCE, which is why this reads past the resolved column to the rows behind it.
  2. SEASON AND EPISODE COUNTS COME OUT OF THE SAME PAYLOAD. `title` carries neither column.
     They are per source here where the corpus had them on one row, so the largest any source
     claims is the one the header states: the header's job is to tell the extractor the scope of
     the thing it is reading about, and a stale source saying "1 season" over reviews of three
     is the failure that matters. See `_largest_count`.
  3. `review_store.review` HAS NO `helpful_yes` COLUMN. `0003_content.sql:239-251` is
     `(id, title_id, source, author, url, rating, published_at, is_critic, body, word_count)`
     with `word_count` GENERATED STORED, so the corpus's
     `ORDER BY COALESCE(helpful_yes,0) DESC, word_count DESC` becomes
     `ORDER BY word_count DESC, id`. The `id` tiebreak is not tidiness: reviews of equal length
     that reorder between two runs change which twelve survive `MAX_PER_SOURCE`, which changes
     the pack text, which changes `sha()` -- and every quote verified against the old text is
     then unverifiable against the new one with nothing in the log to say why.
  4. `title.name`, not `title.primary_title`.

AND ONE SEAM CHANGE, which is what makes the other four testable. The corpus's `build_pack` is a
sync sqlite3 function that interleaves its queries with its rendering; this one is async asyncpg
and splits at the fetch boundary, so `render_pack` is a pure function of rows and `build_pack` is
the four queries in front of it. The caps, the interleave, the plot cap and the series header can
therefore be asserted without a database, which is what M5.4-plan.md §6's "backend (no DB)" list
requires and what the corpus's shape does not permit at all. The 50-word floor stays in SQL where
the corpus put it and is asserted against Postgres.

DO NOT STRIP MARKUP AT BUILD TIME. `clean()` takes out HTML tags and collapses whitespace and
that is ALL it takes out: markdown emphasis, BBCode spoiler tags and smart quotes stay in the
pack exactly as their sources published them, and the folding happens at comparison time in
`norm.norm()`. This reads backwards and it is the one rule here a later reader will "repair". A
cleaner pack verifies FEWER quotes, not more: an extractor transcribing a span across `**` or
`[spoiler]` produces a quote that is right about the film, and it verifies today only because
both the pack and the quote pass through the same fold at the same moment. Strip at build time
and that quote has nothing to match against -- and the damage is invisible, because nothing
raises and the only symptom is a rejection count in `dna_reject` that nobody is counting yet.
`norm.py`'s own docstring measures what would be lost: 7,334 `**` markers and 1,336 `[spoiler]`
tags across the library.

AND THE PACK IS RETAINED, OR STAGE 7 IS UNAUDITABLE. `store_pack` is the second half of decision
382: the bytes go into M5.1's raw store like any other document and `dna_pack` is the row that
indexes them, because §8 stage 7 verifies a quote against THAT TITLE'S PACK and §6.6 promises a
"review of DNA rejects" that cannot explain a single rejection without the text it was measured
against. The sha is `sha(text)` -- the digest of the bytes the extraction will actually read,
computed by the call that built them. `mdc/dna/store.store_title` is the scar that makes that
worth stating: deriving the sha from whatever was on disk at ingest time marked 825 titles
current against a pack no pass had ever seen, and hid 652 that needed re-extracting.

Read-only on `title`, `title_meta` and `review_store.review`; `store_pack` is the only write.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.acquire import rawstore

MAX_REVIEWS = 60          # total, across all sources
MAX_PER_SOURCE = 12       # so no single reviewer culture dominates
MAX_CHARS = 1500          # per review
MIN_WORDS = 50            # below this a "review" is a rating with a sentence
MAX_PLOT_CHARS = 4000

_TAG_RE = re.compile(r"<[^>]+>")
# `norm.py` compiles its own whitespace pattern and that is not a second normalisation: `clean`
# tidies text that is about to be STORED and `norm` folds two strings that are about to be
# COMPARED, which are different jobs on different sides of the model. Sharing the constant would
# put one import between them and invite the next reader to share the rest.
_WS_RE = re.compile(r"\s+")


def clean(s: str | None) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s or "")).strip()


def sha(text: str) -> str:
    """The pack's identity: the first 64 bits of its sha256, ported width and all.

    THE TRUNCATION IS INHERITED AND IS KEPT DELIBERATELY, which is worth one paragraph because
    decision 382 makes this value the thing 0028's `llm_call` references instead of re-carrying
    custody, and a reader meeting a 16-character "sha256" otherwise cannot tell a measured choice
    from a copied line. What 64 bits buy is a `dna_pack` row a person can read out and compare by
    eye; what they cost is a birthday bound around 2^32 packs, which a household library does not
    approach, against 2^128 for the full digest. Two things make the residual risk a refused write
    rather than a wrong verdict: `dna_pack`'s uniqueness constraint turns a collision into an
    error that parks the stage, and `read_pack` re-derives this digest from the bytes it just read
    rather than trusting the row. `rawstore.read` verifies the FULL sha256 of the file one layer
    down, so the stored bytes are never identified by these 64 bits alone.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class PackInfo:
    title_id: int
    title: str
    year: Any
    kind: str
    n_reviews: int
    n_sources: int
    chars: int
    sha: str


# Named change 4 in the docstring: `title.name`. Nothing else about this row moved.
_TITLE = "SELECT id, name, year, kind FROM title WHERE id = $1"

# Named change 2. Every source's claim, coerced and compared in `_largest_count` rather than in
# SQL: `payload ->> 'season_count'` is text whatever the corpus column was, and a `::int` cast
# inside an aggregate over rows this app does not control raises on the first source that ever
# ships "12 (ordered)". A row that cannot be read is a count this title does not have, which is
# what the header's `?` already means.
_COUNTS = """
SELECT payload ->> 'season_count'  AS season_count,
       payload ->> 'episode_count' AS episode_count
  FROM title_meta
 WHERE title_id = $1
"""

# Named change 1, with the corpus's sort KEY kept exactly: the LONGEST plot text any source
# carries, and `plot_short` only where no source carries a full one.
#
# `, source` IS NAMED CHANGE 3'S ARGUMENT APPLIED TO THIS QUERY, and it is not tidiness. The key
# is 0 for every row whose `plot_full` is absent or empty, and it is equal for any two sources
# that shipped plots of one length, so the rows really do tie -- `title_meta` is one row PER
# SOURCE here (`0003_content.sql`'s `PRIMARY KEY (title_id, source)`), which is the difference
# from a table with one row per title. An untied `LIMIT 1` then takes whichever row the planner
# returned first, and `importer/meta.load_title_meta` does `DELETE` then a bulk `INSERT` on every
# import, so physical order is not an invariant of the data: the same rows can pick a different
# plot after a re-import that changed nothing. That changes the pack text, which changes `sha()`,
# which moves `dna_pack.pack_sha` under a verdict decision 382 exists to keep reproducible --
# named change 3's own sentence, arriving through the one query that did not carry the guard.
# `source` rather than a rowid because `(title_id, source)` is the key, so it cannot tie.
_PLOT = """
SELECT payload ->> 'plot_full'  AS plot_full,
       payload ->> 'plot_short' AS plot_short
  FROM title_meta
 WHERE title_id = $1
   AND (payload ->> 'plot_full' IS NOT NULL OR payload ->> 'plot_short' IS NOT NULL)
 ORDER BY length(coalesce(payload ->> 'plot_full', '')) DESC, source
 LIMIT 1
"""

# Named change 3. `word_count` is not one of §4.1 rule 2's weights -- it is a property of the
# text and not of a tag's evidence -- so the floor is a WHERE clause here and stays one.
_REVIEWS = """
SELECT source, body
  FROM review_store.review
 WHERE title_id = $1 AND word_count >= $2
 ORDER BY word_count DESC, id
"""


def _largest_count(rows: Sequence[asyncpg.Record], column: str) -> int | None:
    """The largest count any of a title's meta sources claims, or None.

    Named change 2's other half. Anything that is not a run of digits is not a count and is
    dropped rather than guessed at: the value reaches this app through `payload jsonb`, which is
    exactly the column a corpus-side change can widen without a migration here.
    """
    values = [int(row[column]) for row in rows if (row[column] or "").isdigit()]
    return max(values) if values else None


def render_pack(
    title_id: int,
    name: str | None,
    year: Any,
    kind: str | None,
    seasons: Any,
    eps: Any,
    plot: str | None,
    reviews: Sequence[tuple[str, str]],
) -> tuple[str, PackInfo]:
    """`mdc/dna/packs.py:144-194`'s body, over rows already fetched.

    `reviews` arrives in the order `_REVIEWS` returns it -- longest first within a source -- and
    this function does the rest: the per-source cap, the per-review character cap, the
    round-robin interleave and the total cap, in that order, which is the order the corpus
    applies them in and the only one that makes `MAX_REVIEWS` a spread rather than a prefix.
    """
    head = [f"# {name} ({year or '?'})",
            f"[type] {'series' if kind == 'series' else 'film'}"]
    if kind == "series":
        head.append(f"[note] Series: {seasons or '?'} season(s), {eps or '?'} "
                    f"episodes. Reviews below may discuss different seasons; "
                    f"describe the show AS A WHOLE.")
    head.append("")

    body: list[str] = []
    if plot:
        body += ["[plot:1]", clean(plot)[:MAX_PLOT_CHARS], ""]

    by_src: dict[str, list[str]] = defaultdict(list)
    for src, txt in reviews:
        if len(by_src[src]) < MAX_PER_SOURCE:
            by_src[src].append(clean(txt)[:MAX_CHARS])

    # Interleave rather than concatenate: taking source A's twelve before
    # source B's first means a truncated pack is a single-source pack, which is
    # exactly the bias the spread exists to avoid.
    picked: list[tuple[str, int, str]] = []
    i = 0
    while len(picked) < MAX_REVIEWS:
        added = False
        for src in sorted(by_src):
            if i < len(by_src[src]) and len(picked) < MAX_REVIEWS:
                picked.append((src, i + 1, by_src[src][i]))
                added = True
        if not added:
            break
        i += 1

    for src, n, txt in picked:
        body += [f"[{src}:{n}]", txt, ""]

    text = "\n".join(head + body)
    info = PackInfo(title_id=title_id, title=name or "", year=year,
                    kind=kind or "", n_reviews=len(picked),
                    n_sources=len({s for s, _, _ in picked}),
                    chars=len(text), sha=sha(text))
    return text, info


async def build_pack(conn: asyncpg.Connection, title_id: int) -> tuple[str, PackInfo] | None:
    """A title's pack and its shape, or None when this install has no such title.

    None ONLY for a missing title row, exactly as the corpus returns it. A title with no plot and
    no reviews gets a two-line pack rather than a None, because "there is nothing to extract
    from" is §8 stage 4's verdict and not this function's -- stage 4 is the reviews gate that
    parks a thin title on a 30-day window (decision 336), and a builder that answered the same
    question a second time would answer it with a different rule.
    """
    row = await conn.fetchrow(_TITLE, title_id)
    if row is None:
        return None

    counts = await conn.fetch(_COUNTS, title_id) if row["kind"] == "series" else []
    plot_row = await conn.fetchrow(_PLOT, title_id)
    reviews = await conn.fetch(_REVIEWS, title_id, MIN_WORDS)

    return render_pack(
        row["id"], row["name"], row["year"], row["kind"],
        _largest_count(counts, "season_count"),
        _largest_count(counts, "episode_count"),
        (plot_row["plot_full"] or plot_row["plot_short"]) if plot_row is not None else None,
        [(r["source"], r["body"]) for r in reviews],
    )


async def store_pack(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    text: str,
    info: PackInfo,
    *,
    entity_key: str | None = None,
    run_id: int | None = None,
) -> int:
    """Put the pack in custody and index it. Returns the new `raw_document` id. Decision 382.

    `entity_key` IS THE ACQUISITION TASK'S KEY and there is no second spelling of it --
    `rawstore.store`'s own docstring says so, and `acquire/board.py`'s `documents_for_title`
    joins this column to `acquisition_task.key` to answer "which documents belong to this
    title". A pack filed under anything else is a document §6.6's board can never show, and
    decision 345 makes that board the only window onto bytes the backend container cannot open.
    It stays optional for the same reason the store's parameter does: a pack built outside the
    pipeline -- a test, a one-off rebuild -- belongs to no task, and a wrong key is worse than
    none.

    NOT A TRANSACTION, because `rawstore.store` is deliberately not one either and for the same
    reason: `pipeline.run_task` hands a stage a pooled connection in autocommit, so a stage that
    wants these two writes to be all-or-nothing opens `conn.transaction()` itself. What the split
    costs is a `raw_document` row whose `dna_pack` row never landed, which is a stored pack
    nothing points at -- the cheap failure of the two, against a `dna_pack` row pointing at bytes
    that were never written.

    ONE ROW PER (title, version) AND IT IS UPSERTED. A rebuilt pack replaces its own index row
    rather than accumulating a history: `0027` gives `(title_id, version, pack_sha)` a UNIQUE
    constraint so 0028's `llm_call` can reference it -- scoped to the title, because two titles
    can hold one byte-identical pack (decision 403) -- and what a call whose pack has since been
    rebuilt points at is
    M5.5's question to answer in its own migration, not one this milestone can answer for it.
    The `raw_document` rows are the history in the meantime -- the store keeps every write and
    `built_at` says which one this row names.

    AND `info` MUST BE `text`'S OWN SHAPE, WHICH IS CHECKED HERE RATHER THAN ASSUMED. The two
    arrive as independent parameters, so nothing but this line stops a caller storing one pack's
    bytes under another pack's digest -- and the caller that would is the natural one:
    `craft.augment` returns `(text, CraftInfo)`, a type `store_pack` cannot take, so the only
    value in scope after an augment that satisfies this signature is the base pack's `PackInfo`.
    Storing it writes a row naming a pack the raw store does not hold, and `read_pack` then raises
    for that title on every pass instead of verifying -- the corpus's `store_title` scar
    (`mdc/dna/store.py:277-300`) in this app's shape, which decision 382 exists to prevent. It is
    refused rather than recomputed, because a call that hands over two different packs has made a
    mistake about which one the extraction read, and silently picking one would decide that for
    it. `chars` travels with the digest for the same reason: §6.6 shows it beside a refusal.
    """
    if info.sha != sha(text) or info.chars != len(text):
        raise ValueError(
            f"pack custody for title {title_id}: the text offered has sha {sha(text)} and "
            f"{len(text)} chars while the info names {info.sha} and {info.chars} chars; "
            "decision 382 makes pack_sha the digest of the bytes the extraction will read"
        )
    doc_id = await rawstore.store(
        conn,
        source="pack",
        kind="dna",
        url=f"pack:title:{title_id}",
        entity_key=entity_key,
        content=text.encode("utf-8"),
        # Not JSON and not a fetch: the pack is bytes this app authored, so there is no status to
        # record. `_suffix_for` turns the content type into `.txt`, which is what makes a fan-out
        # directory readable to the operator decision 345 leaves the bytes to.
        content_type="text/plain; charset=utf-8",
        http_status=None,
        run_id=run_id,
    )
    await conn.execute(
        """INSERT INTO dna_pack
             (title_id, version, pack_sha, raw_document_id, n_reviews, n_sources, chars)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (title_id, version) DO UPDATE SET
             pack_sha        = EXCLUDED.pack_sha,
             raw_document_id = EXCLUDED.raw_document_id,
             n_reviews       = EXCLUDED.n_reviews,
             n_sources       = EXCLUDED.n_sources,
             chars           = EXCLUDED.chars,
             built_at        = now()""",
        title_id, version, info.sha, doc_id, info.n_reviews, info.n_sources, info.chars,
    )
    return doc_id
