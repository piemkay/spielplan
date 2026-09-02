"""Load `reviews.sqlite`. Spec v2.1 §4.1 rule 8, §10.

§10 ships the review bodies because they are "needed for future re-extraction and text
embedding" — the DNA pack (§8 stage 5) is built from them and the review-text block of the
feature contract (§4.3) is an SVD over them. A bundle whose reviews are dropped on import
cannot re-extract anything later.

Rule 8 is the delicate part:

    "UTF-8 everywhere; never 'clean' non-ASCII (the corpus legitimately contains CJK, RTL
     scripts, ZWSP, emoji); the 73 known-mojibake review rows are fixed individually in the
     importer."

So: no normalisation, no stripping, no `errors='ignore'`. The only text this module changes is
a row whose bytes are provably UTF-8 that was once decoded as cp1252 — and only when undoing
that round-trips exactly. Everything else passes through untouched, and the repair count is
reported so it can be compared against the expected 73.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import asyncpg

from spielplan.importer.report import ImportReport

# The signatures of UTF-8 read as cp1252: "Ã©" for é, "â€™" for ’, "Â " for a nbsp.
_MOJIBAKE_MARKERS = ("Ã", "â€", "Â")

# `review_store.review` column -> the `reviews.sqlite` column it is shipped in. Three of the
# eight were named after the Postgres side and selected verbatim from SQLite, where they do not
# exist: `rating`, `published_at` and `is_critic` are one column over. `_rows` selects NULL for
# an absent column, so all 485,602 rows loaded with no rating, no date and no critic flag under
# a single warn — §10 promises a report, and it got one, saying nothing was wrong with the data.
#
# `rating` takes `rating_norm` and not `rating_raw`: the raw column is the review's own notation
# ("8/10", "Rotten") and is text, while the target is `double precision`. `rating_norm` is that
# notation already divided through by `rating_scale`, so it is the only column of the three that
# is comparable across the seventeen scales the corpus carries.
REVIEW_SOURCE = {
    "title_id": "title_id",
    "source": "source",
    "author": "author",
    "url": "url",
    "rating": "rating_norm",
    "published_at": "created_date",
    "is_critic": "author_kind",
    "body": "body",
}
REVIEW_COLUMNS = tuple(REVIEW_SOURCE)

# `author_kind` is a two-value vocabulary upstream (351,706 critic / 133,896 user), and
# `is_critic` is a nullable boolean. `bool(author_kind)` would map every review to True,
# including "user" — the failure is invisible because a non-empty string is truthy, so an
# explicit vocabulary is used and an unrecognised kind stays NULL rather than becoming a critic.
_AUTHOR_KIND = {"critic": True, "user": False}


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Undo one cp1252-over-UTF-8 round trip, or return the text unchanged.

    Conservative on purpose. The repair is applied only when the string carries a mojibake
    marker AND re-encoding as cp1252 yields valid UTF-8 AND the result differs. Anything that
    fails a step is left exactly as it arrived — a legitimately Turkish or Vietnamese review
    must not be "fixed" into nonsense.
    """
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text, False
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text, False
    if repaired == text:
        return text, False
    # A real repair removes markers rather than adding them.
    if sum(repaired.count(m) for m in _MOJIBAKE_MARKERS) >= sum(
        text.count(m) for m in _MOJIBAKE_MARKERS
    ):
        return text, False
    return repaired, True


class _Counter:
    def __init__(self) -> None:
        self.rows = 0
        self.repaired = 0


def _rows(db: sqlite3.Connection, available: set[str], counts: _Counter) -> Iterator[tuple]:
    select = ", ".join(
        f'"{src}"' if src in available else "NULL" for src in REVIEW_SOURCE.values()
    )
    body_at = REVIEW_COLUMNS.index("body")
    critic_at = REVIEW_COLUMNS.index("is_critic")
    published_at = REVIEW_COLUMNS.index("published_at")

    from spielplan.importer.load import _timestamp

    for row in db.execute(f'SELECT {select} FROM review'):
        out = list(row)
        counts.rows += 1
        if isinstance(out[body_at], str):
            out[body_at], changed = repair_mojibake(out[body_at])
            counts.repaired += changed
        kind = out[critic_at]
        out[critic_at] = _AUTHOR_KIND.get(kind.strip().lower()) if isinstance(kind, str) else None
        out[published_at] = _timestamp(out[published_at])
        yield tuple(out)


async def load_reviews(
    conn: asyncpg.Connection, db: sqlite3.Connection, report: ImportReport
) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "review" not in tables:
        report.warn("reviews", "reviews.sqlite has no `review` table — nothing loaded")
        return

    available = {r[1] for r in db.execute('PRAGMA table_info("review")')}
    # Named as `target (source)`: the warn used to print the Postgres names, so an operator
    # reading "review has no column rating" had no way to tell a corpus-side change from this
    # app looking in the wrong place — which is what it was doing on every bundle.
    absent = sorted(f"{dst} ({src})" for dst, src in REVIEW_SOURCE.items() if src not in available)
    if absent:
        report.warn("reviews", f"`review` has no column(s) {absent} — imported as NULL")

    counts = _Counter()
    await conn.execute("DELETE FROM review_store.review")
    await conn.copy_records_to_table(
        "review",
        schema_name="review_store",
        columns=list(REVIEW_COLUMNS),
        records=_rows(db, available, counts),
    )
    report.table_counts["loaded:review_store.review"] = counts.rows
    report.note(
        "rule8-mojibake",
        f"{counts.repaired} review row(s) repaired from cp1252-over-UTF-8 "
        f"(expected around 73); the other {counts.rows - counts.repaired:,} were left byte-exact",
        repaired=counts.repaired, expected=73, total=counts.rows,
    )
