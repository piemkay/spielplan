"""§6.6 Data's two ledger editors: DNA verdicts and credit facts.

Two modules and no shared write path (§14.5; decision 423); household rows only, never a bundle
row (decisions 326, 445). This file imports nothing, so no editor reaches a sibling's writer.
"""


class Refused(ValueError):
    """An editor declined the write and wrote nothing; `reason` is shown to the admin verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
