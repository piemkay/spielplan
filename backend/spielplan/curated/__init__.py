"""§6.6 Data's three ledger editors: DNA verdicts, credit facts and the per-facet axes.

Spec v2.1 §6.6 Data as v2.1.3 amends it: "Three separate editors with separate semantics, never one
merged screen - DNA verdicts (`adjudications_v1.tsv`), credit facts (`corrections_v1.tsv`, applied
last at every derive, §8 stage 3) and the per-facet axis TSVs (§6.4) - each showing its rows, its
provenance and the derive that will re-apply it; they write household rows beside the bundle's, which
are read-only in the app, and export the household's rows on demand in the importer's own columns".
Decisions 326, 342, 423 and 445.

THREE MODULES AND NO MERGED WRITE PATH, AND THE SEPARATION IS THE REQUIREMENT. §14.5 is the scar:
"adjudication- AND corrections-at-derivation - two distinct ledgers: DNA verdicts at ingest,
source-credit facts at rebuild - or inherit the bugs they were built to kill". The two appliers in
`derive/ledgers.py` already compose in opposite directions (decision 423: first-effective-wins
against last-wins), and a shared writer is where that difference would first be flattened. So
`adjudications` writes `dna_adjudication`, `corrections` writes `credit_correction`, and `axes`
writes `dna_axis` and its weights; none imports another, and this file holds no function any of
them could share - which is plan check 10, "`grep` finds no shared write function", and what
`test_curated_editors.py` reads the three sources for.

HOUSEHOLD ROWS ONLY (decisions 326 and 445). Every editor writes `origin = 'household'` and never
changes or removes a row whose origin is `'bundle'`: the importer's models-only DELETEs are scoped to
the bundle's rows, so a bundle row edited in the app would be restored by the next re-import and a
household row is never taken by one. The household withdraws only what it wrote.

THIS FILE IMPORTS NOTHING, so that no module can reach a sibling's writer through the package.
"""


class Refused(ValueError):
    """An editor declined the write and wrote nothing; `reason` is shown to the admin verbatim.

    The sentence is composed by the editor that refused, in the operator's terms, and no reader
    rephrases it - the same contract `acquire/actions.ActionRefused` keeps for the board.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
