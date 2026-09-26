"""Load `reviews.sqlite` (§4.1 rule 8, §10).

No normalisation or stripping: only a provable cp1252-over-UTF-8 round trip is undone. Marked but
unrepairable rows are reported as a warning.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import asyncpg

from spielplan.importer.report import ImportReport

# The signatures of UTF-8 read as cp1252: "Ã©" for é, "â€™" for ’, "Â " for a nbsp.
_MOJIBAKE_MARKERS = ("Ã", "â€", "Â")

# `review_store.review` column -> the `reviews.sqlite` column it is shipped in. `rating` takes
# `rating_norm` (comparable across scales), not the textual `rating_raw`.
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

# Explicit, because `bool("user")` is True; an unrecognised kind stays NULL.
_AUTHOR_KIND = {"critic": True, "user": False}


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Undo one cp1252-over-UTF-8 round trip, or return the text unchanged.

    Only when a marker is present, the re-encode is valid UTF-8, and the result differs.
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
        # Marked rows whether or not repaired, so "0 repaired" is distinguishable from a clean corpus.
        self.marked = 0


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
            counts.marked += any(m in out[body_at] for m in _MOJIBAKE_MARKERS)
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
    # Named as `target (source)` so the operator can tell which side is wrong.
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
    # Markers seen and nothing repairable: the shipped corpus's state (truncated sequences), so warn.
    # The fix belongs upstream.
    if counts.marked and not counts.repaired:
        report.warn(
            "rule8-mojibake",
            f"{counts.marked:,} review row(s) carry a cp1252-over-UTF-8 marker and none could "
            "be repaired without guessing at bytes the corpus lost; the bodies are stored "
            "exactly as shipped and the repair belongs upstream",
            marked=counts.marked, repaired=0, total=counts.rows,
        )
    else:
        report.note(
            "rule8-mojibake",
            f"{counts.repaired} of {counts.marked:,} marked review row(s) repaired from "
            f"cp1252-over-UTF-8; the other {counts.rows - counts.repaired:,} were left "
            "byte-exact",
            repaired=counts.repaired, marked=counts.marked, total=counts.rows,
        )
