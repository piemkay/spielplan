"""Source packs: the text an extraction pass is allowed to read (§8 stage 5).

The caps are measured (A/B: +17% tags); do not lower them without re-running it. Markup is never
stripped at build time; `norm()` folds it at comparison. `store_pack` is the only write.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.acquire import rawstore
from spielplan.importer import meta

MAX_REVIEWS = 60          # total, across all sources
MAX_PER_SOURCE = 12       # so no single reviewer culture dominates
MAX_CHARS = 1500          # per review
MIN_WORDS = 50            # below this a "review" is a rating with a sentence
MAX_PLOT_CHARS = 4000

_TAG_RE = re.compile(r"<[^>]+>")
# Not a second normalisation: `clean` tidies stored text, `norm` folds compared text.
_WS_RE = re.compile(r"\s+")


def clean(s: str | None) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s or "")).strip()


def sha(text: str) -> str:
    """The pack's identity: the first 64 bits of its sha256, ported width and all.

    Deliberate: readable by eye; collisions become a refused write via `dna_pack`'s unique constraint.
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


_TITLE = "SELECT id, name, year, kind FROM title WHERE id = $1"

# Coerced in `_largest_count`, not SQL: a `::int` cast raises on the first "12 (ordered)".
_COUNTS = """
SELECT payload ->> 'season_count'  AS season_count,
       payload ->> 'episode_count' AS episode_count
  FROM title_meta
 WHERE title_id = $1
"""

# Every candidate, longest `plot_full` first; `source` breaks ties so the pack sha is stable
# across re-imports. Shared texts (a Wikipedia page for another film) are dropped in `_pick_plot`
# (decision 499).
_PLOT = """
SELECT source,
       payload ->> 'plot_full'  AS plot_full,
       payload ->> 'plot_short' AS plot_short
  FROM title_meta
 WHERE title_id = $1
   AND (payload ->> 'plot_full' IS NOT NULL OR payload ->> 'plot_short' IS NOT NULL)
 ORDER BY length(coalesce(payload ->> 'plot_full', '')) DESC, source
"""

# `word_count` is a property of the text, not a §4.1 rule 2 weight, so the floor stays a WHERE.
_REVIEWS = """
SELECT source, body
  FROM review_store.review
 WHERE title_id = $1 AND word_count >= $2
 ORDER BY word_count DESC, id
"""


def _pick_plot(rows: Sequence[Any], shared: Collection[tuple[str, str]]) -> str | None:
    """`_PLOT`'s answer once decision 499's shared texts are out: longest `plot_full`, ties by
    source, and a row's `plot_short` only where it has no full plot.
    """
    kept = []
    for row in rows:
        full = None if (row["source"], "plot_full") in shared else row["plot_full"]
        short = None if (row["source"], "plot_short") in shared else row["plot_short"]
        if full is not None or short is not None:
            kept.append((-len(full or ""), row["source"], full, short))
    if not kept:
        return None
    _length, _source, full, short = min(kept, key=lambda k: (k[0], k[1]))
    return full or short


def _largest_count(rows: Sequence[asyncpg.Record], column: str) -> int | None:
    """The largest count any of a title's meta sources claims, or None; non-digits are dropped."""
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
    """Per-source cap, per-review character cap, round-robin interleave, then the total cap, in
    that order, so `MAX_REVIEWS` is a spread rather than a prefix.
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

    # Interleave, so a truncated pack is never a single-source pack.
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
    """A title's pack and its shape, or None only when the title row is missing.

    An empty title still gets a pack: "nothing to extract" is stage 4's verdict, not this function's.
    """
    row = await conn.fetchrow(_TITLE, title_id)
    if row is None:
        return None

    counts = await conn.fetch(_COUNTS, title_id) if row["kind"] == "series" else []
    plot_rows = await conn.fetch(_PLOT, title_id)
    shared = {(s, f) for _t, s, f in await meta.shared_plot_texts(conn, [title_id])}
    reviews = await conn.fetch(_REVIEWS, title_id, MIN_WORDS)

    return render_pack(
        row["id"], row["name"], row["year"], row["kind"],
        _largest_count(counts, "season_count"),
        _largest_count(counts, "episode_count"),
        _pick_plot(plot_rows, shared),
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

    Not a transaction: callers wanting atomicity open one. Upserted, one row per (title, version).
    `info` must match `text`; a mismatch is refused, never recomputed.
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
        # `.txt` suffix, readable to the operator.
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
