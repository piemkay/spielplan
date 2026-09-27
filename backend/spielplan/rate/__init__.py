"""The Rate surface's question-picking half (§6.1, §6.8, §13 stream b). Nothing here writes.

`LIVE_LABEL` is the person's current label: the newest non-re-ask row per title; `$1` is the user.
"""

from __future__ import annotations

from spielplan.ledger import observations

LIVE_LABEL = observations.LIVE_LABEL_SQL
VERDICT_LABELS = observations.VERDICT_LABELS

__all__ = ["LIVE_LABEL", "VERDICT_LABELS"]
