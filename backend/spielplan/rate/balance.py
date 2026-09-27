"""§6.1's class-balance widget over a person's live labels (re-asks and superseded rows excluded)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rate import LIVE_LABEL, VERDICT_LABELS

log = logging.getLogger("spielplan.rate.balance")

# §5.2: "a 60%-'liked' labeller gives up ~0.07 rho".
WARN_SHARE = 0.60

# Decision 491: one label is 100% of a distribution, so the sentence waits for fifteen.
WARN_MIN_VERDICTS = 15

# §5.2's measured 5x lever; keep the wording exact.
WARN_COPY = (
    "Spreading your ratings across all three answers matters about five times more than "
    "anything else you can do here."
)

# Decision 491: each tail widens what gets rated; none asks for a different answer.
WARN_TAIL = (
    "Rate some titles you enjoyed as well",
    "When a title was better or worse than fine, say so",
    "Rate some titles you didn't enjoy as well",
)
WARN_HONEST = "but never change an honest answer to even things out."


@dataclass(frozen=True)
class ClassBalance:
    counts: tuple[int, int, int]
    shares: tuple[float, float, float]
    warn: bool             # a class exceeds 60% of the distribution
    copy: str | None       # the §6.1 warning sentence when warn, else None

    @property
    def total(self) -> int:
        return sum(self.counts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": list(self.counts),
            "shares": list(self.shares),
            "labels": list(VERDICT_LABELS),
            "total": self.total,
            "warn": self.warn,
            "copy": self.copy,
            "threshold": WARN_SHARE,
            "arms_at": WARN_MIN_VERDICTS,
        }

    @classmethod
    def of(cls, counts: Sequence[int]) -> ClassBalance:
        """Pure, so the 60% boundary (which does not warn) is testable without a database."""
        n0, n1, n2 = (int(c) for c in counts)
        if min(n0, n1, n2) < 0:
            raise ValueError(f"class counts cannot be negative: {(n0, n1, n2)!r}")
        total = n0 + n1 + n2
        if total == 0:
            return cls(counts=(0, 0, 0), shares=(0.0, 0.0, 0.0), warn=False, copy=None)
        shares = (n0 / total, n1 / total, n2 / total)
        top = max(range(3), key=lambda i: ((n0, n1, n2)[i], -i))
        # Strictly: exactly 60% does not warn.
        warn = total >= WARN_MIN_VERDICTS and shares[top] > WARN_SHARE
        copy = (
            f"Heavy on '{VERDICT_LABELS[top]}'. {WARN_COPY} {WARN_TAIL[top]} - {WARN_HONEST}"
            if warn
            else None
        )
        return cls(counts=(n0, n1, n2), shares=shares, warn=warn, copy=copy)


_COUNTS = f"""
WITH label AS ({LIVE_LABEL})
SELECT l.value, count(*) AS n
  FROM label l
  JOIN title t ON t.id = l.title_id
 WHERE t.kind = ANY($2::text[])
 GROUP BY l.value
"""


async def class_balance(
    conn: asyncpg.Connection, *, user_id: int, kinds: Sequence[str]
) -> ClassBalance:
    """The running three-class distribution over this person's current labels."""
    if not kinds:
        raise ValueError("select at least one kind: 'movie', 'series', or both")
    rows = await conn.fetch(_COUNTS, user_id, list(kinds))
    counts = [0, 0, 0]
    for row in rows:
        counts[int(row["value"])] = int(row["n"])
    return ClassBalance.of(counts)


__all__ = [
    "WARN_COPY",
    "WARN_HONEST",
    "WARN_MIN_VERDICTS",
    "WARN_SHARE",
    "WARN_TAIL",
    "ClassBalance",
    "class_balance",
]
