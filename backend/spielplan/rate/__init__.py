"""The Rate surface's question-picking half. Spec v2.1 §6.1, §6.8, §13 stream (b).

Four modules, split by which sentence of §6.1 each one answers:

* `queue`   — "P(seen)-ordered (Jellyfin history, popularity, household co-seen), seeded first
              run from the imported 100-title decade-stratified `seed_list`", plus §6.8's
              mandatory one-line why on every card.
* `battle`  — "Pairs drawn **at random** from the user's seen titles within verdict bands."
* `balance` — the "running class-balance widget with its warning copy".
* `reask`   — §13 stream (b): "~10% of comparisons/verdicts re-asked after >= 3 days".

Nothing here writes. Every write goes through `spielplan.ledger.observations`, which is §5.2's
one write-path for taste; these modules only decide *what to ask*. That split is what lets the
re-ask stream be invisible in the payload and distinguishable in the row at the same time — the
re-ask reference travels as a field on the card the server holds, and the writer is told about
it separately.

ONE DEFINITION OF "THE PERSON'S CURRENT LABEL"
`LIVE_LABEL` below is it, and `battle` and `balance` both read through it rather than each
spelling out a predicate. Two rules meet in that one query and neither is obvious:

  * **`NOT is_reask`.** §13's re-ask "is a separate silent stream"; it measures the stability of
    a judgement and must not *be* one. `ledger.observations.load_observations` excludes it from
    the fit for the same reason, so the band a battle draws from and the distribution the widget
    reports agree with what the model was actually fitted on.
  * **The newest row wins, rather than `superseded_by IS NULL`.** `record_verdict` stamps
    `superseded_by` for a re-ask too (deliberately: it is the person's latest answer and the
    card must show it). So after a re-ask the only row with a NULL `superseded_by` is the re-ask
    itself, and a predicate written on that column would either drop the title out of every band
    or let the instrument move the model. `DISTINCT ON … ORDER BY created_at DESC` over the
    non-re-ask rows is stable under both re-asks and re-ratings.

`$1` is the user id. Both callers must bind it there; the fragment says so because a SQL string
shared between two modules is a place where a parameter number silently changes meaning.
"""

from __future__ import annotations

from spielplan.ledger import observations

# Defined in `ledger.observations`, because the rule is about the Ledger's own observations and
# `scoring.foldin` needs the same one. Re-exported here so this package's existing readers are
# unchanged.
LIVE_LABEL = observations.LIVE_LABEL_SQL

# §4.2: "value: 0 disliked / 1 ok / 2 liked". §6.1's visible labels are lowercase and ordered
# worst -> best, which is also the stored ordinal, so one tuple serves the copy and the index.
VERDICT_LABELS: tuple[str, str, str] = ("disliked", "fine", "liked")

__all__ = ["LIVE_LABEL", "VERDICT_LABELS"]
