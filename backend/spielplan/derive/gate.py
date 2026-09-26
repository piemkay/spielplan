"""§8 stage 4's reviews gate: the predicate, the sentence it parks with, and the window.

Fifty words is a total across the title's reviews (decision 335), not the pack's per-review floor.
A thin title parks with a deadline, never fails (decision 336); the plot test reads `title.overview`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

# Decision 335's two floors; deliberately not named `MIN_WORDS`, the pack's per-review floor.
MIN_SOURCES = 2
MIN_TOTAL_WORDS = 50

# Counted from when the gate found the title thin, not from a release date.
REVIEW_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class GateCounts:
    """What the gate measured about one title. Three facts, and the reason names all three."""

    has_plot: bool
    sources: int
    words: int


# Decision 335's predicate: `count(DISTINCT source)` (multi-source), `sum(word_count)` off the
# generated column, and a LEFT JOIN so no reviews measures as zeros. The plot test is "holds a
# non-whitespace character": one-argument `btrim` strips only spaces, so a lone newline would pass.
_MEASURE = r"""
    SELECT coalesce(t.overview, '') ~ '\S' AS has_plot,
           coalesce(r.sources, 0)          AS sources,
           coalesce(r.words, 0)            AS words
      FROM title t
      LEFT JOIN (SELECT title_id,
                        count(DISTINCT source) AS sources,
                        sum(word_count)        AS words
                   FROM review_store.review
                  WHERE title_id = $1
                  GROUP BY title_id) r ON r.title_id = t.id
     WHERE t.id = $1
"""


async def measure(conn: asyncpg.Connection, title_id: int) -> GateCounts:
    """What the gate can see about this title right now.

    Raises `LookupError` for a missing title: absent is not thin.
    """
    row = await conn.fetchrow(_MEASURE, title_id)
    if row is None:
        raise LookupError(f"reviews gate: no title row for title_id {title_id}")
    return GateCounts(has_plot=row["has_plot"], sources=int(row["sources"]), words=int(row["words"]))


def passes(counts: GateCounts) -> bool:
    """Decision 335's conjunction: a plot, two sources, fifty words across them."""
    return counts.has_plot and counts.sources >= MIN_SOURCES and counts.words >= MIN_TOTAL_WORDS


def reason(counts: GateCounts) -> str:
    """Why this title is not being packed yet, shown verbatim on the admin board.

    Carries both counts and names the plot only when missing. Plain ASCII: it also reaches a
    Windows console via the worker log.
    """
    plot = "" if counts.has_plot else "no plot yet, "
    return (
        f"reviews gate: {plot}{_counted(counts.sources, 'source')}, "
        f"{_counted(counts.words, 'word')} - retry window {REVIEW_WINDOW.days} days. When it "
        "closes the sources are asked again, so reviews written in the meantime are counted "
        "without anyone retrying this job"
    )


def window_deadline() -> datetime:
    """The instant a title found thin here is asked again; timezone-aware.

    Both `queue.defer` and `acquisition_job.retry_after` take it from this one call.
    """
    return datetime.now(UTC) + REVIEW_WINDOW


def _counted(n: int, noun: str) -> str:
    """"1 source", "2 sources"."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
