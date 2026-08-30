"""§6.1's class-balance widget, and the one sentence it exists to be able to say.

§6.1: "Running **class-balance widget** with its warning copy ('Heavy on "liked". Spreading
across all three classes matters about five times more than anything else you can do here.' —
the measured 5x lever)."

§5.2 supplies both the lever and the threshold: "spreading verdicts across all three classes
matters ~5x more than anything the corpus side can tune (a 60%-'liked' labeller gives up ~0.07
rho) -> the rating UI shows a running class balance."

So the widget is not decoration. It is the only place in the app where the largest single lever
on a person's model is legible to the person pulling it, and the number that fires it — 60% —
is a measurement, not a taste.

WHAT COUNTS AS A LABEL
One per title: the person's current answer, read through `rate.LIVE_LABEL`. Two exclusions come
with it and both are load-bearing:

  * **Re-asks are not counted.** §13 stream (b) poses a question the person has already
    answered, to measure whether the answer holds. Counting it would let the instrument push the
    distribution it is reporting on, and would make a household's balance depend on which rows
    the re-ask scheduler happened to sample.
  * **A superseded verdict is not counted.** A re-rating replaces a label rather than adding
    one — "the running three-class verdict distribution over the user's labels" is a
    distribution over titles, not over taps.

`LIVE_LABEL` reads the newest non-re-ask row per title rather than the row with a NULL
`superseded_by`, because `record_verdict` stamps `superseded_by` for a re-ask too. Both rules
therefore come from one query, and there is no second place for them to drift.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.rate import LIVE_LABEL, VERDICT_LABELS

log = logging.getLogger("spielplan.rate.balance")

# ---------------------------------------------------------------------------------------------
# TUNED NUMBER. §5.2 fixes 60% ("a 60%-'liked' labeller gives up ~0.07 rho"). It belongs in
# `spielplan/ledger/hyperparams.py` with the rest of §5.2's constants; that module is wave-1
# frozen for this milestone, so it lives here. Reported as a gap.
# ---------------------------------------------------------------------------------------------
WARN_SHARE = 0.60

# The coverage row is literal — "present once one class exceeds 60% of that distribution and
# absent below it" — with no floor, so there is none. The decision doc's proposal 41 argues for
# arming the warning only after ~10 verdicts, on the grounds that a warning on the third tap is
# noise; if that reading wins, this is the one number that changes and no other line does.
WARN_MIN_VERDICTS = 1

# §6.1's copy, verbatim. The sentence is the measurement written down, so it is a constant and
# not an f-string: the "about five times more" is §5.2's 5x lever and must not drift into "much
# more" the first time someone edits the surrounding paragraph.
WARN_COPY = (
    "Spreading across all three classes matters about five times more than anything else "
    "you can do here."
)


@dataclass(frozen=True)
class ClassBalance:
    counts: tuple[int, int, int]
    shares: tuple[float, float, float]
    warn: bool             # a class exceeds 60% of the distribution
    copy: str | None       # the §6.1 warning sentence when warn, else None

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def heaviest(self) -> int | None:
        """The class the warning is about, or None when there is nothing to warn about."""
        if self.total == 0:
            return None
        return max(range(3), key=lambda i: (self.counts[i], -i))

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": list(self.counts),
            "shares": list(self.shares),
            "labels": list(VERDICT_LABELS),
            "total": self.total,
            "warn": self.warn,
            "copy": self.copy,
            "threshold": WARN_SHARE,
        }

    @classmethod
    def of(cls, counts: Sequence[int]) -> ClassBalance:
        """Build the widget from three counts. Pure, so the 60% rule is testable without a
        database and the boundary case — exactly 60%, which does not warn — has somewhere to
        be asserted."""
        n0, n1, n2 = (int(c) for c in counts)
        if min(n0, n1, n2) < 0:
            raise ValueError(f"class counts cannot be negative: {(n0, n1, n2)!r}")
        total = n0 + n1 + n2
        if total == 0:
            return cls(counts=(0, 0, 0), shares=(0.0, 0.0, 0.0), warn=False, copy=None)
        shares = (n0 / total, n1 / total, n2 / total)
        top = max(range(3), key=lambda i: ((n0, n1, n2)[i], -i))
        # "exceeds 60%" — strictly. A person sitting exactly on the measured threshold has not
        # yet given anything up, and a warning fired at equality would be a warning about the
        # inequality sign rather than about their labelling.
        warn = total >= WARN_MIN_VERDICTS and shares[top] > WARN_SHARE
        copy = f"Heavy on '{VERDICT_LABELS[top]}'. {WARN_COPY}" if warn else None
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


__all__ = ["WARN_COPY", "WARN_MIN_VERDICTS", "WARN_SHARE", "ClassBalance", "class_balance"]
